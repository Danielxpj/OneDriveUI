"""Assembling one immutable observation of the world, once per tick.

This is the only module that talks to every subsystem, and it is deliberately
the module that **mutates nothing**. It reads the kernel, rclone, the disk and
the session bus, packs the answers into a frozen
:class:`~onedriveui.models.Facts`, and hands it to
:func:`~onedriveui.sync.reducer.reduce`. Anything that acts on what it finds
lives in :mod:`~onedriveui.sync.supervisor`.

Three properties are load-bearing.

**Nothing is remembered that is not re-observable.** Every field is either read
fresh from the world or read back out of SQLite. That is what makes crash
recovery exact rather than approximate: a ``SIGKILL`` takes the collector, the
debouncer and every in-memory counter with it, and the next process rebuilds the
same ``Facts`` from the mount table, the vfsMeta sidecars, the bisync working
directory and the ``latches`` / ``issues`` / ``decisions`` tables. The one
exception is :attr:`~onedriveui.models.Facts.consecutive_net_failures`, which
restarts at zero — and it is bounded by design, because three ticks of a dead
network re-establish it.

**One dead subsystem never kills the tick.** Each source is called inside its
own ``try``/``except``, and the whole pass has a
:data:`~FactCollector.BUDGET_MS` budget. A source that raises, or that the
budget ran out before reaching, keeps its **previous value** and has its name
added to :attr:`~onedriveui.models.Facts.stale`. A stale value is honest — it
was true a moment ago — where a zeroed one is a lie that reads as "nothing is
happening", which is exactly the wrong thing to tell the ladder.

**Nothing expensive happens here.** Every source is a cheap local read
(``/proc``, ``statvfs``, SQLite) or a value some asynchronous poller has already
cached. ARCHITECTURE §7.1 forbids synchronous network I/O on the GUI thread, so
the expensive observations — ``operations/about`` for quota and token health,
``vfs/stats`` for the upload queue — arrive through injected providers that the
Supervisor refreshes on its own schedule. The collector's job is to *sample*
them, not to fetch them.

Every service is injected and every one is optional, so a test builds a
collector out of four lambdas and no daemon, and WP-06's pause manager can be
plugged in later without this module importing it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.bus import BUS
from onedriveui.constants import TICK_ACTIVE_MS, TICK_IDLE_MS, TICK_PAUSED_MS
from onedriveui.data import repo_sync
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
    PauseIntent,
    PauseReason,
    PowerState,
    QuotaInfo,
    SyncState,
    TokenHealth,
    parse_iso,
    utcnow_iso,
)
from onedriveui.strings import ISSUE_TITLE, t
from onedriveui.sync.reducer import LATCH, reduce

log = logging.getLogger(__name__)

__all__ = ["FactCollector", "Source", "SOURCE_NAMES", "interval_for"]


def _iso_or_none(value: Any) -> str | None:
    """Normalise a deadline to the one timestamp format used everywhere."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat()).replace("+00:00", "Z")
    return str(value)


# ═════════════════════════════════════════════════════════════════════════════
# Cadence
# ═════════════════════════════════════════════════════════════════════════════

def interval_for(state: SyncState, facts: Facts) -> int:
    """How long to wait before the next tick, in milliseconds.

    Polling is a trade between how quickly the UI reacts and how much of the
    machine it costs to do nothing. Three rates cover it:

    * **400 ms while bytes are moving.** A progress bar that updates 2.5 times a
      second reads as live; one that updates every two seconds reads as stuck.
    * **10 s while paused.** Nothing is going to change except the user
      resuming, and that arrives as a signal, not as a poll.
    * **2 s otherwise.**

    Args:
        state: The debounced state.
        facts: The observation it came from.

    Returns:
        Milliseconds until the next tick.
    """
    if state in (SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
                 SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA):
        return TICK_PAUSED_MS
    if facts.transferring_count or facts.pin_jobs_active or facts.checks_active:
        return TICK_ACTIVE_MS
    return TICK_IDLE_MS


# ═════════════════════════════════════════════════════════════════════════════
# Sources
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class Source:
    """One observable subsystem.

    Attributes:
        name: The handle that appears in :attr:`~onedriveui.models.Facts.stale`
            and in the logs. Stable — the About pane lists them.
        fields: The ``Facts`` fields this source owns. Naming them is what makes
            carry-forward possible: when the source fails, exactly these keep
            their previous values and nothing else is disturbed.
        every_s: Minimum seconds between calls. ``0`` means every tick.
        read: Returns ``{field: value}``. May raise; the collector catches.
    """

    name: str
    fields: tuple[str, ...]
    every_s: float
    read: Callable[[], Mapping[str, Any]]


#: Every source name, in the order they are polled. Cheap and local first, so
#: that when the budget runs out it is the expensive, already-cached sources
#: that go stale rather than the mount health the ladder needs most.
SOURCE_NAMES: Final[tuple[str, ...]] = (
    "mount", "daemons", "persisted", "pause", "environment",
    "engine", "vfs", "bisync", "account", "notice",
)


class FactCollector(QObject):
    """Builds one :class:`~onedriveui.models.Facts` per tick.

    Args:
        account: The account being observed. One collector per account.
        rcd: The control-plane daemon supervisor — anything with ``health()``.
            ``None`` reports ``DOWN``, which is the truth when there is nothing
            supervising a daemon.
        mountd: The mount controller — ``health(account)`` and
            ``is_serving(account)``.
        stats: The ``core/stats`` poller. Only its cached ``last`` is read; this
            module never triggers a poll.
        vfs_stats: Returns the latest :class:`~onedriveui.models.DiskCacheInfo`,
            or ``None`` when it has not been sampled yet. Injected rather than
            called directly because ``vfs/stats`` is a network round trip.
        quota: Anything with ``current() -> QuotaInfo`` and
            ``token() -> TokenHealth``. WP-06 supplies the real one.
        pause: Anything with ``active() -> PauseReason``, ``until() -> str |
            None`` and optionally ``policy() -> PauseReason`` and
            ``overridden(reason) -> bool``. WP-06 supplies the real one.
        power: A :class:`~onedriveui.platform.power.PowerPolicy`.
        bisync_state: Returns the current :class:`~onedriveui.models.BisyncState`.
        pin_jobs: Returns how many hydration jobs are running.
        monotonic: The clock. Injected so a test can make a 1 500 ms budget
            elapse without sleeping.
        parent: Qt parent.

    Signals:
        collected: Emitted with the new ``Facts`` after every successful tick.
            Also re-emitted on :data:`~onedriveui.bus.BUS.facts_updated`, which
            is what the rest of the application listens to.
    """

    collected = Signal(Facts)

    #: The whole pass must finish inside this. A tick that takes longer than the
    #: idle interval would queue behind itself and the UI would fall further
    #: behind the longer anything is wrong — precisely when it must not.
    BUDGET_MS: Final[int] = 1500

    def __init__(
        self,
        account: AccountInfo,
        *,
        rcd: Any = None,
        mountd: Any = None,
        stats: Any = None,
        vfs_stats: Callable[[], DiskCacheInfo | None] | None = None,
        quota: Any = None,
        pause: Any = None,
        power: Any = None,
        bisync_state: Callable[[], BisyncState] | None = None,
        pin_jobs: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._rcd = rcd
        self._mountd = mountd
        self._stats = stats
        self._vfs_stats = vfs_stats
        self._quota = quota
        self._pause = pause
        self._power = power
        self._bisync_state = bisync_state
        self._pin_jobs = pin_jobs
        self._monotonic = monotonic

        self._started_at = monotonic()
        self._carry: dict[str, Any] = {}
        self._last = Facts(account_id=account.id, sampled_at=utcnow_iso())
        self._execute_id = ""
        self._net_failures = 0
        self._scan_in_progress = False
        self._mount_enabled = True
        #: When each source was last actually read, for the cadence check.
        self._last_read: dict[str, float] = {}
        #: This tick's latches, published by the `persisted` source for the
        #: `notice` source that runs after it. Carried forward with everything
        #: else when `persisted` could not be read.
        self._latches_now: frozenset[str] = frozenset()
        self._source_cache: tuple[Source, ...] | None = None

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._interval_ms = TICK_IDLE_MS
        self._running = False

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        """Begin ticking. Idempotent.

        The first tick runs immediately rather than after an interval: at
        start-up the tray has no state at all, and two seconds of nothing is
        two seconds in which the user assumes we did not launch.
        """
        if self._running:
            return
        self._running = True
        self._started_at = self._monotonic()
        self.tick()

    def stop(self) -> None:
        """Stop ticking. Idempotent. Leaves :meth:`last` readable."""
        self._running = False
        self._timer.stop()

    @property
    def running(self) -> bool:
        return self._running

    # ── reads ───────────────────────────────────────────────────────────────
    def last(self) -> Facts:
        """The most recent observation, without taking a new one."""
        return self._last

    @property
    def interval_ms(self) -> int:
        """The interval the next tick is scheduled at."""
        return self._interval_ms

    # ── inputs the collector is told about rather than polls ────────────────
    def note_network_result(self, ok: bool) -> None:
        """Record the outcome of an rc call, for the offline rung.

        Three consecutive failures is what "offline" means when
        ``NetworkMonitor`` still claims a connection — a captive portal, a VPN
        that dropped, a DNS server that stopped answering. One failure is a
        hiccup and must not blank the UI, which is why this counts rather than
        latches.
        """
        self._net_failures = 0 if ok else self._net_failures + 1

    def set_scan_in_progress(self, scanning: bool) -> None:
        """Mark a cache scan or first-run warm-up as running."""
        self._scan_in_progress = scanning

    def set_mount_enabled(self, enabled: bool) -> None:
        """Whether this account is configured to have a mount at all.

        An account without one is *healthy* with no mount, so the mounting rung
        must not fire for it.
        """
        self._mount_enabled = enabled

    # ── the tick ────────────────────────────────────────────────────────────
    def tick(self) -> Facts:
        """Take one observation, publish it, and schedule the next tick.

        Never raises. A source that fails is carried forward and named in
        ``stale``; a source the budget did not reach is treated identically,
        because from the ladder's point of view "we could not ask" and "we did
        not get an answer" are the same thing.

        Returns:
            The new :class:`~onedriveui.models.Facts`.
        """
        started = self._monotonic()
        values: dict[str, Any] = {}
        stale: set[str] = set()

        for source in self._sources():
            elapsed_ms = (self._monotonic() - started) * 1000.0
            if elapsed_ms >= self.BUDGET_MS:
                # Out of time. Everything not yet read keeps its previous value.
                self._carry_forward(source, values, stale)
                continue
            if not self._due(source, started):
                # Not due yet: reuse the previous value, but this is NOT stale —
                # it is the cadence working as designed.
                self._reuse(source, values)
                continue
            try:
                values.update(source.read())
                self._last_read[source.name] = started
            except Exception:  # noqa: BLE001 - one dead source must not kill the tick
                log.warning("fact source %r failed; carrying its last value forward",
                            source.name, exc_info=True)
                self._carry_forward(source, values, stale)

        facts = self._assemble(values, stale, started)
        self._remember(facts)
        self._last = facts
        self.collected.emit(facts)
        BUS.facts_updated.emit(facts)
        self._reschedule(facts)
        return facts

    # ── scheduling ──────────────────────────────────────────────────────────
    def _reschedule(self, facts: Facts) -> None:
        if not self._running:
            return
        self._interval_ms = interval_for(reduce(facts), facts)
        self._timer.start(self._interval_ms)

    def _on_timeout(self) -> None:
        if self._running:
            self.tick()

    def _due(self, source: Source, now: float) -> bool:
        if source.every_s <= 0:
            return True
        previous = self._last_read.get(source.name)
        return previous is None or (now - previous) >= source.every_s

    # ── carry-forward ───────────────────────────────────────────────────────
    def _carry_forward(self, source: Source, values: dict[str, Any],
                       stale: set[str]) -> None:
        """Reuse the last good value for a source that could not be read.

        The previous value is a fact that *was* true; a zero is a claim that
        nothing is happening. Between the two, only one of them can send the
        tray icon green while an upload is stuck.
        """
        self._reuse(source, values)
        stale.add(source.name)

    def _reuse(self, source: Source, values: dict[str, Any]) -> None:
        for field in source.fields:
            if field in self._carry:
                values[field] = self._carry[field]

    def _remember(self, facts: Facts) -> None:
        for source in self._sources():
            for field in source.fields:
                self._carry[field] = getattr(facts, field)

    # ── assembly ────────────────────────────────────────────────────────────
    def _assemble(self, values: dict[str, Any], stale: set[str],
                  now: float) -> Facts:
        execute_id = str(values.get("execute_id", self._execute_id) or "")
        # A changed executeId means the daemon restarted underneath us, which
        # invalidates every job handle we hold. An id appearing for the first
        # time is not a change — there was nothing to change from.
        changed = bool(self._execute_id and execute_id
                       and execute_id != self._execute_id)
        self._execute_id = execute_id or self._execute_id

        values["execute_id"] = execute_id
        values["execute_id_changed"] = changed
        values["account_id"] = self.account.id
        values["sampled_at"] = utcnow_iso()
        values["startup_elapsed_s"] = max(0.0, now - self._started_at)
        values["consecutive_net_failures"] = self._net_failures
        values["scan_in_progress"] = self._scan_in_progress
        values["mount_enabled"] = self._mount_enabled
        values["stale"] = frozenset(stale)

        allowed = {f for f in Facts.__slots__ if f != "__weakref__"}
        unknown = set(values) - allowed
        if unknown:
            # A source returning a field that does not exist is a programming
            # error, and silently dropping it would hide it until someone
            # wondered why a rung never fired.
            raise TypeError(f"fact source produced unknown fields: {sorted(unknown)}")
        return Facts(**values)

    # ═════════════════════════════════════════════════════════════════════════
    # The sources themselves
    # ═════════════════════════════════════════════════════════════════════════

    def _sources(self) -> tuple[Source, ...]:
        if self._source_cache is not None:
            return self._source_cache
        sources = (
            Source("mount", ("mount",), 0.0, self._read_mount),
            Source("daemons", ("daemon_rcd", "daemon_mount", "execute_id"), 2.0,
                   self._read_daemons),
            Source("persisted",
                   ("issues_blocking", "issues_error", "issues_warning",
                    "pending_decisions", "latches"), 0.0, self._read_persisted),
            Source("pause", ("pause", "policy_pause"), 0.0, self._read_pause),
            Source("environment", ("network", "power"), 0.0, self._read_environment),
            Source("engine", ("transfers_active", "checks_active", "last_error"),
                   0.0, self._read_engine),
            Source("vfs", ("uploads_queued", "uploads_in_progress", "errored_files",
                           "out_of_space"), 0.0, self._read_vfs),
            Source("bisync", ("bisync",), 5.0, self._read_bisync),
            Source("account", ("account_configured", "token", "quota"), 0.0,
                   self._read_account),
            Source("notice", ("info_notice", "pin_jobs_active"), 0.0, self._read_notice),
        )
        assert tuple(s.name for s in sources) == SOURCE_NAMES
        self._source_cache = sources
        return sources

    def _read_mount(self) -> Mapping[str, Any]:
        """`/proc/self/mounts` plus `statvfs` — invariant I6.

        Cheap enough to do every tick: ``statvfs`` on a live rclone mount is
        answered out of the VFS's cached ``about`` data and never touches the
        network. This is first in the list because a stale mount is the one
        hazard where every other read is about to block anyway.
        """
        if self._mountd is None:
            return {"mount": MountHealth.DOWN}
        return {"mount": self._mountd.health(self.account)}

    def _read_daemons(self) -> Mapping[str, Any]:
        """Both rc daemons, every two seconds.

        ``rcd.health()`` costs an ``rc/noop`` with a one-second timeout, which
        is why this is the one source with a cadence rather than running every
        400 ms tick.
        """
        out: dict[str, Any] = {"daemon_rcd": DaemonHealth.DOWN,
                               "daemon_mount": DaemonHealth.DOWN}
        if self._rcd is not None:
            out["daemon_rcd"] = self._rcd.health()
            endpoint = self._rcd.endpoint()
            if endpoint is not None and getattr(endpoint, "execute_id", ""):
                out["execute_id"] = endpoint.execute_id
        if self._mountd is not None:
            # The mount's own rc server is a separate process from the control
            # plane's. A mount can be UP with its rc port dead, which is exactly
            # the state in which a restart must refuse: we cannot prove that no
            # upload is in flight.
            serving = self._mountd.is_serving(self.account)
            health = self._mountd.health(self.account)
            if serving:
                out["daemon_mount"] = DaemonHealth.UP
            elif health is MountHealth.STARTING:
                out["daemon_mount"] = DaemonHealth.STARTING
        return out

    def _read_persisted(self) -> Mapping[str, Any]:
        """The tables that make a hazard survive a crash.

        SQLite on local disk, indexed, single-digit milliseconds. This is the
        source that makes ``reduce()`` produce the same answer after a
        ``SIGKILL`` as before it.
        """
        # `issue_counts` returns a key for EVERY severity plus "total", so
        # there is no missing-key case to guard. Muted issues are excluded by
        # default, which is the point of muting one: it must not keep the tray
        # in ERROR.
        counts = repo_sync.issue_counts(self.account.id)
        self._latches_now = frozenset(repo_sync.latches(self.account.id))
        return {
            "issues_blocking": counts[IssueSeverity.BLOCKING.value],
            "issues_error": counts[IssueSeverity.ERROR.value],
            "issues_warning": counts[IssueSeverity.WARNING.value],
            "pending_decisions": len(repo_sync.pending_decisions(self.account.id)),
            "latches": self._latches_now,
        }

    def _read_pause(self) -> Mapping[str, Any]:
        """Intent and policy, with expiry resolved *here*.

        The reducer has no clock, so an elapsed deadline has to be turned into
        "not paused" before the ladder sees it. Doing it here also means the
        expiry is evaluated against the same instant as everything else in this
        ``Facts``, so a replay of a recorded observation reduces identically.
        """
        intent = PauseIntent()
        policy = PauseReason.NONE

        if self._pause is not None:
            reason = self._pause.active()
            # `PauseManager.until()` answers a datetime; `Facts.pause.until` is
            # an ISO string, because a `Facts` has to be comparable field by
            # field across a restart and a datetime object is not.
            until_iso = _iso_or_none(self._pause.until())
            overridden = False
            checker = getattr(self._pause, "overridden", None)
            if callable(checker):
                overridden = bool(checker(reason))
            intent = PauseIntent(reason=reason, until=until_iso,
                                 overridden=overridden)
            policy_getter = getattr(self._pause, "policy", None)
            if callable(policy_getter):
                policy = policy_getter()
        elif self._power is not None:
            throttle, reason = self._power.should_throttle()
            policy = reason if throttle else PauseReason.NONE

        intent = self._expire(intent)
        return {"pause": intent, "policy_pause": policy}

    def _expire(self, intent: PauseIntent) -> PauseIntent:
        """Drop a pause whose deadline has passed.

        ``until=None`` with ``MANUAL`` is "Until I resume" and never expires;
        that is a deliberate choice by the user, not an omission.
        """
        if intent.reason is PauseReason.NONE or not intent.until:
            return intent
        deadline = parse_iso(intent.until)
        if deadline is None:
            return intent
        now = parse_iso(utcnow_iso())
        if now is not None and now >= deadline:
            return PauseIntent()
        return intent

    def _read_environment(self) -> Mapping[str, Any]:
        """`Gio.NetworkMonitor` and `Gio.PowerProfileMonitor`, via the pump.

        Both are signal-driven and cached by ``PowerPolicy``, so this is a
        property read rather than a D-Bus round trip.
        """
        if self._power is None:
            return {"network": NetworkState.ONLINE, "power": PowerState.NORMAL}
        network, power = self._power.state()
        return {"network": network, "power": power}

    def _read_engine(self) -> Mapping[str, Any]:
        """`core/stats` for this account's group, as the poller last saw it.

        Never polls: `StatsPoller` owns the cadence and the draining order, and
        a second caller here could reset a group between a drain and its reset.
        """
        stats: CoreStats | None = getattr(self._stats, "last", None)
        if stats is None:
            return {"transfers_active": 0, "checks_active": 0, "last_error": ""}
        return {
            "transfers_active": len(stats.transferring),
            "checks_active": len(stats.checking),
            "last_error": stats.last_error or "",
        }

    def _read_vfs(self) -> Mapping[str, Any]:
        """`vfs/stats.diskCache` — the upload queue and the local disk.

        Injected rather than fetched. ``vfs/stats`` is a network round trip to
        the mount daemon, and ARCHITECTURE §7.1 bans synchronous ones on this
        thread; the Supervisor refreshes the value and this samples it.
        """
        info = self._vfs_stats() if self._vfs_stats is not None else None
        if info is None:
            return {"uploads_queued": 0, "uploads_in_progress": 0,
                    "errored_files": 0, "out_of_space": False}
        return {
            "uploads_queued": info.uploads_queued,
            "uploads_in_progress": info.uploads_in_progress,
            "errored_files": info.errored_files,
            "out_of_space": info.out_of_space,
        }

    def _read_bisync(self) -> Mapping[str, Any]:
        """The offline-folder engine: unit state plus the working directory."""
        if self._bisync_state is None:
            return {"bisync": BisyncState.DISABLED}
        return {"bisync": self._bisync_state()}

    def _read_account(self) -> Mapping[str, Any]:
        """Whether the account exists, its token health and its quota.

        All three come from the injected quota service, because all three are
        answered by one ``operations/about`` call whose result it caches for
        five minutes. Asking here would be a network round trip per tick.
        """
        if self._quota is None:
            return {"account_configured": bool(self.account.remote),
                    "token": TokenHealth.UNKNOWN, "quota": QuotaInfo()}
        return {
            "account_configured": bool(self.account.remote),
            "token": self._quota.token(),
            "quota": self._quota.current(),
        }

    def _read_notice(self) -> Mapping[str, Any]:
        """The non-hazard banner, and how many hydration jobs are running.

        An info notice is something worth saying that is not worth colouring the
        tray icon for. Orphaned cache trees are the canonical one: tens of
        gigabytes of someone else's abandoned VFS cache is worth reclaiming, and
        is not in any sense a sync problem.
        """
        pin_jobs = self._pin_jobs() if self._pin_jobs is not None else 0
        notice: str | None = None
        if LATCH.ORPHAN_CACHE in self._latches_now:
            row = repo_sync.latch_detail(self.account.id).get(LATCH.ORPHAN_CACHE, {})
            notice = t(ISSUE_TITLE[IssueCode.ORPHANED_CACHE],
                       size=row.get("detail") or "space")
        return {"info_notice": notice, "pin_jobs_active": pin_jobs}
