"""Per-job Docker bridge confinement on a locally controlled Linux daemon.

Only the new job's bridge gets rules. No global firewall flush, host network,
public inbound listener, IPv6, remote Docker context, or desktop fallback.
"""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys

DENY_V4 = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
           "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
           "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
           "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4")


class NetworkUnavailable(RuntimeError):
    pass


def _run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        # Name the command (never its stderr: this reason reaches the buyer's task log).
        raise NetworkUnavailable(f"job network policy unavailable: `{' '.join(args[:3])}` failed")
    return result.stdout.strip()


DESKTOP = "Docker Desktop's daemon can't isolate rentals"
_HINT = ("run the agent as root on Linux with the native Docker Engine; Docker Desktop and "
         "rootless Docker can't isolate rentals")
_HINTS = ((DESKTOP, "on Windows, re-run the Petabyte installer: it moves the agent into its own WSL "
                    "distro with a native Docker Engine and leaves Docker Desktop as it is; on Linux, "
                    "install Docker Engine (docker-ce) and run the agent as root"),
          ("remote container daemon", "unset DOCKER_HOST and switch to the local engine "
                                      "(`docker context use default`); " + _HINT),
          ("missing ", "install iptables and iproute2 (e.g. `apt-get install iptables iproute2`)"))


def hint(reason):
    """The seller's next step for a NetworkUnavailable reason."""
    return next((h for key, h in _HINTS if str(reason).startswith(key)), _HINT)


def probe():
    """(ok, reason): can ensure() work on this host at all? Creates no network and no rule.

    Same daemon check ensure() runs first, plus the tools it shells out to. ok=False means every
    networked (serving) rental placed here would be refused, so the agent reports it up front."""
    try:
        _local_daemon()
        missing = [tool for tool in ("iptables", "ip6tables", "ip") if not shutil.which(tool)]
        if missing:
            return False, "missing " + ", ".join(missing)
        return True, None
    except NetworkUnavailable as e:
        return False, str(e)
    except Exception as e:                               # noqa: BLE001 — docker absent, hung, bad JSON
        return False, f"container daemon unreachable ({type(e).__name__})"


def _names(tid):
    if isinstance(tid, bool) or not str(tid).isdigit() or int(tid) <= 0:
        raise NetworkUnavailable("invalid task network identity")
    suffix = hashlib.sha256(str(int(tid)).encode()).hexdigest()[:10]
    return "pbj" + suffix, "PBJ" + suffix


def _local_daemon():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise NetworkUnavailable("a local Linux firewall is required")
    endpoint = _run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
    if endpoint != "unix:///var/run/docker.sock" or os.getenv("DOCKER_HOST"):
        raise NetworkUnavailable("remote container daemon refused")
    info = json.loads(_run(["docker", "info", "--format", "{{json .}}"]))
    if info.get("Name") == "docker-desktop" or "Docker Desktop" in str(info.get("OperatingSystem")):
        raise NetworkUnavailable(DESKTOP)               # e.g. its WSL integration in this distro
    if info.get("Name") != socket.gethostname() or info.get("OSType") != "linux":
        raise NetworkUnavailable("container daemon and firewall must share the host")


def _routes():
    routes = json.loads(_run(["ip", "-j", "-4", "route", "show", "table", "all"]))
    denied = set(DENY_V4)
    for row in routes:
        dest = row.get("dst", "default")
        if dest != "default":
            net = ipaddress.ip_network(dest, strict=False)
            if net.prefixlen:
                denied.add(str(net))
    return sorted(denied)


def _rules(iface, prefix, denied):
    outbound, inbound, host = prefix + "O", prefix + "I", prefix + "H"
    chains = {
        outbound: [["-d", cidr, "-j", "DROP"] for cidr in denied] + [["-j", "RETURN"]],
        inbound: [["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"],
                  ["-j", "DROP"]],
        host: [["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "RETURN"],
               ["-j", "DROP"]],
    }
    hooks4 = [("FORWARD", ["-i", iface, "-j", outbound]),
              ("FORWARD", ["-o", iface, "-j", inbound]),
              ("INPUT", ["-i", iface, "-j", host])]
    hooks6 = [("FORWARD", ["-i", iface, "-j", "DROP"]),
              ("FORWARD", ["-o", iface, "-j", "DROP"]),
              ("INPUT", ["-i", iface, "-j", "DROP"])]
    return chains, hooks4, hooks6


def _ensure_rule(tool, chain, rule):
    result = subprocess.run([tool, "-w", "5", "-C", chain, *rule],
                            capture_output=True, timeout=15)
    if result.returncode:
        _run([tool, "-w", "5", "-I", chain, "1", *rule])


def ensure(tid):
    """Create/revalidate a private job bridge before any buyer container may start."""
    _local_daemon()
    iface, prefix = _names(tid)
    name = "pb-net-t" + str(int(tid))
    found = subprocess.run(["docker", "network", "inspect", name],
                           capture_output=True, text=True, timeout=15)
    if found.returncode:
        _run(["docker", "network", "create", "--driver", "bridge", "--ipv6=false",
              "--opt", "com.docker.network.bridge.name=" + iface,
              "--opt", "com.docker.network.bridge.enable_icc=false",
              "--label", "pb.task=" + str(int(tid)), name])
    net = json.loads(_run(["docker", "network", "inspect", name]))[0]
    if (net.get("Driver") != "bridge" or net.get("EnableIPv6")
            or net.get("Labels", {}).get("pb.task") != str(int(tid))
            or net.get("Options", {}).get("com.docker.network.bridge.name") != iface
            or net.get("Options", {}).get("com.docker.network.bridge.enable_icc") != "false"
            or net.get("Containers")):
        raise NetworkUnavailable("job network ownership or isolation mismatch")
    chains, hooks4, hooks6 = _rules(iface, prefix, _routes())
    # A fresh bridge has no workload. Never flush a chain: if an existing policy differs,
    # refuse the launch and leave its protective rules intact for operator review.
    import shlex
    for chain, rules in chains.items():
        state = subprocess.run(["iptables", "-w", "5", "-S", chain],
                               capture_output=True, text=True, timeout=15)
        if state.returncode:
            _run(["iptables", "-w", "5", "-N", chain])
            for rule in rules:
                _run(["iptables", "-w", "5", "-A", chain, *rule])
        expected = [["-N", chain]] + [["-A", chain, *rule] for rule in rules]
        actual = [shlex.split(line) for line in _run(["iptables", "-w", "5", "-S", chain]).splitlines()]
        if actual != expected:
            raise NetworkUnavailable("job firewall differs from required policy")
    for tool, hooks in (("iptables", hooks4), ("ip6tables", hooks6)):
        for chain, rule in hooks:
            _ensure_rule(tool, chain, rule)
        # Move only this job's hooks ahead of pre-existing rules; no unrelated chain is changed.
        for chain, rule in hooks:
            _run([tool, "-w", "5", "-D", chain, *rule])
            _run([tool, "-w", "5", "-I", chain, "1", *rule])
    # Force this bridge's INTERNET egress through the gateway VPN, so the buyer container never
    # reaches the internet on the SELLER's IP. FAIL-CLOSED: if the operator enrolled this node in
    # the egress VPN but the tunnel can't come up, refuse the launch (the rental is refunded) rather
    # than leak the seller IP. Disabled (no-op) on nodes without PB_EGRESS_* config.
    try:
        import egress_vpn
    except Exception:                                    # noqa: BLE001
        egress_vpn = None
    if egress_vpn and egress_vpn.enabled():
        cfg = (net.get("IPAM") or {}).get("Config") or [{}]
        subnet = cfg[0].get("Subnet")
        if not subnet or not egress_vpn.ensure_tunnel():
            raise NetworkUnavailable("egress VPN required but the gateway tunnel is unavailable")
        egress_vpn.route_bridge(subnet)
        egress_vpn.record(tid, subnet)
    return name


def cleanup(tid):
    """Remove only this task's exact hooks/chains, after its containers have stopped."""
    try:
        import egress_vpn
        egress_vpn.unroute_for_tid(tid)                  # remove this bridge's egress-VPN rules
    except Exception:                                    # noqa: BLE001
        pass
    iface, prefix = _names(tid)
    chains, hooks4, hooks6 = _rules(iface, prefix, ())
    for tool, hooks in (("iptables", hooks4), ("ip6tables", hooks6)):
        for chain, rule in hooks:
            subprocess.run([tool, "-w", "5", "-D", chain, *rule],
                           capture_output=True, timeout=15)
    for chain in chains:
        subprocess.run(["iptables", "-w", "5", "-F", chain], capture_output=True, timeout=15)
        subprocess.run(["iptables", "-w", "5", "-X", chain], capture_output=True, timeout=15)
