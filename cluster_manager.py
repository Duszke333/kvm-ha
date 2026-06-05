import logging
import os
import random
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

import libvirt

# CONFIGURATION
BASE_IMAGE = "alpine-base.qcow2"
VM_DIR = "/var/lib/libvirt/images/"
TEMPLATE_XML = "cluster/template.xml"
HAPROXY_CFG = "haproxy/haproxy.cfg"
HAPROXY_BASE_CFG = "haproxy/haproxy_base.cfg"
HAPROXY_PID_FILE = "haproxy/haproxy.pid"
NETWORK_NAME = "default"
LOG_FILE = "logs/cluster_manager.log"

MAX_VMS = 4
MIN_VMS = 1
CPU_HIGH_THRESHOLD = 80.0
CPU_LOW_THRESHOLD = 20.0
COOLDOWN_PERIOD = 30  # [seconds]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)


class ClusterManager:
    def __init__(self):
        try:
            self.conn = libvirt.open("qemu:///system")
        except libvirt.libvirtError as e:
            logging.error(f"Could not connect to KVM: {e}")
            exit(1)
        self.active_vms = {}  # {vm_name: ip_address}
        self.last_action_time = time.time() - COOLDOWN_PERIOD

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
        new_disk = os.path.join(VM_DIR, f"{vm_name}.qcow2")

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
                BASE_IMAGE,
                new_disk,
            ],
            check=True,
        )
        duration = time.time() - start_clone
        logging.info(f"[{vm_name}] Disk cloned in {duration:.3f} seconds!")

        # Generate new XML based on template
        tree = ET.parse(TEMPLATE_XML)
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
        self.active_vms[vm_name] = ip_address
        logging.info(f"[{vm_name}] Done! Assigned IP address: {ip_address}")

        self.update_haproxy()
        self.last_action_time = time.time()

    def _wait_for_ip(self, mac_address):
        """Listens to DHCP leases in libvirt to find the IP of the new machine"""
        network = self.conn.networkLookupByName(NETWORK_NAME)
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
            del self.active_vms[vm_name]
            self.update_haproxy()  # Remove from Load Balancer first (connection draining)

        try:
            domain = self.conn.lookupByName(vm_name)
            if domain.isActive():
                domain.destroy()  # Hard power-off
            domain.undefine()
        except libvirt.libvirtError as e:
            logging.error(f"libvirt error during destroying: {e}")

        disk_path = os.path.join(VM_DIR, f"{vm_name}.qcow2")
        if os.path.exists(disk_path):
            os.remove(disk_path)

        logging.info(f"[{vm_name}] Machine utilized.")
        self.last_action_time = time.time()

    def update_haproxy(self):
        with open(HAPROXY_BASE_CFG, "r") as f:
            cfg = f.read()

        cfg += "\nbackend web_workers\n    balance roundrobin\n"
        for vm, ip in self.active_vms.items():
            cfg += f"    server {vm} {ip}:80 check inter 2000 rise 2 fall 3\n"

        with open(HAPROXY_CFG, "w") as f:
            f.write(cfg)

        cmd = ["haproxy", "-f", HAPROXY_CFG, "-p", HAPROXY_PID_FILE, "-D"]
        if os.path.exists(HAPROXY_PID_FILE):
            with open(HAPROXY_PID_FILE, "r") as f:
                old_pids = f.read().split()
            if old_pids:
                cmd.extend(["-sf"] + old_pids)

        subprocess.run(cmd, check=True)
        logging.info(f"[HAPROXY] Config reloaded. Active nodes: {len(self.active_vms)}")

    def run(self):
        """Main script loop"""
        logging.info("=== Starting Cluster Manager ===")
        # Initialization: provision minimal number of VMs
        for i in range(1, MIN_VMS + 1):
            self.create_vm(i)

        while True:
            try:
                # State check (HIGH AVAILABILITY - FAILOVER)
                crashed_vms = []
                total_cpu = 0
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
                            total_cpu += self.get_cpu_usage(domain)
                    except libvirt.libvirtError:
                        crashed_vms.append(vm_name)

                # Failure handling
                for crashed in crashed_vms:
                    self.destroy_vm(crashed)
                    new_idx = int(crashed.split("-")[1])
                    self.create_vm(new_idx)  # Rebuild machine
                    continue  # Skip scaling logic in this cycle

                # 2. Scaling logic (SCALE-OUT / SCALE-IN)
                if active_count > 0:
                    avg_cpu = total_cpu / active_count
                    logging.info(
                        f"[MONITORING] Average Cluster CPU: {avg_cpu:.1f}% | Active nodes: {active_count}"
                    )

                    # Check if cooldown period has passed
                    if (time.time() - self.last_action_time) > COOLDOWN_PERIOD:
                        # Scale OUT
                        if avg_cpu > CPU_HIGH_THRESHOLD and active_count < MAX_VMS:
                            logging.info(
                                f"[SCALE OUT] Surpassed {CPU_HIGH_THRESHOLD}%. Creating new machine..."
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
                        elif avg_cpu < CPU_LOW_THRESHOLD and active_count > MIN_VMS:
                            logging.info(
                                f"[SCALE IN] Fell below {CPU_LOW_THRESHOLD}%. Removing redundant machine..."
                            )
                            vm_to_remove = list(self.active_vms.keys())[
                                -1
                            ]  # Remove the newest
                            self.destroy_vm(vm_to_remove)

                time.sleep(3)  # Wait before next loop

            except KeyboardInterrupt:
                logging.info("\nStopping Cluster. Removing all machines...")
                for vm in list(self.active_vms.keys()):
                    self.destroy_vm(vm)
                break
            except Exception as e:
                logging.error(f"Main loop error: {e}")
                time.sleep(5)


if __name__ == "__main__":
    manager = ClusterManager()
    manager.run()
