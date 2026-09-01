"""Reading a bisync run: the JSONL log, the verdict, and the tailer that resumes.

bisync is driven as a subprocess with ``--use-json-log --color NEVER
--stats 500ms``, writing to a **file we tail** rather than a pipe, so a GUI
restart re-attaches at the byte offset checkpointed in ``runs.log_offset``
instead of replaying the log — which would duplicate every conflict and activity
row.

``--color NEVER`` is mandatory. Without it ``msg`` carries raw ANSI escapes even
in JSON mode (``"msg":"\\x1b[2mSetting --ignore-listing-checksum …\\x1b[0m"``
**[V]**) and the ``- Path1   …`` columns are padded differently.

Three record shapes come out of rclone v1.75.0, all measured **[V]**::

    {"time":…,"level":"info","msg":"Copying Path2 files to Path1",
     "source":"bisync/resync.go:44"}

    {"time":…,"level":"info","msg":"Copied (new)","size":6,"object":"a.txt",
     "objectType":"*local.Object","source":"operations/copy.go:380"}

    {"time":…,"level":"notice","msg":"\\nTransferred: …",
     "stats":{"bytes":6,…,"transferring":[…]},"source":"accounting/stats.go:551"}

**The verdict is read from the log, never from the exit code.** Exit 130 is
genuinely ambiguous: the same signal produces both

* ``NOTICE: Graceful shutdown completed successfully.`` + ``INFO : Bisync
  successful`` — a clean, resumable state — and
* ``ERROR : Bisync critical error: chtimes …huge.bin.f19291e9.partial: no such
  file or directory`` + ``ERROR : Bisync aborted. Must run --resync to
  recover.`` — with ``.lst`` renamed to ``.lst-err``

both measured on this machine, from the same command with different timing
**[V]**. :func:`classify_verdict` therefore ranks the log's own terminal
evidence above the exit status, and the *specific* safety aborts above the
generic "Bisync aborted." line that always accompanies them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QThread, Signal

from onedriveui import errors as _errors
from onedriveui.models import RunVerdict

__all__ = [
    "BENIGN_BISYNC_PATTERNS",
    "EXIT_CRITICAL",
    "EXIT_NON_CRITICAL",
    "EXIT_OK",
    "EXIT_SIGINT",
    "EXIT_USAGE",
    "LOG_LEVELS",
    "MILESTONES",
    "TERMINAL_RULES",
    "LogRecord",
    "LogTailer",
    "classify_verdict",
    "conflict_path",
    "is_benign",
    "milestone",
    "parse_record",
    "parse_text",
    "stats_counts",
    "strip_rcd_prefix",
]

log = logging.getLogger(__name__)

#: rclone's exit codes for bisync, all verified against the running binary.
EXIT_OK: Final[int] = 0
#: Non-critical: a rerun may succeed. `--max-delete`, "all files changed",
#: "prior lock file found" **[V]**.
EXIT_NON_CRITICAL: Final[int] = 1
#: A flag rclone does not know — cobra prints usage and never initialises its
#: logger, so the output is plain text, not JSON **[V]**.
EXIT_USAGE: Final[int] = 2
#: Critical: `--resync` required. Missing listings, a changed filters file, a
#: `--check-access` failure, an empty listing **[V]**.
EXIT_CRITICAL: Final[int] = 7
#: SIGINT — the Graceful Shutdown path, and **ambiguous on its own** **[V]**.
EXIT_SIGINT: Final[int] = 130

#: The levels rclone's JSON logger emits, lowercase in the JSON and uppercase in
#: the plain log.
LOG_LEVELS: Final[tuple[str, ...]] = (
    "debug", "info", "notice", "warning", "error", "critical")

#: One leading ``[<date> <time> ]<LEVEL>[ ]*: `` group. Applied repeatedly by
#: :func:`strip_rcd_prefix`, because a bisync line relayed through ``rcd``
#: arrives **double-timestamped and level-shifted**:
#: ``NOTICE: 2026/08/30 23:40:09 ERROR : Safety abort: …``.
_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?"
    r"(?:" + "|".join(LOG_LEVELS) + r")\s*:\s+",
    re.IGNORECASE)

#: How many prefixes to peel. Two is what ``rcd`` produces; four is slack.
_MAX_PREFIXES: Final[int] = 4

#: ANSI CSI sequences. ``--color NEVER`` removes them, and this is the belt to
#: that pair of braces: a log captured without the flag is still parseable.
_ANSI_RE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: rclone pads the ``- Path1   Queue copy to Path2   - /…`` lines into columns.
#: Collapsing runs of spaces is what makes those lines matchable.
_SPACES_RE: Final[re.Pattern[str]] = re.compile(r"[ \t]{2,}")

#: The conflict announcement, measured verbatim **[V]**::
#:
#:     NOTICE: - WARNING           New or changed in both paths   - a.txt
#:
#: The JSON record carries **no** ``object`` field for this line — only ``msg`` —
#: which is why the path has to be pulled out of the text.
_CONFLICT_RE: Final[re.Pattern[str]] = re.compile(
    r"^-\s+WARNING\s+New or changed in both paths\s+-\s+(?P<path>.+?)\s*$")

#: Lines that look alarming and are not, **specific to a bisync run**. The
#: shared ones — ``Ignoring --track-renames``, ``WARNING listing try N failed``,
#: ``lock file renewed for`` — already live in
#: :data:`onedriveui.errors.BENIGN_PATTERNS` and are never restated here.
BENIGN_BISYNC_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    #: Our own SIGINT, working exactly as designed (I13).
    re.compile(r"Ignoring sync error due to Graceful Shutdown"),
    re.compile(r"Canceling Sync if not done in:"),
    re.compile(r"Graceful shutdown completed successfully"),
    re.compile(r"\bcontext canceled\b"),
    #: Emitted on every default run since v1.66 — informational, not a warning.
    re.compile(r"Setting --ignore-listing-checksum as neither"),
    #: rclone silently raising a too-small --max-lock to its 2-minute floor.
    re.compile(r"--max-lock cannot be shorter than 2 minutes"),
    re.compile(r"There was nothing to transfer"),
)

#: ``msg`` fragment -> the phase string a UI shows while it is the newest one
#: seen. Every fragment is copied from a real run **[V]**; order matters,
#: because several can appear in one run and the last match wins.
MILESTONES: Final[tuple[tuple[str, str], ...]] = (
    ("Building Path1 and Path2 listings", "listing"),
    ("Path1 checking for diffs", "comparing"),
    ("Path2 checking for diffs", "comparing"),
    ("Copying Path2 files to Path1", "transferring"),
    ("Applying changes", "transferring"),
    ("Do queued copies to", "transferring"),
    ("Resync is copying files to", "transferring"),
    ("Updating listings", "finishing"),
    ("Resync updating listings", "finishing"),
    ("Validating listings for Path1", "finishing"),
    ("Checking access health", "comparing"),
    ("Bisync successful", "done"),
)

#: ``(fragment, verdict)`` in **first-match-wins priority order**, applied to the
#: whole log. The specific safety aborts come first because each of them also
#: emits the generic ``Bisync aborted.`` line, and the specific one is the
#: actionable fact; ``Access test failed`` outranks ``Must run --resync`` for the
#: same reason — a resync alone would not fix a missing ``RCLONE_TEST`` and the
#: run would abort again. Every fragment below was captured from a real run on
#: this machine **[V]**.
TERMINAL_RULES: Final[tuple[tuple[str, RunVerdict], ...]] = (
    ("Safety abort: too many deletes", RunVerdict.ABORTED_MAXDELETE),
    ("Safety abort: all files were changed", RunVerdict.ABORTED_ALLCHANGED),
    ("prior lock file found", RunVerdict.LOCKED),
    ("Lock file exists, but contents are unreadable", RunVerdict.LOCKED),
    ("Access test failed", RunVerdict.ACCESS_DENIED),
    ("check file check failed", RunVerdict.ACCESS_DENIED),
    ("Must run --resync to recover", RunVerdict.NEEDS_RESYNC),
    ("Error is retryable without --resync", RunVerdict.CRITICAL_SOFT),
    ("Bisync aborted. Please try again.", RunVerdict.RETRYABLE),
    ("Bisync successful", RunVerdict.OK),
)


# ─────────────────────────────────────────────────────────────────────────────
# One record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LogRecord:
    """One parsed line of ``bisync.jsonl``.

    Attributes:
        time: rclone's ``time`` — RFC3339Nano with the local UTC offset.
        level: Lowercase: one of :data:`LOG_LEVELS`.
        msg: The message, ANSI-stripped and with its ``rcd`` prefixes peeled.
        source: ``<pkg>/<file>.go:<line>``. Stable enough to key on —
            ``bisync/resolve.go:318`` is a conflict rename.
        object: The object's path **relative to the fs root**, on object-scoped
            records only. ``""`` otherwise — notably on the conflict NOTICEs,
            which carry no ``object`` field at all **[V]**.
        object_type: ``*local.Object``, ``*onedrive.Object``, …
        size: Bytes; present on copy records only.
        stats: The machine-readable stats block, on stats records only.
        raw: The original line, for the diagnostics bundle.
        offset: The byte offset **after** this line, i.e. the resume point once
            this record has been handled. This is what
            ``runs.log_offset`` stores.
    """

    time: str = ""
    level: str = "info"
    msg: str = ""
    source: str = ""
    object: str = ""
    object_type: str = ""
    size: int = 0
    stats: dict[str, Any] | None = None
    raw: str = ""
    offset: int = 0

    @property
    def is_stats(self) -> bool:
        """Whether this record carries the ``stats`` block."""
        return self.stats is not None

    @property
    def is_object(self) -> bool:
        """Whether this record names a specific object."""
        return bool(self.object)

    @property
    def is_error(self) -> bool:
        """Whether rclone logged this at ``error`` or ``critical``."""
        return self.level in ("error", "critical")


def strip_rcd_prefix(text: str) -> str:
    """Peel the ``<time> <LEVEL>: `` prefixes off a log line.

    A bisync line relayed through an ``rcd`` daemon arrives wrapped twice —
    measured **[V]**::

        NOTICE: 2026/08/30 23:40:09 ERROR : Safety abort: too many deletes …

    and a plain (non-JSON) bisync log line is wrapped once::

        2026/08/31 20:36:03 ERROR : Safety abort: too many deletes …

    Any parser that matches on the message must strip both, or a rule keyed on
    ``"ERROR : Bisync aborted"`` silently stops matching when the same line
    arrives through the daemon.

    Args:
        text: One log line, or one ``msg`` field.

    Returns:
        The innermost message, ANSI escapes removed and trailing whitespace
        trimmed. Text with no prefix is returned unchanged apart from that.
    """
    out = _ANSI_RE.sub("", str(text))
    for _ in range(_MAX_PREFIXES):
        stripped = _PREFIX_RE.sub("", out, count=1)
        if stripped == out:
            break
        out = stripped
    return out.rstrip()


def parse_record(line: str | bytes, *, offset: int = 0) -> LogRecord | None:
    """Parse one line of the log, in any of the three shapes rclone emits.

    Args:
        line: The raw line, with or without its newline. Bytes are decoded as
            UTF-8 with replacement — a torn multi-byte character at the tail of a
            partially flushed file must not raise.
        offset: The byte offset **after** this line, stored on the record so the
            caller can checkpoint it.

    Returns:
        The record, or ``None`` for a blank line or for non-JSON output. Non-JSON
        happens for real: a flag error is printed by cobra **before** rclone's
        logger exists, as plain usage text on stdout/stderr **[V]**, so
        ``json.loads`` must always be guarded.
    """
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
    text = text.strip("\r\n")
    if not text.strip():
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None

    stats = body.get("stats")
    return LogRecord(
        time=str(body.get("time", "") or ""),
        level=str(body.get("level", "info") or "info").lower(),
        msg=strip_rcd_prefix(str(body.get("msg", "") or "")),
        source=str(body.get("source", "") or ""),
        object=str(body.get("object", "") or ""),
        object_type=str(body.get("objectType", "") or ""),
        size=int(body.get("size", 0) or 0),
        stats=dict(stats) if isinstance(stats, Mapping) else None,
        raw=text,
        offset=int(offset),
    )


def parse_text(text: str) -> list[LogRecord]:
    """Parse a whole log, keeping each record's resume offset.

    Args:
        text: The log file's contents.

    Returns:
        One record per parseable line, each carrying the byte offset that
        follows it. Unparseable lines are skipped but still advance the offset,
        so a resume point is never wrong because of them.
    """
    out: list[LogRecord] = []
    position = 0
    for line in text.splitlines(keepends=True):
        position += len(line.encode("utf-8"))
        record = parse_record(line, offset=position)
        if record is not None:
            out.append(record)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Filtering and interpretation
# ─────────────────────────────────────────────────────────────────────────────

def _text_of(item: LogRecord | Mapping[str, Any] | str) -> str:
    """The message of a record, a raw mapping or a plain string."""
    if isinstance(item, LogRecord):
        return item.msg
    if isinstance(item, Mapping):
        return strip_rcd_prefix(str(item.get("msg", "") or ""))
    return strip_rcd_prefix(str(item))


def is_benign(item: LogRecord | Mapping[str, Any] | str) -> bool:
    """Whether a log line that looks like a failure is actually harmless.

    Two families, kept apart on purpose:

    * the application-wide ones in
      :data:`onedriveui.errors.BENIGN_PATTERNS` — ``Ignoring --track-renames as
      it doesn't work with copy or move`` (emitted at **ERROR** level on every
      ``--resync`` **[V]**), ``WARNING listing try N failed``, ``lock file
      renewed for``, ``vfs cache: detected external removal``;
    * :data:`BENIGN_BISYNC_PATTERNS`, which are the traces of our **own**
      ``SIGINT`` (I13) and of rclone's routine bookkeeping.

    Intermediate ``ERROR :`` lines are **not** failures when the run ends in
    ``Bisync successful`` — rclone retries internally. Only the terminal verdict
    counts, which is :func:`classify_verdict`'s job; this function is what keeps
    the retries out of the issue list on the way there.

    Args:
        item: A :class:`LogRecord`, a raw parsed JSON mapping, or a line of text.

    Returns:
        True when the line must not be surfaced to the user.
    """
    text = _text_of(item)
    if not text:
        return True
    if _errors.is_benign(text):
        return True
    return any(pattern.search(text) for pattern in BENIGN_BISYNC_PATTERNS)


def milestone(item: LogRecord | Mapping[str, Any] | str) -> str:
    """The UI phase a record announces, if any.

    Args:
        item: A record, a mapping or a line of text.

    Returns:
        One of ``"listing"``, ``"comparing"``, ``"transferring"``,
        ``"finishing"``, ``"done"``, or ``""`` when the line announces no phase
        change. Callers keep the newest non-empty answer.
    """
    text = _SPACES_RE.sub(" ", _text_of(item))
    found = ""
    for fragment, phase in MILESTONES:
        if fragment in text:
            found = phase
    return found


def conflict_path(item: LogRecord | Mapping[str, Any] | str) -> str | None:
    """The path of a file rclone has just declared conflicted.

    The announcement is a NOTICE whose JSON record carries **no** ``object``
    field — only ``msg`` — so the path has to come out of the padded text
    **[V]**::

        - WARNING           New or changed in both paths                - a.txt

    Args:
        item: A record, a mapping or a line of text.

    Returns:
        The conflicted path relative to the sync root, or ``None`` when the line
        is not a conflict announcement.
    """
    text = _SPACES_RE.sub(" ", _text_of(item)).strip()
    match = _CONFLICT_RE.match(text)
    return match.group("path") if match else None


def stats_counts(stats: Mapping[str, Any] | None) -> dict[str, int]:
    """The ``runs`` row's counters, out of one rclone stats block.

    Args:
        stats: A ``stats`` object from a stats record, or ``None``.

    Returns:
        ``{"files_transferred", "bytes", "deletes", "renames", "errors"}``,
        named to match :class:`~onedriveui.models.RunRecord`'s fields. All zero
        for ``None``.
    """
    data = stats or {}

    def count(key: str) -> int:
        try:
            return int(data.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "files_transferred": count("transfers"),
        "bytes": count("bytes"),
        "deletes": count("deletes"),
        "renames": count("renames"),
        "errors": count("errors"),
    }


def classify_verdict(log_text: str | Iterable[str | LogRecord],
                     exit_code: int | None = None) -> RunVerdict:
    """Decide how a bisync run actually ended, from its **log**.

    The exit code alone is not enough, and 130 is the reason. Both of these were
    produced on this machine by the same command with different timing **[V]**:

    ==== =================================================== ==================
    130  ``Graceful shutdown completed successfully.`` +      the workdir holds
         ``Bisync successful``                                a clean
                                                              ``.lst``/``.lst-old``
                                                              pair — resumable
    130  ``Bisync critical error: chtimes …partial: no such   ``.lst`` was
         file or directory`` + ``Bisync aborted. Must run     renamed to
         --resync to recover.``                               ``.lst-err`` —
                                                              locked out
    ==== =================================================== ==================

    So the log's terminal evidence is ranked first (:data:`TERMINAL_RULES`), and
    only a log that says nothing at all falls back to the exit code.

    Args:
        log_text: The log — the whole file's text, an iterable of lines, or an
            iterable of :class:`LogRecord`. Both the plain and the JSON forms
            work: every candidate line is passed through
            :func:`strip_rcd_prefix`, so a log relayed by ``rcd`` classifies
            identically.
        exit_code: The process exit status, when known. Used to distinguish
            :attr:`~RunVerdict.OK` from :attr:`~RunVerdict.CANCELLED` on a
            successful run, and as the last resort when the log is empty or
            truncated.

    Returns:
        The :class:`~onedriveui.models.RunVerdict`.
        :attr:`~RunVerdict.CANCELLED` is reserved for a **graceful** ``SIGINT``
        that still reached ``Bisync successful``; an interrupted run that ended
        badly gets the bad verdict, never ``CANCELLED``.
        :attr:`~RunVerdict.UNKNOWN` only when neither the log nor the exit code
        says anything — for instance a log lost to a crash before the first
        flush.
    """
    haystack = _haystack(log_text)

    for fragment, verdict in TERMINAL_RULES:
        if fragment in haystack:
            if verdict is RunVerdict.OK and exit_code == EXIT_SIGINT:
                return RunVerdict.CANCELLED
            return verdict

    if exit_code is None:
        return RunVerdict.UNKNOWN
    if exit_code == EXIT_OK:
        return RunVerdict.OK
    if exit_code == EXIT_SIGINT:
        return RunVerdict.CANCELLED
    if exit_code == EXIT_CRITICAL:
        return RunVerdict.NEEDS_RESYNC
    if exit_code == EXIT_NON_CRITICAL:
        return RunVerdict.RETRYABLE
    return RunVerdict.UNKNOWN


def _haystack(log_text: str | Iterable[str | LogRecord]) -> str:
    """Every candidate line of a log, prefix-stripped and newline-joined.

    Accepting all three shapes here is what lets one verdict function serve the
    live tailer (records), a re-read of ``bisync.jsonl`` (JSON text) and a plain
    ``-v`` capture pasted into a bug report (plain text).
    """
    if isinstance(log_text, str):
        lines: list[str] = []
        for raw in log_text.splitlines():
            record = parse_record(raw)
            lines.append(record.msg if record is not None else strip_rcd_prefix(raw))
        return "\n".join(lines)

    parts: list[str] = []
    for item in log_text:
        if isinstance(item, LogRecord):
            parts.append(item.msg)
            continue
        record = parse_record(item)
        parts.append(record.msg if record is not None else strip_rcd_prefix(str(item)))
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# The tailer
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class _TailState:
    """Where the tailer is, so :meth:`LogTailer.read_available` can be reused
    without a thread.

    ``offset`` is deliberately the **last complete-line boundary**, never "how
    much has been read": a partially flushed trailing line is simply re-read on
    the next pass. Buffering it instead would mean the same bytes arriving twice
    if a pass ever ran without consuming them, which is how a tailer duplicates
    a conflict row.
    """
    offset: int = 0


class LogTailer(QThread):
    """One short-lived thread per active bisync run (ARCHITECTURE §7.4).

    It reads ``<run_dir>/bisync.jsonl`` incrementally from a byte offset and
    emits parsed records. The offset is the whole point: a GUI restart resumes at
    ``runs.log_offset`` rather than replaying the log, which would duplicate
    every conflict and every activity row.

    **A partial line is never consumed.** The tailer only advances past
    ``\\n``-terminated lines and keeps the remainder in memory, so a record
    caught mid-flush is parsed once, whole, on the next pass — never twice, and
    never as broken JSON.

    Nothing here touches a ``QWidget`` or SQLite; it emits signals, which Qt
    delivers to the GUI thread with ``Qt.QueuedConnection`` because ``BUS`` and
    every consumer live there.

    Attributes:
        record: One :class:`LogRecord` per non-stats line.
        stats: The ``stats`` block of a stats record, as a plain dict.
        progressed: The new byte offset after each batch. Checkpoint this.
        ended: The final byte offset when the tailer stops, so the caller can
            store a resume point that is exactly where it left off.
    """

    record = Signal(object)          # LogRecord
    stats = Signal(dict)
    progressed = Signal(int)
    ended = Signal(int)

    def __init__(self, path: Path | str, *, offset: int = 0,
                 poll_ms: int = 250, follow: bool = True,
                 parent: Any = None) -> None:
        """
        Args:
            path: The log file. It need not exist yet — a run's first flush can
                lag the tailer's start, and the tailer simply waits.
            offset: Where to resume, from ``runs.log_offset``. An offset past the
                current end of the file (a truncated or replaced log) rewinds to
                0 rather than skipping the whole run.
            poll_ms: How long to sleep between passes when there is nothing new.
            follow: Keep polling until :meth:`stop`. When False the thread makes
                exactly one pass and ends, which is what a "catch up on an
                already-finished run" read wants.
            parent: Qt owner.
        """
        super().__init__(parent)
        self._path = Path(os.path.expanduser(str(path)))
        self._state = _TailState(offset=max(0, int(offset)))
        self._poll_ms = max(10, int(poll_ms))
        self._follow = bool(follow)
        self._stopping = False

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """The log file being tailed."""
        return self._path

    @property
    def offset(self) -> int:
        """The resume point: bytes of the log fully parsed and emitted."""
        return self._state.offset

    def stop(self) -> None:
        """Ask the thread to finish after its current pass.

        Idempotent, and safe from any thread. The caller should then
        ``wait()`` — the thread checkpoints its offset through
        :attr:`ended` before returning.
        """
        self._stopping = True

    # ── the work ────────────────────────────────────────────────────────────

    def read_available(self) -> list[LogRecord]:
        """Read and emit everything complete that has appeared since last time.

        Public and synchronous so a test — or a caller that already owns a
        worker — can drive the tailer with no thread at all.

        Returns:
            The records emitted by this pass, in file order. Empty when the file
            does not exist yet, has not grown, or has grown by only a partial
            line.
        """
        state = self._state
        try:
            size = self._path.stat().st_size
        except OSError:
            return []
        if size < state.offset:
            #: The log was truncated or replaced — a fresh run reusing the
            #: directory. Replaying from 0 is right; skipping to the old offset
            #: would silently drop the new run's beginning.
            log.info("bisync log %s shrank (%d < %d); restarting the tail",
                     self._path, size, state.offset)
            state.offset = 0
        if size == state.offset:
            return []

        try:
            with open(self._path, "rb") as handle:
                handle.seek(state.offset)
                chunk = handle.read(size - state.offset)
        except OSError as exc:
            log.warning("could not read %s: %s", self._path, exc)
            return []
        if not chunk:
            return []

        complete, separator, remainder = chunk.rpartition(b"\n")
        if not separator:
            #: Nothing complete yet. Leave the offset where it is; the partial
            #: line is re-read, whole, on the next pass.
            return []
        state.offset += len(chunk) - len(remainder)

        out: list[LogRecord] = []
        for line in complete.split(b"\n"):
            if not line.strip():
                continue
            parsed = parse_record(line, offset=state.offset)
            if parsed is None:
                continue
            out.append(parsed)
        for parsed in out:
            if parsed.is_stats:
                self.stats.emit(dict(parsed.stats or {}))
            else:
                self.record.emit(parsed)
        if out:
            self.progressed.emit(state.offset)
        return out

    def run(self) -> None:
        """The thread body: poll until :meth:`stop`, then report the offset.

        Blocking sleeps only; no Qt event loop, no widgets, no database.
        """
        try:
            while True:
                self.read_available()
                if not self._follow or self._stopping:
                    break
                time.sleep(self._poll_ms / 1000.0)
                if self._stopping:
                    #: One last pass, so the terminal verdict line written
                    #: microseconds before the stop request is never lost.
                    self.read_available()
                    break
        finally:
            self.ended.emit(self._state.offset)

    # ── convenience ─────────────────────────────────────────────────────────

    def drain(self) -> list[LogRecord]:
        """Read the rest of the file in one synchronous pass.

        Used when adopting an orphaned run whose process has already exited:
        there is nothing to follow, only a backlog to catch up on.

        Returns:
            Every record from the current offset to the end of the file.
        """
        out: list[LogRecord] = []
        while True:
            batch = self.read_available()
            if not batch:
                break
            out.extend(batch)
        return out

    def verdict(self, exit_code: int | None = None) -> RunVerdict:
        """Classify the whole log on disk, not just what this tailer has seen.

        The terminal line can be written after the last pass, and a resumed
        tailer starts mid-file, so the verdict is always taken from the complete
        file.

        Args:
            exit_code: The process exit status, when known.

        Returns:
            The :class:`~onedriveui.models.RunVerdict`.
        """
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        return classify_verdict(text, exit_code)
