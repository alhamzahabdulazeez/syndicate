#!/usr/bin/env bash
# Teardown for scripts/lock_egress.sh: removes exactly the rules that script
# added (matched deletes via `iptables -D`), never `iptables -F`. Safe to
# run even if some/all rules are already gone.
set -euo pipefail

NETWORK_NAME="${SYNDICATE_NETWORK_NAME:-syndicate-sandbox-net}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

SUBNET="$($SUDO docker network inspect "$NETWORK_NAME" --format '{{(index .IPAM.Config 0).Subnet}}' 2>/dev/null || true)"

delete_rule_if_present() {
    if $SUDO iptables -C DOCKER-USER "$@" 2>/dev/null; then
        $SUDO iptables -D DOCKER-USER "$@"
        echo "Removed rule: $*"
    else
        echo "Rule not present, nothing to remove: $*"
    fi
}

if [ -n "$SUBNET" ]; then
    delete_rule_if_present -s "$SUBNET" ! -d "$SUBNET" -j DROP
    delete_rule_if_present -s "$SUBNET" -d "$SUBNET" -j RETURN
else
    echo "WARNING: network $NETWORK_NAME not found, skipping subnet-scoped rule removal."
fi
delete_rule_if_present -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

echo "Egress lock rules removed (matched deletes only; chain not flushed)."
$SUDO iptables -L DOCKER-USER -n -v --line-numbers
