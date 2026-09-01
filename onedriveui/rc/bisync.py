"""bisync: the opt-in two-way "Offline folder", driven as a subprocess.

bisync is **never** run through the rc. ``sync/bisync`` over the rc behaves as if
``--max-delete 0`` — any deletion aborts the run — and neither
``_config.MaxDelete`` nor ``rclone rcd --max-delete`` changes it; the only escape
is ``force: true``, which disables the delete-percentage check *and* the
"all files changed" check entirely **[V]**. Losing the single most important
safety feature is not a trade we can make, so bisync is a child process launched
as a systemd transient unit, and the rc daemon keeps the jobs it is good at.

The unit is what makes ``SIGINT`` structural (invariant I13)::

    systemd-run --user --collect --unit=onedriveui-bisync-<acc> \\
      --property=KillSignal=SIGINT --property=TimeoutStopSec=150 \\
      --property=Restart=no -- /usr/bin/rclone bisync …

``systemctl --user stop`` then sends **SIGINT**, and rclone's Graceful Shutdown
drains the transfer queue (30 s) and saves its listings (60 s more) before
exiting 130. A ``SIGKILL`` instead leaves ``<name>.<hash>.partial`` at the
destination, which the next run syncs back as a genuine new file — measured
**[V]**, and the reason ``- *.partial`` is in
:data:`~onedriveui.constants.MANDATORY_EXCLUDES`.

Everything else here exists because bisync's state lives in **file names**:

* The session name is ``sanitize(ConfigString(path1)) + ".." +
  sanitize(ConfigString(path2))``, and every workdir file is prefixed with it.
  There is **no hashing fallback**: a session name over ``NAME_MAX`` produces
  ``error reading lock file: … file name too long`` followed by the wildly
  misleading ``Failed to bisync: prior lock file found`` **[V]**. So
  :func:`session_name` refuses up front, where the message can be honest.
* The presence of ``<session>.path1.lst`` is what makes a non-resync run legal;
  ``.lst-err`` is a permanent lockout until ``--resync``. :func:`workdir_state`
  reads exactly that.
* ``<session>.lck`` is JSON, its ``PID`` is a **string**, and with
  ``--max-lock 2m`` its ``TimeExpires`` is ``TimeRenewed + 2m``; with the default
  ``--max-lock 0`` it is ~200 years out. Both captured verbatim **[V]**.

And two refusals that are not negotiable: ``--resync`` requires an **answered**
decision row (invariant I15) — it only ever copies, so a scheduled resync
resurrects every deleted file forever — and ``--inplace`` is never passed
(invariant I12).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import signal
import socket
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from onedriveui import paths
from onedriveui.atomicio import pid_is_alive
from onedriveui.constants import (
    BISYNC_DEFAULT_MAX_DELETE_PCT,
    BISYNC_MAX_LOCK_MIN,
    MAX_CHECKERS,
    MAX_TRANSFERS,
    REMOTE_VERSIONS_DIR,
    UNIT_BISYNC_TMPL,
)
from onedriveui.errors import ConfigError, SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    BisyncState,
    Decision,
    DecisionKind,
    RunKind,
    RunRecord,
    parse_iso,
    utcnow_iso,
)
from onedriveui.rc import RCLONE_DEFAULT, read_proc_cmdline
from onedriveui.rc import filters as _filters
from onedriveui.rc import guards
from onedriveui.rc.bisync_log import LogTailer

__all__ = [
    "DEFAULT_BISYNC_OPTIONS",
    "GRACEFUL_BUDGET_S",
    "LISTING_SUFFIXES",
    "NAME_MAX",
    "RESYNC_APPROVALS",
    "SESSION_JOIN",
    "SESSION_SUFFIX_BUDGET",
    "STOP_SIGNAL",
    "STOP_TIMEOUT_S",
    "SYSTEMD_RUN",
    "WORKDIR_SUFFIXES",
    "BisyncLock",
    "BisyncPlan",
    "WorkdirState",
    "adopt",
    "assert_resync_approved",
    "assert_stop_signal",
    "build_argv",
    "check_file_for",
    "clear_lock",
    "config_string",
    "device_name",
    "interrupt",
    "is_active",
    "is_remote",
    "plan_run",
    "read_lock",
    "run_stamp",
    "sanitize",
    "seed_check_access",
    "session_name",
    "start",
    "stop",
    "systemd_run_argv",
    "unit_name",
    "workdir_state",
]

log = logging.getLogger(__name__)

#: POSIX ``NAME_MAX`` on every filesystem this ships on (ext4, btrfs, xfs, f2fs,
#: tmpfs): 255 **bytes**, not characters. Confirmed with ``pathconf`` **[V]**.
NAME_MAX: Final[int] = 255

#: What bisync appends to the session name. The longest is ``.path1.lst-new``
#: (14 bytes); ``.lck`` is the shortest. A 16-byte budget covers the longest with
#: two bytes to spare, and is what ``session_name()`` validates against.
SESSION_SUFFIX_BUDGET: Final[int] = 16

#: The separator between the two sanitised config strings.
SESSION_JOIN: Final[str] = ".."

#: Every suffix bisync writes into the workdir, verified by listing a real one
#: mid-run and after a graceful shutdown **[V]**.
WORKDIR_SUFFIXES: Final[tuple[str, ...]] = (
    ".path1.lst", ".path2.lst",
    ".path1.lst-old", ".path2.lst-old",
    ".path1.lst-new", ".path2.lst-new",
    ".path1.lst-err", ".path2.lst-err",
    ".lck",
)

#: The two files whose **presence is what makes a non-resync run legal**.
LISTING_SUFFIXES: Final[tuple[str, str]] = (".path1.lst", ".path2.lst")

#: ``systemd-run`` launches the transient unit. Named here so a test can
#: substitute it without a live systemd.
SYSTEMD_RUN: Final[str] = "systemd-run"

#: The ONLY signal that may stop a bisync (invariant I13). rclone's Graceful
#: Shutdown is bound to SIGINT; anything else abandons a ``.partial`` fragment at
#: the destination that the next run treats as a real file **[V]**.
STOP_SIGNAL: Final[int] = int(signal.SIGINT)

#: ``TimeoutStopSec`` on the transient unit. rclone budgets 30 s to drain the
#: transfer queue and up to 60 s more to save its listings; 150 s leaves margin
#: before systemd escalates to SIGKILL.
STOP_TIMEOUT_S: Final[int] = 150

#: How long a caller should wait for ``Graceful shutdown completed
#: successfully.`` before it gives up on a clean stop and reports the run
#: interrupted. Strictly below :data:`STOP_TIMEOUT_S`, so systemd is still the
#: one that escalates.
GRACEFUL_BUDGET_S: Final[int] = 90

#: The answers to a ``resync_confirm`` decision that authorise a ``--resync``
#: (invariant I15). Default-deny: anything else — including
#: ``repo_sync.EXPIRED_ANSWER`` — refuses. ``"resync"`` is the action id the
#: NEEDS_RESYNC toast offers.
RESYNC_APPROVALS: Final[frozenset[str]] = frozenset({
    "resync", "reset", "confirm", "yes", "ok",
})

#: ``accounts[].offline_folder`` (ARCHITECTURE §9.2) plus the two transfer
#: ceilings §5.4 fixes. Injected rather than imported from ``config.py`` so a
#: caller can override any single value per account.
DEFAULT_BISYNC_OPTIONS: Final[dict[str, Any]] = {
    "enabled": False,
    "local_path": "~/OneDrive-Offline",
    "remote_path": "onedrive:Offline",
    "schedule_minutes": 15,
    "max_delete_percent": BISYNC_DEFAULT_MAX_DELETE_PCT,
    "conflict_resolve": "newer",
    "conflict_loser": "pathname",
    "conflict_suffix": "-{device_name}",
    "check_access": True,
    "check_filename": "RCLONE_TEST",
    "max_lock": f"{BISYNC_MAX_LOCK_MIN}m",
    "resilient": True,
    "recover": True,
    "track_renames": True,
    "create_empty_src_dirs": True,
    "compare": "size,modtime",
    "backup_versions": True,
    "transfers": MAX_TRANSFERS,
    "checkers": MAX_CHECKERS,
}

#: bisync's own default, and the only value that needs no ``--compare`` flag.
_DEFAULT_COMPARE: Final[str] = "size,modtime"

#: The characters bisync keeps when sanitising a config string. Everything else
#: becomes ``_``.
_SESSION_KEEP: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


# ─────────────────────────────────────────────────────────────────────────────
# Session naming — the thing with no hashing fallback
# ─────────────────────────────────────────────────────────────────────────────

def sanitize(text: str) -> str:
    """rclone's own session sanitiser.

    Every character outside ``[A-Za-z0-9.-]`` becomes ``_``, and a single leading
    ``_`` is then stripped — which is what turns the leading ``/`` of an absolute
    path into nothing rather than into a leading underscore.

    Args:
        text: One side's config string.

    Returns:
        The sanitised form. ``"/tmp/x/p1"`` → ``"tmp_x_p1"``;
        ``"od:/tmp/y"`` → ``"od__tmp_y"`` (the ``:`` and the ``/`` each become an
        underscore, hence the double); ``"onedrive:"`` → ``"onedrive_"``.
    """
    out = "".join(char if char in _SESSION_KEEP else "_" for char in str(text))
    return out[1:] if out.startswith("_") else out


def is_remote(side: str) -> bool:
    """Whether a bisync side names an rclone remote rather than a local path.

    A Windows drive letter cannot occur on Linux, so a single colon after a bare
    name is unambiguous: ``onedrive:`` and ``onedrive:Offline`` are remotes,
    ``/home/u/OneDrive-Offline`` and ``./x`` are not.

    Args:
        side: A ``path1``/``path2`` argument.

    Returns:
        True for a remote.
    """
    head, sep, _tail = str(side).partition(":")
    return bool(sep) and bool(head) and "/" not in head


def config_string(side: str) -> str:
    """The canonical ``fs.ConfigString`` of one side, as bisync sees it.

    For a remote this is the argument itself (``onedrive:``,
    ``onedrive:Offline``). For a local path it is the **absolute** path with no
    ``<name>:`` prefix — rclone omits the name for a bare local path — so ``~``
    is expanded and a relative path is resolved before naming.

    ``alias`` remotes are resolved to their target *before* naming, so an alias
    around ``onedrive:`` would silently change the session name and orphan the
    listings. Never wrap the account remote in an alias.

    Args:
        side: A ``path1``/``path2`` argument.

    Returns:
        The config string.
    """
    text = str(side)
    if is_remote(text):
        return text
    return os.path.abspath(os.path.expanduser(text))


def session_name(path1: str, path2: str, *,
                 name_max: int = NAME_MAX,
                 suffix_budget: int = SESSION_SUFFIX_BUDGET) -> str:
    """The workdir file prefix bisync will use for this pair — validated.

    ``sanitize(ConfigString(path1)) + ".." + sanitize(ConfigString(path2))``.
    Verified against real runs::

        session_name("/tmp/x/p1", "/tmp/x/p2")  == "tmp_x_p1..tmp_x_p2"
        session_name("/home/u/OneDrive-Offline", "onedrive:Offline")
            == "home_u_OneDrive-Offline..onedrive_Offline"

    **There is no hashing fallback.** When the name exceeds ``NAME_MAX`` rclone
    does not shorten it; it fails to create the lock file and then reports the
    failure as something else entirely — measured with a 480-character session
    **[V]**::

        ERROR : …lck: error reading lock file: … file name too long
        ERROR : …lck: Lock file exists, but contents are unreadable.
        NOTICE: Failed to bisync: prior lock file found: …lck

    A user chasing that message would delete a lock file that never existed. So
    the length is checked here, **before any run starts**, where the error can
    name the real cause.

    Args:
        path1: The local side.
        path2: The remote side.
        name_max: The filesystem's ``NAME_MAX`` in bytes. 255 everywhere this
            ships; overridable for a test or an exotic filesystem.
        suffix_budget: Bytes reserved for the longest suffix bisync appends
            (``.path1.lst-new``).

    Returns:
        The session name.

    Raises:
        ConfigError: The name plus its longest suffix would not fit in a
            filename, quoting both lengths and naming the two paths to shorten.
        ValueError: Either side is empty.
    """
    if not str(path1).strip() or not str(path2).strip():
        raise ValueError("session_name(): both bisync sides are required")
    name = (sanitize(config_string(path1)) + SESSION_JOIN
            + sanitize(config_string(path2)))
    budget = int(name_max) - int(suffix_budget)
    encoded = len(name.encode("utf-8"))
    if encoded > budget:
        raise ConfigError(
            f"the bisync session name is {encoded} bytes and the workdir "
            f"filename limit leaves {budget} (NAME_MAX {name_max} minus "
            f"{suffix_budget} for '.path1.lst-new'). rclone has NO hashing "
            f"fallback: it would fail to create <session>.lck and then report "
            f"'prior lock file found' for a lock that does not exist. Shorten "
            f"the offline folder path ({path1!r}) or the remote path ({path2!r}).")
    return name


def unit_name(account_id: str) -> str:
    """The transient unit for an account's bisync, e.g.
    ``"onedriveui-bisync-onedrive"``.

    Args:
        account_id: The account.

    Returns:
        The unit name, without ``.service`` — ``systemd-run --unit=`` takes it
        bare and ``systemctl`` accepts it either way.
    """
    return UNIT_BISYNC_TMPL.format(account_id)


def device_name() -> str:
    """The short hostname, for ``--conflict-suffix``.

    Mirrors ``hostname -s`` — Windows OneDrive names a conflicted copy
    ``report-DESKTOP-ABC123.docx``, and this is the same idea.

    Returns:
        The first label of the hostname, or ``"localhost"`` when there is none.
    """
    try:
        host = socket.gethostname()
    except OSError:                                          # pragma: no cover
        host = ""
    return (host.split(".", 1)[0] or "localhost")


def run_stamp(when: str | None = None) -> str:
    """A timestamp safe to embed in a **OneDrive** path and in a file suffix.

    ARCHITECTURE §5.4 asks for an ISO-8601 stamp in ``--backup-dir2
    onedrive:.onedriveui-versions/<ts>`` and in ``--suffix "-<ISO8601>"``. The
    ordinary spelling carries colons, and ``:`` is in
    :data:`~onedriveui.constants.INVALID_CHARS`: every backup would fail with
    ``nameContainsInvalidCharacters``. The basic ISO-8601 form has no separators
    and sorts identically.

    Args:
        when: An ISO stamp, defaulting to now.

    Returns:
        ``"20260831T204944Z"``.
    """
    moment = parse_iso(when) if when else None
    if moment is None:
        moment = parse_iso(utcnow_iso())
    if moment is None:                                       # pragma: no cover
        moment = _dt.datetime.now(_dt.UTC)
    return moment.astimezone(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# The lock file
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class BisyncLock:
    """``<session>.lck``, parsed.

    Captured verbatim from a live run with ``--max-lock 2m`` **[V]**::

        {"Session":"…/work/tmp_…_p1..tmp_…_p2","PID":"225703",
         "TimeRenewed":"2026-08-31T20:36:22.751714555-04:00",
         "TimeExpires":"2026-08-31T20:38:22.751714592-04:00"}

    With the default ``--max-lock 0`` the same file carries
    ``"TimeExpires":"2226-07-14T20:36:57.35348904-04:00"`` — ~200 years, i.e.
    never — which is exactly why every run must pass ``--max-lock 2m``: it is
    what lets rclone self-heal after our process is killed.

    Attributes:
        path: The lock file.
        session: rclone's own ``Session`` field — the workdir path plus the
            session name, without a suffix.
        pid: ``PID``, which is a **string** in the JSON.
        time_renewed: ``TimeRenewed``, RFC3339 with a local offset.
        time_expires: ``TimeExpires``.
        readable: False when the file exists but could not be parsed. rclone
            treats an unreadable lock as expired **only** when ``--max-lock >
            0``; otherwise it is a hard block **[V]**.
        alive: Whether a process with that PID exists and still looks like an
            rclone bisync.
        expired: Whether ``TimeExpires`` is in the past.
    """

    path: Path
    session: str = ""
    pid: int = 0
    time_renewed: str = ""
    time_expires: str = ""
    readable: bool = True
    alive: bool = False
    expired: bool = False

    @property
    def stale(self) -> bool:
        """Whether this lock may safely be removed.

        A lock is stale when its process is gone **or** its expiry has passed. A
        live, unexpired lock means a run is genuinely in progress and the UI
        should show a spinner, not a "force unlock" button.
        """
        if not self.readable:
            return True
        return self.expired or not self.alive

    @property
    def running(self) -> bool:
        """Whether a bisync is genuinely in progress behind this lock."""
        return self.readable and self.alive and not self.expired


def _looks_like_bisync(pid: int) -> bool:
    """Whether ``pid`` is plausibly the rclone that wrote this lock.

    PIDs are recycled, and a stale lock naming a PID that now belongs to
    somebody's text editor would keep the account "running" forever. Reading
    ``/proc/<pid>/cmdline`` settles it — except when the cmdline cannot be read
    at all, which is answered **True**: an unreadable process is not proof of
    death, and treating it as dead would delete a live lock out from under a
    running bisync.
    """
    argv = read_proc_cmdline(pid)
    if not argv:
        return True
    joined = " ".join(argv)
    return "rclone" in joined and "bisync" in joined


def read_lock(workdir: Path | str, session: str) -> BisyncLock | None:
    """Read ``<workdir>/<session>.lck`` and decide whether it is stale.

    Args:
        workdir: The bisync ``--workdir``.
        session: From :func:`session_name`.

    Returns:
        The parsed lock, or ``None`` when no lock file exists — which is the
        normal, idle state. An existing-but-unparseable file comes back with
        ``readable=False`` and is therefore :attr:`~BisyncLock.stale`, matching
        rclone's own behaviour under ``--max-lock 2m``.
    """
    path = Path(os.path.expanduser(str(workdir))) / f"{session}.lck"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        log.warning("bisync lock %s is unreadable; treating it as expired "
                    "(--max-lock > 0 is what makes that safe)", path)
        return BisyncLock(path=path, readable=False, alive=False, expired=True)

    try:
        pid = int(str(body.get("PID", "0")).strip() or 0)
    except ValueError:
        pid = 0
    expires = str(body.get("TimeExpires", "") or "")
    moment = parse_iso(expires)
    now = _dt.datetime.now(_dt.UTC)
    expired = bool(moment is not None and moment.astimezone(_dt.UTC) <= now)
    alive = bool(pid > 0 and pid_is_alive(pid) and _looks_like_bisync(pid))
    return BisyncLock(
        path=path,
        session=str(body.get("Session", "") or ""),
        pid=pid,
        time_renewed=str(body.get("TimeRenewed", "") or ""),
        time_expires=expires,
        readable=True,
        alive=alive,
        expired=expired,
    )


# ─────────────────────────────────────────────────────────────────────────────
# The workdir
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class WorkdirState:
    """What the bisync workdir says about this session.

    Attributes:
        workdir: The ``--workdir``.
        session: The session name.
        state: The :class:`~onedriveui.models.BisyncState` the ladder consumes.
        listing1: ``<session>.path1.lst``, whether or not it exists.
        listing2: ``<session>.path2.lst``.
        has_listings: Both ``.lst`` files are present — the precondition for a
            legal non-resync run.
        has_errors: A ``.lst-err`` is present, i.e. a critical abort renamed the
            listings and only ``--resync`` recovers.
        has_new: A ``.lst-new`` is present, i.e. a run aborted before its
            cleanup step. Harmless on its own.
        has_old: A ``.lst-old`` backup is present — what ``--recover`` uses.
        lock: The parsed lock, or ``None``.
    """

    workdir: Path
    session: str
    state: BisyncState
    listing1: Path
    listing2: Path
    has_listings: bool = False
    has_errors: bool = False
    has_new: bool = False
    has_old: bool = False
    lock: BisyncLock | None = None

    @property
    def needs_resync(self) -> bool:
        """Whether the next run must carry ``--resync`` to do anything at all."""
        return self.state is BisyncState.NEEDS_RESYNC


def workdir_state(workdir: Path | str, session: str, *,
                  enabled: bool = True) -> WorkdirState:
    """Read the workdir and decide what the account's bisync state is.

    The ladder, first match wins:

    ============================= ===========================================
    ``enabled`` is False           :attr:`~BisyncState.DISABLED`
    a ``.lst-err`` exists          :attr:`~BisyncState.NEEDS_RESYNC`
    either ``.lst`` is missing     :attr:`~BisyncState.NEEDS_RESYNC`
    a live, unexpired lock         :attr:`~BisyncState.RUNNING`
    a stale lock                   :attr:`~BisyncState.LOCK_STUCK`
    otherwise                      :attr:`~BisyncState.IDLE`
    ============================= ===========================================

    ``.lst-err`` is checked first because it survives: a critical abort renames
    the listings and *then* releases the lock, so a workdir can hold ``.lst-err``
    and no lock at all — measured **[V]**. :attr:`~BisyncState.CRITICAL` is not
    produced here; it is a *verdict* about a run
    (:func:`~onedriveui.rc.bisync_log.classify_verdict`), not a property of the
    directory.

    Args:
        workdir: The ``--workdir``, i.e. ``paths.bisync_workdir(account_id)``.
        session: From :func:`session_name`.
        enabled: ``offline_folder.enabled``.

    Returns:
        The :class:`WorkdirState`.
    """
    root = Path(os.path.expanduser(str(workdir)))
    listing1 = root / f"{session}{LISTING_SUFFIXES[0]}"
    listing2 = root / f"{session}{LISTING_SUFFIXES[1]}"
    has_listings = listing1.is_file() and listing2.is_file()
    has_errors = any((root / f"{session}.path{n}.lst-err").is_file() for n in (1, 2))
    has_new = any((root / f"{session}.path{n}.lst-new").is_file() for n in (1, 2))
    has_old = any((root / f"{session}.path{n}.lst-old").is_file() for n in (1, 2))
    lock = read_lock(root, session)

    if not enabled:
        state = BisyncState.DISABLED
    elif has_errors or not has_listings:
        state = BisyncState.NEEDS_RESYNC
    elif lock is not None and lock.running:
        state = BisyncState.RUNNING
    elif lock is not None:
        state = BisyncState.LOCK_STUCK
    else:
        state = BisyncState.IDLE

    return WorkdirState(
        workdir=root, session=session, state=state,
        listing1=listing1, listing2=listing2,
        has_listings=has_listings, has_errors=has_errors,
        has_new=has_new, has_old=has_old, lock=lock,
    )


# ─────────────────────────────────────────────────────────────────────────────
# --check-access seeding
# ─────────────────────────────────────────────────────────────────────────────

def check_file_for(side: str, filename: str = "RCLONE_TEST") -> str:
    """Where the ``--check-access`` sentinel lives on one side.

    Args:
        side: A ``path1``/``path2`` argument.
        filename: ``--check-filename``.

    Returns:
        A local absolute path for a local side, or a ``remote:path`` string for a
        remote one. Only the name and location matter to rclone; content and
        timestamps are irrelevant.
    """
    if is_remote(side):
        head, _sep, tail = str(side).partition(":")
        #: Only the TRAILING separator is dropped. A leading one is part of the
        #: root for a filesystem-shaped backend (`onedrive:/srv/x`), and eating
        #: it would silently address a relative path instead.
        root = tail.rstrip("/")
        return f"{head}:{root}/{filename}" if root else f"{head}:{filename}"
    return os.path.join(os.path.abspath(os.path.expanduser(str(side))), filename)


def seed_check_access(path1: str, path2: str, *,
                      filename: str = "RCLONE_TEST",
                      copyfile: Any = None) -> list[str]:
    """Create the ``RCLONE_TEST`` sentinels **before** ``--check-access`` is on.

    ``--check-access`` verifies that identically-placed files named
    ``RCLONE_TEST`` exist in *both* listings. rclone **never creates them**, and
    the check is enforced during ``--resync`` too — so the obvious idea of using
    ``bisync --resync --check-access`` to seed them cannot work. Measured, on the
    resync run itself **[V]**::

        NOTICE: --check-access: Failed to find any files named RCLONE_TEST
        ERROR : Access test failed: Path1 count 0, Path2 count 0 - RCLONE_TEST
        ERROR : Bisync critical error: check file check failed
        ERROR : Bisync aborted. Must run --resync to recover.

    With both files present the same command logs ``INFO : Checking access
    health`` and succeeds **[V]**. This is the single best guard against "the
    network blipped" being read as "the user deleted everything", which is why it
    is worth this much care.

    Args:
        path1: The local side.
        path2: The remote side.
        filename: ``--check-filename``.
        copyfile: ``copyfile(src_local_path, dst_side_path)`` — how to place the
            sentinel on a **remote** side. In production an rc
            ``operations/copyfile``; a local ``path2`` needs none.

    Returns:
        The sides actually seeded, as the strings ``"path1"`` / ``"path2"``.
        Empty when both sentinels already existed.

    Raises:
        ConfigError: A remote side needs seeding and no ``copyfile`` was given.
            Enabling ``--check-access`` anyway would abort every run, including
            the resync meant to fix it.
        OSError: The local sentinel could not be created.
    """
    seeded: list[str] = []

    local_target = Path(check_file_for(path1, filename))
    if is_remote(path1):
        raise ConfigError(
            f"seed_check_access(): path1 {path1!r} is a remote; the local side "
            f"must be path1 so the sentinel can be created before it is copied")
    if not local_target.exists():
        local_target.parent.mkdir(parents=True, exist_ok=True)
        local_target.touch()
        seeded.append("path1")

    if is_remote(path2):
        if copyfile is None:
            raise ConfigError(
                f"seed_check_access(): {filename} must exist on {path2!r} before "
                f"--check-access is enabled — rclone never creates it, and the "
                f"check is enforced during --resync too, so a resync cannot seed "
                f"it. Pass a copyfile callable (rc operations/copyfile).")
        copyfile(str(local_target), check_file_for(path2, filename))
        seeded.append("path2")
    else:
        remote_target = Path(check_file_for(path2, filename))
        if not remote_target.exists():
            remote_target.parent.mkdir(parents=True, exist_ok=True)
            remote_target.touch()
            seeded.append("path2")

    if seeded:
        log.info("seeded %s on %s", filename, ", ".join(seeded))
    return seeded


# ─────────────────────────────────────────────────────────────────────────────
# I15 — --resync needs an answered decision
# ─────────────────────────────────────────────────────────────────────────────

def assert_resync_approved(decision: Decision | Mapping[str, Any] | None) -> None:
    """Invariant I15. Refuse a ``--resync`` without an answered decision row.

    ``--resync`` only ever **copies**: both sides end up with a matching
    superset, and nothing is ever deleted. Run on a schedule it resurrects every
    file the user deletes, forever, and leaves both names behind after every
    rename. It is legitimate exactly three times — the first run, immediately
    after a filters change (invariant I11), and to recover from a critical abort
    — and each of those is a moment a human said yes to.

    Args:
        decision: The ``decisions`` row the UI recorded before it showed the
            dialog, as a :class:`~onedriveui.models.Decision` or a mapping.

    Raises:
        SafetyRefusal: invariant ``"I15"`` — there is no decision, it is the
            wrong kind, it was never answered, or the answer was not an approval
            (an *expired* decision is a refusal, matching the 7-day rule).
    """
    if decision is None:
        raise SafetyRefusal(
            "I15",
            "--resync requires an ANSWERED decisions row: it only copies, never "
            "deletes, so an unattended resync resurrects every deleted file and "
            "leaves a duplicate for every rename")

    if isinstance(decision, Mapping):
        kind = str(decision.get("kind", ""))
        answered_at = decision.get("answered_at")
        answer = decision.get("answer")
    else:
        kind = str(decision.kind)
        answered_at = decision.answered_at
        answer = decision.answer

    if kind != str(DecisionKind.RESYNC_CONFIRM):
        raise SafetyRefusal(
            "I15",
            f"--resync was authorised by a {kind!r} decision; it needs a "
            f"{str(DecisionKind.RESYNC_CONFIRM)!r} one")
    if not answered_at:
        raise SafetyRefusal(
            "I15", "the resync decision has not been answered yet")
    if str(answer or "").strip().lower() not in RESYNC_APPROVALS:
        raise SafetyRefusal(
            "I15",
            f"the resync decision was answered {answer!r}, which is not an "
            f"approval (expected one of {sorted(RESYNC_APPROVALS)})")


def assert_stop_signal(sig: int) -> None:
    """Invariant I13. Refuse to stop a bisync with anything but ``SIGINT``.

    A ``SIGKILL`` — or a ``SIGTERM``, which rclone does not bind to Graceful
    Shutdown for bisync — abandons ``<name>.<hash>.partial`` at the destination.
    The next run lists it as a brand-new file and syncs the fragment everywhere,
    measured **[V]**::

        INFO  : - Path2    File is new    - big.bin.677c7953.partial
        INFO  : big.bin.677c7953.partial: Copied (new)

    Args:
        sig: The signal a caller intends to send.

    Raises:
        SafetyRefusal: invariant ``"I13"`` for anything but ``SIGINT``.
    """
    if int(sig) != STOP_SIGNAL:
        raise SafetyRefusal(
            "I13",
            f"bisync may only be stopped with SIGINT ({STOP_SIGNAL}), not "
            f"{int(sig)}: any other signal skips rclone's Graceful Shutdown and "
            f"leaves a .partial fragment the next run syncs as a real file")


# ─────────────────────────────────────────────────────────────────────────────
# The command line
# ─────────────────────────────────────────────────────────────────────────────

def _options(opts: Mapping[str, Any] | None) -> dict[str, Any]:
    """``offline_folder`` with every default filled in."""
    merged = dict(DEFAULT_BISYNC_OPTIONS)
    merged.update(opts or {})
    return merged


def build_argv(account: AccountInfo,
               opts: Mapping[str, Any] | None = None,
               *,
               run_id: str,
               resync: bool = False,
               resync_mode: str = "",
               resync_decision: Decision | Mapping[str, Any] | None = None,
               force: bool = False,
               filters_changed: bool = False,
               stamp: str = "",
               device: str = "",
               rclone_path: str = RCLONE_DEFAULT,
               extra_args: Sequence[str] = ()) -> list[str]:
    """The bisync command line of ARCHITECTURE §5.4.

    Every choice is a decision that was measured:

    * ``--workdir`` under ``~/.local/state``, **never**
      ``~/.cache/rclone/bisync`` — rclone's own cache cleaning may destroy the
      ``.lst`` files that *are* the sync state.
    * ``--color NEVER`` is mandatory even with ``--use-json-log``: without it the
      ``msg`` fields carry raw ANSI escapes **[V]**.
    * ``--max-lock 2m`` so rclone can self-heal a lock left by a killed process.
      Anything smaller is silently raised to 2 minutes.
    * ``--resilient --recover`` — the "unreliable link" pair — so a transient
      abort does not demand a resync.
    * ``--track-renames`` is **dropped on a resync run**: it does not work with
      copy, and bisync says so at *ERROR* level on every resync, which would
      otherwise pollute the issue list.
    * No ``--inplace`` (I12), no ``--no-cleanup``, no backend flag (I1).

    Args:
        account: The account. ``account.id`` names the workdir, the filters file
            and the version store.
        opts: The ``offline_folder`` block. Missing keys come from
            :data:`DEFAULT_BISYNC_OPTIONS`.
        run_id: The run's id; ``--log-file`` goes to ``paths.run_log_file()``.
        resync: Add ``--resync``. Requires ``resync_decision`` (I15).
        resync_mode: ``--resync-mode``; empty means rclone's default (``path1``
            when ``--resync`` is set).
        resync_decision: The answered ``resync_confirm`` decision (I15).
        force: Add ``--force``, bypassing the ``--max-delete`` **and**
            "all files changed" checks. Only ever after the user answered a
            ``mass_delete`` decision.
        filters_changed: Whether the filters file was just rewritten. Checked
            against I11 by :func:`~onedriveui.rc.guards.assert_bisync_safe`,
            which refuses unless ``resync`` is set in the same call.
        stamp: The version timestamp; defaults to now (:func:`run_stamp`).
        device: The short hostname for ``--conflict-suffix``; defaults to
            :func:`device_name`.
        rclone_path: ``advanced.rclone_path``.
        extra_args: Appended verbatim, then checked against I1 and I12 like
            everything else.

    Returns:
        The full argv, the rclone binary first.

    Raises:
        SafetyRefusal: ``"I2"`` a side is under a fuse mount or the two overlap;
            ``"I11"`` the filters changed without a resync; ``"I12"``
            ``--inplace``; ``"I13"`` the filters file lacks ``- *.partial``;
            ``"I15"`` ``--resync`` without an answered decision; ``"I1"`` a
            backend option reached the command line.
        ConfigError: ``--check-access`` is on but the local ``RCLONE_TEST``
            sentinel does not exist (see :func:`seed_check_access`), or the
            session name would not fit in a filename.
    """
    opt = _options(opts)
    path1 = os.path.abspath(os.path.expanduser(str(opt["local_path"])))
    path2 = str(opt["remote_path"])
    session = session_name(path1, path2)
    workdir = paths.bisync_workdir(account.id)
    filters_file = paths.filters_file(account.id)
    check_filename = str(opt["check_filename"] or "RCLONE_TEST")

    if resync:
        assert_resync_approved(resync_decision)

    guards.assert_bisync_safe(path1, path2, _filters.filters_config(
        account.id, changed=filters_changed, resync=resync,
        extra_args=extra_args))

    if bool(opt["check_access"]) and not Path(
            check_file_for(path1, check_filename)).exists():
        raise ConfigError(
            f"--check-access is enabled but {check_filename!r} does not exist at "
            f"{path1!r}. rclone never creates it and enforces the check during "
            f"--resync too, so every run would abort critically. Call "
            f"bisync.seed_check_access() first.")

    stamp = stamp or run_stamp()
    device = device or device_name()
    suffix_template = str(opt["conflict_suffix"] or "-{device_name}")
    try:
        conflict_suffix = suffix_template.format(device_name=device)
    except (KeyError, IndexError):
        conflict_suffix = suffix_template

    argv: list[str] = [
        rclone_path, "bisync", path1, path2,
        "--workdir", str(workdir),
        "--filters-file", str(filters_file),
    ]
    if resync:
        argv.append("--resync")
        if resync_mode:
            argv += ["--resync-mode", str(resync_mode)]
    else:
        argv += [
            "--conflict-resolve", str(opt["conflict_resolve"]),
            "--conflict-loser", str(opt["conflict_loser"]),
            "--conflict-suffix", conflict_suffix,
        ]
    argv += ["--max-delete", str(int(opt["max_delete_percent"]))]
    if force:
        argv.append("--force")
    if bool(opt["check_access"]):
        argv += ["--check-access", "--check-filename", check_filename]
    argv += ["--max-lock", str(opt["max_lock"] or f"{BISYNC_MAX_LOCK_MIN}m")]
    if bool(opt["resilient"]):
        argv.append("--resilient")
    if bool(opt["recover"]):
        argv.append("--recover")
    if bool(opt["create_empty_src_dirs"]):
        argv.append("--create-empty-src-dirs")
    #: --track-renames is IGNORED during a resync and says so at ERROR level on
    #: every single one; dropping it is cleaner than whitelisting the noise.
    if bool(opt["track_renames"]) and not resync:
        argv.append("--track-renames")
    compare = str(opt["compare"] or _DEFAULT_COMPARE)
    if compare != _DEFAULT_COMPARE:
        argv += ["--compare", compare]
    if bool(opt["backup_versions"]):
        local_backup = paths.versions_dir(account.id) / stamp
        remote_backup = f"{account.fs}{REMOTE_VERSIONS_DIR}/{stamp}"
        argv += [
            "--backup-dir1", str(local_backup),
            "--backup-dir2", remote_backup,
            "--suffix", f"-{stamp}",
            "--suffix-keep-extension",
        ]
    argv += [
        "--transfers", str(int(opt["transfers"])),
        "--checkers", str(int(opt["checkers"])),
        "--use-json-log", "--color", "NEVER",
        #: MANDATORY. rclone's default log level is NOTICE, and every milestone
        #: bisync emits — including the terminal `Bisync successful` the verdict
        #: is read from — is INFO. Measured: the same run without this wrote a
        #: log containing ONE line, the stats block, and classify_verdict() had
        #: nothing to work with **[V]**.
        "--log-level", "INFO",
        "--stats", "500ms", "--stats-log-level", "NOTICE",
        "--log-file", str(paths.run_log_file(run_id)),
    ]
    argv += [str(a) for a in extra_args]

    guards.assert_no_backend_flags(argv)
    guards.assert_no_inplace(argv)
    return argv


def systemd_run_argv(account_id: str, argv: Sequence[str], *,
                     systemd_run: str = SYSTEMD_RUN,
                     stop_timeout_s: int = STOP_TIMEOUT_S) -> list[str]:
    """Wrap a bisync argv in the transient unit that makes ``SIGINT`` structural.

    ``KillSignal=SIGINT`` is the whole point (invariant I13): a plain
    ``systemctl --user stop`` then triggers rclone's Graceful Shutdown rather
    than the ``SIGTERM``/``SIGKILL`` that would strand a ``.partial`` fragment.
    ``Restart=no`` matters as much — a bisync that failed must never be retried
    by systemd behind the state machine's back — and ``--collect`` reaps the
    failed unit so the next run can reuse the name.

    Args:
        account_id: The account, which names the unit.
        argv: The command line from :func:`build_argv`.
        systemd_run: The launcher binary, overridable for tests.
        stop_timeout_s: ``TimeoutStopSec``. Must exceed rclone's 30 s drain plus
            60 s save budget.

    Returns:
        The full ``systemd-run`` command line.
    """
    return [
        systemd_run, "--user", "--collect",
        f"--unit={unit_name(account_id)}",
        "--property=KillSignal=SIGINT",
        f"--property=TimeoutStopSec={int(stop_timeout_s)}",
        "--property=Restart=no",
        "--", *[str(a) for a in argv],
    ]


@dataclass(frozen=True, slots=True)
class BisyncPlan:
    """Everything the supervisor needs to start, watch and record one run.

    Attributes:
        account_id: The account.
        run_id: The run id, which names the run directory.
        kind: ``"resync"`` when ``--resync`` is present, else ``"bisync"``.
        path1: The local side, absolute.
        path2: The remote side.
        session: The workdir prefix.
        workdir: ``--workdir``.
        unit: The transient unit name.
        log_path: ``--log-file``, which the ``LogTailer`` follows.
        argv: The rclone command line.
        launch_argv: The ``systemd-run`` wrapper around it.
        state: The workdir state observed while planning.
    """

    account_id: str
    run_id: str
    kind: str
    path1: str
    path2: str
    session: str
    workdir: Path
    unit: str
    log_path: Path
    argv: tuple[str, ...]
    launch_argv: tuple[str, ...]
    state: WorkdirState

    def to_run_record(self) -> RunRecord:
        """Project this plan onto the ``runs`` row the supervisor inserts.

        Returns:
            A :class:`~onedriveui.models.RunRecord` with ``argv``, ``unit``,
            ``session`` and both listing paths filled in, ``started_at`` set to
            now and ``log_offset`` at 0.
        """
        return RunRecord(
            run_id=self.run_id,
            account_id=self.account_id,
            kind=RunKind.RESYNC if self.kind == "resync" else RunKind.BISYNC,
            argv=self.argv,
            started_at=utcnow_iso(),
            log_path=str(self.log_path),
            log_offset=0,
            unit=self.unit,
            session=self.session,
            listing1=str(self.state.listing1),
            listing2=str(self.state.listing2),
        )


def plan_run(account: AccountInfo, opts: Mapping[str, Any] | None = None, *,
             run_id: str, resync: bool = False,
             **kwargs: Any) -> BisyncPlan:
    """Assemble a whole run without starting anything.

    Building the plan performs every refusal :func:`build_argv` performs, so a
    caller learns that a run is impossible **before** it has written a ``runs``
    row, shown a spinner or touched the workdir.

    Args:
        account: The account.
        opts: The ``offline_folder`` block.
        run_id: The run id.
        resync: Whether this is a resync run.
        **kwargs: Passed through to :func:`build_argv`.

    Returns:
        The :class:`BisyncPlan`.

    Raises:
        SafetyRefusal: Any of I1, I2, I11, I12, I13, I15.
        ConfigError: The session name does not fit, or ``--check-access`` is on
            without a seeded sentinel.
    """
    opt = _options(opts)
    path1 = os.path.abspath(os.path.expanduser(str(opt["local_path"])))
    path2 = str(opt["remote_path"])
    argv = build_argv(account, opt, run_id=run_id, resync=resync, **kwargs)
    session = session_name(path1, path2)
    workdir = paths.bisync_workdir(account.id)
    return BisyncPlan(
        account_id=account.id,
        run_id=run_id,
        kind="resync" if resync else "bisync",
        path1=path1,
        path2=path2,
        session=session,
        workdir=workdir,
        unit=unit_name(account.id),
        log_path=paths.run_log_file(run_id),
        argv=tuple(argv),
        launch_argv=tuple(systemd_run_argv(account.id, argv)),
        state=workdir_state(workdir, session, enabled=bool(opt["enabled"])),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Starting, adopting and stopping
# ─────────────────────────────────────────────────────────────────────────────

def _run(argv: Sequence[str], *, timeout_s: float,
         runner: Any = None) -> tuple[int, str]:
    """Run a short command, returning ``(returncode, stdout)``.

    Never raises: a missing ``systemctl`` is a "no" about the unit, not a crash
    in the fact tick that asked.
    """
    call = runner or subprocess.run
    try:
        done = call([str(a) for a in argv], capture_output=True, text=True,
                    timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", " ".join(str(a) for a in argv), exc)
        return 127, ""
    return int(getattr(done, "returncode", 1) or 0), str(getattr(done, "stdout", "") or "")


def start(plan: BisyncPlan, *, runner: Any = None,
          timeout_s: float = 30.0) -> bool:
    """Launch a planned run as its transient unit.

    The plan has already survived every refusal (:func:`plan_run`), so this only
    hands ``systemd-run`` the argv. The unit does the rest: ``KillSignal=SIGINT``
    makes a later :func:`stop` graceful (I13), and ``Restart=no`` keeps systemd
    from re-running a failed bisync behind the state machine's back.

    Args:
        plan: From :func:`plan_run`.
        runner: A ``subprocess.run``-compatible callable, for tests.
        timeout_s: How long to wait for ``systemd-run`` to return. It returns as
            soon as the unit is queued, not when the run finishes.

    Returns:
        True when the unit was accepted. False otherwise — including when a unit
        of that name is already running, which is systemd refusing a second
        concurrent bisync for the account and is the right answer.
    """
    code, _out = _run(plan.launch_argv, timeout_s=timeout_s, runner=runner)
    if code == 0:
        log.info("started %s for %r (run %s)", plan.unit, plan.account_id,
                 plan.run_id)
    else:
        log.error("systemd-run refused %s for %r (exit %d)", plan.unit,
                  plan.account_id, code)
    return code == 0


def clear_lock(workdir: Path | str, session: str, *, force: bool = False) -> bool:
    """Remove a **stale** ``<session>.lck`` — the "Unlock sync" recovery action.

    rclone's own advice on a stuck run is ``rclone deletefile "<…>.lck"``, which
    is unconditional. This is not: a lock whose process is alive and whose expiry
    has not passed protects a bisync that is genuinely running, and deleting it
    would let a second run start against the same listings.

    Args:
        workdir: The ``--workdir``.
        session: From :func:`session_name`.
        force: Delete even a live lock. Only ever after the user answered a
            ``force_unlock`` decision, and only when the mount and the process
            are both known dead.

    Returns:
        True if a lock file was removed. False when there was none.

    Raises:
        SafetyRefusal: invariant ``"I3"`` — the lock is live and ``force`` is
            not set. A bisync is in flight; unlocking it would let a second run
            delete on both sides from a stale snapshot.
    """
    lock = read_lock(workdir, session)
    if lock is None:
        return False
    if lock.running and not force:
        raise SafetyRefusal(
            "I3",
            f"{str(lock.path)!r} is a LIVE lock: pid {lock.pid} is still running "
            f"and it expires at {lock.time_expires!r}. Removing it would let a "
            f"second bisync run against the same listings and delete on both "
            f"sides from a stale snapshot")
    lock.path.unlink(missing_ok=True)
    log.info("removed the %s bisync lock %s",
             "live" if lock.running else "stale", lock.path)
    return True


def is_active(account_id: str, *, runner: Any = None,
              timeout_s: float = 2.0) -> bool:
    """Whether the account's bisync unit is running right now.

    ``systemctl --user is-active`` is the probe ARCHITECTURE §5.7 names for an
    orphaned run: the GUI can die and be restarted while a bisync keeps going in
    its own transient unit, and this is how the new process finds out.

    Args:
        account_id: The account.
        runner: A ``subprocess.run``-compatible callable, for tests.
        timeout_s: How long to wait for systemctl.

    Returns:
        True while the unit reports ``active`` or ``activating``. False for
        anything else, including a missing systemctl — never raises.
    """
    _code, out = _run(["systemctl", "--user", "is-active", unit_name(account_id)],
                      timeout_s=timeout_s, runner=runner)
    return out.strip() in ("active", "activating")


def adopt(account_id: str, run: RunRecord | None = None, *,
          runner: Any = None, poll_ms: int = 250,
          timeout_s: float = 2.0) -> LogTailer | None:
    """Re-attach to a bisync that outlived the GUI.

    The mount and the rcd survive a GUI crash on purpose, and so does a running
    bisync: it is a transient systemd unit, not a child of ours. On relaunch the
    supervisor asks ``systemctl --user is-active``, and if the answer is yes it
    resumes the log tail **at the stored byte offset** rather than from the
    beginning — replaying would duplicate every conflict and activity row the
    previous process already wrote.

    Args:
        account_id: The account.
        run: The in-flight ``runs`` row, supplying ``log_path`` and
            ``log_offset``.
        runner: A ``subprocess.run``-compatible callable, for tests.
        poll_ms: The tailer's poll interval.
        timeout_s: How long to wait for systemctl.

    Returns:
        An **unstarted** :class:`~onedriveui.rc.bisync_log.LogTailer` positioned
        at ``run.log_offset`` — connect its signals, then ``start()`` it. ``None``
        when the unit is not running, or when there is no run row or log path to
        resume from; use :func:`is_active` when only the yes/no matters.
    """
    if not is_active(account_id, runner=runner, timeout_s=timeout_s):
        return None
    if run is None or not run.log_path:
        log.info("bisync unit for %r is active but no run row names its log; "
                 "cannot re-attach the tailer", account_id)
        return None
    log.info("adopting the running bisync for %r, resuming its log at byte %d",
             account_id, run.log_offset)
    return LogTailer(run.log_path, offset=int(run.log_offset), poll_ms=poll_ms)


def stop(account_id: str, *, runner: Any = None,
         timeout_s: float = float(STOP_TIMEOUT_S)) -> bool:
    """Stop a running bisync — **with SIGINT, always** (invariant I13).

    ``systemctl --user stop`` sends the unit's ``KillSignal``, which
    :func:`systemd_run_argv` pinned to ``SIGINT``. rclone then drains the
    transfer queue for 30 s, saves its listings, and exits 130 — leaving
    ``.lst`` and ``.lst-old`` in a clean, resumable state **[V]**. The caller
    should wait up to :data:`GRACEFUL_BUDGET_S` for ``Graceful shutdown
    completed successfully.`` in the log and show a "Finishing up…" state
    meanwhile.

    Args:
        account_id: The account.
        runner: A ``subprocess.run``-compatible callable, for tests.
        timeout_s: How long to wait for systemctl to return.

    Returns:
        True when systemctl reported success. False otherwise — including when
        the unit was not running, which is not an error.
    """
    assert_stop_signal(STOP_SIGNAL)
    code, _out = _run(["systemctl", "--user", "stop", unit_name(account_id)],
                      timeout_s=timeout_s, runner=runner)
    if code == 0:
        log.info("sent SIGINT to the bisync unit for %r via systemctl stop",
                 account_id)
    return code == 0


def interrupt(pid: int, *, sig: int = STOP_SIGNAL, killer: Any = None) -> bool:
    """Signal a bisync process directly, refusing anything but ``SIGINT``.

    The systemd path (:func:`stop`) is the normal one. This exists for a run this
    process launched itself — a ``Popen`` in a test harness or a fallback when
    systemd is unavailable — and it carries the same refusal, so neither route
    can reach ``SIGKILL``.

    Args:
        pid: The rclone process id, from the lock file or the ``Popen``.
        sig: The signal. Anything but ``SIGINT`` is refused.
        killer: An ``os.kill``-compatible callable, for tests.

    Returns:
        True when the signal was delivered. False when the process was already
        gone or is not ours to signal.

    Raises:
        SafetyRefusal: invariant ``"I13"`` for any signal but ``SIGINT``.
    """
    assert_stop_signal(sig)
    if pid <= 0:
        return False
    send = killer or os.kill
    try:
        send(int(pid), STOP_SIGNAL)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        log.debug("could not SIGINT pid %s: %s", pid, exc)
        return False
    return True
