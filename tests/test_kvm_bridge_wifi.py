"""Offline tests for kvm_bridge_wifi.

These never touch a real interface, so they run anywhere -- including CI.
The end-to-end behaviour (DHCP, routing, throughput) needs a real VM and is
covered by the checklist in CONTRIBUTING.md.
"""

import os
import socket
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kvm_bridge_wifi  # noqa: E402


def test_selftest_passes():
    """The built-in frame-rewriting checks."""
    assert kvm_bridge_wifi.selftest() == 0


def test_checksum_known_value():
    # RFC 1071 worked example: 0x0001 + 0xf203 + 0xf4f5 + 0xf6f7 -> 0x220d
    data = struct.pack("!4H", 0x0001, 0xF203, 0xF4F5, 0xF6F7)
    assert kvm_bridge_wifi.checksum(data) == 0x220D


def test_mac_str():
    assert kvm_bridge_wifi.mac_str(bytes.fromhex("b0ac82c60e1d")) == "b0:ac:82:c6:0e:1d"


def test_is_multicast():
    assert kvm_bridge_wifi.is_multicast(b"\x01\x00\x5e\x00\x00\x01")
    assert kvm_bridge_wifi.is_multicast(b"\xff" * 6)
    assert not kvm_bridge_wifi.is_multicast(bytes.fromhex("b0ac82c60e1d"))


def test_cfg_bool():
    for value in ("yes", "YES", "1", "true", "on", " True "):
        assert kvm_bridge_wifi.cfg_bool(value) is True
    for value in ("no", "0", "false", "off", ""):
        assert kvm_bridge_wifi.cfg_bool(value) is False


def test_load_config_defaults(tmp_path):
    cfg = kvm_bridge_wifi.load_config(str(tmp_path / "missing.conf"))
    assert cfg["vms"] == ""
    assert cfg["uplink"] == "auto"
    assert cfg["host_access"] == "yes"


def test_write_and_load_config_roundtrip(tmp_path):
    path = str(tmp_path / "kvm-bridge-wifi.conf")
    kvm_bridge_wifi.write_config(
        {"vms": "vm-a,vm-b", "uplink": "wlan0",
         "host_access": "no", "ipv6": "yes", "verbose": "no"},
        path,
    )
    cfg = kvm_bridge_wifi.load_config(path)
    assert cfg["vms"] == "vm-a,vm-b"
    assert cfg["uplink"] == "wlan0"
    assert cfg["host_access"] == "no"


def test_legacy_single_vm_key_is_migrated(tmp_path):
    """An old config with `vm = name` must still work after upgrading."""
    path = tmp_path / "old.conf"
    path.write_text("[kvm-bridge-wifi]\nvm = parrot\nuplink = wlan0\n")
    cfg = kvm_bridge_wifi.load_config(str(path))
    assert cfg["vms"] == "parrot"


def test_parse_vms_accepts_commas_and_spaces():
    assert kvm_bridge_wifi.parse_vms("a,b") == ["a", "b"]
    assert kvm_bridge_wifi.parse_vms("a, b  c") == ["a", "b", "c"]
    assert kvm_bridge_wifi.parse_vms("") == []


def test_join_vms_roundtrip():
    vms = ["alpha", "beta"]
    assert kvm_bridge_wifi.parse_vms(kvm_bridge_wifi.join_vms(vms)) == vms


def test_load_state_missing(tmp_path):
    assert kvm_bridge_wifi.load_state(str(tmp_path / "nope.json")) == {}


def test_load_state_broken_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert kvm_bridge_wifi.load_state(str(path)) == {}


def test_load_state_non_dict(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]")
    assert kvm_bridge_wifi.load_state(str(path)) == {}


def test_dhcp_broadcast_flag_set_once():
    udp = 14 + 20
    bootp = udp + 8
    frame = bytearray(bootp + 240)

    frame[12:14] = b"\x08\x00"
    frame[14] = 0x45
    frame[14 + 9] = 17
    frame[udp:udp + 2] = struct.pack("!H", 68)
    frame[udp + 2:udp + 4] = struct.pack("!H", 67)
    frame[udp + 4:udp + 6] = struct.pack("!H", 8 + 240)
    frame[bootp + 236:bootp + 240] = b"\x63\x82\x53\x63"

    assert frame[bootp + 10] & 0x80 == 0
    assert kvm_bridge_wifi.force_dhcp_broadcast(frame, 14, 20) is True
    assert frame[bootp + 10] & 0x80
    assert kvm_bridge_wifi.force_dhcp_broadcast(frame, 14, 20) is False


def test_force_dhcp_ignores_non_dhcp():
    frame = bytearray(b"\x00" * 300)
    frame[14] = 0x45
    assert kvm_bridge_wifi.force_dhcp_broadcast(frame, 14, 20) is False


def test_tap_for_vm_parses_domiflist(monkeypatch):
    listing = (
        " Interface   Type       Source    Model    MAC\n"
        "-------------------------------------------------\n"
        " vnet11      network    default   virtio   52:54:00:1e:3e:7a\n"
        " vnet12      ethernet   -         virtio   52:54:00:d0:50:23\n"
    )

    class Result:
        returncode = 0
        stdout = listing
        stderr = ""

    monkeypatch.setattr(kvm_bridge_wifi, "virsh", lambda *a, **k: Result())
    assert kvm_bridge_wifi.tap_for_vm("vm") == "vnet12"


def test_tap_for_vm_without_ethernet(monkeypatch):
    class Result:
        returncode = 0
        stdout = " vnet11      network    default   virtio   52:54:00:1e:3e:7a\n"
        stderr = ""

    monkeypatch.setattr(kvm_bridge_wifi, "virsh", lambda *a, **k: Result())
    assert kvm_bridge_wifi.tap_for_vm("vm") is None


def test_vm_has_ethernet_nic(monkeypatch):
    class Result:
        returncode = 0
        stdout = "<interface type='ethernet'><model type='virtio'/></interface>"
        stderr = ""

    monkeypatch.setattr(kvm_bridge_wifi, "virsh", lambda *a, **k: Result())
    assert kvm_bridge_wifi.vm_has_ethernet_nic("vm") is True


def test_detect_uplink_prefers_wireless(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stdout = (
            "default via 192.168.1.2 dev eno1 proto dhcp metric 100\n"
            "default via 192.168.1.2 dev wlp3s0 proto dhcp metric 600\n"
        )
        stderr = ""

    monkeypatch.setattr(kvm_bridge_wifi.subprocess, "run", lambda *a, **k: Result())
    monkeypatch.setattr(kvm_bridge_wifi.os.path, "isdir", lambda p: "wlp3s0" in p)
    assert kvm_bridge_wifi.detect_uplink() == "wlp3s0"


def test_detect_uplink_none(monkeypatch):
    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(kvm_bridge_wifi.subprocess, "run", lambda *a, **k: Result())
    assert kvm_bridge_wifi.detect_uplink() is None


@pytest.mark.parametrize("bad", ["999.1.1.1", "not-an-ip", ""])
def test_invalid_seed_ip_rejected(bad):
    with pytest.raises(OSError):
        socket.inet_aton(bad)


# --------------------------------------------------------------------------
# GUI helpers (no display needed -- importing GTK is optional here)
# --------------------------------------------------------------------------

VIRSH_LIST = """\
 Id   Name          State
------------------------------
 12   linux2024-2   running
 -    linux2024     shut off
"""


def test_parse_vm_list_keeps_shut_off_vms():
    """A '-' Id means shut off, not a separator line."""
    import kvm_bridge_wifi_gui as gui

    vms = gui.parse_vm_list(VIRSH_LIST)
    assert ("linux2024-2", "running") in vms
    assert ("linux2024", "shut off") in vms
    assert len(vms) == 2


def test_parse_vm_list_empty():
    import kvm_bridge_wifi_gui as gui

    assert gui.parse_vm_list(" Id   Name   State\n------\n") == []


# --------------------------------------------------------------------------
# cross-distribution dependency helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "release,expected",
    [
        ({"ID": "debian"}, "debian"),
        ({"ID": "ubuntu", "ID_LIKE": "debian"}, "debian"),
        ({"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}, "debian"),
        ({"ID": "fedora"}, "fedora"),
        ({"ID": "rhel", "ID_LIKE": "fedora"}, "fedora"),
        ({"ID": "arch"}, "arch"),
        ({"ID": "manjaro", "ID_LIKE": "arch"}, "arch"),
        ({"ID": "opensuse-tumbleweed", "ID_LIKE": "suse"}, "suse"),
        ({"ID": "alpine"}, "alpine"),
        ({"ID": "gentoo"}, ""),
    ],
)
def test_distro_family(monkeypatch, release, expected):
    monkeypatch.setattr(kvm_bridge_wifi, "os_release", lambda: release)
    assert kvm_bridge_wifi.distro_family() == expected


def test_package_hint_is_copy_pasteable(monkeypatch):
    monkeypatch.setattr(kvm_bridge_wifi, "os_release", lambda: {"ID": "fedora"})
    assert kvm_bridge_wifi.package_hint("gtk").startswith("sudo dnf install")


def test_package_hint_unknown_distro_still_explains(monkeypatch):
    monkeypatch.setattr(kvm_bridge_wifi, "os_release", lambda: {"ID": "gentoo"})
    hint = kvm_bridge_wifi.package_hint("gtk")
    assert "gtk" in hint.lower()


def test_dependency_report_shape():
    report = kvm_bridge_wifi.dependency_report(need_gui=False)
    assert report, "expected at least one requirement"
    for label, ok, fix in report:
        assert isinstance(label, str) and label
        assert isinstance(ok, bool)
        assert isinstance(fix, str)
    labels = " ".join(item[0] for item in report)
    assert "virsh" in labels


def test_dependency_report_gui_is_optional_extra():
    without = kvm_bridge_wifi.dependency_report(need_gui=False)
    with_gui = kvm_bridge_wifi.dependency_report(need_gui=True)
    assert len(with_gui) == len(without) + 1
    assert "GTK" in with_gui[-1][0]


# --------------------------------------------------------------------------
# terminal interface helpers (stdlib only, so these run everywhere)
# --------------------------------------------------------------------------


def test_tui_parses_virsh_output():
    import kvm_bridge_wifi_tui as tui

    text = (
        " Id   Name          State\n"
        "------------------------------\n"
        " 12   linux2024-2   running\n"
        " -    linux2024     shut off\n"
    )
    assert tui.parse_virsh_list(text) == [
        ("linux2024-2", "running"),
        ("linux2024", "shut off"),
    ]


def test_tui_parses_empty_output():
    import kvm_bridge_wifi_tui as tui

    assert tui.parse_virsh_list(" Id   Name   State\n-------\n") == []


def test_tui_imports_without_gtk():
    """The whole point: usable on a machine with no GTK at all."""
    import kvm_bridge_wifi_tui as tui

    assert hasattr(tui, "run")
    assert hasattr(tui, "parse_virsh_list")


def test_tui_refuses_without_a_terminal(monkeypatch, capsys):
    import kvm_bridge_wifi_tui as tui

    monkeypatch.setattr(tui.sys.stdout, "isatty", lambda: False, raising=False)
    assert tui.main([]) == 1
    assert "needs a terminal" in capsys.readouterr().err


# --------------------------------------------------------------------------
# per-VM state store
# --------------------------------------------------------------------------


def test_state_store_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    store = kvm_bridge_wifi.StateStore(path)
    store.update("vm-a", guest_mac="52:54:00:00:00:01", guest_ips=["192.168.1.5"], tap="vnet0")

    again = kvm_bridge_wifi.StateStore(path)
    entry = again.get("vm-a")
    assert entry["guest_mac"] == "52:54:00:00:00:01"
    assert entry["guest_ips"] == ["192.168.1.5"]
    assert entry["tap"] == "vnet0"


def test_state_store_keeps_vms_separate(tmp_path):
    store = kvm_bridge_wifi.StateStore(str(tmp_path / "state.json"))
    store.update("a", guest_ips=["192.168.1.10"])
    store.update("b", guest_ips=["192.168.1.11"])
    assert store.get("a")["guest_ips"] == ["192.168.1.10"]
    assert store.get("b")["guest_ips"] == ["192.168.1.11"]


def test_state_store_remove(tmp_path):
    store = kvm_bridge_wifi.StateStore(str(tmp_path / "state.json"))
    store.update("a", guest_ips=["192.168.1.10"])
    store.remove("a")
    assert store.get("a") == {}


def test_state_store_without_path_is_memory_only():
    store = kvm_bridge_wifi.StateStore(None)
    store.update("a", guest_ips=["192.168.1.10"])
    assert store.get("a")["guest_ips"] == ["192.168.1.10"]


def test_authoritative_lease_replaces_old_addresses():
    """A new DHCP lease must not leave the previous address behind."""
    bridge = _bare_bridge()
    bridge._note_ip(socket.inet_aton("192.168.1.24"))
    assert socket.inet_aton("192.168.1.24") in bridge.guest_ips

    bridge._note_ip(socket.inet_aton("192.168.1.25"), authoritative=True)
    assert set(bridge.guest_ips) == {socket.inet_aton("192.168.1.25")}


def test_traffic_learned_addresses_are_additive():
    """Without a DHCP hint the guest may legitimately hold several addresses."""
    bridge = _bare_bridge()
    bridge._note_ip(socket.inet_aton("192.168.1.24"))
    bridge._note_ip(socket.inet_aton("192.168.1.25"))
    assert len(bridge.guest_ips) == 2


def _bare_bridge():
    bridge = kvm_bridge_wifi.WiFiBridge.__new__(kvm_bridge_wifi.WiFiBridge)
    bridge.uplink_mac = bytes.fromhex("b0ac82c60e1d")
    bridge.host_ips = set()
    bridge.verbose = False
    bridge.dry_run = True
    bridge.handle_ipv6 = True
    bridge.host_access = False
    bridge._host_routes = set()
    bridge.state_store = None
    bridge.vm_name = "test"
    bridge._lock = __import__("threading").Lock()
    bridge.guest_mac = None
    bridge.fixed_guest_mac = None
    bridge.guest_ips = {}
    bridge.guest_v6 = {}
    bridge._counters = {k: 0 for k in (
        "guest->lan", "lan->guest", "dropped", "arp_local",
        "oversized", "errors", "csum_fixed", "arp_proxy",
    )}
    bridge._send_uplink = lambda f: None
    bridge._send_guest = lambda f: None
    return bridge


def test_state_store_adopts_legacy_flat_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"guest_mac": "52:54:00:00:00:09", "guest_ips": ["192.168.1.7"]}')
    store = kvm_bridge_wifi.StateStore(str(path))
    assert store.get("anything") == {}

    store.adopt_legacy("solo")
    assert store.get("solo")["guest_mac"] == "52:54:00:00:00:09"
    assert store.get("solo")["guest_ips"] == ["192.168.1.7"]


def test_parse_vm_list_multiline_state():
    import kvm_bridge_wifi_gui as gui

    text = " -    vm1    shut off\n 3    vm2    paused\n"
    assert gui.parse_vm_list(text) == [("vm1", "shut off"), ("vm2", "paused")]
