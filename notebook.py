"""Sandboxed notebook execution.

Untrusted buyer code is NEVER run on the host. It runs inside a locked-down,
throwaway Docker container: no network, dropped capabilities, no new privileges,
read-only rootfs, a small tmpfs, and hard CPU/RAM/PID limits. Output size is
capped so a malicious notebook can't OOM the host while we read results.
"""
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from typing import List, Union
import gpu_runtime

import nbformat

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "jupyter/base-notebook:latest")
# GPU batch notebooks need CUDA + torch; the default base image is CPU-only. Overridable so an
# operator can pin a smaller/pre-pulled CUDA notebook image.
SANDBOX_GPU_IMAGE = os.getenv("SANDBOX_GPU_IMAGE", "quay.io/jupyter/pytorch-notebook:cuda12-latest")
WALL_TIMEOUT = int(os.getenv("NB_TIMEOUT", "300"))          # outer hard kill (s)
CELL_TIMEOUT = int(os.getenv("NB_CELL_TIMEOUT", "120"))     # per-cell (s)
MAX_OUTPUT_BYTES = int(os.getenv("NB_MAX_OUTPUT", str(8 * 1024 * 1024)))  # 8 MB


def timed(func):
    def wrapper(*args, **kwargs):
        start = time.time()
        try:
            result = func(*args, **kwargs)
            return {"result": result, "duration_seconds": round(time.time() - start, 4)}
        except Exception as e:                       # noqa: BLE001
            logging.exception("Execution failed")
            return {"error": str(e), "duration_seconds": round(time.time() - start, 4)}
    return wrapper


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


@timed
def run_notebook_code(code: Union[str, List[str], dict], cpu: int = 1, ram: int = 2,
                      max_runtime_s: int = None, gpu: bool = False):
    """Execute notebook code in a sandboxed container. Returns a list of outputs.

    max_runtime_s is the buyer's AUTHORIZED runtime budget (audit H1): the container is hard-
    killed at min(NB_TIMEOUT, max_runtime_s) so a job can never consume more of the seller's GPU
    than the buyer paid to authorize. None => the default wall timeout only.

    gpu=True runs in the CUDA notebook image with --gpus all (batch notebook-by-link on a GPU
    node); the same lockdown flags apply — the GPU is the only thing added."""
    if not _docker_available():
        # SECURITY: never fall back to host execution for untrusted code.
        return [{"type": "error",
                 "value": "Sandbox unavailable: Docker is required to run tasks safely."}]

    # Build the notebook
    if isinstance(code, dict):
        nb = nbformat.from_dict(code)
    else:
        cells = [code] if isinstance(code, str) else list(code)
        nb = nbformat.v4.new_notebook()
        nb.cells = [nbformat.v4.new_code_cell(c) for c in cells]

    # Stage the notebook on a RAM-backed tmpfs (see agent_scratch): the buyer's input code AND the
    # executed output.ipynb (which carries their results) are plaintext only in RAM while the
    # container runs — never written to the seller's persistent disk.
    import agent_scratch
    scratch = agent_scratch.make_scratch("pb_nb_")       # 0700 outer
    try:
        # 0700 outer + 0777 leaf, exactly as the media runners in task_fetcher.py do it: the
        # sandbox image runs as an unprivileged user (jovyan) that must write output.ipynb back
        # into the bind mount, but a world-writable dir sitting directly in a world-traversable
        # /dev/shm or /tmp would let ANY other host account read the buyer's code and results (or
        # swap the outputs before we hash them). Docker bind-mounts the LEAF, so the container
        # reaches /work without ever traversing the 0700 parent.
        workdir = os.path.join(scratch, "work")
        os.mkdir(workdir)
        os.chmod(workdir, 0o777)
        in_path = os.path.join(workdir, "input.ipynb")
        out_path = os.path.join(workdir, "output.ipynb")
        with open(in_path, "w", encoding="utf-8") as f:
            nbformat.write(nb, f)

        container = "pb-nb-" + uuid.uuid4().hex[:12]
        cmd = [
            "docker", "run", "--rm", "--name", container,
            "--network", "none",                       # no exfiltration / LAN access
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",                              # immutable rootfs
            "--tmpfs", "/tmp:size=256m",
            "--pids-limit", "256",
            "--cpus", str(max(1, cpu)),
            "--memory", f"{max(1, ram)}g",
            "--memory-swap", f"{max(1, ram)}g",         # disable swap escape
            "-v", f"{workdir}:/work:rw",
            "-w", "/work",
        ]
        if gpu:
            cmd += gpu_runtime.docker_gpu_args()   # nvidia --gpus all / amd /dev/kfd+/dev/dri; caps stay dropped
        cmd += [
            gpu_runtime.image_for("notebook", os.getenv("SANDBOX_GPU_IMAGE")) if gpu else SANDBOX_IMAGE,
            "jupyter", "nbconvert", "--to", "notebook", "--execute",
            f"--ExecutePreprocessor.timeout={CELL_TIMEOUT}",
            "--output", "output.ipynb", "input.ipynb",
        ]

        # Hard wall-clock kill = the smaller of the platform default and the buyer's authorized
        # budget.
        wall = WALL_TIMEOUT
        try:
            if max_runtime_s and int(max_runtime_s) > 0:
                wall = min(WALL_TIMEOUT, int(max_runtime_s))
        except (TypeError, ValueError):
            pass
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=wall)  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- cmd is the agent's fixed `docker run` argv (no shell); buyer code runs INSIDE the sandbox container, not as this argv
            if proc.returncode != 0:
                return [{"type": "error",
                         "value": f"Sandbox execution error: {proc.stderr[-2000:]}"}]
        except subprocess.TimeoutExpired:
            # Our docker CLIENT timed out, but the daemon-owned container keeps running (--rm only
            # fires when the container exits) — force-remove it so a timed-out notebook can't keep
            # burning the seller's GPU/CPU. The workspace is dropped by the finally below.
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
            return [{"type": "error", "value": "Execution timed out"}]

        if not os.path.exists(out_path) or os.path.getsize(out_path) > MAX_OUTPUT_BYTES * 4:
            return [{"type": "error", "value": "Output notebook missing or too large"}]

        executed = nbformat.read(out_path, as_version=4)

        outputs: List[dict] = []
        total = 0
        for cell in executed.cells:
            for output in cell.get("outputs", []):
                t = output.get("output_type")
                if t == "execute_result":
                    val = output["data"].get("text/plain", "")
                    outputs.append({"type": "text", "value": val})
                elif t == "stream":
                    outputs.append({"type": "text", "value": output.get("text", "")})
                elif t == "error":
                    outputs.append({"type": "error", "value": f"{output.get('ename')}: {output.get('evalue')}"})
                elif t == "display_data":
                    data = output.get("data", {})
                    if "image/png" in data:
                        outputs.append({"type": "image", "mime": "image/png", "base64": data["image/png"]})
                    elif "text/html" in data:
                        outputs.append({"type": "html", "value": data["text/html"]})
                total += len(json.dumps(outputs[-1])) if outputs else 0
                if total > MAX_OUTPUT_BYTES:
                    outputs.append({"type": "error", "value": "Output truncated (size cap reached)"})
                    return outputs
        return outputs
    finally:
        # ONE wipe covering every exit — success, error return, timeout, and any raise. The
        # previous per-branch rmtree calls were skipped whenever something in between threw
        # (nbformat.write hitting ENOSPC, nbformat.read on an output.ipynb the buyer's own code
        # clobbered), which left the buyer's plaintext on the seller's disk forever on the
        # fallback base — and, on /dev/shm, pinned in the seller's RAM until reboot.
        shutil.rmtree(scratch, ignore_errors=True)
