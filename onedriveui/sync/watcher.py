"""Watching the sync root, and the landmine already sitting in it.

Two watches, because they see different things:

* **``Gio.FileMonitor`` on the sync root** — the user's own edits, creations,
  renames and deletions. This is what feeds preflight validation, conflict
  globbing, mass-delete counting and cache invalidation.
* **inotify on ``vfsMeta``** — rclone's own bookkeeping. A sidecar being removed
  is the cache evictor taking back a file the user asked to keep, and there is
  no other signal that it happened.

Everything is **coalesced over 400 ms**. Saving a file in a text editor produces
a burst — a temporary file, a rename, an attribute change — and reacting to each
one means three round trips through the preflight validator and three IPC
invalidations for one save. The coalescing window is the same 400 ms as the
active tick, so at most one batch per tick reaches the engine.

**The delete burst is a safety mechanism, not a statistic.** 250 files
disappearing in a minute is either the user tidying up or a mounted drive that
went away, and the two are indistinguishable from here. So it raises one
``MASS_DELETE`` decision and lets a human tell them apart, rather than
propagating a deletion of 250 files to the cloud on the assumption that it was
meant.

And the landmine: **``~/OneDrive/.Trash-1000`` already exists on this machine.**
The file manager creates a trash directory inside any filesystem it deletes from
— including the FUSE mount — so a file "deleted" from the OneDrive folder is
moved into a hidden directory that is itself inside the OneDrive folder, and is
promptly uploaded to the cloud. The user's deleted files end up taking cloud
quota, under a directory they cannot see. :meth:`LocalWatcher.intercept_trash_dir`
drains it into the real trash and says so.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.constants import MASS_DELETE_DEFAULT_THRESHOLD, TICK_ACTIVE_MS
from onedriveui.models import AccountInfo, DecisionKind, IssueCode
from onedriveui.platform import trash

log = logging.getLogger(__name__)

__all__ = ["LocalWatcher", "COALESCE_MS", "BURST_WINDOW_S", "MOUNT_TRASH_DIR"]

#: How long changes are gathered before they are reported. One text-editor save
#: produces a temporary file, a rename and an attribute change; reacting to each
#: means three preflight passes and three IPC invalidations for one save.
COALESCE_MS: Final = TICK_ACTIVE_MS

#: The window over which deletions are counted for the mass-delete gate.
BURST_WINDOW_S: Final = 60.0

#: The file manager's trash directory *inside* the mount. Present on this
#: machine already, and uid-specific: 1000 here, matching XDG_RUNTIME_DIR.
MOUNT_TRASH_DIR: Final = f".Trash-{os.getuid()}"


class LocalWatcher(QObject):
    """Watches the sync root and reports coalesced batches of changes.

    Args:
        account: The account.
        decisions: The :class:`~onedriveui.sync.decisions.DecisionCenter`, for
            the mass-delete gate.
        issues: The :class:`~onedriveui.sync.issues.IssueEngine`.
        threshold: How many deletions in :data:`BURST_WINDOW_S` count as a mass
            delete. From ``safety.mass_delete_threshold``.
        monotonic: The clock, injected for tests.
        parent: Qt parent.

    Signals:
        changed: A coalesced ``list[str]`` of relative paths.
        deleted: A coalesced ``list[str]`` of paths that went away.
        burst: ``(count, window_seconds)`` when the delete threshold trips.
    """

    changed = Signal(list)
    deleted = Signal(list)
    burst = Signal(int, float)

    def __init__(
        self,
        account: AccountInfo,
        *,
        decisions: Any = None,
        issues: Any = None,
        threshold: int = MASS_DELETE_DEFAULT_THRESHOLD,
        monotonic: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._decisions = decisions
        self._issues = issues
        self._threshold = threshold
        self._monotonic = monotonic or time.monotonic

        self._monitors: list[Any] = []
        self._pending_changes: set[str] = set()
        self._pending_deletes: set[str] = set()
        #: Deletion timestamps inside the burst window, oldest first.
        self._deletions: list[float] = []
        self._burst_raised = False

        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(COALESCE_MS)
        self._flush.timeout.connect(self.flush)

    # ═════════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═════════════════════════════════════════════════════════════════════════

    def watch(self) -> bool:
        """Start watching the sync root.

        Returns:
            True when a monitor was established.

        Uses ``Gio.FileMonitor``, which rides the GLib pump the application
        already runs for D-Bus. A second event loop for inotify would need its
        own thread and its own hand-off; this needs neither.
        """
        root = Path(self.account.sync_root).expanduser()
        if not root.is_dir():
            log.warning("cannot watch %s: it does not exist", root)
            return False
        try:
            from gi.repository import Gio

            monitor = Gio.File.new_for_path(str(root)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            monitor.connect("changed", self._on_gio_change)
            self._monitors.append(monitor)
        except Exception:  # noqa: BLE001 - a missing monitor degrades, never crashes
            log.warning("could not start a file monitor on %s", root,
                        exc_info=True)
            return False
        log.info("watching %s", root)
        return True

    def stop(self) -> None:
        """Stop watching. Anything already gathered is flushed first."""
        self.flush()
        for monitor in self._monitors:
            try:
                monitor.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._monitors.clear()

    # ═════════════════════════════════════════════════════════════════════════
    # Events
    # ═════════════════════════════════════════════════════════════════════════

    def _on_gio_change(self, _monitor: Any, file: Any, _other: Any,
                       event: Any) -> None:  # pragma: no cover - needs GLib
        try:
            from gi.repository import Gio

            path = file.get_path() or ""
            if event == Gio.FileMonitorEvent.DELETED:
                self.note_delete(path)
            else:
                self.note_change(path)
        except Exception:  # noqa: BLE001
            log.debug("could not read a file-monitor event", exc_info=True)

    def note_change(self, path: str) -> None:
        """Record a change. Reported after the coalescing window."""
        rel = self._relative(path)
        if rel is None:
            return
        self._pending_changes.add(rel)
        if not self._flush.isActive():
            self._flush.start()

    def note_delete(self, path: str) -> None:
        """Record a deletion, and count it toward the mass-delete gate."""
        rel = self._relative(path)
        if rel is None:
            return
        self._pending_deletes.add(rel)
        self._deletions.append(self._monotonic())
        self._check_burst()
        if not self._flush.isActive():
            self._flush.start()

    def flush(self) -> None:
        """Report everything gathered, as one batch each."""
        if self._pending_changes:
            batch, self._pending_changes = sorted(self._pending_changes), set()
            self.changed.emit(batch)
        if self._pending_deletes:
            batch, self._pending_deletes = sorted(self._pending_deletes), set()
            self.deleted.emit(batch)

    def _relative(self, path: str) -> str | None:
        root = Path(self.account.sync_root).expanduser()
        try:
            return str(Path(path).relative_to(root))
        except ValueError:
            return None

    # ═════════════════════════════════════════════════════════════════════════
    # The mass-delete gate
    # ═════════════════════════════════════════════════════════════════════════

    def delete_burst(self) -> int:
        """How many deletions have happened inside the burst window."""
        cutoff = self._monotonic() - BURST_WINDOW_S
        self._deletions = [t for t in self._deletions if t >= cutoff]
        return len(self._deletions)

    def _check_burst(self) -> None:
        """Raise **one** decision when the threshold trips.

        One, not one per deletion: a folder of 4 000 files disappearing must
        produce a single "delete these 4 000 items?" question, and the flag
        stays set until the burst subsides so a slow trickle past the threshold
        does not re-ask every four hundred milliseconds.
        """
        count = self.delete_burst()
        if count < self._threshold:
            self._burst_raised = False
            return
        if self._burst_raised:
            return
        self._burst_raised = True

        log.warning("%d deletions in %.0f s under %s — asking before this "
                    "propagates", count, BURST_WINDOW_S, self.account.sync_root)
        self.burst.emit(count, BURST_WINDOW_S)
        if self._decisions is not None:
            self._decisions.require(DecisionKind.MASS_DELETE, {
                "count": count,
                "window_s": BURST_WINDOW_S,
                "sync_root": self.account.sync_root,
                "nothing_was_deleted": True,
            })

    # ═════════════════════════════════════════════════════════════════════════
    # The trash-directory landmine
    # ═════════════════════════════════════════════════════════════════════════

    def intercept_trash_dir(self) -> int:
        """Drain ``~/OneDrive/.Trash-1000`` into the real trash.

        Returns:
            How many entries were moved out.

        The file manager creates a trash directory inside **any** filesystem it
        deletes from, the FUSE mount included. So a file the user "deleted" from
        their OneDrive folder is moved into a hidden directory that is itself
        inside the OneDrive folder — and is then uploaded, silently consuming
        cloud quota under a path they cannot see. This directory already exists
        on this machine.

        The mandatory filters exclude it from sync, and this drains what is
        already there into the user's real trash, where they expected it to be.
        """
        root = Path(self.account.sync_root).expanduser()
        nested = root / MOUNT_TRASH_DIR
        if not nested.is_dir():
            return 0

        try:
            drained = trash.drain_nested_trash(root)
        except Exception:  # noqa: BLE001
            log.error("could not drain %s into the real trash", nested,
                      exc_info=True)
            return 0

        moved = len(drained) + self._drain_orphans(root)
        if moved:
            log.warning("moved %d items out of %s into the real trash; they "
                        "were about to be uploaded to OneDrive", moved, nested)
            if self._issues is not None:
                self._issues.raise_issue(
                    IssueCode.PARTIAL_FILE_FOUND,
                    rel_path=MOUNT_TRASH_DIR,
                    detail=f"{moved} deleted items were sitting inside your "
                           f"OneDrive folder and have been moved to the trash")
        return moved

    def _drain_orphans(self, root: Path) -> int:
        """Rescue `files/` entries that have no `.trashinfo` beside them.

        `drain_nested_trash` reads `info/`, correctly: an entry with no info
        file is unrestorable and invisible to every trash browser. But that is
        exactly what makes it worth rescuing here — nothing else will ever
        remove it, so it sits inside the sync root being uploaded forever, and
        the user has no way to see it or delete it.

        Args:
            root: The sync root.

        Returns:
            How many orphans were moved into the real trash.
        """
        moved = 0
        for trash_dir in trash.find_nested_trash_dirs(root):
            files_dir = trash_dir / "files"
            info_dir = trash_dir / "info"
            try:
                entries = sorted(files_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if (info_dir / f"{entry.name}.trashinfo").exists():
                    continue          # a real entry; drain_nested_trash has it
                try:
                    trash.trash(entry)
                except Exception:  # noqa: BLE001 - one orphan is not worth a crash
                    log.warning("could not rescue the orphaned %s", entry,
                                exc_info=True)
                    continue
                log.warning("rescued %s: it was inside the sync root with no "
                            "trashinfo, so nothing else would ever remove it",
                            entry)
                moved += 1
        return moved

    def nested_trash_dirs(self) -> list[Path]:
        """Every file-manager trash directory found inside the sync root."""
        root = Path(self.account.sync_root).expanduser()
        try:
            return list(trash.find_nested_trash_dirs(root))
        except Exception:  # noqa: BLE001
            return []

    # ═════════════════════════════════════════════════════════════════════════
    # Feeding the other engines
    # ═════════════════════════════════════════════════════════════════════════

    def validate(self, rel_paths: Iterable[str]) -> int:
        """Run the preflight validator over a batch and raise what it finds.

        Returns:
            How many violations were recorded.

        This is why the watcher exists at all: a colon in a filename becomes an
        issue the moment the file is saved, rather than minutes later when an
        upload fails on a file the user has moved on from.
        """
        from onedriveui.sync.preflight import validate_path

        root = Path(self.account.sync_root).expanduser()
        violations = []
        for rel in rel_paths:
            violation = validate_path(rel, root)
            if violation is not None:
                violations.append(violation)
        if violations and self._issues is not None:
            self._issues.ingest_preflight(violations)
        return len(violations)
