"""A real on-disk rclone VFS cache tree, built in a temp directory.

`FakeFs` materialises the two mirrored trees rclone keeps —

    <cache>/vfs/<fsname>/<rel_path>       the DATA, a SPARSE file, mode 0600
    <cache>/vfsMeta/<fsname>/<rel_path>   the SIDECAR, JSON, mode 0644

— with synthetic sidecars covering **all six shapes** `rc/vfs.classify()` must
tell apart (`docs/research/rclone-mount-vfs.md` §3.3):

    1. no sidecar at all                      -> ONLINE_ONLY
    2. "Rs": null                             -> ONLINE_ONLY
    3. "Rs": []                                -> ONLINE_ONLY
    4. "Rs": [{Pos:0, Size:Size}]              -> LOCAL
    5. two partial ranges                      -> PARTIAL
    6. "Dirty": true with an empty Fingerprint -> DIRTY

The data files are **genuinely sparse**: only the byte ranges named in `Rs` are
written, so `SEEK_DATA`/`SEEK_HOLE` extent probing (`rc/vfs.local_extents()`)
can be tested against real holes rather than against a mock.

The tree also carries the hostile material the preflight and cache scanners
must survive: a `{HASH}`-suffixed cache directory beside an orphaned one, a
non-ASCII path, a `.partial` leftover, OneNote files, reserved and
invalid Windows names, and an over-long relative path.

Nothing here touches the user's real `~/.cache/rclone`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from onedriveui.models import DiskCacheInfo, FileState

__all__ = [
    "FakeFs", "FakeEntry", "build_fake_fs", "extents", "write_sparse",
    "SHAPES", "FS_NAME", "ORPHAN_FS_NAME",
]

#: The fs directory name rclone derives when a backend flag differs from
#: rclone.conf: `onedrive:` + `--onedrive-chunk-size 30M` -> `onedrive{MxOuf}`.
FS_NAME = "onedrive{MxOuf}"

#: The cache tree left behind by the previous command line — invariant I4's
#: reason for never hand-deriving a cache path.
ORPHAN_FS_NAME = "onedrive"

#: 4 KiB-aligned, so SEEK_DATA/SEEK_HOLE report the ranges we wrote.
_PARTIAL_SIZE = 5_000_000
_PARTIAL_RANGES: tuple[tuple[int, int], ...] = ((0, 520_192), (4_096_000, 126_976))
_LOCAL_SIZE = 1_048_576
_DIRTY_SIZE = 2_000_000
_ONLINE_SIZE = 3_000_000

#: The six classify() shapes, in the order the contract lists them.
SHAPES: tuple[str, ...] = (
    "no_sidecar", "rs_null", "rs_empty", "full_range", "two_ranges", "dirty",
)

_QUICKXOR = "3a96446a13959c1d5634f71a66e131e4"
_MTIME = "2026-08-30T23:26:05.861069681-04:00"
_ATIME = "2026-08-30T23:30:05.535426864-04:00"


# ─────────────────────────────────────────────────────────────────────────────
# Sparse-file helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_sparse(path: Path, size: int, ranges: Sequence[tuple[int, int]]) -> None:
    """Write only `ranges` into a file of `size` bytes, leaving real holes.

    The file is fsync'd, because a filesystem with delayed allocation can report
    a not-yet-written extent as data and would make an extent test flap.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        for pos, length in ranges:
            if length <= 0:
                continue
            fh.seek(pos)
            fh.write(b"\xa5" * min(length, max(0, size - pos)))
        fh.truncate(size)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, 0o600)


def extents(path: Path | str) -> list[tuple[int, int]]:
    """-> [(offset, length)] of the DATA regions, via SEEK_DATA / SEEK_HOLE.

    This is the synchronous ground truth `rc/vfs.local_extents()` uses; the
    sidecar's `Rs` lags it by several seconds. Raises OSError (EINVAL) on a
    filesystem without sparse support, which is the fallback trigger.
    """
    out: list[tuple[int, int]] = []
    fd = os.open(str(path), os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        pos = 0
        while pos < size:
            try:
                data = os.lseek(fd, pos, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno == 6:          # ENXIO: no data past here
                    break
                raise
            try:
                hole = os.lseek(fd, data, os.SEEK_HOLE)
            except OSError:
                hole = size
            if hole <= data:
                break
            out.append((data, hole - data))
            pos = hole
    finally:
        os.close(fd)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Entries
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FakeEntry:
    """One cache item, with the answer every consumer must produce for it."""
    rel_path: str
    shape: str
    size: int
    state: FileState                       #: what classify() must return
    rs: tuple[tuple[int, int], ...] | None  #: None == JSON null, () == []
    dirty: bool = False
    fingerprint: str = ""
    has_sidecar: bool = True
    has_data: bool = True
    ranges: tuple[tuple[int, int], ...] = ()   #: what was actually written

    @property
    def bytes_local(self) -> int:
        return sum(length for _pos, length in (self.rs or ()))

    def sidecar_json(self) -> dict[str, object]:
        """The exact sidecar rclone writes: tab-indented UTF-8, `Rs` null when
        nothing is cached, `Fingerprint` empty while dirty."""
        return {
            "ModTime": _MTIME,
            "ATime": _ATIME,
            "Size": self.size,
            "Rs": None if self.rs is None else [
                {"Pos": pos, "Size": length} for pos, length in self.rs],
            "Fingerprint": self.fingerprint,
            "Dirty": self.dirty,
        }


def _entries() -> tuple[FakeEntry, ...]:
    full_fp = f"{_LOCAL_SIZE},2026-08-31 03:24:34.131323516 +0000 UTC,{_QUICKXOR}"
    part_fp = f"{_PARTIAL_SIZE},2026-08-31 03:24:34.131323516 +0000 UTC,{_QUICKXOR}"
    return (
        # 1. never opened: no sidecar and no data file exist at all.
        FakeEntry(rel_path="Documents/online-no-sidecar.bin", shape="no_sidecar",
                  size=_ONLINE_SIZE, state=FileState.ONLINE_ONLY, rs=None,
                  has_sidecar=False, has_data=False),
        # 2. opened, nothing cached: Rs is JSON null, the data file is all hole.
        FakeEntry(rel_path="Documents/online-rs-null.bin", shape="rs_null",
                  size=_ONLINE_SIZE, state=FileState.ONLINE_ONLY, rs=None,
                  fingerprint=part_fp),
        # 3. the empty-list spelling of the same thing.
        FakeEntry(rel_path="Documents/online-rs-empty.bin", shape="rs_empty",
                  size=_ONLINE_SIZE, state=FileState.ONLINE_ONLY, rs=(),
                  fingerprint=part_fp),
        # 4. fully cached.
        FakeEntry(rel_path="Documents/local-full.bin", shape="full_range",
                  size=_LOCAL_SIZE, state=FileState.LOCAL,
                  rs=((0, _LOCAL_SIZE),), fingerprint=full_fp,
                  ranges=((0, _LOCAL_SIZE),)),
        # 5. two disjoint ranges — a seek-heavy read, non-ASCII path on purpose.
        FakeEntry(rel_path="Imágenes/partial-two-ranges.bin", shape="two_ranges",
                  size=_PARTIAL_SIZE, state=FileState.PARTIAL,
                  rs=_PARTIAL_RANGES, fingerprint=part_fp, ranges=_PARTIAL_RANGES),
        # 6. local edit not yet uploaded: Dirty with an EMPTY Fingerprint.
        FakeEntry(rel_path="Documents/dirty-pending.bin", shape="dirty",
                  size=_DIRTY_SIZE, state=FileState.DIRTY,
                  rs=((0, _DIRTY_SIZE),), dirty=True, fingerprint="",
                  ranges=((0, _DIRTY_SIZE),)),
    )


#: Names the preflight validator must reject, created for real in the sync root.
HOSTILE_NAMES: tuple[str, ...] = (
    "bad:name?.txt",          # invalid characters
    "CON",                    # reserved device name
    "~$draft.docx",           # Office lock file
    "desktop.ini",            # reserved
    "trailing.",              # trailing period
    "spaced ",                # trailing space
    "_vti_config.txt",        # reserved substring
    "interrupted.bin.partial",  # a leftover from a killed transfer
    "Notebook.one",           # OneNote: cannot be synced at all
    "Notebook.onetoc2",
)


# ─────────────────────────────────────────────────────────────────────────────
# The tree
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class FakeFs:
    """A temp `vfs/` + `vfsMeta/` pair plus a fake sync root.

    Attributes mirror what `vfs/stats.diskCache` reports, so a test can hand
    `disk_cache_info()` straight to `rc/vfs` code, or `apply_to(fake_rc)` to make
    the fake daemon describe this very tree.
    """
    root: Path
    fs_name: str = FS_NAME
    entries: dict[str, FakeEntry] = field(default_factory=dict)

    # ── layout ──────────────────────────────────────────────────────────────
    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def data_dir(self) -> Path:
        return self.cache_dir / "vfs" / self.fs_name

    @property
    def meta_dir(self) -> Path:
        return self.cache_dir / "vfsMeta" / self.fs_name

    @property
    def orphan_data_dir(self) -> Path:
        return self.cache_dir / "vfs" / ORPHAN_FS_NAME

    @property
    def orphan_meta_dir(self) -> Path:
        return self.cache_dir / "vfsMeta" / ORPHAN_FS_NAME

    @property
    def sync_root(self) -> Path:
        """A plain directory standing in for the FUSE mountpoint. It is NOT a
        fuse.rclone mount, so `paths.is_under_fuse_mount()` is False for it —
        which is what lets guard tests exercise both branches."""
        return self.root / "OneDrive"

    # ── paths ───────────────────────────────────────────────────────────────
    def data_path(self, rel_path: str) -> Path:
        """The sparse data file. The mapping is a plain join: the cache encodes
        only `/`, so colons, `?`, `*` and non-ASCII survive byte-for-byte."""
        return self.data_dir / rel_path

    def meta_path(self, rel_path: str) -> Path:
        return self.meta_dir / rel_path

    def local_path(self, rel_path: str) -> Path:
        return self.sync_root / rel_path

    # ── contents ────────────────────────────────────────────────────────────
    def sidecar(self, rel_path: str) -> dict:
        """The parsed sidecar, or `{}` when there is none (which classify()
        must read as ONLINE_ONLY)."""
        path = self.meta_path(rel_path)
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def entry(self, key: str) -> FakeEntry:
        """Look an entry up by rel_path or by shape name."""
        if key in self.entries:
            return self.entries[key]
        for entry in self.entries.values():
            if entry.shape == key:
                return entry
        raise KeyError(f"no fake cache entry {key!r}; have {sorted(self.entries)}")

    def by_shape(self) -> dict[str, FakeEntry]:
        return {e.shape: e for e in self.entries.values()}

    def extents(self, rel_path: str) -> list[tuple[int, int]]:
        """SEEK_DATA/SEEK_HOLE extents of one entry's data file."""
        return extents(self.data_path(rel_path))

    # ── mutation, for eviction and re-pin tests ─────────────────────────────
    def add_entry(self, entry: FakeEntry) -> FakeEntry:
        """Materialise one more entry (sidecar + sparse data)."""
        if entry.has_data:
            write_sparse(self.data_path(entry.rel_path), entry.size, entry.ranges)
        if entry.has_sidecar:
            meta = self.meta_path(entry.rel_path)
            meta.parent.mkdir(parents=True, exist_ok=True)
            #: rclone writes tab-indented UTF-8 with a trailing newline.
            meta.write_text(
                json.dumps(entry.sidecar_json(), indent="\t", ensure_ascii=False) + "\n",
                encoding="utf-8")
            os.chmod(meta, 0o644)
        self.entries[entry.rel_path] = entry
        return entry

    def evict(self, rel_path: str) -> int:
        """Unlink META first, then DATA — invariant I5's order. Returns bytes."""
        freed = 0
        data = self.data_path(rel_path)
        meta = self.meta_path(rel_path)
        if meta.exists():
            meta.unlink()
        if data.exists():
            stat = data.stat()
            freed = getattr(stat, "st_blocks", 0) * 512
            data.unlink()
        self.entries.pop(rel_path, None)
        return freed

    def touch_dirty(self, rel_path: str) -> None:
        """Flip an entry's sidecar to Dirty with an empty Fingerprint."""
        entry = self.entry(rel_path)
        self.add_entry(FakeEntry(
            rel_path=entry.rel_path, shape="dirty", size=entry.size,
            state=FileState.DIRTY, rs=entry.rs, dirty=True, fingerprint="",
            has_sidecar=True, has_data=entry.has_data, ranges=entry.ranges))

    # ── reporting ───────────────────────────────────────────────────────────
    @property
    def bytes_used(self) -> int:
        return sum(e.bytes_local for e in self.entries.values())

    @property
    def file_count(self) -> int:
        return sum(1 for e in self.entries.values() if e.has_sidecar)

    def disk_cache_info(self) -> DiskCacheInfo:
        """What `rc/vfs.disk_cache_info()` must return for this tree."""
        return DiskCacheInfo(
            path=str(self.data_dir), path_meta=str(self.meta_dir),
            bytes_used=self.bytes_used, files=self.file_count,
            uploads_queued=0, uploads_in_progress=0, errored_files=0,
            out_of_space=False, hash_type=4096)

    def vfs_stats_response(self) -> dict:
        """The `vfs/stats` body describing this tree."""
        return {
            "diskCache": {
                "bytesUsed": self.bytes_used, "erroredFiles": 0,
                "files": self.file_count, "hashType": 4096, "outOfSpace": False,
                "path": str(self.data_dir), "pathMeta": str(self.meta_dir),
                "uploadsInProgress": 0, "uploadsQueued": 0,
            },
            "fs": f"{self.fs_name}:",
            "inUse": 1,
            "metadataCache": {"dirs": 3, "files": self.file_count},
            "opt": {"CacheMode": 3, "CacheMaxAge": 3600000000000},
        }

    def apply_to(self, fake_rc) -> None:
        """Point a `FakeRc` at this tree so `vfs/stats` reports it (invariant I4:
        cache paths always come from the daemon, never from a guess)."""
        fake_rc.fs_name = f"{self.fs_name}:"
        fake_rc.disk_cache.update(self.vfs_stats_response()["diskCache"])

    def orphan_trees(self) -> list[tuple[Path, int]]:
        """[(path, bytes)] of cache trees that are NOT the live one — what
        `rc/vfs.orphaned_cache_trees()` must find."""
        out: list[tuple[Path, int]] = []
        vfs_root = self.cache_dir / "vfs"
        for child in sorted(vfs_root.iterdir()) if vfs_root.is_dir() else []:
            if not child.is_dir() or child == self.data_dir:
                continue
            total = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            out.append((child, total))
        return out


def build_fake_fs(root: Path, *, entries: Iterable[FakeEntry] | None = None,
                  with_orphan: bool = True, with_sync_root: bool = True) -> FakeFs:
    """Build the whole tree under `root` and return the handle."""
    fs = FakeFs(root=Path(root))
    fs.data_dir.mkdir(parents=True, exist_ok=True)
    fs.meta_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(fs.data_dir, 0o700)
    os.chmod(fs.meta_dir, 0o700)

    for entry in (entries if entries is not None else _entries()):
        fs.add_entry(entry)

    if with_orphan:
        #: The {HASH} footgun: a second, orphaned cache from an older argv.
        stale = fs.orphan_data_dir / "Documents" / "stale.bin"
        write_sparse(stale, 262_144, ((0, 262_144),))
        (fs.orphan_meta_dir / "Documents").mkdir(parents=True, exist_ok=True)
        (fs.orphan_meta_dir / "Documents" / "stale.bin").write_text(
            json.dumps({"ModTime": _MTIME, "ATime": _ATIME, "Size": 262_144,
                        "Rs": [{"Pos": 0, "Size": 262_144}],
                        "Fingerprint": "", "Dirty": False},
                       indent="\t") + "\n", encoding="utf-8")

    if with_sync_root:
        _build_sync_root(fs)
    return fs


def _build_sync_root(fs: FakeFs) -> None:
    """A plausible OneDrive folder, including everything preflight must reject."""
    root = fs.sync_root
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    (root / "Imágenes").mkdir(parents=True, exist_ok=True)
    (root / ".Trash-1000").mkdir(parents=True, exist_ok=True)
    (root / "Documents" / "report.docx").write_bytes(b"ok")
    (root / "Imágenes" / "photo.jpg").write_bytes(b"jpeg")
    (root / ".Trash-1000" / "directorysizes").write_bytes(b"")

    bad = root / "bad names"
    bad.mkdir(parents=True, exist_ok=True)
    for name in HOSTILE_NAMES:
        try:
            (bad / name).write_bytes(b"x")
        except OSError:                       # a name this filesystem refuses
            continue

    #: A relative path over MAX_REL_PATH_CHARS (400), for the preflight test.
    deep = root
    for _ in range(3):
        deep = deep / ("d" * 150)
    try:
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "over-long.txt").write_bytes(b"x")
    except OSError:
        pass
