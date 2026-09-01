""""Notifications" — Windows' five toggles plus three of ours.

The whole page is switches, and the interesting thing about it is what is *not*
here: there is no toggle for "sign in required", "your OneDrive is full" or "your
files aren't syncing". Those are hazards — nothing syncs until they are dealt
with — and a client that let the user turn off the only signal that sync has
stopped would be quietly useless rather than quietly quiet.

Everything on this page is something a reasonable person might not want to hear
about, and each switch maps to exactly one key in
:data:`~onedriveui.ui.notices.SETTING_FOR_TOAST`. That mapping is read in one
place, so a toggle cannot be honoured by the tray and ignored by the notifier.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from onedriveui.strings import SETTINGS
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.containers import SectionHeading, SettingsCard
from onedriveui.ui.widgets.controls import ToggleSwitch

log = logging.getLogger(__name__)

__all__ = ["NotificationsPage", "TOGGLES"]

#: ``(label, dotted config key)``, in the order Windows lists them. The five
#: Microsoft ships first, then the three this client adds.
TOGGLES: Final[tuple[tuple[str, str], ...]] = (
    (SETTINGS.N_PAUSED, "notifications.paused"),
    (SETTINGS.N_SHARED, "notifications.shared_or_edited"),
    (SETTINGS.N_MASS_DELETE, "notifications.mass_delete"),
    (SETTINGS.N_OTHER_ACCOUNTS, "notifications.other_accounts"),
    (SETTINGS.N_SYNC_ISSUES, "notifications.sync_issues"),
    (SETTINGS.N_CONFLICTS, "notifications.conflicts"),
)


class NotificationsPage(QWidget):
    """One switch per notification kind, applied immediately.

    Args:
        account: The account.
        config: The loaded config.
        supervisor: Unused here; accepted so every page has one signature.
        services: The engine's services.
        parent: Qt parent.

    Signals:
        changed: The dotted key that was just written.
    """

    changed = Signal(str)

    def __init__(self, account: Any, *, config: Any = None,
                 supervisor: Any = None, services: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self._config = config
        self._switches: dict[str, ToggleSwitch] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(SPACING["l"], SPACING["l"],
                                  SPACING["l"], SPACING["l"])
        column.setSpacing(SPACING["m"])
        column.addWidget(SectionHeading(SETTINGS.NAV_NOTIFICATIONS, self))

        for label, key in TOGGLES:
            column.addWidget(self._toggle(label, key))
        column.addStretch(1)

    def _toggle(self, label: str, key: str) -> QWidget:
        switch = ToggleSwitch(self)
        switch.setChecked(bool(self._read(key, True)))
        switch.toggled.connect(lambda on, k=key: self._write(k, on))
        self._switches[key] = switch
        return SettingsCard(label, self, content=switch, action_icon=False)

    def switch(self, key: str) -> ToggleSwitch | None:
        """One switch by its dotted key, for tests and deep links."""
        return self._switches.get(key)

    # ═════════════════════════════════════════════════════════════════════════
    # Config
    # ═════════════════════════════════════════════════════════════════════════

    def _read(self, key: str, default: Any) -> Any:
        if self._config is None:
            return default
        # Scoped to THIS page's account. `Config.get`/`set` otherwise resolve
        # every account key against the *active* account, so a Settings window
        # opened on the second account silently read and edited the first one's.
        return self._config.get(
            key, default, account_id=getattr(self.account, "id", None))

    def _write(self, key: str, value: Any) -> None:
        if self._config is None:
            return
        from onedriveui import config as config_module
        from onedriveui.bus import BUS

        if not self._config.set(
                key, value,
                account_id=getattr(self.account, "id", None)):
            return
        try:
            config_module.save(self._config)
        except Exception:  # noqa: BLE001
            log.error("could not save %s", key, exc_info=True)
            return
        BUS.config_changed.emit(key)
        self.changed.emit(key)
