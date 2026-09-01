"""Our own recycle bin, because Microsoft's cannot be listed.

Deleting a file through the FUSE mount sends it to **Microsoft's** cloud recycle
bin. That is fine — it is recoverable, it is what Windows does — except that
rclone offers no way to list it, so the client can show the user a "Recycle bin"
that is permanently empty while their file sits in one they cannot see.

So a delete made *through this client's UI* does something we can account for
instead: a **server-side move** into ``.onedriveui-trash/<timestamp>/``. One
``operations/movefile``, no bytes over the wire, instant on a 4 GB file, and the
result is a directory we can list, restore from and expire on a schedule.

Two hard rules, and they are both about not destroying things:

**``operations/cleanup`` is never called.** Invariant I8. On OneDrive that
endpoint does not empty the recycle bin — it **permanently deletes every
previous version of every file**, and it is unsupported on Personal accounts
entirely. The name is a trap: nothing in this codebase calls it, and a test
greps the source to keep it that way.

**Purging is by retention, and the retention is Microsoft's.** Thirty days for
personal accounts, ninety-three for business, matching what the user's cloud
recycle bin would have done. A shorter window would delete things they still
expected to be able to get back.

Microsoft's own bin stays reachable through :func:`web_recyclebin_url`, labelled
as what it is, because a file deleted from the file manager rather than from our
UI is genuinely in there and not here.
"""

from __future__ import annotations

import logging
import posixpath
from typing import Any

from PySide6.QtCore import QObject, Signal

from onedriveui.constants import (
    REMOTE_TRASH_DIR,
    TRASH_RETENTION_DAYS_BUSINESS,
    TRASH_RETENTION_DAYS_PERSONAL,
    WEB_RECYCLE_BIN,
)
from onedriveui.data import repo_files
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import (
    AccountInfo,
    AccountKind,
    RcEndpoint,
    TrashEntry,
    parse_iso,
    utcnow_iso,
)
from onedriveui.rc import ops

log = logging.getLogger(__name__)

__all__ = ["TrashBin", "retention_days", "trash_path_for", "web_recyclebin_url"]


def retention_days(account: AccountInfo) -> int:
    """How long our own trash keeps an item, matching Microsoft's own policy."""
    return (TRASH_RETENTION_DAYS_BUSINESS
            if account.kind is AccountKind.BUSINESS
            else TRASH_RETENTION_DAYS_PERSONAL)


def trash_path_for(rel_path: str, when: str | None = None) -> str:
    """Where a deleted item goes inside the remote trash.

    Args:
        rel_path: The item's path.
        when: The deletion stamp. Defaults to now.

    Returns:
        ``.onedriveui-trash/<timestamp>/<original path>``.

    The timestamp directory is what makes restoring unambiguous: two files with
    the same name deleted an hour apart land in different folders, so restoring
    the first cannot silently overwrite the second — and the original relative
    path is preserved underneath, so a restore knows exactly where it came from
    without a database lookup.
    """
    stamp = (when or utcnow_iso()).replace(":", "-")
    return posixpath.join(REMOTE_TRASH_DIR, stamp, rel_path.lstrip("/"))


def web_recyclebin_url(account: AccountInfo) -> str:
    """Microsoft's own recycle bin, which rclone cannot list.

    A file deleted through the file manager rather than through this client is
    genuinely in there, so the link is offered rather than the absence explained
    away.
    """
    return WEB_RECYCLE_BIN


class TrashBin(QObject):
    """Soft delete, restore, and expiry — all server-side.

    Args:
        account: The account.
        endpoint: ``() -> RcEndpoint | None`` for the daemon to ask.
        writer: The database writer.
        parent: Qt parent.

    Signals:
        deleted: A new :class:`~onedriveui.models.TrashEntry`.
        restored: The restored entry's id.
    """

    deleted = Signal(TrashEntry)
    restored = Signal(int)

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

    # ═════════════════════════════════════════════════════════════════════════
    # Deleting
    # ═════════════════════════════════════════════════════════════════════════

    def soft_delete(self, rel_path: str, *, is_dir: bool = False,
                    size: int = 0) -> TrashEntry | None:
        """Move an item into our own trash. **One move, zero deletes.**

        Args:
            rel_path: The item to delete.
            is_dir: Whether it is a directory.
            size: Its size, for the "12.4 GB in the recycle bin" line.

        Returns:
            The recorded entry, or ``None`` when the move failed.

        Server-side: the bytes never leave OneDrive, so this is instant on a
        4 GB file and costs nothing in bandwidth. Nothing is deleted — the item
        is exactly where it was, under a different name — which is what makes
        the restore below a rename rather than an upload.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return None

        when = utcnow_iso()
        destination = trash_path_for(rel_path, when)
        try:
            ops.movefile(self.account.fs, rel_path,
                         self.account.fs, destination, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.error("could not move %s into the trash", rel_path, exc_info=True)
            return None

        entry = TrashEntry(
            account_id=self.account.id, rel_path=rel_path,
            trash_path=destination, is_dir=is_dir, size=size,
            deleted_at=when, purge_after=self._purge_after(when),
        )
        try:
            entry_id = repo_files.add_trash(entry, writer=self._writer)
            entry = _with(entry, id=entry_id or 0)
        except Exception:  # noqa: BLE001
            log.error("could not record the trash entry for %s", rel_path,
                      exc_info=True)

        log.info("soft-deleted %s to %s", rel_path, destination)
        self.deleted.emit(entry)
        return entry

    def _purge_after(self, when: str) -> str:
        import datetime as _dt

        deleted_at = parse_iso(when) or _dt.datetime.now(_dt.UTC)
        due = deleted_at + _dt.timedelta(days=retention_days(self.account))
        return due.isoformat().replace("+00:00", "Z")

    # ═════════════════════════════════════════════════════════════════════════
    # Restoring
    # ═════════════════════════════════════════════════════════════════════════

    def restore_from_trash(self, trash_id: int) -> bool:
        """Put an item back where it came from.

        Args:
            trash_id: The row id.

        Returns:
            True when it was restored.

        The original path is stored on the row, so this is one server-side move
        back — no download, no re-upload, and the file keeps its identity in
        OneDrive rather than becoming a new item with a new version history.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return False
        entry = self._entry(trash_id)
        if entry is None:
            log.warning("no trash entry %s to restore", trash_id)
            return False

        try:
            ops.movefile(self.account.fs, entry.trash_path,
                         self.account.fs, entry.rel_path, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.error("could not restore %s", entry.rel_path, exc_info=True)
            return False

        repo_files.mark_restored(trash_id, writer=self._writer)
        log.info("restored %s from the trash", entry.rel_path)
        self.restored.emit(trash_id)
        return True

    def trash_items(self) -> list[TrashEntry]:
        """Everything currently in our own trash, newest first."""
        return repo_files.trash_items(self.account.id)

    def _entry(self, trash_id: int) -> TrashEntry | None:
        for entry in self.trash_items():
            if entry.id == trash_id:
                return entry
        return None

    # ═════════════════════════════════════════════════════════════════════════
    # Expiry
    # ═════════════════════════════════════════════════════════════════════════

    def purge_expired(self) -> int:
        """Delete items past their retention date. **Never ``operations/cleanup``.**

        Returns:
            How many were purged.

        Invariant I8. ``operations/cleanup`` does not empty a recycle bin on
        OneDrive: it permanently deletes **every previous version of every
        file**, and it is unsupported on Personal accounts entirely. Purging is
        done item by item, with ``operations/purge`` on the specific
        timestamped directory — which touches nothing outside it.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return 0
        due = repo_files.purge_due(self.account.id)
        purged = 0
        for entry in due:
            try:
                if entry.is_dir:
                    ops.purge(self.account.fs, entry.trash_path, ep=endpoint)
                else:
                    ops.deletefile(self.account.fs, entry.trash_path, ep=endpoint)
            except (RcError, DaemonUnavailable, OSError):
                log.warning("could not purge %s", entry.trash_path, exc_info=True)
                continue
            purged += 1
        if purged:
            log.info("purged %d expired trash items for %s", purged,
                     self.account.id)
        return purged

    def web_recyclebin_url(self) -> str:
        """Microsoft's own bin, for the deletions we did not make."""
        return web_recyclebin_url(self.account)


def _with(entry: TrashEntry, **changes: Any) -> TrashEntry:
    from dataclasses import replace

    return replace(entry, **changes)


# ═════════════════════════════════════════════════════════════════════════════
# The frozen module-level surface (CONTRACTS §10.9)
#
# Thin wrappers over `TrashBin`. The class carries the injectable endpoint and
# writer, which is what makes it testable without a daemon; these exist because
# the contract is written in terms of functions and the UI calls them that way.
# ═════════════════════════════════════════════════════════════════════════════

def _bin(ep: RcEndpoint | None, account: AccountInfo,
         writer: Any = None) -> TrashBin:
    return TrashBin(account, endpoint=lambda: ep, writer=writer)


def soft_delete(ep: RcEndpoint, account: AccountInfo, rel_path: str, *,
                is_dir: bool = False, size: int = 0,
                writer: Any = None) -> TrashEntry | None:
    """Server-side move into ``.onedriveui-trash/<ts>/``. One move, zero deletes."""
    return _bin(ep, account, writer).soft_delete(rel_path, is_dir=is_dir,
                                                 size=size)


def restore_from_trash(ep: RcEndpoint, account: AccountInfo, trash_id: int, *,
                       writer: Any = None) -> bool:
    """Move an item back to the path it was deleted from."""
    return _bin(ep, account, writer).restore_from_trash(trash_id)


def trash_items(account: AccountInfo) -> list[TrashEntry]:
    """Everything in our own trash, newest first."""
    return _bin(None, account).trash_items()


def purge_expired(ep: RcEndpoint, account: AccountInfo, *,
                  writer: Any = None) -> int:
    """Delete items past their retention date.

    Item by item, with ``operations/purge`` on one timestamped directory.
    ``operations/cleanup`` appears nowhere in this module: on OneDrive it
    permanently deletes every previous version of every file, and it is
    unsupported on Personal accounts entirely (invariant I8).
    """
    return _bin(ep, account, writer).purge_expired()
