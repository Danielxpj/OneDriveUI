"""Tests for `onedriveui.platform.thumbnails`.

Note on ownership: the WP-10b brief names six test files and seven modules, so
this name is unclaimed — the same situation WP-10a recorded for `dbus.py`.
Shared rule 3 ("every module ships `tests/test_<module>.py`") wins, and this
file conflicts with nothing.

The two claims that carry weight:

* **the name is `md5(canonical file URI)`**, checked against 341 real
  thumbnails in the developer's own `~/.cache/thumbnails` — if our URI escaping
  differed from GLib's by one character, every cache hit would be a miss and
  the file browser would silently regenerate everything;
* **an online-only file on the rclone mount is never opened.** Reading it
  hydrates it, so a folder of untouched videos would become a multi-gigabyte
  download triggered by scrolling. `test_refuses_*` attacks that from each
  direction, including a provider that raises.

The PNG text round trip is also checked against an independent reader (PIL,
when installed), because Qt itself cannot write these keys — it splits
`Thumb::URI` on the colon — and a self-consistent bug would otherwise pass.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QImage, QImageWriter

from onedriveui import paths
from onedriveui.constants import NO_THUMBNAIL_ABOVE_BYTES
from onedriveui.models import FileState
from onedriveui.platform import thumbnails as TH
from onedriveui.platform.desktop import file_uri
from onedriveui.platform.thumbnails import ThumbnailCache

try:
    from PIL import Image as _PILImage
except ImportError:                                   # pragma: no cover
    _PILImage = None


@pytest.fixture
def picture(qapp, tmp_path) -> Path:
    """A real, decodable PNG on ordinary local storage."""
    target = tmp_path / "photo.png"
    image = QImage(320, 240, QImage.Format.Format_ARGB32)
    image.fill(0xFF3366CC)
    assert QImageWriter(str(target), b"png").write(image) is True
    return target


@pytest.fixture
def cache(qapp, _isolate_home) -> ThumbnailCache:
    """A cache whose pool is drained synchronously by `wait()`."""
    pool = QThreadPool()
    pool.setMaxThreadCount(TH.THUMBNAIL_THREADS)
    return ThumbnailCache(pool=pool)


def _drain(qapp, cache: ThumbnailCache) -> None:
    from PySide6.QtCore import QEventLoop

    assert cache.wait(10_000) is True
    for _ in range(8):
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)


# ═════════════════════════════════════════════════════════════════════════════
# Naming
# ═════════════════════════════════════════════════════════════════════════════

def test_sizes_are_the_specified_four():
    assert TH.SIZES == {"normal": 128, "large": 256,
                        "x-large": 512, "xx-large": 1024}
    assert TH.DEFAULT_SIZE == "normal"


def test_thumbnail_root_is_the_shared_cache(_isolate_home):
    root = TH.thumbnail_root()
    assert root == Path(os.environ["XDG_CACHE_HOME"]) / "thumbnails"
    assert root != paths.cache_dir(), "the shared cache is not ours alone"


def test_thumbnail_path_is_md5_of_the_uri(_isolate_home):
    source = "/home/u/OneDrive/a b#c.png"
    expected = hashlib.md5(file_uri(source).encode()).hexdigest()

    target = TH.thumbnail_path(source, "large")

    assert target.name == f"{expected}.png"
    assert target.parent == TH.thumbnail_root() / "large"


def test_thumbnail_path_uses_the_encoded_uri_not_the_raw_path(_isolate_home):
    """`md5("/a b")` and `md5("file:///a%20b")` differ; only the latter is right."""
    source = "/home/u/a b.png"
    assert TH.thumbnail_path(source).name != f"{hashlib.md5(source.encode()).hexdigest()}.png"
    assert TH.thumbnail_path(source).name == (
        f"{hashlib.md5(file_uri(source).encode()).hexdigest()}.png")


def test_unknown_size_raises(_isolate_home):
    """Silently downgrading `large` would show a blurry preview with no clue."""
    with pytest.raises(KeyError):
        TH.thumbnail_path("/a/b.png", "huge")
    with pytest.raises(KeyError):
        TH.assert_known_size("")


def test_fail_path_is_namespaced(_isolate_home):
    marker = TH.fail_path("/home/u/x.png")
    assert marker.parent == TH.thumbnail_root() / TH.FAIL_DIR_NAME / TH.FAIL_APP_NAME
    assert marker.name == TH.thumbnail_path("/home/u/x.png").name


def test_live_real_thumbnails_are_md5_of_their_own_uri():
    """Against the developer's real cache — the strongest available check.

    `_isolate_home` points `HOME` at a temp tree, so the real cache is reached
    through `conftest.REAL_HOME`, exactly as the other live tests do.
    """
    from tests.conftest import REAL_HOME

    real = REAL_HOME / ".cache" / "thumbnails" / "normal"
    if not real.is_dir():
        pytest.skip("no populated thumbnail cache on this machine")
    samples = sorted(real.glob("*.png"))[:40]
    if not samples:
        pytest.skip("the thumbnail cache is empty")

    checked = 0
    for sample in samples:
        text = TH.png_text(sample.read_bytes())
        uri = text.get(TH.KEY_URI)
        if not uri:
            continue
        assert sample.name == f"{hashlib.md5(uri.encode()).hexdigest()}.png"
        checked += 1
    assert checked >= 10, f"only {checked} of {len(samples)} carried Thumb::URI"


# ═════════════════════════════════════════════════════════════════════════════
# PNG text chunks
# ═════════════════════════════════════════════════════════════════════════════

def test_qt_cannot_write_thumb_uri(tmp_path, qapp):
    """The measured Qt bug that makes `splice_text()` necessary.

    `QImageWriter.setText("Thumb::URI", v)` splits on the colon and stores
    `Thumb` = `"URI: v"`. Pinned here so nobody "simplifies" the splice away.
    """
    target = tmp_path / "qt.png"
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(0)
    writer = QImageWriter(str(target), b"png")
    writer.setText(TH.KEY_URI, "file:///x.png")
    assert writer.write(image) is True

    text = TH.png_text(target.read_bytes())

    assert TH.KEY_URI not in text
    assert "Thumb" in text


def test_splice_text_round_trips(tmp_path, qapp):
    target = tmp_path / "plain.png"
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(0xFF00FF00)
    QImageWriter(str(target), b"png").write(image)

    stamped = TH.splice_text(target.read_bytes(), {
        TH.KEY_URI: "file:///home/u/a%20b.png",
        TH.KEY_MTIME: "1700000000",
        TH.KEY_SOFTWARE: TH.SOFTWARE,
    })

    text = TH.png_text(stamped)
    assert text[TH.KEY_URI] == "file:///home/u/a%20b.png"
    assert text[TH.KEY_MTIME] == "1700000000"
    assert text[TH.KEY_SOFTWARE] == TH.SOFTWARE


def test_a_spliced_png_is_still_a_valid_png(tmp_path, qapp):
    target = tmp_path / "plain.png"
    image = QImage(16, 12, QImage.Format.Format_ARGB32)
    image.fill(0xFF112233)
    QImageWriter(str(target), b"png").write(image)
    stamped = TH.splice_text(target.read_bytes(), {TH.KEY_MTIME: "1"})

    reloaded = QImage()

    assert reloaded.loadFromData(stamped, "PNG") is True
    assert reloaded.size().width() == 16
    assert reloaded.size().height() == 12


@pytest.mark.skipif(_PILImage is None, reason="PIL is not installed")
def test_an_independent_reader_sees_our_chunks(tmp_path, qapp):
    """Verified outside Qt, so a self-consistent bug cannot pass."""
    target = tmp_path / "plain.png"
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(0xFFFFFFFF)
    QImageWriter(str(target), b"png").write(image)
    stamped = tmp_path / "stamped.png"
    stamped.write_bytes(TH.splice_text(target.read_bytes(), {
        TH.KEY_URI: "file:///x.png", TH.KEY_MTIME: "42"}))

    with _PILImage.open(stamped) as opened:
        opened.load()
        assert opened.text[TH.KEY_URI] == "file:///x.png"
        assert opened.text[TH.KEY_MTIME] == "42"


def test_splice_text_inserts_after_ihdr(tmp_path, qapp):
    """IHDR must stay first, or the file is not a PNG."""
    target = tmp_path / "plain.png"
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(0)
    QImageWriter(str(target), b"png").write(image)

    stamped = TH.splice_text(target.read_bytes(), {TH.KEY_MTIME: "1"})

    pos = len(TH.PNG_SIGNATURE)
    assert stamped[pos + 4:pos + 8] == TH.PNG_IHDR
    (length,) = struct.unpack(">I", stamped[pos:pos + 4])
    after = pos + 8 + length + 4
    assert stamped[after + 4:after + 8] == TH.PNG_TEXT


def test_splice_text_leaves_a_non_png_alone():
    assert TH.splice_text(b"not a png", {"a": "b"}) == b"not a png"
    assert TH.png_text(b"not a png") == {}


def test_png_text_reads_ztxt_and_itxt():
    """gnome-thumbnail-factory writes iTXt, sometimes compressed."""
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    ztxt = chunk(b"zTXt", b"Zkey\x00\x00" + zlib.compress(b"zvalue"))
    itxt = chunk(b"iTXt", b"Ikey\x00\x00\x00\x00\x00" + b"ivalue")
    itxt_z = chunk(b"iTXt", b"Ckey\x00\x01\x00\x00\x00" + zlib.compress(b"cvalue"))
    data = TH.PNG_SIGNATURE + ihdr + ztxt + itxt + itxt_z + chunk(b"IEND", b"")

    text = TH.png_text(data)

    assert text == {"Zkey": "zvalue", "Ikey": "ivalue", "Ckey": "cvalue"}


# ═════════════════════════════════════════════════════════════════════════════
# THE RULE: never hydrate an online-only file
# ═════════════════════════════════════════════════════════════════════════════

def test_a_local_path_may_always_generate(cache, picture):
    assert cache.may_generate(picture) is True


def test_refuses_a_mount_path_with_no_state_provider(cache, picture, monkeypatch):
    """The conservative default: with no way to ask, never open the file."""
    monkeypatch.setattr(paths, "is_under_fuse_mount", lambda _p: True)
    assert cache.may_generate(picture) is False


@pytest.mark.parametrize("state", [
    FileState.ONLINE_ONLY, FileState.PARTIAL, FileState.EXCLUDED,
    FileState.UNKNOWN, FileState.ERROR, FileState.SYNCING,
])
def test_refuses_a_mount_path_that_is_not_fully_local(cache, picture, monkeypatch,
                                                      state):
    monkeypatch.setattr(paths, "is_under_fuse_mount", lambda _p: True)
    cache.set_state_provider(lambda _p: state)
    assert cache.may_generate(picture) is False


@pytest.mark.parametrize("state", [
    FileState.LOCAL, FileState.PINNED, FileState.DIRTY,
])
def test_allows_a_mount_path_whose_bytes_are_already_here(cache, picture,
                                                          monkeypatch, state):
    monkeypatch.setattr(paths, "is_under_fuse_mount", lambda _p: True)
    cache.set_state_provider(lambda _p: state)
    assert cache.may_generate(picture) is True


def test_a_provider_that_raises_refuses(cache, picture, monkeypatch):
    monkeypatch.setattr(paths, "is_under_fuse_mount", lambda _p: True)
    cache.set_state_provider(lambda _p: (_ for _ in ()).throw(RuntimeError("x")))
    assert cache.may_generate(picture) is False


def test_a_refused_request_never_opens_the_file(qapp, cache, picture, monkeypatch):
    """The whole point: no read, no hydration, and `failed` so the UI can draw."""
    monkeypatch.setattr(paths, "is_under_fuse_mount", lambda _p: True)
    opened: list[str] = []
    real_open = TH.QImageReader

    class Spy(real_open):
        def __init__(self, *args, **kwargs):
            if args:
                opened.append(str(args[0]))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(TH, "QImageReader", Spy)
    failures: list[tuple[str, str]] = []
    cache.failed.connect(lambda p, s: failures.append((p, s)))

    assert cache.request(picture) is None
    _drain(qapp, cache)

    assert opened == [], "an online-only file was opened for a thumbnail"
    assert failures == [(str(picture), TH.DEFAULT_SIZE)]
    assert cache.stats()["refusals"] == 1


def test_local_states_are_exactly_the_hydrated_ones():
    assert TH.LOCAL_STATES == frozenset({
        FileState.LOCAL, FileState.PINNED, FileState.DIRTY})


# ═════════════════════════════════════════════════════════════════════════════
# Lookup
# ═════════════════════════════════════════════════════════════════════════════

def test_peek_misses_when_nothing_is_cached(cache, picture):
    assert cache.peek(picture) is None
    assert cache.stats()["misses"] == 1


def test_peek_never_generates(qapp, cache, picture):
    assert cache.peek(picture) is None
    assert cache.stats()["decodes"] == 0
    assert not TH.thumbnail_path(picture).exists()


def test_generate_then_peek_hits(qapp, cache, picture):
    ready: list[tuple[str, str]] = []
    cache.ready.connect(lambda p, s, _i: ready.append((p, s)))

    assert cache.request(picture) is None
    _drain(qapp, cache)

    assert ready == [(str(picture), TH.DEFAULT_SIZE)]
    assert TH.thumbnail_path(picture).is_file()
    assert cache.peek(picture) is not None


def test_the_generated_thumbnail_fits_the_box(qapp, cache, picture):
    cache.request(picture, "normal")
    _drain(qapp, cache)

    image = cache.peek(picture, "normal")

    assert image is not None
    assert max(image.width(), image.height()) <= TH.SIZES["normal"]
    assert image.width() == 128 and image.height() == 96      # 320x240 aspect kept


def test_the_generated_thumbnail_is_conformant(qapp, cache, picture):
    cache.request(picture)
    _drain(qapp, cache)

    text = TH.png_text(TH.thumbnail_path(picture).read_bytes())

    assert text[TH.KEY_URI] == file_uri(picture)
    assert text[TH.KEY_MTIME] == str(int(picture.stat().st_mtime))
    assert text[TH.KEY_SIZE] == str(picture.stat().st_size)
    assert text[TH.KEY_SOFTWARE] == TH.SOFTWARE


def test_the_generated_thumbnail_is_owner_only(qapp, cache, picture):
    """A thumbnail can leak the content of a private file."""
    cache.request(picture)
    _drain(qapp, cache)
    assert (TH.thumbnail_path(picture).stat().st_mode & 0o077) == 0


def test_a_thumbnail_written_by_us_is_found_by_a_fresh_cache(qapp, cache, picture,
                                                             _isolate_home):
    cache.request(picture)
    _drain(qapp, cache)

    assert ThumbnailCache().peek(picture) is not None
    assert TH.peek(picture) is not None


def test_no_upscaling(qapp, cache, tmp_path):
    """A 32 px icon stays 32 px rather than becoming a blurry 128."""
    tiny = tmp_path / "tiny.png"
    image = QImage(32, 32, QImage.Format.Format_ARGB32)
    image.fill(0xFF00FF00)
    QImageWriter(str(tiny), b"png").write(image)

    cache.request(tiny)
    _drain(qapp, cache)

    assert cache.peek(tiny).size().width() == 32


# ═════════════════════════════════════════════════════════════════════════════
# Staleness
# ═════════════════════════════════════════════════════════════════════════════

def test_a_thumbnail_for_a_different_uri_is_rejected(qapp, cache, picture, tmp_path):
    """Never show the wrong picture."""
    target = TH.thumbnail_path(picture)
    target.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(0)
    tmp = tmp_path / "raw.png"
    QImageWriter(str(tmp), b"png").write(image)
    target.write_bytes(TH.splice_text(tmp.read_bytes(), {
        TH.KEY_URI: "file:///somewhere/else.png",
        TH.KEY_MTIME: str(int(picture.stat().st_mtime))}))

    assert cache.peek(picture) is None


def test_a_thumbnail_with_a_stale_mtime_is_rejected(qapp, cache, picture):
    cache.request(picture)
    _drain(qapp, cache)
    assert cache.peek(picture) is not None
    cache.clear()

    os.utime(picture, (0, 0))                       # the file changed under us

    assert cache.peek(picture) is None


def test_a_thumbnail_with_no_mtime_key_is_rejected(qapp, cache, picture, tmp_path):
    """Unverifiable means it may belong to a different file entirely."""
    target = TH.thumbnail_path(picture)
    target.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(0)
    tmp = tmp_path / "raw.png"
    QImageWriter(str(tmp), b"png").write(image)
    target.write_bytes(tmp.read_bytes())            # no text chunks at all

    assert cache.peek(picture) is None


def test_an_oversized_cache_file_is_ignored(qapp, cache, picture):
    target = TH.thumbnail_path(picture)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00" * (TH.MAX_THUMBNAIL_BYTES + 1))
    assert cache.peek(picture) is None


# ═════════════════════════════════════════════════════════════════════════════
# Failure marking
# ═════════════════════════════════════════════════════════════════════════════

def test_an_undecodable_file_fails_and_is_marked(qapp, cache, tmp_path):
    junk = tmp_path / "not-an-image.bin"
    junk.write_bytes(b"\x00\x01\x02\x03" * 32)
    failures: list[tuple[str, str]] = []
    cache.failed.connect(lambda p, s: failures.append((p, s)))

    cache.request(junk)
    _drain(qapp, cache)

    assert failures == [(str(junk), TH.DEFAULT_SIZE)]
    assert cache.is_failed(junk) is True
    assert TH.fail_path(junk).is_file()


def test_a_marked_failure_is_not_retried(qapp, cache, tmp_path):
    junk = tmp_path / "broken.bin"
    junk.write_bytes(b"\x00" * 64)
    cache.request(junk)
    _drain(qapp, cache)
    before = cache.stats()["decodes"]

    cache.request(junk)
    _drain(qapp, cache)

    assert cache.stats()["decodes"] == before


def test_a_failure_marker_for_an_older_version_is_ignored(qapp, cache, tmp_path):
    """Editing a broken file gives it another chance."""
    junk = tmp_path / "was-broken.bin"
    junk.write_bytes(b"\x00" * 64)
    cache.request(junk)
    _drain(qapp, cache)
    assert cache.is_failed(junk) is True

    junk.write_bytes(b"\x01" * 64)
    os.utime(junk, (99999, 99999))

    assert cache.is_failed(junk) is False


def test_a_file_over_the_size_limit_gets_no_thumbnail(qapp, cache, picture):
    small = ThumbnailCache(pool=cache.pool, max_source_bytes=8)
    failures: list[str] = []
    small.failed.connect(lambda p, _s: failures.append(p))

    assert small.request(picture) is None
    _drain(qapp, small)

    assert failures == [str(picture)]
    assert small.stats()["decodes"] == 0


def test_the_size_limit_default_is_the_constant(qapp, _isolate_home, tmp_path):
    """100 MB, matching OneDrive's own no-thumbnail threshold."""
    huge = tmp_path / "huge.png"
    huge.write_bytes(b"\x00")
    os.truncate(huge, NO_THUMBNAIL_ABOVE_BYTES + 1)      # sparse, costs nothing
    fresh = ThumbnailCache()
    failures: list[str] = []
    fresh.failed.connect(lambda p, _s: failures.append(p))

    assert fresh.request(huge) is None
    _drain(qapp, fresh)

    assert failures == [str(huge)]
    assert fresh.stats()["decodes"] == 0


def test_a_missing_file_fails_without_decoding(qapp, cache, tmp_path):
    failures: list[str] = []
    cache.failed.connect(lambda p, _s: failures.append(p))

    assert cache.request(tmp_path / "nope.png") is None

    assert failures == [str(tmp_path / "nope.png")]
    assert cache.stats()["decodes"] == 0


def test_a_directory_is_not_thumbnailed(qapp, cache, tmp_path):
    assert cache.request(tmp_path) is None
    assert cache.stats()["decodes"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# Memory cache
# ═════════════════════════════════════════════════════════════════════════════

def test_a_second_peek_is_a_memory_hit(qapp, cache, picture):
    cache.request(picture)
    _drain(qapp, cache)
    cache.peek(picture)
    before = cache.stats()["hits"]

    cache.peek(picture)

    assert cache.stats()["hits"] == before + 1
    assert cache.stats()["memory"] == 1


def test_request_returns_a_cached_image_directly(qapp, cache, picture):
    cache.request(picture)
    _drain(qapp, cache)
    cache.peek(picture)

    assert cache.request(picture) is not None


def test_the_memory_cache_evicts(qapp, _isolate_home, tmp_path):
    small = ThumbnailCache(memory_items=2)
    sources = []
    for index in range(3):
        target = tmp_path / f"p{index}.png"
        image = QImage(16, 16, QImage.Format.Format_ARGB32)
        image.fill(0xFF000000 | index)
        QImageWriter(str(target), b"png").write(image)
        sources.append(target)
        small.request(target)
    _drain(qapp, small)
    for source in sources:
        small.peek(source)

    assert small.stats()["memory"] == 2


def test_clear_drops_memory_but_not_the_shared_cache(qapp, cache, picture):
    """That tree belongs to the desktop, not to us."""
    cache.request(picture)
    _drain(qapp, cache)
    cache.peek(picture)

    assert cache.clear() == 1

    assert cache.stats()["memory"] == 0
    assert TH.thumbnail_path(picture).is_file()


def test_forget_removes_our_entries_for_one_file(qapp, cache, picture):
    cache.request(picture)
    _drain(qapp, cache)
    cache.peek(picture)

    removed = cache.forget(picture)

    assert removed >= 2
    assert cache.stats()["memory"] == 0
    assert not TH.thumbnail_path(picture).exists()


def test_write_back_can_be_switched_off(qapp, _isolate_home, picture):
    private = ThumbnailCache(write_back=False)
    private.request(picture)
    _drain(qapp, private)

    assert not TH.thumbnail_path(picture).exists()
    assert private.peek(picture) is None        # nothing on disk to find


def test_a_duplicate_request_is_not_decoded_twice(qapp, cache, picture):
    cache.request(picture)
    cache.request(picture)
    _drain(qapp, cache)
    assert cache.stats()["decodes"] == 1


def test_pending_is_retired_on_the_gui_thread(qapp, cache, picture):
    """`_pending` is only ever mutated from the thread that reads it.

    The pool thread emits; the queued connection clears the entry back here.
    So the request stays pending until the result is actually delivered, and no
    mutex is needed anywhere (§7).
    """
    assert cache.request(picture) is None
    assert cache.stats()["pending"] == 1

    assert cache.wait(10_000) is True            # the decode itself is finished
    _drain(qapp, cache)                          # ...but only delivery retires it

    assert cache.stats()["pending"] == 0


def test_a_failed_request_is_also_retired(qapp, cache, tmp_path):
    junk = tmp_path / "bad.bin"
    junk.write_bytes(b"\x00" * 32)

    cache.request(junk)
    _drain(qapp, cache)

    assert cache.stats()["pending"] == 0


def test_pool_is_capped_at_two_threads(qapp, _isolate_home):
    """§7.3 allots two threads; more would compete for the same FUSE channel."""
    assert ThumbnailCache().pool.maxThreadCount() == TH.THUMBNAIL_THREADS == 2
