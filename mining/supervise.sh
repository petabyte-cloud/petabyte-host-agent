#!/bin/sh
# The container stops itself even if the agent is killed. Only a tiny lease
# directory is mounted, never the Docker socket, agent secrets or seller files.
set -eu
valid() {
    [ ! -e /lease/disabled ] || return 1
    read -r expiry < /lease/until || return 1
    case "$expiry" in ''|*[!0-9]*) return 1 ;; esac
    read -r uptime rest < /proc/uptime
    now=${uptime%%.*}
    [ "$expiry" -gt "$now" ] && [ "$expiry" -le "$((now + 31))" ]
}
valid || exit 0
miner "$@" &
miner=$!
trap 'kill "$miner" 2>/dev/null || true; exit 0' TERM INT
while kill -0 "$miner" 2>/dev/null && valid; do sleep 1; done
kill "$miner" 2>/dev/null || true
sleep 1
kill -9 "$miner" 2>/dev/null || true
wait "$miner" 2>/dev/null || true
