"""Node diagnostics: what the agent sends Petabyte support when its GPU checks fail.

A FIXED list of read-only checks: GPU driver + kernel modules + device nodes, GPU kernel errors,
Docker and NVIDIA container-runtime config, the CDI device list, a container CUDA test, and the
last agent log lines. It never reads agent.env contents, home directories or workload data, and
masks keys, emails and network/hardware addresses before anything leaves the machine. The server
only forwards the report to support by email; nothing here runs anything it receives.

Opt out: PB_SHARE_DIAGNOSTICS=false in /etc/petabyte/agent.env.
Standalone on purpose (no task_fetcher import) so `main.py debug` works on an unhealthy node.
"""
import json
import os
import re
import subprocess
import time
import urllib.request

import gpu_runtime
from agent_telemetry import RELEASE, _mask

ENV_FILE = "/etc/petabyte/agent.env"
MAX_CHARS = 150_000              # whole report; the server refuses more than 200k
_SECTION_MAX = 20_000

_MASKS = [
    (re.compile(r"://[^/\s:@]+:[^@\s]+@"), "://«credentials»@"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "«email»"),
    (re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"), "«mac»"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "«ip»"),
    (re.compile(r"\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){3,7}\b"), "«ip6»"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)(\"?\s*[:=]\s*)\S+"), r"\1\2«redacted»"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "«long-token»"),
]
# docker info lines that identify the machine or carry proxy credentials, not GPU state
_DROP_LINE = re.compile(r"^\s*(Name|ID|HTTP Proxy|HTTPS Proxy|No Proxy|Registry|Username):", re.I)


def redact(text):
    """Mask line by line, each line capped: the email pattern backtracks quadratically on a long
    '@'-less run, so an unbounded log line would stall the agent."""
    out = []
    for line in (text or "").splitlines():
        if _DROP_LINE.match(line):
            continue
        line = _mask(line[:2000])
        for pat, repl in _MASKS:
            line = pat.sub(repl, line)
        out.append(line)
    return "\n".join(out)


def _env():
    """Process env, filled from agent.env for `main.py debug` run from a shell (values used only
    to authenticate the upload; the file's contents never go into the report)."""
    env = dict(os.environ)
    try:
        with open(ENV_FILE) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and not k.startswith("#") and k not in env:
                    env[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _cached_cuda_image():
    for img in gpu_runtime.wipe_image_candidates():
        try:
            if img and subprocess.run(["docker", "image", "inspect", img],
                                      capture_output=True, timeout=10).returncode == 0:
                return img
        except Exception:                                    # noqa: BLE001
            return None
    return None


# Device nodes AND the libcuda the spec mounts: a spec generated for an older driver (rolling distros
# update it under Docker) gives "CUDA unknown error" in containers while host CUDA works (2026-09-24).
_CDI_DEVICES = ("ls -l /etc/cdi /var/run/cdi 2>&1; "
                "for f in /etc/cdi/*.yaml /etc/cdi/*.json /var/run/cdi/*.yaml /var/run/cdi/*.json; do "
                "[ -f \"$f\" ] && echo \"== $f\" && grep -E '/dev/|libcuda[.]so' \"$f\"; done; true")
_IN_CONTAINER = ("ls /dev | grep -iE 'nvidia|kfd|dri'; python -c \"import torch; "
                 "print('torch', torch.__version__, 'cuda', torch.version.cuda); "
                 "print('cuda_available', torch.cuda.is_available())\"")


def _checks(gpu_test):
    checks = [
        ("kernel", ["uname", "-r"], 20),
        ("os", ["sh", "-c", "grep -E '^(PRETTY_NAME|ID|VERSION_ID)=' /etc/os-release"], 20),
        ("nvidia-smi", ["nvidia-smi"], 30),
        ("gpu query", ["nvidia-smi", "--query-gpu=name,driver_version,pstate,memory.total,memory.used",
                       "--format=csv"], 30),
        ("rocm-smi", ["rocm-smi"], 30),
        ("gpu kernel modules", ["sh", "-c", "lsmod | grep -iE '^(nvidia|amdgpu)'"], 20),
        ("gpu device nodes", ["sh", "-c", "ls -l /dev/nvidia* /dev/kfd /dev/dri 2>&1"], 20),
        ("gpu kernel errors (dmesg)", ["sh", "-c", "dmesg 2>&1 | grep -iE 'nvrm|xid|nvidia|amdgpu' | tail -60"], 20),
        ("docker version", ["docker", "version", "--format", "{{.Server.Version}}"], 30),
        ("docker info", ["docker", "info"], 30),
        ("docker daemon.json", ["cat", "/etc/docker/daemon.json"], 20),
        ("nvidia container runtime config", ["sh", "-c", "grep -vE '^[[:space:]]*(#|$)' "
                                             "/etc/nvidia-container-runtime/config.toml"], 20),
        ("nvidia toolkit version", ["sh", "-c", "nvidia-ctk --version 2>&1 | head -3"], 20),
        ("CDI specs (device nodes + libcuda version)", ["sh", "-c", _CDI_DEVICES], 20),
        ("agent log (last 200 lines)", ["journalctl", "-u", "petabyte-agent", "-n", "200",
                                        "--no-pager", "-o", "cat"], 30),
    ]
    img = _cached_cuda_image() if gpu_test else None
    if img:        # the check that separates "host CUDA works" from "container CUDA works"
        checks.append((f"container GPU test ({img})",
                       ["docker", "run", "--rm", *gpu_runtime.docker_gpu_args(), "--network", "none",
                        img, "sh", "-c", _IN_CONTAINER], 180))
    elif gpu_test:
        checks.append(("container GPU test", ["echo", "skipped: no CUDA image cached on this node"], 5))
    return checks


def _run(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return f"[exit {r.returncode}]\n{out}"
    except FileNotFoundError:
        return "[not installed]"
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout}s]"
    except Exception as e:                                   # noqa: BLE001
        return f"[error: {type(e).__name__}]"


def collect(reason, spec_id="?", gpu_test=True):
    """The report as plain text. gpu_test=False when a rental may hold the GPU."""
    parts = [f"Petabyte node diagnostics\nagent {RELEASE} · spec {spec_id} · "
             f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\nreason: {reason}\n"]
    for title, cmd, timeout in _checks(gpu_test):
        raw = _run(cmd, timeout)
        if len(raw) > _SECTION_MAX:                          # bound BEFORE masking (cost is per char)
            raw = "…(truncated)\n" + raw[-_SECTION_MAX:]
        parts.append(f"===== {title} =====\n{redact(raw).rstrip()}\n")
    return "\n".join(parts)[:MAX_CHARS]


def send(reason, gpu_test=True, env=None):
    """Collect and upload. Returns (report, server reply), or (None, None) when opted out or
    unconfigured. Raises on network/HTTP errors (callers treat the report as best-effort)."""
    env = env if env is not None else _env()
    if env.get("PB_SHARE_DIAGNOSTICS", "true").strip().lower() == "false":
        return None, None
    url, key, spec = env.get("PETABYTE_API_URL"), env.get("PETABYTE_API_KEY"), env.get("PETABYTE_SPEC_ID")
    if not (url and key and spec):
        return None, None
    report = collect(reason, spec, gpu_test)
    req = urllib.request.Request(
        url.rstrip("/") + "/nodes/diagnostics", method="POST",
        data=json.dumps({"spec_id": int(spec), "reason": reason[:200], "report": report}).encode(),
        # Cloudflare 403s the default "Python-urllib" User-Agent before the request reaches us
        headers={"X-API-KEY": key, "Content-Type": "application/json",
                 "User-Agent": f"petabyte-agent/{RELEASE}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return report, json.load(r)
