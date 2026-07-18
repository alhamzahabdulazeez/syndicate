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


class GithubIssue(TypedDict, total=False):
    """Analyzer's input contract (see syndicate.nodes.analyzer_node): a real
    GitHub issue plus the repo it belongs to. `workspace_dir` points at an
    already-cloned checkout -- fetching the issue and cloning the repo are
    both out of scope here, this is the shape the analyzer consumes once
    that data exists."""

    repo: str  # "owner/name"
    number: int
    title: str
    body: str
    workspace_dir: str


class AgentState(TypedDict, total=False):
    # Real GitHub issue driving this run; when absent, analyzer/architect
    # fall back to the pre-existing stub-ticket behavior untouched.
    issue: GithubIssue
    # Analyzer's structured read of `issue`, consumed by architect_node to
    # build `active_ticket`.
    issue_analysis: dict[str, Any]
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
