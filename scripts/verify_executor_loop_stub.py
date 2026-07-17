#!/usr/bin/env python3
"""Step 7.5: stub-LLM control-flow validation of the REAL executor node.

Drives syndicate.nodes.executor_node (and, per scenario, validator_node /
oversight_git_node / escalate_node / advance_node / _dispatch_router --
all the real node functions, chained directly the same way
scripts/verify_step6_validation_git.py chained validator_node ->
oversight_git_node) against the REAL RuntimeClient / REAL container.
Only the LLM is faked, via syndicate.stub_llm (SYNDICATE_STUB_LLM=1) --
identical call signature/response shape to the real anthropic binding, so
this exercises the actual LLM-call seam, not a bypass of it.

No network, no ANTHROPIC_API_KEY required.

Run one scenario at a time:
    SYNDICATE_RUNTIME_URL=http://localhost SYNDICATE_RUNTIME_PORT=8000 \
        python3 scripts/verify_executor_loop_stub.py <happy|cap|strikes|context>
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

# Real client, real container -- never the mock.
os.environ.pop('SYNDICATE_MOCK_CLIENT', None)
# Real executor code path, stubbed brain -- no key needed.
os.environ['SYNDICATE_STUB_LLM'] = '1'

from syndicate.nodes import (  # noqa: E402
    _MAX_STRIKES,
    _MAX_TOOL_CALLS,
    _dispatch_router,
    advance_node,
    escalate_node,
    executor_node,
    oversight_git_node,
    validator_node,
)
from syndicate.runtime import RuntimeClient, get_runtime_client  # noqa: E402
from syndicate.state import AgentState, TicketStatus  # noqa: E402
from syndicate.stub_llm import (  # noqa: E402
    complete_turn,
    get_call_history,
    set_stub_script,
    tool_turn,
)

BASE_WORKSPACE = os.environ.get(
    'SYNDICATE_STUB_WORKSPACE_BASE', '/home/openhands/syndicate-workspace/step7_5'
)

CALC_BUGGY = 'def add(a, b):\n    return a - b\n'
TEST_CALC_PY = 'from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n'


async def seed_calc_project(client: RuntimeClient, workspace_dir: str) -> None:
    """A tiny git+pytest project with calc.py deliberately buggy, tracked at
    HEAD -- mirrors verify_step6_validation_git.py's seed_workspace so a
    scripted edit_file str_replace has a real, single-occurrence match."""
    setup = await client.run_action(f'mkdir -p {workspace_dir}/tests')
    assert setup.exit_code == 0, f'workspace mkdir failed: {setup.output}'
    await client.write_file(f'{workspace_dir}/calc.py', CALC_BUGGY)
    await client.write_file(f'{workspace_dir}/tests/test_calc.py', TEST_CALC_PY)
    await client.write_file(
        f'{workspace_dir}/.gitignore', '__pycache__/\n*.pyc\n.pytest_cache/\n'
    )
    # pytest itself is a pre-installed, one-time container dependency (see
    # Step 7.5 hygiene notes: installing it required a brief, deliberate
    # unlock/relock of the Step 7 egress lock) -- not installed per seed, so
    # this stays egress-lock-safe and idempotent.
    # `git commit` is allowed to no-op (`|| true`) on a rerun against an
    # already-seeded workspace where content hasn't changed since the last
    # commit -- write_file above already unconditionally reset calc.py to
    # CALC_BUGGY, which is what matters for idempotency, not a fresh commit.
    init = await client.run_action(
        'git init -q '
        '&& git config user.name syndicate-bot '
        '&& git config user.email syndicate-bot@example.invalid '
        '&& git add -- calc.py tests/test_calc.py .gitignore '
        '&& (git commit -q -m seed || true)',
        cwd=workspace_dir,
    )
    assert init.exit_code == 0, f'workspace git setup failed: {init.output}'
    log = await client.run_action('git log --oneline', cwd=workspace_dir)
    assert log.exit_code == 0 and log.output.strip(), (
        f'expected at least one commit in seeded workspace, got: {log.output!r}'
    )


def make_ticket(ticket_id: str, workspace_dir: str, **overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        'id': ticket_id,
        'intent': f'step7.5 stub-llm scenario {ticket_id}',
        'verification_command': 'python3 -m pytest -q',
        'workspace_dir': workspace_dir,
        'files_changed': ['calc.py'],
    }
    ticket.update(overrides)
    return ticket


# ---------------------------------------------------------------------------
# Scenario 1: happy path, multi-tool
# ---------------------------------------------------------------------------


async def scenario_happy(client: RuntimeClient) -> None:
    print('=== Scenario 1: happy path, multi-tool ===')
    workspace_dir = f'{BASE_WORKSPACE}/happy'
    ticket_id = 'STUB-HAPPY'
    calc_path = f'{workspace_dir}/calc.py'

    await seed_calc_project(client, workspace_dir)

    set_stub_script(
        ticket_id,
        [
            tool_turn(
                ('str_replace_based_edit_tool', {'command': 'view', 'path': calc_path})
            ),
            tool_turn(
                (
                    'str_replace_based_edit_tool',
                    {
                        'command': 'str_replace',
                        'path': calc_path,
                        'old_str': 'return a - b',
                        'new_str': 'return a + b',
                    },
                )
            ),
            tool_turn(('bash', {'command': 'python3 -m pytest -q'})),
            complete_turn('fixed calc.add and verified with pytest'),
        ],
    )

    ticket = make_ticket(ticket_id, workspace_dir)
    state: AgentState = {'active_ticket': ticket}

    executor_result = await executor_node(state)
    state.update(executor_result)  # type: ignore[typeddict-item]
    print(f'executor_node ticket_status: {state.get("ticket_status")}')
    assert state.get('ticket_status') == TicketStatus.LOCAL_PASS, (
        f'expected LOCAL_PASS from executor, got {state.get("ticket_status")!r}: '
        f'{executor_result.get("attempt_log")}'
    )
    for line in executor_result.get('attempt_log', []):
        print(f'  attempt_log: {line}')

    history = get_call_history(ticket_id)
    assert len(history) == 1, (
        f'expected exactly one attempt (strike), got {len(history)}'
    )
    calls = history[0].calls_seen
    print(f'stub LLM turns this attempt: {len(calls)}')
    assert len(calls) == 4, (
        f'expected 4 scripted turns (view, edit, verify, complete), got {len(calls)}'
    )

    print('--- tool -> client method dispatch trace ---')
    # Turn 0's tool_use (view) is executed and its result appears as the
    # tool_result in turn 1's *incoming* messages (the last message).
    view_result_msg = calls[1]['messages'][-1]['content'][0]
    print(
        f'turn0 dispatch=read_file tool_result.is_error={view_result_msg["is_error"]} '
        f'content={view_result_msg["content"]!r}'
    )
    assert not view_result_msg['is_error'], 'expected read_file (view) to succeed'
    assert view_result_msg['content'] == CALC_BUGGY, (
        f'expected the real (buggy) calc.py content fed back, got {view_result_msg["content"]!r}'
    )

    edit_result_msg = calls[2]['messages'][-1]['content'][0]
    print(
        f'turn1 dispatch=edit_file tool_result.is_error={edit_result_msg["is_error"]} '
        f'content={edit_result_msg["content"]!r}'
    )
    assert not edit_result_msg['is_error'], (
        'expected edit_file (str_replace) to succeed'
    )
    assert edit_result_msg['content'] == f'edited {calc_path}'

    verify_result_msg = calls[3]['messages'][-1]['content'][0]
    print(
        f'turn2 dispatch=run_action(bash) tool_result.is_error={verify_result_msg["is_error"]} '
        f'content={verify_result_msg["content"]!r}'
    )
    assert not verify_result_msg['is_error'], (
        f'expected the real bash pytest run inside the scripted loop to pass, '
        f'got: {verify_result_msg["content"]!r}'
    )
    assert '1 passed' in verify_result_msg['content']

    read_back = await client.read_file(calc_path)
    print(f'calc.py after scripted edit_file (real read): {read_back!r}')
    assert 'return a + b' in read_back, (
        'expected the scripted edit to have really landed on disk'
    )

    # Drive the rest of the real pipeline: validator (real pytest) ->
    # oversight_git (real git commit) -> advance (distill) -> dispatch (END).
    validator_result = await validator_node(state)
    state.update(validator_result)  # type: ignore[typeddict-item]
    print(
        f'validator_node last_validation_passed={state.get("last_validation_passed")}'
    )
    assert state.get('last_validation_passed') is True

    from syndicate.nodes import _validator_router

    route = _validator_router(state)
    assert route == 'oversight_git', f'expected oversight_git routing, got {route!r}'

    git_result = await oversight_git_node(state)
    state.update(git_result)  # type: ignore[typeddict-item]
    print(f'oversight_git_node ticket_status: {state.get("ticket_status")}')
    assert state.get('ticket_status') == TicketStatus.DONE

    advance_result = advance_node(state)
    state.update(advance_result)  # type: ignore[typeddict-item]
    print(
        f'decision_ledger entries after advance: {len(state.get("decision_ledger") or [])}'
    )
    assert len(state.get('decision_ledger') or []) == 1
    assert state.get('attempt_log') == []

    final_route = _dispatch_router(state)
    print(f'final dispatch route: {final_route!r}')
    from langgraph.graph import END

    assert final_route == END

    print('Scenario 1: PASS')


# ---------------------------------------------------------------------------
# Scenario 2: inner-cap safety (endless tool-call loop that never completes)
# ---------------------------------------------------------------------------


async def scenario_cap(client: RuntimeClient) -> None:
    print('=== Scenario 2: inner-cap safety (runaway attempt bounded) ===')
    workspace_dir = f'{BASE_WORKSPACE}/cap'
    ticket_id = 'STUB-CAP'

    # Deliberately not git-initialized: "git status" fails every strike's
    # fast-check deterministically, regardless of pytest/tooling, so this
    # scenario is purely about the tool-call cap, not fast-check nuance.
    setup = await client.run_action(f'mkdir -p {workspace_dir}')
    assert setup.exit_code == 0

    # A single tool_turn with no completion turn: the stub repeats it
    # forever (see StubAnthropicClient.next_turn), so the real
    # anthropic-shaped loop in _run_agentic_loop can only be stopped by its
    # own _MAX_TOOL_CALLS cap -- never by the script "deciding" to finish.
    set_stub_script(ticket_id, [tool_turn(('bash', {'command': 'true'}))])

    ticket = make_ticket(ticket_id, workspace_dir)
    state: AgentState = {'active_ticket': ticket}

    started = time.monotonic()
    executor_result = await executor_node(state)
    elapsed = time.monotonic() - started
    state.update(executor_result)  # type: ignore[typeddict-item]

    print(f'executor_node wall time: {elapsed:.2f}s')
    print(f'executor_node ticket_status: {state.get("ticket_status")}')
    print(f'executor_node strike_count: {state.get("strike_count")}')
    assert elapsed < 120, f'runaway not bounded: took {elapsed:.2f}s'
    assert state.get('ticket_status') == TicketStatus.ESCALATED, (
        f'expected ESCALATED (recorded as a failed attempt), got {state.get("ticket_status")!r}'
    )
    assert state.get('strike_count') == _MAX_STRIKES

    history = get_call_history(ticket_id)
    assert len(history) == _MAX_STRIKES, (
        f'expected {_MAX_STRIKES} attempts, got {len(history)}'
    )
    for i, attempt in enumerate(history, start=1):
        print(
            f'strike {i}: {len(attempt.calls_seen)} stub LLM turns '
            f'(cap={_MAX_TOOL_CALLS})'
        )
        assert len(attempt.calls_seen) == _MAX_TOOL_CALLS, (
            f'strike {i}: expected the inner cap ({_MAX_TOOL_CALLS} turns) to fire, '
            f'got {len(attempt.calls_seen)}'
        )

    cap_hits = sum(
        1
        for line in executor_result.get('attempt_log', [])
        if 'tool call cap reached without a final answer' in line
    )
    print(f'attempt_log lines reporting cap-reached: {cap_hits}')
    assert cap_hits == _MAX_STRIKES, (
        f'expected every one of the {_MAX_STRIKES} strikes to report cap-reached, got {cap_hits}'
    )

    # Full pipeline: since the workspace was never a git repo, validator's
    # real pytest run also fails -> _validator_router must agree this is
    # "escalate" (budget already exhausted at the executor level).
    validator_result = await validator_node(state)
    state.update(validator_result)  # type: ignore[typeddict-item]
    from syndicate.nodes import _validator_router

    route = _validator_router(state)
    print(f'post-validator routing: {route!r}')
    assert route == 'escalate'

    escalate_result = escalate_node(state)
    state.update(escalate_result)  # type: ignore[typeddict-item]
    advance_result = advance_node(state)
    state.update(advance_result)  # type: ignore[typeddict-item]
    final_route = _dispatch_router(state)
    from langgraph.graph import END

    print(f'final dispatch route: {final_route!r}')
    assert final_route == END

    print('Scenario 2: PASS')


# ---------------------------------------------------------------------------
# Scenario 3: 3-strike via REAL fast-check failures, diagnosis threading
# ---------------------------------------------------------------------------


async def scenario_strikes(client: RuntimeClient) -> None:
    print('=== Scenario 3: 3-strike via real fast-check failures ===')
    workspace_dir = f'{BASE_WORKSPACE}/strikes'
    ticket_id = 'STUB-STRIKES'
    note_path = f'{workspace_dir}/attempt_note.txt'

    await seed_calc_project(client, workspace_dir)

    # Every attempt makes a genuine (but non-fixing) edit -- calc.py's real
    # bug is never touched, so the real fast-check (pytest) fails
    # identically every strike, deterministically driving all 3 strikes.
    set_stub_script(
        ticket_id,
        [
            tool_turn(
                (
                    'str_replace_based_edit_tool',
                    {
                        'command': 'create',
                        'path': note_path,
                        'file_text': 'attempt in progress\n',
                    },
                )
            ),
            complete_turn('made an attempt'),
        ],
    )

    ticket = make_ticket(ticket_id, workspace_dir)
    state: AgentState = {'active_ticket': ticket}

    executor_result = await executor_node(state)
    state.update(executor_result)  # type: ignore[typeddict-item]

    print(f'executor_node ticket_status: {state.get("ticket_status")}')
    print(f'executor_node strike_count: {state.get("strike_count")}')
    for line in executor_result.get('attempt_log', []):
        print(f'  attempt_log: {line}')
    assert state.get('ticket_status') == TicketStatus.ESCALATED
    assert state.get('strike_count') == _MAX_STRIKES

    fast_check_failures = sum(
        1
        for line in executor_result.get('attempt_log', [])
        if 'fast_check' in line and 'exit_code=0' not in line
    )
    print(f'real fast-check failures recorded: {fast_check_failures}')
    assert fast_check_failures == _MAX_STRIKES, (
        f'expected all {_MAX_STRIKES} strikes to be real fast-check failures, got {fast_check_failures}'
    )

    history = get_call_history(ticket_id)
    assert len(history) == _MAX_STRIKES
    print('--- diagnosis threading across strikes ---')
    first_msg_texts = [
        attempt.calls_seen[0]['messages'][0]['content'] for attempt in history
    ]
    assert 'The previous attempt failed its checks:' not in first_msg_texts[0], (
        'strike 1 should have no prior feedback'
    )
    for i in (1, 2):
        has_feedback = 'The previous attempt failed its checks:' in first_msg_texts[i]
        print(f'strike {i + 1} opening message carries prior diagnosis: {has_feedback}')
        assert has_feedback, (
            f"strike {i + 1} should thread strike {i}'s real fast-check output forward"
        )
    print(f'strike 1 opening message (truncated): {first_msg_texts[0][:120]!r}')
    print(f'strike 2 opening message (truncated): {first_msg_texts[1][-200:]!r}')

    # escalate -> advance must distill this ticket's attempt_log into
    # decision_ledger and clear it, then dispatch must route to END --
    # exactly the Step 7 Task 0a/0b wiring, now driven by a real escalation
    # that originated in the inner tool-calling loop rather than fabricated
    # state.
    validator_result = await validator_node(state)
    state.update(validator_result)  # type: ignore[typeddict-item]
    from syndicate.nodes import _validator_router

    route = _validator_router(state)
    print(f'post-validator routing: {route!r}')
    assert route == 'escalate'

    escalate_result = escalate_node(state)
    state.update(escalate_result)  # type: ignore[typeddict-item]
    print(f'escalate_node: {escalate_result!r}')
    assert state.get('ticket_status') == TicketStatus.ESCALATED

    ledger_before = list(state.get('decision_ledger') or [])
    assert ledger_before == []

    advance_result = advance_node(state)
    state.update(advance_result)  # type: ignore[typeddict-item]
    ledger_after = state.get('decision_ledger') or []
    print(f'decision_ledger entries after advance: {len(ledger_after)}')
    assert len(ledger_after) == 1
    assert ledger_after[0].attempt_count == len(executor_result.get('attempt_log', []))
    assert state.get('attempt_log') == [], 'attempt_log must be cleared after advance'

    final_route = _dispatch_router(state)
    from langgraph.graph import END

    print(f'final dispatch route: {final_route!r}')
    assert final_route == END

    print('Scenario 3: PASS')


# ---------------------------------------------------------------------------
# Scenario 4: context-bound spot-check (compact growth, no accumulation)
# ---------------------------------------------------------------------------


async def scenario_context(client: RuntimeClient) -> None:
    print('=== Scenario 4: context-bound spot-check ===')
    workspace_dir = f'{BASE_WORKSPACE}/context'
    ticket_id = 'STUB-CONTEXT'
    calc_path = f'{workspace_dir}/calc.py'

    await seed_calc_project(client, workspace_dir)

    set_stub_script(
        ticket_id,
        [
            tool_turn(
                ('str_replace_based_edit_tool', {'command': 'view', 'path': calc_path})
            ),
            tool_turn(
                (
                    'str_replace_based_edit_tool',
                    {
                        'command': 'str_replace',
                        'path': calc_path,
                        'old_str': 'return a - b',
                        'new_str': 'return a + b',
                    },
                )
            ),
            tool_turn(('bash', {'command': 'echo checkpoint-1'})),
            tool_turn(('bash', {'command': 'python3 -m pytest -q'})),
            complete_turn('done'),
        ],
    )

    ticket = make_ticket(ticket_id, workspace_dir)
    state: AgentState = {'active_ticket': ticket}
    executor_result = await executor_node(state)
    state.update(executor_result)  # type: ignore[typeddict-item]
    print(f'executor_node ticket_status: {state.get("ticket_status")}')
    assert state.get('ticket_status') == TicketStatus.LOCAL_PASS

    history = get_call_history(ticket_id)
    assert len(history) == 1
    calls = history[0].calls_seen
    print(f'turns this attempt: {len(calls)}')
    assert len(calls) == 5

    lens = [c['messages_len'] for c in calls]
    print(f'messages_len handed to the stub at each turn: {lens}')
    expected = [1 + 2 * i for i in range(len(calls))]
    assert lens == expected, (
        f'expected linear/compact growth {expected} (1 initial msg, +2 per turn), got {lens}'
    )

    print('--- per-turn tool_result freshness (no stale accumulation) ---')
    for i in range(1, len(calls)):
        prior_turn_tool_calls = (
            1  # every scripted turn here issues exactly one tool call
        )
        tool_results = calls[i]['messages'][-1]['content']
        print(f'turn {i}: last message has {len(tool_results)} tool_result block(s)')
        assert len(tool_results) == prior_turn_tool_calls, (
            f'turn {i}: expected exactly {prior_turn_tool_calls} fresh tool_result(s) '
            f'carried forward (not accumulated), got {len(tool_results)}'
        )

    print('Scenario 4: PASS')


SCENARIOS = {
    'happy': scenario_happy,
    'cap': scenario_cap,
    'strikes': scenario_strikes,
    'context': scenario_context,
}


async def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        print(f'usage: {sys.argv[0]} <{"|".join(SCENARIOS)}>', file=sys.stderr)
        sys.exit(2)

    client = get_runtime_client()
    try:
        await SCENARIOS[sys.argv[1]](client)
    finally:
        await client.aclose()


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
