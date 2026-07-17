#!/usr/bin/env bash
# Sandbox egress lockdown (Step 7, Task 1): drop all outbound-to-internet
# traffic from the dedicated sandbox network, via the host's DOCKER-USER
# iptables chain. Idempotent (checks before inserting).
#
# Why DOCKER-USER specifically: Docker jumps to DOCKER-USER from the FORWARD
# chain, which only processes traffic *routed through* the host to/from
# containers. Host-terminated traffic -- SSH into this box (INPUT) and the
# host's own outbound incl. Brain->Anthropic (OUTPUT) -- never traverses
# FORWARD, so rules added here cannot break SSH or the host's own LLM
# egress. That is the entire reason this is the safe layer to add rules to.
set -euo pipefail

NETWORK_NAME="${SYNDICATE_NETWORK_NAME:-syndicate-sandbox-net}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

SUBNET="$($SUDO docker network inspect "$NETWORK_NAME" --format '{{(index .IPAM.Config 0).Subnet}}')"
if [ -z "$SUBNET" ]; then
    echo "ERROR: could not discover subnet for network $NETWORK_NAME" >&2
    exit 1
fi
echo "Discovered sandbox subnet: $SUBNET"

insert_rule_if_absent() {
    local position="$1"
    shift
    if $SUDO iptables -C DOCKER-USER "$@" 2>/dev/null; then
        echo "Rule already present, skipping: $*"
    else
        $SUDO iptables -I DOCKER-USER "$position" "$@"
        echo "Inserted rule at position $position: $*"
    fi
}

# Evaluated top-down; desired final order:
#   1) allow ESTABLISHED,RELATED return traffic (so responses on already
#      forwarded connections keep flowing)
#   2) allow intra-subnet traffic within the sandbox network (loopback
#      equivalent for containers on this network)
#   3) drop new outbound from the sandbox subnet to anything else
# Each call inserts at position 1, so calling in this order (DROP, then
# intra-subnet, then established/related) leaves them in the order above.
insert_rule_if_absent 1 -s "$SUBNET" ! -d "$SUBNET" -j DROP
insert_rule_if_absent 1 -s "$SUBNET" -d "$SUBNET" -j RETURN
insert_rule_if_absent 1 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

echo "Egress lock applied for $SUBNET."
$SUDO iptables -L DOCKER-USER -n -v --line-numbers
