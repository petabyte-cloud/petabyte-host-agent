"""common.py — the few things every guest module shares: the job failure type, redacting log, hashing.

Kept out of runner.py so adapters can import it without a circular import (and so `python runner.py`
as __main__ and `import runner` in tests see the same JobFailure class)."""
import hashlib
import os
import time

CHUNK = 1 << 20
# Written by the job user, read by the SYSTEM boot-diagnostics task (provision.ps1), which copies
# its tail to the EC2 serial console. Tokens are redacted before anything is written.
LOG_FILE = r"C:\ProgramData\Petabyte\logs\runner.log" if os.name == "nt" else None
_SECRETS = []          # strings that must never reach a log line (the relay token)


class JobFailure(Exception):
    """Terminal job failure; `cause` is a contract failure_cause, str(e) is buyer-safe."""

    def __init__(self, cause, message=""):
        super().__init__(message)
        self.cause = cause


def redact(msg) -> str:
    msg = str(msg)
    for s in _SECRETS:
        if s:
            msg = msg.replace(s, "<token>")
    return msg


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {redact(msg)}"
    print(line, flush=True)
    if LOG_FILE:                       # pythonw (the logon task) has no console: keep a file trace
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()
