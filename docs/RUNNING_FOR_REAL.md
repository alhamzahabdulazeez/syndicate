# Running Syndicate for real

This is the path to one real result: a genuine GitHub issue, resolved by
Syndicate's graph in a live sandbox, with a real test suite confirming the
fix. No run following this document has happened yet. Nothing below is a
benchmark or a claim of success; it's the procedure, written so the first
real run is reproducible rather than ad hoc.

## Hardware

The development VPS this repo has been verified against has 1 CPU core and
under 100Mi of free RAM at idle. That machine cannot run this. A real issue
run needs the target repo's own build and test tooling to execute inside the
sandbox container, on top of the container's own footprint and the LLM
tool-calling loop driving it, so plan for:

- Multiple CPU cores. A single core serializes the container runtime, the
  target repo's dependency install, and its test run; slow tickets will time
  out against `SYNDICATE_RUNTIME_TIMEOUT_SECONDS` (default 300s) for no
  reason other than CPU starvation.
- Several GB of free RAM, headroom above whatever Docker and the target
  repo's toolchain need on their own (a JS or JVM build can use more than a
  small Python one).
- Enough disk for the `agent-server` image, the cloned target repo, and its
  installed dependencies (10-20GB is a reasonable floor, more for anything
  with a large dependency tree).

These are sizing judgments from reading the pipeline, not a tested minimum —
no run has confirmed them yet.

## What you need

- `ANTHROPIC_API_KEY` for a real Anthropic account. The executor calls
  `claude-sonnet-4-6`; analyzer and architect call `claude-5-sonnet-latest`
  (see `_EXECUTOR_MODEL` / `_ANALYZER_MODEL` / `_ARCHITECT_MODEL` in
  `syndicate/nodes.py`).
- Docker, to run the `agent-server` sandbox
  (`scripts/run_runtime_container.sh`; see the main
  [README](../README.md#quickstart) for the workspace-ownership fix it
  needs first).
- A checkout of the target repo already cloned inside the container's
  workspace. Syndicate does not fetch the issue or clone the repo itself —
  `analyzer_node`'s input contract (`GithubIssue` in `syndicate/state.py`)
  assumes `workspace_dir` already points at a real git checkout. Clone it
  yourself, e.g. `docker exec <container> git clone <repo-url> <workspace_dir>`.
- One real GitHub issue: the repo slug, issue number, title, and body.

## The command

```bash
scripts/run_runtime_container.sh
# clone the target repo into the container's workspace, then:
ANTHROPIC_API_KEY=... \
SYNDICATE_RUNTIME_URL=http://localhost \
SYNDICATE_RUNTIME_PORT=8000 \
python3 scripts/run_real_issue.py \
    --repo owner/name \
    --number 123 \
    --title "The issue title" \
    --body-file issue_body.txt \
    --workspace-dir /home/openhands/syndicate-workspace/owner-name
```

`scripts/run_real_issue.py` fails fast with a clear message if either
`ANTHROPIC_API_KEY` or `SYNDICATE_RUNTIME_URL` is missing, rather than
attempting and slowly failing real LLM calls. It refuses mock mode
unconditionally, so this always exercises the real path.

## What the result means

The script prints `issue_analysis`, the `active_ticket` architect built
(including the `verification_command` it derived from the checkout — see
`_detect_verification_command` in `syndicate/nodes.py`), the attempt log,
and the terminal `ticket_status`:

- `done` — `oversight_git_node` confirmed a real commit landed (`HEAD`
  moved), on the word of `git` itself, not the runtime client's exit code.
  This is the one outcome that means the issue was actually resolved.
- `escalated` — the 3-strike retry budget
  (`syndicate/nodes.py::_MAX_STRIKES`) was exhausted without a verified
  commit. Read the printed attempt log and `validation_diagnosis` for why.

Exit code is 0 for `done`, 1 for anything else.

## Cost

No run has happened yet, so there is no measured dollar figure here, and
one shouldn't be guessed. What's known from the code: the executor allows
up to 3 strikes (`_MAX_STRIKES`), each strike up to 25 tool-calling turns
(`_MAX_TOOL_CALLS`), each turn capped at `max_tokens=8096`. Analyzer and
architect add one bounded call each. That bounds the number of API calls a
single ticket can make; it does not bound their cost, which depends on
context size per call and current Anthropic pricing. Check current pricing
before running this against a real account, and expect the worst case
(3 strikes, 25 turns each) to cost meaningfully more than the best case
(one strike, a handful of turns).
