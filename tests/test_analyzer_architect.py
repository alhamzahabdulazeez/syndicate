from __future__ import annotations

import pytest
from conftest import LocalGitRuntimeClient

from syndicate import nodes, stub_llm
from syndicate.state import GithubIssue, TicketStatus

FIXTURE_ISSUE: GithubIssue = {
    'repo': 'octo-org/octo-repo',
    'number': 42,
    'title': 'load_config("") raises KeyError instead of a clear error',
    'body': (
        'Calling load_config("") raises a raw KeyError. Expected a ValueError '
        'with a helpful message. Relevant module: octo_repo/config.py'
    ),
    'workspace_dir': '/workspace/octo-repo',
}

_ANALYSIS_JSON = (
    '{"problem_summary": "load_config(\\"\\") raises KeyError instead of ValueError", '
    '"root_cause_hypothesis": "empty string key lookup is not guarded", '
    '"suspected_files": ["octo_repo/config.py"]}'
)

_TICKET_JSON = (
    '{"title": "Raise ValueError on empty config key", '
    '"intent": "Guard load_config against an empty key and raise ValueError", '
    '"files_changed": ["octo_repo/config.py"], '
    '"verification_command": "python -m pytest tests/test_config.py -q"}'
)


@pytest.fixture(autouse=True)
def _stub_llm_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SYNDICATE_STUB_LLM', '1')


def _script_analyzer(issue: GithubIssue, text: str = _ANALYSIS_JSON) -> None:
    stub_llm.set_stub_script(
        nodes._issue_key(issue, 'analyzer'), [stub_llm.complete_turn(text)]
    )


def _script_architect(issue: GithubIssue, text: str = _TICKET_JSON) -> None:
    stub_llm.set_stub_script(
        nodes._issue_key(issue, 'architect'), [stub_llm.complete_turn(text)]
    )


async def test_analyzer_turns_a_real_github_issue_into_structured_analysis():
    _script_analyzer(FIXTURE_ISSUE)

    result = await nodes.analyzer_node({'issue': FIXTURE_ISSUE})

    assert 'active_ticket' not in result
    assert result['issue_analysis']['suspected_files'] == ['octo_repo/config.py']
    assert 'KeyError' in result['issue_analysis']['problem_summary']


async def test_architect_turns_analysis_into_the_ticket_shape_executor_expects(
    scripted_client,
):
    analysis = {
        'problem_summary': 'load_config("") raises KeyError instead of ValueError',
        'root_cause_hypothesis': 'empty string key lookup is not guarded',
        'suspected_files': ['octo_repo/config.py'],
    }
    _script_architect(FIXTURE_ISSUE)

    result = await nodes.architect_node(
        {'issue': FIXTURE_ISSUE, 'issue_analysis': analysis}
    )

    ticket = result['active_ticket']
    assert ticket['id'] == 'octo-org/octo-repo#42'
    assert ticket['files_changed'] == ['octo_repo/config.py']
    assert ticket['workspace_dir'] == '/workspace/octo-repo'
    assert ticket['verification_command'] == 'python -m pytest tests/test_config.py -q'
    assert ticket['intent']
    assert ticket['title'] == 'Raise ValueError on empty config key'


async def test_architect_derives_verification_command_from_the_real_repo_manifest_when_model_omits_it(
    scripted_client,
):
    """The model leaves verification_command out; the repo checkout itself
    (peeked at over RuntimeClient, the only channel into the sandbox) has a
    Cargo.toml, so the derived command is cargo-specific -- not the old
    Python-shaped fixture constant."""
    await scripted_client.write_file(
        f'{FIXTURE_ISSUE["workspace_dir"]}/Cargo.toml', '[package]\nname = "octo"\n'
    )
    _script_architect(
        FIXTURE_ISSUE,
        text='{"title": "fix it", "files_changed": ["octo_repo/config.py"]}',
    )

    result = await nodes.architect_node(
        {'issue': FIXTURE_ISSUE, 'issue_analysis': {'problem_summary': 'x'}}
    )

    ticket = result['active_ticket']
    assert ticket['workspace_dir'] == FIXTURE_ISSUE['workspace_dir']
    assert ticket['verification_command'] == 'cargo test'


async def test_architect_refuses_to_fabricate_a_verification_command(scripted_client):
    """No manifest in the checkout and the model didn't supply one either --
    this must fail loudly, not silently default to a generic pytest guess
    that would falsely pass or fail against a repo we know nothing about."""
    _script_architect(
        FIXTURE_ISSUE,
        text='{"title": "fix it", "files_changed": ["octo_repo/config.py"]}',
    )

    with pytest.raises(ValueError, match='could not derive a verification_command'):
        await nodes.architect_node(
            {'issue': FIXTURE_ISSUE, 'issue_analysis': {'problem_summary': 'x'}}
        )


async def test_no_issue_falls_back_to_the_original_stub_ticket_pass_through():
    """No `issue` in state -- both nodes keep today's exact stub behavior, no
    LLM call, so every caller that never sets state['issue'] is unaffected."""
    analyzer_result = await nodes.analyzer_node({})
    assert analyzer_result == {'active_ticket': {'id': 'TICKET-1', 'title': 'stub ticket'}}

    architect_result = await nodes.architect_node(
        {'active_ticket': analyzer_result['active_ticket']}
    )
    assert architect_result == {}


async def test_analyzer_architect_pipeline_end_to_end_produces_a_ticket_the_executor_can_run(
    tmp_path, scripted_client, install_runtime_client
):
    """The full claim: a real GitHub issue, run through analyzer then
    architect with no network, produces exactly the ticket shape
    executor_node/oversight_git_node already consume -- proven by actually
    running that ticket through the unchanged executor -> oversight_git path
    to a landed commit."""
    issue: GithubIssue = dict(FIXTURE_ISSUE, workspace_dir=str(tmp_path))
    _script_analyzer(issue)
    _script_architect(issue)

    analyzer_result = await nodes.analyzer_node({'issue': issue})
    state: dict = {'issue': issue, **analyzer_result}

    architect_result = await nodes.architect_node(state)
    state.update(architect_result)

    ticket = state['active_ticket']
    assert ticket['files_changed'] == ['octo_repo/config.py']
    # Deterministic real-subprocess pass, no pytest project needed in tmp_path
    # -- mirrors test_retry_bounds.py's e2e pattern.
    ticket['verification_command'] = 'true'

    # The executor's stub script only issues a completion (no tool calls),
    # mirroring test_retry_bounds.py's e2e pattern -- the file the ticket
    # declares must already exist on disk for oversight_git's `git add` to
    # have something real to stage.
    config_path = tmp_path / 'octo_repo' / 'config.py'
    config_path.parent.mkdir(parents=True)
    config_path.write_text('def load_config(key):\n    return {}[key]\n')

    ticket_id = ticket['id']
    stub_llm.set_stub_script(ticket_id, [stub_llm.complete_turn('made the fix')])
    state.update({'attempt_log': [], 'strike_count': 0})

    executor_result = await nodes.executor_node(state)
    assert executor_result['ticket_status'] == TicketStatus.LOCAL_PASS
    state.update(executor_result)

    # validator_node's real path needs a real git/subprocess client, same one
    # oversight_git will use -- see test_retry_bounds.py's e2e test.
    install_runtime_client(LocalGitRuntimeClient())
    validator_result = await nodes.validator_node(state)
    assert validator_result['last_validation_passed'] is True
    state.update(validator_result)

    oversight_result = await nodes.oversight_git_node(state)
    assert oversight_result['ticket_status'] == TicketStatus.DONE
