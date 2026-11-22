#!/usr/bin/env python3
"""
kvm-bridge-wifi-gui -- a small GTK control panel for kvm_bridge_wifi.

virt-manager has no plugin API, so a button cannot be added to its own
Settings window.  This is the next best thing: a normal desktop application
that lists your VMs and gives each one an Enable/Disable button.

Run it from the application menu ("kvm-bridge-wifi") or:

    kvm-bridge-wifi-gui

Privileges: the window itself runs as your normal user and only asks for a
password when it actually needs to change something.  That is done with
`sudo -A` and a GTK askpass helper defined below, so there is no separate
password prompt in a terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading

# GTK is imported lazily so the helper functions below stay importable (and
# testable) on machines and CI runners without a display stack.
GTK_ERROR = None
try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError) as exc:  # pragma: no cover - GUI only
    GLib = Gtk = None  # type: ignore[assignment]
    GTK_ERROR = exc


GTK_HINT = (
    "kvm-bridge-wifi needs GTK 3 for Python.\n"
    "  Debian/Ubuntu:  sudo apt install python3-gi gir1.2-gtk-3.0\n"
    "  Fedora:         sudo dnf install python3-gobject gtk3\n"
    "  Arch:           sudo pacman -S python-gobject gtk3\n"
    "  openSUSE:       sudo zypper install python3-gobject gtk3"
)


def show_error_without_gtk(message: str) -> None:
    """Tell the user what is wrong even though we have no GTK to draw with.

    A launcher that exits silently is the worst outcome, so try the common
    desktop dialogs, then fall back to the terminal.
    """
    for tool, args in (
        ("zenity", ["--error", "--text", message]),
        ("kdialog", ["--error", message]),
        ("xmessage", ["-center", message]),
    ):
        path = shutil.which(tool)
        if not path:
            continue
        try:
            result = subprocess.run(
                [path, *args],
                timeout=120,
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return
        # A tool that is installed but cannot draw (wrong display, missing
        # Qt plugins, ...) should not stop us trying the next one.
    sys.stderr.write(message + "\n")


def require_gtk() -> None:
    if GTK_ERROR is None:
        return
    show_error_without_gtk(f"{GTK_HINT}\n\n({GTK_ERROR})")
    raise SystemExit(1)

KVM_BRIDGE_WIFI = shutil.which("kvm-bridge-wifi") or "/usr/local/bin/kvm-bridge-wifi"
CONFIG_PATH = "/etc/kvm-bridge-wifi/kvm-bridge-wifi.conf"
STATE_PATH = "/var/lib/kvm-bridge-wifi/state.json"
UNIT_NAME = "kvm-bridge-wifi.service"

APP_TITLE = "kvm-bridge-wifi"


# --------------------------------------------------------------------------
# privilege handling
# --------------------------------------------------------------------------


def _askpass_helper() -> str:
    """A tiny script sudo can call to pop up a password dialog."""
    path = os.path.join(tempfile.gettempdir(), f"kvm-bridge-wifi-askpass-{os.getuid()}")
    script = (
        "#!/bin/sh\n"
        f"exec {sys.executable} {os.path.abspath(__file__)} --askpass\n"
    )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() == script:
                return path
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(script)
    os.chmod(path, 0o700)
    return path


def root_command(args: list[str]) -> tuple[list[str], dict]:
    """Prefix a command with sudo when we are not already root.

    sudo is the primary path because it behaves the same everywhere and lets
    us supply our own password dialog.  pkexec is only a fallback for systems
    that have no sudo at all.
    """
    if os.geteuid() == 0:
        return args, dict(os.environ)

    if shutil.which("sudo"):
        env = dict(os.environ)
        env["SUDO_ASKPASS"] = _askpass_helper()
        return ["sudo", "-A", *args], env

    if shutil.which("pkexec"):
        return ["pkexec", *args], dict(os.environ)

    raise RuntimeError(
        "neither sudo nor pkexec is available; run kvm-bridge-wifi from a terminal"
    )


def run_root(args: list[str], timeout: int = 120):
    command, env = root_command(args)
    return subprocess.run(
        command, capture_output=True, text=True, env=env, timeout=timeout
    )


def askpass_dialog() -> int:
    """The --askpass entry point: show a dialog, print the password."""
    require_gtk()
    dialog = Gtk.Dialog(title=APP_TITLE)
    dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    dialog.set_border_width(12)

    box = dialog.get_content_area()
    box.set_spacing(8)
    label = Gtk.Label(label="kvm-bridge-wifi needs administrator rights to change the network.")
    label.set_line_wrap(True)
    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_placeholder_text("Password")
    entry.set_activates_default(True)
    box.add(label)
    box.add(entry)

    dialog.show_all()
    response = dialog.run()
    text = entry.get_text()
    dialog.destroy()

    if response != Gtk.ResponseType.OK:
        return 1
    print(text, flush=True)
    return 0


# --------------------------------------------------------------------------
# read-only queries (no root needed)
# --------------------------------------------------------------------------


def parse_vm_list(text: str) -> list[tuple[str, str]]:
    """Turn `virsh list --all` output into [(name, state)].

    Kept separate from the subprocess call so it can be unit-tested without
    libvirt or a display.
    """
    vms = []
    for line in text.splitlines():
        parts = line.split()
        # The separator line is one run of dashes, so it fails the length
        # test.  Do NOT filter on parts[0] == "-": that is the Id column of
        # every shut-off domain, and filtering it hides half your VMs.
        if len(parts) < 3 or parts[0] == "Id":
            continue
        name, state = parts[1], " ".join(parts[2:])
        vms.append((name, state))
    return vms


def list_vms() -> list[tuple[str, str]]:
    """[(name, state)] from libvirt, using a read-only connection."""
    try:
        result = subprocess.run(
            ["virsh", "-r", "-c", "qemu:///system", "list", "--all"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_vm_list(result.stdout)


def read_config() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            section = False
            for line in fh:
                line = line.strip()
                if line.startswith("["):
                    section = line.lower() == "[kvm-bridge-wifi]"
                    continue
                if section and "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def read_state() -> dict:
    import json

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        vms = data.get("vms")
        return vms if isinstance(vms, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled_vms(cfg: dict[str, str] | None = None) -> list[str]:
    """The configured VM list, in order."""
    cfg = read_config() if cfg is None else cfg
    raw = cfg.get("vms", "") or cfg.get("vm", "")
    return [item for item in raw.replace(",", " ").split() if item]


def tap_for(vm: str) -> str:
    """The tap of a VM's unbridged NIC, or '-'."""
    try:
        result = subprocess.run(
            ["virsh", "-r", "-c", "qemu:///system", "domiflist", vm],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "-"
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "ethernet":
            return parts[0]
    return "-"


def service_active() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", UNIT_NAME],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------


class Window(Gtk.Window if Gtk is not None else object):
    def __init__(self) -> None:
        require_gtk()
        super().__init__(title=APP_TITLE)
        self.set_default_size(760, 560)
        self.set_border_width(12)

        self.rows: dict[str, dict] = {}
        self.busy = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(outer)

        outer.pack_start(self._build_header(), False, False, 0)
        outer.pack_start(self._build_vm_list(), True, True, 0)
        outer.pack_start(self._build_status(), False, False, 0)
        outer.pack_start(self._build_footer(), False, False, 0)

        self.refresh()
        GLib.timeout_add_seconds(3, self._tick)

    # -- layout ----------------------------------------------------------

    def _build_header(self) -> Gtk.Widget:
        bar = Gtk.HeaderBar()
        bar.set_show_close_button(True)
        bar.set_title(APP_TITLE)
        bar.set_subtitle("Bridged networking for KVM over Wi-Fi")
        self.set_titlebar(bar)

        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        bar.pack_end(refresh)
        return Gtk.Box()

    def _build_vm_list(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="Virtual machines")
        frame.set_margin_top(6)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(220)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.add(self.listbox)

        frame.add(scroll)
        return frame

    def _build_status(self) -> Gtk.Widget:
        frame = Gtk.Frame(label="Status")
        grid = Gtk.Grid()
        grid.set_border_width(12)
        grid.set_row_spacing(6)
        grid.set_column_spacing(18)

        self.status_labels: dict[str, Gtk.Label] = {}
        rows = [("service", "Service"), ("uplink", "Uplink")]
        for index, (key, title) in enumerate(rows):
            caption = Gtk.Label()
            caption.set_markup(f"<b>{title}</b>")
            caption.set_xalign(0)
            value = Gtk.Label(label="-")
            value.set_xalign(0)
            value.set_selectable(True)
            grid.attach(caption, 0, index, 1, 1)
            grid.attach(value, 1, index, 1, 1)
            self.status_labels[key] = value

        caption = Gtk.Label()
        caption.set_markup("<b>Bridged</b>")
        caption.set_xalign(0)
        caption.set_valign(Gtk.Align.START)
        self.bridged_label = Gtk.Label(label="none")
        self.bridged_label.set_xalign(0)
        self.bridged_label.set_selectable(True)
        self.bridged_label.set_line_wrap(True)
        grid.attach(caption, 0, len(rows), 1, 1)
        grid.attach(self.bridged_label, 1, len(rows), 1, 1)

        frame.add(grid)
        return frame

    def _build_footer(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.message = Gtk.Label(label="")
        self.message.set_xalign(0)
        self.message.set_line_wrap(True)
        box.pack_start(self.message, True, True, 0)

        doctor = Gtk.Button(label="Check setup")
        doctor.connect("clicked", lambda *_: self.show_doctor())
        box.pack_start(doctor, False, False, 0)

        log = Gtk.Button(label="Show log")
        log.connect("clicked", lambda *_: self.show_log())
        box.pack_start(log, False, False, 0)

        return box

    # -- data ------------------------------------------------------------

    def _tick(self) -> bool:
        # Only refresh the cheap parts on a timer; rebuilding the VM list
        # every few seconds would fight with the user's clicks.
        self._update_status()
        return True

    def refresh(self) -> None:
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self.rows.clear()

        vms = list_vms()
        if not vms:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label="No virtual machines found.")
            label.set_margin_top(12)
            label.set_margin_bottom(12)
            row.add(label)
            self.listbox.add(row)
        else:
            for name, state in vms:
                self.listbox.add(self._make_row(name, state))

        self.listbox.show_all()
        self._update_status()

    def _make_row(self, name: str, state: str) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_border_width(10)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label()
        title.set_markup(f"<b>{GLib.markup_escape_text(name)}</b>")
        title.set_xalign(0)
        subtitle = Gtk.Label(label=state)
        subtitle.set_xalign(0)
        subtitle.get_style_context().add_class("dim-label")
        text.pack_start(title, False, False, 0)
        text.pack_start(subtitle, False, False, 0)

        button = Gtk.Button()
        button.set_size_request(110, -1)

        box.pack_start(text, True, True, 0)
        box.pack_start(button, False, False, 0)
        row.add(box)

        self.rows[name] = {"button": button, "state": state}
        button.connect("clicked", lambda _b, n=name: self.toggle(n))
        return row

    def _update_status(self) -> None:
        cfg = read_config()
        active = service_active()
        enabled = enabled_vms(cfg)

        for name, widgets in self.rows.items():
            button = widgets["button"]
            if name in enabled:
                button.set_label("Disable")
                button.get_style_context().remove_class("suggested-action")
                button.get_style_context().add_class("destructive-action")
            else:
                button.set_label("Enable")
                button.get_style_context().remove_class("destructive-action")
                button.get_style_context().add_class("suggested-action")

        self.status_labels["service"].set_text("running" if active else "stopped")
        self.status_labels["uplink"].set_text(cfg.get("uplink", "-") or "-")

        state = read_state()
        lines = []
        for vm in enabled:
            tap = tap_for(vm)
            ips = ", ".join((state.get(vm) or {}).get("guest_ips") or [])
            if tap == "-":
                detail = "waiting for the VM to start"
            elif ips:
                detail = f"{tap} → {ips}"
            else:
                detail = f"{tap} → no address yet"
            lines.append(f"{vm}  ({detail})")
        self.bridged_label.set_text("\n".join(lines) if lines else "none")

    # -- actions ---------------------------------------------------------

    def toggle(self, name: str) -> None:
        """Turn bridging on or off for one VM, leaving the others alone."""
        if self.busy:
            return

        if name in enabled_vms():
            self._run(f"Disabling {name}", [KVM_BRIDGE_WIFI, "disable", "--vm", name])
        else:
            self._run(f"Enabling {name}", [KVM_BRIDGE_WIFI, "enable", "--vm", name])

    def _run(self, description: str, args: list[str]) -> None:
        self.busy = True
        self.message.set_text(f"{description}...")

        def worker() -> None:
            try:
                result = run_root(args)
                if result.returncode == 0:
                    GLib.idle_add(self._done, description + " — done", True)
                else:
                    detail = (result.stderr or result.stdout or "").strip()
                    GLib.idle_add(self._done, f"Failed: {detail}", False)
            except subprocess.TimeoutExpired:
                GLib.idle_add(self._done, "Timed out", False)
            except Exception as exc:  # noqa: BLE001 - show it to the user
                GLib.idle_add(self._done, f"Failed: {exc}", False)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, text: str, ok: bool) -> bool:
        self.busy = False
        self.message.set_text(text)
        self.refresh()
        return False

    def show_log(self) -> None:
        try:
            result = run_root(
                ["journalctl", "-u", UNIT_NAME, "-n", "120", "--no-pager"]
            )
            text = result.stdout or result.stderr
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
        self._text_dialog("kvm-bridge-wifi log", text)

    def show_doctor(self) -> None:
        try:
            result = run_root([KVM_BRIDGE_WIFI, "doctor"])
            text = (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
        self._text_dialog("kvm-bridge-wifi check", text)

    def _text_dialog(self, title: str, text: str) -> None:
        dialog = Gtk.Dialog(title=title, transient_for=self)
        dialog.set_default_size(680, 460)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_monospace(True)
        view.get_buffer().set_text(text or "(empty)")
        scroll.add(view)
        dialog.get_content_area().add(scroll)

        dialog.show_all()
        dialog.run()
        dialog.destroy()


def main(argv: list[str]) -> int:
    require_gtk()
    if "--askpass" in argv:
        return askpass_dialog()

    try:
        window = Window()
        window.connect("destroy", Gtk.main_quit)
        window.show_all()
        Gtk.main()
    except RuntimeError as exc:
        # GTK is installed but cannot open a display -- typically because the
        # command was run over ssh or from a service rather than a desktop
        # session.  Say so instead of dumping a traceback at the user.
        show_error_without_gtk(
            "kvm-bridge-wifi could not open a window on this display.\n\n"
            f"{exc}\n\n"
            "If you are on a desktop, make sure DISPLAY is set.  Otherwise "
            "use the command line:\n\n"
            "  sudo kvm-bridge-wifi status\n"
            "  sudo kvm-bridge-wifi enable --vm NAME"
        )
        return 1
    return 0


def cli() -> None:
    """Console entry point (used by pip and by the installed symlink)."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
