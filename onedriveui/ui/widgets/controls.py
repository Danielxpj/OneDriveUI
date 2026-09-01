"""The Fluent input controls: buttons, the toggle switch, text and choice fields.

Everything geometric in here is Windows 11, not Windows 10. The trap worth
naming: `microsoft-ui-xaml@main` still ships the *legacy* Windows 10 toggle
template (44x20 track, 10 px knob, translate 24). The Windows 11 template lives
in `winui2/main : ToggleSwitch_themeresources.xaml` — 40x20 track, 12 px knob,
0 -> 20 travel, 14 px on hover, 17x14 on press, 83 ms with `KeySpline 0,0,0,1`.
Those are the numbers below, all read out of `theme.METRICS`.

No colour, no icon name and no user-facing string is written here. Colours come
from `theme.T()` / `theme.accent()`, icons from `icons.GLYPHS`, geometry from
`theme.METRICS` / `theme.RADII` / `theme.SPACING`, and every label is passed in
by the caller (WP-12/WP-13 read them out of `strings.py`).
"""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import (
    Property, QEvent, QPointF, QRectF, QSize, QSizeF, Qt, Signal,
)
from PySide6.QtGui import (
    QColor, QPainter, QPaintEvent, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton, QCheckBox, QComboBox, QLineEdit, QProxyStyle, QPushButton,
    QRadioButton, QStyle, QStyleOption, QWidget,
)

from onedriveui.bus import BUS
from onedriveui.ui import icons, motion, qss, theme
from onedriveui.ui.theme import METRICS, OBJ, PROP, RADII, SPACING

# NOTE ON FONTS. No control here calls `setFont()`. The family is set once, on
# QApplication, by `fonts.apply_app_font()`; the size and weight come from the
# stylesheet's type-ramp rules. Setting a font per widget also makes
# QStyleSheetStyle record it as that widget's "custom font" and restore it on
# every repolish, which then defeats any later `setFont()` a caller makes.

# ═════════════════════════════════════════════════════════════════════════════
# Shared painting helpers
# ═════════════════════════════════════════════════════════════════════════════

def lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    """Interpolate two colours, **alpha included**.

    An RGB-only lerp from a transparent fill to an opaque one flashes black
    halfway through, because the transparent colour's RGB is (0, 0, 0).
    """
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def glyph_pixmap(key: str,
                 size: int,
                 colour: str | None,
                 dpr: float) -> QPixmap:
    """One glyph from the frozen registry, rendered for a `dpr` surface.

    `QIcon.pixmap(QSize)` alone hands back a device-pixel-ratio-1 pixmap, which
    `drawPixmap` then MAGNIFIES on a 1.25x, 1.5x or 2x screen — a visibly soft
    glyph next to crisp text. `icons.icon()` registers a 1x and a 2x raster for
    exactly this reason, and the `devicePixelRatio` overload is what picks the
    right one and tags the result so it draws at its logical size.

    Args:
        key: A key of `icons.GLYPHS`.
        size: A native glyph size from `icons.GLYPH_SIZES`.
        colour: `#RRGGBB` from `theme`, or None for the primary text colour.
        dpr: The target surface's `devicePixelRatioF()`.

    Returns:
        A `QPixmap` tagged with `dpr`.
    """
    icon = icons.icon(key, size, color=colour)
    return icon.pixmap(QSize(size, size), float(dpr))


def paint_focus_ring(painter: QPainter,
                     rect: QRectF,
                     radius: float,
                     *,
                     dark: bool | None = None) -> None:
    """Draw the Windows 11 two-tone focus ring around `rect`.

    A 2 px outer stroke and a 1 px inner stroke, inflated `focus_inflate` px
    outside the control, with a ring radius of `radius + focus_inflate`. It
    carries **no accent colour** — Windows 11 has no accent focus ring for
    standard controls; the accent underline on a focused text field is a
    different thing entirely.

    Args:
        painter: An antialiased painter in the widget's logical coordinates.
        rect: The control's own bounds. The ring is drawn outside them.
        radius: The control's corner radius.
        dark: Force a theme; None asks the live one.
    """
    inflate = float(METRICS["focus_inflate"])
    outer_w = float(METRICS["focus_outer"])
    inner_w = float(METRICS["focus_inner"])
    ring_radius = radius + inflate

    outer = QColor(theme.T("FocusStrokeColorOuter", dark=dark))
    inner = QColor(theme.T("FocusStrokeColorInner", dark=dark))

    box = rect.adjusted(-inflate, -inflate, inflate, inflate)
    painter.save()
    painter.setBrush(Qt.BrushStyle.NoBrush)

    half = outer_w / 2.0
    painter.setPen(QPen(outer, outer_w))
    painter.drawRoundedRect(box.adjusted(half, half, -half, -half),
                            ring_radius - half, ring_radius - half)

    inset = outer_w + inner_w / 2.0
    painter.setPen(QPen(inner, inner_w))
    painter.drawRoundedRect(box.adjusted(inset, inset, -inset, -inset),
                            ring_radius - inset, ring_radius - inset)
    painter.restore()


def focus_ring_bounds(rect: QRectF) -> QRectF:
    """Inset `rect` so :func:`paint_focus_ring` lands the ring *inside* it.

    The ring is inflated `focus_inflate` px outside whatever bounds it is given,
    and Qt clips a paint event to the widget's own rect — so handing a widget's
    full rect straight to :func:`paint_focus_ring` throws the entire ring away
    except for four corner slivers. Every caller that has no margin to spend
    (which is all of them: a Fluent control is exactly 32 px, not 38) insets
    first, so the ring is drawn just inside the control's edge.

    Args:
        rect: The control's own bounds.

    Returns:
        The bounds to pass to :func:`paint_focus_ring`.
    """
    inflate = float(METRICS["focus_inflate"])
    return rect.adjusted(inflate, inflate, -inflate, -inflate)


class FocusRingStyle(QProxyStyle):
    """A proxy style that replaces Qt's focus rectangle with the Fluent ring.

    Install it before the stylesheet:
    `app.setStyle(FocusRingStyle())` then `qss.apply(app)`. `QStyleSheetStyle`
    wraps whatever style is set and still delegates `PE_FrameFocusRect` down to
    this proxy (verified), so every focusable Qt control picks the ring up
    without knowing about it.

    **`option.rect` is not the control.** For a `QPushButton` Qt passes
    `SE_PushButtonFocusRect` — the CONTENT box, inside the padding: measured,
    `QRect(12, 6, 154, 20)` on a 178x32 button. Ringing that draws a 2 px stroke
    around the *label*, crossing the glyphs, instead of around the 32 px control.
    A check box and a radio button are the same story. So for a button-shaped
    widget the ring is drawn on the widget's own bounds; for everything else
    (an item view's current-item rect, most of all) `option.rect` is the right
    answer and is used unchanged.
    """

    def __init__(self, base: str | QStyle | None = None) -> None:
        super().__init__(base if base is not None else "Fusion")

    @staticmethod
    def ring_target(option: QStyleOption, widget: QWidget | None) -> QRectF:
        """-> the bounds the ring should hug, in the painter's coordinates.

        Args:
            option: The style option Qt is drawing with.
            widget: The widget being drawn, when Qt supplied one.
        """
        rect = QRectF(option.rect)
        if not isinstance(widget, QAbstractButton):
            return rect
        # A delegate can draw a button primitive into a cell; only trust the
        # widget's rect when Qt is really drawing that widget in its own space.
        own = QRectF(widget.rect())
        if own.contains(rect):
            return own
        return rect

    def drawPrimitive(self,
                      element: QStyle.PrimitiveElement,
                      option: QStyleOption,
                      painter: QPainter,
                      widget: QWidget | None = None) -> None:
        """Draw the two-tone ring for `PE_FrameFocusRect`, else defer."""
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            rect = focus_ring_bounds(self.ring_target(option, widget))
            if rect.width() > 0 and rect.height() > 0:
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                paint_focus_ring(painter, rect, float(RADII["control"]))
                painter.restore()
            return
        super().drawPrimitive(element, option, painter, widget)


def restyle(widget: QWidget, *, deep: bool = True) -> None:
    """Re-polish `widget` and let every custom-painted descendant re-read tokens.

    Call after a theme change, or after any `setProperty()` that a QSS rule keys
    off. A custom `paintEvent` reads `theme.T()` live, so it only needs an
    `update()`; the Qt-drawn parts need the unpolish/polish pair.
    """
    qss.repolish(widget, deep=deep)
    targets: list[QWidget] = [widget]
    if deep:
        targets.extend(widget.findChildren(QWidget))
    for target in targets:
        refresh = getattr(target, "refresh_theme", None)
        if callable(refresh):
            refresh()


class ThemeAware:
    """Mixin: repaint on `BUS.theme_changed`, and expose `refresh_theme()`.

    Custom-painted widgets hold no cached colours — they resolve tokens inside
    `paintEvent` — so reacting to a theme change is exactly one `update()`.
    Qt-drawn widgets override `refresh_theme()` to repolish instead.

    The connection is made to a bound method of a QObject, which PySide holds
    weakly, so it does not keep a closed widget alive.
    """

    def _install_theme_hook(self) -> None:
        """Connect `BUS.theme_changed`. Call once, at the end of `__init__`."""
        BUS.theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, _dark: bool, _accent: str) -> None:
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self.update()  # type: ignore[attr-defined]


# ═════════════════════════════════════════════════════════════════════════════
# Buttons
# ═════════════════════════════════════════════════════════════════════════════

class ButtonVariant(StrEnum):
    """The four Windows 11 button styles.

    STANDARD carries the elevation bottom-stroke; ACCENT is the filled primary;
    SUBTLE is transparent until hovered; HYPERLINK is accent text with no fill.
    """

    STANDARD = "standard"
    ACCENT = "accent"
    SUBTLE = "subtle"
    HYPERLINK = "hyperlink"


#: variant -> the object name its QSS rules are scoped to ("" = the class rule).
_VARIANT_OBJECT: dict[ButtonVariant, str] = {
    ButtonVariant.STANDARD: "",
    ButtonVariant.ACCENT: "",
    ButtonVariant.SUBTLE: OBJ.SUBTLE_BUTTON,
    ButtonVariant.HYPERLINK: OBJ.LINK_BUTTON,
}


class FluentButton(ThemeAware, QPushButton):
    """A Windows 11 button: 32 px tall, radius 4, with the 1 px bottom stroke.

    The box is `padding: 5px 11px` + `min-height: 20px` + a 1 px border, which
    measures exactly `QSize(55, 32)` at the reference metrics. `min-height` is
    load-bearing — drop it and the box collapses onto the resolved face's line
    box.

    Focus draws the two-tone ring outside the control (`FocusRingStyle`); the
    border width stays at 1 px so focusing never changes the layout.
    """

    def __init__(self,
                 text: str = "",
                 parent: QWidget | None = None,
                 *,
                 variant: ButtonVariant = ButtonVariant.STANDARD,
                 icon_key: str | None = None,
                 icon_size: int = 16) -> None:
        """
        Args:
            text: The label. Callers pass a string from `strings.py`.
            variant: One of :class:`ButtonVariant`.
            icon_key: A key of `icons.GLYPHS`, drawn before the label.
            icon_size: A native glyph size from `icons.GLYPH_SIZES`.
        """
        super().__init__(text, parent)
        self._variant = ButtonVariant.STANDARD
        self._icon_only = False
        self._icon_key: str | None = None
        self._icon_size = icon_size
        # A Fluent button has no auto-default indicator; leaving Qt's on makes a
        # dialog's buttons a different size from the same button on a page.
        self.setAutoDefault(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if icon_key is not None:
            self.set_icon(icon_key, icon_size)
        self.set_variant(variant)
        self._install_theme_hook()

    # ── variant ──────────────────────────────────────────────────────────
    def variant(self) -> ButtonVariant:
        return self._variant

    def set_variant(self, variant: ButtonVariant) -> None:
        """Switch style. Sets the dynamic property AND repolishes — one without
        the other leaves the previously matched rule in place."""
        variant = ButtonVariant(variant)
        self._variant = variant
        self.setProperty(PROP.ACCENT, variant is ButtonVariant.ACCENT)
        name = _VARIANT_OBJECT[variant]
        if self._icon_only and variant is ButtonVariant.SUBTLE:
            name = OBJ.ICON_BUTTON
        self.setObjectName(name)
        qss.repolish(self)

    def set_icon_only(self, enabled: bool) -> None:
        """Square 32x32 icon button (subtle fill, no label)."""
        self._icon_only = bool(enabled)
        self.set_variant(self._variant)

    def set_icon(self, key: str, size: int = 16) -> None:
        """Set the leading glyph from the frozen icon registry.

        Raises:
            KeyError: for a key that is not in `icons.GLYPHS`.
            ValueError: for a size that is not a native glyph size.
        """
        self._icon_key = key
        self._icon_size = size
        self.setIcon(icons.icon(key, size))
        self.setIconSize(QSize(size, size))

    def refresh_theme(self) -> None:
        """Re-polish for the new theme's rules, and re-tint the glyph.

        `icons.icon(color=None)` resolves to the theme's primary text colour, so
        a QIcon built under the old theme is stale even though `icons` has
        already dropped its cache.
        """
        if self._icon_key is not None:
            self.setIcon(icons.icon(self._icon_key, self._icon_size))
        qss.repolish(self)


def icon_button(key: str,
                parent: QWidget | None = None,
                *,
                size: int = 16,
                tooltip: str = "") -> FluentButton:
    """A square 32x32 subtle button carrying one glyph.

    Args:
        key: A key of `icons.GLYPHS`.
        tooltip: Supplied by the caller from `strings.py`; never defaulted to a
            literal here.
    """
    button = FluentButton("", parent, variant=ButtonVariant.SUBTLE,
                          icon_key=key, icon_size=size)
    button.set_icon_only(True)
    if tooltip:
        button.setToolTip(tooltip)
    return button


# ═════════════════════════════════════════════════════════════════════════════
# Glyphs. Resolved once at import so a typo fails here, not at paint time.
# ═════════════════════════════════════════════════════════════════════════════

_GLYPH_SEARCH = "search"
_GLYPH_CHECK = "checkmark"
_GLYPH_CHEVRON = "chevron_down"
for _key in (_GLYPH_SEARCH, _GLYPH_CHECK, _GLYPH_CHEVRON):
    icons.glyph_stem(_key)          # raises KeyError on an unknown key
del _key


# ═════════════════════════════════════════════════════════════════════════════
# Text entry
# ═════════════════════════════════════════════════════════════════════════════

class FluentLineEdit(ThemeAware, QLineEdit):
    """A Windows 11 text field: 32 px tall, focused **and** unfocused.

    Focus swaps the bottom border for a 2 px accent underline — the Fluent
    focused text field, which is not the focus ring of the other controls. The
    grown border is paid back by dropping `padding-bottom` by one, so the
    control does not jump from 32 to 33 px the moment it is clicked.
    """

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 placeholder: str = "",
                 search: bool = False,
                 clear_button: bool = False) -> None:
        """
        Args:
            placeholder: Passed in by the caller from `strings.py`.
            search: Render as a search box — a leading glyph and the extra left
                padding the QSS reserves for it.
            clear_button: Qt's inline clear action.
        """
        super().__init__(parent)
        self._search = bool(search)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        if placeholder:
            self.setPlaceholderText(placeholder)
        if search:
            self.setObjectName(OBJ.SEARCH_BOX)
        self.setClearButtonEnabled(bool(clear_button))
        self._install_theme_hook()

    def is_search(self) -> bool:
        return self._search

    def refresh_theme(self) -> None:
        qss.repolish(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Qt paints the field; the search glyph is drawn on top of it."""
        super().paintEvent(event)
        if not self._search:
            return
        size = SPACING["l"]
        pixmap = glyph_pixmap(_GLYPH_SEARCH, size, None, self.devicePixelRatioF())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        x = METRICS["textbox_pad_l"] - SPACING["xxs"]
        y = (self.height() - size) // 2
        painter.setOpacity(1.0 if self.isEnabled() else 0.5)
        painter.drawPixmap(int(x), int(y), pixmap)
        painter.end()


# ═════════════════════════════════════════════════════════════════════════════
# Choice controls
# ═════════════════════════════════════════════════════════════════════════════

def indicator_rect(widget: QWidget) -> QRectF:
    """The 20x20 box a check/radio indicator occupies, leading-edge aligned.

    Computed rather than read back from the style because the QSS
    `::indicator` rule already pins the size, and a protected `initStyleOption`
    is not reachable from every Qt binding.
    """
    size = float(SPACING["xl"])
    y = (widget.height() - size) / 2.0
    if widget.layoutDirection() == Qt.LayoutDirection.RightToLeft:
        x = widget.width() - size
    else:
        x = 0.0
    return QRectF(x, y, size, size)


class FluentCheckBox(ThemeAware, QCheckBox):
    """A 20 px checkbox with an accent fill and a painted checkmark.

    QSS can fill and round the indicator but cannot put a glyph in it without an
    on-disk image, so the check and the indeterminate dash are painted here from
    the frozen glyph registry.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._install_theme_hook()

    def refresh_theme(self) -> None:
        qss.repolish(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        state = self.checkState()
        if state == Qt.CheckState.Unchecked:
            return
        box = indicator_rect(self)
        colour = QColor(
            theme.accent("text") if self.isEnabled()
            else theme.T("TextFillColorDisabled")
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if state == Qt.CheckState.Checked:
            size = SPACING["m"]
            pixmap = glyph_pixmap(_GLYPH_CHECK, size, colour.name(),
                                  self.devicePixelRatioF())
            painter.drawPixmap(
                int(box.center().x() - size / 2.0),
                int(box.center().y() - size / 2.0),
                pixmap,
            )
        else:
            dash = QRectF(0.0, 0.0, float(SPACING["s"]), float(SPACING["xxs"]))
            dash.moveCenter(box.center())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawRoundedRect(dash, RADII["progress_track"], RADII["progress_track"])
        painter.end()


class FluentRadioButton(ThemeAware, QRadioButton):
    """A 20 px radio with an accent ring and a painted centre dot."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._install_theme_hook()

    def refresh_theme(self) -> None:
        qss.repolish(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self.isChecked():
            return
        box = indicator_rect(self)
        colour = QColor(
            theme.accent("text") if self.isEnabled()
            else theme.T("TextFillColorDisabled")
        )
        radius = SPACING["s"] / 2.0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(box.center(), radius, radius)
        painter.end()


class FluentComboBox(ThemeAware, QComboBox):
    """A 32 px drop-down with the Fluent chevron.

    The chevron is painted rather than declared, because QSS `::down-arrow`
    needs an on-disk image and the glyph set is loaded through `icons`.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._install_theme_hook()

    def refresh_theme(self) -> None:
        qss.repolish(self)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        size = SPACING["m"]
        colour = QColor(
            theme.T("TextFillColorSecondary") if self.isEnabled()
            else theme.T("TextFillColorDisabled")
        )
        pixmap = glyph_pixmap(_GLYPH_CHEVRON, size, colour.name(),
                              self.devicePixelRatioF())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        x = self.width() - SPACING["xxl"] + (SPACING["xxl"] - size) // 2
        y = (self.height() - size) // 2
        painter.drawPixmap(int(x), int(y), pixmap)
        painter.end()


# ═════════════════════════════════════════════════════════════════════════════
# ToggleSwitch — the Windows 11 template
# ═════════════════════════════════════════════════════════════════════════════

class ToggleSwitch(ThemeAware, QAbstractButton):
    """The Windows 11 toggle switch.

    Geometry, all from `theme.METRICS`:

    ==================  ==========================================
    track               40 x 20, radius 10, 1 px stroke when off
    knob box            20 x 20, translating **0 -> 20**
    knob at rest        12 x 12
    knob hovered        14 x 14
    knob pressed        17 x 14   (the Fluent "stretch")
    transition          83 ms, `KeySpline 0,0,0,1`
    ==================  ==========================================

    The off and on tracks cross-fade by the knob's own progress, exactly as the
    XAML opacity animation does, and the colour lerp carries alpha so the
    transparent disabled fill never flashes black.
    """

    #: Emitted with the new state after a user toggle, for call sites that want
    #: a signal distinct from `QAbstractButton.toggled`.
    switched = Signal(bool)

    TRACK_W: int = METRICS["toggle_track_w"]
    TRACK_H: int = METRICS["toggle_track_h"]
    BOX: int = METRICS["toggle_knob_box"]
    TRAVEL: int = METRICS["toggle_travel"]
    KNOB: float = float(METRICS["toggle_knob"])
    KNOB_HOVER: float = float(METRICS["toggle_knob_hover"])
    KNOB_PRESS_W: float = float(METRICS["toggle_knob_press_w"])
    KNOB_PRESS_H: float = float(METRICS["toggle_knob_press_h"])
    #: The 1 px optical nudge WinUI applies to the knob margins, interpolated
    #: across the travel instead of snapped, so it does not jump mid-flight.
    NUDGE: float = 1.0
    #: Room reserved around the track for the focus ring, which Fluent draws
    #: OUTSIDE the control (`FocusVisualMargin="-7,-3,-7,-3"`). Qt clips a paint
    #: event to the widget, so without this margin the ring falls outside the
    #: clip entirely. The TRACK is still exactly `TRACK_W x TRACK_H`.
    FOCUS_PAD: int = METRICS["focus_inflate"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(self.TRACK_W + 2 * self.FOCUS_PAD,
                          self.TRACK_H + 2 * self.FOCUS_PAD)

        self._offset = 0.0
        self._knob_w = self.KNOB
        self._knob_h = self.KNOB
        self._hovered = False
        self._offset_anim = None
        self._width_anim = None
        self._height_anim = None

        self.toggled.connect(self._on_toggled)
        self.pressed.connect(self._on_pressed)
        self.released.connect(self._on_released)
        self._install_theme_hook()

    # ── animated properties ──────────────────────────────────────────────
    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = float(value)
        self.update()

    #: 0 .. TRAVEL, in logical px. The property NAME must match the byte string
    #: handed to QPropertyAnimation exactly or the animation silently no-ops.
    knobOffset = Property(float, _get_offset, _set_offset)

    def _get_knob_w(self) -> float:
        return self._knob_w

    def _set_knob_w(self, value: float) -> None:
        self._knob_w = float(value)
        self.update()

    knobWidth = Property(float, _get_knob_w, _set_knob_w)

    def _get_knob_h(self) -> float:
        return self._knob_h

    def _set_knob_h(self, value: float) -> None:
        self._knob_h = float(value)
        self.update()

    knobHeight = Property(float, _get_knob_h, _set_knob_h)

    # ── measurements, for layout and for tests ───────────────────────────
    def sizeHint(self) -> QSize:
        """The 40x20 track plus the focus-visual margin on every side.

        WinUI gives the toggle `FocusVisualMargin="-7,-3,-7,-3"`, i.e. the ring
        is drawn OUTSIDE the track and the control reserves the room for it. Qt
        clips a paint event to the widget, so a widget that is exactly the track
        has nowhere to put a ring: the whole thing lands outside the clip and a
        keyboard user sees four corner slivers. The widget therefore carries
        `FOCUS_PAD` on each side and the track is centred inside it — the track
        itself stays exactly 40x20, which is what :meth:`track_rect` returns.
        """
        return QSize(self.TRACK_W + 2 * self.FOCUS_PAD,
                     self.TRACK_H + 2 * self.FOCUS_PAD)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def knob_offset(self) -> float:
        """Current travel, 0 (off) to TRAVEL (on)."""
        return self._offset

    def knob_size(self) -> QSizeF:
        """Current knob size: 12x12 at rest, 14x14 hovered, 17x14 pressed."""
        return QSizeF(self._knob_w, self._knob_h)

    def track_origin(self) -> QPointF:
        """Top-left of the 40x20 track, centred in whatever size Qt gave us."""
        return QPointF(max(0.0, (self.width() - self.TRACK_W) / 2.0),
                       max(0.0, (self.height() - self.TRACK_H) / 2.0))

    def track_bounds(self) -> QRectF:
        """The track's full 40x20 box, in widget coordinates."""
        origin = self.track_origin()
        return QRectF(origin.x(), origin.y(),
                      float(self.TRACK_W), float(self.TRACK_H))

    def track_rect(self) -> QRectF:
        """The track, inset half a pixel so its 1 px stroke lands on the grid."""
        return self.track_bounds().adjusted(0.5, 0.5, -0.5, -0.5)

    def knob_rect(self) -> QRectF:
        """Where the knob is painted right now."""
        origin = self.track_origin()
        progress = self._progress()
        box_x = progress * self.TRAVEL
        nudge = -self.NUDGE + 2.0 * self.NUDGE * progress
        cx = box_x + self.BOX / 2.0 + nudge
        cy = self.TRACK_H / 2.0
        half_w = self._knob_w / 2.0
        half_h = self._knob_h / 2.0
        cx = max(half_w + 0.5, min(cx, self.TRACK_W - half_w - 0.5))
        return QRectF(origin.x() + cx - half_w, origin.y() + cy - half_h,
                      self._knob_w, self._knob_h)

    def _progress(self) -> float:
        if self.TRAVEL <= 0:  # pragma: no cover - TRAVEL is a frozen 20
            return 1.0 if self.isChecked() else 0.0
        return max(0.0, min(1.0, self._offset / float(self.TRAVEL)))

    def is_hovered(self) -> bool:
        return self._hovered

    # ── state plumbing ───────────────────────────────────────────────────
    def _on_toggled(self, checked: bool) -> None:
        self._offset_anim = motion.animate(
            self, b"knobOffset", float(self.TRAVEL) if checked else 0.0,
            duration="faster", easing=motion.CURVE_IN, parent=self,
        )
        self.switched.emit(checked)

    def _animate_knob(self, width: float, height: float) -> None:
        self._width_anim = motion.animate(
            self, b"knobWidth", width, duration="faster",
            easing=motion.CURVE_IN, parent=self,
        )
        self._height_anim = motion.animate(
            self, b"knobHeight", height, duration="faster",
            easing=motion.CURVE_IN, parent=self,
        )

    def _on_pressed(self) -> None:
        self._animate_knob(self.KNOB_PRESS_W, self.KNOB_PRESS_H)

    def _on_released(self) -> None:
        target = self.KNOB_HOVER if self._hovered else self.KNOB
        self._animate_knob(target, target)

    def enterEvent(self, event: QEvent) -> None:
        self._hovered = True
        if not self.isDown():
            self._animate_knob(self.KNOB_HOVER, self.KNOB_HOVER)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hovered = False
        if not self.isDown():
            self._animate_knob(self.KNOB, self.KNOB)
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        """Stop every animation. A hidden widget that keeps animating keeps the
        CPU awake for a picture nobody can see."""
        motion.stop(self._offset_anim, self._width_anim, self._height_anim)
        super().hideEvent(event)

    def refresh_theme(self) -> None:
        self.update()

    def setChecked(self, checked: bool) -> None:
        """Set the state; the knob animates to match through `toggled`."""
        super().setChecked(bool(checked))

    def set_checked_silently(self, checked: bool) -> None:
        """Set the state and land the knob immediately, emitting no `switched`.

        Used when the UI is reflecting a fact from the engine rather than a user
        action, so a config load does not look like 40 toggles being flipped.
        """
        blocked = self.blockSignals(True)
        super().setChecked(bool(checked))
        self.blockSignals(blocked)
        motion.stop(self._offset_anim)
        self._set_offset(float(self.TRAVEL) if checked else 0.0)

    # ── painting ─────────────────────────────────────────────────────────
    def _track_colours(self) -> tuple[QColor, QColor, QColor]:
        """-> (off fill, off stroke, on fill) for the current interaction state."""
        enabled = self.isEnabled()
        down = self.isDown()
        hover = self._hovered
        if not enabled:
            # ControlAltFillColorDisabled is fully transparent in both themes.
            off_fill = QColor(Qt.GlobalColor.transparent)
            off_stroke = QColor(theme.T("ControlStrongFillColorDisabled"))
            on_fill = QColor(theme.accent("disabled"))
        else:
            if down:
                off_token = "ControlAltFillColorQuarternary"
                on_role = "pressed"
            elif hover:
                off_token = "ControlAltFillColorTertiary"
                on_role = "hover"
            else:
                off_token = "ControlAltFillColorSecondary"
                on_role = "rest"
            off_fill = QColor(theme.T(off_token))
            off_stroke = QColor(theme.T("ControlStrongStrokeColorDefault"))
            on_fill = QColor(theme.accent(on_role))
        return off_fill, off_stroke, on_fill

    def _knob_colour(self) -> QColor:
        """The knob lerps from the off colour to the on-accent colour."""
        if self.isEnabled():
            off = QColor(theme.T("TextFillColorSecondary"))
            on = QColor(theme.accent("text"))
        else:
            off = QColor(theme.T("TextFillColorDisabled"))
            on = QColor(theme.T("TextFillColorInverse"))
        return lerp_color(off, on, self._progress())

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        progress = self._progress()
        radius = float(RADII["toggle_track"])
        track = self.track_rect()
        off_fill, off_stroke, on_fill = self._track_colours()

        # Off track: fill + 1 px strong stroke, fading out as the knob travels.
        painter.setOpacity(1.0 - progress)
        painter.setPen(QPen(off_stroke, 1.0))
        painter.setBrush(off_fill)
        painter.drawRoundedRect(track, radius - 0.5, radius - 0.5)

        # On track: accent fill, no stroke, fading in.
        painter.setOpacity(progress)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(on_fill)
        painter.drawRoundedRect(
            QRectF(0.0, 0.0, float(self.TRACK_W), float(self.TRACK_H)),
            radius, radius,
        )
        painter.setOpacity(1.0)

        knob = self.knob_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._knob_colour())
        painter.drawRoundedRect(knob, knob.height() / 2.0, knob.height() / 2.0)

        if self.hasFocus():
            # Around the TRACK, not the widget: `FOCUS_PAD` is reserved on every
            # side precisely so the ring lands outside the 40x20 pill the way
            # Fluent draws it, instead of on top of it.
            paint_focus_ring(painter, self.track_bounds(), radius)
        painter.end()


# The QSS selectors are the Qt class names; a rename that is not mirrored in
# qss.SEL would silently unstyle the widget, so it fails at import instead.
for _cls, _name in (
    (FluentButton, qss.SEL.BUTTON),
    (FluentLineEdit, qss.SEL.LINE_EDIT),
    (FluentCheckBox, qss.SEL.CHECK_BOX),
    (FluentRadioButton, qss.SEL.RADIO_BUTTON),
    (FluentComboBox, qss.SEL.COMBO_BOX),
    (ToggleSwitch, qss.SEL.TOGGLE),
):
    if _cls.__name__ != _name:  # pragma: no cover - import guard
        raise ValueError(
            f"controls: {_cls.__name__} is styled as {_name!r} in qss.SEL"
        )
del _cls, _name


__all__ = [
    "ButtonVariant", "FluentButton", "FluentLineEdit", "FluentCheckBox",
    "FluentRadioButton", "FluentComboBox", "ToggleSwitch",
    "FocusRingStyle", "ThemeAware",
    "paint_focus_ring", "focus_ring_bounds", "glyph_pixmap", "lerp_color",
    "indicator_rect", "icon_button", "restyle",
]
