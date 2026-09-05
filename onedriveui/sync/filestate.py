"""One badge per file, answered in under twenty milliseconds.

Nautilus calls ``update_file_info`` **on its own UI thread**, synchronously, for
every visible file — and Python extensions cannot construct a
``Nautilus.OperationHandle``, so there is no way to answer asynchronously and
come back later. Anything slow here does not make emblems appear late; it makes
the file manager freeze while scrolling.

So this module is a *read model* and nothing else. The expensive work — walking
thousands of vfsMeta sidecars — happens on an ``IOPool`` worker and lands in the
``cache_index`` table. Answering a status is then one indexed lookup, and
:meth:`FileStateService.statuses` for a thousand paths is one query, not a
thousand.

The badge a file gets is a *merge*, not a single fact, and the precedence is
deliberate:

1. **excluded** — the user said not to sync it. Nothing else matters; showing
   "Sync problem" on a file the user deliberately excluded is just wrong.
2. **error** — an open issue names this path.
3. **dirty / syncing** — bytes are moving now.
4. **pinned** — locally complete *and* the user asked to keep it.
5. **local / partial / online_only** — what the cache actually holds.

And one rule that keeps it honest: a path nothing is known about answers
``UNKNOWN``, never ``ONLINE_ONLY``. They look the same in the file manager and
they mean opposite things — "we have not scanned this yet" versus "this is
definitely not on your disk" — and guessing the second produces a cloud badge on
a file that is sitting right there.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.constants import IPC_BUDGET_MS
from onedriveui.data import repo_files, repo_sync
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import AccountInfo, FileState, FileStatus
from onedriveui.rc import vfs

log = logging.getLogger(__name__)

__all__ = ["FileStateService", "BUDGET_MS"]

#: What the Nautilus IPC has to answer within. Nautilus calls us on its own UI
#: thread and cannot be told to wait.
BUDGET_MS: Final = IPC_BUDGET_MS


class FileStateService(QObject):
    """The merged, indexed answer to "what badge does this file get?".

    Args:
        account: The account.
        endpoint: ``() -> RcEndpoint | None`` for the mount's rc daemon.
        writer: The database writer.
        parent: Qt parent.

    Signals:
        changed: ``(rel_path, FileStatus)`` when one file's badge moves.
        invalidated: A list of paths whose badges are now stale.
    """

    changed = Signal(str, FileStatus)
    invalidated = Signal(list)

    def __init__(
        self,
        account: AccountInfo,
        *,
        endpoint: Any = None,
        writer: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._endpoint = endpoint or (lambda: None)
        self._writer = writer
        #: Paths named by an open issue, refreshed on the issue signals rather
        #: than queried per lookup — the IPC budget does not allow a join.
        self._errored: set[str] = set()
        #: Paths the user excluded. Small, and read once per change.
        self._excluded: set[str] = set()
        self._pinned: set[str] = set()
        self._dirty: set[str] = set()
        #: Whether a cache scan has ever completed for this account. Decides
        #: what a path with no row means; see `_merge`. ``None`` until asked.
        self._scanned: bool | None = None

    # ═════════════════════════════════════════════════════════════════════════
    # Reads — the IPC hot path
    # ═════════════════════════════════════════════════════════════════════════

    def status(self, rel_path: str) -> FileStatus:
        """One file's badge.

        Args:
            rel_path: The path, relative to the sync root.

        Returns:
            Its :class:`~onedriveui.models.FileStatus`. ``UNKNOWN`` when nothing
            is known — never ``ONLINE_ONLY``, which looks the same to the user
            and means the opposite.
        """
        return self.statuses([rel_path])[rel_path]

    def statuses(self, rel_paths: Sequence[str]) -> dict[str, FileStatus]:
        """Badges for many files at once. **One query, not one per path.**

        Args:
            rel_paths: The paths.

        Returns:
            ``{rel_path: FileStatus}``, with an entry for every path asked
            about — a missing key would make the extension raise on Nautilus's
            UI thread.

        This is the call the Nautilus IPC answers from, inside
        :data:`BUDGET_MS`. A folder of a thousand files must cost one indexed
        lookup, because the alternative is a thousand round trips on the file
        manager's own thread while the user is trying to scroll.
        """
        started = time.monotonic()
        try:
            rows = repo_files.file_states(self.account.id, list(rel_paths))
        except Exception:  # noqa: BLE001 - the extension must never see a traceback
            log.error("could not read cache_index", exc_info=True)
            rows = {}
            readable = False
        else:
            readable = True

        out: dict[str, FileStatus] = {}
        for rel_path in rel_paths:
            out[rel_path] = self._merge(rel_path, rows.get(rel_path),
                                        indexed=readable and self._has_scanned())

        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms > BUDGET_MS:
            log.warning("statuses(%d paths) took %.1f ms, over the %d ms budget",
                        len(rel_paths), elapsed_ms, BUDGET_MS)
        return out

    def _has_scanned(self) -> bool:
        """Whether `cache_index` has ever been written for this account."""
        if self._scanned is None:
            try:
                self._scanned = repo_files.cache_generation(self.account.id) > 0
            except Exception:  # noqa: BLE001 - answer "unknown" rather than raise
                log.debug("could not read the cache generation", exc_info=True)
                return False
        return self._scanned

    def _merge(self, rel_path: str, row: Any, *, indexed: bool = True) -> FileStatus:
        """Combine cache state, pins, exclusions and issues into one badge.

        Args:
            rel_path: The path.
            row: Its `cache_index` row, ``None`` or an ``UNKNOWN`` status when
                it has none.
            indexed: Whether that ``None`` is trustworthy — the index was
                readable and a scan has completed. Only then does "no row"
                mean "not on this disk".
        """
        if rel_path in self._excluded:
            # The user said not to sync this. Reporting "Sync problem" on a file
            # they deliberately excluded would be actively misleading.
            return FileStatus(rel_path=rel_path, state=FileState.EXCLUDED,
                              excluded=True)
        # `repo_files.file_states` synthesises an UNKNOWN status for a path with
        # no row, so "no row" arrives both ways.
        if row is None or getattr(row, "state", FileState.UNKNOWN) is FileState.UNKNOWN:
            if not indexed:
                # Not scanned yet, or the index could not be read. ONLINE_ONLY
                # would claim knowledge we do not have, and it renders
                # identically to a real cloud badge.
                return FileStatus(rel_path=rel_path, state=FileState.UNKNOWN)
            # The scan walked the whole VFS cache and this path is not in it:
            # the mount serves it from the cloud. That IS the cloud badge — and
            # without it every file the user has never opened had no emblem at
            # all, which reads as "not synced" next to its cached neighbours.
            return FileStatus(rel_path=rel_path, state=FileState.ONLINE_ONLY,
                              pinned=rel_path in self._pinned)

        state = getattr(row, "state", FileState.UNKNOWN)
        size = getattr(row, "size", 0)
        local = getattr(row, "bytes_local", 0)

        if rel_path in self._errored:
            return FileStatus(rel_path=rel_path, state=FileState.ERROR,
                              size=size, bytes_local=local, has_error=True)
        if rel_path in self._dirty or state is FileState.DIRTY:
            return FileStatus(rel_path=rel_path, state=FileState.DIRTY,
                              size=size, bytes_local=local)
        if rel_path in self._pinned and state is FileState.LOCAL:
            return FileStatus(rel_path=rel_path, state=FileState.PINNED,
                              size=size, bytes_local=local, pinned=True)
        return FileStatus(rel_path=rel_path, state=state, size=size,
                          bytes_local=local,
                          pinned=rel_path in self._pinned)

    # ═════════════════════════════════════════════════════════════════════════
    # Writes — refreshing the model
    # ═════════════════════════════════════════════════════════════════════════

    def refresh_overlays(self) -> None:
        """Reload the small sets the merge consults.

        Pins, exclusions, dirty paths and errored paths are each a few hundred
        rows at most, so they live in memory and are refreshed on their own
        signals. Joining them per lookup would blow the IPC budget on the file
        manager's UI thread.
        """
        try:
            self._pinned = {p.rel_path for p in repo_files.pins(self.account.id)}
            self._excluded = set(repo_files.excluded_paths(self.account.id))
            self._dirty = set(repo_files.dirty_paths(self.account.id))
            self._errored = {i.rel_path for i in repo_sync.open_issues(self.account.id)
                             if i.rel_path}
        except Exception:  # noqa: BLE001
            log.error("could not refresh the file-state overlays", exc_info=True)

    def rebuild(self, *, progress: Any = None, cancel: Any = None) -> int:
        """Walk the VFS cache and rewrite ``cache_index``. **IOPool only.**

        Args:
            progress: ``(done, total)``.
            cancel: ``() -> bool``.

        Returns:
            How many rows were written.

        Uses the generation protocol: rows are written under a **new**
        generation and the old one is pruned only once the walk finishes. An
        interrupted scan therefore leaves the previous generation's rows intact,
        which matters more than it sounds — deleting first would blank every
        emblem in the file manager for the length of the scan, and lose the lot
        entirely on a crash.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return 0
        try:
            info = vfs.disk_cache_info(endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.warning("could not reach the VFS to rebuild the cache index",
                        exc_info=True)
            return 0

        generation = repo_files.next_cache_generation(self.account.id)
        rows = []
        for entry in vfs.scan(info, generation, progress, pinned=self._pinned,
                              cancel=cancel):
            rows.append(entry)
        if cancel is not None and cancel():
            log.info("cache scan cancelled; generation %d left incomplete and "
                     "generation %d untouched", generation, generation - 1)
            return 0

        repo_files.upsert_cache_rows(self.account.id, rows, generation,
                                     writer=self._writer)
        removed = repo_files.prune_cache_generation(self.account.id,
                                                    generation - 1,
                                                    writer=self._writer)
        log.info("cache index rebuilt for %s: %d rows, %d stale rows pruned",
                 self.account.id, len(rows), removed)
        self._scanned = True
        self.refresh_overlays()
        return len(rows)

    def invalidate(self, rel_paths: Iterable[str]) -> None:
        """Announce that these badges are stale.

        Nautilus caches what we last told it and will not ask again on its own,
        so a file that finished uploading keeps its syncing emblem until the
        folder is reopened unless this is called.
        """
        paths = list(rel_paths)
        if not paths:
            return
        self.refresh_overlays()
        self.invalidated.emit(paths)
        BUS.file_states_invalidated.emit(self.account.id, paths)

    def note_state(self, rel_path: str, status: FileStatus) -> None:
        """Publish one file's new badge."""
        self.changed.emit(rel_path, status)
        BUS.file_state_changed.emit(self.account.id, rel_path, status)
