"""Shared pytest fixtures for every OneDriveUI work package.

Nothing here needs rclone, a daemon, a network, D-Bus or a display. The two
rules that make that true:

  * `QT_QPA_PLATFORM=offscreen` is set at the TOP of this file, before PySide6
    is imported anywhere, so a QApplication never reaches the compositor.
  * `_isolate_home` is **autouse**: HOME and every XDG variable point into a
    per-test temp directory, so `paths.config_dir()` and friends can never write
    into the developer's real `~/.config`. `paths` caches nothing, which is what
    makes that patch effective.

Fixtures
--------
    qapp           a single offscreen QApplication for the session
    tmp_config     an isolated config dir holding a valid config.json
    tmp_db         a SQLite database with data/schema.sql applied
    fake_rc        a FakeRc daemon with rclone v1.75.0's quirks
    fake_fs        a real sparse vfs/ + vfsMeta/ tree
    fake_services  stub Supervisor / Pinner / IssueEngine / Quota / Pause
    frozen_clock   a deterministic clock behind models.utcnow_iso()
    bus_spy        records BUS signal emissions and disconnects afterwards
    account        the AccountInfo every fake answers about
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import types
from pathlib import Path
from typing import Any, Iterator

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("ONEDRIVEUI_ANIMATIONS", "1")

import pytest  # noqa: E402

from onedriveui import models as _models  # noqa: E402
from onedriveui import paths as _paths  # noqa: E402
from onedriveui.bus import BUS, SIGNAL_NAMES  # noqa: E402
from onedriveui.models import AccountInfo  # noqa: E402
from onedriveui.ui import theme as _theme  # noqa: E402
from tests.fakes.fake_fs import FakeFs, build_fake_fs  # noqa: E402
from tests.fakes.fake_rc import FakeRc, reset_registry  # noqa: E402
from tests.fakes.fake_services import ACCOUNT, FakeServices  # noqa: E402

#: The developer's REAL home, captured before `_isolate_home` redirects it.
#: The only correct way to reach `~/OneDrive` from a test.
REAL_HOME = Path(os.path.expanduser("~"))

#: The package root, for schema and asset lookups.
REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = REPO_ROOT / "onedriveui" / "data" / "schema.sql"
MIGRATIONS_DIR = REPO_ROOT / "onedriveui" / "data" / "migrations"


# ═════════════════════════════════════════════════════════════════════════════
# Environment isolation
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch) -> Iterator[Path]:
    """Point HOME and every XDG variable at a temp tree, for every test.

    All four XDG variables are UNSET on the target machine, so `paths` falls
    back to `~/.config`, `~/.local/share`, `~/.local/state` and `~/.cache`;
    setting them explicitly here exercises the other branch as well and keeps a
    stray `config_dir()` call out of the real home.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(home / "run"))
    (home / "run").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    monkeypatch.delenv("RCLONE_CACHE_DIR", raising=False)
    yield home


@pytest.fixture(autouse=True)
def _deterministic_theme(monkeypatch) -> Iterator[None]:
    """No portal round trip, no `gsettings` subprocess, no cached colour leaking
    between tests. Light theme unless a test asks for dark explicitly."""
    monkeypatch.setenv("ONEDRIVEUI_ANIMATIONS", "1")
    monkeypatch.setattr(_theme, "_DETECTED_DARK", False, raising=False)
    monkeypatch.setattr(_theme, "_ANIMATIONS", True, raising=False)
    yield
    _theme.invalidate_detection()


# ═════════════════════════════════════════════════════════════════════════════
# Qt
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def qapp():
    """One offscreen QApplication for the whole session.

    Qt forbids a second QApplication in a process and crashes if one is deleted
    while widgets survive, so this is deliberately session-scoped and never
    quit: the interpreter exiting is what tears it down.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("OneDriveUI-tests")
    yield app
    app.processEvents()


@pytest.fixture
def qtbot(qapp):
    """A minimal event pump: `qtbot.wait(ms)` and `qtbot.process()`.

    Deliberately NOT pytest-qt — the suite must run with pytest alone.
    """
    from PySide6.QtCore import QEventLoop, QTimer

    class _Bot:
        app = qapp

        @staticmethod
        def process(times: int = 1) -> None:
            for _ in range(times):
                qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

        @staticmethod
        def wait(ms: int = 10) -> None:
            loop = QEventLoop()
            QTimer.singleShot(ms, loop.quit)
            loop.exec()

    return _Bot()


# ═════════════════════════════════════════════════════════════════════════════
# Config
# ═════════════════════════════════════════════════════════════════════════════

def default_config(account_id: str = ACCOUNT.id, sync_root: str = "~/OneDrive") -> dict[str, Any]:
    """The ARCHITECTURE §9 defaults, as a plain dict.

    `config.py` (WP-01) owns the authoritative table; this copy exists so a
    fixture can write a *valid* config.json before that module is written, and
    so a loader can be diffed against the specification.
    """
    return {
        "schema_version": 1,
        "app": {
            "theme": "system", "accent_source": "onedrive", "animations": "system",
            "autostart": True, "autostart_method": "systemd", "start_minimized": True,
            "keep_tray_icon_when_stopped": True, "first_run_complete": False,
            "active_account_id": None, "locale": "system",
        },
        "advanced": {
            "rclone_path": "/usr/bin/rclone", "rc_port_range": [17800, 17899],
            "log_level": "INFO", "keep_logs_days": 14, "tick_idle_ms": 2000,
            "tick_active_ms": 400, "job_expire": "10m",
            "user_agent": "ISV|OneDriveUI|OneDriveUI/0.1.0",
        },
        "accounts": [{
            "id": account_id, "remote": "onedrive", "kind": "personal",
            "display_name": None, "email": None, "drive_id": None,
            "drive_type": None, "sync_root": sync_root, "enabled": True,
            "mount": {
                "enabled": True, "cache_dir": "~/.cache/rclone",
                "vfs_cache_max_size_gb": 50, "vfs_cache_max_age_hours": 720,
                "vfs_cache_min_free_space_gb": 5, "vfs_cache_poll_interval_s": 60,
                "poll_interval_s": 60, "dir_cache_time_s": 3600,
                "attr_timeout_ms": 1000, "read_chunk_size_mb": 32,
                "read_chunk_size_limit_mb": 512, "read_chunk_streams": 0,
                "write_back_s": 5, "handle_caching_s": 5, "transfers": 4,
                "checkers": 8, "tpslimit": 8.0, "tpslimit_burst": 10,
                "retries": 3, "low_level_retries": 10, "umask": "022",
                "file_perms": "0644", "dir_perms": "0755", "fast_fingerprint": True,
                "links": False, "allow_other": False, "warm_up_on_start": False,
                "extra_args": [],
            },
            "backend": {
                "chunk_size": "10M", "upload_cutoff": "off", "delta": True,
                "no_versions": False, "hard_delete": False,
                "link_scope": "anonymous", "link_type": "view", "link_password": "",
                "hash_type": "auto", "metadata_permissions": "off",
                "expose_onenote_files": False, "encoding": None,
            },
            "files_on_demand": {
                "enabled": True, "auto_free_up_days": None,
                "hydrate_concurrency": 3, "pin_all_in_progress": False,
            },
            "bandwidth": {
                "limit_download": False, "download_kb": None, "upload_mode": "none",
                "upload_kb": None, "auto_percent": 70,
            },
            "pause": {
                "manual_until": None, "manual_indefinite": False,
                "on_metered": True, "on_battery_saver": True, "override_until": {},
            },
            "notifications": {
                "paused": True, "shared_or_edited": True, "mass_delete": True,
                "memories": False, "other_accounts": False, "sync_issues": True,
                "conflicts": True, "sync_complete": True,
            },
            "safety": {
                "mass_delete_threshold": 200, "confirm_first_delete": True,
                "min_disk_space_mb": 500, "warning_min_disk_space_mb": 2048,
                "verify_weekly": True, "refuse_paths_under_mount": True,
            },
            "files": {
                "name_policy": "windows",
                "excluded_extensions": [".lnk", ".tmp", ".partial", ".swp"],
                "max_file_bytes": 250_000_000_000, "max_rel_path_chars": 400,
                "max_total_path_chars": 520,
            },
            "conflicts": {"policy": "ask", "suffix_template": "-{device_name}",
                          "device_name": "testhost"},
            "selective": {"mode": "all", "excluded_paths": []},
            "kfm": {"desktop": False, "documents": False, "pictures": False,
                    "music": False, "videos": False, "method": "move",
                    "leave_shortcut": True},
            "offline_folder": {
                "enabled": False, "local_path": "~/OneDrive-Offline",
                "remote_path": "onedrive:Offline", "schedule_minutes": 15,
                "max_delete_percent": 25, "conflict_resolve": "newer",
                "conflict_loser": "pathname", "conflict_suffix": "-{device_name}",
                "check_access": True, "check_filename": "RCLONE_TEST",
                "max_lock": "2m", "resilient": True, "recover": True,
                "track_renames": True, "create_empty_src_dirs": True,
                "compare": "size,modtime", "backup_versions": True,
            },
            "sharing": {"default_scope": "anonymous", "default_type": "view",
                        "default_expiry_days": None},
            "vault": {
                "enabled": False, "backend": "gocryptfs",
                "container_path": "~/.local/share/onedriveui/vault",
                "mount_at": "{sync_root}/Personal Vault",
                "auto_lock_minutes": 20, "warn_before_minutes": 5,
            },
            "extras": {"screenshots": False,
                       "screenshots_dir": "{PICTURES}/Screenshots",
                       "camera_import": False},
            "integration": {"nautilus_extension": True, "sidebar_bookmark": True,
                            "status_column": True},
            "ui": {"activity_center_width": 360, "activity_rows": 50,
                   "window_geometry": {}},
        }],
    }


class TmpConfig:
    """An isolated config directory holding a valid config.json."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.write(data)

    @property
    def dir(self) -> Path:
        return _paths.config_dir()

    @property
    def path(self) -> Path:
        return _paths.config_file()

    @property
    def account(self) -> dict[str, Any]:
        return self.data["accounts"][0]

    def write(self, data: dict[str, Any] | None = None) -> Path:
        """Write config.json the way `config.py` must: 0600, atomic, UTF-8."""
        if data is not None:
            self.data = data
        path = self.path
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path

    def set(self, dotted: str, value: Any) -> Path:
        """`tmp_config.set("app.theme", "dark")` — the dotted keys config_changed
        carries."""
        node: Any = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[0] if isinstance(node, list) else node
            node = node[part]
        node[parts[-1]] = value
        return self.write()

    def corrupt(self) -> Path:
        """Leave invalid JSON behind, so the .bak repair path can be tested."""
        self.path.write_text("{not json", encoding="utf-8")
        return self.path

    def reload(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))


@pytest.fixture
def tmp_config(_isolate_home) -> TmpConfig:
    """A config.json full of the §9 defaults, in an isolated config dir."""
    return TmpConfig(default_config())


# ═════════════════════════════════════════════════════════════════════════════
# Database
# ═════════════════════════════════════════════════════════════════════════════

class TmpDb:
    """`data/schema.sql` applied to a real file, with the fake account seeded.

    Attribute access falls through to the sqlite3 connection, so
    `tmp_db.execute(...)` and `tmp_db.conn` both work.
    """

    def __init__(self, path: Path, account: AccountInfo = ACCOUNT) -> None:
        self.path = path
        self.account_id = account.id
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            "INSERT INTO accounts (id, remote, kind, display_name, email, drive_id,"
            " drive_type, sync_root, enabled, added_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (account.id, account.remote, account.kind.value, account.display_name,
             account.email, account.drive_id, account.drive_type, account.sync_root,
             account.added_at or _models.utcnow_iso()))
        self.conn.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)

    def rows(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def one(self, sql: str, *params: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def tmp_db(_isolate_home) -> Iterator[TmpDb]:
    """A fresh database at `paths.db_file()` with the schema applied."""
    db = TmpDb(_paths.db_file())
    try:
        yield db
    finally:
        db.close()


# ═════════════════════════════════════════════════════════════════════════════
# The fakes
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fake_rc() -> Iterator[FakeRc]:
    """A dict-backed rc daemon, registered so `fake_rc.call_blocking` and the
    module-level `tests.fakes.fake_rc.call_blocking(ep, ...)` both reach it."""
    rc = FakeRc()
    try:
        yield rc
    finally:
        rc.close()
        reset_registry()


@pytest.fixture
def fake_fs(tmp_path) -> FakeFs:
    """A real sparse `vfs/` + `vfsMeta/` tree with all six classify() shapes."""
    return build_fake_fs(tmp_path / "cachehome")


@pytest.fixture
def fake_services() -> Iterator[FakeServices]:
    """Stub Supervisor / Pinner / IssueEngine / QuotaService / PauseManager."""
    services = FakeServices()
    try:
        yield services
    finally:
        services.reset()


@pytest.fixture
def account() -> AccountInfo:
    return ACCOUNT


# ═════════════════════════════════════════════════════════════════════════════
# Time
# ═════════════════════════════════════════════════════════════════════════════

class FrozenClock:
    """A clock that only moves when a test says so.

    Installing it replaces the `datetime` module object *inside*
    `onedriveui.models`, so `models.utcnow_iso()` is frozen no matter how a
    caller imported it — patching the function itself would miss every
    `from onedriveui.models import utcnow_iso`.
    """

    def __init__(self, start: str = "2026-08-31T12:00:00Z",
                 monotonic: float = 10_000.0) -> None:
        self.now = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        self._monotonic = monotonic
        self.start = self.now

    # ── reads ───────────────────────────────────────────────────────────────
    def iso(self) -> str:
        """Exactly what `models.utcnow_iso()` returns while frozen."""
        return self.now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self.now.timestamp()

    # ── writes ──────────────────────────────────────────────────────────────
    def advance(self, seconds: float) -> str:
        """Move both clocks forward. Returns the new ISO stamp."""
        self.now = self.now + _dt.timedelta(seconds=seconds)
        self._monotonic += seconds
        return self.iso()

    sleep = advance

    def set(self, when: str | _dt.datetime) -> str:
        self.now = (when if isinstance(when, _dt.datetime)
                    else _dt.datetime.fromisoformat(str(when).replace("Z", "+00:00")))
        return self.iso()

    # ── installation ────────────────────────────────────────────────────────
    def install(self, monkeypatch) -> "FrozenClock":
        monkeypatch.setattr(_models, "_dt", self._datetime_module(), raising=True)
        return self

    def patch_time(self, monkeypatch, module) -> "FrozenClock":
        """Freeze `time.monotonic/time/sleep` as seen by one module."""
        monkeypatch.setattr(module, "time", self._time_module(), raising=False)
        return self

    def _datetime_module(self) -> types.SimpleNamespace:
        clock = self

        class _FrozenDatetime(_dt.datetime):
            @classmethod
            def now(cls, tz: _dt.tzinfo | None = None) -> _dt.datetime:
                return clock.now if tz is not None else clock.now.replace(tzinfo=None)

            @classmethod
            def utcnow(cls) -> _dt.datetime:
                return clock.now.replace(tzinfo=None)

        return types.SimpleNamespace(
            datetime=_FrozenDatetime, date=_dt.date, time=_dt.time,
            timedelta=_dt.timedelta, timezone=_dt.timezone, UTC=_dt.UTC)

    def _time_module(self) -> types.SimpleNamespace:
        clock = self
        return types.SimpleNamespace(
            monotonic=clock.monotonic, time=clock.time,
            sleep=lambda seconds: clock.advance(seconds),
            perf_counter=clock.monotonic)


@pytest.fixture
def frozen_clock(monkeypatch) -> FrozenClock:
    """`models.utcnow_iso()` stops moving until `frozen_clock.advance()`."""
    return FrozenClock().install(monkeypatch)


# ═════════════════════════════════════════════════════════════════════════════
# Event bus
# ═════════════════════════════════════════════════════════════════════════════

class BusSpy:
    """Records BUS emissions, then disconnects itself.

    `BUS` is a process-wide singleton, so a test that connects without
    disconnecting leaks into every later test — this fixture makes that
    impossible.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[Any, ...]]] = []
        self._connections: list[tuple[Any, Any]] = []

    def watch(self, *names: str) -> "BusSpy":
        for name in names:
            signal = getattr(BUS, name)
            handler = self._make_handler(name)
            signal.connect(handler)
            self._connections.append((signal, handler))
        return self

    def watch_all(self) -> "BusSpy":
        return self.watch(*SIGNAL_NAMES)

    def _make_handler(self, name: str):
        def handler(*args: Any) -> None:
            self.events.append((name, args))
        return handler

    # ── reads ───────────────────────────────────────────────────────────────
    def of(self, name: str) -> list[tuple[Any, ...]]:
        return [args for signal_name, args in self.events if signal_name == name]

    def names(self) -> list[str]:
        return [name for name, _args in self.events]

    def last(self, name: str) -> tuple[Any, ...] | None:
        rows = self.of(name)
        return rows[-1] if rows else None

    def count(self, name: str) -> int:
        return len(self.of(name))

    def clear(self) -> None:
        self.events.clear()

    def disconnect_all(self) -> None:
        for signal, handler in self._connections:
            try:
                signal.disconnect(handler)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()


@pytest.fixture
def bus_spy() -> Iterator[BusSpy]:
    spy = BusSpy()
    try:
        yield spy
    finally:
        spy.disconnect_all()
