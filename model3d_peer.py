"""Dedicated, prepaid local coder service for one Blender task on another node.

Only the normal authenticated agent API connection crosses machines. The actual LLM stays
on a task-private Docker network with no published port and a fresh, short-lived API key.
"""
import hashlib
import json
import secrets
import subprocess
import time
import uuid

import httpx

MODEL = "qwen3-coder-30b-a3b-instruct"
SHA = "fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad"
SIZE = 18556689568


def api(host, task, route, **values):
    reply = httpx.post(host.API_URL + "/jobs/model3d/peer/" + route, headers=host.HEADERS,
        json={"task_id": task["task_id"], "lease_generation": task["lease_generation"], **values},
        trust_env=False, follow_redirects=False, timeout=15)
    reply.raise_for_status()
    return reply.json()


def wait_ready(host, task, started):
    """Blender never starts inference against a proxy whose paired model has not loaded."""
    until = min(started + 600, started + int(task.get("max_runtime_s") or 0) - 900)
    while time.monotonic() < until and task["task_id"] not in host._KILLED:
        if api(host, task, "status").get("ready") is True:
            return
        time.sleep(2)
    raise RuntimeError("the paired local 30B device did not become ready within the prepaid scene window")


def server_command(task, model, network, name, key, gpu_args, image, label):
    return ["docker", "run", "-d", "--pull=never", "--name", name, "--network", network,
        "--label", f"{label}={task['task_id']}", *gpu_args,
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256",
        "--memory", task["llm_memory"], "--memory-swap", task["llm_memory"], "--cpus", "4",
        "--user=65534:65534", "-e", "HOME=/tmp", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "--log-opt=max-size=1m", "--mount", f"type=bind,src={model},dst=/models/model.gguf,readonly",
        image, "-m", "/models/model.gguf", "--host", "0.0.0.0", "--port", "8080",
        "--alias", MODEL, "--api-key", key, "-c", "32768", "--parallel", "1",
        "--fit", "on", "--fit-target", "768", "--flash-attn", "on",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]


def local_reply(client, url, key, call, left):
    # The API supplies conversation, fixed model and bounded completion only. It cannot ask this
    # agent to fetch a URL, expose a port, change weights, sampling, context or host settings.
    if (call.get("model") != MODEL or type(call.get("max_tokens")) is not int
            or not 1 <= call["max_tokens"] <= 4096 or not isinstance(call.get("messages"), list)):
        raise RuntimeError("the paired scene has an invalid local model call")
    messages = call["messages"]
    if (not 1 <= len(messages) <= 16 or any(not isinstance(m, dict)
            or set(m) != {"role", "content"} or m["role"] not in ("system", "user", "assistant")
            or not isinstance(m["content"], str) for m in messages)
            or sum(len(m["content"].encode()) for m in messages) > 90000):
        raise RuntimeError("the paired scene conversation exceeds its bounds")
    # The pinned 30B uses the existing temperature-only sampler override.
    body = {"model": MODEL, "messages": messages, "max_tokens": call["max_tokens"],
            "temperature": 0.3, "stream": False}
    deadline = time.monotonic() + left
    with client.stream("POST", url + "/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                       json=body, timeout=min(600, left)) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise RuntimeError("the local coder prepaid deadline ended")
            size += len(chunk)
            if size > 512 * 1024:
                raise RuntimeError("the local model response exceeded its limit")
            chunks.append(chunk)
        result = json.loads(b"".join(chunks))
    if result.get("model") != MODEL:
        raise RuntimeError("the local server returned a different model identity")
    return result


def run(host, task):
    tid, started = task["task_id"], time.monotonic()
    tag = uuid.uuid4().hex[:12]
    network, name = "pb3d-peer-" + tag, "pb3d-coder-" + tag
    created = launched = False
    successful = build_failed = False
    phase = "contract verification"
    host._set_ui(status="running", task=f"Create 3D local coder #{tid}")
    try:
        pin = task.get("model") or {}
        if (pin.get("model") != MODEL or pin.get("sha256") != SHA or pin.get("size") != SIZE
                or task.get("llm_context") != 32768 or task.get("llm_max_tokens") != 4096
                or task.get("model3d_peer", {}).get("role") != "coder" or task.get("llm_remote")):
            raise RuntimeError("the dedicated coder requires the pinned local 30B contract")
        deadline = started + int(task.get("max_runtime_s") or 0)
        if deadline - time.monotonic() < 900:
            raise RuntimeError("the coder reservation has too little time for a complete scene")
        model = host._MODEL3D_CACHE.cached(pin)
        if model is None:
            raise RuntimeError("the pinned local 30B cache is not ready")
        # Read existing bytes only, with cancellation and prepaid deadline checks. No second
        # weights cache, fetch, eviction, unverified pin, or host repair.
        phase = "cached weights verification"
        digest = hashlib.sha256()
        with open(model, "rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                if tid in host._KILLED or time.monotonic() >= deadline:
                    raise RuntimeError("the local coder reservation ended during verification")
                digest.update(chunk)
        if digest.hexdigest() != SHA:
            raise RuntimeError("the cached local 30B bytes do not match the pinned SHA256")
        phase = "server image preparation"
        host.template_storage.prepare(task["llm_image"], tid, timeout=min(600, int(deadline-time.monotonic())))
        image = host.template_storage.local(task["llm_image"])
        subprocess.run(["docker", "network", "create", "--internal", network], check=True,
                       capture_output=True, timeout=30)
        created = True
        key = secrets.token_urlsafe(32)
        phase = "private server startup"
        launched = True  # clean this exact name even if docker created it before reporting an error
        result = subprocess.run(server_command(task, model, network, name, key,
            host.gpu_runtime.docker_gpu_args(), image, host.TASK_LABEL), capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("the private local 30B server did not start")
        launched = True
        url = "http://" + host._model3d_coder_address(name, network) + ":8080"
        phase = "loaded model and context verification"
        with httpx.Client(trust_env=False, follow_redirects=False) as client:
            until = min(started + 600, deadline - 900)
            while time.monotonic() < until and tid not in host._KILLED:
                try:
                    health = client.get(url + "/health", timeout=5)
                    if health.status_code == 200:
                        headers = {"Authorization": "Bearer " + key}
                        models = client.get(url + "/v1/models", headers=headers, timeout=5)
                        props = client.get(url + "/props", headers=headers, timeout=5)
                        models.raise_for_status(); props.raise_for_status()
                        if (MODEL not in [r.get("id") for r in models.json().get("data", [])]
                                or props.json().get("default_generation_settings", {}).get("n_ctx") != 32768):
                            raise RuntimeError("the loaded local model or context differs from the scene contract")
                        break
                except httpx.HTTPError:
                    pass  # normal loading; the absolute preparation and prepaid limits still apply
                time.sleep(2)
            else:
                raise RuntimeError("the pinned local 30B server did not finish loading")
            api(host, task, "ready", model=MODEL, sha256=SHA, context=32768, max_tokens=4096)
            host.report_progress(tid, 20, "local 30B ready for the paired Blender device")
            phase = "paired scene inference"
            while tid not in host._KILLED and time.monotonic() < deadline:
                state = api(host, task, "next")
                if state.get("ended"):
                    if state.get("successful") is not True:
                        build_failed = True
                        raise RuntimeError("the build failed: paired Blender build failed")
                    successful = True
                    return
                call = state.get("request")
                if call:
                    response = local_reply(client, url, key, call, deadline - time.monotonic())
                    api(host, task, "result", call_id=call["id"], response=response)
                else:
                    time.sleep(2)
            raise RuntimeError("the local coder's prepaid assignment ended")
    except Exception as exc:  # noqa: BLE001 — never print response data, addresses, keys or Docker argv
        host.report_log(tid, f"dedicated local 30B service stopped during {phase}: {type(exc).__name__}")
    finally:
        if launched:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False)
        if created:
            subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30, check=False)
        result = ("Pinned local 30B served this paired Blender scene" if successful else
                  "the build failed: paired Blender build failed" if build_failed else
                  "the paired local 30B service did not finish within its verified assignment")
        host._post("/jobs/result", host._signed_result(tid,
            status="completed" if successful else "failed", result=result))
        host._set_ui(status="idle", task=None, **({"ok": True} if successful else {"fail": True}))
