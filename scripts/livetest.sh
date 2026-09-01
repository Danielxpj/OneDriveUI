#!/usr/bin/env bash
# Run OneDriveUI against your real account, on a DEDICATED SUBFOLDER.
#
# WHY A SUBFOLDER, AND WHY THIS SCRIPT WAS REWRITTEN
#
# The first version mounted `onedrive:` — the whole drive — a second time,
# alongside the mount you already had. That looked safe ("we manage our own
# mountpoint, yours is untouched") and it destroyed a file.
#
# Two mounts of the same remote each keep their own directory cache and neither
# knows about the other. Rename something in mount A and mount B keeps the old
# listing for up to `--dir-cache-time`. Then a rename in mount B, made against
# that stale view, does what rclone always does for a rename-over: delete the
# destination, then move the source. The delete succeeds against the server. The
# move fails with `itemNotFound`, because the source it believed in was renamed
# fifteen minutes ago. Net result: a real file deleted from the real account,
# and nothing put back.
#
# That is what happened on 2026-09-01 to `LEY CANNABIS obsoleta.docx`.
#
# So this version mounts an ALIAS remote pointing at ONE SUBFOLDER:
#
#     onedriveui_test:  ->  onedrive:OneDriveUI-test
#
# Your own mount still shows that folder, as one directory among fifty-seven at
# your root. But nothing you do to your ordinary files can collide with anything
# this client does, because this client cannot see them: the alias is its entire
# universe. The stale-listing hazard does not disappear — it is inherent to two
# mounts of one account — it is confined to a folder that exists for testing.
#
# The directory cache is also cut from an hour to a minute here, so the window
# in which the two views can disagree is short enough to survive.
#
# WHY THE MOUNTPOINT IS A HIDDEN DIRECTORY
#
# A second OneDrive-shaped entry in the Nautilus sidebar, sitting right under
# your real one, is a good way to put a file in the wrong place. GLib decides
# what to show there in `g_unix_mount_guess_should_display`, and one of its
# rules is that any mountpoint whose path contains a dot component is hidden —
# "suppose it was a purpose to hide this mount", as the source puts it. So the
# test mount lives at `~/.onedriveui-test` and never appears beside yours.
#
# Verified on glib 2.88.3: mounting at `~/zz-hidetest` produced a sidebar entry,
# mounting at `~/.zz-hidetest-dot/mnt` produced none. (`-o x-gvfs-hide` does not
# work here — fusermount3 drops unknown options before they reach the kernel, so
# GLib never sees them.)
#
# To open it: Ctrl+L in Nautilus and type the path, or Ctrl+H to show hidden
# directories.
#
# WHAT IT WRITES TO YOUR ONEDRIVE
#
#   * one folder, `OneDriveUI-test/`, at your root. A real write to your real
#     account, and the only one this script makes.
#
# WHAT IT WRITES TO YOUR RCLONE CONFIG
#
#   * one alias remote, `onedriveui_test:`. Removed by --teardown.
#
# WHAT IT WRITES TO YOUR HOME
#
#   ~/.config/onedriveui/          config.json, filters
#   ~/.local/share/onedriveui/     state.db
#   ~/.local/state/onedriveui/     logs
#   ~/.cache/onedriveui/
#   ~/.config/systemd/user/        onedriveui-rcd.service
#                                  onedriveui-mount@onedriveui_test.service
#   ~/.onedriveui-test/            the mountpoint (hidden from the sidebar)
#
# It does NOT run the setup wizard, install the Nautilus extension, install
# icons, or set autostart. Those are separate, explicit commands.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

#: The real remote your account lives on. Only ever read from, and used once to
#: create the test folder below it.
BASE_REMOTE="${BASE_REMOTE:-onedrive}"
#: The folder inside it this client is allowed to see. Its entire universe.
REMOTE_PATH="${REMOTE_PATH:-OneDriveUI-test}"
#: The alias remote that points at exactly that folder.
TEST_REMOTE="${TEST_REMOTE:-onedriveui_test}"
#: Where it is mounted locally. The leading dot is load-bearing: it is what
#: keeps this mount out of the Nautilus sidebar, beside your real one.
SYNC_ROOT="${SYNC_ROOT:-$HOME/.onedriveui-test}"

MOUNT_UNIT="onedriveui-mount@${TEST_REMOTE}.service"

#: Mountpoints earlier versions of this script used. Teardown has to clean them
#: up too, or the visible sidebar entry outlives the design that created it.
LEGACY_ROOTS=("$HOME/OneDriveUI-test")

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;$d'
    cat <<EOF

USAGE

    scripts/livetest.sh              set up and run the GUI
    scripts/livetest.sh --status     one JSON snapshot, then exit
    scripts/livetest.sh --doctor     every self-check, then exit
    scripts/livetest.sh --teardown   put everything back
    scripts/livetest.sh --help

OVERRIDES

    BASE_REMOTE=$BASE_REMOTE
    REMOTE_PATH=$REMOTE_PATH
    TEST_REMOTE=$TEST_REMOTE
    SYNC_ROOT=$SYNC_ROOT

TEARDOWN LEAVES ONE THING BEHIND ON PURPOSE

    ${BASE_REMOTE}:${REMOTE_PATH} — the folder in your cloud. Deleting a folder
    from your real account is not something this script does on your behalf:

        rclone purge ${BASE_REMOTE}:${REMOTE_PATH}

EOF
}

teardown() {
    echo "stopping units..."
    for unit in "$MOUNT_UNIT" onedriveui-rcd.service onedriveui.service; do
        systemctl --user stop "$unit" 2>/dev/null || true
        systemctl --user disable "$unit" 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/$unit"
    done
    # The first design mounted the whole drive under a different unit name.
    # Anyone upgrading from it still has that unit and that mount.
    for legacy in "$HOME"/.config/systemd/user/onedriveui-mount@*.service; do
        [ -e "$legacy" ] || continue
        name="$(basename "$legacy")"
        echo "stopping legacy unit $name..."
        systemctl --user stop "$name" 2>/dev/null || true
        systemctl --user disable "$name" 2>/dev/null || true
        rm -f "$legacy"
    done
    systemctl --user daemon-reload 2>/dev/null || true

    for root in "$SYNC_ROOT" "${LEGACY_ROOTS[@]}"; do
        grep -q " $root fuse.rclone " /proc/self/mounts 2>/dev/null || continue
        echo "unmounting $root..."
        # Lazy: a shell or a file manager sitting in the directory is enough to
        # make a plain unmount fail, and a half-detached mount is worse than one
        # whose last reference closes on its own.
        fusermount3 -uz "$root" 2>/dev/null || true
    done

    if rclone listremotes 2>/dev/null | grep -qx "${TEST_REMOTE}:"; then
        echo "removing the alias remote ${TEST_REMOTE}:..."
        rclone config delete "$TEST_REMOTE" 2>/dev/null || true
    fi

    echo "removing our local state..."
    rm -rf "$HOME/.config/onedriveui" "$HOME/.local/share/onedriveui" \
           "$HOME/.local/state/onedriveui" "$HOME/.cache/onedriveui"
    rmdir "$SYNC_ROOT" "${LEGACY_ROOTS[@]}" 2>/dev/null || true

    echo
    echo "your own mount, untouched:"
    grep fuse.rclone /proc/self/mounts || echo "  (none)"
    echo
    echo "left in your cloud on purpose: ${BASE_REMOTE}:${REMOTE_PATH}"
    echo "  remove it yourself with:  rclone purge ${BASE_REMOTE}:${REMOTE_PATH}"
    echo "done."
}

case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --teardown) teardown; exit 0 ;;
esac

# ── the base remote has to exist; we only ever read from it ────────────────
if ! rclone listremotes 2>/dev/null | grep -qx "${BASE_REMOTE}:"; then
    echo >&2 "refusing: rclone has no remote called '${BASE_REMOTE}:'."
    echo >&2 "available: $(rclone listremotes 2>/dev/null | tr '\n' ' ')"
    exit 1
fi

# ── refuse a mountpoint that belongs to somebody else ──────────────────────
REAL_ROOT="$(cd "$SYNC_ROOT" 2>/dev/null && pwd || echo "$SYNC_ROOT")"
if grep -q " $REAL_ROOT fuse.rclone " /proc/self/mounts 2>/dev/null; then
    if systemctl --user is-active --quiet "$MOUNT_UNIT" 2>/dev/null; then
        echo >&2 "already mounted at $REAL_ROOT by $MOUNT_UNIT — reusing it."
    else
        echo >&2 "refusing: $REAL_ROOT is a live rclone mount this client did"
        echo >&2 "not create. Run --teardown first, or set SYNC_ROOT elsewhere."
        exit 1
    fi
fi

# ── refuse while anything from the whole-drive design is still live ────────
# The mountpoint moved, so the guard above no longer trips on it. A live
# `onedriveui-mount@onedrive.service` is the exact configuration that deleted a
# file, and leaving it running beside this one would put the account back in it.
for legacy_root in "${LEGACY_ROOTS[@]}"; do
    if grep -q " $legacy_root fuse.rclone " /proc/self/mounts 2>/dev/null; then
        echo >&2 "refusing: $legacy_root is still mounted — that is the"
        echo >&2 "whole-drive setup this redesign replaces. Run --teardown first."
        exit 1
    fi
done
for unit in $(systemctl --user list-units --type=service --state=active \
                  --no-legend 'onedriveui-mount@*.service' 2>/dev/null \
              | awk '{print $1}'); do
    [ "$unit" = "$MOUNT_UNIT" ] && continue
    echo >&2 "refusing: $unit is still running — it mounts more of your account"
    echo >&2 "than this test should ever see. Run --teardown first."
    exit 1
done

# ── the test folder in the cloud ───────────────────────────────────────────
# Idempotent, and the ONE write this script makes to your account.
echo >&2 "ensuring ${BASE_REMOTE}:${REMOTE_PATH} exists (the only cloud write)..."
rclone mkdir "${BASE_REMOTE}:${REMOTE_PATH}" 2>/dev/null || true

# ── the alias: this client's entire universe ───────────────────────────────
if ! rclone listremotes 2>/dev/null | grep -qx "${TEST_REMOTE}:"; then
    echo >&2 "creating alias ${TEST_REMOTE}: -> ${BASE_REMOTE}:${REMOTE_PATH}"
    rclone config create "$TEST_REMOTE" alias \
        remote "${BASE_REMOTE}:${REMOTE_PATH}" >/dev/null
fi

mkdir -p "$SYNC_ROOT"

# The config goes in your REAL config dir, because systemd has to see the units
# it produces. Built from the application's own defaults.
PYTHONPATH="$REPO" python3 - "$SYNC_ROOT" "$TEST_REMOTE" "${LEGACY_ROOTS[@]}" <<'PYEOF'
import pathlib
import sys

from onedriveui import config, paths

sync_root, remote = sys.argv[1], sys.argv[2]
stale_roots = {pathlib.Path(a).expanduser() for a in sys.argv[3:]}
stale_roots.add(pathlib.Path(sync_root))

path = paths.config_file()
cfg = config.load(path) if path.exists() else config.defaults()

# Every account left over from the whole-drive design goes. Not just the ones
# that collide on this mountpoint: `Application.start()` starts an engine for
# EVERY configured account, so a leftover `onedrive` entry would quietly mount
# the whole drive again the moment the GUI came up.
cfg.accounts = [
    a for a in cfg.accounts
    if a.id == remote or a.resolved_sync_root() not in stale_roots
]

account = next((a for a in cfg.accounts if a.id == remote), None)
if account is None:
    account = config.AccountConfig(id=remote, remote=remote)
    cfg.accounts.append(account)
account.sync_root = sync_root

# One minute, not one hour. Two mounts of one account cannot see each other's
# renames, and the directory cache is exactly how long they are allowed to
# disagree. An hour of disagreement is how a rename-over deleted a real file.
account.mount.dir_cache_time_s = 60
account.mount.poll_interval_s = 30

cfg.app.active_account_id = account.id
cfg.app.first_run_complete = True     # never show the wizard: it writes into
cfg.app.autostart = False             # the sync root

config.save(cfg, path, emit=False)
print(f"config:  {path}", file=sys.stderr)
print(f"account: {account.id} -> {account.sync_root}", file=sys.stderr)
PYEOF

echo >&2
echo >&2 "mount:    ${TEST_REMOTE}: (= ${BASE_REMOTE}:${REMOTE_PATH}) at $SYNC_ROOT"
echo >&2 "this client cannot see anything else in your account."
echo >&2 "teardown: scripts/livetest.sh --teardown"
echo >&2

cd "$REPO"
exec python3 -m onedriveui "$@"
