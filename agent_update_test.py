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


# Stand-in for curl: logs every URL, serves the signature, fails any bundle download.
FAKE_CURL = """#!/bin/sh
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
echo "$url" >> "$FAKE_LOG"
case "$url" in
  *.sig) cp "$FAKE_SIG" "$out" ;;
  *) exit 22 ;;
esac
"""


class AgentUpdateSkipTest(unittest.TestCase):
    """A tick whose signature matches the last applied bundle must not download the bundle at all
    (it runs 6-hourly on every node); a changed signature, or a first run, must still fetch it."""

    def _run(self, served_sig, applied_sig):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "bin").mkdir()
            (t / "state").mkdir()
            (t / "agent.env").write_text("PETABYTE_API_URL=https://api.test")
            (t / "served.sig").write_bytes(served_sig)
            if applied_sig is not None:
                (t / "state" / "bundle.sig").write_bytes(applied_sig)
                (t / "state" / "bundle.sha256").write_text("ab" * 32)
            (t / "bin" / "curl").write_text(FAKE_CURL)
            (t / "bin" / "curl").chmod(0o755)
            env = {"PATH": f"{t}/bin:/usr/bin:/bin", "PETABYTE_AGENT_STATE": str(t / "state"),
                   "PETABYTE_AGENT_ENV": str(t / "agent.env"),
                   "FAKE_LOG": str(t / "urls"), "FAKE_SIG": str(t / "served.sig")}
            r = subprocess.run(["bash", str(UPDATE_SH)], capture_output=True, text=True, env=env)
            urls = (t / "urls").read_text().split() if (t / "urls").exists() else []
            return r, urls

    @unittest.skipUnless(shutil.which("rsync"), "update.sh needs rsync")
    def test_unchanged_signature_skips_the_download(self):
        r, urls = self._run(b"S" * 64, b"S" * 64)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("signature unchanged", r.stdout)
        self.assertFalse(any(u.endswith("/agent.tar.gz") for u in urls), urls)

    @unittest.skipUnless(shutil.which("rsync"), "update.sh needs rsync")
    def test_new_signature_or_first_run_fetches_the_bundle(self):
        for applied in (b"O" * 64, None):
            r, urls = self._run(b"S" * 64, applied)
            self.assertNotIn("signature unchanged", r.stdout)
            self.assertTrue(any(u.endswith("/agent.tar.gz") for u in urls), urls)


if __name__ == "__main__":
    unittest.main()
