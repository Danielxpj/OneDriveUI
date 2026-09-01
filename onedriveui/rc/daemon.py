"""The control plane: ``onedriveui-rcd.service`` and the ownership proof.

An rc daemon is equivalent to shell access as this user — ``core/command`` runs
arbitrary rclone command lines and ``config/*`` hands back the OAuth token — and
this machine is already running a stranger's rclone on 127.0.0.1:5572 with
``--rc-no-auth``. So OneDriveUI never drives an endpoint it has not *proved* is
its own, and a daemon that fails the proof raises
:class:`~onedriveui.errors.DaemonForeign` rather than being adopted, reconfigured
or quit.

The proof, strongest evidence first (ARCHITECTURE §5.5):

1. ``rc/noop`` answers. A listening port is not evidence of anything: ``core/quit``
   leaves a unix socket file behind, and *any* rclone started with ``--rc``
   exposes all 101 endpoints.
2. ``core/pid`` → ``/proc/<pid>/cmdline`` contains ``rcd`` **and** our exact
   ``--rc-addr 127.0.0.1:<port>``. The foreign daemon here fails on both counts:
   its argv says ``mount``, and its address is 5572.
3. ``/proc/<pid>/stat`` field 22 matches the start time we recorded, so a
   recycled pid cannot impersonate ours.
4. ``job/list`` → ``executeId``. A change in that UUID *is* the definition of
   "the daemon restarted": every job id, mount, VFS and byte of transfer history
   is gone.

``platform/systemd.py`` (WP-10) is **injected**, never imported: the two packages
are built in parallel, and injecting the service manager also makes every code
path here testable without touching the real user manager.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from PySide6.QtCore import QObject, Signal

from onedriveui import USER_AGENT
from onedriveui.bus import BUS
from onedriveui.constants import (
    ORDERING_DAEMON,
    RC_FORBIDDEN_PORTS,
    RC_JOB_EXPIRE,
    RC_JOB_EXPIRE_INTERVAL,
    RCD_FAILURE_WINDOW_S,
    RCD_MAX_FAILURES,
    RCD_RESTART_LADDER_S,
    STARTUP_GRACE_S,
    UNIT_RCD,
)
from onedriveui.errors import DaemonForeign, DaemonUnavailable, RcError
from onedriveui.models import DaemonHealth, RcEndpoint
from onedriveui.rc import RCLONE_DEFAULT, read_proc_cmdline, read_proc_starttime
from onedriveui.rc import endpoints as _endpoints
from onedriveui.rc.client import call_blocking, is_alive

__all__ = ["RcdSupervisor", "SystemdLike", "execute_id_of", "unit_escape"]

log = logging.getLogger(__name__)

#: How long to wait for `rc/noop` after `systemctl start`, and how often to poll.
#: rclone binds its listener before it does any backend work, so it answers well
#: inside a second; the budget exists for a cold page cache, not for the network.
_POLL_INTERVAL_S = 0.1


class SystemdLike(Protocol):
    """The slice of ``platform/systemd.py`` (WP-10) this module needs.

    Kept as a Protocol so ``RcdSupervisor`` depends on the *shape* rather than
    the module, which lets WP-02 and WP-10 be built in parallel and lets tests
    substitute a recorder. The real implementation drives
    ``org.freedesktop.systemd1`` over the session bus.
    """

    def write_unit(self, name: str, text: str) -> Any: ...
    def daemon_reload(self) -> Any: ...
    def enable(self, name: str) -> Any: ...
    def start(self, name: str) -> Any: ...
    def stop(self, name: str) -> Any: ...
    def restart(self, name: str) -> Any: ...
    def is_active(self, name: str) -> bool: ...
    def status_text(self, name: str) -> str: ...


def unit_escape(text: str) -> str:
    """Escape a literal for a systemd unit file value.

    ``%`` introduces a specifier (``%h``, ``%i``, …) and must be doubled, or a
    home directory containing one silently expands to something else.

    Args:
        text: The literal.

    Returns:
        The escaped literal.
    """
    return str(text).replace("%", "%%")


def execute_id_of(ep: RcEndpoint, timeout_s: float = 2.0) -> str:
    """Read the daemon's ``executeId`` — its per-process identity.

    Args:
        ep: The daemon.
        timeout_s: Socket timeout.

    Returns:
        The UUID from ``job/list``, or ``""`` when the daemon did not answer.
        A caller comparing two of these must treat ``""`` as "unknown", never as
        "changed": an unreachable daemon has not necessarily restarted.
    """
    try:
        return str(call_blocking(ep, "job/list", {}, timeout_s=timeout_s).get(
            "executeId", ""))
    except (RcError, OSError):
        return ""


class RcdSupervisor(QObject):
    """Owns ``onedriveui-rcd.service``: provision, adopt, prove, restart.

    Attributes:
        restarted: Emitted with the new ``executeId`` whenever the daemon is
            observed to have restarted. Every holder of a
            :class:`~onedriveui.models.JobHandle` must drop it on this signal.
    """

    restarted = Signal(str)

    def __init__(self, systemd: SystemdLike, *,
                 rclone_path: str = RCLONE_DEFAULT,
                 user_agent: str = USER_AGENT,
                 job_expire: str = RC_JOB_EXPIRE,
                 job_expire_interval: str = RC_JOB_EXPIRE_INTERVAL,
                 unit_name: str = UNIT_RCD,
                 startup_grace_s: float = STARTUP_GRACE_S,
                 parent: QObject | None = None) -> None:
        """
        Args:
            systemd: The injected service manager (:class:`SystemdLike`).
            rclone_path: ``advanced.rclone_path``.
            user_agent: ``advanced.user_agent``. The
                ``ISV|Company|App/Version`` shape is load-bearing for Microsoft's
                throttle prioritisation — never reformat it.
            job_expire: ``--rc-job-expire-duration``. rclone's 60 s default
                garbage-collects a finished job's ``output`` before a restarted
                GUI can read it, so this application uses 10 m.
            job_expire_interval: ``--rc-job-expire-interval``.
            unit_name: The unit to write and drive.
            startup_grace_s: How long :meth:`ensure_running` waits for
                ``rc/noop`` after starting the unit.
            parent: Owner, for Qt lifetime.
        """
        super().__init__(parent)
        self._systemd = systemd
        self._rclone_path = rclone_path
        self._user_agent = user_agent
        self._job_expire = job_expire
        self._job_expire_interval = job_expire_interval
        self._unit = unit_name
        self._grace = float(startup_grace_s)
        self._endpoint: RcEndpoint | None = None
        #: An endpoint that answered on our port and failed the ownership proof.
        #: Remembered — never driven — so `health()` keeps reporting FOREIGN and
        #: the UI can say *why* nothing is working, instead of a bare "down".
        self._foreign: RcEndpoint | None = None
        self._health = DaemonHealth.DOWN
        self._failures: list[float] = []

    # ── unit text ───────────────────────────────────────────────────────────

    @staticmethod
    def unit_text(port: int, user: str, password: str,
                  *, rclone_path: str = RCLONE_DEFAULT,
                  user_agent: str = USER_AGENT,
                  job_expire: str = RC_JOB_EXPIRE,
                  job_expire_interval: str = RC_JOB_EXPIRE_INTERVAL) -> str:
        """Render ``onedriveui-rcd.service`` (ARCHITECTURE §5.2).

        Args:
            port: The loopback port to bind.
            user: ``--rc-user``.
            password: ``--rc-pass``. Written into a unit file under
                ``~/.config/systemd/user``; the file is not world-readable, and
                the password is regenerated on every provision.
            rclone_path: The rclone binary.
            user_agent: ``--user-agent``.
            job_expire: ``--rc-job-expire-duration``.
            job_expire_interval: ``--rc-job-expire-interval``.

        Returns:
            The complete unit text.

            ``network-online.target`` is deliberately **absent**: it does not
            exist in the systemd ``--user`` manager (``LoadState=not-found``) and
            ``After=``/``Wants=`` on it are silently ignored, so emitting it
            would only mislead the next maintainer. rclone's own retry logic
            covers the boot-time network race, and ``Restart=always`` covers the
            rest.
        """
        exec_start = " ".join(
            unit_escape(token) for token in (
                rclone_path, "rcd",
                "--rc-addr", f"127.0.0.1:{int(port)}",
                "--rc-user", user, "--rc-pass", password,
                "--rc-job-expire-duration", job_expire,
                "--rc-job-expire-interval", job_expire_interval,
                "--rc-server-write-timeout", "1h",
                "--user-agent", user_agent,
                "--use-json-log", "--color", "NEVER",
                "--log-level", "INFO", "--stats", "0",
            )
        )
        return (
            "[Unit]\n"
            f"Description=OneDriveUI rclone control plane\n"
            f"Documentation=https://rclone.org/rc/\n"
            "PartOf=graphical-session.target\n"
            f"{ORDERING_DAEMON}"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_start}\n"
            "Restart=always\n"
            "RestartSec=5\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    # ── ownership ───────────────────────────────────────────────────────────

    @staticmethod
    def verify_ownership(ep: RcEndpoint) -> bool:
        """Is the daemon answering on ``ep`` provably the one we configured?

        Args:
            ep: The endpoint to prove. ``ep.kind`` selects the second half of the
                argv test: an ``"rcd"`` argv must contain ``rcd``, a ``"mount"``
                argv must contain the mountpoint.

        Returns:
            True only if ``core/pid`` answered, ``/proc/<pid>/cmdline`` contains
            ``--rc-addr`` **and** our exact ``host:port`` **and** the kind-specific
            token, and — when ``ep.starttime`` was recorded — ``/proc/<pid>/stat``
            field 22 still matches.

            False for the foreign ``rclone mount … --rc-addr 127.0.0.1:5572`` on
            this machine, whose argv says ``mount``, whose address is 5572, and
            which must never be driven or quit.

            Always False for a port in
            :data:`~onedriveui.constants.RC_FORBIDDEN_PORTS`, whatever the argv
            says. :func:`~onedriveui.rc.endpoints.pick_free_port` can never hand
            one of those out, so an endpoint claiming one is stale, hand-edited
            or hostile — and on this machine 5572 is a *foreign* mount whose argv
            legitimately contains both our mountpoint and its own ``--rc-addr``,
            so the argv proof alone would adopt it.

            Never raises: an unreachable daemon, a vanished pid and an
            unreadable ``/proc`` all mean "not proven", which is the safe answer.
        """
        if not ep.port:
            return False
        if int(ep.port) in RC_FORBIDDEN_PORTS:
            log.warning(
                "endpoint %s claims forbidden port %d; refusing to prove "
                "ownership — that port belongs to someone else",
                ep.kind, ep.port)
            return False
        try:
            pid = int(call_blocking(ep, "core/pid", {}, timeout_s=1.0).get("pid", 0))
        except (RcError, OSError, TypeError, ValueError):
            return False
        if pid <= 0:
            return False

        argv = read_proc_cmdline(pid)
        if not argv:
            return False
        joined = " ".join(argv)
        if "--rc-addr" not in argv:
            return False
        if f"{ep.host}:{ep.port}" not in joined:
            return False
        if ep.kind == "rcd":
            if "rcd" not in argv:
                log.warning(
                    "a daemon answers on %s but its argv is %r, not 'rcd'; it is "
                    "foreign and must never be driven", ep.base_url, argv[:3])
                return False
        else:
            if not ep.mountpoint or str(ep.mountpoint) not in joined:
                return False

        if ep.starttime:
            observed = read_proc_starttime(pid)
            if observed != ep.starttime:
                log.warning(
                    "pid %d on %s has starttime %d, not the recorded %d; the pid "
                    "was recycled", pid, ep.base_url, observed, ep.starttime)
                return False
        return True

    @staticmethod
    def _identify(ep: RcEndpoint) -> RcEndpoint:
        """Stamp ``pid``, ``starttime`` and ``execute_id`` onto a proven endpoint."""
        try:
            pid = int(call_blocking(ep, "core/pid", {}, timeout_s=1.0).get("pid", 0))
        except (RcError, OSError, TypeError, ValueError):
            pid = 0
        return _endpoints.with_identity(
            ep, pid=pid, starttime=read_proc_starttime(pid) if pid else 0,
            execute_id=execute_id_of(ep))

    # ── lifecycle ───────────────────────────────────────────────────────────

    def endpoint(self) -> RcEndpoint | None:
        """The proven endpoint, adopting a persisted one if we have none.

        Returns:
            The endpoint, or ``None`` when nothing is running.

        **Adoption is the "adopt" in provision/adopt/prove/restart.** A process
        that did not itself call :meth:`ensure_running` — ``onedriveui --status``,
        ``--doctor``, a second window, anything short-lived — otherwise reports
        ``DOWN`` for a daemon that is demonstrably alive, because the only
        record of it was in a field this instance never filled.
        :class:`~onedriveui.rc.mountd.MountController` has always read the
        endpoints file for exactly this reason; this is the same move.

        Adopting is safe because it proves nothing by itself: the endpoint still
        has to pass :meth:`verify_ownership` in :meth:`health` before it is
        treated as ours, and that proof checks the pid, the argv, the exact
        ``--rc-addr`` and the process start time.
        """
        if self._endpoint is not None:
            return self._endpoint
        if self._foreign is not None:
            # Something on our port has already FAILED the proof. Re-reading the
            # file here would resurrect the very endpoint we just rejected and
            # hand a foreign daemon to callers who would then drive it — which
            # is the one outcome the ownership proof exists to prevent. It stays
            # refused until `health()` observes the stranger go away, which is
            # what clears this field.
            return None
        found = _endpoints.load_endpoints().get(_endpoints.endpoint_key("rcd", ""))
        if found is not None and found.port:
            self._endpoint = found
        return self._endpoint

    def health(self) -> DaemonHealth:
        """Re-observe the daemon and return its health.

        Returns:
            ``UP`` when our proven daemon answers, ``FOREIGN`` when something
            answers on our port and fails the ownership proof, ``DOWN``
            otherwise. Emits ``BUS.daemon_health`` on a change.

            A refused foreign daemon keeps this at ``FOREIGN`` until it goes
            away, because "someone else owns your port" and "nothing is running"
            need different words in the UI and different fixes from the user.
        """
        # `endpoint()`, not `self._endpoint`: a process that never called
        # `ensure_running()` has an empty field and a live daemon, and reporting
        # DOWN for a daemon that is running is the one answer a health check
        # must not give.
        ep = self.endpoint()
        if ep is not None and ep.port:
            if not is_alive(ep, timeout_s=1.0):
                return self._set_health(DaemonHealth.DOWN)
            if not self.verify_ownership(ep):
                self._foreign = ep
                self._endpoint = None
                return self._set_health(DaemonHealth.FOREIGN)
            return self._set_health(DaemonHealth.UP)

        stranger = self._foreign
        if stranger is not None and stranger.port and is_alive(stranger, timeout_s=1.0):
            if not self.verify_ownership(stranger):
                return self._set_health(DaemonHealth.FOREIGN)
        self._foreign = None
        return self._set_health(DaemonHealth.DOWN)

    def _set_health(self, health: DaemonHealth) -> DaemonHealth:
        if health != self._health:
            self._health = health
            BUS.daemon_health.emit("rcd", health)
        return health

    def ensure_running(self) -> RcEndpoint:
        """Adopt the recorded daemon, or provision a new one, and prove it.

        Called once at startup, before any other rc user exists. It blocks — for
        at most ``startup_grace_s`` — which is correct here and nowhere else:
        nothing can be drawn until the control plane answers, and this runs
        beside the equally blocking database open and migration.

        Returns:
            The proven endpoint, with ``pid``, ``starttime`` and ``execute_id``
            filled in and recorded in ``endpoints.json``.

        Raises:
            DaemonForeign: Something is listening on the recorded port and it is
                not ours. Never adopted, never reconfigured, never ``core/quit``ed
                — the caller must surface it to the user.
            DaemonUnavailable: The unit was started but never answered ``rc/noop``
                within the grace period.
        """
        recorded = self._endpoint or _endpoints.load_endpoints().get("rcd")
        if recorded is not None and recorded.port and is_alive(recorded, timeout_s=1.0):
            if not self.verify_ownership(recorded):
                self._foreign = recorded
                self._set_health(DaemonHealth.FOREIGN)
                raise DaemonForeign(
                    f"a daemon is listening on {recorded.base_url} but failed the "
                    f"/proc ownership proof; it is not ours and will not be driven"
                )
            self._endpoint = self._identify(recorded)
            _endpoints.save_endpoint(self._endpoint)
            self._set_health(DaemonHealth.UP)
            log.info("adopted the running rcd on %s (pid %d, executeId %s)",
                     self._endpoint.base_url, self._endpoint.pid,
                     self._endpoint.execute_id or "?")
            return self._endpoint
        return self._provision()

    def _provision(self) -> RcEndpoint:
        """Write, enable and start the unit, then wait for it and prove it."""
        port = _endpoints.pick_free_port(exclude=_endpoints.known_ports())
        user, password = _endpoints.generate_credentials()
        ep = RcEndpoint(kind="rcd", host="127.0.0.1", port=port,
                        user=user, password=password)

        text = self.unit_text(port, user, password,
                              rclone_path=self._rclone_path,
                              user_agent=self._user_agent,
                              job_expire=self._job_expire,
                              job_expire_interval=self._job_expire_interval)
        self._systemd.write_unit(self._unit, text)
        self._systemd.daemon_reload()
        self._systemd.enable(self._unit)
        self._set_health(DaemonHealth.STARTING)
        self._systemd.start(self._unit)

        if not self._wait_alive(ep):
            self._note_failure()
            self._set_health(DaemonHealth.DOWN)
            raise DaemonUnavailable(
                "rc/noop", 503,
                {"error": f"{self._unit} did not answer rc/noop on {ep.base_url} "
                          f"within {self._grace:g}s",
                 "input": {}, "path": "rc/noop", "status": 503})

        if not self.verify_ownership(ep):
            self._foreign = ep
            self._set_health(DaemonHealth.FOREIGN)
            raise DaemonForeign(
                f"we started {self._unit} but the daemon answering {ep.base_url} "
                f"failed the /proc ownership proof; something else took the port")

        self._endpoint = self._identify(ep)
        _endpoints.save_endpoint(self._endpoint)
        self._set_health(DaemonHealth.UP)
        log.info("started rcd on %s (pid %d, executeId %s)",
                 self._endpoint.base_url, self._endpoint.pid,
                 self._endpoint.execute_id or "?")
        return self._endpoint

    def _wait_alive(self, ep: RcEndpoint) -> bool:
        """Poll ``rc/noop`` until it answers or the grace period expires."""
        deadline = time.monotonic() + self._grace
        while True:
            if is_alive(ep, timeout_s=1.0):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_INTERVAL_S)

    def restart(self, reason: str) -> None:
        """Restart the daemon, on the backoff ladder, and report the new identity.

        Args:
            reason: Why, for the log. Recorded verbatim.

        After :data:`~onedriveui.constants.RCD_MAX_FAILURES` restarts inside
        :data:`~onedriveui.constants.RCD_FAILURE_WINDOW_S`, this stops trying and
        returns without acting: a daemon that cannot stay up is a problem for the
        "Report a problem" flow, not for a tighter retry loop. ``health()`` then
        reports ``DOWN``.

        Emits :attr:`restarted` and ``BUS.daemon_restarted`` when the
        ``executeId`` differs from the one we held — which is the signal every
        job handle, mount and stats group is now stale.
        """
        self._note_failure()
        recent = len(self._failures)
        if recent > RCD_MAX_FAILURES:
            log.error("rcd restart refused (%s): %d failures in %ds; giving up",
                      reason, recent, RCD_FAILURE_WINDOW_S)
            self._set_health(DaemonHealth.DOWN)
            return

        delay = RCD_RESTART_LADDER_S[min(recent - 1, len(RCD_RESTART_LADDER_S) - 1)]
        log.warning("restarting rcd (%s); attempt %d, backoff %ds",
                    reason, recent, delay)
        previous = self._endpoint.execute_id if self._endpoint else ""

        self._set_health(DaemonHealth.STARTING)
        self._systemd.restart(self._unit)

        ep = self._endpoint
        if ep is None or not self._wait_alive(ep):
            self._set_health(DaemonHealth.DOWN)
            return
        if not self.verify_ownership(_endpoints.with_identity(ep, starttime=0)):
            self._foreign = ep
            self._endpoint = None
            self._set_health(DaemonHealth.FOREIGN)
            raise DaemonForeign(
                f"after restarting {self._unit}, the daemon on {ep.base_url} "
                f"failed the /proc ownership proof")

        self._endpoint = self._identify(_endpoints.with_identity(ep, starttime=0))
        _endpoints.save_endpoint(self._endpoint)
        self._set_health(DaemonHealth.UP)

        current = self._endpoint.execute_id
        if current and current != previous:
            log.info("rcd executeId %s -> %s: every job id, mount, VFS and byte "
                     "of transfer history is stale", previous or "?", current)
            self.restarted.emit(current)
            BUS.daemon_restarted.emit("rcd", current)

    def stop(self) -> None:
        """Stop the unit and forget the endpoint.

        Only ever the unit we wrote. A foreign daemon is never stopped, and
        ``core/quit`` is never sent to anything: rc access is shell access, and
        quitting a stranger's rclone would unmount the user's real OneDrive.
        """
        try:
            self._systemd.stop(self._unit)
        finally:
            self._endpoint = None
            self._foreign = None
            _endpoints.forget_endpoint("rcd")
            self._set_health(DaemonHealth.DOWN)

    # ── failure accounting ──────────────────────────────────────────────────

    def _note_failure(self) -> None:
        """Record a failure and drop the ones outside the sliding window."""
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < RCD_FAILURE_WINDOW_S]
        self._failures.append(now)

    @property
    def recent_failures(self) -> int:
        """Failures inside the last :data:`RCD_FAILURE_WINDOW_S` seconds."""
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < RCD_FAILURE_WINDOW_S]
        return len(self._failures)
