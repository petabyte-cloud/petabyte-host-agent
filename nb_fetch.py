"""nb_fetch.py — pure helpers for the Colab-style /run notebook prefetch and the buyer's
unattended startup script (both write into the running container over docker-exec stdin).

Kept separate from task_fetcher so it imports without the agent's heavy dependencies.
The existing safe_fetch downloader pins public destinations and bounds redirects/bytes/time.
Only the validated response bytes reach the running container over docker-exec stdin.
A prefetch failure leaves the runtime available without an unchecked fallback.
"""
import os
import re
import shlex
import subprocess
from urllib.parse import urlparse


def safe_nb_name(url: str) -> str:
    """A safe filename for a fetched notebook/file: the URL's basename, sanitized and bounded.
    Empty/odd paths fall back to notebook.ipynb so it still opens as a notebook."""
    base = os.path.basename((urlparse(url).path or "").rstrip("/"))
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:100].lstrip(".")
    return base or "notebook.ipynb"


def notebook_write_argv(container: str, cache_dir: str, url: str) -> list:
    """Write already-validated bytes from stdin; no URL fetch or execution in the container."""
    target = "./" + safe_nb_name(url)
    inner = f"cd -- {shlex.quote(cache_dir)} && umask 077 && cat > {shlex.quote(target)}"
    return ["docker", "exec", "-i", container, "sh", "-c", inner]


def prefetch_notebook(container: str, cache_dir: str, url: str):
    """Use the same pinned, bounded, redirect-checked downloader as batch notebooks.

    No URL credentials, proxies or private destinations; plaintext stays in memory
    until sent on stdin to the buyer's container. No curl/wget fallback bypasses it.
    """
    from safe_fetch import get
    response = get(url, timeout=45)
    response.raise_for_status()
    subprocess.run(notebook_write_argv(container, cache_dir, url), input=response.content,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)


# Rental secrets (template_params.secrets): unsealed in the agent's RAM, written into a tmpfs inside
# the buyer's container — never docker env/argv, an agent log or the seller's disk. Mode 1777 like
# /tmp: the container's OWN user (root, or e.g. jovyan) writes each file 0400 over docker exec; a
# root-owned 0700 dir would lock a non-root image's startup script out of its own secrets.
SECRETS_DIR = "/run/secrets"
SECRETS_TMPFS = ("--tmpfs", SECRETS_DIR + ":rw,noexec,nosuid,nodev,size=1m,mode=1777")
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def write_secrets(container: str, secrets: dict):
    """One `docker exec -i` per secret, the value on stdin (same pattern as the startup script)."""
    for name, value in secrets.items():
        if not SECRET_NAME.fullmatch(name):
            raise ValueError("invalid secret name")
        subprocess.run(["docker", "exec", "-i", container, "sh", "-c",
                        f"umask 277 && cat > {SECRETS_DIR}/{name}"],
                       input=value.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, check=True)


# Unattended startup script (template_params.startup_script, e.g. a scheduled rental).
STARTUP_LOG = "startup.log"
STARTUP_EXIT = "startup.exitcode"


def start_startup_script(container: str, workdir: str, script: str):
    """Run the buyer's script INSIDE their container, detached. `docker exec` inherits the
    container's own user, capabilities, network and egress policy, so the script can do nothing
    the template itself could not. It travels on stdin (never argv, env or an agent log); output
    goes to <workdir>/startup.log and the exit code to <workdir>/startup.exitcode."""
    q = shlex.quote(workdir)
    subprocess.run(["docker", "exec", "-i", container, "sh", "-c",
                    f"mkdir -p -- {q} && cd -- {q} && umask 077 && cat > .pb-startup.sh"],
                   input=script, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                   timeout=30, check=True)
    runner = (f"cd -- {q} || exit 1; if command -v bash >/dev/null 2>&1; then bash .pb-startup.sh;"
              f" else sh .pb-startup.sh; fi > {STARTUP_LOG} 2>&1 < /dev/null; echo $? > {STARTUP_EXIT}")
    subprocess.run(["docker", "exec", "-d", container, "sh", "-c", runner], text=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=True)


def startup_exit_code(container: str, workdir: str):
    """The script's exit code once it has finished, else None."""
    r = subprocess.run(["docker", "exec", container, "cat", workdir.rstrip("/") + "/" + STARTUP_EXIT],
                       capture_output=True, text=True, timeout=30, check=False)
    out = r.stdout.strip()
    return int(out) if r.returncode == 0 and out.isdigit() else None
