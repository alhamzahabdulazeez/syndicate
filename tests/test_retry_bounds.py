from __future__ import annotations

import pytest
from conftest import LocalGitRuntimeClient

from syndicate import nodes, stub_llm
from syndicate.runtime.base import ActionResult
from syndicate.state import TicketStatus


def _ticket(ticket_id: str, files_changed: list[str] | None = None) -> dict:
    ticket: dict = {'id': ticket_id, 'title': 'do thing'}
    if files_changed is not None:
        ticket['files_changed'] = files_changed
    return ticket


@pytest.fixture(autouse=True)
def _stub_llm_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SYNDICATE_STUB_LLM', '1')


async def test_ticket_failing_once_then_succeeding_takes_exactly_two_executor_attempts(
    scripted_client,
):
    ticket_id = 'retry-fail-once-then-succeed'
    stub_llm.set_stub_script(ticket_id, [stub_llm.complete_turn('made the fix')])
    scripted_client.queue('git status', ActionResult(exit_code=1, output='dirty'))

    state = {'active_ticket': _ticket(ticket_id), 'attempt_log': [], 'strike_count': 0}
    result = await nodes.executor_node(state)

    assert result['ticket_status'] == TicketStatus.LOCAL_PASS
    assert result['strike_count'] == 2
    assert len(stub_llm.get_call_history(ticket_id)) == 2


async def test_ticket_failing_once_then_succeeding_lands_done(
    tmp_path, scripted_client, install_runtime_client
):
    """The full claim, end to end: bounded retry inside the executor, a
    passing validator, and a real commit landing -- reusing the
    already-verified oversight_git behavior rather than re-deriving it.
    """
    ticket_id = 'retry-fail-once-then-succeed-e2e'
    stub_llm.set_stub_script(ticket_id, [stub_llm.complete_turn('made the fix')])
    scripted_client.queue('git status', ActionResult(exit_code=1, output='dirty'))

    (tmp_path / 'foo.py').write_text('print(1)\n')
    ticket = _ticket(ticket_id, files_changed=['foo.py'])
    ticket['workspace_dir'] = str(tmp_path)
    ticket['verification_command'] = (
        'true'  # deterministic real-subprocess pass, no pytest needed
    )
    state: dict = {'active_ticket': ticket, 'attempt_log': [], 'strike_count': 0}

    executor_result = await nodes.executor_node(state)
    assert executor_result['ticket_status'] == TicketStatus.LOCAL_PASS
    assert executor_result['strike_count'] == 2
    state.update(executor_result)

    # validator_node's real path (a non-Mock client) needs its own client --
    # a real local git/subprocess client, same one oversight_git will use.
    install_runtime_client(LocalGitRuntimeClient())
    validator_result = await nodes.validator_node(state)
    assert validator_result['last_validation_passed'] is True
    state.update(validator_result)

    assert nodes._validator_router(state) == 'oversight_git'

    oversight_result = await nodes.oversight_git_node(state)
    assert oversight_result['ticket_status'] == TicketStatus.DONE
    assert (
        len(stub_llm.get_call_history(ticket_id)) == 2
    )  # never a third executor attempt


async def test_ticket_failing_three_times_escalates_never_a_fourth_attempt(
    scripted_client,
):
    ticket_id = 'retry-fail-three-times'
    stub_llm.set_stub_script(ticket_id, [stub_llm.complete_turn('attempt')])
    scripted_client.queue(
        'git status',
        ActionResult(exit_code=1, output='dirty-1'),
        ActionResult(exit_code=1, output='dirty-2'),
        ActionResult(exit_code=1, output='dirty-3'),
    )

    state = {'active_ticket': _ticket(ticket_id), 'attempt_log': [], 'strike_count': 0}
    result = await nodes.executor_node(state)

    assert result['ticket_status'] == TicketStatus.ESCALATED
    assert result['strike_count'] == nodes._MAX_STRIKES
    assert len(stub_llm.get_call_history(ticket_id)) == nodes._MAX_STRIKES


async def test_strike_counter_does_not_leak_between_tickets(scripted_client):
    ticket_a = 'retry-leak-a'
    stub_llm.set_stub_script(ticket_a, [stub_llm.complete_turn('attempt')])
    scripted_client.queue(
        'git status',
        ActionResult(exit_code=1, output='dirty-1'),
        ActionResult(exit_code=1, output='dirty-2'),
        ActionResult(exit_code=1, output='dirty-3'),
    )
    state_a = {'active_ticket': _ticket(ticket_a), 'attempt_log': [], 'strike_count': 0}
    result_a = await nodes.executor_node(state_a)
    assert result_a['ticket_status'] == TicketStatus.ESCALATED
    assert result_a['strike_count'] == nodes._MAX_STRIKES

    # A fresh ticket, as a new run would start -- no strike_count key at all
    # -- must not inherit ticket A's exhausted budget. The shared client's
    # 'git status' queue is empty by this point too, so ticket B only
    # succeeds on the first attempt if its own strike_count truly starts
    # fresh rather than picking up where ticket A left off.
    ticket_b = 'retry-leak-b'
    stub_llm.set_stub_script(ticket_b, [stub_llm.complete_turn('attempt')])
    state_b = {'active_ticket': _ticket(ticket_b), 'attempt_log': []}
    result_b = await nodes.executor_node(state_b)

    assert result_b['ticket_status'] == TicketStatus.LOCAL_PASS
    assert result_b['strike_count'] == 1
    assert len(stub_llm.get_call_history(ticket_b)) == 1
