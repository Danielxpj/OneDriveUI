"""Fluent containers: the settings card, the expander, the info bar, the dialog.

Four things in here are load-bearing and were each measured rather than assumed:

  * **The frozen `#Card` rule cannot size a settings card.** `min-height: 68px`
    in QSS sizes the CONTENT box, so with the rule's own 1 px border the widget
    lands at **70 px**, and `#Card:hover` lights up every card whether or not it
    is clickable. :class:`SettingsCard` therefore paints its own box and pins
    `minimumHeight` to exactly `METRICS["card_min_h"]`. This is the same trap
    `qss.ICON_BUTTON_CONTENT` documents for the 32 px icon button.
  * **QSS `padding` on a container DOES move its layout.** A `QFrame` carrying
    `padding: 12px; border: 1px` reports `contentsMargins() == (13,13,13,13)`
    and its layout is inset by that much (verified). So :class:`InfoBar`, which
    *does* use the frozen banner rules, sets its own layout margins to **zero**
    — setting them again would double the inset.
  * **`QGraphicsDropShadowEffect` paints inside the widget's own bounds.** A
    dialog must therefore reserve `blurRadius` of layout margin on every side
    and `blurRadius + dy` at the bottom, or the shadow is clipped off. That is
    :func:`shadow_margins`, and :class:`ContentDialog` is built around it.
  * **A `QGraphicsEffect` is exclusive.** One effect per widget, so the drop
    shadow and `motion.fade_in`'s opacity effect cannot coexist on the same
    widget. The dialog shadows its *surface* child, leaving the dialog itself
    free to fade.

No colour, no icon name and no user-facing string is written here. Colours come
from `theme.T()` / `theme.accent()`, geometry from `theme.METRICS` /
`theme.RADII` / `theme.SPACING`, glyphs from `icons.GLYPHS`, and every label is
passed in by the caller (WP-12/WP-13 read them out of `strings.py`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable

from PySide6.QtCore import (
    Property, QEvent, QMargins, QPoint, QRectF, QSize, Qt, Signal,
)
from PySide6.QtGui import (
    QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen, QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton, QDialog, QFrame, QGraphicsDropShadowEffect, QGridLayout,
    QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)

from onedriveui.models import IssueSeverity
from onedriveui.ui import fonts, icons, motion, qss, theme
from onedriveui.ui.theme import METRICS, OBJ, PROP, RADII, SPACING
from onedriveui.ui.widgets.controls import (
    ButtonVariant, FluentButton, ThemeAware, icon_button, paint_focus_ring,
)

# ═════════════════════════════════════════════════════════════════════════════
# Geometry. Every value is either a `theme` token or a WinUI / CommunityToolkit
# number the token tables do not carry; the latter are named here once.
# ═════════════════════════════════════════════════════════════════════════════

#: SettingsCard, verbatim from `CommunityToolkit/Windows`.
CARD_MIN_H: int = METRICS["card_min_h"]                 # SettingsCardMinHeight 68
CARD_PAD: int = METRICS["card_pad"]                     # SettingsCardPadding 16
CARD_ICON: int = METRICS["card_icon"]                   # HeaderIconMaxSize 20
CARD_ICON_GAP: int = METRICS["card_icon_gap"]           # HeaderIconMargin 2,0,20,0
CARD_CONTENT_MIN_W: int = METRICS["card_content_min_w"]  # ContentMinWidth 120
CARD_WRAP_THRESHOLD: int = METRICS["card_wrap_threshold"]  # WrapThreshold 476

#: `SettingsCardNoIconThreshold`. The toolkit ships a second, lower threshold for
#: a card with no header icon; `theme.METRICS` only carries the first.
CARD_WRAP_NO_ICON_THRESHOLD: int = 286

#: `SettingsCardVerticalHeaderContentSpacing` — the gap once the card has
#: wrapped its control onto a second row.
CARD_WRAP_SPACING: int = SPACING["s"]

#: The gap between the text column and the control.
CARD_CONTENT_GAP: int = SPACING["l"]

#: `SettingsCardActionIconMaxSize` is 13, which is **not** a native Fluent glyph
#: size (`icons.GLYPH_SIZES` is 12/16/20/24/28/32/48) — scaling a 16 px glyph
#: down to 13 puts the stroke weight wrong, so the chevron is drawn at 12.
CARD_ACTION_ICON: int = SPACING["m"]
#: `SettingsCardActionIconMargin` 14,0,0,0, snapped onto the 4 px grid.
CARD_ACTION_GAP: int = SPACING["l"]

#: Cards stack with a 4 px vertical gap inside one group.
CARD_GROUP_GAP: int = SPACING["xs"]

#: The toolkit dims a disabled header icon rather than hiding it.
CARD_DISABLED_ICON_OPACITY: float = 0.4

#: SettingsExpander, verbatim: header `16,16,4,16`, children `58,8,44,8`.
EXPANDER_HEADER_PAD: tuple[int, int, int, int] = METRICS["expander_header_pad"]
EXPANDER_CHILD_PAD: tuple[int, int, int, int] = METRICS["expander_child_pad"]
#: `SettingsExpanderChevronButtonWidth/Height` — a 32 x 32 button.
EXPANDER_CHEVRON: int = METRICS["expander_chevron"]
#: The chevron glyph inside that button, and how far it rotates when open.
CHEVRON_GLYPH: int = SPACING["m"]
CHEVRON_OPEN_DEG: float = 180.0
#: `SettingsExpanderContentMinHeight`.
EXPANDER_CONTENT_MIN_H: int = SPACING["l"]

#: ContentDialog, verbatim from `generic.xaml`.
DIALOG_MIN_W: int = 320
DIALOG_MAX_W: int = 548
DIALOG_MIN_H: int = 184
DIALOG_MAX_H: int = 756
DIALOG_BUTTON_MIN_W: int = 130
DIALOG_BUTTON_MAX_W: int = 202
#: `ContentDialogPadding`. WinUI's dialog breathes at the xxl step.
DIALOG_PAD: int = SPACING["xxl"]

#: Qt's "no maximum", used to release an animated `maximumHeight` clamp.
UNBOUNDED: int = 16777215

# The description font size the toolkit names and the caption step of the ramp
# are the same number; a divergence would mean one of the two moved.
if METRICS["card_desc_size"] != theme.font_px("caption"):  # pragma: no cover
    raise ValueError(
        "containers: SettingsCardDescriptionFontSize "
        f"{METRICS['card_desc_size']} != the caption ramp step "
        f"{theme.font_px('caption')}"
    )

# ═════════════════════════════════════════════════════════════════════════════
# Type-ramp roles and glyph keys, resolved once so a typo fails at import.
# ═════════════════════════════════════════════════════════════════════════════

_ROLE_TITLE = "body"
_ROLE_STRONG = "body_strong"
_ROLE_CAPTION = "caption"
_ROLE_SUBTITLE = "subtitle"
for _role in (_ROLE_TITLE, _ROLE_STRONG, _ROLE_CAPTION, _ROLE_SUBTITLE):
    theme.font_px(_role)                    # raises KeyError on an unknown role
del _role

_GLYPH_CHEVRON = "chevron_down"
_GLYPH_ACTION = "chevron_right"
_GLYPH_CLOSE = "close"
_GLYPH_INFO = "info"
_GLYPH_SUCCESS = "check"
_GLYPH_WARNING = "warning"
_GLYPH_ERROR = "error"
for _key in (_GLYPH_CHEVRON, _GLYPH_ACTION, _GLYPH_CLOSE, _GLYPH_INFO,
             _GLYPH_SUCCESS, _GLYPH_WARNING, _GLYPH_ERROR):
    icons.glyph_stem(_key)                  # raises KeyError on an unknown key
del _key

#: Token names used often enough to be worth naming once.
_TOKEN_CARD_FILL = "CardBackgroundFillColorDefault"
_TOKEN_CARD_STROKE = "CardStrokeColorDefault"
_TOKEN_HOVER_FILL = "ControlFillColorSecondary"
_TOKEN_PRESS_FILL = "ControlFillColorTertiary"
_TOKEN_DISABLED_FILL = "ControlFillColorDisabled"
_TOKEN_STROKE = "ControlStrokeColorDefault"
_TOKEN_EDGE = "ControlStrokeColorSecondary"
_TOKEN_TEXT = "TextFillColorPrimary"
_TOKEN_TEXT2 = "TextFillColorSecondary"
_TOKEN_TEXT_OFF = "TextFillColorDisabled"
_TOKEN_DIVIDER = "DividerStrokeColorDefault"
_TOKEN_CARD_FILL_2 = "CardBackgroundFillColorSecondary"


# ═════════════════════════════════════════════════════════════════════════════
# Shared painting helpers. `lists` and `chrome` import these rather than
# growing a second copy.
# ═════════════════════════════════════════════════════════════════════════════

def box_path(rect: QRectF,
             top_left: float,
             top_right: float,
             bottom_right: float,
             bottom_left: float) -> QPainterPath:
    """A rounded-rectangle path with an independent radius per corner.

    `QPainter.drawRoundedRect` rounds all four corners equally, but an expanded
    `SettingsExpander` header is rounded only along its top edge and its content
    block only along its bottom — so the two read as one box.

    Args:
        rect: The box, already inset by half a pixel if it carries a 1 px stroke.
        top_left: Radius of the top-left corner, in logical px. 0 is square.
        top_right: Radius of the top-right corner.
        bottom_right: Radius of the bottom-right corner.
        bottom_left: Radius of the bottom-left corner.

    Returns:
        A closed `QPainterPath` walking the box clockwise from the top-left.
    """
    path = QPainterPath()
    path.moveTo(rect.left() + top_left, rect.top())
    path.lineTo(rect.right() - top_right, rect.top())
    if top_right > 0.0:
        path.arcTo(QRectF(rect.right() - 2 * top_right, rect.top(),
                          2 * top_right, 2 * top_right), 90.0, -90.0)
    path.lineTo(rect.right(), rect.bottom() - bottom_right)
    if bottom_right > 0.0:
        path.arcTo(QRectF(rect.right() - 2 * bottom_right,
                          rect.bottom() - 2 * bottom_right,
                          2 * bottom_right, 2 * bottom_right), 0.0, -90.0)
    path.lineTo(rect.left() + bottom_left, rect.bottom())
    if bottom_left > 0.0:
        path.arcTo(QRectF(rect.left(), rect.bottom() - 2 * bottom_left,
                          2 * bottom_left, 2 * bottom_left), 270.0, -90.0)
    path.lineTo(rect.left(), rect.top() + top_left)
    if top_left > 0.0:
        path.arcTo(QRectF(rect.left(), rect.top(), 2 * top_left, 2 * top_left),
                   180.0, -90.0)
    path.closeSubpath()
    return path


def glyph_pixmap(key: str,
                 size: int,
                 colour: str | None = None,
                 dpr: float = 1.0) -> QPixmap:
    """One glyph from the frozen registry, rendered for a `dpr` surface.

    `QIcon.pixmap(QSize)` alone returns a device-pixel-ratio-1 pixmap, which
    draws blurred on a 1.25x or 2x display. Passing the ratio makes Qt render
    the SVG at device resolution and tag the result, so it lays out at `size`
    logical px either way.

    Args:
        key: A key of `icons.GLYPHS`.
        size: Logical size in px. Snapped to the nearest native glyph size by
            `icons.icon()`; never scale a 24 px glyph to 16.
        colour: An opaque `#RRGGBB` the caller resolved from `theme`. None uses
            the theme's primary text colour.
        dpr: The target surface's `devicePixelRatioF()`.

    Returns:
        A `QPixmap` tagged with `dpr`.
    """
    icon = icons.icon(key, size, color=colour)
    return icon.pixmap(QSize(size, size), float(dpr))


def drop_shadow(widget: QWidget,
                name: str = "flyout",
                *,
                dark: bool | None = None) -> QGraphicsDropShadowEffect:
    """Attach a Fluent elevation shadow to `widget` and return the effect.

    `theme.SHADOWS` already carries Qt's doubled blur radius (Qt's radius is the
    kernel diameter, CSS's is roughly two sigma) — do not double it again. The
    shadow colour is black at a per-theme alpha in **both** themes, which is why
    only the alpha varies.

    The effect paints inside the widget's own bounds, so whatever lays `widget`
    out must reserve :func:`shadow_margins` around it.

    Args:
        widget: The widget to shadow. Any previous `QGraphicsEffect` is replaced
            — `QGraphicsEffect` is exclusive, one per widget.
        name: A key of `theme.SHADOWS`: "card", "flyout" or "dialog".
        dark: Force a theme; None asks the live one.

    Returns:
        The attached `QGraphicsDropShadowEffect`.

    Raises:
        KeyError: for an unknown shadow name.
    """
    blur, dy, alpha = theme.shadow(name, dark=dark)
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(float(blur))
    effect.setOffset(0.0, float(dy))
    colour = QColor(Qt.GlobalColor.black)
    colour.setAlpha(int(alpha))
    effect.setColor(colour)
    widget.setGraphicsEffect(effect)
    return effect


def shadow_margins(name: str = "flyout") -> QMargins:
    """The layout margin a shadowed widget must be given room in.

    `blurRadius` on every side, plus the vertical offset at the bottom. Without
    it `QGraphicsDropShadowEffect` is clipped by the widget's own rectangle and
    the elevation simply does not appear.

    Args:
        name: A key of `theme.SHADOWS`.

    Returns:
        `QMargins(blur, blur, blur, blur + dy)`.

    Raises:
        KeyError: for an unknown shadow name.
    """
    blur, dy, _alpha = theme.shadow(name)
    return QMargins(blur, blur, blur, blur + dy)


def _label(text: str,
           parent: QWidget,
           *,
           role: str,
           secondary: bool = False) -> QLabel:
    """A ramp-styled label with an exact line box.

    The type role rides a dynamic property so the sheet supplies the size and
    weight — a per-widget `setFont()` would be recorded by `QStyleSheetStyle` as
    the widget's custom font and restored on every repolish.
    """
    out = QLabel(text, parent)
    qss.set_property(out, PROP.TYPE, role)
    if secondary:
        qss.set_property(out, PROP.ROLE, "secondary")
    out.setFixedHeight(fonts.line_height(role))
    out.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return out


class _GlyphLabel(QLabel):
    """A glyph from the frozen registry, re-tinted whenever the theme moves."""

    #: The toolkit dims a disabled header icon rather than hiding it.
    DISABLED_OPACITY: float = CARD_DISABLED_ICON_OPACITY

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 key: str = _GLYPH_INFO,
                 size: int = SPACING["l"],
                 token: str | None = None,
                 accent: bool = False,
                 alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft
                 | Qt.AlignmentFlag.AlignVCenter) -> None:
        """
        Args:
            key: A key of `icons.GLYPHS`.
            size: Logical glyph size in px.
            token: A `theme.TOKENS` name for the tint, or None for the primary
                text colour.
            accent: Tint with the live accent instead of a token.
            alignment: Where the glyph sits inside the label's box.
        """
        super().__init__(parent)
        self._key = key
        self._size = int(size)
        self._token = token
        self._accent = bool(accent)
        self.setAlignment(alignment)
        self.setFixedHeight(self._size)
        self.refresh_theme()

    def glyph_key(self) -> str:
        return self._key

    def set_glyph(self, key: str, size: int | None = None) -> None:
        """Swap the glyph. Raises KeyError for a key outside `icons.GLYPHS`."""
        icons.glyph_stem(key)
        self._key = key
        if size is not None:
            self._size = int(size)
            self.setFixedHeight(self._size)
        self.refresh_theme()

    def set_tint(self, token: str | None, *, accent: bool = False) -> None:
        """Re-tint from a `theme` token, or from the live accent."""
        self._token = token
        self._accent = bool(accent)
        self.refresh_theme()

    def colour(self) -> str:
        """The opaque `#RRGGBB` the glyph is currently drawn in."""
        if not self.isEnabled():
            return theme.T(_TOKEN_TEXT_OFF)
        if self._accent:
            return theme.accent()
        return theme.T(self._token if self._token else _TOKEN_TEXT)

    def refresh_theme(self) -> None:
        """Re-render the pixmap for the current theme and enabled state."""
        pixmap = glyph_pixmap(self._key, self._size, self.colour(),
                              self.devicePixelRatioF())
        if not self.isEnabled() and self.DISABLED_OPACITY < 1.0:
            faded = QPixmap(pixmap.size())
            faded.setDevicePixelRatio(pixmap.devicePixelRatio())
            faded.fill(Qt.GlobalColor.transparent)
            painter = QPainter(faded)
            painter.setOpacity(self.DISABLED_OPACITY)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()
            pixmap = faded
        self.setPixmap(pixmap)

    def changeEvent(self, event: QEvent) -> None:
        """Re-tint when the widget is enabled or disabled."""
        if event.type() == QEvent.Type.EnabledChange:
            self.refresh_theme()
        super().changeEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
# SectionHeading
# ═════════════════════════════════════════════════════════════════════════════

class SectionHeading(QLabel):
    """A settings group's heading: Body Strong, 24 px above and 8 px below.

    The rhythm is the whole point of the class — Fluent groups are separated by
    whitespace, never by rules, so the two margins are what make a page read as
    grouped at all.
    """

    #: Whitespace above and below, from the frozen spacing scale.
    TOP: int = SPACING["xxl"]
    BOTTOM: int = SPACING["s"]

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        """
        Args:
            text: The heading. Callers pass a string from `strings.py`.
        """
        super().__init__(text, parent)
        qss.set_property(self, PROP.TYPE, _ROLE_STRONG)
        self.setContentsMargins(0, self.TOP, 0, self.BOTTOM)
        self.setFixedHeight(fonts.line_height(_ROLE_STRONG) + self.TOP + self.BOTTOM)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def refresh_theme(self) -> None:
        qss.repolish(self)


# ═════════════════════════════════════════════════════════════════════════════
# SettingsCard
# ═════════════════════════════════════════════════════════════════════════════

class SettingsCard(ThemeAware, QFrame):
    """The Windows 11 settings card.

    ``[20 px icon] 20 gap [title / description] …flex… [control ≥120] [chevron]``
    inside a 68 px box with 16 px of padding, a 4 px radius and a 1 px stroke.

    The box is painted here rather than declared in QSS for two measured
    reasons: the frozen `#Card` rule's `min-height` sizes the CONTENT box and so
    yields 70 px, and its `:hover` fills every card whether or not the card does
    anything when clicked. The CommunityToolkit card only lights up when
    `IsClickEnabled` is set, which is what `clickable` reproduces.

    Below `CARD_WRAP_THRESHOLD` the control drops onto its own row, exactly as
    the toolkit's `SettingsCardWrapThreshold` does.
    """

    #: Emitted when a `clickable` card is clicked or activated by keyboard.
    clicked = Signal()

    def __init__(self,
                 title: str = "",
                 parent: QWidget | None = None,
                 *,
                 description: str = "",
                 icon_key: str | None = None,
                 content: QWidget | None = None,
                 clickable: bool = False,
                 action_icon: bool = True,
                 boxed: bool = True) -> None:
        """
        Args:
            title: The card's primary line. From `strings.py`.
            description: The optional Caption 12 second line.
            icon_key: A key of `icons.GLYPHS` for the 20 px header icon.
            content: The control the card carries on its trailing edge.
            clickable: Give the card hover and press states and emit
                :attr:`clicked`; a chevron is drawn on the trailing edge.
            action_icon: Draw the trailing chevron on a clickable card. The
                expander's header turns it off — its own rotating chevron button
                stands in that place instead.
            boxed: Paint the card's own fill and stroke. A card used as an
                expander's child row turns this off: the row already sits inside
                the expander's box, and a second one would read as a card inside
                a card. :meth:`SettingsExpander.add_row` does it for you.
        """
        super().__init__(parent)
        self._clickable = False
        self._action_icon = bool(action_icon)
        self._boxed = bool(boxed)
        self._hovered = False
        self._pressed = False
        self._wrapped = False
        self._content: QWidget | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(CARD_MIN_H)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        grid = QGridLayout(self)
        grid.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(CARD_WRAP_SPACING)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(2, CARD_CONTENT_MIN_W)
        self._grid = grid

        self._icon = _GlyphLabel(self, key=icon_key or _GLYPH_INFO, size=CARD_ICON)
        self._icon.setFixedWidth(CARD_ICON + CARD_ICON_GAP)
        self._icon.setVisible(icon_key is not None)
        self._icon_key = icon_key
        grid.addWidget(self._icon, 0, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QWidget(self)
        text_column = QVBoxLayout(text)
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(0)
        self._title = _label(title, text, role=_ROLE_TITLE)
        self._description = _label(description, text, role=_ROLE_CAPTION,
                                   secondary=True)
        self._description.setVisible(bool(description))
        text_column.addWidget(self._title)
        text_column.addWidget(self._description)
        self._text = text
        grid.addWidget(text, 0, 1, Qt.AlignmentFlag.AlignVCenter)

        holder = QWidget(self)
        holder_row = QHBoxLayout(holder)
        holder_row.setContentsMargins(CARD_CONTENT_GAP, 0, 0, 0)
        holder_row.setSpacing(SPACING["s"])
        holder_row.addStretch(1)
        self._holder = holder
        self._holder_row = holder_row
        grid.addWidget(holder, 0, 2, Qt.AlignmentFlag.AlignVCenter)

        self._chevron = _GlyphLabel(
            self, key=_GLYPH_ACTION, size=CARD_ACTION_ICON, token=_TOKEN_TEXT2,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        )
        self._chevron.setFixedWidth(CARD_ACTION_GAP + CARD_ACTION_ICON)
        self._chevron.setVisible(False)
        grid.addWidget(self._chevron, 0, 3, Qt.AlignmentFlag.AlignVCenter)
        # Column 4 is left empty for a subclass that needs its own trailing
        # control — the expander's rotating chevron button lands there.

        if content is not None:
            self.set_content(content)
        self.set_clickable(clickable)
        self._install_theme_hook()

    # ── content ──────────────────────────────────────────────────────────
    def title(self) -> str:
        return self._title.text()

    def set_title(self, text: str) -> None:
        """Set the primary line. Callers pass a string from `strings.py`."""
        self._title.setText(text)

    def description(self) -> str:
        return self._description.text()

    def set_description(self, text: str) -> None:
        """Set (or clear) the Caption 12 second line."""
        self._description.setText(text)
        self._description.setVisible(bool(text))

    def icon_key(self) -> str | None:
        return self._icon_key

    def set_icon(self, key: str | None) -> None:
        """Set (or clear) the 20 px header icon.

        Raises:
            KeyError: for a key that is not in `icons.GLYPHS`.
        """
        self._icon_key = key
        if key is not None:
            self._icon.set_glyph(key)
        self._icon.setVisible(key is not None)
        self._apply_wrap(force=True)

    def grid_layout(self) -> QGridLayout:
        """The card's own grid, for a container that re-insets it."""
        return self._grid

    def content(self) -> QWidget | None:
        """The control on the card's trailing edge, if any."""
        return self._content

    def set_content(self, widget: QWidget | None) -> None:
        """Put a control on the trailing edge, replacing any previous one."""
        if self._content is not None:
            self._holder_row.removeWidget(self._content)
            self._content.setParent(None)
        self._content = widget
        if widget is not None:
            widget.setParent(self._holder)
            self._holder_row.addWidget(widget)
        self._holder.setVisible(widget is not None)
        self._apply_wrap(force=True)

    # ── interaction ──────────────────────────────────────────────────────
    def is_clickable(self) -> bool:
        return self._clickable

    def set_clickable(self, enabled: bool) -> None:
        """Turn the whole card into a click target with hover and press states."""
        self._clickable = bool(enabled)
        self._chevron.setVisible(self._clickable and self._action_icon)
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._clickable
                       else Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if self._clickable
                            else Qt.FocusPolicy.NoFocus)
        self.update()

    def is_hovered(self) -> bool:
        return self._hovered

    def is_pressed(self) -> bool:
        return self._pressed

    def is_wrapped(self) -> bool:
        """True while the control sits on its own row."""
        return self._wrapped

    def is_boxed(self) -> bool:
        """True while the card paints its own fill and stroke."""
        return self._boxed

    def set_boxed(self, boxed: bool) -> None:
        """Turn the card's own box painting on or off."""
        self._boxed = bool(boxed)
        self.update()

    def wrap_threshold(self) -> int:
        """The width below which the control wraps, per the toolkit's two.

        Keyed off whether an icon was *set*, not off `isVisible()`: a child of a
        widget that has never been shown reports False, which would silently
        give every freshly built card the no-icon threshold.
        """
        return (CARD_WRAP_THRESHOLD if self._icon_key is not None
                else CARD_WRAP_NO_ICON_THRESHOLD)

    def enterEvent(self, event: QEvent) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        fire = (self._clickable and self._pressed
                and self.rect().contains(event.position().toPoint()))
        self._pressed = False
        self.update()
        super().mouseReleaseEvent(event)
        if fire:
            self.clicked.emit()

    def keyPressEvent(self, event) -> None:
        """Space and Return activate a clickable card, as WinUI's does."""
        if self._clickable and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return,
                                               Qt.Key.Key_Enter):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # ── layout ───────────────────────────────────────────────────────────
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_wrap()

    def showEvent(self, event) -> None:
        """Re-evaluate the wrap on first show.

        `QWidget.resize()` on a widget that has never been shown does not
        deliver a `QResizeEvent` — Qt sets `WA_PendingResizeEvent` and holds it
        until the widget is shown — so a card sized before it appears would
        otherwise miss its own wrap threshold.
        """
        super().showEvent(event)
        self._apply_wrap()

    def _apply_wrap(self, *, force: bool = False) -> None:
        """Move the control between the header row and its own row."""
        wrapped = (self._content is not None
                   and self.width() < self.wrap_threshold())
        if wrapped == self._wrapped and not force:
            return
        self._wrapped = wrapped
        self._grid.removeWidget(self._text)
        self._grid.removeWidget(self._holder)
        if wrapped:
            self._grid.addWidget(self._text, 0, 1, 1, 2,
                                 Qt.AlignmentFlag.AlignVCenter)
            self._grid.addWidget(self._holder, 1, 1, 1, 2,
                                 Qt.AlignmentFlag.AlignLeft)
            self._holder_row.setContentsMargins(0, 0, 0, 0)
        else:
            self._grid.addWidget(self._text, 0, 1,
                                 Qt.AlignmentFlag.AlignVCenter)
            self._grid.addWidget(self._holder, 0, 2,
                                 Qt.AlignmentFlag.AlignVCenter)
            self._holder_row.setContentsMargins(CARD_CONTENT_GAP, 0, 0, 0)
        self._text.setVisible(True)
        self._holder.setVisible(self._content is not None)

    # ── painting ─────────────────────────────────────────────────────────
    def corner_radii(self) -> tuple[float, float, float, float]:
        """The four corner radii. Overridden by the expander's header."""
        radius = float(RADII["control"])
        return radius, radius, radius, radius

    def box_colours(self) -> tuple[str, str, str | None]:
        """-> (fill, stroke, bottom edge or None) for the current state.

        A clickable card follows the toolkit's brush table: rest is the card
        fill, hover is `ControlFillColorSecondary` with the elevation edge,
        press drops to `ControlFillColorTertiary` and loses the edge.
        """
        if not self.isEnabled():
            return theme.T(_TOKEN_DISABLED_FILL), theme.T(_TOKEN_STROKE), None
        if self._clickable and self._pressed:
            return theme.T(_TOKEN_PRESS_FILL), theme.T(_TOKEN_STROKE), None
        if self._clickable and self._hovered:
            return (theme.T(_TOKEN_HOVER_FILL), theme.T(_TOKEN_STROKE),
                    theme.T(_TOKEN_EDGE))
        return theme.T(_TOKEN_CARD_FILL), theme.T(_TOKEN_CARD_STROKE), None

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill, stroke, edge = self.box_colours()
        if not self._boxed:
            # An unboxed card still shows its interaction states, just without a
            # stroke — that is how a clickable expander row reads.
            stroke, edge = fill, None
        path = box_path(rect, *self.corner_radii())
        painter.setPen(QPen(QColor(stroke), 1.0) if self._boxed
                       else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill) if (self._boxed or self._hovered
                                          or self._pressed)
                         else Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if edge is not None:
            radius = self.corner_radii()[3]
            painter.setPen(QPen(QColor(edge), 1.0))
            painter.drawLine(QPoint(int(rect.left() + radius), int(rect.bottom())),
                             QPoint(int(rect.right() - radius), int(rect.bottom())))
        if self._clickable and self.hasFocus():
            # The ring is inflated `focus_inflate` px OUTSIDE whatever it is
            # given, so it is handed an inset rect and lands inside the card
            # rather than being clipped away by the widget's own bounds.
            inflate = float(METRICS["focus_inflate"])
            paint_focus_ring(
                painter,
                QRectF(self.rect()).adjusted(inflate, inflate, -inflate, -inflate),
                float(RADII["control"]),
            )
        painter.end()

    def refresh_theme(self) -> None:
        """Repaint the box and re-tint every glyph and label."""
        self._icon.refresh_theme()
        self._chevron.refresh_theme()
        qss.repolish(self._title)
        qss.repolish(self._description)
        self.update()

    def changeEvent(self, event: QEvent) -> None:
        """Repaint on enable/disable; the glyphs re-tint themselves."""
        if event.type() == QEvent.Type.EnabledChange:
            self.update()
        super().changeEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
# SettingsExpander
# ═════════════════════════════════════════════════════════════════════════════

class _ChevronButton(ThemeAware, QAbstractButton):
    """The expander's 32 x 32 chevron, rotating 0 -> 180 degrees when it opens."""

    SIZE: int = EXPANDER_CHEVRON
    GLYPH: int = CHEVRON_GLYPH

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._angle = 0.0
        self._anim = None
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._install_theme_hook()

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, value: float) -> None:
        self._angle = float(value)
        self.update()

    #: Degrees of rotation, 0 closed to 180 open. The name must match the byte
    #: string handed to QPropertyAnimation exactly or the animation no-ops.
    chevronAngle = Property(float, _get_angle, _set_angle)

    def angle(self) -> float:
        return self._angle

    def animate_to(self, open_: bool) -> None:
        """Rotate to the open or closed angle over the Fluent fast duration."""
        self._anim = motion.animate(
            self, b"chevronAngle", CHEVRON_OPEN_DEG if open_ else 0.0,
            duration="flyout", easing="easy_ease", parent=self,
        )

    def land_at(self, open_: bool) -> None:
        """Jump to the open or closed angle with no animation at all."""
        motion.stop(self._anim)
        self._set_angle(CHEVRON_OPEN_DEG if open_ else 0.0)

    def hideEvent(self, event) -> None:
        motion.stop(self._anim)
        super().hideEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(self.SIZE, self.SIZE)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self.isEnabled() and (self.isDown() or self.underMouse()):
            token = ("SubtleFillColorTertiary" if self.isDown()
                     else "SubtleFillColorSecondary")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.T(token)))
            painter.drawRoundedRect(QRectF(self.rect()), RADII["control"],
                                    RADII["control"])
        colour = theme.T(_TOKEN_TEXT if self.isEnabled() else _TOKEN_TEXT_OFF)
        pixmap = glyph_pixmap(_GLYPH_CHEVRON, self.GLYPH, colour,
                              self.devicePixelRatioF())
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        painter.rotate(self._angle)
        painter.drawPixmap(QPoint(-self.GLYPH // 2, -self.GLYPH // 2), pixmap)
        painter.end()

    def refresh_theme(self) -> None:
        self.update()


class SettingsExpander(ThemeAware, QFrame):
    """A settings card that opens to reveal child rows.

    Header padding is `16,16,4,16` and each child row is inset `58,8,44,8` — the
    58 px left indent is what lines a child row's text up with the header's,
    which is the visual promise the control makes. Every child row carries a
    1 px rule along its top edge (`SettingsExpanderItemBorderThickness 0,1,0,0`),
    and the header loses its bottom radius while the expander is open so the two
    boxes read as one.
    """

    #: Emitted with the new state whenever the expander opens or closes.
    expanded_changed = Signal(bool)

    def __init__(self,
                 title: str = "",
                 parent: QWidget | None = None,
                 *,
                 description: str = "",
                 icon_key: str | None = None,
                 content: QWidget | None = None,
                 expanded: bool = False) -> None:
        """
        Args:
            title: The header's primary line. From `strings.py`.
            description: The header's Caption 12 second line.
            icon_key: A key of `icons.GLYPHS` for the 20 px header icon.
            content: A control on the header's trailing edge, beside the chevron.
            expanded: Start open.
        """
        super().__init__(parent)
        self._expanded = False
        self._anim = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self._header = _ExpanderHeader(title, self, description=description,
                                       icon_key=icon_key, content=content)
        self._header.clicked.connect(self.toggle)
        column.addWidget(self._header)

        self._chevron = _ChevronButton(self._header)
        self._chevron.clicked.connect(self.toggle)
        self._header.attach_chevron(self._chevron)

        body = QFrame(self)
        body.setObjectName(OBJ.CARD_SECONDARY)
        body_column = QVBoxLayout(body)
        body_column.setContentsMargins(0, 0, 0, 0)
        body_column.setSpacing(0)
        body.setMinimumHeight(0)
        self._body = body
        self._body_column = body_column
        column.addWidget(body)

        self._rows: list[QWidget] = []
        self._apply_expanded(expanded, animate=False)
        self._install_theme_hook()

    # ── header passthrough ───────────────────────────────────────────────
    def header(self) -> "SettingsCard":
        """The header card, for a caller that wants to reach its content."""
        return self._header

    def title(self) -> str:
        return self._header.title()

    def set_title(self, text: str) -> None:
        self._header.set_title(text)

    def description(self) -> str:
        return self._header.description()

    def set_description(self, text: str) -> None:
        self._header.set_description(text)

    def set_icon(self, key: str | None) -> None:
        self._header.set_icon(key)

    def content(self) -> QWidget | None:
        return self._header.content()

    def set_content(self, widget: QWidget | None) -> None:
        self._header.set_content(widget)

    def chevron(self) -> _ChevronButton:
        """The rotating chevron button, exposed for tests."""
        return self._chevron

    # ── child rows ───────────────────────────────────────────────────────
    def add_row(self, widget: QWidget) -> QWidget:
        """Add a child row, inset to the toolkit's `58,8,44,8`.

        Args:
            widget: Any widget. A :class:`SettingsCard` is un-boxed on the way
                in — the row is already inside the expander's box, and a second
                one reads as a card inside a card — and loses its 16 px padding,
                because the row's own `58,8,44,8` inset replaces it.

        Returns:
            The wrapper the row was placed in, so a caller can hide one row.
        """
        left, top, right, bottom = EXPANDER_CHILD_PAD
        if isinstance(widget, SettingsCard):
            widget.set_boxed(False)
            widget.grid_layout().setContentsMargins(0, 0, 0, 0)
        wrapper = _ExpanderRow(self._body)
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(left, top, right, bottom)
        row.setSpacing(SPACING["m"])
        widget.setParent(wrapper)
        row.addWidget(widget)
        self._body_column.addWidget(wrapper)
        self._rows.append(wrapper)
        wrapper.set_last(True)
        for previous in self._rows[:-1]:
            previous.set_last(False)
        return wrapper

    def rows(self) -> tuple[QWidget, ...]:
        """Every child row wrapper, in order."""
        return tuple(self._rows)

    def body(self) -> QFrame:
        """The container the child rows live in."""
        return self._body

    # ── open / close ─────────────────────────────────────────────────────
    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, *, animate: bool = True) -> None:
        """Open or close, animating the body's height.

        Args:
            expanded: The new state.
            animate: Land immediately instead of animating. Use this when the
                UI is reflecting a stored fact — restoring a settings page
                should not look like the user opening forty expanders.
        """
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._apply_expanded(expanded, animate=animate)

    def toggle(self) -> None:
        """Flip the open state."""
        self.set_expanded(not self._expanded)

    def _apply_expanded(self, expanded: bool, *, animate: bool) -> None:
        self._expanded = expanded
        self._chevron.setChecked(expanded)
        self._header.set_open(expanded)
        if not animate:
            self._chevron.land_at(expanded)
            self._body.setVisible(expanded)
            self._on_animation_finished()
            self.expanded_changed.emit(expanded)
            return
        self._chevron.animate_to(expanded)
        # `maximumHeight` rests at 0 (closed) or UNBOUNDED (open), and animating
        # from UNBOUNDED would start the collapse 16 million pixels away — so
        # each direction is given an explicit, finite start.
        self._body.setVisible(True)
        if expanded:
            start = 0
            end = max(EXPANDER_CONTENT_MIN_H, self._body.sizeHint().height())
        else:
            start = max(EXPANDER_CONTENT_MIN_H, self._body.height())
            end = 0
        self._body.setMaximumHeight(start)
        self._anim = motion.animate(
            self._body, b"maximumHeight", end, start=start, duration="normal",
            easing=motion.CURVE_IN if expanded else motion.CURVE_OUT,
            parent=self._body, on_finished=self._on_animation_finished,
        )
        if motion.DUR("normal") == 0:
            # A gated animation applies its end value but only notifies on the
            # next event-loop turn; land the clamp now so the layout is right
            # before this call returns.
            self._on_animation_finished()
        self.expanded_changed.emit(expanded)

    def _on_animation_finished(self) -> None:
        """Release the height clamp so a row added later can still grow."""
        if self._expanded:
            self._body.setMaximumHeight(UNBOUNDED)
        else:
            self._body.setMaximumHeight(0)
            self._body.setVisible(False)

    def hideEvent(self, event) -> None:
        motion.stop(self._anim)
        super().hideEvent(event)

    def refresh_theme(self) -> None:
        self._header.refresh_theme()
        self._chevron.refresh_theme()
        for row in self._rows:
            row.refresh_theme()
        qss.repolish(self._body)
        self.update()


class _ExpanderHeader(SettingsCard):
    """The expander's header: a clickable card that squares off when open."""

    def __init__(self,
                 title: str = "",
                 parent: QWidget | None = None,
                 *,
                 description: str = "",
                 icon_key: str | None = None,
                 content: QWidget | None = None) -> None:
        super().__init__(title, parent, description=description,
                         icon_key=icon_key, content=content, clickable=True,
                         action_icon=False)
        left, top, right, bottom = EXPANDER_HEADER_PAD
        self._grid.setContentsMargins(left, top, right, bottom)
        self._open = False

    def attach_chevron(self, chevron: _ChevronButton) -> None:
        """Put the rotating chevron button on the header's trailing edge.

        Column 4, which :class:`SettingsCard` deliberately leaves empty, so the
        button never shares a cell with the static action glyph.
        """
        self._grid.addWidget(chevron, 0, 4, Qt.AlignmentFlag.AlignVCenter)

    def set_open(self, open_: bool) -> None:
        self._open = bool(open_)
        self.update()

    def is_open(self) -> bool:
        return self._open

    def corner_radii(self) -> tuple[float, float, float, float]:
        """Square the bottom corners while the expander is open."""
        radius = float(RADII["control"])
        if self._open:
            return radius, radius, 0.0, 0.0
        return radius, radius, radius, radius


class _ExpanderRow(ThemeAware, QFrame):
    """One child row: a 1 px rule along its top edge, square but for the last."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last = False
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._install_theme_hook()

    def set_last(self, last: bool) -> None:
        """The last row rounds its bottom corners; the others are square."""
        self._last = bool(last)
        self.update()

    def is_last(self) -> bool:
        return self._last

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(RADII["control"]) if self._last else 0.0
        path = box_path(rect, 0.0, 0.0, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.T(_TOKEN_CARD_FILL_2)))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(theme.T(_TOKEN_DIVIDER)), 1.0))
        painter.drawLine(QPoint(0, int(rect.top())),
                         QPoint(self.width(), int(rect.top())))
        painter.end()

    def refresh_theme(self) -> None:
        self.update()


# ═════════════════════════════════════════════════════════════════════════════
# InfoBar
# ═════════════════════════════════════════════════════════════════════════════

class InfoBarSeverity(StrEnum):
    """WinUI's four `InfoBar` severities."""

    INFORMATIONAL = "informational"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


#: severity -> the object name whose frozen banner rule paints it.
_SEVERITY_OBJECT: dict[InfoBarSeverity, str] = {
    InfoBarSeverity.INFORMATIONAL: OBJ.BANNER_INFO,
    InfoBarSeverity.SUCCESS: OBJ.BANNER_SUCCESS,
    InfoBarSeverity.WARNING: OBJ.BANNER_CAUTION,
    InfoBarSeverity.ERROR: OBJ.BANNER_CRITICAL,
}

#: severity -> its leading glyph, from the frozen registry.
_SEVERITY_GLYPH: dict[InfoBarSeverity, str] = {
    InfoBarSeverity.INFORMATIONAL: _GLYPH_INFO,
    InfoBarSeverity.SUCCESS: _GLYPH_SUCCESS,
    InfoBarSeverity.WARNING: _GLYPH_WARNING,
    InfoBarSeverity.ERROR: _GLYPH_ERROR,
}

#: severity -> the token its glyph is tinted with. "" means the live accent,
#: which is what `SystemFillColorAttentionBrush` resolves to.
_SEVERITY_TOKEN: dict[InfoBarSeverity, str] = {
    InfoBarSeverity.INFORMATIONAL: "",
    InfoBarSeverity.SUCCESS: "SystemFillColorSuccess",
    InfoBarSeverity.WARNING: "SystemFillColorCaution",
    InfoBarSeverity.ERROR: "SystemFillColorCritical",
}

#: The one mapping from a sync issue's severity onto an InfoBar's, so WP-12 and
#: WP-13 cannot invent two that disagree.
SEVERITY_FOR_ISSUE: dict[IssueSeverity, InfoBarSeverity] = {
    IssueSeverity.BLOCKING: InfoBarSeverity.ERROR,
    IssueSeverity.ERROR: InfoBarSeverity.ERROR,
    IssueSeverity.WARNING: InfoBarSeverity.WARNING,
    IssueSeverity.INFO: InfoBarSeverity.INFORMATIONAL,
}

for _table in (_SEVERITY_OBJECT, _SEVERITY_GLYPH, _SEVERITY_TOKEN):
    _missing = [member.name for member in InfoBarSeverity if member not in _table]
    if _missing:                                       # pragma: no cover
        raise ValueError(
            f"containers: a severity table is missing {_missing}; every one of "
            "InfoBarSeverity needs an object name, a glyph and a tint"
        )
_missing = [member.name for member in IssueSeverity if member not in SEVERITY_FOR_ISSUE]
if _missing:                                           # pragma: no cover
    raise ValueError(f"containers: SEVERITY_FOR_ISSUE is missing {_missing}")
_bad = [key for key in _SEVERITY_GLYPH.values() if key not in icons.GLYPHS]
if _bad:                                               # pragma: no cover
    raise ValueError(f"containers: a severity names unknown glyph keys {_bad}")
_bad = [token for token in _SEVERITY_TOKEN.values() if token and token not in theme.TOKENS]
if _bad:                                               # pragma: no cover
    raise ValueError(f"containers: a severity names unknown theme tokens {_bad}")
del _table, _missing, _bad


class InfoBar(ThemeAware, QFrame):
    """The Fluent info bar, in all four severities.

    The box comes from the frozen `#BannerInfo` / `#BannerSuccess` /
    `#BannerCaution` / `#BannerCritical` rules, which already carry the right
    background, border, radius and 12 px padding. QSS padding on a container
    moves its layout — a styled `QFrame` reports `contentsMargins() ==
    (13,13,13,13)` for `padding: 12px; border: 1px` — so the layout here is set
    to zero margins and the sheet supplies the inset exactly once.
    """

    #: Emitted when the close button is clicked. The caller decides whether to
    #: hide, delete, or record a dismissal.
    closed = Signal()

    def __init__(self,
                 title: str = "",
                 message: str = "",
                 parent: QWidget | None = None,
                 *,
                 severity: InfoBarSeverity = InfoBarSeverity.INFORMATIONAL,
                 closable: bool = True,
                 close_tooltip: str = "") -> None:
        """
        Args:
            title: The Body Strong headline. From `strings.py`.
            message: The Body 14 detail line, which wraps.
            severity: One of :class:`InfoBarSeverity`.
            closable: Show the trailing close button.
            close_tooltip: Supplied by the caller from `strings.py`; never
                defaulted to a literal here.
        """
        super().__init__(parent)
        self._severity = InfoBarSeverity(severity)
        self._actions: list[FluentButton] = []
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        row = QHBoxLayout(self)
        # ZERO. The banner rule's own `padding: 12px` is the inset, and it is
        # already reflected in this widget's contentsMargins.
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING["m"])
        self._row = row

        self._icon = _GlyphLabel(self, key=_SEVERITY_GLYPH[self._severity],
                                 size=SPACING["l"])
        self._icon.setFixedWidth(SPACING["l"])
        row.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QWidget(self)
        text_column = QVBoxLayout(text)
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(SPACING["xxs"])
        self._title = _label(title, text, role=_ROLE_STRONG)
        self._title.setVisible(bool(title))
        self._message = QLabel(message, text)
        qss.set_property(self._message, PROP.TYPE, _ROLE_TITLE)
        self._message.setWordWrap(True)
        self._message.setVisible(bool(message))
        text_column.addWidget(self._title)
        text_column.addWidget(self._message)
        row.addWidget(text, 1)

        self._actions_holder = QWidget(self)
        actions_row = QHBoxLayout(self._actions_holder)
        actions_row.setContentsMargins(0, 0, 0, 0)
        actions_row.setSpacing(SPACING["s"])
        self._actions_row = actions_row
        self._actions_holder.setVisible(False)
        row.addWidget(self._actions_holder, 0, Qt.AlignmentFlag.AlignTop)

        self._close = icon_button(_GLYPH_CLOSE, self, tooltip=close_tooltip)
        self._close.clicked.connect(self.closed.emit)
        self._close.setVisible(bool(closable))
        row.addWidget(self._close, 0, Qt.AlignmentFlag.AlignTop)

        self.set_severity(self._severity)
        self._install_theme_hook()

    # ── content ──────────────────────────────────────────────────────────
    def severity(self) -> InfoBarSeverity:
        return self._severity

    def set_severity(self, severity: InfoBarSeverity) -> None:
        """Switch severity: the object name, the glyph and its tint all move."""
        self._severity = InfoBarSeverity(severity)
        qss.set_object_name(self, _SEVERITY_OBJECT[self._severity])
        token = _SEVERITY_TOKEN[self._severity]
        self._icon.set_glyph(_SEVERITY_GLYPH[self._severity])
        self._icon.set_tint(token or None, accent=not token)

    def title(self) -> str:
        return self._title.text()

    def set_title(self, text: str) -> None:
        self._title.setText(text)
        self._title.setVisible(bool(text))

    def message(self) -> str:
        return self._message.text()

    def set_message(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    def is_closable(self) -> bool:
        return self._close.isVisible()

    def set_closable(self, closable: bool) -> None:
        self._close.setVisible(bool(closable))

    def close_button(self) -> FluentButton:
        return self._close

    # ── actions ──────────────────────────────────────────────────────────
    def add_action(self, text: str, *, accent: bool = False) -> FluentButton:
        """Add a trailing action button and return it, unconnected.

        Args:
            text: The label. From `strings.py`.
            accent: Render as the filled primary button.

        Returns:
            The new :class:`FluentButton`, for the caller to connect.
        """
        variant = ButtonVariant.ACCENT if accent else ButtonVariant.STANDARD
        button = FluentButton(text, self._actions_holder, variant=variant)
        self._actions_row.addWidget(button)
        self._actions.append(button)
        self._actions_holder.setVisible(True)
        return button

    def actions_buttons(self) -> tuple[FluentButton, ...]:
        """Every action button added so far, in order."""
        return tuple(self._actions)

    def clear_actions(self) -> None:
        """Remove every action button."""
        for button in self._actions:
            self._actions_row.removeWidget(button)
            button.setParent(None)
            button.deleteLater()
        self._actions.clear()
        self._actions_holder.setVisible(False)

    def refresh_theme(self) -> None:
        self._icon.refresh_theme()
        qss.repolish(self, deep=True)
        self.update()


# ═════════════════════════════════════════════════════════════════════════════
# ContentDialog
# ═════════════════════════════════════════════════════════════════════════════

class ContentDialog(QDialog):
    """A Fluent modal dialog with a **reserved shadow margin**.

    `QGraphicsDropShadowEffect` paints inside the widget it is attached to, so a
    dialog that puts its surface flush against the window edge simply has no
    visible elevation — the blur is clipped away. The layout here therefore
    reserves :func:`shadow_margins` around an inner `#DialogSurface` frame, and
    the shadow goes on that frame rather than on the dialog, which also leaves
    the dialog itself free to carry an opacity effect (`QGraphicsEffect` is
    exclusive: one per widget).

    `Qt.NoDropShadowWindowHint` stops the compositor adding a second shadow of
    its own, and the surface is a normal child widget, so its `border-radius`
    clips correctly — the Wayland square-corner problem only affects a top-level
    translucent window painting its own rounded body.
    """

    #: Emitted when the primary / secondary button is clicked, before the
    #: dialog's own accept()/reject() runs.
    primary_clicked = Signal()
    secondary_clicked = Signal()

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 title: str = "",
                 body: str = "",
                 shadow: str = "dialog") -> None:
        """
        Args:
            title: The Subtitle 20/28 headline. From `strings.py`.
            body: The Body 14 message, which wraps.
            shadow: A key of `theme.SHADOWS`; "dialog" is Fluent's shadow64.

        Raises:
            KeyError: for an unknown shadow name.
        """
        super().__init__(parent)
        self._shadow_name = shadow
        self._margins = shadow_margins(shadow)
        self._primary: FluentButton | None = None
        self._secondary: FluentButton | None = None
        self._close: FluentButton | None = None
        self._content: QWidget | None = None

        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(self._margins)
        outer.setSpacing(0)

        surface = QFrame(self)
        surface.setObjectName(OBJ.DIALOG_SURFACE)
        surface.setMinimumWidth(DIALOG_MIN_W)
        surface.setMaximumWidth(DIALOG_MAX_W)
        surface.setMinimumHeight(DIALOG_MIN_H)
        surface.setMaximumHeight(DIALOG_MAX_H)
        outer.addWidget(surface)
        self._surface = surface
        self._effect = drop_shadow(surface, shadow)

        column = QVBoxLayout(surface)
        column.setContentsMargins(DIALOG_PAD, DIALOG_PAD, DIALOG_PAD, DIALOG_PAD)
        column.setSpacing(SPACING["m"])
        self._column = column

        self._title = _label(title, surface, role=_ROLE_SUBTITLE)
        self._title.setVisible(bool(title))
        column.addWidget(self._title)

        self._body = QLabel(body, surface)
        qss.set_property(self._body, PROP.TYPE, _ROLE_TITLE)
        self._body.setWordWrap(True)
        self._body.setVisible(bool(body))
        column.addWidget(self._body)

        # The slot is ALWAYS in the layout and always carries the stretch, so
        # the button row stays pinned to the bottom edge whether or not the
        # dialog was given a custom widget. A hidden widget is dropped from a
        # QBoxLayout entirely — stretch factor included — which is why this is
        # an empty stretching container rather than a `hide()`.
        self._slot = QWidget(surface)
        slot_column = QVBoxLayout(self._slot)
        slot_column.setContentsMargins(0, 0, 0, 0)
        slot_column.setSpacing(SPACING["s"])
        slot_column.addStretch(1)
        self._slot_column = slot_column
        self._slot.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Expanding)
        column.addWidget(self._slot, 1)

        buttons = QWidget(surface)
        button_row = QHBoxLayout(buttons)
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(SPACING["s"])
        self._buttons = buttons
        self._button_row = button_row
        buttons.setVisible(False)
        column.addWidget(buttons)

    # ── geometry ─────────────────────────────────────────────────────────
    def surface(self) -> QFrame:
        """The rounded, shadowed body. Everything visible lives inside it."""
        return self._surface

    def shadow_effect(self) -> QGraphicsDropShadowEffect:
        """The drop shadow attached to :meth:`surface`."""
        return self._effect

    def reserved_margins(self) -> QMargins:
        """The margin reserved around the surface so the shadow is not clipped."""
        return QMargins(self._margins)

    def shadow_name(self) -> str:
        return self._shadow_name

    # ── content ──────────────────────────────────────────────────────────
    def title(self) -> str:
        return self._title.text()

    def set_title(self, text: str) -> None:
        self._title.setText(text)
        self._title.setVisible(bool(text))

    def body(self) -> str:
        return self._body.text()

    def set_body(self, text: str) -> None:
        self._body.setText(text)
        self._body.setVisible(bool(text))

    def content(self) -> QWidget | None:
        """The custom widget between the body and the buttons, if any."""
        return self._content

    def set_content(self, widget: QWidget | None) -> None:
        """Put a custom widget below the message — a form, a list, a tree."""
        if self._content is not None:
            self._slot_column.removeWidget(self._content)
            self._content.setParent(None)
        self._content = widget
        if widget is not None:
            widget.setParent(self._slot)
            self._slot_column.insertWidget(0, widget)   # before the trailing stretch

    # ── buttons ──────────────────────────────────────────────────────────
    def set_buttons(self,
                    primary: str = "",
                    secondary: str = "",
                    close: str = "") -> tuple[FluentButton | None, ...]:
        """Build the dialog's button row, left to right.

        Every button is 32 px tall and between 130 and 202 px wide, which is
        WinUI's `ContentDialog` button metric. The primary button accepts the
        dialog; the secondary and close buttons reject it.

        Args:
            primary: The accent button's label, or "" for none.
            secondary: The standard button's label, or "" for none.
            close: The trailing "close" button's label, or "" for none.

        Returns:
            `(primary, secondary, close)`, each a `FluentButton` or None.
        """
        for existing in (self._primary, self._secondary, self._close):
            if existing is not None:
                self._button_row.removeWidget(existing)
                existing.setParent(None)
                existing.deleteLater()
        self._primary = self._secondary = self._close = None

        self._button_row.addStretch(1)
        if primary:
            self._primary = self._make_button(primary, accent=True)
            self._primary.clicked.connect(self.primary_clicked.emit)
            self._primary.clicked.connect(self.accept)
        if secondary:
            self._secondary = self._make_button(secondary)
            self._secondary.clicked.connect(self.secondary_clicked.emit)
            self._secondary.clicked.connect(self.reject)
        if close:
            self._close = self._make_button(close)
            self._close.clicked.connect(self.reject)
        self._buttons.setVisible(
            any(b is not None for b in (self._primary, self._secondary, self._close))
        )
        return self._primary, self._secondary, self._close

    def _make_button(self, text: str, *, accent: bool = False) -> FluentButton:
        variant = ButtonVariant.ACCENT if accent else ButtonVariant.STANDARD
        button = FluentButton(text, self._buttons, variant=variant)
        button.setMinimumWidth(DIALOG_BUTTON_MIN_W)
        button.setMaximumWidth(DIALOG_BUTTON_MAX_W)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._button_row.addWidget(button)
        return button

    def primary_button(self) -> FluentButton | None:
        return self._primary

    def secondary_button(self) -> FluentButton | None:
        return self._secondary

    def close_button(self) -> FluentButton | None:
        return self._close

    def buttons(self) -> tuple[FluentButton, ...]:
        """Every button currently in the row, left to right."""
        return tuple(b for b in (self._primary, self._secondary, self._close)
                     if b is not None)

    def showEvent(self, event) -> None:
        """Grow to the layout's hint, which `adjustSize()` will not always do.

        `QWidget.show()` sizes a never-resized top-level with `adjustSize()`,
        which clamps to two thirds of the screen. The reserved shadow margin
        makes this window far wider than its visible surface, so on a small
        screen that clamp squeezes the buttons below their WinUI minimum. Only
        ever grows — a caller that resized the dialog itself keeps its size.
        """
        super().showEvent(event)
        hint = self.sizeHint()
        if self.width() < hint.width() or self.height() < hint.height():
            self.resize(self.size().expandedTo(hint))

    def refresh_theme(self) -> None:
        """Re-polish the surface and re-attach a shadow at the new alpha."""
        self._effect = drop_shadow(self._surface, self._shadow_name)
        qss.repolish(self._surface, deep=True)
        self.update()


def card_group(cards: Iterable[QWidget],
               parent: QWidget | None = None,
               *,
               heading: str = "") -> QWidget:
    """Stack settings cards into a Fluent group.

    Cards stack with a 4 px gap; the group's heading is Body Strong with 24 px
    above and 8 px below. Fluent groups are separated by whitespace, never by
    rules, so there is deliberately no divider here.

    Args:
        cards: The cards, in order.
        heading: The group heading. From `strings.py`; "" for no heading.

    Returns:
        A container widget holding the heading and the cards.
    """
    group = QWidget(parent)
    column = QVBoxLayout(group)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(CARD_GROUP_GAP)
    if heading:
        column.addWidget(SectionHeading(heading, group))
    for card in cards:
        card.setParent(group)
        column.addWidget(card)
    return group


__all__ = [
    "CARD_MIN_H", "CARD_PAD", "CARD_ICON", "CARD_ICON_GAP",
    "CARD_CONTENT_MIN_W", "CARD_WRAP_THRESHOLD", "CARD_WRAP_NO_ICON_THRESHOLD",
    "CARD_WRAP_SPACING", "CARD_CONTENT_GAP", "CARD_ACTION_ICON",
    "CARD_ACTION_GAP", "CARD_GROUP_GAP", "CARD_DISABLED_ICON_OPACITY",
    "EXPANDER_HEADER_PAD", "EXPANDER_CHILD_PAD", "EXPANDER_CHEVRON",
    "EXPANDER_CONTENT_MIN_H", "CHEVRON_GLYPH", "CHEVRON_OPEN_DEG",
    "DIALOG_MIN_W", "DIALOG_MAX_W", "DIALOG_MIN_H", "DIALOG_MAX_H",
    "DIALOG_BUTTON_MIN_W", "DIALOG_BUTTON_MAX_W", "DIALOG_PAD", "UNBOUNDED",
    "InfoBarSeverity", "SEVERITY_FOR_ISSUE",
    "SectionHeading", "SettingsCard", "SettingsExpander", "InfoBar",
    "ContentDialog",
    "box_path", "glyph_pixmap", "drop_shadow", "shadow_margins", "card_group",
]
