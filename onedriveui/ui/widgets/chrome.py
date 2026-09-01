"""Window chrome: the navigation rail, the search box and the status glyph.

Three shaping decisions, each with a reason:

  * **The nav rail is a `QListWidget` with a delegate, not a `QTabWidget`.** A
    tab bar reads as Windows 10; Fluent's `NavigationView` is a left rail. The
    delegate exists because the frozen `#NavList::item` QSS rules cannot draw
    the 3 x 16 accent selection indicator, and because a `::item` background
    would paint full-bleed underneath the 4 px-inset hover pill rather than
    behind it.
  * **`sizeHint` returns width 0 here too.** The rail is as wide as its pane, so
    the same vertical/horizontal scrollbar feedback loop that bites the activity
    list would bite it.
  * **The search box's clear button is our own `QAction`, not
    `setClearButtonEnabled`.** Qt's built-in button carries a platform icon that
    cannot be re-tinted for the dark theme, and QSS can only replace it with an
    `image: url(...)` — a path on disk, which the SVG-rendering icon registry
    deliberately does not produce. A trailing `QAction` gives the frozen 12 px
    glyph at the right colour in both themes.

No colour, no icon name and no user-facing string is written here: colours come
from `theme`, glyphs from `icons.GLYPHS`, the state -> icon semantics from
`strings.TRAY_FOR_STATE`, and every label is passed in by the caller.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QModelIndex, QPersistentModelIndex, QRectF, QSize, Qt, QTimer, Signal,
)
from PySide6.QtGui import QAction, QColor, QFontMetricsF, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QLineEdit, QListWidget, QListWidgetItem,
    QSizePolicy, QStyle, QStyleOptionViewItem, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

from onedriveui.bus import BUS
from onedriveui.models import BUSY_STATES, SyncState, TrayIcon
from onedriveui.strings import TRAY_FOR_STATE
from onedriveui.ui import fonts, icons, motion, qss, theme
from onedriveui.ui.theme import METRICS, OBJ, PROP, RADII, SPACING
from onedriveui.ui.widgets.containers import glyph_pixmap
from onedriveui.ui.widgets.controls import (
    ButtonVariant, FluentButton, FluentLineEdit, ThemeAware,
)

# ═════════════════════════════════════════════════════════════════════════════
# NavigationView geometry, verbatim from `NavigationView_themeresources.xaml`.
# ═════════════════════════════════════════════════════════════════════════════

NAV_OPEN_W: int = METRICS["nav_open_w"]          # OpenPaneLength 320
NAV_COMPACT_W: int = METRICS["nav_compact_w"]    # CompactPaneLength 48
NAV_ITEM_H: int = METRICS["nav_item_h"]          # ItemOnLeftMinHeight 36
NAV_ITEM_MARGIN: tuple[int, int] = METRICS["nav_item_margin"]   # 4 h, 2 v
NAV_ICON_BOX: int = METRICS["nav_icon_box"]      # 40
NAV_GLYPH: int = METRICS["nav_glyph"]            # 16
NAV_INDICATOR_W: int = METRICS["nav_indicator_w"]   # 3
NAV_INDICATOR_H: int = METRICS["nav_indicator_h"]   # 16
NAV_TOGGLE: tuple[int, int] = METRICS["nav_toggle"]  # 40 x 36
#: The label starts this far after the 40 px icon box.
NAV_LABEL_GAP: int = SPACING["xs"]
#: The rail's own inset, so the first item does not touch the pane's top edge.
NAV_PANE_PAD: int = SPACING["xs"]

#: The item's glyph key and its stable identifier, carried as item data.
NAV_ICON_ROLE: int = int(Qt.ItemDataRole.UserRole) + 1
NAV_KEY_ROLE: int = int(Qt.ItemDataRole.UserRole) + 2

#: The type-ramp role a nav label draws with.
NAV_ROLE: str = "body"

# ═════════════════════════════════════════════════════════════════════════════
# SearchBox
# ═════════════════════════════════════════════════════════════════════════════

#: The inner clear glyph is 12 px (`TextBox` inner-button icon size).
SEARCH_CLEAR_GLYPH: int = SPACING["m"]
#: Text is settled for this long before `search_changed` fires. This is a
#: debounce, not an animation, so it is deliberately NOT gated through
#: `motion.DUR` — a user who has turned animations off still wants the search to
#: wait until they stop typing.
SEARCH_DEBOUNCE_MS: int = 200
#: Where the clear button hangs. Named once so the class body reads as prose.
SEARCH_CLEAR_POSITION = QLineEdit.ActionPosition.TrailingPosition

_GLYPH_CLEAR = "close"
_GLYPH_TOGGLE = "view_list"
for _key in (_GLYPH_CLEAR, _GLYPH_TOGGLE):
    icons.glyph_stem(_key)                   # raises KeyError on an unknown key
del _key

_TOKEN_TEXT = "TextFillColorPrimary"
_TOKEN_TEXT2 = "TextFillColorSecondary"
_TOKEN_TEXT_OFF = "TextFillColorDisabled"
_TOKEN_HOVER = "SubtleFillColorSecondary"
_TOKEN_SELECTED = "ControlAltFillColorTertiary"


# ═════════════════════════════════════════════════════════════════════════════
# StatusGlyph semantics
# ═════════════════════════════════════════════════════════════════════════════

#: tray semantic -> the in-app glyph that says the same thing. The state -> tray
#: mapping itself is `strings.TRAY_FOR_STATE`, so a state cannot gain a headline
#: without also gaining a glyph, and there is only ever one such table.
GLYPH_FOR_TRAY: dict[TrayIcon, str] = {
    TrayIcon.SYNCED:     "check",
    TrayIcon.SYNCED_BIZ: "check",
    TrayIcon.SYNCING:    "sync",
    TrayIcon.PAUSED:     "pause",
    TrayIcon.SIGNED_OUT: "cloud_off",
    TrayIcon.ERROR:      "error",
    TrayIcon.WARNING:    "warning",
    TrayIcon.INFO:       "info",
    TrayIcon.BLOCKED:    "blocked",
    TrayIcon.NONE:       "",
}

#: tray semantic -> the token it is tinted with. "" is the live accent, which is
#: what "something is happening" reads as everywhere else in the app.
TONE_FOR_TRAY: dict[TrayIcon, str] = {
    TrayIcon.SYNCED:     "SystemFillColorSuccess",
    TrayIcon.SYNCED_BIZ: "SystemFillColorSuccess",
    TrayIcon.SYNCING:    "",
    TrayIcon.PAUSED:     "SystemFillColorSolidNeutral",
    TrayIcon.SIGNED_OUT: "SystemFillColorSolidNeutral",
    TrayIcon.ERROR:      "SystemFillColorCritical",
    TrayIcon.WARNING:    "SystemFillColorCaution",
    TrayIcon.INFO:       "",
    TrayIcon.BLOCKED:    "SystemFillColorCritical",
    TrayIcon.NONE:       "TextFillColorDisabled",
}

#: One turn of the syncing glyph. The tray's own spinner period, so the in-app
#: indicator and the panel icon rotate together.
SPIN_PERIOD_MS: int = icons.SPINNER_PERIOD_MS
#: A full turn, in degrees.
FULL_TURN_DEG: float = 360.0

for _table, _label in ((GLYPH_FOR_TRAY, "GLYPH_FOR_TRAY"),
                       (TONE_FOR_TRAY, "TONE_FOR_TRAY")):
    _missing = [member.name for member in TrayIcon if member not in _table]
    if _missing:                                       # pragma: no cover
        raise ValueError(f"chrome: {_label} is missing {_missing}")
_bad = [key for key in GLYPH_FOR_TRAY.values() if key and key not in icons.GLYPHS]
if _bad:                                               # pragma: no cover
    raise ValueError(f"chrome: GLYPH_FOR_TRAY names unknown glyph keys {_bad}")
_bad = [token for token in TONE_FOR_TRAY.values() if token and token not in theme.TOKENS]
if _bad:                                               # pragma: no cover
    raise ValueError(f"chrome: TONE_FOR_TRAY names unknown theme tokens {_bad}")
_missing = [state.name for state in SyncState if state not in TRAY_FOR_STATE]
if _missing:                                           # pragma: no cover
    raise ValueError(f"chrome: strings.TRAY_FOR_STATE is missing {_missing}")
del _table, _label, _missing, _bad


def tray_for(state: SyncState) -> TrayIcon:
    """The tray semantic for a sync state, from the frozen WP-00 table."""
    return TRAY_FOR_STATE[SyncState(state)]


def glyph_for_state(state: SyncState) -> str:
    """The in-app glyph key for a sync state. "" means "draw nothing"."""
    return GLYPH_FOR_TRAY[tray_for(state)]


def tone_for_state(state: SyncState) -> str:
    """The opaque `#RRGGBB` a sync state's glyph is drawn in."""
    token = TONE_FOR_TRAY[tray_for(state)]
    return theme.T(token) if token else theme.accent()


# ═════════════════════════════════════════════════════════════════════════════
# NavigationView
# ═════════════════════════════════════════════════════════════════════════════

class _NavDelegate(QStyledItemDelegate):
    """Paints one rail item: hover pill, accent indicator, 16 px glyph, label."""

    def __init__(self, parent: QWidget | None = None, *,
                 compact: bool = False) -> None:
        super().__init__(parent)
        self._compact = bool(compact)
        self._pixmaps: dict[tuple[str, int, str], QPixmap] = {}
        self._metrics = QFontMetricsF(fonts.font(NAV_ROLE))
        BUS.theme_changed.connect(self._on_theme_changed)

    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        self._compact = bool(compact)
        self._repaint_view()

    def _on_theme_changed(self, _dark: bool, _accent: str) -> None:
        self.refresh_theme()

    def refresh_theme(self) -> None:
        """Drop the glyph cache and the cached metrics, then repaint."""
        self._pixmaps.clear()
        self._metrics = QFontMetricsF(fonts.font(NAV_ROLE))
        self._repaint_view()

    def _repaint_view(self) -> None:
        parent = self.parent()
        if isinstance(parent, QAbstractItemView):
            parent.viewport().update()

    def _glyph(self, key: str, colour: str, dpr: float) -> QPixmap:
        cache_key = (key, NAV_GLYPH, colour)
        pixmap = self._pixmaps.get(cache_key)
        if pixmap is None:
            pixmap = glyph_pixmap(key, NAV_GLYPH, colour, dpr)
            self._pixmaps[cache_key] = pixmap
        return pixmap

    def sizeHint(self,
                 option: QStyleOptionViewItem,
                 index: QModelIndex | QPersistentModelIndex) -> QSize:
        """**Width 0** — the rail is exactly as wide as its pane."""
        return QSize(0, NAV_ITEM_H + 2 * NAV_ITEM_MARGIN[1])

    def paint(self,
              painter: QPainter,
              option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        """Draw the rail item. The base implementation is never called, so the
        frozen `#NavList::item` background cannot paint full-bleed underneath
        the inset pill."""
        rect = QRectF(option.rect)
        state = option.state
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        pill = rect.adjusted(NAV_ITEM_MARGIN[0], NAV_ITEM_MARGIN[1],
                             -NAV_ITEM_MARGIN[0], -NAV_ITEM_MARGIN[1])
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.T(_TOKEN_SELECTED if selected
                                            else _TOKEN_HOVER)))
            painter.drawRoundedRect(pill, RADII["hover_pill"], RADII["hover_pill"])
        if selected:
            bar = QRectF(pill.left() + 1.0,
                         pill.center().y() - NAV_INDICATOR_H / 2.0,
                         float(NAV_INDICATOR_W), float(NAV_INDICATOR_H))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.accent()))
            painter.drawRoundedRect(bar, RADII["selection_indicator"],
                                    RADII["selection_indicator"])

        colour = theme.T(_TOKEN_TEXT if enabled else _TOKEN_TEXT_OFF)
        key = str(index.data(NAV_ICON_ROLE) or "")
        if key:
            pixmap = self._glyph(key, colour, dpr)
            painter.drawPixmap(
                int(pill.left() + (NAV_ICON_BOX - NAV_GLYPH) / 2.0),
                int(pill.center().y() - NAV_GLYPH / 2.0),
                pixmap,
            )
        if self._compact:
            painter.restore()
            return

        left = pill.left() + NAV_ICON_BOX + NAV_LABEL_GAP
        width = max(0.0, pill.right() - left - NAV_LABEL_GAP)
        if width > 0.0:
            painter.setFont(fonts.font(NAV_ROLE))
            painter.setPen(QColor(colour))
            painter.drawText(
                QRectF(left, pill.top(), width, pill.height()),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._metrics.elidedText(
                    str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
                    Qt.TextElideMode.ElideRight, int(width)),
            )
        painter.restore()


class NavigationView(ThemeAware, QFrame):
    """The Fluent left navigation rail.

    320 px open, 48 px compact, 36 px items with a 4/2 margin and the 3 x 16 r2
    accent selection indicator. The settings window's four destinations — sync
    and backup, account, notifications, about — live here; the labels themselves
    come from `strings.py`, never from this module.

    Compacting is a decision about the *window*, not about the rail, so
    :meth:`set_compact` is exposed and nothing here watches a width. WP-13's
    settings window collapses the pane below 640 px.
    """

    #: Emitted with the newly selected row index.
    current_changed = Signal(int)
    #: Emitted with the newly selected item's stable key, for deep links.
    navigated = Signal(str)
    #: Emitted when the pane toggle button is pressed.
    toggled_compact = Signal(bool)

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 compact: bool = False,
                 toggle_tooltip: str = "") -> None:
        """
        Args:
            compact: Start as the 48 px icon-only rail.
            toggle_tooltip: Supplied by the caller from `strings.py`; never
                defaulted to a literal here.
        """
        super().__init__(parent)
        self._compact = bool(compact)
        self.setObjectName(OBJ.NAV_PANE)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        column = QVBoxLayout(self)
        column.setContentsMargins(NAV_PANE_PAD, NAV_PANE_PAD, NAV_PANE_PAD,
                                  NAV_PANE_PAD)
        column.setSpacing(NAV_PANE_PAD)

        # A SUBTLE button, not `icon_button()`: the frozen `#IconButton` rule
        # pins the box to 32 px, and the pane toggle is 40 x 36.
        self._toggle = FluentButton("", self, variant=ButtonVariant.SUBTLE,
                                    icon_key=_GLYPH_TOGGLE, icon_size=NAV_GLYPH)
        if toggle_tooltip:
            self._toggle.setToolTip(toggle_tooltip)
        self._toggle.setStyleSheet(self.toggle_qss())
        self._toggle.setFixedSize(NAV_TOGGLE[0], NAV_TOGGLE[1])
        self._toggle.setVisible(False)
        self._toggle.clicked.connect(self._on_toggle_clicked)
        column.addWidget(self._toggle)

        self._list = QListWidget(self)
        self._list.setObjectName(OBJ.NAV_LIST)
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.setMouseTracking(True)
        self._list.setUniformItemSizes(True)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._delegate = _NavDelegate(self._list, compact=self._compact)
        self._list.setItemDelegate(self._delegate)
        self._list.currentRowChanged.connect(self._on_row_changed)
        column.addWidget(self._list, 1)

        self._apply_width()
        self._install_theme_hook()

    # ── styling ──────────────────────────────────────────────────────────
    @staticmethod
    def toggle_qss() -> str:
        """Pin the pane toggle to WinUI's 40 x 36 box, as a widget stylesheet.

        `setFixedSize()` alone does not survive: `QStyleSheetStyle.polish()`
        rewrites the widget's minimum size from whatever rule matched it, so a
        button pinned to 36 comes back with a minimum of 32 and the layout then
        lays it out at its 32 px hint (verified). Restating the box in a rule of
        the button's own is what the style will honour — and, like
        `FolderTree.indicator_qss`, it is a widget stylesheet, merged with the
        application sheet rather than replacing it.

        QSS `min-height` sizes the CONTENT box, so the padding and the border
        come off first — the same arithmetic `qss.ICON_BUTTON_CONTENT` does.
        """
        content_w = NAV_TOGGLE[0] - 2 * METRICS["button_pad_h"] - 2
        content_h = NAV_TOGGLE[1] - 2 * METRICS["button_pad_v"] - 2
        return (
            f"{qss.SEL.BUTTON} {{\n"
            f"  min-width: {content_w}px; max-width: {content_w}px;\n"
            f"  min-height: {content_h}px; max-height: {content_h}px;\n"
            f"}}\n"
        )

    # ── items ────────────────────────────────────────────────────────────
    def add_item(self, text: str, icon_key: str, *, key: str = "") -> int:
        """Append a destination and return its row index.

        Args:
            text: The label. From `strings.py`.
            icon_key: A key of `icons.GLYPHS`.
            key: A stable identifier for deep links; defaults to `icon_key`.

        Returns:
            The new row's index.

        Raises:
            KeyError: for an icon key that is not in `icons.GLYPHS`.
        """
        icons.glyph_stem(icon_key)
        item = QListWidgetItem(text)
        item.setData(NAV_ICON_ROLE, icon_key)
        item.setData(NAV_KEY_ROLE, key or icon_key)
        item.setToolTip(text)               # the only affordance when compact
        self._list.addItem(item)
        if self._list.currentRow() < 0:
            self._list.setCurrentRow(0)
        return self._list.row(item)

    def count(self) -> int:
        return self._list.count()

    def item_text(self, index: int) -> str:
        item = self._list.item(index)
        return item.text() if item is not None else ""

    def item_key(self, index: int) -> str:
        item = self._list.item(index)
        return str(item.data(NAV_KEY_ROLE) or "") if item is not None else ""

    def item_icon_key(self, index: int) -> str:
        item = self._list.item(index)
        return str(item.data(NAV_ICON_ROLE) or "") if item is not None else ""

    def index_of(self, key: str) -> int:
        """The row carrying `key`, or -1."""
        for row in range(self._list.count()):
            if self.item_key(row) == key:
                return row
        return -1

    def clear(self) -> None:
        """Remove every destination."""
        self._list.clear()

    # ── selection ────────────────────────────────────────────────────────
    def current_index(self) -> int:
        return self._list.currentRow()

    def set_current_index(self, index: int) -> None:
        """Select a row. Emits :attr:`current_changed` and :attr:`navigated`."""
        self._list.setCurrentRow(int(index))

    def current_key(self) -> str:
        return self.item_key(self._list.currentRow())

    def select_key(self, key: str) -> bool:
        """Select the destination carrying `key`.

        Returns:
            True if the key was found.
        """
        row = self.index_of(key)
        if row < 0:
            return False
        self._list.setCurrentRow(row)
        return True

    def _on_row_changed(self, row: int) -> None:
        self.current_changed.emit(row)
        self.navigated.emit(self.item_key(row))

    # ── compacting ───────────────────────────────────────────────────────
    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        """Switch between the 320 px pane and the 48 px icon-only rail."""
        compact = bool(compact)
        if compact == self._compact:
            return
        self._compact = compact
        self._delegate.set_compact(compact)
        self._apply_width()
        qss.set_property(self, PROP.COMPACT, compact)
        self.toggled_compact.emit(compact)

    def pane_width(self) -> int:
        """The rail's current width: 320 open, 48 compact."""
        return NAV_COMPACT_W if self._compact else NAV_OPEN_W

    def _apply_width(self) -> None:
        self.setFixedWidth(self.pane_width())

    def _on_toggle_clicked(self) -> None:
        self.set_compact(not self._compact)

    def toggle_button(self):
        """The pane-toggle button, hidden until :meth:`set_toggle_visible`."""
        return self._toggle

    def set_toggle_visible(self, visible: bool) -> None:
        """Show the 40 x 36 hamburger above the rail."""
        self._toggle.setVisible(bool(visible))

    # ── plumbing ─────────────────────────────────────────────────────────
    def list_widget(self) -> QListWidget:
        """The underlying rail, for a caller that needs the raw view."""
        return self._list

    def delegate(self) -> _NavDelegate:
        return self._delegate

    def refresh_theme(self) -> None:
        self._delegate.refresh_theme()
        self._toggle.refresh_theme()
        qss.repolish(self, deep=True)
        self.update()


# ═════════════════════════════════════════════════════════════════════════════
# SearchBox
# ═════════════════════════════════════════════════════════════════════════════

class SearchBox(FluentLineEdit):
    """A Fluent search field: leading glyph, inline clear button, debounce.

    The leading 16 px glyph and the extra left padding come from
    :class:`FluentLineEdit`'s search mode and the frozen
    `FluentLineEdit#SearchBox` rule — a QSS type selector matches a subclass, so
    this class inherits the whole box recipe unchanged.

    `search_changed` fires only once the user has stopped typing for
    `SEARCH_DEBOUNCE_MS`; `textChanged` is still there for a caller that wants
    every keystroke.
    """

    #: Emitted with the settled query text.
    search_changed = Signal(str)
    #: Emitted when the inline clear button empties the field.
    cleared = Signal()

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 placeholder: str = "",
                 clear_tooltip: str = "",
                 debounce_ms: int = SEARCH_DEBOUNCE_MS) -> None:
        """
        Args:
            placeholder: The empty-state text. From `strings.py`.
            clear_tooltip: The clear button's tooltip. From `strings.py`.
            debounce_ms: How long the text must be settled before
                :attr:`search_changed` fires.
        """
        super().__init__(parent, placeholder=placeholder, search=True,
                         clear_button=False)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(debounce_ms))
        self._timer.timeout.connect(self._emit_search)

        self._clear_action = QAction(self)
        self._clear_action.setToolTip(clear_tooltip)
        self._clear_action.triggered.connect(self._on_clear)
        self.addAction(self._clear_action, SEARCH_CLEAR_POSITION)
        self._clear_action.setVisible(False)
        self._retint_clear()

        self.textChanged.connect(self._on_text_changed)

    # ── behaviour ────────────────────────────────────────────────────────
    def debounce_ms(self) -> int:
        return self._timer.interval()

    def set_debounce_ms(self, ms: int) -> None:
        """Change the settle time. A debounce is not motion; it is never gated."""
        self._timer.setInterval(int(ms))

    def clear_action(self) -> QAction:
        """The inline clear button's action, for a test or a shortcut."""
        return self._clear_action

    def _on_text_changed(self, text: str) -> None:
        self._clear_action.setVisible(bool(text))
        self._timer.start()

    def _emit_search(self) -> None:
        self.search_changed.emit(self.text())

    def _on_clear(self) -> None:
        had_text = bool(self.text())
        self.clear()
        self._timer.stop()
        self.search_changed.emit("")
        if had_text:
            self.cleared.emit()

    def flush(self) -> None:
        """Emit :attr:`search_changed` now instead of waiting for the debounce."""
        self._timer.stop()
        self._emit_search()

    # ── theme ────────────────────────────────────────────────────────────
    def _retint_clear(self) -> None:
        colour = theme.T(_TOKEN_TEXT2 if self.isEnabled() else _TOKEN_TEXT_OFF)
        self._clear_action.setIcon(
            icons.icon(_GLYPH_CLEAR, SEARCH_CLEAR_GLYPH, color=colour))

    def refresh_theme(self) -> None:
        self._retint_clear()
        qss.repolish(self)
        self.update()


# ═════════════════════════════════════════════════════════════════════════════
# StatusGlyph
# ═════════════════════════════════════════════════════════════════════════════

class StatusGlyph(ThemeAware, QWidget):
    """A sync state as one tinted glyph, spinning while the state is busy.

    The state -> semantic mapping is `strings.TRAY_FOR_STATE`, the same table
    the tray reads, so the status strip and the panel icon can never disagree
    about what a state means. The rotation shares the tray spinner's period for
    the same reason.

    `SyncState.NOT_RUNNING` maps to `TrayIcon.NONE`, which registers no tray
    item and correspondingly paints nothing here.
    """

    #: The default glyph size: the 16 px status-strip icon.
    SIZE: int = SPACING["l"]

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 state: SyncState = SyncState.NOT_RUNNING,
                 size: int = SIZE) -> None:
        """
        Args:
            state: The sync state to show.
            size: Logical glyph size in px.
        """
        super().__init__(parent)
        self._state = SyncState(state)
        self._size = int(size)
        self._angle = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(self._size, self._size)
        self._loop = motion.SafeLoop(
            self, self._set_angle, start=0.0, end=FULL_TURN_DEG,
            duration=SPIN_PERIOD_MS, parent=self,
        )
        self._install_theme_hook()
        self._apply_state()

    # ── state ────────────────────────────────────────────────────────────
    def state(self) -> SyncState:
        return self._state

    def set_state(self, state: SyncState) -> None:
        """Show a different sync state; starts or stops the spin to match."""
        self._state = SyncState(state)
        self._apply_state()

    def tray(self) -> TrayIcon:
        """The tray semantic the current state maps to."""
        return tray_for(self._state)

    def glyph_key(self) -> str:
        """The glyph currently drawn. "" means the state draws nothing."""
        return glyph_for_state(self._state)

    def colour(self) -> str:
        """The opaque `#RRGGBB` the glyph is drawn in."""
        if not self.isEnabled():
            return theme.T(_TOKEN_TEXT_OFF)
        return tone_for_state(self._state)

    def is_spinning(self) -> bool:
        """True while the busy states rotate the glyph."""
        return self._loop.wanted()

    def angle(self) -> float:
        """The current rotation in degrees, for tests."""
        return self._angle

    def glyph_size(self) -> int:
        return self._size

    def set_glyph_size(self, size: int) -> None:
        """Resize the glyph; snapped onto the native ladder when it is drawn."""
        self._size = int(size)
        self.setFixedSize(self._size, self._size)
        self.update()

    def _apply_state(self) -> None:
        if self._state in BUSY_STATES:
            self._loop.start()
        else:
            self._loop.stop()
            self._angle = 0.0
        self.update()

    def _set_angle(self, value: float) -> None:
        self._angle = float(value)
        self.update()

    # ── painting ─────────────────────────────────────────────────────────
    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def showEvent(self, event) -> None:
        """Resume the spin when the glyph comes back into view."""
        super().showEvent(event)
        if self._state in BUSY_STATES:
            self._loop.start()

    def hideEvent(self, event) -> None:
        """A hidden widget that keeps rotating burns CPU for nothing."""
        self._loop.suspend()
        super().hideEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        key = self.glyph_key()
        if not key:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        pixmap = glyph_pixmap(key, self._size, self.colour(),
                              self.devicePixelRatioF())
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        if self._angle:
            painter.rotate(self._angle)
        painter.drawPixmap(-self._size // 2, -self._size // 2, pixmap)
        painter.end()

    def refresh_theme(self) -> None:
        self.update()


__all__ = [
    "NAV_OPEN_W", "NAV_COMPACT_W", "NAV_ITEM_H", "NAV_ITEM_MARGIN",
    "NAV_ICON_BOX", "NAV_GLYPH", "NAV_INDICATOR_W", "NAV_INDICATOR_H",
    "NAV_TOGGLE", "NAV_LABEL_GAP", "NAV_PANE_PAD", "NAV_ICON_ROLE",
    "NAV_KEY_ROLE", "NAV_ROLE",
    "SEARCH_CLEAR_GLYPH", "SEARCH_DEBOUNCE_MS", "SEARCH_CLEAR_POSITION",
    "GLYPH_FOR_TRAY", "TONE_FOR_TRAY", "SPIN_PERIOD_MS", "FULL_TURN_DEG",
    "tray_for", "glyph_for_state", "tone_for_state",
    "NavigationView", "SearchBox", "StatusGlyph",
]
