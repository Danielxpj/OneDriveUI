"""data/db.py — pragmas, the one writer, migrations and corruption recovery.

The pragma assertions are not box-ticking. `foreign_keys` is per-connection and
defaults to OFF, and every table in the schema cascades from `accounts(id)`; a
connection that skipped it would orphan rows instead of deleting them, silently.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.data import db
from onedriveui.errors import SafetyRefusal


@pytest.fixture
def dbpath(_isolate_home) -> Path:
    """A fresh, migrated database, closed again afterwards."""
    path = paths.db_file()
    conn = db.open_rw(path)
    db.migrate(conn)
    yield path
    db.close_all()


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    db.close_all()


# ═════════════════════════════════════════════════════════════════════════════
# Pragmas
# ═════════════════════════════════════════════════════════════════════════════

def test_the_four_house_pragmas_are_applied(dbpath):
    conn = db.open_rw(dbpath)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1      # NORMAL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_pragma_table_is_the_documented_one():
    assert db.PRAGMAS == (
        ("journal_mode", "WAL"), ("synchronous", "NORMAL"),
        ("busy_timeout", "5000"), ("foreign_keys", "ON"))


def test_foreign_keys_actually_cascade(dbpath):
    """The reason the pragma matters: ON DELETE CASCADE is off without it."""
    conn = db.open_rw(dbpath)
    conn.execute("INSERT INTO accounts (id, remote, sync_root, added_at) "
                 "VALUES ('a','a','/tmp/a','t')")
    conn.execute("INSERT INTO latches (account_id, name, set_at) "
                 "VALUES ('a','needs_resync','t')")
    conn.execute("DELETE FROM accounts WHERE id = 'a'")
    assert conn.execute("SELECT count(*) FROM latches").fetchone()[0] == 0


def test_a_foreign_key_violation_is_rejected(dbpath):
    conn = db.open_rw(dbpath)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO latches (account_id, name, set_at) "
                     "VALUES ('ghost','needs_resync','t')")


def test_rows_come_back_as_mappings(dbpath):
    conn = db.open_rw(dbpath)
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["one"] == 1
    assert db.row_to_dict(row) == {"one": 1}
    assert db.row_to_dict(None) is None


def test_the_database_file_is_0600(dbpath):
    assert dbpath.stat().st_mode & 0o777 == 0o600


# ═════════════════════════════════════════════════════════════════════════════
# open_rw — exactly one connection
# ═════════════════════════════════════════════════════════════════════════════

def test_open_rw_returns_exactly_one_connection(dbpath):
    """BUILD_PLAN acceptance."""
    first = db.open_rw(dbpath)
    assert db.open_rw(dbpath) is first
    assert db.open_rw(dbpath) is first


def test_open_rw_refuses_a_second_thread(dbpath):
    """ARCHITECTURE §7.6: no SQLite write outside DbWriter, enforced in code."""
    db.open_rw(dbpath)
    caught: list[BaseException] = []

    def other() -> None:
        try:
            db.open_rw(dbpath)
        except BaseException as exc:      # noqa: BLE001 - recorded for assertion
            caught.append(exc)

    thread = threading.Thread(target=other)
    thread.start()
    thread.join(10)
    assert len(caught) == 1
    assert isinstance(caught[0], SafetyRefusal)
    assert "DbWriter" in str(caught[0])


def test_close_rw_releases_ownership(dbpath):
    db.open_rw(dbpath)
    db.close_rw(dbpath)
    caught: list[bool] = []

    def other() -> None:
        db.open_rw(dbpath)                # now legal: nobody owns it
        caught.append(True)
        db.close_rw(dbpath)

    thread = threading.Thread(target=other)
    thread.start()
    thread.join(10)
    assert caught == [True]


def test_close_rw_is_idempotent(dbpath):
    db.close_rw(dbpath)
    db.close_rw(dbpath)


def test_open_rw_creates_the_parent_directory(tmp_path):
    path = tmp_path / "deep" / "nested" / "state.db"
    conn = db.open_rw(path)
    assert path.exists()
    conn.close()
    db.close_rw(path)


# ═════════════════════════════════════════════════════════════════════════════
# open_ro — thread-local and genuinely read-only
# ═════════════════════════════════════════════════════════════════════════════

def test_open_ro_is_cached_per_thread(dbpath):
    first = db.open_ro(dbpath)
    assert db.open_ro(dbpath) is first
    others: list[sqlite3.Connection] = []

    def other() -> None:
        others.append(db.open_ro(dbpath))
        db.close_ro(dbpath)

    thread = threading.Thread(target=other)
    thread.start()
    thread.join(10)
    assert len(others) == 1
    assert others[0] is not first


def test_open_ro_refuses_writes(dbpath):
    conn = db.open_ro(dbpath)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO kv (account_id, key, value, updated_at) "
                     "VALUES ('','k','v','t')")


def test_open_ro_sees_a_committed_write_immediately(dbpath):
    """WAL: readers do not block and do not go stale after a commit."""
    reader = db.open_ro(dbpath)
    assert reader.execute("SELECT count(*) FROM kv").fetchone()[0] == 0
    writer = db.open_rw(dbpath)
    writer.execute("INSERT INTO kv (account_id, key, value, updated_at) "
                   "VALUES ('','k','v','t')")
    assert reader.execute("SELECT count(*) FROM kv").fetchone()[0] == 1


def test_open_ro_of_a_missing_file_raises(tmp_path):
    """A read-only connection must not conjure an empty database."""
    with pytest.raises(sqlite3.OperationalError):
        db.open_ro(tmp_path / "absent.db")


def test_close_ro_is_idempotent(dbpath):
    db.open_ro(dbpath)
    db.close_ro(dbpath)
    db.close_ro(dbpath)
    db.close_ro()


# ═════════════════════════════════════════════════════════════════════════════
# The FUSE refusal
# ═════════════════════════════════════════════════════════════════════════════

def test_open_rw_refuses_a_database_under_a_fuse_mount(tmp_path, monkeypatch):
    """WAL needs POSIX locking and shared memory the rclone mount lacks."""
    mount = tmp_path / "OneDrive"
    mount.mkdir()
    monkeypatch.setattr(paths, "fuse_rclone_mounts",
                        lambda: [("onedrive:", mount)])
    with pytest.raises(SafetyRefusal, match="I2"):
        db.open_rw(mount / "state.db")
    with pytest.raises(SafetyRefusal):
        db.open_ro(mount / "state.db")
    with pytest.raises(SafetyRefusal):
        db.integrity_check(mount / "state.db")


def test_the_default_database_location_is_not_under_a_mount():
    """~/.local/share is never inside ~/OneDrive; assert it, don't assume it."""
    assert paths.is_under_fuse_mount(paths.db_file()) is False


# ═════════════════════════════════════════════════════════════════════════════
# Migration
# ═════════════════════════════════════════════════════════════════════════════

def test_migrate_brings_a_fresh_database_to_the_current_version(tmp_path):
    conn = db.open_rw(tmp_path / "fresh.db")
    assert db.current_version(conn) == 0
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.current_version(conn) == db.SCHEMA_VERSION


def test_migrate_is_idempotent(dbpath):
    conn = db.open_rw(dbpath)
    tables = db.table_names(conn)
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.migrate(conn) == db.SCHEMA_VERSION
    assert db.table_names(conn) == tables


def test_the_migration_chain_reproduces_schema_sql_exactly(dbpath):
    """The two must never drift: one is applied, the other is the reference."""
    migrated = db.catalogue(db.open_rw(dbpath))
    reference = sqlite3.connect(":memory:")
    reference.executescript(db.schema_sql())
    assert db.catalogue(reference) == migrated
    reference.close()


def test_every_documented_table_exists(dbpath):
    assert set(db.table_names(db.open_rw(dbpath))) == {
        "schema_meta", "accounts", "latches", "activity", "issues", "pins",
        "cache_index", "conflicts", "runs", "decisions", "versions", "trashbin",
        "share_links", "folder_selection", "kfm_folder", "notifications",
        "dialog_seen", "kv"}


def test_migration_files_are_ordered_and_named():
    files = db.migration_files()
    assert files
    assert [version for version, _ in files] == sorted(v for v, _ in files)
    assert files[0][0] == 1
    assert files[0][1].name == "001_initial.sql"
    assert max(v for v, _ in files) == db.SCHEMA_VERSION


def test_a_failing_migration_rolls_back(tmp_path, monkeypatch):
    """A half-applied migration must be impossible."""
    path = tmp_path / "state.db"
    conn = db.open_rw(path)
    bad = tmp_path / "002_bad.sql"
    bad.write_text("CREATE TABLE ok_so_far (x);\nTHIS IS NOT SQL;\n")
    monkeypatch.setattr(db, "migration_files",
                        lambda: [(1, db.MIGRATIONS_DIR / "001_initial.sql"),
                                 (2, bad)])
    with pytest.raises(sqlite3.Error):
        db.migrate(conn)
    assert "ok_so_far" not in db.table_names(conn)
    assert db.current_version(conn) == 1        # stayed at the last good one
    db.close_rw(path)


def test_executescript_all_is_all_or_nothing(dbpath):
    conn = db.open_rw(dbpath)
    with pytest.raises(sqlite3.Error):
        db.executescript_all(conn, ["CREATE TABLE t1 (x);", "NOT SQL;"])
    assert "t1" not in db.table_names(conn)
    db.executescript_all(conn, ["CREATE TABLE t1 (x);", "CREATE TABLE t2 (x);"])
    assert "t1" in db.table_names(conn)
    assert "t2" in db.table_names(conn)


# ═════════════════════════════════════════════════════════════════════════════
# integrity_check
# ═════════════════════════════════════════════════════════════════════════════

def test_integrity_check_passes_a_healthy_database(dbpath):
    db.close_all()
    assert db.integrity_check(dbpath) is True
    assert not list(dbpath.parent.glob("*.corrupt-*"))


def test_integrity_check_creates_a_missing_database(tmp_path):
    path = tmp_path / "state.db"
    assert db.integrity_check(path) is True
    assert path.exists()
    conn = db.open_rw(path)
    assert db.current_version(conn) == db.SCHEMA_VERSION
    db.close_rw(path)


def test_integrity_check_renames_a_corrupt_database_and_recreates_it(dbpath):
    """BUILD_PLAN: renames to state.db.corrupt-<ts> and recreates."""
    conn = db.open_rw(dbpath)
    conn.execute("INSERT INTO kv (account_id, key, value, updated_at) "
                 "VALUES ('','marker','1','t')")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close_all()

    with open(dbpath, "r+b") as handle:      # scribble over the page map
        handle.seek(100)
        handle.write(b"\x00" * 8192)

    assert db.integrity_check(dbpath) is False
    corrupt = list(dbpath.parent.glob("state.db.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].name.startswith("state.db" + db.CORRUPT_SUFFIX)
    assert corrupt[0].stat().st_size > 0      # kept, not deleted

    conn = db.open_rw(dbpath)                 # a working database is in place
    assert db.current_version(conn) == db.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM kv").fetchone()[0] == 0


def test_integrity_check_handles_a_file_that_is_not_a_database(tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"this is a text file, not SQLite")
    assert db.integrity_check(path) is False
    assert len(list(tmp_path.glob("state.db.corrupt-*"))) == 1
    conn = db.open_rw(path)
    assert db.current_version(conn) == db.SCHEMA_VERSION
    db.close_rw(path)


def test_two_corruptions_do_not_collide(tmp_path, monkeypatch):
    """A fixed clock would otherwise make the second rename clobber the first."""
    monkeypatch.setattr(db, "utcnow_iso", lambda: "2026-08-31T12:00:00Z")
    for _ in range(2):
        path = tmp_path / "state.db"
        path.write_bytes(b"not a database at all")
        assert db.integrity_check(path) is False
        db.close_all()
    assert len(list(tmp_path.glob("state.db.corrupt-*"))) == 2


def test_the_wal_sidecars_move_with_a_corrupt_database(dbpath):
    conn = db.open_rw(dbpath)
    conn.execute("INSERT INTO kv (account_id, key, value, updated_at) "
                 "VALUES ('','k','v','t')")
    assert dbpath.with_name(dbpath.name + "-wal").exists()
    db.close_all()
    with open(dbpath, "r+b") as handle:
        handle.seek(100)
        handle.write(b"\x00" * 8192)
    db.integrity_check(dbpath)
    corrupt = next(iter(dbpath.parent.glob("state.db.corrupt-*")))
    # The fresh database must not inherit the old WAL.
    fresh_wal = dbpath.with_name(dbpath.name + "-wal")
    assert not fresh_wal.exists() or fresh_wal.stat().st_size == 0
    assert corrupt.exists()


# ═════════════════════════════════════════════════════════════════════════════
# vacuum_and_prune
# ═════════════════════════════════════════════════════════════════════════════

def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO accounts (id, remote, sync_root, added_at) "
                 "VALUES ('a','a','/tmp/a','2026-01-01T00:00:00Z')")


def test_prune_caps_activity_per_account(dbpath):
    conn = db.open_rw(dbpath)
    _seed(conn)
    conn.executemany(
        "INSERT INTO activity (account_id, rel_path, name, verb, state, "
        "started_at) VALUES ('a',?,?,'uploaded','done','2026-01-01T00:00:00Z')",
        [(f"f{n}", f"f{n}") for n in range(120)])
    removed = db.vacuum_and_prune(conn, activity_rows=50)
    assert removed["activity"] == 70
    assert conn.execute("SELECT count(*) FROM activity").fetchone()[0] == 50
    # the NEWEST rows survived (min(rel_path) would be a LEXICOGRAPHIC min:
    # "f100" < "f70", so compare on the rowid the table actually orders by)
    assert conn.execute("SELECT min(id), max(id) FROM activity").fetchone()[:] \
        == (71, 120)


def test_prune_drops_old_finished_runs(dbpath):
    conn = db.open_rw(dbpath)
    _seed(conn)
    conn.execute(
        "INSERT INTO runs (run_id, account_id, kind, argv, started_at, ended_at)"
        " VALUES ('old','a','bisync','[]', datetime('now','-40 days'),"
        " datetime('now','-40 days'))")
    conn.execute(
        "INSERT INTO runs (run_id, account_id, kind, argv, started_at, ended_at)"
        " VALUES ('new','a','bisync','[]', datetime('now'), datetime('now'))")
    conn.execute(
        "INSERT INTO runs (run_id, account_id, kind, argv, started_at)"
        " VALUES ('open','a','bisync','[]', datetime('now','-40 days'))")
    removed = db.vacuum_and_prune(conn, keep_logs_days=14)
    assert removed["runs"] == 1
    surviving = {row[0] for row in conn.execute("SELECT run_id FROM runs")}
    assert surviving == {"new", "open"}      # an unfinished run is never pruned


def test_prune_drops_superseded_cache_generations(dbpath):
    conn = db.open_rw(dbpath)
    _seed(conn)
    conn.executemany(
        "INSERT INTO cache_index (account_id, rel_path, state, scan_generation,"
        " updated_at) VALUES ('a',?,?,?,'t')",
        [("old1", "local", 1), ("old2", "local", 2), ("new", "local", 3)])
    removed = db.vacuum_and_prune(conn)
    assert removed["cache_index"] == 2
    assert [row[0] for row in
            conn.execute("SELECT rel_path FROM cache_index")] == ["new"]


def test_prune_drops_answered_decisions_and_expired_trash(dbpath):
    conn = db.open_rw(dbpath)
    _seed(conn)
    conn.execute(
        "INSERT INTO decisions (account_id, kind, payload, created_at,"
        " answered_at) VALUES ('a','mass_delete','{}', datetime('now','-60 days'),"
        " datetime('now','-60 days'))")
    conn.execute(
        "INSERT INTO decisions (account_id, kind, payload, created_at)"
        " VALUES ('a','mass_delete','{}', datetime('now','-60 days'))")
    conn.execute(
        "INSERT INTO trashbin (account_id, rel_path, trash_path, deleted_at,"
        " purge_after) VALUES ('a','f','t/f', datetime('now','-60 days'),"
        " datetime('now','-1 days'))")
    removed = db.vacuum_and_prune(conn, decision_days=30)
    assert removed["decisions"] == 1
    assert removed["trashbin"] == 1
    # An UNANSWERED decision is never pruned: expiry means "do not delete".
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


def test_prune_on_an_empty_database_reports_zeroes(dbpath):
    removed = db.vacuum_and_prune(db.open_rw(dbpath))
    assert set(removed) >= {"activity", "issues", "runs", "decisions",
                            "cache_index", "trashbin", "wal_pages"}
    assert all(v == 0 for k, v in removed.items() if k != "wal_pages")


def test_prune_can_vacuum(dbpath):
    db.vacuum_and_prune(db.open_rw(dbpath), vacuum=True)


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def test_schema_sql_is_readable_and_executes():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.schema_sql())
    assert "accounts" in db.table_names(conn)
    conn.close()


def test_a_path_with_a_question_mark_is_escaped_for_the_uri(tmp_path):
    """`?` starts a URI query and would silently truncate the path."""
    weird = tmp_path / "od?db"
    weird.mkdir()
    path = weird / "state.db"
    conn = db.open_rw(path)
    db.migrate(conn)
    conn.execute("INSERT INTO kv (account_id, key, value, updated_at) "
                 "VALUES ('','k','v','t')")
    db.close_rw(path)
    assert db.open_ro(path).execute("SELECT value FROM kv").fetchone()[0] == "v"
    db.close_ro(path)
