"""``core/stats`` and ``core/transferred``: the progress engine.

Two rc endpoints feed everything the Activity Center shows, and both of them
lie to naive code. Every quirk below was captured from rclone v1.75.0
(``docs/research/rclone-rc-api.md`` §3.7–3.9):

* **Absent means empty.** ``transferring``, ``checking`` and ``lastError`` are
  omitted from the response *entirely* when they have nothing in them, and
  ``short: true`` drops the first two as well. Every field is read with
  ``.get()``; :func:`parse_stats` on ``{}`` is a valid, empty
  :class:`~onedriveui.models.CoreStats`.
* **``eta`` is ``null`` when indeterminate** — present, but null. It is
  ``int | None`` all the way to the UI, never ``0``.
* **``checking`` is a list of plain strings** while ``transferring`` is a list
  of objects. There is no symmetry to exploit.
* **``transferring[]`` carries undocumented ``group``/``srcFs``/``dstFs``**,
  which is what makes a "from → to" line and per-account attribution possible.
* **``core/transferred``'s own help is stale.** It documents ``timestamp`` (ms
  epoch) and ``jobid``; v1.75.0 actually returns ``started_at`` /
  ``completed_at`` as RFC3339 strings plus ``group``, ``srcFs`` and ``dstFs``,
  and **no ``jobid`` at all**. :func:`transferred_events` codes against what the
  daemon really sends.
* **``core/transferred`` keeps only the last 100 rows**, re-reports them on
  every poll, and loses the lot when the daemon restarts. It is a window, not a
  log: rows are persisted into the ``activity`` table the moment they are seen,
  deduplicated by ``sha1(group|name|completed_at)``.
* **Global ``core/stats`` accumulates for the whole process lifetime.** Always
  poll with a ``group``.

And one rule that is a safety property rather than a quirk: **``core/stats-reset``
is never called implicitly.** It clears the group's counters *and* wipes
``core/transferred``, so a reset before a drain destroys the only record that
those transfers happened. :func:`reset_group` drains first, in code, and the
poller never resets anything at all.

Threading (ARCHITECTURE §7.1/§7.6). :class:`StatsPoller` lives on the GUI thread
and uses only the asynchronous :class:`~onedriveui.rc.client.RcClient`.
:func:`drain_transferred` and :func:`reset_group` are blocking and belong on an
``IOPool`` worker. The parsers are pure and safe anywhere.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.bus import BUS
from onedriveui.constants import TICK_ACTIVE_MS, TICK_IDLE_MS, TICK_PAUSED_MS
from onedriveui.data import repo_sync
from onedriveui.data.writer import DbWriter
from onedriveui.errors import DaemonUnavailable, RcError, classify, is_benign
from onedriveui.models import (
    ActivityEvent,
    ActivityState,
    ActivityVerb,
    CoreStats,
    RcEndpoint,
    TransferInfo,
    utcnow_iso,
)
from onedriveui.rc.client import call_blocking

__all__ = [
    "SEEN_CAP",
    "StatsPoller",
    "dedupe_key",
    "direction_for",
    "drain_transferred",
    "parse_stats",
    "parse_transfer",
    "persist_events",
    "reset_group",
    "transferred_events",
    "verb_for",
]

log = logging.getLogger(__name__)

#: How many ``core/transferred`` rows a poller remembers as already seen. The
#: endpoint returns at most 100 and re-reports them on every poll, so five times
#: that is ample and the set can never grow without bound.
SEEN_CAP: Final[int] = 500

#: ``what`` values ``core/transferred`` uses, mapped onto the activity verb the
#: user sees. ``transferring`` is resolved by direction instead.
_VERB_FOR_WHAT: Final[dict[str, ActivityVerb]] = {
    "deleting": ActivityVerb.DELETED,
    "moving": ActivityVerb.MOVED,
    "renaming": ActivityVerb.RENAMED,
}

#: ``what`` values that describe work rather than a change to a file. They never
#: become an activity row: a checked file that did not move is not an event.
_NON_EVENT_WHAT: Final[frozenset[str]] = frozenset({
    "checking", "hashing", "listing", "importing", "merging",
})


# ─────────────────────────────────────────────────────────────────────────────
# Coercion — every field is optional, so nothing is ever indexed directly
# ─────────────────────────────────────────────────────────────────────────────

def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _eta(value: Any) -> int | None:
    """``eta`` is seconds, or ``null`` when rclone cannot work it out."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str:
    return "" if value is None else str(value)


# ─────────────────────────────────────────────────────────────────────────────
# core/stats
# ─────────────────────────────────────────────────────────────────────────────

def parse_transfer(row: Mapping[str, Any] | None) -> TransferInfo:
    """One ``core/stats.transferring[]`` entry → :class:`TransferInfo`.

    Args:
        row: The entry. ``size`` may be ``-1`` when the length is unknown and
            ``eta`` may be ``null``; both survive unchanged.

    Returns:
        The transfer. ``group``, ``src_fs`` and ``dst_fs`` are undocumented but
        present in v1.75.0, and are what
        :attr:`~onedriveui.models.TransferInfo.is_upload` reads.
    """
    data = row or {}
    return TransferInfo(
        name=_str(data.get("name")),
        size=_int(data.get("size")),
        bytes=_int(data.get("bytes")),
        percentage=_int(data.get("percentage")),
        speed=_float(data.get("speed")),
        speed_avg=_float(data.get("speedAvg")),
        eta=_eta(data.get("eta")),
        group=_str(data.get("group")),
        src_fs=_str(data.get("srcFs")),
        dst_fs=_str(data.get("dstFs")),
    )


def parse_stats(body: Mapping[str, Any] | None) -> CoreStats:
    """``core/stats`` → :class:`~onedriveui.models.CoreStats`. Never raises.

    Args:
        body: The response, or ``None``/``{}``.

    Returns:
        The stats. ``transferring`` and ``checking`` come back as empty tuples
        when the keys were absent — which is how rclone says "nothing is
        happening" — and ``last_error`` is ``""`` unless ``errors > 0``.

    ``parse_stats({})`` is the idle case and is deliberately not an error: a
    daemon that has done nothing since it started answers exactly that.
    """
    data = body or {}
    rows = data.get("transferring")
    transferring = tuple(
        parse_transfer(row) for row in rows
        if isinstance(row, Mapping)) if isinstance(rows, (list, tuple)) else ()
    names = data.get("checking")
    checking = tuple(
        str(name) for name in names) if isinstance(names, (list, tuple)) else ()
    return CoreStats(
        bytes=_int(data.get("bytes")),
        total_bytes=_int(data.get("totalBytes")),
        speed=_float(data.get("speed")),
        eta=_eta(data.get("eta")),
        errors=_int(data.get("errors")),
        last_error=_str(data.get("lastError")),
        fatal_error=bool(data.get("fatalError")),
        retry_error=bool(data.get("retryError")),
        checks=_int(data.get("checks")),
        total_checks=_int(data.get("totalChecks")),
        transfers=_int(data.get("transfers")),
        total_transfers=_int(data.get("totalTransfers")),
        deletes=_int(data.get("deletes")),
        renames=_int(data.get("renames")),
        elapsed_time=_float(data.get("elapsedTime")),
        transferring=transferring,
        checking=checking,
    )


# ─────────────────────────────────────────────────────────────────────────────
# core/transferred
# ─────────────────────────────────────────────────────────────────────────────

def _looks_remote(fs: str) -> bool:
    """Is this fs string a remote rather than a local path?

    The same rule :attr:`onedriveui.models.TransferInfo.is_upload` uses: a
    remote carries a colon and does not start with ``/``.
    """
    return ":" in fs and not fs.startswith("/")


def direction_for(src_fs: str, dst_fs: str) -> str:
    """Which way the bytes went.

    Args:
        src_fs: ``srcFs`` from the row.
        dst_fs: ``dstFs`` from the row.

    Returns:
        ``"up"`` when the destination is a remote, ``"down"`` when the source
        is, and ``"local"`` when neither is (a local-to-local copy, which the
        offline folder can produce).
    """
    if _looks_remote(dst_fs):
        return "up"
    if _looks_remote(src_fs):
        return "down"
    return "local"


def verb_for(what: str, direction: str) -> ActivityVerb:
    """The activity verb for a ``core/transferred`` row.

    Args:
        what: The row's ``what`` — ``transferring``, ``deleting``, ``moving``,
            ``renaming``, ``checking``, ``hashing``, ``listing``, ``importing``
            or ``merging``.
        direction: From :func:`direction_for`.

    Returns:
        The verb. A transfer becomes ``UPLOADED`` or ``DOWNLOADED``; anything
        local becomes ``MODIFIED``.
    """
    mapped = _VERB_FOR_WHAT.get(str(what).lower())
    if mapped is not None:
        return mapped
    if direction == "up":
        return ActivityVerb.UPLOADED
    if direction == "down":
        return ActivityVerb.DOWNLOADED
    return ActivityVerb.MODIFIED


def dedupe_key(group: str, name: str, completed_at: str) -> str:
    """The ``activity.dedupe_key`` for one completed transfer.

    Args:
        group: The row's ``group``.
        name: The row's ``name``.
        completed_at: The row's ``completed_at``.

    Returns:
        ``sha1(group|name|completed_at)`` in hex, matching the comment on the
        column in ``data/schema.sql``.

    ``core/transferred`` re-reports its whole 100-row window on every poll, and
    reports it again after a daemon restart, so the same completion is seen many
    times. The partial unique index on this column is what makes an insert
    idempotent; the in-memory ``seen`` set merely saves the round trip.
    """
    raw = f"{group}|{name}|{completed_at}".encode("utf-8", "surrogatepass")
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()


def transferred_events(body: Mapping[str, Any] | None, *, account_id: str,
                       group: str | None = None,
                       seen: set[str] | None = None) -> list[ActivityEvent]:
    """``core/transferred`` → activity rows. Pure; no I/O.

    Args:
        body: The response.
        account_id: Which account these belong to.
        group: Keep only rows from this group. ``None`` keeps every row —
            note the endpoint's own ``group`` parameter already filters
            server-side, so this is a second belt for a global poll.
        seen: Dedupe keys already handled. Updated in place with the keys of
            the rows returned, so the next poll skips them.

    Returns:
        One :class:`~onedriveui.models.ActivityEvent` per newly-seen row, in the
        order the daemon reported them. Rows whose ``what`` describes work
        rather than a change (``checking``, ``hashing``, ``listing``, …) are
        dropped: a file that was compared and not moved is not an event.

    Fields are read as v1.75.0 really sends them — ``started_at`` /
    ``completed_at`` / ``group`` / ``srcFs`` / ``dstFs`` — **not** as the
    endpoint's own help documents them (``timestamp`` / ``jobid``, neither of
    which exists).
    """
    rows = (body or {}).get("transferred")
    if not isinstance(rows, (list, tuple)):
        return []
    out: list[ActivityEvent] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        what = _str(row.get("what")).lower()
        if what in _NON_EVENT_WHAT or row.get("checked"):
            continue
        row_group = _str(row.get("group"))
        if group is not None and row_group != group:
            continue
        name = _str(row.get("name"))
        if not name:
            continue
        completed_at = _str(row.get("completed_at"))
        key = dedupe_key(row_group, name, completed_at)
        if seen is not None and key in seen:
            continue
        if seen is not None:
            seen.add(key)

        src_fs = _str(row.get("srcFs"))
        dst_fs = _str(row.get("dstFs"))
        direction = direction_for(src_fs, dst_fs)
        error = _str(row.get("error"))
        benign = bool(error) and is_benign(error)
        size = _int(row.get("size"))
        done = _int(row.get("bytes"))
        state = ActivityState.DONE
        error_kind = None
        if error and not benign:
            state = (ActivityState.CANCELLED
                     if "context canceled" in error.lower()
                     else ActivityState.ERROR)
            error_kind = classify(error, direction=direction)[0]
        out.append(ActivityEvent(
            account_id=account_id,
            rel_path=name,
            name=name.rpartition("/")[2] or name,
            is_dir=False,
            verb=verb_for(what, direction),
            direction=direction,
            state=state,
            bytes=done,
            size=size,
            started_at=_str(row.get("started_at")) or utcnow_iso(),
            completed_at=completed_at or utcnow_iso(),
            error=(error or None) if not benign else None,
            error_kind=error_kind,
            job_group=row_group,
            dedupe_key=key,
        ))
    return out


def persist_events(events: Sequence[ActivityEvent], *,
                   writer: DbWriter | None = None,
                   sync: bool = False,
                   src_fs: str = "", dst_fs: str = "") -> int:
    """Write activity rows through the one SQLite writer.

    Args:
        events: What :func:`transferred_events` produced.
        writer: The :class:`~onedriveui.data.writer.DbWriter` to submit to.
            Defaults to the application's.
        sync: Wait for each commit. Off by default — activity is high volume.
        src_fs: Diagnostics only, stored beside the row.
        dst_fs: Diagnostics only.

    Returns:
        How many rows were submitted. A duplicate is dropped by the unique
        index on ``dedupe_key`` and still counts as submitted, because the
        caller cannot know the outcome of an asynchronous write.

    Nothing here touches SQLite directly: ARCHITECTURE §7.2 gives the read-write
    connection to ``DbWriter`` and to nothing else.
    """
    for event in events:
        repo_sync.append_activity(event, src_fs=src_fs, dst_fs=dst_fs,
                                  writer=writer, sync=sync)
    return len(events)


def drain_transferred(ep: RcEndpoint, *, account_id: str,
                      group: str | None = None, persist: bool = True,
                      writer: DbWriter | None = None,
                      seen: set[str] | None = None,
                      sync: bool = False,
                      timeout_s: float | None = None) -> list[ActivityEvent]:
    """Read ``core/transferred`` and persist every completion it reports.

    Args:
        ep: The daemon to read from.
        account_id: Which account the rows belong to.
        group: Ask the daemon for one group only. ``None`` reads every group.
        persist: Write the rows to the ``activity`` table. Off only for tests
            and for a caller that wants to inspect before storing.
        writer: The ``DbWriter`` to submit to.
        seen: Dedupe keys already handled, updated in place.
        sync: Wait for each commit.
        timeout_s: Socket timeout.

    Returns:
        The newly-seen events, already persisted when ``persist``.

    Raises:
        RcError: The daemon rejected the call.
        DaemonUnavailable: The daemon did not answer.

    **This runs before any ``core/stats-reset``, without exception.** A reset
    empties ``core/transferred`` along with the counters, and these 100 rows are
    the only place a completed transfer is recorded until they reach the
    database. :func:`reset_group` enforces the ordering so a caller cannot get
    it wrong.

    Blocking: ``IOPool`` only (ARCHITECTURE §7.6). :class:`StatsPoller` does the
    same work on the GUI thread through the asynchronous client instead.
    """
    params: dict[str, Any] = {}
    if group:
        params["group"] = group
    body = (call_blocking(ep, "core/transferred", params)
            if timeout_s is None else
            call_blocking(ep, "core/transferred", params, timeout_s=timeout_s))
    events = transferred_events(body, account_id=account_id, group=group,
                                seen=seen)
    if persist and events:
        persist_events(events, writer=writer, sync=sync)
    return events


def reset_group(ep: RcEndpoint, group: str, *, account_id: str = "",
                drain: bool = True, writer: DbWriter | None = None,
                seen: set[str] | None = None,
                timeout_s: float | None = None) -> list[ActivityEvent]:
    """Clear one group's counters — **after** draining its completed transfers.

    Args:
        ep: The daemon.
        group: The group to reset. Required: resetting *every* group is a
            process-wide amnesia this application never wants.
        account_id: Which account the drained rows belong to.
        drain: Persist ``core/transferred`` first. Turning this off throws the
            group's transfer history away and is only correct when the caller
            has already drained.
        writer: The ``DbWriter`` for the drain.
        seen: Dedupe keys already handled.
        timeout_s: Socket timeout.

    Returns:
        The events the drain persisted, so the caller can report them.

    Raises:
        ValueError: ``group`` is empty.
        RcError / DaemonUnavailable: The daemon refused or did not answer.

    ``core/stats-reset`` is **never** called implicitly anywhere in this
    codebase: it wipes ``core/transferred`` as well as the counters, so a poller
    that reset on a whim would silently delete the user's activity history. It
    is called here, explicitly, and only after the drain has run.

    Blocking: ``IOPool`` only.
    """
    if not group:
        raise ValueError(
            "reset_group needs a group: core/stats-reset with no group clears "
            "every group in the daemon, ours and anyone else's")
    events: list[ActivityEvent] = []
    if drain:
        events = drain_transferred(ep, account_id=account_id, group=group,
                                   writer=writer, seen=seen,
                                   timeout_s=timeout_s)
    params = {"group": group}
    if timeout_s is None:
        call_blocking(ep, "core/stats-reset", params)
    else:
        call_blocking(ep, "core/stats-reset", params, timeout_s=timeout_s)
    log.info("core/stats-reset on %s after draining %d transfer(s)",
             group, len(events))
    return events


# ─────────────────────────────────────────────────────────────────────────────
# The adaptive poller
# ─────────────────────────────────────────────────────────────────────────────

class StatsPoller(QObject):
    """Poll ``core/stats`` at a rate that follows what the daemon is doing.

    Three rates, from :mod:`onedriveui.constants`: ``TICK_ACTIVE_MS`` (400 ms)
    while anything is transferring or checking, ``TICK_IDLE_MS`` (2 s) when the
    group is quiet, and ``TICK_PAUSED_MS`` (10 s) while sync is paused. A 400 ms
    poll costs a sub-10 ms loopback round trip and is what makes the progress
    bar move like the Windows client's; holding that rate while nothing is
    happening would be pure waste.

    Attributes:
        stats_updated: ``(CoreStats)`` — every successful poll.
        transfers_updated: ``(list[TransferInfo])`` — emitted only when the set
            of in-flight transfers actually changed, and mirrored onto
            ``BUS.transfers_updated``.
        activity: ``(list[ActivityEvent])`` — newly completed transfers, already
            persisted.
        failed: ``(object)`` — an ``RcError`` from a poll. Polling continues:
            a daemon that is briefly unreachable is a normal condition, and the
            state machine counts the failures itself.
    """

    stats_updated = Signal(object)
    transfers_updated = Signal(list)
    activity = Signal(list)
    failed = Signal(object)

    def __init__(self, client: Any, *, account_id: str = "",
                 group: str | None = None, writer: DbWriter | None = None,
                 idle_ms: int = TICK_IDLE_MS, active_ms: int = TICK_ACTIVE_MS,
                 paused_ms: int = TICK_PAUSED_MS, emit_bus: bool = True,
                 drain: bool = True, parent: QObject | None = None) -> None:
        """
        Args:
            client: The :class:`~onedriveui.rc.client.RcClient` to poll through.
                Not owned.
            account_id: Which account completed transfers belong to.
            group: The stats group to poll. **Pass one.** A global
                ``core/stats`` sums every group over the whole process lifetime,
                so ``bytes`` never stops growing and the progress bar never
                completes.
            writer: The ``DbWriter`` for drained activity rows.
            idle_ms: Poll interval when nothing is happening.
            active_ms: Poll interval while transferring or checking.
            paused_ms: Poll interval while paused.
            emit_bus: Mirror ``transfers_updated`` onto the application bus.
            drain: Read ``core/transferred`` when a transfer completes, and
                persist the rows.
            parent: Qt parent.
        """
        super().__init__(parent)
        self._client = client
        self._account_id = account_id
        self._group = group
        self._writer = writer
        self._idle_ms = max(1, int(idle_ms))
        self._active_ms = max(1, int(active_ms))
        self._paused_ms = max(1, int(paused_ms))
        self._emit_bus = bool(emit_bus)
        self._drain = bool(drain)
        self._paused = False
        self._forced_ms: int | None = None
        self._last = CoreStats()
        self._last_transfers: tuple[TransferInfo, ...] = ()
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._in_flight = False
        self._draining = False
        #: Bumped by stop() and set_group(); a reply from an older generation is
        #: discarded, so nothing is emitted after the poller was told to stop.
        self._generation = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll_once)

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def last(self) -> CoreStats:
        """The most recent successful sample. Starts as an empty ``CoreStats``."""
        return self._last

    @property
    def group(self) -> str | None:
        """The group being polled."""
        return self._group

    @property
    def running(self) -> bool:
        """Is the timer armed?"""
        return self._timer.isActive()

    @property
    def interval_ms(self) -> int:
        """The interval currently in force."""
        return self._timer.interval()

    # ── control ─────────────────────────────────────────────────────────────

    def start(self, interval_ms: int | None = None) -> None:
        """Begin polling, with one immediate sample.

        Args:
            interval_ms: Pin the interval instead of adapting. ``None`` (the
                default) restores adaptive behaviour.
        """
        self._forced_ms = int(interval_ms) if interval_ms else None
        self._timer.setInterval(self._forced_ms or self._choose_interval())
        self._timer.start()
        self.poll_once()

    def stop(self) -> None:
        """Stop polling. Idempotent.

        A reply already in flight is discarded rather than applied, so nothing
        is emitted after this returns.
        """
        self._timer.stop()
        self._generation += 1
        self._in_flight = False
        self._draining = False

    def set_interval(self, ms: int | None) -> int:
        """Pin or unpin the poll interval.

        Args:
            ms: The interval in milliseconds, or ``None`` to go back to
                adapting between idle, active and paused.

        Returns:
            The interval now in force.
        """
        self._forced_ms = int(ms) if ms else None
        return self._apply_interval(self._forced_ms or self._choose_interval())

    def set_paused(self, paused: bool) -> None:
        """Tell the poller sync is paused, so it drops to the slow rate."""
        self._paused = bool(paused)
        if self._forced_ms is None:
            self._apply_interval(self._choose_interval())

    def set_group(self, group: str | None) -> None:
        """Repoint at another group, forgetting the previous sample.

        The dedupe memory is deliberately **kept**: the same completed transfer
        can be reported under a group we have already drained.
        """
        if group == self._group:
            return
        self._group = group
        self._generation += 1
        self._in_flight = False
        self._draining = False
        self._last = CoreStats()
        self._last_transfers = ()

    # ── polling ─────────────────────────────────────────────────────────────

    def poll_once(self) -> None:
        """Issue one ``core/stats``. Never blocks; the answer arrives by signal.

        A poll is skipped while the previous one is still in flight, so a
        stalled daemon cannot build a backlog of requests.
        """
        if self._in_flight:
            return
        params: dict[str, Any] = {}
        if self._group:
            params["group"] = self._group
        self._in_flight = True
        generation = self._generation
        call = self._client.call("core/stats", params)
        call.succeeded.connect(
            lambda body: self._on_stats(body, generation))
        call.failed.connect(
            lambda error: self._on_failed(error, generation))

    def _on_stats(self, body: dict, generation: int = -1) -> None:
        if generation not in (-1, self._generation):
            return
        self._in_flight = False
        previous = self._last
        stats = parse_stats(body)
        self._last = stats
        self.stats_updated.emit(stats)

        if stats.transferring != self._last_transfers:
            self._last_transfers = stats.transferring
            rows = list(stats.transferring)
            self.transfers_updated.emit(rows)
            if self._emit_bus:
                BUS.transfers_updated.emit(rows)

        if self._drain and self._completed_since(previous, stats):
            self._drain_async()

        if self._forced_ms is None:
            self._apply_interval(self._choose_interval())

    def _on_failed(self, error: object, generation: int = -1) -> None:
        if generation not in (-1, self._generation):
            return
        self._in_flight = False
        if isinstance(error, DaemonUnavailable):
            log.debug("core/stats poll: daemon unreachable (%s)", error)
        elif isinstance(error, RcError):
            log.info("core/stats poll failed: %s", error)
        self.failed.emit(error)

    @staticmethod
    def _completed_since(previous: CoreStats, current: CoreStats) -> bool:
        """Did anything finish between these two samples?

        Transfers, deletes, renames and errors are all counters that only move
        when something reached a terminal state, and each of them puts a row
        into ``core/transferred``. A drop in any of them means the daemon
        restarted or the group was reset, which is also worth a drain.
        """
        return (current.transfers != previous.transfers
                or current.deletes != previous.deletes
                or current.renames != previous.renames
                or current.errors != previous.errors)

    def _drain_async(self) -> None:
        """Read ``core/transferred`` without blocking the GUI thread."""
        if self._draining:
            return
        params: dict[str, Any] = {}
        if self._group:
            params["group"] = self._group
        self._draining = True
        generation = self._generation
        call = self._client.call("core/transferred", params)
        call.succeeded.connect(
            lambda body: self._on_transferred(body, generation))
        call.failed.connect(
            lambda error: self._on_drain_failed(error, generation))

    def _on_transferred(self, body: dict, generation: int = -1) -> None:
        if generation not in (-1, self._generation):
            return
        self._draining = False
        events = transferred_events(body, account_id=self._account_id,
                                    group=self._group, seen=self._seen)
        self._trim_seen(events)
        if not events:
            return
        persist_events(events, writer=self._writer)
        self.activity.emit(events)

    def _on_drain_failed(self, error: object, generation: int = -1) -> None:
        if generation not in (-1, self._generation):
            return
        self._draining = False
        log.info("core/transferred poll failed: %s", error)

    def drain_now(self) -> None:
        """Force a ``core/transferred`` read, e.g. just before a group reset."""
        self._drain_async()

    def drain_group(self, group: str) -> None:
        """Drain one group synchronously-ish, for ``JobRegistry``'s cleanup hook.

        Args:
            group: The group whose last job just finished.

        Wire this to ``JobRegistry(before_cleanup=…)``: ``core/stats-delete``
        discards the group's ``core/transferred`` rows along with its counters,
        so they have to be read one last time first. The read is still
        asynchronous — the GUI thread never blocks — and the delete is issued
        immediately afterwards by the registry, which is safe because rclone
        serialises both calls on the same daemon.
        """
        params = {"group": group}
        call = self._client.call("core/transferred", params)
        call.succeeded.connect(
            lambda body, g=group: self._on_group_drained(body, g))
        call.failed.connect(
            lambda error, g=group: log.info(
                "final core/transferred read for %s failed: %s", g, error))

    def _on_group_drained(self, body: dict, group: str) -> None:
        events = transferred_events(body, account_id=self._account_id,
                                    group=group, seen=self._seen)
        self._trim_seen(events)
        if not events:
            return
        persist_events(events, writer=self._writer)
        self.activity.emit(events)

    # ── interval selection ──────────────────────────────────────────────────

    def _choose_interval(self) -> int:
        if self._paused:
            return self._paused_ms
        stats = self._last
        busy = bool(stats.transferring or stats.checking
                    or stats.transfers < stats.total_transfers
                    or stats.checks < stats.total_checks)
        return self._active_ms if busy else self._idle_ms

    def _apply_interval(self, ms: int) -> int:
        if self._timer.interval() != ms:
            self._timer.setInterval(ms)
        return ms

    def _trim_seen(self, events: Iterable[ActivityEvent]) -> None:
        """Keep the dedupe memory bounded at :data:`SEEN_CAP` keys."""
        for event in events:
            if event.dedupe_key:
                self._seen_order.append(event.dedupe_key)
        overflow = len(self._seen_order) - SEEN_CAP
        if overflow <= 0:
            return
        for key in self._seen_order[:overflow]:
            self._seen.discard(key)
        del self._seen_order[:overflow]
