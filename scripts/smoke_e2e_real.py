#!/usr/bin/env python3
"""Real end-to-end smoke test (Step 5-R): trivial resolve + seeded-failure self-heal.

Drives the full graph -- including the real Claude tool-calling executor --
against a live OpenHands runtime, so it needs both a running runtime
(SYNDICATE_RUNTIME_URL) and a real Anthropic API key. If no key is
configured, this fails fast with a single clear line instead of attempting
(and slowly failing) real LLM calls, so re-running this exact command is all
that's needed once a key exists.

Run with:
    ANTHROPIC_API_KEY=... SYNDICATE_RUNTIME_URL=http://localhost \
        SYNDICATE_RUNTIME_PORT=8000 python3 scripts/smoke_e2e_real.py

Assumes the target workspace (see SYNDICATE_SMOKE_WORKSPACE below) already
exists and is a git repo with repo-local identity configured -- e.g. via
scripts/verify_step6_validation_git.py's seeding step.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast

# Ensure repo root is on sys.path so `syndicate` is importable without install.
sys.path.insert(0, str(Path(__file__).parent.parent))

if not os.environ.get('ANTHROPIC_API_KEY'):
    print('Step 5-R blocked: ANTHROPIC_API_KEY not set')
    sys.exit(2)

# This script exercises the real client and real executor, never the mock.
os.environ.pop('SYNDICATE_MOCK_CLIENT', None)

from syndicate.nodes import build_graph  # noqa: E402
from syndicate.state import AgentState, TicketStatus  # noqa: E402

_WORKSPACE_DIR = os.environ.get(
    'SYNDICATE_SMOKE_WORKSPACE', '/home/openhands/syndicate-workspace/sample_project'
)

TRIVIAL_TICKET: dict[str, Any] = {
    'id': 'SMOKE-TRIVIAL',
    'title': 'Add a trivially passing no-op change',
    'intent': 'smoke: trivial resolve',
    'verification_command': 'python3 -m pytest -q',
    'workspace_dir': _WORKSPACE_DIR,
    'files_changed': [],
}

SEEDED_FAILURE_TICKET: dict[str, Any] = {
    'id': 'SMOKE-SEEDED-FAILURE',
    'title': 'Fix the intentionally broken calc.add() implementation',
    'intent': 'smoke: seeded-failure self-heal',
    'verification_command': 'python3 -m pytest -q',
    'workspace_dir': _WORKSPACE_DIR,
    'files_changed': ['calc.py'],
}


async def _run_ticket(ticket: dict[str, Any]) -> AgentState:
    app = build_graph()
    initial_state: AgentState = {'active_ticket': ticket}
    return cast(AgentState, await app.ainvoke(initial_state))


def _count_strikes(final_state: AgentState) -> int:
    # attempt_log is cleared by advance_node on the pass path, so strikes may
    # only be visible in decision_ledger there; check both so this works
    # regardless of which terminal path (commit vs escalate) was taken.
    lines = list(final_state.get('attempt_log') or [])
    for decision in final_state.get('decision_ledger') or []:
        lines.append(decision.summary)
    return sum(1 for line in lines if 'strike=' in line)


async def main() -> None:
    trivial_final = await _run_ticket(TRIVIAL_TICKET)
    print(f'trivial ticket final status: {trivial_final.get("ticket_status")}')
    assert trivial_final.get('ticket_status') in (
        TicketStatus.DONE,
        TicketStatus.LOCAL_PASS,
    ), f'expected trivial ticket to resolve, got {trivial_final.get("ticket_status")!r}'

    seeded_final = await _run_ticket(SEEDED_FAILURE_TICKET)
    strikes_seen = _count_strikes(seeded_final)
    print(
        f'seeded-failure ticket final status: {seeded_final.get("ticket_status")}, '
        f'strikes={strikes_seen}'
    )
    assert strikes_seen >= 1, (
        'expected at least one visible strike in the self-heal loop'
    )
    assert strikes_seen <= 3, f'strike budget exceeded: {strikes_seen}'
    assert seeded_final.get('ticket_status') in (
        TicketStatus.DONE,
        TicketStatus.LOCAL_PASS,
        TicketStatus.ESCALATED,
    ), f'unexpected terminal status {seeded_final.get("ticket_status")!r}'

    print('Step 5-R real end-to-end smoke test passed.')


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
