#!/usr/bin/env bash
# Canonical start script for the Syndicate execution sandbox (an external
# OpenHands agent-server container -- see syndicate/runtime/openhands.py for
# the adapter that talks to it). Idempotent: re-running when the
# container/network already exist is a no-op that just re-verifies
# readiness.
#
# Step 7 (Task 1) extends this from a plain `docker run` into "create a
# dedicated bridge network, then run the container on it" so the egress-lock
# iptables rules in scripts/lock_egress.sh have a stable subnet to target,
# without resorting to --network none (kills the :8000 publish) or
# --internal (unreliable interaction with published-port ingress).
set -euo pipefail

CONTAINER_NAME="${SYNDICATE_CONTAINER_NAME:-syndicate-runtime}"
NETWORK_NAME="${SYNDICATE_NETWORK_NAME:-syndicate-sandbox-net}"
IMAGE="${SYNDICATE_RUNTIME_IMAGE:-ghcr.io/openhands/agent-server:1.12.0-python}"
WORKSPACE_DIR="${SYNDICATE_WORKSPACE_DIR:-$HOME/syndicate-workspace}"
READY_URL="http://127.0.0.1:8000/openapi.json"
READY_TIMEOUT_SECONDS="${SYNDICATE_READY_TIMEOUT_SECONDS:-60}"

mkdir -p "$WORKSPACE_DIR"

if ! docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    echo "Creating dedicated bridge network: $NETWORK_NAME"
    docker network create "$NETWORK_NAME"
else
    echo "Network $NETWORK_NAME already exists, reusing."
fi

if docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    STATE="$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME")"
    CURRENT_NET="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$CONTAINER_NAME")"
    if [ "$CURRENT_NET" != "$NETWORK_NAME" ]; then
        echo "Existing container $CONTAINER_NAME is on network '$CURRENT_NET', not '$NETWORK_NAME'. Recreating."
        docker rm -f "$CONTAINER_NAME" >/dev/null
    elif [ "$STATE" = "running" ]; then
        echo "Container $CONTAINER_NAME already running on $NETWORK_NAME."
    else
        echo "Container $CONTAINER_NAME exists but is not running (state=$STATE). Starting it."
        docker start "$CONTAINER_NAME" >/dev/null
    fi
fi

if ! docker inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "Starting $CONTAINER_NAME on network $NETWORK_NAME"
    docker run -d \
        --name "$CONTAINER_NAME" \
        --network "$NETWORK_NAME" \
        -p 127.0.0.1:8000:8000 \
        --memory=512m --memory-swap=1g \
        --restart unless-stopped \
        -v "$WORKSPACE_DIR:/workspace" \
        "$IMAGE"
fi

echo "Polling readiness at $READY_URL (timeout ${READY_TIMEOUT_SECONDS}s)..."
elapsed=0
until curl -sf "$READY_URL" >/dev/null 2>&1; do
    sleep 2
    elapsed=$((elapsed + 2))
    if [ "$elapsed" -ge "$READY_TIMEOUT_SECONDS" ]; then
        echo "ERROR: runtime did not become ready within ${READY_TIMEOUT_SECONDS}s" >&2
        exit 1
    fi
done
echo "Runtime ready: $READY_URL"
