"""Version history: ours, honestly labelled, next to Microsoft's that we cannot read.

OneDrive keeps real server-side version history. rclone can **delete** versions
(``--onedrive-no-versions``, and ``operations/cleanup``, which invariant I8
forbids for exactly this reason) and can neither list nor restore them. There is
no rc call that answers "what did this file look like last Tuesday?".

So there are two version stories in this client and they are kept visibly apart:

* **Ours.** bisync's ``--backup-dir`` writes the previous copy of every file it
  overwrites into a timestamped directory. Indexing those gives a real, working,
  restorable history for anything the offline folder has touched — and only for
  that.
* **Microsoft's.** Complete, covering every change from every device, and
  reachable only through the web interface. It gets a deep link and a sentence
  explaining why, rather than being quietly omitted so the feature looks whole.

Restoring has one rule that is not obvious and matters enormously: **the current
copy is captured as a new version first.** A user restoring Tuesday's draft has
not decided to discard today's work — they usually want to look at it and often
want to come back. Overwriting without capturing makes "restore" a destructive,
unrepeatable operation, and this is a version-history feature: being able to undo
is the entire point.
"""

from __future__ import annotations

import logging
import posixpath
import re
import sqlite3
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.constants import REMOTE_VERSIONS_DIR, WEB_ROOT
from onedriveui.data import db, repo_files
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import (
    AccountInfo,
    RunRecord,
    VersionEntry,
    utcnow_iso,
)
from onedriveui.rc import ops
from onedriveui.strings import DIALOG

log = logging.getLogger(__name__)

__all__ = ["VersionStore", "run_suffix", "backup_dir_for", "web_version_url",
           "BACKUP_STAMP_RE"]

#: bisync's `--backup-dir` naming, as this client writes it: one directory per
#: run, named by the run's UTC start.
BACKUP_STAMP_RE: Final = re.compile(r"^\d{8}T\d{6}Z$")


def run_suffix(when: str | None = None) -> str:
    """The directory name for one run's backups: ``20260831T120000Z``.

    Compact and sortable, and deliberately free of the colons an ISO stamp
    carries — a colon is an invalid character in a OneDrive path, so a
    backup directory named with one would be unsyncable by the very sync that
    created it.
    """
    stamp = (when or utcnow_iso()).replace("-", "").replace(":", "")
    return stamp.replace("Z", "Z") if stamp.endswith("Z") else f"{stamp}Z"


def backup_dir_for(run_id: str, when: str | None = None) -> str:
    """Where one run's overwritten copies go."""
    return posixpath.join(REMOTE_VERSIONS_DIR, run_suffix(when))


def web_version_url(account: AccountInfo, item_id: str = "") -> str:
    """Microsoft's own version history, in the browser.

    The only route to it. rclone can delete versions and cannot list or restore
    them, so the deep link is the honest answer rather than a gap in the UI.
    """
    if item_id:
        return f"{WEB_ROOT}?id={item_id}&view=versionhistory"
    return WEB_ROOT


class VersionStore(QObject):
    """Indexes bisync's backup directories and restores from them.

    Args:
        account: The account.
        endpoint: ``() -> RcEndpoint | None`` for the daemon to ask.
        writer: The database writer.
        parent: Qt parent.

    Signals:
        indexed: ``(run_id, count)`` after a run's backups are catalogued.
        restored: The restored :class:`~onedriveui.models.VersionEntry`.
    """

    indexed = Signal(str, int)
    restored = Signal(VersionEntry)

    #: Why "Version history" opens a browser rather than a list.
    WHY_WEB: Final = DIALOG.VERSION_HISTORY_WHY

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
    # Indexing
    # ═════════════════════════════════════════════════════════════════════════

    def index_run(self, run: RunRecord) -> int:
        """Catalogue the copies one bisync run displaced.

        Args:
            run: The finished run.

        Returns:
            How many versions were recorded.

        Indexed rather than discovered on demand: listing the backup tree costs
        one Graph request per directory (OneDrive has ``ListR = false``), so
        doing it once per run is cheap and doing it per "show me the versions of
        this file" click is not.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return 0
        directory = backup_dir_for(run.run_id, run.started_at)
        try:
            nodes = ops.list_dir(self.account.fs, directory, ep=endpoint,
                                 files_only=True, recurse=True)
        except (RcError, DaemonUnavailable, OSError):
            log.debug("no backup directory for run %s", run.run_id, exc_info=True)
            return 0

        recorded = 0
        for node in nodes:
            rel_path = node.rel_path[len(directory):].lstrip("/")
            if not rel_path:
                continue
            entry = VersionEntry(
                account_id=self.account.id, rel_path=rel_path,
                backup_path=node.rel_path, side="local",
                captured_at=run.started_at or utcnow_iso(),
                size=node.size, quickxor=node.quickxor,
                reason="overwritten by sync", run_id=run.run_id,
            )
            try:
                repo_files.add_version(entry, writer=self._writer)
                recorded += 1
            except Exception:  # noqa: BLE001
                log.warning("could not record a version of %s", rel_path,
                            exc_info=True)
        if recorded:
            log.info("indexed %d versions from run %s", recorded, run.run_id)
        self.indexed.emit(run.run_id, recorded)
        return recorded

    def versions_for(self, rel_path: str) -> list[VersionEntry]:
        """This file's history — **ours only**, newest first.

        Covers what the offline folder overwrote and nothing else. A file only
        ever changed from the phone has a complete history in OneDrive and none
        here, which is why the UI puts :data:`WHY_WEB` and a link to the web
        version history beside this list rather than presenting it as the
        whole story.
        """
        return repo_files.versions_for(self.account.id, rel_path)

    # ═════════════════════════════════════════════════════════════════════════
    # Restoring
    # ═════════════════════════════════════════════════════════════════════════

    def restore_version(self, version_id: int) -> bool:
        """Put an older copy back — **capturing the current one first**.

        Args:
            version_id: The row to restore.

        Returns:
            True when it was restored.

        The capture is not a nicety. A user restoring Tuesday's draft has not
        decided to discard today's work; they usually want to look at the old
        one and often want to come back. Overwriting without capturing makes
        "restore" destructive and unrepeatable, which in a version-history
        feature defeats the purpose entirely.

        Both steps are server-side moves, so nothing is downloaded and a 4 GB
        file restores as fast as a text file.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return False
        entry = self._entry(version_id)
        if entry is None:
            log.warning("no version %s to restore", version_id)
            return False

        # Step one: today's copy becomes a version of its own.
        captured = posixpath.join(
            REMOTE_VERSIONS_DIR, run_suffix(), entry.rel_path)
        try:
            ops.copyfile(self.account.fs, entry.rel_path,
                         self.account.fs, captured, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            # Refuse rather than proceed. Restoring over an uncaptured current
            # copy is the one outcome this method exists to prevent.
            log.error("could not capture the current copy of %s; refusing to "
                      "restore over it", entry.rel_path, exc_info=True)
            return False

        repo_files.add_version(VersionEntry(
            account_id=self.account.id, rel_path=entry.rel_path,
            backup_path=captured, side="local", captured_at=utcnow_iso(),
            reason="replaced by a restore"), writer=self._writer)

        # Step two: the old copy comes back.
        try:
            ops.copyfile(self.account.fs, entry.backup_path,
                         self.account.fs, entry.rel_path, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.error("could not restore %s; the current copy is captured at %s",
                      entry.rel_path, captured, exc_info=True)
            return False

        log.info("restored %s from %s (the previous copy is at %s)",
                 entry.rel_path, entry.backup_path, captured)
        self.restored.emit(entry)
        return True

    def delete_version(self, version_id: int) -> bool:
        """Remove one of **our** backup copies.

        Only ever touches a file under ``.onedriveui-versions/``. It cannot
        reach a live file, and it cannot reach Microsoft's own version history —
        the endpoint that would (``operations/cleanup``) is forbidden by
        invariant I8 precisely because it destroys all of it at once.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return False
        entry = self._entry(version_id)
        if entry is None:
            return False
        if not entry.backup_path.startswith(f"{REMOTE_VERSIONS_DIR}/"):
            log.error("refusing to delete %s: it is not under %s",
                      entry.backup_path, REMOTE_VERSIONS_DIR)
            return False
        try:
            ops.deletefile(self.account.fs, entry.backup_path, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.warning("could not delete the version at %s", entry.backup_path,
                        exc_info=True)
            return False
        return True

    def _entry(self, version_id: int) -> VersionEntry | None:
        """One `versions` row by id.

        Read through `db.open_ro()` rather than through `repo_files`, which
        offers `versions_for(account, rel_path)` but no by-id getter — and the
        caller of a restore has an id, not a path. A read-only connection on
        this thread is the documented way to query inside the GUI's budget.
        """
        try:
            row = db.open_ro().execute(
                "SELECT id, account_id, rel_path, backup_path, side, "
                "captured_at, size, quickxor, reason, run_id "
                "FROM versions WHERE id = ?", (int(version_id),)).fetchone()
        except sqlite3.Error:
            log.error("could not read version %s", version_id, exc_info=True)
            return None
        if row is None:
            return None
        return VersionEntry(
            id=row["id"], account_id=row["account_id"],
            rel_path=row["rel_path"], backup_path=row["backup_path"],
            side=row["side"] or "local", captured_at=row["captured_at"] or "",
            size=row["size"] or 0, quickxor=row["quickxor"] or "",
            reason=row["reason"] or "", run_id=row["run_id"] or "")

    def web_version_url(self, item_id: str = "") -> str:
        return web_version_url(self.account, item_id)

    def local_backup_root(self) -> Path:
        """Where bisync should point ``--backup-dir`` for this account."""
        return Path(self.account.sync_root).expanduser() / REMOTE_VERSIONS_DIR


# ═════════════════════════════════════════════════════════════════════════════
# The frozen module-level surface (CONTRACTS §10.9)
#
# Thin wrappers over `VersionStore`. The class is where the injectable endpoint
# and writer live, which is what makes the whole thing testable without a
# daemon; these exist because the contract is written in terms of functions and
# the UI calls them that way.
# ═════════════════════════════════════════════════════════════════════════════

def _store(account: AccountInfo, endpoint: Any = None,
           writer: Any = None) -> VersionStore:
    return VersionStore(account, endpoint=endpoint, writer=writer)


def versions_for(account: AccountInfo, rel_path: str, *,
                 endpoint: Any = None) -> list[VersionEntry]:
    """This file's history — **our own `--backup-dir` snapshots only**.

    OneDrive's server-side version history is real and complete; rclone can only
    delete versions, never list or restore them, so it is a web deep link
    (:data:`VersionStore.WHY_WEB`) rather than part of this list.
    """
    return _store(account, endpoint).versions_for(rel_path)


def restore_version(account: AccountInfo, version_id: int, *,
                    endpoint: Any = None, writer: Any = None) -> bool:
    """Restore an older copy, **capturing the current one as a version first**."""
    return _store(account, endpoint, writer).restore_version(version_id)


def delete_version(account: AccountInfo, version_id: int, *,
                   endpoint: Any = None, writer: Any = None) -> bool:
    """Delete one of our own backup copies. Never a live file."""
    return _store(account, endpoint, writer).delete_version(version_id)


def index_run(account: AccountInfo, run: RunRecord, *,
              endpoint: Any = None, writer: Any = None) -> int:
    """Catalogue the copies one bisync run displaced."""
    return _store(account, endpoint, writer).index_run(run)


def backup_dirs(account: AccountInfo) -> Path:
    """Where `--backup-dir` writes for this account."""
    return _store(account).local_backup_root()
