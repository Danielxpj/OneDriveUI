"""data/repo_sync.py + data/repo_files.py.

The headline test is the generation protocol: an INTERRUPTED cache scan must
leave the previous generation's rows intact, because those rows are still the
best information the Nautilus extension has. Deleting first and re-inserting
would blank every emblem for the length of a scan and lose the lot on a crash.
"""

from __future__ import annotations

import sqlite3

import pytest

from onedriveui import paths
from onedriveui.constants import DECISION_EXPIRY_DAYS
from onedriveui.data import db
from onedriveui.data import repo_files as rf
from onedriveui.data import repo_sync as rs
from onedriveui.data.writer import DbWriter
from onedriveui.models import (
    ActivityEvent, ActivityState, ActivityVerb, CacheEntry, ConflictInfo,
    Decision, DecisionKind, DialogKey, FileState, IssueCode, IssueSeverity,
    KfmFolder, LinkScope, LinkType, PinRecord, RecoveryAction, RunKind,
    RunRecord, RunVerdict, ShareLink, SyncIssue, TrashEntry, VersionEntry,
    utcnow_iso,
)

ACC = "onedrive"


@pytest.fixture
def repo(_isolate_home, qapp):
    """A started writer with one seeded account, plus a read-only connection."""
    writer = DbWriter(paths.db_file())
    assert writer.start_writer()
    writer.submit_sync(
        lambda conn: conn.execute(
            "INSERT INTO accounts (id, remote, sync_root, added_at) "
            "VALUES (?,?,?,?)", (ACC, ACC, "/tmp/onedriveui-test/OneDrive",
                                 utcnow_iso())),
        urgent=True)
    conn = db.open_ro(writer.path)
    try:
        yield writer, conn
    finally:
        writer.stop()
        db.close_all()


# ═════════════════════════════════════════════════════════════════════════════
# cache_index — the generation protocol
# ═════════════════════════════════════════════════════════════════════════════

def entries(*names: str, state: FileState = FileState.LOCAL) -> list[CacheEntry]:
    return [CacheEntry(rel_path=n, size=100, bytes_local=100, state=state)
            for n in names]


def test_upsert_then_prune_leaves_only_the_new_generation(repo):
    """BUILD_PLAN acceptance."""
    writer, conn = repo
    rf.upsert_cache_rows(ACC, entries("a", "b", "c"), 1, writer=writer, sync=True)
    assert rf.cache_generation(ACC, conn=conn) == 1

    # generation 2 sees only a and b: c has been deleted upstream
    rf.upsert_cache_rows(ACC, entries("a", "b"), 2, writer=writer, sync=True)
    removed = rf.prune_cache_generation(ACC, 1, writer=writer)
    assert removed == 1
    rows = {r["rel_path"]: r["scan_generation"]
            for r in conn.execute("SELECT rel_path, scan_generation "
                                  "FROM cache_index WHERE account_id = ?", (ACC,))}
    assert rows == {"a": 2, "b": 2}


def test_an_interrupted_scan_leaves_the_old_rows_intact(repo):
    """BUILD_PLAN acceptance: rows for N written, prune NOT run."""
    writer, conn = repo
    rf.upsert_cache_rows(
        ACC, entries("a", "b", "c", "d", state=FileState.ONLINE_ONLY), 1,
        writer=writer, sync=True)

    # A scan claims generation 2 and dies half way through.
    rf.upsert_cache_rows(ACC, entries("a", "b"), 2, writer=writer, sync=True)
    # ... prune_cache_generation is never called.

    rows = {r["rel_path"]: (r["scan_generation"], r["state"])
            for r in conn.execute(
                "SELECT rel_path, scan_generation, state FROM cache_index "
                "WHERE account_id = ?", (ACC,))}
    assert set(rows) == {"a", "b", "c", "d"}
    assert rows["a"] == (2, "local")            # rescanned
    assert rows["c"] == (1, "online_only")      # untouched, still correct
    assert rows["d"] == (1, "online_only")

    # The next scan completes and only then prunes.
    rf.upsert_cache_rows(ACC, entries("a", "b", "c", "d"), 3, writer=writer,
                         sync=True)
    rf.prune_cache_generation(ACC, 2, writer=writer)
    assert conn.execute("SELECT count(*) FROM cache_index").fetchone()[0] == 4


def test_prune_removes_every_generation_at_or_below_the_argument(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, entries("g1"), 1, writer=writer, sync=True)
    rf.upsert_cache_rows(ACC, entries("g2"), 2, writer=writer, sync=True)
    rf.upsert_cache_rows(ACC, entries("g3"), 3, writer=writer, sync=True)
    assert rf.prune_cache_generation(ACC, 2, writer=writer) == 2
    assert [r[0] for r in conn.execute("SELECT rel_path FROM cache_index")] == ["g3"]


def test_next_cache_generation_is_monotonic(repo):
    writer, conn = repo
    assert rf.next_cache_generation(ACC, conn=conn) == 1
    rf.upsert_cache_rows(ACC, entries("a"), 1, writer=writer, sync=True)
    assert rf.next_cache_generation(ACC, conn=conn) == 2
    rf.upsert_cache_rows(ACC, entries("a"), 7, writer=writer, sync=True)
    assert rf.next_cache_generation(ACC, conn=conn) == 8


def test_upsert_accepts_mappings_as_well_as_cache_entries(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, [
        {"rel_path": "m.txt", "state": FileState.PARTIAL, "size": 900,
         "bytes_local": 100, "dirty": True, "atime": "2026-08-31T12:00:00Z",
         "mtime": "2026-08-30T12:00:00Z", "fingerprint": "900,t,xor"},
    ], 1, writer=writer, sync=True)
    row = conn.execute("SELECT * FROM cache_index WHERE rel_path='m.txt'").fetchone()
    assert row["state"] == "partial"
    assert row["bytes_local"] == 100
    assert row["dirty"] == 1
    assert row["fingerprint"] == "900,t,xor"


def test_upsert_of_nothing_is_a_no_op(repo):
    writer, _conn = repo
    assert rf.upsert_cache_rows(ACC, [], 1, writer=writer, sync=True) == 0


def test_upsert_records_shared_paths(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, entries("a", "b"), 1, writer=writer, sync=True,
                         shared_paths={"a"})
    shared = {r["rel_path"]: r["shared"]
              for r in conn.execute("SELECT rel_path, shared FROM cache_index")}
    assert shared == {"a": 1, "b": 0}


def test_dirty_paths_uses_the_partial_index(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, [
        CacheEntry(rel_path="clean", state=FileState.LOCAL),
        CacheEntry(rel_path="dirty", state=FileState.DIRTY, dirty=True),
    ], 1, writer=writer, sync=True)
    assert rf.dirty_paths(ACC, conn=conn) == ["dirty"]


def test_cache_counts_covers_every_state(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, [
        CacheEntry(rel_path="a", state=FileState.LOCAL, bytes_local=10),
        CacheEntry(rel_path="b", state=FileState.LOCAL, bytes_local=20),
        CacheEntry(rel_path="c", state=FileState.ONLINE_ONLY),
    ], 1, writer=writer, sync=True)
    counts = rf.cache_counts(ACC, conn=conn)
    assert counts["local"] == 2
    assert counts["online_only"] == 1
    assert counts["total"] == 3
    assert counts["bytes_local"] == 30
    for state in FileState:
        assert str(state) in counts


# ═════════════════════════════════════════════════════════════════════════════
# file_state / file_states
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unscanned_path_is_unknown_not_online_only(repo):
    """The Nautilus extension must distinguish 'not scanned' from 'in the cloud'."""
    _writer, conn = repo
    status = rf.file_state(ACC, "never/seen.txt", conn=conn)
    assert status.state is FileState.UNKNOWN
    assert status.rel_path == "never/seen.txt"
    assert status.pinned is False


def test_a_pinned_local_file_reads_as_pinned(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, entries("keep.txt"), 1, writer=writer, sync=True)
    assert rf.file_state(ACC, "keep.txt", conn=conn).state is FileState.LOCAL
    rf.set_pin(ACC, "keep.txt", "pinned", writer=writer)
    status = rf.file_state(ACC, "keep.txt", conn=conn)
    assert status.state is FileState.PINNED
    assert status.pinned is True


def test_an_open_issue_marks_the_path_as_errored(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, entries("bad:name.txt"), 1, writer=writer, sync=True)
    issue_id = rs.raise_issue(SyncIssue(
        account_id=ACC, code=IssueCode.NAME_INVALID,
        severity=IssueSeverity.ERROR, rel_path="bad:name.txt",
        title="Invalid name"), writer=writer)
    assert rf.file_state(ACC, "bad:name.txt", conn=conn).has_error is True
    rs.resolve_issue(issue_id, "renamed", writer=writer)
    assert rf.file_state(ACC, "bad:name.txt", conn=conn).has_error is False


def test_excluded_state_sets_the_excluded_flag(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, [CacheEntry(rel_path="x", state=FileState.EXCLUDED)],
                         1, writer=writer, sync=True)
    status = rf.file_state(ACC, "x", conn=conn)
    assert status.excluded is True
    assert status.state is FileState.EXCLUDED


def test_file_states_answers_every_requested_path_in_order(repo):
    writer, conn = repo
    rf.upsert_cache_rows(ACC, entries("a", "b"), 1, writer=writer, sync=True)
    wanted = ["b", "missing", "a"]
    got = rf.file_states(ACC, wanted, conn=conn)
    assert list(got) == wanted
    assert got["missing"].state is FileState.UNKNOWN
    assert got["a"].state is FileState.LOCAL


def test_file_states_handles_more_paths_than_the_parameter_limit(repo):
    writer, conn = repo
    names = [f"f{n:04d}" for n in range(1200)]
    rf.upsert_cache_rows(ACC, entries(*names), 1, writer=writer, sync=True)
    got = rf.file_states(ACC, names, conn=conn)
    assert len(got) == 1200
    assert all(s.state is FileState.LOCAL for s in got.values())


def test_file_states_of_nothing_is_empty(repo):
    _writer, conn = repo
    assert rf.file_states(ACC, [], conn=conn) == {}


# ═════════════════════════════════════════════════════════════════════════════
# pins
# ═════════════════════════════════════════════════════════════════════════════

def test_set_pin_and_read_it_back(repo):
    writer, conn = repo
    rf.set_pin(ACC, "Docs", "pinned", is_dir=True, bytes_total=5000, writer=writer)
    record = rf.pin_for(ACC, "Docs", conn=conn)
    assert isinstance(record, PinRecord)
    assert record.mode == "pinned"
    assert record.is_dir is True
    assert record.bytes_total == 5000
    assert record.satisfied_at is None


def test_set_pin_rejects_an_unknown_mode(repo):
    writer, _conn = repo
    with pytest.raises(ValueError):
        rf.set_pin(ACC, "x", "sticky", writer=writer)


def test_changing_the_mode_clears_satisfaction(repo):
    writer, conn = repo
    rf.set_pin(ACC, "f", "pinned", writer=writer)
    rf.mark_pin_satisfied(ACC, "f", bytes_local=10, writer=writer)
    writer.flush()
    assert rf.pin_for(ACC, "f", conn=conn).satisfied_at is not None
    rf.set_pin(ACC, "f", "online_only", writer=writer)
    assert rf.pin_for(ACC, "f", conn=conn).satisfied_at is None


def test_re_pinning_the_same_mode_keeps_satisfaction(repo):
    writer, conn = repo
    rf.set_pin(ACC, "f", "pinned", writer=writer)
    rf.mark_pin_satisfied(ACC, "f", writer=writer)
    writer.flush()
    rf.set_pin(ACC, "f", "pinned", writer=writer)
    assert rf.pin_for(ACC, "f", conn=conn).satisfied_at is not None


def test_unsatisfied_pins_is_the_pinner_work_queue(repo):
    writer, conn = repo
    rf.set_pin(ACC, "todo", "pinned", writer=writer)
    rf.set_pin(ACC, "done", "pinned", writer=writer)
    rf.set_pin(ACC, "cloud", "online_only", writer=writer)
    rf.mark_pin_satisfied(ACC, "done", writer=writer)
    writer.flush()
    assert [p.rel_path for p in rf.unsatisfied_pins(ACC, conn=conn)] == ["todo"]
    assert len(rf.pins(ACC, conn=conn)) == 3
    assert len(rf.pins(ACC, mode="pinned", conn=conn)) == 2


def test_a_hydration_failure_leaves_the_pin_unsatisfied(repo):
    writer, conn = repo
    rf.set_pin(ACC, "f", "pinned", writer=writer)
    rf.mark_pin_satisfied(ACC, "f", error="quotaLimitReached", writer=writer)
    writer.flush()
    record = rf.pin_for(ACC, "f", conn=conn)
    assert record.satisfied_at is None
    assert record.last_error == "quotaLimitReached"
    assert [p.rel_path for p in rf.unsatisfied_pins(ACC, conn=conn)] == ["f"]


def test_clear_pin(repo):
    writer, conn = repo
    rf.set_pin(ACC, "f", "pinned", writer=writer)
    assert rf.clear_pin(ACC, "f", writer=writer) is True
    assert rf.clear_pin(ACC, "f", writer=writer) is False
    assert rf.pin_for(ACC, "f", conn=conn) is None


def test_a_pin_is_deleted_with_its_account(repo):
    writer, conn = repo
    rf.set_pin(ACC, "f", "pinned", writer=writer)
    writer.submit_sync(lambda c: c.execute("DELETE FROM accounts WHERE id = ?",
                                           (ACC,)), urgent=True)
    assert conn.execute("SELECT count(*) FROM pins").fetchone()[0] == 0


# ═════════════════════════════════════════════════════════════════════════════
# activity
# ═════════════════════════════════════════════════════════════════════════════

def event(**kw) -> ActivityEvent:
    base = dict(account_id=ACC, rel_path="Documents/Report.docx",
                name="Report.docx", verb=ActivityVerb.UPLOADED, direction="up",
                state=ActivityState.DONE, bytes=4096, size=4096,
                started_at=utcnow_iso())
    base.update(kw)
    return ActivityEvent(**base)


def test_append_and_read_back_activity(repo):
    writer, conn = repo
    rowid = rs.append_activity(event(), writer=writer, sync=True)
    assert rowid is not None
    rows = rs.recent_activity(ACC, conn=conn)
    assert len(rows) == 1
    assert rows[0].verb is ActivityVerb.UPLOADED
    assert rows[0].name == "Report.docx"
    assert rows[0].bytes == 4096
    assert rows[0].percentage == 100


def test_a_duplicate_dedupe_key_is_dropped(repo):
    """core/transferred re-reports the same completion after a daemon restart."""
    writer, conn = repo
    first = rs.append_activity(event(dedupe_key="sha1:abc"), writer=writer,
                               sync=True)
    second = rs.append_activity(event(dedupe_key="sha1:abc"), writer=writer,
                                sync=True)
    assert first is not None
    assert second is None
    assert len(rs.recent_activity(ACC, conn=conn)) == 1


def test_rows_without_a_dedupe_key_are_never_merged(repo):
    writer, conn = repo
    rs.append_activity(event(), writer=writer, sync=True)
    rs.append_activity(event(), writer=writer, sync=True)
    assert len(rs.recent_activity(ACC, conn=conn)) == 2


def test_recent_activity_is_newest_first_and_limited(repo):
    writer, conn = repo
    for n in range(10):
        rs.append_activity(event(name=f"f{n}", rel_path=f"f{n}"), writer=writer)
    writer.flush()
    rows = rs.recent_activity(ACC, limit=3, conn=conn)
    assert len(rows) == 3
    assert rows[0].name == "f9"


def test_recent_activity_can_filter_by_verb(repo):
    writer, conn = repo
    rs.append_activity(event(verb=ActivityVerb.UPLOADED), writer=writer)
    rs.append_activity(event(verb=ActivityVerb.DELETED, rel_path="gone"),
                       writer=writer)
    writer.flush()
    rows = rs.recent_activity(ACC, verbs=[ActivityVerb.DELETED], conn=conn)
    assert [r.verb for r in rows] == [ActivityVerb.DELETED]


def test_activity_for_path(repo):
    writer, conn = repo
    rs.append_activity(event(rel_path="a", name="a"), writer=writer)
    rs.append_activity(event(rel_path="b", name="b"), writer=writer)
    writer.flush()
    assert [r.rel_path for r in rs.activity_for_path(ACC, "a", conn=conn)] == ["a"]


def test_update_activity_advances_an_inflight_row(repo):
    writer, conn = repo
    rowid = rs.append_activity(event(state=ActivityState.INFLIGHT, bytes=0),
                               writer=writer, sync=True)
    rs.update_activity(rowid, bytes_done=2048, writer=writer)
    writer.flush()
    assert rs.recent_activity(ACC, conn=conn)[0].bytes == 2048
    rs.update_activity(rowid, state=ActivityState.DONE, bytes_done=4096,
                       writer=writer)
    writer.flush()
    row = rs.recent_activity(ACC, conn=conn)[0]
    assert row.state is ActivityState.DONE
    assert row.completed_at


def test_update_activity_with_nothing_to_do_is_a_no_op(repo):
    writer, _conn = repo
    rs.update_activity(1, writer=writer)         # must not raise


def test_inflight_rows_become_interrupted_not_errored(repo):
    """ARCHITECTURE §5.7: the outcome is UNKNOWN, which is not a failure."""
    writer, conn = repo
    rs.append_activity(event(state=ActivityState.INFLIGHT), writer=writer)
    rs.append_activity(event(rel_path="two", state=ActivityState.INFLIGHT),
                       writer=writer)
    rs.append_activity(event(rel_path="three", state=ActivityState.DONE),
                       writer=writer)
    writer.flush()
    assert rs.mark_inflight_interrupted(ACC, writer=writer) == 2
    states = {r.rel_path: r.state for r in rs.recent_activity(ACC, conn=conn)}
    assert states["three"] is ActivityState.DONE
    assert set(states.values()) == {ActivityState.DONE,
                                    ActivityState.INTERRUPTED}


def test_an_unknown_verb_from_a_newer_version_does_not_crash(repo):
    writer, conn = repo
    writer.submit_sync(lambda c: c.execute(
        "INSERT INTO activity (account_id, rel_path, name, verb, state, "
        "started_at) VALUES (?,?,?,?,?,?)",
        (ACC, "f", "f", "teleported", "done", utcnow_iso())), urgent=True)
    assert rs.recent_activity(ACC, conn=conn)[0].verb is ActivityVerb.MODIFIED


# ═════════════════════════════════════════════════════════════════════════════
# issues
# ═════════════════════════════════════════════════════════════════════════════

def issue(**kw) -> SyncIssue:
    base = dict(account_id=ACC, code=IssueCode.NAME_INVALID,
                severity=IssueSeverity.ERROR, rel_path="bad:name.txt",
                title="That file name isn't allowed", detail="d",
                raw_error="raw",
                actions=(RecoveryAction.RENAME, RecoveryAction.SKIP))
    base.update(kw)
    return SyncIssue(**base)


def test_raise_issue_stores_the_recovery_actions(repo):
    writer, conn = repo
    issue_id = rs.raise_issue(issue(), writer=writer)
    stored = rs.open_issues(ACC, conn=conn)[0]
    assert stored.id == issue_id
    assert stored.actions == (RecoveryAction.RENAME, RecoveryAction.SKIP)
    assert stored.code is IssueCode.NAME_INVALID
    assert stored.occurrences == 1


def test_raising_the_same_issue_bumps_occurrences(repo):
    """ux_issue_open: one row, not a thousand."""
    writer, conn = repo
    first = rs.raise_issue(issue(), writer=writer)
    for _ in range(5):
        assert rs.raise_issue(issue(), writer=writer) == first
    rows = rs.open_issues(ACC, conn=conn)
    assert len(rows) == 1
    assert rows[0].occurrences == 6
    assert rows[0].id == first


def test_two_paths_with_the_same_code_are_two_issues(repo):
    writer, conn = repo
    rs.raise_issue(issue(rel_path="a"), writer=writer)
    rs.raise_issue(issue(rel_path="b"), writer=writer)
    assert len(rs.open_issues(ACC, conn=conn)) == 2


def test_a_null_rel_path_still_deduplicates(repo):
    """IFNULL(rel_path,'') in the index makes account-wide issues unique too."""
    writer, conn = repo
    rs.raise_issue(issue(rel_path=None), writer=writer)
    rs.raise_issue(issue(rel_path=None), writer=writer)
    rows = rs.open_issues(ACC, conn=conn)
    assert len(rows) == 1
    assert rows[0].occurrences == 2


def test_resolving_then_re_raising_opens_a_new_row(repo):
    writer, conn = repo
    first = rs.raise_issue(issue(), writer=writer)
    assert rs.resolve_issue(first, "renamed", writer=writer) is True
    assert rs.resolve_issue(first, "renamed", writer=writer) is False
    assert rs.open_issues(ACC, conn=conn) == []
    second = rs.raise_issue(issue(), writer=writer)
    assert second != first
    assert len(rs.open_issues(ACC, conn=conn)) == 1


def test_issue_counts_covers_every_severity(repo):
    writer, conn = repo
    rs.raise_issue(issue(severity=IssueSeverity.BLOCKING,
                         code=IssueCode.QUOTA_EXCEEDED, rel_path=None),
                   writer=writer)
    rs.raise_issue(issue(rel_path="a"), writer=writer)
    rs.raise_issue(issue(rel_path="b"), writer=writer)
    counts = rs.issue_counts(ACC, conn=conn)
    assert counts["blocking"] == 1
    assert counts["error"] == 2
    assert counts["warning"] == 0
    assert counts["info"] == 0
    assert counts["total"] == 3
    for severity in IssueSeverity:
        assert str(severity) in counts


def test_a_muted_issue_does_not_count_toward_the_state(repo):
    writer, conn = repo
    issue_id = rs.raise_issue(issue(), writer=writer)
    rs.mute_issue(issue_id, writer=writer)
    writer.flush()
    assert rs.issue_counts(ACC, conn=conn)["total"] == 0
    assert rs.issue_counts(ACC, include_muted=True, conn=conn)["total"] == 1
    assert len(rs.open_issues(ACC, include_muted=False, conn=conn)) == 0
    assert len(rs.open_issues(ACC, conn=conn)) == 1


def test_open_issues_can_filter_by_severity(repo):
    writer, conn = repo
    rs.raise_issue(issue(severity=IssueSeverity.WARNING, rel_path="w"),
                   writer=writer)
    rs.raise_issue(issue(rel_path="e"), writer=writer)
    rows = rs.open_issues(ACC, severity=IssueSeverity.WARNING, conn=conn)
    assert [r.rel_path for r in rows] == ["w"]


def test_resolve_issues_by_code_returns_the_ids(repo):
    writer, conn = repo
    ids = {rs.raise_issue(issue(rel_path=f"f{n}"), writer=writer)
           for n in range(3)}
    closed = rs.resolve_issues_by_code(ACC, IssueCode.NAME_INVALID, "auto",
                                       writer=writer)
    assert set(closed) == ids
    assert rs.open_issues(ACC, conn=conn) == []


def test_resolve_issues_by_code_can_target_one_path(repo):
    writer, conn = repo
    rs.raise_issue(issue(rel_path="a"), writer=writer)
    rs.raise_issue(issue(rel_path="b"), writer=writer)
    rs.resolve_issues_by_code(ACC, IssueCode.NAME_INVALID, rel_path="a",
                              writer=writer)
    assert [i.rel_path for i in rs.open_issues(ACC, conn=conn)] == ["b"]


# ═════════════════════════════════════════════════════════════════════════════
# latches — urgent
# ═════════════════════════════════════════════════════════════════════════════

def test_a_latch_is_durable_before_set_latch_returns(repo):
    """A hazard that does not survive a SIGKILL is not a latch."""
    writer, _conn = repo
    rs.set_latch(ACC, "needs_resync", "filters changed", writer=writer)
    independent = sqlite3.connect(f"file:{writer.path}?mode=ro", uri=True)
    try:
        row = independent.execute(
            "SELECT name, detail FROM latches").fetchone()
    finally:
        independent.close()
    assert row == ("needs_resync", "filters changed")


def test_latches_returns_a_frozenset_shaped_for_facts(repo):
    writer, conn = repo
    rs.set_latch(ACC, "needs_resync", writer=writer)
    rs.set_latch(ACC, "mount_failed", writer=writer)
    result = rs.latches(ACC, conn=conn)
    assert isinstance(result, frozenset)
    assert result == {"needs_resync", "mount_failed"}


def test_setting_a_latch_twice_bumps_its_counter(repo):
    """The counter drives the restart ladders in §5.7."""
    writer, conn = repo
    assert rs.set_latch(ACC, "mount_failed", writer=writer) == 0
    assert rs.set_latch(ACC, "mount_failed", writer=writer) == 1
    assert rs.set_latch(ACC, "mount_failed", writer=writer) == 2
    assert rs.latch_detail(ACC, conn=conn)["mount_failed"]["counter"] == 2


def test_a_latch_can_be_refreshed_without_bumping(repo):
    writer, conn = repo
    rs.set_latch(ACC, "mount_failed", "first", writer=writer)
    rs.set_latch(ACC, "mount_failed", "second", increment=False, writer=writer)
    detail = rs.latch_detail(ACC, conn=conn)["mount_failed"]
    assert detail["counter"] == 0
    assert detail["detail"] == "second"


def test_clear_latch_is_durable_and_honest(repo):
    writer, conn = repo
    rs.set_latch(ACC, "needs_resync", writer=writer)
    assert rs.clear_latch(ACC, "needs_resync", writer=writer) is True
    assert rs.clear_latch(ACC, "needs_resync", writer=writer) is False
    assert rs.latches(ACC, conn=conn) == frozenset()


def test_clear_all_latches(repo):
    writer, conn = repo
    for name in rs.LATCH_NAMES:
        rs.set_latch(ACC, name, writer=writer)
    assert len(rs.latches(ACC, conn=conn)) == len(rs.LATCH_NAMES)
    assert rs.clear_all_latches(ACC, ["needs_resync"], writer=writer) == 1
    assert rs.clear_all_latches(ACC, [], writer=writer) == 0
    assert rs.clear_all_latches(ACC, writer=writer) == len(rs.LATCH_NAMES) - 1
    assert rs.latches(ACC, conn=conn) == frozenset()


def test_the_documented_latch_names_are_the_schema_comment(repo):
    assert rs.LATCH_NAMES == ("needs_resync", "bisync_critical",
                              "quota_exceeded", "mount_failed", "orphan_cache")


# ═════════════════════════════════════════════════════════════════════════════
# decisions — urgent
# ═════════════════════════════════════════════════════════════════════════════

def test_a_decision_is_durable_before_create_decision_returns(repo):
    writer, _conn = repo
    decision_id = rs.create_decision(Decision(
        account_id=ACC, kind=DecisionKind.MASS_DELETE,
        payload={"count": 4211}), writer=writer)
    independent = sqlite3.connect(f"file:{writer.path}?mode=ro", uri=True)
    try:
        row = independent.execute(
            "SELECT id, payload FROM decisions").fetchone()
    finally:
        independent.close()
    assert row[0] == decision_id
    assert "4211" in row[1]


def test_a_decision_gets_a_seven_day_expiry(repo):
    writer, conn = repo
    rs.create_decision(Decision(account_id=ACC, created_at="2026-08-31T12:00:00Z"),
                       writer=writer)
    pending = rs.pending_decisions(ACC, conn=conn)[0]
    assert pending.expires_at == "2026-09-07T12:00:00Z"
    assert DECISION_EXPIRY_DAYS == 7
    # Same format as every other timestamp, so a TEXT comparison is valid.
    assert pending.expires_at.endswith("Z") and "T" in pending.expires_at


def test_answering_a_decision_is_durable_and_honest(repo):
    writer, conn = repo
    decision_id = rs.create_decision(Decision(account_id=ACC), writer=writer)
    assert rs.answer_decision(decision_id, "yes", writer=writer) is True
    assert rs.answer_decision(decision_id, "no", writer=writer) is False
    assert rs.pending_decisions(ACC, conn=conn) == []
    row = conn.execute("SELECT answer FROM decisions").fetchone()
    assert row[0] == "yes"


def test_pending_decisions_filters_and_orders(repo):
    writer, conn = repo
    rs.create_decision(Decision(account_id=ACC, kind=DecisionKind.MASS_DELETE,
                                created_at="2026-08-31T10:00:00Z"),
                       writer=writer)
    rs.create_decision(Decision(account_id=ACC, kind=DecisionKind.RESYNC_CONFIRM,
                                created_at="2026-08-31T11:00:00Z"),
                       writer=writer)
    rows = rs.pending_decisions(ACC, conn=conn)
    assert [d.kind for d in rows] == [DecisionKind.MASS_DELETE,
                                      DecisionKind.RESYNC_CONFIRM]
    assert len(rs.pending_decisions(ACC, kind=DecisionKind.RESYNC_CONFIRM,
                                    conn=conn)) == 1
    assert len(rs.pending_decisions(conn=conn)) == 2


def test_an_expired_decision_is_answered_with_a_refusal_not_deleted(repo):
    """Microsoft's rule: expiry means DO NOT DELETE."""
    writer, conn = repo
    old = rs.create_decision(
        Decision(account_id=ACC, created_at="2020-01-01T00:00:00Z",
                 expires_at="2020-01-08T00:00:00Z"), writer=writer)
    fresh = rs.create_decision(Decision(account_id=ACC), writer=writer)
    expired = rs.expire_decisions(ACC, writer=writer)
    assert expired == [old]
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 2
    answer = conn.execute("SELECT answer FROM decisions WHERE id = ?",
                          (old,)).fetchone()[0]
    assert answer == rs.EXPIRED_ANSWER
    assert [d.id for d in rs.pending_decisions(ACC, conn=conn)] == [fresh]


def test_an_expired_but_unanswered_decision_is_still_pending(repo):
    """Expiry is a policy the supervisor applies, not an amnesia."""
    writer, conn = repo
    rs.create_decision(Decision(account_id=ACC, expires_at="2020-01-01T00:00:00Z"),
                       writer=writer)
    assert len(rs.pending_decisions(ACC, conn=conn)) == 1


def test_a_decision_payload_survives_the_round_trip(repo):
    writer, conn = repo
    payload = {"count": 4211, "paths": ["a", "b"], "nested": {"x": True}}
    rs.create_decision(Decision(account_id=ACC, payload=payload), writer=writer)
    assert rs.pending_decisions(ACC, conn=conn)[0].payload == payload


def test_a_malformed_payload_reads_back_as_an_empty_dict(repo):
    writer, conn = repo
    writer.submit_sync(lambda c: c.execute(
        "INSERT INTO decisions (account_id, kind, payload, created_at) "
        "VALUES (?,?,?,?)", (ACC, "mass_delete", "not json", utcnow_iso())),
        urgent=True)
    assert rs.pending_decisions(ACC, conn=conn)[0].payload == {}


# ═════════════════════════════════════════════════════════════════════════════
# runs
# ═════════════════════════════════════════════════════════════════════════════

def run_record(**kw) -> RunRecord:
    base = dict(run_id="r1", account_id=ACC, kind=RunKind.BISYNC,
                argv=("rclone", "bisync", "--resilient"),
                started_at=utcnow_iso(), log_path="/tmp/r1/bisync.jsonl",
                unit="onedriveui-bisync-onedrive", session="a..b")
    base.update(kw)
    return RunRecord(**base)


def test_start_and_finish_a_run(repo):
    writer, conn = repo
    assert rs.start_run(run_record(), writer=writer) == "r1"
    opened = rs.last_run(ACC, conn=conn)
    assert opened.argv == ("rclone", "bisync", "--resilient")
    assert opened.verdict is RunVerdict.UNKNOWN
    assert opened.ended_at is None

    rs.finish_run(run_record(exit_code=0, verdict=RunVerdict.OK,
                             files_transferred=12, bytes=4096, log_offset=999,
                             summary="Bisync successful"), writer=writer)
    finished = rs.last_run(ACC, conn=conn)
    assert finished.verdict is RunVerdict.OK
    assert finished.exit_code == 0
    assert finished.files_transferred == 12
    assert finished.log_offset == 999
    assert finished.ended_at


def test_start_run_is_idempotent_for_the_same_run_id(repo):
    writer, conn = repo
    rs.start_run(run_record(), writer=writer)
    rs.start_run(run_record(unit="renamed"), writer=writer)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    assert rs.last_run(ACC, conn=conn).unit == "renamed"


def test_set_run_offset_checkpoints_the_tailer(repo):
    """Without this a GUI restart replays the log and duplicates conflicts."""
    writer, conn = repo
    rs.start_run(run_record(), writer=writer)
    rs.set_run_offset("r1", 4096, writer=writer)
    writer.flush()
    assert rs.last_run(ACC, conn=conn).log_offset == 4096


def test_last_run_filters_by_kind_and_completion(repo):
    writer, conn = repo
    rs.start_run(run_record(run_id="b", kind=RunKind.BISYNC,
                            started_at="2026-08-31T10:00:00Z"), writer=writer)
    rs.start_run(run_record(run_id="v", kind=RunKind.VERIFY,
                            started_at="2026-08-31T11:00:00Z"), writer=writer)
    assert rs.last_run(ACC, conn=conn).run_id == "v"
    assert rs.last_run(ACC, RunKind.BISYNC, conn=conn).run_id == "b"
    assert rs.last_run(ACC, finished_only=True, conn=conn) is None
    rs.finish_run(run_record(run_id="b", verdict=RunVerdict.OK), writer=writer)
    assert rs.last_run(ACC, finished_only=True, conn=conn).run_id == "b"


def test_last_run_of_an_account_with_no_runs_is_none(repo):
    _writer, conn = repo
    assert rs.last_run(ACC, conn=conn) is None


def test_recent_runs_is_newest_first(repo):
    writer, conn = repo
    for n in range(5):
        rs.start_run(run_record(run_id=f"r{n}",
                                started_at=f"2026-08-3{n}T10:00:00Z"),
                     writer=writer)
    assert [r.run_id for r in rs.recent_runs(ACC, limit=2, conn=conn)] == \
        ["r4", "r3"]
    assert len(rs.recent_runs(ACC, kind=RunKind.VERIFY, conn=conn)) == 0


# ═════════════════════════════════════════════════════════════════════════════
# conflicts
# ═════════════════════════════════════════════════════════════════════════════

def test_add_and_resolve_a_conflict(repo):
    writer, conn = repo
    conflict_id = rs.add_conflict(ConflictInfo(
        account_id=ACC, rel_path="Report.docx",
        loser_path="Report-testhost.docx", winner_side="remote",
        local_size=10, local_mtime="t1", remote_size=20, remote_mtime="t2"),
        writer=writer)
    rows = rs.open_conflicts(ACC, conn=conn)
    assert len(rows) == 1
    assert rows[0].loser_path == "Report-testhost.docx"
    assert rows[0].local_size == 10
    assert rs.resolve_conflict(conflict_id, "keep_both", writer=writer) is True
    assert rs.resolve_conflict(conflict_id, "keep_both", writer=writer) is False
    assert rs.open_conflicts(ACC, conn=conn) == []


def test_re_reporting_the_same_conflict_updates_one_row(repo):
    """ux_conflict_open: the user must not have to dismiss it twice."""
    writer, conn = repo
    first = rs.add_conflict(ConflictInfo(
        account_id=ACC, rel_path="R.docx", loser_path="R-host.docx",
        detected_at="2026-08-31T10:00:00Z"), writer=writer)
    second = rs.add_conflict(ConflictInfo(
        account_id=ACC, rel_path="R.docx", loser_path="R-host.docx",
        detected_at="2026-08-31T11:00:00Z"), writer=writer)
    assert first == second
    rows = rs.open_conflicts(ACC, conn=conn)
    assert len(rows) == 1
    assert rows[0].detected_at == "2026-08-31T11:00:00Z"


def test_resolving_a_conflict_frees_the_loser_path_again(repo):
    writer, conn = repo
    first = rs.add_conflict(ConflictInfo(account_id=ACC, rel_path="R",
                                         loser_path="R-host"), writer=writer)
    rs.resolve_conflict(first, "keep_local", writer=writer)
    second = rs.add_conflict(ConflictInfo(account_id=ACC, rel_path="R",
                                          loser_path="R-host"), writer=writer)
    assert second != first
    assert len(rs.open_conflicts(ACC, conn=conn)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# versions, trash, links
# ═════════════════════════════════════════════════════════════════════════════

def test_versions_are_newest_first(repo):
    writer, conn = repo
    for n in range(3):
        rf.add_version(VersionEntry(
            account_id=ACC, rel_path="R.docx",
            backup_path=f"versions/{n}/R.docx", side="local",
            captured_at=f"2026-08-3{n}T10:00:00Z", size=100 * n,
            reason="overwrite"), writer=writer)
    rows = rf.versions_for(ACC, "R.docx", conn=conn)
    assert [v.backup_path for v in rows] == ["versions/2/R.docx",
                                             "versions/1/R.docx",
                                             "versions/0/R.docx"]
    assert rf.versions_for(ACC, "other", conn=conn) == []


def test_trash_round_trip(repo):
    writer, conn = repo
    trash_id = rf.add_trash(TrashEntry(
        account_id=ACC, rel_path="Old.txt",
        trash_path=".onedriveui-trash/2026/Old.txt", size=42,
        deleted_at="2026-08-01T00:00:00Z",
        purge_after="2026-08-31T00:00:00Z"), writer=writer)
    rows = rf.trash_items(ACC, conn=conn)
    assert len(rows) == 1
    assert rows[0].rel_path == "Old.txt"
    assert rf.mark_restored(trash_id, writer=writer) is True
    assert rf.mark_restored(trash_id, writer=writer) is False
    assert rf.trash_items(ACC, conn=conn) == []
    assert len(rf.trash_items(ACC, include_restored=True, conn=conn)) == 1


def test_purge_due_returns_rather_than_deletes(repo):
    """Removing the row before the bytes are gone would orphan the file."""
    writer, conn = repo
    rf.add_trash(TrashEntry(account_id=ACC, rel_path="Old", trash_path="t/Old",
                            deleted_at="2020-01-01T00:00:00Z",
                            purge_after="2020-01-31T00:00:00Z"), writer=writer)
    rf.add_trash(TrashEntry(account_id=ACC, rel_path="New", trash_path="t/New",
                            deleted_at="2026-08-31T00:00:00Z",
                            purge_after="2099-01-01T00:00:00Z"), writer=writer)
    due = rf.purge_due(ACC, conn=conn)
    assert [t.rel_path for t in due] == ["Old"]
    assert conn.execute("SELECT count(*) FROM trashbin").fetchone()[0] == 2


def test_the_documented_retention_windows(repo):
    assert rf.TRASH_RETENTION_DAYS == {"personal": 30, "business": 93}


def test_share_links_revocation_is_local_bookkeeping(repo):
    """rclone --unlink is a no-op on OneDrive: the URL keeps working."""
    writer, conn = repo
    link_id = rf.record_link(ShareLink(
        account_id=ACC, rel_path="R.docx", url="https://1drv.ms/abc",
        scope=LinkScope.ANONYMOUS, link_type=LinkType.VIEW), writer=writer)
    rows = rf.links_for(ACC, "R.docx", conn=conn)
    assert len(rows) == 1
    assert rows[0].scope is LinkScope.ANONYMOUS
    assert rows[0].link_type is LinkType.VIEW
    assert rf.revoke_link(link_id, writer=writer) is True
    assert rf.revoke_link(link_id, writer=writer) is False
    assert rf.links_for(ACC, "R.docx", conn=conn) == []
    assert len(rf.links_for(ACC, "R.docx", include_revoked=True, conn=conn)) == 1
    assert len(rf.links_for(ACC, conn=conn)) == 0


# ═════════════════════════════════════════════════════════════════════════════
# notifications
# ═════════════════════════════════════════════════════════════════════════════

def test_should_show_is_true_for_an_unseen_key(repo):
    _writer, conn = repo
    assert rf.should_show("quota_full:onedrive", conn=conn) is True


def test_note_notification_then_rate_limit(repo):
    writer, conn = repo
    rf.note_notification("quota_full:onedrive", account_id=ACC, dbus_id=7,
                         payload={"pct": 99}, writer=writer)
    assert rf.should_show("quota_full:onedrive", min_interval_s=0,
                          conn=conn) is True
    assert rf.should_show("quota_full:onedrive", min_interval_s=3600,
                          conn=conn) is False
    row = conn.execute("SELECT dbus_id, payload FROM notifications").fetchone()
    assert row["dbus_id"] == 7
    assert "99" in row["payload"]


def test_suppression_survives_a_restart(repo):
    """A crash loop must not produce a toast storm."""
    writer, conn = repo
    rf.note_notification("k", suppress_for_s=3600, writer=writer)
    assert rf.should_show("k", conn=conn) is False
    assert rf.should_show("k", now="2099-01-01T00:00:00Z", conn=conn) is True


def test_suppress_notification_without_showing_it(repo):
    writer, conn = repo
    rf.suppress_notification("k", 600, account_id=ACC, writer=writer)
    assert rf.should_show("k", conn=conn) is False


# ═════════════════════════════════════════════════════════════════════════════
# kv, selection, kfm, dialogs
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("value", [
    True, False, 0, 42, -1, 3.5, "text", None, [1, 2, 3], {"a": {"b": 1}},
])
def test_kv_round_trips_every_json_type(repo, value):
    writer, conn = repo
    rf.kv_set("k", value, writer=writer)
    assert rf.kv_get("k", conn=conn) == value


def test_kv_types_are_preserved_not_stringified(repo):
    writer, conn = repo
    rf.kv_set("flag", True, writer=writer)
    assert rf.kv_get("flag", conn=conn) is True
    assert rf.kv_get("flag", conn=conn) != "True"


def test_kv_is_scoped_per_account(repo):
    writer, conn = repo
    rf.kv_set("k", "global", writer=writer)
    rf.kv_set("k", "account", account_id=ACC, writer=writer)
    assert rf.kv_get("k", conn=conn) == "global"
    assert rf.kv_get("k", account_id=ACC, conn=conn) == "account"


def test_kv_missing_returns_the_default(repo):
    _writer, conn = repo
    assert rf.kv_get("absent", "fallback", conn=conn) == "fallback"


def test_kv_reads_a_hand_written_non_json_row(repo):
    writer, conn = repo
    writer.submit_sync(lambda c: c.execute(
        "INSERT INTO kv (account_id, key, value, updated_at) "
        "VALUES ('', 'hand', 'plain text', 't')"), urgent=True)
    assert rf.kv_get("hand", conn=conn) == "plain text"


def test_kv_delete(repo):
    writer, conn = repo
    rf.kv_set("k", 1, writer=writer)
    assert rf.kv_delete("k", writer=writer) is True
    assert rf.kv_delete("k", writer=writer) is False
    assert rf.kv_get("k", conn=conn) is None


def test_kv_set_can_be_urgent(repo):
    writer, _conn = repo
    rf.kv_set("k", "durable", urgent=True, writer=writer)
    independent = sqlite3.connect(f"file:{writer.path}?mode=ro", uri=True)
    try:
        assert independent.execute(
            "SELECT value FROM kv WHERE key='k'").fetchone()[0] == '"durable"'
    finally:
        independent.close()


def test_folder_selection(repo):
    writer, conn = repo
    rf.set_selection(ACC, "Photos", False, size_bytes=1_000_000,
                     item_count=42, writer=writer)
    rf.set_selection(ACC, "Documents", True, writer=writer)
    assert rf.selection(ACC, conn=conn) == {"Photos": False, "Documents": True}
    assert rf.excluded_paths(ACC, conn=conn) == ["Photos"]
    row = conn.execute("SELECT size_bytes, item_count FROM folder_selection "
                       "WHERE rel_path='Photos'").fetchone()
    assert row["size_bytes"] == 1_000_000
    assert row["item_count"] == 42


def test_re_selecting_keeps_the_measured_size(repo):
    writer, conn = repo
    rf.set_selection(ACC, "Photos", False, size_bytes=999, writer=writer)
    rf.set_selection(ACC, "Photos", True, writer=writer)
    row = conn.execute("SELECT selected, size_bytes FROM folder_selection")\
        .fetchone()
    assert row["selected"] == 1
    assert row["size_bytes"] == 999


def test_kfm_folder_records_the_original_path(repo):
    """Opting out has to put the folder back where it came from."""
    writer, conn = repo
    rf.set_kfm_folder(ACC, KfmFolder.DESKTOP, True,
                      original_path="/home/u/Desktop",
                      target_path="/home/u/OneDrive/Desktop",
                      journal_path="/tmp/kfm.journal", writer=writer)
    folders = rf.kfm_folders(ACC, conn=conn)
    assert folders["desktop"]["enabled"] is True
    assert folders["desktop"]["original_path"] == "/home/u/Desktop"
    assert folders["desktop"]["moved_at"]

    rf.set_kfm_folder(ACC, KfmFolder.DESKTOP, False, writer=writer)
    folders = rf.kfm_folders(ACC, conn=conn)
    assert folders["desktop"]["enabled"] is False
    assert folders["desktop"]["original_path"] == "/home/u/Desktop"   # kept


def test_dialog_seen(repo):
    writer, conn = repo
    key = DialogKey.FIRST_DELETE
    assert rf.dialog_seen(key, conn=conn) is False
    rf.mark_dialog_seen(key, writer=writer)
    assert rf.dialog_seen(key, conn=conn) is True
    rf.mark_dialog_seen(key, writer=writer)          # idempotent
    assert conn.execute("SELECT count(*) FROM dialog_seen").fetchone()[0] == 1


# ═════════════════════════════════════════════════════════════════════════════
# The repositories do not emit signals
# ═════════════════════════════════════════════════════════════════════════════

def test_the_repos_do_not_import_the_bus():
    """Signals belong to the owning service, which knows what is news.

    Checked on the parsed import statements rather than on the source text,
    because the docstrings legitimately NAME the signals their callers emit.
    """
    import ast
    from pathlib import Path

    for module in (rs, rf):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "onedriveui.bus" not in imported, module.__name__
        assert not hasattr(module, "BUS"), module.__name__


def test_writing_a_row_emits_nothing(repo, bus_spy, qtbot):
    writer, _conn = repo
    bus_spy.watch_all()
    rs.append_activity(event(), writer=writer, sync=True)
    rs.raise_issue(issue(), writer=writer)
    rs.set_latch(ACC, "needs_resync", writer=writer)
    rf.set_pin(ACC, "f", "pinned", writer=writer)
    qtbot.process(3)
    assert [name for name in bus_spy.names() if name != "log_line"] == []
