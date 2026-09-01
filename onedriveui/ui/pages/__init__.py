"""The four Settings sections, with Microsoft's own names.

"Sync and back up", "Account", "Notifications", "About" — the same four, in the
same order, because a user who has used the Windows client should not have to
learn where anything is.

Two rules hold across all of them:

**Immediate apply.** There is no OK button. Toggling a switch writes
``config.json`` atomically and emits ``config_changed`` with the dotted key, and
the engine picks it up on its next tick. A settings window with an Apply button
is a settings window that can be closed with unsaved changes.

**Nothing is silently missing.** A control that cannot work on Linux renders
**disabled with an inline reason**, never hidden — see
:func:`~onedriveui.ui.dialogs.base.disable_with_reason`.
"""

from __future__ import annotations

from onedriveui.ui.pages.page_about import AboutPage
from onedriveui.ui.pages.page_account import AccountPage
from onedriveui.ui.pages.page_notifications import NotificationsPage
from onedriveui.ui.pages.page_sync import SyncPage

__all__ = ["SyncPage", "AccountPage", "NotificationsPage", "AboutPage", "PAGES"]

#: The four sections, in Microsoft's order. The settings window builds its
#: navigation from this, so adding a page is one entry rather than three edits.
PAGES = (
    ("sync", SyncPage),
    ("account", AccountPage),
    ("notifications", NotificationsPage),
    ("about", AboutPage),
)
