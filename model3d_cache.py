"""Prepare server-pinned scene models while idle, before the buyer's booking starts."""
import logging
from pathlib import Path
import re
import threading
import time


class ModelCache:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.running = False
        self.retry_at = 0
        self.ready = {}
        self.pin = None
        self.error = None

    def report(self):
        with self.lock:
            return self.report_unlocked()

    def status(self):
        """Public model identity and byte counts only; never paths, URLs or exception text."""
        with self.lock:
            if not self.pin:
                return None
            sha, size = self.pin['sha256'], self.pin['size']
            ready = sha in self.report_unlocked()
            try:
                downloaded = size if ready else min(size, (self.root / (sha + '.part')).stat().st_size)
            except OSError:
                downloaded = 0
            return {'sha256': sha, 'size': size, 'downloaded': downloaded,
                    'state': 'ready' if ready else 'preparing' if self.running else 'error' if self.error else 'paused',
                    'error': self.error}

    def cached(self, pin):
        import inference_worker
        path = inference_worker._target(pin, self.root)
        return path if path.is_file() and path.stat().st_size == pin["size"] else None

    def warm(self, pin, idle):
        if (not isinstance(pin, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(pin.get("sha256", "")))
                or type(pin.get("size")) is not int or not 0 < pin["size"] < 30 * 1024 ** 3
                or not str(pin.get("url", "")).startswith("https://huggingface.co/") or not idle()):
            return
        pin = dict(pin)
        with self.lock:
            if self.running or time.monotonic() < self.retry_at or pin["sha256"] in self.report_unlocked():
                return
            self.running = True
            self.pin = {'sha256': pin['sha256'], 'size': pin['size']}
            self.error = None

        def prepare():
            import inference_worker
            try:
                path = inference_worker.fetch_model(pin, cancelled=lambda: not idle(), root=self.root, evict=False)
                with self.lock:
                    self.ready[pin["sha256"]] = (Path(path), pin["size"])
            except inference_worker.Cancelled:
                pass
            except Exception as exc:  # optional preparation never breaks heartbeat liveness
                reason = str(exc)
                error = ('insufficient_disk' if reason.startswith('not enough free disk') else
                         'download_failed' if reason.startswith('model download failed') else
                         'checksum_failed' if reason.startswith('inference model failed its hash check') else
                         'preparation_failed')
                logging.warning("Create 3D model preparation paused: %s", error)
                with self.lock:
                    self.error = error
                    self.retry_at = time.monotonic() + 300
            finally:
                with self.lock:
                    self.running = False
        threading.Thread(target=prepare, daemon=True, name="pb-model3d-cache").start()

    def report_unlocked(self):
        # Called while holding lock. A deleted cache must trigger preparation again.
        result = []
        for sha, (path, size) in self.ready.items():
            try:
                if path.is_file() and path.stat().st_size == size:
                    result.append(sha)
            except OSError:
                pass
        return result
