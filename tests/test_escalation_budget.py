from __future__ import annotations

from langgraph.graph import END

from syndicate import nodes
from syndicate.state import RunStatus, TicketStatus


async def test_second_escalation_halts_run_with_budget_of_one(monkeypatch):
    monkeypatch.setattr(nodes, '_MAX_ESCALATIONS', 1)
    state: dict = {}

    first = nodes.escalate_node(state)
    assert first['escalation_count'] == 1
    assert 'run_status' not in first
    state.update(first)

    second = nodes.escalate_node(state)
    assert second['escalation_count'] == 2
    assert second['run_status'] == RunStatus.HALTED_ESCALATION_BUDGET


def test_dispatch_router_halts_regardless_of_ticket_outcome():
    """Tickets after the halt do not execute: dispatch is the only gate into
    executor, and a run-wide halt overrides everything else this ticket did
    or would do -- even a ticket that otherwise looks ready to run again."""
    state = {
        'run_status': RunStatus.HALTED_ESCALATION_BUDGET,
        'ticket_status': TicketStatus.LOCAL_PASS,
        'last_validation_passed': False,
    }
    assert nodes._dispatch_router(state) == END


async def test_escalation_budget_is_run_wide_not_per_ticket(monkeypatch):
    """The budget carries forward across different tickets in the same run
    -- unlike strike_count, which resets per ticket (test_retry_bounds.py's
    leak test). One escalation on ticket A plus one on ticket B, with a
    budget of 1, must halt on the second -- it must not reset just because
    the active_ticket changed.
    """
    monkeypatch.setattr(nodes, '_MAX_ESCALATIONS', 1)
    state: dict = {'active_ticket': {'id': 'ticket-a'}}

    first = nodes.escalate_node(state)
    assert first['escalation_count'] == 1
    assert 'run_status' not in first
    state.update(first)

    state['active_ticket'] = {'id': 'ticket-b'}
    second = nodes.escalate_node(state)

    assert second['escalation_count'] == 2
    assert second['run_status'] == RunStatus.HALTED_ESCALATION_BUDGET
