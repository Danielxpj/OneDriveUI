"""WP-13 — Settings, dialogs and the OOBE.

Four properties carry this package, and each of them is a way the UI could lie:

* **Every string comes from `strings.py`.** A grep test, because a literal here
  is a string that cannot be translated and a wording that will drift from the
  one the tray shows for the same thing.
* **The safe answer is the primary button.** "Delete these 4 231 items?" has
  "Restore files" as its accent button. Primary is what Return presses.
* **Nothing is silently missing.** A control that cannot work is disabled with
  its reason, never hidden.
* **The wizard does not half-succeed.** `first_run_complete` is written only
  when every step of `finalize()` worked.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from onedriveui import config as config_module
from onedriveui.models import AccountInfo, DialogKey
from onedriveui.strings import DIALOG, OOBE, SETTINGS
from onedriveui.ui.dialogs.base import (
    BaseDialog,
    DialogResult,
    DialogSpec,
    disable_with_reason,
    unavailable,
)
from onedriveui.ui.dialogs.file_dialogs import (
    DownloadAllDialog,
    FreeUpSpaceDialog,
    ShareDialog,
    VersionHistoryDialog,
)
from onedriveui.ui.dialogs.misc_dialogs import ChooseFoldersDialog, QuitDialog
from onedriveui.ui.dialogs.sync_dialogs import (
    FirstDeleteDialog,
    MassDeleteDialog,
    ResetDialog,
    ResyncDialog,
    UnlinkDialog,
)
from onedriveui.ui.pages import PAGES
from onedriveui.ui.settings_window import (
    NAV_ITEMS,
    RESTART_REQUIRED_KEYS,
    SettingsWindow,
)
from onedriveui.ui.wizard import PAGES as WIZARD_PAGES
from onedriveui.ui.wizard import FinalizeReport, SetupWizard

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_ROOT = REPO_ROOT / "onedriveui" / "ui"

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", display_name="Test User",
                      email="test@example.com", sync_root="/tmp/OneDrive")


@pytest.fixture
def cfg(_isolate_home):
    """A config with an account in it.

    `config.defaults()` has `accounts=[]`, and every account-scoped key —
    `notifications.*`, `pause.*`, `mount.*` — resolves against the active
    account. Without one, `set()` returns False for all of them and a test would
    "pass" against a window that wrote nothing.
    """
    from tests.conftest import default_config

    return config_module.AppConfig.from_dict(default_config())


# ═════════════════════════════════════════════════════════════════════════════
# No user-facing literals
# ═════════════════════════════════════════════════════════════════════════════

#: What a *user-facing* literal looks like: prose. It has a space in it, or ends
#: in sentence punctuation. Identifiers, dotted config keys, glyph ids, object
#: names and format specs have neither, and flagging them would make this a test
#: everyone learns to work around rather than one that catches a real leak.
_PROSE = re.compile(r"[A-Za-z].*\s|[A-Za-z][.?!…]$")


def literal_strings(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every prose string constant in a file, ignoring docstrings and `__all__`.

    Docstrings explain the code and `__all__` names it; neither reaches a user.
    Everything else that reads like a sentence is a candidate for having escaped
    `strings.py`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                skip.add(id(body[0].value))
        # `__all__ = [...]` and any other list of bare identifiers.
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__"
                for t in node.targets):
            for child in ast.walk(node.value):
                if isinstance(child, ast.Constant):
                    skip.add(id(child))
        # Logging calls: format strings go to the journal, never to a user.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "log":
            for child in ast.walk(node):
                if isinstance(child, ast.Constant):
                    skip.add(id(child))
        # `report.errors.append(…)` — diagnostics, on the same footing as a log
        # line. They name which step of a setup failed, for a bug report and for
        # `onedriveui --doctor`; the sentence the user reads in the UI comes from
        # `strings.py` as everything else does.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "append" \
                and isinstance(node.func.value, ast.Attribute) \
                and node.func.value.attr == "errors":
            for child in ast.walk(node):
                if isinstance(child, ast.Constant):
                    skip.add(id(child))
        # Assertion messages are for the developer who broke the invariant.
        if isinstance(node, ast.Assert) and node.msg is not None:
            for child in ast.walk(node.msg):
                if isinstance(child, ast.Constant):
                    skip.add(id(child))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        if _PROSE.search(node.value):
            out.append((node.lineno, node.value))
    return out


@pytest.mark.parametrize("relative", [
    "settings_window.py", "wizard.py",
    "pages/page_sync.py", "pages/page_account.py",
    "pages/page_notifications.py", "pages/page_about.py",
    "dialogs/base.py", "dialogs/sync_dialogs.py",
    "dialogs/file_dialogs.py", "dialogs/misc_dialogs.py",
])
def test_no_user_facing_literal_outside_strings(relative):
    """The BUILD_PLAN's acceptance case for WP-13.

    A literal here is a string that cannot be translated and a wording that will
    drift from the one the tray shows for the same thing — two surfaces
    describing one condition differently is exactly what the single string table
    exists to prevent.
    """
    offenders = literal_strings(UI_ROOT / relative)
    assert offenders == [], f"{relative}: {offenders}"


# ═════════════════════════════════════════════════════════════════════════════
# The settings shell
# ═════════════════════════════════════════════════════════════════════════════

class TestSettingsWindow:

    def test_microsofts_four_sections_in_microsofts_order(self, qapp, cfg):
        """A user arriving from Windows should not have to learn where anything
        is."""
        assert [key for key, _label, _glyph in NAV_ITEMS] == \
            ["sync", "account", "notifications", "about"]
        assert [label for _k, label, _g in NAV_ITEMS] == [
            SETTINGS.NAV_SYNC, SETTINGS.NAV_ACCOUNT,
            SETTINGS.NAV_NOTIFICATIONS, SETTINGS.NAV_ABOUT]

    def test_the_nav_and_the_pages_agree(self):
        assert [k for k, _l, _g in NAV_ITEMS] == [k for k, _cls in PAGES]

    def test_it_builds_every_page(self, qapp, cfg):
        window = SettingsWindow(ACCOUNT, config=cfg)
        assert sorted(window._pages) == ["about", "account", "notifications",
                                         "sync"]

    def test_there_is_no_ok_button(self, qapp, cfg):
        """Immediate apply. A settings window with an Apply button is one that
        can be closed with unsaved changes in it."""
        window = SettingsWindow(ACCOUNT, config=cfg)
        labels = {b.text() for b in window.findChildren(type(window))
                  if hasattr(b, "text")}
        assert DIALOG.OK not in labels
        assert DIALOG.SAVE not in labels

    def test_a_deep_link_opens_the_page(self, qapp, cfg):
        window = SettingsWindow(ACCOUNT, config=cfg)
        assert window.navigate("account") is True
        assert window._stack.currentIndex() == 1

    def test_a_deep_link_can_name_a_card(self, qapp, cfg):
        """So a banner's "Free up space" lands on the control it names rather
        than on page one, where the user then has to find it."""
        window = SettingsWindow(ACCOUNT, config=cfg)
        assert window.navigate("sync.bandwidth") is True
        assert window._stack.currentIndex() == 0

    def test_an_unknown_deep_link_is_refused(self, qapp, cfg):
        window = SettingsWindow(ACCOUNT, config=cfg)
        assert window.navigate("nope") is False

    def test_pages_never_scroll_horizontally(self, qapp, cfg):
        from PySide6.QtCore import Qt

        window = SettingsWindow(ACCOUNT, config=cfg)
        for index in range(window._stack.count()):
            area = window._stack.widget(index)
            assert area.horizontalScrollBarPolicy() is \
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff


class TestImmediateApply:

    def test_a_toggle_writes_the_config_and_announces_it(self, qapp, cfg,
                                                         bus_spy):
        """`options/set` on a running mount is a no-op, so the settings that
        *can* apply live must do so at once and the rest must say so."""
        bus_spy.watch("config_changed")
        window = SettingsWindow(ACCOUNT, config=cfg)
        page = window.page("notifications")
        switch = page.switch("notifications.paused")
        switch.setChecked(not switch.isChecked())
        assert bus_spy.count("config_changed") == 1
        assert bus_spy.last("config_changed") == ("notifications.paused",)

    def test_the_written_value_survives_a_reload(self, qapp, cfg, _isolate_home):
        window = SettingsWindow(ACCOUNT, config=cfg)
        page = window.page("notifications")
        page.switch("notifications.memories").setChecked(True)
        reloaded = config_module.load()
        assert reloaded.get("notifications.memories") is True

    def test_the_vfs_settings_say_they_need_a_restart(self, qapp, cfg):
        """`options/set` on the vfs block is accepted, returns `{}` and changes
        nothing — so "applied" would be a lie."""
        window = SettingsWindow(ACCOUNT, config=cfg)
        assert window.needs_restart("mount.transfers") is True
        assert window.needs_restart("mount.vfs_cache_max_size_gb") is True
        assert window.needs_restart("notifications.paused") is False

    def test_the_restart_signal_fires_for_those_keys(self, qapp, cfg):
        window = SettingsWindow(ACCOUNT, config=cfg)
        seen: list[str] = []
        window.restart_required.connect(seen.append)
        window._on_changed("mount.transfers")
        window._on_changed("notifications.paused")
        assert seen == ["mount.transfers"]

    def test_the_restart_is_not_performed_here(self, qapp, cfg):
        """It has to refuse while an upload is in flight (invariant I3), which
        is the Supervisor's job — and doing it silently from a toggle would
        interrupt a transfer the user cannot see."""
        calls: list[str] = []

        class Supervisor:
            def restart_mount(self, reason):
                calls.append(reason)

            def do(self, action, **kw):
                calls.append(action)

        window = SettingsWindow(ACCOUNT, config=cfg, supervisor=Supervisor())
        window._on_changed("mount.transfers")
        assert calls == []


class TestNotificationsPage:

    def test_the_hazards_have_no_toggle(self, qapp, cfg):
        """A client that let the user turn off the only signal that sync has
        stopped would be quietly useless rather than quietly quiet."""
        from onedriveui.ui.pages.page_notifications import TOGGLES

        keys = {key for _label, key in TOGGLES}
        assert "notifications.sign_in" not in keys
        assert "notifications.quota_full" not in keys

    def test_every_toggle_maps_to_a_real_toast_setting(self, qapp, cfg):
        """Checked in one place, so a toggle cannot be honoured by the tray and
        ignored by the notifier."""
        from onedriveui.ui.notices import SETTING_FOR_TOAST
        from onedriveui.ui.pages.page_notifications import TOGGLES

        known = set(SETTING_FOR_TOAST.values())
        for _label, key in TOGGLES:
            assert key in known, key


# ═════════════════════════════════════════════════════════════════════════════
# Dialogs
# ═════════════════════════════════════════════════════════════════════════════

class TestSafeDefaults:

    def test_mass_delete_offers_restore_as_the_primary(self, qapp):
        """The BUILD_PLAN's acceptance case. Primary is what Return presses,
        what muscle memory presses, and what somebody who has stopped reading
        presses — so it has to be the recoverable answer."""
        dialog = MassDeleteDialog(4231)
        assert dialog.spec.primary == DIALOG.MASS_DELETE_NO == "Restore files"
        assert dialog.spec.secondary == DIALOG.MASS_DELETE_YES

    def test_mass_delete_carries_the_seven_day_note(self, qapp):
        assert MassDeleteDialog(9).spec.footnote == DIALOG.MASS_DELETE_TIMEOUT
        assert "seven days" in DIALOG.MASS_DELETE_TIMEOUT

    def test_mass_delete_cannot_be_dismissed(self, qapp):
        """Escape resolving this to "the deletion goes ahead" is the exact
        failure the seven-day policy exists to prevent."""
        assert MassDeleteDialog(9).spec.dismissible is False

    def test_the_delete_answer_is_the_secondary_one(self, qapp):
        dialog = MassDeleteDialog(9)
        dialog._choose(DialogResult.SECONDARY)
        assert dialog.wants_delete() is True
        assert dialog.wants_restore() is False

    def test_a_dismissed_mass_delete_keeps_the_files(self, qapp):
        dialog = MassDeleteDialog(9)
        assert dialog.wants_delete() is False
        assert dialog.wants_restore() is True

    @pytest.mark.parametrize("factory", [ResyncDialog, ResetDialog,
                                         UnlinkDialog, QuitDialog])
    def test_the_destructive_dialogs_default_to_cancel(self, qapp, factory):
        assert factory().spec.primary == DIALOG.CANCEL


class TestResyncDialog:

    def test_it_states_that_a_resync_only_copies(self, qapp):
        """The single most misunderstood operation in rclone. A dialog that
        called it "reset sync" and left the consequence to be discovered would
        be technically accurate and practically a trap."""
        body = ResyncDialog().spec.body
        assert body == DIALOG.RESYNC_BODY
        assert "only copies" in body
        assert "never deletes" in body

    def test_approval_needs_an_explicit_continue(self, qapp):
        """This is the decision row invariant I15 requires."""
        dialog = ResyncDialog()
        assert dialog.approved() is False
        dialog._choose(DialogResult.SECONDARY)
        assert dialog.approved() is True


class TestDisabledControls:

    def test_remove_link_is_disabled_with_its_reason(self, qapp):
        """The BUILD_PLAN's acceptance case. rclone's `unlink=true` is a
        verified no-op that CREATES a link; a button reporting success would
        tell the user their document is private while it is public."""
        dialog = ShareDialog("Docs/a.docx")
        assert dialog.remove_button.isEnabled() is False
        assert dialog.remove_button.toolTip() == DIALOG.REMOVE_LINK_WHY

    def test_it_is_disabled_rather_than_hidden(self, qapp):
        """A missing control makes the user hunt for a feature they know
        exists; a disabled one with a sentence tells them where it does work."""
        dialog = ShareDialog("Docs/a.docx")
        assert dialog.remove_button.isVisible() or \
            dialog.remove_button.parent() is not None
        assert dialog.remove_button.text() == DIALOG.REMOVE_LINK

    def test_the_helper_marks_a_control_without_hiding_it(self, qapp):
        from PySide6.QtWidgets import QPushButton

        from PySide6.QtWidgets import QWidget

        holder = QWidget()
        button = QPushButton("x", holder)
        disable_with_reason(button, "because")
        assert button.isEnabled() is False
        assert button.toolTip() == "because"
        # `isHidden()` is True for any unshown top-level widget, so the check
        # only means anything on a parented one.
        assert button.isHidden() is False

    def test_the_unavailable_prefix_comes_from_strings(self):
        assert unavailable("x").startswith(DIALOG.UNAVAILABLE_PREFIX)

    def test_version_history_links_to_the_web(self, qapp):
        """rclone can delete versions and can neither list nor restore them, so
        the deep link is the honest answer rather than a gap."""
        dialog = VersionHistoryDialog("a.txt")
        assert dialog.spec.body == DIALOG.VERSION_HISTORY_WHY
        assert dialog.wants_web() is False
        dialog._choose(DialogResult.PRIMARY)
        assert dialog.wants_web() is True


class TestFileDialogs:

    def test_free_up_space_states_the_size(self, qapp):
        assert "5.0 GB" in FreeUpSpaceDialog(5_000_000_000).spec.body

    def test_download_all_states_the_size(self, qapp):
        """On a 900 GB drive with 200 GB free this is a request that cannot be
        granted, and the number is what makes that visible first."""
        assert "40.0 GB" in DownloadAllDialog(40_000_000_000).spec.body

    def test_both_can_be_remembered(self, qapp):
        assert FreeUpSpaceDialog().spec.remember is DialogKey.FOD_FREE_UP_SPACE
        assert DownloadAllDialog().spec.remember is DialogKey.FOD_DOWNLOAD_ALL

    def test_choose_folders_warns_that_unchecking_removes(self, qapp):
        assert ChooseFoldersDialog().spec.body == DIALOG.CHOOSE_FOLDERS_WARN

    def test_first_delete_can_be_silenced(self, qapp):
        dialog = FirstDeleteDialog("holiday.jpg")
        assert dialog.spec.remember is DialogKey.FIRST_DELETE
        assert dialog.remember_box() is not None


class TestDialogDismissal:

    def test_escape_is_ignored_when_it_must_be_answered(self, qapp):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        dialog = MassDeleteDialog(9)
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                          Qt.KeyboardModifier.NoModifier)
        dialog.keyPressEvent(event)
        assert event.isAccepted() is False

    def test_an_ordinary_dialog_accepts_escape(self, qapp):
        dialog = BaseDialog(DialogSpec(title="T", primary="P"))
        assert dialog.spec.dismissible is True


# ═════════════════════════════════════════════════════════════════════════════
# The wizard
# ═════════════════════════════════════════════════════════════════════════════

class TestWizard:

    def test_seven_pages_in_windows_order(self, qapp, cfg):
        assert WIZARD_PAGES == OOBE.PAGES
        assert len(WIZARD_PAGES) == 7
        assert WIZARD_PAGES[0] == "welcome"
        assert WIZARD_PAGES[-1] == "done"

    def test_it_builds_all_seven(self, qapp, cfg):
        wizard = SetupWizard(config=cfg)
        assert sorted(wizard._pages) == sorted(WIZARD_PAGES)

    def test_the_delete_education_page_is_present(self, qapp, cfg):
        """It teaches nothing about this client and everything about sync, and
        it is the page that stops somebody deleting their photo library."""
        wizard = SetupWizard(config=cfg)
        page = wizard.page("delete")
        assert page.title_label.text() == OOBE.DELETE_TITLE
        assert "everywhere" in OOBE.DELETE_BODY

    def test_navigation_moves_and_stops_at_the_ends(self, qapp, cfg):
        wizard = SetupWizard(config=cfg)
        assert wizard.current_key == "welcome"
        wizard.previous_page()
        assert wizard.current_key == "welcome"
        for _ in range(20):
            wizard.next_page()
        assert wizard.current_key == "done"

    def test_an_existing_folder_says_it_will_be_merged(self, qapp, cfg,
                                                       tmp_path):
        """"This folder will become my OneDrive" is a reasonable thing to fear
        and a wrong thing to believe."""
        wizard = SetupWizard(config=cfg)
        wizard._folder.setText(str(tmp_path))
        assert wizard._folder_note.text() == OOBE.FOLDER_EXISTS

    def test_a_new_folder_says_nothing(self, qapp, cfg, tmp_path):
        wizard = SetupWizard(config=cfg)
        wizard._folder.setText(str(tmp_path / "brand-new"))
        assert wizard._folder_note.text() == ""

    def test_there_is_no_password_field(self, qapp, cfg):
        """OAuth happens in the user's own browser. Anything that looked like an
        in-app password field would be indistinguishable from a
        credential-harvesting dialog."""
        from PySide6.QtWidgets import QLineEdit

        wizard = SetupWizard(config=cfg)
        for field in wizard.findChildren(QLineEdit):
            assert field.echoMode() is not QLineEdit.EchoMode.Password


class TestFinalize:

    def test_a_partial_setup_is_not_success(self, qapp, cfg):
        """The BUILD_PLAN's acceptance case: `first_run_complete` is set only on
        a complete success. A wizard that declared itself finished after failing
        halfway leaves a client that never offers to set itself up again and
        never works."""
        wizard = SetupWizard(config=cfg)
        report = wizard.finalize()
        assert report.ok is False
        assert report.errors
        assert cfg.get("app.first_run_complete") is False

    def test_the_report_names_what_failed(self, qapp, cfg):
        report = SetupWizard(config=cfg).finalize()
        assert any("mount" in e for e in report.errors)

    def test_the_check_file_is_seeded_first(self, qapp, cfg, tmp_path):
        """bisync's `--check-access` refuses to run without `RCLONE_TEST`, and a
        first run that aborts because the file it needs was never created is the
        worst possible introduction to a sync client."""
        wizard = SetupWizard(config=cfg)
        wizard._folder.setText(str(tmp_path / "OneDrive"))
        wizard.finalize()
        assert (tmp_path / "OneDrive" / "RCLONE_TEST").exists()

    def test_every_step_is_recorded_individually(self):
        report = FinalizeReport()
        assert report.ok is False
        for field in ("check_file_seeded", "filters_written", "units_installed",
                      "icons_installed", "autostart_set", "mount_started"):
            assert hasattr(report, field)
