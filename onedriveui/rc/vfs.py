"""The VFS disk cache: Files On-Demand's ground truth, and the one place that
deletes cached bytes.

``rclone mount --vfs-cache-mode full`` keeps two mirrored trees::

    <diskCache.path>/<rel_path>       the DATA — a SPARSE file, mode 0600,
                                      preallocated to the object's FULL remote
                                      size the moment it is first opened
    <diskCache.pathMeta>/<rel_path>   the SIDECAR — JSON, mode 0644:
                                      {ModTime, ATime, Size, Rs, Fingerprint, Dirty}

``Rs`` is the sorted, coalesced list of byte ranges physically present, and it is
the whole of Files On-Demand:

===================================== ==============================
``Rs`` is absent / ``null`` / ``[]``  online-only  ☁
``Rs == [{Pos: 0, Size: Size}]``      locally available  ✅
anything else                         partially cached  ◑
``Dirty: true``                       an un-uploaded local change  ↻
===================================== ==============================

Four measured facts shape every function here.

* **The cache location is never derivable.** rclone hashes command-line backend
  overrides into the fs name (``onedrive{MxOuf}:``), which changes the cache
  directory and orphans everything already materialised. ``vfs/stats`` reports
  the two absolute paths; nothing else may (invariant I4). This machine already
  carries two OneDrive trees, ``vfs/onedrive/`` (4 KB) and
  ``vfs/onedrive{MxOuf}/`` (172 MB) **[V]**.
* **There is no rc endpoint that evicts.** ``vfs/forget`` answers a reassuring
  ``{"forgotten": [...]}`` and provably leaves ``bytesUsed`` and every cache file
  untouched **[V]**; ``options/set {"vfs": ...}`` does not reach a live VFS
  **[V]**. Eviction is ``unlink()`` of both files, **meta first** (invariant I5),
  after which rclone logs ``detected external removal of cache file`` and
  re-downloads correctly on the next open **[V]**.
* **``Rs`` beats the physical file.** After an abnormal event a sparse image can
  hold bytes ``Rs`` does not list, and rclone re-downloads regardless. Badges
  come from ``Rs``; ``SEEK_DATA``/``SEEK_HOLE`` is the *synchronous* mirror of the
  same numbers (byte-identical in every measurement **[V]**) and is what a live
  progress bar reads, because the sidecar lags by ~10 s.
* **``vfs/refresh`` wants the STRING ``"true"``.** A JSON boolean is rejected —
  ``value must be string "recursive"=true`` **[V]**. It is the only parameter in
  the whole rc API that behaves this way.

Everything in this module that walks the tree is blocking and belongs on the
``IOPool`` (ARCHITECTURE §7.3). Nothing here touches Qt.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from onedriveui.errors import RcError, SafetyRefusal
from onedriveui.models import CacheEntry, DiskCacheInfo, FileState, QueueItem, RcEndpoint
from onedriveui.rc import guards
from onedriveui.rc.client import call_blocking

__all__ = [
    "CACHE_DATA_DIRNAME",
    "CACHE_META_DIRNAME",
    "FORCE_UPLOAD_EXPIRY",
    "QUEUE_RACE_MESSAGE",
    "RECURSIVE_TRUE",
    "bytes_local",
    "classify",
    "data_path",
    "defer_uploads",
    "disk_cache_info",
    "parse_disk_cache",
    "entry_for",
    "evict",
    "evict_tree",
    "force_upload_now",
    "forget",
    "local_extents",
    "meta_path",
    "meta_tree_for",
    "orphaned_cache_trees",
    "queue",
    "ranges_of",
    "read_sidecar",
    "refresh",
    "scan",
    "set_poll_interval",
]

log = logging.getLogger(__name__)

#: The two tree names rclone uses under ``--cache-dir``. Declared so
#: :func:`orphaned_cache_trees` and :func:`meta_tree_for` can find the fs-name
#: directory inside an authoritative path — never to *build* one (invariant I4).
CACHE_DATA_DIRNAME: Final[str] = "vfs"
CACHE_META_DIRNAME: Final[str] = "vfsMeta"

#: ``vfs/refresh``'s ``recursive`` parameter must be the STRING ``"true"``; a
#: JSON boolean is rejected. Verified on rclone v1.75.0 **[V]**::
#:
#:     {"error": "value must be string \\"recursive\\"=true", ..., "status": 500}
#:
#: (rclone's own docs say HTTP 400; the running binary answers 500. Neither
#: matters — we never send the boolean.)
RECURSIVE_TRUE: Final[str] = "true"

#: ``vfs/queue-set-expiry``'s "upload this now" value. rclone's documentation
#: names this exact magnitude: *"set it to a large negative number (eg
#: -1000000000)"*.
FORCE_UPLOAD_EXPIRY: Final[float] = -1_000_000_000.0

#: The ``vfs/queue-set-expiry`` failure that is a **normal ~5 s race** against
#: ``--vfs-write-back``, not an error: the item finished uploading between our
#: ``vfs/queue`` read and our write **[V]**.
QUEUE_RACE_MESSAGE: Final[str] = "id not found in queue"

#: How many sidecars :func:`scan` reads between ``progress`` callbacks. Small
#: enough that a UI progress bar moves, large enough that the callback is not
#: the dominant cost of the walk.
_PROGRESS_EVERY: Final[int] = 200

#: ``lseek(SEEK_DATA)`` past the last data region raises ``ENXIO`` — a normal end
#: condition, not a failure. ``EINVAL``/``ENOTSUP`` mean the filesystem has no
#: sparse support at all (FAT/exFAT), which is the sidecar fallback's trigger.
_SEEK_END_ERRNOS: Final[frozenset[int]] = frozenset({errno.ENXIO})
_SEEK_UNSUPPORTED_ERRNOS: Final[frozenset[int]] = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})


# ─────────────────────────────────────────────────────────────────────────────
# I4 — the cache location comes from the daemon, never from arithmetic
# ─────────────────────────────────────────────────────────────────────────────

def disk_cache_info(ep: RcEndpoint, *, timeout_s: float = 4.0) -> DiskCacheInfo:
    """Read ``vfs/stats`` and return the authoritative cache description.

    Invariant I4. The two paths inside ``diskCache`` are the **only** correct
    source of the cache location: they already account for the ``{HASH}`` suffix
    rclone appends when a backend option differs from ``rclone.conf``, for a
    remote sub-path (``onedrive:Documents`` caches under
    ``vfs/onedrive/Documents``) and for ``--cache-dir``. Hand-derivation misses
    all three and silently addresses the wrong tree.

    Args:
        ep: The mount's own rc endpoint — the one hosting the VFS, not the
            control plane.
        timeout_s: Socket timeout. Blocking; call from an ``IOPool`` thread.

    Returns:
        A :class:`~onedriveui.models.DiskCacheInfo` with both absolute paths and
        the aggregate counters the tray reads.

    Raises:
        SafetyRefusal: invariant ``"I4"`` — ``vfs/stats`` carried no
            ``diskCache`` block (the mount was started with
            ``--vfs-cache-mode off``, so there is no cache to address) or either
            path was empty.
        RcError: The daemon answered an error envelope.
        DaemonUnavailable: The daemon did not answer.
    """
    return parse_disk_cache(call_blocking(ep, "vfs/stats", {},
                                          timeout_s=timeout_s))


def parse_disk_cache(stats: Mapping[str, Any]) -> DiskCacheInfo:
    """Turn a ``vfs/stats`` body into a :class:`DiskCacheInfo`.

    Split out of :func:`disk_cache_info` so the same parse serves the blocking
    read and the asynchronous one. The tray needs these counters every couple of
    seconds — that is what tells it an upload is in flight — and ARCHITECTURE
    §7.6 bans synchronous HTTP on the GUI thread, so the polling path issues the
    request through :class:`~onedriveui.rc.client.RcClient` and lands here with
    the body already in hand. Keeping the parse pure means neither path can
    drift from the other, and this module stays free of Qt.

    Args:
        stats: The decoded ``vfs/stats`` response.

    Returns:
        The cache description, both absolute paths included.

    Raises:
        SafetyRefusal: invariant ``"I4"`` — see :func:`disk_cache_info`.
    """
    path, path_meta = guards.assert_cache_paths_from_stats(stats)
    disk: Mapping[str, Any] = stats.get("diskCache") or {}
    return DiskCacheInfo(
        path=path,
        path_meta=path_meta,
        bytes_used=int(disk.get("bytesUsed", 0) or 0),
        files=int(disk.get("files", 0) or 0),
        uploads_queued=int(disk.get("uploadsQueued", 0) or 0),
        uploads_in_progress=int(disk.get("uploadsInProgress", 0) or 0),
        errored_files=int(disk.get("erroredFiles", 0) or 0),
        out_of_space=bool(disk.get("outOfSpace", False)),
        hash_type=int(disk.get("hashType", 0) or 0),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Paths inside the cache — every one of them checked against traversal
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_rel(rel_path: str) -> str:
    """A cache-relative path, forward slashes, no leading or trailing separator.

    The cache trees are served by an internal local backend created as
    ``:local,encoding='Slash,Dot',links=false:`` **[V]**, so only ``/`` and the
    names ``.``/``..`` are ever encoded: colons, question marks, asterisks and
    non-ASCII survive byte-for-byte and the mapping from a mount-relative path to
    a cache path is a plain join.
    """
    return str(rel_path).replace("\\", "/").strip("/")


def _resolve_in(root: str | os.PathLike[str], rel_path: str, *, what: str) -> Path:
    """Join ``rel_path`` under ``root``, refusing anything that escapes it.

    ``rel_path`` reaches :func:`evict` from a context menu, an IPC message or a
    database row, and this module unlinks what it is handed. The guarantee this
    function provides is absolute: **for any input whatsoever, either it raises
    or the path it returns is inside** ``root``. A ``..`` component and a
    symlinked parent are refused; a leading ``/`` is contained rather than
    refused, because ``vfs/queue`` legitimately reports VFS-root-anchored names.

    Args:
        root: ``DiskCacheInfo.path`` or ``.path_meta``.
        rel_path: The item's path relative to the VFS root.
        what: What the caller wanted it for, quoted into the refusal.

    Returns:
        The absolute path inside ``root``.

    Raises:
        SafetyRefusal: invariant ``"I5"`` — the path escapes the cache tree,
            lexically or through a symlink.
    """
    base = Path(os.path.abspath(os.path.expanduser(str(root))))
    raw = str(rel_path).replace("\\", "/")
    if raw.startswith("/"):
        #: A leading "/" is ambiguous: `vfs/queue` sometimes reports a
        #: VFS-root-anchored name, and a caller may equally have joined
        #: `info.path` itself. An absolute path already inside the tree is
        #: relativised; anything else is read the queue's way, by stripping the
        #: separator — which lands it inside the tree and therefore harmless,
        #: rather than pointing `unlink()` at /etc.
        absolute = Path(os.path.abspath(raw))
        rel = (_normalise_rel(str(absolute.relative_to(base)))
               if absolute != base and absolute.is_relative_to(base)
               else _normalise_rel(raw))
    else:
        rel = _normalise_rel(raw)
    candidate = Path(os.path.abspath(base / rel)) if rel else base
    # Compare the lexically normalised forms first: that catches `..` and an
    # absolute `rel_path` even when nothing on the path exists yet.
    if candidate != base and not candidate.is_relative_to(base):
        raise SafetyRefusal(
            "I5",
            f"{what}: {rel_path!r} resolves to {str(candidate)!r}, outside the "
            f"VFS cache tree {str(base)!r}; a cache path may never escape the "
            f"tree vfs/stats reported",
        )
    # Then the symlink-resolved form, so a symlinked directory inside the tree
    # cannot redirect an unlink out of it.
    real_base = Path(os.path.realpath(base))
    real = Path(os.path.realpath(candidate))
    if real != real_base and not real.is_relative_to(real_base):
        raise SafetyRefusal(
            "I5",
            f"{what}: {rel_path!r} resolves through a symlink to {str(real)!r}, "
            f"outside the VFS cache tree {str(real_base)!r}",
        )
    return candidate


def data_path(info: DiskCacheInfo, rel_path: str) -> Path:
    """The sparse **data** file for ``rel_path``.

    Args:
        info: From :func:`disk_cache_info` — never a hand-built value (I4).
        rel_path: The item's path relative to the VFS root.

    Returns:
        The absolute path, proved to be inside ``info.path``.

    Raises:
        SafetyRefusal: ``rel_path`` escapes the cache tree.
    """
    return _resolve_in(info.path, rel_path, what="cache data path")


def meta_path(info: DiskCacheInfo, rel_path: str) -> Path:
    """The JSON **sidecar** for ``rel_path``.

    Args:
        info: From :func:`disk_cache_info`.
        rel_path: The item's path relative to the VFS root.

    Returns:
        The absolute path, proved to be inside ``info.path_meta``.

    Raises:
        SafetyRefusal: ``rel_path`` escapes the cache tree.
    """
    return _resolve_in(info.path_meta, rel_path, what="cache meta path")


def read_sidecar(path: Path | str) -> dict[str, Any]:
    """Parse one ``vfsMeta`` sidecar.

    Args:
        path: The sidecar file.

    Returns:
        The parsed object, or ``{}`` when the file is absent, unreadable, or a
        torn write racing rclone's own rewrite. ``{}`` is the correct answer for
        all three: :func:`classify` reads it as ``ONLINE_ONLY``, which is what
        rclone itself does with a data file that has no metadata.
    """
    try:
        with open(path, "rb") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ─────────────────────────────────────────────────────────────────────────────
# classify — the Rs rules
# ─────────────────────────────────────────────────────────────────────────────

def ranges_of(meta: Mapping[str, Any]) -> list[tuple[int, int]]:
    """``Rs`` as ``[(pos, length)]``.

    Args:
        meta: A parsed sidecar. ``Rs`` may be absent, JSON ``null``, ``[]`` or a
            list of ``{"Pos": int, "Size": int}`` objects.

    Returns:
        The ranges, in the order rclone wrote them (already sorted, coalesced
        and non-overlapping). Malformed or non-positive entries are dropped
        rather than raising: a torn sidecar must degrade to "less is cached",
        never to an exception in a badge painter.
    """
    raw = meta.get("Rs")
    if not isinstance(raw, list):
        return []
    out: list[tuple[int, int]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        try:
            pos = int(item.get("Pos", 0) or 0)
            length = int(item.get("Size", 0) or 0)
        except (TypeError, ValueError):
            continue
        if length > 0 and pos >= 0:
            out.append((pos, length))
    return out


def bytes_local(meta: Mapping[str, Any]) -> int:
    """How many bytes of the object are physically present, per ``Rs``.

    Args:
        meta: A parsed sidecar.

    Returns:
        ``sum(r.Size for r in Rs)``, clamped to ``Size`` so a stale sidecar can
        never report more than 100 % to a progress bar.
    """
    total = sum(length for _pos, length in ranges_of(meta))
    size = int(meta.get("Size", 0) or 0)
    return min(total, size) if size > 0 else total


def classify(meta: Mapping[str, Any], *, pinned: bool = False) -> FileState:
    """The Files On-Demand state of one cache item.

    The rules, in the order they are applied:

    ===================================== =====================================
    ``Dirty: true``                       :attr:`~FileState.DIRTY` — a local
                                          change that exists nowhere else (I3)
    no sidecar / ``Rs`` null / ``Rs`` []  :attr:`~FileState.ONLINE_ONLY`
    ``Rs == [{Pos: 0, Size: Size}]``      :attr:`~FileState.LOCAL`, or
                                          :attr:`~FileState.PINNED` when pinned
    anything else                         :attr:`~FileState.PARTIAL`
    ===================================== =====================================

    **``Rs`` wins over the physical file.** After an abnormal event the sparse
    image can hold bytes ``Rs`` does not list — measured: a restarted mount
    reported ``Rs: null`` for a file whose disk image held all 3 000 000 bytes,
    and ``bytesUsed`` excluded them **[V]**. rclone re-downloads regardless, so
    the badge must follow ``Rs``.

    A sidecar reporting ``Size: 0`` is ``LOCAL``: there is nothing to download,
    and calling an empty file "online-only" would leave a permanent cloud badge
    on it. In practice rclone writes **no sidecar at all** for a zero-byte object
    even after it has been read **[V]**, so this branch only fires for a
    truncated or hand-built one — and ``LOCAL`` is still the honest answer.

    Args:
        meta: The parsed sidecar, or ``{}`` when there is none.
        pinned: Whether the app's own pin table names this path. rclone has no
            pin concept — its evictor will reclaim any item that is not open —
            so "Always keep on this device" is ours to track and re-satisfy.

    Returns:
        The :class:`~onedriveui.models.FileState`. Never
        :attr:`~FileState.SYNCING`, :attr:`~FileState.ERROR` or
        :attr:`~FileState.EXCLUDED`: those come from ``vfs/queue``, the issues
        table and the filters file, none of which the sidecar knows about.
    """
    if not meta:
        return FileState.ONLINE_ONLY
    if meta.get("Dirty"):
        return FileState.DIRTY
    #: -1 means "the sidecar did not say", which is a torn write, not a size.
    raw_size = meta.get("Size")
    try:
        size = -1 if raw_size is None else int(raw_size)
    except (TypeError, ValueError):
        size = -1
    if size == 0:
        return FileState.PINNED if pinned else FileState.LOCAL
    ranges = ranges_of(meta)
    if not ranges:
        return FileState.ONLINE_ONLY
    if size > 0 and len(ranges) == 1 and ranges[0][0] == 0 and ranges[0][1] >= size:
        return FileState.PINNED if pinned else FileState.LOCAL
    #: An unreadable Size with real ranges: something is cached and we cannot
    #: prove it is everything, so the honest badge is "partly here".
    return FileState.PARTIAL


def entry_for(rel_path: str, meta: Mapping[str, Any], *,
              pinned: bool = False) -> CacheEntry:
    """One parsed sidecar as a :class:`~onedriveui.models.CacheEntry`.

    Args:
        rel_path: The item's path relative to the VFS root, forward slashes.
        meta: The parsed sidecar, or ``{}``.
        pinned: Whether the app's pin table names this path.

    Returns:
        The entry, with ``state`` already resolved by :func:`classify`.
        ``atime`` is rclone's own ``ATime`` field — the LRU key that
        ``--vfs-cache-max-age`` measures against — **not** the filesystem atime,
        which is ``relatime`` and unusable.
    """
    return CacheEntry(
        rel_path=_normalise_rel(rel_path),
        size=int(meta.get("Size", 0) or 0),
        bytes_local=bytes_local(meta),
        dirty=bool(meta.get("Dirty", False)),
        atime=meta.get("ATime") or None,
        mtime=meta.get("ModTime") or None,
        fingerprint=str(meta.get("Fingerprint", "") or ""),
        state=classify(meta, pinned=pinned),
    )


# ─────────────────────────────────────────────────────────────────────────────
# scan — the IOPool walk
# ─────────────────────────────────────────────────────────────────────────────

def scan(info: DiskCacheInfo, generation: int,
         progress: Callable[[int, int], None] | None = None,
         *,
         pinned: Iterable[str] | None = None,
         cancel: Callable[[], bool] | None = None) -> Iterator[CacheEntry]:
    """Walk ``pathMeta`` and yield one :class:`CacheEntry` per sidecar.

    The **meta** tree is walked, never the data tree: the sidecar is what rclone
    itself consults, so it is the source of truth for what will be served without
    a network round trip, and a data file with no sidecar is by definition
    uncached.

    A generator, so a caller can slice the walk across ``IOPool`` ticks and stay
    inside its time budget. Blocking: thousands of small JSON reads.
    **``IOPool`` only** (ARCHITECTURE §7.3) — and it must not write to SQLite
    itself; it emits rows for ``DbWriter``.

    Args:
        info: From :func:`disk_cache_info` (I4).
        generation: The scan generation from
            ``data.repo_files.next_cache_generation()``. Passed straight back to
            ``progress`` so a UI can discard callbacks from a superseded scan,
            and carried by the caller into ``upsert_cache_rows()`` so the walk
            and the prune agree on one number.
        progress: ``(entries_seen, generation)``, called every 200 entries and
            once at the end. Never called with a partial count after
            cancellation.
        pinned: Paths the app's pin table names, so the walk can emit
            :attr:`~FileState.PINNED` without a second pass.
        cancel: Polled once per directory and once per 200 entries; a truthy
            answer ends the walk cleanly, mid-tree, leaving every row it did not
            reach untouched in the database.

    Yields:
        One entry per readable sidecar. A sidecar that is absent, unreadable or
        torn is skipped: rclone rewrites them in place, so a concurrent scan
        will occasionally see one mid-write, and skipping is right — the item
        keeps its previous row rather than flapping to ``ONLINE_ONLY``.
    """
    root = Path(os.path.abspath(os.path.expanduser(str(info.path_meta))))
    pins = {_normalise_rel(p) for p in (pinned or ())}
    seen = 0
    if not root.is_dir():
        if progress is not None:
            progress(seen, int(generation))
        return
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        if cancel is not None and cancel():
            return
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, str(root)).replace(os.sep, "/")
            meta = read_sidecar(full)
            if not meta:
                continue
            seen += 1
            yield entry_for(rel, meta, pinned=rel in pins)
            if seen % _PROGRESS_EVERY == 0:
                if progress is not None:
                    progress(seen, int(generation))
                if cancel is not None and cancel():
                    return
    if progress is not None:
        progress(seen, int(generation))


# ─────────────────────────────────────────────────────────────────────────────
# local_extents — the synchronous mirror of Rs
# ─────────────────────────────────────────────────────────────────────────────

def local_extents(data_path: Path | str,
                  *, sidecar: Path | str | None = None) -> list[tuple[int, int]]:
    """The byte ranges physically present in a sparse cache file.

    ``SEEK_DATA``/``SEEK_HOLE`` is the kernel's own answer and is updated
    **synchronously** as bytes land, whereas the sidecar is only rewritten when
    the item is released — a measured lag of ~10 s. Use this for "did my pin
    finish?"; use ``Rs`` (:func:`classify`) for badges.

    Measured byte-identical to ``Rs`` in every test. On a 50 000 000-byte object
    after three 4 KiB reads at offsets 0, 4 096 000 and 32 768 000, rclone's
    sidecar held::

        Rs = [{Pos:0,Size:65536},{Pos:4096000,Size:65536},{Pos:32768000,Size:65536}]

    and this function returned ``[(0, 65536), (4096000, 65536),
    (32768000, 65536)]`` **[V]**.

    Never judge cachedness from ``st_size``: the cache file is preallocated to
    the object's full remote size on first open, so ``ls -l`` shows 50 MB for a
    file holding 192 KiB.

    Args:
        data_path: The sparse data file, from :func:`data_path`.
        sidecar: The ``vfsMeta`` file to fall back to when the filesystem has no
            sparse support. Defaults to the mirrored location derived from
            ``data_path``; pass it explicitly (from :func:`meta_path`, which is
            I4-clean) whenever it is known.

    Returns:
        ``[(offset, length)]``, ascending and non-overlapping — the same shape
        and the same numbers as ``Rs``. ``[]`` when the file does not exist or
        holds no data at all.

    Raises:
        OSError: The file exists but could not be opened at all. ``EINVAL`` /
            ``ENOTSUP`` from ``lseek`` — FAT and exFAT have no sparse support —
            is **not** raised: it falls back to the sidecar's ``Rs``.
    """
    path = Path(os.path.expanduser(str(data_path)))
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return []
    except IsADirectoryError:
        return []
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            #: A directory opens read-only on Linux; nothing about it is a
            #: cached extent, and asking the kernel would only reach the
            #: sparse-support fallback the long way round.
            return []
        size = status.st_size
        out: list[tuple[int, int]] = []
        pos = 0
        while pos < size:
            try:
                start = os.lseek(fd, pos, os.SEEK_DATA)
            except OSError as exc:
                if exc.errno in _SEEK_END_ERRNOS:
                    break                       # only holes remain — normal
                if exc.errno in _SEEK_UNSUPPORTED_ERRNOS:
                    return _extents_from_sidecar(path, sidecar)
                raise
            try:
                end = os.lseek(fd, start, os.SEEK_HOLE)
            except OSError as exc:
                if exc.errno in _SEEK_UNSUPPORTED_ERRNOS:
                    return _extents_from_sidecar(path, sidecar)
                end = size
            if end <= start:
                break
            out.append((start, end - start))
            pos = end
        return out
    finally:
        os.close(fd)


def _extents_from_sidecar(data: Path,
                          meta: Path | str | None) -> list[tuple[int, int]]:
    """The ``EINVAL`` fallback: read ``Rs`` instead of asking the kernel.

    FAT and exFAT implement neither sparse files nor ``SEEK_DATA``. The sidecar
    still carries the same ranges, only staler, so a filesystem that cannot
    answer synchronously degrades to the ~10 s-lagged answer rather than to no
    answer at all.
    """
    sidecar = Path(meta) if meta is not None else _mirror_meta_path(data)
    if sidecar is None:
        return []
    return ranges_of(read_sidecar(sidecar))


def _mirror_meta_path(data: Path) -> Path | None:
    """``…/vfs/<fs>/<rel>`` → ``…/vfsMeta/<fs>/<rel>``.

    Used **only** by the sparse-support fallback, where the alternative is no
    answer at all. It is not an I4 violation because it derives nothing about
    *which* cache tree is live: it rewrites one component of a path that
    ``vfs/stats`` already supplied.
    """
    parts = list(data.parts)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == CACHE_DATA_DIRNAME:
            parts[index] = CACHE_META_DIRNAME
            return Path(*parts)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# evict — THE function that can destroy user data
# ─────────────────────────────────────────────────────────────────────────────

def _unlink(path: Path) -> bool:
    """``unlink()`` one file, tolerating "already gone".

    Factored out so a test can record the exact ORDER of the two unlinks
    :func:`evict` performs, which is invariant I5 and is not observable any
    other way.

    Args:
        path: The file to remove.

    Returns:
        True if this call removed it, False if it was already absent.

    Raises:
        OSError: Anything other than ``ENOENT`` — a permission problem or a
            read-only cache directory is a real failure and must not be
            swallowed into a false "reclaimed" report.
    """
    try:
        os.unlink(str(path))
        return True
    except FileNotFoundError:
        return False


def _physical_bytes(path: Path) -> int:
    """Bytes actually occupied on disk by a sparse file.

    ``st_blocks * 512`` is the only honest measure: ``st_size`` is the object's
    full remote size, preallocated at first open, and reporting it would tell the
    user we freed 50 MB when we freed 192 KiB. Measured exactly equal to
    ``sum(Rs)`` on a real cache file **[V]**.
    """
    try:
        stat = os.stat(str(path))
    except OSError:
        return 0
    blocks = int(getattr(stat, "st_blocks", 0) or 0)
    return min(blocks * 512, int(stat.st_size)) if stat.st_size else blocks * 512


def evict(info: DiskCacheInfo, rel_path: str,
          queue_names: Iterable[str]) -> int:
    """Free the local copy of one file — "Free up space" for a single item.

    There is **no rc endpoint that does this**. ``vfs/forget`` returns
    ``{"forgotten": [...]}`` and provably frees nothing **[V]**, and
    ``options/set`` does not reach a live VFS **[V]**. The supported mechanism is
    to unlink both files; rclone then logs ``detected external removal of cache
    file``, recreates the sparse image on the next open and re-downloads
    correctly — verified, with a matching checksum afterwards **[V]**.

    Two safety properties, in this order:

    1. **Invariant I3** — :func:`~onedriveui.rc.guards.assert_evict_safe` first.
       A ``Dirty: true`` sidecar is an un-uploaded local change: those bytes
       exist on this disk and nowhere else on the planet. An item named in
       ``vfs/queue`` is seconds away from being uploaded. Either one is refused
       **before a single file is touched**.
    2. **Invariant I5** — the **meta** sidecar is unlinked *strictly before* the
       data file. A crash between the two then leaves a data file with no
       metadata, which rclone correctly treats as uncached; the reverse would
       leave metadata claiming ranges that no longer exist, and rclone would
       serve holes as zeros.

    Args:
        info: From :func:`disk_cache_info` (I4).
        rel_path: The item's path relative to the VFS root.
        queue_names: The ``name`` of every ``vfs/queue`` row, from
            :func:`queue`. Pass the live queue, not a cached one — this is the
            second half of I3.

    Returns:
        Bytes reclaimed, measured as ``st_blocks * 512`` **before** the unlink.
        ``0`` when nothing was cached.

    Raises:
        SafetyRefusal: invariant ``"I3"`` — the item is dirty or queued, and
            **no file was touched**; or ``"I5"`` — ``rel_path`` escapes the cache
            tree.
        OSError: The unlink failed for a reason other than "already gone".
    """
    rel = _normalise_rel(rel_path)
    meta_file = meta_path(info, rel)
    data_file = data_path(info, rel)

    # I3 FIRST. Nothing below this line may run for a dirty or queued item.
    guards.assert_evict_safe(read_sidecar(meta_file), queue_names, rel)

    freed = _physical_bytes(data_file)
    # I5: META, then DATA. The order is the whole point of this function.
    _unlink(meta_file)
    _unlink(data_file)
    log.info("evicted %r from the VFS cache, reclaiming %d bytes", rel, freed)
    return freed


def evict_tree(info: DiskCacheInfo, rel_prefix: str,
               queue_names: Iterable[str]) -> int:
    """Free the local copies of a whole folder — "Free up space" on a directory.

    Every item under ``rel_prefix`` is checked against invariant I3 **before any
    of them is unlinked**, so one dirty file inside the folder refuses the whole
    operation rather than deleting its siblings and then stopping half way. Then
    each item is evicted individually, in :func:`evict`'s meta-then-data order.

    **Both** trees are enumerated. A data file with no sidecar is the exact state
    a crash between the two unlinks leaves behind (I5): rclone reads it as
    uncached and will never serve it, but it still occupies the disk the user
    asked to reclaim, and only the data walk can find it.

    Args:
        info: From :func:`disk_cache_info` (I4).
        rel_prefix: The folder relative to the VFS root. ``""`` means the whole
            cache.
        queue_names: The ``name`` of every ``vfs/queue`` row.

    Returns:
        Total bytes reclaimed.

    Raises:
        SafetyRefusal: invariant ``"I3"`` — some item under the prefix is dirty
            or queued, and **nothing was touched**; or ``"I5"`` — the prefix
            escapes the cache tree.
        OSError: An unlink failed for a reason other than "already gone".
    """
    prefix = _normalise_rel(rel_prefix)
    names = list(queue_names)

    #: Both trees, because a data file with NO sidecar is exactly the state a
    #: crash between the two unlinks leaves (I5). rclone reads it as uncached,
    #: so it is never served — but it still occupies the disk the user asked us
    #: to reclaim, and only the data walk can see it.
    targets = sorted(set(_items_under(meta_path(info, prefix), meta_path(info, ""),
                                      prefix))
                     | set(_items_under(data_path(info, prefix), data_path(info, ""),
                                        prefix)))

    # Pass 1: prove every single item is safe. A refusal here has touched nothing.
    for rel in targets:
        guards.assert_evict_safe(read_sidecar(meta_path(info, rel)), names, rel)

    # Pass 2: unlink, still meta-then-data per item.
    freed = 0
    for rel in targets:
        freed += evict(info, rel, names)
    _prune_empty_dirs(meta_path(info, prefix), meta_path(info, ""))
    _prune_empty_dirs(data_path(info, prefix), data_path(info, ""))
    return freed


def _items_under(node: Path, root: Path, prefix: str) -> list[str]:
    """Every file at or under ``node``, as paths relative to ``root``.

    Args:
        node: The file or directory the prefix resolved to, in one of the two
            trees.
        root: That tree's root, so the returned paths are in the same vocabulary
            ``vfs/queue`` and the sidecar walk use.
        prefix: The already-normalised prefix, used when ``node`` is itself a
            file.

    Returns:
        Cache-relative paths, forward slashes. Empty when ``node`` does not
        exist — one tree may legitimately hold an item the other does not.
    """
    if node.is_file():
        return [prefix]
    if not node.is_dir():
        return []
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(str(node), followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            out.append(os.path.relpath(full, str(root)).replace(os.sep, "/"))
    return out


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove now-empty directories at and above ``start``, never past ``stop``.

    The subtree is swept bottom-up first, so a nested ``Documents/sub/`` left
    empty by the eviction goes too, and then the walk continues upwards from the
    prefix. Cosmetic only — rclone recreates any of them on demand. Failures are
    ignored: a directory that is not empty, or that a concurrent mount just
    repopulated, is not an error.
    """
    try:
        base = Path(os.path.realpath(stop))
    except OSError:                                          # pragma: no cover
        return
    if start.is_dir():
        for dirpath, dirnames, _filenames in os.walk(str(start), topdown=False,
                                                     followlinks=False):
            del dirnames
            #: The tree root itself always survives: rclone owns it, and it
            #: would only have to recreate it on the next open.
            if Path(os.path.realpath(dirpath)) == base:
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                continue
    current = start if start.is_dir() else start.parent
    while True:
        try:
            real = Path(os.path.realpath(current))
        except OSError:                                      # pragma: no cover
            return
        if real == base or not real.is_relative_to(base):
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


# ─────────────────────────────────────────────────────────────────────────────
# vfs/queue — the upload side
# ─────────────────────────────────────────────────────────────────────────────

def queue(ep: RcEndpoint, *, timeout_s: float = 4.0) -> list[QueueItem]:
    """The pending write-back queue.

    Every name here is untouchable by :func:`evict` (invariant I3): the item is
    a local change that has not reached the remote yet.

    Args:
        ep: The mount's rc endpoint.
        timeout_s: Socket timeout. Blocking; ``IOPool`` only.

    Returns:
        One :class:`~onedriveui.models.QueueItem` per row. ``expiry`` is seconds
        until the upload starts and **can be negative** once the deadline has
        passed. Empty when nothing is queued.

    Raises:
        RcError: The daemon answered an error envelope.
        DaemonUnavailable: The daemon did not answer.
    """
    body = call_blocking(ep, "vfs/queue", {}, timeout_s=timeout_s)
    rows = body.get("queue") or []
    out: list[QueueItem] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        out.append(QueueItem(
            name=str(row.get("name", "")),
            id=int(row.get("id", 0) or 0),
            size=int(row.get("size", 0) or 0),
            expiry=float(row.get("expiry", 0.0) or 0.0),
            tries=int(row.get("tries", 0) or 0),
            delay=float(row.get("delay", 0.0) or 0.0),
            uploading=bool(row.get("uploading", False)),
        ))
    return out


def _is_queue_race(exc: RcError) -> bool:
    """True for the ``id not found in queue`` answer, which is not a failure.

    The item finished uploading between our ``vfs/queue`` read and our write —
    a ~5 s window that ``--vfs-write-back 5s`` makes routine **[V]**.
    """
    return QUEUE_RACE_MESSAGE in str(getattr(exc, "message", "") or str(exc))


def force_upload_now(ep: RcEndpoint, item_id: int, *,
                     timeout_s: float = 4.0) -> None:
    """Push one queued upload to the front — the "Sync now" button.

    Args:
        ep: The mount's rc endpoint.
        item_id: The ``id`` from :func:`queue`. Ids are per-VFS and monotonic;
            they are **not** queue positions.
        timeout_s: Socket timeout.

    Raises:
        RcError: A real failure. ``id not found in queue`` is swallowed: it is a
            normal race against ``--vfs-write-back``, not an error. Setting the
            expiry of an item that has already *started* uploading also has no
            effect, by rclone's design.
        DaemonUnavailable: The daemon did not answer.
    """
    try:
        call_blocking(ep, "vfs/queue-set-expiry",
                      {"id": int(item_id), "expiry": FORCE_UPLOAD_EXPIRY},
                      timeout_s=timeout_s)
    except RcError as exc:
        if _is_queue_race(exc):
            log.debug("vfs/queue-set-expiry id=%s lost the write-back race", item_id)
            return
        raise


def defer_uploads(ep: RcEndpoint, seconds: float, *,
                  timeout_s: float = 4.0) -> int:
    """Hold every queued upload for ``seconds`` — **this is how pause works**.

    rclone has no pause. ``core/bwlimit`` throttles but still uploads, and
    stopping the mount would take the user's files offline. What actually stops
    data leaving the machine is pushing every queued item's expiry into the
    future; the write-back queue then simply does not fire, the files stay
    ``Dirty`` and materialised, and :func:`force_upload_now` (or letting the
    deadline arrive) resumes exactly where it left off.

    The expiry is set **absolutely**, not relatively, so calling this once a tick
    while paused keeps the deadline pinned at ``seconds`` from now instead of
    compounding into an unreachable future. Measured: an absolute
    ``{"id": n, "expiry": 3600}`` sets the countdown to 3600 s; adding
    ``"relative": true`` adds to whatever remained **[V]**.

    Args:
        ep: The mount's rc endpoint.
        seconds: How long to hold the queue, from now. Must be positive — a
            negative value is :func:`force_upload_now`'s job and passing one here
            would silently flush the queue the caller meant to freeze.
        timeout_s: Socket timeout per item.

    Returns:
        How many items were deferred. Items already ``uploading`` are skipped
        and not counted: rclone documents that setting the expiry of an item that
        has started has no effect, and reporting it as deferred would make the
        UI claim a pause that did not happen.

    Raises:
        ValueError: ``seconds`` is not positive.
        RcError: A real failure. Per-item ``id not found in queue`` races are
            skipped.
        DaemonUnavailable: The daemon did not answer.
    """
    if seconds <= 0:
        raise ValueError(
            f"defer_uploads(seconds={seconds!r}): a pause must be a positive "
            f"number of seconds; a negative expiry FLUSHES the queue "
            f"(see force_upload_now)")
    deferred = 0
    for item in queue(ep, timeout_s=timeout_s):
        if item.uploading:
            continue
        try:
            call_blocking(ep, "vfs/queue-set-expiry",
                          {"id": item.id, "expiry": float(seconds)},
                          timeout_s=timeout_s)
        except RcError as exc:
            if _is_queue_race(exc):
                continue
            raise
        deferred += 1
    if deferred:
        log.info("deferred %d queued upload(s) by %.0fs", deferred, seconds)
    return deferred


# ─────────────────────────────────────────────────────────────────────────────
# Orphaned cache trees — the {HASH} footgun
# ─────────────────────────────────────────────────────────────────────────────

def _base_remote(fs_dirname: str) -> str:
    """``"onedrive{MxOuf}"`` → ``"onedrive"``.

    rclone appends ``{base64hash}`` to the remote name whenever a backend option
    is overridden on the command line, which is exactly what invariant I1
    forbids and exactly what produced the two parallel trees this machine
    carries.
    """
    return fs_dirname.split("{", 1)[0]


def _tree_root_and_fsname(path: str | os.PathLike[str],
                          tree_name: str) -> tuple[Path, str]:
    """Split an authoritative cache path into ``(<cache>/<tree>, "<fsname>")``.

    ``diskCache.path`` is ``<cache>/vfs/<fsname>`` for ``onedrive:`` but
    ``<cache>/vfs/<fsname>/<sub>/<path>`` for ``onedrive:Documents`` — so the
    fs-name directory is found by walking **up** to the child of ``vfs``, never
    by taking the parent. Getting this wrong would make every real folder of the
    live cache look like an orphaned sibling.

    Args:
        path: ``DiskCacheInfo.path`` or ``.path_meta``.
        tree_name: :data:`CACHE_DATA_DIRNAME` or :data:`CACHE_META_DIRNAME`.

    Returns:
        ``(tree_root, fsname)``, or ``(Path(), "")`` when the path is not under a
        directory of that name at all — in which case there is nothing to
        enumerate and no guess is made.
    """
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    for candidate in (target, *target.parents):
        parent = candidate.parent
        if parent.name == tree_name and parent != candidate:
            return parent, candidate.name
    return Path(), ""


def _tree_bytes(root: Path) -> int:
    """Total apparent size of every regular file under ``root``.

    Apparent size, not ``st_blocks``: this number is shown to the user as "you
    could reclaim N", and an orphaned tree's files are fully materialised
    leftovers whose apparent and physical sizes agree. Unreadable entries are
    skipped rather than raising — the figure is advisory.
    """
    total = 0
    for dirpath, _dirnames, filenames in os.walk(str(root), followlinks=False):
        for name in filenames:
            try:
                stat = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            total += int(stat.st_size)
    return total


def meta_tree_for(info: DiskCacheInfo, data_tree: Path | str) -> Path:
    """The ``vfsMeta`` twin of a ``vfs`` tree returned by
    :func:`orphaned_cache_trees`.

    Reclaiming an orphan means deleting **both** trees; this pairs them without
    the caller re-deriving anything. The meta root itself comes from
    ``info.path_meta``, so the authoritative value still wins (I4).

    Args:
        info: From :func:`disk_cache_info`.
        data_tree: One of the paths :func:`orphaned_cache_trees` returned.

    Returns:
        The mirrored ``vfsMeta`` directory. It may not exist — an orphan can have
        lost one half already.
    """
    meta_root, _meta_fs = _tree_root_and_fsname(info.path_meta, CACHE_META_DIRNAME)
    if not str(meta_root):
        mirrored = _mirror_meta_path(Path(os.path.abspath(str(data_tree))))
        return mirrored if mirrored is not None else Path(data_tree)
    return meta_root / Path(data_tree).name


def orphaned_cache_trees(info: DiskCacheInfo, *,
                         same_remote_only: bool = True) -> list[tuple[Path, int]]:
    """Cache trees that belong to this remote but are no longer the live one.

    **The ``{HASH}`` footgun.** rclone hashes the set of command-line backend
    overrides into the fs canonical name, so adding, changing or removing a
    single ``--onedrive-*`` flag turns ``onedrive:`` into ``onedrive{MxOuf}:`` —
    a *different* cache directory. Every previously materialised file instantly
    becomes online-only and the old tree is stranded on disk forever. This is
    precisely what invariant I1 exists to prevent, and this function is how the
    damage already done is found: **this machine carries
    ``~/.cache/rclone/vfs/onedrive/`` beside ``vfs/onedrive{MxOuf}/`` right
    now** **[V]**.

    Args:
        info: From :func:`disk_cache_info` (I4). Its ``path`` identifies the
            live tree, which is never reported.
        same_remote_only: Report only trees whose name shares the live tree's
            base remote — ``onedrive`` and ``onedrive{MxOuf}`` are siblings,
            ``local`` is a different remote's cache and is not ours to offer for
            deletion. Turn it off only for a full "what is in ~/.cache/rclone"
            audit.

    Returns:
        ``[(path, bytes)]``, sorted by path, of **data** trees only — pair each
        with :func:`meta_tree_for` before reclaiming. Empty when the live path is
        not under a ``vfs`` directory, which is the safe answer: nothing is
        proposed for deletion on a guess.
    """
    vfs_root, live_name = _tree_root_and_fsname(info.path, CACHE_DATA_DIRNAME)
    if not live_name or not vfs_root.is_dir():
        return []
    live_base = _base_remote(live_name)
    out: list[tuple[Path, int]] = []
    try:
        children = sorted(vfs_root.iterdir())
    except OSError:                                          # pragma: no cover
        return []
    for child in children:
        if not child.is_dir() or child.name == live_name:
            continue
        if same_remote_only and _base_remote(child.name) != live_base:
            continue
        out.append((child, _tree_bytes(child)))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Directory cache: refresh, forget, poll interval
# ─────────────────────────────────────────────────────────────────────────────

def refresh(ep: RcEndpoint, dirs: Sequence[str] | None = None, *,
            recursive: bool = False, timeout_s: float = 30.0) -> dict[str, Any]:
    """Re-read directories into the VFS directory cache — eager, not lazy.

    ``recursive`` is sent as the **string** ``"true"``. A JSON boolean is
    rejected outright — ``value must be string "recursive"=true`` **[V]** — and
    this is the only parameter in the whole 101-command rc API that behaves that
    way.

    **Never call this recursively from a UI action on OneDrive.** OneDrive
    reports ``ListR: False``, so a recursive refresh is one Microsoft Graph
    request *per directory* — ~120 for this machine's account. Reserve it for the
    ``--vfs-refresh`` startup warm-up.

    Args:
        ep: The mount's rc endpoint.
        dirs: Directories relative to the VFS root. Empty or ``None`` refreshes
            the root. An empty-string entry is dropped: rclone answers
            ``file does not exist`` for an explicit ``""``, while omitting the
            key entirely is what means "the root" **[V]**.
        recursive: Walk the whole tree.
        timeout_s: Socket timeout. A recursive refresh is slow by construction;
            the default is generous for that reason.

    Returns:
        ``{"result": {"<dir>": "OK", ...}}`` — one entry per requested
        directory.

    Raises:
        RcError: The daemon answered an error envelope.
        DaemonUnavailable: The daemon did not answer.
    """
    params: dict[str, Any] = {}
    names = [str(d) for d in (dirs or []) if str(d).strip("/")]
    for index, name in enumerate(names):
        params["dir" if index == 0 else f"dir{index + 1}"] = name.strip("/")
    if recursive:
        params["recursive"] = RECURSIVE_TRUE
    return call_blocking(ep, "vfs/refresh", params, timeout_s=timeout_s)


def forget(ep: RcEndpoint, dirs: Sequence[str] | None = None,
           files: Sequence[str] | None = None, *,
           timeout_s: float = 4.0) -> list[str]:
    """Invalidate directory-cache entries — lazy, cheap, and **frees no disk**.

    ``vfs/forget`` drops entries from the in-memory *directory* cache so the next
    access re-``stat``s from the remote. It answers a reassuring
    ``{"forgotten": [...]}`` and leaves ``diskCache.bytesUsed`` and every cache
    file exactly as they were — measured, with the data files still on disk
    afterwards **[V]**. **Never** call it expecting to reclaim space; that is
    :func:`evict`.

    Args:
        ep: The mount's rc endpoint.
        dirs: Directories to forget. ``None``/empty with no ``files`` forgets
            everything.
        files: Individual files to forget.
        timeout_s: Socket timeout.

    Returns:
        The names rclone says it forgot.

    Raises:
        RcError: The daemon answered an error envelope.
        DaemonUnavailable: The daemon did not answer.
    """
    params: dict[str, Any] = {}
    for index, name in enumerate(str(d).strip("/") for d in (dirs or [])):
        params["dir" if index == 0 else f"dir{index + 1}"] = name
    for index, name in enumerate(str(f).strip("/") for f in (files or [])):
        params["file" if index == 0 else f"file{index + 1}"] = name
    body = call_blocking(ep, "vfs/forget", params, timeout_s=timeout_s)
    return [str(name) for name in (body.get("forgotten") or [])]


def set_poll_interval(ep: RcEndpoint, seconds: int, *,
                      timeout_s: float = 10.0) -> dict[str, Any]:
    """Change the live poller's interval — the one VFS option a running mount
    accepts.

    Every other VFS option needs a mount restart: ``options/set {"vfs": ...}``
    returns ``{}``, ``options/get`` reflects the change, and ``vfs/stats.opt``
    does not move **[V]**.

    Args:
        ep: The mount's rc endpoint.
        seconds: The new interval. ``0`` disables polling. It must stay strictly
            below ``--dir-cache-time`` or polling buys nothing.
        timeout_s: Socket timeout. rclone applies the change only when the
            current poll function picks it up.

    Returns:
        ``{"enabled": bool, "interval": {...}, "supported": bool}``.

    Raises:
        RcError: **HTTP 500 ``poll-interval is not supported by this remote``**
            on a backend without ``ChangeNotify`` — measured on the ``local``
            backend **[V]**. Gate on ``Capabilities.change_notify`` (OneDrive has
            it: ``ChangeNotify: true``, ``interval 1m0s`` **[V]**) rather than
            catching this.
        DaemonUnavailable: The daemon did not answer.
    """
    return call_blocking(ep, "vfs/poll-interval",
                         {"interval": f"{int(seconds)}s"}, timeout_s=timeout_s)
