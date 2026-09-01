"""Tests for `onedriveui.platform.glibpump` — the critical path.

If the pump stalls, D-Bus signals, notifications, metered detection and theme
changes stop *silently*. These tests therefore cover three separate things:

1. It actually delivers GLib callbacks while Qt is busy (the headline case).
2. It is genuinely load-bearing — proved in a subprocess with `QT_NO_GLIB=1`,
   where Qt's own GLib dispatcher is not there to hide a broken pump.
3. Its watchdog fires, so a future stall is loud rather than silent.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from gi.repository import GLib
from PySide6.QtCore import QEventLoop, QTimer

from onedriveui.constants import GLIB_PUMP_MS
from onedriveui.errors import SafetyRefusal
from onedriveui.platform import glibpump

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_platform_leaks():
    """The GLib pump is a process-wide singleton and `BUS` is a process-wide
    QObject. A pump left running here would keep iterating GLib inside another
    package's tests, so every test in this module hands it back."""
    yield
    glibpump.shutdown()


@pytest.fixture
def pump(qapp):
    """An installed, running pump, torn down afterwards."""
    glibpump.shutdown()
    instance = glibpump.install()
    try:
        yield instance
    finally:
        glibpump.shutdown()


def _spin(ms: int) -> None:
    """Run the Qt event loop for `ms` milliseconds — a 'Qt-side operation'."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _drain_glib(times: int = 50) -> None:
    """Empty the default context directly, ignoring the pump."""
    context = GLib.MainContext.default()
    for _ in range(times):
        if not context.iteration(False):
            break


class TestContract:
    def test_pump_ms_comes_from_constants(self):
        assert glibpump.PUMP_MS == GLIB_PUMP_MS == 50

    def test_watchdog_threshold_is_200ms(self):
        assert glibpump.WATCHDOG_MS == 200

    def test_public_api_is_the_manifest_api(self):
        for name in ("install", "ensure_started", "PUMP_MS"):
            assert hasattr(glibpump, name)


class TestThreadGuard:
    def test_is_gui_thread_on_the_main_thread(self, qapp):
        assert glibpump.is_gui_thread() is True

    def test_gui_thread_matches_the_application(self, qapp):
        assert glibpump.gui_thread() == qapp.thread()

    def test_assert_gui_thread_passes_on_the_main_thread(self, qapp):
        glibpump.assert_gui_thread("test")

    def test_gio_is_refused_off_the_gui_thread(self, qapp):
        captured: list[BaseException] = []

        def worker() -> None:
            try:
                glibpump.assert_gui_thread("worker touching Gio")
            except BaseException as exc:  # noqa: BLE001 - the point of the test
                captured.append(exc)

        thread = threading.Thread(target=worker, name="not-the-gui-thread")
        thread.start()
        thread.join(5)

        assert len(captured) == 1
        assert isinstance(captured[0], SafetyRefusal)
        assert captured[0].invariant == glibpump.THREAD_RULE == "S7"
        assert "not-the-gui-thread" in str(captured[0])

    def test_drain_is_refused_off_the_gui_thread(self, pump):
        captured: list[BaseException] = []

        def worker() -> None:
            try:
                pump.drain()
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)

        assert len(captured) == 1
        assert isinstance(captured[0], SafetyRefusal)

    def test_constructing_off_the_gui_thread_is_refused(self, qapp):
        captured: list[BaseException] = []

        def worker() -> None:
            try:
                glibpump.GlibPump()
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)

        assert len(captured) == 1
        assert isinstance(captured[0], SafetyRefusal)


class TestSingleton:
    def test_install_starts_and_returns_the_singleton(self, qapp):
        glibpump.shutdown()
        try:
            first = glibpump.install()
            assert first.is_running
            assert glibpump.current() is first
            assert glibpump.install() is first
        finally:
            glibpump.shutdown()

    def test_ensure_started_installs_when_absent(self, qapp):
        glibpump.shutdown()
        try:
            assert glibpump.current() is None
            instance = glibpump.ensure_started()
            assert instance.is_running
            assert glibpump.current() is instance
        finally:
            glibpump.shutdown()

    def test_ensure_started_restarts_a_stopped_pump(self, pump):
        pump.stop()
        assert not pump.is_running
        assert glibpump.ensure_started() is pump
        assert pump.is_running

    def test_shutdown_clears_the_singleton(self, qapp):
        glibpump.install()
        glibpump.shutdown()
        assert glibpump.current() is None

    def test_shutdown_is_idempotent(self, qapp):
        glibpump.shutdown()
        glibpump.shutdown()
        assert glibpump.current() is None

    def test_install_keeps_the_existing_interval(self, pump, caplog):
        with caplog.at_level(logging.INFO, logger=glibpump.__name__):
            again = glibpump.install(250)
        assert again is pump
        assert again.interval_ms == glibpump.PUMP_MS
        assert "already installed" in caplog.text

    def test_start_without_a_qapplication_logs_and_declines(self, qapp, monkeypatch, caplog):
        instance = glibpump.GlibPump()

        class _NoApp:
            @staticmethod
            def instance():
                return None

        monkeypatch.setattr(glibpump, "QCoreApplication", _NoApp)
        with caplog.at_level(logging.ERROR, logger=glibpump.__name__):
            assert instance.start() is False
        assert not instance.is_running
        assert "no QCoreApplication" in caplog.text

    def test_iterate_without_a_pump_still_drains(self, qapp):
        glibpump.shutdown()
        fired: list[int] = []
        GLib.idle_add(lambda: (fired.append(1), False)[1])
        assert glibpump.iterate() >= 1
        assert fired == [1]


class TestDrain:
    def test_drain_dispatches_an_idle_source(self, pump):
        fired: list[str] = []
        GLib.idle_add(lambda: (fired.append("idle"), False)[1])
        assert pump.drain() >= 1
        assert fired == ["idle"]

    def test_drain_returns_zero_when_nothing_is_ready(self, pump):
        _drain_glib()
        assert pump.drain() == 0

    def test_a_glib_timeout_fires_through_the_qt_loop(self, pump):
        fired: list[str] = []
        GLib.timeout_add(10, lambda: (fired.append("timeout"), False)[1])
        _spin(250)
        assert fired == ["timeout"]
        assert pump.ticks >= 3

    def test_one_iteration_dispatches_every_ready_source(self, pump):
        """GLib batches same-priority sources, so 20 idles are not 20 turns."""
        fired: list[int] = []
        for index in range(20):
            GLib.idle_add(lambda i=index: (fired.append(i), False)[1])
        turns = pump.drain()
        assert len(fired) == 20
        assert turns < 20

    def test_iteration_ceiling_is_enforced(self, pump, caplog):
        """A self-perpetuating source must not starve the Qt event loop."""
        state = {"left": 50}

        def flood() -> bool:
            state["left"] -= 1
            return state["left"] > 0

        GLib.idle_add(flood)
        with caplog.at_level(logging.WARNING, logger=glibpump.__name__):
            turns = pump.drain(max_iterations=5)
        assert turns == 5
        assert state["left"] > 0, "the source should still be pending"
        assert pump.ceiling_hits == 1
        assert "ceiling" in caplog.text
        state["left"] = 0
        _drain_glib()

    def test_stats_and_reset(self, pump):
        GLib.idle_add(lambda: False)
        pump.drain()
        stats = pump.stats()
        assert stats["running"] is True
        assert stats["interval_ms"] == glibpump.PUMP_MS
        assert stats["iterations"] >= 1
        pump.reset_stats()
        assert pump.stats()["iterations"] == 0


class TestWatchdog:
    def test_an_over_long_iteration_is_logged(self, pump, caplog):
        def slow() -> bool:
            time.sleep((glibpump.WATCHDOG_MS + 60) / 1000.0)
            return False

        GLib.idle_add(slow)
        with caplog.at_level(logging.WARNING, logger=glibpump.__name__):
            pump.drain()

        assert pump.overruns == 1
        assert pump.max_drain_ms > glibpump.WATCHDOG_MS
        assert "watchdog" in caplog.text
        assert "Qt event loop was blocked" in caplog.text

    def test_a_fast_iteration_is_not_logged(self, pump, caplog):
        GLib.idle_add(lambda: False)
        with caplog.at_level(logging.WARNING, logger=glibpump.__name__):
            pump.drain()
        assert pump.overruns == 0
        assert "watchdog" not in caplog.text

    def test_a_gap_between_ticks_is_reported_as_a_stall(self, pump, caplog):
        pump.stop()
        pump.reset_stats()
        pump._tick()  # establishes the baseline
        pump._last_tick_perf -= (glibpump.WATCHDOG_MS + glibpump.PUMP_MS + 200) / 1000.0
        with caplog.at_level(logging.WARNING, logger=glibpump.__name__):
            pump._tick()
        assert pump.stalls == 1
        assert "stalled" in caplog.text
        assert "frozen for that long" in caplog.text

    def test_normal_ticking_reports_no_stall(self, pump):
        _spin(300)
        assert pump.ticks >= 3
        assert pump.stalls == 0


class TestFileMonitorDelivery:
    """The headline requirement: Gio callbacks keep arriving under Qt load."""

    @staticmethod
    def _monitor(directory: Path, sink: list[tuple[str, str]]):
        from gi.repository import Gio

        monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
            Gio.FileMonitorFlags.NONE, None
        )
        monitor.set_rate_limit(0)
        monitor.connect(
            "changed",
            lambda _m, gfile, _other, event: sink.append(
                (gfile.get_basename(), event.value_nick)
            ),
        )
        return monitor

    def test_callbacks_keep_firing_during_a_200ms_qt_operation(self, pump, tmp_path):
        events: list[tuple[str, str]] = []
        monitor = self._monitor(tmp_path, events)
        try:
            _spin(60)          # let the watch settle
            events.clear()

            # A 200 ms Qt-side operation that is itself doing Qt work.
            for index, delay in enumerate((20, 60, 100, 140)):
                QTimer.singleShot(
                    delay, lambda i=index: (tmp_path / f"f{i}.txt").write_text("x")
                )
            _spin(200)

            created = [name for name, kind in events if kind == "created"]
            assert len(created) >= 4, f"only {created} arrived during the Qt operation"
            assert pump.ticks >= 3
        finally:
            monitor.cancel()

    def test_callbacks_survive_a_watchdog_length_stall(self, pump, tmp_path):
        events: list[tuple[str, str]] = []
        monitor = self._monitor(tmp_path, events)
        try:
            _spin(60)
            events.clear()
            (tmp_path / "before.txt").write_text("x")
            time.sleep(0.25)          # the Qt loop is blocked outright
            _spin(200)
            assert any(name == "before.txt" for name, _ in events)
        finally:
            monitor.cancel()

    @pytest.mark.slow
    def test_pump_is_load_bearing_without_qt_glib(self, tmp_path):
        """With `QT_NO_GLIB=1`, the pump is the *only* delivery mechanism.

        Qt's default Linux dispatcher is `QPAEventDispatcherGlib`, which
        iterates the default GLib context itself and would mask a broken pump.
        Under `QUnixEventDispatcherQPA` nothing else drains it, so this is the
        honest test of whether the pump works.
        """
        script = textwrap.dedent(
            """
            import json, pathlib, sys
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import QAbstractEventDispatcher, QEventLoop, QTimer

            app = QApplication([])
            from gi.repository import Gio
            from onedriveui.platform import glibpump

            tmp = pathlib.Path(sys.argv[1])
            events = []
            monitor = Gio.File.new_for_path(str(tmp)).monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
            monitor.set_rate_limit(0)
            monitor.connect("changed", lambda *a: events.append(1))

            def spin(ms):
                loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

            def burst(tag):
                for i, d in enumerate((20, 60, 100, 140)):
                    QTimer.singleShot(d, lambda i=i: (tmp / f"{tag}{i}").write_text("x"))
                spin(200)

            burst("off")
            without = len(events)
            pump = glibpump.install()
            events.clear()
            burst("on")
            with_pump = len(events)
            print(json.dumps({
                "dispatcher": QAbstractEventDispatcher.instance().metaObject().className(),
                "without_pump": without,
                "with_pump": with_pump,
            }))
            """
        )
        env = {
            **os.environ,
            "QT_NO_GLIB": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONPATH": str(REPO_ROOT),
        }
        completed = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True, text=True, timeout=120, env=env, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        import json

        result = json.loads(completed.stdout.strip().splitlines()[-1])
        assert result["dispatcher"] == "QUnixEventDispatcherQPA"
        assert result["without_pump"] == 0, "Qt drained GLib; the test proves nothing"
        assert result["with_pump"] >= 4
