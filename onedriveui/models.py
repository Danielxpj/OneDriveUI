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
from enum import StrEnum, IntEnum  # noqa: F401  (IntEnum is part of the frozen import line)
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
