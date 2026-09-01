"""The tray icon, and the four things StatusNotifierItem will not let you do.

The tray on GNOME is not a tray. It is ``org.kde.StatusNotifierItem`` exported
over D-Bus, rendered by the AppIndicator extension, and it is far more
restrictive than ``QSystemTrayIcon``'s API suggests. Four of those restrictions
shape everything here, and each one is a bug that looks like a Qt problem until
you know it is not.

**Icons are transmitted as *names*, never as pixmaps.** SNI sends an
``IconName`` string and the shell resolves it against the icon theme.
``setIcon(QPixmap(...))`` compiles, runs, and shows nothing. Every icon here goes
through ``QIcon.fromTheme()`` against the names this client installs into
``hicolor``.

**The menu is labels only.** ``QWidgetAction`` exports as an *empty label* —
not as a widget, not as nothing, as a blank row the user can click. So there is
no progress bar in the menu, no account header widget, no separators with text.
Everything the tray menu says, it says in a plain label.

**Left-click opens the menu.** The AppIndicator extension maps both buttons to
the menu; ``activated`` with ``Trigger`` never arrives. So "Open Activity
Center" is the **first** item and the default one, because for most users the
first item is what a left-click was going to do anyway.

**The menu must be rebuilt, not mutated.** Changing an action's visibility after
the menu has been exported reflows the DBusMenu incorrectly — the observed
symptom is "Quit" appearing *nested under* "Pause syncing" when the vault item
is toggled. Rebuilding the whole menu on each state change costs nothing at
2.5 Hz and is the only version that stays correct.

The spinner is eight themed icons at 125 ms. It is started and stopped by state
changes rather than left running, because an animation nobody is looking at
still wakes the compositor eight times a second.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from onedriveui.bus import BUS
from onedriveui.constants import SPINNER_FRAME_MS
from onedriveui.models import (
    AccountInfo,
    Facts,
    PauseReason,
    RecoveryAction,
    SyncState,
    TrayIcon,
    VaultState,
)
from onedriveui.strings import MENU, t
from onedriveui.sync.reducer import tooltip as tooltip_for
from onedriveui.sync.reducer import tray_for
from onedriveui.ui import icons

log = logging.getLogger(__name__)

__all__ = ["TrayItem", "SPINNING_STATES", "available"]

#: The states the spinner animates through. Everything else is a still icon —
#: an animation that never stops is an animation the user learns to ignore.
SPINNING_STATES: Final[frozenset[SyncState]] = frozenset({
    SyncState.SYNCING, SyncState.PROCESSING, SyncState.MOUNTING,
    SyncState.INITIALIZING,
})


def available() -> bool:
    """Is there anywhere for a tray icon to appear?

    On this machine: yes — the AppIndicator extension is installed and
    ``org.kde.StatusNotifierWatcher`` is owned by gnome-shell. On a session with
    neither, ``QSystemTrayIcon`` still constructs and simply shows nothing, so
    this is checked rather than assumed: a client whose only affordance is an
    icon that does not exist has no affordance at all, and the caller opens the
    Activity Center window instead.

    Returns ``False`` when there is no ``QApplication`` yet.
    ``isSystemTrayAvailable()`` **segfaults** if called before one exists — not
    raises, segfaults — so the guard is not defensive tidiness. Start-up code
    naturally wants to ask this question early, which is exactly when it is
    fatal.
    """
    if QApplication.instance() is None:
        return False
    return bool(QSystemTrayIcon.isSystemTrayAvailable())


class TrayItem(QObject):
    """One StatusNotifierItem, for one account.

    Args:
        account: The account this icon represents. One item per account: two
            accounts sharing an icon cannot show that one of them is broken.
        supervisor: The :class:`~onedriveui.sync.supervisor.Supervisor`. Every
            menu action goes through its ``do()`` or through a ``BUS`` signal —
            never straight to a service.
        vault: The account's :class:`~onedriveui.sync.vault.Vault`, or ``None``.
        parent: Qt parent.

    Signals:
        activity_requested: The user asked for the Activity Center.
        settings_requested: The user asked for Settings.
        quit_requested: The user asked to quit.
    """

    activity_requested = Signal(str)
    settings_requested = Signal(str)
    quit_requested = Signal()

    def __init__(
        self,
        account: AccountInfo,
        *,
        supervisor: Any = None,
        vault: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._supervisor = supervisor
        self._vault = vault

        self._state = SyncState.NOT_RUNNING
        self._facts = Facts(account_id=account.id)
        self._frame = 0
        self._issue_count = 0

        self._item = QSystemTrayIcon(self)
        self._menu = QMenu()
        self._item.setContextMenu(self._menu)
        # Never connect `activated`: the AppIndicator extension maps both mouse
        # buttons to the menu and this signal does not arrive. Relying on it
        # produces a tray icon that appears to do nothing when clicked.
        self._item.setToolTip(self.account.display_name or self.account.id)

        self._spinner = QTimer(self)
        self._spinner.setInterval(SPINNER_FRAME_MS)
        self._spinner.timeout.connect(self._advance_spinner)

        self._rebuild_menu()
        BUS.state_changed.connect(self._on_state_changed)

    # ═════════════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═════════════════════════════════════════════════════════════════════════

    def show(self) -> None:
        """Register the item with the watcher.

        ``NOT_RUNNING`` deliberately registers nothing: the *absence* of an icon
        is what "OneDrive is not running" looks like, and a grey icon claiming
        to represent a client that is not there is worse than no icon.
        """
        if tray_for(self._state, self.account) is TrayIcon.NONE:
            self._item.hide()
            return
        self._item.show()

    def hide(self) -> None:
        """Remove the item and stop the spinner."""
        self._stop_spinner()
        self._item.hide()

    @property
    def visible(self) -> bool:
        return self._item.isVisible()

    @property
    def state(self) -> SyncState:
        return self._state

    # ═════════════════════════════════════════════════════════════════════════
    # State
    # ═════════════════════════════════════════════════════════════════════════

    def _on_state_changed(self, _old: SyncState, new: SyncState,
                          facts: Facts) -> None:
        if facts.account_id and facts.account_id != self.account.id:
            return
        self.set_state(new, facts)

    def set_state(self, state: SyncState, facts: Facts | None = None) -> None:
        """Repaint the icon, the tooltip and the menu for a new state."""
        self._state = state
        if facts is not None:
            self._facts = facts
            self._issue_count = facts.issues_error + facts.issues_blocking

        if state in SPINNING_STATES:
            self._start_spinner()
        else:
            self._stop_spinner()
            self._apply_icon()

        # Both lines, from `reducer.tooltip()`, so the tray cannot disagree with
        # the Activity Center headline about what is happening.
        self._item.setToolTip(tooltip_for(state, self._facts))
        self._rebuild_menu()
        self.show()

    def _apply_icon(self) -> None:
        """Set the icon by **name**. Never a pixmap: SNI transmits an IconName."""
        tray = tray_for(self._state, self.account)
        if tray is TrayIcon.NONE:
            self._item.hide()
            return
        self._item.setIcon(icons.tray_icon(tray, frame=self._frame))

    # ── the spinner ─────────────────────────────────────────────────────────
    def _start_spinner(self) -> None:
        if self._spinner.isActive():
            return
        self._frame = 0
        self._apply_icon()
        self._spinner.start()

    def _stop_spinner(self) -> None:
        """Stop animating. An animation nobody watches still wakes the
        compositor eight times a second."""
        self._spinner.stop()
        self._frame = 0

    def _advance_spinner(self) -> None:
        self._frame = (self._frame + 1) % len(icons.SPINNER_FRAMES)
        self._apply_icon()

    # ═════════════════════════════════════════════════════════════════════════
    # The menu
    # ═════════════════════════════════════════════════════════════════════════

    def _rebuild_menu(self) -> None:
        """Build the menu from scratch. **Rebuilt, never mutated.**

        Toggling an existing action's visibility after the menu has been
        exported reflows the DBusMenu incorrectly — the observed symptom is
        "Quit" appearing nested *under* "Pause syncing" when the vault item is
        shown or hidden. Rebuilding costs nothing at this cadence and is the
        only version that stays correct.

        Labels only, throughout. ``QWidgetAction`` exports as an empty,
        clickable, blank row, so there is no progress bar and no account header
        here — those live in the Activity Center, which is why opening it is the
        first item.
        """
        self._menu.clear()

        # First and default: the AppIndicator extension maps left-click to the
        # menu, so the first item is what most clicks were going to mean.
        self._add(MENU.OPEN_ACTIVITY, self._open_activity, default=True)
        self._add(MENU.OPEN_FOLDER, self._open_folder)
        self._add(MENU.VIEW_ONLINE, self._view_online)
        self._menu.addSeparator()

        if self._issue_count:
            self._add(t(MENU.SYNC_PROBLEMS, n=self._issue_count),
                      self._open_activity)

        if self._is_paused():
            self._add(MENU.RESUME, self._resume)
        else:
            pause = self._menu.addMenu(MENU.PAUSE)
            for hours, label in ((2, MENU.PAUSE_2H), (8, MENU.PAUSE_8H),
                                 (24, MENU.PAUSE_24H), (None, MENU.PAUSE_UNTIL)):
                action = pause.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, h=hours: self._pause(h))

        self._add_vault_items()

        self._menu.addSeparator()
        self._add(MENU.RECYCLE_BIN, self._open_recycle_bin)
        self._add(MENU.SETTINGS, self._open_settings)
        self._menu.addSeparator()
        self._add(MENU.QUIT, self._quit)

    def _add(self, label: str, slot: Any, *, default: bool = False) -> QAction:
        action = self._menu.addAction(label)
        action.triggered.connect(lambda _checked=False: slot())
        if default:
            self._menu.setDefaultAction(action)
        return action

    def _add_vault_items(self) -> None:
        """Lock or unlock, when there is a vault to lock."""
        if self._vault is None:
            return
        try:
            state = self._vault.state()
        except Exception:  # noqa: BLE001 - the menu must always build
            log.debug("could not read the vault state", exc_info=True)
            return
        if state is VaultState.ABSENT:
            return
        if state is VaultState.UNLOCKED:
            self._add(MENU.LOCK_VAULT, self._vault.lock)
        else:
            self._add(MENU.UNLOCK_VAULT, self._vault.unlock)

    def _is_paused(self) -> bool:
        return self._state in (SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
                               SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA)

    # ═════════════════════════════════════════════════════════════════════════
    # Actions — every one through `do()` or the bus
    # ═════════════════════════════════════════════════════════════════════════

    def _open_activity(self) -> None:
        self.activity_requested.emit(self.account.id)

    def _open_settings(self) -> None:
        self.settings_requested.emit(self.account.id)

    def _quit(self) -> None:
        self.quit_requested.emit()

    def _open_folder(self) -> None:
        self._do(RecoveryAction.SHOW_IN_FOLDER, path=self.account.sync_root)

    def _view_online(self) -> None:
        self._do(RecoveryAction.OPEN_WEB)

    def _open_recycle_bin(self) -> None:
        from onedriveui.sync.trashbin import web_recyclebin_url

        self._do(RecoveryAction.OPEN_WEB, url=web_recyclebin_url(self.account))

    def _pause(self, hours: int | None) -> None:
        if self._supervisor is None:
            return
        self._supervisor.request_pause(PauseReason.MANUAL, hours)

    def _resume(self) -> None:
        if self._supervisor is None:
            return
        self._supervisor.request_resume()

    def _do(self, action: RecoveryAction, **kw: Any) -> None:
        """Every world-changing menu item funnels through `Supervisor.do()`.

        Not a convenience: a safety check added once to `do()` is added to every
        route that reaches it, and the tray is one of three places the same
        actions are offered from.
        """
        if self._supervisor is None:
            log.debug("no supervisor wired; %s was not performed", action.value)
            return
        self._supervisor.do(action, **kw)
