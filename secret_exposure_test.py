# secret_exposure_test — buyer secrets must NOT appear on the docker run argv (ps/proc/cmdline leak).
# They go in a 0600 --env-file. Also asserts host networking is refused for cluster jobs. Offline.
import os
import tempfile
os.environ.setdefault("PETABYTE_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("PETABYTE_API_KEY", "x")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
# The honest-fail path signs the failure result (crypto.sign_proof), whose default key dir is
# /var/lib/petabyte-agent — writable on a real node, but not in the non-root release sandbox. Point
# it at a temp dir so this stays hermetic (set before task_fetcher imports crypto). Must precede the
# task_fetcher import below.
os.environ.setdefault("PETABYTE_AGENT_KEY", os.path.join(tempfile.mkdtemp(prefix="pb-agent-key-"), "key"))
F = []
def ok(l, c):
    print(("ok   " if c else "FAIL ") + l)
    if not c: F.append(l)
import task_fetcher as tf

SECRET = "sk-BUYER-TOKEN-must-not-hit-argv-123"
task = {"task_id": 1, "image": "alpine", "command": "echo hi",
        "env": {"HF_TOKEN": SECRET, "FOO": "bar"}}
argv = tf.build_container_cmd(task, name="pb-test")
joined = " ".join(argv)
ok("secret NOT on argv", SECRET not in joined)
ok("no -e flag used", "-e" not in argv)
ok("--env-file used", "--env-file" in argv)
ef = task.get("_env_file")
ok("env-file path recorded", bool(ef) and os.path.exists(ef))
if ef and os.path.exists(ef):
    mode = oct(os.stat(ef).st_mode & 0o777)
    ok("env-file is 0600", mode == "0o600")
    body = open(ef).read()
    ok("env-file contains the secret (KEY=VALUE)", ("HF_TOKEN=" + SECRET) in body)
    os.remove(ef)

# A2: cluster egress is host-net is refused even with the legacy opt-in
os.environ.pop("PB_ALLOW_HOST_NET_CLUSTER", None)
ok("cluster egress default is NOT --network host", tf._egress_flags({"egress": "cluster"}) == ["--network", "none"])
os.environ["PB_ALLOW_HOST_NET_CLUSTER"] = "true"
ok("legacy opt-in cannot enable host networking", tf._egress_flags({"egress": "cluster"}) == ["--network", "none"])
os.environ.pop("PB_ALLOW_HOST_NET_CLUSTER", None)

# Template launches use the same short-lived env-file policy.
from unittest.mock import patch
for value in ("value\nINJECTED=yes", "value\r", "value\x00"):
    with patch("tempfile.mkstemp") as create:
        try:
            tf._template_env_flags({}, {"TOKEN": value})
            ok("invalid environment rejected", False)
        except ValueError:
            ok("invalid environment rejected before file creation", not create.called)
template = {}
flags = tf._template_env_flags(template, {"TOKEN": SECRET})
path = template["_env_file"]
ok("template secret is absent from argv", SECRET not in " ".join(flags))
ok("template environment file mode is 0600", os.stat(path).st_mode & 0o777 == 0o600)
tf._remove_env_file(template)
ok("template environment file removed after handoff", not os.path.exists(path) and "_env_file" not in template)
tf._remove_env_file(template)  # cleanup is idempotent

# Exceptions after creating the env-file must not leave buyer plaintext behind.
failed_task = {"task_id": 4, "image": "alpine", "command": "true", "env": {"TOKEN": SECRET}}
created_paths = []
real_builder = tf.build_container_cmd

def recording_builder(task, **kwargs):
    argv = real_builder(task, **kwargs)
    created_paths.append(task["_env_file"])
    return argv

with patch.object(tf.shutil if hasattr(tf, "shutil") else __import__("shutil"), "which", return_value="docker"), \
     patch.object(tf, "build_container_cmd", side_effect=recording_builder), \
     patch.object(tf, "_run_streamed", side_effect=RuntimeError("synthetic launch failure")), \
     patch.object(tf, "report_progress"), patch.object(tf, "report_log"), \
     patch.object(tf, "_post"), patch.object(tf, "_set_ui"):
    tf._run_container(failed_task)
ok("failed container launch removes environment file", bool(created_paths)
   and all(not os.path.exists(path) for path in created_paths) and "_env_file" not in failed_task)

print("\n=== secret_exposure: %s ===" % ("0 failures" if not F else "%d FAILED" % len(F)))
raise SystemExit(1 if F else 0)
