#!/usr/bin/env python3
"""
kvm-bridge-wifi -- give a KVM/libvirt guest "bridged" networking over Wi-Fi.

Why this exists
---------------
A real 802.11 bridge needs four MAC addresses in the frame header (WDS /
4-address mode).  Normal access points use three, so they drop any frame whose
source MAC is not the associated station's MAC.  That is why
`br0 + wlp3s0` and `macvtap` do not work: the guest's own MAC never survives
the trip through the AP.

VirtualBox and VMware Fusion do not beat that rule either.  They translate:
every frame leaving the host carries the *host's* MAC, and every frame coming
back is rewritten to the *guest's* MAC before it is handed to the VM.  The AP
only ever sees one device, while the guest keeps its own IP on the LAN subnet.
Fusion calls this "MAC address translation".

This program implements the same idea in user space, for a tap device:

    guest  <-- tap (vnet0) -->  kvm-bridge-wifi  <-- AF_PACKET -->  wlp3s0

Usage
-----
    sudo kvm-bridge-wifi install --vm <domain>    # set everything up
    sudo kvm-bridge-wifi status                   # what is happening right now
    sudo kvm-bridge-wifi doctor                   # find configuration problems
    sudo kvm-bridge-wifi run                      # foreground (systemd does this)
    kvm-bridge-wifi selftest                      # offline checks, no root needed

Requirements
------------
  * root (raw sockets, promisc on the tap)
  * Python 3.10+
  * the VM NIC must be an *unbridged* tap:  <interface type='ethernet'>
  * the AP must allow client-to-client traffic for the guest's subnet
    (client isolation, if enabled, blocks it -- same as VirtualBox)

What works
----------
  * DHCP from the real router, same subnet as the host
  * guest -> LAN / internet
  * LAN / internet -> guest (the router learns "guest IP is at host MAC")
  * IPv4 and IPv6 (ARP / NDP link-layer addresses are rewritten too)
  * guest -> host (the guest's ARP for the host is answered locally)

Known limits
------------
  * the LAN sees the host's MAC, not the guest's (that is the whole trick)
  * MAC-based policy (port security, MAC filtering, captive portals) applies
    to the host
  * an AP that filters unknown source MACs will not work -- but then neither
    does VirtualBox
  * host -> guest needs --host-access, which adds a /32 route into the tap;
    without it the host has no L2 path to the guest
"""

from __future__ import annotations

import argparse
import atexit
import configparser
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800
ETH_P_ARP = 0x0806
ETH_P_IPV6 = 0x86DD
VLAN_TYPES = (0x8100, 0x88A8, 0x9100)

PACKET_HOST = 0
PACKET_BROADCAST = 1
PACKET_MULTICAST = 2
PACKET_OTHERHOST = 3
PACKET_OUTGOING = 4

BROADCAST = b"\xff" * 6
ZERO_MAC = b"\x00" * 6

SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
SIOCGIFADDR = 0x8915
IFF_PROMISC = 0x100

MAX_AGE = 3600.0  # forget learned addresses after an hour of silence

LOCK_PATH = "/run/kvm-bridge-wifi.lock"
REFRESH_SECONDS = 60.0  # gratuitous-ARP refresh interval

CONFIG_PATH = "/etc/kvm-bridge-wifi/kvm-bridge-wifi.conf"
STATE_DIR = "/var/lib/kvm-bridge-wifi"
STATE_PATH = f"{STATE_DIR}/state.json"
INSTALL_DIR = "/usr/local/lib/kvm-bridge-wifi"
BIN_PATH = "/usr/local/bin/kvm-bridge-wifi"
GUI_BIN_PATH = "/usr/local/bin/kvm-bridge-wifi-gui"
TUI_BIN_PATH = "/usr/local/bin/kvm-bridge-wifi-tui"
UNIT_PATH = "/etc/systemd/system/kvm-bridge-wifi.service"
DESKTOP_PATH = "/usr/share/applications/kvm-bridge-wifi.desktop"

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=kvm-bridge-wifi
GenericName=Bridged VM Networking
Comment=Give a KVM virtual machine bridged networking over Wi-Fi
Exec=/usr/local/bin/kvm-bridge-wifi-gui
Icon=network-wireless
Terminal=false
Categories=System;Settings;Network;
Keywords=vm;kvm;libvirt;virt-manager;bridge;wifi;network;
StartupNotify=true
"""

# Used when GTK is unavailable: same menu entry, but it opens a terminal.
DESKTOP_ENTRY_TERMINAL = """[Desktop Entry]
Type=Application
Name=kvm-bridge-wifi
GenericName=Bridged VM Networking
Comment=Give a KVM virtual machine bridged networking over Wi-Fi
Exec=/usr/local/bin/kvm-bridge-wifi-tui
Icon=utilities-terminal
Terminal=true
Categories=System;Settings;Network;
Keywords=vm;kvm;libvirt;virt-manager;bridge;wifi;network;
"""

NIC_XML = "<interface type='ethernet'><model type='virtio'/></interface>"

DEFAULTS = {
    # Comma-separated list of enabled domains.  Empty means "nothing enabled
    # yet" -- the GUI (or `kvm-bridge-wifi enable`) fills this in.
    "vms": "",
    "uplink": "auto",
    "host_access": "yes",
    "ipv6": "yes",
    "verbose": "no",
}

MIN_PYTHON = (3, 10)

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[kvm-bridge-wifi] {msg}", flush=True)


def take_lock() -> "object | None":
    """
    Refuse to run twice against the same tap.

    Two instances both inject into the tap, so every packet is delivered
    twice -- which looks like mysterious duplicates and packet loss, not like
    a configuration mistake.
    """
    try:
        fh = open(LOCK_PATH, "w")
    except OSError:
        return None  # unwritable /run; don't block the user
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return "busy"
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def mac_str(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


def is_multicast(mac: bytes) -> bool:
    return bool(mac[0] & 1)


def if_mac(ifname: str) -> bytes:
    with open(f"/sys/class/net/{ifname}/address", "r", encoding="ascii") as fh:
        return bytes.fromhex(fh.read().strip().replace(":", ""))


def if_exists(ifname: str) -> bool:
    return os.path.exists(f"/sys/class/net/{ifname}")


def if_ipv4_addrs(ifname: str) -> list[bytes]:
    """Primary IPv4 addresses of an interface (enough for our purposes)."""
    out: list[bytes] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", ifname.encode()[:15])
        try:
            res = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, req)
            out.append(res[20:24])
        except OSError:
            pass
    finally:
        sock.close()
    return out


def set_promisc(ifname: str, enabled: bool = True) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("16sH", ifname.encode()[:15], 0)
        cur = fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS, req)
        flags = struct.unpack("16sH", cur)[1]
        flags = (flags | IFF_PROMISC) if enabled else (flags & ~IFF_PROMISC)
        fcntl.ioctl(
            sock.fileno(), SIOCSIFFLAGS, struct.pack("16sH", ifname.encode()[:15], flags)
        )
    finally:
        sock.close()


def set_tap_offloads(ifname: str, enable: bool = False) -> bool:
    """
    Turn TSO/GSO/GRO off on a tap, from the host side.

    virtio-net derives its features from the tap, so this stops the guest
    from handing us 64 KB super-frames that no Wi-Fi driver will transmit.
    Done with a raw SIOCETHTOOL ioctl because ethtool is not always installed.
    """
    import ctypes

    SIOCETHTOOL = 0x8946
    ETHTOOL_STSO = 0x0000001F
    ETHTOOL_SGSO = 0x00000024
    ETHTOOL_SGRO = 0x0000002C

    ok = False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for cmd in (ETHTOOL_STSO, ETHTOOL_SGSO, ETHTOOL_SGRO):
            value = struct.pack("II", cmd, 1 if enable else 0)
            holder = ctypes.create_string_buffer(value)
            ifr = struct.pack(
                "16sP", ifname.encode()[:15], ctypes.addressof(holder)
            )
            try:
                fcntl.ioctl(sock.fileno(), SIOCETHTOOL, ifr)
                ok = True
            except OSError:
                pass
    finally:
        sock.close()
    return ok


def write_sysctl(path: str, value: str) -> bool:
    try:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(value)
        return True
    except OSError:
        return False


def cfg_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "yes", "true", "on")


# --------------------------------------------------------------------------
# cross-distribution dependency checks
# --------------------------------------------------------------------------

# Package names differ per distribution.  Each entry maps a capability to the
# command that installs it, so the message a user sees is copy-pasteable.
PACKAGES = {
    "debian": {
        "gtk": "sudo apt install python3-gi gir1.2-gtk-3.0",
        "libvirt": "sudo apt install libvirt-clients libvirt-daemon-system",
        "pkexec": "sudo apt install pkexec",
    },
    "fedora": {
        "gtk": "sudo dnf install python3-gobject gtk3",
        "libvirt": "sudo dnf install libvirt-client libvirt-daemon",
        "pkexec": "sudo dnf install polkit",
    },
    "arch": {
        "gtk": "sudo pacman -S python-gobject gtk3",
        "libvirt": "sudo pacman -S libvirt",
        "pkexec": "sudo pacman -S polkit",
    },
    "suse": {
        "gtk": "sudo zypper install python3-gobject gtk3",
        "libvirt": "sudo zypper install libvirt-client libvirt-daemon",
        "pkexec": "sudo zypper install polkit",
    },
    "alpine": {
        "gtk": "sudo apk add py3-gobject3 gtk+3.0",
        "libvirt": "sudo apk add libvirt-client libvirt-daemon",
        "pkexec": "sudo apk add polkit",
    },
}


def os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                values[key] = value.strip('"')
    except OSError:
        pass
    return values


def distro_family() -> str:
    """Which package-manager family this looks like."""
    ids = " ".join(
        [os_release().get("ID", ""), os_release().get("ID_LIKE", "")]
    ).lower()
    for name in ("debian", "ubuntu", "linuxmint", "pop"):
        if name in ids:
            return "debian"
    for name in ("fedora", "rhel", "centos", "rocky", "almalinux"):
        if name in ids:
            return "fedora"
    for name in ("arch", "manjaro", "endeavouros"):
        if name in ids:
            return "arch"
    if "suse" in ids or "opensuse" in ids:
        return "suse"
    if "alpine" in ids:
        return "alpine"
    return ""


def package_hint(capability: str) -> str:
    family = distro_family()
    if family and family in PACKAGES:
        return PACKAGES[family][capability]
    return {
        "gtk": "install python3-gobject / python3-gi and GTK 3",
        "libvirt": "install libvirt and the virsh client",
        "pkexec": "install polkit",
    }.get(capability, "")


def has_gtk() -> bool:
    """Can we import GTK 3?

    Importing Gtk as root without an X/Wayland session prints
    "Authorization required" on fd 2.  It is harmless, but it looks alarming
    in an installer, so the probe is run with fd 2 pointed at /dev/null.
    """
    try:
        import gi

        gi.require_version("Gtk", "3.0")
    except (ImportError, ValueError):
        return False

    saved = None
    devnull = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(2)
        os.dup2(devnull, 2)
    except OSError:
        saved = None

    try:
        from gi.repository import Gtk  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False
    finally:
        if saved is not None:
            try:
                os.dup2(saved, 2)
                os.close(saved)
            except OSError:
                pass
        if devnull is not None:
            try:
                os.close(devnull)
            except OSError:
                pass


def dependency_report(need_gui: bool = True) -> list[tuple[str, bool, str]]:
    """[(label, ok, fix-command)] for everything the project needs."""
    report = [
        (
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+",
            sys.version_info >= MIN_PYTHON,
            "upgrade your Python installation",
        ),
        (
            "virsh (libvirt client)",
            shutil.which("virsh") is not None,
            package_hint("libvirt"),
        ),
        (
            "systemd (systemctl)",
            shutil.which("systemctl") is not None,
            "this project needs systemd to run the background service",
        ),
    ]
    if need_gui:
        report.append(
            (
                "GTK 3 for Python (graphical window)",
                has_gtk(),
                package_hint("gtk"),
            )
        )
    return report


def parse_vms(value: str) -> list[str]:
    """Split the configured VM list, accepting commas or whitespace."""
    return [item for item in value.replace(",", " ").split() if item]


def join_vms(vms: list[str]) -> str:
    return ",".join(vms)


def load_config(path: str = CONFIG_PATH) -> dict[str, str]:
    cfg = dict(DEFAULTS)
    try:
        parser = configparser.ConfigParser()
        parser.read(path)
    except (OSError, configparser.Error):
        return cfg
    if not parser.has_section("kvm-bridge-wifi"):
        return cfg

    for key in DEFAULTS:
        if parser.has_option("kvm-bridge-wifi", key):
            cfg[key] = parser.get("kvm-bridge-wifi", key).strip()

    # Older versions had a single `vm` key.  Accept it so an upgrade does not
    # silently forget which VM was bridged.
    if not cfg["vms"] and parser.has_option("kvm-bridge-wifi", "vm"):
        cfg["vms"] = parser.get("kvm-bridge-wifi", "vm").strip()
    return cfg


def write_config(cfg: dict[str, str], path: str = CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# kvm-bridge-wifi configuration -- see README.md\n")
        fh.write("# Normally you edit this through the GUI; if you edit by hand,\n")
        fh.write("# apply the change with: sudo systemctl restart kvm-bridge-wifi\n\n")
        fh.write("[kvm-bridge-wifi]\n")
        fh.write("# Domains to bridge, comma separated.  Empty = none enabled.\n")
        fh.write(f"vms = {cfg.get('vms', '')}\n")
        for key in DEFAULTS:
            if key == "vms":
                continue
            fh.write(f"{key} = {cfg.get(key, DEFAULTS[key])}\n")


def detect_uplink() -> str | None:
    """Interface carrying the default route, preferring a wireless one."""
    try:
        out = subprocess.run(
            ["ip", "-4", "route", "show", "default"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None

    devices = []
    for line in out.splitlines():
        parts = line.split()
        if "dev" in parts:
            index = parts.index("dev")
            if index + 1 < len(parts):
                devices.append(parts[index + 1])
    if not devices:
        return None
    for dev in devices:
        if os.path.isdir(f"/sys/class/net/{dev}/wireless"):
            return dev
    return devices[0]


def is_wireless(ifname: str) -> bool:
    return os.path.isdir(f"/sys/class/net/{ifname}/wireless")


def virsh(args: list[str], timeout: int = 20):
    """Run virsh against the system connection. Returns CompletedProcess."""
    return subprocess.run(
        ["virsh", "-c", "qemu:///system", *args],
        capture_output=True, text=True, timeout=timeout,
    )


def vm_running(vm: str) -> bool:
    try:
        return "running" in virsh(["domstate", vm]).stdout
    except (OSError, subprocess.SubprocessError):
        return False


def vm_exists(vm: str) -> bool:
    try:
        return virsh(["dominfo", vm]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tap_for_vm(vm: str) -> str | None:
    """
    Ask libvirt which tap belongs to the VM's type='ethernet' NIC.

    `domiflist` prints one row per NIC: <iface> <type> <source> <model> <mac>.
    For a type='ethernet' NIC the interface column is the tap we want.
    """
    try:
        result = virsh(["domiflist", vm])
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "ethernet":
            return parts[0]
    return None


def vm_has_ethernet_nic(vm: str, persistent: bool = True) -> bool:
    """Does the VM have a type='ethernet' NIC?

    persistent=True asks about the saved config (what survives a restart);
    persistent=False asks about the running domain.
    """
    args = ["dumpxml", vm]
    if persistent:
        args.insert(1, "--inactive")
    try:
        xml = virsh(args).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "type='ethernet'" in xml


def load_state(path: str = STATE_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class StateStore:
    """
    Learned per-VM state (guest MAC and addresses).

    Several VMs can be bridged at once, so each bridge writes its own entry.
    Writes are serialised: two threads must never interleave a read-modify-
    write on the same JSON file.
    """

    def __init__(self, path: str | None = STATE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        raw = load_state(path) if path else {}
        # Migrate the old flat format: {guest_mac, guest_ips} -> {vms: {...}}
        self._legacy: dict = {}
        if isinstance(raw.get("vms"), dict):
            self.data = raw
        else:
            self.data = {"vms": {}}
            if raw.get("guest_mac") or raw.get("guest_ips"):
                self._legacy = {
                    "guest_mac": raw.get("guest_mac"),
                    "guest_ips": raw.get("guest_ips") or [],
                }

    def adopt_legacy(self, vm: str) -> None:
        """Hand the pre-upgrade state to the only configured VM."""
        if self._legacy and not self.data["vms"]:
            self.data["vms"][vm] = dict(self._legacy)
            self._legacy = {}
            self._write()

    def get(self, vm: str) -> dict:
        with self._lock:
            return dict(self.data["vms"].get(vm) or {})

    def update(
        self,
        vm: str,
        guest_mac: str | None = None,
        guest_ips: list[str] | None = None,
        tap: str | None = None,
    ) -> None:
        with self._lock:
            entry = self.data["vms"].setdefault(vm, {})
            if guest_mac is not None:
                entry["guest_mac"] = guest_mac
            if guest_ips is not None:
                entry["guest_ips"] = guest_ips
            if tap is not None:
                entry["tap"] = tap
            entry["updated"] = int(time.time())
            self._write()

    def remove(self, vm: str) -> None:
        with self._lock:
            if self.data["vms"].pop(vm, None) is not None:
                self._write()

    def _write(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log(f"could not save state: {exc}")


def find_tap() -> str | None:
    """A libvirt tap that is not attached to a bridge (type='ethernet')."""
    candidates = []
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return None
    for name in names:
        if not (name.startswith("vnet") or name.startswith("tap")):
            continue
        if os.path.exists(f"/sys/class/net/{name}/master"):
            continue  # enslaved to a bridge -> not ours
        try:
            if if_mac(name) == ZERO_MAC:
                continue
        except OSError:
            continue
        candidates.append(name)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        log(f"several candidate taps found: {', '.join(candidates)}")
    return None


def eth_parse(data: bytes):
    """Return (dst, src, ethertype, payload_offset) or None."""
    if len(data) < 14:
        return None
    dst, src = data[0:6], data[6:12]
    etype = int.from_bytes(data[12:14], "big")
    off = 14
    while etype in VLAN_TYPES and len(data) >= off + 4:
        etype = int.from_bytes(data[off + 2 : off + 4], "big")
        off += 4
    return dst, src, etype, off


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def fix_icmpv6_checksum(buf: bytearray, off: int) -> None:
    """Recompute the ICMPv6 checksum after we edited a link-layer option."""
    payload_len = int.from_bytes(buf[off + 4 : off + 6], "big")
    next_header = buf[off + 6]
    if next_header != 58 or payload_len < 4:
        return
    src = bytes(buf[off + 8 : off + 24])
    dst = bytes(buf[off + 24 : off + 40])
    msg = bytes(buf[off + 40 : off + 40 + payload_len])
    pseudo = src + dst + struct.pack("!I", payload_len) + b"\x00\x00\x00" + bytes([next_header])
    buf[off + 42 : off + 44] = b"\x00\x00"
    value = checksum(pseudo + msg)
    buf[off + 42 : off + 44] = struct.pack("!H", value if value else 0xFFFF)


def rewrite_ndp_link_layer(buf: bytearray, off: int, new_mac: bytes) -> bool:
    """
    Rewrite the Source/Target Link-Layer Address option of an ICMPv6
    NS/NA/RS/RA so the router maps the guest's IPv6 address to the host MAC.
    """
    if len(buf) < off + 48 or buf[off + 6] != 58:
        return False
    icmp_type = buf[off + 40]
    if icmp_type == 135 or icmp_type == 136:  # NS / NA
        opt = off + 64
    elif icmp_type == 133:  # RS
        opt = off + 48
    elif icmp_type == 134:  # RA
        opt = off + 56
    else:
        return False

    payload_len = int.from_bytes(buf[off + 4 : off + 6], "big")
    end = min(len(buf), off + 40 + payload_len)
    changed = False
    while opt + 2 <= end:
        otype, olen = buf[opt], buf[opt + 1]
        if olen == 0:
            break
        if otype in (1, 2) and olen == 1 and opt + 8 <= end:  # SLLAO / TLLAO
            buf[opt + 2 : opt + 8] = new_mac
            changed = True
            break
        opt += olen * 8
    if changed:
        fix_icmpv6_checksum(buf, off)
    return changed


def _l4_checksum_ok(segment: bytes, pseudo: bytes) -> bool:
    """A valid L4 checksum makes the pseudo-header sum come out as zero."""
    return checksum(pseudo + segment) == 0


def fix_l4_checksum(buf: bytearray, off: int) -> bool:
    """
    Repair a TCP/UDP checksum the guest left to the virtual NIC.

    virtio advertises NETIF_F_HW_CSUM, so the guest may hand us a frame with
    the checksum field unfilled, expecting hardware to do it.  Nothing does:
    we copy raw bytes out to the Wi-Fi interface, and the router drops the
    packet.  Symptom: ping works (ICMP is always done in software) while DNS
    and TCP hang.

    We verify first and only rewrite a checksum that is actually wrong, so
    frames the guest did compute correctly are left byte-for-byte alone.
    """
    if len(buf) < off + 20:
        return False
    version = buf[off] >> 4
    frag = int.from_bytes(buf[off + 6 : off + 8], "big")
    if version == 4:
        ihl = (buf[off] & 0x0F) * 4
        if ihl < 20 or (frag & 0x1FFF):
            return False  # options / non-first fragment
        total_len = int.from_bytes(buf[off + 2 : off + 4], "big")
        proto = buf[off + 9]
        src = bytes(buf[off + 12 : off + 16])
        dst = bytes(buf[off + 16 : off + 20])
        l4 = off + ihl
        seg_len = total_len - ihl
    elif version == 6:
        if (frag & 0x0FFF):
            return False
        payload_len = int.from_bytes(buf[off + 4 : off + 6], "big")
        proto = buf[off + 6]
        src = bytes(buf[off + 8 : off + 24])
        dst = bytes(buf[off + 24 : off + 40])
        l4 = off + 40
        seg_len = payload_len
    else:
        return False

    if proto not in (6, 17) or seg_len < 8:
        return False
    if len(buf) < l4 + seg_len:
        return False

    csum_at = l4 + (16 if proto == 6 else 6)
    if csum_at + 2 > len(buf):
        return False

    segment = bytes(buf[l4 : l4 + seg_len])
    pseudo = (
        src
        + dst
        + b"\x00"
        + bytes([proto])
        + struct.pack("!H", seg_len)
    )

    if _l4_checksum_ok(segment, pseudo):
        return False  # guest did it properly

    fixed = bytearray(segment)
    fixed[csum_at - l4 : csum_at - l4 + 2] = b"\x00\x00"
    value = checksum(pseudo + bytes(fixed))
    buf[csum_at : csum_at + 2] = struct.pack("!H", value if value else 0xFFFF)
    return True


def force_dhcp_broadcast(buf: bytearray, off: int, ihl: int) -> bool:
    """
    Set the BOOTP 'broadcast' flag on a DHCP client->server packet.

    Without it the server is free to reply unicast to chaddr -- the guest's
    MAC -- which the AP will drop because it has never heard of that MAC.
    Setting the flag makes the offer come back as a broadcast we can relay.
    """
    udp = off + ihl
    if len(buf) < udp + 8 + 240:
        return False
    sport = int.from_bytes(buf[udp : udp + 2], "big")
    dport = int.from_bytes(buf[udp + 2 : udp + 4], "big")
    if sport != 68 or dport != 67:
        return False
    bootp = udp + 8
    if buf[bootp + 236 : bootp + 240] != b"\x63\x82\x53\x63":
        return False
    if buf[bootp + 10] & 0x80:
        return False  # already set

    buf[bootp + 10] |= 0x80

    # UDP checksum covers the payload, so recompute it (unless it is disabled).
    if int.from_bytes(buf[udp + 6 : udp + 8], "big") == 0:
        return True
    udp_len = int.from_bytes(buf[udp + 4 : udp + 6], "big")
    src = bytes(buf[off + 12 : off + 16])
    dst = bytes(buf[off + 16 : off + 20])
    pseudo = src + dst + b"\x00" + bytes([buf[off + 9]]) + struct.pack("!H", udp_len)
    buf[udp + 6 : udp + 8] = b"\x00\x00"
    value = checksum(pseudo + bytes(buf[udp : udp + udp_len]))
    buf[udp + 6 : udp + 8] = struct.pack("!H", value if value else 0xFFFF)
    return True


def parse_dhcp(frame: bytes, off: int, ihl: int):
    """Return (yiaddr, chaddr) for a DHCP server->client packet, else None."""
    udp = off + ihl
    if len(frame) < udp + 8 + 236 + 4:
        return None
    sport, dport = int.from_bytes(frame[udp : udp + 2], "big"), int.from_bytes(
        frame[udp + 2 : udp + 4], "big"
    )
    if sport != 67 or dport != 68:
        return None
    bootp = udp + 8
    if frame[bootp + 236 : bootp + 240] != b"\x63\x82\x53\x63":
        return None
    return frame[bootp + 16 : bootp + 20], frame[bootp + 28 : bootp + 34]


# --------------------------------------------------------------------------
# bridge
# --------------------------------------------------------------------------


class WiFiBridge:
    def __init__(
        self,
        uplink: str,
        guest: str,
        guest_mac: bytes | None = None,
        seed_mac: bytes | None = None,
        handle_ipv6: bool = True,
        verbose: bool = False,
        dry_run: bool = False,
        host_access: bool = False,
        disable_offloads: bool = True,
        seed_ips: "list[bytes] | None" = None,
        state_store: "StateStore | None" = None,
        vm_name: str = "",
    ) -> None:
        self.uplink = uplink
        self.guest = guest
        self.handle_ipv6 = handle_ipv6
        self.verbose = verbose
        self.dry_run = dry_run
        self.host_access = host_access
        self.disable_offloads = disable_offloads
        self.state_store = state_store
        self.vm_name = vm_name
        # NOTE: forwarding on the uplink is deliberately left alone.  Turning
        # it off would break libvirt's NAT network (and docker) for every
        # other guest, which is a far worse problem than the one it solves.
        self._host_routes: set[str] = set()

        self.uplink_mac = if_mac(uplink)
        self.host_ips = set(if_ipv4_addrs(uplink))
        # guest_mac pins the address; seed_mac is only a starting guess and
        # is still replaced the moment a different MAC shows up.  That matters
        # because libvirt may hand the VM a new MAC when a NIC is re-created.
        self.fixed_guest_mac = guest_mac

        self._lock = threading.Lock()
        self.guest_mac: bytes | None = guest_mac or seed_mac
        self.guest_ips: dict[bytes, float] = {}
        self.guest_v6: dict[bytes, float] = {}
        for ip in seed_ips or ():
            self.guest_ips[ip] = time.time()

        self.uplink_sock: socket.socket | None = None
        self.guest_sock: socket.socket | None = None
        self._stop = threading.Event()
        self._stopped = False
        self._stop_lock = threading.Lock()
        # Set when the tap or uplink vanishes, so the supervisor can tell
        # "the VM was shut down" apart from "we were asked to stop".
        self.tap_lost = False

        try:
            with open(f"/sys/class/net/{uplink}/mtu", "r", encoding="ascii") as fh:
                self.max_frame = int(fh.read().strip()) + 18
        except OSError:
            self.max_frame = 1518
        self._warned_oversized = False

        self._counters = {
            "guest->lan": 0,
            "lan->guest": 0,
            "dropped": 0,
            "arp_local": 0,
            "oversized": 0,
            "errors": 0,
            "csum_fixed": 0,
            "arp_proxy": 0,
        }

    # -- setup ------------------------------------------------------------

    def _setup_tap(self) -> None:
        # Guest -> host packets arrive on the tap with a source address that
        # the host routes back out the uplink; strict rp_filter would drop
        # them.  Loose/strict both work with this off, and it is tap-local.
        write_sysctl(f"/proc/sys/net/ipv4/conf/{self.guest}/rp_filter", "0")
        # No IPv6 stack on the tap: avoids DAD/ND chatter from the host side.
        write_sysctl(f"/proc/sys/net/ipv6/conf/{self.guest}/disable_ipv6", "1")
        if not self.disable_offloads:
            pass
        elif set_tap_offloads(self.guest, enable=False):
            log(f"disabled TSO/GSO/GRO on {self.guest}")
        else:
            log("could not disable offloads on the tap; large transfers may stall")
        try:
            set_promisc(self.guest, True)
            log(f"{self.guest} set to promiscuous")
        except OSError as exc:
            log(f"warning: could not set promisc on {self.guest}: {exc}")

    def _open(self) -> None:
        self.uplink_sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
        )
        self.uplink_sock.bind((self.uplink, 0))
        self.uplink_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.uplink_sock.settimeout(0.5)

        self.guest_sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)
        )
        self.guest_sock.bind((self.guest, 0))
        self.guest_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.guest_sock.settimeout(0.5)

    # -- learning ---------------------------------------------------------

    def _keepalive(self) -> None:
        """Re-announce guest addresses so router ARP entries never go stale."""
        while not self._stop.is_set():
            for _ in range(int(REFRESH_SECONDS * 2)):
                if self._stop.is_set():
                    return
                time.sleep(0.5)
            with self._lock:
                ips = list(self.guest_ips)
            for ip in ips:
                self._announce(ip)

    def _save_state(self) -> None:
        """Remember what we learned so a restart works while the guest idles."""
        if self.state_store is None or self.dry_run:
            return
        with self._lock:
            mac = mac_str(self.guest_mac) if self.guest_mac else None
            ips = [socket.inet_ntoa(ip) for ip in self.guest_ips]
        self.state_store.update(
            self.vm_name, guest_mac=mac, guest_ips=ips, tap=self.guest
        )

    def _prune(self) -> None:
        now = time.time()
        for table in (self.guest_ips, self.guest_v6):
            for key in [k for k, ts in table.items() if now - ts > MAX_AGE]:
                del table[key]

    def _note_mac(self, mac: bytes) -> None:
        if is_multicast(mac) or mac == ZERO_MAC:
            return
        changed = False
        with self._lock:
            if self.fixed_guest_mac is None and self.guest_mac != mac:
                self.guest_mac = mac
                changed = True
        # _save_state() takes the same lock, so it must be called outside it.
        if changed:
            log(f"guest MAC is {mac_str(mac)}")
            self._save_state()

    def _arp_frame(
        self, oper: int, tha: bytes, tpa: bytes, spa: bytes, eth_dst: bytes = BROADCAST
    ) -> bytes:
        """Build an ARP packet sent by the host MAC."""
        frame = bytearray(eth_dst + self.uplink_mac + b"\x08\x06")
        frame += b"\x00\x01\x08\x00\x06\x04" + struct.pack("!H", oper)
        frame += self.uplink_mac + spa + tha + tpa
        return bytes(frame)

    def _announce(self, ip: bytes) -> None:
        """
        Gratuitous ARP: tell the LAN "this IP lives at the host's MAC".

        Routers forget ARP entries after a few minutes.  Without this, an
        idle guest becomes unreachable from the LAN until it happens to send
        something.  VMware and VirtualBox do the same thing.
        """
        if self.dry_run:
            return
        frame = self._arp_frame(1, ZERO_MAC, ip, ip)
        self._send_uplink(frame)
        if self.verbose:
            log(f"gratuitous ARP for {socket.inet_ntoa(ip)}")

    def _note_ip(self, ip: bytes, authoritative: bool = False) -> None:
        """
        Record an address the guest is using.

        authoritative=True means the router itself told us (a DHCP offer or
        ack for this guest's MAC).  That is the guest's current lease, so any
        other address we remember is stale and is dropped -- otherwise a
        guest that re-leases to a new address keeps showing the old one too.
        Addresses learned from ordinary traffic are additive, because a guest
        can legitimately hold more than one.
        """
        if ip == b"\x00\x00\x00\x00" or ip[0] == 0:
            return

        dropped: list[bytes] = []
        with self._lock:
            if authoritative and set(self.guest_ips) != {ip}:
                dropped = [old for old in self.guest_ips if old != ip]
            new = ip not in self.guest_ips
            if authoritative:
                self.guest_ips = {ip: time.time()}
            else:
                self.guest_ips[ip] = time.time()
            if new:
                log(f"guest IPv4 is {socket.inet_ntoa(ip)}")
            for old in dropped:
                log(f"guest IPv4 {socket.inet_ntoa(old)} is stale, forgetting it")
            self._prune()

        for old in dropped:
            self._drop_host_route(old)
        if new and self.host_access and not self.dry_run:
            self._add_host_route(ip)
        if new or dropped:
            self._announce(ip)
            self._save_state()

    def _drop_host_route(self, ip: bytes) -> None:
        text = socket.inet_ntoa(ip)
        if text not in self._host_routes:
            return
        try:
            subprocess.run(
                ["ip", "route", "del", f"{text}/32", "dev", self.guest],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
        self._host_routes.discard(text)

    def _add_host_route(self, ip: bytes) -> None:
        text = socket.inet_ntoa(ip)
        try:
            subprocess.run(
                ["ip", "route", "replace", f"{text}/32", "dev", self.guest],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._host_routes.add(text)
            log(f"host route {text}/32 -> {self.guest}")
        except OSError as exc:
            log(f"could not add host route for {text}: {exc}")

    def _del_host_routes(self) -> None:
        for text in list(self._host_routes):
            try:
                subprocess.run(
                    ["ip", "route", "del", f"{text}/32", "dev", self.guest],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass
            self._host_routes.discard(text)

    def _note_v6(self, ip: bytes) -> None:
        if ip == b"\x00" * 16:
            return
        with self._lock:
            new = ip not in self.guest_v6
            self.guest_v6[ip] = time.time()
            if new:
                log(f"guest IPv6 is {socket.inet_ntop(socket.AF_INET6, ip)}")
            self._prune()

    # -- guest -> LAN -----------------------------------------------------

    def _from_guest(self) -> None:
        assert self.guest_sock is not None and self.uplink_sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self.guest_sock.recvfrom(65535)
            except TimeoutError:
                if not if_exists(self.guest):
                    log(f"tap {self.guest} disappeared (VM stopped)")
                    self.tap_lost = True
                    self._stop.set()
                    return
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            if addr[2] == PACKET_OUTGOING:
                continue
            try:
                self._handle_guest_frame(data)
            except Exception as exc:  # one bad frame must not kill the bridge
                self._counters["errors"] += 1
                if self.verbose:
                    log(f"guest frame error: {exc!r}")

    def _handle_guest_frame(self, data: bytes) -> None:
        parsed = eth_parse(data)
        if parsed is None:
            return
        dst, src, etype, off = parsed
        if is_multicast(src) or src == ZERO_MAC:
            return

        self._note_mac(src)
        guest_mac = self.guest_mac or src

        if etype == ETH_P_ARP and len(data) >= off + 28:
            oper = int.from_bytes(data[off + 6 : off + 8], "big")
            sha = data[off + 8 : off + 14]
            spa = data[off + 14 : off + 18]
            tpa = data[off + 24 : off + 28]
            self._note_ip(spa)
            # The guest asking for the host itself: answer locally, the frame
            # must never leave the machine.
            if oper == 1 and tpa in self.host_ips:
                self._answer_arp_locally(data, off, guest_mac)
                return
            if oper == 2 and tpa in self.host_ips:
                return  # reply to the host itself, keep it local
            buf = bytearray(data)
            buf[off + 8 : off + 14] = self.uplink_mac  # ARP sender hardware
            buf[6:12] = self.uplink_mac
            self._send_uplink(buf)

        elif etype == ETH_P_IP and len(data) >= off + 20:
            ihl = (data[off] & 0x0F) * 4
            if ihl < 20 or len(data) < off + ihl:
                return
            src_ip = data[off + 12 : off + 16]
            dst_ip = data[off + 16 : off + 20]
            self._note_ip(src_ip)
            if dst_ip in self.host_ips:
                return  # local delivery, the kernel already sees it
            buf = bytearray(data)
            buf[6:12] = self.uplink_mac
            force_dhcp_broadcast(buf, off, ihl)
            if fix_l4_checksum(buf, off):
                self._counters["csum_fixed"] += 1
            self._send_uplink(buf)

        elif etype == ETH_P_IPV6 and len(data) >= off + 40:
            if not self.handle_ipv6:
                return
            self._note_v6(data[off + 8 : off + 24])
            buf = bytearray(data)
            rewrite_ndp_link_layer(buf, off, self.uplink_mac)
            buf[6:12] = self.uplink_mac
            if fix_l4_checksum(buf, off):
                self._counters["csum_fixed"] += 1
            self._send_uplink(buf)

    def _answer_arp_locally(self, request: bytes, off: int, guest_mac: bytes) -> None:
        req_spa = request[off + 14 : off + 18]
        req_tpa = request[off + 24 : off + 28]
        reply = bytearray(request)
        reply[0:6] = guest_mac
        reply[6:12] = self.uplink_mac
        reply[off + 6 : off + 8] = b"\x00\x02"  # ARP reply
        reply[off + 8 : off + 14] = self.uplink_mac
        reply[off + 14 : off + 18] = req_tpa
        reply[off + 18 : off + 24] = guest_mac
        reply[off + 24 : off + 28] = req_spa
        self._counters["arp_local"] += 1
        self._send_guest(reply)
        if self.verbose:
            log(f"answered ARP for host {socket.inet_ntoa(req_tpa)} locally")

    # -- LAN -> guest -----------------------------------------------------

    def _from_uplink(self) -> None:
        assert self.uplink_sock is not None and self.guest_sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self.uplink_sock.recvfrom(65535)
            except TimeoutError:
                if not if_exists(self.uplink):
                    log(f"uplink {self.uplink} disappeared")
                    self.tap_lost = True
                    self._stop.set()
                    return
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.1)
                continue
            if addr[2] in (PACKET_OUTGOING, PACKET_OTHERHOST):
                continue
            try:
                self._handle_uplink_frame(data)
            except Exception as exc:  # one bad frame must not kill the bridge
                self._counters["errors"] += 1
                if self.verbose:
                    log(f"uplink frame error: {exc!r}")

    def _handle_uplink_frame(self, data: bytes) -> None:
        parsed = eth_parse(data)
        if parsed is None:
            return
        dst, src, etype, off = parsed
        guest_mac = self.guest_mac
        if guest_mac is None:
            return

        forward = False
        buf = bytearray(data)

        if etype == ETH_P_ARP and len(data) >= off + 28:
            oper = int.from_bytes(data[off + 6 : off + 8], "big")
            tha = data[off + 18 : off + 24]
            tpa = data[off + 24 : off + 28]
            if oper == 2:  # reply
                if tpa in self.guest_ips and (tha == self.uplink_mac or tha == guest_mac):
                    forward = True
                    buf[off + 18 : off + 24] = guest_mac
            elif oper == 1:  # request
                if tpa in self.guest_ips:
                    # Answer on the guest's behalf: "that IP is at my MAC".
                    # Faster and more reliable than waking the guest, and it
                    # is exactly what proxy ARP does.
                    self._send_uplink(
                        self._arp_frame(
                            2, src, data[off + 14 : off + 18], tpa, eth_dst=src
                        )
                    )
                    self._counters["arp_proxy"] += 1
                    return
            if forward:
                buf[0:6] = guest_mac

        elif etype == ETH_P_IP and len(data) >= off + 20:
            ihl = (data[off] & 0x0F) * 4
            if ihl < 20 or len(data) < off + ihl:
                return
            dst_ip = data[off + 16 : off + 20]
            if dst_ip in self.guest_ips:
                forward = True
            elif is_multicast(dst):
                forward = True
            # DHCP replies addressed to us (unicast to the host MAC, or
            # broadcast) belong to the guest when chaddr matches.
            dhcp = parse_dhcp(data, off, ihl)
            if dhcp is not None:
                yiaddr, chaddr = dhcp
                if chaddr == guest_mac and yiaddr != b"\x00\x00\x00\x00":
                    # The router just told us the lease; trust it over
                    # anything learned from passing traffic.
                    self._note_ip(yiaddr, authoritative=True)
                    forward = True
            if forward:
                buf[0:6] = guest_mac

        elif etype == ETH_P_IPV6 and len(data) >= off + 40:
            if not self.handle_ipv6:
                return
            dst_ip = data[off + 24 : off + 40]
            if dst_ip in self.guest_v6:
                forward = True
            elif is_multicast(dst):
                forward = True
            if forward:
                buf[0:6] = guest_mac

        if forward:
            self._send_guest(buf)

    # -- transmit ---------------------------------------------------------

    def _send_uplink(self, frame: bytes) -> None:
        # virtio TSO/GSO can hand us a 64 KB "super-frame" that no Wi-Fi
        # driver will transmit.  Drop it and explain the fix once.
        if len(frame) > self.max_frame:
            self._counters["oversized"] += 1
            if not self._warned_oversized:
                self._warned_oversized = True
                log(
                    f"warning: {len(frame)}-byte frame exceeds the {self.uplink} MTU; "
                    "large transfers will stall.  In the guest run: "
                    "sudo ethtool -K <iface> tso off gso off gro off"
                )
            return
        if self.verbose:
            parsed = eth_parse(bytes(frame))
            if parsed:
                _, _, etype, off = parsed
                if etype == ETH_P_IP and len(frame) >= off + 20:
                    log(
                        f"guest -> lan  {socket.inet_ntoa(frame[off + 12:off + 16])} -> "
                        f"{socket.inet_ntoa(frame[off + 16:off + 20])}"
                    )
        self._counters["guest->lan"] += 1
        if not self.dry_run and self.uplink_sock is not None:
            try:
                self.uplink_sock.send(frame)
            except OSError as exc:
                self._counters["dropped"] += 1
                if self.verbose:
                    log(f"uplink send failed: {exc}")

    def _send_guest(self, frame: bytes) -> None:
        self._counters["lan->guest"] += 1
        if not self.dry_run and self.guest_sock is not None:
            try:
                self.guest_sock.send(frame)
            except OSError as exc:
                self._counters["dropped"] += 1
                if self.verbose:
                    log(f"tap send failed: {exc}")

    # -- lifecycle --------------------------------------------------------

    def run(self) -> None:
        log(f"uplink {self.uplink}  mac {mac_str(self.uplink_mac)}")
        log(f"host addresses: {', '.join(socket.inet_ntoa(a) for a in self.host_ips) or '-'}")
        log(f"guest tap {self.guest}")
        if self.fixed_guest_mac:
            log(f"guest MAC pinned to {mac_str(self.fixed_guest_mac)}")

        self._setup_tap()
        self._open()

        # Seeded addresses: install routes and tell the LAN about them right
        # away, so a restart while the guest is idle still works.
        with self._lock:
            seeded = list(self.guest_ips)
        for ip in seeded:
            log(f"seeded guest IPv4 {socket.inet_ntoa(ip)}")
            if self.host_access and not self.dry_run:
                self._add_host_route(ip)
            self._announce(ip)

        log("running -- Ctrl-C to stop")

        threads = [
            threading.Thread(target=self._from_guest, name="tap->wlan", daemon=True),
            threading.Thread(target=self._from_uplink, name="wlan->tap", daemon=True),
            threading.Thread(target=self._keepalive, name="arp-refresh", daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            while any(t.is_alive() for t in threads):
                for thread in threads:
                    thread.join(timeout=0.5)
                if self._stop.is_set():
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._stop.set()
        if self.uplink_sock:
            self.uplink_sock.close()
        if self.guest_sock:
            self.guest_sock.close()
        self._del_host_routes()
        try:
            set_promisc(self.guest, False)
        except OSError:
            pass
        log(
            "stopped: {guest->lan} guest->lan, {lan->guest} lan->guest, "
            "{arp_local} local ARP, {dropped} dropped, "
            "{oversized} oversized, {errors} errors, "
            "{csum_fixed} checksums fixed, {arp_proxy} proxy ARP".format(
                **self._counters
            )
        )


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------


def selftest() -> int:
    """Exercise the frame rewriting on synthetic packets."""
    uplink = bytes.fromhex("b0ac82c60e1d")
    guest = bytes.fromhex("5254001e3e7a")
    router = bytes.fromhex("aabbccddeeff")
    gw_ip = socket.inet_aton("192.168.1.2")
    guest_ip = socket.inet_aton("192.168.1.50")

    # --- outbound ARP request: guest asks who has the gateway -------------
    arp = bytearray(b"\xff" * 6 + guest + b"\x08\x06")
    arp += b"\x00\x01\x08\x00\x06\x04\x00\x01"
    arp += guest + guest_ip + b"\x00" * 6 + gw_ip
    bridge = WiFiBridge.__new__(WiFiBridge)
    bridge.uplink_mac = uplink
    bridge.host_ips = set()
    bridge.verbose = False
    bridge.dry_run = True
    bridge.handle_ipv6 = True
    bridge.host_access = False
    bridge._host_routes = set()
    bridge.state_store = None
    bridge.vm_name = "selftest"
    bridge._lock = threading.Lock()
    bridge.guest_mac = None
    bridge.fixed_guest_mac = None
    bridge.guest_ips = {}
    bridge.guest_v6 = {}
    bridge._counters = {
        k: 0
        for k in (
            "guest->lan", "lan->guest", "dropped", "arp_local",
            "oversized", "errors", "csum_fixed", "arp_proxy",
        )
    }
    sent = []
    bridge._send_uplink = lambda f: sent.append(bytes(f))
    bridge._send_guest = lambda f: sent.append(bytes(f))

    bridge._handle_guest_frame(bytes(arp))
    assert len(sent) == 1, "outbound ARP was not forwarded"
    out = sent[0]
    assert out[6:12] == uplink, "ethernet source MAC was not translated"
    assert out[22:28] == uplink, "ARP sender hardware address was not translated"
    assert out[28:32] == guest_ip, "ARP sender protocol address changed"
    assert bridge.guest_ips and guest_ip in bridge.guest_ips, "guest IPv4 not learned"
    assert bridge.guest_mac == guest, "guest MAC not learned"

    # --- inbound ARP reply: router answers the gateway --------------------
    sent.clear()
    reply = bytearray(uplink + router + b"\x08\x06")
    reply += b"\x00\x01\x08\x00\x06\x04\x00\x02"
    reply += router + gw_ip + uplink + guest_ip
    bridge._handle_uplink_frame(bytes(reply))
    assert len(sent) == 1, "inbound ARP reply was not forwarded"
    assert sent[0][0:6] == guest, "ethernet destination was not translated"
    assert sent[0][32:38] == guest, "ARP target hardware address was not translated"

    # --- inbound unicast IPv4 for the guest -------------------------------
    sent.clear()
    ip = bytearray(uplink + router + b"\x08\x00")
    ip += bytes([0x45, 0]) + b"\x00\x28\x00\x01\x00\x00\x40\x06\x00\x00"
    ip += gw_ip + guest_ip
    ip += b"\x00" * 20
    bridge._handle_uplink_frame(bytes(ip))
    assert len(sent) == 1 and sent[0][0:6] == guest, "inbound IPv4 not delivered"

    # --- inbound unicast IPv4 for somebody else must be ignored -----------
    sent.clear()
    other = bytearray(ip)
    other[30:34] = socket.inet_aton("192.168.1.77")
    bridge._handle_uplink_frame(bytes(other))
    assert not sent, "leaked a packet that was not for the guest"

    # --- inbound ARP request for the guest is answered by proxy ARP -------
    sent.clear()
    ask = bytearray(b"\xff" * 6 + router + b"\x08\x06")
    ask += b"\x00\x01\x08\x00\x06\x04\x00\x01"
    ask += router + gw_ip + b"\x00" * 6 + guest_ip
    bridge._handle_uplink_frame(bytes(ask))
    assert len(sent) == 1, "proxy ARP reply missing"
    assert sent[0][20:22] == b"\x00\x02", "proxy reply is not an ARP reply"
    assert sent[0][22:28] == uplink, "proxy reply must carry the host MAC"
    assert sent[0][28:32] == guest_ip, "proxy reply has the wrong sender IP"
    assert sent[0][0:6] == router, "proxy reply must be unicast to the asker"

    # --- outbound IPv4: source MAC must become the host's -----------------
    sent.clear()
    out_ip = bytearray(router + guest + b"\x08\x00")
    out_ip += bytes([0x45, 0]) + b"\x00\x28\x00\x02\x00\x00\x40\x06\x00\x00"
    out_ip += guest_ip + socket.inet_aton("8.8.8.8")
    out_ip += b"\x00" * 20
    bridge._handle_guest_frame(bytes(out_ip))
    assert len(sent) == 1 and sent[0][6:12] == uplink, "outbound IPv4 not rewritten"

    # --- DHCP offer: learn the lease and relay the broadcast --------------
    sent.clear()
    bootp = bytearray(240)
    bootp[0] = 2  # BOOTREPLY
    bootp[1] = 1  # ethernet
    bootp[2] = 6  # hlen
    bootp[16:20] = socket.inet_aton("192.168.1.51")  # yiaddr
    bootp[28:34] = guest  # chaddr
    bootp[236:240] = b"\x63\x82\x53\x63"
    offer = bytearray(b"\xff" * 6 + router + b"\x08\x00")
    offer += bytes([0x45, 0]) + struct.pack("!H", 20 + 8 + 240) + b"\x00\x03\x00\x00\x40\x11\x00\x00"
    offer += gw_ip + b"\xff\xff\xff\xff"
    offer += struct.pack("!HHHH", 67, 68, 8 + 240, 0)
    offer += bootp
    bridge._handle_uplink_frame(bytes(offer))
    assert socket.inet_aton("192.168.1.51") in bridge.guest_ips, "DHCP lease not learned"
    assert len(sent) == 1 and sent[0][0:6] == guest, "DHCP offer not relayed"

    # --- DHCP DISCOVER must leave with the broadcast flag set -------------
    discover = bytearray(router + guest + b"\x08\x00")
    discover += bytes([0x45, 0]) + struct.pack("!H", 20 + 8 + 240)
    discover += b"\x00\x04\x00\x00\x40\x11\x00\x00"
    discover += b"\x00\x00\x00\x00" + b"\xff\xff\xff\xff"
    discover += struct.pack("!HHHH", 68, 67, 8 + 240, 0)
    bootp_d = bytearray(240)
    bootp_d[0] = 1  # BOOTREQUEST
    bootp_d[2] = 6
    bootp_d[10:12] = b"\x00\x00"  # broadcast flag clear
    bootp_d[28:34] = guest
    bootp_d[236:240] = b"\x63\x82\x53\x63"
    discover += bootp_d
    buf = bytearray(discover)
    assert force_dhcp_broadcast(buf, 14, 20), "broadcast flag was not set"
    assert buf[14 + 20 + 8 + 10] & 0x80, "BOOTP broadcast flag still clear"
    assert force_dhcp_broadcast(buf, 14, 20) is False, "flag set twice"

    # --- outbound IPv4 to the host must stay local ------------------------
    sent.clear()
    bridge.host_ips = {socket.inet_aton("192.168.1.10")}
    local = bytearray(uplink + guest + b"\x08\x00")
    local += bytes([0x45, 0]) + b"\x00\x28\x00\x01\x00\x00\x40\x06\x00\x00"
    local += guest_ip + socket.inet_aton("192.168.1.10")
    local += b"\x00" * 20
    bridge._handle_guest_frame(bytes(local))
    assert not sent, "host-bound traffic was sent to the air"

    # --- guest ARP for the host is answered locally -----------------------
    sent.clear()
    ask_host = bytearray(b"\xff" * 6 + guest + b"\x08\x06")
    ask_host += b"\x00\x01\x08\x00\x06\x04\x00\x01"
    ask_host += guest + guest_ip + b"\x00" * 6 + socket.inet_aton("192.168.1.10")
    bridge._handle_guest_frame(bytes(ask_host))
    assert len(sent) == 1 and sent[0][0:6] == guest, "local ARP answer missing"
    assert sent[0][22:28] == uplink, "local ARP answer has wrong sender MAC"

    # --- L4 checksum repair: broken gets fixed, good is left alone --------
    def _tcp_frame(csum: bytes) -> bytearray:
        seg = bytearray(b"\x30\x39\x01\xbb" + b"\x00" * 8 + csum + b"\x00" * 12)
        seg += b"PAYLOAD!"
        frame = bytearray(router + guest + b"\x08\x00")
        frame += bytes([0x45, 0]) + struct.pack("!H", 20 + len(seg))
        frame += b"\x00\x05\x00\x00\x40\x06\x00\x00"
        frame += guest_ip + socket.inet_aton("8.8.8.8")
        frame += seg
        return frame

    good = bytearray(_tcp_frame(b"\x00\x00"))
    pseudo = guest_ip + socket.inet_aton("8.8.8.8") + b"\x00\x06"
    pseudo += struct.pack("!H", len(good) - 34)
    seg = bytes(good[34:])
    value = checksum(pseudo + seg)
    good[34 + 16 : 34 + 18] = struct.pack("!H", value)
    snapshot = bytes(good)
    assert fix_l4_checksum(good, 14) is False, "rewrote an already-correct checksum"
    assert bytes(good) == snapshot, "modified a correct frame"

    bad = _tcp_frame(b"\x00\x00")
    assert fix_l4_checksum(bad, 14) is True, "did not repair a broken checksum"
    assert bad[34 + 16 : 34 + 18] == struct.pack("!H", value), "repair produced a wrong value"

    # --- NDP link-layer option rewriting ----------------------------------
    ns = bytearray(uplink + guest + b"\x86\xdd")
    ns += b"\x60\x00\x00\x00" + struct.pack("!H", 32) + b"\x3a\xff"
    ns += b"\x00" * 16 + b"\x00" * 16  # source / destination IPv6
    ns += b"\x87\x00\x00\x00\x00\x00\x00\x00"
    ns += b"\x00" * 16  # target address
    ns += b"\x01\x01" + guest
    buf = bytearray(ns)
    assert rewrite_ndp_link_layer(buf, 14, uplink)
    assert buf[14 + 64 + 2 : 14 + 64 + 8] == uplink, "NDP SLLAO not rewritten"

    print("selftest: all checks passed")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


SERVICE_UNIT = """[Unit]
Description=kvm-bridge-wifi - bridged networking for a KVM guest over Wi-Fi
After=network-online.target libvirtd.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/kvm-bridge-wifi run
Restart=on-failure
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
"""


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def require_root() -> bool:
    if os.geteuid() != 0:
        log("this command needs root.  Try:  sudo kvm-bridge-wifi ...")
        return False
    return True


def wait_for_tap(vm: str, deadline: float, announced: "set[str] | None" = None) -> str | None:
    """Wait until the VM has a live tap, or the shared deadline passes.

    The deadline is absolute so that waiting for several VMs does not multiply
    the timeout.
    """
    while time.time() < deadline:
        tap = tap_for_vm(vm)
        if tap and if_exists(tap):
            return tap
        if announced is not None and vm not in announced:
            log(f"waiting for {vm} to start (Ctrl-C to stop waiting)")
            announced.add(vm)
        time.sleep(2.0)
    return None


def module_dir() -> str:
    """Directory holding the real source files.

    __file__ is used rather than argv[0] on purpose: under `pip install` the
    command is a generated wrapper that imports the module, so argv[0] points
    at a launcher, not at the code.  The sibling files (GUI, TUI, .desktop)
    sit next to this module either way.
    """
    return os.path.dirname(os.path.realpath(__file__))


def install_files(source: str | None = None, install_gui: bool = True) -> None:
    """Copy the program somewhere stable, add the GUI, write the unit."""
    os.makedirs(INSTALL_DIR, exist_ok=True)

    # Default to our own location.  Resolve symlinks first: when this is
    # re-run from /usr/local/bin/kvm-bridge-wifi (a symlink), the sibling
    # files live next to the *real* file, not next to the link.
    source = os.path.realpath(source or __file__)

    destination = os.path.join(INSTALL_DIR, "kvm_bridge_wifi.py")
    if os.path.realpath(source) != os.path.realpath(destination):
        shutil.copy2(source, destination)
    os.chmod(destination, 0o755)

    _symlink(destination, BIN_PATH)

    # The terminal interface needs only the standard library, so it is always
    # installed.  It is the fallback when GTK is unavailable.
    tui_source = os.path.join(os.path.dirname(os.path.abspath(source)), "kvm_bridge_wifi_tui.py")
    if os.path.exists(tui_source):
        tui_destination = os.path.join(INSTALL_DIR, "kvm_bridge_wifi_tui.py")
        if os.path.realpath(tui_source) != os.path.realpath(tui_destination):
            shutil.copy2(tui_source, tui_destination)
        os.chmod(tui_destination, 0o755)
        _symlink(tui_destination, TUI_BIN_PATH)

    # The GUI lives next to the CLI and is optional.  The menu entry is
    # written either way: it opens the window when GTK is present, and a
    # terminal running the text interface when it is not.
    gui_source = os.path.join(os.path.dirname(os.path.abspath(source)), "kvm_bridge_wifi_gui.py")
    gui_installed = install_gui and os.path.exists(gui_source)
    if gui_installed:
        gui_destination = os.path.join(INSTALL_DIR, "kvm_bridge_wifi_gui.py")
        if os.path.realpath(gui_source) != os.path.realpath(gui_destination):
            shutil.copy2(gui_source, gui_destination)
        os.chmod(gui_destination, 0o755)
        _symlink(gui_destination, GUI_BIN_PATH)

    if os.path.exists(tui_source) or gui_installed:
        _install_desktop_entry(os.path.dirname(source), use_gui=gui_installed)

    with open(UNIT_PATH, "w", encoding="utf-8") as fh:
        fh.write(SERVICE_UNIT)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    log(f"installed {destination} and {UNIT_PATH}")


def ensure_libvirt_group() -> None:
    """
    Let the invoking user talk to libvirtd without a password.

    libvirt ships a polkit rule that allows anyone in the `libvirt` group to
    connect to the system daemon.  Without it, every read of the VM list is
    denied, which makes the GUI and TUI look broken.  Adding the user once
    removes all prompts.
    """
    user = os.environ.get("SUDO_USER")
    if not user or user == "root":
        return

    try:
        import grp
        import pwd

        pwd.getpwnam(user)
        groups = grp.getgrnam("libvirt").gr_mem
    except (KeyError, ImportError):
        return  # no such user, or no libvirt group on this system

    if user in groups:
        return

    result = subprocess.run(
        ["usermod", "-aG", "libvirt", user], capture_output=True, text=True
    )
    if result.returncode == 0:
        log(f"added {user} to the 'libvirt' group")
        log("  (log out and back in for this to take effect)")
    else:
        log(f"could not add {user} to the libvirt group: {result.stderr.strip()}")


def _symlink(target: str, link: str) -> None:
    if os.path.islink(link) or os.path.exists(link):
        try:
            os.unlink(link)
        except OSError:
            return
    try:
        os.symlink(target, link)
    except OSError as exc:
        log(f"could not create {link}: {exc}")


def _install_desktop_entry(source_dir: str | None = None, use_gui: bool = True) -> None:
    """Put a 'kvm-bridge-wifi' icon in the application menu.

    With GTK the entry opens the window.  Without it, it opens a terminal
    running the text interface -- a menu entry that works is better than none.
    """
    if not os.path.isdir(os.path.dirname(DESKTOP_PATH)):
        return

    if use_gui:
        entry = DESKTOP_ENTRY
        if source_dir:
            shipped = os.path.join(source_dir, "kvm-bridge-wifi.desktop")
            try:
                with open(shipped, "r", encoding="utf-8") as fh:
                    entry = fh.read()
            except OSError:
                pass
    else:
        entry = DESKTOP_ENTRY_TERMINAL

    with open(DESKTOP_PATH, "w", encoding="utf-8") as fh:
        fh.write(entry)
    os.chmod(DESKTOP_PATH, 0o644)
    # Refresh the menu database where the tool exists; failure is harmless.
    for tool in (["update-desktop-database", "/usr/share/applications"],):
        if shutil.which(tool[0]):
            subprocess.run(tool, check=False, capture_output=True)


def add_nic_to_vm(vm: str) -> int:
    """Add a persistent type='ethernet' NIC so it survives VM restarts."""
    if not vm_exists(vm):
        log(f"no such libvirt domain: {vm}")
        return 1

    if vm_has_ethernet_nic(vm, persistent=True):
        log(f"{vm} already has a persistent ethernet NIC -- nothing to change")
        return 0

    running = vm_running(vm)
    # If the NIC exists only in the running domain (e.g. someone hot-plugged
    # it with --live), save it to the config instead of adding a second one.
    live_only = running and vm_has_ethernet_nic(vm, persistent=False)

    handle = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False)
    handle.write(NIC_XML)
    handle.close()
    try:
        command = ["attach-device", vm, handle.name, "--config"]
        if running and not live_only:
            command.append("--live")
        result = virsh(command)
        if result.returncode != 0:
            log(f"could not add the NIC: {result.stderr.strip()}")
            return 1
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

    if live_only:
        log(f"saved the existing NIC on {vm} so it survives a restart")
    else:
        log(f"added an unbridged NIC to {vm} (persists across restarts)")
        if not running:
            log("the bridge attaches automatically once you start the VM")
    return 0


def detach_network_nics(vm: str) -> int:
    """Remove NAT/bridge NICs, leaving only the unbridged one."""
    try:
        listing = virsh(["domiflist", vm]).stdout
    except (OSError, subprocess.SubprocessError):
        return 1

    macs = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] in ("network", "bridge"):
            macs.append(parts[-1])

    for mac in macs:
        xml = f"<interface type='network'><mac address='{mac}'/></interface>"
        handle = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False)
        handle.write(xml)
        handle.close()
        try:
            command = ["detach-device", vm, handle.name, "--config"]
            if vm_running(vm):
                command.append("--live")
            result = virsh(command)
            if result.returncode != 0:
                log(f"could not remove NIC {mac}: {result.stderr.strip()}")
            else:
                log(f"removed NIC {mac}")
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
    return 0


def cmd_install(args) -> int:
    """Install the program, service and GUI.  No VM is required.

    Picking a VM happens in the GUI afterwards, which is why --vm is only an
    optional shortcut for people who prefer the terminal.
    """
    if not require_root():
        return 1

    # Check the essentials before touching anything, so a missing package
    # produces one clear instruction instead of a half-finished install.
    blocking = [
        (label, fix)
        for label, ok, fix in dependency_report(need_gui=False)
        if not ok
    ]
    if blocking:
        log("cannot install yet -- missing requirements:")
        for label, fix in blocking:
            log(f"  {label}")
            if fix:
                log(f"      {fix}")
        return 1

    gui_ok = has_gtk()
    if not gui_ok:
        log("note: GTK 3 for Python is missing, so the window will be skipped.")
        log(f"      install it later with:  {package_hint('gtk')}")
        log("      then re-run this install to add the menu entry.")

    cfg = load_config(args.config)
    if args.uplink:
        cfg["uplink"] = args.uplink

    if cfg["uplink"] in ("", "auto"):
        detected = detect_uplink()
        if detected:
            cfg["uplink"] = detected
            log(f"detected uplink {detected}")
        else:
            log("could not detect the Wi-Fi interface; set 'uplink' in the config")

    if cfg["uplink"] and not is_wireless(cfg["uplink"]):
        log(f"note: {cfg['uplink']} is not wireless -- a real bridge would be better")

    write_config(cfg, args.config)
    log(f"wrote {args.config}")
    install_files(install_gui=gui_ok)
    ensure_libvirt_group()

    if args.vm:
        if add_nic_to_vm(args.vm) != 0:
            return 1
        enabled = parse_vms(cfg["vms"])
        if args.vm not in enabled:
            enabled.append(args.vm)
        cfg["vms"] = join_vms(enabled)
        write_config(cfg, args.config)
        log(f"enabled for {args.vm}")

    # Leave the service matching whatever is configured: running if at least
    # one VM is enabled (possibly from an earlier install), stopped otherwise.
    enabled = parse_vms(cfg["vms"])
    if _apply_service(enabled) != 0:
        return 1

    if enabled:
        log(f"service running for: {', '.join(enabled)}")
    else:
        log("installed.  No VM enabled yet -- pick one in the kvm-bridge-wifi window.")
    return 0


def cmd_uninstall(args) -> int:
    if not require_root():
        return 1

    cfg = load_config(args.config)
    enabled = parse_vms(cfg["vms"])

    subprocess.run(["systemctl", "disable", "--now", "kvm-bridge-wifi"], check=False)
    for path in (UNIT_PATH, BIN_PATH, GUI_BIN_PATH, TUI_BIN_PATH, DESKTOP_PATH):
        try:
            os.unlink(path)
        except OSError:
            pass
    subprocess.run(["systemctl", "daemon-reload"], check=False)

    if shutil.which("update-desktop-database") and os.path.isdir("/usr/share/applications"):
        subprocess.run(
            ["update-desktop-database", "/usr/share/applications"],
            check=False, capture_output=True,
        )

    if args.purge:
        for path in (args.config, STATE_PATH, INSTALL_DIR):
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
            except OSError:
                pass
        log("removed the program, service, config and saved state.")
    else:
        log("removed the program, service and menu entry.")
        log(f"config kept at {args.config}; saved state at {STATE_PATH}")
        log("run again with --purge to remove those too.")

    if enabled:
        log("")
        log("note: these VMs still have the extra network card:")
        for vm in enabled:
            log(f"  {vm}   (remove it in virt-manager, or with virsh)")
    return 0


def cmd_setup_vm(args) -> int:
    if not require_root():
        return 1
    return add_nic_to_vm(args.vm)


def _apply_service(enabled: list[str]) -> int:
    """Start, restart or stop the service to match the enabled VM list."""
    if not enabled:
        subprocess.run(["systemctl", "stop", "kvm-bridge-wifi"], check=False)
        subprocess.run(["systemctl", "disable", "kvm-bridge-wifi"], check=False)
        return 0

    # Install on first use so the GUI works straight after a fresh clone.
    if not os.path.exists(UNIT_PATH) or not os.path.exists(BIN_PATH):
        install_files(install_gui=has_gtk())

    subprocess.run(["systemctl", "enable", "kvm-bridge-wifi"], check=False)
    # restart, not start: the set of VMs (and therefore taps) just changed
    result = subprocess.run(
        ["systemctl", "restart", "kvm-bridge-wifi"], capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"could not start the service: {result.stderr.strip()}")
        return 1
    return 0


def cmd_enable(args) -> int:
    """Add a VM to the bridged set.  Idempotent: enabling twice is harmless."""
    if not require_root():
        return 1

    if not vm_exists(args.vm):
        log(f"no such libvirt domain: {args.vm}")
        return 1

    cfg = load_config(args.config)
    if args.uplink:
        cfg["uplink"] = args.uplink

    if cfg["uplink"] in ("", "auto"):
        detected = detect_uplink()
        if not detected:
            log("could not detect the Wi-Fi interface; set 'uplink' in the config")
            return 1
        cfg["uplink"] = detected

    if add_nic_to_vm(args.vm) != 0:
        return 1

    enabled = parse_vms(cfg["vms"])
    if args.vm not in enabled:
        enabled.append(args.vm)
    cfg["vms"] = join_vms(enabled)
    write_config(cfg, args.config)

    if _apply_service(enabled) != 0:
        return 1

    log(f"enabled for {args.vm}; active: {', '.join(enabled)}")
    return 0


def cmd_disable(args) -> int:
    """Remove one VM from the set, or stop everything when none is named."""
    if not require_root():
        return 1

    cfg = load_config(args.config)
    enabled = parse_vms(cfg["vms"])

    if getattr(args, "vm", None):
        if args.vm not in enabled:
            log(f"{args.vm} is not enabled")
            return 0
        enabled = [vm for vm in enabled if vm != args.vm]
    else:
        enabled = []

    cfg["vms"] = join_vms(enabled)
    write_config(cfg, args.config)

    if _apply_service(enabled) != 0:
        return 1

    if enabled:
        log(f"disabled {args.vm}; still active: {', '.join(enabled)}")
    elif getattr(args, "vm", None):
        log(f"disabled {args.vm}; nothing is bridged now")
    else:
        log("disabled all VMs")
    return 0


def cmd_run(args) -> int:
    if not require_root():
        return 1

    if sys.version_info < MIN_PYTHON:
        log(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required")
        return 1

    cfg = load_config(args.config)

    uplink = args.uplink or cfg["uplink"]
    if uplink in ("", "auto"):
        uplink = detect_uplink() or ""
        if not uplink:
            log("could not detect the Wi-Fi interface; set 'uplink' in the config")
            return 1
        log(f"auto-detected uplink {uplink}")
    if not if_exists(uplink):
        log(f"uplink {uplink} does not exist")
        return 1
    if not is_wireless(uplink):
        log(f"warning: {uplink} is not wireless; a real bridge would be better")

    # Which VMs to bridge.  --vm narrows this to a single one, which is handy
    # for debugging without touching the saved configuration.
    vms = [args.vm] if args.vm else parse_vms(cfg["vms"])
    if not vms:
        log("no VMs enabled yet.")
        log("Open the kvm-bridge-wifi window, or run:  sudo kvm-bridge-wifi enable --vm NAME")
        return 0

    lock = take_lock()
    if lock == "busy":
        log(f"another kvm-bridge-wifi is already running (lock {LOCK_PATH}) -- exiting")
        return 1

    store = None if args.no_state else StateStore(STATE_PATH)
    if store is not None and len(vms) == 1:
        store.adopt_legacy(vms[0])

    guest_mac = None
    if args.guest_mac:
        try:
            guest_mac = bytes.fromhex(args.guest_mac.replace(":", "").replace("-", ""))
        except ValueError:
            guest_mac = None
        if not guest_mac or len(guest_mac) != 6:
            log("--guest-mac is not a valid MAC address")
            return 1

    extra_ips: list[bytes] = []
    for chunk in args.guest_ip:
        for item in chunk.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                extra_ips.append(socket.inet_aton(item))
            except OSError:
                log(f"--guest-ip: {item} is not a valid IPv4 address")
                return 1

    # Resolve a tap per VM.  One shared deadline, so waiting for three VMs
    # does not mean waiting three times as long.
    deadline = time.time() + args.wait
    announced: set[str] = set()
    bridges: list[WiFiBridge] = []

    for vm in vms:
        guest = args.guest if (args.guest and len(vms) == 1) else None
        if guest:
            if not if_exists(guest):
                log(f"tap {guest} does not exist")
                return 1
        else:
            guest = wait_for_tap(vm, deadline, announced)
            if not guest:
                log(f"skipping {vm}: no unbridged NIC yet")
                log(f"  fix with:  sudo kvm-bridge-wifi setup-vm {vm}")
                continue

        # Per-VM remembered values.  The MAC is only a hint: libvirt may hand
        # the VM a new one, so learning stays enabled.
        seed_mac = None
        seed_ips = list(extra_ips)
        if store is not None:
            entry = store.get(vm)
            if guest_mac is None and entry.get("guest_mac"):
                try:
                    seed_mac = bytes.fromhex(str(entry["guest_mac"]).replace(":", ""))
                except ValueError:
                    seed_mac = None
            for text in entry.get("guest_ips") or []:
                try:
                    ip = socket.inet_aton(text)
                except OSError:
                    continue
                if ip not in seed_ips:
                    seed_ips.append(ip)

        log(f"bridging {vm} via {guest}")
        bridges.append(
            WiFiBridge(
                uplink=uplink,
                guest=guest,
                guest_mac=guest_mac,
                seed_mac=seed_mac,
                handle_ipv6=not args.no_ipv6 and cfg_bool(cfg["ipv6"]),
                verbose=args.verbose or cfg_bool(cfg["verbose"]),
                dry_run=args.dry_run,
                host_access=args.host_access or cfg_bool(cfg["host_access"]),
                disable_offloads=not args.keep_offloads,
                seed_ips=seed_ips,
                state_store=store,
                vm_name=vm,
            )
        )

    if not bridges:
        log("nothing to bridge -- no VM had a usable tap")
        return 1

    def _bye(*_args):
        for bridge in bridges:
            bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    atexit.register(lambda: [b.stop() for b in bridges])

    # Each bridge blocks in run(), so give each its own thread.
    threads = [
        threading.Thread(target=bridge.run, name=f"wb-{bridge.vm_name}", daemon=True)
        for bridge in bridges
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # A vanished tap means the VM was shut down or restarted.  Exit non-zero
    # so systemd's Restart=on-failure re-arms us and we pick the tap up again
    # when the VM comes back.  Exiting 0 here would leave the VM unbridged
    # until someone noticed.
    if any(bridge.tap_lost for bridge in bridges):
        return 1
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    vms = parse_vms(cfg["vms"])

    print(f"config:    {args.config}")
    print(f"uplink:    {cfg['uplink']}")

    active = subprocess.run(
        ["systemctl", "is-active", "kvm-bridge-wifi"], capture_output=True, text=True
    ).stdout.strip()
    print(f"service:   {active or 'unknown'}")

    main_pid = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "kvm-bridge-wifi"],
        capture_output=True, text=True,
    ).stdout.strip()
    if main_pid not in ("", "0"):
        print(f"bridge:    running (pid {main_pid})")
    else:
        print("bridge:    not running")

    if not vms:
        print("vms:       none enabled")
        return 0

    store = StateStore(None)  # read-only view of the on-disk state
    store.data = load_state()
    if not isinstance(store.data.get("vms"), dict):
        store.data = {"vms": {}}

    print("vms:")
    for vm in vms:
        tap = tap_for_vm(vm) or "-"
        nic = "present" if vm_has_ethernet_nic(vm) else "MISSING"
        entry = store.get(vm)
        ips = ", ".join(entry.get("guest_ips") or []) or "-"
        print(f"  {vm}")
        print(f"    nic:   {nic}")
        print(f"    tap:   {tap}")
        print(f"    guest: {ips}")
    return 0


def cmd_doctor(args) -> int:
    problems = 0

    def check(label: str, ok: bool, hint: str = "") -> None:
        nonlocal problems
        print(f"[{'ok' if ok else 'FAIL'}] {label}")
        if not ok:
            problems += 1
            if hint:
                print(f"       -> {hint}")

    check("running as root", os.geteuid() == 0, "use sudo")

    for label, ok, fix in dependency_report(need_gui=True):
        check(label, ok, fix)

    cfg = load_config(args.config)
    check(
        "config file exists",
        os.path.exists(args.config),
        "run: sudo kvm-bridge-wifi install",
    )

    uplink = cfg["uplink"]
    if uplink in ("", "auto"):
        uplink = detect_uplink() or ""
    check(f"uplink {uplink or '(none)'} exists", bool(uplink) and if_exists(uplink))
    if uplink:
        check(
            f"uplink {uplink} is wireless",
            is_wireless(uplink),
            "a wired link can use a real bridge instead",
        )

    vms = parse_vms(cfg["vms"])
    if not vms:
        print("[--] no VMs enabled yet (open the kvm-bridge-wifi window to pick one)")
    for vm in vms:
        check(f"domain {vm} exists", vm_exists(vm), "sudo virsh list --all")
        check(
            f"{vm} has a persistent unbridged NIC",
            vm_has_ethernet_nic(vm, persistent=True),
            f"run: sudo kvm-bridge-wifi setup-vm {vm}",
        )
        if vm_running(vm):
            tap = tap_for_vm(vm)
            check(f"{vm} tap {tap or '(none)'} exists", bool(tap) and if_exists(tap))
        else:
            print(f"[--] {vm} is not running (the tap appears when it starts)")

    print()
    print("all good" if problems == 0 else f"{problems} problem(s) found")
    return 0 if problems == 0 else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="kvm-bridge-wifi",
        description="Give a KVM/libvirt guest bridged networking over Wi-Fi.",
    )
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser(
        "install", help="install the program, service and GUI (no VM needed)"
    )
    p_install.add_argument(
        "--vm",
        help="optionally enable this domain straight away; otherwise pick one "
        "in the GUI",
    )
    p_install.add_argument("--uplink", help="Wi-Fi interface (default: auto-detect)")
    p_install.add_argument("--config", default=CONFIG_PATH)

    p_setup = sub.add_parser("setup-vm", help="add an unbridged NIC to a domain")
    p_setup.add_argument("vm")

    p_enable = sub.add_parser(
        "enable", help="bridge one VM and start the service (used by the GUI)"
    )
    p_enable.add_argument("--vm", required=True, help="libvirt domain")
    p_enable.add_argument("--uplink", help="override the uplink")
    p_enable.add_argument("--config", default=CONFIG_PATH)

    p_disable = sub.add_parser(
        "disable", help="stop bridging one VM (or all of them)"
    )
    p_disable.add_argument("--vm", help="domain to stop; omit to stop all")
    p_disable.add_argument("--config", default=CONFIG_PATH)

    p_run = sub.add_parser("run", help="run the bridge in the foreground")
    p_run.add_argument("--config", default=CONFIG_PATH)
    p_run.add_argument("--vm", help="bridge only this domain (ignores the list)")
    p_run.add_argument("-u", "--uplink", help="override the uplink")
    p_run.add_argument("-g", "--guest", help="tap device, or 'auto'")
    p_run.add_argument("--guest-mac", help="pin the guest MAC")
    p_run.add_argument("--guest-ip", action="append", default=[], metavar="IP")
    p_run.add_argument("--host-access", action="store_true")
    p_run.add_argument("--keep-offloads", action="store_true")
    p_run.add_argument("--no-ipv6", action="store_true")
    p_run.add_argument("--no-state", action="store_true")
    p_run.add_argument(
        "--wait", type=float, default=300.0, help="seconds to wait for the VM"
    )
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("-v", "--verbose", action="store_true")

    p_status = sub.add_parser("status", help="show current state")
    p_status.add_argument("--config", default=CONFIG_PATH)

    p_doctor = sub.add_parser("doctor", help="check the setup for problems")
    p_doctor.add_argument("--config", default=CONFIG_PATH)

    p_uninstall = sub.add_parser("uninstall", help="remove the service")
    p_uninstall.add_argument("--config", default=CONFIG_PATH)
    p_uninstall.add_argument(
        "--purge",
        action="store_true",
        help="also delete the config file and saved state",
    )

    sub.add_parser("selftest", help="run offline unit checks")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handlers = {
        "install": cmd_install,
        "setup-vm": cmd_setup_vm,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "run": cmd_run,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "uninstall": cmd_uninstall,
        "selftest": lambda _a: selftest(),
    }
    return handlers[args.command](args)


def cli() -> None:
    """Console entry point (used by pip and by the installed symlink)."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
