"""The async job registry: one stable ``_group`` per user-visible operation.

Every rc call — synchronous ones included — allocates a job id and a stats
group. Without an explicit ``_group`` rclone invents ``job/<id>``, so
``core/group-list`` fills with noise and there is no stable key to read progress
under. ``MaxStatsGroups`` caps the map at **1000** and evicts the oldest, so the
vocabulary of group names must be small and stable: one per *user-visible
operation*, never one per file. :func:`group_for` is the only place a group name
is minted.

What this module adds over :class:`~onedriveui.rc.client.JobWatcher`, which
follows exactly one job:

* **A registry.** ``start()`` fires an ``_async`` call and returns a *ticket*
  immediately, because the job id does not exist until the daemon answers. The
  ticket can be stopped during that window, and the race is handled: a job that
  materialises after a cancel is stopped the moment its id arrives.
* **``executeId`` invalidation.** A changed ``executeId`` means the daemon
  restarted: every job id, mount, VFS and byte of transfer history is gone.
  :meth:`JobRegistry.invalidate_all` drops every handle and reports ``lost`` for
  each, and it is wired to ``BUS.daemon_restarted`` so nothing has to remember
  to call it.
* **``core/stats-delete`` cleanup.** A group survives its jobs until it is
  deleted, so the last job out of a group turns the light off. Deleting a group
  also discards that group's ``core/transferred`` rows, which is why the
  ``before_cleanup`` hook exists and why ``StatsPoller.drain_group`` must run
  through it first.

Threading (ARCHITECTURE §7.1): this lives on the GUI thread and never blocks it.
Every call goes through :class:`~onedriveui.rc.client.RcClient`, which is
asynchronous; nothing here touches ``call_blocking``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.errors import RcError
from onedriveui.models import JobHandle, utcnow_iso

__all__ = [
    "GROUP_KINDS",
    "GROUP_ROOT",
    "JobRegistry",
    "group_for",
    "is_ours",
    "split_group",
]

log = logging.getLogger(__name__)

#: Every group this application creates starts here, so a group belonging to
#: some other rclone user of the same daemon is never deleted by our cleanup.
GROUP_ROOT: Final[str] = "onedriveui"

#: The user-visible operations that get a group. This is documentation and a
#: sanity check, not a closed set — a caller may mint another kind — but the
#: vocabulary must stay bounded, because ``MaxStatsGroups`` is 1000.
GROUP_KINDS: Final[tuple[str, ...]] = (
    "pin",        # "Always keep on this device" — hydrating a tree
    "free",       # "Free up space" — evicting a tree
    "copy",       # a folder copy through sync/copy
    "move",       # a folder move
    "upload",     # explicit uploads through operations/uploadfile
    "download",   # explicit downloads
    "delete",     # a purge or a bulk delete
    "size",       # operations/size on a folder
    "check",      # operations/check — the "what is out of date" panel
    "bisync",     # the opt-in Offline folder
    "kfm",        # a known-folder move
    "verify",     # the weekly verification pass
    "share",      # publiclink and friends
    "restore",    # a version or trash restore
)

#: Group names go into HTTP bodies and into our own logs, and are compared for
#: equality across restarts. Anything outside this set is replaced with ``_`` so
#: the name a caller passes can never change shape between runs.
_UNSAFE_GROUP_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _clean(part: str) -> str:
    """One group-name segment, reduced to a stable character set."""
    return _UNSAFE_GROUP_CHARS.sub("_", str(part or "")).strip("_")


def group_for(kind: str, account_id: str = "", detail: str = "") -> str:
    """Mint the stable ``_group`` for one user-visible operation.

    Args:
        kind: What the user asked for — one of :data:`GROUP_KINDS`, or another
            bounded verb. An unknown kind is accepted and logged, because a
            later work package may legitimately add one.
        account_id: Which account it belongs to. Included so two accounts
            syncing at once keep separate progress.
        detail: An optional bounded qualifier — a configured folder name, for
            instance. **Never a file name**: one group per file would evict the
            whole map in 1000 files and make progress unreadable.

    Returns:
        ``"onedriveui/<kind>/<account_id>[/<detail>]"``, with every segment
        reduced to ``[A-Za-z0-9._-]``.

    Stability is the point. ``core/stats {"group": …}`` is the only way to read
    progress, ``job/stopgroup {"group": …}`` is how Pause cancels an operation,
    and both need the name a previous tick used.
    """
    if kind not in GROUP_KINDS:
        log.debug("minting a group for the unlisted kind %r", kind)
    parts = [GROUP_ROOT, _clean(kind) or "job"]
    if account_id:
        parts.append(_clean(account_id))
    if detail:
        parts.append(_clean(detail))
    return "/".join(parts)


def is_ours(group: str) -> bool:
    """Did :func:`group_for` mint this group?

    Cleanup and cancellation are both destructive to whoever owns a group, and
    a shared daemon can carry groups we did not create, so every such action is
    gated on this.
    """
    return str(group or "").startswith(f"{GROUP_ROOT}/")


def split_group(group: str) -> tuple[str, str, str]:
    """Take a group name apart again.

    Args:
        group: A name from :func:`group_for`.

    Returns:
        ``(kind, account_id, detail)``. Empty strings for the parts a name did
        not carry, and ``("", "", "")`` for a group that is not ours.
    """
    if not is_ours(group):
        return ("", "", "")
    parts = str(group).split("/")[1:]
    parts += [""] * (3 - len(parts))
    return (parts[0], parts[1], "/".join(p for p in parts[2:] if p))


# ─────────────────────────────────────────────────────────────────────────────
# Bookkeeping for one outstanding job
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class _Entry:
    """One job between ``start()`` and its terminal signal.

    Deliberately mutable and deliberately private: it is scratch state, not a
    value that crosses a module boundary.
    """

    ticket: str
    path: str
    group: str
    label: str
    started_at: str
    call: Any = None                 # RcCall, until the daemon answers
    handle: JobHandle | None = None
    watcher: Any = None              # JobWatcher, once there is a job id
    cancelled: bool = False
    connections: tuple[Any, ...] = field(default_factory=tuple)


# ─────────────────────────────────────────────────────────────────────────────
# The registry
# ─────────────────────────────────────────────────────────────────────────────

class JobRegistry(QObject):
    """Track every ``_async`` job this application has outstanding.

    Attributes:
        started: ``(JobHandle)`` — the daemon answered with a job id.
        finished: ``(JobHandle, dict)`` — ``job/status`` reported ``finished``;
            the dict is the whole status object, ``output`` included. It is
            captured the instant it arrives, because a finished job is
            garbage-collected after ``--rc-job-expire-duration`` and going back
            for it later fails.
        failed: ``(JobHandle | None, object)`` — a real error. The handle is
            ``None`` when the ``_async`` call itself never got a job id.
        expired: ``(JobHandle)`` — ``job not found`` with an **unchanged**
            ``executeId``. The job did finish; its outcome is unknowable. The
            activity row is ``interrupted``, never ``error``.
        lost: ``(JobHandle)`` — the ``executeId`` **changed**. The daemon
            restarted and every id is stale.
        group_emptied: ``(str)`` — the group's last job left.
    """

    started = Signal(object)
    finished = Signal(object, dict)
    failed = Signal(object, object)
    expired = Signal(object)
    lost = Signal(object)
    group_emptied = Signal(str)

    def __init__(self, client: Any, *, poll_ms: int = 500,
                 cleanup_groups: bool = True,
                 before_cleanup: Callable[[str], None] | None = None,
                 watch_bus: bool = True,
                 parent: QObject | None = None) -> None:
        """
        Args:
            client: The :class:`~onedriveui.rc.client.RcClient` to drive. Not
                owned; the registry never closes it.
            poll_ms: How often each job's ``job/status`` is polled. 500 ms is
                well inside the 10-minute ``--rc-job-expire-duration`` this
                application configures, and far inside rclone's own 60 s.
            cleanup_groups: Send ``core/stats-delete`` when a group's last job
                leaves, so ``core/group-list`` stays readable.
            before_cleanup: Called with the group name immediately before that
                ``core/stats-delete``. **Wire ``StatsPoller.drain_group`` here.**
                Deleting a group also discards its ``core/transferred`` rows,
                and those rows are the only record that the transfers happened.
            watch_bus: Invalidate every handle when ``BUS.daemon_restarted``
                fires. On by default: forgetting it means polling job ids that
                belong to a dead process.
            parent: Qt parent.
        """
        super().__init__(parent)
        self._client = client
        self._poll_ms = max(1, int(poll_ms))
        self._cleanup_groups = bool(cleanup_groups)
        self._before_cleanup = before_cleanup
        self._entries: dict[str, _Entry] = {}
        self._next_ticket = 1
        self._closed = False
        self._watching_bus = bool(watch_bus)
        if self._watching_bus:
            BUS.daemon_restarted.connect(self._on_daemon_restarted)

    # ── introspection ───────────────────────────────────────────────────────

    def __len__(self) -> int:
        """How many jobs are outstanding, started or merely requested."""
        return len(self._entries)

    def active(self) -> list[JobHandle]:
        """Every job that has a real job id, oldest ticket first."""
        return [entry.handle for entry in self._ordered()
                if entry.handle is not None]

    def pending(self) -> list[str]:
        """Tickets whose ``_async`` reply has not arrived yet."""
        return [entry.ticket for entry in self._ordered()
                if entry.handle is None]

    def groups(self) -> list[str]:
        """The distinct groups with at least one outstanding job."""
        seen: list[str] = []
        for entry in self._ordered():
            if entry.group and entry.group not in seen:
                seen.append(entry.group)
        return seen

    def handle_for(self, ticket: str) -> JobHandle | None:
        """The handle a ticket became, or ``None`` while it is still pending."""
        entry = self._entries.get(ticket)
        return entry.handle if entry is not None else None

    def _ordered(self) -> list[_Entry]:
        return [self._entries[key] for key in sorted(
            self._entries, key=lambda t: int(t.lstrip("t") or 0))]

    # ── starting ────────────────────────────────────────────────────────────

    def start(self, path: str, params: Mapping[str, Any] | None = None, *,
              group: str, label: str = "",
              config: Mapping[str, Any] | None = None,
              filt: Mapping[str, Any] | None = None,
              poll_ms: int | None = None) -> str:
        """Fire one ``_async`` rc call and follow it to its end.

        Args:
            path: The rc command path, e.g. ``"sync/copy"``.
            params: The endpoint's own parameters.
            group: The stats group, from :func:`group_for`. Required: a job
                without one is invisible to ``core/stats`` and uncancellable by
                ``job/stopgroup``.
            label: A human label carried on the resulting handle.
            config: ``_config`` overrides, by internal Go field name.
            filt: ``_filter`` rules, by internal filter name.
            poll_ms: Override the registry's poll interval for this job.

        Returns:
            A ticket. It is valid immediately — :meth:`stop` accepts it before
            the daemon has answered — and becomes a
            :class:`~onedriveui.models.JobHandle` when ``started`` fires.

        Raises:
            ValueError: ``group`` is empty.
            SafetyRefusal: ``path`` is banned by I7, I8 or I14. Raised
                synchronously by the transport, because it is a caller bug.
        """
        if not group:
            raise ValueError(
                f"{path} needs a stable _group: without one rclone invents "
                f"job/<id>, which no later tick can find again")
        if self._closed:
            raise RuntimeError("JobRegistry is closed")

        ticket = f"t{self._next_ticket}"
        started_at = utcnow_iso()
        # The transport refuses a banned path synchronously (I7, I8, I14). The
        # call is therefore made BEFORE the entry is registered, so a refusal
        # cannot leave a ticket outstanding for a job that was never started.
        call = self._client.call(path, dict(params or {}), group=group,
                                 async_=True,
                                 config=dict(config) if config else None,
                                 filt=dict(filt) if filt else None)
        self._next_ticket += 1
        entry = _Entry(ticket=ticket, path=path, group=group, label=label,
                       started_at=started_at)
        self._entries[ticket] = entry
        on_ok = self._make_started(ticket, poll_ms)
        on_err = self._make_start_failed(ticket)
        call.succeeded.connect(on_ok)
        call.failed.connect(on_err)
        entry.call = call
        entry.connections = (on_ok, on_err)
        return ticket

    def _make_started(self, ticket: str,
                      poll_ms: int | None) -> Callable[[dict], None]:
        def handler(body: dict) -> None:
            self._on_started(ticket, body, poll_ms)
        return handler

    def _make_start_failed(self, ticket: str) -> Callable[[object], None]:
        def handler(error: object) -> None:
            self._on_start_failed(ticket, error)
        return handler

    def _on_started(self, ticket: str, body: Mapping[str, Any],
                    poll_ms: int | None) -> None:
        entry = self._entries.get(ticket)
        if entry is None:
            return
        entry.call = None
        entry.connections = ()
        if "jobid" not in body:
            # Not an async reply at all: the endpoint answered inline. Treat the
            # body as the output rather than pretending a job exists.
            log.debug("%s answered inline (no jobid); completing ticket %s",
                      entry.path, ticket)
            handle = JobHandle(job_id=0, execute_id=str(body.get("executeId", "")),
                               group=entry.group, path=entry.path,
                               label=entry.label, started_at=entry.started_at)
            entry.handle = handle
            self._retire(ticket)
            self.finished.emit(handle, {"finished": True, "success": True,
                                        "error": "", "output": dict(body)})
            return

        handle = JobHandle(
            job_id=int(body.get("jobid", 0)),
            execute_id=str(body.get("executeId", "")),
            group=entry.group, path=entry.path, label=entry.label,
            started_at=entry.started_at,
        )
        entry.handle = handle
        self.started.emit(handle)

        if entry.cancelled:
            # stop() was called while the request was in flight. The job exists
            # now, so it has to be stopped for real.
            log.info("job %s started after a cancel; stopping it",
                     handle.job_id)
            self._post("job/stop", {"jobid": handle.job_id})
            self._retire(ticket)
            return

        watcher = self._new_watcher()
        entry.watcher = watcher
        watcher.finished.connect(self._make_finished(ticket))
        watcher.failed.connect(self._make_failed(ticket))
        watcher.expired.connect(self._make_expired(ticket))
        watcher.lost.connect(self._make_lost(ticket))
        watcher.watch(handle, poll_ms=poll_ms or self._poll_ms)

    def _new_watcher(self) -> Any:
        """A fresh :class:`~onedriveui.rc.client.JobWatcher` on our client.

        Imported here rather than at module scope so a test can substitute the
        watcher by patching this one method, without touching the transport.
        """
        from onedriveui.rc.client import JobWatcher

        return JobWatcher(self._client, parent=self)

    def _on_start_failed(self, ticket: str, error: object) -> None:
        entry = self._entries.pop(ticket, None)
        if entry is None:
            return
        entry.call = None
        entry.connections = ()
        log.info("%s could not be started: %s", entry.path, error)
        self._maybe_cleanup(entry.group)
        self.failed.emit(None, error)

    # ── terminal outcomes ───────────────────────────────────────────────────

    def _make_finished(self, ticket: str) -> Callable[[dict], None]:
        def handler(status: dict) -> None:
            entry = self._entries.get(ticket)
            if entry is None or entry.handle is None:
                return
            handle = entry.handle
            self._retire(ticket)
            error = str(status.get("error") or "")
            if error and not status.get("success"):
                self.failed.emit(handle, RcError(handle.path, 500, {
                    "error": error, "input": {"jobid": handle.job_id},
                    "path": handle.path, "status": 500}))
                return
            self.finished.emit(handle, dict(status))
        return handler

    def _make_failed(self, ticket: str) -> Callable[[object], None]:
        def handler(error: object) -> None:
            entry = self._entries.get(ticket)
            if entry is None:
                return
            handle = entry.handle
            self._retire(ticket)
            self.failed.emit(handle, error)
        return handler

    def _make_expired(self, ticket: str) -> Callable[[], None]:
        def handler() -> None:
            entry = self._entries.get(ticket)
            if entry is None or entry.handle is None:
                return
            handle = entry.handle
            self._retire(ticket)
            log.info("job %s expired: it finished, but its outcome is no "
                     "longer knowable", handle.job_id)
            self.expired.emit(handle)
        return handler

    def _make_lost(self, ticket: str) -> Callable[[], None]:
        def handler() -> None:
            entry = self._entries.get(ticket)
            if entry is None or entry.handle is None:
                return
            handle = entry.handle
            self._retire(ticket)
            log.warning("job %s lost: the daemon restarted", handle.job_id)
            self.lost.emit(handle)
        return handler

    # ── stopping ────────────────────────────────────────────────────────────

    def stop(self, ticket: str) -> bool:
        """Cancel one job by ticket.

        Args:
            ticket: What :meth:`start` returned.

        Returns:
            True when something was cancelled.

        Safe in the window before the daemon has answered. The request is
        deliberately **not** aborted in that window: aborting the HTTP call
        would not stop a job the daemon had already started, so the entry stays
        connected, and ``job/stop`` is sent the instant the job id arrives.
        No terminal signal is emitted for a job stopped this way — the caller
        already knows.
        """
        entry = self._entries.get(ticket)
        if entry is None:
            return False
        entry.cancelled = True
        if entry.handle is not None:
            self._post("job/stop", {"jobid": entry.handle.job_id})
            self._retire(ticket)
        return True

    def stop_group(self, group: str) -> bool:
        """Cancel every job in ``group``. This is what "Pause sync" runs.

        Args:
            group: A group from :func:`group_for`.

        Returns:
            True when the request was sent.

        Raises:
            ValueError: ``group`` was not minted by :func:`group_for`. A shared
                daemon can carry jobs we did not start, and cancelling those
                would be sabotage.
        """
        if not is_ours(group):
            raise ValueError(
                f"refusing to stop {group!r}: only groups minted by "
                f"jobs.group_for() belong to this application")
        self._post("job/stopgroup", {"group": group})
        for entry in [e for e in self._ordered() if e.group == group]:
            entry.cancelled = True
            if entry.handle is not None:
                self._retire(entry.ticket)
        return True

    def invalidate_all(self, reason: str) -> list[JobHandle]:
        """Drop every handle, because they can no longer mean anything.

        Args:
            reason: Why — logged, and worth carrying into the activity rows the
                caller is about to mark ``interrupted``.

        Returns:
            The handles that were dropped, so the caller can reconcile them.

        Called on an ``executeId`` change. A restarted daemon has new job ids
        from 1, no ``core/transferred`` history, no groups and no VFS: every
        handle we hold now names a different job or none at all. ``lost`` is
        emitted for each, and no ``job/stop`` is sent — there is nothing there
        to stop.
        """
        entries = self._ordered()
        if entries:
            log.warning("invalidating %d job handle(s): %s", len(entries), reason)
        dropped: list[JobHandle] = []
        for entry in entries:
            self._detach_call(entry)
            self._detach_watcher(entry)
            self._entries.pop(entry.ticket, None)
            if entry.handle is not None:
                dropped.append(entry.handle)
        for handle in dropped:
            self.lost.emit(handle)
        return dropped

    def close(self) -> None:
        """Stop following everything and disconnect from the bus. Idempotent.

        No ``job/stop`` is sent: shutdown must not cancel a transfer the user
        started. The jobs keep running in the daemon and are re-observed on the
        next launch.
        """
        if self._closed:
            return
        self._closed = True
        if self._watching_bus:
            try:
                BUS.daemon_restarted.disconnect(self._on_daemon_restarted)
            except (RuntimeError, TypeError):        # pragma: no cover - Qt
                pass
            self._watching_bus = False
        for entry in self._ordered():
            self._detach_call(entry)
            self._detach_watcher(entry)
        self._entries.clear()

    # ── plumbing ────────────────────────────────────────────────────────────

    def _on_daemon_restarted(self, kind: str, execute_id: str) -> None:
        self.invalidate_all(
            f"the {kind} daemon restarted (executeId is now {execute_id})")

    def _post(self, path: str, params: Mapping[str, Any]) -> None:
        """Fire and forget one rc call, logging a failure rather than raising."""
        call = self._client.call(path, dict(params))
        call.failed.connect(
            lambda error, p=path: log.info("%s failed: %s", p, error))

    def _detach_call(self, entry: _Entry) -> None:
        call, entry.call = entry.call, None
        slots, entry.connections = entry.connections, ()
        if call is None:
            return
        if slots:
            try:
                call.succeeded.disconnect(slots[0])
                call.failed.disconnect(slots[1])
            except (RuntimeError, TypeError):        # pragma: no cover - Qt
                pass
        abort = getattr(call, "abort", None)
        if callable(abort):
            abort()

    def _detach_watcher(self, entry: _Entry) -> None:
        watcher, entry.watcher = entry.watcher, None
        if watcher is None:
            return
        watcher.stop()
        watcher.deleteLater()

    def _retire(self, ticket: str) -> None:
        """Remove one entry and, if its group is now empty, clean the group up."""
        entry = self._entries.pop(ticket, None)
        if entry is None:
            return
        self._detach_call(entry)
        self._detach_watcher(entry)
        self._maybe_cleanup(entry.group)

    def _maybe_cleanup(self, group: str) -> None:
        if not group or any(e.group == group for e in self._entries.values()):
            return
        self.group_emptied.emit(group)
        if not self._cleanup_groups or not is_ours(group):
            return
        if self._before_cleanup is not None:
            # core/stats-delete discards this group's core/transferred rows
            # along with its counters, and those rows are the only record that
            # the transfers ever happened. Drain first, always.
            try:
                self._before_cleanup(group)
            except Exception:                        # noqa: BLE001
                log.exception("before_cleanup hook failed for %s; NOT deleting "
                              "the group, so its transfers are not lost", group)
                return
        self._post("core/stats-delete", {"group": group})
