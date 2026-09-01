"""WP-06 — `sync/pause.py`.

Pause against a FUSE mount is not "stop the jobs" — the write-back queue uploads
on its own timer, outside job control entirely. These tests pin down what pause
actually is:

* every queued item's expiry is pushed past the horizon, **every tick**, because
  a file saved during the pause joins the queue with its own five-second expiry;
* the mount is never touched, so cached files stay readable;
* a manual pause survives a restart, and an automatic one has no deadline at all.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from onedriveui import paths
from onedriveui.data import db, repo_files
from onedriveui.data.writer import DbWriter
from onedriveui.errors import RcError
from onedriveui.models import AccountInfo, PauseReason, QueueItem, RcEndpoint, utcnow_iso
from onedriveui.rc import vfs
from onedriveui.sync.pause import DEFER_HORIZON_S, PAUSE_DURATIONS, PauseManager

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")
ENDPOINT = RcEndpoint(kind="mount", port=17801, account_id=ACCOUNT.id)


class Clock:
    """A wall clock that only moves when a test says so."""

    def __init__(self, start: str = "2026-08-31T12:00:00Z") -> None:
        self.now = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))

    def __call__(self) -> _dt.datetime:
        return self.now

    def advance(self, **kw) -> _dt.datetime:
        self.now += _dt.timedelta(**kw)
        return self.now


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


def manager(store, *, clock=None, config=None) -> PauseManager:
    return PauseManager(ACCOUNT, writer=store, now=clock or Clock(),
                        config_get=config or (lambda key, default=None: default))


# ═════════════════════════════════════════════════════════════════════════════
# Manual pause
# ═════════════════════════════════════════════════════════════════════════════

class TestManualPause:

    def test_the_durations_are_microsofts(self):
        assert [hours for hours, _label in PAUSE_DURATIONS] == [2, 8, 24, None]
        assert PAUSE_DURATIONS[-1][1] == "Until I resume"

    def test_pausing_sets_a_deadline(self, qapp, store):
        clock = Clock()
        pause = manager(store, clock=clock)
        pause.pause(PauseReason.MANUAL, 2)
        assert pause.active() is PauseReason.MANUAL
        assert pause.until() == clock.now + _dt.timedelta(hours=2)

    def test_until_i_resume_has_no_deadline(self, qapp, store):
        """Not an omission — a deliberate choice the user made."""
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, None)
        assert pause.active() is PauseReason.MANUAL
        assert pause.until() is None

    def test_the_deadline_ends_it(self, qapp, store):
        clock = Clock()
        pause = manager(store, clock=clock)
        pause.pause(PauseReason.MANUAL, 2)
        clock.advance(hours=2, seconds=1)
        assert pause.active() is PauseReason.NONE

    def test_resume_ends_it_early(self, qapp, store):
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 24)
        pause.resume()
        assert pause.active() is PauseReason.NONE
        assert pause.until() is None

    def test_the_bus_carries_every_change(self, qapp, store, bus_spy):
        bus_spy.watch("pause_changed")
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 8)
        pause.resume()
        assert [args[0] for args in bus_spy.of("pause_changed")] == [
            PauseReason.MANUAL, PauseReason.NONE]


class TestPersistence:
    """A pause the user set must not evaporate because we restarted."""

    def test_a_manual_pause_survives_a_restart(self, qapp, store):
        clock = Clock()
        manager(store, clock=clock).pause(PauseReason.MANUAL, 8)

        reborn = manager(store, clock=clock)
        assert reborn.active() is PauseReason.MANUAL
        assert reborn.until() == clock.now + _dt.timedelta(hours=8)

    def test_the_remaining_time_is_correct_after_a_restart(self, qapp, store):
        clock = Clock()
        manager(store, clock=clock).pause(PauseReason.MANUAL, 8)
        clock.advance(hours=3)
        reborn = manager(store, clock=clock)
        remaining = reborn.until() - clock.now
        assert remaining == _dt.timedelta(hours=5)

    def test_a_pause_that_expired_while_we_were_down_resumes(self, qapp, store):
        clock = Clock()
        manager(store, clock=clock).pause(PauseReason.MANUAL, 2)
        clock.advance(hours=3)
        assert manager(store, clock=clock).active() is PauseReason.NONE

    def test_until_i_resume_survives_indefinitely(self, qapp, store):
        clock = Clock()
        manager(store, clock=clock).pause(PauseReason.MANUAL, None)
        clock.advance(days=30)
        assert manager(store, clock=clock).active() is PauseReason.MANUAL

    def test_the_deadline_is_not_in_the_config_file(self, qapp, store):
        """A restored or hand-edited config must not resurrect or lengthen a
        pause the user already ended."""
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 2)
        assert repo_files.kv_get("pause.until", None, account_id=ACCOUNT.id)
        config = paths.config_file()
        assert not config.exists() or "pause.until" not in config.read_text()

    def test_an_unknown_persisted_reason_is_ignored(self, qapp, store):
        """A row from a future schema must not wedge the client in a pause it
        cannot describe."""
        repo_files.kv_set("pause.reason", "hibernating",
                          account_id=ACCOUNT.id, writer=store)
        assert manager(store).active() is PauseReason.NONE


# ═════════════════════════════════════════════════════════════════════════════
# Automatic pauses
# ═════════════════════════════════════════════════════════════════════════════

class TestPolicyPause:

    def test_metered_pauses_when_the_toggle_is_on(self, qapp, store):
        pause = manager(store, config=lambda key, default=None: True)
        assert pause.policy_pause(metered=True) is PauseReason.METERED

    def test_the_toggle_turns_it_off(self, qapp, store):
        pause = manager(store, config=lambda key, default=None: False)
        assert pause.policy_pause(metered=True) is PauseReason.NONE

    def test_metered_outranks_battery(self, qapp, store):
        """Matching the ladder, where PAUSED_METERED sits above PAUSED_BATTERY."""
        pause = manager(store, config=lambda key, default=None: True)
        assert pause.policy_pause(metered=True, battery=True) is PauseReason.METERED

    def test_a_full_quota_outranks_both(self, qapp, store):
        pause = manager(store, config=lambda key, default=None: True)
        assert pause.policy_pause(metered=True, quota_full=True) is PauseReason.QUOTA

    def test_an_automatic_pause_gets_no_deadline(self, qapp, store):
        """A metered pause that expired after two hours would resume a large
        upload over the connection the user was avoiding."""
        pause = manager(store)
        pause.pause(PauseReason.METERED, 2)
        assert pause.active() is PauseReason.METERED
        assert pause.until() is None

    def test_it_lasts_exactly_as_long_as_the_condition(self, qapp, store):
        clock = Clock()
        pause = manager(store, clock=clock)
        pause.pause(PauseReason.METERED)
        clock.advance(days=7)
        assert pause.active() is PauseReason.METERED
        pause.resume(PauseReason.METERED)
        assert pause.active() is PauseReason.NONE


class TestSyncAnyway:

    def test_it_overrides_one_reason(self, qapp, store):
        config = lambda key, default=None: True          # noqa: E731
        pause = manager(store, config=config)
        pause.pause(PauseReason.METERED)
        pause.sync_anyway(PauseReason.METERED)
        assert pause.active() is PauseReason.NONE
        assert pause.policy_pause(metered=True) is PauseReason.NONE

    def test_it_does_not_disable_the_policy(self, qapp, store):
        """The user said "not right now", not "never ask again"."""
        clock = Clock()
        config = lambda key, default=None: True          # noqa: E731
        pause = manager(store, clock=clock, config=config)
        pause.sync_anyway(PauseReason.METERED, hours=8)
        clock.advance(hours=9)
        assert pause.policy_pause(metered=True) is PauseReason.METERED

    def test_overriding_metered_says_nothing_about_battery(self, qapp, store):
        config = lambda key, default=None: True          # noqa: E731
        pause = manager(store, config=config)
        pause.sync_anyway(PauseReason.METERED)
        assert pause.policy_pause(battery=True) is PauseReason.BATTERY

    def test_it_survives_a_restart(self, qapp, store):
        config = lambda key, default=None: True          # noqa: E731
        clock = Clock()
        manager(store, clock=clock, config=config).sync_anyway(PauseReason.METERED)
        reborn = manager(store, clock=clock, config=config)
        assert reborn.policy_pause(metered=True) is PauseReason.NONE

    def test_it_refuses_to_override_a_manual_pause(self, qapp, store):
        """"Sync Anyway" belongs to the automatic pauses. Applying it to a
        manual one would let a toast undo an explicit choice."""
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 8)
        pause.sync_anyway(PauseReason.MANUAL)
        assert pause.active() is PauseReason.MANUAL


# ═════════════════════════════════════════════════════════════════════════════
# Enforcement — what pause actually does
# ═════════════════════════════════════════════════════════════════════════════

class TestEnforce:

    def test_it_defers_the_whole_queue(self, qapp, store, monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr(vfs, "defer_uploads",
                            lambda ep, seconds, **kw: calls.append((ep, seconds)) or 4)
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 2)
        assert pause.enforce(ENDPOINT) == 4
        assert calls == [(ENDPOINT, DEFER_HORIZON_S)]

    def test_it_does_nothing_when_not_paused(self, qapp, store, monkeypatch):
        monkeypatch.setattr(vfs, "defer_uploads",
                            lambda *a, **kw: pytest.fail("deferred while running"))
        assert manager(store).enforce(ENDPOINT) == 0

    def test_it_runs_every_tick_not_once(self, qapp, store, monkeypatch):
        """A file saved during the pause joins the queue with its own
        five-second expiry and would upload if nothing re-deferred it."""
        ticks: list[float] = []
        monkeypatch.setattr(vfs, "defer_uploads",
                            lambda ep, seconds, **kw: ticks.append(seconds) or 1)
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 2)
        for _ in range(3):
            pause.enforce(ENDPOINT)
        assert ticks == [DEFER_HORIZON_S] * 3

    def test_the_horizon_is_bounded_so_a_crash_cannot_freeze_the_queue(self):
        """The expiry is pushed a fixed distance ahead each tick, not to the
        deadline: if this process dies mid-pause the queue drains within the
        horizon instead of staying frozen for the twenty-three hours the user
        asked for and then being forgotten."""
        assert 0 < DEFER_HORIZON_S <= 3600

    def test_an_unreachable_daemon_is_not_a_failure(self, qapp, store, monkeypatch):
        """A daemon that cannot be reached is not uploading either."""
        def explode(*a, **kw):
            raise RcError("vfs/queue-set-expiry", 500,
                          {"error": "connection refused"})

        monkeypatch.setattr(vfs, "defer_uploads", explode)
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 2)
        assert pause.enforce(ENDPOINT) == 0

    def test_no_endpoint_is_a_no_op(self, qapp, store):
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 2)
        assert pause.enforce(None) == 0

    def test_resume_flushes_the_queue(self, qapp, store, monkeypatch):
        forced: list[int] = []
        monkeypatch.setattr(vfs, "queue", lambda ep, **kw: [
            QueueItem(name="a", id=1), QueueItem(name="b", id=2)])
        monkeypatch.setattr(vfs, "force_upload_now",
                            lambda ep, item_id, **kw: forced.append(item_id))
        assert manager(store).release(ENDPOINT) == 2
        assert forced == [1, 2]

    def test_an_item_already_uploading_is_left_alone(self, qapp, store, monkeypatch):
        """rclone ignores an expiry change on an item that has started, so
        counting it would claim something that did not happen."""
        monkeypatch.setattr(vfs, "queue", lambda ep, **kw: [
            QueueItem(name="a", id=1, uploading=True), QueueItem(name="b", id=2)])
        monkeypatch.setattr(vfs, "force_upload_now", lambda ep, item_id, **kw: None)
        assert manager(store).release(ENDPOINT) == 1


class TestNeverUnmounts:

    def test_pausing_touches_no_mount(self, qapp, store, monkeypatch):
        """Unmounting would stop uploads by taking every cached file away.

        Windows' pause does not do that, and a user who paused to save data
        would find their offline files gone.
        """
        from onedriveui.rc import mountd

        monkeypatch.setattr(mountd, "fusermount_unmount",
                            lambda *a, **kw: pytest.fail("pause unmounted"))
        pause = manager(store)
        pause.pause(PauseReason.MANUAL, 24)
        pause.enforce(None)
        pause.resume()
