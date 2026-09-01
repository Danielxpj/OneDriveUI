"""WP-12a — `onedriveui.ui.activity_center`.

The four acceptance criteria of `BUILD_PLAN.md` §WP-12 that belong to this file,
each with a test named after it:

  * the header shows the account name **while the state is ERROR** — and, here,
    while it is any of the other seventeen states as well;
  * the status line, the tooltip and the banner all come from one status source,
    with a grep proving no wording escaped into the module;
  * every user action reaches `Supervisor.do()` or a `BUS` signal, proved by an
    AST walk over the injected services rather than by a comment;
  * the flyout renders in **every** `SyncState` in both themes, and the run
    writes the labelled contact sheet a human checks OneDrive fidelity against.

Plus the window-shape regressions that motivated the design: this is a top-level
`Qt.Tool`, not a `Qt.Popup` (Mutter dismisses one of those in under 300 ms), and
`Qt.Tool` is literally `Popup | Dialog`, so the flag has to be masked to be
tested at all.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel

from onedriveui.bus import BUS
from onedriveui.constants import (
    ACTIVITY_CENTER_WIDTH, WEB_GET_MORE_STORAGE, WEB_RECYCLE_BIN, WEB_ROOT,
)
from onedriveui.models import (
    AccountInfo, ActivityState, NotificationId, PauseIntent, PauseReason,
    QuotaInfo, RecoveryAction, SyncState, TransferInfo, utcnow_iso,
)
from onedriveui.strings import (
    ACTION_LABEL, DIALOG, FIRST_SYNC_BANNER, MENU, OOBE, SETTINGS, STATUS_LINE,
    STATUS_SUB, TOAST, status_line, status_sub, toast,
)
from onedriveui.ui import fonts, icons, qss, theme
from onedriveui.ui.activity_center import (
    BANNER_SEVERITY, BANNER_TOAST, FIRST_SYNC_STATES, FOOTER_COMMANDS,
    RESUME_STATES, STATUS_ACTION, ActivityCenter, StatusTables,
    pause_remaining, status_format_args,
)
from onedriveui.ui.activity_model import ActivityModel
from onedriveui.ui.theme import METRICS, OBJ, SPACING
from onedriveui.ui.widgets.containers import InfoBarSeverity
from tests.fakes.fake_services import ACCOUNT, FakeServices, facts_for

REPO_ROOT = Path(__file__).resolve().parent.parent
AC_PY = REPO_ROOT / "onedriveui" / "ui" / "activity_center.py"
#: Where the acceptance contact sheet is written — 18 states x 2 themes.
CONTACT_SHEET = REPO_ROOT / "docs" / "wp12a-activity-center.png"

#: Every state the flyout has to render. The enum is the whole surface.
ALL_STATES = tuple(SyncState)


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
def centre(styled, fake_services):
    """A live Activity Center wired to `FakeServices`, torn down afterwards.

    `BUS` is a process-wide singleton, so the teardown is not optional: a window
    left connected keeps receiving every later test's state changes.
    """
    windows: list[ActivityCenter] = []

    def build(account: AccountInfo = ACCOUNT, **kw) -> ActivityCenter:
        kw.setdefault("quota", fake_services.quota)
        window = ActivityCenter(account,
                                supervisor=fake_services.supervisor, **kw)
        windows.append(window)
        return window

    build.services = fake_services                       # type: ignore[attr-defined]
    yield build
    for window in windows:
        window.shutdown()
        window.close()
        window.deleteLater()
    QApplication.instance().processEvents()


def render(widget, *, dpr: float = 1.0) -> QImage:
    """Render a widget offscreen onto a transparent surface."""
    widget.ensurePolished()
    size = widget.size()
    image = QImage(int(round(size.width() * dpr)), int(round(size.height() * dpr)),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    widget.render(painter, QPoint(0, 0))
    painter.end()
    return image


def is_blank(image: QImage) -> bool:
    """True when every pixel is identical — a window that painted nothing."""
    first = image.pixel(0, 0)
    step = max(1, image.width() // 40)
    for x in range(0, image.width(), step):
        for y in range(0, image.height(), step):
            if image.pixel(x, y) != first:
                return False
    return True


def press(widget, key: Qt.Key) -> None:
    event = QKeyEvent(QEvent.Type.KeyPress, int(key), Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


# ═════════════════════════════════════════════════════════════════════════════
# Window shape — why this is not a Qt.Popup
# ═════════════════════════════════════════════════════════════════════════════

def test_the_flyout_is_a_top_level_tool_window_not_a_popup(centre):
    """Verified on this machine: `QSystemTrayIcon.geometry()` is a null rect,
    `QCursor.pos()` is (0, 0), and Mutter dismisses a `Qt.Popup` with no live
    input serial in under 300 ms. So this is a real window.

    `Qt.Tool` is defined as `Popup | Dialog`, so the popup bit is set either
    way — only the masked window type distinguishes the two.
    """
    window = centre()
    kind = window.windowFlags() & Qt.WindowType.WindowType_Mask
    assert kind == Qt.WindowType.Tool
    assert kind != Qt.WindowType.Popup
    assert window.isWindow()
    assert window.parent() is None


def test_the_flyout_is_frameless_translucent_and_keeps_its_compositor_shadow(centre):
    """`Qt.NoDropShadowWindowHint` is deliberately NOT set: it exists to stop a
    second shadow when a widget draws its own, and this one does not — a Fluent
    `shadow16` would need 32 px of reserved margin on every side and the frame
    is specified as exactly 360 px wide."""
    window = centre()
    flags = window.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert not (flags & Qt.WindowType.NoDropShadowWindowHint)
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert window.graphicsEffect() is None


def test_the_flyout_is_exactly_360_px_wide(centre):
    """`constants.ACTIVITY_CENTER_WIDTH`, verbatim, and it never resizes."""
    window = centre()
    assert ActivityCenter.WIDTH == ACTIVITY_CENTER_WIDTH == 360
    assert window.width() == 360
    assert window.minimumWidth() == window.maximumWidth() == 360
    window.adjust_height()
    window.show()
    QApplication.instance().processEvents()
    assert window.width() == 360
    assert window.surface().width() == 360


def test_the_rounded_body_is_a_child_frame(centre):
    """A QSS `border-radius` on a translucent top-level window leaves square
    corners on some Wayland compositors; a child clips correctly. The object
    name is also what every `#Flyout QLabel` rule in the frozen sheet needs."""
    window = centre()
    assert window.surface().objectName() == OBJ.FLYOUT
    assert window.surface().parent() is window
    window.adjust_height()
    window.show()
    QApplication.instance().processEvents()
    image = render(window)
    assert image.pixelColor(0, 0).alpha() == 0            # rounded away
    assert image.pixelColor(180, 40).alpha() == 255       # body painted


def test_the_height_is_content_driven_between_its_two_bounds(centre):
    window = centre()
    window.model().clear()
    assert window.preferred_height() == ActivityCenter.MIN_HEIGHT
    centre.services.seed_activity(60)
    assert window.preferred_height() == ActivityCenter.MAX_HEIGHT
    window.adjust_height()
    assert window.height() == ActivityCenter.MAX_HEIGHT


def test_the_blocks_use_the_frozen_metrics():
    assert ActivityCenter.HEADER_H == METRICS["ac_header_h"] == 64
    assert ActivityCenter.STORAGE_H == METRICS["ac_storage_h"] == 56
    assert ActivityCenter.FOOTER_H == METRICS["ac_footer_h"] == 48
    assert ActivityCenter.INSET == METRICS["ac_inset"] == 16
    assert ActivityCenter.AVATAR == SPACING["xxxl"] == 32
    assert ActivityCenter.GLYPH == SPACING["l"] == 16


def test_the_gear_sits_16_px_in_from_the_right(centre):
    """MC333940: 'the settings entry point is now in the top right corner'.

    Measured from the flyout's **content** edge, which is one pixel inside the
    window: the `#Flyout` rule carries a 1 px `SurfaceStrokeColorFlyout` border,
    and Fluent's content padding sits inside the stroke, not across it.
    """
    window = centre()
    window.adjust_height()
    window.show()
    QApplication.instance().processEvents()
    gear = window.settings_button()
    gear_right = gear.mapTo(window, QPoint(gear.width(), 0)).x()
    surface = window.surface()
    content_right = surface.mapTo(
        window, QPoint(surface.contentsRect().right() + 1, 0)).x()
    assert content_right - gear_right == ActivityCenter.INSET
    assert window.width() - content_right == 1           # the flyout's stroke


def test_escape_dismisses_the_flyout(centre):
    window = centre()
    seen: list[int] = []
    window.dismissed.connect(lambda: seen.append(1))
    window.show()
    QApplication.instance().processEvents()
    press(window, Qt.Key.Key_Escape)
    QApplication.instance().processEvents()
    assert seen == [1]
    assert not window.isVisible()


def test_reopening_an_open_flyout_does_not_replay_the_entrance(centre,
                                                                monkeypatch):
    """The second "Open Activity Center" is a request to bring the window
    forward. Fading in a window the user is already looking at reads as a
    blink, not as an entrance."""
    from onedriveui.ui import motion

    fades: list[str] = []
    monkeypatch.setattr(motion, "fade_in",
                        lambda *a, **kw: fades.append("fade"))

    window = centre()
    window.open_()
    QApplication.instance().processEvents()
    assert fades == ["fade"]

    window.open_()                       # already visible
    QApplication.instance().processEvents()
    assert fades == ["fade"]
    assert window.isVisible()


def test_open_places_the_window_inside_the_work_area(centre):
    window = centre()
    window.open_()
    QApplication.instance().processEvents()
    screen = window.screen() or QApplication.primaryScreen()
    area = screen.availableGeometry()
    where = window.placement()
    assert where.x() + window.width() <= area.right() + 1
    assert where.y() + window.height() <= area.bottom() + 1
    assert window.placement(QPoint(7, 9)) == QPoint(7, 9)


# ═════════════════════════════════════════════════════════════════════════════
# The header — MC333940
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: s.name)
def test_the_header_shows_the_account_name_in_every_state(centre, state):
    """ACCEPTANCE: the account name is visible while the state is ERROR.

    Microsoft made this an explicit change (MC333940) because with two accounts
    signed in, a flyout that drops the name in an error state leaves the user
    unable to tell *which* OneDrive is broken. It holds here structurally:
    `set_state()` does not touch the three header widgets at all.
    """
    window = centre()
    centre.services.drive_state(state)
    assert window.state() is state
    assert window.header_name() == ACCOUNT.display_name
    assert window.header_email() == ACCOUNT.email
    assert not window._name.isHidden()                   # noqa: SLF001


def test_the_error_state_specifically_keeps_the_name(centre):
    """The named acceptance criterion, spelled out on its own."""
    window = centre()
    centre.services.drive_state(SyncState.ERROR)
    assert window.state() is SyncState.ERROR
    assert window.status_headline() == STATUS_LINE[SyncState.ERROR]
    assert window.header_name() == "Test User"


def test_the_header_falls_back_when_there_is_no_display_name(centre):
    """rclone's `Features.UserInfo` is false for OneDrive, so identity can be
    missing; the header still has to name the account."""
    bare = AccountInfo(id="x", remote="work", email="a@b.example")
    assert centre(bare).header_name() == "a@b.example"
    nameless = AccountInfo(id="y", remote="personal")
    window = centre(nameless)
    assert window.header_name() == "personal"
    assert window.header_email() == ""
    assert window._email.isHidden()                      # noqa: SLF001


def test_an_account_update_re_renders_the_header(centre):
    window = centre()
    renamed = AccountInfo(id=ACCOUNT.id, remote=ACCOUNT.remote,
                          display_name="Renamed User", email="new@example.com")
    BUS.account_updated.emit(renamed)
    assert window.header_name() == "Renamed User"
    assert window.header_email() == "new@example.com"


def test_another_accounts_update_is_ignored(centre):
    window = centre()
    BUS.account_updated.emit(AccountInfo(id="someone-else", remote="other",
                                         display_name="Nope"))
    assert window.header_name() == ACCOUNT.display_name


# ═════════════════════════════════════════════════════════════════════════════
# Status, tooltip and banner — one source, no literals
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: s.name)
def test_the_headline_is_the_frozen_table_for_every_state(centre, state):
    window = centre()
    facts = facts_for(state)
    window.set_state(state, facts)
    args = status_format_args(state, facts)
    assert window.status_headline() == status_line(state, **args)


def test_the_fallback_status_source_covers_every_state():
    """`StatusTables` stands in for `sync/reducer.py` until WP-05 lands, so it
    has to answer for all eighteen states without raising."""
    tables = StatusTables()
    for state in ALL_STATES:
        facts = facts_for(state)
        headline, subtext = tables.status_text(state, facts)
        assert headline == status_line(state, **status_format_args(state, facts))
        assert isinstance(subtext, str)
        assert tables.tooltip(state, facts) == f"{headline}\n{subtext}".strip()


def test_the_footer_table_names_real_glyphs_and_real_actions():
    assert len(FOOTER_COMMANDS) == 3
    for glyph, tooltip, action, url in FOOTER_COMMANDS:
        assert glyph in icons.GLYPHS
        assert tooltip in vars(MENU).values()
        assert isinstance(action, RecoveryAction)
        assert url == "" or url.startswith("https://")


def test_the_tooltip_is_the_headline_and_the_second_line(centre):
    """The tooltip is the tray's own two-line text and stays whole, even where
    the flyout body drops the second line as a duplicate of its banner."""
    window = centre()
    facts = facts_for(SyncState.PAUSED_METERED)
    window.set_state(SyncState.PAUSED_METERED, facts)
    headline, subtext = StatusTables().status_text(SyncState.PAUSED_METERED, facts)
    assert window.status_tooltip() == f"{headline}\n{subtext}"
    assert window.surface().toolTip() == window.status_tooltip()
    assert window.status_headline() == headline


def test_the_second_line_never_repeats_the_banner(centre):
    """Two copies of the same sentence eight pixels apart is the failure mode
    a single wording source is supposed to make impossible."""
    window = centre()
    for state in BANNER_TOAST:
        facts = facts_for(state)
        window.set_state(state, facts)
        shown = window.status_subtext()
        title, message = window.banner_text()
        if shown:
            assert shown.rstrip(".") not in message
            assert shown.rstrip(".") not in title


def test_the_metered_reason_is_shown_exactly_once(centre):
    """The concrete case: the reason lives in the banner, beside its override."""
    window = centre()
    facts = facts_for(SyncState.PAUSED_METERED)
    window.set_state(SyncState.PAUSED_METERED, facts)
    reason = status_sub(SyncState.PAUSED_METERED)
    assert reason in window.banner_text()[1]
    assert window.status_subtext() == ""
    assert reason in window.status_tooltip()


def test_an_injected_reducer_wins_over_the_fallback_tables(centre):
    """Production injects `sync/reducer.py`; `StatusTables` only stands in until
    WP-05 lands, so the injection has to actually take precedence."""

    class Reducer:
        def status_text(self, state, facts):
            return f"H:{state.name}", f"S:{facts.account_id}"

        def tooltip(self, state, facts):
            return f"T:{state.name}"

    window = centre(status=Reducer())
    window.set_state(SyncState.SYNCING, facts_for(SyncState.SYNCING))
    assert window.status_headline() == "H:SYNCING"
    assert window.status_subtext() == "S:onedrive"
    assert window.status_tooltip() == "T:SYNCING"


def test_a_timed_pause_counts_down_from_facts_alone(centre):
    """The reducer is pure and clock-free, so the fallback measures between two
    fields of the same `Facts` rather than asking the wall clock."""
    facts = facts_for(SyncState.PAUSED_MANUAL,
                      sampled_at="2026-08-31T12:00:00Z",
                      pause=PauseIntent(reason=PauseReason.MANUAL,
                                        until="2026-08-31T13:30:00Z"))
    assert pause_remaining(facts) == (1, 30)
    window = centre()
    window.set_state(SyncState.PAUSED_MANUAL, facts)
    assert window.status_subtext() == status_sub(SyncState.PAUSED_MANUAL,
                                                 hh=1, mm=30)


def test_an_indefinite_pause_draws_no_countdown(centre):
    """"Until I resume" has no deadline, and a template must never render its
    own placeholders at the user."""
    facts = facts_for(SyncState.PAUSED_MANUAL,
                      pause=PauseIntent(reason=PauseReason.MANUAL, until=None))
    assert pause_remaining(facts) is None
    window = centre()
    window.set_state(SyncState.PAUSED_MANUAL, facts)
    assert window.status_subtext() == ""
    assert "{" not in window.status_tooltip()


def test_an_elapsed_pause_deadline_draws_no_countdown():
    facts = facts_for(SyncState.PAUSED_MANUAL,
                      sampled_at="2026-08-31T14:00:00Z",
                      pause=PauseIntent(reason=PauseReason.MANUAL,
                                        until="2026-08-31T13:30:00Z"))
    assert pause_remaining(facts) is None


def test_no_state_renders_an_unfilled_placeholder(centre):
    """`strings.t()` returns the template untouched when a key is missing, which
    is the right failure mode for a wording bug and the wrong thing to ship."""
    window = centre()
    for state in ALL_STATES:
        window.set_state(state, facts_for(state))
        assert "{" not in window.status_headline()
        assert "{" not in window.status_subtext()
        assert "{" not in " ".join(window.banner_text())


@pytest.mark.parametrize("state", sorted(BANNER_TOAST, key=lambda s: s.name),
                         ids=lambda s: s.name)
def test_a_catalogued_state_banner_comes_from_the_toast_table(centre, state):
    window = centre()
    facts = facts_for(state)
    window.set_state(state, facts)
    summary, body, actions = toast(BANNER_TOAST[state],
                                   **status_format_args(state, facts))
    assert window.banner_visible()
    assert window.banner_text()[1] == body
    assert window.banner().severity() is BANNER_SEVERITY[state]
    assert len(window.banner_actions()) == len(actions)
    assert summary  # the toast has one; the banner may drop it as a duplicate


@pytest.mark.parametrize("state", sorted(BANNER_TOAST, key=lambda s: s.name),
                         ids=lambda s: s.name)
def test_a_banner_never_repeats_the_headline_as_its_title(centre, state):
    """A toast summary restates the status line because a toast is read with no
    status line beside it. Here the headline is two lines up, so the title would
    be the same sentence twice — and it is the one label in the bar that does not
    wrap, so it is also the one that squeezes the message.
    """
    window = centre()
    window.set_state(state, facts_for(state))
    title, _message = window.banner_text()
    assert title.rstrip(".") != window.status_headline().rstrip(".")


@pytest.mark.parametrize("state", sorted(BANNER_TOAST, key=lambda s: s.name),
                         ids=lambda s: s.name)
def test_a_banners_buttons_never_overlap_its_text(centre, state, qapp):
    """The regression this layout exists to prevent.

    With the action button inside the `InfoBar`'s own row, a 326 px bar minus the
    glyph, the close button and a 139 px "get more storage" left the message
    **77 px** — five stacked words — and Qt resolved the over-constrained row by
    drawing the button on top of the text. The actions now live on their own row,
    which is WinUI's narrow-`InfoBar` reflow; this asserts the result.
    """
    window = centre()
    window.set_state(state, facts_for(state))
    window.adjust_height()
    window.show()
    qapp.processEvents()
    banner = window.banner()
    labels = [label for label in banner.findChildren(QLabel)
              if label.isVisible() and label.text()]
    assert labels, f"{state.name}: the banner drew no text"
    for label in labels:
        assert label.width() >= 3 * SPACING["xxxl"], (
            f"{state.name}: {label.text()!r} was squeezed to {label.width()} px")
        label_right = label.mapTo(banner, QPoint(label.width(), 0)).x()
        close_left = banner.close_button().mapTo(banner, QPoint(0, 0)).x()
        assert label_right <= close_left, (
            f"{state.name}: {label.text()!r} runs under the close button")
    for button in window.banner_actions():
        assert button.parentWidget() is not banner
        assert button.mapTo(window, QPoint(0, 0)).y() >= (
            banner.mapTo(window, QPoint(0, banner.height())).y())


def test_the_first_sync_banner_shows_on_a_first_run(centre):
    """[verbatim] shown while INITIALIZING / PROCESSING on an account's first
    run — an account that has never recorded a successful sync."""
    window = centre()
    assert window.is_first_run()                        # ACCOUNT.last_ok_at is None
    for state in FIRST_SYNC_STATES:
        window.set_state(state, facts_for(state))
        assert window.banner_text() == ("", FIRST_SYNC_BANNER)
        assert not window.banner().is_closable()


def test_the_first_sync_banner_is_gone_once_a_sync_has_landed(centre):
    settled = AccountInfo(id=ACCOUNT.id, remote=ACCOUNT.remote,
                          display_name=ACCOUNT.display_name,
                          email=ACCOUNT.email, last_ok_at=utcnow_iso())
    window = centre(settled)
    assert not window.is_first_run()
    window.set_state(SyncState.PROCESSING, facts_for(SyncState.PROCESSING))
    assert not window.banner_visible()


def test_the_first_run_answer_can_be_forced_either_way(centre):
    window = centre()
    window.set_first_run(False)
    window.set_state(SyncState.INITIALIZING, facts_for(SyncState.INITIALIZING))
    assert not window.banner_visible()
    window.set_first_run(True)
    assert window.banner_text() == ("", FIRST_SYNC_BANNER)
    window.set_first_run(None)
    assert window.is_first_run()


def test_the_info_notice_banner_carries_the_engines_own_text(centre):
    window = centre()
    facts = facts_for(SyncState.INFO_NOTICE)
    window.set_state(SyncState.INFO_NOTICE, facts)
    assert window.banner_text() == ("", facts.info_notice)


def test_offline_carries_its_reason_on_the_second_line_not_in_a_banner(centre):
    """`onedrive-features.md` §2.3 gives OFFLINE a status line and a retry, not
    a banner — and it already has a "try again" command button."""
    window = centre()
    window.set_state(SyncState.OFFLINE, facts_for(SyncState.OFFLINE))
    assert not window.banner_visible()
    assert window.status_subtext() == status_sub(SyncState.OFFLINE)
    assert window.command_button().text() == ACTION_LABEL[
        STATUS_ACTION[SyncState.OFFLINE]]


def test_a_state_with_no_catalogued_notice_shows_no_banner(centre):
    """Every one of these already carries its whole story in the status strip:
    a headline, a second line and one command button. `ERROR` and
    `NEEDS_ATTENTION` additionally depend on *which* hazard fired, so no guess is
    baked in — `ui/notices.py` supplies those through `set_banner()`.
    """
    window = centre()
    for state in (SyncState.ERROR, SyncState.NEEDS_ATTENTION,
                  SyncState.UP_TO_DATE, SyncState.SIGNED_OUT,
                  SyncState.AUTH_REQUIRED, SyncState.ACCOUNT_BLOCKED,
                  SyncState.PAUSED_MANUAL, SyncState.OFFLINE,
                  SyncState.SYNCING, SyncState.MOUNTING):
        window.set_state(state, facts_for(state))
        assert not window.banner_visible(), state
        assert window.status_headline() == STATUS_LINE[state].format(
            **status_format_args(state, facts_for(state)))


def test_the_banner_set_is_the_one_the_research_documents(centre):
    """A banner earns its space only where the status strip cannot carry the
    state: the quota InfoBar of ARCHITECTURE §6.6, the two policy pauses that
    offer an override, and the sync-issues banner of features §2.5.
    """
    assert set(BANNER_TOAST) == {
        SyncState.PAUSED_QUOTA, SyncState.PAUSED_METERED,
        SyncState.PAUSED_BATTERY, SyncState.WARNING,
    }
    for state, nid in BANNER_TOAST.items():
        assert TOAST[nid][2], f"{state.name} banner carries no action"


def test_an_explicit_banner_survives_a_state_change(centre):
    """The notice router puts a banner here; a tick must not wipe it."""
    window = centre()
    window.set_banner(MENU.SYNC_PROBLEMS, DIALOG.RESYNC_BODY,
                      severity=InfoBarSeverity.ERROR,
                      actions=(("resync", ACTION_LABEL[RecoveryAction.RESYNC]),),
                      key=NotificationId.NEEDS_RESYNC.value)
    window.set_state(SyncState.UP_TO_DATE, facts_for(SyncState.UP_TO_DATE))
    assert window.banner_text() == (MENU.SYNC_PROBLEMS, DIALOG.RESYNC_BODY)
    window.clear_banner()
    assert not window.banner_visible()


def test_clearing_a_banner_restores_the_states_own(centre):
    window = centre()
    window.set_state(SyncState.PAUSED_METERED, facts_for(SyncState.PAUSED_METERED))
    automatic = window.banner_text()
    window.set_banner(DIALOG.QUIT_TITLE, DIALOG.QUIT_BODY)
    assert window.banner_text() == (DIALOG.QUIT_TITLE, DIALOG.QUIT_BODY)
    window.clear_banner()
    assert window.banner_text() == automatic


def test_an_empty_banner_hides_it(centre):
    window = centre()
    window.set_banner("", "")
    assert not window.banner_visible()
    assert window.banner_text() == ("", "")


def test_a_banner_action_goes_onto_the_bus(centre, bus_spy):
    """`ui/notices.py` owns what an action id means; the flyout only reports it."""
    bus_spy.watch("notification_action")
    window = centre()
    window.set_state(SyncState.PAUSED_METERED, facts_for(SyncState.PAUSED_METERED))
    buttons = window.banner_actions()
    assert len(buttons) == 1
    buttons[0].click()
    action_id = TOAST[NotificationId.SYNC_PAUSED_METERED][2][0][0]
    assert bus_spy.of("notification_action") == [
        (NotificationId.SYNC_PAUSED_METERED.value, action_id)]
    assert window.banner_key() == NotificationId.SYNC_PAUSED_METERED.value


def test_a_live_count_in_a_banner_keeps_up_without_rebuilding_its_buttons(centre):
    """The copy has to track the tick; the buttons must not be destroyed and
    recreated under the pointer 2.5 times a second."""
    window = centre()
    facts = facts_for(SyncState.WARNING, issues_error=3)
    window.set_state(SyncState.WARNING, facts)
    button = window.banner_actions()[0]
    first = window.banner_text()
    BUS.facts_updated.emit(facts_for(SyncState.WARNING, issues_error=9))
    assert window.banner_text() != first
    assert "9" in window.banner_text()[1]
    assert window.banner_actions()[0] is button


def test_no_banner_carries_more_than_two_actions(centre):
    """`NotifySpec` caps at two because GNOME renders about three."""
    window = centre()
    for state in BANNER_TOAST:
        window.set_state(state, facts_for(state))
        assert len(window.banner_actions()) <= 2


# ═════════════════════════════════════════════════════════════════════════════
# Status strip: glyph, progress, the one contextual command
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: s.name)
def test_the_status_glyph_follows_the_frozen_tray_table(centre, state):
    from onedriveui.strings import TRAY_FOR_STATE

    window = centre()
    window.set_state(state, facts_for(state))
    assert window._glyph.tray() is TRAY_FOR_STATE[state]  # noqa: SLF001


def test_the_progress_bar_is_determinate_while_transferring(centre):
    window = centre()
    facts = facts_for(SyncState.SYNCING)
    window.set_state(SyncState.SYNCING, facts)
    bar = window._progress                               # noqa: SLF001
    assert not bar.isHidden()
    assert not bar.is_indeterminate()
    expected = facts.transfers_active / (facts.transfers_active + facts.uploads_queued)
    assert bar.value() == pytest.approx(expected)


def test_the_progress_bar_is_indeterminate_while_merely_busy(centre):
    window = centre()
    window.set_state(SyncState.PROCESSING, facts_for(SyncState.PROCESSING))
    assert window._progress.is_indeterminate()           # noqa: SLF001


def test_the_progress_bar_is_gone_at_rest(centre):
    window = centre()
    window.set_state(SyncState.UP_TO_DATE, facts_for(SyncState.UP_TO_DATE))
    assert window._progress.isHidden()                   # noqa: SLF001


@pytest.mark.parametrize("state", sorted(STATUS_ACTION, key=lambda s: s.name),
                         ids=lambda s: s.name)
def test_the_contextual_command_is_labelled_from_the_frozen_table(centre, state):
    window = centre()
    window.set_state(state, facts_for(state))
    assert window.command_button().text() == ACTION_LABEL[STATUS_ACTION[state]]
    assert not window.command_button().isHidden()


@pytest.mark.parametrize("state", sorted(RESUME_STATES, key=lambda s: s.name),
                         ids=lambda s: s.name)
def test_a_paused_state_offers_resume(centre, state):
    window = centre()
    window.set_state(state, facts_for(state))
    assert window.command_button().text() == MENU.RESUME


def test_a_state_with_no_command_hides_the_button(centre):
    window = centre()
    window.set_state(SyncState.UP_TO_DATE, facts_for(SyncState.UP_TO_DATE))
    assert window.command_button().isHidden()


def test_no_state_offers_the_same_command_twice(centre):
    """A banner action and a status-strip button for one state is the same
    command drawn twice, which is exactly what a single wording source is for.
    The tables are asserted disjoint at import; this is the rendered proof.
    """
    assert not (set(STATUS_ACTION) | RESUME_STATES) & set(BANNER_TOAST)
    window = centre()
    for state in ALL_STATES:
        window.set_state(state, facts_for(state))
        labels: list[str] = []
        if window.banner_visible():
            labels += [button.text()
                       for button in window.banner_actions()]
        if not window.command_button().isHidden():
            labels.append(window.command_button().text())
        if not window.storage_link().isHidden():
            labels.append(window.storage_link().text())
        assert len(labels) == len(set(labels)), f"{state.name}: {labels}"


# ═════════════════════════════════════════════════════════════════════════════
# Storage block
# ═════════════════════════════════════════════════════════════════════════════

def test_the_storage_line_is_the_frozen_template(centre):
    from onedriveui import units

    window = centre()
    quota = centre.services.quota.current()
    assert window.storage_text() == status_sub(
        SyncState.UP_TO_DATE,
        used=units.human_bytes(quota.used),
        total=units.human_bytes(quota.total))


def test_the_storage_bar_tracks_the_quota(centre):
    window = centre()
    centre.services.quota.set_tier("warn")
    assert window.storage_bar().fraction() == pytest.approx(0.85, abs=0.01)
    centre.services.quota.set_tier("full")
    assert window.storage_bar().fraction() == pytest.approx(1.0)


def test_the_upsell_link_appears_only_when_storage_is_tight(centre):
    window = centre()
    centre.services.quota.set_tier("ok")
    assert window.storage_link().isHidden()
    centre.services.quota.set_tier("critical")
    assert not window.storage_link().isHidden()
    assert window.storage_link().text() == ACTION_LABEL[
        RecoveryAction.GET_MORE_STORAGE]


def test_without_a_quota_service_the_storage_comes_from_facts(centre):
    window = centre(quota=None)
    facts = facts_for(SyncState.UP_TO_DATE,
                      quota=QuotaInfo(total=1000, used=900, free=100))
    BUS.facts_updated.emit(facts)
    assert window.storage_bar().fraction() == pytest.approx(0.9)


# ═════════════════════════════════════════════════════════════════════════════
# Actions — Supervisor.do() or the bus, and nothing else
# ═════════════════════════════════════════════════════════════════════════════

def test_the_footer_is_the_five_documented_commands(centre):
    window = centre()
    tooltips = [button.toolTip() for button in window.footer_buttons()]
    assert tooltips == [MENU.OPEN_FOLDER, MENU.VIEW_ONLINE, MENU.RECYCLE_BIN,
                        MENU.SETTINGS, MENU.HELP]
    assert all(button.accessibleName() for button in window.footer_buttons())


def test_open_folder_reaches_the_supervisor(centre):
    window = centre()
    window.footer_buttons()[0].click()
    assert centre.services.supervisor.actions == [
        (RecoveryAction.SHOW_IN_FOLDER, {"path": ACCOUNT.sync_root})]


def test_view_online_and_the_recycle_bin_are_web_deep_links(centre):
    """Invariant I8: `operations/cleanup` deletes file *versions* on OneDrive,
    not the bin. The recycle bin is a URL and nothing else."""
    window = centre()
    window.footer_buttons()[1].click()
    window.footer_buttons()[2].click()
    assert centre.services.supervisor.actions == [
        (RecoveryAction.OPEN_WEB, {"url": WEB_ROOT}),
        (RecoveryAction.OPEN_WEB, {"url": WEB_RECYCLE_BIN}),
    ]


def test_settings_and_help_are_navigation_signals(centre):
    window = centre()
    seen: list[str] = []
    window.settings_requested.connect(lambda: seen.append("settings"))
    window.help_requested.connect(lambda: seen.append("help"))
    window.settings_button().click()
    window.footer_buttons()[3].click()
    window.footer_buttons()[4].click()
    assert seen == ["settings", "settings", "help"]
    assert centre.services.supervisor.actions == []


def test_the_storage_link_reaches_the_supervisor(centre):
    window = centre()
    window.storage_link().click()
    assert centre.services.supervisor.actions == [
        (RecoveryAction.GET_MORE_STORAGE, {"url": WEB_GET_MORE_STORAGE})]


def test_the_contextual_command_reaches_the_supervisor(centre):
    window = centre()
    window.set_state(SyncState.SIGNED_OUT, facts_for(SyncState.SIGNED_OUT))
    window.command_button().click()
    window.set_state(SyncState.ACCOUNT_BLOCKED, facts_for(SyncState.ACCOUNT_BLOCKED))
    window.command_button().click()
    assert centre.services.supervisor.actions == [
        (RecoveryAction.SIGN_IN, {}),
        (RecoveryAction.OPEN_WEB, {"url": WEB_ROOT}),
    ]


def test_resume_uses_the_supervisors_own_entry_point(centre):
    """`request_resume()` is on the frozen `Supervisor` signature; resuming is
    not a `RecoveryAction`, and the pause manager is never touched directly."""
    window = centre()
    window.set_state(SyncState.PAUSED_MANUAL, facts_for(SyncState.PAUSED_MANUAL))
    window.command_button().click()
    assert ("request_resume", {}) in centre.services.supervisor.calls
    assert centre.services.pause.calls == []


def test_activating_a_row_is_re_emitted_not_acted_on(centre):
    window = centre()
    centre.services.seed_activity(3)
    seen: list[object] = []
    window.row_activated.connect(seen.append)
    window.list_view().activated.emit(window.model().index(1, 0))
    assert len(seen) == 1
    assert centre.services.supervisor.actions == []


# ═════════════════════════════════════════════════════════════════════════════
# The two greps
# ═════════════════════════════════════════════════════════════════════════════

def _string_constants(path: Path, *, docstrings: bool = True) -> list[str]:
    """Every string constant in a module.

    `docstrings=False` drops the module, class and function docstrings, which is
    the right filter for "no user-facing literal": a docstring is developer
    documentation and is never rendered at anybody.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    if not docstrings:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                skip.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in skip]


def _fragments(template: str, minimum: int = 10) -> list[str]:
    """The alphabetic-and-space runs of a template, ignoring its placeholders."""
    runs: list[str] = []
    current = ""
    for char in template:
        if char.isalpha() or char == " ":
            current += char
        else:
            runs.append(current)
            current = ""
    runs.append(current)
    return [run.strip() for run in runs if len(run.strip()) >= minimum]


def test_no_status_literal_appears_in_the_activity_center():
    """ACCEPTANCE: the status line, the tooltip and the banner all come from
    `reducer.status_text()` / `strings.STATUS_LINE` — never from a literal here.

    Three passes, because a literal can hide three ways: as its whole self, as an
    implicitly-concatenated pair (which the AST rejoins), and as a distinctive
    fragment of a template whose placeholders were substituted by hand.
    """
    source = AC_PY.read_text(encoding="utf-8")
    templates = (list(STATUS_LINE.values()) + list(STATUS_SUB.values())
                 + [FIRST_SYNC_BANNER])
    for template in templates:
        assert template not in source, f"{template!r} is written into the module"
    for constant in _string_constants(AC_PY):
        for template in templates:
            assert template not in constant, f"{template!r} is quoted"
    for template in templates:
        for fragment in _fragments(template):
            assert fragment not in source, (
                f"{fragment!r} (from {template!r}) is written into the module")


def test_no_user_facing_wording_at_all_appears_in_the_activity_center():
    """`CONTRACTS.md` §11: no user-facing string literal outside `strings.py`.

    Quoted strings only — a docstring that *names* a command ("opening Settings
    or Help") is documentation, and rewriting prose to dodge a grep would make
    the module worse rather than the rule stronger.
    """
    tables = (list(vars(MENU).values()) + list(vars(DIALOG).values())
              + list(vars(SETTINGS).values()) + list(vars(OOBE).values())
              + list(ACTION_LABEL.values())
              + [summary for summary, _b, _a in TOAST.values()])
    wording = {value for value in tables
               if isinstance(value, str) and len(value) >= 4}
    for constant in _string_constants(AC_PY, docstrings=False):
        for phrase in sorted(wording):
            assert phrase not in constant, (
                f"{phrase!r} is quoted in the module as {constant!r}")


def test_the_module_really_does_read_the_frozen_tables():
    """The positive half of the grep: it is sourcing, not just not-literalling."""
    source = AC_PY.read_text(encoding="utf-8")
    assert "from onedriveui.strings import" in source
    assert "status_line" in source and "status_sub" in source
    assert "FIRST_SYNC_BANNER" in source


#: Every method the window is allowed to call on an injected engine object.
#: Reads are free; the only writes are `Supervisor`'s own mediating entry points.
SERVICE_METHODS: dict[str, frozenset[str]] = {
    "_supervisor": frozenset({"do", "snapshot", "state",
                              "request_pause", "request_resume"}),
    "_quota": frozenset({"current"}),
    "_status_source": frozenset({"status_text", "tooltip"}),
}

#: Mutating service calls that must never appear. Every one of these belongs to
#: a service the UI is forbidden to touch directly.
FORBIDDEN_CALLS: tuple[str, ...] = (
    ".pin(", ".unpin(", ".free_up_space(", ".free_up_all(", ".download_all(",
    ".create_link(", ".soft_delete(", ".restore_from_trash(", ".purge_expired(",
    ".evict(", ".evict_tree(", ".forget(", ".call(", ".call_blocking(",
    ".ingest_health(", ".ingest_transfer_error(", ".execute(", ".mute(",
    ".reset_client(", ".request_resync(", ".restart_mount(", ".enforce(",
    ".reclaim_orphaned_cache(", ".sync_anyway(", ".set_auto(", ".apply(",
    ".notify(", ".ensure_mounted(", ".unmount(", ".raise_issue(",
)


def test_no_direct_service_call_leaves_the_activity_center():
    """ACCEPTANCE: every user action goes through `Supervisor.do()` or the bus.

    An AST walk rather than a grep, because the question is *which object* a
    method was called on, and a text search cannot answer that.
    """
    tree = ast.parse(AC_PY.read_text(encoding="utf-8"))
    called: dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        owner = func.value
        if not (isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "self"):
            continue
        if owner.attr in SERVICE_METHODS:
            called[owner.attr].add(func.attr)
    for attribute, allowed in SERVICE_METHODS.items():
        extra = called[attribute] - allowed
        assert not extra, f"self.{attribute} is called with {sorted(extra)}"
    assert "do" in called["_supervisor"], "nothing reaches Supervisor.do()"


def test_the_activity_center_imports_no_engine_module():
    """WP-05 … WP-09 are injected signatures, never imports: WP-12 is a wave
    ahead of them and has to build without them existing."""
    source = AC_PY.read_text(encoding="utf-8")
    assert "onedriveui.sync" not in source
    assert "onedriveui.rc" not in source
    assert "onedriveui.data" not in source
    for call in FORBIDDEN_CALLS:
        assert call not in source, f"{call!r} is a direct service call"


def test_the_bus_connections_are_released_on_shutdown(styled, fake_services):
    """`BUS` is a process-wide singleton; a window that is dropped without
    `shutdown()` keeps receiving ticks forever."""
    window = ActivityCenter(ACCOUNT, supervisor=fake_services.supervisor,
                            quota=fake_services.quota)
    fake_services.drive_state(SyncState.SYNCING)
    assert window.state() is SyncState.SYNCING
    assert window.model().is_attached()
    window.shutdown()
    fake_services.drive_state(SyncState.ERROR)
    assert window.state() is SyncState.SYNCING
    assert not window.model().is_attached()
    window.close()
    window.deleteLater()


def test_an_injected_model_is_not_owned(styled, fake_services):
    """A caller that supplies a model also owns its bus lifetime."""
    model = ActivityModel(account_id=ACCOUNT.id)
    model.attach_bus()
    window = ActivityCenter(ACCOUNT, supervisor=fake_services.supervisor,
                            model=model)
    try:
        window.shutdown()
        assert model.is_attached()
    finally:
        model.detach_bus()
        window.close()
        window.deleteLater()


def test_the_flyout_ignores_another_accounts_ticks(centre):
    window = centre()
    window.set_state(SyncState.UP_TO_DATE, facts_for(SyncState.UP_TO_DATE))
    BUS.state_changed.emit(SyncState.UP_TO_DATE, SyncState.ERROR,
                           facts_for(SyncState.ERROR, account_id="someone-else"))
    assert window.state() is SyncState.UP_TO_DATE


# ═════════════════════════════════════════════════════════════════════════════
# Rendering — 18 states x 2 themes, and the contact sheet
# ═════════════════════════════════════════════════════════════════════════════

def _apply_theme(qapp, dark: bool, monkeypatch) -> None:
    """Put the application on one theme, with animation off.

    Animation off is this machine's real setting — both `gtk-enable-animations`
    and `org.gnome.desktop.interface enable-animations` are false here — and it
    is also what makes a still frame worth looking at: `SafeLoop` freezes an
    indeterminate indicator at its `STATIC_PHASE` mid-cycle rather than at the
    phase-0 frame, where the travelling segment has not entered the track yet
    and the bar renders as an empty rule.
    """
    monkeypatch.setattr(theme, "_DETECTED_DARK", dark, raising=False)
    monkeypatch.setattr(theme, "_ANIMATIONS", False, raising=False)
    theme._STYLESHEET_CACHE.clear()                      # noqa: SLF001
    qss.invalidate()
    icons.clear_cache()
    fonts.apply_app_font(qapp)
    qss.apply(qapp, dark=dark)


#: Two live `core/stats.transferring[]` rows, so the SYNCING tile shows the
#: inline per-file bars that are the most recognisable part of OneDrive's feed.
LIVE_TRANSFERS = (
    TransferInfo(name="Documents/Quarterly report.xlsx", size=4_800_000,
                 bytes=2_160_000, percentage=45, speed=1_240_000.0,
                 speed_avg=1_100_000.0, eta=3, group="job/1",
                 src_fs="/home/u/OneDrive", dst_fs="onedrive:"),
    TransferInfo(name="Pictures/2026/beach.jpg", size=6_200_000,
                 bytes=930_000, percentage=15, speed=480_000.0,
                 speed_avg=460_000.0, eta=11, group="job/1",
                 src_fs="onedrive:", dst_fs="/home/u/OneDrive"),
)


def _tile(qapp, services: FakeServices, state: SyncState,
          *, rows: int = 10) -> QImage:
    """Build a flyout, fill it, drive it to `state`, and render it.

    The activity is seeded **after** the window exists: `seed_activity` reaches
    a model over the bus, so a feed emitted before construction would arrive
    nowhere and every tile would show an empty list.
    """
    window = ActivityCenter(ACCOUNT, supervisor=services.supervisor,
                            quota=services.quota)
    try:
        services.seed_activity(rows)
        if state is SyncState.SYNCING:
            BUS.transfers_updated.emit(list(LIVE_TRANSFERS))
        services.drive_state(state)
        window.adjust_height()
        window.show()
        qapp.processEvents()
        return render(window)
    finally:
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.slow
@pytest.mark.parametrize("dark_theme", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: s.name)
def test_the_flyout_renders_in_every_state_and_theme(qapp, monkeypatch,
                                                     fake_services, state,
                                                     dark_theme):
    """ACCEPTANCE: all eighteen `SyncState`s, light and dark, offscreen."""
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    try:
        _apply_theme(qapp, dark_theme, monkeypatch)
        image = _tile(qapp, fake_services, state, rows=8)
        assert image.width() == ActivityCenter.WIDTH
        assert ActivityCenter.MIN_HEIGHT <= image.height() <= ActivityCenter.MAX_HEIGHT
        assert not is_blank(image)
        # The flyout body is the LAYER surface, not the window base: a flyout
        # sits one layer above the window background.
        assert image.pixelColor(180, 40).name().upper() == \
            theme.layer(dark=dark_theme).upper()
        assert image.pixelColor(0, 0).alpha() == 0        # the 8 px rounding
    finally:
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)
        _apply_theme(qapp, False, monkeypatch)
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)


@pytest.mark.slow
def test_the_contact_sheet_is_written(qapp, monkeypatch, fake_services, tmp_path):
    """ACCEPTANCE: one labelled PNG under `docs/` covering 18 states x 2 themes.

    This is how a human checks OneDrive fidelity without running the app, so it
    is laid out to be read: six columns, the eighteen light tiles first, then the
    eighteen dark ones, every tile captioned with its theme and its state.
    """
    previous_sheet = qapp.styleSheet()
    previous_font = qapp.font()
    tiles: list[tuple[str, QImage]] = []
    try:
        for dark_theme in (False, True):
            _apply_theme(qapp, dark_theme, monkeypatch)
            label = "dark" if dark_theme else "light"
            for state in ALL_STATES:
                tiles.append((f"{label} · {state.name}",
                              _tile(qapp, fake_services, state)))
    finally:
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)
        _apply_theme(qapp, False, monkeypatch)
        qapp.setStyleSheet(previous_sheet)
        qapp.setFont(previous_font)

    assert len(tiles) == 2 * len(ALL_STATES) == 36

    columns = 6
    gutter = SPACING["l"]
    caption_h = SPACING["xxl"]
    cell_w = max(tile.width() for _label, tile in tiles)
    cell_h = max(tile.height() for _label, tile in tiles) + caption_h
    rows = (len(tiles) + columns - 1) // columns
    sheet = QImage(columns * cell_w + gutter * (columns + 1),
                   rows * cell_h + gutter * (rows + 1),
                   QImage.Format.Format_ARGB32_Premultiplied)
    sheet.fill(QColor(theme.T("SolidBackgroundFillColorBaseAlt", dark=False)))

    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.setFont(fonts.font("caption"))
    for position, (label, tile) in enumerate(tiles):
        column, row = position % columns, position // columns
        x = gutter + column * (cell_w + gutter)
        y = gutter + row * (cell_h + gutter)
        painter.setPen(QColor(theme.T("TextFillColorPrimary", dark=False)))
        painter.drawText(x, y + SPACING["l"], label)
        pixmap = QPixmap.fromImage(tile)
        pixmap.setDevicePixelRatio(1.0)
        painter.drawPixmap(x, y + caption_h, pixmap)
    painter.end()

    scratch = tmp_path / "wp12a-activity-center.png"
    assert sheet.save(str(scratch), "PNG")
    assert scratch.stat().st_size > 0

    try:
        CONTACT_SHEET.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(str(CONTACT_SHEET), "PNG")
    except OSError:                                      # pragma: no cover
        pytest.skip("the repository is read-only")
    assert CONTACT_SHEET.is_file()
    assert CONTACT_SHEET.stat().st_size > 10_000


def test_live_transfers_sort_above_the_history_in_the_flyout(centre, qapp):
    """The feed's whole ordering rule, seen through the window that shows it."""
    window = centre()
    centre.services.seed_activity(5)
    BUS.transfers_updated.emit(list(LIVE_TRANSFERS))
    model = window.model()
    assert model.live_count() == len(LIVE_TRANSFERS)
    assert model.row_at(0).state is ActivityState.INFLIGHT
    assert model.row_at(0).has_progress()
    assert model.row_at(len(LIVE_TRANSFERS)).state is ActivityState.DONE


@pytest.mark.slow
def test_the_activity_list_shows_no_horizontal_scrollbar(centre, qapp):
    """The delegate's `sizeHint` returns width 0; the flyout must not undo it."""
    window = centre()
    centre.services.seed_activity(80)
    window.adjust_height()
    window.show()
    qapp.processEvents()
    horizontal = window.list_view().horizontalScrollBar()
    assert horizontal.maximum() == 0
    assert not horizontal.isVisible()
    assert window.list_view().verticalScrollBar().maximum() > 0
