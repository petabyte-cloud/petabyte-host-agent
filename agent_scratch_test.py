"""agent_scratch_test.py — buyer job scratch prefers a RAM-backed (or encrypted) base, not disk.

Proves the selection logic: an operator-provided PB_JOB_SCRATCH_DIR (e.g. an encrypted mount) wins,
else a genuinely RAM-backed tmpfs, else it falls back to the plaintext temp dir only when neither
has room. make_scratch creates a chmod'd dir under the chosen base for the caller to wipe.

Also proves the properties that make the RAM staging worth anything at all:
  * a writable directory with free space is NOT assumed to be RAM (a container/chroot can ship
    /dev/shm as an ordinary DISK dir — staging there is the silent failure this module prevents),
  * the plaintext-disk fallback is loud on EVERY job, and PB_JOB_SCRATCH_REQUIRE_RAM turns it into
    a hard failure instead,
  * a crashed agent's leftovers are reclaimed at startup (tmpfs survives process death), while a
    live agent's in-flight job data is never touched,
  * the notebook runner wipes its workspace in ONE finally covering every exit path.

Offline, stdlib only. Run: python agent_scratch_test.py
"""
import logging
import os
import shutil
import tempfile

import agent_scratch as s

_fail = 0


def ok(label, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        _fail += 1


_min_saved = s._MIN_FREE
_env_saved = os.environ.get("PB_JOB_SCRATCH_DIR")
_req_saved = os.environ.get("PB_JOB_SCRATCH_REQUIRE_RAM")
os.environ.pop("PB_JOB_SCRATCH_REQUIRE_RAM", None)

# 1) operator override (e.g. an encrypted mount) is honored when it has room
_ovr = tempfile.mkdtemp(prefix="pb-ovr-")
os.environ["PB_JOB_SCRATCH_DIR"] = _ovr
s._MIN_FREE = 1
ok("scratch_base honors the PB_JOB_SCRATCH_DIR override", s.scratch_base() == _ovr)
_d = s.make_scratch("t-")
ok("make_scratch creates the dir under the chosen base", os.path.dirname(_d) == _ovr and os.path.isdir(_d))
ok("make_scratch is 0700 by default", (os.stat(_d).st_mode & 0o777) == 0o700)
_d2 = s.make_scratch("t2-", mode=0o777)
ok("make_scratch honors mode=0o777 (container writes results back)", (os.stat(_d2).st_mode & 0o777) == 0o777)
shutil.rmtree(_d, ignore_errors=True)
shutil.rmtree(_d2, ignore_errors=True)
shutil.rmtree(_ovr, ignore_errors=True)

# 2) prefers /dev/shm (tmpfs, RAM) when it exists and has room and there's no override
os.environ.pop("PB_JOB_SCRATCH_DIR", None)
s._MIN_FREE = 1
if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
    ok("prefers /dev/shm (RAM tmpfs) when available", s.scratch_base() == "/dev/shm")
    ok("/dev/shm is confirmed RAM-backed via the mount table", s._is_ram_backed("/dev/shm"))
else:
    ok("(/dev/shm not available here — skipped tmpfs-preference check)", True)

# 3) falls back to the plaintext temp dir when nothing confidential is usable
os.environ["PB_JOB_SCRATCH_DIR"] = "/nonexistent/pb-should-not-exist"
s._MIN_FREE = 1 << 62      # no real base can satisfy this — forces the fallback
ok("falls back to the plaintext temp dir when no RAM/encrypted base is usable",
   s.scratch_base() == tempfile.gettempdir())

# 4) writability + free space do NOT make a directory RAM. A container image, chroot, or odd distro
#    can ship /dev/shm as an ordinary disk directory; selecting it would put the buyer's plaintext
#    on the seller's persistent disk while the log claimed "RAM" — worse than not trying at all.
s._MIN_FREE = 1
_disk = None
for _c in (os.getcwd(), os.path.expanduser("~"), "/var/tmp"):
    if os.path.isdir(_c) and os.access(_c, os.W_OK) and s.fstype(_c) not in ("tmpfs", "ramfs", None):
        _disk = _c
        break
if _disk:
    ok("a writable disk dir with free space is NOT accepted as RAM-backed", not s._is_ram_backed(_disk))
    ok("fstype() reports the real filesystem for a disk dir", s.fstype(_disk) not in ("tmpfs", "ramfs"))
else:
    ok("(no disk-backed dir available here — skipped tmpfs-proof check)", True)

# 5) the plaintext-disk fallback is LOUD on every job, not once per agent lifetime: a single startup
#    line scrolls away and every later job then looks confidential when it is not.
os.environ["PB_JOB_SCRATCH_DIR"] = "/nonexistent/pb-should-not-exist"
s._MIN_FREE = 1 << 62


class _Count(logging.Handler):
    def __init__(self):
        super().__init__()
        self.n = 0

    def emit(self, record):
        if record.levelno >= logging.WARNING and "PLAINTEXT DISK" in record.getMessage():
            self.n += 1


_h = _Count()
s._log.addHandler(_h)
s.scratch_base()
s.scratch_base()
s._log.removeHandler(_h)
ok("every plaintext-disk fallback is warned about (not just the first)", _h.n == 2)

# 6) PB_JOB_SCRATCH_REQUIRE_RAM turns the silent-ish fallback into a hard job failure, for operators
#    who would rather lose the job than write a buyer's plaintext to their disk.
os.environ["PB_JOB_SCRATCH_REQUIRE_RAM"] = "1"
try:
    s.scratch_base()
    ok("PB_JOB_SCRATCH_REQUIRE_RAM fails the job instead of staging on disk", False)
except s.ScratchUnavailable:
    ok("PB_JOB_SCRATCH_REQUIRE_RAM fails the job instead of staging on disk", True)
try:
    s.make_scratch("pb_nb_")
    ok("make_scratch propagates the refusal (the caller fails the job)", False)
except s.ScratchUnavailable:
    ok("make_scratch propagates the refusal (the caller fails the job)", True)
os.environ.pop("PB_JOB_SCRATCH_REQUIRE_RAM", None)

# 7) a crashed agent's leftovers are reclaimed at startup. tmpfs is NOT cleared when a process dies
#    (only on unmount/reboot), so without this the last buyer's plaintext sits in the seller's RAM
#    forever and the leftovers pile up until the free-space guard stops choosing RAM at all.
_base = tempfile.mkdtemp(prefix="pb-sweep-")
os.environ["PB_JOB_SCRATCH_DIR"] = _base
s._MIN_FREE = 1
_stale = os.path.join(_base, "pb_nb_999999999_dead")      # PID that cannot be running
os.makedirs(_stale)
open(os.path.join(_stale, "input.ipynb"), "w").write("buyer plaintext")
_live = s.make_scratch("pb_nb_")                          # this process is alive
_alien = os.path.join(_base, "not-ours-keepme")
os.makedirs(_alien)
_removed = s.sweep_stale()
ok("sweep_stale wipes a dead agent's staged buyer data", not os.path.exists(_stale) and _removed >= 1)
ok("sweep_stale never touches a LIVE agent's in-flight job data", os.path.isdir(_live))
ok("sweep_stale never touches directories that are not ours", os.path.isdir(_alien))
shutil.rmtree(_base, ignore_errors=True)

# 8) the notebook runner must wipe its workspace on EVERY exit path. Per-branch rmtree calls get
#    skipped whenever something in between raises (nbformat.write hitting ENOSPC, nbformat.read on
#    an output.ipynb the buyer's own code clobbered), leaving plaintext behind.
_nb = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "notebook.py"),
           encoding="utf-8").read()
ok("notebook.py wipes the workspace in a finally", "finally:" in _nb and "shutil.rmtree(scratch" in _nb)
ok("notebook.py has exactly ONE workspace wipe (the finally), not per-branch ones",
   _nb.count("shutil.rmtree(") == 1)
ok("notebook.py stages under agent_scratch, not a plain mkdtemp",
   "agent_scratch.make_scratch(" in _nb and "tempfile.mkdtemp" not in _nb)
# 0700 outer + 0777 leaf: the sandbox image's unprivileged user must write output.ipynb back into
# the bind mount, but the world-writable dir must not sit exposed in a world-traversable /dev/shm.
ok("notebook.py bind-mounts a 0777 LEAF inside the 0700 scratch dir",
   'os.path.join(scratch, "work")' in _nb and "os.chmod(workdir, 0o777)" in _nb
   and '"-v", f"{workdir}:/work:rw"' in _nb)

s._MIN_FREE = _min_saved
if _env_saved is None:
    os.environ.pop("PB_JOB_SCRATCH_DIR", None)
else:
    os.environ["PB_JOB_SCRATCH_DIR"] = _env_saved
if _req_saved is not None:
    os.environ["PB_JOB_SCRATCH_REQUIRE_RAM"] = _req_saved

print(f"\n=== agent scratch: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
