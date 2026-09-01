"""The per-file dialogs, and the two controls that are disabled on purpose.

Most of these are ordinary confirmations. Two are not, and they are the reason
this module has a docstring:

**"Remove link" is disabled, always.** rclone's ``unlink=true`` is a verified
no-op that *creates* a link. A button that called it and reported success would
tell the user their document is private while it is still publicly readable —
the worst possible wrong answer a sharing feature can give. So the control is
present, disabled, with :data:`~onedriveui.strings.DIALOG.REMOVE_LINK_WHY`
beside it and a working route to the web interface, which is where revoking
actually happens.

**Version history opens a browser.** OneDrive's server-side history is real and
complete; rclone can *delete* versions and can neither list nor restore them.
The dialog shows our own ``--backup-dir`` snapshots, says plainly that they are
ours and not the full story, and links to the web.

Both follow the same principle as everything else in the UI: a control that
cannot work is shown **disabled with its reason**, never hidden.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from onedriveui.models import DialogKey, LinkScope, LinkType, ShareLink
from onedriveui.strings import ACTION_LABEL, DIALOG, t
from onedriveui.ui.dialogs.base import (
    BaseDialog,
    DialogResult,
    DialogSpec,
    disable_with_reason,
)
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.controls import ButtonVariant, FluentButton
from onedriveui.units import human_bytes

__all__ = ["FreeUpSpaceDialog", "DownloadAllDialog", "ShareDialog",
           "VersionHistoryDialog", "ConflictDialog", "RecycleBinDialog"]


class FreeUpSpaceDialog(BaseDialog):
    """"Free up disk space?" — with the size, and the reassurance.

    Args:
        size_bytes: How much comes back.
        parent: Qt parent.

    The body promises the files stay in OneDrive and download when opened, and
    that promise is enforced rather than asserted: ``vfs.evict()`` refuses a
    dirty or queued item outright, so nothing that exists only on this disk can
    be freed by pressing this.
    """

    def __init__(self, size_bytes: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.FREE_UP_TITLE,
            body=t(DIALOG.FREE_UP_BODY, size=human_bytes(size_bytes)),
            primary=DIALOG.CONTINUE,
            secondary=DIALOG.CANCEL,
            remember=DialogKey.FOD_FREE_UP_SPACE,
        ), parent)
        self.size_bytes = size_bytes

    def approved(self) -> bool:
        return self.result_choice is DialogResult.PRIMARY


class DownloadAllDialog(BaseDialog):
    """"Download all files?" — with the size it will cost.

    Args:
        size_bytes: How much will land on this disk.
        parent: Qt parent.

    The size is the whole point of the dialog. "Download all files" on a 900 GB
    drive with 200 GB free is a request that cannot be granted, and the number is
    the only thing that makes that obvious before it starts.
    """

    def __init__(self, size_bytes: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.DOWNLOAD_ALL_TITLE,
            body=t(DIALOG.DOWNLOAD_ALL_BODY, size=human_bytes(size_bytes)),
            primary=DIALOG.CONTINUE,
            secondary=DIALOG.CANCEL,
            remember=DialogKey.FOD_DOWNLOAD_ALL,
        ), parent)
        self.size_bytes = size_bytes

    def approved(self) -> bool:
        return self.result_choice is DialogResult.PRIMARY


class ShareDialog(BaseDialog):
    """Create a link — and show, disabled, the one thing we cannot do.

    Args:
        rel_path: The item being shared.
        links: Links this client has already issued for it.
        can_revoke: Whether revoking works. **Always False**; the parameter
            exists so the disabled state is visible in the constructor rather
            than buried, and so a test can prove the enabled branch is never
            taken.
        parent: Qt parent.
    """

    def __init__(self, rel_path: str, *, links: list[ShareLink] | None = None,
                 can_revoke: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=rel_path.rsplit("/", 1)[-1],
            primary=DIALOG.OK,
            secondary=DIALOG.CANCEL,
        ), parent)
        self.rel_path = rel_path
        self.links = list(links or [])
        self.scope = LinkScope.ANONYMOUS
        self.link_type = LinkType.VIEW

        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["s"])

        self._list = QListWidget(holder)
        for link in self.links:
            QListWidgetItem(link.url, self._list)
        column.addWidget(self._list)

        # Present, disabled, with the reason beside it. Hiding it would make the
        # user hunt the settings for a "stop sharing" that does not exist here;
        # this tells them it is on the website, which is true and actionable.
        self._remove = FluentButton(DIALOG.REMOVE_LINK, holder,
                                    variant=ButtonVariant.STANDARD)
        if not can_revoke:
            disable_with_reason(self._remove, DIALOG.REMOVE_LINK_WHY)
        column.addWidget(self._remove)

        why = QLabel(DIALOG.REMOVE_LINK_WHY, holder)
        why.setWordWrap(True)
        column.addWidget(why)
        self.set_content(holder)

    @property
    def remove_button(self) -> FluentButton:
        """The disabled control. Exposed so a test can assert it stays that way."""
        return self._remove


class VersionHistoryDialog(BaseDialog):
    """Our own snapshots, labelled as ours, beside a link to the real thing.

    Args:
        rel_path: The file.
        versions: Our ``--backup-dir`` snapshots.
        parent: Qt parent.

    OneDrive's server-side version history is complete and covers every device;
    rclone can delete versions and cannot list or restore them. Presenting our
    partial list as "the version history" would let a user conclude a change
    made on their phone was never recorded. So the list says what it is and the
    web link is right beside it.
    """

    def __init__(self, rel_path: str, *, versions: list[Any] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=rel_path.rsplit("/", 1)[-1],
            body=DIALOG.VERSION_HISTORY_WHY,
            primary=ACTION_LABEL_OPEN_WEB,
            secondary=DIALOG.CLOSE,
        ), parent)
        self.rel_path = rel_path
        self.versions = list(versions or [])

        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget(holder)
        for entry in self.versions:
            QListWidgetItem(f"{entry.captured_at} — "
                            f"{human_bytes(entry.size)}", self._list)
        column.addWidget(self._list)
        self.set_content(holder)

    def wants_web(self) -> bool:
        return self.result_choice is DialogResult.PRIMARY


class ConflictDialog(BaseDialog):
    """"Two people edited this file" — three answers, none of them destructive.

    Args:
        rel_path: The contested file.
        loser_path: Where the other copy already is.
        parent: Qt parent.

    Both copies exist before this dialog opens; the rename has already happened.
    So none of the three answers can lose work, and "newest wins" — the one that
    could — is not offered at all.
    """

    def __init__(self, rel_path: str, loser_path: str = "",
                 parent: QWidget | None = None) -> None:
        from onedriveui.models import RecoveryAction

        super().__init__(DialogSpec(
            title=rel_path.rsplit("/", 1)[-1],
            primary=ACTION_LABEL[RecoveryAction.KEEP_BOTH],
            secondary=ACTION_LABEL[RecoveryAction.KEEP_LOCAL],
            close=ACTION_LABEL[RecoveryAction.KEEP_CLOUD],
        ), parent)
        self.rel_path = rel_path
        self.loser_path = loser_path


class RecycleBinDialog(BaseDialog):
    """Our own trash, with Microsoft's explained rather than faked.

    Args:
        entries: Items in our ``.onedriveui-trash/``.
        parent: Qt parent.

    A file deleted through the file manager went to Microsoft's cloud recycle
    bin, which rclone cannot list — so it is genuinely not in this list, and
    saying so is better than showing an empty window that implies nothing was
    ever deleted.
    """

    def __init__(self, entries: list[Any] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(DialogSpec(
            title=DIALOG.WHERE_ARE_MY_FILES,
            body=DIALOG.RECYCLE_BIN_WHY,
            primary=ACTION_LABEL_OPEN_WEB,
            secondary=DIALOG.CLOSE,
        ), parent)
        self.entries = list(entries or [])

        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget(holder)
        for entry in self.entries:
            QListWidgetItem(f"{entry.rel_path} — {entry.deleted_at}", self._list)
        column.addWidget(self._list)
        self.set_content(holder)

    def wants_web(self) -> bool:
        return self.result_choice is DialogResult.PRIMARY


def _open_web_label() -> str:
    from onedriveui.models import RecoveryAction

    return ACTION_LABEL[RecoveryAction.OPEN_WEB]


#: "View online". Resolved once at import so the dialogs above can name it in
#: their specs without a call in a default argument.
ACTION_LABEL_OPEN_WEB = _open_web_label()
