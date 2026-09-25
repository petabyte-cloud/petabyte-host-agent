"""console.py — pretty, honest agent console output.

Replaces the raw `logging.info(...)` lines a seller sees with a compact, colored, symbol-prefixed
status feed + a startup banner that shows what the node is earning. Degrade-safe: no color when
stdout isn't a TTY or NO_COLOR is set, so logs stay clean in files and CI. Pure stdout — never
raises into the agent loop.
"""
import os
import sys
import time

_NO_COLOR = bool(os.getenv("NO_COLOR")) or not sys.stdout.isatty()


def _c(code, s):
    return s if _NO_COLOR else f"\033[{code}m{s}\033[0m"


def dim(s):     return _c("2", s)
def bold(s):    return _c("1", s)
def green(s):   return _c("32", s)
def cyan(s):    return _c("36", s)
def yellow(s):  return _c("33", s)
def red(s):     return _c("31", s)
def magenta(s): return _c("35", s)

_SYM = {"claim": "▶", "done": "✓", "fail": "✗", "idle": "·",
        "mine": "⛏", "info": "•", "warn": "!", "beat": "♥"}
_COLOR = {"done": green, "fail": red, "claim": cyan, "mine": magenta,
          "idle": dim, "warn": yellow, "beat": magenta}


def _ts():
    return time.strftime("%H:%M:%S")


def line(kind, msg):
    """One status line: <time> <symbol> <message>, colored by kind. Never raises."""
    try:
        colf = _COLOR.get(kind, lambda s: s)
        print(f"{dim(_ts())} {colf(_SYM.get(kind, '•'))} {msg}", flush=True)
    except Exception:
        pass


def banner(api_url, spec_id, provider=None):
    """Startup header identifying the node."""
    try:
        rule = dim("  " + "─" * 48)
        print()
        print("  " + bold(cyan("⬢ PETABYTE")) + "  " + dim("GPU node agent"))
        print(rule)
        who = f"  node    {bold(str(spec_id))}"
        if provider:
            who += dim(f"   ({provider})")
        print(who)
        print(f"  api     {dim(api_url)}")
    except Exception:
        pass


def earnings(net_hr, low_daily, high_daily):
    """Show the honest earnings line under the banner: definitive net/hr + estimated range."""
    try:
        parts = [green(f"${net_hr:.2f}") + dim("/hr net")]
        if high_daily:
            parts.append(dim(f"≈ ${low_daily:.0f}–${high_daily:.0f}/day at typical use"))
        print("  earn    " + "  ".join(parts))
        print(dim("  " + "─" * 48))
        print()
    except Exception:
        pass


def ready(poll_s):
    try:
        print("  " + green("● online") + dim(f" — waiting for work (poll {poll_s}s)"))
        print()
    except Exception:
        pass
