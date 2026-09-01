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
from onedriveui.data import db
from onedriveui.data.writer import WRITER
from onedriveui.models import AccountInfo, RcEndpoint, SyncSnapshot, SyncState

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
    #: The limit from config, applied by `attach_live()` once a daemon exists.
    desired_bandwidth: Any = None
    #: Whether `attach_live()` has already pushed that limit to the daemons.
    _bandwidth_applied: bool = False

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

        self.attach_live()
        return errors

    def attach_live(self) -> None:
        """Point the pollers at the mount and start them.

        Separate from construction because the mount's rc port does not exist
        until `ensure_mounted()` has run — `build_engine` can only hand the
        client a placeholder, and this is where it learns the real address.

        Idempotent, so a mount restart can call it again to re-point the same
        client rather than building a second one.
        """
        client = self.services.get("mount_client")
        mountd = self.services.get("mountd")
        if client is None or mountd is None:
            return
        endpoint = mountd.endpoint(self.account)
        if endpoint is None or not getattr(endpoint, "port", 0):
            log.warning("no mount rc endpoint for %s; transfer progress and the "
                        "upload queue will be blank", self.account.id)
            return
        client.set_endpoint(endpoint)

        # The control-plane client, for sign-in.
        rcd = self.services.get("rcd")
        rcd_client = self.services.get("rcd_client")
        if rcd is not None and rcd_client is not None:
            control = rcd.endpoint()
            if control is not None and getattr(control, "port", 0):
                rcd_client.set_endpoint(control)

        for name in ("stats", "vfs_probe"):
            poller = self.services.get(name)
            if poller is not None and not poller.running:
                poller.start()

        # The limit the user configured, applied the moment there is something
        # to apply it to. On the pool: `ops.set_bwlimit` is a blocking rc call.
        #
        # Once per attach that actually changed something. `attach_live()` runs
        # from `bring_up()` and again whenever the mount reports UP, so applying
        # unconditionally sent two identical `core/bwlimit` round trips to two
        # daemons at every launch.
        bandwidth = self.services.get("bandwidth")
        pool = self.services.get("pool")
        if self._bandwidth_applied:
            return
        if bandwidth is not None and self.desired_bandwidth is not None:
            self._bandwidth_applied = True
            if pool is not None:
                pool.submit(bandwidth.apply, self.desired_bandwidth, kind="rc")
            else:
                bandwidth.apply(self.desired_bandwidth)

    def stop(self) -> None:
        # Pollers first: each holds a QTimer and an in-flight request, and a
        # reply arriving after the supervisor is gone would be delivered into a
        # half-torn-down engine.
        for name in ("stats", "vfs_probe"):
            poller = self.services.get(name)
            if poller is not None:
                try:
                    poller.stop()
                except Exception:  # noqa: BLE001 - shutdown never raises
                    log.debug("could not stop %s", name, exc_info=True)
        # Then the clients. Each owns a QNetworkAccessManager and may still
        # hold replies; `close()` aborts them, which is what stops a reply
        # landing in a torn-down engine after the event loop has gone.
        for name in ("mount_client", "rcd_client"):
            client = self.services.get(name)
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - shutdown never raises
                    log.debug("could not close %s", name, exc_info=True)
        if self.supervisor is not None:
            self.supervisor.stop()
        # The writer is deliberately NOT stopped here. It is the process-wide
        # `WRITER` singleton shared by every account, so the first engine to
        # stop would close the connection the others are still writing through.
        # Its lifecycle belongs to whoever started it — `Application.quit()`,
        # after the last engine has gone.


#: The states in which the pollers drop to their slow cadence.
_PAUSED_STATES: frozenset = frozenset({
    SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
    SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA,
})


#: `Notice.severity` is a plain string; the InfoBar wants its own enum.
_BANNER_SEVERITY: dict[str, str] = {
    "error": "ERROR", "warning": "WARNING", "info": "INFORMATIONAL",
}


def _show_banner(window: Any, notice: Any) -> None:
    """Render one `Notice` in the Activity Center, or clear the banner.

    The two sides were built to fit and never joined: `Notice` carries
    `(action, label)` pairs whose `action` is a `RecoveryAction` value, and
    `ActivityCenter.set_banner(actions=…)` emits exactly that back on
    `BUS.notification_action`, where `NoticeCenter._on_toast_action` turns it
    into `do()`. The whole loop existed except for this connection.
    """
    from onedriveui.ui.widgets.containers import InfoBarSeverity

    if notice is None:
        window.clear_banner()
        return
    severity = getattr(InfoBarSeverity,
                       _BANNER_SEVERITY.get(str(notice.severity), "INFORMATIONAL"))
    window.set_banner(notice.title, notice.detail, severity=severity,
                      actions=tuple(notice.actions),
                      key=str(getattr(notice.code, "value", notice.code)),
                      closable=bool(notice.dismissible))


def _refresh_quota(pool: Any, quota: Any, engine_ref: dict) -> None:
    """Re-read the drive's usage, and report whether the cloud answered.

    Two jobs in one because they share the round trip. `operations/about` is the
    only call this client makes to Microsoft on a schedule, which makes its
    outcome the honest source for the offline rung: `note_network_result()`
    counts consecutive failures, and three of them is what "offline" means when
    the network monitor still claims a connection — a captive portal, a dropped
    VPN, a DNS server that stopped answering.

    It had no caller, so `Facts.network` never left ONLINE and the client could
    not report being offline at all. Wiring it to the mount's loopback rc would
    have been wrong: that failing means the mount died, which the mount health
    already says.
    """
    def report(ok: bool) -> None:
        supervisor = engine_ref.get("supervisor")
        if supervisor is None:
            return
        try:
            supervisor.collector.note_network_result(ok)
        except Exception:  # noqa: BLE001 - observability never breaks a tick
            log.debug("could not record the network result", exc_info=True)

    # Whether the *sample moved*, not whether an exception escaped.
    # `QuotaService.refresh()` catches `RcError`/`DaemonUnavailable`/`OSError`
    # itself and returns the cached value, so `on_error` can never fire and
    # taking `on_done` as proof of a reachable cloud reported every outage as a
    # success — leaving the OFFLINE rung permanently unreachable. `sampled_at`
    # is stamped only on a reply that actually arrived.
    before = getattr(quota.current(), "sampled_at", "")

    def done(_result: Any) -> None:
        after = getattr(quota.current(), "sampled_at", "")
        report(bool(after) and after != before)

    pool.submit(quota.refresh, kind="rc", force=True,
                on_done=done, on_error=lambda _exc: report(False))


def _prune(writer: Any, decisions: Any) -> None:
    """The hourly maintenance of ARCHITECTURE §10.

    Two things, both of which were missing. `decisions.expire_stale()` was the
    only one wired; `db.vacuum_and_prune()` — which caps the activity and issue
    tables and drops old runs — had no caller anywhere, so those tables grew
    for the life of the installation and the 5 000-row cap the schema advertises
    was never applied.

    Submitted to the writer rather than run here: it writes, and every write in
    this application goes through the one thread that owns the connection.
    """
    try:
        decisions.expire_stale()
    except Exception:  # noqa: BLE001 - one half must not stop the other
        log.warning("could not expire stale decisions", exc_info=True)
    if writer is None:
        return
    try:
        # The DELETEs only. `vacuum_and_prune` skips its own checkpoint when it
        # runs as a guest inside the writer's transaction, and that is correct:
        # every op submitted here lands inside `DbWriter`'s `BEGIN IMMEDIATE`,
        # so a `PRAGMA wal_checkpoint` scheduled alongside would be a no-op
        # however it were sequenced. The write-ahead log does not need us —
        # `wal_autocheckpoint` is SQLite's default 1000 pages and is active on
        # this database, and a clean `writer.stop()` truncates it at exit.
        writer.submit(lambda conn: db.vacuum_and_prune(conn),
                      label="vacuum_and_prune")
    except Exception:  # noqa: BLE001 - maintenance is never fatal
        log.warning("could not schedule the database prune", exc_info=True)


def _wire_ipc(ipc: Any, account: AccountInfo, filestate: Any,
              supervisor: Any) -> None:
    """Start the Nautilus server and give it the providers it needs.

    `IpcServer` was constructed and then abandoned: `start()` was never called,
    no state provider was ever set, and nothing subscribed to the actions it
    emits — despite its own docstring saying the Supervisor listens. The whole
    file-manager integration was inert: no emblems, a context menu whose verbs
    reached nothing, and no push invalidation.

    The state provider is the adapter between two different vocabularies. The
    extension asks about **absolute** paths, because that is what Nautilus hands
    it; `FileStateService` answers about paths **relative to the sync root**,
    because that is what the VFS cache is indexed by. Anything outside the root
    is dropped rather than guessed at — a file on another filesystem has no
    sync state, and inventing one would draw a badge that means nothing.

    Args:
        ipc: The server.
        account: Whose sync root the paths are relative to.
        filestate: The `FileStateService` answering the lookups.
        supervisor: Where verbs go. `do()` is the only way to change anything.
    """
    from pathlib import Path as _Path

    from onedriveui.models import RecoveryAction

    root = _Path(account.sync_root)
    # One server, many accounts: registered by root, so a path is answered by
    # the account that owns it rather than by whichever engine was wired last.
    _IPC_ACCOUNTS[account.id] = (root, filestate)

    def states(paths: Any) -> dict[str, str]:
        """Resolve each path through whichever account's root contains it."""
        out: dict[str, str] = {}
        for acc_root, acc_filestate in list(_IPC_ACCOUNTS.values()):
            rel_for: dict[str, str] = {}
            for absolute in paths:
                if absolute in out:
                    continue
                try:
                    rel_for[str(_Path(absolute).relative_to(acc_root))] = absolute
                except ValueError:
                    continue              # not this account's; try the next
            if not rel_for:
                continue
            try:
                found = acc_filestate.statuses(list(rel_for))
            except Exception:  # noqa: BLE001 - one account must not blank the rest
                log.debug("could not read states under %s", acc_root,
                          exc_info=True)
                continue
            for rel, status in found.items():
                if rel in rel_for:
                    out[rel_for[rel]] = str(status.state)
        return out

    def run(verb: str, paths: Any, request: Any = None) -> None:
        # Pause and resume are not recovery actions: they take a duration
        # rather than a path, and the Supervisor exposes them directly. The
        # launcher's "Pause syncing" arrives here.
        if verb in ("pause", "resume"):
            request = request or {}
            if verb == "resume":
                supervisor.request_resume()
                return
            from onedriveui.models import PauseReason

            hours = request.get("hours")
            supervisor.request_pause(PauseReason.MANUAL,
                                     int(hours) if hours else None)
            return
        try:
            action = RecoveryAction(verb)
        except ValueError:
            log.warning("file manager asked for unknown verb %r", verb)
            return
        for absolute in paths or []:
            try:
                rel = str(_Path(absolute).relative_to(root))
            except ValueError:
                log.warning("%s is outside %s; ignoring", absolute, root)
                continue
            try:
                supervisor.do(action, rel_path=rel)
            except Exception:  # noqa: BLE001 - one bad path must not kill the rest
                log.exception("%s failed for %s", verb, rel)

    ipc.set_state_provider(states)
    ipc.set_account(account.id, str(account.sync_root))
    # The full-payload signal: `action_requested` carries no arguments, and a
    # pause without its duration is a pause that cannot be honoured.
    ipc.command_requested.connect(run)
    if not ipc.start():
        log.warning("the Nautilus IPC server did not start; the file manager "
                    "will show no emblems and its menu will do nothing")


#: The rclone remote name first-run setup creates. One name, because the
#: wizard configures exactly one account and every later account is added
#: through Settings.
WIZARD_REMOTE: Final[str] = "onedrive"

#: The one Nautilus server for this process. Created on first use.
_IPC: Any = None

#: account id -> (sync root, FileStateService, Supervisor). One server
#: serves every account, so a request is routed by which root contains
#: the path rather than by whichever engine was wired last.
_IPC_ACCOUNTS: dict[str, tuple] = {}


def _shared_ipc() -> Any:
    """The process-wide `IpcServer`.

    A singleton because its socket is: `paths.ipc_socket()` has no account in
    it, and two servers cannot listen on one path. Each account registers its
    own root and providers through `_wire_ipc`.
    """
    global _IPC
    if _IPC is None:
        from onedriveui.platform.ipc import IpcServer

        _IPC = IpcServer()
    return _IPC


def _bandwidth_state(cfg: Any) -> Any:
    """The configured limits, as the model the controller applies.

    The three `bandwidth.*` keys the Settings page writes have meant nothing
    until now: `BandwidthController` was never constructed, so nothing ever
    called `core/bwlimit` and a user who set 500 KB/s watched rclone transfer at
    line rate. This is the projection that makes those controls real.

    `upload_mode` is a tri-state (`"none"`, `"auto"`, `"limit"`), not a boolean:
    "auto" hands the ceiling to `AutoUploadController` rather than pinning it,
    so it must not be read as a manual limit of zero.
    """
    from onedriveui.models import BandwidthState

    get = cfg.get
    mode = get("bandwidth.upload_mode", "none")
    return BandwidthState(
        download_kb=(get("bandwidth.download_kb")
                     if get("bandwidth.limit_download", False) else None),
        upload_kb=get("bandwidth.upload_kb") if mode == "limit" else None,
        upload_auto=(mode == "auto"),
        auto_percent=get("bandwidth.auto_percent", 70) or 70,
    )


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
    from onedriveui.platform.iopool import instance as io_pool
    from onedriveui.platform.power import PowerPolicy
    from onedriveui.rc.auth import AuthFlow
    from onedriveui.rc.client import RcClient
    from onedriveui.rc.daemon import RcdSupervisor
    from onedriveui.rc.mountd import MountController
    from onedriveui.rc.stats import StatsPoller, VfsStatsPoller
    from onedriveui.sync.bandwidth import BandwidthController
    from onedriveui.sync.activity import ActivityFeed
    from onedriveui.sync.decisions import DecisionCenter
    from onedriveui.sync.filestate import FileStateService
    from onedriveui.sync.issues import IssueEngine
    from onedriveui.sync.pause import PauseManager
    from onedriveui.sync.pinner import Pinner
    from onedriveui.sync.accounts import AccountManager
    from onedriveui.sync.browse import RemoteBrowser
    from onedriveui.sync.quota import QuotaService
    from onedriveui.sync.selective import SelectiveSync
    from onedriveui.sync.supervisor import Supervisor

    cfg = cfg if cfg is not None else config.load()
    if writer is None:
        writer = WRITER
        writer.start_writer()

    systemd = SystemdAdapter()
    rcd = RcdSupervisor(systemd,
                        rclone_path=cfg.get("advanced.rclone_path",
                                            "/usr/bin/rclone"))
    # `rclone_path` as well: it was passed to the daemon and not to the mount,
    # so the "Which rclone" setting moved the control plane onto a different
    # binary while every mount kept running the default one.
    mountd = MountController(systemd,
                             options=lambda acc: _mount_options(cfg, acc),
                             rclone_path=cfg.get("advanced.rclone_path",
                                                 "/usr/bin/rclone"))
    power = PowerPolicy()

    def rc_endpoint() -> Any:
        return rcd.endpoint()

    def mount_endpoint() -> Any:
        return mountd.endpoint(account)

    # The live view of what rclone is doing comes from the MOUNT's own rc, not
    # from the control plane. A file written into the mount is uploaded by the
    # mount process itself, so that process is the only one that can see the
    # transfer while it is happening or the queue behind it — the control daemon
    # knows nothing about either.
    #
    # Port 0 until `bring_up()` learns the real one from `mountd`. Nothing polls
    # before then: `attach_live()` is what starts these.
    mount_client = RcClient(RcEndpoint(kind="mount", host="127.0.0.1", port=0,
                                       account_id=account.id))
    # A second client for the control plane. Sign-in runs `config/create` there,
    # not against the mount, and it must not block the GUI thread while the user
    # is away in a browser.
    rcd_client = RcClient(RcEndpoint(kind="rcd", host="127.0.0.1", port=0))
    # `group=None` on purpose, against `StatsPoller`'s own advice. That warning
    # is about the control plane, where a global `core/stats` sums every
    # operation the client ever ran and no progress bar can ever complete. The
    # mount is the opposite case: its uploads belong to no named group, so a
    # group filter would match nothing at all. Both consumers here read
    # `transferring[]`, which is the live in-flight list and is per-transfer
    # accurate either way; the cumulative aggregates are not used for state.
    stats = StatsPoller(mount_client, account_id=account.id, group=None,
                        writer=writer)
    vfs_probe = VfsStatsPoller(mount_client)

    # Re-authorisation. Without this the token simply expires and the client is
    # stuck: the tray goes to "Sign in required", the Activity Center shows a
    # correctly-worded Sign in button and the toast carries the same action —
    # and all three reached `_do_sign_in`, which logged one warning and
    # returned. There was no way to sign in again from the application at all.
    auth = AuthFlow(rcd_client)

    quota = QuotaService(account, endpoint=rc_endpoint)
    # Both daemons: `core/bwlimit` is per-process, so throttling only the
    # control plane would leave the mount — which does the actual uploading —
    # running flat out. A callable because the mount's port changes on restart.
    def live_endpoints() -> list[Any]:
        found = []
        for ep in (rcd.endpoint(), mountd.endpoint(account)):
            if ep is not None and getattr(ep, "port", 0):
                found.append(ep)
        return found

    bandwidth = BandwidthController(endpoints=live_endpoints, writer=writer)
    pause = PauseManager(account, writer=writer,
                         config_get=lambda key, default=None: cfg.get(key, default))
    issues = IssueEngine(account, writer=writer)
    activity = ActivityFeed(account, writer=writer, issues=issues)
    decisions = DecisionCenter(account, writer=writer)
    # `submit` is how hydration leaves the GUI thread. Without it the Pinner
    # ran a 4 MiB-at-a-time read of every pinned file — and the whole-drive walk
    # behind "Always keep on this device" — inline on the thread painting the
    # UI, which is the freeze §7.3 exists to prevent.
    pinner = Pinner(account, endpoint=mount_endpoint, writer=writer,
                    issues=issues, activity=activity,
                    submit=lambda fn: io_pool().submit(fn, kind="hydrate"))
    filestate = FileStateService(account, endpoint=mount_endpoint, writer=writer)

    # The two Settings actions that confirmed and then did nothing. `services`
    # never carried either key, so `page_account` looked them up, got `None`,
    # and returned — "Choose folders" discarded the user's selection and
    # "Unlink this PC" showed its confirmation and unlinked nothing.
    selective = SelectiveSync(account, writer=writer,
                              evict=lambda rel: pinner.free_up_space(rel))
    accounts_mgr = AccountManager(endpoint=rc_endpoint, writer=writer,
                                  stop_mount=mountd.unmount)
    browser = RemoteBrowser(account, endpoint=rc_endpoint)

    notifier = None
    ipc = None
    if not headless:
        from onedriveui.platform.notify import Notifier

        notifier = Notifier()
        # One server for the process. The socket path is process-wide, so a
        # second account building its own would find the path taken and fail to
        # listen — silently taking the file manager integration down for both.
        ipc = _shared_ipc()

    pool = io_pool()
    #: Filled in immediately below; the quota job needs the Supervisor that is
    #: being constructed with it.
    engine_ref: dict[str, Any] = {}

    supervisor = Supervisor(
        account, rcd=rcd, mountd=mountd, pause=pause, quota=quota,
        power=power, issues=issues, pinner=pinner, notifier=notifier,
        ipc=ipc, writer=writer,
        # Four inputs the collector has always declared and never been given.
        # Without them `FactCollector` sees no transfers, an upload queue of
        # zero and no hydration jobs, so the ladder can only ever decide
        # "up to date" — the client was structurally incapable of reporting
        # that it was syncing.
        stats=stats,
        vfs_stats=lambda: vfs_probe.last,
        pin_jobs=pinner.active,
        auth=auth,
        # `QuotaService.refresh` documents itself as "Blocking, and therefore
        # for an IOPool worker: the GUI thread reads current() instead" — and
        # was being called straight off the tick, which IS the GUI thread. It is
        # a round trip to Microsoft, so a slow network froze the tray for as
        # long as the cloud took to answer. ARCHITECTURE §7.6 bans exactly this.
        jobs_runner={"quota": lambda: _refresh_quota(pool, quota, engine_ref),
                     "prune": lambda: _prune(writer, decisions)})

    # The engine is handed to the services that need to act through it, after
    # the Supervisor exists. `do()` is the single entry point, so this back
    # reference is how a fix offered by an issue reaches the same guards as the
    # identical menu item.
    issues._supervisor = supervisor
    engine_ref["supervisor"] = supervisor

    # Whether this account is meant to have a mount at all. Without it an
    # account whose mount is deliberately off reports the mount as DOWN for
    # ever, and the ladder shows a permanent error for a state the user chose.
    # `FactCollector.set_mount_enabled` existed and had no caller.
    supervisor.collector.set_mount_enabled(
        bool(cfg.get("mount.enabled", True, account_id=account.id)))

    # The file-manager integration, which was built and then never switched on.
    if ipc is not None:
        _wire_ipc(ipc, account, filestate, supervisor)

    return Engine(account=account, writer=writer, supervisor=supervisor,
                  desired_bandwidth=_bandwidth_state(cfg),
                  services={"rcd": rcd, "mountd": mountd, "power": power,
                            "quota": quota, "pause": pause, "issues": issues,
                            "activity": activity, "decisions": decisions,
                            "pinner": pinner, "filestate": filestate,
                            "selective": selective, "accounts": accounts_mgr,
                            "browse": browser,
                            "notifier": notifier, "ipc": ipc,
                            "mount_client": mount_client,
                            "rcd_client": rcd_client, "auth": auth,
                            "stats": stats,
                            "vfs_probe": vfs_probe, "bandwidth": bandwidth,
                            "pool": pool})


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
        #: `start()` may run twice — once for the wizard, once for real.
        self._bus_connected = False
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
        # `integrity_check` says "Run once at startup, before DbWriter opens its
        # connection" and had no caller anywhere. A `state.db` damaged by a
        # power cut therefore made every future launch fail on the first query,
        # permanently, with no way out but deleting the file by hand — while the
        # repair that renames it aside and recreates it sat right there.
        try:
            if not db.integrity_check(paths.db_file()):
                log.warning("state.db was corrupt; it has been set aside and a "
                            "fresh one created")
        except Exception:  # noqa: BLE001 - a failed check must not block start-up
            log.warning("could not verify state.db", exc_info=True)
        # `WRITER`, not a second `DbWriter`. The data layer's `_w(None)` falls
        # back to that module singleton, and it was never started — so every
        # repository call made without an explicit writer raised SafetyRefusal
        # into a swallowing `except`. "Don't show this again" was the visible
        # casualty: the tick was recorded nowhere and the dialog came back every
        # time. Two writer threads on one SQLite file was the other half of it.
        self.writer = WRITER
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
        # Adopt an existing instance rather than failing — `_run_gui` creates
        # one before us so the single-instance guard can run before any database
        # is opened — but still apply our identity to it. Returning early here
        # skipped `setDesktopFileName`, which is what links our windows to the
        # .desktop entry: without it the shell shows "python3".
        self.qt = existing if existing is not None else (
            QCoreApplication(argv) if self.headless else QApplication(argv))
        self.qt.setApplicationName(self.NAME)
        if not self.headless and hasattr(self.qt, "setDesktopFileName"):
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
        """Every configured account that is **enabled**.

        `enabled` is not decoration: `start()` builds an engine, a tray icon and
        a mount unit for everything this returns. An account switched off in
        Settings that still came back from here would put a second icon in the
        tray and a second FUSE mount on the disk — which is exactly what a
        left-over entry did, with the mount pointed at a live remote somebody
        else was already mounting.
        """
        out: list[AccountInfo] = []
        for entry in getattr(self.config, "accounts", []) or []:
            to_info = getattr(entry, "to_account_info", None)
            if not callable(to_info):
                continue
            info = to_info()
            if not getattr(info, "enabled", True):
                log.info("account %s is disabled; no engine, tray or mount",
                         info.id)
                continue
            out.append(info)
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
        self._sync_autostart()
        # Guarded: `start()` runs a second time when first-run setup finishes,
        # and a duplicate connection would deliver every config change twice.
        if not self._bus_connected:
            BUS.config_changed.connect(self._on_config_changed)
            self._bus_connected = True

        # A first run has no account, and therefore nothing to start. The setup
        # wizard exists for exactly this and was constructed by no production
        # code path at all, so a fresh install showed no window, no tray icon
        # and no way to sign in — it simply did nothing and exited. Headless
        # commands are exempt: `--state` answers a question, and popping a
        # seven-page dialog in front of someone who asked one is not an answer.
        if not self.headless and not self.accounts():
            self._run_wizard()
            return

        for account in self.accounts():
            engine = self.engine_for(account)
            if not self.headless:
                self._build_ui(account, engine)
            # Re-point the live view whenever the mount comes back. A mount
            # restart keeps the unit and therefore the port, but a re-provision
            # (`ensure_mounted`, which the wizard can reach after engines
            # exist) picks a fresh one — and pollers left aiming at the old port
            # would report a healthy mount as silent forever. `attach_live()`
            # is idempotent, so re-running it costs a re-point and nothing else.
            BUS.mount_health.connect(
                lambda acc_id, health, e=engine: self._on_mount_health(
                    acc_id, health, e))
            # Slow the pollers while sync is paused. Both accept `set_paused`
            # and neither was ever told, so `TICK_PAUSED_MS` was dead and a
            # paused account kept polling the mount two or three times a second
            # to watch a queue that is deliberately not moving.
            BUS.state_changed.connect(
                lambda _old, new, _facts, e=engine: self._on_state_paused(new, e))
            for problem in engine.bring_up():
                log.warning("%s did not come up — %s", account.id, problem)
            engine.supervisor.start()

    def _run_wizard(self) -> None:
        """Show first-run setup, and start for real once it finishes.

        The wizard writes the account into `config.json`; when it reports back,
        the config is re-read and `start()` runs again — this time with an
        account to build an engine for.
        """
        from onedriveui.rc.daemon import RcdSupervisor
        from onedriveui.rc.mountd import MountController
        from onedriveui.ui.wizard import SetupWizard

        # The wizard's finalize step starts the control daemon and the mount; it
        # reads both out of `services` and does nothing without them, so a
        # wizard built with none would walk the user through seven pages and
        # then set nothing up. There is no account yet, so these are built here
        # rather than taken from an engine that cannot exist.
        systemd = SystemdAdapter()
        rclone = self.config.get("advanced.rclone_path", "/usr/bin/rclone")
        services = {
            "rcd": RcdSupervisor(systemd, rclone_path=rclone),
            "mountd": MountController(
                systemd, rclone_path=rclone,
                options=lambda acc: _mount_options(self.config, acc)),
        }
        wizard = SetupWizard(None, config=self.config, services=services)
        self._windows["__wizard__"] = wizard

        # Sign-in. `sign_in_requested` was emitted by the wizard's own button
        # and connected by nothing, and no production code anywhere wrote an
        # account into `config.json` — so the wizard could not produce the one
        # thing it exists to produce. These two halves are that path.
        from onedriveui.models import RcEndpoint
        from onedriveui.rc.auth import AuthFlow
        from onedriveui.rc.client import RcClient

        rcd = services["rcd"]
        rcd_client = RcClient(RcEndpoint(kind="rcd", host="127.0.0.1", port=0),
                              parent=wizard)
        auth = AuthFlow(rcd_client, parent=wizard)
        # Parented to the wizard on purpose: `AuthFlow` polls every 250 ms and
        # `RcClient` owns a QNetworkAccessManager, and a bare QObject dropped
        # mid-flight is the lifetime bug this rewire has already hit twice.
        services["auth"] = auth

        def sign_in() -> None:
            try:
                rcd.ensure_running()
                endpoint = rcd.endpoint()
                if endpoint is not None and getattr(endpoint, "port", 0):
                    rcd_client.set_endpoint(endpoint)
                auth.start(WIZARD_REMOTE)
            except Exception:  # noqa: BLE001 - reported, never a crash
                log.exception("could not start sign-in")
                BUS.auth_finished.emit(False, "could not start sign-in")

        def signed_in(ok: bool, message: str) -> None:
            """Record the remote rclone just created as this client's account."""
            if not ok:
                log.warning("sign-in did not complete: %s", message)
                return
            try:
                account = config.AccountConfig(id=WIZARD_REMOTE,
                                               remote=WIZARD_REMOTE)
                account.sync_root = str(paths.default_sync_root())
                self.config.accounts = [
                    a for a in self.config.accounts if a.id != account.id
                ] + [account]
                self.config.app.active_account_id = account.id
                config.save(self.config, emit=False)
                wizard.account = account.to_account_info()
                log.info("signed in; %s is now configured", account.id)
            except Exception:  # noqa: BLE001 - the user is told by the wizard
                log.exception("could not record the new account")

        wizard.sign_in_requested.connect(sign_in)
        auth.finished.connect(signed_in)

        def done(report: Any) -> None:
            self.config = config.load()
            self._windows.pop("__wizard__", None)
            wizard.close()
            if self.accounts():
                self.start()
                return
            # Nothing was configured. Leaving the process alive would leave an
            # invisible client holding the single-instance socket, so every
            # later launcher click would be swallowed by a window that is not
            # there. Say so and go.
            errors = "; ".join(getattr(report, "errors", ()) or ())
            log.error("setup finished without configuring an account%s",
                      f": {errors}" if errors else "")
            self.quit()

        wizard.finished.connect(done)
        wizard.show()
        wizard.raise_()
        wizard.activateWindow()

    def _on_state_paused(self, state: Any, engine: Any) -> None:
        """Tell this account's pollers whether sync is paused."""
        paused = state in _PAUSED_STATES
        for name in ("stats", "vfs_probe"):
            poller = engine.services.get(name)
            if poller is None:
                continue
            try:
                poller.set_paused(paused)
            except Exception:  # noqa: BLE001 - cadence is not worth a crash
                log.debug("could not set the %s cadence", name, exc_info=True)

    def _on_mount_health(self, account_id: str, health: Any, engine: Any) -> None:
        """Re-attach the pollers when this account's mount is serving again."""
        from onedriveui.models import MountHealth

        if account_id != engine.account.id or health is not MountHealth.UP:
            return
        engine.attach_live()

    def _on_config_changed(self, key: str) -> None:
        """Config keys whose effect lives outside the config file."""
        if key in ("app.autostart", "app.autostart_method"):
            self._sync_autostart()
        elif key.startswith("bandwidth."):
            self._apply_bandwidth()

    def _apply_bandwidth(self) -> None:
        """Push the configured limit to every daemon, now.

        The limit was computed once when the engine was built and applied once
        when the mount came up, so moving the spinner wrote `config.json` and
        changed nothing until the next launch. `core/bwlimit` takes effect
        immediately, which is the whole reason it exists.
        """
        state = _bandwidth_state(self.config)
        for engine in self._engines.values():
            engine.desired_bandwidth = state
            # A user change always applies, whatever the launch already sent.
            engine._bandwidth_applied = True
            bandwidth = engine.services.get("bandwidth")
            pool = engine.services.get("pool")
            if bandwidth is None:
                continue
            if pool is not None:
                pool.submit(bandwidth.apply, state, kind="rc")
            else:
                bandwidth.apply(state)

    def _sync_autostart(self) -> None:
        """Make the disk agree with ``app.autostart``.

        The *Start OneDrive when I sign in* switch writes its dotted key and
        nothing else — every control on that Settings page goes through one
        uniform ``_write``, which is what keeps "immediate apply" a property of
        the page rather than something each toggle remembers. Turning that key
        into a systemd unit or an XDG entry is the composition root's job, and
        it belongs here for two reasons: the toggle has no business knowing what
        a unit file is, and only a reconcile at start-up can fix a machine whose
        config says ``true`` while the disk holds neither mechanism — the state
        a restored backup, or a config written by a script, leaves behind.

        Never raises. An autostart entry that could not be written is worth a
        log line and nothing more; refusing to start the client over it would
        trade a missing convenience for a missing client.
        """
        if self.headless:
            # `--state` and friends answer a question and exit. Installing a
            # login unit as a side effect of one is not what was asked for.
            return
        from onedriveui.platform import autostart

        want = bool(self.config.get("app.autostart", False))
        wanted_method = str(self.config.get("app.autostart_method",
                                            autostart.METHOD_SYSTEMD))
        try:
            installed = autostart.method()
            target = wanted_method if want else autostart.METHOD_NONE
            if installed == target:
                return
            now = autostart.set_enabled(want, wanted_method)
            log.info("autostart reconciled: %s -> %s", installed, now)
        except Exception:  # noqa: BLE001 - never fail to start over autostart
            log.warning("could not apply app.autostart=%s (%s)", want,
                        wanted_method, exc_info=True)

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
        """Show the Activity Center for an account.

        Through :meth:`ActivityCenter.open_`, never a bare ``show()``. The
        window is a flyout, and everything that makes it one lives in `open_`:
        it re-reads the supervisor, re-measures its own height, and moves itself
        to the bottom right of the work area. A ``show()`` skips all three, so
        the flyout appears wherever the compositor last left it, carrying the
        state it was built with — and asking for it a second time while it is
        already up does nothing at all, because ``show()`` on a visible window
        is a no-op and Mutter is free to ignore ``raise_()`` on a ``Qt.Tool``
        surface. Re-opening an open flyout re-places and refreshes it.
        """
        from onedriveui.ui.activity_center import ActivityCenter

        engine = self._engines.get(account_id)
        if engine is None:
            return None
        window = self._windows.get(account_id)
        if window is None:
            window = ActivityCenter(engine.account, supervisor=engine.supervisor,
                                    quota=engine.services.get("quota"),
                                    activity=engine.services.get("activity"))
            # Without these the header gear and the footer's Settings and Help
            # buttons emit into nothing — they look like dead controls, because
            # that is exactly what they are. The window declares the signals;
            # only the composition root knows what a window is.
            window.settings_requested.connect(
                lambda _=None, a=account_id: self.open_settings(a))
            window.help_requested.connect(self._open_help)

            # The recovery banner. `NoticeCenter.set_banner()` emits
            # `banner_changed` on every state change and nothing was listening,
            # so the flyout offered no way out of ERROR or NEEDS_ATTENTION: the
            # notice router knew the fix, computed the buttons, and published
            # them into a signal with no subscriber.
            notices = engine.services.get("notices")
            if notices is not None:
                notices.banner_changed.connect(
                    lambda notice, w=window: _show_banner(w, notice))
                _show_banner(window, notices.banner())

            self._windows[account_id] = window
        window.open_()
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
        # ARCHITECTURE §7.5, in order. `engine.stop()` rather than
        # `engine.supervisor.stop()`: the engine also stops the transfer poller
        # and the VFS probe, each of which owns a QTimer and may have a request
        # in flight. Stopping only the supervisor left both polling the mount
        # while the rest of the application was being torn down around them, and
        # a reply landing in a half-dismantled engine is the classic
        # shutdown crash.
        for engine in self._engines.values():
            try:
                engine.stop()
            except Exception:  # noqa: BLE001 - we are exiting either way
                log.debug("could not stop the engine for %s",
                          engine.account.id, exc_info=True)

        # Step 2: cancel every IOPool token and wait for the workers. Without
        # this a hydration or a cache walk keeps reading through the FUSE mount
        # while the process is closing its database behind it.
        try:
            from onedriveui.platform import iopool

            iopool.instance().shutdown()
        except Exception:  # noqa: BLE001
            log.debug("could not stop the IO pool", exc_info=True)

        for tray in self._tray.values():
            tray.hide()
        if self._pump is not None:
            from onedriveui.platform import glibpump

            glibpump.shutdown()
        if self.writer is not None:
            self.writer.stop()
        BUS.log_line.emit("shutdown")
        self.qt.quit()
