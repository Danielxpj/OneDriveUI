"""Tests for `onedriveui.platform.desktop`.

Three claims carry real weight and are checked against reality, not against my
own code:

* the generated `.desktop` passes the **real** `desktop-file-validate` with no
  output at all — the acceptance criterion says "no category warning", and that
  warning exits 0, so a clean run is defined as empty output;
* `file_uri()` is byte-identical to `Gio.File.new_for_path().get_uri()` across
  the printable ASCII range and beyond, because the freedesktop thumbnail name
  is `md5(uri)` and one differing escape means every cached thumbnail is a miss;
* `device_id()` is a one-way hash and never leaks `/etc/machine-id`.

`FileManager1.ShowItems` runs against a fake bus that builds the real
`GLib.Variant` from the signature, so a marshalling mistake fails with no D-Bus
traffic; a live test then drives the real, D-Bus-activated service.
"""

from __future__ import annotations

import hashlib
import os
import string
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from gi.repository import Gio, GLib

from onedriveui import APP_DISPLAY_NAME, APP_ID
from onedriveui import paths
from onedriveui.errors import SafetyRefusal
from onedriveui.models import KfmFolder
from onedriveui.platform import desktop as D
from onedriveui.strings import MENU

EXEC = "/usr/bin/onedriveui"


# ═════════════════════════════════════════════════════════════════════════════
# A fake session bus
# ═════════════════════════════════════════════════════════════════════════════

class FakeBus:
    """Records calls, marshalling each one through a real `GLib.Variant`."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[SimpleNamespace] = []
        self.fail = fail

    def call(self, name, path, iface, method, *, signature=None, args=(),
             reply=None, timeout_ms=2000, auto_start=False):
        variant = GLib.Variant(signature, tuple(args)) if signature else None
        self.calls.append(SimpleNamespace(
            name=name, path=path, iface=iface, method=method,
            signature=signature, args=tuple(args), variant=variant,
            timeout_ms=timeout_ms, auto_start=auto_start))
        if self.fail:
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), "no file manager", Gio.IOErrorEnum.NOT_FOUND)
        return ()

    def call_or_none(self, name, path, iface, method, **kwargs):
        try:
            return self.call(name, path, iface, method, **kwargs)
        except GLib.Error:
            return None

    def get_property(self, name, path, iface, prop, default=None, **_kw):
        return default


@pytest.fixture
def fake_bus():
    bus = FakeBus()
    D.set_bus(bus)
    try:
        yield bus
    finally:
        D.set_bus(None)


# ═════════════════════════════════════════════════════════════════════════════
# device_id
# ═════════════════════════════════════════════════════════════════════════════

def test_device_id_is_sha256_of_machine_id_truncated():
    raw = D._read_machine_id()
    expected = hashlib.sha256(raw.encode()).hexdigest()[:D.DEVICE_ID_CHARS]
    assert D.device_id() == expected


def test_device_id_never_contains_the_raw_machine_id():
    raw = D._read_machine_id()
    device = D.device_id()
    assert raw, "this machine has no /etc/machine-id to test against"
    assert raw not in device
    assert device != raw
    assert device != raw[:D.DEVICE_ID_CHARS]


def test_device_id_shape():
    device = D.device_id()
    assert len(device) == 16
    assert all(c in string.hexdigits.lower() for c in device)


def test_device_id_is_stable():
    assert D.device_id() == D.device_id()


def test_device_id_survives_a_missing_machine_id(monkeypatch):
    """A container has no /etc/machine-id; the id must still be well-formed."""
    monkeypatch.setattr(D, "MACHINE_ID_FILES", ("/nonexistent/machine-id",))
    device = D.device_id()
    assert len(device) == D.DEVICE_ID_CHARS
    assert device == hashlib.sha256(
        D.DEVICE_ID_FALLBACK_SEED.encode()).hexdigest()[:D.DEVICE_ID_CHARS]


# ═════════════════════════════════════════════════════════════════════════════
# URIs
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    "plain.txt", "a b.txt", "hash#tag.txt", "per%cent.txt", "it's.txt",
    "AlbumArt_{124EC83E}.jpg", "ünïcødé.txt", "日本語.txt", "emoji😀.png",
    "semi;colon.txt", "back\\slash.txt", "q?uery.txt", "brack[et].txt",
    "plus+and&amp.txt", "at@sign.txt", "eq=uals.txt", "til~de.txt",
])
def test_file_uri_matches_gio_exactly(name):
    """`md5(uri)` is the thumbnail name, so one differing escape breaks lookup."""
    path = f"/home/u/OneDrive/{name}"
    assert D.file_uri(path) == Gio.File.new_for_path(path).get_uri()


def test_file_uri_covers_every_printable_ascii_character():
    for char in string.printable[:94]:
        path = f"/home/u/a{char}b"
        assert D.file_uri(path) == Gio.File.new_for_path(path).get_uri(), repr(char)


def test_file_uri_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert D.file_uri("rel.txt") == D.file_uri(tmp_path / "rel.txt")


def test_file_uri_expands_home():
    assert D.file_uri("~/x.txt") == D.file_uri(Path.home() / "x.txt")


def test_file_uri_does_not_resolve_symlinks(tmp_path):
    """A KFM symlink has its own identity; resolving would reveal the target."""
    target = tmp_path / "real.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert D.file_uri(link).endswith("/link.txt")


@pytest.mark.parametrize("name", ["a b.txt", "hash#tag.txt", "ünïcødé.txt"])
def test_uri_to_path_round_trips(name):
    path = f"/home/u/{name}"
    assert D.uri_to_path(D.file_uri(path)) == path


def test_uri_to_path_rejects_other_schemes():
    assert D.uri_to_path("https://example.com/x") is None
    assert D.uri_to_path("trash:///x") is None


# ═════════════════════════════════════════════════════════════════════════════
# FileManager1
# ═════════════════════════════════════════════════════════════════════════════

def test_show_in_folder_calls_show_items(fake_bus):
    assert D.show_in_folder("/home/u/OneDrive/Report.docx") is True

    call = fake_bus.calls[-1]
    assert call.name == D.FM1_NAME
    assert call.path == D.FM1_PATH
    assert call.iface == D.FM1_IFACE
    assert call.method == D.FM1_SHOW_ITEMS
    assert call.signature == "(ass)"
    assert call.args == (["file:///home/u/OneDrive/Report.docx"], "")


def test_show_in_folder_auto_starts_the_file_manager(fake_bus):
    """FileManager1 is D-Bus activated; Nautilus need not already be running."""
    D.show_in_folder("/home/u/x")
    assert fake_bus.calls[-1].auto_start is True


def test_show_in_folder_passes_a_startup_id(fake_bus):
    D.show_in_folder("/home/u/x", startup_id="tok-1")
    assert fake_bus.calls[-1].args[1] == "tok-1"


def test_show_in_folder_sends_every_target_in_one_call(fake_bus):
    D.show_in_folder("/home/u/a", "/home/u/b", "/home/u/c")
    assert len(fake_bus.calls) == 1
    assert len(fake_bus.calls[0].args[0]) == 3


def test_show_in_folder_with_no_targets_does_nothing(fake_bus):
    assert D.show_in_folder() is False
    assert fake_bus.calls == []


def test_show_in_folder_falls_back_to_the_parent_folder(monkeypatch):
    """Worse than a selection, but better than nothing."""
    D.set_bus(FakeBus(fail=True))
    opened: list[str] = []
    monkeypatch.setattr(D, "open_path", lambda p: opened.append(str(p)) or True)
    try:
        assert D.show_in_folder("/home/u/OneDrive/deep/Report.docx") is True
    finally:
        D.set_bus(None)
    assert opened == ["/home/u/OneDrive/deep"]


def test_show_folders_and_properties_use_their_own_methods(fake_bus):
    D.show_folders("/home/u/OneDrive")
    assert fake_bus.calls[-1].method == D.FM1_SHOW_FOLDERS
    D.show_properties("/home/u/OneDrive/x")
    assert fake_bus.calls[-1].method == D.FM1_SHOW_PROPERTIES


def test_open_url_refuses_a_schemeless_string(monkeypatch):
    opened: list[object] = []
    monkeypatch.setattr(D.QDesktopServices, "openUrl",
                        staticmethod(lambda u: opened.append(u) or True))
    assert D.open_url("/home/u/not-a-url") is False
    assert D.open_url("") is False
    assert opened == []


def test_open_url_accepts_a_real_url(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(D.QDesktopServices, "openUrl",
                        staticmethod(lambda u: opened.append(u.toString()) or True))
    assert D.open_url("https://onedrive.live.com/") is True
    assert opened == ["https://onedrive.live.com/"]


def test_open_path_makes_a_file_url(monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(D.QDesktopServices, "openUrl",
                        staticmethod(lambda u: opened.append(u.toString()) or True))
    assert D.open_path(tmp_path) is True
    assert opened[0].startswith("file://")


# ═════════════════════════════════════════════════════════════════════════════
# The .desktop entry
# ═════════════════════════════════════════════════════════════════════════════

def test_categories_have_exactly_one_main_category():
    assert sum(1 for c in D.CATEGORIES if c in D.MAIN_CATEGORIES) == 1
    assert D.CATEGORIES == ("Network", "FileTransfer")


def test_assert_one_main_category_refuses_two():
    with pytest.raises(SafetyRefusal) as caught:
        D.assert_one_main_category(("Network", "Utility"))
    assert caught.value.invariant == D.ENTRY_RULE
    assert "more than once" in str(caught.value)


def test_assert_one_main_category_allows_additional_categories():
    assert D.assert_one_main_category(("Network", "FileTransfer", "P2P")) == (
        "Network", "FileTransfer", "P2P")


def test_build_desktop_entry_requires_the_desktop_entry_group_first():
    with pytest.raises(SafetyRefusal):
        D.build_desktop_entry([("Desktop Action X", {"Name": "x"})])
    with pytest.raises(SafetyRefusal):
        D.build_desktop_entry([])


def test_build_desktop_entry_renders_types():
    text = D.build_desktop_entry([(D.DESKTOP_GROUP, {
        "Type": "Application", "Terminal": False, "StartupNotify": True,
        "Categories": ("A", "B"), "Dropped": None, "Count": 8,
    })])
    assert "Terminal=false" in text
    assert "StartupNotify=true" in text
    assert "Categories=A;B;" in text
    assert "Count=8" in text
    assert "Dropped" not in text


def test_build_desktop_entry_escapes_newlines():
    text = D.build_desktop_entry([(D.DESKTOP_GROUP, {"Comment": "a\nb\tc\\d"})])
    assert "Comment=a\\nb\\tc\\\\d" in text
    assert text.count("\n") == text.count("\n")   # no raw newline leaked in


def test_desktop_entry_content():
    text = D.desktop_entry_text(EXEC)
    assert text.startswith(f"[{D.DESKTOP_GROUP}]\n")
    assert f"Name={APP_DISPLAY_NAME}\n" in text
    assert f"Icon={APP_ID}\n" in text
    assert f"Exec={EXEC} %U\n" in text
    assert f"StartupWMClass={D.STARTUP_WM_CLASS}\n" in text
    assert "SingleMainWindow=true\n" in text
    assert "Categories=Network;FileTransfer;\n" in text
    assert f"MimeType=x-scheme-handler/{D.URI_SCHEME};\n" in text


def test_desktop_entry_uses_percent_u_not_percent_f():
    """A scheme handler takes URLs; %U and %F are mutually exclusive."""
    exec_line = next(l for l in D.desktop_entry_text(EXEC).splitlines()
                     if l.startswith("Exec=") )
    assert exec_line.endswith(" %U")
    assert "%F" not in exec_line


def test_desktop_actions_use_the_frozen_menu_strings():
    """Dash-icon actions and tray items say the same words — strings.py owns them."""
    text = D.desktop_entry_text(EXEC)
    assert f"Name={MENU.OPEN_FOLDER}\n" in text
    assert f"Name={MENU.PAUSE}\n" in text
    assert f"Name={MENU.SETTINGS}\n" in text
    assert "Actions=OpenFolder;Pause;Settings;\n" in text
    for _action, _label, flag in D.DESKTOP_ACTIONS:
        assert f"Exec={EXEC} {flag}\n" in text


def test_every_declared_action_has_a_group():
    text = D.desktop_entry_text(EXEC)
    for action, _label, _flag in D.DESKTOP_ACTIONS:
        assert f"[Desktop Action {action}]" in text


def test_install_desktop_entry_writes_0644_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(D, "update_desktop_database", lambda *_a, **_k: True)
    assert D.install_desktop_entry(EXEC) is True
    target = paths.desktop_file()
    assert target.is_file()
    assert (target.stat().st_mode & 0o777) == D.ENTRY_MODE
    assert D.install_desktop_entry(EXEC) is False
    assert D.remove_desktop_entry() is True
    assert D.remove_desktop_entry() is False


def test_executable_command_falls_back_to_the_module(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda _n: None)
    assert D.executable_command().endswith(f" -m {APP_ID}")

    monkeypatch.setattr(D.shutil, "which", lambda _n: "/usr/bin/onedriveui")
    assert D.executable_command() == "/usr/bin/onedriveui"


@pytest.mark.skipif(not Path("/usr/bin/desktop-file-validate").exists(),
                    reason="desktop-file-validate is not installed")
def test_desktop_entry_validates_with_no_output(tmp_path):
    """The acceptance criterion: exit 0 AND no category hint."""
    target = tmp_path / f"{APP_ID}.desktop"
    target.write_text(D.desktop_entry_text(EXEC), encoding="utf-8")

    result = subprocess.run(["desktop-file-validate", str(target)],
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (result.stdout + result.stderr).strip() == ""


@pytest.mark.skipif(not Path("/usr/bin/desktop-file-validate").exists(),
                    reason="desktop-file-validate is not installed")
def test_validate_desktop_file_reports_a_hint_as_a_failure(tmp_path):
    """A hint exits 0, so "clean" must mean empty output, not exit 0."""
    bad = tmp_path / "bad.desktop"
    bad.write_text(D.desktop_entry_text(EXEC).replace(
        "Categories=Network;FileTransfer;", "Categories=Network;Utility;"),
        encoding="utf-8")

    ok, output = D.validate_desktop_file(bad)

    assert ok is False
    assert "main category" in output


def test_validate_desktop_file_tolerates_a_missing_validator(monkeypatch, tmp_path):
    monkeypatch.setattr(D.shutil, "which", lambda _n: None)
    assert D.validate_desktop_file(tmp_path / "nope.desktop") == (True, "")


# ═════════════════════════════════════════════════════════════════════════════
# GTK bookmarks
# ═════════════════════════════════════════════════════════════════════════════

def test_bookmark_is_written_to_gtk3_and_gtk4():
    """Both files, because Nautilus 50 is GTK4 but GTK3 choosers are everywhere."""
    assert D.add_sidebar_bookmark("/home/u/OneDrive - Personal") is True
    files = paths.gtk_bookmarks()
    assert len(files) == 2
    assert {f.parent.name for f in files} == {"gtk-3.0", "gtk-4.0"}
    for target in files:
        assert target.read_text().strip() == (
            f"file:///home/u/OneDrive%20-%20Personal {APP_DISPLAY_NAME}")


def test_bookmark_is_idempotent():
    assert D.add_sidebar_bookmark("/home/u/OneDrive") is True
    assert D.add_sidebar_bookmark("/home/u/OneDrive") is False
    for target in paths.gtk_bookmarks():
        assert target.read_text().count("file:///home/u/OneDrive") == 1


def test_bookmark_appends_and_preserves_the_users_own_entries():
    """The file is the user's; a bookmark they added must survive us."""
    for target in paths.gtk_bookmarks():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("file:///home/u/Projects Code\n", encoding="utf-8")

    D.add_sidebar_bookmark("/home/u/OneDrive")

    for target in paths.gtk_bookmarks():
        lines = target.read_text().splitlines()
        assert lines[0] == "file:///home/u/Projects Code"
        assert lines[1].startswith("file:///home/u/OneDrive")


def test_bookmark_repairs_a_file_with_no_trailing_newline():
    for target in paths.gtk_bookmarks():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("file:///home/u/Projects Code", encoding="utf-8")

    D.add_sidebar_bookmark("/home/u/OneDrive")

    for target in paths.gtk_bookmarks():
        assert len(target.read_text().splitlines()) == 2


def test_bookmark_label_is_omitted_when_it_equals_the_basename():
    """What GTK itself writes."""
    D.add_sidebar_bookmark("/home/u/OneDrive", "OneDrive")
    assert paths.gtk_bookmarks()[0].read_text().strip() == "file:///home/u/OneDrive"


def test_remove_sidebar_bookmark():
    D.add_sidebar_bookmark("/home/u/OneDrive")
    assert D.remove_sidebar_bookmark("/home/u/OneDrive") is True
    assert D.remove_sidebar_bookmark("/home/u/OneDrive") is False
    for target in paths.gtk_bookmarks():
        assert target.read_text().strip() == ""


def test_remove_sidebar_bookmark_leaves_other_entries():
    for target in paths.gtk_bookmarks():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("file:///home/u/Projects Code\n", encoding="utf-8")
    D.add_sidebar_bookmark("/home/u/OneDrive")

    D.remove_sidebar_bookmark("/home/u/OneDrive")

    for target in paths.gtk_bookmarks():
        assert target.read_text().splitlines() == ["file:///home/u/Projects Code"]


def test_sidebar_bookmarks_parses_pairs():
    D.add_sidebar_bookmark("/home/u/od-root")
    entries = D.sidebar_bookmarks(paths.gtk_bookmarks()[0])
    assert entries == [("file:///home/u/od-root", APP_DISPLAY_NAME)]


def test_sidebar_bookmarks_parses_an_unlabelled_line():
    D.add_sidebar_bookmark("/home/u/OneDrive")      # label == basename, omitted
    entries = D.sidebar_bookmarks(paths.gtk_bookmarks()[0])
    assert entries == [("file:///home/u/OneDrive", "")]


def test_sidebar_bookmarks_of_a_missing_file_is_empty(tmp_path):
    assert D.sidebar_bookmarks(tmp_path / "nope") == []


# ═════════════════════════════════════════════════════════════════════════════
# XDG user dirs
# ═════════════════════════════════════════════════════════════════════════════

def test_user_dirs_covers_every_kfm_folder():
    dirs = D.user_dirs()
    assert set(dirs) == set(KfmFolder)
    assert all(p.is_absolute() for p in dirs.values())


def test_user_dirs_defaults_when_the_file_is_missing():
    dirs = D.user_dirs()
    assert dirs[KfmFolder.DOCUMENTS] == Path.home() / "Documents"
    assert dirs[KfmFolder.VIDEOS] == Path.home() / "Videos"


def test_user_dirs_reads_the_file(_isolate_home):
    target = D.user_dirs_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '# a comment\n'
        'XDG_DESKTOP_DIR="$HOME/Escritorio"\n'
        'XDG_DOCUMENTS_DIR="$HOME/OneDrive/Documents"\n'
        'XDG_PICTURES_DIR="/mnt/photos"\n'
        'XDG_DOWNLOAD_DIR="$HOME/Downloads"\n',
        encoding="utf-8")

    dirs = D.user_dirs()

    assert dirs[KfmFolder.DESKTOP] == Path.home() / "Escritorio"
    assert dirs[KfmFolder.DOCUMENTS] == Path.home() / "OneDrive" / "Documents"
    assert dirs[KfmFolder.PICTURES] == Path("/mnt/photos")
    assert dirs[KfmFolder.MUSIC] == Path.home() / "Music"      # not in the file


def test_user_dirs_unescapes_shell_quoting(_isolate_home):
    target = D.user_dirs_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('XDG_MUSIC_DIR="$HOME/My\\ Music"\n', encoding="utf-8")
    assert D.user_dirs()[KfmFolder.MUSIC] == Path.home() / "My Music"


def test_user_dir_returns_one():
    assert D.user_dir(KfmFolder.PICTURES) == D.user_dirs()[KfmFolder.PICTURES]


def test_user_dirs_matches_xdg_user_dir_on_the_real_home(monkeypatch):
    """Against the developer's real config, not the isolated one."""
    if not Path("/usr/bin/xdg-user-dir").exists():
        pytest.skip("xdg-user-dir is not installed")
    from tests.conftest import REAL_HOME

    monkeypatch.setenv("HOME", str(REAL_HOME))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    for folder, (_key, _default) in D.USER_DIR_KEYS.items():
        expected = subprocess.run(
            ["xdg-user-dir", folder.name], capture_output=True, text=True,
            timeout=10, env={**os.environ, "HOME": str(REAL_HOME)},
        ).stdout.strip()
        assert str(D.user_dirs()[folder]) == expected, folder


# ═════════════════════════════════════════════════════════════════════════════
# Icons and the Nautilus extension
# ═════════════════════════════════════════════════════════════════════════════

def test_install_icons_writes_the_theme_and_refreshes_the_cache(monkeypatch):
    ran: list[list[str]] = []
    monkeypatch.setattr(D, "_run_tool", lambda argv: ran.append(list(argv)) or True)

    assert D.install_icons() is True

    assert ran and ran[0][0] in D.ICON_CACHE_TOOLS
    assert ran[0][1:3] == ["-f", "-t"]
    assert ran[0][3] == str(paths.icon_theme_dir())
    assert (paths.icon_app_dir() / f"{APP_ID}.svg").is_file()
    assert any(paths.icon_emblem_dir().glob("emblem-*.svg"))


def test_update_icon_cache_falls_back_to_the_gtk3_tool(monkeypatch):
    tried: list[str] = []

    def run_tool(argv):
        tried.append(argv[0])
        return argv[0] == "gtk-update-icon-cache"

    monkeypatch.setattr(D, "_run_tool", run_tool)
    assert D.update_icon_cache() is True
    assert tried == list(D.ICON_CACHE_TOOLS)


def test_install_nautilus_extension_copies_the_source(tmp_path):
    source = tmp_path / "ext.py"
    source.write_text("# extension\n", encoding="utf-8")

    assert D.install_nautilus_extension(source) is True

    target = paths.nautilus_ext_file()
    assert target.read_text() == "# extension\n"
    assert D.nautilus_extension_installed() is True
    assert D.install_nautilus_extension(source) is False       # idempotent
    assert D.remove_nautilus_extension() is True
    assert D.remove_nautilus_extension() is False


def test_install_nautilus_extension_with_nothing_to_install(monkeypatch):
    monkeypatch.setattr(D, "nautilus_extension_source", lambda: None)
    assert D.install_nautilus_extension() is False
    assert D.nautilus_extension_installed() is False


def test_nautilus_extension_target_is_the_per_user_directory():
    """The loader dlopens the SYSTEM libpython, so this is not a venv path."""
    target = paths.nautilus_ext_file()
    assert target.parent == paths.nautilus_ext_dir()
    assert target.parent.name == "extensions"
    assert target.parent.parent.name == "nautilus-python"


# ═════════════════════════════════════════════════════════════════════════════
# Live — the real session bus
# ═════════════════════════════════════════════════════════════════════════════

def _session_bus_available() -> bool:
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


@pytest.mark.skipif(not _session_bus_available(), reason="no session bus")
def test_live_file_manager1_is_activatable(qapp):
    """The service is D-Bus activated, so it is listable even when not running."""
    from onedriveui.platform.dbus import Bus

    names = Bus.session().call_or_none(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "ListActivatableNames", reply="(as)")
    if names is None:
        pytest.skip("could not list activatable names")
    assert D.FM1_NAME in names[0]


@pytest.mark.skipif(not Path("/usr/bin/gtk4-update-icon-cache").exists(),
                    reason="gtk4-update-icon-cache is not installed")
def test_live_icon_cache_rebuild(_isolate_home):
    """The real tool accepts the tree `ui.icons` writes."""
    from onedriveui.ui import icons

    icons.install_theme_icons()
    assert D.update_icon_cache() is True
    assert (paths.icon_theme_dir() / "icon-theme.cache").is_file()
