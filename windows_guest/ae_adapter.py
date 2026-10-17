"""ae_adapter.py — After Effects: inspect via a trusted ExtendScript, render via aerender.

inspect: `AfterFX.com -noui -r ae_inspect.jsx`; the script reads {project, report, workspace} from a
fixed request file in the ctl dir (ASCII JSON written here; the ctl dir reaches the script through
the PB_CTL environment variable) so no buyer string is ever interpolated into JavaScript.
render: aerender selects by -comp NAME, so the selector is re-verified inside this VM (id exists,
name unique) before a fixed argv is spawned; progress/err lines are parsed, never -continueOnMissingFootage."""
import json
import os
import re
from fractions import Fraction

import adobe_contract as C
import presets
from common import JobFailure

JSX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ae_inspect.jsx")
REQUEST_NAME, REPORT_NAME = "ae_inspect_request.json", "ae_inspect_report.json"
NTSC = (24, 30, 60, 48, 120)
_PROG = re.compile(r"^PROGRESS:\s+(?:-?\d+[:;]){3}\d+\s+\((\d+)\)")
_RULES = (
    (re.compile(r"missing[^\n]*(footage|file|media)|(footage|file|media)[^\n]*missing", re.I), "missing_media"),
    (re.compile(r"missing[^\n]*font|font[^\n]*(missing|substitut)", re.I), "missing_fonts"),
    (re.compile(r"(effect|plug-?in)[^\n]*(missing|not installed|could not be found|unavailable)"
                r"|missing[^\n]*(effect|plug-?in)", re.I), "missing_effects"),
    (re.compile(r"(unable|could not|can'?t|cannot) (to )?open|corrupt|damaged|unsupported (file )?(format|version)"
                r"|newer version", re.I), "project_open_failed"),
    (re.compile(r"aerender error|^\s*error\b|After Effects error", re.I), "render_failed"),
)


# ---------------------------------------------------------------- pure helpers
def fps_to_rational(fps):
    fps = float(fps)
    if not fps > 0:
        raise ValueError("bad fps")
    n = round(fps)
    if abs(fps - n) < 1e-4:
        return n, 1
    for k in NTSC:
        if abs(fps - k * 1000 / 1001) < 5e-3:
            return k * 1000, 1001
    f = Fraction(fps).limit_denominator(1001)
    return f.numerator, f.denominator


def build_aerender_argv(aerender, project, comp_name, rs, om, output, rng=None):
    C.check_selector({"id": "0", "name": comp_name})       # length + control-char policy
    argv = [aerender, "-project", project, "-comp", comp_name, "-RStemplate", rs, "-OMtemplate", om,
            "-output", output, "-v", "ERRORS_AND_PROGRESS", "-close", "DO_NOT_SAVE_CHANGES"]
    if rng:
        argv += ["-s", str(int(rng["start"])), "-e", str(int(rng["end"]))]
    return argv


def parse_progress_line(line):
    m = _PROG.match(line.strip())
    return int(m.group(1)) if m else None


def classify_error_line(line):
    s = line.strip()
    if not s or s.startswith("PROGRESS:"):
        return None
    for rx, cause in _RULES:
        if rx.search(s):
            return cause
    return None


def report_from_jsx(raw, profile) -> dict:
    """Shape the (untrusted) JSX output into a contract report; validate_report bounds it later."""
    def strs(k):
        v = raw.get(k) if isinstance(raw, dict) else None
        return [x for x in v if isinstance(x, str)][:200] if isinstance(v, list) else []
    sels = []
    comps = raw.get("comps") if isinstance(raw, dict) and isinstance(raw.get("comps"), list) else []
    for c in comps[:C.MAX_SELECTORS]:
        if not isinstance(c, dict):
            continue
        try:
            num, den = fps_to_rational(c.get("fps"))
        except (TypeError, ValueError):
            num = den = None
        sels.append({"id": c.get("id"), "name": c.get("name"), "width": c.get("width"), "height": c.get("height"),
                     "pixel_aspect": c.get("pixel_aspect"), "fps_num": num, "fps_den": den,
                     "duration_frames": c.get("duration_frames"), "has_audio": c.get("has_audio") is True,
                     "alpha_required": False, "bit_depth": c.get("bit_depth"), "hdr": c.get("hdr") is True})
    return {"app": C.AE, "app_version": str(profile.get("ae_version", "")), "selectors": sels,
            "missing_media": strs("missing_media"), "missing_fonts": strs("missing_fonts"),
            "missing_effects": strs("missing_effects"), "dependencies": [], "warnings": strs("warnings"),
            "errors": strs("errors"), "limitations": strs("limitations") + [C.ALPHA_NOT_DETECTED]}


# ---------------------------------------------------------------- adapter
def inspect(job) -> dict:
    prof = job.profile
    req_path, rep_path = (os.path.join(job.ctl_dir, n) for n in (REQUEST_NAME, REPORT_NAME))
    with open(req_path, "w", encoding="ascii") as f:
        json.dump({"project": job.project_abs, "report": rep_path, "workspace": job.bundle_dir}, f, ensure_ascii=True)
    if os.path.exists(rep_path):
        os.remove(rep_path)
    env = {**os.environ, "PB_CTL": job.ctl_dir}
    rc, tail = job.run([prof["afterfx"], "-noui", "-r", JSX], env=env, timeout=min(job.remaining(), 1800))
    if not os.path.isfile(rep_path):
        raise JobFailure("project_open_failed",
                         f"After Effects produced no inspection report (exit {rc}): {' | '.join(tail[-3:])[:300]}")
    with open(rep_path, "rb") as f:
        body = f.read(8 << 20)
    try:
        raw = json.loads(body)
    except ValueError:
        raise JobFailure("project_open_failed", "inspection report was not valid JSON")
    return report_from_jsx(raw, prof)


def render(job):
    m, prof = job.manifest, job.profile
    rep = C.validate_report({**inspect(job), "project_path": m["project_path"]})
    if rep["errors"]:
        raise JobFailure("project_open_failed", rep["errors"][0])
    for k in ("missing_media", "missing_fonts", "missing_effects"):
        if rep[k]:
            raise JobFailure(k, "; ".join(rep[k][:5]))
    sel = m["selector"]
    hit = next((s for s in rep["selectors"] if s["id"] == sel["id"]), None)
    if hit is None or hit["name"] != sel["name"]:
        raise JobFailure("selector_not_found", "the selected composition is not in this project (renamed or removed?)")
    if hit["ambiguous_name"]:
        raise JobFailure("selector_ambiguous", f"more than one composition is named '{sel['name']}'")
    total = hit["duration_frames"]
    if m["range"]:
        if total and m["range"]["end"] >= total:
            raise JobFailure("bad_task", "range ends after the last frame")
        total = m["range"]["end"] - m["range"]["start"] + 1
    tpl = (prof.get("ae_templates") or {}).get(m["preset_id"])
    if not tpl:
        raise JobFailure("bad_task", "preset is not provisioned on this worker image")
    inter = os.path.join(job.ctl_dir, "ae_render" + tpl.get("ext", ".avi"))
    argv = build_aerender_argv(prof["aerender"], job.project_abs, sel["name"], tpl["rs"], tpl["om"], inter, m["range"])
    job.stage("rendering")
    st = {"base": None, "cause": None, "msg": ""}

    def on_line(line):
        fr = parse_progress_line(line)
        if fr is not None:
            st["base"] = fr if st["base"] is None else st["base"]
            done = fr - st["base"] + 1
            job.progress(frame=done, total_frames=total,
                         percent=min(100.0, 100.0 * done / total) if total else None)
            return
        cause = classify_error_line(line)
        if cause and not st["cause"]:
            st["cause"], st["msg"] = cause, line.strip()[:300]
    rc, tail = job.run(argv, on_line=on_line)
    if st["cause"]:
        raise JobFailure(st["cause"], st["msg"])
    if rc != 0:
        raise JobFailure("render_failed", f"aerender exited {rc}: {' | '.join(tail[-3:])[:300]}")
    if not os.path.isfile(inter) or os.path.getsize(inter) == 0:
        raise JobFailure("output_invalid", "aerender produced no output file")
    return presets.deliver(job, inter)
