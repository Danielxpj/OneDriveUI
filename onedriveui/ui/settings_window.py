"""The Settings window: a four-item shell at 1024x720, applying immediately.

Three decisions worth stating.

**Microsoft's four sections, in Microsoft's order.** "Sync and back up",
"Account", "Notifications", "About". A user who has used the Windows client
should not have to learn where anything is, and reorganising them for our own
convenience would cost exactly that.

**No OK button, anywhere.** Every control applies on change: the write is
atomic, ``config_changed`` carries the dotted key, and the engine picks it up
within a tick. A settings window with an Apply button is one that can be closed
with unsaved changes in it, and the resulting "I turned that on and it did not
work" is unanswerable.

**Deep links.** :meth:`SettingsWindow.navigate` takes ``"sync.bandwidth"`` and
opens the right page with the right card in view, so a banner's "Free up space"
and a toast's "Get more storage" can land somewhere specific rather than on
page one.

One thing this window does *not* do is restart the mount behind the user's back.
Several settings — anything in the ``vfs`` block — cannot take effect on a
running mount: ``options/set`` returns ``{}`` and changes nothing. Those say so
before they apply, and the restart is a separate, visible action.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QScrollArea,
    QStackedWidget,
    QWidget,
)

from onedriveui.constants import SETTINGS_H, SETTINGS_W
from onedriveui.strings import SETTINGS
from onedriveui.ui.pages import PAGES
from onedriveui.ui.widgets.chrome import NavigationView

log = logging.getLogger(__name__)

__all__ = ["SettingsWindow", "NAV_ITEMS", "RESTART_REQUIRED_KEYS"]

#: ``(key, label, glyph)`` per section, in Microsoft's order.
NAV_ITEMS: Final[tuple[tuple[str, str, str], ...]] = (
    ("sync", SETTINGS.NAV_SYNC, "nav_sync"),
    ("account", SETTINGS.NAV_ACCOUNT, "nav_account"),
    ("notifications", SETTINGS.NAV_NOTIFICATIONS, "nav_notifications"),
    ("about", SETTINGS.NAV_ABOUT, "nav_about"),
)

#: Config keys that a running mount cannot pick up.
#:
#: `options/set` on the `vfs` block is accepted, returns `{}`, and changes
#: nothing about the live VFS — so a client that applied one of these and said
#: nothing would leave the user watching a setting that is saved, displayed, and
#: not in effect. These say so before they apply.
RESTART_REQUIRED_KEYS: Final[frozenset[str]] = frozenset({
    "mount.vfs_cache_max_size_gb", "mount.vfs_cache_max_age_hours",
    "mount.vfs_cache_min_free_space_gb", "mount.dir_cache_time_s",
    "mount.poll_interval_s", "mount.read_chunk_size_mb",
    "mount.write_back_s", "mount.transfers", "mount.checkers",
    "mount.allow_other", "mount.links", "mount.umask",
})


class SettingsWindow(QWidget):
    """The four-section shell.

    Args:
        account: The account being configured.
        config: The loaded :class:`~onedriveui.config.AppConfig`.
        supervisor: The Supervisor. Every action funnels through it.
        services: The engine's services.
        parent: Qt parent.

    Signals:
        setting_changed: The dotted key that was just written.
        restart_required: A key whose change needs a mount restart.
    """

    setting_changed = Signal(str)
    restart_required = Signal(str)

    def __init__(self, account: Any, *, config: Any = None,
                 supervisor: Any = None, services: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self._config = config
        self._supervisor = supervisor
        self._services = dict(services or {})
        self._pages: dict[str, QWidget] = {}

        self.setWindowTitle(SETTINGS.NAV_SYNC)
        self.resize(SETTINGS_W, SETTINGS_H)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self._nav = NavigationView(self)
        self._stack = QStackedWidget(self)
        row.addWidget(self._nav)
        row.addWidget(self._stack, 1)

        for (key, label, glyph), (page_key, page_class) in zip(
                NAV_ITEMS, PAGES, strict=True):
            assert key == page_key, "NAV_ITEMS and PAGES disagree"
            self._nav.add_item(label, glyph, key=key)
            page = page_class(account, config=config, supervisor=supervisor,
                              services=services)
            self._pages[key] = page
            self._stack.addWidget(self._scrolled(page))
            changed = getattr(page, "changed", None)
            if changed is not None:
                changed.connect(self._on_changed)

        self._nav.current_changed.connect(self._stack.setCurrentIndex)
        self._nav.set_current_index(0)

    def _scrolled(self, page: QWidget) -> QScrollArea:
        """Each page scrolls vertically and never horizontally.

        A horizontal scrollbar in a settings page means a control is wider than
        the window, which is a layout bug the user would have to work around
        rather than a feature.
        """
        area = QScrollArea(self)
        area.setWidget(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area

    # ═════════════════════════════════════════════════════════════════════════
    # Navigation
    # ═════════════════════════════════════════════════════════════════════════

    def navigate(self, target: str) -> bool:
        """Open a page, or a specific card on one.

        Args:
            target: ``"sync"`` or ``"sync.bandwidth"``.

        Returns:
            True when the target was found.

        Deep links exist so a banner's "Free up space" and a toast's "Get more
        storage" land on the control they name rather than on page one, where
        the user then has to find it.
        """
        page_key, _, card_key = target.partition(".")
        index = self._nav.index_of(page_key)
        if index < 0:
            return False
        self._nav.set_current_index(index)
        self._stack.setCurrentIndex(index)
        if not card_key:
            return True
        page = self._pages.get(page_key)
        card = getattr(page, "card", None)
        widget = card(card_key) if callable(card) else None
        if widget is not None:
            widget.setFocus(Qt.FocusReason.OtherFocusReason)
        return widget is not None

    def page(self, key: str) -> QWidget | None:
        return self._pages.get(key)

    @property
    def nav(self) -> NavigationView:
        return self._nav

    # ═════════════════════════════════════════════════════════════════════════
    # Changes
    # ═════════════════════════════════════════════════════════════════════════

    def _on_changed(self, key: str) -> None:
        """Announce a change, and say when it needs a restart to take effect.

        The restart is not performed here. It has to refuse while an upload is
        in flight (invariant I3), which is the Supervisor's job — and doing it
        silently from a settings toggle would interrupt a transfer the user
        cannot see.
        """
        self.setting_changed.emit(key)
        if key in RESTART_REQUIRED_KEYS:
            log.info("%s takes effect after the mount restarts", key)
            self.restart_required.emit(key)

    def needs_restart(self, key: str) -> bool:
        """Does changing this key need a mount restart?

        ``options/set`` on the ``vfs`` block is accepted, returns ``{}`` and
        changes nothing on a live mount — so the honest answer for these is
        "saved, and in effect after a restart", not "applied".
        """
        return key in RESTART_REQUIRED_KEYS
