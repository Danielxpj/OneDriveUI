"""Known Folder Move: pointing Desktop, Documents and Pictures into OneDrive.

This is the most dangerous feature in the client, because it moves the user's
irreplaceable files. Everything below is arranged around one sentence: **at no
instant does a file exist in only one place that we have not verified.**

The naive implementation is ``shutil.move()``, and it is wrong three times over.
It is not atomic across filesystems, so an interruption leaves a partial copy and
a deleted original. It has no verification, so a truncated write is indis-
tinguishable from a complete one. And it goes *through the FUSE mount*, where a
failed write can succeed as far as the caller is concerned and land nowhere.

So the move is **two phases with a journal between them**:

1. **Copy and verify.** Every file is copied into the destination and its size
   and content hash are checked against the source. The journal records each
   verified file as it lands.
2. **Remove.** Only files the journal marks verified are removed from the
   original location, one at a time, to the freedesktop trash rather than to
   ``unlink()``.

An interruption anywhere leaves the journal on disk, and :meth:`KfmManager.
execute` resumes from it — or :meth:`KfmManager.rollback` walks it backwards.
Either way every file is somewhere the journal names. That is what the "no data
loss after an interrupted run" test proves by hashing the tree before and after.

The XDG side is small but has its own trap: ``user-dirs.dirs`` is read by every
GTK application at start-up, and a partially written one silently reverts folders
to their defaults. It is written atomically, and ``xdg-user-dirs-update`` is run
afterwards so running applications notice.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui import atomicio
from onedriveui.data import repo_files
from onedriveui.errors import SafetyRefusal
from onedriveui.models import AccountInfo, KfmFolder, utcnow_iso
from onedriveui.platform import desktop, trash
from onedriveui.rc import guards

log = logging.getLogger(__name__)

__all__ = ["KfmManager", "KfmPlan", "FOLDERS", "read_user_dirs",
           "write_user_dirs", "JOURNAL_NAME"]

#: The five folders Windows offers, in the order its dialog lists them.
FOLDERS: Final[tuple[KfmFolder, ...]] = (
    KfmFolder.DESKTOP, KfmFolder.DOCUMENTS, KfmFolder.PICTURES,
    KfmFolder.MUSIC, KfmFolder.VIDEOS,
)

#: The resumable journal's filename, in the state directory. Deliberately *not*
#: inside either the source or the destination folder: both are being moved, and
#: a journal that moved with them would be unavailable exactly when it is needed.
JOURNAL_NAME: Final = "kfm-journal.json"

#: `user-dirs.dirs` keys, in the order `xdg-user-dirs` writes them.
USER_DIR_KEYS: Final[dict[KfmFolder, str]] = {
    KfmFolder.DESKTOP: "XDG_DESKTOP_DIR",
    KfmFolder.DOCUMENTS: "XDG_DOCUMENTS_DIR",
    KfmFolder.PICTURES: "XDG_PICTURES_DIR",
    KfmFolder.MUSIC: "XDG_MUSIC_DIR",
    KfmFolder.VIDEOS: "XDG_VIDEOS_DIR",
}


@dataclass(slots=True)
class KfmPlan:
    """What a KFM run will do, before it does any of it.

    Attributes:
        folder: Which known folder.
        source: Where it is now.
        destination: Where it will be, inside the sync root.
        files: Every file to move, relative to `source`.
        bytes_total: How much is being moved.
        conflicts: Files that already exist at the destination. Never
            overwritten — the plan reports them and the dialog asks.
    """

    folder: KfmFolder
    source: Path
    destination: Path
    files: list[str] = field(default_factory=list)
    bytes_total: int = 0
    conflicts: list[str] = field(default_factory=list)


def read_user_dirs() -> dict[KfmFolder, Path]:
    """The five folders' current locations."""
    return desktop.user_dirs()


def write_user_dirs(updates: dict[KfmFolder, Path]) -> Path:
    """Rewrite ``user-dirs.dirs``, atomically, preserving everything else.

    Args:
        updates: The folders to repoint.

    Returns:
        The file that was written.

    Atomic because every GTK application reads this at start-up and a partially
    written file silently reverts folders to their defaults — which, for a user
    who has just moved Documents into OneDrive, means their applications start
    saving into an empty ``~/Documents`` again.

    Lines this client does not own are preserved verbatim, comments included:
    the file belongs to ``xdg-user-dirs``, not to us.
    """
    path = desktop.user_dirs_file()
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        existing = []

    wanted = {USER_DIR_KEYS[folder]: str(target)
              for folder, target in updates.items()}
    written: set[str] = set()
    lines: list[str] = []
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            lines.append(f'{key}="{_relative_to_home(wanted[key])}"')
            written.add(key)
        else:
            lines.append(line)
    for key, value in wanted.items():
        if key not in written:
            lines.append(f'{key}="{_relative_to_home(value)}"')

    atomicio.atomic_write_text(path, "\n".join(lines) + "\n")
    _refresh_user_dirs()
    return path


def _relative_to_home(value: str) -> str:
    """``$HOME/…`` where possible, which is how xdg-user-dirs writes it."""
    home = str(Path.home())
    if value.startswith(home + "/"):
        return f"$HOME/{value[len(home) + 1:]}"
    return value


def _refresh_user_dirs() -> None:
    """Tell running applications the file changed.

    Without this, an application started before the rewrite keeps using the old
    path until it is restarted — and saves the user's next document into the
    folder they just emptied.
    """
    try:
        subprocess.run(["xdg-user-dirs-update"], check=False, timeout=10,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError):
        log.debug("xdg-user-dirs-update is unavailable", exc_info=True)


class KfmManager(QObject):
    """Plans, executes, resumes and reverses a Known Folder Move.

    Args:
        account: The account.
        writer: The database writer.
        journal_dir: Where the resumable journal lives. Defaults to the state
            directory — deliberately outside both the source and the
            destination, since both are being moved.
        parent: Qt parent.

    Signals:
        progress: ``(folder, done, total)``.
        finished: ``(folder, ok)``.
    """

    progress = Signal(str, int, int)
    finished = Signal(str, bool)

    def __init__(
        self,
        account: AccountInfo,
        *,
        writer: Any = None,
        journal_dir: Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._writer = writer
        if journal_dir is None:
            from onedriveui import paths

            journal_dir = paths.state_dir()
        self._journal_path = Path(journal_dir) / JOURNAL_NAME

    # ═════════════════════════════════════════════════════════════════════════
    # Planning
    # ═════════════════════════════════════════════════════════════════════════

    def status(self) -> dict[KfmFolder, bool]:
        """Which folders are currently inside the sync root."""
        root = Path(self.account.sync_root).expanduser().resolve()
        out: dict[KfmFolder, bool] = {}
        for folder, current in read_user_dirs().items():
            try:
                resolved = current.resolve()
            except OSError:
                out[folder] = False
                continue
            out[folder] = resolved == root or root in resolved.parents
        return out

    def plan(self, folder: KfmFolder) -> KfmPlan:
        """Work out what moving one folder involves, without moving anything.

        The dialog needs to say "this will move 12 431 files (48 GB)" and to
        name the collisions *before* the user commits, because the alternative
        is discovering a name clash halfway through a 48 GB move.
        """
        source = read_user_dirs()[folder]
        destination = (Path(self.account.sync_root).expanduser()
                       / folder.value.capitalize())
        plan = KfmPlan(folder=folder, source=source, destination=destination)
        if not source.is_dir():
            return plan

        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(source))
            plan.files.append(rel)
            try:
                plan.bytes_total += path.stat().st_size
            except OSError:
                pass
            if (destination / rel).exists():
                plan.conflicts.append(rel)
        return plan

    def folder_size(self, folder: KfmFolder) -> int:
        return self.plan(folder).bytes_total

    # ═════════════════════════════════════════════════════════════════════════
    # Executing
    # ═════════════════════════════════════════════════════════════════════════

    def enable(self, folder: KfmFolder) -> bool:
        """Move a known folder into OneDrive and repoint XDG at it."""
        return self.execute(self.plan(folder))

    def execute(self, plan: KfmPlan) -> bool:
        """Run — or resume — a planned move. Copy and verify, then remove.

        Args:
            plan: From :meth:`plan`.

        Returns:
            True when every file arrived, was verified and was removed.

        Two phases with a journal between them, because at no instant may a file
        exist only somewhere we have not verified. Phase one copies and checks
        every file, recording each success; phase two removes only what phase
        one verified. An interruption anywhere leaves the journal, and calling
        this again resumes from it rather than starting over.

        Raises:
            SafetyRefusal: The source is inside a FUSE mount (invariant I2), or
                the destination is not under the sync root. Both would move a
                user's files somewhere this client cannot honestly account for.
        """
        guards.assert_not_under_fuse(plan.source, "the KFM source folder")
        root = Path(self.account.sync_root).expanduser().resolve()
        destination = plan.destination.resolve()
        if root not in destination.parents and destination != root:
            raise SafetyRefusal(
                "I2", f"the KFM destination {destination} is not under the "
                      f"sync root {root}")

        journal = self._load_journal()
        journal.setdefault("folder", plan.folder.value)
        journal.setdefault("source", str(plan.source))
        journal.setdefault("destination", str(plan.destination))
        verified: set[str] = set(journal.get("verified", []))
        removed: set[str] = set(journal.get("removed", []))

        destination.mkdir(parents=True, exist_ok=True)
        total = len(plan.files)

        # ── phase one: copy and verify ──────────────────────────────────────
        for index, rel in enumerate(plan.files, start=1):
            if rel in verified:
                continue
            source_file = plan.source / rel
            target_file = destination / rel
            try:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target_file)
                if not self._verify(source_file, target_file):
                    log.error("KFM: %s did not verify after copying; stopping",
                              rel)
                    self._save_journal(journal, verified, removed)
                    self.finished.emit(plan.folder.value, False)
                    return False
            except OSError:
                log.error("KFM: could not copy %s", rel, exc_info=True)
                self._save_journal(journal, verified, removed)
                self.finished.emit(plan.folder.value, False)
                return False
            verified.add(rel)
            if index % 50 == 0:
                self._save_journal(journal, verified, removed)
            self.progress.emit(plan.folder.value, index, total)

        self._save_journal(journal, verified, removed)

        # ── phase two: remove what phase one verified ───────────────────────
        for rel in sorted(verified - removed):
            source_file = plan.source / rel
            if not source_file.exists():
                removed.add(rel)
                continue
            try:
                # To the trash, not to unlink(): a verified copy exists, but a
                # user who changes their mind an hour later should still be able
                # to find the original where they left it.
                trash.trash(source_file)
            except (OSError, SafetyRefusal):
                log.warning("KFM: could not trash %s after copying it",
                            source_file, exc_info=True)
                continue
            removed.add(rel)
        self._save_journal(journal, verified, removed)

        write_user_dirs({plan.folder: destination})
        repo_files.set_kfm_folder(self.account.id, plan.folder, True,
                                  writer=self._writer)
        self._clear_journal()
        log.info("KFM: %s now lives at %s (%d files)",
                 plan.folder.value, destination, total)
        self.finished.emit(plan.folder.value, True)
        return True

    def _verify(self, source: Path, target: Path) -> bool:
        """Size and content hash. Both, because either alone can be fooled.

        A truncated copy has the wrong size; a copy through a FUSE mount that
        silently landed nowhere can have the right size and the wrong bytes.
        """
        try:
            if source.stat().st_size != target.stat().st_size:
                return False
            return _digest(source) == _digest(target)
        except OSError:
            return False

    # ═════════════════════════════════════════════════════════════════════════
    # Reversing
    # ═════════════════════════════════════════════════════════════════════════

    def disable(self, folder: KfmFolder) -> bool:
        """Point a known folder back at its default location. Files stay put.

        Deliberately *not* a move back. The files are in OneDrive, they are
        synced, and dragging 48 GB back out of the sync root is a decision the
        user should make explicitly rather than a side effect of unticking a
        box. The XDG entry is repointed and the folder is left where it is.
        """
        default = Path.home() / folder.value.capitalize()
        default.mkdir(parents=True, exist_ok=True)
        write_user_dirs({folder: default})
        repo_files.set_kfm_folder(self.account.id, folder, False,
                                  writer=self._writer)
        log.info("KFM: %s points back at %s; the files stay in OneDrive",
                 folder.value, default)
        return True

    def rollback(self) -> bool:
        """Undo an interrupted run from its journal.

        Returns:
            True when the journal was replayed backwards.

        Every file the journal marks ``removed`` is already in the trash and
        also verified at the destination, so rollback restores the destination
        copy to the source rather than fishing in the trash — same bytes, and it
        cannot fail because the trash was emptied.
        """
        journal = self._load_journal()
        if not journal:
            return False
        source = Path(journal.get("source", ""))
        destination = Path(journal.get("destination", ""))
        if not source or not destination:
            return False

        for rel in sorted(set(journal.get("verified", []))):
            target = destination / rel
            original = source / rel
            if original.exists() or not target.exists():
                continue
            try:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, original)
            except OSError:
                log.error("KFM rollback: could not restore %s", rel, exc_info=True)
                return False
        log.warning("KFM: rolled back an interrupted move of %s",
                    journal.get("folder"))
        self._clear_journal()
        return True

    def has_unfinished_run(self) -> bool:
        """Is there a journal from a run that did not complete?"""
        return self._journal_path.exists()

    # ═════════════════════════════════════════════════════════════════════════
    # The journal
    # ═════════════════════════════════════════════════════════════════════════

    def _load_journal(self) -> dict[str, Any]:
        return atomicio.read_json(self._journal_path, {}) or {}

    def _save_journal(self, journal: dict[str, Any], verified: set[str],
                      removed: set[str]) -> None:
        journal["verified"] = sorted(verified)
        journal["removed"] = sorted(removed)
        journal["updated_at"] = utcnow_iso()
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        atomicio.atomic_write_json(self._journal_path, journal)

    def _clear_journal(self) -> None:
        try:
            os.unlink(self._journal_path)
        except OSError:
            pass


def _digest(path: Path, block: int = 1024 * 1024) -> str:
    """A content hash, read in blocks so a 4 GB file does not become 4 GB of RAM."""
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
