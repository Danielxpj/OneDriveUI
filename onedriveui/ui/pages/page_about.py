""""About" — versions, diagnostics, and the two things that are wrong on this machine.

Most of the page is what an About page always is. Two parts are not:

**The orphaned-cache reclaim.** rclone hashes the set of *command-line* backend
overrides into the filesystem's canonical name, so one ``--onedrive-chunk-size``
on a mount command line turns ``onedrive:`` into ``onedrive{MxOuf}:`` — a
different VFS cache directory. Every previously materialised file instantly
becomes online-only and the old tree is stranded on disk forever. **This machine
is carrying two such orphans right now**, from exactly that mistake, which is
why invariant I1 exists and why this page offers to reclaim the damage already
done rather than only preventing more.

**The disk-usage note.** Every tool on the system reports the *apparent* size of
an online-only file, because the cache file is preallocated to the object's full
remote size on first open. ``du``, ``ls -l`` and the file manager's properties
pane all say 50 MB for a file holding 192 KiB. Ours come from
``SEEK_DATA``/``SEEK_HOLE`` and are the real ones — and a user who has just seen
two different numbers deserves to be told which is which.

The diagnostics button produces a **redacted** bundle. Tokens, passwords and the
rc credentials are stripped before anything is written, because the whole point
of a bundle is that it can be attached to a public issue.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from onedriveui import APP_NAME, __version__
from onedriveui.strings import DIALOG, SETTINGS
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.containers import SectionHeading, SettingsCard
from onedriveui.ui.widgets.controls import ButtonVariant, FluentButton
from onedriveui.units import human_bytes

log = logging.getLogger(__name__)

__all__ = ["AboutPage"]


class AboutPage(QWidget):
    """Versions, the orphaned-cache reclaim, the disk-usage note, diagnostics.

    Args:
        account: The account.
        config: The loaded config.
        supervisor: The Supervisor, for the reclaim.
        services: The engine's services.
        parent: Qt parent.

    Signals:
        reclaimed: Bytes freed by the orphaned-cache reclaim.
    """

    reclaimed = Signal(int)

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

        column.addWidget(SectionHeading(SETTINGS.NAV_ABOUT, self))
        column.addWidget(self._versions())
        column.addWidget(self._orphaned_cache())
        column.addWidget(self._disk_usage_note())
        column.addWidget(self._diagnostics())
        column.addStretch(1)

    # ═════════════════════════════════════════════════════════════════════════
    # Cards
    # ═════════════════════════════════════════════════════════════════════════

    def _versions(self) -> QWidget:
        card = SettingsCard(f"{APP_NAME} {__version__}", self,
                            description=self._rclone_version(),
                            action_icon=False)
        self._cards["versions"] = card
        return card

    def _rclone_version(self) -> str:
        """rclone's version, or a note that it is missing.

        Worth showing because almost every behaviour this client depends on was
        verified against one specific build, and a user on a much older rclone
        will see things this codebase says are impossible.
        """
        from onedriveui.rc import ops

        try:
            return str(ops.core_version())
        except Exception:  # noqa: BLE001 - an About page never fails to open
            return ""

    def _orphaned_cache(self) -> QWidget:
        """The reclaim button, with the size it would free.

        The orphans exist because a ``--onedrive-*`` flag on a mount command
        line renames the filesystem and therefore its cache directory. Invariant
        I1 stops this client creating any more; this reclaims what is already
        stranded, which on this machine is two whole trees.
        """
        freed = self._orphan_bytes()
        button = FluentButton(SETTINGS.FREE_UP_SPACE, self,
                              variant=ButtonVariant.STANDARD)
        button.clicked.connect(self._on_reclaim)
        card = SettingsCard(
            SETTINGS.FREE_UP_SPACE, self,
            description=(human_bytes(freed) if freed else ""),
            content=button, action_icon=False)
        self._cards["orphaned_cache"] = card
        return card

    def _orphan_bytes(self) -> int:
        mountd = self._services.get("mountd")
        if mountd is None:
            return 0
        try:
            from onedriveui.rc import vfs

            endpoint = mountd.endpoint(self.account)
            if endpoint is None:
                return 0
            info = vfs.disk_cache_info(endpoint)
            return sum(size for _path, size in vfs.orphaned_cache_trees(info))
        except Exception:  # noqa: BLE001
            log.debug("could not measure the orphaned cache", exc_info=True)
            return 0

    def _disk_usage_note(self) -> QWidget:
        """Why `du` disagrees with us.

        Not a footnote: it is the single most reported "bug" in a
        files-on-demand client, and it is not a bug at all.
        """
        note = QLabel(DIALOG.DU_ON_MOUNT_NOTE, self)
        note.setWordWrap(True)
        card = SettingsCard(DIALOG.WHERE_ARE_MY_FILES, self, content=note,
                            action_icon=False)
        self._cards["disk_usage"] = card
        return card

    def _diagnostics(self) -> QWidget:
        button = FluentButton(DIALOG.SAVE, self, variant=ButtonVariant.STANDARD)
        button.clicked.connect(self._on_diagnostics)
        card = SettingsCard(DIALOG.WHERE_ARE_MY_FILES, self,
                            description=SETTINGS.ADVANCED,
                            content=button, action_icon=False)
        self._cards["diagnostics"] = card
        return card

    # ═════════════════════════════════════════════════════════════════════════
    # Actions
    # ═════════════════════════════════════════════════════════════════════════

    def _on_reclaim(self) -> None:
        if self._supervisor is None:
            return
        freed = self._supervisor.reclaim_orphaned_cache()
        log.info("reclaimed %s of orphaned cache", human_bytes(freed))
        self.reclaimed.emit(freed)

    def _on_diagnostics(self) -> None:
        """Build the bundle. **Redacted before anything is written.**

        Tokens, passwords and the rc credentials are stripped by
        ``applog.redact()`` on the way in, because the entire purpose of a
        bundle is that it can be attached to a public issue without a second
        thought.
        """
        from onedriveui import applog

        try:
            bundle = applog.build_diagnostics_bundle()
        except Exception:  # noqa: BLE001
            log.error("could not build the diagnostics bundle", exc_info=True)
            return
        log.info("diagnostics bundle written to %s", bundle)

    def card(self, key: str) -> QWidget | None:
        return self._cards.get(key)
