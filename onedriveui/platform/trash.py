"""The freedesktop Trash — invariant **I10**, and the `.Trash-1000` landmine.

**I10: every local deletion this application performs goes to the Trash, never
`unlink()`.** That is the whole point of this module: a destructive action we
take on the user's behalf must always have an undo, and the undo has to be one
the user's own file manager can see. So `trash()` is the only removal primitive
in the codebase, and it never unlinks the payload — it renames it, or copies and
then removes it, and it always leaves a `.trashinfo` behind that records where
the file came from.

The layout, per the FreeDesktop.org Trash Specification 1.0:

    ~/.local/share/Trash/
    ├── files/<name>                 the content
    ├── info/<name>.trashinfo        [Trash Info] / Path= / DeletionDate=
    └── directorysizes               a cache, one line per trashed directory

Four details that are easy to get wrong, all verified against `gio trash` on the
target machine:

* The info filename is the **full basename in `files/` plus `.trashinfo`** —
  `report.docx` becomes `report.docx.trashinfo`, not `report.trashinfo`.
* `Path=` is **percent-encoded** with `/` left alone. `urllib.parse.quote(p,
  safe="/")` reproduces GLib byte for byte, including `%20`, `%23` and `%25`,
  across the printable ASCII range and UTF-8.
* `DeletionDate` is **local time with no zone suffix**, `%Y-%m-%dT%H:%M:%S`.
* The info file is created **first, with `O_EXCL`**, and only then is the payload
  moved. That ordering is what makes name allocation race-free between us and
  the file manager, and it guarantees nothing can ever sit in `files/` without a
  matching `info/` entry — an orphan there is invisible to every trash browser.

## The `~/OneDrive/.Trash-1000` landmine

Deleting a file *inside* a mount creates `$mountpoint/.Trash-$uid/`, and the
target machine already has one: `gio trash --list` reports
`/home/…/OneDrive/.Trash-1000/files/Escrito`. On a FUSE mount of a cloud remote
that means **a file manager delete uploads the deleted file back to the cloud**
under a hidden directory.

Two defences, and this module is the second:

1. `.Trash-1000/` is in `constants.MANDATORY_EXCLUDES`, so it is excluded from
   the mount argv and from every filters file.
2. `find_nested_trash_dirs()` / `drain_nested_trash()` here find such a directory
   and move its contents into the home trash, preserving each file's original
   path, so the user keeps the undo and the sync tree is left clean.

## Why trashing *inside* the mount is refused

`trash()` raises `SafetyRefusal` for any path at or under a `fuse.rclone`
mountpoint. Both available behaviours would be wrong: writing a nested
`.Trash-$uid` recreates the landmine above, and copying the file out to the home
trash hydrates it — pulling the entire file down through FUSE just to delete it.
Deleting a file that lives in OneDrive is a *remote* operation and belongs to
`sync/trashbin.py`, which moves it server-side into `.onedriveui-trash/`.

Threading: pure filesystem work, no Gio and no Qt, so this module is safe on the
`IOPool`. Trashing a large directory across devices copies, so it should be.
"""

from __future__ import annotations

import datetime as _dt
import errno
import logging
import os
import shutil
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Iterator

from onedriveui import paths
from onedriveui.errors import OneDriveUIError, SafetyRefusal

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Layout
# ─────────────────────────────────────────────────────────────────────────────

#: `$XDG_DATA_HOME/Trash` — the home trash, which is NOT under `paths.data_dir()`
#: (that is `…/share/onedriveui`); the trash is shared with every application.
TRASH_DIR_NAME: Final[str] = "Trash"
FILES_DIR: Final[str] = "files"
INFO_DIR: Final[str] = "info"
DIRECTORYSIZES: Final[str] = "directorysizes"

#: The suffix appended to the name in `files/` to get the info filename.
INFO_SUFFIX: Final[str] = ".trashinfo"

#: The section and keys of a `.trashinfo` file, verbatim from the specification.
INFO_SECTION: Final[str] = "[Trash Info]"
KEY_PATH: Final[str] = "Path"
KEY_DELETION_DATE: Final[str] = "DeletionDate"

#: `DeletionDate` is local time with no zone suffix. Confirmed against
#: `gio trash`, which wrote `DeletionDate=2026-08-31T09:05:04`.
DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S"

#: What `urllib.parse.quote` must leave alone so `Path=` matches GLib exactly.
PATH_SAFE: Final[str] = "/"

#: Trash directories are private: 0700 on the tree, 0600 on each info file.
TRASH_MODE: Final[int] = 0o700
INFO_MODE: Final[int] = 0o600

#: A per-mount trash, per the specification's two forms. `.Trash/$uid` is only
#: usable when `$topdir/.Trash` exists, is a real directory (not a symlink) and
#: has the sticky bit; otherwise `.Trash-$uid` is created.
ADMIN_TRASH_NAME: Final[str] = ".Trash"
USER_TRASH_PREFIX: Final[str] = ".Trash-"

#: How many `name.N.ext` candidates to try before giving up. A collision needs a
#: file manager racing us on the same basename; a thousand of them is a bug.
MAX_NAME_ATTEMPTS: Final[int] = 1024

#: `SafetyRefusal.invariant` for every refusal in this module.
TRASH_RULE: Final[str] = "I10"

#: How deep `find_nested_trash_dirs()` descends by default. The specification
#: puts a mount trash at the top of the mount, and the sync root may hold
#: hundreds of thousands of items — a recursive walk through FUSE is not free.
NESTED_SCAN_DEPTH: Final[int] = 1


@dataclass(frozen=True, slots=True)
class TrashedFile:
    """One entry in a trash directory.

    Deliberately distinct from `models.TrashEntry`, which describes the *remote*
    recycle bin (`onedrive:.onedriveui-trash/`) and is keyed by account and
    remote path. This one is a local filesystem fact.

    Attributes:
        name: The basename inside `files/`, after collision renaming.
        trash_dir: The trash directory holding it.
        original_path: Where it came from, absolute and decoded.
        deleted_at: The `DeletionDate` string, local time, no zone.
        is_dir: Whether the trashed entry is a directory.
        size: Bytes, or 0 when it could not be measured.
    """

    name: str
    trash_dir: Path
    original_path: Path
    deleted_at: str = ""
    is_dir: bool = False
    size: int = 0

    @property
    def files_path(self) -> Path:
        """The payload's location inside the trash."""
        return self.trash_dir / FILES_DIR / self.name

    @property
    def info_path(self) -> Path:
        """The `.trashinfo` describing it."""
        return self.trash_dir / INFO_DIR / (self.name + INFO_SUFFIX)

    @property
    def exists(self) -> bool:
        """Whether the payload is still present."""
        return self.files_path.exists() or self.files_path.is_symlink()


# ═════════════════════════════════════════════════════════════════════════════
# Trash directories
# ═════════════════════════════════════════════════════════════════════════════

def home_trash() -> Path:
    """`$XDG_DATA_HOME/Trash`, created with `files/` and `info/`.

    Returns:
        The home trash directory.
    """
    data = os.environ.get("XDG_DATA_HOME")
    base = Path(data).expanduser() if data else Path.home() / ".local" / "share"
    return _ensure_trash(base / TRASH_DIR_NAME)


def _ensure_trash(trash_dir: Path) -> Path:
    """Create a trash directory's `files/` and `info/` subdirectories.

    Args:
        trash_dir: The trash root.

    Returns:
        `trash_dir`, unchanged.

    Raises:
        OneDriveUIError: If the directories could not be created.
    """
    try:
        for sub in (FILES_DIR, INFO_DIR):
            (trash_dir / sub).mkdir(parents=True, exist_ok=True, mode=TRASH_MODE)
    except OSError as exc:
        raise OneDriveUIError(f"could not prepare the trash at {trash_dir}: {exc}") from exc
    return trash_dir


def _top_dir(path: Path) -> Path:
    """The mountpoint the path lives on.

    Args:
        path: An existing path, or one whose parent exists.

    Returns:
        The nearest ancestor that is a mountpoint, or `/`.
    """
    current = path if path.is_dir() else path.parent
    current = Path(os.path.abspath(current))
    while True:
        if os.path.ismount(current) or current.parent == current:
            return current
        current = current.parent


def mount_trash(top_dir: Path, *, create: bool = True) -> Path | None:
    """The per-mount trash for a filesystem, per the specification's two forms.

    Args:
        top_dir: The mountpoint.
        create: Whether `.Trash-$uid` may be created when it does not exist.

    Returns:
        A prepared trash directory, or `None` when neither form is usable —
        typically a read-only mount, in which case the caller falls back to
        copying into the home trash.
    """
    uid = os.getuid()
    admin = top_dir / ADMIN_TRASH_NAME
    # Form 1: $topdir/.Trash/$uid. Only trustworthy when the administrator
    # created .Trash as a real, sticky directory — the sticky bit is what stops
    # another user replacing our subdirectory with a symlink.
    try:
        if admin.is_dir() and not admin.is_symlink():
            mode = admin.stat().st_mode
            if mode & 0o1000:
                return _ensure_trash(admin / str(uid))
    except OSError:
        pass
    # Form 2: $topdir/.Trash-$uid, which we may create ourselves.
    personal = top_dir / f"{USER_TRASH_PREFIX}{uid}"
    if not create and not personal.is_dir():
        return None
    try:
        return _ensure_trash(personal)
    except OneDriveUIError:
        return None


def _same_device(a: Path, b: Path) -> bool:
    """Whether two paths are on the same filesystem.

    Args:
        a: A path whose parent exists.
        b: An existing directory.

    Returns:
        True if `st_dev` matches. False when either cannot be stat'ed, which
        makes the caller take the safe copy path.
    """
    try:
        first = a if a.exists() or a.is_symlink() else a.parent
        return os.lstat(first).st_dev == os.stat(b).st_dev
    except OSError:
        return False


# ═════════════════════════════════════════════════════════════════════════════
# .trashinfo
# ═════════════════════════════════════════════════════════════════════════════

def encode_path(path: str | os.PathLike[str]) -> str:
    """Percent-encode a path for the `Path=` key.

    Byte-identical to what `gio trash` writes: space becomes `%20`, `#` becomes
    `%23`, `%` becomes `%25`, non-ASCII is UTF-8 percent-encoded, and `/` is left
    alone. Verified across the printable ASCII range on the target machine.

    Args:
        path: The original path, absolute or relative to a mount trash.

    Returns:
        The encoded string.
    """
    return urllib.parse.quote(str(path), safe=PATH_SAFE)


def decode_path(encoded: str) -> str:
    """Reverse `encode_path()`.

    Args:
        encoded: The `Path=` value.

    Returns:
        The decoded path.
    """
    return urllib.parse.unquote(encoded)


def info_text(original: str | os.PathLike[str], when: _dt.datetime | None = None) -> str:
    """Build a `.trashinfo` body.

    Args:
        original: The original path. Absolute for the home trash; relative to
            the mountpoint for a per-mount trash.
        when: The deletion time, or `None` for now. Local time, because the
            specification's format carries no zone.

    Returns:
        The complete file text, ending in a newline.
    """
    stamp = (when or _dt.datetime.now()).strftime(DATE_FORMAT)
    return (f"{INFO_SECTION}\n"
            f"{KEY_PATH}={encode_path(original)}\n"
            f"{KEY_DELETION_DATE}={stamp}\n")


def parse_info(text: str) -> tuple[str, str]:
    """Read a `.trashinfo` body.

    Hand-parsed rather than fed to `configparser`, whose interpolation would
    choke on the `%` sequences that every non-trivial `Path=` contains.

    Args:
        text: The file contents.

    Returns:
        `(decoded original path, deletion date)`. Either may be `""` when the
        file is malformed — a trash browser must survive junk in `info/`.
    """
    original = ""
    stamp = ""
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = stripped == INFO_SECTION
            continue
        if not in_section or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key == KEY_PATH and not original:
            original = decode_path(value.strip())
        elif key == KEY_DELETION_DATE and not stamp:
            stamp = value.strip()
    return original, stamp


# ═════════════════════════════════════════════════════════════════════════════
# Name allocation
# ═════════════════════════════════════════════════════════════════════════════

def _candidate_names(name: str) -> Iterator[str]:
    """Yield collision-free candidates for a name in `files/`.

    Args:
        name: The original basename.

    Yields:
        `name`, then `stem.2.suffix`, `stem.3.suffix`, … which is the shape
        file managers use and which keeps the extension where the user can see
        it.
    """
    yield name
    stem, dot, suffix = name.partition(".")
    if not dot:
        for index in range(2, MAX_NAME_ATTEMPTS):
            yield f"{name}.{index}"
        return
    for index in range(2, MAX_NAME_ATTEMPTS):
        yield f"{stem}.{index}.{suffix}"


def _claim_info(trash_dir: Path, name: str, original: str,
                when: _dt.datetime | None = None) -> tuple[str, Path]:
    """Atomically reserve a name by creating its info file with `O_EXCL`.

    This is the specification's own race-free protocol, and it is why the info
    file is written before the payload moves: `O_EXCL` makes the name ours even
    if a file manager is trashing a same-named file at the same instant.

    Args:
        trash_dir: The trash directory.
        name: The preferred basename.
        original: The `Path=` value to record.
        when: Deletion time, or `None` for now.

    Returns:
        `(claimed name, info file path)`.

    Raises:
        OneDriveUIError: If no free name was found or the info file could not
            be written.
    """
    info_dir = trash_dir / INFO_DIR
    body = info_text(original, when).encode("utf-8")
    for candidate in _candidate_names(name):
        info_path = info_dir / (candidate + INFO_SUFFIX)
        try:
            fd = os.open(info_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, INFO_MODE)
        except FileExistsError:
            continue
        except OSError as exc:
            raise OneDriveUIError(f"could not write {info_path}: {exc}") from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
        except OSError as exc:
            info_path.unlink(missing_ok=True)
            raise OneDriveUIError(f"could not write {info_path}: {exc}") from exc
        # The payload name must be free too: a previous crash between the info
        # write and the move can leave files/<name> occupied on its own.
        if (trash_dir / FILES_DIR / candidate).exists():
            info_path.unlink(missing_ok=True)
            continue
        return candidate, info_path
    raise OneDriveUIError(
        f"no free name for {name!r} in {trash_dir} after {MAX_NAME_ATTEMPTS} tries")


# ═════════════════════════════════════════════════════════════════════════════
# Trashing
# ═════════════════════════════════════════════════════════════════════════════

def assert_trashable(path: Path) -> Path:
    """Refuse to locally trash something that must not be locally trashed.

    Args:
        path: The path about to be removed.

    The refusals come **before** the existence check, deliberately. A caller
    that names a path inside the mount is buggy whether or not the file happens
    to exist, and reporting `FileNotFoundError` would hide that; the existence
    check would also be a FUSE round trip we have already decided not to make.

    Returns:
        The absolute path.

    Raises:
        SafetyRefusal: If the path is at or under a `fuse.rclone` mountpoint, or
            is a trash directory itself.
        FileNotFoundError: If nothing is there.
    """
    absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    if paths.is_under_fuse_mount(absolute):
        raise SafetyRefusal(
            TRASH_RULE,
            f"{absolute} is inside a fuse.rclone mount: a local trash there "
            "would either create the .Trash-$uid directory that syncs deleted "
            "files back to the cloud, or hydrate the whole file through FUSE "
            "just to delete it. Deleting from OneDrive is a remote operation — "
            "use sync.trashbin.soft_delete()",
        )
    if absolute.name in (FILES_DIR, INFO_DIR) and absolute.parent.name == TRASH_DIR_NAME:
        raise SafetyRefusal(TRASH_RULE, f"{absolute} is part of the trash itself")
    if absolute.name.startswith(USER_TRASH_PREFIX) or absolute.name == ADMIN_TRASH_NAME:
        raise SafetyRefusal(
            TRASH_RULE,
            f"{absolute} is a trash directory; use drain_nested_trash() to "
            "empty it into the home trash instead of trashing it wholesale",
        )
    if not absolute.exists() and not absolute.is_symlink():
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(absolute))
    return absolute


def trash(path: str | os.PathLike[str], *,
          when: _dt.datetime | None = None) -> TrashedFile:
    """Move a file or directory to the freedesktop Trash. **Invariant I10.**

    The only removal primitive in the codebase. Same-filesystem removals are a
    `rename()` and therefore atomic and instant; a cross-device removal falls
    back to the mount's own trash, and if that is unusable, to a copy into the
    home trash followed by removing the original — in that order, so a failure
    can never lose the data.

    Args:
        path: What to remove.
        when: The deletion time to record, or `None` for now.

    Returns:
        The `TrashedFile` describing the entry, from which it can be restored.

    Raises:
        SafetyRefusal: If the path is inside a `fuse.rclone` mount, or is itself
            a trash directory.
        FileNotFoundError: If the path does not exist.
        OneDriveUIError: If the trash could not be prepared or the move failed.
    """
    source = assert_trashable(Path(str(path)))
    is_dir = source.is_dir() and not source.is_symlink()

    home = home_trash()
    if _same_device(source, home / FILES_DIR):
        return _trash_into(home, source, str(source), is_dir, when, copy=False)

    # Cross-device. The specification's per-mount trash keeps the data on its
    # own filesystem, which is both faster and honest about where it lives.
    top = _top_dir(source)
    mount = mount_trash(top)
    if mount is not None and _same_device(source, mount / FILES_DIR):
        try:
            relative = str(source.relative_to(top))
        except ValueError:  # pragma: no cover - _top_dir guarantees an ancestor
            relative = str(source)
        return _trash_into(mount, source, relative, is_dir, when, copy=False)

    log.info("%s is on another filesystem with no usable mount trash; "
             "copying into the home trash", source)
    return _trash_into(home, source, str(source), is_dir, when, copy=True)


def trash_tree(path: str | os.PathLike[str], *,
               when: _dt.datetime | None = None) -> TrashedFile:
    """Move a directory tree to the Trash.

    Identical to `trash()` — the specification stores a directory as one entry —
    and named separately only so a caller removing a tree reads as deliberate.

    Args:
        path: The directory to remove.
        when: The deletion time to record, or `None` for now.

    Returns:
        The `TrashedFile` describing the entry.

    Raises:
        SafetyRefusal: As `trash()`.
        NotADirectoryError: If `path` is not a directory.
        FileNotFoundError: If the path does not exist.
        OneDriveUIError: If the move failed.
    """
    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(target))
    return trash(target, when=when)


def _trash_into(trash_dir: Path, source: Path, record: str, is_dir: bool,
                when: _dt.datetime | None, *, copy: bool) -> TrashedFile:
    """Claim a name, move the payload, and record the size.

    Args:
        trash_dir: The destination trash.
        source: What to move.
        record: The value for `Path=` — absolute for the home trash, relative to
            the mountpoint for a per-mount trash.
        is_dir: Whether the payload is a directory.
        when: Deletion time, or `None` for now.
        copy: Copy and then remove, instead of renaming. Required across
            filesystems.

    Returns:
        The resulting `TrashedFile`.

    Raises:
        OneDriveUIError: If the payload could not be moved. The reserved info
            file is removed first, so a failure leaves no orphan.
    """
    size = _measure(source, is_dir)
    stamp = when or _dt.datetime.now()
    name, info_path = _claim_info(trash_dir, source.name, record, stamp)
    destination = trash_dir / FILES_DIR / name
    try:
        if copy:
            # Copy first, remove second: an interrupted copy loses nothing.
            if is_dir:
                shutil.copytree(source, destination, symlinks=True)
                shutil.rmtree(source)
            else:
                shutil.copy2(source, destination, follow_symlinks=False)
                source.unlink()
        else:
            os.rename(source, destination)
    except OSError as exc:
        info_path.unlink(missing_ok=True)
        if copy:
            _remove_partial(destination, is_dir)
        raise OneDriveUIError(f"could not trash {source}: {exc}") from exc

    if is_dir:
        _record_directory_size(trash_dir, name, size)
    log.info("trashed %s -> %s", source, destination)
    return TrashedFile(
        name=name, trash_dir=trash_dir,
        original_path=Path(record) if os.path.isabs(record) else _top_dir(destination) / record,
        deleted_at=stamp.strftime(DATE_FORMAT), is_dir=is_dir, size=size,
    )


def _remove_partial(destination: Path, is_dir: bool) -> None:
    """Clean up a half-written copy after a failed cross-device trash.

    Args:
        destination: The partially written payload.
        is_dir: Whether it is a directory.
    """
    try:
        if is_dir:
            shutil.rmtree(destination, ignore_errors=True)
        else:
            destination.unlink(missing_ok=True)
    except OSError:
        pass


def _measure(path: Path, is_dir: bool) -> int:
    """Total bytes of a file or directory tree.

    Args:
        path: What to measure.
        is_dir: Whether it is a directory.

    Returns:
        Bytes, or 0 when it could not be measured. Never raises: a size is a
        nicety and must not block a deletion.
    """
    try:
        if not is_dir:
            return os.lstat(path).st_size
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _record_directory_size(trash_dir: Path, name: str, size: int) -> None:
    """Append to the `directorysizes` cache.

    Format, per the specification: `<size> <mtime in ms> <urlencoded name>`.
    Best-effort — a trash browser recomputes when the entry is missing.

    Args:
        trash_dir: The trash directory.
        name: The basename in `files/`.
        size: Total bytes.
    """
    try:
        mtime_ms = int(os.lstat(trash_dir / FILES_DIR / name).st_mtime * 1000)
        with (trash_dir / DIRECTORYSIZES).open("a", encoding="utf-8") as handle:
            handle.write(f"{size} {mtime_ms} {encode_path(name)}\n")
    except OSError as exc:
        log.debug("could not update %s: %s", DIRECTORYSIZES, exc)


def _forget_directory_size(trash_dir: Path, name: str) -> None:
    """Drop a `directorysizes` line for an entry that has left the trash.

    Args:
        trash_dir: The trash directory.
        name: The basename that was in `files/`.
    """
    target = trash_dir / DIRECTORYSIZES
    encoded = encode_path(name)
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    kept = [line for line in lines if line.rsplit(" ", 1)[-1] != encoded]
    if len(kept) == len(lines):
        return
    try:
        target.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError as exc:
        log.debug("could not rewrite %s: %s", DIRECTORYSIZES, exc)


# ═════════════════════════════════════════════════════════════════════════════
# Listing and restoring
# ═════════════════════════════════════════════════════════════════════════════

def list_trash(trash_dir: Path | None = None) -> list[TrashedFile]:
    """Every entry in a trash directory, newest first.

    Reads `info/`, not `files/`: an entry with no info file is unrestorable and
    invisible to every other trash browser, so it is not reported either.

    Args:
        trash_dir: A trash directory, or `None` for the home trash.

    Returns:
        The entries, sorted by deletion date descending.
    """
    root = trash_dir if trash_dir is not None else home_trash()
    info_dir = root / INFO_DIR
    files_dir = root / FILES_DIR
    out: list[TrashedFile] = []
    try:
        entries = sorted(info_dir.iterdir())
    except OSError:
        return out
    for info_path in entries:
        if not info_path.name.endswith(INFO_SUFFIX):
            continue
        name = info_path.name[: -len(INFO_SUFFIX)]
        try:
            original, stamp = parse_info(
                info_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        payload = files_dir / name
        try:
            stat = os.lstat(payload)
            is_dir = os.path.isdir(payload) and not os.path.islink(payload)
            size = _measure(payload, is_dir) if is_dir else stat.st_size
        except OSError:
            is_dir, size = False, 0
        # A relative Path= belongs to a per-mount trash and is resolved against
        # the mountpoint the trash directory sits on.
        absolute = (Path(original) if os.path.isabs(original)
                    else _top_dir(root) / original)
        out.append(TrashedFile(name=name, trash_dir=root, original_path=absolute,
                               deleted_at=stamp, is_dir=is_dir, size=size))
    out.sort(key=lambda item: item.deleted_at, reverse=True)
    return out


def find_entry(name: str, trash_dir: Path | None = None) -> TrashedFile | None:
    """Look up one trash entry by its name in `files/`.

    Args:
        name: The basename inside `files/`.
        trash_dir: A trash directory, or `None` for the home trash.

    Returns:
        The entry, or `None` when it is not there.
    """
    for item in list_trash(trash_dir):
        if item.name == name:
            return item
    return None


def restore(item: TrashedFile, *, overwrite: bool = False) -> Path:
    """Put a trashed entry back where it came from.

    Args:
        item: The entry, from `list_trash()` or `trash()`.
        overwrite: Replace a file that has since reappeared at the original
            path. Off by default: silently clobbering the newer file would be a
            second, unlogged deletion.

    Returns:
        The restored path.

    Raises:
        FileNotFoundError: If the payload is no longer in the trash.
        FileExistsError: If something is already at the original path and
            `overwrite` is False.
        OneDriveUIError: If the move back failed.
    """
    payload = item.files_path
    if not payload.exists() and not payload.is_symlink():
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(payload))
    target = item.original_path
    if (target.exists() or target.is_symlink()) and not overwrite:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite and (target.exists() or target.is_symlink()):
            _remove_partial(target, target.is_dir() and not target.is_symlink())
        try:
            os.rename(payload, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            if item.is_dir:
                shutil.copytree(payload, target, symlinks=True)
                shutil.rmtree(payload)
            else:
                shutil.copy2(payload, target, follow_symlinks=False)
                payload.unlink()
    except OSError as exc:
        raise OneDriveUIError(f"could not restore {payload} to {target}: {exc}") from exc
    item.info_path.unlink(missing_ok=True)
    if item.is_dir:
        _forget_directory_size(item.trash_dir, item.name)
    log.info("restored %s -> %s", payload, target)
    return target


# ═════════════════════════════════════════════════════════════════════════════
# The nested-trash landmine
# ═════════════════════════════════════════════════════════════════════════════

def is_nested_trash_dir(path: Path) -> bool:
    """Whether a directory is a per-mount trash.

    Args:
        path: The candidate.

    Returns:
        True for `.Trash-<uid>` and for a `.Trash` holding numeric subdirectories.
    """
    name = path.name
    if name.startswith(USER_TRASH_PREFIX):
        return name[len(USER_TRASH_PREFIX):].isdigit()
    return name == ADMIN_TRASH_NAME


def find_nested_trash_dirs(root: str | os.PathLike[str], *,
                           depth: int = NESTED_SCAN_DEPTH) -> list[Path]:
    """Find per-mount trash directories inside a tree.

    The specification puts a mount trash at the top of its mount, so the default
    depth of 1 is both correct and cheap — and cheapness matters here, because
    `root` is normally the FUSE-mounted sync root, where a recursive walk means
    a network round trip per directory.

    Args:
        root: The tree to inspect, normally the sync root.
        depth: How many levels below `root` to look. 0 checks `root` itself only.

    Returns:
        The trash directories found, in path order.
    """
    base = Path(os.path.abspath(os.path.expanduser(str(root))))
    found: list[Path] = []
    if is_nested_trash_dir(base) and base.is_dir():
        found.append(base)
    frontier = [base]
    for _level in range(max(0, depth)):
        nxt: list[Path] = []
        for directory in frontier:
            try:
                children = sorted(directory.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if not child.is_dir() or child.is_symlink():
                        continue
                except OSError:
                    continue
                if is_nested_trash_dir(child):
                    found.append(child)
                else:
                    nxt.append(child)
        frontier = nxt
    return found


def nested_trash_entries(root: str | os.PathLike[str], *,
                         depth: int = NESTED_SCAN_DEPTH) -> list[TrashedFile]:
    """Everything sitting in the nested trash directories under a tree.

    Args:
        root: The tree to inspect.
        depth: As `find_nested_trash_dirs()`.

    Returns:
        The entries, so the UI can show what a drain would move before it moves
        it.
    """
    out: list[TrashedFile] = []
    for trash_dir in find_nested_trash_dirs(root, depth=depth):
        for candidate in _trash_roots(trash_dir):
            out.extend(list_trash(candidate))
    return out


def _trash_roots(trash_dir: Path) -> Iterable[Path]:
    """The actual trash roots inside a `.Trash` or `.Trash-$uid` directory.

    Args:
        trash_dir: A per-mount trash directory.

    Yields:
        `trash_dir` itself for the `.Trash-$uid` form, or its per-uid
        subdirectories for the administrator-created `.Trash` form.
    """
    if trash_dir.name != ADMIN_TRASH_NAME:
        yield trash_dir
        return
    try:
        children = sorted(trash_dir.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir() and child.name.isdigit():
            yield child


def drain_nested_trash(root: str | os.PathLike[str], *,
                       depth: int = NESTED_SCAN_DEPTH,
                       dry_run: bool = False,
                       max_bytes: int | None = None,
                       remove_empty: bool = True) -> list[TrashedFile]:
    """Empty a `~/OneDrive/.Trash-1000` into the home trash.

    A file manager delete *inside* the sync root creates this directory, and
    everything in it would otherwise sit on the mount forever — visible in
    listings, counted against the item budget, and only kept out of the cloud by
    `constants.MANDATORY_EXCLUDES`. Draining it moves each entry into the home
    trash with its original path preserved, so the user still has an undo and
    the sync tree is left clean.

    **This copies through FUSE.** Each entry crosses from the mount onto local
    disk, which for an online-only file means downloading it. In practice these
    are files the user deleted moments ago and which are therefore already in
    the VFS cache, but pass `max_bytes` when that assumption is not safe, and
    call `nested_trash_entries()` first when the user should be asked.

    Args:
        root: The tree to drain, normally the sync root.
        depth: As `find_nested_trash_dirs()`.
        dry_run: Report what would move without moving anything.
        max_bytes: Skip any entry larger than this. `None` means no limit.
        remove_empty: Remove a nested trash directory once it is empty.

    Returns:
        The entries that were moved — or, under `dry_run`, that would be.
    """
    moved: list[TrashedFile] = []
    for trash_dir in find_nested_trash_dirs(root, depth=depth):
        for source_root in _trash_roots(trash_dir):
            for item in list_trash(source_root):
                if max_bytes is not None and item.size > max_bytes:
                    log.info("skipping %s (%d bytes > %d)",
                             item.name, item.size, max_bytes)
                    continue
                if dry_run:
                    moved.append(item)
                    continue
                relocated = _relocate_to_home(item)
                if relocated is not None:
                    moved.append(relocated)
        if remove_empty and not dry_run:
            _remove_if_empty(trash_dir)
    if moved:
        log.info("drained %d entries from the nested trash under %s", len(moved), root)
    return moved


def _relocate_to_home(item: TrashedFile) -> TrashedFile | None:
    """Move one nested-trash entry into the home trash, keeping its origin.

    Args:
        item: The entry in the nested trash.

    Returns:
        The new home-trash entry, or `None` when the move failed.
    """
    home = home_trash()
    payload = item.files_path
    if not payload.exists() and not payload.is_symlink():
        item.info_path.unlink(missing_ok=True)
        return None
    is_dir = payload.is_dir() and not payload.is_symlink()
    size = item.size or _measure(payload, is_dir)
    when = _parse_stamp(item.deleted_at)
    try:
        name, info_path = _claim_info(home, item.name, str(item.original_path), when)
    except OneDriveUIError as exc:
        log.warning("could not claim a home-trash name for %s: %s", item.name, exc)
        return None
    destination = home / FILES_DIR / name
    try:
        try:
            os.rename(payload, destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            if is_dir:
                shutil.copytree(payload, destination, symlinks=True)
                shutil.rmtree(payload)
            else:
                shutil.copy2(payload, destination, follow_symlinks=False)
                payload.unlink()
    except OSError as exc:
        info_path.unlink(missing_ok=True)
        _remove_partial(destination, is_dir)
        log.warning("could not drain %s: %s", payload, exc)
        return None
    item.info_path.unlink(missing_ok=True)
    if is_dir:
        _forget_directory_size(item.trash_dir, item.name)
        _record_directory_size(home, name, size)
    log.info("drained %s -> %s", payload, destination)
    return TrashedFile(name=name, trash_dir=home, original_path=item.original_path,
                       deleted_at=item.deleted_at, is_dir=is_dir, size=size)


def _parse_stamp(stamp: str) -> _dt.datetime | None:
    """Parse a `DeletionDate`, keeping the original deletion time on a drain.

    Args:
        stamp: The `DeletionDate` value.

    Returns:
        The datetime, or `None` when it cannot be parsed.
    """
    try:
        return _dt.datetime.strptime(stamp, DATE_FORMAT)
    except ValueError:
        return None


def _remove_if_empty(trash_dir: Path) -> bool:
    """Remove a drained nested trash directory.

    Only ever removes empty directories, so a drain that skipped an entry never
    destroys it.

    Args:
        trash_dir: The nested trash.

    Returns:
        True if the directory was removed.
    """
    for root in _trash_roots(trash_dir):
        for sub in (FILES_DIR, INFO_DIR):
            try:
                (root / sub).rmdir()
            except OSError:
                return False
        try:
            (root / DIRECTORYSIZES).unlink(missing_ok=True)
            if root != trash_dir:
                root.rmdir()
        except OSError:
            return False
    try:
        trash_dir.rmdir()
    except OSError:
        return False
    log.info("removed the drained nested trash %s", trash_dir)
    return True


__all__ = [
    "TrashedFile",
    "TRASH_DIR_NAME", "FILES_DIR", "INFO_DIR", "DIRECTORYSIZES", "INFO_SUFFIX",
    "INFO_SECTION", "KEY_PATH", "KEY_DELETION_DATE", "DATE_FORMAT", "PATH_SAFE",
    "TRASH_MODE", "INFO_MODE", "ADMIN_TRASH_NAME", "USER_TRASH_PREFIX",
    "MAX_NAME_ATTEMPTS", "TRASH_RULE", "NESTED_SCAN_DEPTH",
    "home_trash", "mount_trash",
    "encode_path", "decode_path", "info_text", "parse_info",
    "assert_trashable", "trash", "trash_tree",
    "list_trash", "find_entry", "restore",
    "is_nested_trash_dir", "find_nested_trash_dirs", "nested_trash_entries",
    "drain_nested_trash",
]
