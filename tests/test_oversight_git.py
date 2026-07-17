from __future__ import annotations

from pathlib import Path

import pytest
from conftest import LocalGitRuntimeClient, LyingCommitRuntimeClient
from langgraph.errors import GraphRecursionError

from syndicate import nodes
from syndicate.state import TicketStatus


def _ticket(workspace_dir: Path, files_changed: list[str] | None = None) -> dict:
    return {
        'id': 'T1',
        'title': 'do thing',
        'workspace_dir': str(workspace_dir),
        'files_changed': files_changed if files_changed is not None else [],
    }


async def test_ticket_with_no_files_changed_returns_without_commit_or_done(
    tmp_path, install_runtime_client
):
    client = install_runtime_client(LocalGitRuntimeClient())
    state = {'active_ticket': _ticket(tmp_path), 'attempt_log': []}

    result = await nodes.oversight_git_node(state)

    assert result.get('ticket_status') != TicketStatus.DONE
    assert not any(c.startswith('git commit') for c in client.commands)


async def test_ticket_with_files_changed_stages_exactly_that_list_and_reaches_done(
    tmp_path, install_runtime_client
):
    client = install_runtime_client(LocalGitRuntimeClient())
    (tmp_path / 'foo.py').write_text('print(1)\n')
    (tmp_path / 'bar.py').write_text('print(2)\n')
    state = {
        'active_ticket': _ticket(tmp_path, files_changed=['foo.py']),
        'attempt_log': ['strike=1 summary=ok'],
    }

    result = await nodes.oversight_git_node(state)

    assert result['ticket_status'] == TicketStatus.DONE
    add_commands = [c for c in client.commands if c.startswith('git add')]
    assert add_commands == ['git add -- foo.py']
    assert not any(
        c in ('git add .', 'git add -A', 'git add --all') for c in client.commands
    )


async def test_git_identity_is_repo_local_never_global(
    tmp_path, install_runtime_client
):
    client = install_runtime_client(LocalGitRuntimeClient())
    (tmp_path / 'foo.py').write_text('print(1)\n')
    state = {
        'active_ticket': _ticket(tmp_path, files_changed=['foo.py']),
        'attempt_log': [],
    }

    await nodes.oversight_git_node(state)

    assert any(c.startswith('git config user.name') for c in client.commands)
    assert any(c.startswith('git config user.email') for c in client.commands)
    assert not any('--global' in c for c in client.commands)


async def test_commit_rejected_by_hook_does_not_reach_done(
    tmp_path, install_runtime_client
):
    client = install_runtime_client(LocalGitRuntimeClient())
    (tmp_path / 'foo.py').write_text('print(1)\n')
    state = {
        'active_ticket': _ticket(tmp_path, files_changed=['foo.py']),
        'attempt_log': [],
    }
    # Seed the repo and hook before oversight_git_node's own `git init`.
    await client.run_action('git init', cwd=str(tmp_path))
    hooks_dir = tmp_path / '.git' / 'hooks'
    hook = hooks_dir / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o755)

    result = await nodes.oversight_git_node(state)

    assert result.get('ticket_status') != TicketStatus.DONE
    assert result['strike_count'] == 1
    assert any(c.startswith('git commit') for c in client.commands)


async def test_declared_files_changed_with_no_actual_diff_does_not_reach_done(
    tmp_path, install_runtime_client
):
    install_runtime_client(LocalGitRuntimeClient())
    (tmp_path / 'foo.py').write_text('print(1)\n')
    state = {
        'active_ticket': _ticket(tmp_path, files_changed=['foo.py']),
        'attempt_log': [],
    }
    first = await nodes.oversight_git_node(state)
    assert first['ticket_status'] == TicketStatus.DONE

    # Second ticket declares the same file changed, but nothing about it
    # actually changed since the last commit -- the executor's claim is false.
    second_state = {
        'active_ticket': _ticket(tmp_path, files_changed=['foo.py']),
        'attempt_log': [],
        'strike_count': 0,
    }
    second = await nodes.oversight_git_node(second_state)

    assert second.get('ticket_status') != TicketStatus.DONE
    assert second['strike_count'] == 1
    assert any('no commit' in c or 'no changes' in c for c in second['attempt_log'])


async def test_staging_failure_does_not_reach_done(tmp_path, install_runtime_client):
    client = install_runtime_client(LocalGitRuntimeClient())
    state = {
        'active_ticket': _ticket(tmp_path, files_changed=['does_not_exist.py']),
        'attempt_log': [],
    }

    result = await nodes.oversight_git_node(state)

    assert result.get('ticket_status') != TicketStatus.DONE
    assert result['strike_count'] == 1
    assert not any(c.startswith('git commit') for c in client.commands)


async def test_ticket_with_failing_commit_does_not_reach_done(
    tmp_path, install_runtime_client
):
    """The exit-code layer alone: a scripted client that reports git commit
    as a non-zero exit. This is the original failure this suite found --
    oversight_git_node used to ignore this exit code entirely.
    """
    client = install_runtime_client(LocalGitRuntimeClient())
    (tmp_path / 'foo.py').write_text('print(1)\n')
    await client.run_action('git init', cwd=str(tmp_path))
    hook = tmp_path / '.git' / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o755)
    state = {
        'active_ticket': _ticket(tmp_path, files_changed=['foo.py']),
        'attempt_log': [],
    }

    result = await nodes.oversight_git_node(state)

    assert result.get('ticket_status') != TicketStatus.DONE


async def test_commit_exit_zero_but_head_unmoved_does_not_reach_done(
    tmp_path, install_runtime_client
):
    """Belt-and-suspenders: a runtime client that reports git commit
    succeeded (exit 0) without a commit actually landing. Only the
    HEAD-before/after check catches this -- the exit code alone would not.
    """
    client = install_runtime_client(LyingCommitRuntimeClient())
    (tmp_path / 'foo.py').write_text('print(1)\n')
    state = {
        'active_ticket': _ticket(tmp_path, files_changed=['foo.py']),
        'attempt_log': [],
    }

    result = await nodes.oversight_git_node(state)

    assert result.get('ticket_status') != TicketStatus.DONE
    assert result['strike_count'] == 1
    commit_commands = [c for c in client.commands if c.startswith('git commit')]
    assert (
        len(commit_commands) == 1
    )  # the lying commit really was attempted, and really "succeeded"


def test_router_sends_done_ticket_to_advance():
    assert (
        nodes._oversight_git_router({'ticket_status': TicketStatus.DONE}) == 'advance'
    )


def test_router_sends_ticket_with_no_declared_files_straight_to_advance():
    """A ticket that never declared files_changed made no commit attempt --
    that's a no-op, not a failure, and must not be retried (it would loop
    oversight_git -> executor forever, since it can never become DONE)."""
    state = {
        'ticket_status': TicketStatus.LOCAL_PASS,
        'strike_count': 0,
        'active_ticket': {'id': 'T1'},
    }
    assert nodes._oversight_git_router(state) == 'advance'


def test_router_retries_failed_commit_under_strike_budget():
    state = {
        'ticket_status': TicketStatus.LOCAL_PASS,
        'strike_count': 1,
        'active_ticket': {'id': 'T1', 'files_changed': ['foo.py']},
    }
    assert nodes._oversight_git_router(state) == 'executor'


def test_router_escalates_failed_commit_once_strike_budget_exhausted():
    state = {
        'ticket_status': TicketStatus.LOCAL_PASS,
        'strike_count': nodes._MAX_STRIKES,
        'active_ticket': {'id': 'T1', 'files_changed': ['foo.py']},
    }
    assert nodes._oversight_git_router(state) == 'escalate'


# ---------------------------------------------------------------------------
# Regression: oversight_git -> executor -> validator -> oversight_git -> ...
# for a ticket that never declares files_changed (the default stub ticket
# analyzer_node hands back, and what scripts/smoke_e2e_mock.py runs). A
# near-miss infinite loop caught by manually running that script once is not
# a regression test -- nothing stops the next refactor of
# _oversight_git_router from reintroducing it. These run the real compiled
# graph with a hard recursion_limit, so a reintroduced loop fails the test
# deterministically (GraphRecursionError) instead of hanging the suite.
# ---------------------------------------------------------------------------


async def test_graph_terminates_for_ticket_with_no_files_changed(monkeypatch):
    monkeypatch.setenv('SYNDICATE_MOCK_CLIENT', '1')
    graph = nodes.build_graph()

    final_state = await graph.ainvoke({}, config={'recursion_limit': 25})

    assert final_state['ticket_status'] == TicketStatus.LOCAL_PASS
    assert final_state['last_validation_passed'] is True


async def test_recursion_limit_would_catch_a_reintroduced_infinite_loop(monkeypatch):
    """Proves the guard above is load-bearing, not just a test that happens
    to pass: force the router back to its pre-fix behavior (always retry,
    regardless of whether there was ever a commit attempt to retry) and
    confirm the bounded run actually fails instead of hanging.
    """
    monkeypatch.setenv('SYNDICATE_MOCK_CLIENT', '1')
    monkeypatch.setattr(nodes, '_oversight_git_router', lambda state: 'executor')
    graph = nodes.build_graph()

    with pytest.raises(GraphRecursionError):
        await graph.ainvoke({}, config={'recursion_limit': 25})
