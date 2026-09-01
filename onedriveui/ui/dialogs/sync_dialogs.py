"""The destructive gates, where the primary button is the safe answer.

Every dialog here stands between the user and something that cannot be undone
by clicking again, and all of them are built the same way:

**The primary button is the recoverable choice.** "Delete these 4 231 items?"
has **"Restore files"** as its accent button, not "Delete them". Windows makes
the same inversion, and the reason is the same on both: the primary button is
what Return presses, what muscle memory presses, and what somebody who has
stopped reading presses. So it has to be the answer they can come back from.

**Silence is not consent.** These dialogs cannot be dismissed with Escape, and
the mass-delete one carries Microsoft's seven-day note verbatim — if nobody
answers, the files are **not** deleted.

**A resync says what a resync does.** The single most misunderstood operation in
rclone: ``--resync`` only ever *copies*. Run casually it resurrects every file
the user has deleted since sync broke, and the dialog says so in those words
rather than calling it "reset sync" and leaving them to find out.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QWidget

from onedriveui.models import DialogKey
from onedriveui.strings import DIALOG, t
from onedriveui.ui.dialogs.base import BaseDialog, DialogResult, DialogSpec

__all__ = ["MassDeleteDialog", "FirstDeleteDialog", "ResyncDialog",
           "UnlinkDialog", "ResetDialog", "StopBackupDialog"]


class MassDeleteDialog(BaseDialog):
    """"Delete these N items?" — with **"Restore files"** as the primary button.

    Args:
        count: How many items were deleted in the cloud.
        parent: Qt parent.

    The inversion is the whole point. This dialog appears when something deleted
    thousands of files and we cannot tell whether it was the user tidying up or
    a drive that went away, so the button that Return presses has to be the one
    that keeps the files. Making "Delete them" primary would turn a moment of
    inattention into an unrecoverable one.

    It also cannot be dismissed. Escape resolving this to "the deletion goes
    ahead" is the exact failure the seven-day policy exists to prevent.
    """

    def __init__(self, count: int, parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=t(DIALOG.MASS_DELETE_TITLE, n=count),
            body=t(DIALOG.MASS_DELETE_BODY, n=count),
            # Primary = the recoverable answer.
            primary=DIALOG.MASS_DELETE_NO,
            secondary=DIALOG.MASS_DELETE_YES,
            footnote=DIALOG.MASS_DELETE_TIMEOUT,
            dismissible=False,
        ), parent)
        self.count = count

    def wants_delete(self) -> bool:
        """True only if the user explicitly chose to delete.

        Note which way round this is: the *secondary* button means delete.
        """
        return self.result_choice is DialogResult.SECONDARY

    def wants_restore(self) -> bool:
        return self.result_choice is not DialogResult.SECONDARY


class FirstDeleteDialog(BaseDialog):
    """"Deleted files are removed everywhere" — shown once, then remembered.

    Args:
        name: The file that was deleted.
        parent: Qt parent.

    Educational rather than a gate: the delete has already happened locally and
    is about to propagate. Windows shows it the first time and never again, and
    the "don't show this reminder again" box is what makes that true.
    """

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.FIRST_DELETE_TITLE,
            body=t(DIALOG.FIRST_DELETE_BODY, name=name),
            primary=DIALOG.OK,
            remember=DialogKey.FIRST_DELETE,
        ), parent)
        self.name = name


class ResyncDialog(BaseDialog):
    """"Reset sync?" — stating what a resync actually does.

    Args:
        parent: Qt parent.

    ``--resync`` is the single most misunderstood operation in rclone. It only
    ever **copies**: both sides end up with a matching superset and nothing is
    deleted. Run casually it therefore resurrects every file the user has
    deleted since sync broke, and leaves both names behind after every rename.

    The body says that in those words. A dialog that called this "reset sync"
    and left the consequence to be discovered would be technically accurate and
    practically a trap — and this is also the decision row that invariant I15
    requires before a resync may run at all.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.RESYNC_TITLE,
            body=DIALOG.RESYNC_BODY,
            # The safe answer first: not resyncing leaves a broken sync, which
            # is recoverable; resyncing wrongly resurrects deleted files, which
            # is a mess to undo by hand.
            primary=DIALOG.CANCEL,
            secondary=DIALOG.CONTINUE,
            dismissible=False,
        ), parent)

    def approved(self) -> bool:
        """True only on an explicit "Continue". Feeds invariant I15."""
        return self.result_choice is DialogResult.SECONDARY


class UnlinkDialog(BaseDialog):
    """"Unlink account on this PC?" — with the reassurance that files stay.

    Args:
        account_name: Whose account.
        parent: Qt parent.

    The body's promise is not decoration: :meth:`~onedriveui.sync.accounts.
    AccountManager.unlink` genuinely does not touch a single file under
    ``sync_root``, and a test hashes the tree before and after to prove it. The
    dialog can say so because it is true.
    """

    def __init__(self, account_name: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.UNLINK_TITLE,
            body=DIALOG.UNLINK_BODY,
            primary=DIALOG.CANCEL,
            secondary=DIALOG.CONTINUE,
        ), parent)
        self.account_name = account_name

    def approved(self) -> bool:
        return self.result_choice is DialogResult.SECONDARY


class ResetDialog(BaseDialog):
    """"Reset OneDriveUI?" — caches and index only, never files.

    Args:
        parent: Qt parent.

    ``reset_client()`` raises rather than accepting ``keep_files=False``, so
    there is no combination of answers here that deletes anything. The body says
    every file is kept because that is enforced one layer down, not because the
    wording is reassuring.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.RESET_TITLE,
            body=DIALOG.RESET_BODY,
            primary=DIALOG.CANCEL,
            secondary=DIALOG.CONTINUE,
        ), parent)

    def approved(self) -> bool:
        return self.result_choice is DialogResult.SECONDARY


class StopBackupDialog(BaseDialog):
    """"Stop backing up this folder?" — with Desktop's extra option.

    Args:
        folder: Which known folder.
        parent: Qt parent.

    Desktop gets a third choice Windows also offers: "This computer only",
    which keeps the folder backed up on other devices while un-backing it here.
    It exists because a shared Desktop across two machines is the case where
    stopping the backup everywhere is almost never what was meant.
    """

    def __init__(self, folder: str, parent: QWidget | None = None,
                 *, offer_this_computer_only: bool = False) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.STOP_BACKUP_TITLE,
            body=DIALOG.CHOOSE_FOLDERS_WARN,
            primary=DIALOG.CANCEL,
            secondary=(DIALOG.STOP_BACKUP_DESKTOP if offer_this_computer_only
                       else DIALOG.CONTINUE),
        ), parent)
        self.folder = folder
        self.this_computer_only = offer_this_computer_only

    def approved(self) -> bool:
        return self.result_choice is DialogResult.SECONDARY
