"""diagnostics_test.py — the report a failing node sends support is masked, bounded, opt-out-able,
and never carries agent.env contents. No docker/GPU needed: subprocess and HTTP are stubbed.
Run: python diagnostics_test.py
"""
import json
import os
import subprocess
import tempfile

import diagnostics as d

_fail = 0


def ok(name, cond):
    global _fail
    print(("ok   " if cond else "FAIL ") + name)
    _fail += 0 if cond else 1


for _k in ("PETABYTE_API_URL", "PETABYTE_API_KEY", "PETABYTE_SPEC_ID", "PB_SHARE_DIAGNOSTICS"):
    os.environ.pop(_k, None)
SECRET_KEY = "pbk_" + "A1b2C3d4" * 6
env_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".env")
env_file.write(f"PETABYTE_API_URL=https://petabyte.example\nPETABYTE_API_KEY={SECRET_KEY}\n"
               "PETABYTE_SPEC_ID=45\n")
env_file.close()
d.ENV_FILE = env_file.name

leaky = ("NVRM: loading NVIDIA UNIX Open Kernel Module 615.71.09 (root@Sebastian-PC)\n"
         "eth0: RTL8125B, 34:5a:60:9d:f6:42, XID 641\n"
         "heartbeat to 203.0.113.7 from 2405:201:7001:703c::1 as seller@example.com\n"
         f"api_key={SECRET_KEY}\nAuthorization: Bearer abc.def.ghi\n"
         " Name: Sebastian-PC\n HTTPS Proxy: http://bob:hunter2@proxy.local:3128\n"
         "CUDA initialization: CUDA unknown error\n")
ran = []


class _R:
    def __init__(self, out):
        self.stdout, self.stderr, self.returncode = out, "", 0


real_run = subprocess.run
subprocess.run = lambda cmd, **k: ran.append(cmd) or _R(leaky)
try:
    report = d.collect("startup GPU container self-test failed", "45", gpu_test=True)
finally:
    subprocess.run = real_run

ok("keeps the evidence support needs", "CUDA unknown error" in report and "615.71.09" in report)
ok("masks the node API key", SECRET_KEY not in report)
ok("masks emails, IPv4, IPv6 and MAC addresses",
   "seller@example.com" not in report and "203.0.113.7" not in report
   and "2405:201:7001:703c::1" not in report and "34:5a:60:9d:f6:42" not in report)
ok("masks bearer tokens and proxy credentials", "abc.def.ghi" not in report and "hunter2" not in report)
ok("drops docker's machine-name line", "Name: Sebastian-PC" not in report)
ok("never reads agent.env into the report (only the fixed read-only checks run)",
   not any(env_file.name in " ".join(map(str, c)) for c in ran)
   and all(c[0] in ("uname", "sh", "nvidia-smi", "rocm-smi", "docker", "cat", "journalctl", "echo")
           for c in ran))
ok("container GPU test included when allowed", any("container GPU test" in l for l in report.splitlines()))

subprocess.run = lambda cmd, **k: _R("x" * 500_000)
try:
    big = d.collect("r", "45", gpu_test=False)
finally:
    subprocess.run = real_run
ok("report is bounded", len(big) <= d.MAX_CHARS)
ok("no GPU container test when a rental may hold the GPU", "container GPU test" not in big)

# opt-out and missing config never touch the network
calls = []
real_open = d.urllib.request.urlopen
d.urllib.request.urlopen = lambda req, timeout=0: calls.append(req)
ok("opt-out sends nothing", d.send("r", env={"PB_SHARE_DIAGNOSTICS": "false", "PETABYTE_API_URL": "u",
                                              "PETABYTE_API_KEY": "k", "PETABYTE_SPEC_ID": "1"}) == (None, None)
   and not calls)
ok("unconfigured node sends nothing", d.send("r", env={}) == (None, None) and not calls)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return b'{"sent": true}'


sent = []
d.urllib.request.urlopen = lambda req, timeout=0: sent.append(req) or _Resp()
d.collect = lambda reason, spec, gpu_test: "REPORT"
try:
    rep, reply = d.send("startup GPU container self-test failed", gpu_test=False)
finally:
    d.urllib.request.urlopen = real_open
req = sent[0] if sent else None
ok("uploads to /nodes/diagnostics with the node key from agent.env",
   bool(req) and req.full_url == "https://petabyte.example/nodes/diagnostics"
   and req.get_header("X-api-key") == SECRET_KEY and reply == {"sent": True})
ok("sends its own User-Agent (Cloudflare 403s Python-urllib; Sebastian 2026-09-24)",
   bool(req) and (req.get_header("User-agent") or "").startswith("petabyte-agent/"))
ok("posts spec id, reason and report",
   bool(req) and json.loads(req.data) == {"spec_id": 45, "reason": "startup GPU container self-test failed",
                                           "report": "REPORT"})
os.unlink(env_file.name)
print("\nOK — node diagnostics are masked, bounded and opt-out-able" if not _fail else f"\n{_fail} FAILED")
raise SystemExit(1 if _fail else 0)
