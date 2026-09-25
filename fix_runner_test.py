"""fix_runner_test.py — the root fix runner runs ONLY a fix signed by the release key, for THIS node,
unexpired, never twice, with an intact script, and only if the owner opted in. Uses the real
fix-runner.sh + openssl and payloads built by the SERVER's builder (lumaris_api/node_fixes.py), so
the two sides can't drift. Also checks fixes.py (the agent side that queues and reports).
Needs bash, OpenSSL 3 (pkeyutl -rawin, as update.sh), GNU sed, sha256sum.
Run: python fix_runner_test.py
"""
import base64
import os
import subprocess
import sys
import tempfile
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lumaris_api"))
import node_fixes  # noqa: E402  (server-side payload builder)
import fixes  # noqa: E402

_fail = 0


def ok(name, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    _fail += 0 if cond else 1


tmp = tempfile.mkdtemp(prefix="pb-fixrun-")
STATE, UNITS = os.path.join(tmp, "state"), os.path.join(tmp, "units")
os.makedirs(UNITS)
KEY, OTHER = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
PUB = os.path.join(tmp, "release.pub")
with open(PUB, "wb") as f:
    f.write(KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                          serialization.PublicFormat.SubjectPublicKeyInfo))
ENVF = os.path.join(tmp, "agent.env")
MARK = os.path.join(tmp, "ran")


def set_env(allow):
    with open(ENVF, "w") as f:
        f.write(f"PETABYTE_SPEC_ID=45\nPB_ALLOW_REMOTE_FIXES={'true' if allow else 'false'}\n")


fixes.STATE, fixes.ENV_FILE, fixes.UNIT_DIR = STATE, ENVF, UNITS
open(os.path.join(UNITS, fixes.UNITS[0]), "w").close()          # runner "installed"
set_env(True)


def payload(fid, node=45, expires=None, script=None):
    return node_fixes.build_payload(fid, node, expires or int(time.time()) + 3600,
                                    script or f"echo fixed-{fid}\ntouch {MARK}-{fid}\n")


def drop(fid, text, key=KEY):
    return fixes.handle({"id": fid, "payload_b64": base64.b64encode(text.encode()).decode(),
                         "signature": base64.b64encode(key.sign(text.encode())).decode()})


def run():
    env = dict(os.environ, PETABYTE_FIX_STATE=STATE, PETABYTE_RELEASE_PUBKEY=PUB, PETABYTE_AGENT_ENV=ENVF)
    subprocess.run(["bash", os.path.join(HERE, "fix-runner.sh")], env=env, check=True, timeout=120)
    return {fid: (rc, out) for fid, rc, out in fixes.results()}


# ---- agent side: queueing --------------------------------------------------------------------
ok("queued when enabled; payload written last, no temp files left",
   drop(1, payload(1)) and sorted(os.listdir(os.path.join(STATE, "fix-inbox"))) == ["1.payload", "1.sig"])
ok("never queued while a rental may be on the machine",
   not fixes.handle({"id": 2, "payload_b64": "eA==", "signature": "eA=="}, rental_live=True))
ok("malformed delivery ignored", not fixes.handle({"id": 3, "payload_b64": "!!", "signature": "x"}))

# ---- runner: the happy path ------------------------------------------------------------------
res = run()
ok("a valid signed fix runs as-is and reports rc 0 + output",
   res.get(1, ("", ""))[0] == "0" and "fixed-1" in res[1][1] and os.path.exists(MARK + "-1"), str(res))
ok("inbox cleared", os.listdir(os.path.join(STATE, "fix-inbox")) == [])
ok("the agent never re-queues a fix that already ran", not drop(1, payload(1)))
fixes.ack(1)
ok("ack clears the result", fixes.results() == [])
os.remove(MARK + "-1")
p1 = payload(1)                                     # a fresh, validly signed payload for the SAME id
open(os.path.join(STATE, "fix-inbox", "1.sig"), "wb").write(KEY.sign(p1.encode()))
open(os.path.join(STATE, "fix-inbox", "1.payload"), "w").write(p1)
res = run()
ok("a replayed fix id is dropped by the runner itself (never runs twice)",
   1 not in res and not os.path.exists(MARK + "-1") and os.listdir(os.path.join(STATE, "fix-inbox")) == [])


# ---- runner: every rejection -----------------------------------------------------------------
def rejected(fid, text, key=KEY, why=""):
    drop(fid, text, key)
    res = run()
    rc = res.get(fid, ("", ""))[0]
    fixes.ack(fid)
    return rc.startswith("rejected") and why in rc and not os.path.exists(f"{MARK}-{fid}"), rc


for fid, text, key, why, label in (
        (10, payload(10), OTHER, "signature did not verify", "signed by another key"),
        (11, payload(11, node=46), KEY, "another machine", "signed for another node"),
        (12, payload(12, expires=int(time.time()) - 5), KEY, "expired", "expired"),
        (17, payload(17, expires=int(time.time()) + 30 * 86400), KEY, "too far", "signed to live 30 days"),
        (13, payload(13).replace("echo fixed-13", "echo pwned-13"), KEY, "hash mismatch",
         "script changed after its hash was fixed (even if re-signed)"),
        (14, "not a fix\n---\ntouch " + MARK + "-14\n", KEY, "not a node-fix payload", "non-fix payload (domain)"),
        (15, payload(16), KEY, "fix id mismatch", "payload for another fix id"),
):
    good, rc = rejected(fid, text, key, why)
    ok(f"rejected + reported: {label}", good, rc)

# Review (2026-09-24): the owner's review, the workflow and the runner must read ONE format. A
# payload signed with hidden terminal escapes, or with an extra/duplicated header line, is refused.
def raw_payload(fid, script, extra_header=""):
    import hashlib
    sha = hashlib.sha256(script.encode()).hexdigest()
    return (f"pb-node-fix-v1\nnode: 45\nfix: {fid}\nnonce: {'a' * 32}\nexpires: {int(time.time()) + 3600}\n"
            f"{extra_header}sha256: {sha}\n---\n{script}")


good, rc = rejected(30, raw_payload(30, f"echo ok\x1b[2K\rtouch {MARK}-30\n"), why="non-printable")
ok("rejected: script hiding a line behind a terminal escape / carriage return", good, rc)
good, rc = rejected(31, raw_payload(31, f"touch {MARK}-31\n", extra_header="node: 46\n"), why="bad header")
ok("rejected: an extra header line (parsers can't disagree on the node)", good, rc)
os.makedirs(os.path.join(STATE, "fix-inbox"), exist_ok=True)
os.symlink(os.path.join(tmp, "agent.env"), os.path.join(STATE, "fix-inbox", "32.payload"))
run()
ok("a symlinked payload is dropped, never read", not os.path.lexists(os.path.join(STATE, "fix-inbox", "32.payload"))
   and 32 not in {fid for fid, _, _ in fixes.results()})

# pending(): a queued fix or an unreported result keeps the node from taking work
ok("nothing pending when inbox and outbox are empty", not fixes.pending())
drop(33, payload(33))
ok("a queued fix is pending", fixes.pending())
run()
ok("its unreported result is still pending", fixes.pending())
fixes.ack(33)
ok("reported -> not pending", not fixes.pending())

set_env(False)
open(os.path.join(STATE, "fix-inbox", "20.sig"), "wb").write(KEY.sign(payload(20).encode()))
open(os.path.join(STATE, "fix-inbox", "20.payload"), "w").write(payload(20))
res = run()
ok("owner not opted in -> rejected, nothing runs",
   res.get(20, ("", ""))[0].startswith("rejected: support fixes are not enabled")
   and not os.path.exists(MARK + "-20"))
ok("and the agent stops queueing and reports remote_fixes off", not fixes.enabled() and not drop(21, payload(21)))

print("\nOK — the fix runner only runs what was signed for this node" if not _fail else f"\n{_fail} FAILED")
raise SystemExit(1 if _fail else 0)
