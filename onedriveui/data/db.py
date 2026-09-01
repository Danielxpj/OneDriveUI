"""SQLite connection factory and migration runner.

One database, ``~/.local/share/onedriveui/state.db``, opened under exactly one
set of pragmas:

===================  ========  ==================================================
``journal_mode``     ``WAL``   readers never block the writer, and vice versa
``synchronous``      ``NORMAL``  one fsync per checkpoint, not per commit
``busy_timeout``     ``5000``  a reader that catches a checkpoint waits, not fails
``foreign_keys``     ``ON``    ``ON DELETE CASCADE`` from ``accounts`` is real
===================  ========  ==================================================

``foreign_keys`` is per-connection and defaults to **off**: every table in the
schema cascades from ``accounts(id)``, so a connection that forgets the pragma
silently orphans rows instead of deleting them. That is why no caller opens
``sqlite3.connect`` directly.

**One writer, many readers.** :func:`open_rw` hands out the single read-write
connection and refuses to hand it to a second thread — that refusal is how
"no SQLite write outside ``DbWriter``" (ARCHITECTURE §7.6) is enforced in code
rather than in review. Readers call :func:`open_ro`, which is thread-local and
opens ``file:…?mode=ro``, so a query that tried to write would fail loudly.

**The database must not live on the FUSE mount.** SQLite's WAL needs
``mmap``-backed shared memory and POSIX locking, and rclone's FUSE mount
provides neither faithfully; a ``state.db`` there would corrupt under a
concurrent reader. :func:`open_rw` raises :class:`~onedriveui.errors.SafetyRefusal`
rather than trying.

**Corruption is recoverable.** This database is a derived index and a history:
every row can be rebuilt by rescanning ``vfsMeta/`` and re-reading rclone. So
:func:`integrity_check` renames a damaged file aside as ``state.db.corrupt-<ts>``
and starts a fresh one, instead of leaving the user with an application that
will not start.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Final, Iterable

from onedriveui import paths
from onedriveui.errors import SafetyRefusal
from onedriveui.models import utcnow_iso

__all__ = [
    "SCHEMA_VERSION", "open_rw", "open_ro", "close_rw", "close_ro", "close_all",
    "migrate", "integrity_check", "vacuum_and_prune", "schema_sql",
    "migration_files", "current_version", "apply_pragmas", "PRAGMAS",
    "SCHEMA_DIR", "MIGRATIONS_DIR", "CORRUPT_SUFFIX",
]

#: The version ``migrations/`` brings a fresh database to. Bumped by adding a
#: new ``00N_*.sql``, never by editing a shipped one.
SCHEMA_VERSION: Final[int] = 2

SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent
MIGRATIONS_DIR: Final[Path] = SCHEMA_DIR / "migrations"

#: ``state.db.corrupt-20260831T120000Z``
CORRUPT_SUFFIX: Final[str] = ".corrupt-"

#: Applied to every connection, in this order. ``journal_mode`` must run first
#: and outside a transaction; the rest are cheap per-connection settings.
PRAGMAS: Final[tuple[tuple[str, str], ...]] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
    ("foreign_keys", "ON"),
)

#: SQLite's sidecars. Renamed and removed alongside the database itself; a WAL
#: left behind next to a fresh database would be replayed into it.
_SIDECARS: Final[tuple[str, ...]] = ("-wal", "-shm", "-journal")

_MIGRATION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{3})_.*\.sql$")

#: The single read-write connection, per database path, with the thread that
#: owns it. A second thread asking for it is a bug, not a race to be won.
_RW: dict[str, tuple[sqlite3.Connection, int]] = {}
_RW_LOCK = threading.Lock()

#: Per-thread read-only connections, keyed by database path.
_RO = threading.local()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve(path: Path | str | None) -> Path:
    """Normalise a database path argument to an absolute :class:`Path`."""
    return Path(path) if path is not None else paths.db_file()


def _key(path: Path) -> str:
    """The dict key for a database path. Absolute, not symlink-resolved."""
    return os.path.abspath(str(path))


def schema_sql() -> str:
    """The frozen DDL from ``data/schema.sql``.

    Returns:
        The complete schema as text. Used to create a database in one shot in a
        test, and to prove the migration chain and the schema have not drifted
        apart; production databases are built by :func:`migrate`.
    """
    return (SCHEMA_DIR / "schema.sql").read_text(encoding="utf-8")


def migration_files() -> list[tuple[int, Path]]:
    """The migration scripts, in the order they must run.

    Returns:
        ``[(version, path), …]`` sorted ascending, where `version` is the
        integer prefix of the file name. A file whose name does not match
        ``NNN_*.sql`` is ignored rather than guessed at.
    """
    found: list[tuple[int, Path]] = []
    if not MIGRATIONS_DIR.is_dir():
        return found
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        match = _MIGRATION_RE.match(entry.name)
        if match and entry.is_file():
            found.append((int(match.group(1)), entry))
    found.sort(key=lambda pair: pair[0])
    return found


def apply_pragmas(conn: sqlite3.Connection, *, read_only: bool = False) -> None:
    """Apply :data:`PRAGMAS` to a connection.

    Args:
        conn: An open connection.
        read_only: Skip ``journal_mode``, which a ``mode=ro`` connection cannot
            set and does not need — the journal mode is a property of the
            database file, already set by the writer.
    """
    for name, value in PRAGMAS:
        if read_only and name == "journal_mode":
            continue
        conn.execute(f"PRAGMA {name} = {value}")


def _assert_not_on_fuse(path: Path) -> None:
    """Refuse a database path inside a ``fuse.rclone`` mount.

    Raises:
        SafetyRefusal: If the resolved path is at or under any live rclone
            mountpoint. WAL over FUSE loses the locking and shared-memory
            guarantees SQLite relies on, and the failure mode is silent
            corruption rather than an error.
    """
    if paths.is_under_fuse_mount(path):
        raise SafetyRefusal(
            "I2",
            f"refusing to open a SQLite database under a fuse.rclone mount: "
            f"{path}. WAL requires POSIX locking and shared memory that the "
            f"rclone mount does not provide.")


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    """Open one connection with the house pragmas applied."""
    if read_only:
        uri = f"file:{_uri_escape(path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0,
                               isolation_level=None, check_same_thread=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None,
                               check_same_thread=True)
    conn.row_factory = sqlite3.Row
    try:
        apply_pragmas(conn, read_only=read_only)
    except sqlite3.Error:
        conn.close()
        raise
    if not read_only:
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - exotic filesystem
            pass
    return conn


def _uri_escape(path: Path) -> str:
    """Escape a path for a SQLite ``file:`` URI.

    ``?`` and ``#`` start the query and fragment, and would silently truncate a
    path that contains them.
    """
    text = os.path.abspath(str(path))
    return (text.replace("?", "%3f").replace("#", "%23"))


# ─────────────────────────────────────────────────────────────────────────────
# Connections
# ─────────────────────────────────────────────────────────────────────────────

def open_rw(path: Path | str | None = None) -> sqlite3.Connection:
    """Return **the** read-write connection, creating the database if needed.

    There is exactly one per database path, and it belongs to the thread that
    first asked for it — in production, ``DbWriter``'s thread.

    Args:
        path: The database file. Defaults to :func:`onedriveui.paths.db_file`.

    Returns:
        The one read-write connection. Calling twice from the same thread
        returns the identical object.

    Raises:
        SafetyRefusal: If the path is under a ``fuse.rclone`` mount, or if a
            second thread asks for a connection that another thread already
            owns. The second case is ARCHITECTURE §7.6's "no SQLite write
            outside ``DbWriter``", enforced rather than reviewed.
        sqlite3.Error: If the database cannot be opened.
    """
    target = _resolve(path)
    _assert_not_on_fuse(target)
    key = _key(target)
    me = threading.get_ident()
    with _RW_LOCK:
        existing = _RW.get(key)
        if existing is not None:
            conn, owner = existing
            if owner != me:
                raise SafetyRefusal(
                    "DB-WRITER",
                    f"the read-write connection to {target} belongs to thread "
                    f"{owner}; thread {me} must submit through DbWriter and "
                    f"read through open_ro()")
            return conn
        conn = _connect(target, read_only=False)
        _RW[key] = (conn, me)
        return conn


def open_ro(path: Path | str | None = None) -> sqlite3.Connection:
    """Return this thread's read-only connection, opening it on first use.

    Read-only means ``mode=ro``: a query that tried to write raises
    ``sqlite3.OperationalError`` instead of racing the writer. WAL makes these
    reads safe against a concurrent writer with no locking at all, which is what
    lets the GUI thread query the database inside its 5 ms budget.

    Args:
        path: The database file. Defaults to :func:`onedriveui.paths.db_file`.

    Returns:
        A connection private to the calling thread.

    Raises:
        SafetyRefusal: If the path is under a ``fuse.rclone`` mount.
        sqlite3.Error: If the file does not exist — a read-only connection
            cannot create one, on purpose: silently creating an empty database
            would make a missing file look like an empty history.
    """
    target = _resolve(path)
    _assert_not_on_fuse(target)
    key = _key(target)
    cache: dict[str, sqlite3.Connection] = getattr(_RO, "conns", None)  # type: ignore[assignment]
    if cache is None:
        cache = {}
        _RO.conns = cache  # type: ignore[attr-defined]
    conn = cache.get(key)
    if conn is None:
        conn = _connect(target, read_only=True)
        cache[key] = conn
    return conn


def close_rw(path: Path | str | None = None) -> None:
    """Close and forget the read-write connection.

    Args:
        path: The database file, or ``None`` for the default.

    Idempotent, and safe to call from a thread other than the owner: shutdown
    ordering is not always the ordering that acquired the connection.
    """
    key = _key(_resolve(path))
    with _RW_LOCK:
        entry = _RW.pop(key, None)
    if entry is not None:
        try:
            entry[0].close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def close_ro(path: Path | str | None = None) -> None:
    """Close and forget this thread's read-only connection.

    Args:
        path: The database file, or ``None`` for every path this thread opened.
    """
    cache: dict[str, sqlite3.Connection] | None = getattr(_RO, "conns", None)
    if not cache:
        return
    keys = [_key(_resolve(path))] if path is not None else list(cache)
    for key in keys:
        conn = cache.pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - defensive
                pass


def close_all() -> None:
    """Close every connection this thread can reach.

    Read-only connections belonging to *other* threads are left alone — closing
    a connection from a foreign thread is undefined behaviour in SQLite, and
    those threads close their own on exit.
    """
    with _RW_LOCK:
        entries = list(_RW.items())
        _RW.clear()
    for _key_, (conn, _owner) in entries:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
    close_ro()


# ─────────────────────────────────────────────────────────────────────────────
# Migration
# ─────────────────────────────────────────────────────────────────────────────

def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return row is not None


def current_version(conn: sqlite3.Connection) -> int:
    """The schema version recorded in ``schema_meta``.

    Args:
        conn: Any open connection.

    Returns:
        The stored version, or 0 for a database that has never been migrated
        (including a brand new, empty one).
    """
    if not _has_table(conn, "schema_meta"):
        return 0
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """Bring a database up to :data:`SCHEMA_VERSION`.

    Each pending ``migrations/NNN_*.sql`` runs inside its own transaction and
    stamps ``schema_meta.schema_version`` on success, so an interrupted upgrade
    leaves the database at the last version that completed rather than half way
    through one.

    Args:
        conn: A read-write connection, from :func:`open_rw`.

    Returns:
        The version the database is now at.

    Raises:
        sqlite3.Error: If a migration fails. The transaction is rolled back
            first, so the database keeps the version it had.
    """
    version = current_version(conn)
    for target_version, script in migration_files():
        if target_version <= version:
            continue
        sql = script.read_text(encoding="utf-8")
        # The BEGIN/COMMIT must be *inside* the script: executescript() issues
        # an implicit COMMIT before it runs, so a transaction opened around the
        # call would be closed before the first CREATE TABLE ever executed.
        _run_script(conn, [sql, _stamp_sql(target_version)])
        version = target_version
    return version


def _stamp_sql(version: int) -> str:
    """The statement that records a completed migration."""
    return ("INSERT OR REPLACE INTO schema_meta (key, value) "
            f"VALUES ('schema_version', '{int(version)}');")


def _run_script(conn: sqlite3.Connection, scripts: Iterable[str]) -> None:
    """Run SQL scripts as one all-or-nothing transaction.

    Args:
        conn: A read-write connection in autocommit mode.
        scripts: SQL text, concatenated and executed in order.

    Raises:
        sqlite3.Error: After rolling back, leaving the database exactly as it
            was. SQLite's DDL is transactional, so a half-applied migration is
            impossible as long as the BEGIN travels inside the script.
    """
    body = "\n".join(scripts)
    try:
        conn.executescript(f"BEGIN;\n{body}\nCOMMIT;")
    except sqlite3.Error:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - already rolled back
                pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Integrity
# ─────────────────────────────────────────────────────────────────────────────

def _corrupt_name(path: Path) -> Path:
    """``state.db`` -> ``state.db.corrupt-20260831T120000Z``, never colliding."""
    stamp = utcnow_iso().replace("-", "").replace(":", "")
    candidate = path.with_name(f"{path.name}{CORRUPT_SUFFIX}{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}{CORRUPT_SUFFIX}{stamp}-{counter}")
        counter += 1
    return candidate


def _move_aside(path: Path) -> Path:
    """Rename a database and its sidecars out of the way.

    Args:
        path: The database file.

    Returns:
        The new name of the database file.
    """
    target = _corrupt_name(path)
    try:
        os.replace(path, target)
    except OSError:
        # The file may already be gone, or be a directory; either way the
        # recreate below is still the right next step.
        pass
    for suffix in _SIDECARS:
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            try:
                os.replace(sidecar, target.with_name(target.name + suffix))
            except OSError:  # pragma: no cover - defensive
                sidecar.unlink(missing_ok=True)
    return target


def integrity_check(path: Path | str | None = None) -> bool:
    """Verify the database, renaming and recreating it if it is damaged.

    Run once at startup, before ``DbWriter`` opens its connection.

    Args:
        path: The database file. Defaults to :func:`onedriveui.paths.db_file`.

    Returns:
        True if the existing database passed (or did not exist yet, in which
        case a fresh, migrated one is left behind). False if it was corrupt: it
        has been renamed to ``state.db.corrupt-<timestamp>`` — kept, not
        deleted, so it can be examined — and a new, empty, migrated database is
        in its place.

    Raises:
        SafetyRefusal: If the path is under a ``fuse.rclone`` mount.

    Losing this database costs a cache rescan and the activity history. Refusing
    to start costs the user their sync client, so the trade is not close.
    """
    target = _resolve(path)
    _assert_not_on_fuse(target)
    close_rw(target)
    close_ro(target)

    healthy = True
    if target.exists():
        try:
            probe = _connect(target, read_only=False)
        except sqlite3.Error:
            healthy = False
        else:
            try:
                row = probe.execute("PRAGMA integrity_check").fetchone()
                healthy = bool(row) and str(row[0]).lower() == "ok"
                if healthy:
                    # A file can pass integrity_check and still be unusable if a
                    # migration was interrupted; reading the version proves the
                    # catalogue is readable too.
                    current_version(probe)
            except sqlite3.Error:
                healthy = False
            finally:
                probe.close()
        if not healthy:
            _move_aside(target)

    conn = open_rw(target)
    migrate(conn)
    close_rw(target)
    return healthy


# ─────────────────────────────────────────────────────────────────────────────
# Housekeeping
# ─────────────────────────────────────────────────────────────────────────────

def vacuum_and_prune(
    conn: sqlite3.Connection,
    *,
    keep_logs_days: int = 14,
    activity_rows: int = 5_000,
    issue_rows: int = 5_000,
    decision_days: int = 30,
    vacuum: bool = False,
) -> dict[str, int]:
    """The hourly prune from ARCHITECTURE §10.

    Args:
        conn: The read-write connection. **Call this from the ``DbWriter``
            thread only** — it writes.
        keep_logs_days: Drop ``runs`` older than this.
        activity_rows: Keep at most this many ``activity`` rows per account.
        issue_rows: Keep at most this many resolved ``issues`` per account.
        decision_days: Drop answered ``decisions`` older than this.
        vacuum: Also run ``VACUUM``. Off by default: it rewrites the whole file
            and holds an exclusive lock, which is too expensive for an hourly
            timer. The WAL is truncated either way.

    Returns:
        A count of the rows removed per table, plus ``"wal_pages"`` for the
        pages the checkpoint reclaimed.

    ``cache_index`` is pruned by *superseded generation*: every row whose
    ``scan_generation`` is below the newest generation that account has, which
    is the same rule :func:`~onedriveui.data.repo_files.prune_cache_generation`
    applies at the end of a completed scan.
    """
    removed: dict[str, int] = {}
    # Only own the transaction when there is not one already. `DbWriter` wraps
    # every batch in `BEGIN IMMEDIATE`, so a prune submitted to it would raise
    # "cannot start a transaction within a transaction" — which is why the
    # hourly prune of ARCHITECTURE §10 had no caller at all and the activity,
    # issues and runs tables grew without limit for the life of the install.
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "DELETE FROM activity WHERE id NOT IN ("
            "  SELECT id FROM activity a2"
            "  WHERE a2.account_id = activity.account_id"
            "  ORDER BY a2.id DESC LIMIT ?)", (int(activity_rows),))
        removed["activity"] = cur.rowcount if cur.rowcount > 0 else 0

        cur = conn.execute(
            "DELETE FROM issues WHERE resolved_at IS NOT NULL AND id NOT IN ("
            "  SELECT id FROM issues i2"
            "  WHERE i2.account_id = issues.account_id"
            "    AND i2.resolved_at IS NOT NULL"
            "  ORDER BY i2.id DESC LIMIT ?)", (int(issue_rows),))
        removed["issues"] = cur.rowcount if cur.rowcount > 0 else 0

        cur = conn.execute(
            "DELETE FROM runs WHERE ended_at IS NOT NULL AND started_at < "
            "datetime('now', ?)", (f"-{int(keep_logs_days)} days",))
        removed["runs"] = cur.rowcount if cur.rowcount > 0 else 0

        cur = conn.execute(
            "DELETE FROM decisions WHERE answered_at IS NOT NULL AND "
            "answered_at < datetime('now', ?)", (f"-{int(decision_days)} days",))
        removed["decisions"] = cur.rowcount if cur.rowcount > 0 else 0

        cur = conn.execute(
            "DELETE FROM cache_index WHERE scan_generation < ("
            "  SELECT MAX(c2.scan_generation) FROM cache_index c2"
            "  WHERE c2.account_id = cache_index.account_id)")
        removed["cache_index"] = cur.rowcount if cur.rowcount > 0 else 0

        cur = conn.execute(
            "DELETE FROM trashbin WHERE restored_at IS NULL AND "
            "purge_after < datetime('now')")
        removed["trashbin"] = cur.rowcount if cur.rowcount > 0 else 0
        if owns_transaction:
            conn.execute("COMMIT")
    except sqlite3.Error:
        # Only roll back a transaction we opened. Rolling back a caller's would
        # discard writes that have nothing to do with this prune — the writer
        # batches many operations into one transaction.
        if owns_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - already rolled back
                pass
        raise

    if not owns_transaction:
        # A checkpoint or a VACUUM inside someone else's open transaction either
        # fails or blocks. When we are a guest, the pruning DELETEs are the
        # whole contribution; the writer commits, and the next standalone run
        # truncates the WAL.
        removed["wal_pages"] = 0
        return removed

    try:
        conn.execute("PRAGMA optimize")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        removed["wal_pages"] = int(row[1]) if row and row[1] is not None else 0
    except sqlite3.Error:  # pragma: no cover - checkpoint contention
        removed["wal_pages"] = 0
    if vacuum:
        conn.execute("VACUUM")
    return removed


def table_names(conn: sqlite3.Connection) -> list[str]:
    """Every user table in a database, sorted.

    Args:
        conn: Any open connection.

    Returns:
        Table names, excluding SQLite's own ``sqlite_*`` bookkeeping.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    return [str(row[0]) for row in rows]


def catalogue(conn: sqlite3.Connection) -> dict[str, str]:
    """The normalised ``sqlite_master`` catalogue, for drift checks.

    Args:
        conn: Any open connection.

    Returns:
        ``{f"{type}:{name}": normalised_sql}`` for every table, index and
        trigger, with whitespace collapsed and SQLite's auto-created indexes
        excluded — enough to prove that the migration chain and ``schema.sql``
        describe the same database.
    """
    out: dict[str, str] = {}
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%'")
    for kind, name, sql in rows:
        if sql is None:                    # an implicit index from UNIQUE
            continue
        out[f"{kind}:{name}"] = re.sub(r"\s+", " ", str(sql)).strip()
    return out


def executescript_all(conn: sqlite3.Connection, statements: Iterable[str]) -> None:
    """Run several SQL scripts as one all-or-nothing transaction.

    Args:
        conn: A read-write connection.
        statements: SQL scripts, executed in order.

    Raises:
        sqlite3.Error: On the first failure, after a rollback.
    """
    _run_script(conn, statements)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a :class:`sqlite3.Row` to a plain dict.

    Args:
        row: A row, or ``None``.

    Returns:
        A dict of the row's columns, or ``None``.
    """
    return dict(row) if row is not None else None
