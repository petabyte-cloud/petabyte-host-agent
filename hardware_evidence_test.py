"""hardware_evidence finds nvidia-smi on WSL2 too, so a WSL node reports its GPU UUIDs."""
import os
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hardware_evidence as he  # noqa: E402

FAILS = []


def ok(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


ROW = "GPU-11111111-2222-3333-4444-555555555555, NVIDIA GeForce RTX 4080, 16376, 00000000:01:00.0, 0x270410DE, 560.94\n"
calls = []


def fake_run(cmd, **kw):
    calls.append(cmd[0])
    out = ROW if len(cmd) > 1 else "| NVIDIA-SMI 560.94   CUDA Version: 12.6 |"
    return types.SimpleNamespace(stdout=out, returncode=0)


if os.name != "nt":
    wsl = "/usr/lib/wsl/lib/nvidia-smi"
    with mock.patch.object(he.os.path, "exists", side_effect=lambda p: p == wsl), \
            mock.patch.object(he.subprocess, "run", side_effect=fake_run), \
            mock.patch.object(he.Path, "read_text", return_value="boot-id"):
        inv = he._collect_nvidia()
    ok("WSL2: nvidia-smi is found in /usr/lib/wsl/lib (fixed path, no PATH lookup)", set(calls) == {wsl})
    ok("...and the inventory carries the GPU UUID",
       inv is not None and inv["devices"][0]["uuid"] == "GPU-11111111-2222-3333-4444-555555555555")
    calls.clear()
    with mock.patch.object(he.os.path, "exists", side_effect=lambda p: p == "/usr/bin/nvidia-smi"), \
            mock.patch.object(he.subprocess, "run", side_effect=fake_run), \
            mock.patch.object(he.Path, "read_text", return_value="boot-id"):
        he._collect_nvidia()
    ok("a normal Linux host still uses /usr/bin/nvidia-smi", set(calls) == {"/usr/bin/nvidia-smi"})

print(f"\n=== hardware_evidence: {len(FAILS)} failures ===")
sys.exit(1 if FAILS else 0)
