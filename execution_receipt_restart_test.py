"""Real process restart, enrollment/key changes, expiry and corrupt persisted assignments."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pb-receipt-restart-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.key = self.root / "key"
        self.key.write_text(base64.b64encode(os.urandom(32)).decode())
        self.state = self.root / "assignments.json"
        self.env = dict(os.environ, PETABYTE_AGENT_KEY=str(self.key), PETABYTE_API_URL="https://synthetic.invalid",
                        PETABYTE_API_KEY="synthetic-enrollment", PETABYTE_SPEC_ID="42",
                        PETABYTE_RECEIPT_STATE=str(self.state), PYTHONDONTWRITEBYTECODE="1")
        self.call("r.remember({'task_id':7,'spec_id':42,'assignment':'server-bound-token'})")

    def call(self, code, **env):
        result = subprocess.run([sys.executable, "-c", "import execution_receipt as r;" + code],
                                cwd=ROOT, env=dict(self.env, **env), capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_assignment_survives_a_real_process_restart(self):
        result = json.loads(self.call("import json;print(json.dumps(r.make(7,result='done')))"))
        self.assertEqual(result["assignment"], "server-bound-token")
        self.assertEqual(result["spec_id"], 42)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-enrollment", self.state.read_text())
        self.assertNotIn(self.key.read_text(), self.state.read_text())

    def test_identity_endpoint_and_key_changes_invalidate_state(self):
        for changed in ({"PETABYTE_API_KEY":"another-key"}, {"PETABYTE_API_URL":"https://another.invalid"}, {"PETABYTE_SPEC_ID":"99"}):
            self.assertEqual(self.call("print(r.knows(7))", **changed), "False")
        self.key.write_text(base64.b64encode(os.urandom(32)).decode())
        self.assertEqual(self.call("print(r.knows(7))"), "False")

    def test_expired_future_and_corrupt_state_fail_closed(self):
        original = json.loads(self.state.read_text())
        for timestamp in (time.time()-32*86400, time.time()+86400):
            original["tasks"][0][1]["saved_at"] = timestamp
            self.state.write_text(json.dumps(original))
            self.assertEqual(self.call("print(r.knows(7))"), "False")
        self.state.write_text("malformed")
        self.assertEqual(self.call("print(r.knows(7))"), "False")

    def test_acknowledged_task_is_forgotten_durably(self):
        self.call("r.forget(7)")
        self.assertEqual(self.call("print(r.knows(7))"), "False")


if __name__ == "__main__":
    unittest.main()
