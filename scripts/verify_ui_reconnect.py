#!/usr/bin/env python3
"""Task 5.5: reconnect with ?after=<last> resumes without gaps or duplicates.

Starts uvicorn as a real subprocess (container-independent, mock client),
starts a run, deliberately reads only the first few SSE lines then closes
the connection mid-stream (proving disconnect doesn't stop the run --
Task 2's "lifecycle MUST NOT depend on any client connection" -- the run
keeps going server-side), then reconnects with after=<last seq seen> and
proves the combined sequence of both connections is exactly 1..N with no
gaps and no duplicates.

Run with:
    python3 scripts/verify_ui_reconnect.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402


async def main() -> None:
    repo_root = Path(__file__).parent.parent
    env = {**os.environ, 'SYNDICATE_MOCK_CLIENT': '1'}
    data_dir = repo_root / 'data'
    data_dir.mkdir(exist_ok=True)

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

            resp = await http.post('/runs', json={'raw_request': 'Task 5.5 reconnect'})
            run_id = resp.json()['run_id']
            print(f'run_id={run_id}')

            first_batch: list[dict] = []
            async with http.stream('GET', f'/runs/{run_id}/stream') as stream_resp:
                async for line in stream_resp.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    envelope = json.loads(line[len('data:') :].strip())
                    first_batch.append(envelope)
                    print(
                        f'[connection 1] seq={envelope["seq"]} kind={envelope["kind"]}'
                    )
                    if len(first_batch) >= 3:
                        break
            # Deliberately closing the httpx stream context here -- the
            # `async with` exit tears down the connection mid-run (the run
            # is not yet at a terminal kind). The server-side task keeps
            # running regardless (proven by Task 5.4/5.2 already; here we
            # rely on it to have finished by the time we reconnect below).
            print(
                f'[connection 1] closed after {len(first_batch)} events '
                f'(last seq={first_batch[-1]["seq"]})'
            )

            # Give the (fast, mock) run time to reach a terminal kind
            # server-side even though nothing is reading its stream.
            for _ in range(20):
                state_resp = await http.get(f'/runs/{run_id}/state')
                if state_resp.status_code == 200:
                    break
                await asyncio.sleep(0.2)

            last_seq = first_batch[-1]['seq']
            second_batch: list[dict] = []
            async with http.stream(
                'GET', f'/runs/{run_id}/stream?after={last_seq}'
            ) as stream_resp:
                async for line in stream_resp.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    envelope = json.loads(line[len('data:') :].strip())
                    second_batch.append(envelope)
                    print(
                        f'[connection 2, after={last_seq}] seq={envelope["seq"]} kind={envelope["kind"]}'
                    )
                    if envelope['kind'] in ('run_completed', 'run_failed'):
                        break
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    combined = first_batch + second_batch
    seqs = [e['seq'] for e in combined]
    print(f'connection 1 seqs: {[e["seq"] for e in first_batch]}')
    print(f'connection 2 seqs: {[e["seq"] for e in second_batch]}')
    print(f'combined seqs: {seqs}')

    assert len(seqs) == len(set(seqs)), f'duplicate seq across reconnect: {seqs}'
    expected = list(range(1, len(seqs) + 1))
    assert seqs == expected, (
        f'gap in combined sequence: got {seqs}, expected {expected}'
    )
    assert combined[-1]['kind'] in ('run_completed', 'run_failed'), (
        'expected a terminal kind overall'
    )

    print(
        'PASS: Task 5.5 reconnect with ?after= resumes with no gaps and no duplicates'
    )


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
