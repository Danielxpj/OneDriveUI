"""The seven-page setup, and the one thing it must not do.

Windows' OOBE is seven pages — welcome, sign in, folder, backup, delete
education, tutorial, done — and this reproduces them because a user arriving
from Windows already knows what comes next.

The page that matters most is the fifth, "Deleting files removes them
everywhere". It teaches nothing about this client and everything about sync,
and it is the page that stops somebody discovering the semantics of a sync
folder by deleting their photo library from it.

**``finalize()`` is a transaction, and it does not half-succeed.** The last step
seeds ``RCLONE_TEST``, writes the filters file, installs the units and icons,
installs the Nautilus extension, sets autostart and starts the mount — and only
sets ``first_run_complete`` when every one of those worked. A wizard that
marked itself done after failing halfway leaves a client that never offers to
set itself up again and never works, which is the worst of both.

The folder page has a quieter trap. Choosing an existing directory is normal —
most people point this at a folder they already have — and its contents are
**merged**, not replaced. The page says so, because the alternative reading
("this folder will become my OneDrive") is a reasonable thing to fear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from onedriveui.constants import WIZARD_H, WIZARD_W
from onedriveui.strings import DIALOG, OOBE
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.controls import ButtonVariant, FluentButton

log = logging.getLogger(__name__)

__all__ = ["SetupWizard", "WizardPage", "PAGES", "FinalizeReport"]

#: The seven pages, in Windows' order. `strings.OOBE.PAGES` names the same
#: sequence; a test asserts the two agree.
PAGES: Final[tuple[str, ...]] = OOBE.PAGES


@dataclass(slots=True)
class FinalizeReport:
    """What the last step managed to do.

    Every step is recorded rather than collapsed into a bool, because
    ``first_run_complete`` is only set when all of them worked — and when one
    did not, the user has to be told which.
    """

    check_file_seeded: bool = False
    filters_written: bool = False
    units_installed: bool = False
    icons_installed: bool = False
    extension_installed: bool = False
    autostart_set: bool = False
    mount_started: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True only when every step succeeded.

        Deliberately strict. A wizard that declared itself finished after a
        partial setup leaves a client that will never offer to set itself up
        again and never works — which is worse than one that says it failed.
        """
        return not self.errors and all((
            self.check_file_seeded, self.filters_written, self.units_installed,
            self.icons_installed, self.autostart_set, self.mount_started,
        ))


class WizardPage(QWidget):
    """One page: a title, a body, and whatever it needs in between."""

    def __init__(self, key: str, title: str, body: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(SPACING["l"], SPACING["l"],
                                       SPACING["l"], SPACING["l"])
        self.column.setSpacing(SPACING["m"])

        self.title_label = QLabel(title, self)
        self.title_label.setWordWrap(True)
        self.column.addWidget(self.title_label)

        self.body_label = QLabel(body, self)
        self.body_label.setWordWrap(True)
        self.body_label.setVisible(bool(body))
        self.column.addWidget(self.body_label)
        self.column.addStretch(1)

    def can_advance(self) -> bool:
        """Whether Next should be enabled. Most pages always can."""
        return True


class SetupWizard(QWidget):
    """The seven-page first run.

    Args:
        account: The account being set up, or ``None`` before sign-in.
        config: The loaded config.
        services: The engine's services, for :meth:`finalize`.
        parent: Qt parent.

    Signals:
        finished: The :class:`FinalizeReport` from the last step.
        sign_in_requested: The user pressed "Sign in".
    """

    finished = Signal(object)
    sign_in_requested = Signal()

    def __init__(self, account: Any = None, *, config: Any = None,
                 services: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self._config = config
        self._services = dict(services or {})
        self._pages: dict[str, WizardPage] = {}

        self.setWindowTitle(OOBE.WELCOME_TITLE)
        self.resize(WIZARD_W, WIZARD_H)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self._stack = QStackedWidget(self)
        column.addWidget(self._stack, 1)
        column.addWidget(self._build_buttons())

        for key in PAGES:
            page = self._build_page(key)
            self._pages[key] = page
            self._stack.addWidget(page)
        self._stack.setCurrentIndex(0)
        self._sync_buttons()

    # ═════════════════════════════════════════════════════════════════════════
    # Pages
    # ═════════════════════════════════════════════════════════════════════════

    def _build_page(self, key: str) -> WizardPage:
        builder = getattr(self, f"_page_{key}", None)
        if builder is None:  # pragma: no cover - PAGES is frozen
            return WizardPage(key, key.title(), parent=self)
        return builder()

    def _page_welcome(self) -> WizardPage:
        page = WizardPage("welcome", OOBE.WELCOME_TITLE, OOBE.WELCOME_BODY, self)
        self._email = QLineEdit(page)
        self._email.setPlaceholderText(OOBE.WELCOME_EMAIL)
        page.column.insertWidget(2, self._email)
        return page

    def _page_signin(self) -> WizardPage:
        """OAuth happens in the user's browser, and the page says so.

        rclone's authorize flow opens a real browser against Microsoft's own
        sign-in. There is no in-app password field and there should not be:
        anything that looked like one would be indistinguishable from a
        credential-harvesting dialog.
        """
        page = WizardPage("signin", OOBE.SIGNIN_BTN, OOBE.SIGNIN_BROWSER, self)
        button = FluentButton(OOBE.SIGNIN_BTN, page, variant=ButtonVariant.ACCENT)
        button.clicked.connect(self.sign_in_requested.emit)
        page.column.insertWidget(2, button)
        self._signin_status = QLabel("", page)
        page.column.insertWidget(3, self._signin_status)
        return page

    def _page_folder(self) -> WizardPage:
        """Where the OneDrive folder goes — and what happens if it exists.

        Pointing this at an existing directory is the normal case, and its
        contents are **merged** rather than replaced. Saying so is the whole
        reason the page has a third line: "this folder will become my OneDrive"
        is a reasonable thing to fear and a wrong thing to believe.
        """
        page = WizardPage("folder", OOBE.FOLDER_TITLE, OOBE.FOLDER_BODY, self)
        self._folder = QLineEdit(page)
        self._folder.setText(str(Path.home() / "OneDrive"))
        self._folder.textChanged.connect(self._on_folder_changed)
        page.column.insertWidget(2, QLabel(OOBE.FOLDER_LOCATION, page))
        page.column.insertWidget(3, self._folder)
        self._folder_note = QLabel("", page)
        self._folder_note.setWordWrap(True)
        page.column.insertWidget(4, self._folder_note)
        self._on_folder_changed(self._folder.text())
        return page

    def _on_folder_changed(self, text: str) -> None:
        exists = bool(text) and Path(text).expanduser().is_dir()
        self._folder_note.setText(OOBE.FOLDER_EXISTS if exists else "")

    def _page_backup(self) -> WizardPage:
        page = WizardPage("backup", OOBE.BACKUP_TITLE, OOBE.BACKUP_BODY, self)
        later = FluentButton(OOBE.BACKUP_LATER, page,
                             variant=ButtonVariant.SUBTLE)
        later.clicked.connect(self.next_page)
        page.column.insertWidget(2, later)
        return page

    def _page_delete(self) -> WizardPage:
        """The page that stops somebody deleting their photo library.

        It teaches nothing about this client and everything about sync: a file
        removed from the OneDrive folder is removed from every device signed in
        to that account. Windows shows it, and the reason it is worth a whole
        page is that the mistake it prevents is unrecoverable in the moment and
        obvious only afterwards.
        """
        return WizardPage("delete", OOBE.DELETE_TITLE, OOBE.DELETE_BODY, self)

    def _page_tutorial(self) -> WizardPage:
        page = WizardPage("tutorial", OOBE.TUTORIAL_TITLE, parent=self)
        for slide in OOBE.TUTORIAL_SLIDES:
            label = QLabel(slide, page)
            label.setWordWrap(True)
            page.column.insertWidget(page.column.count() - 1, label)
        return page

    def _page_done(self) -> WizardPage:
        page = WizardPage("done", OOBE.DONE_TITLE, OOBE.DONE_BODY, self)
        open_folder = FluentButton(OOBE.OPEN_FOLDER, page,
                                   variant=ButtonVariant.STANDARD)
        open_folder.clicked.connect(self._on_open_folder)
        page.column.insertWidget(2, open_folder)
        return page

    # ═════════════════════════════════════════════════════════════════════════
    # Navigation
    # ═════════════════════════════════════════════════════════════════════════

    def _build_buttons(self) -> QWidget:
        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(SPACING["l"], SPACING["m"],
                               SPACING["l"], SPACING["m"])
        row.addStretch(1)
        self._back = FluentButton(OOBE.BACK, holder,
                                  variant=ButtonVariant.STANDARD)
        self._back.clicked.connect(self.previous_page)
        row.addWidget(self._back)
        self._next = FluentButton(OOBE.NEXT, holder,
                                  variant=ButtonVariant.ACCENT)
        self._next.clicked.connect(self._on_next)
        row.addWidget(self._next)
        return holder

    @property
    def current_key(self) -> str:
        return PAGES[self._stack.currentIndex()]

    def next_page(self) -> None:
        index = min(self._stack.currentIndex() + 1, len(PAGES) - 1)
        self._stack.setCurrentIndex(index)
        self._sync_buttons()

    def previous_page(self) -> None:
        index = max(self._stack.currentIndex() - 1, 0)
        self._stack.setCurrentIndex(index)
        self._sync_buttons()

    def _on_next(self) -> None:
        if self.current_key == PAGES[-1]:
            self.finished.emit(self.finalize())
            return
        self.next_page()

    def _sync_buttons(self) -> None:
        index = self._stack.currentIndex()
        self._back.setEnabled(index > 0)
        self._next.setText(OOBE.BACKUP_START if index == len(PAGES) - 1
                           else OOBE.NEXT)

    def _on_open_folder(self) -> None:
        from onedriveui.platform import desktop

        desktop.open_path(self._folder.text())

    # ═════════════════════════════════════════════════════════════════════════
    # Finalising
    # ═════════════════════════════════════════════════════════════════════════

    def finalize(self) -> FinalizeReport:
        """Do everything the setup promised, and only then declare success.

        Returns:
            A :class:`FinalizeReport`. ``first_run_complete`` is written **only**
            when every step worked.

        The order matters: the check file and the filters have to exist before
        the mount starts, or the first bisync run aborts on a missing
        ``RCLONE_TEST`` and the user's first experience of the client is a
        critical error they did nothing to cause.
        """
        report = FinalizeReport()
        root = Path(self._folder.text()).expanduser()

        report.check_file_seeded = self._seed_check_file(root, report)
        report.filters_written = self._write_filters(report)
        report.units_installed = self._install_units(report)
        report.icons_installed, report.extension_installed = \
            self._install_integration(report)
        report.mount_started = self._start_mount(report)

        # Autostart **last**, and only if everything else worked. It used to run
        # before `_start_mount`, so a setup that failed at the final step still
        # installed a login unit — leaving a client that starts at every boot
        # and has nothing to sync. An unusable install that reappears on every
        # login is worse than one that simply did not finish.
        if report.errors:
            log.warning("setup did not complete: %s", "; ".join(report.errors))
            return report

        report.autostart_set = self._set_autostart(report)
        if report.ok:
            self._mark_complete(report)
        else:
            log.warning("setup did not complete: %s", "; ".join(report.errors))
        return report

    def _seed_check_file(self, root: Path, report: FinalizeReport) -> bool:
        """`RCLONE_TEST` on both sides, before anything syncs.

        bisync's `--check-access` refuses to run without it, and a first run
        that aborts because the file it needs was never created is the worst
        possible introduction to a sync client.
        """
        from onedriveui import paths

        # Never into a live FUSE mount. `sync_root` **is** the mountpoint in the
        # mount-only topology this client actually runs, so writing here does
        # not create a local marker at all — it uploads a file called
        # RCLONE_TEST into the root of the user's OneDrive, visible on every
        # device they own, on the very first run. The file exists solely for
        # bisync's `--check-access`, which the mount topology never uses.
        if paths.is_under_fuse_mount(root):
            log.info("%s is a live mount; not seeding RCLONE_TEST into the "
                     "user's cloud", root)
            return True
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / "RCLONE_TEST").write_text("", encoding="utf-8")
            return True
        except OSError as exc:
            report.errors.append(f"could not seed the check file: {exc}")
            return False

    def _write_filters(self, report: FinalizeReport) -> bool:
        from onedriveui.rc import filters

        account_id = getattr(self.account, "id", "") or "onedrive"
        try:
            filters.write(account_id, filters.render(()))
            return True
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"could not write the filters file: {exc}")
            return False

    def _install_units(self, report: FinalizeReport) -> bool:
        rcd = self._services.get("rcd")
        if rcd is None:
            report.errors.append("no daemon supervisor to install units with")
            return False
        try:
            rcd.ensure_running()
            return True
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"could not start the control daemon: {exc}")
            return False

    def _install_integration(self, report: FinalizeReport) -> tuple[bool, bool]:
        from onedriveui.ext import install as ext_install

        try:
            result = ext_install.install()
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"could not install the integration: {exc}")
            return (False, False)
        # The icons are required; the Nautilus extension is not. A user on a
        # different file manager still gets a working tray and a working client.
        if not result.icons_written:
            report.errors.append("no icons were installed")
        return (bool(result.icons_written), result.extension is not None)

    def _set_autostart(self, report: FinalizeReport) -> bool:
        from onedriveui.platform import autostart

        try:
            # `set_enabled` picks the configured method (systemd unit or XDG
            # desktop file) and refuses to leave both installed — two autostart
            # entries mean two clients, two mounts and a fight over the socket.
            autostart.set_enabled(True)
            return True
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"could not set autostart: {exc}")
            return False

    def _start_mount(self, report: FinalizeReport) -> bool:
        mountd = self._services.get("mountd")
        if mountd is None or self.account is None:
            report.errors.append("no mount controller to start the mount with")
            return False
        try:
            mountd.ensure_mounted(self.account)
            return True
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"could not start the mount: {exc}")
            return False

    def _mark_complete(self, report: FinalizeReport) -> None:
        """Write ``first_run_complete`` — **only** on a complete success.

        This is the flag that stops the wizard reappearing. Setting it after a
        partial setup produces a client that will never offer to configure
        itself again and does not work, which is strictly worse than one that
        asks again.
        """
        if self._config is None:
            return
        from onedriveui import config as config_module
        from onedriveui.bus import BUS

        self._config.set("app.first_run_complete", True)
        try:
            config_module.save(self._config)
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"could not save the configuration: {exc}")
            return
        BUS.config_changed.emit("app.first_run_complete")
        log.info("setup complete")

    def page(self, key: str) -> WizardPage | None:
        return self._pages.get(key)
