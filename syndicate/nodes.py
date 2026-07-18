from __future__ import annotations

import json
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
from syndicate.state import (
    AgentState,
    DecisionSummary,
    GithubIssue,
    RunStatus,
    TicketStatus,
)

_DUMMY_ACTIONS = ['git status', 'python -m pytest']
# Model used by analyzer when making real LLM-assisted runs (not invoked in mock mode).
_ANALYZER_MODEL = 'claude-sonnet-4-6'
# Model used by architect when making real LLM-assisted runs (not invoked in mock mode).
_ARCHITECT_MODEL = 'claude-sonnet-4-6'
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
# Analyzer and Architect — Step 8: turn a real GitHub issue into the ticket
# shape the executor already consumes. Analyzer's input contract is
# `state["issue"]` (see GithubIssue in syndicate/state.py) -- issue text plus
# the repo it belongs to; fetching that issue and cloning the repo both
# happen upstream of this graph, not here. When no issue is present, both
# nodes fall back to the original stub-ticket pass-through unchanged, so
# every existing mock-mode caller (smoke_e2e_mock.py, the pytest suite) keeps
# working with no `issue` key at all.
#
# Both go through the executor's existing LLM seam, _get_llm_client(): real
# mode makes a real Anthropic call, SYNDICATE_STUB_LLM=1 swaps in the
# scripted stand-in with no network and no key. Analyzer and architect use
# distinct stub-script keys (see _issue_key) so they can be scripted
# independently of each other and of the executor's own ticket-id-keyed
# scripts.
# ---------------------------------------------------------------------------


def _issue_key(issue: GithubIssue, role: str) -> str:
    return f'{role}:{issue.get("repo", "")}#{issue.get("number", "")}'


def _parse_llm_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f'LLM response was not valid JSON: {text!r}') from exc
    if not isinstance(parsed, dict):
        raise ValueError(f'LLM response JSON was not an object: {text!r}')
    return parsed


async def _call_llm_json(key: str, model: str, prompt: str, max_tokens: int) -> dict[str, Any]:
    client = _get_llm_client(key)
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[],
        messages=[{'role': 'user', 'content': prompt}],
    )
    text = ''.join(block.text for block in response.content if block.type == 'text')
    return _parse_llm_json(text)


def _analyzer_prompt(issue: GithubIssue) -> str:
    return (
        'You are triaging a GitHub issue for automated resolution.\n'
        f'Repo: {issue.get("repo", "")}\n'
        f'Issue #{issue.get("number", "")}: {issue.get("title", "")}\n\n'
        f'{issue.get("body", "")}\n\n'
        'Reply with a single JSON object, no prose, with keys:\n'
        '  "problem_summary": one sentence stating the bug or request\n'
        '  "root_cause_hypothesis": your best guess at the underlying cause\n'
        '  "suspected_files": repo-relative file paths likely needing changes\n'
    )


# Repo-relative manifest file -> the test command it implies, checked in
# order against the real checkout via RuntimeClient.read_file (the only
# channel Syndicate has into the sandbox filesystem -- see CLAUDE.md's
# decoupling constraint). This is real-repo grounding for
# verification_command, not a guess from issue text alone.
_TEST_MANIFEST_COMMANDS: list[tuple[str, str]] = [
    ('pyproject.toml', 'python3 -m pytest -q'),
    ('pytest.ini', 'python3 -m pytest -q'),
    ('setup.cfg', 'python3 -m pytest -q'),
    ('package.json', 'npm test'),
    ('Cargo.toml', 'cargo test'),
    ('go.mod', 'go test ./...'),
    ('Gemfile', 'bundle exec rspec'),
]


async def _detect_verification_command(
    client: RuntimeClient, workspace_dir: str
) -> str | None:
    for filename, command in _TEST_MANIFEST_COMMANDS:
        try:
            await client.read_file(f'{workspace_dir}/{filename}')
        except FileNotFoundError:
            continue
        return command
    return None


def _architect_prompt(
    issue: GithubIssue, analysis: dict[str, Any], detected_command: str | None
) -> str:
    detection_note = (
        f'Test tooling detected in the checkout: {detected_command!r}. Use this '
        'unless the issue clearly calls for a more specific command (e.g. one '
        'test file).'
        if detected_command
        else 'No recognized test manifest was found in the checkout root. You '
        'must still specify a verification_command based on the issue and repo.'
    )
    return (
        'You are turning a triaged GitHub issue into a work ticket for an '
        'autonomous coding agent.\n'
        f'Repo: {issue.get("repo", "")}\n'
        f'Issue #{issue.get("number", "")}: {issue.get("title", "")}\n\n'
        f'{issue.get("body", "")}\n\n'
        f'Analysis:\n{json.dumps(analysis)}\n\n'
        f'{detection_note}\n\n'
        'Reply with a single JSON object, no prose, with keys:\n'
        '  "title": short imperative summary of the fix\n'
        '  "intent": one sentence describing the change, used in the commit message\n'
        '  "files_changed": repo-relative file paths this ticket will edit\n'
        '  "verification_command": shell command run in the workspace to confirm the fix\n'
    )


async def analyzer_node(state: AgentState) -> dict[str, Any]:
    issue = state.get('issue')
    if issue is None:
        ticket: dict[str, Any] = state.get('active_ticket') or {
            'id': 'TICKET-1',
            'title': 'stub ticket',
        }
        return {'active_ticket': ticket}

    analysis = await _call_llm_json(
        _issue_key(issue, 'analyzer'),
        _ANALYZER_MODEL,
        _analyzer_prompt(issue),
        max_tokens=1024,
    )
    return {'issue_analysis': analysis}


async def architect_node(state: AgentState) -> dict[str, Any]:
    issue = state.get('issue')
    analysis = state.get('issue_analysis')
    if issue is None or analysis is None:
        return {}

    workspace_dir = issue.get('workspace_dir') or _DEFAULT_WORKSPACE_DIR
    detected_command = await _detect_verification_command(
        get_runtime_client(), workspace_dir
    )

    planned = await _call_llm_json(
        _issue_key(issue, 'architect'),
        _ARCHITECT_MODEL,
        _architect_prompt(issue, analysis, detected_command),
        max_tokens=1024,
    )

    verification_command = planned.get('verification_command') or detected_command
    if not verification_command:
        repo = issue.get('repo', '')
        number = issue.get('number', '')
        raise ValueError(
            f'architect could not derive a verification_command for {repo}#{number}: '
            'no recognized test manifest in the checkout and the model did not '
            'provide one. Refusing to fabricate a default -- fix the issue/repo '
            'input or extend _TEST_MANIFEST_COMMANDS.'
        )

    ticket: dict[str, Any] = {
        'id': f'{issue.get("repo", "issue")}#{issue.get("number", "0")}',
        'title': planned.get('title') or issue.get('title') or 'untitled',
        'intent': planned.get('intent') or planned.get('title') or 'update',
        'files_changed': planned.get('files_changed') or [],
        'workspace_dir': workspace_dir,
        'verification_command': verification_command,
    }
    return {'active_ticket': ticket}


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
# PASS. Never invoked on the fail path (see _validator_router). DONE is
# never taken on the process's word for it -- a commit exit code is the
# process's claim about itself, not proof. HEAD is checked before and after
# so a lying/lenient runtime client, a no-op commit ("nothing to commit"),
# or a rejected hook all fail the same way: no DONE, one strike, routed
# through the existing retry/escalate budget rather than a new failure path.
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

    def strike(reason: str) -> dict[str, Any]:
        git_log.append(f'oversight_git: {reason}')
        return {
            'attempt_log': attempt_log + git_log,
            'strike_count': (state.get('strike_count') or 0) + 1,
        }

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

    # HEAD before staging -- fails legitimately on a brand-new repo with no
    # commits yet, in which case DONE below only requires HEAD to resolve.
    head_before_result = await run('git rev-parse HEAD')
    head_before = (
        head_before_result.output.strip() if head_before_result.exit_code == 0 else None
    )

    # Stage only the ticket's explicit paths -- built from a fixed list, never
    # a wildcard, so there is no path-expansion-based staging here.
    paths = ' '.join(shlex.quote(f) for f in files)
    add_result = await run(f'git add -- {paths}')
    if add_result.exit_code != 0:
        return strike('git add failed; nothing staged, no commit attempted')

    intent = ticket.get('intent') or ticket.get('title') or 'update'
    ticket_id = ticket.get('id') or ticket.get('ticket_id') or 'TICKET'
    body = '; '.join(attempt_log) if attempt_log else 'no attempts'
    message = f'[{ticket_id}] {intent}\n\n{body}'
    commit_result = await run(f'git commit -m {shlex.quote(message)}')
    if commit_result.exit_code != 0:
        return strike(
            f'git commit failed (exit={commit_result.exit_code}); declared '
            f'files_changed={files!r} produced no commit'
        )

    # Don't trust the commit's own exit code -- ask git whether a commit
    # actually landed. A ticket that declared files_changed but produced no
    # real diff (or a runtime client that reports success without acting)
    # is failure mode 3 one layer up: a claimed edit that never happened.
    head_after_result = await run('git rev-parse HEAD')
    head_after = (
        head_after_result.output.strip() if head_after_result.exit_code == 0 else None
    )
    if head_after is None or head_after == head_before:
        return strike(
            f'git commit exited 0 but HEAD did not move (before={head_before!r}, '
            f'after={head_after!r}); declared files_changed={files!r} showed no changes'
        )

    return {'attempt_log': attempt_log + git_log, 'ticket_status': TicketStatus.DONE}


def _oversight_git_router(state: AgentState) -> str:
    if state.get('ticket_status') == TicketStatus.DONE:
        return 'advance'
    # A ticket that never declared files to change made no commit attempt at
    # all (see the early return in oversight_git_node) -- that's a no-op,
    # not a failed attempt, and isn't retried. Only a ticket that declared
    # files and then failed to produce a verified commit takes the
    # retry/escalate path below.
    ticket = state.get('active_ticket') or {}
    files = ticket.get('files_changed') or ticket.get('target_files') or []
    if not files:
        return 'advance'
    if (state.get('strike_count') or 0) >= _MAX_STRIKES:
        return 'escalate'
    return 'executor'


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
    builder.add_conditional_edges(
        'oversight_git',
        _oversight_git_router,
        {'advance': 'advance', 'executor': 'executor', 'escalate': 'escalate'},
    )
    builder.add_edge('escalate', 'advance')
    builder.add_edge('advance', 'dispatch')

    return builder.compile(checkpointer=checkpointer)
