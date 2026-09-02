#!/usr/bin/env bash
# OneDriveUI installer — for the current user, no system files touched except
# the packages you agree to install.
#
# WHAT IT DOES, IN ORDER
#
#   1. looks at the machine: distribution, package manager, session type
#   2. checks every dependency pip cannot install — rclone, PySide6, PyGObject,
#      fuse3, nautilus-python — and, if any is missing, prints the exact
#      package line for YOUR distribution and offers to run it for you
#   3. creates .venv WITH --system-site-packages
#   4. pip install -e .
#   5. links ~/.local/bin/onedriveui at the venv's entry point
#   6. onedriveui --install-extension  (Nautilus extension, 27 icons, .desktop)
#   7. onedriveui --doctor
#
# WHY THE VENV NEEDS --system-site-packages
#
# PySide6 and PyGObject must be the DISTRIBUTION's builds. A distro PySide6 is
# compiled against the system Qt; a PyPI wheel ships its own copy of Qt and
# shadows it. The failure is not an import error — it is a QSystemTrayIcon that
# registers no StatusNotifierItem and a QtDBus that marshals differently, both
# of which read as bugs in this application. Same story for PyGObject and the
# system GLib. So the venv borrows them from the system instead of installing
# its own, and this script refuses to build one that cannot see them.
#
# WHAT IT DOES NOT DO
#
#   * it never installs a system package without asking (--yes to skip the ask)
#   * it does not enable autostart — that is a switch in Settings, and turning
#     it on for you means a service you did not ask for at every login
#   * it does not run the setup wizard or sign you in
#   * it does not mount anything or write to your OneDrive
#
# OPTIONS
#
#   --check            only report what is missing; change nothing
#   --yes, -y          answer yes to the dependency prompts (non-interactive)
#   --no-deps          never install system packages; just say what is missing
#   --uninstall        remove the extension, icons, launcher and venv
#   --help, -h         this text
#
# UNINSTALL
#
#     scripts/install.sh --uninstall
#
# which removes the extension, the icons, the launcher and the venv, and leaves
# your config, your database and your files exactly where they are.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$REPO/.venv}"
BINDIR="${BINDIR:-$HOME/.local/bin}"
LINK="$BINDIR/onedriveui"

#: The oldest rclone whose rc API this client was written against. Below it the
#: mount and the rc endpoints differ in ways the code does not paper over.
RCLONE_MIN="1.75.0"
#: The oldest Python that can run the codebase (PEP 695 generics, `type` stmt).
PY_MIN_MAJOR=3
PY_MIN_MINOR=12

ASSUME_YES=0
INSTALL_DEPS=1
CHECK_ONLY=0

bold=$'\033[1m'; red=$'\033[31m'; green=$'\033[32m'; yellow=$'\033[33m'
blue=$'\033[34m'; dim=$'\033[2m'; off=$'\033[0m'
[ -t 1 ] || { bold=; red=; green=; yellow=; blue=; dim=; off=; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$bold" "$off" "$*"; }
ok()   { printf '  %s[ok]%s    %s\n' "$green" "$off" "$*"; }
bad()  { printf '  %s[fail]%s  %s\n' "$red" "$off" "$*"; }
warn() { printf '  %s[warn]%s  %s\n' "$yellow" "$off" "$*"; }
note() { printf '  %s%s%s\n' "$dim" "$*" "$off"; }
info() { printf '  %s%s%s\n' "$blue" "$*" "$off"; }

# ── argument parsing ──────────────────────────────────────────────────────

show_help() {
    sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;$d'
}

DO_UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --uninstall)      DO_UNINSTALL=1 ;;
        --check|--dry-run) CHECK_ONLY=1 ;;
        --yes|-y)         ASSUME_YES=1 ;;
        --no-deps|--no-install-deps) INSTALL_DEPS=0 ;;
        --help|-h)        show_help; exit 0 ;;
        *) say "unknown argument: $1 (try --help)"; exit 2 ;;
    esac
    shift
done

# ── uninstall ─────────────────────────────────────────────────────────────

uninstall() {
    step "Removing the Nautilus extension and the icons"
    if [ -x "$VENV/bin/python" ]; then
        # Not `--uninstall-extension`: that flag deliberately keeps the icons,
        # because removing the file-manager integration should not take the
        # tray icon with it. Here we are removing the whole installation, so
        # the SVGs this script wrote should go too.
        "$VENV/bin/python" -c '
from onedriveui.ext import install
report = install.uninstall(remove_icons=True)
for error in report.errors:
    print(f"  error: {error}")
print("  extension and icons removed")
' || note "could not run the uninstaller — removing what we can"
    else
        note "no venv at $VENV — skipping"
    fi

    step "Removing the launcher"
    if [ -L "$LINK" ]; then
        rm -f "$LINK"; ok "removed $LINK"
    else
        note "$LINK is not our symlink — left alone"
    fi
    rm -f "$HOME/.local/share/applications/onedriveui.desktop"

    step "Removing the virtualenv"
    if [ -d "$VENV" ]; then
        rm -rf "$VENV"; ok "removed $VENV"
    fi

    step "Left in place, on purpose"
    say "  your files            (the sync root — never touched by this script)"
    say "  ~/.config/onedriveui  config.json, filters"
    say "  ~/.local/share/onedriveui  state.db"
    say "  ~/.config/rclone/rclone.conf   your account and its token"
    say ""
    say "  Nothing here deletes data. To go further, do it deliberately:"
    say "    systemctl --user disable --now onedriveui.service"
    say "    rm -rf ~/.config/onedriveui ~/.local/share/onedriveui \\"
    say "           ~/.local/state/onedriveui ~/.cache/onedriveui"
    say ""
    ok "uninstalled"
}

[ "$DO_UNINSTALL" = 1 ] && { uninstall; exit 0; }

# ── 0. what machine is this ───────────────────────────────────────────────
#
# The package names below are the only reason the installer needs to know. The
# family, not the distribution, is what selects them: derivatives (CachyOS,
# EndeavourOS, Mint, Pop!_OS, Nobara) inherit their parent's names through
# ID_LIKE, so the table stays short and still covers most of the desktop world.

DISTRO_ID="unknown"; DISTRO_NAME="unknown Linux"; DISTRO_LIKE=""
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-unknown}"
    DISTRO_NAME="${PRETTY_NAME:-${NAME:-$DISTRO_ID}}"
    DISTRO_LIKE="${ID_LIKE:-}"
fi

# FAMILY drives the package table; PM is the command that installs.
FAMILY=""; PM=""; PM_INSTALL=""; PM_REFRESH=""
detect_family() {
    local token
    for token in "$DISTRO_ID" $DISTRO_LIKE; do
        case "$token" in
            arch|archarm|cachyos|endeavouros|manjaro|garuda) FAMILY=arch; return ;;
            debian|ubuntu|linuxmint|pop|raspbian)            FAMILY=debian; return ;;
            fedora|rhel|centos|almalinux|rocky|nobara)       FAMILY=fedora; return ;;
            opensuse*|suse|sles)                             FAMILY=suse; return ;;
            void)                                            FAMILY=void; return ;;
            alpine)                                          FAMILY=alpine; return ;;
            gentoo)                                          FAMILY=gentoo; return ;;
        esac
    done
    # No usable os-release (or a distribution not in the table): fall back to
    # whichever package manager is actually on the machine.
    if   command -v pacman  >/dev/null 2>&1; then FAMILY=arch
    elif command -v apt-get >/dev/null 2>&1; then FAMILY=debian
    elif command -v dnf     >/dev/null 2>&1; then FAMILY=fedora
    elif command -v zypper  >/dev/null 2>&1; then FAMILY=suse
    elif command -v xbps-install >/dev/null 2>&1; then FAMILY=void
    elif command -v apk     >/dev/null 2>&1; then FAMILY=alpine
    elif command -v emerge  >/dev/null 2>&1; then FAMILY=gentoo
    else FAMILY=unknown
    fi
}
detect_family

case "$FAMILY" in
    arch)   PM=pacman; PM_INSTALL="pacman -S --needed" ;;
    debian) PM=apt;    PM_INSTALL="apt install -y"; PM_REFRESH="apt update" ;;
    fedora) PM=dnf;    PM_INSTALL="dnf install -y" ;;
    suse)   PM=zypper; PM_INSTALL="zypper install -y" ;;
    void)   PM=xbps;   PM_INSTALL="xbps-install -Sy" ;;
    alpine) PM=apk;    PM_INSTALL="apk add" ;;
    gentoo) PM=emerge; PM_INSTALL="emerge --noreplace" ;;
    *)      PM="";     PM_INSTALL="" ;;
esac

# How to become root, if we need to. pkexec is last: it opens a GUI prompt,
# which is fine but loses the terminal output of the package manager.
SUDO=""
if [ "$(id -u)" = 0 ]; then SUDO=""
elif command -v sudo   >/dev/null 2>&1; then SUDO="sudo"
elif command -v doas   >/dev/null 2>&1; then SUDO="doas"
elif command -v pkexec >/dev/null 2>&1; then SUDO="pkexec"
fi

# ── the dependency table ──────────────────────────────────────────────────
#
# One row per thing pip cannot install for you, with the package name it goes
# by in each family. An empty name means "this family ships it inside another
# package" (python's venv, for instance, is only split out on Debian).

declare -A PKG_arch=(
    [python]="python"           [venv]=""
    [rclone]="rclone"           [pyside6]="pyside6"
    [gobject]="python-gobject"  [fuse3]="fuse3"
    [nautilus]="nautilus-python" [wayland]="qt6-wayland"
)
declare -A PKG_debian=(
    [python]="python3"          [venv]="python3-venv"
    [rclone]="rclone"           [pyside6]="python3-pyside6.qtwidgets python3-pyside6.qtsvg python3-pyside6.qtnetwork"
    [gobject]="python3-gi"      [fuse3]="fuse3"
    [nautilus]="python3-nautilus" [wayland]="qt6-wayland"
)
declare -A PKG_fedora=(
    [python]="python3"          [venv]=""
    [rclone]="rclone"           [pyside6]="python3-pyside6"
    [gobject]="python3-gobject" [fuse3]="fuse3"
    [nautilus]="nautilus-python3" [wayland]="qt6-qtwayland"
)
declare -A PKG_suse=(
    [python]="python3"          [venv]=""
    [rclone]="rclone"           [pyside6]="python3-pyside6"
    [gobject]="python3-gobject" [fuse3]="fuse3"
    [nautilus]="python3-nautilus" [wayland]="libQt6WaylandClient6"
)
declare -A PKG_void=(
    [python]="python3"          [venv]=""
    [rclone]="rclone"           [pyside6]="python3-pyside6"
    [gobject]="python3-gobject" [fuse3]="fuse3"
    [nautilus]="nautilus-python" [wayland]="qt6-wayland"
)
declare -A PKG_alpine=(
    [python]="python3"          [venv]=""
    [rclone]="rclone"           [pyside6]="py3-pyside6"
    [gobject]="py3-gobject3"    [fuse3]="fuse3"
    [nautilus]="" [wayland]="qt6-qtwayland"
)
declare -A PKG_gentoo=(
    [python]="dev-lang/python"  [venv]=""
    [rclone]="net-misc/rclone"  [pyside6]="dev-python/pyside"
    [gobject]="dev-python/pygobject" [fuse3]="sys-fs/fuse"
    [nautilus]="gnome-extra/nautilus-python" [wayland]="dev-qt/qtwayland"
)

pkg_for() {  # pkg_for <key> -> the package name(s) for this family, or ""
    local key="$1" name
    case "$FAMILY" in
        arch)   name="${PKG_arch[$key]-}" ;;
        debian) name="${PKG_debian[$key]-}" ;;
        fedora) name="${PKG_fedora[$key]-}" ;;
        suse)   name="${PKG_suse[$key]-}" ;;
        void)   name="${PKG_void[$key]-}" ;;
        alpine) name="${PKG_alpine[$key]-}" ;;
        gentoo) name="${PKG_gentoo[$key]-}" ;;
        *)      name="" ;;
    esac
    printf '%s' "$name"
}

# ── probes ────────────────────────────────────────────────────────────────
#
# Every Python probe runs under the SYSTEM interpreter on purpose: that is the
# one the venv will borrow from, so it is the one whose answer matters.

have_python() {
    python3 -c "import sys; sys.exit(0 if sys.version_info >= ($PY_MIN_MAJOR, $PY_MIN_MINOR) else 1)" 2>/dev/null
}
have_venv()    { python3 -c 'import venv, ensurepip' 2>/dev/null; }
have_rclone()  { command -v rclone >/dev/null 2>&1; }
have_pyside6() { python3 -c 'import PySide6.QtWidgets' 2>/dev/null; }
have_gobject() { python3 -c 'import gi; gi.require_version("Gio", "2.0")' 2>/dev/null; }
have_fuse3()   { command -v fusermount3 >/dev/null 2>&1; }

detail_pyside6() { python3 -c 'import PySide6; print(PySide6.__version__)'; }
detail_gobject() { python3 -c 'import gi; print(gi.__version__)'; }

# nautilus-python installs its loader as a Nautilus 4 extension. The path is
# not the same on every distribution (Debian multiarch puts it under
# /usr/lib/<triplet>/), so glob the ones that exist rather than naming one.
have_nautilus_python() {
    local so
    for so in /usr/lib*/nautilus/extensions-4/libnautilus-python.so \
              /usr/lib/*/nautilus/extensions-4/libnautilus-python.so \
              /usr/local/lib*/nautilus/extensions-4/libnautilus-python.so; do
        [ -e "$so" ] && return 0
    done
    return 1
}
have_nautilus() { command -v nautilus >/dev/null 2>&1; }

# Qt can draw through XWayland, so this is never fatal — but on a Wayland
# session without the platform plugin the window decorations, the scaling and
# the fractional-scale crispness are all the X11 fallback's, which looks like a
# rendering bug in the app rather than a missing package.
have_qt_wayland() {
    python3 - <<'PY' 2>/dev/null
import sys, pathlib
try:
    import PySide6
except Exception:
    sys.exit(1)
root = pathlib.Path(PySide6.__file__).parent
hits = list(root.glob("**/platforms/libqwayland*.so"))
hits += list(pathlib.Path("/usr/lib").glob("qt6/plugins/platforms/libqwayland*.so"))
hits += list(pathlib.Path("/usr/lib64").glob("qt6/plugins/platforms/libqwayland*.so"))
hits += list(pathlib.Path("/usr/lib").glob("*/qt6/plugins/platforms/libqwayland*.so"))
sys.exit(0 if hits else 1)
PY
}

version_ge() {  # version_ge A B -> true when A >= B
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}

rclone_version() {
    rclone version 2>/dev/null | head -1 | awk '{print $2}' | sed 's/^v//'
}

# ── 1. the machine ────────────────────────────────────────────────────────

say ""
say "${bold}OneDriveUI installer${off}"
say "${dim}A OneDrive client for Linux, on top of rclone. Nothing is installed"
say "outside your home directory except the system packages you approve.${off}"

# Everything below — the probes, the venv, the app itself — goes through
# python3. Without it there is nothing to report on.
if ! command -v python3 >/dev/null 2>&1; then
    say ""
    bad "no python3 on this machine"
    note "install your distribution's python3 (≥ ${PY_MIN_MAJOR}.${PY_MIN_MINOR}) and run this again"
    exit 1
fi

step "Looking at this machine"
say "  distribution   $DISTRO_NAME"
if [ -n "$PM" ]; then
    say "  package family $FAMILY ${dim}(via $PM)${off}"
else
    say "  package family ${yellow}unknown${off} ${dim}— you will get the package list, not the command${off}"
fi
say "  session        ${XDG_SESSION_TYPE:-unknown} ${dim}on ${XDG_CURRENT_DESKTOP:-unknown desktop}${off}"
say "  python         $(python3 -V 2>&1 | awk '{print $2}') ${dim}($(command -v python3))${off}"
if [ -d /run/systemd/system ]; then
    say "  init           systemd ${dim}(the two rclone services run as user units)${off}"
else
    say "  init           ${yellow}not systemd${off} ${dim}— the engine is supervised by user units; see README${off}"
fi

# ── 2. the dependency check ───────────────────────────────────────────────

step "Checking the dependencies pip cannot install"
note "pip can install everything else. These five must come from your"
note "distribution, because they are compiled against it."
say ""

missing_keys=(); missing_pkgs=(); optional_keys=()

# check_required <key> <label> <why>
#
# The key is the whole wiring: `have_<key>` is the probe, `detail_<key>` is the
# optional version line, and `pkg_for <key>` is the package to install.
check_required() {
    local key="$1" label="$2" why="$3" pkgs detail=""
    if "have_${key}" 2>/dev/null; then
        if declare -F "detail_${key}" >/dev/null; then
            detail="$("detail_${key}" 2>/dev/null || true)"
        fi
        ok "$label${detail:+ $detail} ${dim}— $why${off}"
        return 0
    fi
    bad "$label — $why"
    pkgs="$(pkg_for "$key")"
    missing_keys+=("$key")
    [ -n "$pkgs" ] && missing_pkgs+=($pkgs)
}

# python itself
if have_python; then
    ok "python $(python3 -c 'import platform; print(platform.python_version())') ${dim}(need ≥ ${PY_MIN_MAJOR}.${PY_MIN_MINOR})${off}"
else
    bad "python ≥ ${PY_MIN_MAJOR}.${PY_MIN_MINOR} — found $(python3 -V 2>&1 | awk '{print $2}')"
    note "the codebase uses 3.12 syntax; an older interpreter cannot import it"
    missing_keys+=(python)
    p="$(pkg_for python)"; [ -n "$p" ] && missing_pkgs+=($p)
fi

# venv + ensurepip (split out of python only on Debian and its derivatives)
if have_venv; then
    ok "python venv ${dim}(the installer builds one at .venv)${off}"
else
    bad "python venv/ensurepip — cannot create the virtualenv without it"
    missing_keys+=(venv)
    p="$(pkg_for venv)"; [ -n "$p" ] && missing_pkgs+=($p)
fi

# rclone, with a version floor
if have_rclone; then
    rcv="$(rclone_version)"
    if [ -n "$rcv" ] && version_ge "$rcv" "$RCLONE_MIN"; then
        ok "rclone $rcv ${dim}(need ≥ $RCLONE_MIN)${off}"
    else
        bad "rclone $rcv is older than $RCLONE_MIN"
        note "this client is written against the v$RCLONE_MIN rc API: older builds"
        note "answer some endpoints differently and lack others entirely"
        missing_keys+=(rclone)
        p="$(pkg_for rclone)"; [ -n "$p" ] && missing_pkgs+=($p)
    fi
else
    bad "rclone — the sync engine; nothing works without it"
    missing_keys+=(rclone)
    p="$(pkg_for rclone)"; [ -n "$p" ] && missing_pkgs+=($p)
fi

# The key is also the probe name (`have_<key>`) and the row in the package
# table, so these three lines are the whole check for three dependencies.
check_required pyside6 "PySide6" \
    "the entire GUI; must be your distribution's build, never a PyPI wheel"

check_required gobject "PyGObject" \
    "notifications, the network and power monitors, the tray"

check_required fuse3 "fuse3 (fusermount3)" \
    "rclone mount is a FUSE filesystem; without it nothing mounts"

# ── the optional ones ─────────────────────────────────────────────────────

if have_nautilus_python; then
    ok "nautilus-python ${dim}(emblems and the Status column)${off}"
elif have_nautilus; then
    warn "nautilus-python — OPTIONAL: without it, no emblems or Status column"
    note "everything else works; install it later and re-run --install-extension"
    optional_keys+=(nautilus)
else
    note "no Nautilus on this machine — the file-manager integration is skipped"
fi

if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
    if have_qt_wayland; then
        ok "Qt Wayland platform plugin"
    else
        warn "Qt Wayland plugin missing — OPTIONAL: Qt will fall back to XWayland"
        note "the app still runs; scaling and decorations will be the X11 ones"
        optional_keys+=(wayland)
    fi
fi

# ── 3. offer to install what is missing ───────────────────────────────────

install_packages() {  # install_packages <pkg>...
    local pkgs=("$@")
    if [ -z "$PM_INSTALL" ]; then
        bad "no known package manager here — install these by hand:"
        say "      ${pkgs[*]}"
        return 1
    fi
    if [ -z "$SUDO" ] && [ "$(id -u)" != 0 ]; then
        bad "no sudo/doas/pkexec — run this as root, or install by hand:"
        say "      $PM_INSTALL ${pkgs[*]}"
        return 1
    fi
    if [ -n "$PM_REFRESH" ]; then
        say ""
        info "\$ $SUDO $PM_REFRESH"
        # A stale apt index is the usual reason an install of a package that
        # plainly exists fails; refresh, but do not fail the run over it.
        $SUDO $PM_REFRESH || warn "index refresh failed — continuing anyway"
    fi
    say ""
    info "\$ $SUDO $PM_INSTALL ${pkgs[*]}"
    $SUDO $PM_INSTALL "${pkgs[@]}"
}

ask() {  # ask <question> -> 0 for yes
    local answer
    [ "$ASSUME_YES" = 1 ] && { say "  $1 ${dim}[--yes]${off}"; return 0; }
    [ -t 0 ] || return 1
    printf '\n  %s [y/N] ' "$1"
    read -r answer || return 1
    case "$answer" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

install_rclone_upstream() {
    if ! command -v curl >/dev/null 2>&1; then
        warn "no curl here, so rclone's own installer cannot be fetched"
        note "install a newer rclone by hand: https://rclone.org/downloads/"
        return 1
    fi
    say ""
    note "rclone's own installer fetches the current release from rclone.org"
    note "and writes /usr/bin/rclone. It is the documented install path when a"
    note "distribution ships an older build."
    info "\$ curl -fsSL https://rclone.org/install.sh | $SUDO bash"
    if ask "Run rclone's official installer now?"; then
        curl -fsSL https://rclone.org/install.sh | $SUDO bash
    else
        return 1
    fi
}

if [ ${#missing_keys[@]} -gt 0 ]; then
    say ""
    say "${red}${bold}Missing: ${missing_keys[*]}${off}"

    if [ "$CHECK_ONLY" = 1 ]; then
        say ""
        [ -n "$PM_INSTALL" ] && say "    $SUDO $PM_INSTALL ${missing_pkgs[*]}"
        say ""
        note "--check: nothing was installed or changed."
        exit 1
    fi

    if [ "$INSTALL_DEPS" = 0 ]; then
        say ""
        say "  Install them and run this script again:"
        [ -n "$PM_INSTALL" ] && say "    $SUDO $PM_INSTALL ${missing_pkgs[*]}"
        exit 1
    fi

    # Python itself is the one thing the installer will not try to fix: on most
    # distributions the answer is not a package but an upgrade of the whole
    # system, and installing a second interpreter beside the system one is how
    # you end up with a PySide6 the venv cannot see.
    for k in "${missing_keys[@]}"; do
        if [ "$k" = python ]; then
            say ""
            say "  ${bold}Python ${PY_MIN_MAJOR}.${PY_MIN_MINOR}+ has to come first, and by hand.${off}"
            note "Upgrade the distribution, or use a distro build of a newer python3."
            note "Do not install a second interpreter beside the system one: it will"
            note "not see the system PySide6, which is the whole point of the venv."
            exit 1
        fi
    done

    if [ ${#missing_pkgs[@]} -gt 0 ]; then
        say ""
        say "  These come from your distribution:"
        say "    ${bold}${missing_pkgs[*]}${off}"
        if ask "Install them now with $PM?"; then
            install_packages "${missing_pkgs[@]}" || exit 1
        else
            say ""
            note "Nothing was installed. Run this when you are ready:"
            [ -n "$PM_INSTALL" ] && say "    $SUDO $PM_INSTALL ${missing_pkgs[*]}"
            exit 1
        fi
    elif [ -z "$PM_INSTALL" ]; then
        say ""
        note "This distribution is not in the package table. The requirement is"
        note "the same everywhere: rclone ≥ $RCLONE_MIN, and your distribution's own"
        note "PySide6, PyGObject and fuse3 packages — never PyPI wheels."
        exit 1
    fi

    # Re-probe. An install that "succeeded" and left the import still failing is
    # the case worth catching here, not three steps later inside the GUI.
    step "Re-checking after the install"
    still=()
    have_python  || still+=(python)
    have_venv    || still+=(venv)
    if have_rclone; then
        rcv="$(rclone_version)"
        if ! version_ge "${rcv:-0}" "$RCLONE_MIN"; then
            warn "rclone is still $rcv, older than $RCLONE_MIN"
            install_rclone_upstream || still+=(rclone)
        fi
    else
        still+=(rclone)
    fi
    have_pyside6 || still+=(pyside6)
    have_gobject || still+=(python-gobject)
    have_fuse3   || still+=(fuse3)

    if [ ${#still[@]} -gt 0 ]; then
        bad "still missing after the install: ${still[*]}"
        note "the package went in but the import still fails — the usual cause is"
        note "that python3 here is not the interpreter the packages were built for."
        note "Check:  python3 -c 'import sys; print(sys.executable, sys.version)'"
        exit 1
    fi
    ok "everything required is present"
else
    say ""
    ok "${bold}every required dependency is present${off}"
fi

# The optional ones get their own, separate offer: they are not worth blocking
# an install over, and a user who says no should still end up with a client.
if [ ${#optional_keys[@]} -gt 0 ] && [ "$CHECK_ONLY" = 0 ] && [ "$INSTALL_DEPS" = 1 ]; then
    opt_pkgs=()
    for k in "${optional_keys[@]}"; do
        p="$(pkg_for "$k")"; [ -n "$p" ] && opt_pkgs+=($p)
    done
    if [ ${#opt_pkgs[@]} -gt 0 ] && [ -n "$PM_INSTALL" ]; then
        say ""
        say "  Optional, and recommended: ${bold}${opt_pkgs[*]}${off}"
        if ask "Install the optional packages too?"; then
            install_packages "${opt_pkgs[@]}" || warn "optional install failed — continuing"
        else
            note "skipped — the client works without them"
        fi
    fi
fi

if [ "$CHECK_ONLY" = 1 ]; then
    say ""
    ok "--check: dependencies satisfied. Run without --check to install."
    exit 0
fi

# ── 4. the venv ───────────────────────────────────────────────────────────

step "Creating the virtualenv at $VENV"
note "with --system-site-packages, so it can see the distro PySide6 and PyGObject"

if [ -d "$VENV" ]; then
    # An existing venv built without --system-site-packages is the failure this
    # whole script is arranged around, and it is invisible until the tray does
    # not appear. Detect it and say so rather than installing into it.
    if "$VENV/bin/python" -c 'import PySide6' 2>/dev/null; then
        ok "reusing the existing venv (it can see the system PySide6)"
    else
        bad "the existing venv cannot see the system PySide6"
        note "it was built without --system-site-packages; rebuilding it"
        rm -rf "$VENV"
    fi
fi

if [ ! -d "$VENV" ]; then
    python3 -m venv --system-site-packages "$VENV"
    ok "created (with --system-site-packages)"
fi

# ── 5. the package ────────────────────────────────────────────────────────

step "Installing OneDriveUI into the virtualenv"

# Editable, because this is a checkout and not a release: `git pull` should be
# enough to update, without a reinstall that silently keeps the old code.
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$REPO"
ok "$("$VENV/bin/onedriveui" --version)"
note "editable install: a git pull updates the app, no reinstall needed"

# ── 6. the launcher ───────────────────────────────────────────────────────

step "Linking $LINK"

mkdir -p "$BINDIR"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    bad "$LINK exists and is not a symlink — leaving it alone"
    note "run the venv's binary directly: $VENV/bin/onedriveui"
else
    ln -sfn "$VENV/bin/onedriveui" "$LINK"
    ok "onedriveui -> $VENV/bin/onedriveui"
fi

case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *)  warn "$BINDIR is not on your PATH. Add it to your shell profile:"
        note "    fish_add_path $BINDIR        # fish"
        note "    export PATH=\"$BINDIR:\$PATH\"  # bash / zsh"
        ;;
esac

# ── 7. the desktop integration ────────────────────────────────────────────

step "Installing the Nautilus extension, the icons and the launcher entry"
"$VENV/bin/onedriveui" --install-extension

# ── 8. the verdict ────────────────────────────────────────────────────────

step "Self-check"
note "a dead daemon and no mount are EXPECTED here: nothing has been started"
note "and no account exists yet — the wizard does both"
"$VENV/bin/onedriveui" --doctor || true

step "Done"
say "  Next:"
if have_nautilus; then
    say "    nautilus -q             ${dim}# Nautilus does not reload extensions while running${off}"
fi
say "    onedriveui              ${dim}# first run opens the setup wizard${off}"
say ""
say "  Autostart is OFF. Turn it on in Settings once you trust it."
say "  Uninstall:  scripts/install.sh --uninstall"
say ""
