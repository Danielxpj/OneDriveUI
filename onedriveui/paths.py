"""FROZEN CONTRACT. Every filesystem path, in one place.

XDG_CONFIG_HOME, XDG_DATA_HOME and XDG_CACHE_HOME are ALL UNSET on the target
machine — never read them without a fallback. XDG_RUNTIME_DIR is /run/user/1000.

Rules baked into this module:
  * Directories this application owns are created on first use with mode 0700.
    Files are NOT created here — a path function only ever returns a path.
  * Nothing here is cached: tests monkeypatch HOME and the XDG variables, and a
    cached Path would outlive the patch.
  * The rclone cache tree (~/.cache/rclone) is READ ONLY for us — rclone owns it.
    The real VFS locations ALWAYS come from vfs/stats.diskCache.path/.pathMeta
    (invariant I4); rclone_vfs_dir()/rclone_vfs_meta_dir() are a last-resort
    fallback for a daemon that is not answering, never the primary source.
  * The sync root IS the FUSE mountpoint. is_under_fuse_mount() is the basis of
    invariant I2 (no rclone data command may name a path under a fuse mount) and
    of the refusal to open the SQLite database under one.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from onedriveui import APP_ID

# ─────────────────────────────────────────────────────────────────────────────
# XDG base directories
# ─────────────────────────────────────────────────────────────────────────────

#: /proc/self/mounts escapes space, tab, newline and backslash as octal.
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")

_PROC_MOUNTS = Path("/proc/self/mounts")

DIR_MODE = 0o700
FILE_MODE = 0o600


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var)
    return Path(v).expanduser() if v else Path.home() / default


def _ensure(path: Path, mode: int = DIR_MODE, *, tighten: bool = True) -> Path:
    """mkdir -p the directory and give the leaf `mode`. Idempotent, and cheap
    enough to call from every accessor: one stat in the common case.

    `tighten` re-applies the mode to a directory that already existed. It is on
    for the directories we own — they hold the rc password, the IPC socket and
    the token-adjacent state, so 0700 is enforced on every call — and OFF for
    shared XDG directories such as ~/.config/systemd/user, whose permissions
    belong to the desktop, not to us. mkdir's own mode argument is masked by the
    umask, hence the explicit chmod.
    """
    try:
        existed = path.is_dir()
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        if (not existed or tighten) and (path.stat().st_mode & 0o777) != mode:
            path.chmod(mode)
    except OSError:
        # A read-only HOME or a racing peer must not crash a path lookup; the
        # caller that actually writes will report the real error.
        pass
    return path


def config_dir() -> Path:
    """~/.config/onedriveui"""
    return _ensure(_xdg("XDG_CONFIG_HOME", ".config") / APP_ID)


def data_dir() -> Path:
    """~/.local/share/onedriveui"""
    return _ensure(_xdg("XDG_DATA_HOME", ".local/share") / APP_ID)


def state_dir() -> Path:
    """~/.local/state/onedriveui"""
    return _ensure(_xdg("XDG_STATE_HOME", ".local/state") / APP_ID)


def cache_dir() -> Path:
    """~/.cache/onedriveui — OUR cache. Not rclone's (see rclone_cache_dir())."""
    return _ensure(_xdg("XDG_CACHE_HOME", ".cache") / APP_ID)


def runtime_dir() -> Path:
    """$XDG_RUNTIME_DIR/onedriveui, falling back to state_dir()/run when unset.

    Everything here is per-boot: sockets, the lock file and endpoints.json."""
    rt = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(rt).expanduser() / APP_ID if rt else state_dir() / "run"
    return _ensure(base)


# ─────────────────────────────────────────────────────────────────────────────
# Our own files
# ─────────────────────────────────────────────────────────────────────────────

def config_file() -> Path:
    """config_dir()/config.json — mode 0600, written atomically with a .bak."""
    return config_dir() / "config.json"


def config_bak_file() -> Path:
    """The .bak repaired from on JSONDecodeError."""
    return config_dir() / "config.json.bak"


def db_file() -> Path:
    """data_dir()/state.db. db.open_rw() REFUSES to open this under a fuse
    mount — SQLite WAL over FUSE loses locking guarantees."""
    return data_dir() / "state.db"


def log_dir() -> Path:
    return _ensure(state_dir() / "logs")


def log_file() -> Path:
    """log_dir()/app.log — rotated 5 MB x 5, redacted."""
    return log_dir() / "app.log"


def bisync_workdir(account_id: str) -> Path:
    """state_dir()/bisync/<account>. NEVER ~/.cache/rclone/bisync — rclone's
    cache cleaning may destroy the .lst files that ARE the sync state."""
    return _ensure(state_dir() / "bisync" / account_id)


def filters_file(account_id: str) -> Path:
    """config_dir()/filters-<account>.txt. A change to this file mandates a
    --resync (invariant I11), which is what the .md5 sidecar detects."""
    return config_dir() / f"filters-{account_id}.txt"


def filters_md5_file(account_id: str) -> Path:
    """The sidecar beside filters_file(): 32 lowercase hex, mode 0600, and NO
    trailing newline."""
    return config_dir() / f"filters-{account_id}.txt.md5"


def run_dir(run_id: str) -> Path:
    """state_dir()/runs/<run_id> — one directory per bisync / verify / pin run."""
    return _ensure(state_dir() / "runs" / run_id)


def run_log_file(run_id: str) -> Path:
    """The --log-file the LogTailer resumes from at runs.log_offset."""
    return run_dir(run_id) / "bisync.jsonl"


def run_meta_file(run_id: str) -> Path:
    return run_dir(run_id) / "meta.json"


def versions_dir(account_id: str) -> Path:
    """state_dir()/versions/<account> — the local --backup-dir side of our own
    version history (the remote side is onedrive:.onedriveui-versions/)."""
    return _ensure(state_dir() / "versions" / account_id)


def vault_dir() -> Path:
    """data_dir()/vault — the gocryptfs CIPHER container. Never the plaintext
    view; that is vault_mountpoint()."""
    return _ensure(data_dir() / "vault")


def vault_mountpoint(sync_root: str | os.PathLike[str] | None = None) -> Path:
    """<sync_root>/Personal Vault — where the decrypted container is mounted.
    Inside the OneDrive tree, exactly as Windows presents it."""
    return mount_point(sync_root) / "Personal Vault"


# ─────────────────────────────────────────────────────────────────────────────
# Runtime (per boot)
# ─────────────────────────────────────────────────────────────────────────────

def endpoints_file() -> Path:
    """runtime_dir()/endpoints.json, mode 0600: the rc ports, credentials, pids,
    starttimes and executeIds. It holds a password — never log it, never bundle
    it into a diagnostics archive."""
    return runtime_dir() / "endpoints.json"


def ui_socket() -> Path:
    """runtime_dir()/ui.sock — the single-instance QLocalServer.

    Always listen on this ABSOLUTE path. The bare QLocalServer.listen("name")
    form lands world-readable in /tmp, where any local user can drive the UI."""
    return runtime_dir() / "ui.sock"


def ui_lock() -> Path:
    """runtime_dir()/ui.lock — the QLockFile guarding single instance."""
    return runtime_dir() / "ui.lock"


def ipc_socket() -> Path:
    """runtime_dir()/ipc.sock, mode 0600 — NDJSON, served to the Nautilus
    extension on a budget of IPC_BUDGET_MS per call."""
    return runtime_dir() / "ipc.sock"


# ─────────────────────────────────────────────────────────────────────────────
# rclone's own files — we read them; rclone owns them
# ─────────────────────────────────────────────────────────────────────────────

def rclone_conf() -> Path:
    """$RCLONE_CONFIG, else ~/.config/rclone/rclone.conf.

    THE only place backend options may live (invariant I1). It holds the OAuth
    refresh token in the clear: never log it, never bundle it (I14)."""
    v = os.environ.get("RCLONE_CONFIG")
    if v:
        return Path(v).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "rclone" / "rclone.conf"


def rclone_cache_dir() -> Path:
    """$RCLONE_CACHE_DIR, else ~/.cache/rclone. READ ONLY — rclone owns it, and
    we point --cache-dir at it so an existing cache is reused rather than
    duplicated. Not created here."""
    v = os.environ.get("RCLONE_CACHE_DIR")
    if v:
        return Path(v).expanduser()
    return _xdg("XDG_CACHE_HOME", ".cache") / "rclone"


def rclone_vfs_dir(remote: str) -> Path:
    """FALLBACK ONLY: <cache>/vfs/<remote>. The authoritative location is
    vfs/stats.diskCache.path (invariant I4) — it alone accounts for the {HASH}
    suffix rclone appends when a backend flag differs, for a remote sub-path,
    and for --cache-dir. Use this only when the daemon cannot be asked."""
    return rclone_cache_dir() / "vfs" / remote


def rclone_vfs_meta_dir(remote: str) -> Path:
    """FALLBACK ONLY: <cache>/vfsMeta/<remote>, the JSON sidecars that are the
    Files-On-Demand ground truth. Authoritative source is
    vfs/stats.diskCache.pathMeta (invariant I4)."""
    return rclone_cache_dir() / "vfsMeta" / remote


# ─────────────────────────────────────────────────────────────────────────────
# Desktop integration
# ─────────────────────────────────────────────────────────────────────────────

def systemd_user_dir() -> Path:
    """~/.config/systemd/user. NOTE: network-online.target does not exist in the
    --user manager; After=/Wants= on it are silently ignored."""
    return _ensure(_xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user", 0o755, tighten=False)


def systemd_unit(name: str) -> Path:
    """The unit file for e.g. 'onedriveui-rcd.service' or the
    'onedriveui-mount@.service' template."""
    return systemd_user_dir() / name


def applications_dir() -> Path:
    """~/.local/share/applications"""
    return _ensure(_xdg("XDG_DATA_HOME", ".local/share") / "applications", 0o755, tighten=False)


def desktop_file() -> Path:
    """~/.local/share/applications/onedriveui.desktop — Categories=Network;FileTransfer;"""
    return applications_dir() / f"{APP_ID}.desktop"


def autostart_dir() -> Path:
    """~/.config/autostart — the XDG autostart method. Never used at the same
    time as the systemd one."""
    return _ensure(_xdg("XDG_CONFIG_HOME", ".config") / "autostart", 0o755, tighten=False)


def autostart_file() -> Path:
    return autostart_dir() / f"{APP_ID}.desktop"


def nautilus_ext_dir() -> Path:
    """~/.local/share/nautilus-python/extensions"""
    return _ensure(
        _xdg("XDG_DATA_HOME", ".local/share") / "nautilus-python" / "extensions", 0o755, tighten=False
    )


def nautilus_ext_file() -> Path:
    return nautilus_ext_dir() / f"{APP_ID}_nautilus.py"


def icon_theme_dir() -> Path:
    """~/.local/share/icons/hicolor"""
    return _ensure(_xdg("XDG_DATA_HOME", ".local/share") / "icons" / "hicolor", 0o755, tighten=False)


def icon_status_dir() -> Path:
    """hicolor/scalable/status — the 10 tray states and the 8 spinner frames.
    QIcon.fromTheme() resolves the names installed here; StatusNotifierItem
    under the GNOME AppIndicator extension cannot take raw pixmaps."""
    return _ensure(icon_theme_dir() / "scalable" / "status", 0o755, tighten=False)


def icon_emblem_dir() -> Path:
    """hicolor/scalable/emblems — Nautilus resolves add_emblem("NAME") through
    emblem-NAME first, so the files here are named emblem-onedriveui-*.svg."""
    return _ensure(icon_theme_dir() / "scalable" / "emblems", 0o755, tighten=False)


def icon_app_dir() -> Path:
    """hicolor/scalable/apps — the application icon itself."""
    return _ensure(icon_theme_dir() / "scalable" / "apps", 0o755, tighten=False)


def gtk_bookmarks() -> list[Path]:
    """Both bookmark files. GTK3 and GTK4 file choosers read different ones, so
    the sidebar entry must be written to both."""
    base = _xdg("XDG_CONFIG_HOME", ".config")
    return [base / "gtk-3.0" / "bookmarks", base / "gtk-4.0" / "bookmarks"]


# ─────────────────────────────────────────────────────────────────────────────
# The sync root
# ─────────────────────────────────────────────────────────────────────────────

def default_sync_root() -> Path:
    """~/OneDrive — the default mountpoint. Not created here: the mount unit
    creates it, and creating it early would mask a stale-mount check."""
    return Path.home() / "OneDrive"


def default_offline_root() -> Path:
    """~/OneDrive-Offline — the optional Topology-B bisync folder. It must be
    DISJOINT from the mount (invariant I2)."""
    return Path.home() / "OneDrive-Offline"


def mount_point(sync_root: str | os.PathLike[str] | None = None) -> Path:
    """The account's mountpoint. sync_root and mountpoint are the same path by
    construction (AccountInfo.sync_root); this resolves ~ and returns the
    default when nothing is configured yet."""
    if sync_root is None or str(sync_root) == "":
        return default_sync_root()
    return Path(os.path.expanduser(str(sync_root)))


# ─────────────────────────────────────────────────────────────────────────────
# FUSE mount enumeration
# ─────────────────────────────────────────────────────────────────────────────

def _unescape_mount_field(field: str) -> str:
    """Undo the octal escaping /proc applies to space (\\040), tab (\\011),
    newline (\\012) and backslash (\\134)."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def fuse_rclone_mounts() -> list[tuple[str, Path]]:
    """[(fs_name, mountpoint)] parsed from /proc/self/mounts where field 3 is
    `fuse.rclone`. This is the ONLY reliable enumeration of rclone mounts —
    `mount/listmounts` is blind to CLI-started mounts, and is banned anyway
    (invariant I7).

    `fs_name` is the raw device field as the kernel recorded it, e.g.
    `onedrive{MxOuf}:` — the {HASH} suffix rclone appends when the mount was
    started with backend flags that differ from rclone.conf. Strip it before
    display; never before comparing.
    """
    out: list[tuple[str, Path]] = []
    try:
        text = _PROC_MOUNTS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] != "fuse.rclone":
            continue
        out.append(
            (_unescape_mount_field(parts[0]), Path(_unescape_mount_field(parts[1])))
        )
    return out


def is_under_fuse_mount(path: str | os.PathLike[str] | Path) -> bool:
    """True if realpath(path) is at or under any fstype `fuse.rclone` mountpoint.
    The basis of invariants I2 and the DB-location check.

    The candidate is resolved first, because ~/Documents may be a KFM symlink
    into the mount. A path that does not exist yet still answers correctly:
    realpath resolves the existing prefix and appends the rest.

    The mountpoints are NOT resolved: the kernel already records them canonical
    in /proc, so resolving them again would only buy an extra lstat() into a
    possibly wedged FUSE filesystem.
    """
    try:
        real = Path(os.path.realpath(os.path.expanduser(str(path))))
    except OSError:
        return False
    for _fs, mountpoint in fuse_rclone_mounts():
        if real == mountpoint or real.is_relative_to(mountpoint):
            return True
    return False
