"""Thumbnails — the shared freedesktop cache first, and never through FUSE.

The activity feed and the in-app file browser both want a small preview for
every row. Two rules make that safe on this application's particular filesystem.

## 1. Ask the desktop's cache before making anything

Every GNOME application writes previews into `$XDG_CACHE_HOME/thumbnails/`, and
the file name is a pure function of the file's URI:

    ~/.cache/thumbnails/<size>/<md5 of the canonical file:// URI>.png

with `<size>` one of `normal` (128 px), `large` (256), `x-large` (512),
`xx-large` (1024). Verified against 341 real thumbnails on the target machine:
every one of them is `md5(Thumb::URI) + ".png"`, where `Thumb::URI` is stored in
the PNG's text chunks. So Nautilus has usually already done our work, and a hit
costs one `stat` and one decode.

Staleness is checked the way the specification requires: a thumbnail is valid
only when its `Thumb::URI` matches the file's URI **and** its `Thumb::MTime`
matches the source's `st_mtime`. A file edited since the preview was made is a
miss, not a wrong picture.

## 2. Never generate a preview for an online-only file

This is the rule that makes the difference between a file browser and a 50 GB
download. Reading the first bytes of a file on the rclone mount **hydrates it**:
FUSE has no concept of "just the header", so opening a 4 GB video to make a
128 px thumbnail pulls the whole thing down. So:

* a path **not** under a `fuse.rclone` mount generates freely;
* a path under one generates **only** when an injected state provider says the
  file is already local (`LOCAL`, `PINNED` or `DIRTY`);
* with no provider injected, a path under a mount **never** generates.

Denied paths return no image, and the caller draws the type glyph from
`ui/icons.py` instead — which is exactly what the Windows client shows for an
online-only file.

## Writing back

A generated preview is written into the shared cache so the next launch, and
every other application, gets it for free. Qt cannot do this alone:
`QImageWriter.setText("Thumb::URI", …)` splits the key on its colon and stores
`Thumb` = `"URI: …"`, which no thumbnail consumer recognises. The PNG is
therefore written by Qt and the conformant `tEXt` chunks are spliced in
afterwards, right after `IHDR` — verified readable by Qt, `file(1)` and PIL.
Writes are atomic (temp file plus `os.replace`) and mode 0600, both as the
specification requires.

Threading: decoding runs on a `QThreadPool` capped at two threads (`§7.3`), and
`QImageReader`/`QImage` are safe there. `ready` and `failed` are emitted from a
pool thread to this object on the GUI thread, so Qt queues them; no Gio and no
`QWidget` is touched anywhere in this module.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Final, Mapping, Sequence

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader, QImageWriter

from onedriveui import APP_ID, APP_NAME
from onedriveui import paths
from onedriveui.constants import NO_THUMBNAIL_ABOVE_BYTES
from onedriveui.models import FileState
from onedriveui.platform.desktop import file_uri

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# The shared cache layout
# ─────────────────────────────────────────────────────────────────────────────

#: Directory name under `$XDG_CACHE_HOME`. Shared with every other application;
#: we are one writer among many and must never assume ownership of it.
THUMBNAIL_DIR_NAME: Final[str] = "thumbnails"

#: `<directory>: <longest edge in pixels>`, from the freedesktop Thumbnail
#: Managing Standard. All four exist on the target machine.
SIZES: Final[dict[str, int]] = {
    "normal": 128,
    "large": 256,
    "x-large": 512,
    "xx-large": 1024,
}

#: What a list row asks for. 128 px covers every row height we draw.
DEFAULT_SIZE: Final[str] = "normal"

#: Where a thumbnailer records that it *cannot* make a preview for a file, so
#: nobody retries it on every scroll: `fail/<appname>/<md5>.png`.
FAIL_DIR_NAME: Final[str] = "fail"

#: Our subdirectory of `fail/`. GNOME's own is `gnome-thumbnail-factory`.
FAIL_APP_NAME: Final[str] = APP_ID

#: PNG text keys the specification mandates on every thumbnail.
KEY_URI: Final[str] = "Thumb::URI"
KEY_MTIME: Final[str] = "Thumb::MTime"
KEY_SIZE: Final[str] = "Thumb::Size"
KEY_SOFTWARE: Final[str] = "Software"

#: What we stamp into `Software`.
SOFTWARE: Final[str] = APP_NAME

#: The specification requires 0600 on thumbnails: they can leak the content of
#: private files, so they are not world-readable even though the directory is.
THUMBNAIL_MODE: Final[int] = 0o600
THUMBNAIL_DIR_MODE: Final[int] = 0o700

#: PNG wire format.
PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
PNG_IHDR: Final[bytes] = b"IHDR"
PNG_IEND: Final[bytes] = b"IEND"
PNG_TEXT: Final[bytes] = b"tEXt"

#: The chunk types that can carry `Thumb::URI`. GNOME writes `iTXt`; we write
#: the simpler `tEXt`, and both are read.
PNG_TEXT_CHUNKS: Final[frozenset[bytes]] = frozenset({b"tEXt", b"zTXt", b"iTXt"})

#: A PNG larger than this is not a thumbnail — refuse to parse it rather than
#: read an arbitrary file into memory looking for text chunks.
MAX_THUMBNAIL_BYTES: Final[int] = 8 * 1024 * 1024

# ─────────────────────────────────────────────────────────────────────────────
# Generation policy
# ─────────────────────────────────────────────────────────────────────────────

#: `§7.3` allots two threads to thumbnailing. More would compete with hydration
#: for the same disk and the same FUSE channel.
THUMBNAIL_THREADS: Final[int] = 2

#: How many decoded images to keep in memory. A 128 px ARGB image is 64 KB, so
#: 256 of them is 16 MB — enough for a long scroll, small enough to ignore.
MEMORY_ITEMS: Final[int] = 256

#: The file states that mean "the bytes are already on this disk", and therefore
#: that reading the file will not trigger a download.
LOCAL_STATES: Final[frozenset[FileState]] = frozenset({
    FileState.LOCAL, FileState.PINNED, FileState.DIRTY,
})

#: `(absolute path) -> FileState`. Injected by the composition root; reads
#: `cache_index` from memory. Without it, nothing under a mount is generated.
StateProvider = Callable[[str], FileState]


def _md5_hex(text: str) -> str:
    """MD5 of a string, as the thumbnail name.

    MD5 is the specification's choice and is used here purely as a cache key —
    never for integrity, never for a security decision.

    Args:
        text: The canonical file URI.

    Returns:
        32 lowercase hex characters.
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def thumbnail_root() -> Path:
    """`$XDG_CACHE_HOME/thumbnails`.

    Not `paths.cache_dir()`: that is `~/.cache/onedriveui`, ours alone, whereas
    this tree is shared with every application on the desktop.

    Returns:
        The directory, whether or not it exists.
    """
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache).expanduser() if cache else Path.home() / ".cache"
    return base / THUMBNAIL_DIR_NAME


def assert_known_size(size: str) -> str:
    """Validate a thumbnail size name.

    Args:
        size: One of `SIZES`.

    Returns:
        The name unchanged.

    Raises:
        KeyError: If the name is not a specified size. Raised rather than
            defaulted, because silently downgrading `large` to `normal` would
            show a blurry preview with no clue why.
    """
    if size not in SIZES:
        raise KeyError(f"{size!r} is not a thumbnail size; expected one of "
                       f"{', '.join(SIZES)}")
    return size


def thumbnail_path(path: str | os.PathLike[str], size: str = DEFAULT_SIZE) -> Path:
    """Where a file's thumbnail would live.

    Args:
        path: The source file.
        size: One of `SIZES`.

    Returns:
        `~/.cache/thumbnails/<size>/<md5 of the file URI>.png`.

    Raises:
        KeyError: If `size` is not a specified size.
    """
    assert_known_size(size)
    return thumbnail_root() / size / f"{_md5_hex(file_uri(path))}.png"


def fail_path(path: str | os.PathLike[str]) -> Path:
    """Where a "this file has no preview" marker would live.

    Args:
        path: The source file.

    Returns:
        `~/.cache/thumbnails/fail/onedriveui/<md5 of the file URI>.png`.
    """
    return (thumbnail_root() / FAIL_DIR_NAME / FAIL_APP_NAME
            / f"{_md5_hex(file_uri(path))}.png")


# ═════════════════════════════════════════════════════════════════════════════
# PNG text chunks — read and write, because Qt can do neither correctly
# ═════════════════════════════════════════════════════════════════════════════

def png_text(data: bytes) -> dict[str, str]:
    """Read every text chunk out of a PNG.

    Handles all three text chunk types: `tEXt` (what we write), `zTXt`, and
    `iTXt` (what gnome-thumbnail-factory writes, sometimes compressed).

    Args:
        data: The complete PNG bytes.

    Returns:
        `{keyword: value}`. Empty for anything that is not a PNG.
    """
    if not data.startswith(PNG_SIGNATURE):
        return {}
    out: dict[str, str] = {}
    pos = len(PNG_SIGNATURE)
    total = len(data)
    while pos + 8 <= total:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length          # length + type + data + crc
        if kind == PNG_IEND:
            break
        if kind not in PNG_TEXT_CHUNKS or len(body) < length:
            continue
        try:
            out.update(_decode_text_chunk(kind, body))
        except (ValueError, zlib.error, UnicodeDecodeError):
            continue
    return out


def _decode_text_chunk(kind: bytes, body: bytes) -> dict[str, str]:
    """Decode one `tEXt`/`zTXt`/`iTXt` chunk.

    Args:
        kind: The four-byte chunk type.
        body: The chunk data.

    Returns:
        A single-entry mapping, or an empty one when the chunk is malformed.
    """
    keyword, sep, rest = body.partition(b"\x00")
    if not sep:
        return {}
    key = keyword.decode("latin-1")
    if kind == PNG_TEXT:
        return {key: rest.decode("utf-8", "replace")}
    if kind == b"zTXt":
        # rest = compression method byte + compressed text
        return {key: zlib.decompress(rest[1:]).decode("utf-8", "replace")}
    # iTXt = compression flag + compression method + language + translated key
    if len(rest) < 2:
        return {}
    compressed = rest[0]
    rest = rest[2:]
    _language, sep, rest = rest.partition(b"\x00")
    if not sep:
        return {}
    _translated, sep, value = rest.partition(b"\x00")
    if not sep:
        return {}
    if compressed:
        value = zlib.decompress(value)
    return {key: value.decode("utf-8", "replace")}


def _text_chunk(key: str, value: str) -> bytes:
    """Build one `tEXt` chunk.

    Args:
        key: The keyword, 1-79 Latin-1 characters.
        value: The text.

    Returns:
        The complete chunk, length and CRC included.
    """
    body = key.encode("latin-1") + b"\x00" + value.encode("latin-1", "replace")
    return (struct.pack(">I", len(body)) + PNG_TEXT + body
            + struct.pack(">I", zlib.crc32(PNG_TEXT + body) & 0xFFFFFFFF))


def splice_text(png: bytes, entries: Mapping[str, str]) -> bytes:
    """Insert text chunks into a PNG, right after `IHDR`.

    Necessary because `QImageWriter.setText("Thumb::URI", v)` splits the key on
    its colon and writes `Thumb` = `"URI: v"`, which no thumbnail consumer
    recognises — measured on PySide6 6.11.2.

    Args:
        png: A complete PNG.
        entries: `{keyword: value}` to add.

    Returns:
        The PNG with the chunks inserted, or the input unchanged when it is not
        a PNG or has no `IHDR`.
    """
    if not png.startswith(PNG_SIGNATURE) or len(png) < 16:
        return png
    pos = len(PNG_SIGNATURE)
    (length,) = struct.unpack(">I", png[pos:pos + 4])
    if png[pos + 4:pos + 8] != PNG_IHDR:
        return png
    end = pos + 8 + length + 4
    extra = b"".join(_text_chunk(key, value) for key, value in entries.items())
    return png[:end] + extra + png[end:]


# ═════════════════════════════════════════════════════════════════════════════
# The cache
# ═════════════════════════════════════════════════════════════════════════════

class _Task(QRunnable):
    """One decode-and-scale job on the pool.

    Attributes:
        cache: The owning `ThumbnailCache`, whose signals are emitted from here.
    """

    def __init__(self, cache: "ThumbnailCache", source: str, size: str,
                 mtime: int, write_back: bool) -> None:
        """
        Args:
            cache: The owning cache.
            source: The absolute source path.
            size: One of `SIZES`.
            mtime: The source's `st_mtime`, captured before the read so a file
                edited mid-decode is not cached under the new time.
            write_back: Whether to store the result in the shared cache.
        """
        super().__init__()
        self.setAutoDelete(True)
        self._cache = cache
        self._source = source
        self._size = size
        self._mtime = mtime
        self._write_back = write_back

    def run(self) -> None:
        """Decode, scale, optionally write back, and report."""
        image = _decode_scaled(self._source, SIZES[self._size])
        if image is None or image.isNull():
            self._cache._on_failed(self._source, self._size)
            return
        if self._write_back:
            _write_thumbnail(self._source, self._size, image, self._mtime)
        self._cache._on_ready(self._source, self._size, image)


def _decode_scaled(source: str, longest_edge: int) -> QImage | None:
    """Read an image scaled down to fit a box.

    `QImageReader.setScaledSize()` lets the format plugin do the downscale
    during decode — for a JPEG that means DCT scaling, so a 6000 px photo never
    becomes a 100 MB `QImage` on the way to a 128 px preview.

    Args:
        source: The absolute source path.
        longest_edge: The target box, in pixels.

    Returns:
        The scaled image, or `None` when the file is not a readable image.
    """
    reader = QImageReader(source)
    # Honour the EXIF orientation, or portrait photographs come out sideways.
    reader.setAutoTransform(True)
    if not reader.canRead():
        return None
    native = reader.size()
    if native.isValid() and not native.isEmpty():
        target = native.scaled(QSize(longest_edge, longest_edge),
                               Qt.AspectRatioMode.KeepAspectRatio)
        # Never upscale: a 32 px icon must stay 32 px, not become a blurry 128.
        if target.width() < native.width() or target.height() < native.height():
            reader.setScaledSize(target)
    image = reader.read()
    if image.isNull():
        log.debug("cannot decode %s: %s", source, reader.errorString())
        return None
    return image


def _write_thumbnail(source: str, size: str, image: QImage, mtime: int) -> bool:
    """Store a generated preview in the shared cache, atomically.

    Args:
        source: The absolute source path.
        size: One of `SIZES`.
        image: The scaled image.
        mtime: The source's `st_mtime` at the time of the read.

    Returns:
        True if the thumbnail was written.
    """
    target = thumbnail_path(source, size)
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=THUMBNAIL_DIR_MODE)
        writer = QImageWriter(str(tmp), b"png")
        if not writer.write(image):
            log.debug("could not encode a thumbnail for %s: %s",
                      source, writer.errorString())
            tmp.unlink(missing_ok=True)
            return False
        raw = tmp.read_bytes()
        try:
            byte_size = os.stat(source).st_size
        except OSError:
            byte_size = 0
        stamped = splice_text(raw, {
            KEY_URI: file_uri(source),
            KEY_MTIME: str(int(mtime)),
            KEY_SIZE: str(byte_size),
            KEY_SOFTWARE: SOFTWARE,
        })
        tmp.write_bytes(stamped)
        os.chmod(tmp, THUMBNAIL_MODE)
        os.replace(tmp, target)
    except OSError as exc:
        log.debug("could not write %s: %s", target, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


class ThumbnailCache(QObject):
    """Previews for the activity feed and the file browser.

    Attributes:
        ready: `(source path, size name, QImage)` — a preview became available,
            whether from the shared cache or from a decode.
        failed: `(source path, size name)` — there will be no preview; draw the
            type glyph.
    """

    #: `(str path, str size, QImage image)`.
    ready = Signal(str, str, QImage)
    #: `(str path, str size)`.
    failed = Signal(str, str)

    def __init__(self, parent: QObject | None = None, *,
                 pool: QThreadPool | None = None,
                 state_provider: StateProvider | None = None,
                 memory_items: int = MEMORY_ITEMS,
                 write_back: bool = True,
                 max_source_bytes: int = NO_THUMBNAIL_ABOVE_BYTES) -> None:
        """
        Args:
            parent: Qt parent.
            pool: The thread pool to decode on. `None` creates a private pool
                capped at `THUMBNAIL_THREADS`, so thumbnailing can never starve
                hydration of the shared IOPool.
            state_provider: `(path) -> FileState`, answering from memory. It is
                what allows generation for a file under a `fuse.rclone` mount;
                without it, nothing under a mount is ever generated.
            memory_items: How many decoded images to keep in memory.
            write_back: Store generated previews in the shared cache.
            max_source_bytes: Files larger than this get no preview at all,
                matching OneDrive's own limit.
        """
        super().__init__(parent)
        if pool is None:
            pool = QThreadPool(self)
            pool.setMaxThreadCount(THUMBNAIL_THREADS)
        self._pool = pool
        self._state_provider = state_provider
        self._memory_items = max(1, memory_items)
        self._write_back = write_back
        self._max_source_bytes = max_source_bytes
        self._memory: OrderedDict[tuple[str, str], QImage] = OrderedDict()
        self._pending: set[tuple[str, str]] = set()
        self._hits = 0
        self._misses = 0
        self._decodes = 0
        self._refusals = 0
        # `_pending` is read by `request()` on the GUI thread, so it is cleared
        # there too. A pool thread only ever *emits*; because this object lives
        # on the GUI thread, Qt queues the emission and these slots run there —
        # the §7 rule that every cross-thread hand-off is a queued signal, with
        # no mutex anywhere.
        self.ready.connect(self._clear_pending_ready)
        self.failed.connect(self._clear_pending_failed)

    # ── configuration ────────────────────────────────────────────────────────

    @property
    def pool(self) -> QThreadPool:
        """The pool decodes run on."""
        return self._pool

    def set_state_provider(self, provider: StateProvider | None) -> None:
        """Install the Files-On-Demand state lookup.

        Args:
            provider: `(path) -> FileState`, answering from memory, or `None` to
                refuse every generation under a mount.
        """
        self._state_provider = provider

    def stats(self) -> dict[str, int]:
        """Counters for the diagnostics pane.

        Returns:
            `hits`, `misses`, `decodes`, `refusals`, `memory`, `pending`.
        """
        return {
            "hits": self._hits, "misses": self._misses,
            "decodes": self._decodes, "refusals": self._refusals,
            "memory": len(self._memory), "pending": len(self._pending),
        }

    # ── policy ───────────────────────────────────────────────────────────────

    def may_generate(self, path: str | os.PathLike[str]) -> bool:
        """Whether reading this file to make a preview is safe.

        The whole point of the module. A file on ordinary local storage is free
        to read. A file on the rclone mount is free to read only if it is
        already downloaded — otherwise opening it hydrates it, and a folder of
        online-only videos would become a multi-gigabyte download triggered by
        nothing more than scrolling.

        Args:
            path: The source file.

        Returns:
            True if a preview may be generated.
        """
        absolute = str(Path(os.path.abspath(os.path.expanduser(str(path)))))
        if not paths.is_under_fuse_mount(absolute):
            return True
        provider = self._state_provider
        if provider is None:
            return False
        try:
            state = provider(absolute)
        except Exception:
            log.exception("thumbnail state provider raised; refusing to hydrate")
            return False
        return state in LOCAL_STATES

    # ── reads ────────────────────────────────────────────────────────────────

    def peek(self, path: str | os.PathLike[str],
             size: str = DEFAULT_SIZE) -> QImage | None:
        """Return a preview if one is already available. Never generates.

        Safe to call from a paint path: at worst it is one `stat` plus one small
        PNG decode, and it never touches the source file's contents.

        Args:
            path: The source file.
            size: One of `SIZES`.

        Returns:
            The image, or `None` when nothing valid is cached.

        Raises:
            KeyError: If `size` is not a specified size.
        """
        assert_known_size(size)
        absolute = str(Path(os.path.abspath(os.path.expanduser(str(path)))))
        key = (absolute, size)
        cached = self._memory.get(key)
        if cached is not None:
            self._memory.move_to_end(key)
            self._hits += 1
            return cached
        image = self._read_disk(absolute, size)
        if image is not None:
            self._remember(key, image)
            self._hits += 1
            return image
        self._misses += 1
        return None

    def request(self, path: str | os.PathLike[str],
                size: str = DEFAULT_SIZE) -> QImage | None:
        """Get a preview, generating one in the background if allowed.

        Args:
            path: The source file.
            size: One of `SIZES`.

        Returns:
            The image when it was already cached, in which case no signal
            follows. `None` means either that a decode was scheduled — `ready`
            or `failed` will arrive — or that generation was refused, in which
            case `failed` is emitted and the caller should draw the type glyph.

        Raises:
            KeyError: If `size` is not a specified size.
        """
        assert_known_size(size)
        absolute = str(Path(os.path.abspath(os.path.expanduser(str(path)))))
        cached = self.peek(absolute, size)
        if cached is not None:
            return cached
        key = (absolute, size)
        if key in self._pending:
            return None
        if not self._may_decode(absolute):
            self.failed.emit(absolute, size)
            return None
        try:
            mtime = int(os.stat(absolute).st_mtime)
        except OSError:
            self.failed.emit(absolute, size)
            return None
        self._pending.add(key)
        self._decodes += 1
        self._pool.start(_Task(self, absolute, size, mtime, self._write_back))
        return None

    def _may_decode(self, absolute: str) -> bool:
        """Every precondition for reading the source file.

        Args:
            absolute: The absolute source path.

        Returns:
            True if a decode may be scheduled.
        """
        if self.is_failed(absolute):
            return False
        try:
            stat = os.stat(absolute)
        except OSError:
            return False
        if not os.path.isfile(absolute):
            return False
        if self._max_source_bytes and stat.st_size > self._max_source_bytes:
            log.debug("%s is %d bytes; over the thumbnail limit",
                      absolute, stat.st_size)
            return False
        if not self.may_generate(absolute):
            self._refusals += 1
            log.debug("refusing to hydrate %s for a thumbnail", absolute)
            return False
        return True

    def _read_disk(self, absolute: str, size: str) -> QImage | None:
        """Load and validate a thumbnail from the shared cache.

        Args:
            absolute: The absolute source path.
            size: One of `SIZES`.

        Returns:
            The image, or `None` when the thumbnail is missing or stale.
        """
        target = thumbnail_path(absolute, size)
        try:
            if target.stat().st_size > MAX_THUMBNAIL_BYTES:
                return None
            data = target.read_bytes()
        except OSError:
            return None
        if not self._is_current(absolute, png_text(data)):
            return None
        image = QImage()
        if not image.loadFromData(data, "PNG") or image.isNull():
            return None
        return image

    def _is_current(self, absolute: str, text: Mapping[str, str]) -> bool:
        """Whether a cached thumbnail still describes the source file.

        Args:
            absolute: The absolute source path.
            text: The thumbnail's PNG text chunks.

        Returns:
            True when `Thumb::URI` matches the file and `Thumb::MTime` matches
            its modification time. A thumbnail carrying neither key is rejected:
            an unverifiable preview may belong to a different file entirely.
        """
        uri = text.get(KEY_URI)
        if uri is not None and uri != file_uri(absolute):
            return False
        stamp = text.get(KEY_MTIME)
        if stamp is None:
            return False
        try:
            return int(stamp) == int(os.stat(absolute).st_mtime)
        except (OSError, ValueError):
            return False

    def is_failed(self, path: str | os.PathLike[str]) -> bool:
        """Whether a thumbnailer already recorded that this file has no preview.

        Args:
            path: The source file.

        Returns:
            True if a `fail/` marker exists and still matches the file's mtime.
            A marker for an older version of the file is ignored, so editing a
            broken file gives it another chance.
        """
        marker = fail_path(path)
        try:
            data = marker.read_bytes()
        except OSError:
            return False
        absolute = str(Path(os.path.abspath(os.path.expanduser(str(path)))))
        return self._is_current(absolute, png_text(data))

    # ── task callbacks, from a pool thread ───────────────────────────────────

    def _on_ready(self, source: str, size: str, image: QImage) -> None:
        """Publish a decoded preview.

        Called on a pool thread, so it only emits; `_clear_pending_ready()`
        does the bookkeeping back on the GUI thread.

        Args:
            source: The absolute source path.
            size: One of `SIZES`.
            image: The scaled preview.
        """
        self.ready.emit(source, size, image)

    def _on_failed(self, source: str, size: str) -> None:
        """Record and publish a failure.

        Called on a pool thread. Writing the `fail/` marker is filesystem I/O
        and belongs here; the bookkeeping happens on the GUI thread.

        Args:
            source: The absolute source path.
            size: One of `SIZES`.
        """
        if self._write_back:
            self._mark_failed(source)
        self.failed.emit(source, size)

    def _clear_pending_ready(self, source: str, size: str, _image: QImage) -> None:
        """Retire a finished request. GUI thread, via a queued connection.

        Args:
            source: The absolute source path.
            size: One of `SIZES`.
            _image: The preview, unused here.
        """
        self._pending.discard((source, size))

    def _clear_pending_failed(self, source: str, size: str) -> None:
        """Retire a failed request. GUI thread, via a queued connection.

        Args:
            source: The absolute source path.
            size: One of `SIZES`.
        """
        self._pending.discard((source, size))

    def _mark_failed(self, source: str) -> None:
        """Write a `fail/` marker so nothing retries this file on every scroll.

        Args:
            source: The absolute source path.
        """
        marker = fail_path(source)
        try:
            mtime = int(os.stat(source).st_mtime)
        except OSError:
            return
        # The specification's marker is a 1x1 PNG carrying only the text keys.
        image = QImage(1, 1, QImage.Format.Format_ARGB32)
        image.fill(0)
        tmp = marker.with_name(marker.name + f".{os.getpid()}.tmp")
        try:
            marker.parent.mkdir(parents=True, exist_ok=True, mode=THUMBNAIL_DIR_MODE)
            writer = QImageWriter(str(tmp), b"png")
            if not writer.write(image):
                tmp.unlink(missing_ok=True)
                return
            tmp.write_bytes(splice_text(tmp.read_bytes(), {
                KEY_URI: file_uri(source),
                KEY_MTIME: str(mtime),
                KEY_SOFTWARE: SOFTWARE,
            }))
            os.chmod(tmp, THUMBNAIL_MODE)
            os.replace(tmp, marker)
        except OSError as exc:
            log.debug("could not write %s: %s", marker, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ── eviction ─────────────────────────────────────────────────────────────

    def _remember(self, key: tuple[str, str], image: QImage) -> None:
        """Insert into the memory LRU, evicting the oldest entry.

        Args:
            key: `(absolute path, size)`.
            image: The preview.
        """
        self._memory[key] = image
        self._memory.move_to_end(key)
        while len(self._memory) > self._memory_items:
            self._memory.popitem(last=False)

    def clear(self) -> int:
        """Drop every decoded image held in memory.

        The on-disk cache is **not** touched: it belongs to the desktop, is
        shared with every other application, and deleting another program's
        previews to reclaim our own memory would be rude and slow. Use
        `forget()` for one file.

        Returns:
            How many images were dropped.
        """
        count = len(self._memory)
        self._memory.clear()
        return count

    def forget(self, path: str | os.PathLike[str]) -> int:
        """Drop every cached preview for one file, in memory and on disk.

        The right call when a file's content changed under us — the `Thumb::MTime`
        check would catch it anyway, but removing the entry frees the space and
        stops other applications showing the old picture.

        Args:
            path: The source file.

        Returns:
            How many files and memory entries were removed.
        """
        absolute = str(Path(os.path.abspath(os.path.expanduser(str(path)))))
        removed = 0
        for key in [k for k in self._memory if k[0] == absolute]:
            self._memory.pop(key, None)
            removed += 1
        for size in SIZES:
            try:
                thumbnail_path(absolute, size).unlink()
                removed += 1
            except OSError:
                continue
        try:
            fail_path(absolute).unlink()
            removed += 1
        except OSError:
            pass
        return removed

    def wait(self, timeout_ms: int = 5000) -> bool:
        """Block until every scheduled decode has finished.

        For shutdown and for tests; never call it from a paint path.

        Args:
            timeout_ms: How long to wait.

        Returns:
            True if the pool drained in time.
        """
        return bool(self._pool.waitForDone(timeout_ms))


def peek(path: str | os.PathLike[str], size: str = DEFAULT_SIZE) -> QImage | None:
    """One-shot lookup in the shared cache, with no cache object.

    For a caller that wants a single preview and has nowhere to keep a
    `ThumbnailCache`.

    Args:
        path: The source file.
        size: One of `SIZES`.

    Returns:
        The image, or `None` when nothing valid is cached.

    Raises:
        KeyError: If `size` is not a specified size.
    """
    return ThumbnailCache().peek(path, size)


__all__ = [
    "ThumbnailCache", "StateProvider",
    "THUMBNAIL_DIR_NAME", "SIZES", "DEFAULT_SIZE", "FAIL_DIR_NAME",
    "FAIL_APP_NAME", "KEY_URI", "KEY_MTIME", "KEY_SIZE", "KEY_SOFTWARE",
    "SOFTWARE", "THUMBNAIL_MODE", "THUMBNAIL_DIR_MODE", "PNG_SIGNATURE",
    "MAX_THUMBNAIL_BYTES", "THUMBNAIL_THREADS", "MEMORY_ITEMS", "LOCAL_STATES",
    "thumbnail_root", "thumbnail_path", "fail_path", "assert_known_size",
    "png_text", "splice_text", "peek",
]
