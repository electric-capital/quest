#!/bin/bash
# Entrypoint for the quest-script-runner container.
#
# This script runs as root inside the container (via --user=0:0) so it can
# set up iptables rules and then drop to the unprivileged target user.
# The container uses --userns=keep-id so UID/GID mapping is correct; root
# inside the container maps to a sub-UID on the host, not actual host root.
#
# Steps:
#   1. Start socat forwarder in the background
#      (localhost:<port> -> 10.0.2.2:<port>)
#   2. Load iptables rules (one atomic iptables-restore) allowing ONLY the sandbox tool-API port on the
#      slirp gateway, and REJECT every other private / link-local
#      destination; REJECT all IPv6 (fail closed if iptables/ip6tables
#      are unavailable)
#   3. Remove execute permission from iptables binaries
#   4. Wait (bounded) for socat to listen; fail closed if it does not
#   5. Drop all capabilities and switch to the target user via setpriv

# wait_for_port_listen PORT PID PROC_NET_TCP
# Return 0 once PROC_NET_TCP lists 127.0.0.1:PORT in LISTEN state (0A).
# Return 1 as soon as PID (the socat process) has exited, or after 200
# checks 5 ms apart (at least 1 s; more with the per-check sleep fork).
# The kernel prints addresses as uppercase hex with the port zero-padded
# to 4 digits; 0100007F is 127.0.0.1 as printed on little-endian hosts.
wait_for_port_listen() {
    local want addr state _
    printf -v want '0100007F:%04X' "$1"
    for _ in {1..200}; do
        while read -r _ addr _ state _; do
            if [[ "$addr" == "$want" && "$state" == "0A" ]]; then
                return 0
            fi
        done < "$3"
        kill -0 "$2" 2>/dev/null || return 1
        sleep 0.005
    done
    return 1
}

# When sourced (by tests) stop here: only the function above is wanted.
[[ "${BASH_SOURCE[0]}" != "$0" ]] && return 0

PORT="${QUEST_PORT:-8000}"
RUN_UID="${QUEST_RUN_UID:-1000}"
RUN_GID="${QUEST_RUN_GID:-1000}"

socat "TCP-LISTEN:${PORT},fork,reuseaddr,bind=127.0.0.1" "TCP:10.0.2.2:${PORT}" &
SOCAT_PID=$!

# Lock down host access: the ONLY permitted destination is the sandbox
# tool-API port on the slirp gateway. Everything else -- including the host
# itself reached via its real LAN IP -- must be rejected.
#
# 10.0.2.2 is the slirp4netns host gateway, but the host is ALSO reachable
# at its real address (e.g. host.containers.internal in /etc/hosts, or any
# 10.x/172.16.x/192.168.x IP the host holds). outbound_addr=127.0.0.1 kills
# genuine internet egress but NOT these host-local addresses: slirp delivers
# them to the host's loopback, so any service bound to 0.0.0.0 (the main
# Quest server, SSHd, the cloud metadata endpoint) answers. A rule that only
# names 10.0.2.2 leaves all of those wide open. So we ACCEPT the one allowed
# gateway:port and then REJECT every private + link-local range outright.
if ! command -v iptables-restore >/dev/null 2>&1 || ! command -v ip6tables >/dev/null 2>&1; then
    echo "restricted sandbox: iptables/ip6tables unavailable, refusing to start" >&2
    exit 1
fi

# IPv6: the container is launched with slirp4netns enable_ipv6=false and
# net.ipv6.conf.all.disable_ipv6=1, so there should be no IPv6 stack at all.
# Belt and braces -- none of the rules below cover IPv6, and slirp's default
# IPv6 gateway (fd00::2) is the host's loopback -- reject every IPv6
# destination and fail closed if the rule cannot be installed.
if ! ip6tables -A OUTPUT -j REJECT; then
    echo "restricted sandbox: failed to install IPv6 egress policy, refusing to start" >&2
    exit 1
fi

# The single allowed channel: the sandbox tool-API port on the gateway,
# then reject the host and the rest of the LAN. 10.0.0.0/8 covers the slirp
# gateway (10.0.2.2) too, so anything not matched by the ACCEPT is denied.
# 169.254.0.0/16 covers cloud metadata (e.g. 169.254.169.254).
#
# Loaded in ONE iptables-restore call (one process start instead of one per
# rule, on the critical path of every sandbox run). The restore commits
# atomically, so the policy is all-or-nothing and a failure fails closed.
if ! iptables-restore <<EOF
*filter
-A OUTPUT -d 10.0.2.2/32 -p tcp -m tcp --dport ${PORT} -j ACCEPT
-A OUTPUT -d 10.0.0.0/8 -j REJECT
-A OUTPUT -d 172.16.0.0/12 -j REJECT
-A OUTPUT -d 192.168.0.0/16 -j REJECT
-A OUTPUT -d 169.254.0.0/16 -j REJECT
-A OUTPUT -d 100.64.0.0/10 -j REJECT
COMMIT
EOF
then
    echo "restricted sandbox: failed to install IPv4 egress policy, refusing to start" >&2
    exit 1
fi

# Remove execute permission from iptables binaries so the user's script
# cannot call them even if capabilities were somehow retained.
chmod 0 /usr/sbin/xtables-nft-multi /usr/sbin/xtables-legacy-multi 2>/dev/null

# socat has been binding while the rules above loaded, so this usually
# passes on its first check. Ordering is safe: nothing can connect to the
# bridge before the user's command starts below, and by then the ACCEPT
# rule for the tool-API port is in place.
if ! wait_for_port_listen "$PORT" "$SOCAT_PID" /proc/net/tcp; then
    echo "restricted sandbox: socat bridge did not start, refusing to start" >&2
    exit 1
fi

# Drop to the target user with all capabilities cleared and no-new-privs set.
# --reuid/--regid: switch to the unprivileged user that owns the workspace
# --clear-groups: remove root's supplementary groups
# --inh-caps=-all: clear inheritable capabilities
# --bounding-set=-all: clear the capability bounding set
# --no-new-privs: prevent the exec'd process from gaining any capabilities
exec setpriv \
    --reuid="${RUN_UID}" --regid="${RUN_GID}" \
    --clear-groups \
    --inh-caps=-all \
    --bounding-set=-all \
    --no-new-privs \
    -- "$@"
