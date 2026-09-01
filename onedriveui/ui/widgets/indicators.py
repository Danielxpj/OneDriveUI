"""Progress, storage, identity and file-status indicators.

Four gotchas are designed around here, all of them verified:

  * **Qt arc angles are 1/16 degree, 0 is 3 o'clock and positive is
    counter-clockwise.** A ring that sweeps clockwise from 12 therefore starts
    at `90 * 16` with a **negative** span. Getting either wrong gives a ring
    that fills backwards from the wrong place and still looks plausible.
  * **A `setLoopCount(-1)` animation keeps painting while hidden.** Every loop
    in here is a `motion.SafeLoop`, and `hideEvent` stops it as well.
  * **The Fluent progress bar's track (1 px) is thinner than its fill (3 px).**
    That is intentional and is why this is a custom paint rather than a
    `QProgressBar`.
  * **`QPainter.CompositionMode_Clear` needs an alpha channel.** The widget's
    backing store is ARGB32, so punching the storage bar's segment gaps works
    on the widget but would not on an opaque pixmap.

No colour literal appears in this file. Segment and avatar colours come from
`theme.GNOME_ACCENTS` / `theme.accent()` / `theme.T()`; the status badges are the
frozen emblem art in `icons`.
"""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor, QPainter, QPainterPath, QPaintEvent, QPen, QPixmap,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from onedriveui.models import FileState
from onedriveui.ui import fonts, icons, motion, theme
from onedriveui.ui.theme import METRICS, RADII, SPACING
from onedriveui.ui.widgets.controls import ThemeAware

# ═════════════════════════════════════════════════════════════════════════════
# Shared constants
# ═════════════════════════════════════════════════════════════════════════════

#: Qt measures arc angles in 1/16 degree.
ANGLE_UNIT = 16
#: 0 is 3 o'clock, so 12 o'clock is +90 degrees.
START_ANGLE = 90 * ANGLE_UNIT
#: A full turn, in Qt's units.
FULL_CIRCLE = 360 * ANGLE_UNIT
#: The default ring diameter: the xxxl step of the spacing scale (32 px), which
#: is exactly WinUI's `ProgressRing` default.
RING_DIAMETER = SPACING["xxxl"]
#: One turn of an indeterminate indicator. Fluent specifies a 2 s period.
INDETERMINATE_MS = 2000
#: The indeterminate arc breathes between these extents, in degrees.
SWEEP_MIN_DEG = 30.0
SWEEP_MAX_DEG = 300.0
#: The indeterminate bar's travelling segment, as a fraction of the track.
BAR_SEGMENT = 1.0 / 3.0
#: The phase an indeterminate indicator freezes at when animation is disabled.
#: NOT the loop's end value: at phase 1.0 the ring's breathing arc is back at its
#: 30 degree minimum and the bar's segment has travelled entirely off the end, so
#: "reduce motion" turned an indeterminate ring into a stub and an indeterminate
#: bar into nothing at all. Mid-cycle is the frame that reads as "working".
STATIC_PHASE = 0.5


class ProgressTone(StrEnum):
    """Which status colour a progress indicator paints its fill in."""

    NORMAL = "normal"
    PAUSED = "paused"
    ERROR = "error"


_TONE_TOKEN: dict[ProgressTone, str] = {
    ProgressTone.PAUSED: "SystemFillColorCaution",
    ProgressTone.ERROR: "SystemFillColorCritical",
}


def _tone_colour(tone: ProgressTone) -> QColor:
    """Accent for NORMAL, the matching status token otherwise."""
    token = _TONE_TOKEN.get(tone)
    if token is None:
        return QColor(theme.accent("rest"))
    return QColor(theme.T(token))


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


class _Indicator(ThemeAware, QWidget):
    """Base: a styled background, a theme hook and a stopped-when-hidden loop."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # A Python subclass of QWidget paints no QSS background without this,
        # which is also what lets the sheet's `background: transparent` rule
        # keep a custom paintEvent from being covered by the Window brush.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._loop: motion.SafeLoop | None = None
        self._install_theme_hook()

    def hideEvent(self, event) -> None:
        """Belt and braces with `SafeLoop`'s own event filter."""
        if self._loop is not None:
            self._loop.suspend()
        super().hideEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
# ProgressRing
# ═════════════════════════════════════════════════════════════════════════════

class ProgressRing(_Indicator):
    """The Fluent progress ring: a 4 px round-capped arc, 32 px across.

    Determinate, the arc starts at 12 o'clock and sweeps **clockwise** — start
    `90 * 16`, span negative. Indeterminate, a 30 - 300 degree arc rotates and
    breathes over a 2 s period; the loop is stopped whenever the ring is hidden.

    WinUI's ring has no visible track (`ControlFillColorTransparent`); one is
    available for the determinate case where a "how much is left" read helps.
    """

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 diameter: int = RING_DIAMETER,
                 thickness: float | None = None,
                 track: bool = False) -> None:
        """
        Args:
            diameter: Outer size in logical px.
            thickness: Stroke width; `METRICS["ring_stroke"]` (4 px) by default.
            track: Paint the unfilled remainder of the circle.
        """
        super().__init__(parent)
        self._diameter = int(diameter)
        self._thickness = float(METRICS["ring_stroke"] if thickness is None else thickness)
        self._value = 0.0
        self._indeterminate = False
        self._phase = 0.0
        self._track = bool(track)
        self._tone = ProgressTone.NORMAL
        self.setFixedSize(self._diameter, self._diameter)
        self._loop = motion.SafeLoop(
            self, self._set_phase, start=0.0, end=1.0,
            duration=INDETERMINATE_MS, static=STATIC_PHASE, parent=self,
        )

    # ── state ────────────────────────────────────────────────────────────
    def value(self) -> float:
        return self._value

    def set_value(self, value: float) -> None:
        """Set the determinate fraction, 0.0 .. 1.0."""
        self._value = _clamp01(float(value))
        self.update()

    def is_indeterminate(self) -> bool:
        return self._indeterminate

    def set_indeterminate(self, enabled: bool) -> None:
        """Switch between the sweeping arc and the determinate one."""
        enabled = bool(enabled)
        if enabled == self._indeterminate:
            return
        self._indeterminate = enabled
        if enabled:
            self._loop.start()
        else:
            self._loop.stop()
        self.update()

    def tone(self) -> ProgressTone:
        return self._tone

    def set_tone(self, tone: ProgressTone) -> None:
        self._tone = ProgressTone(tone)
        self.update()

    def set_track_visible(self, visible: bool) -> None:
        self._track = bool(visible)
        self.update()

    def phase(self) -> float:
        """The indeterminate loop's position, 0.0 .. 1.0. For tests."""
        return self._phase

    def _set_phase(self, value: float) -> None:
        self._phase = float(value)
        self.update()

    def showEvent(self, event) -> None:
        """Resume the sweep when the ring comes back into view."""
        super().showEvent(event)
        if self._indeterminate:
            self._loop.start()

    # ── geometry, exposed so the arc maths is testable ───────────────────
    def arc_rect(self) -> QRectF:
        """The circle the pen strokes, inset by half the stroke plus half a px."""
        margin = self._thickness / 2.0 + 0.5
        return QRectF(self.rect()).adjusted(margin, margin, -margin, -margin)

    def determinate_span(self) -> int:
        """The span in 1/16 degree. NEGATIVE: Qt's positive is anticlockwise."""
        return int(round(-self._value * FULL_CIRCLE))

    def indeterminate_arc(self) -> tuple[int, int]:
        """-> (start, span) in 1/16 degree for the current phase."""
        triangle = 1.0 - abs(2.0 * self._phase - 1.0)
        extent = SWEEP_MIN_DEG + (SWEEP_MAX_DEG - SWEEP_MIN_DEG) * triangle
        start = int(round((90.0 - self._phase * 720.0) * ANGLE_UNIT))
        return start, int(round(-extent * ANGLE_UNIT))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.arc_rect()
        pen = QPen(_tone_colour(self._tone), self._thickness)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self._track:
            track_pen = QPen(QColor(theme.T("ControlStrongStrokeColorDefault")),
                             self._thickness)
            track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(track_pen)
            painter.drawEllipse(rect)

        painter.setPen(pen)
        if self._indeterminate:
            start, span = self.indeterminate_arc()
            painter.drawArc(rect, start, span)
        elif self._value > 0.0:
            painter.drawArc(rect, START_ANGLE, self.determinate_span())
        painter.end()


# ═════════════════════════════════════════════════════════════════════════════
# FluentProgressBar
# ═════════════════════════════════════════════════════════════════════════════

class FluentProgressBar(_Indicator):
    """The Fluent progress bar: a **3 px** fill over a **1 px** track.

    The track being thinner than the fill is the whole visual signature and is
    verbatim from `ProgressBar_themeresources.xaml`; a `QProgressBar` cannot
    express it, which is why this is painted.
    """

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 tone: ProgressTone = ProgressTone.NORMAL) -> None:
        super().__init__(parent)
        self._value = 0.0
        self._indeterminate = False
        self._phase = 0.0
        self._tone = ProgressTone(tone)
        self.setFixedHeight(METRICS["progress_fill_h"])
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._loop = motion.SafeLoop(
            self, self._set_phase, start=0.0, end=1.0,
            duration=INDETERMINATE_MS, easing="easy_ease",
            static=STATIC_PHASE, parent=self,
        )

    def value(self) -> float:
        return self._value

    def set_value(self, value: float) -> None:
        self._value = _clamp01(float(value))
        self.update()

    def is_indeterminate(self) -> bool:
        return self._indeterminate

    def set_indeterminate(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._indeterminate:
            return
        self._indeterminate = enabled
        if enabled:
            self._loop.start()
        else:
            self._loop.stop()
        self.update()

    def tone(self) -> ProgressTone:
        return self._tone

    def set_tone(self, tone: ProgressTone) -> None:
        self._tone = ProgressTone(tone)
        self.update()

    def phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float) -> None:
        self._phase = float(value)
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._indeterminate:
            self._loop.start()

    def sizeHint(self) -> QSize:
        return QSize(METRICS["ac_bar_w"], METRICS["progress_fill_h"])

    def track_rect(self) -> QRectF:
        """The 1 px track, vertically centred in the 3 px box."""
        height = float(METRICS["progress_track_h"])
        return QRectF(0.0, (self.height() - height) / 2.0, float(self.width()), height)

    def fill_rect(self) -> QRectF:
        """The 3 px fill for the current value (or the travelling segment)."""
        height = float(METRICS["progress_fill_h"])
        top = (self.height() - height) / 2.0
        width = float(self.width())
        if self._indeterminate:
            seg = width * BAR_SEGMENT
            travel = (width + seg) * self._phase - seg
            left = max(0.0, travel)
            right = min(width, travel + seg)
            return QRectF(left, top, max(0.0, right - left), height)
        return QRectF(0.0, top, width * self._value, height)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        track = self.track_rect()
        painter.setBrush(QColor(theme.T("ControlStrongStrokeColorDefault")))
        painter.drawRoundedRect(track, RADII["progress_track"], RADII["progress_track"])

        fill = self.fill_rect()
        if fill.width() > 0.0:
            painter.setBrush(_tone_colour(self._tone))
            painter.drawRoundedRect(fill, RADII["progress_fill"], RADII["progress_fill"])
        painter.end()


# ═════════════════════════════════════════════════════════════════════════════
# StorageBar
# ═════════════════════════════════════════════════════════════════════════════

#: A used-storage bar turns amber past this fraction and red at full, matching
#: what OneDrive does when a quota is nearly gone.
QUOTA_CAUTION = 0.9
QUOTA_CRITICAL = 1.0


class StorageBar(_Indicator):
    """The Activity Center's segmented storage bar.

    Segments are clipped to a rounded pill first and their ends therefore
    inherit the pill's caps; the gaps between them are punched with
    `CompositionMode_Clear`, which needs the ARGB backing store the widget
    already has.
    """

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 gap: float = 2.0) -> None:
        """
        Args:
            gap: Transparent gap between segments, in logical px. The `xxs`
                spacing nudge.
        """
        super().__init__(parent)
        self._total = 0
        self._segments: tuple[tuple[int, str], ...] = ()
        self._gap = float(gap)
        self.setFixedHeight(METRICS["ac_bar_h"])
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(METRICS["ac_bar_w"], METRICS["ac_bar_h"])

    def total(self) -> int:
        return self._total

    def segments(self) -> tuple[tuple[int, str], ...]:
        """-> ((bytes, '#RRGGBB'), ...) as currently painted."""
        return self._segments

    def used(self) -> int:
        return sum(size for size, _colour in self._segments)

    def fraction(self) -> float:
        """Used / total, clamped. 0.0 when nothing has been set."""
        if self._total <= 0:
            return 0.0
        return _clamp01(self.used() / float(self._total))

    def set_segments(self, total: int, segments) -> None:
        """Paint arbitrary segments.

        Args:
            total: The denominator in bytes.
            segments: An iterable of `(bytes, colour)` where colour is an opaque
                `#RRGGBB` the caller resolved from `theme`.
        """
        self._total = max(0, int(total))
        self._segments = tuple((max(0, int(size)), str(colour)) for size, colour in segments)
        self.update()

    def set_usage(self, used: int, total: int) -> None:
        """One segment, coloured by how close to full the quota is.

        Accent while there is room, caution past 90 %, critical when full — the
        colours are theme tokens, so this stays right in dark mode.
        """
        used = max(0, int(used))
        total = max(0, int(total))
        fraction = (used / total) if total > 0 else 0.0
        if fraction >= QUOTA_CRITICAL:
            colour = theme.T("SystemFillColorCritical")
        elif fraction >= QUOTA_CAUTION:
            colour = theme.T("SystemFillColorCaution")
        else:
            colour = theme.accent("rest")
        self.set_segments(total, ((used, colour),))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        radius = rect.height() / 2.0

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.setClipPath(path)
        painter.fillRect(rect, QColor(theme.T("ControlAltFillColorTertiary")))

        if self._total <= 0:
            painter.end()
            return

        x = 0.0
        last = len(self._segments) - 1
        for index, (size, colour) in enumerate(self._segments):
            width = rect.width() * (size / float(self._total))
            if width <= 0.0:
                continue
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawRect(QRectF(x, 0.0, width, rect.height()))
            x += width
            if self._gap > 0.0 and index < last:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Clear)
                painter.fillRect(QRectF(x, 0.0, self._gap, rect.height()),
                                 Qt.GlobalColor.transparent)
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver)
                x += self._gap
        painter.end()


# ═════════════════════════════════════════════════════════════════════════════
# Avatar
# ═════════════════════════════════════════════════════════════════════════════

#: The nine GNOME accents, verified by reading the portal back. Used as a
#: deterministic per-person palette so no avatar colour is invented here.
AVATAR_PALETTE: tuple[str, ...] = tuple(theme.GNOME_ACCENTS.values())

#: Relative luminance above which black text reads better than white.
_LUMA_PIVOT = 0.55

_GLYPH_PERSON = "person"
icons.glyph_stem(_GLYPH_PERSON)     # raises KeyError on a typo


def _nearest_glyph_size(size: int) -> int:
    """The native glyph size closest to `size`.

    Never scale a 24 px glyph to 16 px — the stroke weight goes wrong — so a
    request is snapped onto the ladder the assets are actually drawn at.
    """
    return min(icons.GLYPH_SIZES, key=lambda native: (abs(native - size), native))


def initials_for(display_name: str) -> str:
    """First + last initial, or the first two letters of a single name.

    An empty name gives an empty string; the avatar then paints the person
    glyph instead of letters.
    """
    parts = [word for word in (display_name or "").split() if word]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    if parts:
        return parts[0][:2].upper()
    return ""


def avatar_colour(display_name: str) -> str:
    """A stable colour for a name, drawn from the GNOME accent set."""
    digest = 0
    for char in display_name or "":
        digest = (digest * 31 + ord(char)) & 0xFFFFFFFF
    return AVATAR_PALETTE[digest % len(AVATAR_PALETTE)]


def _on_colour(background: str) -> str:
    """Black or white text over `background`, whichever has the contrast."""
    colour = QColor(background)
    luma = (0.2126 * colour.red() + 0.7152 * colour.green()
            + 0.0722 * colour.blue()) / 255.0
    return theme.T("TextFillColorPrimary", dark=luma <= _LUMA_PIVOT)


class Avatar(_Indicator):
    """A circular person picture, falling back to initials on a stable colour.

    A supplied pixmap is scaled at the widget's own `devicePixelRatioF()` and
    tagged with it, or it draws at 1/dpr size on a fractional-scaled display.
    """

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 diameter: int = RING_DIAMETER) -> None:
        super().__init__(parent)
        self._diameter = int(diameter)
        self._name = ""
        self._initials = ""
        self._colour = AVATAR_PALETTE[0]
        self._pixmap: QPixmap | None = None
        self.setFixedSize(self._diameter, self._diameter)

    def display_name(self) -> str:
        return self._name

    def initials(self) -> str:
        return self._initials

    def colour(self) -> str:
        return self._colour

    def set_person(self, display_name: str, pixmap: QPixmap | None = None) -> None:
        """Set the identity. `pixmap` wins over the initials when it is valid."""
        self._name = display_name or ""
        self._initials = initials_for(self._name)
        self._colour = avatar_colour(self._name)
        self._pixmap = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self.update()

    def _label_role(self) -> str:
        """A ramp role sized for the circle, never an invented pixel size."""
        if self._diameter <= SPACING["xxl"]:
            return "caption"
        if self._diameter <= SPACING["xxxl"] + SPACING["s"]:
            return "body_strong"
        return "body_large_strong"

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self._pixmap is not None:
            path = QPainterPath()
            path.addEllipse(rect)
            painter.setClipPath(path)
            dpr = self.devicePixelRatioF()
            scaled = self._pixmap.scaled(
                self.size() * dpr,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(dpr)
            painter.drawPixmap(self.rect(), scaled)
            painter.end()
            return

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._colour))
        painter.drawEllipse(rect)

        if self._initials:
            painter.setFont(fonts.font(self._label_role()))
            painter.setPen(QColor(_on_colour(self._colour)))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._initials)
        else:
            size = min(SPACING["xl"], self._diameter - SPACING["m"])
            size = max(SPACING["m"], size)
            # The `devicePixelRatio` overload, not `pixmap(QSize)`: the plain
            # one hands back a 1x raster that `drawPixmap` then magnifies, and a
            # soft glyph next to crisp text is exactly what a 2x screen shows.
            glyph = icons.icon(_GLYPH_PERSON, _nearest_glyph_size(size),
                               color=_on_colour(self._colour))
            pixmap = glyph.pixmap(QSize(size, size), self.devicePixelRatioF())
            painter.drawPixmap(
                int((self.width() - size) / 2), int((self.height() - size) / 2), pixmap)
        painter.end()


# ═════════════════════════════════════════════════════════════════════════════
# File status badges
# ═════════════════════════════════════════════════════════════════════════════

#: The badge is 10 x 10 on a 16 px icon, 12 on 20, 16 on 32 — i.e. a constant
#: fraction of the icon, which is exactly what METRICS["tray_badge"] encodes on
#: the 24-unit design grid.
BADGE_FRACTION = METRICS["tray_badge"] / 24.0


def status_badge_size(icon_px: int) -> int:
    """The badge diameter for a file icon of `icon_px`."""
    return max(SPACING["s"], int(round(icon_px * BADGE_FRACTION)))


def paint_status_badge(painter: QPainter,
                       rect: QRectF,
                       state: FileState,
                       *,
                       ring: float = 1.0) -> None:
    """Draw a file-status badge into `rect`, with a cut-out separation ring.

    The badge art is the frozen emblem set — the same SVG Nautilus is handed —
    so the in-app file browser and the file manager can never disagree about
    what a state looks like.

    Args:
        painter: A painter over an ARGB surface (a widget backing store, or a
            transparent QPixmap). `CompositionMode_Clear` needs the alpha.
        rect: Where the badge goes; the bottom-LEFT of the file icon, per the
            Explorer convention.
        state: The file state. `FileState.UNKNOWN` has no emblem and paints
            nothing at all.
        ring: Width of the transparent separation ring punched around the badge
            so it reads over any thumbnail.
    """
    stem = icons.emblem_name(state)
    if not stem:
        return
    data = icons.svg_bytes("emblems", icons.emblem_icon_name(stem))
    size = max(1, int(round(min(rect.width(), rect.height()))))
    dpr = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
    pixmap = icons.render_svg(data, size, dpr)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if ring > 0.0:
        painter.setPen(Qt.PenStyle.NoPen)
        # CompositionMode_Clear still needs a BRUSH to have anything to erase
        # with; the colour is irrelevant, the coverage is not.
        painter.setBrush(Qt.GlobalColor.black)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.drawEllipse(rect.adjusted(-ring, -ring, ring, ring))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    painter.drawPixmap(rect.topLeft(), pixmap)
    painter.restore()


def status_badge_pixmap(state: FileState, size: int, dpr: float = 1.0) -> QPixmap:
    """A standalone badge pixmap, for a list delegate that composes its own row."""
    pixmap = QPixmap(max(1, int(round(size * dpr))), max(1, int(round(size * dpr))))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_status_badge(painter, QRectF(0.0, 0.0, float(size), float(size)),
                       state, ring=0.0)
    painter.end()
    return pixmap


class StatusBadge(_Indicator):
    """A file-status badge as a standalone widget, for a details pane."""

    def __init__(self,
                 parent: QWidget | None = None,
                 *,
                 state: FileState = FileState.UNKNOWN,
                 size: int = SPACING["l"]) -> None:
        super().__init__(parent)
        self._state = FileState(state)
        self._size = int(size)
        self.setFixedSize(self._size, self._size)

    def state(self) -> FileState:
        return self._state

    def set_state(self, state: FileState) -> None:
        self._state = FileState(state)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        paint_status_badge(painter, QRectF(self.rect()), self._state, ring=0.0)
        painter.end()


__all__ = [
    "ANGLE_UNIT", "START_ANGLE", "FULL_CIRCLE", "RING_DIAMETER",
    "INDETERMINATE_MS", "SWEEP_MIN_DEG", "SWEEP_MAX_DEG", "BAR_SEGMENT",
    "STATIC_PHASE",
    "QUOTA_CAUTION", "QUOTA_CRITICAL", "AVATAR_PALETTE", "BADGE_FRACTION",
    "ProgressTone", "ProgressRing", "FluentProgressBar", "StorageBar",
    "Avatar", "StatusBadge",
    "initials_for", "avatar_colour", "status_badge_size",
    "paint_status_badge", "status_badge_pixmap",
]
