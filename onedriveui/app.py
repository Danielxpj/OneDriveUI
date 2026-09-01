"""The composition root: where every object is made, in the one order that works.

Start-up order is not a style question here. Six of the steps below fail — some
loudly, some silently — if they happen in the wrong place, and every one of them
was found the hard way:

1. **``QApplication`` before anything Qt.** ``QSystemTrayIcon.isSystemTrayAvailable()``
   *segfaults* without one, and it is exactly the sort of question start-up code
   asks early.
2. **Logging before anything that can fail.** A crash during step 3 with no
   handler installed produces a traceback on a stderr nobody is reading, from a
   process launched by systemd.
3. **The GLib pump before D-Bus.** Notifications, the network monitor and the
   power monitor all ride ``GLib.MainContext.iteration()`` driven by a
   ``QTimer``. Without the pump they connect, report nothing, and never error.
4. **Config before the database.** The database's location comes from config,
   and a default-constructed one would put it somewhere the user did not choose.
5. **The database writer before any repository call.** Every repository submits
   to it; called first, they start one of their own on the wrong thread and the
   thread-affinity guard refuses every later write.
6. **The supervisor last.** It ticks immediately on ``start()``, and a tick that
   reaches a half-built service is the one class of bug that only appears on a
   slow machine.

:data:`STARTUP_ORDER` states that order and a test asserts the code follows it,
because a comment describing an ordering constraint is a comment that will be
violated within a month.

The headless path matters as much as the GUI one. ``onedriveui --state`` builds
everything down to the supervisor, prints one ``SyncState``, and exits — no
window, no tray, no notifier. That is milestone M1, and it is also the only way
to ask what the engine thinks from a script or a bug report.
"""

from __future__ import annotations

import logging
import signal
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Final

from onedriveui import applog, config, paths
from onedriveui.bus import BUS
from onedriveui.data.writer import DbWriter
from onedriveui.models import AccountInfo, SyncSnapshot, SyncState

log = logging.getLogger(__name__)

__all__ = ["Application", "STARTUP_ORDER", "install_crash_handler", "build_engine"]

#: The order the constructor must follow. Asserted by
#: `tests/test_app.py::test_startup_order_matches_the_code`, because an ordering
#: constraint that lives only in a comment is one that gets violated.
STARTUP_ORDER: Final[tuple[str, ...]] = (
    "qt",           # QApplication: isSystemTrayAvailable() segfaults without one
    "logging",      # before anything that can fail
    "glib_pump",    # before any D-Bus consumer
    "config",       # the database's location comes from it
    "theme",        # the stylesheet, before any widget is built
    "database",     # the writer, before any repository call
    "platform",     # power, notifier, IPC
    "rc",           # daemon supervisor, mount controller
    "services",     # pause, quota, issues, activity, pinner, …
    "supervisor",   # last: it ticks immediately
    "ui",           # tray and windows, only in GUI mode
)


def install_crash_handler() -> None:
    """Route uncaught exceptions into the log before they reach stderr.

    Under systemd stderr goes to the journal in a form nobody correlates with
    the application's own log, and in a GUI session it goes nowhere at all. A
    crash that leaves no diagnosable trace is a crash that gets reported as
    "it just stopped working".

    Qt's own slot exceptions are included: PySide6 prints them and *continues*,
    so a broken signal handler otherwise degrades the application silently for
    the rest of the session.
    """
    previous = sys.excepthook

    def handler(kind, value, tb):  # pragma: no cover - only on a real crash
        log.critical("uncaught %s: %s\n%s", kind.__name__, value,
                     "".join(traceback.format_exception(kind, value, tb)))
        previous(kind, value, tb)

    sys.excepthook = handler


class SystemdAdapter:
    """`platform/systemd.py`'s functions, presented as a `SystemdLike` object.

    WP-02's `RcdSupervisor` and `MountController` depend on the *shape* of a
    service manager rather than on the module, which is what let the rc layer
    and the platform layer be built independently and what lets a test
    substitute a recorder. This is the one place the two are joined, and it is
    trivially small — which is the point: an adapter with logic in it would be a
    third implementation to keep in step with the other two.
    """

    __slots__ = ()

    @staticmethod
    def write_unit(name: str, text: str) -> Any:
        from onedriveui.platform import systemd

        return systemd.write_unit(name, text)

    @staticmethod
    def daemon_reload() -> Any:
        from onedriveui.platform import systemd

        return systemd.daemon_reload()

    @staticmethod
    def enable(name: str) -> Any:
        from onedriveui.platform import systemd

        return systemd.enable(name)

    @staticmethod
    def start(name: str) -> Any:
        from onedriveui.platform import systemd

        return systemd.start(name)

    @staticmethod
    def stop(name: str) -> Any:
        from onedriveui.platform import systemd

        return systemd.stop(name)

    @staticmethod
    def restart(name: str) -> Any:
        from onedriveui.platform import systemd

        return systemd.restart(name)

    @staticmethod
    def is_active(name: str) -> bool:
        from onedriveui.platform import systemd

        return bool(systemd.is_active(name))

    @staticmethod
    def status_text(name: str) -> str:
        from onedriveui.platform import systemd

        return str(systemd.status_text(name))


@dataclass(slots=True)
class Engine:
    """Everything below the UI, for one account.

    Built by :func:`build_engine` and usable with no display at all — which is
    what ``--state`` uses, and what makes the whole engine testable without a
    compositor.
    """

    account: AccountInfo
    writer: Any = None
    supervisor: Any = None
    services: dict[str, Any] = field(default_factory=dict)

    def state(self) -> SyncState:
        if self.supervisor is None:
            return SyncState.NOT_RUNNING
        return self.supervisor.state()

    def snapshot(self) -> SyncSnapshot:
        return self.supervisor.snapshot()

    def bring_up(self) -> list[str]:
        """Start the control daemon and the mount this account needs.

        Returns:
            What went wrong, empty when everything came up.

        **This is what makes the difference between a client that works and one
        that correctly reports being broken.** ARCHITECTURE §5.2 says the
        control plane is "always up, starts before any account exists" — and
        nothing else in the running application starts it. Without this call the
        engine observes ``daemon_rcd = DOWN``, rung 5 fires, and the tray sits in
        ERROR forever: an honest report about a daemon we were supposed to have
        started ourselves.

        Not called by the headless one-shot commands. ``--state`` asks what is
        happening; installing a systemd unit as a side effect of asking a
        question would be a surprise.

        Blocking: ``ensure_running()`` waits for the daemon to answer
        ``rc/noop``. It is called once, before the tick loop starts, so nothing
        is polling while it waits.
        """
        errors: list[str] = []
        rcd = self.services.get("rcd")
        if rcd is not None:
            try:
                rcd.ensure_running()
            except Exception as exc:  # noqa: BLE001 - a dead daemon is a state,
                errors.append(f"control daemon: {exc}")  # not a crash
                log.error("could not start the control daemon", exc_info=True)

        mountd = self.services.get("mountd")
        if mountd is not None:
            try:
                mountd.ensure_mounted(self.account)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"mount: {exc}")
                log.error("could not mount %s", self.account.id, exc_info=True)
        return errors

    def stop(self) -> None:
        if self.supervisor is not None:
            self.supervisor.stop()
        if self.writer is not None:
            self.writer.stop()


def _mount_options(cfg: Any, account: AccountInfo) -> dict[str, Any]:
    """The ``accounts[].mount`` block for one account, as a plain mapping.

    Injected into `MountController` rather than imported by it, so the rc layer
    never depends on `config.py` — which is what let the two be built in
    parallel and what lets a test drive a mount with four keys instead of forty.
    """
    try:
        section = cfg.account(account.id).mount
    except Exception:  # noqa: BLE001 - a missing account falls back to defaults
        return {}
    return {f: getattr(section, f) for f in getattr(section, "__slots__", ())}


def build_engine(account: AccountInfo, *, cfg: Any = None,
                 writer: Any = None, headless: bool = False) -> Engine:
    """Wire the engine for one account, in :data:`STARTUP_ORDER`.

    Args:
        account: The account to drive.
        cfg: A loaded config, or ``None`` to load one.
        writer: A started :class:`~onedriveui.data.writer.DbWriter`, or ``None``
            to start one.
        headless: Skip the notifier and the IPC server. ``--state`` uses this;
            so does a test that does not want a D-Bus connection.

    Returns:
        The :class:`Engine`, not yet started.

    Imports are deferred into the function body on purpose. Importing the whole
    service layer at module scope would make ``onedriveui --version`` pay for
    ``rclone``'s transport, the database and Qt's widget set, and would make an
    import cycle out of the fact that the Supervisor knows about the services
    and the services are handed the Supervisor.
    """
    from onedriveui.platform.power import PowerPolicy
    from onedriveui.rc.daemon import RcdSupervisor
    from onedriveui.rc.mountd import MountController
    from onedriveui.sync.activity import ActivityFeed
    from onedriveui.sync.decisions import DecisionCenter
    from onedriveui.sync.filestate import FileStateService
    from onedriveui.sync.issues import IssueEngine
    from onedriveui.sync.pause import PauseManager
    from onedriveui.sync.pinner import Pinner
    from onedriveui.sync.quota import QuotaService
    from onedriveui.sync.supervisor import Supervisor

    cfg = cfg if cfg is not None else config.load()
    if writer is None:
        writer = DbWriter(paths.db_file())
        writer.start_writer()

    systemd = SystemdAdapter()
    rcd = RcdSupervisor(systemd,
                        rclone_path=cfg.get("advanced.rclone_path",
                                            "/usr/bin/rclone"))
    mountd = MountController(systemd, options=lambda acc: _mount_options(cfg, acc))
    power = PowerPolicy()

    def rc_endpoint() -> Any:
        return rcd.endpoint()

    def mount_endpoint() -> Any:
        return mountd.endpoint(account)

    quota = QuotaService(account, endpoint=rc_endpoint)
    pause = PauseManager(account, writer=writer,
                         config_get=lambda key, default=None: cfg.get(key, default))
    issues = IssueEngine(account, writer=writer)
    activity = ActivityFeed(account, writer=writer, issues=issues)
    decisions = DecisionCenter(account, writer=writer)
    pinner = Pinner(account, endpoint=mount_endpoint, writer=writer,
                    issues=issues, activity=activity)
    filestate = FileStateService(account, endpoint=mount_endpoint, writer=writer)

    notifier = None
    ipc = None
    if not headless:
        from onedriveui.platform.ipc import IpcServer
        from onedriveui.platform.notify import Notifier

        notifier = Notifier()
        ipc = IpcServer()

    supervisor = Supervisor(
        account, rcd=rcd, mountd=mountd, pause=pause, quota=quota,
        power=power, issues=issues, pinner=pinner, notifier=notifier,
        ipc=ipc, writer=writer,
        vfs_stats=lambda: None,
        jobs_runner={"quota": lambda: quota.refresh(force=True),
                     "prune": lambda: decisions.expire_stale()})

    # The engine is handed to the services that need to act through it, after
    # the Supervisor exists. `do()` is the single entry point, so this back
    # reference is how a fix offered by an issue reaches the same guards as the
    # identical menu item.
    issues._supervisor = supervisor

    return Engine(account=account, writer=writer, supervisor=supervisor,
                  services={"rcd": rcd, "mountd": mountd, "power": power,
                            "quota": quota, "pause": pause, "issues": issues,
                            "activity": activity, "decisions": decisions,
                            "pinner": pinner, "filestate": filestate,
                            "notifier": notifier, "ipc": ipc})


class Application:
    """The GUI application: engine, tray, windows.

    Args:
        argv: Command-line arguments, for ``QApplication``.
        headless: Build the engine and no UI at all.
    """

    NAME: Final = "OneDriveUI"

    def __init__(self, argv: list[str] | None = None, *,
                 headless: bool = False) -> None:
        self.headless = headless
        self._engines: dict[str, Engine] = {}
        self._tray: dict[str, Any] = {}
        self._windows: dict[str, Any] = {}
        self._pump: Any = None
        self.theme: Any = None
        self.qt: Any = None

        # 1. Qt, before anything that touches it.
        self._start_qt(argv or [])
        # 2. Logging, before anything that can fail.
        self._start_logging()
        # 3. The GLib pump, before any D-Bus consumer.
        self._start_pump()
        # 4. Config — the theme's accent source and the database's location
        #    both come from it.
        self.config = config.load()
        # 5. The theme, before any widget is built.
        self._start_theme()
        # 6. The database writer, before any repository call.
        self.writer = DbWriter(paths.db_file())
        self.writer.start_writer()

    # ═════════════════════════════════════════════════════════════════════════
    # Start-up steps
    # ═════════════════════════════════════════════════════════════════════════

    def _start_qt(self, argv: list[str]) -> None:
        """A ``QApplication`` — or ``QCoreApplication`` when headless.

        First, unconditionally. ``QSystemTrayIcon.isSystemTrayAvailable()``
        segfaults without one, and ``QTimer`` (which the GLib pump is) needs an
        event loop to attach to.
        """
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QApplication

        existing = QCoreApplication.instance()
        if existing is not None:
            self.qt = existing
            return
        self.qt = (QCoreApplication(argv) if self.headless
                   else QApplication(argv))
        self.qt.setApplicationName(self.NAME)
        if not self.headless:
            # `setDesktopFileName` is a QGuiApplication method: it is what links
            # a window to its .desktop entry, so GNOME shows our name and icon
            # rather than "python3". A QCoreApplication has no windows and no
            # such attribute.
            self.qt.setDesktopFileName("onedriveui")
            # Closing the last window must not quit: this application lives in
            # the tray, and a user who closes the Activity Center has not asked
            # to stop syncing.
            self.qt.setQuitOnLastWindowClosed(False)

    def _start_theme(self) -> None:
        """Install the Fluent style, the stylesheet and the focus-ring proxy.

        **Before any widget is built**, which is why it sits ahead of every
        service. Qt applies a stylesheet to widgets as they are polished, so a
        window constructed before the sheet is installed keeps Fusion's defaults
        for the rest of its life — and the symptom is a half-styled window (dark
        cards on a light background) rather than an error anyone would notice in
        a log.

        It sits *after* config because the accent source is a setting.

        Two things go in, in this order:

        * ``FocusRingStyle``, a proxy style that replaces Qt's dotted focus
          rectangle with the Fluent two-tone ring. It has to be installed before
          ``ThemeManager.apply()``, because that calls ``setStyle("Fusion")``
          and would otherwise throw the proxy away.
        * The ``ThemeManager``, which owns light/dark, the accent, and the
          portal subscription that notices the user changing either.

        Headless mode skips it: there are no widgets, and ``QCoreApplication``
        has no ``setStyle``.
        """
        if self.headless:
            return
        try:
            from onedriveui.ui import qss
            from onedriveui.ui.theme import ThemeManager
            from onedriveui.ui.widgets.controls import FocusRingStyle

            qss.ensure_fusion(self.qt)
            self.qt.setStyle(FocusRingStyle(self.qt.style()))

            self.theme = ThemeManager(self.qt)
            self.theme.set_use_system_accent(
                self.config.get("app.accent_source", "onedrive") == "system")
            self.theme.start()
            # Not `ThemeManager.apply()`: it calls `setStyle("Fusion")`, which
            # would replace the proxy installed two lines up and silently take
            # the focus ring with it.
            qss.apply(self.qt, dark=self.theme.is_dark())
        except Exception:  # noqa: BLE001 - an unstyled window beats no window
            log.warning("could not install the theme; the UI will use Qt's "
                        "defaults", exc_info=True)

    def _start_logging(self) -> None:
        install_crash_handler()
        try:
            applog.install()
        except Exception:  # noqa: BLE001 - never fail to start over logging
            logging.basicConfig(level=logging.INFO)
            log.warning("could not install the application log", exc_info=True)

    def _start_pump(self) -> None:
        """The 50 ms ``QTimer`` that drives ``GLib.MainContext.iteration()``.

        **Load-bearing, not an optimisation.** PySide6's ``QDBusArgument``
        cannot marshal the ``uint32`` that
        ``org.freedesktop.Notifications.Notify`` requires, so QtDBus cannot send
        a notification at all and everything D-Bus goes through Gio instead.
        Gio needs a GLib main context to be iterated, and Qt owns the loop —
        hence the pump. Without it, notifications, the network monitor and the
        power monitor all connect, report nothing, and never raise.
        """
        try:
            from onedriveui.platform import glibpump

            self._pump = glibpump.ensure_started()
        except Exception:  # noqa: BLE001
            log.warning("the GLib pump did not start; notifications and the "
                        "network/power monitors will be inert", exc_info=True)

    # ═════════════════════════════════════════════════════════════════════════
    # Accounts
    # ═════════════════════════════════════════════════════════════════════════

    def accounts(self) -> list[AccountInfo]:
        """Every configured account."""
        out: list[AccountInfo] = []
        for entry in getattr(self.config, "accounts", []) or []:
            to_info = getattr(entry, "to_account_info", None)
            if callable(to_info):
                out.append(to_info())
        return out

    def engine_for(self, account: AccountInfo) -> Engine:
        """Build (or fetch) the engine for one account."""
        engine = self._engines.get(account.id)
        if engine is None:
            engine = build_engine(account, cfg=self.config, writer=self.writer,
                                  headless=self.headless)
            self._engines[account.id] = engine
        return engine

    # ═════════════════════════════════════════════════════════════════════════
    # Running
    # ═════════════════════════════════════════════════════════════════════════

    def start(self) -> None:
        """Start every account's engine, and the UI unless headless.

        The order per account is: build the UI, bring the daemon and mount up,
        then start ticking. The supervisor goes **last** because it ticks
        immediately — a tick that reaches a half-built service is the class of
        bug that only shows up on a slow machine, or on somebody else's — and
        the bring-up goes before it so the first tick sees a daemon that is at
        least trying, rather than one nothing ever started.
        """
        for account in self.accounts():
            engine = self.engine_for(account)
            if not self.headless:
                self._build_ui(account, engine)
            for problem in engine.bring_up():
                log.warning("%s did not come up — %s", account.id, problem)
            engine.supervisor.start()

    def _build_ui(self, account: AccountInfo, engine: Engine) -> None:
        from onedriveui.ui.notices import NoticeCenter
        from onedriveui.ui.tray import TrayItem, available

        notices = NoticeCenter(
            account, notifier=engine.services.get("notifier"),
            supervisor=engine.supervisor,
            config_get=lambda key, default=None: self.config.get(key, default))
        notices.connect_bus()
        engine.services["notices"] = notices

        if not available():
            # No tray means the only affordance would be invisible. The Activity
            # Center window is opened instead, so the application is reachable.
            log.warning("no system tray on this session; opening the Activity "
                        "Center instead")
            self.open_activity(account.id)
            return

        tray = TrayItem(account, supervisor=engine.supervisor)
        tray.activity_requested.connect(self.open_activity)
        tray.settings_requested.connect(self.open_settings)
        tray.quit_requested.connect(self.quit)
        tray.show()
        self._tray[account.id] = tray

    def open_activity(self, account_id: str) -> Any:
        """Show the Activity Center for an account."""
        from onedriveui.ui.activity_center import ActivityCenter

        engine = self._engines.get(account_id)
        if engine is None:
            return None
        window = self._windows.get(account_id)
        if window is None:
            window = ActivityCenter(engine.account, supervisor=engine.supervisor,
                                    quota=engine.services.get("quota"))
            # Without these the header gear and the footer's Settings and Help
            # buttons emit into nothing — they look like dead controls, because
            # that is exactly what they are. The window declares the signals;
            # only the composition root knows what a window is.
            window.settings_requested.connect(
                lambda _=None, a=account_id: self.open_settings(a))
            window.help_requested.connect(self._open_help)
            self._windows[account_id] = window
        window.show()
        window.raise_()
        return window

    def _open_help(self) -> None:
        """Help & Settings — the web documentation, in a browser."""
        from onedriveui.constants import WEB_ROOT
        from onedriveui.platform import desktop

        desktop.open_url(WEB_ROOT)

    def open_settings(self, account_id: str) -> Any:
        """Show the Settings window for an account."""
        from onedriveui.ui.settings_window import SettingsWindow

        engine = self._engines.get(account_id)
        if engine is None:
            return None
        key = f"settings:{account_id}"
        window = self._windows.get(key)
        if window is None:
            window = SettingsWindow(engine.account, config=self.config,
                                    supervisor=engine.supervisor,
                                    services=engine.services)
            self._windows[key] = window
        window.show()
        window.raise_()
        return window

    def exec(self) -> int:
        """Run the event loop until something calls :meth:`quit`."""
        self._install_signals()
        return int(self.qt.exec())

    def _install_signals(self) -> None:
        """SIGTERM and SIGINT stop the engine before Qt tears the process down.

        systemd sends SIGTERM on logout. Without this the process dies with the
        mount still up and in-flight activity rows still marked ``inflight`` —
        which the next start-up correctly reports as ``interrupted``, but which
        is avoidable and looks alarming.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.quit())
            except (ValueError, OSError):
                pass          # not the main thread; the caller handles it

    def quit(self) -> None:
        """Stop every engine and leave the loop."""
        log.info("shutting down")
        for engine in self._engines.values():
            engine.supervisor.stop()
        for tray in self._tray.values():
            tray.hide()
        if self._pump is not None:
            from onedriveui.platform import glibpump

            glibpump.shutdown()
        if self.writer is not None:
            self.writer.stop()
        BUS.log_line.emit("shutdown")
        self.qt.quit()
