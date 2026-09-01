"""Start OneDrive when I sign in — **exactly one** of two mechanisms.

Linux offers two ways to launch a GUI at login and they do not know about each
other:

* the **XDG autostart entry**, `~/.config/autostart/onedriveui.desktop`, which
  gnome-session reads and which GNOME's own *Settings ▸ Applications ▸ Startup*
  panel manages, and
* a **systemd user unit**, `~/.config/systemd/user/onedriveui.service`, wanted
  by `graphical-session.target`, which additionally gives us
  `Restart=on-failure` supervision and a journal.

Ship both and the application launches **twice**. The single-instance guard
(`platform.singleinstance`) makes the loser exit, but the user still sees a
flash, a second icon in the dash and a stray journal entry — and worse, whichever
copy wins is nondeterministic. So this module treats "which method" as a
mutually exclusive choice and enforces it in code, not in a comment:

* `install_gui_unit()` removes the XDG entry before writing the unit.
* `install_desktop_file()` disables and removes the unit before writing the entry.
* Every install path finishes with `assert_exclusive()`, which raises
  `SafetyRefusal` if both are somehow on disk. A post-condition, so a future
  caller that finds a third way to install one still trips over it.

`app.autostart_method` (`ARCHITECTURE.md §9`) records the user's choice;
`"systemd"` is the default because supervision is worth more than
discoverability once the app is installed, and `set_enabled()` migrates cleanly
between the two in either direction.

One systemd detail that is easy to get wrong: the GUI unit is wanted by
`graphical-session.target`, **not** `default.target`. `default.target` is
reached at login-session time, before any display exists — correct for the
headless FUSE mount unit, wrong for a Qt application, which would start, fail to
find a compositor and be restarted five times before hitting the start limit.
`network-online.target` does not exist in the user manager at all;
`systemd.write_unit()` refuses any unit text that names it.
"""

from __future__ import annotations

import logging
from typing import Final, Mapping

from onedriveui import APP_DISPLAY_NAME, APP_ID
from onedriveui import paths
from onedriveui.constants import ORDERING_GUI, UNIT_GUI, UNIT_RCD
from onedriveui.errors import OneDriveUIError, SafetyRefusal
from onedriveui.platform import systemd
from onedriveui.platform.desktop import (
    COMMENT,
    DESKTOP_ENTRY_VERSION,
    DESKTOP_GROUP,
    ENTRY_MODE,
    FLAG_BACKGROUND,
    _write_if_changed,
    assert_one_main_category,
    build_desktop_entry,
    executable_command,
    update_desktop_database,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# The two methods
# ─────────────────────────────────────────────────────────────────────────────

#: `app.autostart_method` values, per `ARCHITECTURE.md §9`.
METHOD_SYSTEMD: Final[str] = "systemd"
METHOD_XDG: Final[str] = "xdg"

#: Not a config value: what `method()` answers when autostart is off.
METHOD_NONE: Final[str] = "none"

#: The two installable methods, in preference order. `systemd` wins a conflict
#: because it is the one that supervises us.
METHODS: Final[tuple[str, ...]] = (METHOD_SYSTEMD, METHOD_XDG)

#: `SafetyRefusal.invariant` for the exclusivity rule.
AUTOSTART_RULE: Final[str] = "S13.autostart"

# ─────────────────────────────────────────────────────────────────────────────
# The systemd unit
# ─────────────────────────────────────────────────────────────────────────────

#: The GUI unit's name, from `constants`. Never re-typed.
UNIT: Final[str] = UNIT_GUI

#: The target the GUI unit installs into. NOT `default.target`, which is reached
#: before a display exists.
GUI_TARGET: Final[str] = "graphical-session.target"

#: Restart supervision. Five failures inside five minutes and systemd gives up,
#: which is the behaviour we want: a GUI that cannot start must not spin.
RESTART_SEC: Final[int] = 5
START_LIMIT_INTERVAL_SEC: Final[int] = 300
START_LIMIT_BURST: Final[int] = 5

#: Shutdown must outlast `App.shutdown()`, which waits on a bisync SIGINT.
TIMEOUT_STOP_SEC: Final[int] = 20

#: Inherited from the user manager's environment, which already carries them
#: (`systemctl --user show-environment` confirms WAYLAND_DISPLAY, DISPLAY,
#: XDG_CURRENT_DESKTOP and DBUS_SESSION_BUS_ADDRESS on the target machine).
#: Named explicitly so a `systemctl --user start` from a TTY behaves the same.
PASS_ENVIRONMENT: Final[tuple[str, ...]] = (
    "WAYLAND_DISPLAY", "DISPLAY", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE",
)

# ─────────────────────────────────────────────────────────────────────────────
# The XDG autostart entry
# ─────────────────────────────────────────────────────────────────────────────

#: Seconds gnome-session waits before launching us, so we do not fight the shell
#: and the AppIndicator extension for the login CPU.
AUTOSTART_DELAY_S: Final[int] = 8

#: The key GNOME Tweaks and Settings toggle. `false` disables the entry without
#: deleting it — we read it, but we never write it: our own entry is removed
#: outright when the user turns autostart off, so the file's presence and its
#: effect can never disagree.
GNOME_ENABLED_KEY: Final[str] = "X-GNOME-Autostart-enabled"

#: The specification's own disable key. `Hidden=true` in the user's directory
#: also overrides a same-named system entry in `/etc/xdg/autostart`.
HIDDEN_KEY: Final[str] = "Hidden"

#: An autostart entry is not a launcher: it must not appear in the menu.
NO_DISPLAY: Final[bool] = True

#: `OnlyShowIn` is deliberately NOT set. It is matched against
#: `$XDG_CURRENT_DESKTOP`, and an omitted key means "every desktop" — which is
#: what we want, since the app runs anywhere a session bus does.
AUTOSTART_CATEGORIES: Final[tuple[str, ...]] = ("Network", "FileTransfer")


# ═════════════════════════════════════════════════════════════════════════════
# Method validation and exclusivity
# ═════════════════════════════════════════════════════════════════════════════

def assert_valid_method(method: str) -> str:
    """Validate an `app.autostart_method` value.

    Args:
        method: `"systemd"` or `"xdg"`.

    Returns:
        The method unchanged.

    Raises:
        SafetyRefusal: If it is neither.
    """
    if method not in METHODS:
        raise SafetyRefusal(
            AUTOSTART_RULE,
            f"{method!r} is not an autostart method; expected one of "
            f"{', '.join(METHODS)}",
        )
    return method


def unit_installed() -> bool:
    """Whether our systemd GUI unit file is on disk.

    A pure filesystem check: it must answer correctly with no session bus, and
    the file is ours, so its presence is authoritative.

    Returns:
        True if `~/.config/systemd/user/onedriveui.service` exists.
    """
    return systemd.unit_file(UNIT).is_file()


def unit_enabled() -> bool:
    """Whether systemd would start the GUI unit at login.

    Args:
        None.

    Returns:
        True when `GetUnitFileState` reports `enabled`. False when the manager
        cannot be reached — an unreachable manager will not start us either.
    """
    return systemd.is_enabled(UNIT) == "enabled"


def _entry_values() -> Mapping[str, str]:
    """Parse the `[Desktop Entry]` group of our autostart file.

    Returns:
        `{key: value}`, or an empty mapping when the file is missing.
    """
    try:
        text = paths.autostart_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    in_group = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_group = stripped == f"[{DESKTOP_GROUP}]"
            continue
        if not in_group or not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def desktop_file_installed() -> bool:
    """Whether the XDG autostart entry exists at all.

    Returns:
        True if `~/.config/autostart/onedriveui.desktop` exists, hidden or not.
    """
    return paths.autostart_file().is_file()


def desktop_file_active() -> bool:
    """Whether the XDG autostart entry would actually launch us.

    An entry disabled by `Hidden=true` or `X-GNOME-Autostart-enabled=false` is
    inert, so it cannot cause the double launch this module exists to prevent.
    We never write either key ourselves — turning autostart off deletes the file
    — but GNOME's own Startup panel writes them, and this must read them.

    Returns:
        True if the entry exists and is not disabled.
    """
    if not desktop_file_installed():
        return False
    values = _entry_values()
    if values.get(HIDDEN_KEY, "").lower() == "true":
        return False
    return values.get(GNOME_ENABLED_KEY, "true").lower() != "false"


def installed_methods() -> tuple[str, ...]:
    """Every autostart method currently in effect.

    Returns:
        A tuple drawn from `METHODS`, in preference order. Empty when autostart
        is off.
    """
    found: list[str] = []
    if unit_installed():
        found.append(METHOD_SYSTEMD)
    if desktop_file_active():
        found.append(METHOD_XDG)
    return tuple(found)


def assert_exclusive() -> str:
    """Refuse a machine where both autostart methods are live.

    Called as a post-condition by every install and removal path, so a caller
    that reaches a both-installed state is told immediately rather than shipping
    a double launch to the user.

    Returns:
        The single installed method, or `METHOD_NONE`.

    Raises:
        SafetyRefusal: If both the unit and the XDG entry would start us.
    """
    found = installed_methods()
    if len(found) > 1:
        raise SafetyRefusal(
            AUTOSTART_RULE,
            f"both autostart methods are installed ({', '.join(found)}): "
            f"{systemd.unit_file(UNIT)} and {paths.autostart_file()} would each "
            "launch the application at login, so it would start twice",
        )
    return found[0] if found else METHOD_NONE


def method() -> str:
    """Which autostart method is in effect.

    Never raises — the tray and the Settings page call it on every repaint.

    Returns:
        `"systemd"`, `"xdg"` or `"none"`. On a conflict the systemd unit is
        reported (it is the supervising one) and a warning is logged;
        `assert_exclusive()` is the call that treats that state as a bug.
    """
    found = installed_methods()
    if len(found) > 1:
        log.warning("both autostart methods are installed; reporting %s", found[0])
    return found[0] if found else METHOD_NONE


def enabled() -> bool:
    """Whether the application will start at the next login.

    Returns:
        True if either method is installed and active.
    """
    return method() != METHOD_NONE


def conflict() -> bool:
    """Whether both autostart methods are live.

    Returns:
        True if the application would launch twice at login.
    """
    return len(installed_methods()) > 1


# ═════════════════════════════════════════════════════════════════════════════
# The systemd unit
# ═════════════════════════════════════════════════════════════════════════════

def gui_unit_text(exec_command: str | None = None) -> str:
    """The `onedriveui.service` unit body.

    Args:
        exec_command: The command to run, or `None` for
            `desktop.executable_command()`.

    Returns:
        The complete unit text. It never names `network-online.target`, which
        does not exist in the `--user` manager;`systemd.write_unit()` refuses
        any text that does.
    """
    command = exec_command or executable_command()
    return (
        "[Unit]\n"
        f"Description={APP_DISPLAY_NAME} desktop client (rclone)\n"
        # PartOf + After, from constants — the frozen ordering for a GUI unit.
        f"{ORDERING_GUI}"
        # Without a graphical session there is nothing to draw on: fail fast
        # rather than restart-loop against a missing compositor.
        f"Requisite={GUI_TARGET}\n"
        # The control plane should be up, but we start without it and recover.
        f"Wants={UNIT_RCD}\n"
        f"After={UNIT_RCD}\n"
        f"StartLimitIntervalSec={START_LIMIT_INTERVAL_SEC}\n"
        f"StartLimitBurst={START_LIMIT_BURST}\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command} {FLAG_BACKGROUND}\n"
        "Restart=on-failure\n"
        f"RestartSec={RESTART_SEC}\n"
        f"TimeoutStopSec={TIMEOUT_STOP_SEC}\n"
        "Slice=app.slice\n"
        f"PassEnvironment={' '.join(PASS_ENVIRONMENT)}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "\n"
        "[Install]\n"
        f"WantedBy={GUI_TARGET}\n"
    )


def install_gui_unit(exec_command: str | None = None, *, now: bool = False) -> bool:
    """Install and enable the systemd GUI unit, removing the XDG entry first.

    The XDG entry is removed **before** the unit is written, so a crash between
    the two leaves autostart off rather than doubled.

    Args:
        exec_command: The command to run, or `None` for the default.
        now: Also start the unit immediately.

    Returns:
        True if the unit file's content changed.

    Raises:
        SafetyRefusal: If both methods end up installed.
    """
    remove_desktop_file()
    changed = systemd.write_unit(UNIT, gui_unit_text(exec_command))
    try:
        systemd.enable(UNIT, now=now)
    except OneDriveUIError as exc:
        # The file is on disk and correct; only the [Install] symlink is
        # missing, and the next launch retries. Never fatal.
        log.warning("could not enable %s: %s", UNIT, exc)
    assert_exclusive()
    return changed


def remove_gui_unit(*, now: bool = False) -> bool:
    """Disable and delete the systemd GUI unit.

    Args:
        now: Also stop the running unit. Off by default: turning autostart off
            should not kill the copy the user is looking at.

    Returns:
        True if a unit file was removed.
    """
    if unit_installed():
        try:
            systemd.disable(UNIT, now=now)
        except OneDriveUIError as exc:
            log.warning("could not disable %s: %s", UNIT, exc)
    return systemd.remove_unit(UNIT)


# ═════════════════════════════════════════════════════════════════════════════
# The XDG autostart entry
# ═════════════════════════════════════════════════════════════════════════════

def autostart_entry_text(exec_command: str | None = None) -> str:
    """The `~/.config/autostart/onedriveui.desktop` body.

    Args:
        exec_command: The command to run, or `None` for the default.

    Returns:
        The complete `.desktop` text. `desktop-file-validate` passes on it with
        no output, category hint included — `assert_one_main_category()` is what
        guarantees that.
    """
    command = exec_command or executable_command()
    categories = assert_one_main_category(AUTOSTART_CATEGORIES)
    return build_desktop_entry([(DESKTOP_GROUP, {
        "Type": "Application",
        "Version": DESKTOP_ENTRY_VERSION,
        "Name": APP_DISPLAY_NAME,
        "Comment": COMMENT,
        # No %U here: nothing hands a login-time launch a URL, and declaring a
        # field code we do not use would be a validation error.
        "Exec": f"{command} {FLAG_BACKGROUND}",
        "TryExec": command.split(" ", 1)[0],
        "Icon": APP_ID,
        "Terminal": False,
        "NoDisplay": NO_DISPLAY,
        "StartupNotify": False,
        "Categories": categories,
        GNOME_ENABLED_KEY: True,
        "X-GNOME-Autostart-Delay": AUTOSTART_DELAY_S,
        "X-GNOME-UsesNotifications": True,
        HIDDEN_KEY: False,
    })])


def install_desktop_file(exec_command: str | None = None) -> bool:
    """Install the XDG autostart entry, removing the systemd unit first.

    Args:
        exec_command: The command to run, or `None` for the default.

    Returns:
        True if the entry's content changed.

    Raises:
        SafetyRefusal: If both methods end up installed.
    """
    remove_gui_unit()
    target = paths.autostart_file()
    changed = _write_if_changed(target, autostart_entry_text(exec_command))
    if changed:
        log.info("installed the XDG autostart entry at %s", target)
    assert_exclusive()
    return changed


def remove_desktop_file() -> bool:
    """Delete the XDG autostart entry.

    Deleted rather than marked `Hidden=true`, so the file's presence and its
    effect can never disagree.

    Returns:
        True if a file was removed.
    """
    target = paths.autostart_file()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not remove %s: %s", target, exc)
        return False
    log.info("removed the XDG autostart entry at %s", target)
    return True


# ═════════════════════════════════════════════════════════════════════════════
# The one entry point the UI calls
# ═════════════════════════════════════════════════════════════════════════════

def set_enabled(enable: bool, method: str = METHOD_SYSTEMD, *,
                exec_command: str | None = None, now: bool = False) -> str:
    """Turn autostart on or off, with exactly one mechanism.

    This is what the *Start OneDrive when I sign in* toggle calls. Switching
    method while enabled migrates cleanly: the old mechanism is removed before
    the new one is written, in that order, so no window exists in which both are
    installed.

    Args:
        enable: Whether the application should start at login.
        method: `"systemd"` or `"xdg"`. Ignored when `enable` is False.
        exec_command: The command to run, or `None` for the default.
        now: When installing the systemd unit, also start it.

    Returns:
        The method now in effect: `"systemd"`, `"xdg"` or `"none"`.

    Raises:
        SafetyRefusal: If `method` is not a known method, or if both methods
            somehow end up installed.
    """
    if not enable:
        remove_gui_unit()
        remove_desktop_file()
        return assert_exclusive()

    chosen = assert_valid_method(method)
    if chosen == METHOD_SYSTEMD:
        install_gui_unit(exec_command, now=now)
    else:
        install_desktop_file(exec_command)
    return assert_exclusive()


def repair() -> str:
    """Force the exclusivity rule onto a machine that already broke it.

    Removes the losing method when both are installed — for example after a
    downgrade, a restored backup, or a user who hand-wrote an autostart entry
    while the unit was enabled.

    Returns:
        The single method left in effect, or `METHOD_NONE`.
    """
    found = installed_methods()
    if len(found) > 1:
        log.warning("repairing a double autostart: keeping %s, removing %s",
                    found[0], ", ".join(found[1:]))
        for loser in found[1:]:
            if loser == METHOD_SYSTEMD:
                remove_gui_unit()
            else:
                remove_desktop_file()
    return assert_exclusive()


__all__ = [
    "METHOD_SYSTEMD", "METHOD_XDG", "METHOD_NONE", "METHODS", "AUTOSTART_RULE",
    "UNIT", "GUI_TARGET", "RESTART_SEC", "START_LIMIT_INTERVAL_SEC",
    "START_LIMIT_BURST", "TIMEOUT_STOP_SEC", "PASS_ENVIRONMENT",
    "AUTOSTART_DELAY_S", "GNOME_ENABLED_KEY", "HIDDEN_KEY", "NO_DISPLAY",
    "AUTOSTART_CATEGORIES", "ENTRY_MODE",
    "assert_valid_method", "assert_exclusive", "installed_methods",
    "unit_installed", "unit_enabled", "desktop_file_installed",
    "desktop_file_active", "method", "enabled", "conflict",
    "gui_unit_text", "install_gui_unit", "remove_gui_unit",
    "autostart_entry_text", "install_desktop_file", "remove_desktop_file",
    "set_enabled", "repair", "update_desktop_database",
]
