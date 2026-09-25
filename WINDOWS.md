# Windows seller nodes (WSL2)

Windows machines — where most consumer gaming GPUs live — join Petabyte by running
the standard Linux agent inside **WSL2**. One agent codebase; Windows is a thin
bootstrap around it.

## Why WSL2 (and not a native Windows agent)
- **Same tested code path** as every Linux node — no second agent to maintain.
- **GPU works:** NVIDIA's Windows driver exposes CUDA into WSL2 automatically;
  `nvidia-container-toolkit` (installed by `install.sh`) gives Docker `--gpus all`.
- **Extra isolation for the seller:** buyer jobs run in a Docker sandbox *inside the
  WSL2 VM* — a hardware VM boundary between untrusted code and the Windows host.

## Requirements
- Windows 10 21H2+ or Windows 11, virtualization enabled in BIOS.
- NVIDIA GPU + current NVIDIA **Windows** driver (do NOT install a driver inside WSL).
- Admin PowerShell.

## Install (one line, elevated PowerShell)
```powershell
$env:PETABYTE_API_URL="https://petabyte.market"
$env:PETABYTE_API_KEY="pk_your_node_key"; $env:PRICE_PER_HOUR="1.5"
irm https://petabyte.market/install.ps1 | iex
```
If Windows installs WSL for the first time it may ask to **reboot — rerun the same
command after**. The script then: installs Ubuntu 24.04 → enables systemd → runs the
standard `install.sh` inside WSL (Docker, provision, attest, service) → registers a
hidden **Scheduled Task** so the node comes online at logon.

## Verify
```powershell
wsl -d Ubuntu-24.04 -u root -- systemctl status petabyte-agent
wsl -d Ubuntu-24.04 -u root -- journalctl -u petabyte-agent -f
```
The GPU appears in the marketplace exactly like a Linux node.

## Honest limits
- The node is online while the machine is on and WSL is running (the scheduled task
  keeps it alive after logon). Sleep/hibernate takes it offline — the reaper will
  refund any in-flight booking, and heartbeats resume on wake.
- Laptops on battery may throttle; price accordingly.
- Firecracker/KVM microVM paths don't apply on Windows — the sandbox is Docker inside
  the WSL2 VM (which itself adds a VM boundary).
