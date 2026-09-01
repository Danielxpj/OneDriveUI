"""The state machine, as a pure function.

``reduce(facts) -> SyncState`` is a **first-match-wins priority ladder**, not a
transition graph (ARCHITECTURE §6). Events never assign a state. They mutate the
things :class:`~onedriveui.models.Facts` is built from — the kernel, rclone, the
disk, and a small set of persisted *latches* — and the ladder is re-evaluated
from scratch on the next tick.

Three properties fall out of that shape, and all three are why it was chosen:

* **The tray icon, the tooltip, the Activity Center headline and the Settings
  badge cannot disagree.** They are four renderings of one value. There is no
  code path that updates one and forgets another, because there is no code path
  that updates any of them — they read :func:`tray_for`, :func:`tooltip` and
  :func:`status_text` off the same :class:`~onedriveui.models.SyncState`.
* **Crash recovery is exact.** A `SIGKILL` destroys the collector, the debouncer
  and every counter in memory. It does not destroy the mount table, the vfsMeta
  sidecars, the bisync working directory or the SQLite tables — and those are
  the only inputs. Rebuild ``Facts`` after a restart and ``reduce()`` returns
  byte-identically what it returned before the kill.
* **The whole state layer is testable with hand-built dataclasses.** No daemon,
  no fixtures, no clock.

**This module imports nothing but** :mod:`onedriveui.models` **and**
:mod:`onedriveui.strings`, and a test enforces that. No Qt, no I/O, no globals,
no clock — ``reduce()`` cannot read the time even by accident, which is why
anything time-dependent (has the manual pause expired? is this source stale?) is
resolved by :mod:`~onedriveui.sync.facts` *before* the ladder sees it.

The one deliberate exception is a function-local import of
:mod:`onedriveui.units` inside :func:`_placeholders`, which is documented at its
site: byte formatting is subtle enough that a second copy of it would drift, and
a call-time import keeps the module-level dependency set at zero.

Hysteresis lives here too, in :class:`Debouncer`, but as a *separate* object.
``reduce()`` stays pure; the debouncer is the piece that carries state across
ticks, and it is the only piece, so the crash-recovery property above holds
regardless of what it was doing when the process died.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from onedriveui.models import (
    AccountInfo,
    AccountKind,
    BisyncState,
    DaemonHealth,
    Facts,
    MountHealth,
    NetworkState,
    PauseReason,
    SEVERE_STATES,
    SyncState,
    TokenHealth,
    TrayIcon,
    parse_iso,
)
from onedriveui.strings import TRAY_FOR_STATE, status_line, status_sub

__all__ = [
    "LADDER",
    "LATCH",
    "EFFECT",
    "reduce",
    "explain",
    "Debouncer",
    "status_text",
    "tooltip",
    "tray_for",
    "transition_effects",
    "progress_pct",
    "STARTUP_GRACE_S",
    "OFFLINE_FAILURE_THRESHOLD",
    "DEBOUNCE_SEVERE_TICKS",
    "DEBOUNCE_NORMAL_TICKS",
    "DEBOUNCE_IDLE_TICKS",
    "PROCESSING_ENTRY_DELAY_MS",
    "MOUNTING_SUPPRESS_S",
]


# ═════════════════════════════════════════════════════════════════════════════
# Tunables
#
# These are duplicated from `constants.py` rather than imported, because this
# module's dependency set is frozen at {models, strings} and enforced by a test.
# `test_reducer.py::TestPurity::test_tunables_match_constants` asserts every
# value below equals its counterpart there, so the duplication cannot drift.
# ═════════════════════════════════════════════════════════════════════════════

#: Rung 2's window. Below this many seconds since process start, a daemon that
#: is not up yet is "starting", not "broken".
STARTUP_GRACE_S: Final = 8.0

#: Rung 11. One dropped request is a hiccup; three in a row is being offline.
OFFLINE_FAILURE_THRESHOLD: Final = 3

#: A hazard is published on the tick it is first observed. Delaying an error to
#: be sure of it means showing a green cloud over a broken sync.
DEBOUNCE_SEVERE_TICKS: Final = 1

#: Everything else needs two consecutive ticks, so a single ECONNRESET during a
#: token refresh cannot blank the UI.
DEBOUNCE_NORMAL_TICKS: Final = 2

#: ``UP_TO_DATE`` needs three, because a multi-file batch goes briefly quiet
#: between transfers and a green cloud that flickers mid-upload is a bug report.
DEBOUNCE_IDLE_TICKS: Final = 3

#: ``PROCESSING`` additionally needs to have been the candidate for this long. A
#: 200 ms directory listing must not flash a banner.
PROCESSING_ENTRY_DELAY_MS: Final = 250

#: ``MOUNTING`` is suppressed for this long after a *deliberate* restart. We
#: broke the mount on purpose; showing the user a spinner they did not cause
#: reads as a fault.
MOUNTING_SUPPRESS_S: Final = 15


class LATCH:
    """Names of the persisted hazards in the ``latches`` table (ARCHITECTURE §6.5).

    A latch is how a hazard survives the thing that caused it going out of view.
    ``quota_exceeded`` is set by an HTTP 507 that will not repeat until the next
    upload is attempted; without the latch the state would bounce back to
    ``UP_TO_DATE`` one tick later and the user would never see why nothing
    uploads. Each one is cleared only by an explicit action or by a
    *contradicting observation* — never by time passing.
    """

    NEEDS_RESYNC = "needs_resync"       # -> rung 6
    BISYNC_CRITICAL = "bisync_critical"  # -> rung 5
    QUOTA_EXCEEDED = "quota_exceeded"   # -> rung 7
    MOUNT_FAILED = "mount_failed"       # -> rung 5
    ORPHAN_CACHE = "orphan_cache"       # -> an info notice, never a hazard

    ALL: Final[frozenset[str]] = frozenset({
        "needs_resync", "bisync_critical", "quota_exceeded",
        "mount_failed", "orphan_cache",
    })


class EFFECT:
    """The effect vocabulary :func:`transition_effects` emits.

    These are *declarations*, not calls: the reducer stays pure by naming what
    should happen, and :class:`~onedriveui.sync.supervisor.Supervisor` is the
    only thing that does it. That split is what lets a test assert "entering
    PAUSED_QUOTA defers uploads and leaves downloads alone" without a daemon.

    They are internal identifiers dispatched on by ``Supervisor._run_effect``,
    never shown to anyone, which is why they are here and not in ``strings``.
    """

    # notifications
    TOAST_PAUSED = "toast:paused"
    TOAST_QUOTA_FULL = "toast:quota_full"
    TOAST_SIGN_IN = "toast:sign_in_required"
    TOAST_ACCOUNT_BLOCKED = "toast:account_blocked"
    TOAST_DECISION = "toast:decision"
    TOAST_SYNC_ISSUES = "toast:sync_issues"
    TOAST_SYNC_COMPLETE = "toast:sync_complete"
    TOAST_MOUNT_LOST = "toast:mount_lost"
    TOAST_MOUNT_RESTORED = "toast:mount_restored"

    # the pause machinery
    PAUSE_ENFORCE = "pause:enforce"
    PAUSE_ENFORCE_UPLOADS = "pause:enforce_uploads"
    PAUSE_RELEASE = "pause:release"

    # jobs and stats
    JOBS_SUSPEND = "jobs:suspend"
    JOBS_RESUME = "jobs:resume"
    #: Order matters and is asserted: `core/stats-reset` also wipes
    #: `core/transferred`, so a reset before a drain destroys the only record
    #: that those transfers ever happened.
    STATS_DRAIN = "stats:drain_transferred"
    STATS_RESET = "stats:reset"

    # the mount
    MOUNT_FORCE_UNMOUNT = "mount:force_unmount"
    MOUNT_RESTART = "mount:restart"

    # the UI
    DIALOG_DECISION = "dialog:decision"
    BANNER_QUOTA = "banner:quota"
    IPC_INVALIDATE = "ipc:invalidate"


# ═════════════════════════════════════════════════════════════════════════════
# The ladder
# ═════════════════════════════════════════════════════════════════════════════

def _paused_for(facts: Facts, reason: PauseReason) -> bool:
    """Is a pause of this `reason` in force right now?

    Two fields can say so and they mean different things.
    ``facts.pause`` is the user's *intent*, persisted, and it is the only one
    that can be ``MANUAL``. ``facts.policy_pause`` is *derived* each tick from
    the network and power state plus config, so it evaporates on its own when
    the condition clears — which is exactly the difference between "I paused
    this" and "your laptop is on battery".

    ``overridden`` is "Sync Anyway": the user has acknowledged this specific
    reason for a window, so the rung must stop matching even though the
    condition that caused it is still perfectly true.

    Expiry is deliberately *not* checked here — this function has no clock and
    must not have one. :mod:`~onedriveui.sync.facts` clears an elapsed
    ``pause.until`` before building the ``Facts``, so by the time the ladder
    runs, a pause that is present is a pause that is live.
    """
    if facts.pause.overridden and facts.pause.reason is reason:
        return False
    return facts.policy_pause is reason or facts.pause.reason is reason


def _rung_signed_out(f: Facts) -> bool:
    return not f.account_configured


def _rung_initializing(f: Facts) -> bool:
    return (f.startup_elapsed_s < STARTUP_GRACE_S
            and f.daemon_rcd in (DaemonHealth.DOWN, DaemonHealth.STARTING))


def _rung_account_blocked(f: Facts) -> bool:
    # AADSTS65005. Re-authenticating will NOT fix this one, so it must not be
    # allowed to fall through to AUTH_REQUIRED and offer a sign-in button that
    # sends the user round a loop.
    return f.token is TokenHealth.TENANT_BLOCKED


def _rung_auth_required(f: Facts) -> bool:
    return f.token in (TokenHealth.EXPIRED, TokenHealth.MFA)


def _rung_error(f: Facts) -> bool:
    return (f.daemon_rcd in (DaemonHealth.DOWN, DaemonHealth.FOREIGN)
            or f.mount is MountHealth.STALE
            or f.bisync in (BisyncState.CRITICAL, BisyncState.LOCK_STUCK)
            or f.issues_blocking > 0
            or LATCH.BISYNC_CRITICAL in f.latches
            or LATCH.MOUNT_FAILED in f.latches)


def _rung_needs_attention(f: Facts) -> bool:
    return (f.pending_decisions > 0
            or f.bisync is BisyncState.NEEDS_RESYNC
            or LATCH.NEEDS_RESYNC in f.latches)


def _rung_paused_quota(f: Facts) -> bool:
    # `out_of_space` is the *local* disk cache filling up and `quota.is_full` is
    # the cloud drive filling up. Both stop uploads dead and both are fixed by
    # freeing something, so they share a rung and a headline.
    return (f.quota.is_full or f.out_of_space
            or LATCH.QUOTA_EXCEEDED in f.latches
            or _paused_for(f, PauseReason.QUOTA))


def _rung_paused_manual(f: Facts) -> bool:
    return _paused_for(f, PauseReason.MANUAL)


def _rung_paused_metered(f: Facts) -> bool:
    return _paused_for(f, PauseReason.METERED)


def _rung_paused_battery(f: Facts) -> bool:
    return _paused_for(f, PauseReason.BATTERY)


def _rung_offline(f: Facts) -> bool:
    return (f.network is NetworkState.OFFLINE
            or f.consecutive_net_failures >= OFFLINE_FAILURE_THRESHOLD)


def _rung_mounting(f: Facts) -> bool:
    # `mount_enabled` is false for an account configured without a mount, and
    # then a down mount is the correct steady state rather than a symptom.
    return f.mount_enabled and f.mount in (MountHealth.DOWN, MountHealth.STARTING)


def _rung_syncing(f: Facts) -> bool:
    return bool(f.transfers_active or f.uploads_in_progress or f.pin_jobs_active)


def _rung_processing(f: Facts) -> bool:
    return bool(f.scan_in_progress or f.checks_active
                or f.bisync is BisyncState.RUNNING or f.uploads_queued)


def _rung_warning(f: Facts) -> bool:
    return f.issues_error > 0


def _rung_info_notice(f: Facts) -> bool:
    return f.info_notice is not None


def _rung_always(f: Facts) -> bool:
    return True


#: The 17 rungs, highest priority first. The name is the diagnostic handle that
#: :func:`explain` and the logs use; it is never shown to a user.
#:
#: Rungs 13–15 are the subtle ones and they are deliberate. While transfers are
#: in flight *with* unresolved errors the state is ``SYNCING``, and the error
#: banner renders *below* the status line — which is exactly what Windows does
#: ("Syncing 12 files", plus a persistent "Sync issues" banner). ``WARNING``
#: only becomes the headline once the transfers quiesce. Putting `issues.error`
#: above `transfers_active` would hide live progress behind a stale complaint.
LADDER: Final[tuple[tuple[str, Callable[[Facts], bool], SyncState], ...]] = (
    ("signed_out",      _rung_signed_out,      SyncState.SIGNED_OUT),
    ("initializing",    _rung_initializing,    SyncState.INITIALIZING),
    ("account_blocked", _rung_account_blocked, SyncState.ACCOUNT_BLOCKED),
    ("auth_required",   _rung_auth_required,   SyncState.AUTH_REQUIRED),
    ("error",           _rung_error,           SyncState.ERROR),
    ("needs_attention", _rung_needs_attention, SyncState.NEEDS_ATTENTION),
    ("paused_quota",    _rung_paused_quota,    SyncState.PAUSED_QUOTA),
    ("paused_manual",   _rung_paused_manual,   SyncState.PAUSED_MANUAL),
    ("paused_metered",  _rung_paused_metered,  SyncState.PAUSED_METERED),
    ("paused_battery",  _rung_paused_battery,  SyncState.PAUSED_BATTERY),
    ("offline",         _rung_offline,         SyncState.OFFLINE),
    ("mounting",        _rung_mounting,        SyncState.MOUNTING),
    ("syncing",         _rung_syncing,         SyncState.SYNCING),
    ("processing",      _rung_processing,      SyncState.PROCESSING),
    ("warning",         _rung_warning,         SyncState.WARNING),
    ("info_notice",     _rung_info_notice,     SyncState.INFO_NOTICE),
    ("up_to_date",      _rung_always,          SyncState.UP_TO_DATE),
)


def reduce(facts: Facts) -> SyncState:
    """Collapse one observation of the world into one state.

    Pure: no I/O, no Qt, no globals, no clock. First match in :data:`LADDER`
    wins. ``NOT_RUNNING`` is never returned — it is what the *absence* of a tray
    icon means, not something the engine can observe about itself.

    Args:
        facts: One immutable observation, from
            :meth:`~onedriveui.sync.facts.FactCollector.collect`.

    Returns:
        The state, before hysteresis. Feed it through :class:`Debouncer` before
        showing it to anyone.
    """
    for _name, matches, state in LADDER:
        if matches(facts):
            return state
    # Unreachable: the last rung matches unconditionally. Kept so that deleting
    # that rung fails loudly instead of returning None into the tray.
    raise AssertionError("LADDER has no catch-all rung")


def explain(facts: Facts) -> tuple[str, SyncState]:
    """The winning rung's name alongside its state, for logs and diagnostics.

    "Why is it showing ERROR?" is the question every support thread starts with,
    and ``("error", SyncState.ERROR)`` answers only half of it — but the half
    that says *which rung* is the half that is hard to reconstruct afterwards.
    """
    for name, matches, state in LADDER:
        if matches(facts):
            return name, state
    raise AssertionError("LADDER has no catch-all rung")


# ═════════════════════════════════════════════════════════════════════════════
# Hysteresis
# ═════════════════════════════════════════════════════════════════════════════

class Debouncer:
    """Holds a candidate state until it has earned the right to be published.

    The poll runs at up to 2.5 Hz. Publishing every sample would make the tray
    icon thrash, and worse, would show the user a state that was true for 400 ms
    — a token refresh that momentarily fails ``operations/about`` would blank a
    healthy sync to an error and back again.

    So each state has to be observed consecutively before it takes effect, with
    three exceptions that all point the same way: **a hazard is never delayed,
    and reassurance always is.**

    ==================== ======= ================================================
    State                Ticks   Why
    ==================== ======= ================================================
    Severe               1       An error the user cannot see is worse than one
                                 shown 400 ms early.
    ``UP_TO_DATE``       3       A batch goes quiet between files; a green cloud
                                 mid-upload is a lie.
    ``PROCESSING``       2 + 250 ms  A fast directory listing must not flash.
    ``MOUNTING``         2, and suppressed 15 s after a deliberate restart.
    Everything else      2
    ==================== ======= ================================================

    This is the only object in the state layer that remembers anything across
    ticks, and it is deliberately not persisted: after a crash the debouncer
    restarts empty and the first tick republishes from scratch. That costs at
    most three ticks of a stale tray icon and buys the guarantee that nothing
    about the recovered state was inherited from before the crash.
    """

    def __init__(self, initial: SyncState = SyncState.INITIALIZING) -> None:
        self._current = initial
        self._candidate = initial
        self._count = 0
        self._first_seen = 0.0
        self._restart_at: float | None = None

    # ── reads ───────────────────────────────────────────────────────────────
    @property
    def current(self) -> SyncState:
        """The state currently published to the UI."""
        return self._current

    @property
    def candidate(self) -> SyncState:
        """The state being observed, which may not have been published yet."""
        return self._candidate

    @property
    def streak(self) -> int:
        """How many consecutive ticks :attr:`candidate` has been observed for."""
        return self._count

    # ── the tick ────────────────────────────────────────────────────────────
    def apply(self, new: SyncState, now_monotonic: float) -> SyncState:
        """Feed one raw :func:`reduce` result in; get the state to display out.

        Args:
            new: What the ladder just returned.
            now_monotonic: A monotonic clock reading, in seconds. Monotonic
                specifically: the wall clock can jump backwards across an NTP
                step or a suspend/resume, and a negative elapsed time would let
                ``MOUNTING`` through its suppression window early.

        Returns:
            The state to publish, which is the previous one until `new` has
            been confirmed.
        """
        if new is self._candidate:
            self._count += 1
        else:
            self._candidate = new
            self._count = 1
            self._first_seen = now_monotonic

        if new is self._current:
            return self._current
        if self._admits(new, now_monotonic):
            self._current = new
        return self._current

    def note_mount_restart(self, now_monotonic: float) -> None:
        """Record that *we* just restarted the mount on purpose.

        For the next :data:`MOUNTING_SUPPRESS_S` seconds the resulting
        ``MOUNTING`` is held back, because a spinner appearing the instant we
        tore the mount down reads as a fault the user caused. A restart we did
        not initiate — the kernel losing the FUSE connection — has no such call
        and is published normally.
        """
        self._restart_at = now_monotonic

    def reset(self) -> None:
        """Forget everything, as if the process had just started.

        Used when the account changes underneath us, where carrying a streak
        from the previous account's world would publish a state that was never
        observed about this one.
        """
        self._current = SyncState.INITIALIZING
        self._candidate = SyncState.INITIALIZING
        self._count = 0
        self._first_seen = 0.0
        self._restart_at = None

    # ── internals ───────────────────────────────────────────────────────────
    def _admits(self, new: SyncState, now: float) -> bool:
        if (new is SyncState.MOUNTING
                and self._restart_at is not None
                and now - self._restart_at < MOUNTING_SUPPRESS_S):
            return False
        if (new is SyncState.PROCESSING
                and (now - self._first_seen) * 1000.0 < PROCESSING_ENTRY_DELAY_MS):
            return False
        return self._count >= self._required(new)

    @staticmethod
    def _required(new: SyncState) -> int:
        if new in SEVERE_STATES:
            return DEBOUNCE_SEVERE_TICKS
        if new is SyncState.UP_TO_DATE:
            return DEBOUNCE_IDLE_TICKS
        return DEBOUNCE_NORMAL_TICKS


# ═════════════════════════════════════════════════════════════════════════════
# Rendering — the three surfaces, from one table
# ═════════════════════════════════════════════════════════════════════════════

def _human(n: int) -> str:
    """Decimal byte sizes, via the one implementation of them.

    ``units.human_bytes`` is imported *inside* this function on purpose. This
    module's frozen contract is that it imports nothing but ``models`` and
    ``strings`` at module scope, so that it stays importable with zero
    dependencies and provably cannot reach a clock or the filesystem. Copying
    ``human_bytes`` here to satisfy that letter would be worse than the rule is
    good: its rounding has a subtle re-check (999.96 MB must print "1.0 GB", not
    "1000.0 MB") that a second copy would eventually get wrong. ``units``
    depends only on ``constants``; there is nothing behind this import that
    could do I/O.
    """
    from onedriveui.units import human_bytes

    return human_bytes(n)


def _placeholders(state: SyncState, facts: Facts) -> dict[str, object]:
    """The format arguments this state's templates expect.

    The same key legitimately means different things in different templates —
    ``{total}`` is a *file count* in "Uploading 3 of 12" and a *byte total* in
    "252.5 GB of 1.1 TB used" — so they are built per state rather than in one
    union dict. ``strings.t()`` leaves an unmatched placeholder as literal text
    instead of raising, so getting this wrong shows ``{n}`` in the tray rather
    than crashing it; that is a safety net, not a licence.
    """
    quota = facts.quota
    if state is SyncState.SYNCING:
        done = facts.transfers_active
        total = done + facts.uploads_queued
        return {"n": facts.transferring_count, "done": done, "total": total,
                "bytes": _human(quota.used), "size": _human(quota.total)}
    if state in (SyncState.UP_TO_DATE, SyncState.INFO_NOTICE):
        return {"used": _human(quota.used), "total": _human(quota.total)}
    if state is SyncState.WARNING:
        return {"n": facts.issues_error}
    if state in (SyncState.ERROR, SyncState.NEEDS_ATTENTION):
        return {"n": facts.issues_blocking}
    if state is SyncState.PAUSED_MANUAL:
        return _pause_remaining(facts)
    return {"n": facts.transferring_count}


def _pause_remaining(facts: Facts) -> dict[str, object]:
    """``{hh, mm}`` for "Syncing will resume in 1h 30m".

    This is still pure. It does not ask what time it is; it subtracts two stamps
    that are both *inside* the facts — ``pause.until``, the deadline the user
    chose, and ``sampled_at``, the instant this observation was taken. Reading
    the real clock here would make the same ``Facts`` render differently on a
    replay, and the crash-recovery test compares exactly that.

    Returns ``None`` for both parts when there is no deadline to count down to:
    "Until I resume" has none by design, and a pause whose deadline has already
    passed is one :mod:`~onedriveui.sync.facts` is about to clear. `None` is the
    signal :func:`status_text` uses to drop the second line rather than print a
    countdown with braces in it.
    """
    until = parse_iso(facts.pause.until)
    sampled = parse_iso(facts.sampled_at)
    if until is None or sampled is None:
        return {"hh": None, "mm": None}
    remaining = int((until - sampled).total_seconds())
    if remaining <= 0:
        return {"hh": None, "mm": None}
    # Round the minutes UP, so a pause with 90 seconds left says "0h 2m" and
    # then "0h 1m" rather than sitting on "0h 1m" and jumping to resumed.
    return {"hh": remaining // 3600, "mm": -(-(remaining % 3600) // 60)}


def status_text(state: SyncState, facts: Facts) -> tuple[str, str]:
    """``(headline, subtext)`` for a state, fully formatted.

    Every user-visible word comes from :mod:`onedriveui.strings`; this function
    supplies the numbers and nothing else. The Activity Center headline, the
    tray tooltip and the Settings badge all call it, which is what makes them
    structurally unable to disagree.

    A subtext whose placeholders could not be resolved is dropped rather than
    shown with braces in it — "Syncing will resume in {hh}h {mm}m" is worse than
    no second line at all.
    """
    fmt = _placeholders(state, facts)
    headline = status_line(state, **fmt)
    if any(v is None for v in fmt.values()):
        sub = ""
    else:
        sub = status_sub(state, **fmt)
    if "{" in sub:
        sub = ""
    return headline, sub


def tooltip(state: SyncState, facts: Facts) -> str:
    """The tray tooltip: the same two lines, joined.

    StatusNotifierItem tooltips are plain text under the GNOME AppIndicator
    extension — no markup, and a long single line is truncated rather than
    wrapped — so this stays to two short lines and never embeds a path.
    """
    headline, sub = status_text(state, facts)
    return "\n".join(part for part in (headline, sub) if part)


def tray_for(state: SyncState, account: AccountInfo) -> TrayIcon:
    """The themed icon *name* for a state.

    Always a name, never a pixmap: StatusNotifierItem under the GNOME
    AppIndicator extension cannot reliably take raw pixmaps, and the caller is
    expected to go through ``QIcon.fromTheme()``.

    The one account-dependent case is the healthy one. Windows paints a blue
    cloud for work/school and a white one for personal, and a user with both
    accounts signed in tells them apart in the tray by exactly that.
    """
    icon = TRAY_FOR_STATE.get(state, TrayIcon.NONE)
    if icon is TrayIcon.SYNCED and account.kind is AccountKind.BUSINESS:
        return TrayIcon.SYNCED_BIZ
    return icon


def progress_pct(state: SyncState, facts: Facts) -> int:
    """Percent complete for the Activity Center bar, or ``-1``.

    ``-1`` means indeterminate or not applicable, which is most of the time:
    only ``SYNCING`` with a known queue depth has a denominator worth drawing.
    An indeterminate bar is honest; a bar computed from a denominator that grows
    as the scan discovers more files runs backwards, which is not.
    """
    if state is not SyncState.SYNCING or facts.uploads_queued <= 0:
        # An empty queue is not "100 % done", it is "the denominator is
        # unknown". Drawing a full bar over a transfer that is still running is
        # the single most reliable way to be accused of lying about progress.
        return -1
    done = facts.transfers_active
    return int(done / (done + facts.uploads_queued) * 100)


# ═════════════════════════════════════════════════════════════════════════════
# Transition effects
# ═════════════════════════════════════════════════════════════════════════════

def transition_effects(old: SyncState, new: SyncState, facts: Facts) -> list[str]:
    """What must *happen* when the state changes, as a list of effect names.

    Declarative on purpose (ARCHITECTURE §6.6). The reducer says "toast, then
    start deferring the queue"; the Supervisor is the only thing that does it.
    That is what lets a test assert the *policy* — entering PAUSED_QUOTA defers
    uploads and leaves downloads alone — with no daemon anywhere in sight.

    Args:
        old: The state being left. Equal to `new` on the first tick after start,
            where there is nothing to leave.
        new: The state being entered.
        facts: The observation that produced `new`, for the cases where the
            effect depends on *why* — a stale mount needs unmounting, a merely
            absent one does not.

    Returns:
        Effect names from :class:`EFFECT`, in execution order. Empty when
        `old is new`.
    """
    if old is new:
        return []

    effects: list[str] = []

    # A dead FUSE mount is the one transition that must act before it talks:
    # every read against it blocks in the kernel until it is torn down, so a
    # toast raised first would sit behind a frozen file manager.
    if new is SyncState.ERROR and facts.mount is MountHealth.STALE:
        effects += [EFFECT.MOUNT_FORCE_UNMOUNT, EFFECT.MOUNT_RESTART,
                    EFFECT.TOAST_MOUNT_LOST]

    if new in (SyncState.PAUSED_METERED, SyncState.PAUSED_BATTERY):
        # The toast carries "Sync Anyway", which is the whole point of an
        # automatic pause being visible rather than silent.
        effects += [EFFECT.TOAST_PAUSED, EFFECT.PAUSE_ENFORCE]

    elif new is SyncState.PAUSED_MANUAL:
        effects += [EFFECT.PAUSE_ENFORCE]

    elif new is SyncState.PAUSED_QUOTA:
        # Uploads only. Downloads are left alone deliberately: a full drive can
        # still hydrate a file the user double-clicks, and blocking that would
        # make a storage problem look like a broken client.
        effects += [EFFECT.TOAST_QUOTA_FULL, EFFECT.BANNER_QUOTA,
                    EFFECT.PAUSE_ENFORCE_UPLOADS]

    elif new is SyncState.AUTH_REQUIRED:
        # The mount is NOT unmounted. Cached reads keep working while signed
        # out, which is what Windows does and what makes an expired token an
        # inconvenience rather than a disappearance of every file.
        effects += [EFFECT.TOAST_SIGN_IN, EFFECT.JOBS_SUSPEND]

    elif new is SyncState.ACCOUNT_BLOCKED:
        effects += [EFFECT.TOAST_ACCOUNT_BLOCKED, EFFECT.JOBS_SUSPEND]

    elif new is SyncState.NEEDS_ATTENTION:
        effects += [EFFECT.DIALOG_DECISION, EFFECT.TOAST_DECISION]

    elif new is SyncState.WARNING:
        effects += [EFFECT.TOAST_SYNC_ISSUES]

    elif new is SyncState.UP_TO_DATE:
        if old is SyncState.SYNCING:
            # Drain BEFORE reset, always: `core/stats-reset` also wipes
            # `core/transferred`, so resetting first destroys the only record
            # that those transfers happened. The order here is asserted.
            effects += [EFFECT.STATS_DRAIN, EFFECT.STATS_RESET,
                        EFFECT.TOAST_SYNC_COMPLETE]
        if old is SyncState.ERROR and facts.mount is MountHealth.UP:
            effects += [EFFECT.TOAST_MOUNT_RESTORED]

    # Leaving a pause releases the deferred queue, whatever we are going to.
    if old in (SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
               SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA):
        if new not in (SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
                       SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA):
            effects.append(EFFECT.PAUSE_RELEASE)

    if old in (SyncState.AUTH_REQUIRED, SyncState.ACCOUNT_BLOCKED):
        effects.append(EFFECT.JOBS_RESUME)

    # Every state change moves at least one file's emblem, and Nautilus caches
    # what we told it last time until we say otherwise.
    effects.append(EFFECT.IPC_INVALIDATE)
    return effects
