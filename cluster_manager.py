import argparse
import logging
import os
import random
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import libvirt

# CONFIGURATION
DEFAULT_BASE_IMAGE = "alpine-base.qcow2"
DEFAULT_VM_DIR = "/var/lib/libvirt/images/"
DEFAULT_TEMPLATE_XML = "./cluster/template.xml"
DEFAULT_HAPROXY_CFG = "./haproxy/haproxy.cfg"
DEFAULT_HAPROXY_BASE_CFG = "./haproxy/haproxy_base.cfg"
DEFAULT_HAPROXY_PID_FILE = "./haproxy/haproxy.pid"
DEFAULT_HAPROXY_STATS_SOCKET = "/tmp/haproxy.sock"
DEFAULT_NETWORK_NAME = "default"
DEFAULT_LOG_FILE = "./logs/cluster_manager.log"
DEFAULT_MAX_VMS = 4
DEFAULT_MIN_VMS = 1
DEFAULT_CPU_HIGH_THRESHOLD = 80.0
DEFAULT_CPU_LOW_THRESHOLD = 20.0
DEFAULT_TIME_TO_SCALE = 30  # [seconds]
DEFAULT_TIME_INTERVAL = 5  # [seconds]
DEFAULT_HAPROXY_SERVER_MAXCONN = 1
DEFAULT_HAPROXY_SERVER_MAXQUEUE = 1
DEFAULT_HAPROXY_QUEUE_TIMEOUT_MS = 60000


@dataclass
class ClusterManagerConfig:
    base_image: str
    vm_dir: str
    template_xml: str
    haproxy_base_cfg: str
    haproxy_cfg: str
    haproxy_pid_file: str
    haproxy_stats_socket: str
    network_name: str
    log_file: str
    max_vms: int
    min_vms: int
    cpu_high_threshold: float
    cpu_low_threshold: float
    time_to_scale: int
    time_interval: int
    haproxy_server_maxconn: int
    haproxy_server_maxqueue: int
    haproxy_queue_timeout_ms: int


class ClusterManager:
    def __init__(self, config: ClusterManagerConfig):
        self.config = config
        try:
            self.conn = libvirt.open("qemu:///system")
        except libvirt.libvirtError as e:
            logging.error(f"Could not connect to KVM: {e}")
            exit(1)
        self.active_vms = {}  # {vm_name: ip_address}
        self.last_action_time = time.time()
        self.last_probe = time.time()
        self.high_usage_acc = 0
        self.low_usage_acc = 0

    def get_cpu_usage(self, domain):
        """Calculates % of CPU usage for given machine via libvirt API"""
        try:
            if not domain.isActive():
                return 0.0
            stats1 = domain.getCPUStats(True)[0]
            t1 = time.time()
            time.sleep(1)  # Probe for 1 second
            stats2 = domain.getCPUStats(True)[0]
            t2 = time.time()

            cpu_time_diff = stats2["cpu_time"] - stats1["cpu_time"]
            time_diff = t2 - t1
            vcpus = domain.info()[3]  # Number of cores

            usage = (cpu_time_diff / (time_diff * 1e9 * vcpus)) * 100
            return min(100.0, usage)
        except Exception:
            return 0.0

    def create_vm(self, vm_index):
        """Creates new VM as Linked Clone"""
        vm_name = f"worker-{vm_index}"
        new_disk = os.path.join(self.config.vm_dir, f"{vm_name}.qcow2")

        logging.info(f"[{vm_name}] Starting provisioning...")

        # Creating qcow2 backing file
        start_clone = time.time()
        subprocess.run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                self.config.base_image,
                new_disk,
            ],
            check=True,
        )
        duration = time.time() - start_clone
        logging.info(f"[{vm_name}] Disk cloned in {duration:.3f} seconds!")

        # Generate new XML based on template
        tree = ET.parse(self.config.template_xml)
        root = tree.getroot()

        # Change name, UUID and disk path
        root.find("name").text = vm_name
        root.find("uuid").text = str(uuid.uuid4())

        # Search for disk definition and swap source file
        for disk in root.findall(".//disk[@device='disk']"):
            disk.find("source").set("file", new_disk)

        # Change MAC address to avoid network conflicts
        mac = "52:54:00:%02x:%02x:%02x" % (
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
        )
        root.find(".//interface[@type='network']/mac").set("address", mac)

        # Define and start in KVM
        domain = self.conn.defineXML(ET.tostring(root).decode())
        domain.create()
        logging.info(f"[{vm_name}] Machine started. Awaiting IP address assignment...")

        # Wait for IP address to be assigned via DHCP
        ip_address = self._wait_for_ip(mac)
        logging.info(f"[{vm_name}] Done! Assigned IP address: {ip_address}")

        self.enable_haproxy_server(vm_name, ip_address)
        self.active_vms[vm_name] = ip_address
        self.last_action_time = time.time()
        self.high_usage_acc = 0
        self.low_usage_acc = 0

    def _wait_for_ip(self, mac_address):
        """Listens to DHCP leases in libvirt to find the IP of the new machine"""
        network = self.conn.networkLookupByName(self.config.network_name)
        for _ in range(30):
            for lease in network.DHCPLeases():
                if lease["mac"] == mac_address:
                    return lease["ipaddr"]
            time.sleep(1)
        raise Exception("Could not get IP address from DHCP in time!")

    def destroy_vm(self, vm_name):
        """Deletes VM and erases it's files (Scale-in)"""
        logging.info(f"[{vm_name}] Starting VM decommission...")
        if vm_name in self.active_vms:
            self.disable_haproxy_server(vm_name)
            del self.active_vms[vm_name]

        try:
            domain = self.conn.lookupByName(vm_name)
            if domain.isActive():
                domain.destroy()  # Hard power-off
            domain.undefine()
        except libvirt.libvirtError as e:
            logging.error(f"libvirt error during destroying: {e}")

        disk_path = os.path.join(self.config.vm_dir, f"{vm_name}.qcow2")
        if os.path.exists(disk_path):
            os.remove(disk_path)

        logging.info(f"[{vm_name}] Machine utilized.")
        self.last_action_time = time.time()
        self.high_usage_acc = 0
        self.low_usage_acc = 0

    def _send_haproxy_command(self, command):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(self.config.haproxy_stats_socket)
            sock.sendall(f"{command}\n".encode())
            sock.shutdown(socket.SHUT_WR)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        return response.decode(errors="replace").strip()

    def enable_haproxy_server(self, vm_name, ip_address):
        self._send_haproxy_command(
            f"set server web_workers/{vm_name} addr {ip_address} port 80"
        )
        self._send_haproxy_command(f"enable server web_workers/{vm_name}")
        logging.info(f"[HAPROXY] Enabled {vm_name} at {ip_address}:80")

    def disable_haproxy_server(self, vm_name):
        try:
            self._send_haproxy_command(f"disable server web_workers/{vm_name}")
            logging.info(f"[HAPROXY] Disabled {vm_name}")
        except OSError as e:
            logging.error(f"[HAPROXY] Could not disable {vm_name}: {e}")

    def start_haproxy(self):
        with open(self.config.haproxy_base_cfg, "r") as f:
            cfg = f.read()

        cfg += (
            "\nbackend web_workers\n"
            "    balance leastconn\n"
            "    option redispatch\n"
            f"    timeout queue {self.config.haproxy_queue_timeout_ms}ms\n"
        )
        for vm_index in range(1, self.config.max_vms + 1):
            cfg += (
                f"    server worker-{vm_index} 127.0.0.1:1 "
                "disabled check inter 2000 rise 2 fall 3 "
                f"maxconn {self.config.haproxy_server_maxconn} "
                f"maxqueue {self.config.haproxy_server_maxqueue}\n"
            )

        with open(self.config.haproxy_cfg, "w") as f:
            f.write(cfg)

        cmd = [
            "haproxy",
            "-f",
            self.config.haproxy_cfg,
            "-p",
            self.config.haproxy_pid_file,
            "-D",
        ]
        if os.path.exists(self.config.haproxy_pid_file):
            with open(self.config.haproxy_pid_file, "r") as f:
                old_pids = f.read().split()
            if old_pids:
                cmd.extend(["-sf"] + old_pids)

        subprocess.run(cmd, check=True)
        logging.info(
            f"[HAPROXY] Started with {self.config.max_vms} disabled worker slots"
        )

    def run(self):
        """Main script loop"""
        logging.info("=== Starting Cluster Manager ===")
        self.start_haproxy()
        # Initialization: provision minimal number of VMs
        for i in range(1, self.config.min_vms + 1):
            self.create_vm(i)

        while True:
            try:
                # State check (HIGH AVAILABILITY - FAILOVER)
                crashed_vms = []
                cpu_by_vm = {}
                active_count = len(self.active_vms)

                for vm_name in list(self.active_vms.keys()):
                    try:
                        domain = self.conn.lookupByName(vm_name)
                        state, _ = domain.state()
                        # State 1 is VIR_DOMAIN_RUNNING
                        if state != 1:
                            logging.warning(
                                f"[FAILURE] Machine {vm_name} not responding! (State: {state})"
                            )
                            crashed_vms.append(vm_name)
                        else:
                            cpu_by_vm[vm_name] = self.get_cpu_usage(domain)
                    except libvirt.libvirtError:
                        crashed_vms.append(vm_name)

                # Failure handling
                for crashed in crashed_vms:
                    self.destroy_vm(crashed)
                    new_idx = int(crashed.split("-")[1])
                    self.create_vm(new_idx)  # Rebuild machine
                    continue  # Skip scaling logic in this cycle

                # Scaling logic (SCALE-OUT / SCALE-IN)
                if active_count > 0:
                    avg_cpu = sum(cpu_by_vm.values()) / active_count
                    max_cpu = max(cpu_by_vm.values(), default=0.0)
                    per_vm_cpu = ", ".join(
                        f"{vm}={cpu:.1f}%" for vm, cpu in cpu_by_vm.items()
                    )
                    logging.info(
                        f"[MONITORING] CPU avg={avg_cpu:.1f}% max={max_cpu:.1f}% | {per_vm_cpu} | Active nodes: {active_count}"
                    )

                    now = time.time()
                    elapsed = now - self.last_probe
                    self.high_usage_acc = (
                        self.high_usage_acc + elapsed
                        if avg_cpu >= self.config.cpu_high_threshold
                        else 0
                    )
                    self.low_usage_acc = (
                        self.low_usage_acc + elapsed
                        if avg_cpu <= self.config.cpu_low_threshold
                        else 0
                    )

                    cooldown_elapsed = now - self.last_action_time
                    if cooldown_elapsed > self.config.time_to_scale:
                        # Scale OUT
                        if (
                            self.high_usage_acc >= self.config.time_to_scale
                            and active_count < self.config.max_vms
                        ):
                            logging.info(
                                f"[SCALE OUT] Surpassed {self.config.cpu_high_threshold}% for {self.high_usage_acc:.2f} seconds. Creating new machine..."
                            )
                            # Look for free index
                            new_idx = (
                                max(
                                    [
                                        int(n.split("-")[1])
                                        for n in self.active_vms.keys()
                                    ]
                                )
                                + 1
                            )
                            self.create_vm(new_idx)

                        # Scale IN
                        elif (
                            self.low_usage_acc >= self.config.time_to_scale
                            and active_count > self.config.min_vms
                        ):
                            logging.info(
                                f"[SCALE IN] Fell below {self.config.cpu_low_threshold}% for {self.low_usage_acc:.2f} seconds. Removing redundant machine..."
                            )
                            vm_to_remove = list(self.active_vms.keys())[
                                -1
                            ]  # Remove the newest
                            self.destroy_vm(vm_to_remove)

                    self.last_probe = now

                # interval - number of active machines because we take 1 second to probe CPU for each one
                time.sleep(
                    max(
                        0,
                        self.config.time_interval - len(list(self.active_vms.keys())),
                    )
                )

            except KeyboardInterrupt:
                logging.info("Stopping Cluster. Removing all machines...")
                for vm in list(self.active_vms.keys()):
                    self.destroy_vm(vm)
                break
            except Exception as e:
                logging.error(f"Main loop error: {e}")


def parse_arguments() -> ClusterManagerConfig:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Python3 script for autoscaling virtual machines (workers). "
        "Scaling (out/in) events occur on persistent high/low cpu usage. Traffic is balanced between workers using HAproxy. "
        "Needs KVM virtualizator installed on host with sufficient vCPUs and RAM. "
        'Worker base image is expected to have a web server listening on port 8080 with a "GET /" endpoint; see server.py for an example. '
        "Most capabilities (worker image, HAproxy config, scaling parameters, etc.) can be configured using the options below:",
    )
    parser.add_argument(
        "--base-image", default=DEFAULT_BASE_IMAGE, help="Base worker QCOW2 disk image"
    )
    parser.add_argument(
        "--vm-dir",
        default=DEFAULT_VM_DIR,
        help="Directory on the host where QCOW2 disk images are located",
    )
    parser.add_argument(
        "--template-xml",
        default=DEFAULT_TEMPLATE_XML,
        help="Worker domain XML template",
    )
    parser.add_argument(
        "--haproxy-base-cfg",
        default=DEFAULT_HAPROXY_BASE_CFG,
        help="Base HAproxy config file location",
    )
    parser.add_argument(
        "--haproxy-cfg",
        default=DEFAULT_HAPROXY_CFG,
        help="Runtime HAproxy config file location",
    )
    parser.add_argument(
        "--haproxy-pid-file",
        default=DEFAULT_HAPROXY_PID_FILE,
        help="Runtime HAproxy pid file location",
    )
    parser.add_argument(
        "--haproxy-stats-socket",
        default=DEFAULT_HAPROXY_STATS_SOCKET,
        help="HAProxy Runtime API socket path",
    )
    parser.add_argument(
        "--haproxy-server-maxconn",
        type=int,
        default=DEFAULT_HAPROXY_SERVER_MAXCONN,
        help="Maximum concurrent HAProxy connections sent to each worker slot",
    )
    parser.add_argument(
        "--haproxy-server-maxqueue",
        type=int,
        default=DEFAULT_HAPROXY_SERVER_MAXQUEUE,
        help="Maximum queued HAProxy connections kept on each worker slot",
    )
    parser.add_argument(
        "--haproxy-queue-timeout-ms",
        type=int,
        default=DEFAULT_HAPROXY_QUEUE_TIMEOUT_MS,
        help="Maximum time in milliseconds a request may wait in HAProxy's queue before a worker slot is available",
    )
    parser.add_argument(
        "--network-name",
        default=DEFAULT_NETWORK_NAME,
        help="Virtual network for worker VMs connection name",
    )
    parser.add_argument(
        "--log-file", default=DEFAULT_LOG_FILE, help="Log file location"
    )
    parser.add_argument(
        "--max-vms",
        type=int,
        default=DEFAULT_MAX_VMS,
        help="Maximum concurrent VMs on the host",
    )
    parser.add_argument(
        "--min-vms",
        type=int,
        default=DEFAULT_MIN_VMS,
        help="Minimum concurrent VMs on the host",
    )
    parser.add_argument(
        "--cpu-high-threshold",
        type=float,
        default=DEFAULT_CPU_HIGH_THRESHOLD,
        help="CPU usage percentage above which to scale out",
    )
    parser.add_argument(
        "--cpu-low-threshold",
        type=float,
        default=DEFAULT_CPU_LOW_THRESHOLD,
        help="CPU usage percentage below which to scale in",
    )
    parser.add_argument(
        "--time-to-scale",
        type=int,
        default=DEFAULT_TIME_TO_SCALE,
        help="Time in seconds the CPU usage must stay past a threshold to trigger a scaling event",
    )
    parser.add_argument(
        "--time-interval",
        type=int,
        default=DEFAULT_TIME_INTERVAL,
        help="Time interval in seconds between cluster monitoring checks",
    )

    args = parser.parse_args()
    return ClusterManagerConfig(**vars(args))


def main():
    config = parse_arguments()

    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(message)s",
        handlers=[logging.FileHandler(config.log_file), logging.StreamHandler()],
    )

    manager = ClusterManager(config)
    manager.run()


if __name__ == "__main__":
    main()
