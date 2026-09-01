"""Sign-in: the rc OAuth walk, the ``rclone authorize`` fallback, token health.

rclone's OAuth for OneDrive is an authorization-code flow against
``login.microsoftonline.com`` driven by a **local webserver on 127.0.0.1:53682**
(``bindPort`` is a compile-time constant in ``lib/oauthutil``; it cannot be
moved). The user is sent to ``http://127.0.0.1:53682/auth?state=…`` first, which
302s to Microsoft, and Microsoft redirects back to ``http://localhost:53682/``
with the code.

Three facts shape everything in this module
(``docs/research/rclone-onedrive-backend.md`` §2, ``rclone-rc-api.md`` §9):

1. **The config endpoints block.** ``config/create`` on an OAuth backend starts
   that webserver and holds the HTTP request open until the callback lands —
   observed hanging for over a minute. So the step that starts the browser flow
   is sent with ``_async: true`` and watched as a job, and every step is sent
   with ``opt.nonInteractive`` so rclone answers with a *question* instead of
   prompting on a terminal that does not exist.
2. **Port 53682 is not ours and may be taken.** rclone cannot be told to use
   another one, and a second rclone already waiting for a callback owns it.
   :func:`callback_port_free` is checked **first**, before anything is started,
   because the failure mode otherwise is a sign-in that hangs with no
   explanation.
3. **``AADSTS65005`` and ``AADSTS50076`` are different states.** 50076 is
   "MFA required" and a fresh sign-in fixes it. 65005 is an unmanaged tenant:
   an administrator must claim the domain by DNS, and re-authenticating will
   fail again forever. Offering "Sign in again" for the second is a loop the
   user cannot escape, which is why :func:`probe_token` maps them to different
   :class:`~onedriveui.models.TokenHealth` values and
   :func:`is_reauth_fixable` exists.

Invariant I14: **nothing here ever logs a token.** ``config/dump`` and
``config/get`` return the refresh token in the clear and are refused by
``rc.guards``; the ``parameters`` and ``result`` of a config call are never
logged; and anything that might carry one goes through
:func:`onedriveui.applog.redact` first.
"""

from __future__ import annotations

import errno
import logging
import re
import socket
from collections.abc import Callable, Mapping
from typing import Any, Final

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from onedriveui.applog import redact
from onedriveui.bus import BUS
from onedriveui.constants import (
    OAUTH_CALLBACK_HOST,
    OAUTH_CALLBACK_PORT,
    TOKEN_KEEPALIVE_S,
)
from onedriveui.errors import (
    AUTH_PATTERNS,
    DaemonUnavailable,
    OneDriveUIError,
    RcError,
)
from onedriveui.models import IssueCode, RcEndpoint, TokenHealth, parse_iso, utcnow_iso
from onedriveui.rc import RCLONE_DEFAULT
from onedriveui.rc.client import call_blocking

__all__ = [
    "AUTHORIZE_END_MARKER",
    "AUTHORIZE_START_MARKER",
    "AuthFlow",
    "AuthorizeFallback",
    "MAX_CONFIG_STEPS",
    "OAUTH_CALLBACK",
    "TokenKeepalive",
    "callback_port_free",
    "classify_auth_error",
    "is_reauth_fixable",
    "keepalive",
    "keepalive_due",
    "parse_authorize_token",
    "parse_authorize_url",
    "probe_token",
    "unlink_account",
]

log = logging.getLogger(__name__)

#: Where rclone's OAuth webserver listens. Fixed in ``lib/oauthutil``: the
#: redirect URI registered with Microsoft is ``http://localhost:53682/``, so it
#: cannot be moved without registering a different Azure application.
OAUTH_CALLBACK: Final[tuple[str, int]] = (OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT)

#: A hard stop on the ``config/create`` question loop. The OneDrive walk asks
#: about four things; anything past this many rounds is rclone and this module
#: disagreeing about the protocol, and looping forever would wedge sign-in.
MAX_CONFIG_STEPS: Final[int] = 25

#: ``rclone authorize`` prints the token blob to **stdout** between these two
#: markers. The link goes to **stderr** — the two channels must be read
#: separately or the blob arrives interleaved with NOTICE lines.
AUTHORIZE_START_MARKER: Final[str] = "Paste the following into your remote machine --->"
AUTHORIZE_END_MARKER: Final[str] = "<---End paste"

#: The NOTICE line ``rclone authorize --auth-no-open-browser`` writes to stderr.
_AUTHORIZE_LINK_RE = re.compile(r"go to the following link:\s*(\S+)", re.I)

#: ``config/create``'s answer to "does this machine have a browser?". Answering
#: ``true`` is what starts the local webserver flow — and what makes that one
#: call block, hence ``_async``.
_IS_LOCAL_OPTION: Final[str] = "config_is_local"

#: The headless branch: rclone wants a token blob pasted in. Reaching it means
#: the walk went the wrong way, because this application always has a browser.
_TOKEN_OPTION: Final[str] = "config_token"

#: :data:`onedriveui.errors.AUTH_PATTERNS` classifies into ``IssueCode``; the
#: account state machine speaks ``TokenHealth``. One table, two vocabularies.
_HEALTH_FOR_CODE: Final[dict[IssueCode, TokenHealth]] = {
    IssueCode.AUTH_TENANT_BLOCKED: TokenHealth.TENANT_BLOCKED,
    IssueCode.AUTH_MFA: TokenHealth.MFA,
    IssueCode.AUTH_EXPIRED: TokenHealth.EXPIRED,
}


# ─────────────────────────────────────────────────────────────────────────────
# The callback port
# ─────────────────────────────────────────────────────────────────────────────

def callback_port_free(host: str = OAUTH_CALLBACK_HOST,
                       port: int = OAUTH_CALLBACK_PORT) -> bool:
    """Can rclone's OAuth webserver bind 127.0.0.1:53682 right now?

    Args:
        host: The interface. Always loopback in production.
        port: The port. Always 53682 in production.

    Returns:
        True when a ``bind()`` succeeded (and was closed again immediately).

    A real bind is the only honest test, and ``SO_REUSEADDR`` is deliberately
    not set: with it, Linux would happily bind a port still in ``TIME_WAIT``
    that a peer considers taken.

    This deliberately does **not** use
    :func:`onedriveui.rc.endpoints.port_is_free`, which refuses 53682
    unconditionally — that refusal is about never binding it *ourselves*, which
    is a different question from whether rclone can.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, int(port)))
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL):
            log.debug("bind probe on %s:%d failed unexpectedly: %s",
                      host, port, exc)
        return False
    finally:
        sock.close()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Token health
# ─────────────────────────────────────────────────────────────────────────────

def classify_auth_error(text: str) -> TokenHealth:
    """Map an rclone/Graph error string onto a token state.

    Args:
        text: The raw error, e.g. an ``operations/about`` failure message.

    Returns:
        :attr:`~onedriveui.models.TokenHealth.TENANT_BLOCKED` for
        ``AADSTS65005``, :attr:`~onedriveui.models.TokenHealth.MFA` for
        ``AADSTS50076``, :attr:`~onedriveui.models.TokenHealth.EXPIRED` for the
        five "no usable token" phrasings, and
        :attr:`~onedriveui.models.TokenHealth.UNKNOWN` for anything else —
        including every network error, which must never be reported as a
        sign-out.

    The table is :data:`onedriveui.errors.AUTH_PATTERNS`, in its documented
    order, so a new phrasing is taught in exactly one place.
    """
    low = (text or "").lower()
    for needle, code in AUTH_PATTERNS:
        if needle in low:
            return _HEALTH_FOR_CODE.get(code, TokenHealth.UNKNOWN)
    return TokenHealth.UNKNOWN


def is_reauth_fixable(health: TokenHealth) -> bool:
    """Would signing in again actually help?

    Args:
        health: A token state.

    Returns:
        True for ``EXPIRED`` and ``MFA``. **False for ``TENANT_BLOCKED``**:
        ``AADSTS65005`` means the domain is unmanaged and an administrator has
        to claim it by DNS, so offering "Sign in" there is a loop the user
        cannot escape. That account gets a link to the web instead
        (``ACTIONS_FOR[AUTH_TENANT_BLOCKED]`` is ``OPEN_WEB``).
    """
    return health in (TokenHealth.EXPIRED, TokenHealth.MFA)


def probe_token(fs: str, *, ep: RcEndpoint,
                timeout_s: float | None = None) -> TokenHealth:
    """Is this account's token usable? The cheapest probe there is.

    Args:
        fs: The remote to probe, e.g. ``"onedrive:"``.
        ep: The daemon to ask.
        timeout_s: Socket timeout.

    Returns:
        ``OK`` when ``operations/about`` answered — which also proves the
        refresh token was accepted, because rclone refreshes on demand. One of
        the four failure states otherwise.

    There is no "is my token valid" command in rclone, so this asks the backend
    for its quota and reads the failure. A daemon that does not answer at all
    yields ``UNKNOWN``, never ``EXPIRED``: an offline laptop has not been
    signed out.
    """
    try:
        if timeout_s is None:
            call_blocking(ep, "operations/about", {"fs": fs})
        else:
            call_blocking(ep, "operations/about", {"fs": fs},
                          timeout_s=timeout_s)
    except DaemonUnavailable:
        return TokenHealth.UNKNOWN
    except RcError as exc:
        health = classify_auth_error(exc.message)
        # Never log exc.message unredacted: an OAuth failure can echo the
        # request, and the request carries the state parameter (I14).
        log.info("token probe for %s: %s (%s)", fs, health.value,
                 redact(exc.message))
        return health
    return TokenHealth.OK


def keepalive_due(last_at: str | None, *, now: str | None = None,
                  interval_s: float = TOKEN_KEEPALIVE_S) -> bool:
    """Has it been long enough to run the keepalive probe again?

    Args:
        last_at: When the token was last exercised, as an ISO stamp. ``None``
            or unparseable means "never", which is due.
        now: The current ISO stamp. Defaults to now.
        interval_s: How often. Defaults to
            :data:`~onedriveui.constants.TOKEN_KEEPALIVE_S` (24 h).

    Returns:
        True when the probe should run.

    Microsoft expires a **refresh token after 90 days of non-use**, and a
    machine that is only ever suspended can go that long without a single API
    call. One ``about`` a day costs nothing and makes that impossible.
    """
    previous = parse_iso(last_at)
    if previous is None:
        return True
    current = parse_iso(now or utcnow_iso())
    if current is None:                              # pragma: no cover - clock
        return True
    return (current - previous).total_seconds() >= float(interval_s)


def keepalive(fs: str, *, ep: RcEndpoint, last_at: str | None = None,
              now: str | None = None, interval_s: float = TOKEN_KEEPALIVE_S,
              force: bool = False) -> TokenHealth | None:
    """Exercise the refresh token if it is time, so it never expires unused.

    Args:
        fs: The remote to probe.
        ep: The daemon to ask.
        last_at: When it was last exercised.
        now: The current ISO stamp, for a deterministic caller.
        interval_s: How often to run. 24 h by default.
        force: Run regardless of ``last_at``.

    Returns:
        The token state when the probe ran, or ``None`` when it was skipped as
        too soon. ``None`` is not an error: the caller keeps its previous
        reading and its previous ``last_at``.

    Blocking: ``IOPool`` only (ARCHITECTURE §7.6).
    """
    if not force and not keepalive_due(last_at, now=now, interval_s=interval_s):
        return None
    return probe_token(fs, ep=ep)


class TokenKeepalive(QObject):
    """Run :func:`keepalive` on a timer, off the GUI thread's critical path.

    Attributes:
        due_now: ``()`` — a probe is owed; run :func:`keepalive` on the pool.
        probed: ``(TokenHealth)`` — what :meth:`report` was told.

    The probe itself is one blocking rc call, so this object never runs it: it
    announces that one is owed and the owner submits :func:`keepalive` to the
    ``IOPool``, then calls :meth:`report` with the answer. That keeps
    ARCHITECTURE §7.6's ban on synchronous HTTP on the GUI thread absolute, with
    no exception carved out for "it is only once a day".
    """

    due_now = Signal()
    probed = Signal(object)

    def __init__(self, *, interval_s: float = TOKEN_KEEPALIVE_S,
                 parent: QObject | None = None) -> None:
        """
        Args:
            interval_s: How often to fire. 24 h by default.
            parent: Qt parent.
        """
        super().__init__(parent)
        self._interval_s = float(interval_s)
        self._last_at: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(self._interval_s * 1000)))
        self._timer.timeout.connect(self.due)

    def start(self, last_at: str | None = None) -> None:
        """Arm the timer.

        Args:
            last_at: When the token was last exercised, so a launch after a long
                suspend fires immediately instead of waiting another day.
        """
        self._last_at = last_at
        self._timer.start()
        if keepalive_due(last_at, interval_s=self._interval_s):
            self.due()

    def stop(self) -> None:
        """Disarm the timer. Idempotent."""
        self._timer.stop()

    @property
    def last_at(self) -> str | None:
        """When the last probe was reported."""
        return self._last_at

    def due(self) -> None:
        """Announce that a probe is owed. Emitted on ``due_now``."""
        self.due_now.emit()

    def report(self, health: TokenHealth, at: str | None = None) -> None:
        """Record the outcome of a probe the owner ran.

        Args:
            health: What :func:`probe_token` answered.
            at: When. Defaults to now.
        """
        self._last_at = at or utcnow_iso()
        self.probed.emit(health)


# ─────────────────────────────────────────────────────────────────────────────
# Unlinking
# ─────────────────────────────────────────────────────────────────────────────

def unlink_account(remote: str, *, ep: RcEndpoint, keep_remote: bool = False,
                   timeout_s: float | None = None) -> bool:
    """Sign out: remove the account's credentials from ``rclone.conf``.

    Args:
        remote: The rclone remote name, with or without a trailing colon.
        ep: The daemon to ask. Going through the rc rather than editing the file
            makes rclone rewrite the config itself, so a running daemon's
            in-memory copy and the file cannot disagree.
        keep_remote: Delete only the ``token`` key, leaving the remote's other
            settings in place, so signing back in does not have to re-answer
            every question. ``False`` removes the whole section.
        timeout_s: Socket timeout.

    Returns:
        True when the remote is gone (or its token is), False when it was not
        configured in the first place.

    Raises:
        RcError: The daemon refused.
        DaemonUnavailable: The daemon did not answer.

    Local files are **not** touched. "Unlink this PC" in the Windows client
    keeps everything on disk, and so does this; unmounting and cache disposal
    are the mount controller's business, not the token's.

    The result is deliberately not verified with ``config/get``: that endpoint
    returns the refresh token in the clear and is refused by
    ``rc.guards.assert_rc_path_allowed`` (I14). ``config/listremotes`` answers
    the same question without exposing anything.
    """
    name = str(remote).rstrip(":")
    if not name:
        raise ValueError("unlink_account needs a remote name")

    def _call(path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if timeout_s is None:
            return call_blocking(ep, path, dict(params))
        return call_blocking(ep, path, dict(params), timeout_s=timeout_s)

    listed = _call("config/listremotes", {}).get("remotes") or []
    if name not in [str(item) for item in listed]:
        log.info("unlink: remote %r is not configured", name)
        return False
    if keep_remote:
        removed = _call("config/unset", {"name": name,
                                         "keys": ["token"]}).get("removed") or []
        log.info("unlink: dropped %d key(s) from [%s]", len(removed), name)
        return True
    _call("config/delete", {"name": name})
    log.info("unlink: removed [%s] from rclone.conf", name)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# The rc OAuth walk
# ─────────────────────────────────────────────────────────────────────────────

class AuthFlow(QObject):
    """Drive rclone's non-interactive config state machine through the rc.

    The walk, verified end to end against v1.75.0:

    1. ``config/create`` with ``opt.nonInteractive`` answers a **question**
       instead of prompting — ``State: "*oauth-islocal,choose_type,,"``,
       ``Option.Name: "config_is_local"``. The remote row is already written to
       ``rclone.conf`` at this point, before the flow completes.
    2. The same endpoint is called again with ``opt.continue``, the ``State``
       echoed back and ``result: "true"``. That answer starts the local OAuth
       webserver and **blocks**, so this one call carries ``_async: true`` and
       comes back as ``{"jobid", "executeId"}``.
    3. ``config/oauthstatus`` is polled until it reports
       ``{"status": "running", "authUrl": "http://127.0.0.1:53682/auth?state=…"}``.
       The URL is emitted and opened in the user's browser.
    4. ``job/status`` is polled until the job finishes. Its ``output`` is the
       next ``State``/``Option`` blob, and the walk continues from step 2 until
       a response carries no ``Option`` — which is how rclone says "done".
    5. Cancelling sends ``config/oauthstop`` and then ``job/stop``. Stopping the
       server is what unblocks the job, which then fails with
       ``oauth authentication was cancelled``.

    Attributes:
        url_ready: ``(str)`` — the ``127.0.0.1:53682`` URL to visit. Mirrored
            onto ``BUS.auth_url_ready``.
        finished: ``(bool, str)`` — ``(ok, message)``. Mirrored onto
            ``BUS.auth_finished``.
        step: ``(str)`` — the name of the option just answered, for a progress
            line. Never carries a value, only a question's name.
    """

    url_ready = Signal(str)
    finished = Signal(bool, str)
    step = Signal(str)

    def __init__(self, client: Any, *,
                 open_url: Callable[[str], bool] | None = None,
                 answer: Callable[[Mapping[str, Any]], str] | None = None,
                 poll_ms: int = 250, emit_bus: bool = True,
                 check_port: bool = True,
                 parent: QObject | None = None) -> None:
        """
        Args:
            client: The :class:`~onedriveui.rc.client.RcClient` to drive.
            open_url: How to open the auth URL. Defaults to
                ``QDesktopServices.openUrl``, which on GNOME/Wayland goes
                through the portal and ends up in the user's real browser.
                Injected in tests so no browser is ever launched.
            answer: Supply an answer for one of rclone's questions, given the
                ``Option`` object. Return ``""`` to accept the default. Without
                it every question is answered with its own default, which is
                what the OneDrive walk needs.
            poll_ms: How often ``config/oauthstatus`` and ``job/status`` are
                polled while the browser step is outstanding.
            emit_bus: Mirror the two signals onto the application bus.
            check_port: Verify 53682 is free before starting. Only a test that
                is not really binding anything turns this off.
            parent: Qt parent.
        """
        super().__init__(parent)
        self._client = client
        self._open_url = open_url or (lambda url: QDesktopServices.openUrl(QUrl(url)))
        self._answer_cb = answer
        self._poll_ms = max(1, int(poll_ms))
        self._emit_bus = bool(emit_bus)
        self._check_port = bool(check_port)

        self._running = False
        self._path = "config/create"
        self._name = ""
        self._type = "onedrive"
        self._parameters: dict[str, Any] = {}
        self._steps = 0
        self._auth_url = ""
        self._job_id = 0
        self._execute_id = ""
        #: A slow daemon must not accumulate polls: at most one of each kind is
        #: ever outstanding, so a stalled reply delays the next tick rather than
        #: queueing another request behind it.
        self._oauth_in_flight = False
        self._job_in_flight = False
        #: The outstanding `config/oauthstatus`, so the latch above can tell
        #: "still running" from "aborted and will never answer".
        self._oauth_call: Any = None
        self._oauth_timer = QTimer(self)
        self._oauth_timer.setInterval(self._poll_ms)
        self._oauth_timer.timeout.connect(self._poll_oauth)
        self._job_timer = QTimer(self)
        self._job_timer.setInterval(self._poll_ms)
        self._job_timer.timeout.connect(self._poll_job)

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Is a sign-in in progress?"""
        return self._running

    @property
    def auth_url(self) -> str:
        """The URL the user was sent to, or ``""``."""
        return self._auth_url

    @property
    def job_id(self) -> int:
        """The blocking step's job id, or ``0`` before it exists."""
        return self._job_id

    # ── driving ─────────────────────────────────────────────────────────────

    def start(self, remote: str, *, remote_type: str = "onedrive",
              parameters: Mapping[str, Any] | None = None,
              update: bool = False) -> bool:
        """Begin the walk.

        Args:
            remote: The rclone remote name to create or re-authenticate.
            remote_type: The backend type. ``"onedrive"`` for both Personal and
                Business — ``drive_type`` distinguishes them afterwards.
            parameters: Backend parameters to seed, e.g. ``{"region": "global"}``.
                They are re-sent on **every** step, because rclone's
                ``--continue`` protocol requires every previously-answered value
                to be passed again each time.
            update: Use ``config/update`` instead of ``config/create``, which is
                how an existing account is re-authenticated. ``rclone config
                reconnect`` has no non-interactive mode and must never be
                shelled out to from a GUI.

        Returns:
            True when the walk started. False when it refused before sending
            anything — in which case ``finished`` has already reported why.

        Raises:
            OneDriveUIError: A sign-in is already running on this object.
        """
        if self._running:
            raise OneDriveUIError("a sign-in is already in progress")
        if not remote:
            raise ValueError("start() needs a remote name")

        self._reset()
        self._name = str(remote).rstrip(":")
        self._type = str(remote_type)
        self._parameters = dict(parameters or {})
        self._path = "config/update" if update else "config/create"

        if self._check_port and not callback_port_free():
            host, port = OAUTH_CALLBACK
            self._fail(
                f"another program is already listening on {host}:{port}, which "
                f"is the only port rclone's sign-in can use. Close the other "
                f"rclone sign-in and try again.")
            return False

        self._running = True
        log.info("sign-in: starting the %s walk for [%s]", self._path, self._name)
        self._post(self._body(), async_=False)
        return True

    def cancel(self) -> None:
        """Abandon the walk and tear rclone's OAuth server down.

        ``config/oauthstop`` is what unblocks the job; the job is then stopped
        as well, because a create that has already answered its last question
        would otherwise finish behind our back. A ``config/oauthstop`` when
        nothing is running answers HTTP 500 ``no oauth authentication is in
        progress`` — benign, and swallowed.
        """
        if not self._running:
            return
        log.info("sign-in: cancelled by the user")
        self._stop_timers()
        self._fire("config/oauthstop", {})
        if self._job_id:
            self._fire("job/stop", {"jobid": self._job_id})
        self._fail("sign-in was cancelled", cancelled=True)

    # ── the state machine ───────────────────────────────────────────────────

    def _body(self, state: str = "", result: str = "") -> dict[str, Any]:
        """One ``config/create`` request body.

        ``parameters`` is re-sent every time on purpose: with ``--continue``,
        rclone keeps no memory of previous answers, so every value already
        supplied has to be supplied again.
        """
        opt: dict[str, Any] = {"nonInteractive": True, "noOutput": True}
        if state:
            opt["continue"] = True
            opt["state"] = state
            opt["result"] = result
        return {"name": self._name, "type": self._type,
                "parameters": dict(self._parameters), "opt": opt}

    def _post(self, body: Mapping[str, Any], *, async_: bool) -> None:
        call = self._client.call(self._path, dict(body), async_=async_)
        if async_:
            call.succeeded.connect(self._on_job_started)
        else:
            call.succeeded.connect(self._on_step)
        call.failed.connect(self._on_call_failed)

    def _on_step(self, body: dict) -> None:
        """One answered question, or the end of the walk."""
        if not self._running:
            return
        self._steps += 1
        if self._steps > MAX_CONFIG_STEPS:
            self._fail(f"sign-in did not finish after {MAX_CONFIG_STEPS} steps")
            return

        error = str(body.get("Error") or "")
        if error:
            self._fail(redact(error))
            return

        option = body.get("Option")
        state = str(body.get("State") or "")
        if not isinstance(option, Mapping) or not state:
            # No question left: rclone has written the finished remote.
            log.info("sign-in: [%s] configured", self._name)
            self._succeed()
            return

        name = str(option.get("Name") or "")
        self.step.emit(name)
        if name == _TOKEN_OPTION:
            # Only reachable if config_is_local was answered "false"; this
            # application always has a browser, so it never should be.
            self._fail(
                "rclone asked for a pasted token, which means the browser "
                "sign-in was declined. Start the sign-in again.")
            return

        answer = self._answer_for(option)
        blocking = name == _IS_LOCAL_OPTION and answer.lower() == "true"
        log.info("sign-in: answering %s (%s)", name or "?",
                 "async, starts the browser flow" if blocking else "inline")
        self._post(self._body(state, answer), async_=blocking)
        if blocking:
            self._oauth_timer.start()

    def _answer_for(self, option: Mapping[str, Any]) -> str:
        """The answer for one of rclone's questions.

        A caller-supplied ``answer`` wins. Otherwise ``config_is_local`` is
        always ``"true"`` — we have a browser, and the alternative is the
        paste-a-token branch — and everything else takes the option's own
        default, which is what the interactive walk would do for a user pressing
        Enter.
        """
        if self._answer_cb is not None:
            supplied = str(self._answer_cb(option) or "")
            if supplied:
                return supplied
        if str(option.get("Name") or "") == _IS_LOCAL_OPTION:
            return "true"
        for key in ("DefaultStr", "ValueStr"):
            value = option.get(key)
            if value not in (None, ""):
                return str(value)
        default = option.get("Default")
        if isinstance(default, bool):
            return "true" if default else "false"
        return "" if default is None else str(default)

    # ── the blocking step ───────────────────────────────────────────────────

    def _on_job_started(self, body: dict) -> None:
        if not self._running:
            return
        self._job_id = int(body.get("jobid", 0) or 0)
        self._execute_id = str(body.get("executeId") or "")
        if not self._job_id:
            self._fail("rclone did not start the sign-in job")
            return
        log.info("sign-in: browser flow running as job %d", self._job_id)
        self._job_timer.start()

    def _poll_oauth(self) -> None:
        """Ask ``config/oauthstatus`` for the URL rclone is waiting on."""
        if not self._running or self._auth_url:
            self._oauth_timer.stop()
            return
        # The same abort hole the pollers had: `RcCall.abort()` emits nothing,
        # so a request cancelled by a client teardown or an endpoint change
        # would leave this latched and the sign-in stuck with no error.
        if self._oauth_in_flight:
            call = getattr(self, "_oauth_call", None)
            if call is not None and not call.delivered:
                return
            self._oauth_in_flight = False
        self._oauth_in_flight = True
        call = self._client.call("config/oauthstatus", {})
        self._oauth_call = call
        call.succeeded.connect(self._on_oauth_status)
        call.failed.connect(self._on_oauth_status_failed)

    def _on_oauth_status_failed(self, error: object) -> None:
        self._oauth_in_flight = False
        log.debug("config/oauthstatus: %s", error)

    def _on_oauth_status(self, body: dict) -> None:
        self._oauth_in_flight = False
        if not self._running or self._auth_url:
            return
        if str(body.get("status") or "") != "running":
            return
        url = str(body.get("authUrl") or "")
        if not url:
            return
        self._oauth_timer.stop()
        self._auth_url = url
        # The URL carries the OAuth `state` parameter, which is a bearer of the
        # in-flight authorisation: it is emitted for the browser and redacted
        # for the log (I14).
        log.info("sign-in: opening %s", redact(url))
        self.url_ready.emit(url)
        if self._emit_bus:
            BUS.auth_url_ready.emit(url)
        try:
            self._open_url(url)
        except Exception as exc:                     # noqa: BLE001 - opener is injected
            log.warning("could not open the sign-in URL: %s", exc)

    def _poll_job(self) -> None:
        """Poll the blocking ``config/create`` job to its end."""
        if not self._running or not self._job_id:
            self._job_timer.stop()
            return
        if self._job_in_flight:
            return
        self._job_in_flight = True
        call = self._client.call("job/status", {"jobid": self._job_id})
        call.succeeded.connect(self._on_job_status)
        call.failed.connect(self._on_job_status_failed)

    def _on_job_status(self, body: dict) -> None:
        self._job_in_flight = False
        if not self._running:
            return
        seen = str(body.get("executeId") or "")
        if seen and self._execute_id and seen != self._execute_id:
            self._stop_timers()
            self._fail("the rclone daemon restarted during sign-in; "
                       "start again")
            return
        if not body.get("finished"):
            return
        self._stop_timers()
        self._job_id = 0
        error = str(body.get("error") or "")
        if error:
            self._fail(redact(error))
            return
        output = body.get("output")
        self._on_step(dict(output) if isinstance(output, Mapping) else {})

    def _on_job_status_failed(self, error: object) -> None:
        self._job_in_flight = False
        if not self._running:
            return
        if isinstance(error, RcError) and error.is_job_expired:
            self._stop_timers()
            self._fail("the sign-in job expired before it finished")
            return
        # A single unreachable poll is normal while the daemon is busy holding
        # the blocked config call open; keep polling.
        log.debug("job/status during sign-in: %s", error)

    def _on_call_failed(self, error: object) -> None:
        if not self._running:
            return
        self._stop_timers()
        message = error.message if isinstance(error, RcError) else str(error)
        self._fail(redact(message))

    # ── terminal ────────────────────────────────────────────────────────────

    def _fire(self, path: str, params: Mapping[str, Any]) -> None:
        """Fire and forget, logging any failure instead of raising."""
        call = self._client.call(path, dict(params))
        call.failed.connect(
            lambda error, p=path: log.debug("%s: %s", p, error))

    def _stop_timers(self) -> None:
        self._oauth_timer.stop()
        self._job_timer.stop()

    def _reset(self) -> None:
        self._stop_timers()
        self._oauth_in_flight = False
        self._job_in_flight = False
        self._steps = 0
        self._auth_url = ""
        self._job_id = 0
        self._execute_id = ""

    def _succeed(self) -> None:
        self._running = False
        self._stop_timers()
        self.finished.emit(True, self._name)
        if self._emit_bus:
            BUS.auth_finished.emit(True, self._name)

    def _fail(self, message: str, *, cancelled: bool = False) -> None:
        self._running = False
        self._stop_timers()
        if not cancelled:
            log.warning("sign-in failed: %s", message)
        self.finished.emit(False, message)
        if self._emit_bus:
            BUS.auth_finished.emit(False, message)


# ─────────────────────────────────────────────────────────────────────────────
# The subprocess fallback
# ─────────────────────────────────────────────────────────────────────────────

def parse_authorize_url(text: str) -> str:
    """Pull the sign-in link out of ``rclone authorize``'s **stderr**.

    Args:
        text: Whatever has been read from stderr so far.

    Returns:
        The ``http://127.0.0.1:53682/auth?state=…`` URL, or ``""`` when the
        NOTICE line has not arrived yet.

    rclone writes four NOTICE lines to stderr; only one of them carries the
    link. It is on stderr and not stdout because stdout is reserved for the
    token blob.
    """
    match = _AUTHORIZE_LINK_RE.search(text or "")
    return match.group(1) if match is not None else ""


def parse_authorize_token(text: str) -> str:
    """Pull the token blob out of ``rclone authorize``'s **stdout**.

    Args:
        text: Whatever has been read from stdout so far.

    Returns:
        The JSON blob between ``Paste the following into your remote machine
        --->`` and ``<---End paste``, stripped, or ``""`` when it is not
        complete yet.

    The blob is the whole OAuth token object — ``access_token``,
    ``refresh_token``, ``expiry``. It is never logged (I14): the caller hands it
    straight to ``config/create``'s ``parameters``.
    """
    body = text or ""
    start = body.find(AUTHORIZE_START_MARKER)
    if start < 0:
        return ""
    start += len(AUTHORIZE_START_MARKER)
    end = body.find(AUTHORIZE_END_MARKER, start)
    if end < 0:
        return ""
    return body[start:end].strip()


class AuthorizeFallback(QObject):
    """Sign in through ``rclone authorize``, for when the rc walk cannot run.

    ``rclone authorize onedrive --auth-no-open-browser`` does the same OAuth
    dance in a child process. It is the fallback for a daemon that is down or
    too old, and it is the only path that works with no rcd at all.

    Two channels, two meanings, and they must not be merged: the **link arrives
    on stderr** as a NOTICE line, and the **token blob arrives on stdout**
    between the paste markers. ``QProcess`` is used with
    ``SeparateChannels`` for exactly that reason, and both channels are
    line-buffered because ``readyRead`` delivers arbitrary byte chunks rather
    than lines.

    Attributes:
        url_ready: ``(str)`` — the link to open.
        finished: ``(bool, str)`` — ``(ok, message)``; the message is the remote
            name on success, never the token.
    """

    url_ready = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, *, rclone_path: str = RCLONE_DEFAULT,
                 open_url: Callable[[str], bool] | None = None,
                 emit_bus: bool = True,
                 parent: QObject | None = None) -> None:
        """
        Args:
            rclone_path: The rclone binary.
            open_url: How to open the link. Defaults to ``QDesktopServices``.
            emit_bus: Mirror onto ``BUS.auth_url_ready``/``auth_finished``.
            parent: Qt parent.
        """
        super().__init__(parent)
        self._rclone = rclone_path
        self._open_url = open_url or (lambda url: QDesktopServices.openUrl(QUrl(url)))
        self._emit_bus = bool(emit_bus)
        self._process: QProcess | None = None
        self._out = ""
        self._err = ""
        self._url = ""
        self._token = ""
        self._done = False

    @property
    def token(self) -> str:
        """The captured token blob. Never logged, never rendered."""
        return self._token

    @property
    def auth_url(self) -> str:
        """The link the user was sent to, or ``""``."""
        return self._url

    @property
    def running(self) -> bool:
        """Is the child process alive?"""
        return (self._process is not None
                and self._process.state() != QProcess.ProcessState.NotRunning)

    def start(self, remote_type: str = "onedrive",
              extra_args: list[str] | None = None) -> None:
        """Spawn ``rclone authorize <type> --auth-no-open-browser``.

        Args:
            remote_type: The backend type to authorise.
            extra_args: Anything else to append. Backend option flags must not
                appear here (invariant I1); this is for ``--config`` and the
                like, and is what the tests substitute the binary through.
        """
        if self.running:
            raise OneDriveUIError("an authorize subprocess is already running")
        self._out = self._err = self._url = self._token = ""
        self._done = False
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        args = ["authorize", str(remote_type), "--auth-no-open-browser"]
        args.extend(extra_args or [])
        log.info("sign-in fallback: %s %s", self._rclone, " ".join(args))
        process.start(self._rclone, args)

    def cancel(self) -> None:
        """Kill the child. It holds port 53682 until it gets a code."""
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return
        process.kill()
        process.waitForFinished(2000)

    # ── channels ────────────────────────────────────────────────────────────

    def _read_stdout(self) -> None:
        process = self._process
        if process is None:
            return
        self._out += bytes(process.readAllStandardOutput().data()).decode(
            "utf-8", "replace")
        token = parse_authorize_token(self._out)
        if token:
            self._token = token

    def _read_stderr(self) -> None:
        process = self._process
        if process is None:
            return
        self._err += bytes(process.readAllStandardError().data()).decode(
            "utf-8", "replace")
        if self._url:
            return
        url = parse_authorize_url(self._err)
        if not url:
            return
        self._url = url
        log.info("sign-in fallback: opening %s", redact(url))
        self.url_ready.emit(url)
        if self._emit_bus:
            BUS.auth_url_ready.emit(url)
        try:
            self._open_url(url)
        except Exception as exc:                     # noqa: BLE001 - opener is injected
            log.warning("could not open the sign-in URL: %s", exc)

    def _on_finished(self, exit_code: int, _status: Any) -> None:
        self._read_stdout()
        self._read_stderr()
        if self._done:
            return
        self._done = True
        if self._token:
            self._settle(True, "")
            return
        tail = redact(self._err.strip().splitlines()[-1]) if self._err.strip() else ""
        self._settle(False, tail or f"rclone authorize exited with {exit_code}")

    def _on_error(self, error: Any) -> None:
        if self._done:
            return
        self._done = True
        self._settle(False, f"rclone authorize could not run ({error})")

    def _settle(self, ok: bool, message: str) -> None:
        self.finished.emit(ok, message)
        if self._emit_bus:
            BUS.auth_finished.emit(ok, message)
