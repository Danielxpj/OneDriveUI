"""WP-11a — `onedriveui.ui.qss`.

The geometry assertions here are the load-bearing ones. Each recipe is applied
from the shipped helper — never from a copy pasted into the test — so a change
to `theme.METRICS` moves the measurement and fails the test rather than quietly
shipping a 33 px button.

Every geometry test pins `fonts.reference_font()`. `QPushButton.sizeHint()`
width is font-metric dependent (Qt measures the placeholder "XXXX" for an empty
button), so an unpinned assertion would measure the developer's desktop font.
"""

from __future__ import annotations

import re

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QComboBox, QFrame, QLabel, QLineEdit, QPushButton, QWidget,
)

from onedriveui.ui import fonts, qss, theme
from onedriveui.ui.theme import METRICS, PROP
from onedriveui.ui.widgets.controls import (
    FluentButton, FluentComboBox, FluentLineEdit,
)


# ═════════════════════════════════════════════════════════════════════════════
# Harness
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sheet_bench(qapp):
    """Apply a stylesheet and a pinned font, and put both back afterwards."""
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    qapp.setFont(fonts.reference_font())

    class Bench:
        app = qapp

        @staticmethod
        def apply(sheet: str) -> None:
            qapp.setStyleSheet(sheet)

        @staticmethod
        def measure(widget) -> QSize:
            widget.setFont(fonts.reference_font())
            widget.ensurePolished()
            return widget.sizeHint()

    yield Bench()
    qapp.setStyleSheet(previous_sheet)
    qapp.setFont(previous_font)
    qss.invalidate()


def test_widget_class_names_match_the_selectors():
    """The QSS type selector IS the Qt class name; a rename unstyles the widget."""
    assert FluentButton.__name__ == qss.SEL.BUTTON
    assert FluentLineEdit.__name__ == qss.SEL.LINE_EDIT
    assert FluentComboBox.__name__ == qss.SEL.COMBO_BOX


# ═════════════════════════════════════════════════════════════════════════════
# The button recipe — the acceptance measurement
# ═════════════════════════════════════════════════════════════════════════════

#: The measurement `ARCHITECTURE §4 METRICS` and `BUILD_PLAN` both quote.
RECIPE_SIZE = QSize(55, 32)


def test_button_recipe_measures_exactly_55x32(sheet_bench):
    """`padding: 5px 11px` + `min-height: 20px` + a 1 px border == 55 x 32."""
    sheet_bench.apply(qss.button_box_qss())
    assert sheet_bench.measure(FluentButton()) == RECIPE_SIZE


def test_button_min_height_is_load_bearing(sheet_bench):
    """Remove `min-height` and the box collapses onto the face's line box.

    32 px is `max(content, 20) + 5 + 5 + 1 + 1`. Qt's `min-height` only ever
    GROWS the content box, so `with min-height` can never be shorter than
    `without` — a recipe that measured 32 with it and 33 without is
    arithmetically impossible. What the declaration actually buys is the floor:
    without it the box tracks whatever the resolved face happens to measure.
    """
    sheet_bench.apply(qss.button_box_qss())
    with_min = sheet_bench.measure(FluentButton())
    sheet_bench.apply(qss.button_box_qss(min_height=False))
    without_min = sheet_bench.measure(FluentButton())

    assert with_min == RECIPE_SIZE
    assert without_min.height() != with_min.height()
    assert without_min.height() < with_min.height()
    assert without_min.height() == 30
    assert with_min.height() == METRICS["button_min_h"] + 2 * METRICS["button_pad_v"] + 2


def test_naive_padding_overshoots(sheet_bench):
    """The mis-ordered `5px 11px 11px 6px` shorthand — 36 px, not 32."""
    sheet_bench.apply(qss.button_box_qss(min_height=False, padding="5px 11px 11px 6px"))
    naive = sheet_bench.measure(FluentButton())
    assert naive.height() == 36
    assert naive.height() != RECIPE_SIZE.height()
    assert naive.width() < RECIPE_SIZE.width()


def test_button_box_height_constant_matches_the_recipe(sheet_bench):
    sheet_bench.apply(qss.button_box_qss())
    assert sheet_bench.measure(FluentButton()).height() == qss.BUTTON_BOX_H
    assert qss.BUTTON_BOX_H == METRICS["button_h"]


def test_button_is_32_under_the_full_sheet(sheet_bench):
    """The shipped sheet keeps the box at 32 px in both themes."""
    for dark in (False, True):
        sheet_bench.apply(qss.build(dark=dark))
        assert sheet_bench.measure(FluentButton()).height() == METRICS["button_h"]


def test_focus_does_not_grow_the_button(sheet_bench, qtbot):
    """The Windows 11 focus indicator is a ring OUTSIDE the control.

    A focus rule that fattened the border to 2 px would push the box to 34 and
    make every focused button jump.
    """
    sheet_bench.apply(qss.build(dark=False))
    button = FluentButton()
    unfocused = sheet_bench.measure(button)
    button.show()
    button.setFocus()
    qtbot.process()
    assert button.hasFocus()
    button.ensurePolished()
    assert button.sizeHint() == unfocused
    button.hide()


# ═════════════════════════════════════════════════════════════════════════════
# The text field — 32 px focused AND unfocused
# ═════════════════════════════════════════════════════════════════════════════

def _measure_focused(bench, qtbot, widget) -> tuple[int, int]:
    widget.setFont(fonts.reference_font())
    widget.ensurePolished()
    unfocused = widget.sizeHint().height()
    widget.show()
    widget.setFocus()
    qtbot.process()
    assert widget.hasFocus()
    widget.ensurePolished()
    focused = widget.sizeHint().height()
    widget.hide()
    return unfocused, focused


def test_line_edit_is_32_px_focused_and_unfocused(sheet_bench, qtbot):
    for dark in (False, True):
        sheet_bench.apply(qss.build(dark=dark))
        unfocused, focused = _measure_focused(sheet_bench, qtbot, FluentLineEdit())
        assert unfocused == METRICS["textbox_h"]
        assert focused == METRICS["textbox_h"]


def test_focus_padding_compensation_is_load_bearing(sheet_bench, qtbot):
    """Without the compensation the field jumps 32 -> 33 the moment it is clicked."""
    sheet_bench.apply(qss.build(dark=False) + qss.textbox_box_qss(compensate=False))
    unfocused, focused = _measure_focused(sheet_bench, qtbot, FluentLineEdit())
    assert unfocused == METRICS["textbox_h"]
    assert focused == METRICS["textbox_h"] + qss.TEXTBOX_FOCUS_DELTA
    assert focused != unfocused


def test_line_edit_height_is_face_independent(sheet_bench, qtbot):
    """The field is pinned both ways, so a taller face cannot stretch it."""
    sheet_bench.apply(qss.build(dark=False))
    for family in sorted(fonts.available_families() & set(theme.FONT_CANDIDATES)):
        edit = FluentLineEdit()
        edit.setFont(fonts.font("body", family_name=family))
        edit.ensurePolished()
        assert edit.sizeHint().height() == METRICS["textbox_h"], family


def test_combo_box_is_32_px(sheet_bench, qtbot):
    sheet_bench.apply(qss.build(dark=False))
    unfocused, focused = _measure_focused(sheet_bench, qtbot, FluentComboBox())
    assert unfocused == METRICS["button_h"]
    assert focused == METRICS["button_h"]


# ═════════════════════════════════════════════════════════════════════════════
# Workaround 1 — every button rule declares a border
# ═════════════════════════════════════════════════════════════════════════════

def test_frozen_theme_sheet_has_no_borderless_button_rule():
    for dark in (False, True):
        assert qss.pushbutton_rules_without_border(theme.stylesheet(dark=dark)) == ()


def test_built_sheet_has_no_borderless_button_rule():
    for dark in (False, True):
        assert qss.pushbutton_rules_without_border(qss.build(dark=dark)) == ()


def test_borderless_button_rule_is_detected():
    bad = "QPushButton { background: #0078D4; }"
    assert qss.pushbutton_rules_without_border(bad) == ("QPushButton",)


def test_borderless_rule_makes_build_raise(monkeypatch):
    monkeypatch.setattr(qss, "widget_kit_qss",
                        lambda **_kw: "\nQPushButton { background: #0078D4; }\n")
    qss.invalidate()
    with pytest.raises(ValueError, match="Fusion's gradient"):
        qss.build(dark=False)
    qss.invalidate()


def test_sub_control_rules_are_exempt():
    """`::menu-indicator` never goes through the button primitive."""
    assert qss.pushbutton_rules_without_border(
        "QPushButton::menu-indicator { background: #FF0000; }") == ()


def test_a_border_of_any_side_counts():
    for decl in ("border: none", "border-width: 1px", "border-bottom-color: #E5E5E5",
                 "border-style: solid", "border-color: #E5E5E5"):
        rule = "QPushButton { background: #0078D4; %s; }" % decl
        assert qss.pushbutton_rules_without_border(rule) == (), decl


@pytest.mark.slow
def test_a_borderless_button_really_renders_a_gradient(qapp, sheet_bench):
    """The reason the rule exists, measured rather than trusted.

    Fusion paints its native gradient when a QPushButton is filled with no
    border declaration; three vertical samples of a flat fill must be equal.
    """
    fill = theme.accent("rest", dark=False)

    def sample(sheet: str) -> list[str]:
        sheet_bench.apply(sheet)
        button = QPushButton("")
        button.resize(100, 40)
        image = QImage(100, 40, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        button.render(image)
        return [QImage.pixelColor(image, 50, y).name().upper() for y in (6, 20, 33)]

    gradient = sample("QPushButton { background: %s; }" % fill)
    flat = sample("QPushButton { background: %s; border: 1px solid %s; }" % (fill, fill))
    assert len(set(flat)) == 1, f"a bordered fill must be flat, got {flat}"
    assert flat[0] == fill.upper()
    assert len(set(gradient)) > 1, f"expected Fusion's gradient, got {gradient}"


# ═════════════════════════════════════════════════════════════════════════════
# Workaround 2 — scoped selectors only
# ═════════════════════════════════════════════════════════════════════════════

def test_no_unscoped_rule_in_the_built_sheet():
    for dark in (False, True):
        assert qss.unscoped_rules(qss.build(dark=dark)) == ()


def test_unscoped_rule_is_detected():
    assert qss.unscoped_rules("QWidget { background: #FF0000; }") == ("QWidget",)
    assert qss.unscoped_rules("* { background: #FF0000; }") == ("*",)
    assert qss.unscoped_rules("QWidget { color: #FF0000; }") == ()


def test_unscoped_rule_makes_build_raise(monkeypatch):
    monkeypatch.setattr(qss, "widget_kit_qss",
                        lambda **_kw: "\nQWidget { background: #FF0000; }\n")
    qss.invalidate()
    with pytest.raises(ValueError, match="cascade"):
        qss.build(dark=False)
    qss.invalidate()


@pytest.mark.slow
def test_an_unscoped_rule_really_repaints_descendants(qapp, sheet_bench):
    """`QWidget{background}` on a root turns its children the same colour."""
    root = QWidget()
    root.resize(60, 30)
    child = QLabel("", root)
    child.setGeometry(10, 10, 20, 10)

    def child_pixel(sheet: str) -> str:
        sheet_bench.apply(sheet)
        image = QImage(60, 30, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        root.render(image)
        return QImage.pixelColor(image, 20, 15).name().upper()

    critical = theme.T("SystemFillColorCritical", dark=False)
    cascaded = child_pixel("QWidget { background: %s; }" % critical)
    scoped = child_pixel("#Root { background: %s; }" % critical)
    root.setObjectName("Root")
    assert cascaded == critical.upper()
    assert scoped != cascaded


# ═════════════════════════════════════════════════════════════════════════════
# Workaround 3 — WA_StyledBackground
# ═════════════════════════════════════════════════════════════════════════════

def test_widget_subclass_needs_styled_background(qapp):
    class Panel(QWidget):
        pass

    panel = Panel()
    assert not qss.check_styled_background(panel)
    panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    assert qss.check_styled_background(panel)


def test_frame_subclass_does_not(qapp):
    class Card(QFrame):
        pass

    assert qss.check_styled_background(Card())


def test_every_kit_widget_can_paint_a_styled_background(qapp):
    """The invariant the workaround exists for, asserted on the shipped kit.

    A direct `QWidget` subclass paints no QSS background without
    `WA_StyledBackground`; every widget in the kit therefore either sets it or
    derives from a Qt class that already paints one.
    """
    from onedriveui.ui.widgets import controls, indicators
    from onedriveui.models import FileState

    widgets = [
        controls.FluentButton(),
        controls.FluentLineEdit(),
        controls.FluentCheckBox(),
        controls.FluentRadioButton(),
        controls.FluentComboBox(),
        controls.ToggleSwitch(),
        indicators.ProgressRing(),
        indicators.FluentProgressBar(),
        indicators.StorageBar(),
        indicators.Avatar(),
        indicators.StatusBadge(state=FileState.LOCAL),
    ]
    for widget in widgets:
        assert qss.check_styled_background(widget), type(widget).__name__


# ═════════════════════════════════════════════════════════════════════════════
# Workaround 4 — setProperty needs a repolish
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
def test_set_property_repolishes_and_bare_set_property_does_not(qapp, sheet_bench):
    """The accent rule only takes effect after unpolish/polish."""
    sheet_bench.apply(qss.build(dark=False))
    accent = theme.accent("rest", dark=False).upper()

    def centre(button) -> str:
        image = QImage(80, 32, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        button.render(image)
        return QImage.pixelColor(image, 40, 16).name().upper()

    stale = FluentButton()
    stale.resize(80, 32)
    stale.ensurePolished()
    before = centre(stale)
    stale.setProperty(PROP.ACCENT, True)          # deliberately WITHOUT a repolish
    assert centre(stale) == before, "Qt started repolishing on its own"

    fresh = FluentButton()
    fresh.resize(80, 32)
    fresh.ensurePolished()
    qss.set_property(fresh, PROP.ACCENT, True)
    assert centre(fresh) == accent
    assert fresh.property(PROP.ACCENT) is True


def test_repolish_deep_touches_children(qapp, sheet_bench):
    sheet_bench.apply(qss.build(dark=False))
    root = QWidget()
    child = FluentButton()
    child.setParent(root)
    qss.repolish(root, deep=True)
    assert child.style() is not None


def test_set_object_name_repolishes(qapp, sheet_bench):
    sheet_bench.apply(qss.build(dark=False))
    button = FluentButton()
    qss.set_object_name(button, theme.OBJ.SUBTLE_BUTTON)
    assert button.objectName() == theme.OBJ.SUBTLE_BUTTON


# ═════════════════════════════════════════════════════════════════════════════
# Workaround 5 — the focus compensation is declared
# ═════════════════════════════════════════════════════════════════════════════

def test_focus_compensation_is_present_in_the_built_sheet():
    for dark in (False, True):
        assert qss.check_focus_compensation(qss.build(dark=dark))


def test_missing_focus_compensation_is_detected():
    bad = qss.textbox_box_qss(compensate=False)
    assert not qss.check_focus_compensation(bad)
    with pytest.raises(ValueError, match="32 to 33"):
        qss.validate(qss.button_box_qss() + bad)


def test_focus_delta_matches_the_border_growth():
    """The border grows by `focus_outer - 1`; padding must repay exactly that."""
    assert qss.TEXTBOX_FOCUS_DELTA == METRICS["focus_outer"] - 1


# ═════════════════════════════════════════════════════════════════════════════
# The sheet itself
# ═════════════════════════════════════════════════════════════════════════════

def test_build_composes_the_frozen_sheet_with_the_widget_kit():
    for dark in (False, True):
        sheet = qss.build(dark=dark)
        assert sheet.startswith(theme.stylesheet(dark=dark))
        assert qss.SEL.BUTTON in sheet
        assert qss.SEL.LINE_EDIT in sheet
        assert qss.SEL.TOGGLE in sheet


def test_build_is_cached_per_theme():
    qss.invalidate()
    first = qss.build(dark=False)
    assert qss.build(dark=False) is first
    assert qss.build(dark=True) is not first
    qss.invalidate()
    assert qss.build(dark=False) is not first


def test_every_colour_is_an_opaque_six_digit_hex():
    """`QColor('#RRGGBBAA')` silently yields the wrong colour; only #RRGGBB ships."""
    for dark in (False, True):
        sheet = qss.build(dark=dark)
        # `#Body` / `#Card` are id selectors, not colours: the lookahead
        # rejects any run of hex digits that is followed by more identifier.
        for match in re.finditer(r"#[0-9A-Fa-f]{3,8}(?![0-9A-Za-z_-])", sheet):
            assert len(match.group(0)) == 7, match.group(0)


def test_no_silently_ignored_property_is_emitted():
    """QSS ignores these without a warning; emitting one is a latent bug."""
    banned = ("box-shadow", "transition:", "transform:", "filter:",
              "backdrop-filter", "z-index", "text-overflow", "linear-gradient(",
              "calc(", "var(--")
    for dark in (False, True):
        sheet = qss.build(dark=dark)
        for token in banned:
            assert token not in sheet, token


def test_custom_painted_widgets_are_kept_out_of_the_box_model():
    """A QSS background under a custom paintEvent changes its geometry."""
    sheet = qss.build(dark=False)
    parsed = dict(qss.rules(sheet))
    joined = ", ".join(qss.CUSTOM_PAINTED)
    assert joined in parsed
    body = " ".join(parsed[joined].split())
    assert "background: transparent" in body
    assert "border: none" in body


def test_accent_edge_is_darker_than_the_accent():
    """The accent button's bottom stroke reads as an edge in BOTH themes."""
    for dark in (False, True):
        rest = theme.accent("rest", dark=dark)
        edge = qss.accent_edge(dark=dark)
        assert re.fullmatch(r"#[0-9A-F]{6}", edge)

        def luma(hex_: str) -> float:
            r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        assert luma(edge) < luma(rest)


def test_rules_parser_strips_comments_and_splits_cleanly():
    parsed = qss.rules("/* note */ A { x: 1; } B, C { y: 2; }")
    assert parsed == (("A", "x: 1;"), ("B, C", "y: 2;"))


def test_apply_sets_the_sheet(qapp):
    previous_sheet = qapp.styleSheet()
    try:
        applied = qss.apply(qapp, dark=False)
        assert qapp.styleSheet() == applied
        assert applied == qss.build(dark=False)
    finally:
        qapp.setStyleSheet(previous_sheet)


def test_apply_never_clobbers_an_installed_style(qapp):
    """Once a sheet or a proxy owns the style, `setStyle` would throw the
    two-tone focus ring away, so `ensure_fusion` must decline."""
    previous_sheet = qapp.styleSheet()
    try:
        qss.apply(qapp, dark=False)
        assert qapp.style().objectName() == "", "expected the QStyleSheetStyle wrapper"
        before = qapp.style()
        assert qss.ensure_fusion(qapp) is False
        assert qapp.style() is before
    finally:
        qapp.setStyleSheet(previous_sheet)


def test_ensure_fusion_promotes_a_named_platform_style(qapp, monkeypatch):
    """A named non-Fusion style IS replaced — that is the case it exists for."""
    calls = []

    class FakeStyle:
        @staticmethod
        def objectName() -> str:
            return "Breeze"

    class FakeApp:
        @staticmethod
        def style():
            return FakeStyle()

        @staticmethod
        def setStyle(name: str) -> None:
            calls.append(name)

    assert qss.ensure_fusion(FakeApp()) is True
    assert calls == ["Fusion"]


def test_workarounds_are_documented():
    assert len(qss.WORKAROUNDS) == 5
    assert all(isinstance(item, str) and item for item in qss.WORKAROUNDS)


def test_selectors_are_declared_once():
    names = [value for key, value in vars(qss.SEL).items() if not key.startswith("_")]
    assert len(names) == len(set(names))
    assert set(qss.CUSTOM_PAINTED) <= set(names)
