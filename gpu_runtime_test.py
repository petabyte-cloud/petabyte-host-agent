"""gpu_runtime vendor abstraction — offline. Asserts NVIDIA behaviour is byte-identical to the old
hardcoded values (regression guard) and that AMD gets the ROCm device passthrough + images."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_runtime as gr

_fail = 0


def ok(name, cond):
    global _fail
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


def as_vendor(v):
    os.environ["GPU_VENDOR"] = v
    gr.vendor.cache_clear()


# ---- NVIDIA: must be byte-identical to the old hardcoded behaviour ----
as_vendor("nvidia")
ok("nvidia vendor", gr.vendor() == "nvidia")
ok("nvidia docker args == the old ['--gpus','all']", gr.docker_gpu_args() == ["--gpus", "all"])
ok("nvidia torch image unchanged", gr.torch_image() == "pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime")
ok("nvidia notebook image unchanged", gr.image_for("notebook") == "quay.io/jupyter/pytorch-notebook:cuda12-latest")
ok("nvidia ffmpeg image unchanged", gr.image_for("ffmpeg") == "jrottenberg/ffmpeg:6.1-nvidia")
ok("nvidia has_gpu", gr.has_gpu())

# ---- AMD: ROCm passthrough + images ----
as_vendor("amd")
args = gr.docker_gpu_args()
ok("amd exposes /dev/kfd", "/dev/kfd" in args)
ok("amd exposes /dev/dri", "/dev/dri" in args)
ok("amd adds the video group", "video" in args)
ok("amd does NOT use --gpus (that is nvidia-only)", "--gpus" not in args)
ok("amd torch image is rocm", "rocm" in gr.torch_image())
ok("amd notebook image is rocm", "rocm" in gr.image_for("notebook"))
ok("amd has_gpu", gr.has_gpu())
ok("amd wipe candidates are rocm", all("rocm" in i for i in gr.wipe_image_candidates() if i != os.getenv("VRAM_WIPE_IMAGE", "")))

# ---- override precedence + cpu ----
ok("image_for honours an explicit override", gr.image_for("notebook", "myrepo/custom:tag") == "myrepo/custom:tag")
as_vendor("cpu")
ok("cpu -> no gpu args", gr.docker_gpu_args() == [])
ok("cpu -> not has_gpu", not gr.has_gpu())
ok("cpu -> no reset cmd", gr.gpu_reset_cmd() is None)

os.environ.pop("GPU_VENDOR", None)
gr.vendor.cache_clear()
print("\n=== gpu_runtime: " + ("0 failures" if _fail == 0 else str(_fail) + " FAILED") + " ===")
raise SystemExit(1 if _fail else 0)
