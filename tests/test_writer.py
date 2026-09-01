"""data/writer.py — the single writer thread, batching and durability.

Two guarantees, and both are load-bearing:

* **batching** keeps observability writes off the GUI thread's fsync path;
* **urgent** makes a latch or an answered decision durable BEFORE the call
  returns, because those are what crash recovery reads back.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.constants import DB_FLUSH_MS
from onedriveui.data import db
from onedriveui.data.writer import WRITER, DbWriter


@pytest.fixture
def writer(_isolate_home, qapp):
    """A started DbWriter on an isolated database, stopped afterwards."""
    instance = DbWriter(paths.db_file())
    assert instance.start_writer()
    try:
        yield instance
    finally:
        instance.stop()
        db.close_all()


@pytest.fixture
def reader(writer) -> sqlite3.Connection:
    """A SECOND connection, so durability claims are checked independently."""
    return db.open_ro(writer.path)


def seed_account(writer: DbWriter, account_id: str = "a") -> str:
    writer.submit_sync(
        lambda conn: conn.execute(
            "INSERT INTO accounts (id, remote, sync_root, added_at) "
            "VALUES (?,?,?,?)", (account_id, account_id, "/tmp/a", "t")),
        urgent=True)
    return account_id


# ═════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═════════════════════════════════════════════════════════════════════════════

def test_the_writer_opens_and_migrates_its_own_database(writer):
    tables = writer.submit_sync(lambda conn: db.table_names(conn))
    assert "accounts" in tables
    assert writer.submit_sync(db.current_version) == db.SCHEMA_VERSION


def test_the_module_singleton_exists_but_is_not_started():
    """Starting a thread as an import side effect would beat integrity_check."""
    assert isinstance(WRITER, DbWriter)
    assert WRITER.isRunning() is False


def test_start_writer_is_idempotent(writer):
    assert writer.start_writer() is True
    assert writer.isRunning()


def test_start_writer_reports_a_database_that_cannot_be_opened(_isolate_home,
                                                               qapp, tmp_path,
                                                               monkeypatch):
    from onedriveui.errors import SafetyRefusal
    mount = tmp_path / "OneDrive"
    mount.mkdir()
    monkeypatch.setattr(paths, "fuse_rclone_mounts",
                        lambda: [("onedrive:", mount)])
    instance = DbWriter(mount / "state.db")
    with pytest.raises(SafetyRefusal):
        instance.start_writer()
    instance.stop()


def test_stop_is_idempotent_and_joins(writer):
    assert writer.stop() is True
    assert writer.stop() is True
    assert writer.isRunning() is False


def test_submit_after_stop_raises_rather_than_dropping(writer):
    writer.stop()
    with pytest.raises(RuntimeError, match="stopping"):
        writer.submit(lambda conn: None)
    with pytest.raises(RuntimeError, match="stopping"):
        writer.submit_sync(lambda conn: None)


def test_stop_applies_everything_still_queued(writer):
    """A stop must not silently discard a write the caller believed accepted."""
    seed_account(writer)
    for n in range(200):
        writer.submit(lambda conn, n=n: conn.execute(
            "INSERT INTO kv (account_id, key, value, updated_at) "
            "VALUES ('', ?, ?, 't')", (f"k{n}", str(n))))
    writer.stop()
    conn = sqlite3.connect(str(writer.path))
    assert conn.execute("SELECT count(*) FROM kv").fetchone()[0] == 200
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Ordering, batching and throughput
# ═════════════════════════════════════════════════════════════════════════════

def test_ten_thousand_submits_from_four_threads(writer, reader, bus_spy, qtbot):
    """BUILD_PLAN acceptance: in order, <= 110 batches, no 'database is locked'."""
    seed_account(writer)
    applied: list[tuple[int, int]] = []
    bus_spy.watch("log_line")           # the writer reports every failure here

    def producer(tid: int) -> None:
        for n in range(2_500):
            writer.submit(lambda conn, t=tid, k=n: applied.append((t, k)))

    threads = [threading.Thread(target=producer, args=(t,)) for t in range(4)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert writer.flush(60_000) is True
    elapsed = time.monotonic() - started

    assert len(applied) == 10_000
    assert writer.ops_failed == 0
    assert writer.transaction_errors == 0

    # No "database is locked": one writer plus WAL plus busy_timeout=5000 means
    # SQLITE_BUSY cannot happen, and the writer would have said so if it had.
    qtbot.process(5)
    reported = [args[0] for args in bus_spy.of("log_line")]
    assert not any("locked" in line for line in reported), reported
    assert reported == []

    # In order: each producer's writes were applied in the order it submitted.
    last: dict[int, int] = {}
    for tid, n in applied:
        assert last.get(tid, -1) + 1 == n, f"thread {tid} applied out of order"
        last[tid] = n
    assert last == {t: 2_499 for t in range(4)}

    # Batched: at most one commit per flush window, plus the seed and flush.
    assert writer.batches <= 110, writer.batches
    assert elapsed < 30


def test_a_single_op_commits_without_waiting_out_the_whole_window(writer, reader):
    seed_account(writer)
    started = time.monotonic()
    writer.submit_sync(lambda conn: conn.execute(
        "INSERT INTO kv (account_id, key, value, updated_at) "
        "VALUES ('','solo','1','t')"), urgent=False)
    elapsed_ms = (time.monotonic() - started) * 1000
    assert reader.execute("SELECT value FROM kv WHERE key='solo'").fetchone()[0] == "1"
    assert elapsed_ms < DB_FLUSH_MS * 3


def test_a_batch_is_one_transaction(writer, reader):
    seed_account(writer)
    before = writer.batches
    done = threading.Event()
    for n in range(50):
        writer.submit(lambda conn, n=n: conn.execute(
            "INSERT INTO kv (account_id, key, value, updated_at) "
            "VALUES ('', ?, '1', 't')", (f"b{n}",)))
    writer.submit(lambda conn: done.set())
    writer.flush()
    assert done.wait(5)
    assert writer.batches - before <= 3
    assert reader.execute(
        "SELECT count(*) FROM kv WHERE key LIKE 'b%'").fetchone()[0] == 50


def test_flush_returns_false_when_not_running(_isolate_home, qapp):
    assert DbWriter(paths.db_file()).flush() is False


# ═════════════════════════════════════════════════════════════════════════════
# Durability
# ═════════════════════════════════════════════════════════════════════════════

def test_urgent_is_durable_before_submit_sync_returns(writer):
    """BUILD_PLAN acceptance, verified from a SECOND connection.

    The second connection is opened fresh each time, so nothing can be served
    from a cache this process controls.
    """
    seed_account(writer)
    for n in range(20):
        writer.submit_sync(
            lambda conn, n=n: conn.execute(
                "INSERT INTO latches (account_id, name, set_at) "
                "VALUES ('a', ?, 't')", (f"latch{n}",)),
            urgent=True)
        independent = sqlite3.connect(f"file:{writer.path}?mode=ro", uri=True)
        try:
            count = independent.execute(
                "SELECT count(*) FROM latches").fetchone()[0]
        finally:
            independent.close()
        assert count == n + 1, "urgent write was not durable on return"


def test_a_non_urgent_submit_sync_is_also_durable_on_return(writer, reader):
    seed_account(writer)
    writer.submit_sync(lambda conn: conn.execute(
        "INSERT INTO kv (account_id, key, value, updated_at) "
        "VALUES ('','k','v','t')"), urgent=False)
    assert reader.execute("SELECT value FROM kv WHERE key='k'").fetchone()[0] == "v"


def test_submit_sync_returns_the_operation_result(writer):
    assert writer.submit_sync(lambda conn: 42) == 42
    assert writer.submit_sync(
        lambda conn: conn.execute("SELECT 7").fetchone()[0]) == 7


def test_an_urgent_op_cuts_the_batch_short(writer, reader):
    seed_account(writer)
    for n in range(10):
        writer.submit(lambda conn, n=n: conn.execute(
            "INSERT INTO kv (account_id, key, value, updated_at) "
            "VALUES ('', ?, '1', 't')", (f"pre{n}",)))
    writer.submit_sync(lambda conn: conn.execute(
        "INSERT INTO latches (account_id, name, set_at) "
        "VALUES ('a','needs_resync','t')"), urgent=True)
    # Everything queued before the urgent op committed with it (FIFO).
    assert reader.execute(
        "SELECT count(*) FROM kv WHERE key LIKE 'pre%'").fetchone()[0] == 10
    assert reader.execute("SELECT count(*) FROM latches").fetchone()[0] == 1


# ═════════════════════════════════════════════════════════════════════════════
# Failures
# ═════════════════════════════════════════════════════════════════════════════

def test_a_failing_op_raises_in_the_caller_not_the_writer(writer):
    def boom(conn: sqlite3.Connection) -> None:
        raise ValueError("deliberate")

    with pytest.raises(ValueError, match="deliberate"):
        writer.submit_sync(boom)
    assert writer.isRunning()
    assert writer.ops_failed >= 1


def test_a_failing_op_does_not_poison_its_batch(writer, reader):
    seed_account(writer)
    writer.submit(lambda conn: conn.execute("THIS IS NOT SQL"))
    writer.submit(lambda conn: conn.execute(
        "INSERT INTO kv (account_id, key, value, updated_at) "
        "VALUES ('','survivor','1','t')"))
    writer.flush()
    assert reader.execute(
        "SELECT value FROM kv WHERE key='survivor'").fetchone()[0] == "1"
    assert writer.ops_failed >= 1


def test_a_write_failure_is_reported_on_the_bus(writer, bus_spy, qtbot):
    bus_spy.watch("log_line")
    writer.submit(lambda conn: conn.execute("ALSO NOT SQL"))
    writer.flush()
    qtbot.process(5)
    lines = [args[0] for args in bus_spy.of("log_line")]
    assert any("DbWriter" in line and "write failed" in line for line in lines)


def test_submit_sync_times_out_rather_than_hanging(writer, monkeypatch):
    """A wedged writer must surface as an error, never as a frozen UI."""
    blocked = threading.Event()
    writer.submit(lambda conn: blocked.wait(3))
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            writer.submit_sync(lambda conn: None, timeout_ms=50, urgent=True)
    finally:
        blocked.set()
    writer.flush(10_000)


def test_counters_are_exposed_for_the_about_page(writer):
    seed_account(writer)
    assert writer.batches >= 1
    assert writer.ops_applied >= 1
    assert writer.queue_depth == 0
    assert isinstance(writer.path, Path)


# ═════════════════════════════════════════════════════════════════════════════
# The invariant it enforces
# ═════════════════════════════════════════════════════════════════════════════

def test_no_other_thread_can_open_the_write_connection(writer):
    """ARCHITECTURE §7.6, enforced by db.open_rw's ownership check."""
    from onedriveui.errors import SafetyRefusal
    with pytest.raises(SafetyRefusal):
        db.open_rw(writer.path)


def test_the_gui_thread_reads_through_a_read_only_connection(writer, reader):
    seed_account(writer)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.execute("INSERT INTO kv (account_id, key, value, updated_at) "
                       "VALUES ('','x','1','t')")


def test_concurrent_readers_never_see_database_is_locked(writer):
    """WAL plus busy_timeout: a reader must never fail against a busy writer."""
    seed_account(writer)
    failures: list[str] = []
    stop = threading.Event()

    def reader_thread() -> None:
        conn = db.open_ro(writer.path)
        try:
            while not stop.is_set():
                try:
                    conn.execute("SELECT count(*) FROM kv").fetchone()
                except sqlite3.OperationalError as exc:
                    failures.append(str(exc))
        finally:
            db.close_ro(writer.path)

    readers = [threading.Thread(target=reader_thread) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for n in range(3_000):
            writer.submit(lambda conn, n=n: conn.execute(
                "INSERT INTO kv (account_id, key, value, updated_at) "
                "VALUES ('', ?, '1', 't')", (f"c{n}",)))
        writer.flush(30_000)
    finally:
        stop.set()
        for thread in readers:
            thread.join(10)
    assert failures == []
