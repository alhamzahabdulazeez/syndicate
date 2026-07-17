"""Drives one graph run to completion, translating syndicate's astream
updates into SSE envelopes. Mechanical derivation only -- no chassis edits:
a node_update is the raw (redacted, capped) delta; an executor delta that
appends attempt_log entries also yields one `attempt` event per completed
strike; a change in escalation_count/run_status/ESCALATED status also
yields an `escalation` event. Terminal kind derives from graph completion
or exception.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from server.events import make_envelope
from server.registry import RunRecord
from syndicate.state import RunStatus, TicketStatus

logger = logging.getLogger(__name__)

# A UI-run's own workspace, separate per run_id -- distinct from any
# syndicate test harness's paths (Steps 6/7.5), so concurrent/prior test
# runs against the same container never collide with UI-submitted runs.
_UI_WORKSPACE_ROOT = '/home/openhands/syndicate-workspace/ui-runs'


def build_initial_state(run_id: str, raw_request: str) -> dict[str, Any]:
    return {
        'active_ticket': {
            'id': run_id,
            'title': raw_request[:80],
            'raw_request': raw_request,
            'workspace_dir': f'{_UI_WORKSPACE_ROOT}/{run_id}',
            'verification_command': 'python3 -m pytest -q',
            'files_changed': [],
        }
    }


def _extract_attempts(
    node: str, delta: dict[str, Any], record: RunRecord
) -> list[dict[str, Any]]:
    if node != 'executor':
        return []
    attempt_log = delta.get('attempt_log')
    if not isinstance(attempt_log, list):
        return []
    new_lines = attempt_log[record.last_attempt_log_len :]
    record.last_attempt_log_len = len(attempt_log)

    attempts = []
    for line in new_lines:
        if not isinstance(line, str) or 'fast_check' not in line:
            continue
        strike: int | None = None
        if line.startswith('strike='):
            try:
                strike = int(line.split()[0].split('=', 1)[1])
            except (IndexError, ValueError):
                strike = None
        attempts.append({'strike': strike, 'fast_check_summary': line})
    return attempts


def _extract_escalation(
    delta: dict[str, Any], record: RunRecord
) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}

    escalation_count = delta.get('escalation_count')
    if (
        isinstance(escalation_count, int)
        and escalation_count != record.last_escalation_count
    ):
        record.last_escalation_count = escalation_count
        payload['escalation_count'] = escalation_count

    run_status = delta.get('run_status')
    if run_status is not None:
        run_status_value = (
            run_status.value if isinstance(run_status, RunStatus) else run_status
        )
        if run_status_value != record.last_run_status:
            record.last_run_status = run_status_value
            payload['run_status'] = run_status_value

    if (
        delta.get('ticket_status') == TicketStatus.ESCALATED
        and 'ticket_status' not in payload
    ):
        payload['ticket_status'] = TicketStatus.ESCALATED.value

    return payload or None


async def drive_run(graph: CompiledStateGraph, record: RunRecord) -> None:
    record.status = 'running'
    record.emit(
        make_envelope(
            seq=record.next_seq(),
            run_id=record.run_id,
            kind='run_started',
            node=None,
            payload={'raw_request': record.raw_request},
        )
    )

    config: RunnableConfig = {'configurable': {'thread_id': record.run_id}}
    initial_state = build_initial_state(record.run_id, record.raw_request)

    try:
        async for update in graph.astream(
            initial_state, config=config, stream_mode='updates'
        ):
            for node, raw_delta in update.items():
                # A node returning {} (e.g. architect_node, dispatch_node --
                # frozen chassis stub/router nodes) surfaces here as delta
                # None, not {} -- still a real node transition, so it still
                # gets a node_update (empty payload), not silently dropped.
                delta = raw_delta if isinstance(raw_delta, dict) else {}
                record.emit(
                    make_envelope(
                        seq=record.next_seq(),
                        run_id=record.run_id,
                        kind='node_update',
                        node=node,
                        payload=dict(delta),
                    )
                )
                for attempt in _extract_attempts(node, delta, record):
                    record.emit(
                        make_envelope(
                            seq=record.next_seq(),
                            run_id=record.run_id,
                            kind='attempt',
                            node=node,
                            payload=attempt,
                        )
                    )
                escalation_payload = _extract_escalation(delta, record)
                if escalation_payload is not None:
                    record.emit(
                        make_envelope(
                            seq=record.next_seq(),
                            run_id=record.run_id,
                            kind='escalation',
                            node=node,
                            payload=escalation_payload,
                        )
                    )

        final_snapshot = await graph.aget_state(config)
        final_state = final_snapshot.values if final_snapshot else {}
        ticket_status = final_state.get('ticket_status')
        ticket_status_value = (
            ticket_status.value
            if isinstance(ticket_status, TicketStatus)
            else ticket_status
        )
        record.status = (
            'escalated' if ticket_status == TicketStatus.ESCALATED else 'completed'
        )
        record.emit(
            make_envelope(
                seq=record.next_seq(),
                run_id=record.run_id,
                kind='run_completed',
                node=None,
                payload={'ticket_status': ticket_status_value},
            )
        )
    except Exception as exc:  # noqa: BLE001 -- one run's failure must not crash the server
        logger.exception('run %s failed', record.run_id)
        record.status = 'failed'
        record.emit(
            make_envelope(
                seq=record.next_seq(),
                run_id=record.run_id,
                kind='run_failed',
                node=None,
                payload={'error': str(exc)},
            )
        )
