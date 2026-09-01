"""Desktop surfaces: "show in folder", the `.desktop` entry, bookmarks, icons.

Everything here is the Linux answer to a Windows Explorer affordance, and every
mechanism was measured on the target machine (GNOME 50.4, Wayland, Nautilus
50.2.2) rather than assumed.

What is load-bearing, and why:

* **"Show in folder" is `org.freedesktop.FileManager1.ShowItems`**, never
  `xdg-open` on the parent directory. `ShowItems` opens the folder *with the
  item selected*, which is what Explorer does; `xdg-open` on the parent merely
  opens the folder. The service is D-Bus **activated**
  (`/usr/share/dbus-1/services/org.freedesktop.FileManager1.service`), so it is
  the one call in this package that must pass `auto_start=True`.
* **The Wayland `app_id` comes from `QGuiApplication.setDesktopFileName()`**,
  and it must match the installed entry's basename (`onedriveui.desktop`).
  Without it GNOME labels our window `python3`, shows a generic icon and cannot
  group our notifications. This module owns the file; `ui/app.py` owns the call.
* **`Categories=Network;FileTransfer;` — two entries, exactly one of them a
  registered *main* category.** Adding `Utility;` makes `desktop-file-validate`
  emit "contains more than one main category; application might appear more than
  once in the application menu". `assert_one_main_category()` enforces that in
  code, so the warning can never reappear by accident.
* **`device_id()` is `sha256(/etc/machine-id)[:16]`, never the raw value.**
  `/etc/machine-id` is a stable secret: anything that can read it can correlate
  the user across every application and network trace that leaks it. The hash is
  stable across reboots (which is all we need it for) and carries none of that.
* **Bookmarks go into both `~/.config/gtk-3.0/bookmarks` and `gtk-4.0/bookmarks`.**
  Nautilus 50 is GTK4, but GTK3 file choosers are still everywhere. Appended
  idempotently — the file belongs to the user, not to us, so it is never
  rewritten wholesale.
* **The icon cache must be rebuilt** (`gtk4-update-icon-cache -f -t`) or GTK may
  not see a freshly installed emblem until the next login, and Nautilus silently
  drops emblems it cannot resolve.

Threading: `show_in_folder()`, `open_path()` and `open_url()` touch Gio/Qt and
are GUI-thread only — `Bus.call()` asserts that itself. The pure-filesystem
helpers (`file_uri()`, `user_dirs()`, the bookmark readers) are thread-safe.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from onedriveui import APP_DISPLAY_NAME, APP_ID
from onedriveui import paths
from onedriveui.errors import SafetyRefusal
from onedriveui.models import KfmFolder
from onedriveui.platform.dbus import Bus
from onedriveui.strings import MENU

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# org.freedesktop.FileManager1
# ─────────────────────────────────────────────────────────────────────────────

FM1_NAME: Final[str] = "org.freedesktop.FileManager1"
FM1_PATH: Final[str] = "/org/freedesktop/FileManager1"
FM1_IFACE: Final[str] = "org.freedesktop.FileManager1"

#: `ShowItems(as uris, s startup_id)` — the "show in folder" call.
FM1_SHOW_ITEMS: Final[str] = "ShowItems"
#: `ShowFolders(as uris, s startup_id)` — open the folders themselves.
FM1_SHOW_FOLDERS: Final[str] = "ShowFolders"
#: `ShowItemProperties(as uris, s startup_id)`.
FM1_SHOW_PROPERTIES: Final[str] = "ShowItemProperties"

#: All three FileManager1 methods share this signature.
FM1_SIGNATURE: Final[str] = "(ass)"

#: The file manager may have to be launched. This is the ONLY call in the
#: platform package that is allowed to D-Bus-activate its peer.
FM1_TIMEOUT_MS: Final[int] = 5000

# ─────────────────────────────────────────────────────────────────────────────
# URI escaping
# ─────────────────────────────────────────────────────────────────────────────

#: The characters GLib leaves unescaped in a `file://` URI path, derived by
#: comparing `Gio.File.new_for_path(p).get_uri()` against `urllib.parse.quote`
#: for every printable ASCII character on the target machine. Note `;` IS
#: escaped even though it is an RFC 3986 sub-delimiter. Matching GLib byte for
#: byte matters: the freedesktop thumbnail name is `md5(uri)`, so a single
#: differing escape means every thumbnail another application wrote is a miss.
URI_SAFE: Final[str] = "/!$&'()*+,=:@"

#: `file://` with no host, as GLib and Qt both emit.
FILE_SCHEME: Final[str] = "file://"

# ─────────────────────────────────────────────────────────────────────────────
# The desktop entry
# ─────────────────────────────────────────────────────────────────────────────

#: Desktop Entry Specification version this file conforms to.
DESKTOP_ENTRY_VERSION: Final[str] = "1.5"

#: The group every desktop entry opens with. Must be first, per the spec.
DESKTOP_GROUP: Final[str] = "Desktop Entry"

#: The registered **main** categories (Desktop Menu Specification, table 1).
#: `desktop-file-validate` warns when an entry names more than one of these,
#: because the application then appears more than once in the menu.
MAIN_CATEGORIES: Final[frozenset[str]] = frozenset({
    "AudioVideo", "Audio", "Video", "Development", "Education", "Game",
    "Graphics", "Network", "Office", "Science", "Settings", "System",
    "Utility",
})

#: `Network` is the main category; `FileTransfer` is an additional one that only
#: refines it. Exactly one main category — see `assert_one_main_category()`.
CATEGORIES: Final[tuple[str, ...]] = ("Network", "FileTransfer")

#: Search terms GNOME's overview matches against.
KEYWORDS: Final[tuple[str, ...]] = (
    "onedrive", "cloud", "sync", "rclone", "microsoft", "backup",
)

#: Microsoft's deep-link scheme, so `odopen://` URLs reach us.
URI_SCHEME: Final[str] = "odopen"
MIME_TYPES: Final[tuple[str, ...]] = (f"x-scheme-handler/{URI_SCHEME}",)

#: The X11/XWayland window-class fallback. On native Wayland `app_id` — set by
#: `QGuiApplication.setDesktopFileName(APP_ID)` — is what actually matters.
STARTUP_WM_CLASS: Final[str] = APP_ID

#: Not in `strings.py`: that module is frozen and has no desktop-entry section,
#: and these two keys are shell metadata (the tooltip under the icon), not
#: application chrome. Everything `strings.py` DOES cover — the action labels —
#: is imported from it below.
GENERIC_NAME: Final[str] = "Cloud file sync"
COMMENT: Final[str] = "Keep your OneDrive files in sync"

#: Command-line switches the desktop actions invoke. `ui/app.py` parses them.
FLAG_BACKGROUND: Final[str] = "--background"
FLAG_OPEN_FOLDER: Final[str] = "--open-folder"
FLAG_PAUSE: Final[str] = "--pause"
FLAG_SETTINGS: Final[str] = "--settings"

#: `Actions=` — the right-click menu on the GNOME dash icon. This is the one
#: tray-like surface that works even when no StatusNotifierItem host is
#: installed, so it is populated whether or not the tray is available. Labels
#: come from `strings.MENU`; they are the same words the tray menu uses.
DESKTOP_ACTIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("OpenFolder", MENU.OPEN_FOLDER, FLAG_OPEN_FOLDER),
    ("Pause", MENU.PAUSE, FLAG_PAUSE),
    ("Settings", MENU.SETTINGS, FLAG_SETTINGS),
)

#: `desktop-file-validate`, when the package is installed.
VALIDATOR: Final[str] = "desktop-file-validate"
#: Refreshes the MIME/scheme handler cache in an applications directory.
DESKTOP_DATABASE_TOOL: Final[str] = "update-desktop-database"
#: GTK4 first; the GTK3 tool is the fallback for older systems.
ICON_CACHE_TOOLS: Final[tuple[str, ...]] = (
    "gtk4-update-icon-cache", "gtk-update-icon-cache",
)
#: Every subprocess here is a fast local tool; none may wedge startup.
TOOL_TIMEOUT_S: Final[float] = 30.0

#: 0644 — the desktop reads these, so they are deliberately not 0600.
ENTRY_MODE: Final[int] = 0o644

#: `SafetyRefusal.invariant` for the desktop-entry rules this module enforces.
ENTRY_RULE: Final[str] = "S13"

# ─────────────────────────────────────────────────────────────────────────────
# XDG user directories
# ─────────────────────────────────────────────────────────────────────────────

#: `KfmFolder` -> the `user-dirs.dirs` key and the xdg-user-dirs default. Note
#: the key is `XDG_DOWNLOAD_DIR`, singular, but no `KfmFolder` maps to it: the
#: five here are exactly the folders Windows' Known Folder Move offers.
USER_DIR_KEYS: Final[dict[KfmFolder, tuple[str, str]]] = {
    KfmFolder.DESKTOP:   ("XDG_DESKTOP_DIR", "Desktop"),
    KfmFolder.DOCUMENTS: ("XDG_DOCUMENTS_DIR", "Documents"),
    KfmFolder.PICTURES:  ("XDG_PICTURES_DIR", "Pictures"),
    KfmFolder.MUSIC:     ("XDG_MUSIC_DIR", "Music"),
    KfmFolder.VIDEOS:    ("XDG_VIDEOS_DIR", "Videos"),
}

#: Written by `xdg-user-dirs-update`; authoritative over any guess.
USER_DIRS_FILE: Final[str] = "user-dirs.dirs"

# ─────────────────────────────────────────────────────────────────────────────
# Machine identity
# ─────────────────────────────────────────────────────────────────────────────

#: Read in this order. `/var/lib/dbus/machine-id` is the older location and is
#: still a symlink to the first on many systems.
MACHINE_ID_FILES: Final[tuple[str, ...]] = (
    "/etc/machine-id", "/var/lib/dbus/machine-id",
)

#: How much of the digest `device_id()` keeps. 64 bits of a SHA-256 is far more
#: than enough to name one device in one user's account, and a short id is
#: readable in the About pane and in a conflict-copy suffix.
DEVICE_ID_CHARS: Final[int] = 16

#: Returned when no machine-id can be read at all (a container, typically), so
#: `device_id()` never raises and never returns an empty string.
DEVICE_ID_FALLBACK_SEED: Final[str] = "onedriveui-no-machine-id"


# ═════════════════════════════════════════════════════════════════════════════
# Bus plumbing — injectable exactly like `systemd.set_bus()`
# ═════════════════════════════════════════════════════════════════════════════

_BUS: Bus | None = None


def bus() -> Bus:
    """The session bus this module talks to.

    Returns:
        The bus set by `set_bus()`, or the process-wide session bus.
    """
    return _BUS if _BUS is not None else Bus.session()


def set_bus(new_bus: Bus | None) -> None:
    """Override the bus this module uses.

    Args:
        new_bus: A `Bus`, or any object exposing `call`/`call_or_none`, or
            `None` to go back to the process-wide session bus.
    """
    global _BUS
    _BUS = new_bus


# ═════════════════════════════════════════════════════════════════════════════
# URIs
# ═════════════════════════════════════════════════════════════════════════════

def file_uri(path: str | os.PathLike[str]) -> str:
    """The canonical `file://` URI for a local path.

    Byte-identical to `Gio.File.new_for_path(p).get_uri()` and to
    `QUrl.fromLocalFile(p).toEncoded()`, both verified across the printable
    ASCII range plus non-ASCII on the target machine. The path is made absolute
    but deliberately **not** resolved: a symlink into the sync root has its own
    identity, and `realpath()` here would send the file manager to the target.

    Args:
        path: A local filesystem path.

    Returns:
        e.g. `file:///home/u/OneDrive/a%20b.txt`.
    """
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    return FILE_SCHEME + urllib.parse.quote(absolute, safe=URI_SAFE)


def uri_to_path(uri: str) -> str | None:
    """The local path behind a `file://` URI.

    Args:
        uri: A URI of any scheme.

    Returns:
        The decoded absolute path, or `None` when `uri` is not a `file:` URI.
    """
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme != "file":
        return None
    return urllib.parse.unquote(parsed.path)


# ═════════════════════════════════════════════════════════════════════════════
# Opening things
# ═════════════════════════════════════════════════════════════════════════════

def _fm1_call(method: str, targets: Sequence[str | os.PathLike[str]],
              startup_id: str = "") -> bool:
    """Invoke one `org.freedesktop.FileManager1` method.

    Args:
        method: `ShowItems`, `ShowFolders` or `ShowItemProperties`.
        targets: Local paths.
        startup_id: An XDG startup-notification id, or `""`.

    Returns:
        True if the call reached the file manager.
    """
    uris = [file_uri(p) for p in targets]
    if not uris:
        return False
    result = bus().call_or_none(
        FM1_NAME, FM1_PATH, FM1_IFACE, method,
        signature=FM1_SIGNATURE, args=(uris, startup_id),
        timeout_ms=FM1_TIMEOUT_MS,
        # FileManager1 is D-Bus activated: Nautilus need not already be running.
        auto_start=True,
    )
    if result is None:
        log.info("FileManager1.%s unavailable for %d item(s)", method, len(uris))
        return False
    return True


def show_in_folder(*targets: str | os.PathLike[str], startup_id: str = "") -> bool:
    """Open the containing folder with the items selected — Explorer's
    "Show in folder".

    `QDesktopServices.openUrl()` on the file would *open* it, and on the parent
    directory would lose the selection, so neither is a substitute. If the file
    manager cannot be reached at all we fall back to opening the parent folder,
    which is worse but not nothing.

    Args:
        *targets: Local paths to reveal.
        startup_id: An XDG startup-notification id, or `""`.

    Returns:
        True if a file manager was asked to reveal the items.
    """
    if not targets:
        return False
    if _fm1_call(FM1_SHOW_ITEMS, targets, startup_id):
        return True
    parent = Path(os.path.abspath(os.path.expanduser(str(targets[0])))).parent
    log.info("falling back to opening %s", parent)
    return open_path(parent)


def show_folders(*targets: str | os.PathLike[str], startup_id: str = "") -> bool:
    """Open folders in the file manager.

    Args:
        *targets: Local directory paths.
        startup_id: An XDG startup-notification id, or `""`.

    Returns:
        True if a file manager was asked to open them.
    """
    if not targets:
        return False
    if _fm1_call(FM1_SHOW_FOLDERS, targets, startup_id):
        return True
    return open_path(targets[0])


def show_properties(*targets: str | os.PathLike[str], startup_id: str = "") -> bool:
    """Open the file manager's Properties dialog for the items.

    Args:
        *targets: Local paths.
        startup_id: An XDG startup-notification id, or `""`.

    Returns:
        True if the call reached the file manager.
    """
    return _fm1_call(FM1_SHOW_PROPERTIES, targets, startup_id)


def open_path(path: str | os.PathLike[str]) -> bool:
    """Open a file or folder with the user's default handler.

    Goes through `QDesktopServices`, which uses the XDG portal
    (`org.freedesktop.portal.OpenURI`) on Wayland when it is available and falls
    back to `xdg-open`. We never name a browser or a file manager ourselves.

    Args:
        path: A local path.

    Returns:
        True if the handler was launched.
    """
    return bool(QDesktopServices.openUrl(
        QUrl.fromLocalFile(os.path.abspath(os.path.expanduser(str(path))))))


def open_url(url: str) -> bool:
    """Open a URL in the user's default browser.

    Args:
        url: An absolute URL. A local path is rejected — use `open_path()`.

    Returns:
        True if the browser was launched.
    """
    qurl = QUrl(url)
    if not qurl.isValid() or not qurl.scheme():
        log.warning("refusing to open a URL with no scheme: %r", url)
        return False
    return bool(QDesktopServices.openUrl(qurl))


# ═════════════════════════════════════════════════════════════════════════════
# Device identity
# ═════════════════════════════════════════════════════════════════════════════

def _read_machine_id() -> str:
    """The raw `/etc/machine-id`.

    PRIVATE. This value is a stable system secret and must never leave the
    process: anything holding it can correlate this user across applications.
    `device_id()` is the only thing allowed to consume it.

    Returns:
        The 32-character hex id, or `""` when no file could be read.
    """
    for candidate in MACHINE_ID_FILES:
        try:
            value = Path(candidate).read_text(encoding="ascii", errors="ignore").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def device_id() -> str:
    """A stable, non-identifying id for this machine.

    `sha256(/etc/machine-id)[:16]`. The raw machine-id is **never** returned,
    logged or sent anywhere; the hash is one-way, is stable across reboots and
    reinstalls of this application, and changes only when the OS is reinstalled.

    Returns:
        16 lowercase hex characters.
    """
    seed = _read_machine_id() or DEVICE_ID_FALLBACK_SEED
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:DEVICE_ID_CHARS]


# ═════════════════════════════════════════════════════════════════════════════
# XDG user directories
# ═════════════════════════════════════════════════════════════════════════════

def _unquote_shell(value: str) -> str:
    """Undo the quoting `xdg-user-dirs-update` writes.

    Args:
        value: The right-hand side of a `user-dirs.dirs` assignment.

    Returns:
        The bare path, with `$HOME` expanded and backslash escapes resolved.
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        quote, value = value[0], value[1:-1]
        if quote == '"':
            out: list[str] = []
            index = 0
            while index < len(value):
                char = value[index]
                if char == "\\" and index + 1 < len(value):
                    index += 1
                    out.append(value[index])
                else:
                    out.append(char)
                index += 1
            value = "".join(out)
    home = str(Path.home())
    if value.startswith("$HOME"):
        value = home + value[len("$HOME"):]
    elif value.startswith("${HOME}"):
        value = home + value[len("${HOME}"):]
    return value


def user_dirs_file() -> Path:
    """`~/.config/user-dirs.dirs`.

    Returns:
        The path, whether or not it exists.
    """
    config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config).expanduser() if config else Path.home() / ".config"
    return base / USER_DIRS_FILE


def user_dirs() -> dict[KfmFolder, Path]:
    """The five Known-Folder-Move directories, from `user-dirs.dirs`.

    Parsed directly rather than shelled out to `xdg-user-dir`, which costs a
    process per lookup. A folder the file does not name falls back to the
    xdg-user-dirs default (`~/Desktop`, `~/Documents`, …).

    Returns:
        `{KfmFolder: absolute Path}`, one entry per member.
    """
    raw: dict[str, str] = {}
    try:
        text = user_dirs_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        raw[key.strip()] = _unquote_shell(value)

    home = Path.home()
    out: dict[KfmFolder, Path] = {}
    for folder, (key, default) in USER_DIR_KEYS.items():
        value = raw.get(key, "")
        out[folder] = Path(value) if value else home / default
    return out


def user_dir(folder: KfmFolder) -> Path:
    """One Known-Folder-Move directory.

    Args:
        folder: Which folder.

    Returns:
        Its absolute path.
    """
    return user_dirs()[folder]


# ═════════════════════════════════════════════════════════════════════════════
# GTK sidebar bookmarks
# ═════════════════════════════════════════════════════════════════════════════

def _bookmark_line(path: str | os.PathLike[str], label: str) -> str:
    """One `bookmarks` line: `<uri> <label>`.

    Args:
        path: The bookmarked directory.
        label: The name shown in the sidebar. A label equal to the directory's
            own basename is omitted, which is what GTK itself writes.

    Returns:
        The line, without a trailing newline.
    """
    uri = file_uri(path)
    basename = Path(os.path.abspath(os.path.expanduser(str(path)))).name
    return uri if label == basename else f"{uri} {label}"


def _parse_bookmarks(text: str) -> list[tuple[str, str]]:
    """Split a `bookmarks` file into `(uri, label)` pairs.

    Args:
        text: The file contents.

    Returns:
        One pair per non-empty line; the label is `""` when the line has none.
    """
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        uri, _, label = line.partition(" ")
        out.append((uri, label.strip()))
    return out


def sidebar_bookmarks(path: Path | None = None) -> list[tuple[str, str]]:
    """The bookmarks currently in one GTK bookmarks file.

    Args:
        path: A specific bookmarks file, or `None` for the GTK4 one.

    Returns:
        `[(uri, label)]` in file order.
    """
    target = path if path is not None else paths.gtk_bookmarks()[-1]
    try:
        return _parse_bookmarks(target.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def add_sidebar_bookmark(path: str | os.PathLike[str],
                         label: str = APP_DISPLAY_NAME) -> bool:
    """Add a sidebar entry to both the GTK3 and GTK4 bookmark files.

    Appended, never rewritten: this file is the user's, and a bookmark they
    reordered or renamed must survive us. Adding the same URI twice is a no-op,
    so this is safe to call on every launch.

    Args:
        path: The directory to bookmark, normally the sync root.
        label: The sidebar label.

    Returns:
        True if any file was changed.
    """
    line = _bookmark_line(path, label)
    uri = file_uri(path)
    changed = False
    for target in paths.gtk_bookmarks():
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if any(u == uri for u, _label in _parse_bookmarks(existing)):
            continue
        body = existing if existing.endswith("\n") or not existing else existing + "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body + line + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", target, exc)
            continue
        log.info("added sidebar bookmark %s to %s", uri, target)
        changed = True
    return changed


def remove_sidebar_bookmark(path: str | os.PathLike[str]) -> bool:
    """Drop our sidebar entry from both bookmark files.

    Args:
        path: The bookmarked directory.

    Returns:
        True if any file was changed.
    """
    uri = file_uri(path)
    changed = False
    for target in paths.gtk_bookmarks():
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        kept = [line for line in existing.splitlines()
                if line.strip().split(" ", 1)[0] != uri]
        if len(kept) == len(existing.splitlines()):
            continue
        try:
            target.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write %s: %s", target, exc)
            continue
        log.info("removed sidebar bookmark %s from %s", uri, target)
        changed = True
    return changed


# ═════════════════════════════════════════════════════════════════════════════
# The .desktop entry
# ═════════════════════════════════════════════════════════════════════════════

def assert_one_main_category(categories: Iterable[str]) -> tuple[str, ...]:
    """Refuse a `Categories=` list with more than one *main* category.

    `desktop-file-validate` only emits a hint for this, and a hint is easy to
    reintroduce; the acceptance criterion is a clean validate, so the rule lives
    in code.

    Args:
        categories: The category names, without the trailing separator.

    Returns:
        The categories as a tuple.

    Raises:
        SafetyRefusal: If two or more are registered main categories.
    """
    items = tuple(categories)
    main = [c for c in items if c in MAIN_CATEGORIES]
    if len(main) > 1:
        raise SafetyRefusal(
            ENTRY_RULE,
            f"Categories={';'.join(items)} names {len(main)} main categories "
            f"({', '.join(main)}); the application would appear more than once "
            "in the menu and desktop-file-validate would warn",
        )
    return items


def executable_command() -> str:
    """The command a desktop entry should exec.

    Returns:
        An installed `onedriveui` console script when one is on `PATH`,
        otherwise `<python> -m onedriveui`, which is what a checkout runs.
    """
    installed = shutil.which(APP_ID)
    if installed:
        return installed
    return f"{sys.executable} -m {APP_ID}"


def _semi(values: Iterable[str]) -> str:
    """Render a desktop-entry list: semicolon separated AND terminated.

    Args:
        values: The list members.

    Returns:
        e.g. `Network;FileTransfer;`.
    """
    items = [v for v in values if v]
    return "".join(f"{v};" for v in items)


def _escape_value(value: str) -> str:
    """Escape a desktop-entry value.

    Args:
        value: The raw string.

    Returns:
        The value with newlines, tabs, carriage returns and backslashes escaped
        as the Desktop Entry Specification requires.
    """
    return (value.replace("\\", "\\\\")
                 .replace("\n", "\\n")
                 .replace("\t", "\\t")
                 .replace("\r", "\\r"))


def build_desktop_entry(groups: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
    """Serialise desktop-entry groups to INI text.

    Args:
        groups: `[(group name, {key: value})]` in file order. The first group
            must be `Desktop Entry`. A `None` value drops the key; a bool
            renders as `true`/`false`; a list or tuple renders semicolon
            separated and terminated.

    Returns:
        The complete file text, ending in a newline.

    Raises:
        SafetyRefusal: If the first group is not `Desktop Entry`.
    """
    if not groups or groups[0][0] != DESKTOP_GROUP:
        raise SafetyRefusal(
            ENTRY_RULE,
            f"a desktop entry must open with [{DESKTOP_GROUP}], not "
            f"{groups[0][0] if groups else '(nothing)'}",
        )
    lines: list[str] = []
    for name, entries in groups:
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        for key, value in entries.items():
            if value is None:
                continue
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                rendered = _semi(str(v) for v in value)
                if not rendered:
                    continue
            else:
                rendered = _escape_value(str(value))
            lines.append(f"{key}={rendered}")
    return "\n".join(lines) + "\n"


def desktop_entry_text(exec_command: str | None = None) -> str:
    """The application launcher entry — `~/.local/share/applications/…`.

    `Actions=` is populated whether or not a tray is available: the dash icon's
    right-click menu is the one always-present surface on GNOME, where no
    StatusNotifierItem host ships by default.

    Args:
        exec_command: The command to run, or `None` for `executable_command()`.

    Returns:
        The complete `.desktop` text. `desktop-file-validate` passes on it with
        no output at all.
    """
    command = exec_command or executable_command()
    categories = assert_one_main_category(CATEGORIES)
    groups: list[tuple[str, Mapping[str, Any]]] = [(DESKTOP_GROUP, {
        "Type": "Application",
        "Version": DESKTOP_ENTRY_VERSION,
        "Name": APP_DISPLAY_NAME,
        "GenericName": GENERIC_NAME,
        "Comment": COMMENT,
        # %U, not %F: MimeType declares a scheme handler, and the two are
        # mutually exclusive in the specification.
        "Exec": f"{command} %U",
        "TryExec": command.split(" ", 1)[0],
        "Icon": APP_ID,
        "Terminal": False,
        "Categories": categories,
        "Keywords": KEYWORDS,
        "StartupNotify": True,
        "StartupWMClass": STARTUP_WM_CLASS,
        # Tells GNOME we have one window, so the dash icon focuses the running
        # instance instead of spawning a second process.
        "SingleMainWindow": True,
        # Lists us under Settings > Notifications so the user can mute us.
        "X-GNOME-UsesNotifications": True,
        "MimeType": MIME_TYPES,
        "Actions": tuple(action for action, _label, _flag in DESKTOP_ACTIONS),
    })]
    for action, label, flag in DESKTOP_ACTIONS:
        groups.append((f"Desktop Action {action}", {
            "Name": label,
            "Exec": f"{command} {flag}",
        }))
    return build_desktop_entry(groups)


def install_desktop_entry(exec_command: str | None = None) -> bool:
    """Write the launcher entry and refresh the desktop database.

    Args:
        exec_command: The command to run, or `None` for `executable_command()`.

    Returns:
        True if the file's content changed.
    """
    text = desktop_entry_text(exec_command)
    target = paths.desktop_file()
    changed = _write_if_changed(target, text)
    if changed:
        update_desktop_database()
    return changed


def remove_desktop_entry() -> bool:
    """Delete the launcher entry.

    Returns:
        True if a file was removed.
    """
    target = paths.desktop_file()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not remove %s: %s", target, exc)
        return False
    log.info("removed %s", target)
    update_desktop_database()
    return True


def _write_if_changed(target: Path, text: str) -> bool:
    """Write a 0644 text file only when its content differs.

    Rewriting an unchanged desktop entry churns `update-desktop-database` and
    the shell's own caches for nothing.

    Args:
        target: The destination path.
        text: The complete file body.

    Returns:
        True if the file was written.
    """
    body = text if text.endswith("\n") else text + "\n"
    try:
        if target.read_text(encoding="utf-8") == body:
            return False
    except OSError:
        pass
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        target.chmod(ENTRY_MODE)
    except OSError as exc:
        log.warning("could not write %s: %s", target, exc)
        return False
    log.info("wrote %s (%d bytes)", target, len(body))
    return True


def update_desktop_database(directory: Path | None = None) -> bool:
    """Run `update-desktop-database` so the scheme handler registers.

    Without it `xdg-mime query default x-scheme-handler/odopen` keeps answering
    with whatever was there before.

    Args:
        directory: The applications directory, or `None` for
            `~/.local/share/applications`.

    Returns:
        True if the tool ran and succeeded. False when it is not installed,
        which is not an error — the entry itself is already on disk.
    """
    target = directory if directory is not None else paths.applications_dir()
    return _run_tool([DESKTOP_DATABASE_TOOL, str(target)])


def validate_desktop_file(path: Path | None = None) -> tuple[bool, str]:
    """Run `desktop-file-validate` on an entry.

    Args:
        path: The file to check, or `None` for the installed launcher entry.

    Returns:
        `(ok, output)`. `ok` is True when the validator exits 0 **and prints
        nothing** — a hint (such as the multiple-main-category one) still exits
        0, so a clean run is defined as empty output. When the validator is not
        installed, `(True, "")` is returned: an absent tool is not a failure.
    """
    target = path if path is not None else paths.desktop_file()
    if shutil.which(VALIDATOR) is None:
        return True, ""
    try:
        result = subprocess.run(
            [VALIDATOR, str(target)],
            capture_output=True, text=True, timeout=TOOL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0 and not output, output


def _run_tool(argv: Sequence[str]) -> bool:
    """Run a short-lived desktop helper, tolerating its absence.

    Args:
        argv: The command line.

    Returns:
        True if the tool exited 0.
    """
    if shutil.which(argv[0]) is None:
        log.debug("%s is not installed; skipping", argv[0])
        return False
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True,
                                timeout=TOOL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", argv[0], exc)
        return False
    if result.returncode != 0:
        log.warning("%s exited %d: %s", argv[0], result.returncode,
                    (result.stderr or "").strip())
        return False
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Icons
# ═════════════════════════════════════════════════════════════════════════════

def update_icon_cache(base: Path | None = None) -> bool:
    """Rebuild the GTK icon cache for an icon theme directory.

    Mandatory after installing an emblem: GTK may not notice a new file until
    the next login, and Nautilus silently drops an emblem it cannot resolve —
    logging only a stderr WARNING.

    Args:
        base: The theme root, or `None` for `~/.local/share/icons/hicolor`.

    Returns:
        True if one of the cache tools ran successfully.
    """
    target = base if base is not None else paths.icon_theme_dir()
    for tool in ICON_CACHE_TOOLS:
        if _run_tool([tool, "-f", "-t", str(target)]):
            return True
    return False


def install_icons() -> bool:
    """Install every tray icon, emblem and the app icon, then refresh the cache.

    The art and the layout belong to `ui/icons.py`; this is the desktop-side
    entry point, so a caller that only imports `platform.desktop` still gets the
    cache rebuild that makes the icons visible.

    Returns:
        True if the icon cache was rebuilt.
    """
    from onedriveui.ui import icons

    icons.install_theme_icons()
    return update_icon_cache()


# ═════════════════════════════════════════════════════════════════════════════
# The Nautilus extension
# ═════════════════════════════════════════════════════════════════════════════

def nautilus_extension_source() -> Path | None:
    """Locate the shipped extension source.

    The file itself is WP-14's; this only finds it, so the installer works both
    from a checkout and from an installed package.

    Returns:
        The path, or `None` when the extension has not been shipped yet.
    """
    here = Path(__file__).resolve()
    name = paths.nautilus_ext_file().name
    candidates = (
        here.parent.parent / "ext" / name,
        here.parent.parent.parent / "ext" / name,
        here.parent.parent / "ext" / "nautilus_onedriveui.py",
        here.parent.parent.parent / "ext" / "nautilus_onedriveui.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def install_nautilus_extension(source: str | os.PathLike[str] | None = None) -> bool:
    """Copy the Nautilus extension into `~/.local/share/nautilus-python/extensions`.

    Copied, never symlinked: the loader `dlopen`s the **system**
    `libpython3.14.so` and runs the file under system Python, so a link into a
    checkout that later moves would break the file manager rather than us.
    Nautilus does not hot-reload extensions — the user must restart it, which
    the caller should say.

    Args:
        source: The extension source, or `None` to look for the shipped one.

    Returns:
        True if a file was installed or updated; False when there is nothing to
        install.
    """
    origin = Path(source) if source is not None else nautilus_extension_source()
    if origin is None or not origin.is_file():
        log.info("no Nautilus extension source to install")
        return False
    try:
        text = origin.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("could not read %s: %s", origin, exc)
        return False
    return _write_if_changed(paths.nautilus_ext_file(), text)


def nautilus_extension_installed() -> bool:
    """Whether our extension is present in the per-user extension directory.

    Returns:
        True if the file exists.
    """
    return paths.nautilus_ext_file().is_file()


def remove_nautilus_extension() -> bool:
    """Delete the installed Nautilus extension.

    Returns:
        True if a file was removed.
    """
    try:
        paths.nautilus_ext_file().unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not remove the Nautilus extension: %s", exc)
        return False
    log.info("removed the Nautilus extension")
    return True


__all__ = [
    "FM1_NAME", "FM1_PATH", "FM1_IFACE", "FM1_SHOW_ITEMS", "FM1_SHOW_FOLDERS",
    "FM1_SHOW_PROPERTIES", "FM1_SIGNATURE", "FM1_TIMEOUT_MS",
    "URI_SAFE", "FILE_SCHEME",
    "DESKTOP_ENTRY_VERSION", "DESKTOP_GROUP", "MAIN_CATEGORIES", "CATEGORIES",
    "KEYWORDS", "URI_SCHEME", "MIME_TYPES", "STARTUP_WM_CLASS", "GENERIC_NAME",
    "COMMENT", "DESKTOP_ACTIONS", "ENTRY_MODE", "ENTRY_RULE",
    "FLAG_BACKGROUND", "FLAG_OPEN_FOLDER", "FLAG_PAUSE", "FLAG_SETTINGS",
    "VALIDATOR", "DESKTOP_DATABASE_TOOL", "ICON_CACHE_TOOLS", "TOOL_TIMEOUT_S",
    "USER_DIR_KEYS", "USER_DIRS_FILE", "MACHINE_ID_FILES", "DEVICE_ID_CHARS",
    "bus", "set_bus",
    "file_uri", "uri_to_path",
    "show_in_folder", "show_folders", "show_properties", "open_path", "open_url",
    "device_id",
    "user_dirs", "user_dir", "user_dirs_file",
    "sidebar_bookmarks", "add_sidebar_bookmark", "remove_sidebar_bookmark",
    "assert_one_main_category", "executable_command", "build_desktop_entry",
    "desktop_entry_text", "install_desktop_entry", "remove_desktop_entry",
    "update_desktop_database", "validate_desktop_file",
    "update_icon_cache", "install_icons",
    "nautilus_extension_source", "install_nautilus_extension",
    "nautilus_extension_installed", "remove_nautilus_extension",
]
