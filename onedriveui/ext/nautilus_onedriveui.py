"""The Nautilus extension: emblems, a Status column, and a context submenu.

**This module may import the standard library and ``gi``. Nothing else.**

nautilus-python's loader ``dlopen``s the *system* ``libpython3.14.so.1.0`` and
runs this file inside Nautilus's own process. It does not see a virtualenv, it
does not see ``PYTHONPATH`` as set for our application, and it does not see the
``onedriveui`` package. An import of anything from it fails at load with **no
useful error at all** — Nautilus logs nothing, the extension simply never
appears, and every emblem silently stops working. A dedicated AST test enforces
this, because the failure mode gives you nothing to debug from.

Which means everything shared with the application — the wire protocol, the
emblem names, the action ids — is duplicated here as literals, and the tests
assert those literals still equal the ones in the package. Duplication with an
enforced equality is the only arrangement that survives the constraint.

Three more things about Nautilus that shape the code:

**``update_file_info`` is synchronous.** The asynchronous protocol needs a
``Nautilus.OperationHandle``, which **cannot be constructed from Python**, so
returning ``IN_PROGRESS`` and completing later is not available. This runs on
Nautilus's UI thread, for every visible file, and it must answer immediately —
so it answers from an in-memory dict and says ``unknown`` when it does not know
yet, rather than blocking to find out.

**The module is imported twice per launch cycle.** Nautilus loads extension
modules once to enumerate them and again to instantiate them, so anything with a
side effect — opening the socket, starting a thread — is guarded, or the second
import fights the first for the same file descriptor.

**``get_background_items`` is called with an empty list.** Not with the folder,
not with ``None``: an empty list, on some versions with the folder as a separate
argument. Indexing ``files[0]`` there is the single most common way a Nautilus
Python extension crashes the file manager's menu.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any

import gi

gi.require_version("Nautilus", "4.0")
from gi.repository import GObject, Nautilus  # noqa: E402

# ═════════════════════════════════════════════════════════════════════════════
# Duplicated constants
#
# These MUST equal their counterparts in the `onedriveui` package, and cannot
# import them (see the module docstring). `tests/test_nautilus_ext.py` asserts
# the equality, so a change on either side fails the suite rather than silently
# breaking emblems in a file manager nobody is testing.
# ═════════════════════════════════════════════════════════════════════════════

#: `paths.ipc_socket()`.
SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
    "onedriveui", "ipc.sock")

#: `platform.ipc.PROTOCOL_VERSION`.
PROTOCOL_VERSION = 1

#: `constants.NAUTILUS_IPC_TIMEOUT_MS`, in seconds. Nautilus's UI thread is
#: blocked for the duration, so this is a ceiling on how long the file manager
#: can be made to stutter — not a target.
TIMEOUT_S = 0.2

#: `models.FileState` -> the bare emblem stem, matching `icons.EMBLEM_FOR_STATE`.
#: Nautilus resolves `add_emblem("NAME")` as emblem-NAME -> NAME ->
#: emblem-NAME-symbolic -> NAME-symbolic, so the BARE stem is what goes in.
EMBLEM_FOR_STATE = {
    "online_only": "onedriveui-cloud",
    "local": "onedriveui-local",
    "pinned": "onedriveui-pinned",
    "partial": "onedriveui-syncing",
    "dirty": "onedriveui-syncing",
    "syncing": "onedriveui-syncing",
    "excluded": "onedriveui-excluded",
    "error": "onedriveui-error",
    "unknown": "",
}

#: `strings.FILE_STATE_LABEL` — the Status column's text.
LABEL_FOR_STATE = {
    "online_only": "Available when online",
    "local": "Available on this device",
    "pinned": "Always available on this device",
    "partial": "Downloading…",
    "dirty": "Uploading…",
    "syncing": "Syncing…",
    "excluded": "Not syncing",
    "error": "Sync problem",
    "unknown": "",
}

#: The context submenu, as `(action id, label)`. The ids are
#: `models.RecoveryAction` values, so the server dispatches them through
#: `Supervisor.do()` exactly like the tray menu.
MENU_ITEMS = (
    ("pin", "Always keep on this device"),
    ("free_up_space", "Free up space"),
    ("open_web", "View online"),
    ("stop_syncing_item", "Stop syncing this item"),
)

COLUMN_NAME = "OneDriveUI::status"
COLUMN_ATTRIBUTE = "onedriveui_status"
COLUMN_LABEL = "Status"

#: Guards the double import. Module-level state is per-interpreter, and Nautilus
#: imports this file twice per launch cycle — once to enumerate the providers and
#: once to instantiate them. Without this the second import races the first for
#: the same socket.
_CLIENT_LOCK = threading.Lock()
_CLIENT: "IpcClient | None" = None


class IpcClient:
    """A short-lived newline-delimited-JSON client for the engine's socket.

    One connection per request rather than a persistent one, deliberately. A
    persistent socket inside Nautilus would need a reconnect strategy, a
    keepalive and a thread, and would hold a file descriptor open for the life
    of a file manager that may run for weeks. A connect costs microseconds on a
    unix socket, and if the engine is not running the connect fails immediately
    — which is exactly the answer we want.
    """

    def __init__(self, path: str = SOCKET_PATH, timeout_s: float = TIMEOUT_S):
        self.path = path
        self.timeout_s = timeout_s

    def request(self, payload: dict) -> dict | None:
        """Send one frame and read one reply.

        Args:
            payload: The request.

        Returns:
            The reply, or ``None`` when the engine is not running, is too slow,
            or answered something unparseable. **Never raises**: this is called
            on Nautilus's UI thread, where an exception is a traceback in the
            file manager's log and a file with no emblem.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout_s)
                sock.connect(self.path)
                sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
                return self._read_reply(sock)
        except (OSError, ValueError):
            return None

    def _read_reply(self, sock: socket.socket) -> dict | None:
        """Read frames until one is not an unsolicited push.

        The server pushes ``invalidate`` frames whenever badges go stale, and one
        can arrive between our request and its reply. Treating it as the answer
        would make every status lookup return nothing.
        """
        buffer = b""
        while len(buffer) < 4 * 1024 * 1024:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return None
            if not chunk:
                return None
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    frame = json.loads(line.decode("utf-8"))
                except ValueError:
                    return None
                if isinstance(frame, dict) and frame.get("op") != "invalidate":
                    return frame
        return None

    def states(self, paths: list[str]) -> dict[str, str]:
        """``{absolute path: state}``. Missing paths answer ``"unknown"``."""
        reply = self.request({"op": "state", "v": PROTOCOL_VERSION,
                              "paths": paths})
        if not reply or reply.get("op") != "state":
            return {}
        states = reply.get("states")
        return states if isinstance(states, dict) else {}

    def do(self, action: str, paths: list[str]) -> bool:
        """Ask the engine to perform an action. Fire and forget."""
        reply = self.request({"op": "do", "v": PROTOCOL_VERSION,
                              "action": action, "paths": paths})
        return bool(reply and reply.get("op") == "ok")


def _client() -> IpcClient:
    """The shared client, created once even across the double import."""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _CLIENT = IpcClient()
        return _CLIENT


def _path_of(file: Any) -> str:
    """A file's local path, or ``""`` for anything not on this machine."""
    try:
        if file.get_uri_scheme() != "file":
            return ""
        return file.get_location().get_path() or ""
    except Exception:  # noqa: BLE001 - Nautilus must never see a traceback
        return ""


class OneDriveUIProvider(GObject.GObject,
                         Nautilus.InfoProvider,
                         Nautilus.ColumnProvider,
                         Nautilus.MenuProvider):
    """Emblems, the Status column and the context submenu.

    One class implementing three interfaces, because Nautilus instantiates each
    provider separately and three objects would mean three IPC clients and three
    caches of the same data.
    """

    def __init__(self):
        GObject.GObject.__init__(self)
        #: Absolute path -> state, filled by `update_file_info` and read by it.
        #: The only reason a synchronous hook can be fast enough.
        self._cache: dict[str, str] = {}

    # ═════════════════════════════════════════════════════════════════════════
    # InfoProvider
    # ═════════════════════════════════════════════════════════════════════════

    def update_file_info(self, file: Any) -> None:
        """Attach an emblem and the Status value. **Synchronous, always.**

        Called on Nautilus's UI thread for every visible file. The asynchronous
        protocol needs a ``Nautilus.OperationHandle``, which cannot be
        constructed from Python, so ``IN_PROGRESS`` is not an option and this
        must answer now.

        It therefore answers from an in-memory dict, and asks the engine only
        for paths it has never seen — with a 200 ms ceiling, after which the
        answer is ``unknown``. A file with no emblem for a moment is a cosmetic
        problem; a file manager that stops scrolling is not.
        """
        path = _path_of(file)
        if not path:
            return

        state = self._cache.get(path)
        if state is None:
            state = self._fetch(path)

        emblem = EMBLEM_FOR_STATE.get(state, "")
        if emblem:
            # The BARE stem: Nautilus builds a GThemedIcon trying
            # emblem-NAME -> NAME -> emblem-NAME-symbolic -> NAME-symbolic.
            file.add_emblem(emblem)
        file.add_string_attribute(COLUMN_ATTRIBUTE,
                                  LABEL_FOR_STATE.get(state, ""))

    def _fetch(self, path: str) -> str:
        """One round trip, cached. ``"unknown"`` when the engine is not there."""
        states = _client().states([path])
        for key, value in states.items():
            self._cache[key] = value
        return self._cache.get(path, "unknown")

    def invalidate(self, paths: list[str]) -> None:
        """Drop cached states, so the next redraw asks again.

        Nautilus keeps what it was last told until an extension calls
        ``invalidate_extension_info()``, so a file that finished uploading keeps
        its syncing emblem forever without this.
        """
        for path in paths:
            self._cache.pop(path, None)

    # ═════════════════════════════════════════════════════════════════════════
    # ColumnProvider
    # ═════════════════════════════════════════════════════════════════════════

    def get_columns(self) -> tuple:
        """The Status column, matching Explorer's."""
        return (Nautilus.Column(
            name=COLUMN_NAME,
            attribute=COLUMN_ATTRIBUTE,
            label=COLUMN_LABEL,
            description="Whether this item is available on this device"),)

    # ═════════════════════════════════════════════════════════════════════════
    # MenuProvider
    # ═════════════════════════════════════════════════════════════════════════

    def get_file_items(self, *args: Any) -> tuple:
        """The context submenu for a selection.

        The signature is deliberately ``*args``: Nautilus 4 passes ``(files)``
        and Nautilus 3 passed ``(window, files)``. Declaring the wrong arity
        makes the menu silently not appear, which is indistinguishable from the
        extension not being installed.
        """
        files = args[-1] if args else []
        paths = [p for p in (_path_of(f) for f in files or []) if p]
        if not paths:
            return ()
        return (self._submenu(paths),)

    def get_background_items(self, *args: Any) -> tuple:
        """The menu for the folder background.

        **Called with an empty list**, not with the folder and not with
        ``None``. Indexing ``files[0]`` here is the single most common way a
        Nautilus Python extension takes the file manager's menu down with it.
        """
        candidates = args[-1] if args else None
        if isinstance(candidates, (list, tuple)):
            # An empty list is the documented, normal case: there is no
            # selection to act on, so there is nothing to offer.
            return ()
        path = _path_of(candidates) if candidates is not None else ""
        if not path:
            return ()
        return (self._submenu([path]),)

    def _submenu(self, paths: list[str]) -> Any:
        top = Nautilus.MenuItem(name="OneDriveUI::menu", label="OneDrive",
                                tip="OneDrive actions")
        submenu = Nautilus.Menu()
        top.set_submenu(submenu)
        for action, label in MENU_ITEMS:
            item = Nautilus.MenuItem(name=f"OneDriveUI::{action}", label=label)
            item.connect("activate", self._on_activate, action, list(paths))
            submenu.append_item(item)
        return top

    def _on_activate(self, _item: Any, action: str, paths: list[str]) -> None:
        """Hand the action to the engine, which runs it through ``do()``.

        Nothing is performed here. The extension has no guards, no database and
        no knowledge of invariants; it is a menu, and the one place that decides
        whether an action is safe is the Supervisor.
        """
        _client().do(action, paths)
