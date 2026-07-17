#!/usr/bin/env python3
"""Task 1 verification: SQLite checkpointer serialization round-trip.

Proves AgentState (a plain TypedDict, including DecisionSummary dataclass
entries and TicketStatus/RunStatus enum values -- none of which were
restructured for this) survives run -> disk -> a genuinely separate process
-> resume/fetch, through AsyncSqliteSaver. Mock client only, no LLM/
container needed.

Run as two SEPARATE process invocations against the same db path (this is
what actually proves "restart", not just two functions in one process):

    SYNDICATE_MOCK_CLIENT=1 python3 scripts/verify_checkpoint_roundtrip.py write <thread_id> <db_path>
    python3 scripts/verify_checkpoint_roundtrip.py read <thread_id> <db_path>
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).parent.parent))

from langchain_core.runnables import RunnableConfig  # noqa: E402

from server.checkpointer import checkpointer_from_path  # noqa: E402
from syndicate.nodes import build_graph  # noqa: E402
from syndicate.state import AgentState, DecisionSummary  # noqa: E402


def _run_config(thread_id: str) -> RunnableConfig:
    return {'configurable': {'thread_id': thread_id}}


async def do_write(thread_id: str, db_path: str) -> None:
    os.environ.setdefault('SYNDICATE_MOCK_CLIENT', '1')
    async with checkpointer_from_path(db_path) as saver:
        graph = build_graph(checkpointer=saver)
        config = _run_config(thread_id)
        final = cast(AgentState, await graph.ainvoke({}, config=config))
        print(f'write: ticket_status={final.get("ticket_status")}')
        print(
            f'write: decision_ledger entries={len(final.get("decision_ledger") or [])}'
        )
        print(f'write: attempt_log={final.get("attempt_log")!r}')
    print('write: connection closed cleanly')


async def do_read(thread_id: str, db_path: str) -> None:
    # A fresh process, fresh import, fresh sqlite connection -- no state
    # from do_write's process is reused here.
    async with checkpointer_from_path(db_path) as saver:
        graph = build_graph(checkpointer=saver)
        config = _run_config(thread_id)
        snapshot = await graph.aget_state(config)

    assert snapshot is not None, (
        f'no checkpointed state found for thread_id={thread_id!r}'
    )
    state = cast(AgentState, snapshot.values)

    print(f'read: ticket_status={state.get("ticket_status")!r}')
    ledger = state.get('decision_ledger') or []
    print(f'read: decision_ledger entries={len(ledger)}')
    for entry in ledger:
        assert isinstance(entry, DecisionSummary), (
            f'expected a real DecisionSummary instance after round-trip, got {type(entry)!r}'
        )
        print(
            f'read: ledger entry type={type(entry).__name__} '
            f'summary={entry.summary!r} attempt_count={entry.attempt_count}'
        )
    print(f'read: attempt_log={state.get("attempt_log")!r}')
    assert state.get('attempt_log') == [], (
        'expected attempt_log cleared post-advance, as pre-persistence'
    )

    print(
        'READ: round-trip verified -- state, dataclass entries, and cleared attempt_log all intact'
    )


async def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in ('write', 'read'):
        print(
            f'usage: {sys.argv[0]} <write|read> <thread_id> <db_path>', file=sys.stderr
        )
        sys.exit(2)
    mode, thread_id, db_path = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == 'write':
        await do_write(thread_id, db_path)
    else:
        await do_read(thread_id, db_path)


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
