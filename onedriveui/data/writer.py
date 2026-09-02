"""The one thread that writes to SQLite.

ARCHITECTURE §7.2. Every mutation in the application is a callable submitted
here; nothing else in the process holds a read-write connection, and
:func:`~onedriveui.data.db.open_rw` refuses to give one to a second thread, so
that rule is enforced rather than reviewed.

**Why a thread at all.** ``synchronous=NORMAL`` still fsyncs at every WAL
checkpoint, and a checkpoint behind a busy VFS cache can take tens of
milliseconds. On the GUI thread that is a visible stutter in the tray spinner.
Moving every write to one thread means the GUI never blocks on ``fsync``, and
one thread means the writes cannot deadlock against each other.

**Why batching.** A single sync tick can produce a hundred row updates. Each in
its own transaction is a hundred fsyncs; all of them in one is one. The queue is
drained into a single transaction every :data:`~onedriveui.constants.DB_FLUSH_MS`
milliseconds, which caps the cost of observability data at ten transactions a
second no matter how noisy rclone gets.

**Why ``urgent``.** Batching means a crash can lose the last ≤100 ms of writes.
That is fine for an activity row — it is a record of something rclone already
did — and unacceptable for a ``latches`` row or an answered ``decisions`` row,
which are the things that make crash recovery correct. Those go in with
``urgent=True``, which commits immediately, and :meth:`DbWriter.submit_sync`
does not return until the commit has happened, so the UI never claims a
decision is recorded before it durably is.

The ordering guarantee is FIFO across every producer thread: one
:class:`queue.Queue` feeds one consumer, so two writes submitted in an order
are applied in that order even from different threads.
"""

from __future__ import annotations

import atexit
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final

from PySide6.QtCore import QThread

from onedriveui import paths
from onedriveui.bus import BUS
from onedriveui.constants import DB_FLUSH_MS
from onedriveui.data import db

__all__ = ["DbWriter", "WRITER", "WriteOp", "Op"]

#: A unit of work: it is handed the read-write connection and may do anything
#: with it except commit — the writer owns the transaction.
Op = Callable[[sqlite3.Connection], Any]

#: How long an otherwise-empty batch waits for a straggler before committing.
#: Small enough that a lone write is durable in single-digit milliseconds, large
#: enough that a burst arriving from four threads is not split into one
#: transaction per row.
_IDLE_GRACE_S: Final[float] = 0.005

#: Hard ceiling on one transaction, so a runaway producer cannot build an
#: unbounded statement batch and a single rollback cannot lose an unbounded
#: amount of work.
_MAX_BATCH: Final[int] = 5_000

#: Wake-up interval while the queue is empty, so :meth:`DbWriter.stop` is
#: observed promptly even if no work ever arrives.
_POLL_S: Final[float] = 0.05


@dataclass(slots=True)
class WriteOp:
    """One submitted mutation and, when synchronous, its result channel."""

    fn: Op
    urgent: bool = False
    label: str = ""
    done: threading.Event | None = None
    result: Any = None
    error: BaseException | None = None
    #: Monotonic submit time, for the debug counters.
    queued_at: float = field(default_factory=time.monotonic)


class DbWriter(QThread):
    """The single SQLite writer thread.

    Example:
        >>> writer = DbWriter(path)                      # doctest: +SKIP
        >>> writer.start_writer()                        # doctest: +SKIP
        >>> writer.submit(lambda c: c.execute(SQL, args))
        >>> writer.submit_sync(set_latch, urgent=True)   # durable on return
        >>> writer.stop()                                # doctest: +SKIP

    Every public method is safe to call from any thread. Nothing here touches a
    ``QWidget``, and no signal is emitted from inside a transaction.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        flush_ms: int = DB_FLUSH_MS,
        parent: Any = None,
    ) -> None:
        """
        Args:
            path: The database file. Defaults to
                :func:`onedriveui.paths.db_file`.
            flush_ms: The batch window. The default is
                :data:`~onedriveui.constants.DB_FLUSH_MS` (100 ms), which is the
                documented bound on how much observability data a crash can
                cost.
            parent: Qt parent, if any.
        """
        super().__init__(parent)
        self._path = Path(path) if path is not None else None
        self._queue: queue.Queue[WriteOp | None] = queue.Queue()
        self._window_s = max(0.0, float(flush_ms) / 1000.0)
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._open_error: BaseException | None = None
        self._conn: sqlite3.Connection | None = None
        # Counters, read by tests and the About page. Written only by the
        # writer thread, read by anyone: int assignment is atomic under the GIL.
        self.batches = 0
        self.ops_applied = 0
        self.ops_failed = 0
        self.transaction_errors = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    @property
    def path(self) -> Path:
        """The database file this writer owns."""
        return self._path if self._path is not None else paths.db_file()

    @property
    def queue_depth(self) -> int:
        """How many operations are waiting. Approximate by nature."""
        return self._queue.qsize()

    def start_writer(self, *, timeout_ms: int = 5_000) -> bool:
        """Start the thread and wait until the connection is open.

        Args:
            timeout_ms: How long to wait for the connection.

        Returns:
            True when the writer is running and its connection is open.

        Raises:
            SafetyRefusal: If the database is under a FUSE mount or another
                thread already owns the read-write connection.
            sqlite3.Error: If the database cannot be opened.

        Waiting matters: a caller that submitted immediately after ``start()``
        would have no way to learn that the database failed to open, and the
        first symptom would be silently lost writes.
        """
        if self.isRunning():
            return self._ready.is_set()
        self._stopping.clear()
        self._ready.clear()
        self._open_error = None
        self.start()
        self._ready.wait(max(0.0, timeout_ms / 1000.0))
        if self._open_error is not None:
            error, self._open_error = self._open_error, None
            raise error
        return self._ready.is_set()

    def run(self) -> None:
        """The thread body: open the connection, then batch until stopped.

        Never called directly — ``QThread.start()`` calls it. The connection is
        opened *here*, inside the thread, because a SQLite connection belongs to
        the thread that created it and :func:`~onedriveui.data.db.open_rw`
        refuses to share one across threads.
        """
        try:
            self._conn = db.open_rw(self.path)
            db.migrate(self._conn)
        except BaseException as exc:  # noqa: BLE001 - reported to the starter
            self._open_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                batch = self._collect()
                if batch is None:
                    break
                if batch:
                    self._apply(batch)
        finally:
            self._drain_remaining()
            self._conn = None
            db.close_rw(self.path)

    def _collect(self) -> list[WriteOp] | None:
        """Block for the first operation, then accumulate a batch.

        Returns:
            The operations to commit together, or ``None`` when the writer has
            been told to stop. An empty list means "nothing arrived, loop
            again" — never an empty transaction, which is what keeps the batch
            count proportional to the work rather than to the wall clock.
        """
        try:
            first = self._queue.get(timeout=_POLL_S)
        except queue.Empty:
            return None if self._stopping.is_set() else []
        if first is None:
            return None

        batch = [first]
        if first.urgent:
            return batch
        deadline = time.monotonic() + self._window_s
        while len(batch) < _MAX_BATCH:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=min(remaining, _IDLE_GRACE_S))
            except queue.Empty:
                # The producers have gone quiet: commit now rather than holding
                # a transaction open for the rest of the window.
                break
            if item is None:
                self._stopping.set()
                self._queue.put(None)
                break
            batch.append(item)
            if item.urgent:
                break
        return batch

    def _apply(self, batch: list[WriteOp]) -> None:
        """Run one batch inside a single transaction, then release its waiters.

        A failing operation does not poison its neighbours: its exception is
        recorded on its own :class:`WriteOp` and the batch commits. A failure of
        the *transaction* — a disk error, a checkpoint that could not take the
        lock — rolls the whole batch back and reports it to every waiter, which
        is the only case where an already-"applied" operation is undone.
        """
        conn = self._conn
        if conn is None:  # pragma: no cover - only during teardown
            for op in batch:
                op.error = RuntimeError("DbWriter connection is closed")
                self._release(op)
            return

        applied = 0
        failed = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            self.transaction_errors += 1
            self._report(f"could not begin a transaction: {exc}")
            for op in batch:
                op.error = exc
                self._release(op)
            return

        for op in batch:
            try:
                op.result = op.fn(conn)
                applied += 1
            except BaseException as exc:  # noqa: BLE001 - isolated per op
                op.error = exc
                failed += 1
                self._report(
                    f"write failed{f' [{op.label}]' if op.label else ''}: "
                    f"{exc.__class__.__name__}: {exc}")
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self.transaction_errors += 1
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - already rolled back
                pass
            self._report(f"commit failed, {len(batch)} write(s) rolled back: {exc}")
            for op in batch:
                if op.error is None:
                    op.error = exc
                    applied -= 1
                    failed += 1
        self.batches += 1
        self.ops_applied += max(0, applied)
        self.ops_failed += failed
        for op in batch:
            self._release(op)

    def _drain_remaining(self) -> None:
        """Apply whatever is still queued at shutdown, in one last batch.

        Called from ``run``'s ``finally``, so a stop request never silently
        discards work a caller believed was accepted.
        """
        leftovers: list[WriteOp] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                leftovers.append(item)
        if leftovers and self._conn is not None:
            self._apply(leftovers)
        else:
            for op in leftovers:
                op.error = RuntimeError("DbWriter stopped before this write ran")
                self._release(op)

    @staticmethod
    def _release(op: WriteOp) -> None:
        """Wake a :meth:`submit_sync` caller."""
        if op.done is not None:
            op.done.set()

    @staticmethod
    def _report(message: str) -> None:
        """Push a failure onto the log pane without importing the log module.

        ``BUS.log_line`` is queued when it crosses a thread, so this is safe
        from inside the writer and cannot re-enter it.
        """
        try:
            BUS.log_line.emit(f"DbWriter: {message}")
        except RuntimeError:  # pragma: no cover - BUS torn down at exit
            pass

    # ── submission ───────────────────────────────────────────────────────────
    def submit(self, op: Op, *, urgent: bool = False, label: str = "") -> None:
        """Queue a mutation and return immediately.

        Args:
            op: A callable taking the read-write connection. It must not commit,
                begin or roll back — the writer owns the transaction.
            urgent: Commit this operation's batch at once instead of waiting out
                the batch window. Use for ``latches`` and ``decisions``, whose
                loss would break crash recovery.
            label: A short name used in the log line if the write fails.

        Raises:
            RuntimeError: If the writer has been stopped. A silently dropped
                write is far worse than a loud one.
        """
        if self._stopping.is_set():
            raise RuntimeError("DbWriter is stopping; write rejected")
        if not self.isRunning():
            self.start_writer()
        self._queue.put(WriteOp(fn=op, urgent=bool(urgent), label=label))

    def submit_sync(
        self,
        op: Op,
        timeout_ms: int = 5_000,
        *,
        urgent: bool = True,
        label: str = "",
    ) -> Any:
        """Queue a mutation and wait until it has been committed.

        Args:
            op: A callable taking the read-write connection.
            timeout_ms: How long to wait for the commit.
            urgent: Commit at once. True by default — a caller that is blocking
                on the result almost always needs durability, and this is the
                path ``latches`` and ``decisions`` take.
            label: A short name used in the log line if the write fails.

        Returns:
            Whatever `op` returned. **The transaction has committed before this
            returns**, so a second connection can already read the row.

        Raises:
            RuntimeError: If the writer is stopped, or the timeout expires with
                the operation still queued.
            BaseException: Whatever `op` raised, re-raised in the caller's
                thread so a failed write cannot be mistaken for a successful
                one.
        """
        if self._stopping.is_set():
            raise RuntimeError("DbWriter is stopping; write rejected")
        if not self.isRunning():
            self.start_writer()
        if self.isCurrentThread():  # pragma: no cover - guarded misuse
            # Waiting on the thread that would have to do the work is a
            # guaranteed deadlock; say so instead of hanging for the timeout.
            raise RuntimeError("submit_sync() called from the writer thread")

        done = threading.Event()
        work = WriteOp(fn=op, urgent=bool(urgent), label=label, done=done)
        self._queue.put(work)
        if not done.wait(max(0.0, timeout_ms / 1000.0)):
            raise RuntimeError(
                f"DbWriter.submit_sync timed out after {timeout_ms} ms"
                f"{f' [{label}]' if label else ''}")
        if work.error is not None:
            raise work.error
        return work.result

    def isCurrentThread(self) -> bool:  # noqa: N802 - matches Qt's naming
        """True when the caller is running on this writer's thread."""
        return QThread.currentThread() is self

    def flush(self, timeout_ms: int = 5_000) -> bool:
        """Block until everything queued so far has been committed.

        Args:
            timeout_ms: How long to wait.

        Returns:
            True when the queue was drained. False when the writer was not
            running, in which case there was nothing to flush.

        Implemented as a no-op write, so it inherits the FIFO guarantee: when
        the marker commits, everything submitted before it has committed too.
        """
        if not self.isRunning():
            return False
        self.submit_sync(lambda _conn: None, timeout_ms, urgent=True,
                         label="flush")
        return True

    def stop(self, timeout_ms: int = 5_000) -> bool:
        """Flush, close the connection and join the thread.

        Args:
            timeout_ms: How long to wait for the thread to finish.

        Returns:
            True if the thread exited within the timeout.

        Idempotent. Anything still queued is applied first: a stop must not lose
        a write the caller believed was accepted.
        """
        if not self.isRunning():
            self._stopping.set()
            return True
        self._stopping.set()
        self._queue.put(None)
        finished = self.wait(timeout_ms)
        if not finished:  # pragma: no cover - a wedged filesystem
            self._report("thread did not exit within the shutdown timeout")
        return bool(finished)


#: The application's writer. Constructed but **not started** at import: starting
#: a thread as a side effect of an import would open the database before
#: ``integrity_check()`` has had a chance to move a corrupt one aside. ``app.py``
#: calls ``WRITER.start_writer()`` once, after the integrity check.
WRITER: Final[DbWriter] = DbWriter()


@atexit.register
def _stop_writer_at_exit() -> None:
    """Join the writer thread before the interpreter tears the process down.

    ``Application.quit()`` already stops it, and that is where the lifecycle
    belongs — but ``submit()`` starts the writer on demand, so *any* caller can
    bring the thread up in a process that never built an ``Application``. The
    Nautilus extension, a one-off script and the test suite are all such
    processes, and none of them has a shutdown step to hook.

    Left running, the thread outlives the interpreter's own teardown: PySide
    destroys the ``QApplication``, Qt finds a live ``QThread`` under it and
    calls ``qFatal()``, and the process dies with SIGABRT and a core dump after
    its work is already done. Stopping here is idempotent and costs nothing when
    the writer was never started.
    """
    try:
        if WRITER.isRunning():
            WRITER.stop()
    except Exception:  # noqa: BLE001 - the interpreter is going away regardless
        pass
