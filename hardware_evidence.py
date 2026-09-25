"""Bounded GPU software inventory. This does not resist a coherently malicious host root."""
import csv
import io
import os
import platform
import re
import subprocess
import uuid
from pathlib import Path

_SESSION = str(uuid.uuid4())


def _collect_nvidia():
    try:
        command = "/usr/bin/nvidia-smi" if os.name != "nt" else str(
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe")
        output = subprocess.run([command, "--query-gpu=uuid,name,memory.total,pci.bus_id,pci.device_id,driver_version",  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- fixed nvidia-smi path (list form, no shell); args are literal constants, no remote/buyer input
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                timeout=5, check=True).stdout
        if len(output) > 32768:
            return None
        rows = list(csv.reader(io.StringIO(output)))
        devices = []
        for row in rows:
            if len(row) != 6:
                return None
            gpu_uuid, model, memory, bus, device, driver = (v.strip() for v in row)
            devices.append(dict(uuid=gpu_uuid, model=model, vram_mb=float(memory),
                                pci_bus=bus, pci_device=device))
        display = subprocess.run([command], capture_output=True, text=True, timeout=5, check=True).stdout  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- fixed nvidia-smi path (list form, no shell); no remote/buyer input
        cuda = re.search(r"CUDA Version:\s*([0-9.]+)", display)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip() if os.name != "nt" else _SESSION
        return dict(version=1, session=_SESSION, boot_id=boot, kernel=platform.release(),
                    driver=driver, cuda=cuda.group(1) if cuda else "unavailable", devices=devices,
                    visibility={k:os.environ.get(k, "") for k in ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES")})
    except (OSError, ValueError, subprocess.SubprocessError, UnboundLocalError):
        return None


def _collect_amd():
    """AMD/ROCm hardware evidence via rocm-smi (uuid, model, vram, driver). Best-effort; None if
    rocm-smi is absent or its output is unusable. Mirrors the NVIDIA collector's shape."""
    try:
        out = subprocess.run(
            ["rocm-smi", "--showuniqueid", "--showproductname", "--showmeminfo", "vram",
             "--showdriverversion", "--json"],
            capture_output=True, text=True, timeout=5, check=True).stdout
        if len(out) > 32768:
            return None
        import json
        data = json.loads(out)
        if not isinstance(data, dict):
            return None
        driver, devices = "unavailable", []
        for card, info in data.items():
            if not isinstance(info, dict):
                continue
            if str(card).lower() == "system":
                driver = info.get("Driver version") or info.get("Driver Version") or driver
                continue
            if not str(card).lower().startswith("card"):
                continue
            uid = info.get("Unique ID") or info.get("GUID") or ""
            model = (info.get("Card series") or info.get("Card model")
                     or info.get("Card SKU") or "AMD GPU").strip()
            vram_mb = 0.0
            for k, v in info.items():
                if "vram total memory" in k.lower():
                    try:
                        vram_mb = float(int(v) / (1024 * 1024))
                    except Exception:
                        pass
            devices.append(dict(uuid=uid, model=model, vram_mb=vram_mb, pci_bus="", pci_device=""))
        if not devices:
            return None
        boot = (Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                if os.name != "nt" else _SESSION)
        return dict(version=1, session=_SESSION, boot_id=boot, kernel=platform.release(),
                    driver=driver, cuda="rocm", devices=devices,
                    visibility={k: os.environ.get(k, "")
                                for k in ("HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES")})
    except (OSError, ValueError, subprocess.SubprocessError, UnboundLocalError):
        return None


def collect():
    """Host GPU hardware evidence — NVIDIA first, then AMD/ROCm. None on a non-GPU host."""
    return _collect_nvidia() or _collect_amd()
