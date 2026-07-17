#!/usr/bin/env python3
"""LLM-free verification harness for Step 7 Task 0b (global escalation budget).

Fabricates two independent tickets, each exhausting its own strike budget and
escalating, and drives escalate_node/_dispatch_router directly (no client, no
LLM, no network) with SYNDICATE_MAX_ESCALATIONS=1. Proves that once the
run-wide cap is exceeded, the run halts with a clear terminal status
(RunStatus.HALTED_ESCALATION_BUDGET) and dispatch forces END regardless of
whether more tickets would otherwise be dispatched.

Run with:
    SYNDICATE_MAX_ESCALATIONS=1 python3 scripts/verify_step7_escalation_budget.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `syndicate` is importable without install.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Must be set before syndicate.nodes is imported: _MAX_ESCALATIONS is read
# from the environment at import time.
os.environ['SYNDICATE_MAX_ESCALATIONS'] = '1'

from langgraph.graph import END  # noqa: E402

from syndicate.nodes import (  # noqa: E402
    _MAX_ESCALATIONS,
    _dispatch_router,
    escalate_node,
)
from syndicate.state import AgentState, RunStatus, TicketStatus  # noqa: E402


def main() -> None:
    print('=== Task 0b: global escalation budget (cap=1) ===')
    assert _MAX_ESCALATIONS == 1, (
        f'expected cap=1 from env override, got {_MAX_ESCALATIONS}'
    )

    # Ticket 1 escalates. Cumulative escalation_count starts at 0 (fresh run).
    state: AgentState = {'active_ticket': {'id': 'TICKET-1'}}
    result_1 = escalate_node(state)
    state.update(result_1)  # type: ignore[typeddict-item]
    print(f'after ticket 1 escalation: {result_1!r}')
    assert state.get('escalation_count') == 1
    assert state.get('run_status') is None, (
        'budget should not be exceeded yet: escalation_count(1) == cap(1)'
    )
    route_1 = _dispatch_router(state)
    print(f'dispatch route after ticket 1: {route_1!r}')
    assert route_1 == END, (
        'single-ticket run: an escalated ticket is terminal regardless of budget'
    )

    # Simulate advancing to a second, independent ticket. escalation_count is
    # run-wide and carries over; only per-ticket fields (ticket_status,
    # active_ticket) are what a real next-ticket dispatch would reset.
    state['active_ticket'] = {'id': 'TICKET-2'}
    state['ticket_status'] = TicketStatus.PENDING

    result_2 = escalate_node(state)
    state.update(result_2)  # type: ignore[typeddict-item]
    print(f'after ticket 2 escalation: {result_2!r}')
    assert state.get('escalation_count') == 2, (
        f'expected cumulative count 2, got {state.get("escalation_count")}'
    )
    assert state.get('run_status') == RunStatus.HALTED_ESCALATION_BUDGET, (
        f'expected run halted once escalation_count(2) > cap(1), '
        f'got run_status={state.get("run_status")!r}'
    )

    route_2 = _dispatch_router(state)
    print(f'dispatch route after ticket 2 (over budget): {route_2!r}')
    assert route_2 == END, (
        'run must halt to END once the global escalation budget is exceeded'
    )

    print(
        'PASS: global escalation budget (cap=1) halted the run on the second '
        'escalation with run_status=HALTED_ESCALATION_BUDGET, regardless of '
        'any remaining tickets.'
    )


if __name__ == '__main__':
    main()
    sys.exit(0)
