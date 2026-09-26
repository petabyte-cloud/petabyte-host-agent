"""Versioned seller receipts. These authenticate a report, not its truth."""
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

_tasks = OrderedDict()
_lock = threading.RLock()
_scope_loaded = None
_STATE_MAX_AGE = 31 * 24 * 3600


def _state_identity():
    # Keep separate API endpoints, enrollment keys, specs and signing identities isolated.
    # Hash credentials; never write the enrollment key or signing key to the receipt store.
    names = ("PETABYTE_API_URL", "PETABYTE_API_KEY", "PETABYTE_SPEC_ID")
    values = [os.getenv(name, "") for name in names]
    if not all(values):
        return None, None
    import crypto
    key_path = getattr(crypto, "KEY_PATH", None)
    if not key_path:
        return None, None
    key = Path(key_path)
    try:
        raw = key.read_bytes()
    except OSError:
        return None, None
    scope = hashlib.sha256(json.dumps(values).encode() + raw).hexdigest()
    path = Path(os.getenv("PETABYTE_RECEIPT_STATE", str(key.with_name("assignments.json"))))
    return path, scope


def _restore():
    global _scope_loaded
    path, scope = _state_identity()
    if scope == _scope_loaded:
        now = time.time()
        for tid in list(_tasks):
            age = now - _tasks[tid].get("saved_at", 0)
            if not 0 <= age <= _STATE_MAX_AGE:
                _tasks.pop(tid, None)
        return path, scope
    _tasks.clear()
    _scope_loaded = scope
    if path is None or not path.exists():
        return path, scope
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            return path, scope
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            return path, scope
        stored = json.loads(path.read_text())
        if not isinstance(stored, dict) or stored.get("scope") != scope:
            return path, scope
        now = time.time()
        for tid, binding in stored.get("tasks", [])[-4096:]:
            age = now - float(binding["saved_at"])
            if 0 <= age <= _STATE_MAX_AGE:
                _tasks[int(tid)] = binding
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        _tasks.clear()
    return path, scope


def _persist(path, scope):
    if path is None:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("receipt state path must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".assignments-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({"scope": scope, "tasks": list(_tasks.items())}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def knows(tid):
    with _lock:
        _restore()
        return int(tid) in _tasks


def forget(tid):
    with _lock:
        path, scope = _restore()
        _tasks.pop(int(tid), None)
        _persist(path, scope)


def remember(task):
    with _lock:
        path, scope = _restore()
        _tasks[int(task["task_id"])] = {
            "spec_id": task.get("spec_id"), "assignment": task.get("assignment", ""),
            "lease_generation": task.get("lease_generation", 0), "saved_at": time.time()}
        _tasks.move_to_end(int(task["task_id"]))
        while len(_tasks) > 4096:
            _tasks.popitem(last=False)
        _persist(path, scope)


def generation(tid):
    with _lock:
        _restore()
        return _tasks.get(int(tid), {}).get("lease_generation", 0)


def make(tid, *, status="completed", result=None, content_hash=None, output_hash=None):
    with _lock:
        _restore()
        binding = dict(_tasks.get(int(tid), {}))
    proof = {
        "version": 2, "task_id": int(tid), "spec_id": binding.get("spec_id"),
        "assignment": binding.get("assignment", ""), "status": status,
        "ts": int(time.time()),
        "result_digest": hashlib.sha256(json.dumps(result, ensure_ascii=False,
                                      separators=(",", ":")).encode()).hexdigest(),
        "output_hash": output_hash or hashlib.sha256(str(result or status).encode()).hexdigest(),
    }
    if content_hash:
        proof["content_hash"] = content_hash
    elif isinstance(result, str):
        proof["content_hash"] = hashlib.sha256(result.encode()).hexdigest()
    return proof
