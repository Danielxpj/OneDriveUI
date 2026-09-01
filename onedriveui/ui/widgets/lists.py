"""The activity list and the tri-state folder tree.

Two verified traps shape this module.

  * **A full-width delegate must return width 0 from `sizeHint`.** Returning
    `option.rect.width()` closes a feedback loop with the vertical scrollbar —
    the view asks the delegate how wide a row is, the delegate answers "as wide
    as the viewport", the vertical scrollbar then narrows the viewport, and the
    row no longer fits, so a horizontal scrollbar appears::

        sizeHint -> QSize(option.rect.width(), 56)  : hscroll max=14 visible=True
        sizeHint -> QSize(0, 56)                    : hscroll max=0  visible=False

    `QListView` uses the viewport width for a zero-width hint, and `option.rect`
    inside `paint()` is already the correct full-row rectangle either way.
  * **`Qt.ItemIsAutoTristate` only rolls state UP.** Qt recomputes a parent from
    its children, but never pushes a parent's new state down, so selective sync
    needs the other direction written by hand — and `setCheckState()` re-emits
    `itemChanged`, so the propagation must be behind a re-entrancy guard or the
    first click recurses through the whole tree.

Nothing here formats a number or a date. A row is handed the strings it should
draw — `units.relative_time()`, `units.human_rate()`, `units.eta_text()` and
`strings.issue_title()` all live outside the widget kit, which is what lets the
kit render in a gallery with no engine at all. A raw rclone error is **never**
drawn: `ActivityRow.error_text` is user-worded copy the caller resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterator

from PySide6.QtCore import (
    QModelIndex, QPersistentModelIndex, QRectF, QSize, Qt, Signal,
)
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QListView, QStyle, QStyleOptionViewItem,
    QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QWidget,
)

from onedriveui.bus import BUS
from onedriveui.models import (
    ActivityEvent, ActivityState, ActivityVerb, FileState,
)
from onedriveui.strings import FILE_STATE_LABEL, VERB_LABEL, issue_title
from onedriveui.ui import fonts, icons, motion, theme
from onedriveui.ui.theme import METRICS, OBJ, RADII, SPACING, Surface
from onedriveui.ui.widgets.containers import box_path, glyph_pixmap

# ═════════════════════════════════════════════════════════════════════════════
# Row geometry — the 56 px two-line activity row of the Activity Center.
# ═════════════════════════════════════════════════════════════════════════════

#: Two-line and single-line row heights.
ROW_H: int = METRICS["ac_row_h_2line"]          # 56
ROW_H_COMPACT: int = METRICS["ac_row_h_1line"]  # 48
#: The flyout's horizontal content inset.
ROW_INSET: int = METRICS["ac_inset"]            # 16
#: The file-type icon and the gap after it.
ROW_ICON: int = SPACING["xxxl"]                 # 32
ROW_ICON_GAP: int = SPACING["m"]                # 12
#: The trailing status glyph.
ROW_STATUS: int = SPACING["xl"]                 # 20
#: The hover pill is inset 4 px from each edge, so a 360 px row pills 4 … 356.
ROW_PILL_INSET: int = SPACING["xs"]             # 4
#: The selection bar: the same 3 x 16 r2 indicator the nav pane uses.
SELECTION_BAR_W: int = METRICS["nav_indicator_w"]
SELECTION_BAR_H: int = METRICS["nav_indicator_h"]
SELECTION_BAR_R: int = RADII["selection_indicator"]
#: Gaps inside the text column.
LINE_GAP: int = SPACING["xxs"]                  # 2, primary -> secondary
BAR_GAP: int = SPACING["xs"]                    # 4, secondary -> progress bar
#: The inline progress bar reuses the Fluent bar's own geometry: a 3 px fill
#: over a 1 px track, so a row and a `FluentProgressBar` cannot drift apart.
BAR_FILL_H: int = METRICS["progress_fill_h"]
BAR_TRACK_H: int = METRICS["progress_track_h"]
#: The error chip: a caption-height pill with the xs padding step.
CHIP_PAD: int = SPACING["xs"]
CHIP_GAP: int = SPACING["s"]
#: The middot that joins "Uploaded", "2 minutes ago", "1.2 MB/s" and "12s left".
#: Punctuation, not copy — every word in that line is passed in by the caller.
SUBTITLE_SEPARATOR: str = " · "

#: The type-ramp roles a row draws with.
ROLE_PRIMARY: str = "body"
ROLE_SECONDARY: str = "caption"

#: A row's whole payload. `QStandardItemModel`-based callers set this one role.
ROW_ROLE: int = int(Qt.ItemDataRole.UserRole) + 1
#: A `TriStateItem`'s remote-relative path, stored as item data so it survives a
#: round trip through Qt even when the Python wrapper does not.
PATH_ROLE: int = int(Qt.ItemDataRole.UserRole) + 2


# ═════════════════════════════════════════════════════════════════════════════
# Semantic tables. Every value is a `theme` token name or an `icons.GLYPHS` key.
# ═════════════════════════════════════════════════════════════════════════════

#: file state -> the token its trailing glyph is tinted with. "" is the live
#: accent, which is what "in flight" reads as everywhere else in the app.
STATUS_TOKEN: dict[FileState, str] = {
    FileState.ONLINE_ONLY: "TextFillColorSecondary",
    FileState.PARTIAL:     "",
    FileState.LOCAL:       "SystemFillColorSuccess",
    FileState.PINNED:      "SystemFillColorSuccess",
    FileState.DIRTY:       "",
    FileState.SYNCING:     "",
    FileState.EXCLUDED:    "SystemFillColorSolidNeutral",
    FileState.ERROR:       "SystemFillColorCritical",
    FileState.UNKNOWN:     "TextFillColorTertiary",
}

#: activity state -> the per-file state its trailing glyph shows. An
#: INTERRUPTED row genuinely does not know its outcome, which is what UNKNOWN
#: means — it must not claim success.
FILE_STATE_FOR_ACTIVITY: dict[ActivityState, FileState] = {
    ActivityState.INFLIGHT:    FileState.SYNCING,
    ActivityState.DONE:        FileState.LOCAL,
    ActivityState.ERROR:       FileState.ERROR,
    ActivityState.CANCELLED:   FileState.UNKNOWN,
    ActivityState.INTERRUPTED: FileState.UNKNOWN,
}

#: `TreeViewItem` minimum height, and the 20 px check indicator inside it.
TREE_ROW_H: int = METRICS["button_h"]           # 32
TREE_INDICATOR: int = SPACING["xl"]             # 20
CHECK_GLYPH: int = SPACING["m"]                 # 12
#: The expand chevron, at WinUI's `Expander` glyph size.
CHEVRON_GLYPH: int = SPACING["m"]               # 12

_GLYPH_FOLDER = "folder"
_GLYPH_FILE = "file"
_GLYPH_CHECK = "checkmark"
_GLYPH_BRANCH_OPEN = "chevron_down"
_GLYPH_BRANCH_SHUT = "chevron_right"
for _key in (_GLYPH_FOLDER, _GLYPH_FILE, _GLYPH_CHECK,
             _GLYPH_BRANCH_OPEN, _GLYPH_BRANCH_SHUT):
    icons.glyph_stem(_key)                  # raises KeyError on an unknown key
del _key

_missing = [state.name for state in FileState if state not in STATUS_TOKEN]
if _missing:                                            # pragma: no cover
    raise ValueError(f"lists: STATUS_TOKEN is missing {_missing}")
_missing = [state.name for state in ActivityState if state not in FILE_STATE_FOR_ACTIVITY]
if _missing:                                            # pragma: no cover
    raise ValueError(f"lists: FILE_STATE_FOR_ACTIVITY is missing {_missing}")
_bad = [token for token in STATUS_TOKEN.values() if token and token not in theme.TOKENS]
if _bad:                                                # pragma: no cover
    raise ValueError(f"lists: STATUS_TOKEN names unknown theme tokens {_bad}")
del _missing, _bad

_TOKEN_TEXT = "TextFillColorPrimary"
_TOKEN_TEXT2 = "TextFillColorSecondary"
_TOKEN_TEXT_OFF = "TextFillColorDisabled"
_TOKEN_HOVER = "SubtleFillColorSecondary"
_TOKEN_PRESSED = "SubtleFillColorTertiary"
_TOKEN_TRACK = "ControlStrongStrokeColorDefault"
_TOKEN_CRITICAL = "SystemFillColorCritical"
_TOKEN_CRITICAL_BG = "SystemFillColorCriticalBackground"


# ═════════════════════════════════════════════════════════════════════════════
# The row payload
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class ActivityRow:
    """Everything one activity row draws, already worded and already formatted.

    The widget kit never formats a byte count, a rate, a duration or a
    timestamp: those live in `units`, which the kit deliberately cannot import
    so that the gallery renders with no engine present. A caller builds a row
    with :meth:`from_event` and passes the three formatted strings in.

    Attributes:
        name: The file name, middle-elided so the extension survives.
        verb: What happened, drawn through `strings.VERB_LABEL`.
        state: The activity's own state; ERROR draws the chip.
        file_state: Picks the trailing glyph and its tint.
        icon_key: A key of `icons.GLYPHS` for the 32 px leading icon. "" falls
            back to the folder or document glyph.
        is_dir: Chooses that fallback.
        time_text: e.g. `units.relative_time(event.completed_at)`.
        rate_text: e.g. `units.human_rate(bytes_per_second)`.
        eta_text: e.g. `units.eta_text(seconds)`.
        error_text: User-worded copy from `strings`. **Never** a raw error.
        progress: 0.0 … 1.0 for a determinate bar; negative for no bar at all.
        subtitle: Overrides the composed second line outright.
    """

    name: str = ""
    verb: ActivityVerb = ActivityVerb.MODIFIED
    state: ActivityState = ActivityState.DONE
    file_state: FileState = FileState.UNKNOWN
    icon_key: str = ""
    is_dir: bool = False
    time_text: str = ""
    rate_text: str = ""
    eta_text: str = ""
    error_text: str = ""
    progress: float = -1.0
    subtitle: str = ""

    # ── derived ──────────────────────────────────────────────────────────
    def glyph_key(self) -> str:
        """The leading icon's key of `icons.GLYPHS`."""
        if self.icon_key:
            return self.icon_key
        return _GLYPH_FOLDER if self.is_dir else _GLYPH_FILE

    def second_line(self) -> str:
        """The composed caption line, or :attr:`subtitle` when one was given."""
        if self.subtitle:
            return self.subtitle
        parts = [text for text in (VERB_LABEL.get(str(self.verb), ""),
                                   self.time_text, self.rate_text, self.eta_text)
                 if text]
        return SUBTITLE_SEPARATOR.join(parts)

    def has_progress(self) -> bool:
        """True when the row draws an inline progress bar."""
        return self.progress >= 0.0

    def has_error(self) -> bool:
        """True when the row draws its error chip."""
        return bool(self.error_text)

    def with_progress(self, progress: float) -> "ActivityRow":
        """A copy with a new progress fraction.

        Live progress is applied by writing one row back into the model —
        `model.setData(index, row.with_progress(v), ROW_ROLE)` emits
        `dataChanged` for that single index and repaints exactly one row. Never
        rebuild the model on a stats tick.
        """
        return replace(self, progress=float(progress))

    # ── construction ─────────────────────────────────────────────────────
    @classmethod
    def from_event(cls,
                   event: ActivityEvent,
                   *,
                   time_text: str = "",
                   rate_text: str = "",
                   eta_text: str = "",
                   error_text: str = "",
                   icon_key: str = "",
                   file_state: FileState | None = None) -> "ActivityRow":
        """Build a row from a WP-00 `ActivityEvent`.

        Args:
            event: The stored activity record.
            time_text: The relative timestamp, formatted by the caller.
            rate_text: The transfer rate, formatted by the caller.
            eta_text: The remaining time, formatted by the caller.
            error_text: User-worded failure copy. Defaults to
                `strings.issue_title(event.error_kind)`, then to the generic
                "sync problem" label — never to `event.error`, which is raw
                rclone output and must not reach a user.
            icon_key: Override the leading glyph.
            file_state: Override the trailing status glyph's state.

        Returns:
            A fully populated :class:`ActivityRow`.
        """
        name = event.name or event.rel_path.rsplit("/", 1)[-1]
        if not error_text and event.state is ActivityState.ERROR:
            if event.error_kind is not None:
                error_text = issue_title(event.error_kind)
            else:
                error_text = FILE_STATE_LABEL[str(FileState.ERROR)]
        progress = -1.0
        if event.state is ActivityState.INFLIGHT:
            progress = (event.bytes / event.size) if event.size > 0 else 0.0
        resolved = (file_state if file_state is not None
                    else FILE_STATE_FOR_ACTIVITY[event.state])
        return cls(
            name=name, verb=event.verb, state=event.state, file_state=resolved,
            icon_key=icon_key, is_dir=event.is_dir, time_text=time_text,
            rate_text=rate_text, eta_text=eta_text, error_text=error_text,
            progress=progress,
        )


def status_colour(state: FileState, *, surface: Surface = "layer") -> str:
    """The trailing glyph's tint for a file state, as an opaque `#RRGGBB`."""
    token = STATUS_TOKEN[FileState(state)]
    return theme.T(token, on=surface) if token else theme.accent()


# ═════════════════════════════════════════════════════════════════════════════
# ActivityDelegate
# ═════════════════════════════════════════════════════════════════════════════

class ActivityDelegate(QStyledItemDelegate):
    """Paints one activity row: icon, name, verb, time, progress, status.

    ``sizeHint`` returns **width 0**. That is not a shortcut — it is the fix for
    the vertical/horizontal scrollbar feedback loop described in this module's
    docstring, and `ActivityListView` keeps `setUniformItemSizes(True)` on top
    of it, which lets the view skip the per-row hint entirely.

    Fonts, metrics and glyph pixmaps are resolved once and cached, because this
    method runs for every visible row on every scroll frame; the cache is
    dropped on `BUS.theme_changed`.
    """

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 compact: bool = False,
                 surface: Surface = "layer") -> None:
        """
        Args:
            parent: Usually the view, so a theme change can repaint it.
            compact: Draw the 48 px single-line row instead of the 56 px one.
            surface: Which surface the row sits on — "layer" for the Activity
                Center flyout, "base" for a page in the settings window. It
                selects the pre-composited hover and text tokens.
        """
        super().__init__(parent)
        self._compact = bool(compact)
        self._surface: Surface = surface
        self._pixmaps: dict[tuple[str, int, str], QPixmap] = {}
        self._fm_primary = QFontMetricsF(fonts.font(ROLE_PRIMARY))
        self._fm_secondary = QFontMetricsF(fonts.font(ROLE_SECONDARY))
        BUS.theme_changed.connect(self._on_theme_changed)

    # ── configuration ────────────────────────────────────────────────────
    def is_compact(self) -> bool:
        return self._compact

    def set_compact(self, compact: bool) -> None:
        """Switch between the 56 px two-line row and the 48 px one-line row."""
        self._compact = bool(compact)
        self._repaint_view()

    def surface(self) -> Surface:
        return self._surface

    def set_surface(self, surface: Surface) -> None:
        """Pick which pre-composited token column the row resolves against."""
        if surface not in ("base", "layer"):
            raise ValueError(
                f"ActivityDelegate.set_surface: expected 'base' or 'layer', "
                f"not {surface!r}"
            )
        self._surface = surface
        self._pixmaps.clear()
        self._repaint_view()

    def row_height(self) -> int:
        """The constant row height this delegate reports."""
        return ROW_H_COMPACT if self._compact else ROW_H

    def _on_theme_changed(self, _dark: bool, _accent: str) -> None:
        self.refresh_theme()

    def refresh_theme(self) -> None:
        """Drop every cached font, metric and pixmap, then repaint the view."""
        self._pixmaps.clear()
        self._fm_primary = QFontMetricsF(fonts.font(ROLE_PRIMARY))
        self._fm_secondary = QFontMetricsF(fonts.font(ROLE_SECONDARY))
        self._repaint_view()

    def _repaint_view(self) -> None:
        parent = self.parent()
        if isinstance(parent, QAbstractItemView):
            parent.viewport().update()

    # ── data ─────────────────────────────────────────────────────────────
    def row_for(self, index: QModelIndex | QPersistentModelIndex) -> ActivityRow:
        """The row payload for an index.

        Reads `ROW_ROLE` and falls back to `Qt.DisplayRole`, so a plain string
        model still renders as a list of names rather than as blank rows.
        """
        payload = index.data(ROW_ROLE)
        if isinstance(payload, ActivityRow):
            return payload
        return ActivityRow(name=str(index.data(Qt.ItemDataRole.DisplayRole) or ""))

    def _glyph(self, key: str, size: int, colour: str, dpr: float) -> QPixmap:
        cache_key = (key, size, colour)
        pixmap = self._pixmaps.get(cache_key)
        if pixmap is None:
            pixmap = glyph_pixmap(key, size, colour, dpr)
            self._pixmaps[cache_key] = pixmap
        return pixmap

    # ── measurement ──────────────────────────────────────────────────────
    def sizeHint(self,
                 option: QStyleOptionViewItem,
                 index: QModelIndex | QPersistentModelIndex) -> QSize:
        """**Width 0.** See the module docstring: any other width grows a
        phantom horizontal scrollbar on a full-width list."""
        return QSize(0, self.row_height())

    def text_column(self, rect: QRectF) -> tuple[float, float]:
        """-> (x, width) of the text column inside a row rectangle."""
        left = rect.left() + ROW_INSET + ROW_ICON + ROW_ICON_GAP
        right = rect.right() - ROW_INSET - ROW_STATUS - ROW_ICON_GAP
        return left, max(0.0, right - left)

    # ── painting ─────────────────────────────────────────────────────────
    def paint(self,
              painter: QPainter,
              option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        """Draw the whole row. `QStyledItemDelegate.paint` is never called.

        Skipping the base implementation is what keeps the frozen
        `#ActivityList::item` hover rule from painting a full-width rectangle
        underneath the 4 px-inset Fluent hover pill: a QSS `::item` background
        is drawn by `CE_ItemViewItem`, which only runs if the delegate asks for
        it.
        """
        row = self.row_for(index)
        rect = QRectF(option.rect)
        state = option.state
        enabled = bool(state & QStyle.StateFlag.State_Enabled)
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        self._paint_background(painter, rect, selected=selected, hovered=hovered)
        self._paint_icon(painter, rect, row, dpr, enabled=enabled)

        left, width = self.text_column(rect)
        if width > 0.0:
            self._paint_text(painter, rect, row, left, width, enabled=enabled)
        self._paint_status(painter, rect, row, dpr)
        painter.restore()

    def _paint_background(self, painter: QPainter, rect: QRectF, *,
                          selected: bool, hovered: bool) -> None:
        """The 4 px-inset hover pill and the 3 x 16 accent selection bar."""
        if not (selected or hovered):
            return
        pill = rect.adjusted(ROW_PILL_INSET, 0.0, -ROW_PILL_INSET, 0.0)
        token = _TOKEN_PRESSED if selected else _TOKEN_HOVER
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.T(token, on=self._surface)))
        painter.drawRoundedRect(pill, RADII["hover_pill"], RADII["hover_pill"])
        if selected:
            bar = QRectF(pill.left() + 1.0,
                         pill.center().y() - SELECTION_BAR_H / 2.0,
                         float(SELECTION_BAR_W), float(SELECTION_BAR_H))
            painter.setBrush(QColor(theme.accent()))
            painter.drawRoundedRect(bar, SELECTION_BAR_R, SELECTION_BAR_R)

    def _paint_icon(self, painter: QPainter, rect: QRectF, row: ActivityRow,
                    dpr: float, *, enabled: bool) -> None:
        colour = theme.T(_TOKEN_TEXT if enabled else _TOKEN_TEXT_OFF,
                         on=self._surface)
        pixmap = self._glyph(row.glyph_key(), ROW_ICON, colour, dpr)
        painter.drawPixmap(
            int(rect.left() + ROW_INSET),
            int(rect.center().y() - ROW_ICON / 2.0),
            pixmap,
        )

    def _paint_text(self, painter: QPainter, rect: QRectF, row: ActivityRow,
                    left: float, width: float, *, enabled: bool) -> None:
        primary_h = float(fonts.line_height(ROLE_PRIMARY))
        secondary_h = float(fonts.line_height(ROLE_SECONDARY))
        block = primary_h
        if not self._compact:
            block += LINE_GAP + secondary_h
            if row.has_progress():
                block += BAR_GAP + BAR_FILL_H
        top = rect.top() + (rect.height() - block) / 2.0

        painter.setFont(fonts.font(ROLE_PRIMARY))
        painter.setPen(QColor(theme.T(_TOKEN_TEXT if enabled else _TOKEN_TEXT_OFF,
                                      on=self._surface)))
        # File names elide in the MIDDLE so the extension stays readable.
        painter.drawText(
            QRectF(left, top, width, primary_h),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self._fm_primary.elidedText(row.name, Qt.TextElideMode.ElideMiddle,
                                        int(width)),
        )
        if self._compact:
            return

        y = top + primary_h + LINE_GAP
        chip_w = 0.0
        if row.has_error():
            chip_w = min(width, self._chip_width(row.error_text))
            self._paint_chip(painter, QRectF(left + width - chip_w, y, chip_w,
                                             secondary_h), row.error_text)
            chip_w += CHIP_GAP
        text_w = max(0.0, width - chip_w)
        if text_w > 0.0:
            painter.setFont(fonts.font(ROLE_SECONDARY))
            painter.setPen(QColor(theme.T(_TOKEN_TEXT2 if enabled else _TOKEN_TEXT_OFF,
                                          on=self._surface)))
            painter.drawText(
                QRectF(left, y, text_w, secondary_h),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._fm_secondary.elidedText(
                    row.second_line(), Qt.TextElideMode.ElideRight, int(text_w)),
            )
        if row.has_progress():
            self._paint_progress(painter, row,
                                 left, y + secondary_h + BAR_GAP, width)

    def _chip_width(self, text: str) -> float:
        return self._fm_secondary.horizontalAdvance(text) + 2.0 * CHIP_PAD

    def _paint_chip(self, painter: QPainter, rect: QRectF, text: str) -> None:
        """The error chip: critical text on the critical background, 4 px round."""
        painter.setFont(fonts.font(ROLE_SECONDARY))
        radius = float(RADII["control"])
        path = box_path(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                        radius, radius, radius, radius)
        painter.setPen(QPen(QColor(theme.T(_TOKEN_CRITICAL, on=self._surface)), 1.0))
        painter.setBrush(QColor(theme.T(_TOKEN_CRITICAL_BG, on=self._surface)))
        painter.drawPath(path)
        painter.setPen(QColor(theme.T(_TOKEN_CRITICAL, on=self._surface)))
        painter.drawText(
            rect,
            int(Qt.AlignmentFlag.AlignCenter),
            self._fm_secondary.elidedText(text, Qt.TextElideMode.ElideRight,
                                          int(rect.width() - 2 * CHIP_PAD)),
        )

    def _paint_progress(self, painter: QPainter, row: ActivityRow,
                        left: float, top: float, width: float) -> None:
        """The inline bar: a 3 px fill over a 1 px track, exactly as Fluent's."""
        painter.setPen(Qt.PenStyle.NoPen)
        track_top = top + (BAR_FILL_H - BAR_TRACK_H) / 2.0
        painter.setBrush(QColor(theme.T(_TOKEN_TRACK, on=self._surface)))
        painter.drawRoundedRect(
            QRectF(left, track_top, width, float(BAR_TRACK_H)),
            RADII["progress_track"], RADII["progress_track"])
        fraction = 0.0 if row.progress < 0.0 else min(1.0, row.progress)
        if fraction <= 0.0:
            return
        colour = (theme.T(_TOKEN_CRITICAL, on=self._surface)
                  if row.state is ActivityState.ERROR else theme.accent())
        painter.setBrush(QColor(colour))
        painter.drawRoundedRect(
            QRectF(left, top, width * fraction, float(BAR_FILL_H)),
            RADII["progress_fill"], RADII["progress_fill"])

    def _paint_status(self, painter: QPainter, rect: QRectF, row: ActivityRow,
                      dpr: float) -> None:
        key = icons.GLYPH_FOR_FILE_STATE[row.file_state]
        pixmap = self._glyph(key, ROW_STATUS,
                             status_colour(row.file_state, surface=self._surface),
                             dpr)
        painter.drawPixmap(
            int(rect.right() - ROW_INSET - ROW_STATUS),
            int(rect.center().y() - ROW_STATUS / 2.0),
            pixmap,
        )


# ═════════════════════════════════════════════════════════════════════════════
# ActivityListView
# ═════════════════════════════════════════════════════════════════════════════

class ActivityListView(QListView):
    """A `QListView` wired the way the activity delegate needs it.

    `setMouseTracking(True)` is not optional — without it the view never sets
    `State_MouseOver` and the delegate's hover pill can never appear.
    `setUniformItemSizes(True)` lets the view skip the per-row `sizeHint`, which
    is the single largest win when the model holds thousands of rows.
    """

    #: Emitted with the index a row was activated on (double-click or Return).
    row_activated = Signal(object)

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 compact: bool = False,
                 surface: Surface = "layer") -> None:
        """
        Args:
            compact: Use the 48 px single-line row.
            surface: "layer" inside the Activity Center flyout, "base" on a page.
        """
        super().__init__(parent)
        self.setObjectName(OBJ.ACTIVITY_LIST)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self._delegate = ActivityDelegate(self, compact=compact, surface=surface)
        self.setItemDelegate(self._delegate)
        self.activated.connect(self.row_activated.emit)

    def delegate(self) -> ActivityDelegate:
        """The row delegate, for a caller that wants to reconfigure it."""
        return self._delegate

    def set_compact(self, compact: bool) -> None:
        """Switch the whole list between the 56 px and 48 px row."""
        self._delegate.set_compact(compact)
        self.scheduleDelayedItemsLayout()

    def refresh_theme(self) -> None:
        self._delegate.refresh_theme()
        self.viewport().update()


# ═════════════════════════════════════════════════════════════════════════════
# The tri-state folder tree
# ═════════════════════════════════════════════════════════════════════════════

class TriStateItem(QTreeWidgetItem):
    """One folder in the selective-sync tree.

    The remote-relative path is kept in item **data**, not on the Python object,
    so `FolderTree` can read it back off whatever wrapper Qt hands it.
    """

    def __init__(self,
                 text: str = "",
                 *,
                 rel_path: str = "",
                 size_text: str = "") -> None:
        """
        Args:
            text: The folder's display name.
            rel_path: Its path relative to the sync root, used to build filters.
            size_text: An optional right-hand column, formatted by the caller
                (`units.human_bytes()` lives outside the widget kit).
        """
        super().__init__([text, size_text])
        # ItemIsUserCheckable but NOT ItemIsUserTristate: the user may tick and
        # untick a folder, never set "partially" by hand — a partial state is
        # always a statement about the children. ItemIsAutoTristate is left off
        # too; see `FolderTree` for the measurement that made that the right
        # call.
        self.setFlags(self.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        self.setData(0, PATH_ROLE, rel_path)
        self.setCheckState(0, Qt.CheckState.Unchecked)

    def rel_path(self) -> str:
        """The path this folder maps to, relative to the sync root."""
        return str(self.data(0, PATH_ROLE) or "")

    def set_rel_path(self, rel_path: str) -> None:
        self.setData(0, PATH_ROLE, rel_path)

    def size_text(self) -> str:
        return self.text(1)

    def set_size_text(self, text: str) -> None:
        """Set the right-hand size column. The caller formats the number."""
        self.setText(1, text)

    def state(self) -> Qt.CheckState:
        """This folder's check state."""
        return self.checkState(0)


def rel_path_of(item: QTreeWidgetItem) -> str:
    """The stored path of any tree item, `TriStateItem` or not."""
    return str(item.data(0, PATH_ROLE) or "")


class _CheckGlyphDelegate(QStyledItemDelegate):
    """Overlays the Fluent checkmark on a tree's own check indicator.

    QSS can fill and round an indicator but cannot put a glyph inside one
    without an `image: url(...)` — a path on disk, which the SVG-rendering icon
    registry deliberately never produces. So Qt paints the item (background,
    indicator box, expander arrow, text) and this draws the tick or the
    indeterminate dash on top, at the rect the style itself reports. Hit-testing
    stays entirely Qt's, which is what keeps a click on the box toggling the
    right row.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pixmaps: dict[tuple[str, int, str], QPixmap] = {}
        BUS.theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, _dark: bool, _accent: str) -> None:
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self._pixmaps.clear()
        parent = self.parent()
        if isinstance(parent, QAbstractItemView):
            parent.viewport().update()

    def _glyph(self, key: str, size: int, colour: str, dpr: float) -> QPixmap:
        cache_key = (key, size, colour)
        pixmap = self._pixmaps.get(cache_key)
        if pixmap is None:
            pixmap = glyph_pixmap(key, size, colour, dpr)
            self._pixmaps[cache_key] = pixmap
        return pixmap

    def branch_glyph(self, key: str, colour: str, dpr: float) -> QPixmap:
        """A cached branch chevron, for `FolderTree.drawBranches`."""
        return self._glyph(key, CHEVRON_GLYPH, colour, dpr)

    def sizeHint(self,
                 option: QStyleOptionViewItem,
                 index: QModelIndex | QPersistentModelIndex) -> QSize:
        """Keep Qt's natural width — a tree column has to size to it — and pin
        the height to WinUI's `TreeViewItem` minimum."""
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(hint.height(), TREE_ROW_H))

    def paint(self,
              painter: QPainter,
              option: QStyleOptionViewItem,
              index: QModelIndex | QPersistentModelIndex) -> None:
        super().paint(painter, option, index)
        if index.column() != 0:
            return
        raw = index.data(Qt.ItemDataRole.CheckStateRole)
        if raw is None:
            return
        # `data()` hands back a PLAIN INT for CheckStateRole, and PySide6's
        # `Qt.CheckState` is not an IntEnum — `2 == Qt.CheckState.Checked` is
        # False (verified), which would silently draw the indeterminate dash on
        # every ticked row. Coerce before comparing.
        try:
            state = Qt.CheckState(raw)
        except ValueError:                        # pragma: no cover - defensive
            return
        if state == Qt.CheckState.Unchecked:
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else None
        if style is None:
            return
        box = QRectF(style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, widget))
        if box.isEmpty():
            return
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        colour = (theme.accent("text") if enabled
                  else theme.T(_TOKEN_TEXT_OFF))
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if state == Qt.CheckState.Checked:
            pixmap = self._glyph(_GLYPH_CHECK, CHECK_GLYPH, colour, dpr)
            painter.drawPixmap(int(box.center().x() - CHECK_GLYPH / 2.0),
                               int(box.center().y() - CHECK_GLYPH / 2.0),
                               pixmap)
        else:
            dash = QRectF(0.0, 0.0, float(SPACING["s"]), float(SPACING["xxs"]))
            dash.moveCenter(box.center())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(dash, RADII["progress_track"],
                                    RADII["progress_track"])
        painter.restore()


class FolderTree(QTreeWidget):
    """The selective-sync tree, with propagation in **both** directions.

    `Qt.ItemIsAutoTristate` would give the upward half for free, and this class
    deliberately does **not** use it. Measured: Qt's own parent recompute runs
    *after* `QTreeModel::setData` has already emitted the child's
    `itemChanged`, so it arrives as a second **top-level** signal that the
    re-entrancy guard has already been lifted for. One user click then produced
    two propagations and reported the *parent* as the thing the user toggled.
    Owning both directions here makes a click cost exactly one walk and name
    exactly one folder.

    The downward half has to be written in any case, and because
    `setCheckState()` re-emits `itemChanged`, writing it without a re-entrancy
    guard makes the first click walk the tree once per node it touches.

    A partially checked parent is never forced onto its children: "some of these
    are selected" is not a state a leaf can be in.
    """

    #: Emitted for the item the **user** changed — never for the cascade —
    #: as (rel_path, `Qt.CheckState` value).
    folder_toggled = Signal(str, int)
    #: Emitted once after any propagation settles, for a live "N folders, X GB"
    #: summary line.
    selection_changed = Signal()

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 size_column: bool = False) -> None:
        """
        Args:
            size_column: Show the second, right-aligned column of folder sizes.
        """
        super().__init__(parent)
        self._guard = False
        self._propagations = 0
        self._changes = 0
        self.setHeaderHidden(True)
        self.setColumnCount(2 if size_column else 1)
        self.setUniformRowHeights(True)
        self.setAnimated(not motion.reduced_motion())
        self.setExpandsOnDoubleClick(True)
        self.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        if size_column:
            header = self.header()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, header.ResizeMode.Stretch)
            header.setSectionResizeMode(1, header.ResizeMode.ResizeToContents)
        self._delegate = _CheckGlyphDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setStyleSheet(self.indicator_qss())
        self.itemChanged.connect(self._on_item_changed)

    # ── styling ──────────────────────────────────────────────────────────
    @staticmethod
    def indicator_qss(*, dark: bool | None = None) -> str:
        """The tree's own check-indicator rules, as a **widget** stylesheet.

        `theme.stylesheet()` styles `QCheckBox::indicator` but has no rule for
        `QTreeView::indicator`, and adding one is not this package's to make —
        the sheet is frozen. Without a rule the indicator falls through to the
        base style and paints a filled square in Qt's default palette, which
        reads as a black box on a Fluent surface.

        A widget stylesheet is the contained fix: it is merged with the
        application sheet, it is scoped to this one tree, and it costs a single
        repolish of the tree's subtree when it is set. The recipe below is the
        same one the frozen `QCheckBox::indicator` rules use, so the tree's
        boxes and a `FluentCheckBox` cannot drift apart.

        Args:
            dark: Force a theme; None asks the live one.

        Returns:
            A QSS fragment for `QTreeView.setStyleSheet()`.
        """
        def t(token: str) -> str:
            return theme.T(token, dark=dark)

        radius = RADII["control"]
        accent = theme.accent("rest", dark=dark)
        accent_hover = theme.accent("hover", dark=dark)
        return (
            f"QTreeView::indicator {{\n"
            f"  width: {TREE_INDICATOR}px; height: {TREE_INDICATOR}px;\n"
            f"}}\n"
            f"QTreeView::indicator:unchecked {{\n"
            f"  background: {t('ControlAltFillColorSecondary')};\n"
            f"  border: 1px solid {t('ControlStrongStrokeColorDefault')};\n"
            f"  border-radius: {radius}px;\n"
            f"}}\n"
            f"QTreeView::indicator:unchecked:hover {{\n"
            f"  background: {t('ControlAltFillColorTertiary')};\n"
            f"}}\n"
            f"QTreeView::indicator:checked, QTreeView::indicator:indeterminate {{\n"
            f"  background: {accent}; border: 1px solid {accent};\n"
            f"  border-radius: {radius}px;\n"
            f"}}\n"
            f"QTreeView::indicator:checked:hover,\n"
            f"QTreeView::indicator:indeterminate:hover {{\n"
            f"  background: {accent_hover}; border: 1px solid {accent_hover};\n"
            f"}}\n"
            f"QTreeView::indicator:disabled {{\n"
            f"  background: {t('ControlFillColorDisabled')};\n"
            f"  border: 1px solid {t('ControlStrongFillColorDisabled')};\n"
            f"}}\n"
        )

    def delegate(self) -> _CheckGlyphDelegate:
        """The delegate that overlays the checkmark glyph."""
        return self._delegate

    def drawBranches(self, painter: QPainter, rect, index) -> None:
        """Draw the expand chevron ourselves.

        The frozen sheet's `QTreeView::branch { background: transparent }` rule
        also removes the *arrow*, and QSS can only put one back with an
        `image: url(...)` — a file path, which the SVG icon registry does not
        produce. Without this a collapsed folder shows nothing to click, and
        "Choose folders" becomes undiscoverable.
        """
        if not self.model().hasChildren(index):
            return
        key = (_GLYPH_BRANCH_OPEN if self.isExpanded(index)
               else _GLYPH_BRANCH_SHUT)
        colour = theme.T(_TOKEN_TEXT2 if self.isEnabled() else _TOKEN_TEXT_OFF)
        pixmap = self._delegate.branch_glyph(key, colour,
                                             self.devicePixelRatioF())
        cell = QRectF(float(rect.right() - self.indentation()), float(rect.top()),
                      float(self.indentation()), float(rect.height()))
        painter.drawPixmap(int(cell.center().x() - CHEVRON_GLYPH / 2.0),
                           int(cell.center().y() - CHEVRON_GLYPH / 2.0), pixmap)

    def refresh_theme(self) -> None:
        """Rebuild the indicator sheet and the glyph cache for a new theme."""
        self.setStyleSheet(self.indicator_qss())
        self._delegate.refresh_theme()
        self.viewport().update()

    # ── building ─────────────────────────────────────────────────────────
    def add_folder(self,
                   name: str,
                   parent: QTreeWidgetItem | None = None,
                   *,
                   rel_path: str = "",
                   checked: Qt.CheckState = Qt.CheckState.Checked,
                   size_text: str = "") -> TriStateItem:
        """Add one folder and settle the tree around it.

        Args:
            name: The display name.
            parent: The parent folder, or None for a top-level one.
            rel_path: The path relative to the sync root.
            checked: The folder's initial state.
            size_text: The right-hand column, already formatted.

        Returns:
            The new :class:`TriStateItem`.
        """
        item = TriStateItem(name, rel_path=rel_path, size_text=size_text)
        previous = self._guard
        self._guard = True
        try:
            if parent is None:
                self.addTopLevelItem(item)
            else:
                parent.addChild(item)
            item.setCheckState(0, Qt.CheckState(checked))
            self._push_down(item, item.checkState(0))
            self._pull_up(item.parent())
        finally:
            self._guard = previous
        return item

    def top_level_items(self) -> tuple[QTreeWidgetItem, ...]:
        """Every root folder, in order."""
        return tuple(self.topLevelItem(i) for i in range(self.topLevelItemCount()))

    def walk(self, item: QTreeWidgetItem | None = None) -> Iterator[QTreeWidgetItem]:
        """Depth-first over every item, parents before children."""
        if item is None:
            for root in self.top_level_items():
                yield root
                yield from self.walk(root)
            return
        for index in range(item.childCount()):
            child = item.child(index)
            yield child
            yield from self.walk(child)

    # ── propagation ──────────────────────────────────────────────────────
    def excluded(self) -> list[str]:
        """The folders the user has unticked, as sync-root-relative paths.

        The tree could be populated and could report *changes*, but nothing
        could read the resulting selection out of it — so "Choose folders"
        had no way to tell anyone what the user had chosen, and applied the
        selection that was already stored instead.

        Only fully-unchecked folders are returned. A partially checked parent
        means "some of these are excluded", and excluding the parent would
        exclude the children the user deliberately kept.

        Returns:
            Relative paths, in tree order.
        """
        return [item.rel_path() for item in self.walk()
                if item.checkState(0) is Qt.CheckState.Unchecked
                and item.rel_path()]

    def is_updating(self) -> bool:
        """True while a propagation is running; the guard's own state."""
        return self._guard

    def propagation_count(self) -> int:
        """How many propagations have actually run. A user click costs one."""
        return self._propagations

    def change_count(self) -> int:
        """How many `itemChanged` signals were seen, cascade included.

        Together with :meth:`propagation_count` this is the evidence that the
        guard works: one click produces many changes and exactly one walk.
        """
        return self._changes

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        self._changes += 1
        if column != 0 or self._guard:
            return
        self._propagate(item, notify=True)

    def _propagate(self, item: QTreeWidgetItem, *, notify: bool) -> None:
        """Push the item's state down and roll the ancestors back up, once."""
        self._guard = True
        self._propagations += 1
        try:
            state = item.checkState(0)
            self._push_down(item, state)
            self._pull_up(item.parent())
        finally:
            self._guard = False
        if notify:
            # `Qt.CheckState` is a real Python enum here — `int()` on it raises,
            # `.value` is the wire-compatible integer the signal declares.
            self.folder_toggled.emit(rel_path_of(item), item.checkState(0).value)
        self.selection_changed.emit()

    def _push_down(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        """Force every descendant to `state`.

        `PartiallyChecked` is never pushed: a leaf cannot be "partly selected",
        and forcing it would destroy the child states the partial is describing.
        """
        if state == Qt.CheckState.PartiallyChecked:
            return
        for index in range(item.childCount()):
            child = item.child(index)
            if child.checkState(0) != state:
                child.setCheckState(0, state)
            self._push_down(child, state)

    def _pull_up(self, item: QTreeWidgetItem | None) -> None:
        """Recompute every ancestor from its children, root-ward."""
        while item is not None:
            total = item.childCount()
            if total == 0:
                item = item.parent()
                continue
            checked = 0
            partial = False
            for index in range(total):
                child_state = item.child(index).checkState(0)
                if child_state == Qt.CheckState.Checked:
                    checked += 1
                elif child_state == Qt.CheckState.PartiallyChecked:
                    partial = True
            if partial or 0 < checked < total:
                new_state = Qt.CheckState.PartiallyChecked
            elif checked == total:
                new_state = Qt.CheckState.Checked
            else:
                new_state = Qt.CheckState.Unchecked
            if item.checkState(0) != new_state:
                item.setCheckState(0, new_state)
            item = item.parent()

    # ── programmatic changes ─────────────────────────────────────────────
    def set_state(self,
                  item: QTreeWidgetItem,
                  state: Qt.CheckState,
                  *,
                  silent: bool = True) -> None:
        """Set one folder's state and run the same both-direction propagation.

        Args:
            item: The folder to change.
            state: Its new state.
            silent: Do not emit :attr:`folder_toggled`. This is the default
                because a programmatic change is the UI reflecting a stored
                fact, and restoring a saved selection must not look like the
                user ticking forty folders.
        """
        previous = self._guard
        self._guard = True
        try:
            item.setCheckState(0, Qt.CheckState(state))
        finally:
            self._guard = previous
        self._propagate(item, notify=not silent)

    def set_all(self, state: Qt.CheckState) -> None:
        """Set every folder in the tree, in one propagation."""
        self._guard = True
        self._propagations += 1
        try:
            for root in self.top_level_items():
                root.setCheckState(0, Qt.CheckState(state))
                self._push_down(root, Qt.CheckState(state))
        finally:
            self._guard = False
        self.selection_changed.emit()

    # ── reading back ─────────────────────────────────────────────────────
    def states(self) -> dict[str, Qt.CheckState]:
        """Every folder's path mapped to its state."""
        return {rel_path_of(item): item.checkState(0) for item in self.walk()}

    def checked_paths(self) -> tuple[str, ...]:
        """Every fully checked folder, in tree order."""
        return tuple(rel_path_of(item) for item in self.walk()
                     if item.checkState(0) == Qt.CheckState.Checked)

    def partial_paths(self) -> tuple[str, ...]:
        """Every partially checked folder — the ancestors a filter must keep."""
        return tuple(rel_path_of(item) for item in self.walk()
                     if item.checkState(0) == Qt.CheckState.PartiallyChecked)

    def top_checked_paths(self) -> tuple[str, ...]:
        """The shallowest fully checked folders.

        A checked folder under a checked folder is redundant in an rclone
        filter, so this is the minimal set of subtrees to include.
        """
        out: list[str] = []
        for item in self.walk():
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            parent = item.parent()
            if parent is not None and parent.checkState(0) == Qt.CheckState.Checked:
                continue
            out.append(rel_path_of(item))
        return tuple(out)


__all__ = [
    "ROW_H", "ROW_H_COMPACT", "ROW_INSET", "ROW_ICON", "ROW_ICON_GAP",
    "ROW_STATUS", "ROW_PILL_INSET", "SELECTION_BAR_W", "SELECTION_BAR_H",
    "SELECTION_BAR_R", "LINE_GAP", "BAR_GAP", "BAR_FILL_H", "BAR_TRACK_H",
    "CHIP_PAD", "CHIP_GAP", "SUBTITLE_SEPARATOR",
    "TREE_ROW_H", "TREE_INDICATOR", "CHECK_GLYPH", "CHEVRON_GLYPH",
    "ROLE_PRIMARY", "ROLE_SECONDARY", "ROW_ROLE", "PATH_ROLE",
    "STATUS_TOKEN", "FILE_STATE_FOR_ACTIVITY", "status_colour",
    "ActivityRow", "ActivityDelegate", "ActivityListView",
    "TriStateItem", "FolderTree", "rel_path_of",
]
