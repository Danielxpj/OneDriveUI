"""The rc transport: an async ``QNetworkAccessManager`` client and a blocking twin.

Wire facts this module encodes, every one of them measured against rclone
v1.75.0 (``docs/research/rclone-rc-api.md``):

* **POST only.** ``GET``/``HEAD`` on an rc path is 404 and ``PUT`` is 405.
* **JSON body, never form or query.** Form and query parameters arrive as
  *strings*, so ``{"_async": true}`` would become ``"true"`` and be ignored.
* **HTTP basic auth on every request.** ``--rc-no-auth`` exempts nothing in
  v1.75.0 (all 101 commands report ``NoAuth: false``), so credentials are always
  attached.
* **The 4-key error envelope** ``{"error", "input", "path", "status"}``, whose
  ``status`` always mirrors the HTTP status.
* **``X-Rclone-Jobid`` on every response**, sync or async.
* **``executeId`` is the daemon's identity.** A change means the daemon
  restarted: every job id, mount, VFS and byte of transfer history is gone. It is
  also the only way to tell an *expired* job (``job/status`` → HTTP 500 ``job not
  found``, same ``executeId``) from a *lost* one (different ``executeId``).

Threading (ARCHITECTURE §7): :class:`RcClient` and :class:`JobWatcher` live on
the GUI thread and never block it — QNAM is asynchronous and every reply is
``deleteLater()``d. :func:`call_blocking` is the twin for ``IOPool`` worker
threads and for the two startup-only probes (:func:`is_alive` and the ownership
proof); it uses ``http.client`` precisely so it does *not* need a QNAM, which
must not be shared across threads.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import socket
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QCoreApplication,
    QEvent,
    QObject,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from onedriveui.constants import OFFLINE_FAILURE_THRESHOLD, RC_TIMEOUT_S
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import JobHandle, RcEndpoint
from onedriveui.rc import guards

__all__ = [
    "JOBID_HEADER",
    "JobWatcher",
    "RcCall",
    "RcClient",
    "build_params",
    "call_blocking",
    "is_alive",
]

log = logging.getLogger(__name__)

#: Present on every rc response, synchronous calls included.
JOBID_HEADER = "X-Rclone-Jobid"

#: What the `rclone rc` *client* reports when the daemon is unreachable. The
#: server never emits it, so it is unambiguous shorthand for "no daemon".
_UNREACHABLE_STATUS = 503

_JSON_CONTENT_TYPE = "application/json"


# ─────────────────────────────────────────────────────────────────────────────
# Parameter assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_params(params: Mapping[str, Any] | None = None, *,
                 group: str | None = None, async_: bool = False,
                 config: Mapping[str, Any] | None = None,
                 filt: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge the four special parameters into a call's body.

    The four are accepted by *every* rc endpoint and are the only way to reach
    most of rclone's behaviour: ``sync/copy``'s entire documented parameter set
    is ``srcFs``/``dstFs``/``createEmptySrcDirs``, and everything else —
    transfers, backup-dir, max-delete, include/exclude — arrives through
    ``_config`` and ``_filter``.

    Args:
        params: The endpoint's own parameters. Copied, never mutated.
        group: ``_group`` — the stats group. Progress is read with
            ``core/stats {"group": …}``; without one, rclone invents ``job/<id>``
            and ``core/group-list`` fills with noise.
        async_: ``_async`` — run as a background job, answering
            ``{"jobid", "executeId"}`` immediately.
        config: ``_config`` — per-call global-flag overrides, keyed by rclone's
            *internal* Go field names (``Transfers``, ``BackupDir``, …). Note
            that ``_config.BwLimit`` is accepted and does **not** throttle; only
            ``core/bwlimit`` does.
        filt: ``_filter`` — per-call filter rules, keyed by the internal filter
            names (``IncludeRule``, ``ExcludeRule``, ``MaxSize``, …).

    Returns:
        A new dict. An explicit key already in ``params`` always wins, so a
        caller that has assembled ``_group`` itself is never overridden.
    """
    body: dict[str, Any] = dict(params or {})
    if group is not None:
        body.setdefault("_group", group)
    if async_:
        body.setdefault("_async", True)
    if config is not None:
        body.setdefault("_config", dict(config))
    if filt is not None:
        body.setdefault("_filter", dict(filt))
    return body


def _latin1(value: Any) -> str:
    """Decode a header field, whether Qt handed us a ``QByteArray`` or ``bytes``.

    HTTP header bytes are latin-1 by RFC 7230, and rclone only ever emits ASCII
    there, so this can never raise.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("latin-1")
    data = getattr(value, "data", None)
    if callable(data):
        return bytes(data()).decode("latin-1")
    return str(value)


def _auth_header(ep: RcEndpoint) -> str:
    token = base64.b64encode(f"{ep.user}:{ep.password}".encode()).decode("ascii")
    return f"Basic {token}"


def _envelope(path: str, status: int, message: str,
              params: Mapping[str, Any]) -> dict[str, Any]:
    """Synthesise rclone's 4-key error body for a failure that never reached it."""
    return {"error": message, "input": dict(params), "path": path, "status": status}


def _decode(payload: bytes, path: str, status: int,
            params: Mapping[str, Any]) -> dict[str, Any]:
    """Parse a response body, tolerating the non-JSON ones.

    Two rc responses are not JSON: a 401 is the plain text ``401 Unauthorized``
    and a 404 for an unknown *HTTP* path is plain ``Not Found``. Both are
    normalised into the 4-key envelope so callers only ever see one shape.
    """
    if not payload:
        return {} if status < 400 else _envelope(path, status, f"HTTP {status}", params)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        text = payload.decode("utf-8", "replace").strip()
        return _envelope(path, status, text or f"HTTP {status}", params)
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


# ─────────────────────────────────────────────────────────────────────────────
# One asynchronous call
# ─────────────────────────────────────────────────────────────────────────────

class RcCall(QObject):
    """One in-flight rc request. Exactly one of its two signals fires, once.

    Delivery is always deferred past the ``call()`` that created it, so a caller
    can connect after the fact without a race.

    Attributes:
        succeeded: Emitted with the parsed JSON body on HTTP 200/202.
        failed: Emitted with an :class:`~onedriveui.errors.RcError` (or
            :class:`~onedriveui.errors.DaemonUnavailable` when the daemon never
            answered).
    """

    succeeded = Signal(dict)
    failed = Signal(object)

    def __init__(self, path: str, params: Mapping[str, Any],
                 parent: QObject | None = None) -> None:
        """
        Args:
            path: The rc command path, e.g. ``"core/stats"``.
            params: The assembled request body.
            parent: Owner, for Qt lifetime.
        """
        super().__init__(parent)
        self.path = path
        self.params: dict[str, Any] = dict(params)
        #: Response headers, keys lower-cased as Qt normalises them. Read one
        #: with :meth:`header`, which is case-insensitive.
        self.headers: dict[str, str] = {}
        self.result: dict[str, Any] | None = None
        self.error: RcError | None = None
        self.delivered = False
        self._reply: QNetworkReply | None = None
        self._timer: QTimer | None = None

    # ── state ───────────────────────────────────────────────────────────────

    def header(self, name: str) -> str:
        """One response header, looked up case-insensitively.

        Qt 6 normalises incoming header names to lower case, so ``headers`` is
        keyed that way; HTTP header names are case-insensitive by RFC 9110 in any
        event, and this is the only correct way to read one.

        Args:
            name: The header name in any casing, e.g. ``"X-Rclone-Jobid"``.

        Returns:
            The value, or ``""`` when the header was absent.
        """
        return self.headers.get(name.lower(), "")

    @property
    def jobid(self) -> int:
        """The ``X-Rclone-Jobid`` header, or ``0`` before the reply arrives.

        Present on *every* rc response, synchronous calls included: rclone
        allocates a job id and a ``job/<id>`` stats group for each one.
        """
        try:
            return int(self.header(JOBID_HEADER) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def execute_id(self) -> str:
        """``executeId`` from an ``_async`` response body, else ``""``."""
        return str((self.result or {}).get("executeId", ""))

    def handle(self, group: str = "", label: str = "") -> JobHandle | None:
        """Build a :class:`~onedriveui.models.JobHandle` from an ``_async`` reply.

        Args:
            group: The ``_group`` the job was started under.
            label: A human label for the activity row.

        Returns:
            The handle, or ``None`` if this call has not succeeded or did not
            answer with a ``jobid`` (i.e. it was not an ``_async`` call).
        """
        body = self.result or {}
        if "jobid" not in body:
            return None
        return JobHandle(
            job_id=int(body["jobid"]),
            execute_id=str(body.get("executeId", "")),
            group=group or str(self.params.get("_group", "")),
            path=self.path,
            label=label,
        )

    # ── delivery ────────────────────────────────────────────────────────────

    def abort(self) -> None:
        """Cancel the request. No signal is emitted for an aborted call."""
        self.delivered = True
        self._stop_timer()
        reply, self._reply = self._reply, None
        if reply is not None and reply.isRunning():
            reply.abort()

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None

    def _emit_success(self, body: dict[str, Any]) -> None:
        if self.delivered:
            return
        self.delivered = True
        self.result = body
        self.succeeded.emit(body)

    def _emit_failure(self, error: RcError) -> None:
        if self.delivered:
            return
        self.delivered = True
        self.error = error
        self.failed.emit(error)


# ─────────────────────────────────────────────────────────────────────────────
# The asynchronous client
# ─────────────────────────────────────────────────────────────────────────────

class RcClient(QObject):
    """Async rc client for the GUI thread, over one ``QNetworkAccessManager``.

    One instance per daemon. QNAM keeps the TCP connection alive between calls,
    which matters for the 1 Hz stats poll — a fresh connect per tick is pure
    waste against a loopback daemon that answers in single-digit milliseconds.
    """

    def __init__(self, endpoint: RcEndpoint, parent: QObject | None = None) -> None:
        """
        Args:
            endpoint: The daemon to drive. Its ``host``/``port`` build the URL
                and its ``user``/``password`` the basic-auth header. Replace it
                with :meth:`set_endpoint` after a restart rather than building a
                second client.
            parent: Owner, for Qt lifetime.
        """
        super().__init__(parent)
        self._endpoint = endpoint
        self._nam = QNetworkAccessManager(self)
        self._nam.setAutoDeleteReplies(False)
        self._inflight: set[RcCall] = set()
        self._closed = False

    # ── endpoint ────────────────────────────────────────────────────────────

    @property
    def endpoint(self) -> RcEndpoint:
        """The daemon this client is pointed at."""
        return self._endpoint

    def set_endpoint(self, endpoint: RcEndpoint) -> None:
        """Repoint the client after a daemon restart or a port change.

        In-flight calls are aborted: their job ids belong to a process that no
        longer exists.

        Args:
            endpoint: The new endpoint.
        """
        if endpoint == self._endpoint:
            return
        self._abort_all()
        self._endpoint = endpoint

    # ── calling ─────────────────────────────────────────────────────────────

    def call(self, path: str, params: dict | None = None, *,
             group: str | None = None, async_: bool = False,
             config: dict | None = None, filt: dict | None = None,
             timeout_s: float = RC_TIMEOUT_S) -> RcCall:
        """POST one rc command. Returns immediately; the answer arrives by signal.

        Args:
            path: The rc command path, e.g. ``"core/stats"``. Checked against the
                banned list (I7, I8, I14) before anything is sent.
            params: The endpoint's own parameters.
            group: ``_group``, the stats group.
            async_: ``_async`` — answer with ``{"jobid", "executeId"}`` at once
                and run in the background. Mandatory for ``sync/*``,
                ``operations/size`` and ``operations/check``, which can run for
                minutes on a OneDrive tree.
            config: ``_config`` overrides, by internal field name.
            filt: ``_filter`` rules, by internal field name.
            timeout_s: Abort after this long. The default of
                :data:`~onedriveui.constants.RC_TIMEOUT_S` (4 s) is generous for
                a loopback daemon that answers in under 10 ms, and short enough
                that a wedged daemon cannot stall a UI tick.

        Returns:
            The :class:`RcCall`. Connect to its ``succeeded``/``failed`` signals.

        Raises:
            SafetyRefusal: ``path`` is banned by I7, I8 or I14. This is raised
                synchronously, because it is a bug in the caller and must not be
                reported as a runtime failure.
        """
        guards.assert_rc_path_allowed(path)
        body = build_params(params, group=group, async_=async_,
                            config=config, filt=filt)
        call = RcCall(path, body, parent=self)

        if self._closed:
            error = DaemonUnavailable(
                path, _UNREACHABLE_STATUS,
                _envelope(path, _UNREACHABLE_STATUS, "rc client closed", body))
            QTimer.singleShot(0, lambda: call._emit_failure(error))
            return call

        request = self._request(path)
        payload = QByteArray(json.dumps(body).encode("utf-8"))
        reply = self._nam.post(request, payload)
        call._reply = reply
        self._inflight.add(call)

        timer = QTimer(call)
        timer.setSingleShot(True)
        timer.setInterval(max(1, int(timeout_s * 1000)))
        timer.timeout.connect(lambda: self._on_timeout(call))
        timer.start()
        call._timer = timer

        reply.finished.connect(lambda: self._on_finished(call, reply))
        return call

    def _request(self, path: str) -> QNetworkRequest:
        ep = self._endpoint
        request = QNetworkRequest(QUrl(f"{ep.base_url}/{path.lstrip('/')}"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader,
                          _JSON_CONTENT_TYPE)
        request.setRawHeader(b"Authorization", _auth_header(ep).encode("ascii"))
        # Belt and braces beside our own QTimer: this one fires when the socket
        # stalls mid-body, which a wall-clock timer alone would not catch early.
        request.setTransferTimeout(int(RC_TIMEOUT_S * 1000))
        return request

    def _on_timeout(self, call: RcCall) -> None:
        """Our own wall-clock deadline expired. Report first, then abort.

        Order matters: ``reply.abort()`` emits ``finished`` synchronously, and
        ``_on_finished`` would otherwise deliver Qt's generic "Operation
        canceled" before this method could say what actually happened.
        """
        if call.delivered:
            return
        seconds = call._timer.interval() / 1000 if call._timer is not None else 0.0
        reply = call._reply
        call._reply = None
        self._inflight.discard(call)
        call._emit_failure(DaemonUnavailable(
            call.path, _UNREACHABLE_STATUS,
            _envelope(call.path, _UNREACHABLE_STATUS,
                      f"rc call timed out after {seconds:.3g}s", call.params)))
        if reply is not None and reply.isRunning():
            # Emits finished(); _on_finished sees `delivered` and only deletes.
            reply.abort()

    def _on_finished(self, call: RcCall, reply: QNetworkReply) -> None:
        """The single exit path. Every reply is deleteLater()'d here, without
        exception — a leaked QNetworkReply holds its socket and its buffer for
        the life of the process."""
        try:
            self._inflight.discard(call)
            call._stop_timer()
            call._reply = None
            if call.delivered:
                return

            call.headers = {
                _latin1(name).lower(): _latin1(value)
                for name, value in reply.rawHeaderPairs()
            }
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            payload = bytes(reply.readAll().data())
            net_error = reply.error()

            if status is None:
                message = reply.errorString() or "no response from the rc daemon"
                call._emit_failure(DaemonUnavailable(
                    call.path, _UNREACHABLE_STATUS,
                    _envelope(call.path, _UNREACHABLE_STATUS, message, call.params)))
                return

            status = int(status)
            body = _decode(payload, call.path, status, call.params)
            if status >= 400:
                call._emit_failure(RcError(call.path, status, body))
                return
            if net_error not in (QNetworkReply.NetworkError.NoError,):
                # A 2xx that Qt still flagged (e.g. the connection dropped while
                # the body was streaming) is not a usable answer.
                call._emit_failure(DaemonUnavailable(
                    call.path, _UNREACHABLE_STATUS,
                    _envelope(call.path, _UNREACHABLE_STATUS,
                              reply.errorString(), call.params)))
                return
            call._emit_success(body)
        finally:
            reply.deleteLater()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def _abort_all(self) -> None:
        for call in list(self._inflight):
            call.abort()
        self._inflight.clear()

    def close(self) -> None:
        """Abort every in-flight call, flush the pending reply deletions, and
        refuse new ones.

        Idempotent. Called from ``App.shutdown()`` before the event loop stops,
        so no reply can arrive into a half-torn-down object graph.

        The explicit ``sendPostedEvents(DeferredDelete)`` is load-bearing, not
        tidiness: ``deleteLater()`` only takes effect on the next loop turn, and
        a ``QNetworkAccessManager`` destroyed while it still owns a
        finished-but-undeleted ``QNetworkReply`` tears the reply down as a child
        instead, which aborts the process with *pure virtual method called*.
        Flushing here guarantees the replies die before their manager can.
        """
        if self._closed:
            return
        self._closed = True
        self._abort_all()
        self._nam.clearAccessCache()
        app = QCoreApplication.instance()
        if app is not None:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


# ─────────────────────────────────────────────────────────────────────────────
# The blocking twin
# ─────────────────────────────────────────────────────────────────────────────

def call_blocking(ep: RcEndpoint, path: str, params: dict | None = None,
                  timeout_s: float = 30.0) -> dict:
    """Synchronous rc call, for ``IOPool`` threads and startup-only probes.

    Uses ``http.client`` rather than QNAM: a ``QNetworkAccessManager`` may not be
    shared across threads, and a worker that needs an answer before it can
    continue should not be running an event loop to get it.

    **Never call this from the GUI thread in the hot path** (ARCHITECTURE §7.6).
    The two sanctioned exceptions are :func:`is_alive` and
    ``RcdSupervisor.verify_ownership()``, both of which run once at startup with
    a one-second budget.

    Args:
        ep: The daemon.
        path: The rc command path.
        params: Request body. Put ``_async``/``_group``/``_config``/``_filter``
            in here directly, or build them with :func:`build_params`.
        timeout_s: Socket timeout. The default of 30 s suits a worker running an
            ``operations/check``; drop it to ~1 s for a liveness probe.

    Returns:
        The parsed JSON response body.

    Raises:
        SafetyRefusal: ``path`` is banned by I7, I8 or I14.
        DaemonUnavailable: The daemon did not answer — connection refused,
            DNS/socket failure or timeout. Always HTTP 503, which the server
            itself never emits.
        RcError: The daemon answered with a 4xx/5xx envelope.
    """
    guards.assert_rc_path_allowed(path)
    body = dict(params or {})
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": _JSON_CONTENT_TYPE,
        "Content-Length": str(len(payload)),
        "Authorization": _auth_header(ep),
    }
    conn = http.client.HTTPConnection(ep.host, ep.port, timeout=timeout_s)
    try:
        conn.request("POST", "/" + path.lstrip("/"), payload, headers)
        response = conn.getresponse()
        status = int(response.status)
        raw = response.read()
    except (OSError, socket.timeout, http.client.HTTPException) as exc:
        raise DaemonUnavailable(
            path, _UNREACHABLE_STATUS,
            _envelope(path, _UNREACHABLE_STATUS,
                      f'connection failed: Post "{ep.base_url}/{path}": {exc}',
                      body)) from exc
    finally:
        conn.close()

    parsed = _decode(raw, path, status, body)
    if status >= 400:
        raise RcError(path, status, parsed)
    return parsed


def is_alive(ep: RcEndpoint, timeout_s: float = 1.0) -> bool:
    """Probe ``rc/noop`` — the canonical liveness check.

    A listening port is **never** proof of a live daemon of ours: ``core/quit``
    does not unlink a unix socket, any ``rclone`` started with ``--rc`` exposes
    all 101 endpoints, and this machine already has a stranger's rclone on 5572.
    Liveness and ownership are two separate questions; this answers only the
    first. See ``RcdSupervisor.verify_ownership()`` for the second.

    Args:
        ep: The daemon.
        timeout_s: Socket timeout. One second: a loopback daemon answers
            ``rc/noop`` in single-digit milliseconds or it is not there.

    Returns:
        True only if the daemon answered ``rc/noop`` with a 2xx. Never raises.
    """
    if not ep.port:
        return False
    try:
        call_blocking(ep, "rc/noop", {}, timeout_s=timeout_s)
    except (RcError, OSError):
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Watching an async job to its end
# ─────────────────────────────────────────────────────────────────────────────

class JobWatcher(QObject):
    """Poll one ``_async`` job and report *which* of four ends it reached.

    The four ends are genuinely different states and the UI treats them
    differently:

    ``finished``
        ``job/status`` came back with ``finished: true``. The payload is the
        whole status object, ``output`` included — captured immediately, because
        a finished job is garbage-collected after
        ``--rc-job-expire-duration`` and going back for it later fails.
    ``failed``
        A real error, or the daemon stayed unreachable for
        :data:`~onedriveui.constants.OFFLINE_FAILURE_THRESHOLD` consecutive
        polls.
    ``expired``
        HTTP 500 ``job not found`` with an **unchanged** ``executeId``. The job
        *did* finish; its outcome is simply no longer knowable. The activity row
        is marked ``interrupted``, never ``error``.
    ``lost``
        The ``executeId`` **changed** — the daemon restarted. Every job id,
        mount, VFS and byte of transfer history is gone; the caller must drop all
        in-flight handles and re-observe.

    Distinguishing the last two is impossible from ``job/status`` alone, which is
    why a ``job not found`` triggers a second call to ``job/list`` for the
    daemon's current ``executeId`` before anything is emitted.
    """

    finished = Signal(dict)
    failed = Signal(object)
    expired = Signal()
    lost = Signal()

    _POLLING = "polling"
    _DISAMBIGUATING = "disambiguating"

    def __init__(self, client: RcClient, parent: QObject | None = None) -> None:
        """
        Args:
            client: The client to poll through. Not owned; the watcher never
                closes it.
            parent: Owner, for Qt lifetime.
        """
        super().__init__(parent)
        self._client = client
        self._handle: JobHandle | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._state = self._POLLING
        self._call: RcCall | None = None
        #: The exact (succeeded, failed) slot pair connected to `_call`, so
        #: `stop()` disconnects those two and only those two.
        self._slots: tuple[Any, Any] | None = None
        self._net_failures = 0
        self._done = False

    @property
    def handle(self) -> JobHandle | None:
        """The job being watched, or ``None`` when idle."""
        return self._handle

    @property
    def running(self) -> bool:
        """True between :meth:`watch` and the terminal signal."""
        return self._handle is not None and not self._done

    def watch(self, handle: JobHandle, poll_ms: int = 500) -> None:
        """Start watching ``handle``.

        Args:
            handle: From :meth:`RcCall.handle` or ``JobRegistry``. Its
                ``execute_id`` is the daemon identity every poll is compared
                against, so it must be the one the ``_async`` reply carried.
            poll_ms: Interval. The default of 500 ms is well inside the 10-minute
                ``--rc-job-expire-duration`` this application configures, and
                far inside rclone's own 60 s default.

        Any previous watch is stopped first; a watcher follows one job at a time.
        """
        self.stop()
        self._handle = handle
        self._done = False
        self._state = self._POLLING
        self._net_failures = 0
        self._timer.setInterval(max(1, int(poll_ms)))
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        """Stop polling without emitting anything. Idempotent.

        The in-flight poll is disconnected first and only then aborted, and the
        abort is conditional: ``RcCall``'s frozen contract is its two signals, so
        a substituted call object need not offer ``abort()``. Disconnecting is
        what actually guarantees silence; aborting merely frees the socket a few
        milliseconds sooner.
        """
        self._timer.stop()
        call, self._call = self._call, None
        slots, self._slots = self._slots, None
        if call is not None:
            if slots is not None:
                on_ok, on_err = slots
                call.succeeded.disconnect(on_ok)
                call.failed.disconnect(on_err)
            abort = getattr(call, "abort", None)
            if callable(abort):
                abort()
        self._handle = None
        self._done = True

    # ── the poll loop ───────────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._done or self._handle is None or self._call is not None:
            return
        if self._state == self._DISAMBIGUATING:
            call = self._client.call("job/list", {})
            slots = (self._on_executeid, self._on_executeid_failed)
        else:
            call = self._client.call("job/status", {"jobid": self._handle.job_id})
            slots = (self._on_status, self._on_status_failed)
        call.succeeded.connect(slots[0])
        call.failed.connect(slots[1])
        self._call = call
        self._slots = slots

    def _settle(self, emit: Any, *args: Any) -> None:
        """Stop the loop and fire exactly one terminal signal."""
        self._timer.stop()
        self._call = None
        self._slots = None
        self._done = True
        emit.emit(*args)

    def _on_status(self, body: dict) -> None:
        self._call = None
        self._slots = None
        if self._done or self._handle is None:
            return
        self._net_failures = 0
        seen = str(body.get("executeId", ""))
        if seen and self._handle.execute_id and seen != self._handle.execute_id:
            log.info("job %s: executeId changed %s -> %s; the daemon restarted",
                     self._handle.job_id, self._handle.execute_id, seen)
            self._settle(self.lost)
            return
        if body.get("finished"):
            self._settle(self.finished, body)

    def _on_status_failed(self, error: object) -> None:
        self._call = None
        self._slots = None
        if self._done or self._handle is None:
            return
        if isinstance(error, DaemonUnavailable):
            self._net_failures += 1
            if self._net_failures >= OFFLINE_FAILURE_THRESHOLD:
                self._settle(self.failed, error)
            return
        if isinstance(error, RcError) and error.is_job_expired:
            # Ambiguous on its own: expired, or the daemon restarted. Ask.
            self._state = self._DISAMBIGUATING
            self._tick()
            return
        self._settle(self.failed, error)

    def _on_executeid(self, body: dict) -> None:
        self._call = None
        self._slots = None
        if self._done or self._handle is None:
            return
        self._net_failures = 0
        current = str(body.get("executeId", ""))
        if current and self._handle.execute_id and current != self._handle.execute_id:
            log.info("job %s: 'job not found' with a NEW executeId (%s -> %s); "
                     "the daemon restarted", self._handle.job_id,
                     self._handle.execute_id, current)
            self._settle(self.lost)
        else:
            log.info("job %s: 'job not found' with an unchanged executeId; the "
                     "job expired and its outcome is unknown", self._handle.job_id)
            self._settle(self.expired)

    def _on_executeid_failed(self, error: object) -> None:
        self._call = None
        self._slots = None
        if self._done:
            return
        if isinstance(error, DaemonUnavailable):
            self._net_failures += 1
            if self._net_failures < OFFLINE_FAILURE_THRESHOLD:
                return          # retry the disambiguation on the next tick
        self._settle(self.failed, error)
