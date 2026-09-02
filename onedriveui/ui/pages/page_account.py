""""Account" — who is signed in, how much space, and the two big buttons.

The page is small; two of its four controls carry the weight.

**"Choose folders" is a delete in a filter's clothing.** Unchecking a folder
removes the local copies, and the dialog says so with a number attached. Nothing
happens until a resync has succeeded, and even then the removal goes to the
freedesktop trash — but the button still opens a confirmation rather than a
tree with a Save that quietly does all that.

**"Unlink this PC" keeps every file.** Microsoft's own dialog promises it and so
does ours, and unlike a reassurance it is enforced:
:meth:`~onedriveui.sync.accounts.AccountManager.unlink` raises rather than
accepting ``keep_files=False``, and a test hashes the tree before and after.

There is deliberately no storage card here. The one that used to sit second was
titled "Get more storage" and contained a bar — which is two different things
badly merged: the usage figure belongs where the user is already looking at sync
state (the Activity Center shows "252.5 GB of 1.1 TB used" with a bar), and "Get
more storage" is a link, not a heading. The result was an empty box with a title
that promised an action it did not offer.

The identity shown at the top is worth a note. rclone's ``Features.UserInfo`` is
**false** for OneDrive, so the display name is captured during OAuth or, failing
that, read off an item's ``created-by-display-name`` metadata. When neither
worked the header shows the remote name rather than a plausible-looking guess.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from onedriveui.strings import DIALOG, SETTINGS
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.containers import SectionHeading, SettingsCard
from onedriveui.ui.widgets.controls import ButtonVariant, FluentButton

log = logging.getLogger(__name__)

__all__ = ["AccountPage"]


class AccountPage(QWidget):
    """Identity, storage, folder choice, unlink.

    Args:
        account: The account.
        config: The loaded config.
        supervisor: The Supervisor.
        services: The engine's services.
        parent: Qt parent.

    Signals:
        unlinked: The account id, after a confirmed unlink.
    """

    unlinked = Signal(str)

    def __init__(self, account: Any, *, config: Any = None,
                 supervisor: Any = None, services: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self._config = config
        self._supervisor = supervisor
        self._services = dict(services or {})
        self._cards: dict[str, QWidget] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(SPACING["l"], SPACING["l"],
                                  SPACING["l"], SPACING["l"])
        column.setSpacing(SPACING["m"])

        column.addWidget(SectionHeading(SETTINGS.NAV_ACCOUNT, self))
        column.addWidget(self._identity())
        column.addWidget(self._choose_folders())
        column.addWidget(self._unlink())
        column.addStretch(1)

    # ═════════════════════════════════════════════════════════════════════════
    # Cards
    # ═════════════════════════════════════════════════════════════════════════

    def _identity(self) -> QWidget:
        """The display name and email, or the remote name when we have neither.

        rclone cannot tell us who the user is — ``Features.UserInfo`` is false
        for OneDrive and ``config userinfo`` errors — so this is whatever OAuth
        captured or what an item's ``created-by-display-name`` said. Showing the
        remote name when both failed is better than a placeholder that looks
        like a real name.
        """
        name = self.account.display_name or self.account.remote
        card = SettingsCard(name, self,
                            description=self.account.email or self.account.fs,
                            action_icon=False)
        self._cards["identity"] = card
        return card

    def _choose_folders(self) -> QWidget:
        button = FluentButton(SETTINGS.CHOOSE_FOLDERS, self,
                              variant=ButtonVariant.STANDARD)
        button.clicked.connect(self._on_choose_folders)
        card = SettingsCard(SETTINGS.CHOOSE_FOLDERS, self,
                            description=DIALOG.CHOOSE_FOLDERS_WARN,
                            content=button, action_icon=False)
        self._cards["choose_folders"] = card
        return card

    def _unlink(self) -> QWidget:
        button = FluentButton(SETTINGS.UNLINK, self,
                              variant=ButtonVariant.STANDARD)
        button.clicked.connect(self._on_unlink)
        card = SettingsCard(SETTINGS.UNLINK, self,
                            description=DIALOG.UNLINK_BODY,
                            content=button, action_icon=False)
        self._cards["unlink"] = card
        return card

    # ═════════════════════════════════════════════════════════════════════════
    # Actions
    # ═════════════════════════════════════════════════════════════════════════

    def _on_choose_folders(self) -> None:
        """Show the tree with a preview of what unchecking costs.

        The preview is what makes the decision possible. "Some folders will be
        removed from this computer" is a warning; "12 431 files (48 GB) go to
        the trash" is information.
        """
        from onedriveui.ui.dialogs.misc_dialogs import ChooseFoldersDialog

        selective = self._services.get("selective")
        preview = None
        if selective is not None:
            try:
                preview = selective.preview(selective.excluded())
            except Exception:  # noqa: BLE001 - a missing preview is not a blocker
                log.debug("could not preview the folder selection", exc_info=True)

        dialog = ChooseFoldersDialog(preview=preview, parent=self)
        self._fill_folder_tree(dialog, selective)
        dialog.exec()
        if not dialog.approved() or selective is None:
            return
        try:
            # What the user actually ticked — not `selective.excluded()`, which
            # is the selection that was *already* stored. Applying that made OK
            # a no-op that looked like it had worked.
            selective.apply(dialog.tree.excluded())
        except Exception:  # noqa: BLE001 - a refused change is reported, not silent
            log.error("the folder selection could not be applied", exc_info=True)

    def _fill_folder_tree(self, dialog: Any, selective: Any) -> None:
        """Populate the picker with the drive's top-level folders.

        The tree was never filled, so the dialog opened empty and there was
        nothing to choose from. The listing is a blocking rc call, so it goes to
        the IOPool (ARCHITECTURE §7.6) and lands back on the GUI thread — which
        the modal `exec()` below still services, because a modal dialog runs a
        nested event loop rather than blocking it.

        Only the top level. `children()` is explicitly never recursive, and
        walking a whole drive to open a dialog is the kind of thing that makes a
        settings window take a minute.
        """
        browser = self._services.get("browse")
        if browser is None:
            return
        try:
            stored = selective.selection() if selective is not None else {}
        except Exception:  # noqa: BLE001 - an unknown selection means all-checked
            stored = {}

        def fill(nodes: Any) -> None:
            from PySide6.QtCore import Qt

            tree = dialog.tree
            for node in nodes or ():
                if not getattr(node, "is_dir", False):
                    continue
                rel = node.rel_path
                checked = (Qt.CheckState.Checked if stored.get(rel, True)
                           else Qt.CheckState.Unchecked)
                try:
                    tree.add_folder(node.name, rel_path=rel, checked=checked)
                except RuntimeError:
                    return          # the dialog was closed while we listed

        from onedriveui.platform.iopool import instance as io_pool

        io_pool().submit(browser.children, "", kind="rc", on_done=fill,
                         on_error=lambda exc: log.warning(
                             "could not list the drive's folders: %s", exc))

    def _on_unlink(self) -> None:
        """Confirm, then unlink. **Every file stays.**

        Not a promise made by this dialog: `unlink()` raises rather than
        accepting `keep_files=False`, so there is no path from this button to a
        deleted file.
        """
        from onedriveui.ui.dialogs.sync_dialogs import UnlinkDialog

        dialog = UnlinkDialog(self.account.display_name or self.account.id, self)
        dialog.exec()
        if not dialog.approved():
            return
        accounts = self._services.get("accounts")
        if accounts is not None:
            accounts.unlink(self.account)
        self.unlinked.emit(self.account.id)

    def card(self, key: str) -> QWidget | None:
        return self._cards.get(key)
