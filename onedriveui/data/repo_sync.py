"""Repository for the sync-history tables.

Owns the reads and writes for ``activity``, ``issues``, ``runs``,
``conflicts``, ``decisions`` and ``latches``.

Every function here is one of two shapes:

* a **read**, which runs on the caller's thread against that thread's
  ``mode=ro`` connection and is budgeted under 5 ms — every query below is
  covered by an index in ``schema.sql``;
* a **write**, which is handed to :class:`~onedriveui.data.writer.DbWriter` and
  never touches a connection itself.

Two of those writes are ``urgent``:

* :func:`set_latch` / :func:`clear_latch` — a latch is a hazard that must
  survive a ``SIGKILL``. Feeding ladder rungs 5-7 from a row that was still in a
  100 ms batch when the process died would silently un-latch the hazard.
* :func:`create_decision` / :func:`answer_decision` — the UI tells the user
  their answer is recorded. It must be true before the dialog closes.

Everything else — activity rows, issue occurrences, run progress — is
observability, and losing the last ≤100 ms of it to a crash costs nothing that
cannot be re-derived.

This module deliberately does not import ``bus``. Signals for these tables are
emitted by their owning service (``sync/activity.py``, ``sync/issues.py``,
``sync/conflicts.py``, ``sync/decisions.py``), which knows whether a row is news
to the user; a repository does not.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from typing import Any, Final, Iterable, Sequence

from onedriveui.constants import ACTIVITY_UI_ROWS, DECISION_EXPIRY_DAYS
from onedriveui.data import db
from onedriveui.data.writer import WRITER, DbWriter
from onedriveui.models import (
    ActivityEvent, ActivityState, ActivityVerb, ConflictInfo, Decision,
    DecisionKind, IssueCode, IssueSeverity, RecoveryAction, RunKind, RunRecord,
    RunVerdict, SyncIssue, utcnow_iso,
)

__all__ = [
    "append_activity", "update_activity", "recent_activity", "activity_for_path",
    "mark_inflight_interrupted",
    "raise_issue", "resolve_issue", "open_issues", "issue_counts", "mute_issue",
    "start_run", "finish_run", "last_run", "recent_runs", "set_run_offset",
    "add_conflict", "open_conflicts", "resolve_conflict",
    "create_decision", "pending_decisions", "answer_decision",
    "expire_decisions",
    "set_latch", "clear_latch", "latches", "latch_detail",
    "EXPIRED_ANSWER", "LATCH_NAMES",
]

#: The answer :func:`expire_decisions` writes. It is a **refusal**: an expired
#: mass-delete prompt means "do not delete", matching Microsoft's 7-day policy,
#: never "go ahead because nobody objected".
EXPIRED_ANSWER: Final[str] = "expired"

#: The latch names ARCHITECTURE §6.5 defines. Not an enum in ``models`` because
#: the ladder consumes them as the plain strings ``Facts.latches`` carries.
LATCH_NAMES: Final[tuple[str, ...]] = (
    "needs_resync", "bisync_critical", "quota_exceeded", "mount_failed",
    "orphan_cache",
)


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────

def _w(writer: DbWriter | None) -> DbWriter:
    """The writer to submit to. Explicit beats the singleton, for tests."""
    return writer if writer is not None else WRITER


def _ro(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    """This thread's read-only connection, unless one was supplied."""
    return conn if conn is not None else db.open_ro()


def _bool(value: Any) -> bool:
    """SQLite has no boolean type; 0/1 come back as ints."""
    return bool(value)


def _json_list(raw: Any) -> list[Any]:
    """Parse a JSON list column, tolerating NULL and malformed text."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _plus_days(iso: str, days: int) -> str:
    """`iso` shifted forward, in exactly ``utcnow_iso()``'s format.

    Computed here rather than with SQLite's ``datetime(?, '+7 days')`` because
    SQLite renders ``YYYY-MM-DD HH:MM:SS`` — a space and no ``Z`` — and these
    columns are compared as TEXT. Two different spellings of the same instant
    would make ``expires_at <= now`` wrong for any pair on the same day.
    """
    from onedriveui.models import parse_iso
    base = parse_iso(iso) or _dt.datetime.now(_dt.UTC)
    if base.tzinfo is None:
        base = base.replace(tzinfo=_dt.UTC)
    moved = (base + _dt.timedelta(days=int(days))).astimezone(_dt.UTC)
    return moved.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _enum(cls: type, value: Any, fallback: Any) -> Any:
    """Coerce a stored string back to its enum, defaulting on an unknown one.

    A row written by a newer version must not crash an older one; an unknown
    verb rendering as ``MODIFIED`` is a cosmetic loss, a traceback is not.
    """
    if value is None:
        return fallback
    try:
        return cls(value)
    except ValueError:
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# activity
# ─────────────────────────────────────────────────────────────────────────────

_ACTIVITY_COLUMNS = (
    "id, account_id, rel_path, name, is_dir, verb, direction, state, bytes, "
    "size, started_at, completed_at, error, error_kind, job_group, run_id, "
    "src_fs, dst_fs, dedupe_key")


def _activity_from_row(row: sqlite3.Row) -> ActivityEvent:
    """Build an :class:`~onedriveui.models.ActivityEvent` from a table row."""
    return ActivityEvent(
        id=int(row["id"]),
        account_id=str(row["account_id"]),
        rel_path=str(row["rel_path"]),
        name=str(row["name"]),
        is_dir=_bool(row["is_dir"]),
        verb=_enum(ActivityVerb, row["verb"], ActivityVerb.MODIFIED),
        direction=str(row["direction"] or ""),
        state=_enum(ActivityState, row["state"], ActivityState.DONE),
        bytes=int(row["bytes"] or 0),
        size=int(row["size"] or 0),
        started_at=str(row["started_at"] or ""),
        completed_at=row["completed_at"],
        error=row["error"],
        error_kind=_enum(IssueCode, row["error_kind"], None)
        if row["error_kind"] else None,
        job_group=str(row["job_group"] or ""),
        run_id=str(row["run_id"] or ""),
        dedupe_key=row["dedupe_key"],
    )


def append_activity(
    event: ActivityEvent,
    *,
    src_fs: str = "",
    dst_fs: str = "",
    writer: DbWriter | None = None,
    sync: bool = False,
    timeout_ms: int = 5_000,
) -> int | None:
    """Record one activity row.

    Args:
        event: The event. ``event.id`` is ignored — the table assigns it.
        src_fs: The rclone source fs, kept for diagnostics only.
        dst_fs: The rclone destination fs.
        writer: The writer to submit to. Defaults to the application's.
        sync: Wait for the commit and return the new row id. Off by default:
            activity is high-volume and the caller almost never needs the id.
        timeout_ms: How long to wait when `sync`.

    Returns:
        The new row id when `sync`, otherwise ``None``. ``None`` is also
        returned for a duplicate: ``dedupe_key`` carries a partial unique index,
        and ``core/transferred`` genuinely re-reports the same completion after
        a daemon restart, so a duplicate is expected and silently dropped.

    ``activity`` is the reason this table exists at all: ``core/transferred``
    keeps only 100 entries, is wiped by ``core/stats-reset``, and is lost on any
    daemon restart.
    """
    params = (
        event.account_id, event.rel_path, event.name, int(event.is_dir),
        str(event.verb), event.direction, str(event.state), int(event.bytes),
        int(event.size), event.started_at or utcnow_iso(), event.completed_at,
        event.error, str(event.error_kind) if event.error_kind else None,
        event.job_group, event.run_id, src_fs, dst_fs, event.dedupe_key,
    )
    sql = (
        "INSERT INTO activity (account_id, rel_path, name, is_dir, verb, "
        "direction, state, bytes, size, started_at, completed_at, error, "
        "error_kind, job_group, run_id, src_fs, dst_fs, dedupe_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING")

    def op(conn: sqlite3.Connection) -> int | None:
        cur = conn.execute(sql, params)
        return int(cur.lastrowid) if cur.rowcount else None

    if sync:
        return _w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="append_activity")
    _w(writer).submit(op, label="append_activity")
    return None


def update_activity(
    activity_id: int,
    *,
    state: ActivityState | None = None,
    bytes_done: int | None = None,
    completed_at: str | None = None,
    error: str | None = None,
    error_kind: IssueCode | None = None,
    writer: DbWriter | None = None,
) -> None:
    """Advance an in-flight activity row.

    Args:
        activity_id: The row to update.
        state: The new state, e.g. ``DONE`` or ``INTERRUPTED``.
        bytes_done: Bytes transferred so far.
        completed_at: The completion stamp. Defaults to now when `state`
            becomes a terminal one and no stamp was given.
        error: The raw error text, for diagnostics.
        error_kind: The classified code.
        writer: The writer to submit to.

    Only the arguments you pass are written, so a progress tick does not clobber
    an error recorded a moment earlier by another code path.
    """
    sets: list[str] = []
    params: list[Any] = []
    if state is not None:
        sets.append("state = ?")
        params.append(str(state))
        if completed_at is None and state in (
                ActivityState.DONE, ActivityState.ERROR,
                ActivityState.CANCELLED, ActivityState.INTERRUPTED):
            completed_at = utcnow_iso()
    if bytes_done is not None:
        sets.append("bytes = ?")
        params.append(int(bytes_done))
    if completed_at is not None:
        sets.append("completed_at = ?")
        params.append(completed_at)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if error_kind is not None:
        sets.append("error_kind = ?")
        params.append(str(error_kind))
    if not sets:
        return
    params.append(int(activity_id))
    sql = f"UPDATE activity SET {', '.join(sets)} WHERE id = ?"
    _w(writer).submit(lambda conn: conn.execute(sql, params),
                      label="update_activity")


def mark_inflight_interrupted(
    account_id: str,
    *,
    run_id: str | None = None,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Close out every in-flight row as ``interrupted``.

    Called on startup and whenever the daemon's ``executeId`` changes: an
    in-flight row whose daemon is gone has an unknown outcome, and ARCHITECTURE
    §5.7 is explicit that this is ``interrupted``, **not** ``error`` — we do not
    know that it failed, only that we stopped watching.

    Args:
        account_id: The account whose rows to close.
        run_id: Restrict to one run.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        How many rows were closed.
    """
    sql = ("UPDATE activity SET state = ?, completed_at = ? "
           "WHERE account_id = ? AND state = ?")
    params: list[Any] = [str(ActivityState.INTERRUPTED), utcnow_iso(),
                         account_id, str(ActivityState.INFLIGHT)]
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).rowcount)

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="mark_inflight_interrupted") or 0)


def recent_activity(
    account_id: str,
    limit: int = ACTIVITY_UI_ROWS,
    *,
    verbs: Sequence[ActivityVerb] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[ActivityEvent]:
    """The newest activity rows for an account.

    Args:
        account_id: The account.
        limit: How many rows, newest first.
        verbs: Restrict to these verbs, e.g. only uploads.
        conn: A connection to read through. Defaults to this thread's read-only
            one.

    Returns:
        Events, newest first. Served by ``ix_activity_recent``.
    """
    sql = (f"SELECT {_ACTIVITY_COLUMNS} FROM activity WHERE account_id = ?")
    params: list[Any] = [account_id]
    if verbs:
        placeholders = ",".join("?" * len(verbs))
        sql += f" AND verb IN ({placeholders})"
        params += [str(v) for v in verbs]
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return [_activity_from_row(row) for row in _ro(conn).execute(sql, params)]


def activity_for_path(
    account_id: str,
    rel_path: str,
    limit: int = 20,
    *,
    conn: sqlite3.Connection | None = None,
) -> list[ActivityEvent]:
    """Every recorded event for one file, newest first.

    Args:
        account_id: The account.
        rel_path: POSIX path relative to the sync root.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Events, newest first. Served by ``ix_activity_path``.
    """
    rows = _ro(conn).execute(
        f"SELECT {_ACTIVITY_COLUMNS} FROM activity "
        "WHERE account_id = ? AND rel_path = ? "
        "ORDER BY id DESC LIMIT ?", (account_id, rel_path, int(limit)))
    return [_activity_from_row(row) for row in rows]


# ─────────────────────────────────────────────────────────────────────────────
# issues
# ─────────────────────────────────────────────────────────────────────────────

_ISSUE_COLUMNS = (
    "id, account_id, code, severity, rel_path, title, detail, raw_error, "
    "actions, first_seen_at, last_seen_at, occurrences, resolved_at, "
    "resolution, muted")


def _issue_from_row(row: sqlite3.Row) -> SyncIssue:
    """Build a :class:`~onedriveui.models.SyncIssue` from a table row."""
    actions: list[RecoveryAction] = []
    for name in _json_list(row["actions"]):
        try:
            actions.append(RecoveryAction(name))
        except ValueError:
            continue
    return SyncIssue(
        id=int(row["id"]),
        account_id=str(row["account_id"]),
        code=_enum(IssueCode, row["code"], IssueCode.UNKNOWN),
        severity=_enum(IssueSeverity, row["severity"], IssueSeverity.ERROR),
        rel_path=row["rel_path"],
        title=str(row["title"] or ""),
        detail=str(row["detail"] or ""),
        raw_error=str(row["raw_error"] or ""),
        actions=tuple(actions),
        first_seen_at=str(row["first_seen_at"] or ""),
        last_seen_at=str(row["last_seen_at"] or ""),
        occurrences=int(row["occurrences"] or 1),
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
        muted=_bool(row["muted"]),
    )


def raise_issue(
    issue: SyncIssue,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Open an issue, or bump the one already open for the same target.

    ``ux_issue_open`` is unique on ``(account_id, code, IFNULL(rel_path,''))``
    **while ``resolved_at`` is NULL**, so the same failure reported a thousand
    times is one row with ``occurrences = 1000`` rather than a thousand rows.
    Resolving it and hitting the same failure again correctly opens a new one.

    Args:
        issue: The issue to raise. ``id`` is ignored; ``first_seen_at`` and
            ``last_seen_at`` default to now.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The row id of the open issue — new or bumped.
    """
    now = utcnow_iso()
    params = (
        issue.account_id, str(issue.code), str(issue.severity), issue.rel_path,
        issue.title, issue.detail, issue.raw_error,
        json.dumps([str(a) for a in issue.actions]),
        issue.first_seen_at or now, issue.last_seen_at or now,
        int(issue.muted),
    )
    sql = (
        "INSERT INTO issues (account_id, code, severity, rel_path, title, "
        "detail, raw_error, actions, first_seen_at, last_seen_at, occurrences, "
        "muted) VALUES (?,?,?,?,?,?,?,?,?,?,1,?) "
        "ON CONFLICT (account_id, code, IFNULL(rel_path,'')) "
        "WHERE resolved_at IS NULL DO UPDATE SET "
        "  last_seen_at = excluded.last_seen_at,"
        "  occurrences  = issues.occurrences + 1,"
        "  severity     = excluded.severity,"
        "  title        = excluded.title,"
        "  detail       = excluded.detail,"
        "  raw_error    = excluded.raw_error,"
        "  actions      = excluded.actions "
        "RETURNING id")

    def op(conn: sqlite3.Connection) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0])

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="raise_issue"))


def resolve_issue(
    issue_id: int,
    resolution: str = "auto",
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Close an open issue.

    Args:
        issue_id: The row to close.
        resolution: ``retried``, ``renamed``, ``ignored``, ``deleted`` or
            ``auto`` (the condition simply stopped being true).
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when a row was closed; False when it was already resolved or does
        not exist. The caller uses that to decide whether to emit
        ``BUS.issue_resolved``, so it must be honest.
    """
    sql = ("UPDATE issues SET resolved_at = ?, resolution = ? "
           "WHERE id = ? AND resolved_at IS NULL")
    params = (utcnow_iso(), resolution, int(issue_id))

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                       label="resolve_issue"))


def resolve_issues_by_code(
    account_id: str,
    code: IssueCode,
    resolution: str = "auto",
    *,
    rel_path: str | None = None,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> list[int]:
    """Close every open issue with a given code, returning their ids.

    Args:
        account_id: The account.
        code: The issue code to clear.
        resolution: Why it was closed.
        rel_path: Restrict to one path.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The ids that were closed, so the caller can emit one
        ``BUS.issue_resolved`` per row.
    """
    sql = ("UPDATE issues SET resolved_at = ?, resolution = ? "
           "WHERE account_id = ? AND code = ? AND resolved_at IS NULL")
    params: list[Any] = [utcnow_iso(), resolution, account_id, str(code)]
    if rel_path is not None:
        sql += " AND rel_path = ?"
        params.append(rel_path)
    sql += " RETURNING id"

    def op(conn: sqlite3.Connection) -> list[int]:
        return [int(row[0]) for row in conn.execute(sql, params)]

    return list(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                       label="resolve_issues_by_code") or [])


def mute_issue(
    issue_id: int,
    muted: bool = True,
    *,
    writer: DbWriter | None = None,
) -> None:
    """Hide an issue from the notice bar without resolving it.

    Args:
        issue_id: The row to mute.
        muted: True to mute, False to un-mute.
        writer: The writer to submit to.
    """
    _w(writer).submit(
        lambda conn: conn.execute(
            "UPDATE issues SET muted = ? WHERE id = ?",
            (int(bool(muted)), int(issue_id))), label="mute_issue")


def open_issues(
    account_id: str,
    *,
    severity: IssueSeverity | None = None,
    include_muted: bool = True,
    limit: int = 500,
    conn: sqlite3.Connection | None = None,
) -> list[SyncIssue]:
    """Every unresolved issue for an account, most recent first.

    Args:
        account_id: The account.
        severity: Restrict to one severity.
        include_muted: Include issues the user muted.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Open issues, newest first. Served by the partial index
        ``ix_issues_open``, so this stays fast even with thousands resolved.
    """
    sql = (f"SELECT {_ISSUE_COLUMNS} FROM issues "
           "WHERE account_id = ? AND resolved_at IS NULL")
    params: list[Any] = [account_id]
    if severity is not None:
        sql += " AND severity = ?"
        params.append(str(severity))
    if not include_muted:
        sql += " AND muted = 0"
    sql += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    return [_issue_from_row(row) for row in _ro(conn).execute(sql, params)]


def issue_counts(
    account_id: str,
    *,
    include_muted: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """Open issue counts per severity, for the ladder and the badge.

    Args:
        account_id: The account.
        include_muted: Count muted issues too. Off by default: a muted issue
            must not keep the tray in ``ERROR``.
        conn: A connection to read through.

    Returns:
        A dict with a key for **every** :class:`~onedriveui.models.IssueSeverity`
        plus ``"total"``, so a caller never has to guard a missing key.
    """
    sql = ("SELECT severity, count(*) AS n FROM issues "
           "WHERE account_id = ? AND resolved_at IS NULL")
    params: list[Any] = [account_id]
    if not include_muted:
        sql += " AND muted = 0"
    sql += " GROUP BY severity"
    counts = {str(s): 0 for s in IssueSeverity}
    total = 0
    for row in _ro(conn).execute(sql, params):
        counts[str(row["severity"])] = int(row["n"])
        total += int(row["n"])
    counts["total"] = total
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# runs
# ─────────────────────────────────────────────────────────────────────────────

_RUN_COLUMNS = (
    "run_id, account_id, kind, argv, started_at, ended_at, exit_code, verdict, "
    "log_path, log_offset, unit, session, listing1, listing2, "
    "files_transferred, bytes, deletes, renames, errors, summary")


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    """Build a :class:`~onedriveui.models.RunRecord` from a table row."""
    return RunRecord(
        run_id=str(row["run_id"]),
        account_id=str(row["account_id"]),
        kind=_enum(RunKind, row["kind"], RunKind.BISYNC),
        argv=tuple(str(a) for a in _json_list(row["argv"])),
        started_at=str(row["started_at"] or ""),
        ended_at=row["ended_at"],
        exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
        verdict=_enum(RunVerdict, row["verdict"], RunVerdict.UNKNOWN),
        log_path=str(row["log_path"] or ""),
        log_offset=int(row["log_offset"] or 0),
        unit=str(row["unit"] or ""),
        session=str(row["session"] or ""),
        listing1=str(row["listing1"] or ""),
        listing2=str(row["listing2"] or ""),
        files_transferred=int(row["files_transferred"] or 0),
        bytes=int(row["bytes"] or 0),
        deletes=int(row["deletes"] or 0),
        renames=int(row["renames"] or 0),
        errors=int(row["errors"] or 0),
        summary=str(row["summary"] or ""),
    )


def start_run(
    record: RunRecord,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> str:
    """Open a run row before the process is launched.

    The row exists *first* on purpose: if the launch itself crashes the GUI, the
    next start finds an open run, adopts or fails it, and can re-attach the log
    tailer at ``log_offset``. A row written after a successful launch would
    leave an orphaned bisync nobody remembers starting.

    Args:
        record: The run. ``started_at`` defaults to now.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The ``run_id``.
    """
    params = (
        record.run_id, record.account_id, str(record.kind),
        json.dumps(list(record.argv)), record.started_at or utcnow_iso(),
        record.log_path, int(record.log_offset), record.unit, record.session,
        record.listing1, record.listing2,
    )
    sql = ("INSERT INTO runs (run_id, account_id, kind, argv, started_at, "
           "log_path, log_offset, unit, session, listing1, listing2) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
           "ON CONFLICT(run_id) DO UPDATE SET "
           "  started_at = excluded.started_at, argv = excluded.argv,"
           "  log_path = excluded.log_path, unit = excluded.unit,"
           "  session = excluded.session")
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=False, label="start_run")
    return record.run_id


def finish_run(
    record: RunRecord,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> None:
    """Close a run row with its verdict and counters.

    Args:
        record: The finished run. ``ended_at`` defaults to now.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    The verdict is classified from the run's **log**, never from the exit code
    alone, and this function simply stores whatever the classifier decided.
    """
    params = (
        record.ended_at or utcnow_iso(), record.exit_code, str(record.verdict),
        int(record.log_offset), record.listing1, record.listing2,
        int(record.files_transferred), int(record.bytes), int(record.deletes),
        int(record.renames), int(record.errors), record.summary, record.run_id,
    )
    sql = ("UPDATE runs SET ended_at = ?, exit_code = ?, verdict = ?, "
           "log_offset = ?, listing1 = ?, listing2 = ?, files_transferred = ?, "
           "bytes = ?, deletes = ?, renames = ?, errors = ?, summary = ? "
           "WHERE run_id = ?")
    _w(writer).submit_sync(lambda conn: conn.execute(sql, params), timeout_ms,
                           urgent=False, label="finish_run")


def set_run_offset(
    run_id: str,
    offset: int,
    *,
    writer: DbWriter | None = None,
) -> None:
    """Checkpoint the log tailer's byte offset.

    Args:
        run_id: The run being tailed.
        offset: The byte offset already consumed.
        writer: The writer to submit to.

    This is what makes a GUI restart resume the log instead of replaying it —
    replaying would duplicate every conflict and every activity row.
    """
    _w(writer).submit(
        lambda conn: conn.execute(
            "UPDATE runs SET log_offset = ? WHERE run_id = ?",
            (int(offset), run_id)), label="set_run_offset")


def last_run(
    account_id: str,
    kind: RunKind | None = None,
    *,
    finished_only: bool = False,
    conn: sqlite3.Connection | None = None,
) -> RunRecord | None:
    """The most recent run for an account.

    Args:
        account_id: The account.
        kind: Restrict to one kind of run.
        finished_only: Ignore runs that are still open.
        conn: A connection to read through.

    Returns:
        The newest matching run, or ``None``.
    """
    sql = f"SELECT {_RUN_COLUMNS} FROM runs WHERE account_id = ?"
    params: list[Any] = [account_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(str(kind))
    if finished_only:
        sql += " AND ended_at IS NOT NULL"
    sql += " ORDER BY started_at DESC LIMIT 1"
    row = _ro(conn).execute(sql, params).fetchone()
    return _run_from_row(row) if row is not None else None


def recent_runs(
    account_id: str,
    limit: int = 20,
    *,
    kind: RunKind | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[RunRecord]:
    """Recent runs, newest first.

    Args:
        account_id: The account.
        limit: How many rows.
        kind: Restrict to one kind of run.
        conn: A connection to read through.

    Returns:
        Runs, newest first. Served by ``ix_runs_recent``.
    """
    sql = f"SELECT {_RUN_COLUMNS} FROM runs WHERE account_id = ?"
    params: list[Any] = [account_id]
    if kind is not None:
        sql += " AND kind = ?"
        params.append(str(kind))
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    return [_run_from_row(row) for row in _ro(conn).execute(sql, params)]


# ─────────────────────────────────────────────────────────────────────────────
# conflicts
# ─────────────────────────────────────────────────────────────────────────────

_CONFLICT_COLUMNS = (
    "id, account_id, rel_path, loser_path, winner_side, detected_at, run_id, "
    "resolved_at, resolution, local_size, local_mtime, remote_size, "
    "remote_mtime")


def _conflict_from_row(row: sqlite3.Row) -> ConflictInfo:
    """Build a :class:`~onedriveui.models.ConflictInfo` from a table row."""
    return ConflictInfo(
        id=int(row["id"]),
        account_id=str(row["account_id"]),
        rel_path=str(row["rel_path"]),
        loser_path=str(row["loser_path"]),
        winner_side=str(row["winner_side"] or ""),
        detected_at=str(row["detected_at"] or ""),
        run_id=str(row["run_id"] or ""),
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
        local_size=int(row["local_size"] or 0),
        local_mtime=str(row["local_mtime"] or ""),
        remote_size=int(row["remote_size"] or 0),
        remote_mtime=str(row["remote_mtime"] or ""),
    )


def add_conflict(
    conflict: ConflictInfo,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Record a conflict, or refresh the one already open for the same loser.

    ``ux_conflict_open`` is unique on ``(account_id, loser_path)`` while
    unresolved, so a bisync run that re-reports the same rename does not create
    a second row the user must dismiss twice.

    Args:
        conflict: The conflict. ``detected_at`` defaults to now.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The row id of the open conflict.
    """
    params = (
        conflict.account_id, conflict.rel_path, conflict.loser_path,
        conflict.winner_side, conflict.detected_at or utcnow_iso(),
        conflict.run_id, conflict.local_size or None, conflict.local_mtime,
        conflict.remote_size or None, conflict.remote_mtime,
    )
    sql = ("INSERT INTO conflicts (account_id, rel_path, loser_path, "
           "winner_side, detected_at, run_id, local_size, local_mtime, "
           "remote_size, remote_mtime) VALUES (?,?,?,?,?,?,?,?,?,?) "
           "ON CONFLICT (account_id, loser_path) WHERE resolved_at IS NULL "
           "DO UPDATE SET detected_at = excluded.detected_at,"
           "  rel_path = excluded.rel_path, run_id = excluded.run_id,"
           "  winner_side = excluded.winner_side "
           "RETURNING id")

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                      label="add_conflict"))


def open_conflicts(
    account_id: str,
    *,
    limit: int = 500,
    conn: sqlite3.Connection | None = None,
) -> list[ConflictInfo]:
    """Unresolved conflicts for an account, newest first.

    Args:
        account_id: The account.
        limit: How many rows.
        conn: A connection to read through.

    Returns:
        Open conflicts.
    """
    rows = _ro(conn).execute(
        f"SELECT {_CONFLICT_COLUMNS} FROM conflicts "
        "WHERE account_id = ? AND resolved_at IS NULL "
        "ORDER BY detected_at DESC, id DESC LIMIT ?",
        (account_id, int(limit)))
    return [_conflict_from_row(row) for row in rows]


def resolve_conflict(
    conflict_id: int,
    resolution: str,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Close a conflict.

    Args:
        conflict_id: The row to close.
        resolution: ``keep_both``, ``keep_local`` or ``keep_cloud``.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when a row was closed.
    """
    sql = ("UPDATE conflicts SET resolved_at = ?, resolution = ? "
           "WHERE id = ? AND resolved_at IS NULL")
    params = (utcnow_iso(), resolution, int(conflict_id))

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=False,
                                       label="resolve_conflict"))


# ─────────────────────────────────────────────────────────────────────────────
# decisions  — urgent: the UI claims these are recorded
# ─────────────────────────────────────────────────────────────────────────────

_DECISION_COLUMNS = (
    "id, account_id, kind, payload, created_at, expires_at, answered_at, "
    "answer, run_id")


def _decision_from_row(row: sqlite3.Row) -> Decision:
    """Build a :class:`~onedriveui.models.Decision` from a table row."""
    payload: Any
    try:
        payload = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return Decision(
        id=int(row["id"]),
        account_id=str(row["account_id"]),
        kind=_enum(DecisionKind, row["kind"], DecisionKind.MASS_DELETE),
        payload=payload,
        created_at=str(row["created_at"] or ""),
        expires_at=row["expires_at"],
        answered_at=row["answered_at"],
        answer=row["answer"],
        run_id=str(row["run_id"] or ""),
    )


def create_decision(
    decision: Decision,
    *,
    expiry_days: int = DECISION_EXPIRY_DAYS,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Record a blocking decision, durably, before the dialog is shown.

    Written with ``urgent=True``: the row must exist on disk before the UI puts
    the question to the user, or a crash between the prompt and the answer would
    lose the fact that a hazard was ever detected.

    Args:
        decision: The decision. ``created_at`` defaults to now, and
            ``expires_at`` to `expiry_days` from now when not set.
        expiry_days: The default expiry window. Expiry means "do not do the
            destructive thing", never "assume yes".
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The new row id.
    """
    created = decision.created_at or utcnow_iso()
    expires = decision.expires_at or _plus_days(created, expiry_days)
    sql = ("INSERT INTO decisions (account_id, kind, payload, created_at, "
           "expires_at, answered_at, answer, run_id) "
           "VALUES (?,?,?,?,?,?,?,?) RETURNING id")
    params = (
        decision.account_id, str(decision.kind),
        json.dumps(decision.payload, ensure_ascii=False), created, expires,
        decision.answered_at, decision.answer, decision.run_id,
    )

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=True,
                                      label="create_decision"))


def pending_decisions(
    account_id: str | None = None,
    *,
    kind: DecisionKind | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[Decision]:
    """Unanswered decisions, oldest first.

    Args:
        account_id: The account, or ``None`` for every account.
        kind: Restrict to one kind.
        conn: A connection to read through.

    Returns:
        Pending decisions. Served by the partial index
        ``ix_decisions_pending``. An **expired but unanswered** decision is
        still pending: expiry is a policy the supervisor applies, not a reason
        to forget the question was asked.
    """
    sql = f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE answered_at IS NULL"
    params: list[Any] = []
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    if kind is not None:
        sql += " AND kind = ?"
        params.append(str(kind))
    sql += " ORDER BY created_at ASC, id ASC"
    return [_decision_from_row(row) for row in _ro(conn).execute(sql, params)]


def answer_decision(
    decision_id: int,
    answer: str,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Record the user's answer, durably.

    Written with ``urgent=True``: :func:`~onedriveui.rc.guards` will refuse a
    ``--resync`` without an answered row (invariant I15), so a crash that lost
    this write would lock the account out of syncing until the user answered
    again — or, worse, an answer that had not committed could be read back as
    unanswered while the UI had already started the run.

    Args:
        decision_id: The row to answer.
        answer: The answer token, e.g. ``"yes"``, ``"no"``, ``"keep_both"``.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when the row was answered; False when it was already answered or
        does not exist.
    """
    sql = ("UPDATE decisions SET answered_at = ?, answer = ? "
           "WHERE id = ? AND answered_at IS NULL")
    params = (utcnow_iso(), answer, int(decision_id))

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=True,
                                       label="answer_decision"))


def expire_decisions(
    account_id: str | None = None,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> list[int]:
    """Answer every overdue decision with a refusal.

    Microsoft's rule is that an unanswered deletion prompt expires into "**do
    not delete**" after seven days, and this implements exactly that: the row is
    answered with :data:`EXPIRED_ANSWER`, which every consumer treats as a
    refusal of the destructive branch. The row is never deleted — the history of
    what was asked is part of what makes a later "why did nothing sync?" answer
    possible.

    Args:
        account_id: The account, or ``None`` for every account.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The ids that were expired.
    """
    now = utcnow_iso()
    sql = ("UPDATE decisions SET answered_at = ?, answer = ? "
           "WHERE answered_at IS NULL AND expires_at IS NOT NULL "
           "AND expires_at <= ?")
    params: list[Any] = [now, EXPIRED_ANSWER, now]
    if account_id is not None:
        sql += " AND account_id = ?"
        params.append(account_id)
    sql += " RETURNING id"

    def op(conn: sqlite3.Connection) -> list[int]:
        return [int(row[0]) for row in conn.execute(sql, params)]

    return list(_w(writer).submit_sync(op, timeout_ms, urgent=True,
                                       label="expire_decisions") or [])


# ─────────────────────────────────────────────────────────────────────────────
# latches — urgent: these must survive a SIGKILL
# ─────────────────────────────────────────────────────────────────────────────

def set_latch(
    account_id: str,
    name: str,
    detail: str | None = None,
    *,
    increment: bool = True,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Latch a hazard, durably.

    A latch is the mechanism that makes crash recovery exact: ``needs_resync``,
    ``bisync_critical``, ``quota_exceeded``, ``mount_failed`` and
    ``orphan_cache`` all describe conditions that are still true after a
    ``SIGKILL`` and that the ladder must see on the very first tick after a
    restart. Losing one to a 100 ms batch would let the application come back up
    believing everything is fine, so this write is ``urgent`` and does not
    return until it has committed.

    Args:
        account_id: The account.
        name: One of :data:`LATCH_NAMES`.
        detail: Human-readable context, shown in the notice bar.
        increment: Bump ``counter`` when the latch was already set. That counter
            is what drives the restart ladders in ARCHITECTURE §5.7.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        The latch's counter after the write: 0 the first time it is set.
    """
    sql = ("INSERT INTO latches (account_id, name, set_at, detail, counter) "
           "VALUES (?,?,?,?,0) "
           "ON CONFLICT(account_id, name) DO UPDATE SET "
           "  detail = excluded.detail,"
           f"  counter = latches.counter + {1 if increment else 0} "
           "RETURNING counter")
    params = (account_id, name, utcnow_iso(), detail)

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=True,
                                      label=f"set_latch:{name}"))


def clear_latch(
    account_id: str,
    name: str,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> bool:
    """Release a latched hazard, durably.

    Args:
        account_id: The account.
        name: The latch to clear.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        True when a latch was actually cleared.

    Also ``urgent``: clearing must be as durable as setting, or a crash right
    after a successful ``--resync`` would leave ``needs_resync`` latched and the
    account permanently blocked.
    """
    sql = "DELETE FROM latches WHERE account_id = ? AND name = ?"

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, (account_id, name)).rowcount)

    return bool(_w(writer).submit_sync(op, timeout_ms, urgent=True,
                                       label=f"clear_latch:{name}"))


def latches(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> frozenset[str]:
    """Every latch currently set for an account.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        A frozenset of latch names, shaped for
        :attr:`onedriveui.models.Facts.latches`.
    """
    rows = _ro(conn).execute(
        "SELECT name FROM latches WHERE account_id = ?", (account_id,))
    return frozenset(str(row[0]) for row in rows)


def latch_detail(
    account_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    """Latches with their timestamps, details and counters.

    Args:
        account_id: The account.
        conn: A connection to read through.

    Returns:
        ``{name: {"set_at": …, "detail": …, "counter": …}}`` — what the notice
        bar renders to explain *why* syncing is blocked.
    """
    rows = _ro(conn).execute(
        "SELECT name, set_at, detail, counter FROM latches WHERE account_id = ?",
        (account_id,))
    return {
        str(row["name"]): {
            "set_at": str(row["set_at"] or ""),
            "detail": row["detail"],
            "counter": int(row["counter"] or 0),
        }
        for row in rows
    }


def clear_all_latches(
    account_id: str,
    names: Iterable[str] | None = None,
    *,
    writer: DbWriter | None = None,
    timeout_ms: int = 5_000,
) -> int:
    """Clear several latches at once, durably.

    Args:
        account_id: The account.
        names: The latches to clear, or ``None`` for all of them.
        writer: The writer to submit to.
        timeout_ms: How long to wait for the commit.

    Returns:
        How many latches were cleared.
    """
    sql = "DELETE FROM latches WHERE account_id = ?"
    params: list[Any] = [account_id]
    if names is not None:
        wanted = [str(n) for n in names]
        if not wanted:
            return 0
        sql += f" AND name IN ({','.join('?' * len(wanted))})"
        params += wanted

    def op(conn: sqlite3.Connection) -> int:
        return int(conn.execute(sql, params).rowcount)

    return int(_w(writer).submit_sync(op, timeout_ms, urgent=True,
                                      label="clear_all_latches") or 0)
