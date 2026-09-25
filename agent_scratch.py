"""RAM-backed (or operator-provided encrypted) scratch for buyer job data.

A buyer's code / inputs / results are plaintext only while their job actually runs. Staging that on a
tmpfs (default /dev/shm, which is RAM-backed on Linux and needs no mount or privilege) instead of the
seller's disk means there is NO plaintext copy on persistent disk to image or recover after the job —
it lives in RAM and is gone on unmount / reboot. An operator who needs more room than RAM can
point PB_JOB_SCRATCH_DIR at an ENCRYPTED mount (dm-crypt / gocryptfs / LUKS) to keep the same
at-rest property on disk. Only when neither is usable does it fall back to the normal (plaintext)
temp dir — loudly, on every job, so a seller silently staging buyer plaintext on disk is visible in
the log rather than invisible. Operators who would rather LOSE the job than write buyer plaintext to
disk set PB_JOB_SCRATCH_REQUIRE_RAM=1 and the fallback becomes a hard failure instead.

Honest boundary (by design — "harder, not impossible"): a root host owner can still read the RAM
(or the decrypted mount) *while the job is running*. This raises the attacker's cost from "image the
disk and read buyer data at leisure, after the fact, in bulk" to "capture live memory during each
individual run" — which is far harder to do at scale and leaves no at-rest artifact. True
confidentiality against a root host still requires a TEE (see CONFIDENTIAL compute mode).
"""
import logging
import os
import shutil
import tempfile

_log = logging.getLogger("petabyte.agent.scratch")

# Headroom required before we stage on a tmpfs base, so a job never fills RAM out from under the box
# or other jobs (tmpfs is size-capped, so an over-large job would otherwise hit ENOSPC). Real GPU
# seller nodes have many GB of /dev/shm; tiny/constrained hosts fall back to disk. Operator-tunable.
_MIN_FREE = int(os.getenv("PB_JOB_SCRATCH_MIN_FREE_BYTES", str(1024 * 1024 * 1024)))  # 1 GiB

_RAM_FSTYPES = ("tmpfs", "ramfs")

# Every prefix make_scratch() is called with. sweep_stale() reclaims leftovers with these names at
# agent start; anything else in the scratch base is not ours and is never touched.
_PREFIXES = ("pb_nb_", "pb-run-")


class ScratchUnavailable(RuntimeError):
    """No RAM/encrypted scratch base was usable while PB_JOB_SCRATCH_REQUIRE_RAM is set.

    Raised instead of quietly staging the buyer's plaintext on the seller's persistent disk: the
    job fails, which is the outcome an operator asked for by setting the flag."""


def _require_ram() -> bool:
    return os.getenv("PB_JOB_SCRATCH_REQUIRE_RAM", "").strip().lower() in ("1", "true", "yes", "on")


def _unescape(mountpoint: str) -> str:
    # /proc mount tables octal-escape space/tab/newline/backslash in the mount point.
    for esc, ch in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        mountpoint = mountpoint.replace(esc, ch)
    return mountpoint


def fstype(path: str):
    """Filesystem type actually backing `path`, from /proc/self/mountinfo.

    Uses the LONGEST matching mount point, so a disk filesystem mounted *under* a tmpfs is reported
    as the disk it is. Returns None when it cannot be determined (no /proc, non-Linux); callers must
    treat None as "not proven to be RAM" rather than assuming."""
    try:
        real = os.path.realpath(path)
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    best, best_len = None, -1
    for line in lines:
        pre, sep, post = line.partition(" - ")
        if not sep:
            continue
        fields = pre.split(" ")
        if len(fields) < 5:
            continue
        mp = _unescape(fields[4])
        if real == mp or real.startswith(mp.rstrip("/") + "/"):
            if len(mp) > best_len:
                best, best_len = post.split(" ")[0], len(mp)
    return best


def _is_ram_backed(path: str) -> bool:
    return fstype(path) in _RAM_FSTYPES


def _usable(path: str, need: int) -> bool:
    """Writable directory with at least `need` bytes free."""
    try:
        if not (path and os.path.isdir(path) and os.access(path, os.W_OK)):
            return False
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize >= need
    except Exception:            # noqa: BLE001 — incl. no os.statvfs on non-Linux
        return False


def scratch_base(need_bytes: int = 0) -> str:
    """Directory to create per-job scratch under: an operator-provided encrypted mount, else a
    RAM-backed tmpfs, else the plaintext temp dir (loudly, or a raise under REQUIRE_RAM).

    need_bytes is what the caller expects to stage, so a job larger than the remaining RAM is sent
    to disk (slow but correct) instead of hitting ENOSPC on the tmpfs and dying mid-rental."""
    need = max(_MIN_FREE, int(need_bytes or 0))
    tried = []

    # 1) Operator override. Trusted BY THE OPERATOR to be encrypted-at-rest (dm-crypt/LUKS/gocryptfs)
    #    or their own tmpfs, so we do not require a RAM fstype here — only that it works.
    override = os.getenv("PB_JOB_SCRATCH_DIR", "").strip()
    if override:
        tried.append(override)
        if _usable(override, need):
            return override
        _log.warning("PB_JOB_SCRATCH_DIR=%s is not usable (missing, unwritable, or <%d bytes free); "
                     "falling through to RAM/plaintext selection.", override, need)

    # 2) RAM. Writability and free space do NOT prove a directory is RAM: a container image, chroot,
    #    or unusual distro can ship /dev/shm as an ORDINARY DISK DIRECTORY, and staging there would
    #    put the buyer's plaintext on the seller's persistent disk while the log claimed "RAM" — the
    #    exact silent failure this module exists to prevent. So require a tmpfs/ramfs mount.
    #    The temp dir is checked too because most systemd distros mount /tmp as tmpfs, which is
    #    every bit as RAM-backed as /dev/shm.
    for cand in ("/dev/shm", tempfile.gettempdir()):
        if cand in tried:
            continue
        tried.append(cand)
        if _usable(cand, need) and _is_ram_backed(cand):
            return cand

    # 3) Plaintext disk. Never silent: this is the one outcome where the buyer's code/results land on
    #    the seller's persistent disk, so it is logged for EVERY job it affects (not once per agent
    #    lifetime — a single startup line scrolls away and every later job looks confidential).
    fallback = tempfile.gettempdir()
    if _require_ram():
        raise ScratchUnavailable(
            f"PB_JOB_SCRATCH_REQUIRE_RAM is set and no RAM/encrypted scratch base is usable "
            f"(tried {tried}, need >={need} bytes free) — refusing to stage buyer data on disk")
    _log.warning(
        "no RAM/encrypted scratch base usable (tried %s, need >=%d bytes free); buyer job data "
        "WILL BE STAGED ON THE PLAINTEXT DISK at %s and is recoverable from it afterwards. Set "
        "PB_JOB_SCRATCH_DIR to an encrypted mount, free RAM in /dev/shm, or set "
        "PB_JOB_SCRATCH_REQUIRE_RAM=1 to fail the job instead.", tried, need, fallback)
    return fallback


def make_scratch(prefix: str, mode: int = 0o700, need_bytes: int = 0) -> str:
    """mkdtemp() under scratch_base(), chmod'd to `mode`. The caller MUST wipe it in a finally
    (shutil.rmtree); on a tmpfs base the plaintext is then gone from RAM too.

    The owning PID is embedded in the directory name so sweep_stale() can tell a crashed agent's
    leftovers from a live agent's in-flight job."""
    d = tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}_", dir=scratch_base(need_bytes))
    try:
        os.chmod(d, mode)
    except OSError:
        pass
    return d


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True          # exists but not ours, or unknown -> assume live, never delete


def sweep_stale() -> int:
    """Remove scratch left by a PREVIOUS agent process, and return how many were removed.

    /dev/shm is NOT cleared when a process dies — only on unmount/reboot — so an agent that is
    killed or crashes mid-job leaves the buyer's plaintext sitting in the seller's RAM indefinitely,
    and those leftovers accumulate until the free-space guard stops choosing RAM at all and every
    later job silently falls back to the plaintext disk. Called once at agent start.

    Only directories that match a make_scratch() prefix, are owned by this user, and whose embedded
    PID is no longer running are removed — so this can never delete a concurrently running agent's
    in-flight job data."""
    removed = 0
    bases, seen = [], set()
    for b in (os.getenv("PB_JOB_SCRATCH_DIR", "").strip(), "/dev/shm", tempfile.gettempdir()):
        if b and b not in seen and os.path.isdir(b):
            seen.add(b)
            bases.append(b)
    for base in bases:
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            prefix = next((p for p in _PREFIXES if name.startswith(p)), None)
            if not prefix:
                continue
            pid_part = name[len(prefix):].split("_")[0]
            if not pid_part.isdigit() or _pid_is_live(int(pid_part)):
                continue
            path = os.path.join(base, name)
            try:
                uid = getattr(os, "geteuid", lambda: -1)()
                if not os.path.isdir(path) or (uid >= 0 and os.lstat(path).st_uid != uid):
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                removed += 1
    if removed:
        _log.warning("reclaimed %d job scratch dir(s) left by a previous agent process "
                     "(buyer plaintext outlives a crash in /dev/shm until it is wiped)", removed)
    return removed
