#!/usr/bin/env python3
"""Real-runtime smoke test for file ops (Step 5).

Connects to a *live* OpenHands agent server (SYNDICATE_MOCK_CLIENT unset) and
exercises OpenHandsRuntimeClient.read_file/write_file/edit_file against it, to
prove the file-ops HTTP integration works end to end. This does not require
an LLM or an Anthropic API key -- it only talks to the runtime container.

The endpoints exercised here (`/api/file/download/{path}` and
`/api/file/upload/{path}`) were discovered by inspecting the runtime's
`GET /openapi.json`.

Requires a running OpenHands agent server, configured via env:
    SYNDICATE_RUNTIME_URL=http://<host>[:<port>]      (required)
    SYNDICATE_RUNTIME_PORT=<port>                      (optional, appended if
                                                        SYNDICATE_RUNTIME_URL
                                                        has no port)
    SYNDICATE_RUNTIME_SESSION_API_KEY=<key>            (optional, sent as
                                                        X-Session-API-Key)

Run with:
    SYNDICATE_RUNTIME_URL=http://localhost:8000 python3 scripts/verify_runtime_fileops.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `syndicate` is importable without install.
sys.path.insert(0, str(Path(__file__).parent.parent))

# This script exercises the real client, never the mock.
os.environ.pop('SYNDICATE_MOCK_CLIENT', None)

from syndicate.runtime import get_runtime_client  # noqa: E402

PATH = '/tmp/syndicate_fileops_check.txt'
MISSING_PATH = '/tmp/syndicate_fileops_missing.txt'
ORIGINAL = 'hello syndicate fileops'
REPLACED = 'hello syndicate fileops, edited'


async def main() -> None:
    client = get_runtime_client()
    try:
        await client.write_file(PATH, ORIGINAL)
        read_back = await client.read_file(PATH)
        print(f'create+read: {read_back!r}')
        assert read_back == ORIGINAL, f'expected {ORIGINAL!r}, got {read_back!r}'

        await client.edit_file(PATH, 'fileops', 'fileops, edited')
        read_back = await client.read_file(PATH)
        print(f'edit+read: {read_back!r}')
        assert read_back == REPLACED, f'expected {REPLACED!r}, got {read_back!r}'

        try:
            await client.read_file(MISSING_PATH)
        except FileNotFoundError:
            print('read of missing file correctly raised FileNotFoundError')
        else:
            raise AssertionError('expected FileNotFoundError for a missing file')

        print('Real runtime file-ops check passed.')
    finally:
        await client.aclose()


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
