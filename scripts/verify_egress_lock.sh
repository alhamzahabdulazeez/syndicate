#!/usr/bin/env bash
# Step 7, Task 2 -- THE gate. Empirically proves all four egress-lock
# invariants and distinguishes failure modes. Exits non-zero on any failure;
# if invariant 1 passes but 2/3 fail, the drop rule is too broad -- this
# script says so explicitly rather than just failing generically.
#
# Run with:
#   bash scripts/verify_egress_lock.sh 2>&1 | tee artifacts/step7/01_egress.log
set -uo pipefail

CONTAINER_NAME="${SYNDICATE_CONTAINER_NAME:-syndicate-runtime}"
PUBLIC_IP="1.1.1.1"
PUBLIC_HOST="www.google.com"
FAILURES=0

pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1"; FAILURES=$((FAILURES + 1)); }

echo "=== Invariant 1: container egress blocked (route-level, not just DNS) ==="
if docker exec "$CONTAINER_NAME" curl -sf -m 5 "http://$PUBLIC_IP" >/dev/null 2>&1; then
    fail "container reached raw public IP $PUBLIC_IP -- egress lock is NOT effective (route-level)"
else
    pass "container outbound to raw public IP $PUBLIC_IP blocked/timed out"
fi

if docker exec "$CONTAINER_NAME" curl -sf -m 5 "https://$PUBLIC_HOST" >/dev/null 2>&1; then
    fail "container reached public hostname $PUBLIC_HOST -- egress lock is NOT effective"
else
    pass "container outbound to public hostname $PUBLIC_HOST blocked/timed out"
fi

echo "=== Invariant 2: ingress to the sandbox on :8000 still works from the host ==="
if curl -sf -m 5 http://127.0.0.1:8000/openapi.json >/dev/null 2>&1; then
    pass "host -> 127.0.0.1:8000/openapi.json reachable"
else
    fail "host -> 127.0.0.1:8000/openapi.json NOT reachable -- drop rule is too broad, blocking ingress too"
fi

echo "=== Invariant 3: request/response (established/related return traffic) intact ==="
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if SYNDICATE_RUNTIME_URL=http://localhost SYNDICATE_RUNTIME_PORT=8000 \
    python3 "$REPO_ROOT/scripts/verify_runtime_fileops.py"; then
    pass "verify_runtime_fileops.py passed end-to-end -- established/related return traffic flows"
else
    fail "verify_runtime_fileops.py FAILED -- drop rule is too broad, blocking return traffic on forwarded connections"
fi

echo "=== Invariant 4: host's own egress (Brain -> Anthropic path) unaffected ==="
HOST_CODE="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "https://$PUBLIC_HOST" 2>/dev/null || echo 000)"
if [ "$HOST_CODE" != "000" ]; then
    pass "host -> https://$PUBLIC_HOST reachable (http_code=$HOST_CODE) -- host egress untouched"
else
    fail "host -> https://$PUBLIC_HOST NOT reachable -- egress rules leaked into host's own OUTPUT path"
fi

# Known gap, not covered above: an "Invariant 5" for intra-subnet allow
# (container A -> container B on the sandbox network still works under the
# lock) is untested. lock_egress.sh's rule 2 (-s $SUBNET -d $SUBNET -j
# RETURN) exists to permit this, but with a single container on the network
# there is nothing else on the subnet to verify it against. Would need a
# second container on syndicate-sandbox-net to actually exercise this path.
#
# Also known and unaddressed: the DROP in lock_egress.sh is unconditional
# past the intra-subnet/established carve-outs -- there is no allowlist.
# Under the lock, a ticket cannot pip install, git clone, or npm install
# from inside the sandbox. No hook for exceptions exists anywhere in
# lock_egress.sh/unlock_egress.sh. Not solved here; flagging so it's a
# known constraint on any Step 8 work that needs dependency resolution
# inside the sandbox, not a rediscovered one.

echo
echo "=== DOCKER-USER chain (for the record) ==="
IPTABLES_SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    IPTABLES_SUDO="sudo"
fi
$IPTABLES_SUDO iptables -L DOCKER-USER -n -v --line-numbers

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "RESULT: all 4 egress-lock invariants PASS."
    exit 0
else
    echo "RESULT: $FAILURES invariant(s) FAILED. See [FAIL] lines above."
    exit 1
fi
