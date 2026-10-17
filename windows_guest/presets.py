"""presets.py — preset id -> pinned ffmpeg argv, ffprobe validation, final delivery.

The Adobe app writes an intermediate (AE: lossless AVI via the image's output module; Premiere:
H.264 .mp4 via the image's .epr). `deliver()` turns that into the MKV the preset promises: stream
copy when ffprobe already shows h264 yuv420p (+aac/no audio), otherwise a pinned encode. Frame rate
is never resampled (-fps_mode passthrough) and nothing is scaled; the validator then insists the
result matches what the inspection promised (size, exact fps, frames +-1, audio)."""
import json
import os
import re
from fractions import Fraction

import adobe_contract as C
from common import JobFailure, sha256_file

_FRAME = re.compile(r"^frame=\s*(\d+)")


def ffprobe_argv(ffprobe, path):
    return [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
            "-count_packets", path]


def decode_check_argv(ffmpeg, path):
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-i", path, "-f", "null", "-"]


def encode_argv(preset_id, ffmpeg, src, dst, *, src_info=None, nvenc=False):
    p = C.PRESETS[preset_id]
    info = src_info or {}
    a = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
         "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-fps_mode", "passthrough"]
    if info.get("video_codec") == p["video_codec"] and info.get("pix_fmt") == "yuv420p":
        a += ["-c:v", "copy"]
    elif nvenc:
        a += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"]
    else:
        a += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    if info.get("audio_codec") in (None, p["audio_codec"]):
        a += ["-c:a", "copy"]
    else:
        a += ["-c:a", "aac", "-ar", "48000", "-b:a", "192k"]
        if (info.get("channels") or 0) > 6:
            a += ["-ac", "6"]
    return a + ["-f", "matroska", dst]


def probe_summary(text) -> dict:
    """First video + first audio stream of an ffprobe JSON document."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise JobFailure("output_invalid", "ffprobe output was not JSON")
    streams = doc.get("streams") if isinstance(doc, dict) and isinstance(doc.get("streams"), list) else []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        raise JobFailure("output_invalid", "no video stream")
    fps = _frac(v.get("r_frame_rate")) or _frac(v.get("avg_frame_rate"))
    frames = _int(v.get("nb_read_packets")) or _int(v.get("nb_frames"))
    if not frames and fps:
        dur = (doc.get("format") or {}).get("duration")
        try:
            frames = round(float(dur) * fps)
        except (TypeError, ValueError):
            frames = None
    return {"video_codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"), "width": _int(v.get("width")),
            "height": _int(v.get("height")), "fps": fps, "frames": frames,
            "audio_codec": a.get("codec_name") if a else None, "channels": _int(a.get("channels")) if a else None}


def check_output(info, expected, rng, *, sha256, size, preset_id="mkv_h264_aac") -> dict:
    """Metadata for /v1/done (validate_output_metadata) or JobFailure(output_invalid)."""
    p = C.PRESETS[preset_id]
    exp = expected or {}

    def bad(why):
        raise JobFailure("output_invalid", why)
    if info["video_codec"] != p["video_codec"] or info["pix_fmt"] != "yuv420p":
        bad(f"output is {info['video_codec']}/{info['pix_fmt']}, not {p['video_codec']}/yuv420p")
    for k in ("width", "height"):
        if exp.get(k) and info.get(k) != exp[k]:
            bad(f"output {k} {info.get(k)} != expected {exp[k]}")
    if not info.get("fps"):
        bad("output frame rate unknown")
    if exp.get("fps_num") and exp.get("fps_den") and info["fps"] != Fraction(exp["fps_num"], exp["fps_den"]):
        bad(f"output fps {info['fps']} != expected {exp['fps_num']}/{exp['fps_den']}")
    want = (rng["end"] - rng["start"] + 1) if rng else exp.get("duration_frames")
    if not info.get("frames"):
        bad("output has no frames")
    if want and abs(info["frames"] - want) > 1:
        bad(f"output has {info['frames']} frames, expected {want}")
    if exp.get("has_audio") and not info.get("audio_codec"):
        bad("output has no audio but the project has audio")
    if info.get("audio_codec") not in (None, p["audio_codec"]):
        bad(f"output audio is {info['audio_codec']}, not {p['audio_codec']}")
    md = {"sha256": sha256, "bytes": size, "width": info["width"], "height": info["height"],
          "fps_num": info["fps"].numerator, "fps_den": info["fps"].denominator, "frames": info["frames"],
          "has_audio": bool(info.get("audio_codec")), "video_codec": info["video_codec"],
          "audio_codec": info.get("audio_codec") or "", "pix_fmt": info["pix_fmt"]}
    try:
        return C.validate_output_metadata(md)
    except C.ContractError as e:
        bad(str(e))


def deliver(job, src):
    """Intermediate -> validated MKV in job.ctl_dir. Returns (path, metadata)."""
    prof, m = job.profile, job.manifest
    dst = os.path.join(job.ctl_dir, m["output_name"])
    job.stage("encoding")
    info = _probe(job, src)
    argv = encode_argv(m["preset_id"], prof["ffmpeg"], src, dst, src_info=info,
                       nvenc=bool(prof.get("nvenc_qualified")))
    total = info.get("frames")

    def on_line(line):
        mt = _FRAME.match(line)
        if mt:
            fr = int(mt.group(1))
            job.progress(frame=fr, total_frames=total, percent=min(100.0, 100.0 * fr / total) if total else None)
    rc, tail = job.run(argv, on_line=on_line)
    if rc != 0:
        raise JobFailure("encode_failed", f"ffmpeg exited {rc}: {' | '.join(tail[-3:])[:300]}")
    job.stage("validating")
    size = os.path.getsize(dst) if os.path.isfile(dst) else 0
    if size == 0:
        raise JobFailure("output_invalid", "encoder produced no output")
    if size > job.limits["max_output_bytes"]:
        raise JobFailure("output_too_large", f"output is {size} bytes")
    out = _probe(job, dst)
    rc, tail = job.run(decode_check_argv(prof["ffmpeg"], dst))
    if rc != 0:
        raise JobFailure("output_invalid", f"output does not decode cleanly: {' | '.join(tail[-2:])[:300]}")
    return dst, check_output(out, m.get("expected"), m.get("range"), sha256=sha256_file(dst), size=size,
                             preset_id=m["preset_id"])


def _probe(job, path):
    rc, lines = job.run(ffprobe_argv(job.profile["ffprobe"], path))
    if rc != 0:
        raise JobFailure("output_invalid", f"ffprobe failed ({rc}) on {os.path.basename(path)}")
    return probe_summary("\n".join(lines))


def _frac(s):
    try:
        f = Fraction(s)
        return f if f > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
