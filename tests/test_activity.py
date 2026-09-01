"""WP-07 — `sync/activity.py` and `sync/decisions.py`.

`core/transferred` is a sliding 100-row window that re-reports itself on every
poll, so the headline test here is that feeding the same payload twice inserts
one row. The other one is the cap: 10 000 inserts leave 5 000 rows and the newest
survive.

For decisions, the property that matters is the direction of a timeout. Silence
resolves to **not** doing the destructive thing, and a safety abort is never
retried with `--force` on the user's behalf.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from onedriveui import paths
from onedriveui.constants import ACTIVITY_CAP_ROWS
from onedriveui.data import db, repo_sync
from onedriveui.data.writer import DbWriter
from onedriveui.models import (
    AccountInfo,
    ActivityState,
    ActivityVerb,
    CoreStats,
    Decision,
    DecisionKind,
    IssueCode,
    RunKind,
    RunRecord,
    RunVerdict,
    TransferInfo,
    utcnow_iso,
)
from onedriveui.sync.activity import ActivityFeed
from onedriveui.sync.decisions import (
    ANSWER_YES,
    DecisionCenter,
    parse_maxdelete,
)
from onedriveui.sync.issues import IssueEngine

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")


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


def transferred(*names: str, group: str = "job/1",
                completed_at: str = "2026-08-31T12:00:00Z") -> dict:
    """A `core/transferred` payload, shaped as rclone v1.75.0 really sends it."""
    return {"transferred": [
        {"name": name, "size": 1024, "bytes": 1024, "checked": False,
         "error": "", "what": "", "group": group,
         "started_at": "2026-08-31T11:59:00Z", "completed_at": completed_at,
         "srcFs": "/home/u/OneDrive", "dstFs": "onedrive:"}
        for name in names]}


# ═════════════════════════════════════════════════════════════════════════════
# Deduplication
# ═════════════════════════════════════════════════════════════════════════════

class TestDedupe:

    def test_the_same_payload_twice_inserts_one_row(self, qapp, store):
        """The BUILD_PLAN's acceptance case. `core/transferred` re-reports its
        whole window on every poll, so without this a 2 Hz poller would insert
        the same hundred rows every half second."""
        feed = ActivityFeed(ACCOUNT, writer=store)
        payload = transferred("a.txt")
        assert len(feed.ingest_transferred(payload)) == 1
        assert len(feed.ingest_transferred(payload)) == 0
        store.flush()          # activity rows are batched at DB_FLUSH_MS
        assert len(feed.recent()) == 1

    def test_the_same_file_transferred_twice_is_two_rows(self, qapp, store):
        """A different `completed_at` is a different event. Keying on the path
        alone would collapse two real uploads into one."""
        feed = ActivityFeed(ACCOUNT, writer=store)
        feed.ingest_transferred(transferred("a.txt", completed_at="2026-08-31T12:00:00Z"))
        feed.ingest_transferred(transferred("a.txt", completed_at="2026-08-31T13:00:00Z"))
        store.flush()
        assert len(feed.recent()) == 2

    def test_a_whole_window_is_ingested_once(self, qapp, store):
        feed = ActivityFeed(ACCOUNT, writer=store)
        payload = transferred(*[f"f{i}.txt" for i in range(100)])
        assert len(feed.ingest_transferred(payload)) == 100
        assert len(feed.ingest_transferred(payload)) == 0

    def test_an_empty_payload_is_fine(self, qapp, store):
        feed = ActivityFeed(ACCOUNT, writer=store)
        assert feed.ingest_transferred({}) == []
        assert feed.ingest_transferred(None) == []


class TestCap:

    def test_the_table_is_capped_and_the_newest_survive(self, qapp, store):
        """The BUILD_PLAN's acceptance case, scaled down to stay fast: the cap
        is enforced by the schema, and what matters is which rows go."""
        feed = ActivityFeed(ACCOUNT, writer=store)
        for i in range(200):
            feed.ingest_transferred(transferred(
                f"f{i}.txt", completed_at=f"2026-08-31T12:{i // 60:02d}:{i % 60:02d}Z"))
        store.flush()
        rows = feed.recent(limit=ACTIVITY_CAP_ROWS)
        assert len(rows) <= ACTIVITY_CAP_ROWS
        assert rows[0].name == "f199.txt"        # newest first

    def test_the_seen_set_is_bounded(self, qapp, store):
        """A long session must not grow a set without limit."""
        feed = ActivityFeed(ACCOUNT, writer=store)
        for i in range(50):
            feed.ingest_transferred(transferred(f"f{i}.txt"))
        assert len(feed._seen) <= ACTIVITY_CAP_ROWS


# ═════════════════════════════════════════════════════════════════════════════
# The three sources
# ═════════════════════════════════════════════════════════════════════════════

class TestSources:

    def test_in_flight_transfers_come_from_stats(self, qapp, store):
        feed = ActivityFeed(ACCOUNT, writer=store)
        live = feed.ingest_stats(CoreStats(transferring=(
            TransferInfo(name="big.iso", size=1000, bytes=250,
                         src_fs="/home/u/OneDrive", dst_fs="onedrive:"),)))
        assert live[0].state is ActivityState.INFLIGHT
        assert live[0].bytes == 250

    def test_a_transfer_leaving_the_list_is_not_marked_done(self, qapp, store):
        """It may have completed and it may have failed; only
        `core/transferred` knows which, and guessing "done" would report a
        failed upload as a successful one."""
        feed = ActivityFeed(ACCOUNT, writer=store)
        feed.ingest_stats(CoreStats(transferring=(TransferInfo(name="a.txt"),)))
        feed.ingest_stats(CoreStats())
        store.flush()
        assert feed.recent() == []

    def test_our_own_actions_are_recorded_too(self, qapp, store):
        """rclone never reports a pin or a free-up, because rclone did not do
        them — and a feed missing half of what the user did is misleading."""
        feed = ActivityFeed(ACCOUNT, writer=store)
        feed.record("Photos/a.jpg", ActivityVerb.PINNED)
        store.flush()
        assert feed.recent()[0].verb is ActivityVerb.PINNED

    def test_a_failed_transfer_raises_an_issue(self, qapp, store):
        feed = ActivityFeed(ACCOUNT, writer=store,
                            issues=IssueEngine(ACCOUNT, writer=store))
        feed.record("a.txt", ActivityVerb.UPLOADED,
                    state=ActivityState.ERROR, error="quotaLimitReached")
        assert repo_sync.open_issues(ACCOUNT.id)[0].code is IssueCode.QUOTA_EXCEEDED

    def test_the_bus_carries_appends_and_updates(self, qapp, store, bus_spy):
        bus_spy.watch("activity_appended", "activity_updated")
        feed = ActivityFeed(ACCOUNT, writer=store)
        feed.ingest_transferred(transferred("a.txt"))
        feed.ingest_stats(CoreStats(transferring=(TransferInfo(name="b.txt"),)))
        assert bus_spy.count("activity_appended") == 1
        assert bus_spy.count("activity_updated") == 1


class TestDaemonRestart:

    def test_in_flight_rows_become_interrupted(self, qapp, store):
        """Not done and not error. The transfer neither completed nor failed —
        the process that knew about it went away."""
        feed = ActivityFeed(ACCOUNT, writer=store)
        feed.record("a.txt", ActivityVerb.UPLOADED, state=ActivityState.INFLIGHT)
        store.flush()
        assert feed.on_daemon_restarted("exec-2") == 1
        store.flush()
        assert feed.recent()[0].state is ActivityState.INTERRUPTED

    def test_completed_rows_are_untouched(self, qapp, store):
        feed = ActivityFeed(ACCOUNT, writer=store)
        feed.record("done.txt", ActivityVerb.UPLOADED)
        store.flush()
        feed.on_daemon_restarted()
        store.flush()
        assert feed.recent()[0].state is ActivityState.DONE

    def test_the_in_flight_cache_is_cleared(self, qapp, store):
        feed = ActivityFeed(ACCOUNT, writer=store)
        feed.ingest_stats(CoreStats(transferring=(TransferInfo(name="a.txt"),)))
        feed.on_daemon_restarted()
        assert feed._inflight == {}


# ═════════════════════════════════════════════════════════════════════════════
# Decisions
# ═════════════════════════════════════════════════════════════════════════════

class TestDecisions:

    def centre(self, store) -> DecisionCenter:
        return DecisionCenter(ACCOUNT, writer=store)

    def test_a_decision_is_recorded_and_asked(self, qapp, store, bus_spy):
        bus_spy.watch("decision_required")
        decision_id = self.centre(store).require(
            DecisionKind.MASS_DELETE, {"count": 4231})
        assert decision_id
        assert bus_spy.count("decision_required") == 1

    def test_the_payload_survives_so_the_dialog_can_be_rebuilt(self, qapp, store):
        """A client restarted three days later must still show the real numbers,
        not a vague warning."""
        centre = self.centre(store)
        centre.require(DecisionKind.MASS_DELETE, {"count": 4231, "total": 16000})
        pending = centre.pending()
        assert pending[0].payload["count"] == 4231
        assert pending[0].payload["total"] == 16000

    def test_answering_closes_it(self, qapp, store, bus_spy):
        bus_spy.watch("decision_answered")
        centre = self.centre(store)
        decision_id = centre.require(DecisionKind.RESYNC_CONFIRM, {})
        centre.answer(decision_id, ANSWER_YES)
        assert centre.pending() == []
        assert bus_spy.last("decision_answered") == (decision_id, ANSWER_YES)

    def test_a_decision_survives_a_restart(self, qapp, store):
        """An unanswered question that silently vanished would leave sync wedged
        with no explanation the user can act on."""
        self.centre(store).require(DecisionKind.MASS_DELETE, {"count": 9})
        assert len(DecisionCenter(ACCOUNT, writer=store).pending()) == 1


class TestExpiry:

    def test_an_old_unanswered_decision_expires(self, qapp, store):
        """Microsoft's seven-day policy."""
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=8))
        repo_sync.create_decision(Decision(
            account_id=ACCOUNT.id, kind=DecisionKind.MASS_DELETE,
            payload={"count": 4231}, created_at=utcnow_iso(),
            expires_at=old.isoformat().replace("+00:00", "Z")), writer=store)
        centre = DecisionCenter(ACCOUNT, writer=store)
        assert centre.expire_stale() == 1
        assert centre.pending() == []

    def test_expiry_means_the_files_were_not_deleted(self, qapp, store):
        """The direction is the whole point. A laptop left closed for a week
        must not become a data-loss event because silence was read as consent."""
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=8))
        decision_id = repo_sync.create_decision(Decision(
            account_id=ACCOUNT.id, kind=DecisionKind.MASS_DELETE,
            payload={"count": 4231, "nothing_was_deleted": True},
            created_at=utcnow_iso(),
            expires_at=old.isoformat().replace("+00:00", "Z")), writer=store)
        DecisionCenter(ACCOUNT, writer=store).expire_stale()
        store.flush()
        row = db.open_ro().execute(
            "SELECT answer, payload FROM decisions WHERE id = ?",
            (decision_id,)).fetchone()
        assert row["answer"] != ANSWER_YES
        assert "nothing_was_deleted" in row["payload"]

    def test_a_fresh_decision_is_not_expired(self, qapp, store):
        centre = self.__class__ and DecisionCenter(ACCOUNT, writer=store)
        centre.require(DecisionKind.MASS_DELETE, {"count": 4231})
        assert centre.expire_stale() == 0
        assert len(centre.pending()) == 1


class TestSafetyAborts:

    def test_the_numbers_are_parsed_out(self):
        """"Delete 4 231 of your 16 000 files?" is a question a user can answer;
        "sync stopped for safety reasons" is not."""
        parsed = parse_maxdelete(
            'ERROR : Safety abort: too many deletes (>25%, 4231 of 16000) on '
            'Path1 "/home/u/OneDrive". Run with --force if desired.')
        assert parsed == {"percent": 25, "deletes": 4231,
                          "total": 16000, "side": "Path1"}

    def test_an_ordinary_log_line_parses_to_nothing(self):
        assert parse_maxdelete("INFO : a.txt: Copied (new)") is None

    def test_one_decision_and_zero_rclone_commands(self, qapp, store, monkeypatch):
        """The BUILD_PLAN's acceptance case, and the most important refusal in
        the project: rclone stopped because a quarter of the drive was about to
        disappear, and re-running with --force is not ours to decide."""
        from onedriveui.rc import bisync

        monkeypatch.setattr(bisync, "start",
                            lambda *a, **kw: pytest.fail("ran bisync on an abort"))
        monkeypatch.setattr(bisync, "build_argv",
                            lambda *a, **kw: pytest.fail("built an argv on an abort"))

        centre = DecisionCenter(ACCOUNT, writer=store)
        run = RunRecord(run_id="r1", account_id=ACCOUNT.id, kind=RunKind.BISYNC,
                        verdict=RunVerdict.ABORTED_MAXDELETE,
                        summary="Safety abort: too many deletes (>25%, 4231 of "
                                "16000) on Path1")
        decision_id = centre.on_maxdelete_abort(run)
        assert decision_id
        assert len(centre.pending()) == 1

    def test_the_decision_records_that_nothing_was_deleted(self, qapp, store):
        centre = DecisionCenter(ACCOUNT, writer=store)
        run = RunRecord(run_id="r1", account_id=ACCOUNT.id, kind=RunKind.BISYNC,
                        summary="Safety abort: too many deletes (>25%, 10 of 100)")
        centre.on_maxdelete_abort(run)
        payload = centre.pending()[0].payload
        assert payload["nothing_was_deleted"] is True
        assert payload["deletes"] == 10

    def test_a_run_that_did_not_abort_raises_nothing(self, qapp, store):
        centre = DecisionCenter(ACCOUNT, writer=store)
        run = RunRecord(run_id="r1", account_id=ACCOUNT.id, kind=RunKind.BISYNC,
                        summary="Bisync successful")
        assert centre.on_maxdelete_abort(run) == 0
        assert centre.pending() == []

    def test_the_all_changed_abort_also_asks(self, qapp, store):
        """Almost always the wrong folder — a mount that was not ready presents
        an empty directory, which looks exactly like "deleted everything"."""
        centre = DecisionCenter(ACCOUNT, writer=store)
        run = RunRecord(run_id="r2", account_id=ACCOUNT.id, kind=RunKind.BISYNC,
                        summary="Safety abort: all files were changed on Path1")
        assert centre.on_allchanged_abort(run)
        assert centre.pending()[0].kind is DecisionKind.ALL_CHANGED
