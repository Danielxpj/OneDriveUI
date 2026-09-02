"""The orchestrator, and the only thing in the application that changes the world.

Everything else in the engine observes or decides. :class:`Supervisor` is what
*acts*: it drives the tick loop, publishes the debounced state, executes the
effects the reducer declared, keeps the latches honest, runs the scheduled
maintenance, and — through :meth:`Supervisor.do` — performs every user action.

**`do()` is the single entry point, and that is a design decision, not a
convenience.** UI code never calls a service directly. One consequence is
obvious (there is exactly one place to look when asking "what happened when the
user clicked Retry?"); the two that matter more are less obvious:

* Every action passes the same guards. A "Free up space" invoked from the
  Nautilus context menu, the Activity Center and a toast button all go through
  one code path, so a safety check added once is added everywhere. The
  alternative — three call sites, one of which forgets invariant I5 — is how
  files get lost.
* Every action is *recordable*. The dispatch table is data, so the diagnostics
  bundle can list what was asked for and when, and a test can assert that all
  eighteen :class:`~onedriveui.models.RecoveryAction` members are handled rather
  than silently ignored.

Every collaborator is injected and every one is optional. A missing service
makes its actions logged no-ops rather than crashes, which is what lets the
engine be brought up in stages — ``onedriveui --state`` (milestone M1) runs the
whole tick loop with no UI, no notifier and no pinner at all.

Threading (ARCHITECTURE §7): this object lives on the GUI thread and everything
it calls from here is either cheap or asynchronous. The blocking work — a cache
scan, ``rclone check``, draining ``core/transferred`` — is handed to injected
callables that the application runs on an ``IOPool`` worker.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.constants import (
    CACHE_SCAN_INTERVAL_S,
    MOUNT_RESTART_MAX_PER_HOUR,
    QUOTA_TTL_S,
    TICK_ACTIVE_MS,
    TICK_IDLE_MS,
    TOKEN_KEEPALIVE_S,
    VERIFY_INTERVAL_S,
    WEB_GET_MORE_STORAGE,
)
from onedriveui.data import db, repo_sync
from onedriveui.errors import SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    DecisionKind,
    Facts,
    IssueCode,
    IssueSeverity,
    MountHealth,
    NotificationId,
    PauseReason,
    RecoveryAction,
    SyncIssue,
    SyncSnapshot,
    SyncState,
    utcnow_iso,
)
from onedriveui.sync.decisions import ANSWER_YES
from onedriveui.sync.facts import FactCollector
from onedriveui.sync.reducer import (
    EFFECT,
    LATCH,
    Debouncer,
    progress_pct,
    reduce,
    status_text,
    tooltip,
    transition_effects,
    tray_for,
)

log = logging.getLogger(__name__)

__all__ = ["Supervisor", "MOUNT_HEALTHY_CLEAR_S", "PRUNE_INTERVAL_S"]

#: How long the mount must stay healthy before the `mount_failed` latch is
#: cleared. A mount that comes up and dies again inside a minute has not
#: recovered; clearing the latch on the first healthy tick would hide a
#: flapping mount behind a green cloud that blinks.
MOUNT_HEALTHY_CLEAR_S: Final = 60.0

#: Housekeeping cadence: expire stale decisions, purge due trash, trim the
#: activity and issue tables to their row caps.
PRUNE_INTERVAL_S: Final = 3600


#: Which pause each paused state represents. The automatic ones are derived
#: from the environment every tick and are never recorded in `PauseManager`, so
#: this is how the enforcement learns what is actually in force.
_PAUSE_REASON: dict[SyncState, PauseReason] = {
    SyncState.PAUSED_MANUAL: PauseReason.MANUAL,
    SyncState.PAUSED_METERED: PauseReason.METERED,
    SyncState.PAUSED_BATTERY: PauseReason.BATTERY,
    SyncState.PAUSED_QUOTA: PauseReason.QUOTA,
}

#: The states in which the upload queue must be held back on every tick.
_PAUSED_STATES: frozenset[SyncState] = frozenset({
    SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
    SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA,
})


class Supervisor(QObject):
    """The tick loop, the effect executor, and `do()`.

    Args:
        account: The account this supervisor drives. One per account.
        collector: The :class:`~onedriveui.sync.facts.FactCollector`. Built from
            the other arguments when not supplied. It owns the polling cadence;
            this object listens rather than running a second timer, so there is
            no way for the two to drift apart or to double-tick.
        rcd: The control-plane daemon supervisor.
        mountd: The mount controller. Owns the restart ladder itself; this
            object owns the *policy* of when to ask it.
        stats: The ``core/stats`` poller, for the drain-then-reset sequence.
        pause: WP-06's pause manager.
        quota: WP-06's quota service.
        power: A :class:`~onedriveui.platform.power.PowerPolicy`.
        vfs_stats: Returns the latest ``DiskCacheInfo``, or ``None``.
        bisync_state: Returns the current ``BisyncState``.
        pin_jobs: Returns how many hydration jobs are running.
        issues: WP-07's issue engine, which executes the issue-scoped recovery
            actions.
        pinner: WP-08's pinner, for the space actions.
        notifier: WP-10's notifier. Absent means no toasts, not a crash.
        ipc: WP-10's IPC server, for invalidating Nautilus's emblem cache.
        jobs: The job registry, so a daemon restart can invalidate every handle.
        auth: The OAuth flow, for the sign-in action.
        bisync: WP-04's bisync runner, for an approved ``--resync``.
        writer: The database writer. Latch writes are urgent and go through it.
        jobs_runner: ``{name: callable}`` for the scheduled maintenance —
            ``cache_scan``, ``verify``, ``token_keepalive``, ``prune``,
            ``quota`` and ``stats_reset``. Each runs on an ``IOPool`` worker; a
            missing one is a logged no-op.
        monotonic: The clock, injected so a test can move time without sleeping.
        parent: Qt parent.

    Signals:
        state_changed: ``(old, new, facts)``, mirroring
            :data:`~onedriveui.bus.BUS.state_changed` for direct listeners.
    """

    state_changed = Signal(object, object, Facts)

    #: The scheduled work, in seconds unless the key says ``_ms``. Public
    #: because the About pane lists it and a support thread asks about it.
    SCHEDULE: Final[dict[str, int]] = {
        "tick_idle_ms": TICK_IDLE_MS,
        "tick_active_ms": TICK_ACTIVE_MS,
        "quota_s": QUOTA_TTL_S,
        "cache_scan_s": CACHE_SCAN_INTERVAL_S,
        "verify_s": VERIFY_INTERVAL_S,
        "token_keepalive_s": TOKEN_KEEPALIVE_S,
        "prune_s": PRUNE_INTERVAL_S,
    }

    def __init__(
        self,
        account: AccountInfo,
        *,
        collector: FactCollector | None = None,
        rcd: Any = None,
        mountd: Any = None,
        stats: Any = None,
        pause: Any = None,
        quota: Any = None,
        power: Any = None,
        vfs_stats: Callable[[], Any] | None = None,
        bisync_state: Callable[[], Any] | None = None,
        pin_jobs: Callable[[], int] | None = None,
        issues: Any = None,
        pinner: Any = None,
        notifier: Any = None,
        ipc: Any = None,
        jobs: Any = None,
        auth: Any = None,
        bisync: Any = None,
        writer: Any = None,
        jobs_runner: Mapping[str, Callable[[], Any]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._rcd = rcd
        self._mountd = mountd
        self._stats = stats
        self._pause = pause
        self._quota = quota
        self._power = power
        self._issues = issues
        self._pinner = pinner
        self._notifier = notifier
        self._ipc = ipc
        self._jobs = jobs
        self._auth = auth
        self._bisync = bisync
        self._writer = writer
        self._runners: dict[str, Callable[[], Any]] = dict(jobs_runner or {})
        self._monotonic = monotonic

        self._collector = collector or FactCollector(
            account, rcd=rcd, mountd=mountd, stats=stats, pause=pause,
            quota=quota, power=power, vfs_stats=vfs_stats,
            bisync_state=bisync_state, pin_jobs=pin_jobs,
            monotonic=monotonic, parent=self)
        self._debouncer = Debouncer()
        self._state = SyncState.INITIALIZING
        self._snapshot = SyncSnapshot(state=self._state, facts=self._collector.last())
        self._running = False
        self._mount_healthy_since: float | None = None
        #: Actions currently executing, so `do()` cannot recurse into itself.
        self._in_flight: set[RecoveryAction] = set()
        #: The health facts the issue engine last saw; see `_on_collected`.
        self._issue_signature: tuple[Any, ...] | None = None
        # Every scheduled job is treated as having just run. Seeded here rather
        # than only in `start()` so the property holds however the loop is
        # driven — `onedriveui --state` ticks once without ever calling
        # `start()`, and a weekly `rclone check` firing during a one-shot status
        # query would be an expensive surprise.
        self._last_job_run: dict[str, float] = {
            key: monotonic() for key in self.SCHEDULE}
        #: Every action ever dispatched, newest last, for the diagnostics
        #: bundle. Bounded so a long-running session cannot grow without limit.
        self.history: list[tuple[str, str, dict[str, Any]]] = []

    # ═════════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═════════════════════════════════════════════════════════════════════════

    def start(self) -> None:
        """Wire the tick loop up and take the first observation. Idempotent."""
        if self._running:
            return
        self._running = True
        self._collector.collected.connect(self._on_collected)
        # Re-seeded on every start, so a supervisor stopped for an hour and
        # started again does not fire a weekly check and a full cache scan
        # simultaneously in its first second.
        now = self._monotonic()
        for key in self.SCHEDULE:
            self._last_job_run[key] = now
        self._collector.start()

    def stop(self) -> None:
        """Stop ticking. Idempotent. Leaves :meth:`state` and :meth:`snapshot`
        readable, because the tray still has to paint something while shutting
        down."""
        if not self._running:
            return
        self._running = False
        self._collector.stop()
        try:
            self._collector.collected.disconnect(self._on_collected)
        except (RuntimeError, TypeError):
            pass

    @property
    def running(self) -> bool:
        return self._running

    @property
    def collector(self) -> FactCollector:
        return self._collector

    # ═════════════════════════════════════════════════════════════════════════
    # Reads
    # ═════════════════════════════════════════════════════════════════════════

    def state(self) -> SyncState:
        """The debounced state currently published."""
        return self._state

    def snapshot(self) -> SyncSnapshot:
        """Everything the UI renders, from one source."""
        return self._snapshot

    # ═════════════════════════════════════════════════════════════════════════
    # The tick
    # ═════════════════════════════════════════════════════════════════════════

    def _on_collected(self, facts: Facts) -> None:
        """One observation has arrived. Decide, publish, act.

        Order matters. The latches are reconciled *before* the ladder runs, so a
        hazard observed this tick is visible on this tick rather than the next
        one; the effects run *after* the state is published, so the UI has
        already repainted by the time a toast appears.
        """
        self._reconcile_latches(facts)

        # Turn this observation into durable issues, and close the ones the
        # world has already fixed. Both methods existed, were tested, and had
        # no caller: the `issues` table stayed empty for account-wide problems,
        # so the tray's "Sync problems (N)" item never appeared, the flyout's
        # issue list was always empty, and nothing ever auto-resolved. The
        # ladder counts issues; this is what there was to count.
        if self._issues is not None:
            # Only when one of the facts they actually read has changed.
            # `reconcile()` walks a dozen issue codes and `raise_issue()` and
            # `resolve()` are each a blocking `DbWriter.submit_sync()` round
            # trip — running the pair unconditionally put ten of those on the
            # GUI thread every two seconds, for ever, to discover nothing had
            # changed. In the steady state this signature never moves and the
            # whole block is skipped.
            signature = (facts.mount, facts.token, facts.daemon_rcd,
                         facts.network, facts.out_of_space, facts.bisync,
                         facts.quota.total, facts.quota.is_full,
                         facts.issues_error, facts.issues_blocking)
            if signature != self._issue_signature:
                self._issue_signature = signature
                try:
                    self._issues.ingest_health(facts)
                    self._issues.reconcile(facts)
                except Exception:  # noqa: BLE001 - bookkeeping never stops a tick
                    log.warning("could not update the issue list", exc_info=True)

        raw = reduce(facts)
        new = self._debouncer.apply(raw, self._monotonic())
        old, self._state = self._state, new
        self._snapshot = self._build_snapshot(new, facts)

        if new is not old:
            log.info("state %s -> %s (raw %s, stale=%s)",
                     old.value, new.value, raw.value, sorted(facts.stale))
            BUS.state_changed.emit(old, new, facts)
            self.state_changed.emit(old, new, facts)
            for effect in transition_effects(old, new, facts):
                self._run_effect(effect, facts)

        # Every tick while paused, not just on the edge into it.
        # `PauseManager.enforce` says so in its own docstring — "Every tick, not
        # once, because --vfs-write-back keeps adding items: a file saved during
        # the pause joins the queue with its own five-second expiry and would
        # upload immediately if nothing re-deferred it." It was reachable only
        # from a transition effect, so a pause held whatever was already queued
        # and let everything saved afterwards straight through.
        if new in _PAUSED_STATES:
            self._enforce_pause()

        self._run_due_jobs()

    def _build_snapshot(self, state: SyncState, facts: Facts) -> SyncSnapshot:
        headline, subtext = status_text(state, facts)
        return SyncSnapshot(
            state=state,
            facts=facts,
            headline=headline,
            subtext=subtext,
            tooltip=tooltip(state, facts),
            tray=tray_for(state, self.account),
            progress_pct=progress_pct(state, facts),
            banner_code=self._banner_for(state, facts),
            changed_at=utcnow_iso(),
        )

    def _banner_for(self, state: SyncState, facts: Facts) -> IssueCode | None:
        """The code for the banner that renders *below* the status line.

        This is what makes rungs 13–15 work. While files are transferring the
        headline says so, and the unresolved errors appear underneath rather
        than replacing it — which is what Windows does, and the only way to show
        both facts at once without one hiding the other.
        """
        if state is SyncState.PAUSED_QUOTA:
            return IssueCode.DISK_FULL if facts.out_of_space else IssueCode.QUOTA_EXCEEDED
        if state in (SyncState.SYNCING, SyncState.PROCESSING) and facts.issues_error:
            return self._top_issue_code()
        if state is SyncState.INFO_NOTICE:
            return IssueCode.ORPHANED_CACHE
        return None

    def _top_issue_code(self) -> IssueCode | None:
        """The most severe open issue's code, for the banner.

        Read rather than guessed: a banner that says "Sync issues" when the real
        problem is an invalid filename sends the user looking in the wrong
        place. Only reached on a state change, never on every tick.
        """
        try:
            issues: list[SyncIssue] = repo_sync.open_issues(self.account.id)
        except Exception:  # noqa: BLE001 - a banner is never worth a crash
            log.debug("could not read open issues for the banner", exc_info=True)
            return None
        order = {IssueSeverity.BLOCKING: 0, IssueSeverity.ERROR: 1,
                 IssueSeverity.WARNING: 2, IssueSeverity.INFO: 3}
        ranked = sorted(issues, key=lambda i: (order.get(i.severity, 9),
                                               i.last_seen_at or ""))
        return ranked[0].code if ranked else None

    # ═════════════════════════════════════════════════════════════════════════
    # Latches
    # ═════════════════════════════════════════════════════════════════════════

    def _reconcile_latches(self, facts: Facts) -> None:
        """Set and clear the two latches that have an *observational* clear.

        ``needs_resync`` and ``bisync_critical`` are deliberately absent: per
        ARCHITECTURE §6.5 they are cleared only by a successful ``--resync``,
        which is an explicit action a human approved. Clearing them because
        bisync happens to look idle would silently discard the reason the user
        was asked in the first place.
        """
        full = facts.quota.is_full or facts.out_of_space
        latched = LATCH.QUOTA_EXCEEDED in facts.latches
        if full and not latched:
            self._set_latch(LATCH.QUOTA_EXCEEDED, "about reports no free space")
        elif latched and not full and facts.quota.total > 0:
            # A contradicting observation, and only a real one: a quota of zero
            # means `about` has not answered yet, not that space appeared.
            self._clear_latch(LATCH.QUOTA_EXCEEDED)

        if facts.mount is MountHealth.UP:
            if self._mount_healthy_since is None:
                self._mount_healthy_since = self._monotonic()
            elif (LATCH.MOUNT_FAILED in facts.latches
                  and self._monotonic() - self._mount_healthy_since
                  >= MOUNT_HEALTHY_CLEAR_S):
                self._clear_latch(LATCH.MOUNT_FAILED)
        else:
            self._mount_healthy_since = None

    def _set_latch(self, name: str, detail: str = "") -> None:
        log.warning("latching %s for %s: %s", name, self.account.id, detail)
        try:
            repo_sync.set_latch(self.account.id, name, detail or None,
                                writer=self._writer)
        except Exception:  # noqa: BLE001 - a failed latch write must not kill the tick
            log.error("could not persist the %s latch", name, exc_info=True)

    def _clear_latch(self, name: str) -> None:
        log.info("clearing the %s latch for %s", name, self.account.id)
        try:
            repo_sync.clear_latch(self.account.id, name, writer=self._writer)
        except Exception:  # noqa: BLE001
            log.error("could not clear the %s latch", name, exc_info=True)

    # ═════════════════════════════════════════════════════════════════════════
    # Effects
    # ═════════════════════════════════════════════════════════════════════════

    def _run_effect(self, effect: str, facts: Facts) -> None:
        handler = self._EFFECTS.get(effect)
        if handler is None:
            # The reducer named something this object cannot do. That is a
            # programming error and is loud, because an effect that silently
            # does nothing is a pause that never defers or a mount that is never
            # restarted.
            raise KeyError(f"no handler for effect {effect!r}")
        try:
            getattr(self, handler)(facts)
        except SafetyRefusal:
            raise
        except Exception:  # noqa: BLE001 - one failed effect must not stop the rest
            log.error("effect %s failed", effect, exc_info=True)

    # ── notifications ───────────────────────────────────────────────────────
    def _toast(self, nid: NotificationId, **fmt: object) -> None:
        if self._notifier is None:
            log.debug("no notifier; skipping the %s toast", nid.value)
            return
        self._notifier.toast(nid, account_id=self.account.id, **fmt)

    def _effect_toast_paused(self, facts: Facts) -> None:
        """Carries the "Sync Anyway" action, which is the point of the toast.

        A silent automatic pause is indistinguishable from a broken client, and
        the user has no way to discover the override exists.
        """
        nid = (NotificationId.SYNC_PAUSED_METERED
               if facts.policy_pause is PauseReason.METERED
               else NotificationId.SYNC_PAUSED_BATTERY)
        self._toast(nid)

    def _effect_toast_quota_full(self, facts: Facts) -> None:
        self._toast(NotificationId.QUOTA_FULL)

    def _effect_toast_sign_in(self, facts: Facts) -> None:
        self._toast(NotificationId.SIGN_IN_REQUIRED)

    def _effect_toast_account_blocked(self, facts: Facts) -> None:
        self._toast(NotificationId.ACCOUNT_BLOCKED)

    def _effect_toast_decision(self, facts: Facts) -> None:
        self._toast(NotificationId.MASS_DELETE)

    def _effect_toast_sync_issues(self, facts: Facts) -> None:
        self._toast(NotificationId.SYNC_ISSUES, n=facts.issues_error)

    def _effect_toast_sync_complete(self, facts: Facts) -> None:
        self._toast(NotificationId.SYNC_COMPLETE)

    def _effect_toast_mount_lost(self, facts: Facts) -> None:
        self._toast(NotificationId.MOUNT_LOST)

    def _effect_toast_mount_restored(self, facts: Facts) -> None:
        self._toast(NotificationId.MOUNT_RESTORED)

    # ── the pause machinery ─────────────────────────────────────────────────
    def _effect_pause_enforce(self, facts: Facts) -> None:
        """Begin deferring the VFS queue. **Never unmounts.**

        Pausing on Windows stops sync; it does not make your files disappear.
        Unmounting would strand every cached file the user can currently open
        offline, which is the opposite of what "pause" means to anyone.
        """
        self._enforce_pause()

    def _effect_pause_enforce_uploads(self, facts: Facts) -> None:
        """Uploads only — a full drive can still hydrate a file on demand."""
        self._enforce_pause()

    def _enforce_pause(self) -> None:
        if self._pause is None or self._mountd is None:
            log.debug("no pause manager or mount controller; nothing to defer")
            return
        endpoint = self._mountd.endpoint(self.account)
        if endpoint is None:
            return
        deferred = self._pause.enforce(endpoint,
                                       reason=_PAUSE_REASON.get(self._state))
        log.info("deferred %s queued uploads for %s", deferred, self.account.id)

    def _effect_pause_release(self, facts: Facts) -> None:
        """Flush the deferred queue on resume.

        `release(ep)` returns 0 immediately when `ep` is None, and it was being
        called bare — so resuming from a pause released nothing and the deferred
        uploads sat until their horizon expired on its own, which is the whole
        thing the flush exists to avoid.
        """
        if self._pause is None:
            return
        resume = getattr(self._pause, "release", None)
        if not callable(resume):
            return
        endpoint = (self._mountd.endpoint(self.account)
                    if self._mountd is not None else None)
        released = resume(endpoint)
        log.info("released %s deferred upload(s) for %s", released,
                 self.account.id)

    # ── jobs and stats ──────────────────────────────────────────────────────
    def _effect_jobs_suspend(self, facts: Facts) -> None:
        if self._jobs is None:
            return
        self._jobs.invalidate_all("sync suspended: the account needs attention")

    def _effect_jobs_resume(self, facts: Facts) -> None:
        log.info("resuming scheduled work for %s", self.account.id)

    def _effect_stats_drain(self, facts: Facts) -> None:
        """Persist `core/transferred` into the activity table.

        This must happen before the reset. `core/stats-reset` wipes
        `core/transferred` along with the counters, so a reset first destroys
        the only record that those transfers ever happened, and the Activity
        Center's history with them.
        """
        if self._stats is None:
            return
        self._stats.drain_now()

    def _effect_stats_reset(self, facts: Facts) -> None:
        """`core/stats-reset` for the group, *after* the drain above.

        Routed through the injected runners rather than called here because
        `rc.stats.reset_group()` is blocking and belongs on an IOPool worker.
        With no runner wired the counters simply keep accumulating, which is
        untidy but harmless — unlike the reverse mistake, which loses history.
        """
        self._run_job("stats_reset")

    # ── the mount ───────────────────────────────────────────────────────────
    def _effect_mount_force_unmount(self, facts: Facts) -> None:
        """`fusermount3 -uz` on an ENOTCONN corpse.

        Nothing else fixes it: the kernel keeps the mount entry, `ismount()`
        keeps saying True, and every filesystem call against it blocks or
        returns ENOTCONN until the entry is gone.
        """
        if self._mountd is None:
            return
        self._mountd.unmount(self.account, lazy=True)

    def _effect_mount_restart(self, facts: Facts) -> None:
        self.restart_mount("the mount went stale")

    # ── the UI ──────────────────────────────────────────────────────────────
    def _effect_dialog_decision(self, facts: Facts) -> None:
        """Ask the UI to raise the pending decision, once.

        Emitted on the *transition* into NEEDS_ATTENTION rather than every tick,
        so a dialog the user dismissed does not reappear 400 ms later.
        """
        for decision in repo_sync.pending_decisions(self.account.id):
            BUS.decision_required.emit(decision)
            return

    def _effect_banner_quota(self, facts: Facts) -> None:
        log.info("quota banner raised for %s: %s used of %s",
                 self.account.id, facts.quota.used, facts.quota.total)

    def _effect_ipc_invalidate(self, facts: Facts) -> None:
        """Tell Nautilus its emblems are out of date.

        It caches what we last told it and will not ask again on its own, so
        without this a file that finished uploading keeps its syncing emblem
        until the folder is reopened.
        """
        if self._ipc is None:
            return
        self._ipc.broadcast_invalidate([self.account.sync_root])

    #: Effect name -> the method that performs it. Data, so a test can assert
    #: that every name the reducer can emit has somewhere to land.
    _EFFECTS: Final[dict[str, str]] = {
        EFFECT.TOAST_PAUSED: "_effect_toast_paused",
        EFFECT.TOAST_QUOTA_FULL: "_effect_toast_quota_full",
        EFFECT.TOAST_SIGN_IN: "_effect_toast_sign_in",
        EFFECT.TOAST_ACCOUNT_BLOCKED: "_effect_toast_account_blocked",
        EFFECT.TOAST_DECISION: "_effect_toast_decision",
        EFFECT.TOAST_SYNC_ISSUES: "_effect_toast_sync_issues",
        EFFECT.TOAST_SYNC_COMPLETE: "_effect_toast_sync_complete",
        EFFECT.TOAST_MOUNT_LOST: "_effect_toast_mount_lost",
        EFFECT.TOAST_MOUNT_RESTORED: "_effect_toast_mount_restored",
        EFFECT.PAUSE_ENFORCE: "_effect_pause_enforce",
        EFFECT.PAUSE_ENFORCE_UPLOADS: "_effect_pause_enforce_uploads",
        EFFECT.PAUSE_RELEASE: "_effect_pause_release",
        EFFECT.JOBS_SUSPEND: "_effect_jobs_suspend",
        EFFECT.JOBS_RESUME: "_effect_jobs_resume",
        EFFECT.STATS_DRAIN: "_effect_stats_drain",
        EFFECT.STATS_RESET: "_effect_stats_reset",
        EFFECT.MOUNT_FORCE_UNMOUNT: "_effect_mount_force_unmount",
        EFFECT.MOUNT_RESTART: "_effect_mount_restart",
        EFFECT.DIALOG_DECISION: "_effect_dialog_decision",
        EFFECT.BANNER_QUOTA: "_effect_banner_quota",
        EFFECT.IPC_INVALIDATE: "_effect_ipc_invalidate",
    }

    # ═════════════════════════════════════════════════════════════════════════
    # Scheduled maintenance
    # ═════════════════════════════════════════════════════════════════════════

    def _run_due_jobs(self) -> None:
        """Run whatever is due, on the back of the tick.

        Deliberately not a second timer. Riding the tick means maintenance can
        never run while the engine is stopped, never overlaps a tick, and stops
        the moment the loop does — three failure modes that a separate
        ``QTimer`` per job would each have to solve on its own.
        """
        now = self._monotonic()
        for name, key in (("quota", "quota_s"),
                          ("cache_scan", "cache_scan_s"),
                          ("verify", "verify_s"),
                          ("token_keepalive", "token_keepalive_s"),
                          ("prune", "prune_s")):
            interval = self.SCHEDULE[key]
            if now - self._last_job_run.get(key, 0.0) < interval:
                continue
            self._last_job_run[key] = now
            self._run_job(name)

    def _run_job(self, name: str) -> None:
        runner = self._runners.get(name) or getattr(self, f"_job_{name}", None)
        if runner is None:
            log.debug("scheduled job %r has no runner; skipping", name)
            return
        log.info("running scheduled job %r for %s", name, self.account.id)
        try:
            runner()
        except Exception:  # noqa: BLE001 - maintenance never kills the loop
            log.error("scheduled job %r failed", name, exc_info=True)

    def _job_quota(self) -> None:
        """Refresh `operations/about`, which answers quota *and* token health."""
        if self._quota is None:
            return
        refresh = getattr(self._quota, "refresh", None)
        if callable(refresh):
            refresh()

    def _job_prune(self) -> None:
        """Housekeeping: expire stale decisions.

        Expiry means **do not delete**, matching Microsoft's own seven-day
        policy: an unanswered "are you sure you want to delete 4 000 files?"
        that ages out must resolve to *not deleting them*, never to assuming
        consent from silence.
        """
        expired = repo_sync.expire_decisions(self.account.id, writer=self._writer)
        if expired:
            log.info("expired %d unanswered decisions for %s (nothing was deleted)",
                     len(expired), self.account.id)

    # ═════════════════════════════════════════════════════════════════════════
    # Actions — the single entry point
    # ═════════════════════════════════════════════════════════════════════════

    def do(self, action: RecoveryAction, **kw: Any) -> None:
        """Perform a recovery or user action. **The only way to change anything.**

        Args:
            action: What to do.
            **kw: The action's arguments — ``issue`` for the issue-scoped ones,
                ``rel_path`` for the file ones, ``decision_id`` for a resync.

        Raises:
            KeyError: The action has no handler. Deliberately loud: an action
                that silently passes is a button that does nothing, and the user
                has no way to tell that apart from a slow one.
            SafetyRefusal: The action would violate an invariant.
        """
        handler = self._ACTIONS.get(action)
        if handler is None:
            raise KeyError(f"no handler for recovery action {action!r}")

        # Re-entrancy guard. Four actions — FORCE_DELETE, RESTORE_FROM_BACKUP,
        # UNLOCK_BISYNC and STOP_SYNCING_ITEM — are delegated in BOTH
        # directions: `do()` hands them to `IssueEngine.execute()`, whose
        # `_fix_*` hands them straight back to `do()`. Neither side implements
        # them, so the pair recursed until Python gave up, and a user clicking
        # the fix button on a sync issue took the whole client down with a
        # RecursionError. Refusing the re-entry turns a crash into one honest
        # log line, and it holds for any future cycle rather than for these four.
        if action in self._in_flight:
            log.error(
                "do(%s) re-entered itself for %s — the action is delegated in a "
                "cycle and implemented at neither end; refusing to recurse",
                action.value, self.account.id)
            return

        self.history.append((utcnow_iso(), action.value, dict(kw)))
        del self.history[:-200]
        log.info("do(%s) for %s: %s", action.value, self.account.id, sorted(kw))
        self._in_flight.add(action)
        try:
            getattr(self, handler)(**kw)
        finally:
            self._in_flight.discard(action)

    # ── issue-scoped: delegated to WP-07's engine ───────────────────────────
    def _action_via_issues(self, action: RecoveryAction, **kw: Any) -> None:
        issue = kw.pop("issue", None)
        if self._issues is None:
            log.warning("no issue engine wired; %s was not performed", action.value)
            return
        self._issues.execute(action, issue, **kw)

    def _do_retry(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.RETRY, **kw)

    def _do_rename(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.RENAME, **kw)

    def _do_skip(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.SKIP, **kw)

    def _do_keep_both(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.KEEP_BOTH, **kw)

    def _do_keep_local(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.KEEP_LOCAL, **kw)

    def _do_keep_cloud(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.KEEP_CLOUD, **kw)

    def _do_force_delete(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.FORCE_DELETE, **kw)

    def _do_restore_from_backup(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.RESTORE_FROM_BACKUP, **kw)

    def _do_unlock_bisync(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.UNLOCK_BISYNC, **kw)

    def _do_stop_syncing_item(self, **kw: Any) -> None:
        self._action_via_issues(RecoveryAction.STOP_SYNCING_ITEM, **kw)

    # ── handled here ────────────────────────────────────────────────────────
    def _do_sign_in(self, **kw: Any) -> None:
        """Start OAuth. The URL reaches the UI on `BUS.auth_url_ready`.

        The mount is deliberately left alone throughout: cached files stay
        readable while the user signs in, matching Windows, so an expired token
        is an inconvenience rather than every file vanishing.
        """
        if self._auth is None:
            log.warning("no auth flow wired; sign-in was not started")
            return
        # `AuthFlow.start(remote, ...)` requires the remote to re-authorise —
        # calling it bare raised TypeError, so even a wired flow could not have
        # signed anyone in.
        #
        # `update=True` because SIGN_IN is always a *re*-authentication: it is
        # the recovery action for AUTH_EXPIRED and AUTH_MFA, on an account whose
        # remote is already in `rclone.conf`. The flag picks `config/update`
        # over `config/create`, and on rclone v1.75.0 that difference is
        # destructive: `create` deletes the whole section before rewriting it,
        # so every key this app wrote under invariant I1 — the backend options
        # from the rclone page, chunk size, region, client_id, drive_id and
        # drive_type — is silently lost at the exact moment the user is already
        # troubleshooting. `update` edits in place and keeps them. The wizard's
        # own call stays on `create`: its remote genuinely is new.
        self._auth.start(self.account.remote, update=True)

    def _do_pin(self, *, rel_path: str | None = None,
                recursive: bool = False, **kw: Any) -> None:
        """Keep a path on this device. The other half of free-up-space.

        Hydration itself is the Pinner's job and runs on the IOPool; this only
        records the intent and asks for it.
        """
        if self._pinner is None:
            log.warning("no pinner wired; pin was not performed")
            return
        if not rel_path:
            log.warning("pin needs a rel_path")
            return
        # A folder is pinned recursively unless told otherwise. "Always keep on
        # this device" on a folder means its contents; pinning the directory
        # entry alone downloads nothing, which looks exactly like the feature
        # not working. The caller can still ask for a shallow pin explicitly.
        if not recursive:
            from pathlib import Path as _Path

            try:
                recursive = (_Path(self.account.sync_root).expanduser()
                             / rel_path).is_dir()
            except OSError:
                recursive = False
        self._pinner.pin(rel_path, recursive=recursive)

    def _do_unpin(self, *, rel_path: str | None = None,
                  recursive: bool = False, **kw: Any) -> None:
        """Stop keeping a path on this device.

        Deliberately *not* an eviction: unpinning releases the promise, and
        the cache reclaims the bytes on its own schedule. A user who wants the
        space back now asks for free-up-space, which is a separate action with
        its own guard.
        """
        if self._pinner is None:
            log.warning("no pinner wired; unpin was not performed")
            return
        if not rel_path:
            log.warning("unpin needs a rel_path")
            return
        self._pinner.unpin(rel_path, recursive=recursive)

    def _do_free_up_space(self, *, rel_path: str | None = None, **kw: Any) -> None:
        """Evict cached copies. Never touches a dirty or queued file.

        The refusal lives in ``rc/vfs.evict``/``guards.assert_evict_safe``
        (invariant I5), not here — this is the entry point, not the guard.
        """
        if self._pinner is None:
            log.warning("no pinner wired; free-up-space was not performed")
            return
        if rel_path:
            self._pinner.free_up_space(rel_path)
        else:
            self._pinner.free_up_all()

    def _do_get_more_storage(self, **kw: Any) -> None:
        self._open_url(WEB_GET_MORE_STORAGE)

    def _do_resync(self, *, decision_id: int | None = None, **kw: Any) -> None:
        if decision_id is None:
            raise SafetyRefusal(
                "I15", "a resync needs the id of the decision the user answered")
        self.request_resync(decision_id=decision_id)

    def _do_restart_mount(self, *, reason: str = "requested by the user",
                          **kw: Any) -> None:
        self.restart_mount(reason)

    def _do_reclaim_cache(self, **kw: Any) -> None:
        self.reclaim_orphaned_cache()

    def _do_open_web(self, *, url: str = "", **kw: Any) -> None:
        self._open_url(url or WEB_GET_MORE_STORAGE)

    def _do_show_in_folder(self, *, path: str = "", **kw: Any) -> None:
        """Open a directory; reveal a file inside its parent.

        The distinction is the whole behaviour. ``show_in_folder()`` opens the
        *containing* folder with the item selected — right for "show me where
        this file is", and wrong for "Open your OneDrive folder", which lands
        the user in their home directory with a folder highlighted instead of
        inside it. Both the tray menu and the Activity Center's folder button
        ask for the sync root, which is a directory, so the common case is
        open.
        """
        from pathlib import Path

        from onedriveui.platform import desktop

        target = path or self.account.sync_root
        if Path(target).expanduser().is_dir():
            desktop.open_path(target)
            return
        desktop.show_in_folder(target)

    def _open_url(self, url: str) -> None:
        from onedriveui.platform import desktop

        desktop.open_url(url)

    #: Every :class:`~onedriveui.models.RecoveryAction`, mapped to a method. A
    #: test asserts the coverage is total: a missing entry is a button that does
    #: nothing, which is worse than a button that reports a failure.
    _ACTIONS: Final[dict[RecoveryAction, str]] = {
        RecoveryAction.RETRY: "_do_retry",
        RecoveryAction.RENAME: "_do_rename",
        RecoveryAction.SKIP: "_do_skip",
        RecoveryAction.KEEP_BOTH: "_do_keep_both",
        RecoveryAction.KEEP_LOCAL: "_do_keep_local",
        RecoveryAction.KEEP_CLOUD: "_do_keep_cloud",
        RecoveryAction.SIGN_IN: "_do_sign_in",
        RecoveryAction.PIN: "_do_pin",
        RecoveryAction.UNPIN: "_do_unpin",
        RecoveryAction.FREE_UP_SPACE: "_do_free_up_space",
        RecoveryAction.GET_MORE_STORAGE: "_do_get_more_storage",
        RecoveryAction.RESYNC: "_do_resync",
        RecoveryAction.FORCE_DELETE: "_do_force_delete",
        RecoveryAction.RESTORE_FROM_BACKUP: "_do_restore_from_backup",
        RecoveryAction.UNLOCK_BISYNC: "_do_unlock_bisync",
        RecoveryAction.RESTART_MOUNT: "_do_restart_mount",
        RecoveryAction.RECLAIM_CACHE: "_do_reclaim_cache",
        RecoveryAction.OPEN_WEB: "_do_open_web",
        RecoveryAction.SHOW_IN_FOLDER: "_do_show_in_folder",
        RecoveryAction.STOP_SYNCING_ITEM: "_do_stop_syncing_item",
    }

    # ═════════════════════════════════════════════════════════════════════════
    # Named actions
    # ═════════════════════════════════════════════════════════════════════════

    def request_pause(self, reason: PauseReason, hours: int | None) -> None:
        """Pause syncing. **Never unmounts.**

        Args:
            reason: Why. ``MANUAL`` is the user's own choice; the others are
                policy and clear themselves when the condition does.
            hours: 2, 8 or 24, or ``None`` for "Until I resume".
        """
        if self._pause is None:
            log.warning("no pause manager wired; pause was not performed")
            return
        self._pause.pause(reason, hours)
        BUS.pause_changed.emit(reason, self._pause.until())

    def request_resume(self) -> None:
        """Resume, and drain the deferred queue."""
        if self._pause is None:
            return
        self._pause.resume()
        BUS.pause_changed.emit(PauseReason.NONE, None)

    def request_resync(self, *, decision_id: int) -> None:
        """Run a bisync ``--resync``. Requires an answered decision — invariant I15.

        ``--resync`` only ever *copies*: both sides end up with a matching
        superset and nothing is deleted. Run unattended it resurrects every file
        the user has ever deleted and leaves both names behind after every
        rename, so it is legitimate exactly three times — the first run, right
        after a filters change, and to recover from a critical abort — and each
        of those is a moment a human said yes to.

        Args:
            decision_id: The ``decisions`` row the UI recorded before it showed
                the dialog.

        Raises:
            SafetyRefusal: Invariant ``I15`` — no such row, the wrong kind,
                unanswered, or not an approval. An *expired* decision is a
                refusal, matching the seven-day rule.
        """
        # There is no bisync engine any more. This client mounts the remote;
        # it does not keep a second, locally-materialised copy synchronised
        # two-way, and "--resync" is meaningless without one. The decision is
        # still validated so an approval recorded by an older build cannot be
        # replayed into whatever comes next.
        row = self._decision_row(decision_id)
        if row is None:
            raise SafetyRefusal(
                "I15", f"no decision {decision_id} to authorise a resync")
        # Existence is not authorisation. The docstring above promises a refusal
        # for a decision that is the wrong kind, unanswered, or answered with
        # anything but yes — and the comment below promises the row is "still
        # validated" — but the only check here was `row is None`, so a decision
        # the user had explicitly *declined* authorised the resync just as well
        # as one they approved. `ANSWER_EXPIRED` is refused by the same
        # comparison, which is the seven-day rule: nobody was there to say yes.
        if str(row.get("kind") or "") != DecisionKind.RESYNC_CONFIRM.value:
            raise SafetyRefusal(
                "I15", f"decision {decision_id} is a "
                f"{row.get('kind')!r}, not a resync confirmation")
        if not row.get("answered_at"):
            raise SafetyRefusal(
                "I15", f"decision {decision_id} has not been answered")
        if str(row.get("answer") or "") != ANSWER_YES:
            raise SafetyRefusal(
                "I15", f"decision {decision_id} was answered "
                f"{row.get('answer')!r}, which is not an approval")
        log.warning(
            "a resync was requested for %s by decision %s, but this client has "
            "no two-way sync engine to run one", self.account.id, decision_id)

    def _decision_row(self, decision_id: int) -> Mapping[str, Any] | None:
        """One `decisions` row, as a mapping, or ``None``.

        Read through ``db.open_ro()`` rather than through ``repo_sync``, which
        offers ``pending_decisions()`` but no by-id getter — and an *answered*
        decision is by definition not pending. A read-only connection on this
        thread is the documented way to query inside the GUI's budget.
        """
        try:
            row = db.open_ro().execute(
                "SELECT id, account_id, kind, answered_at, answer "
                "FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        except sqlite3.Error:
            log.error("could not read decision %s", decision_id, exc_info=True)
            return None
        return dict(row) if row is not None else None

    def restart_mount(self, reason: str) -> None:
        """Restart the mount unit, unless an upload would be lost.

        **Invariant I3.** A file being uploaded exists in the VFS cache and
        nowhere else until the upload finishes. Restarting under it risks
        exactly the loss the invariant forbids, so this refuses — returns,
        having logged why — while ``uploadsInProgress`` is anything but zero,
        *and* when the count cannot be read at all, because "we could not ask"
        is not evidence that the answer is zero.

        The one exception is a mount that is already ``STALE``. An ENOTCONN
        corpse serves nobody, its uploads are already lost, and a restart is the
        only thing that brings the account back.

        Args:
            reason: Why, for the log. Recorded verbatim.
        """
        if self._mountd is None:
            log.warning("no mount controller wired; not restarting (%s)", reason)
            return

        health = self._mountd.health(self.account)
        if health is not MountHealth.STALE:
            pending = self._mountd.uploads_in_progress(self.account)
            if pending != 0:
                # `-1` is "we could not ask", and that splits in two. If the
                # mount's rc is alive but the call failed, stay cautious: there
                # may be an upload we cannot see. If nothing is serving at all,
                # there is no VFS holding anything and nothing to protect.
                #
                # That distinction is what makes stale-mount recovery work. The
                # reducer emits MOUNT_FORCE_UNMOUNT and MOUNT_RESTART together,
                # in that order, so by the time the restart runs the corpse is
                # gone, the mount is no longer STALE, `vfs/stats` has nobody to
                # answer it — and the refusal below fired every single time. The
                # recovery path could not complete: the client tore the dead
                # mount down and then declined to bring it back, leaving the
                # account with no filesystem until someone restarted the app.
                unreachable = pending < 0
                if unreachable and not self._mountd.is_serving(self.account):
                    log.info(
                        "no mount is serving %s; there is no upload to protect, "
                        "restarting (%s)", self.account.id, reason)
                else:
                    log.warning(
                        "refusing to restart the mount for %s (%s): %s — invariant "
                        "I3 forbids disturbing an upload that exists nowhere else",
                        self.account.id, reason,
                        f"uploadsInProgress={pending}" if pending > 0
                        else "vfs/stats could not be read")
                    return

        if self._mountd.restarts_this_hour(self.account) >= MOUNT_RESTART_MAX_PER_HOUR:
            # The ladder is exhausted. Latch it, so the state survives a restart
            # of *us* and the user is told rather than left watching a spinner.
            self._set_latch(LATCH.MOUNT_FAILED,
                            f"{MOUNT_RESTART_MAX_PER_HOUR} restarts in an hour")
            return

        self._mountd.restart(self.account, reason)
        # Suppress the MOUNTING that we are about to cause: a spinner appearing
        # the instant we tore the mount down reads as a fault the user caused.
        self._debouncer.note_mount_restart(self._monotonic())

    def reset_client(self, *, keep_files: bool = True) -> None:
        """Tear the client's own state down and start again.

        Args:
            keep_files: Always honoured, and defaulted to True on purpose.
                "Reset" means our caches, units and database — never the user's
                files. There is no combination of arguments here that deletes
                anything under ``sync_root``.
        """
        if not keep_files:
            raise SafetyRefusal(
                "I2", "reset_client never deletes files under sync_root; "
                      "keep_files=False is not an option this client offers")
        log.warning("resetting the client for %s (files are untouched)",
                    self.account.id)
        self.stop()
        if self._mountd is not None:
            self._mountd.unmount(self.account, lazy=True)
        stop_daemon = getattr(self._rcd, "stop", None)
        if callable(stop_daemon):
            stop_daemon()
        repo_sync.clear_all_latches(self.account.id, writer=self._writer)
        self._debouncer.reset()

    def reclaim_orphaned_cache(self) -> int:
        """Delete abandoned VFS cache trees left behind by a renamed remote.

        Passing a backend flag on the mount command line renames the filesystem
        — ``onedrive:`` becomes ``onedrive{MxOuf}:`` — and rclone then builds a
        *new* cache tree under the new name, orphaning the old one. Tens of
        gigabytes can sit there indefinitely, belonging to nothing. Invariant I1
        stops us creating any more; this reclaims the ones already there.

        Returns:
            Bytes reclaimed.
        """
        if self._mountd is None:
            return 0
        from onedriveui.rc import guards, vfs

        endpoint = self._mountd.endpoint(self.account)
        if endpoint is None:
            return 0
        info = vfs.disk_cache_info(endpoint)
        freed = 0
        for path, size in vfs.orphaned_cache_trees(info):
            # Two guards before anything is removed, because this is the only
            # place in the engine that deletes a directory tree outright.
            # `orphaned_cache_trees` already refuses to report the live tree or
            # another remote's; these check that the answer is still true now.
            if path == info.path or not path.is_dir():
                continue
            guards.assert_not_under_fuse(path, "reclaiming an orphaned cache tree")
            # Both halves go, or neither means anything: a `vfs` tree without
            # its `vfsMeta` twin is a set of files rclone believes are cached
            # and cannot describe.
            meta = vfs.meta_tree_for(info, path)
            log.warning("reclaiming orphaned cache tree %s (%d bytes)", path, size)
            if _remove_tree(path):
                freed += size
                if meta != path and meta.is_dir():
                    _remove_tree(meta)
        self._clear_latch(LATCH.ORPHAN_CACHE)
        return freed


def _remove_tree(path: Path) -> bool:
    """Delete a directory tree, reporting rather than raising on failure.

    Reclaiming cache is housekeeping. A tree that cannot be removed — a
    permission problem, a file still open — is worth a log line and nothing
    more; raising here would turn "we could not free 40 GB" into "the About
    pane crashed".

    Args:
        path: The tree to remove.

    Returns:
        True when it is gone.
    """
    try:
        shutil.rmtree(path)
    except OSError:
        log.error("could not remove the cache tree %s", path, exc_info=True)
        return False
    return True
