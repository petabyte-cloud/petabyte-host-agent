import argparse
import subprocess
import os
import shutil
import requests
import re
import uuid
import socket
import logging
from typing import List, Dict, Optional

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

KERNEL_URL = "https://cdn.kernel.org/pub/linux/kernel/v5.x/linux-5.10.1.tar.xz"
KERNEL_ARCHIVE = "linux-5.10.1.tar.xz"
KERNEL_DIR = "linux-5.10.1"
ROOTFS_URL = "https://cloud-images.ubuntu.com/minimal/releases/focal/release/ubuntu-20.04-minimal-cloudimg-amd64.img"
ROOTFS_IMG = "rootfs.img"

def download_file(url, filename):
    if os.path.exists(filename):
        print(f"[+] {filename} already exists.")
        return
    print(f"[+] Downloading {filename} ...")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(filename, 'wb') as f:
            shutil.copyfileobj(r.raw, f)
    print(f"[+] Downloaded {filename}.")

def extract_kernel_source():
    if os.path.exists(KERNEL_DIR):
        print(f"[+] Kernel source directory already exists.")
        return
    subprocess.run(["tar", "-xf", KERNEL_ARCHIVE])

def build_kernel():
    os.chdir(KERNEL_DIR)
    subprocess.run(["make", "defconfig"])
    subprocess.run(["make", "-j", str(os.cpu_count())])
    os.chdir("..")
    bzimage = os.path.join(KERNEL_DIR, "arch/x86/boot/bzImage")
    if os.path.exists(bzimage):
        shutil.copy(bzimage, "vmlinux")
        print("[+] Kernel built and copied to vmlinux.")
    else:
        print("[!] Kernel build failed.")

def auto_build_kernel():
    if not os.path.exists("vmlinux"):
        print("[*] Kernel not found. Building from source...")
        download_file(KERNEL_URL, KERNEL_ARCHIVE)
        extract_kernel_source()
        build_kernel()

def list_nvidia_gpus() -> List[str]:
    gpus = []
    try:
        output = subprocess.check_output(['lspci', '-nn'], text=True)
        for line in output.splitlines():
            if 'NVIDIA' in line:
                match = re.search(r'^(\S+)', line)
                if match:
                    gpus.append(match.group(1))
    except Exception as e:
        print(f"[!] GPU detection error: {e}")
    return gpus

def gpu_menu_select(gpus: List[str]) -> List[str]:
    print("Select GPUs to passthrough (comma-separated):")
    for i, gpu in enumerate(gpus):
        print(f"{i}: {gpu}")
    selected = input("Your choice: ").strip()
    try:
        indices = [int(x) for x in selected.split(",") if x.isdigit()]
        return [gpus[i] for i in indices if i < len(gpus)]
    except Exception as e:
        print(f"[!] Invalid input: {e}")
        return []

def vfio_bind(pci_id: str):
    # WARNING: GPU passthrough must bind the ENTIRE IOMMU group (GPU + its audio
    # function), is destructive to the host's use of that device, and should be
    # restored on teardown. This minimal bind is for dedicated passthrough hosts
    # only. Verify `ls /sys/bus/pci/devices/0000:{id}/iommu_group/devices` first.
    subprocess.run(["modprobe", "vfio-pci"])
    print(f"[+] Binding {pci_id} to vfio-pci...")
    try:
        with open(f"/sys/bus/pci/devices/0000:{pci_id}/driver/unbind", 'w') as f:
            f.write(f"0000:{pci_id}")
    except Exception: pass
    try:
        with open(f"/sys/bus/pci/devices/0000:{pci_id}/vendor") as f:
            vendor = f.read().strip()
        with open(f"/sys/bus/pci/devices/0000:{pci_id}/device") as f:
            device = f.read().strip()
        with open("/sys/bus/pci/drivers/vfio-pci/new_id", 'w') as f:
            f.write(f"{vendor} {device}")
    except Exception as e:
        print(f"[!] Failed to bind GPU: {e}")

def run_qemu(cpu, ram, passthrough_ids, vm_id="default"):
    # Per-tenant copy-on-write overlay on top of the shared base image.
    # The Ubuntu cloud image is qcow2 (NOT raw) -- using format=raw silently fails.
    overlay = f"/tmp/rootfs-{vm_id}.qcow2"
    try:
        subprocess.run(["qemu-img", "create", "-f", "qcow2",
                        "-F", "qcow2", "-b", os.path.abspath(ROOTFS_IMG), overlay],
                       check=True, capture_output=True)
    except Exception as e:
        print(f"[!] overlay create failed ({e}); falling back to shared image")
        overlay = ROOTFS_IMG
    vfio_args = []
    for pci_id in passthrough_ids:
        vfio_args += ["-device", f"vfio-pci,host={pci_id}"]

    cmd = [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-machine", "type=q35,accel=kvm",
        "-cpu", "host",
        "-smp", str(cpu),
        "-m", f"{ram}G",
        "-kernel", "vmlinux",
        "-drive", f"file={overlay},format=qcow2,if=virtio",
        "-append", "console=ttyS0 root=/dev/vda rw",
        "-nographic"
    ] + vfio_args

    print(f"[+] Starting QEMU...")
    subprocess.run(cmd)

def run_firecracker(cpu, ram):
    print("[*] Starting Firecracker...")
    socket_path = "/tmp/firecracker.socket"
    if os.path.exists(socket_path):
        os.remove(socket_path)
    subprocess.Popen(["firecracker", "--api-sock", socket_path])

    import time, httpx
    time.sleep(1)
    # NOTE: Firecracker needs an UNCOMPRESSED kernel image (vmlinux/ELF), not a
    # bzImage, and an ext4 rootfs (not the qcow2 cloud image). Provision those
    # separately before enabling this path on real hardware.
    transport = httpx.HTTPTransport(uds=socket_path)
    client = httpx.Client(transport=transport, base_url="http://localhost", timeout=10)

    def put(endpoint, data):
        client.put(endpoint, json=data)

    put("/boot-source", {
        "kernel_image_path": "vmlinux",
        "boot_args": "console=ttyS0 reboot=k panic=1 pci=off"
    })
    put("/drives/rootfs", {
        "drive_id": "rootfs",
        "path_on_host": ROOTFS_IMG,
        "is_root_device": True,
        "is_read_only": False
    })
    put("/machine-config", {
        "vcpu_count": cpu,
        "mem_size_mib": ram * 1024,
        "ht_enabled": True
    })
    put("/actions", {"action_type": "InstanceStart"})
    print("[+] Firecracker launched.")
    
def install_dependencies():
    print("[+] Installing QEMU and Firecracker...")

    subprocess.run(["sudo", "apt", "update"])
    subprocess.run(["sudo", "apt", "install", "-y", "qemu-system-x86", "qemu-kvm", "pciutils", "curl", "make", "gcc"])

    if not shutil.which("firecracker"):
        print("[+] Installing Firecracker binary...")
        subprocess.run([
            "bash", "-c",
            "curl -LO https://github.com/firecracker-microvm/firecracker/releases/latest/download/firecracker-x86_64 && "
            "chmod +x firecracker-x86_64 && "
            "sudo mv firecracker-x86_64 /usr/local/bin/firecracker"
        ])
    else:
        print("[✓] Firecracker already installed.")

    print("[✓] All dependencies installed.")

def uninstall_dependencies():
    print("[!] Uninstalling QEMU and Firecracker...")
    
    print("[!] Removing Firecracker binary...")
    subprocess.run(["sudo", "rm", "-f", "/usr/local/bin/firecracker"])

    print("[!] Deleting kernel and rootfs files...")
    for file in [KERNEL_ARCHIVE, "vmlinux", ROOTFS_IMG]:
        if os.path.exists(file):
            os.remove(file)
            print(f"  - Removed {file}")
    if os.path.exists(KERNEL_DIR):
        print(f"  - Removing {KERNEL_DIR}/ source directory...")
        shutil.rmtree(KERNEL_DIR)
        print(f"  - Removed {KERNEL_DIR} source directory")

    print("[!] Purging QEMU from system...")
    subprocess.run(["sudo", "apt", "remove", "--purge", "-y", "qemu-system-x86", "qemu-kvm"])
    subprocess.run(["sudo", "apt", "autoremove", "-y"])

    print("[✓] Uninstall complete.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("vm_type", choices=["qemu", "firecracker"])
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--ram", type=int, default=2)
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--install", action="store_true", help="Install dependencies")
    parser.add_argument("--uninstall", action="store_true", help="Remove QEMU, Firecracker, and downloaded files")

    args = parser.parse_args()
    if args.uninstall:
        uninstall_dependencies()
        return
    if args.install:
        install_dependencies()
        return
    # Ensure rootfs and kernel are ready
    download_file(ROOTFS_URL, ROOTFS_IMG)
    auto_build_kernel()

    gpu_ids = []
    if args.cuda and args.vm_type == "qemu":
        gpus = list_nvidia_gpus()
        if gpus:
            selected = gpu_menu_select(gpus)
            for pci_id in selected:
                vfio_bind(pci_id)
                gpu_ids.append(pci_id)

    if args.vm_type == "qemu":
        run_qemu(args.cpu, args.ram, gpu_ids)
    elif args.vm_type == "firecracker":
        run_firecracker(args.cpu, args.ram)

def launch_vm_task(task_id: int, vm_type: str = "docker", cpu: int = 2, ram: int = 2, cuda: bool = False) -> Dict:
    """
    Launch a VM task and return VM details.
    Returns a dictionary with VM connection information.
    """
    logging.info(f"Launching VM task {task_id}: type={vm_type}, cpu={cpu}, ram={ram}GB, cuda={cuda}")
    
    vm_id = str(uuid.uuid4())
    
    # For Windows, use Docker containers as lightweight VMs
    if os.name == 'nt' or vm_type == "docker":
        return launch_docker_vm(task_id, vm_id, cpu, ram, cuda)
    elif vm_type == "qemu":
        # QEMU on Linux
        return launch_qemu_vm(task_id, vm_id, cpu, ram, cuda)
    elif vm_type == "firecracker":
        # Firecracker on Linux
        return launch_firecracker_vm(task_id, vm_id, cpu, ram)
    else:
        # Fallback to Docker
        return launch_docker_vm(task_id, vm_id, cpu, ram, cuda)

def launch_docker_vm(task_id: int, vm_id: str, cpu: int, ram: int, cuda: bool) -> Dict:
    """Launch a Docker container as a VM."""
    try:
        # Check if Docker is available
        try:
            subprocess.run(["docker", "--version"], check=True, capture_output=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            # No host fallback. Returning a fake endpoint here would hand the buyer a
            # connection string to a machine that isn't running and let the booking
            # settle against nothing. Refuse so the server refunds the escrow.
            raise RuntimeError(
                "Docker is not available on this node; cannot launch a VM. "
                "Install Docker + the NVIDIA toolkit and restart the agent.")
        
        # Calculate memory limit (Docker uses MB)
        memory_mb = ram * 1024
        
        # Build Docker command
        docker_cmd = [
            "docker", "run", "-d",
            "--name", f"petabyte-vm-{vm_id}",
            "--cpus", str(cpu),
            "--memory", f"{memory_mb}m",
            "--memory-swap", f"{memory_mb}m",
            "--pids-limit", "512",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--cap-add", "CHOWN", "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--hostname", f"vm-{vm_id}",
            "ubuntu:20.04",
            "tail", "-f", "/dev/null"  # Keep container running
        ]
        
        if cuda:
            docker_cmd.insert(2, "--gpus")
            docker_cmd.insert(3, "all")
        
        # Launch container
        result = subprocess.run(docker_cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            container_id = result.stdout.strip()
            logging.info(f"Docker container launched: {container_id}")
            
            # Get container IP (if on Linux with bridge network)
            try:
                inspect_result = subprocess.run(
                    ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container_id],
                    capture_output=True, text=True
                )
                ip_address = inspect_result.stdout.strip() or "127.0.0.1"
            except:
                ip_address = "127.0.0.1"
            
            return {
                "vm_type": "docker",
                "vm_id": vm_id,
                "container_id": container_id,
                "ip_address": ip_address,
                "port": 22,
                "connection_string": f"docker exec -it {container_id} /bin/bash",
                "status": "running"
            }
        else:
            logging.error(f"Docker launch failed: {result.stderr}")
            return {
                "vm_type": "docker",
                "vm_id": vm_id,
                "status": "failed",
                "error": result.stderr
            }
    except Exception as e:
        logging.exception("Error launching Docker VM")
        return {
            "vm_type": "docker",
            "vm_id": vm_id,
            "status": "failed",
            "error": str(e)
        }

def launch_qemu_vm(task_id: int, vm_id: str, cpu: int, ram: int, cuda: bool) -> Dict:
    """QEMU micro-VM backend — NOT IMPLEMENTED.

    This must NEVER return a synthetic 'running' status: doing so previously reported
    a fabricated SSH address to the buyer for a machine that does not exist. Until the
    real QEMU + VFIO passthrough path is built (see docs/isolation-roadmap.md, Phase 2),
    fail loudly so the booking is refunded rather than silently mis-fulfilled."""
    raise NotImplementedError(
        "QEMU micro-VM backend is not implemented yet; use the Docker execution path.")

def launch_firecracker_vm(task_id: int, vm_id: str, cpu: int, ram: int) -> Dict:
    """Firecracker micro-VM backend — NOT IMPLEMENTED. See launch_qemu_vm."""
    raise NotImplementedError(
        "Firecracker micro-VM backend is not implemented yet; use the Docker execution path.")

if __name__ == "__main__":
    main()
