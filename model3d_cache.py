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

    def report(self):
        with self.lock:
            return self.report_unlocked()

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

        def prepare():
            import inference_worker
            try:
                path = inference_worker.fetch_model(pin, cancelled=lambda: not idle(), root=self.root, evict=False)
                with self.lock:
                    self.ready[pin["sha256"]] = (Path(path), pin["size"])
            except inference_worker.Cancelled:
                pass
            except Exception as exc:  # optional preparation never breaks heartbeat liveness
                logging.warning("Create 3D model preparation paused: %s", type(exc).__name__)
                with self.lock:
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
