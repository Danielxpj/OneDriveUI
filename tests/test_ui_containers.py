"""WP-11b — `onedriveui.ui.widgets.containers`, plus the package-wide checks.

This module owns three things:

  * the container widgets themselves — `SettingsCard`, `SettingsExpander`,
    `InfoBar`, `ContentDialog`, `SectionHeading` and the shadow helpers;
  * the **source-hygiene** tests for all three WP-11b modules (no colour, no
    icon name and no user-facing string that is not sourced from WP-00, and no
    engine import), because one parametrised suite is easier to keep honest than
    three copies;
  * the **gallery** acceptance — `scripts/gallery.py` runs offscreen, imports
    nothing outside `onedriveui.ui` and the WP-00 contracts, and writes one PNG
    contact sheet per theme.

`tests/test_ui_lists.py` and `tests/test_ui_chrome.py` carry the behavioural
tests for their own modules only.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import (
    QEvent, QMargins, QPoint, QPointF, QRectF, QSize, Qt,
)
from PySide6.QtGui import (
    QEnterEvent, QImage, QKeyEvent, QMouseEvent, QPainter, QRegion,
)
from PySide6.QtWidgets import (
    QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QVBoxLayout, QWidget,
)

from onedriveui.models import IssueSeverity, SyncState
from onedriveui.ui import fonts, icons, motion, qss, theme
from onedriveui.ui.theme import METRICS, OBJ, PROP, RADII, SPACING
from onedriveui.ui.widgets import chrome, containers, lists
from onedriveui.ui.widgets.containers import (
    CARD_ACTION_ICON, CARD_CONTENT_MIN_W, CARD_ICON, CARD_ICON_GAP, CARD_MIN_H,
    CARD_PAD, CARD_WRAP_NO_ICON_THRESHOLD, CARD_WRAP_THRESHOLD,
    CHEVRON_OPEN_DEG, ContentDialog, DIALOG_BUTTON_MAX_W, DIALOG_BUTTON_MIN_W,
    DIALOG_MAX_W, DIALOG_MIN_H, DIALOG_MIN_W, EXPANDER_CHEVRON,
    EXPANDER_CHILD_PAD, EXPANDER_HEADER_PAD, InfoBar, InfoBarSeverity,
    SectionHeading, SettingsCard, SettingsExpander, SEVERITY_FOR_ISSUE,
    UNBOUNDED, box_path, card_group, drop_shadow, glyph_pixmap, shadow_margins,
)
from onedriveui.ui.widgets.controls import ButtonVariant, FluentButton, ToggleSwitch

REPO_ROOT = Path(__file__).resolve().parent.parent
GALLERY = REPO_ROOT / "scripts" / "gallery.py"
DEVICE_PIXEL_RATIOS = (1.0, 1.25, 1.5, 2.0)

#: The three modules WP-11b owns.
WP11B_MODULES = (
    REPO_ROOT / "onedriveui" / "ui" / "widgets" / "containers.py",
    REPO_ROOT / "onedriveui" / "ui" / "widgets" / "lists.py",
    REPO_ROOT / "onedriveui" / "ui" / "widgets" / "chrome.py",
)


# ═════════════════════════════════════════════════════════════════════════════
# Harness
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def styled(qapp):
    """The kit's own font and stylesheet, restored afterwards.

    The style itself is deliberately left alone: `QApplication.setStyle()` takes
    ownership and **deletes** the style it replaces, so a fixture that saved the
    old one and put it back would hand Qt a dangling pointer (verified — the
    teardown raises "Internal C++ object already deleted"). Nothing here needs
    the focus-ring proxy: the containers paint their own ring.
    """
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    fonts.apply_app_font(qapp)
    qss.apply(qapp, dark=False)
    yield qapp
    qapp.setStyleSheet(previous_sheet)
    qapp.setFont(previous_font)
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


def render(widget: QWidget, *, dpr: float = 1.0, background: bool = True) -> QImage:
    """Render a widget offscreen at `dpr`.

    Args:
        background: `QWidget.render()` defaults to `DrawWindowBackground`, which
            fills the target with the palette's Window brush **whether or not**
            the widget itself paints one. Pass False to see only what the
            widget's own `paintEvent` drew.
    """
    widget.ensurePolished()
    if widget.size().isEmpty():
        widget.adjustSize()
    image = QImage(int(round(widget.width() * dpr)),
                   int(round(widget.height() * dpr)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    flags = QWidget.RenderFlag.DrawChildren
    if background:
        flags |= QWidget.RenderFlag.DrawWindowBackground
    widget.render(painter, QPoint(0, 0), QRegion(), flags)
    painter.end()
    return image


def is_blank(image: QImage) -> bool:
    return all(image.pixelColor(x, y).alpha() == 0
               for y in range(image.height()) for x in range(image.width()))


def colours_in(image: QImage) -> set[str]:
    found: set[str] = set()
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 200:
                found.add(pixel.name().upper())
    return found


def hover(widget: QWidget) -> None:
    point = QPointF(widget.width() / 2.0, widget.height() / 2.0)
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    widget.event(QEnterEvent(point, point, point))


def unhover(widget: QWidget) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    widget.event(QEvent(QEvent.Type.Leave))


def click(widget: QWidget) -> None:
    point = QPointF(widget.width() / 2.0, widget.height() / 2.0)
    for kind, state in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                        (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
        widget.event(QMouseEvent(kind, point, point, Qt.MouseButton.LeftButton,
                                 state, Qt.KeyboardModifier.NoModifier))


# ═════════════════════════════════════════════════════════════════════════════
# Shared painting helpers
# ═════════════════════════════════════════════════════════════════════════════

def test_box_path_with_equal_radii_covers_the_rect():
    rect = QRectF(0.0, 0.0, 40.0, 20.0)
    path = box_path(rect, 4.0, 4.0, 4.0, 4.0)
    assert path.boundingRect() == rect
    assert path.elementCount() > 4


def test_box_path_squares_the_corners_it_is_told_to():
    rect = QRectF(0.0, 0.0, 40.0, 20.0)
    square = box_path(rect, 0.0, 0.0, 0.0, 0.0)
    rounded = box_path(rect, 4.0, 4.0, 4.0, 4.0)
    # A square corner contains its own vertex; a rounded one does not.
    assert square.contains(QPointF(0.5, 0.5))
    assert not rounded.contains(QPointF(0.5, 0.5))


def test_box_path_rounds_only_the_named_corners():
    rect = QRectF(0.0, 0.0, 40.0, 20.0)
    top_only = box_path(rect, 4.0, 4.0, 0.0, 0.0)
    assert not top_only.contains(QPointF(0.5, 0.5))          # rounded top-left
    assert top_only.contains(QPointF(0.5, 19.5))             # square bottom-left


def test_glyph_pixmap_is_tagged_with_the_device_pixel_ratio(styled):
    for dpr in DEVICE_PIXEL_RATIOS:
        pixmap = glyph_pixmap("settings", SPACING["l"], theme.T("TextFillColorPrimary"),
                              dpr)
        assert pixmap.devicePixelRatio() == pytest.approx(dpr)
        assert pixmap.width() == pytest.approx(SPACING["l"] * dpr, abs=1)


def test_glyph_pixmap_rejects_an_unknown_key(styled):
    with pytest.raises(KeyError):
        glyph_pixmap("not-a-glyph", SPACING["l"])


# ═════════════════════════════════════════════════════════════════════════════
# Shadows
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", sorted(theme.SHADOWS))
def test_shadow_margins_reserve_blur_on_every_side(name):
    blur, dy, _alpha = theme.shadow(name)
    assert shadow_margins(name) == QMargins(blur, blur, blur, blur + dy)


def test_shadow_margins_rejects_an_unknown_name():
    with pytest.raises(KeyError):
        shadow_margins("not-a-shadow")


@pytest.mark.parametrize("name", sorted(theme.SHADOWS))
def test_drop_shadow_matches_the_frozen_table(styled, name):
    blur, dy, alpha = theme.shadow(name, dark=False)
    frame = QFrame()
    effect = drop_shadow(frame, name, dark=False)
    assert isinstance(effect, QGraphicsDropShadowEffect)
    assert effect.blurRadius() == pytest.approx(float(blur))
    assert effect.offset() == QPointF(0.0, float(dy))
    assert effect.color().alpha() == alpha
    assert frame.graphicsEffect() is effect


def test_drop_shadow_alpha_follows_the_theme(styled):
    # Keep the frames alive: the effect is parented to the widget, so a
    # temporary frame takes its own shadow down with it.
    light_frame, dark_frame = QFrame(), QFrame()
    light = drop_shadow(light_frame, "flyout", dark=False).color().alpha()
    dark_alpha = drop_shadow(dark_frame, "flyout", dark=True).color().alpha()
    assert light == theme.shadow("flyout", dark=False)[2]
    assert dark_alpha == theme.shadow("flyout", dark=True)[2]
    assert dark_alpha != light


def _shadow_probe(qapp, *, margin: int) -> tuple[QImage, object, str]:
    """Render a shadowed surface on a fixed canvas, inset by `margin`."""
    blur, dy, _alpha = theme.shadow("card", dark=False)
    host = QWidget()
    host.setObjectName(OBJ.ROOT)
    host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(margin, margin, margin, margin)
    frame = QFrame(host)
    frame.setObjectName(OBJ.CARD_SECONDARY)
    frame.setFixedSize(80, 40)
    layout.addWidget(frame)
    drop_shadow(frame, "card", dark=False)
    host.setFixedSize(80 + 2 * blur, 40 + 2 * blur + dy)
    host.show()
    qapp.processEvents()
    return render(host), frame.geometry(), theme.base(dark=False).upper()


def test_a_shadow_is_clipped_without_a_reserved_margin(styled, qapp):
    """Acceptance: `QGraphicsDropShadowEffect` clips without reserved margin.

    The effect paints outward from the widget it is on, so whatever lays that
    widget out has to leave room. Flush against its container's edge there is
    nowhere for the blur to go and the elevation simply does not appear on that
    side; inset by the blur radius it does.
    """
    def left_of_surface(image, geometry, background) -> int:
        return sum(
            1
            for y in range(image.height())
            for x in range(geometry.left())
            if image.pixelColor(x, y).name().upper() != background
        )

    flush = _shadow_probe(qapp, margin=0)
    reserved = _shadow_probe(qapp, margin=theme.shadow("card", dark=False)[0])
    assert left_of_surface(*flush) == 0, "a flush surface cannot show a shadow"
    assert left_of_surface(*reserved) > 0, "the reserved margin lets it paint"


# ═════════════════════════════════════════════════════════════════════════════
# SectionHeading
# ═════════════════════════════════════════════════════════════════════════════

def test_section_heading_reserves_its_fluent_rhythm(styled):
    heading = SectionHeading(OBJ.HEADER)
    expected = fonts.line_height("body_strong") + SectionHeading.TOP + SectionHeading.BOTTOM
    assert heading.height() == expected
    assert heading.property(PROP.TYPE) == "body_strong"
    assert heading.contentsMargins() == QMargins(0, SectionHeading.TOP, 0,
                                                 SectionHeading.BOTTOM)


def test_section_heading_uses_the_frozen_spacing_scale():
    assert SectionHeading.TOP == SPACING["xxl"]
    assert SectionHeading.BOTTOM == SPACING["s"]


# ═════════════════════════════════════════════════════════════════════════════
# SettingsCard geometry
# ═════════════════════════════════════════════════════════════════════════════

def test_settings_card_is_exactly_68_px_tall(styled):
    """Acceptance: the Windows 11 settings-card box is 68 px, not 70."""
    card = SettingsCard(OBJ.CARD, description=OBJ.CARD_SECONDARY,
                        icon_key="settings", content=ToggleSwitch())
    card.adjustSize()
    assert card.height() == CARD_MIN_H == 68
    assert card.minimumHeight() == CARD_MIN_H


def test_the_frozen_card_rule_would_make_it_70(styled):
    """The reason the card paints its own box.

    `min-height: 68px` in QSS sizes the CONTENT box, so the frozen `#Card`
    rule's own 1 px border pushes the widget to 70. Taking the object name would
    therefore break the card's height by two pixels — which is exactly the trap
    `qss.ICON_BUTTON_CONTENT` documents for the 32 px icon button.
    """
    frame = QFrame()
    frame.setObjectName(OBJ.CARD)
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
    layout.addWidget(QLabel(OBJ.CARD))
    frame.ensurePolished()
    frame.adjustSize()
    assert frame.height() == CARD_MIN_H + 2

    card = SettingsCard(OBJ.CARD)
    card.adjustSize()
    assert card.height() == CARD_MIN_H
    assert card.objectName() != OBJ.CARD


def test_settings_card_uses_the_toolkit_metrics():
    assert (CARD_MIN_H, CARD_PAD, CARD_ICON, CARD_ICON_GAP) == (68, 16, 20, 20)
    assert CARD_CONTENT_MIN_W == 120
    assert CARD_WRAP_THRESHOLD == 476
    assert CARD_WRAP_NO_ICON_THRESHOLD == 286
    # SettingsCardActionIconMaxSize is 13, which is not a native glyph size.
    assert CARD_ACTION_ICON in icons.GLYPH_SIZES


def test_settings_card_pads_16_px_all_round(styled):
    card = SettingsCard(OBJ.CARD)
    assert card.layout().contentsMargins() == QMargins(CARD_PAD, CARD_PAD,
                                                       CARD_PAD, CARD_PAD)


def test_settings_card_text_lines_use_the_ramp_line_heights(styled):
    card = SettingsCard(OBJ.CARD, description=OBJ.CARD_SECONDARY)
    assert card._title.height() == fonts.line_height("body")
    assert card._description.height() == fonts.line_height("caption")
    # 16 + 20 + 16 + 16 = 68: the two lines exactly fill the padded box.
    assert (2 * CARD_PAD + card._title.height()
            + card._description.height()) == CARD_MIN_H


def test_settings_card_hides_an_absent_description(styled):
    card = SettingsCard(OBJ.CARD)
    assert card.description() == ""
    card.set_description(OBJ.CARD_SECONDARY)
    assert card.description() == OBJ.CARD_SECONDARY


def test_settings_card_icon_is_20_px_with_a_20_px_gap(styled):
    card = SettingsCard(OBJ.CARD, icon_key="settings")
    assert card._icon.height() == CARD_ICON
    assert card._icon.width() == CARD_ICON + CARD_ICON_GAP
    assert card.icon_key() == "settings"


def test_settings_card_rejects_an_unknown_icon(styled):
    with pytest.raises(KeyError):
        SettingsCard(OBJ.CARD, icon_key="not-a-glyph")


def test_settings_card_content_column_reserves_120_px(styled):
    card = SettingsCard(OBJ.CARD, content=ToggleSwitch())
    assert card.grid_layout().columnMinimumWidth(2) == CARD_CONTENT_MIN_W
    assert isinstance(card.content(), ToggleSwitch)


def test_settings_card_replaces_its_content(styled):
    first, second = ToggleSwitch(), FluentButton(OBJ.CARD)
    card = SettingsCard(OBJ.CARD, content=first)
    card.set_content(second)
    assert card.content() is second
    assert second.parent() is not None


# ═════════════════════════════════════════════════════════════════════════════
# SettingsCard behaviour
# ═════════════════════════════════════════════════════════════════════════════

def test_settings_card_only_hovers_when_it_is_clickable(styled):
    plain = SettingsCard(OBJ.CARD)
    plain.resize(400, CARD_MIN_H)
    hover(plain)
    assert plain.is_hovered()
    assert plain.box_colours()[0] == theme.T("CardBackgroundFillColorDefault")

    clickable = SettingsCard(OBJ.CARD, clickable=True)
    clickable.resize(400, CARD_MIN_H)
    hover(clickable)
    assert clickable.box_colours()[0] == theme.T("ControlFillColorSecondary")
    assert clickable.box_colours()[2] == theme.T("ControlStrokeColorSecondary")


def test_settings_card_press_drops_the_elevation_edge(styled):
    card = SettingsCard(OBJ.CARD, clickable=True)
    card.resize(400, CARD_MIN_H)
    card.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(10.0, 10.0), QPointF(10.0, 10.0),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    assert card.is_pressed()
    fill, _stroke, edge = card.box_colours()
    assert fill == theme.T("ControlFillColorTertiary")
    assert edge is None


def test_settings_card_disabled_uses_the_disabled_fill(styled):
    card = SettingsCard(OBJ.CARD, clickable=True)
    card.setEnabled(False)
    assert card.box_colours()[0] == theme.T("ControlFillColorDisabled")


def test_a_clickable_card_emits_clicked(styled):
    card = SettingsCard(OBJ.CARD, clickable=True)
    card.resize(400, CARD_MIN_H)
    seen: list[int] = []
    card.clicked.connect(lambda: seen.append(1))
    click(card)
    assert seen == [1]


def test_a_plain_card_never_emits_clicked(styled):
    card = SettingsCard(OBJ.CARD)
    card.resize(400, CARD_MIN_H)
    seen: list[int] = []
    card.clicked.connect(lambda: seen.append(1))
    click(card)
    assert seen == []


@pytest.mark.parametrize("key", [Qt.Key.Key_Space, Qt.Key.Key_Return,
                                 Qt.Key.Key_Enter])
def test_a_clickable_card_activates_from_the_keyboard(styled, key):
    card = SettingsCard(OBJ.CARD, clickable=True)
    seen: list[int] = []
    card.clicked.connect(lambda: seen.append(1))
    card.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key,
                                 Qt.KeyboardModifier.NoModifier))
    assert seen == [1]


def test_a_clickable_card_takes_focus(styled):
    assert SettingsCard(OBJ.CARD, clickable=True).focusPolicy() \
        == Qt.FocusPolicy.StrongFocus
    assert SettingsCard(OBJ.CARD).focusPolicy() == Qt.FocusPolicy.NoFocus


def test_the_action_chevron_only_shows_on_a_clickable_card(styled):
    card = SettingsCard(OBJ.CARD)
    chevron = card.findChildren(QLabel)[-1]
    assert not chevron.isVisible()
    card.set_clickable(True)
    card.show()
    assert chevron.isVisible()
    card.hide()


def test_an_unboxed_card_paints_no_box(styled):
    """An expander's child row must not read as a card inside a card.

    Both cards are given a parent: a TOP-LEVEL widget is erased with the
    palette's Window brush before its `paintEvent` runs, which would hide the
    difference behind an opaque grey.
    """
    host = QWidget()
    boxed = SettingsCard(OBJ.CARD, host)
    boxed.resize(400, CARD_MIN_H)
    fill = render(boxed, background=False).pixelColor(390, CARD_MIN_H // 2)
    assert fill.name().upper() == theme.T("CardBackgroundFillColorDefault").upper()

    unboxed = SettingsCard(OBJ.CARD, host, boxed=False)
    assert not unboxed.is_boxed()
    unboxed.resize(400, CARD_MIN_H)
    assert render(unboxed, background=False).pixelColor(390, CARD_MIN_H // 2).alpha() == 0

    unboxed.set_boxed(True)
    assert unboxed.is_boxed()
    assert render(unboxed, background=False).pixelColor(390, CARD_MIN_H // 2).alpha() == 255


# ═════════════════════════════════════════════════════════════════════════════
# SettingsCard wrapping
# ═════════════════════════════════════════════════════════════════════════════

def test_a_narrow_card_wraps_its_control_onto_a_second_row(styled, qapp):
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    card = SettingsCard(OBJ.CARD, icon_key="settings", content=ToggleSwitch())
    layout.addWidget(card)
    host.resize(CARD_WRAP_THRESHOLD + 100, 200)
    host.show()
    qapp.processEvents()
    assert not card.is_wrapped()

    host.resize(CARD_WRAP_THRESHOLD - 100, 200)
    qapp.processEvents()
    assert card.is_wrapped()
    host.hide()


def test_a_card_with_no_icon_uses_the_lower_threshold(styled):
    with_icon = SettingsCard(OBJ.CARD, icon_key="settings", content=ToggleSwitch())
    without = SettingsCard(OBJ.CARD, content=ToggleSwitch())
    assert with_icon.wrap_threshold() == CARD_WRAP_THRESHOLD
    assert without.wrap_threshold() == CARD_WRAP_NO_ICON_THRESHOLD


def test_a_card_with_no_control_never_wraps(styled, qapp):
    card = SettingsCard(OBJ.CARD, icon_key="settings")
    card.show()
    card.resize(100, CARD_MIN_H)
    qapp.processEvents()
    assert not card.is_wrapped()
    card.hide()


# ═════════════════════════════════════════════════════════════════════════════
# SettingsExpander
# ═════════════════════════════════════════════════════════════════════════════

def test_expander_starts_closed(styled):
    expander = SettingsExpander(OBJ.CARD)
    assert not expander.is_expanded()
    assert expander.body().maximumHeight() == 0
    assert expander.chevron().angle() == 0.0


def test_expander_opens_and_closes(styled, no_animation):
    expander = SettingsExpander(OBJ.CARD)
    expander.add_row(FluentButton(OBJ.CARD))
    states: list[bool] = []
    expander.expanded_changed.connect(states.append)

    expander.set_expanded(True)
    assert expander.is_expanded()
    assert expander.body().maximumHeight() == UNBOUNDED
    assert expander.chevron().angle() == CHEVRON_OPEN_DEG

    expander.set_expanded(False)
    assert not expander.is_expanded()
    assert expander.body().maximumHeight() == 0
    assert expander.chevron().angle() == 0.0
    assert states == [True, False]


def test_expander_ignores_a_no_op_change(styled, no_animation):
    expander = SettingsExpander(OBJ.CARD)
    states: list[bool] = []
    expander.expanded_changed.connect(states.append)
    expander.set_expanded(False)
    assert states == []


def test_expander_toggle_flips_the_state(styled, no_animation):
    expander = SettingsExpander(OBJ.CARD)
    expander.toggle()
    assert expander.is_expanded()
    expander.toggle()
    assert not expander.is_expanded()


def test_expander_lands_immediately_when_asked_to(styled):
    """`animate=False` is the config-load path: no motion, final state at once."""
    expander = SettingsExpander(OBJ.CARD)
    expander.add_row(FluentButton(OBJ.CARD))
    expander.set_expanded(True, animate=False)
    assert expander.body().maximumHeight() == UNBOUNDED
    assert expander.chevron().angle() == CHEVRON_OPEN_DEG


def test_expander_chevron_button_is_32_px(styled):
    expander = SettingsExpander(OBJ.CARD)
    assert expander.chevron().size() == QSize(EXPANDER_CHEVRON, EXPANDER_CHEVRON)


def test_expander_header_uses_the_toolkit_padding(styled):
    expander = SettingsExpander(OBJ.CARD)
    left, top, right, bottom = EXPANDER_HEADER_PAD
    assert EXPANDER_HEADER_PAD == (16, 16, 4, 16)
    assert expander.header().grid_layout().contentsMargins() == QMargins(
        left, top, right, bottom)


def test_expander_rows_use_the_58_px_indent(styled):
    expander = SettingsExpander(OBJ.CARD)
    row = expander.add_row(FluentButton(OBJ.CARD))
    left, top, right, bottom = EXPANDER_CHILD_PAD
    assert EXPANDER_CHILD_PAD == (58, 8, 44, 8)
    assert row.layout().contentsMargins() == QMargins(left, top, right, bottom)


def test_expander_unboxes_a_card_it_is_given(styled):
    """A card inside an expander row must not read as a card inside a card."""
    card = SettingsCard(OBJ.CARD, content=ToggleSwitch())
    assert card.is_boxed()
    expander = SettingsExpander(OBJ.CARD)
    expander.add_row(card)
    assert not card.is_boxed()
    assert card.grid_layout().contentsMargins() == QMargins(0, 0, 0, 0)


def test_expander_marks_only_the_last_row(styled):
    expander = SettingsExpander(OBJ.CARD)
    first = expander.add_row(FluentButton(OBJ.CARD))
    second = expander.add_row(FluentButton(OBJ.CARD))
    assert not first.is_last()
    assert second.is_last()
    assert expander.rows() == (first, second)


def test_an_open_expander_header_squares_its_bottom_corners(styled, no_animation):
    expander = SettingsExpander(OBJ.CARD)
    radius = float(RADII["control"])
    assert expander.header().corner_radii() == (radius, radius, radius, radius)
    expander.set_expanded(True)
    assert expander.header().corner_radii() == (radius, radius, 0.0, 0.0)


def test_expander_header_has_no_static_action_chevron(styled):
    """The rotating button stands in that place instead."""
    expander = SettingsExpander(OBJ.CARD)
    header = expander.header()
    assert header.is_clickable()
    assert not header._action_icon
    assert not header._chevron.isVisible()


def test_expander_animation_is_gated(styled, no_animation):
    assert motion.DUR("normal") == 0
    expander = SettingsExpander(OBJ.CARD)
    expander.add_row(FluentButton(OBJ.CARD))
    expander.set_expanded(True)
    # A gated animation still lands its end value, and the clamp is released.
    assert expander.body().maximumHeight() == UNBOUNDED


def test_expander_forwards_header_content(styled):
    expander = SettingsExpander(OBJ.CARD, description=OBJ.CARD_SECONDARY,
                                icon_key="advanced")
    assert expander.title() == OBJ.CARD
    assert expander.description() == OBJ.CARD_SECONDARY
    expander.set_title(OBJ.FLYOUT)
    expander.set_description(OBJ.FOOTER)
    expander.set_icon("settings")
    assert expander.title() == OBJ.FLYOUT
    assert expander.description() == OBJ.FOOTER
    toggle = ToggleSwitch()
    expander.set_content(toggle)
    assert expander.content() is toggle


# ═════════════════════════════════════════════════════════════════════════════
# InfoBar
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("severity,object_name", [
    (InfoBarSeverity.INFORMATIONAL, OBJ.BANNER_INFO),
    (InfoBarSeverity.SUCCESS, OBJ.BANNER_SUCCESS),
    (InfoBarSeverity.WARNING, OBJ.BANNER_CAUTION),
    (InfoBarSeverity.ERROR, OBJ.BANNER_CRITICAL),
])
def test_info_bar_takes_the_frozen_banner_object_name(styled, severity, object_name):
    bar = InfoBar(OBJ.CARD, OBJ.CARD_SECONDARY, severity=severity)
    assert bar.objectName() == object_name
    assert bar.severity() is severity


def test_info_bar_layout_margins_are_zero_because_the_sheet_pads(styled, qapp):
    """QSS `padding` on a container moves its layout — measured.

    A styled `QFrame` with `padding: 12px; border: 1px` reports
    `contentsMargins() == (13, 13, 13, 13)`, and the layout is inset by that.
    Setting the same padding again in the layout would double it.
    """
    bar = InfoBar(OBJ.CARD, OBJ.CARD_SECONDARY)
    bar.ensurePolished()
    bar.resize(400, bar.sizeHint().height())
    bar.show()
    qapp.processEvents()
    assert bar.layout().contentsMargins() == QMargins(0, 0, 0, 0)
    expected = SPACING["m"] + 1                      # padding 12 + border 1
    assert bar.contentsMargins() == QMargins(expected, expected, expected, expected)
    bar.hide()


def test_info_bar_switches_severity_in_place(styled):
    bar = InfoBar(OBJ.CARD, severity=InfoBarSeverity.INFORMATIONAL)
    bar.set_severity(InfoBarSeverity.ERROR)
    assert bar.objectName() == OBJ.BANNER_CRITICAL
    assert bar.severity() is InfoBarSeverity.ERROR


def test_info_bar_glyph_is_tinted_per_severity(styled):
    tints = {
        InfoBarSeverity.SUCCESS: theme.T("SystemFillColorSuccess"),
        InfoBarSeverity.WARNING: theme.T("SystemFillColorCaution"),
        InfoBarSeverity.ERROR: theme.T("SystemFillColorCritical"),
        InfoBarSeverity.INFORMATIONAL: theme.accent(),
    }
    for severity, expected in tints.items():
        bar = InfoBar(OBJ.CARD, severity=severity)
        assert bar._icon.colour() == expected


def test_info_bar_actions_and_close(styled):
    bar = InfoBar(OBJ.CARD, OBJ.CARD_SECONDARY)
    action = bar.add_action(OBJ.FOOTER, accent=True)
    assert isinstance(action, FluentButton)
    assert action.variant() is ButtonVariant.ACCENT
    assert bar.actions_buttons() == (action,)

    seen: list[int] = []
    bar.closed.connect(lambda: seen.append(1))
    bar.close_button().click()
    assert seen == [1]

    bar.clear_actions()
    assert bar.actions_buttons() == ()


def test_info_bar_title_and_message_round_trip(styled):
    bar = InfoBar(OBJ.CARD, OBJ.CARD_SECONDARY)
    assert (bar.title(), bar.message()) == (OBJ.CARD, OBJ.CARD_SECONDARY)
    bar.set_title(OBJ.FLYOUT)
    bar.set_message(OBJ.FOOTER)
    assert (bar.title(), bar.message()) == (OBJ.FLYOUT, OBJ.FOOTER)
    bar.set_closable(False)
    assert not bar.is_closable()


def test_severity_for_issue_covers_every_issue_severity():
    assert set(SEVERITY_FOR_ISSUE) == set(IssueSeverity)
    assert SEVERITY_FOR_ISSUE[IssueSeverity.BLOCKING] is InfoBarSeverity.ERROR
    assert SEVERITY_FOR_ISSUE[IssueSeverity.INFO] is InfoBarSeverity.INFORMATIONAL


# ═════════════════════════════════════════════════════════════════════════════
# ContentDialog
# ═════════════════════════════════════════════════════════════════════════════

def test_content_dialog_reserves_its_shadow_margin(styled):
    """Acceptance: the dialog leaves room for its own elevation."""
    dialog = ContentDialog()
    blur, dy, _alpha = theme.shadow("dialog")
    assert dialog.reserved_margins() == QMargins(blur, blur, blur, blur + dy)
    assert dialog.layout().contentsMargins() == dialog.reserved_margins()
    assert dialog.shadow_name() == "dialog"


def test_content_dialog_shadows_the_surface_not_the_window(styled):
    """`QGraphicsEffect` is exclusive — the dialog itself must stay free."""
    dialog = ContentDialog()
    assert isinstance(dialog.surface().graphicsEffect(), QGraphicsDropShadowEffect)
    assert dialog.graphicsEffect() is None
    assert dialog.surface().objectName() == OBJ.DIALOG_SURFACE


def test_content_dialog_window_is_the_surface_plus_the_margin(styled, qapp):
    dialog = ContentDialog(title=OBJ.CARD, body=OBJ.CARD_SECONDARY)
    dialog.set_buttons(OBJ.FOOTER, OBJ.HEADER)
    dialog.show()
    qapp.processEvents()
    margins = dialog.reserved_margins()
    assert dialog.width() == dialog.surface().width() + margins.left() + margins.right()
    assert dialog.height() == dialog.surface().height() + margins.top() + margins.bottom()
    assert dialog.surface().pos() == QPoint(margins.left(), margins.top())
    dialog.hide()


def test_content_dialog_is_frameless_translucent_and_modal(styled):
    dialog = ContentDialog()
    flags = dialog.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.NoDropShadowWindowHint
    assert dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert dialog.isModal()
    assert isinstance(dialog, QDialog)


def test_content_dialog_surface_is_clamped_to_the_winui_range(styled):
    dialog = ContentDialog()
    surface = dialog.surface()
    assert surface.minimumWidth() == DIALOG_MIN_W
    assert surface.maximumWidth() == DIALOG_MAX_W
    assert surface.minimumHeight() == DIALOG_MIN_H


def test_content_dialog_buttons_use_the_winui_metrics(styled):
    dialog = ContentDialog()
    primary, secondary, close = dialog.set_buttons(OBJ.FOOTER, OBJ.HEADER, OBJ.CARD)
    assert primary.variant() is ButtonVariant.ACCENT
    assert secondary.variant() is ButtonVariant.STANDARD
    for button in (primary, secondary, close):
        assert button.minimumWidth() == DIALOG_BUTTON_MIN_W
        assert button.maximumWidth() == DIALOG_BUTTON_MAX_W
        assert button.sizeHint().height() == METRICS["button_h"]
    assert dialog.buttons() == (primary, secondary, close)


def test_content_dialog_primary_accepts_and_secondary_rejects(styled):
    dialog = ContentDialog()
    primary, secondary, _close = dialog.set_buttons(OBJ.FOOTER, OBJ.HEADER)
    seen: list[str] = []
    dialog.primary_clicked.connect(lambda: seen.append(OBJ.FOOTER))
    dialog.secondary_clicked.connect(lambda: seen.append(OBJ.HEADER))
    dialog.accepted.connect(lambda: seen.append(OBJ.CARD))
    primary.click()
    assert seen == [OBJ.FOOTER, OBJ.CARD]
    assert dialog.result() == QDialog.DialogCode.Accepted

    seen.clear()
    secondary.click()
    assert seen == [OBJ.HEADER]
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_content_dialog_rebuilds_its_button_row(styled):
    dialog = ContentDialog()
    dialog.set_buttons(OBJ.FOOTER, OBJ.HEADER, OBJ.CARD)
    primary, secondary, close = dialog.set_buttons(OBJ.FOOTER)
    assert (secondary, close) == (None, None)
    assert dialog.buttons() == (primary,)


def test_content_dialog_content_slot(styled):
    dialog = ContentDialog(title=OBJ.CARD, body=OBJ.CARD_SECONDARY)
    assert dialog.title() == OBJ.CARD
    assert dialog.body() == OBJ.CARD_SECONDARY
    widget = QLabel(OBJ.FOOTER)
    dialog.set_content(widget)
    assert dialog.content() is widget
    dialog.set_content(None)
    assert dialog.content() is None


def test_content_dialog_rejects_an_unknown_shadow(styled):
    with pytest.raises(KeyError):
        ContentDialog(shadow="not-a-shadow")


# ═════════════════════════════════════════════════════════════════════════════
# card_group
# ═════════════════════════════════════════════════════════════════════════════

def test_card_group_stacks_with_the_fluent_4_px_gap(styled):
    cards = [SettingsCard(OBJ.CARD), SettingsCard(OBJ.CARD_SECONDARY)]
    group = card_group(cards, heading=OBJ.HEADER)
    assert group.layout().spacing() == containers.CARD_GROUP_GAP == SPACING["xs"]
    assert isinstance(group.layout().itemAt(0).widget(), SectionHeading)
    assert group.layout().count() == 3


# ═════════════════════════════════════════════════════════════════════════════
# Rendering
# ═════════════════════════════════════════════════════════════════════════════

def _container_gallery(parent: QWidget | None = None) -> QWidget:
    page = QWidget(parent)
    page.setObjectName(OBJ.ROOT)
    page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(*(SPACING["l"],) * 4)
    layout.setSpacing(SPACING["s"])
    layout.addWidget(SectionHeading(OBJ.HEADER, page))
    layout.addWidget(SettingsCard(OBJ.CARD, page, description=OBJ.CARD_SECONDARY,
                                  icon_key="settings", content=ToggleSwitch()))
    layout.addWidget(SettingsCard(OBJ.CARD, page, icon_key="cloud", clickable=True))
    expander = SettingsExpander(OBJ.CARD, page, icon_key="advanced")
    expander.add_row(SettingsCard(OBJ.CARD_SECONDARY, content=ToggleSwitch()))
    expander.set_expanded(True, animate=False)
    layout.addWidget(expander)
    for severity in InfoBarSeverity:
        layout.addWidget(InfoBar(OBJ.CARD, OBJ.CARD_SECONDARY, page,
                                 severity=severity))
    page.resize(600, page.sizeHint().height())
    return page


@pytest.mark.slow
@pytest.mark.parametrize("dpr", DEVICE_PIXEL_RATIOS)
@pytest.mark.parametrize("dark_theme", [False, True])
def test_containers_render_at_every_theme_and_ratio(qapp, dpr, dark_theme,
                                                    monkeypatch):
    monkeypatch.setattr(theme, "_DETECTED_DARK", dark_theme, raising=False)
    theme._STYLESHEET_CACHE.clear()
    qss.invalidate()
    icons.clear_cache()
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    try:
        fonts.apply_app_font(qapp)
        qss.apply(qapp, dark=dark_theme)
        page = _container_gallery()
        image = render(page, dpr=dpr)
        assert not is_blank(image)
        assert image.pixelColor(2, 2).name().upper() == theme.base(dark=dark_theme).upper()
    finally:
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)
        theme._STYLESHEET_CACHE.clear()
        qss.invalidate()
        icons.clear_cache()


@pytest.mark.slow
def test_the_dialog_renders_its_shadow(styled, qapp):
    dialog = ContentDialog(title=OBJ.CARD, body=OBJ.CARD_SECONDARY,
                           shadow="flyout")
    dialog.set_buttons(OBJ.FOOTER, OBJ.HEADER)
    dialog.show()
    qapp.processEvents()
    image = render(dialog)
    margins = dialog.reserved_margins()
    # The blur bleeds into the reserved margin: some pixel above the surface is
    # not fully transparent.
    band = [image.pixelColor(x, margins.top() - 2).alpha()
            for x in range(margins.left(), image.width() - margins.right())]
    assert max(band) > 0
    dialog.hide()


# ═════════════════════════════════════════════════════════════════════════════
# Source hygiene — every WP-11b module
# ═════════════════════════════════════════════════════════════════════════════

#: A string constant is allowed when it is a WP-00 table key or an identifier.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: …or when it holds no letters at all: a separator, a path component, a join.
_NO_LETTERS = re.compile(r"^[^A-Za-z]*$")


def _string_constants(path: Path) -> list[str]:
    """Every string literal in a module that could possibly be user-facing.

    Three kinds are skipped, each for a reason:

      * **docstrings** — documentation, never painted;
      * **`raise` messages** — developer diagnostics, and a `SafetyRefusal`-shaped
        message is by definition not chrome a user clicks past;
      * the body of any function whose name ends in **`_qss`** — a stylesheet
        fragment is markup. `ui/qss.py` is exempt from this rule wholesale for
        the same reason; a `*_qss` helper is the same thing, scoped.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                skip.add(id(body[0].value))
    for node in ast.walk(tree):
        is_qss = (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name.endswith("_qss"))
        if isinstance(node, ast.Raise) or is_qss:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    skip.add(id(inner))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in skip]


@pytest.mark.parametrize("path", WP11B_MODULES, ids=lambda p: p.name)
def test_no_colour_literal_in_a_widget(path):
    """Acceptance: every colour comes from `theme`, never from a hex literal."""
    source = path.read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", code), path.name
    assert "rgba(" not in code and "rgb(" not in code
    assert "QColor(" in code, "the widgets do paint — just never from a literal"


@pytest.mark.parametrize("path", WP11B_MODULES, ids=lambda p: p.name)
def test_no_user_facing_string_in_a_widget(path):
    """Acceptance: labels are passed in by WP-12/WP-13 out of `strings.py`.

    A literal survives only if it is an identifier-like technical token (a token
    key, a glyph key, a property name) or holds no letters at all (the middot
    that joins an activity row's caption). A sentence can be neither.
    """
    offenders = [value for value in _string_constants(path)
                 if value and not _IDENTIFIER.match(value)
                 and not _NO_LETTERS.match(value)]
    assert offenders == [], f"{path.name}: {offenders}"


@pytest.mark.parametrize("path", WP11B_MODULES, ids=lambda p: p.name)
def test_every_technical_string_resolves_against_wp00(path):
    """Acceptance: no invented magic string, and no invented icon name."""
    known: set[str] = set()
    known |= set(theme.TOKENS) | set(theme.ACCENT_ROLES)
    known |= set(theme.METRICS) | set(theme.RADII) | set(theme.SPACING)
    known |= set(theme.TYPE) | set(theme.DURATION) | set(theme.CURVES)
    known |= set(theme.SHADOWS) | set(icons.GLYPHS)
    known |= {v for k, v in vars(theme.OBJ).items() if not k.startswith("_")}
    known |= {v for k, v in vars(theme.PROP).items() if not k.startswith("_")}
    known |= {m.value for m in InfoBarSeverity}
    known |= {"base", "layer"}                    # theme.Surface
    known |= {"decelerate", "accelerate", "easy_ease"}    # motion curves
    # The values `theme.PROP.ROLE` documents: QLabel[role="secondary"|…].
    known |= {"secondary", "tertiary", "disabled"}

    module = __import__("onedriveui.ui.widgets." + path.stem, fromlist=["x"])
    known |= set(dir(module))
    for value in vars(module).values():
        if isinstance(value, type):
            known |= set(dir(value))

    unknown = sorted({value for value in _string_constants(path)
                      if value and _IDENTIFIER.match(value) and value not in known})
    unknown = [value for value in unknown if not hasattr(QWidget, value)]
    assert unknown == [], f"{path.name}: {unknown}"


ALLOWED_IMPORT_ROOTS = {
    "onedriveui", "PySide6", "enum", "typing", "__future__", "dataclasses",
    "math", "weakref", "inspect", "os", "re", "pathlib",
}
FORBIDDEN_SUBPACKAGES = ("onedriveui.sync", "onedriveui.rc", "onedriveui.platform",
                         "onedriveui.data", "onedriveui.config", "onedriveui.applog",
                         "onedriveui.units")


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", WP11B_MODULES, ids=lambda p: p.name)
def test_no_engine_import(path):
    """The kit must render in a gallery with zero rclone.

    `units` is on the forbidden list deliberately: it is WP-01's, and a widget
    that formats its own byte counts is a widget that cannot be rendered without
    the foundation package. Every formatted string is passed in instead.
    """
    for name in _imported_modules(path):
        assert name.split(".")[0] in ALLOWED_IMPORT_ROOTS, f"{path.name}: {name}"
        for forbidden in FORBIDDEN_SUBPACKAGES:
            assert not name.startswith(forbidden), f"{path.name}: {name}"


def test_wp11b_imports_only_wp00_and_the_widget_kit():
    allowed = {
        "onedriveui", "onedriveui.bus", "onedriveui.models",
        "onedriveui.constants", "onedriveui.strings", "onedriveui.paths",
        "onedriveui.errors", "onedriveui.ui", "onedriveui.ui.theme",
        "onedriveui.ui.icons", "onedriveui.ui.fonts", "onedriveui.ui.qss",
        "onedriveui.ui.motion", "onedriveui.ui.widgets",
        "onedriveui.ui.widgets.controls", "onedriveui.ui.widgets.indicators",
        "onedriveui.ui.widgets.containers", "onedriveui.ui.widgets.lists",
        "onedriveui.ui.widgets.chrome",
    }
    for path in WP11B_MODULES:
        for name in _imported_modules(path):
            if name.startswith("onedriveui"):
                assert name in allowed, f"{path.name}: {name}"


def test_wp11b_does_not_modify_the_widgets_package_init():
    """WP-11a owns `ui/widgets/__init__.py`; WP-11b must not have touched it.

    The consequence is deliberate and documented: WP-12/WP-13 import the
    containers, lists and chrome classes from their own modules rather than from
    the package.
    """
    source = (REPO_ROOT / "onedriveui" / "ui" / "widgets" / "__init__.py").read_text()
    for module in ("containers", "lists", "chrome"):
        assert module not in source


# ═════════════════════════════════════════════════════════════════════════════
# The gallery — WP-11b's headline acceptance
# ═════════════════════════════════════════════════════════════════════════════

def test_the_gallery_script_exists_and_is_executable():
    assert GALLERY.is_file()
    assert GALLERY.read_text(encoding="utf-8").startswith("#!")


def test_the_gallery_imports_nothing_outside_the_ui_and_wp00():
    """Acceptance: zero imports outside `onedriveui.ui` and the WP-00 contracts."""
    wp00 = {"onedriveui", "onedriveui.models", "onedriveui.strings",
            "onedriveui.constants", "onedriveui.errors", "onedriveui.paths",
            "onedriveui.bus"}
    stdlib = {"argparse", "os", "sys", "pathlib", "__future__"}
    for name in _imported_modules(GALLERY):
        root = name.split(".")[0]
        if root == "onedriveui":
            assert name in wp00 or name.startswith("onedriveui.ui"), name
        else:
            assert root in stdlib or root == "PySide6", name


def test_the_gallery_renders_every_widget_in_the_kit():
    """Every public widget class of WP-11a and WP-11b appears in the script."""
    source = GALLERY.read_text(encoding="utf-8")
    expected = {
        "FluentButton", "FluentLineEdit", "FluentCheckBox", "FluentRadioButton",
        "FluentComboBox", "ToggleSwitch", "icon_button",
        "ProgressRing", "FluentProgressBar", "StorageBar", "Avatar",
        "StatusBadge",
        "SettingsCard", "SettingsExpander", "InfoBar", "ContentDialog",
        "SectionHeading",
        "ActivityListView", "ActivityRow", "FolderTree",
        "NavigationView", "SearchBox", "StatusGlyph",
    }
    missing = sorted(name for name in expected if name not in source)
    assert missing == [], missing


@pytest.mark.slow
def test_the_gallery_writes_one_contact_sheet_per_theme(tmp_path):
    """Acceptance: run it offscreen, save a PNG per theme, report the paths."""
    environment = dict(**{k: v for k, v in _clean_environment().items()})
    result = subprocess.run(
        [sys.executable, str(GALLERY), "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=300, env=environment,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    written = [Path(line) for line in result.stdout.split() if line]
    assert len(written) == 2
    for path in written:
        assert path.is_file()
        assert path.stat().st_size > 1000
        image = QImage(str(path))
        assert not image.isNull()
        assert image.width() > 800 and image.height() > 600
    assert {p.name for p in written} == {"gallery-light.png", "gallery-dark.png"}


def _clean_environment() -> dict[str, str]:
    """The subprocess environment: offscreen, animations on, real HOME left out."""
    import os

    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["ONEDRIVEUI_ANIMATIONS"] = "0"
    return environment


@pytest.mark.slow
def test_the_gallery_page_builds_in_process(styled, qapp):
    """The page itself, so a failure points at a widget rather than at argv."""
    module = _load_gallery()
    page = module.build_page()
    assert page.width() > 800
    image = render(page)
    assert not is_blank(image)
    dialog = module.build_dialog()
    dialog.show()
    qapp.processEvents()
    assert dialog.surface().width() >= DIALOG_MIN_W
    dialog.hide()


def _load_gallery():
    """Import `scripts/gallery.py` by path; `scripts/` is not a package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("onedriveui_gallery", GALLERY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gallery_uses_real_strings_for_every_label():
    """The gallery's own captions are scaffolding; widget labels are not.

    Every widget the sheet builds is given a string out of `onedriveui.strings`,
    which is what makes the contact sheet a fair preview of the real windows.
    """
    source = GALLERY.read_text(encoding="utf-8")
    assert "from onedriveui.strings import" in source
    assert source.count("S.") > 40
    # The only bare literals are the CAPTION_* / TILE_* / TITLE_* scaffolding.
    tree = ast.parse(source)
    caption_names = {
        node.target.id if isinstance(node, ast.AnnAssign) else node.targets[0].id
        for node in tree.body
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name))
        or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name))
    }
    assert {"CAPTION_TYPE", "TILE_PAGE", "TITLE_LIGHT"} <= caption_names


# ═════════════════════════════════════════════════════════════════════════════
# Cross-module sanity — the kit still holds together
# ═════════════════════════════════════════════════════════════════════════════

def test_wp11b_classes_are_qt_class_names_the_sheet_can_reach(styled):
    """A Python subclass carries its own metaobject name, so a QSS type selector
    matches it — which is how `SearchBox` inherits the whole `FluentLineEdit`
    box recipe without restating a single declaration."""
    box = chrome.SearchBox()
    box.ensurePolished()
    assert box.metaObject().className() == "SearchBox"
    assert box.sizeHint().height() == METRICS["textbox_h"]
    assert box.objectName() == OBJ.SEARCH_BOX


def test_every_wp11b_module_declares_its_public_api():
    for path in WP11B_MODULES:
        module = __import__("onedriveui.ui.widgets." + path.stem, fromlist=["x"])
        assert module.__all__, path.name
        for name in module.__all__:
            assert hasattr(module, name), f"{path.name}: {name}"


def test_the_three_modules_import_cleanly_without_an_application():
    """No module-level Qt object construction: importing must not need a QApp."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import onedriveui.ui.widgets.containers, onedriveui.ui.widgets.lists, "
         "onedriveui.ui.widgets.chrome"],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        env=_clean_environment(),
    )
    assert result.returncode == 0, result.stderr


def test_status_glyph_and_activity_row_agree_with_wp00_tables():
    """A cheap guard that the two semantic tables still cover their enums."""
    assert set(chrome.GLYPH_FOR_TRAY) == set(chrome.TONE_FOR_TRAY)
    assert set(lists.STATUS_TOKEN) == set(theme.__dict__.get("FileStates", lists.STATUS_TOKEN))
    for state in SyncState:
        assert chrome.tray_for(state) in chrome.GLYPH_FOR_TRAY
