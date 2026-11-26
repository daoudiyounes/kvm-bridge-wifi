# kvm-bridge-wifi

**Bridged networking for a KVM/libvirt VM on a Wi-Fi-only machine.**

Your VM gets an address from your real router — the same network as your
laptop — so other machines on the LAN can reach it. No Ethernet cable, no
router changes, no `br0`.

```
   ┌─────────┐   tap    ┌─────────────────┐   raw socket   ┌─────────┐   ┌────┐
   │   VM    │◄────────►│ kvm-bridge-wifi │◄──────────────►│ wlp3s0  │──►│ AP │
   │ .1.50   │  vnet0   │                 │   MAC rewrite  │ (Wi-Fi) │   └────┘
   └─────────┘          └─────────────────┘                └─────────┘
```

---

## Quick start

Two commands, no VM name to remember:

```bash
git clone https://github.com/younesdaoudi/kvm-bridge-wifi
cd kvm-bridge-wifi
sudo ./install.sh
```

No `git`? Download the ZIP from the green **Code** button on GitHub, unzip it,
and run `sudo ./install.sh` inside the folder.

The installer puts `kvm-bridge-wifi` in your application menu and **opens the
window for you**. Every VM is listed with an **Enable** button — click the
one you want, and that's it.

---

## Getting it onto a new machine

Three ways, in order of how much they give you.

### 1. Clone and run the installer — recommended

```bash
git clone https://github.com/younesdaoudi/kvm-bridge-wifi
cd kvm-bridge-wifi
sudo ./install.sh
```

This is the complete path: the command, the window, the terminal interface,
the systemd service, the menu entry, and the `libvirt` group fix. Nothing else
to do.

No `git` installed? Open the repo page, press the green **Code** button →
**Download ZIP**, unzip it, then inside the folder:

```bash
sudo ./install.sh
```

### 2. pip or pipx

```bash
pipx install git+https://github.com/younesdaoudi/kvm-bridge-wifi
sudo kvm-bridge-wifi install
```

The first command installs the three commands. The second is what actually
sets up the service, the menu entry and the group — `pip` cannot do those
itself, because they are system changes.

> **Heads up:** if you install into a virtual environment, that environment
> does not see the system's GTK, so you will get the terminal interface and
> not the window. Use option 1 if you want the window.

### 3. Run it without installing anything

From a clone, the daemon works directly:

```bash
sudo python3 kvm_bridge_wifi.py run
```

Useful for trying it once. It needs `--guest vnetN` or a VM in the config, so
option 1 is friendlier.

### What the installer changes

So you know what you are agreeing to:

| Change | Why |
|---|---|
| `/usr/local/lib/kvm-bridge-wifi/` | the program itself |
| `/usr/local/bin/kvm-bridge-wifi{,-gui,-tui}` | the commands |
| `/etc/systemd/system/kvm-bridge-wifi.service` | starts automatically |
| `/etc/kvm-bridge-wifi/kvm-bridge-wifi.conf` | your settings |
| `/var/lib/kvm-bridge-wifi/state.json` | learned guest addresses |
| `/usr/share/applications/kvm-bridge-wifi.desktop` | the menu entry |
| adds you to the `libvirt` group | so `virsh` needs no password |
| adds a `type='ethernet'` NIC to the VMs you enable | the tap to bridge |

`sudo kvm-bridge-wifi uninstall --purge` reverses all of it, except the extra
VM NICs — it prints which VMs still have one.

### Upgrading

```bash
cd kvm-bridge-wifi
git pull
sudo ./install.sh
```

Your configuration and saved state are kept.

Then **start (or restart) that VM**. Inside it, check:

```bash
ip -4 addr        # should show something like 192.168.1.x, not 192.168.122.x
ping -c3 192.168.1.1
curl -sI https://debian.org
```

That's it. To confirm everything is healthy from the host:

```bash
sudo kvm-bridge-wifi status
sudo kvm-bridge-wifi doctor
```

---

## The GUI

This is how you configure kvm-bridge-wifi. Open **kvm-bridge-wifi** from your
application menu and you get every VM with its own **Enable** button:

![the kvm-bridge-wifi window](docs/screenshot.png)

Click **Enable** next to a VM and it:

1. adds the unbridged network card to that VM (persistently),
2. adds it to the bridged list,
3. starts the service.

The button turns into **Disable**. Each VM is independent — enable two and
both are bridged at once. You will be asked for your password once per
change, in a normal dialog.

The status panel shows the service state, the uplink, and every bridged VM
with its tap and the address the guest received. **Check setup** and
**Show log** run `doctor` and `journalctl` for you.

> **Why isn't this a button inside virt-manager?**
> virt-manager has no plugin or extension API — it is a plain GTK
> application, so no project can add a button to its Settings window. This
> window is the closest thing that is actually possible: one click per VM,
> no terminal.

The GUI needs GTK 3 for Python. It is present on virtually every GNOME/KDE
desktop; if it is missing:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0     # Debian/Ubuntu
sudo dnf install python3-gobject gtk3          # Fedora
sudo pacman -S python-gobject gtk3             # Arch
```

---

## Three ways to use it

You do not have to install anything extra for any of these.

| Interface | Command | Needs |
|---|---|---|
| **Window** | `kvm-bridge-wifi-gui` or the menu entry | GTK 3 (usually already there) |
| **Terminal interface** | `kvm-bridge-wifi-tui` | **nothing** — standard library only |
| **Commands** | `sudo kvm-bridge-wifi …` | nothing |

### The terminal interface

If GTK is missing, or you are on a server, or you are working over `ssh`,
use the terminal interface. It looks like this:

```
 kvm-bridge-wifi
 bridged networking for KVM over Wi-Fi

 VIRTUAL MACHINES
 ----------------------------------------------------------------
 > linux2024-2                 ENABLED    running
       vnet1  ->  192.168.1.25
   linux2024                   ENABLED    running
       vnet4  ->  192.168.1.26

 STATUS
 service  running
 uplink   wlp3s0
 bridged  linux2024-2, linux2024

 up/down move   enter toggle   r refresh   d check   l log   q quit
```

Same operations as the window: arrow keys to move, **Enter** to enable or
disable, `d` for the setup check, `l` for the log, `q` to quit.

It uses `curses`, which ships inside `libpython3-stdlib` — the package that
comes with `python3` itself. There is nothing to install, on any
distribution, and it works with no display at all.

### Commands

```bash
sudo kvm-bridge-wifi enable --vm NAME
sudo kvm-bridge-wifi disable --vm NAME
```

### Why you may be asked for a password

Reading libvirt as a normal user is controlled by polkit. The installer adds
you to the `libvirt` group, which the standard libvirt polkit rule allows
without a password. **Log out and back in once** for that to take effect.
Until then, the interfaces fall back to asking for your password.

---

## Why this exists

You tried "Bridge device" in virt-manager, or `macvtap`, and it just… didn't
work. That is not your fault and not a misconfiguration. Here is why.

Wi-Fi frames carry **three** MAC addresses: receiver, transmitter, and one
more. A real Layer-2 bridge needs **four** — it has to tell the access point
"this frame came from a machine behind me." That extra slot only exists in
"WDS" / 4-address mode, which consumer access points essentially never enable.

So when your VM sends a frame with its own MAC, the access point sees a source
MAC that is not the associated station and silently drops it. This is why
`br0 + wlp3s0` fails, and why `macvtap` hangs at DHCP.

**VirtualBox and VMware do not beat this rule either.** They translate:
frames leave the host carrying the *host's* MAC, and replies are rewritten
back to the *guest's* MAC before they reach the VM. VMware documents this as
"MAC address translation." The access point only ever sees one device — your
laptop — while the guest keeps its own IP on your subnet.

`kvm-bridge-wifi` does exactly that, in user space, for KVM.

### What it has to fix along the way

MAC translation alone is not enough. Each of these is a real failure that was
found by testing, and each is a few lines in the source:

| Problem | Symptom | Fix |
|---|---|---|
| ARP payload still names the guest's MAC | Router sends frames the AP drops | Rewrite the sender hardware address inside ARP |
| Same for IPv6 neighbour discovery | IPv6 dies after a few seconds | Rewrite the NDP link-layer option, fix the ICMPv6 checksum |
| DHCP clients leave the broadcast flag clear | Router replies unicast to the guest's MAC; offer is lost; guest times out | Force the broadcast flag on |
| virtio hands over frames with blank TCP/UDP checksums | `ping` works, DNS and TCP hang forever | Verify and repair L4 checksums |
| virtio sends 64 KB super-frames | Large downloads stall | Disable TSO/GSO/GRO on the tap |
| Routers forget ARP entries | Guest becomes unreachable after a few idle minutes | Gratuitous ARP every 60 s, plus proxy ARP replies |

---

## What works

| | |
|---|---|
| Guest IP | from your real router, same subnet as the host |
| Guest → LAN / internet | ✅ |
| LAN → guest | ✅ |
| Guest → host | ✅ |
| Host → guest | ✅ |
| DHCP | ✅ |
| IPv6 | ✅ |
| MAC seen by the access point | the host's MAC, always |

**Verified** on Debian 13 host + Parrot Security 7.4 guest, Wi-Fi only:
real DHCP lease, 0% packet loss to gateway and internet, HTTPS, 5 MB
downloads, and host→guest in both directions.

---

## Requirements

* Linux with KVM and libvirt (`virt-manager` or `virsh`)
* root (`sudo`)
* Python **3.10+**, standard library only — nothing to `pip install`
* A Wi-Fi connection. A wired connection should use a normal bridge instead.

GTK is **optional**. Without it you still get the full terminal interface and
all the commands; the installer simply skips the window.

### Install the prerequisites

Most desktop distributions already have these. If the installer says something
is missing, here is the command for your distribution:

| Distribution | Install command |
|---|---|
| Debian / Ubuntu / Mint / Pop!\_OS | `sudo apt install python3 libvirt-clients libvirt-daemon-system` |
| Fedora / RHEL / Rocky | `sudo dnf install python3 libvirt-client libvirt-daemon` |
| Arch / Manjaro / EndeavourOS | `sudo pacman -S python libvirt` |
| openSUSE | `sudo zypper install python3 libvirt-client libvirt-daemon` |
| Alpine | `sudo apk add python3 libvirt-client libvirt-daemon` |

For the graphical window you also need GTK 3 for Python. This is optional —
the terminal interface covers everything without it.

| Distribution | Install command |
|---|---|
| Debian / Ubuntu | `sudo apt install python3-gi gir1.2-gtk-3.0` |
| Fedora | `sudo dnf install python3-gobject gtk3` |
| Arch | `sudo pacman -S python-gobject gtk3` |
| openSUSE | `sudo zypper install python3-gobject gtk3` |

If GTK is missing, the installer still works. It installs the terminal
interface, and the menu entry opens that instead. Install GTK later and
re-run the installer to switch the menu entry to the window.

---

## How it works, step by step

1. **Enabling a VM** gives it an extra network card of type `ethernet` — an
   unbridged tap that libvirt creates. In virt-manager this shows up as
   **"Generic ethernet connection"**. It goes into the VM's *persistent*
   config, so it survives restarts.
2. **A systemd service** starts `kvm-bridge-wifi`, which waits for each enabled
   VM's tap to appear. VMs that are not running are simply waited for.
3. The bridge **learns** each guest's MAC and IP from the traffic it sees,
   and remembers them in `/var/lib/kvm-bridge-wifi/state.json`.
4. Every frame is rewritten as described above, in both directions.
5. VMs that are **not running** go on a watch list. The service bridges them
   the moment they start, and puts them back on the list if they stop again.
   A shut-down VM never holds up the others — every enabled VM is checked on
   its own.

Nothing is permanently changed about your host's networking. No `br0`, no
`iptables` rules, no changes to `/etc/network/interfaces`.

---

## virt-manager notes

The installer sets the NIC up for you, so normally you never open the
hardware dialog. If you do look at it:

* The correct entry is **Network source → Generic ethernet connection**.
* **Do not** pick "Bridge device" — that needs a real Linux bridge on a
  wired interface and will not work over Wi-Fi.
* You will see **two** NICs if your VM already had a NAT one. That is fine:
  the NAT NIC keeps working for internet access, and the new one gives the VM
  a presence on your LAN.

---

## Commands

```bash
sudo kvm-bridge-wifi install              # install everything (no VM needed)
sudo kvm-bridge-wifi enable --vm NAME     # bridge this VM (adds up to the list)
sudo kvm-bridge-wifi disable --vm NAME    # stop bridging this VM
sudo kvm-bridge-wifi disable              # stop bridging everything
sudo kvm-bridge-wifi status               # what is happening right now
sudo kvm-bridge-wifi doctor               # check for common problems
sudo kvm-bridge-wifi setup-vm NAME        # only add the NIC to a VM
sudo kvm-bridge-wifi run                  # foreground mode (systemd runs this)
sudo kvm-bridge-wifi prune                # forget VMs deleted from libvirt
sudo kvm-bridge-wifi uninstall            # remove the service and menu entry
sudo kvm-bridge-wifi uninstall --purge    # ...and delete the config and saved state
     kvm-bridge-wifi-tui                  # terminal interface, no dependencies
     kvm-bridge-wifi selftest             # offline checks, no root needed
     kvm-bridge-wifi-gui                  # the graphical control panel
```

Useful flags for `install`:

```
--vm NAME            enable this VM straight away (optional)
--uplink IFACE       force the Wi-Fi interface (default: auto-detect)
```

Most people never type any of these — the GUI covers them.

---

## Configuration

`/etc/kvm-bridge-wifi/kvm-bridge-wifi.conf`:

```ini
[kvm-bridge-wifi]
vms = vm-one,vm-two    # comma separated; empty = nothing enabled
uplink = auto          # or wlp3s0, wlan0, ...
host_access = yes
ipv6 = yes
verbose = no
```

You normally never touch this — the GUI writes it. If you do edit it,
apply the change with `sudo systemctl restart kvm-bridge-wifi`.

---

## Troubleshooting

Start here — it catches most problems:

```bash
sudo kvm-bridge-wifi doctor
```

**The guest gets no address / stays at 192.168.122.x**

Run `sudo kvm-bridge-wifi status`. It prints a `state:` line per VM that
tells you exactly which step is stuck:

| state | meaning |
|---|---|
| `connected` | working |
| `stopped` | VM is powered off — start it, the bridge picks it up automatically |
| `starting` | VM is booting; the tap appears shortly |
| `no-nic` | the VM has no bridged NIC yet — `sudo kvm-bridge-wifi setup-vm NAME` |
| `deleted` | the VM no longer exists — `sudo kvm-bridge-wifi prune` |
| `guest-nic-down` | **the guest never configured its second NIC** |
| `no-lease` | traffic flows but no DHCP — check AP client isolation |

**`guest-nic-down` is by far the most common.** The bridge is working
perfectly; the guest simply never brought the new network card up. Most cloud
images (Ubuntu, Debian cloud, Fedora cloud) only configure their **first**
interface, so the card you added sits there unused.

Fix it **inside the guest**:

```bash
# quick test — not persistent
sudo ip link set enp7s0 up
sudo dhclient enp7s0

# Ubuntu / Debian cloud images (persistent)
sudo tee /etc/netplan/60-kvm-bridge.yaml >/dev/null <<'EOF'
network:
  version: 2
  ethernets:
    enp7s0:
      dhcp4: true
      optional: true
EOF
sudo chmod 600 /etc/netplan/60-kvm-bridge.yaml
sudo netplan apply
```

Replace `enp7s0` with the guest's second interface name — `ip -br a` inside
the guest shows it (it is usually the one with no address).

> netplan silently ignores files that other users can read, so the
> `chmod 600` is required, not optional.

On a NetworkManager guest instead:

```bash
sudo nmcli connection add type ethernet ifname enp7s0 con-name lan ipv4.method auto
sudo nmcli connection up lan
```

**Other things to check**

* Is the VM actually enabled? The button in the window should read
  **Disable**. If it says **Enable**, click it.
* Does your access point have **client isolation** / "AP isolation" on?
  Then the guest can reach the router but not other LAN devices. VirtualBox
  has the same limitation. Turn it off in the router settings.

**`ping` works but DNS and web browsing hang**

Checksum offload. The bridge repairs this automatically — check the log for
`checksums fixed`. If you are running an old copy, update.

**Large downloads stall**

TSO super-frames. The bridge disables them at startup; look for
`disabled TSO/GSO/GRO on <tap>`. If it is missing, do it inside the guest:

```bash
sudo ethtool -K eth0 tso off gso off gro off
```

**Packets arrive twice / about 50% loss**

Two copies of the bridge are running. A lock file prevents this; check with
`pgrep -af kvm-bridge-wifi`. Stop the extra one.

**The window shows an old address as well as the current one**

Older versions accumulated every address they had ever seen. A DHCP lease is
now treated as authoritative and replaces the previous one, so this should
not happen any more. If you upgraded and still see two addresses, restart the
service once: `sudo systemctl restart kvm-bridge-wifi`.

**The guest lost its IP after I activated another NIC**

NetworkManager's single auto-generated profile moved to the other interface.
Give each NIC its own profile:

```bash
nmcli connection add type ethernet ifname enp7s0 con-name lan-bridge ipv4.method auto
nmcli connection up lan-bridge
```

**It worked, then stopped after a while**

The router's ARP entry expired. The bridge re-announces every 60 seconds, so
this should not happen — if it does, please open an issue with
`sudo kvm-bridge-wifi status` output.

**Nothing in the log**

`journalctl -u kvm-bridge-wifi -f`. It only logs when it learns something, so
generate some traffic in the guest.

---

## Limitations

* **The LAN sees your host's MAC, not the guest's.** That is the whole trick.
  Anything doing MAC-based policy — port security, MAC filtering, captive
  portals — treats the guest as your laptop. VirtualBox and VMware behave the
  same way.
* **AP/client isolation** blocks guest ↔ other-LAN-device traffic.
* **Throughput** is bounded by the user-space copy path: expect a few hundred
  KB/s to a few MB/s. Fine for browsing and SSH; not for bulk transfer.
* **Not a security boundary.** The guest shares the host's Layer-2 identity.
  It runs as root and parses frames from the network, so treat it like any
  other privileged network daemon.
* **Wi-Fi only.** If you have a cable, use a real bridge — it is strictly
  better.
* **Linux only.** It relies on `AF_PACKET` raw sockets.

## What has actually been tested

Being precise about this matters, because Wi-Fi behaves differently on every
card, driver and access point.

**Verified working:**

| | |
|---|---|
| Host | Debian 13, kernel 6.12 |
| Wi-Fi | Realtek `rtw89_8852be` (`wlp3s0`), WPA2 home router |
| Guest | Parrot Security 7.4, virtio NIC |
| Result | DHCP lease, LAN + internet, DNS, HTTPS, 5 MB download, host→guest |
| Multiple VMs | **two VMs bridged at once**, each with its own LAN address and route |

**Not yet verified:** other Wi-Fi drivers, enterprise/captive networks,
IPv6-only networks, non-virtio NIC models, other distributions.

The install and dependency logic knows about apt, dnf, pacman, zypper and apk,
but it has only been *run* on Debian. On other distributions the checks should
still produce the right instructions; if they do not, that is worth an issue.

Some access points actively filter frames with unexpected source MACs. On
those, nothing in userspace can help — the same applies to VirtualBox's
bridged mode. If it fails for you, that is a useful report, not a mistake on
your part.

### If it does not work for you

Please open an issue with:

```bash
uname -r
lspci -nnk | grep -A3 -i net          # card and driver
sudo kvm-bridge-wifi doctor
sudo kvm-bridge-wifi status
journalctl -u kvm-bridge-wifi -n 50 --no-pager
ip -br addr                           # inside the guest
```

That is enough to tell whether it is a driver, an AP, or a bug here.

---

## Alternatives

* **Plug in an Ethernet cable.** A real `br0` is better in every way. This
  project exists for people who cannot.
* **`parprouted` + `dhcp-helper`** — the packaged Debian approach. Proxy ARP
  plus a routed setup. More moving parts, needs two addresses on the same
  subnet and a DHCP relay.
* **Tailscale subnet router** — great if you only need to reach the VM from
  your own devices, not from the LAN.

---

## Project layout

```
kvm_bridge_wifi.py          the daemon and CLI (single file, no dependencies)
kvm_bridge_wifi_gui.py      GTK control panel (optional, needs python3-gi)
kvm_bridge_wifi_tui.py      terminal control panel (standard library only)
kvm-bridge-wifi.desktop     application-menu entry
install.sh             installer wrapper
kvm-bridge-wifi.conf.example
tests/                 pytest suite
.github/workflows/     CI
```

## Contributing

Bug reports welcome — especially with your Wi-Fi card, driver and AP model.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
