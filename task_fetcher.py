"""Petabyte agent task loop.

Talks to the hardened API:
  - POST /heartbeat        (liveness for the spec this node serves)
  - GET  /jobs/next        (claim a job for hardware we own)
  - POST /jobs/result      (notebook result)
  - POST /jobs/vm_details  (vm connection info)

Auth is the real encrypted API key (X-API-KEY). Heartbeat runs on its own thread
so a long-running job never makes the node look offline (which would get it reaped).
"""
import hashlib
import logging
import os
import threading
import time

import time as _t

import httpx
import safe_fetch
import gpu_runtime

import crypto
import agent_scratch
import idle_mining
import fixes as _fixes
import mining_income
try:
    import workload_seal as _seal      # sealed-workload crypto (opt-in; unwrap+unseal in RAM only)
except Exception:                       # noqa: BLE001 — older bundles may not ship it
    _seal = None
from notebook import run_notebook_code
from vm import launch_vm_task
import agent_telemetry as _tel

try:
    import console as _con                            # pretty seller-facing console feed
except Exception:                                     # noqa: BLE001 — never block the agent
    _con = None


def _safe_ext(v, default="mp4"):
    """Sanitize a buyer-supplied container/extension before it is joined into a HOST path.
    Defense-in-depth: the API validates this too, but the agent runs as root, so a value like
    '../../../etc/cron.d/x' must never reach os.path.join on the seller's filesystem."""
    import os as _o
    import re as _r
    v = _o.path.basename(str(v if v is not None else default)).lstrip(".").lower()
    return v if _r.match(r"^[a-z0-9]{1,8}$", v) else default

# Console output IS the user feed now, so keep the root logger quiet (WARNING+); the pretty
# lines carry the routine story, logging carries problems.
logging.basicConfig(level=logging.WARNING, format="[%(asctime)s] %(levelname)s: %(message)s")
_earn = {"shown": False}                              # latest earnings forecast from heartbeat

# ONE computer == ONE agent process == ONE API key + ONE spec. A single user can run as MANY
# computers as they own: each machine runs its own agent with its OWN PETABYTE_API_KEY (minted
# per machine at /create_api_key) and its OWN PETABYTE_SPEC_ID. The platform treats each spec as a
# distinct machine, so one account's computers can even be gang-scheduled together into one
# distributed cluster (anti_affinity is per-machine — see router.select_plan / /distributed).
API_URL = os.getenv("PETABYTE_API_URL")        # e.g. https://petabyte.market
API_KEY = os.getenv("PETABYTE_API_KEY")        # this machine's encrypted key from POST /create_api_key
SPEC_ID = os.getenv("PETABYTE_SPEC_ID")        # the spec (machine) this agent serves
HEARTBEAT_S = int(os.getenv("HEARTBEAT_INTERVAL", "15"))
POLL_S = int(os.getenv("JOB_POLL_INTERVAL", "5"))

if not API_URL or not API_KEY or not SPEC_ID:
    raise SystemExit("Set PETABYTE_API_URL, PETABYTE_API_KEY and PETABYTE_SPEC_ID")

HEADERS = {"X-API-KEY": API_KEY}


# ---- selling schedule: the hours this node offers its GPU for sale, in NODE-LOCAL time ----
# The seller picks a window at install (PETABYTE_SELL_SCHEDULE) and can change it from the dashboard
# (pushed back to us in the heartbeat reply). The agent's local clock is authoritative: we report
# selling_now so the SERVER can refuse NEW reservations outside the window, and we additionally
# refuse a job at claim time if its known runtime would run PAST the window end ("let it finish,
# don't over-run"). In-flight jobs are NEVER interrupted — the gate only stops NEW work.
def _parse_sched(text):
    """'HH:MM-HH:MM' -> (start_min, end_min); 'always'/''/None/malformed -> (None, None) = always-on."""
    if not text or str(text).strip().lower() in ("always", "always-on", "24/7", "none"):
        return (None, None)
    try:
        a, b = str(text).strip().split("-", 1)

        def _m(x):
            hh, mm = x.strip().split(":", 1)
            return (int(hh) % 24) * 60 + (int(mm) % 60)
        return (_m(a), _m(b))
    except Exception:  # noqa: BLE001 — a malformed value must never brick selling; treat as always-on
        return (None, None)


_sched = {"start": None, "end": None}
_sched["start"], _sched["end"] = _parse_sched(os.getenv("PETABYTE_SELL_SCHEDULE", ""))


def _now_minutes():
    lt = time.localtime()
    return lt.tm_hour * 60 + lt.tm_min


# Set when this host can't run a GPU container (the mandatory VRAM wipe fails — e.g. CUDA can't init
# inside ANY container while nvidia-smi works). The node then reports selling_now=False, so the server
# stops placing buyers here, until an idle self-test passes. Before this, every buyer routed to such a
# node got a refused launch (a seller's RTX 5070 Ti, 2026-09-23).
_GPU_UNUSABLE = threading.Event()
# A job claimed or running in job_loop (batch jobs block it; template rentals are tracked in
# _pb_vm_watch). _CLAIM_LOCK orders claiming a job against queueing a support fix, so a fix never
# lands mid-job and no job is claimed while a fix is pending (fixes.py).
_JOB_RUNNING = threading.Event()
_CLAIM_LOCK = threading.Lock()


def _selling_now():
    if _GPU_UNUSABLE.is_set() or _fixes.pending():   # not offered while a support fix is in flight
        return False
    s, e = _sched["start"], _sched["end"]
    if s is None or e is None or s == e:
        return True                                   # always-on
    n = _now_minutes()
    return (s <= n < e) if s < e else (n >= s or n < e)   # s>e wraps past midnight


def _minutes_left_in_window():
    """Minutes until the window closes; None when always-on (no limit)."""
    s, e = _sched["start"], _sched["end"]
    if s is None or e is None or s == e:
        return None
    return (e - _now_minutes()) % 1440


def _job_within_schedule(task):
    """Refuse a NEW job when outside the selling window, or when its KNOWN runtime would over-run the
    window end. Unknown-duration jobs run (can't prove an over-run); in-flight jobs are never touched."""
    if not _selling_now():
        return False
    left = _minutes_left_in_window()
    if left is None:
        return True
    dur = task.get("max_runtime_s")
    try:
        dur = int(dur)
    except (TypeError, ValueError):
        return True                                   # unknown duration — let it run
    return (dur / 60.0) <= left


def _set_ui(status=None, task=None, ok=None, fail=None):
    try:
        import ui
        if status is not None:
            ui.agent_status["status"] = status
        if task is not None:
            ui.agent_status["current_task"] = task
        if ok:
            ui.agent_status["tasks_completed"] = ui.agent_status.get("tasks_completed", 0) + 1
        if fail:
            ui.agent_status["tasks_failed"] = ui.agent_status.get("tasks_failed", 0) + 1
    except Exception:
        pass


# ---- confidential computing: capability probe + periodic self-attestation ----
_cc_cache = {"caps": None, "at": 0.0}
_CC_REFRESH_S = int(os.getenv("CONFIDENTIAL_REFRESH_S", "1800"))     # re-probe capabilities ~30 min
_attest_state = {"next": 0.0}
_ATTEST_EVERY_S = int(os.getenv("CONFIDENTIAL_REATTEST_S", "3600"))  # re-attest before TTL expiry


def _confidential_caps():
    """Cached confidential-computing capability probe (safe; refreshed every _CC_REFRESH_S)."""
    now = time.time()
    if _cc_cache["caps"] is None or (now - _cc_cache["at"]) > _CC_REFRESH_S:
        try:
            import confidential_detect as _cd
            _cc_cache["caps"] = _cd.detect_confidential()
        except Exception:
            _cc_cache["caps"] = None
        _cc_cache["at"] = now
    return _cc_cache["caps"]


def _maybe_attest_confidential():
    """Periodically (re)attest the node's TEE so its confidential status stays FRESH server-side.

    On real hardware this is where the vendor SDK (NVIDIA nvtrust / AMD / Intel DCAP) produces the
    signed evidence — not available in this dev environment, so it is skipped with a clear log.
    In CONFIDENTIAL_COMPUTING_DEV_MODE the node self-attests via the dev-mock provider, signing the
    report with its Ed25519 device key. The SERVER refuses dev-mock in production (fail-closed)."""
    now = time.time()
    if now < _attest_state["next"]:
        return
    _attest_state["next"] = now + _ATTEST_EVERY_S
    caps = _confidential_caps()
    if not caps or caps.get("confidential_level", "NONE") == "NONE":
        return
    dev = (os.getenv("CONFIDENTIAL_COMPUTING_DEV_MODE", "").strip().lower() in ("1", "true", "yes")
           or bool(caps.get("dev_mode")))
    if not dev:
        logging.info("confidential: real TEE attestation needs the vendor SDK on this host; "
                     "skipping self-attest (see docs/CONFIDENTIAL_COMPUTING.md)")
        return
    try:
        import crypto
        ch = httpx.post(f"{API_URL}/attestation/challenge", json={"spec_id": int(SPEC_ID)},
                        headers=HEADERS, timeout=10, trust_env=False)
        if ch.status_code != 200:
            return
        nonce = ch.json().get("nonce")
        report = {"nonce": nonce, "measurement": os.getenv("TEE_MEASUREMENT", "mr_dev_mock"),
                  "vendor": "dev_mock", "ts": int(time.time()), "dev_mode": True}
        sig = crypto.sign_proof(report)
        pr = httpx.post(f"{API_URL}/prove_tee",
                        json={"spec_id": int(SPEC_ID), "report": report, "signature": sig,
                              "provider": "dev_mock"}, headers=HEADERS, timeout=10, trust_env=False)
        if pr.status_code == 200:
            logging.info("confidential: dev-mock self-attestation refreshed (DEVELOPMENT ONLY)")
    except Exception as e:                                        # noqa: BLE001
        logging.warning(f"confidential self-attest failed: {e}")


# Sealed-workload state (opt-in). The RSA private key is EPHEMERAL and RAM-only — never written to
# disk — so a stolen disk cannot unwrap the AES key. The AES key (once unwrapped from the platform's
# heartbeat reply) also lives only here in memory; it is used to decrypt dispatched bundles in RAM.
_SEAL_PRIV = None
_SEAL_PUB = None
_SEAL_AES = None


def _ensure_seal_keypair():
    global _SEAL_PRIV, _SEAL_PUB
    if _seal is not None and _SEAL_PRIV is None:
        try:
            _SEAL_PRIV, _SEAL_PUB = _seal.generate_node_keypair()
        except Exception as e:                          # noqa: BLE001 — sealing stays disabled
            logging.warning(f"sealed-workload keypair unavailable: {e}")


def heartbeat_loop():
    global _SEAL_AES
    while True:
        try:
            _ensure_seal_keypair()
            import hardware_evidence
            _hb = {"spec_id": int(SPEC_ID), "hardware_evidence": hardware_evidence.collect(),
                   "selling_now": _advertise_selling_now(),  # JIT also waits for tunnel enrollment
                   "remote_fixes": _fixes.enabled()}  # owner allowed signed support fixes
            if _JOB_NET["ok"] is not None:           # can this host isolate a networked app?
                _hb["job_network"] = dict(_JOB_NET)
            _bundle = _agent_bundle()
            if _bundle:                               # which signed agent bundle this node runs
                _hb["agent_bundle"] = _bundle
            if os.getenv("PETABYTE_MINING_FLOOR") == "true":
                _hb["mining_floor_enabled"] = True
                _hb["mining_income"] = mining_income.heartbeat_report()
            if _SEAL_PUB:
                _hb["seal_pubkey"] = _SEAL_PUB           # opt-in: let the platform wrap our AES key
            _caps = _confidential_caps()
            if _caps:
                _hb["confidential"] = _caps                       # REPORTED caps (server stores as reported)
            _mining_ticket = idle_mining.controller.ticket()
            r = httpx.post(f"{API_URL}/heartbeat", json=_hb, headers=HEADERS, timeout=10, trust_env=False)
            if r.status_code == 200:
                _body = r.json()
                try:
                    with _pb_vm_lock:
                        _live = any(not v.get("reported") for v in _pb_vm_watch.values())
                    idle_mining.controller.heartbeat(_mining_ticket, _body.get("idle_mining"), live=_live)
                except Exception as exc:
                    logging.warning("Idle mining paused: %s", exc)
                    idle_mining.controller.revoke()
                # Owner may have changed the selling window in the dashboard — adopt it live.
                _sc = _body.get("sell_schedule")
                if _sc is not None:
                    _sched["start"], _sched["end"] = _parse_sched(_sc)
                _wk = _body.get("wrapped_key")
                if _wk and _seal is not None and _SEAL_PRIV is not None:
                    try:                                 # unwrap the node's AES key into RAM only
                        _SEAL_AES = _seal.unwrap_aes_key(_SEAL_PRIV, _wk)
                    except Exception as _e:              # noqa: BLE001
                        logging.warning(f"seal key unwrap failed: {_e}")
                # Live earnings forecast: show it once under the banner, and expose it to the
                # desktop dashboard via the shared ui.agent_status dict.
                _e = _body.get("earnings")
                if _e:
                    try:
                        import ui as _ui
                        _ui.agent_status["earnings"] = _e
                    except Exception:
                        pass
                    if _con and not _earn["shown"]:
                        _earn["shown"] = True
                        _con.earnings(_e.get("net_per_hour", 0.0),
                                      _e.get("estimated_daily_usd_low", 0.0),
                                      _e.get("estimated_daily_usd_high", 0.0))
                        _con.ready(POLL_S)
                # Reap dead-rental containers the exit-watchdog can't see (server stopped/expired
                # the VM, or the container outlived a prior agent process) so they stop holding host
                # ports. Driven from the always-on heartbeat, keyed on the node's active VM set.
                try:
                    _reap_orphan_templates(_body.get("active_vm_tasks"))
                except Exception as _re:             # noqa: BLE001 — never break a heartbeat over GC
                    logging.debug(f"orphan reap skipped: {_re}")
                # GRACEFUL PREEMPTION: the server flagged these spot VMs for reclaim. Checkpoint NOW
                # so the resumed job loses as little work as possible, then let periodic backups
                # stop. The server hard-kills + finalizes at the grace deadline regardless, and the
                # orphan reap above tears the container down once it does — so this is best-effort.
                try:
                    for _p in (_body.get("preempt") or []):
                        _handle_preempt(_p)
                except Exception as _pe:             # noqa: BLE001 — never break a heartbeat over preempt
                    logging.debug(f"preempt handling skipped: {_pe}")
                # SIGNED SUPPORT FIX (fixes.py): queue it for the root runner, which verifies it;
                # then report anything the runner finished. Never breaks a heartbeat.
                try:
                    with _CLAIM_LOCK:
                        _fixes.handle(_body.get("fix"),
                                      rental_live=_rental_live() or _JOB_RUNNING.is_set())
                    if _fixes.results() and _FIX_REPORT.acquire(blocking=False):
                        threading.Thread(target=_report_fix_results, daemon=True,
                                         name="pb-fix-report").start()
                except Exception as _fe:             # noqa: BLE001
                    logging.debug(f"fix handling skipped: {_fe}")
                _tel.event(_tel.EVENTS.HEARTBEAT, message="heartbeat ok", status_code=200)
                _maybe_attest_confidential()   # keep TEE attestation FRESH (dev-mode self-attest)
            else:
                logging.warning(f"heartbeat {r.status_code}: {r.text[:200]}")
                _tel.event(_tel.EVENTS.HEARTBEAT_MISSED, message="heartbeat non-200",
                           status_code=r.status_code)
        except Exception as e:                          # noqa: BLE001
            logging.error(f"heartbeat error: {e}")
            _tel.event(_tel.EVENTS.HEARTBEAT_MISSED, message="heartbeat error",
                       reason=str(e)[:120])
        time.sleep(HEARTBEAT_S)


def _submit_signed(tid, output_hash, result=None, status="completed"):
    import execution_receipt
    proof = execution_receipt.make(tid, status=status, result=result, output_hash=output_hash)
    httpx.post(f"{API_URL}/jobs/result", headers=HEADERS, timeout=15, json={
        "task_id": tid, "result": result, "status": status,
        "proof": proof, "signature": crypto.sign_proof(proof)}, trust_env=False)


def _run_notebook(task):
    tid = task["task_id"]
    _set_ui(status="running", task=f"Notebook #{tid}")
    code = task.get("code", "")
    env = None
    try:
        import json as _json
        env = _json.loads(code) if code.strip().startswith("{") else None
    except Exception:
        env = None
    if env and (env.get("bundle_b64") or env.get("bundle_sealed")):
        return _run_project_bundle(task, env)   # a project with local dependencies (maybe sealed)
    if env and env.get("notebook_url"):
        return _run_notebook_url(task, env)     # run a .ipynb by link, return its executed output
    code = env.get("code", code) if env else code
    result = run_notebook_code(code, max_runtime_s=task.get("max_runtime_s"))
    try:
        _submit_signed(tid, crypto.sha256_hex(result), result=_to_str(result))
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                              # noqa: BLE001
        logging.error(f"submit result error: {e}")
        _set_ui(status="idle", task=None, fail=True)


def _url_is_public(url):
    from safe_fetch import is_public_url
    return is_public_url(url)


def _safe_notebook_get(url, timeout=60, max_redirects=5):
    from safe_fetch import get
    return get(url, timeout=timeout, max_redirects=max_redirects)


def _run_notebook_url(task, env):
    """Batch 'run this notebook link and give me the result': the AGENT fetches the .ipynb over the
    network (the sandbox runs --network none, so the fetch must happen out here), then executes it
    in the locked-down container and submits the executed output inline. The server validated the
    URL, but we re-apply the SSRF guard here (and per redirect hop) because this fetch runs on the
    host netns — outside the DOCKER-USER egress firewall — so it must not reach metadata/LAN."""
    tid = task["task_id"]
    url = env["notebook_url"]
    try:
        r = _safe_notebook_get(url, timeout=60)
        r.raise_for_status()
        nb = r.json()                                    # a full nbformat notebook dict
    except Exception as e:                               # noqa: BLE001
        # Report the failure as the job result instead of letting it look like a silent vanish —
        # the buyer sees WHY (bad link, private notebook, not JSON) at GET /tasks/{id}.
        _submit_signed(tid, crypto.sha256_hex(str(e)),
                       result="notebook fetch failed: destination, response or download rejected", status="failed")
        _set_ui(status="idle", task=None, fail=True)
        return
    result = run_notebook_code(nb, gpu=bool(env.get("gpu")),
                               max_runtime_s=env.get("max_runtime_s") or task.get("max_runtime_s"))
    try:
        _submit_signed(tid, crypto.sha256_hex(result), result=_to_str(result))
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                               # noqa: BLE001
        logging.error(f"submit result error: {e}")
        _set_ui(status="idle", task=None, fail=True)


def _run_project_bundle(task, env):
    """Unpack a base64 tar.gz project the buyer bundled and run its entry script in an
    isolated GPU container (local imports + data files present, no network)."""
    tid = task["task_id"]
    import base64, io, tarfile, tempfile, shutil, subprocess, uuid as _uuid
    if not shutil.which("docker"):
        _submit_signed(tid, crypto.sha256_hex("no-docker"),
                       result="this node has no Docker to run a project bundle")
        _set_ui(status="idle", task=None, fail=True); return
    # Stage the buyer's bundle on a RAM-backed tmpfs (see agent_scratch): the code + data files are
    # plaintext only in RAM while the container runs, never written to the seller's persistent disk.
    # need_bytes: a tar.gz EXPANDS on extract, so ask for room for the unpacked tree (4x the encoded
    # blob, a deliberate over-estimate). Without it a large bundle can pick a nearly-full tmpfs and
    # then die with ENOSPC halfway through extractall — mid-rental, with the buyer already charged.
    try:
        work = agent_scratch.make_scratch(
            "pb-run-", need_bytes=len(env.get("bundle_sealed") or env.get("bundle_b64") or "") * 4)
    except agent_scratch.ScratchUnavailable as _e:
        # The operator set PB_JOB_SCRATCH_REQUIRE_RAM: they would rather lose the job than have the
        # buyer's plaintext land on their persistent disk. Fail it explicitly so the buyer is told,
        # instead of letting the exception escape and the job look like it silently vanished.
        _submit_signed(tid, crypto.sha256_hex(str(_e)), result=f"project run failed: {_e}")
        _set_ui(status="idle", task=None, fail=True); return
    try:
        if env.get("bundle_sealed"):
            # SEALED: decrypt the bundle in RAM only. The ciphertext (not the code) is what arrived,
            # and the AES key lives only in this process's memory (never on the seller's disk).
            if _seal is None or _SEAL_AES is None:
                _submit_signed(tid, crypto.sha256_hex("no-seal-key"),
                               result=("sealed workload received but this node holds no seal key yet "
                                       "(waiting on a heartbeat) — the platform will retry it"))
                _set_ui(status="idle", task=None, fail=True); return
            data = _seal.unseal(_SEAL_AES, env["bundle_sealed"], aad=str(tid).encode())
        else:
            data = base64.b64decode(env["bundle_b64"])
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            t.extractall(work, filter="data")       # 'data' filter blocks path traversal (3.12+)
        entry = (env.get("entry") or "main.py").lstrip("/")
        gpu = bool(env.get("gpu"))
        image = gpu_runtime.torch_image() if gpu else "python:3.11-slim"
        name = f"pb-run-{tid}-{_uuid.uuid4().hex[:6]}"
        cmd = ["docker", "run", "--rm", "--name", name, "--network", "none",
               "-v", f"{work}:/work", "-w", "/work"]
        cmd += _isolation_flags(task)
        if gpu:
            cmd += [*gpu_runtime.docker_gpu_args()]
        rt = task.get("max_runtime_s")
        inner = (f"timeout {int(rt)} " if rt else "") + f"python {entry}"
        cmd += [image, "sh", "-c", inner]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=(int(rt) if rt else 3600) + 120)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        out = out[-100000:] if out.strip() else f"(no output; exit code {p.returncode})"
        _submit_signed(tid, crypto.sha256_hex(out), result=_to_str(out))
        _set_ui(status="idle", task=None, ok=(p.returncode == 0), fail=(p.returncode != 0))
    except Exception as e:                          # noqa: BLE001
        _submit_signed(tid, crypto.sha256_hex(str(e)), result=f"project run failed: {e}")
        _set_ui(status="idle", task=None, fail=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_test(task):
    """Known-answer test: compute the deterministic hash and submit it signed."""
    tid = task["task_id"]
    _set_ui(status="running", task=f"Test #{tid}")
    try:
        h = crypto.compute_test_hash(int(task["size"]), int(task["seed"]))
        _submit_signed(tid, h)
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                              # noqa: BLE001
        logging.error(f"test error: {e}")
        _set_ui(status="idle", task=None, fail=True)


def _run_vm(task):
    _post("/jobs/result", _signed_result(task["task_id"], status="failed", failure_cause="unsupported_runtime"))
    _set_ui(status="idle", task=None, fail=True)


def _to_str(obj):
    import json
    return obj if isinstance(obj, str) else json.dumps(obj)


def _post(path, payload):
    try:
        httpx.post(f"{API_URL}{path}", headers=HEADERS, json=payload, timeout=15, trust_env=False)
    except Exception as e:                              # noqa: BLE001
        logging.error(f"{path} error: {e}")


def _register_vm_tunnel(vm_id, tunnel_port, ip_address=None, attempts=5) -> bool:
    """P1-7: report node:port to the control plane so a template VM flips starting->running and
    the gateway can route buyers to it. ip_address is optional — the server falls back to the
    public source IP of this request. Retries a few times: right after /launch the VMRoute may not
    be committed yet (409 'not in a registrable state'), and a transient 5xx must not strand the
    VM in 'starting'. register_vm_tunnel is idempotent, so retrying is safe."""
    body = {"vm_id": str(vm_id), "tunnel_port": int(tunnel_port)}
    if ip_address:
        body["ip_address"] = ip_address
    delay = 2
    for i in range(attempts):
        try:
            r = httpx.post(f"{API_URL}/vm/register_tunnel", headers=HEADERS, json=body, timeout=15, trust_env=False)
            if r.status_code // 100 == 2:
                return True
            # 404 (no such VM) / 403 (not our node) are terminal — don't hammer.
            if r.status_code in (403, 404):
                logging.error(f"register_tunnel refused: HTTP {r.status_code} {r.text[:200]}")
                return False
            logging.warning(f"register_tunnel HTTP {r.status_code} (attempt {i+1}/{attempts})")
        except Exception as e:                          # noqa: BLE001
            logging.warning(f"register_tunnel error (attempt {i+1}/{attempts}): {e}")
        if i < attempts - 1:
            time.sleep(delay)
            delay = min(delay * 2, 20)
    return False


def _inject_ssh_key(container_name, ssh_pubkey) -> None:
    """Best-effort: drop the buyer's SSH public key into the container's authorized_keys so an
    sshd template accepts `ssh root@<id>.<zone>`. A no-op for templates without sshd (most serving
    templates expose an HTTP port, not port 22) and never fatal — mirrors the notebook prefetch."""
    if not ssh_pubkey:
        return
    import subprocess
    try:
        script = ('mkdir -p /root/.ssh && chmod 700 /root/.ssh && '
                  'printf "%s\\n" "$PB_KEY" >> /root/.ssh/authorized_keys && '
                  'chmod 600 /root/.ssh/authorized_keys')
        subprocess.run(["docker", "exec", "-e", f"PB_KEY={ssh_pubkey}", container_name,
                        "sh", "-c", script], capture_output=True, timeout=30, check=False)
    except Exception as e:                              # noqa: BLE001
        logging.info(f"ssh key inject skipped for {container_name}: {e}")


def _post_result_ack(payload) -> bool:
    """POST a terminal /jobs/result and return True ONLY on an acknowledged 2xx response.

    Unlike _post (fire-and-forget: it swallows transport errors AND ignores the HTTP status), the
    watchdog must know the terminal result was actually recorded before it stops watching — a
    timeout, 5xx, or 4xx must NOT count as delivered, or the task is stranded 'running' with the
    seller's unit consumed and the buyer's escrow held. Retrying is safe: /jobs/result is
    idempotent on a terminal task (returns 200 {"idempotent": true})."""
    try:
        r = httpx.post(f"{API_URL}/jobs/result", headers=HEADERS, json=payload, timeout=15, trust_env=False)
    except Exception as e:                              # noqa: BLE001
        logging.error(f"/jobs/result error: {e}")
        return False
    if r.status_code // 100 != 2:
        logging.error(f"/jobs/result not acknowledged: HTTP {r.status_code}")
        return False
    return True


def _post_result_ack_retry(payload, attempts: int = 4) -> bool:
    """_post_result_ack, retried. Returns True once the server acknowledges.

    This path is the LAST word on the task: a failed launch never reaches _register_vm, so the
    watchdog never re-sends for it. Dropping the return value meant one timeout or 5xx left the
    task 'running' server-side forever with the reservation and the buyer's card hold still held,
    while the container was already gone. /jobs/result is idempotent on a terminal task, so
    retrying costs nothing but the wait."""
    delay = 1.0
    for _ in range(max(1, attempts)):
        if _post_result_ack(payload):
            return True
        time.sleep(delay)
        delay = min(delay * 2, 8.0)
    logging.error("terminal result never acknowledged; task may stay 'running' server-side")
    return False


def report_progress(task_id, percent, message=""):
    _post("/jobs/progress", {"task_id": task_id, "percent": percent, "message": message})


def report_log(task_id, line):
    _post("/jobs/log", {"task_id": task_id, "line": line})


def _restore_volume(volume, restore_ref, task_id):
    """Download via a pre-signed GET URL, VERIFY the signed hash, decrypt, restore."""
    if not restore_ref:
        return
    try:
        import hashlib, subprocess, os as _os
        from cryptography.fernet import Fernet
        g = httpx.post(f"{API_URL}/jobs/restore_url", headers=HEADERS, timeout=15,
                       json={"task_id": task_id, "snapshot_ref": restore_ref}, trust_env=False).json()
        enc = safe_fetch.get(g["download_url"], timeout=120, max_bytes=128 * 1024 * 1024).content
        if g.get("content_hash") and hashlib.sha256(enc).hexdigest() != g["content_hash"]:
            report_log(task_id, "RESTORE INTEGRITY CHECK FAILED — aborting")
            return
        data = Fernet(g["enc_key"].encode()).decrypt(enc)      # client-side decrypt
        _os.makedirs(f"/var/lib/petabyte/vol/{volume}", exist_ok=True)
        local = f"/tmp/{volume}-restore.tar"
        open(local, "wb").write(data)
        subprocess.check_call(["tar", "-xf", local, "-C", f"/var/lib/petabyte/vol/{volume}"])
        report_log(task_id, f"restored {volume} from {restore_ref} (verified)")
    except Exception as e:                              # noqa: BLE001
        logging.error(f"restore failed: {e}")


def _backup_once(task, volume):
    """Snapshot -> encrypt -> upload via a one-object pre-signed PUT -> sign checkpoint.
    The node holds NO standing object-storage credentials."""
    tid = task["task_id"]
    try:
        import subprocess, hashlib, time as _tt
        from cryptography.fernet import Fernet
        local = f"/tmp/{volume}-{int(_tt.time())}.tar"
        subprocess.check_call(["tar", "-cf", local,
                               "-C", f"/var/lib/petabyte/vol/{volume}", "."])
        grant = httpx.post(f"{API_URL}/jobs/backup_url", headers=HEADERS, timeout=15,
                           json={"task_id": tid,
                                 "filename": f"{volume}-{int(_tt.time())}.tar.enc"}, trust_env=False).json()
        enc = Fernet(grant["enc_key"].encode()).encrypt(open(local, "rb").read())
        httpx.put(grant["upload_url"], content=enc, timeout=300, trust_env=False)
        h = hashlib.sha256(enc).hexdigest()             # hash of the uploaded bytes
        proof = {"task_id": tid, "output_hash": h[:16], "ts": int(_tt.time())}
        _post("/jobs/checkpoint", {"task_id": tid, "snapshot_ref": grant["snapshot_ref"],
                                   "size_bytes": len(enc), "content_hash": h,
                                   "proof": proof, "signature": crypto.sign_proof(proof)})
        report_log(tid, f"backup -> {grant['snapshot_ref']} ({len(enc)} bytes, encrypted)")
    except Exception as e:                              # noqa: BLE001
        logging.error(f"backup failed: {e}")


def _start_backup_thread(task):
    """Periodic backups for a stateful task (recovery point = interval)."""
    if not task.get("backup_enabled"):
        return None
    interval = max(30, int(task.get("backup_interval_s") or 300))
    volume = task.get("volume") or "task-data"
    stop = threading.Event()
    def loop():
        while not stop.wait(interval):
            _backup_once(task, volume)
    threading.Thread(target=loop, daemon=True).start()
    return stop


# Live template jobs keyed by vm_id, so the heartbeat thread can checkpoint one on a preempt signal.
_LIVE_TEMPLATES = {}
_PREEMPT_HANDLED = set()


def _handle_preempt(entry):
    """Server flagged this spot VM for reclaim: checkpoint NOW (graceful) and stop periodic backups.
    Once per vm_id — the grace window is short and the server finalizes + the orphan reap tears the
    container down. Best-effort; correctness rests on periodic checkpoints + server hard-kill."""
    vm_id = entry.get("vm_id")
    if not vm_id or vm_id in _PREEMPT_HANDLED:
        return
    live = _LIVE_TEMPLATES.get(vm_id)
    if not live:
        return
    _PREEMPT_HANDLED.add(vm_id)
    logging.info(f"preempt: checkpointing vm {vm_id} before reclaim ({entry.get('deadline_s')}s grace)")
    try:
        _backup_once(live["task"], live["volume"])       # one last recovery point
    finally:
        if live.get("stop"):
            live["stop"].set()                            # halt the periodic backup loop


def _egress_flags(task):
    """Networked jobs require a validated private bridge; host networking is never allowed."""
    policy = (task.get("egress") or "none").lower()
    if policy in ("limited", "open"):
        return []  # caller must attach network_policy.ensure(task_id), never the default bridge
    return ["--network", "none"]


def _host_ram_bytes():
    try:
        import os as _o
        return _o.sysconf("SC_PAGE_SIZE") * _o.sysconf("SC_PHYS_PAGES")
    except Exception:                                    # noqa: BLE001
        return 0


def _default_mem_cap():
    """A conservative RAM cap for a job the server didn't size: total host RAM minus a headroom
    reserve, so a runaway container can't OOM-kill the agent/host. None on hosts we can't measure
    (e.g. non-Linux desktop, where Docker Desktop's own VM already bounds container memory)."""
    total = _host_ram_bytes()
    if total <= 0:
        return None
    reserve = 2 * 1024 ** 3                              # keep ~2 GiB for the host + agent
    return str(max(1024 ** 3, total - reserve)) + "b"   # never below 1 GiB


def _default_cpu_cap():
    """Leave the host at least one core so a job can't peg every CPU and starve the agent."""
    try:
        import os as _o
        n = _o.cpu_count() or 1
    except Exception:                                    # noqa: BLE001
        return None
    return str(max(1, n - 1))


def _env_true(name: str) -> bool:
    return os.getenv(name, "false").strip().lower() in ("1", "true", "yes", "on")


def _select_runtime(available_runtimes: str, is_gpu: bool):
    """Choose the OCI runtime for `--runtime`, or None for docker's default (runc).

    NEVER weakens isolation below gVisor. Kata Containers runs each container in its OWN hardware VM
    (KVM) — the strongest boundary — and is now the DEFAULT whenever the kata runtime is available.
    Selection:
      * AGENT_RUNTIME=runc                                      -> docker default (no extra runtime)
      * AGENT_RUNTIME=gvisor|runsc, or AGENT_KATA_ENABLED=false -> opt OUT to gVisor (else default)
      * default / AGENT_RUNTIME=kata -> Kata when installed AND (CPU job, or GPU job with
        AGENT_KATA_GPU=true); otherwise SAFELY falls back to gVisor/default — never fails, and never
        runs a GPU job on a Kata that cannot pass the GPU through. GPU-in-VM stays gated on
        AGENT_KATA_GPU because most nodes lack VFIO.

    `available_runtimes` is the `docker info --format {{.Runtimes}}` string; the Kata runtime is
    registered under one of several names depending on the install (kata / kata-runtime / …)."""
    import re
    # `docker info --format {{.Runtimes}}` is `map[NAME:{PATH ARGS} ...]`; match the map KEYS
    # (the runtime names passed to --runtime), never the path (which itself contains "kata-runtime").
    names = set(re.findall(r"([A-Za-z0-9_.-]+):\{", available_runtimes or ""))

    def present(*cands):
        return next((c for c in cands if c in names), None)

    kata = present("kata-runtime", "kata-qemu", "kata-clh", "kata", "io.containerd.kata.v2")
    gvisor = present("runsc")
    pref = (os.getenv("AGENT_RUNTIME") or "auto").strip().lower()
    if pref == "runc":
        return None
    # Opt OUT of Kata (back to gVisor) via either control.
    if pref in ("gvisor", "runsc") or os.getenv("AGENT_KATA_ENABLED", "").strip().lower() in ("0", "false", "no", "off"):
        return gvisor
    # DEFAULT: prefer Kata (VM per container) whenever it is available. A GPU job still needs
    # AGENT_KATA_GPU (VFIO) or it falls back to gVisor.
    if kata and (not is_gpu or _env_true("AGENT_KATA_GPU")):
        return kata
    return gvisor   # kata unavailable / GPU-without-VFIO -> gVisor


def _isolation_flags(task):
    """Hardening flags applied to EVERY buyer container — this protects the SELLER's
    host from the buyer's workload (see docs/isolation-roadmap.md).

    Mirrors the notebook sandbox (notebook.py):
      * --cap-drop ALL              — the job gets NO Linux capabilities, so it cannot
                                       add routes / reconfigure the host firewall
                                       (NET_ADMIN, NET_RAW), load kernel modules
                                       (SYS_MODULE), or otherwise escalate. This is
                                       also what makes the host egress firewall
                                       (install.sh DOCKER-USER rules) un-bypassable
                                       from inside a job.
      * --security-opt no-new-privileges — no setuid escalation.
      * --pids-limit                — fork-bomb cap.
      * --memory/--memory-swap/--cpus — sized to the booking when the server sends them
                                       (never guessed high, so a big legit rental isn't
                                       throttled; absent -> only the pids cap applies).
    gVisor (runsc) is used as a user-space kernel boundary when installed. GPU jobs keep
    device access (the NVIDIA runtime injects the device via cgroups, not capabilities),
    so dropping ALL caps does not break --gpus.

    STRICT ROOTFS (opt-in via the server's AGENT_STRICT_ROOTFS / AGENT_CONTAINER_USER):
      * read_only -> --read-only (immutable rootfs) + a small writable /tmp tmpfs + HOME=/tmp,
        so a workload's own caches (pip, torch/CUDA JIT, ~/.cache) still land somewhere writable
        without persisting to — or tampering with — the image. This is the CIS/PodSecurity
        "restricted" baseline the notebook sandbox (notebook.py) already runs unconditionally.
      * run_as  -> --user, forcing the job non-root.
    Both default OFF: unlike the notebook path (which owns its image), a buyer job runs an
    ARBITRARY image — an s6-overlay server, a cache-writing model server — that may not tolerate a
    read-only rootfs or a forced UID. Operators flip these on once they've validated their image
    mix; the default profile is byte-for-byte the pre-existing hardening.
    """
    import subprocess
    flags = ["--cap-drop", "ALL",
             "--security-opt", "no-new-privileges",
             "--pids-limit", str(task.get("pids") or 1024)]
    # Some images start as root, chown their data dir, then drop to an unprivileged user
    # (gosu/su-exec/s6/linuxserver, most game servers). --cap-drop ALL removes CAP_SETUID/SETGID so
    # that drop fails ("operation not permitted") and the container exits at boot. Re-add ONLY the
    # server-declared minimal init caps (SETUID/SETGID/CHOWN/DAC_OVERRIDE/FOWNER — never
    # NET_ADMIN/SYS_ADMIN/NET_RAW/SYS_MODULE), so a chown-and-drop entrypoint runs while the host
    # stays protected. Validated against the whitelist so a compromised server can't inject a
    # dangerous cap.
    # SYS_CHROOT: needed by the `shell` (raw SSH box) template — sshd's privilege-separation
    # pre-auth child chroots to /run/sshd; without it every login dies "chroot: Operation not
    # permitted [preauth]" during KEX. SYS_CHROOT only permits chroot(), not a host escape.
    # AUDIT_WRITE lets sshd write its login/logout audit records (linux_audit_write_entry); without
    # it the session is authenticated then closed post-auth. Both are in Docker's default cap set.
    _ALLOWED_INIT_CAPS = {"SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETPCAP", "SYS_CHROOT", "AUDIT_WRITE"}
    for _cap in (task.get("init_caps") or []):
        _c = str(_cap).upper().replace("CAP_", "")
        if _c in _ALLOWED_INIT_CAPS:
            flags += ["--cap-add", _c]
    # RAM/CPU caps: sized to the booking when the server sends them; otherwise fall back to a
    # CONSERVATIVE host-derived default (never "no cap"). Without this a buyer container could
    # allocate all host RAM and OOM-kill the seller's box + the agent, or pin every CPU (H6).
    mem = task.get("memory") or _default_mem_cap()
    if mem:
        flags += ["--memory", str(mem), "--memory-swap", str(mem)]   # cap RAM; no swap escape
    cpus = task.get("cpus") or _default_cpu_cap()
    if cpus:
        flags += ["--cpus", str(cpus)]
    # SECURITY (peer abuse — DoS the seller + co-tenants): bound the resources a buyer job can
    # exhaust beyond RAM/CPU/pids. --shm-size caps /dev/shm (default 64 MB; unbounded shm is a
    # host-memory DoS and an IPC surface), and --ulimit caps file descriptors and processes so a
    # job can't exhaust host-wide kernel resources shared with co-tenants and the agent.
    flags += ["--shm-size", str(task.get("shm_size") or "64m"),
              "--ulimit", f"nofile={int(task.get('nofile') or 4096)}",
              "--ulimit", f"nproc={int(task.get('nproc') or 2048)}"]
    # Writable-layer disk quota (opt-in): the container's writable layer and the per-task volume
    # are otherwise unbounded, so a buyer can fill the seller's disk and take the host + every
    # co-tenant down. --storage-opt size=NNg needs an overlay2+pquota (xfs/btrfs) backing store, so
    # it errors on ext4 and is applied ONLY when the operator sets AGENT_JOB_DISK_GB (they know
    # their storage driver supports it). A bounded scratch is always provided via the read_only tmpfs.
    _disk_gb = os.getenv("AGENT_JOB_DISK_GB")
    if _disk_gb and str(_disk_gb).strip().isdigit():
        flags += ["--storage-opt", f"size={int(_disk_gb)}G"]
    if task.get("read_only"):
        # Immutable rootfs + a writable scratch. The tmpfs counts against the RAM cap, so keep it
        # modest; HOME=/tmp routes user/CUDA/pip caches onto it instead of the read-only rootfs.
        flags += ["--read-only",
                  "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
                  "-e", "HOME=/tmp"]
    run_as = task.get("run_as")
    if run_as:
        flags += ["--user", str(run_as)]   # force non-root (the image must tolerate the UID)
    try:
        info = subprocess.check_output(["docker", "info", "--format", "{{.Runtimes}}"],
                                       text=True, timeout=5)
        rt = _select_runtime(info, bool(task.get("gpu")))
        if rt:
            flags = ["--runtime", rt] + flags   # Kata (VM per container) or gVisor (user-space kernel)
    except Exception:
        pass
    return flags


from nb_fetch import prefetch_notebook as _prefetch_notebook


_VRAM_WIPE_PY = (
    "import torch\n"
    "for d in range(torch.cuda.device_count()):\n"
    "    torch.cuda.set_device(d)\n"
    "    free,_ = torch.cuda.mem_get_info()\n"
    "    n = int(free*0.92)\n"
    "    if n > 0:\n"
    "        buf = torch.zeros(n, dtype=torch.uint8, device='cuda')  # overwrite free VRAM with 0s\n"
    "        del buf\n"
    "    torch.cuda.synchronize(); torch.cuda.empty_cache()\n"
    "print('pb-vram-wiped', torch.cuda.device_count())\n")


def _cuda_wipe_image():
    """Pick a locally-CACHED CUDA-capable image to run the VRAM memset, so wiping never triggers a
    multi-GB pull on the teardown/claim hot path. Returns None if none is cached (caller then just
    logs a recommendation rather than stalling)."""
    import subprocess
    for img in gpu_runtime.wipe_image_candidates():
        if not img:
            continue
        try:
            if subprocess.run(["docker", "image", "inspect", img],
                              capture_output=True, timeout=10).returncode == 0:
                return img
        except Exception:                                # noqa: BLE001
            return None
    return None


def _wipe_gpu_vram(tid=None):
    """Best-effort: zero the GPU's FREE VRAM so residual data from the previous tenant cannot be
    read by the next one (P-2). Strategy: (1) try `nvidia-smi --gpu-reset` (fast, no image) which
    clears memory when no compute clients are attached; (2) else run a short torch memset in a
    LOCALLY-CACHED CUDA image (--rm --gpus all --network none, time-bounded). Limits (documented in
    docs/SECURITY_AUDIT_PEER_TO_PEER.md): it cannot clear memory another live process still holds,
    coverage is ~92% of free VRAM, and the real fix is MIG/MPS partitioning. Never raises, never
    blocks a job for more than the timeout."""
    import shutil, subprocess
    if not shutil.which("docker") or not gpu_runtime.has_gpu():
        return False
    _reset = gpu_runtime.gpu_reset_cmd()                 # (1) driver-level reset — cleanest when it works
    if _reset:
        try:
            if subprocess.run(_reset, capture_output=True, timeout=60).returncode == 0:
                return True
        except Exception:                                # noqa: BLE001
            pass
    img = _cuda_wipe_image()                              # (2) overwrite free VRAM via a cached CUDA image
    if not img:
        logging.info("VRAM wipe skipped: no cached CUDA image (set VRAM_WIPE_IMAGE or enable MIG); "
                     "residual VRAM from the prior tenant is NOT cleared on this node")
        return False
    try:
        result = subprocess.run(["docker", "run", "--rm", *gpu_runtime.docker_gpu_args(), "--network", "none",
                        "--label", (f"pb.task={tid}" if tid else "pb.kind=vram-wipe"),
                        img, "python", "-c", _VRAM_WIPE_PY],
                       capture_output=True, timeout=int(os.getenv("VRAM_WIPE_TIMEOUT_S", "180")))
        return result.returncode == 0
    except Exception:                                    # noqa: BLE001 — the CALLER decides fail-open vs closed
        return False


def _refuse_unclean_vram(task):
    """Fail-closed handler when a mandatory VRAM wipe could not be verified: tell the buyer why and
    report the job failed so it is retried on a node that can wipe (and not billed here)."""
    tid = task.get("task_id")
    msg = ("Refused: the GPU's VRAM could not be verified clear of the previous tenant's data "
           "(wipe failed/unavailable). Retry — the platform will place this on a node that can "
           "wipe. Operators: cache a CUDA image (VRAM_WIPE_IMAGE) or enable MIG.")
    logging.error(f"[{tid}] VRAM wipe unverified — refusing job")
    try:
        report_log(tid, msg)
    except Exception:                                    # noqa: BLE001
        pass
    # FAILED, not the _submit_signed default "completed": a refusal ran nothing, and reporting it as
    # completed credited the node and tried to settle the booking while the VM sat "starting".
    _post("/jobs/result", _signed_result(tid, status="failed", result=msg,
                                         failure_cause="vram_wipe_unverified"))
    _set_ui(status="idle", task=None, fail=True)
    _mark_gpu_unusable("mandatory VRAM wipe failed")


def _mark_gpu_unusable(why):
    """Stop offering this node (selling_now=False) and re-test idle every 10 min; re-offer it the
    moment a GPU container passes the wipe. ponytail: also pauses CPU jobs on this node — fine for
    GPU sellers; split per job type if CPU-only work ever matters here."""
    if _GPU_UNUSABLE.is_set():
        return
    _GPU_UNUSABLE.set()
    logging.error(f"GPU containers unusable ({why}): this node is NOT offered to buyers until an idle "
                  "self-test passes (re-checked every 10 min). Check the host with: docker run --rm "
                  "--gpus all <CUDA image> python -c 'import torch; print(torch.cuda.is_available())'")

    def _recheck():
        while True:
            time.sleep(600)
            if _rental_live():
                continue                                 # never grab VRAM under a paying buyer
            if not _cuda_wipe_image():
                _ensure_wipe_image_async()               # image missing -> (re)pull, test next round
            elif _wipe_gpu_vram():
                _GPU_UNUSABLE.clear()
                logging.warning("GPU container self-test passed: offering this node to buyers again")
                return
    threading.Thread(target=_recheck, daemon=True, name="pb-gpu-selftest").start()
    threading.Thread(target=lambda: _send_diagnostics(why), daemon=True, name="pb-diagnostics").start()


_FIX_REPORT = threading.Lock()


def _report_fix_results():
    """Tell support what the fix runner did, after re-checking the GPU (a passing self-test re-offers
    the node). Runs off the heartbeat thread: the self-test can take minutes."""
    try:
        import diagnostics
        for fid, rc, output in _fixes.results():
            selftest = None
            if gpu_runtime.has_gpu() and not _rental_live() and _cuda_wipe_image():
                selftest = bool(_wipe_gpu_vram())
                if selftest and _GPU_UNUSABLE.is_set():
                    _GPU_UNUSABLE.clear()
                    logging.warning("GPU self-test passed after a support fix: offering this node again")
            try:
                r = httpx.post(f"{API_URL}/nodes/fixes/{fid}/result", headers=HEADERS, timeout=30,
                               trust_env=False,
                               json={"spec_id": int(SPEC_ID), "rc": rc, "selftest_passed": selftest,
                                     "output": diagnostics.redact(output)[-50_000:]})
            except Exception as e:                       # noqa: BLE001 — retried next heartbeat
                logging.info(f"fix #{fid} result not sent yet: {e}")
                continue
            if r.status_code in (200, 404, 409):         # recorded, or the server will never take it
                _fixes.ack(fid)
                logging.warning(f"support fix #{fid} finished (rc {rc}); reported to Petabyte support")
    finally:
        _FIX_REPORT.release()


def _send_diagnostics(why):
    """Send support the node's diagnostics report (diagnostics.py: fixed read-only checks, masked;
    opt out with PB_SHARE_DIAGNOSTICS=false). Best-effort: never blocks or breaks the agent."""
    try:
        import diagnostics
        _report, reply = diagnostics.send(why, gpu_test=not _rental_live())
        if reply:
            logging.warning("sent a diagnostics report to Petabyte support so they can fix this node")
    except Exception as e:                               # noqa: BLE001
        logging.info(f"diagnostics report not sent: {e}")


def _gpu_startup_selftest():
    """Prove a GPU container works BEFORE the first heartbeat offers this node, so a host whose
    containers can't use CUDA never costs a buyer a refused launch. Only when the wipe image is
    already cached (a first-boot pull is covered by the refusal path) and the wipe is mandatory."""
    if (os.getenv("AGENT_ALLOW_UNVERIFIED_VRAM", "false").lower() == "true"
            or not gpu_runtime.has_gpu() or not _cuda_wipe_image() or _rental_live()):
        return                                           # a restored live rental owns the GPU
    if not _wipe_gpu_vram():
        _mark_gpu_unusable("startup GPU container self-test failed")


def _rental_live():
    with _pb_vm_lock:
        return any(not v.get("reported") for v in _pb_vm_watch.values())


def _dns_flags():
    """Pin a networked job's DNS resolver to a trusted public one so the SELLER's host cannot
    simply lie in DNS to hijack the buyer's `pip`/`git`/model downloads (P-3). This raises the bar
    only — a root host still controls routing and can IP-MITM, so a buyer must still verify
    downloads (HTTPS, `pip --require-hashes`, pinned digests). Operator-tunable via AGENT_JOB_DNS
    (comma-separated); empty string disables pinning (use the host resolver)."""
    raw = os.getenv("AGENT_JOB_DNS", "1.1.1.1,9.9.9.9")
    flags = []
    for s in raw.split(","):
        s = s.strip()
        if s:
            flags += ["--dns", s]
    return flags


def _task_volume(task) -> str:
    """Per-task Docker volume name for a template's cache/work dir. MUST be unique per rental so
    one buyer's working data (notebooks, HF token, model cache, game saves) can never appear in the
    next buyer's rental of the same template on this host."""
    return f"pb-vol-t{task['task_id']}-{task.get('template', 'tpl')}"


# Can this host isolate a networked (serving) rental? network_policy refuses every one on Docker
# Desktop, rootless Docker, non-root or a remote daemon, while --network none batch jobs still run —
# so the node looked healthy and every buyer app on it was refunded. Probed at startup, re-probed
# hourly (every 5 min while failing), reported on the heartbeat so the server stops placing apps.
_JOB_NET = {"ok": None, "reason": None, "hint": None, "repaired": False}


def _set_job_network(ok, reason=None):
    try:
        import network_policy
        hint = None if ok else network_policy.hint(reason)
    except Exception:                                    # noqa: BLE001 — advice must never break a refusal
        hint = None
    if (ok, reason) != (_JOB_NET["ok"], _JOB_NET["reason"]) and not ok:
        logging.warning(f"this node can only run batch jobs: {reason}. To run apps: {hint}")
    _JOB_NET.update(ok=ok, reason=reason, hint=hint)
    try:
        import ui
        ui.agent_status["job_network"] = dict(_JOB_NET)
    except Exception:                                    # noqa: BLE001
        pass


def _probe_job_network():
    try:
        import isolation
        import network_policy
        # repaired: the auto-update's isolation repair (isolation.py, run as root) changed this host
        _JOB_NET["repaired"] = bool(isolation.last_repair().get("repaired"))
        _set_job_network(*network_policy.probe())
    except Exception as e:                               # noqa: BLE001 — never break the agent
        logging.info(f"job network probe skipped: {e}")


_BUNDLE_FILE = "/var/lib/petabyte-agent/bundle.sha256"   # update.sh / install.sh write it


def _agent_bundle():
    """sha256 of the signed agent bundle this node last applied, or None (git/dev installs).
    Re-read every heartbeat: update.sh records it after a run that didn't restart the agent."""
    try:
        with open(_BUNDLE_FILE) as f:
            sha = f.read().strip()
    except OSError:
        return None
    return sha if len(sha) == 64 and all(ch in "0123456789abcdef" for ch in sha) else None


def _job_network_loop():
    last = time.time()
    while True:
        time.sleep(300)                                  # a launch failure is re-checked within 5 min
        if not _JOB_NET["ok"] or time.time() - last >= 3600:
            _probe_job_network()
            last = time.time()


def _ensure_job_network(tid):
    """(network name, None), or (None, reason) when the per-job bridge can't be built."""
    try:
        import network_policy
        return network_policy.ensure(tid), None
    except Exception as e:                                   # noqa: BLE001 — never crash a job
        # network_policy.ensure() raises NetworkUnavailable with a DIFFERENT message for each of
        # ~8 distinct refusals (not root, remote daemon, docker network create failed, firewall
        # policy drift...). Discarding it left every one of them as the same unactionable line,
        # so the operator could not tell a missing iptables binary from a hijacked chain (PET-144).
        logging.warning("job network refused for task %s: %s: %s",
                        tid, type(e).__name__, e)
        reason = str(e) or type(e).__name__
        _set_job_network(False, reason)                  # the real outcome beats the probe
        return None, reason


def _apply_egress_bandwidth_cap(net):
    """OPT-IN, best-effort: cap a job network's OUTBOUND bandwidth so a buyer cannot saturate the
    seller's uplink with high-volume transfers (the seller pays for/answers for that traffic). Set
    AGENT_EGRESS_MBIT (e.g. 100) to enable; unset leaves the link uncapped (unchanged). Applied via
    `tc` on the per-job bridge and torn down with the network. Never raises — if tc/root is
    unavailable the job still runs, just uncapped."""
    mbit = os.getenv("AGENT_EGRESS_MBIT", "").strip()
    if not mbit or not mbit.isdigit() or int(mbit) <= 0:
        return
    import shutil, subprocess
    if not shutil.which("tc"):
        logging.info("AGENT_EGRESS_MBIT set but `tc` is missing (apt-get install iproute2) — "
                     "egress bandwidth left uncapped")
        return
    try:
        nid = subprocess.run(["docker", "network", "inspect", net, "-f", "{{.Id}}"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        if not nid:
            return
        iface = "br-" + nid[:12]
        subprocess.run(["tc", "qdisc", "replace", "dev", iface, "root", "tbf",
                        "rate", f"{int(mbit)}mbit", "burst", "32kbit", "latency", "400ms"],
                       capture_output=True, timeout=15)
    except Exception:                                    # noqa: BLE001 — never fail a job over shaping
        pass


def _cleanup_job_resources(tid, name=None):
    """Best-effort teardown of one job's container, per-task volume(s) and per-task network. Called
    when the watchdog sees the container gone (or on stop) so a finished rental leaves no data and
    no leaked disk/network on the host. Everything is scoped by the pb.task=<tid> label, so it can
    only ever remove THIS rental's resources."""
    import subprocess
    _kill_reverse_tunnel(tid)                            # drop the ssh -R for this rental (if any)
    try:
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=pb.task={tid}"],
                             capture_output=True, text=True, timeout=15).stdout.split()
        for cid in ids:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
        vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"label=pb.task={tid}"],
                              capture_output=True, text=True, timeout=15).stdout.split()
        for v in vols:
            subprocess.run(["docker", "volume", "rm", v], capture_output=True, timeout=15)
        removed = subprocess.run(["docker", "network", "rm", f"pb-net-t{tid}"], capture_output=True, timeout=15)
        if removed.returncode == 0:
            import network_policy
            network_policy.cleanup(tid)
    except Exception:                                    # noqa: BLE001
        pass


def _free_host_port():
    """A currently-free TCP host port. Interactive templates used to bind the container port on the
    host verbatim (0.0.0.0:8888:8888), so a SECOND VM on the same node collided on 8888 and failed
    with "port is already allocated" — one interactive VM per node, ever. Mapping an ephemeral host
    port per rental removes that limit (and means a leftover container can't block new VMs). Small
    TOCTOU window: we close the probe socket before docker binds, and docker's own bind is the
    source of truth — a rare race just surfaces as a launch failure the buyer retries."""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_template_port(host_port, container_name, timeout_s=180):
    """Confirm a service accepts TCP before its VM route starts metering.

    A running container does not prove its browser service started. An unhealthy
    Jupyter process must not leave a buyer paying for an unreachable VM.
    """
    import socket
    import subprocess
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", int(host_port)), timeout=1):
                return True
        except OSError:
            pass
        try:
            state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                                   capture_output=True, text=True, timeout=5)
            if state.returncode != 0 or state.stdout.strip() != "true":
                return False
        except Exception:  # noqa: BLE001 — retry a transient Docker hiccup until timeout
            pass
        time.sleep(2)
    return False


def _start_ready_poll(tid, name, host_port, path):
    import threading
    threading.Thread(target=_await_ready, args=(tid, name, host_port, path),
                     name=f"pb-ready-{tid}", daemon=True).start()


def _await_ready(tid, name, host_port, path):
    """Poll http://127.0.0.1:<host_port><path> until it answers 200, then report 'ready'. Stops
    when the watchdog has reported the container (it died) or after PB_READY_TIMEOUT_S (a 30 GB
    model download can take a long time; never ready = never billed, so a long limit is safe)."""
    deadline = time.monotonic() + int(os.getenv("PB_READY_TIMEOUT_S", "3600"))
    url = f"http://127.0.0.1:{int(host_port)}{path}"
    while time.monotonic() < deadline:
        with _pb_vm_lock:
            alive = tid in _pb_vm_watch
        if not alive:
            return
        try:
            if httpx.get(url, timeout=3, trust_env=False).status_code == 200:
                _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                           "status": "ready"})
                report_log(tid, "app is ready: health check passed")
                return
        except Exception:                                # noqa: BLE001, S110 — still loading
            pass
        time.sleep(5)
    report_log(tid, f"app did not pass its health check ({path}) in time; it was not billed")


def _start_ollama_pull(tid, name, model):
    threading.Thread(target=_ollama_pull, args=(tid, name, model),
                     name=f"pb-ollama-{tid}", daemon=True).start()


def _ollama_pull(tid, name, model, timeout_s=3600):
    """Pull the buyer's model into their running Ollama container. The template passed it as
    OLLAMA_MODEL, which the official image never reads, so buyers got an empty server.

    No 'loading' state: Ollama answers its API while pulling, and 'loading' means not billed, so it
    would let a buyer use the GPU for free by asking for a model that never finishes. 'ready' is
    posted once the model is in (timeline + Manage page); billing is unchanged."""
    import re
    import subprocess
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}", str(model)):
        report_log(tid, "ollama: invalid model name; not pulled")
        return
    report_log(tid, f"ollama: pulling model {model}")
    err = ""
    for _ in range(12):                                  # the server inside needs a moment to start
        with _pb_vm_lock:
            if tid not in _pb_vm_watch:
                return                                   # rental already over
        try:
            r = subprocess.run(["docker", "exec", name, "ollama", "pull", model],
                               capture_output=True, text=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            report_log(tid, f"ollama: pulling {model} did not finish in {timeout_s // 60} min")
            return
        except Exception as e:                           # noqa: BLE001
            err = type(e).__name__
            break
        if r.returncode == 0:
            report_log(tid, f"ollama: model {model} pulled; ready")
            _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                       "status": "ready"})
            return
        err = _mask_secrets(((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1])[:200]
        if "could not connect" not in err.lower():
            break                                        # a real error (unknown model, disk full)
        time.sleep(5)
    report_log(tid, f"ollama: could not pull {model}: {err}; the server runs without it")


_SECRETISH = None
_pb_log_sent = set()                                 # task ids whose failure log tail was sent


def _mask_secrets(text):
    """Mask token-looking strings (a Jupyter URL token, a Hugging Face token) before text that came
    out of a container or Docker is shown to the buyer."""
    import re
    global _SECRETISH
    if _SECRETISH is None:
        _SECRETISH = re.compile(r"(hf_[A-Za-z0-9]{8,}|(?:token|key|secret|password)=[^\s&]+)", re.IGNORECASE)
    return _SECRETISH.sub("[redacted]", text or "")


def _container_log_tail(name, lines=25):
    """Last lines of a container's output, for the buyer's failure message (secrets masked)."""
    import subprocess
    try:
        r = subprocess.run(["docker", "logs", "--tail", str(lines), name],
                           capture_output=True, text=True, timeout=10, check=False)
    except Exception:                                    # noqa: BLE001
        return ""
    return _mask_secrets(((r.stdout or "") + (r.stderr or "")).strip())[-1800:]


def _launch_failure_reason(e):
    """One short, masked reason for a failed template launch: Docker's own stderr tail for a refused
    `docker run` (pull denied, bad image, port taken, runtime error), the exception type otherwise.
    It used to be a bare "container launch failed", so nobody could tell why a node refused."""
    import subprocess
    if isinstance(e, subprocess.CalledProcessError):
        lines = [ln.strip() for ln in str(e.stderr or "").splitlines() if ln.strip()]
        text = " | ".join(lines[-3:]) or f"docker exited {e.returncode}"
    else:
        text = f"{type(e).__name__}: {e}"
    return _mask_secrets(text)[-300:]


# Server-driven orphan reap. The container watchdog only knows containers THIS process launched
# (in-memory) and only reaps them when they EXIT — so a container from a previous agent run, or one
# whose rental the SERVER stopped/expired while the container keeps running, was never cleaned up
# and kept holding its host port. The heartbeat now returns this node's still-active VM task ids;
# anything labelled pb.kind=template that is NOT in that set (after a grace period, so a
# just-launched container isn't reaped on a stale list) is a dead rental and gets GC'd.
_orphan_since = {}                                   # task_id -> first time we saw it orphaned
_orphan_lock = __import__("threading").Lock()
_REAP_GRACE_S = int(os.getenv("PB_ORPHAN_REAP_GRACE_S", "120"))


def _container_label_task(cid):
    import subprocess
    try:
        out = subprocess.run(["docker", "inspect", "-f",
                              '{{index .Config.Labels "pb.task"}}', cid],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:                                # noqa: BLE001
        return None


def _interactive_labels(task):
    """`--label pb.interactive=1` for an INTERACTIVE rental, nothing for a batch job.

    The orphan reap keys off this label, and it has to mean exactly what the server can vouch for:
    active_vm_task_ids joins through VMRoute, so it lists interactive rentals only. A batch
    template job carries pb.kind=template too and has no route — offered to the reap it would be a
    permanent orphan candidate and get force-removed while someone is paying for it. A vm_id on the
    task is what says a route exists.

    Its own function so both agents share the rule and a test can execute it, rather than reading
    the launch code and hoping.
    """
    return ["--label", "pb.interactive=1"] if task.get("vm_id") else []


def _list_template_containers():
    """Ids of RUNNING interactive-rental containers on this host (extracted so the reap is testable)."""
    import subprocess
    try:
        # pb.interactive=1, NOT pb.kind=template. The server's active set comes from
        # active_vm_task_ids, which joins through VMRoute and therefore lists INTERACTIVE rentals
        # only — a batch template job carries the same pb.kind label and has no route, so it could
        # never appear in that set and was force-removed after the grace period. Reaping a running
        # batch job someone is paying for is a worse outcome than leaking a container, so the
        # selector is narrowed to match exactly what the server can vouch for.
        return subprocess.run(["docker", "ps", "-q", "--filter", "label=pb.interactive=1"],
                              capture_output=True, text=True, timeout=15).stdout.split()
    except Exception:                                # noqa: BLE001
        return []


def _reap_orphan_templates(active_task_ids):
    """GC running INTERACTIVE-rental containers whose rental is no longer active server-side.

    active_task_ids: the set the heartbeat reported (None => server didn't tell us this beat; do
    nothing rather than risk reaping a live rental on missing data)."""
    if active_task_ids is None:
        return
    active = {str(t) for t in active_task_ids}
    ids = _list_template_containers()
    now = time.time()
    seen = set()
    for cid in ids:
        task = _container_label_task(cid)
        if not task:
            continue
        seen.add(task)
        if task in active:
            with _orphan_lock:
                _orphan_since.pop(task, None)        # rental is live again — reset any grace timer
            continue
        with _orphan_lock:
            first = _orphan_since.setdefault(task, now)
        if now - first >= _REAP_GRACE_S:             # orphaned long enough to be sure it's dead
            logging.warning(f"reaping orphan template container for task {task} "
                            f"(not in the server's active set) — freeing its host port")
            _cleanup_job_resources(task, cid)
            with _orphan_lock:
                _orphan_since.pop(task, None)
    # forget grace timers for tasks whose containers are gone entirely
    with _orphan_lock:
        for t in [t for t in _orphan_since if t not in seen]:
            _orphan_since.pop(t, None)


# ---- reverse tunnel (buyer-IP privacy) --------------------------------------------------------
# When PB_TUNNEL_GATEWAY is set, the node binds the container to LOOPBACK and dials OUT with
# `ssh -R` to a locked-down account on the gateway, which binds a gateway-loopback port that
# forwards back here. The node opens ZERO inbound ports; the gateway (and only it) reaches the VM
# at 127.0.0.1:<remoteport>. Unset => the public-IP path is unchanged. Home GPUs behind NAT can
# sell because the connection is OUTBOUND.
_TUN_GW = os.getenv("PB_TUNNEL_GATEWAY", "").strip()          # e.g. pbtun@137.184.198.133
_TUN_KEY = os.getenv("PB_TUNNEL_KEY", "/etc/petabyte/tunnel_key")
_TUN_PORTS = os.getenv("PB_TUNNEL_PORTS", "20000-20050")
_tunnels = {}                                                 # task_id -> (remoteport, Popen)
# Self-enrolled key + known_hosts live in the unit's StateDirectory ($HOME is read-only there).
_TUN_STATE_KEY = "/var/lib/petabyte-agent/tunnel_key"
_TUN_KNOWN_HOSTS = "/var/lib/petabyte-agent/known_hosts"
# Supervised interactive rentals: task_id -> {name, host_port, vm_id, rp, reg, down_since, next_try,
# delay}. Their gateway ports are saved so a restarted agent re-binds the SAME port: the gateway
# routes by port alone, so a restored rental landing on another rental's old port would briefly
# send one buyer's traffic to another buyer's app.
_tun_rentals = {}
_TUN_PORTS_FILE = "/var/lib/petabyte-agent/tunnel_ports.json"
_TUN_PORTS_LOCK = threading.Lock()
_TUN_CONFIRM_S = 20          # wait for ssh's "remote forward success"; > ConnectTimeout=10
_TUN_RETRY_MIN_S, _TUN_RETRY_MAX_S = 15, 120                  # re-open backoff
_TUN_GIVE_UP_S = 600         # then fail the rental: the buyer is not billed for an unreachable VM
_tun_sup_started = threading.Event()
# Set once self-enrollment has settled (enabled, refused, or not applicable). run_agent waits on it
# before claiming work: a JIT droplet is booked seconds after it registers, and enrollment takes ~70s
# (API + the gateway's 60s key sync), so claiming at once refused its first serving job.
_TUN_SETTLED = threading.Event()


def _reverse_tunnel_enabled():
    """A gateway address AND the key to reach it. Both halves are required.

    A node configured with PB_TUNNEL_GATEWAY but no key file would happily CLAIM a serving rental,
    launch the container, then discover it cannot publish it -- and fail the buyer two and a half
    minutes later (see _open_reverse_tunnel). Refusing the job up front instead lets the server
    place it on a node that can actually serve it."""
    return bool(_TUN_GW) and os.path.exists(_TUN_KEY)


def _advertise_selling_now():
    """A JIT standby node is sellable only after its browser tunnel is enrolled.

    Standby bookings create an interactive template immediately. A fresh node can
    heartbeat before the gateway accepts its key; selling it then makes that first
    task fail with network_policy and refunds the buyer. Ordinary sellers retain
    their chosen selling schedule and existing tunnel policy.
    """
    scheduled = _selling_now()
    return scheduled and (not os.getenv("PROVIDER", "").startswith("pb-jit-")
                          or _reverse_tunnel_enabled())


def _TUN_PORT_BUSY(stderr_line: str) -> bool:
    """True when ssh refused this SPECIFIC port, so another one is worth a try.

    ssh says "Warning: remote port forwarding failed for listen port N" when the gateway will not
    bind that port -- taken, or outside the key's permitlisten. Anything else (Permission denied,
    no such identity, Connection refused, host key) is about the connection, and will fail exactly
    the same way on all the remaining ports."""
    low = (stderr_line or "").lower()
    return "port forwarding failed" in low or "address already in use" in low


def _tun_port_range():
    a, _, b = _TUN_PORTS.partition("-")
    return range(int(a), int(b) + 1) if b else range(int(a), int(a) + 1)


def _popen(cmd, capture_stderr=False):
    import subprocess
    # stderr is kept for the tunnel, which otherwise discards the ONE line that says why it failed
    # and leaves the operator reading "no free port" for what was really a rejected key. The caller
    # must drain it for the tunnel's whole life (_drain_ssh_stderr): an unread PIPE fills at ~64 KB
    # and ssh then blocks mid-write, freezing the tunnel.
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, text=capture_stderr or None,
                            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL)


def _drain_ssh_stderr(p, up, last):
    """Read a tunnel's ssh stderr until ssh exits. Sets `up` on OpenSSH's own confirmation that the
    gateway bound the port (a LogLevel=DEBUG1 line); keeps the last non-debug line, which is the one
    that says why ssh died."""
    try:
        for line in p.stderr:
            line = line.strip()
            if "remote forward success" in line:
                up.set()
            elif line and not line.startswith("debug"):
                last[0] = line
    except Exception:                                         # noqa: BLE001 - pipe closed with ssh
        pass


def _open_reverse_tunnel(host_port, tid, prefer=None):
    """Dial OUT to the gateway, binding a gateway-loopback remoteport -> our 127.0.0.1:host_port.
    Returns the remoteport (int) or None. The ssh process is tracked so teardown can kill it.
    `prefer` (the rental's previous port) is tried first; ports other supervised rentals hold are
    skipped, so a re-open never takes a port another buyer's route still points at.

    Only a BUSY PORT is worth retrying on the next port. Every other failure -- no key, key not
    authorized, gateway unreachable -- fails identically on all 51 ports, and the loop used to
    sleep 3s after each one anyway: two and a half minutes of waiting, a message blaming port
    exhaustion, and then teardown of a container that was serving perfectly well. Observed on a
    live node whose snapshot simply had no /etc/petabyte/tunnel_key.

    "Up" means ssh reported the forward bound, not "still alive after 3s": with no ConnectTimeout an
    unreachable gateway kept ssh alive for minutes, so a dead port was registered and metered."""
    if not os.path.exists(_TUN_KEY):
        report_log(tid, f"reverse tunnel key {_TUN_KEY} is missing: this node cannot publish a "
                        "serving rental until the operator installs it")
        return None
    held = {s.get("rp") for t, s in list(_tun_rentals.items()) if t != tid}
    ports = [x for x in _tun_port_range() if x not in held]
    if prefer in ports:
        ports.remove(prefer)
        ports.insert(0, prefer)
    last = ""
    for rp in ports:
        cmd = ["ssh", "-i", _TUN_KEY, "-N", "-T", "-o", "LogLevel=DEBUG1",
               "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={_TUN_KNOWN_HOSTS}",
               "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "BatchMode=yes",
               "-R", f"127.0.0.1:{rp}:127.0.0.1:{host_port}", _TUN_GW]
        p = _popen(cmd, capture_stderr=True)
        up, err = threading.Event(), [""]
        drain = threading.Thread(target=_drain_ssh_stderr, args=(p, up, err), daemon=True,
                                 name=f"pb-tun-{tid}")
        drain.start()
        # ponytail: an ssh that never prints the confirmation but stays up past ConnectTimeout is
        # accepted after _TUN_CONFIRM_S, so a build with different debug wording still serves.
        for _ in range(_TUN_CONFIRM_S * 2):
            if up.is_set() or p.poll() is not None:
                break
            up.wait(0.5)
        if p.poll() is None:                                  # bound (or survived the wait)
            _tunnels[tid] = (rp, p)
            report_log(tid, f"reverse tunnel up: gateway 127.0.0.1:{rp} -> vm (node opens no inbound port)")
            return rp
        drain.join(timeout=5)
        last = err[0]
        if not _TUN_PORT_BUSY(last):
            report_log(tid, f"reverse tunnel refused by the gateway, not retrying the other "
                            f"ports: {last[:200]}")
            return None
    report_log(tid, f"reverse tunnel FAILED: no free gateway port in {_TUN_PORTS} ({last[:120]})")
    return None


def _tunnel_key_accepted(gw, port):
    """True once the gateway lets our key bind its first port: an `ssh -R` still up after 4s took."""
    p = _popen(["ssh", "-i", _TUN_STATE_KEY, "-N", "-T", "-o", "LogLevel=ERROR", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={_TUN_KNOWN_HOSTS}",
                "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
                "-R", f"127.0.0.1:{port}:127.0.0.1:9", gw])
    time.sleep(4)
    up = p.poll() is None
    try:
        p.terminate()
    except Exception:                                         # noqa: BLE001
        pass
    return up


def _ensure_tunnel_async():
    """Self-enroll the reverse tunnel when the operator did not hand-configure one. Before this every
    serving template (Jupyter, vLLM, Blender, Spaces...) was refused `network_policy` on any node
    nobody enrolled by hand on the gateway — every new node since 2026-09-17. Makes a node-local
    ed25519 key (the private half never leaves this machine), registers the public half, and turns
    the tunnel on only once the gateway ACCEPTS the key (its sync runs every ~60s), so this node
    never claims a serving rental it cannot publish."""
    import shutil, subprocess
    if _TUN_GW or not (shutil.which("ssh") and shutil.which("ssh-keygen")):
        _TUN_SETTLED.set()
        return                                                # hand-configured, or no ssh client

    def _run():
        try:
            _enroll()
        finally:
            _TUN_SETTLED.set()

    def _enroll():
        global _TUN_GW, _TUN_KEY, _TUN_PORTS
        try:
            if not os.path.exists(_TUN_STATE_KEY):
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"pb-node-{SPEC_ID}",
                                "-f", _TUN_STATE_KEY], check=True, capture_output=True, timeout=30)
            pub = open(_TUN_STATE_KEY + ".pub").read().strip()
        except Exception as e:                                # noqa: BLE001 — never crash the agent
            logging.warning(f"reverse tunnel: could not create a key: {e}")
            return
        for _ in range(40):                                   # ~20 min: enroll + the gateway key sync
            try:
                r = httpx.post(f"{API_URL}/node/tunnel", headers=HEADERS, timeout=20, trust_env=False,
                               json={"spec_id": int(SPEC_ID), "public_key": pub})
                if r.status_code == 200:
                    gw, ports = r.json()["gateway"], r.json()["ports"]
                    if _tunnel_key_accepted(gw, int(ports.split("-")[0])):
                        _TUN_KEY, _TUN_PORTS = _TUN_STATE_KEY, ports
                        _TUN_GW = gw                          # set LAST: it is what enables serving
                        logging.warning(f"reverse tunnel enrolled: {gw} ports {ports}")
                        return
                elif r.status_code in (400, 403, 404):        # refused: stop asking
                    logging.warning(f"reverse tunnel enrollment refused ({r.status_code}): {r.text[:200]}")
                    return
                elif r.status_code == 503:
                    # Not offered RIGHT NOW (e.g. the server's TUNNEL_GATEWAY briefly missing after a
                    # deploy on 2026-09-24/25). Keep asking: giving up here left the node unservable
                    # until someone restarted its agent.
                    logging.warning(f"reverse tunnel enrollment not offered yet (503); retrying: {r.text[:200]}")
            except Exception as e:                            # noqa: BLE001
                logging.debug(f"reverse tunnel enrollment retry: {e}")
            time.sleep(30)
        logging.warning("reverse tunnel: the gateway never accepted this node's key")
    threading.Thread(target=_run, daemon=True, name="pb-tunnel-enroll").start()


def _kill_reverse_tunnel(tid):
    """Tear down a rental's ssh -R and stop supervising it. The orphan reap passes the task id as
    the str from a docker label while _tunnels is keyed by int, so the reaped rental's tunnel used
    to stay up: normalize here, where every teardown path goes through."""
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        pass
    if _tun_rentals.pop(tid, None) is not None:
        _save_tunnel_ports()
    t = _tunnels.pop(tid, None)
    if t:
        try:
            t[1].terminate()
        except Exception:                                     # noqa: BLE001
            pass


def _save_tunnel_ports():
    """Persist task_id -> gateway port so a restarted agent re-binds the same ports. Best-effort.

    The supervisor, claim and teardown threads all call this. With one shared ".tmp" path, a writer
    paused mid-dump kept writing its OLDER snapshot into the inode another writer had just renamed
    into place: a corrupt file, so a restarted agent re-bound no saved port. The lock orders
    snapshot+write, and a unique temp file keeps a second agent process off this one's file too."""
    import json
    import tempfile
    with _TUN_PORTS_LOCK:
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_TUN_PORTS_FILE), prefix=".tunnel_ports.")
            with os.fdopen(fd, "w") as f:
                json.dump({str(t): s["rp"] for t, s in list(_tun_rentals.items()) if s.get("rp")}, f)
            os.replace(tmp, _TUN_PORTS_FILE)
        except Exception:                                     # noqa: BLE001 - only a preference
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _saved_tunnel_ports():
    """{task_id: gateway port} from the last agent run. Read ONCE, before restoring rewrites it."""
    import json
    try:
        with open(_TUN_PORTS_FILE) as f:
            return {int(k): int(v) for k, v in json.load(f).items()}
    except Exception:                                         # noqa: BLE001 - none saved
        return {}


def _supervise_tunnel(tid, name, host_port, vm_id, rp=None, registered=False):
    """Keep an interactive rental's reverse tunnel alive (see _check_tunnels). Idempotent."""
    if tid in _tun_rentals:
        return
    _tun_rentals[tid] = {"name": name, "host_port": int(host_port), "vm_id": vm_id, "rp": rp,
                         "reg": rp if registered else None, "down_since": None,
                         "next_try": 0.0, "delay": _TUN_RETRY_MIN_S}
    _save_tunnel_ports()
    if not _tun_sup_started.is_set():
        _tun_sup_started.set()
        threading.Thread(target=_tunnel_supervisor, name="pb-tunnel-supervisor", daemon=True).start()


def _tunnel_supervisor():
    while True:
        try:
            _check_tunnels()
        except Exception as e:                                # noqa: BLE001
            logging.error(f"tunnel supervisor error: {e}")
        time.sleep(5)


def _check_tunnels(now=None):
    """One pass over supervised rentals. The ssh -R used to be fire-and-forget: when it exited
    (gateway reboot, a >45s network drop, an agent restart) the rental stayed 'running' and billed
    but unreachable. Re-open it (same port first) and re-register it, with backoff; if it cannot be
    restored within _TUN_GIVE_UP_S, hand the rental to the watchdog to report failed, so the buyer
    is billed only for the time it was reachable."""
    now = time.time() if now is None else now
    for tid, s in list(_tun_rentals.items()):
        with _pb_vm_lock:
            w = _pb_vm_watch.get(tid)
            live = bool(w) and not w.get("reported") and not w.get("fail")
        if not live:                                          # rental over: teardown owns it
            _kill_reverse_tunnel(tid)
            continue
        t = _tunnels.get(tid)
        alive = bool(t) and t[1].poll() is None
        if alive and s["reg"] == t[0]:
            s.update(down_since=None, delay=_TUN_RETRY_MIN_S)
            continue
        if s["down_since"] is None:
            s["down_since"] = now
            report_log(tid, "reverse tunnel is down or unregistered; restoring it")
        if now - s["down_since"] >= _TUN_GIVE_UP_S:
            report_log(tid, f"reverse tunnel could not be restored in {_TUN_GIVE_UP_S // 60} min; "
                            "failing the rental so it is not billed while unreachable")
            with _pb_vm_lock:
                if tid in _pb_vm_watch:
                    _pb_vm_watch[tid]["fail"] = "tunnel_lost"   # the watchdog reports + tears down
            _kill_reverse_tunnel(tid)
            continue
        if now < s["next_try"] or not _reverse_tunnel_enabled():
            continue                                          # backing off / not enrolled yet
        if not alive:
            rp = _open_reverse_tunnel(s["host_port"], tid, prefer=s["rp"])
            if tid not in _tun_rentals:                       # torn down while we were dialing
                _kill_reverse_tunnel(tid)
                continue
            if rp and s["rp"] != rp:
                s["rp"] = rp
                _save_tunnel_ports()
            alive = bool(rp)
        if alive and (not s["vm_id"] or _register_vm_tunnel(s["vm_id"], s["rp"], ip_address="127.0.0.1")):
            s.update(reg=s["rp"], down_since=None, delay=_TUN_RETRY_MIN_S)
            report_log(tid, f"reverse tunnel restored: gateway 127.0.0.1:{s['rp']} (re-registered)")
            continue
        s["next_try"] = now + s["delay"]
        s["delay"] = min(s["delay"] * 2, _TUN_RETRY_MAX_S)


def _publish_flags(port, host_port=None, bind=None):
    """Expose serving workloads only to the authenticated outbound tunnel on loopback."""
    if not port:
        return []
    return ["-p", f"127.0.0.1:{host_port or port}:{port}"]


def _template_env_flags(task, env):
    if not env:
        return []
    import tempfile
    for key, value in env.items():
        if any(char in str(key) + str(value) for char in ("\n", "\r", "\x00")):
            raise ValueError("invalid container environment")
    fd, path = tempfile.mkstemp(prefix="pb-env-", suffix=".env")
    try:
        with os.fdopen(fd, "w") as handle:
            for key, value in env.items():
                handle.write(f"{key}={value}\n")
    except BaseException:
        os.unlink(path)
        raise
    task["_env_file"] = path
    return ["--env-file", path]


def _remove_env_file(task):
    path = task.pop("_env_file", None)
    if path:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _run_template(task):
    """Launch a one-click stack (Ollama/vLLM/ComfyUI/game server/...) and report it."""
    if task.get("port") and not _reverse_tunnel_enabled():
        # SAY WHY. `network_policy` is also what a failed per-job Docker bridge reports, and that
        # other branch writes a task log explaining itself. This one wrote nothing at all, so the
        # buyer-visible record of a refused rental was an empty log plus a cause that points at
        # container networking. On 2026-09-25 that sent a P0 investigation at cloud-init, the
        # standby image and the subnet firewall for hours, when the truth was that this node's
        # reverse tunnel had never enrolled (PET-144). The cause string stays `network_policy` —
        # the server confirms this exact case from its own state in _platform_misplacement(), so
        # the seller is already not charged for it — but the log line must name the real reason.
        report_log(task["task_id"],
                   "template refused: this node has no enrolled reverse tunnel"
                   + (f" (gateway {_TUN_GW} configured, key {_TUN_KEY} missing)" if _TUN_GW
                      else " (no gateway: self-enrolment never succeeded — POST /node/tunnel)")
                   + "; a serving template needs one to publish its port, so the rental is "
                     "refused up front instead of failing the buyer minutes later")
        _post("/jobs/result", _signed_result(task["task_id"], status="failed", failure_cause="network_policy"))
        _set_ui(status="idle", task=None, fail=True)
        return
    tid = task["task_id"]
    _set_ui(status="running", task=f"Template {task.get('template')} #{tid}")
    _restore_volume(task.get("volume"), task.get("restore_from"), task["task_id"])
    _backup_stop = _start_backup_thread(task)
    if task.get("vm_id"):        # register so the heartbeat can checkpoint this job on a preempt signal
        _LIVE_TEMPLATES[task["vm_id"]] = {"task": task, "volume": task.get("volume") or "task-data",
                                          "stop": _backup_stop}
    image = task.get("image"); port = task.get("port")
    # Ephemeral host port per rental so a second VM on this node doesn't collide on the container
    # port (the old fixed 0.0.0.0:port:port meant one interactive VM per node). The gateway reaches
    # the VM at node_ip:host_port — this is exactly what we register as the tunnel port below.
    host_port = _free_host_port() if port else None
    params = task.get("params", {})
    report_progress(tid, 10, f"pulling {image}")
    import shutil, subprocess, uuid as _uuid
    if not shutil.which("docker"):
        _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                   "status": "failed"})
        return
    name = f"pb-{task.get('template')}-{_uuid.uuid4().hex[:8]}"
    # SECURITY (tenant isolation): every job container is labelled with its task id so the
    # watchdog (and any operator) can find, stop and GC exactly this rental's resources, and so
    # a per-task volume/network is never confused with another tenant's.
    cmd = ["docker", "run", "-d", "--name", name,
           "--label", f"pb.task={tid}", "--label", "pb.kind=template"]
    if task.get("health") and host_port:
        cmd += ["--label", f"pb.health={task['health']}", "--label", f"pb.health_port={host_port}"]
    cmd += _interactive_labels(task)           # only an interactive rental is a reap candidate
    if task.get("vm_id") and host_port:        # lets a restarted agent re-open + re-register its tunnel
        cmd += ["--label", f"pb.vm_id={task['vm_id']}", "--label", f"pb.host_port={host_port}"]
    # Petabyte Spaces (persistent): restart the app in place on a crash/OOM — same container, same
    # host port, so the reverse tunnel stays valid. The watchdog already treats Docker's "restarting"
    # state as alive, so this needs no watchdog change; after 5 straight failures the container ends
    # "exited" and the watchdog fails the rental (a broken repo can't bill forever).
    if task.get("restart"):
        cmd += ["--restart", "on-failure:5"]
    # Reverse-tunnel mode binds the container to LOOPBACK (nothing public on this node); the gateway
    # reaches it via the outbound ssh -R opened after start. Otherwise publish on the public NIC.
    _rev = _reverse_tunnel_enabled() and bool(port)
    cmd += _publish_flags(port, host_port, bind="127.0.0.1" if _rev else None)
    cmd += _isolation_flags(task)              # Phase-1 sandbox (gVisor if present)
    egress = _egress_flags(task)
    cmd += egress                              # protect the HOST's home internet
    # SECURITY (co-tenant isolation): a networked template used to run on Docker's default bridge,
    # where every other tenant's container on this host — and the host gateway itself — is
    # reachable (buyer B could curl buyer A's Ollama on 172.17.0.x, or nc the host SSH on the
    # gateway). Put each networked job on its OWN user-defined bridge instead, so two rentals on
    # one host cannot see each other. `none`/`host` (batch/cluster) keep their explicit posture.
    net = None
    if not egress:                             # _egress_flags returned [] == the "limited"/"open" default bridge
        net, why = _ensure_job_network(tid)
        if not net:
            # FAIL CLOSED. Omitting --network here does not mean "no network", it means Docker's
            # SHARED DEFAULT BRIDGE — exactly the co-tenant/host-gateway exposure the block above
            # exists to remove, silently restored whenever `docker network create` happens to
            # fail. Adding `--network none` instead would hand the buyer a serving template that
            # can never pull its model or answer the tunnel, so refuse the rental outright and let
            # the server's failure path refund it.
            report_log(tid, f"template refused: could not create the per-job network pb-net-t{tid} "
                            f"({why}); running on the shared default bridge would expose this "
                            "rental to co-tenant containers and the host gateway")
            _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                       "status": "failed"})
            _cleanup_job_resources(tid)        # drop the labelled volume/network we may have made
            _set_ui(status="idle", task=None, fail=True)
            return
        cmd += ["--network", net]
        cmd += _dns_flags()                    # pin the resolver so the host can't DNS-lie (P-3)
    if task.get("gpu"):
        cmd += [*gpu_runtime.docker_gpu_args()]
    if task.get("cache"):
        # SECURITY (CRITICAL — cross-tenant data): the cache/work dir is a PER-TASK named volume,
        # never a shared pb-cache-<template>. A shared volume handed buyer B a fresh rental mounted
        # on buyer A's Jupyter work dir, HF token cache and game saves. A per-task name means each
        # rental starts empty and is GC'd with the container. (Cost: a model re-download per rental;
        # correctness and tenant privacy win. A future read-only shared model cache can be added
        # behind an explicit registry flag.)
        vol = _task_volume(task)
        try:                                            # create it labelled so teardown can find it
            subprocess.run(["docker", "volume", "create", "--label", f"pb.task={tid}", vol],
                           capture_output=True, timeout=30)
        except Exception:                                # noqa: BLE001 — -v auto-creates it anyway
            pass
        cmd += ["-v", f"{vol}:{task['cache']}"]
    model = params.get("model")
    _template_env = dict(task.get("env") or {})
    if task.get("model_env") and model:
        _template_env[task["model_env"]] = model
    try:
        try:
            cmd += _template_env_flags(task, _template_env)
            cmd += [image]
            # a model delivered as a CLI arg (vllm --model, TGI --model-id, llama.cpp -hf)
            if task.get("model_arg") and model:
                cmd += [task["model_arg"], model]
            cmd += list(task.get("args") or [])        # extra image args / batch command
            run = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if run.returncode:                         # keep Docker's stderr: it says WHY
                raise subprocess.CalledProcessError(run.returncode, cmd, run.stdout, run.stderr)
            cid = run.stdout.strip()
        finally:
            _remove_env_file(task)
        _register_vm(tid, name)  # watchdog: detect if this container dies
        if task.get("model_env") == "OLLAMA_MODEL" and model:
            _start_ollama_pull(tid, name, model)       # the image never reads OLLAMA_MODEL
        if task.get("health") and host_port:
            # A registered tunnel is not a working app: vLLM/llama.cpp download and load the model
            # AFTER this point, and a model too big for the GPU never serves. Tell the server the app
            # is loading; _await_ready reports 'ready' once the health endpoint answers. Billing and
            # the buyer's "Open" link wait for that.
            _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                       "status": "loading"})
            _start_ready_poll(tid, name, host_port, task["health"])
        # Colab-style /run: if a notebook URL was passed, fetch it INTO the running container's
        # work dir so it opens ready-to-run. Best-effort and image-agnostic (a plain `docker exec`
        # after start — never overrides the image's startup, so a fetch failure can't break the
        # runtime; the user still gets a working Jupyter, just without the file preloaded).
        nb_url = params.get("notebook_url")
        if nb_url and task.get("template") in {"jupyter", "pytorch", "tensorflow"}:
            try:
                _prefetch_notebook(name, task.get("cache") or "/home/jovyan/work", nb_url)
            except Exception as _e:  # noqa: BLE001 — prefetch is best-effort
                report_log(tid, "notebook prefetch skipped: download or container write rejected")
        if task.get("template") == "finetune" and not _wait_for_template_port(host_port, name):
            report_log(tid, "Axolotl JupyterLab did not open its service port; failing the rental")
            try:
                _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                           "status": "failed"})
                _post_result_ack_retry(_signed_result(tid, status="failed",
                                                      result="Axolotl JupyterLab did not start"))
            finally:
                _cleanup_job_resources(tid, name)
                _set_ui(status="idle", task=None, fail=True)
            return
        report_progress(tid, 100, "running")
        _hp = host_port or port
        _node_ip = None
        if _rev:
            _rp = _open_reverse_tunnel(host_port, tid)
            if not _rp:
                # Reverse tunnel is REQUIRED here — fail the rental rather than fall back to a
                # public bind that would leak the seller's IP / port.
                _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                           "status": "failed"})
                _cleanup_job_resources(tid, name)
                _set_ui(status="idle", task=None, fail=True)
                return
            _hp = _rp                                  # the gateway-side loopback port
            _node_ip = "127.0.0.1"                     # gateway reaches the VM at ITS OWN loopback:rp
        _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": cid[:12],
                                   "port": _hp, "connection_string": f"http://<node-ip>:{_hp}",
                                   "status": "running"})
        # P1-7: THE missing hop. Publishing the port isn't enough — the control plane must be told
        # which node:port hosts this rental, or the VMRoute stays 'starting' forever (then gets
        # auto-cancelled + refunded) and the buyer's address never resolves. Register the tunnel
        # (best-effort SSH-key inject first; a no-op for templates without sshd).
        vm_id = task.get("vm_id")
        if vm_id and port:
            _inject_ssh_key(name, task.get("ssh_pubkey"))
            _registered = _register_vm_tunnel(vm_id, _hp, ip_address=_node_ip)
            if _registered:
                report_log(tid, f"tunnel registered: vm {vm_id} -> {_node_ip or 'node'}:{_hp}")
            else:
                report_log(tid, f"tunnel registration failed for vm {vm_id}; VM may stay 'starting'"
                                + (" (retrying in the background)" if _rev else ""))
            if _rev:                                   # watch the ssh -R; re-open it if it drops
                _supervise_tunnel(tid, name, host_port, vm_id, rp=_hp, registered=_registered)
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                              # noqa: BLE001
        _why = _launch_failure_reason(e)
        report_log(tid, f"container launch failed: {_why}")
        _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                   "status": "failed"})
        # Mark the TASK failed too (not just the VM): a docker-run failure (e.g. a port collision)
        # used to leave _run_template returning normally, so the loop logged the job "completed"
        # while the buyer's VM showed failed. Post a failed result so the two agree.
        try:
            _post_result_ack_retry(_signed_result(tid, status="failed",
                                                  result=f"container launch failed: {_why}"))
        except Exception:                               # noqa: BLE001
            pass
        # A failed launch never reaches _register_vm, so the watchdog will never GC the labelled
        # volume/network created above. Reap them here or a re-delivered task id remounts them.
        _cleanup_job_resources(tid, name)
        _set_ui(status="idle", task=None, fail=True)


def _measure_fp16_tflops(n=None):
    """Achieved FP16 dense matmul throughput (TFLOPS) on THIS GPU, server-comparable.

    This is the gamer-style authenticity number: a hardware-invariant a genuine card
    can reach and a weaker card physically cannot. The server compares it to the
    published spec of the CLAIMED gpu_model (gpu_benchmark.classify) to catch a listing
    that over-claims its silicon. Returns None if torch/CUDA is unavailable (older
    agents just omit it — the server then records the number without a verdict).

    `n` is the matmul dimension the SERVER dispatched for this benchmark (GPU-bound
    challenge): the node no longer picks its own size, so it can't choose a trivially-small
    problem. We also report the problem size and the self-timed seconds so the server can
    recompute the throughput itself (and upper-bound it by its own dispatch->result clock)
    rather than trust a bare number. Returns (tflops, n, seconds).

    Runs in a SHORT-LIVED child process: in-process, torch's CUDA context (~1.5 GB with the cuBLAS
    workspace) stayed resident for the agent's whole life — VRAM a buyer pays for, and a live
    compute client that makes `nvidia-smi --gpu-reset` (the preferred VRAM wipe) impossible.
    (Reported by a seller from nvidia-smi, 2026-09-23.) The context dies with the child."""
    import subprocess, sys
    try:
        n = int(n or os.getenv("BENCH_MATMUL_N", "8192"))
        iters = int(os.getenv("BENCH_MATMUL_ITERS", "30"))
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- fixed Python source, integer-only argv, no shell.
        p = subprocess.run([sys.executable, "-c", _FP16_BENCH_PY, str(n), str(iters)],
                           capture_output=True, text=True, timeout=600)
        dt = float((p.stdout or "").strip().splitlines()[-1]) if p.returncode == 0 else 0.0
        if dt <= 0:
            return None
        flops = 2.0 * (n ** 3) * iters          # 2*N^3 per matmul
        return (round(flops / dt / 1e12, 1), n, round(dt / iters, 6))   # (TFLOPS, N, s/matmul)
    except Exception:                            # noqa: BLE001 — never crash the agent
        return None


# Timed FP16 GEMM; prints the elapsed seconds for `iters` matmuls (0 when there is no CUDA).
_FP16_BENCH_PY = (
    "import sys, time, torch\n"
    "if not torch.cuda.is_available():\n"
    "    print(0); sys.exit(0)\n"
    "n, iters = int(sys.argv[1]), int(sys.argv[2])\n"
    "a = torch.randn(n, n, device='cuda', dtype=torch.float16)\n"
    "b = torch.randn(n, n, device='cuda', dtype=torch.float16)\n"
    "for _ in range(3):\n"
    "    c = a @ b\n"
    "torch.cuda.synchronize(); t0 = time.time()\n"
    "for _ in range(iters):\n"
    "    c = a @ b\n"
    "torch.cuda.synchronize(); print(time.time() - t0)\n")


def _measure_blender_score():
    """Blender Open Data score (OptiX, sum of standard-scene samples/min) via the OFFICIAL
    benchmark-launcher-cli when it's installed on the node.

    This is the workload-relevant authenticity number: Petabyte renders Blender, and
    opendata.blender.org publishes a per-GPU public median the server compares against
    (gpu_benchmark 'blender_optix'). Returns None if the CLI isn't present (the server then
    just skips the Blender check) — never crashes the agent."""
    import shutil, subprocess, json as _json
    cli = os.getenv("BLENDER_BENCH_CLI") or shutil.which("benchmark-launcher-cli")
    if not cli:
        return None
    try:
        scenes = [s for s in os.getenv("BLENDER_BENCH_SCENES", "classroom").split(",") if s]
        out = subprocess.run([cli, "benchmark", *scenes, "--device-type", "OPTIX",  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- cli/scenes are the node's own env config, not remote/buyer input; fixed argv (no shell)
                              "--json"], capture_output=True, text=True, timeout=1800)
        rows = _json.loads(out.stdout or "[]")
        total = 0.0
        for row in rows:                             # sum the scene medians -> Open Data score
            spm = (row.get("stats") or {}).get("samples_per_minute")
            if spm:
                total += float(spm)
        return round(total, 1) if total > 0 else None
    except Exception:                                # noqa: BLE001 — never crash the agent
        return None


def _run_benchmark(task):
    """Measure LLM tokens/sec + FP16 matmul TFLOPS + Blender Open Data, submit a SIGNED result.
    Every measured score goes INSIDE the signed proof so the server checks the attributable
    (non-repudiable) number, not a bare unsigned meta field."""
    tid = task["task_id"]
    _set_ui(status="running", task=f"Benchmark #{tid}")
    spec_id = int(os.getenv("PETABYTE_SPEC_ID"))
    report_progress(tid, 40, "benchmarking")
    # tokens/sec: a real node runs a fixed prompt through a local model and counts
    # generated tokens / wall-time. Hook your LLM harness here (env stub for now).
    tokens_sec = float(os.getenv("BENCH_TOKENS_SEC", "0"))
    # FP16 matmul TFLOPS on the SERVER-DISPATCHED problem size (GPU-bound challenge): the server
    # picks `gemm_n`, the node can't choose a trivially-small matmul, and we report the size + the
    # self-timed per-matmul seconds so the server recomputes the throughput and floor-checks it.
    fp16 = _measure_fp16_tflops(n=task.get("gemm_n"))
    tflops = gemm_n = gemm_s = None
    if fp16 is not None:
        tflops, gemm_n, gemm_s = fp16
    report_progress(tid, 70, f"fp16 matmul: {tflops} TFLOPS" if tflops else "benchmarking")
    # Blender Open Data: workload-relevant, public per-GPU medians (advisory signal).
    blender = _measure_blender_score()
    report_progress(tid, 90, f"blender: {blender}" if blender else "benchmarking")

    metrics = {}
    if tflops is not None:
        metrics["tflops_fp16"] = tflops
    if blender is not None:
        metrics["blender_optix"] = blender
    meta = {"harness": ",".join(metrics) or "stub", "metrics": list(metrics)}
    # scores live in the SIGNED proof (attributable); meta is freeform display. gemm_n/gemm_seconds
    # are signed too so the server can recompute & floor-check the throughput (not trust the number).
    proof = {"task_id": tid, "output_hash": "benchmark", "ts": int(_t.time()), **metrics}
    if tflops is not None:
        proof["gemm_n"], proof["gemm_seconds"] = gemm_n, gemm_s
    # Answer the server's FRESH proof-of-work challenge (proves this benchmark is a real,
    # current computation on this node — not a pre-canned or replayed number).
    _seed, _size = task.get("bench_seed"), task.get("bench_size")
    if _seed is not None and _size is not None:
        try:
            proof["challenge_hash"] = crypto.compute_test_hash(int(_size), int(_seed))
        except Exception:                            # noqa: BLE001 — never crash the agent
            pass
    import execution_receipt, hardware_evidence
    proof.update(execution_receipt.make(tid, result=None, output_hash=proof.get("output_hash")))
    proof["hardware_evidence"] = hardware_evidence.collect()
    httpx.post(f"{API_URL}/jobs/benchmark_result", headers=HEADERS, timeout=20, json={
        "spec_id": spec_id, "tokens_sec": tokens_sec,
        "meta": meta, "proof": proof, "signature": crypto.sign_proof(proof)}, trust_env=False)
    _set_ui(status="idle", task=None, ok=True)


def _run_docker(argv, timeout=None):
    """Run a `docker run --rm ...` command with a unique --name, and if the CLIENT times out,
    force-remove the daemon-owned container. Killing the local docker client does NOT stop the
    container (--rm only fires when the container itself exits), so without this a timed-out
    job would keep burning the seller's GPU/CPU until it finished on its own."""
    import subprocess as _sp
    import uuid as _uuid
    name = None
    if list(argv[:3]) == ["docker", "run", "--rm"]:
        name = "pb-task-" + _uuid.uuid4().hex[:12]
        argv = list(argv[:3]) + ["--name", name] + list(argv[3:])
    try:
        return _sp.run(argv, check=True, timeout=timeout)
    except _sp.TimeoutExpired:
        if name:
            try:
                _sp.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            except Exception:
                pass
        raise


CONTAINER_OUTPUT_CAP = 256 * 1024   # bytes of stdout/stderr returned to the buyer as the result
CONTAINER_DRAIN_CAP = 512 * 1024 * 1024   # total streamed bytes before the container is judged hostile


def _force_rm_container(name):
    """Best-effort kill of a daemon-owned container by name — killing the local docker CLIENT
    process never stops the container itself."""
    import subprocess as _sp
    try:
        _sp.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
    except Exception:                                   # noqa: BLE001
        pass


def _run_streamed(argv, name, timeout_s=None, keep_cap=None, drain_cap=None):
    """Run an UNTRUSTED container's argv, streaming its merged stdout/stderr under a hard
    memory bound.

    `capture_output=True` would buffer the container's ENTIRE output inside this process —
    a hostile image printing forever OOMs the seller host, because the container's --memory
    cap does not bound the agent draining its pipes. Instead: read chunkwise, KEEP only the
    first `keep_cap` bytes (the signed result payload), and kill the container outright once
    `drain_cap` total bytes have streamed — past that point the output is a flood, not a
    verbose job. `timeout_s` is enforced by a watchdog that removes the container by name
    (which also EOFs the pipe). Returns (returncode, text, truncated, timed_out, overflowed)."""
    import subprocess as _sp
    import threading
    keep_cap = CONTAINER_OUTPUT_CAP if keep_cap is None else keep_cap
    drain_cap = CONTAINER_DRAIN_CAP if drain_cap is None else drain_cap
    p = _sp.Popen(argv, stdout=_sp.PIPE, stderr=_sp.STDOUT)
    state = {"timed_out": False}

    def _watchdog():
        state["timed_out"] = True
        _force_rm_container(name)
        try:
            p.kill()
        except Exception:                               # noqa: BLE001
            pass

    timer = threading.Timer(int(timeout_s), _watchdog) if timeout_s else None
    if timer:
        timer.daemon = True
        timer.start()
    kept = bytearray()
    drained = 0
    truncated = overflowed = False
    try:
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            drained += len(chunk)
            if len(kept) < keep_cap:
                take = min(len(chunk), keep_cap - len(kept))
                kept += chunk[:take]
                if take < len(chunk):
                    truncated = True
            else:
                truncated = True
            if drained > drain_cap:
                overflowed = True
                _force_rm_container(name)
                try:
                    p.kill()
                except Exception:                       # noqa: BLE001
                    pass
                break
        try:
            rc = p.wait(timeout=60)
        except _sp.TimeoutExpired:
            p.kill()
            rc = p.wait(timeout=10)
    finally:
        if timer:
            timer.cancel()
        try:
            p.stdout.close()
        except Exception:                               # noqa: BLE001
            pass
    return rc, kept.decode("utf-8", "replace"), truncated, state["timed_out"], overflowed


def build_container_cmd(task, name=None):
    """Build a single-node buyer container argv and its temporary environment file.

    An arbitrary buyer image is UNTRUSTED, so it runs under exactly the seller-protection profile
    the render/transcode jobs use:
      * _egress_flags  -> default `--network none` (batch job gets no network unless the platform
                          explicitly widened it); never the host network on this single-node path.
      * _isolation_flags -> --cap-drop ALL, --security-opt no-new-privileges, --pids-limit,
                          --memory/--cpus caps, gVisor (runsc) when present, and the operator-gated
                          --read-only / --user (non-root) profile.
      * env  -> a 0600 `--env-file` (NOT `-e` on the argv, which leaks secrets to `ps`/`/proc`).
    The buyer `command` is shell-split into argv (list form — no shell, so no injection)."""
    argv = ["docker", "run", "--rm"]
    if name:
        argv += ["--name", name]
    argv += ["--network", "none"]
    argv += _isolation_flags(task)
    env = task.get("env") or {}
    if task.get("gpu"):
        argv += [*gpu_runtime.docker_gpu_args()]
    # Platform Python batch jobs override an image's service/audit entrypoint.
    if task.get("entrypoint"):
        if task["entrypoint"] != "python3":
            raise ValueError("unsupported batch entrypoint")
        argv += ["--entrypoint", "python3"]
    argv += [task.get("image")]
    command = task.get("command")
    if command:
        import shlex
        argv += shlex.split(command)
    argv[3:3] = _template_env_flags(task, env)
    return argv


def _run_container(task):
    """Run a single-node arbitrary buyer image+command and return its (bounded) output as the
    signed result. Hard-killed at the AUTHORIZED runtime budget (audit H1) so it can't burn more
    of the seller's GPU than the buyer paid to authorize."""
    tid = task["task_id"]
    _set_ui(status="running", task=f"Container #{tid}")
    import shutil, uuid as _uuid
    if not shutil.which("docker"):
        report_log(tid, "docker not installed; cannot run container sandbox")
        _post("/jobs/result", _signed_result(tid, status="failed", failure_cause="node_error"))
        _set_ui(status="idle", task=None, fail=True)
        return
    if not task.get("image"):
        report_log(tid, "no image specified for container job")
        _post("/jobs/result", _signed_result(tid, status="failed", failure_cause="bad_task"))
        _set_ui(status="idle", task=None, fail=True)
        return
    name = "pb-task-" + _uuid.uuid4().hex[:12]
    _rt = task.get("max_runtime_s")
    try:
        argv = build_container_cmd(task, name=name)
        report_progress(tid, 10, f"pulling & running {task.get('image')}")
        # Streamed + bounded: never buffers the untrusted image's output unbounded in this
        # process, and kills the daemon-owned container on runtime or output-flood breach.
        rc, out, _trunc, timed_out, overflowed = _run_streamed(
            argv, name, timeout_s=(int(_rt) if _rt else None))
        _ef = task.pop("_env_file", None)        # docker consumed it at launch; remove the 0600 secret file
        if _ef:
            try:
                os.remove(_ef)
            except OSError:
                pass
        if timed_out:
            report_log(tid, "container exceeded authorized runtime; killed")
            _post("/jobs/result", _signed_result(tid, status="failed", result="timeout",
                                                 failure_cause="timeout"))
            _set_ui(status="idle", task=None, fail=True)
            return
        if overflowed:
            report_log(tid, "container output flood exceeded the drain budget; killed")
            _post("/jobs/result", _signed_result(tid, status="failed", result=_to_str(out),
                                                 failure_cause="output_flood"))
            _set_ui(status="idle", task=None, fail=True)
            return
        status = "completed" if rc == 0 else "failed"
        # rc != 0 is the BUYER's own program exiting non-zero — advisory cause "container_exit"
        # (the node ran the job fine). It never exempts the seller; it's for the buyer's logs.
        _post("/jobs/result", _signed_result(tid, status=status, result=_to_str(out),
                                             content_hash=crypto.sha256_hex(out),
                                             failure_cause=(None if rc == 0 else "container_exit")))
        _set_ui(status="idle", task=None, ok=(status == "completed"),
                fail=(status != "completed"))
    except Exception as e:                              # noqa: BLE001
        report_log(tid, f"container failed: {e}")
        _post("/jobs/result", _signed_result(tid, status="failed", failure_cause="node_error"))
        _set_ui(status="idle", task=None, fail=True)
    finally:
        _remove_env_file(task)


def _render_setup_expr(samples=None, gpu=True, engine=None):
    """Blender `--python-expr`, run after the buyer's .blend loads and before `-a` renders it.

    0) `engine` (from _render_plan, only ever 'CYCLES') switches the scene's engine first — a
       Blender Internal file, or an EEVEE file the buyer asked to convert.
    1) Cycles defaults to the CPU and a headless container has no saved Blender preferences, so
       every "GPU render" used to run on the seller's CPU (2026-09-25: no Cycles device was ever
       chosen). Point Cycles at the GPU the container was given: OptiX, else CUDA/HIP/oneAPI.
       get_devices_for_type() also returns the CPU row, and a new device entry defaults to
       use=True, so enabling every row rendered hybrid CPU+GPU (#556) — enable only the GPUs.
    2) Apply the buyer's requested sample count (POST /render `samples`, previously ignored).
    3) A movie output format is rendered as PNG frames, so a range split across nodes can be
       stitched back together (a node can't append to another node's video).
    It comes from OUR argv, not the scene: --disable-autoexec still blocks the .blend's own scripts.
    """
    n = int(samples) if samples else 0
    return (
        "import bpy\n"
        "s = bpy.context.scene\n"
        + (f"s.render.engine = '{engine}'\n" if engine else "")
        + "if s.render.is_movie_format:\n"
        "    s.render.image_settings.file_format = 'PNG'\n"
        "if s.render.engine == 'CYCLES':\n"
        f"    if {n} > 0:\n"
        f"        s.cycles.samples = {n}\n"
        f"    if {bool(gpu)}:\n"
        "        p = bpy.context.preferences.addons['cycles'].preferences\n"
        "        for dt in ('OPTIX', 'CUDA', 'HIP', 'ONEAPI'):\n"
        "            try:\n"
        "                p.compute_device_type = dt\n"
        "                devs = p.get_devices_for_type(dt)\n"
        "            except Exception:\n"
        "                continue\n"
        "            if any(d.type != 'CPU' for d in devs):\n"
        "                for d in devs:\n"
        "                    d.use = d.type != 'CPU'\n"
        "                s.cycles.device = 'GPU'\n"
        "                print('PBDEVICE=' + dt, flush=True)\n"
        "                break\n"
        "        else:\n"
        "            print('PBDEVICE=CPU', flush=True)\n"
    )


def _render_setup_expr_279(engine=None):
    """Blender 2.79b `--python-expr` — Python 3.5, so no f-strings in the generated code. Keeps the
    file's own engine (Blender Internal) unless the buyer named one, writes PNG frames (stitchable
    across nodes), and uses every core the container's --cpus cap allows (a file saved with FIXED
    threads would otherwise render on the author's thread count)."""
    return ("import bpy\n"
            "s = bpy.context.scene\n"
            + (f"s.render.engine = '{engine}'\n" if engine else "")
            + "s.render.image_settings.file_format = 'PNG'\n"
            "s.render.threads_mode = 'AUTO'\n")


# Blender 2.79b: the last release with Blender Internal, the only faithful renderer for a pre-2.8
# scene. Pinned to the hash download.blender.org publishes in release279b.sha256 (checked
# 2026-09-26: the downloaded tarball matched it, and its md5 matched release279b.md5).
BLENDER_279_URL = ("https://download.blender.org/release/Blender2.79/"
                   "blender-2.79b-linux-glibc219-x86_64.tar.bz2")
BLENDER_279_SHA256 = "43824a4e0b0c6de6fa34ff224eec44c1cc9f26a95f6f3c8c2558d1c05704183c"
BLENDER_279_CACHE = "/var/lib/petabyte-agent/blender/2.79b"
_BLENDER_279_TOP = "blender-2.79b-linux-glibc219-x86_64"      # the tarball's top-level directory


def _ensure_blender_279():
    """Host dir holding Blender 2.79b. The render container runs --network none, so the binary
    comes from the HOST: fetched ONCE from download.blender.org, refused unless its sha256 matches
    the pinned published hash, extracted into the agent cache, then bind-mounted READ-ONLY into
    the render container. Concurrent first renders each download into their own temp dir; the
    atomic rename means only a complete, verified tree is ever at the final path."""
    import shutil
    import tarfile
    import tempfile
    final = os.path.join(BLENDER_279_CACHE, _BLENDER_279_TOP)
    if os.path.isfile(os.path.join(final, "blender")):
        return final
    os.makedirs(BLENDER_279_CACHE, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=BLENDER_279_CACHE)
    try:
        tarball, h = os.path.join(tmp, "blender.tar.bz2"), hashlib.sha256()
        with httpx.stream("GET", BLENDER_279_URL, timeout=600, trust_env=False) as r, \
                open(tarball, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != BLENDER_279_SHA256:
            raise RuntimeError(f"Blender 2.79b download sha256 {h.hexdigest()} != pinned "
                               f"{BLENDER_279_SHA256}; refusing to run it")
        with tarfile.open(tarball, "r:bz2") as t:
            t.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        try:
            os.rename(os.path.join(tmp, _BLENDER_279_TOP), final)
        except OSError:
            if not os.path.isfile(os.path.join(final, "blender")):   # not a lost race: real error
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return final


def _blend_file_version(path):
    """Blender version a .blend was saved with, from its 12-byte header: b'BLENDER' + pointer size
    ('_'/'-') + endianness ('v'/'V') + 3 digits, e.g. b'BLENDER-v275' -> 275. A gzip-compressed
    .blend (2.7x "Compress File") is read through gzip. None if unreadable or another header —
    zstd-compressed files (3.0+) and the 5.x header are both 2.8+ anyway."""
    import gzip
    try:
        with open(path, "rb") as f:
            gz = f.read(2) == b"\x1f\x8b"
        with (gzip.open if gz else open)(path, "rb") as f:
            h = f.read(12)
    except Exception:                                    # noqa: BLE001 — unknown = not legacy
        return None
    return int(h[9:12]) if h[:7] == b"BLENDER" and h[9:12].isdigit() else None


def _render_plan(file_version, engine="auto", blender_version="latest", detect=lambda: ""):
    """How to render a scene -> (runner, set_engine, note, refuse_cause).

    runner: "2.79" (host Blender 2.79b, CPU) or "latest" (the render image, GPU); set_engine: the
    engine to switch the scene to, or None to keep the file's own; note: the line for the buyer's
    log; refuse_cause: a failure_cause when it must not render. `detect()` returns the engine
    modern Blender sees; it is called only when the answer changes the plan.

      file          blender_version / engine        -> result
      pre-2.8       2.79, or engine=BLENDER_RENDER  -> 2.79b on CPU, as authored
      pre-2.8       latest + auto/CYCLES            -> Cycles on GPU + note (look may differ)
      2.8+/unknown  2.79, or engine=BLENDER_RENDER  -> refuse (2.79 can't open newer files)
      2.8+/unknown  engine=CYCLES                   -> convert to Cycles on GPU
      2.8+/unknown  auto, file engine EEVEE         -> refuse (no headless GPU EEVEE)
      2.8+/unknown  auto, anything else             -> render as is
    """
    engine = engine if engine in ("auto", "CYCLES", "BLENDER_RENDER") else "auto"
    legacy = file_version is not None and file_version < 280
    saved = f"Blender {file_version // 100}.{file_version % 100}" if file_version else "a newer Blender"
    if blender_version in ("2.79", "2.79b") or engine == "BLENDER_RENDER":
        if not legacy:
            msg = (f"This .blend was saved in {saved}; Blender 2.79 can only render files saved "
                   "in 2.79 or earlier. Your .blend was NOT changed — submit again without "
                   "blender_version=2.79.")
            return (None, None, msg, "unsupported_blender_version")
        return ("2.79", None if engine == "auto" else engine, None, None)
    if legacy:
        msg = (f"This .blend was saved in {saved} (Blender Internal era); rendering it in Cycles "
               "on the GPU, so materials and lighting may look different from the original. "
               "Submit with blender_version=2.79 to render it exactly as authored (Blender 2.79b, "
               "CPU).")
        return ("latest", "CYCLES", msg, None)
    if engine == "CYCLES":
        return ("latest", "CYCLES", None, None)
    found = detect() or ""
    if "EEVEE" in found.upper():
        msg = (f"This scene's render engine is {found}, which cannot be rendered on a headless "
               "GPU. Your .blend was NOT changed — set the scene's render engine to Cycles, or "
               "submit again with engine=CYCLES to convert it, for a fast GPU render.")
        return (None, None, msg, "unsupported_engine_eevee")
    return ("latest", None, None, None)


def _run_render(task):
    """Render an assigned frame range by launching Blender AS A CONTAINER.
    The seller never installs Blender — the image is pulled on demand and cached;
    the scene streams in and frames stream out via pre-signed URLs. No host binary."""
    tid = task["task_id"]
    fs, fe = task.get("frame_start"), task.get("frame_end")
    image = task.get("image", "linuxserver/blender:latest")
    _set_ui(status="running", task=f"Render #{tid} frames {fs}-{fe}")
    import shutil, subprocess, os as _os, tempfile, tarfile
    if not shutil.which("docker"):
        report_log(tid, "docker not installed; cannot run render sandbox")
        _post("/jobs/result", _signed_result(tid, status="failed"))
        return
    # Blender is the container's ENTRYPOINT, not a command handed to the image's own init.
    # linuxserver/blender's s6 /init boots a whole desktop (Selkies, Wayland, pulseaudio, dbus) around
    # it, and under --cap-drop ALL its shutdown can't signal those non-root services (no CAP_KILL):
    # Blender saved the frame and quit in 2s, then the container never exited (5.2.2-ls241,
    # 2026-09-26) — every probe and render hung until the runtime budget killed it as a failure.
    ep = ["--entrypoint", "blender", image]
    work = tempfile.mkdtemp(prefix=f"render-{tid}-")
    scene = _os.path.join(work, "scene.blend")
    out_dir = _os.path.join(work, "out"); _os.makedirs(out_dir, exist_ok=True)
    _os.chmod(out_dir, 0o777)   # forced non-root container writes frames; the 0700 parent tmpdir
    # keeps this world-writable leaf unreachable by any other host account
    try:
        # 1) pull the scene via a pre-signed GET (no standing creds on the node)
        g = httpx.post(f"{API_URL}/jobs/input_url", headers=HEADERS, timeout=15,
                       json={"task_id": tid, "ref": task.get("blend_ref", "")}, trust_env=False).json()
        open(scene, "wb").write(safe_fetch.get(g["download_url"], timeout=120, max_bytes=128 * 1024 * 1024).content)
        report_progress(tid, 15, f"scene fetched; rendering {fs}-{fe} in {image}")

        # 1b) Detect the scene's render engine BEFORE committing GPU time. EEVEE(-Next) needs a GPU
        # DISPLAY context that does not exist in a headless container — it errors EGL_BAD_MATCH and
        # crawls in software (tens of minutes/frame). So we do NOT render EEVEE: hand the buyer's
        # file straight back with a note to switch to Cycles (which renders headless on the GPU via
        # OptiX reliably). Best-effort — on any detection error we fall through and just render.
        def _detect_engine():
            try:
                _dcmd = ["docker", "run", "--rm", "--network", "none"]
                _dcmd += _isolation_flags(task)
                _dcmd += ["-v", f"{scene}:/scene.blend:ro", *ep, "-b", "/scene.blend",
                          "--disable-autoexec", "--python-expr",
                          "import bpy;print('PBENGINE='+bpy.context.scene.render.engine)"]
                _d = subprocess.run(_dcmd, capture_output=True, text=True, timeout=120, check=False)
                for _l in (_d.stdout or "").splitlines():
                    if _l.startswith("PBENGINE="):
                        return _l.split("=", 1)[1].strip()
            except Exception:                                # noqa: BLE001 — detection is advisory
                return ""
            return ""
        # A pre-2.8 (Blender Internal) file loads in modern Blender as EEVEE, so the header version
        # decides first (2026-09-25: a Blender 2.75 scene was refused as EEVEE and could never
        # render). See _render_plan for the full table.
        runner, set_engine, note, refuse = _render_plan(
            _blend_file_version(scene), task.get("engine") or "auto",
            task.get("blender_version") or "latest", detect=_detect_engine)
        if refuse:
            report_log(tid, note)
            _post("/jobs/result", _signed_result(tid, status="failed", failure_cause=refuse,
                                                 result=note))
            _set_ui(status="idle", task=None, fail=True)
            return
        if note:
            report_log(tid, note)
        # 2) render inside the container (GPU via NVIDIA Container Toolkit).
        # --network none: batch render needs no network -> no exfil / LAN access.
        # _isolation_flags: cap-drop ALL etc. so a malicious .blend (Blender auto-runs
        # embedded Python) cannot escalate or touch the host. --disable-autoexec stops
        # the scene's embedded scripts from running at all.
        cmd = ["docker", "run", "--rm", "--network", "none"]
        cmd += _isolation_flags(task)
        cmd += ["-v", f"{scene}:/scene.blend:ro", "-v", f"{out_dir}:/out"]
        if runner == "2.79":
            # Blender Internal is CPU-only: no GPU flags. The host's verified 2.79b tree is mounted
            # read-only and run in the same image (it ships the X/GL libs 2.79 links against).
            report_progress(tid, 18, "rendering as authored with Blender 2.79b (CPU)")
            cmd += ["-v", f"{_ensure_blender_279()}:/opt/blender-2.79b:ro",
                    "--entrypoint", "/opt/blender-2.79b/blender", image,
                    "-b", "/scene.blend", "--disable-autoexec",
                    "--python-expr", _render_setup_expr_279(set_engine)]
        else:
            if task.get("gpu"):
                cmd += [*gpu_runtime.docker_gpu_args()]
            cmd += [*ep, "-b", "/scene.blend", "--disable-autoexec",
                    "--python-expr", _render_setup_expr(task.get("samples"), gpu=bool(task.get("gpu")),
                                                        engine=set_engine)]
        cmd += ["-o", "/out/frame_", "-s", str(fs), "-e", str(fe), "-a"]
        # Hard-kill the container at the buyer's AUTHORIZED runtime budget (audit H1): a render
        # can't consume more of the seller's GPU than the buyer paid to authorize (_run_docker
        # force-removes the container when the client-side timeout fires).
        _rt = task.get("max_runtime_s")
        _run_docker(cmd, timeout=(int(_rt) if _rt else None))
        report_progress(tid, 85, "uploading frames")
        # 3) tar the frames and upload as the buyer's DOWNLOADABLE output — UNENCRYPTED, under the
        # job's output prefix — so an artist downloads exactly the frames Blender rendered, via
        # /jobs/output_url (NOT the encrypted backup path, which the buyer could never open).
        bundle = _os.path.join(work, f"frames_{fs}_{fe}.tar")
        with tarfile.open(bundle, "w") as tf:
            tf.add(out_dir, arcname="frames")
        grant = httpx.post(f"{API_URL}/jobs/output_put", headers=HEADERS, timeout=15,
                           json={"task_id": tid, "filename": f"frames_{fs}_{fe}.tar"}, trust_env=False).json()
        raw = open(bundle, "rb").read()
        httpx.put(grant["upload_url"], content=raw, timeout=600, trust_env=False)   # plain bytes — the buyer's artifact
        _post("/jobs/result", _signed_result(tid, status="completed",
                                             result=grant["ref"],   # clean s3 URI -> /jobs/output_url
                                             content_hash=hashlib.sha256(raw).hexdigest()))
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                              # noqa: BLE001
        report_log(tid, f"render failed: {e}")
        _post("/jobs/result", _signed_result(tid, status="failed"))
        _set_ui(status="idle", task=None, fail=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run_transcode(task):
    """Transcode an assigned time segment with FFmpeg IN A CONTAINER (NVENC if GPU).
    Seller installs nothing; the image is pulled on demand. Input/output via
    pre-signed URLs."""
    tid = task["task_id"]
    ss, se = task.get("start_time"), task.get("end_time")
    image = task.get("image", "jrottenberg/ffmpeg:6.1-nvidia")
    _set_ui(status="running", task=f"Transcode #{tid} seg {ss}-{se}")
    import shutil, subprocess, os as _os, tempfile
    if not shutil.which("docker"):
        _post("/jobs/result", _signed_result(tid, status="failed")); return
    outer = tempfile.mkdtemp(prefix=f"tc-{tid}-")   # 0700: only the agent user can traverse it
    work = _os.path.join(outer, "work"); _os.makedirs(work)
    # The forced non-root container user (AGENT_CONTAINER_USER) must write /work, so the
    # leaf is 0777 — but it is reachable only through the 0700 parent, so no other host
    # account can read buyer inputs or swap outputs before they are hashed + uploaded.
    _os.chmod(work, 0o777)
    src = _os.path.join(work, "in"); dst = _os.path.join(work, f"out.{_safe_ext(task.get('container'))}")
    try:
        g = httpx.post(f"{API_URL}/jobs/input_url", headers=HEADERS, timeout=15,
                       json={"task_id": tid, "ref": task.get("input_ref", "")}, trust_env=False).json()
        open(src, "wb").write(safe_fetch.get(g["download_url"], timeout=120, max_bytes=128 * 1024 * 1024).content)
        report_progress(tid, 20, "transcoding")
        vcodec = {"h264": "h264_nvenc", "h265": "hevc_nvenc", "av1": "av1_nvenc"} \
            if task.get("gpu") else {"h264": "libx264", "h265": "libx265", "av1": "libaom-av1"}
        args = ["docker", "run", "--rm", "--network", "none"]
        args += _isolation_flags(task)
        args += ["-v", f"{src}:/in:ro", "-v", f"{work}:/work"]
        if task.get("gpu"):
            args += [*gpu_runtime.docker_gpu_args()]
        ff = [image, "-y"]
        # keyframe-aware segment cut (seek before input for speed, re-encode for accuracy)
        if ss is not None and se is not None and se >= 0:
            ff += ["-ss", str(ss), "-to", str(se)]
        ff += ["-i", "/in", "-c:v", vcodec.get(task.get("codec", "h264"), "h264_nvenc")]
        if task.get("resolution"):
            ff += ["-s", task["resolution"]]
        if task.get("crf") is not None:
            ff += ["-crf", str(task["crf"])]
        elif task.get("bitrate"):
            ff += ["-b:v", task["bitrate"]]
        ff += [f"/work/{_os.path.basename(dst)}"]
        # Audit H1: hard-kill at the buyer's authorized runtime budget so a job can never
        # consume more of the seller's GPU than was paid to authorize.
        _rt = task.get("max_runtime_s")
        _run_docker(args + ff, timeout=(int(_rt) if _rt else None))
        report_progress(tid, 80, "uploading")
        grant = httpx.post(f"{API_URL}/jobs/backup_url", headers=HEADERS, timeout=15,
                           json={"task_id": tid, "filename": _os.path.basename(dst)}, trust_env=False).json()
        from cryptography.fernet import Fernet
        raw = open(dst, "rb").read()
        enc = Fernet(grant["enc_key"].encode()).encrypt(raw)
        httpx.put(grant["upload_url"], content=enc, timeout=600, trust_env=False)
        _post("/jobs/result", _signed_result(tid, status="completed", result=grant["snapshot_ref"],
                                             content_hash=hashlib.sha256(raw).hexdigest()))
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                              # noqa: BLE001
        report_log(tid, f"transcode failed: {e}")
        _post("/jobs/result", _signed_result(tid, status="failed"))
        _set_ui(status="idle", task=None, fail=True)
    finally:
        shutil.rmtree(outer, ignore_errors=True)


def _run_stitch(task):
    """Assemble a fan-out job: concat transcode segments (or collect render frames)
    into one final output, uploaded via a pre-signed PUT."""
    tid = task["task_id"]
    refs = task.get("segment_refs", [])
    image = task.get("image", "jrottenberg/ffmpeg:6.1-nvidia")
    _set_ui(status="running", task=f"Assemble #{tid} ({len(refs)} parts)")
    import shutil, subprocess, os as _os, tempfile
    if not shutil.which("docker"):
        _post("/jobs/result", _signed_result(tid, status="failed")); return
    outer = tempfile.mkdtemp(prefix=f"stitch-{tid}-")   # 0700: only the agent user can traverse it
    work = _os.path.join(outer, "work"); _os.makedirs(work)
    # The forced non-root container user (AGENT_CONTAINER_USER) must write /work, so the
    # leaf is 0777 — but it is reachable only through the 0700 parent, so no other host
    # account can read buyer inputs or swap outputs before they are hashed + uploaded.
    _os.chmod(work, 0o777)
    try:
        # pull each segment via a restore-style GET, concat with ffmpeg
        listfile = _os.path.join(work, "list.txt")
        with open(listfile, "w") as lf:
            for i, ref in enumerate(refs):
                gg = httpx.post(f"{API_URL}/jobs/input_url", headers=HEADERS, timeout=15,
                                json={"task_id": tid, "ref": ref}, trust_env=False).json()
                p = _os.path.join(work, f"seg{i}.{_safe_ext(task.get('container'))}")
                open(p, "wb").write(safe_fetch.get(gg["download_url"], timeout=120, max_bytes=128 * 1024 * 1024).content)
                lf.write(f"file '{p}'\n")
        out = _os.path.join(work, f"final.{_safe_ext(task.get('container'))}")
        if task.get("kind") == "transcode":
            # The agent already fetched every segment to /work, so the concat container
            # needs NO network — close it (previously stitch ran with default egress,
            # exposing ffmpeg-protocol SSRF/exfil on buyer-supplied inputs) and drop caps.
            concat = ["docker", "run", "--rm", "--network", "none"]
            concat += _isolation_flags(task)
            concat += ["-v", f"{work}:/work", image, "-y",
                       "-f", "concat", "-safe", "0", "-i", "/work/list.txt", "-c", "copy",
                       f"/work/{_os.path.basename(out)}"]
            # Audit H1: bound concat to the authorized runtime budget.
            _rt = task.get("max_runtime_s")
            _run_docker(concat, timeout=(int(_rt) if _rt else None))
        else:   # render: tar the collected frames
            import tarfile
            with tarfile.open(out, "w") as tf:
                tf.add(work, arcname="frames")
        grant = httpx.post(f"{API_URL}/jobs/backup_url", headers=HEADERS, timeout=15,
                           json={"task_id": tid, "filename": _os.path.basename(out)}, trust_env=False).json()
        from cryptography.fernet import Fernet
        raw = open(out, "rb").read()
        enc = Fernet(grant["enc_key"].encode()).encrypt(raw)
        httpx.put(grant["upload_url"], content=enc, timeout=600, trust_env=False)
        _post("/jobs/result", _signed_result(tid, status="completed", result=grant["snapshot_ref"],
                                             content_hash=hashlib.sha256(raw).hexdigest()))
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                              # noqa: BLE001
        report_log(tid, f"assemble failed: {e}")
        _post("/jobs/result", _signed_result(tid, status="failed"))
    finally:
        shutil.rmtree(outer, ignore_errors=True)


def _run_distributed(task):
    """No untrusted distributed execution until the peer-only overlay is implemented."""
    tid = task["task_id"]
    report_log(tid, "distributed execution unavailable: isolated peer-only overlay required")
    _post("/jobs/result", _signed_result(tid, status="failed", failure_cause="network_policy"))
    _set_ui(status="idle", task=None, fail=True)


def _signed_result(tid, status="completed", result=None, content_hash=None, failure_cause=None):
    import execution_receipt
    proof = execution_receipt.make(tid, status=status, result=result, content_hash=content_hash)
    body = {"task_id": tid, "status": status, "result": result,
            "proof": proof, "signature": crypto.sign_proof(proof)}
    if failure_cause:
        body["failure_cause"] = str(failure_cause)[:64]
    return body



# ---- Container liveness watchdog (fixes: a dead job container was never noticed) ----
# The agent launches a job container with `docker run -d` and then returns to polling.
# If that container later crashes/OOMs/exits, nothing used to notice: the task stayed
# 'running' forever, the seller's unit stayed consumed, and the buyer's escrow stayed
# held. This watchdog tracks every launched job container and, the moment one exits,
# POSTs a SIGNED terminal /jobs/result. That triggers the server's already-correct
# failure path (note_job_failed / free reservation / void hold; settle on exit 0).
import threading as _pb_thr
_pb_vm_watch = {}                 # task_id -> {"name": str, "reported": bool}
_pb_vm_lock = _pb_thr.Lock()
_pb_vm_started = {"on": False}


def _register_vm(task_id, name):
    """Track a launched job container so the watchdog can report its exit."""
    with _pb_vm_lock:
        _pb_vm_watch[task_id] = {"name": name, "reported": False}
        if not _pb_vm_started["on"]:
            _pb_vm_started["on"] = True
            _pb_thr.Thread(target=_pb_vm_watchdog, name="pb-vm-watchdog", daemon=True).start()


def _pb_vm_scan():
    """One sweep: report any tracked container that has exited (once)."""
    import subprocess as _sp
    with _pb_vm_lock:
        items = [(tid, d["name"], d.get("fail")) for tid, d in _pb_vm_watch.items() if not d["reported"]]
    for tid, name, fail in items:
        if fail:                                # the agent gave up on it (e.g. its tunnel is lost)
            status, code = fail, 1
        else:
            try:
                r = _sp.run(["docker", "inspect", "-f", "{{.State.Status}}:{{.State.ExitCode}}", name],
                            capture_output=True, text=True, timeout=10)
            except Exception:
                continue  # docker hiccup — try again next sweep
            if r.returncode != 0:
                status, code = "gone", 1            # container was removed entirely
            else:
                parts = (r.stdout.strip().split(":") + ["1"])
                status = parts[0]
                try:
                    code = int(parts[1])
                except Exception:
                    code = 1
            if status in ("running", "created", "restarting", "paused"):
                continue                            # still alive — keep watching
        final = "completed" if code == 0 else "failed"
        report_log(tid, f"watchdog: job container {name} is {status} (exit {code}) -> reporting {final}")
        if final == "failed" and status != "gone" and not fail and tid not in _pb_log_sent:
            _pb_log_sent.add(tid)             # once: an unacknowledged result is retried next sweep
            _tail = _container_log_tail(name)
            if _tail:
                report_log(tid, "container log (last lines):\n" + _tail)
        # Mark 'reported' ONLY on an acknowledged 2xx — a swallowed transport error or a non-2xx
        # (5xx/timeout/401/409) must keep the task in the watch set so the next sweep retries,
        # rather than silently stranding it 'running' with the reservation + card hold held.
        if not _post_result_ack(_signed_result(tid, status=final, result=f"container_{status}_exit_{code}",
                                               failure_cause=fail)):   # e.g. tunnel_lost
            logging.error(f"watchdog: /jobs/result NOT acknowledged for task {tid}; retry next sweep")
            continue                            # not acknowledged — keep watching
        logging.warning(f"watchdog reported task {tid} {final} (container {status}, exit {code})")
        # SECURITY (tenant isolation): the rental is over — GC this task's container, its per-task
        # volume (so the next tenant can never mount its data) and its per-task network. Scoped by
        # the pb.task=<tid> label, so only this rental's resources are removed.
        _cleanup_job_resources(tid, name)
        with _pb_vm_lock:
            # DROP it, don't just flag it: the result is acknowledged and the container, volume and
            # network are gone, so there is nothing left to watch. Marking it kept one entry per
            # task for the life of the agent, which on a busy node grows without bound.
            _pb_vm_watch.pop(tid, None)
        import execution_receipt
        execution_receipt.forget(tid)


def _pb_vm_watchdog():
    interval = int(os.getenv("PB_WATCHDOG_INTERVAL_S", "15"))
    logging.info(f"container watchdog started (interval={interval}s)")
    while True:
        try:
            _pb_vm_scan()
        except Exception as e:                  # noqa: BLE001
            logging.error(f"watchdog loop error: {e}")
        time.sleep(interval)


def _restore_vm_watch():
    """Reattach the watchdog to labelled rentals whose assignment survived an agent restart, and
    re-open their reverse tunnels: the ssh -R dies with the agent (every auto-update), which used to
    leave every live rental billed but unreachable."""
    import execution_receipt
    import re
    import subprocess
    try:
        result = subprocess.run(["docker", "ps", "-aq", "--filter", "label=pb.interactive=1"],
                                capture_output=True, text=True, timeout=15, check=False)
        if result.returncode:
            return
        saved = _saved_tunnel_ports()
        for container in result.stdout.split():
            task_id = _container_label_task(container)
            if task_id and task_id.isdigit() and execution_receipt.knows(int(task_id)):
                _register_vm(int(task_id), container)
                hl = subprocess.run(["docker", "inspect", "-f",
                                     "|".join('{{index .Config.Labels "%s"}}' % k for k in
                                              ("pb.health", "pb.health_port", "pb.vm_id", "pb.host_port")),
                                     container], capture_output=True, text=True, timeout=10, check=False)
                path, hp, vm_id, tun_hp = ((hl.stdout or "").strip().split("|") + ["", "", ""])[:4]
                if path.startswith("/") and hp.isdigit():
                    _start_ready_poll(int(task_id), container, int(hp), path)
                if tun_hp.isdigit() and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", vm_id):
                    _supervise_tunnel(int(task_id), container, int(tun_hp), vm_id,
                                      rp=saved.get(int(task_id)))
                else:
                    logging.warning(f"rental {task_id}: no pb.vm_id/pb.host_port label (started by "
                                    "an older agent); its reverse tunnel cannot be restored")
    except (OSError, subprocess.TimeoutExpired):
        logging.warning("Could not restore rental watchdog; Docker is unavailable")


def job_loop():
    _restore_vm_watch()
    while True:
        try:
            idle_mining.controller.polling()
            with _CLAIM_LOCK:
                _fix_pending = _fixes.pending()
                if not _fix_pending:
                    _JOB_RUNNING.set()          # claim window: the heartbeat won't queue a fix now
            if _fix_pending:                     # a support fix is queued/running: no new jobs
                time.sleep(POLL_S)
                continue
            # spec_id pins the claim to THIS machine: one account's machines (or every JIT standby
            # droplet, which share the operator account) must never run each other's jobs.
            r = httpx.get(f"{API_URL}/jobs/next", headers=HEADERS, params={"spec_id": SPEC_ID},
                          timeout=20, trust_env=False)
            if r.status_code == 204:
                pass                 # no job available right now
            elif r.status_code == 200:
                task = r.json()
                import execution_receipt
                execution_receipt.remember(task)
                # Honour the seller's selling window: outside it, or if this job would over-run the
                # window's end, refuse (report failed so the buyer isn't billed and it retries on
                # another node). Never interrupts a job already running.
                if not _job_within_schedule(task):
                    _post("/jobs/result", _signed_result(task["task_id"], status="failed",
                                                         failure_cause="outside_selling_schedule"))
                    if _con:
                        _con.line("skip", f"outside selling hours — declined #{task.get('task_id')}")
                    continue
                try:
                    idle_mining.controller.before_work()
                except Exception:
                    _post("/jobs/result", _signed_result(task["task_id"], status="failed",
                                                        failure_cause="idle_miner_stop_failed"))
                    continue
                if (task.get("compute_mode", "STANDARD") == "CONFIDENTIAL"
                        or task.get("attestation_required") or task.get("required_gpu_confidential")
                        or task.get("required_cpu_tee")):
                    _post("/jobs/result", _signed_result(task["task_id"], status="failed",
                                                        failure_cause="confidential_unavailable"))
                    continue
                if _con:
                    _con.line("claim", f"claimed {task.get('task_type')} #{task.get('task_id')}")
                tt = task.get("task_type")
                # Join the SAME trace that started on the platform: the job envelope carries
                # the W3C trace context (added by the API when the job is dispatched).
                carrier = task.get("trace_context") or {}
                _tel.bind(job_id=task.get("task_id"), transaction_id=task.get("transaction_id"))
                # Emit JOB_RECEIVED INSIDE the span so the receipt joins THIS job's trace
                # (the span extracts the platform trace from the carrier). Emitting it before
                # the span would attribute it to the previous job's still-lingering trace_id.
                with _tel.span("gpu.job.execute", carrier=carrier, task_type=str(tt)):
                    _tel.event(_tel.EVENTS.JOB_RECEIVED, message="job claimed",
                               task_type=tt, job_id=task.get("task_id"))
                    _tel.event(_tel.EVENTS.JOB_EXECUTION_STARTED, message="execution started",
                               task_type=tt)
                    try:
                        # SECURITY (P-2 cross-tenant VRAM residue): before running a GPU job, zero
                        # the card's free VRAM so this buyer can never read the PREVIOUS tenant's
                        # model weights/data left in device memory (NVIDIA does not clear VRAM
                        # between processes). Done at START (not teardown) so a crashed/killed prior
                        # job can't skip it. Best-effort + time-bounded; never blocks the job.
                        if task.get("gpu") and not _wipe_gpu_vram(task.get("task_id")) \
                                and os.getenv("AGENT_ALLOW_UNVERIFIED_VRAM", "false").lower() != "true":
                            # MANDATORY VRAM wipe (P-2): we could NOT verify the previous tenant's
                            # residual device memory was cleared, so running the next tenant here
                            # would let them read it. Fail-closed — refuse this job (report it failed
                            # so the buyer is not billed for a job that never ran and retries on a
                            # node that can wipe). A single-tenant operator who accepts the residue
                            # risk can set AGENT_ALLOW_UNVERIFIED_VRAM=true.
                            _refuse_unclean_vram(task)
                            continue
                        if tt == "notebook":
                            _run_notebook(task)
                        elif tt == "test":
                            _run_test(task)
                        elif tt == "template":
                            _run_template(task)
                        elif tt == "benchmark":
                            _run_benchmark(task)
                        elif tt == "render":
                            _run_render(task)
                        elif tt == "transcode":
                            _run_transcode(task)
                        elif tt == "stitch":
                            _run_stitch(task)
                        elif tt == "distributed":
                            _run_distributed(task)
                        elif tt == "container":
                            _run_container(task)
                        else:
                            _run_vm(task)
                        _tel.event(_tel.EVENTS.JOB_EXECUTION_COMPLETED,
                                   message="execution completed", task_type=tt)
                        if _con:
                            _con.line("done", f"finished {tt} #{task.get('task_id')}")
                    except Exception as _je:             # noqa: BLE001
                        _tel.event(_tel.EVENTS.JOB_EXECUTION_FAILED,
                                   message="execution failed", task_type=tt,
                                   reason=str(_je)[:200])
                        if _con:
                            _con.line("fail", f"{tt} #{task.get('task_id')} failed: {str(_je)[:80]}")
                        raise
                continue  # immediately poll again after finishing
            else:
                logging.warning(f"/jobs/next {r.status_code}: {r.text[:200]}")
        except Exception as e:                          # noqa: BLE001
            logging.error(f"job poll error: {e}")
        finally:
            _JOB_RUNNING.clear()
            idle_mining.controller.after_work()
        time.sleep(POLL_S)


def _prepare_idle_mining_gpu(device):
    """Reuse the cached VRAM overwrite on one device; never reset a seller's GPU."""
    import subprocess
    image = _cuda_wipe_image()
    if not image:
        return False
    try:
        active = subprocess.run(
            ["nvidia-smi", "-i", device, "--query-compute-apps=pid", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        # The agent holds its OWN resident CUDA context (attestation/capability probe); the
        # gate is meant to detect a BUYER's compute job, so exclude our own pid.
        others = [pp for pp in active.stdout.split()
                  if pp.strip() and pp.strip() != str(os.getpid())]
        if active.returncode or others:
            return False
        result = _run_docker(
            ["docker", "run", "--rm", "--pull=never", "--gpus", f"device={device}",
             "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
             image, "python", "-c", _VRAM_WIPE_PY], timeout=45,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 -- mining never bypasses failed memory preparation
        return False


def _ensure_wipe_image_async():
    """Cache the VRAM-wipe image if none is present. install.sh does this on a fresh install, but a
    node that AUTO-UPDATES never re-runs install.sh — and without a cached image the mandatory wipe
    fails and the node refuses every GPU job forever (every seller node, 2026-09-23). Pulls once in
    the background; jobs keep refusing (fail-closed) until it lands. NVIDIA only (ROCm image is ~20 GB)."""
    import subprocess
    if (os.getenv("AGENT_ALLOW_UNVERIFIED_VRAM", "false").lower() == "true"
            or not gpu_runtime.has_gpu() or gpu_runtime.vendor() == "amd" or _cuda_wipe_image()):
        return

    def _pull():
        try:
            cc = subprocess.run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                                capture_output=True, text=True, timeout=20).stdout.split(".")[0].strip()
            img = ("pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime" if cc.isdigit() and int(cc) >= 10
                   else "pytorch/pytorch:2.4.1-cuda12.4-cudnn9-runtime")   # Blackwell needs CUDA 12.8
            logging.warning(f"VRAM-wipe image missing — pulling {img} (GPU jobs are refused until it lands)")
            r = subprocess.run(["docker", "pull", img], capture_output=True, timeout=3600)
            logging.warning(f"VRAM-wipe image {img}: {'cached' if r.returncode == 0 else 'pull FAILED'}")
        except Exception as e:                           # noqa: BLE001 — never crash the agent
            logging.warning(f"VRAM-wipe image pull failed: {e}")
    threading.Thread(target=_pull, daemon=True, name="pb-wipe-image").start()


def run_agent():
    # Telemetry first — degrade-safe: if the collector is unreachable the agent still runs.
    _tel.init(agent_id=SPEC_ID, seller_id=os.getenv("PROVIDER"))
    _tel.event(_tel.EVENTS.STARTUP, message="agent started", api_url=API_URL, spec_id=SPEC_ID)
    if _con:
        _con.banner(API_URL, SPEC_ID, os.getenv("PROVIDER"))
    else:
        logging.warning(f"agent -> {API_URL} (spec {SPEC_ID})")
    # A killed or crashed agent leaves the previous buyer's staged plaintext behind: /dev/shm is a
    # tmpfs that survives process death (only unmount/reboot clears it), so those dirs would sit in
    # the seller's RAM indefinitely and pile up until the free-space guard stops choosing RAM and
    # every later job quietly falls back to the plaintext disk. Reclaim them before taking work.
    try:
        agent_scratch.sweep_stale()
    except Exception:                                    # noqa: BLE001 — never block startup
        pass
    for _bg in (_ensure_wipe_image_async, _ensure_tunnel_async):
        try:
            _bg()
        except Exception:                                # noqa: BLE001 — never block startup
            _TUN_SETTLED.set()
    idle_mining.controller.prepare_gpu = _prepare_idle_mining_gpu
    mining_income.start()
    _restore_vm_watch()  # recover detached rentals before any mining permit is considered
    try:
        _gpu_startup_selftest()          # before the first heartbeat can offer this node
    except Exception:                    # noqa: BLE001 — never block startup
        pass
    _probe_job_network()                 # the first heartbeat already says whether apps can run here
    threading.Thread(target=_job_network_loop, daemon=True, name="pb-jobnet-probe").start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()   # online while we wait
    _TUN_SETTLED.wait(timeout=240)       # don't claim a serving job this node can't publish yet
    job_loop()


if __name__ == "__main__":
    run_agent()
