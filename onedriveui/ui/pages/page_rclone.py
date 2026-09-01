""""rclone engine" — the page this client was missing.

Every other page in this window is a faithful copy of a Microsoft one. This is
not, because Microsoft has no rclone underneath. rclone *is* the product here:
the client's entire job is to run `rclone rcd`, write `rclone mount` units, and
show what the result is doing. And until this page existed, **none of the
twenty-eight mount parameters it writes could be changed from the UI at all** —
`settings_window.RESTART_REQUIRED_KEYS` already listed twelve of them by name,
declaring which ones need a remount when changed, while nothing anywhere could
change one. The only way to tune a mount was to hand-edit `config.json` and
restart the client.

Three things shape the design.

**It is honest about the restart.** `options/set` on the `vfs` block is accepted
by rclone, returns `{}`, and changes nothing about a running VFS. So a setting
that cannot take effect until the mount restarts says so, on the control, and
the page offers the restart rather than leaving the user to guess. The keys that
behave this way are `RESTART_REQUIRED_KEYS`, which this page imports rather than
re-listing — one table, so the two can never disagree.

**It shows the command.** The footer renders the exact `rclone mount` argv these
settings produce, from `MountController.build_argv` — the same function that
writes the unit, not a re-implementation. For anyone who knows rclone, that one
block answers more questions than the rest of the page, and it makes a bad value
visible before it is applied rather than after the mount fails to start.

**Backend flags are refused, loudly.** `extra_args` goes through
`guards.assert_no_backend_flags`. A backend option on the mount command line
renames the filesystem to `onedrive{HASH}:` and orphans the VFS cache — that is
invariant I1, it has already cost this machine two abandoned cache trees, and it
is exactly the mistake a free-text argument box invites.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QLabel,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from onedriveui.strings import SETTINGS
from onedriveui.ui.theme import SPACING
from onedriveui.ui.widgets.containers import (
    InfoBar,
    InfoBarSeverity,
    SectionHeading,
    SettingsCard,
    SettingsExpander,
)
from onedriveui.ui.widgets.controls import (
    ButtonVariant,
    FluentButton,
    FluentLineEdit,
    ToggleSwitch,
)

log = logging.getLogger(__name__)

__all__ = ["RclonePage"]

#: `(label, dotted key, minimum, maximum)` per integer control, grouped into the
#: card they belong to. A table rather than forty hand-written widgets: every one
#: of these behaves identically, and the differences that matter — the range —
#: are the only thing worth stating per row.
#:
#: The ranges are rclone's practical limits, not its parser's. `transfers` above
#: about 16 against one OneDrive account earns HTTP 429s rather than throughput,
#: and a `dir_cache_time` of zero turns every `ls` into a Graph round trip.
CACHE_ROWS: tuple[tuple[str, str, int, int], ...] = (
    (SETTINGS.RC_CACHE_MAX_SIZE, "mount.vfs_cache_max_size_gb", 1, 4096),
    (SETTINGS.RC_CACHE_MAX_AGE, "mount.vfs_cache_max_age_hours", 1, 8760),
    (SETTINGS.RC_CACHE_MIN_FREE, "mount.vfs_cache_min_free_space_gb", 0, 1024),
    (SETTINGS.RC_WRITE_BACK, "mount.write_back_s", 0, 3600),
)

FRESHNESS_ROWS: tuple[tuple[str, str, int, int], ...] = (
    (SETTINGS.RC_DIR_CACHE, "mount.dir_cache_time_s", 1, 86_400),
    (SETTINGS.RC_POLL, "mount.poll_interval_s", 0, 3600),
    (SETTINGS.RC_ATTR_TIMEOUT, "mount.attr_timeout_ms", 0, 60_000),
)

TRANSFER_ROWS: tuple[tuple[str, str, int, int], ...] = (
    (SETTINGS.RC_N_TRANSFERS, "mount.transfers", 1, 32),
    (SETTINGS.RC_N_CHECKERS, "mount.checkers", 1, 64),
    (SETTINGS.RC_TPS_BURST, "mount.tpslimit_burst", 1, 100),
    (SETTINGS.RC_RETRIES, "mount.retries", 0, 20),
    (SETTINGS.RC_LOW_RETRIES, "mount.low_level_retries", 0, 50),
)

READ_ROWS: tuple[tuple[str, str, int, int], ...] = (
    (SETTINGS.RC_CHUNK, "mount.read_chunk_size_mb", 1, 1024),
    (SETTINGS.RC_CHUNK_LIMIT, "mount.read_chunk_size_limit_mb", 0, 8192),
)

#: `(label, dotted key)` per switch.
FILE_SWITCHES: tuple[tuple[str, str], ...] = (
    (SETTINGS.RC_ALLOW_OTHER, "mount.allow_other"),
    (SETTINGS.RC_LINKS, "mount.links"),
    (SETTINGS.RC_FAST_FINGER, "mount.fast_fingerprint"),
)

#: How many log lines the live view keeps. The ring holds 500; showing all of
#: them in a 160 px box is scrolling, not information.
LOG_LINES: int = 200

#: Stand-ins for the per-launch rc credentials in the rendered command.
RC_USER_PLACEHOLDER = "onedriveui"
RC_PASS_PLACEHOLDER = "<generated at launch>"

#: `(label, dotted key)` per free-text field.
FILE_TEXTS: tuple[tuple[str, str], ...] = (
    (SETTINGS.RC_UMASK, "mount.umask"),
    (SETTINGS.RC_FILE_PERMS, "mount.file_perms"),
    (SETTINGS.RC_DIR_PERMS, "mount.dir_perms"),
)


class RclonePage(QWidget):
    """Every rclone mount parameter, and the command line they produce.

    Args:
        account: The account being configured. **Used**, not decorative: config
            reads and writes are scoped to this account's id, because a window
            opened on one account must not edit another's mount.
        config: The `Config`.
        supervisor: Where the remount request goes — `do()` is the only way
            anything in this client changes.
        services: The engine's services, for `mountd` (which renders the argv).
        parent: Qt parent.

    Attributes:
        changed: `(dotted key)` — emitted after every successful write.
    """

    changed = Signal(str)

    def __init__(self, account: Any, *, config: Any = None,
                 supervisor: Any = None, services: Any = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self._config = config
        self._supervisor = supervisor
        self._services = dict(services or {})
        self._cards: dict[str, QWidget] = {}
        self._dirty: set[str] = set()

        column = QVBoxLayout(self)
        column.setContentsMargins(SPACING["l"], SPACING["l"],
                                  SPACING["l"], SPACING["l"])
        column.setSpacing(SPACING["m"])

        column.addWidget(SectionHeading(SETTINGS.NAV_RCLONE, self))

        self._notice = InfoBar("", SETTINGS.RC_INTRO, self,
                               severity=InfoBarSeverity.INFORMATIONAL,
                               closable=False)
        column.addWidget(self._notice)

        column.addWidget(self._binary_card())
        column.addWidget(self._group(SETTINGS.RC_CACHE, SETTINGS.RC_CACHE_DESC,
                                     "cache", CACHE_ROWS,
                                     texts=((SETTINGS.RC_CACHE_DIR,
                                             "mount.cache_dir"),)))
        column.addWidget(self._group(SETTINGS.RC_FRESHNESS,
                                     SETTINGS.RC_FRESHNESS_DESC,
                                     "freshness", FRESHNESS_ROWS))
        column.addWidget(self._transfers_group())
        column.addWidget(self._group(SETTINGS.RC_READS, SETTINGS.RC_READS_DESC,
                                     "reads", READ_ROWS))
        column.addWidget(self._group(SETTINGS.RC_FILES, SETTINGS.RC_FILES_DESC,
                                     "files", (), texts=FILE_TEXTS,
                                     switches=FILE_SWITCHES))
        column.addWidget(self._backend_card())
        column.addWidget(self._extra_args())
        column.addWidget(self._log_card())
        column.addWidget(self._command_card())
        column.addWidget(self._apply_card())
        column.addStretch(1)

        self._refresh_command()

    # ═════════════════════════════════════════════════════════════════════════
    # Cards
    # ═════════════════════════════════════════════════════════════════════════

    def _binary_card(self) -> QWidget:
        """Which rclone. Not per-account: one binary drives every mount."""
        field = FluentLineEdit(self)
        field.setText(str(self._read("advanced.rclone_path",
                                     "/usr/bin/rclone") or ""))
        field.editingFinished.connect(
            lambda: self._write("advanced.rclone_path", field.text().strip()))
        card = SettingsCard(SETTINGS.RC_ENGINE, self,
                            description=SETTINGS.RC_ENGINE_DESC,
                            content=field, action_icon=False)
        self._cards["binary"] = card
        return card

    def _group(self, title: str, description: str, key: str,
               rows: tuple[tuple[str, str, int, int], ...],
               *, texts: tuple[tuple[str, str], ...] = (),
               switches: tuple[tuple[str, str], ...] = ()) -> QWidget:
        """One expander holding a set of related parameters."""
        expander = SettingsExpander(title, self, description=description)
        for label, dotted, low, high in rows:
            expander.add_row(self._labelled(self._mark(label, dotted),
                                            self._spin(dotted, low, high)))
        for label, dotted in texts:
            expander.add_row(self._labelled(self._mark(label, dotted),
                                            self._text(dotted)))
        for label, dotted in switches:
            expander.add_row(self._labelled(self._mark(label, dotted),
                                            self._switch(dotted)))
        self._cards[key] = expander
        return expander

    def _transfers_group(self) -> QWidget:
        """The transfer knobs, plus the one that is a float."""
        expander = SettingsExpander(SETTINGS.RC_TRANSFERS, self,
                                    description=SETTINGS.RC_TRANSFERS_DESC)
        for label, dotted, low, high in TRANSFER_ROWS:
            expander.add_row(self._labelled(self._mark(label, dotted),
                                            self._spin(dotted, low, high)))
        # `tpslimit` is transactions per second and rclone accepts a fraction —
        # 0.5 is a legitimate "one request every two seconds" for an account
        # being rate-limited. An int spinner would silently forbid that.
        expander.add_row(self._labelled(
            self._mark(SETTINGS.RC_TPSLIMIT, "mount.tpslimit"),
            self._spin_float("mount.tpslimit", 0.0, 100.0)))
        self._cards["transfers"] = expander
        return expander

    def _backend_card(self) -> QWidget:
        """The upload chunk size, written where rclone expects to find it.

        `rc/conf.set_backend_options()` calls itself "**The only way this is
        done**" and had no caller anywhere, so the whole `backend.*` block of
        `config.json` was inert: twelve settings that were stored, validated and
        never applied to anything.

        It is explicit rather than automatic on purpose. This writes the user's
        real `rclone.conf`, and doing that silently at start-up would rewrite a
        remote they configured by hand. The button is the consent.
        """
        holder = QWidget(self)
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(SPACING["xs"])

        self._chunk_field = FluentLineEdit(holder)
        self._chunk_field.setText(str(self._read("backend.chunk_size", "10M") or ""))
        inner.addWidget(self._labelled(SETTINGS.RC_CHUNK_UPLOAD,
                                       self._chunk_field))

        apply_button = FluentButton(SETTINGS.RC_BACKEND_APPLY, holder,
                                    variant=ButtonVariant.STANDARD)
        apply_button.clicked.connect(self._write_backend)
        inner.addWidget(apply_button)

        self._backend_note = QLabel("", holder)
        self._backend_note.setWordWrap(True)
        self._backend_note.hide()
        inner.addWidget(self._backend_note)

        card = SettingsCard(SETTINGS.RC_BACKEND, self,
                            description=SETTINGS.RC_BACKEND_DESC,
                            content=holder, action_icon=False)
        self._cards["backend"] = card
        return card

    def _write_backend(self) -> None:
        """Store the chunk size and push it into `rclone.conf`.

        Both halves, in that order: the config file is this client's record of
        intent, and `rclone.conf` is what rclone reads. A refusal from either —
        a chunk size that is not a positive multiple of 320 KiB, a remote that
        is not configured — is reported on the control rather than swallowed.
        """
        from onedriveui.errors import ConfigError, SafetyRefusal
        from onedriveui.rc import conf

        value = self._chunk_field.text().strip()
        self._write("backend.chunk_size", value)
        remote = getattr(self.account, "remote", "")
        if not remote:
            return
        try:
            conf.set_backend_options(remote, {"chunk_size": value})
        except (ConfigError, SafetyRefusal, OSError) as exc:
            self._backend_note.setText(str(exc))
            self._backend_note.show()
            return
        self._backend_note.setText(SETTINGS.RC_BACKEND_OK)
        self._backend_note.show()
        self._show_pending()
        self._dirty.add("backend.chunk_size")

    def _extra_args(self) -> QWidget:
        """Free text, checked against invariant I1 before it is stored."""
        box = QPlainTextEdit(self)
        box.setPlainText("\n".join(self._read("mount.extra_args", []) or []))
        box.setFixedHeight(90)
        box.focusOutEvent = self._wrap_focus_out(box)  # type: ignore[method-assign]
        self._extra_box = box

        holder = QWidget(self)
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(SPACING["xs"])
        inner.addWidget(box)
        self._extra_error = QLabel("", holder)
        self._extra_error.setWordWrap(True)
        self._extra_error.hide()
        inner.addWidget(self._extra_error)

        card = SettingsCard(SETTINGS.RC_EXTRA, self,
                            description=SETTINGS.RC_EXTRA_DESC,
                            content=holder, action_icon=False)
        self._cards["extra_args"] = card
        return card

    def _log_card(self) -> QWidget:
        """A live tail of the engine log.

        `BUS.log_line` had three emitters — the application, the log handler and
        the database writer — and **no subscriber at all**: every line was
        published into nothing. `applog.RING` keeps the last 500 for exactly
        this, and its own comment says so.

        For a client whose whole job is driving rclone, "what is it doing, and
        what did it say when it failed" is the question the UI is asked most
        often, and until now the only answer was `journalctl`.
        """
        from onedriveui import applog
        from onedriveui.bus import BUS

        holder = QWidget(self)
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(SPACING["xs"])

        self._log_view = QPlainTextEdit(holder)
        self._log_view.setReadOnly(True)
        self._log_view.setFixedHeight(160)
        self._log_view.setMaximumBlockCount(LOG_LINES)
        try:
            self._log_view.setPlainText("\n".join(applog.RING.lines(LOG_LINES)))
        except Exception:  # noqa: BLE001 - an empty view beats no page
            log.debug("could not read the log ring", exc_info=True)
        inner.addWidget(self._log_view)

        open_button = FluentButton(SETTINGS.RC_LOG_OPEN, holder,
                                   variant=ButtonVariant.STANDARD)
        open_button.clicked.connect(self._open_log_file)
        inner.addWidget(open_button)

        # No `destroyed` handler: by the time that fires the C++ object is
        # gone, and reaching back through `self` for the bound method to
        # disconnect raises on a deleted object. Qt already drops a connection
        # whose receiver is a QObject when that receiver is destroyed, which is
        # exactly this case; `_append_log` catches the race for the rest.
        BUS.log_line.connect(self._append_log)

        card = SettingsCard(SETTINGS.RC_LOG, self,
                            description=SETTINGS.RC_LOG_DESC,
                            content=holder, action_icon=False)
        self._cards["log"] = card
        return card

    def _append_log(self, line: str) -> None:
        """One line, live. `setMaximumBlockCount` caps the widget's own memory."""
        view = getattr(self, "_log_view", None)
        if view is None:
            return
        try:
            view.appendPlainText(line)
        except RuntimeError:  # pragma: no cover - the widget outlived by the bus
            self._drop_log_subscription()

    def _drop_log_subscription(self) -> None:
        """Stop listening. Called when the widget is already going away, so it
        must not assume the C++ side is still there."""
        from onedriveui.bus import BUS

        try:
            BUS.log_line.disconnect(self._append_log)
        except (RuntimeError, TypeError):  # pragma: no cover - already gone
            pass

    def _open_log_file(self) -> None:
        from onedriveui import paths
        from onedriveui.platform import desktop

        desktop.open_path(paths.log_file())

    def _command_card(self) -> QWidget:
        """The argv these settings produce, from the code that writes the unit."""
        self._command = QPlainTextEdit(self)
        self._command.setReadOnly(True)
        self._command.setFixedHeight(120)
        card = SettingsCard(SETTINGS.RC_COMMAND, self,
                            description=SETTINGS.RC_COMMAND_DESC,
                            content=self._command, action_icon=False)
        self._cards["command"] = card
        return card

    def _apply_card(self) -> QWidget:
        """The restart, offered only once something needs it."""
        self._apply = FluentButton(SETTINGS.RC_APPLY, self,
                                   variant=ButtonVariant.ACCENT)
        self._apply.clicked.connect(self._restart_mount)
        self._apply.setEnabled(False)
        card = SettingsCard(SETTINGS.RC_PENDING, self,
                            content=self._apply, action_icon=False)
        card.hide()
        self._cards["apply"] = card
        return card

    # ═════════════════════════════════════════════════════════════════════════
    # Controls
    # ═════════════════════════════════════════════════════════════════════════

    def _spin(self, dotted: str, low: int, high: int) -> QSpinBox:
        spinner = QSpinBox(self)
        spinner.setRange(low, high)
        spinner.setValue(int(self._read(dotted, low) or low))
        # `editingFinished`, not `valueChanged`: the latter fires on every
        # intermediate value while a number is being typed or an arrow held
        # down, and each one is an atomic rewrite-and-fsync of config.json.
        # Typing "1024" wrote the file four times, three of them for values the
        # user never chose.
        spinner.editingFinished.connect(
            lambda k=dotted, w=spinner: self._write(k, w.value()))
        return spinner

    def _spin_float(self, dotted: str, low: float, high: float) -> QDoubleSpinBox:
        spinner = QDoubleSpinBox(self)
        spinner.setRange(low, high)
        spinner.setDecimals(1)
        spinner.setSingleStep(0.5)
        spinner.setValue(float(self._read(dotted, low) or low))
        spinner.editingFinished.connect(
            lambda k=dotted, w=spinner: self._write(k, w.value()))
        return spinner

    def _text(self, dotted: str) -> FluentLineEdit:
        field = FluentLineEdit(self)
        field.setText(str(self._read(dotted, "") or ""))
        field.editingFinished.connect(
            lambda k=dotted, f=field: self._write(k, f.text().strip()))
        return field

    def _switch(self, dotted: str) -> ToggleSwitch:
        switch = ToggleSwitch(self)
        switch.setChecked(bool(self._read(dotted, False)))
        switch.toggled.connect(lambda on, k=dotted: self._write(k, on))
        return switch

    def _mark(self, label: str, dotted: str) -> str:
        """Append the restart marker to a label whose key needs one."""
        from onedriveui.ui.settings_window import RESTART_REQUIRED_KEYS

        return f"{label} *" if dotted in RESTART_REQUIRED_KEYS else label

    def _labelled(self, text: str, control: QWidget) -> QWidget:
        holder = QWidget(self)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACING["xs"])
        column.addWidget(QLabel(text, holder))
        column.addWidget(control)
        return holder

    # ═════════════════════════════════════════════════════════════════════════
    # Reading and writing
    # ═════════════════════════════════════════════════════════════════════════

    def _read(self, dotted: str, default: Any) -> Any:
        if self._config is None:
            return default
        return self._config.get(dotted, default,
                                account_id=getattr(self.account, "id", None))

    def _write(self, dotted: str, value: Any) -> None:
        """Write one key against **this** account, then re-render the command.

        Scoped by `account_id`: `Config.get`/`set` otherwise resolve every
        account key against the *active* account, so a Settings window opened on
        the second account silently edited the first one's mount.
        """
        if self._config is None:
            return
        from onedriveui import config as config_module
        from onedriveui.bus import BUS
        from onedriveui.ui.settings_window import RESTART_REQUIRED_KEYS

        account_id = getattr(self.account, "id", None)
        try:
            if not self._config.set(dotted, value, account_id=account_id):
                return
        except Exception:  # noqa: BLE001 - a rejected value is not a crash
            log.warning("could not set %s", dotted, exc_info=True)
            return
        try:
            config_module.save(self._config)
        except Exception:  # noqa: BLE001 - a failed save must not lose the window
            log.error("could not save %s", dotted, exc_info=True)
            return

        BUS.config_changed.emit(dotted)
        self.changed.emit(dotted)
        if dotted in RESTART_REQUIRED_KEYS:
            self._dirty.add(dotted)
            self._show_pending()
        self._refresh_command()

    def _wrap_focus_out(self, box: QPlainTextEdit) -> Callable[[Any], None]:
        """Validate and store `extra_args` when the box loses focus."""
        original = type(box).focusOutEvent

        def handler(event: Any) -> None:
            original(box, event)
            self._store_extra_args(box.toPlainText())

        return handler

    def _store_extra_args(self, raw: str) -> None:
        """Store the extra arguments, refusing any backend flag (invariant I1).

        A backend option on a mount command line renames the filesystem to
        `onedrive{HASH}:` and sends its VFS cache to a directory nothing else
        will ever look in. The guard that knows which flags those are already
        exists; this is a text box wired to it.
        """
        from onedriveui.errors import SafetyRefusal
        from onedriveui.rc import guards

        args = [line.strip() for line in raw.splitlines() if line.strip()]
        try:
            guards.assert_no_backend_flags(args)
        except SafetyRefusal as refusal:
            self._extra_error.setText(str(refusal))
            self._extra_error.show()
            return
        self._extra_error.hide()
        self._write("mount.extra_args", args)

    # ═════════════════════════════════════════════════════════════════════════
    # The command, and applying it
    # ═════════════════════════════════════════════════════════════════════════

    def _refresh_command(self) -> None:
        """Re-render the argv from the controller that writes the real unit."""
        mountd = self._services.get("mountd")
        if mountd is None or not hasattr(self, "_command"):
            return
        try:
            # A placeholder port and credentials: they are minted per launch, and
            # rendering a live rc password into a settings window would put it on
            # every screenshot anyone ever posts. Every other flag is real.
            argv = mountd.build_argv(self.account, 0,
                                     (RC_USER_PLACEHOLDER, RC_PASS_PLACEHOLDER))
        except Exception as exc:  # noqa: BLE001 - a bad value is a message
            self._command.setPlainText(str(exc))
            return
        self._command.setPlainText(" ".join(str(part) for part in argv))

    def _show_pending(self) -> None:
        card = self._cards.get("apply")
        if card is not None:
            card.show()
        if hasattr(self, "_apply"):
            self._apply.setEnabled(True)

    def _restart_mount(self) -> None:
        """Ask the Supervisor to restart the mount. The only way to apply these."""
        from onedriveui.models import RecoveryAction

        if self._supervisor is None:
            return
        self._apply.setEnabled(False)
        self._apply.setText(SETTINGS.RC_RESTARTING)
        try:
            self._supervisor.do(RecoveryAction.RESTART_MOUNT)
        except Exception:  # noqa: BLE001 - a refusal is reported, not raised
            log.warning("the mount restart was refused", exc_info=True)
            self._apply.setEnabled(True)
            self._apply.setText(SETTINGS.RC_APPLY)
            return

        # `do()` returns nothing whether the restart happened or was refused —
        # invariant I3 declines while an upload is in flight, and the ladder
        # declines after too many restarts in an hour. Clearing the notice
        # unconditionally told the user their settings were live when they may
        # not be, so the mount's own health is what decides.
        self._apply.setText(SETTINGS.RC_APPLY)
        mountd = self._services.get("mountd")
        restarting = True
        if mountd is not None:
            try:
                from onedriveui.models import MountHealth

                restarting = mountd.health(self.account) is not MountHealth.UP
            except Exception:  # noqa: BLE001 - assume it took
                restarting = True
        if not restarting:
            # Still UP and serving: the restart did not happen.
            self._apply.setEnabled(True)
            return
        self._dirty.clear()
        card = self._cards.get("apply")
        if card is not None:
            card.hide()

    # ═════════════════════════════════════════════════════════════════════════
    # Reads, for the settings window and for tests
    # ═════════════════════════════════════════════════════════════════════════

    def card(self, key: str) -> QWidget | None:
        """One card by key, for deep-link navigation and for tests."""
        return self._cards.get(key)

    def pending_restart(self) -> frozenset[str]:
        """Which changed keys are waiting for the mount to restart."""
        return frozenset(self._dirty)

    def command_text(self) -> str:
        """The rendered argv, for tests."""
        return self._command.toPlainText() if hasattr(self, "_command") else ""
