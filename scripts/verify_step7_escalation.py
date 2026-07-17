#!/usr/bin/env python3
"""LLM-free verification harness for Step 7 Task 0a (escalation distillation).

Fabricates AgentState by hand and drives escalate_node/advance_node/
_dispatch_router directly -- no client, no LLM, no network -- to prove that
an escalated ticket's attempt_log is distilled into decision_ledger (and
cleared) *before* the graph would exit, closing the drift where the old
"escalate" edge went straight to END and skipped advance_node entirely.

Run with:
    python3 scripts/verify_step7_escalation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on sys.path so `syndicate` is importable without install.
sys.path.insert(0, str(Path(__file__).parent.parent))

from langgraph.graph import END  # noqa: E402

from syndicate.nodes import _dispatch_router, advance_node, escalate_node  # noqa: E402
from syndicate.state import AgentState, TicketStatus  # noqa: E402


def main() -> None:
    print('=== Task 0a: escalation must distill before exit ===')

    # Simulate a ticket that just exhausted its strike budget in
    # validator_node (strike_count >= _MAX_STRIKES already established the
    # "escalate" route via _validator_router; we start from that point).
    state: AgentState = {
        'active_ticket': {'id': 'ESC-TICKET-1'},
        'attempt_log': [
            "strike=1 fast_check exit_code=1 output='boom'",
            "strike=2 fast_check exit_code=1 output='boom again'",
            "strike=3 fast_check exit_code=1 output='boom once more'",
        ],
        'strike_count': 3,
        'decision_ledger': [],
    }

    escalate_result = escalate_node(state)
    state.update(escalate_result)  # type: ignore[typeddict-item]
    print(f'escalate_node result: {escalate_result!r}')
    assert state.get('ticket_status') == TicketStatus.ESCALATED, (
        f'expected ticket_status ESCALATED, got {state.get("ticket_status")!r}'
    )

    ledger_before = list(state.get('decision_ledger') or [])
    assert ledger_before == [], 'expected empty decision_ledger before advance_node'

    advance_result = advance_node(state)
    state.update(advance_result)  # type: ignore[typeddict-item]
    print(f'advance_node result: {advance_result!r}')

    ledger_after = state.get('decision_ledger') or []
    assert len(ledger_after) == 1, (
        f'expected exactly one distilled ledger entry, got {len(ledger_after)}'
    )
    entry = ledger_after[0]
    print(
        f'decision_ledger entry: summary={entry.summary!r} attempt_count={entry.attempt_count}'
    )
    assert entry.attempt_count == 3, (
        f'expected attempt_count=3, got {entry.attempt_count}'
    )
    assert 'boom' in entry.summary, (
        'expected distilled summary to carry the failure text'
    )

    cleared_log = state.get('attempt_log')
    print(f'attempt_log after advance: {cleared_log!r}')
    assert cleared_log == [], f'expected attempt_log cleared, got {cleared_log!r}'

    route = _dispatch_router(state)
    print(f'post-advance dispatch route: {route!r}')
    assert route == END, (
        f'expected dispatch to route to END after an escalated ticket, got {route!r}'
    )

    print(
        "PASS: escalated ticket's attempt_log was distilled into decision_ledger "
        'and cleared before the graph would exit.'
    )


if __name__ == '__main__':
    main()
    sys.exit(0)
