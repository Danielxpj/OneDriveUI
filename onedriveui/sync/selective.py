""""Choose folders": what to sync, and what happens to what you deselect.

Two things make this harder than it looks, and both are safety properties rather
than features.

**A filters change without an immediate ``--resync`` locks the account out of
syncing.** bisync compares the current filters against the digest recorded with
the last listing; if they differ it aborts as critical and refuses to run until a
resync happens. So the write and the resync are one transaction (invariant I11),
and :class:`~onedriveui.rc.filters.FiltersTransaction` makes forgetting the
second half undo the first — a crash between them restores the previous file and
digest, leaving the account exactly as syncable as it was.

**Deselecting a folder must never delete it before the resync has succeeded.**
The order is: write filters, resync, *then* prune. Doing it the other way round
means a failed resync has already destroyed the local copies of files that are
still perfectly fine in the cloud — and the user's next action is usually to
re-tick the folder and re-download 40 GB they already had.

And the prune itself goes to the **freedesktop trash**, never to ``unlink()``
(invariant I10). Unticking a folder in a settings dialog is not a delete
confirmation, and a user who did it by accident has to be able to get their
files back without a round trip through the cloud.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.data import repo_files
from onedriveui.errors import SafetyRefusal
from onedriveui.models import AccountInfo, RunVerdict
from onedriveui.platform import trash
from onedriveui.rc import filters

log = logging.getLogger(__name__)

__all__ = ["SelectiveSync", "PruneResult"]


class PruneResult:
    """What a prune actually did, for the confirmation the user sees.

    Attributes:
        trashed: Paths moved to the freedesktop trash.
        skipped: Paths left alone, with the reason.
        bytes_freed: How much space came back.
    """

    __slots__ = ("trashed", "skipped", "bytes_freed")

    def __init__(self) -> None:
        self.trashed: list[str] = []
        self.skipped: list[tuple[str, str]] = []
        self.bytes_freed = 0

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"PruneResult(trashed={len(self.trashed)}, "
                f"skipped={len(self.skipped)}, bytes={self.bytes_freed})")


class SelectiveSync(QObject):
    """Reads and writes the folder selection, safely.

    Args:
        account: The account.
        writer: The database writer.
        resync: ``(account) -> RunVerdict`` performing the mandatory resync.
            Injected because it needs an answered decision (invariant I15) and
            that gate belongs to the Supervisor, not here.
        parent: Qt parent.

    Signals:
        applied: The new set of excluded paths.
        pruned: The :class:`PruneResult`.
    """

    applied = Signal(list)
    pruned = Signal(object)

    def __init__(
        self,
        account: AccountInfo,
        *,
        writer: Any = None,
        resync: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._writer = writer
        self._resync = resync

    # ═════════════════════════════════════════════════════════════════════════
    # Reads
    # ═════════════════════════════════════════════════════════════════════════

    def selection(self) -> dict[str, bool]:
        """``{rel_path: selected}`` for the folder picker's tri-state tree."""
        return repo_files.selection(self.account.id)

    def excluded(self) -> list[str]:
        """The folders currently excluded from sync."""
        return repo_files.excluded_paths(self.account.id)

    def preview(self, excluded: list[str]) -> PruneResult:
        """What :meth:`apply` would remove, without removing anything.

        The dialog has to be able to say "this will move 4 231 files (12.4 GB)
        to the trash" *before* the user commits, because "Choose folders" looks
        like a filter and behaves like a delete.
        """
        result = PruneResult()
        root = Path(self.account.sync_root).expanduser()
        for rel_path in excluded:
            target = root / rel_path
            if not target.exists():
                continue
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    result.trashed.append(str(path.relative_to(root)))
                    try:
                        result.bytes_freed += path.stat().st_size
                    except OSError:
                        pass
        return result

    # ═════════════════════════════════════════════════════════════════════════
    # Writing
    # ═════════════════════════════════════════════════════════════════════════

    def apply(self, excluded: list[str], *, prune: bool = True) -> PruneResult:
        """Change the selection: filters, then resync, then prune. In that order.

        Args:
            excluded: Folders to stop syncing, relative to the sync root.
            prune: Whether to reclaim the local copies afterwards. ``False``
                leaves them on disk, which is what a user who unticked a folder
                only to stop *uploading* it wants.

        Returns:
            A :class:`PruneResult` describing what was reclaimed.

        Raises:
            SafetyRefusal: The filters changed and the resync did not succeed.
                Invariant I11. The previous filters file and digest are restored
                *before* this propagates, so the account is left exactly as
                syncable as it was — and, because the prune is below the resync
                and not above it, **nothing local has been touched**. Failing
                loudly is the point: a settings page that quietly reported
                success would leave the user believing a selection took effect
                when it did not.
        """
        result = PruneResult()
        with filters.rewrite(self.account.id, excluded) as txn:
            if not txn.changed:
                log.info("the folder selection is unchanged; nothing to do")
                self._persist(excluded)
                return result

            verdict = self._run_resync()
            if verdict is not RunVerdict.OK:
                # Falling out of the block *without* calling `resynced()` is how
                # the rollback is triggered: `FiltersTransaction.__exit__`
                # restores the previous file and digest and raises SafetyRefusal.
                # Nothing local has been touched, because the prune is below this
                # line and not above it — which is the ordering this whole module
                # exists to get right.
                log.error("the resync after a filters change did not succeed "
                          "(%s); rolling back and pruning nothing",
                          getattr(verdict, "value", verdict))
            else:
                txn.resynced()

        self._persist(excluded)
        self.applied.emit(list(excluded))
        if prune:
            result = self.prune_local(excluded)
        return result

    def _run_resync(self) -> RunVerdict:
        if self._resync is None:
            log.error("no resync runner wired; a filters change cannot be "
                      "committed without one (invariant I11)")
            return RunVerdict.UNKNOWN
        try:
            return self._resync(self.account)
        except SafetyRefusal:
            raise
        except Exception:  # noqa: BLE001
            log.error("the resync failed", exc_info=True)
            return RunVerdict.UNKNOWN

    def _persist(self, excluded: list[str]) -> None:
        for rel_path in excluded:
            repo_files.set_selection(self.account.id, rel_path, False,
                                     writer=self._writer)

    # ═════════════════════════════════════════════════════════════════════════
    # Pruning
    # ═════════════════════════════════════════════════════════════════════════

    def prune_local(self, excluded: list[str]) -> PruneResult:
        """Move deselected folders to the trash. **Never ``unlink()``.**

        Args:
            excluded: The folders to reclaim.

        Returns:
            What was moved and what was skipped.

        Invariant I10: this goes to the freedesktop trash. Unticking a folder in
        a settings dialog is not a delete confirmation, and a user who did it by
        accident must be able to get their files back from the trash rather than
        by re-downloading 40 GB from the cloud.

        A path that is not actually under the sync root is skipped rather than
        trashed. `assert_trashable` is the second half of that check; the first
        is here, because a bug that fed this an absolute path from elsewhere
        would otherwise trash the wrong tree.
        """
        result = PruneResult()
        root = Path(self.account.sync_root).expanduser().resolve()
        for rel_path in excluded:
            target = (root / rel_path).resolve()
            if not target.exists():
                continue
            if root not in target.parents and target != root:
                result.skipped.append((rel_path, "outside the sync root"))
                log.error("refusing to prune %s: it is not under %s",
                          target, root)
                continue
            try:
                size = sum(p.stat().st_size for p in target.rglob("*")
                           if p.is_file())
                trash.assert_trashable(target)
                trash.trash_tree(target)
            except (SafetyRefusal, OSError) as exc:
                result.skipped.append((rel_path, str(exc)))
                log.warning("could not trash %s", target, exc_info=True)
                continue
            result.trashed.append(rel_path)
            result.bytes_freed += size
            log.info("moved %s to the trash (%d bytes)", target, size)

        self.pruned.emit(result)
        return result

    # ═════════════════════════════════════════════════════════════════════════
    # The mount's own view
    # ═════════════════════════════════════════════════════════════════════════

    def as_mount_excludes(self, excluded: list[str] | None = None) -> list[str]:
        """The same selection, as ``--exclude`` arguments for the mount.

        Args:
            excluded: The folders, or ``None`` to read them from the database.

        Returns:
            One rclone filter rule per excluded folder.

        The mount needs the exclusions too, and it needs them on the command
        line rather than in the filters file: a folder that is filtered out of
        bisync but still visible through the mount is a folder the user can
        open, edit and expect to sync. Two views of one selection is exactly the
        kind of disagreement this codebase is arranged to prevent.
        """
        paths = self.excluded() if excluded is None else excluded
        return [filters.exclude_rule(rel_path) for rel_path in paths]

    def exclude(self, rel_path: str) -> None:
        """Stop syncing one item, from a context menu or an issue.

        Records the exclusion and nothing else — no filters rewrite, no resync,
        no deletion. The batch is committed by :meth:`apply` when the user
        confirms, because a single right-click must not trigger a full resync of
        the account.
        """
        if not rel_path:
            return
        repo_files.set_selection(self.account.id, rel_path, False,
                                 writer=self._writer)
        log.info("marked %s excluded; apply() commits it", rel_path)
