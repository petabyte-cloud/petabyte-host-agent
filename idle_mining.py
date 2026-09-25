"""Optional GPU idle-mining container (DOGE via FishHash on unMineable). No mining
imports/dependencies or auto-downloads.

The server can only grant idle time, never enable mining or change a payout.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

STATE = Path(os.getenv("PETABYTE_MINING_STATE", "/var/lib/petabyte-agent/mining"))
NAME = "petabyte-idle-gpu"
LABEL = "market.petabyte.idle-miner=1"


def boot_time():
    # /proc/uptime in a Linux container uses BOOTTIME, including suspend time.
    return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, "CLOCK_BOOTTIME") else time.monotonic()


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=10, check=False)


def configuration(env=None):
    """Validate the GPU DOGE (FishHash) idle-mining config; returns the config dict or raises."""
    env = os.environ if env is None else env
    address = env.get("DOGE_ADD", "")
    if not re.fullmatch(r"[DA9][1-9A-HJ-NP-Za-km-z]{33,34}", address):
        raise ValueError("Set DOGE_ADD to the DOGE payout address")
    image = env.get("PETABYTE_GPU_MINING_IMAGE", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Build the GPU mining image, then set PETABYTE_GPU_MINING_IMAGE to its sha256 image ID")
    device = env.get("PETABYTE_MINING_GPU_UUID", "")
    if not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", device):
        raise ValueError("Select exactly one NVIDIA GPU using PETABYTE_MINING_GPU_UUID")
    # DOGE is a pool conversion payout, not direct Scrypt mining. An explicit opt-in
    # acknowledges conversion and the pool's own fees/thresholds.
    if env.get("PETABYTE_DOGE_POOL_CONVERSION") != "true":
        raise ValueError("GPU DOGE payouts require PETABYTE_DOGE_POOL_CONVERSION=true and a compatible payout pool")
    worker = env.get("PETABYTE_MINING_WORKER", "")
    if not re.fullmatch(r"pb-[a-zA-Z0-9_-]{1,48}", worker):
        raise ValueError("Set PETABYTE_MINING_WORKER to a unique pb-NODE_NAME")
    return {"coin": "DOGE", "address": address, "image": image, "device": device,
            "worker": worker, "user": f"DOGE:{address}.{worker}"}


def stop():
    """Remove only our explicitly labelled miner container; confirm it is gone. Also sweeps a
    legacy petabyte-idle-cpu container if an older agent left one behind."""
    result = docker("ps", "-aq", "--filter", "name=^/petabyte-idle-(cpu|gpu)$", "--filter", f"label={LABEL}")
    if result.returncode:
        raise RuntimeError("Cannot confirm idle miner is stopped: Docker unavailable")
    for cid in result.stdout.split():
        if not re.fullmatch(r"[0-9a-f]{12,64}", cid):
            raise RuntimeError("Unexpected container identity")
        result = docker("rm", "-f", cid)
        if result.returncode:
            # --rm may remove it concurrently after lease expiry.
            check = docker("ps", "-aq", "--filter", f"id={cid}")
            if check.returncode or check.stdout.strip():
                raise RuntimeError("Idle miner did not stop; rental dispatch blocked")


class Controller:
    def __init__(self):
        self.lock = threading.RLock()
        self.generation = 0
        self.busy = False
        self.touched = False
        self.prepare_gpu = lambda device: False

    def ticket(self):
        with self.lock:
            return self.generation, boot_time()

    def enabled(self):
        return os.getenv("PETABYTE_IDLE_MINING") == "true" and not (STATE / "disabled").exists()

    def revoke(self):
        if STATE.exists():
            (STATE / "until").write_text("0\n")
        if self.touched or self.enabled() or STATE.exists():
            stop()

    def polling(self):
        with self.lock:
            self.generation += 1
            self.busy = True

    def before_work(self):
        with self.lock:
            self.generation += 1
            self.busy = True
            self.revoke()

    def after_work(self):
        with self.lock:
            self.generation += 1
            self.busy = False

    def heartbeat(self, ticket, permit, live=False):
        with self.lock:
            if ticket[0] != self.generation:
                return
            seconds = permit.get("seconds", 0) if isinstance(permit, dict) else 0
            if type(seconds) is not int or not 0 < seconds <= 30:
                seconds = 0
            until = int(ticket[1] + seconds)
            if not self.enabled() or self.busy or live or until <= boot_time():
                self.revoke()
                return
            config = configuration()
            STATE.mkdir(parents=True, exist_ok=True)
            STATE.chmod(0o755)
            tmp = STATE / "until.tmp"
            tmp.write_text(f"{until}\n")
            tmp.chmod(0o644)
            tmp.replace(STATE / "until")
            self.touched = True
            self.start_gpu(config)

    def start_gpu(self, config=None):
        if config is None:
            config = configuration()
        result = docker("ps", "-q", "--filter", f"name=^/{NAME}$", "--filter", f"label={LABEL}")
        if result.returncode:
            raise RuntimeError("GPU mining status unavailable")
        if result.stdout.strip():
            return
        if not self.prepare_gpu(config["device"]):
            raise RuntimeError("GPU memory preparation unavailable; idle GPU mining stays off")
        if int((STATE / "until").read_text()) <= boot_time() or not self.enabled():
            return
        result = docker(
            "run", "-d", "--rm", "--pull=never", "--name", NAME,
            "--label", LABEL, "--restart=no", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=128", "--cpus=1",
            "--memory=4g", "--memory-swap=4g", "--user=65534:65534",
            "--gpus", f"device={config['device']}", "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "-e", "HOME=/tmp",
            "--log-opt=max-size=1m", "--log-opt=max-file=1",
            "--mount", f"type=bind,src={STATE.resolve()},dst=/lease,readonly",
            config["image"], "--algo", "FISHHASH", "--pool", "stratum+ssl://fishhash.unmineable.com:443",
            "--user", config["user"], "--tstop", "75", "--tstart", "60",
        )
        if result.returncode:
            raise RuntimeError("GPU mining could not start")
        logging.getLogger(__name__).warning(
            "Optional idle GPU mining: DOGE payout to %s via fishhash.unmineable.com", config["address"])


controller = Controller()


def control(action):
    """Local seller control: does not restart the agent or interrupt paid work."""
    if action == "disable":
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "disabled").touch()
        (STATE / "until").write_text("0\n")
        stop()
        print("Idle mining disabled. Paid work continues normally.")
    elif action == "enable":
        config = configuration()
        if os.getenv("PETABYTE_IDLE_MINING") != "true":
            raise ValueError("Set PETABYTE_IDLE_MINING=true in the seller agent environment first")
        (STATE / "disabled").unlink(missing_ok=True)
        print(f"Idle mining allowed on the next idle heartbeat. DOGE payout: {config['address']}")
    else:
        result = docker("ps", "--format", "{{.Names}}", "--filter", "name=^/petabyte-idle-(cpu|gpu)$", "--filter", f"label={LABEL}")
        print(json.dumps({"enabled": controller.enabled(), "running": bool(result.stdout.strip()),
                          "docker_available": result.returncode == 0,
                          "containers": result.stdout.split(),
                          "coin": "DOGE",
                          "payout_address": os.getenv("DOGE_ADD", "")}))


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv
    load_dotenv(os.getenv("AGENT_ENV", "/etc/petabyte/agent.env"))
    STATE = Path(os.getenv("PETABYTE_MINING_STATE", "/var/lib/petabyte-agent/mining"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "enable", "disable"))
    control(parser.parse_args().action)
