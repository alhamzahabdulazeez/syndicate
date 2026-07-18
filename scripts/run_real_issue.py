#!/usr/bin/env python3
"""One real end-to-end run: a real GitHub issue through analyzer -> architect
-> executor -> validator -> oversight_git, against a live OpenHands runtime
and a real Anthropic key. This is the entry point docs/RUNNING_FOR_REAL.md
points at -- until this script runs, "the real path is complete" is a claim
about the code, not a result.

Fetching the issue and cloning the repo are both out of scope here (see the
Analyzer/Architect docstring in syndicate/nodes.py): --workspace-dir must
already be a git checkout of the target repo inside the sandbox container,
with repo-local git identity available (oversight_git_node sets it, but the
checkout itself is the operator's job).

Run with:
    ANTHROPIC_API_KEY=... SYNDICATE_RUNTIME_URL=http://localhost \
        SYNDICATE_RUNTIME_PORT=8000 python3 scripts/run_real_issue.py \
        --repo owner/name --number 123 --title "..." \
        --body-file issue_body.txt \
        --workspace-dir /home/openhands/syndicate-workspace/owner-name
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import cast

# Ensure repo root is on sys.path so `syndicate` is importable without install.
sys.path.insert(0, str(Path(__file__).parent.parent))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='"owner/name"')
    parser.add_argument('--number', required=True, type=int, help='issue number')
    parser.add_argument('--title', required=True, help='issue title')
    body_group = parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument('--body', help='issue body text')
    body_group.add_argument('--body-file', help='path to a file containing the issue body')
    parser.add_argument(
        '--workspace-dir',
        required=True,
        help=(
            'path inside the sandbox container to an already-cloned checkout '
            'of --repo; this script does not clone anything'
        ),
    )
    return parser.parse_args()


def main() -> None:
    if not os.environ.get('ANTHROPIC_API_KEY'):
        print('run_real_issue blocked: ANTHROPIC_API_KEY not set')
        sys.exit(2)
    if not os.environ.get('SYNDICATE_RUNTIME_URL'):
        print(
            'run_real_issue blocked: SYNDICATE_RUNTIME_URL not set -- this '
            'script needs a live OpenHands runtime, not mock mode. See '
            'docs/RUNNING_FOR_REAL.md.'
        )
        sys.exit(2)
    # This script exercises the real path end to end; never the mock runtime.
    os.environ.pop('SYNDICATE_MOCK_CLIENT', None)

    args = _parse_args()
    body = args.body if args.body is not None else Path(args.body_file).read_text()

    from syndicate.nodes import build_graph
    from syndicate.state import AgentState, GithubIssue, TicketStatus

    issue: GithubIssue = {
        'repo': args.repo,
        'number': args.number,
        'title': args.title,
        'body': body,
        'workspace_dir': args.workspace_dir,
    }

    async def _run() -> AgentState:
        graph = build_graph()
        initial_state: AgentState = {'issue': issue}
        return cast(AgentState, await graph.ainvoke(initial_state))

    final_state = asyncio.run(_run())

    print(f'issue: {args.repo}#{args.number}')
    print(f'issue_analysis: {final_state.get("issue_analysis")!r}')
    print(f'active_ticket: {final_state.get("active_ticket")!r}')
    print(f'ticket_status: {final_state.get("ticket_status")}')
    for line in final_state.get('attempt_log') or []:
        print(f'  attempt_log: {line}')
    for decision in final_state.get('decision_ledger') or []:
        print(f'  decision: {decision.summary}')

    sys.exit(0 if final_state.get('ticket_status') == TicketStatus.DONE else 1)


if __name__ == '__main__':
    main()
