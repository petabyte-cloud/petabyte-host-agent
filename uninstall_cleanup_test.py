"""Execute uninstall with fake commands: preserve foreign disks and failed-cleanup evidence."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class UninstallTest(unittest.TestCase):
    def run_uninstall(self, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "commands.jsonl"
            sudo = root / "sudo"
            sudo.write_text('''#!/usr/bin/env python3
import json, os, sys
a=sys.argv[1:]
with open(os.environ["COMMAND_LOG"],"a") as f: f.write(json.dumps(a)+"\\n")
if a[0]=="python3" and os.getenv("FAIL_CLEANUP"): sys.exit(1)
if a[0] in ("iptables","ip6tables") and "-D" in a: sys.exit(1)
if a[0]=="losetup" and "-j" in a: print("/dev/loop77")
''')
            sudo.chmod(0o755)
            for name, text in {"docker": "exit 0", "mountpoint": "exit 0",
                               "findmnt": 'echo "${TEST_MOUNT_SOURCE:-/dev/mapper/pbscratch}"',
                               "iptables": "exit 0", "ip6tables": "exit 0", "losetup": "exit 0"}.items():
                p = root / name
                p.write_text("#!/bin/sh\n" + text + "\n")
                p.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + ":" + os.environ["PATH"], COMMAND_LOG=str(log), **extra)
            source = Path(__file__).with_name("uninstall.sh").read_text()
            # stdin also makes this fixture portable across CRLF Windows checkouts.
            result = subprocess.run(["bash"], input=source, text=True, capture_output=True, env=env)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            return result, commands

    def test_success_detaches_only_owned_backing_loop(self):
        result, commands = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["losetup", "-d", "/dev/loop77"], commands)
        self.assertFalse(any("-D" in c for c in commands if c[0] == "losetup"))
        cleanup = commands.index(["python3", "/opt/petabyte-agent/template_storage.py", "uninstall"])
        removal = commands.index(["rm", "-rf", "/var/lib/petabyte"])
        self.assertLess(cleanup, removal)

    def test_failed_image_cleanup_keeps_ledger_and_agent_files(self):
        result, commands = self.run_uninstall(FAIL_CLEANUP="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(["rm", "-rf", "/var/lib/petabyte"], commands)
        self.assertNotIn(["rm", "-rf", "/opt/petabyte-agent", "/etc/petabyte"], commands)

    def test_foreign_mount_is_never_unmounted_or_deleted(self):
        result, commands = self.run_uninstall(TEST_MOUNT_SOURCE="/dev/foreign")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] == "umount" for c in commands))
        self.assertNotIn(["rm", "-rf", "/var/lib/petabyte"], commands)


if __name__ == "__main__":
    unittest.main()
