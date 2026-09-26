"""agent_cli — human-facing terminal UX for the Petabyte seller node.

The node's day-to-day output is a debug log (heartbeats, job claims). That log is for
diagnosis and stays exactly as it is. THIS module is the other half a human needs: a
clean startup banner, a `doctor` preflight that says what's wrong and what to do about
it, and `--help` / `--version`. It shares cli_ui with the buyer CLI and the desktop app
so every Petabyte surface speaks the same visual language.

`doctor` must never import task_fetcher — that module hard-exits at import time when the
node isn't configured yet, which is exactly the situation doctor exists to diagnose.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

import cli_ui

# Entrypoints set this before calling handle_cli() so the shared module stays identical
# across lumaris_agent (main.py) and desktop-app (petabyte_desktop.py).
PRODUCT = "Petabyte Agent"

try:
    from version import VERSION            # desktop-app ships version.py
except Exception:
    VERSION = "1.0.0"                      # keep in step with setup.py


def _entry():
    try:
        name = os.path.basename(sys.argv[0])
        return name if name.endswith(".py") else "main.py"
    except Exception:
        return "main.py"


def _help() -> str:
    entry = _entry()
    return f"""\
{PRODUCT} {VERSION} — list your GPU on the Petabyte marketplace.

Usage:
  python {entry}              start the node (heartbeat + job polling + local dashboard)
  python {entry} doctor       check env, GPU, Docker and API connectivity before starting
  python {entry} debug        send Petabyte support this node's diagnostics (--dry-run: just print it)
  python {entry} fixes        status | enable | disable: let support apply signed fixes to this machine
  python {entry} --version    print the version
  python {entry} --help       show this help

Environment:
  PETABYTE_API_URL   Petabyte API base URL               (required)
  PETABYTE_API_KEY   node API key from the /install page  (required)
  PETABYTE_SPEC_ID   the spec id this node serves         (required to run jobs)
  PETABYTE_AGENT_KEY path to the Ed25519 node key         (optional; auto-created)

Colour follows the terminal: it turns off when output is piped or NO_COLOR is set.
Add --json to `doctor` for machine-readable output."""


def banner(ui: cli_ui.Ui, *, product=PRODUCT, url=None) -> None:
    """One tidy startup header instead of a bare log line."""
    marker = "◆" if ui.unicode else "*"      # ◆ is not cp1252-encodable — fall back on ASCII
    ui.line(ui.bold(ui.teal(f"{marker} {product}")) + ui.dim(f"  v{VERSION}"))
    if url:
        ui.line(ui.dim(f"  API {url}"))


def _detect_hw():
    """(cpu, ram_gb, gpu_model, gpu_count, vram_gb) — reuse provision's detector if present."""
    try:
        import provision
        return provision.detect()
    except Exception:
        return (os.cpu_count() or 1, None, os.getenv("GPU_MODEL"),
                int(os.getenv("GPU_COUNT", "0") or 0), int(os.getenv("VRAM_GB", "0") or 0))


def _run_doctor(as_json: bool) -> int:
    api = os.getenv("PETABYTE_API_URL")
    key = os.getenv("PETABYTE_API_KEY")
    spec = os.getenv("PETABYTE_SPEC_ID")
    checks = []

    def add(name, level, detail):
        # level: "ok" | "warn" | "fail"
        checks.append({"check": name, "level": level, "detail": detail})

    add("API URL set", "ok" if api else "fail", api or "PETABYTE_API_URL is not set")
    add("API key set", "ok" if key else "fail",
        (key[:8] + ("…" if cli_ui.supports_unicode() else "...")) if key
        else "PETABYTE_API_KEY is not set (create one on /install)")
    add("Spec id set", "ok" if spec else "warn",
        spec or "PETABYTE_SPEC_ID not set — run provision.py to register this node")

    cpu, ram, gpu, gc, vram = _detect_hw()
    if gpu:
        add("GPU detected", "ok", f"{gpu} x{gc or 1}" + (f", {vram}GB" if vram else ""))
    else:
        add("GPU detected", "warn", "no NVIDIA GPU found (CPU-only node)")

    add("Docker available", "ok" if shutil.which("docker") else "warn",
        shutil.which("docker") or "docker not found — buyer jobs run in Docker on this host")

    # API reachability + key validity (best-effort; never raise out of doctor)
    if api:
        try:
            import httpx
            t0 = time.time()
            r = httpx.get(f"{api}/healthz", timeout=8)
            ms = int((time.time() - t0) * 1000)
            add("API reachable", "ok" if r.status_code < 500 else "fail",
                f"HTTP {r.status_code} in {ms}ms")
            if key:
                vr = httpx.get(f"{api}/verify_api_key", headers={"X-API-KEY": key}, timeout=8)
                add("API key valid", "ok" if vr.status_code == 200 else "fail",
                    f"HTTP {vr.status_code}" + ("" if vr.status_code == 200 else " — key invalid/revoked"))
        except Exception as e:
            add("API reachable", "fail", f"{type(e).__name__}: {e}")

    fails = [c for c in checks if c["level"] == "fail"]
    healthy = not fails

    if as_json:
        cli_ui.emit_json({"product": PRODUCT, "version": VERSION, "healthy": healthy,
                          "checks": checks})
        return 0 if healthy else 1

    ui = cli_ui.out
    ui.heading(f"{PRODUCT} · doctor")
    for c in checks:
        state = {"ok": "ok", "warn": "warning", "fail": "failed"}[c["level"]]
        ui.line(f"{ui.status_label(state):<22} {ui.dim(c['check'] + ':'):<28} {c['detail']}")
    ui.blank()
    if healthy:
        ui.success("Preflight passed — this node is ready to start.",
                   Start="python main.py")
    else:
        cli_ui.err.error(
            "Preflight failed — the node cannot serve jobs yet.",
            reason=", ".join(c["check"] for c in fails) + " failing.",
            checks=["set the missing environment variables (see --help)",
                    "create a node key + register on the /install page"],
            run="python provision.py")
    return 0 if healthy else 1


def handle_cli(argv) -> bool:
    """Handle --help / --version / doctor. Returns True if it handled the invocation
    (the caller should then stop and NOT launch the node). Exits the process for
    doctor so the exit code reflects preflight health."""
    args = list(argv or [])
    if args and args[0] == "storage":
        try:
            import template_storage
        except ImportError:  # shared desktop CLI ships no seller Docker ledger
            raise SystemExit("Template storage is only available in the seller agent install.") from None
        import json
        with template_storage.locked():
            state = template_storage.read()
        state["policy"] = {"cache_budget_bytes": int(template_storage.policy()[0]),
                           "disk_reserve_bytes": int(template_storage.policy()[1])}
        print(json.dumps(state, indent=2))
        return True
    if args and args[0] == "mining":
        try:
            import idle_mining
        except ImportError:  # this file is shared with desktop-app, which ships no miner
            raise SystemExit("Idle mining is only available in the seller agent install.") from None
        action = args[1] if len(args) > 1 else "status"
        if action not in ("status", "enable", "disable"):
            raise SystemExit("Usage: python main.py mining [status|enable|disable]")
        idle_mining.control(action)
        return True
    if any(a in ("-h", "--help", "help") for a in args):
        cli_ui.out.line(_help())
        return True
    if any(a in ("-V", "--version", "version") for a in args):
        cli_ui.out.line(f"{PRODUCT} {VERSION}")
        return True
    if "doctor" in args:
        sys.exit(_run_doctor("--json" in args))
    if args and args[0] == "fixes":
        try:
            import fixes
        except ImportError:  # this file is shared with desktop-app, which ships no fix runner
            raise SystemExit("Support fixes are only available in the Linux seller agent install.") from None
        sys.exit(fixes.control(args[1] if len(args) > 1 else "status", assume_yes="--yes" in args))
    if args and args[0] == "debug":
        sys.exit(_run_debug("--dry-run" in args))
    return False


def _run_debug(dry_run: bool) -> int:
    """Send Petabyte support this node's diagnostics report (the same one the agent sends by itself
    when its GPU checks fail). --dry-run only prints it. A copy is always kept locally."""
    try:
        import diagnostics
    except ImportError:  # this file is shared with desktop-app, which ships no diagnostics
        raise SystemExit("Diagnostics are only available in the Linux seller agent install.") from None
    copy = "/var/tmp/petabyte-diagnostics.txt"
    if dry_run:
        report = diagnostics.collect("seller dry run", diagnostics._env().get("PETABYTE_SPEC_ID", "?"))
        cli_ui.out.line(report)
        return 0
    try:
        report, reply = diagnostics.send("seller ran: main.py debug")
    except Exception as e:                                   # noqa: BLE001
        cli_ui.out.line(f"Could not send the report ({e}). Run with sudo, check your connection, or "
                        "try again in an hour.")
        return 1
    if report is None:
        cli_ui.out.line("Not sent: PB_SHARE_DIAGNOSTICS=false, or PETABYTE_API_URL/API_KEY/SPEC_ID are "
                        "missing (run with sudo so /etc/petabyte/agent.env can be read).")
        return 1
    try:
        with open(copy, "w") as f:
            f.write(report)
    except OSError:
        copy = None
    cli_ui.out.line("Sent your node's diagnostics to Petabyte support. We'll be in touch."
                    + (f" A copy of exactly what was sent is in {copy}." if copy else ""))
    return 0
