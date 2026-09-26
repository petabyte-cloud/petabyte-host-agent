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
#   5) Registers a hidden Scheduled Task so the node comes online at logon.
#
# GPU note: NVIDIA's Windows driver exposes CUDA to WSL2 automatically (no driver
# install inside Linux). install.sh adds nvidia-container-toolkit so Docker jobs
# can use --gpus all.

$ErrorActionPreference = "Stop"
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

$StateDir = Join-Path $env:ProgramData "Petabyte"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$StateFile = Join-Path $StateDir "install-state.json"
$prev = $null
if (Test-Path $StateFile) { try { $prev = Get-Content $StateFile -Raw | ConvertFrom-Json } catch {} }

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
   distro = $Distro; installedAt = (Get-Date).ToString("o") } |
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
}

# --- 3. run the standard Linux installer inside WSL -------------------------
Write-Host "==> installing the Petabyte agent inside $Distro"
$sh = @(
    "command -v curl >/dev/null || { apt-get update -y && apt-get install -y curl ca-certificates; }",
    $keep,
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
