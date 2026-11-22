#!/usr/bin/env bash
#
# kvm-bridge-wifi installer
#
#   sudo ./install.sh
#
# No VM name needed: this registers the program in your PATH and application
# menu, then opens the window where you pick which VMs to bridge.
#
# All the real work happens in kvm_bridge_wifi.py so it can be tested and read in
# one place.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM=""
UPLINK=""
NO_GUI=0

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

Installs kvm-bridge-wifi, adds it to your application menu, and opens the
window where you choose which VMs to bridge.

  --vm NAME        enable this VM immediately (optional; you can also
                   pick it in the window afterwards)
  --uplink IFACE   Wi-Fi interface (default: auto-detect)
  --no-gui         do not open the window after installing
  -h, --help       show this help

Your VMs:  sudo virsh list --all
EOF
}

# --- tell the user exactly what to install, on their distribution -----------
install_hint() {
  local family="" ids=""
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    ids="$( . /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}" )"
  fi
  case "$ids" in
    *debian*|*ubuntu*) family="debian" ;;
    *fedora*|*rhel*|*centos*) family="fedora" ;;
    *arch*|*manjaro*) family="arch" ;;
    *suse*) family="suse" ;;
    *alpine*) family="alpine" ;;
  esac

  case "$family" in
    debian) echo "  sudo apt install python3 libvirt-clients libvirt-daemon-system" ;;
    fedora) echo "  sudo dnf install python3 libvirt-client libvirt-daemon" ;;
    arch)   echo "  sudo pacman -S python libvirt" ;;
    suse)   echo "  sudo zypper install python3 libvirt-client libvirt-daemon" ;;
    alpine) echo "  sudo apk add python3 libvirt-client libvirt-daemon" ;;
    *)      echo "  install python3, libvirt and the virsh client with your package manager" ;;
  esac
}

die() {
  echo "error: $1" >&2
  echo >&2
  echo "Install what is missing, then run this script again:" >&2
  install_hint >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --vm)      VM="${2:-}"; shift 2 ;;
    --uplink)  UPLINK="${2:-}"; shift 2 ;;
    --no-gui)  NO_GUI=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "error: this needs root.  Try:  sudo ./install.sh" >&2
  exit 1
fi

# --- pre-flight ------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "error: Python 3.10 or newer is required, found $(python3 -V 2>&1)" >&2
  echo >&2
  install_hint >&2
  exit 1
fi

command -v virsh >/dev/null 2>&1 || die "virsh is not installed (it comes with libvirt)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required to run the background service"

# GTK is only needed for the window.  Missing GTK is not fatal -- the command
# line still works -- but say so now rather than letting the menu item fail.
if ! python3 -c 'import gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk' 2>/dev/null; then
  echo "note: the graphical window needs GTK 3 for Python."
  case "$( . /etc/os-release 2>/dev/null; echo "${ID:-}" )" in
    ubuntu|debian|linuxmint|pop) echo "      sudo apt install python3-gi gir1.2-gtk-3.0" ;;
    fedora|rhel|centos)          echo "      sudo dnf install python3-gobject gtk3" ;;
    arch|manjaro)                echo "      sudo pacman -S python-gobject gtk3" ;;
    *suse*)                      echo "      sudo zypper install python3-gobject gtk3" ;;
    *)                           echo "      install python3-gobject / python3-gi and GTK 3" ;;
  esac
  echo "      Installing without the window for now."
  NO_GUI=1
fi

echo "==> installing kvm-bridge-wifi"
INSTALL_ARGS=(install)
[ -n "$VM" ] && INSTALL_ARGS+=(--vm "$VM")
[ -n "$UPLINK" ] && INSTALL_ARGS+=(--uplink "$UPLINK")
python3 "$HERE/kvm_bridge_wifi.py" "${INSTALL_ARGS[@]}"

# ---------------------------------------------------------------------------
# Hand the window back to the user who ran sudo, not to root.
# ---------------------------------------------------------------------------
launch_gui() {
  local target="${SUDO_USER:-}"
  [ "$NO_GUI" -eq 1 ] && return 0
  [ -z "$target" ] && return 0
  [ ! -x /usr/local/bin/kvm-bridge-wifi-gui ] && return 0

  # Reuse the caller's session so the window appears on their desktop.
  local display="${DISPLAY:-}"
  if [ -z "$display" ]; then
    for candidate in :0 :1 :2; do
      if [ -S "/tmp/.X11-unix/X${candidate#:}" ]; then display="$candidate"; break; fi
    done
  fi

  echo "==> opening the kvm-bridge-wifi window"
  setsid sudo -u "$target" \
    env DISPLAY="${display:-:0}" \
        XDG_RUNTIME_DIR="/run/user/$(id -u "$target")" \
        XDG_CURRENT_DESKTOP="${XDG_CURRENT_DESKTOP:-}" \
    /usr/local/bin/kvm-bridge-wifi-gui >/dev/null 2>&1 &
}

launch_gui

cat <<EOF

Done.

  kvm-bridge-wifi                 # the window is in your application menu
  sudo kvm-bridge-wifi status     # what is happening
  sudo kvm-bridge-wifi doctor     # check for problems
  journalctl -u kvm-bridge-wifi -f

Pick a VM in the window and press Enable.  Inside the guest you should then
get an address from your real router -- the same subnet as the host -- instead
of 192.168.122.x.

In virt-manager the extra NIC shows up as "Generic ethernet connection".
Do not change it to "Bridge device"; that needs a real bridge on a wired
link and will not work over Wi-Fi.
EOF
