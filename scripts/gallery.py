#!/usr/bin/env python3
"""Render every widget in the Fluent kit to a PNG contact sheet, per theme.

This is how a human checks the Fluent fidelity of WP-11 without running the
application: it needs no rclone, no daemon, no D-Bus, no network and no display.
Every widget is constructed from `onedriveui.ui` and the frozen WP-00 contracts
and nothing else, which is the property that keeps the widget kit renderable in
isolation — if this script ever needs an engine import, the kit has grown a
dependency it must not have.

Usage::

    python3 scripts/gallery.py                    # writes docs/gallery-{light,dark}.png
    python3 scripts/gallery.py --out /tmp/sheets  # somewhere else
    python3 scripts/gallery.py --theme dark       # one theme only
    python3 scripts/gallery.py --dpr 2.0          # render at 2x

Every user-facing label below comes from `onedriveui.strings`, exactly as the
real windows will take theirs. The only literals in this file are the gallery's
own scaffolding — the section captions naming which widgets a block contains —
which are developer chrome and never ship inside a window.

The sheet shows the two-tone focus ring three times over — on a `FluentButton`,
on a `ToggleSwitch` and on a clickable `SettingsCard` — because that ring is the
single easiest thing in the kit to get wrong. `FocusRingStyle` is handed
`SE_PushButtonFocusRect` for a push button (the CONTENT box, inside the padding)
and has to substitute the widget's own bounds; the toggle reserves
`ToggleSwitch.FOCUS_PAD` around its track because Qt clips a paint event to the
widget and an un-padded ring survives only as four corner slivers. Both are
regressions worth seeing rather than trusting.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Offscreen BEFORE PySide6 is imported anywhere, so the gallery never needs a
# compositor and behaves identically in CI and on a developer's desktop.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt                             # noqa: E402
from PySide6.QtGui import (                                       # noqa: E402
    QColor, QImage, QPainter, QPixmap, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (                                   # noqa: E402
    QApplication, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:                               # noqa: E402
    sys.path.insert(0, str(_REPO_ROOT))

from onedriveui.models import (                                   # noqa: E402
    ActivityState, ActivityVerb, FileState, SyncState, ThemeMode,
)
from onedriveui.strings import S, status_line                     # noqa: E402
from onedriveui.ui import fonts, icons, qss, theme                # noqa: E402
from onedriveui.ui.theme import OBJ, PROP, SPACING                # noqa: E402
from onedriveui.ui.widgets import containers, controls            # noqa: E402
from onedriveui.ui.widgets.containers import (                    # noqa: E402
    ContentDialog, InfoBar, InfoBarSeverity, SectionHeading, SettingsCard,
    SettingsExpander,
)
from onedriveui.ui.widgets.chrome import (                        # noqa: E402
    NavigationView, SearchBox, StatusGlyph,
)
from onedriveui.ui.widgets.controls import (                      # noqa: E402
    ButtonVariant, FluentButton, FluentCheckBox, FluentComboBox, FluentLineEdit,
    FluentRadioButton, ToggleSwitch, icon_button,
)
from onedriveui.ui.widgets.indicators import (                    # noqa: E402
    Avatar, FluentProgressBar, ProgressRing, ProgressTone, StatusBadge,
    StorageBar,
)
from onedriveui.ui.widgets.lists import (                         # noqa: E402
    ROW_ROLE, ActivityListView, ActivityRow, FolderTree,
)

# ═════════════════════════════════════════════════════════════════════════════
# The gallery's own scaffolding. These captions never appear in the product;
# every string a WIDGET is given below comes from `onedriveui.strings`.
# ═════════════════════════════════════════════════════════════════════════════

CAPTION_TYPE = "Type ramp"
CAPTION_BUTTONS = "Buttons"
CAPTION_FIELDS = "Text fields"
CAPTION_CHOICE = "Choice controls"
CAPTION_PROGRESS = "Progress"
CAPTION_IDENTITY = "Identity and storage"
CAPTION_BADGES = "File status badges"
CAPTION_STATUS = "Status glyphs"
CAPTION_CARDS = "Settings cards"
CAPTION_EXPANDER = "Settings expander"
CAPTION_INFOBARS = "Info bars"
CAPTION_ACTIVITY = "Activity list"
CAPTION_TREE = "Selective sync tree"
CAPTION_NAV = "Navigation"
CAPTION_DIALOG = "Content dialog"

#: The sheet's own tile captions and header.
TILE_PAGE = "widget kit"
TILE_DIALOG = "ContentDialog + reserved shadow margin"
TILE_FOCUS = "focus ring, one control at a time"
TITLE_LIGHT = "light theme"
TITLE_DARK = "dark theme"
#: Joins the header's three fields. Punctuation only, never copy.
SHEET_JOIN = "   ·   "

#: The type-ramp roles, largest first, shown as their own names.
RAMP_ROLES: tuple[str, ...] = ("subtitle", "body_large", "body_strong", "body",
                               "caption")

#: The sync states worth a status glyph on the sheet — one per tray semantic.
STATUS_STATES: tuple[SyncState, ...] = (
    SyncState.UP_TO_DATE, SyncState.SYNCING, SyncState.PAUSED_MANUAL,
    SyncState.WARNING, SyncState.ERROR, SyncState.SIGNED_OUT,
    SyncState.ACCOUNT_BLOCKED, SyncState.INFO_NOTICE, SyncState.OFFLINE,
)

#: The file states worth a badge. UNKNOWN correctly paints nothing.
BADGE_STATES: tuple[FileState, ...] = (
    FileState.ONLINE_ONLY, FileState.PARTIAL, FileState.LOCAL,
    FileState.PINNED, FileState.DIRTY, FileState.SYNCING,
    FileState.EXCLUDED, FileState.ERROR,
)

#: Sizes the sheet renders lists and rails at.
ACTIVITY_W = theme.METRICS["ac_width"]
ACTIVITY_ROWS = 5
TREE_H = 200
NAV_H = 220
COLUMN_GAP = SPACING["xxl"]
SHEET_GUTTER = SPACING["xxl"]


# ═════════════════════════════════════════════════════════════════════════════
# Small builders
# ═════════════════════════════════════════════════════════════════════════════

def _column(parent: QWidget) -> tuple[QWidget, QVBoxLayout]:
    """One vertical column of the page."""
    box = QWidget(parent)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACING["s"])
    return box, layout


def _row(parent: QWidget, *widgets: QWidget) -> QWidget:
    """A horizontal strip of widgets, left-aligned."""
    box = QWidget(parent)
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACING["s"])
    for widget in widgets:
        widget.setParent(box)
        layout.addWidget(widget)
    layout.addStretch(1)
    return box


def _caption(text: str, parent: QWidget) -> SectionHeading:
    return SectionHeading(text, parent)


# ═════════════════════════════════════════════════════════════════════════════
# Sections
# ═════════════════════════════════════════════════════════════════════════════

def _type_ramp(parent: QWidget) -> QWidget:
    """One label per ramp role, drawn through the sheet's type rules."""
    box, layout = _column(parent)
    for role in RAMP_ROLES:
        label = QLabel(role, box)
        qss.set_property(label, PROP.TYPE, role)
        label.setFixedHeight(fonts.line_height(role))
        layout.addWidget(label)
    secondary = QLabel(S.SETTINGS.BACKUP_DESC, box)
    qss.set_property(secondary, PROP.TYPE, "caption")
    qss.set_property(secondary, PROP.ROLE, "secondary")
    layout.addWidget(secondary)
    return box


def _buttons(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    labels = {
        ButtonVariant.STANDARD: S.DIALOG.CANCEL,
        ButtonVariant.ACCENT: S.DIALOG.CONTINUE,
        ButtonVariant.SUBTLE: S.DIALOG.CLOSE,
        ButtonVariant.HYPERLINK: S.SETTINGS.GET_MORE_STORAGE,
    }
    for variant, label in labels.items():
        layout.addWidget(_row(box, FluentButton(label, variant=variant)))
    disabled = FluentButton(S.DIALOG.SAVE)
    disabled.setEnabled(False)
    layout.addWidget(_row(box, disabled))
    layout.addWidget(_row(
        box,
        icon_button("settings", tooltip=S.MENU.SETTINGS),
        icon_button("pause", tooltip=S.MENU.PAUSE),
        icon_button("refresh", tooltip=S.ACTION_LABEL[list(S.ACTION_LABEL)[0]]),
        icon_button("close", tooltip=S.DIALOG.CLOSE),
    ))
    return box


def _fields(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    layout.addWidget(FluentLineEdit(box, placeholder=S.SETTINGS.LIMIT_TO))
    search = SearchBox(box, placeholder=S.SETTINGS.CHOOSE_FOLDERS,
                       clear_tooltip=S.DIALOG.CLOSE)
    search.setText(S.SETTINGS.NAV_ACCOUNT)
    layout.addWidget(search)
    read_only = FluentLineEdit(box)
    read_only.setText(S.SETTINGS.KB_PER_SEC)
    read_only.setReadOnly(True)
    layout.addWidget(read_only)
    disabled = FluentLineEdit(box, placeholder=S.SETTINGS.EXCLUDED_EXT)
    disabled.setEnabled(False)
    layout.addWidget(disabled)
    return box


def _choice(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    checked = FluentCheckBox(S.SETTINGS.N_PAUSED, box)
    checked.setChecked(True)
    mixed = FluentCheckBox(S.SETTINGS.N_SHARED, box)
    mixed.setTristate(True)
    mixed.setCheckState(Qt.CheckState.PartiallyChecked)
    unchecked = FluentCheckBox(S.SETTINGS.N_MASS_DELETE, box)
    for widget in (checked, mixed, unchecked):
        layout.addWidget(widget)

    ask = FluentRadioButton(S.SETTINGS.COLLAB_ASK, box)
    ask.setChecked(True)
    keep = FluentRadioButton(S.SETTINGS.COLLAB_KEEP_BOTH, box)
    layout.addWidget(ask)
    layout.addWidget(keep)

    combo = FluentComboBox(box)
    combo.addItems([S.MENU.PAUSE_2H, S.MENU.PAUSE_8H, S.MENU.PAUSE_24H,
                    S.MENU.PAUSE_UNTIL])
    layout.addWidget(combo)

    on = ToggleSwitch(box)
    on.set_checked_silently(True)
    off = ToggleSwitch(box)
    disabled = ToggleSwitch(box)
    disabled.set_checked_silently(True)
    disabled.setEnabled(False)
    layout.addWidget(_row(box, on, off, disabled))
    return box


def _progress(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    ring = ProgressRing(box, track=True)
    ring.set_value(0.35)
    spinning = ProgressRing(box)
    spinning.set_indeterminate(True)
    paused_ring = ProgressRing(box, track=True)
    paused_ring.set_value(0.6)
    paused_ring.set_tone(ProgressTone.PAUSED)
    error_ring = ProgressRing(box, track=True)
    error_ring.set_value(0.8)
    error_ring.set_tone(ProgressTone.ERROR)
    layout.addWidget(_row(box, ring, spinning, paused_ring, error_ring))

    for value, tone in ((0.65, ProgressTone.NORMAL),
                        (0.4, ProgressTone.PAUSED),
                        (0.25, ProgressTone.ERROR)):
        bar = FluentProgressBar(box, tone=tone)
        bar.set_value(value)
        layout.addWidget(bar)
    indeterminate = FluentProgressBar(box)
    indeterminate.set_indeterminate(True)
    layout.addWidget(indeterminate)
    return box


def _identity(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    named = Avatar(box)
    named.set_person(S.SETTINGS.NAV_ACCOUNT)
    anonymous = Avatar(box)
    anonymous.set_person("")
    large = Avatar(box, diameter=SPACING["xxxl"] + SPACING["l"])
    large.set_person(S.OOBE.WELCOME_TITLE)
    layout.addWidget(_row(box, named, anonymous, large))

    total = 5 * 1000 ** 3
    for used in (int(total * 0.42), int(total * 0.93), total):
        bar = StorageBar(box)
        bar.set_usage(used, total)
        layout.addWidget(bar)
    segmented = StorageBar(box)
    segmented.set_segments(total, (
        (int(total * 0.30), theme.accent()),
        (int(total * 0.12), theme.T("SystemFillColorSuccess")),
        (int(total * 0.08), theme.T("SystemFillColorCaution")),
    ))
    layout.addWidget(segmented)
    return box


def _badge_caption_width() -> int:
    """The narrowest column width that never clips a badge caption.

    `QLabel.setWordWrap(True)` breaks between WORDS and never inside one, so a
    column narrower than the longest single word does not wrap — it clips, and
    "Downloading" renders as "Downloadi" with no ellipsis. Measure the widest
    word in the real strings and let the column be at least that.
    """
    metrics = fonts.metrics("caption")
    widest = 0.0
    for state in BADGE_STATES:
        for word in S.FILE_STATE_LABEL[str(state)].split():
            widest = max(widest, metrics.horizontalAdvance(word))
    floor = theme.METRICS["nav_icon_box"] + SPACING["xl"]
    return max(floor, int(widest) + SPACING["xs"])


def _badges(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    strip = QWidget(box)
    grid = QGridLayout(strip)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(SPACING["s"])
    grid.setVerticalSpacing(SPACING["xs"])
    caption_w = _badge_caption_width()
    for column, state in enumerate(BADGE_STATES):
        badge = StatusBadge(strip, state=state, size=SPACING["xl"])
        grid.addWidget(badge, 0, column)
        label = QLabel(S.FILE_STATE_LABEL[str(state)], strip)
        qss.set_property(label, PROP.TYPE, "caption")
        qss.set_property(label, PROP.ROLE, "secondary")
        label.setWordWrap(True)
        label.setFixedWidth(caption_w)
        grid.addWidget(label, 1, column, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(strip)
    return box


def _status(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    for state in STATUS_STATES:
        line = QWidget(box)
        row = QHBoxLayout(line)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACING["s"])
        row.addWidget(StatusGlyph(line, state=state))
        label = QLabel(status_line(state, n=12), line)
        row.addWidget(label)
        row.addStretch(1)
        layout.addWidget(line)
    return box


def _cards(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    layout.setSpacing(containers.CARD_GROUP_GAP)

    toggle = ToggleSwitch()
    toggle.set_checked_silently(True)
    layout.addWidget(SettingsCard(
        S.SETTINGS.START_AT_SIGNIN, box, icon_key="autostart", content=toggle))

    metered = SettingsCard(S.SETTINGS.PAUSE_METERED, box,
                           description=S.STATUS_SUB[SyncState.PAUSED_METERED],
                           icon_key="metered", content=ToggleSwitch())
    layout.addWidget(metered)

    clickable = SettingsCard(S.SETTINGS.CHOOSE_FOLDERS, box,
                             description=S.DIALOG.CHOOSE_FOLDERS_WARN,
                             icon_key="choose_folders", clickable=True)
    layout.addWidget(clickable)
    # The sheet's focus demonstration. A clickable card paints the two-tone
    # ring itself, inset so the whole ring lands inside its own bounds.
    box.focus_demo = clickable

    with_button = SettingsCard(S.SETTINGS.UNLINK, box,
                               description=S.DIALOG.UNLINK_BODY,
                               icon_key="unlink",
                               content=FluentButton(S.SETTINGS.UNLINK))
    layout.addWidget(with_button)

    disabled = SettingsCard(S.SETTINGS.FOD, box, description=S.SETTINGS.FOD_DESC,
                            icon_key="cloud", content=ToggleSwitch())
    disabled.setEnabled(False)
    layout.addWidget(disabled)
    return box


def _expander(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    expander = SettingsExpander(S.SETTINGS.ADVANCED, box,
                                description=S.SETTINGS.BANDWIDTH_GLOBAL_NOTE,
                                icon_key="advanced")
    expander.add_row(SettingsCard(
        S.SETTINGS.LIMIT_DOWNLOAD, content=ToggleSwitch(), action_icon=False))
    expander.add_row(SettingsCard(
        S.SETTINGS.LIMIT_UPLOAD, content=ToggleSwitch(), action_icon=False))
    rate = FluentComboBox()
    rate.addItems([S.SETTINGS.ADJUST_AUTO, S.SETTINGS.LIMIT_TO])
    expander.add_row(SettingsCard(
        S.SETTINGS.ADJUST_AUTO, description=S.SETTINGS.KB_PER_SEC,
        content=rate, action_icon=False))
    expander.set_expanded(True, animate=False)
    layout.addWidget(expander)

    closed = SettingsExpander(S.SETTINGS.EXCLUDED_EXT, box,
                              description=S.SETTINGS.FILE_COLLAB,
                              icon_key="excluded")
    closed.add_row(FluentLineEdit(placeholder=S.SETTINGS.EXCLUDED_EXT))
    layout.addWidget(closed)
    return box


def _infobars(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    blocks = (
        (InfoBarSeverity.INFORMATIONAL, S.STATUS_LINE[SyncState.INFO_NOTICE],
         S.FIRST_SYNC_BANNER, ""),
        (InfoBarSeverity.SUCCESS, S.STATUS_LINE[SyncState.UP_TO_DATE],
         S.SETTINGS.BACKUP_DESC, ""),
        (InfoBarSeverity.WARNING, S.STATUS_LINE[SyncState.PAUSED_METERED],
         S.STATUS_SUB[SyncState.PAUSED_METERED], S.MENU.RESUME),
        (InfoBarSeverity.ERROR, S.STATUS_LINE[SyncState.PAUSED_QUOTA],
         S.STATUS_SUB[SyncState.PAUSED_QUOTA], S.SETTINGS.GET_MORE_STORAGE),
    )
    for severity, title, message, action in blocks:
        bar = InfoBar(title, message, box, severity=severity,
                      close_tooltip=S.DIALOG.CLOSE)
        if action:
            bar.add_action(action, accent=severity is InfoBarSeverity.ERROR)
        layout.addWidget(bar)
    return box


def _activity(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    view = ActivityListView(box)
    view.setFixedSize(ACTIVITY_W, view.delegate().row_height() * ACTIVITY_ROWS)
    model = QStandardItemModel(view)
    rows = (
        ActivityRow(
            name=S.DIALOG.WHERE_ARE_MY_FILES, verb=ActivityVerb.UPLOADED,
            state=ActivityState.INFLIGHT, file_state=FileState.SYNCING,
            time_text=S.MENU.PAUSE_2H, rate_text=S.SETTINGS.KB_PER_SEC,
            eta_text=S.MENU.PAUSE_8H, progress=0.42, icon_key="file_table",
        ),
        ActivityRow(
            name=S.SETTINGS.MANAGE_BACKUP, verb=ActivityVerb.DOWNLOADED,
            state=ActivityState.DONE, file_state=FileState.LOCAL,
            time_text=S.MENU.PAUSE_24H, icon_key="file_pdf",
        ),
        ActivityRow(
            name=S.SETTINGS.NAV_ABOUT, verb=ActivityVerb.MODIFIED,
            state=ActivityState.ERROR, file_state=FileState.ERROR,
            time_text=S.MENU.PAUSE_UNTIL,
            error_text=S.ISSUE_TITLE[list(S.ISSUE_TITLE)[0]],
            icon_key="file_text",
        ),
        ActivityRow(
            name=S.SETTINGS.CHOOSE_FOLDERS, verb=ActivityVerb.PINNED,
            state=ActivityState.DONE, file_state=FileState.PINNED,
            is_dir=True, time_text=S.MENU.PAUSE_2H,
        ),
        ActivityRow(
            name=S.SETTINGS.FREE_UP_SPACE, verb=ActivityVerb.FREED,
            state=ActivityState.DONE, file_state=FileState.ONLINE_ONLY,
            time_text=S.MENU.PAUSE_8H, is_dir=False,
        ),
    )
    for row in rows:
        item = QStandardItem()
        item.setData(row, ROW_ROLE)
        model.appendRow(item)
    view.setModel(model)
    view.setCurrentIndex(model.index(0, 0))
    layout.addWidget(view)
    return box


def _gb(nbytes: int) -> str:
    """Format a folder size for the demo tree.

    Deliberately local: the gallery may import only ``onedriveui.ui`` and the
    WP-00 contracts, so it cannot reach for ``onedriveui.units`` (WP-01).
    """
    return f"{nbytes / 1_000_000_000:.1f} GB"


def _tree(parent: QWidget) -> QWidget:
    box, layout = _column(parent)
    tree = FolderTree(box, size_column=True)
    tree.setFixedHeight(TREE_H)
    # Sizes are computed, never literal, so the no-hardcoded-strings test still holds.
    root = tree.add_folder(S.SETTINGS.NAV_SYNC, rel_path=S.SETTINGS.NAV_SYNC,
                           size_text=_gb(12_400_000_000))
    for name, nbytes in ((S.SETTINGS.NAV_ACCOUNT, 8_100_000_000),
                         (S.SETTINGS.NAV_NOTIFICATIONS, 3_650_000_000),
                         (S.SETTINGS.NAV_ABOUT, 642_000_000)):
        tree.add_folder(name, root, rel_path=name, size_text=_gb(nbytes))
    tree.add_folder(S.SETTINGS.EXCLUDED_EXT, root.child(0),
                    rel_path=S.SETTINGS.EXCLUDED_EXT)
    tree.set_state(root.child(1), Qt.CheckState.Unchecked)
    # Expand the root but leave the first child shut, so the sheet shows both
    # branch chevrons.
    tree.expandItem(root)
    layout.addWidget(tree)
    return box


def _navigation_pane(parent: QWidget, *, compact: bool) -> NavigationView:
    nav = NavigationView(parent, compact=compact,
                         toggle_tooltip=S.MENU.SETTINGS)
    for label, key in ((S.SETTINGS.NAV_SYNC, "nav_sync"),
                       (S.SETTINGS.NAV_ACCOUNT, "nav_account"),
                       (S.SETTINGS.NAV_NOTIFICATIONS, "nav_notifications"),
                       (S.SETTINGS.NAV_ABOUT, "nav_about")):
        nav.add_item(label, key)
    nav.set_toggle_visible(True)
    nav.setFixedHeight(NAV_H)
    return nav


def _nav_section(parent: QWidget) -> QWidget:
    box = QWidget(parent)
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(SPACING["l"])
    row.addWidget(_navigation_pane(box, compact=False))
    row.addWidget(_navigation_pane(box, compact=True))
    row.addStretch(1)
    return box


# ═════════════════════════════════════════════════════════════════════════════
# The page
# ═════════════════════════════════════════════════════════════════════════════

def build_page(parent: QWidget | None = None) -> QWidget:
    """Every widget in the kit, laid out in three columns.

    Args:
        parent: Optional parent; the page is normally a top-level.

    Returns:
        A styled root widget sized to its contents.
    """
    page = QWidget(parent)
    page.setObjectName(OBJ.ROOT)
    page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    outer = QHBoxLayout(page)
    outer.setContentsMargins(*(SPACING["xxl"],) * 4)
    outer.setSpacing(COLUMN_GAP)

    left, left_layout = _column(page)
    for caption, factory in ((CAPTION_TYPE, _type_ramp),
                             (CAPTION_BUTTONS, _buttons),
                             (CAPTION_FIELDS, _fields),
                             (CAPTION_CHOICE, _choice)):
        left_layout.addWidget(_caption(caption, left))
        left_layout.addWidget(factory(left))
    left_layout.addStretch(1)
    outer.addWidget(left)

    middle, middle_layout = _column(page)
    for caption, factory in ((CAPTION_PROGRESS, _progress),
                             (CAPTION_IDENTITY, _identity),
                             (CAPTION_BADGES, _badges),
                             (CAPTION_STATUS, _status),
                             (CAPTION_NAV, _nav_section)):
        middle_layout.addWidget(_caption(caption, middle))
        middle_layout.addWidget(factory(middle))
    middle_layout.addStretch(1)
    outer.addWidget(middle)

    right, right_layout = _column(page)
    for caption, factory in ((CAPTION_CARDS, _cards),
                             (CAPTION_EXPANDER, _expander),
                             (CAPTION_INFOBARS, _infobars),
                             (CAPTION_ACTIVITY, _activity),
                             (CAPTION_TREE, _tree)):
        right_layout.addWidget(_caption(caption, right))
        block = factory(right)
        right_layout.addWidget(block)
        demo = getattr(block, "focus_demo", None)
        if demo is not None:
            page.focus_demo = demo
    right_layout.addStretch(1)
    outer.addWidget(right, 1)

    page.adjustSize()
    return page


def build_dialog() -> ContentDialog:
    """The modal dialog, shown so its reserved shadow margin is visible."""
    dialog = ContentDialog(title=S.DIALOG.MASS_DELETE_TITLE.format(n=214),
                           body=S.DIALOG.MASS_DELETE_BODY.format(n=214))
    dialog.set_buttons(S.DIALOG.MASS_DELETE_NO, S.DIALOG.MASS_DELETE_YES,
                       S.DIALOG.CANCEL)
    note = QLabel(S.DIALOG.MASS_DELETE_TIMEOUT)
    qss.set_property(note, PROP.TYPE, "caption")
    qss.set_property(note, PROP.ROLE, "secondary")
    note.setWordWrap(True)
    dialog.set_content(note)
    return dialog


# ═════════════════════════════════════════════════════════════════════════════
# Rendering
# ═════════════════════════════════════════════════════════════════════════════

#: The live ThemeManager, kept alive for as long as the sheet is being rendered.
#: `theme.current_dark()` asks whichever manager last called `start()`, so this
#: reference is what makes a widget's `theme.T()` resolve to the pinned theme.
_MANAGER: theme.ThemeManager | None = None


def apply_theme(app: QApplication, *, dark: bool) -> theme.ThemeManager:
    """Pin the live theme and re-apply the sheet.

    `ThemeManager.apply()` is deliberately NOT used: it calls
    `app.setStyle("Fusion")`, which would replace the `FocusRingStyle` proxy
    installed at startup and silently throw the two-tone focus ring away.

    Args:
        app: The application to restyle.
        dark: Which theme to pin.

    Returns:
        The manager now answering `theme.current_dark()`.
    """
    global _MANAGER
    if _MANAGER is not None:
        _MANAGER.stop()
    manager = theme.ThemeManager()
    manager.set_mode(ThemeMode.DARK if dark else ThemeMode.LIGHT)
    manager.start()
    _MANAGER = manager
    theme.invalidate_detection()
    qss.invalidate()
    icons.clear_cache()
    qss.apply(app, dark=dark)
    return manager


def render_widget(widget: QWidget, *, dpr: float = 1.0) -> QImage:
    """Render a widget offscreen at `dpr` into an ARGB image.

    Focus is moved deliberately. Qt hands the keyboard to the first focusable
    widget when a window is shown, which on this page is a button — and a
    focused `QPushButton` is not where the focus ring reads correctly (see the
    note in the module docstring), so the sheet would advertise an artefact
    instead of a state. If the page named a `focus_demo`, that widget gets the
    focus instead; otherwise nothing does.
    """
    widget.ensurePolished()
    widget.show()
    QApplication.processEvents()
    focused = QApplication.focusWidget()
    if focused is not None:
        focused.clearFocus()
    demo = getattr(widget, "focus_demo", None)
    if demo is not None:
        demo.setFocus(Qt.FocusReason.OtherFocusReason)
    QApplication.processEvents()
    size = widget.size()
    image = QImage(int(round(size.width() * dpr)), int(round(size.height() * dpr)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    widget.hide()
    return image


def build_focus_page(widget: QWidget) -> QWidget:
    """A one-control page whose only widget is the focus demonstration.

    Only one widget per window can hold the keyboard, so the focus strip is
    rendered a page at a time rather than as one tile with four focused
    controls — which Qt cannot produce and a hand-painted ring would only
    pretend to.
    """
    page = QWidget()
    page.setObjectName(OBJ.ROOT)
    page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    layout = QVBoxLayout(page)
    layout.setContentsMargins(SPACING["l"], SPACING["l"], SPACING["l"], SPACING["l"])
    layout.addWidget(widget)
    page.focus_demo = widget
    page.adjustSize()
    return page


def render_focus_strip(*, dpr: float = 1.0) -> QImage:
    """Every focusable control, focused, stacked into one tile.

    The ring is the easiest thing in the kit to get wrong — it is drawn by a
    proxy style at a rect Qt chooses, and for a push button that rect is the
    label's box, not the button's. Rendering it is the only way to see that.
    """
    card = SettingsCard(S.SETTINGS.CHOOSE_FOLDERS,
                        description=S.DIALOG.CHOOSE_FOLDERS_WARN,
                        icon_key="choose_folders", clickable=True)
    demos: list[QWidget] = [
        FluentButton(S.DIALOG.CANCEL),
        ToggleSwitch(),
        FluentCheckBox(S.SETTINGS.PAUSE_METERED),
        card,
    ]
    images: list[QImage] = []
    pages: list[QWidget] = []
    for demo in demos:
        page = build_focus_page(demo)
        pages.append(page)
        images.append(render_widget(page, dpr=dpr))

    width = max(image.width() for image in images)
    height = sum(image.height() for image in images)
    strip = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    strip.fill(Qt.GlobalColor.transparent)
    painter = QPainter(strip)
    y = 0
    for image in images:
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(1.0)
        painter.drawPixmap(0, y, pixmap)
        y += image.height()
    painter.end()
    for page in pages:
        page.deleteLater()
    return strip


def compose(tiles: list[tuple[str, QImage]], *, dark: bool,
            title: str = "") -> QImage:
    """Lay the rendered tiles out side by side with captions."""
    caption_h = SPACING["xxl"]
    header_h = SPACING["xxxl"] if title else 0
    width = (sum(tile.width() for _label, tile in tiles)
             + SHEET_GUTTER * (len(tiles) + 1))
    height = (max(tile.height() for _label, tile in tiles)
              + SHEET_GUTTER * 2 + caption_h + header_h)

    sheet = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor(theme.T("SolidBackgroundFillColorSecondary", dark=dark)))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    text_colour = QColor(theme.T("TextFillColorPrimary", dark=dark))
    if title:
        painter.setPen(text_colour)
        painter.setFont(fonts.font("subtitle"))
        painter.drawText(SHEET_GUTTER, header_h, title)
    painter.setFont(fonts.font("caption"))
    painter.setPen(QColor(theme.T("TextFillColorSecondary", dark=dark)))
    x = SHEET_GUTTER
    for label, tile in tiles:
        painter.drawText(x, header_h + SHEET_GUTTER + SPACING["m"], label)
        pixmap = QPixmap.fromImage(tile)
        pixmap.setDevicePixelRatio(1.0)
        painter.drawPixmap(x, header_h + SHEET_GUTTER + caption_h, pixmap)
        x += tile.width() + SHEET_GUTTER
    painter.end()
    return sheet


def render_sheet(app: QApplication, *, dark: bool, dpr: float = 1.0) -> QImage:
    """Build every widget at one theme and compose the contact sheet."""
    apply_theme(app, dark=dark)
    page = build_page()
    dialog = build_dialog()
    tiles = [(TILE_PAGE, render_widget(page, dpr=dpr)),
             (TILE_FOCUS, render_focus_strip(dpr=dpr)),
             (TILE_DIALOG, render_widget(dialog, dpr=dpr))]
    title = SHEET_JOIN.join((
        TITLE_DARK if dark else TITLE_LIGHT,
        theme.base(dark=dark),
        theme.accent(dark=dark),
    ))
    sheet = compose(tiles, dark=dark, title=title)
    dialog.deleteLater()
    page.deleteLater()
    return sheet


def write_sheets(out_dir: Path, *, themes: tuple[bool, ...], dpr: float) -> list[Path]:
    """Render and save one PNG per theme. Returns the paths written."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle(controls.FocusRingStyle())
    fonts.apply_app_font(app)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for dark in themes:
        sheet = render_sheet(app, dark=dark, dpr=dpr)
        path = out_dir / (FILENAME_DARK if dark else FILENAME_LIGHT)
        if not sheet.save(str(path), FORMAT_PNG):
            raise OSError(f"gallery: could not write {path}")
        written.append(path)
    return written


FILENAME_LIGHT = "gallery-light.png"
FILENAME_DARK = "gallery-dark.png"
FORMAT_PNG = "PNG"
DEFAULT_OUT = _REPO_ROOT / "docs"


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="directory to write the contact sheets into")
    parser.add_argument("--theme", choices=("light", "dark", "both"),
                        default="both", help="which theme(s) to render")
    parser.add_argument("--dpr", type=float, default=1.0,
                        help="device pixel ratio to render at")
    args = parser.parse_args(argv)

    themes: tuple[bool, ...]
    if args.theme == "light":
        themes = (False,)
    elif args.theme == "dark":
        themes = (True,)
    else:
        themes = (False, True)

    for path in write_sheets(args.out, themes=themes, dpr=args.dpr):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
