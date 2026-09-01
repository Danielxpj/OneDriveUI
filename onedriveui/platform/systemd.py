"""`systemd --user` control over `org.freedesktop.systemd1`.

The user manager exports its full `Manager` interface on the **session** bus, so
writing, enabling, starting, stopping and inspecting our three unit families
costs no subprocess at all. Only two things here shell out, because they have no
D-Bus equivalent worth the code: `systemd-run` for a transient unit, and
`journalctl` for a log tail.

> **`network-online.target` must never appear in anything this module writes or
> launches.** It does not exist in the `--user` manager: `After=`/`Wants=` on it
> are silently ignored, so a unit that names it *looks* ordered after the
> network and is not. Every write path here refuses it with a `SafetyRefusal`
> rather than letting a maintainer believe an ordering that does not exist.
> rclone's own retry logic is what actually covers a boot-time network race.

Unit files are written through `atomicio.atomic_write_text()` (WP-01): a
half-written unit file is a unit systemd refuses to load, which takes the mount
down at the next boot.

Ownership note: this module never *invents* unit text. `rc/daemon.py` and
`rc/mountd.py` (WP-02) own the argv and the unit bodies; this module owns
getting them onto disk atomically, telling systemd about them, and reading state
back. WP-02 depends on these signatures and injects this module rather than
importing it, so the two packages can be built in parallel.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from gi.repository import GLib

from onedriveui import atomicio, paths
from onedriveui.errors import OneDriveUIError, SafetyRefusal
from onedriveui.platform.dbus import Bus

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# D-Bus surface
# ─────────────────────────────────────────────────────────────────────────────

SYSTEMD_NAME: Final[str] = "org.freedesktop.systemd1"
SYSTEMD_PATH: Final[str] = "/org/freedesktop/systemd1"
MANAGER_IFACE: Final[str] = "org.freedesktop.systemd1.Manager"
UNIT_IFACE: Final[str] = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE: Final[str] = "org.freedesktop.systemd1.Service"

#: `StartUnit` job mode. "replace" is what `systemctl start` uses.
JOB_MODE: Final[str] = "replace"

#: Properties that live on `.Service` rather than on `.Unit`. `show()` consults
#: this first and falls back to the other interface, so callers never have to
#: know which one a property belongs to.
SERVICE_PROPERTIES: Final[frozenset[str]] = frozenset({
    "StatusText", "MainPID", "ExecMainPID", "ExecMainStatus", "ExecMainCode",
    "Result", "NRestarts", "Type", "WatchdogTimestamp", "StatusErrno",
    "KillSignal", "TimeoutStopUSec", "RestartUSec",
})

PROP_ACTIVE_STATE: Final[str] = "ActiveState"
PROP_SUB_STATE: Final[str] = "SubState"
PROP_LOAD_STATE: Final[str] = "LoadState"
PROP_UNIT_FILE_STATE: Final[str] = "UnitFileState"
PROP_STATUS_TEXT: Final[str] = "StatusText"

ACTIVE: Final[str] = "active"
ACTIVATING: Final[str] = "activating"
DEACTIVATING: Final[str] = "deactivating"
INACTIVE: Final[str] = "inactive"
FAILED: Final[str] = "failed"
NOT_FOUND: Final[str] = "not-found"

# ─────────────────────────────────────────────────────────────────────────────
# Unit files
# ─────────────────────────────────────────────────────────────────────────────

#: A unit file is readable by the whole desktop; it holds no secret. (The rc
#: password lives in `endpoints.json` at 0600 and reaches units by environment.)
UNIT_FILE_MODE: Final[int] = 0o644

_UNIT_SUFFIXES: Final[tuple[str, ...]] = (
    ".service", ".socket", ".target", ".timer", ".path", ".slice", ".mount", ".scope",
)
#: `\Z`, never `$`: `$` also matches *before* a trailing newline, so `$` would
#: accept `"onedriveui.service\n"` and let a stray newline reach a D-Bus call.
_UNIT_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_.\\@:-]+(" + "|".join(re.escape(s) for s in _UNIT_SUFFIXES) + r")\Z"
)

#: The target that does not exist in the user manager. Never emit it.
FORBIDDEN_TARGET: Final[str] = "network-online.target"

#: `SafetyRefusal.invariant` tag for the forbidden target; ARCHITECTURE.md §5.2
#: is where the rule and the measurement behind it live.
UNIT_RULE: Final[str] = "S5.2"

# ─────────────────────────────────────────────────────────────────────────────
# Transients and the journal
# ─────────────────────────────────────────────────────────────────────────────

SYSTEMD_RUN: Final[str] = "systemd-run"
JOURNALCTL: Final[str] = "journalctl"

#: bisync must be interrupted, never killed: a `SIGKILL` mid-transfer leaves a
#: `<name>.<hash>.partial` at the destination that the next run syncs back as a
#: genuine new file (invariant I13).
TRANSIENT_KILL_SIGNAL: Final[str] = "SIGINT"

#: rclone bisync's graceful shutdown can take well over a minute on a large
#: tree; 150 s is the ceiling ARCHITECTURE.md §5.4 fixes.
TRANSIENT_TIMEOUT_STOP_S: Final[int] = 150

#: A transient must not be resurrected behind our back — the supervisor owns the
#: restart ladder, not systemd.
TRANSIENT_RESTART: Final[str] = "no"

#: `systemd-run` returns as soon as the job is enqueued; `journalctl -n` reads a
#: bounded number of records. Neither should ever approach these.
RUN_TIMEOUT_S: Final[float] = 10.0
JOURNAL_TIMEOUT_S: Final[float] = 5.0
JOURNAL_DEFAULT_LINES: Final[int] = 200


# ─────────────────────────────────────────────────────────────────────────────
# Guards
# ─────────────────────────────────────────────────────────────────────────────

def assert_valid_unit_name(name: str) -> str:
    """Validate a systemd unit name.

    Args:
        name: The unit name, e.g. `onedriveui-mount@onedrive.service`.

    Returns:
        The name unchanged.

    Raises:
        SafetyRefusal: If the name is empty, contains a path separator, or does
            not end in a known unit suffix. A name with a `/` in it would make
            `write_unit()` escape `~/.config/systemd/user`.
    """
    if not name or "/" in name or os.sep in name or not _UNIT_NAME_RE.match(name):
        raise SafetyRefusal(UNIT_RULE, f"{name!r} is not a valid systemd unit name")
    return name


def assert_no_network_online_target(text: str, *, where: str = "unit text") -> None:
    """Refuse anything naming `network-online.target`.

    Args:
        text: Unit text, or a joined property list.
        where: What is being checked, for the message.

    Raises:
        SafetyRefusal: If `network-online.target` appears anywhere in `text`.
    """
    if FORBIDDEN_TARGET in text:
        raise SafetyRefusal(
            UNIT_RULE,
            f"{where} names {FORBIDDEN_TARGET}, which does not exist in the "
            "systemd --user manager: After=/Wants= on it are silently ignored, "
            "so the ordering it implies is a lie",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bus plumbing
# ─────────────────────────────────────────────────────────────────────────────

_BUS: Bus | None = None
_UNIT_PATHS: dict[str, str] = {}


def bus() -> Bus:
    """The session bus the `--user` manager listens on."""
    return _BUS if _BUS is not None else Bus.session()


def set_bus(new_bus: Bus | None) -> None:
    """Override the bus this module uses.

    Args:
        new_bus: A `Bus` (or test double exposing `call`/`get_property`), or
            `None` to go back to the process-wide session bus.
    """
    global _BUS
    _BUS = new_bus
    _UNIT_PATHS.clear()


def available() -> bool:
    """Whether the systemd user manager can be reached on the session bus."""
    result = bus().call_or_none(
        SYSTEMD_NAME, SYSTEMD_PATH, MANAGER_IFACE, "GetUnitFileState",
        signature="(s)", args=("basic.target",), reply="(s)",
    )
    return result is not None


def _manager_call(
    method: str,
    *,
    signature: str | None = None,
    args: Iterable[Any] = (),
    reply: str | None = None,
) -> tuple[Any, ...]:
    """Call a `Manager` method, translating GLib errors into our hierarchy.

    Args:
        method: Manager method name.
        signature: Argument signature.
        args: Arguments.
        reply: Expected reply signature.

    Returns:
        The unpacked reply tuple.

    Raises:
        OneDriveUIError: If the manager is unreachable or returned an error.
    """
    try:
        return bus().call(
            SYSTEMD_NAME, SYSTEMD_PATH, MANAGER_IFACE, method,
            signature=signature, args=args, reply=reply,
        )
    except GLib.Error as exc:
        raise OneDriveUIError(f"systemd {method} failed: {exc.message}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Unit files on disk
# ─────────────────────────────────────────────────────────────────────────────

def unit_file(name: str) -> Path:
    """The path a unit is written to.

    Args:
        name: The unit name.

    Returns:
        `~/.config/systemd/user/<name>`.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    return paths.systemd_unit(assert_valid_unit_name(name))


def read_unit(name: str) -> str:
    """The current on-disk text of a unit, or `""` if it does not exist.

    Args:
        name: The unit name.

    Returns:
        The file contents.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    path = unit_file(name)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_unit(name: str, text: str, *, reload: bool = True) -> bool:
    """Write a unit file atomically, and tell systemd about it.

    A unit whose content is unchanged is not rewritten at all: touching the file
    would make `systemctl --user daemon-reload` churn and, worse, would restart
    the "did the unit change?" reasoning in `rc/daemon.py` on every launch.

    Args:
        name: The unit name, e.g. `onedriveui-rcd.service`.
        text: The complete unit body.
        reload: Run `daemon_reload()` after a change, so `enable()` and
            `start()` see the new file.

    Returns:
        True if the file's content changed.

    Raises:
        SafetyRefusal: If the name is invalid or the text names
            `network-online.target`.
        OneDriveUIError: If the file could not be written.
    """
    assert_valid_unit_name(name)
    assert_no_network_online_target(text, where=f"unit {name}")
    path = unit_file(name)
    body = text if text.endswith("\n") else text + "\n"
    if read_unit(name) == body:
        log.debug("unit %s unchanged", name)
        return False
    try:
        atomicio.atomic_write_text(path, body, mode=UNIT_FILE_MODE)
    except OSError as exc:
        raise OneDriveUIError(f"could not write {path}: {exc}") from exc
    log.info("wrote unit %s (%d bytes)", name, len(body))
    if reload:
        try:
            daemon_reload()
        except OneDriveUIError as exc:
            log.warning("daemon-reload after writing %s failed: %s", name, exc)
    return True


def remove_unit(name: str, *, reload: bool = True) -> bool:
    """Delete a unit file we installed.

    Args:
        name: The unit name.
        reload: Run `daemon_reload()` afterwards.

    Returns:
        True if a file was removed.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    path = unit_file(name)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not remove unit %s: %s", name, exc)
        return False
    log.info("removed unit %s", name)
    if reload:
        try:
            daemon_reload()
        except OneDriveUIError as exc:
            log.warning("daemon-reload after removing %s failed: %s", name, exc)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Manager operations
# ─────────────────────────────────────────────────────────────────────────────

def daemon_reload() -> None:
    """`systemctl --user daemon-reload`.

    Raises:
        OneDriveUIError: If the manager could not be reached.
    """
    _UNIT_PATHS.clear()
    _manager_call("Reload")
    log.debug("systemd --user daemon-reload")


def enable(name: str, *, now: bool = False, force: bool = True) -> bool:
    """Enable a unit, creating its `[Install]` symlinks.

    Args:
        name: The unit name.
        now: Also start it.
        force: Overwrite conflicting symlinks.

    Returns:
        True if the unit carried install information.

    Raises:
        SafetyRefusal: If the unit name is invalid.
        OneDriveUIError: If the manager returned an error.
    """
    assert_valid_unit_name(name)
    result = _manager_call(
        "EnableUnitFiles",
        signature="(asbb)", args=([name], False, force), reply="(ba(sss))",
    )
    carries_install = bool(result[0]) if result else False
    daemon_reload()
    log.info("enabled %s (install info: %s)", name, carries_install)
    if now:
        start(name)
    return carries_install


def disable(name: str, *, now: bool = False) -> bool:
    """Disable a unit, removing its `[Install]` symlinks.

    Args:
        name: The unit name.
        now: Stop it first.

    Returns:
        True if any symlink was removed.

    Raises:
        SafetyRefusal: If the unit name is invalid.
        OneDriveUIError: If the manager returned an error.
    """
    assert_valid_unit_name(name)
    if now:
        try:
            stop(name)
        except OneDriveUIError as exc:
            log.warning("stop before disable of %s failed: %s", name, exc)
    result = _manager_call(
        "DisableUnitFiles", signature="(asb)", args=([name], False), reply="(a(sss))"
    )
    changes = list(result[0]) if result else []
    daemon_reload()
    log.info("disabled %s (%d change(s))", name, len(changes))
    return bool(changes)


def start(name: str, mode: str = JOB_MODE) -> str:
    """Start a unit.

    Args:
        name: The unit name.
        mode: The systemd job mode.

    Returns:
        The job object path.

    Raises:
        SafetyRefusal: If the unit name is invalid.
        OneDriveUIError: If the manager returned an error.
    """
    assert_valid_unit_name(name)
    result = _manager_call("StartUnit", signature="(ss)", args=(name, mode), reply="(o)")
    log.info("started %s", name)
    return str(result[0]) if result else ""


def stop(name: str, mode: str = JOB_MODE) -> str:
    """Stop a unit.

    Args:
        name: The unit name.
        mode: The systemd job mode.

    Returns:
        The job object path.

    Raises:
        SafetyRefusal: If the unit name is invalid.
        OneDriveUIError: If the manager returned an error.
    """
    assert_valid_unit_name(name)
    result = _manager_call("StopUnit", signature="(ss)", args=(name, mode), reply="(o)")
    log.info("stopped %s", name)
    return str(result[0]) if result else ""


def restart(name: str, mode: str = JOB_MODE) -> str:
    """Restart a unit, starting it if it was not running.

    Args:
        name: The unit name.
        mode: The systemd job mode.

    Returns:
        The job object path.

    Raises:
        SafetyRefusal: If the unit name is invalid.
        OneDriveUIError: If the manager returned an error.
    """
    assert_valid_unit_name(name)
    result = _manager_call("RestartUnit", signature="(ss)", args=(name, mode), reply="(o)")
    log.info("restarted %s", name)
    return str(result[0]) if result else ""


def try_restart(name: str, mode: str = JOB_MODE) -> str:
    """Restart a unit only if it is already running.

    Args:
        name: The unit name.
        mode: The systemd job mode.

    Returns:
        The job object path.

    Raises:
        SafetyRefusal: If the unit name is invalid.
        OneDriveUIError: If the manager returned an error.
    """
    assert_valid_unit_name(name)
    result = _manager_call(
        "TryRestartUnit", signature="(ss)", args=(name, mode), reply="(o)"
    )
    return str(result[0]) if result else ""


def reset_failed(name: str) -> None:
    """Clear a unit's failed state so the restart ladder can try again.

    Args:
        name: The unit name.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    assert_valid_unit_name(name)
    bus().call_or_none(
        SYSTEMD_NAME, SYSTEMD_PATH, MANAGER_IFACE, "ResetFailedUnit",
        signature="(s)", args=(name,),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reading state
# ─────────────────────────────────────────────────────────────────────────────

def unit_path(name: str) -> str | None:
    """The D-Bus object path of a unit, loading it if necessary.

    `GetUnit` is tried first because it has no side effect; `LoadUnit` is the
    fallback for a unit systemd has not read yet.

    Args:
        name: The unit name.

    Returns:
        The object path, or `None` if there is no such unit.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    assert_valid_unit_name(name)
    cached = _UNIT_PATHS.get(name)
    if cached is not None:
        return cached
    for method in ("GetUnit", "LoadUnit"):
        result = bus().call_or_none(
            SYSTEMD_NAME, SYSTEMD_PATH, MANAGER_IFACE, method,
            signature="(s)", args=(name,), reply="(o)",
        )
        if result:
            path = str(result[0])
            _UNIT_PATHS[name] = path
            return path
    return None


def show(name: str, prop: str, *, iface: str | None = None, default: Any = None) -> Any:
    """Read one property of a unit — the D-Bus form of `systemctl show -p`.

    Args:
        name: The unit name.
        prop: The property, e.g. `ActiveState` or `StatusText`.
        iface: Force an interface. By default `SERVICE_PROPERTIES` decides, and
            the other interface is tried if the first says no such property.
        default: Returned when the unit or the property is unavailable.

    Returns:
        The property value, or `default`.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    path = unit_path(name)
    if path is None:
        return default
    if iface is not None:
        candidates: tuple[str, ...] = (iface,)
    elif prop in SERVICE_PROPERTIES:
        candidates = (SERVICE_IFACE, UNIT_IFACE)
    else:
        candidates = (UNIT_IFACE, SERVICE_IFACE)
    sentinel = object()
    for candidate in candidates:
        value = bus().get_property(SYSTEMD_NAME, path, candidate, prop, sentinel)
        if value is not sentinel:
            return value
    return default


def state(name: str) -> tuple[str, str]:
    """A unit's `(ActiveState, SubState)`.

    Args:
        name: The unit name.

    Returns:
        e.g. `("active", "running")`, or `("inactive", "dead")`. A unit that
        does not exist reports `("inactive", "not-found")` — systemd itself
        answers `("inactive", "dead")` with `LoadState=not-found` for a name it
        has never heard of, which is indistinguishable from a stopped unit, so
        the `LoadState` is folded into the sub-state here.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    path = unit_path(name)
    if path is None:
        return (INACTIVE, NOT_FOUND)
    props = bus().get_all(SYSTEMD_NAME, path, UNIT_IFACE)
    if not props:
        return (INACTIVE, NOT_FOUND)
    if str(props.get(PROP_LOAD_STATE, "")) == NOT_FOUND:
        return (str(props.get(PROP_ACTIVE_STATE, INACTIVE)), NOT_FOUND)
    return (
        str(props.get(PROP_ACTIVE_STATE, INACTIVE)),
        str(props.get(PROP_SUB_STATE, "")),
    )


def is_active(name: str) -> bool:
    """Whether a unit is fully active.

    `activating` is deliberately **not** active: the mount controller must not
    treat a still-starting `Type=notify` mount as usable.

    Args:
        name: The unit name.

    Returns:
        True only when `ActiveState == "active"`.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    return state(name)[0] == ACTIVE


def is_failed(name: str) -> bool:
    """Whether a unit is in the failed state.

    Args:
        name: The unit name.

    Returns:
        True when `ActiveState == "failed"`.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    return state(name)[0] == FAILED


def is_enabled(name: str) -> str:
    """The unit-file state, as `systemctl is-enabled` reports it.

    Args:
        name: The unit name.

    Returns:
        `enabled`, `disabled`, `static`, `masked`, ... or `""` when unknown.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    assert_valid_unit_name(name)
    result = bus().call_or_none(
        SYSTEMD_NAME, SYSTEMD_PATH, MANAGER_IFACE, "GetUnitFileState",
        signature="(s)", args=(name,), reply="(s)",
    )
    return str(result[0]) if result else ""


def exists(name: str) -> bool:
    """Whether systemd can load a unit by this name.

    Args:
        name: The unit name.

    Returns:
        True when a unit file exists and is loadable.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    path = unit_path(name)
    if path is None:
        return False
    load_state = bus().get_property(SYSTEMD_NAME, path, UNIT_IFACE, PROP_LOAD_STATE, "")
    return str(load_state) not in ("", NOT_FOUND)


def status_text(name: str) -> str:
    """A service's `StatusText` — what `sd_notify(STATUS=...)` last published.

    `rclone mount --daemon`-less units run `Type=notify`, so this is where the
    mount publishes "vfs cache: cleaned ..." and similar.

    Args:
        name: The unit name.

    Returns:
        The status line, or `""`.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    value = show(name, PROP_STATUS_TEXT, iface=SERVICE_IFACE, default="")
    return str(value) if value else ""


def main_pid(name: str) -> int:
    """A service's main PID, or 0.

    Args:
        name: The unit name.

    Returns:
        The PID, or 0 when the service is not running.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    value = show(name, "MainPID", iface=SERVICE_IFACE, default=0)
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover - hostile peer
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Transient units
# ─────────────────────────────────────────────────────────────────────────────

def build_transient_argv(
    unit: str,
    argv: Sequence[str],
    *,
    properties: Sequence[str] = (),
    description: str = "",
    collect: bool = True,
    kill_signal: str = TRANSIENT_KILL_SIGNAL,
    timeout_stop_s: int = TRANSIENT_TIMEOUT_STOP_S,
    restart: str = TRANSIENT_RESTART,
) -> list[str]:
    """Build the `systemd-run --user` command line for a transient unit.

    Pure and side-effect free, so the exact argv is testable without launching
    anything.

    Args:
        unit: The transient unit name, e.g. `onedriveui-bisync-onedrive`.
        argv: The command to run. Must be non-empty.
        properties: Extra `--property=` values, e.g. `("MemoryMax=1G",)`.
        description: Optional `--description=`.
        collect: Pass `--collect`, so a failed transient is garbage-collected
            instead of lingering in the failed state and blocking the next run.
        kill_signal: `KillSignal=`. Defaults to `SIGINT` — bisync must be
            interrupted, never killed (invariant I13).
        timeout_stop_s: `TimeoutStopSec=`, in seconds.
        restart: `Restart=`. Defaults to `no`; the supervisor owns retries.

    Returns:
        The full argv, ending in `--` followed by `argv`.

    Raises:
        SafetyRefusal: If the unit name is invalid, `argv` is empty, or anything
            names `network-online.target`.
    """
    assert_valid_unit_name(f"{unit}.service" if "." not in unit else unit)
    command = [str(part) for part in argv]
    if not command:
        raise SafetyRefusal(UNIT_RULE, "run_transient() needs a non-empty argv")
    props = [
        f"KillSignal={kill_signal}",
        f"TimeoutStopSec={int(timeout_stop_s)}",
        f"Restart={restart}",
        *[str(p) for p in properties],
    ]
    assert_no_network_online_target(
        " ".join([unit, description, *props, *command]),
        where=f"transient unit {unit}",
    )
    line = [SYSTEMD_RUN, "--user", f"--unit={unit}"]
    if collect:
        line.append("--collect")
    if description:
        line.append(f"--description={description}")
    line.extend(f"--property={p}" for p in props)
    line.append("--")
    line.extend(command)
    return line


def run_transient(
    unit: str,
    argv: Sequence[str],
    *,
    properties: Sequence[str] = (),
    description: str = "",
    collect: bool = True,
    kill_signal: str = TRANSIENT_KILL_SIGNAL,
    timeout_stop_s: int = TRANSIENT_TIMEOUT_STOP_S,
    restart: str = TRANSIENT_RESTART,
    env: dict[str, str] | None = None,
) -> str:
    """Launch a command as a transient `systemd --user` unit.

    This is how bisync runs: as a supervised unit rather than a child process,
    so a GUI crash cannot orphan it and `stop()` reaches it with `SIGINT`.

    `systemd-run` returns as soon as the job is enqueued, so this call does not
    block for the lifetime of the command.

    Args:
        unit: The transient unit name.
        argv: The command to run.
        properties: Extra `--property=` values.
        description: Optional `--description=`.
        collect: Pass `--collect`.
        kill_signal: `KillSignal=`.
        timeout_stop_s: `TimeoutStopSec=`, in seconds.
        restart: `Restart=`.
        env: Extra environment for `systemd-run` itself.

    Returns:
        The unit name, with `.service` appended if it had no suffix — the name
        `stop()`, `state()` and `journal_tail()` then take.

    Raises:
        SafetyRefusal: If any argument fails validation.
        OneDriveUIError: If `systemd-run` is missing, times out, or exits non-zero.
    """
    line = build_transient_argv(
        unit, argv, properties=properties, description=description, collect=collect,
        kill_signal=kill_signal, timeout_stop_s=timeout_stop_s, restart=restart,
    )
    if shutil.which(SYSTEMD_RUN) is None:
        raise OneDriveUIError(f"{SYSTEMD_RUN} is not installed")
    environment = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(
            line, capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
            env=environment, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OneDriveUIError(f"{SYSTEMD_RUN} for {unit} failed: {exc}") from exc
    if completed.returncode != 0:
        raise OneDriveUIError(
            f"{SYSTEMD_RUN} for {unit} exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )
    log.info("started transient unit %s", unit)
    _UNIT_PATHS.pop(unit, None)
    return unit if "." in unit else f"{unit}.service"


def journal_tail(name: str, lines: int = JOURNAL_DEFAULT_LINES) -> list[str]:
    """The last N journal lines for a unit.

    Args:
        name: The unit name.
        lines: How many records to read.

    Returns:
        The message lines, oldest first. Empty when `journalctl` is missing,
        times out, or has nothing for the unit — a diagnostics helper must never
        raise into a settings page.

    Raises:
        SafetyRefusal: If the unit name is invalid.
    """
    assert_valid_unit_name(name)
    if shutil.which(JOURNALCTL) is None:
        return []
    command = [
        JOURNALCTL, "--user", "-u", name, "-n", str(max(1, int(lines))),
        "--no-pager", "-o", "cat",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=JOURNAL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("journalctl for %s failed: %s", name, exc)
        return []
    if completed.returncode != 0:
        log.debug("journalctl for %s exited %d", name, completed.returncode)
        return []
    return [line for line in completed.stdout.splitlines() if line]


__all__ = [
    "ACTIVATING",
    "ACTIVE",
    "DEACTIVATING",
    "FAILED",
    "FORBIDDEN_TARGET",
    "INACTIVE",
    "JOB_MODE",
    "JOURNAL_DEFAULT_LINES",
    "MANAGER_IFACE",
    "NOT_FOUND",
    "SERVICE_IFACE",
    "SERVICE_PROPERTIES",
    "SYSTEMD_NAME",
    "SYSTEMD_PATH",
    "SYSTEMD_RUN",
    "TRANSIENT_KILL_SIGNAL",
    "TRANSIENT_RESTART",
    "TRANSIENT_TIMEOUT_STOP_S",
    "UNIT_FILE_MODE",
    "UNIT_IFACE",
    "UNIT_RULE",
    "assert_no_network_online_target",
    "assert_valid_unit_name",
    "available",
    "build_transient_argv",
    "bus",
    "daemon_reload",
    "disable",
    "enable",
    "exists",
    "is_active",
    "is_enabled",
    "is_failed",
    "journal_tail",
    "main_pid",
    "read_unit",
    "remove_unit",
    "reset_failed",
    "restart",
    "run_transient",
    "set_bus",
    "show",
    "start",
    "state",
    "status_text",
    "stop",
    "try_restart",
    "unit_file",
    "unit_path",
    "write_unit",
]
