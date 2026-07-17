from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, TypedDict


class TicketStatus(str, Enum):
    PENDING = 'pending'
    IN_PROGRESS = 'in_progress'
    LOCAL_PASS = 'local_pass'
    DONE = 'done'
    ESCALATED = 'escalated'


class RunStatus(str, Enum):
    """Whole-run (not per-ticket) terminal status."""

    RUNNING = 'running'
    HALTED_ESCALATION_BUDGET = 'halted_escalation_budget'


@dataclass
class DecisionSummary:
    summary: str
    attempt_count: int


class AgentState(TypedDict, total=False):
    active_ticket: dict[str, Any]
    attempt_log: list[str]
    decision_ledger: list[DecisionSummary]
    ticket_status: TicketStatus
    last_validation_passed: bool
    # Unified per-ticket retry budget shared by executor fast-checks and the
    # real validator's pytest run (see syndicate.nodes._MAX_STRIKES).
    strike_count: int
    # Truncated pytest failure output carried from validator back to executor
    # as feedback for the next attempt.
    validation_diagnosis: str | None
    # Run-wide (cumulative across tickets) escalation count and terminal
    # status; see syndicate.nodes._MAX_ESCALATIONS and escalate_node.
    escalation_count: int
    run_status: RunStatus
