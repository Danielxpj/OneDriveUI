"""The mandatory GLib main-context pump — the critical path of the platform layer.

Qt owns this process's event loop. GLib's default `MainContext` therefore never
gets iterated unless we iterate it ourselves, and every `Gio` object we rely on
delivers its callbacks through that context:

* `Gio.DBusConnection` signal subscriptions — `ActionInvoked`,
  `NotificationClosed`, `PropertiesChanged` on NetworkManager/UPower, systemd
  job completion;
* `Gio.NetworkMonitor` / `Gio.PowerProfileMonitor` `notify::` and
  `network-changed`;
* `Gio.FileMonitor` on the sync root;
* the XDG portal theme watcher.

None of them raises when the pump stalls. They simply stop firing, forever, with
no error anywhere — which is why this module carries a watchdog that logs both
an over-long single drain (`WATCHDOG_MS`) and an over-long gap between ticks.

Measured on the target machine, 2026-08-31, and worth knowing before anyone
decides this module is redundant:

* Qt's default Linux dispatcher here is `QPAEventDispatcherGlib`, which iterates
  `g_main_context_default()` itself. Under it, GLib sources fire even with the
  pump stopped.
* Set `QT_NO_GLIB=1` — as a Flatpak runtime, a Qt build without GLib support, or
  a user chasing an unrelated bug may — and the dispatcher becomes
  `QUnixEventDispatcherQPA`. A `Gio.FileMonitor` then delivered **0** callbacks
  across a 200 ms Qt-side operation with the pump stopped, and **24** with it
  running. `tests/test_glibpump.py::test_pump_is_load_bearing_without_qt_glib`
  reproduces exactly that, in a subprocess.

So the pump is redundant on one dispatcher and the only delivery mechanism on
the other. It is cheap (a 50 ms no-op when Qt already drained the context) and
it removes a silent, environment-dependent failure mode. It stays.

Threading: `GLib.MainContext.default()` is iterated on the **GUI thread only**.
Two threads iterating the same context is a race, and a `Gio` callback that
lands off the GUI thread would then touch Qt objects from the wrong thread. That
rule is enforced here, in code, by `assert_gui_thread()` — which every other
module in this package calls before it touches `Gio`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Final

from gi.repository import GLib
from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread, QTimer

from onedriveui.constants import GLIB_PUMP_MS
from onedriveui.errors import SafetyRefusal

log = logging.getLogger(__name__)

#: The pump interval. Sourced from `constants.GLIB_PUMP_MS` — never re-typed.
PUMP_MS: Final[int] = GLIB_PUMP_MS

#: A single drain that exceeds this is logged as an overrun. 200 ms is the
#: threshold named in the project risk register; it is not in `constants.py`
#: because it is a property of this pump alone and no other module may tune it.
WATCHDOG_MS: Final[int] = 200

#: Hard ceiling on GLib **context iterations** per tick. One iteration
#: dispatches every source ready at the same priority, so this bounds loop
#: turns, not callbacks. Without it a self-perpetuating source (a
#: `Gio.FileMonitor` under `rm -rf`, say) would starve the Qt event loop from
#: inside a Qt timer callback — the exact freeze this pump exists to avoid.
MAX_ITERATIONS_PER_TICK: Final[int] = 128

#: ARCHITECTURE.md section whose rule `assert_gui_thread()` enforces. Passed as
#: `SafetyRefusal.invariant` so a violation is greppable next to I1..I15.
THREAD_RULE: Final[str] = "S7"


# ─────────────────────────────────────────────────────────────────────────────
# Thread identity — the precondition every Gio call in this package shares
# ─────────────────────────────────────────────────────────────────────────────

def gui_thread() -> QThread | None:
    """The thread the Qt application object lives on.

    Returns:
        The `QThread` owning `QCoreApplication.instance()`, or `None` when no Qt
        application has been created yet.
    """
    app = QCoreApplication.instance()
    return app.thread() if app is not None else None


def is_gui_thread() -> bool:
    """Whether the caller is on the GUI thread.

    Before `QCoreApplication` exists there is no Qt notion of a GUI thread, so
    Python's main thread stands in — which is the thread the application object
    is about to be created on.

    Returns:
        True if the calling thread is the GUI thread.
    """
    owner = gui_thread()
    if owner is not None:
        return QThread.currentThread() == owner
    return threading.current_thread() is threading.main_thread()


def assert_gui_thread(what: str) -> None:
    """Refuse to touch GLib/Gio from anything but the GUI thread.

    Args:
        what: A short description of the call being guarded, used in the message.

    Raises:
        SafetyRefusal: If the caller is not on the GUI thread. This is always a
            bug in the caller (ARCHITECTURE.md §7), never a user-facing error.
    """
    if not is_gui_thread():
        raise SafetyRefusal(
            THREAD_RULE,
            f"{what} was called from thread "
            f"{threading.current_thread().name!r}; GLib/Gio is GUI-thread only",
        )


# ─────────────────────────────────────────────────────────────────────────────
# The pump
# ─────────────────────────────────────────────────────────────────────────────

class GlibPump(QObject):
    """Drains `GLib.MainContext.default()` from a Qt timer on the GUI thread.

    The pump is deliberately non-blocking: each tick dispatches every *ready*
    GLib source and returns, so the Qt event loop keeps running and no GLib
    source can block a Qt repaint. The counters it keeps (`ticks`, `iterations`,
    `overruns`, `stalls`) are what the About pane and the diagnostics bundle
    report when a user says "notifications stopped working".
    """

    def __init__(
        self,
        interval_ms: int = PUMP_MS,
        *,
        watchdog_ms: int = WATCHDOG_MS,
        max_iterations: int = MAX_ITERATIONS_PER_TICK,
        parent: QObject | None = None,
    ) -> None:
        """Create a pump. It does not run until `start()` is called.

        Args:
            interval_ms: Timer period in milliseconds.
            watchdog_ms: A drain longer than this is logged as an overrun.
            max_iterations: Ceiling on GLib context iterations per tick.
            parent: Optional Qt parent.

        Raises:
            SafetyRefusal: If constructed off the GUI thread.
        """
        assert_gui_thread("GlibPump()")
        super().__init__(parent)
        self._interval_ms = max(1, int(interval_ms))
        self._watchdog_ms = max(1, int(watchdog_ms))
        self._max_iterations = max(1, int(max_iterations))
        self._context = GLib.MainContext.default()
        self._timer: QTimer | None = None
        self._last_tick_perf = 0.0

        self.ticks = 0
        self.iterations = 0
        self.overruns = 0
        self.stalls = 0
        self.ceiling_hits = 0
        self.max_drain_ms = 0.0
        self.max_gap_ms = 0.0
        self.last_drain_ms = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def interval_ms(self) -> int:
        """The timer period in milliseconds."""
        return self._interval_ms

    @property
    def watchdog_ms(self) -> int:
        """The single-drain overrun threshold in milliseconds."""
        return self._watchdog_ms

    @property
    def context(self) -> GLib.MainContext:
        """The GLib main context this pump drains."""
        return self._context

    @property
    def is_running(self) -> bool:
        """Whether the Qt timer is currently active."""
        return self._timer is not None and self._timer.isActive()

    def start(self) -> bool:
        """Start ticking.

        A `QTimer` needs a `QCoreApplication` to be delivered at all, so if none
        exists yet this logs an error and returns False rather than installing a
        timer that would never fire. Call again after the application is built.

        Returns:
            True if the pump is running when this returns.

        Raises:
            SafetyRefusal: If called off the GUI thread.
        """
        assert_gui_thread("GlibPump.start()")
        if self.is_running:
            return True
        if QCoreApplication.instance() is None:
            log.error(
                "GLib pump NOT started: no QCoreApplication yet. D-Bus signals, "
                "notifications, metered detection and theme changes will not be "
                "delivered until start() is called again after the application "
                "object exists."
            )
            return False
        if self._timer is None:
            timer = QTimer(self)
            timer.setInterval(self._interval_ms)
            # A coarse timer may drift by 5%% of the interval; D-Bus latency is
            # user-visible (a toast action button that lags), so pay for precise.
            timer.setTimerType(Qt.TimerType.PreciseTimer)
            timer.timeout.connect(self._tick)
            self._timer = timer
        self._last_tick_perf = time.perf_counter()
        self._timer.start()
        log.info(
            "GLib pump started: %d ms interval, %d ms watchdog, %d iterations/tick max",
            self._interval_ms, self._watchdog_ms, self._max_iterations,
        )
        return True

    def stop(self) -> None:
        """Stop ticking. GLib sources stay registered; nothing is delivered."""
        if self._timer is not None:
            self._timer.stop()

    # ── the work ─────────────────────────────────────────────────────────────

    def drain(self, max_iterations: int | None = None) -> int:
        """Dispatch every ready GLib source, without blocking.

        Args:
            max_iterations: Override the per-call iteration ceiling.

        Returns:
            The number of GLib context iterations performed. One iteration
            dispatches every source ready at the same priority, so this counts
            loop turns, not callbacks.

        Raises:
            SafetyRefusal: If called off the GUI thread.
        """
        assert_gui_thread("GlibPump.drain()")
        ceiling = self._max_iterations if max_iterations is None else max(1, int(max_iterations))
        context = self._context
        started = time.perf_counter()
        count = 0
        while count < ceiling:
            # may_block=False: return immediately once nothing else is ready.
            if not context.iteration(False):
                break
            count += 1
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        self.iterations += count
        self.last_drain_ms = elapsed_ms
        if elapsed_ms > self.max_drain_ms:
            self.max_drain_ms = elapsed_ms
        if elapsed_ms > self._watchdog_ms:
            self.overruns += 1
            log.warning(
                "GLib pump watchdog: one drain took %.0f ms (> %d ms) over %d "
                "context iterations — the Qt event loop was blocked for that long",
                elapsed_ms, self._watchdog_ms, count,
            )
        if count >= ceiling:
            self.ceiling_hits += 1
            log.warning(
                "GLib pump hit its %d-iteration ceiling in a single tick; a GLib "
                "source is flooding and delivery is now spread across ticks",
                ceiling,
            )
        return count

    def _tick(self) -> None:
        """Timer slot: check for a stall, then drain.

        Raises:
            SafetyRefusal: If Qt ever delivers this on a non-GUI thread.
        """
        assert_gui_thread("GlibPump._tick()")
        now = time.perf_counter()
        gap_ms = (now - self._last_tick_perf) * 1000.0
        self._last_tick_perf = now
        self.ticks += 1
        if self.ticks > 1:
            if gap_ms > self.max_gap_ms:
                self.max_gap_ms = gap_ms
            if gap_ms > self._interval_ms + self._watchdog_ms:
                self.stalls += 1
                log.warning(
                    "GLib pump stalled: %.0f ms since the previous iteration "
                    "(interval %d ms) — D-Bus signals, notifications, metered "
                    "detection and theme changes were frozen for that long",
                    gap_ms, self._interval_ms,
                )
        self.drain()

    # ── observability ────────────────────────────────────────────────────────

    def stats(self) -> dict[str, float | int | bool]:
        """A snapshot of the pump's counters, for the About pane and diagnostics.

        Returns:
            A plain dict of counters; safe to serialise into a bundle.
        """
        return {
            "running": self.is_running,
            "interval_ms": self._interval_ms,
            "watchdog_ms": self._watchdog_ms,
            "ticks": self.ticks,
            "iterations": self.iterations,
            "overruns": self.overruns,
            "stalls": self.stalls,
            "ceiling_hits": self.ceiling_hits,
            "max_drain_ms": round(self.max_drain_ms, 3),
            "max_gap_ms": round(self.max_gap_ms, 3),
            "last_drain_ms": round(self.last_drain_ms, 3),
        }

    def reset_stats(self) -> None:
        """Zero every counter. Used by the diagnostics pane and by tests."""
        self.ticks = 0
        self.iterations = 0
        self.overruns = 0
        self.stalls = 0
        self.ceiling_hits = 0
        self.max_drain_ms = 0.0
        self.max_gap_ms = 0.0
        self.last_drain_ms = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# The process-wide singleton
# ─────────────────────────────────────────────────────────────────────────────

_PUMP: GlibPump | None = None


def install(
    interval_ms: int = PUMP_MS,
    *,
    watchdog_ms: int = WATCHDOG_MS,
    max_iterations: int = MAX_ITERATIONS_PER_TICK,
) -> GlibPump:
    """Create (once) and start the process-wide pump.

    Called from the composition root immediately after `DbWriter`, before the
    theme watcher, the notifier and the IPC server — all of which depend on it.
    Calling it a second time returns the existing pump and re-starts it if it
    had been stopped; the interval of an existing pump is not changed.

    Args:
        interval_ms: Timer period for a freshly created pump.
        watchdog_ms: Overrun threshold for a freshly created pump.
        max_iterations: Per-tick iteration ceiling for a freshly created pump.

    Returns:
        The singleton pump.

    Raises:
        SafetyRefusal: If called off the GUI thread.
    """
    global _PUMP
    assert_gui_thread("glibpump.install()")
    if _PUMP is None:
        _PUMP = GlibPump(
            interval_ms, watchdog_ms=watchdog_ms, max_iterations=max_iterations
        )
    elif _PUMP.interval_ms != interval_ms:
        log.info(
            "glibpump.install(%d) ignored: the pump is already installed at %d ms",
            interval_ms, _PUMP.interval_ms,
        )
    _PUMP.start()
    return _PUMP


def ensure_started() -> GlibPump:
    """Return the running pump, installing and/or starting it if necessary.

    Every module in this package calls this from its constructor, so that a
    `Notifier` or a `PowerPolicy` built before the composition root reached its
    pump line still receives D-Bus signals.

    Returns:
        The singleton pump. It may not be running if no `QCoreApplication`
        exists yet — check `GlibPump.is_running`.

    Raises:
        SafetyRefusal: If called off the GUI thread.
    """
    pump = install() if _PUMP is None else _PUMP
    if not pump.is_running:
        pump.start()
    return pump


def current() -> GlibPump | None:
    """The installed pump, or `None` if `install()` has never been called."""
    return _PUMP


def iterate(max_iterations: int | None = None) -> int:
    """Drain the GLib context once, right now, without waiting for a tick.

    Used at shutdown to flush pending D-Bus replies, and by tests that must not
    depend on timer scheduling.

    Args:
        max_iterations: Override the per-call iteration ceiling.

    Returns:
        The number of GLib context iterations performed; 0 when no pump exists.
    """
    pump = _PUMP
    if pump is None:
        assert_gui_thread("glibpump.iterate()")
        context = GLib.MainContext.default()
        ceiling = MAX_ITERATIONS_PER_TICK if max_iterations is None else max_iterations
        count = 0
        while count < ceiling and context.iteration(False):
            count += 1
        return count
    return pump.drain(max_iterations)


def shutdown() -> None:
    """Stop and forget the singleton pump.

    Drains once first, so a `CloseNotification` or a systemd `StopUnit` reply
    issued during teardown is actually delivered.
    """
    global _PUMP
    pump = _PUMP
    if pump is None:
        return
    try:
        pump.drain()
    except SafetyRefusal:  # pragma: no cover - shutdown from the wrong thread
        log.warning("glibpump.shutdown() called off the GUI thread; not draining")
    pump.stop()
    pump.deleteLater()
    _PUMP = None


__all__ = [
    "GlibPump",
    "MAX_ITERATIONS_PER_TICK",
    "PUMP_MS",
    "THREAD_RULE",
    "WATCHDOG_MS",
    "assert_gui_thread",
    "current",
    "ensure_started",
    "gui_thread",
    "install",
    "is_gui_thread",
    "iterate",
    "shutdown",
]
