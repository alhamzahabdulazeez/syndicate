#!/usr/bin/env python3
"""LLM-free verification harness for Step 6 (real validator_node + oversight_git_node).

Talks to the live OpenHands runtime container directly (real client, real
pytest, real git) but fabricates AgentState by hand and calls the node
functions directly instead of running the executor/Anthropic loop -- so this
needs no ANTHROPIC_API_KEY.

Run with:
    SYNDICATE_RUNTIME_URL=http://localhost SYNDICATE_RUNTIME_PORT=8000 \
        python3 scripts/verify_step6_validation_git.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path so `syndicate` is importable without install.
sys.path.insert(0, str(Path(__file__).parent.parent))

# This harness exercises the real client, never the mock.
os.environ.pop('SYNDICATE_MOCK_CLIENT', None)

from syndicate.nodes import (  # noqa: E402
    _validator_router,
    oversight_git_node,
    validator_node,
)
from syndicate.runtime import RuntimeClient, get_runtime_client  # noqa: E402
from syndicate.state import AgentState  # noqa: E402

WORKSPACE_DIR = os.environ.get(
    'SYNDICATE_VERIFY_WORKSPACE', '/home/openhands/syndicate-workspace/sample_project'
)

CALC_PY = 'def add(a, b):\n    return a + b\n'
TEST_CALC_PY = (
    'from calc import add\n\n\n'
    'def test_add_positive():\n'
    '    assert add(2, 3) == 5\n\n\n'
    'def test_add_negative():\n'
    '    assert add(-1, -1) == -2\n'
)


def make_ticket(**overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        'id': 'STEP6-SAMPLE',
        'intent': 'step6 verification harness run',
        'verification_command': 'python3 -m pytest -q',
        'workspace_dir': WORKSPACE_DIR,
        'files_changed': ['calc.py'],
    }
    ticket.update(overrides)
    return ticket


async def seed_workspace(client: RuntimeClient) -> None:
    """Seed the sample project with the test file committed as pre-existing
    baseline, but calc.py left uncommitted -- so Scenario A's git node has a
    real diff (calc.py) to stage and commit, matching a ticket that adds it.
    """
    setup = await client.run_action(f'mkdir -p {WORKSPACE_DIR}/tests')
    assert setup.exit_code == 0, f'workspace mkdir failed: {setup.output}'

    await client.write_file(f'{WORKSPACE_DIR}/tests/test_calc.py', TEST_CALC_PY)
    await client.write_file(
        f'{WORKSPACE_DIR}/.gitignore', '__pycache__/\n*.pyc\n.pytest_cache/\n'
    )

    init = await client.run_action(
        'git init -q '
        '&& git config user.name syndicate-bot '
        '&& git config user.email syndicate-bot@example.invalid '
        '&& python3 -m pip install --quiet pytest '
        '&& git add -- tests/test_calc.py .gitignore && git commit -q -m seed',
        cwd=WORKSPACE_DIR,
    )
    assert init.exit_code == 0, f'workspace git/pytest setup failed: {init.output}'

    # calc.py is intentionally written *after* the seed commit, so it starts
    # life as an untracked, uncommitted file -- Scenario A commits it.
    await client.write_file(f'{WORKSPACE_DIR}/calc.py', CALC_PY)


async def scenario_a(client: RuntimeClient) -> None:
    print('=== Scenario A: pass path ===')
    state: AgentState = {
        'active_ticket': make_ticket(),
        'attempt_log': ["strike=1 fast_check exit_code=0 output='fast checks passed'"],
    }

    validator_result = await validator_node(state)
    state.update(validator_result)  # type: ignore[typeddict-item]
    assert state.get('last_validation_passed') is True, (
        f'expected validation to pass, got {validator_result}'
    )

    route = _validator_router(state)
    assert route == 'oversight_git', (
        f'expected pass routing to oversight_git, got {route!r}'
    )

    git_result = await oversight_git_node(state)
    state.update(git_result)  # type: ignore[typeddict-item]

    log = await client.run_action('git log --oneline', cwd=WORKSPACE_DIR)
    print(
        f'raw pytest tail (from validator_diagnosis, empty on pass): '
        f'{state.get("validation_diagnosis")!r}'
    )
    print(f'git log --oneline:\n{log.output}')
    assert 'STEP6-SAMPLE' in log.output, 'expected templated commit in git log'

    status = await client.run_action('git status --porcelain', cwd=WORKSPACE_DIR)
    print(f'git status --porcelain: {status.output!r}')
    assert status.output.strip() == '', 'expected clean working tree after commit'

    print('Scenario A: PASS')


async def scenario_b(client: RuntimeClient) -> None:
    print('=== Scenario B: fail path ===')
    # Break the implementation so the seeded tests fail.
    await client.edit_file(f'{WORKSPACE_DIR}/calc.py', 'return a + b', 'return a - b')

    before = await client.run_action('git log --oneline', cwd=WORKSPACE_DIR)
    before_count = len(before.output.strip().splitlines())

    state: AgentState = {'active_ticket': make_ticket(), 'strike_count': 0}

    validator_result = await validator_node(state)
    state.update(validator_result)  # type: ignore[typeddict-item]
    print(
        f'validator_node result: last_validation_passed='
        f'{validator_result.get("last_validation_passed")}, '
        f'strike_count={validator_result.get("strike_count")}'
    )
    assert state.get('last_validation_passed') is False, 'expected validation to fail'

    diagnosis = state.get('validation_diagnosis') or ''
    print(f'truncated diagnosis:\n{diagnosis}')
    assert diagnosis, 'expected a non-empty truncated diagnosis'

    route = _validator_router(state)
    print(f'routing decision: {route!r}')
    assert route == 'executor', (
        f'expected fail-under-cap routing to executor, got {route!r}'
    )

    after = await client.run_action('git log --oneline', cwd=WORKSPACE_DIR)
    after_count = len(after.output.strip().splitlines())
    print(f'git log count before={before_count} after={after_count}')
    assert after_count == before_count, 'expected no commit on the fail path'

    # Restore the fix so re-runs of this harness start from a clean state.
    await client.edit_file(f'{WORKSPACE_DIR}/calc.py', 'return a - b', 'return a + b')

    print('Scenario B: PASS')


def scenario_c() -> None:
    print('=== Scenario C: guardrails ===')
    forbidden_patterns = [
        r'git add \.',
        r'add -A',
        r'--force',
        r'push',
        r'config --global',
    ]
    syndicate_dir = Path(__file__).parent.parent / 'syndicate'
    files = sorted(syndicate_dir.glob('*.py'))
    violations: list[str] = []
    for path in files:
        text = path.read_text()
        for pattern in forbidden_patterns:
            for match in re.finditer(pattern, text):
                line_no = text.count('\n', 0, match.start()) + 1
                violations.append(f'{path}:{line_no}: matched {pattern!r}')

    for v in violations:
        print(v)
    print(f'grepped {len(files)} files under syndicate/ for {forbidden_patterns}')
    assert not violations, f'forbidden git patterns found: {violations}'

    print('Scenario C: PASS')


async def main() -> None:
    client = get_runtime_client()
    try:
        await seed_workspace(client)
        await scenario_a(client)
        await scenario_b(client)
        scenario_c()
        print('All Step 6 verification scenarios passed.')
    finally:
        await client.aclose()


if __name__ == '__main__':
    asyncio.run(main())
    sys.exit(0)
