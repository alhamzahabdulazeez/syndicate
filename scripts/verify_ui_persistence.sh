#!/usr/bin/env bash
# Task 5.4: complete a run, SIGTERM uvicorn, restart (fresh process, same
# db file), GET /runs lists it and /state returns its final state.
# Container-independent (SYNDICATE_MOCK_CLIENT=1).
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
rm -rf data && mkdir -p data
export SYNDICATE_MOCK_CLIENT=1

start_server() {
    python3 -m server.app > /tmp/syndicate_ui_persistence_server.log 2>&1 &
    echo $!
}

wait_healthy() {
    for i in $(seq 1 30); do
        curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

echo "=== process A: start, complete a run ==="
PID_A=$(start_server)
echo "server A pid=$PID_A"
if ! wait_healthy; then
    echo "FAIL: server A did not become healthy"
    cat /tmp/syndicate_ui_persistence_server.log
    exit 1
fi

RESP=$(curl -sf -m 5 -X POST http://127.0.0.1:8080/runs -H "Content-Type: application/json" \
    -d '{"raw_request": "Task 5.4 persistence across restart"}')
RUN_ID=$(python3 -c "import sys, json; print(json.load(sys.stdin)['run_id'])" <<< "$RESP")
echo "run_id=$RUN_ID"

timeout 15 curl -N -sf "http://127.0.0.1:8080/runs/$RUN_ID/stream" > /tmp/syndicate_ui_persistence_stream.log
echo "run completed (stream consumed to terminal event)"

echo "=== SIGTERM process A ==="
kill -TERM "$PID_A"
wait "$PID_A" 2>/dev/null
echo "process A exit status: $?"
sleep 1
if curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1; then
    echo "FAIL: server A still responding after SIGTERM"
    exit 1
fi
echo "server A confirmed down"

echo "=== process B: fresh restart, same db file ==="
PID_B=$(start_server)
echo "server B pid=$PID_B"
if ! wait_healthy; then
    echo "FAIL: server B did not become healthy"
    cat /tmp/syndicate_ui_persistence_server.log
    exit 1
fi

echo "--- GET /runs (fresh process, in-memory registry empty -- must come from checkpointer) ---"
RUNS_RESP=$(curl -sf -m 5 http://127.0.0.1:8080/runs)
echo "$RUNS_RESP"

echo "--- GET /runs/{id}/state (fresh process) ---"
STATE_RESP=$(curl -sf -m 5 "http://127.0.0.1:8080/runs/$RUN_ID/state")
echo "$STATE_RESP"

kill -TERM "$PID_B" 2>/dev/null
wait "$PID_B" 2>/dev/null

python3 - "$RUN_ID" "$RUNS_RESP" "$STATE_RESP" <<'PYEOF'
import json
import sys

run_id, runs_json, state_json = sys.argv[1], sys.argv[2], sys.argv[3]
runs = json.loads(runs_json)
state = json.loads(state_json)

matching = [r for r in runs if r["run_id"] == run_id]
assert matching, f"run_id {run_id!r} not found in GET /runs after restart: {runs!r}"
print(f"GET /runs (post-restart) lists run: {matching[0]}")
assert matching[0]["status"] in ("completed", "escalated"), f"unexpected status: {matching[0]}"

assert state.get("ticket_status") == "local_pass", f"unexpected final ticket_status: {state.get('ticket_status')!r}"
assert state.get("attempt_log") == [], "expected attempt_log cleared in final state"
assert len(state.get("decision_ledger") or []) == 1, "expected one decision_ledger entry"
print("GET /state (post-restart) returns correct final state: "
      f"ticket_status={state['ticket_status']!r}, "
      f"decision_ledger entries={len(state['decision_ledger'])}")

print("PASS: Task 5.4 persistence across restart")
PYEOF
PY_EXIT=$?

rm -rf data /tmp/syndicate_ui_persistence_stream.log

if [ "$PY_EXIT" -ne 0 ]; then
    echo "FAIL: persistence assertions failed"
    exit 1
fi
echo "RESULT: Task 5.4 -- PASS"
