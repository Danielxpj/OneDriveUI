"""WP-12b — `ui/tray.py`, `ui/notices.py`, `ui/filebrowser.py`.

StatusNotifierItem is much less capable than `QSystemTrayIcon`'s API suggests,
and each of its restrictions is a bug that looks like a Qt problem until you know
it is not. These tests pin all four:

* icons go out as **names**, never pixmaps — a pixmap shows nothing;
* the menu is **labels only** — `QWidgetAction` exports as a blank clickable row;
* **"Open Activity Center" is first and default** — the AppIndicator extension
  maps left-click to the menu, so `activated` never arrives;
* the menu is **rebuilt, not mutated** — toggling visibility reflows the DBusMenu
  and nests "Quit" under "Pause syncing".

Plus the rule that keeps notifications bearable: a toast is for a *change* and a
banner is for a *condition*.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from onedriveui.constants import SPINNER_FRAME_MS
from onedriveui.models import (
    AccountInfo,
    AccountKind,
    Facts,
    FileState,
    IssueCode,
    IssueSeverity,
    NotificationId,
    PauseReason,
    QuotaInfo,
    RecoveryAction,
    SyncIssue,
    SyncState,
    TrayIcon,
    VaultState,
)
from onedriveui.strings import MENU
from onedriveui.ui import icons
from onedriveui.ui.filebrowser import COLUMNS, PATH_ROLE, FileBrowser
from onedriveui.ui.notices import (
    SETTING_FOR_TOAST,
    TOAST_FOR_STATE,
    NoticeCenter,
)
from onedriveui.ui.tray import SPINNING_STATES, TrayItem, available

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", display_name="Test User",
                      sync_root="/tmp/OneDrive")
BUSINESS = AccountInfo(id="work", remote="work", kind=AccountKind.BUSINESS,
                       sync_root="/tmp/Work")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


class RecordingSupervisor:
    def __init__(self):
        self.actions: list[tuple[RecoveryAction, dict]] = []
        self.calls: list[tuple[str, tuple]] = []

    def do(self, action, **kw):
        self.actions.append((action, dict(kw)))

    def request_pause(self, reason, hours):
        self.calls.append(("pause", (reason, hours)))

    def request_resume(self):
        self.calls.append(("resume", ()))


class RecordingNotifier:
    def __init__(self):
        self.toasts: list[NotificationId] = []

    def toast(self, nid, *, account_id="", **fmt):
        self.toasts.append(nid)
        return 1


def facts(**kw) -> Facts:
    base = dict(account_id=ACCOUNT.id, account_configured=True,
                quota=QuotaInfo(total=1_000, used=10, free=990))
    base.update(kw)
    return Facts(**base)


def menu_labels(tray: TrayItem) -> list[str]:
    return [a.text() for a in tray._menu.actions() if not a.isSeparator()]


# ═════════════════════════════════════════════════════════════════════════════
# The tray icon
# ═════════════════════════════════════════════════════════════════════════════

class TestTrayIcon:

    def test_availability_is_safe_before_a_qapplication(self):
        """`isSystemTrayAvailable()` SEGFAULTS without one — not raises,
        segfaults — and start-up code naturally asks early."""
        assert available() in (True, False)

    def test_every_state_paints_something(self, qapp):
        """All 17 reachable states, through the single TRAY_FOR_STATE map."""
        tray = TrayItem(ACCOUNT)
        for state in set(SyncState) - {SyncState.NOT_RUNNING}:
            tray.set_state(state, facts())
            assert not tray._item.icon().isNull() or state in SPINNING_STATES

    def test_not_running_registers_no_item(self, qapp):
        """The ABSENCE of an icon is what "OneDrive is not running" looks like;
        a grey icon claiming to represent a client that is not there is worse."""
        tray = TrayItem(ACCOUNT)
        tray.set_state(SyncState.NOT_RUNNING, facts())
        assert tray.visible is False

    def test_icons_are_requested_by_name_never_as_a_pixmap(self):
        """SNI transmits an IconName string; `setIcon(QPixmap(...))` compiles,
        runs and shows nothing. Checked with the AST so the rule survives edits."""
        source = (REPO_ROOT / "onedriveui" / "ui" / "tray.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in ("QPixmap", "QImage"):
                pytest.fail(f"tray.py builds a pixmap at line {node.lineno}")

    def test_business_gets_its_own_healthy_icon(self, qapp):
        """A user with both accounts tells them apart in the tray by this."""
        personal = TrayItem(ACCOUNT)
        work = TrayItem(BUSINESS)
        personal.set_state(SyncState.UP_TO_DATE, facts())
        work.set_state(SyncState.UP_TO_DATE, facts(account_id=BUSINESS.id))
        assert icons.tray_icon_name(TrayIcon.SYNCED) != \
            icons.tray_icon_name(TrayIcon.SYNCED_BIZ)


class TestSpinner:

    def test_it_runs_only_while_something_is_happening(self, qapp):
        tray = TrayItem(ACCOUNT)
        tray.set_state(SyncState.SYNCING, facts(transfers_active=2))
        assert tray._spinner.isActive() is True

    def test_it_stops_when_the_state_settles(self, qapp):
        """An animation nobody watches still wakes the compositor eight times a
        second."""
        tray = TrayItem(ACCOUNT)
        tray.set_state(SyncState.SYNCING, facts(transfers_active=2))
        tray.set_state(SyncState.UP_TO_DATE, facts())
        assert tray._spinner.isActive() is False

    def test_it_stops_on_hide(self, qapp):
        tray = TrayItem(ACCOUNT)
        tray.set_state(SyncState.SYNCING, facts(transfers_active=1))
        tray.hide()
        assert tray._spinner.isActive() is False

    def test_the_frame_rate_is_the_documented_125_ms(self, qapp):
        assert TrayItem(ACCOUNT)._spinner.interval() == SPINNER_FRAME_MS
        assert SPINNER_FRAME_MS * len(icons.SPINNER_FRAMES) == 1000

    def test_the_frames_wrap(self, qapp):
        tray = TrayItem(ACCOUNT)
        tray.set_state(SyncState.SYNCING, facts(transfers_active=1))
        for _ in range(len(icons.SPINNER_FRAMES)):
            tray._advance_spinner()
        assert tray._frame == 0

    def test_every_busy_state_spins(self, qapp):
        tray = TrayItem(ACCOUNT)
        for state in SPINNING_STATES:
            tray.set_state(state, facts())
            assert tray._spinner.isActive() is True, state


class TestMenu:

    def test_open_activity_center_is_first_and_default(self, qapp):
        """The AppIndicator extension maps BOTH buttons to the menu — `activated`
        with Trigger never arrives — so the first item is what a left-click was
        going to mean."""
        tray = TrayItem(ACCOUNT)
        assert menu_labels(tray)[0] == MENU.OPEN_ACTIVITY
        assert tray._menu.defaultAction().text() == MENU.OPEN_ACTIVITY

    def test_the_activated_signal_is_never_relied_on(self):
        """It does not arrive under the AppIndicator extension, and relying on
        it produces an icon that appears to do nothing when clicked."""
        source = (REPO_ROOT / "onedriveui" / "ui" / "tray.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "activated":
                if isinstance(node.value, ast.Attribute) and \
                        node.value.attr == "_item":
                    pytest.fail("tray.py connects QSystemTrayIcon.activated")

    def test_the_menu_carries_no_widget_actions(self, qapp):
        """`QWidgetAction` exports as an empty, clickable, blank row — not as a
        widget and not as nothing."""
        from PySide6.QtWidgets import QWidgetAction

        tray = TrayItem(ACCOUNT)
        tray.set_state(SyncState.SYNCING, facts(transfers_active=3))
        for action in tray._menu.actions():
            assert not isinstance(action, QWidgetAction)

    def test_every_label_comes_from_strings(self, qapp):
        tray = TrayItem(ACCOUNT)
        known = {v for k, v in vars(MENU).items() if not k.startswith("_")}
        for label in menu_labels(tray):
            assert label in known or label.startswith("View sync problems")

    def test_pausing_and_resuming_swap_places(self, qapp):
        tray = TrayItem(ACCOUNT)
        assert MENU.PAUSE in menu_labels(tray)
        tray.set_state(SyncState.PAUSED_MANUAL, facts())
        labels = menu_labels(tray)
        assert MENU.RESUME in labels
        assert MENU.PAUSE not in labels

    def test_the_menu_is_rebuilt_not_mutated(self, qapp):
        """Toggling an exported action's visibility reflows the DBusMenu — the
        observed symptom is "Quit" nesting under "Pause syncing"."""
        tray = TrayItem(ACCOUNT)
        first = list(tray._menu.actions())
        tray.set_state(SyncState.PAUSED_MANUAL, facts())
        assert list(tray._menu.actions()) != first
        # Quit is still a top-level item, not a child of anything.
        assert menu_labels(tray)[-1] == MENU.QUIT
        assert tray._menu.actions()[-1].menu() is None

    def test_quit_stays_top_level_through_every_state(self, qapp):
        """The reflow bug's actual symptom, checked across all of them."""
        tray = TrayItem(ACCOUNT)
        for state in set(SyncState) - {SyncState.NOT_RUNNING}:
            tray.set_state(state, facts(issues_error=2))
            assert menu_labels(tray)[-1] == MENU.QUIT
            assert tray._menu.actions()[-1].menu() is None

    def test_the_issue_count_appears_only_when_there_are_issues(self, qapp):
        tray = TrayItem(ACCOUNT)
        assert not any("sync problems" in x.lower() for x in menu_labels(tray))
        tray.set_state(SyncState.WARNING, facts(issues_error=3))
        assert any("(3)" in x for x in menu_labels(tray))

    def test_the_vault_item_appears_only_with_a_vault(self, qapp):
        class LockedVault:
            def state(self):
                return VaultState.LOCKED

            def unlock(self):
                pass

        assert MENU.UNLOCK_VAULT not in menu_labels(TrayItem(ACCOUNT))
        tray = TrayItem(ACCOUNT, vault=LockedVault())
        assert MENU.UNLOCK_VAULT in menu_labels(tray)

    def test_an_absent_vault_shows_nothing(self, qapp):
        class NoVault:
            def state(self):
                return VaultState.ABSENT

        tray = TrayItem(ACCOUNT, vault=NoVault())
        assert MENU.UNLOCK_VAULT not in menu_labels(tray)
        assert MENU.LOCK_VAULT not in menu_labels(tray)

    def test_a_broken_vault_does_not_break_the_menu(self, qapp):
        class Exploding:
            def state(self):
                raise RuntimeError("gocryptfs went away")

        tray = TrayItem(ACCOUNT, vault=Exploding())
        assert menu_labels(tray)[-1] == MENU.QUIT


class TestTrayActions:

    def test_pause_goes_through_the_supervisor(self, qapp):
        supervisor = RecordingSupervisor()
        tray = TrayItem(ACCOUNT, supervisor=supervisor)
        tray._pause(8)
        assert supervisor.calls == [("pause", (PauseReason.MANUAL, 8))]

    def test_open_folder_goes_through_do(self, qapp):
        """The same entry point as the Nautilus submenu and the file browser, so
        a guard added once covers all three."""
        supervisor = RecordingSupervisor()
        tray = TrayItem(ACCOUNT, supervisor=supervisor)
        tray._open_folder()
        assert supervisor.actions[0][0] is RecoveryAction.SHOW_IN_FOLDER

    def test_no_ui_file_calls_a_service_directly(self):
        """Every action reaches `Supervisor.do()` or a `BUS` signal."""
        banned = ("rc.vfs", "rc.ops", "rc.bisync", "data.repo_sync",
                  "data.repo_files")
        offenders = []
        for name in ("tray.py", "notices.py", "filebrowser.py"):
            text = (REPO_ROOT / "onedriveui" / "ui" / name).read_text(
                encoding="utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for prefix in banned:
                        if node.module.endswith(prefix):
                            offenders.append(f"{name}:{node.lineno} {node.module}")
        assert offenders == []

    def test_a_missing_supervisor_is_a_no_op(self, qapp):
        tray = TrayItem(ACCOUNT)
        tray._open_folder()
        tray._pause(2)
        tray._resume()


# ═════════════════════════════════════════════════════════════════════════════
# Notices
# ═════════════════════════════════════════════════════════════════════════════

class TestNotices:

    def centre(self, **kw) -> NoticeCenter:
        kw.setdefault("notifier", RecordingNotifier())
        return NoticeCenter(ACCOUNT, **kw)

    def test_a_toast_is_for_a_change_not_a_condition(self, qapp):
        """Toasting a condition every tick is how a notification system becomes
        something the user turns off."""
        notifier = RecordingNotifier()
        centre = self.centre(notifier=notifier)
        full = facts(quota=QuotaInfo(total=1_000, used=1_000, free=0))
        centre._on_state_changed(SyncState.UP_TO_DATE, SyncState.PAUSED_QUOTA, full)
        centre._on_state_changed(SyncState.PAUSED_QUOTA, SyncState.PAUSED_QUOTA, full)
        assert notifier.toasts == [NotificationId.QUOTA_FULL]

    def test_a_condition_gets_a_banner_for_as_long_as_it_lasts(self, qapp):
        centre = self.centre()
        full = facts(quota=QuotaInfo(total=1_000, used=1_000, free=0))
        centre._on_state_changed(SyncState.UP_TO_DATE, SyncState.PAUSED_QUOTA, full)
        assert centre.banner() is not None
        assert centre.banner().code is IssueCode.QUOTA_EXCEEDED

    def test_the_settings_toggles_are_honoured_in_one_place(self, qapp):
        """A toggle cannot be obeyed by the tray and ignored by the notifier if
        there is only one place it is read."""
        notifier = RecordingNotifier()
        centre = self.centre(notifier=notifier,
                             config_get=lambda key, default=None: False)
        assert centre.raise_toast(NotificationId.SYNC_ISSUES) is False
        assert notifier.toasts == []

    def test_a_hazard_has_no_toggle(self, qapp):
        """Sign-in-required is not optional: nothing syncs until it is fixed."""
        assert NotificationId.SIGN_IN_REQUIRED not in SETTING_FOR_TOAST
        centre = self.centre(config_get=lambda key, default=None: False)
        assert centre.enabled(NotificationId.SIGN_IN_REQUIRED) is True

    def test_sync_complete_only_follows_something_syncing(self, qapp):
        """Otherwise it fires on start-up and after every transient hiccup."""
        notifier = RecordingNotifier()
        centre = self.centre(notifier=notifier)
        centre._on_state_changed(SyncState.OFFLINE, SyncState.UP_TO_DATE, facts())
        assert notifier.toasts == []
        centre._on_state_changed(SyncState.SYNCING, SyncState.UP_TO_DATE, facts())
        assert notifier.toasts == [NotificationId.SYNC_COMPLETE]

    def test_errors_banner_beneath_a_busy_state(self, qapp):
        """Both facts are visible at once, which is what Windows does and the
        only arrangement in which neither hides the other."""
        centre = self.centre()
        notice = centre.banner_for(SyncState.SYNCING, facts(transfers_active=2,
                                                            issues_error=3))
        assert notice is not None
        assert RecoveryAction.RETRY in [a for a, _label in notice.actions]

    def test_a_full_drive_cannot_be_dismissed(self, qapp):
        """Closing it would leave a client that looks healthy and silently syncs
        nothing — the notice is the only remaining signal."""
        centre = self.centre()
        full = facts(quota=QuotaInfo(total=1_000, used=1_000, free=0))
        centre.set_banner(centre.banner_for(SyncState.PAUSED_QUOTA, full))
        assert centre.dismiss() is False
        assert centre.banner() is not None

    def test_an_ordinary_notice_can_be_dismissed(self, qapp):
        centre = self.centre()
        centre.set_banner(centre.banner_for(SyncState.WARNING,
                                            facts(issues_error=1)))
        assert centre.dismiss() is True
        assert centre.banner() is None

    def test_a_fresh_occurrence_reopens_a_dismissed_notice(self, qapp):
        """The user dismissed a previous occurrence; a new one is new
        information."""
        centre = self.centre()
        centre.set_banner(centre.banner_for(SyncState.WARNING,
                                            facts(issues_error=1)))
        centre.dismiss()
        centre._on_issue_raised(SyncIssue(
            account_id=ACCOUNT.id, code=IssueCode.UPLOAD_FAILED,
            severity=IssueSeverity.ERROR, title="x"))
        centre.set_banner(centre.banner_for(SyncState.WARNING,
                                            facts(issues_error=1)))
        assert centre.banner() is not None

    def test_an_automatic_pause_toasts_without_a_banner(self, qapp):
        """The toast carries "Sync Anyway"; a banner would say it twice."""
        centre = self.centre()
        assert centre.banner_for(SyncState.PAUSED_METERED,
                                 facts(policy_pause=PauseReason.METERED)) is None
        assert TOAST_FOR_STATE[SyncState.PAUSED_METERED] is \
            NotificationId.SYNC_PAUSED_METERED

    def test_a_toast_action_routes_through_do(self, qapp):
        supervisor = RecordingSupervisor()
        centre = self.centre(supervisor=supervisor)
        centre._on_toast_action("quota_full", RecoveryAction.FREE_UP_SPACE.value)
        assert supervisor.actions[0][0] is RecoveryAction.FREE_UP_SPACE

    def test_an_unknown_toast_action_is_ignored(self, qapp):
        supervisor = RecordingSupervisor()
        centre = self.centre(supervisor=supervisor)
        centre._on_toast_action("x", "not-an-action")
        assert supervisor.actions == []

    def test_every_notice_title_comes_from_strings(self, qapp):
        from onedriveui.strings import ISSUE_TITLE

        centre = self.centre()
        titles = {t for t in ISSUE_TITLE.values()}
        for state in set(SyncState) - {SyncState.NOT_RUNNING}:
            notice = centre.banner_for(state, facts(issues_error=1))
            if notice is None:
                continue
            template = notice.title
            assert any(template == t or "{" in t for t in titles) or \
                notice.code is IssueCode.ORPHANED_CACHE


# ═════════════════════════════════════════════════════════════════════════════
# The file browser
# ═════════════════════════════════════════════════════════════════════════════

class TestFileBrowser:

    @pytest.fixture
    def tree(self, tmp_path) -> AccountInfo:
        root = tmp_path / "OneDrive"
        (root / "Photos").mkdir(parents=True)
        (root / "Photos" / "a.jpg").write_bytes(b"x" * 100)
        (root / "notes.txt").write_text("hello")
        return AccountInfo(id="onedrive", remote="onedrive", sync_root=str(root))

    def test_it_shows_explorers_columns(self, qapp, tree):
        browser = FileBrowser(tree)
        assert [browser.model.horizontalHeaderItem(i).text()
                for i in range(len(COLUMNS))] == list(COLUMNS)

    def test_folders_sort_before_files(self, qapp, tree):
        browser = FileBrowser(tree)
        assert [browser.model.item(i, 0).text() for i in range(2)] == \
            ["Photos", "notes.txt"]

    def test_a_folder_is_not_read_until_it_is_expanded(self, qapp, tree):
        """One Graph request per directory is what a recursive read costs on a
        backend with ListR = false."""
        browser = FileBrowser(tree)
        photos = browser.model.item(0, 0)
        assert photos.rowCount() == 1
        assert photos.child(0).data(PATH_ROLE) is None      # a placeholder

    def test_expanding_populates_it(self, qapp, tree):
        browser = FileBrowser(tree)
        photos = browser.model.item(0, 0)
        browser._on_expanded(photos.index())
        assert [photos.child(i, 0).text() for i in range(photos.rowCount())] == \
            ["a.jpg"]

    def test_the_status_column_is_one_query_per_directory(self, qapp, tree):
        """Not one per row: a per-row lookup makes scrolling a large folder
        visibly stutter, on the thread that is drawing it."""
        calls: list[int] = []

        class Counting:
            def statuses(self, paths):
                calls.append(len(paths))
                return {p: type("S", (), {"state": FileState.LOCAL})()
                        for p in paths}

        FileBrowser(tree, filestate=Counting())
        assert calls == [2]

    def test_the_status_text_comes_from_strings(self, qapp, tree):
        from onedriveui.strings import FILE_STATE_LABEL

        class Local:
            def statuses(self, paths):
                return {p: type("S", (), {"state": FileState.LOCAL})()
                        for p in paths}

        browser = FileBrowser(tree, filestate=Local())
        assert browser.model.item(0, 1).text() == \
            FILE_STATE_LABEL[FileState.LOCAL.value]

    def test_an_unknown_state_shows_no_label_rather_than_a_guess(self, qapp, tree):
        browser = FileBrowser(tree)
        assert browser.model.item(0, 1).text() == ""

    def test_actions_go_through_do(self, qapp, tree):
        supervisor = RecordingSupervisor()
        browser = FileBrowser(tree, supervisor=supervisor)
        browser._act(RecoveryAction.FREE_UP_SPACE, ["notes.txt"])
        assert supervisor.actions[0][0] is RecoveryAction.FREE_UP_SPACE
        assert supervisor.actions[0][1]["rel_path"] == "notes.txt"

    def test_a_batch_acts_on_every_selected_path(self, qapp, tree):
        supervisor = RecordingSupervisor()
        browser = FileBrowser(tree, supervisor=supervisor)
        browser._act(RecoveryAction.FREE_UP_SPACE, ["a", "b", "c"])
        assert len(supervisor.actions) == 3

    def test_there_is_no_horizontal_scrollbar(self, qapp, tree):
        """A horizontal scrollbar in a file list is a layout bug the user has to
        work around."""
        from PySide6.QtCore import Qt as _Qt

        browser = FileBrowser(tree)
        assert browser.view.horizontalScrollBarPolicy() is \
            _Qt.ScrollBarPolicy.ScrollBarAlwaysOff

    def test_rows_are_uniform_height_for_virtualisation(self, qapp, tree):
        """Which is what lets the view scroll 5 000 rows without measuring each."""
        assert FileBrowser(tree).view.uniformRowHeights() is True

    def test_a_missing_folder_is_not_a_crash(self, qapp, tmp_path):
        account = AccountInfo(id="x", remote="x",
                              sync_root=str(tmp_path / "gone"))
        assert FileBrowser(account).row_count() == 0
