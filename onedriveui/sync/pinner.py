"""Files On-Demand: "Always keep on this device" and "Free up space".

Windows implements this with a filesystem filter driver. Linux has no such
thing, so both directions are done through the FUSE mount, and each has one
non-obvious rule that everything else follows from.

**Hydrating is just reading the file — but only if you read it correctly.**
Opening the file through the mount and reading it end to end makes rclone
download it, and that is the whole mechanism. What breaks it is the fast paths:
``sendfile()`` and ``copy_file_range()`` either fail outright on FUSE or silently
copy holes as zeros, and Python's ``shutil`` reaches for both. So the read is
done in explicit 4 MiB blocks with ``buffering=0``, which is slower to write and
is the only version that is correct.

**Dehydrating is unlinking two files, in one specific order.** There is no rc
endpoint that frees a cached file: ``vfs/forget`` returns a reassuring
``{"forgotten": [...]}`` and provably frees nothing, and ``options/set`` does not
reach a live VFS. Unlinking the sidecar *strictly before* the data file is the
supported route — rclone logs ``detected external removal of cache file`` and
re-downloads correctly. The order is invariant I5: a crash between the two
unlinks then leaves a data file with no metadata, which rclone treats as
uncached, whereas the reverse leaves metadata claiming ranges that no longer
exist and rclone serves holes as zeros.

Two refusals sit in front of all of it. A ``Dirty`` sidecar is an un-uploaded
local change — those bytes exist on this disk and nowhere else on the planet —
and an item in ``vfs/queue`` is seconds from being uploaded. Freeing either is
data loss, so it is refused and raised as an issue rather than done.

And one thing that has to be watched rather than assumed: rclone's own cache
evictor deletes files by age and by size cap, and it does not know that the user
asked for one to be kept. :class:`RepinWatcher` notices its victims and puts
them back — without it, "Always keep on this device" quietly stops being true
after a week.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.constants import HYDRATE_BLOCK_BYTES, MAX_CONCURRENT_PINS
from onedriveui.data import repo_files
from onedriveui.errors import DaemonUnavailable, RcError, SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    FileState,
    FileStatus,
    IssueCode,
    RcEndpoint,
)
from onedriveui.rc import vfs

log = logging.getLogger(__name__)

__all__ = ["Pinner", "RepinWatcher", "hydrate_file", "EVICTOR_LOG_MARKER"]

#: The journal line rclone writes when its own evictor removes a cached file.
#: Watching for this is how a pin that the evictor undid is noticed — the file
#: simply becomes online-only again, with nothing else to distinguish it from a
#: file the user never pinned.
EVICTOR_LOG_MARKER: Final = "Removing old cache file not in use"


def hydrate_file(path: Path | str, *,
                 block: int = HYDRATE_BLOCK_BYTES,
                 progress: Any = None,
                 cancel: Any = None) -> int:
    """Download a file by reading it through the mount.

    Args:
        path: The absolute path **inside the FUSE mount**. Reading anywhere else
            downloads nothing.
        block: Bytes per read. 4 MiB by default: large enough that the per-call
            FUSE overhead disappears, small enough that a cancel is noticed
            promptly and that memory stays flat on a 4 GB file.
        progress: ``(done, total)`` called after each block.
        cancel: ``() -> bool``. Checked between blocks.

    Returns:
        Bytes read.

    Opened with ``buffering=0`` and read in an explicit loop rather than handed
    to ``shutil`` or ``os.sendfile``. The fast paths are the trap here:
    ``copy_file_range()`` and ``sendfile()`` either fail on FUSE or copy holes
    as zeros, and a "download" that produced a file of zeros with a correct
    length would be far worse than one that failed.
    """
    path = Path(path)
    total = path.stat().st_size
    done = 0
    with open(path, "rb", buffering=0) as handle:
        while True:
            if cancel is not None and cancel():
                log.info("hydration of %s cancelled after %d bytes", path, done)
                break
            chunk = handle.read(block)
            if not chunk:
                break
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return done


class Pinner(QObject):
    """Pins, unpins, frees and downloads. At most three hydrations at a time.

    Args:
        account: The account.
        endpoint: ``() -> RcEndpoint | None`` for the mount's rc daemon.
        writer: The database writer.
        issues: The :class:`~onedriveui.sync.issues.IssueEngine`, so a refused
            eviction becomes a visible issue rather than an exception nobody
            sees.
        activity: The :class:`~onedriveui.sync.activity.ActivityFeed`.
        submit: ``(callable) -> None`` putting work on an ``IOPool`` thread.
            ``None`` runs it inline, which is what tests want and what the
            headless ``--state`` mode can live with.
        parent: Qt parent.

    Signals:
        progress: ``(rel_path, done, total)`` during a hydration.
    """

    progress = Signal(str, int, int)

    #: Concurrent hydrations. Equal to `MAX_TRANSFERS`, and for the same reason:
    #: beyond four parallel streams OneDrive starts returning HTTP 429, and a
    #: throttled download is slower than a sequential one.
    MAX_CONCURRENT_PINS: Final[int] = MAX_CONCURRENT_PINS

    def __init__(
        self,
        account: AccountInfo,
        *,
        endpoint: Any = None,
        writer: Any = None,
        issues: Any = None,
        activity: Any = None,
        submit: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._endpoint = endpoint or (lambda: None)
        self._writer = writer
        self._issues = issues
        self._activity = activity
        self._submit = submit
        self._active: dict[str, bool] = {}
        self._cancelled: set[str] = set()

    # ═════════════════════════════════════════════════════════════════════════
    # Pinning
    # ═════════════════════════════════════════════════════════════════════════

    def pin(self, rel_path: str, *, recursive: bool = False) -> None:
        """Mark an item "Always keep on this device" and download it.

        Args:
            rel_path: The item, relative to the sync root.
            recursive: Pin every file beneath a folder.

        The pin is recorded **before** the download starts, and that order
        matters: a crash mid-hydration leaves a recorded, unsatisfied pin that
        the next start-up finishes, whereas recording it afterwards would leave
        a half-downloaded file that nothing knows was wanted.
        """
        repo_files.set_pin(self.account.id, rel_path, mode="pinned",
                           writer=self._writer)
        self._cancelled.discard(rel_path)
        self._emit_state(rel_path, FileState.PINNED)
        for target in self._expand(rel_path, recursive):
            self._start_hydration(target)

    def unpin(self, rel_path: str, *, recursive: bool = False) -> None:
        """Stop keeping an item on this device. **Does not delete it.**

        The local copy stays until the user asks for the space back or rclone's
        own evictor takes it. Unpinning and freeing are two different requests,
        and conflating them would make "I don't need this pinned any more"
        silently take the file offline.
        """
        repo_files.clear_pin(self.account.id, rel_path, writer=self._writer)
        self._emit_state(rel_path, FileState.LOCAL)
        if recursive:
            for target in self._expand(rel_path, True):
                repo_files.clear_pin(self.account.id, target, writer=self._writer)

    # ═════════════════════════════════════════════════════════════════════════
    # Freeing
    # ═════════════════════════════════════════════════════════════════════════

    def free_up_space(self, rel_path: str) -> int:
        """Free one item's local copy.

        Args:
            rel_path: The item.

        Returns:
            Bytes reclaimed. ``0`` when it was refused.

        A ``Dirty`` sidecar or a queued upload is **refused** — those bytes
        exist here and nowhere else — and the refusal becomes a
        :class:`~onedriveui.models.SyncIssue` the user can see and act on, not
        an exception that reaches them as a traceback or, worse, nothing at all.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return 0
        try:
            info = vfs.disk_cache_info(endpoint)
            queue_names = {item.name for item in vfs.queue(endpoint)}
        except (RcError, DaemonUnavailable, OSError):
            log.warning("could not read the VFS before freeing %s", rel_path,
                        exc_info=True)
            return 0

        # A folder needs `evict_tree`, which checks every item under the prefix
        # against invariant I3 *before* unlinking any of them. `evict` on a
        # directory finds no sidecar and returns zero, so "Free up space" on a
        # folder — which is how the file-manager menu is almost always used —
        # reclaimed nothing at all and said so with a silent 0.
        target = Path(self.account.sync_root).expanduser() / rel_path
        try:
            is_folder = target.is_dir()
        except OSError:
            is_folder = False

        try:
            freed = (vfs.evict_tree(info, rel_path, queue_names) if is_folder
                     else vfs.evict(info, rel_path, queue_names))
        except SafetyRefusal as refusal:
            self._refuse(rel_path, str(refusal))
            return 0
        except OSError:
            log.error("could not free %s", rel_path, exc_info=True)
            return 0

        repo_files.clear_pin(self.account.id, rel_path, writer=self._writer)
        self._emit_state(rel_path, FileState.ONLINE_ONLY)
        self._record(rel_path, "freed", freed)
        log.info("freed %d bytes by evicting %s", freed, rel_path)
        return freed

    def free_up_all(self) -> int:
        """Free every cached file that is safe to free.

        Returns:
            Bytes reclaimed.

        Dirty and queued items are skipped rather than refused as a batch: one
        un-uploaded file must not stop the other nine thousand being reclaimed,
        and the skipped ones are reported individually.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return 0
        try:
            info = vfs.disk_cache_info(endpoint)
            queue_names = {item.name for item in vfs.queue(endpoint)}
        except (RcError, DaemonUnavailable, OSError):
            log.warning("could not read the VFS before freeing everything",
                        exc_info=True)
            return 0

        freed = 0
        for entry in vfs.scan(info, generation=0):
            if entry.dirty:
                continue
            try:
                freed += vfs.evict(info, entry.rel_path, queue_names)
            except SafetyRefusal as refusal:
                self._refuse(entry.rel_path, str(refusal))
            except OSError:
                log.warning("could not free %s", entry.rel_path, exc_info=True)
        log.info("freed %d bytes for %s", freed, self.account.id)
        return freed

    def _refuse(self, rel_path: str, detail: str) -> None:
        """Turn a refusal into something the user can see and act on."""
        log.warning("refusing to free %s: %s", rel_path, detail)
        if self._issues is not None:
            self._issues.raise_issue(IssueCode.FILE_IN_USE, rel_path=rel_path,
                                     detail=detail)

    # ═════════════════════════════════════════════════════════════════════════
    # Bulk download
    # ═════════════════════════════════════════════════════════════════════════

    def download_all(self) -> None:
        """"Download all files": hydrate the whole drive, three at a time.

        Deliberately not "start every file at once". Beyond four parallel
        streams OneDrive returns HTTP 429, and rclone's retry then makes the
        whole batch slower than a queue of three would have been.
        """
        root = Path(self.account.sync_root).expanduser()
        for path in sorted(root.rglob("*")):
            if path.is_file():
                self.pin(str(path.relative_to(root)))

    def cancel(self, rel_path: str) -> None:
        """Stop hydrating one item. Whatever arrived stays cached."""
        self._cancelled.add(rel_path)
        self._active.pop(rel_path, None)

    def active(self) -> int:
        """How many hydrations are running."""
        return len(self._active)

    def sizing(self, rel_path: str) -> tuple[int, int]:
        """``(bytes local, bytes total)`` for a progress bar.

        The local half comes from ``SEEK_DATA``/``SEEK_HOLE``, not from the
        sidecar: the kernel updates it synchronously as bytes land, whereas the
        sidecar is only rewritten when the item is released — a measured ~10 s
        lag, which is an eternity in a progress bar. Never from ``st_size``
        either: the cache file is preallocated to the object's full remote size
        on first open, so a file holding 192 KiB reports 50 MB.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return (0, 0)
        try:
            info = vfs.disk_cache_info(endpoint)
            data = vfs.data_path(info, rel_path)
            extents = vfs.local_extents(data)
        except (RcError, DaemonUnavailable, OSError):
            return (0, 0)
        local = sum(size for _pos, size in extents)
        try:
            total = os.path.getsize(data)
        except OSError:
            total = 0
        return (local, total)

    # ═════════════════════════════════════════════════════════════════════════
    # Internals
    # ═════════════════════════════════════════════════════════════════════════

    def _expand(self, rel_path: str, recursive: bool) -> list[str]:
        root = Path(self.account.sync_root).expanduser()
        target = root / rel_path
        if not recursive or not target.is_dir():
            return [rel_path]
        return [str(p.relative_to(root)) for p in sorted(target.rglob("*"))
                if p.is_file()]

    def _start_hydration(self, rel_path: str) -> None:
        if len(self._active) >= self.MAX_CONCURRENT_PINS:
            # Deliberately dropped rather than queued in memory: the pin row is
            # already on disk and unsatisfied, so the next sweep picks it up.
            # An in-memory queue would lose the same work on a crash.
            log.debug("%d hydrations already running; %s waits for a sweep",
                      len(self._active), rel_path)
            return
        self._active[rel_path] = True
        work = lambda: self._hydrate(rel_path)      # noqa: E731
        if self._submit is not None:
            self._submit(work)
        else:
            work()

    def _hydrate(self, rel_path: str) -> None:
        path = Path(self.account.sync_root).expanduser() / rel_path
        try:
            hydrate_file(
                path,
                progress=lambda done, total: self._on_progress(rel_path, done, total),
                cancel=lambda: rel_path in self._cancelled)
        except OSError:
            log.warning("could not hydrate %s", rel_path, exc_info=True)
            self._active.pop(rel_path, None)
            return
        self._active.pop(rel_path, None)
        if rel_path not in self._cancelled:
            repo_files.mark_pin_satisfied(self.account.id, rel_path,
                                          writer=self._writer)
            self._record(rel_path, "pinned", 0)

    def _on_progress(self, rel_path: str, done: int, total: int) -> None:
        self.progress.emit(rel_path, done, total)
        BUS.pin_progress.emit(rel_path, done, total)

    def _emit_state(self, rel_path: str, state: FileState) -> None:
        """Tell the UI and the Nautilus IPC that one file's badge moved."""
        BUS.file_state_changed.emit(self.account.id, rel_path, FileStatus(
            rel_path=rel_path, state=state,
            pinned=state is FileState.PINNED))

    def _record(self, rel_path: str, verb: str, size: int) -> None:
        if self._activity is None:
            return
        from onedriveui.models import ActivityVerb

        self._activity.record(
            rel_path,
            ActivityVerb.FREED if verb == "freed" else ActivityVerb.PINNED,
            size=size)


class RepinWatcher(QObject):
    """Puts back the pins rclone's own evictor took away.

    rclone's VFS cache evicts by age (``--vfs-cache-max-age``) and by size cap
    (``--vfs-cache-max-size``), and it has no idea that a user asked for a
    particular file to be kept. Left alone, "Always keep on this device" simply
    stops being true after a week — silently, with the badge still showing.

    Two detectors, because either alone misses cases:

    * the ``pins`` table's unsatisfied rows, which survive a restart and catch
      anything evicted while we were not running;
    * the journal line rclone writes when it evicts
      (:data:`EVICTOR_LOG_MARKER`), which catches it within a cycle.

    Args:
        pinner: The :class:`Pinner` to re-queue through.
        account: The account.
        parent: Qt parent.
    """

    repinned = Signal(str)

    def __init__(self, pinner: Pinner, account: AccountInfo,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pinner = pinner
        self.account = account

    def sweep(self) -> list[str]:
        """Re-queue every pin that is no longer satisfied.

        Returns:
            The paths re-queued.
        """
        try:
            unsatisfied = repo_files.unsatisfied_pins(self.account.id)
        except Exception:  # noqa: BLE001 - a sweep failure is not worth a crash
            log.error("could not read the pin table", exc_info=True)
            return []
        repinned: list[str] = []
        for pin in unsatisfied:
            log.info("re-hydrating %s: the evictor took it back", pin.rel_path)
            self._pinner.pin(pin.rel_path)
            self.repinned.emit(pin.rel_path)
            repinned.append(pin.rel_path)
        return repinned

    def on_log_lines(self, lines: Iterable[str]) -> list[str]:
        """Watch the mount's journal for the evictor's own confessions.

        Args:
            lines: Journal lines.

        Returns:
            The paths re-queued.
        """
        repinned: list[str] = []
        pinned = {pin.rel_path for pin in repo_files.pins(self.account.id)}
        for line in lines:
            if EVICTOR_LOG_MARKER not in (line or ""):
                continue
            for rel_path in pinned:
                if rel_path in line:
                    log.info("the evictor removed pinned %s; re-hydrating",
                             rel_path)
                    self._pinner.pin(rel_path)
                    self.repinned.emit(rel_path)
                    repinned.append(rel_path)
        return repinned
