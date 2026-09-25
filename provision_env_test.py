"""provision_env_test.py — re-provisioning must not delete the operator's own settings, and the
agent key must live somewhere the hardened service can actually reach.

Both of these were real failures on a live node:

  * `provision.py` rewrote /etc/petabyte/agent.env with O_TRUNC and only its own four keys, so
    PRICE_PER_HOUR / PB_TUNNEL_GATEWAY / AGENT_ALLOW_UNVERIFIED_VRAM vanished on every
    re-provision. The agent came back up missing settings the operator had set, and the GPU job
    was refused by the VRAM gate with no obvious cause.
  * `crypto.KEY_PATH` defaulted under $HOME while petabyte-agent.service runs ProtectHome=true,
    so anything touching the key died with "[Errno 30] Read-only file system: '/root/.petabyte'"
    — including the container watchdog, which then could not report an exited container and left
    the platform showing a dead job as "running".

Run: python provision_env_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        _fail += 1


import provision  # noqa: E402

# ----------------------------------------------------------------- agent.env preservation
d = tempfile.mkdtemp()
env = os.path.join(d, "agent.env")
with open(env, "w") as f:
    f.write(
        "PETABYTE_API_URL=https://old.example\n"
        "PETABYTE_API_KEY=old-key\n"
        "PETABYTE_SPEC_ID=1\n"
        "PETABYTE_AGENT_KEY=/old/path\n"
        "PRICE_PER_HOUR=0.75\n"
        "PB_TUNNEL_GATEWAY=pbtun@203.0.113.1\n"
        "AGENT_ALLOW_UNVERIFIED_VRAM=true\n"
        "# a comment\n"
        "\n"
        "XMR_ADD=4abc\n"
    )

kept = provision._preserved_env_lines(env)
ok("operator settings are preserved", set(kept) == {
    "PRICE_PER_HOUR=0.75", "PB_TUNNEL_GATEWAY=pbtun@203.0.113.1",
    "AGENT_ALLOW_UNVERIFIED_VRAM=true", "XMR_ADD=4abc"})
ok("the four provisioning-owned keys are NOT preserved (no duplicates after a rewrite)",
   not any(line.split("=", 1)[0] in provision.MANAGED_ENV_KEYS for line in kept))
ok("comments and blank lines are dropped rather than duplicated",
   not any(line.strip().startswith("#") or not line.strip() for line in kept))
ok("a missing agent.env is not an error (first provision)",
   provision._preserved_env_lines(os.path.join(d, "nope.env")) == [])
ok("an unreadable path yields nothing rather than raising",
   provision._preserved_env_lines(d) == [])

with open(env, "w") as f:
    f.write("NO_EQUALS_LINE\nGOOD=1\n")
ok("a malformed line is skipped, a good one survives",
   provision._preserved_env_lines(env) == ["GOOD=1"])

# ----------------------------------------------------------------- key path
import crypto  # noqa: E402

ok("the default key path is NOT under $HOME, which ProtectHome=true hides",
   crypto._STATE_KEY == "/var/lib/petabyte-agent/agent_ed25519.key")
ok("it is the StateDirectory the unit already grants write access to",
   crypto.KEY_PATH in (crypto._STATE_KEY, crypto._LEGACY_KEY))
ok("an explicit PETABYTE_AGENT_KEY still wins",
   os.getenv("PETABYTE_AGENT_KEY") is None or crypto.KEY_PATH == os.environ["PETABYTE_AGENT_KEY"])
ok("a node provisioned before this keeps its existing key (identity preserved)",
   (crypto.KEY_PATH == crypto._LEGACY_KEY) == os.path.exists(crypto._LEGACY_KEY))

print(f"\n=== provision_env: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
