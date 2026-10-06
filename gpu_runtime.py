"""GPU vendor runtime abstraction — ONE place that decides how to expose the host GPU to a
container, which base image to run, and how to reset VRAM, so the rest of the agent stays vendor-
agnostic.

NVIDIA behaviour is byte-identical to before (`--gpus all` + the same CUDA images). AMD/ROCm adds
the /dev/kfd + /dev/dri device passthrough and ROCm images. ROCm's PyTorch keeps the `torch.cuda`
API, so the benchmark GEMM and the VRAM-wipe code run UNCHANGED — only the image + device flags
differ. CPU hosts get no GPU args.

`GPU_VENDOR=nvidia|amd|cpu` overrides detection (tests / manual seller override). Nothing here runs
a container or shells out beyond `shutil.which`.
"""
import functools
import os
import shutil


# WSL2 ships nvidia-smi and libcuda in /usr/lib/wsl/lib, which is on an interactive shell's PATH but NOT
# on the agent's systemd service PATH. Without it vendor() said "cpu" on every Windows node: no
# `--gpus all`, the mandatory VRAM wipe "failed" instantly and every GPU rental was refused (prod spec
# 253/265, RTX 4080 on WSL2, 2026-10-02). Appending it to the PROCESS env fixes detection and every
# child (nvidia-smi, docker probes, diagnostics) at once; on non-WSL hosts the dir doesn't exist.
WSL_LIB = "/usr/lib/wsl/lib"


def ensure_wsl_path(wsl_lib=WSL_LIB):
    path = os.environ.get("PATH", "")
    if os.path.isdir(wsl_lib) and wsl_lib not in path.split(os.pathsep):
        os.environ["PATH"] = path + os.pathsep + wsl_lib if path else wsl_lib


ensure_wsl_path()


def _has(cmd):
    return shutil.which(cmd) is not None


@functools.lru_cache(maxsize=1)
def vendor():
    """'nvidia' | 'amd' | 'cpu'. Env GPU_VENDOR wins; else detect by the installed tool.
    Cached — call vendor.cache_clear() in tests that flip GPU_VENDOR."""
    v = (os.getenv("GPU_VENDOR") or "").strip().lower()
    if v in ("nvidia", "amd", "cpu"):
        return v
    if _has("nvidia-smi"):
        return "nvidia"
    if _has("rocminfo") or _has("rocm-smi"):
        return "amd"
    return "cpu"


def has_gpu():
    return vendor() in ("nvidia", "amd")


def docker_gpu_args():
    """Docker args that expose the host GPU to a container, for the detected vendor.

    NVIDIA: the nvidia-container-toolkit runtime (`--gpus all`). AMD: the KFD compute device + DRI
    render nodes and the `video` group. CPU: nothing.

    Security (2026-10-06 audit): AMD jobs keep Docker's DEFAULT seccomp profile. Earlier this path
    shipped `seccomp=unconfined`, which handed an untrusted buyer container the full host syscall
    surface. ROCm/HIP works under the default profile on current runtimes; a node that genuinely
    needs the wider surface can opt back in with AGENT_AMD_SECCOMP_UNCONFINED=true (fail-safe: the
    default is confined, and a job that cannot init just fails the node's GPU self-test)."""
    v = vendor()
    if v == "nvidia":
        return ["--gpus", "all"]
    if v == "amd":
        args = ["--device", "/dev/kfd", "--device", "/dev/dri", "--group-add", "video"]
        if (os.getenv("AGENT_AMD_SECCOMP_UNCONFINED") or "").strip().lower() in ("1", "true", "yes", "on"):
            args += ["--security-opt", "seccomp=unconfined"]
        return args
    return []


# Torch runtime image per vendor — used by the FP16 benchmark GEMM and the VRAM wipe. ROCm's torch
# exposes the same torch.cuda API, so the workload code is identical across vendors.
_TORCH = {"nvidia": "pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime",
          "amd": "rocm/pytorch:latest"}

# Default GPU base images per role. Operators still override via task["image"] or the env knobs;
# these are the vendor-correct fallbacks the agent picks when none is set.
_IMAGES = {
    "notebook": {"nvidia": "quay.io/jupyter/pytorch-notebook:cuda12-latest",
                 "amd": "rocm/jupyter-pytorch:latest"},
    "ffmpeg": {"nvidia": "jrottenberg/ffmpeg:6.1-nvidia",
               "amd": "jrottenberg/ffmpeg:6.1-vaapi"},
}


def torch_image():
    return _TORCH.get(vendor(), _TORCH["nvidia"])


def image_for(role, override=None):
    """The base image for `role`: an explicit override (env/task) wins, else the vendor default."""
    if override:
        return override
    m = _IMAGES.get(role, {})
    return m.get(vendor()) or m.get("nvidia")


def wipe_image_candidates():
    """Locally-cached image candidates for the VRAM memset, vendor-first, plus VRAM_WIPE_IMAGE."""
    if vendor() == "amd":
        base = ["rocm/pytorch:latest"]
    else:
        # CUDA 12.8 first: it is the only one with kernels for Blackwell (RTX 50-series, sm_120).
        # install.sh caches exactly one of these by compute capability, so older cards fall through.
        base = ["pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime",
                "pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime",
                "pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime"]
    return [i for i in base + [os.getenv("VRAM_WIPE_IMAGE", "")] if i]


def gpu_reset_cmd():
    """Best-effort driver-level GPU reset command for the vendor, or None if the tool is absent."""
    v = vendor()
    if v == "nvidia" and _has("nvidia-smi"):
        return ["nvidia-smi", "--gpu-reset"]
    if v == "amd" and _has("rocm-smi"):
        return ["rocm-smi", "--gpureset"]
    return None
