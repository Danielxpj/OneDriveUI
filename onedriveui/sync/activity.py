"""The feed: what happened, once each, newest first.

Three sources describe the same transfers and none of them is sufficient alone:

* ``core/transferred`` — completed transfers, but only the **last 100**, and
  re-reported in full on every poll, and lost entirely when the daemon restarts.
  It is a sliding window, not a log.
* ``core/stats.transferring[]`` — what is moving *right now*, with byte counts.
  Rows vanish the instant a transfer finishes, so watching only this means
  never seeing anything complete.
* The bisync log — what the offline folder did, which the other two never see.

Merging them needs a stable identity, and the one that works is
``sha1(group|name|completed_at)``: the same completed transfer re-reported on the
next poll produces the same key and is dropped. Using the path alone would
collapse a file uploaded twice in an hour into one row; using a row number would
break the moment the window slid.

**Nothing here ever calls ``core/stats-reset``.** The reset clears the group's
counters *and* wipes ``core/transferred`` — so resetting before draining
destroys the only record that those transfers happened, and this module is the
thing that would be destroying it. The drain is explicit, ordered, and owned by
the Supervisor's transition effects.

One more rule with a reason: on ``daemon_restarted`` every row still marked
``inflight`` becomes ``interrupted``, not ``done`` and not ``error``. The
transfer neither completed nor failed — the process that knew about it went
away. Marking it done would claim a file is synced when it is not; marking it
failed would raise an issue about something that will very likely just resume.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.constants import ACTIVITY_CAP_ROWS, ACTIVITY_UI_ROWS
from onedriveui.data import repo_sync
from onedriveui.models import (
    AccountInfo,
    ActivityEvent,
    ActivityState,
    ActivityVerb,
    CoreStats,
    TransferInfo,
    utcnow_iso,
)
from onedriveui.rc import stats as rc_stats

log = logging.getLogger(__name__)

__all__ = ["ActivityFeed", "CAP_ROWS", "UI_ROWS"]

#: Rows kept per account. Beyond this the oldest go: an activity feed is a
#: recent history, not an audit log, and 5 000 rows is already more than anyone
#: scrolls through.
CAP_ROWS: Final = ACTIVITY_CAP_ROWS

#: Rows the Activity Center renders at once.
UI_ROWS: Final = ACTIVITY_UI_ROWS


class ActivityFeed(QObject):
    """Merges the three sources into one deduplicated, capped feed.

    Args:
        account: The account.
        writer: The database writer.
        issues: The :class:`~onedriveui.sync.issues.IssueEngine`, so a failed
            transfer becomes an issue as well as a feed row.
        parent: Qt parent.

    Signals:
        appended: A completed :class:`~onedriveui.models.ActivityEvent`.
        updated: An in-flight one, as its byte count moves.
    """

    appended = Signal(ActivityEvent)
    updated = Signal(ActivityEvent)

    def __init__(
        self,
        account: AccountInfo,
        *,
        writer: Any = None,
        issues: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._writer = writer
        self._issues = issues
        #: Dedupe keys already persisted, so a re-reported transfer costs a set
        #: lookup rather than a database round trip. Bounded with the table.
        self._seen: set[str] = set()
        #: In-flight rows by name, so a progress update finds its row without
        #: querying. Cleared when the transfer leaves `transferring[]`.
        self._inflight: dict[str, ActivityEvent] = {}

    # ═════════════════════════════════════════════════════════════════════════
    # Ingest
    # ═════════════════════════════════════════════════════════════════════════

    def ingest_transferred(self, body: dict[str, Any] | None) -> list[ActivityEvent]:
        """Persist completed transfers from a ``core/transferred`` payload.

        Args:
            body: The raw response.

        Returns:
            Only the events that were **new**. ``core/transferred`` re-reports
            its whole 100-row window on every poll, so without the dedupe a
            2 Hz poller would insert the same hundred rows every half second.
        """
        # `transferred_events` does the dedupe itself against the set it is
        # handed, and updates it in place — so the window's other 99 rows never
        # reach the database on the next poll.
        fresh: list[ActivityEvent] = []
        for event in rc_stats.transferred_events(
                body, account_id=self.account.id, seen=self._seen):
            stored = self._append(event, deduped=True)
            if stored is not None:
                fresh.append(stored)
        return fresh

    def ingest_stats(self, stats: CoreStats) -> list[ActivityEvent]:
        """Update the in-flight rows from ``core/stats.transferring[]``.

        Args:
            stats: The parsed stats.

        Returns:
            The in-flight events, updated.

        A row that has left ``transferring[]`` is *not* marked done here. It may
        have completed, and it may have failed, and only ``core/transferred``
        knows which — inferring "done" from "no longer moving" would report a
        failed upload as a successful one.
        """
        live: list[ActivityEvent] = []
        names = set()
        for transfer in stats.transferring:
            names.add(transfer.name)
            event = self._as_inflight(transfer)
            self._inflight[transfer.name] = event
            live.append(event)
            self.updated.emit(event)
            BUS.activity_updated.emit(event)
        for gone in set(self._inflight) - names:
            self._inflight.pop(gone, None)
        return live

    def ingest_log_conflict(self, rel_path: str, run_id: str = "") -> ActivityEvent | None:
        """Record what the offline folder did, which the rc never sees."""
        return self._append(ActivityEvent(
            account_id=self.account.id, rel_path=rel_path,
            name=rel_path.rsplit("/", 1)[-1], verb=ActivityVerb.MODIFIED,
            state=ActivityState.DONE, run_id=run_id,
            started_at=utcnow_iso(), completed_at=utcnow_iso(),
            dedupe_key=rc_stats.dedupe_key(run_id, rel_path, utcnow_iso()),
        ))

    def record(self, rel_path: str, verb: ActivityVerb, *,
               state: ActivityState = ActivityState.DONE,
               size: int = 0, error: str = "") -> ActivityEvent | None:
        """Record something this client did itself — a pin, a free-up, a share.

        These never appear in rclone's counters, because rclone did not do them,
        and a feed that showed uploads but not "Always kept on this device"
        would be describing half of what the user did.
        """
        now = utcnow_iso()
        return self._append(ActivityEvent(
            account_id=self.account.id, rel_path=rel_path,
            name=rel_path.rsplit("/", 1)[-1], verb=verb, state=state,
            size=size, bytes=size if state is ActivityState.DONE else 0,
            error=error, started_at=now, completed_at=now,
            dedupe_key=rc_stats.dedupe_key("local", rel_path, now),
        ))

    # ═════════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═════════════════════════════════════════════════════════════════════════

    def on_daemon_restarted(self, execute_id: str = "") -> int:
        """Mark every in-flight row ``interrupted``.

        Args:
            execute_id: The new daemon's id, for the log.

        Returns:
            How many rows were marked.

        Not ``done`` and not ``error``. The transfer neither completed nor
        failed — the process that knew about it went away, and its
        ``core/transferred`` history went with it. Claiming success would tell
        the user a file is safely in OneDrive when it may not be; claiming
        failure would raise an issue about something that is about to resume on
        its own.
        """
        self._inflight.clear()
        try:
            marked = repo_sync.mark_inflight_interrupted(
                self.account.id, writer=self._writer)
        except Exception:  # noqa: BLE001
            log.error("could not mark in-flight activity interrupted", exc_info=True)
            return 0
        if marked:
            log.warning("%d in-flight transfers marked interrupted after the "
                        "daemon restarted (%s)", marked, execute_id or "?")
        return marked

    def recent(self, limit: int = UI_ROWS) -> list[ActivityEvent]:
        """The newest rows, for the Activity Center."""
        return repo_sync.recent_activity(self.account.id, limit=limit)

    def for_path(self, rel_path: str, limit: int = 10) -> list[ActivityEvent]:
        """This file's history, for its properties pane."""
        return repo_sync.activity_for_path(self.account.id, rel_path, limit=limit)

    # ═════════════════════════════════════════════════════════════════════════
    # Internals
    # ═════════════════════════════════════════════════════════════════════════

    def _append(self, event: ActivityEvent, *,
                deduped: bool = False) -> ActivityEvent | None:
        """Persist one event, deduplicated, and raise an issue if it failed.

        Args:
            event: The event.
            deduped: The caller has already checked and recorded the key.
        """
        if not deduped and event.dedupe_key and event.dedupe_key in self._seen:
            return None
        try:
            event_id = repo_sync.append_activity(event, writer=self._writer)
        except Exception:  # noqa: BLE001 - the feed is never worth a crash
            log.error("could not record activity for %r", event.rel_path,
                      exc_info=True)
            return None

        if event.dedupe_key:
            self._seen.add(event.dedupe_key)
            # Bounded with the table it mirrors, so a long session cannot grow
            # this set without limit.
            if len(self._seen) > CAP_ROWS:
                self._seen = set(list(self._seen)[-CAP_ROWS:])

        stored = _with(event, id=event_id or 0)
        self.appended.emit(stored)
        BUS.activity_appended.emit(stored)

        if stored.error and self._issues is not None:
            self._issues.ingest_transfer_error(stored)
        return stored

    def _as_inflight(self, transfer: TransferInfo) -> ActivityEvent:
        existing = self._inflight.get(transfer.name)
        return ActivityEvent(
            id=existing.id if existing else 0,
            account_id=self.account.id,
            rel_path=transfer.name,
            name=transfer.name.rsplit("/", 1)[-1],
            verb=(ActivityVerb.UPLOADED if transfer.is_upload
                  else ActivityVerb.DOWNLOADED),
            direction="up" if transfer.is_upload else "down",
            state=ActivityState.INFLIGHT,
            bytes=transfer.bytes,
            size=transfer.size,
            started_at=existing.started_at if existing else utcnow_iso(),
            job_group=transfer.group,
        )


def _with(event: ActivityEvent, **changes: Any) -> ActivityEvent:
    from dataclasses import replace

    return replace(event, **changes)
