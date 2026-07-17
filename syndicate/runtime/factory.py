"""Selects which RuntimeClient adapter to instantiate, driven entirely by
Syndicate's own SYNDICATE_* configuration namespace -- never OpenHands- or
any other backend-specific env vars. Adding a new backend means adding a
module beside openhands.py/mock.py and a branch here; syndicate/nodes.py
never changes.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from syndicate.runtime.base import RuntimeClient
from syndicate.runtime.mock import MockRuntimeClient
from syndicate.runtime.openhands import OpenHandsRuntimeClient


def _runtime_base_url() -> str:
    url = os.environ.get('SYNDICATE_RUNTIME_URL')
    if not url:
        raise RuntimeError(
            'SYNDICATE_RUNTIME_URL is not set. It must point at a running '
            'OpenHands agent server, e.g. http://localhost:60000.'
        )
    url = url.rstrip('/')

    port = os.environ.get('SYNDICATE_RUNTIME_PORT')
    if port and urlparse(url).port is None:
        url = f'{url}:{port}'

    return url


def get_runtime_client() -> RuntimeClient:
    if os.environ.get('SYNDICATE_MOCK_CLIENT') == '1':
        return MockRuntimeClient()

    base_url = _runtime_base_url()
    session_api_key = os.environ.get('SYNDICATE_RUNTIME_SESSION_API_KEY')
    timeout = float(os.environ.get('SYNDICATE_RUNTIME_TIMEOUT_SECONDS', '300'))
    return OpenHandsRuntimeClient(
        base_url=base_url, session_api_key=session_api_key, timeout=timeout
    )
