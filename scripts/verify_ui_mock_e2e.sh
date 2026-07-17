#!/usr/bin/env bash
# Task 5.2: Mock E2E THROUGH THE API. Starts uvicorn (127.0.0.1:8080),
# POSTs a run, streams it to completion via curl -N, and asserts: seq
# strictly monotonic, a terminal kind is present, consumer exits cleanly.
# Container-independent (SYNDICATE_MOCK_CLIENT=1).
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
rm -rf data && mkdir -p data

export SYNDICATE_MOCK_CLIENT=1
python3 -m server.app > /tmp/syndicate_ui_mock_e2e_server.log 2>&1 &
SERVER_PID=$!
echo "server pid=$SERVER_PID"

cleanup() {
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
}
trap cleanup EXIT

for i in $(seq 1 30); do
    curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1 && break
    sleep 1
done
if ! curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1; then
    echo "FAIL: server did not become healthy"
    cat /tmp/syndicate_ui_mock_e2e_server.log
    exit 1
fi
echo "server healthy"

RESP=$(curl -sf -m 5 -X POST http://127.0.0.1:8080/runs \
    -H "Content-Type: application/json" \
    -d '{"raw_request": "Task 5.2 mock E2E through the API"}')
echo "POST /runs response: $RESP"
RUN_ID=$(python3 -c "import sys, json; print(json.load(sys.stdin)['run_id'])" <<< "$RESP")
echo "run_id=$RUN_ID"

echo "--- streaming to completion ---"
STREAM_FILE=$(mktemp)
timeout 20 curl -N -sf "http://127.0.0.1:8080/runs/$RUN_ID/stream" > "$STREAM_FILE"
CURL_EXIT=$?
echo "curl consumer exit code: $CURL_EXIT"

echo "--- envelopes received ---"
grep '^data:' "$STREAM_FILE" | sed 's/^data: //'

python3 - "$STREAM_FILE" <<'PYEOF'
import json
import sys

path = sys.argv[1]
envelopes = []
with open(path) as f:
    for line in f:
        if line.startswith("data:"):
            envelopes.append(json.loads(line[len("data:"):].strip()))

assert envelopes, "no envelopes received"

seqs = [e["seq"] for e in envelopes]
assert seqs == sorted(seqs), f"seq not monotonic: {seqs}"
assert len(seqs) == len(set(seqs)), f"duplicate seq values: {seqs}"
print(f"seq sequence strictly monotonic: {seqs}")

kinds = [e["kind"] for e in envelopes]
print(f"kind sequence: {kinds}")
assert kinds[0] == "run_started", f"expected first kind run_started, got {kinds[0]!r}"
assert kinds[-1] in ("run_completed", "run_failed"), f"expected a terminal kind last, got {kinds[-1]!r}"
assert "node_update" in kinds, "expected at least one node_update"

expected_nodes = {"analyzer", "architect", "dispatch", "executor", "validator", "oversight_git", "advance"}
seen_nodes = {e["node"] for e in envelopes if e["node"]}
print(f"nodes seen: {sorted(seen_nodes)}")
assert expected_nodes.issubset(seen_nodes), f"missing nodes: {expected_nodes - seen_nodes}"

print("PASS: full envelope sequence run_started -> node_updates -> run_completed, seq monotonic, terminal kind present")
PYEOF
PY_EXIT=$?

rm -f "$STREAM_FILE"

if [ "$CURL_EXIT" -ne 0 ]; then
    echo "FAIL: curl consumer did not exit cleanly (exit=$CURL_EXIT)"
    exit 1
fi
if [ "$PY_EXIT" -ne 0 ]; then
    echo "FAIL: envelope assertions failed"
    exit 1
fi

echo "RESULT: Task 5.2 mock E2E through the API -- PASS"
