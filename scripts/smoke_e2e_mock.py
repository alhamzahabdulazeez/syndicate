#!/usr/bin/env python3
"""End-to-end smoke test for the mock loop closure (Step 3).

Run with:
    SYNDICATE_MOCK_CLIENT=1 python3 scripts/smoke_e2e_mock.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import cast

# Ensure repo root is on sys.path so `syndicate` is importable without install.
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('SYNDICATE_MOCK_CLIENT', '1')

from syndicate.nodes import build_graph  # noqa: E402  (env must be set first)
from syndicate.state import AgentState  # noqa: E402

EXPECTED_TRANSITIONS = [
    'analyzer',
    'architect',
    'dispatch',
    'executor',
    'validator',
    'oversight_git',
    'advance',
    'dispatch',
    'END',
]


async def main() -> None:
    app = build_graph()

    initial_state: AgentState = {}
    transitions: list[str] = []

    async for chunk in app.astream(initial_state):
        for node_name in chunk:
            transitions.append(node_name)

    transitions.append('END')

    transition_str = '->'.join(transitions)
    print(transition_str)

    expected_str = '->'.join(EXPECTED_TRANSITIONS)
    assert transitions == EXPECTED_TRANSITIONS, (
        f'Unexpected transitions.\n  got:      {transition_str}\n  expected: {expected_str}'
    )

    final_state = cast(AgentState, await app.ainvoke(initial_state))
    attempt_log = final_state.get('attempt_log', [])
    assert attempt_log == [], (
        f'attempt_log must be empty after advance_node, got: {attempt_log!r}'
    )

    print('All assertions passed.')


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
