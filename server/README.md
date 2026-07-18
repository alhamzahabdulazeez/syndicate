# Syndicate UI + service layer

A minimal FastAPI service wrapping the frozen `syndicate/` LangGraph chassis:
submit a `raw_request`, persist runs in SQLite, stream execution as SSE into
a live timeline, inspect state in a workspace pane.

## This is a lower-level entry point, not the issue-resolution pipeline

`POST /runs` takes a free-text `raw_request` and seeds `active_ticket`
directly (`server/runner.py::build_initial_state`). It bypasses
`analyzer_node`/`architect_node` entirely -- no issue triage, no
repo-grounded `verification_command` derivation, just `raw_request` handed
straight to the executor as the ticket's task.

For running Syndicate against a real GitHub issue -- the
analyzer→architect→executor→validator→oversight_git pipeline that derives
its own ticket from the issue and repo -- use `scripts/run_real_issue.py`
instead, documented in [`docs/RUNNING_FOR_REAL.md`](../docs/RUNNING_FOR_REAL.md).
That is a separate entry path; this server does not go through it.

## Running

```bash
python3 -m server.app
```

Binds `127.0.0.1:8080` only (single uvicorn worker). Requires the runtime
container only if you are *not* using `SYNDICATE_MOCK_CLIENT=1`/
`SYNDICATE_STUB_LLM=1` -- see `syndicate/` and the Step 5-7.5 artifacts for
what those toggles do.

## Security -- loopback-only, no auth

This API triggers arbitrary sandbox bash via the frozen chassis's executor
node. A public bind would be RCE-with-a-form-field. There is no
authentication in v1 *because* the service only ever binds to
`127.0.0.1:8080` -- if that ever changes (a non-loopback bind), auth must be
added first; this is on the deferred ledger, not a "todo later" left
unstated.

**Remote viewing:** don't open the port. Use an SSH tunnel from your own
machine:

```bash
ssh -L 8080:127.0.0.1:8080 user@vps
```

Then browse to `http://127.0.0.1:8080/` locally, same as if the box were
your own machine.

## Known v1 limitations

- **Backlog is in-memory only.** Each run's SSE backlog (used for
  `?after=N` replay/reconnect) lives in the server process's memory,
  bounded to the most recent 2000 events per run. If the server restarts,
  the backlog for any run from before that restart is gone -- timeline
  replay for those runs is out of scope for v1. The run's actual *state*
  is not lost, though: it's checkpointed to SQLite (`data/checkpoints.db`)
  and remains available via `GET /runs` and `GET /runs/{id}/state` across
  restarts.
- **Concurrency cap = 1.** Only one run can be active at a time, enforced
  server-side (`409` on a second `POST /runs` while one is active) --
  matches the single execution sandbox this wraps.
- **Single uvicorn worker only.** A second worker would duplicate the
  compiled graph and its checkpointer connection, and would collide with
  the single execution sandbox container. Don't run this with
  `--workers 2+` or behind a process manager that spawns multiple copies.
