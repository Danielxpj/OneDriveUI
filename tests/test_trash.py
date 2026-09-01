"""Tests for `onedriveui.platform.trash` — invariant **I10**.

The specification claims are checked against `gio trash` itself wherever the
tool is installed, because "I read the spec and implemented it" is exactly the
kind of assertion that is wrong in a way unit tests written from the same
misreading cannot catch. So:

* `test_live_gio_reads_our_trashinfo` trashes with **our** code and restores
  with **`gio trash --list`**, and
* `test_live_our_encoding_matches_gio` trashes with **`gio`** and asserts our
  `encode_path()` reproduces its `Path=` byte for byte.

The two safety behaviours get the most attention: nothing inside a
`fuse.rclone` mount may be trashed locally (it would either upload the deleted
file or hydrate it), and the info file is always written first, with `O_EXCL`,
so `files/` can never hold an entry that no trash browser can see.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.errors import OneDriveUIError, SafetyRefusal
from onedriveui.platform import trash as T

GIO = shutil.which("gio")


@pytest.fixture
def work(_isolate_home) -> Path:
    """A directory on the same filesystem as the isolated home trash."""
    target = Path(os.environ["HOME"]) / "work"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _file(work: Path, name: str, content: str = "hello") -> Path:
    target = work / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ═════════════════════════════════════════════════════════════════════════════
# Layout
# ═════════════════════════════════════════════════════════════════════════════

def test_home_trash_is_created_with_files_and_info(_isolate_home):
    root = T.home_trash()
    assert root == Path(os.environ["XDG_DATA_HOME"]) / "Trash"
    assert (root / T.FILES_DIR).is_dir()
    assert (root / T.INFO_DIR).is_dir()


def test_home_trash_is_not_our_data_dir(_isolate_home):
    """The trash is shared with every application; data_dir() is ours alone."""
    assert T.home_trash() != paths.data_dir()
    assert T.home_trash().name == "Trash"


def test_home_trash_is_private(_isolate_home):
    assert (T.home_trash() / T.FILES_DIR).stat().st_mode & 0o077 == 0


# ═════════════════════════════════════════════════════════════════════════════
# .trashinfo — format and escaping
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,encoded", [
    ("/a/plain.txt", "/a/plain.txt"),
    ("/a/with space.txt", "/a/with%20space.txt"),
    ("/a/hash#tag.txt", "/a/hash%23tag.txt"),
    ("/a/per%cent.txt", "/a/per%25cent.txt"),
    ("/a/back\\slash.txt", "/a/back%5Cslash.txt"),
    ("/a/bra[ck]et.txt", "/a/bra%5Bck%5Det.txt"),
])
def test_encode_path(raw, encoded):
    assert T.encode_path(raw) == encoded


def test_encode_path_leaves_separators_alone():
    assert T.encode_path("/a/b/c") == "/a/b/c"


@pytest.mark.parametrize("raw", [
    "/a/plain.txt", "/a/with space.txt", "/a/hash#tag.txt", "/a/per%cent.txt",
    "/a/ünïcødé — em dash.txt", "/a/日本語.txt", "/a/it's (1).txt",
])
def test_encode_decode_round_trip(raw):
    assert T.decode_path(T.encode_path(raw)) == raw


def test_info_text_shape():
    when = _dt.datetime(2026, 8, 31, 9, 5, 4)
    text = T.info_text("/home/u/a b.txt", when)
    assert text == ("[Trash Info]\n"
                    "Path=/home/u/a%20b.txt\n"
                    "DeletionDate=2026-08-31T09:05:04\n")


def test_deletion_date_carries_no_timezone():
    """The specification's format has no zone; gio writes local time."""
    text = T.info_text("/x", _dt.datetime(2026, 1, 2, 3, 4, 5))
    stamp = text.splitlines()[2].removeprefix("DeletionDate=")
    assert stamp == "2026-01-02T03:04:05"
    assert "+" not in stamp and "Z" not in stamp


def test_parse_info_round_trips():
    when = _dt.datetime(2026, 8, 31, 9, 5, 4)
    original, stamp = T.parse_info(T.info_text("/home/u/a b#c.txt", when))
    assert original == "/home/u/a b#c.txt"
    assert stamp == "2026-08-31T09:05:04"


def test_parse_info_survives_junk():
    assert T.parse_info("") == ("", "")
    assert T.parse_info("not an ini file") == ("", "")
    assert T.parse_info("[Other]\nPath=/x\n") == ("", "")


def test_parse_info_ignores_keys_outside_the_section():
    text = "[Trash Info]\nPath=/a\nDeletionDate=2026-01-01T00:00:00\n[X]\nPath=/b\n"
    assert T.parse_info(text)[0] == "/a"


# ═════════════════════════════════════════════════════════════════════════════
# Trashing
# ═════════════════════════════════════════════════════════════════════════════

def test_trash_moves_the_file_and_records_its_origin(work):
    source = _file(work, "report.docx")

    item = T.trash(source)

    assert not source.exists()
    assert item.files_path.read_text() == "hello"
    assert item.original_path == source
    assert item.is_dir is False
    assert item.size == 5


def test_info_filename_is_the_full_basename_plus_suffix(work):
    """`report.docx` -> `report.docx.trashinfo`, NOT `report.trashinfo`."""
    item = T.trash(_file(work, "report.docx"))
    assert item.info_path.name == "report.docx.trashinfo"
    assert item.info_path.is_file()


def test_info_file_is_owner_only(work):
    """A trashed file's original path can be sensitive."""
    item = T.trash(_file(work, "secret.txt"))
    assert (item.info_path.stat().st_mode & 0o077) == 0


@pytest.mark.parametrize("name", [
    "with space.txt", "hash#tag.txt", "per%cent.txt", "ünïcødé.txt",
    "日本語.txt", "it's (1).txt", "semi;colon.txt", "bra[ck]et.txt",
])
def test_awkward_names_round_trip(work, name):
    source = _file(work, name)
    item = T.trash(source)

    original, _stamp = T.parse_info(item.info_path.read_text(encoding="utf-8"))

    assert original == str(source)
    assert T.restore(item) == source
    assert source.read_text() == "hello"


def test_a_collision_gets_a_new_name(work):
    first = T.trash(_file(work, "note.txt", "one"))
    second = T.trash(_file(work, "note.txt", "two"))

    assert first.name == "note.txt"
    assert second.name == "note.2.txt"
    assert second.info_path.name == "note.2.txt.trashinfo"
    assert first.files_path.read_text() == "one"
    assert second.files_path.read_text() == "two"


def test_collisions_keep_the_extension_visible(work):
    for _ in range(4):
        T.trash(_file(work, "photo.jpeg"))
    names = sorted(p.name for p in (T.home_trash() / T.FILES_DIR).iterdir())
    assert names == ["photo.2.jpeg", "photo.3.jpeg", "photo.4.jpeg", "photo.jpeg"]


def test_a_name_with_no_extension_still_gets_a_suffix(work):
    T.trash(_file(work, "Makefile"))
    second = T.trash(_file(work, "Makefile"))
    assert second.name == "Makefile.2"


def test_the_info_file_is_written_before_the_payload_moves(work, monkeypatch):
    """So `files/` can never hold an entry no trash browser can see."""
    seen: list[bool] = []
    real_rename = os.rename

    def spy(src, dst):
        seen.append((T.home_trash() / T.INFO_DIR
                     / (Path(dst).name + T.INFO_SUFFIX)).is_file())
        return real_rename(src, dst)

    monkeypatch.setattr(T.os, "rename", spy)
    T.trash(_file(work, "ordered.txt"))

    assert seen == [True]


def test_a_failed_move_leaves_no_orphan_info_file(work, monkeypatch):
    def boom(_src, _dst):
        raise OSError(13, "denied")

    monkeypatch.setattr(T.os, "rename", boom)
    source = _file(work, "doomed.txt")

    with pytest.raises(OneDriveUIError):
        T.trash(source)

    assert list((T.home_trash() / T.INFO_DIR).iterdir()) == []
    assert source.exists(), "the original must survive a failed trash"


def test_a_crashed_half_trash_does_not_reuse_the_name(work):
    """An info file with no payload must not let a new payload inherit it."""
    T.trash(_file(work, "half.txt"))
    (T.home_trash() / T.FILES_DIR / "half.txt").unlink()   # simulate a crash

    second = T.trash(_file(work, "half.txt"))

    assert second.name == "half.2.txt"


def test_trash_a_directory(work):
    tree = work / "tree" / "deep"
    tree.mkdir(parents=True)
    (tree / "a.bin").write_bytes(b"0" * 1234)

    item = T.trash_tree(work / "tree")

    assert item.is_dir is True
    assert item.size == 1234
    assert (item.files_path / "deep" / "a.bin").read_bytes() == b"0" * 1234
    assert not (work / "tree").exists()


def test_trash_tree_refuses_a_file(work):
    with pytest.raises(NotADirectoryError):
        T.trash_tree(_file(work, "notadir.txt"))


def test_directorysizes_records_a_trashed_directory(work):
    (work / "big").mkdir()
    (work / "big" / "x").write_bytes(b"0" * 99)

    item = T.trash_tree(work / "big")

    line = (T.home_trash() / T.DIRECTORYSIZES).read_text().strip()
    size, _mtime, name = line.split(" ")
    assert int(size) == 99
    assert name == T.encode_path(item.name)


def test_restore_removes_the_directorysizes_line(work):
    (work / "big").mkdir()
    (work / "big" / "x").write_bytes(b"0" * 9)
    item = T.trash_tree(work / "big")

    T.restore(item)

    assert (T.home_trash() / T.DIRECTORYSIZES).read_text().strip() == ""


def test_trash_a_symlink_does_not_follow_it(work):
    target = _file(work, "target.txt")
    link = work / "link.txt"
    link.symlink_to(target)

    item = T.trash(link)

    assert item.files_path.is_symlink()
    assert target.exists(), "the symlink's target must not be trashed"


def test_trash_a_missing_path_raises(work):
    with pytest.raises(FileNotFoundError):
        T.trash(work / "nope.txt")


# ═════════════════════════════════════════════════════════════════════════════
# I10 refusals
# ═════════════════════════════════════════════════════════════════════════════

def test_refuses_a_path_under_a_fuse_mount(monkeypatch, work):
    """Neither available behaviour is acceptable — §13's landmine, or hydration."""
    monkeypatch.setattr(paths, "is_under_fuse_mount", lambda _p: True)
    source = _file(work, "in-the-cloud.txt")

    with pytest.raises(SafetyRefusal) as caught:
        T.trash(source)

    assert caught.value.invariant == T.TRASH_RULE
    assert "sync.trashbin.soft_delete" in str(caught.value)
    assert source.exists()


def test_refuses_to_trash_a_trash_directory(work):
    nested = work / f"{T.USER_TRASH_PREFIX}{os.getuid()}"
    nested.mkdir()
    with pytest.raises(SafetyRefusal) as caught:
        T.trash(nested)
    assert "drain_nested_trash" in str(caught.value)

    admin = work / T.ADMIN_TRASH_NAME
    admin.mkdir()
    with pytest.raises(SafetyRefusal):
        T.trash(admin)


def test_assert_trashable_returns_the_absolute_path(work, monkeypatch):
    source = _file(work, "rel.txt")
    monkeypatch.chdir(work)
    assert T.assert_trashable(Path("rel.txt")) == source


@pytest.mark.skipif(not paths.fuse_rclone_mounts(), reason="no rclone mount here")
def test_live_refuses_the_real_sync_root():
    """Against the actual `~/OneDrive` on this machine."""
    _fs, mountpoint = paths.fuse_rclone_mounts()[0]

    with pytest.raises(SafetyRefusal) as caught:
        T.trash(mountpoint / "anything")

    assert caught.value.invariant == T.TRASH_RULE


# ═════════════════════════════════════════════════════════════════════════════
# Listing and restoring
# ═════════════════════════════════════════════════════════════════════════════

def test_list_trash_reports_what_was_trashed(work):
    T.trash(_file(work, "a.txt"))
    T.trash(_file(work, "b.txt"))

    entries = T.list_trash()

    assert {e.name for e in entries} == {"a.txt", "b.txt"}
    assert all(e.exists for e in entries)


def test_list_trash_ignores_a_payload_with_no_info_file(work):
    """It is unrestorable and invisible to every other browser."""
    T.trash(_file(work, "orphan.txt"))
    (T.home_trash() / T.INFO_DIR / "orphan.txt.trashinfo").unlink()

    assert T.list_trash() == []


def test_list_trash_of_an_empty_trash(_isolate_home):
    assert T.list_trash() == []


def test_find_entry(work):
    T.trash(_file(work, "findme.txt"))
    assert T.find_entry("findme.txt") is not None
    assert T.find_entry("nope.txt") is None


def test_restore_recreates_missing_parents(work):
    source = _file(work, "deep/nested/file.txt")
    item = T.trash(source)
    shutil.rmtree(work / "deep")

    assert T.restore(item) == source

    assert source.read_text() == "hello"
    assert not item.info_path.exists()
    assert not item.files_path.exists()


def test_restore_refuses_to_clobber(work):
    source = _file(work, "conflict.txt", "old")
    item = T.trash(source)
    source.write_text("new", encoding="utf-8")

    with pytest.raises(FileExistsError):
        T.restore(item)

    assert source.read_text() == "new"
    assert item.exists, "the trashed copy must survive a refused restore"


def test_restore_can_overwrite_when_asked(work):
    source = _file(work, "conflict.txt", "old")
    item = T.trash(source)
    source.write_text("new", encoding="utf-8")

    T.restore(item, overwrite=True)

    assert source.read_text() == "old"


def test_restore_a_vanished_payload_raises(work):
    item = T.trash(_file(work, "gone.txt"))
    item.files_path.unlink()

    with pytest.raises(FileNotFoundError):
        T.restore(item)


# ═════════════════════════════════════════════════════════════════════════════
# The nested-trash landmine
# ═════════════════════════════════════════════════════════════════════════════

def _make_nested(root: Path, uid: int | None = None) -> Path:
    """Build a `.Trash-$uid` the way a file-manager delete inside a mount does."""
    nested = root / f"{T.USER_TRASH_PREFIX}{os.getuid() if uid is None else uid}"
    (nested / T.FILES_DIR).mkdir(parents=True)
    (nested / T.INFO_DIR).mkdir(parents=True)
    return nested


def _put(nested: Path, name: str, original: str, content: str = "x") -> None:
    (nested / T.FILES_DIR / name).write_text(content, encoding="utf-8")
    (nested / T.INFO_DIR / (name + T.INFO_SUFFIX)).write_text(
        T.info_text(original, _dt.datetime(2026, 8, 30, 12, 0, 0)), encoding="utf-8")


def test_is_nested_trash_dir():
    assert T.is_nested_trash_dir(Path("/x/.Trash-1000")) is True
    assert T.is_nested_trash_dir(Path("/x/.Trash")) is True
    assert T.is_nested_trash_dir(Path("/x/.Trash-abc")) is False
    assert T.is_nested_trash_dir(Path("/x/Trash")) is False
    assert T.is_nested_trash_dir(Path("/x/.Trashcan")) is False


def test_find_nested_trash_dirs_at_the_top_of_a_root(work):
    nested = _make_nested(work)
    (work / "Documents").mkdir()

    assert T.find_nested_trash_dirs(work) == [nested]


def test_find_nested_trash_dirs_respects_depth(work):
    deep = work / "a" / "b"
    deep.mkdir(parents=True)
    nested = _make_nested(deep)

    assert T.find_nested_trash_dirs(work, depth=1) == []
    assert T.find_nested_trash_dirs(work, depth=3) == [nested]


def test_find_nested_trash_dirs_default_depth_is_one():
    """A recursive walk of the sync root means a round trip per directory."""
    assert T.NESTED_SCAN_DEPTH == 1


def test_nested_trash_entries_lists_without_moving(work):
    nested = _make_nested(work)
    _put(nested, "Escrito", str(work / "Escrito"))

    entries = T.nested_trash_entries(work)

    assert [e.name for e in entries] == ["Escrito"]
    assert (nested / T.FILES_DIR / "Escrito").exists()


def test_drain_moves_entries_into_the_home_trash(work):
    nested = _make_nested(work)
    _put(nested, "Escrito", str(work / "Escrito"), "cloud content")

    moved = T.drain_nested_trash(work)

    assert [m.name for m in moved] == ["Escrito"]
    assert moved[0].trash_dir == T.home_trash()
    assert moved[0].files_path.read_text() == "cloud content"
    assert moved[0].original_path == work / "Escrito"
    assert not nested.exists(), "a drained nested trash is removed"


def test_drain_preserves_the_original_deletion_time(work):
    nested = _make_nested(work)
    _put(nested, "old.txt", str(work / "old.txt"))

    moved = T.drain_nested_trash(work)

    assert moved[0].deleted_at == "2026-08-30T12:00:00"
    _original, stamp = T.parse_info(moved[0].info_path.read_text())
    assert stamp == "2026-08-30T12:00:00"


def test_drain_keeps_the_undo(work):
    """The user deleted it; a drain must not turn that into a permanent loss."""
    nested = _make_nested(work)
    _put(nested, "recoverable.txt", str(work / "recoverable.txt"), "payload")

    moved = T.drain_nested_trash(work)
    restored = T.restore(moved[0])

    assert restored == work / "recoverable.txt"
    assert restored.read_text() == "payload"


def test_drain_dry_run_moves_nothing(work):
    nested = _make_nested(work)
    _put(nested, "a.txt", str(work / "a.txt"))

    reported = T.drain_nested_trash(work, dry_run=True)

    assert [r.name for r in reported] == ["a.txt"]
    assert (nested / T.FILES_DIR / "a.txt").exists()
    assert nested.exists()
    assert T.list_trash() == []


def test_drain_respects_max_bytes(work):
    nested = _make_nested(work)
    _put(nested, "small.txt", str(work / "small.txt"), "x")
    _put(nested, "huge.txt", str(work / "huge.txt"), "y" * 5000)

    moved = T.drain_nested_trash(work, max_bytes=100)

    assert [m.name for m in moved] == ["small.txt"]
    assert (nested / T.FILES_DIR / "huge.txt").exists()
    assert nested.exists(), "a nested trash with entries left must not be removed"


def test_drain_handles_a_collision_in_the_home_trash(work):
    T.trash(_file(work, "note.txt", "local"))
    nested = _make_nested(work)
    _put(nested, "note.txt", str(work / "note.txt"), "cloud")

    moved = T.drain_nested_trash(work)

    assert moved[0].name == "note.2.txt"
    assert moved[0].files_path.read_text() == "cloud"
    assert T.find_entry("note.txt").files_path.read_text() == "local"


def test_drain_of_an_admin_trash_form(work):
    admin = work / T.ADMIN_TRASH_NAME
    per_uid = admin / str(os.getuid())
    (per_uid / T.FILES_DIR).mkdir(parents=True)
    (per_uid / T.INFO_DIR).mkdir(parents=True)
    _put(per_uid, "a.txt", str(work / "a.txt"))

    moved = T.drain_nested_trash(work)

    assert [m.name for m in moved] == ["a.txt"]
    assert not admin.exists()


def test_drain_of_a_clean_root_does_nothing(work):
    assert T.drain_nested_trash(work) == []


def test_drain_drops_an_info_file_whose_payload_vanished(work):
    nested = _make_nested(work)
    _put(nested, "ghost.txt", str(work / "ghost.txt"))
    (nested / T.FILES_DIR / "ghost.txt").unlink()

    assert T.drain_nested_trash(work) == []
    assert not nested.exists()


def test_trash_dir_name_matches_the_mandatory_exclude():
    """`.Trash-1000/` is excluded from the mount argv and from every filter."""
    from onedriveui.constants import MANDATORY_EXCLUDES

    uid_dir = f"{T.USER_TRASH_PREFIX}1000/"
    assert f"- {uid_dir}" in MANDATORY_EXCLUDES


# ═════════════════════════════════════════════════════════════════════════════
# Cross-device
# ═════════════════════════════════════════════════════════════════════════════

def test_mount_trash_creates_the_user_form(tmp_path):
    trash_dir = T.mount_trash(tmp_path)
    assert trash_dir == tmp_path / f"{T.USER_TRASH_PREFIX}{os.getuid()}"
    assert (trash_dir / T.FILES_DIR).is_dir()


def test_mount_trash_uses_a_sticky_admin_trash(tmp_path):
    admin = tmp_path / T.ADMIN_TRASH_NAME
    admin.mkdir()
    admin.chmod(0o1777)

    trash_dir = T.mount_trash(tmp_path)

    assert trash_dir == admin / str(os.getuid())


def test_mount_trash_ignores_a_non_sticky_admin_trash(tmp_path):
    """Without the sticky bit another user could swap our subdirectory."""
    admin = tmp_path / T.ADMIN_TRASH_NAME
    admin.mkdir()
    admin.chmod(0o777)

    assert T.mount_trash(tmp_path) == tmp_path / f"{T.USER_TRASH_PREFIX}{os.getuid()}"


def test_mount_trash_without_create(tmp_path):
    assert T.mount_trash(tmp_path, create=False) is None


def test_cross_device_falls_back_to_a_copy(work, monkeypatch):
    """No usable mount trash: copy into the home trash, then remove."""
    monkeypatch.setattr(T, "_same_device", lambda _a, _b: False)
    monkeypatch.setattr(T, "mount_trash", lambda _top, **_kw: None)
    source = _file(work, "far-away.txt", "payload")

    item = T.trash(source)

    assert item.trash_dir == T.home_trash()
    assert item.files_path.read_text() == "payload"
    assert not source.exists()
    assert item.original_path == source


def test_cross_device_copy_removes_the_original_only_after_copying(work, monkeypatch):
    """An interrupted copy must lose nothing."""
    monkeypatch.setattr(T, "_same_device", lambda _a, _b: False)
    monkeypatch.setattr(T, "mount_trash", lambda _top, **_kw: None)
    monkeypatch.setattr(T.shutil, "copy2",
                        lambda *_a, **_kw: (_ for _ in ()).throw(OSError(5, "io")))
    source = _file(work, "interrupted.txt")

    with pytest.raises(OneDriveUIError):
        T.trash(source)

    assert source.exists()
    assert list((T.home_trash() / T.INFO_DIR).iterdir()) == []


def test_cross_device_uses_the_mount_trash_with_a_relative_path(tmp_path, monkeypatch):
    """The specification records a mount-trash origin relative to the mountpoint."""
    # Only the mount trash counts as "same device"; the home trash does not.
    monkeypatch.setattr(T, "_same_device",
                        lambda _a, dest: T.USER_TRASH_PREFIX in str(dest))
    monkeypatch.setattr(T, "_top_dir", lambda _p: tmp_path)
    source = tmp_path / "sub" / "far.txt"
    source.parent.mkdir(parents=True)
    source.write_text("payload", encoding="utf-8")

    item = T.trash(source)

    assert item.trash_dir == tmp_path / f"{T.USER_TRASH_PREFIX}{os.getuid()}"
    recorded = item.info_path.read_text().splitlines()[1].removeprefix("Path=")
    assert recorded == "sub/far.txt"
    assert not recorded.startswith("/")
    assert item.original_path == source


# ═════════════════════════════════════════════════════════════════════════════
# Live — checked against gio, not against my reading of the spec
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(GIO is None, reason="gio is not installed")
@pytest.mark.parametrize("name", [
    "wp10b probe with space.txt", "wp10b#hash%pct.txt", "wp10b-ünïcødé.txt",
    "wp10b-a~b!c'd(e)f*g+h,i=j:k@l&m?n[o]p.txt",
])
def test_live_our_encoding_matches_gio(work, name, monkeypatch):
    """Trash with gio, then assert our encoder reproduces its `Path=` exactly."""
    monkeypatch.setenv("XDG_DATA_HOME", os.environ["XDG_DATA_HOME"])
    source = _file(work, name)
    T.home_trash()

    result = subprocess.run([GIO, "trash", str(source)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        pytest.skip(f"gio trash declined: {result.stderr.strip()}")

    info = T.home_trash() / T.INFO_DIR / (name + T.INFO_SUFFIX)
    written = next(line for line in info.read_text().splitlines()
                   if line.startswith("Path="))
    assert written == f"Path={T.encode_path(source)}"


@pytest.mark.skipif(GIO is None, reason="gio is not installed")
def test_live_gio_reads_our_trashinfo(monkeypatch):
    """Trash with our code, list it with gio, restore it with our code.

    `gio trash --list` goes through the **gvfsd** daemon, which reads the
    session's own `XDG_DATA_HOME` and ignores this process's isolated one — so
    proving interop requires the real home trash. The file is created in the
    real home under an unmistakable name and restored in a `finally`, and the
    assertions are what prove the restore worked.
    """
    from tests.conftest import REAL_HOME

    monkeypatch.setenv("HOME", str(REAL_HOME))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    source = REAL_HOME / "onedriveui-wp10b-trash probe#1.txt"
    source.write_text("interop probe", encoding="utf-8")

    item = None
    try:
        item = T.trash(source)
        assert not source.exists()

        result = subprocess.run([GIO, "trash", "--list"], capture_output=True,
                                text=True, timeout=30)
        if result.returncode != 0:
            pytest.skip(f"gio trash --list declined: {result.stderr.strip()}")

        # gio prints "trash:///<urlencoded name>\t<original path>".
        assert str(source) in result.stdout, result.stdout
        row = next(line for line in result.stdout.splitlines()
                   if line.endswith(f"\t{source}"))
        assert urllib.parse.unquote(row.split("\t")[0]) == f"trash:///{item.name}"
    finally:
        if item is not None and item.exists:
            T.restore(item, overwrite=True)
        source.unlink(missing_ok=True)

    assert not item.files_path.exists()
    assert not item.info_path.exists()
