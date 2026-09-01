"""The NDJSON socket the Nautilus extension talks to. **20 ms, hard.**

`Nautilus.InfoProvider.update_file_info` runs on the file manager's UI thread
and **must be synchronous**: the asynchronous `OperationResult.IN_PROGRESS`
protocol needs a `Nautilus.OperationHandle`, which is an opaque boxed struct
with no Python constructor —

    TypeError: struct cannot be created directly; try using a constructor

— so returning one from Python is impossible. Whatever the extension asks us,
it asks *inline, while Nautilus is painting a row*. If we take 200 ms, the
user's file manager freezes for 200 ms.

Hence the shape of this server:

* **A hard `BUDGET_MS = 20` per request**, measured with `perf_counter()` around
  the answer. Paths still unresolved when the budget runs out are answered
  `unknown` (`FileState.UNKNOWN`) rather than made to wait — an unknown state
  draws no emblem, which is exactly the right degradation.
* **Nothing here ever blocks.** Reads are `readyRead`-driven and buffered,
  writes go straight into Qt's socket buffer, and the state lookup is a call
  into an injected provider that reads `cache_index`/`pins` from memory. No
  SQLite write, no rc call, no filesystem walk.
* **A push channel.** Once a peer has said `hello` it stays connected and
  receives unsolicited `{"op":"invalidate","paths":[…]}` frames, which the
  extension turns into `FileInfo.invalidate_extension_info()` calls. That is the
  only supported way to make Nautilus re-read a file's emblem, and it is why the
  connection is long-lived rather than request/response.
* **Connect-refused when we are down.** `stop()` removes the socket file, so an
  extension started before the application gets `ENOENT`/`ECONNREFUSED`
  immediately instead of a 200 ms timeout on every single row.

Wire protocol — newline-delimited JSON over `$XDG_RUNTIME_DIR/onedriveui/ipc.sock`:

    -> {"op":"hello","v":1}
    <- {"op":"hello","v":1,"account":"onedrive","root":"/home/u/OneDrive"}
    -> {"op":"state","paths":["/abs/a","/abs/b"]}
    <- {"op":"state","states":{"/abs/a":"online_only","/abs/b":"pinned"}}
    -> {"op":"menu","paths":[...]}          <- {"op":"menu","actions":[...]}
    -> {"op":"do","action":"pin","paths":[...]}   <- {"op":"ok"}
    <= {"op":"invalidate","paths":[...]}    (server push, unsolicited)

Threading: Qt sockets, so GUI thread only. Every answer must therefore be cheap
enough to compute between two frames — which is what the budget enforces.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from onedriveui import paths
from onedriveui.bus import BUS
from onedriveui.constants import IPC_BUDGET_MS, NAUTILUS_IPC_TIMEOUT_MS
from onedriveui.models import FileState

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Protocol
# ─────────────────────────────────────────────────────────────────────────────

#: Wire protocol version. Bumped only for an incompatible change; the extension
#: refuses to talk to a server announcing a version it does not know.
PROTOCOL_VERSION: Final[int] = 1

OP_HELLO: Final[str] = "hello"
OP_STATE: Final[str] = "state"
OP_MENU: Final[str] = "menu"
OP_DO: Final[str] = "do"
OP_OK: Final[str] = "ok"
OP_ERROR: Final[str] = "error"
OP_INVALIDATE: Final[str] = "invalidate"

#: Every operation a peer may send.
REQUEST_OPS: Final[frozenset[str]] = frozenset({OP_HELLO, OP_STATE, OP_MENU, OP_DO})

#: The documented protocol, exported so the extension and the tests share one
#: definition instead of two that drift.
PROTOCOL: Final[dict[str, Any]] = {
    "version": PROTOCOL_VERSION,
    "requests": {
        OP_HELLO: (),
        OP_STATE: ("paths",),
        OP_MENU: ("paths",),
        OP_DO: ("action", "paths"),
    },
    "responses": {
        OP_HELLO: ("v", "account", "root", "budget_ms"),
        OP_STATE: ("states",),
        OP_MENU: ("actions",),
        OP_OK: (),
        OP_ERROR: ("error",),
    },
    "push": {OP_INVALIDATE: ("paths",)},
}

LINE_SEP: Final[bytes] = b"\n"

#: A single request may not exceed this. A 1 000-path `state` query is roughly
#: 60 KB, so this leaves an order of magnitude of headroom while still capping
#: what one peer can make us buffer.
MAX_LINE_BYTES: Final[int] = 4 * 1024 * 1024

#: Requests dropped per connection before it is closed. A peer that keeps
#: sending junk is broken, not a client.
MAX_BAD_REQUESTS: Final[int] = 8

# ─────────────────────────────────────────────────────────────────────────────
# Budget
# ─────────────────────────────────────────────────────────────────────────────

#: The hard per-request budget, from `constants`. Never re-typed, never tuned
#: locally: it exists because Nautilus calls us on its UI thread.
BUDGET_MS: Final[int] = IPC_BUDGET_MS

#: What the extension sets as its own call timeout. Ours must be well inside it.
CLIENT_TIMEOUT_MS: Final[int] = NAUTILUS_IPC_TIMEOUT_MS

#: The answer for any path we could not resolve in time. Draws no emblem.
UNKNOWN: Final[str] = FileState.UNKNOWN.value

#: Socket permissions: 0600, inside a 0700 runtime directory.
SOCKET_OPTIONS: Final[QLocalServer.SocketOption] = (
    QLocalServer.SocketOption.UserAccessOption
)

#: `(path list) -> {path: FileState value}`. Injected by the composition root so
#: this module never imports the data layer; the real one reads `cache_index`
#: and `pins` out of memory.
StateProvider = Callable[[Sequence[str]], Mapping[str, str]]

#: `(path list) -> [action id]`. Which context-menu verbs apply to a selection.
MenuProvider = Callable[[Sequence[str]], Sequence[str]]


def _encode(payload: Mapping[str, Any]) -> bytes:
    """Serialise one NDJSON frame.

    Args:
        payload: The message object.

    Returns:
        Compact UTF-8 JSON plus a newline.
    """
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + LINE_SEP


def _drain_deferred_deletes() -> None:
    """Deliver every pending `deleteLater()` now.

    `QCoreApplication.processEvents()` does not deliver `DeferredDelete` — only
    an event loop's own unwinding does — so an object torn down outside one
    leaves the event queued indefinitely. Draining explicitly keeps a socket's
    destruction inside the call that asked for it.
    """
    app = QCoreApplication.instance()
    if app is not None:
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _as_paths(value: Any) -> list[str]:
    """Coerce a request's `paths` field to a list of strings.

    Args:
        value: Whatever arrived in the JSON.

    Returns:
        The strings in it; anything else is dropped rather than rejected, so one
        bad entry never costs the whole selection its emblems.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


class IpcServer(QObject):
    """The NDJSON server the Nautilus extension connects to.

    Attributes:
        action_requested: `(verb, [absolute path])` — a context-menu verb the
            user chose in the file manager. Mirrored onto
            `BUS.ipc_action_requested`, which is what the Supervisor listens to.
    """

    #: `(str verb, list[str] absolute paths)`.
    action_requested = Signal(str, list)

    #: The hard per-request budget, in milliseconds.
    BUDGET_MS: Final[int] = BUDGET_MS

    #: The wire protocol, for the extension and the tests.
    PROTOCOL: Final[dict[str, Any]] = PROTOCOL

    def __init__(self, parent: QObject | None = None, *,
                 socket_path: str | os.PathLike[str] | None = None,
                 state_provider: StateProvider | None = None,
                 menu_provider: MenuProvider | None = None,
                 account_id: str = "",
                 sync_root: str | os.PathLike[str] = "",
                 mirror_to_bus: bool = True) -> None:
        """
        Args:
            parent: Qt parent.
            socket_path: Override the socket location. Defaults to
                `paths.ipc_socket()`.
            state_provider: `(paths) -> {path: FileState value}`. Must answer
                from memory. `None` answers everything `unknown`.
            menu_provider: `(paths) -> [action id]`. `None` offers no actions.
            account_id: Announced in the `hello` response.
            sync_root: Announced in the `hello` response, so the extension knows
                which tree to badge.
            mirror_to_bus: Also emit `BUS.ipc_action_requested` for every `do`.
        """
        super().__init__(parent)
        self._socket_path = str(socket_path) if socket_path is not None else str(paths.ipc_socket())
        self._state_provider = state_provider
        self._menu_provider = menu_provider
        self._account_id = account_id
        self._sync_root = str(sync_root)
        self._mirror_to_bus = mirror_to_bus
        self._server: QLocalServer | None = None
        self._peers: dict[QLocalSocket, bytearray] = {}
        self._bad: dict[QLocalSocket, int] = {}
        self._subscribers: set[QLocalSocket] = set()
        #: Per-peer ``(readyRead, disconnected)`` slots, kept so `_release()`
        #: can disconnect them *by reference* — see its docstring.
        self._handlers: dict[QLocalSocket, tuple[Callable[[], None],
                                                 Callable[[], None]]] = {}
        self._requests = 0
        self._over_budget = 0
        self._max_ms = 0.0

    # ── configuration ────────────────────────────────────────────────────────

    @property
    def socket_path(self) -> str:
        """The absolute socket path — under `$XDG_RUNTIME_DIR`, mode 0600."""
        return self._socket_path

    @property
    def is_listening(self) -> bool:
        """Whether the server is accepting connections."""
        return self._server is not None and self._server.isListening()

    @property
    def peer_count(self) -> int:
        """How many extensions are connected."""
        return len(self._peers)

    def set_state_provider(self, provider: StateProvider | None) -> None:
        """Install the `cache_index`/`pins` lookup.

        Args:
            provider: `(paths) -> {path: FileState value}`, answering from
                memory, or `None` to answer everything `unknown`.
        """
        self._state_provider = provider

    def set_menu_provider(self, provider: MenuProvider | None) -> None:
        """Install the context-menu lookup.

        Args:
            provider: `(paths) -> [action id]`, or `None` for no actions.
        """
        self._menu_provider = provider

    def set_account(self, account_id: str, sync_root: str | os.PathLike[str]) -> None:
        """Update what `hello` announces.

        Args:
            account_id: The active account.
            sync_root: Its sync root.
        """
        self._account_id = account_id
        self._sync_root = str(sync_root)

    def stats(self) -> dict[str, float | int]:
        """Counters for the diagnostics pane.

        Returns:
            `requests`, `over_budget`, `max_ms`, `peers`, `subscribers`.
        """
        return {
            "requests": self._requests,
            "over_budget": self._over_budget,
            "max_ms": round(self._max_ms, 3),
            "peers": len(self._peers),
            "subscribers": len(self._subscribers),
        }

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Listen on the IPC socket.

        Returns:
            True if the server is listening. A stale socket from a crashed
            process is removed first — `QLocalServer` refuses to bind over one.
        """
        if self.is_listening:
            return True
        paths.runtime_dir()
        QLocalServer.removeServer(self._socket_path)
        server = QLocalServer(self)
        server.setSocketOptions(SOCKET_OPTIONS)
        if not server.listen(self._socket_path):
            log.error("IPC cannot listen on %s: %s",
                      self._socket_path, server.errorString())
            server.deleteLater()
            return False
        server.newConnection.connect(self._on_new_connection)
        self._server = server
        log.info("IPC listening on %s (budget %d ms)",
                 server.fullServerName(), self.BUDGET_MS)
        return True

    def stop(self) -> None:
        """Stop listening, drop every peer, and remove the socket file.

        Removing the file is the point: an extension that starts before us then
        fails to connect *immediately* instead of spending its 200 ms timeout on
        every row it paints.

        The final `sendPostedEvents(None, DeferredDelete)` is not decoration.
        `QCoreApplication.processEvents()` deliberately does **not** deliver
        `deleteLater()`, so without an explicit drain every socket this server
        ever accepted leaves a `DeferredDelete` queued against it. Those events
        outlive the server, and the first real `QEventLoop.exec()` afterwards
        delivers them to memory that is gone — a segfault with no connection to
        the code that caused it.
        """
        for peer in list(self._peers):
            self._forget(peer)
            self._release(peer)
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None
        QLocalServer.removeServer(self._socket_path)
        _drain_deferred_deletes()
        log.info("IPC stopped; %s removed", self._socket_path)

    # ── connections ──────────────────────────────────────────────────────────

    def _on_new_connection(self) -> None:
        """Accept every pending extension and wire up its reader."""
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            peer = self._server.nextPendingConnection()
            if peer is None:
                break
            self._peers[peer] = bytearray()
            self._bad[peer] = 0
            on_read = lambda p=peer: self._on_ready_read(p)          # noqa: E731
            on_gone = lambda p=peer: self._on_disconnected(p)        # noqa: E731
            self._handlers[peer] = (on_read, on_gone)
            peer.readyRead.connect(on_read)
            peer.disconnected.connect(on_gone)

    def _on_disconnected(self, peer: QLocalSocket) -> None:
        """Release a peer that went away.

        Args:
            peer: The socket.
        """
        self._forget(peer)
        self._release(peer)

    def _release(self, peer: QLocalSocket) -> None:
        """Unhook a peer's signals and schedule its deletion, exactly once.

        Both halves matter, and both were measured on PySide6 6.11:

        * **Disconnect first.** ``abort()`` emits ``disconnected``
          *synchronously*, so tearing a peer down without unhooking re-enters
          :meth:`_on_disconnected` from inside :meth:`stop`.
        * **Tolerate a dead wrapper.** PySide6 raises ``RuntimeError``
          ("Internal C++ object already deleted") from *any* method on a socket
          whose C++ half a delivered ``DeferredDelete`` has already reaped. A
          late ``disconnected`` for such a peer would otherwise throw straight
          out of a Qt slot, where there is no caller to catch it.

        The handlers are disconnected *by reference* and only while this peer is
        still registered: a bare ``disconnect()`` on an already-unhooked signal
        emits a ``RuntimeWarning`` rather than raising, which no ``except``
        clause can suppress.

        Args:
            peer: The socket to release. Safe to call more than once, and safe
                after the underlying C++ object is gone.
        """
        handlers = self._handlers.pop(peer, None)
        if handlers is not None:
            on_read, on_gone = handlers
            try:
                peer.readyRead.disconnect(on_read)
                peer.disconnected.disconnect(on_gone)
            except (RuntimeError, TypeError):
                pass
        try:
            peer.abort()
            peer.deleteLater()
        except RuntimeError:
            pass          # already reaped; nothing left to free

    def _forget(self, peer: QLocalSocket) -> None:
        """Drop every reference to a peer.

        Args:
            peer: The socket.
        """
        self._peers.pop(peer, None)
        self._bad.pop(peer, None)
        self._subscribers.discard(peer)

    def _on_ready_read(self, peer: QLocalSocket) -> None:
        """Consume whatever arrived and dispatch every complete line.

        `readyRead` delivers arbitrary chunks, never lines, so the tail is kept
        until its newline turns up.

        Args:
            peer: The connected extension.
        """
        buffer = self._peers.get(peer)
        if buffer is None:
            return
        buffer += bytes(peer.readAll().data())
        if len(buffer) > MAX_LINE_BYTES:
            log.warning("IPC peer sent %d bytes with no newline; closing it",
                        len(buffer))
            self._forget(peer)
            self._release(peer)
            return
        while LINE_SEP in buffer:
            raw, _, rest = bytes(buffer).partition(LINE_SEP)
            buffer.clear()
            buffer += rest
            if raw.strip():
                self._dispatch(peer, raw)

    # ── request handling ─────────────────────────────────────────────────────

    def _dispatch(self, peer: QLocalSocket, raw: bytes) -> None:
        """Answer one request, inside the budget.

        Args:
            peer: The connected extension.
            raw: One line, without its newline.
        """
        started = time.perf_counter()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._bad_request(peer, f"bad json: {exc}")
            return
        if not isinstance(payload, dict):
            self._bad_request(peer, "not an object")
            return
        op = payload.get("op")
        if op not in REQUEST_OPS:
            self._bad_request(peer, f"unknown op {op!r}")
            return

        if op == OP_HELLO:
            self._subscribers.add(peer)
            response: dict[str, Any] = {
                "op": OP_HELLO, "v": PROTOCOL_VERSION,
                "account": self._account_id, "root": self._sync_root,
                "budget_ms": self.BUDGET_MS,
            }
        elif op == OP_STATE:
            response = {"op": OP_STATE,
                        "states": self._states(_as_paths(payload.get("paths")), started)}
        elif op == OP_MENU:
            response = {"op": OP_MENU,
                        "actions": self._menu(_as_paths(payload.get("paths")))}
        else:
            response = self._do(payload)

        self._send(peer, response)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._requests += 1
        self._max_ms = max(self._max_ms, elapsed_ms)
        if elapsed_ms > self.BUDGET_MS:
            self._over_budget += 1
            log.warning("IPC %s took %.1f ms, over the %d ms budget",
                        op, elapsed_ms, self.BUDGET_MS)

    def _bad_request(self, peer: QLocalSocket, reason: str) -> None:
        """Answer a malformed request, closing a peer that keeps sending them.

        Args:
            peer: The connected extension.
            reason: What was wrong.
        """
        log.warning("IPC bad request: %s", reason)
        self._send(peer, {"op": OP_ERROR, "v": PROTOCOL_VERSION, "error": reason})
        self._bad[peer] = self._bad.get(peer, 0) + 1
        if self._bad[peer] >= MAX_BAD_REQUESTS:
            log.warning("IPC closing a peer after %d bad requests", self._bad[peer])
            self._forget(peer)
            self._release(peer)

    def _states(self, wanted: Sequence[str], started: float) -> dict[str, str]:
        """Resolve file states, abandoning the lookup at the budget.

        The provider is asked for everything at once — one batched lookup is
        what makes a thousand paths affordable — and the budget is checked
        before and after. On a timeout every path is answered `unknown`, which
        draws no emblem: a missing badge is a far better outcome than a frozen
        file manager.

        Args:
            wanted: The absolute paths the extension asked about.
            started: `perf_counter()` at the top of the request.

        Returns:
            `{path: FileState value}`, with an entry for every requested path.
        """
        if not wanted:
            return {}
        budget_s = self.BUDGET_MS / 1000.0
        out = {path: UNKNOWN for path in wanted}
        provider = self._state_provider
        if provider is None:
            return out
        if time.perf_counter() - started >= budget_s:
            log.warning("IPC state budget spent before the lookup; %d unknown",
                        len(wanted))
            return out
        try:
            resolved = provider(wanted)
        except Exception:
            # A provider bug must degrade to "unknown", never take down the
            # file manager's rendering.
            log.exception("IPC state provider raised; answering unknown")
            return out
        if time.perf_counter() - started >= budget_s:
            log.warning("IPC state lookup exceeded %d ms; answering unknown",
                        self.BUDGET_MS)
            return out
        for path, state in resolved.items():
            if path in out and isinstance(state, str):
                out[path] = state
        return out

    def _menu(self, wanted: Sequence[str]) -> list[str]:
        """Which context-menu verbs apply to a selection.

        Args:
            wanted: The selected absolute paths.

        Returns:
            The action ids, or `[]` when no provider is installed.
        """
        provider = self._menu_provider
        if provider is None or not wanted:
            return []
        try:
            return [str(action) for action in provider(wanted)]
        except Exception:
            log.exception("IPC menu provider raised; offering no actions")
            return []

    def _do(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Route a context-menu verb into the application.

        The verb is *emitted*, never executed here: the UI layer never calls a
        service directly, and neither does this. `Supervisor` is what acts.

        Args:
            payload: The decoded request.

        Returns:
            The response frame.
        """
        action = payload.get("action")
        targets = _as_paths(payload.get("paths"))
        if not isinstance(action, str) or not action:
            return {"op": OP_ERROR, "v": PROTOCOL_VERSION, "error": "no action"}
        log.info("IPC action %r on %d path(s)", action, len(targets))
        self.action_requested.emit(action, targets)
        if self._mirror_to_bus:
            BUS.ipc_action_requested.emit(action, targets)
        return {"op": OP_OK, "v": PROTOCOL_VERSION}

    # ── push ─────────────────────────────────────────────────────────────────

    def broadcast_invalidate(self, targets: Iterable[str]) -> int:
        """Tell every connected extension that some paths changed.

        The extension answers by calling `FileInfo.invalidate_extension_info()`
        on each one, which is the only supported way to make Nautilus re-run
        `update_file_info` and repaint an emblem.

        Args:
            targets: Absolute paths whose state changed.

        Returns:
            How many peers were notified.
        """
        items = [str(path) for path in targets]
        if not items or not self._subscribers:
            return 0
        frame = _encode({"op": OP_INVALIDATE, "v": PROTOCOL_VERSION, "paths": items})
        sent = 0
        for peer in list(self._subscribers):
            if peer.state() != QLocalSocket.LocalSocketState.ConnectedState:
                self._forget(peer)
                continue
            peer.write(frame)
            peer.flush()
            sent += 1
        log.debug("IPC pushed %d invalidation(s) to %d peer(s)", len(items), sent)
        return sent

    def _send(self, peer: QLocalSocket, payload: Mapping[str, Any]) -> None:
        """Write one response frame.

        Args:
            peer: The connected extension.
            payload: The response object.
        """
        if peer.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return
        peer.write(_encode(payload))
        peer.flush()


class IpcClient:
    """A blocking client for the IPC socket — what the extension does.

    The Nautilus extension cannot import this module (it runs under the system
    interpreter, dependency-free), so this is a faithful reimplementation of its
    half of the protocol, used by the tests and by any in-tree tool that wants to
    ask the running application a question.

    Every wait is bounded by `CLIENT_TIMEOUT_MS`, and a server that is not
    running fails immediately with connect-refused rather than timing out.
    """

    def __init__(self, socket_path: str | os.PathLike[str] | None = None, *,
                 timeout_ms: int = CLIENT_TIMEOUT_MS) -> None:
        """
        Args:
            socket_path: The server's socket, or `None` for `paths.ipc_socket()`.
            timeout_ms: Bound on every call.
        """
        self.socket_path = str(socket_path) if socket_path is not None else str(paths.ipc_socket())
        self.timeout_ms = timeout_ms
        self._sock: QLocalSocket | None = None
        self._buffer = bytearray()

    def connect(self) -> bool:
        """Open the connection.

        Returns:
            True if the server answered. False means it is not running, which
            the extension treats as "no OneDrive today" and retries later.
        """
        sock = QLocalSocket()
        sock.connectToServer(self.socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
        if not sock.waitForConnected(self.timeout_ms):
            # No deleteLater(): this socket has no parent, so PySide6 — not Qt —
            # owns it, and dropping the last Python reference frees it now. A
            # deferred-delete event would then land on freed memory the next
            # time a real event loop runs, which segfaults the process.
            sock.abort()
            return False
        self._sock = sock
        self._buffer = bytearray()
        return True

    def close(self) -> None:
        """Disconnect. Idempotent."""
        if self._sock is not None:
            self._sock.disconnectFromServer()
            self._sock.abort()
            self._sock = None       # parentless: dropping the reference frees it

    def __enter__(self) -> "IpcClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        """Whether the connection is open."""
        return (self._sock is not None
                and self._sock.state() == QLocalSocket.LocalSocketState.ConnectedState)

    def request(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Send one request and read its response.

        Args:
            payload: The request frame.

        Returns:
            The decoded response, or `None` on timeout or disconnect.
        """
        if not self.send(payload):
            return None
        return self.read(skip_pushes=True)

    def send(self, payload: Mapping[str, Any]) -> bool:
        """Write one frame without waiting for an answer.

        Args:
            payload: The request frame.

        Returns:
            True if the bytes reached the socket.
        """
        if self._sock is None:
            return False
        self._sock.write(_encode(payload))
        return bool(self._sock.waitForBytesWritten(self.timeout_ms))

    def read(self, *, skip_pushes: bool = False) -> dict[str, Any] | None:
        """Read one frame.

        Args:
            skip_pushes: Discard unsolicited `invalidate` frames, so a caller
                waiting for a reply is not handed a push that arrived first.

        Returns:
            The decoded frame, or `None` on timeout.
        """
        while True:
            frame = self._next_frame()
            if frame is None:
                return None
            if skip_pushes and frame.get("op") == OP_INVALIDATE:
                continue
            return frame

    def _next_frame(self) -> dict[str, Any] | None:
        """Pull the next complete line off the socket.

        Bytes already sitting in Qt's read buffer are drained **before** any
        wait. Without that, a caller that pumped the event loop itself — which
        is how the server and the client run in one process under test — would
        block for a full timeout on data it already has.

        Returns:
            The decoded frame, or `None` on timeout or a decode failure.
        """
        if self._sock is None:
            return None
        while True:
            self._buffer += bytes(self._sock.readAll().data())
            if LINE_SEP in self._buffer:
                break
            if not self._sock.waitForReadyRead(self.timeout_ms):
                return None
        raw, _, rest = bytes(self._buffer).partition(LINE_SEP)
        self._buffer = bytearray(rest)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    # ── the four operations ──────────────────────────────────────────────────

    def hello(self) -> dict[str, Any] | None:
        """Announce ourselves and subscribe to invalidations.

        Returns:
            The `hello` response, or `None`.
        """
        return self.request({"op": OP_HELLO, "v": PROTOCOL_VERSION})

    def state(self, targets: Sequence[str]) -> dict[str, str]:
        """Ask for the state of some paths.

        Args:
            targets: Absolute paths.

        Returns:
            `{path: FileState value}`. Everything is `unknown` when the server
            did not answer — which is what the extension renders as no emblem.
        """
        reply = self.request({"op": OP_STATE, "v": PROTOCOL_VERSION,
                              "paths": list(targets)})
        if not reply or reply.get("op") != OP_STATE:
            return {path: UNKNOWN for path in targets}
        states = reply.get("states")
        if not isinstance(states, dict):
            return {path: UNKNOWN for path in targets}
        return {path: str(states.get(path, UNKNOWN)) for path in targets}

    def menu(self, targets: Sequence[str]) -> list[str]:
        """Ask which context-menu verbs apply.

        Args:
            targets: The selected absolute paths.

        Returns:
            The action ids.
        """
        reply = self.request({"op": OP_MENU, "v": PROTOCOL_VERSION,
                              "paths": list(targets)})
        if not reply or reply.get("op") != OP_MENU:
            return []
        actions = reply.get("actions")
        return [str(a) for a in actions] if isinstance(actions, list) else []

    def do(self, action: str, targets: Sequence[str]) -> bool:
        """Invoke a context-menu verb.

        Args:
            action: The verb id.
            targets: The selected absolute paths.

        Returns:
            True if the server accepted it.
        """
        reply = self.request({"op": OP_DO, "v": PROTOCOL_VERSION,
                              "action": action, "paths": list(targets)})
        return bool(reply) and reply.get("op") == OP_OK


__all__ = [
    "IpcServer", "IpcClient", "PROTOCOL", "PROTOCOL_VERSION", "BUDGET_MS",
    "CLIENT_TIMEOUT_MS", "UNKNOWN", "LINE_SEP", "MAX_LINE_BYTES",
    "MAX_BAD_REQUESTS", "REQUEST_OPS", "SOCKET_OPTIONS",
    "OP_HELLO", "OP_STATE", "OP_MENU", "OP_DO", "OP_OK", "OP_ERROR",
    "OP_INVALIDATE", "StateProvider", "MenuProvider",
]
