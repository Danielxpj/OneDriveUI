"""WP-04 — `onedriveui/rc/vfs.py`.

`evict()` is the one function in this application that can destroy user data, so
it carries the heaviest test weight in the repo. Three properties are asserted
independently, because each has its own failure mode:

  * **I3** — a `Dirty:true` or queued item is refused, and the refusal is proved
    to have touched **no file**, by mtime and by inode, not merely by "the file
    still exists".
  * **I5** — the meta sidecar is unlinked *strictly before* the data file. The
    order is recorded by substituting `vfs._unlink`, and the crash it protects
    against is then simulated: killing between the two must leave a data file
    with no metadata, which `classify()` reads as ONLINE_ONLY.
  * **path containment** — `rel_path` reaches `evict()` from a context menu and
    an IPC socket. `../../..` must never reach `unlink()`.

The `local_extents()` numbers are not invented. A real `rclone mount
--vfs-cache-mode full` was run on a throwaway `local` remote on port 17840, three
4 KiB reads were issued at offsets 0, 4 096 000 and 32 768 000 of a 50 000 000-
byte object, and rclone's own sidecar then held::

    "Rs": [{"Pos":0,"Size":65536},{"Pos":4096000,"Size":65536},
           {"Pos":32768000,"Size":65536}]

while SEEK_DATA/SEEK_HOLE on the sparse file returned
`[(0, 65536), (4096000, 65536), (32768000, 65536)]` and `st_blocks*512` was
196 608. Those figures are reproduced as `REAL_*` below.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from onedriveui.errors import DaemonUnavailable, RcError, SafetyRefusal
from onedriveui.models import DiskCacheInfo, FileState, QueueItem
from onedriveui.rc import vfs as vfs_mod
from onedriveui.rc.vfs import (
    FORCE_UPLOAD_EXPIRY,
    QUEUE_RACE_MESSAGE,
    RECURSIVE_TRUE,
    bytes_local,
    classify,
    data_path,
    defer_uploads,
    disk_cache_info,
    entry_for,
    evict,
    evict_tree,
    force_upload_now,
    forget,
    local_extents,
    meta_path,
    meta_tree_for,
    orphaned_cache_trees,
    queue,
    ranges_of,
    read_sidecar,
    refresh,
    scan,
    set_poll_interval,
)
from tests.fakes import fake_rc as fake_rc_mod
from tests.fakes.fake_fs import FS_NAME, ORPHAN_FS_NAME, FakeEntry, write_sparse

# ── the measured reality this module is written against ──────────────────────
#: A 50 000 000-byte object after three 4 KiB reads through a real rclone mount.
REAL_SIZE = 50_000_000
REAL_RS = ({"Pos": 0, "Size": 65_536},
           {"Pos": 4_096_000, "Size": 65_536},
           {"Pos": 32_768_000, "Size": 65_536})
REAL_EXTENTS = [(0, 65_536), (4_096_000, 65_536), (32_768_000, 65_536)]
REAL_PHYSICAL_BYTES = 196_608

#: The two OneDrive cache trees this machine genuinely carries side by side.
LIVE_TREE = "onedrive{MxOuf}"
ORPHAN_TREE = "onedrive"


@pytest.fixture
def rc(fake_rc, fake_fs, monkeypatch):
    """A fake daemon describing the fake cache tree, wired into `vfs`."""
    fake_fs.apply_to(fake_rc)
    monkeypatch.setattr(vfs_mod, "call_blocking", fake_rc_mod.call_blocking)
    return fake_rc


def _stat_snapshot(*paths: Path) -> dict[Path, tuple]:
    """(st_mtime_ns, st_ctime_ns, st_ino, st_size) for every existing path."""
    out: dict[Path, tuple] = {}
    for path in paths:
        st = path.stat()
        out[path] = (st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_size)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# I4 — the cache location comes from vfs/stats
# ═════════════════════════════════════════════════════════════════════════════

class TestDiskCacheInfo:

    def test_reads_both_paths_from_vfs_stats(self, rc, fake_fs):
        info = disk_cache_info(rc.endpoint)
        assert info.path == str(fake_fs.data_dir)
        assert info.path_meta == str(fake_fs.meta_dir)
        assert rc.count("vfs/stats") == 1

    def test_paths_carry_the_hash_suffix_a_derivation_would_miss(self, rc, fake_fs):
        """I4's whole reason: `~/.cache/rclone/vfs/onedrive` is NOT the answer."""
        info = disk_cache_info(rc.endpoint)
        assert Path(info.path).name == FS_NAME == LIVE_TREE
        assert Path(info.path).name != ORPHAN_FS_NAME
        hand_derived = Path(info.path).parent / "onedrive"
        assert Path(info.path) != hand_derived

    def test_carries_the_counters_the_tray_reads(self, rc, fake_fs):
        rc.disk_cache.update(uploadsQueued=3, uploadsInProgress=1,
                             erroredFiles=2, outOfSpace=True, hashType=4096)
        info = disk_cache_info(rc.endpoint)
        assert (info.uploads_queued, info.uploads_in_progress) == (3, 1)
        assert info.errored_files == 2
        assert info.out_of_space is True
        assert info.hash_type == 4096
        assert info.files == fake_fs.file_count

    def test_refuses_when_the_vfs_has_no_disk_cache(self, rc):
        """`--vfs-cache-mode off` has no diskCache block at all."""
        rc.set("vfs/stats", {"fs": "onedrive:", "inUse": 1})
        with pytest.raises(SafetyRefusal) as caught:
            disk_cache_info(rc.endpoint)
        assert caught.value.invariant == "I4"

    def test_refuses_a_half_empty_disk_cache(self, rc):
        rc.set("vfs/stats", {"diskCache": {"path": "/x", "pathMeta": ""}})
        with pytest.raises(SafetyRefusal) as caught:
            disk_cache_info(rc.endpoint)
        assert caught.value.invariant == "I4"

    def test_propagates_a_dead_daemon(self, rc):
        rc.stop()
        with pytest.raises(DaemonUnavailable):
            disk_cache_info(rc.endpoint)


# ═════════════════════════════════════════════════════════════════════════════
# classify — the six fixture shapes
# ═════════════════════════════════════════════════════════════════════════════

class TestClassify:

    @pytest.mark.parametrize("shape", [
        "no_sidecar", "rs_null", "rs_empty", "full_range", "two_ranges", "dirty",
    ])
    def test_every_fixture_shape(self, fake_fs, shape):
        entry = fake_fs.entry(shape)
        assert classify(fake_fs.sidecar(entry.rel_path)) is entry.state

    def test_no_sidecar_is_online_only(self):
        assert classify({}) is FileState.ONLINE_ONLY

    def test_rs_json_null_is_online_only(self):
        assert classify({"Size": 10, "Rs": None}) is FileState.ONLINE_ONLY

    def test_rs_empty_list_is_online_only(self):
        assert classify({"Size": 10, "Rs": []}) is FileState.ONLINE_ONLY

    def test_one_full_range_is_local(self):
        assert classify({"Size": 10, "Rs": [{"Pos": 0, "Size": 10}]}) is FileState.LOCAL

    def test_two_partial_ranges_are_partial(self):
        meta = {"Size": REAL_SIZE, "Rs": list(REAL_RS)}
        assert classify(meta) is FileState.PARTIAL

    def test_dirty_with_an_empty_fingerprint_is_dirty(self):
        meta = {"Size": 2_000_000, "Fingerprint": "",
                "Rs": [{"Pos": 0, "Size": 2_000_000}], "Dirty": True}
        assert classify(meta) is FileState.DIRTY

    def test_dirty_outranks_a_full_range(self):
        """A fully cached but un-uploaded file is DIRTY, never LOCAL: it is the
        only copy in existence and eviction must refuse it (I3)."""
        meta = {"Size": 8, "Rs": [{"Pos": 0, "Size": 8}], "Dirty": True}
        assert classify(meta) is FileState.DIRTY
        assert classify(meta, pinned=True) is FileState.DIRTY

    def test_pinned_only_upgrades_a_local_file(self):
        full = {"Size": 8, "Rs": [{"Pos": 0, "Size": 8}]}
        part = {"Size": 8, "Rs": [{"Pos": 0, "Size": 4}]}
        assert classify(full, pinned=True) is FileState.PINNED
        assert classify(part, pinned=True) is FileState.PARTIAL
        assert classify({}, pinned=True) is FileState.ONLINE_ONLY

    def test_zero_byte_file_is_local(self):
        """There is nothing to download; a permanent cloud badge would be a lie."""
        assert classify({"Size": 0, "Rs": [{"Pos": 0, "Size": 0}]}) is FileState.LOCAL

    def test_a_single_range_covering_the_whole_object_is_local(self):
        """rclone can report a range one block larger than Size."""
        assert classify({"Size": 100, "Rs": [{"Pos": 0, "Size": 4096}]}) is FileState.LOCAL

    def test_a_range_not_starting_at_zero_is_partial(self):
        meta = {"Size": 100, "Rs": [{"Pos": 10, "Size": 90}]}
        assert classify(meta) is FileState.PARTIAL

    def test_a_torn_sidecar_degrades_rather_than_raising(self):
        assert classify({"Size": "?", "Rs": [{"Pos": None, "Size": "x"}, 7]}) \
            is FileState.ONLINE_ONLY

    def test_never_invents_syncing_error_or_excluded(self):
        """Those three come from vfs/queue, the issues table and the filters
        file — none of which a sidecar knows anything about."""
        produced = {
            classify(m) for m in (
                {}, {"Size": 1, "Rs": None}, {"Size": 1, "Rs": []},
                {"Size": 1, "Rs": [{"Pos": 0, "Size": 1}]},
                {"Size": 4, "Rs": [{"Pos": 0, "Size": 1}]},
                {"Size": 1, "Rs": [{"Pos": 0, "Size": 1}], "Dirty": True})
        }
        assert produced.isdisjoint(
            {FileState.SYNCING, FileState.ERROR, FileState.EXCLUDED,
             FileState.UNKNOWN})


class TestRangesAndBytes:

    def test_ranges_of_the_real_capture(self):
        assert ranges_of({"Rs": list(REAL_RS)}) == REAL_EXTENTS

    def test_ranges_of_null_and_empty(self):
        assert ranges_of({"Rs": None}) == []
        assert ranges_of({"Rs": []}) == []
        assert ranges_of({}) == []

    def test_ranges_drops_malformed_entries(self):
        raw = {"Rs": [{"Pos": 0, "Size": 4}, "junk", {"Pos": 1}, {"Size": -3}]}
        assert ranges_of(raw) == [(0, 4)]

    def test_bytes_local_sums_the_real_capture(self):
        assert bytes_local({"Size": REAL_SIZE, "Rs": list(REAL_RS)}) == 196_608

    def test_bytes_local_never_exceeds_size(self):
        """A stale sidecar must not make a progress bar read 400 %."""
        meta = {"Size": 10, "Rs": [{"Pos": 0, "Size": 40}]}
        assert bytes_local(meta) == 10


class TestEntryFor:

    def test_projects_the_whole_sidecar(self, fake_fs):
        fixture = fake_fs.entry("two_ranges")
        entry = entry_for(fixture.rel_path, fake_fs.sidecar(fixture.rel_path))
        assert entry.rel_path == fixture.rel_path
        assert entry.size == fixture.size
        assert entry.bytes_local == fixture.bytes_local
        assert entry.state is FileState.PARTIAL
        assert entry.dirty is False
        assert entry.atime and entry.mtime
        assert entry.fingerprint

    def test_dirty_entry_has_an_empty_fingerprint(self, fake_fs):
        fixture = fake_fs.entry("dirty")
        entry = entry_for(fixture.rel_path, fake_fs.sidecar(fixture.rel_path))
        assert entry.dirty is True
        assert entry.fingerprint == ""
        assert entry.state is FileState.DIRTY


# ═════════════════════════════════════════════════════════════════════════════
# scan
# ═════════════════════════════════════════════════════════════════════════════

class TestScan:

    def test_yields_one_entry_per_sidecar(self, fake_fs):
        info = fake_fs.disk_cache_info()
        found = {e.rel_path: e for e in scan(info, 7)}
        expected = {e.rel_path for e in fake_fs.entries.values() if e.has_sidecar}
        assert set(found) == expected

    def test_walks_pathmeta_not_the_data_tree(self, fake_fs):
        """A data file with no sidecar is uncached by definition and must not
        appear — that is exactly the post-crash state I5 engineers for."""
        write_sparse(fake_fs.data_path("Documents/ghost.bin"), 4096, ((0, 4096),))
        info = fake_fs.disk_cache_info()
        assert "Documents/ghost.bin" not in {e.rel_path for e in scan(info, 1)}

    def test_states_match_the_fixture_answers(self, fake_fs):
        info = fake_fs.disk_cache_info()
        for entry in scan(info, 1):
            assert entry.state is fake_fs.entry(entry.rel_path).state

    def test_non_ascii_paths_survive_byte_for_byte(self, fake_fs):
        info = fake_fs.disk_cache_info()
        assert "Imágenes/partial-two-ranges.bin" in {e.rel_path for e in scan(info, 1)}

    def test_is_a_generator_not_a_list(self, fake_fs):
        result = scan(fake_fs.disk_cache_info(), 1)
        assert hasattr(result, "__next__")
        assert next(result).rel_path

    def test_progress_reports_the_generation_it_was_given(self, fake_fs):
        seen: list[tuple[int, int]] = []
        list(scan(fake_fs.disk_cache_info(), 42,
                  lambda count, gen: seen.append((count, gen))))
        assert seen
        assert {gen for _n, gen in seen} == {42}
        assert seen[-1][0] == fake_fs.file_count

    def test_cancel_ends_the_walk_cleanly(self, fake_fs):
        produced = list(scan(fake_fs.disk_cache_info(), 1, cancel=lambda: True))
        assert produced == []

    def test_pinned_paths_come_back_pinned(self, fake_fs):
        target = fake_fs.entry("full_range").rel_path
        states = {e.rel_path: e.state
                  for e in scan(fake_fs.disk_cache_info(), 1, pinned=[target])}
        assert states[target] is FileState.PINNED

    def test_skips_a_torn_sidecar_instead_of_flapping(self, fake_fs):
        """rclone rewrites sidecars in place; a concurrent scan WILL see one
        mid-write. Skipping keeps the previous row instead of blanking it."""
        victim = fake_fs.entry("full_range").rel_path
        fake_fs.meta_path(victim).write_text('{"ModTime": "2026', encoding="utf-8")
        assert victim not in {e.rel_path for e in scan(fake_fs.disk_cache_info(), 1)}

    def test_missing_meta_root_is_empty_not_an_error(self, tmp_path):
        info = DiskCacheInfo(path=str(tmp_path / "d"), path_meta=str(tmp_path / "m"))
        calls: list[tuple[int, int]] = []
        assert list(scan(info, 3, lambda n, g: calls.append((n, g)))) == []
        assert calls == [(0, 3)]

    def test_progress_fires_on_a_large_tree(self, fake_fs):
        for index in range(450):
            fake_fs.add_entry(FakeEntry(
                rel_path=f"bulk/f{index:04d}.bin", shape="full_range", size=16,
                state=FileState.LOCAL, rs=((0, 16),), ranges=((0, 16),)))
        seen: list[int] = []
        rows = list(scan(fake_fs.disk_cache_info(), 9, lambda n, _g: seen.append(n)))
        assert len(rows) == fake_fs.file_count
        assert 200 in seen and 400 in seen
        assert seen[-1] == len(rows)


# ═════════════════════════════════════════════════════════════════════════════
# local_extents — byte-identical to Rs
# ═════════════════════════════════════════════════════════════════════════════

class TestLocalExtents:

    def test_byte_identical_to_the_sidecar_rs(self, fake_fs):
        """The acceptance bullet, on the fixture's own two-range entry."""
        entry = fake_fs.entry("two_ranges")
        meta = fake_fs.sidecar(entry.rel_path)
        assert local_extents(fake_fs.data_path(entry.rel_path)) == ranges_of(meta)

    def test_byte_identical_on_the_real_measured_capture(self, tmp_path):
        """The exact numbers a real rclone mount produced on this machine."""
        data = tmp_path / "vfs" / FS_NAME / "huge.bin"
        write_sparse(data, REAL_SIZE, REAL_EXTENTS)
        meta = tmp_path / "vfsMeta" / FS_NAME / "huge.bin"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"Size": REAL_SIZE, "Rs": list(REAL_RS),
                                    "Dirty": False}), encoding="utf-8")
        assert local_extents(data) == REAL_EXTENTS
        assert local_extents(data) == ranges_of(read_sidecar(meta))
        assert sum(n for _p, n in local_extents(data)) == REAL_PHYSICAL_BYTES

    def test_full_file_is_a_single_range(self, fake_fs):
        entry = fake_fs.entry("full_range")
        assert local_extents(fake_fs.data_path(entry.rel_path)) == [(0, entry.size)]

    def test_all_hole_file_has_no_extents(self, fake_fs):
        entry = fake_fs.entry("rs_null")
        assert local_extents(fake_fs.data_path(entry.rel_path)) == []

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert local_extents(tmp_path / "nope.bin") == []

    def test_st_size_would_lie_but_extents_do_not(self, fake_fs):
        """The cache file is preallocated to the FULL remote size on first open,
        so `ls -l` shows 5 MB for a file holding 647 KiB."""
        entry = fake_fs.entry("two_ranges")
        data = fake_fs.data_path(entry.rel_path)
        assert data.stat().st_size == entry.size
        assert sum(n for _p, n in local_extents(data)) < entry.size

    def test_einval_falls_back_to_the_sidecar(self, fake_fs, monkeypatch):
        """FAT and exFAT implement neither sparse files nor SEEK_DATA."""
        entry = fake_fs.entry("two_ranges")
        real_lseek = os.lseek

        def refuse(fd, pos, whence):
            if whence in (os.SEEK_DATA, os.SEEK_HOLE):
                raise OSError(errno.EINVAL, "Invalid argument")
            return real_lseek(fd, pos, whence)

        monkeypatch.setattr(vfs_mod.os, "lseek", refuse)
        got = local_extents(fake_fs.data_path(entry.rel_path),
                            sidecar=fake_fs.meta_path(entry.rel_path))
        assert got == ranges_of(fake_fs.sidecar(entry.rel_path))

    def test_einval_fallback_derives_the_sidecar_when_not_given(
            self, fake_fs, monkeypatch):
        entry = fake_fs.entry("two_ranges")
        real_lseek = os.lseek

        def refuse(fd, pos, whence):
            if whence in (os.SEEK_DATA, os.SEEK_HOLE):
                raise OSError(errno.EINVAL, "Invalid argument")
            return real_lseek(fd, pos, whence)

        monkeypatch.setattr(vfs_mod.os, "lseek", refuse)
        got = local_extents(fake_fs.data_path(entry.rel_path))
        assert got == ranges_of(fake_fs.sidecar(entry.rel_path))

    def test_an_unexpected_oserror_still_raises(self, fake_fs, monkeypatch):
        entry = fake_fs.entry("two_ranges")

        def boom(fd, pos, whence):
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(vfs_mod.os, "lseek", boom)
        with pytest.raises(OSError):
            local_extents(fake_fs.data_path(entry.rel_path))


# ═════════════════════════════════════════════════════════════════════════════
# evict — THE function
# ═════════════════════════════════════════════════════════════════════════════

class TestEvictRefusals:

    def test_dirty_item_raises_and_touches_no_file(self, fake_fs):
        """The acceptance bullet, asserted by mtime AND inode AND size."""
        entry = fake_fs.entry("dirty")
        info = fake_fs.disk_cache_info()
        data = fake_fs.data_path(entry.rel_path)
        meta = fake_fs.meta_path(entry.rel_path)
        before = _stat_snapshot(data, meta)

        with pytest.raises(SafetyRefusal) as caught:
            evict(info, entry.rel_path, set())

        assert caught.value.invariant == "I3"
        assert "Dirty" in str(caught.value)
        assert data.exists() and meta.exists()
        assert _stat_snapshot(data, meta) == before

    def test_queued_item_raises_and_touches_no_file(self, fake_fs):
        entry = fake_fs.entry("full_range")
        info = fake_fs.disk_cache_info()
        data = fake_fs.data_path(entry.rel_path)
        meta = fake_fs.meta_path(entry.rel_path)
        before = _stat_snapshot(data, meta)

        with pytest.raises(SafetyRefusal) as caught:
            evict(info, entry.rel_path, {entry.rel_path})

        assert caught.value.invariant == "I3"
        assert "vfs/queue" in str(caught.value)
        assert _stat_snapshot(data, meta) == before

    def test_queue_name_matches_across_the_two_vocabularies(self, fake_fs):
        """vfs/queue reports a VFS-relative name; the sidecar walk produces the
        same path relative to pathMeta. A leading './' must not defeat I3."""
        entry = fake_fs.entry("full_range")
        with pytest.raises(SafetyRefusal):
            evict(fake_fs.disk_cache_info(), entry.rel_path,
                  {"./" + entry.rel_path})

    def test_refuses_a_path_that_escapes_the_cache_tree(self, fake_fs, tmp_path):
        victim = tmp_path / "precious.txt"
        victim.write_text("do not delete me", encoding="utf-8")
        info = fake_fs.disk_cache_info()
        relative = os.path.relpath(victim, fake_fs.meta_dir)
        with pytest.raises(SafetyRefusal) as caught:
            evict(info, relative, set())
        assert caught.value.invariant == "I5"
        assert victim.exists()

    def test_refuses_a_dotdot_climb(self, fake_fs):
        with pytest.raises(SafetyRefusal):
            evict(fake_fs.disk_cache_info(), "Documents/../../../../etc/hosts", set())

    def test_an_absolute_path_is_contained_never_followed(self, fake_fs):
        """A leading '/' is the `vfs/queue` vocabulary, so it is read as
        VFS-root-anchored — which lands inside the cache — rather than sending
        unlink() to /etc."""
        info = fake_fs.disk_cache_info()
        assert evict(info, "/etc/passwd", set()) == 0
        assert Path("/etc/passwd").exists()
        assert data_path(info, "/etc/passwd").is_relative_to(Path(info.path))

    @pytest.mark.parametrize("hostile", [
        "../../../../etc/passwd",
        "Documents/../../../../../etc/passwd",
        "/etc/passwd",
        "//etc//passwd",
        "./../..",
        "..",
        "a/./../../..",
        "",
        "/",
        "\\..\\..\\etc\\passwd",
    ])
    def test_no_input_can_ever_resolve_outside_the_cache(self, fake_fs, hostile):
        """The property that matters: `evict()` unlinks, and `rel_path` arrives
        from a context menu and an IPC socket."""
        info = fake_fs.disk_cache_info()
        for resolve, root in ((data_path, info.path), (meta_path, info.path_meta)):
            try:
                got = resolve(info, hostile)
            except SafetyRefusal as refusal:
                assert refusal.invariant == "I5"
                continue
            assert got.is_relative_to(Path(root)), hostile

    def test_refuses_a_symlink_out_of_the_tree(self, fake_fs, tmp_path):
        """A symlinked directory inside the cache must not redirect an unlink."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep", encoding="utf-8")
        (fake_fs.meta_dir / "escape").symlink_to(outside)
        (fake_fs.data_dir / "escape").symlink_to(outside)
        with pytest.raises(SafetyRefusal) as caught:
            evict(fake_fs.disk_cache_info(), "escape/precious.txt", set())
        assert caught.value.invariant == "I5"
        assert (outside / "precious.txt").exists()


class TestEvictOrdering:

    def test_meta_is_unlinked_strictly_before_data(self, fake_fs, monkeypatch):
        """Invariant I5, proved by the ORDER of the two unlink calls."""
        entry = fake_fs.entry("full_range")
        info = fake_fs.disk_cache_info()
        order: list[Path] = []
        real_unlink = vfs_mod._unlink

        def spy(path: Path) -> bool:
            order.append(Path(path))
            return real_unlink(path)

        monkeypatch.setattr(vfs_mod, "_unlink", spy)
        evict(info, entry.rel_path, set())

        assert order == [fake_fs.meta_path(entry.rel_path),
                         fake_fs.data_path(entry.rel_path)]
        assert order.index(fake_fs.meta_path(entry.rel_path)) < \
            order.index(fake_fs.data_path(entry.rel_path))

    def test_a_crash_between_the_two_leaves_an_online_only_item(
            self, fake_fs, monkeypatch):
        """The reason for the order: after the meta unlink and before the data
        unlink, rclone (and classify()) must read the item as uncached."""
        entry = fake_fs.entry("full_range")
        info = fake_fs.disk_cache_info()
        real_unlink = vfs_mod._unlink

        def crash_after_meta(path: Path) -> bool:
            if Path(path) == fake_fs.data_path(entry.rel_path):
                raise KeyboardInterrupt("simulated SIGKILL between the unlinks")
            return real_unlink(path)

        monkeypatch.setattr(vfs_mod, "_unlink", crash_after_meta)
        with pytest.raises(KeyboardInterrupt):
            evict(info, entry.rel_path, set())

        assert not fake_fs.meta_path(entry.rel_path).exists()
        assert fake_fs.data_path(entry.rel_path).exists()
        assert classify(read_sidecar(fake_fs.meta_path(entry.rel_path))) \
            is FileState.ONLINE_ONLY

    def test_the_reverse_order_would_be_the_dangerous_one(self, fake_fs):
        """Documenting the failure I5 prevents: metadata claiming ranges whose
        data file is gone still classifies LOCAL, and rclone would serve holes."""
        entry = fake_fs.entry("full_range")
        fake_fs.data_path(entry.rel_path).unlink()
        assert classify(fake_fs.sidecar(entry.rel_path)) is FileState.LOCAL


class TestEvictSuccess:

    def test_removes_both_files_and_reports_the_bytes(self, fake_fs):
        entry = fake_fs.entry("two_ranges")
        info = fake_fs.disk_cache_info()
        physical = fake_fs.data_path(entry.rel_path).stat().st_blocks * 512

        freed = evict(info, entry.rel_path, set())

        assert freed == physical
        assert freed == entry.bytes_local
        assert not fake_fs.data_path(entry.rel_path).exists()
        assert not fake_fs.meta_path(entry.rel_path).exists()

    def test_reports_physical_bytes_not_apparent_size(self, fake_fs):
        """A partial item's cache file is preallocated to its full remote size;
        reporting st_size would claim 5 MB freed for 647 KiB."""
        entry = fake_fs.entry("two_ranges")
        freed = evict(fake_fs.disk_cache_info(), entry.rel_path, set())
        assert freed < entry.size

    def test_evicting_an_absent_item_is_a_no_op(self, fake_fs):
        assert evict(fake_fs.disk_cache_info(), "Documents/never-cached.bin",
                     set()) == 0

    def test_an_evicted_item_classifies_online_only(self, fake_fs):
        entry = fake_fs.entry("full_range")
        evict(fake_fs.disk_cache_info(), entry.rel_path, set())
        assert classify(fake_fs.sidecar(entry.rel_path)) is FileState.ONLINE_ONLY

    def test_a_real_unlink_failure_is_not_swallowed(self, fake_fs, monkeypatch):
        entry = fake_fs.entry("full_range")

        def deny(path):
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(vfs_mod.os, "unlink", deny)
        with pytest.raises(PermissionError):
            evict(fake_fs.disk_cache_info(), entry.rel_path, set())


class TestEvictTree:

    def test_one_dirty_item_refuses_the_whole_folder(self, fake_fs):
        """Two passes: prove everything safe, THEN unlink. A refusal must not
        have deleted the dirty item's clean siblings."""
        info = fake_fs.disk_cache_info()
        clean = fake_fs.add_entry(FakeEntry(
            rel_path="Documents/sibling.bin", shape="full_range", size=64,
            state=FileState.LOCAL, rs=((0, 64),), ranges=((0, 64),)))
        dirty = fake_fs.entry("dirty")
        assert dirty.rel_path.startswith("Documents/")

        before = _stat_snapshot(fake_fs.data_path(clean.rel_path),
                                fake_fs.meta_path(clean.rel_path),
                                fake_fs.data_path(dirty.rel_path),
                                fake_fs.meta_path(dirty.rel_path))
        with pytest.raises(SafetyRefusal) as caught:
            evict_tree(info, "Documents", set())
        assert caught.value.invariant == "I3"
        assert _stat_snapshot(fake_fs.data_path(clean.rel_path),
                              fake_fs.meta_path(clean.rel_path),
                              fake_fs.data_path(dirty.rel_path),
                              fake_fs.meta_path(dirty.rel_path)) == before

    def test_one_queued_item_refuses_the_whole_folder(self, fake_fs):
        info = fake_fs.disk_cache_info()
        fake_fs.evict("Documents/dirty-pending.bin")
        with pytest.raises(SafetyRefusal):
            evict_tree(info, "Documents", {"Documents/local-full.bin"})
        assert fake_fs.data_path("Documents/local-full.bin").exists()

    def test_evicts_a_clean_folder_and_sums_the_bytes(self, fake_fs):
        info = fake_fs.disk_cache_info()
        fake_fs.evict("Documents/dirty-pending.bin")
        freed = evict_tree(info, "Documents", set())
        assert freed > 0
        assert not fake_fs.meta_path("Documents/local-full.bin").exists()
        assert not fake_fs.data_path("Documents/local-full.bin").exists()

    def test_a_single_file_prefix_works(self, fake_fs):
        info = fake_fs.disk_cache_info()
        freed = evict_tree(info, "Documents/local-full.bin", set())
        assert freed > 0
        assert not fake_fs.data_path("Documents/local-full.bin").exists()

    def test_prunes_the_emptied_directories(self, fake_fs):
        info = fake_fs.disk_cache_info()
        evict_tree(info, "Imágenes", set())
        assert not (fake_fs.meta_dir / "Imágenes").exists()
        assert not (fake_fs.data_dir / "Imágenes").exists()
        assert fake_fs.meta_dir.is_dir()
        assert fake_fs.data_dir.is_dir()

    def test_prunes_nested_emptied_directories_too(self, fake_fs):
        fake_fs.add_entry(FakeEntry(
            rel_path="Deep/a/b/c.bin", shape="full_range", size=32,
            state=FileState.LOCAL, rs=((0, 32),), ranges=((0, 32),)))
        evict_tree(fake_fs.disk_cache_info(), "Deep", set())
        assert not (fake_fs.meta_dir / "Deep").exists()
        assert not (fake_fs.data_dir / "Deep").exists()

    def test_reclaims_a_data_file_that_lost_its_sidecar(self, fake_fs):
        """Exactly the state a crash between the two unlinks leaves (I5): rclone
        never serves it, but the disk is still occupied."""
        info = fake_fs.disk_cache_info()
        orphan = fake_fs.data_dir / "Stale" / "half-evicted.bin"
        write_sparse(orphan, 8192, ((0, 8192),))
        assert not (fake_fs.meta_dir / "Stale" / "half-evicted.bin").exists()

        freed = evict_tree(info, "Stale", set())

        assert freed > 0
        assert not orphan.exists()

    def test_a_queued_orphan_data_file_still_refuses(self, fake_fs):
        info = fake_fs.disk_cache_info()
        orphan = fake_fs.data_dir / "Stale" / "pending.bin"
        write_sparse(orphan, 8192, ((0, 8192),))
        with pytest.raises(SafetyRefusal) as caught:
            evict_tree(info, "Stale", {"Stale/pending.bin"})
        assert caught.value.invariant == "I3"
        assert orphan.exists()

    def test_evicting_the_whole_cache_leaves_both_roots_standing(self, fake_fs):
        info = fake_fs.disk_cache_info()
        fake_fs.evict("Documents/dirty-pending.bin")
        evict_tree(info, "", set())
        assert list(scan(info, 1)) == []
        assert fake_fs.meta_dir.is_dir()
        assert fake_fs.data_dir.is_dir()

    def test_refuses_a_prefix_escaping_the_tree(self, fake_fs):
        with pytest.raises(SafetyRefusal):
            evict_tree(fake_fs.disk_cache_info(), "../../..", set())


# ═════════════════════════════════════════════════════════════════════════════
# vfs/queue
# ═════════════════════════════════════════════════════════════════════════════

class TestQueue:

    def test_parses_every_field(self, rc):
        rc.add_queue_item("Documents/newfile.bin", size=2_000_000,
                          expiry=4.996, tries=1, delay=5.0)
        rows = queue(rc.endpoint)
        assert rows == [QueueItem(name="Documents/newfile.bin", id=1,
                                  size=2_000_000, expiry=4.996, tries=1,
                                  delay=5.0, uploading=False)]

    def test_empty_queue(self, rc):
        assert queue(rc.endpoint) == []

    def test_negative_expiry_is_allowed(self, rc):
        rc.add_queue_item("a.bin", expiry=-12.5)
        assert queue(rc.endpoint)[0].expiry == -12.5


class TestForceUploadNow:

    def test_sends_the_large_negative_expiry(self, rc):
        item = rc.add_queue_item("a.bin", size=10)
        force_upload_now(rc.endpoint, item["id"])
        sent = rc.last("vfs/queue-set-expiry")
        assert sent is not None
        assert sent.params == {"id": item["id"], "expiry": FORCE_UPLOAD_EXPIRY}
        assert FORCE_UPLOAD_EXPIRY < 0

    def test_swallows_the_write_back_race(self, rc):
        """`id not found in queue` is a NORMAL ~5 s race, not an error."""
        force_upload_now(rc.endpoint, 999)          # never queued
        assert rc.count("vfs/queue-set-expiry") == 1

    def test_a_real_error_still_raises(self, rc):
        rc.fail("vfs/queue-set-expiry", status=500, message="vfs cache disabled")
        with pytest.raises(RcError):
            force_upload_now(rc.endpoint, 1)


class TestDeferUploads:

    def test_pins_every_queued_item_at_an_absolute_expiry(self, rc):
        rc.add_queue_item("a.bin", size=1)
        rc.add_queue_item("b.bin", size=2)
        assert defer_uploads(rc.endpoint, 3600) == 2
        sent = [c.params for c in rc.calls_to("vfs/queue-set-expiry")]
        assert sent == [{"id": 1, "expiry": 3600.0}, {"id": 2, "expiry": 3600.0}]
        assert all("relative" not in p for p in sent)

    def test_repeating_it_does_not_compound(self, rc):
        """Absolute, not relative: a pause held across ticks stays at N seconds
        rather than sliding into an unreachable future."""
        rc.add_queue_item("a.bin", size=1)
        defer_uploads(rc.endpoint, 600)
        defer_uploads(rc.endpoint, 600)
        assert rc.queue[0]["expiry"] == 600.0

    def test_skips_items_already_uploading(self, rc):
        """Setting the expiry of a started upload has no effect; counting it
        would make the UI claim a pause that did not happen."""
        rc.add_queue_item("busy.bin", size=1, uploading=True)
        rc.add_queue_item("idle.bin", size=1)
        assert defer_uploads(rc.endpoint, 60) == 1
        assert [c.params["id"] for c in rc.calls_to("vfs/queue-set-expiry")] == [2]

    def test_empty_queue_defers_nothing(self, rc):
        assert defer_uploads(rc.endpoint, 60) == 0
        assert rc.count("vfs/queue-set-expiry") == 0

    def test_refuses_a_non_positive_pause(self, rc):
        rc.add_queue_item("a.bin", size=1)
        for bad in (0, -1, -1_000_000_000):
            with pytest.raises(ValueError):
                defer_uploads(rc.endpoint, bad)
        assert rc.count("vfs/queue-set-expiry") == 0

    def test_a_per_item_race_is_skipped_not_fatal(self, rc, monkeypatch):
        rc.add_queue_item("a.bin", size=1)
        rc.add_queue_item("b.bin", size=1)
        real = fake_rc_mod.call_blocking
        seen: list[int] = []

        def flaky(ep, path, params=None, timeout_s=30.0):
            if path == "vfs/queue-set-expiry":
                if (params or {}).get("id") == 1:
                    raise RcError(path, 500, {"error": QUEUE_RACE_MESSAGE})
                seen.append((params or {}).get("id", 0))
            return real(ep, path, params, timeout_s)

        monkeypatch.setattr(vfs_mod, "call_blocking", flaky)
        assert defer_uploads(rc.endpoint, 30) == 1
        assert seen == [2]


# ═════════════════════════════════════════════════════════════════════════════
# Orphaned cache trees — the {HASH} footgun
# ═════════════════════════════════════════════════════════════════════════════

class TestOrphanedCacheTrees:

    def test_finds_the_hash_sibling_this_machine_really_has(self, fake_fs):
        info = fake_fs.disk_cache_info()
        found = orphaned_cache_trees(info)
        assert [p.name for p, _b in found] == [ORPHAN_TREE]
        assert Path(info.path).name == LIVE_TREE

    def test_agrees_with_the_fixtures_own_answer(self, fake_fs):
        assert [p for p, _b in orphaned_cache_trees(fake_fs.disk_cache_info())] \
            == [p for p, _b in fake_fs.orphan_trees()]

    def test_reports_the_reclaimable_bytes(self, fake_fs):
        found = orphaned_cache_trees(fake_fs.disk_cache_info())
        assert found and found[0][1] == 262_144

    def test_never_reports_the_live_tree(self, fake_fs):
        info = fake_fs.disk_cache_info()
        assert Path(info.path) not in [p for p, _b in orphaned_cache_trees(info)]

    def test_ignores_another_remotes_cache_by_default(self, fake_fs):
        """`vfs/local` belongs to a different remote and is not ours to delete."""
        other = fake_fs.cache_dir / "vfs" / "local" / "x.bin"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"x" * 100)
        info = fake_fs.disk_cache_info()
        assert "local" not in [p.name for p, _b in orphaned_cache_trees(info)]
        assert "local" in [p.name for p, _b in
                           orphaned_cache_trees(info, same_remote_only=False)]

    def test_a_remote_subpath_cache_is_not_shredded(self, fake_fs):
        """`onedrive:Documents` caches under vfs/<fs>/Documents. Taking the
        PARENT of that would report every real folder as an orphan."""
        sub = fake_fs.data_dir / "Documents"
        info = DiskCacheInfo(path=str(sub), path_meta=str(fake_fs.meta_dir / "Documents"))
        found = [p.name for p, _b in orphaned_cache_trees(info)]
        assert "Imágenes" not in found
        assert found == [ORPHAN_TREE]

    def test_a_path_outside_a_vfs_tree_proposes_nothing(self, tmp_path):
        info = DiskCacheInfo(path=str(tmp_path), path_meta=str(tmp_path))
        assert orphaned_cache_trees(info) == []

    def test_meta_tree_pairs_with_the_data_tree(self, fake_fs):
        info = fake_fs.disk_cache_info()
        data_tree, _bytes = orphaned_cache_trees(info)[0]
        assert meta_tree_for(info, data_tree) == fake_fs.orphan_meta_dir
        assert meta_tree_for(info, data_tree).is_dir()


# ═════════════════════════════════════════════════════════════════════════════
# refresh / forget / poll-interval
# ═════════════════════════════════════════════════════════════════════════════

class TestRefresh:

    def test_recursive_is_sent_as_the_string_true(self, rc):
        """The one parameter in the whole rc API that rejects a JSON boolean."""
        refresh(rc.endpoint, [], recursive=True)
        sent = rc.last("vfs/refresh").params
        assert sent["recursive"] == RECURSIVE_TRUE
        assert sent["recursive"] == "true"
        assert not isinstance(sent["recursive"], bool)

    def test_a_json_boolean_would_be_rejected(self, rc):
        with pytest.raises(RcError) as caught:
            rc.call_blocking("vfs/refresh", {"recursive": True})
        assert 'must be string' in str(caught.value)

    def test_non_recursive_omits_the_key_entirely(self, rc):
        refresh(rc.endpoint, ["Documents"])
        assert "recursive" not in rc.last("vfs/refresh").params

    def test_no_dirs_refreshes_the_root(self, rc):
        assert refresh(rc.endpoint) == {"result": {"": "OK"}}
        assert rc.last("vfs/refresh").params == {}

    def test_numbers_the_dir_keys_the_way_rclone_expects(self, rc):
        refresh(rc.endpoint, ["a", "b", "c"])
        assert rc.last("vfs/refresh").params == {"dir": "a", "dir2": "b", "dir3": "c"}

    def test_drops_an_empty_dir_rather_than_asking_for_it(self, rc):
        """An explicit "" answers `file does not exist`; omitting it means root."""
        refresh(rc.endpoint, ["", "/", "Documents/"])
        assert rc.last("vfs/refresh").params == {"dir": "Documents"}


class TestForget:

    def test_returns_the_forgotten_names(self, rc):
        assert forget(rc.endpoint, dirs=["Documents"], files=["a.bin"]) \
            == ["Documents", "a.bin"]

    def test_numbers_dir_and_file_keys_separately(self, rc):
        forget(rc.endpoint, dirs=["a", "b"], files=["x", "y"])
        assert rc.last("vfs/forget").params == {
            "dir": "a", "dir2": "b", "file": "x", "file2": "y"}

    def test_provably_frees_no_disk(self, rc, fake_fs):
        """rclone answers a reassuring {"forgotten": [...]} and leaves every
        cache file and every byte exactly where it was. Never rely on it."""
        entry = fake_fs.entry("full_range")
        before_bytes = disk_cache_info(rc.endpoint).bytes_used
        before = _stat_snapshot(fake_fs.data_path(entry.rel_path),
                                fake_fs.meta_path(entry.rel_path))

        assert forget(rc.endpoint, files=[entry.rel_path]) == [entry.rel_path]

        assert _stat_snapshot(fake_fs.data_path(entry.rel_path),
                              fake_fs.meta_path(entry.rel_path)) == before
        assert disk_cache_info(rc.endpoint).bytes_used == before_bytes


class TestSetPollInterval:

    def test_sends_a_duration_string(self, rc):
        body = set_poll_interval(rc.endpoint, 30)
        assert rc.last("vfs/poll-interval").params == {"interval": "30s"}
        assert body["interval"]["seconds"] == 30
        assert body["enabled"] is True

    def test_zero_disables_polling(self, rc):
        assert set_poll_interval(rc.endpoint, 0)["enabled"] is False

    def test_raises_on_a_backend_without_change_notify(self, rc):
        """The local backend answers HTTP 500 `poll-interval is not supported by
        this remote`; gate on Capabilities.change_notify instead of catching."""
        rc.supports_change_notify = False
        with pytest.raises(RcError) as caught:
            set_poll_interval(rc.endpoint, 30)
        assert "not supported by this remote" in str(caught.value)


# ═════════════════════════════════════════════════════════════════════════════
# Path helpers
# ═════════════════════════════════════════════════════════════════════════════

class TestPaths:

    def test_data_and_meta_mirror_exactly(self, fake_fs):
        info = fake_fs.disk_cache_info()
        rel = "Imágenes/partial-two-ranges.bin"
        assert data_path(info, rel) == fake_fs.data_path(rel)
        assert meta_path(info, rel) == fake_fs.meta_path(rel)
        assert data_path(info, rel).name == meta_path(info, rel).name

    def test_hostile_names_survive_byte_for_byte(self, fake_fs):
        """The cache backend encodes only `/` and the names `.`/`..`."""
        info = fake_fs.disk_cache_info()
        rel = "weird:name?with*chars.txt"
        assert data_path(info, rel).name == rel

    def test_leading_and_trailing_separators_are_normalised(self, fake_fs):
        info = fake_fs.disk_cache_info()
        assert data_path(info, "/Documents/a.bin/") == data_path(info, "Documents/a.bin")

    def test_read_sidecar_of_a_missing_file_is_empty(self, tmp_path):
        assert read_sidecar(tmp_path / "gone.json") == {}

    def test_read_sidecar_of_a_torn_file_is_empty(self, tmp_path):
        torn = tmp_path / "torn.json"
        torn.write_text('{"ModTime": "2026-08', encoding="utf-8")
        assert read_sidecar(torn) == {}

    def test_read_sidecar_of_a_non_object_is_empty(self, tmp_path):
        listy = tmp_path / "list.json"
        listy.write_text("[1, 2, 3]", encoding="utf-8")
        assert read_sidecar(listy) == {}


class TestModuleHygiene:

    def test_imports_no_widgets(self):
        """`vfs` runs on the IOPool; a QWidget import there is a threading bug
        waiting to happen (ARCHITECTURE §7.6)."""
        source = Path(vfs_mod.__file__).read_text(encoding="utf-8")
        assert "QtWidgets" not in source
        assert "QtGui" not in source

    def test_never_hand_derives_a_cache_path(self):
        """I4: the fallbacks in `paths` are for a daemon that will not answer,
        and `vfs` is the module that always can ask."""
        source = Path(vfs_mod.__file__).read_text(encoding="utf-8")
        assert "rclone_vfs_dir" not in source
        assert "rclone_vfs_meta_dir" not in source

    def test_every_public_name_exists(self):
        for name in vfs_mod.__all__:
            assert hasattr(vfs_mod, name), name

    def test_no_unlink_is_reachable_without_assert_evict_safe(self):
        """BUILD_PLAN's named risk: "any evict() call not preceded by
        `assert_evict_safe`". Asserted structurally so a future edit that
        reorders the two lines fails here rather than in production."""
        import ast

        tree = ast.parse(Path(vfs_mod.__file__).read_text(encoding="utf-8"))
        functions = {node.name: node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)}

        def call_lines(node, name):
            out = []
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                label = (func.attr if isinstance(func, ast.Attribute)
                         else getattr(func, "id", ""))
                if label == name:
                    out.append(sub.lineno)
            return out

        evict_fn = functions["evict"]
        guard_at = call_lines(evict_fn, "assert_evict_safe")
        unlink_at = call_lines(evict_fn, "_unlink")
        assert guard_at, "evict() does not call assert_evict_safe at all"
        assert unlink_at, "evict() no longer unlinks anything"
        assert max(guard_at) < min(unlink_at), \
            "assert_evict_safe must run BEFORE any unlink (invariant I3)"

        #: And nothing else in the module unlinks a cache file directly.
        unlinking = {name for name, node in functions.items()
                     if call_lines(node, "_unlink")}
        assert unlinking == {"evict"}, unlinking

    def test_evict_tree_proves_the_whole_folder_before_it_unlinks_any_of_it(self):
        """Two passes, not one: a refusal half way through would already have
        deleted the dirty item's siblings."""
        import ast

        tree = ast.parse(Path(vfs_mod.__file__).read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "evict_tree")
        guard_at = [c.lineno for c in ast.walk(node) if isinstance(c, ast.Call)
                    and getattr(c.func, "attr", "") == "assert_evict_safe"]
        evict_at = [c.lineno for c in ast.walk(node) if isinstance(c, ast.Call)
                    and getattr(c.func, "id", "") == "evict"]
        assert guard_at and evict_at
        assert max(guard_at) < min(evict_at)
