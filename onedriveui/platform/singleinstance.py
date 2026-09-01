"""One running copy, and a channel the second launch hands its argv down.

Two guards, doing different jobs:

* a **`QLocalServer`** at `$XDG_RUNTIME_DIR/onedriveui/ui.sock`. Listening on it
  *is* the claim to be the primary instance, and connecting to it *is* how a
  second launch says "raise your window" or "open the folder". Liveness is
  therefore not inferred from a file: a process that is listening is alive, and
  one that is not, is not.
* a **`QLockFile`** at `…/ui.lock`, which records the owning pid, host and
  application name. It cannot forward arguments, so it is not a substitute for
  the socket; it closes the window where two copies start close enough together
  that both find the socket unconnectable.

**The socket path is explicit and absolute.** `QLocalServer.listen("name")` puts
the socket in `/tmp` — measured `/tmp/OneDriveUI-<user>` on the target
machine — where it is world-readable and shared across login sessions, and
driving this socket is equivalent to driving the user's OneDrive. Everything
here goes through `paths.ui_socket()`, which is under `$XDG_RUNTIME_DIR`
(`/run/user/1000/onedriveui/`, mode 0700, wiped at logout), and the listener
additionally sets `UserAccessOption` so the socket itself is owner-only.

The wire format is one line of JSON, the same NDJSON shape `platform.ipc` uses:

    -> {"op": "launch", "v": 1, "argv": [...], "cwd": "/home/u", "pid": 4242}
    <- {"op": "ok", "v": 1, "pid": 1234}

Threading: `QLocalServer`/`QLocalSocket` are Qt objects, so everything here is
GUI-thread only, and every wait is bounded — a launch must never hang because a
wedged primary stopped reading.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Final

from PySide6.QtCore import QCoreApplication, QEvent, QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from onedriveui import paths

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────────────────────────────────────

#: Wire protocol version. A peer announcing anything else is answered and
#: ignored rather than acted on.
PROTOCOL_VERSION: Final[int] = 1

OP_LAUNCH: Final[str] = "launch"
OP_OK: Final[str] = "ok"
OP_ERROR: Final[str] = "error"

#: NDJSON: one compact JSON object per line.
LINE_SEP: Final[bytes] = b"\n"

#: A single message may not exceed this. A hostile or broken peer that never
#: sends a newline would otherwise grow the buffer without limit.
MAX_LINE_BYTES: Final[int] = 256 * 1024

# ─────────────────────────────────────────────────────────────────────────────
# Timeouts — every one of them bounded, all at launch time
# ─────────────────────────────────────────────────────────────────────────────

#: How long a starting instance waits to reach an existing one. Generous enough
#: for a busy login, short enough that a stale socket costs nothing noticeable.
CONNECT_TIMEOUT_MS: Final[int] = 500

#: How long the secondary waits for its argv to reach the socket.
SEND_TIMEOUT_MS: Final[int] = 1000

#: How long it then waits for the `{"op":"ok"}` reply. Deliberately short and
#: deliberately not fatal: once `waitForBytesWritten()` has succeeded on a
#: connected socket the message is delivered, and the acknowledgement only tells
#: us the primary got as far as parsing it. A primary busy painting its first
#: frame can legitimately be slower than this.
ACK_TIMEOUT_MS: Final[int] = 300

#: How long `try_acquire()` waits for the lock file.
LOCK_TIMEOUT_MS: Final[int] = 200

#: `QLockFile`'s time-based staleness is disabled: the primary legitimately
#: holds this lock for days, and a lock whose owner is still running must never
#: be stolen. Qt's pid-based check still reclaims a lock whose owner died.
STALE_LOCK_TIME_MS: Final[int] = 0

#: Socket permissions. The runtime directory is already 0700; this makes the
#: socket itself owner-only too, so neither alone has to be relied on.
SOCKET_OPTIONS: Final[QLocalServer.SocketOption] = (
    QLocalServer.SocketOption.UserAccessOption
)


def _encode(payload: dict[str, Any]) -> bytes:
    """Serialise one NDJSON message.

    Args:
        payload: The message object.

    Returns:
        Compact UTF-8 JSON plus a newline.
    """
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + LINE_SEP


class SingleInstance(QObject):
    """The single-instance guard and the argv hand-over channel.

    Attributes:
        message: Emitted in the primary instance with the `argv` list a
            secondary launch handed over. The composition root turns that into
            "raise the window", "open the folder", and so on.
    """

    #: `list[str]` — the argv of a second launch.
    message = Signal(list)

    def __init__(self, parent: QObject | None = None, *,
                 socket_path: str | os.PathLike[str] | None = None,
                 lock_path: str | os.PathLike[str] | None = None) -> None:
        """
        Args:
            parent: Qt parent.
            socket_path: Override the socket location. Defaults to
                `paths.ui_socket()`; tests point it at a temp runtime dir.
            lock_path: Override the lock location. Defaults to
                `paths.ui_lock()`.
        """
        super().__init__(parent)
        self._socket_path = str(socket_path) if socket_path is not None else str(paths.ui_socket())
        self._lock_path = str(lock_path) if lock_path is not None else str(paths.ui_lock())
        self._server: QLocalServer | None = None
        self._lock: QLockFile | None = None
        self._peers: dict[QLocalSocket, bytearray] = {}
        self._primary = False

    # ── introspection ────────────────────────────────────────────────────────

    @property
    def socket_path(self) -> str:
        """The absolute socket path. Never a bare name, never under `/tmp`."""
        return self._socket_path

    @property
    def lock_path(self) -> str:
        """The absolute lock-file path."""
        return self._lock_path

    @property
    def is_primary(self) -> bool:
        """Whether this process holds the instance."""
        return self._primary

    @property
    def server(self) -> QLocalServer | None:
        """The listening server, or `None` in a secondary process."""
        return self._server

    def peer_info(self) -> tuple[int, str, str] | None:
        """Who currently holds the lock.

        Returns:
            `(pid, hostname, application)` from the lock file, or `None` when
            nothing holds it.
        """
        lock = self._lock if self._lock is not None else QLockFile(self._lock_path)
        pid, host, app = lock.getLockInfo()
        return (int(pid), str(host), str(app)) if pid else None

    # ── acquisition ──────────────────────────────────────────────────────────

    def try_acquire(self) -> bool:
        """Become the primary instance, or discover that one already exists.

        The order matters. Connecting first means a live primary is detected by
        the only test that cannot lie — it answered — before any file is touched
        or removed. Only once that fails do we treat the socket as debris.

        Returns:
            True if this process is now the primary and is listening. False if
            another instance is already running, in which case the caller should
            `send()` its argv and exit 0.
        """
        if self._primary:
            return True
        # Creates $XDG_RUNTIME_DIR/onedriveui with mode 0700 if it is not there.
        paths.runtime_dir()

        if self._probe_existing():
            log.info("another instance is listening on %s", self._socket_path)
            return False

        if not self._take_lock():
            log.info("another instance holds %s", self._lock_path)
            return False

        # Nothing answered, and we hold the lock: any socket file left here is
        # debris from a crash. QLocalServer refuses to listen over it otherwise.
        QLocalServer.removeServer(self._socket_path)

        server = QLocalServer(self)
        server.setSocketOptions(SOCKET_OPTIONS)
        if not server.listen(self._socket_path):
            log.error("cannot listen on %s: %s", self._socket_path, server.errorString())
            self._release_lock()
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        self._primary = True
        log.info("primary instance listening on %s", server.fullServerName())
        return True

    def _probe_existing(self) -> bool:
        """Whether a primary instance answers on the socket.

        Returns:
            True if a connection succeeded.
        """
        probe = QLocalSocket()
        try:
            probe.connectToServer(self._socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
            if probe.waitForConnected(CONNECT_TIMEOUT_MS):
                probe.disconnectFromServer()
                return True
            return False
        finally:
            # No deleteLater(): a parentless QLocalSocket is owned by PySide6,
            # so it is freed when this reference goes out of scope. A queued
            # deferred delete would then hit freed memory the next time a real
            # event loop runs — a segfault far from its cause.
            probe.abort()

    def _take_lock(self) -> bool:
        """Acquire the lock file, reclaiming it only from a genuinely dead owner.

        `QLockFile.removeStaleLockFile()` was measured on the target machine to
        delete the lock **even when the owning pid is alive** — it does not
        check. So the liveness test below is load-bearing, not a belt-and-braces
        extra: without it, a second launch would evict a running primary from
        its own lock. It runs only after `tryLock` has already failed and only
        when the recorded host is this one, since a pid from another machine
        says nothing about a process here.

        Returns:
            True if the lock is now held.
        """
        lock = QLockFile(self._lock_path)
        # A running primary holds this for days: time-based staleness would let
        # a second launch steal it. Qt's pid check is the only one we want.
        lock.setStaleLockTime(STALE_LOCK_TIME_MS)
        if lock.tryLock(LOCK_TIMEOUT_MS):
            self._lock = lock
            return True
        if lock.error() != QLockFile.LockError.LockFailedError:
            return False
        pid, host, app = lock.getLockInfo()
        if not pid:
            return False
        if host and host != _hostname():
            log.info("lock at %s belongs to %s (pid %s); leaving it alone",
                     self._lock_path, host, pid)
            return False
        if _pid_is_alive(int(pid)):
            log.info("lock at %s is held by live pid %s (%s)",
                     self._lock_path, pid, app)
            return False
        log.info("reclaiming the lock at %s from dead pid %s", self._lock_path, pid)
        lock.removeStaleLockFile()
        if lock.tryLock(LOCK_TIMEOUT_MS):
            self._lock = lock
            return True
        return False

    def _release_lock(self) -> None:
        """Drop the lock file, if held."""
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    # ── the primary side ─────────────────────────────────────────────────────

    def _on_new_connection(self) -> None:
        """Accept every pending peer and wire up its reader."""
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            peer = self._server.nextPendingConnection()
            if peer is None:
                break
            self._peers[peer] = bytearray()
            peer.readyRead.connect(lambda p=peer: self._on_ready_read(p))
            peer.disconnected.connect(lambda p=peer: self._drop_peer(p))

    def _on_ready_read(self, peer: QLocalSocket) -> None:
        """Consume whatever bytes arrived, dispatching every complete line.

        `readyRead` delivers arbitrary chunks, not lines, so the tail is kept
        until its newline turns up.

        Args:
            peer: The connected secondary instance.
        """
        buffer = self._peers.get(peer)
        if buffer is None:
            return
        buffer += bytes(peer.readAll().data())
        if len(buffer) > MAX_LINE_BYTES:
            log.warning("peer sent %d bytes with no newline; dropping it", len(buffer))
            self._drop_peer(peer)
            peer.abort()
            return
        while LINE_SEP in buffer:
            raw, _, rest = bytes(buffer).partition(LINE_SEP)
            buffer.clear()
            buffer += rest
            self._handle_line(peer, raw)

    def _handle_line(self, peer: QLocalSocket, raw: bytes) -> None:
        """Act on one NDJSON message and answer it.

        Args:
            peer: The connected secondary instance.
            raw: One line, without its newline.
        """
        if not raw.strip():
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("undecodable single-instance message: %s", exc)
            self._reply(peer, {"op": OP_ERROR, "v": PROTOCOL_VERSION,
                               "error": "bad json"})
            return
        if not isinstance(payload, dict) or payload.get("op") != OP_LAUNCH:
            self._reply(peer, {"op": OP_ERROR, "v": PROTOCOL_VERSION,
                               "error": "unknown op"})
            return
        argv = [str(a) for a in payload.get("argv", []) if isinstance(a, (str, int, float))]
        log.info("second launch handed over argv=%r", argv)
        # Answer first: the secondary is blocked on this reply before it exits,
        # and whatever `message` triggers must not delay it.
        self._reply(peer, {"op": OP_OK, "v": PROTOCOL_VERSION, "pid": os.getpid()})
        self.message.emit(argv)

    def _reply(self, peer: QLocalSocket, payload: dict[str, Any]) -> None:
        """Write one NDJSON response.

        Args:
            peer: The connected secondary instance.
            payload: The response object.
        """
        if peer.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return
        peer.write(_encode(payload))
        peer.flush()

    def _drop_peer(self, peer: QLocalSocket) -> None:
        """Forget a disconnected peer.

        Args:
            peer: The socket to release.
        """
        self._peers.pop(peer, None)
        peer.deleteLater()

    # ── the secondary side ───────────────────────────────────────────────────

    def send(self, argv: list[str] | None = None, *,
             timeout_ms: int = SEND_TIMEOUT_MS) -> bool:
        """Hand this launch's argv to the running instance.

        Args:
            argv: The arguments to forward. `None` means `sys.argv[1:]`.
            timeout_ms: Bound on the whole exchange.

        Returns:
            True if the primary acknowledged. False means no instance answered —
            it exited between `try_acquire()` and here — and the caller should
            retry `try_acquire()` rather than exiting silently.
        """
        payload = {
            "op": OP_LAUNCH,
            "v": PROTOCOL_VERSION,
            "argv": list(sys.argv[1:] if argv is None else argv),
            "cwd": os.getcwd(),
            "pid": os.getpid(),
        }
        sock = QLocalSocket()
        try:
            sock.connectToServer(self._socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
            if not sock.waitForConnected(CONNECT_TIMEOUT_MS):
                log.warning("no instance answered on %s: %s",
                            self._socket_path, sock.errorString())
                return False
            sock.write(_encode(payload))
            if not sock.waitForBytesWritten(timeout_ms):
                log.warning("handing over argv timed out")
                return False
            buffer = bytearray()
            while LINE_SEP not in buffer:
                if not sock.waitForReadyRead(ACK_TIMEOUT_MS):
                    # The bytes are on a connected socket, so the primary has
                    # them; it simply has not answered yet. Reporting failure
                    # here would make the caller start a second copy, which is
                    # the exact outcome this module exists to prevent.
                    log.info("argv delivered; no acknowledgement within %d ms",
                             ACK_TIMEOUT_MS)
                    return True
                buffer += bytes(sock.readAll().data())
            reply = _decode_line(bytes(buffer).split(LINE_SEP, 1)[0])
            return bool(reply) and reply.get("op") == OP_OK
        finally:
            # Parentless and therefore PySide6-owned: see `_probe_existing()`.
            sock.disconnectFromServer()
            sock.abort()

    # ── teardown ─────────────────────────────────────────────────────────────

    def release(self) -> None:
        """Stop listening, drop the lock and remove the socket.

        Idempotent, and safe to call in a secondary process that never acquired
        anything. Call it from `App.shutdown()`: a socket left behind is only
        debris the next launch has to clean up, but the lock file would make the
        next launch pay for a pid probe.

        The pending `deleteLater()` events are drained before returning.
        `QCoreApplication.processEvents()` never delivers `DeferredDelete`, so
        without this the sockets accepted here would keep a delete event queued
        against them long after this object is gone, and the next real
        `QEventLoop.exec()` would deliver it to freed memory.
        """
        for peer in list(self._peers):
            self._peers.pop(peer, None)
            peer.abort()
            peer.deleteLater()
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
            QLocalServer.removeServer(self._socket_path)
        self._release_lock()
        self._primary = False
        _drain_deferred_deletes()


def _decode_line(raw: bytes) -> dict[str, Any] | None:
    """Parse one NDJSON line.

    Args:
        raw: The line, without its newline.

    Returns:
        The decoded object, or `None` when it is not a JSON object.
    """
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _drain_deferred_deletes() -> None:
    """Deliver every pending `deleteLater()` now.

    `QCoreApplication.processEvents()` does not deliver `DeferredDelete` — only
    an event loop's own unwinding does — so an object torn down outside one
    leaves the event queued indefinitely.
    """
    if QCoreApplication.instance() is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _hostname() -> str:
    """This machine's name, as `QLockFile` records it.

    Returns:
        The node name, or `""` when it cannot be read.
    """
    try:
        return os.uname().nodename
    except OSError:
        return ""


def _pid_is_alive(pid: int) -> bool:
    """Whether a process exists.

    Args:
        pid: The process id from the lock file.

    Returns:
        True if the pid is live. A pid we may not signal is still live.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def acquire_or_forward(argv: list[str] | None = None, *,
                       parent: QObject | None = None) -> SingleInstance | None:
    """The whole single-instance dance, as one call for `main()`.

    Args:
        argv: Arguments to forward when another instance is running. `None`
            means `sys.argv[1:]`.
        parent: Qt parent for the guard.

    Returns:
        The listening `SingleInstance` in the primary process; `None` in a
        secondary process, which should then `sys.exit(0)`.
    """
    guard = SingleInstance(parent)
    if guard.try_acquire():
        return guard
    if not guard.send(argv):
        # The primary vanished between the probe and the hand-over. One retry,
        # because otherwise this launch would exit having done nothing at all.
        if guard.try_acquire():
            return guard
    return None


__all__ = [
    "SingleInstance", "acquire_or_forward",
    "PROTOCOL_VERSION", "OP_LAUNCH", "OP_OK", "OP_ERROR", "LINE_SEP",
    "MAX_LINE_BYTES", "CONNECT_TIMEOUT_MS", "SEND_TIMEOUT_MS", "ACK_TIMEOUT_MS",
    "LOCK_TIMEOUT_MS", "STALE_LOCK_TIME_MS", "SOCKET_OPTIONS",
]
