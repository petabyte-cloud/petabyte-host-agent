"""Exercise the shipped shell policy against fresh and pre-existing configuration.

Run on Linux/WSL: python3 installer_policy_test.py. No installation is performed.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class MiningOptOutTest(unittest.TestCase):
    def test_explicit_opt_out_overrides_existing_config(self):
        for path in (ROOT / "lumaris_agent/install.sh", ROOT / "lumaris_api/installers/install.sh"):
            source = path.read_text()
            if path.parent.name == "lumaris_agent":
                block = source.split('_mine="${PETABYTE_IDLE_MINING:-true}"', 1)[1]
                block = '_mine="${PETABYTE_IDLE_MINING:-true}"' + block.split("# Selling schedule:", 1)[0]
            else:
                block = source.split("# Honor the native connector's opt-out", 1)[1]
                block = block[block.index("if ["):].split("# Turn on the Kata runtime", 1)[0]
            for initial in ("", "PETABYTE_IDLE_MINING=true\nDOGE_ADD=old-wallet\nUNRELATED=keep\n"):
                with self.subTest(path=path, existing=bool(initial)), tempfile.TemporaryDirectory() as tmp:
                    envf = Path(tmp) / "agent.env"
                    envf.write_text(initial)
                    env = {**os.environ, "ENVF": str(envf), "PETABYTE_IDLE_MINING": "false"}
                    subprocess.run(["bash", "-euc", block], env=env, check=True)
                    result = envf.read_text().splitlines()
                    self.assertEqual(result.count("PETABYTE_IDLE_MINING=false"), 1)
                    self.assertIn("DOGE_ADD=", result)
                    self.assertNotIn("PETABYTE_IDLE_MINING=true", result)
                    self.assertNotIn("DOGE_ADD=old-wallet", result)
                    if initial:
                        self.assertIn("UNRELATED=keep", result)

    def test_both_windows_entrypoints_forward_policy(self):
        for path in (ROOT / "lumaris_agent/install.ps1", ROOT / "lumaris_api/installers/install.ps1"):
            source = path.read_text()
            self.assertIn("export PETABYTE_IDLE_MINING=", source, str(path))


if __name__ == "__main__":
    unittest.main()
