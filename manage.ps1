# Petabyte node manager — pause, resume, uninstall, status.
# Run elevated. Either pass -Action, or set $env:PETABYTE_ACTION (for `irm ... | iex`).
#   $env:PETABYTE_ACTION="pause"; irm https://petabyte.market/manage.ps1 | iex
#
# uninstall reverts cleanly: it stops + removes the agent and the auto-start task,
# and — reading the state file written at install — unregisters the Ubuntu distro
# ONLY if we created it, and disables WSL ONLY if it wasn't already on this PC
# before Petabyte. It never nukes a distro/WSL the user already had.

param([ValidateSet("pause","resume","uninstall","status")][string]$Action = $env:PETABYTE_ACTION)
if (-not $Action) { $Action = "status" }
$ErrorActionPreference = "Stop"

$StateDir  = Join-Path $env:ProgramData "Petabyte"
$StateFile = Join-Path $StateDir "install-state.json"
$Task      = "PetabyteNode"

# read install state (defaults are conservative: assume things pre-existed, so we
# don't remove more than we should if the state file is missing).
$state = @{ wslPreexisted = $true; distroPreexisted = $true; distro = "Ubuntu-24.04" }
if (Test-Path $StateFile) {
    try { $state = Get-Content $StateFile -Raw | ConvertFrom-Json } catch {}
}
$Distro = $state.distro
function Admin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
if (-not (Admin)) { Write-Host "Run this in an elevated (Administrator) PowerShell." -ForegroundColor Red; exit 1 }

function Agent($cmd) { try { wsl.exe -d $Distro -u root -- bash -lc $cmd 2>$null } catch {} }

switch ($Action) {

  "pause" {
    Write-Host "Pausing Petabyte node (stays installed, just stops earning/running)..."
    Agent "systemctl stop petabyte-agent"
    try { Disable-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue | Out-Null } catch {}
    wsl.exe --shutdown
    Write-Host "Paused. Nothing runs until you resume. Resume anytime with -Action resume." -ForegroundColor Green
  }

  "resume" {
    Write-Host "Resuming Petabyte node..."
    try { Enable-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue | Out-Null } catch {}
    try { Start-ScheduledTask -TaskName $Task } catch {}
    Start-Sleep -Seconds 3
    Agent "systemctl start petabyte-agent"
    Write-Host "Resumed — node coming back online." -ForegroundColor Green
  }

  "status" {
    Write-Host "distro: $Distro   (wslPreexisted=$($state.wslPreexisted), distroPreexisted=$($state.distroPreexisted))"
    Agent "systemctl status petabyte-agent --no-pager -l | head -n 5"
  }

  "uninstall" {
    Write-Host "Uninstalling Petabyte node..." -ForegroundColor Yellow
    # 1. stop + remove the agent and auto-start task
    Agent "systemctl disable --now petabyte-agent"
    try { Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue } catch {}

    # 2. the distro: unregister only if WE created it; otherwise just remove the agent files
    if (-not $state.distroPreexisted) {
        Write-Host "  removing the $Distro distro we created..."
        wsl.exe --shutdown
        wsl.exe --unregister $Distro
    } else {
        Write-Host "  keeping $Distro (it was already on your PC); removing only Petabyte files."
        Agent "rm -rf /opt/petabyte /etc/petabyte 2>/dev/null; rm -f /etc/systemd/system/petabyte-agent.service 2>/dev/null; systemctl daemon-reload 2>/dev/null"
    }

    # 3. WSL itself: disable only if it wasn't already enabled before us
    if (-not $state.wslPreexisted) {
        Write-Host "  WSL was not enabled before Petabyte — disabling it..."
        try {
            Disable-WindowsOptionalFeature -Online -FeatureName "Microsoft-Windows-Subsystem-Linux" -NoRestart -ErrorAction SilentlyContinue | Out-Null
            Disable-WindowsOptionalFeature -Online -FeatureName "VirtualMachinePlatform" -NoRestart -ErrorAction SilentlyContinue | Out-Null
            Write-Host "  WSL disabled. A reboot completes its removal." -ForegroundColor Yellow
        } catch { Write-Host "  (couldn't auto-disable WSL features; you can turn them off in 'Windows Features'.)" }
    } else {
        Write-Host "  leaving WSL enabled (it was on before Petabyte)."
    }

    # 4. clean our state
    if (Test-Path $StateDir) { Remove-Item -Recurse -Force $StateDir -ErrorAction SilentlyContinue }
    Write-Host "Uninstalled. Petabyte is gone; your games and files were never touched." -ForegroundColor Green
    if (-not $state.wslPreexisted) { Write-Host "Reboot when convenient to finish removing WSL." -ForegroundColor Yellow }
  }
}
