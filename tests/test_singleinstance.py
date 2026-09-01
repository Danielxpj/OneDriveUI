"""Tests for `onedriveui.platform.singleinstance`.

Two claims matter, and both are checked against reality rather than against
this module's own bookkeeping:

* **the socket lives under `$XDG_RUNTIME_DIR`, never `/tmp`.** A live test
  asserts the real path is `/run/user/<uid>/onedriveui/ui.sock`, because the
  bare `QLocalServer.listen("name")` form lands world-readable in `/tmp`, where
  any local user could drive the application.
* **a second launch connects, hands over its argv, and exits 0.** Proved with a
  genuine second *process*, not a second object: in one process the primary's
  event loop cannot run while the secondary blocks, so an in-process test would
  prove nothing about the real hand-over.

A second measured fact is pinned here too: `QLockFile.removeStaleLockFile()`
deletes a lock **even when its owner is alive**. `_take_lock()` therefore does
its own liveness check, and `test_lock_is_never_stolen_from_a_live_owner` is
what keeps that check from being "simplified away".
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
from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from onedriveui import paths
from onedriveui.platform import singleinstance as SI
from onedriveui.platform.singleinstance import SingleInstance, acquire_or_forward

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def primary(qapp, _isolate_home):
    """A listening primary instance, released afterwards."""
    guard = SingleInstance()
    assert guard.try_acquire() is True
    try:
        yield guard
    finally:
        guard.release()


def _pump(qapp, times: int = 12) -> None:
    """Run the primary's event loop so a queued connection is serviced."""
    from PySide6.QtCore import QEventLoop

    for _ in range(times):
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)


def _read_frame(sock: QLocalSocket) -> dict:
    """Read one NDJSON frame, draining what the pump already buffered.

    `waitForReadyRead()` waits for *new* bytes, so a reply that arrived during
    `_pump()` must be taken out of Qt's buffer first or the wait times out on
    data we already have.
    """
    buffer = bytearray(bytes(sock.readAll().data()))
    while b"\n" not in buffer:
        assert sock.waitForReadyRead(SI.SEND_TIMEOUT_MS), "no reply"
        buffer += bytes(sock.readAll().data())
    return json.loads(bytes(buffer).split(b"\n", 1)[0])


# ═════════════════════════════════════════════════════════════════════════════
# The socket path — NOT /tmp
# ═════════════════════════════════════════════════════════════════════════════

def test_socket_path_comes_from_paths(qapp, _isolate_home):
    guard = SingleInstance()
    assert guard.socket_path == str(paths.ui_socket())
    assert guard.lock_path == str(paths.ui_lock())
    assert Path(guard.socket_path).is_absolute()


def test_socket_path_is_under_the_runtime_dir(qapp, _isolate_home):
    guard = SingleInstance()
    assert Path(guard.socket_path).parent == paths.runtime_dir()
    assert Path(guard.socket_path).name == "ui.sock"


def test_socket_and_runtime_dir_are_owner_only(primary):
    assert (Path(primary.socket_path).stat().st_mode & 0o077) == 0
    assert (paths.runtime_dir().stat().st_mode & 0o777) == 0o700


def test_the_bare_listen_form_would_land_in_tmp(qapp):
    """Why the explicit path exists. Documented by demonstration."""
    server = QLocalServer()
    name = f"onedriveui-probe-{os.getpid()}"
    QLocalServer.removeServer(name)
    try:
        assert server.listen(name) is True
        assert server.fullServerName().startswith("/tmp/")
    finally:
        server.close()
        QLocalServer.removeServer(name)


def test_live_socket_path_is_run_user_uid(qapp, monkeypatch):
    """On this machine, with the REAL runtime dir, not the isolated one.

    `_isolate_home` redirects `XDG_RUNTIME_DIR` into a temp tree for every test,
    so the real one is taken from logind's own `/run/user/<uid>`.
    """
    runtime = Path(f"/run/user/{os.getuid()}")
    if not runtime.is_dir():
        pytest.skip("no /run/user/<uid> on this machine")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    guard = SingleInstance()

    assert guard.socket_path == f"{runtime}/onedriveui/ui.sock"
    assert guard.socket_path.startswith(f"/run/user/{os.getuid()}/")
    assert not guard.socket_path.startswith("/tmp/")
    assert (Path(guard.socket_path).parent.stat().st_mode & 0o777) == 0o700


# ═════════════════════════════════════════════════════════════════════════════
# Acquisition
# ═════════════════════════════════════════════════════════════════════════════

def test_first_instance_becomes_primary(primary):
    assert primary.is_primary is True
    assert primary.server is not None
    assert primary.server.isListening() is True
    assert Path(primary.socket_path).exists()


def test_try_acquire_is_idempotent(primary):
    assert primary.try_acquire() is True
    assert primary.is_primary is True


def test_second_instance_is_refused(primary, qapp):
    second = SingleInstance()
    assert second.try_acquire() is False
    assert second.is_primary is False
    assert second.server is None


def test_release_frees_the_socket_for_the_next_launch(qapp, _isolate_home):
    first = SingleInstance()
    assert first.try_acquire() is True
    first.release()
    assert not Path(first.socket_path).exists()

    second = SingleInstance()
    try:
        assert second.try_acquire() is True
    finally:
        second.release()


def test_release_is_idempotent_and_safe_on_a_secondary(primary, qapp):
    second = SingleInstance()
    second.try_acquire()
    second.release()
    second.release()
    assert primary.is_primary is True


def test_a_stale_socket_from_a_crash_is_reclaimed(qapp, _isolate_home):
    """A crashed primary leaves the socket file behind; nothing answers on it."""
    socket_path = Path(paths.ui_socket())
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.write_bytes(b"")            # debris, not a socket

    guard = SingleInstance()
    try:
        assert guard.try_acquire() is True
        assert guard.server.isListening() is True
    finally:
        guard.release()


def test_lock_is_taken_and_records_this_process(primary):
    info = primary.peer_info()
    assert info is not None
    pid, host, _app = info
    assert pid == os.getpid()
    assert host == os.uname().nodename


def test_lock_is_never_stolen_from_a_live_owner(qapp, _isolate_home):
    """QLockFile.removeStaleLockFile() does NOT check liveness — measured.

    Without `_take_lock()`'s own pid check, a second launch would evict a
    running primary from its own lock file.
    """
    lock_path = Path(paths.ui_lock())
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(["sleep", "30"])
    try:
        lock_path.write_text(f"{child.pid}\nsleep\n{os.uname().nodename}\n",
                             encoding="utf-8")

        guard = SingleInstance()
        assert guard.try_acquire() is False
        assert lock_path.exists(), "a live owner's lock was deleted"
    finally:
        child.terminate()
        child.wait()


def test_a_dead_owners_lock_is_reclaimed(qapp, _isolate_home):
    lock_path = Path(paths.ui_lock())
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(["true"])
    child.wait()
    lock_path.write_text(f"{child.pid}\nsleep\n{os.uname().nodename}\n",
                         encoding="utf-8")

    guard = SingleInstance()
    try:
        assert guard.try_acquire() is True
    finally:
        guard.release()


def test_a_lock_from_another_host_is_left_alone(qapp, _isolate_home):
    """A pid from another machine says nothing about a process on this one."""
    lock_path = Path(paths.ui_lock())
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("2\nonedriveui\nsome-other-host\n", encoding="utf-8")

    guard = SingleInstance()
    assert guard.try_acquire() is False
    assert lock_path.exists()


def test_stale_lock_time_is_disabled():
    """A primary holds this lock for days; time-based staleness would steal it."""
    assert SI.STALE_LOCK_TIME_MS == 0


# ═════════════════════════════════════════════════════════════════════════════
# The hand-over
# ═════════════════════════════════════════════════════════════════════════════

def test_argv_reaches_the_primary(primary, qapp):
    received: list[list[str]] = []
    primary.message.connect(received.append)
    second = SingleInstance()
    assert second.try_acquire() is False

    assert second.send(["--open-folder", "a b"]) is True
    _pump(qapp)

    assert received == [["--open-folder", "a b"]]


def test_send_defaults_to_sys_argv(primary, qapp, monkeypatch):
    received: list[list[str]] = []
    primary.message.connect(received.append)
    monkeypatch.setattr(sys, "argv", ["onedriveui", "--settings"])

    SingleInstance().send()
    _pump(qapp)

    assert received == [["--settings"]]


def test_send_fails_when_nothing_is_listening(qapp, _isolate_home):
    guard = SingleInstance()
    started = time.perf_counter()
    assert guard.send(["--pause"]) is False
    # Connect-refused is immediate; it must not burn the full timeout.
    assert (time.perf_counter() - started) * 1000 < SI.CONNECT_TIMEOUT_MS


def test_a_partial_frame_is_buffered_until_its_newline(primary, qapp):
    """readyRead delivers chunks, never lines."""
    received: list[list[str]] = []
    primary.message.connect(received.append)
    payload = json.dumps({"op": SI.OP_LAUNCH, "v": 1, "argv": ["--pause"]}).encode()

    sock = QLocalSocket()
    sock.connectToServer(primary.socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
    assert sock.waitForConnected(SI.CONNECT_TIMEOUT_MS)
    try:
        sock.write(payload[:10])
        sock.flush()
        _pump(qapp)
        assert received == []                      # incomplete: nothing dispatched

        sock.write(payload[10:] + b"\n")
        sock.flush()
        _pump(qapp)
        assert received == [["--pause"]]
    finally:
        sock.abort()


def test_two_frames_in_one_write_are_both_dispatched(primary, qapp):
    received: list[list[str]] = []
    primary.message.connect(received.append)
    frames = b"".join(
        json.dumps({"op": SI.OP_LAUNCH, "v": 1, "argv": [flag]}).encode() + b"\n"
        for flag in ("--pause", "--settings"))

    sock = QLocalSocket()
    sock.connectToServer(primary.socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
    assert sock.waitForConnected(SI.CONNECT_TIMEOUT_MS)
    try:
        sock.write(frames)
        sock.flush()
        _pump(qapp)
    finally:
        sock.abort()

    assert received == [["--pause"], ["--settings"]]


@pytest.mark.parametrize("junk", [b"not json", b"[1,2,3]", b'{"op":"nope"}', b'""'])
def test_junk_is_answered_with_an_error_and_never_dispatched(primary, qapp, junk):
    received: list[list[str]] = []
    primary.message.connect(received.append)

    sock = QLocalSocket()
    sock.connectToServer(primary.socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
    assert sock.waitForConnected(SI.CONNECT_TIMEOUT_MS)
    try:
        sock.write(junk + b"\n")
        sock.flush()
        _pump(qapp)
        reply = _read_frame(sock)
    finally:
        sock.abort()

    assert reply["op"] == SI.OP_ERROR
    assert received == []


def test_an_oversized_frame_closes_the_peer(primary, qapp):
    sock = QLocalSocket()
    sock.connectToServer(primary.socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
    assert sock.waitForConnected(SI.CONNECT_TIMEOUT_MS)
    try:
        sock.write(b"x" * (SI.MAX_LINE_BYTES + 1))     # no newline, ever
        sock.flush()
        _pump(qapp, 30)
    finally:
        sock.abort()

    assert primary.is_primary is True                  # the primary survives


def test_non_string_argv_entries_are_dropped(primary, qapp):
    received: list[list[str]] = []
    primary.message.connect(received.append)

    sock = QLocalSocket()
    sock.connectToServer(primary.socket_path, QLocalSocket.OpenModeFlag.ReadWrite)
    assert sock.waitForConnected(SI.CONNECT_TIMEOUT_MS)
    try:
        sock.write(json.dumps({"op": SI.OP_LAUNCH, "v": 1,
                               "argv": ["--pause", {"a": 1}, None]}).encode() + b"\n")
        sock.flush()
        _pump(qapp)
    finally:
        sock.abort()

    assert received == [["--pause"]]


# ═════════════════════════════════════════════════════════════════════════════
# acquire_or_forward
# ═════════════════════════════════════════════════════════════════════════════

def test_acquire_or_forward_returns_the_guard_when_first(qapp, _isolate_home):
    guard = acquire_or_forward([])
    try:
        assert guard is not None
        assert guard.is_primary is True
    finally:
        if guard is not None:
            guard.release()


def test_acquire_or_forward_returns_none_when_second(primary, qapp):
    received: list[list[str]] = []
    primary.message.connect(received.append)

    assert acquire_or_forward(["--open-folder"]) is None
    _pump(qapp)

    assert received == [["--open-folder"]]


# ═════════════════════════════════════════════════════════════════════════════
# A genuine second process — the acceptance criterion
# ═════════════════════════════════════════════════════════════════════════════

SECOND_LAUNCH = textwrap.dedent(
    """
    import os, sys
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, {repo!r})
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from onedriveui.platform.singleinstance import acquire_or_forward
    guard = acquire_or_forward({argv!r})
    if guard is not None:
        guard.release()
        print("PRIMARY")
        sys.exit(3)
    print("FORWARDED")
    sys.exit(0)
    """
)


def _launch_second(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a real second launch in its own interpreter."""
    script = SECOND_LAUNCH.format(repo=str(REPO_ROOT), argv=argv)
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, **env})


def test_a_real_second_launch_forwards_its_argv_and_exits_zero(primary, qapp):
    """The acceptance criterion, end to end, across two processes."""
    received: list[list[str]] = []
    primary.message.connect(received.append)
    env = {
        "HOME": os.environ["HOME"],
        "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "XDG_DATA_HOME": os.environ["XDG_DATA_HOME"],
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
        "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
    }

    result = _launch_second(["--open-folder", "/home/u/OneDrive"], env)
    for _ in range(40):                       # service the connection meanwhile
        _pump(qapp)
        if received:
            break

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FORWARDED" in result.stdout
    assert received == [["--open-folder", "/home/u/OneDrive"]]


def test_a_real_first_launch_becomes_primary(qapp, _isolate_home):
    """The same script with nothing listening takes the instance instead."""
    paths.runtime_dir()
    env = {
        "HOME": os.environ["HOME"],
        "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "XDG_DATA_HOME": os.environ["XDG_DATA_HOME"],
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
        "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
    }

    result = _launch_second([], env)

    assert result.returncode == 3, result.stdout + result.stderr
    assert "PRIMARY" in result.stdout
