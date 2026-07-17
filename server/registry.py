"""In-memory run registry: one active run at a time, per-run backlog+queue.

The run's lifecycle (the asyncio.Task in RunRecord.task) is decoupled from
any client connection -- disconnect/refresh must not stop it. Backlog is
bounded (in-memory, v1: lost across a server restart -- state remains
available via the checkpointer; see README).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from server.events import Envelope

RunStatusLiteral = Literal['queued', 'running', 'completed', 'failed', 'escalated']

_BACKLOG_MAXLEN = 2000


@dataclass
class RunRecord:
    run_id: str
    raw_request: str
    status: RunStatusLiteral = 'queued'
    created_at: float = field(default_factory=time.time)
    backlog: deque[Envelope] = field(
        default_factory=lambda: deque(maxlen=_BACKLOG_MAXLEN)
    )
    # Fan-out wake signal, not a delivery queue: an asyncio.Queue is single-
    # consumer (competing .get() calls would each only see a subset of
    # items), which would silently drop events for a second concurrent
    # viewer of the same run. Every stream connection instead keeps its own
    # `last_sent` seq cursor and re-scans `backlog` (the single source of
    # truth) whenever woken by this event -- see server/app.py's stream_run.
    new_event: asyncio.Event = field(default_factory=asyncio.Event)
    seq_counter: int = 0
    task: asyncio.Task[None] | None = None
    # Derivation bookkeeping consumed by server/runner.py -- not part of any
    # public envelope.
    last_attempt_log_len: int = 0
    last_escalation_count: int = 0
    last_run_status: str | None = None

    def next_seq(self) -> int:
        self.seq_counter += 1
        return self.seq_counter

    def emit(self, envelope: Envelope) -> None:
        self.backlog.append(envelope)
        self.new_event.set()


class RunRegistry:
    """Concurrency cap = 1, enforced here (not in the route handler), so the
    active-run check and the record's creation are atomic within one
    synchronous method call (no await between check and set)."""

    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._active_run_id: str | None = None

    def try_start(self, run_id: str, raw_request: str) -> RunRecord | None:
        if self._active_run_id is not None:
            return None
        record = RunRecord(run_id=run_id, raw_request=raw_request)
        self._runs[run_id] = record
        self._active_run_id = run_id
        return record

    def finish(self, run_id: str) -> None:
        if self._active_run_id == run_id:
            self._active_run_id = None

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    @property
    def active_run_id(self) -> str | None:
        return self._active_run_id

    def all_known(self) -> list[RunRecord]:
        return list(self._runs.values())
