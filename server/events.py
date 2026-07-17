"""SSE event envelope: typed, discriminated, redacted, size-capped.

Every SSE `data:` line is one JSON envelope:
    {seq, ts, run_id, kind, node, payload}

`kind` is a closed literal set. Derivation from syndicate's astream updates
is mechanical -- no chassis edits -- see server/runner.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal, TypedDict

EventKind = Literal[
    'run_started',
    'node_update',
    'attempt',
    'escalation',
    'run_completed',
    'run_failed',
    'heartbeat',
]


class Envelope(TypedDict):
    seq: int | None
    ts: str
    run_id: str
    kind: EventKind
    node: str | None
    payload: dict[str, Any]


_MAX_PAYLOAD_BYTES = 8192

# Secret-shaped patterns: Anthropic keys, generic api_key/token assignments,
# bearer tokens, AWS-style access keys, PEM private key blocks. Mirrors the
# redaction pattern used in prior steps' hygiene secrets-scans -- nothing
# resembling a secret may be echoed into events or the UI, even though this
# build makes no real LLM calls (env keys could still appear in captured
# bash output, e.g. `env` or a misguided `echo $ANTHROPIC_API_KEY`).
_SECRET_PATTERNS = [
    re.compile(r'sk-ant-[A-Za-z0-9_-]{10,}'),
    re.compile(
        r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{12,}['\"]?"
    ),
    re.compile(r'(?i)bearer\s+[A-Za-z0-9_\-\.]{12,}'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(
        r'-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----'
    ),
]

_REDACTED = '[REDACTED]'


def redact_text(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return redact_value(asdict(value))
    return value


def cap_payload(
    payload: dict[str, Any], max_bytes: int = _MAX_PAYLOAD_BYTES
) -> dict[str, Any]:
    """The UI is a monitor, not a log archive: cap any single payload,
    truncating with an explicit marker rather than silently growing SSE
    frames (or backlog memory) unbounded."""
    encoded = json.dumps(payload, default=str)
    if len(encoded.encode()) <= max_bytes:
        return payload
    preview = encoded[: max_bytes - 200]
    return {
        'truncated': True,
        'original_size_bytes': len(encoded.encode()),
        'preview': preview,
    }


def make_envelope(
    *,
    seq: int | None,
    run_id: str,
    kind: EventKind,
    node: str | None,
    payload: dict[str, Any],
) -> Envelope:
    safe_payload = cap_payload(redact_value(payload))
    return {
        'seq': seq,
        'ts': datetime.now(UTC).isoformat(),
        'run_id': run_id,
        'kind': kind,
        'node': node,
        'payload': safe_payload,
    }


def format_sse(envelope: Envelope) -> str:
    return f'data: {json.dumps(envelope)}\n\n'
