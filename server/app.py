"""FastAPI service wrapping the frozen syndicate LangGraph chassis.

Security: binds 127.0.0.1:8080 ONLY (see __main__ below / README). This API
triggers arbitrary sandbox bash via the frozen chassis's executor node -- a
public bind is RCE-with-a-form-field. No auth in v1 *because* it is
loopback-only; remote viewing is an SSH tunnel, not a public listener.
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from server.checkpointer import checkpointer_from_path
from server.events import format_sse, redact_value
from server.models import RunRequest
from server.registry import RunRecord, RunRegistry
from server.runner import drive_run
from syndicate.nodes import build_graph
from syndicate.state import DecisionSummary

logger = logging.getLogger(__name__)

_DB_PATH = os.environ.get('SYNDICATE_UI_DB_PATH', 'data/checkpoints.db')
_HEARTBEAT_SECONDS = 15.0
_STATIC_DIR = os.path.join(os.path.dirname(__file__), 'static')


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    os.makedirs(os.path.dirname(_DB_PATH) or '.', exist_ok=True)
    async with checkpointer_from_path(_DB_PATH) as saver:
        app.state.graph = build_graph(checkpointer=saver)
        app.state.registry = RunRegistry()
        yield


app = FastAPI(lifespan=lifespan)


def _current_rss_kb() -> int:
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except OSError:
        pass
    # Fallback: max RSS (not "current", but always available cross-platform).
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


async def _run_and_release(record: RunRecord) -> None:
    try:
        await drive_run(app.state.graph, record)
    finally:
        app.state.registry.finish(record.run_id)


@app.post('/runs')
async def create_run(body: RunRequest) -> JSONResponse:
    registry: RunRegistry = app.state.registry
    run_id = str(uuid4())
    record = registry.try_start(run_id, body.raw_request)
    if record is None:
        return JSONResponse(
            status_code=409,
            content={
                'detail': 'a run is already active',
                'run_id': registry.active_run_id,
            },
        )
    record.task = asyncio.create_task(_run_and_release(record))
    return JSONResponse(status_code=200, content={'run_id': run_id})


@app.get('/runs')
async def list_runs() -> list[dict[str, Any]]:
    registry: RunRegistry = app.state.registry
    known_ids = {r.run_id for r in registry.all_known()}
    results = [
        {
            'run_id': r.run_id,
            'status': r.status,
            'created_at': r.created_at,
        }
        for r in registry.all_known()
    ]

    # Runs from a prior process lifetime (in-memory registry lost across a
    # restart, but the checkpointer persists) -- one entry per thread_id,
    # the latest checkpoint only.
    graph = app.state.graph
    latest_by_thread: dict[str, Any] = {}
    async for checkpoint_tuple in graph.checkpointer.alist(None):
        thread_id = checkpoint_tuple.config['configurable']['thread_id']
        if thread_id in known_ids:
            continue
        existing = latest_by_thread.get(thread_id)
        if (
            existing is None
            or checkpoint_tuple.checkpoint['ts'] > existing.checkpoint['ts']
        ):
            latest_by_thread[thread_id] = checkpoint_tuple

    for thread_id, ct in latest_by_thread.items():
        values = ct.checkpoint.get('channel_values', {})
        ticket_status = values.get('ticket_status')
        status = 'unknown'
        if ticket_status is not None:
            status_value = getattr(ticket_status, 'value', ticket_status)
            status = 'escalated' if status_value == 'escalated' else 'completed'
        results.append(
            {'run_id': thread_id, 'status': status, 'created_at': ct.checkpoint['ts']}
        )

    results.sort(key=lambda r: str(r['created_at']), reverse=True)
    return results


@app.get('/runs/{run_id}/state')
async def get_run_state(run_id: str) -> dict[str, Any]:
    graph = app.state.graph
    config = {'configurable': {'thread_id': run_id}}
    snapshot = await graph.aget_state(config)
    if snapshot is None or not snapshot.values:
        raise HTTPException(status_code=404, detail=f'no state for run_id={run_id!r}')

    values = dict(snapshot.values)
    ledger = values.get('decision_ledger') or []
    values['decision_ledger'] = [
        {'summary': e.summary, 'attempt_count': e.attempt_count}
        if isinstance(e, DecisionSummary)
        else e
        for e in ledger
    ]
    return dict(redact_value(values))


@app.get('/runs/{run_id}/stream')
async def stream_run(run_id: str, after: int = 0) -> StreamingResponse:
    registry: RunRegistry = app.state.registry
    record = registry.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'unknown run_id={run_id!r}')

    async def event_source() -> AsyncIterator[str]:
        # backlog is the single source of truth; last_sent is this
        # connection's own cursor into it, so replay-then-live-tail never
        # double-sends and multiple concurrent viewers of the same run each
        # get the full stream independently (see RunRecord.new_event).
        last_sent = after
        while True:
            pending = [
                e
                for e in list(record.backlog)
                if e['seq'] is not None and e['seq'] > last_sent
            ]
            for envelope in pending:
                yield format_sse(envelope)
                assert envelope['seq'] is not None
                last_sent = envelope['seq']
                if envelope['kind'] in ('run_completed', 'run_failed'):
                    return

            record.new_event.clear()
            try:
                await asyncio.wait_for(
                    record.new_event.wait(), timeout=_HEARTBEAT_SECONDS
                )
            except TimeoutError:
                yield ': heartbeat\n\n'

    return StreamingResponse(event_source(), media_type='text/event-stream')


@app.get('/health')
async def health() -> dict[str, Any]:
    registry: RunRegistry = app.state.registry
    return {
        'status': 'ok',
        'active_run_id': registry.active_run_id,
        'rss_kb': _current_rss_kb(),
    }


# Static mount LAST: registered after all API routes so it never shadows them.
if os.path.isdir(_STATIC_DIR):
    app.mount('/', StaticFiles(directory=_STATIC_DIR, html=True), name='static')


if __name__ == '__main__':
    import uvicorn

    # Loopback-only, single worker (a second worker would duplicate the
    # compiled graph/checkpointer connection and collide with the single
    # execution sandbox) -- see module docstring and README.
    uvicorn.run(app, host='127.0.0.1', port=8080, workers=1, log_level='info')
