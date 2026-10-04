"""The FP16 benchmark runs in the locally cached CUDA image (no host torch needed); host torch is the
fallback, and an old install's host torch is removed once the image has worked (2026-10-04)."""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def ok(label, cond):
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf  # noqa: E402

IMG = "pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime"
calls, dropped = [], []


def runner(docker_ok=True, host_ok=True):
    def run(cmd, **kw):
        calls.append(cmd)
        good = docker_ok if cmd[0] == "docker" else host_ok
        return types.SimpleNamespace(returncode=0 if good else 1, stdout="1.5\n" if good else "")
    return run


tf._drop_host_torch = lambda: dropped.append(1)
tf.shutil = __import__("shutil")
real_which = tf.shutil.which
tf.shutil.which = lambda name: "/usr/bin/docker" if name == "docker" else real_which(name)
tf._cached_cuda_images = lambda: [IMG]
tf.gpu_runtime.vendor = lambda: "nvidia"
tf.gpu_runtime.docker_gpu_args = lambda: ["--gpus", "all"]

tf.subprocess.run = runner()
r = tf._measure_fp16_tflops(1024)
ok("NVIDIA with a cached image: the GEMM runs in that image, never the host",
   len(calls) == 1 and calls[0][0] == "docker" and IMG in calls[0])
ok("...never pulling and with no network", "--pull" in calls[0] and calls[0][calls[0].index("--pull") + 1] == "never"
   and calls[0][calls[0].index("--network") + 1] == "none")
ok("...and the result is computed from its timing", r is not None and r[1] == 1024)
ok("a successful in-image run lets the old host torch go", dropped == [1])

calls.clear(); dropped.clear()
tf.subprocess.run = runner(docker_ok=False)
r = tf._measure_fp16_tflops(1024)
ok("the image fails -> host torch fallback, and host torch is kept",
   [c[0] for c in calls] == ["docker", sys.executable] and r is not None and dropped == [])

calls.clear()
tf.gpu_runtime.vendor = lambda: "amd"
tf.subprocess.run = runner()
tf._measure_fp16_tflops(1024)
ok("AMD keeps the host (ROCm) torch path", [c[0] for c in calls] == [sys.executable])

calls.clear()
tf.gpu_runtime.vendor = lambda: "nvidia"
tf._cached_cuda_images = lambda: []
tf._measure_fp16_tflops(1024)
ok("no cached image -> host torch (a node that skipped the wipe image)", [c[0] for c in calls] == [sys.executable])

IMG2 = "pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime"
calls.clear(); dropped.clear()
tf._cached_cuda_images = lambda: [IMG2, IMG]


def first_image_fails(cmd, **kw):
    calls.append(cmd)
    good = cmd[0] == "docker" and IMG in cmd
    return types.SimpleNamespace(returncode=0 if good else 1, stdout="1.5\n" if good else "")


tf.subprocess.run = first_image_fails
r = tf._measure_fp16_tflops(1024)
ok("every cached image is tried in order before the host (CodeRabbit #776)",
   [IMG2 in c or IMG in c for c in calls] == [True, True] and IMG in calls[1] and r is not None)

calls.clear(); dropped.clear()
tf._cached_cuda_images = lambda: [IMG]


def docker_raises(cmd, **kw):
    calls.append(cmd)
    if cmd[0] == "docker":
        raise OSError("docker daemon gone")
    return types.SimpleNamespace(returncode=0, stdout="1.5\n")


tf.subprocess.run = docker_raises
r = tf._measure_fp16_tflops(1024)
ok("a container run that RAISES still falls back to host torch",
   [c[0] for c in calls] == ["docker", sys.executable] and r is not None and dropped == [])

# ---- an inspect that fails for one candidate does not hide the others (CodeRabbit #776)
import importlib  # noqa: E402
tf3 = importlib.reload(tf)
tf3.gpu_runtime.wipe_image_candidates = lambda: [IMG2, IMG]


def inspect_first_times_out(cmd, **kw):
    if IMG2 in cmd:
        raise tf3.subprocess.TimeoutExpired(cmd, 10)
    return types.SimpleNamespace(returncode=0)


tf3.subprocess.run = inspect_first_times_out
ok("a failed inspect of one image still finds the next cached one", tf3._cached_cuda_images() == [IMG])

# ---- the cleanup removes only torch's CUDA stack from the venv
import importlib  # noqa: E402
tf2 = importlib.reload(tf)
dists = [types.SimpleNamespace(metadata={"Name": n}) for n in
         ("torch", "triton", "nvidia-cublas-cu12", "nvidia-cudnn-cu13", "nvidia-ml-py", "httpx", "numpy")]
uninstalled, pip_rc = [], [1, 0]
import importlib.metadata as md  # noqa: E402
real_dists = md.distributions
md.distributions = lambda: dists
tf2.subprocess.run = lambda cmd, **kw: (uninstalled.append(cmd), types.SimpleNamespace(returncode=pip_rc.pop(0)))[1]
tf2._drop_host_torch()                       # pip fails: nothing recorded, so ...
tf2._drop_host_torch()                       # ... the next benchmark simply retries
md.distributions = real_dists
ok("cleanup uninstalls torch, triton and the nvidia-* CUDA wheels only (keeps nvidia-ml-py, numpy)",
   uninstalled and uninstalled[0][-5:] == ["-q", "nvidia-cublas-cu12", "nvidia-cudnn-cu13", "torch", "triton"])
ok("a failed uninstall is retried next time (no stale marker)", len(uninstalled) == 2)
md.distributions = lambda: [types.SimpleNamespace(metadata={"Name": "httpx"})]
uninstalled.clear()
tf2._drop_host_torch()
md.distributions = real_dists
ok("nothing to remove -> no pip call", uninstalled == [])

print(f"\n=== bench_image: {len(FAILS)} failures ===")
sys.exit(1 if FAILS else 0)
