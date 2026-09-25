"""update.sh must actually DETECT a changed file. A dry-run `rsync -n` without -i/-v prints
nothing, so `grep -q .` would be false forever and a signed update would silently never apply
(root-run agent code stuck at the installed version). This guards that regression."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

UPDATE_SH = Path(__file__).resolve().parent / "update.sh"


class AgentUpdateDetectTest(unittest.TestCase):
    def test_dryrun_rsync_is_itemized(self):
        # The change-detection rsync feeding `grep -q .` must itemize (-i) or be verbose (-v);
        # otherwise it prints nothing and the update never applies.
        line = next(l for l in UPDATE_SH.read_text().splitlines()
                    if "rsync" in l and "grep -q" in l)
        flags = re.search(r"rsync\s+(-\S+)", line).group(1)
        self.assertIn("n", flags, f"detection rsync must be a dry-run: {line.strip()}")
        self.assertTrue("i" in flags or "v" in flags,
                        f"detection rsync must itemize/verbose, got: {line.strip()}")

    @unittest.skipUnless(shutil.which("rsync"), "rsync not installed")
    def test_rsync_flags_detect_change_and_ignore_identical(self):
        exclude = ["--exclude", ".venv", "--exclude", "*.env", "--exclude", "*.log",
                   "--exclude", "__pycache__", "--exclude", ".git"]
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "src", Path(tmp) / "dst"
            src.mkdir(); dst.mkdir()
            (src / "idle_mining.py").write_text("NAME = 'petabyte-idle-gpu'\n")
            (dst / "idle_mining.py").write_text("NAME = 'petabyte-idle-cpu'\n")

            def dryrun():
                return subprocess.run(["rsync", "-rcni", *exclude, f"{src}/", f"{dst}/"],
                                      capture_output=True, text=True, check=True).stdout

            self.assertTrue(dryrun().strip(), "a differing file must be detected")
            shutil.copy(src / "idle_mining.py", dst / "idle_mining.py")
            self.assertFalse(dryrun().strip(), "identical trees must report no change")


if __name__ == "__main__":
    unittest.main()
