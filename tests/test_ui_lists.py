"""WP-11b — `onedriveui.ui.widgets.lists`.

The two headline acceptance criteria live here:

  * a full-width list view shows **no** horizontal scrollbar, because the
    delegate's `sizeHint` returns width 0 — and the same view with a delegate
    that answers `option.rect.width()` grows one, which is what makes the zero
    load-bearing rather than decorative;
  * tri-state propagation runs in **both** directions and does not recurse.

Source hygiene (no colour, no icon name, no user-facing string, no engine
import) is asserted for all three WP-11b modules in
`tests/test_ui_containers.py`.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QImage, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QListView, QStyle, QStyleOptionViewItem,
)

from onedriveui.constants import ACTIVITY_CAP_ROWS
from onedriveui.models import (
    ActivityEvent, ActivityState, ActivityVerb, FileState, IssueCode,
)
from onedriveui.strings import FILE_STATE_LABEL, VERB_LABEL, issue_title
from onedriveui.ui import fonts, icons, qss, theme
from onedriveui.ui.theme import METRICS, OBJ, SPACING
from onedriveui.ui.widgets import lists
from onedriveui.ui.widgets.lists import (
    BAR_FILL_H, BAR_TRACK_H, FILE_STATE_FOR_ACTIVITY, PATH_ROLE,
    ROW_H, ROW_H_COMPACT, ROW_ICON, ROW_ICON_GAP, ROW_INSET, ROW_PILL_INSET,
    ROW_ROLE, ROW_STATUS, SELECTION_BAR_H, SELECTION_BAR_W, STATUS_TOKEN,
    SUBTITLE_SEPARATOR, TREE_INDICATOR, TREE_ROW_H, ActivityDelegate,
    ActivityListView, ActivityRow, FolderTree, TriStateItem, rel_path_of,
    status_colour,
)

DEVICE_PIXEL_RATIOS = (1.0, 1.25, 1.5, 2.0)
#: A width that comfortably holds an activity row: the flyout's own.
VIEW_W = METRICS["ac_width"]


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


def sample_row(**overrides) -> ActivityRow:
    """A realistic in-flight upload row."""
    fields = dict(
        name=OBJ.CARD, verb=ActivityVerb.UPLOADED,
        state=ActivityState.INFLIGHT, file_state=FileState.SYNCING,
        time_text=OBJ.HEADER, rate_text=OBJ.FOOTER, eta_text=OBJ.DIVIDER,
        progress=0.42,
    )
    fields.update(overrides)
    return ActivityRow(**fields)


def model_of(rows) -> QStandardItemModel:
    model = QStandardItemModel()
    for row in rows:
        item = QStandardItem()
        item.setData(row, ROW_ROLE)
        model.appendRow(item)
    return model


def paint_row(delegate: ActivityDelegate, row: ActivityRow, *,
              width: int = VIEW_W, selected: bool = False,
              hovered: bool = False, dpr: float = 1.0) -> QImage:
    """Paint one row into a transparent image and return it."""
    model = model_of([row])
    height = delegate.row_height()
    image = QImage(int(width * dpr), int(height * dpr),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    option = QStyleOptionViewItem()
    option.rect = image.rect().adjusted(0, 0, 0, 0)
    option.rect.setSize(QSize(width, height))
    option.state = QStyle.StateFlag.State_Enabled
    if selected:
        option.state |= QStyle.StateFlag.State_Selected
    if hovered:
        option.state |= QStyle.StateFlag.State_MouseOver
    painter = QPainter(image)
    delegate.paint(painter, option, model.index(0, 0))
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


# ═════════════════════════════════════════════════════════════════════════════
# ActivityRow
# ═════════════════════════════════════════════════════════════════════════════

def test_row_geometry_comes_from_the_frozen_metrics():
    assert (ROW_H, ROW_H_COMPACT) == (56, 48)
    assert (ROW_INSET, ROW_ICON, ROW_ICON_GAP, ROW_STATUS) == (16, 32, 12, 20)
    assert ROW_PILL_INSET == SPACING["xs"]
    assert (SELECTION_BAR_W, SELECTION_BAR_H) == (3, 16)
    assert (BAR_FILL_H, BAR_TRACK_H) == (3, 1)


def test_second_line_joins_verb_time_rate_and_eta():
    row = sample_row()
    parts = row.second_line().split(SUBTITLE_SEPARATOR)
    assert parts == [VERB_LABEL[str(ActivityVerb.UPLOADED)], OBJ.HEADER,
                     OBJ.FOOTER, OBJ.DIVIDER]


def test_second_line_skips_what_it_was_not_given():
    row = ActivityRow(name=OBJ.CARD, verb=ActivityVerb.DELETED)
    assert row.second_line() == VERB_LABEL[str(ActivityVerb.DELETED)]


def test_an_explicit_subtitle_wins():
    row = sample_row(subtitle=OBJ.FLYOUT)
    assert row.second_line() == OBJ.FLYOUT


def test_every_verb_has_a_label():
    for verb in ActivityVerb:
        assert VERB_LABEL[str(verb)]
        assert ActivityRow(verb=verb).second_line() == VERB_LABEL[str(verb)]


def test_glyph_key_falls_back_to_folder_or_document():
    assert ActivityRow(is_dir=True).glyph_key() == "folder"
    assert ActivityRow(is_dir=False).glyph_key() == "file"
    assert ActivityRow(icon_key="file_pdf").glyph_key() == "file_pdf"
    assert ActivityRow(icon_key="file_pdf").glyph_key() in icons.GLYPHS


def test_has_progress_and_has_error():
    assert not ActivityRow().has_progress()
    assert ActivityRow(progress=0.0).has_progress()
    assert not ActivityRow().has_error()
    assert ActivityRow(error_text=OBJ.CARD).has_error()


def test_with_progress_returns_a_new_frozen_row():
    row = sample_row()
    updated = row.with_progress(0.9)
    assert updated.progress == pytest.approx(0.9)
    assert row.progress == pytest.approx(0.42)
    assert updated.name == row.name


def test_row_is_frozen_and_slotted():
    row = ActivityRow()
    with pytest.raises(Exception):
        row.name = OBJ.CARD


# ═════════════════════════════════════════════════════════════════════════════
# ActivityRow.from_event
# ═════════════════════════════════════════════════════════════════════════════

def test_from_event_maps_an_in_flight_upload():
    event = ActivityEvent(name=OBJ.CARD, verb=ActivityVerb.UPLOADED,
                          state=ActivityState.INFLIGHT, bytes=42, size=100)
    row = ActivityRow.from_event(event, time_text=OBJ.HEADER)
    assert row.name == OBJ.CARD
    assert row.verb is ActivityVerb.UPLOADED
    assert row.file_state is FileState.SYNCING
    assert row.progress == pytest.approx(0.42)
    assert row.time_text == OBJ.HEADER


def test_from_event_takes_the_basename_when_there_is_no_name():
    event = ActivityEvent(rel_path="Documents/Reports/q3.xlsx")
    assert ActivityRow.from_event(event).name == "q3.xlsx"


def test_from_event_never_shows_a_raw_error():
    """Invariant: rclone's own text is diagnostics, never chrome."""
    event = ActivityEvent(state=ActivityState.ERROR,
                          error="Failed to copy: 429 tooManyRequests",
                          error_kind=IssueCode.THROTTLED)
    row = ActivityRow.from_event(event)
    assert row.error_text == issue_title(IssueCode.THROTTLED)
    assert "429" not in row.error_text


def test_from_event_falls_back_to_the_generic_problem_label():
    event = ActivityEvent(state=ActivityState.ERROR, error_kind=None)
    row = ActivityRow.from_event(event)
    assert row.error_text == FILE_STATE_LABEL[str(FileState.ERROR)]


def test_from_event_prefers_a_supplied_error_text():
    event = ActivityEvent(state=ActivityState.ERROR,
                          error_kind=IssueCode.THROTTLED)
    row = ActivityRow.from_event(event, error_text=OBJ.FLYOUT)
    assert row.error_text == OBJ.FLYOUT


def test_from_event_draws_no_bar_for_a_finished_row():
    for state in (ActivityState.DONE, ActivityState.ERROR,
                  ActivityState.CANCELLED, ActivityState.INTERRUPTED):
        event = ActivityEvent(state=state, bytes=50, size=100)
        assert not ActivityRow.from_event(event).has_progress()


def test_from_event_handles_a_zero_byte_transfer():
    event = ActivityEvent(state=ActivityState.INFLIGHT, bytes=0, size=0)
    assert ActivityRow.from_event(event).progress == pytest.approx(0.0)


def test_every_activity_state_maps_to_a_file_state():
    assert set(FILE_STATE_FOR_ACTIVITY) == set(ActivityState)
    # An interrupted row does not know its outcome and must not claim success.
    assert FILE_STATE_FOR_ACTIVITY[ActivityState.INTERRUPTED] is FileState.UNKNOWN


def test_from_event_lets_the_caller_override_the_file_state():
    event = ActivityEvent(state=ActivityState.DONE)
    row = ActivityRow.from_event(event, file_state=FileState.PINNED,
                                 icon_key="folder")
    assert row.file_state is FileState.PINNED
    assert row.glyph_key() == "folder"


# ═════════════════════════════════════════════════════════════════════════════
# status_colour
# ═════════════════════════════════════════════════════════════════════════════

def test_status_token_covers_every_file_state():
    assert set(STATUS_TOKEN) == set(FileState)


@pytest.mark.parametrize("state", list(FileState))
def test_status_colour_resolves_to_a_theme_colour(styled, state):
    colour = status_colour(state)
    token = STATUS_TOKEN[state]
    expected = theme.T(token, on="layer") if token else theme.accent()
    assert colour == expected
    assert colour.startswith("#") and len(colour) == 7


def test_status_colour_follows_the_surface(styled):
    on_base = status_colour(FileState.ONLINE_ONLY, surface="base")
    on_layer = status_colour(FileState.ONLINE_ONLY, surface="layer")
    assert on_base == theme.T("TextFillColorSecondary", on="base")
    assert on_layer == theme.T("TextFillColorSecondary", on="layer")
    assert on_base != on_layer


# ═════════════════════════════════════════════════════════════════════════════
# The sizeHint acceptance
# ═════════════════════════════════════════════════════════════════════════════

def test_the_delegate_size_hint_is_width_zero(styled):
    """Acceptance: width 0, so the view uses the viewport width."""
    delegate = ActivityDelegate()
    model = model_of([sample_row()])
    hint = delegate.sizeHint(QStyleOptionViewItem(), model.index(0, 0))
    assert hint == QSize(0, ROW_H)
    delegate.set_compact(True)
    assert delegate.sizeHint(QStyleOptionViewItem(), model.index(0, 0)) \
        == QSize(0, ROW_H_COMPACT)


class _FullWidthDelegate(ActivityDelegate):
    """The bug: a delegate that answers with the row's own width."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), self.row_height())


def _hscroll_probe(qapp, delegate_class) -> tuple[bool, int]:
    """-> (visible, maximum) of a full-width list's horizontal scrollbar.

    The sequence is the Activity Center's own lifecycle, and it is what closes
    the feedback loop: the list is shown while it is short, so there is no
    vertical scrollbar and the viewport is the full 360 px; the delegate is
    asked how wide a row is and answers 360; then transfers arrive, the vertical
    scrollbar appears and takes 12 px, and the cached 360 px row no longer fits.
    """
    view = QListView()
    model = QStandardItemModel()
    view.setModel(model)
    view.setItemDelegate(delegate_class(view))
    view.setUniformItemSizes(True)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.resize(VIEW_W, ROW_H * 6)
    view.show()
    for _ in range(3):                      # short: no vertical scrollbar yet
        item = QStandardItem()
        item.setData(sample_row(), ROW_ROLE)
        model.appendRow(item)
    qapp.processEvents()
    assert view.viewport().width() == VIEW_W
    for _ in range(40):                     # now it needs one
        item = QStandardItem()
        item.setData(sample_row(), ROW_ROLE)
        model.appendRow(item)
    qapp.processEvents()
    assert view.viewport().width() < VIEW_W
    bar = view.horizontalScrollBar()
    result = (bar.isVisible(), bar.maximum())
    view.hide()
    return result


def test_a_full_width_list_has_no_horizontal_scrollbar(styled, qapp):
    """Acceptance: `sizeHint` width 0 removes the phantom scrollbar.

    Verified both ways round, and the numbers match the ones in the research
    note: the width-0 hint gives `max=0 visible=False`, the row's own width
    gives `max=12 visible=True`.
    """
    visible, maximum = _hscroll_probe(qapp, ActivityDelegate)
    assert maximum == 0
    assert not visible

    bad_visible, bad_maximum = _hscroll_probe(qapp, _FullWidthDelegate)
    assert bad_maximum > 0, "the width-0 hint is load-bearing, not decorative"
    assert bad_visible


def test_the_activity_list_view_also_pins_the_policy_off(styled, qapp):
    view = ActivityListView()
    view.setModel(model_of([sample_row() for _ in range(40)]))
    view.resize(VIEW_W, ROW_H * 6)
    view.show()
    qapp.processEvents()
    assert view.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert not view.horizontalScrollBar().isVisible()
    view.hide()


# ═════════════════════════════════════════════════════════════════════════════
# ActivityListView wiring
# ═════════════════════════════════════════════════════════════════════════════

def test_the_view_is_wired_the_way_the_delegate_needs(styled):
    view = ActivityListView()
    assert view.objectName() == OBJ.ACTIVITY_LIST
    # Without mouse tracking the view never sets State_MouseOver and the hover
    # pill can never appear.
    assert view.hasMouseTracking()
    assert view.uniformItemSizes()
    assert view.verticalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
    assert view.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert isinstance(view.delegate(), ActivityDelegate)


def test_the_view_switches_to_compact_rows(styled):
    view = ActivityListView()
    assert view.delegate().row_height() == ROW_H
    view.set_compact(True)
    assert view.delegate().is_compact()
    assert view.delegate().row_height() == ROW_H_COMPACT


def test_the_view_emits_row_activated(styled, qapp):
    view = ActivityListView()
    model = model_of([sample_row()])
    view.setModel(model)
    seen: list[object] = []
    view.row_activated.connect(seen.append)
    view.activated.emit(model.index(0, 0))
    assert len(seen) == 1


def test_the_delegate_rejects_an_unknown_surface(styled):
    with pytest.raises(ValueError):
        ActivityDelegate().set_surface("mica")


def test_the_delegate_reads_a_plain_display_role(styled):
    """A string model still renders as names rather than as blank rows."""
    model = QStandardItemModel()
    model.appendRow(QStandardItem(OBJ.CARD))
    row = ActivityDelegate().row_for(model.index(0, 0))
    assert row.name == OBJ.CARD


def test_the_text_column_leaves_room_for_the_icon_and_the_status(styled):
    from PySide6.QtCore import QRectF

    delegate = ActivityDelegate()
    left, width = delegate.text_column(QRectF(0.0, 0.0, float(VIEW_W), float(ROW_H)))
    assert left == ROW_INSET + ROW_ICON + ROW_ICON_GAP == 60
    assert left + width <= VIEW_W - ROW_INSET - ROW_STATUS


# ═════════════════════════════════════════════════════════════════════════════
# Row painting
# ═════════════════════════════════════════════════════════════════════════════

def test_a_row_paints_its_icon_name_and_status(styled):
    image = paint_row(ActivityDelegate(), sample_row())
    assert not is_blank(image)
    # The leading icon lands at the 16 px inset.
    icon_band = [image.pixelColor(x, ROW_H // 2).alpha()
                 for x in range(ROW_INSET, ROW_INSET + ROW_ICON)]
    assert max(icon_band) > 0
    # The trailing status glyph lands at the far inset.
    status_band = [image.pixelColor(x, ROW_H // 2).alpha()
                   for x in range(VIEW_W - ROW_INSET - ROW_STATUS,
                                  VIEW_W - ROW_INSET)]
    assert max(status_band) > 0


def test_an_in_flight_row_paints_its_progress_bar_in_the_accent(styled):
    image = paint_row(ActivityDelegate(), sample_row())
    assert theme.accent().upper() in colours_in(image)


def test_a_finished_row_paints_no_progress_bar(styled):
    done = sample_row(state=ActivityState.DONE, file_state=FileState.LOCAL,
                      progress=-1.0)
    assert theme.accent().upper() not in colours_in(paint_row(ActivityDelegate(), done))


def test_an_error_row_paints_the_chip(styled):
    row = sample_row(state=ActivityState.ERROR, file_state=FileState.ERROR,
                     progress=-1.0, error_text=OBJ.FLYOUT)
    found = colours_in(paint_row(ActivityDelegate(), row))
    assert theme.T("SystemFillColorCritical", on="layer").upper() in found
    assert theme.T("SystemFillColorCriticalBackground", on="layer").upper() in found


def test_a_selected_row_paints_the_accent_selection_bar(styled):
    image = paint_row(ActivityDelegate(), sample_row(progress=-1.0), selected=True)
    bar_x = ROW_PILL_INSET + 1
    centre = ROW_H // 2
    assert image.pixelColor(bar_x, centre).name().upper() == theme.accent().upper()
    assert image.pixelColor(VIEW_W // 2, 1).alpha() > 0     # the pill fill


def test_a_hovered_row_paints_the_subtle_pill(styled):
    image = paint_row(ActivityDelegate(), sample_row(progress=-1.0), hovered=True)
    expected = theme.T("SubtleFillColorSecondary", on="layer").upper()
    assert image.pixelColor(VIEW_W // 2, 2).name().upper() == expected
    # The pill is inset 4 px, so the very edge is untouched.
    assert image.pixelColor(0, 2).alpha() == 0


def test_a_resting_row_paints_no_pill(styled):
    image = paint_row(ActivityDelegate(), sample_row(progress=-1.0))
    assert image.pixelColor(VIEW_W // 2, 1).alpha() == 0


def test_a_compact_row_draws_only_one_line(styled):
    """48 px, no caption line and no inline bar — so no accent anywhere.

    The row's file state is LOCAL rather than SYNCING so that the trailing
    status glyph is success-green: an accent-tinted glyph would make the
    assertion pass for the wrong reason.
    """
    row = sample_row(file_state=FileState.LOCAL)
    tall = paint_row(ActivityDelegate(), row)
    compact = paint_row(ActivityDelegate(compact=True), row)
    assert compact.height() == ROW_H_COMPACT
    assert theme.accent().upper() in colours_in(tall), "the tall row does bar"
    assert theme.accent().upper() not in colours_in(compact)


def test_a_disabled_row_uses_the_disabled_text_colour(styled):
    model = model_of([sample_row(progress=-1.0)])
    image = QImage(VIEW_W, ROW_H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    option = QStyleOptionViewItem()
    option.rect.setSize(QSize(VIEW_W, ROW_H))
    option.state = QStyle.StateFlag(0)
    painter = QPainter(image)
    ActivityDelegate().paint(painter, option, model.index(0, 0))
    painter.end()
    assert theme.T("TextFillColorDisabled", on="layer").upper() in colours_in(image)


@pytest.mark.parametrize("dpr", DEVICE_PIXEL_RATIOS)
def test_a_row_paints_at_every_device_pixel_ratio(styled, dpr):
    image = paint_row(ActivityDelegate(), sample_row(), dpr=dpr)
    assert image.width() == int(VIEW_W * dpr)
    assert not is_blank(image)


def test_the_delegate_caches_its_glyphs(styled):
    delegate = ActivityDelegate()
    paint_row(delegate, sample_row())
    cached = len(delegate._pixmaps)
    assert cached > 0
    paint_row(delegate, sample_row())
    assert len(delegate._pixmaps) == cached
    delegate.refresh_theme()
    assert delegate._pixmaps == {}


# ═════════════════════════════════════════════════════════════════════════════
# Scrolling 5 000 rows — the performance acceptance
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
def test_five_thousand_rows_scroll_smoothly(styled, qapp, capsys):
    """Acceptance: measure and report the paint time per row.

    The budget is a whole millisecond per row — roughly thirty times what a
    60 Hz frame of a 360 x 620 flyout actually needs — so this fails on a real
    regression rather than on a busy machine.
    """
    rows = [sample_row(name=f"{OBJ.CARD}-{index}",
                       progress=(index % 100) / 100.0)
            for index in range(ACTIVITY_CAP_ROWS)]
    assert len(rows) == 5000
    model = model_of(rows)

    view = ActivityListView()
    view.setModel(model)
    view.resize(VIEW_W, ROW_H * 10)
    view.show()
    qapp.processEvents()

    # 1. The delegate's own cost, measured directly over every row.
    delegate = view.delegate()
    image = QImage(VIEW_W, ROW_H, QImage.Format.Format_ARGB32_Premultiplied)
    option = QStyleOptionViewItem()
    option.rect.setSize(QSize(VIEW_W, ROW_H))
    option.state = QStyle.StateFlag.State_Enabled
    painter = QPainter(image)
    started = time.perf_counter()
    for index in range(model.rowCount()):
        delegate.paint(painter, option, model.index(index, 0))
    per_row_ms = (time.perf_counter() - started) * 1000.0 / model.rowCount()
    painter.end()

    # 2. A scroll: 60 viewport repaints, ten rows each.
    bar = view.verticalScrollBar()
    started = time.perf_counter()
    frames = 60
    for frame in range(frames):
        bar.setValue(int(bar.maximum() * frame / frames))
        view.viewport().repaint()
    per_frame_ms = (time.perf_counter() - started) * 1000.0 / frames
    view.hide()

    with capsys.disabled():
        print(f"\n    activity list: {model.rowCount()} rows, "
              f"{per_row_ms:.3f} ms/row painted, "
              f"{per_frame_ms:.2f} ms per 10-row viewport frame")

    assert per_row_ms < 1.0, f"{per_row_ms:.3f} ms per row"
    assert per_frame_ms < 16.7, f"{per_frame_ms:.2f} ms per frame"
    assert view.horizontalScrollBar().maximum() == 0


@pytest.mark.slow
def test_a_live_progress_update_touches_one_row(styled, qapp):
    """Live progress is one `setData`, never a model rebuild."""
    model = model_of([sample_row() for _ in range(50)])
    view = ActivityListView()
    view.setModel(model)
    view.resize(VIEW_W, ROW_H * 5)
    view.show()
    qapp.processEvents()

    changed: list[int] = []
    model.dataChanged.connect(lambda top, bottom, roles: changed.append(top.row()))
    index = model.index(3, 0)
    row = view.delegate().row_for(index)
    model.setData(index, row.with_progress(0.75), ROW_ROLE)
    assert changed == [3]
    assert view.delegate().row_for(index).progress == pytest.approx(0.75)
    view.hide()


# ═════════════════════════════════════════════════════════════════════════════
# TriStateItem
# ═════════════════════════════════════════════════════════════════════════════

def test_tri_state_item_stores_its_path_as_item_data(styled):
    item = TriStateItem(OBJ.CARD, rel_path="Documents/Q3", size_text=OBJ.FOOTER)
    assert item.rel_path() == "Documents/Q3"
    assert rel_path_of(item) == "Documents/Q3"
    assert item.size_text() == OBJ.FOOTER
    assert item.data(0, PATH_ROLE) == "Documents/Q3"
    item.set_rel_path("Pictures")
    item.set_size_text(OBJ.HEADER)
    assert (item.rel_path(), item.size_text()) == ("Pictures", OBJ.HEADER)


def test_tri_state_item_is_checkable_but_not_user_tristate(styled):
    """A user ticks or unticks; "partially" is only ever computed."""
    item = TriStateItem(OBJ.CARD)
    assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert not (item.flags() & Qt.ItemFlag.ItemIsUserTristate)
    assert not (item.flags() & Qt.ItemFlag.ItemIsAutoTristate)
    assert item.state() == Qt.CheckState.Unchecked


# ═════════════════════════════════════════════════════════════════════════════
# FolderTree propagation — the acceptance
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tree(styled) -> FolderTree:
    """A three-level tree: Documents / (a, b, c) / a-1."""
    folder = FolderTree(size_column=True)
    root = folder.add_folder(OBJ.CARD, rel_path="Documents")
    children = [folder.add_folder(f"{OBJ.CARD}-{index}", root,
                                  rel_path=f"Documents/{index}")
                for index in range(3)]
    folder.add_folder(OBJ.FOOTER, children[0], rel_path="Documents/0/deep")
    folder._children = children          # convenience for the tests
    return folder


def _states(tree: FolderTree) -> list[Qt.CheckState]:
    return [item.checkState(0) for item in tree.walk()]


def test_a_new_tree_is_fully_checked(tree):
    assert all(state == Qt.CheckState.Checked for state in _states(tree))
    assert tree.propagation_count() == 0, "building must not cost a walk"


def test_unchecking_a_child_makes_the_parent_partial(tree):
    root = tree.topLevelItem(0)
    tree._children[1].setCheckState(0, Qt.CheckState.Unchecked)
    assert root.checkState(0) == Qt.CheckState.PartiallyChecked


def test_unchecking_every_child_unchecks_the_parent(tree):
    root = tree.topLevelItem(0)
    for child in tree._children:
        child.setCheckState(0, Qt.CheckState.Unchecked)
    assert root.checkState(0) == Qt.CheckState.Unchecked


def test_checking_a_parent_pushes_all_the_way_down(tree):
    root = tree.topLevelItem(0)
    root.setCheckState(0, Qt.CheckState.Unchecked)
    assert all(state == Qt.CheckState.Unchecked for state in _states(tree))
    root.setCheckState(0, Qt.CheckState.Checked)
    assert all(state == Qt.CheckState.Checked for state in _states(tree))
    # Including the grandchild: the push is recursive.
    assert tree._children[0].child(0).checkState(0) == Qt.CheckState.Checked


def test_a_partial_state_is_never_pushed_onto_children(tree):
    """"Some of these are selected" is not a state a leaf can be in."""
    root = tree.topLevelItem(0)
    tree._children[1].setCheckState(0, Qt.CheckState.Unchecked)
    assert root.checkState(0) == Qt.CheckState.PartiallyChecked
    # The other two are untouched — a pushed partial would have flattened them.
    assert tree._children[0].checkState(0) == Qt.CheckState.Checked
    assert tree._children[2].checkState(0) == Qt.CheckState.Checked


def test_propagation_does_not_recurse(tree):
    """Acceptance: one user change costs exactly one walk.

    `setCheckState()` re-emits `itemChanged` for every node the walk touches, so
    the guard is what stops the first click from re-entering once per node. The
    evidence is the pair of counters: many changes, exactly one propagation.
    """
    before_walks = tree.propagation_count()
    before_changes = tree.change_count()
    tree._children[1].setCheckState(0, Qt.CheckState.Unchecked)
    assert tree.propagation_count() - before_walks == 1
    assert tree.change_count() - before_changes > 1, "the cascade did happen"
    assert not tree.is_updating(), "the guard is lifted again"


def test_a_deep_push_still_costs_one_walk(tree):
    root = tree.topLevelItem(0)
    before = tree.propagation_count()
    root.setCheckState(0, Qt.CheckState.Unchecked)
    assert tree.propagation_count() - before == 1


def test_folder_toggled_names_only_the_item_the_user_changed(tree):
    seen: list[tuple[str, int]] = []
    tree.folder_toggled.connect(lambda path, state: seen.append((path, state)))
    tree._children[1].setCheckState(0, Qt.CheckState.Unchecked)
    assert seen == [("Documents/1", Qt.CheckState.Unchecked.value)]


def test_selection_changed_fires_once_per_propagation(tree):
    seen: list[int] = []
    tree.selection_changed.connect(lambda: seen.append(1))
    tree._children[1].setCheckState(0, Qt.CheckState.Unchecked)
    assert seen == [1]


def test_set_state_is_silent_by_default(tree):
    seen: list[tuple[str, int]] = []
    tree.folder_toggled.connect(lambda path, state: seen.append((path, state)))
    tree.set_state(tree._children[1], Qt.CheckState.Unchecked)
    assert seen == []
    assert tree.topLevelItem(0).checkState(0) == Qt.CheckState.PartiallyChecked
    tree.set_state(tree._children[2], Qt.CheckState.Unchecked, silent=False)
    assert seen == [("Documents/2", Qt.CheckState.Unchecked.value)]


def test_set_all_settles_the_whole_tree_in_one_walk(tree):
    before = tree.propagation_count()
    tree.set_all(Qt.CheckState.Unchecked)
    assert all(state == Qt.CheckState.Unchecked for state in _states(tree))
    assert tree.propagation_count() - before == 1
    tree.set_all(Qt.CheckState.Checked)
    assert all(state == Qt.CheckState.Checked for state in _states(tree))


# ═════════════════════════════════════════════════════════════════════════════
# FolderTree read-back
# ═════════════════════════════════════════════════════════════════════════════

def test_checked_paths_are_in_tree_order(tree):
    assert tree.checked_paths() == ("Documents", "Documents/0",
                                    "Documents/0/deep", "Documents/1",
                                    "Documents/2")


def test_partial_paths_name_the_ancestors_a_filter_must_keep(tree):
    tree._children[1].setCheckState(0, Qt.CheckState.Unchecked)
    assert tree.partial_paths() == ("Documents",)


def test_top_checked_paths_are_the_minimal_subtrees(tree):
    assert tree.top_checked_paths() == ("Documents",)
    tree._children[1].setCheckState(0, Qt.CheckState.Unchecked)
    assert tree.top_checked_paths() == ("Documents/0", "Documents/2")


def test_states_maps_every_path(tree):
    states = tree.states()
    assert set(states) == {"Documents", "Documents/0", "Documents/0/deep",
                           "Documents/1", "Documents/2"}
    assert all(state == Qt.CheckState.Checked for state in states.values())


def test_walk_is_depth_first_parents_first(tree):
    paths = [rel_path_of(item) for item in tree.walk()]
    assert paths.index("Documents") < paths.index("Documents/0")
    assert paths.index("Documents/0") < paths.index("Documents/0/deep")


def test_top_level_items(tree):
    assert len(tree.top_level_items()) == 1
    assert rel_path_of(tree.top_level_items()[0]) == "Documents"


def test_add_folder_starts_unchecked_when_asked(styled):
    folder = FolderTree()
    item = folder.add_folder(OBJ.CARD, checked=Qt.CheckState.Unchecked)
    assert item.checkState(0) == Qt.CheckState.Unchecked
    assert folder.checked_paths() == ()


# ═════════════════════════════════════════════════════════════════════════════
# FolderTree presentation
# ═════════════════════════════════════════════════════════════════════════════

def test_the_tree_styles_its_own_check_indicator(styled):
    """The frozen sheet has no `QTreeView::indicator` rule, so the tree brings
    one — otherwise the box falls through to the base style and paints a filled
    square in Qt's default palette."""
    sheet = FolderTree.indicator_qss(dark=False)
    assert "QTreeView::indicator" in sheet
    assert theme.accent("rest", dark=False) in sheet
    assert theme.T("ControlStrongStrokeColorDefault", dark=False) in sheet
    assert f"width: {TREE_INDICATOR}px" in sheet
    tree = FolderTree()
    assert tree.styleSheet() == FolderTree.indicator_qss()


def test_the_tree_sheet_does_not_break_a_qss_workaround(styled):
    """A widget stylesheet is still QSS: the same rules apply to it.

    `qss.validate()` itself cannot be used on a fragment — it also insists on
    finding a focused text-field rule — so the two checks that do apply are made
    directly.
    """
    fragment = FolderTree.indicator_qss(dark=False)
    assert qss.pushbutton_rules_without_border(fragment) == ()
    assert qss.unscoped_rules(fragment) == ()
    # Every rule is a `::indicator` sub-control, so nothing cascades.
    assert all("::indicator" in selector for selector, _body in qss.rules(fragment))


def test_the_tree_rebuilds_its_sheet_for_a_new_theme(styled, monkeypatch):
    tree = FolderTree()
    light = tree.styleSheet()
    monkeypatch.setattr(theme, "_DETECTED_DARK", True, raising=False)
    theme._STYLESHEET_CACHE.clear()
    icons.clear_cache()
    tree.refresh_theme()
    assert tree.styleSheet() != light
    assert theme.accent("rest", dark=True) in tree.styleSheet()
    monkeypatch.setattr(theme, "_DETECTED_DARK", False, raising=False)
    icons.clear_cache()


def test_the_tree_rows_are_32_px(styled, tree, qapp):
    tree.resize(400, 300)
    tree.show()
    qapp.processEvents()
    hint = tree.delegate().sizeHint(QStyleOptionViewItem(),
                                    tree.model().index(0, 0))
    assert hint.height() >= TREE_ROW_H == METRICS["button_h"]
    tree.hide()


def test_the_check_glyph_survives_a_plain_int_check_state(styled, tree, qapp):
    """`data(CheckStateRole)` hands back an int, and `Qt.CheckState` is not an
    IntEnum here — the delegate must coerce or every ticked row draws a dash."""
    raw = tree.model().index(0, 0).data(Qt.ItemDataRole.CheckStateRole)
    assert raw == Qt.CheckState.Checked.value
    assert not isinstance(raw, Qt.CheckState) or raw == Qt.CheckState.Checked

    tree.resize(400, 300)
    tree.expandAll()
    tree.show()
    qapp.processEvents()
    image = QImage(tree.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    tree.viewport().render(painter, QPoint(0, 0))
    painter.end()
    found = colours_in(image)
    assert theme.accent().upper() in found, "the ticked boxes are accent-filled"
    tree.hide()


def test_the_tree_draws_a_branch_chevron(styled, tree, qapp):
    """Without one a collapsed folder shows nothing to click."""
    tree.resize(400, 300)
    tree.collapseAll()
    tree.show()
    qapp.processEvents()
    image = QImage(tree.viewport().size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    tree.viewport().render(painter, QPoint(0, 0))
    painter.end()
    # Something is painted to the LEFT of the first item's check indicator.
    band = [image.pixelColor(x, TREE_ROW_H // 2).alpha()
            for x in range(0, tree.indentation())]
    assert max(band) > 0
    tree.hide()


def test_the_tree_honours_the_reduced_motion_preference(styled, monkeypatch):
    monkeypatch.setattr(theme, "_ANIMATIONS", False, raising=False)
    assert not FolderTree().isAnimated()
    monkeypatch.setattr(theme, "_ANIMATIONS", True, raising=False)
    assert FolderTree().isAnimated()


def test_the_size_column_is_optional(styled):
    assert FolderTree().columnCount() == 1
    assert FolderTree(size_column=True).columnCount() == 2


# ═════════════════════════════════════════════════════════════════════════════
# Theme reactions
# ═════════════════════════════════════════════════════════════════════════════

def test_a_theme_change_drops_the_delegate_caches(styled):
    from onedriveui.bus import BUS

    view = ActivityListView()
    paint_row(view.delegate(), sample_row())
    assert view.delegate()._pixmaps
    BUS.theme_changed.emit(True, theme.accent("rest", dark=True))
    assert view.delegate()._pixmaps == {}


def test_the_list_module_declares_its_public_api():
    assert lists.__all__
    for name in lists.__all__:
        assert hasattr(lists, name), name
