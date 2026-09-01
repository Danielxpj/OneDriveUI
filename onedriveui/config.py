"""Typed configuration: the whole of ARCHITECTURE §9, as dataclasses.

``~/.config/onedriveui/config.json``, mode 0600, written atomically with a
``.bak`` and repaired from it on a ``JSONDecodeError``.

Three rules shape everything below.

**Loading never fails.** A user who hand-edits this file and gets it wrong must
still be able to start the application and fix the setting in the UI. So
:func:`load` falls back per key: a value of the wrong type, or a string outside
its enumeration, is replaced by that key's default and the rest of the document
is kept. A file that is not JSON at all falls back to ``.bak``, and then to a
document of pure defaults. Nothing on this path raises.

**Saving can fail, loudly.** :func:`validate` refuses a configuration that would
break an invariant or a Microsoft hard limit — more than 4 transfers, a chunk
size that is not a multiple of 320 KiB, a sync root nested inside somebody
else's FUSE mount — and :func:`save` calls it first, so an unsafe document
never reaches the disk. That asymmetry is deliberate: we tolerate a bad file we
did not write, and refuse to author one.

**Runtime state does not live here.** ``paused_until``, latches, wizard
completion and the rc ``executeId`` are in SQLite, so that a hand-edit of this
file cannot corrupt them and a corrupt file cannot lose them.

Every mutation that reaches the disk emits :data:`~onedriveui.bus.BUS`
``config_changed`` once per changed dotted key — ``"app.theme"``,
``"advanced.log_level"``, ``"mount.transfers"``, ``"account.sync_root"``,
``"accounts"`` for a whole account added or removed. Consumers filter on the
prefix, which is why account sections are spelled section-first.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import socket
import types
import typing
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Final, Iterable

from onedriveui import APP_ID, USER_AGENT
from onedriveui import paths
from onedriveui.atomicio import backup_then_write, read_json
from onedriveui.bus import BUS
from onedriveui.constants import (
    ACTIVITY_CENTER_WIDTH, ACTIVITY_UI_ROWS, AUTO_UPLOAD_PERCENT,
    BANDWIDTH_CEIL_KB, BANDWIDTH_FLOOR_KB, BISYNC_DEFAULT_MAX_DELETE_PCT,
    BISYNC_MAX_LOCK_MIN, DEFAULT_LOW_LEVEL_RETRIES, DEFAULT_RETRIES,
    DEFAULT_TPSLIMIT, DEFAULT_TPSLIMIT_BURST, MASS_DELETE_DEFAULT_THRESHOLD,
    MAX_CHECKERS, MAX_CONCURRENT_PINS, MAX_FILE_BYTES, MAX_REL_PATH_CHARS,
    MAX_TOTAL_PATH_CHARS, MAX_TRANSFERS, ONEDRIVE_CHUNK_MULTIPLE,
    RC_FORBIDDEN_PORTS, RC_JOB_EXPIRE, RC_PORT_RANGE, TICK_ACTIVE_MS,
    TICK_IDLE_MS,
)
from onedriveui.errors import ConfigError
from onedriveui.models import AccountInfo, AccountKind
from onedriveui.units import parse_size

__all__ = [
    "CONFIG_SCHEMA_VERSION", "CHOICES", "SECTION_TYPES", "ACCOUNT_SECTIONS",
    "AppSection", "AdvancedSection", "MountSection", "BackendSection",
    "FilesOnDemandSection", "BandwidthSection", "PauseSection",
    "NotificationsSection", "SafetySection", "FilesSection", "ConflictsSection",
    "SelectiveSection", "KfmSection", "OfflineFolderSection", "SharingSection",
    "VaultSection", "ExtrasSection", "IntegrationSection", "UiSection",
    "AccountConfig", "AppConfig",
    "defaults", "load", "save", "validate", "migrate", "account",
    "changed_keys", "default_device_name",
]

#: Bumped only when a migration step is added below. ``schema_version`` in the
#: file drives which steps run.
CONFIG_SCHEMA_VERSION: Final[int] = 1

#: Sentinel for "no usable value here". Distinct from ``None``, which is a
#: legitimate stored value for every nullable key in the schema.
_UNSET: Final[Any] = object()


def default_device_name() -> str:
    """The short hostname, as ``hostname -s`` reports it.

    Returns:
        The first label of the system hostname, or ``"localhost"`` when the
        name cannot be read. Used for the conflict-copy suffix, so it must be
        filesystem-safe and stable across reboots.
    """
    try:
        name = socket.gethostname()
    except OSError:
        return "localhost"
    short = name.split(".")[0].strip()
    return short or "localhost"


# ═════════════════════════════════════════════════════════════════════════════
# Section dataclasses — ARCHITECTURE §9, key for key, default for default
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class AppSection:
    """``app.*`` — appearance and startup behaviour."""

    theme: str = "system"
    accent_source: str = "onedrive"
    animations: str = "system"
    autostart: bool = True
    autostart_method: str = "systemd"
    start_minimized: bool = True
    keep_tray_icon_when_stopped: bool = True
    first_run_complete: bool = False
    active_account_id: str | None = None
    locale: str = "system"


@dataclass(slots=True)
class AdvancedSection:
    """``advanced.*`` — knobs no ordinary user touches."""

    rclone_path: str = "/usr/bin/rclone"
    rc_port_range: list[int] = field(
        default_factory=lambda: [RC_PORT_RANGE.start, RC_PORT_RANGE.stop - 1])
    log_level: str = "INFO"
    keep_logs_days: int = 14
    tick_idle_ms: int = TICK_IDLE_MS
    tick_active_ms: int = TICK_ACTIVE_MS
    job_expire: str = RC_JOB_EXPIRE
    user_agent: str = USER_AGENT


@dataclass(slots=True)
class MountSection:
    """``accounts[].mount.*`` — everything that becomes mount argv.

    Backend options are deliberately absent: they live in
    :class:`BackendSection` and are mirrored into ``rclone.conf``, never onto a
    command line (invariant I1).
    """

    enabled: bool = True
    cache_dir: str = "~/.cache/rclone"
    vfs_cache_max_size_gb: int = 50
    vfs_cache_max_age_hours: int = 720
    vfs_cache_min_free_space_gb: int = 5
    vfs_cache_poll_interval_s: int = 60
    poll_interval_s: int = 60
    dir_cache_time_s: int = 3600
    attr_timeout_ms: int = 1000
    read_chunk_size_mb: int = 32
    read_chunk_size_limit_mb: int = 512
    read_chunk_streams: int = 0
    write_back_s: int = 5
    handle_caching_s: int = 5
    transfers: int = MAX_TRANSFERS
    checkers: int = MAX_CHECKERS
    tpslimit: float = DEFAULT_TPSLIMIT
    tpslimit_burst: int = DEFAULT_TPSLIMIT_BURST
    retries: int = DEFAULT_RETRIES
    low_level_retries: int = DEFAULT_LOW_LEVEL_RETRIES
    umask: str = "022"
    file_perms: str = "0644"
    dir_perms: str = "0755"
    fast_fingerprint: bool = True
    links: bool = False
    allow_other: bool = False
    warm_up_on_start: bool = False
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BackendSection:
    """``accounts[].backend.*`` — mirrored into ``rclone.conf`` only (I1)."""

    chunk_size: str = "10M"
    upload_cutoff: str = "off"
    delta: bool = True
    no_versions: bool = False
    hard_delete: bool = False
    link_scope: str = "anonymous"
    link_type: str = "view"
    link_password: str = ""
    hash_type: str = "auto"
    metadata_permissions: str = "off"
    expose_onenote_files: bool = False
    encoding: str | None = None


@dataclass(slots=True)
class FilesOnDemandSection:
    """``accounts[].files_on_demand.*``."""

    enabled: bool = True
    auto_free_up_days: int | None = None
    hydrate_concurrency: int = MAX_CONCURRENT_PINS
    pin_all_in_progress: bool = False


@dataclass(slots=True)
class BandwidthSection:
    """``accounts[].bandwidth.*`` — KB/s (1000) throughout, as the UI shows it.

    ``core/bwlimit`` is process-global, so these values are global in effect
    even though they are stored per account; the settings page says so.
    """

    limit_download: bool = False
    download_kb: int | None = None
    upload_mode: str = "none"
    upload_kb: int | None = None
    auto_percent: int = AUTO_UPLOAD_PERCENT


@dataclass(slots=True)
class PauseSection:
    """``accounts[].pause.*`` — policy only.

    The *live* pause deadline lives in SQLite: a hand-edit of this file must not
    be able to resurrect or extend a pause the user already ended.
    """

    manual_until: str | None = None
    manual_indefinite: bool = False
    on_metered: bool = True
    on_battery_saver: bool = True
    override_until: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NotificationsSection:
    """``accounts[].notifications.*`` — Windows' five toggles plus three of ours."""

    paused: bool = True
    shared_or_edited: bool = True
    mass_delete: bool = True
    memories: bool = False
    other_accounts: bool = False
    sync_issues: bool = True
    conflicts: bool = True
    sync_complete: bool = True


@dataclass(slots=True)
class SafetySection:
    """``accounts[].safety.*``."""

    mass_delete_threshold: int = MASS_DELETE_DEFAULT_THRESHOLD
    confirm_first_delete: bool = True
    min_disk_space_mb: int = 500
    warning_min_disk_space_mb: int = 2048
    verify_weekly: bool = True
    #: Read-only. Shown in the settings page and never editable: turning it off
    #: would let an rclone data command name a path under the mount (I2).
    refuse_paths_under_mount: bool = True


@dataclass(slots=True)
class FilesSection:
    """``accounts[].files.*`` — naming and size policy."""

    name_policy: str = "windows"
    excluded_extensions: list[str] = field(
        default_factory=lambda: [".lnk", ".tmp", ".partial", ".swp"])
    max_file_bytes: int = MAX_FILE_BYTES
    max_rel_path_chars: int = MAX_REL_PATH_CHARS
    max_total_path_chars: int = MAX_TOTAL_PATH_CHARS


@dataclass(slots=True)
class ConflictsSection:
    """``accounts[].conflicts.*``."""

    policy: str = "ask"
    suffix_template: str = "-{device_name}"
    device_name: str = field(default_factory=default_device_name)


@dataclass(slots=True)
class SelectiveSection:
    """``accounts[].selective.*`` — "Choose folders"."""

    mode: str = "all"
    excluded_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KfmSection:
    """``accounts[].kfm.*`` — Known Folder Move."""

    desktop: bool = False
    documents: bool = False
    pictures: bool = False
    music: bool = False
    videos: bool = False
    method: str = "move"
    leave_shortcut: bool = True


@dataclass(slots=True)
class OfflineFolderSection:
    """``accounts[].offline_folder.*`` — the opt-in Topology-B bisync folder."""

    enabled: bool = False
    local_path: str = "~/OneDrive-Offline"
    remote_path: str = "onedrive:Offline"
    schedule_minutes: int = 15
    max_delete_percent: int = BISYNC_DEFAULT_MAX_DELETE_PCT
    conflict_resolve: str = "newer"
    conflict_loser: str = "pathname"
    conflict_suffix: str = "-{device_name}"
    check_access: bool = True
    check_filename: str = "RCLONE_TEST"
    max_lock: str = f"{BISYNC_MAX_LOCK_MIN}m"
    resilient: bool = True
    recover: bool = True
    track_renames: bool = True
    create_empty_src_dirs: bool = True
    compare: str = "size,modtime"
    backup_versions: bool = True


@dataclass(slots=True)
class SharingSection:
    """``accounts[].sharing.*`` — defaults for the "Copy link" flyout."""

    default_scope: str = "anonymous"
    default_type: str = "view"
    default_expiry_days: int | None = None


@dataclass(slots=True)
class VaultSection:
    """``accounts[].vault.*`` — the gocryptfs-backed Personal Vault."""

    enabled: bool = False
    backend: str = "gocryptfs"
    container_path: str = f"~/.local/share/{APP_ID}/vault"
    mount_at: str = "{sync_root}/Personal Vault"
    auto_lock_minutes: int = 20
    warn_before_minutes: int = 5


@dataclass(slots=True)
class ExtrasSection:
    """``accounts[].extras.*`` — screenshots and camera import."""

    screenshots: bool = False
    screenshots_dir: str = "{PICTURES}/Screenshots"
    camera_import: bool = False


@dataclass(slots=True)
class IntegrationSection:
    """``accounts[].integration.*`` — desktop integration toggles."""

    nautilus_extension: bool = True
    sidebar_bookmark: bool = True
    status_column: bool = True


@dataclass(slots=True)
class UiSection:
    """``accounts[].ui.*`` — remembered window geometry and list sizes."""

    activity_center_width: int = ACTIVITY_CENTER_WIDTH
    activity_rows: int = ACTIVITY_UI_ROWS
    window_geometry: dict[str, Any] = field(default_factory=dict)


#: The account sections, in the order ARCHITECTURE §9.2 lists them. Order is
#: load-bearing: it is the key order of the JSON document, which keeps a
#: hand-diff of two config files readable.
ACCOUNT_SECTIONS: Final[tuple[str, ...]] = (
    "mount", "backend", "files_on_demand", "bandwidth", "pause",
    "notifications", "safety", "files", "conflicts", "selective", "kfm",
    "offline_folder", "sharing", "vault", "extras", "integration", "ui",
)


@dataclass(slots=True)
class AccountConfig:
    """One entry of ``accounts[]`` — a single rclone remote and its policy."""

    id: str = "onedrive"
    remote: str = "onedrive"
    kind: str = "personal"
    display_name: str | None = None
    email: str | None = None
    drive_id: str | None = None
    drive_type: str | None = None
    sync_root: str = "~/OneDrive"
    enabled: bool = True

    mount: MountSection = field(default_factory=MountSection)
    backend: BackendSection = field(default_factory=BackendSection)
    files_on_demand: FilesOnDemandSection = field(default_factory=FilesOnDemandSection)
    bandwidth: BandwidthSection = field(default_factory=BandwidthSection)
    pause: PauseSection = field(default_factory=PauseSection)
    notifications: NotificationsSection = field(default_factory=NotificationsSection)
    safety: SafetySection = field(default_factory=SafetySection)
    files: FilesSection = field(default_factory=FilesSection)
    conflicts: ConflictsSection = field(default_factory=ConflictsSection)
    selective: SelectiveSection = field(default_factory=SelectiveSection)
    kfm: KfmSection = field(default_factory=KfmSection)
    offline_folder: OfflineFolderSection = field(default_factory=OfflineFolderSection)
    sharing: SharingSection = field(default_factory=SharingSection)
    vault: VaultSection = field(default_factory=VaultSection)
    extras: ExtrasSection = field(default_factory=ExtrasSection)
    integration: IntegrationSection = field(default_factory=IntegrationSection)
    ui: UiSection = field(default_factory=UiSection)

    @property
    def fs(self) -> str:
        """The rclone fs string, always ``<remote>:``.

        A ``{HASH}`` suffix appearing here would mean a backend flag leaked onto
        a command line (invariant I1).
        """
        return f"{self.remote}:"

    def resolved_sync_root(self) -> Path:
        """The sync root with ``~`` expanded.

        Returns:
            An absolute path. Not resolved through symlinks — the mountpoint
            check wants the configured location, and resolving would follow a
            KFM symlink straight into the mount.
        """
        return Path(os.path.expanduser(self.sync_root or "~/OneDrive"))

    def resolved_offline_path(self) -> Path:
        """The Topology-B local folder with ``~`` expanded."""
        return Path(os.path.expanduser(
            self.offline_folder.local_path or "~/OneDrive-Offline"))

    def to_account_info(self) -> AccountInfo:
        """Project this configuration onto the frozen runtime model.

        Returns:
            An :class:`~onedriveui.models.AccountInfo`. The nullable JSON fields
            become empty strings, because the model uses ``""`` for "unknown"
            and every consumer already treats it that way.
        """
        try:
            kind = AccountKind(self.kind)
        except ValueError:
            kind = AccountKind.PERSONAL
        return AccountInfo(
            id=self.id,
            remote=self.remote,
            kind=kind,
            display_name=self.display_name or "",
            email=self.email or "",
            drive_id=self.drive_id or "",
            drive_type=self.drive_type or kind.value,
            sync_root=str(self.resolved_sync_root()),
            enabled=self.enabled,
        )


@dataclass(slots=True)
class AppConfig:
    """The whole of ``config.json``."""

    schema_version: int = CONFIG_SCHEMA_VERSION
    app: AppSection = field(default_factory=AppSection)
    advanced: AdvancedSection = field(default_factory=AdvancedSection)
    accounts: list[AccountConfig] = field(default_factory=list)

    # ── serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        """Render the document exactly as it is written to disk.

        Returns:
            A plain JSON-safe dict. Key order follows ARCHITECTURE §9, because
            the file is meant to be readable and diffable by hand.
        """
        return {
            "schema_version": int(self.schema_version),
            "app": _section_to_dict(self.app),
            "advanced": _section_to_dict(self.advanced),
            "accounts": [_account_to_dict(a) for a in self.accounts],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "AppConfig":
        """Build a configuration from parsed JSON, defaulting per bad key.

        Args:
            raw: Anything ``json.load`` produced. A non-dict — a list, a string,
                ``None`` — yields a document of pure defaults rather than an
                error, because this runs on the startup path.

        Returns:
            A complete :class:`AppConfig`. Unknown keys are dropped; keys with
            an unusable value fall back to that key's default; missing keys take
            their default.
        """
        if not isinstance(raw, dict):
            return defaults()
        data = migrate(raw)
        cfg = cls(
            schema_version=CONFIG_SCHEMA_VERSION,
            app=_section_from_dict(AppSection, data.get("app"), "app"),
            advanced=_section_from_dict(AdvancedSection, data.get("advanced"),
                                        "advanced"),
            accounts=[],
        )
        raw_accounts = data.get("accounts")
        if isinstance(raw_accounts, list):
            for entry in raw_accounts:
                if isinstance(entry, dict):
                    cfg.accounts.append(_account_from_dict(entry))
        return cfg

    # ── lookup ───────────────────────────────────────────────────────────────
    def account(self, account_id: str | None = None) -> AccountConfig | None:
        """Find one account.

        Args:
            account_id: The account to find. ``None`` means "the active one",
                which is ``app.active_account_id`` when it names a real account
                and otherwise the first account in the list.

        Returns:
            The matching :class:`AccountConfig`, or ``None`` when there are no
            accounts at all (the state the OOBE exists to fix).
        """
        if not self.accounts:
            return None
        if account_id is not None:
            for entry in self.accounts:
                if entry.id == account_id:
                    return entry
            return None
        active = self.app.active_account_id
        if active:
            for entry in self.accounts:
                if entry.id == active:
                    return entry
        return self.accounts[0]

    def get(self, dotted: str, default: Any = None, *,
            account_id: str | None = None) -> Any:
        """Read a value by its dotted key.

        Args:
            dotted: ``"app.theme"``, ``"advanced.log_level"``,
                ``"mount.transfers"``, ``"account.sync_root"``, or a bare
                top-level key such as ``"schema_version"``. Account keys resolve
                against the active account.
            default: Returned when the key does not exist.
            account_id: Which account an account-scoped key belongs to;
                ``None`` means the active one.

        Returns:
            The stored value, or `default`.
        """
        owner, name = self._resolve(dotted, account_id)
        if owner is None or not hasattr(owner, name):
            return default
        return getattr(owner, name)

    def set(self, dotted: str, value: Any, *,
            account_id: str | None = None) -> bool:
        """Write a value by its dotted key, coercing it to the field's type.

        Args:
            dotted: As :meth:`get`.
            value: The new value. Coerced the same way loading coerces, so a
                string ``"4"`` from a line edit becomes the integer 4.
            account_id: Which account an account-scoped key belongs to;
                ``None`` means the active one.

        Returns:
            True when the stored value changed, False when the key is unknown
            or the value was already equal. The return value is what a settings
            page uses to decide whether a save is worth doing.

        Raises:
            ConfigError: If the value cannot be coerced to the field's type.
        """
        owner, name = self._resolve(dotted, account_id)
        if owner is None or not hasattr(owner, name):
            return False
        hints = _hints_for(type(owner))
        if name not in hints:
            return False
        coerced = _coerce(value, hints[name], _default_for(type(owner), name),
                          key=dotted)
        if coerced is _UNSET:
            raise ConfigError(f"{dotted}: cannot accept {value!r}")
        if getattr(owner, name) == coerced:
            return False
        setattr(owner, name, coerced)
        return True

    def _resolve(self, dotted: str, account_id: str | None = None) -> tuple[Any, str]:
        """Map a dotted key onto ``(owning object, field name)``.

        Args:
            dotted: The key.
            account_id: Which account an account-scoped key belongs to.
                ``None`` keeps the historical behaviour — the active account —
                which is right for the tray and wrong for a Settings window
                opened on a *specific* account: with two accounts configured,
                every edit landed on whichever one happened to be active.
        """
        parts = str(dotted).split(".")
        if len(parts) == 1:
            return self, parts[0]
        head, name = parts[0], parts[-1]
        if len(parts) != 2:
            return None, name
        if head == "app":
            return self.app, name
        if head == "advanced":
            return self.advanced, name
        acc = self.account(account_id)
        if acc is None:
            return None, name
        if head == "account":
            return acc, name
        if head in ACCOUNT_SECTIONS:
            return getattr(acc, head), name
        return None, name


# ═════════════════════════════════════════════════════════════════════════════
# Enumerated values — the only legal strings for the choice keys
# ═════════════════════════════════════════════════════════════════════════════

#: Dotted key -> the values ARCHITECTURE §9 allows. Used twice: :func:`load`
#: silently falls back to the default for a value outside its set, and
#: :func:`validate` refuses one. One table, so the two can never disagree.
CHOICES: Final[dict[str, tuple[str, ...]]] = {
    "app.theme": ("system", "light", "dark"),
    "app.accent_source": ("onedrive", "system"),
    "app.animations": ("system", "on", "off"),
    "app.autostart_method": ("systemd", "xdg"),
    "advanced.log_level": ("DEBUG", "INFO", "WARNING"),
    "account.kind": ("personal", "business"),
    "backend.link_scope": ("anonymous", "organization", "users"),
    "backend.link_type": ("view", "edit", "embed"),
    "backend.metadata_permissions": ("off", "read", "read,write"),
    "bandwidth.upload_mode": ("none", "auto", "limit"),
    "files.name_policy": ("windows", "rclone"),
    "conflicts.policy": ("ask", "keep_both"),
    "selective.mode": ("all", "subset"),
    "kfm.method": ("move", "symlink"),
    "offline_folder.conflict_resolve": ("newer", "older", "larger", "smaller",
                                        "none"),
    "offline_folder.conflict_loser": ("num", "pathname", "delete"),
    "sharing.default_scope": ("anonymous", "organization", "users"),
    "sharing.default_type": ("view", "edit", "embed"),
    "vault.backend": ("gocryptfs",),
}

#: The auto-lock intervals the Windows client offers.
VAULT_LOCK_MINUTES: Final[tuple[int, ...]] = (20, 60, 120, 240)

#: Section name -> dataclass, for the account sections.
SECTION_TYPES: Final[dict[str, type]] = {
    "mount": MountSection,
    "backend": BackendSection,
    "files_on_demand": FilesOnDemandSection,
    "bandwidth": BandwidthSection,
    "pause": PauseSection,
    "notifications": NotificationsSection,
    "safety": SafetySection,
    "files": FilesSection,
    "conflicts": ConflictsSection,
    "selective": SelectiveSection,
    "kfm": KfmSection,
    "offline_folder": OfflineFolderSection,
    "sharing": SharingSection,
    "vault": VaultSection,
    "extras": ExtrasSection,
    "integration": IntegrationSection,
    "ui": UiSection,
}

#: The account scalar fields, in §9.2 order.
_ACCOUNT_SCALARS: Final[tuple[str, ...]] = (
    "id", "remote", "kind", "display_name", "email", "drive_id", "drive_type",
    "sync_root", "enabled",
)

#: I1: a backend option must never appear on a command line. Any argv token
#: starting with one of these renames the fs to ``onedrive{HASH}:`` and silently
#: relocates the entire VFS cache.
_BACKEND_FLAG_PREFIXES: Final[tuple[str, ...]] = ("--onedrive-", "--drive-")

#: I12: an interrupted in-place transfer corrupts the destination and the
#: corruption propagates back on the next run.
_BANNED_ARGS: Final[frozenset[str]] = frozenset({"--inplace"})


# ═════════════════════════════════════════════════════════════════════════════
# Coercion — the per-key fallback that keeps loading total
# ═════════════════════════════════════════════════════════════════════════════

_HINT_CACHE: dict[type, dict[str, Any]] = {}
_DEFAULT_CACHE: dict[type, dict[str, Any]] = {}


def _hints_for(cls: type) -> dict[str, Any]:
    """Resolved type hints for a section dataclass, cached."""
    cached = _HINT_CACHE.get(cls)
    if cached is None:
        cached = typing.get_type_hints(cls)
        _HINT_CACHE[cls] = cached
    return cached


def _defaults_for(cls: type) -> dict[str, Any]:
    """A fresh default value per field of a section dataclass."""
    cached = _DEFAULT_CACHE.get(cls)
    if cached is None:
        cached = {}
        for f in fields(cls):
            if f.default is not dataclasses.MISSING:
                cached[f.name] = f.default
            elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                cached[f.name] = f.default_factory  # type: ignore[misc]
            else:
                cached[f.name] = None
        _DEFAULT_CACHE[cls] = cached
    out: dict[str, Any] = {}
    for name, value in cached.items():
        out[name] = value() if callable(value) else value
    return out


def _default_for(cls: type, name: str) -> Any:
    """One field's default value."""
    return _defaults_for(cls).get(name)


def _is_optional(hint: Any) -> tuple[bool, Any]:
    """Split ``X | None`` into ``(True, X)``; anything else into ``(False, hint)``.

    Both spellings of a union are accepted: ``typing.Union`` and ``types.UnionType``
    are the same object from Python 3.14 on, and were not before.
    """
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(args) == 1:
            return True, args[0]
    return False, hint


def _as_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    return _UNSET


def _as_int(value: Any) -> Any:
    if isinstance(value, bool):
        return _UNSET
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return _UNSET
    return _UNSET


def _as_float(value: Any) -> Any:
    if isinstance(value, bool):
        return _UNSET
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return _UNSET
    return _UNSET


def _as_str(value: Any) -> Any:
    if isinstance(value, str):
        return value
    return _UNSET


def _coerce(value: Any, hint: Any, default: Any, *, key: str = "") -> Any:
    """Coerce one JSON value to a field's declared type.

    Args:
        value: The parsed JSON value.
        hint: The resolved type annotation of the field.
        default: The field's default, used to size and type list/dict contents.
        key: The dotted key, used to look up :data:`CHOICES`.

    Returns:
        The coerced value, or the sentinel that tells the caller to fall back to
        the default. Never raises: a bad key must not stop startup.
    """
    optional, inner = _is_optional(hint)
    if value is None:
        return None if optional else _UNSET

    origin = typing.get_origin(inner)
    if origin is list:
        if not isinstance(value, list):
            return _UNSET
        (item_hint,) = typing.get_args(inner) or (str,)
        out: list[Any] = []
        for item in value:
            coerced = _coerce(item, item_hint, None)
            if coerced is _UNSET:
                return _UNSET
            out.append(coerced)
        return out
    if origin is dict:
        if not isinstance(value, dict):
            return _UNSET
        return copy.deepcopy(value)

    if inner is bool:
        return _as_bool(value)
    if inner is int:
        result = _as_int(value)
    elif inner is float:
        result = _as_float(value)
    elif inner is str:
        result = _as_str(value)
    elif inner is typing.Any or inner is object:
        return copy.deepcopy(value)
    else:
        return _UNSET

    if result is _UNSET:
        return _UNSET
    allowed = CHOICES.get(key)
    if allowed is not None and result not in allowed:
        return _UNSET
    return result


def _section_from_dict(cls: type, raw: Any, prefix: str) -> Any:
    """Build a section dataclass, defaulting any key that will not coerce."""
    values = _defaults_for(cls)
    if isinstance(raw, dict):
        hints = _hints_for(cls)
        for name, hint in hints.items():
            if name not in raw:
                continue
            coerced = _coerce(raw[name], hint, values[name],
                              key=f"{prefix}.{name}")
            if coerced is not _UNSET:
                values[name] = coerced
    return cls(**values)


def _account_from_dict(raw: dict[str, Any]) -> AccountConfig:
    """Build one ``accounts[]`` entry, defaulting per bad key."""
    scalars = _defaults_for(AccountConfig)
    hints = _hints_for(AccountConfig)
    for name in _ACCOUNT_SCALARS:
        if name not in raw:
            continue
        coerced = _coerce(raw[name], hints[name], scalars[name],
                          key=f"account.{name}")
        if coerced is not _UNSET:
            scalars[name] = coerced
    # `id` defaults to the remote name, per §9.2.
    if not raw.get("id") and scalars.get("remote"):
        scalars["id"] = scalars["remote"]

    kwargs: dict[str, Any] = {name: scalars[name] for name in _ACCOUNT_SCALARS}
    for section, section_cls in SECTION_TYPES.items():
        kwargs[section] = _section_from_dict(section_cls, raw.get(section),
                                             section)
    return AccountConfig(**kwargs)


def _section_to_dict(section: Any) -> dict[str, Any]:
    """Render a section dataclass as plain JSON values, in field order."""
    out: dict[str, Any] = {}
    for f in fields(section):
        value = getattr(section, f.name)
        out[f.name] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value
    return out


def _account_to_dict(acc: AccountConfig) -> dict[str, Any]:
    """Render one account, scalars first then sections in §9.2 order."""
    out: dict[str, Any] = {name: getattr(acc, name) for name in _ACCOUNT_SCALARS}
    for section in ACCOUNT_SECTIONS:
        out[section] = _section_to_dict(getattr(acc, section))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════

def _migrate_0_to_1(raw: dict[str, Any]) -> dict[str, Any]:
    """Wrap a pre-versioned, account-less document into the v1 shape.

    A document written before ``schema_version`` existed carried the account's
    sections at the top level and had no ``accounts`` list. Moving them under a
    single account is lossless and is what lets an early config survive an
    upgrade instead of being silently replaced by defaults.
    """
    if isinstance(raw.get("accounts"), list):
        return raw
    loose = {name: raw[name] for name in ACCOUNT_SECTIONS if name in raw}
    loose.update({name: raw[name] for name in _ACCOUNT_SCALARS if name in raw})
    if not loose:
        return raw
    upgraded = {k: v for k, v in raw.items()
                if k not in ACCOUNT_SECTIONS and k not in _ACCOUNT_SCALARS}
    upgraded["accounts"] = [loose]
    return upgraded


#: from-version -> the step that produces the next version.
_MIGRATIONS: Final[dict[int, Callable[[dict[str, Any]], dict[str, Any]]]] = {
    0: _migrate_0_to_1,
}


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Bring a parsed document forward to :data:`CONFIG_SCHEMA_VERSION`.

    Args:
        raw: A parsed ``config.json``. Not modified.

    Returns:
        A new dict at the current schema version. A document from a *newer*
        version is returned as-is with the version stamped down: the unknown
        keys it carries are dropped by :meth:`AppConfig.from_dict`, which is a
        safe downgrade — the user loses the newer settings, not the file.
    """
    data = copy.deepcopy(raw)
    version = _as_int(data.get("schema_version", 0))
    if version is _UNSET or version < 0:
        version = 0
    while version < CONFIG_SCHEMA_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            break
        data = step(data)
        version += 1
    data["schema_version"] = CONFIG_SCHEMA_VERSION
    return data


# ═════════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════════

def _fuse_mounts() -> list[tuple[str, Path]]:
    """The live ``fuse.rclone`` mounts. Indirect so tests can substitute it."""
    return paths.fuse_rclone_mounts()


def _is_strictly_under_fuse_mount(path: Path) -> str | None:
    """Is `path` nested *inside* somebody's rclone mount?

    Args:
        path: An absolute path, already ``~``-expanded.

    Returns:
        The offending mountpoint as a string, or ``None``.

    Being *equal* to a mountpoint is the normal case and is fine — the sync root
    IS our own mountpoint. Being strictly below one is the failure: a sync root
    inside a live FUSE tree means every rclone data command would name a path
    under a mount, which is invariant I2.
    """
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        return None
    for _fs, mountpoint in _fuse_mounts():
        if real != mountpoint and real.is_relative_to(mountpoint):
            return str(mountpoint)
    return None


def _validate_app(cfg: AppConfig, problems: list[str]) -> None:
    for key, allowed in CHOICES.items():
        head, _, name = key.partition(".")
        if head not in ("app", "advanced"):
            continue
        owner = cfg.app if head == "app" else cfg.advanced
        value = getattr(owner, name)
        if value not in allowed:
            problems.append(f"{key}: {value!r} is not one of {allowed}")

    ports = cfg.advanced.rc_port_range
    if len(ports) != 2:
        problems.append("advanced.rc_port_range: must be exactly [low, high]")
    else:
        low, high = ports
        if not (1024 <= low <= high <= 65535):
            problems.append(
                f"advanced.rc_port_range: {low}-{high} is not a valid range "
                "inside 1024-65535")
        else:
            clashes = sorted(p for p in RC_FORBIDDEN_PORTS if low <= p <= high)
            if clashes:
                problems.append(
                    f"advanced.rc_port_range: {low}-{high} includes reserved "
                    f"port(s) {clashes} — 5572/5573 belong to the user's own "
                    "rclone and 53682 is rclone's OAuth callback")
    if cfg.advanced.keep_logs_days < 1:
        problems.append("advanced.keep_logs_days: must be at least 1")
    if cfg.advanced.tick_active_ms < 1 or cfg.advanced.tick_idle_ms < 1:
        problems.append("advanced.tick_active_ms/tick_idle_ms: must be positive")
    if cfg.advanced.tick_active_ms > cfg.advanced.tick_idle_ms:
        problems.append(
            "advanced.tick_active_ms: the active tick must be at least as fast "
            "as the idle tick")


def _validate_mount(acc: AccountConfig, problems: list[str]) -> None:
    mount = acc.mount
    if mount.transfers > MAX_TRANSFERS:
        problems.append(
            f"mount.transfers: {mount.transfers} exceeds the hard maximum of "
            f"{MAX_TRANSFERS} — more reliably triggers HTTP 429 on OneDrive")
    if mount.transfers < 1:
        problems.append("mount.transfers: must be at least 1")
    if mount.checkers > MAX_CHECKERS:
        problems.append(
            f"mount.checkers: {mount.checkers} exceeds the hard maximum of "
            f"{MAX_CHECKERS}")
    if mount.checkers < 1:
        problems.append("mount.checkers: must be at least 1")
    if mount.poll_interval_s >= mount.dir_cache_time_s:
        problems.append(
            f"mount.poll_interval_s: {mount.poll_interval_s}s must be strictly "
            f"less than mount.dir_cache_time_s ({mount.dir_cache_time_s}s), or "
            "remote changes are never noticed")
    if mount.poll_interval_s < 1:
        problems.append("mount.poll_interval_s: must be at least 1")
    if mount.read_chunk_streams != 0:
        problems.append(
            "mount.read_chunk_streams: must stay 0 — Graph rate-limits "
            "multi-stream reads hard")
    if mount.tpslimit <= 0:
        problems.append("mount.tpslimit: must be positive")
    if mount.vfs_cache_max_size_gb < 1:
        problems.append("mount.vfs_cache_max_size_gb: must be at least 1")
    if mount.read_chunk_size_mb < 1:
        problems.append("mount.read_chunk_size_mb: must be at least 1")

    for arg in mount.extra_args:
        token = arg.split("=", 1)[0].strip()
        if token.lower() in _BANNED_ARGS:
            problems.append(
                f"mount.extra_args: {arg!r} is banned (I12) — an interrupted "
                "in-place transfer corrupts the destination")
        for prefix in _BACKEND_FLAG_PREFIXES:
            if token.startswith(prefix):
                problems.append(
                    f"mount.extra_args: {arg!r} is a backend option (I1) — it "
                    "renames the fs and silently relocates the whole VFS "
                    "cache; put it in rclone.conf instead")


def _validate_backend(acc: AccountConfig, problems: list[str]) -> None:
    backend = acc.backend
    try:
        chunk = parse_size(backend.chunk_size)
    except ValueError:
        problems.append(
            f"backend.chunk_size: {backend.chunk_size!r} is not a size")
    else:
        if chunk <= 0 or chunk % ONEDRIVE_CHUNK_MULTIPLE:
            problems.append(
                f"backend.chunk_size: {backend.chunk_size!r} ({chunk} bytes) is "
                f"not a positive multiple of 320 KiB "
                f"({ONEDRIVE_CHUNK_MULTIPLE}) — Graph rejects the upload "
                "session outright")
    try:
        parse_size(backend.upload_cutoff)
    except ValueError:
        problems.append(
            f"backend.upload_cutoff: {backend.upload_cutoff!r} is not a size "
            "or 'off'")
    if acc.kind == "personal" or acc.drive_type == "personal":
        if backend.no_versions:
            problems.append(
                "backend.no_versions: must stay false on a personal drive (I9) "
                "— Personal cannot delete versions")
        if backend.hard_delete:
            problems.append(
                "backend.hard_delete: must stay false on a personal drive (I9) "
                "— Personal does not implement permanentDelete")


def _validate_paths(acc: AccountConfig, problems: list[str]) -> None:
    root = acc.resolved_sync_root()
    if not root.is_absolute():
        problems.append(f"account.sync_root: {acc.sync_root!r} is not absolute")
    nested_in = _is_strictly_under_fuse_mount(root)
    if nested_in is not None:
        problems.append(
            f"account.sync_root: {acc.sync_root!r} is nested inside the FUSE "
            f"mount at {nested_in} (I2) — the sync root must BE a mountpoint, "
            "never live under one")
    if acc.offline_folder.enabled:
        # There is no engine behind it any more. This client mounts the remote;
        # the Topology-B "offline folder" kept a second, locally-materialised
        # copy synchronised two-way with bisync, and that engine has been
        # removed — it was never reachable from the UI, never wired into the
        # composition root, and it is the one design that creates a second copy
        # of the user's data on this machine. Refusing loudly is the honest
        # alternative to a switch that is stored, validated and does nothing.
        problems.append(
            "offline_folder.enabled: this client has no two-way sync engine; "
            "the mount at account.sync_root is the only copy it manages")
        offline = acc.resolved_offline_path()
        if offline == root or offline.is_relative_to(root) or root.is_relative_to(offline):
            problems.append(
                f"offline_folder.local_path: {acc.offline_folder.local_path!r} "
                f"must be disjoint from account.sync_root {acc.sync_root!r} "
                "(I2) — bisync may never name a path under the mount")
        nested = _is_strictly_under_fuse_mount(offline)
        if nested is not None:
            problems.append(
                f"offline_folder.local_path: nested inside the FUSE mount at "
                f"{nested} (I2)")


def _validate_policy(acc: AccountConfig, problems: list[str]) -> None:
    for key, allowed in CHOICES.items():
        head, _, name = key.partition(".")
        if head in ("app", "advanced"):
            continue
        owner = acc if head == "account" else getattr(acc, head, None)
        if owner is None:
            continue
        value = getattr(owner, name, None)
        if value not in allowed:
            problems.append(f"{key}: {value!r} is not one of {allowed}")

    band = acc.bandwidth
    for label, kb in (("download_kb", band.download_kb),
                      ("upload_kb", band.upload_kb)):
        if kb is None:
            continue
        if not (BANDWIDTH_FLOOR_KB <= kb <= BANDWIDTH_CEIL_KB):
            problems.append(
                f"bandwidth.{label}: {kb} KB/s is outside "
                f"{BANDWIDTH_FLOOR_KB}-{BANDWIDTH_CEIL_KB}")
    if not (1 <= band.auto_percent <= 100):
        problems.append("bandwidth.auto_percent: must be between 1 and 100")
    if band.limit_download and band.download_kb is None:
        problems.append(
            "bandwidth.limit_download: enabled without a download_kb value")
    if band.upload_mode == "limit" and band.upload_kb is None:
        problems.append("bandwidth.upload_mode: 'limit' without an upload_kb value")

    safety = acc.safety
    if not 0 <= safety.mass_delete_threshold <= 100_000:
        problems.append(
            "safety.mass_delete_threshold: must be between 0 and 100000")
    if not safety.refuse_paths_under_mount:
        problems.append(
            "safety.refuse_paths_under_mount: is read-only and must stay true "
            "(I2)")
    if safety.min_disk_space_mb < 0 or safety.warning_min_disk_space_mb < 0:
        problems.append("safety.min_disk_space_mb: must not be negative")

    files = acc.files
    if files.max_file_bytes > MAX_FILE_BYTES:
        problems.append(
            f"files.max_file_bytes: {files.max_file_bytes} exceeds OneDrive's "
            f"hard limit of {MAX_FILE_BYTES}")
    if files.max_rel_path_chars > MAX_REL_PATH_CHARS:
        problems.append(
            f"files.max_rel_path_chars: {files.max_rel_path_chars} exceeds "
            f"{MAX_REL_PATH_CHARS}")
    if files.max_total_path_chars > MAX_TOTAL_PATH_CHARS:
        problems.append(
            f"files.max_total_path_chars: {files.max_total_path_chars} exceeds "
            f"{MAX_TOTAL_PATH_CHARS}")

    fod = acc.files_on_demand
    if not 1 <= fod.hydrate_concurrency <= MAX_TRANSFERS:
        problems.append(
            f"files_on_demand.hydrate_concurrency: must be between 1 and "
            f"{MAX_TRANSFERS}")
    if fod.auto_free_up_days is not None and fod.auto_free_up_days < 1:
        problems.append(
            "files_on_demand.auto_free_up_days: must be at least 1 day, or null")

    vault = acc.vault
    if vault.auto_lock_minutes not in VAULT_LOCK_MINUTES:
        problems.append(
            f"vault.auto_lock_minutes: {vault.auto_lock_minutes} is not one of "
            f"{VAULT_LOCK_MINUTES}")
    if vault.warn_before_minutes < 0:
        problems.append("vault.warn_before_minutes: must not be negative")

    offline = acc.offline_folder
    if offline.schedule_minutes < 1:
        problems.append("offline_folder.schedule_minutes: must be at least 1")
    if not 0 <= offline.max_delete_percent <= 100:
        problems.append(
            "offline_folder.max_delete_percent: must be between 0 and 100")
    lock_minutes = _lock_minutes(offline.max_lock)
    if lock_minutes is None:
        problems.append(
            f"offline_folder.max_lock: {offline.max_lock!r} is not a duration")
    elif lock_minutes < BISYNC_MAX_LOCK_MIN:
        problems.append(
            f"offline_folder.max_lock: {offline.max_lock!r} is below rclone's "
            f"hard minimum of {BISYNC_MAX_LOCK_MIN}m")
    if offline.check_access and not offline.check_filename.strip():
        problems.append(
            "offline_folder.check_filename: required while check_access is on")

    ui = acc.ui
    if ui.activity_center_width < 200:
        problems.append("ui.activity_center_width: must be at least 200")
    if ui.activity_rows < 1:
        problems.append("ui.activity_rows: must be at least 1")


def _lock_minutes(text: str) -> float | None:
    """Parse a Go duration such as ``"2m"`` / ``"90s"`` / ``"1h"`` into minutes."""
    raw = str(text).strip().lower()
    if not raw:
        return None
    units = {"s": 1 / 60, "m": 1.0, "h": 60.0}
    factor = units.get(raw[-1])
    body = raw[:-1] if factor is not None else raw
    if factor is None:
        factor = 1.0
    try:
        return float(body) * factor
    except ValueError:
        return None


def validate(cfg: AppConfig, *, raise_on_error: bool = True) -> list[str]:
    """Check a configuration against every hard limit and invariant.

    Args:
        cfg: The configuration to check.
        raise_on_error: Raise when problems are found. Defaults to True, so the
            bare ``validate(cfg)`` a caller writes is a rejection, not a
            silently ignored report. Pass False to inspect without raising —
            which is what a settings page does to paint an inline error.

    Returns:
        Every problem found, each a complete sentence naming the dotted key.
        An empty list means the configuration is safe to write.

    Raises:
        ConfigError: When `raise_on_error` and the list is non-empty. The
            message contains every problem, not just the first, so a user
            fixing a hand-edited file sees all of them at once.
    """
    problems: list[str] = []
    if cfg.schema_version != CONFIG_SCHEMA_VERSION:
        problems.append(
            f"schema_version: {cfg.schema_version} is not "
            f"{CONFIG_SCHEMA_VERSION}")
    _validate_app(cfg, problems)

    seen: set[str] = set()
    roots: dict[str, str] = {}
    remotes: dict[str, str] = {}
    for acc in cfg.accounts:
        if not acc.id:
            problems.append("account.id: must not be empty")
        elif acc.id in seen:
            problems.append(f"account.id: {acc.id!r} appears more than once")
        seen.add(acc.id)

        # Two mounts of one remote is the configuration that destroyed a real
        # file on a real account: each mount keeps its own directory cache,
        # neither knows about the other, and a rename made against a stale
        # listing deletes the destination on the server before the move fails.
        # Nothing here checked for it, and every `AccountConfig` defaults to the
        # same `~/OneDrive`, so two accounts added without an explicit root
        # collided by construction. It is refused at the config layer because
        # that is the only place that sees every account at once.
        if acc.enabled:
            root = str(acc.resolved_sync_root())
            if root in roots:
                problems.append(
                    f"account.sync_root: {acc.id!r} and {roots[root]!r} both "
                    f"mount {root} — two mounts of one mountpoint")
            roots[root] = acc.id

            if acc.remote:
                if acc.remote in remotes:
                    problems.append(
                        f"account.remote: {acc.id!r} and {remotes[acc.remote]!r} "
                        f"both mount {acc.remote}: — two live mounts of one "
                        f"remote cannot see each other's renames")
                remotes[acc.remote] = acc.id
        if not acc.remote:
            problems.append("account.remote: must not be empty")
        elif ":" in acc.remote:
            problems.append(
                f"account.remote: {acc.remote!r} must be the bare remote name, "
                "with no colon")
        _validate_mount(acc, problems)
        _validate_backend(acc, problems)
        _validate_paths(acc, problems)
        _validate_policy(acc, problems)

    active = cfg.app.active_account_id
    if active and active not in seen:
        problems.append(
            f"app.active_account_id: {active!r} names no configured account")

    if problems and raise_on_error:
        raise ConfigError("config.json is not valid:\n  - "
                          + "\n  - ".join(problems))
    return problems


def clamp(cfg: AppConfig) -> list[str]:
    """Force every clampable value into its safe range, in place.

    Applied by :func:`load` so that a hand-edited file with ``transfers: 32``
    starts the application at 4 instead of refusing to start at all. Only values
    that *have* a safe answer are clamped: a sync root nested inside somebody
    else's mount has no safe substitute, so it is left alone for
    :func:`validate` to reject.

    Args:
        cfg: The configuration to repair, modified in place.

    Returns:
        The dotted key of every value that had to be changed.
    """
    fixed: list[str] = []

    def note(key: str, owner: Any, name: str, value: Any) -> None:
        if getattr(owner, name) != value:
            setattr(owner, name, value)
            fixed.append(key)

    adv = cfg.advanced
    note("advanced.keep_logs_days", adv, "keep_logs_days",
         max(1, adv.keep_logs_days))
    note("advanced.tick_idle_ms", adv, "tick_idle_ms", max(1, adv.tick_idle_ms))
    note("advanced.tick_active_ms", adv, "tick_active_ms",
         min(max(1, adv.tick_active_ms), adv.tick_idle_ms))
    if len(adv.rc_port_range) != 2 or not (
            1024 <= adv.rc_port_range[0] <= adv.rc_port_range[1] <= 65535) or any(
            adv.rc_port_range[0] <= p <= adv.rc_port_range[1]
            for p in RC_FORBIDDEN_PORTS):
        note("advanced.rc_port_range", adv, "rc_port_range",
             [RC_PORT_RANGE.start, RC_PORT_RANGE.stop - 1])

    for acc in cfg.accounts:
        # There is no two-way sync engine any more, so this cannot be honoured.
        # Clamped rather than only rejected: `validate()` runs on **save**, and
        # a stored `true` that only `validate()` refused would make every future
        # settings write fail while the config still loaded — the exact trap of
        # "clamp cannot repair what validate rejects". Turning it off on load
        # means the refusal in `validate()` only ever sees a hand-edit.
        note("offline_folder.enabled", acc.offline_folder, "enabled", False)
        mount = acc.mount
        note("mount.transfers", mount, "transfers",
             min(max(1, mount.transfers), MAX_TRANSFERS))
        note("mount.checkers", mount, "checkers",
             min(max(1, mount.checkers), MAX_CHECKERS))
        note("mount.read_chunk_streams", mount, "read_chunk_streams", 0)
        note("mount.dir_cache_time_s", mount, "dir_cache_time_s",
             max(2, mount.dir_cache_time_s))
        note("mount.poll_interval_s", mount, "poll_interval_s",
             min(max(1, mount.poll_interval_s), mount.dir_cache_time_s - 1))
        note("mount.vfs_cache_max_size_gb", mount, "vfs_cache_max_size_gb",
             max(1, mount.vfs_cache_max_size_gb))
        note("mount.read_chunk_size_mb", mount, "read_chunk_size_mb",
             max(1, mount.read_chunk_size_mb))
        if mount.tpslimit <= 0:
            note("mount.tpslimit", mount, "tpslimit", DEFAULT_TPSLIMIT)
        safe_args = [a for a in mount.extra_args if not _is_banned_arg(a)]
        note("mount.extra_args", mount, "extra_args", safe_args)

        backend = acc.backend
        try:
            chunk = parse_size(backend.chunk_size)
        except ValueError:
            chunk = -1
        if chunk <= 0 or chunk % ONEDRIVE_CHUNK_MULTIPLE:
            note("backend.chunk_size", backend, "chunk_size",
                 _default_for(BackendSection, "chunk_size"))
        if acc.kind == "personal" or acc.drive_type == "personal":
            note("backend.no_versions", backend, "no_versions", False)
            note("backend.hard_delete", backend, "hard_delete", False)

        band = acc.bandwidth
        for name in ("download_kb", "upload_kb"):
            kb = getattr(band, name)
            if kb is not None:
                note(f"bandwidth.{name}", band, name,
                     min(max(BANDWIDTH_FLOOR_KB, kb), BANDWIDTH_CEIL_KB))
        note("bandwidth.auto_percent", band, "auto_percent",
             min(max(1, band.auto_percent), 100))

        safety = acc.safety
        note("safety.mass_delete_threshold", safety, "mass_delete_threshold",
             min(max(0, safety.mass_delete_threshold), 100_000))
        note("safety.refuse_paths_under_mount", safety,
             "refuse_paths_under_mount", True)
        note("safety.min_disk_space_mb", safety, "min_disk_space_mb",
             max(0, safety.min_disk_space_mb))
        note("safety.warning_min_disk_space_mb", safety,
             "warning_min_disk_space_mb", max(0, safety.warning_min_disk_space_mb))

        files = acc.files
        note("files.max_file_bytes", files, "max_file_bytes",
             min(max(1, files.max_file_bytes), MAX_FILE_BYTES))
        note("files.max_rel_path_chars", files, "max_rel_path_chars",
             min(max(1, files.max_rel_path_chars), MAX_REL_PATH_CHARS))
        note("files.max_total_path_chars", files, "max_total_path_chars",
             min(max(1, files.max_total_path_chars), MAX_TOTAL_PATH_CHARS))

        fod = acc.files_on_demand
        note("files_on_demand.hydrate_concurrency", fod, "hydrate_concurrency",
             min(max(1, fod.hydrate_concurrency), MAX_TRANSFERS))
        if fod.auto_free_up_days is not None and fod.auto_free_up_days < 1:
            note("files_on_demand.auto_free_up_days", fod, "auto_free_up_days",
                 None)

        vault = acc.vault
        if vault.auto_lock_minutes not in VAULT_LOCK_MINUTES:
            note("vault.auto_lock_minutes", vault, "auto_lock_minutes",
                 min(VAULT_LOCK_MINUTES,
                     key=lambda m: abs(m - vault.auto_lock_minutes)))
        note("vault.warn_before_minutes", vault, "warn_before_minutes",
             max(0, vault.warn_before_minutes))

        offline = acc.offline_folder
        note("offline_folder.schedule_minutes", offline, "schedule_minutes",
             max(1, offline.schedule_minutes))
        note("offline_folder.max_delete_percent", offline, "max_delete_percent",
             min(max(0, offline.max_delete_percent), 100))
        minutes = _lock_minutes(offline.max_lock)
        if minutes is None or minutes < BISYNC_MAX_LOCK_MIN:
            note("offline_folder.max_lock", offline, "max_lock",
                 f"{BISYNC_MAX_LOCK_MIN}m")

        ui = acc.ui
        note("ui.activity_center_width", ui, "activity_center_width",
             max(200, ui.activity_center_width))
        note("ui.activity_rows", ui, "activity_rows", max(1, ui.activity_rows))

    return fixed


def _is_banned_arg(arg: str) -> bool:
    """True for an argv token invariant I1 or I12 forbids."""
    token = str(arg).split("=", 1)[0].strip()
    if token.lower() in _BANNED_ARGS:
        return True
    return any(token.startswith(prefix) for prefix in _BACKEND_FLAG_PREFIXES)


# ═════════════════════════════════════════════════════════════════════════════
# Change detection
# ═════════════════════════════════════════════════════════════════════════════

def changed_keys(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Diff two rendered documents into the dotted keys that changed.

    Args:
        old: The previously written document, as :meth:`AppConfig.to_dict`
            renders it.
        new: The document about to be written.

    Returns:
        Dotted keys, in document order: ``"schema_version"``, ``"app.theme"``,
        ``"advanced.log_level"``, ``"account.sync_root"``,
        ``"mount.transfers"``, and the bare ``"accounts"`` when an account was
        added, removed or reordered. Account sections are spelled section-first
        because that is the prefix every consumer filters on.
    """
    keys: list[str] = []
    if old.get("schema_version") != new.get("schema_version"):
        keys.append("schema_version")
    for head in ("app", "advanced"):
        old_section = old.get(head) or {}
        new_section = new.get(head) or {}
        for name in new_section:
            if old_section.get(name, _UNSET) != new_section[name]:
                keys.append(f"{head}.{name}")

    old_accounts = {a.get("id"): a for a in (old.get("accounts") or [])
                    if isinstance(a, dict)}
    new_accounts = {a.get("id"): a for a in (new.get("accounts") or [])
                    if isinstance(a, dict)}
    if set(old_accounts) != set(new_accounts) or (
            [a.get("id") for a in (old.get("accounts") or [])]
            != [a.get("id") for a in (new.get("accounts") or [])]):
        keys.append("accounts")
    for account_id, new_account in new_accounts.items():
        old_account = old_accounts.get(account_id)
        if old_account is None:
            continue
        for name in _ACCOUNT_SCALARS:
            if old_account.get(name, _UNSET) != new_account.get(name):
                keys.append(f"account.{name}")
        for section in ACCOUNT_SECTIONS:
            old_values = old_account.get(section) or {}
            new_values = new_account.get(section) or {}
            for name in new_values:
                if old_values.get(name, _UNSET) != new_values[name]:
                    keys.append(f"{section}.{name}")
    # Two accounts can legitimately change the same section key; the UI cares
    # that "mount.transfers" changed, not how many times.
    seen: set[str] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


# ═════════════════════════════════════════════════════════════════════════════
# The public surface
# ═════════════════════════════════════════════════════════════════════════════

#: The last document written per path, so :func:`save` can diff without a
#: re-read. Falls back to reading the file when this session has not written it.
_LAST_WRITTEN: dict[str, dict[str, Any]] = {}


def defaults(*, with_account: bool = False) -> AppConfig:
    """A configuration of pure ARCHITECTURE §9 defaults.

    Args:
        with_account: Include one default ``onedrive`` account. False by
            default: a fresh install has no account until the OOBE adds one,
            and ``app.first_run_complete`` is what gates that.

    Returns:
        A new :class:`AppConfig`.
    """
    cfg = AppConfig()
    if with_account:
        cfg.accounts.append(AccountConfig())
    return cfg


def load(path: Path | str | None = None) -> AppConfig:
    """Read ``config.json``, falling back rather than failing.

    The recovery ladder, in order:

    1. ``config.json`` parses -> use it.
    2. It does not -> ``config.json.bak`` parses -> use that.
    3. Neither -> a document of pure defaults.

    In every case each individual key is still coerced to its declared type and
    checked against its enumeration, and anything unusable takes its default.
    Values outside a safe range are clamped by :func:`clamp`.

    Args:
        path: The file to read. Defaults to
            :func:`onedriveui.paths.config_file`.

    Returns:
        A complete :class:`AppConfig`. This function never raises: a user whose
        config is broken must still get a running application in which to fix
        it.
    """
    target = Path(path) if path is not None else paths.config_file()
    raw = read_json(target, default=_UNSET)
    if raw is _UNSET or not isinstance(raw, dict):
        backup = target.with_name(target.name + ".bak")
        raw = read_json(backup, default=_UNSET)
    if raw is _UNSET or not isinstance(raw, dict):
        raw = {}
    cfg = AppConfig.from_dict(raw)
    clamp(cfg)
    return cfg


def _is_json_object(raw: bytes) -> bool:
    """Is this a JSON object? The bar for keeping a file as the backup.

    Deliberately shallow: a settings file that parses is worth keeping even if
    a value in it is out of range, because :func:`load` can clamp that. One
    that does not parse is worth nothing, and promoting it would destroy the
    last copy that does.
    """
    try:
        return isinstance(json.loads(raw.decode("utf-8")), dict)
    except (ValueError, UnicodeDecodeError):
        return False


def save(
    cfg: AppConfig,
    path: Path | str | None = None,
    *,
    emit: bool = True,
    check: bool = True,
) -> Path:
    """Validate, then atomically write ``config.json``, then announce the diff.

    Args:
        cfg: The configuration to persist.
        path: Where to write. Defaults to
            :func:`onedriveui.paths.config_file`.
        emit: Emit ``BUS.config_changed`` once per changed dotted key. The
            emission happens *after* the bytes are on disk, so a slot that
            re-reads the file sees the new value.
        check: Run :func:`validate` first. Only ever pass False from a test that
            is deliberately writing a bad file.

    Returns:
        The path written.

    Raises:
        ConfigError: When validation fails. Nothing was written.
        OSError: When the file could not be written. The previous contents and
            the ``.bak`` both survive.
    """
    target = Path(path) if path is not None else paths.config_file()
    if check:
        validate(cfg)

    new = cfg.to_dict()
    key = str(target)
    previous = _LAST_WRITTEN.get(key)
    if previous is None:
        on_disk = read_json(target, default=None)
        previous = (AppConfig.from_dict(on_disk).to_dict()
                    if isinstance(on_disk, dict) else None)

    payload = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    # Only rotate a file that is itself valid JSON into `.bak`. Without this,
    # the first save after recovering from a corrupt `config.json` copied that
    # corrupt file straight over the good backup it had just been rescued by.
    backup_then_write(target, payload, mode=0o600,
                      keep_if=_is_json_object)
    _LAST_WRITTEN[key] = copy.deepcopy(new)

    if emit and previous is not None:
        for dotted in changed_keys(previous, new):
            BUS.config_changed.emit(dotted)
    return target


def account(cfg: AppConfig, account_id: str | None = None) -> AccountConfig | None:
    """Find one account in a configuration.

    A module-level alias for :meth:`AppConfig.account`, so callers that hold
    only the module can write ``config.account(cfg)``.

    Args:
        cfg: The configuration to search.
        account_id: The account id, or ``None`` for the active account.

    Returns:
        The account, or ``None`` when it does not exist.
    """
    return cfg.account(account_id)


def forget_write_cache(path: Path | str | None = None) -> None:
    """Drop the remembered "last written" document.

    Args:
        path: The file to forget, or ``None`` to forget every path.

    Only useful in tests and after an external edit: the cache exists so
    :func:`save` need not re-read the file to compute its diff.
    """
    if path is None:
        _LAST_WRITTEN.clear()
    else:
        _LAST_WRITTEN.pop(str(Path(path)), None)


def section_names() -> tuple[str, ...]:
    """Every section name, top-level first then the account sections.

    Returns:
        The 19 section names ARCHITECTURE §9 defines.
    """
    return ("app", "advanced", *ACCOUNT_SECTIONS)


def dotted_keys(cfg: AppConfig | None = None) -> tuple[str, ...]:
    """Every dotted key a configuration can emit, in document order.

    Args:
        cfg: Ignored except for symmetry with the rest of the module; the key
            set is fixed by the dataclasses, not by the values.

    Returns:
        ``("schema_version", "app.theme", …, "ui.window_geometry")``.
    """
    keys: list[str] = ["schema_version"]
    keys += [f"app.{f.name}" for f in fields(AppSection)]
    keys += [f"advanced.{f.name}" for f in fields(AdvancedSection)]
    keys += [f"account.{name}" for name in _ACCOUNT_SCALARS]
    for name in ACCOUNT_SECTIONS:
        keys += [f"{name}.{f.name}" for f in fields(SECTION_TYPES[name])]
    return tuple(keys)


def iter_sections(acc: AccountConfig) -> Iterable[tuple[str, Any]]:
    """Yield ``(name, section)`` for each of an account's sections, in order."""
    for name in ACCOUNT_SECTIONS:
        yield name, getattr(acc, name)
