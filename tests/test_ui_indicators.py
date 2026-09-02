"""WP-11a — `onedriveui.ui.widgets.indicators`.

The arc arithmetic is the part that looks right and is wrong: Qt measures in
1/16 degree, 0 is 3 o'clock and positive is anticlockwise, so a ring that sweeps
clockwise from 12 is `start = 90 * 16` with a **negative** span. These tests
assert the numbers and then check the painted pixels land in the quadrant the
numbers claim.
"""

from __future__ import annotations

import re

import pytest
from PySide6.QtCore import QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QVBoxLayout, QWidget

from onedriveui.models import FileState
from onedriveui.ui import fonts, icons, qss, theme
from onedriveui.ui.theme import METRICS, SPACING
from onedriveui.ui.widgets import indicators
from onedriveui.ui.widgets.indicators import (
    Avatar, FluentProgressBar, ProgressRing, ProgressTone, StatusBadge, StorageBar,
)


# ═════════════════════════════════════════════════════════════════════════════
# Harness
# ═════════════════════════════════════════════════════════════════════════════

def render(widget, *, dpr: float = 1.0) -> QImage:
    widget.ensurePolished()
    size = widget.size() if not widget.size().isEmpty() else widget.sizeHint()
    widget.resize(size)
    image = QImage(int(round(size.width() * dpr)), int(round(size.height() * dpr)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    return image


def painted(image: QImage) -> list[tuple[int, int]]:
    return [
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 40
    ]


def colours_in(image: QImage) -> set[str]:
    return {
        image.pixelColor(x, y).name().upper()
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() > 200
    }


def is_blank(image: QImage) -> bool:
    return not painted(image)


@pytest.fixture
def no_animation(monkeypatch):
    monkeypatch.setattr(theme, "_ANIMATIONS", False, raising=False)
    yield
    monkeypatch.setattr(theme, "_ANIMATIONS", True, raising=False)


@pytest.fixture
def styled(qapp):
    """The kit's own stylesheet.

    Load-bearing for these renders: every indicator sets `WA_StyledBackground`
    so the QSS box model reaches it, and it is the sheet's
    `ProgressRing { background: transparent }` rule that then keeps a
    custom-painted widget from being filled with the palette's Window brush.
    """
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    fonts.apply_app_font(qapp)
    qss.apply(qapp, dark=theme.current_dark())
    yield qapp
    qapp.setStyleSheet(previous_sheet)
    qapp.setFont(previous_font)
    qss.invalidate()


# ═════════════════════════════════════════════════════════════════════════════
# ProgressRing — the arc arithmetic
# ═════════════════════════════════════════════════════════════════════════════

def test_angle_constants_are_qt_sixteenths():
    """Qt measures arcs in 1/16 degree; 0 is 3 o'clock."""
    assert indicators.ANGLE_UNIT == 16
    assert indicators.START_ANGLE == 90 * 16 == 1440
    assert indicators.FULL_CIRCLE == 360 * 16 == 5760


def test_determinate_span_is_negative(qapp):
    """Positive is ANTICLOCKWISE, so a clockwise sweep needs a negative span."""
    ring = ProgressRing()
    ring.set_value(0.25)
    assert ring.determinate_span() == -(indicators.FULL_CIRCLE // 4)
    assert ring.determinate_span() < 0
    ring.set_value(1.0)
    assert ring.determinate_span() == -indicators.FULL_CIRCLE
    ring.set_value(0.0)
    assert ring.determinate_span() == 0


def test_value_is_clamped(qapp):
    ring = ProgressRing()
    ring.set_value(-3.0)
    assert ring.value() == 0.0
    ring.set_value(9.0)
    assert ring.value() == 1.0


def test_ring_defaults_match_winui(qapp):
    """4 px stroke, 32 px across — `ProgressRing_themeresources.xaml`."""
    ring = ProgressRing()
    assert ring.size() == QSize(32, 32)
    assert indicators.RING_DIAMETER == SPACING["xxxl"] == 32
    assert ring._thickness == METRICS["ring_stroke"] == 4


def test_arc_rect_insets_by_half_the_stroke(qapp):
    ring = ProgressRing()
    rect = ring.arc_rect()
    margin = METRICS["ring_stroke"] / 2.0 + 0.5
    assert rect.left() == pytest.approx(margin)
    assert rect.width() == pytest.approx(32 - 2 * margin)


@pytest.mark.slow
def test_ring_sweeps_clockwise_from_twelve(qapp, styled):
    """A quarter ring paints the TOP-RIGHT quadrant and nothing on the left."""
    ring = ProgressRing()
    ring.resize(32, 32)
    ring.set_value(0.25)
    image = render(ring)
    pixels = painted(image)
    assert pixels, "the ring painted nothing"

    centre = 16
    top_right = [p for p in pixels if p[0] > centre and p[1] < centre]
    top_left = [p for p in pixels if p[0] < centre - 4 and p[1] < centre - 4]
    bottom_left = [p for p in pixels if p[0] < centre - 4 and p[1] > centre + 4]
    assert len(top_right) > 10, "nothing painted in the clockwise-from-12 quadrant"
    assert not top_left, "the arc swept anticlockwise"
    assert not bottom_left


@pytest.mark.slow
def test_ring_at_zero_paints_nothing_without_a_track(qapp, styled):
    ring = ProgressRing()
    ring.resize(32, 32)
    ring.set_value(0.0)
    assert is_blank(render(ring))
    ring.set_track_visible(True)
    assert not is_blank(render(ring))


@pytest.mark.slow
@pytest.mark.parametrize("dark_theme", [False, True])
def test_ring_paints_the_tone_colour(qapp, styled, dark_theme, monkeypatch):
    monkeypatch.setattr(theme, "_DETECTED_DARK", dark_theme, raising=False)
    ring = ProgressRing()
    ring.resize(32, 32)
    ring.set_value(1.0)
    assert theme.accent("rest", dark=dark_theme).upper() in colours_in(render(ring))

    ring.set_tone(ProgressTone.ERROR)
    assert theme.T("SystemFillColorCritical", dark=dark_theme).upper() \
        in colours_in(render(ring))
    ring.set_tone(ProgressTone.PAUSED)
    assert theme.T("SystemFillColorCaution", dark=dark_theme).upper() \
        in colours_in(render(ring))


def test_indeterminate_arc_stays_within_the_documented_extents(qapp):
    ring = ProgressRing()
    ring.set_indeterminate(True)
    for phase in (0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0):
        ring._set_phase(phase)
        start, span = ring.indeterminate_arc()
        assert span < 0, "the sweep must still be clockwise"
        degrees = abs(span) / indicators.ANGLE_UNIT
        assert indicators.SWEEP_MIN_DEG - 0.5 <= degrees <= indicators.SWEEP_MAX_DEG + 0.5
        assert isinstance(start, int)
    ring.set_indeterminate(False)


def test_indeterminate_loop_starts_and_hide_stops_it(qapp, qtbot):
    """`setLoopCount(-1)` keeps repainting behind a hidden widget."""
    ring = ProgressRing()
    ring.show()
    ring.set_indeterminate(True)
    qtbot.process()
    assert ring._loop.is_running()
    ring.hide()
    qtbot.process()
    assert not ring._loop.is_running()
    ring.show()
    qtbot.process()
    assert ring._loop.is_running()
    ring.set_indeterminate(False)
    assert not ring._loop.is_running()
    ring.hide()


def test_indeterminate_never_spins_with_animation_off(qapp, no_animation, qtbot):
    ring = ProgressRing()
    ring.show()
    ring.set_indeterminate(True)
    qtbot.process()
    assert ring._loop.animation.duration() == 0
    assert not ring._loop.is_running()
    ring.hide()


def test_indeterminate_period_is_two_seconds():
    assert indicators.INDETERMINATE_MS == 2000


# ═════════════════════════════════════════════════════════════════════════════
# FluentProgressBar — a 3 px fill over a 1 px track
# ═════════════════════════════════════════════════════════════════════════════

def test_track_is_thinner_than_the_fill(qapp):
    """Verbatim `ProgressBar_themeresources.xaml`: fill 3 px, track 1 px."""
    bar = FluentProgressBar()
    bar.resize(200, METRICS["progress_fill_h"])
    assert METRICS["progress_track_h"] < METRICS["progress_fill_h"]
    assert bar.track_rect().height() == METRICS["progress_track_h"] == 1
    bar.set_value(1.0)
    assert bar.fill_rect().height() == METRICS["progress_fill_h"] == 3
    assert bar.height() == METRICS["progress_fill_h"]


def test_bar_fill_tracks_the_value(qapp):
    bar = FluentProgressBar()
    bar.resize(200, 3)
    for value, width in ((0.0, 0.0), (0.25, 50.0), (1.0, 200.0)):
        bar.set_value(value)
        assert bar.fill_rect().width() == pytest.approx(width)


def test_bar_track_is_centred(qapp):
    bar = FluentProgressBar()
    bar.resize(200, 3)
    track = bar.track_rect()
    assert track.top() == pytest.approx(1.0)
    assert track.width() == 200.0


@pytest.mark.slow
def test_bar_paints_the_track_and_the_fill(qapp, styled):
    bar = FluentProgressBar()
    bar.resize(200, 3)
    bar.set_value(0.5)
    present = colours_in(render(bar))
    assert theme.accent("rest", dark=False).upper() in present
    assert theme.T("ControlStrongStrokeColorDefault", dark=False).upper() in present


@pytest.mark.slow
@pytest.mark.parametrize("tone,token", [
    (ProgressTone.PAUSED, "SystemFillColorCaution"),
    (ProgressTone.ERROR, "SystemFillColorCritical"),
])
def test_bar_tones(qapp, styled, tone, token):
    bar = FluentProgressBar(tone=tone)
    bar.resize(200, 3)
    bar.set_value(1.0)
    assert bar.tone() is tone
    assert theme.T(token, dark=False).upper() in colours_in(render(bar))


def test_bar_indeterminate_segment_is_a_third(qapp):
    bar = FluentProgressBar()
    bar.resize(300, 3)
    bar.set_indeterminate(True)
    assert indicators.BAR_SEGMENT == pytest.approx(1.0 / 3.0)
    bar._set_phase(0.5)
    segment = bar.fill_rect()
    assert segment.width() == pytest.approx(300 * indicators.BAR_SEGMENT, abs=1.0)
    # The segment enters from the left and leaves at the right.
    bar._set_phase(0.0)
    assert bar.fill_rect().left() == 0.0
    bar._set_phase(1.0)
    assert bar.fill_rect().right() == pytest.approx(300.0)
    bar.set_indeterminate(False)


def test_bar_indeterminate_loop_stops_on_hide(qapp, qtbot):
    bar = FluentProgressBar()
    bar.resize(200, 3)
    bar.show()
    bar.set_indeterminate(True)
    qtbot.process()
    assert bar._loop.is_running()
    bar.hide()
    qtbot.process()
    assert not bar._loop.is_running()


# ═════════════════════════════════════════════════════════════════════════════
# StorageBar
# ═════════════════════════════════════════════════════════════════════════════

def test_storage_bar_geometry(qapp):
    bar = StorageBar()
    assert bar.height() == METRICS["ac_bar_h"] == 4
    assert bar.sizeHint() == QSize(METRICS["ac_bar_w"], METRICS["ac_bar_h"])


def test_storage_bar_segments_and_fraction(qapp):
    bar = StorageBar()
    assert bar.fraction() == 0.0
    bar.set_segments(1000, ((300, theme.accent("rest")),
                            (200, theme.T("SystemFillColorSuccess"))))
    assert bar.total() == 1000
    assert bar.used() == 500
    assert bar.fraction() == pytest.approx(0.5)
    assert len(bar.segments()) == 2


def test_storage_bar_usage_colours_by_threshold(qapp):
    bar = StorageBar()
    bar.set_usage(10, 100)
    assert bar.segments()[0][1] == theme.accent("rest")
    bar.set_usage(95, 100)
    assert bar.segments()[0][1] == theme.T("SystemFillColorCaution")
    bar.set_usage(100, 100)
    assert bar.segments()[0][1] == theme.T("SystemFillColorCritical")
    assert indicators.QUOTA_CAUTION == 0.9
    assert indicators.QUOTA_CRITICAL == 1.0


def test_storage_bar_handles_a_zero_total(qapp):
    bar = StorageBar()
    bar.set_usage(0, 0)
    assert bar.fraction() == 0.0
    bar.resize(200, 4)
    render(bar)          # must not divide by zero


@pytest.mark.slow
def test_storage_bar_paints_track_and_segments(qapp, styled):
    bar = StorageBar()
    bar.resize(200, 4)
    bar.set_segments(100, ((50, theme.accent("rest")),))
    present = colours_in(render(bar))
    assert theme.accent("rest", dark=False).upper() in present
    assert theme.T("ControlAltFillColorTertiary", dark=False).upper() in present


@pytest.mark.slow
def test_storage_bar_punches_transparent_gaps(qapp, styled):
    """The gap is `CompositionMode_Clear`, which needs the ARGB backing store."""
    bar = StorageBar(gap=4.0)
    bar.resize(200, 4)
    bar.set_segments(100, ((40, theme.accent("rest")),
                           (40, theme.T("SystemFillColorSuccess"))))
    image = render(bar)
    row = 2
    transparent = [x for x in range(200) if image.pixelColor(x, row).alpha() == 0]
    assert transparent, "no gap was punched between the segments"
    # The gap sits where the first segment ends, at 40 % of 200 px.
    assert 78 <= min(transparent) <= 82


# ═════════════════════════════════════════════════════════════════════════════
# Avatar
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,expected", [
    ("Daniel Perez", "DP"),
    ("daniel perez jimenez", "DJ"),
    ("Daniel", "DA"),
    ("d", "D"),
    ("", ""),
    ("   ", ""),
])
def test_initials(name, expected):
    assert indicators.initials_for(name) == expected


def test_avatar_colour_is_stable_and_from_the_gnome_set():
    """No colour is invented; the palette is WP-00's verified GNOME accents."""
    assert indicators.AVATAR_PALETTE == tuple(theme.GNOME_ACCENTS.values())
    assert len(indicators.AVATAR_PALETTE) == 9
    first = indicators.avatar_colour("Daniel Perez")
    assert first == indicators.avatar_colour("Daniel Perez")
    assert first in indicators.AVATAR_PALETTE
    names = {indicators.avatar_colour(f"Person {i}") for i in range(30)}
    assert len(names) > 1, "the hash must spread across the palette"


def test_avatar_set_person(qapp):
    avatar = Avatar()
    avatar.set_person("Daniel Perez")
    assert avatar.display_name() == "Daniel Perez"
    assert avatar.initials() == "DP"
    assert avatar.colour() in indicators.AVATAR_PALETTE
    assert avatar.size() == QSize(indicators.RING_DIAMETER, indicators.RING_DIAMETER)


@pytest.mark.slow
def test_avatar_paints_initials_on_its_colour(qapp, styled):
    avatar = Avatar()
    avatar.set_person("Daniel Perez")
    image = render(avatar)
    present = colours_in(image)
    assert avatar.colour().upper() in present
    assert not is_blank(image)


@pytest.mark.slow
def test_avatar_without_a_name_paints_a_glyph(qapp, styled):
    avatar = Avatar()
    avatar.set_person("")
    assert avatar.initials() == ""
    assert not is_blank(render(avatar))


@pytest.mark.slow
def test_avatar_prefers_a_pixmap_and_honours_dpr(qapp, styled):
    """A pixmap must be tagged with the widget's dpr or it draws at 1/dpr size."""
    photo = QPixmap(64, 64)
    photo.fill(QColor(theme.T("SystemFillColorSuccess", dark=False)))
    avatar = Avatar()
    avatar.set_person("Daniel Perez", photo)
    image = render(avatar)
    assert theme.T("SystemFillColorSuccess", dark=False).upper() in colours_in(image)

    hi = render(avatar, dpr=2.0)
    assert hi.width() == indicators.RING_DIAMETER * 2
    assert hi.devicePixelRatio() == 2.0


def test_avatar_ignores_a_null_pixmap(qapp):
    avatar = Avatar()
    avatar.set_person("Daniel Perez", QPixmap())
    assert avatar._pixmap is None


def test_avatar_label_role_scales_with_the_circle(qapp):
    """The label size comes from the type ramp, never from an invented factor."""
    for diameter, role in ((20, "caption"), (32, "body_strong"), (64, "body_large_strong")):
        avatar = Avatar(diameter=diameter)
        assert avatar._label_role() == role
        assert theme.font_px(avatar._label_role()) == fonts.font(role).pixelSize()


# ═════════════════════════════════════════════════════════════════════════════
# File status badges
# ═════════════════════════════════════════════════════════════════════════════

def test_badge_size_scales_with_the_icon():
    """10 x 10 on a 16 px icon, 12 on 20, 16 on 32 — a constant fraction."""
    assert indicators.status_badge_size(24) == METRICS["tray_badge"] == 10
    assert indicators.status_badge_size(48) == 2 * METRICS["tray_badge"]
    assert indicators.status_badge_size(8) >= SPACING["s"]


@pytest.mark.slow
@pytest.mark.parametrize("state", [s for s in FileState if s is not FileState.UNKNOWN],
                         ids=lambda s: s.value)
def test_every_file_state_paints_a_badge(qapp, state):
    pixmap = indicators.status_badge_pixmap(state, SPACING["l"])
    assert not pixmap.isNull()
    assert not is_blank(pixmap.toImage())


def test_unknown_state_paints_nothing(qapp):
    """`FileState.UNKNOWN` has no emblem in WP-00 and must draw nothing."""
    assert icons.emblem_name(FileState.UNKNOWN) == ""
    pixmap = indicators.status_badge_pixmap(FileState.UNKNOWN, SPACING["l"])
    assert is_blank(pixmap.toImage())


@pytest.mark.slow
def test_status_badge_uses_the_frozen_emblem_art(qapp):
    """The in-app badge and the Nautilus emblem are the same SVG."""
    size = SPACING["l"]
    mine = indicators.status_badge_pixmap(FileState.LOCAL, size).toImage()
    stem = icons.emblem_name(FileState.LOCAL)
    theirs = icons.render_svg(
        icons.svg_bytes("emblems", icons.emblem_icon_name(stem)), size, 1.0
    ).toImage()
    assert mine == theirs


@pytest.mark.slow
def test_paint_status_badge_punches_a_ring(qapp):
    """The 1 px cut-out ring is what makes a badge read over a thumbnail."""
    size = SPACING["xl"]
    # CompositionMode_Clear needs an alpha channel. A widget's backing store is
    # ARGB32 and has one; an opaque QPixmap may not, which is exactly the
    # caveat the module documents.
    image = QImage(size + 8, size + 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(theme.T("SystemFillColorSuccessBackground", dark=False)))
    painter = QPainter(image)
    indicators.paint_status_badge(
        painter, QRectF(4.0, 4.0, float(size), float(size)),
        FileState.LOCAL, ring=2.0)
    painter.end()
    cleared = [
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() < 255
    ]
    assert cleared, "no separation ring was cut out"


@pytest.mark.slow
@pytest.mark.parametrize("dpr", [1.0, 2.0, 3.0])
def test_the_badge_art_fills_the_box_at_every_ratio(qapp, dpr):
    """The whole emblem, not its top-left corner.

    `icons._render` allocates `px * dpr` device pixels and calls
    `setDevicePixelRatio()` on the pixmap, which puts the painter over it into
    LOGICAL coordinates. Rendering the SVG into the DEVICE rectangle therefore
    drew the art at `dpr` times its size and kept only the part that fitted:
    at dpr 2, one quarter of an emblem on every HiDPI screen. Comparing the
    scaled render against the 1x one is the check that catches it — the size
    and the ratio were both right while the picture was wrong.
    """
    size = SPACING["xl"]
    stem = icons.emblem_name(FileState.LOCAL)
    data = icons.svg_bytes("emblems", icons.emblem_icon_name(stem))

    reference = icons.render_svg(data, size, 1.0).toImage()
    scaled = icons.render_svg(data, size, dpr).toImage().scaled(
        reference.width(), reference.height(),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation)

    assert scaled.size() == reference.size()
    off = sum(
        1
        for y in range(reference.height())
        for x in range(reference.width())
        if abs(reference.pixelColor(x, y).alpha()
               - scaled.pixelColor(x, y).alpha()) > 96
    )
    # Antialiasing differs between the two rasterisations, so a handful of edge
    # pixels is expected; a quarter-of-the-art crop is thousands.
    assert off < reference.width() * reference.height() * 0.05, (
        f"the dpr {dpr} render does not match the 1x art ({off} pixels differ)")


def test_status_badge_widget(qapp):
    badge = StatusBadge(state=FileState.PINNED)
    assert badge.state() is FileState.PINNED
    assert badge.size() == QSize(SPACING["l"], SPACING["l"])
    badge.set_state(FileState.ERROR)
    assert badge.state() is FileState.ERROR
    assert not is_blank(render(badge))


# ═════════════════════════════════════════════════════════════════════════════
# Theme and DPI coverage
# ═════════════════════════════════════════════════════════════════════════════

def _indicator_gallery() -> list:
    ring = ProgressRing(track=True)
    ring.set_value(0.4)
    bar = FluentProgressBar()
    bar.resize(200, 3)
    bar.set_value(0.7)
    storage = StorageBar()
    storage.resize(200, 4)
    storage.set_usage(700, 1000)
    avatar = Avatar()
    avatar.set_person("Daniel Perez")
    badge = StatusBadge(state=FileState.SYNCING)
    return [ring, bar, storage, avatar, badge]


@pytest.mark.slow
@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("dark_theme", [False, True])
def test_indicators_render_at_every_theme_and_ratio(qapp, styled, dpr, dark_theme, monkeypatch):
    monkeypatch.setattr(theme, "_DETECTED_DARK", dark_theme, raising=False)
    icons.clear_cache()
    try:
        for widget in _indicator_gallery():
            image = render(widget, dpr=dpr)
            assert image.devicePixelRatio() == dpr
            assert image.width() == int(round(widget.width() * dpr))
            assert not is_blank(image), type(widget).__name__
    finally:
        icons.clear_cache()


def test_every_indicator_declares_a_styled_background(qapp):
    """A direct QWidget subclass paints no QSS background without the attribute."""
    for widget in _indicator_gallery():
        assert widget.testAttribute(Qt.WidgetAttribute.WA_StyledBackground), \
            type(widget).__name__
        assert qss.check_styled_background(widget)


def test_tone_tokens_are_frozen_theme_tokens():
    assert set(indicators._TONE_TOKEN.values()) <= set(theme.TOKENS)
    assert set(indicators._TONE_TOKEN) == {ProgressTone.PAUSED, ProgressTone.ERROR}
    assert len(ProgressTone) == 3


def test_indicators_expose_a_refresh_hook(qapp):
    """A theme change is one repaint; nothing caches a colour."""
    for widget in _indicator_gallery():
        assert callable(getattr(widget, "refresh_theme", None))
        widget.refresh_theme()


# ═════════════════════════════════════════════════════════════════════════════
# Adversarial-audit regressions: the reduced-motion static frame.
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def no_animation(monkeypatch):
    """The machine's real setting: both animation preferences are false here."""
    monkeypatch.setattr(theme, "_ANIMATIONS", False, raising=False)
    yield
    monkeypatch.setattr(theme, "_ANIMATIONS", True, raising=False)


def test_indeterminate_ring_shows_a_real_arc_with_animation_off(qapp, no_animation):
    """A stopped loop must not land on its emptiest frame.

    `SafeLoop` used to apply `end` when animation is disabled, and for a cyclic
    phase the end frame IS the first frame: the ring's breathing arc is back at
    its 30 degree minimum, so "reduce motion" turned an indeterminate ring into
    a stub. Every user with animations off — which is this machine's default —
    saw that instead of a spinner.
    """
    host = QWidget()
    layout = QVBoxLayout(host)
    ring = ProgressRing()
    layout.addWidget(ring)
    host.show()
    ring.set_indeterminate(True)

    assert ring._loop.static_value() == indicators.STATIC_PHASE == 0.5
    assert not ring._loop.is_running(), "no loop should spin at 0 ms"
    _start, span = ring.indeterminate_arc()
    degrees = abs(span) / indicators.ANGLE_UNIT
    assert degrees == pytest.approx(indicators.SWEEP_MAX_DEG)
    assert degrees > indicators.SWEEP_MIN_DEG * 2
    host.hide()


def test_indeterminate_bar_is_visible_with_animation_off(qapp, no_animation):
    """At phase 1.0 the travelling segment has left the track entirely.

    `fill_rect()` returned a ZERO-WIDTH rect, i.e. an indeterminate progress bar
    that painted nothing at all.
    """
    host = QWidget()
    layout = QVBoxLayout(host)
    bar = FluentProgressBar()
    layout.addWidget(bar)
    host.resize(300, 40)
    host.show()
    bar.set_indeterminate(True)

    assert bar._loop.static_value() == indicators.STATIC_PHASE
    fill = bar.fill_rect()
    assert fill.width() > 0.0, "the indeterminate bar painted nothing"
    assert fill.width() == pytest.approx(bar.width() * indicators.BAR_SEGMENT, abs=1.0)
    # And it is centred, not jammed against an edge.
    assert fill.left() > 0.0
    assert fill.right() < float(bar.width())
    host.hide()


def test_safe_loop_static_defaults_to_end_for_a_one_shot(qapp, no_animation):
    """A rotation's end frame IS its first frame, so the default is still right."""
    from onedriveui.ui import motion

    widget = QWidget()
    widget.show()
    seen: list[float] = []
    loop = motion.SafeLoop(widget, seen.append, start=0.0, end=360.0,
                           duration="normal", parent=widget)
    assert loop.static_value() == 360.0
    loop.start()
    assert seen == [360.0]
    widget.hide()


def test_avatar_glyph_is_device_pixel_ratio_aware(qapp):
    """The fallback person glyph went through `pixmap(QSize)` — a 1x raster."""
    source = (indicators.__file__)
    code = open(source, encoding="utf-8").read()
    body = "\n".join(line for line in code.splitlines()
                     if not line.lstrip().startswith("#"))
    for call in re.finditer(r"\.pixmap\(([^)]*\)?[^)]*)\)", body):
        assert "," in call.group(1), call.group(0)
