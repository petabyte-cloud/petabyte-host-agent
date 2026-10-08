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
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time

import time as _t

import httpx
import safe_fetch
import gpu_runtime
import disk_quota
import template_storage

import crypto
import agent_scratch
import inference_worker
import fixes as _fixes
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


# Opt-in NVIDIA driver updates (gpu_driver.py, run as root by update.sh). The agent mirrors the
# seller's dashboard choice into OPTIN, publishes its live tasks to BUSY on every heartbeat, and claims
# no new job while gpu_driver.py holds DRAIN (it then installs and reboots only an idle node).
_DRV = {n: f"/var/lib/petabyte-agent/{f}" for n, f in (
    ("optin", "driver_update_optin"), ("busy", "busy.json"), ("drain", "drain"), ("state", "gpu_driver.json"))}


def _draining():
    return os.path.exists(_DRV["drain"])


def _driver_sync(body):
    try:
        if isinstance(body, dict) and "driver_updates" in body:
            if body["driver_updates"]:
                open(_DRV["optin"], "w").close()
            elif os.path.exists(_DRV["optin"]):
                os.unlink(_DRV["optin"])
        with open(_DRV["busy"] + ".tmp", "w") as f:
            json.dump({"at": time.time(), "tasks": _live_task_ids()}, f)
        os.replace(_DRV["busy"] + ".tmp", _DRV["busy"])
    except OSError:                                      # e.g. a dev run without the state dir
        pass


def _driver_update_report():
    """The last opt-in driver update attempt, for the seller's dashboard, or None."""
    try:
        with open(_DRV["state"]) as f:
            st = json.load(f)
        return {k: st.get(k) for k in ("ok", "reason", "before", "target", "at", "rebooting")}
    except (OSError, ValueError):
        return None


def _selling_now():
    if _GPU_UNUSABLE.is_set() or _fixes.pending():   # not offered while a support fix is in flight
        return False
    if _draining():                                   # an opt-in driver update waits for idle
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


def _note_egress_gateway(cfg):
    """The API moved this node's buyer egress to another gateway (the one in its own country):
    re-point wg-egress at it. The API only moves a node with no live rental, so no buyer
    connection is cut."""
    if not isinstance(cfg, dict):
        return
    if "PB_EGRESS_DIRECT_HOSTS" in cfg:                  # in-country storage reached directly
        try:
            import egress_vpn
            egress_vpn.note_direct_hosts(cfg["PB_EGRESS_DIRECT_HOSTS"])
        except Exception as e:                          # noqa: BLE001 - no bypass; tunnel still works
            logging.warning(f"direct-storage egress sync failed: {e}")
    pub, endpoint = cfg.get("PB_EGRESS_GATEWAY_PUBKEY"), cfg.get("PB_EGRESS_GATEWAY_ENDPOINT")
    if not (isinstance(pub, str) and isinstance(endpoint, str)):
        return
    if (pub, endpoint) == (os.getenv("PB_EGRESS_GATEWAY_PUBKEY"), os.getenv("PB_EGRESS_GATEWAY_ENDPOINT")):
        return
    try:
        import egress_vpn
        if egress_vpn.switch_gateway(pub, endpoint):
            logging.warning(f"buyer egress moved to gateway {cfg.get('id')} ({endpoint})")
    except Exception as e:                              # noqa: BLE001 - keep the old peer
        logging.warning(f"egress gateway switch failed: {e}")


def heartbeat_loop():
    global _SEAL_AES
    while True:
        try:
            _ensure_seal_keypair()
            import hardware_evidence
            _hb = {"spec_id": int(SPEC_ID), "hardware_evidence": hardware_evidence.collect(),
                   "selling_now": _advertise_selling_now(),  # JIT also waits for tunnel enrollment
                   "gpu_busy": _gpu_busy_report(),           # why it is not offered (busy-GPU guard)
                   "render_capabilities": _render_capabilities(),   # engines + headless_eevee (self-tested)
                   "remote_fixes": _fixes.enabled(),  # owner allowed signed support fixes
                   "driver_update": _driver_update_report()}   # last opt-in driver update attempt
            try:
                import diagnostics
                _hb["diagnostics"] = diagnostics.status()
            except Exception:
                pass  # optional support metadata never breaks liveness
            _storage = template_storage.heartbeat_report(globals().get("_TUN_GW", ""))
            if _storage is not None:
                if _SEAL_PUB:                            # can unseal rental secrets into /run/secrets
                    _storage["secrets_version"] = 1
                _hb["template_storage"] = _storage
            try:
                _hb["workload"] = _workload_report()     # admin abuse view; None when idle
            except Exception:                            # noqa: BLE001 — never breaks liveness
                pass
            if _JOB_NET["ok"] is not None:           # can this host isolate a networked app?
                _hb["job_network"] = dict(_JOB_NET)
            _hb["gateways"] = _gateway_report()       # per-rental gateway support + RTT to each
            _hb["live_tasks"] = _live_task_ids()     # lets the server end tasks this node lost
            _bundle = _agent_bundle()
            if _bundle:                               # which signed agent bundle this node runs
                _hb["agent_bundle"] = _bundle
            if _SEAL_PUB:
                _hb["seal_pubkey"] = _SEAL_PUB           # opt-in: let the platform wrap our AES key
            _caps = _confidential_caps()
            if _caps:
                _hb["confidential"] = _caps                       # REPORTED caps (server stores as reported)
            _infer_ticket = inference_worker.controller.ticket()
            _inf = inference_worker.controller.report()
            if _inf is not None:
                _hb["inference"] = _inf                  # community inference pool worker state
            r = httpx.post(f"{API_URL}/heartbeat", json=_hb, headers=HEADERS, timeout=10, trust_env=False)
            if r.status_code == 200:
                _body = r.json()
                if _body.get("pause_template_probes"):
                    try:
                        import template_probe
                        template_probe.yield_to_paid_work()
                    except Exception:
                        logging.warning("Could not stop optional template probe; its runtime remains bounded")
                try:
                    with _pb_vm_lock:
                        _live = any(not v.get("reported") for v in _pb_vm_watch.values())
                    inference_worker.controller.heartbeat(_infer_ticket, _body.get("inference_worker"),
                                                          live=_live)
                except Exception as exc:                 # noqa: BLE001 — never breaks liveness
                    logging.warning("Inference worker paused: %s", exc)
                # Owner may have changed the selling window in the dashboard — adopt it live.
                template_storage.set_catalog(_body.get("template_image_catalog"))
                _note_gateways(_body.get("gateways"))
                _note_egress_gateway(_body.get("egress_gateway"))
                _note_agent_update(_body)       # server-requested signed self-update (job_loop)
                _driver_sync(_body)             # opt-in driver updates: consent + what is running
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
                # BUYER IMAGE SNAPSHOT (image_snapshot.py): bake each requested id once, off-thread.
                try:
                    for _s in (_body.get("snapshots") or []):
                        _handle_snapshot(_s)
                except Exception as _se:             # noqa: BLE001
                    logging.debug(f"snapshot handling skipped: {_se}")
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
    payload = {"task_id": tid, "result": result, "status": status,
               "proof": proof, "signature": crypto.sign_proof(proof)}
    if not _post_result_ack_retry(payload):
        raise RuntimeError(f"/jobs/result was not acknowledged for task {tid}")


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


def _lease_payload(payload):
    if "task_id" in payload:
        import execution_receipt
        return {**payload, "lease_generation": execution_receipt.generation(payload["task_id"])}
    return payload


def _post(path, payload):
    try:
        httpx.post(f"{API_URL}{path}", headers=HEADERS, json=_lease_payload(payload), timeout=15, trust_env=False)
    except Exception as e:                              # noqa: BLE001
        logging.error(f"{path} error: {e}")


def _register_vm_tunnel(vm_id, tunnel_port, ip_address=None, attempts=5, lease_generation=None,
                        game_udp_host_port=None, service_bridge=False, service_udp_port=None,
                        gateway_id=None) -> bool:
    """P1-7: report node:port to the control plane so a template VM flips starting->running and
    the gateway can route buyers to it. ip_address is optional — the server falls back to the
    public source IP of this request. Retries a few times: right after /launch the VMRoute may not
    be committed yet (409 'not in a registrable state'), and a transient 5xx must not strand the
    VM in 'starting'. register_vm_tunnel is idempotent, so retrying is safe."""
    body = {"vm_id": str(vm_id), "tunnel_port": int(tunnel_port)}
    if service_bridge:
        body["service_bridge"] = True
    if service_udp_port is not None:
        body["service_udp_port"] = service_udp_port
    if lease_generation is not None:
        body["lease_generation"] = lease_generation
    # Persisted receipts survive an agent restart; only this VM's execution can
    # re-register its tunnel after a migration.
    for tid, rental in _tun_rentals.items():
        if rental.get("vm_id") == vm_id:
            import execution_receipt
            body["lease_generation"] = execution_receipt.generation(tid)
            gateway_id = gateway_id or rental.get("gw_id")
            if tid in _port_bridges:
                body["service_bridge"] = True
                if _port_bridges[tid]["bridge"].udp_port:
                    body["service_udp_port"] = _port_bridges[tid]["bridge"].udp_port
            break
    if ip_address:
        body["ip_address"] = ip_address
    body["gateway_id"] = gateway_id or _DEFAULT_GW_ID      # which gateway's loopback holds the port
    if game_udp_host_port is not None:
        body["game_udp_host_port"] = int(game_udp_host_port)
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


# Started inside a `custom` image launched with ssh (task ssh_start): the image's OWN sshd, publickey
# only — no password or keyboard-interactive (PAM) login whatever the image's sshd_config says. sshd
# daemonizes and stays in the container's namespaces under its cap set; nothing touches the host.
_SSHD_START = ('if [ ! -x /usr/sbin/sshd ]; then exit 3; fi; ssh-keygen -A >/dev/null 2>&1; '
               'mkdir -p /run/sshd && /usr/sbin/sshd -o AuthenticationMethods=publickey '
               '-o PasswordAuthentication=no -o PermitRootLogin=prohibit-password')


def _inject_ssh_key(container_name, ssh_pubkey, start_sshd=False):
    """Best-effort: drop the buyer's SSH public key into the container's authorized_keys so an
    sshd template accepts `ssh root@<id>.<zone>`; with start_sshd also start the image's own sshd.
    A no-op for templates without sshd (most serving templates expose an HTTP port, not port 22)
    and never fatal — mirrors the notebook prefetch. Returns the exec's exit code (None if not run):
    3 = the image has no /usr/sbin/sshd."""
    if not ssh_pubkey:
        return None
    import subprocess
    try:
        script = ('mkdir -p /root/.ssh && chmod 700 /root/.ssh && '
                  'printf "%s\\n" "$PB_KEY" >> /root/.ssh/authorized_keys && '
                  'chmod 600 /root/.ssh/authorized_keys')
        if start_sshd:
            script += " && " + _SSHD_START
        # -u 0: authorized_keys is root's and sshd must start as root, whatever USER the image sets.
        return subprocess.run(["docker", "exec", "-u", "0", "-e", f"PB_KEY={ssh_pubkey}", container_name,
                               "sh", "-c", script], capture_output=True, timeout=30, check=False).returncode
    except Exception as e:                              # noqa: BLE001
        logging.info(f"ssh key inject skipped for {container_name}: {e}")
        return None


def _post_result_ack(payload) -> bool:
    """POST a terminal /jobs/result and return True ONLY on an acknowledged 2xx response.

    Unlike _post (fire-and-forget: it swallows transport errors AND ignores the HTTP status), the
    watchdog must know the terminal result was actually recorded before it stops watching — a
    timeout, 5xx, or 4xx must NOT count as delivered, or the task is stranded 'running' with the
    seller's unit consumed and the buyer's escrow held. Retrying is safe: /jobs/result is
    idempotent on a terminal task (returns 200 {"idempotent": true})."""
    try:
        r = httpx.post(f"{API_URL}/jobs/result", headers=HEADERS, json=_lease_payload(payload), timeout=15, trust_env=False)
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


def _restore_volume(volume, restore_ref, task_id, task=None):
    """Download via a pre-signed GET URL, VERIFY the signed hash, decrypt, restore."""
    if not restore_ref:
        return True
    try:
        import hashlib
        from cryptography.fernet import Fernet
        g = httpx.post(f"{API_URL}/jobs/restore_url", headers=HEADERS, timeout=15,
                       json=_lease_payload({"task_id": task_id, "snapshot_ref": restore_ref}), trust_env=False)
        g.raise_for_status()
        g = g.json()
        enc = safe_fetch.get(g["download_url"], timeout=120, max_bytes=128 * 1024 * 1024).content
        if g.get("content_hash") and hashlib.sha256(enc).hexdigest() != g["content_hash"]:
            report_log(task_id, "RESTORE INTEGRITY CHECK FAILED — aborting")
            return False
        data = Fernet(g["enc_key"].encode()).decrypt(enc)      # client-side decrypt
        import io, tarfile, workspace_snapshot
        destination = workspace_snapshot.directory(task or {"task_id": task_id}, volume)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if sum(member.size for member in archive.getmembers()) > 128 * 1024 * 1024:
                raise ValueError("expanded workspace exceeds the restore limit")
            # The archive came from an untrusted seller. Reject traversal, devices
            # and escaping links rather than unpacking as root through plain tar.
            def confined(member, path):
                safe = tarfile.data_filter(member, path)
                # Container images use fixed numeric service UIDs. Dropping their
                # ownership makes a restored Jupyter/Blender directory root-owned
                # and unwritable. Preserve it only after the confinement checks.
                return safe.replace(uid=member.uid, gid=member.gid) if safe else None
            archive.extractall(destination, filter=confined)
        report_log(task_id, f"restored {volume} from {restore_ref} (verified)")
        return True
    except Exception as e:                              # noqa: BLE001
        logging.error(f"restore failed: {e}")
        return False


# Reproducible folders a checkpoint never spends its size budget on (matched by NAME at any
# depth): reinstall with pip/npm or re-download after a restore. Without this, one `pip install
# torch` in a Jupyter work dir pushed the archive past the restore limit and every later backup
# failed (task 673).
BACKUP_EXCLUDED_DIRS = ("site-packages", "dist-packages", "node_modules", "__pycache__", ".cache")
# Fernet adds ~1/3 (base64): a 90 MiB tar encrypts to ~120 MiB, under the 128 MiB restore limit.
_BACKUP_TAR_BUDGET = 90 * 1024 * 1024
_BACKUP_NOTICES = {}        # (task_id, kind) -> monotonic ts last shown to the buyer


def _backup_notice(tid, kind, message, every_s=1800):
    """Tell the buyer (job log) and ops (agent log) about a backup problem: at once, then at most
    every `every_s` per kind — never silently, never every 60 s."""
    last = _BACKUP_NOTICES.get((tid, kind))
    if last is not None and time.monotonic() - last < every_s:
        return
    _BACKUP_NOTICES[(tid, kind)] = time.monotonic()
    logging.warning(f"task {tid}: {message}")
    report_log(tid, message)


def _human(n):
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


def _backup_manifest(source, budget=_BACKUP_TAR_BUDGET):
    """(paths, skipped) for a workspace checkpoint. Excluded folders are pruned; if the rest is
    over budget the smallest files are kept first (notebooks, code, results) and the files that
    don't fit are returned as skipped [(path, size)], largest first. Paths start with './' so a
    buyer-named file like '--checkpoint-action=...' can never be read by tar as an option."""
    dirs, files = [], []
    for root, dnames, fnames in os.walk(source):        # never follows links out of the volume
        rel = os.path.relpath(root, source)
        dirs.append(rel)
        for d in list(dnames):
            if d in BACKUP_EXCLUDED_DIRS or os.path.islink(os.path.join(root, d)):
                dnames.remove(d)                         # a dir symlink is archived as a link
                if d not in BACKUP_EXCLUDED_DIRS:
                    fnames.append(d)
        for f in fnames:
            try:
                size = os.lstat(os.path.join(root, f)).st_size
            except OSError:
                continue                                 # vanished mid-walk
            files.append((os.path.normpath(os.path.join(rel, f)), size))
    used = 512 * len(dirs) + 1024                        # tar headers + end-of-archive
    keep, skipped = set(), []
    for path, size in sorted(files, key=lambda x: x[1]):
        cost = 512 + -(-size // 512) * 512
        if used + cost <= budget:
            keep.add(path); used += cost
        else:
            skipped.append((path, size))
    paths = dirs + [p for p, _ in files if p in keep]
    return ["./" + p if p != "." else "." for p in paths], skipped[::-1]


def _backup_once(task, volume):
    """Snapshot -> encrypt -> upload via a one-object pre-signed PUT -> sign checkpoint.
    The node holds NO standing object-storage credentials."""
    tid = task["task_id"]
    try:
        import subprocess, hashlib, time as _tt
        from cryptography.fernet import Fernet
        import tempfile, workspace_snapshot
        source = workspace_snapshot.directory(task, volume)
        paths, skipped = _backup_manifest(source)
        handle, local = tempfile.mkstemp(prefix=f"pb-backup-t{tid}-", suffix=".tar")
        os.close(handle)
        with tempfile.NamedTemporaryFile("wb", prefix=f"pb-backup-t{tid}-", suffix=".list") as listing:
            listing.write(b"\0".join(os.fsencode(p) for p in paths))
            listing.flush()
            # --ignore-failed-read: a file deleted since the walk (Jupyter's atomic save) is not
            # fatal; exit 1 only means a file changed while read — fine for a periodic snapshot.
            tar = subprocess.run(["tar", "-cf", local, "--no-recursion", "--ignore-failed-read",
                                  "-C", source, "--verbatim-files-from", "--null", "-T", listing.name],
                                 capture_output=True, timeout=600)
        if tar.returncode > 1:
            raise ValueError(f"tar failed: {tar.stderr.decode(errors='replace')[-200:]}")
        grant = httpx.post(f"{API_URL}/jobs/backup_url", headers=HEADERS, timeout=15,
                           json=_lease_payload({"task_id": tid,
                                 "filename": f"{volume}-{__import__('uuid').uuid4().hex}.tar.enc"}), trust_env=False)
        grant.raise_for_status()
        grant = grant.json()
        with open(local, "rb") as archive:
            enc = Fernet(grant["enc_key"].encode()).encrypt(archive.read())
        if len(enc) > 128 * 1024 * 1024:
            raise ValueError("workspace checkpoint exceeds the 128 MiB restore limit")
        uploaded = httpx.put(grant["upload_url"], content=enc, timeout=300, trust_env=False)
        uploaded.raise_for_status()
        h = hashlib.sha256(enc).hexdigest()             # hash of the uploaded bytes
        proof = {"task_id": tid, "output_hash": h[:16], "ts": int(_tt.time())}
        # Not _post: that swallows a rejected checkpoint, and the buyer would read "backup ->"
        # for a recovery point the server never recorded.
        httpx.post(f"{API_URL}/jobs/checkpoint", headers=HEADERS, timeout=15, trust_env=False,
                   json=_lease_payload({"task_id": tid, "snapshot_ref": grant["snapshot_ref"],
                                        "size_bytes": len(enc), "content_hash": h, "proof": proof,
                                        "signature": crypto.sign_proof(proof)})).raise_for_status()
        report_log(tid, f"backup -> {grant['snapshot_ref']} ({len(enc)} bytes, encrypted)")
        if _BACKUP_NOTICES.pop((tid, "failed"), None) is not None:
            report_log(tid, "workspace backups resumed")
        _backup_notice(tid, "info", "workspace backups never include reproducible folders ("
                       + ", ".join(BACKUP_EXCLUDED_DIRS) + "); reinstall/re-download them after a restore",
                       every_s=float("inf"))
        if skipped:
            shown = ", ".join(f"{p} ({_human(s)})" for p, s in skipped[:5])
            more = f" and {len(skipped) - 5} more" if len(skipped) > 5 else ""
            _backup_notice(tid, "skipped", f"WARNING: {len(skipped)} file(s) do not fit the "
                           f"{_BACKUP_TAR_BUDGET // 1024 ** 2} MB workspace backup and are NOT protected: "
                           f"{shown}{more}. Download them or keep a copy elsewhere.")
    except Exception as e:                              # noqa: BLE001
        # Our own ValueErrors are buyer-safe; anything else may carry a presigned URL.
        reason = str(e) if isinstance(e, ValueError) else type(e).__name__
        _backup_notice(tid, "failed", f"WORKSPACE BACKUP FAILED ({reason}); changes since the "
                       "last successful backup are NOT protected. Retrying every interval.")
    finally:
        if "local" in locals():
            try:
                os.unlink(local)
            except OSError:
                pass


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


_SNAPSHOTS_HANDLED = set()


def _snapshot_post(path, payload):
    return httpx.post(f"{API_URL}{path}", headers=HEADERS, json=_lease_payload(payload),
                      timeout=30, trust_env=False)


def _handle_snapshot(entry):
    """The server re-sends a pending snapshot every beat until our first part-URL call: bake each
    id once per agent process (a restart before that call simply bakes it again)."""
    sid = entry.get("id") if isinstance(entry, dict) else None
    if not sid or sid in _SNAPSHOTS_HANDLED:
        return
    _SNAPSHOTS_HANDLED.add(sid)
    import image_snapshot
    threading.Thread(target=image_snapshot.bake, args=(entry, _snapshot_post, report_log),
                     daemon=True, name=f"pb-snapshot-{sid}").start()


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
    """Choose the OCI runtime for `--runtime`, or None for Docker's default runtime.

    Prefers Kata (hardware VM per container, the strongest boundary) whenever the kata runtime is
    available. If an operator has registered gVisor (runsc) themselves it is selected when Kata is
    unavailable; the agent does not install it, so on a fresh install the non-Kata fallback is
    Docker's hardened default runtime (normally runc): all caps dropped, no-new-privileges, seccomp, private
    namespaces, no runtime socket, no host filesystem, a per-job firewalled network — a hardened
    container on the host kernel, not a VM. (2026-10-06 audit: earlier comments here and on the
    /security page wrongly described gVisor as the fallback; it was never installed.)
    Selection:
      * AGENT_RUNTIME=runc                                      -> docker default (no extra runtime)
      * AGENT_RUNTIME=gvisor|runsc, or AGENT_KATA_ENABLED=false -> registered runsc, else Docker default
      * default / AGENT_RUNTIME=kata -> Kata when installed AND (CPU job, or GPU job with
        AGENT_KATA_GPU=true); otherwise registered runsc if available, then Docker's
        default runtime. Never run a GPU job on Kata without GPU passthrough. GPU-in-VM stays gated on
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
    # Opt out of Kata via either control; runsc is selected only if registered.
    if pref in ("gvisor", "runsc") or os.getenv("AGENT_KATA_ENABLED", "").strip().lower() in ("0", "false", "no", "off"):
        return gvisor
    # DEFAULT: prefer Kata (VM per container) whenever it is available. A GPU job still needs
    # AGENT_KATA_GPU (VFIO) or it falls back to registered runsc, then Docker's default.
    if kata and (not is_gpu or _env_true("AGENT_KATA_GPU")):
        return kata
    return gvisor   # Kata unavailable / GPU without VFIO: registered runsc, else Docker default


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
      * --security-opt no-new-privileges — no setuid escalation, except the
        platform's Fedora KDE desktop, whose abc user needs sudo for package installs.
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
    flags = ["--cap-drop", "ALL", "--pids-limit", str(task.get("pids") or 1024),
             # Writable-layer quota wherever this host's Docker can enforce it (disk_quota.py);
             # hosts that can't (ext4/WSL) would otherwise reject EVERY buyer container.
             *disk_quota.flags(),
             # Bound host-side stdout/stderr storage independently of the container layer.
             "--log-driver", "local", "--log-opt", "max-size=10m",
             "--log-opt", "max-file=3"]
    # The pinned Webtop image already grants its abc desktop user passwordless sudo.
    # NNP blocks the setuid transition, producing a broken terminal. Permit that
    # transition ONLY for the server-declared Fedora KDE template task. Root remains
    # inside the container, with the same reduced capability whitelist, private
    # network/volume and resource limits; never grant privileged mode or host mounts.
    # An arbitrary image/batch job cannot opt in through a buyer parameter.
    desktop_sudo = (task.get("task_type") == "template"
                    and task.get("template") == "fedora-kde"
                    and task.get("allow_sudo") is True)
    if not desktop_sudo:
        flags += ["--security-opt", "no-new-privileges"]
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


import nb_fetch  # noqa: E402
from nb_fetch import prefetch_notebook as _prefetch_notebook


_VRAM_WIPE_PY = (
    "import sys, torch\n"
    "CHUNK = 128 * 1024 * 1024       # 128 MiB blocks\n"
    "HEADROOM = 512 * 1024 * 1024    # always leave this free for the display/compositor + this\n"
    "                                # process's own CUDA context. A single 92%-of-free alloc OOM'd\n"
    "                                # a 6 GB card that also drives a monitor and falsely benched it;\n"
    "                                # chunked allocation wipes the same free region without one\n"
    "                                # giant contiguous block, and tolerates free VRAM fluctuating.\n"
    "wiped = 0\n"
    "for d in range(torch.cuda.device_count()):\n"
    "    torch.cuda.set_device(d)\n"
    "    bufs = []  # held until the end so all free VRAM is overwritten SIMULTANEOUSLY (same\n"
    "               # coverage as the old single buffer; freeing between chunks could leave residue)\n"
    "    while True:\n"
    "        free, _ = torch.cuda.mem_get_info()\n"
    "        if free <= CHUNK + HEADROOM:\n"
    "            break\n"
    "        try:\n"
    "            buf = torch.zeros(CHUNK, dtype=torch.uint8, device='cuda')  # overwrite free VRAM with 0s\n"
    "        except RuntimeError:        # OOM/fragmentation: we've taken what we safely can\n"
    "            break\n"
    "        # Verify the device actually holds zeros before we claim 'verified clear'. A silent\n"
    "        # no-op (failed alloc, wrong device, driver quirk) would otherwise exit 0 and be\n"
    "        # trusted. count_nonzero is an on-device reduction over the whole buffer (cheap, no\n"
    "        # multi-GB host copy), so this checks every wiped byte, not a sample.\n"
    "        if int(torch.count_nonzero(buf).item()) != 0:\n"
    "            print('pb-vram-verify-failed', d); sys.exit(3)\n"
    "        bufs.append(buf); wiped += CHUNK\n"
    "    del bufs\n"
    "    torch.cuda.synchronize(); torch.cuda.empty_cache()\n"
    "print('pb-vram-wiped', torch.cuda.device_count(), wiped)\n")


def _cached_cuda_images():
    """The locally-CACHED CUDA-capable images, in wipe_image_candidates order (never pulls)."""
    import subprocess
    found = []
    for img in gpu_runtime.wipe_image_candidates():
        if not img:
            continue
        try:
            if subprocess.run(["docker", "image", "inspect", img],
                              capture_output=True, timeout=10).returncode == 0:
                found.append(img)
        except Exception:                                # noqa: BLE001 - check the next candidate
            continue
    return found


def _cuda_wipe_image():
    """Pick a locally-CACHED CUDA-capable image to run the VRAM memset, so wiping never triggers a
    multi-GB pull on the teardown/claim hot path. Returns None if none is cached (caller then just
    logs a recommendation rather than stalling)."""
    found = _cached_cuda_images()
    return found[0] if found else None


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
    # Run the memset, retrying ONCE on a non-verification failure. The container self-test can
    # fire within a second of an agent (auto-update) restart; a single transient docker/driver
    # hiccup used to bench a healthy node for 10 min. A verify failure (exit 3) is deterministic —
    # the VRAM really wasn't clean — so it is NOT retried and fails closed immediately.
    for attempt in range(2):
        try:
            result = subprocess.run(["docker", "run", "--rm", *gpu_runtime.docker_gpu_args(), "--network", "none",
                            "--label", (f"pb.task={tid}" if tid else "pb.kind=vram-wipe"),
                            img, "python", "-c", _VRAM_WIPE_PY],
                           capture_output=True, timeout=int(os.getenv("VRAM_WIPE_TIMEOUT_S", "180")))
            if result.returncode == 0:
                return True
            if result.returncode == 3:                   # verification failed — deterministic, fail closed
                return False
        except Exception:                                # noqa: BLE001 — the CALLER decides fail-open vs closed
            pass
        if attempt == 0:
            time.sleep(2)                                # let a transient startup/driver hiccup clear
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


# The server says (heartbeat `agent_update`) that a newer SIGNED bundle is served. job_loop runs the
# same update the 6-hourly timer runs, but now and only between jobs: update.sh restarts the agent,
# which would kill a job mid-run. update.sh still verifies the bundle against the pinned release key
# and refuses anything unsigned or tampered, so this can only ever apply a genuinely signed release.
_UPDATE_UNIT = "petabyte-agent-update.service"
_UPDATE_RETRY_S = 1800        # a refused/failed update (e.g. bad signature) retries at most this often
_AGENT_UPDATE = {"wanted": None, "started": 0.0}


def _note_agent_update(body):
    au = body.get("agent_update") if isinstance(body, dict) else None
    want = str(au.get("bundle") or "") if isinstance(au, dict) and au.get("required") else ""
    _AGENT_UPDATE["wanted"] = want if len(want) == 64 else None


def _systemctl(*args):
    import subprocess as _sp
    try:
        return _sp.run(["systemctl", *args], capture_output=True, text=True, timeout=15)
    except (OSError, _sp.TimeoutExpired):
        return None


def _self_update_holds_claims():
    """True while a server-requested self-update is starting or running: job_loop claims nothing, so
    the agent restart that update.sh performs never lands mid-job. Respects the seller's opt-out:
    PETABYTE_AUTO_UPDATE=false leaves the update timer uninstalled, and then nothing runs here."""
    want = _AGENT_UPDATE["wanted"]
    if not want or want == _agent_bundle():
        return False
    st = _systemctl("is-active", _UPDATE_UNIT)
    if st is not None and st.stdout.strip() in ("activating", "active"):
        return True                                  # update.sh is running: wait for its restart
    if time.time() - _AGENT_UPDATE["started"] < _UPDATE_RETRY_S:
        return False                                 # tried recently and it did not land: keep serving
    en = _systemctl("is-enabled", "petabyte-agent-update.timer")
    if en is None or en.stdout.strip() != "enabled":
        return False                                 # auto-update opted out / not a systemd host
    _AGENT_UPDATE["started"] = time.time()
    r = _systemctl("start", "--no-block", _UPDATE_UNIT)
    if r is None or r.returncode != 0:
        logging.warning("agent self-update could not start: %s", (r.stderr if r else "")[:200])
        return False
    logging.info("newer signed agent available (%s…): updating before the next job", want[:12])
    if _con:
        _con.line("update", "newer signed agent available — updating before the next job")
    return True


def _job_network_loop():
    last = time.time()
    while True:
        time.sleep(300)                                  # a launch failure is re-checked within 5 min
        if not _JOB_NET["ok"] or time.time() - last >= 3600:
            _probe_job_network()
            last = time.time()


def _ensure_job_network(tid, allowed_udp_port=None):
    """(network name, None), or (None, reason) when the per-job bridge can't be built."""
    try:
        import network_policy
        net = (network_policy.ensure(tid, allowed_udp_port=allowed_udp_port)
               if allowed_udp_port is not None else network_policy.ensure(tid))
        return net, None
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
    live = [vm for vm, entry in _LIVE_TEMPLATES.items() if entry["task"]["task_id"] == tid]
    for vm in live:
        entry = _LIVE_TEMPLATES.pop(vm)
        if entry.get("stop"):
            entry["stop"].set()
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


def _free_udp_host_port(bind_ip):
    """Reserve a free UDP host port on the node's private game WireGuard address."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((str(bind_ip), 0))
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


def _start_ready_poll(tid, name, host_port, path, auth=None, process=None):
    import threading
    threading.Thread(target=_await_ready, args=(tid, name, host_port, path, auth, process),
                     name=f"pb-ready-{tid}", daemon=True).start()


def _await_ready(tid, name, host_port, path, auth=None, process=None):
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
            options = {"auth": tuple(auth)} if auth else {}
            if process == "minecraft":
                # The pinned image's monitor speaks the Java status protocol, not HTTP.
                ready = subprocess.run(["docker", "exec", name, "mc-health"],
                                       capture_output=True, timeout=5).returncode == 0
            else:
                ready = httpx.get(url, timeout=3, trust_env=False, **options).status_code == 200
                if ready and process:
                    # An nginx login page is not proof that a desktop session booted.
                    if process != "plasmashell" or subprocess.run(
                            ["docker", "exec", name, "pgrep", "-x", "plasmashell"],
                            capture_output=True, timeout=5).returncode != 0:
                        time.sleep(5)
                        continue
            if ready:
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
# Multi-gateway: every live gateway, {id: "user@host"} (from /node/tunnel and each heartbeat). A
# rental names the gateway its address resolves to (payload tunnel_gateway); its ssh -R must land on
# THAT box, because the route the API stores is 127.0.0.1:<port> on it. "us" = _TUN_GW (default).
_DEFAULT_GW_ID = "us"
_TUN_GWS = {}
_GW_TARGET_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}@[A-Za-z0-9.:\[\]-]{1,253}$")
_GW_ID_RE = re.compile(r"^[a-z]{2,8}$")


def _note_gateways(rows):
    """Adopt the API's gateway list ({id, target}); malformed entries are ignored."""
    if not isinstance(rows, list):
        return
    seen = {}
    for g in rows[:16]:
        if (isinstance(g, dict) and isinstance(g.get("id"), str) and _GW_ID_RE.match(g["id"])
                and isinstance(g.get("target"), str) and _GW_TARGET_RE.match(g["target"])):
            seen[g["id"]] = g["target"]
    if seen:
        _TUN_GWS.clear()
        _TUN_GWS.update(seen)


def _rental_gateway(task):
    """(gateway id, ssh target) this rental's tunnel must reach. The default gateway is always our
    own enrolled/hand-configured _TUN_GW; another one is the target the API named for it."""
    g = task.get("tunnel_gateway") if isinstance(task, dict) else None
    if isinstance(g, dict) and g.get("id") and g.get("id") != _DEFAULT_GW_ID:
        gid, target = str(g.get("id")), str(g.get("target") or "") or _TUN_GWS.get(str(g.get("id")), "")
        if _GW_ID_RE.match(gid) and _GW_TARGET_RE.match(target):
            return gid, target
        return gid, ""                                    # unknown/unusable: fails, never mis-routes
    return _DEFAULT_GW_ID, None


def _gateway_tcp_ms():
    """TCP connect time from this node to each gateway's sshd, {id: ms} (the tunnel's own path)."""
    out = {}
    targets = dict(_TUN_GWS)
    if _TUN_GW:
        targets.setdefault(_DEFAULT_GW_ID, _TUN_GW)
    for gid, target in list(targets.items())[:8]:
        host = target.rsplit("@", 1)[-1].strip("[]")
        try:
            start = time.monotonic()
            with socket.create_connection((host, 22), timeout=2):
                out[gid] = round((time.monotonic() - start) * 1000, 1)
        except OSError:
            pass
    return out


_GW_PROBE = {"at": 0.0, "value": {}, "thread": None}


def _gateway_report():
    """The heartbeat's `gateways` field: proves per-rental gateway support; RTTs refresh in the
    background every ~5 min so a slow probe never delays liveness."""
    now = time.monotonic()
    t = _GW_PROBE["thread"]
    if now - _GW_PROBE["at"] > 300 and (t is None or not t.is_alive()):
        def run():
            _GW_PROBE["value"] = _gateway_tcp_ms()
            _GW_PROBE["at"] = time.monotonic()
        _GW_PROBE["thread"] = threading.Thread(target=run, daemon=True, name="pb-gw-probe")
        _GW_PROBE["thread"].start()
    return {"version": 1, "tcp_ms": dict(_GW_PROBE["value"])}
_port_bridges = {}
_PORT_BRIDGES_FILE = "/var/lib/petabyte-agent/port_bridges.json"
_PORT_BRIDGES_LOCK = threading.RLock()


def _native_bridge_options(endpoints, saved=None):
    if not any(item["protocol"] == "udp" for item in endpoints):
        if saved:
            raise ValueError("unexpected native UDP state")
        return {}
    import ipaddress
    import egress_vpn
    if not egress_vpn.enabled() or not egress_vpn.ensure_tunnel():
        raise ValueError("public UDP requires an enrolled WireGuard tunnel")
    for attempt in range(20):
        if egress_vpn.peer_ready():
            break
        if attempt == 19:
            raise ValueError("public UDP gateway handshake is not ready")
        time.sleep(.5)
    bind = str(ipaddress.ip_interface(os.environ["PB_EGRESS_ADDR"]).ip)
    gateway = "10.9.0.1"
    if saved and (saved.get("udp_bind") != bind or saved.get("udp_gateway") != gateway
                  or type(saved.get("udp_port")) is not int or not 1024 <= saved["udp_port"] <= 65535):
        raise ValueError("native UDP assignment changed during restart")
    return dict(udp_bind=bind, udp_gateway=gateway, udp_port=saved["udp_port"] if saved else 0)


def _start_service_bridge(endpoints, token, generation, native):
    import port_bridge
    import egress_vpn
    bridge = port_bridge.Bridge(endpoints, token, generation, **native)
    try:
        if bridge.udp_port:
            egress_vpn.service_udp_firewall(bridge.udp_port)
        return bridge.start()
    except BaseException:
        _shutdown_service_bridge(bridge)
        raise


def _shutdown_service_bridge(bridge):
    bridge.shutdown()
    if bridge.udp_port:
        import egress_vpn
        try:
            egress_vpn.service_udp_firewall(bridge.udp_port, remove=True)
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            pass  # The closed authenticated socket is fenced even if rule removal fails.


def _persist_port_bridges():
    import tempfile
    with _PORT_BRIDGES_LOCK:
        if os.path.islink(_PORT_BRIDGES_FILE):
            raise ValueError("bridge state must not be a symlink")
        fd, path = tempfile.mkstemp(dir=os.path.dirname(_PORT_BRIDGES_FILE), prefix=".port_bridges.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({str(tid): state["config"] for tid, state in _port_bridges.items()}, handle)
            os.replace(path, _PORT_BRIDGES_FILE)
        finally:
            if os.path.exists(path):
                os.unlink(path)


def _restore_port_bridge(tid, name):
    """Restore only this recorded rental's actual loopback Docker bindings."""
    import execution_receipt
    try:
        import stat
        info = os.lstat(_PORT_BRIDGES_FILE)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            return None
        with open(_PORT_BRIDGES_FILE) as handle:
            config = json.load(handle).get(str(tid))
        if (not config or not execution_receipt.knows(tid)
                or config["generation"] != execution_receipt.generation(tid)):
            return None
        info = subprocess.run(["docker", "inspect", name], capture_output=True,
                              text=True, timeout=10, check=True)
        data = json.loads(info.stdout)[0]
        # The journal records the container NAME (pb-...); after an agent restart or host reboot
        # _restore_vm_watch finds it by ID (docker ps -q). Same container if either matches; the
        # mismatch failed every SSH/public-port rental on each agent restart (2026-10-03 reboot test).
        if config["container"] not in {name, data.get("Id"), (data.get("Id") or "")[:12],
                                       (data.get("Name") or "").lstrip("/")}:
            return None
        actual = data["NetworkSettings"]["Ports"]
        for endpoint in config["endpoints"]:
            expected = {"HostIp": "127.0.0.1", "HostPort": str(endpoint["host_port"])}
            if expected not in (actual.get(f"{endpoint['container_port']}/{endpoint['protocol']}") or []):
                return None
        native = _native_bridge_options(config["endpoints"], config.get("native_udp"))
        if native and not config.get("native_udp"):
            return None  # An older UDP journal has no assigned native port; do not guess on restart.
        bridge = _start_service_bridge(config["endpoints"], config["token"], config["generation"], native)
        with _PORT_BRIDGES_LOCK:
            _port_bridges[tid] = dict(bridge=bridge, config=config)
        return bridge.port
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
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
                          or _reverse_tunnel_enabled()) and not _gpu_busy_elsewhere() and _wipe_ready()


_WIPE_READY = {"ok": False}


def _wipe_ready():
    """Can this node run the mandatory VRAM wipe a rental starts with? Not while its wipe image is
    still downloading after an install or an auto-update: offering the GPU then made every claimed
    job refuse itself, and two refusals quarantined the node for 6 h (spec 246, 2026-10-06). Nodes
    that skip the wipe (AGENT_ALLOW_UNVERIFIED_VRAM) and AMD nodes are unchanged."""
    if (_WIPE_READY["ok"] or os.getenv("AGENT_ALLOW_UNVERIFIED_VRAM", "false").lower() == "true"
            or not gpu_runtime.has_gpu() or gpu_runtime.vendor() == "amd"):
        return True
    _WIPE_READY["ok"] = bool(_cuda_wipe_image())          # once cached, it stays cached
    return _WIPE_READY["ok"]


# `kicked` starts at import: run_agent already starts the first pull of the wipe image.
_WIPE_WAIT = {"since": None, "kicked": time.time(), "reported": False}


def _wipe_image_pending():
    """True while a GPU node must not CLAIM jobs: its mandatory-wipe image is still downloading
    (fresh install, or right after an auto-update). _wipe_ready() keeps the node off sale, but the
    server still hands it GPU probes — and claiming one then made the job refuse itself, bench a
    healthy node and email a false "mandatory VRAM wipe failed" report (spec 270, 2026-10-08).
    Retries a failed pull every 10 min; still not cached after 2 h is stuck, not slow, so support
    gets ONE diagnostics report (a node that can never pull must not go silent)."""
    if _wipe_ready():
        if _WIPE_WAIT["since"] is not None:
            _WIPE_WAIT.update(since=None, reported=False)
            logging.warning("VRAM-wipe image cached: taking jobs again")
        return False
    now = time.time()
    if _WIPE_WAIT["since"] is None:
        _WIPE_WAIT["since"] = now
        logging.warning("VRAM-wipe image still downloading: not taking jobs until it is cached")
    if now - _WIPE_WAIT["kicked"] >= 600:                  # the first pull may have failed
        _WIPE_WAIT["kicked"] = now
        _ensure_wipe_image_async()
    if now - _WIPE_WAIT["since"] >= 7200 and not _WIPE_WAIT["reported"]:
        _WIPE_WAIT["reported"] = True
        threading.Thread(target=lambda: _send_diagnostics("VRAM-wipe image not cached after 2 h"),
                         daemon=True, name="pb-diagnostics").start()
    return True


# Headless EEVEE capability (2026-10-06). EEVEE needs an EGL GPU context; whether that works in a
# headless container is build/driver-specific, so we NEVER assume it — the node advertises EEVEE
# only after a tiny real EEVEE GPU render actually succeeds here. Tested once per boot, only when the
# render image is already cached (never pulls on the hot path). AGENT_EEVEE_ENABLED=false opts out.
# "error": why the node is not EEVEE-capable (self-test output tail), sent in the heartbeat so a
# failing node can be diagnosed without shell access (2026-10-07: the RTX 2060 reported only false).
# "backend": the Blender GPU backend whose self-test passed; EEVEE renders use the same one.
_EEVEE = {"ok": None, "blender_version": None, "error": None, "backend": None}
# Vulkan first: it needs no EGL. On the RTX 2060 (2026-10-07) NVIDIA's EGL loaded but Blender
# segfaulted creating the headless OpenGL context; Vulkan renders headless in the same image.
_EEVEE_BACKENDS = ("vulkan", "opengl")


def _eevee_render_image():
    return os.getenv("EEVEE_RENDER_IMAGE", os.getenv("RENDER_IMAGE", "linuxserver/blender:latest"))


def _eevee_selftest(img):
    """Self-test each GPU backend in turn; the first that renders on the GPU becomes the node's EEVEE
    backend. On failure the heartbeat gets every backend's reason. Never raises."""
    errs = []
    for backend in _EEVEE_BACKENDS:
        if _eevee_selftest_one(img, backend):
            _EEVEE["backend"], _EEVEE["error"] = backend, None
            return True
        errs.append(f"[{backend}] {_EEVEE['error']}")
    _EEVEE["backend"], _EEVEE["error"] = None, " ".join(errs) + " [diag] " + _eevee_diag(img)
    return False


def _eevee_diag(img):
    """Why can't the NVIDIA graphics stack start in the render container? Unresolved dependencies of
    the driver's EGL/Vulkan libraries as the toolkit mounted them, plus the host driver and toolkit
    versions. Both backends segfaulted inside NVIDIA's libraries on the RTX 2060 (2026-10-07), the
    signature of a toolkit that mounts only part of the graphics stack. Never raises."""
    import subprocess
    script = ("d=/usr/lib/x86_64-linux-gnu; echo nvlibs=$(ls $d | grep -c nvidia); "
              "for l in libEGL_nvidia.so.0 libGLX_nvidia.so.0; do [ -e $d/$l ] || { echo absent=$l; continue; }; "
              "ldd $d/$l | awk '/not found/{print \"missing=\"$1}'; done")
    parts = []
    try:
        r = _run_docker(["docker", "run", "--rm", "--network", "none", *_isolation_flags({"gpu": True}),
                         *gpu_runtime.docker_gpu_args(), *gpu_runtime.graphics_env(),
                         "--entrypoint", "sh", img, "-c", script],
                        timeout=60, capture_output=True, text=True, check=False)
        parts.append(" ".join(((r.stdout or "") + (r.stderr or "")).split())[:300])
    except Exception as e:                               # noqa: BLE001
        parts.append(f"container {type(e).__name__}")
    for cmd in (["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                ["nvidia-container-cli", "--version"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
            parts.append(f"{cmd[0]}={out.splitlines()[0][:40] if out else '?'}")
        except Exception:                                # noqa: BLE001
            parts.append(f"{cmd[0]}=n/a")
    return "; ".join(parts)


def _eevee_selftest_one(img, backend):
    """One tiny headless EEVEE GPU render of the factory scene on one Blender GPU backend. True only
    if a frame is produced on the GPU with no software (llvmpipe) fallback. Records the Blender
    version, and the reason in _EEVEE["error"] on failure."""
    import tempfile, subprocess, os as _os, shutil as _sh
    out = tempfile.mkdtemp(prefix="eevee-selftest-")
    try:
        _os.chmod(out, 0o777)
        expr = _render_setup_expr(samples=1, gpu=True, engine="EEVEE", resolution=(64, 64)) + (
            # Ask Blender which GL renderer drew the frame: the only reliable software-GL test. EGL
            # warnings alone are not (the 2060 rendered with exit 0 but logged EGL_BAD_MATCH, 10-07).
            "\nimport gpu\n"
            "def _pb_gpu(*a):\n"
            "    try:\n"
            "        print('PBGPU=' + gpu.platform.renderer_get() + ' | ' + gpu.platform.vendor_get(), flush=True)\n"
            "    except Exception as e:\n"
            "        print('PBGPU=?' + str(e), flush=True)\n"
            "bpy.app.handlers.render_post.append(_pb_gpu)\n")
        # gpu=True: the GPU runtime, as a real render gets. With {} a KVM host picked Kata, whose VM
        # has no /proc/driver/nvidia, so the NVIDIA hook failed and EEVEE never passed (2060, 10-07).
        cmd = ["docker", "run", "--rm", "--network", "none", *_isolation_flags({"gpu": True}),
               *gpu_runtime.docker_gpu_args(), *gpu_runtime.graphics_env(),
               # TMPDIR=/out: a crash leaves blender.crash.txt where we can read it after exit
               "-v", f"{out}:/out", "-e", "TMPDIR=/out", "--entrypoint", "blender", img,
               "-b", "--gpu-backend", backend, "--factory-startup", "--disable-autoexec",
               "--python-exit-code", "86", "--python-expr", expr, "-o", "/out/pb_eevee_", "-f", "1"]
        r = _run_docker(cmd, timeout=int(os.getenv("EEVEE_SELFTEST_TIMEOUT_S", "180")),
                        capture_output=True, text=True, check=False)
        raw = (r.stdout or "") + (r.stderr or "")
        combined = raw.lower()
        for line in combined.splitlines():
            if line.startswith("blender ") and _EEVEE["blender_version"] is None:
                _EEVEE["blender_version"] = line.split()[1] if len(line.split()) > 1 else None
        produced = any(f.startswith("pb_eevee_") for f in _os.listdir(out))
        gl = next((ln[6:].strip() for ln in raw.splitlines() if ln.startswith("PBGPU=")), None)
        if gl and not gl.startswith("?"):                # Blender named its renderer: trust that
            software = any(w in gl.lower() for w in ("llvmpipe", "softpipe", "swrast", "software"))
        else:                                            # no answer: fall back to log heuristics
            software = ("llvmpipe" in combined or "egl_bad" in combined
                        or "could not open display" in combined or "software rasteriz" in combined)
        ok = bool(r.returncode == 0 and produced and not software)
        if not ok:
            # The libraries a crash ran through (blender.crash.txt), and the output minus NVIDIA's
            # repeated "EGL_SUCCESS" lines, which buried the real error on the 2060.
            crash = ""
            try:
                with open(_os.path.join(out, "blender.crash.txt"), errors="replace") as f:
                    libs = dict.fromkeys(re.findall(r"(lib[\w.+-]*\.so[\w.]*)", f.read()))
                crash = f", crash in {' '.join(list(libs)[:6]) or '?'}"
            except OSError:
                pass
            tail = "\n".join(ln for ln in raw.strip().splitlines() if "EGL_SUCCESS" not in ln)[-180:]
            _EEVEE["error"] = (
                f"exit {r.returncode}{'' if produced else ', no frame'}{', software GL' if software else ''}"
                f"{', renderer ' + gl if gl else ''}{crash}: " + tail)
        else:
            _EEVEE["error"] = None
        return ok
    except Exception as e:                               # noqa: BLE001 — capability probe, never fatal
        _EEVEE["error"] = f"{type(e).__name__}: {e}"[:200]
        return False
    finally:
        import shutil as _sh2; _sh2.rmtree(out, ignore_errors=True)


def _eevee_ready():
    """Can this node render EEVEE headless on the GPU? Cached after one real self-test per boot.
    Only runs the test once the render image is cached (so it never pulls on the hot path); until
    then it is simply not advertised — fail-safe, never a false capability."""
    if _EEVEE["ok"] is not None:
        return _EEVEE["ok"]
    import subprocess
    if os.getenv("AGENT_EEVEE_ENABLED", "true").strip().lower() not in ("1", "true", "yes", "on"):
        prerequisite_error = "EEVEE self-test disabled; set AGENT_EEVEE_ENABLED=true to enable it"
    elif not gpu_runtime.has_gpu():
        prerequisite_error = "No GPU detected; check that the agent can access the GPU and driver"
    elif not __import__("shutil").which("docker"):
        prerequisite_error = "Docker executable not found; install Docker and make it available on PATH"
    else:
        prerequisite_error = None
    if prerequisite_error:
        _EEVEE["ok"], _EEVEE["error"] = False, prerequisite_error
        return False
    img = _eevee_render_image()
    try:
        cached = subprocess.run(["docker", "image", "inspect", img],
                                capture_output=True, timeout=10).returncode == 0
    except Exception:                                    # noqa: BLE001
        cached = False
    if not cached:
        _EEVEE["error"] = f"not tested yet: render image {img} is not cached"
        return False                                     # not tested yet; re-checked next time
    _EEVEE["ok"] = _eevee_selftest(img)
    return _EEVEE["ok"]


_EEVEE_PROBE = threading.Lock()


def _eevee_probe_bg():
    """Run _eevee_ready in the background unless it already has an answer or is running. Called at
    boot AND after each render: boot skips the test while the render image isn't cached, and before
    2026-10-07 nothing retried, so a node that pulled the image later never advertised EEVEE."""
    if _EEVEE["ok"] is not None or not _EEVEE_PROBE.acquire(blocking=False):
        return

    def run():
        try:
            _eevee_ready()
        finally:
            _EEVEE_PROBE.release()
    threading.Thread(target=run, daemon=True, name="pb-eevee-probe").start()


def _render_capabilities():
    """What this node can render, for the marketplace. `headless_eevee` is only true after a real
    EEVEE self-test passed here; `render_engines` always includes Cycles on a GPU node."""
    v = gpu_runtime.vendor()
    engines, eevee = [], False
    if v in ("nvidia", "amd"):
        engines.append("CYCLES")
        eevee = _EEVEE["ok"] is True   # cached: the background probe / a render runs the real self-test
        if eevee:
            engines.append("EEVEE")
    try:
        gpu_count = len(_gpu_usage() or []) if gpu_runtime.has_gpu() else 0
    except Exception:                                    # noqa: BLE001
        gpu_count = 1 if gpu_runtime.has_gpu() else 0
    return {"blender": True, "render_engines": engines, "headless_eevee": bool(eevee),
            "gpu_count": gpu_count, "gpu_vendor": v, "blender_version": _EEVEE["blender_version"],
            "eevee_error": _EEVEE["error"], **_nvidia_stack()}


_NV_STACK = {"at": 0.0, "v": {}}


def _nvidia_stack():
    """Host NVIDIA driver + container-toolkit versions, so ops see which sellers run an old stack
    (gpu_toolkit.py upgrades the toolkit; drivers are the seller's). Cached for 10 minutes."""
    if gpu_runtime.vendor() != "nvidia":
        return {}
    if time.time() - _NV_STACK["at"] > 600:
        import gpu_toolkit
        try:
            drv = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                                 capture_output=True, text=True, timeout=10).stdout.split()[0]
        except Exception:                                # noqa: BLE001 — advisory
            drv = None
        _NV_STACK.update(at=time.time(), v={"nvidia_driver": drv, "nvidia_toolkit": gpu_toolkit.installed()})
    return _NV_STACK["v"]


# Busy-GPU guard (owner 2026-10-05). A GPU already loaded by something that is not ours (a miner on
# the host, the seller's own work) is not offered: a renter would get a fraction of the card, and a
# seller could rent it to a second account of theirs while it mines. Spec 269 held 100 %/300 W/3.4 GB
# for 8 h with no process or container visible to the agent. Sampled once a minute, only while
# nothing of ours runs; GPU_BUSY_SAMPLES readings in a row flip the state either way.
GPU_BUSY_UTIL = int(os.getenv("GPU_BUSY_UTIL_PCT", "50"))
GPU_BUSY_MEM_MB = int(os.getenv("GPU_BUSY_MEM_MB", "2048"))
GPU_BUSY_SAMPLES = 3
_GPU_BUSY = {"busy": False, "since": None, "last": 0.0, "window": [], "detail": None}


def _ours_running():
    """Any container of ours (rental, probe, wipe, benchmark, inference) running, or Docker unsure.
    A miner is never ours (idle mining was removed 2026-10-06), even one carrying our old label: its
    GPU load counts as busy-elsewhere, so the node is held off sale."""
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Labels}}"], capture_output=True, text=True,
                           timeout=10)
    except Exception:                                    # noqa: BLE001 — unsure: no new evidence
        return True
    return r.returncode != 0 or any(("pb." in line or "market.petabyte" in line) and "idle-miner" not in line
                                    for line in r.stdout.splitlines())


def _gpu_busy_elsewhere(now=None):
    st = _GPU_BUSY
    now = time.time() if now is None else now
    if now - st["last"] < 60:
        return st["busy"]
    st["last"] = now
    if not gpu_runtime.has_gpu() or _rental_live() or _JOB_RUNNING.is_set() or _ours_running():
        st["window"].clear()                             # our own load proves nothing either way
        return st["busy"]
    usage = _gpu_usage()
    if not usage:                                        # nvidia-smi failed (stalls under load): no evidence
        st["window"].clear()
        return st["busy"]
    hits = [g for g in usage
            if (g.get("util") or 0) >= GPU_BUSY_UTIL or (g.get("mem_mb") or 0) >= GPU_BUSY_MEM_MB]
    st["window"] = (st["window"] + [bool(hits)])[-GPU_BUSY_SAMPLES:]
    if len(st["window"]) == GPU_BUSY_SAMPLES:
        if all(st["window"]) and not st["busy"]:
            st.update(busy=True, since=int(now), detail=hits[0])
            logging.warning("GPU busy with something outside Petabyte (%s): not offered to buyers", hits[0])
            if _con:
                _con.line("busy", "GPU is busy with another program: not offered until it is idle")
        elif not any(st["window"]) and st["busy"]:
            st.update(busy=False, since=None, detail=None)
            logging.info("GPU idle again: offered to buyers")
    return st["busy"]


def _gpu_busy_report():
    """Heartbeat field while the guard holds the node off sale, so the seller is told why."""
    st = _GPU_BUSY
    if not st["busy"]:
        return None
    d = st["detail"] or {}
    return {"since": st["since"], "util": d.get("util"), "mem_mb": d.get("mem_mb"),
            "power_w": d.get("power_w")}


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


def _open_reverse_tunnel(host_port, tid, prefer=None, gateway=None):
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
    target = _TUN_GW if gateway is None else gateway     # "" = a gateway we cannot name: refuse
    if not target:
        report_log(tid, "reverse tunnel: this rental's gateway is unknown to this node; not opening it")
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
               "-R", f"127.0.0.1:{rp}:127.0.0.1:{host_port}", target]
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
            report_log(tid, f"reverse tunnel up: gateway {target.rsplit('@', 1)[-1]} 127.0.0.1:{rp} -> vm "
                            "(node opens no inbound port)")
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
                    _note_gateways(r.json().get("gateways"))
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
    with _PORT_BRIDGES_LOCK:
        bridge = _port_bridges.pop(tid, None)
    if bridge:
        _shutdown_service_bridge(bridge["bridge"])
        try:
            _persist_port_bridges()
        except (OSError, ValueError):
            logging.error("could not persist service bridge cleanup")
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


def _supervise_tunnel(tid, name, host_port, vm_id, rp=None, registered=False, gw=(None, None)):
    """Keep an interactive rental's reverse tunnel alive (see _check_tunnels). Idempotent.
    `gw` is the rental's (gateway id, ssh target); (None, None) = the default gateway."""
    if tid in _tun_rentals:
        return
    _tun_rentals[tid] = {"name": name, "host_port": int(host_port), "vm_id": vm_id, "rp": rp,
                         "gw_id": gw[0] or _DEFAULT_GW_ID, "gw_target": gw[1],
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
            rp = _open_reverse_tunnel(s["host_port"], tid, prefer=s["rp"], gateway=s.get("gw_target"))
            if tid not in _tun_rentals:                       # torn down while we were dialing
                _kill_reverse_tunnel(tid)
                continue
            if rp and s["rp"] != rp:
                s["rp"] = rp
                _save_tunnel_ports()
            alive = bool(rp)
        if alive and (not s["vm_id"] or _register_vm_tunnel(s["vm_id"], s["rp"], ip_address="127.0.0.1",
                                                            gateway_id=s.get("gw_id"))):
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


def _start_storage_guard(tid, name, volume):
    def guard():
        while True:
            time.sleep(5)
            with _pb_vm_lock:
                if tid not in _pb_vm_watch:
                    return
            try:
                reason = template_storage.job_violation(volume)
            except Exception:
                continue  # A failed probe is not proof of a quota violation.
            if reason:
                report_log(tid, reason + "; stopping this rental")
                try:
                    _post_result_ack_retry(_signed_result(tid, status="failed", result=reason))
                finally:
                    _cleanup_job_resources(tid, name)
                return
    threading.Thread(target=guard, daemon=True, name=f"pb-storage-{tid}").start()


def _start_startup_script(tid, name, workdir, script):
    """Best-effort: a script that cannot start leaves the rental itself up and says why."""
    try:
        nb_fetch.start_startup_script(name, workdir, script)
    except Exception as e:  # noqa: BLE001
        report_log(tid, "startup script could not start: " + _launch_failure_reason(e))
        return
    report_log(tid, f"startup script started; output -> {workdir}/{nb_fetch.STARTUP_LOG}")
    threading.Thread(target=_watch_startup_script, args=(tid, name, workdir), daemon=True,
                     name=f"pb-startup-{tid}").start()


def _watch_startup_script(tid, name, workdir, every_s=15):
    """Report the exit code once: the buyer's timeline records it and the server stops the rental
    if the buyer asked. ponytail: an agent restart drops this watcher (the script keeps running, the
    exit is then not reported and no auto-stop happens: the buyer keeps the window they paid for);
    restore it in _restore_vm_watch if that matters."""
    while True:
        time.sleep(every_s)
        with _pb_vm_lock:
            if tid not in _pb_vm_watch:
                return                                 # the rental ended first
        try:
            code = nb_fetch.startup_exit_code(name, workdir)
        except Exception:  # noqa: BLE001 — a failed probe is not an exit
            continue
        if code is not None:
            report_log(tid, f"startup script exited with code {code}")
            _post("/jobs/startup_done", {"task_id": tid, "exit_code": min(code, 255)})
            return


def _unseal_secrets(task):
    """The rental's secrets, unsealed in RAM with this node's key. Errors never carry a value."""
    for _ in range(2 * HEARTBEAT_S):       # a just-restarted agent gets its key on the next heartbeat
        if _SEAL_AES is not None:
            break
        time.sleep(1)
    if _seal is None or _SEAL_AES is None:
        raise RuntimeError("rental secrets refused: this node has no seal key yet")
    try:
        data = json.loads(_seal.unseal(_SEAL_AES, task["secrets_sealed"], aad=str(task["task_id"]).encode()))
    except Exception:  # noqa: BLE001
        raise RuntimeError("rental secrets could not be unsealed on this node") from None
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and nb_fetch.SECRET_NAME.fullmatch(k) and isinstance(v, str)
            for k, v in data.items()):
        raise RuntimeError("rental secrets payload is malformed")
    return data


def _deliver_secrets(tid, name, secrets):
    try:
        nb_fetch.write_secrets(name, secrets)
    except Exception:  # noqa: BLE001 — docker's stderr could echo nothing useful; keep it value-free
        raise RuntimeError("rental secrets could not be written to /run/secrets") from None
    report_log(tid, f"{len(secrets)} secret(s) delivered to {nb_fetch.SECRETS_DIR} (in-memory tmpfs)")


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
    if _restore_volume(task.get("volume"), task.get("restore_from"), tid, task) is False:
        report_log(tid, "Host recovery refused: checkpoint could not be restored; saved work was not replaced with an empty workspace")
        _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "", "status": "failed"})
        _cleanup_job_resources(tid)
        _set_ui(status="idle", task=None, fail=True)
        return
    _backup_stop = _start_backup_thread(task)
    if task.get("vm_id"):        # register so the heartbeat can checkpoint this job on a preempt signal
        _LIVE_TEMPLATES[task["vm_id"]] = {"task": task, "volume": task.get("volume") or "task-data",
                                          "stop": _backup_stop}
    image = task.get("image"); port = task.get("port")
    # Ephemeral host port per rental so a second VM on this node doesn't collide on the container
    # port (the old fixed 0.0.0.0:port:port meant one interactive VM per node). The gateway reaches
    # the VM at node_ip:host_port — this is exactly what we register as the tunnel port below.
    host_port = _free_host_port() if port else None
    game_udp_host_port = None
    game_wg_ip = None
    if task.get("template") == "palworld":
        import ipaddress
        _game_addr = os.getenv("PB_EGRESS_ADDR", "").strip()
        try:
            game_wg_ip = str(ipaddress.ip_interface(_game_addr).ip)
        except ValueError:
            game_wg_ip = None
        try:
            import egress_vpn
            if not game_wg_ip or not egress_vpn.enabled() or not egress_vpn.ensure_tunnel():
                raise RuntimeError("Palworld needs the seller's enrolled WireGuard game tunnel")
            game_udp_host_port = _free_udp_host_port(game_wg_ip)
        except Exception as _e:
            report_log(tid, f"Palworld refused: public UDP forwarding is unavailable ({_e})")
            _post("/jobs/result", _signed_result(tid, status="failed", failure_cause="network_policy"))
            _set_ui(status="idle", task=None, fail=True)
            return
    params = task.get("params", {})
    report_progress(tid, 10, f"pulling {image}")
    import shutil, subprocess, uuid as _uuid
    if not shutil.which("docker"):
        _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                   "status": "failed"})
        return
    name = f"pb-{task.get('template')}-{_uuid.uuid4().hex[:8]}"
    _service_endpoints = []
    _service_bridge = None
    _health_port = host_port
    try:
        if task.get("public_services"):
            for item in task["public_services"]:
                kind = socket.SOCK_DGRAM if item["protocol"] == "udp" else socket.SOCK_STREAM
                with socket.socket(socket.AF_INET, kind) as listener:
                    listener.bind(("127.0.0.1", 0))
                    local_port = listener.getsockname()[1]
                _service_endpoints.append(dict(container_port=item["container_port"],
                                               protocol=item["protocol"], host_port=local_port))
            native = _native_bridge_options(_service_endpoints)
            _service_bridge = _start_service_bridge(_service_endpoints, task["service_credential"],
                                                    task.get("lease_generation", 0), native)
            if native:
                native["udp_port"] = _service_bridge.udp_port
            host_port = _service_bridge.port
            _health_port = next((item["host_port"] for item in _service_endpoints
                                 if item["container_port"] == port and item["protocol"] == "tcp"), None)
            with _PORT_BRIDGES_LOCK:
                _port_bridges[tid] = dict(bridge=_service_bridge,
                    config=dict(container=name, endpoints=_service_endpoints,
                                token=task["service_credential"], generation=task.get("lease_generation", 0),
                                native_udp=native or None))
    except Exception as error:
        if _service_bridge:
            _shutdown_service_bridge(_service_bridge)
        report_log(tid, f"template service bridge could not start: {_launch_failure_reason(error)}")
        _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "", "status": "failed"})
        try:
            _post_result_ack_retry(_signed_result(tid, status="failed", failure_cause="network_policy"))
        finally:
            _cleanup_job_resources(tid, name)
            _set_ui(status="idle", task=None, fail=True)
        return
    # SECURITY (tenant isolation): every job container is labelled with its task id so the
    # watchdog (and any operator) can find, stop and GC exactly this rental's resources, and so
    # a per-task volume/network is never confused with another tenant's.
    cmd = ["docker", "run", "--pull=never", "-d", "--name", name,
           "--label", f"pb.task={tid}", "--label", "pb.kind=template"]
    if task.get("health") and _health_port:
        cmd += ["--label", f"pb.health={task['health']}", "--label", f"pb.health_port={_health_port}"]
        if task.get("health_process") in {"plasmashell", "minecraft"}:
            cmd += ["--label", "pb.health_process=" + task["health_process"]]
    if _service_bridge:
        cmd += ["--label", "pb.service_bridge=1"]
    cmd += _interactive_labels(task)           # only an interactive rental is a reap candidate
    if task.get("vm_id") and host_port:        # lets a restarted agent re-open + re-register its tunnel
        cmd += ["--label", f"pb.vm_id={task['vm_id']}", "--label", f"pb.host_port={host_port}"]
        _lgw = _rental_gateway(task)
        if _lgw[0] != _DEFAULT_GW_ID:            # ...to the SAME gateway its address resolves to
            cmd += ["--label", f"pb.gateway={_lgw[0]}", "--label", f"pb.gateway_target={_lgw[1]}"]
    # Petabyte Spaces (persistent): restart the app in place on a crash/OOM — same container, same
    # host port, so the reverse tunnel stays valid. The watchdog already treats Docker's "restarting"
    # state as alive, so this needs no watchdog change; after 5 straight failures the container ends
    # "exited" and the watchdog fails the rental (a broken repo can't bill forever).
    if task.get("restart"):
        cmd += ["--restart", "on-failure:5"]
    # Reverse-tunnel mode binds the container to LOOPBACK (nothing public on this node); the gateway
    # reaches it via the outbound ssh -R opened after start. Otherwise publish on the public NIC.
    _rev = _reverse_tunnel_enabled() and bool(port)
    if _service_bridge:
        for endpoint in _service_endpoints:
            cmd += ["-p", f"127.0.0.1:{endpoint['host_port']}:{endpoint['container_port']}/{endpoint['protocol']}"]
    else:
        cmd += _publish_flags(port, host_port, bind="127.0.0.1" if _rev else None)
    if game_udp_host_port is not None:
        cmd += ["-p", f"{game_wg_ip}:{game_udp_host_port}:{port}/udp"]
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
        net, why = _ensure_job_network(
            tid, allowed_udp_port=(port if task.get("template") == "palworld" else None))
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
    _secrets = None
    try:
        try:
            if task.get("secrets_sealed"):        # fail the launch (refunded) rather than run without
                _secrets = _unseal_secrets(task)
                cmd += list(nb_fetch.SECRETS_TMPFS)
            if task.get("snapshot"):              # the buyer's own baked image, verified before load
                import image_snapshot
                image = image_snapshot.load(task["snapshot"], tid)
                outcome = "loaded from your snapshot (sha256 and image id verified)"
            else:
                outcome = template_storage.prepare(image, tid,
                    cached_only=bool(params.get("cached_image_only", False)),
                    timeout=params.get("max_startup_seconds", 900))
            report_log(tid, "Docker image " + outcome + "; model/work files are private to this rental")
            cmd += _template_env_flags(task, _template_env)
            cmd += [image]
            # a model delivered as a CLI arg (vllm --model, TGI --model-id, llama.cpp -hf)
            if task.get("model_arg") and model:
                cmd += [task["model_arg"], model]
            cmd += list(task.get("args") or [])        # extra image args / batch command
            # This launches the requested workload, so the argv intentionally includes values
            # returned by the trusted API. It is always an argv list (shell=False); the new host
            # bind address is parsed and normalized by ipaddress, its UDP port comes from bind(0),
            # and the container port is fixed by the curated Palworld template. No value becomes
            # shell syntax or a Docker option.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
            run = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if run.returncode:                         # keep Docker's stderr: it says WHY
                raise subprocess.CalledProcessError(run.returncode, cmd, run.stdout, run.stderr)
            cid = run.stdout.strip()
        finally:
            _remove_env_file(task)
        if _secrets is not None:                  # before the startup script, which may read them
            _deliver_secrets(tid, name, _secrets)
            _secrets = None
        _register_vm(tid, name)  # watchdog: detect if this container dies
        if _service_bridge:
            _persist_port_bridges()  # a restart must never rebind a different rental's service
        _start_storage_guard(tid, name, vol if task.get("cache") else None)
        if task.get("model_env") == "OLLAMA_MODEL" and model:
            if params.get("max_startup_seconds"):
                _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                           "status": "loading"})
            _start_ollama_pull(tid, name, model)       # the image never reads OLLAMA_MODEL
        if task.get("health") and _health_port:
            # A registered tunnel is not a working app: vLLM/llama.cpp download and load the model
            # AFTER this point, and a model too big for the GPU never serves. Tell the server the app
            # is loading; _await_ready reports 'ready' once the health endpoint answers. Billing and
            # the buyer's "Open" link wait for that.
            _post("/jobs/vm_details", {"task_id": tid, "vm_type": "template", "vm_id": "",
                                       "status": "loading"})
            if task.get("health_auth"):
                _start_ready_poll(tid, name, _health_port, task["health"], task["health_auth"], task.get("health_process"))
            else:
                _start_ready_poll(tid, name, _health_port, task["health"],
                                  process=task.get("health_process"))
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
        if params.get("startup_script"):
            _start_startup_script(tid, name, task.get("cache") or "/tmp", params["startup_script"])
        if task.get("template") == "finetune" and not _wait_for_template_port(_health_port, name):
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
        _gw = _rental_gateway(task)
        if _rev:
            _rp = _open_reverse_tunnel(host_port, tid, gateway=_gw[1])
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
            _ssh_start = task.get("template") == "custom" and task.get("ssh_start") is True
            if _inject_ssh_key(name, task.get("ssh_pubkey"), start_sshd=_ssh_start) == 3:
                report_log(tid, "SSH was requested but this image has no /usr/sbin/sshd: add "
                                "openssh-server to the image (the rental's other ports still run)")
            _registered = _register_vm_tunnel(
                vm_id, _hp, ip_address=(_node_ip if task.get("template") != "palworld" else None),
                lease_generation=task.get("lease_generation", 0),
                game_udp_host_port=game_udp_host_port, service_bridge=bool(_service_bridge),
                service_udp_port=_service_bridge.udp_port if _service_bridge else None,
                gateway_id=_gw[0])
            if _registered:
                report_log(tid, f"tunnel registered: vm {vm_id} -> {_node_ip or 'node'}:{_hp}")
            else:
                report_log(tid, f"tunnel registration failed for vm {vm_id}; VM may stay 'starting'"
                                + (" (retrying in the background)" if _rev else ""))
            if _rev:                                   # watch the ssh -R; re-open it if it drops
                _supervise_tunnel(tid, name, host_port, vm_id, rp=_hp, registered=_registered, gw=_gw)
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
    (Reported by a seller from nvidia-smi, 2026-09-23.) The context dies with the child.

    NVIDIA: the child is a container of the locally CACHED CUDA image the VRAM wipe already needs,
    so a host torch is no longer required (install.sh skipped its 2.8 GB download from 2026-10-04),
    and a newer torch's CUDA build never has to match the host driver. Host torch stays the fallback
    (and the AMD path); once the image has worked, an old install's host torch is removed. That
    leaves no host fallback by design: an image that cannot run GPU containers also fails the
    mandatory VRAM wipe, so the node refuses every GPU job until its image works again anyway."""
    import shutil, subprocess, sys
    try:
        n = int(n or os.getenv("BENCH_MATMUL_N", "8192"))
        iters = int(os.getenv("BENCH_MATMUL_ITERS", "30"))
        args = ["-c", _FP16_BENCH_PY, str(n), str(iters)]
        imgs = (_cached_cuda_images() if gpu_runtime.vendor() != "amd" and shutil.which("docker") else [])
        runs = [["docker", "run", "--rm", "--pull", "never", *gpu_runtime.docker_gpu_args(), "--network",
                 "none", "--label", "pb.kind=fp16-bench", img, "python", *args] for img in imgs]
        runs.append([sys.executable, *args])                 # host torch (AMD, or no usable image)
        dt = 0.0
        for cmd in runs:
            try:
                # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- fixed Python source, integer-only argv, a locally cached image, no shell.
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                dt = float(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else 0.0
            except Exception:                                # noqa: BLE001 - try the next way to run it
                dt = 0.0
            if dt > 0:
                if cmd[0] == "docker":
                    _drop_host_torch()
                break
        if dt <= 0:
            return None
        flops = 2.0 * (n ** 3) * iters          # 2*N^3 per matmul
        return (round(flops / dt / 1e12, 1), n, round(dt / iters, 6))   # (TFLOPS, N, s/matmul)
    except Exception:                            # noqa: BLE001 — never crash the agent
        return None


def _drop_host_torch():
    """After a benchmark ran in the cached image: uninstall the host torch an older install.sh put in
    this venv (torch, triton, nvidia-* CUDA wheels: several GB of a seller's disk). Checks what is
    installed each time (cheap; benchmarks are rare), so a reinstalled torch goes too and a failed
    uninstall is simply retried. Nothing else on the host imports it; the wipe, runtime check and
    diagnostics run in images."""
    import importlib.metadata as md
    import subprocess, sys
    try:
        names = {(d.metadata["Name"] or "") for d in md.distributions()}
        pkgs = sorted(n for n in names if n.lower() in ("torch", "triton")
                      or (n.lower().startswith("nvidia-") and n.lower() != "nvidia-ml-py"))
        if pkgs and subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "-q", *pkgs],
                                   capture_output=True, timeout=600).returncode == 0:
            logging.info(f"removed host torch ({len(pkgs)} packages): the benchmark runs in the cached image")
    except Exception:                            # noqa: BLE001 — disk cleanup is best-effort
        pass


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


def _run_template_probe(task):
    """Unpaid, bounded diagnostic using the same cleanup and GPU isolation as rentals."""
    import json
    import template_probe
    import execution_receipt
    tid = task["task_id"]
    _set_ui(status="running", task=f"Template check #{tid}")
    answer = template_probe.run(task["template_probe"], _run_docker, template_storage.prepare,
        _isolation_flags({"gpu": True, "memory": "4g", "cpus": 2, "pids": 256}),
        gpu_runtime.docker_gpu_args(), tid)
    result = json.dumps(answer, separators=(",", ":"))
    status = "completed" if answer.get("status") == "completed" else "failed"
    proof = execution_receipt.make(tid, result=result, status=status)
    response = httpx.post(f"{API_URL}/jobs/result", headers=HEADERS, timeout=20, trust_env=False,
        json={"task_id": tid, "status": status, "result": result,
              "proof": proof, "signature": crypto.sign_proof(proof)})
    _set_ui(status="idle", task=None, ok=response.is_success)


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
    if task.get("runtime_check"):
        import benchmark_runtime
        try:
            proof["runtime_check"] = benchmark_runtime.run(
                task["runtime_check"], _run_docker,
                _isolation_flags({"gpu": True, "memory": "4g", "cpus": 2, "pids": 256}),
                gpu_runtime.docker_gpu_args())
        except ValueError:
            proof["runtime_check"] = {"status": "failed"}
    import execution_receipt, hardware_evidence
    proof.update(execution_receipt.make(tid, result=None, output_hash=proof.get("output_hash")))
    proof["hardware_evidence"] = hardware_evidence.collect()
    response = httpx.post(f"{API_URL}/jobs/benchmark_result", headers=HEADERS, timeout=20, json={
        "spec_id": spec_id, "tokens_sec": tokens_sec,
        "meta": meta, "proof": proof, "signature": crypto.sign_proof(proof)}, trust_env=False)
    _set_ui(status="idle", task=None, ok=response.is_success)


def _run_docker(argv, timeout=None, *, capture_output=False, text=False, check=True):
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
        return _sp.run(argv, check=check, timeout=timeout, capture_output=capture_output, text=text)
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


_IMG_FORMATS = {"PNG": "PNG", "JPEG": "JPEG", "JPG": "JPEG", "EXR": "OPEN_EXR", "OPEN_EXR": "OPEN_EXR"}


def _render_setup_expr(samples=None, gpu=True, engine=None, file_format=None, resolution=None,
                       optix=True):
    """Blender `--python-expr`, run after the buyer's .blend loads and before `-a` renders it.

    0) `engine` (from _render_plan) switches the scene's engine first: 'CYCLES' for a Blender
       Internal file or a convert request, or 'EEVEE' to render EEVEE headless on the GPU. EEVEE
       resolves to the version's real id (BLENDER_EEVEE_NEXT on 4.2+, else BLENDER_EEVEE) so the
       same expr works across Blender versions. None keeps the file's own engine.
    1) Cycles defaults to the CPU and a headless container has no saved Blender preferences, so
       every "GPU render" used to run on the seller's CPU (2026-09-25: no Cycles device was ever
       chosen). Point Cycles at the GPU the container was given: OptiX, else CUDA/HIP/oneAPI.
       get_devices_for_type() also returns the CPU row, and a new device entry defaults to
       use=True, so enabling every row rendered hybrid CPU+GPU (#556) — enable only the GPUs.
       EEVEE renders on the GPU through its GL/EGL context automatically (no device table); the
       node only advertises headless EEVEE after a real self-test, so we do not CPU-guard it here.
    2) Apply the buyer's requested sample count (Cycles samples / EEVEE taa_render_samples).
    3) Output format (PNG default; a movie format is rendered as frames so a split range can be
       stitched) and an optional resolution override.
    It comes from OUR argv, not the scene: --disable-autoexec still blocks the .blend's own scripts.
    """
    n = int(samples) if samples else 0
    fmt = _IMG_FORMATS.get(str(file_format or "PNG").upper(), "PNG")
    res = ""
    if resolution and isinstance(resolution, (list, tuple)) and len(resolution) == 2:
        try:
            w, h = int(resolution[0]), int(resolution[1])
            if w > 0 and h > 0:
                res = (f"s.render.resolution_x = {w}\ns.render.resolution_y = {h}\n"
                       "s.render.resolution_percentage = 100\n")
        except (TypeError, ValueError):
            res = ""
    set_engine = ""
    if engine == "EEVEE":
        set_engine = (
            "eng = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}\n"
            "s.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in eng else 'BLENDER_EEVEE'\n"
            "print('PBENGINE=' + s.render.engine, flush=True)\n")
    elif engine:
        set_engine = f"s.render.engine = '{engine}'\n"
    return (
        "import bpy\n"
        "s = bpy.context.scene\n"
        + set_engine
        + f"s.render.image_settings.file_format = '{fmt}'\n"
        + res
        + "if s.render.engine == 'CYCLES':\n"
        f"    if {n} > 0:\n"
        f"        s.cycles.samples = {n}\n"
        f"    if {bool(gpu)}:\n"
        "        p = bpy.context.preferences.addons['cycles'].preferences\n"
        f"        for dt in {('OPTIX', 'CUDA', 'HIP', 'ONEAPI') if optix else ('CUDA', 'HIP', 'ONEAPI')}:\n"
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
        "            raise RuntimeError('No Cycles GPU device is available; refusing CPU fallback for the requested GPU render')\n"
        + ("elif s.render.engine in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'):\n"
           f"    if {n} > 0:\n"
           f"        s.eevee.taa_render_samples = {n}\n" if n > 0 else
           "elif s.render.engine in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'):\n    pass\n")
    )


def _relink_expr(search_dir):
    """Prefix for a project-folder render: relink every texture/library the .blend can't find, by
    file name, from the uploaded folder (Blender's File > External Data > Find Missing Files). Fixes
    absolute paths from the author's machine (C:\\Users\\...\\wood.jpg). Python 3.5-safe (2.79b)."""
    if not search_dir:
        return ""
    return ("import bpy\n"
            "try:\n"
            "    bpy.ops.file.find_missing_files(directory=%r)\n"
            "except Exception as e:\n"
            "    print('PBRELINK failed: %%s' %% e)\n") % search_dir


# Render inputs stream to disk; a project archive (scene + textures) may be large.
_SCENE_MAX = int(os.getenv("RENDER_INPUT_MAX_BYTES", str(4 * 1024 ** 3)))
_ZIP_MAX_FILES = 20000


def _zip_junk(name):
    return any(p.startswith(".") or p == "__MACOSX" for p in name.split("/"))


def _scene_in_zip(infos):
    """The archive's main .blend: the shallowest, then the largest (linked libraries sit deeper or
    are smaller). Mirrors lumaris_api/blend_inspect.scene_in_zip, so the check and the render agree."""
    blends = [i for i in infos if i.filename.replace("\\", "/").lower().endswith(".blend")
              and not _zip_junk(i.filename.replace("\\", "/"))]
    if not blends:
        return None
    return min(blends, key=lambda i: (i.filename.replace("\\", "/").count("/"), -i.file_size)).filename


def _unzip_scene(path, dest):
    """Extract an uploaded project archive into `dest`; return the main .blend's path inside it.
    Refuses absolute or `..` names and symlinks, skips macOS/hidden junk, and caps the file count and
    the bytes ACTUALLY written (a zip bomb's declared sizes can lie)."""
    import zipfile
    root = os.path.realpath(dest)
    os.makedirs(root, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > _ZIP_MAX_FILES:
            raise RuntimeError(f"The project archive has too many files ({_ZIP_MAX_FILES:,} max).")
        main = _scene_in_zip(infos)
        if not main:
            raise RuntimeError("No .blend file in the uploaded project archive.")
        budget = _SCENE_MAX
        for i in infos:
            name = i.filename.replace("\\", "/")
            parts = name.split("/")
            if (name.startswith("/") or ".." in parts or ":" in parts[0]
                    or (i.external_attr >> 16) & 0o170000 == 0o120000):
                raise RuntimeError(f"Unsafe path in the project archive: {i.filename[:120]}")
            if _zip_junk(name):
                continue
            target = os.path.realpath(os.path.join(root, name))
            if not target.startswith(root + os.sep):
                raise RuntimeError(f"Unsafe path in the project archive: {i.filename[:120]}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(i) as src, open(target, "wb") as dst:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    budget -= len(block)
                    if budget < 0:
                        raise RuntimeError("The project archive expands past the size limit.")
                    dst.write(block)
    return main.replace("\\", "/")


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


def _render_plan(file_version, engine="auto", blender_version="latest", detect=lambda: "", eevee_ok=False):
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
    engine = engine if engine in ("auto", "CYCLES", "BLENDER_RENDER", "EEVEE") else "auto"
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
    if engine == "EEVEE":
        if eevee_ok:
            return ("latest", "EEVEE", "Rendering with EEVEE on the GPU (headless).", None)
        return (None, None,
                "This node is not set up for headless EEVEE rendering. Your .blend was NOT changed "
                "— submit with engine=CYCLES for a fast GPU render, or retry to land on an "
                "EEVEE-capable node.", "unsupported_engine_eevee")
    found = detect() or ""
    if "EEVEE" in found.upper():
        if eevee_ok:
            return ("latest", "EEVEE",
                    "This scene uses EEVEE; rendering it with EEVEE on the GPU (headless).", None)
        msg = (f"This scene's render engine is {found}, which this node cannot render headless. "
               "Your .blend was NOT changed — set the engine to Cycles, submit with engine=CYCLES "
               "to convert it, or retry to land on an EEVEE-capable node.")
        return (None, None, msg, "unsupported_engine_eevee")
    return ("latest", None, None, None)


def _render_frame_files(directory, frame_start, frame_end):
    """Refuse an empty/partial or unsafe archive even when Blender returned zero."""
    from pathlib import Path
    import stat
    root = Path(directory)
    expected = {f"frame_{n:04d}.png" for n in range(int(frame_start), int(frame_end) + 1)}
    actual = {p.name for p in root.iterdir()}
    if actual != expected:
        raise RuntimeError(f"Render frame range incomplete: expected {len(expected)} PNG frames, found {len(actual)} files")
    files = []
    for name in sorted(expected):
        path = root / name
        if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size < 45:
            raise RuntimeError(f"Render output is not a regular complete PNG: {name}")
        with path.open('rb') as f:
            header = f.read(24)
            f.seek(-12, 2)
            end = f.read()
        if (header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR'
                or not int.from_bytes(header[16:20], 'big') or not int.from_bytes(header[20:24], 'big')
                or end != b'\x00\x00\x00\x00IEND\xaeB`\x82'):
            raise RuntimeError(f"Render output has invalid or truncated PNG structure: {name}")
        files.append(path)
    return files


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
    mount, blend_in, search = ["-v", f"{scene}:/scene.blend:ro"], "/scene.blend", None
    out_dir = _os.path.join(work, "out"); _os.makedirs(out_dir, exist_ok=True)
    _os.chmod(out_dir, 0o777)   # forced non-root container writes frames; the 0700 parent tmpdir
    # keeps this world-writable leaf unreachable by any other host account
    try:
        # 1) pull the scene via a pre-signed GET (no standing creds on the node)
        g = httpx.post(f"{API_URL}/jobs/input_url", headers=HEADERS, timeout=15,
                       json={"task_id": tid, "ref": task.get("blend_ref", "")}, trust_env=False).json()
        with open(scene, "wb") as fh:            # streamed to disk: a project archive can be large
            safe_fetch.get(g["download_url"], timeout=1800, max_bytes=_SCENE_MAX, sink=fh)
        with open(scene, "rb") as fh:
            is_zip = fh.read(4) == b"PK\x03\x04"
        if is_zip:
            # A project folder (scene + textures), zipped by /studio: render the main .blend in place
            # and relink any texture it can't find from the folder, by file name.
            pdir = _os.path.join(work, "scene")
            main = _unzip_scene(scene, pdir)
            _os.unlink(scene)
            scene = _os.path.join(pdir, main)
            mount, blend_in, search = ["-v", f"{pdir}:/scene:ro"], "/scene/" + main, "/scene"
            report_log(tid, f"project folder: rendering {main}; missing textures are relinked from the folder")
        report_progress(tid, 15, f"scene fetched; rendering {fs}-{fe} in {image}")

        # 1b) Detect the scene's render engine BEFORE committing GPU time. EEVEE needs an EGL GPU
        # context; the node renders EEVEE headless on the GPU ONLY if its EEVEE self-test passed
        # (_eevee_ready) — otherwise EEVEE is refused with a note to switch to Cycles, so a scene
        # never silently falls back to slow software rendering. Best-effort — on any detection error
        # we fall through and just render.
        def _detect_engine():
            try:
                _dcmd = ["docker", "run", "--rm", "--network", "none"]
                _dcmd += _isolation_flags(task)
                _dcmd += [*mount, *ep, "-b", blend_in,
                          "--disable-autoexec", "--python-expr",
                          "import bpy;print('PBENGINE='+bpy.context.scene.render.engine)"]
                _d = _run_docker(_dcmd, capture_output=True, text=True, timeout=120, check=False)
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
            task.get("blender_version") or "latest", detect=_detect_engine,
            eevee_ok=(_EEVEE["ok"] is True))   # cached: the background probe ran the real self-test;
            # the server only routes an EEVEE job to a node that already advertised the capability.
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
        cmd += [*mount, "-v", f"{out_dir}:/out"]
        if runner == "2.79":
            # Blender Internal is CPU-only: no GPU flags. The host's verified 2.79b tree is mounted
            # read-only and run in the same image (it ships the X/GL libs 2.79 links against).
            report_progress(tid, 18, "rendering as authored with Blender 2.79b (CPU)")
            cmd += ["-v", f"{_ensure_blender_279()}:/opt/blender-2.79b:ro",
                    "--entrypoint", "/opt/blender-2.79b/blender", image,
                    "-b", blend_in, "--disable-autoexec",
                    "--python-expr", _relink_expr(search) + _render_setup_expr_279(set_engine)]
        else:
            if task.get("gpu"):
                cmd += [*gpu_runtime.docker_gpu_args()]
                if set_engine == "EEVEE":
                    cmd += gpu_runtime.graphics_env()   # EEVEE needs the driver's EGL/Vulkan libraries
            # EEVEE renders on the backend whose self-test passed on this node (Vulkan or OpenGL)
            _be = (["--gpu-backend", _EEVEE["backend"]]
                   if set_engine == "EEVEE" and _EEVEE.get("backend") else [])
            cmd += [*ep, "-b", *_be, blend_in, "--disable-autoexec",
                    "--python-exit-code", "86",
                    "--python-expr", _relink_expr(search) + _render_setup_expr(task.get("samples"), gpu=bool(task.get("gpu")),
                                                        engine=set_engine, file_format=task.get("format"),
                                                        resolution=task.get("resolution"))]
        cmd += ["-o", "/out/frame_", "-s", str(fs), "-e", str(fe), "-j", "1", "-a"]
        # Hard-kill the container at the buyer's AUTHORIZED runtime budget (audit H1): a render
        # can't consume more of the seller's GPU than the buyer paid to authorize (_run_docker
        # force-removes the container when the client-side timeout fires).
        _rt = task.get("max_runtime_s")
        _t0 = time.monotonic()
        try:
            _run_docker(cmd, timeout=(int(_rt) if _rt else None), capture_output=True, text=True)
        except subprocess.CalledProcessError as _ce:
            # Blender exited non-zero. Surface WHY instead of a generic failure (2026-10-06), and log
            # its raw output: the mapped reason is only a summary, and once hid the real error.
            _out = (_ce.stderr or "") + (_ce.stdout or "")
            report_log(tid, "Blender output (last lines):\n" + _out.strip()[-1500:])
            _left = (int(_rt) - (time.monotonic() - _t0)) if _rt else None
            # OptiX can fail where plain CUDA works on the same card (driver/OptiX mismatch): retry
            # once on CUDA within the remaining authorized runtime instead of failing the buyer.
            if "PBDEVICE=OPTIX" not in _out or (_left is not None and _left < 60):
                raise RuntimeError(_blender_fail_reason(_out)) from _ce
            report_log(tid, "OptiX failed on this node; retrying the render on CUDA")
            cmd[cmd.index("--python-expr") + 1] = _relink_expr(search) + _render_setup_expr(
                task.get("samples"), gpu=True, engine=set_engine, file_format=task.get("format"),
                resolution=task.get("resolution"), optix=False)
            try:
                _run_docker(cmd, timeout=(int(_left) if _left is not None else None),
                            capture_output=True, text=True)
            except subprocess.CalledProcessError as _ce2:
                _out = (_ce2.stderr or "") + (_ce2.stdout or "")
                report_log(tid, "Blender output on CUDA (last lines):\n" + _out.strip()[-1500:])
                raise RuntimeError(_blender_fail_reason(_out)) from _ce2
        _render_frame_files(out_dir, fs, fe)
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
        uploaded = httpx.put(grant["upload_url"], content=raw, timeout=600, trust_env=False)
        uploaded.raise_for_status()   # storage rejection is failure, never a completed result
        _post("/jobs/result", _signed_result(tid, status="completed",
                                             result=grant["ref"],   # clean s3 URI -> /jobs/output_url
                                             content_hash=hashlib.sha256(raw).hexdigest()))
        _set_ui(status="idle", task=None, ok=True)
    except Exception as e:                              # noqa: BLE001
        reason = str(e)[:600] or "the render did not finish on this node"
        report_log(tid, f"render failed: {reason}")
        _post("/jobs/result", _signed_result(tid, status="failed", result=reason))
        _set_ui(status="idle", task=None, fail=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        _eevee_probe_bg()        # the render image is cached now: test EEVEE if boot couldn't


def _blender_fail_reason(output: str) -> str:
    """Map Blender's output to a plain-English reason the buyer can act on, else the raw tail.
    The buyer is never charged for a failed render (see the render except); this only explains it."""
    # Our PBDEVICE= marker and Cycles' "Writing constant memory" status line appear in EVERY GPU
    # render, so they must never decide the reason (2026-10-07: every OptiX failure on the RTX 2060
    # read as "out of GPU memory" for a scene that peaks at ~150 MB).
    output = "\n".join(ln for ln in (output or "").splitlines() if not ln.startswith("PBDEVICE="))
    low = output.lower()
    tail = output.strip()[-400:]
    if "out of memory" in low or "out of gpu memory" in low:
        return ("The scene ran out of GPU memory on this node. Try a GPU with more VRAM, lower the "
                "resolution/samples, or render fewer frames at a time. You were not charged.")
    if "no cycles gpu device" in low or any(
            ("optix" in ln or "cuda" in ln) and ("error" in ln or "fail" in ln or "insufficient" in ln)
            for ln in low.splitlines()):
        return ("This node's GPU could not run the Cycles render (driver/OptiX/CUDA). The job is "
                "retried on another node; you were not charged for this attempt.")
    if ("no such file" in low or "unable to open" in low or "cannot read" in low
            or "version" in low and "unsupported" in low or "malformed" in low):
        return ("The render Blender could not open this .blend (it may be too old or corrupt). "
                "Re-save it in a recent Blender and re-submit. You were not charged.")
    if "eevee" in low or "egl" in low:
        return ("EEVEE could not start a GPU context on this node. Re-submit with engine=CYCLES "
                "(\"Use recommended settings\" in Studio) for a GPU render. You were not charged.")
    return ("The render did not finish on this node (you were not charged). Blender reported:\n"
            + (tail or "no output")) if tail else "The render did not finish on this node (you were not charged)."


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
_CURRENT_TASK = {"id": None}      # the job job_loop is executing right now
_pb_vm_lock = _pb_thr.Lock()
_pb_vm_started = {"on": False}


def _live_task_ids():
    """Every task this agent runs right now: the job in hand plus watched rentals (the only work
    that outlives job_loop's call). The server fails a claimed task missing from this list after a
    grace (db.reap_unreported_tasks), so it must never under-report."""
    with _pb_vm_lock:
        ids = {int(t) for t in _pb_vm_watch}
    if _CURRENT_TASK["id"] is not None:
        ids.add(int(_CURRENT_TASK["id"]))
    return sorted(ids)


def _register_vm(task_id, name):
    """Track a launched job container so the watchdog can report its exit."""
    with _pb_vm_lock:
        _pb_vm_watch[task_id] = {"name": name, "reported": False}
        if not _pb_vm_started["on"]:
            _pb_vm_started["on"] = True
            _pb_thr.Thread(target=_pb_vm_watchdog, name="pb-vm-watchdog", daemon=True).start()


# Crypto mining is not allowed in rentals (2026-10-01: a free-credit buyer ran WildRig on a rented
# RTX 2060 behind an encoded pool on a bare AWS IP, invisible to the gateway's pool-domain denylist).
# Detect it by WHAT RUNS in the rental: a known miner binary or a stratum pool URL on its command line.
# ponytail: name/URL match only; a renamed binary with its pool in a config file slips through.
# Names: the owner's mining-reference-lists (2026-10-01, 50 Windows executables -> Linux names, .exe
# optional) plus earlier ones. Bare "miner" (GMiner's miner.exe) is left out on purpose: too generic
# to kill a rental over; such a miner still trips the stratum-URL check.
_MINER_RE = re.compile(
    r"(?i)(?:^|[/\s])(?:wildrig\S*|xmrig\S*|srbminer\S*|t-?rex|teamredminer|lolminer|nbminer|bzminer|rigel"
    r"|nanominer|phoenixminer|cryptodredge|bminer|tt-?miner|tbminer|ccminer|sgminer|ethminer|minerd"
    r"|cpuminer\S*|urx-isotope-cpuminer\S*|cgminer|bfgminer|excavator|xmr-stak\S*|miniz|verthashminer"
    r"|gminer|onezerominer|kawpowminer|z-enemy|nheqminer|peakminer\S*)(?:\.exe)?(?:\s|$)|stratum\d?\+(?:tcp|ssl|tls)://")
_MINER_CHECK_S = 60


def _ps_args(ps_out):
    """Command lines from `docker top <c> -eo pid,args`: header skipped, PID column dropped."""
    return [p[1].strip() for p in (ln.split(None, 1) for ln in (ps_out or "").splitlines()[1:]) if len(p) == 2]


def _miner_hit(ps_out):
    """The first process command line that is a crypto miner, else None."""
    for line in _ps_args(ps_out):
        if _MINER_RE.search(line):
            return line[:200]
    return None


def _rental_miner(tid, name):
    """Throttled (per rental, once a minute) check of a live rental's processes for a miner."""
    now = time.time()
    with _pb_vm_lock:
        w = _pb_vm_watch.get(tid)
        if not w or now - w.get("miner_checked", 0) < _MINER_CHECK_S:
            return None
        w["miner_checked"] = now
    try:
        # The pid column is required: without it dockerd answers "Couldn't find PID field in ps output"
        # and the check silently never ran (2026-10-01..03, a miner pinned a 2060 for hours unseen).
        r = subprocess.run(["docker", "top", name, "-eo", "pid,args"], capture_output=True, text=True, timeout=10)
    except Exception:                                    # noqa: BLE001 — docker hiccup: next minute
        return None
    if r.returncode != 0:
        return None
    with _pb_vm_lock:
        if tid in _pb_vm_watch:                          # the admin workload view (heartbeat)
            _pb_vm_watch[tid]["procs"] = _proc_names(r.stdout)
    return _miner_hit(r.stdout)


def _proc_names(ps_out, limit=8):
    """Program names only (argv[0] basename) — never arguments, which can carry the buyer's secrets."""
    seen = []
    for line in _ps_args(ps_out):
        base = os.path.basename(line.split()[0])[:40] if line.split() else ""
        if base and base not in seen:
            seen.append(base)
    return seen[:limit]


def _gpu_usage():
    """Per-GPU utilization/power/VRAM/temperature from nvidia-smi, or None (no NVIDIA / no driver)."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,memory.used,temperature.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
    except Exception:                                    # noqa: BLE001
        return None
    gpus = []
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        f = [x.strip() for x in line.split(",")]
        try:
            gpus.append({"util": int(float(f[0])), "power_w": round(float(f[1])),
                         "mem_mb": int(float(f[2])), "temp_c": int(float(f[3]))})
        except (ValueError, IndexError):
            continue
    return gpus or None


def _workload_report():
    """What each live rental is running, for the admin workload view; None when nothing is rented."""
    with _pb_vm_lock:
        rentals = [{"task_id": tid, "procs": list(w.get("procs") or [])}
                   for tid, w in _pb_vm_watch.items() if not w.get("reported") and not w.get("fail")]
    return {"gpus": _gpu_usage(), "rentals": rentals} if rentals else None


def _pb_vm_scan():
    """One sweep: report any tracked container that has exited (once)."""
    import subprocess as _sp
    with _pb_vm_lock:
        items = [(tid, d["name"], d.get("fail")) for tid, d in _pb_vm_watch.items() if not d["reported"]]
    for tid, name, fail in items:
        if not fail:
            hit = _rental_miner(tid, name)
            if hit:
                report_log(tid, "stopped: crypto mining is not allowed on Petabyte rentals "
                                f"(process: {hit})")
                try:
                    _sp.run(["docker", "kill", name], capture_output=True, timeout=20)
                except Exception:                        # noqa: BLE001 — cleanup below removes it
                    pass
                fail = "crypto_mining"
                with _pb_vm_lock:
                    if tid in _pb_vm_watch:
                        _pb_vm_watch[tid]["fail"] = fail   # retried sweeps keep the cause
        if fail:                                # the agent gave up on it (e.g. its tunnel is lost)
            status, code = fail, 1
        else:
            try:
                r = _sp.run(["docker", "inspect", "-f", "{{.State.Status}}:{{.State.ExitCode}}", name],
                            capture_output=True, text=True, timeout=10)
            except Exception:
                continue  # docker hiccup — try again next sweep
            if r.returncode != 0:
                if "No such" not in (r.stderr or ""):
                    continue                        # daemon down (host shutting down), not gone
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


def _boot_time():
    """This host's boot time (epoch seconds) from /proc/stat, or None."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _restart_host_stopped(container, task_id):
    """Start a rental container again if the HOST stopped it, not the buyer's app.

    A seller rebooting mid-rental stops every container: Docker records exit 255 when its daemon
    died under a running container, otherwise the stop signal's code with FinishedAt before this
    boot. Booking 320 (2026-10-02): the watchdog then reported container_exited_exit_255 as a job
    failure and deleted the container AND its workspace volume, though the rental was still paid.
    `docker start` brings back the same container — writable layer, volume and host port binding
    intact — so the buyer's machine resumes. One attempt only: if it exits again, the watchdog
    reports it as before. Returns True when it was restarted."""
    try:
        r = subprocess.run(["docker", "inspect", "-f",
                            "{{.State.Status}}|{{.State.ExitCode}}|{{.State.FinishedAt}}|"
                            "{{.HostConfig.NetworkMode}}", container],
                           capture_output=True, text=True, timeout=10, check=False)
        status, code, finished, net = ((r.stdout or "").strip().split("|") + [""] * 4)[:4]
        if r.returncode or status != "exited":
            return False
        boot = _boot_time()
        try:
            from datetime import datetime, timezone
            fin = datetime.strptime(finished[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            fin = None
        if code != "255" and not (boot and fin and fin < boot):
            return False                         # it stopped while this boot was up: a real exit
        if net == f"pb-net-t{task_id}":
            # The reboot wiped the job bridge's firewall/egress rules; re-apply them before the
            # container can run again, and leave it stopped (watchdog reports it) if that fails.
            import network_policy
            network_policy.ensure(int(task_id))
        ok = subprocess.run(["docker", "start", container], capture_output=True,
                            timeout=60, check=False).returncode == 0
    except Exception as e:                       # noqa: BLE001 — the watchdog handles it as before
        logging.warning(f"rental {task_id}: could not restart after host stop: {e}")
        return False
    if ok:
        report_log(int(task_id), f"host restarted; rental container {container} started again "
                                 "with its workspace intact")
    return ok


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
                _restart_host_stopped(container, task_id)    # before the watchdog can see "exited"
                _register_vm(int(task_id), container)
                hl = subprocess.run(["docker", "inspect", "-f",
                                     "|".join('{{index .Config.Labels "%s"}}' % k for k in
                                              ("pb.health", "pb.health_port", "pb.vm_id", "pb.host_port", "pb.service_bridge", "pb.health_process",
                                               "pb.gateway", "pb.gateway_target")),
                                     container], capture_output=True, text=True, timeout=10, check=False)
                (path, hp, vm_id, tun_hp, bridge_flag, health_process, gw_id,
                 gw_target) = ((hl.stdout or "").strip().split("|") + [""] * 8)[:8]
                if bridge_flag == "1":
                    restored = _restore_port_bridge(int(task_id), container)
                    if restored is None:
                        with _pb_vm_lock:
                            _pb_vm_watch[int(task_id)]["fail"] = "service_bridge_state_lost"
                        continue
                    tun_hp = str(restored)
                if path.startswith("/") and hp.isdigit():
                    if health_process == "plasmashell":
                        info = subprocess.run(["docker", "inspect", container], capture_output=True,
                                              text=True, timeout=10, check=True)
                        env = dict(item.split("=", 1) for item in json.loads(info.stdout)[0]["Config"]["Env"] if "=" in item)
                        if env.get("CUSTOM_USER") != "petabyte" or not env.get("PASSWORD"):
                            with _pb_vm_lock:
                                _pb_vm_watch[int(task_id)]["fail"] = "desktop_auth_state_lost"
                            continue
                        _start_ready_poll(int(task_id), container, int(hp), path,
                                          ["petabyte", env["PASSWORD"]], "plasmashell")
                    else:
                        _start_ready_poll(int(task_id), container, int(hp), path,
                                          process=health_process or None)
                if tun_hp.isdigit() and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", vm_id):
                    _supervise_tunnel(int(task_id), container, int(tun_hp), vm_id,
                                      rp=saved.get(int(task_id)),
                                      gw=(_rental_gateway({"tunnel_gateway": {"id": gw_id, "target": gw_target}})
                                          if gw_id else (None, None)))
                else:
                    logging.warning(f"rental {task_id}: no pb.vm_id/pb.host_port label (started by "
                                    "an older agent); its reverse tunnel cannot be restored")
    except (OSError, subprocess.TimeoutExpired):
        logging.warning("Could not restore rental watchdog; Docker is unavailable")


def job_loop():
    _restore_vm_watch()
    while True:
        try:
            with _CLAIM_LOCK:
                _fix_pending = _fixes.pending()
                if not _fix_pending:
                    _JOB_RUNNING.set()          # claim window: the heartbeat won't queue a fix now
            if _fix_pending:                     # a support fix is queued/running: no new jobs
                time.sleep(POLL_S)
                continue
            if _self_update_holds_claims():      # updating to a newer signed agent: no new jobs
                time.sleep(POLL_S)
                continue
            if _draining():                      # an opt-in driver update waits for this node to idle
                time.sleep(POLL_S)
                continue
            if _wipe_image_pending():            # mandatory-wipe image still downloading: no new jobs
                time.sleep(30)                   # the pull takes minutes; no need to poll faster
                continue
            # spec_id pins the claim to THIS machine: one account's machines (or every JIT standby
            # droplet, which share the operator account) must never run each other's jobs.
            r = httpx.get(f"{API_URL}/jobs/next", headers=HEADERS, params={"spec_id": SPEC_ID},
                          timeout=20, trust_env=False)
            if r.status_code == 204:
                pass                 # no job available right now
            elif r.status_code == 200:
                task = r.json()
                _CURRENT_TASK["id"] = task.get("task_id")
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
                inference_worker.controller.before_work()   # a rental always gets the GPU first
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
                        elif tt == "template_probe":
                            _run_template_probe(task)
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
            _CURRENT_TASK["id"] = None
            _JOB_RUNNING.clear()
            inference_worker.controller.after_work()
        time.sleep(POLL_S)


def _remove_legacy_miner():
    """Idle mining is gone (2026-10-06). An agent updated from a version that had it may still
    hold its miner container: remove only those labelled containers, once, at startup."""
    import subprocess
    try:
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", "label=market.petabyte.idle-miner=1"],
                             capture_output=True, text=True, timeout=15, check=False).stdout.split()
        for cid in ids:
            if re.fullmatch(r"[0-9a-f]{12,64}", cid):
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30, check=False)
    except Exception:                                    # noqa: BLE001 — never block startup
        pass


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
            template_storage.prepare(img, 0, timeout=3600)
            logging.warning(f"VRAM-wipe image {img}: cached")
        except Exception as e:                           # noqa: BLE001 — never crash the agent
            logging.warning(f"VRAM-wipe image pull failed: {e}")
    threading.Thread(target=_pull, daemon=True, name="pb-wipe-image").start()


def run_agent():
    # Telemetry first — degrade-safe: if the collector is unreachable the agent still runs.
    _tel.init(agent_id=SPEC_ID, seller_id=os.getenv("PROVIDER"))
    _tel.event(_tel.EVENTS.STARTUP, message="agent started", api_url=API_URL, spec_id=SPEC_ID)
    try:                                   # the egress gateway the API last moved this node to
        import egress_vpn
        egress_vpn.load_gateway_override()
    except Exception as e:                 # noqa: BLE001 - egress stays on agent.env's gateway
        logging.warning(f"egress gateway override not applied: {e}")
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
    _remove_legacy_miner()
    # The inference worker publishes through this node's own reverse tunnel (default gateway).
    inference_worker.controller.attach(
        open_tunnel=lambda port: _open_reverse_tunnel(port, "inference"),
        close_tunnel=lambda: _kill_reverse_tunnel("inference"),
        tunnel_alive=lambda: bool(_tunnels.get("inference")) and _tunnels["inference"][1].poll() is None)
    try:
        inference_worker.stop_container()        # a server left behind by a previous agent run
    except Exception:                            # noqa: BLE001 — never block startup
        pass
    _restore_vm_watch()  # recover detached rentals before the first heartbeat offers this node
    try:
        _gpu_startup_selftest()          # before the first heartbeat can offer this node
    except Exception:                    # noqa: BLE001 — never block startup
        pass
    _probe_job_network()                 # the first heartbeat already says whether apps can run here
    threading.Thread(target=_job_network_loop, daemon=True, name="pb-jobnet-probe").start()
    # Headless-EEVEE capability: a real EEVEE GPU render in the background (only runs when the render
    # image is cached), so the node advertises EEVEE only where it actually works. Never blocks boot.
    _eevee_probe_bg()
    threading.Thread(target=heartbeat_loop, daemon=True).start()   # online while we wait
    _TUN_SETTLED.wait(timeout=240)       # don't claim a serving job this node can't publish yet
    job_loop()


if __name__ == "__main__":
    run_agent()
