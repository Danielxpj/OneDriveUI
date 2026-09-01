"""Tests for `onedriveui.platform.autostart`.

The one rule this module exists to enforce is that **exactly one** autostart
mechanism is ever installed. Everything else here is in service of proving that:
the two writers, the two removers, the reporting functions and the migration
between methods are all checked for it, and `test_never_both_*` attacks it from
every direction a caller could reach.

No live systemd is touched. `systemd.set_bus()` takes a fake manager that
records `EnableUnitFiles`/`DisableUnitFiles`/`Reload`, and `conftest`'s
`_isolate_home` keeps `~/.config/systemd/user` and `~/.config/autostart` inside
a temp tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from gi.repository import Gio, GLib

from onedriveui import APP_DISPLAY_NAME, APP_ID
from onedriveui import paths
from onedriveui.constants import ORDERING_GUI, UNIT_GUI, UNIT_RCD
from onedriveui.errors import SafetyRefusal
from onedriveui.platform import autostart as A
from onedriveui.platform import desktop as D
from onedriveui.platform import systemd as SD

EXEC = "/usr/bin/onedriveui"


# ═════════════════════════════════════════════════════════════════════════════
# A fake systemd manager
# ═════════════════════════════════════════════════════════════════════════════

class FakeManagerBus:
    """Answers the handful of Manager calls `autostart` makes.

    Builds a real `GLib.Variant` from every signature/args pair, exactly as
    `tests/test_systemd.py` does, so a marshalling mistake fails here with no
    D-Bus traffic at all.
    """

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self.unit_file_state: dict[str, str] = {}
        self.fail_methods: set[str] = set()

    def of(self, method: str) -> list[SimpleNamespace]:
        return [c for c in self.calls if c.method == method]

    def call(self, name, path, iface, method, *, signature=None, args=(),
             reply=None, timeout_ms=2000, auto_start=False):
        variant = GLib.Variant(signature, tuple(args)) if signature else None
        self.calls.append(SimpleNamespace(
            name=name, path=path, iface=iface, method=method,
            signature=signature, args=tuple(args), variant=variant))
        if method in self.fail_methods:
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), f"fake {method} failure",
                Gio.IOErrorEnum.FAILED)
        if method == "EnableUnitFiles":
            self.unit_file_state[args[0][0]] = "enabled"
            return (True, [("symlink", "/x", "/y")])
        if method == "DisableUnitFiles":
            self.unit_file_state.pop(args[0][0], None)
            return ([("unlink", "/x", "")],)
        if method == "GetUnitFileState":
            return (self.unit_file_state.get(args[0], "disabled"),)
        if method in ("StartUnit", "StopUnit"):
            return ("/org/freedesktop/systemd1/job/1",)
        return ()

    def call_or_none(self, name, path, iface, method, **kwargs):
        try:
            return self.call(name, path, iface, method, **kwargs)
        except GLib.Error:
            return None

    def get_property(self, name, path, iface, prop, default=None, **_kw):
        return default

    def get_all(self, name, path, iface):
        return {}


@pytest.fixture
def fake_systemd():
    """Install a fake `org.freedesktop.systemd1` for the duration of a test."""
    bus = FakeManagerBus()
    SD.set_bus(bus)
    try:
        yield bus
    finally:
        SD.set_bus(None)


@pytest.fixture(autouse=True)
def _clean(_isolate_home, fake_systemd):
    """Every test starts with neither method installed."""
    yield
    A.remove_desktop_file()
    SD.remove_unit(A.UNIT, reload=False)


# ═════════════════════════════════════════════════════════════════════════════
# Methods
# ═════════════════════════════════════════════════════════════════════════════

def test_methods_are_the_config_schema_values():
    """`app.autostart_method` is documented as "systemd"|"xdg" — §9."""
    assert A.METHODS == ("systemd", "xdg")
    assert A.METHOD_SYSTEMD == "systemd"
    assert A.METHOD_XDG == "xdg"
    assert A.METHOD_NONE not in A.METHODS


@pytest.mark.parametrize("bad", ["", "SYSTEMD", "xdg ", "launchd", "none", "both"])
def test_assert_valid_method_rejects_anything_else(bad):
    with pytest.raises(SafetyRefusal) as caught:
        A.assert_valid_method(bad)
    assert caught.value.invariant == A.AUTOSTART_RULE


def test_assert_valid_method_returns_the_method():
    assert A.assert_valid_method(A.METHOD_XDG) == A.METHOD_XDG


def test_unit_name_comes_from_constants():
    assert A.UNIT is UNIT_GUI


# ═════════════════════════════════════════════════════════════════════════════
# THE RULE: never both
# ═════════════════════════════════════════════════════════════════════════════

def test_nothing_installed_reports_none():
    assert A.installed_methods() == ()
    assert A.method() == A.METHOD_NONE
    assert A.enabled() is False
    assert A.conflict() is False
    assert A.assert_exclusive() == A.METHOD_NONE


def test_never_both_when_installing_the_unit_over_an_entry():
    A.install_desktop_file(EXEC)
    assert A.installed_methods() == (A.METHOD_XDG,)

    A.install_gui_unit(EXEC)

    assert A.installed_methods() == (A.METHOD_SYSTEMD,)
    assert not paths.autostart_file().exists()
    assert A.method() == A.METHOD_SYSTEMD


def test_never_both_when_installing_an_entry_over_the_unit():
    A.install_gui_unit(EXEC)
    assert A.installed_methods() == (A.METHOD_SYSTEMD,)

    A.install_desktop_file(EXEC)

    assert A.installed_methods() == (A.METHOD_XDG,)
    assert not SD.unit_file(A.UNIT).exists()
    assert A.method() == A.METHOD_XDG


@pytest.mark.parametrize("first", [A.METHOD_SYSTEMD, A.METHOD_XDG])
@pytest.mark.parametrize("second", [A.METHOD_SYSTEMD, A.METHOD_XDG])
def test_never_both_across_every_set_enabled_migration(first, second):
    """Any method -> any method leaves exactly one installed."""
    assert A.set_enabled(True, first) == first
    assert A.set_enabled(True, second) == second
    assert len(A.installed_methods()) == 1
    assert A.installed_methods() == (second,)


def test_assert_exclusive_raises_when_both_are_on_disk():
    """The post-condition fires even when the state was reached behind our back."""
    A.install_gui_unit(EXEC)
    # Hand-write an autostart entry, as a stale package or a restored backup
    # would: bypassing install_desktop_file() bypasses its own removal step.
    paths.autostart_file().write_text(A.autostart_entry_text(EXEC), encoding="utf-8")

    assert A.conflict() is True
    with pytest.raises(SafetyRefusal) as caught:
        A.assert_exclusive()
    assert caught.value.invariant == A.AUTOSTART_RULE
    assert "twice" in str(caught.value)


def test_method_never_raises_on_a_conflict(caplog):
    """The tray repaints from `method()`; it must degrade, not explode."""
    A.install_gui_unit(EXEC)
    paths.autostart_file().write_text(A.autostart_entry_text(EXEC), encoding="utf-8")

    assert A.method() == A.METHOD_SYSTEMD          # systemd wins: it supervises
    assert A.enabled() is True


def test_repair_removes_the_losing_method():
    A.install_gui_unit(EXEC)
    paths.autostart_file().write_text(A.autostart_entry_text(EXEC), encoding="utf-8")
    assert A.conflict() is True

    assert A.repair() == A.METHOD_SYSTEMD

    assert A.conflict() is False
    assert not paths.autostart_file().exists()
    assert SD.unit_file(A.UNIT).exists()


def test_repair_is_a_no_op_when_only_one_is_installed():
    A.install_desktop_file(EXEC)
    assert A.repair() == A.METHOD_XDG
    assert paths.autostart_file().exists()


def test_set_enabled_false_removes_both():
    A.install_gui_unit(EXEC)
    paths.autostart_file().write_text("[Desktop Entry]\nType=Application\n",
                                      encoding="utf-8")

    assert A.set_enabled(False) == A.METHOD_NONE

    assert not SD.unit_file(A.UNIT).exists()
    assert not paths.autostart_file().exists()
    assert A.enabled() is False


# ═════════════════════════════════════════════════════════════════════════════
# The systemd unit
# ═════════════════════════════════════════════════════════════════════════════

def test_gui_unit_text_uses_the_frozen_ordering():
    text = A.gui_unit_text(EXEC)
    assert ORDERING_GUI in text
    assert f"WantedBy={A.GUI_TARGET}" in text
    assert f"ExecStart={EXEC} {D.FLAG_BACKGROUND}" in text
    assert f"Wants={UNIT_RCD}" in text


def test_gui_unit_is_wanted_by_graphical_session_not_default():
    """`default.target` is reached before a display exists — §4.2."""
    text = A.gui_unit_text(EXEC)
    assert "WantedBy=graphical-session.target" in text
    assert "WantedBy=default.target" not in text


def test_gui_unit_never_names_network_online_target():
    """It does not exist in the --user manager; write_unit() would refuse it."""
    assert SD.FORBIDDEN_TARGET not in A.gui_unit_text(EXEC)
    A.install_gui_unit(EXEC)          # would raise SafetyRefusal if it did


def test_gui_unit_has_restart_supervision():
    text = A.gui_unit_text(EXEC)
    assert "Restart=on-failure" in text
    assert f"RestartSec={A.RESTART_SEC}" in text
    assert f"StartLimitBurst={A.START_LIMIT_BURST}" in text


def test_install_gui_unit_writes_enables_and_reports_change(fake_systemd):
    assert A.install_gui_unit(EXEC) is True
    assert SD.unit_file(A.UNIT).is_file()
    assert [c.args[0] for c in fake_systemd.of("EnableUnitFiles")] == [[A.UNIT]]
    assert A.unit_enabled() is True

    # Idempotent: the same text is not rewritten.
    assert A.install_gui_unit(EXEC) is False


def test_install_gui_unit_survives_an_enable_failure(fake_systemd):
    """The file is on disk and correct; only the symlink is missing."""
    fake_systemd.fail_methods.add("EnableUnitFiles")
    assert A.install_gui_unit(EXEC) is True
    assert SD.unit_file(A.UNIT).is_file()
    assert A.unit_installed() is True


def test_remove_gui_unit_disables_first(fake_systemd):
    A.install_gui_unit(EXEC)
    assert A.remove_gui_unit() is True
    assert fake_systemd.of("DisableUnitFiles")
    assert not SD.unit_file(A.UNIT).exists()
    assert A.remove_gui_unit() is False


def test_unit_installed_needs_no_bus():
    """A pure filesystem check: it must answer with the manager unreachable."""
    A.install_gui_unit(EXEC)
    SD.set_bus(None)
    try:
        assert A.unit_installed() is True
    finally:
        SD.set_bus(FakeManagerBus())


def test_install_gui_unit_now_starts_it(fake_systemd):
    A.install_gui_unit(EXEC, now=True)
    assert fake_systemd.of("StartUnit")


# ═════════════════════════════════════════════════════════════════════════════
# The XDG autostart entry
# ═════════════════════════════════════════════════════════════════════════════

def test_autostart_entry_lands_in_the_right_place():
    A.install_desktop_file(EXEC)
    target = paths.autostart_file()
    assert target == paths.autostart_dir() / f"{APP_ID}.desktop"
    assert target.is_file()
    assert (target.stat().st_mode & 0o777) == D.ENTRY_MODE


def test_autostart_entry_content():
    text = A.autostart_entry_text(EXEC)
    assert text.startswith(f"[{D.DESKTOP_GROUP}]\n")
    assert f"Name={APP_DISPLAY_NAME}\n" in text
    assert f"Exec={EXEC} {D.FLAG_BACKGROUND}\n" in text
    assert "NoDisplay=true\n" in text
    assert f"{A.GNOME_ENABLED_KEY}=true\n" in text
    assert f"{A.HIDDEN_KEY}=false\n" in text
    assert f"X-GNOME-Autostart-Delay={A.AUTOSTART_DELAY_S}\n" in text


def test_autostart_entry_declares_no_field_code():
    """Nothing hands a login-time launch a URL; %U would be a validation error."""
    text = A.autostart_entry_text(EXEC)
    exec_line = next(l for l in text.splitlines() if l.startswith("Exec="))
    assert "%" not in exec_line


def test_autostart_entry_has_exactly_one_main_category():
    text = A.autostart_entry_text(EXEC)
    categories = next(l for l in text.splitlines() if l.startswith("Categories="))
    named = [c for c in categories.removeprefix("Categories=").split(";") if c]
    assert sum(1 for c in named if c in D.MAIN_CATEGORIES) == 1


def test_install_desktop_file_is_idempotent():
    assert A.install_desktop_file(EXEC) is True
    assert A.install_desktop_file(EXEC) is False


def test_remove_desktop_file():
    A.install_desktop_file(EXEC)
    assert A.remove_desktop_file() is True
    assert A.remove_desktop_file() is False


# ── the disable keys we read but never write ────────────────────────────────

def test_a_hidden_entry_is_not_active():
    A.install_desktop_file(EXEC)
    target = paths.autostart_file()
    target.write_text(target.read_text().replace(f"{A.HIDDEN_KEY}=false",
                                                 f"{A.HIDDEN_KEY}=true"))
    assert A.desktop_file_installed() is True
    assert A.desktop_file_active() is False
    assert A.method() == A.METHOD_NONE


def test_a_gnome_disabled_entry_is_not_active():
    """What GNOME Tweaks writes when the user unticks us."""
    A.install_desktop_file(EXEC)
    target = paths.autostart_file()
    target.write_text(target.read_text().replace(
        f"{A.GNOME_ENABLED_KEY}=true", f"{A.GNOME_ENABLED_KEY}=false"))
    assert A.desktop_file_active() is False
    assert A.enabled() is False


def test_a_disabled_entry_does_not_count_as_a_conflict():
    """An inert entry cannot cause a double launch, so it is not one."""
    A.install_gui_unit(EXEC)
    paths.autostart_file().write_text(
        A.autostart_entry_text(EXEC).replace(f"{A.HIDDEN_KEY}=false",
                                             f"{A.HIDDEN_KEY}=true"),
        encoding="utf-8")
    assert A.conflict() is False
    assert A.assert_exclusive() == A.METHOD_SYSTEMD


def test_keys_outside_the_desktop_entry_group_are_ignored():
    paths.autostart_file().write_text(
        f"[{D.DESKTOP_GROUP}]\nType=Application\n"
        f"[Desktop Action X]\n{A.HIDDEN_KEY}=true\n", encoding="utf-8")
    assert A.desktop_file_active() is True


# ═════════════════════════════════════════════════════════════════════════════
# desktop-file-validate — the acceptance criterion
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not Path("/usr/bin/desktop-file-validate").exists(),
                    reason="desktop-file-validate is not installed")
def test_autostart_entry_validates_with_no_output(tmp_path):
    """Exit 0 AND no output: the category hint exits 0 too."""
    target = tmp_path / f"{APP_ID}.desktop"
    target.write_text(A.autostart_entry_text(EXEC), encoding="utf-8")

    result = subprocess.run(["desktop-file-validate", str(target)],
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (result.stdout + result.stderr).strip() == ""


@pytest.mark.skipif(not Path("/usr/bin/systemd-analyze").exists(),
                    reason="systemd-analyze is not installed")
def test_gui_unit_verifies_under_systemd_analyze(tmp_path):
    """A real systemd parses the unit with no warnings.

    `ExecStart` points at `/usr/bin/true`, because the real entry point is
    WP-14's and `systemd-analyze` checks that the binary exists.
    """
    unit = tmp_path / UNIT_GUI
    unit.write_text(A.gui_unit_text("/usr/bin/true"), encoding="utf-8")

    result = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)],
                            capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (result.stdout + result.stderr).strip() == ""
