""""Sync and back up" — the page where most of the settings live.

Three things here are worth knowing before reading the code.

**Every change applies immediately.** There is no Apply button: a toggle writes
``config.json`` atomically and emits ``config_changed`` with its dotted key, and
the engine reads it on the next tick. A settings window with an Apply button is
one that can be closed with unsaved changes still in it.

**Bandwidth limits are global, and the page says so.** ``core/bwlimit`` is
process-wide, so a limit set "for this account" throttles every account on the
device. Rather than pretend otherwise or quietly apply it,
:data:`~onedriveui.strings.SETTINGS.BANDWIDTH_GLOBAL_NOTE` sits under the
control.

**The Files On-Demand buttons are confirmed.** "Free up space" and "Download all
files" both do something large and slow that is annoying to undo, so each is a
dialog with the size in it rather than a button that starts immediately.

And the rule that governs the whole page: a control that cannot work on Linux is
**disabled with its reason inline**, never hidden. A missing control makes the
user hunt for a feature they know exists; a disabled one with a sentence tells
them what is true.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from onedriveui.constants import BANDWIDTH_CEIL_KB, BANDWIDTH_FLOOR_KB
from onedriveui.strings import DIALOG, SETTINGS
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.containers import (
    SectionHeading,
    SettingsCard,
    SettingsExpander,
)
from onedriveui.ui.widgets.controls import (
    ButtonVariant,
    FluentButton,
    ToggleSwitch,
)

log = logging.getLogger(__name__)

__all__ = ["SyncPage"]


class SyncPage(QWidget):
    """Backup, camera and screenshots, autostart, pause policy, Advanced.

    Args:
        account: The account being configured.
        config: The loaded :class:`~onedriveui.config.AppConfig`.
        supervisor: The Supervisor, for the actions.
        services: The engine's services, for the ones with their own object.
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
        self._supervisor = supervisor
        self._services = dict(services or {})
        self._cards: dict[str, QWidget] = {}

        column = QVBoxLayout(self)
        column.setContentsMargins(SPACING["l"], SPACING["l"],
                                  SPACING["l"], SPACING["l"])
        column.setSpacing(SPACING["m"])

        column.addWidget(SectionHeading(SETTINGS.NAV_SYNC, self))
        column.addWidget(self._backup_card())
        column.addWidget(self._toggle_card(SETTINGS.SCREENSHOTS,
                                           "extras.screenshots"))
        column.addWidget(self._toggle_card(SETTINGS.CAMERA_IMPORT,
                                           "extras.camera_import"))
        column.addWidget(self._toggle_card(SETTINGS.START_AT_SIGNIN,
                                           "app.autostart"))
        column.addWidget(self._toggle_card(SETTINGS.PAUSE_METERED,
                                           "pause.on_metered"))
        column.addWidget(self._toggle_card(SETTINGS.PAUSE_BATTERY,
                                           "pause.on_battery_saver"))
        column.addWidget(self._advanced())
        column.addStretch(1)

    # ═════════════════════════════════════════════════════════════════════════
    # Cards
    # ═════════════════════════════════════════════════════════════════════════

    def _backup_card(self) -> QWidget:
        card = SettingsCard(SETTINGS.MANAGE_BACKUP, self,
                            description=SETTINGS.BACKUP_DESC,
                            clickable=True)
        self._cards["backup"] = card
        return card

    def _toggle_card(self, title: str, key: str,
                     description: str = "") -> QWidget:
        """One switch, wired to one dotted config key.

        The wiring is uniform on purpose: every toggle on this page writes
        through the same :meth:`_write`, so "immediate apply" is a property of
        the page rather than something each control remembers to do.
        """
        switch = ToggleSwitch(self)
        switch.setChecked(bool(self._read(key, False)))
        switch.toggled.connect(lambda on, k=key: self._write(k, on))
        card = SettingsCard(title, self, description=description,
                            content=switch, action_icon=False)
        self._cards[key] = card
        return card

    def _advanced(self) -> QWidget:
        """The Advanced expander: collaboration, bandwidth, FOD, exclusions."""
        expander = SettingsExpander(SETTINGS.ADVANCED, self)
        expander.add_row(self._collaboration())
        expander.add_row(self._bandwidth())
        expander.add_row(self._files_on_demand())
        expander.add_row(self._excluded_extensions())
        self._cards["advanced"] = expander
        return expander

    def _collaboration(self) -> QWidget:
        """Which conflict policy: ask, or always keep both.

        "Newest wins" is not offered, here or anywhere: it is the one resolution
        that silently destroys somebody's work.
        """
        switch = ToggleSwitch(self)
        switch.setChecked(self._read("conflicts.policy", "ask") == "keep_both")
        switch.toggled.connect(
            lambda on: self._write("conflicts.policy",
                                   "keep_both" if on else "ask"))
        card = SettingsCard(SETTINGS.FILE_COLLAB, self,
                            description=SETTINGS.COLLAB_KEEP_BOTH,
                            content=switch, action_icon=False, boxed=False)
        self._cards["collaboration"] = card
        return card

    def _bandwidth(self) -> QWidget:
        """Two spinners in KB/s, and the note that they are global.

        The unit is the OneDrive UI's KB/s (1000). The conversion to rclone's
        KiB/s happens in exactly one function, ``units.kb_to_kib()``, and never
        here — one open-coded ratio makes every user's limit 2.4 % wrong forever
        and nobody ever notices.
        """
        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["s"])

        self._download_kb = self._rate_spinner("bandwidth.download_kb")
        self._upload_kb = self._rate_spinner("bandwidth.upload_kb")
        column.addWidget(self._labelled(SETTINGS.LIMIT_DOWNLOAD,
                                        self._download_kb, holder))
        column.addWidget(self._labelled(SETTINGS.LIMIT_UPLOAD,
                                        self._upload_kb, holder))

        auto = ToggleSwitch(holder)
        auto.setChecked(bool(self._read("bandwidth.upload_mode", "none") == "auto"))
        auto.toggled.connect(
            lambda on: self._write("bandwidth.upload_mode",
                                   "auto" if on else "none"))
        column.addWidget(self._labelled(SETTINGS.ADJUST_AUTO, auto, holder))

        # `core/bwlimit` is process-wide: a limit set "for this account"
        # throttles every account on the device. Saying so is cheaper than the
        # support thread that follows from not saying so.
        note = QLabel(SETTINGS.BANDWIDTH_GLOBAL_NOTE, holder)
        note.setWordWrap(True)
        column.addWidget(note)

        card = SettingsCard(SETTINGS.BANDWIDTH, self, content=holder,
                            action_icon=False, boxed=False)
        self._cards["bandwidth"] = card
        return card

    def _rate_spinner(self, key: str) -> QSpinBox:
        spinner = QSpinBox(self)
        spinner.setRange(BANDWIDTH_FLOOR_KB, BANDWIDTH_CEIL_KB)
        spinner.setSuffix(f" {SETTINGS.KB_PER_SEC}")
        spinner.setValue(int(self._read(key, None) or BANDWIDTH_FLOOR_KB))
        spinner.valueChanged.connect(lambda value, k=key: self._write(k, value))
        return spinner

    def _files_on_demand(self) -> QWidget:
        """Two buttons, each behind a confirmation with the size in it."""
        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["s"])

        self._free_up = FluentButton(SETTINGS.FREE_UP_SPACE, holder,
                                     variant=ButtonVariant.STANDARD)
        self._free_up.clicked.connect(self._on_free_up)
        column.addWidget(self._free_up)

        self._download_all = FluentButton(SETTINGS.DOWNLOAD_ALL, holder,
                                          variant=ButtonVariant.STANDARD)
        self._download_all.clicked.connect(self._on_download_all)
        column.addWidget(self._download_all)

        card = SettingsCard(SETTINGS.FOD, self, description=SETTINGS.FOD_DESC,
                            content=holder, action_icon=False, boxed=False)
        self._cards["fod"] = card
        return card

    def _excluded_extensions(self) -> QWidget:
        from PySide6.QtWidgets import QLineEdit

        field = QLineEdit(self)
        field.setText(", ".join(self._read("files.excluded_extensions", []) or []))
        field.editingFinished.connect(
            lambda: self._write("files.excluded_extensions",
                                [x.strip() for x in field.text().split(",")
                                 if x.strip()]))
        card = SettingsCard(SETTINGS.EXCLUDED_EXT, self, content=field,
                            action_icon=False, boxed=False)
        self._cards["excluded"] = card
        return card

    def _labelled(self, text: str, control: QWidget,
                  parent: QWidget) -> QWidget:
        holder = QWidget(parent)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["xs"])
        column.addWidget(QLabel(text, holder))
        column.addWidget(control)
        return holder

    # ═════════════════════════════════════════════════════════════════════════
    # Actions
    # ═════════════════════════════════════════════════════════════════════════

    def _on_free_up(self) -> None:
        """Confirm with the size, then go through ``do()``."""
        from onedriveui.models import RecoveryAction
        from onedriveui.ui.dialogs.file_dialogs import FreeUpSpaceDialog

        dialog = FreeUpSpaceDialog(self._cached_bytes(), self)
        dialog.exec()
        if dialog.approved() and self._supervisor is not None:
            self._supervisor.do(RecoveryAction.FREE_UP_SPACE)

    def _on_download_all(self) -> None:
        """Confirm with the size. On a 900 GB drive this is a request that
        cannot be granted, and the number is what makes that visible first."""
        from onedriveui.ui.dialogs.file_dialogs import DownloadAllDialog

        dialog = DownloadAllDialog(self._remote_bytes(), self)
        dialog.exec()
        if not dialog.approved():
            return
        pinner = self._services.get("pinner")
        if pinner is not None:
            pinner.download_all()

    def _cached_bytes(self) -> int:
        info = self._services.get("filestate")
        return int(getattr(info, "cached_bytes", 0) or 0)

    def _remote_bytes(self) -> int:
        quota = self._services.get("quota")
        return int(getattr(quota.current(), "used", 0)) if quota else 0

    # ═════════════════════════════════════════════════════════════════════════
    # Config
    # ═════════════════════════════════════════════════════════════════════════

    def _read(self, key: str, default: Any) -> Any:
        if self._config is None:
            return default
        return self._config.get(key, default)

    def _write(self, key: str, value: Any) -> None:
        """Write one dotted key, atomically, and announce it.

        Immediate apply: there is no Save button on this page, so the write and
        the signal happen on the toggle. The engine picks the change up on its
        next tick, which is at most two seconds away.
        """
        if self._config is None:
            return
        from onedriveui import config as config_module
        from onedriveui.bus import BUS

        if not self._config.set(key, value):
            return                      # unchanged, or an unknown key
        try:
            config_module.save(self._config)
        except Exception:  # noqa: BLE001 - a failed save must not lose the window
            log.error("could not save %s", key, exc_info=True)
            return
        BUS.config_changed.emit(key)
        self.changed.emit(key)

    def card(self, key: str) -> QWidget | None:
        """One card by key, for tests and for deep-link navigation."""
        return self._cards.get(key)
