"""distributed_run_test.py — the EXECUTION proof for distributed compute.

The control-plane test (lumaris_api/distributed_test.py) proves the platform gang-schedules N
nodes, escrows all-or-nothing, and coordinates rendezvous. THIS test proves the other half — that
a cluster the platform assembled ACTUALLY RUNS: N independent OS processes, each a rank, rendezvous
through the master address, exchange real data over real TCP sockets, and every rank ends holding
the identical, correct globally-reduced result. No GPU, no torch, no Docker, no VPN — hermetic and
offline, so "one job really ran across N ranks and produced the right answer" is a green test, not
a claim.

Covered:
  * build_torchrun_cmd wires the master address / this rank / world size into a real torchrun
    launch (the exact argv a training rank runs in its container);
  * resolve_master: rank 0 is its own master immediately; a joining rank polls until rank 0 is up;
    a master that never appears fails the rank (and, gang-scheduled, the whole cluster);
  * THE PROOF: a real 4-process all-reduce — every rank converges on the correct element-wise sum;
  * gang failure at execution: a missing rank makes the run fail (no rank falsely reports success),
    proven both at the function level and across real processes.

Run:  python distributed_run_test.py     (from the lumaris_agent/ directory)
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import distributed_run as dr  # noqa: E402

_fail = 0
_HERE = os.path.dirname(os.path.abspath(__file__))


def ok(label, cond):
    global _fail
    print(("ok  " if cond else "FAIL") + "  " + label)
    if not cond:
        _fail += 1


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(rank, world, port, outdir, *, dim=8, seed=0, timeout=30):
    out = os.path.join(outdir, f"rank{rank}.json")
    p = subprocess.Popen(
        [sys.executable, "-m", "distributed_run", "selftest",
         "--rank", str(rank), "--world-size", str(world), "--master", f"127.0.0.1:{port}",
         "--dim", str(dim), "--seed", str(seed), "--bind", "127.0.0.1",
         "--timeout", str(timeout), "--out", out],
        cwd=_HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p, out


# --------------------------------------------------------------------------- 1) launch command
for network in (["--network","host"],["--network","bridge"],None):
    try:
        dr.build_torchrun_cmd(image="buyer/image",command="train.py",rank=0,world_size=2,
                             master_addr="10.8.0.1",master_port=29500,egress_flags=network)
        ok("unsupported distributed launch rejected",False)
    except ValueError as exc:
        ok("unsupported distributed launch rejected", "overlay" in str(exc))

# --------------------------------------------------------------------------- 2) resolve master
m = dr.resolve_master({"rank": 0, "is_master": True}, my_host="10.8.0.1", my_port=29500)
ok("rank 0 is its own master immediately (no polling)",
   m["is_master"] is True and m["master_addr"] == "10.8.0.1" and m["master_port"] == 29500)

# a joining rank polls the rendezvous until rank 0 has registered
_polls = {"n": 0}


def _fetch_ready_on_3rd():
    _polls["n"] += 1
    if _polls["n"] < 3:
        return {"master_addr": None, "master_port": None, "my_rank": 1}
    return {"master_addr": "10.8.0.1", "master_port": 29500, "my_rank": 1}


_clock = {"t": 0.0}
m2 = dr.resolve_master(
    {"rank": 1, "is_master": False}, my_host="10.8.0.7", my_port=29500,
    current={"master_addr": None}, fetch=_fetch_ready_on_3rd,
    sleep=lambda s: _clock.__setitem__("t", _clock["t"] + s),
    now=lambda: _clock["t"], timeout_s=120)
ok("a joining rank polls rendezvous until rank 0 is up, then joins it",
   m2["is_master"] is False and m2["master_addr"] == "10.8.0.1" and _polls["n"] >= 3)

# a master that never appears fails the rank (=> gang failure of the whole cluster)
_clk = {"t": 0.0}
try:
    dr.resolve_master(
        {"rank": 1, "is_master": False}, my_host="10.8.0.7", my_port=29500,
        current={"master_addr": None}, fetch=lambda: {"master_addr": None},
        sleep=lambda s: _clk.__setitem__("t", _clk["t"] + 10), now=lambda: _clk["t"],
        timeout_s=30)
    ok("a rank whose master never appears fails (gang semantics)", False)
except TimeoutError:
    ok("a rank whose master never appears fails (gang semantics)", True)

ok("is_selftest recognises the built-in cluster self-test",
   dr.is_selftest({"command": dr.SELFTEST_SENTINEL}) is True
   and dr.is_selftest({"distributed": {"selftest": True}}) is True
   and dr.is_selftest({"command": "train.py"}) is False)


# --------------------------------------------------------------------------- 3) THE PROOF
# A real 4-process all-reduce: 4 independent OS processes, each a rank, rendezvous through the
# master and reduce over real TCP sockets. Every rank must end with the SAME correct sum.
N, DIM, SEED = 4, 8, 20260812
port = _free_port()
with tempfile.TemporaryDirectory() as d:
    procs = [_spawn(r, N, port, d, dim=DIM, seed=SEED, timeout=30) for r in range(N)]
    rcs = [p.wait(timeout=60) for p, _ in procs]
    results = [json.load(open(out)) for _, out in procs]

expected = dr.expected_allreduce(N, DIM, SEED)
ok("all N rank processes exited successfully (rc==0)", all(rc == 0 for rc in rcs))
ok("every rank reported ok", all(r.get("ok") for r in results))
ok("EVERY rank converged on the IDENTICAL reduced vector",
   len({tuple(r["result"]) for r in results}) == 1)
ok("the reduced vector is the CORRECT element-wise sum across all ranks",
   all(r["result"] == expected for r in results))
ok("the master coordinated exactly N contributors (no rank silently dropped)",
   all(r.get("contributors") == N for r in results))
# each rank contributed a DISTINCT vector, so the sum could only be right if all really participated
ok("the result reflects every distinct per-rank contribution (not a single node's echo)",
   expected != [x * N for x in dr.rank_vector(0, DIM, SEED)])


# --------------------------------------------------------------------------- 4) gang failure
# Function level: a master missing a worker times out (fast + deterministic).
t0 = time.monotonic()
try:
    dr.master_allreduce("127.0.0.1", _free_port(), world_size=2,
                        my_vector=dr.rank_vector(0, 4, 1), timeout_s=2)
    ok("a master whose worker never joins fails the run (TimeoutError)", False)
except TimeoutError:
    ok("a master whose worker never joins fails the run (TimeoutError)",
       time.monotonic() - t0 < 10)

# a worker with no master to reach fails, too
try:
    dr.worker_allreduce("127.0.0.1", _free_port(), rank=1,
                        my_vector=dr.rank_vector(1, 4, 1), timeout_s=2)
    ok("a worker that can't reach the master fails the run (TimeoutError)", False)
except TimeoutError:
    ok("a worker that can't reach the master fails the run (TimeoutError)", True)

# Process level: launch a 2-rank cluster but only bring up rank 0 — the run must NOT succeed.
port2 = _free_port()
with tempfile.TemporaryDirectory() as d:
    p, out = _spawn(0, 2, port2, d, dim=4, seed=1, timeout=3)
    rc = p.wait(timeout=30)
    res = json.load(open(out))
ok("across real processes, a cluster missing a rank fails — no false success",
   rc != 0 and res.get("ok") is False and "worker" in (res.get("error") or "").lower())


# ---- VPN refusal: a rank without an active mesh refuses BEFORE it launches anything ----------
# _run_distributed runs the buyer container with `--network host`, on the assumption that a
# WireGuard mesh confines the rank to its peers. Without that mesh the container sees the host LAN
# and the cloud-metadata endpoint, so the rank must refuse — and it must refuse EARLY, before
# rendezvous and before docker, because a rank that launched and only then refused would already
# have run buyer code with exactly the exposure the check exists to prevent.
#
# The refusal is also a MONEY path: the failed result reaches main._fail_distributed_if_member,
# which fails the gang and refunds every held rank escrow, including ranks that had already
# finished and were waiting to be paid. So "refused without launching" is the behaviour that keeps
# that refund correct, and it is asserted here rather than assumed.
import types  # noqa: E402

for _n in ("crypto", "notebook", "vm", "agent_telemetry"):
    sys.modules.setdefault(_n, types.ModuleType(_n))
sys.modules["crypto"].sign_proof = lambda p: "sig"
sys.modules["notebook"].run_notebook_code = lambda *a, **k: []
sys.modules["vm"].launch_vm_task = lambda *a, **k: None
os.environ.setdefault("PETABYTE_API_URL", "https://test.local")
os.environ.setdefault("PETABYTE_API_KEY", "pk_test")
os.environ.setdefault("PETABYTE_SPEC_ID", "1")

import task_fetcher as tf   # noqa: E402
import wireguard as _wg     # noqa: E402

_posted, _logged, _http, _spawned = [], [], [], []
# task_fetcher imports subprocess INSIDE its functions, so the stdlib module object is what has
# to be patched for the "nothing was launched" guarantee to actually hold.
_orig = (tf._post, tf.report_log, tf._set_ui, tf._signed_result,
         tf.httpx.post, subprocess.run, _wg.vpn_enabled, _wg.interface_up)
try:
    tf._post = lambda path, payload: _posted.append((path, payload))
    tf.report_log = lambda tid, msg: _logged.append(str(msg))
    tf._set_ui = lambda **kw: None
    tf._signed_result = lambda tid, **kw: dict(task_id=tid, **kw)
    tf.httpx.post = lambda *a, **k: _http.append(a[0] if a else k.get("url")) or (_ for _ in ()).throw(
        AssertionError("rendezvous must not be reached when the mesh is absent"))
    subprocess.run = lambda *a, **k: _spawned.append(a) or (_ for _ in ()).throw(
        AssertionError("no container may be launched when the mesh is absent"))

    _task = {"task_id": 4242, "task_type": "distributed", "image": "buyer/img",
             "distributed": {"rank": 0, "world_size": 2, "register_url": "/jobs/rendezvous"}}

    # (a) the operator never opted this node in
    _wg.vpn_enabled = lambda: False
    _wg.interface_up = lambda name="wg0": True
    _posted.clear(); _logged.clear(); _http.clear(); _spawned.clear()
    tf._run_distributed(_task)
    ok("no AGENT_VPN_ENABLED: the rank refuses and reports FAILED",
       any(p == "/jobs/result" and b.get("status") == "failed" for p, b in _posted))
    ok("...without reaching rendezvous or launching a container",
       not _http and not _spawned)
    ok("...and says why, naming the mesh requirement",
       any("overlay" in m for m in _logged))

    # (b) opted in, but there is no live interface — the flag alone is not isolation
    _wg.vpn_enabled = lambda: True
    _wg.interface_up = lambda name="wg0": False
    _posted.clear(); _logged.clear(); _http.clear(); _spawned.clear()
    tf._run_distributed(_task)
    ok("AGENT_VPN_ENABLED=true but wg0 down: still refuses (the flag alone is not a mesh)",
       any(p == "/jobs/result" and b.get("status") == "failed" for p, b in _posted)
       and not _http and not _spawned)
    ok("...and says why here too, so a seller with a dead wg0 is not left guessing",
       any("overlay" in m for m in _logged))
    _wg.vpn_enabled=lambda: True
    _wg.interface_up=lambda name="wg0": True
    os.environ["PB_ALLOW_HOST_NET_CLUSTER"]="true"
    _posted.clear();_http.clear();_spawned.clear()
    tf._run_distributed(_task)
    ok("even an active mesh and the legacy opt-in cannot enable host execution",
       any(p=="/jobs/result" and b.get("status")=="failed" for p,b in _posted)
       and not _http and not _spawned)
    os.environ.pop("PB_ALLOW_HOST_NET_CLUSTER",None)
finally:
    (tf._post, tf.report_log, tf._set_ui, tf._signed_result,
     tf.httpx.post, subprocess.run, _wg.vpn_enabled, _wg.interface_up) = _orig

print(f"\n=== distributed execution: {'0 failures' if _fail == 0 else str(_fail) + ' FAILED'} ===")
raise SystemExit(1 if _fail else 0)
