"""cli_ui — a tiny, dependency-free terminal presentation layer for Petabyte CLIs.

CLI output IS user interface. This module lets the buyer CLI, the seller agent, and
the packaged desktop .exe present the same information the way a human reads it —
semantic colour, status icons, aligned tables, key/value panels — while staying
correct in the places pretty output usually breaks:

  * stdout is a pipe / file (not a TTY)     -> colour + animation OFF, plain text
  * NO_COLOR is set                          -> colour OFF (https://no-color.org)
  * the terminal can't encode Unicode (cp1252 on old Windows consoles) -> ASCII icons
  * a caller wants machine-readable output    -> use emit_json(); never coloured

It has ZERO third-party dependencies on purpose: it must survive being frozen into a
PyInstaller .exe with no hidden-import or data-file surprises. Everything here is
stdlib. Keep it that way.

Colour is *semantic*, not decoration (see the Petabyte CLI style):
    green  -> healthy / success / online
    yellow -> warning / pending
    red    -> failure / offline / error
    cyan   -> informational
    dim    -> metadata / secondary
Amber and teal are the two brand accents, used sparingly for headings/emphasis.

Environment overrides (all optional):
    NO_COLOR=1            force colour off
    PETABYTE_COLOR=always|never|auto   explicit control (default: auto)
    FORCE_COLOR=1         force colour on even when stdout is not a TTY
    PETABYTE_ASCII=1      force ASCII icons (never emit Unicode symbols)
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys

# ---------------------------------------------------------------- ANSI codes
_RESET = "\033[0m"
_SGR = {
    "bold": "1", "dim": "2", "italic": "3", "underline": "4",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "grey": "90", "gray": "90",
    # 256-colour brand accents (degrade gracefully; supported by every modern terminal)
    "amber": "38;5;214", "teal": "38;5;44", "mint": "38;5;42",
}

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    """Remove SGR escape sequences — used by tests to assert on visible text."""
    return _ANSI_RE.sub("", s)


def visible_len(s: str) -> int:
    return len(strip_ansi(s))


# ---------------------------------------------------------------- capability probing
def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def supports_color(stream=None) -> bool:
    """Decide whether to emit ANSI colour for *stream* (default: sys.stdout).

    Honours NO_COLOR, PETABYTE_COLOR, FORCE_COLOR, TERM=dumb, and TTY-ness. On
    Windows it also tries to switch the console into VT-processing mode; if that
    fails we stay monochrome rather than printing raw escape codes.
    """
    stream = stream if stream is not None else sys.stdout
    mode = os.environ.get("PETABYTE_COLOR", "auto").strip().lower()
    if mode == "never" or _env_flag("NO_COLOR"):
        return False
    if mode == "always" or _env_flag("FORCE_COLOR"):
        return _enable_windows_vt(stream)
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    return _enable_windows_vt(stream)


def _enable_windows_vt(stream) -> bool:
    """Best-effort: turn on ANSI escape processing on a Windows console.

    Returns True when colour is safe to emit. On non-Windows this is a no-op that
    returns True. Modern Windows Terminal / PowerShell already support VT; this just
    makes older conhost sessions behave too."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        ENABLE_VT = 0x0004
        handle = kernel32.GetStdHandle(-11 if stream is sys.stdout else -12)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
        return True
    except Exception:
        return False


def supports_unicode(stream=None) -> bool:
    stream = stream if stream is not None else sys.stdout
    if _env_flag("PETABYTE_ASCII"):
        return False
    enc = (getattr(stream, "encoding", None) or "").lower()
    if not enc:
        return False
    if "utf" in enc:
        return True
    # Probe: can this encoding represent the symbols we use?
    try:
        "✓✗•→".encode(enc)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- the UI object
_UNICODE_ICONS = {
    "ok": "✓", "fail": "✗", "warn": "▲", "info": "ℹ", "bullet": "•",
    "arrow": "→", "dot_on": "●", "dot_off": "○",
}
_ASCII_ICONS = {
    "ok": "OK", "fail": "x", "warn": "!", "info": "i", "bullet": "*",
    "arrow": "->", "dot_on": "*", "dot_off": "o",
}

# state -> (icon-key, colour) — the canonical status vocabulary shared across surfaces.
_STATUS = {
    "online": ("dot_on", "green"), "ready": ("dot_on", "green"),
    "running": ("dot_on", "green"), "active": ("dot_on", "green"),
    "completed": ("ok", "green"), "verified": ("ok", "green"),
    "healthy": ("ok", "green"), "paid": ("ok", "green"), "ok": ("ok", "green"),
    "pending": ("dot_on", "yellow"), "warning": ("warn", "yellow"),
    "degraded": ("warn", "yellow"), "held": ("warn", "yellow"),
    "queued": ("dot_on", "yellow"), "connecting": ("dot_on", "yellow"),
    "offline": ("dot_off", "red"), "failed": ("fail", "red"),
    "error": ("fail", "red"), "unverified": ("dot_off", "red"),
    "rejected": ("fail", "red"),
}


class Ui:
    """A presenter bound to one output stream. Cheap to construct; make one per stream."""

    def __init__(self, stream=None, *, color=None, unicode=None):
        self.stream = stream if stream is not None else sys.stdout
        self.color = supports_color(self.stream) if color is None else bool(color)
        self.unicode = supports_unicode(self.stream) if unicode is None else bool(unicode)
        self.icons = _UNICODE_ICONS if self.unicode else _ASCII_ICONS

    # ---- low-level -------------------------------------------------------
    def paint(self, text, *styles) -> str:
        if not self.color or not styles:
            return text
        codes = ";".join(_SGR[s] for s in styles if s in _SGR)
        return f"\033[{codes}m{text}{_RESET}" if codes else text

    # semantic colours
    def bold(self, t): return self.paint(t, "bold")
    def dim(self, t): return self.paint(t, "dim")
    def green(self, t): return self.paint(t, "green")
    def yellow(self, t): return self.paint(t, "yellow")
    def red(self, t): return self.paint(t, "red")
    def cyan(self, t): return self.paint(t, "cyan")
    def blue(self, t): return self.paint(t, "blue")
    def amber(self, t): return self.paint(t, "amber")
    def teal(self, t): return self.paint(t, "teal")
    def muted(self, t): return self.paint(t, "dim")

    def icon(self, key) -> str:
        return self.icons.get(key, "")

    # ---- width -----------------------------------------------------------
    def width(self, default=80) -> int:
        try:
            w = shutil.get_terminal_size((default, 24)).columns
            return w if w and w > 0 else default
        except Exception:
            return default

    # ---- write helpers ---------------------------------------------------
    def _write(self, s=""):
        stream = self.stream
        if stream is None:
            # A windowed (pyinstaller --windowed) build has no console: sys.stdout /
            # sys.stderr are None. Presentation is best-effort — never crash on output.
            return self
        try:
            stream.write(s + "\n")
        except UnicodeEncodeError:
            # last-ditch: the stream lied about its encoding — degrade to ASCII.
            stream.write(strip_ansi(s).encode("ascii", "replace").decode() + "\n")
        except (OSError, ValueError):
            # closed or broken stream — output is best-effort, never fatal.
            pass
        return self

    def line(self, s=""):
        return self._write(s)

    def blank(self):
        return self._write("")

    # ---- structured components ------------------------------------------
    def heading(self, text) -> "Ui":
        """A bold, brand-accented section title with a hairline underline."""
        self._write(self.bold(self.teal(text.upper())))
        self._write(self.dim("─" * min(self.width(), max(8, visible_len(text))) if self.unicode
                             else "-" * max(8, len(text))))
        return self

    def section(self, text) -> "Ui":
        self._write("")
        self._write(self.bold(text))
        return self

    def rule(self) -> "Ui":
        ch = "─" if self.unicode else "-"
        return self._write(self.dim(ch * self.width()))

    def status_label(self, state) -> str:
        """Return an icon+coloured word for a known state (falls back to plain text)."""
        key = str(state or "").strip().lower()
        icon_key, colour = _STATUS.get(key, ("bullet", "cyan"))
        icon = self.icon(icon_key)
        label = str(state)
        painted = getattr(self, colour, self.cyan)(f"{icon} {label}".strip())
        return painted

    def kv(self, label, value, *, width=12, status=False) -> "Ui":
        """A left-aligned label + value row (the backbone of a summary panel)."""
        lbl = self.dim(f"{label:<{width}}")
        val = self.status_label(value) if status else str(value)
        return self._write(f"{lbl} {val}")

    def panel(self, title, rows, *, label_width=None) -> "Ui":
        """A titled key/value block.  rows: list of (label, value) or (label, value, 'status')."""
        if title:
            self._write(self.bold(title))
        pairs = [(str(r[0]), r[1], (len(r) > 2 and r[2] == "status")) for r in rows]
        lw = label_width or (max((len(p[0]) for p in pairs), default=0))
        for lbl, val, is_status in pairs:
            self.kv(lbl, val, width=lw, status=is_status)
        return self

    def table(self, headers, rows, *, aligns=None, max_col=None) -> "Ui":
        """Aligned columns with a dim header rule. Values are coerced to str.

        aligns: optional list of 'l'/'r' per column (default left, but numeric-looking
        columns auto-right-align). max_col: optional per-column max width (long values
        are middle-truncated so the head and tail stay visible)."""
        cols = len(headers)
        srows = [[("" if v is None else str(v)) for v in r] for r in rows]
        if max_col:
            ell = "…" if self.unicode else "..."
            srows = [[truncate_mid(v, max_col[i], ellipsis=ell)
                      if (max_col and i < len(max_col) and max_col[i])
                      else v for i, v in enumerate(r)] for r in srows]
        widths = [visible_len(str(headers[i])) for i in range(cols)]
        for r in srows:
            for i in range(cols):
                widths[i] = max(widths[i], visible_len(r[i]) if i < len(r) else 0)
        if aligns is None:
            aligns = []
            for i in range(cols):
                sample = [r[i] for r in srows if i < len(r)]
                numeric = sample and all(_looks_numeric(v) for v in sample)
                aligns.append("r" if numeric else "l")

        def fmt(cells, styler=None):
            out = []
            for i in range(cols):
                cell = cells[i] if i < len(cells) else ""
                pad = widths[i] - visible_len(cell)
                cell = (" " * pad + cell) if aligns[i] == "r" else (cell + " " * pad)
                out.append(styler(cell) if styler else cell)
            return "  ".join(out).rstrip()

        self._write(fmt([str(h) for h in headers], self.bold))
        self._write(self.dim(fmt(["─" * widths[i] if self.unicode else "-" * widths[i]
                                  for i in range(cols)])))
        for r in srows:
            self._write(fmt(r))
        return self

    # ---- messages --------------------------------------------------------
    def success(self, title, **fields) -> "Ui":
        self._write(self.green(f"{self.icon('ok')} {title}"))
        if fields:
            lw = max(len(k) for k in fields)
            for k, v in fields.items():
                self.kv(k, v, width=lw)
        return self

    def info(self, msg) -> "Ui":
        return self._write(f"{self.cyan(self.icon('info'))} {msg}")

    def warn(self, msg) -> "Ui":
        return self._write(f"{self.yellow(self.icon('warn'))} {self.yellow(msg)}")

    def step(self, msg, *, done=False) -> "Ui":
        """A progress log line — deterministic, safe in CI (no animation)."""
        mark = self.green(self.icon("ok")) if done else self.dim(self.icon("arrow"))
        return self._write(f"{mark} {msg}")

    def error(self, title, *, reason=None, checks=None, run=None, detail=None) -> "Ui":
        """A human-first error: what failed, why, what to check, what to run next.

        Low-level *detail* (status codes, tracebacks) is printed dimmed and last so it
        stays available without burying the actionable part."""
        self._write(self.red(f"{self.icon('fail')} {title}"))
        if reason:
            self._write(f"  {self.dim('Reason:')} {reason}")
        if checks:
            self._write(f"  {self.dim('Check:')}")
            for c in checks:
                self._write(f"    {self.icon('bullet')} {c}")
        if run:
            runs = [run] if isinstance(run, str) else run
            self._write(f"  {self.dim('Run:')}")
            for r in runs:
                self._write(f"    {self.cyan(r)}")
        if detail:
            self._write(self.dim(f"  ({detail})"))
        return self


def _looks_numeric(v: str) -> bool:
    s = strip_ansi(str(v)).strip().lstrip("$").rstrip("%").replace(",", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def truncate_mid(value, width: int, ellipsis: str = "…") -> str:
    """Middle-truncate a long value so both ends stay legible: abcd…wxyz.

    Long IDs/hashes should never be shown in full in a narrow column, but the head
    and tail are what humans use to recognise them, so keep both. The *ellipsis*
    marker is configurable so callers can pass an ASCII "..." when the stream can't
    encode Unicode; the retained width is computed from its length."""
    s = str(value)
    if width <= 0 or len(s) <= width:
        return s
    ell = ellipsis
    if width <= len(ell):
        return s[:width]
    keep = width - len(ell)
    head = (keep + 1) // 2
    tail = keep - head
    return s[:head] + ell + (s[-tail:] if tail else "")


def emit_json(obj, stream=None) -> None:
    """Machine-readable output: compact, uncoloured, newline-terminated, valid JSON.

    This is the contract other programs parse. It must never contain ANSI codes and
    must never change shape for cosmetic reasons."""
    stream = stream if stream is not None else sys.stdout
    if stream is None:
        # A windowed (pyinstaller --windowed) build has no stdout to write to.
        return
    stream.write(json.dumps(obj, default=str) + "\n")


# Convenience module-level presenters bound to the standard streams.
out = Ui(sys.stdout)
err = Ui(sys.stderr)


def for_stream(stream, **kw) -> Ui:
    return Ui(stream, **kw)
