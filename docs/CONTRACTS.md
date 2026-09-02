# OneDriveUI — FROZEN CONTRACTS

**These files are written once, in WP-00, and are then READ-ONLY for every other work package.**
Everything below is real, importable Python. Implementers `import` from these modules and must not change
them. If a contract genuinely must change, it goes through the WP-00 owner and every dependent package is
notified — it is not edited in place by a consumer.

Ten files are frozen:

| File | Contents |
|---|---|
| `onedriveui/models.py` | every enum and frozen dataclass crossing a module boundary |
| `onedriveui/bus.py` | the single `EventBus` and every cross-module Signal |
| `onedriveui/constants.py` | every hard limit and magic number |
| `onedriveui/errors.py` | the exception hierarchy and the error classifier |
| `onedriveui/strings.py` | every user-visible string |
| `onedriveui/paths.py` | every filesystem path |
| `onedriveui/data/schema.sql` | the DDL (in `ARCHITECTURE.md §10`) |
| `onedriveui/ui/theme.py` | every Fluent token, light and dark |
| `onedriveui/ui/icons.py` | the icon-name registry |
| `onedriveui/__init__.py` | app identity |

§10 then gives the **frozen public signature of every core service class**. Those files are owned by their
work packages, but the signatures below are a contract: a package may add private helpers, never change a
listed signature.

---

## 0. `onedriveui/__init__.py`

```python
"""OneDriveUI — a Windows-11-parity OneDrive client for Linux, on rclone."""

__version__ = "0.1.0"

APP_ID = "onedriveui"                 # Wayland app_id, .desktop basename, desktop-entry hint
APP_NAME = "OneDriveUI"
APP_DISPLAY_NAME = "OneDrive"         # what the user sees; we clone OneDrive's chrome
ORG_NAME = "OneDriveUI"

#: Load-bearing for Microsoft's throttle prioritisation. Format is
#: "ISV|CompanyName|AppName/Version" (or NONISV|...). Do not reformat.
USER_AGENT = f"ISV|OneDriveUI|OneDriveUI/{__version__}"

RCLONE_MIN_VERSION = (1, 75, 0)

__all__ = [
    "__version__", "APP_ID", "APP_NAME", "APP_DISPLAY_NAME",
    "ORG_NAME", "USER_AGENT", "RCLONE_MIN_VERSION",
]
```

---

## 1. `onedriveui/models.py`

Imports only the stdlib. No Qt, no `gi`, no I/O. Every dataclass is `frozen=True, slots=True` so it can be
passed across threads and compared cheaply.

```python
"""FROZEN CONTRACT. Every enum and frozen dataclass that crosses a module boundary.

Rules:
  * stdlib only — no Qt, no gi, no I/O, no logging.
  * every dataclass is frozen and slotted.
  * every timestamp is an RFC3339 UTC string produced by utcnow_iso(), never a
    naive datetime and never a float epoch, because these values round-trip
    through SQLite TEXT columns and rclone's own JSON.
  * every file path that identifies a synced item is `rel_path`: POSIX, relative
    to the account's sync_root, no leading slash. NEVER an inode (rclone inodes
    are unstable across remounts) and never an absolute path.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import StrEnum, IntEnum
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Time
# ─────────────────────────────────────────────────────────────────────────────

def utcnow_iso() -> str:
    """The one timestamp format used everywhere: RFC3339, UTC, second precision."""
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str | None) -> _dt.datetime | None:
    """Parse anything utcnow_iso() or rclone emits (RFC3339, optionally nanosecond)."""
    if not s:
        return None
    t = s.replace("Z", "+00:00")
    if "." in t:                                     # rclone emits RFC3339Nano
        head, _, tail = t.partition(".")
        frac, sign, off = tail.partition("+") if "+" in tail else tail.partition("-")
        t = f"{head}.{frac[:6]}{sign}{off}" if sign else f"{head}.{frac[:6]}"
    try:
        return _dt.datetime.fromisoformat(t)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Core state
# ─────────────────────────────────────────────────────────────────────────────

class SyncState(StrEnum):
    """The 17 states produced by sync.reducer.reduce(). Order here is documentation
    only; the authoritative precedence is reducer.LADDER."""
    NOT_RUNNING     = "not_running"      # not reachable from reduce(); == no tray icon
    INITIALIZING    = "initializing"
    SIGNED_OUT      = "signed_out"
    ACCOUNT_BLOCKED = "account_blocked"
    AUTH_REQUIRED   = "auth_required"
    ERROR           = "error"
    NEEDS_ATTENTION = "needs_attention"
    PAUSED_QUOTA    = "paused_quota"
    PAUSED_MANUAL   = "paused_manual"
    PAUSED_METERED  = "paused_metered"
    PAUSED_BATTERY  = "paused_battery"
    OFFLINE         = "offline"
    MOUNTING        = "mounting"
    SYNCING         = "syncing"
    PROCESSING      = "processing"
    WARNING         = "warning"
    INFO_NOTICE     = "info_notice"
    UP_TO_DATE      = "up_to_date"


SEVERE_STATES: frozenset[SyncState] = frozenset({
    SyncState.ERROR, SyncState.AUTH_REQUIRED, SyncState.ACCOUNT_BLOCKED,
    SyncState.PAUSED_QUOTA, SyncState.NEEDS_ATTENTION,
})
BUSY_STATES: frozenset[SyncState] = frozenset({
    SyncState.SYNCING, SyncState.PROCESSING, SyncState.MOUNTING, SyncState.INITIALIZING,
})
PAUSED_STATES: frozenset[SyncState] = frozenset({
    SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
    SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA,
})


class TrayIcon(StrEnum):
    """The 10 Windows tray states. Values are themed icon NAMES installed into
    ~/.local/share/icons/hicolor/scalable/status/. StatusNotifierItem under the
    GNOME AppIndicator extension cannot reliably take raw pixmaps — always
    QIcon.fromTheme(name)."""
    SYNCED     = "onedriveui-synced"
    SYNCING    = "onedriveui-syncing"      # frame 0; see icons.SPINNER_FRAMES
    PAUSED     = "onedriveui-paused"
    SIGNED_OUT = "onedriveui-signedout"
    ERROR      = "onedriveui-error"
    WARNING    = "onedriveui-warning"
    INFO       = "onedriveui-info"
    BLOCKED    = "onedriveui-blocked"
    SYNCED_BIZ = "onedriveui-synced-business"   # blue cloud for work/school
    NONE       = ""                              # NOT_RUNNING: register no item


class FileState(StrEnum):
    """Per-file Files-On-Demand state. Derived from the vfsMeta sidecar's `Rs`
    ranges plus our own pin table; see rc/vfs.py::classify()."""
    ONLINE_ONLY = "online_only"   # no sidecar, or Rs is null/[]
    PARTIAL     = "partial"       # Rs has >=1 range, sum < Size
    LOCAL       = "local"         # Rs == [{Pos:0, Size:Size}]
    PINNED      = "pinned"        # LOCAL and present in the pins table
    DIRTY       = "dirty"         # sidecar Dirty:true -> un-uploaded local change
    SYNCING     = "syncing"       # currently in transferring[] or vfs/queue
    EXCLUDED    = "excluded"      # filtered out of sync
    ERROR       = "error"         # an open issue names this path
    UNKNOWN     = "unknown"       # not scanned yet; the IPC answers this on timeout


class PauseReason(StrEnum):
    NONE    = "none"
    MANUAL  = "manual"
    METERED = "metered"
    BATTERY = "battery"
    QUOTA   = "quota"


class AccountKind(StrEnum):
    PERSONAL = "personal"
    BUSINESS = "business"


class DaemonHealth(StrEnum):
    DOWN     = "down"
    STARTING = "starting"
    UP       = "up"
    FOREIGN  = "foreign"     # a daemon on our port that failed the ownership proof


class MountHealth(StrEnum):
    DOWN     = "down"
    STARTING = "starting"
    UP       = "up"
    STALE    = "stale"       # /proc line present but statvfs() raises ENOTCONN


class TokenHealth(StrEnum):
    OK              = "ok"
    EXPIRED         = "expired"
    MFA             = "mfa"              # AADSTS50076 — re-auth fixes it
    TENANT_BLOCKED  = "tenant_blocked"   # AADSTS65005 — re-auth will NOT fix it
    UNKNOWN         = "unknown"


class NetworkState(StrEnum):
    ONLINE  = "online"
    METERED = "metered"
    OFFLINE = "offline"


class PowerState(StrEnum):
    NORMAL = "normal"
    SAVER  = "saver"


class BisyncState(StrEnum):
    IDLE          = "idle"
    RUNNING       = "running"
    NEEDS_RESYNC  = "needs_resync"    # .lst missing or .lst-err present
    LOCK_STUCK    = "lock_stuck"
    CRITICAL      = "critical"
    DISABLED      = "disabled"        # offline_folder.enabled is false


class VaultState(StrEnum):
    ABSENT   = "absent"
    LOCKED   = "locked"
    UNLOCKED = "unlocked"
    ERROR    = "error"


# ─────────────────────────────────────────────────────────────────────────────
# Activity, issues, decisions
# ─────────────────────────────────────────────────────────────────────────────

class ActivityVerb(StrEnum):
    UPLOADED   = "uploaded"
    DOWNLOADED = "downloaded"
    MODIFIED   = "modified"
    CREATED    = "created"
    DELETED    = "deleted"
    RENAMED    = "renamed"
    MOVED      = "moved"
    SHARED     = "shared"
    RESTORED   = "restored"
    PINNED     = "pinned"
    FREED      = "freed"


class ActivityState(StrEnum):
    INFLIGHT    = "inflight"
    DONE        = "done"
    ERROR       = "error"
    CANCELLED   = "cancelled"
    INTERRUPTED = "interrupted"   # daemon restarted or job expired: outcome unknown


class IssueSeverity(StrEnum):
    BLOCKING = "blocking"   # ladder rung 5 -> SyncState.ERROR
    ERROR    = "error"      # ladder rung 15 -> SyncState.WARNING
    WARNING  = "warning"    # listed, never changes the state
    INFO     = "info"       # a notice banner only


class IssueCode(StrEnum):
    NAME_INVALID        = "name_invalid"
    RESERVED_NAME       = "reserved_name"
    PATH_TOO_LONG       = "path_too_long"
    FILE_TOO_LARGE      = "file_too_large"
    QUOTA_EXCEEDED      = "quota_exceeded"
    DISK_FULL           = "disk_full"
    AUTH_EXPIRED        = "auth_expired"
    AUTH_MFA            = "auth_mfa"
    AUTH_TENANT_BLOCKED = "auth_tenant_blocked"
    THROTTLED           = "throttled"
    NETWORK_UNREACHABLE = "network_unreachable"
    MALWARE_DETECTED    = "malware_detected"
    FILE_IN_USE         = "file_in_use"
    PERMISSION_LOST     = "permission_lost"
    CONFLICT            = "conflict"
    CASE_COLLISION      = "case_collision"
    MASS_DELETE_BLOCKED = "mass_delete_blocked"
    ALL_FILES_CHANGED   = "all_files_changed"
    CHECK_ACCESS_FAILED = "check_access_failed"
    NEEDS_RESYNC        = "needs_resync"
    BISYNC_LOCK_STUCK   = "bisync_lock_stuck"
    BISYNC_CRITICAL     = "bisync_critical"
    MOUNT_DEAD          = "mount_dead"
    ORPHANED_CACHE      = "orphaned_cache"
    PARTIAL_FILE_FOUND  = "partial_file_found"
    ONENOTE_HIDDEN      = "onenote_hidden"
    VAULT_INACCESSIBLE  = "vault_inaccessible"
    UPLOAD_FAILED       = "upload_failed"
    DOWNLOAD_FAILED     = "download_failed"
    UNKNOWN             = "unknown"


class RecoveryAction(StrEnum):
    RETRY                = "retry"
    RENAME               = "rename"
    SKIP                 = "skip"
    KEEP_BOTH            = "keep_both"
    KEEP_LOCAL           = "keep_local"
    KEEP_CLOUD           = "keep_cloud"
    SIGN_IN              = "sign_in"
    PIN                  = "pin"
    UNPIN                = "unpin"
    FREE_UP_SPACE        = "free_up_space"
    GET_MORE_STORAGE     = "get_more_storage"
    RESYNC               = "resync"
    FORCE_DELETE         = "force_delete"
    RESTORE_FROM_BACKUP  = "restore_from_backup"
    UNLOCK_BISYNC        = "unlock_bisync"
    RESTART_MOUNT        = "restart_mount"
    RECLAIM_CACHE        = "reclaim_cache"
    OPEN_WEB             = "open_web"
    SHOW_IN_FOLDER       = "show_in_folder"
    STOP_SYNCING_ITEM    = "stop_syncing_item"


class DecisionKind(StrEnum):
    MASS_DELETE    = "mass_delete"
    FIRST_DELETE   = "first_delete"
    RESYNC_CONFIRM = "resync_confirm"
    ALL_CHANGED    = "all_changed"
    FORCE_UNLOCK   = "force_unlock"
    KFM_OPTOUT     = "kfm_optout"
    QUOTA_FULL     = "quota_full"


class RunKind(StrEnum):
    BISYNC = "bisync"
    RESYNC = "resync"
    VERIFY = "verify"
    PIN_ALL = "pin_all"
    PRUNE  = "prune"
    KFM    = "kfm"


class RunVerdict(StrEnum):
    OK                   = "ok"                    # "Bisync successful"
    RETRYABLE            = "retryable"             # "Bisync aborted. Please try again."
    NEEDS_RESYNC         = "needs_resync"          # "Must run --resync to recover."
    CRITICAL_SOFT        = "critical_soft"         # "...retryable without --resync due to --resilient"
    ABORTED_MAXDELETE    = "aborted_maxdelete"     # "Safety abort: too many deletes"
    ABORTED_ALLCHANGED   = "aborted_allchanged"    # "Safety abort: all files were changed"
    ACCESS_DENIED        = "access_denied"         # "Access test failed"
    LOCKED               = "locked"                # "prior lock file found"
    CANCELLED            = "cancelled"             # graceful SIGINT that finished cleanly
    UNKNOWN              = "unknown"


class LinkScope(StrEnum):
    ANONYMOUS    = "anonymous"
    ORGANIZATION = "organization"
    USERS        = "users"


class LinkType(StrEnum):
    VIEW  = "view"
    EDIT  = "edit"
    EMBED = "embed"


class ConflictPolicy(StrEnum):
    ASK       = "ask"          # "Let me choose to merge changes or keep both copies"
    KEEP_BOTH = "keep_both"    # "Always keep both copies (rename the copy on this computer)"


class FodMode(StrEnum):
    ON_DEMAND    = "on_demand"      # the "Free up disk space" button
    DOWNLOAD_ALL = "download_all"   # the "Download all files" button


class KfmFolder(StrEnum):
    DESKTOP   = "desktop"
    DOCUMENTS = "documents"
    PICTURES  = "pictures"
    MUSIC     = "music"
    VIDEOS    = "videos"


class ThemeMode(StrEnum):
    SYSTEM = "system"
    LIGHT  = "light"
    DARK   = "dark"


class NotificationId(StrEnum):
    """The 23 catalogued OneDrive toasts plus our additions. The value is the
    `replaces_id` key, so re-notifying the same id updates the bubble in place."""
    SYNC_PAUSED_MANUAL   = "sync_paused_manual"
    SYNC_PAUSED_METERED  = "sync_paused_metered"
    SYNC_PAUSED_BATTERY  = "sync_paused_battery"
    SYNC_RESUMED         = "sync_resumed"
    SYNC_ISSUES          = "sync_issues"
    SIGN_IN_REQUIRED     = "sign_in_required"
    ACCOUNT_BLOCKED      = "account_blocked"
    QUOTA_WARNING        = "quota_warning"
    QUOTA_FULL           = "quota_full"
    LOW_DISK             = "low_disk"
    MASS_DELETE          = "mass_delete"
    FIRST_DELETE         = "first_delete"
    SHARED_WITH_ME       = "shared_with_me"
    SHARED_ITEM_EDITED   = "shared_item_edited"
    CONFLICT_DETECTED    = "conflict_detected"
    FILE_BLOCKED         = "file_blocked"
    NAME_INVALID         = "name_invalid"
    MOUNT_LOST           = "mount_lost"
    MOUNT_RESTORED       = "mount_restored"
    ENGINE_DEAD          = "engine_dead"
    NEEDS_RESYNC         = "needs_resync"
    BACKUP_COMPLETE      = "backup_complete"
    DOWNLOAD_ALL_DONE    = "download_all_done"
    FREE_UP_SPACE_DONE   = "free_up_space_done"
    VAULT_WARNING        = "vault_warning"
    VAULT_LOCKED         = "vault_locked"
    MEMORIES             = "memories"
    OTHER_ACCOUNTS       = "other_accounts"
    SYNC_COMPLETE        = "sync_complete"


class DialogKey(StrEnum):
    """Keys for `dialog_seen` — the 'Don't show this again' opt-outs."""
    FIRST_DELETE          = "first_delete"
    MASS_DELETE_ALWAYS    = "mass_delete_always"
    FOD_FREE_UP_SPACE     = "fod_free_up_space"
    FOD_DOWNLOAD_ALL      = "fod_download_all"
    KFM_RELOGIN_NOTE      = "kfm_relogin_note"
    DU_ON_MOUNT_NOTE      = "du_on_mount_note"
    OOBE_TUTORIAL         = "oobe_tutorial"


# ─────────────────────────────────────────────────────────────────────────────
# Value objects
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AccountInfo:
    id: str
    remote: str                       # rclone remote name, no colon
    kind: AccountKind = AccountKind.PERSONAL
    display_name: str = ""
    email: str = ""
    drive_id: str = ""
    drive_type: str = "personal"
    sync_root: str = ""               # absolute local path == the mountpoint
    enabled: bool = True
    added_at: str = ""
    last_ok_at: str | None = None

    @property
    def fs(self) -> str:
        """The rclone fs string. Always `<remote>:` — a {HASH} suffix here means
        a backend flag leaked onto a command line (invariant I1)."""
        return f"{self.remote}:"


@dataclass(frozen=True, slots=True)
class QuotaInfo:
    total: int = 0
    used: int = 0
    free: int = 0
    trashed: int = 0
    sampled_at: str = ""
    frozen: bool = False

    @property
    def pct(self) -> float:
        return (self.used / self.total * 100.0) if self.total else 0.0

    @property
    def is_full(self) -> bool:
        return self.total > 0 and self.free <= 0

    @property
    def tier(self) -> str:
        """'ok' < 80 % | 'warn' 80-89 | 'critical' 90-99 | 'full' >= 100."""
        p = self.pct
        if self.is_full or p >= 100.0:
            return "full"
        if p >= 90.0:
            return "critical"
        if p >= 80.0:
            return "warn"
        return "ok"


@dataclass(frozen=True, slots=True)
class TransferInfo:
    """One row of core/stats.transferring[]. `group`, `srcFs` and `dstFs` are
    undocumented but present in v1.75.0."""
    name: str
    size: int = 0
    bytes: int = 0
    percentage: int = 0
    speed: float = 0.0
    speed_avg: float = 0.0
    eta: int | None = None            # null when indeterminate
    group: str = ""
    src_fs: str = ""
    dst_fs: str = ""

    @property
    def is_upload(self) -> bool:
        return ":" in self.dst_fs and not self.dst_fs.startswith("/")


@dataclass(frozen=True, slots=True)
class CoreStats:
    """core/stats. NOTE: `transferring`, `checking` and `lastError` are OMITTED
    from the response entirely when empty, `eta` can be null, and `checking` is a
    list of PLAIN STRINGS while `transferring` is a list of dicts. Always build
    this via rc.stats.parse_stats(), never by direct indexing."""
    bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: int | None = None
    errors: int = 0
    last_error: str = ""
    fatal_error: bool = False
    retry_error: bool = False
    checks: int = 0
    total_checks: int = 0
    transfers: int = 0
    total_transfers: int = 0
    deletes: int = 0
    renames: int = 0
    elapsed_time: float = 0.0
    transferring: tuple[TransferInfo, ...] = ()
    checking: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VfsStats:
    """vfs/stats.diskCache. `path` and `path_meta` are the ONLY correct source of
    the cache location (invariant I4) — never hand-derive them."""
    fs: str = ""
    path: str = ""
    path_meta: str = ""
    bytes_used: int = 0
    files: int = 0
    errored_files: int = 0
    uploads_queued: int = 0
    uploads_in_progress: int = 0
    out_of_space: bool = False
    hash_type: int = 0                # 1 = MD5 (local), 4096 = quickxor (OneDrive)
    metadata_dirs: int = 0
    metadata_files: int = 0
    in_use: int = 0


@dataclass(frozen=True, slots=True)
class QueueItem:
    """vfs/queue. `expiry` is seconds and CAN be negative."""
    name: str
    id: int
    size: int = 0
    expiry: float = 0.0
    tries: int = 0
    delay: float = 0.0
    uploading: bool = False


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One parsed vfsMeta sidecar."""
    rel_path: str
    size: int = 0
    bytes_local: int = 0              # sum(r.Size for r in Rs)
    dirty: bool = False
    atime: str | None = None          # rclone's LRU key — NOT filesystem atime
    mtime: str | None = None
    fingerprint: str = ""             # "<size>,<mtime UTC>,<quickxor>"
    state: FileState = FileState.UNKNOWN


@dataclass(frozen=True, slots=True)
class FileStatus:
    """What the Nautilus extension and the file browser render."""
    rel_path: str
    state: FileState = FileState.UNKNOWN
    size: int = 0
    bytes_local: int = 0
    pinned: bool = False
    shared: bool = False
    has_error: bool = False
    excluded: bool = False


@dataclass(frozen=True, slots=True)
class PinRecord:
    account_id: str
    rel_path: str
    mode: str = "pinned"              # pinned | online_only | auto
    is_dir: bool = False
    requested_at: str = ""
    satisfied_at: str | None = None
    bytes_total: int = 0
    bytes_local: int = 0
    last_error: str | None = None
    generation: int = 0


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    id: int = 0
    account_id: str = ""
    rel_path: str = ""
    name: str = ""
    is_dir: bool = False
    verb: ActivityVerb = ActivityVerb.MODIFIED
    direction: str = ""               # up | down | local | remote
    state: ActivityState = ActivityState.DONE
    bytes: int = 0
    size: int = 0
    started_at: str = ""
    completed_at: str | None = None
    error: str | None = None
    error_kind: IssueCode | None = None
    job_group: str = ""
    run_id: str = ""
    dedupe_key: str | None = None

    @property
    def percentage(self) -> int:
        return int(self.bytes / self.size * 100) if self.size else 0


@dataclass(frozen=True, slots=True)
class SyncIssue:
    id: int = 0
    account_id: str = ""
    code: IssueCode = IssueCode.UNKNOWN
    severity: IssueSeverity = IssueSeverity.ERROR
    rel_path: str | None = None
    title: str = ""                   # already user-worded, from strings.py
    detail: str = ""
    raw_error: str = ""               # diagnostics only — NEVER shown raw
    actions: tuple[RecoveryAction, ...] = ()
    first_seen_at: str = ""
    last_seen_at: str = ""
    occurrences: int = 1
    resolved_at: str | None = None
    resolution: str | None = None
    muted: bool = False


@dataclass(frozen=True, slots=True)
class ConflictInfo:
    id: int = 0
    account_id: str = ""
    rel_path: str = ""
    loser_path: str = ""              # e.g. "Report-hostname.docx"
    winner_side: str = ""             # local | remote
    detected_at: str = ""
    run_id: str = ""
    resolved_at: str | None = None
    resolution: str | None = None
    local_size: int = 0
    local_mtime: str = ""
    remote_size: int = 0
    remote_mtime: str = ""


@dataclass(frozen=True, slots=True)
class Decision:
    id: int = 0
    account_id: str = ""
    kind: DecisionKind = DecisionKind.MASS_DELETE
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    expires_at: str | None = None     # expiry means DO NOT DELETE (Microsoft's 7-day rule)
    answered_at: str | None = None
    answer: str | None = None
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str = ""
    account_id: str = ""
    kind: RunKind = RunKind.BISYNC
    argv: tuple[str, ...] = ()
    started_at: str = ""
    ended_at: str | None = None
    exit_code: int | None = None
    verdict: RunVerdict = RunVerdict.UNKNOWN
    log_path: str = ""
    log_offset: int = 0               # the LogTailer resume point
    unit: str = ""
    session: str = ""
    listing1: str = ""
    listing2: str = ""
    files_transferred: int = 0
    bytes: int = 0
    deletes: int = 0
    renames: int = 0
    errors: int = 0
    summary: str = ""


@dataclass(frozen=True, slots=True)
class VersionEntry:
    id: int = 0
    account_id: str = ""
    rel_path: str = ""
    backup_path: str = ""             # local dir, or onedrive:.onedriveui-versions/<ts>/...
    side: str = "remote"              # local | remote
    captured_at: str = ""
    size: int = 0
    quickxor: str = ""
    reason: str = "overwrite"         # overwrite | delete
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class TrashEntry:
    id: int = 0
    account_id: str = ""
    rel_path: str = ""
    trash_path: str = ""              # .onedriveui-trash/<ts>/<rel_path>
    is_dir: bool = False
    size: int = 0
    deleted_at: str = ""
    purge_after: str = ""
    restored_at: str | None = None


@dataclass(frozen=True, slots=True)
class ShareLink:
    id: int = 0
    account_id: str = ""
    rel_path: str = ""
    url: str = ""
    scope: LinkScope = LinkScope.ANONYMOUS
    link_type: LinkType = LinkType.VIEW
    has_password: bool = False
    expires_at: str | None = None
    created_at: str = ""
    revoked_at: str | None = None     # LOCAL BOOKKEEPING ONLY — see ShareService.can_revoke()


@dataclass(frozen=True, slots=True)
class RemoteFolderNode:
    """One row of operations/list. Build UI rows from `name`, NOT from `path`:
    rclone's `Path` is relative to `fs`, not to fs+remote."""
    rel_path: str
    name: str
    is_dir: bool = False
    size: int = -1                    # OneDrive directories report -1
    mod_time: str = ""
    mime_type: str = ""
    item_id: str = ""
    quickxor: str = ""
    created_by: str = ""
    modified_by: str = ""
    malware_detected: bool = False
    children_loaded: bool = False


@dataclass(frozen=True, slots=True)
class JobHandle:
    job_id: int
    execute_id: str                   # per-daemon-process UUID; a change == daemon restarted
    group: str
    path: str                         # the rc endpoint, e.g. "sync/copy"
    label: str = ""
    started_at: str = ""


@dataclass(frozen=True, slots=True)
class NotifySpec:
    """GNOME renders about 3 action buttons; we cap at 2 plus the implicit default."""
    id: NotificationId
    summary: str
    body: str = ""
    actions: tuple[tuple[str, str], ...] = ()   # ((action_id, label), ...)
    urgency: int = 1                            # 0 low, 1 normal, 2 critical — sent as GVariant BYTE 'y'
    timeout_ms: int = -1
    transient: bool = False
    resident: bool = False
    account_id: str = ""


@dataclass(frozen=True, slots=True)
class PauseIntent:
    reason: PauseReason = PauseReason.NONE
    until: str | None = None          # ISO; None with MANUAL == "Until I resume"
    overridden: bool = False          # "Sync Anyway" was pressed for this reason
    set_at: str = ""


@dataclass(frozen=True, slots=True)
class BandwidthState:
    download_kb: int | None = None    # KB/s (1000) as the OneDrive UI shows it
    upload_kb: int | None = None
    upload_auto: bool = False
    auto_percent: int = 70
    measured_capacity_kb: int = 0


@dataclass(frozen=True, slots=True)
class Capabilities:
    """operations/fsinfo. Gate every optional affordance on this, NEVER on a
    backend-name check. `name` is stripped of any {HASH} suffix before display."""
    name: str = ""
    root: str = ""
    precision_ns: int = 0
    hashes: tuple[str, ...] = ()
    features: dict[str, bool] = field(default_factory=dict)

    def has(self, feature: str) -> bool:
        return bool(self.features.get(feature, False))

    @property
    def change_notify(self) -> bool: return self.has("ChangeNotify")
    @property
    def list_r(self) -> bool:        return self.has("ListR")       # False for OneDrive
    @property
    def public_link(self) -> bool:   return self.has("PublicLink")
    @property
    def case_insensitive(self) -> bool: return self.has("CaseInsensitive")


@dataclass(frozen=True, slots=True)
class RcEndpoint:
    kind: str                          # "rcd" | "mount"
    host: str = "127.0.0.1"
    port: int = 0
    user: str = ""
    password: str = ""
    pid: int = 0
    starttime: int = 0                 # /proc/<pid>/stat field 22, against PID reuse
    execute_id: str = ""
    mountpoint: str = ""               # mount endpoints only
    account_id: str = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class DiskCacheInfo:
    """Resolved from vfs/stats. `path`/`path_meta` account for the {HASH} suffix,
    any remote sub-path and --cache-dir. NEVER hand-derive these (invariant I4)."""
    path: str = ""
    path_meta: str = ""
    bytes_used: int = 0
    files: int = 0
    uploads_queued: int = 0
    uploads_in_progress: int = 0
    errored_files: int = 0
    out_of_space: bool = False
    hash_type: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Facts — the sole input to the reducer
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Facts:
    """One immutable observation of the world, rebuilt every tick from the kernel,
    rclone, disk and the session bus. reduce(Facts) -> SyncState is pure.

    Nothing here is remembered: every field is either re-observed or read from a
    persisted table. That is what makes crash recovery exact."""
    account_id: str = ""
    sampled_at: str = ""
    startup_elapsed_s: float = 0.0

    # processes
    daemon_rcd: DaemonHealth = DaemonHealth.DOWN
    daemon_mount: DaemonHealth = DaemonHealth.DOWN
    mount: MountHealth = MountHealth.DOWN
    mount_enabled: bool = True
    execute_id: str = ""
    execute_id_changed: bool = False

    # account
    account_configured: bool = False
    token: TokenHealth = TokenHealth.UNKNOWN
    quota: QuotaInfo = field(default_factory=QuotaInfo)

    # environment
    network: NetworkState = NetworkState.ONLINE
    power: PowerState = PowerState.NORMAL
    consecutive_net_failures: int = 0

    # engine
    transfers_active: int = 0
    checks_active: int = 0
    uploads_queued: int = 0
    uploads_in_progress: int = 0
    errored_files: int = 0
    out_of_space: bool = False
    pin_jobs_active: int = 0
    scan_in_progress: bool = False
    bisync: BisyncState = BisyncState.DISABLED

    # persisted
    issues_blocking: int = 0
    issues_error: int = 0
    issues_warning: int = 0
    pending_decisions: int = 0
    latches: frozenset[str] = frozenset()
    pause: PauseIntent = field(default_factory=PauseIntent)
    policy_pause: PauseReason = PauseReason.NONE
    info_notice: str | None = None

    # bookkeeping
    stale: frozenset[str] = frozenset()   # source names whose value is a carried-over sample
    last_error: str = ""

    @property
    def transferring_count(self) -> int:
        return self.transfers_active + self.uploads_in_progress


@dataclass(frozen=True, slots=True)
class SyncSnapshot:
    """What the UI renders. Produced by the reducer + Debouncer."""
    state: SyncState
    facts: Facts
    headline: str = ""
    subtext: str = ""
    tooltip: str = ""
    tray: TrayIcon = TrayIcon.SYNCED
    progress_pct: int = -1            # -1 == indeterminate / not applicable
    banner_code: IssueCode | None = None
    changed_at: str = ""
```

---

## 2. `onedriveui/bus.py`

```python
"""FROZEN CONTRACT. The one application-wide event bus.

Rules:
  * Every cross-module signal is declared HERE and nowhere else.
  * Nobody subclasses EventBus. Modules emit and connect.
  * Payloads are frozen dataclasses or primitives — never a mutable dict,
    never a QWidget.
  * BUS is created before QApplication and lives on the GUI thread, so a signal
    emitted from a worker is delivered with Qt.QueuedConnection automatically.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from onedriveui.models import (
    AccountInfo, ActivityEvent, ConflictInfo, DaemonHealth, Decision, Facts,
    FileStatus, MountHealth, PauseReason, QuotaInfo, RunRecord, SyncIssue,
    SyncState, VaultState,
)


class EventBus(QObject):
    # ── state ────────────────────────────────────────────────────────────────
    facts_updated           = Signal(Facts)                  # sync/facts.py
    state_changed           = Signal(SyncState, SyncState, Facts)  # old, new, facts

    # ── transfers and activity ───────────────────────────────────────────────
    transfers_updated       = Signal(list)                   # list[TransferInfo]
    activity_appended       = Signal(ActivityEvent)
    activity_updated        = Signal(ActivityEvent)

    # ── quota ────────────────────────────────────────────────────────────────
    quota_updated           = Signal(QuotaInfo)

    # ── issues, conflicts, decisions ─────────────────────────────────────────
    issue_raised            = Signal(SyncIssue)
    issue_resolved          = Signal(int)                    # issue id
    conflict_detected       = Signal(ConflictInfo)
    decision_required       = Signal(Decision)
    decision_answered       = Signal(int, str)               # decision id, answer

    # ── per-file state ───────────────────────────────────────────────────────
    file_state_changed      = Signal(str, str, FileStatus)   # account_id, rel_path, status
    file_states_invalidated = Signal(str, list)              # account_id, list[str]
    pin_progress            = Signal(str, int, int)          # rel_path, done, total

    # ── runs ─────────────────────────────────────────────────────────────────
    run_started             = Signal(RunRecord)
    run_finished            = Signal(RunRecord)

    # ── processes ────────────────────────────────────────────────────────────
    daemon_health           = Signal(str, DaemonHealth)      # "rcd" | "mount", health
    daemon_restarted        = Signal(str, str)               # kind, new execute_id
    mount_health            = Signal(str, MountHealth)       # account_id, health

    # ── accounts and auth ────────────────────────────────────────────────────
    account_added           = Signal(AccountInfo)
    account_updated         = Signal(AccountInfo)
    account_removed         = Signal(str)                    # account_id
    auth_url_ready          = Signal(str)                    # the 127.0.0.1:53682 authUrl
    auth_finished           = Signal(bool, str)              # ok, message

    # ── controls ─────────────────────────────────────────────────────────────
    pause_changed           = Signal(PauseReason, object)    # reason, datetime|None
    bandwidth_changed       = Signal(object)                 # BandwidthState
    config_changed          = Signal(str)                    # dotted key, e.g. "mount.transfers"
    theme_changed           = Signal(bool, str)              # dark, accent hex

    # ── notifications and IPC ────────────────────────────────────────────────
    toast_requested         = Signal(object)                 # NotifySpec
    notification_action     = Signal(str, str)               # toast key, action id
    ipc_action_requested    = Signal(str, list)              # verb, list[abs path]

    # ── misc ─────────────────────────────────────────────────────────────────
    log_line                = Signal(str)
    vault_state_changed     = Signal(VaultState)


#: The singleton. Import this, never construct another EventBus.
BUS = EventBus()
```

---

## 3. `onedriveui/constants.py`

```python
"""FROZEN CONTRACT. Every hard limit and magic number lives here.

Values sourced from Microsoft's published OneDrive restrictions, from rclone
v1.75.0's own behaviour, and from empirical measurement on the target machine.
"""

from __future__ import annotations

# ── OneDrive name and path restrictions ─────────────────────────────────────
INVALID_CHARS = '"*:<>?/\\|'
RESERVED_NAMES: frozenset[str] = frozenset({
    ".lock", "CON", "PRN", "AUX", "NUL", "desktop.ini",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
})
RESERVED_PREFIXES: tuple[str, ...] = ("~$",)
RESERVED_SUBSTRINGS: tuple[str, ...] = ("_vti_",)

MAX_FILE_BYTES = 250 * 1000**3          # 250 GB
MAX_REL_PATH_CHARS = 400                # the part below the sync root
MAX_TOTAL_PATH_CHARS = 520
RECOMMENDED_ITEM_COUNT = 300_000        # per sync instance
NO_THUMBNAIL_ABOVE_BYTES = 100 * 1024**2

# ── rclone / Microsoft Graph ceilings ───────────────────────────────────────
#: Graph requires the resumable-upload chunk size to be a multiple of 320 KiB.
#: This is a hard API requirement, not an rclone preference.
ONEDRIVE_CHUNK_MULTIPLE = 327_680
MAX_TRANSFERS = 4                        # >4 reliably triggers HTTP 429
MAX_CHECKERS = 8
DEFAULT_TPSLIMIT = 8.0                   # 10/s exactly equals 3000 req / 5 min
DEFAULT_TPSLIMIT_BURST = 10              # rclone's default of 1 makes the UI jerky
DEFAULT_RETRIES = 3
DEFAULT_LOW_LEVEL_RETRIES = 10
#: Microsoft's per-user caps, for the About pane and for our own guard rails.
GRAPH_REQ_PER_5MIN = 3_000
GRAPH_INGRESS_PER_HOUR = 50 * 1000**3
GRAPH_EGRESS_PER_HOUR = 100 * 1000**3

# ── rc transport ────────────────────────────────────────────────────────────
#: 5572 and 5573 are ALREADY OCCUPIED on the target machine by the user's own
#: rclone processes. Never bind them; never assume a daemon there is ours.
RC_PORT_RANGE = range(17800, 17900)
RC_FORBIDDEN_PORTS: frozenset[int] = frozenset({5572, 5573, 53682})
RC_TIMEOUT_S = 4.0
RC_JOB_EXPIRE = "10m"                    # the 60 s default GC's job output too soon
RC_JOB_EXPIRE_INTERVAL = "30s"
OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 53682              # rclone's fixed bindPort; check it is free first

# ── ticking and pumping ─────────────────────────────────────────────────────
TICK_IDLE_MS = 2000
TICK_ACTIVE_MS = 400
TICK_PAUSED_MS = 10_000
GLIB_PUMP_MS = 50                        # LOAD-BEARING: all D-Bus rides this pump
SPINNER_FRAME_MS = 125                   # 8 frames == a 1 s rotation
STARTUP_GRACE_S = 8.0
IPC_BUDGET_MS = 20                       # Nautilus calls us on its UI thread
NAUTILUS_IPC_TIMEOUT_MS = 200

# ── hysteresis ──────────────────────────────────────────────────────────────
DEBOUNCE_SEVERE_TICKS = 1
DEBOUNCE_NORMAL_TICKS = 2
DEBOUNCE_IDLE_TICKS = 3                  # UP_TO_DATE needs 3 quiet ticks
PROCESSING_ENTRY_DELAY_MS = 250
MOUNTING_SUPPRESS_S = 15
OFFLINE_FAILURE_THRESHOLD = 3

# ── restart ladders ─────────────────────────────────────────────────────────
MOUNT_RESTART_LADDER_S = (10, 30, 120, 600)
MOUNT_RESTART_MAX_PER_HOUR = 3
RCD_RESTART_LADDER_S = (1, 2, 4, 8, 30)
RCD_MAX_FAILURES = 5
RCD_FAILURE_WINDOW_S = 300

# ── concurrency ─────────────────────────────────────────────────────────────
IO_POOL_THREADS = 4
MAX_CONCURRENT_PINS = 3                  # <= MAX_TRANSFERS
HYDRATE_BLOCK_BYTES = 4 * 1024**2        # sequential reads through FUSE, buffering=0
DB_FLUSH_MS = 100

# ── safety ──────────────────────────────────────────────────────────────────
MASS_DELETE_DEFAULT_THRESHOLD = 200
DECISION_EXPIRY_DAYS = 7                 # expiry means DO NOT DELETE
BISYNC_MAX_LOCK_MIN = 2                  # rclone's hard minimum; smaller is auto-raised
BISYNC_DEFAULT_MAX_DELETE_PCT = 25
TRASH_RETENTION_DAYS_PERSONAL = 30
TRASH_RETENTION_DAYS_BUSINESS = 93
BANDWIDTH_FLOOR_KB = 50                  # matches the Windows UI's minimum
BANDWIDTH_CEIL_KB = 100_000
AUTO_UPLOAD_PERCENT = 70                 # Microsoft pins "Adjust automatically" here
AUTO_UPLOAD_BURST_S = 60

# ── row caps ────────────────────────────────────────────────────────────────
ACTIVITY_CAP_ROWS = 5_000
ISSUE_CAP_ROWS = 5_000
ACTIVITY_UI_ROWS = 50
CACHE_SCAN_INTERVAL_S = 6 * 3600
QUOTA_TTL_S = 300
TOKEN_KEEPALIVE_S = 24 * 3600            # refresh tokens die after 90 days of non-use
VERIFY_INTERVAL_S = 7 * 24 * 3600

# ── units ───────────────────────────────────────────────────────────────────
KB = 1000                                # the OneDrive UI's unit
KIB = 1024                               # rclone's --bwlimit unit. NEVER mix them.

# ── systemd ─────────────────────────────────────────────────────────────────
UNIT_GUI = "onedriveui.service"
UNIT_RCD = "onedriveui-rcd.service"
UNIT_MOUNT_TMPL = "onedriveui-mount@{}.service"
UNIT_BISYNC_TMPL = "onedriveui-bisync-{}"
#: network-online.target DOES NOT EXIST in the systemd --user manager. After= and
#: Wants= on it are silently ignored. Never emit it.
ORDERING_GUI = ("PartOf=graphical-session.target\n"
                "After=graphical-session.target\n")
ORDERING_DAEMON = "After=graphical-session-pre.target\n"

# ── remote emulation paths ──────────────────────────────────────────────────
REMOTE_TRASH_DIR = ".onedriveui-trash"
REMOTE_VERSIONS_DIR = ".onedriveui-versions"

# ── web deep-links ──────────────────────────────────────────────────────────
WEB_ROOT = "https://onedrive.live.com/"
WEB_RECYCLE_BIN = "https://onedrive.live.com/?id=recyclebin"
WEB_RESTORE = "https://onedrive.live.com/RestoreYourOneDrive"
WEB_GET_MORE_STORAGE = "https://www.microsoft.com/microsoft-365/onedrive/compare-onedrive-plans"

# ── Windows 11 metrics reproduced verbatim ──────────────────────────────────
ACTIVITY_CENTER_WIDTH = 360
ACTIVITY_CENTER_HEADER_H = 64
ACTIVITY_CENTER_STORAGE_H = 56
ACTIVITY_CENTER_FOOTER_H = 48
ACTIVITY_ROW_H_2LINE = 56
ACTIVITY_ROW_H_1LINE = 48
SETTINGS_W, SETTINGS_H = 1024, 720
NAV_PANE_W, NAV_PANE_COMPACT_W = 320, 48
NAV_ITEM_H = 36
SETTINGS_CARD_MIN_H = 68
WIZARD_W, WIZARD_H = 500, 350
```

---

## 4. `onedriveui/errors.py`

```python
"""FROZEN CONTRACT. Exception hierarchy plus the ONE place a new error string is
ever taught.

Every error source funnels through classify(): core/transferred[].error,
core/stats.lastError, the rc error envelope, bisync log records, preflight
violations, and health facts.
"""

from __future__ import annotations

import re
from typing import Any

from onedriveui.models import IssueCode, IssueSeverity, RecoveryAction


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class OneDriveUIError(Exception):
    """Base for everything this application raises."""


class RcError(OneDriveUIError):
    """An rc call failed. rclone's error body is a stable 4-key object echoing the
    input: {"error", "input", "path", "status"}; `status` always mirrors HTTP."""

    def __init__(self, path: str, status: int, body: dict[str, Any] | None = None) -> None:
        self.path = path
        self.status = status
        self.body = body or {}
        self.message = str(self.body.get("error", "")) or f"HTTP {status}"
        super().__init__(f"{path}: {self.message}")

    @property
    def is_not_found(self) -> bool:
        return self.status == 404 or "not found" in self.message.lower()

    @property
    def is_job_expired(self) -> bool:
        """Ambiguous on its own — disambiguate against execute_id."""
        return self.status == 500 and "job not found" in self.message.lower()


class DaemonUnavailable(RcError):
    """The daemon did not answer rc/noop within the timeout."""


class DaemonForeign(OneDriveUIError):
    """A live daemon on our port failed the /proc ownership proof. NEVER drive it,
    never core/quit it — rc access is equivalent to shell access as this user."""


class MountLost(OneDriveUIError):
    """The mountpoint is gone or stale (ENOTCONN)."""


class BisyncCritical(OneDriveUIError):
    def __init__(self, verdict: str, log_tail: str = "") -> None:
        self.verdict = verdict
        self.log_tail = log_tail
        super().__init__(f"bisync critical: {verdict}")


class SafetyRefusal(OneDriveUIError):
    """Raised by rc/guards.py. This is ALWAYS a bug in the caller, never a
    user-facing error to be clicked past. It is never caught and swallowed."""

    def __init__(self, invariant: str, detail: str) -> None:
        self.invariant = invariant
        super().__init__(f"[{invariant}] {detail}")


class ConfigError(OneDriveUIError):
    """config.json failed validation in a way that cannot be defaulted around."""


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────

#: Auth failures have no dedicated rclone command; we probe with `about` and
#: match its error text. AADSTS65005 is NOT fixable by re-auth (an admin must
#: claim the domain), which is why it maps to a different code from AADSTS50076.
AUTH_PATTERNS: tuple[tuple[str, IssueCode], ...] = (
    ("aadsts65005",                IssueCode.AUTH_TENANT_BLOCKED),
    ("aadsts50076",                IssueCode.AUTH_MFA),
    ("empty token found",          IssueCode.AUTH_EXPIRED),
    ("invalid_grant",              IssueCode.AUTH_EXPIRED),
    ("couldn't fetch token",       IssueCode.AUTH_EXPIRED),
    ("token has expired",          IssueCode.AUTH_EXPIRED),
    ("failed to get token",        IssueCode.AUTH_EXPIRED),
)

#: Lines that look alarming and are not. NEVER surfaced to the user.
BENIGN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Ignoring --track-renames as it doesn't work with copy or move"),
    re.compile(r"WARNING\s+listing try \d+ failed"),
    re.compile(r"Skipped copy as --dry-run is set"),
    re.compile(r"vfs cache: detected external removal of cache file"),   # that is us, evicting
    re.compile(r"Can't follow symlink without -L/--copy-links"),
    re.compile(r"lock file renewed for"),
)

_RULES: tuple[tuple[re.Pattern[str], IssueCode, IssueSeverity], ...] = (
    (re.compile(r"quotaLimitReached|insufficient storage|507", re.I),
     IssueCode.QUOTA_EXCEEDED,      IssueSeverity.BLOCKING),
    (re.compile(r"no space left on device|ENOSPC", re.I),
     IssueCode.DISK_FULL,           IssueSeverity.BLOCKING),
    (re.compile(r"pathIsTooLong|path.{0,10}too long", re.I),
     IssueCode.PATH_TOO_LONG,       IssueSeverity.ERROR),
    (re.compile(r"nameContainsInvalidCharacters|invalid characters", re.I),
     IssueCode.NAME_INVALID,        IssueSeverity.ERROR),
    (re.compile(r"malware|virus.{0,12}detected", re.I),
     IssueCode.MALWARE_DETECTED,    IssueSeverity.ERROR),
    (re.compile(r"are same name when lowercase|nameAlreadyExists", re.I),
     IssueCode.CASE_COLLISION,      IssueSeverity.ERROR),
    (re.compile(r"accessDenied|403|permission denied|unauthorized", re.I),
     IssueCode.PERMISSION_LOST,     IssueSeverity.ERROR),
    (re.compile(r"429|activityLimitReached|too many requests|Retry-After", re.I),
     IssueCode.THROTTLED,           IssueSeverity.WARNING),
    (re.compile(r"connection refused|connection reset|no such host|network is unreachable"
                r"|i/o timeout|EOF|dial tcp", re.I),
     IssueCode.NETWORK_UNREACHABLE, IssueSeverity.WARNING),
    (re.compile(r"text file busy|resource busy|ETXTBSY|EBUSY", re.I),
     IssueCode.FILE_IN_USE,         IssueSeverity.WARNING),
    (re.compile(r"Safety abort: too many deletes", re.I),
     IssueCode.MASS_DELETE_BLOCKED, IssueSeverity.BLOCKING),
    (re.compile(r"Safety abort: all files were changed", re.I),
     IssueCode.ALL_FILES_CHANGED,   IssueSeverity.BLOCKING),
    (re.compile(r"Access test failed", re.I),
     IssueCode.CHECK_ACCESS_FAILED, IssueSeverity.BLOCKING),
    (re.compile(r"Must run --resync to recover|cannot find prior Path1 or Path2 listings"
                r"|filters file has changed", re.I),
     IssueCode.NEEDS_RESYNC,        IssueSeverity.BLOCKING),
    (re.compile(r"prior lock file found", re.I),
     IssueCode.BISYNC_LOCK_STUCK,   IssueSeverity.BLOCKING),
    (re.compile(r"Transport endpoint is not connected|ENOTCONN", re.I),
     IssueCode.MOUNT_DEAD,          IssueSeverity.BLOCKING),
)

ACTIONS_FOR: dict[IssueCode, tuple[RecoveryAction, ...]] = {
    IssueCode.NAME_INVALID:        (RecoveryAction.RENAME, RecoveryAction.SKIP),
    IssueCode.RESERVED_NAME:       (RecoveryAction.RENAME, RecoveryAction.SKIP),
    IssueCode.PATH_TOO_LONG:       (RecoveryAction.RENAME, RecoveryAction.SHOW_IN_FOLDER),
    IssueCode.FILE_TOO_LARGE:      (RecoveryAction.SKIP, RecoveryAction.SHOW_IN_FOLDER),
    IssueCode.QUOTA_EXCEEDED:      (RecoveryAction.GET_MORE_STORAGE, RecoveryAction.FREE_UP_SPACE),
    IssueCode.DISK_FULL:           (RecoveryAction.FREE_UP_SPACE,),
    IssueCode.AUTH_EXPIRED:        (RecoveryAction.SIGN_IN,),
    IssueCode.AUTH_MFA:            (RecoveryAction.SIGN_IN,),
    IssueCode.AUTH_TENANT_BLOCKED: (RecoveryAction.OPEN_WEB,),
    IssueCode.THROTTLED:           (),
    IssueCode.NETWORK_UNREACHABLE: (RecoveryAction.RETRY,),
    IssueCode.MALWARE_DETECTED:    (RecoveryAction.OPEN_WEB, RecoveryAction.SKIP),
    IssueCode.FILE_IN_USE:         (RecoveryAction.RETRY,),
    IssueCode.PERMISSION_LOST:     (RecoveryAction.STOP_SYNCING_ITEM, RecoveryAction.OPEN_WEB),
    IssueCode.CONFLICT:            (RecoveryAction.KEEP_BOTH, RecoveryAction.KEEP_LOCAL,
                                    RecoveryAction.KEEP_CLOUD),
    IssueCode.CASE_COLLISION:      (RecoveryAction.RENAME,),
    IssueCode.MASS_DELETE_BLOCKED: (RecoveryAction.FORCE_DELETE, RecoveryAction.RESTORE_FROM_BACKUP),
    IssueCode.ALL_FILES_CHANGED:   (RecoveryAction.FORCE_DELETE, RecoveryAction.SKIP),
    IssueCode.CHECK_ACCESS_FAILED: (RecoveryAction.RESYNC, RecoveryAction.SKIP),
    IssueCode.NEEDS_RESYNC:        (RecoveryAction.RESYNC,),
    IssueCode.BISYNC_LOCK_STUCK:   (RecoveryAction.UNLOCK_BISYNC,),
    IssueCode.BISYNC_CRITICAL:     (RecoveryAction.RESYNC,),
    IssueCode.MOUNT_DEAD:          (RecoveryAction.RESTART_MOUNT,),
    IssueCode.ORPHANED_CACHE:      (RecoveryAction.RECLAIM_CACHE,),
    IssueCode.PARTIAL_FILE_FOUND:  (RecoveryAction.SKIP, RecoveryAction.RETRY),
    IssueCode.ONENOTE_HIDDEN:      (RecoveryAction.OPEN_WEB,),
    IssueCode.VAULT_INACCESSIBLE:  (RecoveryAction.OPEN_WEB,),
    IssueCode.UPLOAD_FAILED:       (RecoveryAction.RETRY, RecoveryAction.SKIP),
    IssueCode.DOWNLOAD_FAILED:     (RecoveryAction.RETRY, RecoveryAction.SKIP),
    IssueCode.UNKNOWN:             (RecoveryAction.RETRY, RecoveryAction.SKIP),
}


def is_benign(text: str) -> bool:
    """True for rclone lines that look like errors and are not."""
    return any(p.search(text) for p in BENIGN_PATTERNS)


def classify(
    raw: str,
    status: int | None = None,
    rel_path: str | None = None,
    direction: str = "",
) -> tuple[IssueCode, IssueSeverity, tuple[RecoveryAction, ...]]:
    """Map raw rclone / Graph error text (plus an optional HTTP status) onto an
    IssueCode, a severity and the fix actions to offer.

    Never raises. Unmatched text becomes UPLOAD_FAILED / DOWNLOAD_FAILED when a
    direction is known, else UNKNOWN.
    """
    text = raw or ""
    low = text.lower()

    for needle, code in AUTH_PATTERNS:
        if needle in low:
            return code, IssueSeverity.BLOCKING, ACTIONS_FOR[code]

    if status == 507:
        return (IssueCode.QUOTA_EXCEEDED, IssueSeverity.BLOCKING,
                ACTIONS_FOR[IssueCode.QUOTA_EXCEEDED])
    if status in (429, 503):
        return IssueCode.THROTTLED, IssueSeverity.WARNING, ()

    for pattern, code, sev in _RULES:
        if pattern.search(text):
            return code, sev, ACTIONS_FOR[code]

    if direction == "up":
        return (IssueCode.UPLOAD_FAILED, IssueSeverity.ERROR,
                ACTIONS_FOR[IssueCode.UPLOAD_FAILED])
    if direction == "down":
        return (IssueCode.DOWNLOAD_FAILED, IssueSeverity.ERROR,
                ACTIONS_FOR[IssueCode.DOWNLOAD_FAILED])
    return IssueCode.UNKNOWN, IssueSeverity.ERROR, ACTIONS_FOR[IssueCode.UNKNOWN]


def is_auth_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(n in low for n, _ in AUTH_PATTERNS)


def is_fatal(code: IssueCode) -> bool:
    """HTTP 507 is a FatalError in rclone and 400/pathIsTooLong is a NoRetryError:
    neither is ever retried, so both must be surfaced immediately."""
    return code in (IssueCode.QUOTA_EXCEEDED, IssueCode.PATH_TOO_LONG,
                    IssueCode.AUTH_TENANT_BLOCKED)


def is_transient(code: IssueCode) -> bool:
    return code in (IssueCode.THROTTLED, IssueCode.NETWORK_UNREACHABLE,
                    IssueCode.FILE_IN_USE)
```

---

## 5. `onedriveui/strings.py`

Every user-visible string lives here, keyed. **No literal user-facing text is permitted anywhere else in the
codebase.** Provenance is marked per string: `[verbatim]` = confirmed against Microsoft documentation or a
Microsoft-sourced transcription; `[approx]` = reconstructed; `[ours]` = Linux-specific, no Windows original.

```python
"""FROZEN CONTRACT. Every user-visible string.

Provenance tags in comments:
  [verbatim] confirmed against Microsoft docs / Group Policy strings / MC posts
  [approx]   reconstructed from screenshots or tutorials
  [ours]     Linux-specific; no Windows original exists
"""

from __future__ import annotations

from onedriveui.models import (
    IssueCode, NotificationId, RecoveryAction, SyncState,
)


def t(template: str, **fmt: object) -> str:
    """Format a template. Missing keys render as the literal placeholder rather
    than raising, so a wording bug never crashes the UI."""
    try:
        return template.format(**fmt)
    except (KeyError, IndexError):
        return template


# ─────────────────────────────────────────────────────────────────────────────
# Status lines — the single source for the tray tooltip, the Activity Center
# headline, and the Settings badge. Rendering these three from one table is what
# makes them structurally unable to disagree.
# ─────────────────────────────────────────────────────────────────────────────

STATUS_LINE: dict[SyncState, str] = {
    SyncState.UP_TO_DATE:      "Your files are synced",              # [verbatim]
    SyncState.INFO_NOTICE:     "Your files are synced",              # [verbatim]
    SyncState.SYNCING:         "Syncing {n} files",                  # [verbatim]
    SyncState.PROCESSING:      "Processing changes",                 # [verbatim]
    SyncState.MOUNTING:        "Processing changes",                 # [verbatim]
    SyncState.INITIALIZING:    "Starting OneDrive…",            # [approx]
    SyncState.PAUSED_MANUAL:   "Sync is paused",                     # [verbatim]
    SyncState.PAUSED_METERED:  "Sync is paused",                     # [verbatim]
    SyncState.PAUSED_BATTERY:  "Sync is paused",                     # [verbatim]
    SyncState.PAUSED_QUOTA:    "Your OneDrive is full",              # [verbatim]
    SyncState.SIGNED_OUT:      "You're not signed in",               # [verbatim]
    SyncState.AUTH_REQUIRED:   "Sign in required",                   # [verbatim]
    SyncState.ACCOUNT_BLOCKED: "Your account is blocked",            # [approx]
    SyncState.WARNING:         "Sync issues",                        # [verbatim]
    SyncState.ERROR:           "Action needed",                      # [verbatim]
    SyncState.NEEDS_ATTENTION: "Action needed",                      # [verbatim]
    SyncState.OFFLINE:         "OneDrive isn't connected",           # [approx]
    SyncState.NOT_RUNNING:     "OneDrive isn't running",             # [approx]
}

STATUS_SUB: dict[SyncState, str] = {
    SyncState.SYNCING:         "Uploading {done} of {total} ({bytes} of {size})",  # [approx]
    SyncState.PAUSED_MANUAL:   "Syncing will resume in {hh}h {mm}m",               # [verbatim]
    SyncState.PAUSED_METERED:  "This PC is on a metered network",                  # [approx]
    SyncState.PAUSED_BATTERY:  "This PC is in battery saver mode",                 # [approx]
    SyncState.WARNING:         "{n} files couldn't be synced",                     # [approx]
    SyncState.PAUSED_QUOTA:    "You need more storage to keep syncing",            # [approx]
    SyncState.OFFLINE:         "Your files will sync when you're back online",     # [approx]
    SyncState.UP_TO_DATE:      "{used} of {total} used",                           # [approx]
}

#: Shown while an account performs its very first scan. [verbatim]
FIRST_SYNC_BANNER = (
    "We're checking all your files to make sure they are up to date on this "
    "personal computer. This might take a while if you have a lot of files."
)

# ─────────────────────────────────────────────────────────────────────────────
# Tray menu. The DBusMenu is LABEL-ONLY: QWidgetAction is exported as an empty
# label, so no progress bars or custom widgets may appear here.
# ─────────────────────────────────────────────────────────────────────────────

class MENU:
    OPEN_ACTIVITY   = "Open Activity Center"        # [ours] — first and default item
    OPEN_FOLDER     = "Open your OneDrive folder"   # [verbatim]
    VIEW_ONLINE     = "View online"                 # [verbatim]
    SYNC_PROBLEMS   = "View sync problems ({n})"    # [verbatim]
    PAUSE           = "Pause syncing"               # [verbatim]
    PAUSE_2H        = "2 hours"                     # [verbatim]
    PAUSE_8H        = "8 hours"                     # [verbatim]
    PAUSE_24H       = "24 hours"                    # [verbatim]
    PAUSE_UNTIL     = "Until I resume"              # [ours]
    RESUME          = "Resume syncing"              # [verbatim]
    SETTINGS        = "Settings"                    # [verbatim]
    HELP            = "Help & Settings"             # [verbatim]
    QUIT            = "Quit OneDrive"               # [verbatim]
    RECYCLE_BIN     = "Recycle bin"                 # [verbatim] (MC333940)
    LOCK_VAULT      = "Lock Personal Vault"         # [verbatim]
    UNLOCK_VAULT    = "Unlock Personal Vault"       # [verbatim]


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

class SETTINGS:
    NAV_SYNC          = "Sync and back up"                     # [verbatim]
    NAV_ACCOUNT       = "Account"                              # [verbatim]
    NAV_NOTIFICATIONS = "Notifications"                        # [verbatim]
    NAV_ABOUT         = "About"                                # [verbatim]

    MANAGE_BACKUP     = "Manage folder backup"                 # [verbatim]
    BACKUP_DESC       = "Back up your important PC folders to OneDrive"          # [approx]
    START_AT_SIGNIN   = "Start OneDrive when I sign in to this device"           # [verbatim]
    #: The shipped Windows string contains a grammatical error ("is in on").
    #: Microsoft's own policy documentation uses the corrected form. We ship the
    #: corrected wording and note the discrepancy here rather than cloning a typo.
    PAUSE_METERED     = "Pause syncing when this device is on a metered network" # [verbatim, corrected]
    PAUSE_BATTERY     = "Pause syncing when this device is in battery saver mode"# [verbatim]
    SCREENSHOTS       = "Save screenshots I capture to OneDrive"                 # [verbatim]
    CAMERA_IMPORT     = "Save photos and videos from devices to OneDrive"        # [verbatim]

    ADVANCED          = "Advanced settings"                    # [verbatim]
    FILE_COLLAB       = "File collaboration"                   # [verbatim]
    COLLAB_ASK        = "Let me choose to merge changes or keep both copies"     # [verbatim]
    COLLAB_KEEP_BOTH  = "Always keep both copies (rename the copy on this computer)"  # [verbatim]

    BANDWIDTH         = "Bandwidth"                            # [verbatim]
    LIMIT_DOWNLOAD    = "Limit download rate"                  # [verbatim]
    LIMIT_UPLOAD      = "Limit upload rate"                    # [verbatim]
    LIMIT_TO          = "Limit to"                             # [verbatim]
    ADJUST_AUTO       = "Adjust automatically"                 # [verbatim]
    KB_PER_SEC        = "KB/s"                                 # [verbatim] — 1000, not 1024
    #: rclone's core/bwlimit is in KiB/s. units.kb_to_kib() converts in exactly
    #: one place; the UI never shows KiB.
    BANDWIDTH_GLOBAL_NOTE = "Bandwidth limits apply to all accounts on this device."  # [ours]

    FOD               = "Files On-Demand"                      # [verbatim]
    FREE_UP_SPACE     = "Free up disk space"                   # [verbatim]
    DOWNLOAD_ALL      = "Download all files"                   # [verbatim]
    FOD_DESC          = ("Save space by keeping files online-only until you open them, "
                         "or download everything to this device.")               # [approx]

    EXCLUDED_EXT      = "Excluded file extensions"             # [verbatim]
    CHOOSE_FOLDERS    = "Choose folders"                       # [verbatim]
    UNLINK            = "Unlink this PC"                       # [verbatim]
    ADD_ACCOUNT       = "Add an account"                       # [verbatim]
    GET_MORE_STORAGE  = "Get more storage"                     # [verbatim]
    VAULT_TIMEOUT     = "Lock Personal Vault after"            # [approx]

    # Notifications tab — all five default ON. [verbatim]
    N_PAUSED          = "Notify me when syncing is paused"
    N_SHARED          = "Notify me when others share with me or edit my shared items"
    N_MASS_DELETE     = "Notify me when many files are deleted in the cloud"
    N_MEMORIES        = "Notify me when this day in history memories are available"
    N_OTHER_ACCOUNTS  = "Notify me to load files from my other accounts to this PC"
    N_SYNC_ISSUES     = "Notify me when files can't be synced"                   # [ours]
    N_CONFLICTS       = "Notify me when there's a conflict"                      # [ours]


# ─────────────────────────────────────────────────────────────────────────────
# Dialogs
# ─────────────────────────────────────────────────────────────────────────────

class DIALOG:
    CONTINUE   = "Continue"          # [verbatim]
    OK         = "OK"                # [verbatim]
    CANCEL     = "Cancel"            # [verbatim]
    CLOSE      = "Close"             # [verbatim]
    SAVE       = "Save changes"      # [verbatim]

    FIRST_DELETE_TITLE = "Deleted files are removed everywhere"                  # [verbatim]
    FIRST_DELETE_BODY  = ("You deleted {name} from your OneDrive folder. It will be "
                          "deleted everywhere you're signed in to OneDrive.")    # [approx]
    FIRST_DELETE_OPT   = "Don't show this reminder again"                        # [verbatim]

    MASS_DELETE_TITLE  = "Delete these {n} items?"                               # [verbatim]
    MASS_DELETE_BODY   = ("{n} items were deleted from your OneDrive. If this wasn't "
                          "you, you can restore them.")                          # [approx]
    MASS_DELETE_YES    = "Delete them"                                           # [verbatim]
    MASS_DELETE_NO     = "Restore files"                                         # [verbatim]
    MASS_DELETE_ALWAYS = "Always remove files"                                   # [verbatim]
    #: Governed by the ForcedLocalMassDeleteDetection policy. [verbatim]
    MASS_DELETE_TIMEOUT = ("If you don't confirm this within seven days, the files "
                           "aren't deleted.")

    UNLINK_TITLE = "Unlink account on this PC?"                                  # [verbatim]
    UNLINK_BODY  = ("Any files you have marked as always keep on this device will stay "
                    "on this PC. You will not be able to see other OneDrive files on "
                    "this PC.")                                                  # [verbatim]

    CHOOSE_FOLDERS_ALL  = "Make all files available"                             # [verbatim]
    CHOOSE_FOLDERS_WARN = ("Unchecked folders will be removed from this computer. "
                           "They will still be available online.")               # [approx]

    STOP_BACKUP_TITLE   = "Stop backing up this folder?"                         # [approx]
    STOP_BACKUP_DESKTOP = "This computer only"                                   # [verbatim]
    WHERE_ARE_MY_FILES  = "Where are my files"                                   # [verbatim]

    FREE_UP_TITLE  = "Free up disk space?"                                       # [approx]
    FREE_UP_BODY   = ("Your files will stay in OneDrive and will download when you "
                      "open them. This frees {size} on this PC.")                # [approx]
    DOWNLOAD_ALL_TITLE = "Download all files?"                                   # [approx]
    DOWNLOAD_ALL_BODY  = ("This will use {size} on this PC. You can free up space "
                          "again at any time.")                                  # [approx]

    RESYNC_TITLE = "Reset sync?"                                                 # [ours]
    RESYNC_BODY  = ("Resetting only copies files — it never deletes them. Files you "
                    "deleted while sync was broken may come back, and renamed files "
                    "may appear twice.")                                         # [ours]

    RESET_TITLE = "Reset OneDriveUI?"                                            # [ours]
    RESET_BODY  = ("This clears the local cache and index. Every file on this PC and "
                   "in OneDrive is kept, and so are your settings and folder choices.")  # [ours]

    QUIT_TITLE = "Close OneDrive?"                                               # [verbatim]
    QUIT_BODY  = ("If you close OneDrive, your files will not be kept in sync until you "
                  "open it again.")                                              # [approx]

    #: Shown wherever a control is present but cannot work on Linux.
    UNAVAILABLE_PREFIX = "Not available on Linux: "                              # [ours]
    REMOVE_LINK_WHY = ("rclone cannot revoke a OneDrive sharing link. Use the OneDrive "
                       "website to stop sharing.")                               # [ours]
    VERSION_HISTORY_WHY = ("Version history is stored by OneDrive and can only be "
                           "browsed on the web. OneDriveUI keeps its own snapshots of "
                           "files it replaced.")                                 # [ours]
    RECYCLE_BIN_WHY = ("OneDrive's recycle bin can only be browsed on the web. Files "
                       "you delete in OneDriveUI can be restored here.")         # [ours]
    VAULT_CLOUD_WHY = ("Your cloud Personal Vault can't be opened from Linux. This vault "
                       "encrypts files on this device only.")                    # [ours]
    DU_ON_MOUNT_NOTE = ("Disk-usage tools report the full size of online-only files. "
                        "OneDriveUI's own figures are accurate.")                # [ours]


# ─────────────────────────────────────────────────────────────────────────────
# OOBE — 7 pages, ~500x350
# ─────────────────────────────────────────────────────────────────────────────

class OOBE:
    WELCOME_TITLE  = "Set up OneDrive"                                           # [verbatim]
    WELCOME_BODY   = "Put your files in OneDrive to get them from any device."   # [verbatim]
    SIGNIN_BTN     = "Sign in"                                                   # [verbatim]
    SIGNIN_BROWSER = "Finish signing in in your browser, then come back here."   # [ours]
    FOLDER_TITLE   = "Your OneDrive folder"                                      # [verbatim]
    FOLDER_BODY    = "Add files here so you can get to them from any device."    # [verbatim]
    CHANGE_LOC     = "Change location"                                           # [verbatim]
    USE_LOC        = "Use this location"                                         # [verbatim]
    BACKUP_TITLE   = "Back up folders on this PC"                                # [verbatim]
    DELETE_TITLE   = "Deleting files removes them everywhere"                    # [verbatim]
    TUTORIAL_TITLE = "Get to know your OneDrive"                                 # [verbatim]
    DONE_TITLE     = "Your OneDrive is ready for you"                            # [verbatim]
    OPEN_FOLDER    = "Open my OneDrive folder"                                   # [verbatim]
    NEXT           = "Next"                                                      # [verbatim]
    NOT_NOW        = "Not now"                                                   # [verbatim]


# ─────────────────────────────────────────────────────────────────────────────
# Issues
# ─────────────────────────────────────────────────────────────────────────────

ISSUE_TITLE: dict[IssueCode, str] = {
    IssueCode.NAME_INVALID:        "The file name contains characters that aren't allowed",
    IssueCode.RESERVED_NAME:       "This file name is reserved and can't be uploaded",
    IssueCode.PATH_TOO_LONG:       "The file path is too long",
    IssueCode.FILE_TOO_LARGE:      "This file is larger than OneDrive allows",
    IssueCode.QUOTA_EXCEEDED:      "Your OneDrive is full",
    IssueCode.DISK_FULL:           "There isn't enough space on this PC",
    IssueCode.AUTH_EXPIRED:        "Sign in required",
    IssueCode.AUTH_MFA:            "You need to verify your identity",
    IssueCode.AUTH_TENANT_BLOCKED: "Your organisation must claim this domain",
    IssueCode.THROTTLED:           "OneDrive is busy — retrying",
    IssueCode.NETWORK_UNREACHABLE: "OneDrive isn't connected",
    IssueCode.MALWARE_DETECTED:    "This file was blocked because it may be unsafe",
    IssueCode.FILE_IN_USE:         "The file is open in another program",
    IssueCode.PERMISSION_LOST:     "You no longer have permission to this item",
    IssueCode.CONFLICT:            "Two people edited this file",
    IssueCode.CASE_COLLISION:      "A file with the same name already exists",
    IssueCode.MASS_DELETE_BLOCKED: "Delete these {n} items?",
    IssueCode.ALL_FILES_CHANGED:   "Everything looks different — is this the right folder?",
    IssueCode.CHECK_ACCESS_FAILED: "OneDriveUI couldn't verify both sides",
    IssueCode.NEEDS_RESYNC:        "Sync needs to be reset",
    IssueCode.BISYNC_LOCK_STUCK:   "A previous sync didn't finish",
    IssueCode.BISYNC_CRITICAL:     "Sync stopped and needs attention",
    IssueCode.MOUNT_DEAD:          "Your OneDrive folder isn't available",
    IssueCode.ORPHANED_CACHE:      "Old cached files are using {size}",
    IssueCode.PARTIAL_FILE_FOUND:  "A previous transfer was interrupted",
    IssueCode.ONENOTE_HIDDEN:      "OneNote notebooks aren't synced",
    IssueCode.VAULT_INACCESSIBLE:  "Your cloud Personal Vault can't be opened from Linux",
    IssueCode.UPLOAD_FAILED:       "Couldn't upload this file",
    IssueCode.DOWNLOAD_FAILED:     "Couldn't download this file",
    IssueCode.UNKNOWN:             "This file couldn't be synced",
}

ACTION_LABEL: dict[RecoveryAction, str] = {
    RecoveryAction.RETRY:               "Try again",
    RecoveryAction.RENAME:              "Rename",
    RecoveryAction.SKIP:                "Ignore",
    RecoveryAction.KEEP_BOTH:           "Keep both",
    RecoveryAction.KEEP_LOCAL:          "Keep the version on this PC",
    RecoveryAction.KEEP_CLOUD:          "Keep the version in OneDrive",
    RecoveryAction.SIGN_IN:             "Sign in",
    RecoveryAction.FREE_UP_SPACE:       "Free up space",
    RecoveryAction.GET_MORE_STORAGE:    "Get more storage",
    RecoveryAction.RESYNC:              "Reset sync",
    RecoveryAction.FORCE_DELETE:        "Delete them",
    RecoveryAction.RESTORE_FROM_BACKUP: "Restore files",
    RecoveryAction.UNLOCK_BISYNC:       "Clear and retry",
    RecoveryAction.RESTART_MOUNT:       "Reconnect",
    RecoveryAction.RECLAIM_CACHE:       "Reclaim space",
    RecoveryAction.OPEN_WEB:            "View online",
    RecoveryAction.SHOW_IN_FOLDER:      "Show in folder",
    RecoveryAction.STOP_SYNCING_ITEM:   "Stop syncing this item",
}


# ─────────────────────────────────────────────────────────────────────────────
# Toasts. GNOME renders about 3 action buttons; NotifySpec caps at 2.
# ─────────────────────────────────────────────────────────────────────────────

TOAST: dict[NotificationId, tuple[str, str, tuple[tuple[str, str], ...]]] = {
    NotificationId.SYNC_PAUSED_METERED: (
        "Sync is paused",
        "This PC is on a metered network.",
        (("sync_anyway", "Sync Anyway"),)),                                      # [verbatim]
    NotificationId.SYNC_PAUSED_BATTERY: (
        "Sync is paused",
        "This PC is in battery saver mode.",
        (("sync_anyway", "Sync Anyway"),)),                                      # [verbatim]
    NotificationId.SYNC_PAUSED_MANUAL: (
        "Sync is paused", "Your files are not currently syncing.", ()),          # [verbatim]
    NotificationId.SYNC_ISSUES: (
        "Sync issues",
        "{n} files couldn't be synced.",
        (("view", "View sync problems"),)),                                      # [verbatim]
    NotificationId.MASS_DELETE: (
        "Did you delete {n} files?",
        "{n} files were deleted from your OneDrive.",
        (("restore", "Restore files"), ("delete", "Delete them"))),              # [verbatim]
    NotificationId.QUOTA_FULL: (
        "Your OneDrive is full",
        "You need more storage to keep syncing.",
        (("storage", "Get more storage"),)),                                     # [verbatim]
    NotificationId.QUOTA_WARNING: (
        "You're running out of storage", "{pct}% of your OneDrive is used.",
        (("storage", "Get more storage"),)),                                     # [approx]
    NotificationId.SIGN_IN_REQUIRED: (
        "Sign in required", "OneDrive needs you to sign in again.",
        (("signin", "Sign in"),)),                                               # [verbatim]
    NotificationId.VAULT_WARNING: (
        "Still Using Your Personal Vault?",
        "Your Personal Vault will lock in 5 minutes.",
        (("lock", "Lock Personal Vault"),)),                                     # [verbatim]
    NotificationId.VAULT_LOCKED: (
        "Personal Vault Locked", "Your Personal Vault is now locked.", ()),      # [verbatim]
    NotificationId.CONFLICT_DETECTED: (
        "Two people edited this file",
        "A copy of {name} was saved as {loser}.",
        (("view", "Show in folder"),)),                                          # [approx]
    NotificationId.MOUNT_LOST: (
        "Your OneDrive folder isn't available",
        "OneDriveUI is reconnecting…", ()),                                 # [ours]
    NotificationId.MOUNT_RESTORED: (
        "OneDrive reconnected", "Your files are available again.", ()),          # [ours]
    NotificationId.ENGINE_DEAD: (
        "OneDrive stopped working",
        "OneDriveUI couldn't restart the sync engine.",
        (("report", "Report a problem"),)),                                      # [ours]
    NotificationId.NEEDS_RESYNC: (
        "Sync needs to be reset",
        "OneDriveUI can't continue until sync is reset.",
        (("resync", "Reset sync"),)),                                            # [ours]
    NotificationId.FILE_BLOCKED: (
        "A file was blocked", "{name} may be unsafe and wasn't synced.",
        (("web", "View online"),)),                                              # [approx]
    NotificationId.NAME_INVALID: (
        "Some names can't be synced",
        "{n} files have names that prevent syncing.",
        (("view", "View sync problems"),)),                                      # [verbatim]
    NotificationId.SHARED_WITH_ME: (
        "{who} shared {name} with you", "", (("open", "Open"),)),                # [approx]
    NotificationId.SHARED_ITEM_EDITED: (
        "{who} edited {name}", "", (("open", "Open"),)),                         # [approx]
    NotificationId.DOWNLOAD_ALL_DONE: (
        "All your files are on this PC", "{size} downloaded.", ()),              # [approx]
    NotificationId.FREE_UP_SPACE_DONE: (
        "Space freed", "{size} is now available on this PC.", ()),               # [approx]
    NotificationId.BACKUP_COMPLETE: (
        "Your folders are backed up", "{folders} are now in OneDrive.", ()),     # [approx]
    NotificationId.LOW_DISK: (
        "This PC is running out of space", "{size} free on this device.",
        (("free", "Free up space"),)),                                           # [approx]
    NotificationId.SYNC_COMPLETE: (
        "Your files are synced", "", ()),                                        # [verbatim]
    NotificationId.SYNC_RESUMED: (
        "Sync resumed", "", ()),                                                 # [approx]
    NotificationId.ACCOUNT_BLOCKED: (
        "Your account is blocked", "Contact your administrator.",
        (("web", "View online"),)),                                              # [approx]
    NotificationId.FIRST_DELETE: (
        "Deleted files are removed everywhere",
        "{name} will be deleted everywhere you're signed in.", ()),              # [verbatim]
    NotificationId.MEMORIES: (
        "On this day", "You have memories from {year}.", ()),                    # [approx]
    NotificationId.OTHER_ACCOUNTS: (
        "Load files from your other accounts",
        "You're signed in to another OneDrive account.", ()),                    # [approx]
}


# ─────────────────────────────────────────────────────────────────────────────
# Activity feed verbs
# ─────────────────────────────────────────────────────────────────────────────

VERB_LABEL: dict[str, str] = {
    "uploaded": "Uploaded", "downloaded": "Downloaded", "modified": "Modified",
    "created": "Created",   "deleted": "Deleted",       "renamed": "Renamed",
    "moved": "Moved",       "shared": "Shared",         "restored": "Restored",
    "pinned": "Always kept on this device",
    "freed": "Freed up space",
}

FILE_STATE_LABEL: dict[str, str] = {
    "online_only": "Available when online",   # [verbatim] — the Explorer Status column
    "local":       "Available on this device",# [verbatim]
    "pinned":      "Always available on this device",  # [verbatim]
    "partial":     "Downloading…",
    "dirty":       "Uploading…",
    "syncing":     "Syncing…",
    "excluded":    "Not syncing",
    "error":       "Sync problem",
    "unknown":     "",
}
```

---

## 6. `onedriveui/paths.py`

```python
"""FROZEN CONTRACT. Every filesystem path, in one place.

XDG_CONFIG_HOME, XDG_DATA_HOME and XDG_CACHE_HOME are ALL UNSET on the target
machine — never read them without a fallback. XDG_RUNTIME_DIR is /run/user/1000.
"""

from __future__ import annotations

import os
from pathlib import Path

from onedriveui import APP_ID


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var)
    return Path(v).expanduser() if v else Path.home() / default


def config_dir() -> Path:  ...          # ~/.config/onedriveui           (0700, created)
def data_dir() -> Path:    ...          # ~/.local/share/onedriveui      (0700, created)
def state_dir() -> Path:   ...          # ~/.local/state/onedriveui      (0700, created)
def cache_dir() -> Path:   ...          # ~/.cache/onedriveui            (0700, created)
def runtime_dir() -> Path: ...          # $XDG_RUNTIME_DIR/onedriveui    (0700, created)
                                        # falls back to state_dir()/run when unset

def config_file() -> Path:  ...         # config_dir()/config.json
def db_file() -> Path:      ...         # data_dir()/state.db
def log_dir() -> Path:      ...         # state_dir()/logs
def log_file() -> Path:     ...         # log_dir()/app.log

def bisync_workdir(account_id: str) -> Path: ...   # state_dir()/bisync/<acc>
                                        # NEVER ~/.cache/rclone/bisync — rclone's cache
                                        # cleaning may destroy sync state there.
def filters_file(account_id: str) -> Path: ...     # config_dir()/filters-<acc>.txt
def run_dir(run_id: str) -> Path:          ...     # state_dir()/runs/<run_id>
def versions_dir(account_id: str) -> Path: ...     # state_dir()/versions/<acc>

def endpoints_file() -> Path: ...       # runtime_dir()/endpoints.json  (0600)
def ui_socket() -> Path:      ...       # runtime_dir()/ui.sock  — NEVER the bare
                                        # QLocalServer.listen("name") form, which lands
                                        # world-readable in /tmp.
def ui_lock() -> Path:        ...       # runtime_dir()/ui.lock
def ipc_socket() -> Path:     ...       # runtime_dir()/ipc.sock  (0600)

def rclone_conf() -> Path:  ...         # $RCLONE_CONFIG or ~/.config/rclone/rclone.conf
def rclone_cache_dir() -> Path: ...     # ~/.cache/rclone — READ ONLY; rclone owns it.
                                        # The real VFS paths ALWAYS come from
                                        # vfs/stats.diskCache.path/.pathMeta (invariant I4).

def systemd_user_dir() -> Path:  ...    # ~/.config/systemd/user
def applications_dir() -> Path:  ...    # ~/.local/share/applications
def autostart_dir() -> Path:     ...    # ~/.config/autostart
def nautilus_ext_dir() -> Path:  ...    # ~/.local/share/nautilus-python/extensions
def icon_theme_dir() -> Path:    ...    # ~/.local/share/icons/hicolor
def gtk_bookmarks() -> list[Path]: ...  # [~/.config/gtk-3.0/bookmarks,
                                        #  ~/.config/gtk-4.0/bookmarks]

def default_sync_root() -> Path: ...    # ~/OneDrive


def is_under_fuse_mount(path: Path) -> bool:
    """True if realpath(path) is at or under any fstype `fuse.rclone` mountpoint.
    The basis of invariants I2 and the DB-location check."""
    ...


def fuse_rclone_mounts() -> list[tuple[str, Path]]:
    """[(fs_name, mountpoint)] parsed from /proc/self/mounts where field 3 is
    `fuse.rclone`. This is the ONLY reliable enumeration of rclone mounts —
    `mount/listmounts` is blind to CLI-started mounts."""
    ...
```

---

## 7. `onedriveui/ui/theme.py`

Every value below is transcribed from `microsoft-ui-xaml@main :
controls/dev/CommonStyles/Common_themeresources_any.xaml` (the authoritative Fluent token source —
`generic.xaml` contains only legacy `System*Color` tokens) and from Microsoft Learn's *Typography in Windows*.

Fluent's control fills are **translucent overlays on Mica**, and Wayland cannot do Mica or Acrylic — so the
tables below are **pre-composited to opaque hex** over the two surfaces we actually paint on. These are
pixel-identical to what Windows shows with "Transparency effects" off, which is a shipping, supported
Windows appearance.

```python
"""FROZEN CONTRACT. The complete Fluent design token set.

Qt gotchas baked into this module:
  * QColor("#AARRGGBB") is valid; QColor("#RRGGBBAA") is NOT and silently yields
    the wrong colour. Every literal here is opaque #RRGGBB or Qt-order #AARRGGBB.
  * Alpha does not compose predictably across separate QSS rules, which is why
    every fill token is pre-composited.
  * QSS silently ignores: box-shadow, transition, transform, opacity, filter,
    backdrop-filter, text-overflow, z-index, cursor, :not(), CSS variables,
    calc(), rem/em, and linear-gradient() (Qt's is qlineargradient).
  * `QPushButton { background: X }` with NO border declaration renders the Fusion
    GRADIENT, not a flat fill. Always declare a border.
  * A Python SUBCLASS of QWidget ignores QSS backgrounds without
    WA_StyledBackground. Derive containers from QFrame instead.
  * A bare `QWidget { ... }` selector cascades to EVERY descendant. Always scope.
"""

from __future__ import annotations

from typing import Literal

Surface = Literal["base", "layer"]

# ═════════════════════════════════════════════════════════════════════════════
# 1. Base surfaces (opaque, no alpha) — the Mica substitutes
# ═════════════════════════════════════════════════════════════════════════════

BASE_LIGHT  = "#F3F3F3"   # SolidBackgroundFillColorBase   — window background
BASE_DARK   = "#202020"
LAYER_LIGHT = "#FFFFFF"   # SolidBackgroundFillColorQuarternary — flyout / card surface
LAYER_DARK  = "#2C2C2C"

# ═════════════════════════════════════════════════════════════════════════════
# 2. Tokens, pre-composited. Key -> (light_on_base, light_on_layer,
#                                    dark_on_base,  dark_on_layer)
# ═════════════════════════════════════════════════════════════════════════════

_COMPOSITED: dict[str, tuple[str, str, str, str]] = {
    # ── text ───────────────────────────────────────────────────────────────
    # NOTE: TextFillColorPrimary light is #E4000000 (89% black) and flattens to
    # #1A1A1A. Painting pure black text is the fastest way to look wrong.
    "TextFillColorPrimary":            ("#1A1A1A", "#1B1B1B", "#FFFFFF", "#FFFFFF"),
    "TextFillColorSecondary":          ("#5C5C5C", "#616161", "#CCCCCC", "#CFCFCF"),
    "TextFillColorTertiary":           ("#868686", "#8D8D8D", "#969696", "#9C9C9C"),
    "TextFillColorDisabled":           ("#9B9B9B", "#A3A3A3", "#717171", "#797979"),
    "TextFillColorInverse":            ("#FFFFFF", "#FFFFFF", "#1A1A1A", "#1B1B1B"),

    # ── control fills ──────────────────────────────────────────────────────
    "ControlFillColorDefault":         ("#FBFBFB", "#FFFFFF", "#2D2D2D", "#383838"),
    "ControlFillColorSecondary":       ("#F6F6F6", "#FCFCFC", "#323232", "#3D3D3D"),
    "ControlFillColorTertiary":        ("#F5F5F5", "#FDFDFD", "#272727", "#333333"),
    "ControlFillColorDisabled":        ("#F5F5F5", "#FDFDFD", "#2A2A2A", "#353535"),
    "ControlFillColorInputActive":     ("#FFFFFF", "#FFFFFF", "#1E1E1E", "#1E1E1E"),
    "ControlSolidFillColorDefault":    ("#FFFFFF", "#FFFFFF", "#454545", "#454545"),
    "ControlStrongFillColorDefault":   ("#868686", "#8D8D8D", "#9A9A9A", "#9F9F9F"),
    "ControlStrongFillColorDisabled":  ("#A6A6A6", "#AEAEAE", "#575757", "#606060"),

    # ── subtle / alt fills (hover and press states) ────────────────────────
    "SubtleFillColorSecondary":        ("#EAEAEA", "#F6F6F6", "#2D2D2D", "#383838"),
    "SubtleFillColorTertiary":         ("#EDEDED", "#F9F9F9", "#292929", "#343434"),
    "ControlAltFillColorSecondary":    ("#EDEDED", "#F9F9F9", "#1D1D1D", "#282828"),
    "ControlAltFillColorTertiary":     ("#E5E5E5", "#F0F0F0", "#2A2A2A", "#353535"),
    "ControlAltFillColorQuarternary":  ("#DCDCDC", "#E7E7E7", "#303030", "#3B3B3B"),

    # ── strokes ────────────────────────────────────────────────────────────
    "ControlStrokeColorDefault":       ("#E5E5E5", "#F0F0F0", "#303030", "#3B3B3B"),
    # The Fluent "1 px bottom stroke": light theme puts the DARKER secondary
    # stroke on the BOTTOM (the gradient carries ScaleY=-1); dark theme puts the
    # BRIGHTER one on the TOP. In Qt this is just border-bottom-color /
    # border-top-color.
    "ControlStrokeColorSecondary":     ("#CCCCCC", "#D6D6D6", "#353535", "#404040"),
    "CardStrokeColorDefault":          ("#E5E5E5", "#F0F0F0", "#1D1D1D", "#282828"),
    "DividerStrokeColorDefault":       ("#E5E5E5", "#F0F0F0", "#323232", "#3D3D3D"),
    "ControlStrongStrokeColorDefault": ("#868686", "#8D8D8D", "#9A9A9A", "#9F9F9F"),
    "SurfaceStrokeColorFlyout":        ("#E5E5E5", "#F0F0F0", "#1A1A1A", "#232323"),
    "SurfaceStrokeColorDefault":       ("#C1C1C1", "#C8C8C8", "#424242", "#494949"),

    # ── cards and layers ───────────────────────────────────────────────────
    "CardBackgroundFillColorDefault":  ("#FBFBFB", "#FFFFFF", "#2B2B2B", "#373737"),
    "CardBackgroundFillColorSecondary":("#F5F5F5", "#FAFAFA", "#272727", "#333333"),
    "LayerFillColorDefault":           ("#F9F9F9", "#FFFFFF", "#3A3A3A", "#3A3A3A"),
    "SmokeFillColorDefault":           ("#AAAAAA", "#B2B2B2", "#161616", "#1F1F1F"),

    # ── solid backgrounds ──────────────────────────────────────────────────
    "SolidBackgroundFillColorBase":    ("#F3F3F3", "#F3F3F3", "#202020", "#202020"),
    "SolidBackgroundFillColorBaseAlt": ("#DADADA", "#DADADA", "#0A0A0A", "#0A0A0A"),
    "SolidBackgroundFillColorSecondary":("#EEEEEE", "#EEEEEE", "#1C1C1C", "#1C1C1C"),
    "SolidBackgroundFillColorTertiary":("#F9F9F9", "#F9F9F9", "#282828", "#282828"),
    "SolidBackgroundFillColorQuarternary":("#FFFFFF", "#FFFFFF", "#2C2C2C", "#2C2C2C"),

    # ── focus ring (two-tone, NO accent) ───────────────────────────────────
    "FocusStrokeColorOuter":           ("#1A1A1A", "#1B1B1B", "#FFFFFF", "#FFFFFF"),
    "FocusStrokeColorInner":           ("#FFFFFF", "#FFFFFF", "#000000", "#000000"),

    # ── status (already opaque in the source) ──────────────────────────────
    "SystemFillColorSuccess":          ("#0F7B0F", "#0F7B0F", "#6CCB5F", "#6CCB5F"),
    "SystemFillColorSuccessBackground":("#DFF6DD", "#DFF6DD", "#393D1B", "#393D1B"),
    "SystemFillColorCaution":          ("#9D5D00", "#9D5D00", "#FCE100", "#FCE100"),
    "SystemFillColorCautionBackground":("#FFF4CE", "#FFF4CE", "#433519", "#433519"),
    # NOTE: SystemErrorTextColor (#C50500/#FFF000) in generic.xaml is a legacy
    # Windows 8 token. The CURRENT error colour is SystemFillColorCritical.
    "SystemFillColorCritical":         ("#C42B1C", "#C42B1C", "#FF99A4", "#FF99A4"),
    "SystemFillColorCriticalBackground":("#FDE7E9", "#FDE7E9", "#442726", "#442726"),
    "SystemFillColorNeutral":          ("#868686", "#8D8D8D", "#8B8B8B", "#909090"),
    "SystemFillColorSolidNeutral":     ("#8A8A8A", "#8A8A8A", "#9D9D9D", "#9D9D9D"),
    "SystemFillColorSolidAttentionBackground":("#F7F7F7", "#F7F7F7", "#2E2E2E", "#2E2E2E"),
}

# ═════════════════════════════════════════════════════════════════════════════
# 3. Accent
# ═════════════════════════════════════════════════════════════════════════════

#: Windows 11's default system accent ramp, verified.
ACCENT_RAMP_SYSTEM: dict[str, str] = {
    "Light3": "#99EBFF", "Light2": "#4CC2FF", "Light1": "#0091F8",
    "Base":   "#0078D4",
    "Dark1":  "#0067C0", "Dark2":  "#003E92", "Dark3":  "#001A68",
}

#: The OneDrive brand blue (#0364B8) expanded into an equivalent 7-stop ramp by
#: measuring the per-stop HSL delta of the system ramp and reapplying it. The
#: transform round-trips the system ramp exactly, which validates it.
ACCENT_RAMP_ONEDRIVE: dict[str, str] = {
    "Light3": "#82E1FD", "Light2": "#36B2FC", "Light1": "#047BDB",
    "Base":   "#0364B8",
    "Dark1":  "#0355A4", "Dark2":  "#023077", "Dark3":  "#01124E",
}

#: WinUI picks a DIFFERENT ramp stop per theme:
#:   AccentFillColorDefaultBrush = SystemAccentColorDark1  in LIGHT
#:                               = SystemAccentColorLight2 in DARK
#: Using the base #0364B8 in both themes is wrong in BOTH.
#: Hover = the same colour at 90 % opacity; pressed = 80 %. Pre-composited here.
ACCENT_ONEDRIVE = {
    "light": {"rest": "#0355A4", "hover": "#1B65AC", "pressed": "#3375B4",
              "disabled": "#BFBFBF", "text": "#FFFFFF"},
    # Text on accent is BLACK in dark theme, because the dark accent is a light
    # blue. Hardcoding white text on accent buttons breaks dark-mode contrast.
    "dark":  {"rest": "#36B2FC", "hover": "#34A3E6", "pressed": "#3295D0",
              "disabled": "#434343", "text": "#000000"},
}

#: The OneDrive logo is a FOUR-FLAT-SHAPE construction, not a gradient. viewBox
#: "0 5.5 32 20.5" — it is WIDER THAN TALL and must never be stretched to square.
LOGO_COLORS = {"rear_top": "#0364B8", "left": "#0078D4",
               "right": "#1490DF", "front": "#28A8EA"}
LOGO_VIEWBOX = (0.0, 5.5, 32.0, 20.5)

# ═════════════════════════════════════════════════════════════════════════════
# 4. Geometry
# ═════════════════════════════════════════════════════════════════════════════

RADII = {
    "control": 4,        # ControlCornerRadius
    "overlay": 8,        # OverlayCornerRadius — flyouts, dialogs, menus
    "toggle_track": 10,  # the 20 px-tall pill
    "progress_fill": 1.5,
    "progress_track": 0.5,
    "hover_pill": 4,
    "selection_indicator": 2,
}

SPACING = {"xxs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 20, "xxl": 24, "xxxl": 32}

METRICS = {
    # Button: ButtonPadding 11,5,11,6 + 1 px border. `padding: 5px 11px` with
    # `min-height: 20px` measures EXACTLY QSize(55, 32). Omitting min-height
    # gives 33 px.
    "button_h": 32, "button_pad_h": 11, "button_pad_v": 5, "button_min_h": 20,
    # TextBox: min height 32, padding 10,5,6,6, border 1 -> focused 1,1,1,2.
    # The focused bottom border grows 1->2 px, so padding-bottom must drop by 1
    # or the control jumps 32 -> 33 px on focus.
    "textbox_h": 32, "textbox_pad_l": 10, "textbox_pad_b": 6, "textbox_pad_b_focus": 5,
    # Windows 11 ToggleSwitch — the WINUI2 template, NOT the legacy Windows 10
    # 44x20/10 px one that ships in microsoft-ui-xaml@main.
    "toggle_track_w": 40, "toggle_track_h": 20,
    "toggle_knob": 12, "toggle_knob_box": 20, "toggle_travel": 20,
    "toggle_knob_hover": 14, "toggle_knob_press_w": 17, "toggle_knob_press_h": 14,
    # ProgressBar: the TRACK (1 px) is THINNER than the FILL (3 px). Intentional.
    "progress_fill_h": 3, "progress_track_h": 1,
    "ring_stroke": 4,
    # SettingsCard / SettingsExpander (CommunityToolkit, verbatim)
    "card_min_h": 68, "card_pad": 16, "card_icon": 20, "card_icon_gap": 20,
    "card_desc_size": 12, "card_content_min_w": 120, "card_wrap_threshold": 476,
    "expander_header_pad": (16, 16, 4, 16), "expander_child_pad": (58, 8, 44, 8),
    "expander_chevron": 32,
    # NavigationView
    "nav_open_w": 320, "nav_compact_w": 48, "nav_item_h": 36,
    "nav_item_margin": (4, 2), "nav_icon_box": 40, "nav_glyph": 16,
    "nav_indicator_w": 3, "nav_indicator_h": 16, "nav_toggle": (40, 36),
    # Flyout (FlyoutContentPadding 16,15,16,17)
    "flyout_pad": (16, 15, 16, 17), "flyout_min_w": 96, "flyout_max_w": 456,
    # Focus ring: 2 px outer + 1 px inner, inflated 3 px outside the control,
    # ring radius = control radius + 3. It carries NO accent colour.
    "focus_outer": 2, "focus_inner": 1, "focus_inflate": 3,
    # Activity Center
    "ac_width": 360, "ac_header_h": 64, "ac_storage_h": 56, "ac_footer_h": 48,
    "ac_row_h_2line": 56, "ac_row_h_1line": 48, "ac_inset": 16,
    "ac_bar_w": 328, "ac_bar_h": 4,
    # Badges
    "tray_badge": 10, "tray_badge_ring": 1,
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. Typography — Windows 11 ramp, in PIXELS. Only 400 and 600 are ever used;
#    never Bold(700), never italic. Sentence case everywhere.
# ═════════════════════════════════════════════════════════════════════════════

TYPE: dict[str, tuple[int, int, int]] = {   # name -> (px, line_height, weight)
    "caption":           (12, 16, 400),
    "body":              (14, 20, 400),
    "body_strong":       (14, 20, 600),
    "body_large":        (18, 24, 400),
    "body_large_strong": (18, 24, 600),
    "subtitle":          (20, 28, 600),
    "title":             (28, 36, 600),
    "title_large":       (40, 52, 600),
    "display":           (68, 92, 600),
}

#: Segoe UI (Variable) is proprietary and must not be redistributed. Inter is
#: SIL OFL-1.1 and explicitly permits bundling. Selawik (Microsoft's own OFL
#: face) is metrically compatible with Segoe UI and is the first choice when it
#: is vendored.
#: WARNING: fontconfig SUBSTITUTES every unknown family, so
#: QFont.setFamilies([...]) does NOT walk to the first installed family —
#: ui/fonts.py must filter candidates against QFontDatabase.families() itself.
FONT_CANDIDATES: tuple[str, ...] = ("Selawik", "Inter", "Adwaita Sans", "Noto Sans", "Cantarell")

#: Noto Sans at 14 px measures lineSpacing 19.0, not the ramp's 20 — so line
#: heights must be set explicitly (QTextBlockFormat.setLineHeight(20, FixedHeight)
#: or a fixed widget height), regardless of which face resolves.
FALLBACK_LINE_HEIGHT_DELTA = 1

# ═════════════════════════════════════════════════════════════════════════════
# 6. Motion. Every duration passes through duration(), which returns 0 when
#    animations are disabled — and BOTH gtk-enable-animations and
#    org.gnome.desktop.interface enable-animations are FALSE on this machine.
#    Qt's animation timer is 60 Hz and does not sync to a 144/180 Hz display.
# ═════════════════════════════════════════════════════════════════════════════

DURATION = {
    "faster": 83,     # ControlFasterAnimationDuration — toggle knob
    "fast":   167,    # ControlFastAnimationDuration
    "normal": 250,    # ControlNormalAnimationDuration
    "slow":   350,
    "flyout": 150,    # Activity Center open: fade + 16 px rise
}

#: Fluent's standard curve is KeySpline (0,0,0,1). Reproduced exactly in Qt with
#: QEasingCurve(BezierSpline).addCubicBezierSegment(QPointF(0,0), QPointF(0,1),
#: QPointF(1,1)) — verified: valueForProgress(0.5) == 0.8899. OutCubic is close
#: but NOT identical; use the explicit bezier.
CURVES = {
    "decelerate":     ((0.0, 0.0), (0.0, 1.0)),      # curveDecelerateMid
    "easy_ease":      ((0.33, 0.0), (0.67, 1.0)),
    "accelerate":     ((0.8, 0.0), (0.78, 1.0)),     # curveAccelerateMin
    "point_to_point": ((0.55, 0.55), (0.0, 1.0)),
}

# ═════════════════════════════════════════════════════════════════════════════
# 7. Elevation. QSS has NO box-shadow. QGraphicsDropShadowEffect's blur radius
#    is roughly 2x the CSS blur (Qt's is the kernel diameter, CSS's is ~2 sigma),
#    it paints INSIDE the widget's own bounds (so a popup must reserve
#    blurRadius of layout margin on every side), and it is EXCLUSIVE — one
#    QGraphicsEffect per widget, so a shadow and an opacity effect cannot
#    coexist. Also add Qt.NoDropShadowWindowHint or the compositor adds a
#    second shadow.
# ═════════════════════════════════════════════════════════════════════════════

SHADOWS = {                       # (qt_blur_radius, dy, alpha_light, alpha_dark)
    "card":   (8,   2,  31, 61),
    "flyout": (32,  8,  36, 71),   # Fluent shadow16
    "dialog": (128, 32, 51, 122),  # Fluent shadow64
}


# ═════════════════════════════════════════════════════════════════════════════
# 8. Public API
# ═════════════════════════════════════════════════════════════════════════════

def T(token: str, *, dark: bool | None = None, on: Surface = "base") -> str:
    """Resolve a Fluent token to an opaque '#RRGGBB' for the current theme.

    `on` selects which surface the (originally translucent) token is composited
    over: "base" for the window background, "layer" for a card or flyout.
    `dark=None` means "ask the live ThemeManager".
    Raises KeyError for an unknown token — a typo must fail loudly, not paint black.
    """
    ...


def accent(role: str = "rest", *, dark: bool | None = None) -> str:
    """role in {"rest","hover","pressed","disabled","text"}."""
    ...


def base(*, dark: bool | None = None) -> str: ...     # the window background
def layer(*, dark: bool | None = None) -> str: ...    # the card / flyout surface


def font_px(role: str) -> int: ...
def line_height(role: str) -> int: ...
def weight(role: str) -> int: ...


def duration(name_or_ms: str | int) -> int:
    """Return the duration in ms, or 0 when animations are disabled."""
    ...


def curve(name: str):
    """-> QEasingCurve built from CURVES[name] as an explicit BezierSpline."""
    ...


def stylesheet(*, dark: bool | None = None) -> str:
    """The complete application QSS for the given theme. Built once per theme
    change; re-applying it is an expensive full re-polish, so ThemeManager
    debounces (the XDG portal emits color-scheme SettingChanged TWICE per change)."""
    ...


class ThemeManager:
    """The theme source of truth.

    QStyleHints.colorScheme() is driven ENTIRELY by ~/.config/gtk-{3,4}.0/settings.ini
    `gtk-application-prefer-dark-theme` and ignores org.freedesktop.appearance;
    on this machine a stale `=true` makes Qt report Dark even when GNOME is light,
    and colorSchemeChanged NEVER fires. DO NOT USE IT.

    QPalette.Accent is a hard-coded #308cc6 and is NOT the system accent.

    The XDG portal is reliable. It is read via Gio because PySide6's QDBusArgument
    cannot demarshal the accent-colour `(ddd)` struct.
    """

    def start(self) -> None: ...
    def is_dark(self) -> bool: ...
    def accent_hex(self) -> str: ...
    def animations_enabled(self) -> bool: ...
    def apply(self, app) -> None: ...          # sets Fusion + the stylesheet
    # emits BUS.theme_changed(dark: bool, accent: str), debounced
```

---

## 8. `onedriveui/ui/icons.py`

```python
"""FROZEN CONTRACT. The icon-name registry.

Segoe Fluent Icons is not licensed for Linux redistribution, so the glyph
codepoints from Windows documentation are unusable. We ship Fluent UI System
Icons (github.com/microsoft/fluentui-system-icons, MIT) at their NATIVE sizes —
never scale a 24 px glyph to 16 px or the stroke weight is wrong.

Tray icons and file-manager emblems are installed as FILES into
~/.local/share/icons/hicolor/... and referenced by NAME, because:
  * StatusNotifierItem under the GNOME AppIndicator extension cannot reliably
    take raw pixmaps — it transmits an IconName.
  * Nautilus 50 SILENTLY DROPS any emblem missing from the active icon theme
    (the theme here is breeze-dark, which lacks emblem-synchronizing and
    emblem-default), logging only a stderr WARNING. Shipping our own into
    hicolor and running gtk4-update-icon-cache is mandatory.
"""

from __future__ import annotations

from onedriveui.models import FileState, SyncState, TrayIcon

# ═════════════════════════════════════════════════════════════════════════════
# Tray — hicolor/scalable/status/, plus 16/22/24/32/48 px PNG fallbacks.
# Include 22 and 24 explicitly for GNOME's appindicator.
# ═════════════════════════════════════════════════════════════════════════════

TRAY_ICON_NAMES: tuple[str, ...] = (
    "onedriveui-synced",              # plain white cloud (personal)
    "onedriveui-synced-business",     # plain blue cloud (work/school)
    "onedriveui-syncing",             # frame 0 of the spinner
    "onedriveui-paused",              # cloud + pause badge
    "onedriveui-signedout",           # GREY cloud with a diagonal line
    "onedriveui-error",               # red circle + white cross
    "onedriveui-warning",             # yellow triangle
    "onedriveui-info",                # blue circle with 'i'
    "onedriveui-blocked",             # red 'no entry' circle
    "onedriveui-processing",          # reserved; currently aliases -syncing
)

#: 8 frames == a 1 s rotation at SPINNER_FRAME_MS (125 ms). SNI has NO animation
#: support, so the spinner is a QTimer swapping these names via setIcon().
SPINNER_FRAMES: tuple[str, ...] = tuple(f"onedriveui-syncing-{i}" for i in range(1, 9))

TRAY_FOR_STATE: dict[SyncState, TrayIcon] = {
    SyncState.UP_TO_DATE:      TrayIcon.SYNCED,
    SyncState.INFO_NOTICE:     TrayIcon.INFO,
    SyncState.SYNCING:         TrayIcon.SYNCING,
    SyncState.PROCESSING:      TrayIcon.SYNCING,
    SyncState.MOUNTING:        TrayIcon.SYNCING,
    SyncState.INITIALIZING:    TrayIcon.SYNCING,
    SyncState.PAUSED_MANUAL:   TrayIcon.PAUSED,
    SyncState.PAUSED_METERED:  TrayIcon.PAUSED,
    SyncState.PAUSED_BATTERY:  TrayIcon.PAUSED,
    SyncState.PAUSED_QUOTA:    TrayIcon.WARNING,
    SyncState.SIGNED_OUT:      TrayIcon.SIGNED_OUT,
    SyncState.AUTH_REQUIRED:   TrayIcon.BLOCKED,
    SyncState.ACCOUNT_BLOCKED: TrayIcon.BLOCKED,
    SyncState.ERROR:           TrayIcon.ERROR,
    SyncState.WARNING:         TrayIcon.WARNING,
    SyncState.NEEDS_ATTENTION: TrayIcon.INFO,
    SyncState.OFFLINE:         TrayIcon.INFO,
    SyncState.NOT_RUNNING:     TrayIcon.NONE,
}

# ═════════════════════════════════════════════════════════════════════════════
# Emblems — hicolor/scalable/emblems/.
# Nautilus builds a GThemedIcon from add_emblem("NAME") trying, in order:
#   emblem-NAME -> NAME -> emblem-NAME-symbolic -> NAME-symbolic
# so pass the BARE STEM, e.g. "onedriveui-cloud" resolves emblem-onedriveui-cloud.
# ═════════════════════════════════════════════════════════════════════════════

EMBLEM_STEMS: tuple[str, ...] = (
    "onedriveui-cloud",      # online-only
    "onedriveui-local",      # locally available (green check)
    "onedriveui-pinned",     # always keep on this device (filled green circle)
    "onedriveui-syncing",    # in flight
    "onedriveui-error",      # sync problem
    "onedriveui-shared",     # shared with people
    "onedriveui-excluded",   # not syncing
    "onedriveui-locked",     # Personal Vault
)

EMBLEM_FOR_STATE: dict[FileState, str] = {
    FileState.ONLINE_ONLY: "onedriveui-cloud",
    FileState.PARTIAL:     "onedriveui-syncing",
    FileState.LOCAL:       "onedriveui-local",
    FileState.PINNED:      "onedriveui-pinned",
    FileState.DIRTY:       "onedriveui-syncing",
    FileState.SYNCING:     "onedriveui-syncing",
    FileState.EXCLUDED:    "onedriveui-excluded",
    FileState.ERROR:       "onedriveui-error",
    FileState.UNKNOWN:     "",
}

# ═════════════════════════════════════════════════════════════════════════════
# In-app glyphs — Fluent UI System Icons (MIT), bundled as SVG at native sizes.
# Key -> asset stem. Available at 12/16/20/24/28/32/48.
# ═════════════════════════════════════════════════════════════════════════════

GLYPHS: dict[str, str] = {
    # navigation / chrome
    "settings": "settings", "back": "arrow_left", "forward": "arrow_right",
    "chevron_down": "chevron_down", "chevron_right": "chevron_right",
    "chevron_up": "chevron_up", "close": "dismiss", "more": "more_horizontal",
    "kebab": "more_vertical", "search": "search", "refresh": "arrow_sync",
    "open_external": "open", "folder": "folder", "folder_open": "folder_open",
    "file": "document", "image": "image", "video": "video", "music": "music_note_2",
    # sync verbs
    "upload": "arrow_upload", "download": "arrow_download",
    "sync": "arrow_sync_circle", "pause": "pause", "play": "play",
    "cloud": "cloud", "cloud_off": "cloud_off", "cloud_sync": "cloud_sync",
    "pin": "pin", "unpin": "pin_off", "delete": "delete", "restore": "arrow_undo",
    "rename": "rename", "share": "share", "link": "link", "copy": "copy",
    "history": "history", "recycle": "delete_dismiss",
    # status
    "check": "checkmark_circle", "error": "error_circle",
    "warning": "warning", "info": "info", "blocked": "prohibited",
    "lock": "lock_closed", "unlock": "lock_open", "person": "person",
    "people": "people", "storage": "hard_drive", "wifi": "wifi_1",
    "battery": "battery_charge", "metered": "cellular_data_1",
}

GLYPH_SIZES: tuple[int, ...] = (12, 16, 20, 24, 28, 32, 48)

APP_ICON_NAME = "onedriveui"     # hicolor/scalable/apps/onedriveui.svg


# ═════════════════════════════════════════════════════════════════════════════
# API
# ═════════════════════════════════════════════════════════════════════════════

def icon(key: str, size: int = 16, color: str | None = None):
    """-> QIcon for a GLYPHS key at a NATIVE size. `color` recolours a monochrome
    SVG via QPainter.CompositionMode_SourceIn. Raises KeyError on an unknown key
    and ValueError on a non-native size."""
    ...


def tray_icon(tray: TrayIcon, frame: int = 0):
    """-> QIcon.fromTheme(name). NEVER a raw pixmap: SNI transmits an IconName.
    `frame` selects a SPINNER_FRAMES entry when tray is SYNCING."""
    ...


def tray_icon_name(tray: TrayIcon, frame: int = 0) -> str: ...


def emblem_name(state: FileState) -> str:
    """-> the bare stem for Nautilus.FileInfo.add_emblem(). "" means no emblem."""
    ...


def logo(px: int):
    """-> QIcon of the flat 2019 four-shape OneDrive mark. The 2025 refresh uses
    seven radial gradients over a 648x431 viewBox and turns to mud at 16 px —
    always use the flat mark at <= 32 px. The mark is WIDER THAN TALL
    (viewBox '0 5.5 32 20.5') and must not be stretched to square."""
    ...


def render_svg(data: bytes, px: int, dpr: float = 1.0):
    """-> QPixmap. Allocates round(px*dpr), calls setDevicePixelRatio BEFORE
    painting, and renders into QRectF(0, 0, dev, dev) in device coordinates."""
    ...


def badged(base_name: str, badge: str, px: int):
    """-> QPixmap: a base icon with a 10x10 status badge in the bottom-right
    (bottom-LEFT for file overlays), separated by a 1 px cut-out ring painted
    with CompositionMode_Clear so the badge reads at 16 px.

    Do NOT setDevicePixelRatio on a pixmap passed to QIcon.addPixmap — QIcon
    indexes by RAW pixel size. DO set it on pixmaps drawn via QPainter.drawPixmap."""
    ...


def install_theme_icons() -> None:
    """Write every tray, emblem and app SVG into ~/.local/share/icons/hicolor/
    and run `gtk4-update-icon-cache -f -t`. Without this, Nautilus emblems
    silently do not appear."""
    ...
```

---

## 9. `onedriveui/units.py` (frozen signatures)

```python
def human_bytes(n: int, *, style: str = "windows") -> str:
    """'4.8 GB' — decimal (1000) units, matching the OneDrive UI. style='binary'
    gives GiB for developer-facing surfaces only."""

def human_rate(bytes_per_s: float) -> str: ...          # "1.2 MB/s"
def human_duration(seconds: float) -> str: ...          # "2h 15m"
def eta_text(seconds: int | None) -> str: ...           # "" when None
def relative_time(iso: str) -> str: ...                 # "2 minutes ago", "Yesterday"

def kb_to_kib(kb: int) -> int:
    """THE conversion. The OneDrive UI is KB/s (1000); rclone's --bwlimit and
    core/bwlimit are KiB/s (1024). round(kb * 1000 / 1024). Never inlined
    anywhere else."""

def kib_to_kb(kib: int) -> int: ...
def parse_size(text: str) -> int: ...                   # "30M", "20G", "512Ki"
def format_bwlimit(down_kb: int | None, up_kb: int | None) -> str:
    """-> an rclone rate string, e.g. "1Mi:100Ki" or "off". NEVER string-compare
    the value core/bwlimit echoes back: rclone normalises '1M:100k' to
    '1Mi:100Ki'."""
```

---

## 10. Frozen service signatures

A work package may add private helpers. It may **not** change a signature listed here.

### 10.1 `rc/client.py`

```python
class RcClient(QObject):
    def __init__(self, endpoint: RcEndpoint, parent: QObject | None = None) -> None: ...
    def call(self, path: str, params: dict | None = None, *,
             group: str | None = None, async_: bool = False,
             config: dict | None = None, filt: dict | None = None,
             timeout_s: float = RC_TIMEOUT_S) -> "RcCall": ...
    def close(self) -> None: ...

class RcCall(QObject):
    succeeded = Signal(dict)
    failed    = Signal(object)          # RcError

def call_blocking(ep: RcEndpoint, path: str, params: dict | None = None,
                  timeout_s: float = 30.0) -> dict:
    """For IOPool threads only. Raises RcError / DaemonUnavailable."""

class JobWatcher(QObject):
    finished = Signal(dict)             # job/status output
    failed   = Signal(object)           # RcError
    expired  = Signal()                 # 'job not found' with an UNCHANGED execute_id
    lost     = Signal()                 # execute_id CHANGED == daemon restarted
    def watch(self, handle: JobHandle, poll_ms: int = 500) -> None: ...
    def stop(self) -> None: ...

def is_alive(ep: RcEndpoint, timeout_s: float = 1.0) -> bool: ...   # rc/noop
```

### 10.2 `rc/daemon.py` / `rc/mountd.py`

```python
class RcdSupervisor(QObject):
    restarted = Signal(str)             # new execute_id
    def ensure_running(self) -> RcEndpoint: ...     # raises DaemonForeign
    def endpoint(self) -> RcEndpoint | None: ...
    def health(self) -> DaemonHealth: ...
    def restart(self, reason: str) -> None: ...
    def stop(self) -> None: ...
    @staticmethod
    def verify_ownership(ep: RcEndpoint) -> bool: ...
    @staticmethod
    def unit_text(port: int, user: str, password: str) -> str: ...

class MountController(QObject):
    def ensure_mounted(self, account: AccountInfo) -> None: ...
    def health(self, account: AccountInfo) -> MountHealth: ...
    def endpoint(self, account: AccountInfo) -> RcEndpoint | None: ...
    def unmount(self, account: AccountInfo, *, lazy: bool = True) -> None: ...
    def restart(self, account: AccountInfo, reason: str) -> None:
        """Refuses (returns without acting, logging why) while
        uploads_in_progress > 0 UNLESS health() is already STALE — invariant I3."""
    def build_argv(self, account: AccountInfo, port: int,
                   creds: tuple[str, str]) -> list[str]:
        """Calls guards.assert_no_backend_flags() on its own output. Raises
        SafetyRefusal rather than emit a --onedrive-* flag (invariant I1)."""
    def unit_text(self, account: AccountInfo, port: int, creds) -> str: ...
    def status_text(self, account: AccountInfo) -> str: ...

def is_live(mountpoint: Path) -> MountHealth:
    """BOTH a /proc/self/mounts fuse.rclone entry AND a statvfs() that does not
    raise. os.path.ismount() alone returns True for a dead ENOTCONN mount."""

def rclone_mounts() -> list[tuple[str, Path]]: ...
```

### 10.3 `rc/guards.py` — every function raises `SafetyRefusal`, never returns False

```python
def assert_not_under_fuse(path: Path, what: str) -> None: ...            # I2
def assert_disjoint(local: Path, mountpoints: list[Path]) -> None: ...   # I2
def assert_no_backend_flags(argv: list[str]) -> None: ...                # I1
def assert_bisync_safe(path1: str, path2: str, cfg) -> None: ...         # I2, I11, I12, I13
def assert_evict_safe(meta: dict, queue_names: set[str], rel_path: str) -> None: ...  # I3
def assert_db_not_on_fuse(path: Path) -> None: ...
def rewrite_mount_path_to_remote(path: Path, account: AccountInfo) -> str:
    """Turn ~/OneDrive/x into onedrive:x so a user action on a mounted path can
    be honoured through the remote rather than refused."""
```

### 10.4 `rc/vfs.py`

```python
def disk_cache_info(ep: RcEndpoint) -> DiskCacheInfo: ...      # invariant I4

def classify(meta: dict, *, pinned: bool = False) -> FileState:
    """local        iff Rs == [{Pos:0, Size:Size}]
       online_only  iff no sidecar, or Rs is null/[]
       dirty        iff Dirty is true
       partial      otherwise
       pinned       iff local and `pinned`
    Rs WINS over the physical file: after abnormal events the sparse file can
    hold bytes that Rs does not list, and rclone re-downloads regardless."""

def scan(info: DiskCacheInfo, generation: int,
         progress=None) -> Iterator[CacheEntry]:
    """Walk pathMeta. IOPool only."""

def local_extents(data_path: Path) -> list[tuple[int, int]]:
    """SEEK_DATA / SEEK_HOLE — byte-identical to Rs and SYNCHRONOUS, whereas the
    sidecar lags ~10 s. Falls back to the sidecar on EINVAL (FAT/exFAT)."""

def evict(info: DiskCacheInfo, rel_path: str, queue_names: set[str]) -> int:
    """assert_evict_safe() first, then unlink META, then DATA (invariant I5).
    Returns bytes reclaimed. Raises SafetyRefusal on a dirty or queued item."""

def evict_tree(info: DiskCacheInfo, rel_prefix: str, queue_names: set[str]) -> int: ...
def queue(ep: RcEndpoint) -> list[QueueItem]: ...
def force_upload_now(ep: RcEndpoint, item_id: int) -> None:
    """vfs/queue-set-expiry with a large negative expiry. 'id not found in queue'
    is a NORMAL ~5 s race against --vfs-write-back, not an error."""
def defer_uploads(ep: RcEndpoint, seconds: float) -> int: ...   # how pause works
def orphaned_cache_trees(info: DiskCacheInfo) -> list[tuple[Path, int]]: ...
def refresh(ep: RcEndpoint, dirs: list[str], *, recursive: bool = False) -> dict:
    """vfs/refresh. NOTE: `recursive` must be sent as the STRING "true" — a JSON
    boolean is rejected with HTTP 400. Unique in the whole rc API.
    NEVER call recursively from a UI action: OneDrive has ListR=false, so it is
    one Graph request per directory."""
def forget(ep: RcEndpoint, dirs=None, files=None) -> list[str]:
    """Invalidates the in-memory dir cache ONLY. Does NOT free disk — it returns
    a reassuring {"forgotten": [...]} while bytesUsed is unchanged."""
def set_poll_interval(ep: RcEndpoint, seconds: int) -> dict:
    """HTTP 500 on backends without ChangeNotify. Gate on Capabilities.change_notify."""
```

### 10.5 `sync/reducer.py` — pure

```python
LADDER: tuple[tuple[str, Callable[[Facts], bool], SyncState], ...]

def reduce(facts: Facts) -> SyncState:
    """Pure. No I/O, no Qt, no globals, no clock. First match in LADDER wins."""

class Debouncer:
    def apply(self, new: SyncState, now_monotonic: float) -> SyncState: ...
    def reset(self) -> None: ...

def status_text(state: SyncState, facts: Facts) -> tuple[str, str]: ...   # headline, subtext
def tooltip(state: SyncState, facts: Facts) -> str: ...
def tray_for(state: SyncState, account: AccountInfo) -> TrayIcon: ...
def transition_effects(old: SyncState, new: SyncState, facts: Facts) -> list[str]: ...
```

### 10.6 `sync/supervisor.py`

```python
class Supervisor(QObject):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> SyncState: ...
    def snapshot(self) -> SyncSnapshot: ...
    def do(self, action: RecoveryAction, **kw) -> None:
        """THE single entry point for every recovery and every user action that
        changes the world. UI code never calls a service directly."""
    def request_pause(self, reason: PauseReason, hours: int | None) -> None: ...
    def request_resume(self) -> None: ...
    def request_resync(self, *, decision_id: int) -> None:
        """Raises SafetyRefusal without an ANSWERED decision row (invariant I15)."""
    def restart_mount(self, reason: str) -> None: ...
    def reset_client(self, *, keep_files: bool = True) -> None: ...
    def reclaim_orphaned_cache(self) -> int: ...
    SCHEDULE: dict[str, int]
```

### 10.7 `sync/pinner.py`, `sync/pause.py`, `sync/bandwidth.py`

```python
class Pinner(QObject):
    progress = Signal(str, int, int)
    def pin(self, rel_path: str, *, recursive: bool = False) -> None: ...
    def unpin(self, rel_path: str, *, recursive: bool = False) -> None: ...
    def free_up_space(self, rel_path: str) -> int: ...
    def free_up_all(self) -> int: ...
    def download_all(self) -> None: ...
    def cancel(self, rel_path: str) -> None: ...
    def active(self) -> int: ...
    def sizing(self, rel_path: str) -> tuple[int, int]: ...     # (local, total)
    MAX_CONCURRENT_PINS: int = 3

class PauseManager(QObject):
    def pause(self, reason: PauseReason, hours: int | None = None) -> None: ...
    def resume(self, reason: PauseReason | None = None) -> None: ...
    def sync_anyway(self, reason: PauseReason) -> None: ...
    def active(self) -> PauseReason: ...
    def until(self) -> datetime | None: ...
    def enforce(self, ep: RcEndpoint) -> int:
        """Called EVERY TICK while paused. Pushes every vfs/queue item's expiry
        past the pause deadline. Returns items deferred. Files already uploading
        finish — that is stated in the UI, not hidden."""
    PAUSE_DURATIONS: tuple[tuple[int | None, str], ...]

class BandwidthController(QObject):
    def apply(self, state: BandwidthState) -> None:
        """core/bwlimit on BOTH daemons. _config.BwLimit is accepted and echoed
        by rclone but does NOT throttle — core/bwlimit is the only one that works."""
    def set_auto(self, on: bool, percent: int = 70) -> None: ...
    def current(self) -> BandwidthState: ...
    def reapply_after_restart(self) -> None: ...
```

### 10.8 `sync/issues.py`, `sync/decisions.py`, `sync/preflight.py`

```python
class IssueEngine(QObject):
    def ingest_transfer_error(self, ev: ActivityEvent) -> int | None: ...
    def ingest_log_record(self, rec: dict, run_id: str) -> int | None: ...
    def ingest_health(self, facts: Facts) -> None: ...
    def ingest_preflight(self, violations: list["Violation"]) -> None: ...
    def reconcile(self, facts: Facts) -> int: ...          # auto-resolve
    def execute(self, action: RecoveryAction, issue: SyncIssue, **kw) -> bool: ...
    def mute(self, issue_id: int) -> None: ...
    def counts(self, account_id: str) -> tuple[int, int, int]: ...   # blocking, error, warning

class DecisionCenter(QObject):
    def require(self, kind: DecisionKind, payload: dict,
                expires_in_days: int = 7) -> int: ...
    def answer(self, decision_id: int, answer: str) -> None: ...
    def pending(self, account_id: str) -> list[Decision]: ...
    def expire_stale(self) -> int:
        """Expiry means DO NOT DELETE, matching Microsoft's 7-day policy."""
    def on_maxdelete_abort(self, run: RunRecord, parsed: dict) -> int: ...

@dataclass(frozen=True, slots=True)
class Violation:
    rel_path: str
    code: IssueCode
    detail: str
    suggested_name: str | None = None

def validate_name(name: str) -> Violation | None: ...
def validate_path(rel_path: str, sync_root: Path) -> Violation | None: ...
def validate_size(path: Path) -> Violation | None: ...
def suggest(name: str) -> str: ...           # deterministic; the Rename default
def scan_tree(root: Path, budget_ms: int = 250) -> Iterator[Violation]: ...
```

### 10.9 `sync/sharing.py`, `sync/versions.py`, `sync/trashbin.py`

```python
class ShareService(QObject):
    def create_link(self, rel_path: str, link_type: LinkType, scope: LinkScope,
                    expire_days: int | None = None,
                    password: str | None = None) -> ShareLink: ...
    def links_for(self, rel_path: str) -> list[ShareLink]: ...
    def can_revoke(self) -> bool:
        """ALWAYS False. `rclone link --unlink` is a verified SILENT NO-OP on
        OneDrive that CREATES a link — the parameter is declared and never read.
        The UI must show the control DISABLED with strings.DIALOG.REMOVE_LINK_WHY,
        never pretend a link was revoked."""
        return False
    def web_manage_url(self, rel_path: str) -> str: ...
    def mailto_url(self, link: ShareLink, recipients: list[str]) -> str: ...
    def permissions(self, rel_path: str) -> list[dict]: ...

def versions_for(account_id: str, rel_path: str) -> list[VersionEntry]:
    """OUR OWN bisync --backup-dir snapshots only. OneDrive's server-side version
    history is real but rclone can only DELETE versions, never list or restore
    them — that is a web deep-link (strings.DIALOG.VERSION_HISTORY_WHY)."""
def restore_version(account_id: str, version_id: int) -> None:
    """Captures the CURRENT copy as a new version first, then restores."""
def web_version_url(account_id: str, item_id: str) -> str: ...

def soft_delete(ep: RcEndpoint, account: AccountInfo, rel_path: str) -> TrashEntry:
    """Server-side move into .onedriveui-trash/<ts>/ — instant. This is what OUR
    Delete does. A delete through the mount goes to Microsoft's own cloud recycle
    bin instead, which we cannot list (strings.DIALOG.RECYCLE_BIN_WHY)."""
def restore_from_trash(ep: RcEndpoint, account: AccountInfo, trash_id: int) -> None: ...
def purge_expired(ep: RcEndpoint, account: AccountInfo) -> int: ...
def web_recyclebin_url(account: AccountInfo) -> str: ...
# NOTE: operations/cleanup is NEVER called (invariant I8) — on OneDrive it
# deletes FILE VERSIONS, not the trash, and is unsupported on Personal.
```

### 10.10 `platform/notify.py`, `platform/ipc.py`

```python
class Notifier(QObject):
    action_invoked = Signal(str, str)          # NotificationId value, action id
    def notify(self, spec: NotifySpec) -> int:
        """Gio.DBusConnection + GLib.Variant("(susssasa{sv}i)"). PySide6's QtDBus
        CANNOT marshal the uint32 this signature needs, so QtDBus is unusable here.
        `urgency` is sent as GVariant BYTE 'y' — sending it as 'i' misbehaves.
        Sets the desktop-entry hint so GNOME shows our name and groups bubbles.
        Body text is GLib.markup_escape_text()d: body-markup is ON."""
    def close(self, nid: int) -> None: ...
    def capabilities(self) -> frozenset[str]:
        """On this machine: {actions, body, body-markup, icon-static, persistence,
        sound}. NO body-images, NO body-hyperlinks, NO action-icons, NO inline-reply."""
    def is_enabled(self, nid: NotificationId) -> bool: ...
    MAX_ACTIONS: int = 2                       # GNOME renders about 3

class IpcServer(QObject):
    action_requested = Signal(str, list)
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def broadcast_invalidate(self, paths: list[str]) -> None: ...
    BUDGET_MS: int = 20
    # Wire protocol — newline-delimited JSON over $XDG_RUNTIME_DIR/onedriveui/ipc.sock:
    #   -> {"op":"hello","v":1}
    #   <- {"op":"hello","v":1,"account":"onedrive","root":"/home/u/OneDrive"}
    #   -> {"op":"state","paths":["/abs/a","/abs/b"]}
    #   <- {"op":"state","states":{"/abs/a":"online_only","/abs/b":"pinned"}}
    #   -> {"op":"menu","paths":[...]}      <- {"op":"menu","actions":[...]}
    #   -> {"op":"do","action":"pin","paths":[...]}   <- {"op":"ok"}
    #   <= {"op":"invalidate","paths":[...]}   (server push, unsolicited)
    # Every response is produced from cache_index/pins in <= BUDGET_MS. On a
    # timeout the answer is "unknown"; the extension NEVER blocks Nautilus's UI
    # thread, because update_file_info must be synchronous
    # (Nautilus.OperationHandle cannot be constructed from Python).
```

---

## 11. Contract compliance checklist

Every work package's PR is checked against this list before merge.

- [ ] No file owned by another package was modified.
- [ ] No user-facing string literal outside `strings.py`.
- [ ] No colour literal outside `ui/theme.py`.
- [ ] No icon name literal outside `ui/icons.py`.
- [ ] No `Signal` declared outside `bus.py`.
- [ ] No magic number that belongs in `constants.py`.
- [ ] No `--onedrive-*` flag in any argv (I1) — `assert_no_backend_flags` is called.
- [ ] No rclone data command names a path under a fuse mount (I2).
- [ ] No eviction path bypasses `assert_evict_safe` (I3).
- [ ] Cache paths come from `vfs/stats` (I4), never `os.path.join` on a guess.
- [ ] `mount/mount`, `mount/unmount`, `mount/listmounts` and `operations/cleanup` appear nowhere (I7, I8).
- [ ] No synchronous HTTP, no `requests`, no `urllib` on the GUI thread.
- [ ] No `Gio` call off the GUI thread; no `QWidget` touched off the GUI thread.
- [ ] No SQLite write outside `DbWriter`.
- [ ] `QSystemTrayIcon.showMessage` is not used; notifications go through `Notifier`.
- [ ] Every `QNetworkReply` is `deleteLater()`d.
- [ ] Every looping animation is stopped in `hideEvent`.
- [ ] A `tests/test_<module>.py` exists and passes against the `FakeRc` fixture.
