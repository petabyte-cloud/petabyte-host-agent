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


def graphics_env():
    """Extra docker args for a GPU *graphics* (EGL) workload such as headless EEVEE. NVIDIA's
    container toolkit mounts only the compute+utility driver libraries by default, so EGL has no
    GPU driver and EEVEE cannot start; this asks it for the graphics libraries too. AMD's /dev/dri
    is already passed by docker_gpu_args().

    The toolkit mounts libEGL_nvidia, but GLVND only loads a vendor listed in an egl_vendor.d JSON,
    which NVIDIA's OpenGL images ship and generic images (linuxserver/blender) don't, so EGL fell
    back to Mesa llvmpipe (RTX 2060, 2026-10-07: Blender reported "llvmpipe | Mesa"). We mount that
    one-line JSON read-only and point GLVND at it, so EEVEE gets the NVIDIA driver or fails loudly.

    Vulkan (Blender's --gpu-backend vulkan, which needs no EGL) has the same gap: the image lists only
    Mesa's Vulkan ICDs, so NVIDIA's ICD file is mounted the same way and the loader pointed at it."""
    if vendor() == "nvidia":
        return ["-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics",
                "-v", f"{_nvidia_egl_vendor_json()}:/pb-egl/10_nvidia.json:ro",
                "-e", "__EGL_VENDOR_LIBRARY_FILENAMES=/pb-egl/10_nvidia.json",
                "-v", f"{_nvidia_vulkan_icd_json()}:/pb-egl/nvidia_icd.json:ro",
                "-e", "VK_DRIVER_FILES=/pb-egl/nvidia_icd.json",
                "-e", "VK_ICD_FILENAMES=/pb-egl/nvidia_icd.json",   # older Vulkan loaders
                # The image ships NVIDIA EGL *window-system* plugins (wayland/gbm/xcb/xlib) from its
                # own distro packages, which NVIDIA's libEGL loads at startup; built for other driver
                # series they can crash the host driver (2060, driver 550: segfault on both backends).
                # Headless rendering needs none of them, so point the loader at a dir with no configs.
                "-e", "__EGL_EXTERNAL_PLATFORM_CONFIG_DIRS=/pb-egl/no-platforms",
                *_nvidia_compat_lib_args()]
    return []


# Driver libraries an OLD nvidia-container-toolkit doesn't know to mount. libnvidia-gpucomp (driver
# 535+) is a hard dependency of libEGL_nvidia/libGLX_nvidia; toolkit 1.12.1 on the RTX 2060 (driver
# 550.67) left it out, so EEVEE segfaulted on both backends (2026-10-07). Mounted into a private dir
# on the library path, never over the toolkit's own mounts (newer toolkits mount it themselves).
_COMPAT_LIBS = ("libnvidia-gpucomp.so.",)
_LIB_DIRS = ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib")


@functools.lru_cache(maxsize=1)
def _nvidia_compat_lib_args():
    found = {}
    for d in _LIB_DIRS:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.startswith(_COMPAT_LIBS) and n[n.index(".so.") + 4:][:1].isdigit() and n not in found:
                found[n] = os.path.join(d, n)
    args = [a for n, p in sorted(found.items()) for a in ("-v", f"{p}:/pb-nvlib/{n}:ro")]
    return args + (["-e", "LD_LIBRARY_PATH=/pb-nvlib"] if found else [])


def _nvidia_egl_vendor_json():
    return _driver_json("petabyte-egl-",
                        '{"file_format_version": "1.0.0", "ICD": {"library_path": "libEGL_nvidia.so.0"}}\n')


def _nvidia_vulkan_icd_json():
    return _driver_json("petabyte-vk-", '{"file_format_version": "1.0.1", "ICD": '
                        '{"library_path": "libGLX_nvidia.so.0", "api_version": "1.3.0"}}\n')


@functools.lru_cache(maxsize=None)
def _driver_json(prefix, body):
    """Create a driver-discovery JSON once in the agent's Docker-visible TMPDIR."""
    import tempfile
    # mkstemp avoids a predictable filename/symlink in a shared temp directory. The systemd agent
    # sets TMPDIR=/var/lib/petabyte-agent so Docker sees this bind-mount source despite PrivateTmp.
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(path, 0o644)
    except Exception:
        os.unlink(path)
        raise
    return path


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
