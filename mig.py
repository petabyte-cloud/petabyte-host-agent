"""Per-rental NVIDIA MIG slices: one hardware partition of an A100/A30/H100/H200 per rental.

For a slice rental the agent creates a MIG GPU instance + compute instance on the GPU the API placed it
on, gives the container ONLY that slice (--gpus device=MIG-<uuid>), destroys it when the rental ends,
and garbage-collects slices a crashed agent left behind. Every step is checked; anything unexpected
raises Unverified and the caller refuses the rental. It never falls back to the whole card.

The agent never changes MIG *mode* (that needs a GPU reset and is the seller's choice). It only creates
and destroys instances on GPUs already in MIG mode, and only destroys instances it recorded in STATE.
"""
import json
import os
import re
import subprocess
import threading

STATE = "/var/lib/petabyte-agent/mig_slices.json"
_LOCK = threading.Lock()
_PENDING = set()          # task ids between create() and a running container: gc() must not reap them
_PROFILE = re.compile(r"[1-7]g\.\d{1,3}gb")
_UUID = re.compile(r"MIG-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class Unverified(RuntimeError):
    """MIG isolation for this rental could not be verified: refuse it."""


def _smi(*args, timeout=30):
    return subprocess.run(["nvidia-smi", *args], capture_output=True, text=True, timeout=timeout, check=False)


def gpus():
    """{index: name} of the NVIDIA GPUs currently in MIG mode; {} when none or no driver."""
    try:
        out = _smi("--query-gpu=index,name,mig.mode.current", "--format=csv,noheader", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in out.stdout.splitlines() if out.returncode == 0 else ():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[0].isdigit() and parts[2].lower() == "enabled":
            found[int(parts[0])] = parts[1]
    return found


def report():
    """Heartbeat capability: which GPU indexes can host per-rental slices. None = no MIG here."""
    found = gpus()
    return {"version": 1, "gpus": sorted(found)} if found else None


def _devices():
    """{MIG uuid: parent GPU index} from `nvidia-smi -L`."""
    out = _smi("-L", timeout=10)
    if out.returncode:
        raise Unverified("nvidia-smi -L failed")
    found, gpu = {}, None
    for line in out.stdout.splitlines():
        head = re.match(r"GPU (\d+):", line)
        if head:
            gpu = int(head.group(1))
            continue
        uuid = _UUID.search(line)
        if uuid and line.lstrip().startswith("MIG") and gpu is not None:
            found[uuid.group(0)] = gpu
    return found


def _instances(gpu):
    """GPU instance ids that exist on `gpu` (`nvidia-smi mig -lgi`), or None if it cannot be read."""
    out = _smi("mig", "-i", str(gpu), "-lgi", timeout=15)
    text = out.stdout + out.stderr
    if out.returncode:
        return set() if "no gpu instances found" in text.lower() else None
    # |   0  MIG 1g.10gb          19        9          0:1     |  -> GPU, name, profile id, instance id
    return {int(m.group(1)) for m in re.finditer(r"^\|\s*\d+\s+MIG\s+\S+\s+\d+\s+(\d+)\s", out.stdout, re.M)}


def _load():
    try:
        with open(STATE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)


def _boot():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _destroy(rec):
    """Destroy one recorded slice; True once its GPU instance is verifiably gone."""
    gpu, gi = int(rec["gpu"]), int(rec["gi"])
    if rec.get("boot") != _boot() or (rec.get("uuid") and rec["uuid"] not in _devices()):
        # Already gone: a reboot (or driver reload) drops every MIG instance, and its GPU instance id
        # may now belong to ANOTHER rental's new slice. Never destroy by a recycled id.
        return True
    # No recorded compute instance id: -dci without -ci removes every one in this GI, so -dgi can work.
    ci = ("-ci", str(rec["ci"])) if rec.get("ci") is not None else ()
    _smi("mig", "-i", str(gpu), "-gi", str(gi), *ci, "-dci")
    _smi("mig", "-i", str(gpu), "-gi", str(gi), "-dgi")
    left = _instances(gpu)
    return left is not None and gi not in left


def create(tid, gpu, profile):
    """Create task `tid`'s slice on GPU `gpu` and return its MIG uuid. Raises Unverified (after undoing
    any partial instance) when the GPU is not in MIG mode or the new slice cannot be identified."""
    if type(gpu) is not int or gpu < 0 or not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise Unverified("malformed MIG placement")
    key = str(tid)
    with _LOCK:
        state = _load()
        if key in state and state[key].get("uuid") in _devices():
            _PENDING.add(key)
            return state[key]["uuid"]            # the same task re-delivered: keep its slice
        if key in state and not _destroy(state.pop(key)):
            raise Unverified("a stale slice for this task could not be removed")
        if gpu not in gpus():
            raise Unverified(f"GPU {gpu} is not in MIG mode")
        before, gis_before = _devices(), _instances(gpu)
        out = _smi("mig", "-i", str(gpu), "-cgi", profile, "-C")
        gi = re.search(r"created GPU instance ID\s+(\d+)", out.stdout)
        ci = re.search(r"created compute instance ID\s+(\d+)", out.stdout)
        gi_id = int(gi.group(1)) if gi else None
        if gi_id is None and gis_before is not None:
            # Unrecognised wording (or a partial failure): still find the instance so it is not leaked.
            added = (_instances(gpu) or set()) - gis_before
            gi_id = added.pop() if len(added) == 1 else None
        if gi_id is not None:
            # Record it BEFORE anything else can fail, so a crash from here on is still GC'd.
            state[key] = {"gpu": gpu, "gi": gi_id, "ci": int(ci.group(1)) if ci else None,
                          "profile": profile, "boot": _boot()}
            _save(state)
        new = [u for u, g in _devices().items() if u not in before and g == gpu] if gi and ci else []
        if out.returncode or len(new) != 1:
            if key in state and _destroy(state[key]):
                del state[key]
                _save(state)
            why = (out.stderr or out.stdout).strip()[:200] if out.returncode else "the new slice was not identified"
            raise Unverified(f"could not create a {profile} slice on GPU {gpu}: {why}")
        state[key]["uuid"] = new[0]
        _save(state)
        _PENDING.add(key)
        return new[0]


def docker_args(uuid):
    """The only GPU flags a slice rental gets: exactly this MIG device, never --gpus all."""
    if not _UUID.fullmatch(uuid or ""):
        raise Unverified("malformed MIG uuid")
    return ["--gpus", f"device={uuid}", "-e", f"NVIDIA_VISIBLE_DEVICES={uuid}"]


def verify_container(name, uuid):
    """Inside the running container exactly one GPU and exactly this MIG device must be visible."""
    try:
        out = subprocess.run(["docker", "exec", name, "nvidia-smi", "-L"], capture_output=True,
                             text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise Unverified(f"could not list the container's GPUs: {e}") from None
    lines = out.stdout.splitlines()
    seen = [m.group(0) for line in lines for m in [_UUID.search(line)] if m and line.lstrip().startswith("MIG")]
    parents = [line for line in lines if re.match(r"GPU \d+:", line)]
    if out.returncode or seen != [uuid] or len(parents) != 1:
        raise Unverified(f"the container sees {len(seen)} MIG device(s) on {len(parents)} GPU(s), not only its slice")


def launched(tid):
    """The rental's container is running; from now on gc() reaps the slice once the container is gone."""
    _PENDING.discard(str(tid))


def destroy(tid, only_if_idle=False):
    """Tear down task `tid`'s slice (no-op if it has none). True when nothing of it is left.
    only_if_idle (gc): leave it alone if a launch claimed it, checked under the same lock as create()."""
    key = str(tid)
    with _LOCK:
        if only_if_idle and key in _PENDING:
            return False
        _PENDING.discard(key)
        state = _load()
        if key not in state:
            return True
        if not _destroy(state[key]):
            return False                         # keep the record: gc() retries
        del state[key]
        _save(state)
        return True


def live():
    """Task ids that currently hold a slice on this node."""
    return set(_load())


def gc():
    """Destroy recorded slices whose rental container no longer exists (agent crash, missed teardown).
    Called at startup and from the heartbeat. Returns the task ids it reaped."""
    reaped = []
    for key in live() - set(_PENDING):
        try:
            ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=pb.task={key}"],
                                 capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if ids.returncode == 0 and not ids.stdout.split() and destroy(key, only_if_idle=True):
            reaped.append(key)
    return reaped
