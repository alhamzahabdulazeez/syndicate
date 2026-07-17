# CLAUDE.md

Guidance for working in this repository.

## What this project is

Syndicate is a LangGraph orchestration layer for autonomous coding agents.
It holds the graph, the state, and the retry/escalation logic. It has no
code-execution capability of its own: every sandbox action is delegated
over REST to an external OpenHands `agent-server` Docker container through
the `RuntimeClient` interface in `syndicate/runtime/`. See
[README.md](README.md) for the architecture and rationale, and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for how the OpenHands
dependency is scoped.

## The decoupling constraint

OpenHands `agent-server` is targeted as an execution sandbox only, through
its bash/file REST endpoints. Never target its conversation or agent
layer. Running Syndicate's LangGraph loop and OpenHands' own agent loop
against the same sandbox creates two competing reasoning loops driving one
workspace, which is the exact failure mode this architecture exists to
avoid. If a change starts routing decisions through OpenHands' agent API
rather than its raw execution API, stop and reconsider the design before
proceeding.

## Directory map

- `syndicate/` — the LangGraph chassis. `nodes.py` defines the graph
  (analyzer → architect → dispatch → executor → validator → oversight_git/
  escalate → advance) and its retry/escalation logic. `state.py` defines
  the typed `AgentState`. `stub_llm.py` is a scripted stand-in for the
  executor's Anthropic client (`SYNDICATE_STUB_LLM=1`), used by the
  verification scripts.
- `syndicate/runtime/` — the execution-backend abstraction. `base.py`
  defines the `RuntimeClient` protocol every backend must satisfy.
  `openhands.py` is the real adapter, talking to an external
  `ghcr.io/openhands/agent-server` container over HTTP. `mock.py` is a
  no-network stand-in used in mock mode. `factory.py` picks between them
  based on env vars. To add a backend, add a module here that satisfies
  `RuntimeClient` and wire it into `factory.py`; `syndicate/nodes.py`
  should never need to change.
- `server/` — a FastAPI service (`app.py`) wrapping the compiled graph:
  submit a run, stream it as SSE, inspect state, persist checkpoints to
  SQLite (`checkpointer.py`). Binds `127.0.0.1:8080` only; see
  `server/README.md` for why and for the full API surface.
- `scripts/` — verification scripts (`verify_*.py`, `smoke_e2e_*.py`) and
  the shell entry points for the sandbox container:
  `run_runtime_container.sh` (start it) and `lock_egress.sh` /
  `unlock_egress.sh` (network isolation).

There is no `tests/` directory and no pytest suite. Correctness is
verified by the scripts under `scripts/`.

## Mock mode vs. live container

Mock mode (no Docker, no network) is the fast loop for chassis/server
changes:

```bash
SYNDICATE_MOCK_CLIENT=1 python3 scripts/smoke_e2e_mock.py
SYNDICATE_MOCK_CLIENT=1 python3 -m server.app
```

Against the real sandbox:

```bash
scripts/run_runtime_container.sh
SYNDICATE_RUNTIME_URL=http://localhost SYNDICATE_RUNTIME_PORT=8000 python3 -m server.app
```

`SYNDICATE_MOCK_CLIENT=1` selects the mock `RuntimeClient`
(`syndicate/runtime/mock.py`) in `factory.py`. `SYNDICATE_STUB_LLM=1`
separately selects the scripted Anthropic stand-in
(`syndicate/stub_llm.py`) in `nodes.py`. They're independent switches: a
verification script can stub the LLM while still talking to a live
container, or run fully offline with both stubbed.

## Lint and verification commands

```bash
ruff check syndicate server scripts
mypy syndicate server scripts --ignore-missing-imports
SYNDICATE_MOCK_CLIENT=1 python3 scripts/smoke_e2e_mock.py
```

For changes touching `syndicate/runtime/` or `server/checkpointer.py`,
also run the mock-mode UI scripts
(`scripts/verify_ui_{mock_e2e,bind,concurrency,reconnect,persistence}.{sh,py}`)
and `scripts/verify_checkpoint_roundtrip.py`, plus, if Docker is
available, `scripts/verify_runtime_{echo,fileops}.py` against a live
container.

## Env vars

All configuration is `SYNDICATE_*`. There should never be a reason to
read an OpenHands-prefixed variable in this codebase; if you find one,
it's a bug.

| Variable | Used by | Purpose |
|---|---|---|
| `SYNDICATE_MOCK_CLIENT` | `syndicate/runtime/factory.py` | `1` selects the mock `RuntimeClient` instead of the OpenHands adapter. |
| `SYNDICATE_STUB_LLM` | `syndicate/nodes.py` | `1` selects the scripted stand-in for the executor's Anthropic client. |
| `SYNDICATE_RUNTIME_URL` | `syndicate/runtime/factory.py` | Base URL of the live OpenHands agent-server. |
| `SYNDICATE_RUNTIME_PORT` | `syndicate/runtime/factory.py` | Port of the live OpenHands agent-server. |
| `SYNDICATE_RUNTIME_SESSION_API_KEY` | `syndicate/runtime/factory.py` | Optional session API key sent to the agent-server. |
| `SYNDICATE_RUNTIME_TIMEOUT_SECONDS` | `syndicate/runtime/factory.py` | Per-action timeout against the agent-server (default 300). |
| `SYNDICATE_MAX_ESCALATIONS` | `syndicate/nodes.py` | Run-wide escalation budget before the graph halts (default 3). |
| `SYNDICATE_UI_DB_PATH` | `server/app.py` | SQLite checkpoint DB path (default `data/checkpoints.db`). |
| `SYNDICATE_CONTAINER_NAME` | `scripts/run_runtime_container.sh`, `scripts/verify_egress_lock.sh` | Name of the sandbox Docker container. |
| `SYNDICATE_NETWORK_NAME` | `scripts/run_runtime_container.sh`, `scripts/lock_egress.sh`, `scripts/unlock_egress.sh` | Name of the sandbox's isolated Docker network. |
| `SYNDICATE_RUNTIME_IMAGE` | `scripts/run_runtime_container.sh` | Sandbox container image (default `ghcr.io/openhands/agent-server:1.12.0-python`). |
| `SYNDICATE_WORKSPACE_DIR` | `scripts/run_runtime_container.sh` | Host directory mounted into the sandbox as the workspace. |
| `SYNDICATE_READY_TIMEOUT_SECONDS` | `scripts/run_runtime_container.sh` | How long to wait for the container to report ready (default 60). |

## Validation before committing a change here

```bash
ruff check syndicate server scripts
mypy syndicate server scripts --ignore-missing-imports
SYNDICATE_MOCK_CLIENT=1 python3 scripts/smoke_e2e_mock.py
```
