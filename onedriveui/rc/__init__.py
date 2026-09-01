"""The rclone control layer — transport, daemons, safety guards, ``rclone.conf``.

Module map (WP-02 owns these seven; WP-03 and WP-04 add the rest):

    ``rc.client``     POST-only rc transport: ``RcClient`` (async, QNAM),
                      ``call_blocking()`` (IOPool threads), ``JobWatcher``.
    ``rc.endpoints``  Port bind-probe, credential generation, ``endpoints.json``.
    ``rc.daemon``     ``RcdSupervisor`` — the control-plane unit plus the
                      ``/proc`` ownership proof that keeps us off the user's
                      pre-existing rclone on 127.0.0.1:5572.
    ``rc.mountd``     ``MountController`` — the data-plane unit, the I6 liveness
                      probe and the upload-aware restart ladder.
    ``rc.guards``     Every non-overridable refusal (ARCHITECTURE §3).
    ``rc.conf``       The only writer of backend options (invariant I1).

Nothing in this package may import ``onedriveui.ui`` or ``onedriveui.sync``.

The three ``/proc`` readers below are package-internal. ``atomicio`` supplies
``proc_starttime()`` for the application at large, but its version reads the
literal ``/proc`` and offers no cmdline reader; the ownership proof needs both,
and needs the tree root to be substitutable so every branch of the proof can be
exercised without a real daemon. :data:`PROC` is that seam.
"""

from __future__ import annotations

import errno
import stat as _stat
from pathlib import Path

from onedriveui.paths import FILE_MODE

__all__ = [
    "FUSERMOUNT3",
    "PROC",
    "RCLONE_DEFAULT",
    "read_proc_cmdline",
    "read_proc_starttime",
]

#: ``advanced.rclone_path``'s default (ARCHITECTURE §9.1). Declared once for the
#: whole package; ``RcdSupervisor`` and ``MountController`` both take an override.
RCLONE_DEFAULT = "/usr/bin/rclone"

#: The setuid FUSE helper. ``umount(8)`` needs root for a user FUSE mount;
#: ``fusermount3`` exists for exactly this (research/rclone-mount-vfs §1.3).
FUSERMOUNT3 = "/usr/bin/fusermount3"

#: The ``/proc`` root the ownership proof reads. Substituted in tests, which
#: cannot fabricate a real process with a chosen argv and start time.
PROC = Path("/proc")

#: ``/proc/<pid>/stat`` field 22 (1-based) is the start time in clock ticks since
#: boot. Fields 1 and 2 cannot be reached by ``split()``: field 2 is the
#: executable name in parentheses and may itself contain spaces and parentheses.
#: Splitting after the LAST ``")"`` puts field 3 at index 0, so field N is at
#: index N - 3.
_STARTTIME_FIELD = 22
_STARTTIME_INDEX = _STARTTIME_FIELD - 3


def read_proc_cmdline(pid: int) -> list[str]:
    """The NUL-separated argv of ``pid``.

    The first half of the ownership proof: our own daemon's argv contains
    ``rcd`` and our exact ``--rc-addr``, and the stranger's on 127.0.0.1:5572
    contains ``mount`` and 5572.

    Args:
        pid: Process id.

    Returns:
        The argv as a list, or ``[]`` when the process is gone or unreadable —
        both of which mean "not proven", which is the safe answer. The trailing
        empty element (``/proc`` NUL-terminates the last argument too) is dropped.
    """
    try:
        raw = (PROC / str(int(pid)) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return []
    parts = raw.decode("utf-8", "replace").split("\0")
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def read_proc_starttime(pid: int) -> int:
    """``/proc/<pid>/stat`` field 22 — the anti-PID-reuse fingerprint.

    Args:
        pid: Process id.

    Returns:
        The start time in clock ticks since boot, or ``0`` when unreadable.
        ``0`` is never a real start time, so a caller may treat it as "unknown".
    """
    try:
        text = (PROC / str(int(pid)) / "stat").read_text(
            encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return 0
    close = text.rfind(")")
    if close < 0:
        return 0
    fields = text[close + 1:].split()
    if len(fields) <= _STARTTIME_INDEX:
        return 0
    try:
        return int(fields[_STARTTIME_INDEX])
    except ValueError:
        return 0


def _mode_of(path: Path, default: int = FILE_MODE) -> int:
    """The current permission bits of ``path``, or ``default`` when it is absent.

    Used when rewriting a file we did not create (``rclone.conf``) so its
    permissions survive the atomic replace — rclone creates that file 0600 and
    widening it would publish the OAuth refresh token to every process on the box.
    """
    try:
        return _stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR):
            raise
        return default
