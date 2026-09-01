"""Quit, the folder picker, and the vault — the dialogs that are mostly wording.

Three small ones, each carrying a sentence the user needs before they answer:

**Quit** says syncing stops. On Windows the client is always running and quitting
is unusual; on Linux a user who closes a window expects the application to be
gone, so the consequence has to be stated rather than assumed.

**Choose folders** warns that unchecking removes the local copies — and it is
the *preview* that makes this honest, because "12 431 files (48 GB) will be moved
to the trash" is a different decision from "some folders will be removed".

**The vault** explains, every time it appears, that this is local encryption and
not Microsoft's Personal Vault. Somebody deciding where to put a passport scan is
entitled to know which one they are using.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from onedriveui.strings import DIALOG, SETTINGS, t
from onedriveui.ui.dialogs.base import BaseDialog, DialogResult, DialogSpec
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.lists import FolderTree
from onedriveui.units import human_bytes

__all__ = ["QuitDialog", "ChooseFoldersDialog", "VaultDialog",
           "DiskUsageNoteDialog"]


class QuitDialog(BaseDialog):
    """"Close OneDrive?" — with what stops when it does.

    Args:
        parent: Qt parent.

    The primary is Cancel. Quitting is not destructive, but it *is* the thing
    that silently stops a backup running, and a user who hit the wrong window
    control should not lose their sync to a reflex.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.QUIT_TITLE,
            body=DIALOG.QUIT_BODY,
            primary=DIALOG.CANCEL,
            secondary=DIALOG.CONTINUE,
        ), parent)

    def approved(self) -> bool:
        return self.result_choice is DialogResult.SECONDARY


class ChooseFoldersDialog(BaseDialog):
    """The selective-sync tree, with the cost of unchecking stated up front.

    Args:
        preview: A :class:`~onedriveui.sync.selective.PruneResult` from
            ``SelectiveSync.preview()``, or ``None``.
        parent: Qt parent.

    "Choose folders" looks like a filter and behaves like a delete. The warning
    line says the unchecked folders are removed from this computer; the preview
    turns that into a number, which is the difference between a decision the
    user can make and one they can only guess at.

    Nothing is removed until a resync has succeeded, and even then it goes to the
    trash — but that is a promise the dialog can make because
    :meth:`~onedriveui.sync.selective.SelectiveSync.apply` enforces it, not
    because the wording is soothing.
    """

    def __init__(self, *, preview: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=SETTINGS.CHOOSE_FOLDERS,
            body=DIALOG.CHOOSE_FOLDERS_WARN,
            primary=DIALOG.SAVE,
            secondary=DIALOG.CANCEL,
        ), parent)
        self.preview = preview

        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["s"])

        self._tree = FolderTree(holder)
        column.addWidget(self._tree)

        if preview is not None and getattr(preview, "trashed", None):
            # The number, not the warning: "some folders will be removed" and
            # "12 431 files (48 GB) go to the trash" are different decisions.
            cost = QLabel(
                t(DIALOG.FREE_UP_BODY,
                  size=human_bytes(getattr(preview, "bytes_freed", 0))), holder)
            cost.setWordWrap(True)
            column.addWidget(cost)

        self.set_content(holder)

    @property
    def tree(self) -> FolderTree:
        return self._tree

    def approved(self) -> bool:
        return self.result_choice is DialogResult.PRIMARY


class VaultDialog(BaseDialog):
    """Unlock the vault — saying, every time, which vault this is.

    Args:
        locked: Whether it is currently locked.
        parent: Qt parent.

    :data:`~onedriveui.strings.DIALOG.VAULT_CLOUD_WHY` appears on every
    appearance, not once in a tooltip. Somebody deciding where to keep a
    passport scan is entitled to know that this encrypts files on *this device*
    and is not Microsoft's Personal Vault — and a person who learns that later
    has already made the decision on a false premise.
    """

    def __init__(self, *, locked: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=SETTINGS.VAULT_TIMEOUT,
            body=DIALOG.VAULT_CLOUD_WHY,
            primary=DIALOG.CONTINUE,
            secondary=DIALOG.CANCEL,
        ), parent)
        self.locked = locked

    def approved(self) -> bool:
        return self.result_choice is DialogResult.PRIMARY


class DiskUsageNoteDialog(BaseDialog):
    """Why `du` and the file manager disagree with us about the size.

    Args:
        parent: Qt parent.

    Every disk-usage tool reports the *apparent* size of an online-only file,
    because rclone preallocates the cache file to the object's full remote size
    on first open. A 50 MB file holding 192 KiB shows as 50 MB in ``ls -l``, in
    ``du``, and in the file manager's properties pane. Our own figures come from
    ``SEEK_DATA``/``SEEK_HOLE`` and are the real ones — and a user who has just
    seen two different numbers deserves to be told which is which rather than
    left to conclude one of them is a bug.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        from onedriveui.models import DialogKey

        super().__init__(DialogSpec(
            title=DIALOG.WHERE_ARE_MY_FILES,
            body=DIALOG.DU_ON_MOUNT_NOTE,
            primary=DIALOG.OK,
            remember=DialogKey.DU_ON_MOUNT_NOTE,
        ), parent)
