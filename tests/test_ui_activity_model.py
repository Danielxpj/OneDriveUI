"""WP-12a — `onedriveui.ui.activity_model`.

Three things are being proved here.

  * **Order and dedupe.** Live `transferring[]` rows sort above history, the
    schema's ``dedupe_key`` collapses a persisted in-flight row onto the live one
    it duplicates, and the extra path rule survives the ``job/<id>`` renumbering
    a daemon restart causes.
  * **Cheap updates.** A 2.5 Hz progress tick must be a `dataChanged` over the
    live rows, never a model reset — a reset drops the selection and the scroll
    position of a feed the user is reading.
  * **5 000 rows.** The acceptance measurement, reported in the test output.
"""

from __future__ import annotations

import ast
import hashlib
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from onedriveui.constants import ACTIVITY_CAP_ROWS, ACTIVITY_UI_ROWS
from onedriveui.models import (
    ActivityEvent, ActivityState, ActivityVerb, FileState, IssueCode,
    TransferInfo, utcnow_iso,
)
from onedriveui.strings import (
    ACTION_LABEL, FILE_STATE_LABEL, ISSUE_TITLE, STATUS_LINE, STATUS_SUB,
    VERB_LABEL,
)
from onedriveui.ui import fonts, icons, qss
from onedriveui.ui.activity_model import (
    ICON_FOR_SUFFIX, LIVE_FILE_STATE, LIVE_VERB, ROLE_KEY, ROLE_LIVE, ROLE_PATH,
    ROLE_ROW, ROLE_SOURCE, ActivityModel, dedupe_key, icon_key_for,
    key_for_event, key_for_transfer, row_for_event, row_for_transfer,
)
from onedriveui.ui.widgets.lists import (
    ROW_H, ROW_ROLE, SUBTITLE_SEPARATOR, ActivityListView,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PY = REPO_ROOT / "onedriveui" / "ui" / "activity_model.py"
#: The flyout's fixed width, which is the width the 5 000-row list scrolls at.
VIEW_W = 360


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


def transfer(name: str = "Documents/report.docx", **kw) -> TransferInfo:
    """A `core/stats.transferring[]` row, upload by default."""
    fields = {"size": 1_000_000, "bytes": 500_000, "percentage": 50,
              "speed": 180_000.0, "speed_avg": 175_000.0, "eta": 3,
              "group": "job/1", "src_fs": "/home/u/OneDrive",
              "dst_fs": "onedrive:"}
    fields.update(kw)
    return TransferInfo(name=name, **fields)


def event(rel_path: str = "Pictures/a.jpg", **kw) -> ActivityEvent:
    """One persisted `activity` row, completed by default."""
    stamp = utcnow_iso()
    fields = {"account_id": "onedrive", "name": rel_path.rsplit("/", 1)[-1],
              "verb": ActivityVerb.UPLOADED, "state": ActivityState.DONE,
              "size": 1024, "bytes": 1024, "started_at": stamp,
              "completed_at": stamp}
    fields.update(kw)
    return ActivityEvent(rel_path=rel_path, **fields)


def resets(model: ActivityModel) -> list[str]:
    """Record model resets and dataChanged spans for one model."""
    log: list[str] = []
    model.modelAboutToBeReset.connect(lambda: log.append("reset"))
    model.rowsInserted.connect(
        lambda _p, first, last: log.append(f"insert:{first}-{last}"))
    model.rowsRemoved.connect(
        lambda _p, first, last: log.append(f"remove:{first}-{last}"))
    model.dataChanged.connect(
        lambda top, bottom, *_: log.append(f"changed:{top.row()}-{bottom.row()}"))
    return log


# ═════════════════════════════════════════════════════════════════════════════
# Identity — the schema's dedupe_key, verbatim
# ═════════════════════════════════════════════════════════════════════════════

def test_dedupe_key_is_the_schema_formula():
    """`data/schema.sql` documents `sha1(group|name|completed_at)`."""
    expected = hashlib.sha1(b"job/1|Documents/report.docx|2026-08-31T12:00:00Z",
                            usedforsecurity=False).hexdigest()
    assert dedupe_key("job/1", "Documents/report.docx",
                      "2026-08-31T12:00:00Z") == expected
    assert len(dedupe_key("", "", "")) == 40


def test_a_live_row_and_its_persisted_in_flight_twin_share_a_key():
    """Neither has completed, so the third field is empty for both — which is
    exactly what makes the duplicate collapse."""
    live = transfer("Documents/report.docx", group="job/7")
    stored = event("Documents/report.docx", job_group="job/7",
                   state=ActivityState.INFLIGHT, completed_at=None)
    assert key_for_transfer(live) == key_for_event(stored)


def test_a_completed_row_keeps_a_different_key():
    live = transfer("Documents/report.docx", group="job/7")
    done = event("Documents/report.docx", job_group="job/7")
    assert key_for_transfer(live) != key_for_event(done)


def test_key_for_event_prefers_the_stored_key():
    """The unique index in SQLite is on the stored column, so it wins."""
    stored = event(dedupe_key="deadbeef")
    assert key_for_event(stored) == "deadbeef"


def test_key_for_event_falls_back_to_the_name_when_there_is_no_rel_path():
    stored = ActivityEvent(name="loose.txt", job_group="job/2")
    assert key_for_event(stored) == dedupe_key("job/2", "loose.txt", "")


# ═════════════════════════════════════════════════════════════════════════════
# Row construction
# ═════════════════════════════════════════════════════════════════════════════

def test_icon_key_for_maps_types_onto_the_frozen_registry():
    assert icon_key_for("a/b/report.PDF") == "file_pdf"
    assert icon_key_for("holiday.jpeg") == "image"
    assert icon_key_for("song.flac") == "music"
    assert icon_key_for("archive.tar.gz") == "file_zip"
    assert icon_key_for("no-suffix") == "file"
    assert icon_key_for("Documents", is_dir=True) == "folder"


def test_every_icon_key_exists_in_the_frozen_registry():
    for key in ICON_FOR_SUFFIX.values():
        assert key in icons.GLYPHS


def test_a_live_upload_row_is_worded_from_the_frozen_tables():
    row = row_for_transfer(transfer())
    assert row.name == "report.docx"
    assert row.state is ActivityState.INFLIGHT
    assert row.file_state is LIVE_FILE_STATE[True]
    assert row.verb is LIVE_VERB[True]
    assert row.subtitle.startswith(FILE_STATE_LABEL[str(FileState.DIRTY)])
    assert SUBTITLE_SEPARATOR in row.subtitle
    assert row.progress == pytest.approx(0.5)


def test_a_live_download_row_uses_the_download_state():
    row = row_for_transfer(transfer(dst_fs="/home/u/OneDrive", src_fs="onedrive:"))
    assert row.file_state is LIVE_FILE_STATE[False]
    assert row.verb is LIVE_VERB[False]
    assert row.subtitle.startswith(FILE_STATE_LABEL[str(FileState.PARTIAL)])


def test_a_live_row_drops_the_parts_rclone_has_not_measured_yet():
    """`speed` is 0 and `eta` is null while a transfer starts up."""
    row = row_for_transfer(transfer(speed=0.0, eta=None))
    assert row.rate_text == ""
    assert row.eta_text == ""
    assert row.subtitle == FILE_STATE_LABEL[str(FileState.DIRTY)]


def test_a_live_row_of_unknown_size_falls_back_to_the_percentage():
    row = row_for_transfer(transfer(size=0, bytes=0, percentage=40))
    assert row.progress == pytest.approx(0.4)


def test_a_history_row_carries_a_relative_time_and_a_type_icon():
    row = row_for_event(event("Documents/q3.xlsx"))
    assert row.name == "q3.xlsx"
    assert row.icon_key == "file_table"
    assert row.time_text != ""
    assert VERB_LABEL[str(ActivityVerb.UPLOADED)] in row.second_line()


def test_a_history_row_never_draws_a_raw_rclone_error():
    raw = "quotaLimitReached: Insufficient Space Available"
    row = row_for_event(event(state=ActivityState.ERROR, error=raw,
                              error_kind=IssueCode.QUOTA_EXCEEDED))
    assert row.error_text == ISSUE_TITLE[IssueCode.QUOTA_EXCEEDED]
    assert raw not in row.error_text


def test_an_interrupted_row_times_off_its_start_stamp():
    """A row whose daemon vanished has no completion stamp, but it does know
    when it began — a blank second line would read as "nothing happened"."""
    row = row_for_event(event(state=ActivityState.INTERRUPTED, completed_at=None))
    assert row.time_text != ""
    assert row.file_state is FileState.UNKNOWN


# ═════════════════════════════════════════════════════════════════════════════
# Ordering, dedupe and the cap
# ═════════════════════════════════════════════════════════════════════════════

def test_live_rows_sort_above_history(qapp):
    model = ActivityModel()
    model.set_history([event("a.txt"), event("b.txt")])
    model.set_live([transfer("Documents/report.docx")])
    assert model.rowCount() == 3
    assert model.live_count() == 1
    assert model.history_count() == 2
    assert model.row_at(0).name == "report.docx"
    assert model.row_at(1).name == "a.txt"
    assert model.data(model.index(0, 0), ROLE_LIVE) is True
    assert model.data(model.index(1, 0), ROLE_LIVE) is False


def test_a_persisted_in_flight_duplicate_is_collapsed(qapp):
    model = ActivityModel()
    live = transfer("Documents/report.docx", group="job/7")
    twin = event("Documents/report.docx", job_group="job/7",
                 state=ActivityState.INFLIGHT, completed_at=None)
    model.set_live([live])
    model.set_history([twin, event("other.txt")])
    assert model.rowCount() == 2
    assert model.history_count() == 1
    assert model.row_at(1).name == "other.txt"


def test_the_path_rule_survives_a_daemon_restart(qapp):
    """`group` is renumbered when the daemon restarts, so the keys no longer
    match — the in-flight row still has to be suppressed."""
    model = ActivityModel()
    model.set_live([transfer("Documents/report.docx", group="job/9")])
    stale = event("Documents/report.docx", job_group="job/1",
                  state=ActivityState.INFLIGHT, completed_at=None)
    assert key_for_transfer(transfer("Documents/report.docx", group="job/9")) \
        != key_for_event(stale)
    model.set_history([stale])
    assert model.rowCount() == 1
    assert model.history_count() == 0


def test_history_is_deduped_against_itself(qapp):
    model = ActivityModel()
    row = event(dedupe_key="same")
    model.set_history([row, row, row])
    assert model.rowCount() == 1


def test_a_completed_row_for_a_transferring_path_still_shows(qapp):
    """The same file can legitimately have finished once and be uploading
    again; only the *in-flight* echo is a duplicate."""
    model = ActivityModel()
    model.set_live([transfer("Documents/report.docx", group="job/9")])
    model.set_history([event("Documents/report.docx")])
    assert model.rowCount() == 2


def test_the_cap_is_the_schema_cap(qapp):
    model = ActivityModel()
    assert model.cap == ACTIVITY_CAP_ROWS
    assert ActivityModel.PAGE == ACTIVITY_UI_ROWS


def test_the_cap_counts_live_rows_too(qapp):
    model = ActivityModel(cap=4)
    model.set_live([transfer("a"), transfer("b")])
    model.set_history([event(f"h{i}.txt") for i in range(10)])
    assert model.rowCount() == 4
    assert model.history_count() == 2


def test_a_cap_smaller_than_the_live_set_shows_no_history(qapp):
    model = ActivityModel(cap=1)
    model.set_live([transfer("a"), transfer("b")])
    model.set_history([event("h.txt")])
    assert model.history_count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# Roles
# ═════════════════════════════════════════════════════════════════════════════

def test_the_row_role_is_the_widget_kits_own_number():
    """Two role tables would be free to disagree; this one is re-exported."""
    assert ROLE_ROW == ROW_ROLE


def test_every_role_answers(qapp):
    model = ActivityModel()
    model.set_history([event("Documents/q3.xlsx")])
    index = model.index(0, 0)
    assert model.data(index, ROLE_ROW).name == "q3.xlsx"
    assert model.data(index, int(Qt.ItemDataRole.DisplayRole)) == "q3.xlsx"
    assert model.data(index, ROLE_PATH) == "Documents/q3.xlsx"
    assert model.data(index, ROLE_KEY) == model.keys()[0]
    assert model.data(index, ROLE_LIVE) is False
    assert isinstance(model.data(index, ROLE_SOURCE), ActivityEvent)
    assert model.data(index, int(Qt.ItemDataRole.EditRole)) is None


def test_the_tooltip_carries_the_full_path(qapp):
    """The 216 px text column middle-elides a name; the tooltip is the rest."""
    model = ActivityModel()
    model.set_history([event("Documents/Quarterly/report.docx")])
    tooltip = model.data(model.index(0, 0), int(Qt.ItemDataRole.ToolTipRole))
    assert tooltip.startswith("Documents/Quarterly/report.docx")
    assert VERB_LABEL[str(ActivityVerb.UPLOADED)] in tooltip


def test_an_invalid_index_answers_nothing(qapp):
    model = ActivityModel()
    assert model.data(QModelIndex(), ROLE_ROW) is None
    assert model.rowCount(model.index(0, 0)) == 0
    assert model.flags(QModelIndex()) == Qt.ItemFlag.NoItemFlags


def test_rows_are_selectable_and_never_editable(qapp):
    model = ActivityModel()
    model.set_history([event()])
    flags = model.flags(model.index(0, 0))
    assert flags & Qt.ItemFlag.ItemIsSelectable
    assert flags & Qt.ItemFlag.ItemIsEnabled
    assert not (flags & Qt.ItemFlag.ItemIsEditable)


def test_source_accessors_discriminate(qapp):
    model = ActivityModel()
    model.set_live([transfer()])
    model.set_history([event()])
    assert model.transfer_at(0) is not None and model.event_at(0) is None
    assert model.event_at(1) is not None and model.transfer_at(1) is None


# ═════════════════════════════════════════════════════════════════════════════
# Update economics
# ═════════════════════════════════════════════════════════════════════════════

def test_a_progress_tick_is_a_data_change_not_a_reset(qapp):
    """The same files at new percentages: the common case at 2.5 Hz."""
    model = ActivityModel()
    model.set_live([transfer(bytes=100_000)])
    model.set_history([event("h.txt")])
    log = resets(model)
    model.set_live([transfer(bytes=900_000)])
    assert log == ["changed:0-0"]
    assert model.row_at(0).progress == pytest.approx(0.9)


def test_a_changed_live_set_resets_the_model(qapp):
    """A different set of files changes the history dedupe too."""
    model = ActivityModel()
    model.set_live([transfer("a")])
    log = resets(model)
    model.set_live([transfer("a"), transfer("b")])
    assert "reset" in log


def test_appending_one_event_inserts_one_row(qapp):
    model = ActivityModel()
    model.set_live([transfer()])
    model.set_history([event("old.txt")])
    log = resets(model)
    model.append_event(event("new.txt"))
    assert log == ["insert:1-1"]
    assert model.row_at(1).name == "new.txt"
    assert model.row_at(2).name == "old.txt"


def test_appending_a_duplicate_updates_in_place(qapp):
    model = ActivityModel()
    first = event("f.txt", dedupe_key="k", bytes=10, size=100,
                  state=ActivityState.INFLIGHT, completed_at=None)
    model.set_history([first])
    log = resets(model)
    model.append_event(event("f.txt", dedupe_key="k", bytes=90, size=100,
                             state=ActivityState.INFLIGHT, completed_at=None))
    assert log == ["changed:0-0"]
    assert model.rowCount() == 1
    assert model.row_at(0).progress == pytest.approx(0.9)


def test_updating_an_unknown_event_appends_it(qapp):
    model = ActivityModel()
    model.set_history([])
    model.update_event(event("brand-new.txt"))
    assert model.rowCount() == 1


def test_an_update_never_overwrites_a_live_row(qapp):
    """The live sample is newer than any persisted echo; letting the stored row
    win would make the percentage go backwards."""
    model = ActivityModel()
    live = transfer("Documents/report.docx", group="job/7", bytes=900_000)
    model.set_live([live])
    model.update_event(event("Documents/report.docx", job_group="job/7",
                             state=ActivityState.INFLIGHT, completed_at=None,
                             bytes=1, size=1_000_000))
    assert model.rowCount() == 1
    assert model.row_at(0).progress == pytest.approx(0.9)


def test_refresh_times_drops_the_cache_and_repaints(qapp):
    model = ActivityModel()
    model.set_history([event("a.txt"), event("b.txt")])
    first = model.row_at(0)
    assert model.row_at(0) is first          # cached, not rebuilt
    log = resets(model)
    model.refresh_times()
    assert log == ["changed:0-1"]
    assert model.row_at(0) is not first


def test_refresh_times_on_an_empty_model_is_silent(qapp):
    model = ActivityModel()
    log = resets(model)
    model.refresh_times()
    assert log == []


def test_clear_empties_everything(qapp):
    model = ActivityModel()
    model.set_live([transfer()])
    model.set_history([event()])
    model.clear()
    assert model.rowCount() == 0
    assert model.keys() == ()
    assert model.index_of("nope") == -1


# ═════════════════════════════════════════════════════════════════════════════
# The bus
# ═════════════════════════════════════════════════════════════════════════════

def test_the_model_follows_the_bus_when_asked(qapp, fake_services):
    model = ActivityModel(account_id=fake_services.account.id)
    model.attach_bus()
    try:
        assert model.is_attached()
        fake_services.seed_activity(3)
        assert model.rowCount() == 3
        fake_services.supervisor.emit_activity(
            "Documents/live.docx", state=ActivityState.INFLIGHT, size=100, done=10)
        assert model.rowCount() == 4
    finally:
        model.detach_bus()
    assert not model.is_attached()


def test_attach_and_detach_are_idempotent(qapp, fake_services):
    model = ActivityModel()
    model.detach_bus()
    model.attach_bus()
    model.attach_bus()
    try:
        fake_services.seed_activity(2)
        assert model.rowCount() == 2
    finally:
        model.detach_bus()
        model.detach_bus()
    fake_services.seed_activity(2)
    assert model.rowCount() == 2                # detached: no further growth


def test_another_accounts_rows_are_ignored(qapp):
    model = ActivityModel(account_id="mine")
    model.append_event(event(account_id="theirs"))
    assert model.rowCount() == 0
    model.append_event(event(account_id="mine"))
    assert model.rowCount() == 1


def test_an_unfiltered_model_accepts_every_account(qapp):
    model = ActivityModel()
    model.append_event(event(account_id="theirs"))
    assert model.rowCount() == 1


def test_transfers_updated_drives_the_live_block(qapp):
    from onedriveui.bus import BUS

    model = ActivityModel()
    model.attach_bus()
    try:
        BUS.transfers_updated.emit([transfer("a"), transfer("b"), "not a transfer"])
        assert model.live_count() == 2
    finally:
        model.detach_bus()


# ═════════════════════════════════════════════════════════════════════════════
# No literal escapes into the model either
# ═════════════════════════════════════════════════════════════════════════════

def _string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def test_the_model_holds_no_user_facing_wording():
    """Every word a row draws comes from `strings.py`, through `ActivityRow`."""
    tables = (list(STATUS_LINE.values()) + list(STATUS_SUB.values())
              + list(VERB_LABEL.values()) + list(FILE_STATE_LABEL.values())
              + list(ISSUE_TITLE.values()) + list(ACTION_LABEL.values()))
    wording = {value for value in tables if len(value) >= 6}
    source = MODEL_PY.read_text(encoding="utf-8")
    for constant in _string_constants(MODEL_PY):
        for phrase in wording:
            assert phrase not in constant, f"{phrase!r} is quoted in the model"
    for phrase in wording:
        assert phrase not in source, f"{phrase!r} appears in the model source"


def test_the_model_sources_its_wording_from_strings():
    """The positive half: it really does read the frozen tables."""
    source = MODEL_PY.read_text(encoding="utf-8")
    assert "from onedriveui.strings import FILE_STATE_LABEL" in source


# ═════════════════════════════════════════════════════════════════════════════
# 5 000 rows — the performance acceptance
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.slow
def test_five_thousand_rows_scroll_smoothly(styled, qapp, capsys):
    """Acceptance: 5 000 rows, measured and reported, with no h-scrollbar.

    Three numbers, because three different things could regress: how long the
    model takes to accept the rows, how long one row costs the delegate, and how
    long a viewport frame takes while scrolling. The budgets are generous — a
    whole millisecond per row is about thirty times what a 360 x 620 flyout needs
    at 60 Hz — so this fails on a real regression rather than on a busy machine.
    """
    stamp = utcnow_iso()
    rows = [ActivityEvent(id=index, account_id="onedrive",
                          rel_path=f"Documents/folder-{index % 40}/file-{index}.docx",
                          name=f"file-{index}.docx",
                          verb=ActivityVerb.UPLOADED,
                          state=ActivityState.DONE,
                          size=1024 * (index + 1), bytes=1024 * (index + 1),
                          started_at=stamp, completed_at=stamp,
                          job_group=f"job/{index}")
            for index in range(ACTIVITY_CAP_ROWS)]

    model = ActivityModel()
    started = time.perf_counter()
    model.set_history(rows)
    load_ms = (time.perf_counter() - started) * 1000.0
    assert model.rowCount() == ACTIVITY_CAP_ROWS

    view = ActivityListView()
    view.setModel(model)
    view.resize(VIEW_W, ROW_H * 10)
    view.show()
    qapp.processEvents()

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

    bar = view.verticalScrollBar()
    frames = 60
    started = time.perf_counter()
    for frame in range(frames):
        bar.setValue(int(bar.maximum() * frame / frames))
        view.viewport().repaint()
    per_frame_ms = (time.perf_counter() - started) * 1000.0 / frames
    horizontal = view.horizontalScrollBar()
    view.hide()

    with capsys.disabled():
        print(f"\n    activity model: {model.rowCount()} rows loaded in "
              f"{load_ms:.1f} ms, {per_row_ms:.3f} ms/row painted, "
              f"{per_frame_ms:.2f} ms per 10-row viewport frame "
              f"({1000.0 / max(per_frame_ms, 1e-6):.0f} fps)")

    assert load_ms < 250.0, f"{load_ms:.1f} ms to accept 5 000 rows"
    assert per_row_ms < 1.0, f"{per_row_ms:.3f} ms per row"
    assert per_frame_ms < 16.7, f"{per_frame_ms:.2f} ms per frame"
    assert horizontal.maximum() == 0
    assert not horizontal.isVisible()


@pytest.mark.slow
def test_five_thousand_rows_build_only_what_is_visible(qapp):
    """The payload cache is lazy: loading the feed must not format 5 000
    relative timestamps for rows nobody has scrolled to."""
    stamp = utcnow_iso()
    model = ActivityModel()
    model.set_history([event(f"f{index}.txt", dedupe_key=f"k{index}",
                             started_at=stamp, completed_at=stamp)
                       for index in range(ACTIVITY_CAP_ROWS)])
    assert model._cache.count(None) == ACTIVITY_CAP_ROWS      # noqa: SLF001
    for row in range(20):
        model.row_at(row)
    assert model._cache.count(None) == ACTIVITY_CAP_ROWS - 20  # noqa: SLF001
