"""distributed_run.py — the SELLER-side execution of one rank of a distributed cluster.

The control plane (API) gang-schedules N nodes across distinct machines, assigns ranks
0..N-1, escrows all-or-nothing, and coordinates rendezvous. THIS module is the other half:
what the agent actually does on the node when it claims a `distributed` task —

  1. register this rank's overlay identity (WireGuard key + port; cluster_overlay.py), then
  2. wait until every rank has registered and build the job's peer-only overlay, then
  3. EXECUTE inside that overlay (rank 0 at 10.77.0.1 is the master):
       * a real training run — launch the buyer's container under `torchrun` wired to the
         master address / this rank / the world size (`build_torchrun_cmd`), OR
       * the built-in CLUSTER SELF-TEST — a genuine cross-process all-reduce that proves the N
         ranks really talk to each other and compute the correct global reduction. No GPU, no
         torch, no custom image needed; it is the "does my cluster actually work end-to-end?"
         smoke test a buyer can run before paying for a long job.

Everything here is dependency-free (Python stdlib only) so it is unit-testable on any box —
no GPU, no Docker, no torch, no WireGuard. `task_fetcher._run_distributed` is the thin glue
that calls into these functions with the live httpx client.

The self-test all-reduce is a REDUCE→BROADCAST all-reduce over a star topology through the
master (rank 0): every worker sends its per-rank vector to the master over a real TCP socket,
the master sums all N vectors (its own included) and broadcasts the total back, so every rank
ends holding the identical, correct global sum. That is a true all-reduce — the same INPUT→
OUTPUT contract NCCL's ring all-reduce provides — chosen here for its topology simplicity and
zero dependencies. Production GPU jobs use NCCL's ring all-reduce over the per-job overlay; this
built-in exists to VALIDATE the cluster wiring, not to replace NCCL.
"""
import json
import os
import shlex
import socket
import struct
import sys
import time

# A buyer sets command to this sentinel (or passes selftest=true to /distributed, which the
# server rewrites to it) to request the built-in cluster all-reduce instead of a container.
SELFTEST_SENTINEL = "petabyte:selftest-allreduce"

DEFAULT_RENDEZVOUS_PORT = int(os.getenv("DIST_RENDEZVOUS_PORT", "29500"))


# --------------------------------------------------------------------------- helpers

def is_selftest(task: dict) -> bool:
    """True when this distributed task is the built-in cluster self-test (no container)."""
    if (task or {}).get("command") == SELFTEST_SENTINEL:
        return True
    dist = (task or {}).get("distributed") or {}
    return bool(dist.get("selftest"))


def build_torchrun_cmd(*, command: str, rank: int, world_size: int, master_addr: str,
                       master_port: int, nproc_per_node: int = 1) -> str:
    """The container command that runs THIS rank: torchrun with a static rendezvous on rank 0's
    overlay address. The agent runs it through build_container_cmd (`--network none`, no
    capabilities) and then moves the rank's verified overlay device into that container's
    namespace (cluster_overlay.py), so the only addresses the rank can reach are its peers'."""
    args = ["torchrun", f"--nnodes={int(world_size)}", f"--nproc_per_node={int(nproc_per_node)}",
            f"--node_rank={int(rank)}", "--rdzv_backend=static",
            f"--master_addr={master_addr}", f"--master_port={int(master_port)}"]
    return shlex.join(["sh", "-c", OVERLAY_GATE, "pbgate", *args]) + (f" {command}" if command else "")


# The container starts with only `lo`; the agent moves the overlay in a moment later. torchrun must
# not race it, so it is exec'd only once pbwg0 is UP (IFF_UP = bit 0 of the sysfs flags, read inside
# the container's own netns). The agent verifies right after bringing it up and kills the container
# on any mismatch. Needs /bin/sh in the image, like every CUDA/PyTorch base image; exit 97 = no overlay.
OVERLAY_GATE = ("i=0; while :; do f=; read f 2>/dev/null < /sys/class/net/pbwg0/flags; "
                "case \"$f\" in *[13579bdfBDF]) break;; esac; i=$((i+1)); "
                "[ $i -gt 3000 ] && { echo 'cluster overlay never came up' >&2; exit 97; }; "
                "sleep 0.1; done; exec \"$@\"")


def rank_env(*, rank: int, world_size: int, master_addr: str, master_port: int,
             backend: str = "nccl") -> dict:
    """The standard distributed env (overrides the buyer's), with NCCL/gloo pinned to the overlay
    device — the only interface besides lo inside the rank's namespace."""
    from cluster_overlay import IFACE
    return {"MASTER_ADDR": master_addr, "MASTER_PORT": str(int(master_port)), "RANK": str(int(rank)),
            "NODE_RANK": str(int(rank)), "WORLD_SIZE": str(int(world_size)), "PET_BACKEND": backend,
            "NCCL_SOCKET_IFNAME": IFACE, "GLOO_SOCKET_IFNAME": IFACE, "NCCL_IB_DISABLE": "1"}


# --------------------------------------------------------------------------- collective
# A real, dependency-free cross-process all-reduce (reduce→broadcast through the master).
# Framing: a 4-byte big-endian length prefix followed by a UTF-8 JSON body.

def _send_msg(sock: socket.socket, obj: dict) -> None:
    body = json.dumps(obj, separators=(",", ":")).encode()
    sock.sendall(struct.pack(">I", len(body)) + body)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed mid-message")
        buf += chunk
    return bytes(buf)


def _recv_msg(sock: socket.socket) -> dict:
    (n,) = struct.unpack(">I", _recv_exact(sock, 4))
    if n > 8 * 1024 * 1024:                       # 8 MiB cap — a self-test vector is tiny
        raise ValueError("message too large")
    return json.loads(_recv_exact(sock, n).decode())


def rank_vector(rank: int, dim: int, seed: int):
    """The per-rank input vector for the self-test. Distinct per rank (so a rank that fails to
    contribute changes the sum and is DETECTABLE), and deterministic (so the correct all-reduced
    result is known in advance — see `expected_allreduce`)."""
    base = int(seed) + int(rank)
    return [base + i for i in range(int(dim))]


def expected_allreduce(world_size: int, dim: int, seed: int):
    """The correct all-reduced (element-wise summed) vector every rank must end up holding."""
    out = [0] * int(dim)
    for r in range(int(world_size)):
        v = rank_vector(r, dim, seed)
        for i in range(int(dim)):
            out[i] += v[i]
    return out


def master_allreduce(bind_host: str, port: int, world_size: int, my_vector,
                     *, timeout_s: int = 120) -> dict:
    """Rank 0: accept the other N-1 ranks, sum every rank's vector (own included), broadcast the
    total back to all. Returns {'result', 'contributors'}. Raises TimeoutError if not every rank
    connects in time — that is gang failure surfaced at execution: a missing rank fails the run."""
    n_workers = int(world_size) - 1
    dim = len(my_vector)
    total = list(my_vector)
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind((bind_host, int(port)))
    lsock.listen(max(1, n_workers))
    lsock.settimeout(1.0)
    conns, seen, deadline = [], set(), time.monotonic() + max(1, int(timeout_s))
    try:
        while len(seen) < n_workers:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"master: only {len(seen)}/{n_workers} workers joined before timeout "
                    f"(cluster incomplete — gang failure)")
            try:
                conn, _ = lsock.accept()
            except socket.timeout:
                continue
            conn.settimeout(max(1.0, deadline - time.monotonic()))
            msg = _recv_msg(conn)
            r = int(msg.get("rank", -1))
            vec = msg.get("vector") or []
            if r in seen or r <= 0 or r >= world_size or len(vec) != dim:
                conn.close()
                continue                     # ignore duplicates / malformed / rank collisions
            seen.add(r)
            conns.append(conn)
            for i in range(dim):
                total[i] += int(vec[i])
        for conn in conns:                   # broadcast the reduced vector to every worker
            _send_msg(conn, {"result": total, "world_size": world_size,
                             "contributors": len(seen) + 1})
        return {"result": total, "contributors": len(seen) + 1}
    finally:
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        lsock.close()


def worker_allreduce(master_host: str, master_port: int, rank: int, my_vector,
                     *, timeout_s: int = 120) -> dict:
    """A non-master rank: connect to the master, send this rank's vector, receive the reduced
    total. Retries the connect until the master is listening or the deadline passes."""
    deadline = time.monotonic() + max(1, int(timeout_s))
    last_err = None
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(max(1.0, deadline - time.monotonic()))
        try:
            s.connect((master_host, int(master_port)))
            _send_msg(s, {"rank": int(rank), "vector": list(my_vector)})
            reply = _recv_msg(s)
            return {"result": reply["result"], "contributors": reply.get("contributors")}
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            last_err = e
            time.sleep(0.2)
        finally:
            try:
                s.close()
            except Exception:
                pass
    raise TimeoutError(f"rank {rank}: could not reach master {master_host}:{master_port} "
                       f"({last_err})")


def run_allreduce_rank(rank: int, world_size: int, master_host: str, master_port: int,
                       *, dim: int = 8, seed: int = 0, bind_host: str = None,
                       timeout_s: int = 120) -> dict:
    """Run THIS rank of the built-in cluster all-reduce and return the reduced vector.

    rank 0 coordinates (binds `bind_host` or the master host); every other rank joins it. On
    return, `result` is the element-wise sum of every rank's `rank_vector` — identical on every
    rank — which is exactly what `expected_allreduce` predicts."""
    my_vec = rank_vector(rank, dim, seed)
    if int(rank) == 0:
        out = master_allreduce(bind_host or master_host, master_port, world_size, my_vec,
                               timeout_s=timeout_s)
    else:
        out = worker_allreduce(master_host, master_port, rank, my_vec, timeout_s=timeout_s)
    return {"rank": int(rank), "world_size": int(world_size), "result": out["result"],
            "contributors": out.get("contributors"), "dim": int(dim), "seed": int(seed)}


# --------------------------------------------------------------------------- CLI
# `python -m distributed_run selftest --rank R --world-size N --master HOST:PORT …`
# Each rank runs as its own OS process, so the self-test is a genuine multi-process
# distributed run — the exact shape the hermetic proof (distributed_run_test.py) spawns.

def _parse_hostport(s: str, default_port: int = DEFAULT_RENDEZVOUS_PORT):
    if ":" in s:
        host, _, port = s.rpartition(":")
        return host, int(port)
    return s, default_port


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "selftest":
        print("usage: python -m distributed_run selftest --rank R --world-size N "
              "--master HOST:PORT [--dim D] [--seed S] [--bind HOST] [--timeout T] [--out FILE]",
              file=sys.stderr)
        return 2
    opts, it = {}, iter(argv[1:])
    for a in it:
        if a.startswith("--"):
            opts[a[2:]] = next(it, None)
    try:
        rank = int(opts["rank"])
        world = int(opts["world-size"])
        mhost, mport = _parse_hostport(opts["master"])
        dim = int(opts.get("dim", 8))
        seed = int(opts.get("seed", 0))
        timeout_s = int(opts.get("timeout", 120))
        bind = opts.get("bind")
    except (KeyError, TypeError, ValueError) as e:
        print(f"bad arguments: {e}", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    try:
        out = run_allreduce_rank(rank, world, mhost, mport, dim=dim, seed=seed,
                                 bind_host=bind, timeout_s=timeout_s)
        out.update({"ok": True, "elapsed_s": round(time.monotonic() - t0, 3)})
        rc = 0
    except Exception as e:                    # noqa: BLE001 — report failure, don't crash-trace
        out = {"rank": rank, "world_size": world, "ok": False, "error": str(e),
               "elapsed_s": round(time.monotonic() - t0, 3)}
        rc = 1
    payload = json.dumps(out)
    if opts.get("out"):
        with open(opts["out"], "w") as f:
            f.write(payload)
    print(payload)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
