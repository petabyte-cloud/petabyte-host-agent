#!/usr/bin/env python3
"""Provision this machine as a Petabyte node using ONLY an API key (no creds):
detect hardware, register the spec, attest it (Ed25519), and write the agent env.

The node authenticates every call with X-API-KEY, so no username/password ever
lives on the machine. Create the key on the /install page (that button also makes
your account a seller). The key's account must already be a seller.

Env:
  PETABYTE_API_URL, PETABYTE_API_KEY               (required)
  PRICE_PER_HOUR (unset => auto-priced from this GPU's benchmark), UNITS (1), MAX_HOURS (24)
  GPU_MODEL/GPU_COUNT/VRAM_GB                       (override auto-detect)
  AGENT_ENV (default /etc/petabyte/agent.env), PETABYTE_AGENT_KEY
"""
import base64
import os
import re
import socket
import subprocess
import time

import httpx
import crypto
import cli_ui


# The four keys provisioning owns and rewrites; everything else in agent.env belongs to the
# operator and survives (see _preserved_env_lines).
MANAGED_ENV_KEYS = ("PETABYTE_API_URL", "PETABYTE_API_KEY", "PETABYTE_SPEC_ID",
                    "PETABYTE_AGENT_KEY",
                    # Egress VPN config the API assigns at registration (rewritten each provision).
                    "PB_EGRESS_GATEWAY_PUBKEY", "PB_EGRESS_GATEWAY_ENDPOINT", "PB_EGRESS_ADDR")


def _preserved_env_lines(env_path: str) -> list[str]:
    """Operator-set lines in an existing agent.env that re-provisioning must not destroy.

    Keeps comments and blank-free settings verbatim; drops only the keys provisioning rewrites,
    so a re-provision cannot end up with the same variable twice."""
    try:
        with open(env_path) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() in MANAGED_ENV_KEYS:
            continue
        kept.append(line.rstrip())
    return kept


def detect():
    cpu = os.cpu_count() or 1
    try:
        kb = int(next(l for l in open("/proc/meminfo") if l.startswith("MemTotal")).split()[1])
        ram = max(1, kb // 1024 // 1024)
    except Exception:
        ram = 1
    gpu_model = os.getenv("GPU_MODEL")
    gpu_count = int(os.getenv("GPU_COUNT", "0"))
    vram = int(os.getenv("VRAM_GB", "0"))
    if not gpu_model:
        rows = nvidia_gpu_rows() or amd_gpu_rows()
        if rows:
            # HONESTY (mixed-GPU host): taking rows[0]'s model with len(rows) as the count listed a
            # box holding 1x H100 + 3x GT710 as "4x H100" — a buyer scheduled onto rank 1 gets a
            # GT710 at H100 prices. Advertise only the LARGEST homogeneous group, so the model and
            # the count always describe the same silicon.
            names = [n for n, _m in rows]
            gpu_model = max(names, key=lambda n: (names.count(n), -names.index(n)))
            group = [r for r in rows if r[0] == gpu_model]
            gpu_count = len(group)
            vram = int(float(group[0][1])) // 1024
            if len(group) < len(rows):
                print(f"Mixed GPUs detected ({', '.join(names)}); listing only the "
                      f"{gpu_count}x {gpu_model} this host can honestly offer.")
    return cpu, ram, gpu_model, gpu_count, vram


def nvidia_gpu_rows() -> list:
    """[(model, vram_mib)] for every NVIDIA GPU nvidia-smi can see on THIS machine. Empty when
    there is no NVIDIA GPU or the driver/tool is missing. Never raises."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            text=True, timeout=15, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return []
    rows = []
    for ln in out.splitlines():
        parts = ln.split(",")
        if len(parts) >= 2 and parts[0].strip():
            rows.append((parts[0].strip(), parts[1].strip()))
    return rows


def nvidia_gpu_names() -> list:
    """The NVIDIA GPUs actually present on THIS machine, per nvidia-smi. Empty when there is no
    NVIDIA GPU or the driver/tool is missing. Never raises."""
    return [n for n, _m in nvidia_gpu_rows()]


def amd_gpu_rows() -> list:
    """[(model, vram_mib)] for every AMD GPU rocm-smi/rocminfo can see. Empty if none / tools
    missing. Never raises. Mirrors nvidia_gpu_rows so detect() is vendor-agnostic."""
    import json
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"],
            text=True, timeout=15, stderr=subprocess.DEVNULL)
        data = json.loads(out)
    except Exception:
        try:
            ro = subprocess.check_output(["rocminfo"], text=True, timeout=15,
                                         stderr=subprocess.DEVNULL)
        except Exception:
            return []
        rows = []
        for m in re.finditer(r"Marketing Name:\s*(.+)", ro):
            name = m.group(1).strip()
            if name and "cpu" not in name.lower():
                rows.append((name, "0"))
        return rows
    rows = []
    for card, info in (data.items() if isinstance(data, dict) else []):
        if not str(card).lower().startswith("card") or not isinstance(info, dict):
            continue
        name = (info.get("Card series") or info.get("Card model")
                or info.get("Card SKU") or "AMD GPU").strip()
        vram_mib = "0"
        for k, v in info.items():
            if "vram total memory" in k.lower():
                try:
                    vram_mib = str(int(int(v) / (1024 * 1024)))   # bytes -> MiB
                except Exception:
                    pass
        rows.append((name, vram_mib))
    return rows


_VENDOR_WORDS = ("nvidia", "geforce", "tesla", "quadro")


def _norm_gpu(name) -> str:
    """Normalise a GPU model string for comparison: lowercase, punctuation to spaces (so
    'A100-SXM4-40GB' and 'A100 SXM4 40GB' are one thing), and drop the vendor/marketing words
    nvidia-smi and sellers write inconsistently ('NVIDIA GeForce RTX 4090' vs 'RTX 4090')."""
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    for word in _VENDOR_WORDS:
        s = re.sub(rf"\b{word}\b", " ", s)
    return " ".join(s.split())


def _gpu_matches(declared, present) -> bool:
    """True when `declared` plausibly names the same silicon as the nvidia-smi string `present`.

    Substring in either direction, so a seller may legitimately write the short 'RTX 4090' for
    nvidia-smi's 'NVIDIA GeForce RTX 4090'. The lie that pays is claiming a BETTER card, and
    'h100' is a substring of neither direction of 'geforce rtx 3060'. A declaration that
    normalises to nothing (e.g. GPU_MODEL='NVIDIA') never matches — otherwise the empty string
    would be a substring of every GPU on the host."""
    d, p = _norm_gpu(declared), _norm_gpu(present)
    if not d or not p:
        return False
    return d in p or p in d


def _unverified_gpu_override() -> bool:
    """Is the ALLOW_UNVERIFIED_GPU bypass actually permitted on this box?

    The bypass skips nvidia-smi entirely, so it is precisely the switch a seller would flip to
    list an H100 they do not own. It exists only for CI/dev harnesses that fake a GPU, so honour
    it ONLY when the operator has explicitly marked this environment as development/test. An
    UNSET environment counts as production (fail-closed): a real seller machine never sets one, so
    in the field the override simply does not exist. (Deliberately reads DEPLOYMENT_ENVIRONMENT /
    ENVIRONMENT and not AGENT_ENV — in this module AGENT_ENV is the agent.env FILE PATH.)"""
    if os.getenv("ALLOW_UNVERIFIED_GPU", "false").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    env = (os.getenv("DEPLOYMENT_ENVIRONMENT") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if env in ("development", "dev", "test", "testing", "ci", "local"):
        return True
    print(f"ALLOW_UNVERIFIED_GPU is set but this node is not marked development/test "
          f"(ENVIRONMENT={env or 'unset'}) — ignoring the override and verifying against "
          f"nvidia-smi.")
    return False


def verify_declared_gpu(gpu_model, gpu_count):
    """Refuse to register a GPU this machine cannot actually show.

    GPU model/count/VRAM come from env vars (GPU_MODEL/GPU_COUNT/VRAM_GB) or nvidia-smi. A seller
    could set GPU_MODEL='NVIDIA H100 80GB HBM3' on a box with no GPU and get an attested, priced
    H100 listing — attestation proves key possession, not silicon. This is the honest-agent gate:
    if a GPU is declared, nvidia-smi must show at least that many NVIDIA GPUs on this host, or we
    stop. It does NOT defend against a seller who patches the agent (only a server-timed benchmark
    does — see the REQUIRE_VERIFIED_HW path), but it stops the trivial env-var lie and a
    misconfigured driver. Bypass for a legitimately GPU-less listing is impossible (there is
    nothing to claim); ALLOW_UNVERIFIED_GPU=true exists only for CI/dev harnesses that fake a GPU
    and is honoured only in an explicitly non-production environment (_unverified_gpu_override).

    The declaration is checked as a WHOLE — model AND count together, against the full nvidia-smi
    list — because each half alone has a hole: a count with no model skipped the gate entirely and
    still registered as GPU_COUNT=8, and a count-only comparison let a box with one RTX 3060 pass
    while declaring GPU_MODEL='NVIDIA H100 80GB HBM3'."""
    declared_model = (gpu_model or "").strip()
    try:
        declared_count = int(gpu_count or 0)
    except (TypeError, ValueError):
        declared_count = 0
    if not declared_model and declared_count <= 0:
        return                                            # CPU-only node — nothing to verify
    if not declared_model or declared_count <= 0:
        # An incoherent half-declaration is not "CPU-only": GPU_COUNT=8 with no GPU_MODEL used to
        # return here and still reach the registry as an 8-GPU spec, and a model with count 0
        # lists silicon nobody can be scheduled onto. Refuse rather than guess which half is real.
        _fail("Incomplete GPU declaration",
              reason=(f"GPU_MODEL={declared_model or 'unset'!r} and GPU_COUNT={declared_count} "
                      "disagree. A GPU listing needs BOTH a model and a positive count."),
              checks=["set GPU_MODEL and GPU_COUNT together, or",
                      "unset GPU_MODEL/GPU_COUNT/VRAM_GB to auto-detect (or list as CPU-only)"],
              run="nvidia-smi --query-gpu=name --format=csv,noheader")
    if _unverified_gpu_override():
        return                                            # explicit test/dev override
    present = nvidia_gpu_names()
    if not present:
        _fail("Declared a GPU this machine cannot show",
              reason=(f"GPU_MODEL is set to {declared_model!r} but nvidia-smi reports no NVIDIA "
                      "GPU on this host. Petabyte will not list hardware it cannot see."),
              checks=["install the NVIDIA driver + `nvidia-smi` and re-run, or",
                      "unset GPU_MODEL/GPU_COUNT/VRAM_GB to list this box as a CPU-only node"],
              run="nvidia-smi")
    matching = [n for n in present if _gpu_matches(declared_model, n)]
    if not matching:
        _fail("Declared a GPU model this machine does not have",
              reason=(f"GPU_MODEL={declared_model!r} but nvidia-smi shows "
                      f"{', '.join(present)}. Petabyte will not list silicon it cannot see."),
              checks=["fix GPU_MODEL to name the card this host actually has, or",
                      "unset GPU_MODEL/GPU_COUNT/VRAM_GB and let the agent auto-detect it"],
              run="nvidia-smi --query-gpu=name --format=csv,noheader")
    if len(matching) < declared_count:
        # Count only the MATCHING cards: on a mixed host (1x H100 + 3x GT710) the total would
        # otherwise let "4x H100" through, and rank 1 would land on a GT710 at H100 prices.
        _fail("Declared more GPUs than this machine has",
              reason=(f"GPU_COUNT={declared_count} but nvidia-smi shows {len(matching)} GPU(s) "
                      f"matching {declared_model!r} out of {len(present)} total "
                      f"({', '.join(present)})."),
              run="nvidia-smi --query-gpu=name --format=csv,noheader")


def _fail(title, **kw):
    """Readable error to stderr, then exit non-zero (keeps the old exit contract)."""
    cli_ui.err.error(title, **kw)
    raise SystemExit(1)


def resolve_price(client, gpu_model):
    """Decide the hourly listing price for this node.

    The seller's explicit PRICE_PER_HOUR always wins. When it is unset — the common
    case, because onboarding is meant to be one command — we do NOT guess a flat rate.
    We ask the server for a fair, benchmark-anchored suggestion for the GPU we just
    detected (the same number the /install page shows), so a 4090 never lists at the
    same price as a 2060. Only if that call cannot be reached do we fall back to a
    labelled placeholder, and we say so out loud.

    Returns (price: float, basis: str).
    """
    raw = (os.getenv("PRICE_PER_HOUR") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v, "seller-set"
        except ValueError:
            pass
        print(f"PRICE_PER_HOUR={raw!r} is not a positive number — ignoring it and auto-pricing.")
    try:
        r = client.get("/pricing/suggest", params={"gpu_model": gpu_model or ""})
        if r.status_code == 200:
            body = r.json()
            p = float(body.get("suggested_price") or 0)
            if p > 0:
                return p, "auto: " + str(body.get("basis") or "benchmark-anchored")
    except Exception as e:  # network/parse — never let pricing block onboarding
        print(f"could not fetch a benchmark-anchored price ({e}); using a placeholder.")
    print("WARNING: no price set and the pricing service was unreachable — listing at "
          "$1.00/hr as a placeholder. Set PRICE_PER_HOUR or edit your listing to fix it.")
    return 1.0, "fallback (pricing service unreachable)"


def main():
    ui = cli_ui.out
    ui.heading("Provision this machine as a Petabyte node")
    API = os.environ.get("PETABYTE_API_URL")
    KEY = os.environ.get("PETABYTE_API_KEY")
    if not API:
        _fail("PETABYTE_API_URL is not set",
              reason="The node needs to know which Petabyte API to register with.",
              run="export PETABYTE_API_URL=https://petabyte.market")
    if not KEY:
        _fail("PETABYTE_API_KEY is not set",
              reason="The node authenticates with an API key (no username/password).",
              checks=["create one on the /install page (that button also makes you a seller)"],
              run="export PETABYTE_API_KEY=<your node key>")

    cpu, ram, gpu, gc, vram = detect()
    verify_declared_gpu(gpu, gc)
    ui.step("Detected hardware", done=True)
    ui.panel("", [
        ("CPU", f"{cpu} cores"),
        ("RAM", f"{ram} GB"),
        ("GPU", (f"{gpu} x{gc}" if gpu else "none (CPU-only node)")),
        ("VRAM", (f"{vram} GB" if vram else "—")),
    ], label_width=5)

    h = {"X-API-KEY": KEY}
    provider = os.getenv("PROVIDER", socket.gethostname() or "petabyte-node")
    with httpx.Client(base_url=API, timeout=20) as c:
        price, price_basis = resolve_price(c, gpu)
        # REPORTED confidential-computing capabilities (best-effort; never blocks registration).
        try:
            import confidential_detect as _cd
            cc_caps = _cd.detect_confidential()
        except Exception:
            cc_caps = None
        # Report our WireGuard egress public key so the API can assign a tunnel address and route
        # buyer egress through the gateway (hides the seller IP). Best-effort; needs wireguard-tools.
        try:
            import shutil as _sh
            import egress_vpn as _ev
            egress_pub = _ev.node_pubkey() if _sh.which("wg") else None
        except Exception:
            egress_pub = None
        ui.step(f"Registering spec with {API} …")
        spec = c.post("/register_specs", headers=h, json={
            "cpu": cpu, "ram": ram, "duration": int(os.getenv("MAX_HOURS", "24")),
            "price_per_hour": price,
            "provider": provider, "gpu_model": gpu, "gpu_count": gc, "vram_gb": vram,
            "units": int(os.getenv("UNITS", "1")),
            "egress_pubkey": egress_pub,
            "confidential": cc_caps})
        if spec.status_code == 403:
            _fail("This API key's account is not a seller",
                  reason="register_specs returned HTTP 403 (not a seller account).",
                  checks=["re-create the key from the /install page — that button makes "
                          "your account a seller"])
        if spec.status_code == 401:
            _fail("API key rejected (invalid or revoked)",
                  reason="register_specs returned HTTP 401.",
                  checks=["create a fresh node key on the /install page"])
        if spec.status_code >= 400:
            _fail("Could not register this node",
                  detail=f"HTTP {spec.status_code}: {spec.text[:200]}")
        _sj = spec.json()
        spec_id = _sj["spec_id"]
        # Egress-VPN config the API assigned (empty unless enabled server-side): written to agent.env
        # below so the agent tunnels buyer egress through the gateway on the next job.
        egress_env = {k: _sj[k] for k in
                      ("PB_EGRESS_GATEWAY_PUBKEY", "PB_EGRESS_GATEWAY_ENDPOINT", "PB_EGRESS_ADDR")
                      if _sj.get(k)}
        ui.step(f"Registered spec #{spec_id}", done=True)

        ui.step("Attesting node identity (Ed25519) …")
        att = {"cpu": cpu, "ram": ram, "gpu_model": gpu,
               "nonce": base64.b64encode(os.urandom(9)).decode(), "ts": int(time.time())}
        pr = c.post("/prove", headers=h, json={
            "spec_id": spec_id, "attestation": att,
            "signature": crypto.sign_proof(att), "pubkey": crypto.public_key_b64()})
        if pr.status_code >= 400:
            _fail("Attestation failed", detail=f"HTTP {pr.status_code}: {pr.text[:200]}")
        ui.step("Attestation accepted", done=True)

    key_path = os.getenv("PETABYTE_AGENT_KEY", crypto.KEY_PATH)
    env_path = os.getenv("AGENT_ENV", "/etc/petabyte/agent.env")
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    # agent.env holds the node's encrypted API key. Create it 0600 FROM THE START (O_CREAT with
    # mode) instead of open()-then-chmod, which leaves a brief window where the key file is
    # world/group-readable at the default umask.
    # O_TRUNC rewrites the file, so anything the operator put here — PRICE_PER_HOUR,
    # PB_TUNNEL_GATEWAY, AGENT_ALLOW_UNVERIFIED_VRAM, idle-mining addresses — used to be silently
    # deleted on every re-provision. The agent then came back up missing settings the operator
    # believed were set, which is the worst kind of failure: quiet and blamed on something else.
    # Carry over every line we do not own.
    preserved = _preserved_env_lines(env_path)
    _fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(_fd, "w") as f:
        f.write(f"PETABYTE_API_URL={API}\n"
                f"PETABYTE_API_KEY={KEY}\n"
                f"PETABYTE_SPEC_ID={spec_id}\n"
                f"PETABYTE_AGENT_KEY={key_path}\n")
        for _k, _v in egress_env.items():
            f.write(f"{_k}={_v}\n")
        if preserved:
            f.write("".join(line + "\n" for line in preserved))
    os.chmod(env_path, 0o600)   # belt-and-suspenders if the file pre-existed with looser perms
    ui.blank()
    ui.success("Node provisioned and online", **{
        "Spec": f"#{spec_id}",
        "GPU": (f"{gpu} x{gc}" if gpu else "CPU-only"),
        "Price": f"${price:.2f}/hour ({price_basis})",
        "Env": env_path,
        "Next": "python main.py   (or start the petabyte-agent service)",
    })


if __name__ == "__main__":
    main()
