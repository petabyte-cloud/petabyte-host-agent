"""premiere_adapter.py — Premiere Pro via the official UXP API (host app premierepro >= 25.6).

Python runs a loopback-only bridge (127.0.0.1:47811, per-run token the plugin reads from its own
PluginData folder) that the trusted UXP plugin (windows_guest/uxp/) polls for one verb at a time:
inspect (Project.open -> sequences) or render (EncoderManager.exportSequence). "Accepted/queued"
is never success: the plugin reports the export promise (+ COMPLETE event when events exist), then
Python waits for the output file to be stable and ffprobes it (presets.deliver).
The .prproj (gzip XML) is also parsed here defensively for Dynamic Link / MOGRT / media references."""
import glob
import gzip
import hmac
import http.server
import json
import os
import re
import secrets
import threading
import time
from fractions import Fraction

import adobe_contract as C
import presets
from common import JobFailure, log

PORT = 47811
TICKS_PER_SECOND = 254016000000
PRPROJ_MAX = 256 << 20
PLUGIN_ID = "market.petabyte.render"
HANDSHAKE_S = 240
_PATH_TAGS = {"filepath", "actualmediafilepath", "relativepath", "originalpath"}


class PrprojError(ValueError):
    pass


# ---------------------------------------------------------------- pure: project XML
def analyze_prproj_xml(data: bytes) -> dict:
    """{dynamic_link, mogrt, media: [referenced paths]} from the (decompressed) project XML."""
    import xml.etree.ElementTree as ET           # stdlib; DOCTYPE/ENTITY rejected before parsing
    if len(data) > PRPROJ_MAX:
        raise PrprojError("project file is too large")
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", data, re.I):
        raise PrprojError("project XML declares a DOCTYPE/ENTITY, which is not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise PrprojError(f"project XML could not be parsed: {str(e)[:80]}")
    out = {"dynamic_link": False, "mogrt": False, "media": []}
    seen = set()
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if "dynamiclink" in tag.lower() or tag in ("AEComp", "AECompositionLink"):
            out["dynamic_link"] = True
        text = (el.text or "").strip()
        if not text or len(text) > 1024 or tag.lower() not in _PATH_TAGS:
            continue
        low = text.lower()
        if low.endswith((".aep", ".aepx")):
            out["dynamic_link"] = True
        if low.endswith(".mogrt"):
            out["mogrt"] = True
        if low not in seen and len(seen) < 5000:
            seen.add(low)
            out["media"].append(text)
    low_all = data.lower()
    if b"dynamiclink" in low_all or b"aecomp" in low_all:
        out["dynamic_link"] = True
    if b".mogrt" in low_all:
        out["mogrt"] = True
    return out


def parse_prproj(path) -> dict:
    with open(path, "rb") as f:
        head = f.read(2)
    try:
        if head == b"\x1f\x8b":
            with gzip.open(path, "rb") as g:
                data = g.read(PRPROJ_MAX + 1)
        else:
            with open(path, "rb") as f:
                data = f.read(PRPROJ_MAX + 1)
    except (OSError, EOFError) as e:
        raise PrprojError(f"project file could not be read ({type(e).__name__})")
    return analyze_prproj_xml(data)


def missing_media(media, files) -> list:
    """Referenced media not present in the extracted bundle (by relative path or basename)."""
    rel = {f.casefold() for f in files}
    base = {f.rsplit("/", 1)[-1].casefold() for f in files}
    out = []
    for p in media:
        q = p.replace("\\", "/")
        if q.casefold().endswith((".aep", ".aepx")):
            continue                                       # reported as Dynamic Link instead
        if q.lstrip("./").casefold() in rel or q.rsplit("/", 1)[-1].casefold() in base:
            continue
        out.append(q.rsplit("/", 1)[-1][:120])
    return sorted(set(out))[:200]


def report_from_plugin(res, static, files, profile) -> dict:
    """Shape the plugin's inspect result + static XML facts into a contract report."""
    sels, lims = [], []
    seqs = res.get("sequences") if isinstance(res, dict) and isinstance(res.get("sequences"), list) else []
    for s in seqs[:C.MAX_SELECTORS]:
        if not isinstance(s, dict):
            continue
        try:
            tb = int(str(s.get("timebase_ticks")))
            fps = Fraction(TICKS_PER_SECOND, tb)
            frames = round(int(str(s.get("end_ticks"))) / tb)
        except (TypeError, ValueError, ZeroDivisionError):
            fps, frames = None, None
        if s.get("has_audio") is None:
            lims.append(f"audio presence unknown for sequence '{str(s.get('name'))[:60]}'")
        sels.append({"id": str(s.get("guid", "")).strip("{}"), "name": s.get("name"), "width": s.get("width"),
                     "height": s.get("height"), "pixel_aspect": 1.0, "fps_num": fps.numerator if fps else None,
                     "fps_den": fps.denominator if fps else None, "duration_frames": frames,
                     "has_audio": s.get("has_audio") is True, "alpha_required": False, "bit_depth": 8, "hdr": False})
    offline = [x for x in (res.get("offline") or []) if isinstance(x, str)] if isinstance(res, dict) else []
    lims += [x for x in (res.get("limitations") or []) if isinstance(x, str)] if isinstance(res, dict) else []
    lims.append(C.ALPHA_NOT_DETECTED)
    if isinstance(res, dict) and res.get("events") is False:
        lims.append("encoder events unavailable in this host; completion relies on file stability")
    deps = [q.rsplit("/", 1)[-1][:120] for q in (p.replace("\\", "/") for p in static["media"])
            if q.lower().endswith((".aep", ".aepx", ".mogrt"))]
    if static["dynamic_link"] and not profile.get("dynamic_link_qualified"):
        lims.append("Dynamic Link is not qualified on this worker")
    return {"app": C.PREMIERE, "app_version": str(profile.get("premiere_version", "")), "selectors": sels,
            "missing_media": sorted(set(missing_media(static["media"], files) + offline))[:200],
            "missing_fonts": [], "missing_effects": [], "dependencies": sorted(set(deps))[:200],
            "warnings": [], "errors": [], "limitations": lims, "dynamic_link": static["dynamic_link"],
            "mogrt": static["mogrt"]}


# ---------------------------------------------------------------- plugin bridge
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if hmac.compare_digest(self.headers.get("X-PB-Token", ""), self.server.session.token):
            return True
        self._reply(403, {})
        return False

    def do_GET(self):
        if not self._auth():
            return
        s = self.server.session
        with s.lock:
            job = s.pending
        if self.path != "/job":
            self._reply(404, {})
        elif job:
            self._reply(200, job)
        else:
            self._reply(204)

    def do_POST(self):
        if not self._auth():
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            return self._reply(413, {})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._reply(400, {})
        s = self.server.session
        if self.path == "/hello":
            s.plugin_info = body if isinstance(body, dict) else {}
            s.hello.set()
        elif self.path == "/progress":
            s.on_progress(body)
        elif self.path == "/log":
            log("plugin: " + C._clip(str(body.get("message", "")) if isinstance(body, dict) else "", 300))
        elif self.path == "/result":
            with s.lock:
                if isinstance(body, dict) and s.pending and body.get("id") == s.pending["id"]:
                    s.result, s.pending = body, None
                    s.done.set()
        else:
            return self._reply(404, {})
        s.job.touch()
        self._reply(200, {})


def token_dirs(profile) -> list:
    if profile.get("uxp_plugin_data"):
        return [profile["uxp_plugin_data"]]
    pid = profile.get("uxp_plugin_id", PLUGIN_ID)
    return glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Adobe", "UXP", "PluginsStorage", "PPRO", "*",
                                  "*", pid, "PluginData"))


class _Session:
    """One Premiere process + bridge server for the lifetime of a job."""

    def __init__(self, job):
        self.job, self.token = job, secrets.token_hex(16)
        self.lock, self.hello, self.done = threading.Lock(), threading.Event(), threading.Event()
        self.pending = self.result = None
        self.plugin_info, self._seq, self._progress_cb = {}, 0, None
        dirs = token_dirs(job.profile)
        if not dirs:
            raise JobFailure("app_crashed", "UXP plugin data folder not found; image profile needs uxp_plugin_data")
        for d in dirs:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "pb_token.txt"), "w", encoding="ascii") as f:
                f.write(self.token)
        try:
            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
        except OSError as e:
            raise JobFailure("app_crashed", f"plugin bridge port busy: {e.strerror}")
        self.server.session = self
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.proc = job.spawn([job.profile["premiere"]])
        deadline = time.monotonic() + min(HANDSHAKE_S, max(1, job.remaining()))
        try:
            while not self.hello.wait(0.25):
                job.flush()
                if self.proc.poll() is not None:
                    raise JobFailure("app_crashed", f"Premiere Pro exited ({self.proc.poll()}) before the plugin connected")
                if time.monotonic() > deadline:
                    raise JobFailure("app_crashed", "Premiere Pro UXP plugin did not connect (autoload not working?)")
        except BaseException:
            self.close()
            raise
        log(f"plugin connected: {C._clip(json.dumps(self.plugin_info), 200)}")

    def on_progress(self, body):
        pct = body.get("percent") if isinstance(body, dict) else None
        self.job.progress(percent=pct if isinstance(pct, (int, float)) and 0 <= pct <= 100 else None,
                          message="exporting")

    def call(self, payload) -> dict:
        self._seq += 1
        with self.lock:
            self.pending, self.result = {"id": self._seq, **payload}, None
            self.done.clear()
        try:
            while not self.done.wait(0.25):
                self.job.flush()
                self.job.check()
                if self.proc.poll() is not None:
                    raise JobFailure("app_crashed", f"Premiere Pro exited ({self.proc.poll()}) during {payload['verb']}")
        finally:
            with self.lock:
                self.pending = None
        return self.result

    def close(self):
        try:
            self.proc.kill()
        finally:
            self.server.shutdown()
            self.server.server_close()


def _wait_stable(path, job, checks=3, interval=1.0):
    last, same = -1, 0
    while same < checks:
        job.check()
        job.flush()
        size = os.path.getsize(path) if os.path.isfile(path) else -1
        same = same + 1 if size == last and size > 0 else 0
        last = size
        time.sleep(interval)
    return last


def _ok(res, cause):
    if not isinstance(res, dict) or not res.get("ok"):
        err = str((res or {}).get("error", "no result"))[:300] if isinstance(res, dict) else "no result"
        raise JobFailure(cause, err)
    return res.get("result") or {}


# ---------------------------------------------------------------- adapter
def _static(job):
    try:
        return parse_prproj(job.project_abs)
    except PrprojError as e:
        raise JobFailure("project_open_failed", str(e))


def inspect(job) -> dict:
    static = _static(job)
    sess = _Session(job)
    try:
        res = _ok(sess.call({"verb": "inspect", "project": job.project_abs}), "project_open_failed")
    finally:
        sess.close()
    return report_from_plugin(res, static, job.files, job.profile)


def render(job):
    m, prof = job.manifest, job.profile
    static = _static(job)
    if (static["dynamic_link"] or static["mogrt"]) and not prof.get("dynamic_link_qualified"):
        raise JobFailure("unsupported_dependency", "Dynamic Link / MOGRT dependencies are not qualified on this worker")
    epr = (prof.get("premiere_presets") or {}).get(m["preset_id"])
    if not epr:
        raise JobFailure("bad_task", "preset is not provisioned on this worker image")
    sess = _Session(job)
    try:
        res = _ok(sess.call({"verb": "inspect", "project": job.project_abs}), "project_open_failed")
        rep = C.validate_report({**report_from_plugin(res, static, job.files, prof), "project_path": m["project_path"]})
        if rep["missing_media"]:
            raise JobFailure("missing_media", "; ".join(rep["missing_media"][:5]))
        sel = m["selector"]
        hit = next((s for s in rep["selectors"] if s["id"] == sel["id"]), None)
        if hit is None or hit["name"] != sel["name"]:
            raise JobFailure("selector_not_found", "the selected sequence is not in this project (renamed or removed?)")
        if hit["ambiguous_name"]:
            log(f"note: several sequences named '{sel['name']}'; selecting by GUID")
        if m["range"] and hit["duration_frames"] and m["range"]["end"] >= hit["duration_frames"]:
            raise JobFailure("bad_task", "range ends after the last frame")
        out = os.path.join(job.ctl_dir, "ppro_export.mp4")
        if os.path.exists(out):
            os.remove(out)
        job.stage("exporting")
        res = _ok(sess.call({"verb": "render", "project": job.project_abs, "guid": sel["id"], "output": out,
                             "preset": epr, "range": m["range"]}), "export_failed")
        if res.get("status") not in ("resolved", "complete"):
            raise JobFailure("export_failed", f"export ended with status {str(res.get('status'))[:40]}")
        if _wait_stable(out, job) <= 0:
            raise JobFailure("export_failed", "Premiere Pro produced no output file")
    finally:
        sess.close()
    return presets.deliver(job, out)
