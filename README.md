# Syndicate

A LangGraph orchestration layer for autonomous coding agents. It does not
execute code itself: every sandbox action is delegated over REST to an
external OpenHands `agent-server` container.

## The problem

Autonomous coding agents fail in three specific ways:

1. **Unbounded token growth.** A long-running agent accumulates its full
   history in context until it hits the model's limit or the run costs
   more than the ticket is worth.
2. **Infinite error loops.** An agent hits a failing command, retries the
   same broken approach, and never stops.
3. **Silent hallucination.** An agent reports success on a ticket it never
   completed, and nothing downstream catches it.

## How Syndicate addresses each

**Bounded self-healing retry.** Each ticket gets up to three attempts
before the graph routes it to escalation. `_MAX_STRIKES` is defined in
[`syndicate/nodes.py`](syndicate/nodes.py#L32) and enforced by the
`while strike < _MAX_STRIKES` loop at
[`syndicate/nodes.py`](syndicate/nodes.py#L240).

**Run-wide escalation budget.** A ticket that exhausts its three strikes
counts against a run-wide budget too (`SYNDICATE_MAX_ESCALATIONS`, default
3). Once a run exceeds it, the graph halts rather than continuing a run
whose tickets keep failing across the board. See
[`escalate_node`](syndicate/nodes.py#L325).

**Commit-gated completion.** A ticket is only marked `DONE` on the word of
`git` itself, not on a runtime client's exit code. `HEAD` is captured before
staging and again after commit; `DONE` requires it to have actually moved.
A failed `git add`, a failed `git commit`, a commit that exits 0 but leaves
`HEAD` unchanged (a no-op commit, a rejected hook, a runtime client
reporting success without acting) all count as one strike and route back
through the same retry/escalate budget above, via
[`_oversight_git_router`](syndicate/nodes.py#L420), rather than being taken
on faith. See
[`oversight_git_node`](syndicate/nodes.py#L347). This is what answers
failure mode 3: a ticket can no longer report success without a verified
commit behind it.

**Compressed decision ledger.** Each completed ticket is distilled into a
`DecisionSummary` and appended to `decision_ledger`
([`syndicate/state.py`](syndicate/state.py#L24),
[`syndicate/nodes.py`](syndicate/nodes.py#L453)) rather than kept as a
full transcript. This is the mechanism that bounds state size across a
long run.

**LangGraph routing.** The graph (analyzer, architect, dispatch, executor,
validator, oversight_git/escalate, advance) is a `StateGraph` with
conditional edges that decide retry vs. escalate vs. advance, built in
[`build_graph()`](syndicate/nodes.py#L463). State transitions are explicit
and inspectable, defined as graph edges rather than as branches inside a
Python loop.

**Runtime abstraction.** Sandbox execution goes through the
`RuntimeClient` protocol defined in
[`syndicate/runtime/base.py`](syndicate/runtime/base.py). The only real
adapter today is `OpenHandsRuntimeClient`
([`syndicate/runtime/openhands.py`](syndicate/runtime/openhands.py)),
which talks to an external agent-server container over HTTP. A mock
backend ([`syndicate/runtime/mock.py`](syndicate/runtime/mock.py))
satisfies the same protocol with no network calls, for local development
and the verification scripts. Adding a backend means adding a module that
satisfies the protocol; `syndicate/nodes.py` does not change.

## Architecture

Syndicate is the brain: the graph, the state, the retry and escalation
logic, the routing decisions. It has no code-execution capability of its
own.

The OpenHands `agent-server` container is the muscle. It runs bash
commands and edits files inside a sandbox, reachable only over its
published REST API.

This split is a deliberate design decision. Running two competing
reasoning loops against the same sandbox, Syndicate's graph and OpenHands'
own agent loop, is the failure mode this architecture exists to avoid.
Syndicate targets the sandbox's execution API only, never its
conversation or agent layer.

## Requirements

- Python. See `requires-python` in [`pyproject.toml`](pyproject.toml).
- Docker, for the sandbox container. Mock mode runs without it.

## Install

```bash
git clone https://github.com/alhamzahabdulazeez/syndicate.git
cd syndicate
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The `[dev]` extra pulls in pytest, mypy, and ruff — needed to run the
Verification checks below, including the test suite.

## Quickstart

### Mock mode

No Docker, no network:

```bash
SYNDICATE_MOCK_CLIENT=1 python3 scripts/smoke_e2e_mock.py
SYNDICATE_MOCK_CLIENT=1 python3 -m server.app
```

### Against a real sandbox

The container runs as uid 10001, not root. `run_runtime_container.sh`
bind-mounts a host workspace directory (`$SYNDICATE_WORKSPACE_DIR`,
default `$HOME/syndicate-workspace`) into it. A root-owned host directory
there makes the container crash-loop on startup with
`PermissionError: [Errno 13] Permission denied: 'workspace/conversations'`.
Fix the ownership before starting the container:

```bash
mkdir -p "${SYNDICATE_WORKSPACE_DIR:-$HOME/syndicate-workspace}"
chown -R 10001:10001 "${SYNDICATE_WORKSPACE_DIR:-$HOME/syndicate-workspace}"
```

Then start the container and the server:

```bash
scripts/run_runtime_container.sh
SYNDICATE_RUNTIME_URL=http://localhost SYNDICATE_RUNTIME_PORT=8000 python3 -m server.app
```

The server binds `127.0.0.1:8080` only. See
[`server/README.md`](server/README.md) for the API surface.

### Sandbox network isolation

The container starts with unrestricted outbound access. `scripts/lock_egress.sh`
installs DOCKER-USER rules that drop outbound traffic from the sandbox subnet,
while leaving intra-subnet traffic, established connections, the published port,
and the host's own network alone. `scripts/unlock_egress.sh` reverses it, and
both are idempotent.

`scripts/verify_egress_lock.sh` checks four invariants against a live container:
outbound from the sandbox fails, host ingress on the published port still
works, established return traffic still flows, and the host's own egress is
untouched.

An operator runs these. The graph does not enforce them. Two constraints are
recorded in `verify_egress_lock.sh`: there is no allowlist, so a ticket under
the lock cannot `pip install`, `git clone`, or `npm install` from inside the
sandbox; and the intra-subnet path is permitted by rule but has no behavioral
test, since exercising it needs a second container on the network.

## Verification

Check the claims above against a clone:

```bash
ruff check syndicate/ server/ scripts/ tests/
mypy syndicate/ server/
pytest
python3 scripts/smoke_e2e_mock.py
python3 scripts/verify_runtime_echo.py      # requires a live container
python3 scripts/verify_runtime_fileops.py   # requires a live container
```

## Status

Syndicate has a 29-test pytest suite (`tests/`) covering decision-ledger
compression, the run-wide escalation budget, retry bounds, the
oversight_git commit-verification fix and its router, and `RuntimeClient`
protocol conformance across the mock and OpenHands adapters. It runs
entirely against the mock runtime. The integration scripts under
`scripts/` are the layer that exercises a live container end to end —
the unit suite doesn't replace that.

Syndicate is ~1,601 lines across `syndicate/` and `server/`. It is an
orchestration layer, and it makes no claim to being a full agent
framework.

There are no published SWE-bench Verified numbers yet. The benchmark is a
goal here, not a claim.

Remaining roadmap: a real end-to-end issue-resolution run measured against
a benchmark.

## License

MIT. See [LICENSE](LICENSE). Syndicate delegates execution to the OpenHands
`agent-server` container; that dependency is documented in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
