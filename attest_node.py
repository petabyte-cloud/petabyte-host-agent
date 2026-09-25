#!/usr/bin/env python3
"""Attest this node at POST /prove using the agent's signing identity.

Run once after registering a spec, before serving jobs:
  PETABYTE_API_URL=... PETABYTE_API_JWT=<seller JWT> SPEC_ID=<id> python attest_node.py
"""
import os, time, base64, subprocess, httpx, crypto


def _hw_identity() -> dict:
    """Best-effort GPU identity (uuid/pci/driver/model/count) to PIN server-side. The server
    compares this across attestations and revokes verification if it changes (anti tamper/swap).
    nvidia-smi is seller-controlled so this is evidence, not proof — but a pinned fingerprint still
    makes 'verify once, swap later' detectable. Empty dict on a CPU node or if nvidia-smi is absent."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid,pci.bus_id,name,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return {}
    uuids, pcis, names, drivers = [], [], [], []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4 and parts[0]:
            uuids.append(parts[0]); pcis.append(parts[1]); names.append(parts[2]); drivers.append(parts[3])
    if not uuids:
        return {}
    cuda = ""
    try:
        smi = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10).stdout
        import re
        m = re.search(r"CUDA Version:\s*([0-9.]+)", smi)
        cuda = m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        pass
    return {"gpu_uuid": uuids, "pci_bus_id": pcis, "driver_version": drivers[0] if drivers else "",
            "cuda_version": cuda, "gpu_model": names[0] if names else "", "gpu_count": len(uuids)}


def main():
    api_url = os.environ["PETABYTE_API_URL"]
    jwt = os.environ["PETABYTE_API_JWT"]      # seller JWT (owner of the spec)
    spec_id = int(os.environ["SPEC_ID"])
    att = {"node": "petabyte-agent", "nonce": base64.b64encode(os.urandom(9)).decode(),
           "ts": int(time.time())}
    att.update(_hw_identity())                 # pin GPU uuid/pci/driver/cuda (signed below)
    r = httpx.post(f"{api_url}/prove", headers={"Authorization": f"Bearer {jwt}"},
                   json={"spec_id": spec_id, "attestation": att,
                         "signature": crypto.sign_proof(att),
                         "pubkey": crypto.public_key_b64()}, timeout=15)
    print(r.status_code, r.text)


if __name__ == "__main__":
    main()
