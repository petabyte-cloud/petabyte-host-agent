"""runner.py — in-guest entry point for one Adobe attempt (fresh EC2 Windows GPU instance, interactive
session; the control plane launches the instance and terminates it after /done).

    python runner.py [--profile C:\\ProgramData\\Petabyte\\guest-profile.json] [--dev]

Config comes from EC2 user data via IMDSv2 only (contract PROTOCOL 2): JSON {"pb_relay", "pb_token",
"pb_attempt"}, validated by adobe_contract.check_user_data. PB_RELAY/PB_TOKEN env overrides are
honoured only with --dev (tests; they also allow http://127.0.0.1). Tool paths come only from the
trusted image profile, never from the manifest or the bundle. Flow:

    GET {base}/manifest -> validate (contract) -> GET {base}/input_url -> stream the bundle from the
    presigned URL (no bearer, size cap, sha256) -> safe extract -> adapter inspect/render (events,
    deadline + no-progress watchdog, process-tree kill) -> POST {base}/output_url -> presigned PUT
    (report JSON | final MKV) -> VRAM scrub -> POST {base}/done (+ "uploaded")
"""
import argparse
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import adobe_contract as C
import ae_adapter
import bundle
import common
import premiere_adapter
import vram_scrub
from common import CHUNK, JobFailure, log

PROFILE_PATH = r"C:\ProgramData\Petabyte\guest-profile.json"
# Pinned Mozilla root bundle installed by the image build (versions.json ca_bundle). Embedded Python
# would otherwise trust only roots already in the Windows store, which a fresh image with Windows
# Update cut off often lacks (it never fetches missing roots on demand the way CryptoAPI does).
CA_BUNDLE = r"C:\ProgramData\Petabyte\cacert.pem"
WIN = os.name == "nt"
HTTP_TIMEOUT = 30
IMDS_BASE = "http://169.254.169.254"
IMDS_TIMEOUT = 2
IMDS_WAIT_S = 120              # user data can lag the first seconds after boot
EVENT_INTERVAL = 0.5           # <= 2 progress events / s, coalesced
DONE_RETRIES = 12              # backoff 1,2,4,..,30s: ~4 min of trying before giving up
ADAPTERS = {C.AE: ae_adapter, C.PREMIERE: premiere_adapter}
_EOF = object()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):      # 3xx surfaces as HTTPError instead of being followed
        return None


def _tls():
    return ssl.create_default_context(cafile=CA_BUNDLE) if os.path.exists(CA_BUNDLE) else ssl.create_default_context()


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                       urllib.request.HTTPSHandler(context=_tls())).open


# ---------------------------------------------------------------- config: EC2 user data over IMDSv2
def read_user_data(imds_base=IMDS_BASE, *, wait_s=IMDS_WAIT_S, pause=3.0) -> bytes:
    """PUT /latest/api/token, then GET /latest/user-data with it. Retries until wait_s is used up."""
    op, deadline, last = _opener(), time.monotonic() + wait_s, None
    while True:
        try:
            tok = op(urllib.request.Request(imds_base + "/latest/api/token", data=b"", method="PUT",
                                            headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}),
                     timeout=IMDS_TIMEOUT).read(512).decode("ascii").strip()
            with op(urllib.request.Request(imds_base + "/latest/user-data",
                                           headers={"X-aws-ec2-metadata-token": tok}), timeout=IMDS_TIMEOUT) as r:
                return r.read(C.USER_DATA_MAX_BYTES + 1)
        except (urllib.error.URLError, OSError, UnicodeDecodeError) as e:
            last = e
        if time.monotonic() >= deadline:
            raise SystemExit(f"user data not delivered via IMDSv2: {type(last).__name__}")
        time.sleep(pause)


def discover(dev: bool, imds_base=IMDS_BASE, **imds_kw) -> dict:
    """-> {"relay", "token", "attempt"}; --dev + PB_RELAY/PB_TOKEN env skips IMDS (tests only)."""
    if dev and os.environ.get("PB_RELAY"):
        raw = json.dumps({"pb_relay": os.environ["PB_RELAY"], "pb_token": os.environ.get("PB_TOKEN", ""),
                          "pb_attempt": os.environ.get("PB_ATTEMPT", "dev")}).encode()
    else:
        raw = read_user_data(imds_base, **imds_kw)
    try:
        return C.check_user_data(raw, dev=dev)
    except C.ContractError as e:
        raise SystemExit(f"user data rejected: {e}")


def check_storage_url(url, dev=False) -> str:
    """A presigned URL the API handed us: https (http loopback with --dev), no userinfo."""
    u = urlsplit(url) if isinstance(url, str) and len(url) <= 8192 else None
    if not u or not u.hostname or "@" in u.netloc or not (
            u.scheme == "https" or (dev and u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))):
        raise JobFailure("bad_task", "storage URL from the API is not acceptable")
    return url


# ---------------------------------------------------------------- API client (bearer) + storage (no bearer)
class Api:
    def __init__(self, base, token, dev=False):
        self.base, self._auth, self.dev = base.rstrip("/"), "Bearer " + token, dev
        self._open, self._storage = _opener(), _opener()

    def call(self, method, path, obj=None, *, retries=3, limit=1 << 20) -> dict:
        """JSON in/out, bearer attached. 5xx/connection errors retry with backoff; 3xx/4xx do not."""
        body = json.dumps(obj, separators=(",", ":")).encode() if obj is not None else None
        hdrs = {"Authorization": self._auth, "Accept": "application/json"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        last = None
        for i in range(retries):
            try:
                req = urllib.request.Request(self.base + path, data=body, method=method, headers=hdrs)
                with self._open(req, timeout=HTTP_TIMEOUT) as r:
                    data = r.read(limit + 1)
                if len(data) > limit:
                    raise JobFailure("bad_task", f"{path} response too large")
                return json.loads(data) if data.strip() else {}
            except urllib.error.HTTPError as e:
                last = e
                if e.code < 500:
                    break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = e
            if i + 1 < retries:
                time.sleep(min(30, 2 ** i))
        log(f"{method} {path} failed: {common.redact(last)}")
        raise last

    def post_quiet(self, path, obj) -> bool:
        try:
            self.call("POST", path, obj, retries=1)
            return True
        except Exception:                                   # noqa: BLE001 — events are best effort
            return False

    def download(self, dest, cap, job):
        """GET {base}/input_url, then stream the bundle from the presigned URL (no bearer).
        Returns (sha256, size). Enforces cap and the job deadline."""
        info = self.call("GET", "/input_url")
        url = check_storage_url(info.get("url"), self.dev)
        if info.get("size") != job.manifest["input"]["size"]:
            raise JobFailure("bad_task", "input_url size disagrees with the manifest")
        h, size = hashlib.sha256(), 0
        with self._storage(urllib.request.Request(url), timeout=HTTP_TIMEOUT) as r, open(dest, "wb") as out:
            while chunk := r.read(CHUNK):
                size += len(chunk)
                if size > cap:
                    raise JobFailure("bundle_rejected", "upload is larger than the manifest allows")
                h.update(chunk)
                out.write(chunk)
                job.touch()
                if job.remaining() <= 0:
                    raise JobFailure("timeout", "deadline reached while fetching input")
        return h.hexdigest(), size

    def upload(self, kind, fpath) -> dict:
        """POST {base}/output_url, then PUT the file to the presigned URL with exactly the returned
        headers + Content-Length (streamed, never chunked, no bearer). Returns done.uploaded."""
        size, sha = os.path.getsize(fpath), common.sha256_file(fpath)
        want = C.validate_upload_request({"kind": kind, "bytes": size, "sha256": sha})
        try:
            grant = self.call("POST", "/output_url", want)
        except (urllib.error.URLError, OSError) as e:
            raise JobFailure("result_upload_failed", f"output_url failed: {type(e).__name__}")
        url, hdrs = check_storage_url(grant.get("url"), self.dev), grant.get("headers")
        if not isinstance(hdrs, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                             or k.lower() in ("authorization", "content-length", "transfer-encoding", "host")
                                             for k, v in hdrs.items()):
            raise JobFailure("bad_task", "output_url returned unusable headers")
        try:
            with open(fpath, "rb") as f:
                req = urllib.request.Request(url, data=f, method="PUT", headers={**hdrs, "Content-Length": str(size)})
                with self._storage(req, timeout=HTTP_TIMEOUT) as r:
                    r.read()
        except (urllib.error.URLError, OSError) as e:
            raise JobFailure("result_upload_failed", f"{kind} upload failed: {type(e).__name__}")
        return want


# ---------------------------------------------------------------- manifest
def validate_manifest(m) -> dict:
    try:
        return _validate_manifest(m)
    except C.ContractError as e:
        raise JobFailure("bad_task", str(e))


def _validate_manifest(m):
    if not isinstance(m, dict) or m.get("protocol") != C.PROTOCOL:
        raise C.ContractError("unsupported manifest protocol")
    mode, app = m.get("mode"), m.get("app")
    if mode not in ("render", "inspect", "scrub"):
        raise C.ContractError("bad mode")
    d = m.get("deadline_s")
    if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 7 * 86400:
        raise C.ContractError("bad deadline")
    aid = m.get("attempt_id")
    if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", aid):
        raise C.ContractError("bad attempt id")
    lim = m.get("limits") if isinstance(m.get("limits"), dict) else {}
    out = {"protocol": C.PROTOCOL, "attempt_id": aid, "mode": mode, "app": app, "deadline_s": d,
           "limits": {k: min(v, lim[k]) if isinstance(lim.get(k), int) and lim[k] > 0 else v
                      for k, v in C.LIMITS.items()}}
    if mode == "scrub":
        return out
    if app not in C.APPS:
        raise C.ContractError("bad app")
    out["project_path"] = C.check_rel_path(m.get("project_path"), ext=C.PROJECT_EXT[app])
    inp = m.get("input") if isinstance(m.get("input"), dict) else {}
    size, sha = inp.get("size"), inp.get("sha256")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= out["limits"]["max_bundle_bytes"]:
        raise C.ContractError("bad input size")
    if sha is not None and not C.is_hex64(sha):
        raise C.ContractError("bad input digest")
    out["input"] = {"size": size, "sha256": sha}
    if mode == "render":
        if sha is None:
            raise C.ContractError("render requires the inspected input digest")
        out["selector"] = C.check_selector(m.get("selector"))
        C.check_preset(m.get("preset_id"), app)
        out["preset_id"] = m["preset_id"]
        exp = m.get("expected") if isinstance(m.get("expected"), dict) else {}
        out["expected"] = {}
        for k, hi in (("width", 16384), ("height", 16384), ("fps_num", 10**6), ("fps_den", 10**6),
                      ("duration_frames", 10**8)):
            v = exp.get(k)
            out["expected"][k] = v if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= hi else None
        out["expected"]["has_audio"] = exp.get("has_audio") is True
        out["range"] = C.check_range(m.get("range"), total_frames=out["expected"]["duration_frames"])
        out["output_name"] = C.check_output_name(m.get("output_name"))
    return out


# ---------------------------------------------------------------- processes (tree-killable)
def _job_object(popen):
    """Windows Job Object with KILL_ON_JOB_CLOSE wrapping `popen`; children inherit it."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    h = k32.CreateJobObjectW(None, None)
    info = EXT()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info))
    if not k32.AssignProcessToJobObject(h, ctypes.c_void_p(int(popen._handle))):
        log(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
    return h


class Proc:
    """A child whose whole process tree can be killed; stdout lines arrive on .q (then _EOF)."""

    def __init__(self, argv, *, cwd=None, env=None):
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd,
                  env=env, text=True, encoding="utf-8", errors="replace")
        if WIN:
            kw["creationflags"] = 0x08000000                  # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        self.p = subprocess.Popen(argv, **kw)
        self.h = _job_object(self.p) if WIN else None
        self.q = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            self.q.put(line.rstrip("\r\n"))
        self.q.put(_EOF)

    def poll(self):
        return self.p.poll()

    def kill(self):
        try:
            if WIN:
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                k32.CloseHandle.argtypes = [ctypes.c_void_p]
                k32.TerminateJobObject(self.h, 1)
                k32.CloseHandle(self.h)
            else:
                os.killpg(self.p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            self.p.kill()
        except OSError:
            pass
        self.p.wait(timeout=30)


# ---------------------------------------------------------------- the job
class Job:
    def __init__(self, api, manifest, profile, workspace):
        self.api, self.manifest, self.profile = api, manifest, profile
        self.limits = manifest["limits"]
        self.bundle_dir, self.ctl_dir = os.path.join(workspace, "bundle"), os.path.join(workspace, "ctl")
        self.deadline = time.monotonic() + manifest["deadline_s"]
        self.last_activity = time.monotonic()
        self.files, self.project_abs = [], None
        self._last_sent, self._pending = 0.0, None

    def remaining(self):
        return self.deadline - time.monotonic()

    def touch(self):
        self.last_activity = time.monotonic()

    def check(self):
        if self.remaining() <= 0:
            raise JobFailure("timeout", f"deadline of {self.manifest['deadline_s']}s reached")
        if time.monotonic() - self.last_activity > self.limits["no_progress_s"]:
            raise JobFailure("no_progress", f"no progress for {self.limits['no_progress_s']}s")

    # events: stage/log go out at once; progress is coalesced to <= 1 per EVENT_INTERVAL
    def stage(self, s):
        self.flush(force=True)
        self.touch()
        log(f"stage {s}")
        self._send({"type": "stage", "stage": s})

    def progress(self, *, frame=None, total_frames=None, percent=None, message=""):
        self.touch()
        self._pending = {"type": "progress", "frame": frame, "total_frames": total_frames,
                         "percent": percent, "message": message}
        self.flush()

    def flush(self, force=False):
        if self._pending and (force or time.monotonic() - self._last_sent >= EVENT_INTERVAL):
            ev, self._pending = self._pending, None
            self._send(ev)

    def _send(self, ev):
        try:
            ev = C.validate_event(ev)
        except C.ContractError as e:
            log(f"dropping malformed event: {e}")
            return
        if len(json.dumps(ev, separators=(",", ":")).encode()) > C.MAX_EVENT_BYTES:
            log("dropping oversized event")
            return
        self._last_sent = time.monotonic()
        self.api.post_quiet("/event", ev)

    # processes
    def spawn(self, argv, **kw) -> Proc:
        log(("spawn " + os.path.basename(argv[0]) + " " + " ".join(argv[1:]))[:300])
        return Proc(argv, **kw)

    def run(self, argv, *, on_line=None, timeout=None, keep=4000, **kw):
        """Run to completion under the deadline / no-progress watchdog. Returns (rc, last lines)."""
        p, lines, t0 = self.spawn(argv, **kw), [], time.monotonic()
        try:
            while True:
                try:
                    line = p.q.get(timeout=0.25)
                except queue.Empty:
                    line = None
                if line is _EOF:
                    break
                if line is not None:
                    lines.append(line)
                    del lines[:-keep]
                    if on_line:
                        on_line(line)
                self.flush()
                self.check()
                if timeout and time.monotonic() - t0 > timeout:
                    raise JobFailure("timeout", f"{os.path.basename(argv[0])} exceeded {int(timeout)}s")
            return p.p.wait(), lines
        except BaseException:
            p.kill()
            raise


# ---------------------------------------------------------------- orchestration
def run_job(job: Job):
    """-> (output metadata | None, done.uploaded)."""
    m, api = job.manifest, job.api
    job.stage("fetching_input")
    src = os.path.join(job.ctl_dir, "input.bin")
    try:
        sha, size = api.download(src, min(job.limits["max_bundle_bytes"], m["input"]["size"]), job)
    except (urllib.error.URLError, OSError) as e:
        raise JobFailure("input_fetch_failed", f"input download failed: {type(e).__name__}")
    if size != m["input"]["size"]:
        raise JobFailure("input_fetch_failed", f"input is {size} bytes, manifest says {m['input']['size']}")
    if m["mode"] == "render" and sha != m["input"]["sha256"]:
        raise JobFailure("input_digest_mismatch", "the uploaded bundle changed since it was inspected")
    try:
        job.files = bundle.extract(src, job.bundle_dir, m["project_path"], limits=job.limits)
    except bundle.BundleError as e:
        raise JobFailure("bundle_rejected", str(e))
    os.remove(src)
    job.project_abs = os.path.join(job.bundle_dir, *m["project_path"].split("/"))
    adapter = ADAPTERS[m["app"]]
    if m["mode"] == "inspect":
        job.stage("inspecting")
        rep = dict(adapter.inspect(job))
        rep.update(app=m["app"], project_path=m["project_path"], input_sha256=sha)
        rpath = os.path.join(job.ctl_dir, "report.json")
        with open(rpath, "wb") as f:
            f.write(fit_report(C.validate_report(rep)))
        job.stage("uploading_result")
        return None, api.upload("report", rpath)
    path, md = adapter.render(job)
    if os.path.getsize(path) > job.limits["max_output_bytes"]:
        raise JobFailure("output_too_large", "rendered output exceeds the size limit")
    job.stage("uploading_result")
    return md, api.upload("output", path)


def fit_report(rep: dict) -> bytes:
    """Serialize a validated report under MAX_REPORT_BYTES, trimming lists if needed."""
    lists = ("selectors", "missing_media", "missing_fonts", "missing_effects", "dependencies",
             "warnings", "errors", "limitations")
    body = json.dumps(rep, separators=(",", ":")).encode()
    trimmed = False
    while len(body) > C.MAX_REPORT_BYTES:
        for k in lists:
            rep[k] = rep[k][:max(1, len(rep[k]) * 3 // 4)]
        if not trimmed:
            rep["limitations"].append("report truncated to fit the size limit")
            trimmed = True
        body = json.dumps(rep, separators=(",", ":")).encode()
    return body


def reset_dirs(workspace):
    for d in ("bundle", "ctl"):
        p = os.path.join(workspace, d)
        shutil.rmtree(p, ignore_errors=True)
        os.makedirs(p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=PROFILE_PATH)
    ap.add_argument("--dev", action="store_true", help="allow PB_RELAY/PB_TOKEN env + http loopback (tests only)")
    a = ap.parse_args(argv)
    log(f"runner start (protocol {C.PROTOCOL})")
    try:
        with open(a.profile, encoding="utf-8") as f:
            profile = json.load(f)
        cfg = discover(a.dev)
    except BaseException:                                       # noqa: BLE001 — pythonw has no console:
        log("startup failed: " + traceback.format_exc())       # the log file is the only trace
        raise
    common._SECRETS.append(cfg["token"])
    log(f"config ok: relay {urlsplit(cfg['relay']).hostname} attempt {cfg['attempt']}")
    api = Api(cfg["relay"], cfg["token"], a.dev)
    done, job = {"status": "failed", "failure_cause": "guest_crashed", "message": ""}, None
    try:
        manifest = validate_manifest(api.call("GET", "/manifest"))
        log(f"attempt {manifest['attempt_id']} (user data {cfg['attempt']}) mode={manifest['mode']} "
            f"app={manifest.get('app')}")
        md = uploaded = None
        if manifest["mode"] != "scrub":                         # scrub: no workspace, no events, just done
            job = Job(api, manifest, profile, profile["workspace"])
            reset_dirs(profile["workspace"])
            md, uploaded = run_job(job)
        done = {"status": "completed", "message": "ok"}
        if md:
            done["output"] = md
        if uploaded:
            done["uploaded"] = uploaded
    except JobFailure as e:
        log(f"failed {e.cause}: {e}")
        done = {"status": "failed", "failure_cause": e.cause, "message": common.redact(e)[:1000]}
    except BaseException as e:                                  # noqa: BLE001 — must still POST /done
        log(traceback.format_exc())
        done = {"status": "failed", "failure_cause": "guest_crashed",
                "message": common.redact(f"{type(e).__name__}: {e}")[:1000]}
    finally:
        if job:
            try:
                job.stage("cleaning_up")
            except Exception:                                   # noqa: BLE001
                pass
        done["vram_scrub"] = vram_scrub.scrub()
        log(f"vram scrub: {done['vram_scrub']}")
        done = C.validate_done(done)
        try:
            api.call("POST", "/done", done, retries=DONE_RETRIES)
        except Exception:                                       # noqa: BLE001 — logged by call()
            pass
    return 0 if done["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
