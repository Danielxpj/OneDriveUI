"""WP-05 — `sync/supervisor.py`, the orchestrator.

The supervisor is the only object in the application that changes the world, so
the tests that matter here are about *refusing* to:

* `restart_mount()` refuses while an upload is in flight, and also when it could
  not find out — invariant I3, where "we could not ask" is not evidence the
  answer is zero.
* `request_resync()` refuses without an answered decision — invariant I15.
* `reset_client(keep_files=False)` refuses outright: there is no combination of
  arguments here that deletes a user's files.

Plus the coverage properties that keep the dispatch honest: every
`RecoveryAction` has a handler, every `EFFECT` the reducer can emit has one, and
an action the table does not know about raises rather than quietly doing nothing.
"""

from __future__ import annotations

import pytest

from onedriveui import paths
from onedriveui.constants import MOUNT_RESTART_MAX_PER_HOUR
from onedriveui.data import db, repo_sync
from onedriveui.data.writer import DbWriter
from onedriveui.errors import SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    Decision,
    DecisionKind,
    DiskCacheInfo,
    IssueCode,
    IssueSeverity,
    MountHealth,
    NotificationId,
    PauseReason,
    QuotaInfo,
    RecoveryAction,
    SyncIssue,
    SyncState,
    TrayIcon,
    utcnow_iso,
)
from onedriveui.sync.reducer import EFFECT, LATCH
from onedriveui.sync.supervisor import MOUNT_HEALTHY_CLEAR_S, Supervisor

from tests.test_facts import (  # the collector's stubs are the supervisor's too
    FakeClock,
    StubMountd,
    StubPause,
    StubPower,
    StubQuota,
    StubRcd,
    StubStats,
)

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")


# ═════════════════════════════════════════════════════════════════════════════
# Stubs
# ═════════════════════════════════════════════════════════════════════════════

class RecordingMountd(StubMountd):
    """A mount controller that records restarts instead of performing them."""

    def __init__(self, health=MountHealth.UP, uploads=0, restarts=0):
        super().__init__(health=health)
        self._uploads = uploads
        self._restarts = restarts
        self.restarted: list[str] = []
        self.unmounted = 0

    def uploads_in_progress(self, account):
        return self._uploads

    def restarts_this_hour(self, account):
        return self._restarts

    def restart(self, account, reason):
        self.restarted.append(reason)

    def unmount(self, account, *, lazy=True):
        self.unmounted += 1

    def endpoint(self, account):
        return None


class RecordingNotifier:
    def __init__(self):
        self.toasts: list[tuple[NotificationId, dict]] = []

    def toast(self, nid, *, account_id="", **fmt):
        self.toasts.append((nid, dict(fmt)))
        return 1


class RecordingIssues:
    def __init__(self):
        self.executed: list[tuple[RecoveryAction, object, dict]] = []

    def execute(self, action, issue, **kw):
        self.executed.append((action, issue, dict(kw)))
        return True


class RecordingStats(StubStats):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.drained = 0

    def drain_now(self):
        self.drained += 1


class RecordingPause(StubPause):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls: list[tuple[str, object]] = []

    def pause(self, reason, hours=None):
        self.calls.append(("pause", (reason, hours)))

    def resume(self, reason=None):
        self.calls.append(("resume", reason))

    def enforce(self, endpoint):
        self.calls.append(("enforce", endpoint))
        return 3


class RecordingIpc:
    def __init__(self):
        self.invalidated: list[list[str]] = []

    def broadcast_invalidate(self, paths_):
        self.invalidated.append(list(paths_))


@pytest.fixture
def store(_isolate_home, qapp):
    """A live database with the account seeded, and a started writer."""
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


def supervisor(store, *, clock=None, **kwargs) -> Supervisor:
    defaults = dict(
        rcd=StubRcd(),
        mountd=RecordingMountd(),
        stats=RecordingStats(),
        pause=RecordingPause(),
        quota=StubQuota(),
        writer=store,
    )
    defaults.update(kwargs)
    return Supervisor(ACCOUNT, monotonic=clock or FakeClock(), **defaults)


def drive(sup: Supervisor, ticks: int = 4) -> None:
    """Tick until the debouncer has published whatever it is going to."""
    for _ in range(ticks):
        sup._on_collected(sup.collector.tick())


# ═════════════════════════════════════════════════════════════════════════════
# The tick loop
# ═════════════════════════════════════════════════════════════════════════════

class TestTickLoop:

    def test_start_runs_the_collector(self, qapp, store):
        sup = supervisor(store)
        sup.start()
        try:
            assert sup.running is True
            assert sup.collector.running is True
        finally:
            sup.stop()

    def test_stop_leaves_the_state_readable(self, qapp, store):
        """The tray still has to paint something while shutting down."""
        sup = supervisor(store)
        sup.start()
        sup.stop()
        assert sup.running is False
        assert sup.state() in set(SyncState)
        assert sup.snapshot().headline

    def test_start_and_stop_are_idempotent(self, qapp, store):
        sup = supervisor(store)
        sup.start()
        sup.start()
        sup.stop()
        sup.stop()
        assert sup.running is False

    def test_the_state_is_debounced_not_raw(self, qapp, store):
        """A healthy world does not go green on the first tick."""
        sup = supervisor(store)
        sup._on_collected(sup.collector.tick())
        assert sup.state() is SyncState.INITIALIZING
        drive(sup, 3)
        assert sup.state() is SyncState.UP_TO_DATE

    def test_a_hazard_is_published_immediately(self, qapp, store):
        repo_sync.set_latch(ACCOUNT.id, LATCH.BISYNC_CRITICAL, writer=store)
        sup = supervisor(store)
        sup._on_collected(sup.collector.tick())
        assert sup.state() is SyncState.ERROR

    def test_the_bus_carries_the_transition(self, qapp, store, bus_spy):
        bus_spy.watch("state_changed")
        sup = supervisor(store)
        drive(sup)
        assert bus_spy.count("state_changed") >= 1
        old, new, _facts = bus_spy.last("state_changed")
        assert new is SyncState.UP_TO_DATE
        assert old is not new

    def test_no_signal_when_nothing_changed(self, qapp, store, bus_spy):
        sup = supervisor(store)
        drive(sup, 4)
        bus_spy.watch("state_changed")
        drive(sup, 3)
        assert bus_spy.count("state_changed") == 0


class TestSnapshot:

    def test_the_snapshot_renders_every_surface_from_one_state(self, qapp, store):
        sup = supervisor(store)
        drive(sup)
        snap = sup.snapshot()
        assert snap.state is SyncState.UP_TO_DATE
        assert snap.headline == "Your files are synced"
        assert snap.tray is TrayIcon.SYNCED
        assert snap.tooltip.startswith(snap.headline)
        assert snap.changed_at.endswith("Z")

    def test_a_full_quota_banners_the_right_code(self, qapp, store):
        full = QuotaInfo(total=1_000, used=1_000, free=0)
        sup = supervisor(store, quota=StubQuota(quota=full))
        drive(sup)
        assert sup.state() is SyncState.PAUSED_QUOTA
        assert sup.snapshot().banner_code is IssueCode.QUOTA_EXCEEDED

    def test_a_full_local_disk_banners_a_different_code(self, qapp, store):
        """Cloud-full and disk-full share a rung but are not the same problem,
        and the fix is different in each case."""
        sup = supervisor(store,
                         vfs_stats=lambda: DiskCacheInfo(out_of_space=True))
        drive(sup)
        assert sup.snapshot().banner_code is IssueCode.DISK_FULL

    def test_errors_banner_beneath_a_busy_headline(self, qapp, store):
        """Windows shows "Syncing N files" with a sync-issues banner below.

        The headline must stay on the transfer and the error must still be
        visible; showing one instead of the other loses information either way.
        """
        repo_sync.raise_issue(SyncIssue(
            account_id=ACCOUNT.id, code=IssueCode.NAME_INVALID,
            severity=IssueSeverity.ERROR, rel_path="bad:name", title="bad"),
            writer=store)
        sup = supervisor(store, stats=RecordingStats(transferring=2))
        drive(sup)
        assert sup.state() is SyncState.SYNCING
        assert sup.snapshot().headline == "Syncing 2 files"
        assert sup.snapshot().banner_code is IssueCode.NAME_INVALID

    def test_a_quiet_state_has_no_banner(self, qapp, store):
        sup = supervisor(store)
        drive(sup)
        assert sup.snapshot().banner_code is None


# ═════════════════════════════════════════════════════════════════════════════
# Effects
# ═════════════════════════════════════════════════════════════════════════════

class TestEffects:

    def test_every_effect_the_reducer_can_emit_has_a_handler(self):
        """A named effect with nowhere to land is a pause that never defers."""
        declared = {v for k, v in vars(EFFECT).items() if not k.startswith("_")}
        assert declared == set(Supervisor._EFFECTS)
        for method in Supervisor._EFFECTS.values():
            assert callable(getattr(Supervisor, method))

    def test_an_unknown_effect_is_loud(self, qapp, store):
        sup = supervisor(store)
        with pytest.raises(KeyError):
            sup._run_effect("effect:invented", sup.collector.tick())

    def test_a_failing_effect_does_not_stop_the_others(self, qapp, store):
        """One broken toast must not prevent the queue being deferred."""
        class Exploding:
            def toast(self, *a, **kw):
                raise RuntimeError("the notification daemon is not running")

        pause = RecordingPause()
        mountd = RecordingMountd()
        mountd.endpoint = lambda account: object()
        sup = supervisor(store, notifier=Exploding(), pause=pause, mountd=mountd)
        facts = sup.collector.tick()
        sup._run_effect(EFFECT.TOAST_PAUSED, facts)      # raises internally
        sup._run_effect(EFFECT.PAUSE_ENFORCE, facts)     # must still happen
        assert [name for name, _arg in pause.calls] == ["enforce"]

    def test_entering_a_pause_toasts_and_defers(self, qapp, store):
        notifier = RecordingNotifier()
        pause = RecordingPause(reason=PauseReason.METERED, policy=PauseReason.METERED)
        mountd = RecordingMountd()
        mountd.endpoint = lambda account: object()
        sup = supervisor(store, notifier=notifier, pause=pause, mountd=mountd,
                         power=StubPower())
        drive(sup)
        assert sup.state() is SyncState.PAUSED_METERED
        assert notifier.toasts[0][0] is NotificationId.SYNC_PAUSED_METERED
        assert any(name == "enforce" for name, _ in pause.calls)

    def test_pausing_never_unmounts(self, qapp, store):
        """Cached files must stay readable while paused, as on Windows."""
        mountd = RecordingMountd()
        sup = supervisor(store, mountd=mountd,
                         pause=RecordingPause(reason=PauseReason.MANUAL))
        drive(sup)
        assert sup.state() is SyncState.PAUSED_MANUAL
        assert mountd.unmounted == 0

    def test_auth_required_never_unmounts_either(self, qapp, store):
        from onedriveui.models import TokenHealth

        mountd = RecordingMountd()
        sup = supervisor(store, mountd=mountd,
                         quota=StubQuota(token=TokenHealth.EXPIRED))
        drive(sup)
        assert sup.state() is SyncState.AUTH_REQUIRED
        assert mountd.unmounted == 0

    def test_a_stale_mount_is_unmounted_then_restarted(self, qapp, store):
        """`fusermount3 -uz` is the only thing that clears an ENOTCONN corpse."""
        mountd = RecordingMountd(health=MountHealth.STALE)
        sup = supervisor(store, mountd=mountd, notifier=RecordingNotifier())
        drive(sup)
        assert sup.state() is SyncState.ERROR
        assert mountd.unmounted == 1
        assert mountd.restarted == ["the mount went stale"]

    def test_finishing_a_batch_drains_before_it_resets(self, qapp, store):
        """`core/stats-reset` wipes `core/transferred` along with the counters."""
        order: list[str] = []
        stats = RecordingStats(transferring=2)
        stats.drain_now = lambda: order.append("drain")
        sup = supervisor(store, stats=stats,
                         jobs_runner={"stats_reset": lambda: order.append("reset")})
        drive(sup)
        assert sup.state() is SyncState.SYNCING
        stats.last = RecordingStats().last
        drive(sup)
        assert sup.state() is SyncState.UP_TO_DATE
        assert order == ["drain", "reset"]

    def test_every_change_invalidates_nautilus(self, qapp, store):
        ipc = RecordingIpc()
        sup = supervisor(store, ipc=ipc)
        drive(sup)
        assert ipc.invalidated == [[ACCOUNT.sync_root]]

    def test_a_missing_service_is_a_no_op_not_a_crash(self, qapp, store):
        """M1 runs the whole loop with no UI, no notifier and no pinner."""
        sup = Supervisor(ACCOUNT, monotonic=FakeClock(), writer=store)
        drive(sup)
        assert sup.state() in set(SyncState)


# ═════════════════════════════════════════════════════════════════════════════
# Latches
# ═════════════════════════════════════════════════════════════════════════════

class TestLatches:

    def test_a_full_drive_latches(self, qapp, store):
        full = QuotaInfo(total=1_000, used=1_000, free=0)
        sup = supervisor(store, quota=StubQuota(quota=full))
        drive(sup)
        assert LATCH.QUOTA_EXCEEDED in repo_sync.latches(ACCOUNT.id)

    def test_space_appearing_again_clears_it(self, qapp, store):
        quota = StubQuota(quota=QuotaInfo(total=1_000, used=1_000, free=0))
        sup = supervisor(store, quota=quota)
        drive(sup)
        assert LATCH.QUOTA_EXCEEDED in repo_sync.latches(ACCOUNT.id)
        quota._quota = QuotaInfo(total=1_000, used=10, free=990)
        drive(sup)
        assert LATCH.QUOTA_EXCEEDED not in repo_sync.latches(ACCOUNT.id)

    def test_an_unanswered_about_does_not_clear_it(self, qapp, store):
        """A quota of zero means `about` has not answered, not that space
        appeared. Clearing on that would unlatch a genuinely full drive every
        time the network hiccups."""
        quota = StubQuota(quota=QuotaInfo(total=1_000, used=1_000, free=0))
        sup = supervisor(store, quota=quota)
        drive(sup)
        quota._quota = QuotaInfo()          # total == 0: nothing was learned
        drive(sup)
        assert LATCH.QUOTA_EXCEEDED in repo_sync.latches(ACCOUNT.id)

    def test_needs_resync_is_never_cleared_by_observation(self, qapp, store):
        """Only a successful `--resync` clears it — a human said yes to that.

        Clearing it because bisync happens to look idle would silently discard
        the reason the user was asked in the first place.
        """
        repo_sync.set_latch(ACCOUNT.id, LATCH.NEEDS_RESYNC, writer=store)
        sup = supervisor(store)
        drive(sup, 10)
        assert LATCH.NEEDS_RESYNC in repo_sync.latches(ACCOUNT.id)

    def test_the_mount_failed_latch_needs_a_full_minute_of_health(self, qapp, store):
        """A mount that comes up and dies again inside a minute has not recovered."""
        repo_sync.set_latch(ACCOUNT.id, LATCH.MOUNT_FAILED, writer=store)
        clock = FakeClock()
        sup = supervisor(store, clock=clock)
        drive(sup, 2)
        assert LATCH.MOUNT_FAILED in repo_sync.latches(ACCOUNT.id)
        clock.advance(MOUNT_HEALTHY_CLEAR_S + 1)
        drive(sup, 2)
        assert LATCH.MOUNT_FAILED not in repo_sync.latches(ACCOUNT.id)

    def test_a_mount_that_drops_restarts_the_clock(self, qapp, store):
        repo_sync.set_latch(ACCOUNT.id, LATCH.MOUNT_FAILED, writer=store)
        clock = FakeClock()
        mountd = RecordingMountd()
        sup = supervisor(store, clock=clock, mountd=mountd)
        drive(sup, 2)
        clock.advance(MOUNT_HEALTHY_CLEAR_S - 5)
        mountd._health = MountHealth.DOWN
        drive(sup, 2)
        mountd._health = MountHealth.UP
        clock.advance(10)
        drive(sup, 2)
        assert LATCH.MOUNT_FAILED in repo_sync.latches(ACCOUNT.id)


# ═════════════════════════════════════════════════════════════════════════════
# Refusals — invariants I3 and I15
# ═════════════════════════════════════════════════════════════════════════════

class TestRestartMount:

    def test_refuses_while_an_upload_is_in_flight(self, qapp, store, caplog):
        """Invariant I3. The file exists in the VFS cache and nowhere else."""
        mountd = RecordingMountd(health=MountHealth.UP, uploads=1)
        supervisor(store, mountd=mountd).restart_mount("a test asked")
        assert mountd.restarted == []
        assert "I3" in caplog.text

    def test_refuses_when_it_could_not_ask(self, qapp, store):
        """`uploads_in_progress()` returns -1 when `vfs/stats` is unreachable.

        "We could not ask" is not evidence that the answer is zero, and this is
        the exact case where treating it as zero destroys an upload.
        """
        mountd = RecordingMountd(health=MountHealth.UP, uploads=-1)
        supervisor(store, mountd=mountd).restart_mount("a test asked")
        assert mountd.restarted == []

    def test_restarts_a_healthy_idle_mount(self, qapp, store):
        mountd = RecordingMountd(health=MountHealth.UP, uploads=0)
        supervisor(store, mountd=mountd).restart_mount("a test asked")
        assert mountd.restarted == ["a test asked"]

    def test_a_stale_mount_is_restarted_even_mid_upload(self, qapp, store):
        """Its uploads are already lost; only a restart brings the account back."""
        mountd = RecordingMountd(health=MountHealth.STALE, uploads=3)
        supervisor(store, mountd=mountd).restart_mount("stale")
        assert mountd.restarted == ["stale"]

    def test_an_exhausted_ladder_latches_instead_of_looping(self, qapp, store):
        mountd = RecordingMountd(restarts=MOUNT_RESTART_MAX_PER_HOUR)
        supervisor(store, mountd=mountd).restart_mount("again")
        assert mountd.restarted == []
        assert LATCH.MOUNT_FAILED in repo_sync.latches(ACCOUNT.id)

    def test_a_deliberate_restart_suppresses_the_spinner(self, qapp, store):
        """We broke the mount on purpose; MOUNTING would read as a fault."""
        clock = FakeClock()
        mountd = RecordingMountd()
        sup = supervisor(store, clock=clock, mountd=mountd)
        drive(sup)
        sup.restart_mount("a test asked")
        mountd._health = MountHealth.DOWN
        drive(sup, 4)
        assert sup.state() is SyncState.UP_TO_DATE

    def test_no_mount_controller_is_a_logged_no_op(self, qapp, store):
        sup = Supervisor(ACCOUNT, monotonic=FakeClock(), writer=store)
        sup.restart_mount("nothing to restart")


class TestRequestResync:

    def _decision(self, store, *, answer: str | None, kind=DecisionKind.RESYNC_CONFIRM):
        decision_id = repo_sync.create_decision(Decision(
            account_id=ACCOUNT.id, kind=kind, payload={},
            created_at=utcnow_iso()), writer=store)
        if answer is not None:
            repo_sync.answer_decision(decision_id, answer, writer=store)
        return decision_id

    def test_refuses_without_a_decision_row(self, qapp, store):
        """Invariant I15. `--resync` only copies, so an unattended one
        resurrects every deleted file and duplicates every rename."""
        with pytest.raises(SafetyRefusal) as excinfo:
            supervisor(store).request_resync(decision_id=99_999)
        assert excinfo.value.invariant == "I15"

    def test_refuses_an_unanswered_decision(self, qapp, store):
        decision_id = self._decision(store, answer=None)
        with pytest.raises(SafetyRefusal):
            supervisor(store).request_resync(decision_id=decision_id)

    def test_refuses_a_declined_decision(self, qapp, store):
        decision_id = self._decision(store, answer="no")
        with pytest.raises(SafetyRefusal):
            supervisor(store).request_resync(decision_id=decision_id)

    def test_an_approved_one_is_accepted(self, qapp, store):
        """The gate opens for a real approval — and only for one.

        This used to assert that an injected bisync runner ran. There is no
        bisync engine any more (f58e05a deleted the whole Topology-B stack), so
        `request_resync` validates the decision and logs that it has nothing to
        run. The surviving property is the one worth pinning: an approved
        decision must pass the I15 gate that `no`, `expired` and unanswered are
        all refused by. Asserting the absence of the refusal keeps the three
        negative cases honest — without this, deleting the whole gate would
        still leave them green.
        """
        decision_id = self._decision(store, answer="yes")
        supervisor(store).request_resync(decision_id=decision_id)

    def test_refuses_the_wrong_kind_of_decision(self, qapp, store):
        """An approval for a mass delete is not an approval for a resync."""
        decision_id = self._decision(store, answer="yes",
                                     kind=DecisionKind.MASS_DELETE)
        with pytest.raises(SafetyRefusal) as excinfo:
            supervisor(store).request_resync(decision_id=decision_id)
        assert excinfo.value.invariant == "I15"

    def test_do_resync_without_an_id_is_refused(self, qapp, store):
        with pytest.raises(SafetyRefusal):
            supervisor(store).do(RecoveryAction.RESYNC)


class TestResetClient:

    def test_never_deletes_the_users_files(self, qapp, store):
        """There is no combination of arguments here that touches sync_root."""
        with pytest.raises(SafetyRefusal):
            supervisor(store).reset_client(keep_files=False)

    def test_clears_our_own_state(self, qapp, store):
        repo_sync.set_latch(ACCOUNT.id, LATCH.QUOTA_EXCEEDED, writer=store)
        mountd = RecordingMountd()
        sup = supervisor(store, mountd=mountd)
        sup.start()
        sup.reset_client()
        assert sup.running is False
        assert mountd.unmounted == 1
        assert repo_sync.latches(ACCOUNT.id) == frozenset()


# ═════════════════════════════════════════════════════════════════════════════
# do() — the single entry point
# ═════════════════════════════════════════════════════════════════════════════

class TestDo:

    def test_every_recovery_action_has_a_handler(self):
        """A missing entry is a button that does nothing, which the user cannot
        tell apart from a slow one."""
        assert set(Supervisor._ACTIONS) == set(RecoveryAction)
        assert len(RecoveryAction) == 20   # PIN and UNPIN joined in f58e05a
        for method in Supervisor._ACTIONS.values():
            assert callable(getattr(Supervisor, method))

    def test_an_unhandled_action_raises(self, qapp, store):
        with pytest.raises(KeyError):
            supervisor(store).do("not-an-action")  # type: ignore[arg-type]

    @pytest.mark.parametrize("action", [
        RecoveryAction.RETRY, RecoveryAction.RENAME, RecoveryAction.SKIP,
        RecoveryAction.KEEP_BOTH, RecoveryAction.KEEP_LOCAL,
        RecoveryAction.KEEP_CLOUD, RecoveryAction.FORCE_DELETE,
        RecoveryAction.RESTORE_FROM_BACKUP, RecoveryAction.UNLOCK_BISYNC,
        RecoveryAction.STOP_SYNCING_ITEM,
    ])
    def test_issue_scoped_actions_reach_the_issue_engine(self, qapp, store, action):
        issues = RecordingIssues()
        issue = SyncIssue(account_id=ACCOUNT.id, code=IssueCode.UPLOAD_FAILED,
                          severity=IssueSeverity.ERROR, title="x")
        supervisor(store, issues=issues).do(action, issue=issue)
        assert issues.executed == [(action, issue, {})]

    def test_free_up_space_reaches_the_pinner(self, qapp, store):
        class Pinner:
            def __init__(self):
                self.freed: list[str] = []
                self.all = 0

            def free_up_space(self, rel_path):
                self.freed.append(rel_path)
                return 1

            def free_up_all(self):
                self.all += 1
                return 1

        pinner = Pinner()
        sup = supervisor(store, pinner=pinner)
        sup.do(RecoveryAction.FREE_UP_SPACE, rel_path="Photos/a.jpg")
        sup.do(RecoveryAction.FREE_UP_SPACE)
        assert pinner.freed == ["Photos/a.jpg"]
        assert pinner.all == 1

    def test_restart_mount_goes_through_do(self, qapp, store):
        mountd = RecordingMountd()
        supervisor(store, mountd=mountd).do(RecoveryAction.RESTART_MOUNT,
                                            reason="from a toast")
        assert mountd.restarted == ["from a toast"]

    def test_sign_in_starts_the_auth_flow(self, qapp, store):
        """SIGN_IN re-authorises the account's own remote, in place.

        The stub used to take no arguments at all, so it could not see either
        half of the real call. Both are asserted now:

        * the remote, because `AuthFlow.start()` has no default for it;
        * `update=True`, because SIGN_IN is always a *re*-authentication.
          `config/create` — what `update=False` selects — deletes the whole
          `[remote]` section before rewriting it on rclone v1.75.0, taking every
          backend key the client wrote under I1 with it. `config/update` edits
          in place.
        """
        class Auth:
            def __init__(self):
                self.calls: list[tuple[str, bool]] = []

            def start(self, remote, *, update=False, **kw):
                self.calls.append((remote, update))

        auth = Auth()
        supervisor(store, auth=auth).do(RecoveryAction.SIGN_IN)
        assert auth.calls == [(ACCOUNT.remote, True)]

    def test_web_actions_open_a_url(self, qapp, store, monkeypatch):
        opened: list[str] = []
        from onedriveui.platform import desktop

        monkeypatch.setattr(desktop, "open_url", lambda url: opened.append(url) or True)
        sup = supervisor(store)
        sup.do(RecoveryAction.GET_MORE_STORAGE)
        sup.do(RecoveryAction.OPEN_WEB, url="https://example.invalid/x")
        assert opened == ["https://www.microsoft.com/microsoft-365/onedrive/"
                          "compare-onedrive-plans",
                          "https://example.invalid/x"]

    def test_a_directory_is_opened_not_revealed(self, qapp, store, tmp_path,
                                                monkeypatch):
        """"Open your OneDrive folder" must land the user INSIDE it.

        `show_in_folder()` opens the containing folder with the item selected,
        which for the sync root means the user's home directory with a folder
        highlighted — not the folder they asked for.
        """
        from onedriveui.platform import desktop

        opened: list[str] = []
        monkeypatch.setattr(desktop, "open_path",
                            lambda p, **kw: opened.append(str(p)) or True)
        monkeypatch.setattr(desktop, "show_in_folder",
                            lambda *t, **kw: pytest.fail("revealed a directory"))
        supervisor(store).do(RecoveryAction.SHOW_IN_FOLDER, path=str(tmp_path))
        assert opened == [str(tmp_path)]

    def test_a_file_is_revealed_not_opened(self, qapp, store, tmp_path,
                                           monkeypatch):
        """The other half: "show me where this is" must not launch the file."""
        from onedriveui.platform import desktop

        target = tmp_path / "a.txt"
        target.write_text("x")
        seen: list[tuple] = []
        monkeypatch.setattr(desktop, "show_in_folder",
                            lambda *t, **kw: seen.append(t) or True)
        monkeypatch.setattr(desktop, "open_path",
                            lambda *a, **kw: pytest.fail("opened a file"))
        supervisor(store).do(RecoveryAction.SHOW_IN_FOLDER, path=str(target))
        assert seen == [(str(target),)]

    def test_a_missing_service_records_rather_than_crashes(self, qapp, store):
        """Every action must survive being invoked before its service exists."""
        sup = Supervisor(ACCOUNT, monotonic=FakeClock(), writer=store)
        for action in RecoveryAction:
            if action is RecoveryAction.RESYNC:
                continue        # covered by its own refusal test
            sup.do(action, rel_path="x", url="https://example.invalid/", path="/tmp")

    def test_every_action_is_recorded(self, qapp, store):
        """The diagnostics bundle answers "what did the user ask for?"."""
        sup = supervisor(store, issues=RecordingIssues())
        sup.do(RecoveryAction.RETRY, issue=None)
        sup.do(RecoveryAction.SKIP, issue=None)
        assert [action for _at, action, _kw in sup.history] == ["retry", "skip"]

    def test_the_history_is_bounded(self, qapp, store):
        """A long session must not grow a list without limit."""
        sup = supervisor(store, issues=RecordingIssues())
        for _ in range(250):
            sup.do(RecoveryAction.RETRY, issue=None)
        assert len(sup.history) == 200


class TestPauseAndResume:

    def test_pause_reaches_the_manager_and_the_bus(self, qapp, store, bus_spy):
        bus_spy.watch("pause_changed")
        pause = RecordingPause()
        supervisor(store, pause=pause).request_pause(PauseReason.MANUAL, 2)
        assert pause.calls == [("pause", (PauseReason.MANUAL, 2))]
        assert bus_spy.last("pause_changed")[0] is PauseReason.MANUAL

    def test_until_i_resume_passes_no_hours(self, qapp, store):
        pause = RecordingPause()
        supervisor(store, pause=pause).request_pause(PauseReason.MANUAL, None)
        assert pause.calls == [("pause", (PauseReason.MANUAL, None))]

    def test_resume_reaches_the_manager(self, qapp, store, bus_spy):
        bus_spy.watch("pause_changed")
        pause = RecordingPause()
        supervisor(store, pause=pause).request_resume()
        assert ("resume", None) in pause.calls
        assert bus_spy.last("pause_changed")[0] is PauseReason.NONE


# ═════════════════════════════════════════════════════════════════════════════
# Scheduled maintenance
# ═════════════════════════════════════════════════════════════════════════════

class TestSchedule:

    def test_the_schedule_is_published(self):
        assert set(Supervisor.SCHEDULE) >= {
            "tick_idle_ms", "tick_active_ms", "quota_s", "cache_scan_s",
            "verify_s", "token_keepalive_s", "prune_s"}

    def test_nothing_fires_in_the_first_second(self, qapp, store):
        """Start-up must not launch a weekly check and a full cache scan at once."""
        ran: list[str] = []
        sup = supervisor(store, jobs_runner={
            name: (lambda n=name: ran.append(n))
            for name in ("quota", "cache_scan", "verify", "token_keepalive", "prune")})
        sup.start()
        try:
            drive(sup, 3)
            assert ran == []
        finally:
            sup.stop()

    def test_a_due_job_runs_once(self, qapp, store):
        ran: list[str] = []
        clock = FakeClock()
        sup = supervisor(store, clock=clock,
                         jobs_runner={"quota": lambda: ran.append("quota")})
        sup.start()
        try:
            clock.advance(Supervisor.SCHEDULE["quota_s"] + 1)
            drive(sup, 3)
            assert ran == ["quota"]
        finally:
            sup.stop()

    def test_a_failing_job_never_stops_the_loop(self, qapp, store):
        def explode():
            raise RuntimeError("rclone check could not start")

        clock = FakeClock()
        sup = supervisor(store, clock=clock, jobs_runner={"verify": explode})
        sup.start()
        try:
            clock.advance(Supervisor.SCHEDULE["verify_s"] + 1)
            drive(sup, 3)
            assert sup.running is True
        finally:
            sup.stop()

    def test_expiring_a_decision_deletes_nothing(self, qapp, store, frozen_clock):
        """Microsoft's seven-day rule: an unanswered "delete 4 000 files?" that
        ages out resolves to NOT deleting them. Silence is never consent."""
        decision_id = repo_sync.create_decision(Decision(
            account_id=ACCOUNT.id, kind=DecisionKind.MASS_DELETE,
            payload={"count": 4_000}, created_at=utcnow_iso(),
            expires_at="2026-08-30T00:00:00Z"), writer=store)
        supervisor(store)._job_prune()
        row = db.open_ro().execute(
            "SELECT answer FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        assert row is not None
        assert row["answer"] != "yes"
