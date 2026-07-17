"""API request/response models.

syndicate/ has no Pydantic models to reuse (see artifacts/ui/00_audit.md,
finding (c)) -- AgentState is a plain TypedDict, DecisionSummary a plain
dataclass. This defines the one model the API actually needs new; state
values are reused (imported, never redefined) and serialized at the
boundary in server/events.py.
"""

from __future__ import annotations

from pydantic import BaseModel


class RunRequest(BaseModel):
    raw_request: str
