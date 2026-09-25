"""nb_fetch.py — pure helpers for the Colab-style /run notebook prefetch.

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
