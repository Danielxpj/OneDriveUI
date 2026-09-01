#!/usr/bin/env bash
# Install OneDriveUI for the current user.
#
# WHAT IT DOES, IN ORDER
#
#   1. checks the things pip cannot install — rclone, PySide6, PyGObject,
#      nautilus-python — and stops before touching anything if one is missing
#   2. creates .venv WITH --system-site-packages
#   3. pip install -e .
#   4. links ~/.local/bin/onedriveui at the venv's entry point
#   5. onedriveui --install-extension  (Nautilus extension, 27 icons, .desktop)
#   6. onedriveui --doctor
#
# WHY THE VENV NEEDS --system-site-packages
#
# PySide6 and PyGObject must be the DISTRIBUTION's builds. The pacman PySide6 is
# compiled against the system Qt; a PyPI wheel ships its own copy of Qt and
# shadows it. The failure is not an import error — it is a QSystemTrayIcon that
# registers no StatusNotifierItem and a QtDBus that marshals differently, both
# of which read as bugs in this application. Same story for PyGObject and the
# system GLib. So the venv borrows them from the system instead of installing
# its own, and this script refuses to build one that cannot see them.
#
# WHAT IT DOES NOT DO
#
#   * it does not enable autostart — that is a switch in Settings, and turning
#     it on for you means a service you did not ask for at every login
#   * it does not run the setup wizard or sign you in
#   * it does not mount anything or write to your OneDrive
#   * it does not install system packages; it tells you the pacman line
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

bold=$'\033[1m'; red=$'\033[31m'; green=$'\033[32m'; dim=$'\033[2m'; off=$'\033[0m'
[ -t 1 ] || { bold=; red=; green=; dim=; off=; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$bold" "$off" "$*"; }
ok()   { printf '  %s[ok]%s   %s\n' "$green" "$off" "$*"; }
bad()  { printf '  %s[fail]%s %s\n' "$red" "$off" "$*"; }
note() { printf '  %s%s%s\n' "$dim" "$*" "$off"; }

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

case "${1:-}" in
    --uninstall) uninstall; exit 0 ;;
    --help|-h)
        sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;$d'
        exit 0 ;;
    "") ;;
    *) say "unknown argument: $1 (try --help)"; exit 2 ;;
esac

# ── 1. the things pip cannot install ──────────────────────────────────────

step "Checking what pip cannot install for you"

missing=()

if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    ok "python $(python3 -c 'import platform; print(platform.python_version())')"
else
    bad "python 3.12 or newer required, found $(python3 -V 2>&1)"
    missing+=(python)
fi

if command -v rclone >/dev/null 2>&1; then
    ok "rclone $(rclone version 2>/dev/null | head -1 | awk '{print $2}')"
else
    bad "rclone — the sync engine; nothing works without it"
    missing+=(rclone)
fi

# Imported with the SYSTEM interpreter on purpose: that is the one the venv will
# borrow from, so it is the one whose answer matters.
if python3 -c 'import PySide6.QtWidgets' 2>/dev/null; then
    ok "PySide6 $(python3 -c 'import PySide6; print(PySide6.__version__)')"
else
    bad "PySide6 — the entire GUI"
    missing+=(pyside6)
fi

if python3 -c 'import gi; gi.require_version("Gio", "2.0")' 2>/dev/null; then
    ok "PyGObject $(python3 -c 'import gi; print(gi.__version__)')"
else
    bad "PyGObject — notifications, the network and power monitors, the tray"
    missing+=(python-gobject)
fi

# The only optional one. Without it the application runs and the file emblems
# simply never appear, which is a confusing way to discover a missing package.
if [ -e /usr/lib/nautilus/extensions-4/libnautilus-python.so ]; then
    ok "nautilus-python"
else
    bad "nautilus-python — OPTIONAL: without it, no emblems or Status column"
    note "everything else works; install it later and re-run --install-extension"
fi

if [ ${#missing[@]} -gt 0 ]; then
    say ""
    say "${red}Stopping.${off} Install these first, then run this script again:"
    say ""
    say "    sudo pacman -S ${missing[*]}"
    say ""
    note "on a non-Arch distribution the package names differ, but the"
    note "requirement does not: they must be the DISTRIBUTION's builds."
    exit 1
fi

# ── 2. the venv ───────────────────────────────────────────────────────────

step "Creating the virtualenv at $VENV"

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

# ── 3. the package ────────────────────────────────────────────────────────

step "Installing OneDriveUI"

# Editable, because this is a checkout and not a release: `git pull` should be
# enough to update, without a reinstall that silently keeps the old code.
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$REPO"
ok "$("$VENV/bin/onedriveui" --version)"

# ── 4. the launcher ───────────────────────────────────────────────────────

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
    *)  note "$BINDIR is not on your PATH. Add it to your shell profile:"
        note "    fish_add_path $BINDIR        # fish"
        note "    export PATH=\"$BINDIR:\$PATH\"  # bash / zsh"
        ;;
esac

# ── 5. the desktop integration ────────────────────────────────────────────

step "Installing the Nautilus extension, the icons and the launcher entry"
"$VENV/bin/onedriveui" --install-extension

# ── 6. the verdict ────────────────────────────────────────────────────────

step "Self-check"
# Expected to report a dead daemon and no mount on a first install: nothing has
# been started and no account exists yet. `|| true` because those are not
# installation failures, and exiting non-zero here would say they were.
"$VENV/bin/onedriveui" --doctor || true

step "Done"
say "  Next:"
say "    onedriveui              ${dim}# first run opens the setup wizard${off}"
say ""
say "  Autostart is OFF. Turn it on in Settings once you trust it."
say "  Uninstall:  scripts/install.sh --uninstall"
