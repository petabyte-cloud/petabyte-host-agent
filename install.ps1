# Petabyte Windows node installer — runs the (tested) Linux agent inside WSL2.
# Run in an ELEVATED PowerShell (the /install page generates this with your key filled in):
#   $env:PETABYTE_API_URL="https://petabyte.market"
#   $env:PETABYTE_API_KEY="pk_your_node_key"
#   irm https://petabyte.market/install.ps1 | iex
# PRICE_PER_HOUR is optional: leave it unset to auto-price from your GPU's benchmark,
# or set $env:PRICE_PER_HOUR="1.5" to pin your own rate.
#
# What it does:
#   1) Verifies admin + NVIDIA driver (nvidia-smi on Windows).
#   2) Installs WSL2 + Ubuntu 24.04 (may require ONE reboot; rerun after).
#   3) Enables systemd inside the distro.
#   4) Runs the standard Linux install.sh inside WSL (Docker sandbox, provision,
#      attestation, petabyte-agent systemd service) — same code as Linux nodes.
#   5) Registers a hidden Scheduled Task so the node comes online at logon.
#
# GPU note: NVIDIA's Windows driver exposes CUDA to WSL2 automatically (no driver
# install inside Linux). install.sh adds nvidia-container-toolkit so Docker jobs
# can use --gpus all.

$ErrorActionPreference = "Stop"
$Distro = "Ubuntu-24.04"

function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# --- 0. preconditions -------------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "run this in an elevated (Administrator) PowerShell." }

foreach ($v in "PETABYTE_API_URL","PETABYTE_API_KEY") {
    if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue)) { Fail "set `$env:$v first." }
}
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Host "WARNING: nvidia-smi not found — install the NVIDIA Windows driver for GPU jobs." -ForegroundColor Yellow
}

# --- 1. WSL2 + Ubuntu -------------------------------------------------------
$wslOk = $false
try { wsl.exe --status | Out-Null; $wslOk = $true } catch {}
$wslPre = $wslOk    # was WSL already present BEFORE Petabyte touched anything?
if (-not $wslOk) {
    Write-Host "==> installing WSL2 (a reboot may be required — rerun this script after)"
    wsl.exe --install --no-distribution
    Write-Host "If Windows asks to reboot: reboot, then rerun this script." -ForegroundColor Yellow
}
wsl.exe --set-default-version 2 | Out-Null

$have = (wsl.exe -l -q) -join "`n"
$distroPre = ($have -match [regex]::Escape($Distro))   # did this distro exist before us?

# Record pre-install state so the uninstaller knows what WE added vs. what was
# already here (so "uninstall" truly reverts a fresh machine, but never nukes a
# distro/WSL the user already had).
$StateDir = Join-Path $env:ProgramData "Petabyte"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
@{ wslPreexisted = [bool]$wslPre; distroPreexisted = [bool]$distroPre;
   distro = $Distro; installedAt = (Get-Date).ToString("o") } |
   ConvertTo-Json | Set-Content (Join-Path $StateDir "install-state.json")

if ($have -notmatch [regex]::Escape($Distro)) {
    Write-Host "==> installing $Distro (first run may prompt to create a UNIX user)"
    wsl.exe --install -d $Distro --no-launch
    wsl.exe --install -d $Distro
}

# --- 2. systemd inside the distro (needed for the agent service) ------------
Write-Host "==> enabling systemd in $Distro"
wsl.exe -d $Distro -u root -- sh -c "printf '[boot]\nsystemd=true\n' > /etc/wsl.conf"
wsl.exe --shutdown
Start-Sleep -Seconds 3

# --- 3. run the standard Linux installer inside WSL -------------------------
Write-Host "==> installing the Petabyte agent inside $Distro"
$sh = @(
    "export PETABYTE_API_URL='$($env:PETABYTE_API_URL)'",
    "export PETABYTE_API_KEY='$($env:PETABYTE_API_KEY)'",
    "export PRICE_PER_HOUR='$(if ($env:PRICE_PER_HOUR) { $env:PRICE_PER_HOUR } else { '' })'",
    "export PETABYTE_SELL_SCHEDULE='$(if ($env:PETABYTE_SELL_SCHEDULE) { $env:PETABYTE_SELL_SCHEDULE } else { 'always' })'",
    "export PETABYTE_IDLE_MINING='$(if ($env:PETABYTE_IDLE_MINING) { $env:PETABYTE_IDLE_MINING } else { 'true' })'",
    "export UNITS='$(if ($env:UNITS) { $env:UNITS } else { '1' })'",
    "export GPU_MODEL='$($env:GPU_MODEL)'",
    "export PETABYTE_KATA='$(if ($env:PETABYTE_KATA) { $env:PETABYTE_KATA } else { '' })'",
    "if [ -f ./install.sh ]; then bash ./install.sh; else bash <(curl -fsSL $($env:PETABYTE_API_URL)/install.sh); fi"
) -join "; "
wsl.exe -d $Distro -u root -- bash -lc "$sh"
if ($LASTEXITCODE -ne 0) { Fail "agent install inside WSL failed (see output above)." }

# --- 4. keep the node online: start WSL (and its systemd) at logon ----------
Write-Host "==> registering auto-start task"
$action  = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro --exec sleep infinity"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
Register-ScheduledTask -TaskName "PetabyteNode" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "PetabyteNode"

Write-Host ""
Write-Host "node online (inside WSL2)." -ForegroundColor Green
Write-Host "  status: wsl -d $Distro -u root -- systemctl status petabyte-agent"
Write-Host "  logs:   wsl -d $Distro -u root -- journalctl -u petabyte-agent -f"
Write-Host ""
Write-Host "Low-commitment controls (manage.ps1):" -ForegroundColor Cyan
Write-Host "  pause:     `$env:PETABYTE_ACTION='pause';     irm $($env:PETABYTE_API_URL)/manage.ps1 | iex"
Write-Host "  resume:    `$env:PETABYTE_ACTION='resume';    irm $($env:PETABYTE_API_URL)/manage.ps1 | iex"
Write-Host "  uninstall: `$env:PETABYTE_ACTION='uninstall'; irm $($env:PETABYTE_API_URL)/manage.ps1 | iex"
Write-Host "  (uninstall removes the agent + distro, and if WSL wasn't already on your PC, disables it too.)"
