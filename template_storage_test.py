"""Exercise image ownership and admission without touching a real Docker daemon."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import template_storage as storage


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = patch.object(storage, "STATE", Path(self.tmp.name) / "images.json")
        self.state.start()
        self.addCleanup(self.state.stop)
        self.images = {}
        self.active = set()
        self.calls = []
        self.docker = patch.object(storage, "docker", self.command)
        self.docker.start()
        self.addCleanup(self.docker.stop)
        self.free = patch.object(storage, "free_bytes", return_value=100 * storage.GIB)
        self.free.start()
        self.addCleanup(self.free.stop)
        self.policy = patch.object(storage, "policy", return_value=(30 * storage.GIB, 10 * storage.GIB))
        self.policy.start()
        self.addCleanup(self.policy.stop)

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("image", "inspect"):
            value = self.images.get(args[2])
            if not value:
                value = next((x for x in self.images.values() if x["Id"] == args[2]), None)
            if not value:
                raise subprocess.CalledProcessError(1, args)
            return json.dumps([value])
        if args[:2] == ("image", "ls"):
            return "\n".join(x["Id"] for x in self.images.values())
        if args[0] == "pull":
            self.images[args[1]] = {"Id": "sha256:new", "Size": storage.GIB}
        if args[0] == "ps":
            return "container" if args[-1].split("=", 1)[-1] in self.active else ""
        if args[:2] == ("image", "rm"):
            self.images = {k: v for k, v in self.images.items() if v["Id"] != args[2]}
        return ""

    def test_cache_miss_refuses_without_pull(self):
        with self.assertRaisesRegex(RuntimeError, "not cached"):
            storage.prepare("ollama@sha256:a", 1, cached_only=True)
        self.assertFalse(any(x[0] == "pull" for x in self.calls))

    def test_preexisting_image_is_never_owned_or_deleted(self):
        self.images["image"] = {"Id": "sha256:existing", "Size": storage.GIB}
        self.assertEqual(storage.prepare("image", 5), "cached")
        self.assertEqual(storage.read()["images"], {})
        storage.uninstall()
        self.assertIn("image", self.images)

    def test_another_tag_with_same_id_is_never_owned(self):
        self.images["other"] = {"Id": "sha256:new", "Size": storage.GIB}
        storage.prepare("image", 5)
        self.assertEqual(storage.read()["images"], {})

    def test_download_audit_and_cleanup(self):
        self.assertEqual(storage.prepare("image", 42), "downloaded")
        self.assertEqual(storage.prepare("image", 43), "cached")
        self.assertEqual([x["task_id"] for x in storage.read()["events"]], [42, 43])
        storage.uninstall()
        self.assertFalse(self.images)

    def test_low_free_disk_and_zero_budget_refuse_before_download(self):
        for budget, free in ((0, 100 * storage.GIB), (30 * storage.GIB, 1)):
            with patch.object(storage, "policy", return_value=(budget, 10 * storage.GIB)), \
                    patch.object(storage, "free_bytes", return_value=free):
                with self.assertRaisesRegex(RuntimeError, "prevents"):
                    storage.prepare("image", 1)
        self.assertFalse(any(x[0] == "pull" for x in self.calls))

    def test_oversized_download_is_owned_then_removed(self):
        with patch.object(storage, "policy", return_value=(1, 10 * storage.GIB)):
            with self.assertRaisesRegex(RuntimeError, "exceeded"):
                storage.prepare("image", 1)
        self.assertFalse(self.images)

    def test_old_unused_cache_can_make_room_for_new_image(self):
        self.images["old"] = {"Id":"sha256:old", "Size": storage.GIB}
        with storage.locked():
            storage.save({"images":{"sha256:old":{"ref":"old", "bytes":storage.GIB,
                         "used_at":0, "task_id":1}},"events":[]})
        with patch.object(storage, "policy", return_value=(storage.GIB, 10 * storage.GIB)):
            self.assertEqual(storage.prepare("new", 2), "downloaded")
        self.assertNotIn("old", self.images)
        self.assertIn("new", self.images)
        self.assertEqual(storage.STATE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(storage.STATE.with_suffix(".lock").stat().st_mode & 0o777, 0o600)

    def test_active_image_preserved_and_uninstall_reports_incomplete(self):
        storage.prepare("image", 1)
        self.active.add("sha256:new")
        with self.assertRaisesRegex(RuntimeError, "ledger retained"):
            storage.uninstall()
        self.assertIn("sha256:new", storage.read()["images"])

    def test_runtime_guard_enforces_private_volume_and_reserve(self):
        original = self.command
        def command(*args, **kw):
            if args[:2] == ("volume", "inspect"):
                return json.dumps([{"Labels": {"pb.task":"7"}, "Mountpoint":"/private-volume"}])
            return original(*args, **kw)
        with patch.object(storage, "docker", command), patch.object(storage.subprocess, "run") as run:
            run.return_value.stdout = str(21 * storage.GIB) + " /private-volume"
            self.assertEqual(storage.job_violation("private"), "Seller per-rental data budget exceeded")
            self.assertEqual(run.call_args.args[0], ["du", "-sb", "--", "/private-volume"])
        with patch.object(storage, "free_bytes", return_value=1):
            self.assertEqual(storage.job_violation(), "Seller free disk reserve reached")
        with patch.object(storage, "docker", return_value=json.dumps([{"Labels":{},"Mountpoint":"/foreign"}])):
            with self.assertRaisesRegex(RuntimeError, "unowned"):
                storage.job_violation("foreign")

    def test_heartbeat_never_waits_for_storage_lock(self):
        held = threading.Event()
        release = threading.Event()
        def hold():
            with storage.locked():
                held.set()
                release.wait(3)
        worker = threading.Thread(target=hold)
        worker.start()
        held.wait(1)
        start = time.monotonic()
        storage.heartbeat_report()
        elapsed = time.monotonic() - start
        release.set()
        worker.join()
        self.assertLess(elapsed, 0.1)
        storage._PROBE.join(2)


if __name__ == "__main__":
    unittest.main()
