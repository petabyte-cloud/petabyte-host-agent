"""No network or mining: lease lifecycle, process ownership and paid-work priority (GPU DOGE)."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import idle_mining as m

_DOGE = "D" * 34
_GPU = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_IMG = "sha256:" + "b" * 64


class MiningTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "mining"
        self.addCleanup(patch.stopall)
        patch.object(m, "STATE", self.state).start()
        self.now = 100
        patch.object(m, "boot_time", lambda: self.now).start()
        self.docker = patch.object(m, "docker", return_value=SimpleNamespace(returncode=0, stdout="")).start()
        self.env = {"PETABYTE_IDLE_MINING": "true", "DOGE_ADD": _DOGE,
                    "PETABYTE_MINING_WORKER": "pb-test",
                    "PETABYTE_GPU_MINING_IMAGE": _IMG,
                    "PETABYTE_MINING_GPU_UUID": _GPU,
                    "PETABYTE_DOGE_POOL_CONVERSION": "true"}
        patch.dict(os.environ, self.env, clear=True).start()
        self.c = m.Controller()
        self.c.prepare_gpu = lambda device: True

    def grant(self):
        self.c.heartbeat(self.c.ticket(), {"seconds": 30})

    def test_default_off_never_invokes_docker_or_writes_lease(self):
        os.environ.pop("PETABYTE_IDLE_MINING")
        self.grant()
        self.c.before_work()
        self.docker.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_gpu_container_sandboxed_and_scoped_to_one_device(self):
        self.grant()
        args = next(c.args for c in self.docker.call_args_list if c.args[0] == "run")
        for flag in ("--read-only", "--cap-drop=ALL", "--pull=never", "--restart=no"):
            self.assertIn(flag, args)
        self.assertIn("petabyte-idle-gpu", args)
        self.assertIn("device=" + _GPU, args)
        self.assertIn("FISHHASH", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertEqual((self.state / "until").read_text(), "130\n")

    def test_paid_work_invalidates_inflight_heartbeat(self):
        ticket = self.c.ticket()
        self.c.before_work()
        self.c.after_work()
        self.c.heartbeat(ticket, {"seconds": 30})
        self.assertFalse(self.state.exists())

    def test_busy_and_live_detached_vm_prevent_restart(self):
        self.c.before_work()
        self.grant()
        self.c.after_work()
        self.c.heartbeat(self.c.ticket(), {"seconds": 30}, live=True)
        self.assertFalse(any(c.args[0] == "run" for c in self.docker.call_args_list))

    def test_slow_heartbeat_does_not_extend_server_permit(self):
        ticket = self.c.ticket()
        self.now = 131
        self.c.heartbeat(ticket, {"seconds": 30})
        self.assertFalse(self.state.exists())

    def test_invalid_and_missing_permits_revoke(self):
        self.grant()
        for value in (None, {}, {"seconds": True}, {"seconds": 31}, {"seconds": -1}):
            self.c.heartbeat(self.c.ticket(), value)
            self.assertEqual((self.state / "until").read_text(), "0\n")

    def test_disable_survives_new_controller_and_does_not_stop_agent(self):
        self.grant()
        m.control("disable")
        self.assertFalse(m.Controller().enabled())
        self.docker.reset_mock()
        self.grant()
        self.assertFalse(any(c.args[0] == "run" for c in self.docker.call_args_list))

    def test_stop_failure_blocks_paid_execution(self):
        self.docker.return_value = SimpleNamespace(returncode=1, stdout="")
        with self.assertRaises(RuntimeError):
            self.c.before_work()
        self.assertTrue(self.c.busy)

    def test_restart_with_disabled_env_still_stops_old_miner(self):
        self.grant()
        os.environ.pop("PETABYTE_IDLE_MINING")
        self.docker.reset_mock()
        nc = m.Controller()
        nc.prepare_gpu = lambda device: True
        nc.before_work()
        self.assertTrue(self.docker.called)
        self.assertEqual((self.state / "until").read_text(), "0\n")

    def test_stop_targets_only_exact_labelled_owned_container(self):
        cid = "b" * 64
        self.docker.side_effect = [SimpleNamespace(returncode=0, stdout=cid),
                                  SimpleNamespace(returncode=0, stdout="")]
        m.stop()
        first = self.docker.call_args_list[0].args
        self.assertIn("name=^/petabyte-idle-(cpu|gpu)$", first)
        self.assertIn("label=market.petabyte.idle-miner=1", first)
        self.assertEqual(self.docker.call_args_list[1].args, ("rm", "-f", cid))

    def test_doge_requires_explicit_conversion_and_wallet(self):
        os.environ.pop("PETABYTE_DOGE_POOL_CONVERSION")
        with self.assertRaises(ValueError):
            m.configuration()
        os.environ["PETABYTE_DOGE_POOL_CONVERSION"] = "true"
        self.assertEqual(m.configuration()["user"], "DOGE:" + _DOGE + ".pb-test")

    def test_gpu_preparation_required_before_mining(self):
        # If VRAM prep is unavailable, GPU mining must NOT start (buyer isolation).
        self.c.prepare_gpu = lambda device: False
        with self.assertRaises(RuntimeError):
            self.grant()
        self.assertFalse(any(c.args[0] == "run" for c in self.docker.call_args_list))

    def test_invalid_config_never_launches(self):
        for key, value in (("PETABYTE_GPU_MINING_IMAGE", "unknown:latest"),
                           ("PETABYTE_MINING_GPU_UUID", "not-a-uuid"),
                           ("DOGE_ADD", "--help"),
                           ("PETABYTE_MINING_WORKER", "bad worker")):
            with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                self.grant()
        self.assertFalse(any(c.args[0] == "run" for c in self.docker.call_args_list))


if __name__ == "__main__":
    unittest.main()
