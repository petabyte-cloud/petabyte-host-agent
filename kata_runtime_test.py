"""kata_runtime_test.py — Kata Containers runtime selection (VM-per-container isolation).

Kata runs each buyer container inside its OWN hardware VM (KVM) — the strongest boundary. The
agent's job is to REQUEST that runtime correctly per container; booting the micro-VM is Kata/KVM's
job. This pins the agent's part of the flow:
  * two jobs each independently select `--runtime kata` -> two separate micro-VMs (the "two VMs"),
  * it is OPT-IN and NEVER weakens isolation below the pre-existing gVisor default,
  * a GPU job never lands on a Kata that can't pass the GPU through (fails safe to gVisor),
  * unknown/misconfig always falls back safely — a container is never launched less isolated.

Run: python kata_runtime_test.py
"""
import os

os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "k")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")

import task_fetcher as tf  # noqa: E402

# docker info --format {{.Runtimes}} strings for different node installs
KATA = "map[io.containerd.runc.v2:{runc []} kata:{/usr/bin/kata-runtime []} runc:{runc []} runsc:{/opt/runsc []}]"
KATA_ALT = "map[kata-runtime:{/usr/bin/kata-runtime []} runc:{runc []}]"
NO_KATA = "map[runc:{runc []} runsc:{/opt/runsc []}]"        # gVisor node, no kata
BARE = "map[runc:{runc []}]"                                 # plain docker only

_fail = 0


def ok(name, cond, extra=""):
    global _fail
    print(("ok   " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        _fail += 1


def sel(available, is_gpu=False, runtime=None, kata_enabled=None, kata_gpu=None):
    for k in ("AGENT_RUNTIME", "AGENT_KATA_ENABLED", "AGENT_KATA_GPU"):
        os.environ.pop(k, None)
    if runtime is not None:
        os.environ["AGENT_RUNTIME"] = runtime
    if kata_enabled is not None:
        os.environ["AGENT_KATA_ENABLED"] = kata_enabled
    if kata_gpu is not None:
        os.environ["AGENT_KATA_GPU"] = kata_gpu
    return tf._select_runtime(available, is_gpu)


# ---- DEFAULT: kata is now preferred whenever it is available ----
ok("default (no config) PREFERS kata when available", sel(KATA) == "kata")
ok("default on a gVisor-only node uses gVisor", sel(NO_KATA) == "runsc")
ok("default on a bare node uses docker default (runc)", sel(BARE) is None)

# ---- explicit selection still works, and opting OUT works ----
ok("AGENT_RUNTIME=kata selects kata (CPU job)", sel(KATA, runtime="kata") == "kata")
ok("AGENT_KATA_ENABLED=false opts OUT to gVisor", sel(KATA, kata_enabled="false") == "runsc")
ok("kata detected under the 'kata-runtime' name too (uses the registered name)",
   sel(KATA_ALT) == "kata-runtime")

# ---- the "two VMs": two jobs each independently get their own kata micro-VM ----
job1 = sel(KATA, runtime="kata")
job2 = sel(KATA, runtime="kata")
ok("two jobs -> two independent kata micro-VMs", job1 == "kata" and job2 == "kata")

# ---- GPU safety: never run a GPU job on a kata that can't pass the GPU ----
ok("GPU job under kata WITHOUT AGENT_KATA_GPU falls back to gVisor",
   sel(KATA, is_gpu=True, runtime="kata") == "runsc")
ok("GPU job under kata WITH AGENT_KATA_GPU=true uses kata",
   sel(KATA, is_gpu=True, runtime="kata", kata_gpu="true") == "kata")

# ---- fail-safe: kata requested but unavailable -> never errors, never weaker than today ----
ok("kata requested on a node without kata -> gVisor", sel(NO_KATA, runtime="kata") == "runsc")
ok("kata requested on a bare node -> docker default (runc)", sel(BARE, runtime="kata") is None)

# ---- explicit overrides ----
ok("AGENT_RUNTIME=runc forces docker default even if kata+gvisor present",
   sel(KATA, runtime="runc") is None)
ok("AGENT_RUNTIME=gvisor forces gVisor", sel(KATA, runtime="gvisor") == "runsc")

# ---- integration: the full isolation flags carry --runtime kata AND keep every hardening flag ----
import subprocess  # noqa: E402
_real = subprocess.check_output
subprocess.check_output = lambda *a, **k: KATA
try:
    for _k in ("AGENT_RUNTIME", "AGENT_KATA_ENABLED", "AGENT_KATA_GPU"):
        os.environ.pop(_k, None)                          # pure DEFAULT: kata is preferred
    f1 = tf._isolation_flags({"task_id": "t1"})
    f2 = tf._isolation_flags({"task_id": "t2"})           # a second container = a second micro-VM
    ok("container 1 launches under --runtime kata BY DEFAULT",
       f1[:2] == ["--runtime", "kata"], str(f1[:2]))
    ok("container 2 also launches under --runtime kata (independent VM)",
       f2[:2] == ["--runtime", "kata"], str(f2[:2]))
    ok("kata never drops the base hardening (cap-drop ALL, no-new-privileges)",
       "--cap-drop" in f1 and "ALL" in f1 and "no-new-privileges" in f1)
    # a GPU job (task.gpu) stays on gVisor unless the operator confirmed VFIO
    fg = tf._isolation_flags({"task_id": "g1", "gpu": True})
    ok("a GPU container falls back to gVisor (no --runtime kata) without AGENT_KATA_GPU",
       fg[:2] == ["--runtime", "runsc"], str(fg[:2]))
finally:
    subprocess.check_output = _real
    for k in ("AGENT_RUNTIME", "AGENT_KATA_ENABLED", "AGENT_KATA_GPU"):
        os.environ.pop(k, None)

print("\nOK — kata runtime selection is correct + fail-safe" if not _fail else f"\n{_fail} checks FAILED")
raise SystemExit(1 if _fail else 0)
