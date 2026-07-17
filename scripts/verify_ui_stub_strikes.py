#!/usr/bin/env python3
"""Task 5.3: stub 3-strike scenario through the API (container UP).

Task 0 audit confirmed (by code inspection) that a full-graph run honors
SYNDICATE_STUB_LLM=1; this empirically proves it end-to-end through the
real FastAPI routes and SSE machinery, container UP.

Runs uvicorn IN-PROCESS (not a separate OS process), for two structural
reasons -- neither is a shortcut on correctness, both are necessary:

  1. syndicate.stub_llm's script registry is a process-local in-memory
     dict. A separately-launched `uvicorn` subprocess would never see
     set_stub_script() calls made from a different process's interpreter.
  2. The API only accepts {"raw_request": str} -- POST /runs assigns
     run_id via uuid4() internally, so there is no way to register a stub
     script or pre-seed a workspace for a run_id that doesn't exist yet.
     Rather than racing the server's own async graph traversal to sneak
     registration in before the executor node runs (LangGraph + the real
     AsyncSqliteSaver checkpointer do yield real scheduling points between
     nodes, but racing them is not "deterministic"), this monkeypatches
     server.app.uuid4 for the single POST call so the run_id is a fixed,
     known value prepared in advance -- a standard dependency-injection-
     via-monkeypatch test seam, not a change to any production code path.

Every route/response/SSE mechanism exercised here is the real, unmodified
server code, hitting the real container over a real HTTP socket.

Run with the container already up:
    python3 scripts/verify_ui_stub_strikes.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.pop('SYNDICATE_MOCK_CLIENT', None)
os.environ['SYNDICATE_STUB_LLM'] = '1'
os.environ.setdefault('SYNDICATE_RUNTIME_URL', 'http://localhost')
os.environ.setdefault('SYNDICATE_RUNTIME_PORT', '8000')
os.environ['SYNDICATE_UI_DB_PATH'] = 'data/checkpoints_stub_strikes_verify.db'

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from server import app as app_module  # noqa: E402
from syndicate.runtime import get_runtime_client  # noqa: E402
from syndicate.stub_llm import complete_turn, set_stub_script, tool_turn  # noqa: E402

FIXED_RUN_ID = 'stub-strikes-e2e-fixed-id'
WORKSPACE_DIR = f'/home/openhands/syndicate-workspace/ui-runs/{FIXED_RUN_ID}'
CALC_BUGGY = 'def add(a, b):\n    return a - b\n'
TEST_CALC_PY = 'from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n'


async def seed_workspace() -> None:
    client = get_runtime_client()
    try:
        setup = await client.run_action(f'mkdir -p {WORKSPACE_DIR}/tests')
        assert setup.exit_code == 0, f'mkdir failed: {setup.output}'
        await client.write_file(f'{WORKSPACE_DIR}/calc.py', CALC_BUGGY)
        await client.write_file(f'{WORKSPACE_DIR}/tests/test_calc.py', TEST_CALC_PY)
        await client.write_file(
            f'{WORKSPACE_DIR}/.gitignore', '__pycache__/\n*.pyc\n.pytest_cache/\n'
        )
        init = await client.run_action(
            'git init -q '
            '&& git config user.name syndicate-bot '
            '&& git config user.email syndicate-bot@example.invalid '
            '&& git add -- calc.py tests/test_calc.py .gitignore '
            '&& (git commit -q -m seed || true)',
            cwd=WORKSPACE_DIR,
        )
        assert init.exit_code == 0, f'git setup failed: {init.output}'
    finally:
        await client.aclose()
    print(f'workspace seeded at {WORKSPACE_DIR}')


def register_stub_script() -> None:
    note_path = f'{WORKSPACE_DIR}/attempt_note.txt'
    set_stub_script(
        FIXED_RUN_ID,
        [
            tool_turn(
                (
                    'str_replace_based_edit_tool',
                    {
                        'command': 'create',
                        'path': note_path,
                        'file_text': 'attempt in progress\n',
                    },
                )
            ),
            complete_turn('made an attempt'),
        ],
    )
    print(f'stub script registered for ticket_id={FIXED_RUN_ID!r}')


async def main() -> None:
    await seed_workspace()
    register_stub_script()

    config = uvicorn.Config(
        app_module.app, host='127.0.0.1', port=8080, log_level='warning'
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    async with httpx.AsyncClient(
        base_url='http://127.0.0.1:8080', timeout=30.0
    ) as http:
        for _ in range(30):
            try:
                resp = await http.get('/health')
                if resp.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError('in-process uvicorn did not become healthy')
        print('in-process server healthy')

        # The one deliberate test seam: force this single POST's run_id to
        # the fixed value we already seeded/registered above.
        with mock.patch.object(app_module, 'uuid4', return_value=FIXED_RUN_ID):
            resp = await http.post(
                '/runs', json={'raw_request': 'Task 5.3 stub 3-strike scenario'}
            )
        resp.raise_for_status()
        run_id = resp.json()['run_id']
        print(f'POST /runs -> run_id={run_id!r}')
        assert run_id == FIXED_RUN_ID, f'expected fixed run_id, got {run_id!r}'

        envelopes = []
        async with http.stream('GET', f'/runs/{run_id}/stream') as stream_resp:
            async for line in stream_resp.aiter_lines():
                if not line.startswith('data:'):
                    continue
                envelope = json.loads(line[len('data:') :].strip())
                envelopes.append(envelope)
                print(json.dumps(envelope))
                if envelope['kind'] in ('run_completed', 'run_failed'):
                    break

        server.should_exit = True
        await server_task

    seqs = [e['seq'] for e in envelopes]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), (
        f'seq not strictly monotonic: {seqs}'
    )

    attempts = [e for e in envelopes if e['kind'] == 'attempt']
    print(f'attempt events: {len(attempts)}')
    assert len(attempts) == 3, (
        f'expected 3 attempt events (3-strike), got {len(attempts)}'
    )
    strikes = [a['payload']['strike'] for a in attempts]
    assert strikes == [1, 2, 3], f'expected strikes [1, 2, 3], got {strikes}'
    print(f'strikes escalating: {strikes}')

    escalations = [e for e in envelopes if e['kind'] == 'escalation']
    assert escalations, 'expected at least one escalation event'
    assert escalations[0]['payload'].get('ticket_status') == 'escalated', (
        f'expected escalation payload to carry ticket_status=escalated, got {escalations[0]["payload"]!r}'
    )
    print(f'escalation event: {escalations[0]["payload"]}')

    terminal = envelopes[-1]
    assert terminal['kind'] == 'run_completed', (
        f'expected terminal run_completed, got {terminal["kind"]!r}'
    )
    assert terminal['payload'].get('ticket_status') == 'escalated'
    print(f'terminal event: {terminal["kind"]} payload={terminal["payload"]}')

    print(
        'PASS: Task 5.3 stub 3-strike scenario through the API -- three escalating attempt events, '
        'one escalation event, terminal run_completed(ticket_status=escalated)'
    )


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
