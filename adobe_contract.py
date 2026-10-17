"""adobe_contract.py — the ONE contract shared by the control plane (lumaris_api) and the in-guest
runner (windows_guest/) that runs on a fresh first-party EC2 Windows GPU instance per attempt.

Pure stdlib, no I/O, importable on Linux (API) and inside the Windows guest. Everything a buyer
can influence passes through a validator here before it reaches a process argument, a file path
or a script.

Task description the control plane turns into a guest manifest (build_guest_manifest):

    {"app": "after_effects"|"premiere_pro", "mode": "render"|"inspect"|"scrub",
     "input_sha256": "<hex64>"|None, "project_path": str,
     "selector": {"id": str, "name": str}|None, "preset_id": str|None,
     "range": {"start": int, "end": int}|None, "expected": {...}|None, "output_name": str}

PROTOCOL 2 — guest <-> API over HTTPS. The instance's EC2 user data (read through IMDSv2 only) is
JSON <= 4 KB: {"pb_relay": "https://<host>/adobe/guest/v1", "pb_token": "<64 lowercase hex>",
"pb_attempt": "<[A-Za-z0-9._-]{1,80}>"}. Every API call below is relative to pb_relay, carries
`Authorization: Bearer <pb_token>`, uses JSON bodies and is never redirected (3xx = error).
Storage transfers go to presigned URLs WITHOUT the bearer token.

    GET  {base}/manifest    -> guest manifest (build_guest_manifest); input = {"size", "sha256"|null}
    GET  {base}/input_url   -> {"url": <presigned https GET>, "size": int}; the guest streams the
                               bundle from url (size cap), computes sha256; render mode must match
                               manifest input.sha256 (input_digest_mismatch)
    POST {base}/event       -> one event (validate_event), <= MAX_EVENT_BYTES, coalesced <= 2/s
    POST {base}/output_url  -> body {"kind": "output"|"report", "bytes": int, "sha256": hex64}
                               -> {"url": <presigned https PUT>, "headers": {str: str}}; the guest
                               PUTs the file to url with exactly those headers + Content-Length,
                               streamed, no chunked encoding. "report" = inspection report JSON
                               (<= MAX_REPORT_BYTES, validate_report); "output" = the final MKV
                               (<= limits.max_output_bytes)
    POST {base}/done        -> terminal status (validate_done) plus, when status is "completed" and
                               something was uploaded, "uploaded": {"kind", "sha256", "bytes"} naming
                               the object the server should finalize. Retried until 2xx; the first
                               done wins server-side. Scrub mode: VRAM scrub then done, no uploads.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

PROTOCOL = 2
RUNTIME = "windows-adobe-vm/1"
USER_DATA_MAX_BYTES = 4096
GUEST_PATH = "/adobe/guest/v1"                # pb_relay must end with this
UPLOAD_KINDS = ("output", "report")

AE, PREMIERE = "after_effects", "premiere_pro"
APPS = (AE, PREMIERE)
INSPECT_KIND = "adobe_inspect"
RENDER_KINDS = (AE, PREMIERE)                 # task_type == app for render tasks
ALL_KINDS = RENDER_KINDS + (INSPECT_KIND,)
PROJECT_EXT = {AE: ".aep", PREMIERE: ".prproj"}

# Buyer-visible stages; they map onto the existing task status + progress_msg, not a new FSM.
STAGES = ("queued", "preparing_windows", "fetching_input", "inspecting", "rendering", "exporting",
          "encoding", "validating", "uploading_result", "cleaning_up")

# failure_cause values (<=64 chars, /jobs/result). "infra" causes mean the VM never reached the
# point where buyer work ran: the server refunds those instead of billing.
INFRA_FAILURES = frozenset({
    "adobe_runtime_unavailable", "adobe_profile_mismatch", "vm_boot_failed", "guest_not_ready",
    "gpu_not_ready", "network_policy_unverified", "host_quarantined", "input_fetch_failed",
    "result_upload_failed",     # the host could not store the validated output: nothing delivered
})
JOB_FAILURES = frozenset({
    "bad_task", "bundle_rejected", "input_digest_mismatch", "project_open_failed",
    "selector_not_found", "selector_ambiguous", "missing_media", "missing_fonts",
    "missing_effects", "unsupported_dependency", "app_version_mismatch", "render_failed",
    "export_failed", "encode_failed", "output_invalid", "output_too_large", "timeout",
    "no_progress", "cancelled", "guest_crashed", "app_crashed",
})
FAILURE_CAUSES = INFRA_FAILURES | JOB_FAILURES
# Causes the guest itself may report in /done: job failures plus the two transfers it now performs.
GUEST_FAILURES = JOB_FAILURES | {"input_fetch_failed", "result_upload_failed"}

# Approved output presets. Container choice is separate from codecs; the guest maps a preset id to
# its own pinned ffmpeg arguments and Adobe templates (windows_guest/presets.py). A preset never
# carries alpha/HDR: projects that need either are rejected at preflight, not silently flattened.
PRESETS = {
    "mkv_h264_aac": {
        "label": "MKV — H.264 video + AAC audio (8-bit 4:2:0, SDR, opaque)",
        "container": "mkv", "video_codec": "h264", "audio_codec": "aac",
        "bit_depth": 8, "alpha": False, "hdr": False, "apps": (AE, PREMIERE),
    },
}
# The inspectors do not yet detect a transparency requirement (alpha_required is always False), so
# every report carries this limitation instead of implying the alpha check ran.
ALPHA_NOT_DETECTED = "Transparency (alpha) needs are not detected; the MKV output is opaque."

# Hard limits (also enforced by the guest extractor on the bytes it actually writes).
LIMITS = {
    "max_bundle_bytes": 8 * 1024**3,           # compressed upload (= the agent's safe_fetch sink cap)
    "max_expanded_bytes": 60 * 1024**3,
    "max_files": 20000,
    "max_file_bytes": 16 * 1024**3,
    "max_path_depth": 24,
    "max_path_chars": 240,                     # under MAX_PATH once rooted at the guest workspace
    "max_extract_s": 1800,
    "max_output_bytes": 50 * 1024**3,
    "no_progress_s": 900,
}
MAX_EVENT_BYTES = 16 * 1024
MAX_REPORT_BYTES = 256 * 1024
MAX_SELECTORS = 500
EVENT_TYPES = ("stage", "progress", "log")

_HEX64 = re.compile(r"[0-9a-f]{64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_WIN_RESERVED = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(\..*)?", re.I)
_BAD_CHARS = set('<>:"|?*\\') | {chr(c) for c in range(32)}


class ContractError(ValueError):
    """A value failed contract validation. str(e) is safe to show the buyer."""


def is_hex64(s) -> bool:
    return isinstance(s, str) and bool(_HEX64.fullmatch(s))


def is_digest(s) -> bool:
    return isinstance(s, str) and bool(_DIGEST.fullmatch(s))


def is_profile_id(s) -> bool:
    return isinstance(s, str) and bool(_PROFILE.fullmatch(s))


def check_rel_path(path, *, ext=None) -> str:
    """A project-relative path that is safe on Windows and cannot leave the workspace.

    Returns the normalized forward-slash form. Rejects absolute/UNC/device/drive paths, '..',
    alternate data streams (':'), reserved device names, trailing dots/spaces, control chars."""
    if not isinstance(path, str) or not path or len(path) > LIMITS["max_path_chars"]:
        raise ContractError("project path is missing or too long")
    p = path.replace("\\", "/")
    if p.startswith("/") or p.startswith("//") or re.match(r"^[A-Za-z]:", p):
        raise ContractError("project path must be relative to the uploaded bundle")
    parts = p.split("/")
    if len(parts) > LIMITS["max_path_depth"]:
        raise ContractError("project path is nested too deeply")
    for seg in parts:
        if seg in ("", ".", ".."):
            raise ContractError("project path has an empty, '.' or '..' segment")
        if any(ch in _BAD_CHARS for ch in seg):
            raise ContractError("project path has a character Windows does not allow")
        if seg[-1] in ". " or _WIN_RESERVED.fullmatch(seg):
            raise ContractError("project path uses a reserved Windows name")
    if ext and not p.lower().endswith(ext):
        raise ContractError(f"project path must end in {ext}")
    return p


def check_selector(sel) -> dict:
    """{"id": str, "name": str}. The id is the authoritative identity (AE item id as decimal
    string, Premiere sequence GUID); the name is display-only and re-checked for ambiguity."""
    if not isinstance(sel, dict):
        raise ContractError("choose a composition or sequence from the inspection report")
    sid, name = sel.get("id"), sel.get("name")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9{}-]{1,64}", sid):
        raise ContractError("selector id is invalid")
    if not isinstance(name, str) or not (0 < len(name) <= 256) or any(ord(c) < 32 for c in name):
        raise ContractError("selector name is invalid")
    return {"id": sid, "name": name}


def check_range(rng, *, total_frames=None):
    """None (full duration) or {"start": int, "end": int} inclusive frame numbers, 0-based within
    the composition/sequence. Bounds-checked against the preflight duration when known."""
    if rng is None:
        return None
    if not isinstance(rng, dict):
        raise ContractError("range must be {start, end}")
    s, e = rng.get("start"), rng.get("end")
    if not (isinstance(s, int) and isinstance(e, int)) or isinstance(s, bool) or isinstance(e, bool):
        raise ContractError("range start/end must be whole frame numbers")
    if s < 0 or e < s:
        raise ContractError("range end must be at or after its start")
    if total_frames is not None and e >= int(total_frames):
        raise ContractError(f"range ends after the last frame ({int(total_frames) - 1})")
    return {"start": s, "end": e}


def check_preset(preset_id, app) -> dict:
    p = PRESETS.get(preset_id) if isinstance(preset_id, str) else None
    if not p or app not in p["apps"]:
        raise ContractError("unknown output preset for this application")
    return p


def build_guest_manifest(adobe: dict, *, attempt_id: str, deadline_s: int, input_size: int) -> dict:
    """What the guest is told (GET {base}/manifest). Built by the control plane; contains no URLs,
    credentials or storage keys."""
    mode = adobe.get("mode")
    if mode not in ("render", "inspect", "scrub"):
        raise ContractError("bad mode")
    m = {"protocol": PROTOCOL, "attempt_id": attempt_id, "mode": mode, "app": adobe.get("app"),
         "deadline_s": int(deadline_s), "limits": dict(LIMITS)}
    if mode == "scrub":
        return m
    if m["app"] not in APPS:
        raise ContractError("bad app")
    m["project_path"] = check_rel_path(adobe.get("project_path"), ext=PROJECT_EXT[m["app"]])
    m["input"] = {"size": int(input_size), "sha256": adobe.get("input_sha256")}
    if mode == "render":
        if not is_hex64(adobe.get("input_sha256")):
            raise ContractError("render requires the inspected input digest")
        m["selector"] = check_selector(adobe.get("selector"))
        check_preset(adobe.get("preset_id"), m["app"])
        m["preset_id"] = adobe["preset_id"]
        exp = adobe.get("expected") or {}
        m["range"] = check_range(adobe.get("range"), total_frames=exp.get("duration_frames"))
        m["expected"] = {k: exp.get(k) for k in ("width", "height", "fps_num", "fps_den",
                                                 "duration_frames", "has_audio")}
        m["output_name"] = check_output_name(adobe.get("output_name") or "output.mkv")
    return m


def check_output_name(name) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.mkv", name):
        raise ContractError("bad output name")
    return name


def validate_event(ev) -> dict:
    """A guest event. Untrusted: shape-checked, strings clipped, unknown keys dropped."""
    if not isinstance(ev, dict) or ev.get("type") not in EVENT_TYPES:
        raise ContractError("bad event")
    out = {"type": ev["type"]}
    if ev["type"] == "stage":
        if ev.get("stage") not in STAGES:
            raise ContractError("bad stage")
        out["stage"] = ev["stage"]
    elif ev["type"] == "progress":
        pct = ev.get("percent")
        if pct is not None and not (isinstance(pct, (int, float)) and 0 <= pct <= 100):
            raise ContractError("bad percent")
        out["percent"] = None if pct is None else round(float(pct), 1)   # None = indeterminate
        for k in ("frame", "total_frames"):
            v = ev.get(k)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                raise ContractError("bad frame counter")
            out[k] = v
    out["message"] = _clip(ev.get("message"), 500)
    return out


def validate_done(d) -> dict:
    if not isinstance(d, dict) or d.get("status") not in ("completed", "failed"):
        raise ContractError("bad done")
    out = {"status": d["status"], "message": _clip(d.get("message"), 1000)}
    if d["status"] == "failed":
        fc = d.get("failure_cause")
        out["failure_cause"] = fc if fc in GUEST_FAILURES else "guest_crashed"
    scrub = d.get("vram_scrub") if isinstance(d.get("vram_scrub"), dict) else {}
    out["vram_scrub"] = {"verified": scrub.get("verified") is True,
                         "bytes": scrub.get("bytes") if isinstance(scrub.get("bytes"), int) else 0}
    md = d.get("output") if isinstance(d.get("output"), dict) else None
    if d["status"] == "completed" and md is not None:
        out["output"] = validate_output_metadata(md)
    if d["status"] == "completed" and d.get("uploaded") is not None:
        out["uploaded"] = validate_uploaded(d["uploaded"])
        if "output" in out and (out["uploaded"]["kind"] != "output"
                                or out["uploaded"]["sha256"] != out["output"]["sha256"]
                                or out["uploaded"]["bytes"] != out["output"]["bytes"]):
            raise ContractError("uploaded object does not match the output metadata")
    return out


def validate_uploaded(u) -> dict:
    """done.uploaded: which presigned object the guest finished PUTting. The server re-checks the
    stored object's size/sha256 before finalizing the attempt."""
    if not isinstance(u, dict) or u.get("kind") not in UPLOAD_KINDS or not is_hex64(u.get("sha256")):
        raise ContractError("bad uploaded object")
    n = u.get("bytes")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= LIMITS["max_output_bytes"]:
        raise ContractError("bad uploaded size")
    return {"kind": u["kind"], "sha256": u["sha256"], "bytes": n}


def validate_upload_request(r) -> dict:
    """Body of POST {base}/output_url."""
    out = validate_uploaded(r)
    cap = LIMITS["max_output_bytes"] if out["kind"] == "output" else MAX_REPORT_BYTES
    if out["bytes"] > cap:
        raise ContractError("upload larger than the limit for its kind")
    return out


def check_user_data(raw, *, dev=False) -> dict:
    """Parse + validate EC2 user data -> {"relay", "token", "attempt"}. pb_relay must be https
    (http://127.0.0.1|localhost only with dev), no userinfo/query/fragment, path ending in GUEST_PATH."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > USER_DATA_MAX_BYTES:
        raise ContractError("user data missing or too large")
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ContractError("user data is not JSON")
    if not isinstance(d, dict):
        raise ContractError("user data is not an object")
    relay, token, attempt = d.get("pb_relay"), d.get("pb_token"), d.get("pb_attempt")
    if not isinstance(relay, str) or len(relay) > 1024:
        raise ContractError("bad pb_relay")
    u = urlsplit(relay)
    local = u.scheme == "http" and dev and u.hostname in ("127.0.0.1", "localhost")
    if not (u.scheme == "https" or local) or not u.hostname or "@" in u.netloc or u.query or u.fragment \
            or not u.path.endswith(GUEST_PATH) or any(ord(c) < 33 or c in "#?\\" for c in relay):
        raise ContractError("bad pb_relay")
    if not is_hex64(token):
        raise ContractError("bad pb_token")
    if not isinstance(attempt, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", attempt):
        raise ContractError("bad pb_attempt")
    return {"relay": relay, "token": token, "attempt": attempt}


def validate_output_metadata(md) -> dict:
    """The guest's own ffprobe summary of the MKV it uploaded. Advisory: the host re-checks size
    and sha256 of the bytes it actually received; the server re-checks the stored object."""
    def _int(k, lo=0, hi=10**13):
        v = md.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ContractError(f"bad output metadata {k}")
        return v
    sha = md.get("sha256")
    if not is_hex64(sha):
        raise ContractError("bad output sha256")
    return {"sha256": sha, "bytes": _int("bytes", 1), "width": _int("width", 1, 16384),
            "height": _int("height", 1, 16384), "fps_num": _int("fps_num", 1, 1000000),
            "fps_den": _int("fps_den", 1, 1000000), "frames": _int("frames", 1, 10**8),
            "has_audio": md.get("has_audio") is True,
            "video_codec": _clip(md.get("video_codec"), 32), "audio_codec": _clip(md.get("audio_codec"), 32),
            "pix_fmt": _clip(md.get("pix_fmt"), 32)}


def validate_report(rep) -> dict:
    """An inspection report. Untrusted guest output, size-bounded by the relay before parsing.
    Returns the normalized, bounded form the server stores and the UI renders."""
    if not isinstance(rep, dict) or rep.get("app") not in APPS:
        raise ContractError("bad report")
    out = {"app": rep["app"], "app_version": _clip(rep.get("app_version"), 32),
           "project_path": _clip(rep.get("project_path"), LIMITS["max_path_chars"]),
           "input_sha256": rep.get("input_sha256") if is_hex64(rep.get("input_sha256")) else None,
           "selectors": [], "missing_media": [], "missing_fonts": [], "missing_effects": [],
           "dependencies": [], "warnings": [], "errors": [], "limitations": []}
    sels = rep.get("selectors") if isinstance(rep.get("selectors"), list) else []
    for s in sels[:MAX_SELECTORS]:
        try:
            sel = check_selector({"id": s.get("id"), "name": s.get("name")})
        except (ContractError, AttributeError):
            continue
        meta = {}
        for k, lo, hi in (("width", 1, 16384), ("height", 1, 16384), ("fps_num", 1, 1000000),
                          ("fps_den", 1, 1000000), ("duration_frames", 1, 10**8)):
            v = s.get(k)
            meta[k] = v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else None
        meta["pixel_aspect"] = s.get("pixel_aspect") if isinstance(s.get("pixel_aspect"), (int, float)) else None
        meta["has_audio"] = s.get("has_audio") is True
        meta["alpha_required"] = s.get("alpha_required") is True
        meta["hdr"] = s.get("hdr") is True
        meta["bit_depth"] = s.get("bit_depth") if s.get("bit_depth") in (8, 16, 32) else None
        out["selectors"].append({**sel, **meta})
    names = [s["name"] for s in out["selectors"]]
    for s in out["selectors"]:
        s["ambiguous_name"] = names.count(s["name"]) > 1
    for k in ("missing_media", "missing_fonts", "missing_effects", "dependencies", "warnings",
              "errors", "limitations"):
        v = rep.get(k) if isinstance(rep.get(k), list) else []
        out[k] = [_clip(x, 300) for x in v[:200] if isinstance(x, str)]
    out["dynamic_link"] = rep.get("dynamic_link") is True
    out["mogrt"] = rep.get("mogrt") is True
    return out


def blocking_reasons(report: dict, selector_id: str, preset_id: str, *, dynamic_link_ok=False) -> list:
    """Why a render of `selector_id` with `preset_id` cannot be accepted, from a validated
    report. Empty list = no known blocker (unresolved compatibility is still conservative: an
    error recorded by the inspector blocks everything)."""
    why = list(report.get("errors") or [])
    for k, label in (("missing_media", "missing media"), ("missing_fonts", "missing fonts"),
                     ("missing_effects", "missing effects/plugins")):
        if report.get(k):
            why.append(f"{label}: {', '.join(report[k][:5])}")
    if report.get("dynamic_link") and not dynamic_link_ok:
        why.append("Dynamic Link to After Effects is not supported on this worker; render the "
                   "linked comps to media and replace them in the sequence")
    if report.get("mogrt") and not dynamic_link_ok:
        why.append("Motion Graphics templates (MOGRT) are not qualified on this worker; render "
                   "and replace them")
    sel = next((s for s in report.get("selectors") or [] if s["id"] == selector_id), None)
    if sel is None:
        why.append("the selected composition/sequence is not in the inspection report")
        return why
    if sel.get("ambiguous_name"):
        why.append(f"more than one item is named '{sel['name']}'; rename it so the choice is unambiguous")
    try:
        p = check_preset(preset_id, report["app"])
    except ContractError as e:
        why.append(str(e))
        return why
    if sel.get("alpha_required") and not p["alpha"]:
        why.append("this item needs alpha, but the preset delivers opaque video")
    if sel.get("hdr") and not p["hdr"]:
        why.append("this item is HDR, but the preset delivers SDR")
    if not sel.get("duration_frames"):
        why.append("the inspector could not determine this item's duration")
    return why


def _clip(v, n):
    if not isinstance(v, str):
        return ""
    v = "".join(ch for ch in v if ch == "\n" or ord(ch) >= 32)
    return v[:n]
