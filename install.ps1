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
#   2) Installs WSL2 (may require ONE reboot; rerun after) and gives the agent its OWN distro,
#      "Petabyte" (Ubuntu 24.04), so Docker Desktop's WSL integration never serves it.
#   3) Enables systemd inside the distro.
#   4) Runs the standard Linux install.sh inside WSL (Docker sandbox, provision,
#      attestation, petabyte-agent systemd service) — same code as Linux nodes.
#   5) Registers a Scheduled Task so the node comes online at logon. Its window
#      ("Petabyte node - keep open") is the node: closing it takes the GPU offline.
#
# Re-running this on a PC that already has a node repairs THAT node (same listing);
# set $env:PETABYTE_NEW_NODE="1" to list it as a new node instead.
#
# GPU note: NVIDIA's Windows driver exposes CUDA to WSL2 automatically (no driver
# install inside Linux). install.sh adds nvidia-container-toolkit so Docker jobs
# can use --gpus all.

$ErrorActionPreference = "Stop"


# Ask immediately, but never make unattended installers wait for input.
# Keep-awake is ON by default (owner, 2026-10-02): a seller PC that idle-sleeps drops every rental, and the
# old prompt defaulted to "no" whenever nobody was watching. Only an EXPLICIT earlier "no" is reused.
$StateDir = Join-Path $env:ProgramData "Petabyte"
$StateFile = Join-Path $StateDir "install-state.json"
$prev = $null
if (Test-Path $StateFile) { try { $prev = Get-Content $StateFile -Raw | ConvertFrom-Json } catch {} }
$keepAwakeDefault = if ($prev -and $prev.keepAwakeChosen -eq $true -and $null -ne $prev.keepAwake) { [bool]$prev.keepAwake } else { $true }
$keepAwake = $keepAwakeDefault
$keepAwakeChosen = [bool]($prev -and $prev.keepAwakeChosen -eq $true)
$keepAwakeEnv = ([string]$env:PETABYTE_KEEP_AWAKE).Trim().ToLowerInvariant()
if ($keepAwakeEnv -in @("true","1","yes","y")) { $keepAwake = $true; $keepAwakeChosen = $true }
elseif ($keepAwakeEnv -in @("false","0","no","n")) { $keepAwake = $false; $keepAwakeChosen = $true }
else {
    Write-Host "Keep this PC awake while the Petabyte seller agent runs, so rentals are not cut off? [Y/n; 15s timeout]"
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true).Key
                if ($key -eq [ConsoleKey]::Y) { $keepAwake = $true; $keepAwakeChosen = $true; break }
                if ($key -eq [ConsoleKey]::N) { $keepAwake = $false; $keepAwakeChosen = $true; break }
                if ($key -eq [ConsoleKey]::Enter) { break }
            }
            Start-Sleep -Milliseconds 100
        }
    } catch {
        # No interactive console (for example, irm ... | iex in an automated shell).
        $keepAwake = $keepAwakeDefault
    }
}
$env:PETABYTE_KEEP_AWAKE = ([string]$keepAwake).ToLowerInvariant()
if ($keepAwake) { Write-Host "Keep-awake ON: this PC will not idle-sleep while the agent runs (the screen can still turn off; manual sleep and closing the lid still work). To turn it off, rerun with `$env:PETABYTE_KEEP_AWAKE='false'." -ForegroundColor Cyan }
else { Write-Host "Keep-awake OFF: if Windows sleeps, your node goes offline and active rentals end." -ForegroundColor Yellow }
# The agent's OWN distro. Buyers' apps need the NATIVE Docker Engine (each rental gets its own
# firewalled network); a distro served by Docker Desktop's WSL integration (often the seller's own
# Ubuntu) can't isolate them, and Docker Desktop must never be touched. So: a distro of our own.
$Distro = "Petabyte"
$RootfsUrl = "https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz"
$env:WSL_UTF8 = "1"   # else `wsl -l` prints UTF-16 and distro-name matching silently fails

function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }
function HasDistro($name) {
    (((wsl.exe -l -q) -join "`n") -replace "`0", "") -match "(?m)^\s*$([regex]::Escape($name))\s*$"
}

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

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

# An earlier install in another distro (before the agent had its own): upgrade it IN PLACE when its
# Docker is native; MOVE it here, keeping the same node registration, when Docker Desktop serves it.
$old = if ($prev -and $prev.distro -and $prev.distro -ne $Distro) { [string]$prev.distro } else { $null }
$migrate = $false
if ($old -and (HasDistro $old)) {
    $sig = (wsl.exe -d $old -u root -- sh -c "{ test -s /etc/petabyte/agent.env && echo pb-agent; readlink -f /var/run/docker.sock; docker info --format '{{.OperatingSystem}}'; } 2>/dev/null") -join " "
    if ($sig -match "pb-agent") {
        if ($sig -match "docker-desktop|Docker Desktop") { $migrate = $true } else { $Distro = $old }
    }
}

# Record pre-install state so the uninstaller knows what WE added vs. what was
# already here (so "uninstall" truly reverts a fresh machine, but never nukes a
# distro/WSL the user already had). A re-run keeps the FIRST run's answers.
$distroPre = if ($prev -and $prev.distro -eq $Distro) { [bool]$prev.distroPreexisted } else { [bool](HasDistro $Distro) }
if ($prev) { $wslPre = [bool]$prev.wslPreexisted }
@{ wslPreexisted = [bool]$wslPre; distroPreexisted = $distroPre;
   distro = $Distro; keepAwake = [bool]$keepAwake; keepAwakeChosen = [bool]$keepAwakeChosen; installedAt = (Get-Date).ToString("o") } |
   ConvertTo-Json | Set-Content $StateFile

if (-not (HasDistro $Distro)) {
    Write-Host "==> creating the $Distro WSL distro (Ubuntu 24.04, ~350 MB download)"
    $tar = Join-Path $env:TEMP "petabyte-ubuntu-rootfs.tar.gz"
    curl.exe -fL --retry 3 -o $tar $RootfsUrl
    if ($LASTEXITCODE -ne 0) { Fail "could not download the Ubuntu image ($RootfsUrl)." }
    $sums = curl.exe -fsSL ($RootfsUrl.Substring(0, $RootfsUrl.LastIndexOf("/")) + "/SHA256SUMS")
    $want = (($sums | Where-Object { $_ -match "ubuntu-noble-wsl-amd64-wsl\.rootfs\.tar\.gz$" }) -split "\s+")[0]
    if (-not $want -or (Get-FileHash $tar -Algorithm SHA256).Hash -ne $want) {
        Remove-Item $tar -Force; Fail "the downloaded Ubuntu image failed its SHA-256 check."
    }
    wsl.exe --import $Distro (Join-Path $StateDir "wsl") $tar --version 2
    $imported = ($LASTEXITCODE -eq 0)
    Remove-Item $tar -Force
    if (-not $imported) { Fail "could not create the $Distro WSL distro." }
}

# --- 2. systemd inside the distro (needed for the agent service) ------------
Write-Host "==> enabling systemd in $Distro"
wsl.exe -d $Distro -u root -- sh -c "printf '[boot]\nsystemd=true\n' > /etc/wsl.conf"
wsl.exe --shutdown
Start-Sleep -Seconds 3

# --- 2b. move an existing node out of a Docker Desktop distro -----------------
$keep = "true"
function KeepOld {   # a failed move leaves the node running where it was, and the state saying so
    wsl.exe -d $old -u root -- sh -c "systemctl enable --now petabyte-agent petabyte-agent-update.timer >/dev/null 2>&1; true"
    $prev | ConvertTo-Json | Set-Content $StateFile
}
if ($migrate) {
    Write-Host "==> moving this node out of $old (Docker Desktop serves it) into $Distro; Docker Desktop is left as it is"
    wsl.exe -d $old -u root -- sh -c "systemctl disable --now petabyte-agent petabyte-agent-update.timer petabyte-egress.service >/dev/null 2>&1; true"
    # Same registration + node key, so it stays the same listing. cmd.exe pipes bytes untouched
    # (a PowerShell pipe would re-encode the tar stream).
    cmd.exe /c "wsl.exe -d $old -u root -- tar -C /etc -cf - petabyte | wsl.exe -d $Distro -u root -- tar -C /etc -xpf -"
    if ($LASTEXITCODE -ne 0) {
        KeepOld
        Fail "could not copy this node's registration out of $old; it keeps running there (batch jobs only)."
    }
    $keep = "export PETABYTE_KEEP_SPEC=1"
} elseif ($env:PETABYTE_NEW_NODE -ne "1") {
    # Re-running the installer on a PC that already has a node REPAIRS that node: same listing,
    # same spec id (install.sh -> provision.py re-attests it with this key). Without this, every
    # reinstall listed the PC again and the old listing stayed behind as an offline duplicate.
    # PETABYTE_NEW_NODE=1 forces a fresh registration.
    $existing = (wsl.exe -d $Distro -u root -- sh -c "sed -n 's/^PETABYTE_SPEC_ID=//p' /etc/petabyte/agent.env 2>/dev/null") -join ""
    if ($existing.Trim()) {
        Write-Host ""
        Write-Host "This PC already has a Petabyte node (#$($existing.Trim())). Repairing it in place - it keeps the same listing." -ForegroundColor Cyan
        Write-Host "Next time it is just offline, you don't need to reinstall - resume it instead:" -ForegroundColor Cyan
        Write-Host "  irm $($env:PETABYTE_API_URL)/manage.ps1 | iex     (then choose 1 = Resume)" -ForegroundColor Cyan
        Write-Host ""
        $keep = "export PETABYTE_KEEP_SPEC=1"
    }
}

# --- 3. run the standard Linux installer inside WSL -------------------------
Write-Host "==> installing the Petabyte agent inside $Distro"
$sh = @(
    "command -v curl >/dev/null || { apt-get update -y && apt-get install -y curl ca-certificates; }",
    $keep,
    "export PETABYTE_API_URL='$($env:PETABYTE_API_URL)'",
    "export PETABYTE_API_KEY='$($env:PETABYTE_API_KEY)'",
    "export PETABYTE_KEEP_AWAKE='$($env:PETABYTE_KEEP_AWAKE)'",
    "export PRICE_PER_HOUR='$(if ($env:PRICE_PER_HOUR) { $env:PRICE_PER_HOUR } else { '' })'",
    "export PETABYTE_SELL_SCHEDULE='$(if ($env:PETABYTE_SELL_SCHEDULE) { $env:PETABYTE_SELL_SCHEDULE } else { 'always' })'",
    "export UNITS='$(if ($env:UNITS) { $env:UNITS } else { '1' })'",
    "export GPU_MODEL='$($env:GPU_MODEL)'",
    "export PETABYTE_KATA='$(if ($env:PETABYTE_KATA) { $env:PETABYTE_KATA } else { '' })'",
    "if [ -f ./install.sh ]; then bash ./install.sh; else bash <(curl -fsSL $($env:PETABYTE_API_URL)/install.sh); fi"
) -join "; "
wsl.exe -d $Distro -u root -- bash -lc "$sh"
if ($LASTEXITCODE -ne 0) {
    if ($migrate) { KeepOld }
    Fail "agent install inside WSL failed (see output above)."
}
if ($migrate) {   # only now: the node runs in $Distro. Nothing else in $old is touched.
    wsl.exe -d $old -u root -- sh -c "rm -rf /opt/petabyte-agent /etc/petabyte /etc/systemd/system/petabyte-agent* /etc/systemd/system/petabyte-egress.service; systemctl daemon-reload >/dev/null 2>&1; true"
    Write-Host "==> removed the old agent from $old"
}

# --- 4. keep the node online: start WSL (and its systemd) at logon ----------
Write-Host "==> registering auto-start task"
# Its window IS the node: it keeps WSL running, so closing it takes the GPU offline. On Windows 11
# it opens as a Windows Terminal tab, so keepalive.sh titles it and says so instead of leaving a
# blank `wsl.exe` tab that sellers close. Falls back to a bare keep-alive if the script is missing.
$action  = New-ScheduledTaskAction -Execute "wsl.exe" `
    -Argument "-d $Distro --exec /bin/sh -c `"bash /opt/petabyte-agent/keepalive.sh || exec sleep infinity`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
Register-ScheduledTask -TaskName "PetabyteNode" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "PetabyteNode"

# Keep the Windows host awake only while the WSL seller service is active.
# This does not keep the display on or override manual sleep/lid actions.
if ($keepAwake) {
    $KeepAwakeScriptPath = Join-Path $StateDir "keep-awake.ps1"
    @'
param([Parameter(Mandatory=$true)][string]$Distro)
$ErrorActionPreference = "SilentlyContinue"
if (-not ("PetabytePower.Native" -as [type])) {
    Add-Type -Namespace PetabytePower -Name Native -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
public static extern uint SetThreadExecutionState(uint esFlags);
"@
}
$ES_CONTINUOUS = [Convert]::ToUInt32("80000000", 16)
$ES_SYSTEM_REQUIRED = [uint32]0x00000001
try {
    while ($true) {
        $task = Get-ScheduledTask -TaskName "PetabyteNode" -ErrorAction SilentlyContinue
        $active = $false
        if ($task -and $task.State -eq "Running") {
            wsl.exe -d $Distro -u root -- systemctl is-active --quiet petabyte-agent
            $active = ($LASTEXITCODE -eq 0)
        }
        if ($active) {
            [void][PetabytePower.Native]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
        } else {
            [void][PetabytePower.Native]::SetThreadExecutionState($ES_CONTINUOUS)
        }
        Start-Sleep -Seconds 30
    }
} finally {
    [void][PetabytePower.Native]::SetThreadExecutionState($ES_CONTINUOUS)
}
'@ | Set-Content -Path $KeepAwakeScriptPath -Encoding UTF8
    try {
        $userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $keepAction = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$KeepAwakeScriptPath`" -Distro `"$Distro`""
        $keepTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
        $keepPrincipal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest
        $keepSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
        Register-ScheduledTask -TaskName "PetabyteKeepAwake" -Action $keepAction -Trigger $keepTrigger `
            -Principal $keepPrincipal -Settings $keepSettings -Force | Out-Null
        Start-ScheduledTask -TaskName "PetabyteKeepAwake"
    } catch {
        Write-Host "WARNING: could not register the optional keep-awake helper: $_" -ForegroundColor Yellow
    }
} else {
    try { Stop-ScheduledTask -TaskName "PetabyteKeepAwake" -ErrorAction SilentlyContinue } catch {}
    try { Unregister-ScheduledTask -TaskName "PetabyteKeepAwake" -Confirm:$false -ErrorAction SilentlyContinue } catch {}
}

Write-Host ""
Write-Host "node online (inside WSL2)." -ForegroundColor Green
Write-Host "A window titled 'Petabyte node - keep open' is now running: that IS your node." -ForegroundColor Yellow
Write-Host "Keep it open (minimise it is fine). Closing it takes your GPU offline. It reopens at every logon." -ForegroundColor Yellow
Write-Host "Node offline later? Don't reinstall - resume: irm $($env:PETABYTE_API_URL)/manage.ps1 | iex  (choose 1 = Resume)" -ForegroundColor Yellow
Write-Host "  status: wsl -d $Distro -u root -- systemctl status petabyte-agent"
Write-Host "  logs:   wsl -d $Distro -u root -- journalctl -u petabyte-agent -f"
Write-Host ""
Write-Host "Low-commitment controls (manage.ps1):" -ForegroundColor Cyan
Write-Host "  pause:     `$env:PETABYTE_ACTION='pause';     irm $($env:PETABYTE_API_URL)/manage.ps1 | iex"
Write-Host "  resume:    `$env:PETABYTE_ACTION='resume';    irm $($env:PETABYTE_API_URL)/manage.ps1 | iex"
Write-Host "  uninstall: `$env:PETABYTE_ACTION='uninstall'; irm $($env:PETABYTE_API_URL)/manage.ps1 | iex"
Write-Host "  (uninstall removes the agent + distro, and if WSL wasn't already on your PC, disables it too.)"
