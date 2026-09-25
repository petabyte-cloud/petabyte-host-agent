"""
Petabyte Agent - Main Entry Point
Runs both the task fetcher and UI.

Usage:
  python main.py            start the node (heartbeat + job polling + local dashboard)
  python main.py doctor     preflight: env, GPU, Docker, API connectivity
  python main.py --version  print the version
  python main.py --help     show help

The heartbeat/job-poll log stays on stdout and in petabyte_agent.log for diagnosis.
Set PETABYTE_QUIET=1 for a clean console (warnings+ only); PETABYTE_VERBOSE=1 for debug.
"""
import logging
import sys
import os
import threading

from dotenv import load_dotenv

import cli_ui
import agent_cli

# Load environment variables
load_dotenv()

# Set environment variables if not already set
if not os.getenv("FASTAPI_SERVER_URL"):
    os.environ["FASTAPI_SERVER_URL"] = "https://Api.petabyte.market"


def _console_level():
    if cli_ui._env_flag("PETABYTE_VERBOSE") or "--verbose" in sys.argv:
        return logging.DEBUG
    if cli_ui._env_flag("PETABYTE_QUIET") or "--quiet" in sys.argv:
        return logging.WARNING
    return logging.INFO


def _configure_logging():
    """File handler keeps full INFO diagnostics; console level is user-adjustable.

    Nothing is removed — the on-disk log is unchanged. PETABYTE_QUIET only calms the
    console so the startup summary stays readable; PETABYTE_VERBOSE turns it all on."""
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(_console_level())
    fileh = logging.FileHandler("petabyte_agent.log")
    fileh.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
    console.setFormatter(fmt)
    fileh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers[:] = [console, fileh]


def run_agent_loop():
    """Run the agent (heartbeat thread + job poll loop)."""
    from task_fetcher import run_agent          # imported lazily: hard-exits if unconfigured
    logging.info("Starting Petabyte Agent...")
    try:
        run_agent()
    except KeyboardInterrupt:
        logging.info("Agent stopped by user")


def main():
    """Main entry point."""
    # --help / --version / doctor are handled before importing the config-required
    # modules, so `doctor` can diagnose an un-configured node instead of hard-exiting.
    if agent_cli.handle_cli(sys.argv[1:]):
        return

    _configure_logging()
    ui = cli_ui.out
    agent_cli.banner(ui, url=os.getenv("PETABYTE_API_URL"))

    # Fail early and readably if the node isn't configured, instead of crashing deep in
    # an import. (task_fetcher enforces the same requirement; this is the friendly front.)
    missing = [v for v in ("PETABYTE_API_URL", "PETABYTE_API_KEY", "PETABYTE_SPEC_ID")
               if not os.getenv(v)]
    if missing:
        cli_ui.err.error(
            "This node is not configured yet.",
            reason="Missing " + ", ".join(missing) + ".",
            checks=["run `python main.py doctor` to see exactly what's needed",
                    "run `python provision.py` to register this machine and write agent.env"],
            run="python main.py doctor")
        sys.exit(1)

    ui.line(ui.dim("  dashboard  ") + "http://127.0.0.1:5000")
    ui.line(ui.dim("  logs       ") + "petabyte_agent.log  (PETABYTE_QUIET=1 to calm this console)")
    ui.blank()

    # Import here so `doctor`/`--help` work even when the node isn't configured yet.
    from ui import run_ui

    ui_thread = threading.Thread(target=run_ui, args=('127.0.0.1', 5000, False), daemon=True)
    ui_thread.start()
    logging.info("UI started on http://127.0.0.1:5000")

    try:
        run_agent_loop()
    except KeyboardInterrupt:
        ui.blank()
        ui.info("Shutting down…")
        sys.exit(0)


if __name__ == "__main__":
    main()
