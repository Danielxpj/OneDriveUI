"""Repository for the per-file tables.

Owns ``pins``, ``cache_index``, ``versions``, ``trashbin``, ``share_links``,
``notifications``, ``kv``, ``folder_selection``, ``kfm_folder`` and
``dialog_seen``.

Every row here is keyed by ``rel_path``: POSIX, relative to the account's sync
root, no leading slash. **Never an inode** — rclone's inode numbers are not
stable across remounts, and both xattrs and ``gio`` metadata fail outright on
the rclone FUSE mount, so the path is the only identifier that survives.

The interesting mechanism in this module is the **generation-based**
``cache_index`` upsert. A full cache scan walks thousands of ``vfsMeta/``
sidecars and takes seconds; deleting the account's rows first and re-inserting
would leave the Nautilus extension answering "unknown" for every file for the
whole of that window, and would lose the lot outright if the scan were
interrupted half way. Instead:

1. the scan picks a new generation number ``N``;
2. :func:`upsert_cache_rows` writes each row it observes with
   ``scan_generation = N``, **updating** the existing row for that path;
3. only when the walk has completed does the scanner call
   :func:`prune_cache_generation` with ``N - 1``, which deletes every row no
   longer at or above the current generation — the paths that have genuinely
   disappeared.

An interrupted scan therefore never calls step 3, and every row it did not
reach keeps its previous, still-correct value. That is the whole design, and it
is why the pruning argument is "everything at or below this", not "this one".
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from onedriveui.constants import (
    TRASH_RETENTION_DAYS_BUSINESS, TRASH_RETENTION_DAYS_PERSONAL,
)
from onedriveui.data import db
from onedriveui.data.writer import WRITER, DbWriter
from onedriveui.models import (
    CacheEntry, FileState, FileStatus, KfmFolder, LinkScope, LinkType,
    PinRecord, ShareLink, TrashEntry, VersionEntry, parse_iso, utcnow_iso,
)

__all__ = [
    "set_pin", "clear_pin", "pins", "pin_for", "unsatisfied_pins",
    "mark_pin_satisfied", "next_cache_generation",
    "upsert_cache_rows", "prune_cache_generation", "cache_generation",
    "file_state", "file_states", "dirty_paths", "cache_counts",
    "add_version", "versions_for",
    "add_trash", "trash_items", "mark_restored", "purge_due",
    "record_link", "links_for", "revoke_link",
    "note_notification", "should_show", "suppress_notification",
    "kv_get", "kv_set", "kv_delete",
    "selection", "set_selection", "excluded_paths",
    "set_kfm_folder", "kfm_folders",
    "dialog_seen", "mark_dialog_seen",
    "PIN_MODES", "TRASH_RETENTION_DAYS",
]

#: ``pins.mode``. ``auto`` means "rclone's LRU may evict this freely".
PIN_MODES: tuple[str, ...] = ("pinned", "online_only", "auto")

#: How long our own recycle bin keeps an item, by drive kind.
TRASH_RETENTION_DAYS: dict[str, int] = {
    "personal": TRASH_RETENTION_DAYS_PERSONAL,
    "business": TRASH_RETENTION_DAYS_BUSINESS,
}


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _w(writer: DbWriter | None) -> DbWriter:
    """The writer to submit to. Explicit beats the singleton, for tests."""
    return writer if writer is not None else WRITER


def _ro(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    """This thread's read-only connection, unless one was supplied."""
    return conn if conn is not None else db.open_ro()


def _enum(cls: type, value: Any, fallback: Any) -> Any:
    """Coerce a stored string back to its enum, defaulting on an unknown one."""
    if value is None:
        return fallback
    try:
        return cls(value)
    except ValueError:
        return fallback


def _chunks(items: Sequence[Any], size: int = 400) -> Iterable[Sequence[Any]]:
    """Split a sequence so no query exceeds SQLite's parameter limit.

    SQLite's default ``SQLITE_MAX_VARIABLE_NUMBER`` is generous but finite, and
    the Nautilus extension can legitimately ask about a whole directory at once.
    """
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ─────────────────────────────────────────────────────────────────────────────
# pins
# ─────────────────────────────────────────────────────────────────────────────

_PIN_COLUMNS = (
    "account_id, rel_path, mode, is_dir, requested_at, satisfied_at, "
    "bytes_total, bytes_local, last_error, generation")


def _pin_from_row(row: sqlite3.Row) -> PinRecord:
    """Build a :class:`~onedriveui.models.PinRecord` from a table row."""
    return PinRecord(
        account_id=str(row["account_id"]),
        rel_path=str(row["rel_path"]),
        mode=str(row["mode"]),
        is_dir=bool(row["is_dir"]),
        requested_at=str(row["requested_at"] or ""),
        satisfied_at=row["satisfied_at"],
        bytes_total=int(row["bytes_total"] or 0),
        bytes_local=int(row["bytes_local"] or 0),
        last_error=row["last_error"],
        generation=int(row["generation"] or 0),
    )


def set_pin(
    account_id: str,
    rel_path: str,
    mode: str = "pinned",
    *,
    is_dir: bool = False,
    bytes_total: int | None = None,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> None:
    """Record the user's intent for one path.

    rclone has **no pin concept** and its LRU evictor ignores us entirely, so
    this table is authoritative: when an eviction is detected the pinner replays
    it and re-hydrates. Changing the mode resets ``satisfied_at``, because the
    new intent has not been satisfied yet.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        mode: One of :data:`PIN_MODES`.
        is_dir: Whether the path is a directory — a pinned directory means
            "keep everything under it".
        bytes_total: The expected size, for the progress bar.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Raises:
        ValueError: If `mode` is not one of :data:`PIN_MODES`.
    """
    if mode not in PIN_MODES:
        raise ValueError(f"pin mode must be one of {PIN_MODES}, not {mode!r}")
    sql = ("INSERT INTO pins (account_id, rel_path, mode, is_dir, "
           "requested_at, bytes_total, generation) VALUES (?,?,?,?,?,?,0) "
           "ON CONFLICT(account_id, rel_path) DO UPDATE SET "
           "  mode = excluded.mode,"
           "  is_dir = excluded.is_dir,"
           "  requested_at = excluded.requested_at,"
           "  bytes_total = COALESCE(excluded.bytes_total, pins.bytes_total),"
           "  satisfied_at = CASE WHEN pins.mode = excluded.mode "
           "                      THEN pins.satisfied_at ELSE NULL END,"
           "  last_error = NULL,"
           "  generation = pins.generation + 1")
    params = (account_id, rel_path, mode, int(is_dir), utcnow_iso(),
              bytes_total)
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=False, label="set_pin")


def clear_pin(
    account_id: str,
    rel_path: str,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Forget an explicit intent, returning the path to rclone's LRU.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when a row was removed.
    """
    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(
            "DELETE FROM pins WHERE account_id = ? AND rel_path = ?",
            (account_id, rel_path)).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                       label="clear_pin"))


def mark_pin_satisfied(
    account_id: str,
    rel_path: str,
    *,
    bytes_local: int | None = None,
    error: str | None = None,
    writer: DbWriter | None = None,
) -> None:
    """Record the outcome of a hydration.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        bytes_local: Bytes now present locally.
        error: The failure text, if it failed. Passing an error leaves
            ``satisfied_at`` NULL so the pinner retries.
        writer: The writer to submit to.
    """
    if error is not None:
        sql = ("UPDATE pins SET last_error = ?, bytes_local = COALESCE(?, "
               "bytes_local) WHERE account_id = ? AND rel_path = ?")
        params: tuple[Any, ...] = (error, bytes_local, account_id, rel_path)
    else:
        sql = ("UPDATE pins SET satisfied_at = ?, last_error = NULL, "
               "bytes_local = COALESCE(?, bytes_local) "
               "WHERE account_id = ? AND rel_path = ?")
        params = (utcnow_iso(), bytes_local, account_id, rel_path)
    _w(writer).submit(lambda conn: conn.execute(sql, params),
                      label="mark_pin_satisfied")


def pins(
    account_id: str,
    *,
    mode: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[PinRecord]:
    """Every recorded intent for an account.

    Args:
        account_id: The account.
        mode: Restrict to one of :data:`PIN_MODES`.
        conn: A connection to read through.

    Returns:
        Pin records, in path order.
    """
    sql = f"SELECT {_PIN_COLUMNS} FROM pins WHERE account_id = ?"
    params: list[Any] = [account_id]
    if mode is not None:
        sql += " AND mode = ?"
        params.append(mode)
    sql += " ORDER BY rel_path"
    return [_pin_from_row(row) for row in _ro(conn).execute(sql, params)]


def pin_for(
    account_id: str,
    rel_path: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> PinRecord | None:
    """The intent recorded for one path, if any.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        conn: A connection to read through.

    Returns:
        The pin record, or ``None``.
    """
    row = _ro(conn).execute(
        f"SELECT {_PIN_COLUMNS} FROM pins WHERE account_id = ? AND rel_path = ?",
        (account_id, rel_path)).fetchone()
    return _pin_from_row(row) if row is not None else None


def unsatisfied_pins(
    account_id: str,
    *,
    limit: int = 5_000,
    conn: sqlite3.Connection | None = None,
) -> list[PinRecord]:
    """Pinned paths that are not yet fully local.

    This is the pinner's work queue, and the reason it survives a restart: a
    pin requested before a crash is still unsatisfied afterwards and is picked
    up again automatically.

    Args:
        account_id: The account.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Pin records, oldest request first. Served by the partial index
        ``ix_pins_todo``.
    """
    rows = _ro(conn).execute(
        f"SELECT {_PIN_COLUMNS} FROM pins WHERE account_id = ? "
        "AND mode = 'pinned' AND satisfied_at IS NULL "
        "ORDER BY requested_at ASC LIMIT ?", (account_id, int(limit)))
    return [_pin_from_row(row) for row in rows]


# ─────────────────────────────────────────────────────────────────────────────
# cache_index — the generation protocol
# ─────────────────────────────────────────────────────────────────────────────

_CACHE_UPSERT = (
    "INSERT INTO cache_index (account_id, rel_path, state, size, bytes_local, "
    "dirty, shared, atime, mtime, fingerprint, scan_generation, updated_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(account_id, rel_path) DO UPDATE SET "
    "  state = excluded.state,"
    "  size = excluded.size,"
    "  bytes_local = excluded.bytes_local,"
    "  dirty = excluded.dirty,"
    "  shared = excluded.shared,"
    "  atime = excluded.atime,"
    "  mtime = excluded.mtime,"
    "  fingerprint = excluded.fingerprint,"
    "  scan_generation = excluded.scan_generation,"
    "  updated_at = excluded.updated_at")


def cache_generation(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """The newest generation recorded for an account.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        The highest ``scan_generation``, or 0 when the account has no rows.
    """
    row = _ro(conn).execute(
        "SELECT MAX(scan_generation) FROM cache_index WHERE account_id = ?",
        (account_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def next_cache_generation(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """The generation number a new scan should claim.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        :func:`cache_generation` plus one. Monotonic per account, and read
        rather than stored so an interrupted scan's partially written
        generation is never reused.
    """
    return cache_generation(account_id, conn=conn) + 1


def upsert_cache_rows(
    account_id: str,
    rows: Iterable[CacheEntry | Mapping[str, Any]],
    generation: int,
    *,
    shared_paths: set[str] | None = None,
    writer: DbWriter | None = None,
    sync: bool = False,
    timeout_ms: int = 30_000,
) -> int:
    """Write one slice of a cache scan at generation `generation`.

    Each row **updates** the existing row for its path rather than replacing the
    account's rows wholesale, so a scan that is interrupted leaves every path it
    did not reach exactly as it was. Nothing is deleted here; deletion is
    :func:`prune_cache_generation`'s job and only ever runs after a scan
    completes.

    Args:
        account_id: The account.
        rows: :class:`~onedriveui.models.CacheEntry` objects, or mappings with
            the same keys.
        generation: The scan generation, from :func:`next_cache_generation`.
        shared_paths: Paths known to be shared, so the emblem survives a rescan
            that has no sharing information of its own.
        writer: The writer to submit to.
        sync: Wait for the commit. A scanner slicing a large tree leaves this
            off and flushes once at the end.
        timeout_ms: How long to wait when `sync`.

    Returns:
        How many rows were submitted.
    """
    gen = int(generation)
    now = utcnow_iso()
    shared = shared_paths or set()
    payload: list[tuple[Any, ...]] = []
    for entry in rows:
        if isinstance(entry, CacheEntry):
            rel_path = entry.rel_path
            state = str(entry.state)
            size = int(entry.size)
            bytes_local = int(entry.bytes_local)
            dirty = int(entry.dirty)
            atime = entry.atime
            mtime = entry.mtime
            fingerprint = entry.fingerprint
        else:
            rel_path = str(entry["rel_path"])
            state = str(entry.get("state", FileState.UNKNOWN))
            size = int(entry.get("size", 0) or 0)
            bytes_local = int(entry.get("bytes_local", 0) or 0)
            dirty = int(bool(entry.get("dirty", False)))
            atime = entry.get("atime")
            mtime = entry.get("mtime")
            fingerprint = str(entry.get("fingerprint", "") or "")
        payload.append((account_id, rel_path, state, size, bytes_local, dirty,
                        int(rel_path in shared), atime, mtime, fingerprint,
                        gen, now))
    if not payload:
        return 0

    def op(conn: sqlite3.Connection) -> int:
        conn.executemany(_CACHE_UPSERT, payload)
        return len(payload)

    if sync:
        return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                          label="upsert_cache_rows") or 0)
    _w(writer).submit(op, label="upsert_cache_rows")
    return len(payload)


def prune_cache_generation(
    account_id: str,
    generation: int,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 30_000,
) -> int:
    """Delete every cache row a completed scan superseded.

    **Call this only when a full scan finished.** It removes every row for the
    account whose ``scan_generation`` is at or below `generation` — the paths
    the scan did not observe, which are the ones that have genuinely gone. An
    interrupted scan must not call it: its unvisited rows are still the best
    information available.

    Args:
        account_id: The account.
        generation: The last superseded generation. A scan that wrote
            generation ``N`` passes ``N - 1``, which leaves only its own rows.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        How many rows were removed.
    """
    sql = ("DELETE FROM cache_index WHERE account_id = ? "
           "AND scan_generation <= ?")

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, (account_id, int(generation))).rowcount)

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="prune_cache_generation") or 0)


def _status_from_row(
    row: sqlite3.Row | None,
    rel_path: str,
    *,
    pinned: bool,
    has_error: bool,
) -> FileStatus:
    """Fold a cache row, the pin table and the issue table into one status.

    A path with no cache row is ``UNKNOWN``, not ``ONLINE_ONLY``: the Nautilus
    extension must be able to tell "not scanned yet" from "scanned, and it is
    online-only", because only the second is safe to render as a cloud emblem.
    """
    if row is None:
        return FileStatus(rel_path=rel_path, state=FileState.UNKNOWN,
                          pinned=pinned, has_error=has_error)
    state = _enum(FileState, row["state"], FileState.UNKNOWN)
    if pinned and state is FileState.LOCAL:
        state = FileState.PINNED
    return FileStatus(
        rel_path=rel_path,
        state=state,
        size=int(row["size"] or 0),
        bytes_local=int(row["bytes_local"] or 0),
        pinned=pinned,
        shared=bool(row["shared"]),
        has_error=has_error,
        excluded=state is FileState.EXCLUDED,
    )


def file_state(
    account_id: str,
    rel_path: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> FileStatus:
    """The rendered status of one path.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        conn: A connection to read through.

    Returns:
        A :class:`~onedriveui.models.FileStatus`, always — a path this process
        has never heard of comes back ``UNKNOWN`` rather than raising, because
        this answers the Nautilus IPC on a 20 ms budget and an exception there
        would stall the file manager's UI thread.
    """
    return file_states(account_id, [rel_path], conn=conn)[rel_path]


def file_states(
    account_id: str,
    rel_paths: Sequence[str],
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, FileStatus]:
    """The rendered status of many paths, in three indexed queries.

    Args:
        account_id: The account.
        rel_paths: The paths to look up.
        conn: A connection to read through.

    Returns:
        A status for **every** requested path, in the order asked. Three queries
        rather than three per path: the file manager asks about a whole
        directory at once, and a per-path round trip would blow the IPC budget.
    """
    wanted = list(dict.fromkeys(str(p) for p in rel_paths))
    if not wanted:
        return {}
    read = _ro(conn)

    cache: dict[str, sqlite3.Row] = {}
    pinned: set[str] = set()
    errored: set[str] = set()
    for chunk in _chunks(wanted):
        marks = ",".join("?" * len(chunk))
        for row in read.execute(
                "SELECT rel_path, state, size, bytes_local, dirty, shared "
                f"FROM cache_index WHERE account_id = ? AND rel_path IN ({marks})",
                (account_id, *chunk)):
            cache[str(row["rel_path"])] = row
        for row in read.execute(
                "SELECT rel_path FROM pins WHERE account_id = ? "
                f"AND mode = 'pinned' AND rel_path IN ({marks})",
                (account_id, *chunk)):
            pinned.add(str(row[0]))
        for row in read.execute(
                "SELECT rel_path FROM issues WHERE account_id = ? "
                f"AND resolved_at IS NULL AND rel_path IN ({marks})",
                (account_id, *chunk)):
            errored.add(str(row[0]))

    return {
        path: _status_from_row(cache.get(path), path,
                               pinned=path in pinned, has_error=path in errored)
        for path in wanted
    }


def dirty_paths(
    account_id: str,
    *,
    limit: int = 5_000,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Paths whose sidecar says ``Dirty: true``.

    A dirty cache item is an un-uploaded local change that exists **nowhere
    else**. Invariant I3 forbids evicting, force-unmounting around, or
    bisync'ing one, so this list is a safety input, not a display list.

    Args:
        account_id: The account.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Paths, in path order. Served by the partial index ``ix_cache_dirty``.
    """
    rows = _ro(conn).execute(
        "SELECT rel_path FROM cache_index WHERE account_id = ? AND dirty = 1 "
        "ORDER BY rel_path LIMIT ?", (account_id, int(limit)))
    return [str(row[0]) for row in rows]


def cache_counts(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """How many cached files are in each state.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        A count for **every** :class:`~onedriveui.models.FileState` plus
        ``"total"`` and ``"bytes_local"``, so a caller never guards a key.
    """
    counts = {str(state): 0 for state in FileState}
    total = 0
    for row in _ro(conn).execute(
            "SELECT state, count(*) AS n, SUM(bytes_local) AS b "
            "FROM cache_index WHERE account_id = ? GROUP BY state",
            (account_id,)):
        counts[str(row["state"])] = int(row["n"])
        total += int(row["n"])
    counts["total"] = total
    row = _ro(conn).execute(
        "SELECT SUM(bytes_local) FROM cache_index WHERE account_id = ?",
        (account_id,)).fetchone()
    counts["bytes_local"] = int(row[0]) if row and row[0] is not None else 0
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# versions
# ─────────────────────────────────────────────────────────────────────────────

def add_version(
    entry: VersionEntry,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Record a snapshot we took before overwriting or deleting a file.

    OneDrive Personal cannot delete server-side versions at all, so this table
    is our own honest version history: the row points at a real copy under
    ``versions/<account>/`` or ``onedrive:.onedriveui-versions/``.

    Args:
        entry: The version. ``captured_at`` defaults to now.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The new row id.
    """
    sql = ("INSERT INTO versions (account_id, rel_path, backup_path, side, "
           "captured_at, size, quickxor, reason, run_id) "
           "VALUES (?,?,?,?,?,?,?,?,?) RETURNING id")
    params = (entry.account_id, entry.rel_path, entry.backup_path, entry.side,
              entry.captured_at or utcnow_iso(), int(entry.size),
              entry.quickxor, entry.reason, entry.run_id)

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="add_version"))


def versions_for(
    account_id: str,
    rel_path: str,
    *,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[VersionEntry]:
    """Every snapshot of one path, newest first.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Version entries, newest first. Served by ``ix_versions_path``.
    """
    rows = _ro(conn).execute(
        "SELECT id, account_id, rel_path, backup_path, side, captured_at, "
        "size, quickxor, reason, run_id FROM versions "
        "WHERE account_id = ? AND rel_path = ? "
        "ORDER BY captured_at DESC, id DESC LIMIT ?",
        (account_id, rel_path, int(limit)))
    return [
        VersionEntry(
            id=int(row["id"]), account_id=str(row["account_id"]),
            rel_path=str(row["rel_path"]), backup_path=str(row["backup_path"]),
            side=str(row["side"]), captured_at=str(row["captured_at"] or ""),
            size=int(row["size"] or 0), quickxor=str(row["quickxor"] or ""),
            reason=str(row["reason"] or ""), run_id=str(row["run_id"] or ""),
        )
        for row in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# trashbin
# ─────────────────────────────────────────────────────────────────────────────

def add_trash(
    entry: TrashEntry,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Record an item we moved to our own recycle bin.

    Only ever for deletions made **through our UI**. A delete through the mount
    lands in Microsoft's own cloud recycle bin and is none of our business.

    Args:
        entry: The trashed item. ``deleted_at`` defaults to now.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The new row id.
    """
    sql = ("INSERT INTO trashbin (account_id, rel_path, trash_path, is_dir, "
           "size, deleted_at, purge_after) VALUES (?,?,?,?,?,?,?) RETURNING id")
    params = (entry.account_id, entry.rel_path, entry.trash_path,
              int(entry.is_dir), int(entry.size),
              entry.deleted_at or utcnow_iso(), entry.purge_after)

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="add_trash"))


def trash_items(
    account_id: str,
    *,
    include_restored: bool = False,
    limit: int = 500,
    conn: sqlite3.Connection | None = None,
) -> list[TrashEntry]:
    """Items in our recycle bin, newest first.

    Args:
        account_id: The account.
        include_restored: Include items already put back.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Trash entries. Served by ``ix_trash_recent``.
    """
    sql = ("SELECT id, account_id, rel_path, trash_path, is_dir, size, "
           "deleted_at, purge_after, restored_at FROM trashbin "
           "WHERE account_id = ?")
    if not include_restored:
        sql += " AND restored_at IS NULL"
    sql += " ORDER BY deleted_at DESC, id DESC LIMIT ?"
    rows = _ro(conn).execute(sql, (account_id, int(limit)))
    return [
        TrashEntry(
            id=int(row["id"]), account_id=str(row["account_id"]),
            rel_path=str(row["rel_path"]), trash_path=str(row["trash_path"]),
            is_dir=bool(row["is_dir"]), size=int(row["size"] or 0),
            deleted_at=str(row["deleted_at"] or ""),
            purge_after=str(row["purge_after"] or ""),
            restored_at=row["restored_at"],
        )
        for row in rows
    ]


def mark_restored(
    trash_id: int,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Mark a trashed item as put back.

    Args:
        trash_id: The row to mark.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when a row was marked; False when it was already restored.
    """
    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(
            "UPDATE trashbin SET restored_at = ? "
            "WHERE id = ? AND restored_at IS NULL",
            (utcnow_iso(), int(trash_id))).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                       label="mark_restored"))


def purge_due(
    account_id: str,
    *,
    now: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[TrashEntry]:
    """Trashed items whose retention window has expired.

    Args:
        account_id: The account.
        now: The instant to compare against. Defaults to now.
        conn: A connection to read through.

    Returns:
        Entries the purge task may delete for real. Returned rather than
        deleted here: removing the row before the bytes are gone would lose
        track of a file that still exists on disk.
    """
    stamp = now or utcnow_iso()
    rows = _ro(conn).execute(
        "SELECT id, account_id, rel_path, trash_path, is_dir, size, "
        "deleted_at, purge_after, restored_at FROM trashbin "
        "WHERE account_id = ? AND restored_at IS NULL AND purge_after <= ? "
        "ORDER BY purge_after", (account_id, stamp))
    return [
        TrashEntry(
            id=int(row["id"]), account_id=str(row["account_id"]),
            rel_path=str(row["rel_path"]), trash_path=str(row["trash_path"]),
            is_dir=bool(row["is_dir"]), size=int(row["size"] or 0),
            deleted_at=str(row["deleted_at"] or ""),
            purge_after=str(row["purge_after"] or ""),
            restored_at=row["restored_at"],
        )
        for row in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# share_links
# ─────────────────────────────────────────────────────────────────────────────

def record_link(
    link: ShareLink,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Remember a link we created.

    Args:
        link: The share link. ``created_at`` defaults to now.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The new row id.
    """
    sql = ("INSERT INTO share_links (account_id, rel_path, url, scope, "
           "link_type, has_password, expires_at, created_at) "
           "VALUES (?,?,?,?,?,?,?,?) RETURNING id")
    params = (link.account_id, link.rel_path, link.url, str(link.scope),
              str(link.link_type), int(link.has_password), link.expires_at,
              link.created_at or utcnow_iso())

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="record_link"))


def links_for(
    account_id: str,
    rel_path: str | None = None,
    *,
    include_revoked: bool = False,
    limit: int = 200,
    conn: sqlite3.Connection | None = None,
) -> list[ShareLink]:
    """Links created for a path, or for the whole account.

    Args:
        account_id: The account.
        rel_path: Restrict to one path, or ``None`` for all.
        include_revoked: Include links marked revoked.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Share links, newest first. Served by ``ix_share_path``.
    """
    sql = ("SELECT id, account_id, rel_path, url, scope, link_type, "
           "has_password, expires_at, created_at, revoked_at "
           "FROM share_links WHERE account_id = ?")
    params: list[Any] = [account_id]
    if rel_path is not None:
        sql += " AND rel_path = ?"
        params.append(rel_path)
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return [
        ShareLink(
            id=int(row["id"]), account_id=str(row["account_id"]),
            rel_path=str(row["rel_path"]), url=str(row["url"]),
            scope=_enum(LinkScope, row["scope"], LinkScope.ANONYMOUS),
            link_type=_enum(LinkType, row["link_type"], LinkType.VIEW),
            has_password=bool(row["has_password"]),
            expires_at=row["expires_at"],
            created_at=str(row["created_at"] or ""),
            revoked_at=row["revoked_at"],
        )
        for row in _ro(conn).execute(sql, params)
    ]


def revoke_link(
    link_id: int,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Mark a link revoked **locally**.

    rclone's ``--unlink`` is a no-op on OneDrive: the URL keeps working. This
    row records that we stopped offering it, and the UI must say so rather than
    claim the link is dead.

    Args:
        link_id: The row to mark.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when a row was marked.
    """
    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(
            "UPDATE share_links SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL",
            (utcnow_iso(), int(link_id))).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                       label="revoke_link"))


# ─────────────────────────────────────────────────────────────────────────────
# notifications
# ─────────────────────────────────────────────────────────────────────────────

def note_notification(
    key: str,
    *,
    account_id: str = "",
    dbus_id: int | None = None,
    payload: Any = None,
    suppress_for_s: int = 0,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> None:
    """Record that a toast was shown, and for how long not to repeat it.

    Args:
        key: The toast's stable key, e.g. ``"quota_full:onedrive"``.
        account_id: The account the toast belongs to.
        dbus_id: The id the notification daemon returned, so the toast can be
            replaced or closed later.
        payload: Anything JSON-serialisable to remember with it.
        suppress_for_s: Do not show the same key again for this long.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.
    """
    now = utcnow_iso()
    suppressed = _shift_seconds(now, suppress_for_s) if suppress_for_s > 0 else None
    sql = ("INSERT INTO notifications (key, account_id, dbus_id, "
           "last_shown_at, suppressed_until, payload) VALUES (?,?,?,?,?,?) "
           "ON CONFLICT(key) DO UPDATE SET "
           "  account_id = excluded.account_id,"
           "  dbus_id = excluded.dbus_id,"
           "  last_shown_at = excluded.last_shown_at,"
           "  suppressed_until = excluded.suppressed_until,"
           "  payload = excluded.payload")
    params = (key, account_id, dbus_id, now, suppressed,
              json.dumps(payload, ensure_ascii=False) if payload is not None
              else None)
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=False, label="note_notification")


def _shift_seconds(iso: str, seconds: int) -> str:
    """`iso` shifted forward by `seconds`, in exactly ``utcnow_iso()``'s format.

    Not SQLite's ``datetime(?, '+N seconds')``: that renders ``YYYY-MM-DD
    HH:MM:SS`` with a space and no ``Z``, and these columns are compared as
    TEXT, so two spellings of the same instant would compare wrongly.
    """
    base = parse_iso(iso) or _dt.datetime.now(_dt.UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=_dt.UTC)
    moved = base + _dt.timedelta(seconds=int(seconds))
    return moved.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def suppress_notification(
    key: str,
    seconds: int,
    *,
    account_id: str = "",
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> None:
    """Silence one toast key for a while without showing it.

    Args:
        key: The toast key.
        seconds: How long to suppress it for.
        account_id: The account the key belongs to.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.
    """
    now = utcnow_iso()
    sql = ("INSERT INTO notifications (key, account_id, last_shown_at, "
           "suppressed_until) VALUES (?,?,?,?) "
           "ON CONFLICT(key) DO UPDATE SET suppressed_until = excluded.suppressed_until")
    params = (key, account_id, now, _shift_seconds(now, seconds))
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=False, label="suppress_notification")


def should_show(
    key: str,
    *,
    min_interval_s: int = 0,
    now: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Whether a toast with this key may be shown.

    Args:
        key: The toast key.
        min_interval_s: Refuse if the same key was shown more recently than
            this. Rate-limiting lives here rather than in the notifier so it
            survives a restart — a crash loop must not produce a toast storm.
        now: The instant to compare against. Defaults to now.
        conn: A connection to read through.

    Returns:
        True when the toast may be shown.
    """
    stamp = now or utcnow_iso()
    row = _ro(conn).execute(
        "SELECT last_shown_at, suppressed_until FROM notifications "
        "WHERE key = ?", (key,)).fetchone()
    if row is None:
        return True
    suppressed = row["suppressed_until"]
    if suppressed and str(suppressed) > stamp:
        return False
    if min_interval_s > 0 and row["last_shown_at"]:
        if _shift_seconds(str(row["last_shown_at"]), min_interval_s) > stamp:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# kv
# ─────────────────────────────────────────────────────────────────────────────

def kv_get(
    key: str,
    default: Any = None,
    *,
    account_id: str = "",
    conn: sqlite3.Connection | None = None,
) -> Any:
    """Read a small piece of persisted state.

    Values are stored as JSON, so a bool stays a bool across a restart. A value
    that is not JSON — a row someone edited by hand — is returned as its raw
    text rather than treated as missing.

    Args:
        key: The key.
        default: Returned when the key does not exist.
        account_id: The owning account, or ``""`` for application-wide state.
        conn: A connection to read through.

    Returns:
        The stored value, or `default`.
    """
    row = _ro(conn).execute(
        "SELECT value FROM kv WHERE account_id = ? AND key = ?",
        (account_id, key)).fetchone()
    if row is None or row[0] is None:
        return default
    raw = str(row[0])
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def kv_set(
    key: str,
    value: Any,
    *,
    account_id: str = "",
    writer: DbWriter | None = None,
    urgent: bool = False,
    timeout_ms: int = 5_000,
) -> None:
    """Write a small piece of persisted state.

    Args:
        key: The key.
        value: Anything JSON-serialisable.
        account_id: The owning account, or ``""`` for application-wide state.
        writer: The writer to submit to.
        urgent: Commit immediately. Use for anything a crash must not lose.
        timeout_ms: How long to wait for the commit.
    """
    sql = ("INSERT INTO kv (account_id, key, value, updated_at) "
           "VALUES (?,?,?,?) ON CONFLICT(account_id, key) DO UPDATE SET "
           "value = excluded.value, updated_at = excluded.updated_at")
    params = (account_id, key, json.dumps(value, ensure_ascii=False),
              utcnow_iso())
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=urgent, label=f"kv_set:{key}")


def kv_delete(
    key: str,
    *,
    account_id: str = "",
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Remove a key.

    Args:
        key: The key.
        account_id: The owning account.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when a row was removed.
    """
    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(
            "DELETE FROM kv WHERE account_id = ? AND key = ?",
            (account_id, key)).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                       label="kv_delete"))


# ─────────────────────────────────────────────────────────────────────────────
# folder_selection
# ─────────────────────────────────────────────────────────────────────────────

def selection(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, bool]:
    """The "Choose folders" selection, as a path -> selected map.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        ``{rel_path: selected}``. A path that is absent is inherited from its
        parent, so an empty map means "everything is selected".
    """
    rows = _ro(conn).execute(
        "SELECT rel_path, selected FROM folder_selection WHERE account_id = ?",
        (account_id,))
    return {str(row["rel_path"]): bool(row["selected"]) for row in rows}


def excluded_paths(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[str]:
    """Deselected folders, in path order.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        The paths that must appear as ``- <path>/**`` rules in the filters file.
        Rewriting that file mandates an immediate ``--resync`` (invariant I11).
    """
    rows = _ro(conn).execute(
        "SELECT rel_path FROM folder_selection "
        "WHERE account_id = ? AND selected = 0 ORDER BY rel_path",
        (account_id,))
    return [str(row[0]) for row in rows]


def set_selection(
    account_id: str,
    rel_path: str,
    selected: bool,
    *,
    size_bytes: int | None = None,
    item_count: int | None = None,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> None:
    """Record whether a folder is selected for syncing.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        selected: True to sync it, False to exclude it.
        size_bytes: The folder's measured size, for the picker's labels.
        item_count: The folder's measured item count.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.
    """
    sql = ("INSERT INTO folder_selection (account_id, rel_path, selected, "
           "size_bytes, item_count, updated_at) VALUES (?,?,?,?,?,?) "
           "ON CONFLICT(account_id, rel_path) DO UPDATE SET "
           "  selected = excluded.selected,"
           "  size_bytes = COALESCE(excluded.size_bytes, folder_selection.size_bytes),"
           "  item_count = COALESCE(excluded.item_count, folder_selection.item_count),"
           "  updated_at = excluded.updated_at")
    params = (account_id, rel_path, int(bool(selected)), size_bytes,
              item_count, utcnow_iso())
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=False, label="set_selection")


# ─────────────────────────────────────────────────────────────────────────────
# kfm_folder
# ─────────────────────────────────────────────────────────────────────────────

def set_kfm_folder(
    account_id: str,
    folder: KfmFolder | str,
    enabled: bool,
    *,
    original_path: str | None = None,
    target_path: str | None = None,
    journal_path: str | None = None,
    moved_at: str | None = None,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> None:
    """Record the state of one Known Folder Move.

    The ``original_path`` is what makes the move reversible: opting out has to
    put the folder back where it came from, and a journal path lets an
    interrupted copy-verify-remove be resumed rather than restarted.

    Args:
        account_id: The account.
        folder: One of :class:`~onedriveui.models.KfmFolder`.
        enabled: Whether the folder is currently redirected.
        original_path: Where the folder lived before the move.
        target_path: Where it lives now.
        journal_path: The move journal, for crash recovery.
        moved_at: When the move completed. Defaults to now when `enabled`.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.
    """
    sql = ("INSERT INTO kfm_folder (account_id, folder, enabled, "
           "original_path, target_path, journal_path, moved_at) "
           "VALUES (?,?,?,?,?,?,?) "
           "ON CONFLICT(account_id, folder) DO UPDATE SET "
           "  enabled = excluded.enabled,"
           "  original_path = COALESCE(excluded.original_path, kfm_folder.original_path),"
           "  target_path = COALESCE(excluded.target_path, kfm_folder.target_path),"
           "  journal_path = excluded.journal_path,"
           "  moved_at = excluded.moved_at")
    params = (account_id, str(folder), int(bool(enabled)), original_path,
              target_path, journal_path,
              moved_at if moved_at is not None
              else (utcnow_iso() if enabled else None))
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=False, label="set_kfm_folder")


def kfm_folders(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    """Every recorded Known Folder Move for an account.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        ``{folder: {"enabled": …, "original_path": …, "target_path": …,
        "journal_path": …, "moved_at": …}}``.
    """
    rows = _ro(conn).execute(
        "SELECT folder, enabled, original_path, target_path, journal_path, "
        "moved_at FROM kfm_folder WHERE account_id = ?", (account_id,))
    return {
        str(row["folder"]): {
            "enabled": bool(row["enabled"]),
            "original_path": row["original_path"],
            "target_path": row["target_path"],
            "journal_path": row["journal_path"],
            "moved_at": row["moved_at"],
        }
        for row in rows
    }


# ─────────────────────────────────────────────────────────────────────────────
# dialog_seen
# ─────────────────────────────────────────────────────────────────────────────

def dialog_seen(
    key: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Whether a "don't show this again" dialog has already been shown.

    Args:
        key: A :class:`~onedriveui.models.DialogKey` value.
        conn: A connection to read through.

    Returns:
        True when the user has seen it.
    """
    row = _ro(conn).execute(
        "SELECT 1 FROM dialog_seen WHERE key = ?", (str(key),)).fetchone()
    return row is not None


def mark_dialog_seen(
    key: str,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> None:
    """Remember that a dialog has been shown.

    Args:
        key: A :class:`~onedriveui.models.DialogKey` value.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.
    """
    sql = ("INSERT INTO dialog_seen (key, seen_at) VALUES (?,?) "
           "ON CONFLICT(key) DO UPDATE SET seen_at = excluded.seen_at")
    _w(writer).submit_sync(
        lambda conn: conn.execute(sql, (str(key), utcnow_iso())), timeout_ms,
        urgent=False, label="mark_dialog_seen")
