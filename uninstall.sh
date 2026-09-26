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
# Fail visibly and retain ownership evidence if Docker refuses cleanup.
if command -v docker >/dev/null 2>&1; then
    sudo python3 /opt/petabyte-agent/template_storage.py uninstall
fi
# An old scratch-down script detached ALL loop devices. Do not invoke it here.
# Detach only the loop backed by our own scratch file, after checking its mount.
if mountpoint -q /var/lib/petabyte/scratch; then
    _source=$(findmnt -n -o SOURCE --target /var/lib/petabyte/scratch)
    [ "$_source" = /dev/mapper/pbscratch ] || { echo "Unexpected scratch mount; cleanup stopped" >&2; exit 1; }
    sudo umount /var/lib/petabyte/scratch
    sudo cryptsetup close pbscratch
fi
if command -v losetup >/dev/null 2>&1; then
    while IFS= read -r _loop; do
        [ -n "$_loop" ] && sudo losetup -d "$_loop"
    done < <(sudo losetup -j /var/lib/petabyte/scratch.img --output NAME --noheadings)
fi
sudo rm -f /usr/local/sbin/pb-scratch-up /usr/local/sbin/pb-scratch-down
sudo systemctl disable --now petabyte-scratch.service petabyte-egress.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/petabyte-scratch.service /etc/systemd/system/petabyte-egress.service
# Remove only Petabyte's jumps/chains; never flush the host firewall.
for _tables in iptables ip6tables; do
    command -v "$_tables" >/dev/null 2>&1 || continue
    while sudo "$_tables" -D DOCKER-USER -j PB-EGRESS 2>/dev/null; do :; done
    sudo "$_tables" -F PB-EGRESS 2>/dev/null || true
    sudo "$_tables" -X PB-EGRESS 2>/dev/null || true
    for _bridge in docker0 br+; do
        while sudo "$_tables" -D INPUT -i "$_bridge" -j PB-HOST-IN 2>/dev/null; do :; done
    done
    sudo "$_tables" -F PB-HOST-IN 2>/dev/null || true
    sudo "$_tables" -X PB-HOST-IN 2>/dev/null || true
done
sudo systemctl daemon-reload
sudo rm -rf /var/lib/petabyte
# install.sh installs the agent at /opt/petabyte-agent and secrets at /etc/petabyte.
sudo rm -rf /opt/petabyte-agent /etc/petabyte
echo "Uninstalled. Petabyte is gone. Owned template images and rental resources were removed. Docker was left installed — remove it with your"
echo "package manager if you added it only for Petabyte:  sudo apt-get remove docker-ce"
