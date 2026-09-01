"""WP-03 — `onedriveui/rc/stats.py`.

The payloads below are the verbatim captures from
`docs/research/rclone-rc-api.md` §3.7–3.9, so the parser is tested against what
rclone v1.75.0 actually sends rather than against what its help says:

  * the idle body really does **omit** `transferring`, `checking` and
    `lastError` entirely;
  * `eta` really is present-and-`null` when indeterminate;
  * `checking` really is a list of plain strings while `transferring` is a list
    of objects;
  * `core/transferred` really answers `started_at` / `completed_at` / `group` /
    `srcFs` / `dstFs` and **no** `timestamp` and **no** `jobid`, contradicting
    its own built-in help.

The safety property under test is the ordering: a drain always precedes a
`core/stats-reset`, because the reset wipes `core/transferred` along with the
counters and those 100 rows are the only record a transfer ever happened.
"""

from __future__ import annotations

import pytest

from onedriveui import paths
from onedriveui.bus import BUS
from onedriveui.constants import TICK_ACTIVE_MS, TICK_IDLE_MS, TICK_PAUSED_MS
from onedriveui.data import db
from onedriveui.data.writer import DbWriter
from onedriveui.errors import RcError
from onedriveui.models import (
    ActivityState, ActivityVerb, CoreStats, IssueCode, TransferInfo, utcnow_iso,
)
from onedriveui.rc import stats as rcstats
from tests.fakes import fake_rc as fake_rc_module

ACC = "onedrive"
GROUP = "onedriveui/pin/onedrive"

# ── verbatim captures ────────────────────────────────────────────────────────

IDLE_STATS = {
    "bytes": 0, "checks": 0, "deletedDirs": 0, "deletes": 0,
    "elapsedTime": 7.925e-06, "errors": 0, "eta": None, "fatalError": False,
    "listed": 0, "renames": 0, "retryError": False,
    "serverSideCopies": 0, "serverSideCopyBytes": 0,
    "serverSideMoveBytes": 0, "serverSideMoves": 0,
    "speed": 0, "totalBytes": 0, "totalChecks": 0, "totalTransfers": 0,
    "transferTime": 0, "transfers": 0,
}

ACTIVE_STATS = {
    "bytes": 5332998, "checks": 0, "deletedDirs": 0, "deletes": 0,
    "elapsedTime": 5.03430132, "errors": 0, "eta": 42, "fatalError": False,
    "listed": 6, "renames": 0, "retryError": False,
    "serverSideCopies": 0, "serverSideCopyBytes": 0,
    "serverSideMoveBytes": 0, "serverSideMoves": 0,
    "speed": 1060029.116061751, "totalBytes": 50331660, "totalChecks": 0,
    "totalTransfers": 5, "transferTime": 5.034144617,
    "transferring": [
        {"bytes": 2682880, "dstFs": "/tmp/x/dst3", "eta": 10, "group": "bigjob",
         "name": "big.bin", "percentage": 31, "size": 8388608,
         "speed": 533074.320824698, "speedAvg": 536569.9085890673,
         "srcFs": "/tmp/x/t"},
        {"bytes": 40, "dstFs": "onedrive:", "eta": None, "group": "bigjob",
         "name": "up.bin", "percentage": 4, "size": 1000,
         "speed": 0.0, "speedAvg": 0.0, "srcFs": "/home/u/OneDrive"},
    ],
    "transfers": 1,
}

TRANSFERRED = {"transferred": [
    {"error": "", "name": "a.txt", "size": 6, "bytes": 6, "checked": False,
     "what": "transferring",
     "started_at": "2026-08-30T23:26:05.895290105-04:00",
     "completed_at": "2026-08-30T23:26:05.895521017-04:00",
     "group": GROUP, "srcFs": "/home/u/OneDrive", "dstFs": "onedrive:"},
    {"error": "context canceled", "name": "big.bin", "size": 8388608,
     "bytes": 7008256, "checked": False, "what": "transferring",
     "started_at": "2026-08-30T23:26:00.000000000-04:00",
     "completed_at": "2026-08-30T23:26:19.000000000-04:00",
     "group": GROUP, "srcFs": "onedrive:", "dstFs": "/home/u/OneDrive"},
]}


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def rc(fake_rc, monkeypatch):
    """The fake daemon, with the blocking helpers routed into it."""
    fake_rc.deliver_mode = "manual"
    monkeypatch.setattr(rcstats, "call_blocking", fake_rc_module.call_blocking)
    return fake_rc


@pytest.fixture
def writer(_isolate_home, qapp):
    """A started `DbWriter` with the fake account seeded, plus a reader."""
    wr = DbWriter(paths.db_file())
    assert wr.start_writer()
    wr.submit_sync(
        lambda conn: conn.execute(
            "INSERT INTO accounts (id, remote, sync_root, added_at) "
            "VALUES (?,?,?,?)", (ACC, ACC, "/tmp/onedriveui-test/OneDrive",
                                 utcnow_iso())),
        urgent=True)
    try:
        yield wr
    finally:
        wr.stop()
        db.close_all()


def pump(rc, rounds: int = 8) -> None:
    for _ in range(rounds):
        if not rc.pending:
            return
        rc.flush()


# ═════════════════════════════════════════════════════════════════════════════
# parse_stats — absent means empty
# ═════════════════════════════════════════════════════════════════════════════

def test_parse_stats_of_an_empty_body_is_a_valid_empty_corestats():
    """WP-03 acceptance: `parse_stats({})` raises nothing and answers empty
    tuples, because a fresh daemon really does omit every list."""
    stats = rcstats.parse_stats({})
    assert isinstance(stats, CoreStats)
    assert stats.transferring == ()
    assert stats.checking == ()
    assert stats.bytes == 0
    assert stats.eta is None
    assert stats.last_error == ""
    assert stats.fatal_error is False


@pytest.mark.parametrize("body", [None, {}, {"bytes": None}, {"eta": "nonsense"}])
def test_parse_stats_never_raises(body):
    assert isinstance(rcstats.parse_stats(body), CoreStats)


def test_parse_stats_of_the_verbatim_idle_body():
    stats = rcstats.parse_stats(IDLE_STATS)
    assert stats.transferring == () and stats.checking == ()
    assert stats.eta is None, "`eta` is present but null when indeterminate"
    assert stats.elapsed_time == pytest.approx(7.925e-06)


def test_parse_stats_of_a_captured_mid_transfer_body():
    """WP-03 acceptance: a real mid-transfer payload, `srcFs`/`dstFs`/`group`
    included — three fields rclone's own documentation never mentions."""
    stats = rcstats.parse_stats(ACTIVE_STATS)
    assert stats.bytes == 5332998
    assert stats.total_bytes == 50331660
    assert stats.eta == 42
    assert stats.speed == pytest.approx(1060029.116061751)
    assert stats.transfers == 1 and stats.total_transfers == 5
    assert len(stats.transferring) == 2

    first, second = stats.transferring
    assert isinstance(first, TransferInfo)
    assert first.name == "big.bin"
    assert first.group == "bigjob"
    assert first.src_fs == "/tmp/x/t"
    assert first.dst_fs == "/tmp/x/dst3"
    assert first.percentage == 31
    assert first.speed_avg == pytest.approx(536569.9085890673)
    assert first.is_upload is False, "a local destination is not an upload"

    assert second.eta is None, "a per-file eta can be null too"
    assert second.is_upload is True, "dstFs 'onedrive:' means we are uploading"


def test_checking_is_a_list_of_plain_strings():
    """`checking` is strings, `transferring` is objects. No symmetry."""
    stats = rcstats.parse_stats({**IDLE_STATS, "checking": ["f00114.txt", "b.txt"]})
    assert stats.checking == ("f00114.txt", "b.txt")
    assert all(isinstance(name, str) for name in stats.checking)


def test_last_error_only_appears_when_there_are_errors():
    quiet = rcstats.parse_stats(IDLE_STATS)
    assert quiet.last_error == ""
    noisy = rcstats.parse_stats({**IDLE_STATS, "errors": 3,
                                 "lastError": "context canceled",
                                 "retryError": True})
    assert noisy.errors == 3
    assert noisy.last_error == "context canceled"
    assert noisy.retry_error is True


def test_a_short_poll_drops_both_lists():
    """`short: true` omits `transferring` and `checking` as well."""
    body = {k: v for k, v in ACTIVE_STATS.items() if k != "transferring"}
    stats = rcstats.parse_stats(body)
    assert stats.transferring == ()
    assert stats.bytes == 5332998


def test_parse_transfer_of_a_bare_row():
    info = rcstats.parse_transfer({"name": "x"})
    assert info.name == "x" and info.size == 0 and info.eta is None


# ═════════════════════════════════════════════════════════════════════════════
# core/transferred — the fields rclone really sends
# ═════════════════════════════════════════════════════════════════════════════

def test_transferred_events_read_started_at_and_completed_at():
    """The built-in help documents `timestamp` and `jobid`; v1.75.0 sends
    neither. Reading the documented fields would produce empty timestamps and no
    grouping at all."""
    events = rcstats.transferred_events(TRANSFERRED, account_id=ACC)
    assert len(events) == 2
    first = events[0]
    assert first.started_at == "2026-08-30T23:26:05.895290105-04:00"
    assert first.completed_at == "2026-08-30T23:26:05.895521017-04:00"
    assert first.job_group == GROUP
    assert first.rel_path == "a.txt"
    assert first.size == 6 and first.bytes == 6


def test_transferred_events_ignore_the_documented_but_absent_fields():
    """A row that also carried the documented keys must not be read from them."""
    body = {"transferred": [{
        **TRANSFERRED["transferred"][0],
        "timestamp": 1_767_000_000_000, "jobid": 42,
    }]}
    event = rcstats.transferred_events(body, account_id=ACC)[0]
    assert event.started_at == "2026-08-30T23:26:05.895290105-04:00"
    assert "1767000000000" not in (event.started_at + (event.completed_at or ""))


def test_direction_and_verb_come_from_srcfs_and_dstfs():
    events = rcstats.transferred_events(TRANSFERRED, account_id=ACC)
    upload, download = events
    assert upload.direction == "up"
    assert upload.verb is ActivityVerb.UPLOADED
    assert download.direction == "down"
    assert download.verb is ActivityVerb.DOWNLOADED


@pytest.mark.parametrize("src,dst,expected", [
    ("/home/u/OneDrive", "onedrive:", "up"),
    ("onedrive:", "/home/u/OneDrive", "down"),
    ("/a", "/b", "local"),
    ("/home/u/OneDrive", "/mnt/backup", "local"),
])
def test_direction_for(src, dst, expected):
    assert rcstats.direction_for(src, dst) == expected


@pytest.mark.parametrize("what,direction,verb", [
    ("transferring", "up", ActivityVerb.UPLOADED),
    ("transferring", "down", ActivityVerb.DOWNLOADED),
    ("transferring", "local", ActivityVerb.MODIFIED),
    ("deleting", "up", ActivityVerb.DELETED),
    ("moving", "up", ActivityVerb.MOVED),
    ("renaming", "up", ActivityVerb.RENAMED),
])
def test_verb_for(what, direction, verb):
    assert rcstats.verb_for(what, direction) is verb


def test_a_cancelled_transfer_is_cancelled_not_an_error():
    _upload, download = rcstats.transferred_events(TRANSFERRED, account_id=ACC)
    assert download.state is ActivityState.CANCELLED
    assert download.error == "context canceled"
    assert download.bytes == 7008256 and download.size == 8388608


def test_a_real_error_is_classified():
    body = {"transferred": [{
        "error": "quotaLimitReached: insufficient storage", "name": "big.iso",
        "size": 10, "bytes": 0, "checked": False, "what": "transferring",
        "started_at": "2026-08-30T23:26:05Z",
        "completed_at": "2026-08-30T23:26:06Z",
        "group": GROUP, "srcFs": "/home/u/OneDrive", "dstFs": "onedrive:"}]}
    event = rcstats.transferred_events(body, account_id=ACC)[0]
    assert event.state is ActivityState.ERROR
    assert event.error_kind is IssueCode.QUOTA_EXCEEDED


def test_a_benign_line_is_not_an_error():
    body = {"transferred": [{
        "error": "Skipped copy as --dry-run is set", "name": "a.txt",
        "size": 1, "bytes": 1, "checked": False, "what": "transferring",
        "started_at": "2026-08-30T23:26:05Z",
        "completed_at": "2026-08-30T23:26:06Z",
        "group": GROUP, "srcFs": "/x", "dstFs": "onedrive:"}]}
    event = rcstats.transferred_events(body, account_id=ACC)[0]
    assert event.state is ActivityState.DONE
    assert event.error is None


def test_work_rows_are_not_activity():
    """A file that was compared, hashed or listed and not moved is not an event."""
    body = {"transferred": [
        {"name": "a.txt", "what": "checking", "error": "", "checked": True,
         "group": GROUP, "srcFs": "/x", "dstFs": "onedrive:",
         "started_at": "", "completed_at": ""},
        {"name": "b.txt", "what": "hashing", "error": "", "checked": False,
         "group": GROUP, "srcFs": "/x", "dstFs": "onedrive:",
         "started_at": "", "completed_at": ""},
    ]}
    assert rcstats.transferred_events(body, account_id=ACC) == []


def test_transferred_events_of_an_empty_or_malformed_body():
    assert rcstats.transferred_events({}, account_id=ACC) == []
    assert rcstats.transferred_events(None, account_id=ACC) == []
    assert rcstats.transferred_events({"transferred": None}, account_id=ACC) == []
    assert rcstats.transferred_events({"transferred": ["nope"]},
                                      account_id=ACC) == []


def test_a_group_filter_drops_other_groups():
    events = rcstats.transferred_events(TRANSFERRED, account_id=ACC,
                                        group="someone-elses-group")
    assert events == []


# ═════════════════════════════════════════════════════════════════════════════
# Dedupe
# ═════════════════════════════════════════════════════════════════════════════

def test_dedupe_key_is_sha1_of_group_name_completed_at():
    import hashlib

    expected = hashlib.sha1(f"{GROUP}|a.txt|2026-01-01T00:00:00Z".encode(),
                            usedforsecurity=False).hexdigest()
    assert rcstats.dedupe_key(GROUP, "a.txt", "2026-01-01T00:00:00Z") == expected


def test_the_seen_set_stops_the_same_hundred_rows_coming_back():
    """`core/transferred` re-reports its whole 100-row window on every poll."""
    seen: set[str] = set()
    first = rcstats.transferred_events(TRANSFERRED, account_id=ACC, seen=seen)
    assert len(first) == 2 and len(seen) == 2
    again = rcstats.transferred_events(TRANSFERRED, account_id=ACC, seen=seen)
    assert again == []


def test_two_completions_of_the_same_name_are_distinct_events():
    rows = [dict(TRANSFERRED["transferred"][0]),
            {**TRANSFERRED["transferred"][0],
             "completed_at": "2026-08-30T23:30:00.000000000-04:00"}]
    seen: set[str] = set()
    events = rcstats.transferred_events({"transferred": rows},
                                        account_id=ACC, seen=seen)
    assert len(events) == 2
    assert events[0].dedupe_key != events[1].dedupe_key


# ═════════════════════════════════════════════════════════════════════════════
# drain_transferred and reset_group
# ═════════════════════════════════════════════════════════════════════════════

def test_drain_transferred_persists_the_rows(rc, writer):
    rc.set("core/transferred", TRANSFERRED)
    events = rcstats.drain_transferred(rc.endpoint, account_id=ACC,
                                       group=GROUP, writer=writer, sync=True)
    assert len(events) == 2
    assert writer.flush()
    conn = db.open_ro(writer.path)
    rows = list(conn.execute(
        "SELECT rel_path, verb, direction, state, job_group FROM activity "
        "ORDER BY id"))
    assert [row["rel_path"] for row in rows] == ["a.txt", "big.bin"]
    assert rows[0]["verb"] == "uploaded"
    assert rows[0]["direction"] == "up"
    assert rows[1]["state"] == "cancelled"
    assert rows[0]["job_group"] == GROUP


def test_drain_transferred_asks_for_one_group(rc, writer):
    rc.set("core/transferred", {"transferred": []})
    rcstats.drain_transferred(rc.endpoint, account_id=ACC, group=GROUP,
                              writer=writer)
    assert rc.last("core/transferred").params == {"group": GROUP}


def test_drain_transferred_can_skip_persisting(rc, writer):
    rc.set("core/transferred", TRANSFERRED)
    events = rcstats.drain_transferred(rc.endpoint, account_id=ACC,
                                       group=GROUP, persist=False,
                                       writer=writer)
    assert len(events) == 2
    assert writer.flush()
    conn = db.open_ro(writer.path)
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 0


def test_reset_group_drains_before_it_resets(rc, writer):
    """WP-03's safety property. `core/stats-reset` wipes `core/transferred` as
    well as the counters, so the rows have to be in the database first."""
    order: list[str] = []
    rc.set("core/transferred",
           lambda p: order.append("drain") or dict(TRANSFERRED))
    rc.set("core/stats-reset", lambda p: order.append("reset") or {})

    events = rcstats.reset_group(rc.endpoint, GROUP, account_id=ACC,
                                 writer=writer)

    assert order == ["drain", "reset"]
    assert len(events) == 2
    assert writer.flush()
    conn = db.open_ro(writer.path)
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 2


def test_reset_group_scopes_the_reset(rc, writer):
    rc.set("core/transferred", {"transferred": []})
    rcstats.reset_group(rc.endpoint, GROUP, account_id=ACC, writer=writer)
    assert rc.last("core/stats-reset").params == {"group": GROUP}


def test_reset_group_refuses_to_reset_everything(rc):
    """`core/stats-reset` with no group clears every group in the daemon."""
    with pytest.raises(ValueError, match="group"):
        rcstats.reset_group(rc.endpoint, "")
    assert rc.count("core/stats-reset") == 0


def test_reset_group_can_be_told_the_drain_already_happened(rc, writer):
    rcstats.reset_group(rc.endpoint, GROUP, account_id=ACC, drain=False,
                        writer=writer)
    assert rc.count("core/transferred") == 0
    assert rc.count("core/stats-reset") == 1


def test_drain_transferred_propagates_an_rc_error(rc, writer):
    rc.fail("core/transferred", status=500, message="boom")
    with pytest.raises(RcError):
        rcstats.drain_transferred(rc.endpoint, account_id=ACC, writer=writer)


# ═════════════════════════════════════════════════════════════════════════════
# StatsPoller
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def poller(rc, writer, qapp):
    p = rcstats.StatsPoller(rc, account_id=ACC, group=GROUP, writer=writer)
    try:
        yield p
    finally:
        p.stop()


def test_the_poller_never_resets_stats(poller, rc):
    """`core/stats-reset` is never called implicitly, anywhere."""
    rc.set("core/stats", dict(ACTIVE_STATS))
    poller.start()
    pump(rc)
    poller.poll_once()
    pump(rc)
    rc.assert_never("core/stats-reset")
    rc.assert_never("core/stats-delete")


def test_the_poller_asks_for_its_group(poller, rc):
    """A global `core/stats` sums every group over the whole process lifetime,
    so `bytes` would never stop growing."""
    poller.start()
    pump(rc)
    assert rc.last("core/stats").params == {"group": GROUP}


def test_the_poller_publishes_transfers_on_the_bus(poller, rc, bus_spy):
    bus_spy.watch("transfers_updated")
    seen: list[list[TransferInfo]] = []
    poller.transfers_updated.connect(seen.append)
    rc.set("core/stats", dict(ACTIVE_STATS))
    poller.start()
    pump(rc)
    assert len(seen) == 1
    assert [t.name for t in seen[0]] == ["big.bin", "up.bin"]
    assert bus_spy.count("transfers_updated") == 1


def test_transfers_are_only_republished_when_they_change(poller, rc):
    seen: list[list[TransferInfo]] = []
    poller.transfers_updated.connect(seen.append)
    rc.set("core/stats", dict(ACTIVE_STATS))
    poller.start()
    pump(rc)
    poller.poll_once()
    pump(rc)
    assert len(seen) == 1, "an unchanged transfer list is not news"


def test_the_interval_adapts_between_idle_active_and_paused(poller, rc):
    rc.set("core/stats", dict(IDLE_STATS))
    poller.start()
    pump(rc)
    assert poller.interval_ms == TICK_IDLE_MS

    rc.set("core/stats", dict(ACTIVE_STATS))
    poller.poll_once()
    pump(rc)
    assert poller.interval_ms == TICK_ACTIVE_MS

    poller.set_paused(True)
    assert poller.interval_ms == TICK_PAUSED_MS

    poller.set_paused(False)
    rc.set("core/stats", dict(IDLE_STATS))
    poller.poll_once()
    pump(rc)
    assert poller.interval_ms == TICK_IDLE_MS


def test_checking_alone_counts_as_active(poller, rc):
    rc.set("core/stats", {**IDLE_STATS, "checking": ["a.txt"]})
    poller.start()
    pump(rc)
    assert poller.interval_ms == TICK_ACTIVE_MS


def test_an_unfinished_transfer_count_counts_as_active(poller, rc):
    rc.set("core/stats", {**IDLE_STATS, "transfers": 2, "totalTransfers": 9})
    poller.start()
    pump(rc)
    assert poller.interval_ms == TICK_ACTIVE_MS


def test_set_interval_pins_and_unpins(poller, rc):
    rc.set("core/stats", dict(IDLE_STATS))
    poller.start()
    pump(rc)
    assert poller.set_interval(50) == 50
    poller.poll_once()
    pump(rc)
    assert poller.interval_ms == 50, "a pinned interval survives a poll"
    poller.set_interval(None)
    assert poller.interval_ms == TICK_IDLE_MS


def test_the_poller_drains_when_a_transfer_completes(poller, rc, writer):
    rc.set("core/stats", dict(IDLE_STATS))
    rc.set("core/transferred", dict(TRANSFERRED))
    seen: list[list] = []
    poller.activity.connect(seen.append)
    poller.start()
    pump(rc)
    assert rc.count("core/transferred") == 0, "nothing finished yet"

    rc.set("core/stats", {**IDLE_STATS, "transfers": 2})
    poller.poll_once()
    pump(rc)

    assert rc.count("core/transferred") == 1
    assert len(seen) == 1 and len(seen[0]) == 2
    assert writer.flush()
    conn = db.open_ro(writer.path)
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 2


def test_the_poller_does_not_re_report_the_same_completions(poller, rc):
    rc.set("core/stats", {**IDLE_STATS, "transfers": 1})
    rc.set("core/transferred", dict(TRANSFERRED))
    seen: list[list] = []
    poller.activity.connect(seen.append)
    poller.start()
    pump(rc)
    rc.set("core/stats", {**IDLE_STATS, "transfers": 2})
    poller.poll_once()
    pump(rc)
    assert len(seen) == 1, "the second drain saw only rows it had already stored"


def test_draining_can_be_switched_off(rc, writer, qapp):
    p = rcstats.StatsPoller(rc, account_id=ACC, group=GROUP, writer=writer,
                            drain=False)
    try:
        rc.set("core/stats", {**IDLE_STATS, "transfers": 3})
        p.start()
        pump(rc)
    finally:
        p.stop()
    assert rc.count("core/transferred") == 0


def test_drain_group_is_the_registry_cleanup_hook(poller, rc, writer):
    """`JobRegistry(before_cleanup=poller.drain_group)`: one last read before
    `core/stats-delete` takes the group's history with it."""
    rc.set("core/transferred", dict(TRANSFERRED))
    poller.drain_group(GROUP)
    pump(rc)
    assert rc.last("core/transferred").params == {"group": GROUP}
    assert writer.flush()
    conn = db.open_ro(writer.path)
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 2


def test_a_failed_poll_is_reported_and_polling_continues(poller, rc):
    failures: list[object] = []
    poller.failed.connect(failures.append)
    rc.fail("core/stats", status=500, message="boom", times=1)
    poller.start()
    pump(rc)
    assert len(failures) == 1
    assert poller.running is True

    rc.set("core/stats", dict(IDLE_STATS))
    poller.poll_once()
    pump(rc)
    assert poller.last.bytes == 0


def test_stop_discards_a_reply_already_in_flight(poller, rc):
    seen: list[object] = []
    poller.stats_updated.connect(seen.append)
    rc.set("core/stats", dict(ACTIVE_STATS))
    poller.start()                       # the reply is queued, not delivered
    poller.stop()
    pump(rc)
    assert seen == []
    assert poller.running is False


def test_a_second_poll_is_skipped_while_one_is_outstanding(poller, rc):
    poller.start()
    poller.poll_once()
    poller.poll_once()
    assert rc.count("core/stats") == 1
    pump(rc)
    poller.poll_once()
    assert rc.count("core/stats") == 2


def test_set_group_forgets_the_previous_sample(poller, rc):
    rc.set("core/stats", dict(ACTIVE_STATS))
    poller.start()
    pump(rc)
    assert poller.last.bytes == 5332998
    poller.set_group("onedriveui/check/onedrive")
    assert poller.group == "onedriveui/check/onedrive"
    assert poller.last.bytes == 0


def test_the_seen_memory_is_bounded(poller):
    events = [
        rcstats.transferred_events(
            {"transferred": [{**TRANSFERRED["transferred"][0],
                              "completed_at": f"2026-08-30T23:26:{i:02d}Z"}]},
            account_id=ACC, seen=poller._seen)[0]
        for i in range(60)
    ]
    for _ in range(rcstats.SEEN_CAP + 50):
        poller._trim_seen(events)
    assert len(poller._seen) <= rcstats.SEEN_CAP
    assert len(poller._seen_order) <= rcstats.SEEN_CAP


def test_persist_events_submits_every_row(writer):
    events = rcstats.transferred_events(TRANSFERRED, account_id=ACC)
    assert rcstats.persist_events(events, writer=writer, sync=True) == 2
    assert writer.flush()
    conn = db.open_ro(writer.path)
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 2


def test_a_duplicate_row_is_dropped_by_the_unique_index(writer):
    events = rcstats.transferred_events(TRANSFERRED, account_id=ACC)
    rcstats.persist_events(events, writer=writer, sync=True)
    rcstats.persist_events(events, writer=writer, sync=True)
    assert writer.flush()
    conn = db.open_ro(writer.path)
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 2


def test_the_bus_mirror_can_be_switched_off(rc, writer, qapp, bus_spy):
    bus_spy.watch("transfers_updated")
    p = rcstats.StatsPoller(rc, account_id=ACC, group=GROUP, writer=writer,
                            emit_bus=False)
    try:
        rc.set("core/stats", dict(ACTIVE_STATS))
        p.start()
        pump(rc)
    finally:
        p.stop()
    assert bus_spy.count("transfers_updated") == 0
    assert BUS is not None
