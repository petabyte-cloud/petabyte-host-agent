#!/usr/bin/env bash
# An optional OS sleep inhibitor; failure to acquire it must never stop the seller agent.
# WSL host sleep is managed by install.ps1's Windows-side helper instead.
set -euo pipefail
cd /opt/petabyte-agent

if [ "${PETABYTE_KEEP_AWAKE:-false}" = "true" ]; then
  if grep -qiE '(microsoft|wsl)' /proc/sys/kernel/osrelease 2>/dev/null; then
    echo "petabyte-agent: WSL host sleep is managed by the Windows keep-awake task" >&2
  elif command -v systemd-inhibit >/dev/null 2>&1; then
    # Run the inhibitor as a child so a failed inhibition cannot block agent startup. systemd
    # stops this child with the service; only idle sleep is inhibited, not manual sleep or lid close.
    systemd-inhibit --what=sleep --mode=block --who=Petabyte \
      --why="Petabyte seller agent is online" -- \
      /bin/bash -c 'while systemctl is-active --quiet petabyte-agent; do sleep 30; done' &
  else
    echo "petabyte-agent: systemd-inhibit unavailable; starting normally without blocking sleep" >&2
  fi
fi

exec /opt/petabyte-agent/.venv/bin/python /opt/petabyte-agent/main.py
