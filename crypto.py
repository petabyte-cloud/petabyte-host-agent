"""Agent signing identity (Ed25519).

The SAME key is used to (a) attest the node at POST /prove and (b) sign every
job result. The API verifies result signatures against the pubkey registered at
attestation, binding results to this node's hardware.
"""
import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# WHERE the key lives matters because petabyte-agent.service runs with ProtectHome=true, which
# makes $HOME unreachable: with the old `~/.petabyte` default every job that touched the key died
# with "[Errno 30] Read-only file system: '/root/.petabyte'". Worse, that killed the container
# WATCHDOG too, so a job whose container had exited kept being reported as running.
#
# Default to the StateDirectory the unit already creates (and already uses for
# PETABYTE_RECEIPT_STATE), which systemd puts in ReadWritePaths. Nodes provisioned before this
# keep their existing key — and therefore their attested identity — via the legacy fallback.
_STATE_KEY = "/var/lib/petabyte-agent/agent_ed25519.key"
_LEGACY_KEY = os.path.expanduser("~/.petabyte/agent_ed25519.key")
KEY_PATH = os.getenv("PETABYTE_AGENT_KEY",
                     _LEGACY_KEY if os.path.exists(_LEGACY_KEY) else _STATE_KEY)


def load_or_create_key() -> Ed25519PrivateKey:
    if os.path.exists(KEY_PATH):
        raw = base64.b64decode(open(KEY_PATH).read().strip())
        return Ed25519PrivateKey.from_private_bytes(raw)
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    key = Ed25519PrivateKey.generate()
    # Create the private-key file 0600 FROM THE START (O_CREAT with mode) rather than open()-then-
    # chmod, which leaves a brief window where the key is world/group-readable at the default umask.
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(key.private_bytes_raw()).decode())
    os.chmod(KEY_PATH, 0o600)   # belt-and-suspenders if the file pre-existed with looser perms
    return key


def public_key_b64() -> str:
    return base64.b64encode(load_or_create_key().public_key().public_bytes_raw()).decode()


def sign_proof(proof: dict) -> str:
    key = load_or_create_key()
    msg = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(key.sign(msg)).decode()


def sha256_hex(data) -> str:
    if not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def compute_test_hash(size: int, seed: int) -> str:
    """MUST match the server's db.compute_test_hash exactly (integer-deterministic)."""
    MOD = (1 << 61) - 1
    a = (seed % MOD) or 1
    acc = 0
    for i in range(size):
        a = (a * 6364136223846793005 + 1442695040888963407) % MOD
        acc = (acc + a * (i + 1)) % MOD
    return hashlib.sha256(str(acc).encode()).hexdigest()
