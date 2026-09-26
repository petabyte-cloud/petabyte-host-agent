"""Exercise the shipped shell policy against fresh and pre-existing configuration.

The property under test is a consent guard, not a formatting detail: a seller who sets
PETABYTE_IDLE_MINING=false must actually stop mining, including on a machine an earlier install
already configured to mine. So the opt-out has to clear the settings that install left behind, not
merely decline to add new ones.

Only the installers sellers actually receive are checked. Committed copies under
lumaris_api/installers/ were retired in f66abe6 precisely because they drifted from the real script
and a change to them never reached a single seller (see lumaris_api/installers/README.md); asserting
against a path that no longer exists is how this suite spent its life erroring instead of guarding.
INSTALLERS is derived from what is on disk so that a future move fails loudly here rather than
quietly reducing the guard to nothing.

Run on Linux/WSL: python3 installer_policy_test.py. No installation is performed.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]

# The canonical node installers — the only copy edited, served and shipped (installers/README.md).
SH_INSTALLERS = (ROOT / "lumaris_agent/install.sh",)
PS1_INSTALLERS = (ROOT / "lumaris_agent/install.ps1",)


class MiningOptOutTest(unittest.TestCase):
    def test_installers_exist(self):
        """A missing installer must fail as a missing guard, not as an incidental traceback.

        This suite asserted against lumaris_api/installers/install.sh for as long as that file had
        been gone, so every run ended in FileNotFoundError from inside the real assertion — and
        because no workflow ran it, nobody saw the difference between "the opt-out is broken" and
        "the test cannot find the installer". Check the premise first and say which one it is.
        """
        for path in SH_INSTALLERS + PS1_INSTALLERS:
            self.assertTrue(path.is_file(),
                            f"{path} is missing, so the mining opt-out is unguarded. If the "
                            f"installers moved, update SH_INSTALLERS/PS1_INSTALLERS here — do not "
                            f"delete the assertion.")

    def test_explicit_opt_out_overrides_existing_config(self):
        for path in SH_INSTALLERS:
            source = path.read_text()
            marker = '_mine="${PETABYTE_IDLE_MINING:-true}"'
            self.assertIn(marker, source, f"{path} no longer reads PETABYTE_IDLE_MINING the way "
                                          f"this test extracts it; the block below would be empty "
                                          f"and would assert nothing.")
            block = marker + source.split(marker, 1)[1].split("# Selling schedule:", 1)[0]
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

    def test_windows_entrypoint_forwards_policy(self):
        for path in PS1_INSTALLERS:
            source = path.read_text()
            self.assertIn("export PETABYTE_IDLE_MINING=", source, str(path))


if __name__ == "__main__":
    unittest.main()
