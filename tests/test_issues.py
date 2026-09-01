"""WP-07 — `sync/issues.py` and `sync/conflicts.py`.

The three properties that make an issue list usable rather than a wall:
deduplication on `(account, code, rel_path)`, auto-resolution of what the world
has already fixed, and an action attached to every issue that actually works.

Plus the conflict rule Windows established and this reproduces byte for byte:
the losing copy is renamed `Budget-<hostname>.xlsx`, with the suffix *before* the
extension, and no version is ever deleted.
"""

from __future__ import annotations

import socket

import pytest

from onedriveui import paths
from onedriveui.data import db, repo_sync
from onedriveui.data.writer import DbWriter
from onedriveui.errors import ACTIONS_FOR
from onedriveui.models import (
    AccountInfo,
    ActivityEvent,
    BisyncState,
    ConflictPolicy,
    DaemonHealth,
    Facts,
    IssueCode,
    IssueSeverity,
    MountHealth,
    NetworkState,
    QuotaInfo,
    RecoveryAction,
    SyncIssue,
    TokenHealth,
    utcnow_iso,
)
from onedriveui.sync.issues import AUTO_RESOLVE, IssueEngine
from onedriveui.sync.preflight import Violation

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")

HEALTHY = Facts(
    account_id=ACCOUNT.id, account_configured=True,
    daemon_rcd=DaemonHealth.UP, mount=MountHealth.UP, token=TokenHealth.OK,
    quota=QuotaInfo(total=1_000, used=10, free=990),
    network=NetworkState.ONLINE, bisync=BisyncState.DISABLED,
)


@pytest.fixture
def store(_isolate_home, qapp):
    writer = DbWriter(paths.db_file())
    assert writer.start_writer()
    writer.submit_sync(
        lambda conn: conn.execute(
            "INSERT INTO accounts (id, remote, sync_root, added_at) VALUES (?,?,?,?)",
            (ACCOUNT.id, ACCOUNT.remote, ACCOUNT.sync_root, utcnow_iso())),
        urgent=True)
    try:
        yield writer
    finally:
        writer.stop()
        db.close_all()


def engine(store, **kwargs) -> IssueEngine:
    return IssueEngine(ACCOUNT, writer=store, **kwargs)


class RecordingSupervisor:
    def __init__(self):
        self.actions: list[tuple[RecoveryAction, dict]] = []

    def do(self, action, **kw):
        self.actions.append((action, dict(kw)))


# ═════════════════════════════════════════════════════════════════════════════
# Raising and deduplicating
# ═════════════════════════════════════════════════════════════════════════════

class TestRaise:

    def test_an_issue_is_recorded_with_its_fixes(self, qapp, store):
        eng = engine(store)
        issue_id = eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt",
                                   detail=":")
        assert issue_id
        issue = eng.open_issues()[0]
        assert issue.code is IssueCode.NAME_INVALID
        assert issue.actions == ACTIONS_FOR[IssueCode.NAME_INVALID]

    def test_the_title_comes_from_strings(self, qapp, store):
        """No user-facing string literal lives outside `strings.py`."""
        eng = engine(store)
        eng.raise_issue(IssueCode.NAME_INVALID, rel_path="a")
        assert eng.open_issues()[0].title == (
            "The file name contains characters that aren't allowed")

    def test_the_same_problem_twice_is_one_row(self, qapp, store):
        """A file failing every 400 ms for an hour must be one row with a
        counter, not nine thousand — otherwise the one genuinely new problem is
        buried under repeats of the old one."""
        eng = engine(store)
        for _ in range(50):
            eng.raise_issue(IssueCode.UPLOAD_FAILED, rel_path="a.txt")
        issues = eng.open_issues()
        assert len(issues) == 1
        assert issues[0].occurrences == 50

    def test_different_paths_are_different_issues(self, qapp, store):
        eng = engine(store)
        eng.raise_issue(IssueCode.UPLOAD_FAILED, rel_path="a.txt")
        eng.raise_issue(IssueCode.UPLOAD_FAILED, rel_path="b.txt")
        assert len(eng.open_issues()) == 2

    def test_different_codes_on_one_path_are_different_issues(self, qapp, store):
        eng = engine(store)
        eng.raise_issue(IssueCode.UPLOAD_FAILED, rel_path="a.txt")
        eng.raise_issue(IssueCode.NAME_INVALID, rel_path="a.txt")
        assert len(eng.open_issues()) == 2

    def test_the_bus_carries_it(self, qapp, store, bus_spy):
        bus_spy.watch("issue_raised")
        engine(store).raise_issue(IssueCode.UPLOAD_FAILED, rel_path="a.txt")
        assert bus_spy.count("issue_raised") == 1


class TestSeverity:

    @pytest.mark.parametrize("code", [
        IssueCode.AUTH_EXPIRED, IssueCode.QUOTA_EXCEEDED, IssueCode.MOUNT_DEAD,
        IssueCode.BISYNC_CRITICAL, IssueCode.MALWARE_DETECTED,
    ])
    def test_account_wide_problems_are_blocking(self, qapp, store, code):
        eng = engine(store)
        eng.raise_issue(code)
        assert eng.counts()[0] >= 1

    def test_one_bad_filename_is_not_blocking(self, qapp, store):
        """Blocking puts the tray into ERROR. One bad name among thirty thousand
        good ones has not stopped everything, and saying it has would make the
        red icon meaningless."""
        eng = engine(store)
        eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt")
        blocking, error, _warning = eng.counts()
        assert blocking == 0
        assert error == 1

    def test_throttling_is_only_a_warning(self, qapp, store):
        """rclone retries a 429 on its own; it is worth saying and not worth
        colouring the tray for."""
        eng = engine(store)
        eng.raise_issue(IssueCode.THROTTLED)
        assert eng.counts() == (0, 0, 1)


class TestIngest:

    def test_a_failed_transfer_becomes_an_issue(self, qapp, store):
        eng = engine(store)
        eng.ingest_transfer_error(ActivityEvent(
            account_id=ACCOUNT.id, rel_path="a.txt", name="a.txt",
            direction="up", error="quotaLimitReached"))
        assert eng.open_issues()[0].code is IssueCode.QUOTA_EXCEEDED

    def test_a_successful_transfer_raises_nothing(self, qapp, store):
        eng = engine(store)
        assert eng.ingest_transfer_error(ActivityEvent(
            account_id=ACCOUNT.id, rel_path="a.txt", name="a.txt")) is None
        assert eng.open_issues() == []

    def test_an_unrecognised_upload_failure_is_not_just_unknown(self, qapp, store):
        """"Couldn't upload this file" and "Couldn't download this file" need
        different words and different fixes."""
        eng = engine(store)
        eng.ingest_transfer_error(ActivityEvent(
            account_id=ACCOUNT.id, rel_path="a.txt", name="a.txt",
            direction="up", error="something rclone has never said before"))
        assert eng.open_issues()[0].code is IssueCode.UPLOAD_FAILED

    def test_health_facts_raise_the_account_wide_issues(self, qapp, store):
        from dataclasses import replace

        eng = engine(store)
        eng.ingest_health(replace(HEALTHY, mount=MountHealth.STALE,
                                  token=TokenHealth.EXPIRED))
        codes = {i.code for i in eng.open_issues()}
        assert IssueCode.MOUNT_DEAD in codes
        assert IssueCode.AUTH_EXPIRED in codes

    def test_a_healthy_world_raises_nothing(self, qapp, store):
        eng = engine(store)
        eng.ingest_health(HEALTHY)
        assert eng.open_issues() == []

    def test_preflight_violations_are_recorded(self, qapp, store):
        eng = engine(store)
        eng.ingest_preflight([
            Violation("bad:name.txt", IssueCode.NAME_INVALID, ":"),
            Violation("NUL.txt", IssueCode.RESERVED_NAME, "reserved"),
        ])
        assert len(eng.open_issues()) == 2

    def test_only_error_log_records_become_issues(self, qapp, store):
        eng = engine(store)
        assert eng.ingest_log_record({"level": "info", "msg": "copied a.txt"}) is None
        assert eng.ingest_log_record({"level": "error", "msg": "quotaLimitReached",
                                      "object": "a.txt"}) is not None


# ═════════════════════════════════════════════════════════════════════════════
# Auto-resolution
# ═════════════════════════════════════════════════════════════════════════════

class TestReconcile:

    def test_a_renewed_token_closes_its_issues(self, qapp, store):
        eng = engine(store)
        eng.raise_issue(IssueCode.AUTH_EXPIRED)
        assert eng.reconcile(HEALTHY) >= 1
        assert eng.open_issues() == []

    def test_space_appearing_closes_the_quota_issue(self, qapp, store):
        eng = engine(store)
        eng.raise_issue(IssueCode.QUOTA_EXCEEDED)
        eng.reconcile(HEALTHY)
        assert eng.open_issues() == []

    def test_an_unanswered_about_does_not_close_it(self, qapp, store):
        """A quota of zero means we have not learned anything yet, not that
        space appeared."""
        from dataclasses import replace

        eng = engine(store)
        eng.raise_issue(IssueCode.QUOTA_EXCEEDED)
        eng.reconcile(replace(HEALTHY, quota=QuotaInfo()))
        assert len(eng.open_issues()) == 1

    def test_a_bad_filename_is_never_auto_resolved(self, qapp, store):
        """It does not fix itself, and closing it would hide a file that is
        still not syncing."""
        eng = engine(store)
        eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt")
        eng.reconcile(HEALTHY)
        assert len(eng.open_issues()) == 1

    def test_only_the_declared_codes_are_eligible(self):
        """The table is the contract; anything absent needs a human."""
        assert IssueCode.NAME_INVALID not in AUTO_RESOLVE
        assert IssueCode.FILE_TOO_LARGE not in AUTO_RESOLVE
        assert IssueCode.CONFLICT not in AUTO_RESOLVE
        assert IssueCode.AUTH_EXPIRED in AUTO_RESOLVE

    def test_the_bus_carries_the_resolution(self, qapp, store, bus_spy):
        eng = engine(store)
        eng.raise_issue(IssueCode.AUTH_EXPIRED)
        bus_spy.watch("issue_resolved")
        eng.reconcile(HEALTHY)
        assert bus_spy.count("issue_resolved") >= 1


class TestMute:

    def test_a_muted_issue_stops_colouring_the_tray(self, qapp, store):
        """Which is the whole reason a user mutes one."""
        eng = engine(store)
        issue_id = eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt")
        assert eng.counts()[1] == 1
        eng.mute(issue_id)
        store.flush()
        assert eng.counts()[1] == 0

    def test_but_it_stays_unresolved(self, qapp, store):
        """The file still is not syncing. Closing it for tidiness would be a
        lie told to make a list look shorter."""
        eng = engine(store)
        issue_id = eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt")
        eng.mute(issue_id)
        store.flush()
        assert [i.id for i in eng.open_issues()] == [issue_id]


# ═════════════════════════════════════════════════════════════════════════════
# Fixing
# ═════════════════════════════════════════════════════════════════════════════

class TestExecute:

    def test_every_recovery_action_has_a_handler(self):
        """`ACTIONS_FOR` can offer any of the eighteen, and an offered button
        that does nothing is worse than no button."""
        assert set(IssueEngine._HANDLERS) == set(RecoveryAction)

    def test_every_action_any_issue_offers_is_handled(self):
        offered = {a for actions in ACTIONS_FOR.values() for a in actions}
        assert offered <= set(IssueEngine._HANDLERS)

    def test_an_unknown_action_raises(self, qapp, store):
        with pytest.raises(KeyError):
            engine(store).execute("not-an-action")  # type: ignore[arg-type]

    def test_skip_closes_the_issue_and_leaves_the_file(self, qapp, store, tmp_path):
        path = tmp_path / "bad:name.txt"
        path.write_text("keep me")
        eng = IssueEngine(
            AccountInfo(id=ACCOUNT.id, remote="onedrive", sync_root=str(tmp_path)),
            writer=store)
        issue_id = eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt")
        issue = eng.open_issues()[0]
        assert eng.execute(RecoveryAction.SKIP, issue) is True
        assert eng.open_issues() == []
        assert path.exists()

    def test_rename_uses_the_deterministic_suggestion(self, qapp, store, tmp_path):
        path = tmp_path / "bad:name.txt"
        path.write_text("x")
        eng = IssueEngine(
            AccountInfo(id=ACCOUNT.id, remote="onedrive", sync_root=str(tmp_path)),
            writer=store)
        eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt")
        assert eng.execute(RecoveryAction.RENAME, eng.open_issues()[0]) is True
        assert (tmp_path / "bad_name.txt").exists()
        assert not path.exists()

    def test_rename_accepts_a_name_the_user_typed(self, qapp, store, tmp_path):
        path = tmp_path / "bad:name.txt"
        path.write_text("x")
        eng = IssueEngine(
            AccountInfo(id=ACCOUNT.id, remote="onedrive", sync_root=str(tmp_path)),
            writer=store)
        eng.raise_issue(IssueCode.NAME_INVALID, rel_path="bad:name.txt")
        eng.execute(RecoveryAction.RENAME, eng.open_issues()[0],
                    new_name="Quarterly Report.txt")
        assert (tmp_path / "Quarterly Report.txt").exists()

    def test_a_failed_fix_leaves_the_issue_open(self, qapp, store, tmp_path):
        """Marking it fixed when it was not is how a file silently stops
        syncing while the UI says everything is fine."""
        eng = IssueEngine(
            AccountInfo(id=ACCOUNT.id, remote="onedrive", sync_root=str(tmp_path)),
            writer=store)
        eng.raise_issue(IssueCode.NAME_INVALID, rel_path="not-on-disk:x.txt")
        assert eng.execute(RecoveryAction.RENAME, eng.open_issues()[0]) is False
        assert len(eng.open_issues()) == 1

    @pytest.mark.parametrize("action", [
        RecoveryAction.SIGN_IN, RecoveryAction.FREE_UP_SPACE,
        RecoveryAction.GET_MORE_STORAGE, RecoveryAction.RESYNC,
        RecoveryAction.RESTART_MOUNT, RecoveryAction.RECLAIM_CACHE,
        RecoveryAction.OPEN_WEB, RecoveryAction.SHOW_IN_FOLDER,
        RecoveryAction.UNLOCK_BISYNC, RecoveryAction.FORCE_DELETE,
        RecoveryAction.RESTORE_FROM_BACKUP, RecoveryAction.STOP_SYNCING_ITEM,
    ])
    def test_the_world_changing_actions_go_through_the_supervisor(
            self, qapp, store, action):
        """`do()` is the single entry point; this engine never calls a service
        directly, so a guard added once is added everywhere."""
        supervisor = RecordingSupervisor()
        eng = engine(store, supervisor=supervisor)
        issue = SyncIssue(account_id=ACCOUNT.id, code=IssueCode.UNKNOWN,
                          severity=IssueSeverity.ERROR, rel_path="a.txt", title="x")
        assert eng.execute(action, issue) is True
        assert supervisor.actions[0][0] is action

    def test_no_supervisor_reports_failure_rather_than_pretending(self, qapp, store):
        eng = engine(store)
        issue = SyncIssue(account_id=ACCOUNT.id, code=IssueCode.UNKNOWN,
                          severity=IssueSeverity.ERROR, title="x")
        assert eng.execute(RecoveryAction.SIGN_IN, issue) is False


# ═════════════════════════════════════════════════════════════════════════════
# Conflicts
# ═════════════════════════════════════════════════════════════════════════════

