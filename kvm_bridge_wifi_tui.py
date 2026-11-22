#!/usr/bin/env python3
"""
kvm-bridge-wifi-tui -- a terminal control panel for kvm_bridge_wifi.

Why this exists
---------------
The GTK window is pleasant, but GTK 3 for Python is not installed everywhere,
and it is useless on a server with no desktop or over ssh.  This uses only
Python's standard library (`curses`), which ships inside libpython3-stdlib --
the package you already have if you have python3 at all.

So: any Linux with Python can run a full interface, with nothing to install.

Keys
----
  up/down, j/k   move
  enter, space   enable or disable the selected VM
  r              refresh
  d              run the setup check (doctor)
  l              show the log
  q              quit
"""

from __future__ import annotations

import curses
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import kvm_bridge_wifi as wb
except ImportError:  # running from a directory without the module
    wb = None


KVM_BRIDGE_WIFI = "kvm-bridge-wifi"


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


def enabled_vms() -> list[str]:
    if wb is None:
        return []
    return wb.parse_vms(wb.load_config()["vms"])


def parse_virsh_list(text: str) -> list[tuple[str, str]]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] == "Id":
            continue
        out.append((parts[1], " ".join(parts[2:])))
    return out


# Reading libvirt as a normal user depends on polkit rules, and on many
# systems it is simply denied.  The list is fetched once (asking for a
# password only if necessary) and then reused, so the interface does not
# prompt on every redraw.
_VM_CACHE: list[tuple[str, str]] = []
_NEEDS_ROOT = False


def fetch_vms(stdscr=None) -> list[tuple[str, str]]:
    """Read the VM list, falling back to sudo if polkit denies us."""
    global _VM_CACHE, _NEEDS_ROOT

    command = ["virsh", "-c", "qemu:///system", "list", "--all"]
    if not _NEEDS_ROOT:
        try:
            result = subprocess.run(
                ["virsh", "-r", "-c", "qemu:///system", "list", "--all"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                _VM_CACHE = parse_virsh_list(result.stdout)
                return _VM_CACHE
        except (OSError, subprocess.SubprocessError):
            pass
        _NEEDS_ROOT = True

    if stdscr is not None:
        code, out = run_privileged(stdscr, command)
    else:
        try:
            result = subprocess.run(
                ["sudo", *command] if os.geteuid() != 0 else command,
                capture_output=True, text=True, timeout=60,
            )
            code, out = result.returncode, result.stdout
        except (OSError, subprocess.SubprocessError):
            code, out = 1, ""

    if code == 0:
        _VM_CACHE = parse_virsh_list(out)
    return _VM_CACHE


def vms() -> list[tuple[str, str]]:
    """The cached list; refresh() re-reads it."""
    return _VM_CACHE


def state_entries() -> dict[str, dict]:
    """Per-VM state.  The file is world-readable, so no privileges needed."""
    if wb is None:
        return {}
    raw = wb.load_state()
    entries = raw.get("vms")
    return entries if isinstance(entries, dict) else {}


def guest_ips() -> dict[str, list[str]]:
    return {vm: (entry.get("guest_ips") or []) for vm, entry in state_entries().items()}


def tap_of(vm: str) -> str:
    """Live tap if we can see it, otherwise the last one we recorded."""
    if wb is not None:
        live = wb.tap_for_vm(vm)
        if live:
            return live
    return (state_entries().get(vm) or {}).get("tap") or "-"


def service_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "kvm-bridge-wifi"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def uplink() -> str:
    if wb is None:
        return "-"
    return wb.load_config()["uplink"] or "-"


# --------------------------------------------------------------------------
# running privileged commands
# --------------------------------------------------------------------------


def run_privileged(stdscr, args: list[str]) -> tuple[int, str]:
    """
    Run a command as root, giving the terminal back to it first.

    Leaving curses mode is what makes this universal: sudo can then prompt for
    a password normally, and password managers, ssh agents and PAM all behave
    exactly as the user expects.
    """
    curses.def_prog_mode()
    curses.endwin()
    try:
        result = subprocess.run(
            ["sudo", *args] if os.geteuid() != 0 else list(args),
            capture_output=True, text=True,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    finally:
        curses.reset_prog_mode()
        stdscr.refresh()


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------


def addstr_safe(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    stdscr.addstr(y, x, text[: max(0, width - x - 1)], attr)


def draw(stdscr, selected: int, message: str) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    has_colour = curses.has_colors()
    if has_colour:
        curses.start_color()
        try:
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_CYAN, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
        except curses.error:
            has_colour = False

    def colour(pair: int) -> int:
        return curses.color_pair(pair) if has_colour else 0

    title = " kvm-bridge-wifi "
    addstr_safe(stdscr, 0, 0, title.center(min(width - 1, 60)), curses.A_BOLD | colour(3))
    addstr_safe(stdscr, 1, 0, " bridged networking for KVM over Wi-Fi", curses.A_DIM)

    enabled = enabled_vms()
    active = service_active()
    ips = guest_ips()

    row = 3
    addstr_safe(stdscr, row, 1, "VIRTUAL MACHINES", curses.A_BOLD)
    row += 1
    addstr_safe(stdscr, row, 1, "-" * min(width - 2, 70), curses.A_DIM)
    row += 1

    machines = vms()
    if not machines:
        addstr_safe(stdscr, row, 2, "No virtual machines found.", colour(2))
        row += 2

    # One formatted line per VM, so narrow terminals degrade gracefully
    # instead of columns colliding.
    name_width = max(10, min(28, width - 34))

    for index, (name, state) in enumerate(machines):
        is_enabled = name in enabled
        marker = ">" if index == selected else " "
        status = "ENABLED" if is_enabled else "disabled"

        label = f"{marker} {name}"[:name_width].ljust(name_width)
        rest = f"  {status:<9}  {state}"
        # Fill the row so the highlight looks like a proper selection bar.
        line = (label + rest).ljust(max(0, min(width - 3, 78)))

        if index == selected:
            addstr_safe(stdscr, row, 1, line, curses.A_REVERSE)
        else:
            addstr_safe(stdscr, row, 1, line)
            if is_enabled:
                # repaint just the word ENABLED in colour
                addstr_safe(
                    stdscr, row, 1 + name_width + 2, "ENABLED",
                    colour(1) | curses.A_BOLD,
                )

        if is_enabled:
            row += 1
            tap = tap_of(name)
            address = ", ".join(ips.get(name) or []) or "waiting for the VM to start"
            addstr_safe(stdscr, row, 4, f"{tap}  ->  {address}", curses.A_DIM)
        row += 1

    # status block
    row = max(row, height - 9)
    addstr_safe(stdscr, row, 1, "STATUS", curses.A_BOLD)
    row += 1
    addstr_safe(stdscr, row, 1, f"service  {'running' if active else 'stopped'}",
                colour(1) if active else colour(2))
    row += 1
    addstr_safe(stdscr, row, 1, f"uplink   {uplink()}")
    row += 1
    addstr_safe(stdscr, row, 1, f"bridged  {', '.join(enabled) if enabled else 'none'}")

    # message line
    if message:
        wrapped = textwrap.wrap(message, width=max(20, width - 4))[:2]
        for line in wrapped:
            row += 1
            if row < height - 1:
                addstr_safe(stdscr, row, 1, line, colour(4))

    addstr_safe(
        stdscr, height - 1, 0,
        " up/down move   enter toggle   r refresh   d check   l log   q quit "[: width - 1],
        curses.A_REVERSE,
    )
    stdscr.refresh()


def show_text(stdscr, title: str, text: str) -> None:
    """Scrollable-ish text view; any key returns."""
    curses.def_prog_mode()
    curses.endwin()
    print(f"\n=== {title} ===\n")
    print(text or "(empty)")
    print("\nPress Enter to return.")
    try:
        input()
    finally:
        curses.reset_prog_mode()
        stdscr.refresh()


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


def run(stdscr) -> int:
    curses.curs_set(0)
    stdscr.keypad(True)

    selected = 0
    message = ""

    # First read happens here so a password prompt (if polkit requires one)
    # appears once, before the full-screen interface is drawn.
    fetch_vms(stdscr)

    while True:
        machines = vms()
        if selected >= len(machines):
            selected = max(0, len(machines) - 1)
        draw(stdscr, selected, message)
        message = ""

        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return 0

        if key in (ord("q"), 27):
            return 0
        if key in (curses.KEY_UP, ord("k")) and selected > 0:
            selected -= 1
        elif key in (curses.KEY_DOWN, ord("j")) and selected < len(machines) - 1:
            selected += 1
        elif key == ord("r"):
            fetch_vms(stdscr)
            message = "refreshed"
        elif key == ord("d"):
            code, out = run_privileged(stdscr, [KVM_BRIDGE_WIFI, "doctor"])
            show_text(stdscr, "check", out)
        elif key == ord("l"):
            code, out = run_privileged(
                stdscr, ["journalctl", "-u", "kvm-bridge-wifi", "-n", "80", "--no-pager"]
            )
            show_text(stdscr, "log", out)
        elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            if not machines:
                continue
            name = machines[selected][0]
            if name in enabled_vms():
                code, out = run_privileged(
                    stdscr, [KVM_BRIDGE_WIFI, "disable", "--vm", name]
                )
                message = f"disabled {name}" if code == 0 else out.strip()[:200]
            else:
                code, out = run_privileged(
                    stdscr, [KVM_BRIDGE_WIFI, "enable", "--vm", name]
                )
                message = f"enabled {name}" if code == 0 else out.strip()[:200]


def main(argv: list[str]) -> int:
    if not sys.stdout.isatty():
        sys.stderr.write(
            "kvm-bridge-wifi-tui needs a terminal.\n"
            "Use the command line instead:  sudo kvm-bridge-wifi status\n"
        )
        return 1
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    return 0


def cli() -> None:
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
