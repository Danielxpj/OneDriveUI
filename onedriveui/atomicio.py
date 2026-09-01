"""Crash-safe file primitives.

Everything this application persists outside SQLite — ``config.json``, the
bisync filters file and its md5 sidecar, ``endpoints.json``, run metadata — is
written through here, because every one of them is a file whose truncation
breaks startup.

The write protocol, in order, and every step matters:

1. Write the payload to ``<name>.<pid>.tmp`` **in the same directory**, so the
   final rename cannot cross a filesystem boundary and degrade into a copy.
2. ``flush()`` then ``os.fsync(fd)``: the bytes are on the platter, not in the
   page cache.
3. ``os.replace()``: POSIX guarantees the rename is atomic, so a reader sees
   either the whole old file or the whole new one — never a half-written one.
4. ``os.fsync()`` on the **directory**: without this the rename itself can be
   lost in a power cut, leaving the old inode with the new name unlinked.

Step 4 is the one that is always forgotten and the one that turns "atomic
write" into "atomic write that survives a crash".

The PID helpers live here rather than in ``platform/`` because
``endpoints.json`` records a PID next to its ``/proc/<pid>/stat`` start time,
and comparing both is the only way to tell "our daemon" from "some other
process that happens to have been given that PID after ours died". A bare
``os.kill(pid, 0)`` answers the wrong question.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import TracebackType
from collections.abc import Callable
from typing import Any, Final

from onedriveui.paths import DIR_MODE, FILE_MODE

__all__ = [
    "atomic_write_bytes", "atomic_write_text", "atomic_write_json",
    "backup_then_write", "read_json", "md5_of_file", "md5_of_bytes",
    "pid_is_alive", "proc_starttime", "InstanceLock", "fsync_dir",
    "BAK_SUFFIX", "TMP_SUFFIX", "MD5_CHUNK_BYTES",
]

#: The rotated previous copy. ``config.py`` repairs from it on a JSONDecodeError.
BAK_SUFFIX: Final[str] = ".bak"

#: The in-flight name. It carries the PID so two processes racing on the same
#: target cannot clobber each other's temporary file.
TMP_SUFFIX: Final[str] = ".tmp"

#: 1 MiB. Large enough that md5 of a 250 GB file is not syscall-bound, small
#: enough that hashing a file on the FUSE mount does not pin a huge buffer.
MD5_CHUNK_BYTES: Final[int] = 1024 * 1024

#: /proc/<pid>/stat field 22 (1-indexed) is the process start time in clock
#: ticks since boot. Fields 1 and 2 (pid and comm) are skipped by splitting
#: after the LAST ')', because comm is the executable name in parentheses and
#: may itself contain spaces and parentheses — ``sh -c 'exec -a "a b) c" ...'``
#: is enough to break every naive ``line.split()[21]``.
_STARTTIME_FIELD: Final[int] = 22


# ─────────────────────────────────────────────────────────────────────────────
# Atomic writes
# ─────────────────────────────────────────────────────────────────────────────

def fsync_dir(directory: Path | str) -> None:
    """fsync a directory so a rename into it is durable.

    Args:
        directory: The directory whose entries must be flushed.

    A directory that cannot be opened for reading (an exotic filesystem, a
    read-only mount) is not an error: the payload is already fsynced, and the
    caller's write has still succeeded.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    mode: int = FILE_MODE,
    sync_dir: bool = True,
) -> Path:
    """Write bytes so that the target is never observed partially written.

    Args:
        path: The destination file. Its parent directory is created with mode
            0700 if it does not exist.
        data: The exact bytes to land at `path`.
        mode: Permissions for the finished file. Defaults to 0600, because
            every file written through this module is adjacent to a credential.
        sync_dir: fsync the parent directory after the rename. Only ever set
            this False for a scratch file whose loss is acceptable.

    Returns:
        The destination path.

    Raises:
        OSError: If the payload could not be written or renamed. The temporary
            file is removed first, so a failure never leaves debris and never
            leaves a truncated destination — the previous contents survive
            intact.
    """
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{target.name}.", suffix=TMP_SUFFIX, dir=str(parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        # Includes KeyboardInterrupt and SystemExit on purpose: a Ctrl-C during
        # a config save must not leave a .tmp behind either.
        tmp.unlink(missing_ok=True)
        raise
    if sync_dir:
        fsync_dir(parent)
    return target


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    mode: int = FILE_MODE,
    encoding: str = "utf-8",
    sync_dir: bool = True,
) -> Path:
    """Atomically write text.

    Args:
        path: The destination file.
        text: The content. Written verbatim — no trailing newline is added,
            because the filters ``.md5`` sidecar must have none.
        mode: Permissions for the finished file.
        encoding: Text encoding. UTF-8 everywhere; rclone's own files are UTF-8.
        sync_dir: fsync the parent directory after the rename.

    Returns:
        The destination path.
    """
    return atomic_write_bytes(
        path, text.encode(encoding), mode=mode, sync_dir=sync_dir)


def atomic_write_json(
    path: Path | str,
    obj: Any,
    *,
    mode: int = FILE_MODE,
    indent: int | None = 2,
    sort_keys: bool = False,
    sync_dir: bool = True,
) -> Path:
    """Atomically write an object as JSON.

    The whole document is serialised **before** the temporary file is opened,
    so an unserialisable value raises without having touched the filesystem at
    all — the destination keeps its previous, valid contents.

    Args:
        path: The destination file.
        obj: Any JSON-serialisable object.
        mode: Permissions for the finished file. 0600 by default.
        indent: Passed to ``json.dumps``; 2 keeps ``config.json`` hand-editable.
        sort_keys: Sort object keys, for files that are diffed or hashed.
        sync_dir: fsync the parent directory after the rename.

    Returns:
        The destination path.

    Raises:
        TypeError: If `obj` is not JSON-serialisable. Nothing was written.
    """
    payload = json.dumps(obj, indent=indent, sort_keys=sort_keys,
                         ensure_ascii=False)
    if indent is not None:
        payload += "\n"
    return atomic_write_bytes(path, payload.encode("utf-8"),
                              mode=mode, sync_dir=sync_dir)


def _worth_keeping(keep_if: Callable[[bytes], bool], previous: bytes) -> bool:
    """Run the caller's predicate, treating a raised exception as "no".

    A predicate that blows up on the bytes it was handed has, in effect,
    answered: whatever is in that file is not something to promote to the
    backup.
    """
    try:
        return bool(keep_if(previous))
    except Exception:  # noqa: BLE001 - see the docstring
        return False


def backup_then_write(
    path: Path | str,
    data: bytes | str,
    *,
    mode: int = FILE_MODE,
    suffix: str = BAK_SUFFIX,
    encoding: str = "utf-8",
    keep_if: Callable[[bytes], bool] | None = None,
) -> Path:
    """Rotate the existing file to ``<name><suffix>``, then atomically write.

    The rotation is a **copy**, not a rename, and it is fsynced before the new
    payload is written. A rename would leave no file at `path` for the duration
    of the write, so a concurrent reader — the Nautilus extension, a second
    instance starting up — would see ENOENT and conclude the config was never
    written. Copy-then-replace means the path always resolves to a complete
    file, old or new.

    Args:
        path: The destination file.
        data: Bytes, or text to be encoded with `encoding`.
        mode: Permissions for both the backup and the finished file.
        suffix: The backup suffix. ``.bak`` per ARCHITECTURE §9.
        encoding: Encoding used when `data` is a string.
        keep_if: Given the bytes currently in `path`, return True if they are
            worth keeping as the backup. When it returns False the existing
            backup is left **untouched** rather than replaced.

            This is what stops a recovery from eating its own lifeline. The
            sequence is: `config.json` is corrupt, `load()` falls back to
            `config.json.bak` and returns the good settings, the application
            then saves — and an unconditional rotation copies the *corrupt*
            `config.json` over the good `.bak` that had just saved it. One more
            bad write and both copies are gone.

    Returns:
        The destination path.
    """
    target = Path(path)
    payload = data if isinstance(data, bytes) else data.encode(encoding)

    if target.exists():
        try:
            previous = target.read_bytes()
        except OSError:
            previous = None
        if previous is not None and (keep_if is None or _worth_keeping(keep_if,
                                                                       previous)):
            # The backup is itself written atomically: a crash here must not
            # destroy the only good copy while the real target is still valid.
            atomic_write_bytes(target.with_name(target.name + suffix),
                               previous, mode=mode)
    return atomic_write_bytes(target, payload, mode=mode)


def read_json(path: Path | str, default: Any = None) -> Any:
    """Read a JSON file, returning `default` on any failure.

    Args:
        path: The file to read.
        default: What to return when the file is missing, unreadable, or not
            valid JSON.

    Returns:
        The parsed object, or `default`. Never raises — a corrupt file on the
        startup path must degrade, not abort.
    """
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Hashing
# ─────────────────────────────────────────────────────────────────────────────

def md5_of_file(path: Path | str, *, chunk_bytes: int = MD5_CHUNK_BYTES) -> str:
    """Return the MD5 of a file's contents, byte-identical to ``md5sum``.

    Used for the bisync filters ``.md5`` sidecar, which is how invariant I11
    detects that the filters file changed and a ``--resync`` is now mandatory.

    Args:
        path: The file to hash. Read in binary with no newline translation, so
            the digest matches ``md5sum`` on any content.
        chunk_bytes: Read size. Bounded so hashing a huge file on the FUSE
            mount does not allocate proportionally to the file.

    Returns:
        32 lowercase hex characters, with no filename and no trailing newline —
        exactly the first field of ``md5sum``'s output.

    Raises:
        OSError: If the file cannot be read.
    """
    digest = hashlib.md5()
    with open(path, "rb", buffering=0) as handle:
        while True:
            block = handle.read(chunk_bytes)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def md5_of_bytes(data: bytes | str, *, encoding: str = "utf-8") -> str:
    """Return the MD5 of an in-memory payload.

    Args:
        data: Bytes, or text to be encoded with `encoding`.
        encoding: Encoding used when `data` is a string.

    Returns:
        32 lowercase hex characters, identical to ``md5_of_file`` on a file
        holding the same bytes.
    """
    payload = data if isinstance(data, bytes) else data.encode(encoding)
    return hashlib.md5(payload).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Process identity
# ─────────────────────────────────────────────────────────────────────────────

def proc_starttime(pid: int) -> int | None:
    """Read ``/proc/<pid>/stat`` field 22, the process start time.

    Args:
        pid: A process id.

    Returns:
        The start time in clock ticks since boot, or ``None`` if the process
        does not exist or ``/proc`` is not readable.

    The field is parsed by splitting after the **last** ``)`` rather than by
    ``split()[21]``: field 2 is the executable name in parentheses and is
    allowed to contain both spaces and parentheses, so the naive index is wrong
    for any process that chose a hostile ``argv[0]``.
    """
    if pid <= 0:
        return None
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1:].split()
    # fields[0] is field 3 (state), so field N lives at index N - 3.
    index = _STARTTIME_FIELD - 3
    if len(fields) <= index:
        return None
    try:
        return int(fields[index])
    except ValueError:
        return None


def pid_is_alive(pid: int, starttime: int | None = None) -> bool:
    """Answer whether a *specific* process is still running.

    PIDs are recycled. ``endpoints.json`` may name a daemon that died an hour
    ago and whose PID now belongs to somebody's text editor; driving an rc
    daemon is equivalent to shell access as this user, so "is that PID alive?"
    is not a safe question. Pairing the PID with its start time makes the
    identity unforgeable in practice: the tuple is unique for the lifetime of a
    boot.

    Args:
        pid: The process id recorded when the process was started.
        starttime: The ``/proc/<pid>/stat`` field 22 value recorded at the same
            moment. When ``None`` or ``0`` the check degrades to mere existence
            — acceptable only for a process we did not start ourselves.

    Returns:
        True only when a process with that id exists **and**, when a start time
        was supplied, was started at that instant.
    """
    if pid is None or pid <= 0:
        return False
    observed = proc_starttime(pid)
    if observed is None:
        return False
    if starttime:
        return observed == int(starttime)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Single-instance lock
# ─────────────────────────────────────────────────────────────────────────────

class InstanceLock:
    """An advisory lock file recording the holder's PID and start time.

    ``flock`` alone answers "is it locked?" — the kernel releases it when the
    holder dies, which is exactly what we want. The recorded ``pid starttime``
    line answers the second question the UI actually asks: *who* holds it, so
    "OneDriveUI is already running" can name a process, and so a lock file left
    behind on a filesystem where ``flock`` is a no-op can still be judged stale.

    Example:
        >>> lock = InstanceLock(paths.ui_lock())     # doctest: +SKIP
        >>> if not lock.acquire():                   # doctest: +SKIP
        ...     raise SystemExit("already running")
    """

    __slots__ = ("_path", "_fd", "_pid", "_starttime")

    def __init__(self, path: Path | str) -> None:
        """
        Args:
            path: The lock file. Created with mode 0600 on first acquire; its
                parent directory is created with mode 0700.
        """
        self._path = Path(path)
        self._fd: int | None = None
        self._pid = os.getpid()
        self._starttime = proc_starttime(self._pid) or 0

    @property
    def path(self) -> Path:
        """The lock file path."""
        return self._path

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._fd is not None

    def acquire(self) -> bool:
        """Try to take the lock without blocking.

        Returns:
            True if the lock is now held by this object (including when it
            already was — acquire is idempotent). False if another **live**
            process holds it.

        Raises:
            OSError: If the lock file cannot be created at all, which means the
                runtime directory is unusable and startup should fail loudly.
        """
        if self._fd is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
                     FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{self._pid} {self._starttime}\n".encode("ascii"))
            os.fsync(fd)
        except OSError:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        """Release the lock and remove the file. Idempotent."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def owner(self) -> tuple[int, int] | None:
        """Read the ``(pid, starttime)`` recorded in the lock file.

        Returns:
            The recorded pair, or ``None`` when the file is missing, empty or
            malformed. Says nothing about whether that process is still alive —
            pass the result to :func:`pid_is_alive` for that.
        """
        try:
            raw = self._path.read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        parts = raw.split()
        if not parts:
            return None
        try:
            pid = int(parts[0])
            starttime = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            return None
        return pid, starttime

    def owner_is_alive(self) -> bool:
        """True when the recorded owner is still the process that took the lock.

        Returns:
            False for a missing lock file and for a stale one whose PID has
            been recycled — which is what makes a leftover lock recoverable
            instead of permanently fatal.
        """
        recorded = self.owner()
        if recorded is None:
            return False
        pid, starttime = recorded
        if pid == self._pid and self._fd is not None:
            return True
        return pid_is_alive(pid, starttime)

    def __enter__(self) -> "InstanceLock":
        """Acquire the lock, raising if another live process holds it.

        Raises:
            BlockingIOError: If the lock is held elsewhere.
        """
        if not self.acquire():
            raise BlockingIOError(
                errno.EWOULDBLOCK, f"lock held by another process: {self._path}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
