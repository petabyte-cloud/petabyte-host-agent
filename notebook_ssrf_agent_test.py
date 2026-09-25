# notebook_ssrf_agent_test — the AGENT re-applies the SSRF guard at fetch time (fetch runs on the
# host netns, outside DOCKER-USER egress) and on EVERY redirect hop. Offline (IP literals + fake httpx).
import os
os.environ.setdefault("PETABYTE_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("PETABYTE_API_KEY", "x")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")
F = []
def ok(l, c):
    print(("ok   " if c else "FAIL ") + l)
    if not c: F.append(l)
import task_fetcher as tf

for u in ("http://169.254.169.254/x.ipynb", "http://10.0.0.5/x.ipynb", "http://127.0.0.1/x", "http://[fd00:ec2::254]/x"):
    ok("agent blocks %s" % u, tf._url_is_public(u)[0] is False)
ok("agent allows public 1.1.1.1", tf._url_is_public("https://1.1.1.1/x.ipynb")[0] is True)

from unittest.mock import patch, MagicMock
response = MagicMock()
response.status = 302
response.getheader.return_value = "http://169.254.169.254/latest/meta-data/"
with patch("safe_fetch._PinnedConnection") as connection:
    connection.return_value.getresponse.return_value = response
    try:
        tf._safe_notebook_get("https://1.1.1.1/x.ipynb")
        ok("redirect-to-IMDS refused", False)
    except ValueError:
        ok("redirect-to-IMDS refused", True)
print("\n=== notebook_ssrf_agent: %s ===" % ("0 failures" if not F else "%d FAILED" % len(F)))
raise SystemExit(1 if F else 0)
