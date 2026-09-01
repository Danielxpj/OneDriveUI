"""WP-00 acceptance suite: every frozen contract, proved.

The BUILD_PLAN's acceptance criteria for WP-00 are implemented here one for one,
plus the self-tests for the fixtures the other fourteen work packages depend on.
If this file is green, no downstream package can be blocked by a contract that
is merely *claimed* to be complete:

  * every Fluent token resolves to an opaque #RRGGBB in both themes on both
    surfaces, and an unknown token raises;
  * every SyncState has a headline and a tray icon;
  * every IssueCode has a title and a set of recovery actions;
  * every NotificationId has a toast, and no toast exceeds GNOME's 2 buttons;
  * `errors.classify()` answers correctly for one representative string per row
    of ARCHITECTURE §12.2, and the six benign patterns stay silent;
  * every dataclass is frozen, slotted and picklable;
  * `schema.sql` and its migration execute cleanly;
  * the live `~/OneDrive` fuse.rclone mount is discoverable.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
import pickle
import re
import sqlite3

import pytest
from PySide6.QtCore import Signal
from PySide6.QtGui import QColor

from onedriveui import (
    APP_DISPLAY_NAME, APP_ID, APP_NAME, ORG_NAME, RCLONE_MIN_VERSION, USER_AGENT,
    __version__,
)
from onedriveui import constants, models, paths, strings
from onedriveui.bus import BUS, SIGNAL_NAMES, EventBus
from onedriveui.errors import (
    ACTIONS_FOR, AUTH_PATTERNS, BENIGN_PATTERNS, BisyncCritical, ConfigError,
    DaemonForeign, DaemonUnavailable, MountLost, OneDriveUIError, RcError,
    SafetyRefusal, classify, is_auth_failure, is_benign, is_fatal, is_transient,
)
from onedriveui.models import (
    AccountInfo, ActivityVerb, DiskCacheInfo, FileState, IssueCode, IssueSeverity,
    NotificationId, PauseReason, QuotaInfo, RecoveryAction, SyncState, TrayIcon,
    parse_iso, utcnow_iso,
)
from onedriveui.strings import (
    ACTION_LABEL, ISSUE_TITLE, STATUS_LINE, STATUS_SUB, TOAST, TRAY_FOR_STATE, S,
)
from onedriveui.ui import icons, theme
from tests.conftest import MIGRATIONS_DIR, REAL_HOME, SCHEMA_SQL
from tests.fakes.fake_fs import extents
from tests.fakes.fake_rc import BANNED_PATHS, RcFault
from tests.fakes.fake_rc import call_blocking as fake_call_blocking
from tests.fakes.fake_rc import is_alive as fake_is_alive
from tests.fakes.fake_services import facts_for, snapshot_for

HEX_RE = re.compile(r"^#[0-9A-F]{6}$", re.IGNORECASE)


# ═════════════════════════════════════════════════════════════════════════════
# 0. The modules import at all — and import only what they are allowed to
# ═════════════════════════════════════════════════════════════════════════════

def test_every_contract_module_imports():
    import onedriveui.bus, onedriveui.constants, onedriveui.errors  # noqa: F401
    import onedriveui.models, onedriveui.paths, onedriveui.strings  # noqa: F401
    import onedriveui.ui.icons, onedriveui.ui.theme                 # noqa: F401


def test_models_imports_only_the_stdlib():
    """`models.py` is the one module every other module may import, so it may
    not drag in Qt, gi, SQLite or any I/O of its own."""
    tree = ast.parse(paths.Path(models.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "datetime", "dataclasses", "enum", "typing"}, roots
    assert "PySide6" not in roots and "gi" not in roots
    assert "onedriveui" not in roots, "models.py must be the bottom of the stack"


def test_app_identity_constants():
    assert APP_ID == "onedriveui"
    assert APP_NAME == "OneDriveUI"
    assert APP_DISPLAY_NAME == "OneDrive"
    assert ORG_NAME == "OneDriveUI"
    assert RCLONE_MIN_VERSION == (1, 75, 0)
    #: The decoration format is load-bearing for Microsoft's throttle priority.
    assert USER_AGENT == f"ISV|OneDriveUI|OneDriveUI/{__version__}"
    assert USER_AGENT.count("|") == 2


# ═════════════════════════════════════════════════════════════════════════════
# 1. models — enums, dataclasses, time
# ═════════════════════════════════════════════════════════════════════════════

DATACLASSES = tuple(
    obj for _name, obj in vars(models).items()
    if inspect.isclass(obj) and dataclasses.is_dataclass(obj)
    and obj.__module__ == models.__name__
)

#: Constructor arguments for the dataclasses with required fields.
REQUIRED_ARGS: dict[str, dict] = {
    "AccountInfo": {"id": "onedrive", "remote": "onedrive"},
    "TransferInfo": {"name": "a.bin"},
    "QueueItem": {"name": "a.bin", "id": 1},
    "CacheEntry": {"rel_path": "a.bin"},
    "FileStatus": {"rel_path": "a.bin"},
    "PinRecord": {"account_id": "onedrive", "rel_path": "a.bin"},
    "RemoteFolderNode": {"rel_path": "a.bin", "name": "a.bin"},
    "JobHandle": {"job_id": 7, "execute_id": "uuid", "group": "g", "path": "sync/copy"},
    "NotifySpec": {"id": NotificationId.SYNC_COMPLETE, "summary": "Your files are synced"},
    "RcEndpoint": {"kind": "rcd"},
    "SyncSnapshot": {"state": SyncState.UP_TO_DATE, "facts": models.Facts()},
}


def _instance(cls):
    return cls(**REQUIRED_ARGS.get(cls.__name__, {}))


def test_the_dataclass_set_is_not_empty():
    assert len(DATACLASSES) >= 25, "models.py lost a dataclass"


@pytest.mark.parametrize("cls", DATACLASSES, ids=lambda c: c.__name__)
def test_dataclasses_are_frozen_and_slotted(cls):
    params = cls.__dataclass_params__
    assert params.frozen, f"{cls.__name__} must be frozen=True"
    assert "__slots__" in vars(cls), f"{cls.__name__} must be slots=True"
    assert not hasattr(_instance(cls), "__dict__"), (
        f"{cls.__name__} still carries a __dict__; slots=True was not applied")


@pytest.mark.parametrize("cls", DATACLASSES, ids=lambda c: c.__name__)
def test_dataclasses_are_picklable(cls):
    """These cross a QThread boundary and land in SQLite, so they must survive a
    round trip through pickle unchanged."""
    obj = _instance(cls)
    assert pickle.loads(pickle.dumps(obj)) == obj


@pytest.mark.parametrize("cls", DATACLASSES, ids=lambda c: c.__name__)
def test_frozen_dataclasses_reject_mutation(cls):
    obj = _instance(cls)
    field = dataclasses.fields(cls)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, "mutated")


def test_enum_values_are_unique_and_snake_case():
    for enum_cls in (SyncState, FileState, IssueCode, RecoveryAction, ActivityVerb,
                     NotificationId, PauseReason, IssueSeverity):
        values = [member.value for member in enum_cls]
        assert len(values) == len(set(values)), f"{enum_cls.__name__} has a duplicate value"
        for value in values:
            assert value == value.lower(), f"{enum_cls.__name__}.{value} is not lower case"


def test_utcnow_iso_is_rfc3339_utc_seconds():
    stamp = utcnow_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", stamp), stamp
    parsed = parse_iso(stamp)
    assert parsed is not None and parsed.tzinfo is not None


@pytest.mark.parametrize("text", [
    "2026-08-31T03:24:34Z",
    "2026-08-31T03:24:34.131323516Z",              # rclone's RFC3339Nano
    "2026-08-30T23:26:05.861069681-04:00",         # sidecar ModTime
    "2026-08-30T23:26:05-04:00",
])
def test_parse_iso_accepts_everything_rclone_emits(text):
    assert parse_iso(text) is not None


def test_parse_iso_is_total():
    assert parse_iso(None) is None
    assert parse_iso("") is None
    assert parse_iso("not a date") is None


def test_quota_tiers():
    assert QuotaInfo(total=100, used=10, free=90).tier == "ok"
    assert QuotaInfo(total=100, used=85, free=15).tier == "warn"
    assert QuotaInfo(total=100, used=95, free=5).tier == "critical"
    assert QuotaInfo(total=100, used=100, free=0).tier == "full"
    assert QuotaInfo(total=100, used=100, free=0).is_full
    assert QuotaInfo().pct == 0.0                    # no division by zero


def test_account_fs_string_carries_no_backend_hash():
    """Invariant I1: a `{HASH}` in an fs string means a backend flag leaked onto
    a command line."""
    assert AccountInfo(id="a", remote="onedrive").fs == "onedrive:"
    assert "{" not in AccountInfo(id="a", remote="onedrive").fs


def test_state_groupings_are_subsets_of_syncstate():
    for group in (models.SEVERE_STATES, models.BUSY_STATES, models.PAUSED_STATES):
        assert group <= set(SyncState)
    assert models.PAUSED_STATES <= set(SyncState)


# ═════════════════════════════════════════════════════════════════════════════
# 2. constants
# ═════════════════════════════════════════════════════════════════════════════

def test_rc_ports_never_collide_with_the_users_own_daemons():
    """5572/5573 are already occupied on this machine and 53682 is rclone's
    fixed OAuth callback port."""
    assert constants.RC_FORBIDDEN_PORTS == frozenset({5572, 5573, 53682})
    for port in constants.RC_FORBIDDEN_PORTS:
        assert port not in constants.RC_PORT_RANGE
    assert constants.OAUTH_CALLBACK_PORT == 53682


def test_graph_ceilings():
    assert constants.ONEDRIVE_CHUNK_MULTIPLE == 320 * 1024   # Graph hard requirement
    assert constants.MAX_TRANSFERS == 4
    assert constants.MAX_CHECKERS == 8
    assert constants.MAX_CONCURRENT_PINS <= constants.MAX_TRANSFERS
    assert constants.KB == 1000 and constants.KIB == 1024


def test_mandatory_excludes_are_rclone_filter_lines():
    assert constants.MANDATORY_EXCLUDES[0] == "- *.partial"
    for line in constants.MANDATORY_EXCLUDES:
        assert line.startswith("- "), line
    joined = "\n".join(constants.MANDATORY_EXCLUDES)
    for needle in (".Trash-1000/", constants.REMOTE_TRASH_DIR,
                   constants.REMOTE_VERSIONS_DIR, "*.one", "*.onetoc2", "desktop.ini"):
        assert needle in joined


def test_systemd_ordering_never_names_network_online_target():
    """`network-online.target` does not exist in the --user manager; emitting it
    is silently ignored, which is worse than not emitting it."""
    assert "network-online" not in constants.ORDERING_GUI
    assert "network-online" not in constants.ORDERING_DAEMON
    assert "PartOf=graphical-session.target" in constants.ORDERING_GUI


def test_spinner_period_is_one_second():
    assert constants.SPINNER_FRAME_MS * len(icons.SPINNER_FRAMES) == 1000
    assert icons.SPINNER_PERIOD_MS == 1000


def test_reserved_names_cover_the_windows_device_names():
    for name in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "desktop.ini", ".lock"):
        assert name in constants.RESERVED_NAMES
    assert constants.RESERVED_PREFIXES == ("~$",)
    assert constants.RESERVED_SUBSTRINGS == ("_vti_",)


# ═════════════════════════════════════════════════════════════════════════════
# 3. errors — ARCHITECTURE §12.2, one representative string per row
# ═════════════════════════════════════════════════════════════════════════════

#: (IssueCode, IssueSeverity, raw text, status, direction) — 28 rows, in the
#: order of the table in ARCHITECTURE §12.2.
CLASSIFY_ROWS: tuple[tuple[IssueCode, IssueSeverity, str, int | None, str], ...] = (
    (IssueCode.NAME_INVALID, IssueSeverity.ERROR,
     "nameContainsInvalidCharacters: the name contains invalid characters", None, ""),
    (IssueCode.RESERVED_NAME, IssueSeverity.ERROR,
     "upload failed: this file name is reserved and can't be uploaded", None, ""),
    (IssueCode.PATH_TOO_LONG, IssueSeverity.ERROR,
     "itemNotFound: InnerError.Code == pathIsTooLong", 400, ""),
    (IssueCode.FILE_TOO_LARGE, IssueSeverity.ERROR,
     "entityTooLarge: the upload exceeds the maximum file size", None, ""),
    (IssueCode.QUOTA_EXCEEDED, IssueSeverity.BLOCKING,
     "quotaLimitReached: your OneDrive is out of space", None, ""),
    (IssueCode.DISK_FULL, IssueSeverity.BLOCKING,
     "write /home/u/.cache/rclone/vfs: no space left on device", None, ""),
    (IssueCode.AUTH_EXPIRED, IssueSeverity.BLOCKING,
     "failed to get token: empty token found - please run rclone config reconnect",
     None, ""),
    (IssueCode.AUTH_MFA, IssueSeverity.BLOCKING,
     "AADSTS50076: due to a configuration change made by your administrator", None, ""),
    (IssueCode.AUTH_TENANT_BLOCKED, IssueSeverity.BLOCKING,
     "AADSTS65005: the application needs access to a service that your organization "
     "has not subscribed to", None, ""),
    (IssueCode.THROTTLED, IssueSeverity.WARNING,
     "HTTP 429 activityLimitReached, Retry-After: 41", None, ""),
    (IssueCode.NETWORK_UNREACHABLE, IssueSeverity.WARNING,
     "dial tcp 13.107.42.12:443: connect: connection refused", None, ""),
    (IssueCode.MALWARE_DETECTED, IssueSeverity.ERROR,
     "metadata malware-detected=true: the item was blocked", None, ""),
    (IssueCode.FILE_IN_USE, IssueSeverity.WARNING,
     "open /home/u/OneDrive/a.bin: text file busy", None, ""),
    (IssueCode.PERMISSION_LOST, IssueSeverity.ERROR,
     "accessDenied: you do not have permission to view this item", None, ""),
    (IssueCode.CONFLICT, IssueSeverity.WARNING,
     "bisync: conflict between Path1 and Path2 copies of the file", None, ""),
    (IssueCode.CASE_COLLISION, IssueSeverity.ERROR,
     "two files are same name when lowercase", None, ""),
    (IssueCode.MASS_DELETE_BLOCKED, IssueSeverity.BLOCKING,
     "Safety abort: too many deletes (>25%, 250 of 900), use --force", None, ""),
    (IssueCode.ALL_FILES_CHANGED, IssueSeverity.BLOCKING,
     "Safety abort: all files were changed on Path1", None, ""),
    (IssueCode.CHECK_ACCESS_FAILED, IssueSeverity.BLOCKING,
     "Access test failed: 0 matching files of 2 RCLONE_TEST files", None, ""),
    (IssueCode.NEEDS_RESYNC, IssueSeverity.BLOCKING,
     "Bisync aborted. Must run --resync to recover.", None, ""),
    (IssueCode.BISYNC_LOCK_STUCK, IssueSeverity.BLOCKING,
     "prior lock file found: /home/u/.local/state/onedriveui/bisync/onedrive.lck",
     None, ""),
    (IssueCode.BISYNC_CRITICAL, IssueSeverity.BLOCKING,
     "CRITICAL_ERROR bisync stopped part way through", None, ""),
    (IssueCode.MOUNT_DEAD, IssueSeverity.BLOCKING,
     "statfs /home/u/OneDrive: Transport endpoint is not connected", None, ""),
    (IssueCode.ORPHANED_CACHE, IssueSeverity.INFO,
     "orphaned cache tree beside the live one, 1.2 GB", None, ""),
    (IssueCode.PARTIAL_FILE_FOUND, IssueSeverity.WARNING,
     "found big.bin.partial left by an interrupted transfer", None, ""),
    (IssueCode.ONENOTE_HIDDEN, IssueSeverity.INFO,
     "Notebook.onetoc2 cannot be synced through Graph", None, ""),
    (IssueCode.VAULT_INACCESSIBLE, IssueSeverity.INFO,
     "Personal Vault cannot be opened from this client", None, ""),
    (IssueCode.UPLOAD_FAILED, IssueSeverity.ERROR,
     "an rclone failure nothing in the table recognises", None, "up"),
)


def test_the_taxonomy_table_has_all_28_rows():
    assert len(CLASSIFY_ROWS) == 28


@pytest.mark.parametrize("code,severity,raw,status,direction", CLASSIFY_ROWS,
                         ids=[row[0].name for row in CLASSIFY_ROWS])
def test_classify_matches_the_documented_row(code, severity, raw, status, direction):
    got_code, got_severity, got_actions = classify(raw, status, None, direction)
    assert got_code is code, f"{raw!r} classified as {got_code}"
    assert got_severity is severity
    assert got_actions == ACTIONS_FOR[code]


def test_classify_download_direction_and_unknown_fallback():
    assert classify("unrecognised", direction="down")[0] is IssueCode.DOWNLOAD_FAILED
    assert classify("unrecognised")[0] is IssueCode.UNKNOWN
    assert classify("")[0] is IssueCode.UNKNOWN


def test_classify_uses_http_status_before_text():
    """507 is a FatalError and 429/503 carry Retry-After; both are decided by the
    status code, whatever the body says."""
    assert classify("anything", 507)[0] is IssueCode.QUOTA_EXCEEDED
    assert classify("anything", 429)[0] is IssueCode.THROTTLED
    assert classify("anything", 503)[0] is IssueCode.THROTTLED
    assert classify("anything", 429)[2] == ()          # nothing for the user to do


def test_classify_never_raises():
    for raw in ("", "   ", "\x00\x01", "%s %d {}", "a" * 10_000):
        code, severity, actions = classify(raw)
        assert isinstance(code, IssueCode)
        assert isinstance(severity, IssueSeverity)
        assert isinstance(actions, tuple)


BENIGN_LINES = (
    "ERROR : Ignoring --track-renames as it doesn't work with copy or move, only sync",
    "WARNING  listing try 2 failed - retrying",
    "NOTICE: a.txt: Skipped copy as --dry-run is set",
    "INFO : vfs cache: detected external removal of cache file",
    "WARNING: Can't follow symlink without -L/--copy-links",
    "INFO : bisync: lock file renewed for another 2m0s",
)


def test_there_are_exactly_six_benign_patterns():
    assert len(BENIGN_PATTERNS) == 6 == len(BENIGN_LINES)


@pytest.mark.parametrize("line", BENIGN_LINES)
def test_is_benign_suppresses_the_six_noisy_lines(line):
    assert is_benign(line) is True


def test_is_benign_does_not_swallow_a_real_error():
    assert is_benign("quotaLimitReached") is False
    assert is_benign("Safety abort: too many deletes") is False


@pytest.mark.parametrize("code", list(IssueCode), ids=lambda c: c.name)
def test_actions_for_covers_every_issue_code(code):
    actions = ACTIONS_FOR[code]
    assert isinstance(actions, tuple)
    for action in actions:
        assert isinstance(action, RecoveryAction)
        assert ACTION_LABEL[action]


def test_auth_patterns_and_helpers():
    assert is_auth_failure("AADSTS65005 something")
    assert is_auth_failure("Empty token found")          # matching is case folded
    assert not is_auth_failure("no space left on device")
    for needle, code in AUTH_PATTERNS:
        assert needle == needle.lower(), "AUTH_PATTERNS is matched against .lower()"
        assert classify(needle.upper())[0] is code


def test_fatal_and_transient_sets():
    assert is_fatal(IssueCode.QUOTA_EXCEEDED)
    assert is_fatal(IssueCode.PATH_TOO_LONG)
    assert is_fatal(IssueCode.AUTH_TENANT_BLOCKED)
    assert not is_fatal(IssueCode.THROTTLED)
    assert is_transient(IssueCode.THROTTLED)
    assert is_transient(IssueCode.NETWORK_UNREACHABLE)
    assert is_transient(IssueCode.FILE_IN_USE)


def test_rc_error_carries_the_four_key_envelope():
    body = {"error": "job not found", "input": {"jobid": 99999},
            "path": "job/status", "status": 500}
    err = RcError("job/status", 500, body)
    assert err.message == "job not found"
    assert err.is_job_expired
    #: NOTE the overlap: `is_not_found` matches ANY "not found" text, so it is
    #: also True for an expired job. Callers must test `is_job_expired` FIRST
    #: (and then compare execute_id) before treating a failure as a missing path.
    assert err.is_not_found
    assert not RcError("operations/stat", 500, {"error": "internal"}).is_not_found
    assert set(err.body) == {"error", "input", "path", "status"}
    assert RcError("operations/list", 404, {"error": "directory not found"}).is_not_found
    assert RcError("x", 500).message == "HTTP 500"        # empty body still reads


def test_exception_hierarchy():
    for cls in (RcError, DaemonForeign, MountLost, BisyncCritical, SafetyRefusal,
                ConfigError):
        assert issubclass(cls, OneDriveUIError)
    assert issubclass(DaemonUnavailable, RcError)
    refusal = SafetyRefusal("I2", "path is under a fuse mount")
    assert refusal.invariant == "I2" and "I2" in str(refusal)
    critical = BisyncCritical("CRITICAL_ERROR", "tail")
    assert critical.verdict == "CRITICAL_ERROR"


# ═════════════════════════════════════════════════════════════════════════════
# 4. strings
# ═════════════════════════════════════════════════════════════════════════════

FORMAT_ARGS = {"n": 3, "done": 2, "total": 5, "bytes": "1.2 GB", "size": "4.8 GB",
               "used": "252 GB", "hh": 1, "mm": 30, "name": "Report.docx",
               "loser": "Report-host.docx", "who": "Alex", "pct": 92,
               "folders": "Desktop", "year": 2019}


@pytest.mark.parametrize("state", list(SyncState), ids=lambda s: s.name)
def test_status_line_covers_every_sync_state(state):
    assert state in STATUS_LINE, f"STATUS_LINE has no wording for {state.name}"
    text = strings.status_line(state, **FORMAT_ARGS)
    assert text and "{" not in text and "}" not in text


@pytest.mark.parametrize("state", list(SyncState), ids=lambda s: s.name)
def test_tray_for_state_covers_every_sync_state(state):
    assert state in TRAY_FOR_STATE, f"TRAY_FOR_STATE has no icon for {state.name}"
    tray = TRAY_FOR_STATE[state]
    assert isinstance(tray, TrayIcon)
    #: NOT_RUNNING is the only state that registers no tray item at all.
    assert (tray is TrayIcon.NONE) == (state is SyncState.NOT_RUNNING)


@pytest.mark.parametrize("state", sorted(STATUS_SUB, key=lambda s: s.value),
                         ids=lambda s: s.name)
def test_status_sub_formats_without_leftovers(state):
    text = strings.status_sub(state, **FORMAT_ARGS)
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("code", list(IssueCode), ids=lambda c: c.name)
def test_issue_title_covers_every_issue_code(code):
    assert code in ISSUE_TITLE, f"ISSUE_TITLE has no wording for {code.name}"
    text = strings.issue_title(code, **FORMAT_ARGS)
    assert text and "{" not in text


@pytest.mark.parametrize("nid", list(NotificationId), ids=lambda n: n.name)
def test_toast_covers_every_notification_id(nid):
    assert nid in TOAST, f"TOAST has no entry for {nid.name}"
    summary, body, actions = strings.toast(nid, **FORMAT_ARGS)
    assert summary and "{" not in summary
    assert "{" not in body
    #: GNOME renders about three buttons; NotifySpec caps at two.
    assert len(actions) <= 2, f"{nid.name} offers {len(actions)} actions"
    for action_id, label in actions:
        assert action_id and label
        assert action_id.islower()


@pytest.mark.parametrize("action", list(RecoveryAction), ids=lambda a: a.name)
def test_action_label_covers_every_recovery_action(action):
    assert strings.action_label(action)


@pytest.mark.parametrize("state", list(FileState), ids=lambda s: s.name)
def test_file_state_label_covers_every_file_state(state):
    assert state.value in strings.FILE_STATE_LABEL


@pytest.mark.parametrize("verb", list(ActivityVerb), ids=lambda v: v.name)
def test_verb_label_covers_every_activity_verb(verb):
    assert strings.VERB_LABEL[verb.value]


def test_t_never_raises_on_a_missing_placeholder():
    """A wording bug must not crash the UI: `t()` returns the template."""
    assert strings.t("{missing} files") == "{missing} files"
    assert strings.t("{n} files", n=2) == "2 files"


def test_status_line_falls_back_rather_than_raising():
    assert strings.status_line("not-a-state") == STATUS_LINE[SyncState.NOT_RUNNING]
    assert strings.issue_title("not-a-code") == ISSUE_TITLE[IssueCode.UNKNOWN]
    assert strings.status_sub(SyncState.MOUNTING) == ""


def test_S_namespace_aliases_every_table():
    assert S.STATUS_LINE is STATUS_LINE
    assert S.TOAST is TOAST
    assert S.TRAY_FOR_STATE is TRAY_FOR_STATE
    assert S.MENU.PAUSE == "Pause syncing"
    assert S.t("{n}", n=1) == "1"


def test_oobe_has_seven_pages():
    assert len(strings.OOBE.PAGES) == 7
    assert strings.OOBE.PAGES[0] == "welcome" and strings.OOBE.PAGES[-1] == "done"
    assert len(strings.OOBE.TUTORIAL_SLIDES) == 4


def test_linux_only_explanations_exist_for_every_impossible_control():
    """Where rclone cannot do what Windows does, the UI must say so rather than
    pretend (§14.1)."""
    for text in (strings.DIALOG.REMOVE_LINK_WHY, strings.DIALOG.VERSION_HISTORY_WHY,
                 strings.DIALOG.RECYCLE_BIN_WHY, strings.DIALOG.VAULT_CLOUD_WHY,
                 strings.DIALOG.DU_ON_MOUNT_NOTE):
        #: A whole sentence saying WHY, not a bare "unavailable".
        assert text.endswith(".") and len(text.split()) >= 8
    assert strings.DIALOG.UNAVAILABLE_PREFIX.endswith(": ")


# ═════════════════════════════════════════════════════════════════════════════
# 5. paths
# ═════════════════════════════════════════════════════════════════════════════

def test_xdg_directories_are_created_0700(tmp_config):
    for getter in (paths.config_dir, paths.data_dir, paths.state_dir,
                   paths.cache_dir, paths.runtime_dir):
        directory = getter()
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700, directory


def test_shared_xdg_directories_are_not_tightened(tmp_config):
    """`~/.config/systemd/user` belongs to the desktop, not to us."""
    for getter in (paths.systemd_user_dir, paths.applications_dir,
                   paths.autostart_dir, paths.icon_status_dir):
        directory = getter()
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o755, directory


def test_paths_are_not_cached_across_an_environment_change(monkeypatch, tmp_path):
    """Tests monkeypatch HOME; a cached Path would outlive the patch."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "one"))
    first = paths.config_dir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "two"))
    assert paths.config_dir() != first


def test_file_paths_hang_off_their_directories(tmp_config):
    assert paths.config_file().parent == paths.config_dir()
    assert paths.config_file().name == "config.json"
    assert paths.db_file() == paths.data_dir() / "state.db"
    assert paths.log_file() == paths.log_dir() / "app.log"
    assert paths.endpoints_file() == paths.runtime_dir() / "endpoints.json"
    assert paths.ipc_socket().is_absolute()
    assert paths.ui_socket().is_absolute()
    assert paths.filters_md5_file("onedrive").name == "filters-onedrive.txt.md5"
    #: bisync state must NOT live in rclone's own cache: cache cleaning would
    #: destroy the .lst files that ARE the sync state.
    assert "rclone" not in str(paths.bisync_workdir("onedrive"))
    assert paths.bisync_workdir("onedrive").is_relative_to(paths.state_dir())


def test_rclone_paths_honour_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "custom.conf"))
    assert paths.rclone_conf() == tmp_path / "custom.conf"
    monkeypatch.delenv("RCLONE_CONFIG")
    assert paths.rclone_conf().name == "rclone.conf"
    monkeypatch.setenv("RCLONE_CACHE_DIR", str(tmp_path / "cache"))
    assert paths.rclone_cache_dir() == tmp_path / "cache"


def test_rclone_cache_dir_is_never_created_by_us(monkeypatch, tmp_path):
    """rclone owns that tree; we only read it."""
    monkeypatch.setenv("RCLONE_CACHE_DIR", str(tmp_path / "not-created"))
    assert not paths.rclone_cache_dir().exists()


def test_gtk_bookmarks_covers_gtk3_and_gtk4(tmp_config):
    files = paths.gtk_bookmarks()
    assert len(files) == 2
    assert {f.parent.name for f in files} == {"gtk-3.0", "gtk-4.0"}


def test_mount_point_defaults_and_expands(tmp_config):
    assert paths.mount_point(None) == paths.default_sync_root()
    assert paths.mount_point("") == paths.default_sync_root()
    assert paths.mount_point("~/Elsewhere") == paths.Path.home() / "Elsewhere"
    assert paths.vault_mountpoint("~/OneDrive").name == "Personal Vault"


def test_proc_mount_octal_unescaping():
    assert paths._unescape_mount_field(r"/home/u/My\040Drive") == "/home/u/My Drive"
    assert paths._unescape_mount_field(r"a\134b") == "a\\b"


@pytest.mark.live
def test_fuse_rclone_mounts_finds_the_live_onedrive_mount():
    """WP-00 acceptance: the real `~/OneDrive` mount is discoverable through
    /proc/self/mounts — `mount/listmounts` is blind to CLI-started mounts and is
    banned by invariant I7."""
    mounts = paths.fuse_rclone_mounts()
    assert mounts, "no fuse.rclone mount found in /proc/self/mounts"
    expected = REAL_HOME / "OneDrive"
    points = [point for _fs, point in mounts]
    assert expected in points, f"{expected} not among {points}"
    fs_name = next(fs for fs, point in mounts if point == expected)
    #: The device field carries rclone's {HASH} suffix; strip it for display,
    #: never before comparing.
    assert fs_name.startswith("onedrive")
    assert fs_name.endswith(":")


@pytest.mark.live
def test_is_under_fuse_mount_answers_for_the_live_mount():
    root = REAL_HOME / "OneDrive"
    assert paths.is_under_fuse_mount(root) is True
    #: A path that does not exist yet still answers correctly.
    assert paths.is_under_fuse_mount(root / "nope" / "deeper.txt") is True
    assert paths.is_under_fuse_mount(REAL_HOME) is False
    assert paths.is_under_fuse_mount("/tmp") is False


# ═════════════════════════════════════════════════════════════════════════════
# 6. ui/theme — the acceptance criterion with the most permutations
# ═════════════════════════════════════════════════════════════════════════════

TOKEN_CASES = [(token, dark, surface)
               for token in theme.TOKENS
               for dark in (False, True)
               for surface in ("base", "layer")]


def test_the_token_table_is_complete():
    assert len(theme.TOKENS) >= 40
    assert len(TOKEN_CASES) == len(theme.TOKENS) * 4


@pytest.mark.parametrize("token,dark,surface", TOKEN_CASES,
                         ids=[f"{t}-{'dark' if d else 'light'}-{s}"
                              for t, d, s in TOKEN_CASES])
def test_every_token_resolves_to_an_opaque_hex(token, dark, surface):
    value = theme.T(token, dark=dark, on=surface)
    assert HEX_RE.match(value), f"{token}/{dark}/{surface} = {value!r}"
    colour = QColor(value)
    assert colour.isValid(), f"QColor rejected {value!r}"
    #: QColor("#RRGGBBAA") is silently WRONG in Qt; every literal must be opaque.
    assert colour.alpha() == 255


def test_unknown_token_raises_key_error():
    with pytest.raises(KeyError) as excinfo:
        theme.T("NoSuchFluentToken")
    assert "theme.TOKENS" in str(excinfo.value)


def test_unknown_surface_raises_value_error():
    with pytest.raises(ValueError):
        theme.T("TextFillColorPrimary", on="mica")
    with pytest.raises(ValueError):
        theme.surface("mica")


def test_token_maps_match_the_resolver():
    for token in theme.TOKENS:
        assert theme.TOKENS_LIGHT[token] == theme.T(token, dark=False, on="base")
        assert theme.TOKENS_LIGHT_LAYER[token] == theme.T(token, dark=False, on="layer")
        assert theme.TOKENS_DARK[token] == theme.T(token, dark=True, on="base")
        assert theme.TOKENS_DARK_LAYER[token] == theme.T(token, dark=True, on="layer")


def test_light_and_dark_actually_differ():
    """A table that accidentally copies one theme into the other still passes a
    hex check, so compare the two."""
    differing = sum(1 for token in theme.TOKENS
                    if theme.T(token, dark=False) != theme.T(token, dark=True))
    assert differing >= len(theme.TOKENS) * 0.8


def test_primary_text_is_not_pure_black_or_white_in_light_theme():
    assert theme.T("TextFillColorPrimary", dark=False) == "#1A1A1A"
    assert theme.T("TextFillColorPrimary", dark=True) == "#FFFFFF"


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("role", theme.ACCENT_ROLES)
def test_accent_roles_resolve(role, dark):
    value = theme.accent(role, dark=dark)
    assert HEX_RE.match(value), value
    assert QColor(value).isValid()


def test_accent_uses_a_different_ramp_stop_per_theme():
    """WinUI takes Dark1 in light theme and Light2 in dark; using the base blue
    in both is wrong in both."""
    assert theme.accent("rest", dark=False) == theme.ACCENT_RAMP_ONEDRIVE["Dark1"]
    assert theme.accent("rest", dark=True) == theme.ACCENT_RAMP_ONEDRIVE["Light2"]
    #: Text on the accent is BLACK in dark theme — the dark accent is light blue.
    assert theme.accent("text", dark=False) == "#FFFFFF"
    assert theme.accent("text", dark=True) == "#000000"


def test_unknown_accent_role_raises():
    with pytest.raises(KeyError):
        theme.accent("sparkle")


def test_hover_and_pressed_are_the_composited_rest_colour():
    for dark, ground in ((False, theme.BASE_LIGHT), (True, theme.BASE_DARK)):
        rest = theme.accent("rest", dark=dark)
        assert theme.mix(rest, ground, theme.ACCENT_HOVER_ALPHA) == theme.accent("hover", dark=dark)
        assert theme.mix(rest, ground, theme.ACCENT_PRESSED_ALPHA) == theme.accent("pressed", dark=dark)


def test_accent_ramps_are_seven_stops_of_hex():
    for ramp in (theme.ACCENT_RAMP_SYSTEM, theme.ACCENT_RAMP_ONEDRIVE,
                 theme.accent_ramp(), theme.accent_ramp(system=True)):
        assert set(ramp) == {"Light3", "Light2", "Light1", "Base",
                             "Dark1", "Dark2", "Dark3"}
        assert all(HEX_RE.match(v) for v in ramp.values())
    assert theme.ACCENT_RAMP_ONEDRIVE["Base"] == "#0364B8"     # OneDrive brand blue
    assert theme.ACCENT_RAMP_SYSTEM["Base"] == "#0078D4"       # Windows default


def test_gnome_accents_are_the_nine_verified_colours():
    assert len(theme.GNOME_ACCENTS) == 9
    assert theme.GNOME_ACCENTS["blue"] == "#3584E4"
    assert all(HEX_RE.match(v) for v in theme.GNOME_ACCENTS.values())


def test_mix_is_the_documented_composite():
    assert theme.mix("#FFFFFF", "#000000", 0.5) == "#808080"
    assert theme.mix("#FFFFFF", "#000000", 1.0) == "#FFFFFF"
    assert theme.mix("#FFFFFF", "#000000", 0.0) == "#000000"
    with pytest.raises(ValueError):
        theme.mix("not-a-colour", "#000000", 0.5)


def test_surfaces_are_opaque_and_distinct():
    assert theme.base(dark=False) == theme.BASE_LIGHT == "#F3F3F3"
    assert theme.base(dark=True) == theme.BASE_DARK == "#202020"
    assert theme.layer(dark=False) == theme.LAYER_LIGHT == "#FFFFFF"
    assert theme.layer(dark=True) == theme.LAYER_DARK == "#2C2C2C"
    assert theme.surface("layer", dark=True) == theme.LAYER_DARK


def test_logo_geometry_is_wider_than_tall():
    """The flat 2019 mark, never stretched to square."""
    x, y, w, h = theme.LOGO_VIEWBOX
    assert (x, y, w, h) == (0.0, 5.5, 32.0, 20.5)
    assert w > h
    assert set(theme.LOGO_COLORS) == {"rear_top", "left", "right", "front"}
    assert all(HEX_RE.match(v) for v in theme.LOGO_COLORS.values())


def test_type_ramp_and_geometry():
    for role, row in theme.TYPE.items():
        px, line, weight = row
        assert 0 < px <= 68 and line > px and 100 <= weight <= 900, role
        assert theme.font_px(role) == px
        assert theme.line_height(role) == line
        assert theme.weight(role) == weight
    #: The WinUI ramp, verbatim: caption 12 -> display 68.
    assert theme.TYPE["caption"] == (12, 16, 400)
    assert theme.TYPE["body"] == (14, 20, 400)
    assert theme.TYPE["display"][0] == 68
    with pytest.raises(KeyError):
        theme.font_px("no-such-role")
    assert theme.RADII["control"] == 4
    assert all(v >= 0 for v in theme.SPACING.values())


def test_motion_durations_and_curves():
    assert theme.duration("fast") == theme.DURATION["fast"]
    assert theme.duration(250) == 250
    with pytest.raises(KeyError):
        theme.duration("glacial")
    curve = theme.curve(next(iter(theme.CURVES)))
    assert curve is not None
    with pytest.raises(KeyError):
        theme.curve("bouncy")


def test_animations_can_be_switched_off_by_the_desktop(monkeypatch):
    monkeypatch.setenv("ONEDRIVEUI_ANIMATIONS", "0")
    theme.invalidate_detection()
    assert theme.animations_enabled() is False
    assert theme.duration("fast") == 0
    monkeypatch.setenv("ONEDRIVEUI_ANIMATIONS", "1")
    theme.invalidate_detection()
    assert theme.animations_enabled() is True


def test_shadows_carry_a_per_theme_alpha():
    """QSS drops box-shadow entirely, so every shadow is a
    QGraphicsDropShadowEffect built from this table — and a dark surface needs a
    denser shadow than a light one to read at all."""
    for name in theme.SHADOWS:
        blur, dy, light_alpha = theme.shadow(name, dark=False)
        _blur, _dy, dark_alpha = theme.shadow(name, dark=True)
        assert blur > 0 and dy >= 0
        assert 0 <= light_alpha <= 255 and 0 <= dark_alpha <= 255
        assert dark_alpha > light_alpha, name
    assert set(theme.SHADOWS) == {"card", "flyout", "dialog"}
    with pytest.raises(KeyError):
        theme.shadow("no-such-shadow")


@pytest.mark.parametrize("dark", [False, True])
def test_stylesheet_builds_and_avoids_properties_qss_ignores(dark):
    sheet = theme.stylesheet(dark=dark)
    assert len(sheet) > 2000
    assert theme.base(dark=dark) in sheet
    #: QSS silently drops these; a rule that uses one is dead code on screen.
    for ignored in ("box-shadow", "transition:", "backdrop-filter", "z-index",
                    "text-overflow", "calc(", "linear-gradient("):
        assert ignored not in sheet, f"stylesheet uses {ignored}, which QSS ignores"
    #: No raw "#RRGGBBAA" literals — Qt reads those as #AARRGGBB.
    assert not re.search(r"#[0-9A-Fa-f]{8}\b", sheet)


def test_stylesheet_is_cached_per_theme():
    assert theme.stylesheet(dark=False) is theme.stylesheet(dark=False)
    assert theme.stylesheet(dark=False) != theme.stylesheet(dark=True)


def test_theme_manager_exposes_both_contract_names():
    assert theme.ThemeWatcher is theme.ThemeManager
    assert theme.manager() is None            # nothing started one in tests
    assert theme.ThemeManager.DEBOUNCE_MS == 60


# ═════════════════════════════════════════════════════════════════════════════
# 7. ui/icons
# ═════════════════════════════════════════════════════════════════════════════

def test_tray_icon_names_cover_every_tray_icon():
    for tray in TrayIcon:
        if tray is TrayIcon.NONE:
            assert tray.value == ""
            continue
        assert tray.value in icons.TRAY_ICON_NAMES, tray


def test_spinner_frames_are_eight_named_files():
    assert len(icons.SPINNER_FRAMES) == 8
    assert icons.SPINNER_FRAMES[0] == "onedriveui-syncing-1"
    assert icons.SPINNER_FRAMES[-1] == "onedriveui-syncing-8"


@pytest.mark.parametrize("state", list(SyncState), ids=lambda s: s.name)
def test_tray_icon_name_for_every_state(state):
    tray = TRAY_FOR_STATE[state]
    name = icons.tray_icon_name(tray)
    if tray is TrayIcon.NONE:
        assert name == ""            # register no StatusNotifierItem at all
    else:
        assert name in icons.THEME_ICON_NAMES


def test_tray_icon_name_cycles_the_spinner():
    for frame in range(20):
        name = icons.tray_icon_name(TrayIcon.SYNCING, frame)
        assert name == icons.SPINNER_FRAMES[frame % 8]


@pytest.mark.parametrize("state", list(FileState), ids=lambda s: s.name)
def test_emblem_and_glyph_for_every_file_state(state):
    stem = icons.emblem_name(state)
    if state is FileState.UNKNOWN:
        assert stem == ""
    else:
        assert stem in icons.EMBLEM_STEMS
        #: Nautilus tries emblem-NAME first, so the FILE carries the prefix while
        #: add_emblem() is given the bare stem.
        assert icons.emblem_icon_name(stem) == f"emblem-{stem}"
    assert icons.GLYPH_FOR_FILE_STATE[state] in icons.GLYPHS


def test_icon_registry_is_exhaustive_and_unique():
    assert len(icons.ICON_NAMES) == len(set(icons.ICON_NAMES))
    assert icons.APP_ICON_NAME == APP_ID
    for name in icons.THEME_ICON_NAMES:
        assert name in icons.ICON_NAMES


@pytest.mark.parametrize("name", icons.THEME_ICON_NAMES)
def test_every_theme_icon_renders_valid_svg(name):
    data = icons.svg_bytes(icons._category_for(name), name)
    assert data.startswith(b"<svg") and data.rstrip().endswith(b"</svg>")


@pytest.mark.parametrize("key", sorted(icons.GLYPHS))
def test_every_glyph_key_renders(key, qapp):
    assert icons.glyph_stem(key)
    pixmap_icon = icons.icon(key, 16)
    assert not pixmap_icon.isNull()
    assert not pixmap_icon.pixmap(16, 16).isNull()


def test_icon_rejects_a_typo_and_a_non_native_size(qapp):
    with pytest.raises(KeyError):
        icons.icon("no-such-glyph")
    with pytest.raises(ValueError):
        icons.icon("settings", 17)          # never scale a 24 px glyph to 17
    with pytest.raises(KeyError):
        icons.any_icon("no-such-icon")


def test_any_icon_snaps_to_a_native_size(qapp):
    assert not icons.any_icon("settings", 17).isNull()
    assert not icons.any_icon("onedriveui-synced").isNull()


def test_render_svg_and_logo(qapp):
    pixmap = icons.render_svg(icons.logo_svg(), 32)
    assert pixmap.width() == 32 and not pixmap.isNull()
    logo = icons.logo(32)
    assert not logo.isNull()
    #: The mark is wider than tall and must not be letterboxed into a square.
    sizes = logo.availableSizes()
    assert sizes and all(s.width() >= s.height() for s in sizes)


def test_badged_pixmaps(qapp):
    for badge in icons.BADGES:
        pixmap = icons.badged("onedriveui-synced", badge, 24)
        assert pixmap.width() == 24 and not pixmap.isNull()
    with pytest.raises(KeyError):
        icons.badged("onedriveui-synced", "no-such-badge", 16)
    with pytest.raises(ValueError):
        icons.badged("onedriveui-synced", "ok", 16, corner="tr")


def test_tray_and_emblem_svg_are_coloured(qapp):
    assert b"#0078D4" in icons.tray_svg("onedriveui-synced-business")
    assert icons.tray_svg("onedriveui-syncing", 3).startswith(b"<svg")
    for stem in icons.EMBLEM_STEMS:
        assert icons.emblem_svg(stem).startswith(b"<svg")


@pytest.mark.slow
def test_install_theme_icons_writes_every_name(qapp, tmp_config):
    """Nautilus silently drops an emblem missing from the active icon theme, so
    the installer must land every file it promises."""
    icons.install_theme_icons()
    for name, path in icons.installed_icon_files().items():
        assert path.is_file(), f"{name} was not installed at {path}"
        assert path.read_bytes().startswith(b"<svg")
    assert (paths.icon_theme_dir() / "index.theme").is_file()


def test_clear_cache_is_idempotent(qapp):
    icons.icon("settings", 16)
    icons.clear_cache()
    icons.clear_cache()
    assert not icons.icon("settings", 16).isNull()


# ═════════════════════════════════════════════════════════════════════════════
# 8. bus
# ═════════════════════════════════════════════════════════════════════════════

def test_signal_names_lists_exactly_the_declared_signals():
    """A signal added without updating SIGNAL_NAMES (and ARCHITECTURE §11) fails
    here instead of silently existing."""
    declared = {name for name, value in vars(EventBus).items()
                if isinstance(value, Signal)}
    assert declared == set(SIGNAL_NAMES)
    assert len(SIGNAL_NAMES) == len(set(SIGNAL_NAMES)) == 33


@pytest.mark.parametrize("name", SIGNAL_NAMES)
def test_every_signal_is_connectable(name, bus_spy):
    bus_spy.watch(name)
    assert hasattr(BUS, name)


def test_bus_is_a_singleton():
    from onedriveui import bus as bus_module
    assert bus_module.BUS is BUS
    assert isinstance(BUS, EventBus)


def test_signals_carry_frozen_payloads(bus_spy):
    bus_spy.watch("state_changed", "quota_updated")
    facts = facts_for(SyncState.SYNCING)
    BUS.state_changed.emit(SyncState.UP_TO_DATE, SyncState.SYNCING, facts)
    BUS.quota_updated.emit(QuotaInfo(total=10, used=1, free=9))
    old, new, payload = bus_spy.last("state_changed")
    assert old is SyncState.UP_TO_DATE and new is SyncState.SYNCING
    assert dataclasses.is_dataclass(payload) and payload.__dataclass_params__.frozen
    assert bus_spy.count("quota_updated") == 1


# ═════════════════════════════════════════════════════════════════════════════
# 9. data/schema.sql
# ═════════════════════════════════════════════════════════════════════════════

EXPECTED_TABLES = {
    "schema_meta", "accounts", "latches", "activity", "issues", "pins",
    "cache_index", "conflicts", "runs", "decisions", "versions", "trashbin",
    "share_links", "folder_selection", "kfm_folder", "notifications",
    "dialog_seen", "kv",
}


def test_schema_executes_into_memory_cleanly():
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED_TABLES <= tables
        assert tables - EXPECTED_TABLES <= {"sqlite_sequence"}
    finally:
        conn.close()


def test_initial_migration_matches_the_schema():
    """The shipped migration must build the same database as schema.sql, minus
    the per-connection PRAGMAs (journal_mode cannot change inside a migration)."""
    migration = MIGRATIONS_DIR / "001_initial.sql"
    text = migration.read_text(encoding="utf-8")
    assert "PRAGMA journal_mode" not in text
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(text)
        objects = {(row[0], row[1]) for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()
    other = sqlite3.connect(":memory:")
    try:
        other.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        expected = {(row[0], row[1]) for row in other.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    finally:
        other.close()
    assert objects == expected


def test_schema_indexes_and_triggers_exist(tmp_db):
    names = {row["name"] for row in tmp_db.rows(
        "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')")}
    for required in ("ux_activity_dedupe", "ix_activity_recent", "ux_issue_open",
                     "ix_issues_open", "ix_pins_todo", "ix_cache_dirty",
                     "ux_conflict_open", "ix_decisions_pending", "trg_activity_cap"):
        assert required in names, f"schema.sql lost {required}"


def test_activity_cap_matches_the_constant():
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    assert f"LIMIT {constants.ACTIVITY_CAP_ROWS}" in text


def test_schema_enforces_the_open_issue_uniqueness(tmp_db):
    """One row per (account, code, path) while unresolved — a file failing every
    tick must not produce thousands of rows."""
    row = (tmp_db.account_id, IssueCode.NAME_INVALID.value, "error", "a.txt",
           "The file name contains characters that aren't allowed",
           utcnow_iso(), utcnow_iso())
    sql = ("INSERT INTO issues (account_id, code, severity, rel_path, title,"
           " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?)")
    tmp_db.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(sql, row)
    tmp_db.execute("UPDATE issues SET resolved_at = ?", (utcnow_iso(),))
    tmp_db.execute(sql, row)              # resolving frees the slot
    assert tmp_db.count("issues") == 2


def test_schema_cascades_account_deletion(tmp_db):
    tmp_db.execute(
        "INSERT INTO pins (account_id, rel_path, mode, requested_at) VALUES (?,?,?,?)",
        (tmp_db.account_id, "a.txt", "pinned", utcnow_iso()))
    tmp_db.execute("DELETE FROM accounts WHERE id = ?", (tmp_db.account_id,))
    assert tmp_db.count("pins") == 0


def test_foreign_keys_reject_an_unknown_account(tmp_db):
    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(
            "INSERT INTO pins (account_id, rel_path, mode, requested_at)"
            " VALUES ('ghost','a.txt','pinned',?)", (utcnow_iso(),))


# ═════════════════════════════════════════════════════════════════════════════
# 10. Fixture self-tests — FakeRc
# ═════════════════════════════════════════════════════════════════════════════

def test_fake_rc_stats_omits_empty_keys(fake_rc):
    """core/stats OMITS transferring, checking and lastError when empty; code
    that indexes them directly crashes against the real daemon."""
    stats = fake_rc.call_blocking("core/stats")
    assert "transferring" not in stats
    assert "checking" not in stats
    assert "lastError" not in stats
    assert "eta" in stats and stats["eta"] is None      # present, but null


def test_fake_rc_stats_shapes(fake_rc):
    fake_rc.set_transfers([{"name": "big.bin", "size": 8_388_608, "bytes": 2_682_880}])
    fake_rc.set_checking(["f00114.txt", "f00115.txt"])
    fake_rc.set_error("context canceled", errors=3)
    fake_rc.set_eta(42)
    stats = fake_rc.call_blocking("core/stats")
    assert stats["eta"] == 42
    assert stats["lastError"] == "context canceled"
    #: checking is a list of STRINGS, transferring a list of DICTS.
    assert all(isinstance(x, str) for x in stats["checking"])
    assert all(isinstance(x, dict) for x in stats["transferring"])
    row = stats["transferring"][0]
    assert {"name", "size", "bytes", "percentage", "speed", "speedAvg", "eta",
            "group", "srcFs", "dstFs"} <= set(row)
    #: short:true drops both lists.
    short = fake_rc.call_blocking("core/stats", {"short": True})
    assert "transferring" not in short and "checking" not in short


def test_fake_rc_stat_is_200_while_list_is_404(fake_rc):
    fake_rc.add_file("Documents", is_dir=True)
    fake_rc.add_file("Documents/a.txt", size=6)
    assert fake_rc.call_blocking(
        "operations/stat", {"fs": "onedrive:", "remote": "nope.txt"}) == {"item": None}
    with pytest.raises(RcError) as excinfo:
        fake_rc.call_blocking("operations/list", {"fs": "onedrive:", "remote": "nope"})
    assert excinfo.value.status == 404
    assert set(excinfo.value.body) == {"error", "input", "path", "status"}


def test_fake_rc_list_reports_directories_as_minus_one(fake_rc):
    fake_rc.add_file("Docs", is_dir=True)
    rows = fake_rc.call_blocking("operations/list", {"fs": "onedrive:", "remote": ""})["list"]
    assert rows[0]["Size"] == -1                      # OneDrive dirs report -1
    assert rows[0]["Name"] == "Docs"


def test_fake_rc_refresh_rejects_a_json_boolean(fake_rc):
    """The one boolean in the whole rc API that must be the STRING "true"."""
    with pytest.raises(RcError) as excinfo:
        fake_rc.call_blocking("vfs/refresh", {"recursive": True})
    assert excinfo.value.status == 400
    assert 'value must be string "recursive"=true' in excinfo.value.message
    assert fake_rc.call_blocking("vfs/refresh", {"recursive": "true"}) == {"result": {"": "OK"}}
    assert fake_rc.call_blocking("vfs/refresh", {}) == {"result": {"": "OK"}}
    #: An explicit empty dir is an error; omit `dir` to refresh the root.
    assert fake_rc.call_blocking("vfs/refresh", {"dir": ""})["result"][""] == "file does not exist"


def test_fake_rc_job_ids_ride_the_header(fake_rc):
    call = fake_rc.call("core/stats")
    assert call.headers["X-Rclone-Jobid"] == str(call.jobid) != "0"
    assert fake_rc.last_headers["Server"] == "rclone/v1.75.0"


def test_fake_rc_async_returns_a_job_handle(fake_rc):
    fake_rc.add_file("a.txt", size=6)
    out = fake_rc.call_blocking("operations/list",
                                {"fs": "onedrive:", "remote": "", "_async": True,
                                 "_group": "sync/Docs"})
    assert set(out) == {"jobid", "executeId"}
    assert out["executeId"] == fake_rc.execute_id
    status = fake_rc.job_status(out["jobid"])
    assert status["finished"] is True and status["group"] == "sync/Docs"
    assert "list" in status["output"]


def test_fake_rc_expired_job_is_500_job_not_found(fake_rc):
    out = fake_rc.call_blocking("core/stats", {"_async": True})
    fake_rc.expire_jobs()
    with pytest.raises(RcError) as excinfo:
        fake_rc.job_status(out["jobid"])
    assert excinfo.value.status == 500
    assert excinfo.value.is_job_expired
    #: The executeId is UNCHANGED, so this is an expiry, not a restart.
    assert fake_rc.call_blocking("job/list")["executeId"] == out["executeId"]


def test_fake_rc_restart_changes_the_execute_id(fake_rc):
    before = fake_rc.call_blocking("job/list")["executeId"]
    out = fake_rc.call_blocking("core/stats", {"_async": True})
    assert out["executeId"] == before
    after = fake_rc.restart()
    assert after != before
    #: Job ids start from 1 again, so an id captured before the restart is not
    #: merely expired — it may now name a DIFFERENT job. That is why a caller
    #: compares executeId before deciding what a `job not found` means.
    assert fake_rc.call_blocking("core/stats", {"_async": True})["jobid"] == 1
    assert fake_rc.call_blocking("job/list")["executeId"] == after
    with pytest.raises(RcError):
        fake_rc.job_status(out["jobid"])


def test_fake_rc_group_list_is_null_when_empty(fake_rc):
    assert fake_rc.call_blocking("core/group-list") == {"groups": None}
    fake_rc.call_blocking("core/stats", {"_group": "sync/Docs"})
    assert fake_rc.call_blocking("core/group-list")["groups"] == ["sync/Docs"]


def test_fake_rc_bwlimit_normalises_to_binary_units(fake_rc):
    out = fake_rc.call_blocking("core/bwlimit", {"rate": "1M:100k"})
    #: NEVER string-compare the echo against what you sent.
    assert out["rate"] == "1Mi:100Ki"
    assert out["bytesPerSecondTx"] == 1_048_576
    assert out["bytesPerSecondRx"] == 102_400
    assert fake_rc.call_blocking("core/bwlimit", {})["rate"] == "1Mi:100Ki"
    assert fake_rc.call_blocking("core/bwlimit", {"rate": "off"})["bytesPerSecond"] == -1


def test_fake_rc_queue_expiry_race_is_a_500(fake_rc):
    item = fake_rc.add_queue_item("queued.bin", size=6_291_456)
    assert fake_rc.call_blocking("vfs/queue")["queue"][0]["id"] == item["id"]
    assert fake_rc.call_blocking("vfs/queue-set-expiry",
                                 {"id": item["id"], "expiry": -1_000_000_000}) == {}
    with pytest.raises(RcError) as excinfo:
        fake_rc.call_blocking("vfs/queue-set-expiry", {"id": 999, "expiry": -1})
    assert "id not found in queue" in excinfo.value.message


def test_fake_rc_poll_interval_needs_change_notify(fake_rc):
    assert fake_rc.call_blocking("vfs/poll-interval", {})["supported"] is True
    fake_rc.supports_change_notify = False
    with pytest.raises(RcError) as excinfo:
        fake_rc.call_blocking("vfs/poll-interval", {"interval": "30s"})
    assert excinfo.value.status == 500


def test_fake_rc_fsinfo_reports_onedrives_real_capabilities(fake_rc):
    info = fake_rc.call_blocking("operations/fsinfo", {"fs": "onedrive:"})
    assert info["Hashes"] == ["quickxor"]
    assert info["Precision"] == 1_000_000_000
    assert info["Features"]["ChangeNotify"] is True
    assert info["Features"]["ListR"] is False      # --fast-list is a no-op here
    assert info["Features"]["PublicLink"] is True


def test_fake_rc_about_may_omit_trashed(fake_rc):
    fake_rc.set_quota(total=100, used=40, trashed=None)
    about = fake_rc.call_blocking("operations/about", {"fs": "onedrive:"})
    assert "trashed" not in about and about["free"] == 60


def test_fake_rc_auth_failure_surfaces_through_about(fake_rc):
    fake_rc.auth_error = "failed to get token: empty token found"
    with pytest.raises(RcError) as excinfo:
        fake_rc.call_blocking("operations/about", {"fs": "onedrive:"})
    assert classify(excinfo.value.message)[0] is IssueCode.AUTH_EXPIRED


def test_fake_rc_scripting(fake_rc):
    fake_rc.script("core/stats", [{"bytes": 1}, {"bytes": 2}, {"bytes": 3}])
    assert [fake_rc.call_blocking("core/stats")["bytes"] for _ in range(4)] == [1, 2, 3, 3]
    fake_rc.fail("operations/about", status=507, message="quotaLimitReached", times=1)
    with pytest.raises(RcError) as excinfo:
        fake_rc.call_blocking("operations/about", {"fs": "onedrive:"})
    assert excinfo.value.status == 507
    assert "total" in fake_rc.call_blocking("operations/about", {"fs": "onedrive:"})
    fake_rc.set("core/pid", lambda params: {"pid": 4321})
    assert fake_rc.call_blocking("core/pid")["pid"] == 4321
    fake_rc.set("rc/noop", RcFault(500, "boom"))
    with pytest.raises(RcError):
        fake_rc.call_blocking("rc/noop")


def test_fake_rc_records_every_call(fake_rc):
    fake_rc.call_blocking("core/stats", {"group": "g"})
    fake_rc.call_blocking("core/stats", {"group": "g"})
    assert fake_rc.count("core/stats") == 2
    assert fake_rc.last("core/stats").params == {"group": "g"}
    fake_rc.assert_never("core/quit")


def test_fake_rc_async_delivery_is_deferred(fake_rc):
    seen: list[dict] = []
    call = fake_rc.call("core/stats")
    call.succeeded.connect(seen.append)
    assert seen == []                    # nothing before the loop turns
    assert fake_rc.flush() == 1
    assert len(seen) == 1
    assert fake_rc.flush() == 0          # delivery is idempotent


def test_fake_rc_failed_signal_carries_an_rc_error(fake_rc):
    caught: list[object] = []
    call = fake_rc.call("operations/list", {"fs": "onedrive:", "remote": "nope"})
    call.failed.connect(caught.append)
    fake_rc.flush()
    assert isinstance(caught[0], RcError) and caught[0].status == 404


def test_fake_rc_offline_raises_daemon_unavailable(fake_rc):
    assert fake_rc.is_alive() is True
    fake_rc.stop()
    assert fake_rc.is_alive() is False
    with pytest.raises(DaemonUnavailable):
        fake_rc.call_blocking("core/stats")


def test_fake_rc_module_surface_matches_rc_client(fake_rc):
    assert fake_is_alive(fake_rc.endpoint) is True
    assert "version" in fake_call_blocking(fake_rc.endpoint, "core/version")
    stranger = models.RcEndpoint(kind="rcd", host="127.0.0.1", port=19999)
    with pytest.raises(DaemonUnavailable):
        fake_call_blocking(stranger, "core/version")


@pytest.mark.parametrize("path", sorted(BANNED_PATHS))
def test_fake_rc_refuses_the_endpoints_invariants_ban(path, fake_rc):
    """I7/I8: mount/* and operations/cleanup must appear nowhere in the codebase,
    so calling one is a test failure, not a response."""
    with pytest.raises(AssertionError):
        fake_rc.call_blocking(path, {})


def test_fake_rc_unknown_method_is_404(fake_rc):
    with pytest.raises(RcError) as excinfo:
        fake_rc.call_blocking("does/not/exist")
    assert excinfo.value.status == 404
    assert "couldn't find method" in excinfo.value.message


# ═════════════════════════════════════════════════════════════════════════════
# 11. Fixture self-tests — FakeFs
# ═════════════════════════════════════════════════════════════════════════════

def test_fake_fs_builds_the_two_mirrored_trees(fake_fs):
    assert fake_fs.data_dir.is_dir() and fake_fs.meta_dir.is_dir()
    assert fake_fs.data_dir.name == fake_fs.meta_dir.name == "onedrive{MxOuf}"
    assert fake_fs.data_dir.parent.name == "vfs"
    assert fake_fs.meta_dir.parent.name == "vfsMeta"


def test_fake_fs_covers_all_six_classify_shapes(fake_fs):
    from tests.fakes.fake_fs import SHAPES
    by_shape = fake_fs.by_shape()
    assert set(by_shape) == set(SHAPES) and len(SHAPES) == 6


@pytest.mark.parametrize("shape,expected", [
    ("no_sidecar", FileState.ONLINE_ONLY),
    ("rs_null", FileState.ONLINE_ONLY),
    ("rs_empty", FileState.ONLINE_ONLY),
    ("full_range", FileState.LOCAL),
    ("two_ranges", FileState.PARTIAL),
    ("dirty", FileState.DIRTY),
])
def test_fake_fs_sidecars_match_the_documented_shape(fake_fs, shape, expected):
    entry = fake_fs.entry(shape)
    assert entry.state is expected
    meta = fake_fs.sidecar(entry.rel_path)
    if shape == "no_sidecar":
        assert meta == {}
        assert not fake_fs.data_path(entry.rel_path).exists()
        return
    assert set(meta) == {"ModTime", "ATime", "Size", "Rs", "Fingerprint", "Dirty"}
    assert meta["Size"] == entry.size
    if shape == "rs_null":
        assert meta["Rs"] is None                 # JSON null, not []
    elif shape == "rs_empty":
        assert meta["Rs"] == []
    elif shape == "full_range":
        assert meta["Rs"] == [{"Pos": 0, "Size": entry.size}]
    elif shape == "two_ranges":
        assert len(meta["Rs"]) == 2
        assert sum(r["Size"] for r in meta["Rs"]) < meta["Size"]
    elif shape == "dirty":
        #: Dirty ⇔ a pending local change, and the Fingerprint is empty until it
        #: has been uploaded.
        assert meta["Dirty"] is True and meta["Fingerprint"] == ""


def test_fake_fs_data_files_are_really_sparse(fake_fs):
    """SEEK_DATA/SEEK_HOLE must see the same ranges the sidecar lists — that
    equivalence is what `rc/vfs.local_extents()` relies on."""
    entry = fake_fs.entry("two_ranges")
    found = fake_fs.extents(entry.rel_path)
    assert found == [(pos, size) for pos, size in entry.rs]
    #: An untouched file is one big hole.
    assert fake_fs.extents(fake_fs.entry("rs_null").rel_path) == []
    assert extents(fake_fs.data_path(fake_fs.entry("full_range").rel_path)) == [
        (0, fake_fs.entry("full_range").size)]


def test_fake_fs_preserves_non_ascii_paths(fake_fs):
    entry = fake_fs.entry("two_ranges")
    assert "Imágenes" in entry.rel_path
    assert fake_fs.data_path(entry.rel_path).exists()
    assert fake_fs.meta_path(entry.rel_path).exists()


def test_fake_fs_reports_a_disk_cache_info(fake_fs):
    info = fake_fs.disk_cache_info()
    assert isinstance(info, DiskCacheInfo)
    assert info.path == str(fake_fs.data_dir)
    assert info.path_meta == str(fake_fs.meta_dir)
    assert info.hash_type == 4096              # quickxor, not MD5
    assert info.bytes_used == fake_fs.bytes_used > 0


def test_fake_fs_drives_the_fake_daemon(fake_fs, fake_rc):
    """Invariant I4: the cache location always comes from vfs/stats."""
    fake_fs.apply_to(fake_rc)
    disk = fake_rc.call_blocking("vfs/stats")["diskCache"]
    assert disk["path"] == str(fake_fs.data_dir)
    assert disk["pathMeta"] == str(fake_fs.meta_dir)


def test_fake_fs_eviction_removes_both_files(fake_fs):
    entry = fake_fs.entry("full_range")
    fake_fs.evict(entry.rel_path)
    assert not fake_fs.data_path(entry.rel_path).exists()
    assert not fake_fs.meta_path(entry.rel_path).exists()
    assert fake_fs.sidecar(entry.rel_path) == {}


def test_fake_fs_has_an_orphaned_cache_tree(fake_fs):
    orphans = fake_fs.orphan_trees()
    assert orphans and orphans[0][0].name == "onedrive"
    assert orphans[0][1] > 0
    assert fake_fs.data_dir not in [path for path, _size in orphans]


def test_fake_fs_sync_root_carries_hostile_names(fake_fs):
    from tests.fakes.fake_fs import HOSTILE_NAMES
    present = {p.name for p in (fake_fs.sync_root / "bad names").iterdir()}
    assert len(present & set(HOSTILE_NAMES)) >= 8
    assert (fake_fs.sync_root / ".Trash-1000").is_dir()
    assert not paths.is_under_fuse_mount(fake_fs.sync_root)


def test_fake_fs_touch_dirty(fake_fs):
    entry = fake_fs.entry("full_range")
    fake_fs.touch_dirty(entry.rel_path)
    meta = fake_fs.sidecar(entry.rel_path)
    assert meta["Dirty"] is True and meta["Fingerprint"] == ""


# ═════════════════════════════════════════════════════════════════════════════
# 12. Fixture self-tests — FakeServices
# ═════════════════════════════════════════════════════════════════════════════

def test_fake_services_can_drive_every_sync_state(fake_services, bus_spy):
    bus_spy.watch("state_changed", "facts_updated")
    snaps = fake_services.drive_all_states()
    assert [s.state for s in snaps] == list(SyncState)
    assert bus_spy.count("state_changed") == len(list(SyncState))
    for snap in snaps:
        assert snap.headline
        assert snap.tray is TRAY_FOR_STATE[snap.state]
        assert "{" not in snap.headline and "{" not in snap.subtext


def test_fake_services_can_raise_every_issue(fake_services, bus_spy):
    bus_spy.watch("issue_raised")
    issues = fake_services.raise_every_issue()
    assert {i.code for i in issues} == set(IssueCode)
    assert bus_spy.count("issue_raised") == len(list(IssueCode))
    for issue in issues:
        assert issue.title and "{" not in issue.title
        assert issue.actions == ACTIONS_FOR[issue.code]
    blocking, error, warning = fake_services.issues.counts(fake_services.account.id)
    assert blocking and error and warning


def test_fake_services_can_fire_every_toast(fake_services, bus_spy):
    bus_spy.watch("toast_requested")
    specs = fake_services.fire_every_toast()
    assert {s.id for s in specs} == set(NotificationId)
    assert bus_spy.count("toast_requested") == len(list(NotificationId))
    for spec in specs:
        assert len(spec.actions) <= fake_services.notifier.MAX_ACTIONS
        assert "{" not in spec.summary and "{" not in spec.body


def test_fake_supervisor_records_actions_instead_of_performing_them(fake_services):
    supervisor = fake_services.supervisor
    supervisor.do(RecoveryAction.RETRY, issue_id=7)
    supervisor.do(RecoveryAction.FREE_UP_SPACE)
    assert [a for a, _kw in supervisor.actions] == [RecoveryAction.RETRY,
                                                    RecoveryAction.FREE_UP_SPACE]
    assert supervisor.actions[0][1] == {"issue_id": 7}


def test_fake_supervisor_refuses_a_resync_without_an_answered_decision(fake_services):
    """Invariant I15 — a resync is never launched from a bare button press."""
    supervisor = fake_services.supervisor
    with pytest.raises(SafetyRefusal):
        supervisor.request_resync(decision_id=1)
    decision = supervisor.require_decision()
    supervisor.answer_decision(decision.id, "yes")
    supervisor.request_resync(decision_id=decision.id)
    assert ("request_resync", {"decision_id": decision.id}) in supervisor.calls


def test_fake_supervisor_pause_and_resume(fake_services, bus_spy):
    bus_spy.watch("state_changed")
    fake_services.supervisor.request_pause(PauseReason.METERED, None)
    assert fake_services.supervisor.state() is SyncState.PAUSED_METERED
    fake_services.supervisor.request_resume()
    assert fake_services.supervisor.state() is SyncState.SYNCING


def test_fake_pinner_tracks_pins_and_emits_progress(fake_services, bus_spy):
    bus_spy.watch("file_state_changed", "pin_progress")
    pinner = fake_services.pinner
    pinner.set_size("Documents/a.bin", 0, 1024)
    pinner.pin("Documents/a.bin")
    assert "Documents/a.bin" in pinner.pinned
    pinner.emit_progress("Documents/a.bin", 512, 1024)
    assert pinner.active() == 1
    assert bus_spy.count("pin_progress") == 1
    account_id, rel_path, status = bus_spy.last("file_state_changed")
    assert rel_path == "Documents/a.bin" and status.state is FileState.PINNED
    freed = pinner.free_up_space("Documents/a.bin")
    assert freed == 0 and "Documents/a.bin" in pinner.online_only
    assert pinner.MAX_CONCURRENT_PINS == constants.MAX_CONCURRENT_PINS


def test_fake_issue_engine_reconciles_when_the_condition_clears(fake_services, bus_spy):
    bus_spy.watch("issue_resolved")
    engine = fake_services.issues
    engine.raise_issue(IssueCode.MOUNT_DEAD)
    healthy = facts_for(SyncState.UP_TO_DATE)
    assert engine.reconcile(healthy) == 1
    assert bus_spy.count("issue_resolved") == 1
    assert engine.open_issues() == []


def test_fake_issue_engine_executes_a_recovery_action(fake_services):
    engine = fake_services.issues
    issue = engine.raise_issue(IssueCode.UPLOAD_FAILED, rel_path="a.txt")
    assert engine.execute(RecoveryAction.RETRY, issue) is True
    assert engine.issues[issue.id].resolved_at is not None
    assert engine.executed[0][0] is RecoveryAction.RETRY


def test_fake_quota_service_tiers(fake_services, bus_spy):
    bus_spy.watch("quota_updated")
    quota = fake_services.quota
    assert quota.tier() == "ok"
    quota.set_tier("warn")
    assert quota.tier() == "warn" and 80 <= quota.pct() < 90
    quota.set_tier("full")
    assert quota.is_full() and quota.tier() == "full"
    quota.refresh()
    assert quota.refreshes == 1
    assert bus_spy.count("quota_updated") >= 3


def test_fake_pause_manager(fake_services, bus_spy):
    bus_spy.watch("pause_changed")
    pause = fake_services.pause
    assert pause.active() is PauseReason.NONE
    pause.queue_size = 4
    pause.pause(PauseReason.MANUAL, hours=2)
    assert pause.active() is PauseReason.MANUAL and pause.until() is not None
    assert pause.enforce(None) == 4          # every queued upload is deferred
    pause.sync_anyway(PauseReason.MANUAL)
    assert pause.active() is PauseReason.NONE
    assert [args[0] for args in bus_spy.of("pause_changed")][-1] is PauseReason.NONE
    assert pause.PAUSE_DURATIONS[-1][0] is None       # "Until I resume"


def test_fake_notifier_caps_actions_and_can_be_disabled(fake_services):
    notifier = fake_services.notifier
    assert notifier.MAX_ACTIONS == 2
    assert "body-markup" in notifier.capabilities()
    assert "body-images" not in notifier.capabilities()
    notifier.disabled.add(NotificationId.MEMORIES)
    assert notifier.notify(
        models.NotifySpec(id=NotificationId.MEMORIES, summary="x")) == 0
    with pytest.raises(AssertionError):
        notifier.notify(models.NotifySpec(
            id=NotificationId.SYNC_ISSUES, summary="x",
            actions=(("a", "A"), ("b", "B"), ("c", "C"))))


def test_snapshot_for_uses_the_business_cloud(fake_services):
    business = dataclasses.replace(fake_services.account, kind=models.AccountKind.BUSINESS)
    snap = snapshot_for(SyncState.UP_TO_DATE, account=business)
    assert snap.tray is TrayIcon.SYNCED_BIZ


# ═════════════════════════════════════════════════════════════════════════════
# 13. Fixture self-tests — config, db, clock, bus spy
# ═════════════════════════════════════════════════════════════════════════════

def test_tmp_config_writes_a_valid_0600_config(tmp_config):
    assert tmp_config.path.is_file()
    assert tmp_config.path.stat().st_mode & 0o777 == 0o600
    data = tmp_config.reload()
    assert data["schema_version"] == 1
    assert data["accounts"][0]["mount"]["transfers"] == constants.MAX_TRANSFERS
    assert data["accounts"][0]["mount"]["checkers"] == constants.MAX_CHECKERS
    assert data["advanced"]["rc_port_range"] == [17800, 17899]
    assert data["advanced"]["job_expire"] == constants.RC_JOB_EXPIRE
    assert data["accounts"][0]["backend"]["no_versions"] is False   # invariant I9


def test_tmp_config_set_and_corrupt(tmp_config):
    tmp_config.set("app.theme", "dark")
    assert tmp_config.reload()["app"]["theme"] == "dark"
    tmp_config.corrupt()
    with pytest.raises(json.JSONDecodeError):
        tmp_config.reload()


def test_tmp_config_lives_in_an_isolated_home(tmp_config):
    assert str(tmp_config.path).startswith(str(paths.Path.home()))
    assert paths.Path.home() != REAL_HOME


def test_tmp_db_has_the_schema_and_the_seed_account(tmp_db):
    assert tmp_db.count("accounts") == 1
    assert tmp_db.one("SELECT * FROM accounts")["id"] == tmp_db.account_id
    assert tmp_db.path == paths.db_file()
    #: SQLite over FUSE loses locking guarantees; the DB never lives there.
    assert not paths.is_under_fuse_mount(tmp_db.path)


def test_frozen_clock_freezes_utcnow(frozen_clock):
    first = models.utcnow_iso()
    assert first == frozen_clock.iso() == "2026-08-31T12:00:00Z"
    assert models.utcnow_iso() == first
    frozen_clock.advance(90)
    assert models.utcnow_iso() == "2026-08-31T12:01:30Z"
    assert frozen_clock.monotonic() == 10_090.0
    assert parse_iso(models.utcnow_iso()) is not None


def test_frozen_clock_can_patch_a_module_time(frozen_clock, monkeypatch):
    import types as _types
    module = _types.SimpleNamespace(time=None)
    frozen_clock.patch_time(monkeypatch, module)
    start = module.time.monotonic()
    frozen_clock.advance(5)
    assert module.time.monotonic() == start + 5


def test_bus_spy_disconnects_itself(bus_spy):
    bus_spy.watch("log_line")
    BUS.log_line.emit("hello")
    assert bus_spy.of("log_line") == [("hello",)]
    bus_spy.disconnect_all()
    BUS.log_line.emit("ignored")
    assert bus_spy.count("log_line") == 1
    bus_spy.clear()
    assert bus_spy.events == []


def test_qapp_is_offscreen(qapp):
    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    assert qapp.instance() is qapp


def test_isolated_home_is_not_the_real_home(tmp_config):
    assert os.environ["HOME"] != str(REAL_HOME)
    assert not str(paths.config_dir()).startswith(str(REAL_HOME / ".config"))
