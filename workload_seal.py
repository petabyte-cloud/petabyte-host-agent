"""workload_seal.py — software "sealed workload" crypto (defense-in-depth, NOT a TEE).

Goal (the buyer's ask): a buyer's workload should be ENCRYPTED in transit and at rest on the
seller's node, and decrypted ONLY in the agent's RAM. This RAISES THE BAR — a seller can no longer
`cat` the payload off disk or the job envelope — but it does not stop a root seller who dumps live
RAM (the AES key and the decrypted plaintext are both in the agent's memory). The only defense
against that is CONFIDENTIAL/TEE mode (attestation-gated key release, key_release.py). Use this for
STANDARD nodes as a cheap hardening layer.

Flow:
  1. Agent generates an EPHEMERAL RSA keypair in RAM (never written to disk) at startup and sends
     the PUBLIC key to the platform.
  2. Platform mints a per-node AES-256 key (stored Fernet-encrypted at rest on the platform),
     RSA-OAEP-wraps it to the node's public key, and returns the wrapped blob.
  3. Agent unwraps the AES key with its in-RAM private key and holds it in memory only.
  4. Platform AES-256-GCM-SEALS each dispatched workload (the project bundle) to that node's key.
  5. Agent UNSEALS in RAM and runs the workload from a RAM/encrypted scratch dir.

This module is dependency-light (only `cryptography`) so the SAME file can live on both the platform
and the agent.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_RSA_BITS = 3072
_OAEP = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None)


# ---- agent side: ephemeral keypair (kept in RAM only) --------------------------------------
def generate_node_keypair():
    """An ephemeral RSA private key (agent-side, in RAM only) and its public-key PEM (to send to
    the platform). The private key is never serialized to disk — losing it on restart is fine; the
    agent just re-registers and gets a fresh wrapped AES key."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_BITS)
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub_pem


def unwrap_aes_key(priv, wrapped_b64: str) -> bytes:
    """Agent: recover the AES key from the platform's RSA-wrapped blob, in memory."""
    return priv.decrypt(base64.b64decode(wrapped_b64), _OAEP)


# ---- platform side: mint + wrap the per-node AES key ---------------------------------------
def new_aes_key() -> bytes:
    """A fresh AES-256 key. The platform stores this Fernet-encrypted at rest and reuses it for a
    node until the node re-registers a new public key."""
    return AESGCM.generate_key(bit_length=256)


def wrap_aes_key(pub_pem: str, aes_key: bytes) -> str:
    """Platform: RSA-OAEP-wrap the AES key to the node's public key; base64 for transport."""
    pub = serialization.load_pem_public_key(pub_pem.encode())
    if not isinstance(pub, rsa.RSAPublicKey):
        raise ValueError("node public key is not RSA")
    return base64.b64encode(pub.encrypt(aes_key, _OAEP)).decode()


# ---- both sides: seal / unseal a payload (AES-256-GCM) -------------------------------------
def seal(aes_key: bytes, plaintext: bytes, aad: bytes | None = None) -> str:
    """AES-256-GCM encrypt -> base64(nonce(12) || ciphertext||tag). `aad` (e.g. the task id) binds
    the ciphertext to a context so a blob can't be replayed onto a different job."""
    nonce = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(nonce, plaintext, aad)
    return base64.b64encode(nonce + ct).decode()


def unseal(aes_key: bytes, blob_b64: str, aad: bytes | None = None) -> bytes:
    """Reverse of seal(); raises cryptography.exceptions.InvalidTag on tamper / wrong key / wrong
    aad (fail-closed — never returns attacker-controlled plaintext)."""
    raw = base64.b64decode(blob_b64)
    return AESGCM(aes_key).decrypt(raw[:12], raw[12:], aad)
