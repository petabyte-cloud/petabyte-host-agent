"""notebook_batch_test.py — the batch 'run this notebook LINK and return its output' path.

A buyer books a node in batch mode and passes a public .ipynb URL. The AGENT (not the sandbox,
which runs --network none) fetches the notebook, executes it in the locked-down container, and
submits the executed output inline — so the buyer gets a real result at GET /tasks/{id} without any
interactive VM to connect to.

Offline: the heavy deps (crypto/notebook/vm/telemetry) and httpx are stubbed, so we drive the real
task_fetcher routing and assert what it fetches, how it calls the runner (incl. gpu=True), and what
it submits. The actual GPU docker argv is proven on a real node, not here. Run: python notebook_batch_test.py
"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PETABYTE_API_URL", "https://test.local")
os.environ.setdefault("PETABYTE_API_KEY", "pk_test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")

for _name in ("crypto", "notebook", "vm", "agent_telemetry"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
sys.modules["crypto"].sign_proof = lambda p: "sig"
sys.modules["crypto"].sha256_hex = lambda x: "hash"
_RUN_CALLS = []
sys.modules["notebook"].run_notebook_code = lambda code, **k: (_RUN_CALLS.append((code, k))
                                                               or {"result": [{"type": "text",
                                                                               "value": "42"}]})
sys.modules["vm"].launch_vm_task = lambda *a, **k: None

import task_fetcher as tf   # noqa: E402

_fail = 0


def ok(label, cond):
    global _fail
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        _fail += 1


# Fake httpx: GET returns a notebook, POST (the result submit) is captured.
_POSTS = []
_NB = {"nbformat": 4, "nbformat_minor": 5, "cells": [
    {"cell_type": "code", "source": "print(1)", "metadata": {}, "outputs": [], "execution_count": None}]}


class _Resp:
    def __init__(self, js): self._js = js
    def raise_for_status(self): pass
    def json(self): return self._js


class _Boom:
    def raise_for_status(self): raise RuntimeError("404 Not Found")
    def json(self): return {}


tf._set_ui = lambda **k: None
tf.httpx = types.SimpleNamespace(
    get=lambda url, **k: (_GET_STATE.append(url) or _GET_STATE_RESP[0]),
    post=lambda url, **k: _POSTS.append(k.get("json")) or types.SimpleNamespace(status_code=200),
    URL=lambda u: u)
# This test exercises the fetch/execute FLOW, not the SSRF guard (which has its own dedicated test,
# notebook_ssrf_agent_test). Stub the guard to pass so the flow test does not depend on real DNS
# resolving the placeholder host.
tf.safe_fetch.get = lambda url, **kwargs: (_GET_STATE.append(url) or _GET_STATE_RESP[0])
_GET_STATE = []
_GET_STATE_RESP = [_Resp(_NB)]

# ---- a notebook_url task: fetch the link, run it, submit the output ------------------------------
_RUN_CALLS.clear(); _POSTS.clear(); _GET_STATE.clear()
tf._run_notebook({"task_id": 5,
                  "code": json.dumps({"notebook_url": "https://raw.x/y.ipynb", "gpu": True,
                                      "max_runtime_s": 120})})
ok("the agent fetches the notebook URL over the network (sandbox is --network none)",
   _GET_STATE == ["https://raw.x/y.ipynb"])
ok("the FETCHED notebook dict is what gets executed (not the raw JSON string)",
   len(_RUN_CALLS) == 1 and _RUN_CALLS[0][0] == _NB)
ok("gpu=True is passed through so a GPU node actually runs on the GPU",
   _RUN_CALLS[0][1].get("gpu") is True)
ok("the buyer's authorized runtime budget is passed to the runner",
   _RUN_CALLS[0][1].get("max_runtime_s") == 120)
ok("a completed result is submitted (the buyer gets output at /tasks/{id})",
   len(_POSTS) == 1 and _POSTS[0]["status"] == "completed" and _POSTS[0]["task_id"] == 5)

# ---- a bad link reports WHY instead of vanishing silently ----------------------------------------
_RUN_CALLS.clear(); _POSTS.clear(); _GET_STATE.clear()
_GET_STATE_RESP[0] = _Boom()
tf._run_notebook({"task_id": 6, "code": json.dumps({"notebook_url": "https://raw.x/missing.ipynb"})})
ok("a fetch failure never reaches the runner", len(_RUN_CALLS) == 0)
ok("a fetch failure is reported AS the job result (buyer sees the reason, not a silent hang)",
   len(_POSTS) == 1 and "fetch failed" in (_POSTS[0]["result"] or ""))
_GET_STATE_RESP[0] = _Resp(_NB)

# ---- inline-code and bundle tasks are unaffected by the new branch -------------------------------
_RUN_CALLS.clear(); _POSTS.clear(); _GET_STATE.clear()
tf._run_notebook({"task_id": 7, "code": "print('hi')"})       # plain inline code
ok("a plain inline-code notebook still runs (no URL fetch)",
   _GET_STATE == [] and len(_RUN_CALLS) == 1 and _RUN_CALLS[0][0] == "print('hi')")
ok("inline code does NOT force gpu (unchanged default path)",
   "gpu" not in _RUN_CALLS[0][1])

# ---- the server wires gpu from the node it placed the job on -------------------------------------
# (documented contract: /launch batch sets {"notebook_url","gpu","max_runtime_s"} in task.code)
ok("_run_notebook routes a notebook_url env to the batch runner",
   "_run_notebook_url" in tf._run_notebook.__code__.co_names)

print(f"\n=== notebook_batch: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
