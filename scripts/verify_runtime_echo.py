#!/usr/bin/env python3
"""Real-runtime smoke test for Step 4 (Real Runtime Integration).

Connects to a *live* OpenHands agent server (SYNDICATE_MOCK_CLIENT unset) and
runs a plain echo command through RuntimeClient.run_action, to prove the
real HTTP integration actually works end to end.

Requires a running OpenHands agent server, configured via env:
    SYNDICATE_RUNTIME_URL=http://<host>[:<port>]      (required)
    SYNDICATE_RUNTIME_PORT=<port>                      (optional, appended if
                                                        SYNDICATE_RUNTIME_URL
                                                        has no port)
    SYNDICATE_RUNTIME_SESSION_API_KEY=<key>            (optional, sent as
                                                        X-Session-API-Key)

Run with:
    SYNDICATE_RUNTIME_URL=http://localhost:60000 python3 scripts/verify_runtime_echo.py
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

MARKER = 'syndicate_runtime_ok'


async def main() -> None:
    client = get_runtime_client()
    try:
        result = await client.run_action(f'echo {MARKER}')

        print(f'ActionResult(exit_code={result.exit_code!r}, output={result.output!r})')

        assert result.exit_code == 0, f'expected exit_code 0, got {result.exit_code!r}'
        assert MARKER in result.output, (
            f'expected marker {MARKER!r} in output, got {result.output!r}'
        )

        print('Real runtime echo check passed.')
    finally:
        await client.aclose()


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
