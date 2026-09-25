#!/usr/bin/env bash
# Petabyte one-line node installer (Ubuntu/Debian).
#   PETABYTE_API_URL=https://petabyte.market PETABYTE_API_KEY=pk_your_node_key \
#     bash <(curl -fsSL https://petabyte.market/install.sh)
# The /install page generates this exact command with your key already filled in.
# PRICE_PER_HOUR is optional: leave it unset and the node auto-prices from its GPU's
# benchmark; set it (e.g. PRICE_PER_HOUR=1.5) to pin your own rate.
set -euo pipefail
: "${PETABYTE_API_URL:?set PETABYTE_API_URL}"
: "${PETABYTE_API_KEY:?set PETABYTE_API_KEY (create one on the /install page)}"
REPO="${PETABYTE_REPO:-https://github.com/petabyte-cloud/petabyte.git}"
SUBDIR="${PETABYTE_AGENT_SUBDIR:-lumaris_agent}"
APP=/opt/petabyte-agent
ENVF=/etc/petabyte/agent.env
KEYF=/etc/petabyte/agent_ed25519.key
mkdir -p /etc/petabyte  # BUGFIX: create before egress-firewall block writes into it

echo "==> installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv git curl ca-certificates rsync wireguard-tools

echo "==> installing Docker (sandbox runtime)"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "==> installing nvidia-container-toolkit (GPU in containers; native + WSL2)"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -y && apt-get install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker && systemctl restart docker || true
elif command -v rocminfo >/dev/null 2>&1 || command -v rocm-smi >/dev/null 2>&1; then
  echo "==> AMD ROCm GPU detected; containers get the GPU via /dev/kfd + /dev/dri (no extra toolkit)"
  # ROCm exposes the GPU through device files, not a container runtime shim, so no
  # nvidia-container-toolkit equivalent is needed. Just let the agent user reach the GPU groups.
  usermod -aG video,render "${SUDO_USER:-$(whoami)}" 2>/dev/null || true
fi

# --- Kata Containers: VM-per-container isolation (DEFAULT on KVM-capable nodes) ----
# Kata runs each buyer container in its OWN hardware VM (KVM) — the strongest boundary against a
# buyer escaping onto the seller host. Installed BY DEFAULT when this node has /dev/kvm (bare-metal
# or nested-virt; NOT standard cloud droplets, which transparently stay on gVisor). Opt out with
# PETABYTE_KATA=false. GPU-in-VM also needs IOMMU+VFIO the operator sets up (then set
# AGENT_KATA_GPU=true); until then GPU jobs safely use gVisor.
# Supply chain: a PINNED release checked against a PINNED SHA-256 (GitHub's asset digest) BEFORE it
# is unpacked, unpacked into a scratch dir on disk (never straight into /), and only opt/kata is
# moved into place. Fail-safe: no KVM, low disk, a download/checksum/unpack failure, or an
# unparseable daemon.json all leave the node on gVisor/default — never blocking the install and
# never overwriting docker's config.
KATA_VERSION="3.20.0"
KATA_SHA256="bc6bc429cbf28199193cff5dea449991153842971be8ab95f07b954e4baecef5"
KATA_OK=0
if [ "${PETABYTE_KATA:-true}" != "false" ]; then
  if [ ! -e /dev/kvm ]; then
    echo "==> no /dev/kvm on this node — Kata skipped (normal on cloud droplets); using gVisor/default"
  elif [ "$(df -Pk /var/tmp | awk 'NR==2{print $4}')" -lt 6000000 ] 2>/dev/null; then
    echo "!! under 6 GB free on /var/tmp — Kata skipped (~930 MB download, ~3 GB unpacked); using gVisor/default"
  else
    echo "==> installing Kata Containers ${KATA_VERSION} (default; VM per container; ~930 MB download)"
    KTMP="$(mktemp -d /var/tmp/pbkata.XXXXXX)"
    KURL="https://github.com/kata-containers/kata-containers/releases/download/${KATA_VERSION}/kata-static-${KATA_VERSION}-amd64.tar.xz"
    if curl -fL --retry 3 -o "$KTMP/kata.tar.xz" "$KURL" \
       && echo "${KATA_SHA256}  $KTMP/kata.tar.xz" | sha256sum -c --status - \
       && mkdir -p "$KTMP/x" && tar -xJf "$KTMP/kata.tar.xz" -C "$KTMP/x" \
       && [ -x "$KTMP/x/opt/kata/bin/containerd-shim-kata-v2" ] \
       && rm -rf /opt/kata && mv "$KTMP/x/opt/kata" /opt/kata; then
      ln -sf /opt/kata/bin/containerd-shim-kata-v2 /usr/local/bin/containerd-shim-kata-v2 || true
      # Merge the kata runtime into docker's config; ABORT (never clobber) if it doesn't parse, so
      # the nvidia runtime and any hardening already in daemon.json can't be silently dropped.
      if python3 - <<'KATAPY'
import json, os, sys
p = "/etc/docker/daemon.json"; d = {}
if os.path.exists(p):
    try:
        d = json.load(open(p))
    except Exception:
        sys.exit(1)
d.setdefault("runtimes", {})["kata"] = {"runtimeType": "io.containerd.kata.v2"}
tmp = p + ".pbtmp"
open(tmp, "w").write(json.dumps(d, indent=2))
os.replace(tmp, p)
KATAPY
      then
        systemctl restart docker 2>/dev/null || true
        KATA_OK=1
        echo "==> Kata installed — buyer containers will run under --runtime kata"
      else
        echo "!! /etc/docker/daemon.json is not valid JSON — Kata skipped, docker config left untouched"
      fi
    else
      echo "!! Kata download/checksum/unpack failed — keeping the existing runtime (gVisor/default)"
    fi
    rm -rf "$KTMP"
  fi
fi

# --- Buyer egress VPN (hide the seller IP) -----------------------------------------
# When this node is enrolled in the egress VPN (PB_EGRESS_* set in /etc/petabyte/agent.env by the
# API), the agent routes every buyer container's INTERNET traffic through the Petabyte gateway as a
# WireGuard tunnel, so the seller's IP never reaches the internet on a buyer's behalf (fail-closed:
# no tunnel -> the buyer gets no internet, never the seller NIC). The agent brings the tunnel up
# lazily on the first job (lumaris_agent/egress_vpn.py); we just make sure the tools are present and
# print the node's egress public key so an operator/the API can add it as a gateway peer.
if grep -qs '^PB_EGRESS_GATEWAY_ENDPOINT=' /etc/petabyte/agent.env 2>/dev/null; then
  apt-get install -y wireguard-tools >/dev/null 2>&1 || true
  echo "==> egress VPN enrolled; node egress pubkey:"
  ( cd "${AGENT_DIR:-/opt/petabyte-agent}" 2>/dev/null && .venv/bin/python -c \
      "import egress_vpn; print(egress_vpn.node_pubkey())" 2>/dev/null ) || true
fi

# --- Container egress lockdown (protects the SELLER) -------------------------------
# A buyer's job runs on the seller's machine and network. Left open, it can reach the
# cloud metadata endpoint (169.254.169.254 -> steal the host's IAM/cloud credentials)
# and the seller's own LAN (router admin, NAS, other hosts). We DROP both from
# containers, on the DOCKER-USER chain so it governs all container-forwarded traffic.
# The job cannot remove these rules: the agent runs every job with --cap-drop ALL, so
# it has neither NET_ADMIN nor NET_RAW. Toggle with PETABYTE_LOCKDOWN_EGRESS=false.
if [ "${PETABYTE_LOCKDOWN_EGRESS:-true}" = "true" ]; then
  echo "==> installing container egress firewall (block cloud-metadata + LAN)"
  apt-get install -y iptables >/dev/null 2>&1 || true
  install -m 0755 /dev/stdin /etc/petabyte/egress-firewall.sh <<'FW'
#!/usr/bin/env bash
# Petabyte container egress lockdown. Idempotent: rebuilds a dedicated PB-EGRESS chain
# jumped from DOCKER-USER. Re-applied on boot (docker flushes DOCKER-USER on restart).
set -euo pipefail
command -v iptables >/dev/null || exit 0
iptables -N PB-EGRESS 2>/dev/null || iptables -F PB-EGRESS
# let a container's OWN replies back in (only NEW outbound to the bad nets is dropped)
iptables -A PB-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# cloud instance metadata service (IMDS) — IAM/credential theft
iptables -A PB-EGRESS -d 169.254.0.0/16 -j DROP
# Allow DNS (port 53) to the host's configured upstream resolver(s) BEFORE the LAN drop below.
# A droplet's resolver is frequently an RFC-1918 / 10.x address; without this the 10.0.0.0/8 drop
# blackholes it and NO container can resolve a name — every game-server download, pip install and
# model fetch fails with a DNS timeout. Permitting DNS to the resolver is not LAN pivoting; every
# other LAN target below still gets dropped.
for _R in $(awk '/^nameserver/{print $2}' /run/systemd/resolve/resolv.conf /etc/resolv.conf 2>/dev/null | sort -u); do
  case "$_R" in 127.*) continue ;; esac
  iptables -A PB-EGRESS -d "$_R" -p udp --dport 53 -j RETURN
  iptables -A PB-EGRESS -d "$_R" -p tcp --dport 53 -j RETURN
done
# the seller's own private LAN — pivot / attack of router, NAS, other hosts
iptables -A PB-EGRESS -d 10.0.0.0/8      -j DROP
iptables -A PB-EGRESS -d 192.168.0.0/16  -j DROP
# Abuse ports (protect the SELLER's IP reputation and legal standing): a buyer's job runs behind
# the host's home IP, so if it sends spam or scans/attacks over Windows-file-sharing ports it is
# the SELLER who gets the abuse complaint / block. Drop outbound SMTP (spam) and NetBIOS/SMB
# (worms, lateral movement). Toggle with PETABYTE_BLOCK_ABUSE_PORTS=false; a workload that
# genuinely needs to send mail should use an authenticated API, not raw SMTP.
if [ "${PETABYTE_BLOCK_ABUSE_PORTS:-true}" = "true" ]; then
  for _p in 25 465 587 137 138 139 445; do
    iptables -A PB-EGRESS -p tcp --dport "$_p" -j DROP
    iptables -A PB-EGRESS -p udp --dport "$_p" -j DROP
  done
fi
# 172.16.0.0/12 is NOT blocked wholesale — Docker's own bridge networks live there and blocking
# them breaks container NAT. Instead, DROP the host's REAL LAN subnets, discovered dynamically from
# the non-Docker network interfaces, so a corporate/home LAN in 172.16-31.x (or any private range)
# is protected without breaking Docker. This closes the residual where a buyer container could
# reach a seller whose LAN happens to sit in 172.16/12.
for _cidr in $(ip -o -4 addr show 2>/dev/null \
      | awk '$2 !~ /^(docker|br-|veth|lo|wg|tun|tap|cni|flannel|kube)/ {print $4}'); do
  case "$_cidr" in
    10.*|192.168.*) : ;;                        # already dropped wholesale above
    172.1[6-9].*|172.2[0-9].*|172.3[01].*)      # a real LAN inside Docker's range — protect it
      iptables -A PB-EGRESS -d "$_cidr" -j DROP ;;
    169.254.*) : ;;                             # link-local already dropped
    *) : ;;                                     # public IP on the host NIC — leave it (that's the internet)
  esac
done
# --- Outbound abuse caps (OPT-IN) ---------------------------------------------------------
# A buyer's job runs behind the SELLER's IP, so a compromised/malicious job can use it for
# port-scanning or connection flooding and it is the seller who gets the abuse complaint. These
# cap NEW outbound TCP connections per container. Default OFF (no change) — a seller who wants the
# protection sets them in /etc/petabyte/egress.env; the buyer's job then runs, just rate-limited:
#     PB_EGRESS_MAX_CONNS   concurrent outbound TCP connections per container (e.g. 500)
#     PB_EGRESS_CONN_RATE   new outbound TCP connections/sec per container   (e.g. 50)
[ -r /etc/petabyte/egress.env ] && . /etc/petabyte/egress.env
if [ -n "${PB_EGRESS_MAX_CONNS:-}" ]; then
  iptables -A PB-EGRESS -p tcp --syn -m connlimit --connlimit-above "$PB_EGRESS_MAX_CONNS" --connlimit-mask 32 -j DROP 2>/dev/null || true
fi
if [ -n "${PB_EGRESS_CONN_RATE:-}" ]; then
  iptables -A PB-EGRESS -p tcp --syn -m hashlimit --hashlimit-name pb_egress \
    --hashlimit-mode srcip --hashlimit-above "${PB_EGRESS_CONN_RATE}/sec" \
    --hashlimit-burst "$(( PB_EGRESS_CONN_RATE * 4 ))" -j DROP 2>/dev/null || true
fi
iptables -A PB-EGRESS -j RETURN
iptables -C DOCKER-USER -j PB-EGRESS 2>/dev/null || iptables -I DOCKER-USER -j PB-EGRESS
# Host-service protection: a job container must not open NEW connections to the HOST itself
# (SSH on the bridge gateway 172.17.0.1:22, the agent's local dashboard, any co-tenant service
# bound to the host). Drop NEW inbound arriving on Docker bridge interfaces; a container's own
# outbound replies (ESTABLISHED) and its NAT'd internet traffic (FORWARD, untouched here) still
# work, so `limited` templates keep pulling models. Toggle with PETABYTE_LOCKDOWN_HOST=false.
if [ "${PETABYTE_LOCKDOWN_HOST:-true}" = "true" ]; then
  iptables -N PB-HOST-IN 2>/dev/null || iptables -F PB-HOST-IN
  iptables -A PB-HOST-IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  iptables -A PB-HOST-IN -p icmp -j RETURN
  iptables -A PB-HOST-IN -m conntrack --ctstate NEW -j DROP
  iptables -A PB-HOST-IN -j RETURN
  # jump for every Docker bridge (docker0 + user-defined per-job pb-net-* bridges = br-*)
  for _if in docker0; do
    iptables -C INPUT -i "$_if" -j PB-HOST-IN 2>/dev/null || iptables -I INPUT -i "$_if" -j PB-HOST-IN
  done
  # match all bridge interfaces by prefix so per-job pb-net-* networks are covered too
  iptables -C INPUT -i br+ -j PB-HOST-IN 2>/dev/null || iptables -I INPUT -i br+ -j PB-HOST-IN
fi
# IPv6 metadata (best-effort; AWS IMDS over v6)
if command -v ip6tables >/dev/null; then
  ip6tables -N PB-EGRESS 2>/dev/null || ip6tables -F PB-EGRESS
  ip6tables -A PB-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
  ip6tables -A PB-EGRESS -d fd00:ec2::254/128 -j DROP
  ip6tables -A PB-EGRESS -j RETURN
  ip6tables -C DOCKER-USER -j PB-EGRESS 2>/dev/null || ip6tables -I DOCKER-USER -j PB-EGRESS
fi
echo "petabyte: container egress lockdown applied"
FW
  cat > /etc/systemd/system/petabyte-egress.service <<'UNIT'
[Unit]
Description=Petabyte container egress lockdown (block cloud-metadata + LAN)
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/etc/petabyte/egress-firewall.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now petabyte-egress.service || /etc/petabyte/egress-firewall.sh || true
fi

# --- Ephemeral encrypted job scratch (OPT-IN) ---------------------------------------------
# Stage buyer code/results on a dm-crypt volume whose key is a fresh RANDOM value each boot, so the
# buyer's plaintext is encrypted-at-rest and cryptographically UNRECOVERABLE after any reboot/
# unmount. Points PB_JOB_SCRATCH_DIR at it and sets PB_JOB_SCRATCH_REQUIRE_RAM=1 so a job FAILS
# rather than ever landing plaintext on the bare disk. Enable with PETABYTE_ENCRYPTED_SCRATCH=true.
if [ "${PETABYTE_ENCRYPTED_SCRATCH:-false}" = "true" ]; then
  echo "==> installing ephemeral encrypted job scratch"
  apt-get install -y cryptsetup >/dev/null 2>&1 || true
  _SZ="${PETABYTE_SCRATCH_SIZE:-16G}"
  install -m 0755 /dev/stdin /usr/local/sbin/pb-scratch-up <<SCUP
#!/usr/bin/env bash
set -euo pipefail
mkdir -p /var/lib/petabyte /var/lib/petabyte/scratch
mountpoint -q /var/lib/petabyte/scratch && exit 0
[ -f /var/lib/petabyte/scratch.img ] || truncate -s "$_SZ" /var/lib/petabyte/scratch.img
LOOP=\$(losetup --show -f /var/lib/petabyte/scratch.img)
cryptsetup open --type plain --key-file /dev/urandom "\$LOOP" pbscratch   # RANDOM key, never stored
mkfs.ext4 -q /dev/mapper/pbscratch
mount -o noatime /dev/mapper/pbscratch /var/lib/petabyte/scratch
chmod 700 /var/lib/petabyte/scratch
SCUP
  install -m 0755 /dev/stdin /usr/local/sbin/pb-scratch-down <<'SCDN'
#!/usr/bin/env bash
umount /var/lib/petabyte/scratch 2>/dev/null || true
cryptsetup close pbscratch 2>/dev/null || true
losetup -D 2>/dev/null || true
SCDN
  cat > /etc/systemd/system/petabyte-scratch.service <<'SCUNIT'
[Unit]
Description=Petabyte ephemeral encrypted job scratch
After=local-fs.target
Before=petabyte-agent.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/pb-scratch-up
ExecStop=/usr/local/sbin/pb-scratch-down
[Install]
WantedBy=multi-user.target
SCUNIT
  systemctl daemon-reload
  systemctl enable --now petabyte-scratch.service || true
  # point the agent at it, and refuse to ever fall back to the plaintext disk
  grep -q "^PB_JOB_SCRATCH_DIR=" "$ENVF" 2>/dev/null || echo "PB_JOB_SCRATCH_DIR=/var/lib/petabyte/scratch" >> "$ENVF"
  grep -q "^PB_JOB_SCRATCH_REQUIRE_RAM=" "$ENVF" 2>/dev/null || echo "PB_JOB_SCRATCH_REQUIRE_RAM=1" >> "$ENVF"
fi

echo "==> fetching agent"
mkdir -p "$APP" /etc/petabyte

# Pin the release verification PUBLIC key. The API substitutes it into this script at download
# time; update.sh then requires every future agent bundle to be signed by the matching offline
# key before applying it. If it's empty (unset on the server) we remove the file so auto-update
# stays OFF (fail-closed) rather than trusting TLS alone for code that runs as root.
cat > /etc/petabyte/release_ed25519.pub <<'PBPUBKEY_EOF'
__PETABYTE_RELEASE_PUBKEY_PEM__
PBPUBKEY_EOF
if ! grep -q "BEGIN PUBLIC KEY" /etc/petabyte/release_ed25519.pub 2>/dev/null; then
  rm -f /etc/petabyte/release_ed25519.pub
fi

if [ -f "./task_fetcher.py" ]; then
  cp -r ./* "$APP"/                          # running from inside lumaris_agent/ locally
else
  TMP=$(mktemp -d)
  # Preferred: fetch the agent bundle from OUR server (no GitHub needed => works when the
  # repo is private, and no host ever holds a git credential).
  if curl -fsSL "$PETABYTE_API_URL/agent.tar.gz" -o "$TMP/agent.tar.gz" 2>/dev/null \
     && tar -xzf "$TMP/agent.tar.gz" -C "$TMP" 2>/dev/null && [ -d "$TMP/lumaris_agent" ]; then
    cp -r "$TMP/lumaris_agent/." "$APP"/
  else
    # Fallback: clone the repo (needs access if the repo is private).
    echo "==> agent bundle unavailable, falling back to git clone"
    git clone --depth 1 "$REPO" "$TMP/repo"
    cp -r "$TMP/repo/$SUBDIR/." "$APP"/
  fi
  rm -rf "$TMP"
fi
cd "$APP"
python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -q -r requirements.txt
# On a GPU node the FP16 authenticity benchmark (task_fetcher._measure_fp16_tflops) needs torch to
# run the seeded GEMM proof; without it the benchmark returns 0, the node never becomes
# hardware-verified, and it is silently NEVER listed. torch is intentionally out of requirements.txt
# (CPU-only agents don't need a ~2 GB CUDA wheel), so install it here only when a GPU is present.
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "==> installing torch for the GPU authenticity benchmark (GPU node)"
  .venv/bin/pip install -q torch numpy || echo "WARN: torch install failed; node will register but stay UNVERIFIED until torch is present"
elif command -v rocminfo >/dev/null 2>&1 || command -v rocm-smi >/dev/null 2>&1; then
  echo "==> installing ROCm torch for the GPU authenticity benchmark (AMD node)"
  .venv/bin/pip install -q --index-url https://download.pytorch.org/whl/rocm6.2 torch numpy || echo "WARN: rocm torch install failed; node stays UNVERIFIED until torch is present"
fi

# VRAM-wipe image. The agent REFUSES every GPU job unless it can first zero the previous tenant's
# VRAM, and the wipe only runs from a LOCALLY cached CUDA image (it never pulls mid-job) — so a
# fresh node used to refuse 100% of GPU rentals. Cache it now (one-time, ~3-4 GB). Blackwell
# cards (compute capability >= 10: RTX 50-series, B200) need the CUDA 12.8 build.
if command -v nvidia-smi >/dev/null 2>&1 && [ "${AGENT_ALLOW_UNVERIFIED_VRAM:-false}" != "true" ]; then
  _cc=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1 || true)
  if [ "${_cc:-0}" -ge 10 ] 2>/dev/null; then _wipe=pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime
  else _wipe=pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime; fi
  echo "==> caching the VRAM-wipe image $_wipe (one-time; GPU jobs are refused without it)"
  docker pull "$_wipe" >/dev/null || echo "WARN: could not cache $_wipe; GPU jobs will be refused until: docker pull $_wipe"
fi

echo "==> registering + attesting this node"
PETABYTE_AGENT_KEY="$KEYF" AGENT_ENV="$ENVF" \
  PETABYTE_API_URL="$PETABYTE_API_URL" PETABYTE_API_KEY="$PETABYTE_API_KEY" \
  PRICE_PER_HOUR="${PRICE_PER_HOUR:-}" UNITS="${UNITS:-1}" GPU_MODEL="${GPU_MODEL:-}" \
  PROVIDER="${PROVIDER:-}" \
  .venv/bin/python provision.py

# Kata: turn the runtime on for this node when it was installed above (provision.py preserves
# operator-set lines, so this survives re-provisioning). GPU jobs stay on gVisor until the operator
# confirms VFIO passthrough with AGENT_KATA_GPU=true.
if [ "${KATA_OK:-0}" = "1" ]; then
  grep -q '^AGENT_RUNTIME=' "$ENVF" 2>/dev/null || echo 'AGENT_RUNTIME=kata' >> "$ENVF"
fi

# Idle mining. Default ON, seeding the payout address the server baked in (__PB_DOGE_ADD__ =
# MINING_DOGE_ADDRESS). A DISTRIBUTION build (e.g. the agent bundled inside another installer)
# forces it OFF by exporting PETABYTE_IDLE_MINING=false before running this script: the node then
# seeds PETABYTE_IDLE_MINING=false and NO payout address at all, so it can never mine. Always
# yields to paid work; toggle any time with: petabyte agent mining disable
_mine="${PETABYTE_IDLE_MINING:-true}"
# An explicit opt-out must also override settings left by an older installation.
if [ "$_mine" = "false" ]; then
  sed -i '/^PETABYTE_IDLE_MINING=/d; /^DOGE_ADD=/d' "$ENVF"
fi
_doge=__PB_DOGE_ADD__
[ "$_mine" = "true" ] || _doge=""        # mining off => never seed a mining payout address
for setting in "PETABYTE_IDLE_MINING=$_mine" "DOGE_ADD=$_doge"; do
  key=${setting%%=*}
  grep -q "^${key}=" "$ENVF" || printf '%s\n' "$setting" >> "$ENVF"
done

# Selling schedule: the node-local hours this machine offers its GPU for sale. The /install page
# bakes the seller's pick into this env (default 09:00-17:00); a bare install defaults to always-on
# so behaviour is unchanged. Node-local time is authoritative — the agent reports selling_now and
# the platform refuses NEW reservations outside the window; it never interrupts a running rental.
grep -q "^PETABYTE_SELL_SCHEDULE=" "$ENVF" || \
  printf 'PETABYTE_SELL_SCHEDULE=%s\n' "${PETABYTE_SELL_SCHEDULE:-always}" >> "$ENVF"

if [ "$_mine" = "false" ]; then
  echo "==> idle mining is OFF"
elif grep -q "^DOGE_ADD=." "$ENVF"; then
  echo "==> idle mining is ON by default, paying the payout address configured for this node"
  echo "    in $ENVF (build the GPU miner image to start). Disable: petabyte agent mining disable"
else
  echo "==> idle mining is ON by default (inert until you set DOGE_ADD in $ENVF and build the"
  echo "    GPU miner image); it pays only YOUR wallet. Disable: petabyte agent mining disable"
fi

echo "==> starting service"
cp "$APP/petabyte-agent.service" /etc/systemd/system/petabyte-agent.service
systemctl daemon-reload
systemctl enable petabyte-agent
systemctl restart petabyte-agent

# Auto-update: ON by default. The channel is Ed25519-signed and FAIL-CLOSED — update.sh
# applies a bundle only if it verifies against the pinned release key at
# /etc/petabyte/release_ed25519.pub (embedded by /install.sh over TLS); an unsigned,
# tampered, or unavailable bundle is refused and skipped, never applied. Opt out any
# time with PETABYTE_AUTO_UPDATE=false (or update manually with `petabyte update`).
if [ "${PETABYTE_AUTO_UPDATE:-true}" = "true" ] && [ -f "$APP/petabyte-agent-update.service" ]; then
  chmod +x "$APP/update.sh" 2>/dev/null || true
  cp "$APP/petabyte-agent-update.service" /etc/systemd/system/petabyte-agent-update.service
  cp "$APP/petabyte-agent-update.timer" /etc/systemd/system/petabyte-agent-update.timer
  systemctl daemon-reload
  systemctl enable --now petabyte-agent-update.timer
  echo "==> auto-update ENABLED (petabyte-agent-update.timer, every 6h)."
  echo "    Updates are signature-verified against a pinned key (see update.sh)."
else
  echo "==> auto-update OFF (PETABYTE_AUTO_UPDATE=false). Update manually with: petabyte update"
  echo "    Re-enable by unsetting PETABYTE_AUTO_UPDATE (default) or setting it =true."
fi

# One-time consent: may Petabyte support apply fixes SIGNED for this machine (fix-runner.sh)?
# PETABYTE_REMOTE_FIXES=true|false skips the question (non-interactive installs default to NO).
ALLOW_FIXES="${PETABYTE_REMOTE_FIXES:-}"
if [ -z "$ALLOW_FIXES" ] && { : < /dev/tty; } 2>/dev/null; then
  printf "Allow Petabyte support to apply fixes to this machine if its GPU setup breaks?
  (signed for this machine only, run once, never during a job or rental, reported back) [y/N] " > /dev/tty
  read -r _ans < /dev/tty || _ans=""
  case "$_ans" in y|Y|yes|YES) ALLOW_FIXES=true ;; esac
fi
if [ "$ALLOW_FIXES" = "true" ]; then
  "$APP/.venv/bin/python" "$APP/main.py" fixes enable --yes || echo "support fixes could not be enabled"
else
  echo "==> support fixes OFF (turn on any time: sudo $APP/.venv/bin/python $APP/main.py fixes enable)"
fi
echo "==> diagnostics: if this machine's GPU checks fail, the agent sends Petabyte support a report"
echo "    (GPU driver + Docker config, GPU errors, recent agent logs; keys/emails/addresses masked,"
echo "    never your files) so we can fix it. Opt out: PB_SHARE_DIAGNOSTICS=false in /etc/petabyte/agent.env"
echo "✅ node online. logs: journalctl -u petabyte-agent -f"
