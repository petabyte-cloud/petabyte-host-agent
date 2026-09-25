"""agent_cli_test.py — the seller node's human-facing CLI, asserted (no server needed).

The node's `doctor`, `--help`, `--version`, and `provision.py` are what a seller sees
before anything works — often when the node is NOT yet configured. This proves:

  * doctor runs on an UNCONFIGURED node (it must not hard-exit importing task_fetcher)
  * doctor --json is pure, valid JSON on stdout (no stray log lines) — the machine contract
  * --help / --version exit 0 and say the right things
  * provision.py fails readably (no traceback) when the API key/URL is missing
  * an unconfigured `python main.py` explains itself instead of crashing

Run:  python agent_cli_test.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_fail = 0


def ok(label, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + label + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        _fail += 1


def _encodable(text, enc):
    try:
        text.encode(enc)
        return True
    except Exception:
        return False


def run(script, *args, clear_config=True, color=None, ascii_icons=False):
    """Run an agent script as a subprocess with a clean, unconfigured environment."""
    env = dict(os.environ)
    for k in ("PETABYTE_API_URL", "PETABYTE_API_KEY", "PETABYTE_SPEC_ID",
              "PETABYTE_COLOR", "NO_COLOR", "PETABYTE_ASCII"):
        env.pop(k, None)
    if color == "always":
        env["PETABYTE_COLOR"] = "always"
    if color == "never":
        env["NO_COLOR"] = "1"
    if ascii_icons:
        env["PETABYTE_ASCII"] = "1"
        env["NO_COLOR"] = "1"
    p = subprocess.run([sys.executable, script, *args], cwd=HERE, env=env,
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    return p.returncode, p.stdout, p.stderr


# ---- --version / --help --------------------------------------------------
code, so, se = run("main.py", "--version")
ok("--version exits 0", code == 0, str(code))
ok("--version prints product + version", "Petabyte Agent" in so and "1.0.0" in so)

code, so, se = run("main.py", "--help")
ok("--help exits 0", code == 0)
ok("--help documents usage + doctor", "Usage" in so and "doctor" in so)
ok("--help documents required env vars", "PETABYTE_API_URL" in so and "PETABYTE_API_KEY" in so)

# ---- doctor on an UNCONFIGURED node -------------------------------------
code, so, se = run("main.py", "doctor", color="always")
combined = so + se
ok("doctor on an unconfigured node exits 1", code == 1, str(code))
ok("doctor did NOT hard-exit at import (it produced its report)",
   "doctor" in combined.lower() and "API URL set" in combined)
ok("doctor tells the seller what to run next", "provision.py" in combined)
ok("doctor has no raw traceback", "Traceback" not in combined)
ok("doctor with colour emits ANSI", "\033[" in combined)

# ---- doctor --json is a clean machine-readable contract -----------------
code, so, se = run("main.py", "doctor", "--json")
try:
    dj = json.loads(so)                       # stdout must be JSON ALONE (no log noise)
except Exception:
    dj = None
ok("doctor --json: stdout is pure valid JSON", isinstance(dj, dict))
ok("doctor --json has the expected shape",
   bool(dj) and {"product", "version", "healthy", "checks"} <= set(dj or {}))
ok("doctor --json never contains ANSI", "\033[" not in so)
ok("doctor --json lists per-check results", bool(dj) and isinstance(dj.get("checks"), list)
   and all("check" in c and "level" in c for c in dj["checks"]))

# ---- ASCII fallback (no Unicode, no colour) -----------------------------
# The contract is that a limited console (e.g. Windows cp1252) never sees the glyphs it
# can't render: box-drawing, check marks, geometric shapes, ellipsis, bullet, arrow.
code, so, se = run("main.py", "doctor", ascii_icons=True)
DANGEROUS = set("✓✗●○▲─…•→")
ok("doctor in ASCII mode emits no cp1252-breaking glyphs",
   not (DANGEROUS & set(so + se)), repr(sorted(DANGEROUS & set(so + se))))
ok("doctor in ASCII mode emits no ANSI", "\033[" not in (so + se))
ok("doctor in ASCII mode is cp1252-encodable (won't crash a Windows console)",
   _encodable(so + se, "cp1252"))

# ---- provision.py readable errors ---------------------------------------
code, so, se = run("provision.py")               # no API url/key set
combined = so + se
ok("provision without config exits 1", code == 1, str(code))
ok("provision names the missing variable", "PETABYTE_API_URL" in combined or "PETABYTE_API_KEY" in combined)
ok("provision has no raw traceback", "Traceback" not in combined)

# ---- unconfigured `python main.py` explains itself ----------------------
code, so, se = run("main.py")
combined = so + se
ok("unconfigured launch exits 1", code == 1, str(code))
ok("unconfigured launch is a readable message, not a crash",
   "not configured" in combined.lower() and "Traceback" not in combined)
ok("unconfigured launch points at doctor", "doctor" in combined)

print("\n" + ("PASS" if not _fail else f"FAIL ({_fail})") + " — agent CLI")
sys.exit(1 if _fail else 0)
