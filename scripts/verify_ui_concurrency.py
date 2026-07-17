#!/usr/bin/env python3
"""Task 5.6: a second POST /runs while a run is active -> 409 with the
active run_id.

Fires two POST /runs concurrently (asyncio.gather, not sequential) against
a real uvicorn subprocess (mock client, container-independent). Concurrency
cap = 1 is enforced synchronously inside RunRegistry.try_start (no await
between the active-run check and marking the new run active), so whichever
request's coroutine reaches the handler first wins 200 and the other gets
409 with that run's id -- regardless of how fast the (mock) graph itself
completes afterward.

Run with:
    python3 scripts/verify_ui_concurrency.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402


async def main() -> None:
    repo_root = Path(__file__).parent.parent
    env = {**os.environ, 'SYNDICATE_MOCK_CLIENT': '1'}
    (repo_root / 'data').mkdir(exist_ok=True)

    server = subprocess.Popen(
        [sys.executable, '-m', 'server.app'],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient(
            base_url='http://127.0.0.1:8080', timeout=10.0
        ) as http:
            for _ in range(30):
                try:
                    if (await http.get('/health')).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError('server did not become healthy')
            print('server healthy')

            resp_a, resp_b = await asyncio.gather(
                http.post('/runs', json={'raw_request': 'Task 5.6 concurrency A'}),
                http.post('/runs', json={'raw_request': 'Task 5.6 concurrency B'}),
            )
            print(f'response A: status={resp_a.status_code} body={resp_a.json()}')
            print(f'response B: status={resp_b.status_code} body={resp_b.json()}')
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    statuses = sorted([resp_a.status_code, resp_b.status_code])
    assert statuses == [200, 409], (
        f'expected exactly one 200 and one 409, got {statuses}'
    )

    accepted, rejected = (
        (resp_a, resp_b) if resp_a.status_code == 200 else (resp_b, resp_a)
    )
    accepted_run_id = accepted.json()['run_id']
    rejected_body = rejected.json()
    print(f'accepted run_id={accepted_run_id}, rejected body={rejected_body}')

    assert rejected_body.get('run_id') == accepted_run_id, (
        f"expected 409's run_id to name the active run, got {rejected_body!r} vs accepted {accepted_run_id!r}"
    )
    assert 'detail' in rejected_body

    print(
        'PASS: Task 5.6 -- second concurrent POST /runs got 409 naming the active run_id'
    )


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
