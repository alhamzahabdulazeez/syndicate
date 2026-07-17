#!/usr/bin/env bash
# Task 5.7: uvicorn binds 127.0.0.1:8080 ONLY. This API triggers arbitrary
# sandbox bash via the frozen chassis's executor node -- a public bind is
# RCE-with-a-form-field. Proves: loopback succeeds, the box's external IP
# fails, and the listening socket itself is loopback-only.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
rm -rf data && mkdir -p data
export SYNDICATE_MOCK_CLIENT=1

python3 -m server.app > /tmp/syndicate_ui_bind_server.log 2>&1 &
SERVER_PID=$!
echo "server pid=$SERVER_PID"
trap 'kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null' EXIT

for i in $(seq 1 30); do
    curl -sf -m 2 http://127.0.0.1:8080/health >/dev/null 2>&1 && break
    sleep 1
done

echo "=== curl http://127.0.0.1:8080/health (must succeed) ==="
if curl -sf -m 3 http://127.0.0.1:8080/health; then
    echo
    echo "loopback: OK"
    LOOPBACK_OK=1
else
    echo "loopback: FAILED (unexpected)"
    LOOPBACK_OK=0
fi

EXTERNAL_IP="$(ip -4 addr show eth0 | grep inet | awk '{print $2}' | cut -d/ -f1 | head -1)"
echo
echo "=== curl http://$EXTERNAL_IP:8080/health (must FAIL -- not bound there) ==="
curl -sf -m 3 "http://$EXTERNAL_IP:8080/health" >/tmp/syndicate_ui_bind_external.log 2>&1
CURL_EXTERNAL_EXIT=$?
if [ "$CURL_EXTERNAL_EXIT" -eq 0 ]; then
    echo "external bind: UNEXPECTED SUCCESS -- this would be RCE-with-a-form-field"
    EXTERNAL_BLOCKED=0
elif [ "$CURL_EXTERNAL_EXIT" -eq 7 ]; then
    echo "external bind: connection refused as expected (curl exit=7, could not connect)"
    EXTERNAL_BLOCKED=1
else
    echo "external bind: curl failed for an unexpected reason (exit=$CURL_EXTERNAL_EXIT) -- not a confirmed connection-refused"
    EXTERNAL_BLOCKED=0
fi

echo
echo "=== ss -ltnp | grep 8080 (must show 127.0.0.1 only) ==="
SS_OUTPUT="$(ss -ltnp 2>/dev/null | grep 8080 || true)"
echo "$SS_OUTPUT"
if echo "$SS_OUTPUT" | grep -q "127.0.0.1:8080" && ! echo "$SS_OUTPUT" | grep -qE "0\.0\.0\.0:8080|\*:8080|:::8080"; then
    echo "socket: loopback-only, confirmed"
    SOCKET_OK=1
else
    echo "socket: NOT loopback-only -- FAIL"
    SOCKET_OK=0
fi

echo
if [ "$LOOPBACK_OK" -eq 1 ] && [ "$EXTERNAL_BLOCKED" -eq 1 ] && [ "$SOCKET_OK" -eq 1 ]; then
    echo "RESULT: Task 5.7 bind security -- PASS"
    exit 0
else
    echo "RESULT: Task 5.7 bind security -- FAIL"
    exit 1
fi
