"""WP-05 — `sync/facts.py`, the observation pass.

Two things are being proved here.

**Degradation is graceful and honest.** A source that raises, or that the tick
budget never reached, keeps its previous value and says so in `facts.stale`.
Zeroing it instead would tell the ladder that nothing is happening, which is the
one wrong answer.

**Crash recovery is exact.** The last test class discards the collector the way
a `SIGKILL` would, rebuilds one from nothing but what is on disk, and asserts the
reconstructed `SyncState` is identical. This is the most important test in the
repository: everything else the engine does is recoverable by trying again, and
this is the property that says the recovery lands somewhere true.
"""

from __future__ import annotations

import pytest

from onedriveui import paths
from onedriveui.constants import TICK_ACTIVE_MS, TICK_IDLE_MS, TICK_PAUSED_MS
from onedriveui.data import db, repo_sync
from onedriveui.data.writer import DbWriter
from onedriveui.models import (
    AccountInfo,
    BisyncState,
    CoreStats,
    DaemonHealth,
    DiskCacheInfo,
    Facts,
    IssueCode,
    IssueSeverity,
    MountHealth,
    NetworkState,
    PauseReason,
    PowerState,
    QuotaInfo,
    RcEndpoint,
    SyncIssue,
    SyncState,
    TokenHealth,
    TransferInfo,
    utcnow_iso,
)
from onedriveui.sync.facts import SOURCE_NAMES, FactCollector, interval_for
from onedriveui.sync.reducer import LATCH, reduce

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")


# ═════════════════════════════════════════════════════════════════════════════
# Stubs — every service the collector talks to, in about forty lines
# ═════════════════════════════════════════════════════════════════════════════

class StubRcd:
    def __init__(self, health=DaemonHealth.UP, execute_id="exec-1"):
        self._health = health
        self._execute_id = execute_id
        self.calls = 0

    def health(self):
        self.calls += 1
        return self._health

    def endpoint(self):
        return RcEndpoint(kind="rcd", port=17800, execute_id=self._execute_id)

    def stop(self):
        self.stopped = True


class StubMountd:
    def __init__(self, health=MountHealth.UP, serving=True):
        self._health = health
        self._serving = serving

    def health(self, account):
        return self._health

    def is_serving(self, account):
        return self._serving


class StubStats:
    def __init__(self, transferring=0, checking=0, last_error=""):
        self.last = CoreStats(
            transferring=tuple(TransferInfo(name=f"f{i}") for i in range(transferring)),
            checking=tuple(f"c{i}" for i in range(checking)),
            last_error=last_error,
        )


class StubQuota:
    def __init__(self, quota=None, token=TokenHealth.OK):
        self._quota = quota or QuotaInfo(total=1_000_000, used=10_000, free=990_000)
        self._token = token

    def current(self):
        return self._quota

    def token(self):
        return self._token


class StubPause:
    def __init__(self, reason=PauseReason.NONE, until=None,
                 policy=PauseReason.NONE, overridden=False):
        self._reason = reason
        self._until = until
        self._policy = policy
        self._overridden = overridden

    def active(self):
        return self._reason

    def until(self):
        return self._until

    def policy(self):
        return self._policy

    def overridden(self, reason):
        return self._overridden and reason is self._reason


class StubPower:
    def __init__(self, network=NetworkState.ONLINE, power=PowerState.NORMAL):
        self._state = (network, power)

    def state(self):
        return self._state

    def should_throttle(self):
        network, power = self._state
        if network is NetworkState.METERED:
            return (True, PauseReason.METERED)
        if power is PowerState.SAVER:
            return (True, PauseReason.BATTERY)
        return (False, PauseReason.NONE)


class FakeClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def store(_isolate_home, qapp):
    """A live database with the account seeded, and a started writer.

    Deliberately not the `tmp_db` fixture: that one applies `schema.sql` by
    hand, and `DbWriter` runs its own migration on start, so the two collide on
    `schema_meta`. The persisted source is where crash recovery actually lives,
    so these tests use the real writer rather than a hand-built schema.
    """
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


def collector(store, *, clock=None, **kwargs) -> FactCollector:
    """A collector wired to healthy stubs, overridable per test.

    `store` is required, not optional: the persisted source is the one that
    makes crash recovery work, and a test that skipped the database would be
    testing a collector that cannot do the thing it exists for.
    """
    defaults = dict(
        rcd=StubRcd(),
        mountd=StubMountd(),
        stats=StubStats(),
        quota=StubQuota(),
        pause=StubPause(),
        power=StubPower(),
        bisync_state=lambda: BisyncState.DISABLED,
        pin_jobs=lambda: 0,
        vfs_stats=lambda: DiskCacheInfo(),
    )
    defaults.update(kwargs)
    return FactCollector(ACCOUNT, monotonic=clock or FakeClock(), **defaults)


# ═════════════════════════════════════════════════════════════════════════════
# The happy path
# ═════════════════════════════════════════════════════════════════════════════

class TestCollect:

    def test_a_healthy_world_reduces_to_up_to_date(self, qapp, store):
        facts = collector(store).tick()
        assert reduce(facts) is SyncState.UP_TO_DATE

    def test_identity_and_timestamp_are_always_stamped(self, qapp, store):
        facts = collector(store).tick()
        assert facts.account_id == ACCOUNT.id
        assert facts.sampled_at.endswith("Z")

    def test_last_returns_the_previous_observation(self, qapp, store):
        col = collector(store)
        assert col.last().account_id == ACCOUNT.id
        taken = col.tick()
        assert col.last() is taken

    def test_facts_is_frozen(self, qapp, store):
        facts = collector(store).tick()
        with pytest.raises(Exception):
            facts.transfers_active = 9  # type: ignore[misc]

    def test_every_declared_source_contributes(self, qapp, store):
        """The source list and SOURCE_NAMES agree, in order."""
        col = collector(store)
        assert tuple(s.name for s in col._sources()) == SOURCE_NAMES

    def test_no_source_owns_a_field_twice(self, qapp, store):
        """Two sources writing one field would make carry-forward ambiguous."""
        owned: list[str] = []
        for source in collector(store)._sources():
            owned.extend(source.fields)
        assert len(owned) == len(set(owned))

    def test_startup_elapsed_grows(self, qapp, store):
        clock = FakeClock()
        col = collector(store, clock=clock)
        col.tick()
        clock.advance(5.0)
        assert col.tick().startup_elapsed_s == pytest.approx(5.0)


class TestSourcesMapToFields:

    def test_daemon_health_is_read_from_the_supervisor(self, qapp, store):
        facts = collector(store, rcd=StubRcd(health=DaemonHealth.FOREIGN)).tick()
        assert facts.daemon_rcd is DaemonHealth.FOREIGN
        assert reduce(facts) is SyncState.ERROR

    def test_mount_health_comes_from_the_i6_probe(self, qapp, store):
        facts = collector(store, mountd=StubMountd(health=MountHealth.STALE)).tick()
        assert facts.mount is MountHealth.STALE

    def test_a_live_mount_with_a_dead_rc_port_is_not_a_serving_daemon(self, qapp, store):
        """The state in which a restart must refuse: we cannot prove the queue
        is empty, because we cannot ask."""
        facts = collector(store,
                          mountd=StubMountd(health=MountHealth.UP, serving=False)).tick()
        assert facts.mount is MountHealth.UP
        assert facts.daemon_mount is DaemonHealth.DOWN

    def test_transfers_and_checks_come_from_the_stats_poller(self, qapp, store):
        facts = collector(store, stats=StubStats(transferring=3, checking=2)).tick()
        assert (facts.transfers_active, facts.checks_active) == (3, 2)
        assert reduce(facts) is SyncState.SYNCING

    def test_the_upload_queue_comes_from_the_injected_vfs_sample(self, qapp, store):
        info = DiskCacheInfo(uploads_queued=4, uploads_in_progress=1,
                             errored_files=2, out_of_space=True)
        facts = collector(store, vfs_stats=lambda: info).tick()
        assert facts.uploads_queued == 4
        assert facts.uploads_in_progress == 1
        assert facts.errored_files == 2
        assert facts.out_of_space is True

    def test_an_unsampled_vfs_reports_zero_rather_than_guessing(self, qapp, store):
        facts = collector(store, vfs_stats=lambda: None).tick()
        assert facts.uploads_queued == 0

    def test_environment_comes_from_the_power_policy(self, qapp, store):
        facts = collector(store,
                          power=StubPower(network=NetworkState.OFFLINE)).tick()
        assert facts.network is NetworkState.OFFLINE
        assert reduce(facts) is SyncState.OFFLINE

    def test_quota_and_token_come_from_the_quota_service(self, qapp, store):
        full = QuotaInfo(total=1_000, used=1_000, free=0)
        facts = collector(store,
                          quota=StubQuota(quota=full, token=TokenHealth.EXPIRED)).tick()
        assert facts.quota.is_full
        assert facts.token is TokenHealth.EXPIRED

    def test_mount_enabled_is_told_not_polled(self, qapp, store):
        col = collector(store, mountd=StubMountd(health=MountHealth.DOWN))
        assert reduce(col.tick()) is SyncState.MOUNTING
        col.set_mount_enabled(False)
        assert reduce(col.tick()) is SyncState.UP_TO_DATE

    def test_scan_in_progress_is_told_not_polled(self, qapp, store):
        col = collector(store)
        col.set_scan_in_progress(True)
        assert reduce(col.tick()) is SyncState.PROCESSING

    def test_missing_services_degrade_to_down_not_to_a_crash(self, qapp, store):
        """Constructing a collector with nothing wired must still tick."""
        bare = FactCollector(ACCOUNT, monotonic=FakeClock())
        facts = bare.tick()
        assert facts.daemon_rcd is DaemonHealth.DOWN
        assert facts.mount is MountHealth.DOWN


class TestNetworkFailures:

    def test_three_consecutive_failures_read_as_offline(self, qapp, store):
        """A captive portal leaves NetworkMonitor claiming a connection."""
        col = collector(store)
        for _ in range(3):
            col.note_network_result(False)
        assert reduce(col.tick()) is SyncState.OFFLINE

    def test_one_success_clears_the_run(self, qapp, store):
        col = collector(store)
        col.note_network_result(False)
        col.note_network_result(False)
        col.note_network_result(True)
        assert col.tick().consecutive_net_failures == 0


class TestExecuteId:

    def test_first_sighting_is_not_a_change(self, qapp, store):
        """There was nothing to change from."""
        assert collector(store).tick().execute_id_changed is False

    def test_a_new_execute_id_means_the_daemon_restarted(self, qapp, store):
        clock = FakeClock()
        rcd = StubRcd(execute_id="exec-1")
        col = collector(store, rcd=rcd, clock=clock)
        col.tick()
        rcd._execute_id = "exec-2"
        clock.advance(3.0)          # past the daemons source's 2 s cadence
        facts = col.tick()
        assert facts.execute_id == "exec-2"
        assert facts.execute_id_changed is True

    def test_the_same_id_is_not_a_change(self, qapp, store):
        clock = FakeClock()
        col = collector(store, clock=clock)
        col.tick()
        clock.advance(3.0)
        assert col.tick().execute_id_changed is False


# ═════════════════════════════════════════════════════════════════════════════
# Degradation
# ═════════════════════════════════════════════════════════════════════════════

class TestStaleSources:

    def test_a_raising_source_carries_its_last_value_forward(self, qapp, store):
        """The previous value was true a moment ago. A zero is a claim that
        nothing is happening, and that is the one answer that misleads."""
        stats = StubStats(transferring=3)
        col = collector(store, stats=stats)
        assert col.tick().transfers_active == 3

        class Exploding:
            @property
            def last(self):
                raise RuntimeError("core/stats is unreachable")

        col._stats = Exploding()
        facts = col.tick()
        assert facts.transfers_active == 3
        assert "engine" in facts.stale

    def test_one_dead_source_does_not_stop_the_others(self, qapp, store):
        def explode():
            raise OSError("statvfs: ENOTCONN")

        broken = StubMountd()
        broken.health = lambda account: explode()
        facts = collector(store, mountd=broken, stats=StubStats(transferring=2)).tick()
        assert "mount" in facts.stale
        assert facts.transfers_active == 2

    def test_a_healthy_tick_names_nothing_stale(self, qapp, store):
        assert collector(store).tick().stale == frozenset()

    def test_recovery_clears_the_stale_flag(self, qapp, store):
        broken = StubMountd()
        calls = {"n": 0}

        def flaky(account):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient")
            return MountHealth.UP

        broken.health = flaky
        col = collector(store, mountd=broken)
        assert "mount" in col.tick().stale
        assert col.tick().stale == frozenset()

    def test_the_budget_marks_everything_it_did_not_reach(self, qapp, store):
        """A tick that overruns must not queue behind itself.

        The whole point of the budget is that a wedged subsystem makes the UI
        slightly out of date rather than progressively further behind — which
        would be the failure mode precisely when something is wrong.
        """
        clock = FakeClock()

        def slow(account):
            clock.advance(2.0)      # blow the 1 500 ms budget in one source
            return MountHealth.UP

        mountd = StubMountd()
        mountd.health = slow
        facts = collector(store, mountd=mountd, clock=clock).tick()

        assert "mount" not in facts.stale       # it did answer, slowly
        assert set(SOURCE_NAMES[1:]) <= facts.stale

    def test_the_budget_is_the_documented_1500_ms(self):
        assert FactCollector.BUDGET_MS == 1500


class TestCadence:

    def test_the_daemon_probe_is_not_run_every_tick(self, qapp, store):
        """`rcd.health()` costs an `rc/noop` with a one-second timeout."""
        clock = FakeClock()
        rcd = StubRcd()
        col = collector(store, rcd=rcd, clock=clock)
        col.tick()
        clock.advance(0.4)
        col.tick()
        clock.advance(0.4)
        col.tick()
        assert rcd.calls == 1

        clock.advance(2.0)
        col.tick()
        assert rcd.calls == 2

    def test_a_source_that_is_merely_not_due_is_not_stale(self, qapp, store):
        """Not asking on purpose is not the same as failing to get an answer."""
        clock = FakeClock()
        col = collector(store, clock=clock)
        col.tick()
        clock.advance(0.4)
        facts = col.tick()
        assert "daemons" not in facts.stale
        assert facts.daemon_rcd is DaemonHealth.UP


class TestInterval:

    def test_paused_polls_slowly(self):
        assert interval_for(SyncState.PAUSED_MANUAL, Facts()) == TICK_PAUSED_MS

    def test_transferring_polls_fast(self):
        assert interval_for(SyncState.SYNCING,
                            Facts(transfers_active=1)) == TICK_ACTIVE_MS

    def test_hydrating_polls_fast_too(self):
        assert interval_for(SyncState.SYNCING,
                            Facts(pin_jobs_active=1)) == TICK_ACTIVE_MS

    def test_idle_polls_at_the_default(self):
        assert interval_for(SyncState.UP_TO_DATE, Facts()) == TICK_IDLE_MS

    def test_the_collector_adopts_the_interval(self, qapp, store):
        col = collector(store, stats=StubStats(transferring=2))
        col.start()
        try:
            assert col.interval_ms == TICK_ACTIVE_MS
        finally:
            col.stop()


class TestPauseExpiry:
    """Expiry is resolved here, because the reducer has no clock."""

    def test_a_live_deadline_is_kept(self, qapp, store, frozen_clock):
        pause = StubPause(reason=PauseReason.MANUAL, until="2026-08-31T14:00:00Z")
        facts = collector(store, pause=pause).tick()
        assert facts.pause.reason is PauseReason.MANUAL
        assert reduce(facts) is SyncState.PAUSED_MANUAL

    def test_an_elapsed_deadline_is_dropped_before_the_ladder_sees_it(
            self, qapp, store, frozen_clock):
        pause = StubPause(reason=PauseReason.MANUAL, until="2026-08-31T11:00:00Z")
        facts = collector(store, pause=pause).tick()
        assert facts.pause.reason is PauseReason.NONE
        assert reduce(facts) is SyncState.UP_TO_DATE

    def test_until_i_resume_never_expires(self, qapp, store, frozen_clock):
        """No deadline is a deliberate choice, not a missing value."""
        pause = StubPause(reason=PauseReason.MANUAL, until=None)
        facts = collector(store, pause=pause).tick()
        assert reduce(facts) is SyncState.PAUSED_MANUAL

    def test_sync_anyway_is_carried_into_the_facts(self, qapp, store):
        pause = StubPause(reason=PauseReason.METERED, overridden=True,
                          policy=PauseReason.METERED)
        facts = collector(store, pause=pause).tick()
        assert facts.pause.overridden is True
        assert reduce(facts) is SyncState.UP_TO_DATE

    def test_without_a_pause_manager_the_power_policy_drives_it(self, qapp, store):
        facts = collector(store, pause=None,
                          power=StubPower(network=NetworkState.METERED)).tick()
        assert facts.policy_pause is PauseReason.METERED


# ═════════════════════════════════════════════════════════════════════════════
# The persisted sources
# ═════════════════════════════════════════════════════════════════════════════

class TestPersisted:

    def test_issue_counts_reach_the_ladder(self, qapp, store):
        repo_sync.raise_issue(SyncIssue(
            account_id=ACCOUNT.id, code=IssueCode.UPLOAD_FAILED,
            severity=IssueSeverity.ERROR, rel_path="a.txt", title="failed"),
            writer=store)
        facts = collector(store).tick()
        assert facts.issues_error == 1
        assert reduce(facts) is SyncState.WARNING

    def test_a_blocking_issue_is_an_error(self, qapp, store):
        repo_sync.raise_issue(SyncIssue(
            account_id=ACCOUNT.id, code=IssueCode.MALWARE_DETECTED,
            severity=IssueSeverity.BLOCKING, rel_path="x", title="blocked"),
            writer=store)
        assert reduce(collector(store).tick()) is SyncState.ERROR

    def test_latches_are_read_every_tick(self, qapp, store):
        repo_sync.set_latch(ACCOUNT.id, LATCH.QUOTA_EXCEEDED, writer=store)
        facts = collector(store).tick()
        assert LATCH.QUOTA_EXCEEDED in facts.latches
        assert reduce(facts) is SyncState.PAUSED_QUOTA

    def test_clearing_a_latch_clears_the_state(self, qapp, store):
        col = collector(store)
        repo_sync.set_latch(ACCOUNT.id, LATCH.QUOTA_EXCEEDED, writer=store)
        assert reduce(col.tick()) is SyncState.PAUSED_QUOTA
        repo_sync.clear_latch(ACCOUNT.id, LATCH.QUOTA_EXCEEDED, writer=store)
        assert reduce(col.tick()) is SyncState.UP_TO_DATE

    def test_the_orphan_cache_latch_becomes_an_info_notice(self, qapp, store):
        """Worth saying, not worth colouring the tray icon for."""
        repo_sync.set_latch(ACCOUNT.id, LATCH.ORPHAN_CACHE, "48.2 GB", writer=store)
        facts = collector(store).tick()
        assert facts.info_notice == "Old cached files are using 48.2 GB"
        assert reduce(facts) is SyncState.INFO_NOTICE

    def test_pending_decisions_ask_for_attention(self, qapp, store):
        from onedriveui.models import Decision, DecisionKind
        repo_sync.create_decision(Decision(
            account_id=ACCOUNT.id, kind=DecisionKind.MASS_DELETE,
            payload={"count": 250}, created_at=utcnow_iso()), writer=store)
        assert reduce(collector(store).tick()) is SyncState.NEEDS_ATTENTION


# ═════════════════════════════════════════════════════════════════════════════
# Crash recovery — the most important test in the repository
# ═════════════════════════════════════════════════════════════════════════════

class TestCrashRecovery:
    """Discard the collector the way a SIGKILL would, and rebuild from disk.

    The engine's whole design rests on this: nothing is remembered that is not
    re-observable, so the state after a kill is the state before it. If this
    fails, a crash during an upload can leave the client cheerfully reporting
    "Your files are synced" over an unresolved hazard, and no amount of careful
    behaviour anywhere else compensates for that.
    """

    def _world(self, store):
        """A world with three hazards, all of which live on disk."""
        repo_sync.set_latch(ACCOUNT.id, LATCH.NEEDS_RESYNC, writer=store)
        repo_sync.set_latch(ACCOUNT.id, LATCH.ORPHAN_CACHE, "12.0 GB", writer=store)
        repo_sync.raise_issue(SyncIssue(
            account_id=ACCOUNT.id, code=IssueCode.NAME_INVALID,
            severity=IssueSeverity.ERROR, rel_path="bad:name.txt",
            title="Invalid name"), writer=store)

    def test_the_state_survives_byte_for_byte(self, qapp, store):
        self._world(store)
        before = collector(store).tick()

        # kill -9: the collector, its carry-forward, its debouncer and every
        # counter are gone. Nothing is handed to the replacement.
        reborn = collector(store).tick()

        assert reduce(reborn) is reduce(before)
        assert reduce(before) is SyncState.NEEDS_ATTENTION

    def test_every_field_but_the_timestamps_is_identical(self, qapp, store):
        self._world(store)
        before = collector(store).tick()
        reborn = collector(store).tick()

        volatile = {"sampled_at", "startup_elapsed_s"}
        for field in Facts.__slots__:
            if field in volatile:
                continue
            assert getattr(reborn, field) == getattr(before, field), field

    def test_a_hazard_is_visible_on_the_very_first_tick_after_a_restart(
            self, qapp, store):
        """Not after three ticks of hysteresis — on the first observation.

        A restart that showed a green cloud for a second before admitting the
        drive is full would be exactly the moment a user reaches for the wrong
        conclusion about what happened while they were away.
        """
        repo_sync.set_latch(ACCOUNT.id, LATCH.QUOTA_EXCEEDED, writer=store)
        assert reduce(collector(store).tick()) is SyncState.PAUSED_QUOTA

    def test_recovery_does_not_depend_on_the_daemons_coming_back(self, qapp, store):
        """The hazard is on disk; a still-dead daemon cannot erase it."""
        repo_sync.set_latch(ACCOUNT.id, LATCH.BISYNC_CRITICAL, writer=store)
        facts = collector(store, rcd=StubRcd(health=DaemonHealth.DOWN)).tick()
        assert LATCH.BISYNC_CRITICAL in facts.latches

    def test_only_the_net_failure_counter_is_forgotten(self, qapp, store):
        """And it is bounded: three ticks of a dead network re-establish it.

        This is the one documented exception to "nothing is remembered", so it
        is pinned down rather than left as a comment.
        """
        col = collector(store)
        for _ in range(5):
            col.note_network_result(False)
        assert col.tick().consecutive_net_failures == 5
        assert collector(store).tick().consecutive_net_failures == 0


class TestAssembly:

    def test_an_unknown_field_is_a_loud_error(self, qapp, store):
        """Dropping it silently would hide the bug until someone wondered why a
        rung never fired."""
        col = collector(store)
        col._bisync_state = None
        sources = list(col._sources())
        col._source_cache = tuple(sources[:1]) + (
            type(sources[0])("bogus", ("nope",), 0.0, lambda: {"nope": 1}),)
        with pytest.raises(TypeError, match="unknown fields"):
            col.tick()


class TestLifecycle:

    def test_start_ticks_immediately(self, qapp, store):
        """Two seconds of no tray icon reads as "it did not launch"."""
        col = collector(store)
        col.start()
        try:
            assert col.last().sampled_at
            assert col.running is True
        finally:
            col.stop()

    def test_start_is_idempotent(self, qapp, store):
        col = collector(store)
        col.start()
        first = col.last()
        col.start()
        try:
            assert col.last() is first
        finally:
            col.stop()

    def test_stop_leaves_the_last_observation_readable(self, qapp, store):
        col = collector(store)
        col.start()
        col.stop()
        assert col.running is False
        assert col.last().account_id == ACCOUNT.id

    def test_the_bus_carries_every_observation(self, qapp, store, bus_spy):
        bus_spy.watch("facts_updated")
        facts = collector(store).tick()
        assert bus_spy.events == [("facts_updated", (facts,))]
