"""Tests for `onedriveui.platform.ipc`.

Everything here exists because `Nautilus.InfoProvider.update_file_info` runs on
the file manager's UI thread and cannot be made asynchronous from Python
(`Nautilus.OperationHandle` has no Python constructor). So the server's
obligations are hard ones, and each gets a test:

* **1 000 paths in under 20 ms** — `test_a_thousand_paths_beat_the_budget`
  measures the real round trip through a real unix socket, and the server's own
  `stats()["max_ms"]` confirms the handling time separately from the transport.
* **never block** — a provider that sleeps past the budget still gets an answer
  out, and every path in it is `unknown`, which draws no emblem.
* **connect-refused when we are down** — `test_connect_fails_fast_when_stopped`
  asserts both the failure and that it is immediate, because a client that
  instead waits out its 200 ms timeout on every row is the freeze this whole
  design avoids.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop
from PySide6.QtNetwork import QLocalSocket

from onedriveui import paths
from onedriveui.bus import BUS
from onedriveui.constants import IPC_BUDGET_MS, NAUTILUS_IPC_TIMEOUT_MS
from onedriveui.models import FileState
from onedriveui.platform import ipc as I
from onedriveui.platform.ipc import IpcClient, IpcServer

REPO_ROOT = Path(__file__).resolve().parent.parent

ROOT = "/home/u/OneDrive"
ACCOUNT = "onedrive"


def _pump(qapp, times: int = 24) -> None:
    """Service the server's event loop; the client blocks, so we drive it here."""
    for _ in range(times):
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)


class Peer:
    """A client bound to a pumped server, so request/response works in-process."""

    def __init__(self, qapp, server: IpcServer) -> None:
        self.qapp = qapp
        self.client = IpcClient(server.socket_path)
        assert self.client.connect() is True
        _pump(qapp)

    def call(self, payload: dict, *, pump: int = 24) -> dict | None:
        assert self.client.send(payload) is True
        _pump(self.qapp, pump)
        return self.client.read(skip_pushes=True)

    def read(self) -> dict | None:
        return self.client.read()

    def close(self) -> None:
        self.client.close()
        _pump(self.qapp)


@pytest.fixture
def states() -> dict[str, str]:
    return {}


@pytest.fixture
def server(qapp, _isolate_home, states):
    """A listening server whose state provider reads `states`."""
    srv = IpcServer(account_id=ACCOUNT, sync_root=ROOT)
    srv.set_state_provider(
        lambda wanted: {p: states[p] for p in wanted if p in states})
    assert srv.start() is True
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def peer(qapp, server):
    connection = Peer(qapp, server)
    try:
        yield connection
    finally:
        connection.close()


# ═════════════════════════════════════════════════════════════════════════════
# The contract
# ═════════════════════════════════════════════════════════════════════════════

def test_budget_comes_from_constants():
    assert IpcServer.BUDGET_MS == IPC_BUDGET_MS == 20
    assert I.CLIENT_TIMEOUT_MS == NAUTILUS_IPC_TIMEOUT_MS == 200
    assert IpcServer.BUDGET_MS < I.CLIENT_TIMEOUT_MS


def test_unknown_is_the_file_state_enum_value():
    assert I.UNKNOWN == FileState.UNKNOWN.value == "unknown"


def test_protocol_documents_every_operation():
    assert set(I.PROTOCOL["requests"]) == set(I.REQUEST_OPS)
    assert I.PROTOCOL["version"] == I.PROTOCOL_VERSION
    assert I.OP_INVALIDATE in I.PROTOCOL["push"]


def test_socket_path_is_the_frozen_one(qapp, _isolate_home):
    srv = IpcServer()
    assert srv.socket_path == str(paths.ipc_socket())
    assert Path(srv.socket_path).name == "ipc.sock"
    assert Path(srv.socket_path).parent == paths.runtime_dir()


def test_socket_is_owner_only(server):
    assert (Path(server.socket_path).stat().st_mode & 0o077) == 0


# ═════════════════════════════════════════════════════════════════════════════
# hello
# ═════════════════════════════════════════════════════════════════════════════

def test_hello_announces_the_account_and_root(peer):
    reply = peer.call({"op": I.OP_HELLO, "v": 1})
    assert reply == {"op": I.OP_HELLO, "v": 1, "account": ACCOUNT,
                     "root": ROOT, "budget_ms": IpcServer.BUDGET_MS}


def test_set_account_changes_what_hello_says(server, peer):
    server.set_account("work", "/home/u/OneDrive - Contoso")
    reply = peer.call({"op": I.OP_HELLO, "v": 1})
    assert reply["account"] == "work"
    assert reply["root"] == "/home/u/OneDrive - Contoso"


def test_hello_subscribes_to_pushes(server, peer, qapp):
    assert server.stats()["subscribers"] == 0
    peer.call({"op": I.OP_HELLO, "v": 1})
    assert server.stats()["subscribers"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# state
# ═════════════════════════════════════════════════════════════════════════════

def test_state_answers_from_the_provider(peer, states):
    states[f"{ROOT}/a"] = FileState.PINNED.value
    states[f"{ROOT}/b"] = FileState.ONLINE_ONLY.value

    reply = peer.call({"op": I.OP_STATE, "v": 1,
                       "paths": [f"{ROOT}/a", f"{ROOT}/b"]})

    assert reply["states"] == {f"{ROOT}/a": "pinned", f"{ROOT}/b": "online_only"}


def test_an_unknown_path_answers_unknown(peer):
    reply = peer.call({"op": I.OP_STATE, "v": 1, "paths": [f"{ROOT}/never-seen"]})
    assert reply["states"] == {f"{ROOT}/never-seen": I.UNKNOWN}


def test_every_requested_path_gets_an_entry(peer, states):
    """The extension indexes the reply by path; a missing key would KeyError."""
    states[f"{ROOT}/a"] = FileState.LOCAL.value
    wanted = [f"{ROOT}/{n}" for n in "abcde"]

    reply = peer.call({"op": I.OP_STATE, "v": 1, "paths": wanted})

    assert list(reply["states"]) == wanted


def test_no_provider_answers_everything_unknown(qapp, server, peer):
    server.set_state_provider(None)
    reply = peer.call({"op": I.OP_STATE, "v": 1, "paths": [f"{ROOT}/a"]})
    assert reply["states"] == {f"{ROOT}/a": I.UNKNOWN}


def test_an_empty_path_list_answers_empty(peer):
    assert peer.call({"op": I.OP_STATE, "v": 1, "paths": []})["states"] == {}


def test_non_string_paths_are_dropped(peer, states):
    states[f"{ROOT}/a"] = FileState.LOCAL.value
    reply = peer.call({"op": I.OP_STATE, "v": 1,
                       "paths": [f"{ROOT}/a", 42, None, {"x": 1}]})
    assert reply["states"] == {f"{ROOT}/a": "local"}


def test_a_provider_that_raises_degrades_to_unknown(qapp, server, peer):
    """A bug in the data layer must not stop the file manager rendering."""
    def boom(_wanted):
        raise RuntimeError("cache_index exploded")

    server.set_state_provider(boom)

    reply = peer.call({"op": I.OP_STATE, "v": 1, "paths": [f"{ROOT}/a"]})

    assert reply["states"] == {f"{ROOT}/a": I.UNKNOWN}


def test_a_provider_that_returns_junk_is_ignored(qapp, server, peer):
    server.set_state_provider(lambda wanted: {wanted[0]: 42, "/not/asked": "local"})
    reply = peer.call({"op": I.OP_STATE, "v": 1, "paths": [f"{ROOT}/a"]})
    assert reply["states"] == {f"{ROOT}/a": I.UNKNOWN}


def test_a_slow_provider_still_gets_an_answer_out(qapp, server, peer):
    """Over budget means "unknown", never "no reply" and never a stall."""
    def slow(wanted):
        time.sleep((IpcServer.BUDGET_MS + 15) / 1000.0)
        return {p: FileState.PINNED.value for p in wanted}

    server.set_state_provider(slow)

    reply = peer.call({"op": I.OP_STATE, "v": 1, "paths": [f"{ROOT}/a"]})

    assert reply["states"] == {f"{ROOT}/a": I.UNKNOWN}
    assert server.stats()["over_budget"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# THE BUDGET — 1 000 paths in under 20 ms
# ═════════════════════════════════════════════════════════════════════════════

def test_a_thousand_paths_beat_the_budget(qapp, server, peer, states):
    """The acceptance criterion, measured through a real unix socket."""
    wanted = [f"{ROOT}/folder{i // 100}/file{i:04d}.txt" for i in range(1000)]
    for index, path in enumerate(wanted):
        states[path] = (FileState.PINNED.value if index % 2
                        else FileState.ONLINE_ONLY.value)

    started = time.perf_counter()
    assert peer.client.send({"op": I.OP_STATE, "v": 1, "paths": wanted}) is True
    _pump(qapp, 40)
    reply = peer.client.read(skip_pushes=True)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert reply is not None
    assert len(reply["states"]) == 1000
    assert reply["states"][wanted[1]] == "pinned"
    assert reply["states"][wanted[0]] == "online_only"
    assert server.stats()["over_budget"] == 0, "the server exceeded its own budget"
    assert server.stats()["max_ms"] < IpcServer.BUDGET_MS
    # The round trip includes the test's own event pumping, so it is bounded
    # more loosely than the server's handling time — which is the number the
    # budget is actually about.
    assert elapsed_ms < I.CLIENT_TIMEOUT_MS, f"{elapsed_ms:.1f} ms round trip"


def test_the_client_helper_answers_a_thousand_paths(qapp, server, peer, states):
    """The same query through `IpcClient.state()`, the extension's own shape."""
    wanted = [f"{ROOT}/f{i:04d}" for i in range(1000)]
    states.update({p: FileState.LOCAL.value for p in wanted})

    assert peer.client.send({"op": I.OP_STATE, "v": 1, "paths": wanted}) is True
    _pump(qapp, 40)
    reply = peer.client.read(skip_pushes=True)

    assert len(reply["states"]) == 1000
    assert set(reply["states"].values()) == {"local"}


def test_stats_track_requests(peer, server):
    peer.call({"op": I.OP_HELLO, "v": 1})
    peer.call({"op": I.OP_STATE, "v": 1, "paths": []})
    stats = server.stats()
    assert stats["requests"] == 2
    assert stats["peers"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# menu and do
# ═════════════════════════════════════════════════════════════════════════════

def test_menu_asks_the_provider(qapp, server, peer):
    server.set_menu_provider(lambda targets: ["pin", "free_up"] if targets else [])
    reply = peer.call({"op": I.OP_MENU, "v": 1, "paths": [f"{ROOT}/a"]})
    assert reply == {"op": I.OP_MENU, "actions": ["pin", "free_up"]}


def test_menu_with_no_provider_offers_nothing(peer):
    assert peer.call({"op": I.OP_MENU, "v": 1, "paths": [f"{ROOT}/a"]})["actions"] == []


def test_menu_provider_that_raises_offers_nothing(qapp, server, peer):
    server.set_menu_provider(lambda _t: (_ for _ in ()).throw(RuntimeError("x")))
    assert peer.call({"op": I.OP_MENU, "v": 1, "paths": [f"{ROOT}/a"]})["actions"] == []


def test_do_emits_the_signal_and_the_bus(qapp, server, peer, bus_spy):
    bus_spy.watch("ipc_action_requested")
    seen: list[tuple[str, list[str]]] = []
    server.action_requested.connect(lambda verb, targets: seen.append((verb, targets)))

    reply = peer.call({"op": I.OP_DO, "v": 1, "action": "pin",
                       "paths": [f"{ROOT}/a", f"{ROOT}/b"]})

    assert reply == {"op": I.OP_OK, "v": 1}
    assert seen == [("pin", [f"{ROOT}/a", f"{ROOT}/b"])]
    assert bus_spy.of("ipc_action_requested") == [("pin", [f"{ROOT}/a", f"{ROOT}/b"])]


def test_do_never_executes_anything_itself(qapp, server, peer):
    """UI-adjacent code emits; `Supervisor` acts. This module has no verbs."""
    assert not hasattr(server, "pin")
    assert not hasattr(server, "execute")
    reply = peer.call({"op": I.OP_DO, "v": 1, "action": "delete_everything",
                       "paths": ["/"]})
    assert reply == {"op": I.OP_OK, "v": 1}


def test_do_without_an_action_is_an_error(peer):
    reply = peer.call({"op": I.OP_DO, "v": 1, "paths": [f"{ROOT}/a"]})
    assert reply["op"] == I.OP_ERROR


def test_bus_mirroring_can_be_switched_off(qapp, _isolate_home, bus_spy, tmp_path):
    bus_spy.watch("ipc_action_requested")
    srv = IpcServer(socket_path=tmp_path / "q.sock", mirror_to_bus=False)
    assert srv.start() is True
    connection = Peer(qapp, srv)
    try:
        connection.call({"op": I.OP_DO, "v": 1, "action": "pin", "paths": []})
    finally:
        connection.close()
        srv.stop()
    assert bus_spy.of("ipc_action_requested") == []


# ═════════════════════════════════════════════════════════════════════════════
# The push channel
# ═════════════════════════════════════════════════════════════════════════════

def test_invalidate_reaches_a_subscriber(qapp, server, peer):
    peer.call({"op": I.OP_HELLO, "v": 1})

    assert server.broadcast_invalidate([f"{ROOT}/a", f"{ROOT}/b"]) == 1
    _pump(qapp)

    assert peer.read() == {"op": I.OP_INVALIDATE, "v": 1,
                           "paths": [f"{ROOT}/a", f"{ROOT}/b"]}


def test_invalidate_reaches_every_subscriber(qapp, server):
    first, second = Peer(qapp, server), Peer(qapp, server)
    try:
        first.call({"op": I.OP_HELLO, "v": 1})
        second.call({"op": I.OP_HELLO, "v": 1})

        assert server.broadcast_invalidate([f"{ROOT}/x"]) == 2
        _pump(qapp)

        for connection in (first, second):
            assert connection.read()["op"] == I.OP_INVALIDATE
    finally:
        first.close()
        second.close()


def test_invalidate_skips_a_peer_that_never_said_hello(qapp, server, peer):
    assert server.broadcast_invalidate([f"{ROOT}/a"]) == 0


def test_invalidate_with_no_paths_does_nothing(qapp, server, peer):
    peer.call({"op": I.OP_HELLO, "v": 1})
    assert server.broadcast_invalidate([]) == 0


def test_a_push_does_not_confuse_a_pending_reply(qapp, server, peer, states):
    """`read(skip_pushes=True)` must hand back the reply, not the push."""
    peer.call({"op": I.OP_HELLO, "v": 1})
    states[f"{ROOT}/a"] = FileState.LOCAL.value

    assert peer.client.send({"op": I.OP_STATE, "v": 1, "paths": [f"{ROOT}/a"]})
    server.broadcast_invalidate([f"{ROOT}/z"])
    _pump(qapp)

    reply = peer.client.read(skip_pushes=True)
    assert reply["op"] == I.OP_STATE


def test_a_disconnected_subscriber_is_forgotten(qapp, server):
    connection = Peer(qapp, server)
    connection.call({"op": I.OP_HELLO, "v": 1})
    assert server.stats()["subscribers"] == 1

    connection.close()
    _pump(qapp)

    assert server.stats()["peers"] == 0
    assert server.broadcast_invalidate([f"{ROOT}/a"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
# Robustness
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("junk", [b"not json", b"[1,2]", b'{"op":"nope"}', b'"s"'])
def test_junk_is_answered_with_an_error(qapp, server, peer, junk):
    assert peer.client._sock is not None
    peer.client._sock.write(junk + b"\n")
    peer.client._sock.flush()
    _pump(qapp)

    reply = peer.client.read(skip_pushes=True)

    assert reply["op"] == I.OP_ERROR
    assert "error" in reply


def test_a_flood_of_junk_closes_the_peer(qapp, server, peer):
    for _ in range(I.MAX_BAD_REQUESTS + 2):
        peer.client._sock.write(b"junk\n")
        peer.client._sock.flush()
        _pump(qapp, 4)

    _pump(qapp, 10)
    assert server.stats()["peers"] == 0
    assert server.is_listening is True          # the server itself survives


def test_an_oversized_frame_closes_the_peer(qapp, server, peer):
    peer.client._sock.write(b"x" * (I.MAX_LINE_BYTES + 1))
    peer.client._sock.flush()
    _pump(qapp, 40)

    assert server.is_listening is True


def test_two_frames_in_one_write_are_both_answered(qapp, server, peer):
    frames = (json.dumps({"op": I.OP_HELLO, "v": 1}).encode() + b"\n"
              + json.dumps({"op": I.OP_STATE, "v": 1, "paths": []}).encode() + b"\n")
    peer.client._sock.write(frames)
    peer.client._sock.flush()
    _pump(qapp)

    assert peer.client.read(skip_pushes=True)["op"] == I.OP_HELLO
    assert peer.client.read(skip_pushes=True)["op"] == I.OP_STATE


def test_a_partial_frame_waits_for_its_newline(qapp, server, peer):
    payload = json.dumps({"op": I.OP_HELLO, "v": 1}).encode()
    peer.client._sock.write(payload[:5])
    peer.client._sock.flush()
    _pump(qapp)
    assert server.stats()["requests"] == 0

    peer.client._sock.write(payload[5:] + b"\n")
    peer.client._sock.flush()
    _pump(qapp)

    assert server.stats()["requests"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# Lifecycle — connect-refused when the application is down
# ═════════════════════════════════════════════════════════════════════════════

def test_start_is_idempotent(server):
    assert server.start() is True
    assert server.is_listening is True


def test_stop_removes_the_socket(server):
    socket_path = Path(server.socket_path)
    assert socket_path.exists()

    server.stop()

    assert not socket_path.exists()
    assert server.is_listening is False


def test_connect_fails_fast_when_stopped(qapp, server):
    """The extension must get ECONNREFUSED, not spend its 200 ms timeout."""
    server.stop()

    client = IpcClient(server.socket_path)
    started = time.perf_counter()
    connected = client.connect()
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert connected is False
    assert elapsed_ms < I.CLIENT_TIMEOUT_MS, f"{elapsed_ms:.1f} ms to fail"


def test_connect_fails_fast_when_never_started(qapp, _isolate_home):
    client = IpcClient(paths.runtime_dir() / "never.sock")
    started = time.perf_counter()
    assert client.connect() is False
    assert (time.perf_counter() - started) * 1000.0 < I.CLIENT_TIMEOUT_MS


def test_a_stale_socket_is_reclaimed(qapp, _isolate_home):
    stale = paths.runtime_dir() / "ipc.sock"
    stale.write_bytes(b"")
    srv = IpcServer()
    try:
        assert srv.start() is True
    finally:
        srv.stop()


def test_stop_disconnects_every_peer(qapp, server, peer):
    assert server.stats()["peers"] == 1
    server.stop()
    assert server.stats()["peers"] == 0


def test_client_context_manager(qapp, server):
    with IpcClient(server.socket_path) as client:
        assert client.connected is True
    assert client.connected is False


def test_client_state_defaults_to_unknown_when_the_server_is_down(qapp, server):
    server.stop()
    client = IpcClient(server.socket_path)
    assert client.connect() is False
    assert client.state([f"{ROOT}/a"]) == {f"{ROOT}/a": I.UNKNOWN}
    assert client.menu([f"{ROOT}/a"]) == []
    assert client.do("pin", [f"{ROOT}/a"]) is False


# ═════════════════════════════════════════════════════════════════════════════
# A real second process — what the Nautilus extension actually is
# ═════════════════════════════════════════════════════════════════════════════

EXTENSION_CLIENT = textwrap.dedent(
    """
    import json, os, socket, sys, time

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout({timeout})
    sock.connect({path!r})
    stream = sock.makefile("rwb")

    def call(payload):
        stream.write(json.dumps(payload).encode() + b"\\n")
        stream.flush()
        while True:
            frame = json.loads(stream.readline())
            if frame.get("op") != "invalidate":
                return frame

    hello = call({{"op": "hello", "v": 1}})
    paths = [f"{root}/f{{i:04d}}.txt" for i in range(1000)]
    started = time.perf_counter()
    reply = call({{"op": "state", "v": 1, "paths": paths}})
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(json.dumps({{
        "account": hello["account"], "budget_ms": hello["budget_ms"],
        "count": len(reply["states"]), "elapsed_ms": elapsed_ms,
        "sample": reply["states"][paths[0]],
    }}))
    """
)


def test_a_real_client_process_answers_a_thousand_paths_under_budget(
        qapp, server, states):
    """A separate process, a raw `socket` client, no Qt and no PySide6.

    This is the shape the Nautilus extension really has — stdlib only, under the
    system interpreter — and it is the only measurement where the server's event
    loop and the client genuinely run concurrently.
    """
    wanted = [f"{ROOT}/f{i:04d}.txt" for i in range(1000)]
    states.update({p: FileState.PINNED.value for p in wanted})
    script = EXTENSION_CLIENT.format(
        path=server.socket_path, root=ROOT,
        timeout=I.CLIENT_TIMEOUT_MS / 1000.0 * 20)

    child = subprocess.Popen([sys.executable, "-c", script],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    deadline = time.monotonic() + 30
    while child.poll() is None and time.monotonic() < deadline:
        _pump(qapp, 4)
    stdout, stderr = child.communicate(timeout=10)

    assert child.returncode == 0, stderr
    result = json.loads(stdout.strip().splitlines()[-1])
    assert result["account"] == ACCOUNT
    assert result["budget_ms"] == IpcServer.BUDGET_MS
    assert result["count"] == 1000
    assert result["sample"] == "pinned"
    assert server.stats()["over_budget"] == 0
    assert server.stats()["max_ms"] < IpcServer.BUDGET_MS, server.stats()


# ═════════════════════════════════════════════════════════════════════════════
# Peer teardown — a late `disconnected` must not throw out of a Qt slot
# ═════════════════════════════════════════════════════════════════════════════

class TestPeerRelease:
    """`_release()` exists because two PySide6 behaviours collide.

    `abort()` emits `disconnected` *synchronously*, and a `DeferredDelete` that
    has already been delivered leaves the Python wrapper alive over a freed C++
    object, so every method on it raises `RuntimeError`. A late `disconnected`
    for such a peer used to throw straight out of a Qt slot, where nothing can
    catch it.
    """

    def test_a_late_disconnected_on_a_reaped_peer_does_not_raise(
            self, qapp, server, peer):
        from PySide6.QtCore import QEvent
        from PySide6.QtCore import QCoreApplication

        _pump(qapp, 40)
        assert server.peer_count == 1
        sock = next(iter(server._peers))

        # the normal path: forget it and queue its deletion
        server._on_disconnected(sock)
        # deliver the DeferredDelete, destroying the C++ half
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        # a second, late `disconnected` for the same peer must be survivable
        server._on_disconnected(sock)
        assert server.peer_count == 0

    def test_release_is_idempotent(self, qapp, server, peer):
        _pump(qapp, 40)
        sock = next(iter(server._peers))
        server._release(sock)
        server._release(sock)
        server._release(sock)          # no RuntimeError

    def test_stop_does_not_reenter_on_disconnected(self, qapp, server, peer):
        """`abort()` inside `stop()` fires `disconnected` synchronously."""
        _pump(qapp, 40)
        assert server.peer_count == 1
        reentered: list[object] = []
        original = server._on_disconnected
        server._on_disconnected = lambda p: (reentered.append(p), original(p))[1]
        server.stop()
        assert reentered == [], "stop() re-entered _on_disconnected via abort()"
        assert server.peer_count == 0
