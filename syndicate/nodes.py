from __future__ import annotations

import os
import shlex
from typing import Any

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from syndicate.runtime import (
    ActionResult,
    MockRuntimeClient,
    RuntimeClient,
    get_runtime_client,
)
from syndicate.state import AgentState, DecisionSummary, RunStatus, TicketStatus

_DUMMY_ACTIONS = ['git status', 'python -m pytest']
# Model used by architect when making real LLM-assisted runs (not invoked in mock mode).
_ARCHITECT_MODEL = 'claude-5-sonnet-latest'
# Model used by executor when making real LLM-assisted runs (not invoked in mock mode).
_EXECUTOR_MODEL = 'claude-sonnet-4-6'

# Anthropic-defined, schema-less tools -- execution is dispatched to
# RuntimeClient.run_action / read_file / write_file / edit_file below.
_EXECUTOR_TOOLS: list[dict[str, Any]] = [
    {'type': 'bash_20250124', 'name': 'bash'},
    {'type': 'text_editor_20250728', 'name': 'str_replace_based_edit_tool'},
]
_MAX_TOOL_CALLS = 25
_MAX_STRIKES = 3
_FAST_CHECK_ACTIONS = ['git status', 'python -m pytest']

# Run-wide (cumulative across tickets) escalation budget -- the higher-order
# guard on top of the per-ticket _MAX_STRIKES budget. Overridable so ops can
# tighten/loosen it without a code change; default matches _MAX_STRIKES only
# by convention, the two are independent knobs.
_MAX_ESCALATIONS = int(os.environ.get('SYNDICATE_MAX_ESCALATIONS', '3'))

# Real validator (Step 6): command run inside the sandbox when a ticket
# doesn't carry its own `verification_command`.
_DEFAULT_VERIFICATION_COMMAND = 'python3 -m pytest -q'
_VALIDATOR_TIMEOUT_SECONDS = 300.0
# Assumes the openhands-runtime image's default sandbox user; tickets should
# normally set active_ticket["workspace_dir"] explicitly.
_DEFAULT_WORKSPACE_DIR = '/home/openhands/syndicate-workspace'
_DIAGNOSIS_MAX_LINES = 60
_DIAGNOSIS_MAX_BYTES = 4096

_GIT_BOT_NAME = 'syndicate-bot'
_GIT_BOT_EMAIL = 'syndicate-bot@example.invalid'


# ---------------------------------------------------------------------------
# Analyzer and Architect — stub implementations; do not modify.
# ---------------------------------------------------------------------------


def analyzer_node(state: AgentState) -> dict[str, Any]:
    ticket: dict[str, Any] = state.get('active_ticket') or {
        'id': 'TICKET-1',
        'title': 'stub ticket',
    }
    return {'active_ticket': ticket}


def architect_node(state: AgentState) -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Dispatch — conditional router node
# ---------------------------------------------------------------------------


def dispatch_node(state: AgentState) -> dict[str, Any]:
    return {}


def _dispatch_router(state: AgentState) -> str:
    # Run-wide halt overrides everything else, regardless of this ticket's
    # own outcome or whether more tickets remain.
    if state.get('run_status') == RunStatus.HALTED_ESCALATION_BUDGET:
        return END
    # No ticket-queue/next-ticket dispatch exists yet (single-ticket runs
    # only); an escalated ticket is terminal for the run same as a pass.
    if state.get('ticket_status') == TicketStatus.ESCALATED:
        return END
    if state.get('last_validation_passed'):
        return END
    return 'executor'


# ---------------------------------------------------------------------------
# Executor — Step 5: real Claude tool-calling loop with a 3-strike
# self-healing retry, escalating after 3 failed attempts. The strike budget
# (state["strike_count"]) is shared with the real validator (Step 6): a
# validator failure resumes counting here rather than granting a fresh 3.
# ---------------------------------------------------------------------------


async def _run_tool(
    client: RuntimeClient, name: str, tool_input: dict[str, Any], cwd: str
) -> tuple[str, bool]:
    """Execute one tool_use block against the runtime client.

    Returns (content, is_error) for the corresponding tool_result block.
    """
    try:
        if name == 'bash':
            if tool_input.get('restart'):
                return 'bash session restarted', False
            result = await client.run_action(tool_input['command'], cwd=cwd)
            if result.exit_code != 0:
                return f'exit_code={result.exit_code}\n{result.output}', True
            return result.output, False
        if name == 'str_replace_based_edit_tool':
            command = tool_input['command']
            path = tool_input['path']
            if command == 'view':
                return await client.read_file(path), False
            if command == 'create':
                await client.write_file(path, tool_input['file_text'])
                return f'created {path}', False
            if command == 'str_replace':
                await client.edit_file(
                    path, tool_input['old_str'], tool_input['new_str']
                )
                return f'edited {path}', False
            return f'unsupported command {command!r}', True
        return f'unknown tool {name!r}', True
    except (FileNotFoundError, ValueError) as exc:
        return str(exc), True


def _get_llm_client(ticket_id: str | None) -> Any:
    """The executor's one LLM-call seam.

    SYNDICATE_STUB_LLM=1 (mirroring SYNDICATE_MOCK_CLIENT) swaps in a scripted
    stand-in (syndicate.stub_llm) with the identical call signature and
    response shape as anthropic.AsyncAnthropic() -- see syndicate/stub_llm.py.
    Never invoked in mock-client mode (executor_node short-circuits before
    reaching here); real mode with no key set fails naturally when this
    constructs the real client, exactly as before.
    """
    if os.environ.get('SYNDICATE_STUB_LLM') == '1':
        from syndicate.stub_llm import get_stub_client

        return get_stub_client(ticket_id or '')

    import anthropic  # type: ignore[import-not-found]

    return anthropic.AsyncAnthropic()


async def _run_agentic_loop(
    client: RuntimeClient,
    task_prompt: str,
    feedback: str | None,
    cwd: str,
    ticket_id: str | None = None,
) -> str:
    """Drive one Claude tool-calling attempt, capped at _MAX_TOOL_CALLS turns."""
    anthropic_client = _get_llm_client(ticket_id)
    user_content = (
        task_prompt
        if feedback is None
        else f'{task_prompt}\n\nThe previous attempt failed its checks:\n{feedback}'
    )
    messages: list[dict[str, Any]] = [{'role': 'user', 'content': user_content}]

    for _ in range(_MAX_TOOL_CALLS):
        response = await anthropic_client.messages.create(
            model=_EXECUTOR_MODEL,
            max_tokens=8096,
            tools=_EXECUTOR_TOOLS,
            messages=messages,
        )
        messages.append({'role': 'assistant', 'content': response.content})

        if response.stop_reason != 'tool_use':
            return ''.join(
                block.text for block in response.content if block.type == 'text'
            )

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != 'tool_use':
                continue
            content, is_error = await _run_tool(client, block.name, block.input, cwd)
            tool_results.append(
                {
                    'type': 'tool_result',
                    'tool_use_id': block.id,
                    'content': content,
                    'is_error': is_error,
                }
            )
        messages.append({'role': 'user', 'content': tool_results})

    return 'tool call cap reached without a final answer'


async def _run_fast_checks(client: RuntimeClient, cwd: str) -> ActionResult:
    """Local, LLM-free sanity checks run after each attempt.

    Step 7.5 found these previously ran with no `cwd` at all, so on a real
    client they executed wherever the container's default directory
    happened to be (not the ticket's workspace) and failed unconditionally
    (e.g. "git status" -> "not a git repository"). Threaded the same
    workspace_dir validator_node already uses.
    """
    for action in _FAST_CHECK_ACTIONS:
        result = await client.run_action(action, cwd=cwd)
        if result.exit_code != 0:
            return result
    return ActionResult(exit_code=0, output='fast checks passed')


async def executor_node(state: AgentState) -> dict[str, Any]:
    client = get_runtime_client()
    log: list[str] = list(state.get('attempt_log') or [])

    if isinstance(client, MockRuntimeClient):
        for action in _DUMMY_ACTIONS:
            result = await client.run_action(action)
            log.append(
                f'action={action!r} exit_code={result.exit_code} output={result.output!r}'
            )
        return {'attempt_log': log, 'ticket_status': TicketStatus.LOCAL_PASS}

    ticket = state.get('active_ticket') or {}
    ticket_id = ticket.get('id') or ticket.get('ticket_id')
    workspace_dir = ticket.get('workspace_dir') or _DEFAULT_WORKSPACE_DIR
    task_prompt = f'Ticket: {ticket!r}'
    feedback: str | None = state.get('validation_diagnosis')
    strike = state.get('strike_count') or 0

    while strike < _MAX_STRIKES:
        strike += 1
        summary = await _run_agentic_loop(
            client, task_prompt, feedback, workspace_dir, ticket_id
        )
        log.append(f'strike={strike} summary={summary!r}')

        check = await _run_fast_checks(client, workspace_dir)
        log.append(
            f'strike={strike} fast_check exit_code={check.exit_code} output={check.output!r}'
        )

        if check.exit_code == 0:
            return {
                'attempt_log': log,
                'ticket_status': TicketStatus.LOCAL_PASS,
                'strike_count': strike,
                'validation_diagnosis': None,
            }

        feedback = check.output

    return {
        'attempt_log': log,
        'ticket_status': TicketStatus.ESCALATED,
        'strike_count': strike,
    }


# ---------------------------------------------------------------------------
# Validator — Step 6: real pytest execution inside the sandbox. Mock mode
# keeps the Step 5 stub behavior unchanged.
# ---------------------------------------------------------------------------


def _truncate_diagnosis(output: str) -> str:
    lines = output.splitlines()
    truncated = '\n'.join(lines[-_DIAGNOSIS_MAX_LINES:])
    encoded = truncated.encode()
    if len(encoded) > _DIAGNOSIS_MAX_BYTES:
        truncated = encoded[-_DIAGNOSIS_MAX_BYTES:].decode(errors='replace')
    return truncated


async def validator_node(state: AgentState) -> dict[str, Any]:
    client = get_runtime_client()

    if isinstance(client, MockRuntimeClient):
        return {'last_validation_passed': True}

    ticket = state.get('active_ticket') or {}
    command = ticket.get('verification_command') or _DEFAULT_VERIFICATION_COMMAND
    cwd = ticket.get('workspace_dir') or _DEFAULT_WORKSPACE_DIR

    result = await client.run_action(
        command, cwd=cwd, timeout=_VALIDATOR_TIMEOUT_SECONDS
    )

    if result.exit_code == 0:
        return {'last_validation_passed': True, 'validation_diagnosis': None}

    return {
        'last_validation_passed': False,
        'validation_diagnosis': _truncate_diagnosis(result.output),
        'strike_count': (state.get('strike_count') or 0) + 1,
    }


def _validator_router(state: AgentState) -> str:
    if state.get('last_validation_passed'):
        return 'oversight_git'
    if (state.get('strike_count') or 0) >= _MAX_STRIKES:
        return 'escalate'
    return 'executor'


# ---------------------------------------------------------------------------
# Escalate — Step 7 (Task 0a/0b): marks the ticket ESCALATED and bumps the
# run-wide escalation budget, then hands off to `advance` so the failed
# ticket's attempt_log is distilled into decision_ledger before the graph
# exits -- previously this branch routed straight to END, so failures never
# reached the audit trail that successes (via oversight_git -> advance) did.
# ---------------------------------------------------------------------------


def escalate_node(state: AgentState) -> dict[str, Any]:
    escalation_count = (state.get('escalation_count') or 0) + 1
    result: dict[str, Any] = {
        'ticket_status': TicketStatus.ESCALATED,
        'escalation_count': escalation_count,
    }
    if escalation_count > _MAX_ESCALATIONS:
        result['run_status'] = RunStatus.HALTED_ESCALATION_BUDGET
    return result


# ---------------------------------------------------------------------------
# Oversight (git half of Node C) — Step 6: local-only commit on validation
# PASS. Never invoked on the fail path (see _validator_router).
# ---------------------------------------------------------------------------


async def oversight_git_node(state: AgentState) -> dict[str, Any]:
    client = get_runtime_client()
    ticket = state.get('active_ticket') or {}
    workspace_dir = ticket.get('workspace_dir') or _DEFAULT_WORKSPACE_DIR
    attempt_log = list(state.get('attempt_log') or [])
    git_log: list[str] = []

    async def run(command: str) -> ActionResult:
        result = await client.run_action(command, cwd=workspace_dir)
        git_log.append(f'$ {command}\nexit={result.exit_code}\n{result.output}')
        return result

    is_repo = await run('git rev-parse --is-inside-work-tree')
    if is_repo.exit_code != 0:
        await run('git init')
    # Always (re)assert repo-local identity -- never --global, never touching
    # host/container-global git config.
    await run(f'git config user.name {shlex.quote(_GIT_BOT_NAME)}')
    await run(f'git config user.email {shlex.quote(_GIT_BOT_EMAIL)}')

    files: list[str] = ticket.get('files_changed') or ticket.get('target_files') or []
    if not files:
        return {'attempt_log': attempt_log + git_log}

    # Stage only the ticket's explicit paths -- built from a fixed list, never
    # a wildcard, so there is no path-expansion-based staging here.
    paths = ' '.join(shlex.quote(f) for f in files)
    await run(f'git add -- {paths}')

    intent = ticket.get('intent') or ticket.get('title') or 'update'
    ticket_id = ticket.get('id') or ticket.get('ticket_id') or 'TICKET'
    body = '; '.join(attempt_log) if attempt_log else 'no attempts'
    message = f'[{ticket_id}] {intent}\n\n{body}'
    await run(f'git commit -m {shlex.quote(message)}')

    return {'attempt_log': attempt_log + git_log, 'ticket_status': TicketStatus.DONE}


# ---------------------------------------------------------------------------
# Advance — distils attempt_log into a DecisionSummary and resets the log.
# Status-agnostic by construction: it joins whatever attempt_log holds
# regardless of ticket_status, so it distills equally on the oversight_git
# (pass) path and the escalate_node (fail) path -- see Task 0a.
# ---------------------------------------------------------------------------


def advance_node(state: AgentState) -> dict[str, Any]:
    log: list[str] = list(state.get('attempt_log') or [])
    summary = DecisionSummary(
        summary='; '.join(log) if log else 'no attempts',
        attempt_count=len(log),
    )
    ledger: list[DecisionSummary] = list(state.get('decision_ledger') or [])
    return {
        'decision_ledger': ledger + [summary],
        'attempt_log': [],  # explicit reset
    }


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(checkpointer: Checkpointer = None) -> CompiledStateGraph:
    """checkpointer defaults to None (no persistence), preserving prior
    behavior for every existing caller (all Steps 5-7.5 scripts call this
    with no arguments). Server layer (server/app.py) is the only caller
    that passes one, for run persistence -- see artifacts/ui/00_audit.md."""
    builder: StateGraph = StateGraph(AgentState)

    builder.add_node('analyzer', analyzer_node)
    builder.add_node('architect', architect_node)
    builder.add_node('dispatch', dispatch_node)
    builder.add_node('executor', executor_node)
    builder.add_node('validator', validator_node)
    builder.add_node('oversight_git', oversight_git_node)
    builder.add_node('escalate', escalate_node)
    builder.add_node('advance', advance_node)

    builder.set_entry_point('analyzer')
    builder.add_edge('analyzer', 'architect')
    builder.add_edge('architect', 'dispatch')
    builder.add_conditional_edges(
        'dispatch',
        _dispatch_router,
        {'executor': 'executor', END: END},
    )
    builder.add_edge('executor', 'validator')
    builder.add_conditional_edges(
        'validator',
        _validator_router,
        {
            'oversight_git': 'oversight_git',
            'executor': 'executor',
            'escalate': 'escalate',
        },
    )
    builder.add_edge('oversight_git', 'advance')
    builder.add_edge('escalate', 'advance')
    builder.add_edge('advance', 'dispatch')

    return builder.compile(checkpointer=checkpointer)
