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
