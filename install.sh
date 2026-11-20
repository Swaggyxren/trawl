#!/usr/bin/env bash
# Install Trawl from a local checkout (or TRAWL_SOURCE git URL) without Flatpak/AppImage.
set -euo pipefail

APP_ID="io.github.Swaggyxren.Trawl"
MIN_PYTHON="3.10"
MIN_ADW="1.5"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
  local answer
  read -r -p "$* [y/N] " answer || return 1
  [[ "$answer" =~ ^[Yy]$ ]]
}

if [[ "${EUID}" -eq 0 ]]; then
  die "Do not run this script as root. It uses sudo only for system packages."
fi

command -v sudo >/dev/null 2>&1 || die "sudo is required to install system packages."

# --------------------------------------------------------------- distro detect
[[ -r /etc/os-release ]] || die "/etc/os-release not found; cannot detect distro."
# shellcheck disable=SC1091
source /etc/os-release
DISTRO_ID="${ID:-unknown}"
DISTRO_LIKE="${ID_LIKE:-}"

PKG_MANAGER=""
PACKAGES=()
OPTIONAL_PACKAGES=()
case "${DISTRO_ID}" in
  arch|manjaro|endeavouros|cachyos)
    PKG_MANAGER="pacman"
    PACKAGES=(python python-gobject gtk4 libadwaita python-pipx desktop-file-utils)
    OPTIONAL_PACKAGES=(libcanberra gsound)
    ;;
  debian|ubuntu|linuxmint|pop|elementary)
    PKG_MANAGER="apt"
    PACKAGES=(python3 python3-venv python3-pip python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pipx desktop-file-utils)
    OPTIONAL_PACKAGES=(libcanberra-gtk3-module gsound-tools)
    ;;
  fedora|rhel|centos|rocky|almalinux)
    PKG_MANAGER="dnf"
    PACKAGES=(python3 python3-pip python3-gobject gtk4 libadwaita pipx desktop-file-utils)
    OPTIONAL_PACKAGES=(libcanberra-gtk3 gsound)
    ;;
  opensuse*|suse|sles)
    PKG_MANAGER="zypper"
    PACKAGES=(python3 python3-pip python3-gobject gtk4 libadwaita pipx desktop-file-utils)
    OPTIONAL_PACKAGES=(libcanberra-gtk3 gsound)
    ;;
  *)
    while IFS= read -r line; do
      case "${DISTRO_LIKE}" in
        *debian*) PACKAGES=(python3 python3-venv python3-pip python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pipx desktop-file-utils); break ;;
        *arch*) PACKAGES=(python python-gobject gtk4 libadwaita python-pipx desktop-file-utils); break ;;
        *fedora*|*rhel*) PACKAGES=(python3 python3-pip python3-gobject gtk4 libadwaita pipx desktop-file-utils); break ;;
        *suse*) PACKAGES=(python3 python3-pip python3-gobject gtk4 libadwaita pipx desktop-file-utils); break ;;
      esac
      break
    done <<< "${DISTRO_LIKE}"
    if [[ ${#PACKAGES[@]} -eq 0 ]]; then
      printf 'Unknown distro (%s). Required system packages:\n' "${DISTRO_ID}"
      printf '  Python 3, PyGObject, GTK 4, libadwaita typelibs, pipx\n'
      printf '  optional: libcanberra / GSound for completion sounds\n'
      exit 1
    fi
    ;;
esac

say "Detected distro: ${DISTRO_ID} (${PKG_MANAGER})"

# ------------------------------------------------------------- version checks
python_version() {
  python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true
}

adw_version() {
  python3 - <<'PY' 2>/dev/null || true
import gi
gi.require_version("Adw", "1")
from gi.repository import Adw
print("%d.%d" % (Adw.MAJOR_VERSION, Adw.MINOR_VERSION))
PY
}

version_at_least() {
  python3 - "$1" "$2" <<'PY'
import sys
def parts(v):
    return tuple(int(x) for x in v.split(".")[:2])
sys.exit(0 if parts(sys.argv[1]) >= parts(sys.argv[2]) else 1)
PY
}

CURRENT_PYTHON="$(python_version)"
if [[ -n "${CURRENT_PYTHON}" ]] && ! version_at_least "${CURRENT_PYTHON}" "${MIN_PYTHON}"; then
  die "Python ${CURRENT_PYTHON} is too old; Trawl needs >= ${MIN_PYTHON}."
elif [[ -z "${CURRENT_PYTHON}" ]]; then
  say "Python 3 not found yet; it will be installed."
else
  say "Python ${CURRENT_PYTHON} OK (minimum ${MIN_PYTHON})."
fi

CURRENT_ADW="$(adw_version)"
if [[ -n "${CURRENT_ADW}" ]] && ! version_at_least "${CURRENT_ADW}" "${MIN_ADW}"; then
  die "libadwaita ${CURRENT_ADW} is too old; Trawl needs >= ${MIN_ADW}."
elif [[ -n "${CURRENT_ADW}" ]]; then
  say "libadwaita ${CURRENT_ADW} OK (minimum ${MIN_ADW})."
else
  say "libadwaita typelibs not found yet; they will be installed."
fi

# ------------------------------------------------------------ package install
INSTALL_PACKAGES=("${PACKAGES[@]}")
if confirm "Install system packages: ${INSTALL_PACKAGES[*]}?"; then
  case "${PKG_MANAGER}" in
    pacman) sudo pacman -S --needed --noconfirm "${INSTALL_PACKAGES[@]}" ;;
    apt)
      sudo apt-get update
      sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${INSTALL_PACKAGES[@]}"
      ;;
    dnf) sudo dnf install -y "${INSTALL_PACKAGES[@]}" ;;
    zypper) sudo zypper --non-interactive install "${INSTALL_PACKAGES[@]}" ;;
  esac
else
  die "System packages are required; stopping."
fi

if confirm "Install optional sound packages: ${OPTIONAL_PACKAGES[*]}?"; then
  case "${PKG_MANAGER}" in
    pacman) sudo pacman -S --needed --noconfirm "${OPTIONAL_PACKAGES[@]}" || true ;;
    apt) sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${OPTIONAL_PACKAGES[@]}" || true ;;
    dnf) sudo dnf install -y "${OPTIONAL_PACKAGES[@]}" || true ;;
    zypper) sudo zypper --non-interactive install "${OPTIONAL_PACKAGES[@]}" || true ;;
  esac
fi

# Recheck after install.
CURRENT_PYTHON="$(python_version)"
[[ -n "${CURRENT_PYTHON}" ]] || die "Python still unavailable after install."
version_at_least "${CURRENT_PYTHON}" "${MIN_PYTHON}" || die "Python ${CURRENT_PYTHON} is too old."
CURRENT_ADW="$(adw_version)"
[[ -n "${CURRENT_ADW}" ]] || die "libadwaita typelibs still unavailable after install."
version_at_least "${CURRENT_ADW}" "${MIN_ADW}" || die "libadwaita ${CURRENT_ADW} is too old."

# ---------------------------------------------------------------- pipx install
TRAWL_SOURCE="${TRAWL_SOURCE:-${SCRIPT_DIR}}"
if confirm "Install Trawl with pipx from ${TRAWL_SOURCE}?"; then
  pipx install --system-site-packages --force "${TRAWL_SOURCE}"
else
  die "pipx install declined; stopping."
fi

PIPX_BIN_DIR="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || python3 -m pipx environment --value PIPX_BIN_DIR 2>/dev/null || printf '%s\n' "${HOME}/.local/bin")"
LAUNCHER="${PIPX_BIN_DIR}/trawl"
[[ -x "${LAUNCHER}" ]] || die "Expected launcher not found: ${LAUNCHER}"

# ----------------------------------------------------------- desktop + icons
say "Installing desktop entry and icons into ${HOME}/.local/share"
mkdir -p "${HOME}/.local/share/applications" \
  "${HOME}/.local/share/icons/hicolor/scalable/apps" \
  "${HOME}/.local/share/icons/hicolor/256x256/apps"

sed "s|^Exec=.*|Exec=${LAUNCHER} %U|" \
  "${SCRIPT_DIR}/data/io.github.Swaggyxren.Trawl.desktop" \
  > "${HOME}/.local/share/applications/io.github.Swaggyxren.Trawl.desktop"

cp "${SCRIPT_DIR}/data/icons/hicolor/scalable/apps/io.github.Swaggyxren.Trawl.svg" \
  "${HOME}/.local/share/icons/hicolor/scalable/apps/"
cp "${SCRIPT_DIR}/data/icons/hicolor/256x256/apps/io.github.Swaggyxren.Trawl.png" \
  "${HOME}/.local/share/icons/hicolor/256x256/apps/"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${HOME}/.local/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
  gtk4-update-icon-cache -q -f "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
elif command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -q -f "${HOME}/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

say "Trawl installed successfully."
printf '  Start from the app menu (Trawl), or run:\n    %s\n' "${LAUNCHER}"
printf '  Open a link directly:\n    %s https://example.com/archive.zip\n' "${LAUNCHER}"
