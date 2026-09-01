"""The data plane: ``onedriveui-mount@<account>.service``, liveness and recovery.

The mount is owned by systemd, not by the GUI and not by the rc daemon. That
buys ``Restart=on-failure``, an ``ExecStop`` that always runs, ``sd_notify``
readiness — ``Type=notify`` means the unit is ``active`` only once the mount is
actually usable — and a free telemetry channel in ``StatusText``. It also means a
GUI crash cannot unmount the user's files.

Three facts shape everything here, all measured on this machine:

* **A dead mount is indistinguishable from a live one in ``/proc``.** After a
  ``SIGKILL`` the ``/proc/self/mounts`` line survives, ``os.path.ismount()``
  still returns ``True``, and every access fails with ``ENOTCONN`` (errno 107).
  Liveness therefore needs the ``/proc`` line **and** a ``statvfs()`` that does
  not raise — invariant I6. Recovery is exactly one command, ``fusermount3 -uz``.
* **``mount/mount`` is banned** (invariant I7). A duplicate VFS is permanently
  unaddressable, and ``mount/listmounts`` cannot even see a CLI-started mount, so
  ``/proc/self/mounts`` is the only honest enumeration.
* **No backend flag may reach the argv** (invariant I1). ``build_argv()`` checks
  its own output, so the ``onedrive{HASH}:`` rename that orphans the whole VFS
  cache cannot be introduced by editing this file.
"""

from __future__ import annotations

import errno
import logging
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer

from onedriveui import APP_DISPLAY_NAME, USER_AGENT, paths
from onedriveui.bus import BUS
from onedriveui.constants import (
    DEFAULT_LOW_LEVEL_RETRIES,
    DEFAULT_RETRIES,
    DEFAULT_TPSLIMIT,
    DEFAULT_TPSLIMIT_BURST,
    MANDATORY_EXCLUDES,
    MAX_CHECKERS,
    MAX_TRANSFERS,
    MOUNT_RESTART_LADDER_S,
    MOUNT_RESTART_MAX_PER_HOUR,
    ORDERING_DAEMON,
    UNIT_MOUNT_TMPL,
)
from onedriveui.errors import ConfigError, MountLost, RcError, SafetyRefusal
from onedriveui.models import AccountInfo, MountHealth, RcEndpoint
from onedriveui.rc import FUSERMOUNT3, RCLONE_DEFAULT
from onedriveui.rc import endpoints as _endpoints
from onedriveui.rc import guards
from onedriveui.rc.client import call_blocking, is_alive
from onedriveui.rc.daemon import SystemdLike, unit_escape

__all__ = [
    "DEFAULT_MOUNT_OPTIONS",
    "MOUNT_EXCLUDE_DIRS",
    "MountController",
    "fusermount_unmount",
    "is_live",
    "rclone_mounts",
    "systemctl_status_text",
]

log = logging.getLogger(__name__)

#: The three directory trees §5.3 keeps out of the mount, derived from the frozen
#: :data:`~onedriveui.constants.MANDATORY_EXCLUDES` so the mount argv and the
#: bisync filters file can never drift apart. `.Trash-1000/` is the file-manager
#: trash created *inside* the mount, which would otherwise be uploaded; the other
#: two are our own recycle bin and version store on the remote.
MOUNT_EXCLUDE_DIRS: tuple[str, ...] = tuple(
    rule[2:-1] for rule in MANDATORY_EXCLUDES
    if rule.startswith("- ") and rule.endswith("/")
)

#: The `accounts[].mount` block of ARCHITECTURE §9.2, which is also exactly the
#: argv of §5.3. `MountController` takes a provider so `config.py` (WP-01) can
#: override any of them per account; these are the values the specification
#: fixes, and every one of them is a decision:
#:
#:   * `vfs_cache_max_age_hours = 720`, not rclone's 1 h default, which would
#:     evict a file the user just marked "Always keep on this device".
#:   * `read_chunk_streams = 0`. Parallel streams cause Graph 429s.
#:   * `poll_interval_s` must stay strictly below `dir_cache_time_s` or polling
#:     buys nothing.
#:   * `transfers` and `checkers` are capped at OneDrive Personal's real limits.
DEFAULT_MOUNT_OPTIONS: dict[str, Any] = {
    "enabled": True,
    "cache_dir": "~/.cache/rclone",
    "vfs_cache_max_size_gb": 50,
    "vfs_cache_max_age_hours": 720,
    "vfs_cache_min_free_space_gb": 5,
    "vfs_cache_poll_interval_s": 60,
    "poll_interval_s": 60,
    "dir_cache_time_s": 3600,
    "attr_timeout_ms": 1000,
    "read_chunk_size_mb": 32,
    "read_chunk_size_limit_mb": 512,
    "read_chunk_streams": 0,
    "write_back_s": 5,
    "handle_caching_s": 5,
    "transfers": MAX_TRANSFERS,
    "checkers": MAX_CHECKERS,
    "tpslimit": DEFAULT_TPSLIMIT,
    "tpslimit_burst": DEFAULT_TPSLIMIT_BURST,
    "retries": DEFAULT_RETRIES,
    "low_level_retries": DEFAULT_LOW_LEVEL_RETRIES,
    "umask": "022",
    "file_perms": "0644",
    "dir_perms": "0755",
    "fast_fingerprint": True,
    "links": False,
    "allow_other": False,
    "warm_up_on_start": False,
    "extra_args": [],
}

#: `statvfs()` errnos that mean "the FUSE daemon behind this mountpoint is gone".
_STALE_ERRNOS = frozenset({
    errno.ENOTCONN, errno.ENODEV, errno.EIO, errno.ESTALE, errno.ECONNABORTED,
})

_SECONDS_PER_HOUR = 3600


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers — these produce §5.3's exact spellings
# ─────────────────────────────────────────────────────────────────────────────

def _dur(seconds: float) -> str:
    """Seconds as rclone's compact duration: ``3600`` → ``"1h"``, ``60`` → ``"1m"``."""
    value = int(seconds)
    if value and value % _SECONDS_PER_HOUR == 0:
        return f"{value // _SECONDS_PER_HOUR}h"
    if value and value % 60 == 0:
        return f"{value // 60}m"
    return f"{value}s"


def _dur_ms(milliseconds: float) -> str:
    """Milliseconds as a duration: ``1000`` → ``"1s"``, ``250`` → ``"250ms"``."""
    value = int(milliseconds)
    if value and value % 1000 == 0:
        return f"{value // 1000}s"
    return f"{value}ms"


def _num(value: float) -> str:
    """``8.0`` → ``"8"``, ``8.5`` → ``"8.5"`` — rclone accepts both, the doc says 8."""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


# ─────────────────────────────────────────────────────────────────────────────
# I6 — liveness
# ─────────────────────────────────────────────────────────────────────────────

def rclone_mounts() -> list[tuple[str, Path]]:
    """Every ``fuse.rclone`` mount on this machine, from ``/proc/self/mounts``.

    Returns:
        ``[(fs_name, mountpoint)]``. ``fs_name`` is the raw device field the
        kernel recorded — ``onedrive{MxOuf}:`` for a mount started with backend
        flags. Strip the suffix before display; never before comparing.

        This is the *only* reliable enumeration: ``mount/listmounts`` reports
        ``[]`` for a CLI-started mount and is banned anyway (I7).
    """
    return list(paths.fuse_rclone_mounts())


def is_live(mountpoint: Path | str | os.PathLike[str]) -> MountHealth:
    """Invariant I6: liveness needs a ``/proc`` line **and** a working ``statvfs``.

    ``os.path.ismount()`` alone is not enough. After ``kill -9`` on an rclone
    mount the kernel keeps the mount entry, ``ismount()`` keeps saying ``True``,
    and every filesystem call returns ``ENOTCONN`` (errno 107) — a state that is
    fixed by ``fusermount3 -uz`` and by nothing else.

    Args:
        mountpoint: The path to probe.

    Returns:
        ``DOWN`` when no ``fuse.rclone`` line names this path, ``STALE`` when one
        does but ``statvfs()`` fails, and ``UP`` when both agree.

        ``statvfs()`` on a live rclone mount is answered from the VFS's cached
        ``about`` data and does not touch the network, so this is cheap enough to
        call on every fact tick.
    """
    target = Path(os.path.realpath(os.path.expanduser(str(mountpoint))))
    mounted = any(target == mount for _fs, mount in rclone_mounts())
    if not mounted:
        return MountHealth.DOWN
    try:
        os.statvfs(str(target))
    except OSError as exc:
        if exc.errno in _STALE_ERRNOS:
            return MountHealth.STALE
        if exc.errno == errno.ENOENT:
            return MountHealth.DOWN
        log.warning("statvfs(%s) failed with errno %s; treating the mount as stale",
                    target, exc.errno)
        return MountHealth.STALE
    return MountHealth.UP


def fusermount_unmount(mountpoint: Path | str | os.PathLike[str], *,
                       lazy: bool = True, timeout_s: float = 10.0,
                       binary: str = FUSERMOUNT3) -> bool:
    """Tear a mount down with the setuid FUSE helper.

    ``umount(8)`` needs root for a user FUSE mount; ``fusermount3`` exists for
    exactly this. Plain ``-u`` fails with ``EBUSY`` when any process holds a cwd
    or an fd inside the mount, so a desktop client wants ``-uz``, which detaches
    immediately and always succeeds — including on the ``ENOTCONN`` corpse a
    ``SIGKILL`` leaves behind.

    Args:
        mountpoint: The path to unmount.
        lazy: Use ``-uz`` (detach now, clean up when the last fd closes) rather
            than ``-u``.
        timeout_s: How long to wait for the helper.
        binary: Override the helper path.

    Returns:
        True if the helper exited 0, or if nothing was mounted there to begin
        with. False on a non-zero exit, a timeout or a missing helper — all of
        which are logged with the helper's stderr.
    """
    target = str(Path(os.path.expanduser(str(mountpoint))))
    argv = [binary, "-uz" if lazy else "-u", target]
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("%s failed: %s", " ".join(argv), exc)
        return False
    if done.returncode != 0:
        stderr = (done.stderr or "").strip()
        if "not found in /etc/mtab" in stderr or "not mounted" in stderr:
            return True
        log.error("%s exited %d: %s", " ".join(argv), done.returncode, stderr)
        return False
    log.info("unmounted %s with %s", target, argv[1])
    return True


def systemctl_status_text(unit: str, *, timeout_s: float = 2.0) -> str:
    """``systemctl --user show -p StatusText <unit>``, parsed.

    ``Type=notify`` makes rclone push a live status line that systemd exposes
    here, e.g. ``[23:29] vfs cache: objects 3 (was 3) in use 0, to upload 0,
    uploading 0, total size 2.525Mi``. It is a zero-cost health channel that
    still works when the rc port does not, which is exactly when it is needed.

    Args:
        unit: The unit name.
        timeout_s: How long to wait for systemctl.

    Returns:
        The status line, or ``""`` when the unit is unknown, has no status text,
        or systemctl is unavailable. Never raises.
    """
    argv = ["systemctl", "--user", "show", "-p", "StatusText", "--value", unit]
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", " ".join(argv), exc)
        return ""
    if done.returncode != 0:
        return ""
    text = (done.stdout or "").strip()
    # `--value` is honoured by systemd >= 230; fall back to splitting the pair.
    if text.startswith("StatusText="):
        text = text[len("StatusText="):].strip()
    return text


# ─────────────────────────────────────────────────────────────────────────────
# The controller
# ─────────────────────────────────────────────────────────────────────────────

class MountController(QObject):
    """Owns one ``onedriveui-mount@<account>.service`` per account."""

    def __init__(self, systemd: SystemdLike, *,
                 options: Callable[[AccountInfo], Mapping[str, Any]] | None = None,
                 rclone_path: str = RCLONE_DEFAULT,
                 user_agent: str = USER_AGENT,
                 schedule: Callable[[int, Callable[[], None]], None] | None = None,
                 parent: QObject | None = None) -> None:
        """
        Args:
            systemd: The injected service manager (:class:`SystemdLike`).
            options: Returns the ``accounts[].mount`` block for an account.
                Defaults to :data:`DEFAULT_MOUNT_OPTIONS`. Injected rather than
                imported so WP-02 does not depend on WP-01's ``config.py``.
            rclone_path: ``advanced.rclone_path``.
            user_agent: ``advanced.user_agent``. The ``ISV|Company|App/Version``
                shape is load-bearing for Microsoft's throttle prioritisation.
            schedule: ``(delay_ms, callback)`` scheduler for the restart ladder.
                Defaults to ``QTimer.singleShot``; tests pass one that runs the
                callback inline.
            parent: Owner, for Qt lifetime.
        """
        super().__init__(parent)
        self._systemd = systemd
        self._options = options or (lambda _account: DEFAULT_MOUNT_OPTIONS)
        self._rclone_path = rclone_path
        self._user_agent = user_agent
        self._schedule = schedule or (lambda ms, fn: QTimer.singleShot(ms, fn))
        self._endpoints: dict[str, RcEndpoint] = {}
        self._health: dict[str, MountHealth] = {}
        self._restarts: dict[str, list[float]] = {}

    # ── naming ──────────────────────────────────────────────────────────────

    @staticmethod
    def unit_name(account: AccountInfo) -> str:
        """The unit for ``account``, e.g. ``"onedriveui-mount@onedrive.service"``.

        A concrete instance file, not the shared template: every account has its
        own mountpoint, cache directory and rc port, so nothing useful could be
        parameterised by ``%i`` alone. systemd prefers a concrete
        ``foo@bar.service`` file over the ``foo@.service`` template, so writing
        one is both legal and simpler.
        """
        return UNIT_MOUNT_TMPL.format(account.id)

    @staticmethod
    def mountpoint(account: AccountInfo) -> Path:
        """The account's mountpoint — which *is* its sync root, by construction."""
        return paths.mount_point(account.sync_root)

    def options_for(self, account: AccountInfo) -> dict[str, Any]:
        """The account's ``mount`` block, with every default filled in.

        Args:
            account: The account.

        Returns:
            A complete option mapping: the injected provider's values layered
            over :data:`DEFAULT_MOUNT_OPTIONS`, so a partial config can never
            produce a half-built argv.
        """
        merged = dict(DEFAULT_MOUNT_OPTIONS)
        merged.update(self._options(account) or {})
        return merged

    # ── argv ────────────────────────────────────────────────────────────────

    def build_argv(self, account: AccountInfo, port: int,
                   creds: tuple[str, str]) -> list[str]:
        """The mount command line of ARCHITECTURE §5.3.

        Every omission is as deliberate as every inclusion:

        * **No ``--daemon``.** It is broken with ``--rc --rc-addr`` in v1.75.0:
          the parent binds the port before forking and the child dies with
          ``bind: address already in use``. systemd owns the process instead.
        * **No ``--allow-other``.** It fails unless root adds
          ``user_allow_other`` to ``/etc/fuse.conf``, which is commented out here.
        * **No ``--vfs-read-chunk-streams``.** Parallel streams cause Graph 429s.
        * **No ``--onedrive-*``.** ``chunk_size``, ``delta``, ``link_scope``,
          ``link_type``, ``hash_type`` and ``metadata_permissions`` all live in
          ``rclone.conf`` (invariant I1).

        Args:
            account: The account. ``account.fs`` supplies ``<remote>:`` and
                ``account.sync_root`` the mountpoint.
            port: The loopback rc port for *this mount's* own rc server — the
                one that serves ``vfs/*``, ``core/stats`` and
                ``core/transferred``. Distinct from the control plane's port.
            creds: ``(user, password)`` for that rc server.

        Returns:
            The full argv, rclone binary first. Checked against I1 and I12
            before it is returned, ``extra_args`` included.

        Raises:
            SafetyRefusal: invariant ``"I1"`` — a backend option or a connection
                string reached the command line; or ``"I12"`` — ``--inplace``.
            ConfigError: ``poll_interval_s`` is not strictly below
                ``dir_cache_time_s``, which would make polling useless.
        """
        opt = self.options_for(account)
        user, password = creds
        mountpoint = self.mountpoint(account)

        poll_s = int(opt["poll_interval_s"])
        dir_cache_s = int(opt["dir_cache_time_s"])
        if poll_s >= dir_cache_s:
            raise ConfigError(
                f"poll_interval_s ({poll_s}) must be strictly below "
                f"dir_cache_time_s ({dir_cache_s}); otherwise the directory cache "
                f"outlives every poll and remote changes never appear")

        argv: list[str] = [
            self._rclone_path, "mount", account.fs, str(mountpoint),
            "--vfs-cache-mode", "full",
            "--cache-dir", str(Path(os.path.expanduser(str(opt["cache_dir"])))),
            "--vfs-cache-max-size", f"{int(opt['vfs_cache_max_size_gb'])}G",
            "--vfs-cache-max-age", f"{int(opt['vfs_cache_max_age_hours'])}h",
            "--vfs-cache-min-free-space", f"{int(opt['vfs_cache_min_free_space_gb'])}G",
            "--vfs-cache-poll-interval", _dur(opt["vfs_cache_poll_interval_s"]),
            "--vfs-write-back", f"{int(opt['write_back_s'])}s",
        ]
        if opt.get("fast_fingerprint", True):
            argv.append("--vfs-fast-fingerprint")
        argv += [
            "--dir-cache-time", _dur(dir_cache_s),
            "--poll-interval", f"{poll_s}s",
            "--attr-timeout", _dur_ms(opt["attr_timeout_ms"]),
            "--vfs-read-chunk-size", f"{int(opt['read_chunk_size_mb'])}M",
            "--vfs-read-chunk-size-limit", f"{int(opt['read_chunk_size_limit_mb'])}M",
            "--transfers", str(min(int(opt["transfers"]), MAX_TRANSFERS)),
            "--checkers", str(min(int(opt["checkers"]), MAX_CHECKERS)),
            "--tpslimit", _num(opt["tpslimit"]),
            "--tpslimit-burst", str(int(opt["tpslimit_burst"])),
            "--retries", str(int(opt["retries"])),
            "--low-level-retries", str(int(opt["low_level_retries"])),
            "--file-perms", str(opt["file_perms"]),
            "--dir-perms", str(opt["dir_perms"]),
            "--umask", str(opt["umask"]),
            "--devname", APP_DISPLAY_NAME,
        ]
        for directory in MOUNT_EXCLUDE_DIRS:
            argv += ["--exclude", f"/{directory}/**"]
        argv += [
            "--rc",
            "--rc-addr", f"127.0.0.1:{int(port)}",
            "--rc-user", user, "--rc-pass", password,
            "--user-agent", self._user_agent,
            "--use-json-log", "--color", "NEVER", "--log-level", "INFO",
        ]
        argv += [str(extra) for extra in (opt.get("extra_args") or ())]

        guards.assert_no_backend_flags(argv)
        guards.assert_no_inplace(argv)
        return argv

    def unit_text(self, account: AccountInfo, port: int,
                  creds: tuple[str, str]) -> str:
        """Render the mount unit (ARCHITECTURE §5.3).

        Args:
            account: The account.
            port: The mount's own rc port.
            creds: ``(user, password)`` for that rc server.

        Returns:
            The complete unit text.

            ``Type=notify`` is what makes the unit report ``active`` only once
            the mount is *usable*, so anything ordered after it can rely on the
            mount; ``Type=simple`` reports ``active`` immediately and every
            consumer then races. ``ExecStop=fusermount3 -uz`` guarantees the
            mount is detached even when rclone is already dead, ``KillMode=mixed``
            sends ``SIGTERM`` only to the main process so in-flight uploads get
            the full ``TimeoutStopSec=120`` to drain, and ``ExecStartPre`` clears
            an ``ENOTCONN`` corpse left by a previous crash.
        """
        argv = self.build_argv(account, port, creds)
        mountpoint = str(self.mountpoint(account))
        exec_start = " ".join(unit_escape(token) for token in argv)
        stop = f"{unit_escape(FUSERMOUNT3)} -uz {unit_escape(mountpoint)}"
        return (
            "[Unit]\n"
            f"Description=OneDriveUI mount for {unit_escape(account.id)}\n"
            "Documentation=https://rclone.org/commands/rclone_mount/\n"
            "PartOf=graphical-session.target\n"
            f"{ORDERING_DAEMON}"
            "\n"
            "[Service]\n"
            "Type=notify\n"
            f"ExecStartPre=-{stop}\n"
            f"ExecStart={exec_start}\n"
            f"ExecStop={stop}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "TimeoutStopSec=120\n"
            "KillMode=mixed\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    # ── lifecycle ───────────────────────────────────────────────────────────

    def endpoint(self, account: AccountInfo) -> RcEndpoint | None:
        """The mount's own rc endpoint, or ``None`` when it has never been started.

        Args:
            account: The account.

        Returns:
            The endpoint serving ``vfs/*``, ``core/stats`` and
            ``core/transferred`` for this mount. Not the control plane's — that
            process has no VFS at all.
        """
        cached = self._endpoints.get(account.id)
        if cached is not None:
            return cached
        found = _endpoints.load_endpoints().get(
            _endpoints.endpoint_key("mount", account.id))
        if found is not None:
            self._endpoints[account.id] = found
        return found

    def health(self, account: AccountInfo) -> MountHealth:
        """Invariant I6 applied to this account's mountpoint.

        Args:
            account: The account.

        Returns:
            ``DOWN``, ``STALE`` or ``UP``. Emits ``BUS.mount_health`` on a change.
        """
        health = is_live(self.mountpoint(account))
        if self._health.get(account.id) != health:
            self._health[account.id] = health
            BUS.mount_health.emit(account.id, health)
        return health

    def status_text(self, account: AccountInfo) -> str:
        """rclone's ``sd_notify`` status line for this mount.

        Args:
            account: The account.

        Returns:
            e.g. ``"[23:29] vfs cache: objects 3 (was 3) in use 0, to upload 0,
            uploading 0, total size 2.525Mi"``, or ``""``. Read through the
            injected service manager when it offers ``status_text``, otherwise
            straight from ``systemctl --user show -p StatusText``.
        """
        unit = self.unit_name(account)
        getter = getattr(self._systemd, "status_text", None)
        if callable(getter):
            try:
                return str(getter(unit) or "")
            except Exception:                                  # noqa: BLE001
                log.debug("systemd.status_text(%s) failed; falling back", unit,
                          exc_info=True)
        return systemctl_status_text(unit)

    def ensure_mounted(self, account: AccountInfo) -> None:
        """Bring the mount up, clearing a stale one first.

        Idempotent: a live mount returns immediately without touching systemd.

        Args:
            account: The account to mount.

        Raises:
            SafetyRefusal: The argv would have carried a backend option (I1).
            ConfigError: The mount options are internally inconsistent.
            MountLost: The mountpoint exists as a non-directory, so nothing can
                be mounted there.
        """
        # `mount.enabled` is a real setting and was read by nothing: an account
        # configured with no mount got one anyway on the next start-up, which
        # for a user who deliberately turned it off is the client undoing their
        # decision — and mounting a remote is not a small side effect.
        if not self.options_for(account).get("enabled", True):
            log.info("mount is disabled for %s; not mounting", account.id)
            return

        mountpoint = self.mountpoint(account)
        health = self.health(account)
        if health is MountHealth.UP:
            return
        if health is MountHealth.STALE:
            log.warning("%s is stale (ENOTCONN); clearing it with fusermount3 -uz",
                        mountpoint)
            fusermount_unmount(mountpoint, lazy=True)

        if mountpoint.exists() and not mountpoint.is_dir():
            raise MountLost(f"{mountpoint} exists and is not a directory")
        try:
            mountpoint.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MountLost(f"cannot create the mountpoint {mountpoint}: {exc}") from exc

        port = _endpoints.pick_free_port(exclude=_endpoints.known_ports())
        user, password = _endpoints.generate_credentials()
        unit = self.unit_name(account)

        self._systemd.write_unit(unit, self.unit_text(account, port, (user, password)))
        self._systemd.daemon_reload()
        self._systemd.enable(unit)
        self._health[account.id] = MountHealth.STARTING
        BUS.mount_health.emit(account.id, MountHealth.STARTING)
        # `restart`, not `start`. The unit text just written carries a NEW rc
        # port and NEW credentials, and `start` on a unit systemd already counts
        # as active is a silent no-op: the previous rclone keeps running on the
        # previous port while the lines below record the new one and save it to
        # `endpoints.json`. Everything that then drives the mount — vfs/stats,
        # vfs/refresh, the pinner, the transfer poller — aims at a port nothing
        # is listening on, and the mount looks dead while it is working fine.
        #
        # Safe here because a healthy mount already returned above: by this
        # point the mount is DOWN, or was STALE and has just been detached, so
        # there is no live VFS holding an upload for invariant I3 to protect.
        self._systemd.restart(unit)

        ep = RcEndpoint(kind="mount", host="127.0.0.1", port=port, user=user,
                        password=password, mountpoint=str(mountpoint),
                        account_id=account.id)
        self._endpoints[account.id] = ep
        _endpoints.save_endpoint(ep)
        log.info("started %s on %s for %s", unit, ep.base_url, mountpoint)

    def unmount(self, account: AccountInfo, *, lazy: bool = True,
                force: bool = False) -> None:
        """Stop the unit and detach the mount.

        ``mount/unmount`` is never used: it is banned by I7, it cannot see a
        CLI-started mount, and it would not destroy the VFS anyway.

        **Invariant I3.** ``fusermount3 -uz`` destroys the VFS, and a file still
        uploading exists in that cache and nowhere else. So this refuses while
        ``uploadsInProgress`` is anything but zero — including the ``-1`` that
        means ``vfs/stats`` could not be read at all, which is *not* evidence of
        safety. A ``STALE`` mount is exempt: an ``ENOTCONN`` corpse serves
        nobody and only detaching it can recover the account.

        Args:
            account: The account.
            lazy: Pass ``-uz`` to ``fusermount3``. The default; plain ``-u``
                fails with ``EBUSY`` whenever anything holds an fd inside.
            force: Skip the I3 upload check. For an explicit, informed user
                action only — the caller must have shown what is still
                uploading. Never pass it from an automatic code path.

        Raises:
            SafetyRefusal: invariant ``"I3"`` — an upload is in flight, or its
                count could not be read, and `force` was not given.
        """
        if not force and self.health(account) is not MountHealth.STALE:
            pending = self.uploads_in_progress(account)
            if pending != 0:
                raise SafetyRefusal(
                    "I3",
                    f"refusing to unmount {self.mountpoint(account)} for "
                    f"{account.id}: "
                    + (f"{pending} upload(s) still in progress"
                       if pending > 0 else
                       "vfs/stats could not be read, so it is unknown whether "
                       "an upload is in flight")
                    + " — a file mid-upload exists in the VFS cache and nowhere "
                    "else, and -uz would destroy it")
        unit = self.unit_name(account)
        try:
            self._systemd.stop(unit)
        except Exception:                                      # noqa: BLE001
            log.warning("stopping %s failed; unmounting anyway", unit, exc_info=True)
        fusermount_unmount(self.mountpoint(account), lazy=lazy)
        self._endpoints.pop(account.id, None)
        _endpoints.forget_endpoint("mount", account.id)
        self._health[account.id] = MountHealth.DOWN
        BUS.mount_health.emit(account.id, MountHealth.DOWN)

    # ── the upload-aware restart ladder ─────────────────────────────────────

    def uploads_in_progress(self, account: AccountInfo) -> int:
        """``vfs/stats.diskCache.uploadsInProgress`` for this mount.

        Args:
            account: The account.

        Returns:
            The count, or ``-1`` when the VFS could not be asked — which is *not*
            the same as zero, and :meth:`restart` treats it as a reason to refuse.
        """
        ep = self.endpoint(account)
        if ep is None or not ep.port:
            return -1
        try:
            stats = call_blocking(ep, "vfs/stats", {}, timeout_s=2.0)
        except (RcError, OSError):
            return -1
        disk = stats.get("diskCache")
        if not isinstance(disk, Mapping):
            return -1
        try:
            return int(disk.get("uploadsInProgress", 0))
        except (TypeError, ValueError):
            return -1

    def restart(self, account: AccountInfo, reason: str) -> None:
        """Restart the mount unit, on the ladder, unless an upload is in flight.

        **Invariant I3.** A file being uploaded exists in the VFS cache and
        nowhere else until the upload completes; restarting the mount under it
        risks exactly the loss that invariant forbids. So this refuses — returns
        without acting, logging why — while ``uploadsInProgress > 0``, and also
        when the count cannot be read at all.

        The one exception is a mount that is *already* ``STALE``: an ``ENOTCONN``
        corpse serves nobody, its uploads are already lost, and only a restart
        can bring the account back.

        Args:
            account: The account.
            reason: Why, for the log. Recorded verbatim.

        The ladder is :data:`~onedriveui.constants.MOUNT_RESTART_LADDER_S`
        (10 s / 30 s / 2 m / 10 m), capped at
        :data:`~onedriveui.constants.MOUNT_RESTART_MAX_PER_HOUR` attempts per
        hour, after which this returns without acting.
        """
        health = self.health(account)
        if health is not MountHealth.STALE:
            pending = self.uploads_in_progress(account)
            if pending != 0:
                log.warning(
                    "refusing to restart the mount for %s (%s): %s — invariant I3 "
                    "forbids disturbing an upload that exists nowhere else",
                    account.id, reason,
                    f"uploadsInProgress={pending}" if pending > 0
                    else "vfs/stats could not be read")
                return

        history = self._prune_restarts(account.id)
        if len(history) >= MOUNT_RESTART_MAX_PER_HOUR:
            log.error("refusing to restart the mount for %s (%s): already %d "
                      "restarts this hour", account.id, reason, len(history))
            return

        delay_s = MOUNT_RESTART_LADDER_S[
            min(len(history), len(MOUNT_RESTART_LADDER_S) - 1)]
        history.append(time.monotonic())
        unit = self.unit_name(account)
        log.warning("restarting %s in %ds (%s); attempt %d this hour",
                    unit, delay_s, reason, len(history))
        self._schedule(int(delay_s * 1000), lambda: self._do_restart(account))

    def rewrite_unit(self, account: AccountInfo) -> bool:
        """Re-render this account's unit from the current mount options.

        **This is what makes a settings change reach rclone.** The argv lives
        only in the unit file, and the only place that file was ever written is
        `ensure_mounted()` — which returns immediately when the mount is already
        UP. So restarting a healthy mount re-executed the *old* command line,
        and every parameter on the rclone settings page was saved, displayed,
        and never applied.

        The port and credentials are deliberately reused rather than re-minted:
        anything already driving this mount — the transfer poller, the VFS
        probe, the pinner — is pointed at that address, and moving it would
        leave them all talking to a port nobody is listening on.

        Args:
            account: The account whose unit to re-render.

        Returns:
            True when the unit was rewritten. False when nothing is recorded
            for this account yet, in which case `ensure_mounted()` is the call
            that should be made instead.
        """
        endpoint = self.endpoint(account)
        if endpoint is None or not endpoint.port:
            return False
        unit = self.unit_name(account)
        self._systemd.write_unit(
            unit, self.unit_text(account, endpoint.port,
                                 (endpoint.user, endpoint.password)))
        self._systemd.daemon_reload()
        log.info("re-rendered %s from the current mount options", unit)
        return True

    def _do_restart(self, account: AccountInfo) -> None:
        """The deferred half of :meth:`restart`, after the ladder delay."""
        mountpoint = self.mountpoint(account)
        if is_live(mountpoint) is MountHealth.STALE:
            fusermount_unmount(mountpoint, lazy=True)
        unit = self.unit_name(account)
        try:
            # Pick up any settings changed since the unit was written. Without
            # this a restart is a no-op as far as configuration goes.
            self.rewrite_unit(account)
        except Exception:  # noqa: BLE001 - restart with the old argv beats not
            log.warning("could not re-render %s; restarting it as it is", unit,
                        exc_info=True)
        try:
            self._systemd.restart(unit)
        except Exception:                                      # noqa: BLE001
            log.error("restarting %s failed", unit, exc_info=True)
            return
        self._health[account.id] = MountHealth.STARTING
        BUS.mount_health.emit(account.id, MountHealth.STARTING)

    def _prune_restarts(self, account_id: str) -> list[float]:
        now = time.monotonic()
        history = [t for t in self._restarts.get(account_id, [])
                   if now - t < _SECONDS_PER_HOUR]
        self._restarts[account_id] = history
        return history

    def restarts_this_hour(self, account: AccountInfo) -> int:
        """How many restarts have been scheduled for ``account`` in the last hour."""
        return len(self._prune_restarts(account.id))

    def is_serving(self, account: AccountInfo) -> bool:
        """Does this mount's own rc server answer ``rc/noop``?

        Args:
            account: The account.

        Returns:
            True when the mount's rc port is live. A ``UP`` health with a dead rc
            port means the mount works but ``vfs/*`` is unreachable — the state in
            which :meth:`restart` refuses, because it cannot prove no upload is
            in flight.
        """
        ep = self.endpoint(account)
        return ep is not None and is_alive(ep, timeout_s=1.0)
