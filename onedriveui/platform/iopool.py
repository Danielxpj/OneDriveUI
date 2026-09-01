"""`IOPool` — the four worker threads every blocking call in this codebase names.

Ten modules document a function as "Blocking: ``IOPool`` only (ARCHITECTURE
§7.6)". `ARCHITECTURE.md` §7.3 specifies the pool that sentence refers to: one
`QThreadPool`, `maxThreadCount = 4`, per-task-kind concurrency caps, a shared
cancellation token, progress by `Signal`, and no SQLite write connection opened
on a worker. §7.6 bans "any synchronous HTTP on the GUI thread" outright.

The pool did not exist. Every one of those blocking calls therefore ran inline
on the GUI thread — `QuotaService.refresh()`, which is a round trip to
Microsoft, was being driven straight off the supervisor tick, so a slow network
froze the tray flyout for as long as the cloud took to answer.

**What belongs here:** blocking filesystem work, through-FUSE reads, and
`rc.call_blocking()`. **What does not:** anything Qt-widget-shaped, any `Gio`
call (§7.6 bans those off the GUI thread), and any SQLite write — a task emits
records and `DbWriter` persists them, which is the whole reason `DbWriter`
owns a thread of its own.

Two design points are load-bearing.

**Results come back on the GUI thread.** `_Signals` is parented to the pool,
which lives on the GUI thread, so its affinity is the GUI thread and a `Signal`
emitted from a worker is delivered through the event loop rather than run inline
on the worker. Callers can therefore touch widgets in `on_done` without knowing
a thread was involved. The parent is also what keeps it alive: `QThreadPool`
takes C++ ownership of a started `QRunnable`, and PySide will otherwise collect
the Python wrapper — and the `_Signals` it holds — while the worker is still
inside `run()`, which surfaces as "Signal source has been deleted" raised on the
worker thread with the result lost. `_pump()` holds its own reference too.

**Per-kind caps are enforced by not starting the task**, never by blocking a
worker on a semaphore. With four threads and, say, five queued single-slot
`cache_scan` tasks, gating inside `run()` would park four threads on a lock and
deadlock the pool against itself. Instead the queue lives here and `_pump()`
starts only what fits.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from typing import Any, Callable, Final, Mapping

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal

log = logging.getLogger(__name__)

__all__ = [
    "MAX_THREADS", "KIND_LIMITS", "Cancelled", "CancelToken", "IOPool",
    "instance", "reset_singleton",
]

#: `ARCHITECTURE.md` §7.3. Four, not "however many cores": the work here is
#: blocking I/O against one FUSE mount and one loopback daemon, and more
#: concurrency against a single mount buys queueing, not throughput.
MAX_THREADS: Final[int] = 4

#: Per-kind concurrency, from the §7.3 table. A kind absent from this map is
#: capped at :data:`MAX_THREADS`, which is the pool's own limit anyway.
#:
#: `hydrate` is 3 rather than 4 so a hydration storm can never occupy every
#: thread and starve the scan that tells the UI what is cached. The single-slot
#: kinds are single because they are already sequential internally — two
#: concurrent cache scans would produce two conflicting `cache_index` rewrites.
KIND_LIMITS: Final[Mapping[str, int]] = {
    "hydrate": 3,
    "cache_scan": 1,
    "evict": 1,
    "preflight": 1,
    "kfm": 1,
    "thumbnail": 2,
    "tree_size": 1,
    #: Not in the §7.3 table: `rc.call_blocking()` from a scheduled job. Capped
    #: at 2 because these are loopback round trips that spend their time
    #: waiting, and because a stalled daemon must not consume the whole pool.
    "rc": 2,
}


class Cancelled(Exception):
    """Raised by :meth:`CancelToken.raise_if_cancelled` inside a task.

    Caught by the pool and reported as neither success nor failure: a cancelled
    task's `on_done` and `on_error` are both skipped, because the caller that
    cancelled it is no longer interested in either answer.
    """


class CancelToken:
    """Cooperative cancellation, shared by a group of tasks.

    Cooperative because there is no safe way to interrupt a blocking `read()`
    on a FUSE mount from outside: the task has to look. Long tasks check
    :attr:`cancelled` between units of work — per file, per directory, per
    4 MiB chunk — and return early.

    Backed by a `threading.Event`, so :meth:`wait` lets a caller block on the
    cancellation itself rather than polling it.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        """Has this token been cancelled?"""
        return self._event.is_set()

    def cancel(self) -> None:
        """Cancel every task holding this token. Idempotent."""
        self._event.set()

    def raise_if_cancelled(self) -> None:
        """Abort the current task if the token is cancelled.

        Raises:
            Cancelled: If :meth:`cancel` has been called.
        """
        if self._event.is_set():
            raise Cancelled()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled.

        Args:
            timeout: Seconds, or `None` to wait forever.

        Returns:
            True if the token was cancelled, False if the wait timed out.
        """
        return self._event.wait(timeout)


class _Signals(QObject):
    """The reply channel for one task.

    Constructed on the calling (GUI) thread so its affinity is that thread and
    every emission from a worker is queued rather than run inline. This object
    is what makes `submit()` safe to use from widget code.
    """

    done = Signal(object)
    failed = Signal(object)
    progress = Signal(object)
    #: Internal: the task left the worker, whatever the outcome. Drives
    #: `_pump()`, and is separate from `done`/`failed` so a caller that raises
    #: in its own `on_done` slot cannot stop the pool from starting the next
    #: task.
    retired = Signal(str)


class _Task(QRunnable):
    """One callable on the pool.

    The callable is invoked with the token appended as a `token=` keyword when
    it declares one, so a long task can cooperate without every caller having
    to close over the token by hand.
    """

    __slots__ = ("_fn", "_args", "_kwargs", "_signals", "_token", "_kind",
                 "_wants_token", "_wants_progress")

    def __init__(self, kind: str, fn: Callable[..., Any], args: tuple,
                 kwargs: dict, signals: _Signals, token: CancelToken,
                 wants_token: bool, wants_progress: bool = False) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._kind = kind
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._signals = signals
        self._token = token
        self._wants_token = wants_token
        self._wants_progress = wants_progress

    def release(self) -> None:
        """Drop the reply channel for a task that will never run."""
        signals, self._signals = self._signals, None
        if signals is not None:
            signals.deleteLater()

    def signals_are(self, signals: _Signals) -> bool:
        """Is this the task that owns `signals`? Used to release the reference."""
        return self._signals is signals

    def run(self) -> None:
        """Run the callable and report, then always retire.

        `BaseException` rather than `Exception`: a worker thread that lets an
        exception escape `run()` takes the whole pool thread down silently, and
        the caller's `on_error` is the only place the failure can still be
        reported.
        """
        try:
            if self._token.cancelled:
                return
            kwargs = dict(self._kwargs)
            if self._wants_token:
                kwargs["token"] = self._token
            if self._wants_progress:
                # `on_progress` was accepted, connected, and then never given
                # anything to emit through: the task had no way to report.
                # Handed over the same way the token is — only to a callable
                # that asks for it.
                kwargs["progress"] = self._signals.progress.emit
            try:
                result = self._fn(*self._args, **kwargs)
            except Cancelled:
                return
            except BaseException as exc:  # noqa: BLE001 - see the docstring
                # WARNING, not debug: with no `on_error` connected the signal
                # below goes nowhere, and a task that failed silently at the
                # default level is indistinguishable from one that worked.
                log.warning("IOPool task %r failed: %s",
                            self._kind, exc, exc_info=True)
                self._signals.failed.emit(exc)
            else:
                if not self._token.cancelled:
                    self._signals.done.emit(result)
        finally:
            if self._signals is not None:
                self._signals.retired.emit(self._kind)


class IOPool(QObject):
    """The four blocking-work threads, with per-kind admission control.

    Args:
        max_threads: Pool size. Defaults to :data:`MAX_THREADS`.
        parent: Qt parent.

    Attributes:
        idle: Emitted when the last queued and running task retires. The
            shutdown sequence in §7.5 waits on this rather than polling.
    """

    idle = Signal()

    def __init__(self, *, max_threads: int = MAX_THREADS,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(max_threads)))
        #: Kind -> tasks waiting for a slot of that kind.
        self._queued: dict[str, deque[_Task]] = defaultdict(deque)
        #: Kind -> how many are running right now.
        self._running: dict[str, int] = defaultdict(int)
        #: Tokens with work still outstanding, so `cancel_all()` can reach them.
        #: A set, and pruned as tasks retire: an append-only list meant an O(n)
        #: scan on every submission and a `threading.Event` kept for the life of
        #: the process for every task ever run.
        self._tokens: set[CancelToken] = set()
        #: Tasks handed to the pool and not yet retired. See `_pump`.
        self._live: set[_Task] = set()

    # ── submission ──────────────────────────────────────────────────────────

    def submit(self, fn: Callable[..., Any], *args: Any,
               kind: str = "rc",
               on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[BaseException], None] | None = None,
               on_progress: Callable[[Any], None] | None = None,
               token: CancelToken | None = None,
               **kwargs: Any) -> CancelToken:
        """Run `fn` on a worker and deliver the result on the GUI thread.

        Args:
            fn: The blocking callable. If it declares a `token` parameter it is
                passed the :class:`CancelToken`, so it can return early.
            *args: Positional arguments for `fn`.
            kind: Which §7.3 concurrency class this is. Unknown kinds are
                capped at the pool size.
            on_done: Called with the return value, on the GUI thread. Not called
                if the task was cancelled or raised.
            on_error: Called with the exception, on the GUI thread.
            on_progress: Called with whatever the task emits through its
                progress channel, on the GUI thread.
            token: Share a token to make several tasks cancellable together.
                A fresh one is created when omitted.
            **kwargs: Keyword arguments for `fn`.

        Returns:
            The token governing this task.
        """
        token = token if token is not None else CancelToken()
        self._tokens.add(token)

        # Parented to the pool, which lives on the GUI thread: that is what
        # gives the signals GUI-thread affinity *and* keeps the C++ object alive
        # for the task's whole run.
        signals = _Signals(self)
        if on_done is not None:
            signals.done.connect(on_done)
        if on_error is not None:
            signals.failed.connect(on_error)
        if on_progress is not None:
            signals.progress.connect(on_progress)
        # Explicitly queued: `retired` is emitted from the worker, and `_pump`
        # touches the queues, which belong to this thread.
        signals.retired.connect(
            lambda finished_kind, sig=signals: self._on_retired(finished_kind, sig),
            Qt.ConnectionType.QueuedConnection)

        task = _Task(kind, fn, args, kwargs, signals, token,
                     _accepts(fn, "token"), _accepts(fn, "progress"))
        self._queued[kind].append(task)
        self._pump()
        return token

    def _pump(self) -> None:
        """Start every queued task whose kind has a free slot.

        Called on the GUI thread only: on submission, and on the queued
        `retired` signal. Nothing here blocks — a task that cannot start yet
        stays in its queue, so a worker thread is never parked on admission.
        """
        for kind, waiting in self._queued.items():
            limit = KIND_LIMITS.get(kind, self._pool.maxThreadCount())
            while waiting and self._running[kind] < limit:
                task = waiting.popleft()
                self._running[kind] += 1
                # Hold a Python reference for the whole run. `QThreadPool.start()`
                # hands ownership to C++, and PySide is then free to collect the
                # Python `_Task` wrapper — taking `_Signals` with it — while the
                # worker is still inside `run()`. The symptom is
                # "RuntimeError: Signal source has been deleted" raised from the
                # worker thread, and the task's result is lost with it.
                self._live.add(task)
                self._pool.start(task)

    def _on_retired(self, kind: str, signals: _Signals | None = None) -> None:
        self._running[kind] = max(0, self._running[kind] - 1)
        if signals is not None:
            for task in list(self._live):
                if task.signals_are(signals):
                    self._live.discard(task)
                    break
            # The reply channel has done its job. `deleteLater` rather than a
            # plain drop: we are inside its own signal's delivery.
            signals.deleteLater()
        self._pump()
        if self.idle_now:
            # Nothing outstanding: no token can still be governing work, so the
            # whole set goes rather than growing for the life of the process.
            self._tokens.clear()
            self.idle.emit()

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def active(self) -> int:
        """How many tasks are running right now."""
        return sum(self._running.values())

    @property
    def pending(self) -> int:
        """How many tasks are queued but not started."""
        return sum(len(q) for q in self._queued.values())

    @property
    def idle_now(self) -> bool:
        """Nothing running and nothing queued."""
        return self.active == 0 and self.pending == 0

    @property
    def max_threads(self) -> int:
        """The pool size."""
        return self._pool.maxThreadCount()

    # ── shutdown ────────────────────────────────────────────────────────────

    def cancel_all(self) -> None:
        """Cancel every token and drop everything not yet started.

        Step 2 of the §7.5 shutdown sequence.

        What this actually does, precisely, because the difference matters at
        shutdown: a **queued** task never starts. A **running** task stops early
        only if it looks — which means only if its callable declared a `token`
        parameter and checks it between units of work. A single blocking call
        that took no token (a `vfs/stats` round trip, one `operations/about`)
        runs to completion regardless; :meth:`wait_for_done` is what bounds
        that, not this.
        """
        for token in self._tokens:
            token.cancel()
        dropped = 0
        for waiting in self._queued.values():
            while waiting:
                task = waiting.popleft()
                # Release the reply channel too. Dropping the task alone left
                # its `_Signals` parented to this pool — alive, connected, and
                # never delivered — for the life of the process.
                task.release()
                dropped += 1
        self._queued.clear()
        self._tokens.clear()
        if dropped:
            log.debug("IOPool dropped %d queued task(s) on cancel", dropped)

    def wait_for_done(self, msec: int = 3000) -> bool:
        """Block until every running task has left, or the timeout expires.

        Args:
            msec: Milliseconds to wait. §7.5 uses 3000.

        Returns:
            True if the pool drained, False on timeout — in which case the
            caller is shutting down anyway and the remaining tasks are daemon
            work that dies with the process.
        """
        return self._pool.waitForDone(int(msec))

    def shutdown(self, msec: int = 3000) -> bool:
        """Cancel everything, then wait. The whole of §7.5 step 2."""
        self.cancel_all()
        return self.wait_for_done(msec)


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    """Does `fn` declare a parameter called `name`?

    Inspected once per submission rather than required of every caller, so a
    plain `lambda: quota.refresh(force=True)` and a long cancellable walk that
    wants both `token` and `progress` can be submitted the same way.
    """
    try:
        import inspect

        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        # Builtins and C callables have no retrievable signature. Assuming they
        # want nothing is the safe answer.
        return False


# ═════════════════════════════════════════════════════════════════════════════
# The process-wide pool
# ═════════════════════════════════════════════════════════════════════════════

_POOL: IOPool | None = None


def instance() -> IOPool:
    """The one pool, created on first use.

    A singleton because §7.3 says *one* `QThreadPool` with four threads: two
    pools would be eight threads against one FUSE mount, which is the queueing
    the cap exists to prevent.
    """
    global _POOL
    if _POOL is None:
        _POOL = IOPool()
        log.debug("IOPool started with %d threads", _POOL.max_threads)
    return _POOL


def reset_singleton() -> None:
    """Drop the process-wide pool. For tests, and for a clean shutdown."""
    global _POOL
    if _POOL is not None:
        _POOL.shutdown()
    _POOL = None
