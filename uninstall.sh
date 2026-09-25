#!/usr/bin/env bash
# Petabyte Linux node uninstaller — stops + removes the agent service, the
# auto-update units, and all agent files. Paths must match install.sh.
set -e
echo "Stopping and removing the Petabyte agent..."
# The auto-update timer runs as root every 6h; it MUST be removed or it will keep
# re-downloading the agent into /opt/petabyte-agent after this script runs.
sudo systemctl disable --now petabyte-agent-update.timer 2>/dev/null || true
sudo systemctl disable --now petabyte-agent-update.service 2>/dev/null || true
sudo systemctl disable --now petabyte-agent 2>/dev/null || true
sudo rm -f /etc/systemd/system/petabyte-agent.service \
           /etc/systemd/system/petabyte-agent-update.service \
           /etc/systemd/system/petabyte-agent-update.timer
sudo systemctl daemon-reload 2>/dev/null || true
# install.sh installs the agent at /opt/petabyte-agent and secrets at /etc/petabyte.
sudo rm -rf /opt/petabyte-agent /etc/petabyte
echo "Uninstalled. Petabyte is gone. Docker was left installed — remove it with your"
echo "package manager if you added it only for Petabyte:  sudo apt-get remove docker-ce"
