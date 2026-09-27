"""Bounded GPU known-answer workload in the actual catalog PyTorch image.

No fallback to host Python/CPU: runtime failures are reported as failures. A
malicious host can still emulate/relay this check; it is not hardware attestation.
"""
import json
import re

# Fixed program, never source supplied by a buyer or server. The only inputs are
# a validated nonce and fixed dimension. Integer operands/results are exactly
# representable in float32 (sum magnitude <= 576); TF32 also represents them.
PROGRAM = r'''
import hashlib, json, sys, torch
n = 64
nonce = sys.argv[1]
def values(label):
    raw = b"".join(hashlib.sha256(bytes.fromhex(nonce) + label + i.to_bytes(4, "big")).digest()
                   for i in range(n*n//32))
    return [b % 7 - 3 for b in raw]
if not torch.cuda.is_available():
    raise RuntimeError("CUDA is unavailable in the catalog container")
a = torch.tensor(values(b"A"), dtype=torch.float32, device="cuda").reshape(n,n)
b = torch.tensor(values(b"B"), dtype=torch.float32, device="cuda").reshape(n,n)
out = (a @ b).cpu().reshape(-1).tolist()
if any(not float(x).is_integer() for x in out):
    raise RuntimeError("non-integral challenge result")
digest = hashlib.sha256(json.dumps([int(x) for x in out], separators=(",", ":")).encode()).hexdigest()
print(json.dumps({"output_hash":digest}, separators=(",", ":")))
'''


def run(challenge, runner, isolation_flags, gpu_flags):
    if not isinstance(challenge, dict) or type(challenge.get("version")) is not int or challenge["version"] != 1:
        raise ValueError("unsupported runtime challenge")
    if type(challenge.get("n")) is not int or challenge["n"] != 64:
        raise ValueError("invalid runtime challenge size")
    nonce, image = challenge.get("nonce"), challenge.get("image")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise ValueError("invalid runtime challenge nonce")
    if (not isinstance(image, str) or len(image) > 512 or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}", image)):
        raise ValueError("a digest-pinned runtime image is required")
    result = {k: challenge[k] for k in ("version", "nonce", "n", "image")}
    result["status"] = "failed"
    try:
        if not gpu_flags:
            return result
        command = ["docker", "run", "--rm", "--pull=never", "--network", "none", *isolation_flags,
                   *gpu_flags, "--entrypoint", "python3", image, "-c", PROGRAM, nonce]
        completed = runner(command, timeout=180, capture_output=True, text=True, check=False)
        # Bound parser input and never forward logs/host paths in signed/public evidence.
        output = completed.stdout or ""
        if completed.returncode:
            error = (getattr(completed, "stderr", "") or "").lower()
            if "no such image" in error or "unable to find image" in error:
                result["failure"] = "IMAGE_NOT_CACHED"
            elif "cuda is unavailable" in error:
                result["failure"] = "CUDA_UNAVAILABLE"
            return result
        if len(output) > 4096:
            return result
        body = json.loads(output.strip().splitlines()[-1])
        digest = body.get("output_hash")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            result.update(status="completed", output_hash=digest)
    except Exception:  # noqa: BLE001 -- never hide failure through a CPU fallback
        pass
    return result
