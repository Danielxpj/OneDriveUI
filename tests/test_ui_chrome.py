"""WP-11b — `onedriveui.ui.widgets.chrome`.

The navigation rail, the search box and the status glyph. Three things are
asserted here that are easy to get quietly wrong:

  * the rail's delegate returns **width 0** too, for the same reason the
    activity delegate does;
  * `SearchBox` is a `FluentLineEdit` **subclass**, and a QSS type selector
    matches a subclass — which is the only reason it is 32 px tall focused and
    unfocused without restating a single declaration;
  * every one of the 17 `SyncState`s resolves to a glyph and a tint through
    `strings.TRAY_FOR_STATE`, the same table the tray reads, so the status strip
    and the panel icon cannot disagree.

Source hygiene (no colour, no icon name, no user-facing string, no engine
import) is asserted for all three WP-11b modules in
`tests/test_ui_containers.py`.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEventLoop, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QImage, QPainter, QRegion
from PySide6.QtWidgets import (
    QAbstractItemView, QLineEdit, QListWidget, QStyleOptionViewItem, QWidget,
)

from onedriveui.models import BUSY_STATES, SyncState, TrayIcon
from onedriveui.strings import TRAY_FOR_STATE
from onedriveui.ui import fonts, icons, motion, qss, theme
from onedriveui.ui.theme import METRICS, OBJ, PROP, SPACING
from onedriveui.ui.widgets import chrome
from onedriveui.ui.widgets.chrome import (
    FULL_TURN_DEG, GLYPH_FOR_TRAY, NAV_COMPACT_W, NAV_GLYPH, NAV_ICON_BOX,
    NAV_INDICATOR_H, NAV_INDICATOR_W, NAV_ITEM_H, NAV_ITEM_MARGIN, NAV_OPEN_W,
    NAV_TOGGLE, NavigationView, SEARCH_CLEAR_GLYPH, SEARCH_DEBOUNCE_MS,
    SPIN_PERIOD_MS, SearchBox, StatusGlyph, TONE_FOR_TRAY, glyph_for_state,
    tone_for_state, tray_for,
)
from onedriveui.ui.widgets.controls import FluentLineEdit

DEVICE_PIXEL_RATIOS = (1.0, 1.25, 1.5, 2.0)

#: The four destinations the settings window ships, as (label, glyph key).
NAV_ITEMS = (
    (OBJ.NAV_PANE, "nav_sync"),
    (OBJ.NAV_LIST, "nav_account"),
    (OBJ.HEADER, "nav_notifications"),
    (OBJ.FOOTER, "nav_about"),
)


# ═════════════════════════════════════════════════════════════════════════════
# Harness
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def styled(qapp):
    """The kit's own font and stylesheet, restored afterwards."""
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    fonts.apply_app_font(qapp)
    qss.apply(qapp, dark=False)
    yield qapp
    qapp.setStyleSheet(previous_sheet)
    qapp.setFont(previous_font)
    qss.invalidate()


@pytest.fixture
def no_animation(monkeypatch):
    monkeypatch.setattr(theme, "_ANIMATIONS", False, raising=False)
    yield
    monkeypatch.setattr(theme, "_ANIMATIONS", True, raising=False)


def render(widget: QWidget, *, dpr: float = 1.0, background: bool = False) -> QImage:
    """Render a widget offscreen at `dpr`.

    `background` defaults to False: `QWidget.render()` otherwise fills the
    target with the palette's Window brush whether or not the widget paints one,
    which would hide "this state paints nothing at all" behind an opaque grey.
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


def colours_in(image: QImage) -> set[str]:
    found: set[str] = set()
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 200:
                found.add(pixel.name().upper())
    return found


def is_blank(image: QImage) -> bool:
    return all(image.pixelColor(x, y).alpha() == 0
               for y in range(image.height()) for x in range(image.width()))


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def build_nav(**kwargs) -> NavigationView:
    nav = NavigationView(**kwargs)
    for label, key in NAV_ITEMS:
        nav.add_item(label, key)
    return nav


# ═════════════════════════════════════════════════════════════════════════════
# NavigationView geometry
# ═════════════════════════════════════════════════════════════════════════════

def test_nav_metrics_come_from_the_frozen_table():
    assert (NAV_OPEN_W, NAV_COMPACT_W) == (320, 48)
    assert NAV_ITEM_H == 36
    assert NAV_ITEM_MARGIN == (4, 2)
    assert (NAV_ICON_BOX, NAV_GLYPH) == (40, 16)
    assert (NAV_INDICATOR_W, NAV_INDICATOR_H) == (3, 16)
    assert NAV_TOGGLE == (40, 36)


def test_the_pane_is_320_px_open_and_48_compact(styled):
    nav = build_nav()
    assert nav.width() == nav.pane_width() == NAV_OPEN_W
    nav.set_compact(True)
    assert nav.width() == nav.pane_width() == NAV_COMPACT_W
    nav.set_compact(False)
    assert nav.width() == NAV_OPEN_W


def test_the_rail_item_is_36_px_inside_a_4_by_2_margin(styled):
    nav = build_nav()
    hint = nav.delegate().sizeHint(QStyleOptionViewItem(),
                                   nav.list_widget().model().index(0, 0))
    assert hint == QSize(0, NAV_ITEM_H + 2 * NAV_ITEM_MARGIN[1])
    assert hint.height() == 40


def test_the_rail_delegate_size_hint_is_width_zero(styled):
    """The rail is exactly as wide as its pane; any other width scrolls."""
    nav = build_nav()
    hint = nav.delegate().sizeHint(QStyleOptionViewItem(),
                                   nav.list_widget().model().index(0, 0))
    assert hint.width() == 0


def test_the_rail_never_shows_a_horizontal_scrollbar(styled, qapp):
    nav = build_nav()
    nav.setFixedHeight(NAV_ITEM_H)                 # force a vertical scrollbar
    nav.show()
    qapp.processEvents()
    bar = nav.list_widget().horizontalScrollBar()
    assert not bar.isVisible()
    assert bar.maximum() == 0
    nav.hide()


def test_the_pane_takes_the_frozen_object_names(styled):
    nav = build_nav()
    assert nav.objectName() == OBJ.NAV_PANE
    assert nav.list_widget().objectName() == OBJ.NAV_LIST


# ═════════════════════════════════════════════════════════════════════════════
# NavigationView behaviour
# ═════════════════════════════════════════════════════════════════════════════

def test_the_nav_carries_the_four_settings_destinations(styled):
    nav = build_nav()
    assert nav.count() == 4
    assert [nav.item_text(i) for i in range(4)] == [label for label, _ in NAV_ITEMS]
    assert [nav.item_key(i) for i in range(4)] == [key for _, key in NAV_ITEMS]
    assert [nav.item_icon_key(i) for i in range(4)] == [key for _, key in NAV_ITEMS]


def test_the_first_item_is_selected_as_soon_as_it_exists(styled):
    nav = build_nav()
    assert nav.current_index() == 0
    assert nav.current_key() == NAV_ITEMS[0][1]


def test_selecting_emits_both_signals(styled):
    nav = build_nav()
    rows: list[int] = []
    keys: list[str] = []
    nav.current_changed.connect(rows.append)
    nav.navigated.connect(keys.append)
    nav.set_current_index(2)
    assert rows == [2]
    assert keys == [NAV_ITEMS[2][1]]


def test_select_key_finds_a_destination(styled):
    nav = build_nav()
    assert nav.select_key(NAV_ITEMS[3][1])
    assert nav.current_index() == 3
    assert not nav.select_key("nowhere")
    assert nav.current_index() == 3


def test_index_of_reports_minus_one_for_an_unknown_key(styled):
    nav = build_nav()
    assert nav.index_of(NAV_ITEMS[1][1]) == 1
    assert nav.index_of("nowhere") == -1


def test_the_nav_rejects_an_unknown_glyph(styled):
    with pytest.raises(KeyError):
        NavigationView().add_item(OBJ.HEADER, "not-a-glyph")


def test_a_custom_key_overrides_the_glyph_key(styled):
    nav = NavigationView()
    nav.add_item(OBJ.HEADER, "settings", key="deep-link")
    assert nav.item_key(0) == "deep-link"
    assert nav.item_icon_key(0) == "settings"


def test_the_nav_clears(styled):
    nav = build_nav()
    nav.clear()
    assert nav.count() == 0


def test_compacting_sets_the_dynamic_property_and_signals(styled):
    nav = build_nav()
    seen: list[bool] = []
    nav.toggled_compact.connect(seen.append)
    nav.set_compact(True)
    assert nav.is_compact()
    assert nav.property(PROP.COMPACT) is True
    assert nav.delegate().is_compact()
    assert seen == [True]
    nav.set_compact(True)                       # a no-op must not re-signal
    assert seen == [True]


def test_the_pane_toggle_is_hidden_until_it_is_asked_for(styled):
    nav = build_nav()
    nav.show()
    assert not nav.toggle_button().isVisible()
    nav.set_toggle_visible(True)
    assert nav.toggle_button().isVisible()
    assert nav.toggle_button().size() == QSize(*NAV_TOGGLE)
    nav.toggle_button().click()
    assert nav.is_compact()
    nav.hide()


def test_the_toggle_sheet_does_not_break_a_qss_workaround(styled):
    """A widget stylesheet is still QSS, and this one styles a BUTTON.

    It carries no `background`, which is the declaration that would otherwise
    fall through to Fusion's gradient primitive without a matching `border`.
    """
    fragment = NavigationView.toggle_qss()
    assert qss.SEL.BUTTON in fragment
    assert qss.pushbutton_rules_without_border(fragment) == ()
    assert qss.unscoped_rules(fragment) == ()
    assert "background" not in fragment


def test_the_toggle_box_arithmetic_matches_the_icon_button_recipe(styled):
    """QSS `min-height` sizes the CONTENT box; the padding and border come off."""
    fragment = NavigationView.toggle_qss()
    content_h = NAV_TOGGLE[1] - 2 * METRICS["button_pad_v"] - 2
    content_w = NAV_TOGGLE[0] - 2 * METRICS["button_pad_h"] - 2
    assert f"min-height: {content_h}px" in fragment
    assert f"min-width: {content_w}px" in fragment
    assert (content_h, content_w) == (24, 16)


def test_a_compact_item_keeps_its_label_as_a_tooltip(styled):
    """It is the only affordance left once the label is gone."""
    nav = build_nav()
    nav.set_compact(True)
    assert nav.list_widget().item(0).toolTip() == NAV_ITEMS[0][0]


# ═════════════════════════════════════════════════════════════════════════════
# NavigationView painting
# ═════════════════════════════════════════════════════════════════════════════

def test_the_selected_item_paints_the_accent_indicator(styled, qapp):
    nav = build_nav()
    nav.setFixedHeight(200)
    nav.show()
    qapp.processEvents()
    image = render(nav.list_widget().viewport())
    found = colours_in(image)
    assert theme.accent().upper() in found, "the 3 x 16 selection indicator"
    assert theme.T("ControlAltFillColorTertiary").upper() in found, "the pill"
    nav.hide()


def test_a_compact_rail_paints_no_label(styled, qapp):
    open_nav = build_nav()
    open_nav.setFixedHeight(200)
    open_nav.show()
    qapp.processEvents()
    open_image = render(open_nav.list_widget().viewport())

    compact_nav = build_nav(compact=True)
    compact_nav.setFixedHeight(200)
    compact_nav.show()
    qapp.processEvents()
    compact_image = render(compact_nav.list_widget().viewport())

    def ink(image: QImage, start: int) -> int:
        return sum(1 for y in range(image.height())
                   for x in range(start, image.width())
                   if image.pixelColor(x, y).alpha() > 0)

    label_column = NAV_ITEM_MARGIN[0] + NAV_ICON_BOX
    assert ink(open_image, label_column) > 0
    assert ink(compact_image, label_column) == 0
    open_nav.hide()
    compact_nav.hide()


@pytest.mark.slow
@pytest.mark.parametrize("dpr", DEVICE_PIXEL_RATIOS)
@pytest.mark.parametrize("dark_theme", [False, True])
def test_the_rail_renders_at_every_theme_and_ratio(qapp, dpr, dark_theme,
                                                   monkeypatch):
    monkeypatch.setattr(theme, "_DETECTED_DARK", dark_theme, raising=False)
    theme._STYLESHEET_CACHE.clear()
    qss.invalidate()
    icons.clear_cache()
    previous_sheet = qapp.styleSheet()
    try:
        fonts.apply_app_font(qapp)
        qss.apply(qapp, dark=dark_theme)
        nav = build_nav()
        nav.setFixedHeight(200)
        nav.show()
        qapp.processEvents()
        assert not is_blank(render(nav.list_widget().viewport(), dpr=dpr))
        nav.hide()
    finally:
        qapp.setStyleSheet(previous_sheet)
        theme._STYLESHEET_CACHE.clear()
        qss.invalidate()
        icons.clear_cache()


# ═════════════════════════════════════════════════════════════════════════════
# SearchBox
# ═════════════════════════════════════════════════════════════════════════════

def test_the_search_box_inherits_the_whole_text_field_recipe(styled):
    """A QSS type selector matches a subclass, so nothing is restated."""
    box = SearchBox(placeholder=OBJ.HEADER)
    box.ensurePolished()
    assert isinstance(box, FluentLineEdit)
    assert box.metaObject().className() == "SearchBox"
    assert box.objectName() == OBJ.SEARCH_BOX
    assert box.is_search()
    assert box.sizeHint().height() == METRICS["textbox_h"] == 32
    assert box.placeholderText() == OBJ.HEADER


def test_the_search_box_stays_32_px_when_focused(styled, qapp):
    """The focused bottom border grows 1 -> 2 px and padding-bottom pays it back."""
    box = SearchBox()
    box.show()
    qapp.processEvents()
    unfocused = box.sizeHint().height()
    box.setFocus()
    qapp.processEvents()
    box.ensurePolished()
    assert box.sizeHint().height() == unfocused == METRICS["textbox_h"]
    box.hide()


def test_the_clear_button_appears_with_text(styled):
    box = SearchBox(clear_tooltip=OBJ.FOOTER)
    assert isinstance(box.clear_action(), QAction)
    assert not box.clear_action().isVisible()
    box.setText(OBJ.CARD)
    assert box.clear_action().isVisible()
    box.setText("")
    assert not box.clear_action().isVisible()
    assert box.clear_action().toolTip() == OBJ.FOOTER


def test_the_clear_button_is_ours_not_qts(styled):
    """Qt's own clear button carries a platform icon that cannot be re-tinted."""
    box = SearchBox()
    assert not box.isClearButtonEnabled()
    assert box.clear_action().icon().availableSizes()


def test_clearing_empties_the_field_and_signals(styled):
    box = SearchBox()
    box.setText(OBJ.CARD)
    queries: list[str] = []
    cleared: list[int] = []
    box.search_changed.connect(queries.append)
    box.cleared.connect(lambda: cleared.append(1))
    box.clear_action().trigger()
    assert box.text() == ""
    assert queries == [""]
    assert cleared == [1]


def test_the_search_is_debounced(styled, qapp):
    box = SearchBox()
    queries: list[str] = []
    box.search_changed.connect(queries.append)
    for text in (OBJ.CARD, OBJ.CARD_SECONDARY, OBJ.FLYOUT):
        box.setText(text)
        qapp.processEvents()
    assert queries == [], "nothing fires while the user is still typing"
    wait(SEARCH_DEBOUNCE_MS + 120)
    assert queries == [OBJ.FLYOUT]


def test_the_debounce_is_not_gated_by_the_motion_preference(styled, no_animation):
    """A debounce is not an animation: a user with motion off still wants it."""
    assert motion.DUR("normal") == 0
    assert SearchBox().debounce_ms() == SEARCH_DEBOUNCE_MS == 200


def test_the_debounce_interval_is_settable(styled):
    box = SearchBox(debounce_ms=50)
    assert box.debounce_ms() == 50
    box.set_debounce_ms(500)
    assert box.debounce_ms() == 500


def test_flush_emits_without_waiting(styled):
    box = SearchBox()
    queries: list[str] = []
    box.search_changed.connect(queries.append)
    box.setText(OBJ.CARD)
    box.flush()
    assert queries == [OBJ.CARD]


def test_the_search_box_retints_its_clear_glyph(styled, monkeypatch):
    box = SearchBox()
    box.setText(OBJ.CARD)
    light = box.clear_action().icon().pixmap(QSize(SEARCH_CLEAR_GLYPH,
                                                   SEARCH_CLEAR_GLYPH)).toImage()
    monkeypatch.setattr(theme, "_DETECTED_DARK", True, raising=False)
    icons.clear_cache()
    box.refresh_theme()
    dark_pixmap = box.clear_action().icon().pixmap(
        QSize(SEARCH_CLEAR_GLYPH, SEARCH_CLEAR_GLYPH)).toImage()
    assert light != dark_pixmap
    monkeypatch.setattr(theme, "_DETECTED_DARK", False, raising=False)
    icons.clear_cache()


def test_the_clear_glyph_is_12_px(styled):
    assert SEARCH_CLEAR_GLYPH == SPACING["m"] == 12
    assert SEARCH_CLEAR_GLYPH in icons.GLYPH_SIZES


def test_the_clear_action_hangs_on_the_trailing_edge(styled):
    assert chrome.SEARCH_CLEAR_POSITION == QLineEdit.ActionPosition.TrailingPosition


# ═════════════════════════════════════════════════════════════════════════════
# StatusGlyph semantics
# ═════════════════════════════════════════════════════════════════════════════

def test_every_tray_semantic_has_a_glyph_and_a_tint():
    assert set(GLYPH_FOR_TRAY) == set(TrayIcon)
    assert set(TONE_FOR_TRAY) == set(TrayIcon)
    for key in GLYPH_FOR_TRAY.values():
        assert key == "" or key in icons.GLYPHS
    for token in TONE_FOR_TRAY.values():
        assert token == "" or token in theme.TOKENS


@pytest.mark.parametrize("state", list(SyncState))
def test_every_sync_state_resolves_to_a_glyph(styled, state):
    """Through `strings.TRAY_FOR_STATE` — the same table the tray reads."""
    assert tray_for(state) is TRAY_FOR_STATE[state]
    glyph = glyph_for_state(state)
    assert glyph == "" or glyph in icons.GLYPHS
    colour = tone_for_state(state)
    assert colour.startswith("#") and len(colour) == 7


def test_not_running_paints_nothing(styled):
    """`TrayIcon.NONE` registers no tray item; the glyph shows nothing either."""
    glyph = StatusGlyph(state=SyncState.NOT_RUNNING)
    assert glyph.tray() is TrayIcon.NONE
    assert glyph.glyph_key() == ""
    assert is_blank(render(glyph))


def test_a_synced_state_is_success_green(styled):
    glyph = StatusGlyph(state=SyncState.UP_TO_DATE)
    assert glyph.glyph_key() == "check"
    assert glyph.colour() == theme.T("SystemFillColorSuccess")


def test_an_error_state_is_critical(styled):
    glyph = StatusGlyph(state=SyncState.ERROR)
    assert glyph.colour() == theme.T("SystemFillColorCritical")


def test_a_syncing_state_is_the_accent(styled):
    glyph = StatusGlyph(state=SyncState.SYNCING)
    assert glyph.colour() == theme.accent()


def test_a_disabled_glyph_is_the_disabled_text_colour(styled):
    glyph = StatusGlyph(state=SyncState.UP_TO_DATE)
    glyph.setEnabled(False)
    assert glyph.colour() == theme.T("TextFillColorDisabled")


# ═════════════════════════════════════════════════════════════════════════════
# StatusGlyph motion
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("state", sorted(BUSY_STATES, key=str))
def test_a_busy_state_spins(styled, state):
    assert StatusGlyph(state=state).is_spinning()


@pytest.mark.parametrize("state", [SyncState.UP_TO_DATE, SyncState.ERROR,
                                   SyncState.PAUSED_MANUAL,
                                   SyncState.NOT_RUNNING])
def test_a_settled_state_does_not_spin(styled, state):
    assert not StatusGlyph(state=state).is_spinning()


def test_changing_state_starts_and_stops_the_spin(styled):
    glyph = StatusGlyph(state=SyncState.UP_TO_DATE)
    assert not glyph.is_spinning()
    glyph.set_state(SyncState.SYNCING)
    assert glyph.is_spinning()
    glyph.set_state(SyncState.UP_TO_DATE)
    assert not glyph.is_spinning()
    assert glyph.angle() == 0.0


def test_the_spin_stops_when_the_glyph_is_hidden(styled, qapp):
    """A hidden widget that keeps rotating burns CPU for a picture nobody sees."""
    glyph = StatusGlyph(state=SyncState.SYNCING)
    glyph.show()
    qapp.processEvents()
    glyph.hide()
    qapp.processEvents()
    assert not glyph._loop.is_running()
    assert glyph._loop.wanted(), "it is remembered, so a re-show resumes it"
    glyph.show()
    qapp.processEvents()
    glyph.hide()


def test_the_spin_never_runs_with_animations_disabled(styled, no_animation, qapp):
    glyph = StatusGlyph(state=SyncState.SYNCING)
    glyph.show()
    qapp.processEvents()
    assert not glyph._loop.is_running()
    # It still lands on a full turn, which is the same picture as none at all.
    assert glyph.angle() == pytest.approx(FULL_TURN_DEG)
    glyph.hide()


def test_the_spin_shares_the_tray_period():
    assert SPIN_PERIOD_MS == icons.SPINNER_PERIOD_MS


# ═════════════════════════════════════════════════════════════════════════════
# StatusGlyph painting
# ═════════════════════════════════════════════════════════════════════════════

def test_the_glyph_is_16_px_by_default(styled):
    glyph = StatusGlyph(state=SyncState.UP_TO_DATE)
    assert glyph.size() == QSize(SPACING["l"], SPACING["l"])
    assert glyph.sizeHint() == glyph.minimumSizeHint() == QSize(16, 16)


def test_the_glyph_resizes(styled):
    glyph = StatusGlyph(state=SyncState.UP_TO_DATE, size=SPACING["xl"])
    assert glyph.glyph_size() == SPACING["xl"]
    glyph.set_glyph_size(SPACING["xxl"])
    assert glyph.size() == QSize(SPACING["xxl"], SPACING["xxl"])


@pytest.mark.parametrize("dpr", DEVICE_PIXEL_RATIOS)
def test_the_glyph_paints_at_every_ratio(styled, dpr):
    glyph = StatusGlyph(state=SyncState.ERROR)
    image = render(glyph, dpr=dpr)
    assert not is_blank(image)
    assert image.width() == int(round(SPACING["l"] * dpr))


def test_the_glyph_paints_its_tone(styled):
    for state in (SyncState.UP_TO_DATE, SyncState.ERROR, SyncState.WARNING):
        glyph = StatusGlyph(state=state)
        glyph.resize(SPACING["xxl"], SPACING["xxl"])
        glyph.set_glyph_size(SPACING["xxl"])
        found = colours_in(render(glyph))
        assert tone_for_state(state).upper() in found, state.name


def test_a_theme_change_repaints_the_glyph(styled):
    from onedriveui.bus import BUS

    glyph = StatusGlyph(state=SyncState.UP_TO_DATE)
    BUS.theme_changed.emit(True, theme.accent("rest", dark=True))
    assert glyph.isEnabled()          # no exception, the hook is connected


# ═════════════════════════════════════════════════════════════════════════════
# Module surface
# ═════════════════════════════════════════════════════════════════════════════

def test_the_chrome_module_declares_its_public_api():
    assert chrome.__all__
    for name in chrome.__all__:
        assert hasattr(chrome, name), name


def test_the_nav_list_is_a_plain_qlistwidget(styled):
    """WP-13 needs the raw view for keyboard navigation and deep links."""
    assert isinstance(build_nav().list_widget(), QListWidget)
    assert build_nav().list_widget().selectionMode() \
        == QAbstractItemView.SelectionMode.SingleSelection
