"""Tests for `onedriveui.platform.systemd`.

Every mutating path runs against a fake `org.freedesktop.systemd1` so the real
user manager is never touched. The live tests are strictly read-only: they
inspect the user's own `rclone-onedrive.service`, and they never write, enable,
start or stop anything.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from gi.repository import Gio, GLib

from onedriveui import paths
from onedriveui.constants import UNIT_BISYNC_TMPL, UNIT_MOUNT_TMPL, UNIT_RCD
from onedriveui.errors import OneDriveUIError, SafetyRefusal
from onedriveui.platform import systemd as SD

#: A unit the user already runs. Read-only in every live test below.
LIVE_UNIT = "rclone-onedrive.service"

GOOD_UNIT = """[Unit]
Description=OneDriveUI rclone control plane
PartOf=graphical-session.target
After=graphical-session-pre.target

[Service]
Type=simple
ExecStart=/usr/bin/rclone rcd --rc-addr 127.0.0.1:17801

[Install]
WantedBy=default.target
"""


# ═════════════════════════════════════════════════════════════════════════════
# A fake systemd manager
# ═════════════════════════════════════════════════════════════════════════════

class FakeManagerBus:
    """Records `Manager` calls and answers unit-property reads from a dict."""

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self.units: dict[str, dict[str, object]] = {}
        self.unit_file_state: dict[str, str] = {}
        self.fail_methods: set[str] = set()
        self.missing: set[str] = set()

    # -- helpers -------------------------------------------------------------
    def add_unit(self, name, active="active", sub="running", load="loaded", **extra):
        self.units[name] = {
            SD.PROP_ACTIVE_STATE: active,
            SD.PROP_SUB_STATE: sub,
            SD.PROP_LOAD_STATE: load,
            **extra,
        }
        return f"/org/freedesktop/systemd1/unit/{name.replace('.', '_2e')}"

    def path_of(self, name):
        return f"/org/freedesktop/systemd1/unit/{name.replace('.', '_2e')}"

    def name_of(self, path):
        for name in self.units:
            if self.path_of(name) == path:
                return name
        return None

    def of(self, method):
        return [c for c in self.calls if c.method == method]

    def last(self, method):
        rows = self.of(method)
        assert rows, f"no {method} call was made"
        return rows[-1]

    # -- Bus surface ---------------------------------------------------------
    def call(self, name, path, iface, method, *, signature=None, args=(),
             reply=None, timeout_ms=2000, auto_start=False):
        variant = GLib.Variant(signature, tuple(args)) if signature else None
        self.calls.append(SimpleNamespace(
            name=name, path=path, iface=iface, method=method,
            signature=signature, args=tuple(args), variant=variant, reply=reply,
        ))
        if method in self.fail_methods:
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), f"fake {method} failure", Gio.IOErrorEnum.FAILED
            )
        if method in ("GetUnit", "LoadUnit"):
            unit = args[0]
            if unit in self.missing:
                raise GLib.Error.new_literal(
                    Gio.io_error_quark(), "no such unit", Gio.IOErrorEnum.NOT_FOUND
                )
            if method == "GetUnit" and unit not in self.units:
                raise GLib.Error.new_literal(
                    Gio.io_error_quark(), "not loaded", Gio.IOErrorEnum.NOT_FOUND
                )
            if unit not in self.units:
                self.add_unit(unit, active="inactive", sub="dead", load="not-found")
            return (self.path_of(unit),)
        if method in ("StartUnit", "StopUnit", "RestartUnit", "TryRestartUnit"):
            return (f"/org/freedesktop/systemd1/job/{len(self.calls)}",)
        if method == "EnableUnitFiles":
            return (True, [("symlink", "/x", "/y")])
        if method == "DisableUnitFiles":
            return ([("unlink", "/x", "")],)
        if method == "GetUnitFileState":
            return (self.unit_file_state.get(args[0], "disabled"),)
        return ()

    def call_or_none(self, name, path, iface, method, **kwargs):
        try:
            return self.call(name, path, iface, method, **kwargs)
        except GLib.Error:
            return None

    def get_property(self, name, path, iface, prop, default=None, **_kwargs):
        unit = self.name_of(path)
        if unit is None:
            return default
        props = self.units[unit]
        if iface == SD.SERVICE_IFACE and prop not in SD.SERVICE_PROPERTIES:
            return default
        if iface == SD.UNIT_IFACE and prop in SD.SERVICE_PROPERTIES:
            return default
        return props.get(prop, default)

    def get_all(self, name, path, iface, **_kwargs):
        unit = self.name_of(path)
        if unit is None:
            return {}
        return dict(self.units[unit])


@pytest.fixture(autouse=True)
def _no_bus_override_leaks():
    """`systemd.set_bus()` is module-global state; never let it escape a test."""
    yield
    SD.set_bus(None)


@pytest.fixture
def fake_sd() -> FakeManagerBus:
    bus = FakeManagerBus()
    SD.set_bus(bus)
    try:
        yield bus
    finally:
        SD.set_bus(None)


# ═════════════════════════════════════════════════════════════════════════════
# The forbidden target
# ═════════════════════════════════════════════════════════════════════════════

class TestNetworkOnlineTarget:
    def test_the_constant_names_it(self):
        assert SD.FORBIDDEN_TARGET == "network-online.target"

    def test_clean_text_passes(self):
        SD.assert_no_network_online_target(GOOD_UNIT)

    @pytest.mark.parametrize("line", [
        "After=network-online.target\n",
        "Wants=network-online.target\n",
        "Requires=network-online.target\n",
        "# ordered after network-online.target\n",
    ])
    def test_any_mention_is_refused(self, line):
        with pytest.raises(SafetyRefusal) as excinfo:
            SD.assert_no_network_online_target(GOOD_UNIT + line)
        assert excinfo.value.invariant == SD.UNIT_RULE
        assert "silently ignored" in str(excinfo.value)

    def test_write_unit_refuses_it(self, fake_sd):
        with pytest.raises(SafetyRefusal):
            SD.write_unit(UNIT_RCD, GOOD_UNIT + "Wants=network-online.target\n")
        assert not SD.unit_file(UNIT_RCD).exists()

    def test_a_transient_property_cannot_smuggle_it(self):
        with pytest.raises(SafetyRefusal):
            SD.build_transient_argv(
                "onedriveui-bisync-x", ["/usr/bin/rclone", "bisync"],
                properties=("After=network-online.target",),
            )

    def test_a_transient_command_cannot_smuggle_it(self):
        with pytest.raises(SafetyRefusal):
            SD.build_transient_argv(
                "onedriveui-bisync-x", ["/bin/systemctl", "start",
                                        "network-online.target"],
            )

    def test_the_module_itself_never_emits_it(self):
        source = Path(SD.__file__).read_text(encoding="utf-8")
        emitting = [
            line for line in source.splitlines()
            if SD.FORBIDDEN_TARGET in line
            and any(line.strip().startswith(p) for p in ("After=", "Wants=", "Requires="))
        ]
        assert emitting == []


# ═════════════════════════════════════════════════════════════════════════════
# Unit names
# ═════════════════════════════════════════════════════════════════════════════

class TestUnitNames:
    @pytest.mark.parametrize("name", [
        UNIT_RCD,
        "onedriveui.service",
        UNIT_MOUNT_TMPL.format("onedrive"),
        "onedriveui-mount@.service",
        "graphical-session.target",
        "foo.timer",
        "foo.socket",
    ])
    def test_valid_names_are_accepted(self, name):
        assert SD.assert_valid_unit_name(name) == name

    @pytest.mark.parametrize("name", [
        "", "onedriveui", "onedriveui.txt", "../escape.service",
        "sub/dir.service", "a b.service", "onedriveui.service\n",
    ])
    def test_invalid_names_are_refused(self, name):
        with pytest.raises(SafetyRefusal):
            SD.assert_valid_unit_name(name)

    def test_a_traversing_name_can_never_reach_unit_file(self):
        with pytest.raises(SafetyRefusal):
            SD.unit_file("../../../tmp/evil.service")

    def test_unit_file_lands_in_the_user_unit_dir(self):
        assert SD.unit_file(UNIT_RCD) == paths.systemd_user_dir() / UNIT_RCD


# ═════════════════════════════════════════════════════════════════════════════
# Writing unit files
# ═════════════════════════════════════════════════════════════════════════════

class TestWriteUnit:
    def test_a_new_unit_is_written_and_reported_as_changed(self, fake_sd):
        assert SD.write_unit(UNIT_RCD, GOOD_UNIT) is True
        path = SD.unit_file(UNIT_RCD)
        assert path.read_text(encoding="utf-8") == GOOD_UNIT

    def test_unchanged_content_is_not_rewritten(self, fake_sd):
        SD.write_unit(UNIT_RCD, GOOD_UNIT)
        path = SD.unit_file(UNIT_RCD)
        before = path.stat().st_mtime_ns
        assert SD.write_unit(UNIT_RCD, GOOD_UNIT) is False
        assert path.stat().st_mtime_ns == before

    def test_changed_content_is_rewritten(self, fake_sd):
        SD.write_unit(UNIT_RCD, GOOD_UNIT)
        assert SD.write_unit(UNIT_RCD, GOOD_UNIT + "# tweak\n") is True
        assert "# tweak" in SD.read_unit(UNIT_RCD)

    def test_a_missing_trailing_newline_is_added(self, fake_sd):
        SD.write_unit(UNIT_RCD, "[Unit]\nDescription=x")
        assert SD.read_unit(UNIT_RCD).endswith("\n")
        assert SD.write_unit(UNIT_RCD, "[Unit]\nDescription=x") is False

    def test_the_file_mode_is_0644(self, fake_sd):
        SD.write_unit(UNIT_RCD, GOOD_UNIT)
        mode = stat.S_IMODE(SD.unit_file(UNIT_RCD).stat().st_mode)
        assert mode == SD.UNIT_FILE_MODE == 0o644

    def test_no_temporary_file_is_left_behind(self, fake_sd):
        SD.write_unit(UNIT_RCD, GOOD_UNIT)
        leftovers = [p.name for p in paths.systemd_user_dir().iterdir()
                     if p.name.startswith(".")]
        assert leftovers == []

    def test_a_write_triggers_a_daemon_reload(self, fake_sd):
        SD.write_unit(UNIT_RCD, GOOD_UNIT)
        assert len(fake_sd.of("Reload")) == 1

    def test_reload_can_be_declined(self, fake_sd):
        SD.write_unit(UNIT_RCD, GOOD_UNIT, reload=False)
        assert fake_sd.of("Reload") == []

    def test_a_failing_reload_does_not_lose_the_file(self, fake_sd, caplog):
        fake_sd.fail_methods.add("Reload")
        with caplog.at_level("WARNING", logger=SD.__name__):
            assert SD.write_unit(UNIT_RCD, GOOD_UNIT) is True
        assert SD.unit_file(UNIT_RCD).exists()
        assert "daemon-reload" in caplog.text

    def test_read_unit_of_a_missing_file(self, fake_sd):
        assert SD.read_unit("onedriveui-nothing.service") == ""

    def test_remove_unit(self, fake_sd):
        SD.write_unit(UNIT_RCD, GOOD_UNIT)
        assert SD.remove_unit(UNIT_RCD) is True
        assert not SD.unit_file(UNIT_RCD).exists()
        assert SD.remove_unit(UNIT_RCD) is False

    def test_an_unwritable_directory_raises_our_error(self, fake_sd, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(SD.atomicio, "atomic_write_text", boom)
        with pytest.raises(OneDriveUIError):
            SD.write_unit(UNIT_RCD, GOOD_UNIT)

    def test_units_are_written_through_wp01_atomicio(self, fake_sd, monkeypatch):
        """tmp -> fsync -> os.replace -> fsync(dir); never a plain open()."""
        seen: list[tuple] = []
        real = SD.atomicio.atomic_write_text
        monkeypatch.setattr(
            SD.atomicio, "atomic_write_text",
            lambda path, text, **kw: (seen.append((path, kw)), real(path, text, **kw))[1],
        )
        SD.write_unit(UNIT_RCD, GOOD_UNIT)
        assert len(seen) == 1
        assert seen[0][0] == SD.unit_file(UNIT_RCD)
        assert seen[0][1]["mode"] == SD.UNIT_FILE_MODE


# ═════════════════════════════════════════════════════════════════════════════
# Manager operations
# ═════════════════════════════════════════════════════════════════════════════

class TestManagerOperations:
    def test_daemon_reload(self, fake_sd):
        SD.daemon_reload()
        call = fake_sd.last("Reload")
        assert call.iface == SD.MANAGER_IFACE
        assert call.signature is None

    def test_start(self, fake_sd):
        job = SD.start(UNIT_RCD)
        call = fake_sd.last("StartUnit")
        assert call.signature == "(ss)"
        assert call.args == (UNIT_RCD, SD.JOB_MODE)
        assert call.reply == "(o)"
        assert job.startswith("/org/freedesktop/systemd1/job/")

    def test_stop(self, fake_sd):
        SD.stop(UNIT_RCD)
        assert fake_sd.last("StopUnit").args == (UNIT_RCD, "replace")

    def test_restart(self, fake_sd):
        SD.restart(UNIT_RCD)
        assert fake_sd.last("RestartUnit").args == (UNIT_RCD, "replace")

    def test_try_restart(self, fake_sd):
        SD.try_restart(UNIT_RCD)
        assert fake_sd.last("TryRestartUnit").args == (UNIT_RCD, "replace")

    def test_a_custom_job_mode(self, fake_sd):
        SD.start(UNIT_RCD, mode="fail")
        assert fake_sd.last("StartUnit").args == (UNIT_RCD, "fail")

    def test_enable(self, fake_sd):
        assert SD.enable(UNIT_RCD) is True
        call = fake_sd.last("EnableUnitFiles")
        assert call.signature == "(asbb)"
        assert call.args == ([UNIT_RCD], False, True)
        assert len(fake_sd.of("Reload")) == 1

    def test_enable_now_also_starts(self, fake_sd):
        SD.enable(UNIT_RCD, now=True)
        assert fake_sd.of("StartUnit")

    def test_disable(self, fake_sd):
        assert SD.disable(UNIT_RCD) is True
        call = fake_sd.last("DisableUnitFiles")
        assert call.signature == "(asb)"
        assert call.args == ([UNIT_RCD], False)

    def test_disable_now_stops_first(self, fake_sd):
        SD.disable(UNIT_RCD, now=True)
        assert fake_sd.calls[0].method == "StopUnit"

    def test_disable_now_survives_a_failing_stop(self, fake_sd, caplog):
        fake_sd.fail_methods.add("StopUnit")
        with caplog.at_level("WARNING", logger=SD.__name__):
            SD.disable(UNIT_RCD, now=True)
        assert fake_sd.of("DisableUnitFiles")

    def test_reset_failed(self, fake_sd):
        SD.reset_failed(UNIT_RCD)
        assert fake_sd.last("ResetFailedUnit").args == (UNIT_RCD,)

    def test_a_manager_error_becomes_our_error(self, fake_sd):
        fake_sd.fail_methods.add("StartUnit")
        with pytest.raises(OneDriveUIError) as excinfo:
            SD.start(UNIT_RCD)
        assert "StartUnit" in str(excinfo.value)

    def test_every_mutating_call_validates_the_unit_name(self, fake_sd):
        for func in (SD.start, SD.stop, SD.restart, SD.try_restart, SD.enable,
                     SD.disable, SD.reset_failed, SD.is_enabled):
            with pytest.raises(SafetyRefusal):
                func("../evil")


# ═════════════════════════════════════════════════════════════════════════════
# Reading state
# ═════════════════════════════════════════════════════════════════════════════

class TestReadState:
    def test_state_of_a_running_unit(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        assert SD.state(UNIT_RCD) == ("active", "running")

    def test_state_of_a_stopped_unit(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD, active="inactive", sub="dead")
        assert SD.state(UNIT_RCD) == ("inactive", "dead")

    def test_a_not_found_unit_is_distinguishable_from_a_stopped_one(self, fake_sd):
        fake_sd.add_unit("gone.service", active="inactive", sub="dead", load="not-found")
        assert SD.state("gone.service") == ("inactive", SD.NOT_FOUND)

    def test_an_unloadable_unit(self, fake_sd):
        fake_sd.missing.add("nope.service")
        assert SD.state("nope.service") == (SD.INACTIVE, SD.NOT_FOUND)
        assert SD.is_active("nope.service") is False
        assert SD.exists("nope.service") is False

    def test_is_active(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        assert SD.is_active(UNIT_RCD) is True

    def test_activating_is_not_active(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD, active="activating", sub="start")
        assert SD.is_active(UNIT_RCD) is False

    def test_is_failed(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD, active="failed", sub="failed")
        assert SD.is_failed(UNIT_RCD) is True
        assert SD.is_active(UNIT_RCD) is False

    def test_exists(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        assert SD.exists(UNIT_RCD) is True

    def test_is_enabled(self, fake_sd):
        fake_sd.unit_file_state[UNIT_RCD] = "enabled"
        assert SD.is_enabled(UNIT_RCD) == "enabled"

    def test_is_enabled_when_unknown(self, fake_sd):
        fake_sd.fail_methods.add("GetUnitFileState")
        assert SD.is_enabled(UNIT_RCD) == ""

    def test_status_text_comes_from_the_service_interface(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD, StatusText="vfs cache: objects 22")
        assert SD.status_text(UNIT_RCD) == "vfs cache: objects 22"

    def test_status_text_defaults_to_empty(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        assert SD.status_text(UNIT_RCD) == ""

    def test_main_pid(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD, MainPID=4242)
        assert SD.main_pid(UNIT_RCD) == 4242

    def test_main_pid_of_a_stopped_unit(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        assert SD.main_pid(UNIT_RCD) == 0

    def test_show_finds_a_unit_property(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        assert SD.show(UNIT_RCD, SD.PROP_ACTIVE_STATE) == "active"

    def test_show_falls_back_to_the_service_interface(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD, StatusText="hello")
        assert SD.show(UNIT_RCD, "StatusText") == "hello"

    def test_show_returns_the_default_for_an_unknown_property(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        assert SD.show(UNIT_RCD, "NoSuchProperty", default="fallback") == "fallback"

    def test_show_accepts_an_explicit_interface(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD, StatusText="hello")
        assert SD.show(UNIT_RCD, "StatusText", iface=SD.UNIT_IFACE, default="") == ""

    def test_unit_path_is_cached_and_cleared_by_a_reload(self, fake_sd):
        fake_sd.add_unit(UNIT_RCD)
        SD.unit_path(UNIT_RCD)
        SD.unit_path(UNIT_RCD)
        assert len(fake_sd.of("GetUnit")) == 1
        SD.daemon_reload()
        SD.unit_path(UNIT_RCD)
        assert len(fake_sd.of("GetUnit")) == 2

    def test_load_unit_is_the_fallback_for_an_unloaded_unit(self, fake_sd):
        assert SD.unit_path("cold.service") is not None
        assert fake_sd.of("GetUnit")
        assert fake_sd.of("LoadUnit")

    def test_available(self, fake_sd):
        assert SD.available() is True
        fake_sd.fail_methods.add("GetUnitFileState")
        assert SD.available() is False


# ═════════════════════════════════════════════════════════════════════════════
# Transient units
# ═════════════════════════════════════════════════════════════════════════════

class TestTransient:
    def test_the_argv_matches_the_specification(self):
        argv = SD.build_transient_argv(
            UNIT_BISYNC_TMPL.format("onedrive"),
            ["/usr/bin/rclone", "bisync", "/home/u/OneDrive-Offline", "onedrive:Offline"],
        )
        assert argv == [
            "systemd-run", "--user", "--unit=onedriveui-bisync-onedrive", "--collect",
            "--property=KillSignal=SIGINT",
            "--property=TimeoutStopSec=150",
            "--property=Restart=no",
            "--",
            "/usr/bin/rclone", "bisync", "/home/u/OneDrive-Offline", "onedrive:Offline",
        ]

    def test_sigint_is_the_default_kill_signal(self):
        """Invariant I13: a SIGKILL mid-transfer leaves a `.partial` behind."""
        assert SD.TRANSIENT_KILL_SIGNAL == "SIGINT"
        argv = SD.build_transient_argv("onedriveui-bisync-x", ["/bin/true"])
        assert "--property=KillSignal=SIGINT" in argv
        assert "SIGKILL" not in " ".join(argv)

    def test_the_stop_timeout_is_150s(self):
        assert SD.TRANSIENT_TIMEOUT_STOP_S == 150

    def test_restart_is_off_by_default(self):
        assert "--property=Restart=no" in SD.build_transient_argv(
            "onedriveui-bisync-x", ["/bin/true"]
        )

    def test_extra_properties_are_appended(self):
        argv = SD.build_transient_argv(
            "onedriveui-bisync-x", ["/bin/true"], properties=("MemoryMax=1G",)
        )
        assert "--property=MemoryMax=1G" in argv
        assert argv.index("--property=MemoryMax=1G") < argv.index("--")

    def test_a_description_is_passed(self):
        argv = SD.build_transient_argv(
            "onedriveui-bisync-x", ["/bin/true"], description="OneDrive bisync"
        )
        assert "--description=OneDrive bisync" in argv

    def test_collect_can_be_declined(self):
        argv = SD.build_transient_argv("onedriveui-bisync-x", ["/bin/true"], collect=False)
        assert "--collect" not in argv

    def test_the_command_is_separated_by_a_double_dash(self):
        argv = SD.build_transient_argv("onedriveui-bisync-x", ["/bin/true", "--user"])
        assert argv[argv.index("--") + 1:] == ["/bin/true", "--user"]

    def test_an_empty_command_is_refused(self):
        with pytest.raises(SafetyRefusal):
            SD.build_transient_argv("onedriveui-bisync-x", [])

    def test_an_invalid_unit_name_is_refused(self):
        with pytest.raises(SafetyRefusal):
            SD.build_transient_argv("../evil", ["/bin/true"])

    def test_run_transient_invokes_systemd_run(self, monkeypatch):
        recorded: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            recorded["argv"] = argv
            recorded["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/systemd-run")
        monkeypatch.setattr(SD.subprocess, "run", fake_run)

        unit = SD.run_transient("onedriveui-bisync-onedrive", ["/usr/bin/rclone", "bisync"])
        assert unit == "onedriveui-bisync-onedrive.service"
        assert recorded["argv"][0:3] == ["systemd-run", "--user",
                                         "--unit=onedriveui-bisync-onedrive"]
        assert recorded["timeout"] == SD.RUN_TIMEOUT_S

    def test_run_transient_keeps_an_explicit_suffix(self, monkeypatch):
        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/systemd-run")
        monkeypatch.setattr(
            SD.subprocess, "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )
        assert SD.run_transient("x.service", ["/bin/true"]) == "x.service"

    def test_run_transient_raises_on_a_non_zero_exit(self, monkeypatch):
        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/systemd-run")
        monkeypatch.setattr(
            SD.subprocess, "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "unit exists"),
        )
        with pytest.raises(OneDriveUIError) as excinfo:
            SD.run_transient("onedriveui-bisync-x", ["/bin/true"])
        assert "unit exists" in str(excinfo.value)

    def test_run_transient_raises_when_systemd_run_is_missing(self, monkeypatch):
        monkeypatch.setattr(SD.shutil, "which", lambda _n: None)
        with pytest.raises(OneDriveUIError) as excinfo:
            SD.run_transient("onedriveui-bisync-x", ["/bin/true"])
        assert "systemd-run" in str(excinfo.value)

    def test_run_transient_raises_on_a_timeout(self, monkeypatch):
        def boom(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, SD.RUN_TIMEOUT_S)

        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/systemd-run")
        monkeypatch.setattr(SD.subprocess, "run", boom)
        with pytest.raises(OneDriveUIError):
            SD.run_transient("onedriveui-bisync-x", ["/bin/true"])

    def test_run_transient_passes_extra_environment(self, monkeypatch):
        recorded: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            recorded["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/systemd-run")
        monkeypatch.setattr(SD.subprocess, "run", fake_run)
        SD.run_transient("onedriveui-bisync-x", ["/bin/true"], env={"RCLONE_X": "1"})
        assert recorded["env"]["RCLONE_X"] == "1"
        assert "PATH" in recorded["env"]


# ═════════════════════════════════════════════════════════════════════════════
# Journal
# ═════════════════════════════════════════════════════════════════════════════

class TestJournal:
    def test_lines_are_parsed(self, monkeypatch):
        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/journalctl")
        monkeypatch.setattr(
            SD.subprocess, "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "one\ntwo\n\n", ""),
        )
        assert SD.journal_tail(UNIT_RCD) == ["one", "two"]

    def test_the_command_is_user_scoped_and_bounded(self, monkeypatch):
        recorded: dict[str, object] = {}

        def fake_run(argv, **_kwargs):
            recorded["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/journalctl")
        monkeypatch.setattr(SD.subprocess, "run", fake_run)
        SD.journal_tail(UNIT_RCD, 25)
        assert recorded["argv"] == [
            "journalctl", "--user", "-u", UNIT_RCD, "-n", "25",
            "--no-pager", "-o", "cat",
        ]

    def test_a_missing_journalctl_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(SD.shutil, "which", lambda _n: None)
        assert SD.journal_tail(UNIT_RCD) == []

    def test_a_failure_returns_nothing(self, monkeypatch):
        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/journalctl")
        monkeypatch.setattr(
            SD.subprocess, "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"),
        )
        assert SD.journal_tail(UNIT_RCD) == []

    def test_a_timeout_returns_nothing(self, monkeypatch):
        def boom(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, SD.JOURNAL_TIMEOUT_S)

        monkeypatch.setattr(SD.shutil, "which", lambda _n: "/usr/bin/journalctl")
        monkeypatch.setattr(SD.subprocess, "run", boom)
        assert SD.journal_tail(UNIT_RCD) == []

    def test_an_invalid_unit_name_is_refused(self):
        with pytest.raises(SafetyRefusal):
            SD.journal_tail("../evil")


# ═════════════════════════════════════════════════════════════════════════════
# Live: read-only, against this machine's real user manager
# ═════════════════════════════════════════════════════════════════════════════

def _user_manager_present() -> bool:
    return Path(f"/run/user/{os.getuid()}/systemd/private").exists() or bool(
        os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    )


@pytest.mark.live
@pytest.mark.skipif(not _user_manager_present(), reason="no systemd --user manager")
class TestLive:
    """Strictly read-only. Nothing here writes, enables, starts or stops."""

    @pytest.fixture(autouse=True)
    def _real_bus(self, qapp):
        SD.set_bus(None)
        yield
        SD.set_bus(None)

    def test_the_user_manager_answers(self):
        assert SD.available() is True

    def test_the_live_rclone_mount_unit_is_active(self):
        if not SD.exists(LIVE_UNIT):
            pytest.skip(f"{LIVE_UNIT} is not installed on this machine")
        active, sub = SD.state(LIVE_UNIT)
        assert active == SD.ACTIVE
        assert sub == "running"
        assert SD.is_active(LIVE_UNIT) is True
        assert SD.is_failed(LIVE_UNIT) is False

    def test_the_live_unit_publishes_a_status_text(self):
        if not SD.exists(LIVE_UNIT):
            pytest.skip(f"{LIVE_UNIT} is not installed on this machine")
        assert SD.main_pid(LIVE_UNIT) > 0
        assert SD.is_enabled(LIVE_UNIT) in ("enabled", "disabled", "static", "linked")

    def test_a_nonexistent_unit_reports_not_found(self):
        name = "onedriveui-test-does-not-exist.service"
        assert SD.state(name) == (SD.INACTIVE, SD.NOT_FOUND)
        assert SD.is_active(name) is False
        assert SD.exists(name) is False

    def test_network_online_target_really_is_absent_from_the_user_manager(self):
        """The measurement the invariant rests on."""
        assert SD.exists(SD.FORBIDDEN_TARGET) is False
        assert SD.state(SD.FORBIDDEN_TARGET) == (SD.INACTIVE, SD.NOT_FOUND)

    def test_graphical_session_target_does_exist(self):
        """The target our units order against, by contrast."""
        assert SD.exists("graphical-session.target") is True

    def test_journal_tail_reads_the_real_journal(self):
        if not SD.exists(LIVE_UNIT):
            pytest.skip(f"{LIVE_UNIT} is not installed on this machine")
        lines = SD.journal_tail(LIVE_UNIT, 3)
        assert isinstance(lines, list)
        assert len(lines) <= 3
