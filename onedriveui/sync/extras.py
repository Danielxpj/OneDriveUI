"""The two conveniences Windows offers, done the way Linux does them.

**"Save screenshots to OneDrive."** Windows hooks PrtScn. GNOME already owns
that key and saves to ``~/Pictures/Screenshots``, so hooking it would fight the
desktop and lose. Instead the folder is watched, and a new screenshot is moved
into OneDrive after it has finished being written — which is the part that
matters, because a screenshot tool writes its file in pieces and moving it
mid-write uploads a truncated PNG.

**"Import photos from a camera."** Windows watches for removable media. Here
that is ``GVolumeMonitor``, which reports a mount the moment the user plugs a
phone or an SD card in. The import is **copy, verify, then optionally remove** —
never a move, because the source is removable and can be unplugged mid-operation,
and a half-moved photo library is not recoverable from either end.

Both are off by default. A client that started copying the user's camera roll
into their cloud storage the first time they plugged a phone in would be doing
something they did not ask for with data they may not want uploaded.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui.models import AccountInfo, ActivityVerb, KfmFolder
from onedriveui.platform import desktop

log = logging.getLogger(__name__)

__all__ = ["ScreenshotWatcher", "CameraImporter", "screenshots_dir",
           "SETTLE_MS", "IMAGE_SUFFIXES"]

#: How long a new file must stop changing before it is treated as complete. A
#: screenshot tool writes its PNG in pieces; moving it mid-write uploads a
#: truncated image, and the user's only clue is that the thumbnail is grey.
SETTLE_MS: Final = 1500

#: What counts as an importable picture or video.
IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset({
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif",
    ".tiff", ".dng", ".cr2", ".nef", ".arw", ".raf", ".orf",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp",
})


def screenshots_dir() -> Path:
    """Where this desktop puts screenshots.

    GNOME's own location, from ``user-dirs.dirs`` rather than hardcoded: a user
    who has moved ``~/Pictures`` — including one who moved it into OneDrive with
    KFM — must not have their screenshots watched at the old path.
    """
    return desktop.user_dir(KfmFolder.PICTURES) / "Screenshots"


class ScreenshotWatcher(QObject):
    """Moves new screenshots into OneDrive once they have finished being written.

    Args:
        account: The account.
        source: The folder to watch. Defaults to :func:`screenshots_dir`.
        destination: Where they go, relative to the sync root.
        activity: The :class:`~onedriveui.sync.activity.ActivityFeed`.
        monotonic: The clock, injected for tests.
        parent: Qt parent.

    Signals:
        captured: The relative path of a screenshot that was moved in.
    """

    captured = Signal(str)

    def __init__(
        self,
        account: AccountInfo,
        *,
        source: Path | None = None,
        destination: str = "Pictures/Screenshots",
        activity: Any = None,
        monotonic: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._source = Path(source) if source else None
        self._destination = destination
        self._activity = activity
        self._monotonic = monotonic or time.monotonic
        self._monitor: Any = None
        #: Files seen but not yet settled: ``{path: (size, first_seen)}``.
        self._settling: dict[Path, tuple[int, float]] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(SETTLE_MS)
        self._timer.timeout.connect(self._settle)

    @property
    def source(self) -> Path:
        return self._source or screenshots_dir()

    def start(self) -> bool:
        """Begin watching. Returns True when a monitor was established."""
        folder = self.source
        if not folder.is_dir():
            log.info("no screenshots folder at %s yet", folder)
            return False
        try:
            from gi.repository import Gio

            self._monitor = Gio.File.new_for_path(str(folder)).monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_change)
        except Exception:  # noqa: BLE001
            log.warning("could not watch %s", folder, exc_info=True)
            return False
        self._timer.start()
        log.info("watching %s for screenshots", folder)
        return True

    def stop(self) -> None:
        self._timer.stop()
        if self._monitor is not None:
            try:
                self._monitor.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._monitor = None

    def _on_change(self, _monitor: Any, file: Any, _other: Any,
                   _event: Any) -> None:  # pragma: no cover - needs GLib
        path = file.get_path()
        if path:
            self.note(Path(path))

    def note(self, path: Path) -> None:
        """Record a candidate file. It moves once it stops growing."""
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        self._settling[path] = (size, self._monotonic())
        if not self._timer.isActive():
            self._timer.start()

    def _settle(self) -> None:
        """Move everything that has stopped changing.

        Size-stability rather than a fixed delay, because a screenshot of a 4K
        display takes noticeably longer to write than one of a dialog box, and a
        delay long enough for the first is an irritating pause for the second.
        """
        now = self._monotonic()
        for path, (size, seen) in list(self._settling.items()):
            try:
                current = path.stat().st_size
            except OSError:
                self._settling.pop(path, None)
                continue
            if current != size:
                self._settling[path] = (current, now)
                continue
            if (now - seen) * 1000.0 < SETTLE_MS:
                continue
            self._settling.pop(path, None)
            self._move(path)
        if not self._settling:
            self._timer.stop()

    def _move(self, path: Path) -> None:
        target = (Path(self.account.sync_root).expanduser()
                  / self._destination / path.name)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
        except OSError:
            log.warning("could not move the screenshot %s", path, exc_info=True)
            return
        rel = f"{self._destination}/{path.name}"
        log.info("moved a screenshot into OneDrive: %s", rel)
        self.captured.emit(rel)
        if self._activity is not None:
            self._activity.record(rel, ActivityVerb.CREATED,
                                  size=_size_of(target))


class CameraImporter(QObject):
    """Copies pictures off removable media. **Copy and verify — never move.**

    Args:
        account: The account.
        destination: Where imports go, relative to the sync root.
        activity: The :class:`~onedriveui.sync.activity.ActivityFeed`.
        parent: Qt parent.

    Signals:
        media_found: The mount path of a newly attached volume.
        imported: ``(count, bytes)`` after an import.
    """

    media_found = Signal(str)
    imported = Signal(int, int)

    def __init__(
        self,
        account: AccountInfo,
        *,
        destination: str = "Pictures/Imported",
        activity: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._destination = destination
        self._activity = activity
        self._monitor: Any = None

    def start(self) -> bool:
        """Watch for removable media via ``GVolumeMonitor``."""
        try:
            from gi.repository import Gio

            self._monitor = Gio.VolumeMonitor.get()
            self._monitor.connect("mount-added", self._on_mount)
        except Exception:  # noqa: BLE001
            log.warning("could not watch for removable media", exc_info=True)
            return False
        return True

    def stop(self) -> None:
        self._monitor = None

    def _on_mount(self, _monitor: Any, mount: Any) -> None:  # pragma: no cover
        try:
            root = mount.get_root().get_path()
        except Exception:  # noqa: BLE001
            return
        if root:
            self.media_found.emit(root)

    def candidates(self, media_root: Path | str) -> list[Path]:
        """Every importable picture or video on the volume."""
        root = Path(media_root)
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*")
                      if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)

    def import_from(self, media_root: Path | str, *,
                    remove_after: bool = False) -> tuple[int, int]:
        """Copy pictures off a volume into OneDrive.

        Args:
            media_root: The mounted volume.
            remove_after: Delete the originals once every copy has been
                verified. Off by default and never assumed.

        Returns:
            ``(files imported, bytes imported)``.

        Copy and verify, never move. The source is removable and can be
        unplugged at any instant; a move interrupted halfway leaves the photo
        library recoverable from neither end, and the whole point of importing
        a camera roll is that it is the only copy.
        """
        destination = Path(self.account.sync_root).expanduser() / self._destination
        destination.mkdir(parents=True, exist_ok=True)

        count = 0
        total = 0
        verified: list[Path] = []
        for source in self.candidates(media_root):
            target = destination / source.name
            if target.exists() and _digest(target) == _digest(source):
                verified.append(source)
                continue
            try:
                shutil.copy2(source, target)
            except OSError:
                log.warning("could not import %s", source, exc_info=True)
                continue
            if _digest(target) != _digest(source):
                log.error("%s did not verify after copying; leaving the "
                          "original alone", source)
                continue
            verified.append(source)
            count += 1
            total += _size_of(target)
            if self._activity is not None:
                self._activity.record(f"{self._destination}/{source.name}",
                                      ActivityVerb.CREATED,
                                      size=_size_of(target))

        if remove_after:
            for source in verified:
                try:
                    os.unlink(source)
                except OSError:
                    log.warning("could not remove %s after importing it",
                                source, exc_info=True)

        log.info("imported %d files (%d bytes) from %s", count, total, media_root)
        self.imported.emit(count, total)
        return (count, total)


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _digest(path: Path, block: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb", buffering=0) as handle:
            while True:
                chunk = handle.read(block)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()
