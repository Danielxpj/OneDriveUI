"""Pause, against a filesystem that has no pause button.

rclone has no notion of pausing. Three obvious approaches all fail, and knowing
why is the whole design of this module:

* **``core/bwlimit`` does not stop uploads**, it slows them. A "paused" client
  that is still trickling a 4 GB video over a metered connection is worse than
  one that never claimed to pause.
* **Stopping the jobs does nothing.** The mount's ``--vfs-write-back`` queue
  uploads on its own timer, entirely outside job control. Cancel every job and
  the queue keeps draining.
* **Unmounting works and is unacceptable.** It stops uploads by taking every
  file away — including the cached ones the user can currently open offline.
  Windows' pause does not do that, and neither does this one. **Pausing never
  unmounts.**

What actually stops bytes leaving the machine is pushing every queued item's
expiry past the pause deadline, once per tick. The write-back timer then simply
never fires, the files stay ``Dirty`` and fully materialised on disk, and
resuming flushes the queue and picks up precisely where it left off. Nothing is
lost, nothing is re-uploaded, and no file becomes unreadable.

One consequence is user-visible and is stated rather than hidden: **a file that
has already started uploading finishes.** rclone documents that changing the
expiry of an item that has begun has no effect, and there is no way to interrupt
it without discarding the transfer. The UI says so.

Two kinds of pause live here and they behave differently on purpose:

* **Manual** is the user's own choice — 2, 8 or 24 hours, or "Until I resume".
  It has a deadline, it survives a restart, and nothing but the user or that
  deadline ends it.
* **Automatic** (metered, battery saver) has **no timeout at all**. It lasts
  exactly as long as the condition does, because a metered pause that expired
  after two hours would resume a large upload on the user's phone tethering.
  "Sync Anyway" overrides one reason for a window without disabling the policy.

The deadline is persisted in SQLite rather than in ``config.json``, so that a
hand-edited or restored config file cannot resurrect or extend a pause the user
has already ended.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.bus import BUS
from onedriveui.data import repo_files
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import AccountInfo, PauseReason, RcEndpoint, parse_iso
from onedriveui.rc import vfs

log = logging.getLogger(__name__)

__all__ = ["PauseManager", "PAUSE_DURATIONS", "DEFER_HORIZON_S", "KV_KEYS"]

#: The tray submenu, in Microsoft's order. ``None`` hours is "Until I resume",
#: which has no deadline by design — the user ends it.
PAUSE_DURATIONS: Final[tuple[tuple[int | None, str], ...]] = (
    (2, "2 hours"), (8, "8 hours"), (24, "24 hours"), (None, "Until I resume"),
)

#: How far ahead each queued item's expiry is pushed on every enforcing tick.
#: Deliberately *not* "until the deadline": the expiry is set absolutely, so
#: re-pinning it a fixed distance ahead every tick means that if this process
#: dies the queue drains within this many seconds rather than being frozen for
#: the twenty-three hours the user asked for and then forgotten about.
DEFER_HORIZON_S: Final = 900.0


class KV_KEYS:
    """Where the live pause state is kept, in the ``kv`` table.

    Not in ``config.json``: the config file holds *policy* (should we pause on a
    metered connection?) and this holds *fact* (we are paused until 14:03). A
    user who restores an old config, or edits one by hand, must not be able to
    reinstate a pause they already ended — and, more importantly, must not be
    able to accidentally lengthen one.
    """

    REASON = "pause.reason"
    UNTIL = "pause.until"
    SET_AT = "pause.set_at"
    OVERRIDE = "pause.override"          # {reason: iso deadline}


class PauseManager(QObject):
    """Owns the pause state, its persistence, and its enforcement.

    Args:
        account: The account being paused. Pauses are per account, because two
            accounts on one machine can be in different situations.
        writer: The database writer. State changes are urgent — a pause that a
            crash loses is a pause that silently resumed.
        config_get: ``(dotted_key, default) -> value``, for the two policy
            toggles (``pause.on_metered``, ``pause.on_battery_saver``). Injected
            so this module does not have to own a config file handle.
        now: The clock, injected for tests.
        parent: Qt parent.

    Signals:
        changed: ``(reason, until)`` whenever the pause state moves. Mirrors
            :data:`~onedriveui.bus.BUS.pause_changed`.
    """

    changed = Signal(PauseReason, object)

    PAUSE_DURATIONS: Final = PAUSE_DURATIONS

    def __init__(
        self,
        account: AccountInfo,
        *,
        writer: Any = None,
        config_get: Any = None,
        now: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._writer = writer
        self._config_get = config_get or (lambda key, default=None: default)
        self._now = now or (lambda: _dt.datetime.now(_dt.UTC))

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_deadline)

        self._reason = PauseReason.NONE
        self._until: _dt.datetime | None = None
        self._overrides: dict[str, str] = {}
        self._load()

    # ═════════════════════════════════════════════════════════════════════════
    # Persistence
    # ═════════════════════════════════════════════════════════════════════════

    def _load(self) -> None:
        """Restore the pause across a restart, and re-arm its timer.

        A user who pauses for eight hours and reboots is still paused. Without
        this the client would come back up syncing, which is both surprising and
        — on a metered connection they were avoiding — expensive.
        """
        reason = repo_files.kv_get(KV_KEYS.REASON, PauseReason.NONE.value,
                                   account_id=self.account.id)
        try:
            self._reason = PauseReason(str(reason))
        except ValueError:
            log.warning("ignoring an unknown persisted pause reason %r", reason)
            self._reason = PauseReason.NONE
        self._until = parse_iso(repo_files.kv_get(KV_KEYS.UNTIL, None,
                                                  account_id=self.account.id))
        stored = repo_files.kv_get(KV_KEYS.OVERRIDE, {}, account_id=self.account.id)
        self._overrides = dict(stored) if isinstance(stored, dict) else {}

        if self._reason is not PauseReason.NONE and self._expired():
            log.info("the persisted pause for %s expired while we were not "
                     "running; resuming", self.account.id)
            self.resume()
            return
        self._rearm()

    def _persist(self) -> None:
        until = self._until.isoformat().replace("+00:00", "Z") if self._until else None
        repo_files.kv_set(KV_KEYS.REASON, self._reason.value,
                          account_id=self.account.id, writer=self._writer)
        repo_files.kv_set(KV_KEYS.UNTIL, until,
                          account_id=self.account.id, writer=self._writer)
        repo_files.kv_set(KV_KEYS.SET_AT,
                          self._now().isoformat().replace("+00:00", "Z"),
                          account_id=self.account.id, writer=self._writer)

    def _persist_overrides(self) -> None:
        repo_files.kv_set(KV_KEYS.OVERRIDE, dict(self._overrides),
                          account_id=self.account.id, writer=self._writer)

    # ═════════════════════════════════════════════════════════════════════════
    # The pause itself
    # ═════════════════════════════════════════════════════════════════════════

    def pause(self, reason: PauseReason, hours: int | None = None) -> None:
        """Pause syncing. **Never unmounts.**

        Args:
            reason: ``MANUAL`` for the user's own choice; the policy reasons are
                normally set by :meth:`policy_pause` rather than called directly.
            hours: 2, 8 or 24 for a manual pause, or ``None`` for "Until I
                resume". **Ignored for the automatic reasons**, which have no
                deadline at all: a metered pause that expired after two hours
                would resume a large upload over the connection the user was
                avoiding, which is the exact harm the toggle exists to prevent.
        """
        if reason is PauseReason.NONE:
            self.resume()
            return

        self._reason = reason
        if reason is PauseReason.MANUAL and hours:
            self._until = self._now() + _dt.timedelta(hours=hours)
        else:
            self._until = None
        self._persist()
        self._rearm()
        log.info("paused %s (%s) until %s", self.account.id, reason.value,
                 self._until.isoformat() if self._until else "the user resumes")
        self._emit()

    def resume(self, reason: PauseReason | None = None) -> None:
        """Resume syncing, and flush the deferred queue.

        Args:
            reason: Resume only if this is the active reason. Used by the
                automatic pauses, where the metered condition clearing must not
                also cancel a manual pause the user set while it was in force.
        """
        if reason is not None and self._reason is not reason:
            return
        self._reason = PauseReason.NONE
        self._until = None
        self._timer.stop()
        self._persist()
        log.info("resumed %s", self.account.id)
        self._emit()

    def sync_anyway(self, reason: PauseReason, hours: int = 8) -> None:
        """The "Sync Anyway" toast button: override one policy pause.

        This does **not** turn the policy off. The user is saying "not right
        now" about one occasion, not "never ask again about metered
        connections", and conflating the two would quietly disable a safeguard
        from a toast button.

        Args:
            reason: Which automatic pause to override.
            hours: How long the override lasts. After it, the same condition
                pauses again — which is correct: the user is still on a metered
                connection, and they can press it again.
        """
        if reason in (PauseReason.NONE, PauseReason.MANUAL):
            log.warning("sync_anyway(%s) is only for the automatic pauses",
                        reason.value)
            return
        deadline = self._now() + _dt.timedelta(hours=hours)
        self._overrides[reason.value] = deadline.isoformat().replace("+00:00", "Z")
        self._persist_overrides()
        log.info("overriding the %s pause for %s until %s",
                 reason.value, self.account.id, deadline.isoformat())
        if self._reason is reason:
            self.resume(reason)

    # ── reads ───────────────────────────────────────────────────────────────
    def active(self) -> PauseReason:
        """The reason currently in force, or ``NONE``."""
        if self._reason is not PauseReason.NONE and self._expired():
            self.resume()
        return self._reason

    def until(self) -> _dt.datetime | None:
        """When it ends, or ``None`` for "Until I resume" and the automatic ones."""
        return self._until

    def overridden(self, reason: PauseReason) -> bool:
        """Has "Sync Anyway" been pressed for this reason, and is it still live?"""
        deadline = parse_iso(self._overrides.get(getattr(reason, "value", "")))
        return deadline is not None and self._now() < deadline

    def policy_pause(self, *, metered: bool = False, battery: bool = False,
                     quota_full: bool = False) -> PauseReason:
        """Which automatic pause the environment calls for, honouring the toggles.

        Metered outranks battery saver, matching the ladder in ARCHITECTURE §6.3
        where ``PAUSED_METERED`` sits above ``PAUSED_BATTERY``. A full quota
        outranks both, because neither of the others would let an upload through
        anyway and "your OneDrive is full" is the actionable message.

        Args:
            metered: The connection is metered.
            battery: Battery saver is on.
            quota_full: The drive is full.

        Returns:
            The reason, or ``NONE``.
        """
        if quota_full:
            return PauseReason.QUOTA
        if (metered and self._config_get("pause.on_metered", True)
                and not self.overridden(PauseReason.METERED)):
            return PauseReason.METERED
        if (battery and self._config_get("pause.on_battery_saver", True)
                and not self.overridden(PauseReason.BATTERY)):
            return PauseReason.BATTERY
        return PauseReason.NONE

    # ═════════════════════════════════════════════════════════════════════════
    # Enforcement
    # ═════════════════════════════════════════════════════════════════════════

    def enforce(self, ep: RcEndpoint | None, *,
                reason: PauseReason | None = None) -> int:
        """Push every queued upload past the horizon. Called **every tick** while paused.

        Every tick, not once, because ``--vfs-write-back`` keeps adding items: a
        file saved during the pause joins the queue with its own five-second
        expiry and would upload immediately if nothing re-deferred it.

        The expiry is set absolutely rather than relatively (verified against
        rclone v1.75.0), so re-pinning it each tick holds the deadline steady
        instead of compounding into an unreachable future.

        Args:
            ep: The **mount's** rc endpoint — the daemon with the VFS. The
                control plane has no queue at all. ``None`` is a no-op.

        Returns:
            How many items were deferred. Items already uploading are skipped
            and not counted: rclone ignores an expiry change on an item that has
            started, and counting it would make the UI claim a pause that did
            not happen.
        """
        # `reason` overrides `active()`, which only ever reflects a *manual*
        # pause: `_reason` is set by `pause()` and nothing else, while the
        # metered, battery-saver and quota pauses are derived fresh from the
        # environment on every tick and never recorded here. Gating on
        # `active()` alone meant all three automatic pauses deferred nothing at
        # all — the state said "Paused", and the upload queue drained anyway.
        effective = reason if reason is not None else self.active()
        if ep is None or effective is PauseReason.NONE:
            return 0
        try:
            deferred = vfs.defer_uploads(ep, DEFER_HORIZON_S)
        except (RcError, DaemonUnavailable, OSError):
            # A daemon that cannot be reached is not uploading either, so this
            # is a delay rather than a failure of the pause.
            log.warning("could not defer the upload queue for %s",
                        self.account.id, exc_info=True)
            return 0
        if deferred:
            log.debug("deferred %d queued uploads for %s", deferred, self.account.id)
        return deferred

    def release(self, ep: RcEndpoint | None = None) -> int:
        """Flush the deferred queue on resume, rather than waiting out the horizon.

        Args:
            ep: The mount's rc endpoint.

        Returns:
            How many items were released.
        """
        if ep is None:
            return 0
        released = 0
        try:
            for item in vfs.queue(ep):
                if getattr(item, "uploading", False):
                    continue
                vfs.force_upload_now(ep, item.id)
                released += 1
        except (RcError, DaemonUnavailable, OSError):
            # The horizon expires on its own, so the queue drains regardless;
            # this only makes it immediate.
            log.warning("could not flush the upload queue for %s",
                        self.account.id, exc_info=True)
        return released

    # ═════════════════════════════════════════════════════════════════════════
    # Internals
    # ═════════════════════════════════════════════════════════════════════════

    def _expired(self) -> bool:
        return self._until is not None and self._now() >= self._until

    def _rearm(self) -> None:
        """Arm a single-shot timer for the deadline.

        Qt's timers take a 32-bit millisecond count, so a 24-hour pause is
        clamped rather than overflowed; :meth:`active` re-checks the deadline on
        every read, so an early wake-up simply re-arms.
        """
        self._timer.stop()
        if self._until is None:
            return
        remaining = (self._until - self._now()).total_seconds()
        if remaining <= 0:
            self._on_deadline()
            return
        self._timer.start(int(min(remaining, 3600.0) * 1000))

    def _on_deadline(self) -> None:
        if self._expired():
            log.info("the pause deadline for %s arrived", self.account.id)
            self.resume()
        else:
            self._rearm()

    def _emit(self) -> None:
        self.changed.emit(self._reason, self._until)
        BUS.pause_changed.emit(self._reason, self._until)
