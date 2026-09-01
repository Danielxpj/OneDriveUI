"""Our own file browser, for the Status column Nautilus cannot always give us.

The Nautilus extension is the primary route: emblems on the icons, a Status
column, and a context submenu, right where the user already keeps their files.
This window exists for the cases where that is not enough or not there — a user
on a different file manager, a machine where the extension is not installed, and
the "Free up space" / "Always keep on this device" batch operations that are
painful to drive from a context menu one file at a time.

Two things make it fast enough to be worth having:

**It is lazy.** A directory's children are read when it is expanded, from the
local ``cache_index`` — not from the mount, which for an online-only folder is a
Graph request per directory (OneDrive has ``ListR = false``).

**The Status column is one query per expansion, not one per row.** It comes from
:meth:`~onedriveui.sync.filestate.FileStateService.statuses`, which answers a
thousand paths in a single indexed lookup. A per-row lookup would make scrolling
a large folder visibly stutter.

Every action here funnels through :meth:`~onedriveui.sync.supervisor.Supervisor.do`,
identically to the tray menu and the Nautilus submenu — so a guard added once
covers all three, and no route can quietly skip one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtGui import QAction, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from onedriveui.models import AccountInfo, FileState, RecoveryAction
from onedriveui.strings import ACTION_LABEL, FILE_STATE_LABEL, SETTINGS
from onedriveui.ui import icons

log = logging.getLogger(__name__)

__all__ = ["FileBrowser", "COLUMNS", "PATH_ROLE", "STATE_ROLE"]

#: Explorer's own column set, in Explorer's order.
COLUMNS: Final[tuple[str, ...]] = ("Name", "Status", "Size")

#: The item's path relative to the sync root.
PATH_ROLE: Final = int(Qt.ItemDataRole.UserRole) + 1
#: Its :class:`~onedriveui.models.FileState`.
STATE_ROLE: Final = int(Qt.ItemDataRole.UserRole) + 2
#: Whether it is a directory.
DIR_ROLE: Final = int(Qt.ItemDataRole.UserRole) + 3


class FileBrowser(QWidget):
    """A lazy tree of the sync root with a Status column and the file actions.

    Args:
        account: The account whose folder is shown.
        supervisor: The Supervisor. Every action goes through its ``do()``.
        filestate: The
            :class:`~onedriveui.sync.filestate.FileStateService` the Status
            column reads from.
        parent: Qt parent.

    Signals:
        activated: The relative path of a double-clicked file.
    """

    activated = Signal(str)

    def __init__(
        self,
        account: AccountInfo,
        *,
        supervisor: Any = None,
        filestate: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._supervisor = supervisor
        self._filestate = filestate
        self._root = Path(account.sync_root).expanduser()

        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(list(COLUMNS))

        self._view = QTreeView(self)
        self._view.setModel(self._model)
        self._view.setUniformRowHeights(True)          # the virtualisation win
        self._view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        self._view.expanded.connect(self._on_expanded)
        self._view.doubleClicked.connect(self._on_activated)

        header = self._view.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        # Explorer never scrolls horizontally, and neither does this: a
        # horizontal scrollbar in a file list is a layout bug the user has to
        # work around.
        self._view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self.reload()

    # ═════════════════════════════════════════════════════════════════════════
    # Populating
    # ═════════════════════════════════════════════════════════════════════════

    def reload(self) -> None:
        """Rebuild from the sync root."""
        self._model.removeRows(0, self._model.rowCount())
        self._populate(self._model.invisibleRootItem(), self._root)

    def _on_expanded(self, index: QModelIndex) -> None:
        """Fill a folder's children the first time it is opened.

        Lazily, because reading the whole tree up front means one Graph request
        per directory for anything not cached — which on a large drive exhausts
        the per-user rate limit before the window has finished opening.
        """
        item = self._model.itemFromIndex(index)
        if item is None or not item.data(DIR_ROLE):
            return
        if item.rowCount() != 1 or item.child(0).data(PATH_ROLE) is not None:
            return                                     # already populated
        item.removeRow(0)
        self._populate(item, self._root / str(item.data(PATH_ROLE) or ""))

    def _populate(self, parent: QStandardItem, directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir(),
                             key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            log.debug("could not list %s", directory, exc_info=True)
            return

        rows = [self._relative(path) for path in entries]
        statuses = self._statuses(rows)
        for path, rel in zip(entries, rows, strict=True):
            parent.appendRow(self._row(path, rel, statuses.get(rel)))

    def _row(self, path: Path, rel: str, status: Any) -> list[QStandardItem]:
        is_dir = path.is_dir()
        state = getattr(status, "state", FileState.UNKNOWN)

        name = QStandardItem(path.name)
        name.setEditable(False)
        name.setData(rel, PATH_ROLE)
        name.setData(state, STATE_ROLE)
        name.setData(is_dir, DIR_ROLE)
        emblem = icons.emblem_name(state)
        if emblem:
            name.setIcon(icons.emblem_icon(emblem))

        status_item = QStandardItem(FILE_STATE_LABEL.get(state.value, ""))
        status_item.setEditable(False)

        size_item = QStandardItem("" if is_dir else _human(path))
        size_item.setEditable(False)
        size_item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignVCenter)

        if is_dir:
            # A placeholder child, so the expander arrow appears without the
            # directory having been read.
            name.appendRow(QStandardItem(""))
        return [name, status_item, size_item]

    def _statuses(self, rel_paths: list[str]) -> dict[str, Any]:
        """One query for the whole directory, never one per row."""
        if self._filestate is None or not rel_paths:
            return {}
        try:
            return self._filestate.statuses(rel_paths)
        except Exception:  # noqa: BLE001 - a missing badge is not worth a crash
            log.debug("could not read file states", exc_info=True)
            return {}

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._root))
        except ValueError:
            return path.name

    # ═════════════════════════════════════════════════════════════════════════
    # Actions
    # ═════════════════════════════════════════════════════════════════════════

    def selected_paths(self) -> list[str]:
        """The relative paths currently selected, deduplicated."""
        out: list[str] = []
        for index in self._view.selectionModel().selectedRows(0):
            rel = self._model.itemFromIndex(index).data(PATH_ROLE)
            if rel and rel not in out:
                out.append(str(rel))
        return out

    def _on_context_menu(self, point: Any) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        menu = QMenu(self)
        for action, label in (
            (RecoveryAction.FREE_UP_SPACE, SETTINGS.FREE_UP_SPACE),
            (RecoveryAction.SHOW_IN_FOLDER,
             ACTION_LABEL[RecoveryAction.SHOW_IN_FOLDER]),
            (RecoveryAction.OPEN_WEB, ACTION_LABEL[RecoveryAction.OPEN_WEB]),
            (RecoveryAction.STOP_SYNCING_ITEM,
             ACTION_LABEL[RecoveryAction.STOP_SYNCING_ITEM]),
        ):
            entry: QAction = menu.addAction(label)
            entry.triggered.connect(
                lambda _checked=False, a=action, p=list(paths): self._act(a, p))
        menu.exec(self._view.viewport().mapToGlobal(point))

    def _act(self, action: RecoveryAction, paths: list[str]) -> None:
        """Perform an action on every selected path, through ``do()``.

        The same entry point the tray menu and the Nautilus submenu use, so a
        guard added once applies to all three and no route can skip one.
        """
        if self._supervisor is None:
            log.debug("no supervisor wired; %s was not performed", action.value)
            return
        for rel in paths:
            self._supervisor.do(action, rel_path=rel,
                                path=str(self._root / rel))

    def _on_activated(self, index: QModelIndex) -> None:
        item = self._model.itemFromIndex(index.siblingAtColumn(0))
        if item is None or item.data(DIR_ROLE):
            return
        rel = item.data(PATH_ROLE)
        if rel:
            self.activated.emit(str(rel))

    # ═════════════════════════════════════════════════════════════════════════
    # For tests and callers
    # ═════════════════════════════════════════════════════════════════════════

    @property
    def model(self) -> QStandardItemModel:
        return self._model

    @property
    def view(self) -> QTreeView:
        return self._view

    def row_count(self) -> int:
        return self._model.rowCount()

    def state_of(self, rel_path: str) -> FileState:
        """The state shown for one path, or ``UNKNOWN``."""
        for row in range(self._model.rowCount()):
            item = self._model.item(row, 0)
            if item is not None and item.data(PATH_ROLE) == rel_path:
                return item.data(STATE_ROLE) or FileState.UNKNOWN
        return FileState.UNKNOWN


def _human(path: Path) -> str:
    from onedriveui.units import human_bytes

    try:
        return human_bytes(path.stat().st_size)
    except OSError:
        return ""
