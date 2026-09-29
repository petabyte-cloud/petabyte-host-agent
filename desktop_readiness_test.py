"""Do not bill a desktop merely because its login proxy answers HTTP."""
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("PETABYTE_API_URL", "http://localhost")
os.environ.setdefault("PETABYTE_API_KEY", "offline")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
import task_fetcher as tf
import execution_receipt


class DesktopReadiness(unittest.TestCase):
    def test_wireguard_readiness_requires_recent_expected_peer_handshake(self):
        import egress_vpn
        from unittest.mock import Mock
        with patch.dict(os.environ, PB_EGRESS_GATEWAY_PUBKEY="expected-peer"), \
             patch.object(egress_vpn, "enabled", return_value=True), \
             patch.object(egress_vpn.shutil, "which", return_value="wg"), \
             patch.object(egress_vpn.time, "time", return_value=1000):
            for stdout, expected in [("expected-peer\t990\n", True), ("different-peer\t990\n", False),
                                      ("expected-peer\t0\n", False), ("expected-peer\t800\n", False),
                                      ("expected-peer\t1100\n", False), ("expected-peer\tbad\n", False)]:
                with patch.object(egress_vpn, "_run", return_value=Mock(returncode=0, stdout=stdout)):
                    self.assertIs(egress_vpn.peer_ready(), expected)

    def test_native_udp_restores_exact_assignment_and_fails_without_wireguard(self):
        import egress_vpn
        endpoint = [dict(container_port=10014, protocol="udp", host_port=12345)]
        saved = dict(udp_bind="10.9.0.7", udp_gateway="10.9.0.1", udp_port=32000)
        with patch.dict(os.environ, PB_EGRESS_ADDR="10.9.0.7/32"), \
             patch.object(egress_vpn, "enabled", return_value=True), \
             patch.object(egress_vpn, "peer_ready", return_value=True), \
             patch.object(egress_vpn, "ensure_tunnel", return_value=True):
            self.assertEqual(tf._native_bridge_options(endpoint, saved), saved)
            for stale in [{**saved, "udp_bind": "10.9.0.8"}, {**saved, "udp_gateway": "169.254.169.254"},
                          {**saved, "udp_port": True}]:
                with self.assertRaises(ValueError):
                    tf._native_bridge_options(endpoint, stale)
            with patch.object(egress_vpn, "ensure_tunnel", return_value=False):
                with self.assertRaises(ValueError):
                    tf._native_bridge_options(endpoint)
            with patch.object(egress_vpn, "peer_ready", return_value=False), patch.object(tf.time, "sleep"):
                with self.assertRaises(ValueError):
                    tf._native_bridge_options(endpoint)
            rule = egress_vpn.service_udp_rule(32000)
            self.assertIn("wg-egress", rule)
            self.assertIn("10.9.0.1/32", rule)
            self.assertIn("10.9.0.7/32", rule)
            self.assertNotIn("0.0.0.0/0", rule)

    def test_native_listener_firewall_removed_on_failure_and_shutdown(self):
        import egress_vpn
        import port_bridge
        from unittest.mock import Mock
        bridge = Mock(udp_port=32000)
        bridge.start.side_effect = RuntimeError("thread capacity")
        with patch.object(port_bridge, "Bridge", return_value=bridge), \
             patch.object(egress_vpn, "service_udp_firewall") as firewall:
            with self.assertRaises(RuntimeError):
                tf._start_service_bridge([], "a" * 64, 4, {})
            firewall.assert_any_call(32000)
            firewall.assert_any_call(32000, remove=True)
            bridge.shutdown.assert_called_once()

    def test_bridge_start_failure_reports_failure_and_cleans_up(self):
        task = dict(task_id=42, template="custom", image="local-test", port=8080,
                    public_services=[dict(container_port=8080, protocol="tcp")],
                    service_credential="a" * 64)
        with patch.object(tf, "_reverse_tunnel_enabled", return_value=True), \
             patch.object(tf, "_set_ui"), patch.object(tf, "_restore_volume"), \
             patch.object(tf, "_start_backup_thread"), patch.object(tf, "_free_host_port", return_value=18000), \
             patch("shutil.which", return_value="docker"), \
             patch("port_bridge.Bridge.start", side_effect=OSError("no listener capacity")), \
             patch.object(tf, "report_log"), patch.object(tf, "report_progress"), \
             patch.object(tf, "_post") as post, patch.object(tf, "_post_result_ack_retry") as result, \
             patch.object(tf, "_signed_result", return_value={"status": "failed"}), \
             patch.object(tf, "_cleanup_job_resources") as cleanup:
            tf._run_template(task)
        self.assertEqual(post.call_args.args[1]["status"], "failed")
        result.assert_called_once()
        cleanup.assert_called_once()

    def test_authenticated_page_waits_for_kde(self):
        tf._pb_vm_watch[41] = {}
        posts = []
        with patch.object(tf.httpx, "get", return_value=SimpleNamespace(status_code=200)) as http, \
             patch.object(tf.subprocess, "run", side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as docker, \
             patch.object(tf.time, "sleep"), patch.object(tf, "_post", side_effect=lambda *a: posts.append(a)), \
             patch.object(tf, "report_log"):
            tf._await_ready(41, "pb-fedora-kde-owned", 18000, "/", ["petabyte", "secret"], "plasmashell")
        tf._pb_vm_watch.pop(41)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(docker.call_count, 2)
        self.assertEqual(http.call_args.kwargs["auth"], ("petabyte", "secret"))
        self.assertEqual([p[1]["status"] for p in posts], ["ready"])

    def test_bridge_restore_rejects_public_file_and_stale_assignment(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bridge.json"
            path.write_text(json.dumps({"41": dict(container="owned", generation=4, endpoints=[], token="a"*64)}))
            path.chmod(0o644)
            with patch.object(tf, "_PORT_BRIDGES_FILE", str(path)), \
                 patch.object(execution_receipt, "knows", return_value=True), \
                 patch.object(execution_receipt, "generation", return_value=5), \
                 patch.object(tf.subprocess, "run") as inspect:
                self.assertIsNone(tf._restore_port_bridge(41, "owned"))
                path.chmod(0o600)
                self.assertIsNone(tf._restore_port_bridge(41, "owned"))
                self.assertFalse(inspect.called)

if __name__ == "__main__":
    unittest.main()
