"""confidential_detect.py — safe host inspection for confidential-computing (TEE) capability.

The node agent calls detect_confidential() and reports the result to Petabyte (heartbeat +
registration). This module NEVER decides whether a machine "is confidential" — it only reports
what the host advertises. The authoritative decision is made server-side by real remote
attestation (see lumaris_api/attestation.py + docs/CONFIDENTIAL_COMPUTING.md). A seller saying
"I have SEV-SNP" is a *reported* capability, not proof.

Design rules:
  * SAFE probing — every probe is wrapped; a missing utility / unreadable path yields a safe
    "unknown/unsupported", never a crash. No optional tool (mokutil, tpm2-tools, nvidia-smi) is
    required to be installed.
  * Report `supported` (hardware/firmware can do it) separately from `enabled` (it is actually
    turned on now). A capability that is supported-but-off is not confidential.
  * GPU confidential computing is claimed ONLY for datacenter-class CC GPUs (H100/H200/B200/…);
    a gaming card (RTX 3090/4090/5090) NEVER reports GPU confidential compute.
  * Dependency injection (_read/_run/_exists) so the whole thing is unit-testable offline.

DEV MODE: with CONFIDENTIAL_COMPUTING_DEV_MODE=true, and only when no real capability is
detected, a simulated capability set is returned with dev_mode=True and an explicit
"DEVELOPMENT ONLY" note. It must never be presented as real hardware.
"""
from __future__ import annotations

import os
import subprocess

# Datacenter GPUs that support NVIDIA Confidential Computing. Gaming cards are deliberately absent.
_GPU_CC_FAMILIES = ("H100", "H200", "H800", "B200", "B100", "GB200", "GH200")

_DEV_ONLY = "DEVELOPMENT ONLY — simulated capabilities, NOT real hardware, NOT production trusted"


# ---------------------------------------------------------------- safe I/O primitives
def _default_read(path: str):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def _default_exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except Exception:
        return False


def _default_run(args):
    """Run a command, return stdout (str) or None. Never raises, never blocks forever."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if out.returncode != 0 and not out.stdout:
            return None
        return out.stdout
    except Exception:
        return None


# ---------------------------------------------------------------- individual probes
def _cpu_info(read):
    """(vendor, model) from /proc/cpuinfo, plus the raw flags string (lowercased)."""
    vendor = model = None
    flags = ""
    txt = read("/proc/cpuinfo") or ""
    for line in txt.splitlines():
        low = line.lower()
        if vendor is None and low.startswith("vendor_id"):
            vendor = line.split(":", 1)[-1].strip()
        elif model is None and low.startswith("model name"):
            model = line.split(":", 1)[-1].strip()
        elif not flags and low.startswith("flags"):
            flags = low.split(":", 1)[-1].strip()
    # map raw vendor_id to a friendly name
    if vendor:
        if "AMD" in vendor:
            vendor = "AMD"
        elif "Intel" in vendor:
            vendor = "Intel"
    return vendor, model, flags


def _sev_snp(read, exists, flags):
    """AMD SEV-SNP. supported: CPU flag or kvm_amd module param present. enabled: host param on."""
    supported = ("sev_snp" in flags) or ("sev-snp" in flags)
    enabled = False
    param = read("/sys/module/kvm_amd/parameters/sev_snp")
    if param is not None:
        supported = True
        enabled = param.strip().upper() in ("Y", "1", "TRUE")
    # a running SEV-SNP guest exposes /dev/sev-guest
    if exists("/dev/sev-guest"):
        supported = True
        enabled = True
    return {"supported": bool(supported), "enabled": bool(enabled)}


def _tdx(read, exists, flags):
    """Intel TDX. supported: CPU flag or kvm_intel param. enabled: host param on / guest dev."""
    supported = ("tdx" in flags) or ("tdx_host_platform" in flags)
    enabled = False
    param = read("/sys/module/kvm_intel/parameters/tdx")
    if param is not None:
        supported = True
        enabled = param.strip().upper() in ("Y", "1", "TRUE")
    if exists("/sys/firmware/tdx") or exists("/dev/tdx_guest") or exists("/sys/module/tdx"):
        supported = True
        enabled = enabled or exists("/dev/tdx_guest")
    return {"supported": bool(supported), "enabled": bool(enabled)}


def _secure_boot(read, exists, run):
    """UEFI Secure Boot state. Prefers mokutil; falls back to the EFI var; then UEFI presence."""
    out = run(["mokutil", "--sb-state"])
    if out:
        low = out.lower()
        if "enabled" in low:
            return True
        if "disabled" in low:
            return False
    # EFI var: SecureBoot-<guid>; the value's last byte is 1 when enabled.
    try:
        base = "/sys/firmware/efi/efivars"
        if exists(base):
            for name in (os.listdir(base) if hasattr(os, "listdir") else []):
                if name.startswith("SecureBoot-"):
                    data = _default_read(os.path.join(base, name))
                    if data:
                        return data.encode("utf-8", "replace")[-1] == 1
    except Exception:
        pass
    return None  # unknown (not UEFI, or unreadable)


def _tpm(exists):
    """TPM present if a TPM device or sysfs class node exists."""
    for p in ("/dev/tpm0", "/dev/tpmrm0", "/sys/class/tpm/tpm0"):
        if exists(p):
            return True
    return False


def _gpu_cc(run):
    """NVIDIA GPU confidential compute. Datacenter CC GPUs only; gaming cards never qualify."""
    model = None
    count = 0
    out = run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if out:
        names = [n.strip() for n in out.splitlines() if n.strip()]
        count = len(names)
        model = names[0] if names else None
    up = (model or "").upper()
    cc_capable = any(fam in up for fam in _GPU_CC_FAMILIES)
    supported = enabled = False
    if cc_capable:
        supported = True
        # `nvidia-smi conf-compute -f` prints the CC status (ON/OFF) on CC-capable platforms.
        cc = run(["nvidia-smi", "conf-compute", "-f"]) or run(["nvidia-smi", "conf-compute", "--get-mode"])
        if cc:
            low = cc.lower()
            enabled = ("on" in low) or ("enabled" in low) or ("cc status: on" in low)
    return {"model": model, "count": count,
            "confidential_compute": {"supported": bool(supported), "enabled": bool(enabled)}}


# ---------------------------------------------------------------- public API
def detect_confidential(*, dev_mode=None, read=None, run=None, exists=None) -> dict:
    """Return the host's REPORTED confidential-computing capabilities as a dict:

        {"cpu": {"vendor","model","sev_snp":{supported,enabled}},
         "tdx": {supported,enabled}, "secure_boot": bool|None, "tpm": bool,
         "gpu": {"model","count","confidential_compute":{supported,enabled}},
         "attestation": {"supported": bool},
         "confidential_level": NONE|CPU_CONFIDENTIAL|GPU_CONFIDENTIAL|FULL_CONFIDENTIAL,
         "dev_mode": bool, "note": <present only in dev mode>}

    Never raises. Missing utilities degrade to unsupported/unknown."""
    read = read or _default_read
    run = run or _default_run
    exists = exists or _default_exists
    if dev_mode is None:
        dev_mode = os.getenv("CONFIDENTIAL_COMPUTING_DEV_MODE", "").strip().lower() in ("1", "true", "yes")

    vendor = model = None
    flags = ""
    try:
        vendor, model, flags = _cpu_info(read)
    except Exception:
        pass
    try:
        sev = _sev_snp(read, exists, flags)
    except Exception:
        sev = {"supported": False, "enabled": False}
    try:
        tdx = _tdx(read, exists, flags)
    except Exception:
        tdx = {"supported": False, "enabled": False}
    try:
        sb = _secure_boot(read, exists, run)
    except Exception:
        sb = None
    try:
        tpm = _tpm(exists)
    except Exception:
        tpm = False
    try:
        gpu = _gpu_cc(run)
    except Exception:
        gpu = {"model": None, "count": 0, "confidential_compute": {"supported": False, "enabled": False}}

    gpu_cc = gpu.get("confidential_compute", {})
    att_supported = bool(sev["supported"] or tdx["supported"] or gpu_cc.get("supported"))
    level = _level_from(sev["enabled"], tdx["enabled"], gpu_cc.get("enabled"))

    caps = {
        "cpu": {"vendor": vendor, "model": model, "sev_snp": sev},
        "tdx": tdx,
        "secure_boot": sb,
        "tpm": tpm,
        "gpu": gpu,
        "attestation": {"supported": att_supported},
        "confidential_level": level,
        "dev_mode": False,
    }

    # DEV MODE: only simulate when nothing real was found, and mark it unmistakably.
    if dev_mode and level == "NONE" and not att_supported:
        return _dev_mode_caps(vendor, model, gpu.get("model"), gpu.get("count", 0))
    return caps


def _level_from(sev_enabled, tdx_enabled, gpu_cc_enabled) -> str:
    cpu = bool(sev_enabled or tdx_enabled)
    gpu = bool(gpu_cc_enabled)
    if cpu and gpu:
        return "FULL_CONFIDENTIAL"
    if cpu:
        return "CPU_CONFIDENTIAL"
    if gpu:
        return "GPU_CONFIDENTIAL"
    return "NONE"


def _dev_mode_caps(vendor, model, gpu_model, gpu_count) -> dict:
    """A simulated FULL-confidential capability set for local development. Explicitly flagged so
    it can never be mistaken for, or presented as, real hardware."""
    return {
        "cpu": {"vendor": vendor or "AMD", "model": model or "EPYC 9654 (simulated)",
                "sev_snp": {"supported": True, "enabled": True}},
        "tdx": {"supported": False, "enabled": False},
        "secure_boot": True,
        "tpm": True,
        "gpu": {"model": gpu_model or "NVIDIA H100 (simulated)", "count": gpu_count or 1,
                "confidential_compute": {"supported": True, "enabled": True}},
        "attestation": {"supported": True},
        "confidential_level": "FULL_CONFIDENTIAL",
        "dev_mode": True,
        "note": _DEV_ONLY,
    }


def summarize(caps: dict) -> str:
    """One-line human summary for the agent `doctor` / UI."""
    if not caps:
        return "Confidential computing: unknown"
    if caps.get("dev_mode"):
        return f"Confidential computing: {caps.get('confidential_level')} (DEV MODE — simulated, not real)"
    lvl = caps.get("confidential_level", "NONE")
    if lvl == "NONE":
        return "Confidential computing: not available on this machine"
    bits = []
    if (caps.get("cpu", {}).get("sev_snp") or {}).get("enabled"):
        bits.append("SEV-SNP")
    if (caps.get("tdx") or {}).get("enabled"):
        bits.append("TDX")
    if (caps.get("gpu", {}).get("confidential_compute") or {}).get("enabled"):
        bits.append("GPU TEE")
    return f"Confidential computing: {lvl} (" + ", ".join(bits) + ") — reported, pending attestation"


if __name__ == "__main__":  # pragma: no cover - manual probe
    import json
    print(json.dumps(detect_confidential(), indent=2))
    print(summarize(detect_confidential()))
