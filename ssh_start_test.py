"""Buyer SSH on the agent: key injection + the image's own sshd, inside the container only.

The agent writes the buyer's PUBLIC key into the container and, for a custom image launched with
ssh, starts that image's sshd publickey-only. It must stay a `docker exec` into the buyer's own
container (no host mount, no privileged flag, no new caps) and the container's isolation flags must
be the same cap-drop ALL + no-new-privileges set the shell template already runs with.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "offline")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf
import template_storage

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOmm6X1kvR0k2bE0n6Dn2sJ6l0p5dYq3i9fT0rCw1xyz buyer@laptop"
SSH_CAPS = ["SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "FOWNER", "SYS_CHROOT", "AUDIT_WRITE"]


class InjectTest(unittest.TestCase):
    def run_inject(self, start, rc=0):
        with patch.object(tf.subprocess, "run", return_value=SimpleNamespace(returncode=rc)) as run:
            got = tf._inject_ssh_key("pb-custom-1234", KEY, start_sshd=start)
        return got, run.call_args.args[0]

    def test_start_sshd_argv_is_a_container_exec_only(self):
        rc, argv = self.run_inject(True)
        self.assertEqual(rc, 0)
        self.assertEqual(argv[:6], ["docker", "exec", "-u", "0", "-e", f"PB_KEY={KEY}"])
        self.assertEqual(argv[6:9], ["pb-custom-1234", "sh", "-c"])
        for flag in ("-v", "--volume", "--mount", "--privileged", "--cap-add", "--pid", "--network"):
            self.assertNotIn(flag, argv)
        script = argv[9]
        self.assertIn('"$PB_KEY" >> /root/.ssh/authorized_keys', script)    # key from env, never in sh text
        self.assertNotIn(KEY, script)
        self.assertIn("/usr/sbin/sshd -o AuthenticationMethods=publickey", script)
        self.assertIn("PasswordAuthentication=no", script)
        self.assertIn("PermitRootLogin=prohibit-password", script)
        self.assertIn("exit 3", script)                                      # no sshd in the image

    def test_plain_inject_never_starts_sshd(self):
        _, argv = self.run_inject(False)
        self.assertNotIn("sshd", argv[-1])

    def test_no_key_runs_nothing(self):
        with patch.object(tf.subprocess, "run") as run:
            self.assertIsNone(tf._inject_ssh_key("c", "", start_sshd=True))
        run.assert_not_called()

    def test_missing_sshd_is_reported(self):
        rc, _ = self.run_inject(True, rc=3)
        self.assertEqual(rc, 3)


class IsolationTest(unittest.TestCase):
    def test_ssh_caps_keep_the_sandbox(self):
        with patch.object(tf.subprocess, "check_output", return_value="map[runc:{runc []}]"):
            flags = tf._isolation_flags({"task_type": "template", "template": "custom", "init_caps": SSH_CAPS})
        self.assertEqual(flags[flags.index("--cap-drop") + 1], "ALL")
        self.assertIn("no-new-privileges", flags)
        added = [flags[i + 1] for i, f in enumerate(flags) if f == "--cap-add"]
        self.assertEqual(sorted(added), sorted(SSH_CAPS))
        for flag in ("--privileged", "--mount", "-v"):
            self.assertNotIn(flag, flags)


class RunTemplateTest(unittest.TestCase):
    def launch(self, task, inject_rc=0):
        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch.object(tf.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="cid", stderr="")) as run, \
             patch.object(tf, "_set_ui"), patch.object(tf, "_restore_volume"), \
             patch.object(tf, "_start_backup_thread"), patch.object(tf, "_free_host_port", return_value=18000), \
             patch.object(tf, "_reverse_tunnel_enabled", return_value=True), \
             patch.object(tf, "_open_reverse_tunnel", return_value=21000), \
             patch.object(tf, "_ensure_job_network", return_value=("pb-net-t7", None)), \
             patch.object(tf, "report_progress"), patch.object(tf, "_isolation_flags", return_value=["--cap-drop", "ALL"]), \
             patch.object(tf.template_storage, "prepare", return_value="cached"), \
             patch.object(tf, "_register_vm"), patch.object(tf, "_start_storage_guard"), \
             patch.object(tf, "_register_vm_tunnel", return_value=True), patch.object(tf, "_supervise_tunnel"), \
             patch.object(tf, "_post"), patch.object(tf, "report_log") as log, \
             patch.object(tf, "_inject_ssh_key", return_value=inject_rc) as inject:
            tf._run_template(task)
        docker_run = next(c.args[0] for c in run.call_args_list if c.args[0][:2] == ["docker", "run"])
        return inject, log, docker_run

    def task(self, **kw):
        return dict(task_id=7, template="custom", image="local/img", port=22, vm_id="vmabc",
                    ssh_pubkey=KEY, ssh_start=True, egress="limited", **kw)

    def test_custom_ssh_starts_sshd_and_publishes_only_on_loopback(self):
        inject, log, docker_run = self.launch(self.task())
        inject.assert_called_once()
        self.assertTrue(inject.call_args.kwargs["start_sshd"])
        self.assertIn("127.0.0.1:18000:22", " ".join(docker_run))           # nothing on the public NIC
        for flag in ("--privileged", "--mount", "-v"):
            self.assertNotIn(flag, docker_run)

    def test_image_without_sshd_is_logged_for_the_buyer(self):
        _, log, _ = self.launch(self.task(), inject_rc=3)
        self.assertTrue(any("openssh-server" in c.args[1] for c in log.call_args_list))

    def test_only_custom_may_ask_for_sshd_start(self):
        inject, _, _ = self.launch({**self.task(), "template": "ollama"})
        self.assertFalse(inject.call_args.kwargs["start_sshd"])
        inject, _, _ = self.launch({**self.task(), "ssh_start": "true"})
        self.assertFalse(inject.call_args.kwargs["start_sshd"])


class CapabilityReportTest(unittest.TestCase):
    def test_report_advertises_ssh_start(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(template_storage, "STATE", Path(root) / "images.json"), \
             patch.object(template_storage, "inspect", return_value=None), \
             patch.object(template_storage, "free_bytes", return_value=10 ** 9), \
             patch.object(template_storage, "policy", return_value=(10 ** 9, 10 ** 8)):
            report = template_storage.report()
        self.assertEqual((report or {}).get("ssh_start_version"), 1, report)


if __name__ == "__main__":
    unittest.main()
