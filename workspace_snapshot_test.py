"""Offline round-trip of a real workspace archive, encryption and failed recovery."""
import hashlib
import io
import json
import os
import subprocess
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import httpx
from cryptography.fernet import Fernet
import task_fetcher as tf
import workspace_snapshot as ws


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        self.target = Path(self.temp.name) / "target"
        self.source.mkdir(); self.target.mkdir()
        (self.source / "scene.blend").write_bytes(b"saved scene")
        (self.source / "frame0253.png").write_bytes(b"completed frame")
        self.task = {"task_id": 42, "task_type": "template", "template": "blender",
                     "cache": "/config", "volume": "blender-data", "lease_generation": 1}
        self.key = Fernet.generate_key().decode()
        self.upload = None
        self.checkpoints = []
        getattr(tf, "_BACKUP_NOTICES", {}).clear()

    def response(self, value, code=200):
        return httpx.Response(code, json=value, request=httpx.Request("POST", "http://localhost"))

    def post(self, url, **kw):
        if url.endswith("/backup_url"):
            return self.response({"enc_key": self.key, "upload_url": "http://localhost/upload", "snapshot_ref": "backups/1/42/snapshot"})
        if url.endswith("/restore_url"):
            return self.response({"enc_key": self.key, "download_url": "http://localhost/download",
                                  "content_hash": hashlib.sha256(self.upload).hexdigest()})
        if url.endswith("/checkpoint"):
            self.checkpoints.append(kw["json"])
        return self.response({})

    def put(self, url, **kw):
        self.upload = kw["content"]
        return self.response({})

    def test_saved_scene_and_frames_restore_to_actual_docker_workspace(self):
        with patch.object(ws, "directory", side_effect=[str(self.source), str(self.target)]), \
             patch.object(tf.httpx, "post", side_effect=self.post), \
             patch.object(tf.httpx, "put", side_effect=self.put), \
             patch.object(tf.safe_fetch, "get", side_effect=lambda *a, **k: httpx.Response(200, content=self.upload)), \
             patch.object(tf, "_lease_payload", side_effect=lambda p: {**p, "lease_generation": 1}), \
             patch.object(tf.crypto, "sign_proof", return_value="signature"), \
             patch.object(tf, "report_log"):
            tf._backup_once(self.task, "blender-data")
            self.assertEqual(len(self.checkpoints), 1)
            self.assertEqual(self.checkpoints[0]["lease_generation"], 1)
            self.assertTrue(tf._restore_volume("blender-data", "backups/1/42/snapshot", 42, self.task))
        self.assertEqual((self.target / "scene.blend").read_bytes(), b"saved scene")
        self.assertEqual((self.target / "frame0253.png").read_bytes(), b"completed frame")

    def test_failed_upload_never_records_a_checkpoint(self):
        with patch.object(ws, "directory", return_value=str(self.source)), \
             patch.object(tf.httpx, "post", side_effect=self.post), \
             patch.object(tf.httpx, "put", return_value=self.response({}, 503)), \
             patch.object(tf, "report_log"):
            tf._backup_once(self.task, "blender-data")
        self.assertEqual(self.checkpoints, [])

    def backup(self, put=None):
        with patch.object(ws, "directory", return_value=str(self.source)), \
             patch.object(tf.httpx, "post", side_effect=self.post), \
             patch.object(tf.httpx, "put", side_effect=put or self.put), \
             patch.object(tf, "_lease_payload", side_effect=lambda p: p), \
             patch.object(tf.crypto, "sign_proof", return_value="signature"), \
             patch.object(tf, "report_log") as log:
            tf._backup_once(self.task, "jupyter-data")
        return [c.args[1] for c in log.call_args_list]

    def test_venv_and_model_no_longer_silently_stop_notebook_backups(self):
        # Task 673: pip-installed torch + a downloaded model in the Jupyter work dir pushed the
        # archive past 128 MiB; every later backup raised and was only logged on the host.
        def sparse(rel, size):
            path = self.source / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb") as f:
                f.truncate(size)
        sparse("venv/lib/python3.11/site-packages/torch/libtorch_cuda.so", 40 * 1024 ** 2)
        sparse(".cache/huggingface/hub/model.safetensors", 40 * 1024 ** 2)
        sparse("model.bin", 95 * 1024 ** 2)   # > the 90 MB budget
        (self.source / "analysis.ipynb").write_bytes(b'{"cells": []}')
        (self.source / "--checkpoint-action=exec=touch pwned").write_bytes(b"just a file")
        lines = self.backup()
        self.assertEqual(len(self.checkpoints), 1, lines)
        self.assertLess(len(self.upload), 128 * 1024 ** 2)
        self.assertTrue(any("model.bin" in l and "NOT protected" in l for l in lines), lines)
        self.assertTrue(any("site-packages" in l and ".cache" in l for l in lines), lines)
        with patch.object(ws, "directory", return_value=str(self.target)), \
             patch.object(tf.httpx, "post", side_effect=self.post), \
             patch.object(tf.safe_fetch, "get", side_effect=lambda *a, **k: httpx.Response(200, content=self.upload)), \
             patch.object(tf, "report_log"):
            self.assertTrue(tf._restore_volume("jupyter-data", "backups/1/42/snapshot", 42, self.task))
        self.assertEqual((self.target / "analysis.ipynb").read_bytes(), b'{"cells": []}')
        self.assertTrue((self.target / "scene.blend").exists())
        self.assertTrue((self.target / "--checkpoint-action=exec=touch pwned").exists())
        self.assertTrue((self.target / "venv/lib/python3.11").is_dir())
        self.assertFalse((self.target / "venv/lib/python3.11/site-packages").exists())
        self.assertFalse((self.target / ".cache").exists())
        self.assertFalse((self.target / "model.bin").exists())
        self.assertFalse(Path("pwned").exists())

    def test_failed_backup_tells_the_buyer_once_then_resumed(self):
        failing = lambda url, **kw: self.response({}, 503)
        first, second = self.backup(failing), self.backup(failing)
        self.assertEqual(self.checkpoints, [])
        self.assertTrue(any("WORKSPACE BACKUP FAILED" in l for l in first), first)
        self.assertFalse(any("localhost/upload" in l for l in first), first)  # no presigned URL
        self.assertEqual(second, [])                                         # rate-limited
        ok = self.backup()
        self.assertEqual(len(self.checkpoints), 1)
        self.assertIn("workspace backups resumed", ok)

    def test_rejected_checkpoint_is_not_reported_as_a_backup(self):
        post = self.post
        self.post = lambda url, **kw: (self.response({}, 409) if url.endswith("/checkpoint")
                                       else post(url, **kw))
        lines = self.backup()
        self.assertFalse(any(l.startswith("backup ->") for l in lines), lines)
        self.assertTrue(any("WORKSPACE BACKUP FAILED" in l for l in lines), lines)

    def test_wrong_task_volume_is_refused(self):
        def docker(args, **kw):
            return subprocess.CompletedProcess(args, 0, json.dumps([{
                "Mountpoint": str(self.source), "Labels": {"pb.task": "41"}}]), "")
        with patch.object(ws.subprocess, "run", side_effect=docker):
            with self.assertRaises(ValueError):
                ws.directory(self.task, "blender-data")

    def test_tampered_hash_and_traversal_cannot_restore_outside_workspace(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            member = tarfile.TarInfo("../escaped")
            member.size = 4
            tar.addfile(member, io.BytesIO(b"bad!"))
        self.upload = Fernet(self.key.encode()).encrypt(archive.getvalue())
        with patch.object(ws, "directory", return_value=str(self.target)), \
             patch.object(tf.httpx, "post", side_effect=self.post), \
             patch.object(tf.safe_fetch, "get", return_value=httpx.Response(200, content=self.upload)), \
             patch.object(tf, "report_log"):
            self.assertFalse(tf._restore_volume("blender-data", "checkpoint", 42, self.task))
        self.assertFalse((self.target.parent / "escaped").exists())
        with patch.object(ws, "directory") as directory, \
             patch.object(tf.httpx, "post", side_effect=self.post), \
             patch.object(tf.safe_fetch, "get", return_value=httpx.Response(200, content=b"tampered")), \
             patch.object(tf, "report_log"):
            self.assertFalse(tf._restore_volume("blender-data", "checkpoint", 42, self.task))
        directory.assert_not_called()

    def test_failed_restore_does_not_start_empty_desktop(self):
        task = {**self.task, "restore_from": "checkpoint", "port": 3000}
        with patch.object(tf, "_reverse_tunnel_enabled", return_value=True), \
             patch.object(tf, "_restore_volume", return_value=False), \
             patch.object(tf, "_cleanup_job_resources"), patch.object(tf, "_post") as post, \
             patch.object(tf, "report_log"), patch.object(tf, "_set_ui"), \
             patch.object(subprocess, "run") as docker:
            tf._run_template(task)
        docker.assert_not_called()
        self.assertEqual(post.call_args.args[1]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
