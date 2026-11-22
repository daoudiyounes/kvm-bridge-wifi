# Contributing

Thanks for taking a look. Bug reports are as welcome as patches — especially
reports that say *which* Wi-Fi card, driver and AP you have, because that is
where this approach succeeds or fails.

## Before you start

Read the "Why this exists" section of the README. The short version: a real
802.11 bridge needs four MAC addresses, consumer APs use three, so everything
here is MAC translation. Changes that assume true Layer-2 bridging will not
work and will be declined.

## Development setup

No dependencies beyond Python 3.10+ and the standard library.

```bash
git clone https://github.com/daoudiyounes/kvm-bridge-wifi
cd kvm-bridge-wifi
python3 kvm_bridge_wifi.py selftest     # offline, no root needed
python3 -m pytest -q               # same checks plus unit tests
```

## Testing on a real VM

The offline tests cover the frame rewriting but cannot cover the interesting
parts. For an end-to-end run you need a libvirt host with Wi-Fi:

```bash
sudo ./install.sh          # then press Enable in the window
sudo kvm-bridge-wifi status
sudo kvm-bridge-wifi doctor
```

Then, inside the guest:

```bash
ip -4 addr            # a real LAN address, not 192.168.122.x
ping -c3 <your-router>
curl -sI https://debian.org
```

## What to include in a bug report

* host distro and kernel version (`uname -r`)
* Wi-Fi card and driver (`lspci -nnk | grep -A3 -i net`)
* `sudo kvm-bridge-wifi doctor`
* `sudo kvm-bridge-wifi status`
* the last 50 lines of `journalctl -u kvm-bridge-wifi`
* guest distro, and the output of `ip -br addr` inside it

## Style

* Standard library only. The whole point is that it installs with no
  dependencies.
* Keep it in one file — `kvm_bridge_wifi.py` should stay readable top to bottom.
* Comment the *why*, not the *what*. Several behaviours here exist because of
  specific, non-obvious failure modes; those reasons are worth keeping.
* Run `python3 kvm_bridge_wifi.py selftest` before opening a pull request.

## Commit messages

Plain, imperative, one line: `fix DHCP broadcast flag for busybox clients`.
