"""atomicio.py — crash-safe writes, md5, and PID identity.

The central claim under test is that a write which is interrupted at ANY point
leaves the destination either wholly old or wholly new, never truncated, and
that the `.bak` always parses.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from onedriveui import atomicio
from onedriveui.paths import FILE_MODE


# ═════════════════════════════════════════════════════════════════════════════
# Atomic writes
# ═════════════════════════════════════════════════════════════════════════════

def test_atomic_write_bytes_creates_the_file_with_0600(tmp_path):
    target = tmp_path / "sub" / "endpoints.json"
    atomicio.atomic_write_bytes(target, b"payload")
    assert target.read_bytes() == b"payload"
    assert target.stat().st_mode & 0o777 == FILE_MODE


def test_atomic_write_bytes_leaves_no_temporary_files(tmp_path):
    target = tmp_path / "a.json"
    atomicio.atomic_write_bytes(target, b"x" * 4096)
    assert [p.name for p in tmp_path.iterdir()] == ["a.json"]


def test_atomic_write_text_adds_no_trailing_newline(tmp_path):
    """The filters .md5 sidecar must be exactly 32 hex characters."""
    target = tmp_path / "filters.txt.md5"
    digest = atomicio.md5_of_bytes(b"rules")
    atomicio.atomic_write_text(target, digest)
    assert target.read_text(encoding="utf-8") == digest
    assert len(target.read_bytes()) == 32


def test_atomic_write_json_round_trips(tmp_path):
    target = tmp_path / "config.json"
    payload = {"schema_version": 1, "app": {"theme": "dark"}, "accounts": []}
    atomicio.atomic_write_json(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert target.read_text(encoding="utf-8").endswith("\n")


def test_atomic_write_json_refuses_unserialisable_without_touching_the_file(tmp_path):
    """The old, valid document must survive a caller's mistake."""
    target = tmp_path / "config.json"
    atomicio.atomic_write_json(target, {"good": True})
    with pytest.raises(TypeError):
        atomicio.atomic_write_json(target, {"bad": object()})
    assert json.loads(target.read_text(encoding="utf-8")) == {"good": True}
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_a_crash_mid_write_never_truncates_the_destination(tmp_path, monkeypatch):
    """BUILD_PLAN acceptance: killing the process mid-atomic_write_json.

    The kill is simulated by making the payload write itself raise, which is
    the moment a real SIGKILL would be most damaging — the temporary file is
    open and partially written.
    """
    target = tmp_path / "config.json"
    good = {"schema_version": 1, "app": {"theme": "light"}}
    atomicio.atomic_write_json(target, good)
    original = target.read_bytes()

    real_replace = os.replace
    calls: list[int] = []

    class Boom(OSError):
        pass

    def die(*_args, **_kwargs):
        calls.append(1)
        raise Boom(errno.EIO, "simulated SIGKILL mid-write")

    # 1. die while writing the temp file
    monkeypatch.setattr(atomicio.os, "fsync", die)
    with pytest.raises(OSError):
        atomicio.atomic_write_json(target, {"schema_version": 2})
    assert target.read_bytes() == original
    assert json.loads(target.read_text(encoding="utf-8")) == good
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    # 2. die at the rename itself
    monkeypatch.undo()
    monkeypatch.setattr(atomicio.os, "replace", die)
    with pytest.raises(OSError):
        atomicio.atomic_write_json(target, {"schema_version": 3})
    assert target.read_bytes() == original
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

    monkeypatch.undo()
    assert os.replace is real_replace
    assert len(calls) == 2


def test_every_intermediate_state_of_the_file_parses(tmp_path):
    """Read the target from another thread throughout a hundred rewrites.

    The reader never sees a partial document, because os.replace is atomic.
    """
    target = tmp_path / "config.json"
    atomicio.atomic_write_json(target, {"n": 0})
    stop = threading.Event()
    failures: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(target.read_text(encoding="utf-8"))
            except FileNotFoundError:
                failures.append("the path vanished mid-write")
            except json.JSONDecodeError as exc:
                failures.append(f"partial document observed: {exc}")

    watcher = threading.Thread(target=reader, daemon=True)
    watcher.start()
    try:
        for n in range(1, 200):
            atomicio.atomic_write_json(target, {"n": n, "pad": "x" * 5000})
    finally:
        stop.set()
        watcher.join(5)
    assert failures == []


# ═════════════════════════════════════════════════════════════════════════════
# backup_then_write
# ═════════════════════════════════════════════════════════════════════════════

def test_backup_then_write_rotates_the_previous_copy(tmp_path):
    target = tmp_path / "config.json"
    atomicio.atomic_write_json(target, {"v": 1})
    atomicio.backup_then_write(target, json.dumps({"v": 2}))
    assert json.loads(target.read_text()) == {"v": 2}
    assert json.loads((tmp_path / "config.json.bak").read_text()) == {"v": 1}


def test_backup_then_write_on_a_missing_file_makes_no_bak(tmp_path):
    target = tmp_path / "config.json"
    atomicio.backup_then_write(target, b"{}")
    assert target.exists()
    assert not (tmp_path / "config.json.bak").exists()


def test_the_bak_always_parses_across_many_rewrites(tmp_path):
    """BUILD_PLAN acceptance: the .bak always parses."""
    target = tmp_path / "config.json"
    backup = tmp_path / "config.json.bak"
    for n in range(50):
        atomicio.backup_then_write(target, json.dumps({"n": n}))
        assert json.loads(target.read_text())["n"] == n
        if backup.exists():
            assert json.loads(backup.read_text())["n"] == n - 1


def test_backup_then_write_keeps_0600_on_both_files(tmp_path):
    target = tmp_path / "config.json"
    atomicio.backup_then_write(target, b"{}")
    atomicio.backup_then_write(target, b'{"x":1}')
    assert target.stat().st_mode & 0o777 == FILE_MODE
    assert (tmp_path / "config.json.bak").stat().st_mode & 0o777 == FILE_MODE


def test_backup_then_write_accepts_bytes_and_text(tmp_path):
    target = tmp_path / "a.txt"
    atomicio.backup_then_write(target, "héllo")
    assert target.read_text(encoding="utf-8") == "héllo"
    atomicio.backup_then_write(target, b"raw")
    assert target.read_bytes() == b"raw"


# ═════════════════════════════════════════════════════════════════════════════
# read_json
# ═════════════════════════════════════════════════════════════════════════════

def test_read_json_returns_the_default_for_every_failure(tmp_path):
    missing = tmp_path / "nope.json"
    assert atomicio.read_json(missing, default={"d": 1}) == {"d": 1}
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert atomicio.read_json(corrupt, default=None) is None
    binary = tmp_path / "bin.json"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    assert atomicio.read_json(binary, default="fallback") == "fallback"


def test_read_json_reads_a_good_file(tmp_path):
    path = tmp_path / "good.json"
    atomicio.atomic_write_json(path, [1, 2, 3])
    assert atomicio.read_json(path) == [1, 2, 3]


# ═════════════════════════════════════════════════════════════════════════════
# md5
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("payload", [
    b"",
    b"a",
    b"- *.partial\n- .Trash-1000/\n",
    b"\x00\xff" * 1000,
    "héllo wörld".encode("utf-8"),
])
def test_md5_of_file_is_byte_identical_to_md5sum(tmp_path, payload):
    """BUILD_PLAN acceptance, checked against the real md5sum binary."""
    md5sum = shutil.which("md5sum")
    if md5sum is None:
        pytest.skip("md5sum not installed")
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)
    expected = subprocess.run([md5sum, str(path)], capture_output=True,
                              text=True, check=True).stdout.split()[0]
    assert atomicio.md5_of_file(path) == expected


def test_md5_of_file_matches_md5_of_bytes(tmp_path):
    payload = b"x" * (3 * atomicio.MD5_CHUNK_BYTES + 17)   # spans chunks
    path = tmp_path / "big.bin"
    path.write_bytes(payload)
    assert atomicio.md5_of_file(path) == atomicio.md5_of_bytes(payload)


def test_md5_is_32_lowercase_hex(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"anything")
    digest = atomicio.md5_of_file(path)
    assert len(digest) == 32
    assert digest == digest.lower()
    int(digest, 16)


def test_md5_of_bytes_accepts_text():
    assert atomicio.md5_of_bytes("abc") == atomicio.md5_of_bytes(b"abc")


def test_md5_of_a_missing_file_raises(tmp_path):
    with pytest.raises(OSError):
        atomicio.md5_of_file(tmp_path / "absent")


# ═════════════════════════════════════════════════════════════════════════════
# PID identity
# ═════════════════════════════════════════════════════════════════════════════

def test_proc_starttime_reads_field_22():
    """Cross-checked against a raw parse of /proc/self/stat."""
    pid = os.getpid()
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = raw[raw.rfind(")") + 1:].split()
    assert atomicio.proc_starttime(pid) == int(fields[19])   # field 22


def test_proc_starttime_of_a_dead_pid_is_none():
    assert atomicio.proc_starttime(4_000_000) is None
    assert atomicio.proc_starttime(0) is None
    assert atomicio.proc_starttime(-1) is None


def test_proc_starttime_survives_a_comm_with_spaces_and_parens(tmp_path, monkeypatch):
    """A hostile argv[0] breaks every naive `line.split()[21]`."""
    fake = tmp_path / "proc"
    (fake / "999").mkdir(parents=True)
    # Each token equals its own 1-indexed field number, so a wrong index is
    # visible in the assertion rather than merely wrong.
    tail = " ".join(str(n) for n in range(4, 54))
    (fake / "999" / "stat").write_text(f"999 (evil ) name) S {tail}\n")

    real_open = open

    def fake_open(path, *args, **kwargs):
        if str(path) == "/proc/999/stat":
            return real_open(fake / "999" / "stat", *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert atomicio.proc_starttime(999) == 22


def test_pid_is_alive_requires_a_matching_starttime():
    pid = os.getpid()
    starttime = atomicio.proc_starttime(pid)
    assert atomicio.pid_is_alive(pid, starttime) is True
    # The same PID with a different start time is a RECYCLED pid, not ours.
    assert atomicio.pid_is_alive(pid, starttime + 1) is False


def test_pid_is_alive_without_a_starttime_only_checks_existence():
    assert atomicio.pid_is_alive(os.getpid()) is True
    assert atomicio.pid_is_alive(os.getpid(), 0) is True
    assert atomicio.pid_is_alive(4_000_000, 12345) is False
    assert atomicio.pid_is_alive(0) is False


def test_pid_is_alive_of_a_reaped_child_is_false():
    proc = subprocess.Popen(["/bin/true"])
    starttime = atomicio.proc_starttime(proc.pid)
    proc.wait()
    assert atomicio.pid_is_alive(proc.pid, starttime) is False


# ═════════════════════════════════════════════════════════════════════════════
# InstanceLock
# ═════════════════════════════════════════════════════════════════════════════

def test_instance_lock_acquire_and_release(tmp_path):
    lock = atomicio.InstanceLock(tmp_path / "ui.lock")
    assert lock.acquire() is True
    assert lock.held is True
    assert lock.acquire() is True                   # idempotent
    assert lock.path.stat().st_mode & 0o777 == FILE_MODE
    pid, starttime = lock.owner()
    assert pid == os.getpid()
    assert starttime == atomicio.proc_starttime(os.getpid())
    lock.release()
    assert lock.held is False
    assert not lock.path.exists()
    lock.release()                                  # idempotent


def test_instance_lock_blocks_a_second_process(tmp_path):
    """A real second process, because flock is per-open-file-description."""
    path = tmp_path / "ui.lock"
    held = atomicio.InstanceLock(path)
    assert held.acquire()
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from onedriveui.atomicio import InstanceLock\n"
        "print('acquired' if InstanceLock(%r).acquire() else 'blocked')\n"
        % (str(Path(atomicio.__file__).resolve().parent.parent), str(path))
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, timeout=30, check=True).stdout.strip()
    held.release()
    assert out == "blocked"


def test_instance_lock_is_free_again_after_the_holder_exits(tmp_path):
    path = tmp_path / "ui.lock"
    lock = atomicio.InstanceLock(path)
    assert lock.acquire()
    lock.release()
    assert atomicio.InstanceLock(path).acquire() is True


def test_instance_lock_context_manager(tmp_path):
    path = tmp_path / "ui.lock"
    with atomicio.InstanceLock(path) as lock:
        assert lock.held
    assert not path.exists()


def test_instance_lock_context_manager_raises_when_held(tmp_path):
    path = tmp_path / "ui.lock"
    first = atomicio.InstanceLock(path)
    assert first.acquire()
    try:
        with pytest.raises(BlockingIOError):
            with atomicio.InstanceLock(path):
                pass
    finally:
        first.release()


def test_owner_is_alive_detects_a_stale_lock_file(tmp_path):
    """A lock file left by a process whose PID has been recycled is stale."""
    path = tmp_path / "ui.lock"
    path.write_text("4000000 999999\n", encoding="ascii")
    assert atomicio.InstanceLock(path).owner_is_alive() is False


def test_owner_of_a_garbage_lock_file_is_none(tmp_path):
    path = tmp_path / "ui.lock"
    path.write_text("not a pid\n", encoding="ascii")
    lock = atomicio.InstanceLock(path)
    assert lock.owner() is None
    assert lock.owner_is_alive() is False
    path.write_text("", encoding="ascii")
    assert lock.owner() is None


def test_owner_of_a_missing_lock_file_is_none(tmp_path):
    assert atomicio.InstanceLock(tmp_path / "absent.lock").owner() is None


def test_owner_is_alive_is_true_while_we_hold_it(tmp_path):
    lock = atomicio.InstanceLock(tmp_path / "ui.lock")
    assert lock.acquire()
    try:
        assert lock.owner_is_alive() is True
    finally:
        lock.release()


# ═════════════════════════════════════════════════════════════════════════════
# fsync_dir
# ═════════════════════════════════════════════════════════════════════════════

def test_fsync_dir_is_silent_on_a_missing_directory(tmp_path):
    atomicio.fsync_dir(tmp_path / "absent")          # must not raise


def test_fsync_dir_works_on_a_real_directory(tmp_path):
    atomicio.fsync_dir(tmp_path)
