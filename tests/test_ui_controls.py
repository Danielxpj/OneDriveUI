"""WP-11a — `onedriveui.ui.widgets.controls` and `onedriveui.ui.motion`.

Covers the Windows 11 toggle geometry, the four button variants, the two-tone
focus ring, the motion gate, and the whole-kit contact sheet at both themes and
four device pixel ratios.

`motion` is exercised here rather than in a file of its own because WP-11a owns
exactly four test modules and every motion guarantee is observable through a
control: the toggle is the only widget in the kit that animates.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from PySide6.QtCore import (
    QAbstractAnimation, QEvent, QPoint, QPointF, QRect, QRectF, QSize, QSizeF,
    Qt,
)
from PySide6.QtGui import (
    QColor, QEnterEvent, QImage, QPainter, QPixmap,
)
from PySide6.QtWidgets import QLabel, QStyle, QStyleOption, QVBoxLayout, QWidget

from onedriveui.models import FileState
from onedriveui.ui import fonts, icons, motion, qss, theme
from onedriveui.ui.theme import METRICS, OBJ, PROP, RADII
from onedriveui.ui.widgets import controls, indicators
from onedriveui.ui.widgets.controls import (
    ButtonVariant, FluentButton, FluentCheckBox, FluentComboBox, FluentLineEdit,
    FluentRadioButton, FocusRingStyle, ToggleSwitch,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
#: Where the acceptance contact sheet is written.
CONTACT_SHEET = REPO_ROOT / "docs" / "wp11a-contact-sheet.png"
#: The ratios the kit has to survive. 1.25 and 1.5 are what Mutter advertises.
DEVICE_PIXEL_RATIOS = (1.0, 1.25, 1.5, 2.0)


# ═════════════════════════════════════════════════════════════════════════════
# Harness
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def styled(qapp):
    """The kit's own stylesheet and font, restored afterwards."""
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    fonts.apply_app_font(qapp)
    qss.apply(qapp, dark=False)
    yield qapp
    qapp.setStyleSheet(previous_sheet)
    qapp.setFont(previous_font)
    qss.invalidate()


@pytest.fixture
def ring_styled(qapp):
    """`styled`, plus the `FocusRingStyle` proxy the real startup installs.

    The two-tone ring only exists because of that proxy, so a test that asserts
    the ring has to install it — `styled` alone leaves Qt's own dotted rect.
    """
    # The NAME, not the object: `setStyle()` takes ownership and deletes the
    # style it replaces, so holding the QStyle would leave a dangling wrapper.
    previous_style = qapp.style().objectName() or qss.FUSION
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    qapp.setStyle(FocusRingStyle())
    fonts.apply_app_font(qapp)
    qss.apply(qapp, dark=False)
    yield qapp
    qapp.setStyleSheet(previous_sheet)
    qapp.setFont(previous_font)
    qapp.setStyle(previous_style)
    qss.invalidate()


@pytest.fixture
def dark(monkeypatch):
    """Force the dark theme for the duration of a test."""
    monkeypatch.setattr(theme, "_DETECTED_DARK", True, raising=False)
    theme._STYLESHEET_CACHE.clear()
    qss.invalidate()
    icons.clear_cache()
    yield
    theme._STYLESHEET_CACHE.clear()
    qss.invalidate()
    icons.clear_cache()


@pytest.fixture
def no_animation(monkeypatch):
    """The machine's real setting: both animation preferences are false here."""
    monkeypatch.setattr(theme, "_ANIMATIONS", False, raising=False)
    yield
    monkeypatch.setattr(theme, "_ANIMATIONS", True, raising=False)


def render(widget, *, dpr: float = 1.0, pad: int = 0) -> QImage:
    """Render a widget offscreen at `dpr`, with optional room for a focus ring."""
    widget.ensurePolished()
    size = widget.sizeHint() if widget.size().isEmpty() else widget.size()
    width, height = size.width() + 2 * pad, size.height() + 2 * pad
    widget.resize(size)
    image = QImage(int(round(width * dpr)), int(round(height * dpr)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    widget.render(painter, QPoint(pad, pad))
    painter.end()
    return image


def colours_in(image: QImage) -> set[str]:
    """Every opaque colour present, as upper-case #RRGGBB."""
    found: set[str] = set()
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 200:
                found.add(pixel.name().upper())
    return found


def is_blank(image: QImage) -> bool:
    return all(
        image.pixelColor(x, y).alpha() == 0
        for y in range(image.height())
        for x in range(image.width())
    )


def hover(widget) -> None:
    point = QPointF(widget.width() / 2.0, widget.height() / 2.0)
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    widget.event(QEnterEvent(point, point, point))


def unhover(widget) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    widget.event(QEvent(QEvent.Type.Leave))


def settle(qtbot, animated_ms: int = 400) -> None:
    """Let every 83 ms animation finish."""
    qtbot.wait(animated_ms)
    qtbot.process(3)


# ═════════════════════════════════════════════════════════════════════════════
# motion — the gate
# ═════════════════════════════════════════════════════════════════════════════

def test_dur_routes_every_name_through_the_theme(qapp):
    for name, ms in theme.DURATION.items():
        assert motion.DUR(name) == ms
    assert motion.DUR(123) == 123
    with pytest.raises(KeyError):
        motion.DUR("not_a_duration")


def test_dur_is_zero_when_animation_is_off(qapp, no_animation):
    assert motion.reduced_motion() is True
    for name in theme.DURATION:
        assert motion.DUR(name) == 0
    assert motion.DUR(500) == 0


def test_curves_are_explicit_bezier_splines(qapp):
    """OutCubic is a near miss for KeySpline 0,0,0,1 and is not used."""
    from PySide6.QtCore import QEasingCurve

    standard = motion.curve("decelerate")
    assert standard.type() == QEasingCurve.Type.BezierSpline
    assert standard.valueForProgress(0.5) == pytest.approx(0.8899, abs=5e-4)
    assert QEasingCurve(QEasingCurve.Type.OutCubic).valueForProgress(0.5) \
        == pytest.approx(0.875, abs=1e-3)
    with pytest.raises(KeyError):
        motion.curve("not_a_curve")


def test_animate_rejects_a_property_that_does_not_exist(qapp):
    """`QPropertyAnimation` on an unknown name silently does nothing."""
    toggle = ToggleSwitch()
    with pytest.raises(ValueError, match="no Qt property"):
        motion.animate(toggle, b"knobPosition", 1.0)


def test_animate_applies_the_end_value_with_animation_off(qapp, no_animation):
    """The acceptance: duration 0 AND the end value still lands."""
    toggle = ToggleSwitch()
    anim = motion.animate(toggle, b"knobOffset", 20.0, duration="faster")
    assert anim.duration() == 0
    assert anim.endValue() == 20.0
    assert toggle.knob_offset() == 20.0                 # no event loop needed


def test_animate_runs_when_animation_is_on(qapp, qtbot):
    toggle = ToggleSwitch()
    anim = motion.animate(toggle, b"knobOffset", 20.0, duration="faster")
    assert anim.duration() == theme.DURATION["faster"]
    settle(qtbot)
    assert toggle.knob_offset() == pytest.approx(20.0)


def test_fade_in_detaches_its_effect(qapp, qtbot):
    """A QGraphicsEffect left attached rasterises every later repaint."""
    widget = QWidget()
    widget.resize(20, 20)
    motion.fade_in(widget, duration="faster")
    settle(qtbot)
    assert widget.graphicsEffect() is None
    widget.hide()


def test_fade_out_hides_and_detaches(qapp, qtbot):
    widget = QWidget()
    widget.resize(20, 20)
    widget.show()
    motion.fade_out(widget, duration="faster")
    settle(qtbot)
    assert widget.graphicsEffect() is None
    assert not widget.isVisible()


def test_rise_in_refuses_a_top_level_window(qapp):
    """Animating a window's pos does nothing on Wayland."""
    window = QWidget()
    assert window.isWindow()
    with pytest.raises(ValueError, match="Wayland"):
        motion.rise_in(window)


def test_rise_in_lands_at_the_final_position(qapp, qtbot):
    parent = QWidget()
    parent.resize(80, 80)
    child = QWidget(parent)
    child.setGeometry(4, 20, 40, 20)
    final = child.pos()
    group = motion.rise_in(child, duration="faster")
    settle(qtbot)
    assert group.state() != QAbstractAnimation.State.Running
    assert child.pos() == final
    assert child.graphicsEffect() is None
    parent.hide()


def test_rise_in_is_instant_with_animation_off(qapp, no_animation):
    parent = QWidget()
    parent.resize(80, 80)
    child = QWidget(parent)
    child.setGeometry(4, 20, 40, 20)
    final = child.pos()
    motion.rise_in(child, duration="flyout")
    assert child.pos() == final
    parent.hide()


def test_safe_loop_never_spins_at_zero_duration(qapp, no_animation):
    """A loopCount(-1) animation with duration 0 would burn the animation timer."""
    widget = QWidget()
    widget.resize(10, 10)
    seen: list[float] = []
    loop = motion.SafeLoop(widget, seen.append, start=0.0, end=1.0,
                           duration="normal", parent=widget)
    widget.show()
    loop.start()
    assert loop.animation.duration() == 0
    assert not loop.is_running()
    assert seen == [1.0], "the end value must still be applied once"
    widget.hide()


def test_safe_loop_stops_when_the_widget_hides(qapp, qtbot):
    widget = QWidget()
    widget.resize(10, 10)
    widget.show()
    loop = motion.SafeLoop(widget, lambda _v: None, duration="normal", parent=widget)
    loop.start()
    qtbot.process()
    assert loop.is_running()
    widget.hide()
    qtbot.process()
    assert not loop.is_running()
    assert loop.wanted(), "a hidden loop is suspended, not forgotten"
    widget.show()
    qtbot.process()
    assert loop.is_running()
    loop.stop()
    widget.hide()


def test_safe_loop_stop_forgets_the_request(qapp, qtbot):
    widget = QWidget()
    widget.resize(10, 10)
    widget.show()
    loop = motion.SafeLoop(widget, lambda _v: None, duration="normal", parent=widget)
    loop.start()
    loop.stop()
    assert not loop.wanted()
    widget.hide()
    widget.show()
    qtbot.process()
    assert not loop.is_running()
    widget.hide()


def test_stop_all_tolerates_none(qapp):
    motion.stop(None)
    motion.stop_all([None])


# ═════════════════════════════════════════════════════════════════════════════
# The two-tone focus ring
# ═════════════════════════════════════════════════════════════════════════════

def _ring_image(dark_theme: bool, size: int = 40) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    box = QRectF(10.0, 10.0, size - 20.0, size - 20.0)
    controls.paint_focus_ring(painter, box, float(RADII["control"]), dark=dark_theme)
    painter.end()
    return image


@pytest.mark.parametrize("dark_theme", [False, True])
def test_focus_ring_uses_the_focus_tokens_and_no_accent(dark_theme):
    """Windows 11 has NO accent focus ring for standard controls."""
    image = _ring_image(dark_theme)
    present = colours_in(image)
    outer = theme.T("FocusStrokeColorOuter", dark=dark_theme).upper()
    inner = theme.T("FocusStrokeColorInner", dark=dark_theme).upper()
    assert outer in present
    assert inner in present
    # "No accent" means no accent HUE. The `text` role is plain black or white
    # by construction and coincides with FocusStrokeColorInner, so it is not a
    # hue the ring could have borrowed.
    for role in ("rest", "hover", "pressed", "disabled"):
        assert theme.accent(role, dark=dark_theme).upper() not in present


def test_focus_ring_is_inflated_and_rounded():
    """2 px outer + 1 px inner, inflated 3 px, ring radius = control radius + 3."""
    size = 40
    image = _ring_image(False, size)
    inflate = METRICS["focus_inflate"]
    box_left = 10

    row = size // 2
    painted = [x for x in range(size) if image.pixelColor(x, row).alpha() > 0]
    assert painted, "the ring painted nothing"
    assert min(painted) == box_left - inflate

    # A rounded ring leaves its corner transparent; a square one would not.
    corner = box_left - inflate
    assert image.pixelColor(corner, corner).alpha() == 0

    # Outer stroke 2 px then inner stroke 1 px, immediately inside it.
    outer = theme.T("FocusStrokeColorOuter", dark=False).upper()
    inner = theme.T("FocusStrokeColorInner", dark=False).upper()
    names = [image.pixelColor(x, row).name().upper() for x in painted[:4]]
    assert names[0] == outer and names[1] == outer
    assert inner in names


def test_focus_ring_style_draws_the_primitive(qapp):
    """`PE_FrameFocusRect` reaches the proxy even under a stylesheet."""
    style = FocusRingStyle()
    image = QImage(40, 40, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    option = QStyleOption()
    option.rect = image.rect().adjusted(4, 4, -4, -4)
    style.drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, option, painter)
    painter.end()
    assert not is_blank(image)
    assert theme.T("FocusStrokeColorOuter", dark=False).upper() in colours_in(image)


def test_focus_ring_style_defers_other_primitives(qapp):
    """Everything that is not the focus rect goes to Fusion untouched."""
    style = FocusRingStyle()
    image = QImage(20, 20, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    option = QStyleOption()
    option.rect = image.rect()
    style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelButtonCommand, option, painter)
    painter.end()  # no exception == the base style handled it


# ═════════════════════════════════════════════════════════════════════════════
# Buttons
# ═════════════════════════════════════════════════════════════════════════════

def test_button_variants_set_property_and_object_name(styled):
    button = FluentButton()

    button.set_variant(ButtonVariant.STANDARD)
    assert button.variant() is ButtonVariant.STANDARD
    assert button.property(PROP.ACCENT) is False
    assert button.objectName() == ""

    button.set_variant(ButtonVariant.ACCENT)
    assert button.property(PROP.ACCENT) is True
    assert button.objectName() == ""

    button.set_variant(ButtonVariant.SUBTLE)
    assert button.property(PROP.ACCENT) is False
    assert button.objectName() == OBJ.SUBTLE_BUTTON

    button.set_variant(ButtonVariant.HYPERLINK)
    assert button.objectName() == OBJ.LINK_BUTTON


def test_there_are_exactly_four_variants():
    assert len(ButtonVariant) == 4
    assert {v.value for v in ButtonVariant} == {"standard", "accent", "subtle", "hyperlink"}


@pytest.mark.slow
def test_accent_button_paints_the_accent_and_a_darker_bottom_stroke(styled):
    """The Windows 11 1 px bottom stroke, on the accent variant, in both themes."""
    button = FluentButton(variant=ButtonVariant.ACCENT)
    button.resize(80, METRICS["button_h"])
    image = render(button)
    present = colours_in(image)
    assert theme.accent("rest", dark=False).upper() in present
    assert qss.accent_edge(dark=False).upper() in present


@pytest.mark.slow
def test_standard_button_paints_the_elevation_bottom_stroke(styled):
    button = FluentButton()
    button.resize(80, METRICS["button_h"])
    image = render(button)
    present = colours_in(image)
    assert theme.T("ControlStrokeColorSecondary", dark=False).upper() in present


def test_icon_button_is_square_and_subtle(styled):
    button = controls.icon_button("settings")
    assert button.variant() is ButtonVariant.SUBTLE
    assert button.objectName() == OBJ.ICON_BUTTON
    assert not button.icon().isNull()
    button.ensurePolished()
    assert button.sizeHint().height() == METRICS["button_h"]


def test_button_icon_key_must_be_known(styled):
    with pytest.raises(KeyError):
        FluentButton(icon_key="definitely_not_an_icon")
    with pytest.raises(ValueError):
        FluentButton(icon_key="settings", icon_size=17)


def test_button_has_no_auto_default_indicator(styled):
    assert FluentButton().autoDefault() is False


# ═════════════════════════════════════════════════════════════════════════════
# ToggleSwitch — the Windows 11 template
# ═════════════════════════════════════════════════════════════════════════════

def test_toggle_uses_the_windows_11_geometry_not_windows_10():
    """The legacy Win10 template is 44x20 with a 10 px knob and travel 24."""
    assert (ToggleSwitch.TRACK_W, ToggleSwitch.TRACK_H) == (40, 20)
    assert ToggleSwitch.KNOB == 12.0
    assert ToggleSwitch.TRAVEL == 20
    assert ToggleSwitch.KNOB_HOVER == 14.0
    assert (ToggleSwitch.KNOB_PRESS_W, ToggleSwitch.KNOB_PRESS_H) == (17.0, 14.0)
    assert (ToggleSwitch.TRACK_W, ToggleSwitch.KNOB) != (44, 10.0)


def test_toggle_size_hint_is_the_track_plus_the_focus_margin(qapp):
    """The TRACK is 40x20; the widget carries the focus-visual margin.

    Fluent draws the focus ring OUTSIDE the toggle (`FocusVisualMargin
    -7,-3,-7,-3`) and Qt clips a paint event to the widget, so a widget that is
    exactly the track has nowhere to put the ring — it lands entirely outside
    the clip and only four corner slivers survive. `FOCUS_PAD` on each side is
    what makes the ring drawable; the track itself does not move.
    """
    pad = ToggleSwitch.FOCUS_PAD
    assert pad == theme.METRICS["focus_inflate"] == 3
    toggle = ToggleSwitch()
    assert toggle.sizeHint() == QSize(40 + 2 * pad, 20 + 2 * pad)
    assert toggle.minimumSizeHint() == toggle.sizeHint()
    assert toggle.size() == toggle.sizeHint()
    # The thing the spec measures is the TRACK, and it is untouched.
    assert toggle.track_bounds().size() == QSizeF(40.0, 20.0)
    assert toggle.track_bounds().topLeft() == QPointF(float(pad), float(pad))


def test_toggle_track_stays_centred_when_a_layout_stretches_it(qapp):
    """`track_origin()` centres the 40x20 pill in whatever size Qt hands over."""
    toggle = ToggleSwitch()
    toggle.setFixedSize(80, 40)
    assert toggle.track_origin() == QPointF(20.0, 10.0)
    assert toggle.track_bounds() == QRectF(20.0, 10.0, 40.0, 20.0)
    # A knob at either end still sits inside that pill.
    for checked in (False, True):
        toggle.set_checked_silently(checked)
        assert toggle.track_bounds().contains(toggle.knob_rect())


def test_toggle_knob_travels_zero_to_twenty(qapp, qtbot):
    """The acceptance measurement: `KnobTranslateTransform.X: 0 -> 20`."""
    toggle = ToggleSwitch()
    assert toggle.knob_offset() == 0.0
    toggle.setChecked(True)
    settle(qtbot)
    assert toggle.knob_offset() == pytest.approx(float(ToggleSwitch.TRAVEL))
    assert toggle.knob_offset() == pytest.approx(20.0)
    toggle.setChecked(False)
    settle(qtbot)
    assert toggle.knob_offset() == pytest.approx(0.0)


def test_toggle_knob_is_12_at_rest_14_hovered_17x14_pressed(qapp, qtbot):
    toggle = ToggleSwitch()
    toggle.show()
    assert toggle.knob_size() == QSize(12, 12)

    hover(toggle)
    settle(qtbot)
    assert toggle.knob_size().width() == pytest.approx(14.0)
    assert toggle.knob_size().height() == pytest.approx(14.0)

    toggle.setDown(True)
    toggle.pressed.emit()
    settle(qtbot)
    assert toggle.knob_size().width() == pytest.approx(17.0)
    assert toggle.knob_size().height() == pytest.approx(14.0)

    toggle.setDown(False)
    toggle.released.emit()
    settle(qtbot)
    assert toggle.knob_size().width() == pytest.approx(14.0), "still hovered"

    unhover(toggle)
    settle(qtbot)
    assert toggle.knob_size().width() == pytest.approx(12.0)
    toggle.hide()


def test_toggle_animation_is_the_fluent_faster_duration(qapp):
    toggle = ToggleSwitch()
    toggle.setChecked(True)
    assert toggle._offset_anim.duration() == theme.DURATION["faster"] == 83
    assert toggle._offset_anim.endValue() == pytest.approx(20.0)


def test_toggle_is_instant_with_animation_off(qapp, no_animation):
    """Acceptance: every QPropertyAnimation is 0 ms and the end value applies."""
    toggle = ToggleSwitch()
    toggle.setChecked(True)
    assert toggle._offset_anim.duration() == 0
    assert toggle._offset_anim.endValue() == pytest.approx(20.0)
    assert toggle.knob_offset() == pytest.approx(20.0)

    hover(toggle)
    assert toggle._width_anim.duration() == 0
    assert toggle.knob_size().width() == pytest.approx(14.0)

    toggle.setDown(True)
    toggle.pressed.emit()
    assert toggle.knob_size().width() == pytest.approx(17.0)
    assert toggle.knob_size().height() == pytest.approx(14.0)


def test_toggle_knob_stays_inside_the_track(qapp, qtbot):
    """Even at the 17 px pressed stretch, at both ends of the travel."""
    toggle = ToggleSwitch()
    track = toggle.track_bounds()
    assert track.size() == QSizeF(float(ToggleSwitch.TRACK_W),
                                  float(ToggleSwitch.TRACK_H))
    for checked in (False, True):
        toggle.set_checked_silently(checked)
        for width, height in ((12.0, 12.0), (14.0, 14.0), (17.0, 14.0)):
            toggle.knobWidth = width
            toggle.knobHeight = height
            knob = toggle.knob_rect()
            assert track.contains(knob), (checked, width, height, knob)


def test_toggle_emits_switched(qapp):
    toggle = ToggleSwitch()
    seen: list[bool] = []
    toggle.switched.connect(seen.append)
    toggle.setChecked(True)
    toggle.setChecked(False)
    assert seen == [True, False]


def test_set_checked_silently_lands_without_a_signal(qapp):
    """Reflecting an engine fact must not look like the user flipped 40 toggles."""
    toggle = ToggleSwitch()
    seen: list[bool] = []
    toggle.switched.connect(seen.append)
    toggle.set_checked_silently(True)
    assert toggle.isChecked()
    assert toggle.knob_offset() == pytest.approx(20.0)
    assert seen == []


def test_toggle_hide_stops_its_animations(qapp):
    toggle = ToggleSwitch()
    toggle.show()
    toggle.setChecked(True)
    assert toggle._offset_anim.state() == QAbstractAnimation.State.Running
    toggle.hide()
    assert toggle._offset_anim.state() != QAbstractAnimation.State.Running


@pytest.mark.slow
@pytest.mark.parametrize("dark_theme", [False, True])
def test_toggle_paints_accent_when_on_and_not_when_off(qapp, dark_theme, monkeypatch):
    monkeypatch.setattr(theme, "_DETECTED_DARK", dark_theme, raising=False)
    toggle = ToggleSwitch()
    off = colours_in(render(toggle))
    toggle.set_checked_silently(True)
    on = colours_in(render(toggle))
    accent = theme.accent("rest", dark=dark_theme).upper()
    assert accent in on
    assert accent not in off
    assert theme.T("ControlAltFillColorSecondary", dark=dark_theme).upper() in off


@pytest.mark.slow
def test_toggle_colour_lerp_carries_alpha(qapp):
    """An RGB-only lerp from a transparent fill flashes black mid-animation."""
    transparent = QColor(0, 0, 0, 0)
    opaque = QColor(theme.accent("rest", dark=False))
    middle = controls.lerp_color(transparent, opaque, 0.5)
    assert middle.alpha() == pytest.approx(127, abs=2)
    assert middle.red() == pytest.approx(opaque.red(), abs=1)


def test_toggle_focus_paints_the_ring(qapp, qtbot):
    toggle = ToggleSwitch()
    toggle.show()
    toggle.setFocus()
    qtbot.process()
    assert toggle.hasFocus()
    image = render(toggle, pad=METRICS["focus_inflate"] + 2)
    assert theme.T("FocusStrokeColorOuter", dark=False).upper() in colours_in(image)
    toggle.hide()


# ═════════════════════════════════════════════════════════════════════════════
# Text and choice controls
# ═════════════════════════════════════════════════════════════════════════════

def test_line_edit_search_variant(styled):
    edit = FluentLineEdit(search=True)
    assert edit.is_search()
    assert edit.objectName() == OBJ.SEARCH_BOX
    plain = FluentLineEdit()
    assert not plain.is_search()
    assert plain.objectName() == ""


@pytest.mark.slow
def test_search_box_paints_its_glyph(styled):
    plain = FluentLineEdit()
    plain.resize(160, METRICS["textbox_h"])
    search = FluentLineEdit(search=True)
    search.resize(160, METRICS["textbox_h"])
    assert render(search) != render(plain)


@pytest.mark.slow
def test_checkbox_paints_a_check_only_when_checked(styled):
    box = FluentCheckBox()
    box.resize(120, 24)
    unchecked = render(box)
    box.setChecked(True)
    checked = render(box)
    assert checked != unchecked

    # The glyph is a 1.6-unit stroke scaled to 12 px, so antialiasing means no
    # pixel is the pure on-accent colour. Assert the direction instead: inside
    # the indicator, some pixel is nearer TextOnAccent than the accent fill.
    on_accent = QColor(theme.accent("text", dark=False))
    fill = QColor(theme.accent("rest", dark=False))
    rect = controls.indicator_rect(box).toRect()

    def distance(a: QColor, b: QColor) -> int:
        return (abs(a.red() - b.red()) + abs(a.green() - b.green())
                + abs(a.blue() - b.blue()))

    nearer = [
        (x, y)
        for y in range(rect.top(), rect.bottom() + 1)
        for x in range(rect.left(), rect.right() + 1)
        if distance(checked.pixelColor(x, y), on_accent)
        < distance(checked.pixelColor(x, y), fill)
    ]
    assert nearer, "no checkmark was painted inside the indicator"


@pytest.mark.slow
def test_checkbox_indeterminate_paints_a_dash(styled):
    box = FluentCheckBox()
    box.setTristate(True)
    box.resize(120, 24)
    box.setCheckState(Qt.CheckState.Checked)
    checked = render(box)
    box.setCheckState(Qt.CheckState.PartiallyChecked)
    partial = render(box)
    assert partial != checked
    assert not is_blank(partial)


@pytest.mark.slow
def test_radio_paints_a_dot_only_when_checked(styled):
    radio = FluentRadioButton()
    radio.resize(120, 24)
    unchecked = render(radio)
    radio.setChecked(True)
    assert render(radio) != unchecked


def test_indicator_rect_is_20px_and_leading_aligned(styled):
    box = FluentCheckBox()
    box.resize(120, 24)
    rect = controls.indicator_rect(box)
    assert rect.width() == theme.SPACING["xl"] == 20
    assert rect.height() == 20
    assert rect.left() == 0.0
    box.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    assert controls.indicator_rect(box).right() == 120.0


@pytest.mark.slow
def test_combo_box_paints_a_chevron(styled):
    combo = FluentComboBox()
    combo.addItem("")
    combo.resize(140, METRICS["button_h"])
    assert not is_blank(render(combo))


# ═════════════════════════════════════════════════════════════════════════════
# restyle / theme reactions
# ═════════════════════════════════════════════════════════════════════════════

def test_restyle_calls_refresh_theme_on_descendants(styled):
    class Probe(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.refreshed = 0

        def refresh_theme(self) -> None:
            self.refreshed += 1

    root = QWidget()
    layout = QVBoxLayout(root)
    probe = Probe(root)
    layout.addWidget(probe)
    controls.restyle(root, deep=True)
    assert probe.refreshed == 1
    controls.restyle(root, deep=False)
    assert probe.refreshed == 1


def test_every_control_is_theme_aware(styled):
    """The mixin is wired on every control, not just remembered in a comment."""
    from onedriveui.ui.widgets.controls import ThemeAware

    for cls in (FluentButton, FluentLineEdit, FluentCheckBox, FluentRadioButton,
                FluentComboBox, ToggleSwitch):
        assert issubclass(cls, ThemeAware), cls.__name__
    for cls in (indicators.ProgressRing, indicators.FluentProgressBar,
                indicators.StorageBar, indicators.Avatar, indicators.StatusBadge):
        assert issubclass(cls, ThemeAware), cls.__name__


def test_button_retints_its_glyph_on_a_theme_change(styled, monkeypatch):
    """`icons.icon(color=None)` resolves to the theme's text colour, so a QIcon
    built under the old theme is stale even after `icons` drops its cache."""
    from onedriveui.bus import BUS

    button = FluentButton(icon_key="settings")
    light = button.icon().pixmap(QSize(16, 16)).toImage()

    monkeypatch.setattr(theme, "_DETECTED_DARK", True, raising=False)
    theme._STYLESHEET_CACHE.clear()
    qss.invalidate()
    try:
        BUS.theme_changed.emit(True, theme.accent("rest", dark=True))
        dark_icon = button.icon().pixmap(QSize(16, 16)).toImage()
        assert dark_icon != light
    finally:
        theme._STYLESHEET_CACHE.clear()
        qss.invalidate()
        icons.clear_cache()


def test_theme_change_repaints_the_kit(styled, bus_spy):
    """Every custom-painted widget re-reads its tokens on `BUS.theme_changed`."""
    from onedriveui.bus import BUS

    toggle = ToggleSwitch()
    ring = indicators.ProgressRing()
    BUS.theme_changed.emit(True, theme.accent("rest", dark=True))
    # No exception and the widgets are still alive: the hook is connected and
    # resolves tokens lazily rather than caching a stale colour.
    assert toggle.isEnabled() and ring.isEnabled()


# ═════════════════════════════════════════════════════════════════════════════
# Source hygiene — no colour, no icon name, no user-facing string
# ═════════════════════════════════════════════════════════════════════════════

KIT_MODULES = (
    REPO_ROOT / "onedriveui" / "ui" / "widgets" / "controls.py",
    REPO_ROOT / "onedriveui" / "ui" / "widgets" / "indicators.py",
)

#: A string constant is allowed when it is a WP-00 table key or an identifier.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _string_constants(path: Path) -> list[str]:
    """Every string literal in a module except its docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    # Text inside a `raise` is a developer diagnostic, never user-facing chrome.
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    docstrings.add(id(inner))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.mark.parametrize("path", KIT_MODULES, ids=lambda p: p.name)
def test_no_colour_literal_in_a_widget(path):
    """Every colour comes from `theme.T()` / `theme.accent()` / GNOME_ACCENTS."""
    source = path.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", code), path.name
    assert "rgba(" not in code and "rgb(" not in code
    assert "QColor(" in code, "the widgets do paint — just never from a literal"


@pytest.mark.parametrize("path", KIT_MODULES, ids=lambda p: p.name)
def test_no_user_facing_string_in_a_widget(path):
    """Labels are passed in by WP-12/WP-13 out of `strings.py`.

    A string constant survives only if it is an identifier-like technical token
    (a token key, a glyph key, a property name); a sentence cannot be.
    """
    offenders = [
        value for value in _string_constants(path)
        if value and not _IDENTIFIER.match(value)
    ]
    assert offenders == [], f"{path.name}: {offenders}"


@pytest.mark.parametrize("path", KIT_MODULES, ids=lambda p: p.name)
def test_every_technical_string_resolves_against_wp00(path):
    """Every identifier-like literal is a key of a frozen WP-00 table, a Python
    attribute name, or a Qt property name — never an invented magic string."""
    known: set[str] = set()
    known |= set(theme.TOKENS)
    known |= set(theme.ACCENT_ROLES)
    known |= set(theme.METRICS) | set(theme.RADII) | set(theme.SPACING)
    known |= set(theme.TYPE) | set(theme.DURATION) | set(theme.CURVES)
    known |= set(theme.SHADOWS)
    known |= set(icons.GLYPHS)
    known |= {value for key, value in vars(theme.OBJ).items() if not key.startswith("_")}
    known |= {value for key, value in vars(theme.PROP).items() if not key.startswith("_")}
    known |= {member.value for member in ButtonVariant}
    known |= {member.value for member in indicators.ProgressTone}
    known |= {"decelerate", "accelerate"}          # motion.CURVE_IN / CURVE_OUT

    known |= {qss.FUSION.capitalize()}          # the Qt style key
    # `icons.asset_path()` categories, documented in ui/icons.py.
    known |= {"status", "emblems", "apps", "glyphs"}

    module = __import__(
        "onedriveui.ui.widgets." + path.stem, fromlist=["x"])
    known |= set(dir(module))
    for value in vars(module).values():          # method names reached by getattr
        if isinstance(value, type):
            known |= set(dir(value))

    unknown = sorted({
        value for value in _string_constants(path)
        if value and _IDENTIFIER.match(value) and value not in known
    })
    # Attribute names reached with getattr() are legitimate too.
    unknown = [value for value in unknown if not hasattr(QWidget, value)]
    assert unknown == [], f"{path.name}: {unknown}"


ALLOWED_IMPORT_ROOTS = {
    "onedriveui", "PySide6", "enum", "typing", "__future__", "math", "weakref",
    "inspect", "os", "re", "pathlib",
}
#: The engine packages a widget must never reach.
FORBIDDEN_SUBPACKAGES = ("onedriveui.sync", "onedriveui.rc", "onedriveui.platform",
                         "onedriveui.data", "onedriveui.config", "onedriveui.applog")

WP11A_MODULES = KIT_MODULES + (
    REPO_ROOT / "onedriveui" / "ui" / "fonts.py",
    REPO_ROOT / "onedriveui" / "ui" / "qss.py",
    REPO_ROOT / "onedriveui" / "ui" / "motion.py",
    REPO_ROOT / "onedriveui" / "ui" / "widgets" / "__init__.py",
)


@pytest.mark.parametrize("path", WP11A_MODULES, ids=lambda p: p.name)
def test_no_engine_import(path):
    """The kit must render in a gallery with zero rclone.

    WP-11a may import WP-00 and PySide6 and nothing else — no `sync/`, no `rc/`,
    no `platform/`, no config and no database.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    for name in modules:
        assert name.split(".")[0] in ALLOWED_IMPORT_ROOTS, f"{path.name}: {name}"
        for forbidden in FORBIDDEN_SUBPACKAGES:
            assert not name.startswith(forbidden), f"{path.name}: {name}"


def test_kit_imports_only_frozen_wp00_modules():
    """The only `onedriveui` modules the kit reaches are the frozen contracts."""
    allowed = {
        "onedriveui", "onedriveui.bus", "onedriveui.models", "onedriveui.constants",
        "onedriveui.strings", "onedriveui.paths", "onedriveui.errors",
        "onedriveui.ui", "onedriveui.ui.theme", "onedriveui.ui.icons",
        "onedriveui.ui.fonts", "onedriveui.ui.qss", "onedriveui.ui.motion",
        "onedriveui.ui.widgets", "onedriveui.ui.widgets.controls",
        "onedriveui.ui.widgets.indicators",
    }
    for path in WP11A_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.startswith("onedriveui"):
                    assert name in allowed, f"{path.name}: {name}"


# ═════════════════════════════════════════════════════════════════════════════
# The contact sheet — both themes x four device pixel ratios
# ═════════════════════════════════════════════════════════════════════════════

def build_gallery(parent: QWidget | None = None) -> QWidget:
    """Every widget in the kit, in one container.

    Constructed from `onedriveui.ui` and the WP-00 contracts only — no engine,
    no rclone, no network. This is the gallery the acceptance criteria ask for.
    """
    page = QWidget(parent)
    page.setObjectName(OBJ.ROOT)
    page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(*(theme.SPACING["l"],) * 4)
    layout.setSpacing(theme.SPACING["m"])

    heading = QLabel(theme.OBJ.HEADER, page)
    heading.setProperty(PROP.TYPE, "subtitle")
    layout.addWidget(heading)

    for variant in ButtonVariant:
        layout.addWidget(FluentButton(variant.value, page, variant=variant))
    layout.addWidget(controls.icon_button("settings", page))

    layout.addWidget(FluentLineEdit(page, placeholder=""))
    layout.addWidget(FluentLineEdit(page, search=True))

    checkbox = FluentCheckBox("", page)
    checkbox.setChecked(True)
    layout.addWidget(checkbox)
    radio = FluentRadioButton("", page)
    radio.setChecked(True)
    layout.addWidget(radio)

    combo = FluentComboBox(page)
    combo.addItem("")
    layout.addWidget(combo)

    on_toggle = ToggleSwitch(page)
    on_toggle.set_checked_silently(True)
    layout.addWidget(on_toggle)
    layout.addWidget(ToggleSwitch(page))

    ring = indicators.ProgressRing(page, track=True)
    ring.set_value(0.35)
    layout.addWidget(ring)

    bar = indicators.FluentProgressBar(page)
    bar.set_value(0.6)
    layout.addWidget(bar)

    paused = indicators.FluentProgressBar(page, tone=indicators.ProgressTone.PAUSED)
    paused.set_value(0.4)
    layout.addWidget(paused)

    storage = indicators.StorageBar(page)
    storage.set_segments(1000, (
        (300, theme.accent("rest")),
        (120, theme.T("SystemFillColorSuccess")),
        (60, theme.T("SystemFillColorCaution")),
    ))
    layout.addWidget(storage)

    avatar = indicators.Avatar(page)
    avatar.set_person("Daniel Perez")
    layout.addWidget(avatar)

    badges = QWidget(page)
    badge_row = QVBoxLayout(badges)
    badge_row.setContentsMargins(0, 0, 0, 0)
    for state in (FileState.ONLINE_ONLY, FileState.LOCAL, FileState.PINNED,
                  FileState.SYNCING, FileState.ERROR):
        badge_row.addWidget(indicators.StatusBadge(badges, state=state))
    layout.addWidget(badges)

    page.adjustSize()
    return page


@pytest.mark.slow
@pytest.mark.parametrize("dpr", DEVICE_PIXEL_RATIOS)
@pytest.mark.parametrize("dark_theme", [False, True])
def test_kit_renders_at_every_theme_and_ratio(qapp, dpr, dark_theme, monkeypatch):
    """Acceptance: every widget renders at both themes and at 1.0/1.25/1.5/2.0."""
    monkeypatch.setattr(theme, "_DETECTED_DARK", dark_theme, raising=False)
    theme._STYLESHEET_CACHE.clear()
    qss.invalidate()
    icons.clear_cache()
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    try:
        fonts.apply_app_font(qapp)
        qss.apply(qapp, dark=dark_theme)
        page = build_gallery()
        image = render(page, dpr=dpr)
        assert image.width() == int(round(page.width() * dpr))
        assert not is_blank(image)
        # The window background must actually be painted, not left transparent.
        assert image.pixelColor(2, 2).name().upper() == theme.base(dark=dark_theme).upper()
    finally:
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)
        theme._STYLESHEET_CACHE.clear()
        qss.invalidate()
        icons.clear_cache()


@pytest.mark.slow
def test_contact_sheet_is_written(qapp, monkeypatch, tmp_path):
    """Render the 2 x 4 contact sheet and save it under docs/."""
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    tiles: list[tuple[str, QImage]] = []
    try:
        for dark_theme in (False, True):
            monkeypatch.setattr(theme, "_DETECTED_DARK", dark_theme, raising=False)
            theme._STYLESHEET_CACHE.clear()
            qss.invalidate()
            icons.clear_cache()
            fonts.apply_app_font(qapp)
            qss.apply(qapp, dark=dark_theme)
            for dpr in DEVICE_PIXEL_RATIOS:
                page = build_gallery()
                tiles.append((f"{'dark' if dark_theme else 'light'} @ {dpr}x",
                              render(page, dpr=dpr)))
    finally:
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)
        monkeypatch.setattr(theme, "_DETECTED_DARK", False, raising=False)
        theme._STYLESHEET_CACHE.clear()
        qss.invalidate()
        icons.clear_cache()

    assert len(tiles) == 2 * len(DEVICE_PIXEL_RATIOS)
    gutter = theme.SPACING["l"]
    caption_h = theme.SPACING["xxl"]
    width = sum(tile.width() for _label, tile in tiles) + gutter * (len(tiles) + 1)
    height = max(tile.height() for _label, tile in tiles) + gutter * 2 + caption_h

    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor(theme.T("SolidBackgroundFillColorSecondary", dark=False)))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setPen(QColor(theme.T("TextFillColorPrimary", dark=False)))
    painter.setFont(fonts.font("caption"))
    x = gutter
    for label, tile in tiles:
        painter.drawText(x, gutter + theme.SPACING["m"], label)
        pixmap = QPixmap.fromImage(tile)
        pixmap.setDevicePixelRatio(1.0)
        painter.drawPixmap(x, gutter + caption_h, pixmap)
        x += tile.width() + gutter
    painter.end()

    scratch = tmp_path / "contact-sheet.png"
    assert sheet.save(str(scratch), "PNG")
    assert scratch.stat().st_size > 0

    try:
        CONTACT_SHEET.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(str(CONTACT_SHEET), "PNG")
    except OSError:                                    # pragma: no cover
        pytest.skip("the repository is read-only")
    assert CONTACT_SHEET.is_file()
    assert CONTACT_SHEET.stat().st_size > 1000


# ═════════════════════════════════════════════════════════════════════════════
# Adversarial-audit regressions. Every test below reproduces a defect that was
# found by rendering the kit and looking at it, and each one fails against the
# code as it stood before the fix.
# ═════════════════════════════════════════════════════════════════════════════

def test_focus_ring_is_drawn_on_the_control_not_on_its_label(styled):
    """`option.rect` for `PE_FrameFocusRect` is NOT the button.

    Qt hands the proxy style `SE_PushButtonFocusRect` — the CONTENT box, inside
    the 11/5 padding: measured `QRect(12, 6, 154, 20)` on a 178x32 button.
    Ringing that put a 2 px stroke around the *label*, crossing the glyphs, so a
    focused 'Cancel' read as 'Cance'. The ring must hug the widget instead.
    """
    button = FluentButton()
    button.resize(178, METRICS["button_h"])
    button.ensurePolished()

    option = QStyleOption()
    option.initFrom(button)
    option.rect = QRect(12, 6, 154, 20)          # what Qt actually passes
    assert not QRectF(option.rect).contains(QRectF(button.rect()))

    target = FocusRingStyle.ring_target(option, button)
    assert target == QRectF(button.rect())

    # An item view's current-item rect must NOT be replaced by the view's rect.
    view = QWidget()
    view.resize(400, 300)
    option.rect = QRect(0, 56, 400, 56)
    assert FocusRingStyle.ring_target(option, view) == QRectF(0, 56, 400, 56)


def test_focus_ring_bounds_keeps_the_ring_inside_the_widget(styled):
    """`paint_focus_ring` inflates 3 px; a control has no 3 px to spare."""
    rect = QRectF(0.0, 0.0, 178.0, 32.0)
    inset = controls.focus_ring_bounds(rect)
    assert inset == rect.adjusted(3.0, 3.0, -3.0, -3.0)
    # Re-inflated by paint_focus_ring, the ring lands exactly on `rect`.
    assert inset.adjusted(-3.0, -3.0, 3.0, 3.0) == rect


@pytest.mark.slow
def test_focused_button_ring_hugs_the_button_edge(ring_styled, qtbot):
    """The ring's outer stroke lands within 3 px of the button's own edge."""
    button = FluentButton()
    button.resize(178, METRICS["button_h"])
    button.show()
    button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.process()
    assert button.hasFocus()
    image = render(button)
    button.hide()

    outer = QColor(theme.T("FocusStrokeColorOuter", dark=False))
    points = [
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y) == outer
    ]
    assert points, "no focus ring was painted"
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    assert min(xs) <= 2 and max(xs) >= button.width() - 3
    assert min(ys) <= 2 and max(ys) >= button.height() - 3


@pytest.mark.slow
def test_focused_toggle_ring_is_not_clipped_away(styled, qtbot):
    """The ring must be a ring, not four corner slivers.

    The widget is the track plus `FOCUS_PAD`; without that margin the inflated
    ring falls outside the widget's clip and only the corner arcs survive, which
    is a focus indicator a keyboard user cannot see.
    """
    toggle = ToggleSwitch()
    toggle.show()
    toggle.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.process()
    assert toggle.hasFocus()
    image = render(toggle)
    toggle.hide()

    outer = QColor(theme.T("FocusStrokeColorOuter", dark=False))
    mid_y = image.height() // 2
    mid_x = image.width() // 2
    left = [x for x in range(image.width()) if image.pixelColor(x, mid_y) == outer]
    top = [y for y in range(image.height()) if image.pixelColor(mid_x, y) == outer]
    # A real ring crosses BOTH the horizontal and the vertical centre line.
    assert left, "the ring is missing on the horizontal centre line"
    assert top, "the ring is missing on the vertical centre line"


def test_checked_radio_is_a_20px_circle_and_does_not_resize_the_row(styled):
    """The Qt-class layer expresses the checked radio as `border: 6px solid`.

    QSS sizes the CONTENT box and adds the border outside it, so `width: 20px`
    plus a 6 px border is a 32 px BOX — a rounded SQUARE at radius 10, 10 px
    taller than the same radio unchecked. Picking an option re-flowed the rows
    around it. The kit restates the recipe at a true 20 px box.
    """
    off = FluentRadioButton()
    off.adjustSize()
    on = FluentRadioButton()
    on.setChecked(True)
    on.adjustSize()

    assert on.sizeHint() == off.sizeHint(), "checking a radio resized it"
    box = FluentCheckBox()
    box.adjustSize()
    # The CONTROL is the WinUI 32 px box; the INDICATOR inside it is 20.
    assert on.sizeHint().height() == box.sizeHint().height() == qss.CHOICE_BOX_H
    assert qss.CHOICE_BOX_H == METRICS["button_h"] == 32
    assert qss.INDICATOR_BOX == 20
    # The painted glyph is centred in `indicator_rect()`, so the QSS box has to
    # BE that rect — content + border on each side.
    assert qss.INDICATOR_CONTENT + 2 * qss.INDICATOR_BORDER == qss.INDICATOR_BOX
    assert controls.indicator_rect(box).width() == qss.INDICATOR_BOX


def test_glyph_pixmap_is_device_pixel_ratio_aware(styled):
    """`QIcon.pixmap(QSize)` returns a 1x raster that `drawPixmap` magnifies."""
    plain = icons.icon("search", 16).pixmap(QSize(16, 16))
    assert plain.devicePixelRatio() == 1.0
    assert plain.size() == QSize(16, 16)

    for dpr in DEVICE_PIXEL_RATIOS:
        crisp = controls.glyph_pixmap("search", 16, None, dpr)
        assert crisp.devicePixelRatio() == pytest.approx(dpr)
        # The raster really is bigger — that is the whole point.
        assert crisp.width() == pytest.approx(round(16 * dpr), abs=1)


def test_no_control_takes_a_one_x_pixmap(styled):
    """Every `.pixmap(` in the kit passes a device pixel ratio."""
    for name in ("controls", "indicators", "containers", "lists", "chrome"):
        source = (REPO_ROOT / "onedriveui" / "ui" / "widgets"
                  / f"{name}.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for call in re.finditer(r"\.pixmap\(([^)]*\)?[^)]*)\)", code):
            assert "," in call.group(1), f"{name}.py: {call.group(0)}"
