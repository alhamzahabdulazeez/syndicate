Syndicate
A LangGraph orchestration layer for autonomous coding agents. It does not
execute code itself: every sandbox action is delegated over REST to an
external OpenHands agent-server
container.
The problem
Autonomous coding agents fail in three specific ways:
Unbounded token growth. A long-running agent accumulates its full
history in context until it hits the model's limit or the run costs
more than the ticket is worth.
Infinite error loops. An agent hits a failing command, retries the
same broken approach, and never stops.
Silent hallucination. An agent reports success on a ticket it never
completed, and nothing downstream catches it.
How Syndicate addresses each
Bounded self-healing retry. Each ticket gets up to three attempts
before the graph routes it to escalation. _MAX_STRIKES is defined in
syndicate/nodes.py and enforced by the
while strike < _MAX_STRIKES loop at
syndicate/nodes.py.
Run-wide escalation budget. A ticket that exhausts its three strikes
counts against a run-wide budget too (SYNDICATE_MAX_ESCALATIONS, default
3). Once a run exceeds it, the graph halts rather than continuing a run
whose tickets keep failing across the board. See
escalate_node.
Compressed decision ledger. Each completed ticket is distilled into a
DecisionSummary and appended to decision_ledger
(syndicate/state.py,
syndicate/nodes.py) rather than kept as a
full transcript. This is the mechanism that bounds state size across a
long run.
LangGraph routing. The graph (analyzer, architect, dispatch, executor,
validator, oversight_git/escalate, advance) is a StateGraph with
conditional edges that decide retry vs. escalate vs. advance, built in
build_graph(). State transitions are explicit
and inspectable, defined as graph edges rather than as branches inside a
Python loop.
Runtime abstraction. Sandbox execution goes through the
RuntimeClient protocol defined in
syndicate/runtime/base.py. The only real
adapter today is OpenHandsRuntimeClient
(syndicate/runtime/openhands.py),
which talks to an external agent-server container over HTTP. A mock
backend (syndicate/runtime/mock.py)
satisfies the same protocol with no network calls, for local development
and the verification scripts. Adding a backend means adding a module that
satisfies the protocol; syndicate/nodes.py does not change.
Architecture
Syndicate is the brain: the graph, the state, the retry and escalation
logic, the routing decisions. It has no code-execution capability of its
own.
The OpenHands agent-server container is the muscle. It runs bash
commands and edits files inside a sandbox, reachable only over its
published REST API.
This split is a deliberate design decision. Running two competing
reasoning loops against the same sandbox, Syndicate's graph and OpenHands'
own agent loop, is the failure mode this architecture exists to avoid.
Syndicate targets the sandbox's execution API only, never its
conversation or agent layer.
Requirements
Python. See requires-python in pyproject.toml.
Docker, for the sandbox container. Mock mode runs without it.
Install
Bash
Quickstart
Mock mode
No Docker, no network:
Bash
Against a real sandbox
The container runs as uid 10001, not root. run_runtime_container.sh
bind-mounts a host workspace directory ($SYNDICATE_WORKSPACE_DIR,
default $HOME/syndicate-workspace) into it. A root-owned host directory
there makes the container crash-loop on startup with
PermissionError: [Errno 13] Permission denied: 'workspace/conversations'.
Fix the ownership before starting the container:
Bash
Then start the container and the server:
Bash
The server binds 127.0.0.1:8080 only. See
server/README.md for the API surface.
Verification
Check the claims above against a clone:
Bash
Status
Syndicate has no unit test suite. Correctness is verified through
integration scripts under scripts/. A pytest suite is planned.
Syndicate is ~1,473 lines across syndicate/ and server/. It is an
orchestration layer, and it makes no claim to being a full agent
framework.
There are no published SWE-bench Verified numbers yet. The benchmark is a
goal here, not a claim.
Remaining roadmap: a real pytest suite, dual-sandbox egress enforcement,
and a real end-to-end issue-resolution run measured against a benchmark.
License
MIT. See LICENSE. Syndicate delegates execution to the OpenHands
agent-server container; that dependency is documented in
THIRD_PARTY_NOTICES.md.