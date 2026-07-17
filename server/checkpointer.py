"""SQLite checkpointer construction, shared by server/app.py and the
Task 1 round-trip verification script.

syndicate/state.py's AgentState carries a plain dataclass (DecisionSummary)
and two str-Enums (TicketStatus, RunStatus) -- LangGraph's default msgpack
serde deserializes these today but warns they are "unregistered" and "will
be blocked in a future version" unless explicitly allow-listed. Explicitly
allow-listing them here closes that forward-compat gap without touching
syndicate/ or restructuring AgentState (which the UI brief's Task 1
explicitly forbids doing to work around a serialization issue).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_ALLOWED_MSGPACK_MODULES = [
    ('syndicate.state', 'DecisionSummary'),
    ('syndicate.state', 'TicketStatus'),
    ('syndicate.state', 'RunStatus'),
]


@asynccontextmanager
async def checkpointer_from_path(db_path: str) -> AsyncIterator[AsyncSqliteSaver]:
    serde = JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_MSGPACK_MODULES)
    async with aiosqlite.connect(db_path) as conn:
        yield AsyncSqliteSaver(conn, serde=serde)
