"""Real CUDA + notebook startup diagnostic, with no buyer files, ports or funds.

Fixed programs only, digest-pinned images, bounded execution and normal Docker
cleanup. This reports software evidence; host administrators can still spoof it.
"""
import json
import subprocess
import threading

import benchmark_runtime

_CANCEL = threading.Event()
_RUNNING = threading.Event()


def yield_to_paid_work():
    if not _RUNNING.is_set():
        return
    _CANCEL.set()
    # Only our labelled diagnostic containers, never a rental or a seller-owned container.
    found = subprocess.run(["docker", "ps", "-aq", "--filter", "label=pb.template_probe=1"],
                           timeout=5, capture_output=True, text=True, check=False)
    import re
    for cid in (found.stdout or "").split():
        if re.fullmatch(r"[0-9a-f]{12,64}", cid):
            subprocess.run(["docker", "rm", "-f", cid], timeout=5, capture_output=True, check=False)

PROGRAM = r'''
import hashlib, json, os, shutil, subprocess, sys, time, urllib.request
template, nonce = sys.argv[1:3]
n = 64
result = dict(version=1, n=n, nonce=nonce, template=template, status="failed")
def values(label):
    raw = b"".join(hashlib.sha256(bytes.fromhex(nonce)+label+i.to_bytes(4,"big")).digest() for i in range(n*n//32))
    return [b % 7 - 3 for b in raw]
try:
    if template == "tensorflow":
        import tensorflow as tf
        gpus = tf.config.list_physical_devices("GPU")
        if not gpus:
            raise LookupError("no CUDA device")
        tf.config.set_soft_device_placement(False)
        for gpu in gpus:
            tf.config.experimental.set_memory_growth(gpu, True)
        with tf.device("/GPU:0"):
            a = tf.reshape(tf.constant(values(b"A"), dtype=tf.float32), (n,n))
            b = tf.reshape(tf.constant(values(b"B"), dtype=tf.float32), (n,n))
            tensor = tf.matmul(a,b)
            if "GPU" not in tensor.device:
                raise LookupError("CPU fallback refused")
            output = tensor.numpy().reshape(-1).tolist()
    else:
        import torch
        if not torch.cuda.is_available():
            raise LookupError("no CUDA device")
        a = torch.tensor(values(b"A"),dtype=torch.float32,device="cuda").reshape(n,n)
        b = torch.tensor(values(b"B"),dtype=torch.float32,device="cuda").reshape(n,n)
        output = (a @ b).cpu().reshape(-1).tolist()
    if any(not float(x).is_integer() for x in output):
        raise ValueError("non-integral result")
    result["output_hash"] = hashlib.sha256(json.dumps([int(x) for x in output],separators=(",",":")).encode()).hexdigest()
except LookupError:
    result["failure"] = "CUDA_UNAVAILABLE"
except ImportError:
    result["failure"] = "CHECK_FAILED"
except Exception:
    result["failure"] = "OPERATION_FAILED"
if "failure" not in result:
    app = None
    try:
        launcher = shutil.which("start-notebook.py") or shutil.which("start-notebook.sh")
        if not launcher:
            raise RuntimeError("template launcher unavailable")
        env = dict(os.environ, JUPYTER_TOKEN=nonce, JUPYTER_PORT="8888")
        app = subprocess.Popen([launcher,"--ServerApp.ip=127.0.0.1","--ServerApp.port=8888","--ServerApp.port_retries=0"],
                               env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        until = time.monotonic()+60
        while time.monotonic()<until and app.poll() is None:
            try:
                # Loopback-only, no externally published ports or external network access.
                with urllib.request.urlopen("http://127.0.0.1:8888/api/status?token="+nonce,timeout=2) as response:
                    if response.status == 200 and isinstance(json.load(response),dict):
                        result.update(status="completed",app_ready=True)
                        break
            except Exception:
                time.sleep(.5)
        if result["status"] != "completed":
            result["failure"] = "APP_START_FAILED"
    except Exception:
        result["failure"] = "APP_START_FAILED"
    finally:
        if app is not None:
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=5)
print(json.dumps(result,separators=(",",":")))
'''


def run(challenge, runner, prepare, isolation_flags, gpu_flags, task_id):
    # Same strict bounds as the independent server checker; validate before image preparation.
    if not isinstance(challenge, dict) or challenge.get("template") not in ("pytorch", "tensorflow", "jupyter"):
        raise ValueError("unsupported template probe")
    benchmark_runtime.validate(challenge)
    env = challenge.get("env", {})
    if (not isinstance(env, dict) or any(k != "LD_LIBRARY_PATH" or not isinstance(v, str) or len(v) > 4096 for k,v in env.items())):
        raise ValueError("invalid template probe environment")
    answer = {k: challenge[k] for k in ("version", "nonce", "n", "image", "template")}
    answer.update(status="failed", failure="CHECK_FAILED")
    if not gpu_flags:
        return dict(answer, failure="CUDA_UNAVAILABLE")
    _CANCEL.clear()
    _RUNNING.set()
    try:
        prepare(challenge["image"], task_id, timeout=120)
    except Exception as exc:
        reason = str(exc).lower()
        _RUNNING.clear()
        return dict(answer, failure="CACHE_POLICY" if "budget" in reason or "reserve" in reason else "IMAGE_UNAVAILABLE")
    try:
        # One permitted container variable for the catalog TensorFlow CUDA library path.
        env_flags = [arg for k,v in env.items()
                     for arg in ("-e", k+"="+str(v))]
        if _CANCEL.is_set():
            return answer
        command = ["docker", "run", "--rm", "--label", "pb.template_probe=1", "--label", f"pb.task={task_id}", "--pull=never", "--network", "none", *isolation_flags,
                   *gpu_flags, *env_flags, "--entrypoint", "python3", challenge["image"], "-c", PROGRAM,
                   challenge["template"], challenge["nonce"]]
        proc = runner(command, timeout=180, capture_output=True, text=True, check=False)
        output = proc.stdout or ""
        if _CANCEL.is_set() or proc.returncode or len(output) > 4096:
            return answer
        body = json.loads(output.strip().splitlines()[-1])
        # Copy only protocol fields. No stderr, host paths or arbitrary caller metadata leaves the host.
        for key in ("status", "failure", "output_hash", "app_ready"):
            if key in body:
                answer[key] = body[key]
        if answer["status"] == "completed":
            answer.pop("failure", None)
        return answer
    except subprocess.TimeoutExpired:
        return dict(answer, failure="TIMEOUT")
    except Exception:
        return answer
    finally:
        _RUNNING.clear()
