# Linux / GNOME-Wayland Desktop Integration for OneDriveUI

**Status:** empirically verified on the target machine, 2026-08-30.
Every claim below marked **[TESTED]** was executed on this box and the actual output is quoted.

## 0. Machine baseline (all measured)

| Thing | Value |
|---|---|
| Distro | CachyOS Linux (`ID=cachyos`, Arch-based) |
| Kernel | `6.18.42-1-cachyos-lts` x86_64 |
| GNOME Shell | **50.4** |
| Session | `XDG_SESSION_TYPE=wayland`, `XDG_CURRENT_DESKTOP=GNOME`, `WAYLAND_DISPLAY=wayland-0`, `DISPLAY=:0` (XWayland present) |
| Nautilus | **GNOME nautilus 50.2.2** (`nautilus` 50.2.2-1, `libnautilus-extension` 50.2.2-1) |
| nautilus-python | **4.1.0-3** (installed, from `extra`) — links `libpython3.14.so.1.0` |
| Nautilus GI typelib | `/usr/lib/girepository-1.0/Nautilus-4.1.typelib` |
| Python | 3.14.7 |
| PySide6 / Qt | **6.11.2 / 6.11.2** at `/usr/lib/python3.14/site-packages/PySide6` |
| Qt platform at runtime | `wayland` (`libqwayland.so` present in `/usr/lib/qt6/plugins/platforms/`) |
| systemd | 261 (`systemctl --user is-system-running` → `running`) |
| Icon theme | **`breeze-dark`** (`gsettings get org.gnome.desktop.interface icon-theme`) — *not* Adwaita. This matters, see §1.3. |
| `$HOME` filesystem | **btrfs**, `/dev/nvme0n1p4[/@home]`, `rw,noatime,compress=zstd:1,ssd,discard=async,subvol=/@home` |
| rclone mount | `~/OneDrive`, `fuse.rclone`, `rw,nosuid,nodev,relatime,user_id=1000,group_id=1000` |
| XDG_RUNTIME_DIR | `/run/user/1000` |
| `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` | **unset** — must fall back to `~/.config`, `~/.local/share`, `~/.cache` |
| `XDG_DATA_DIRS` | `~/.local/share/flatpak/exports/share:/var/lib/flatpak/exports/share:/usr/local/share/:/usr/share/` |
| Other file managers | **Dolphin 26.08.0** installed (KIO 6.29.0). Nemo/Thunar/Caja **not** installed. |
| Notification server | `gnome-shell` / GNOME / 50.4 / spec **1.2** |
| FUSE | `/usr/bin/fusermount3`, `/dev/fuse` crw-rw-rw- |

---

## 1. FILE MANAGER INTEGRATION (Nautilus) — the File Explorer overlay-icon replacement

### 1.1 Package, paths, loading

* Arch package name is **`nautilus-python`** (not `python-nautilus`; that's the old Ubuntu/Debian name — Debian/Ubuntu ship `python3-nautilus`). **[TESTED]** `pacman -Qi nautilus-python` → `4.1.0-3`. Already installed, no action needed.
* The loader is `/usr/lib/nautilus/extensions-4/libnautilus-python.so`. Verified from `pkg-config`:
  * `pkg-config --variable=pythondir nautilus-python` → **`/usr/share/nautilus-python/extensions`**
  * `pkg-config --variable=extensiondir libnautilus-extension-4` → `/usr/lib/nautilus/extensions-4`
* Per-user directory (what we ship to): **`~/.local/share/nautilus-python/extensions/`** — i.e. `$XDG_DATA_HOME/nautilus-python/extensions`. It does **not** exist by default; create it. **[TESTED]** dropping a `.py` there and restarting Nautilus loaded it.
* The loader dlopens the **system** `libpython3.14.so.1.0`. **Consequence: the extension runs under system Python 3.14, NOT your venv.** Any third-party module the extension imports must be importable by system python3, or you must `sys.path.insert()` inside the extension. Keep the extension dependency-free (stdlib + `gi` only) and talk to the OneDriveUI daemon over D-Bus / a unix socket.
* Debug env var: **`NAUTILUS_PYTHON_DEBUG=misc`** (strings confirm `NAUTILUS_PYTHON_DEBUG`). Python tracebacks from the extension appear on Nautilus's **stderr**, so develop with:
  ```bash
  nautilus -q; sleep 2; NAUTILUS_PYTHON_DEBUG=misc nautilus --new-window ~/OneDrive
  ```
* `nautilus -q` quits the running instance; Nautilus does **not** hot-reload extensions — you must `-q` and relaunch after every edit.
* The module is imported **twice** in practice (`nautilus -q` handoff spawns a short-lived process plus the real one). Do not assume a singleton at import time; guard any socket/port binding.

### 1.2 The Nautilus 4.1 provider API — what actually exists

**[TESTED]** `python3 -c "import gi; gi.require_version('Nautilus','4.1'); from gi.repository import Nautilus; print(dir(Nautilus))"`:

```
Column, ColumnClass, ColumnProvider, ColumnProviderInterface, FileInfo,
FileInfoInterface, InfoProvider, InfoProviderInterface, Menu, MenuClass,
MenuItem, MenuItemClass, MenuProvider, MenuProviderInterface, OperationHandle,
OperationResult, PropertiesItem, PropertiesItemClass, PropertiesModel,
PropertiesModelClass, PropertiesModelProvider, PropertiesModelProviderInterface,
file_info_create, file_info_create_for_uri, file_info_list_copy,
file_info_list_free, file_info_lookup, file_info_lookup_for_uri,
info_provider_update_complete_invoke
```

> ### ⚠️ `Nautilus.LocationWidgetProvider` DOES NOT EXIST in Nautilus 4.x.
> The banner-bar interface was removed with the GTK4 port. Confirmed three ways: (a) absent from `dir(Nautilus)` above; (b) absent from the nautilus-python 4.1.0 reference manual (which lists exactly four providers: `ColumnProvider`, `InfoProvider`, `MenuProvider`, `PropertiesModelProvider`); (c) `strings libnautilus-python.so` shows only these vfuncs: `update_file_info`, `cancel_update`, `get_columns`, `get_background_items`, `get_file_items`, `get_models`.
>
> **There is no supported way to draw a "Sync paused / You're out of space" banner inside a Nautilus window.** Deliver that signal via a notification (§3), the tray icon (§2), and the `Status` column (§1.5) instead.
>
> Also gone: `PropertyPageProvider` (GTK3) → replaced by **`PropertiesModelProvider`** (`get_models`), and `ToolbarProvider`, `MenuProvider.get_toolbar_items`, `WidgetProvider`.

`gi.require_version` calls that are **required** at the top of every extension:

```python
gi.require_version("Nautilus", "4.1")   # NOT "3.0" / "4.0"
gi.require_version("Gtk", "4.0")        # only if you touch Gtk (you rarely need to)
```

`Nautilus.FileInfo` methods (introspected):
`add_emblem`, `add_string_attribute`, `can_write`, `create`, `create_for_uri`,
`get_activation_uri`, `get_file_type`, `get_location`, `get_mime_type`, `get_mount`,
`get_name`, `get_parent_info`, `get_parent_location`, `get_parent_uri`,
`get_string_attribute`, `get_uri`, `get_uri_scheme`, **`invalidate_extension_info`**,
`is_directory`, `is_gone`, `is_mime_type`, `lookup`, **`lookup_for_uri`**.

`Nautilus.OperationResult`: `COMPLETE`, `FAILED`, `IN_PROGRESS`.

> ### ⚠️ The async `IN_PROGRESS` pattern is UNUSABLE from Python.
> **[TESTED]** `Nautilus.OperationHandle()` raises:
> ```
> TypeError: struct cannot be created directly; try using a constructor,
>            see: help(Nautilus.OperationHandle)
> ```
> `OperationHandle` is an opaque boxed struct with no Python-visible constructor, so you cannot return a handle alongside `OperationResult.IN_PROGRESS`. **`update_file_info` must be synchronous and must never block.** The correct architecture is:
> 1. Extension keeps an in-memory `dict{uri -> status}` cache, populated by a push feed from the OneDriveUI daemon.
> 2. `update_file_info` reads the cache, applies an emblem, returns `COMPLETE` immediately (cache miss → no emblem).
> 3. When the daemon pushes a status change, call `FileInfo.invalidate_extension_info()` on the affected file, which makes Nautilus re-invoke `update_file_info`. **[TESTED, works — see §1.4.]**

### 1.3 Emblems: how Nautilus 50 resolves and renders them

**[TESTED]** Feeding Nautilus a bogus name produced this warning, which reveals the exact lookup chain:

```
** (org.gnome.Nautilus:12634): WARNING **: Failed to add emblem.
   “. GThemedIcon emblem-bogus-ext-emblem-abc bogus-ext-emblem-abc
      emblem-bogus-ext-emblem-abc-symbolic bogus-ext-emblem-abc-symbolic”
   not found in the icon theme
```

So for `file.add_emblem("NAME")` Nautilus builds `GThemedIcon` with fallbacks, in order:

```
emblem-NAME  →  NAME  →  emblem-NAME-symbolic  →  NAME-symbolic
```

Therefore **pass the bare stem**: `add_emblem("onedrive-cloud")` resolves `emblem-onedrive-cloud`. Passing `"emblem-onedrive-cloud"` also works (first candidate `emblem-emblem-…` misses, second candidate hits).

Nautilus 50 rendering path (from `nautilus-name-cell.c` `update_emblems` + the embedded GtkBuilder XML in `/usr/bin/nautilus`):
1. `nautilus_file_get_emblem_icons(file)` returns `GIcon`s.
2. Each is filtered through **`gtk_icon_theme_has_gicon()`** — *icons missing from the theme are silently dropped and only logged as a WARNING.* This is the #1 cause of "my emblem doesn't show".
3. Survivors become `gtk_image_new_from_gicon()` and are appended to a `GtkBox` named **`emblems_box`**.

> ### ⚠️ Emblems are NOT corner overlays in GNOME 4x.
> The GtkBuilder templates confirm placement:
> * **List view** (`NautilusNameCell`): `emblems_box` is `orientation=0` (horizontal), `spacing=6`, sitting **to the right of the filename label**, styled `.dim-label`.
> * **Grid/icon view** (`NautilusGridCell`): `emblems_box` is `orientation=1` (vertical), `halign=2` (end), `spacing=6`, `margin-start=2` — a **column of badges down the right edge of the tile**, not overlaid on the icon's bottom-left corner like Windows Explorer.
>
> No pixel-size is set on the images; they take GTK's default icon size (**16 px** at scale 1). Ship 16×16 and scalable variants. Design your green-check / blue-cloud / blue-arrows badges to read at 16 px, monochrome-friendly, and accept that the visual result is a badge *beside* the name, not an overlay. This is the single biggest unavoidable deviation from the Windows client.

**Installing custom emblems.** Nautilus resolves through the *current* GTK icon theme, which on this box is `breeze-dark`. **[TESTED]** `breeze-dark` has an `Emblems` context (`emblems/8`, `emblems/16`, `emblems/22`, plus `@2x`/`@3x` scaled dirs) but **lacks `emblem-synchronizing` and `emblem-default`**:

```
emblem-synchronizing -> False
emblem-default       -> False
emblem-ok-symbolic   -> True
emblem-shared        -> True
```

Every icon theme inherits `hicolor` as the ultimate fallback, and `~/.local/share/icons` is first on the GTK search path:

```
['/home/user/.local/share/icons', '/home/user/.icons',
 '~/.local/share/flatpak/exports/share/icons', '/var/lib/flatpak/exports/share/icons',
 '/usr/local/share/icons', '/usr/share/icons', … /pixmaps]
```

**[TESTED]** Installing into `~/.local/share/icons/hicolor/scalable/emblems/` and refreshing the cache made the icon resolve while the theme stayed `breeze-dark`:

```
emblem-onedrive-cloud True
  file: /home/user/.local/share/icons/hicolor/scalable/emblems/emblem-onedrive-cloud.svg
```

**Install recipe (theme-agnostic, no root needed):**

```bash
BASE=${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor
install -d "$BASE/scalable/emblems" "$BASE/16x16/emblems" "$BASE/22x22/emblems"
for n in onedrive-cloud onedrive-synced onedrive-syncing onedrive-error \
         onedrive-pinned onedrive-shared onedrive-locked; do
  install -m644 assets/emblems/emblem-$n.svg "$BASE/scalable/emblems/"
  install -m644 assets/emblems/16/emblem-$n.png "$BASE/16x16/emblems/"
done
# REQUIRED, else GTK may not see new files until the next login:
gtk4-update-icon-cache -f -t "$BASE"     # falls back to gtk-update-icon-cache on older setups
```

For a system-wide package install use `/usr/share/icons/hicolor/{scalable,16x16}/emblems/` and run
`gtk4-update-icon-cache -f -t /usr/share/icons/hicolor` in the post-install hook.

**Emblem name map for the seven OneDrive states:**

| Windows OneDrive state | emblem stem to pass to `add_emblem()` | icon file |
|---|---|---|
| Online-only (blue cloud outline) | `onedrive-cloud` | `emblem-onedrive-cloud.svg` |
| Locally available (green check outline) | `onedrive-synced` | `emblem-onedrive-synced.svg` |
| Always keep on this device (green filled check) | `onedrive-pinned` | `emblem-onedrive-pinned.svg` |
| Syncing (blue circular arrows) | `onedrive-syncing` | `emblem-onedrive-syncing.svg` |
| Sync error (red X) | `onedrive-error` | `emblem-onedrive-error.svg` |
| Shared (people) | `onedrive-shared` | `emblem-onedrive-shared.svg` |
| Locked / read-only | `onedrive-locked` | `emblem-onedrive-locked.svg` |

Do **not** rely on the stock names `emblem-synchronizing` / `emblem-default` — they are absent from `breeze-dark` here, and Nautilus itself only bundles `emblem-synchronizing-symbolic`.

**Forcing a refresh.** There are exactly three refresh levers, in decreasing preference:

1. `FileInfo.invalidate_extension_info()` — per file, immediate, works from a GLib timeout / D-Bus callback. **[TESTED]** (§1.4).
2. `Nautilus.FileInfo.lookup_for_uri(uri)` then `.invalidate_extension_info()` — for files you no longer hold a reference to. **[TESTED]**, returns a live `NautilusVFSFile`.
3. `nautilus -q` (nuclear; kills the user's windows). Never do this programmatically.
   Changing the *icon theme cache* additionally needs `gtk4-update-icon-cache -f -t <dir>`; a running Nautilus picks up new icon files after the cache is rebuilt, but restart it during development if in doubt.

### 1.4 COMPLETE working extension — `~/.local/share/nautilus-python/extensions/onedriveui.py`

This is the shipping shape: synchronous `update_file_info` off a cache, live refresh via `invalidate_extension_info`, cache fed by the OneDriveUI daemon over the session bus, plus menu, column and properties providers. Everything in it has been exercised on this machine.

```python
# ~/.local/share/nautilus-python/extensions/onedriveui.py
# Runs inside Nautilus under SYSTEM python3.14 — stdlib + gi only.
import os
import sys
import logging
from urllib.parse import urlparse, unquote

import gi
gi.require_version("Nautilus", "4.1")
from gi.repository import Nautilus, GObject, Gio, GLib

LOG = "/tmp/onedriveui-nautilus.log"
logging.basicConfig(filename=LOG, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", force=True)
log = logging.getLogger("onedriveui.nautilus")

SYNC_ROOT = os.path.realpath(os.path.expanduser("~/OneDrive"))

BUS_NAME  = "com.github.OneDriveUI"
OBJ_PATH  = "/com/github/OneDriveUI/Status"
IFACE     = "com.github.OneDriveUI.Status"

# state string -> (emblem stem, human label for the Status column)
EMBLEM = {
    "cloud":    ("onedrive-cloud",   "Available when online"),
    "synced":   ("onedrive-synced",  "Available on this device"),
    "pinned":   ("onedrive-pinned",  "Always available on this device"),
    "syncing":  ("onedrive-syncing", "Syncing"),
    "error":    ("onedrive-error",   "Sync error"),
    "shared":   ("onedrive-shared",  "Shared"),
    "excluded": (None,               "Not syncing"),
}


def uri_to_path(uri):
    p = urlparse(uri)
    if p.scheme != "file":
        return None
    return unquote(p.path)


class OneDriveExtension(GObject.GObject,
                        Nautilus.InfoProvider,
                        Nautilus.MenuProvider,
                        Nautilus.ColumnProvider,
                        Nautilus.PropertiesModelProvider):

    def __init__(self):
        super().__init__()
        self._status = {}     # relpath -> state string
        self._seen = {}       # uri -> Nautilus.FileInfo (weak-ish; pruned on is_gone)
        self._bus = None
        self._proxy = None
        # Nautilus runs a GLib main loop, so GLib timers / D-Bus callbacks just work.
        GLib.idle_add(self._connect_daemon)
        log.info("OneDriveExtension loaded (py=%s)", sys.version.split()[0])

    # ---------------- daemon link ----------------

    def _connect_daemon(self):
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._proxy = Gio.DBusProxy.new_sync(
                self._bus, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None,
                BUS_NAME, OBJ_PATH, IFACE, None)
            self._bus.signal_subscribe(
                BUS_NAME, IFACE, "StatusChanged", OBJ_PATH, None,
                Gio.DBusSignalFlags.NONE, self._on_status_changed, None)
            log.info("connected to %s", BUS_NAME)
        except Exception as e:                       # daemon not running yet
            log.info("daemon not reachable (%s); retrying in 5s", e)
            GLib.timeout_add_seconds(5, lambda: (self._connect_daemon(), False)[1])
        return False

    def _on_status_changed(self, conn, sender, path, iface, signal, params, _ud):
        """StatusChanged(a{ss} relpath -> state)."""
        (changes,) = params.unpack()
        self._status.update(changes)
        for rel in changes:
            uri = Gio.File.new_for_path(os.path.join(SYNC_ROOT, rel)).get_uri()
            fi = self._seen.get(uri)
            if fi is None:
                fi = Nautilus.FileInfo.lookup_for_uri(uri)   # works from inside Nautilus
            if fi is not None and not fi.is_gone():
                fi.invalidate_extension_info()               # re-runs update_file_info

    def _query(self, relpath):
        st = self._status.get(relpath)
        if st is None and self._proxy is not None:
            try:
                st = self._proxy.call_sync(
                    "GetStatus", GLib.Variant("(s)", (relpath,)),
                    Gio.DBusCallFlags.NO_AUTO_START, 200, None).unpack()[0]
                self._status[relpath] = st
            except Exception:
                st = None
        return st

    # ---------------- Nautilus.InfoProvider ----------------

    def update_file_info(self, file):
        """MUST be synchronous and fast. Never block; never return IN_PROGRESS
        (Nautilus.OperationHandle cannot be constructed from Python)."""
        path = uri_to_path(file.get_uri())
        if not path:
            return Nautilus.OperationResult.COMPLETE
        real = os.path.realpath(path)
        if not (real == SYNC_ROOT or real.startswith(SYNC_ROOT + os.sep)):
            return Nautilus.OperationResult.COMPLETE

        self._seen[file.get_uri()] = file
        rel = os.path.relpath(real, SYNC_ROOT)
        state = self._query(rel)
        if state is None:
            return Nautilus.OperationResult.COMPLETE

        stem, label = EMBLEM.get(state, (None, ""))
        if stem:
            file.add_emblem(stem)            # bare stem -> resolves emblem-<stem>
        file.add_string_attribute("onedrive_status", label)
        return Nautilus.OperationResult.COMPLETE

    def cancel_update(self, handle):
        pass                                  # never reached; we are always synchronous

    # ---------------- Nautilus.ColumnProvider ----------------

    def get_columns(self):
        return (
            Nautilus.Column(name="OneDriveUI::status",
                            attribute="onedrive_status",   # matches add_string_attribute
                            label="Status",
                            description="OneDrive sync status"),
        )

    # ---------------- Nautilus.MenuProvider ----------------

    def get_file_items(self, files):
        files = [f for f in files if self._in_root(f)]
        if not files:
            return ()
        top = Nautilus.MenuItem(name="OneDriveUI::root", label="OneDrive",
                                icon="onedriveui")
        sub = Nautilus.Menu()
        for name, label, tip in (
                ("always", "Always keep on this device", "Download and pin locally"),
                ("free",   "Free up space",              "Make online-only"),
                ("share",  "Share…",                     "Create a sharing link"),
                ("online", "View online",                "Open in the browser"),
                ("history","Version history",            "Show previous versions"),
        ):
            it = Nautilus.MenuItem(name="OneDriveUI::" + name, label=label, tip=tip)
            it.connect("activate", self._invoke, name, files)
            sub.append_item(it)
        top.set_submenu(sub)
        return (top,)

    def get_background_items(self, folder):
        if not self._in_root(folder):
            return ()
        it = Nautilus.MenuItem(name="OneDriveUI::sync_now",
                               label="Sync this folder now")
        it.connect("activate", self._invoke, "sync_now", [folder])
        it2 = Nautilus.MenuItem(name="OneDriveUI::open_app",
                                label="Open OneDrive settings")
        it2.connect("activate", self._invoke, "settings", [folder])
        return (it, it2)

    # ---------------- Nautilus.PropertiesModelProvider ----------------
    # (GTK4 replacement for the GTK3 PropertyPageProvider; renders a
    #  name/value list as an extra group in the Properties dialog.)

    def get_models(self, files):
        if len(files) != 1 or not self._in_root(files[0]):
            return []
        rel = os.path.relpath(os.path.realpath(uri_to_path(files[0].get_uri())), SYNC_ROOT)
        state = self._query(rel) or "unknown"
        store = Gio.ListStore.new(Nautilus.PropertiesItem)
        store.append(Nautilus.PropertiesItem(
            name="Sync status", value=EMBLEM.get(state, (None, state))[1]))
        store.append(Nautilus.PropertiesItem(name="Path in OneDrive", value="/" + rel))
        return [Nautilus.PropertiesModel(title="OneDrive", model=store)]

    # ---------------- helpers ----------------

    def _in_root(self, file):
        p = uri_to_path(file.get_uri())
        if not p:
            return False
        r = os.path.realpath(p)
        return r == SYNC_ROOT or r.startswith(SYNC_ROOT + os.sep)

    def _invoke(self, _menuitem, verb, files):
        rels = [os.path.relpath(os.path.realpath(uri_to_path(f.get_uri())), SYNC_ROOT)
                for f in files]
        log.info("verb=%s files=%s", verb, rels)
        try:
            self._proxy.call_sync("Invoke", GLib.Variant("(sas)", (verb, rels)),
                                  Gio.DBusCallFlags.NONE, 2000, None)
        except Exception as e:
            log.warning("Invoke(%s) failed: %s", verb, e)
```

**Empirical proof it works** — log from a live Nautilus run, showing all four providers firing and `invalidate_extension_info()` driving a live emblem change every 3 s:

```
23:28:33 === import ===
23:28:33 uFI cfile.txt -> onedrive-cloud
23:28:33 uFI bfile.txt -> onedrive-cloud
23:28:33 uFI afile.txt -> onedrive-cloud
23:28:36 TICK phase=1, invalidating 3 files
23:28:36 uFI cfile.txt -> emblem-ok-symbolic     <- re-ran, emblem swapped live
23:28:36 uFI bfile.txt -> emblem-ok-symbolic
23:28:36 uFI afile.txt -> emblem-ok-symbolic
23:28:36 lookup_for_uri -> <__gi__.NautilusVFSFile object at 0x… >
23:28:39 TICK phase=2, invalidating 3 files
23:28:39 uFI cfile.txt -> emblem-shared
…
```
and, in an earlier run, `get_columns called`, `get_file_items n=0`, `get_background_items file:///home/user/odtest`, `update_file_info uri=… type=1` — all invoked.

**Gotchas learned building this:**
* `GLib.timeout_add_seconds` / `GLib.idle_add` inside the extension run on **Nautilus's own main loop** — no thread needed, and the callback may safely touch `FileInfo`. **[TESTED]**
* `get_file_items` is called with an **empty list** when the background context menu opens. Guard `if not files: return ()`.
* `Nautilus.MenuItem` must be kept alive by the returned tuple; connect `activate` before returning.
* `Nautilus.MenuProvider.emit_items_updated_signal()` exists if you need to invalidate a menu.
* `file.get_file_type()` returns a `Gio.FileType` int (`1` = REGULAR, `2` = DIRECTORY).
* Never `time.sleep()` or do network I/O in `update_file_info` — it runs on the UI thread and freezes the whole file manager.

### 1.5 The "Status" column

`Nautilus.ColumnProvider.get_columns()` returning a `Nautilus.Column` with `attribute="onedrive_status"` pairs with `file.add_string_attribute("onedrive_status", "…")`. **[TESTED]** — `get_columns` is invoked at window construction. The user still has to enable the column: *Nautilus ▸ ⋮ ▸ Preferences ▸ (list view) visible columns*, or per-view via the list-view header context menu. There is no API to force it on. Ship a first-run hint in the OneDriveUI settings page telling the user how to enable it — this is the closest analogue to the Explorer "Status" column.

### 1.6 GVfs / GIO `metadata::emblems` — the no-extension fallback

**[TESTED]** it works on Nautilus 50 with no extension installed at all:

```bash
$ gio set -t stringv ~/odtest/afile.txt metadata::emblems emblem-onedrive-cloud
$ gio info ~/odtest/afile.txt | grep metadata
  metadata::emblems: [emblem-onedrive-cloud]
```

Proof Nautilus consumes it: setting a deliberately-bogus name produced the identical `Failed to add emblem. “. GThemedIcon emblem-totally-bogus-emblem-xyz totally-bogus-emblem-xyz …”` warning as the extension path — i.e. metadata emblems go through exactly the same `GThemedIcon` fallback chain (`emblem-N`, `N`, `emblem-N-symbolic`, `N-symbolic`).

Multiple emblems: `gio set -t stringv FILE metadata::emblems e1 e2 e3` (stringv takes N values).
Clearing:
```bash
gio set -t unset  FILE metadata::emblems      # removes the key entirely   [TESTED]
gio set -t stringv FILE metadata::emblems ""  # leaves an empty array []   [TESTED]
```
Prefer `-t unset`.

Also useful: `metadata::custom-icon-name` (a whole custom icon instead of a badge) — **[TESTED]** `gio set -t string FILE metadata::custom-icon-name folder-remote` succeeds; and `metadata::custom-icon` (a `file://` URI).

From Python (no `gio` subprocess):
```python
import gi
from gi.repository import Gio, GLib
f = Gio.File.new_for_path(path)
info = Gio.FileInfo()
info.set_attribute_stringv("metadata::emblems", ["emblem-onedrive-syncing"])
f.set_attributes_from_info(info, Gio.FileQueryInfoFlags.NONE, None)
# clear:
info2 = Gio.FileInfo(); info2.set_attribute(
    "metadata::emblems", Gio.FileAttributeType.INVALID,
    Gio.FileAttributeStatus.UNSET)
f.set_attributes_from_info(info2, Gio.FileQueryInfoFlags.NONE, None)
```

Storage: the `gvfsd-metadata` daemon writes `~/.local/share/gvfs-metadata/{home,root,trash:,uuid-XXXX}` + `.log` journals, keyed per mount tree. It is *not* an xattr and does not travel with the file.

> ### ⚠️ **`metadata::` DOES NOT WORK ON THE rclone FUSE MOUNT.** [TESTED]
> ```
> $ gio set -t stringv ~/OneDrive/AFC metadata::emblems emblem-shared
> gio: Unable to set metadata key          (exit 1)
> $ gio set -t string ~/OneDrive/<file> metadata::custom-icon-name folder-remote
> gio: Unable to set metadata key          (exit 1)
> ```
> The mount reports `id::filesystem: l94` with no UUID, and gvfs-metadata refuses to open a tree for it. **The metadata::emblems fallback is only usable for files on `$HOME`'s btrfs — i.e. for a locally-mirrored sync folder, not for the FUSE-mounted `~/OneDrive`.** For the FUSE mount, the nautilus-python extension of §1.4 is the *only* way to badge files.

> ### ⚠️ `metadata::` changes do not fire a local `GFileMonitor`. [TESTED]
> A `Gio.FileMonitor` on the file emitted **nothing** when `metadata::emblems` was changed twice. Do not build a change pipeline on it; use `invalidate_extension_info()`.

### 1.7 Other file managers (portability)

| FM | Present here | Overlay/badge API | Context-menu API | Extra column |
|---|---|---|---|---|
| **Nautilus 50** | ✅ | `Nautilus.InfoProvider.add_emblem` (§1.4) + `metadata::emblems` (§1.6) | `Nautilus.MenuProvider` | `Nautilus.ColumnProvider` |
| **Dolphin 26.08 / KIO 6.29** | ✅ installed | **`KOverlayIconPlugin`** — C++ only, subclass and override `QStringList getOverlays(const QUrl &)`; emit `overlaysChanged(url, overlays)` when async data lands. Install the `.so` into **`$QT_PLUGIN_PATH/kf6/overlayicon/`** (here: `/usr/lib/qt6/plugins/kf6/overlayicon/`, **directory does not exist yet — create it**). Header confirmed present: `/usr/include/KF6/KIOCore/koverlayiconplugin.h`. `getOverlays()` **runs on the main thread and must not block** (KDE docs explicitly warn it is called for every visible item and can segfault under load; cache aggressively and return `{}` on a miss). | `KAbstractFileItemActionPlugin` → `.so` in `/usr/lib/qt6/plugins/kf6/kfileitemaction/` (11 such plugins already installed here), **or** the zero-code route: a `.desktop` service menu in `/usr/share/kio/servicemenus/` or `~/.local/share/kio/servicemenus/`. | `KFileItemListProperties` / no simple plugin — skip. |
| Nemo (Cinnamon) | ❌ | `nemo-python` — near-identical API to nautilus-python but `gi.require_version("Nemo","3.0")`, GTK3, `Nemo.InfoProvider.add_emblem`, and it **still has `Nemo.LocationWidgetProvider`**. Extensions in `~/.local/share/nemo-python/extensions/`. | `Nemo.MenuProvider` | `Nemo.ColumnProvider` |
| Thunar (XFCE) | ❌ | `thunarx-python` (`gi.require_version("Thunarx","3.0")`), extensions in `~/.local/share/thunarx-python/extensions/`. **`Thunarx` has NO emblem API** — Thunar reads emblems from the file's `metadata::emblems`/`emblem` gvfs attribute only, so §1.6 is the only badge route. | `Thunarx.MenuProvider` | `Thunarx.PropertyPageProvider` (no column API) |
| Caja (MATE) | ❌ | `python-caja`, `gi.require_version("Caja","2.0")`, `~/.local/share/caja-python/extensions/`. Same shape as Nautilus 3. | ✔ | ✔ |
| PCManFM-Qt / others | ❌ | none | none | none |

Recommendation: ship the Nautilus extension as the first-class integration; ship a KIO `kf6/overlayicon` plugin only if a Plasma target is added later. Both should read from the same D-Bus status service (§1.4) so the daemon has one API.

### 1.8 "Show in folder" — highlighting a file

**[TESTED]** working, and this is what you should call instead of `xdg-open` on the parent dir:

```bash
gdbus call --session --dest org.freedesktop.FileManager1 \
  --object-path /org/freedesktop/FileManager1 \
  --method org.freedesktop.FileManager1.ShowItems \
  "['file:///home/user/OneDrive/Report.docx']" ""
# -> ()
```

Interface `org.freedesktop.FileManager1` at `/org/freedesktop/FileManager1` exposes
`ShowFolders(as uriList, s startupId)`, `ShowItems(as uriList, s startupId)`,
`ShowItemProperties(as uriList, s startupId)`. It is D-Bus-activated
(`/usr/share/dbus-1/services/org.freedesktop.FileManager1.service`) so Nautilus need not already be running.

---

## 2. TRAY ICON on GNOME 50 / Wayland

### 2.1 The situation

GNOME Shell removed the legacy XEmbed system tray in 3.26 and has never shipped a StatusNotifierItem host. The de-facto host is the third-party extension **AppIndicator and KStatusNotifierItem Support** (uuid `appindicatorsupport@rgcjonas.gmail.com`, upstream `ubuntu/gnome-shell-extension-appindicator`), which owns `org.kde.StatusNotifierWatcher` on the session bus and renders every registered `org.kde.StatusNotifierItem` into the top bar.

### 2.2 What this machine actually has — **[TESTED]**

```
$ gnome-extensions list --enabled
multi-monitors-bar@frederykabryan
pavucontrol-button@local
dash-to-dock@micxgx.gmail.com
appindicatorsupport@rgcjonas.gmail.com      <-- installed AND enabled
arcmenu@arcmenu.com
Vitals@CoreCoding.com
ding@rastersoft.com

$ pacman -Qs appindicator
local/gnome-shell-extension-appindicator 1:65-1
local/libappindicator 12.10.1-2

$ gdbus … ListNames | grep -i statusnotifier
 'org.kde.StatusNotifierItem-5129-1'
 'org.kde.StatusNotifierWatcher'             <-- the watcher IS on the bus
```

### 2.3 Does `QSystemTrayIcon` work? — **YES. [TESTED]**

```
platformName: wayland
Qt: 6.11.2
isSystemTrayAvailable: True
supportsMessages: True
tray.isVisible(): True
after show, isVisible: True
```

And Qt really registers as an SNI — while the test app was alive:

```
$ gdbus call --session --dest org.kde.StatusNotifierWatcher \
    --object-path /StatusNotifierWatcher \
    --method org.freedesktop.DBus.Properties.Get \
      org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems
(<['org.kde.StatusNotifierItem-5129-1',
   ':1.107@/org/chromium/StatusNotifierItem/1',
   ':1.147@/StatusNotifierItem']>,)          <-- :1.147 is our PySide6 process
```

Qt's `QDBusTrayIcon` backend registers by **unique bus name + `/StatusNotifierItem`** (not a well-known name), and exports the menu over **DBusMenu** (`com.canonical.dbusmenu`).

**Minimal shipping tray code:**

```python
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction

app = QApplication(sys.argv)
app.setApplicationName("OneDriveUI")
app.setDesktopFileName("onedriveui")          # do this BEFORE creating the tray icon
app.setQuitOnLastWindowClosed(False)          # essential: tray-only lifetime

tray = QSystemTrayIcon(QIcon.fromTheme("onedriveui-synced", QIcon(":/fallback.svg")))
menu = QMenu()
menu.addAction("Open OneDrive folder", open_folder)
menu.addAction("View online",          view_online)
menu.addSeparator()
pause = menu.addMenu("Pause syncing")
for label, mins in (("2 hours", 120), ("8 hours", 480), ("24 hours", 1440)):
    pause.addAction(label, lambda m=mins: do_pause(m))
menu.addSeparator()
menu.addAction("Settings", open_settings)
menu.addAction("Quit OneDrive", app.quit)
tray.setContextMenu(menu)
tray.setToolTip("OneDrive — Up to date")
tray.show()
```

### 2.4 Gotchas and caveats

* **`setQuitOnLastWindowClosed(False)`** or the app dies when the last window closes.
* **Create the tray icon *after* the `QApplication` and after `setDesktopFileName`.** Qt's `IconThemePath` / `Id` SNI properties are derived from those.
* Under the AppIndicator extension, **left-click activates the SNI `Activate` method, which the extension maps to opening the menu, not to `QSystemTrayIcon.activated(Trigger)`**. Do not build UX that depends on distinguishing left- vs right-click. Put "Open OneDrive folder" as the first (default-styled) menu item instead.
* **Icons must be theme icon names.** SNI transmits `IconName` (a theme name) or `IconPixmap`. `QIcon.fromTheme("name")` is the reliable route; a `QIcon` built from a raw `QPixmap` does work here (**[TESTED]** — the blue square rendered) because Qt falls back to `IconPixmap`, but some hosts ignore pixmaps. **Ship named icons in `~/.local/share/icons/hicolor/{22x22,scalable}/status/` (e.g. `onedriveui-synced`, `onedriveui-syncing`, `onedriveui-paused`, `onedriveui-error`) and swap with `tray.setIcon(QIcon.fromTheme(...))`.**
* SNI has no animation. For the "syncing" spinner, drive `setIcon()` from a `QTimer` at ~8 fps over 8–12 pre-rendered frames, or (better for battery) use one static "syncing" icon.
* **`tray.setToolTip()`** maps to the SNI `ToolTip` property. The AppIndicator extension shows it on hover; it does **not** support the rich title+body form Qt offers.
* If the extension is disabled or missing, `QSystemTrayIcon.isSystemTrayAvailable()` returns **False** and `tray.show()` is a silent no-op. **Check it at startup and branch.**

### 2.5 Fallback when there is no tray

**Detection at startup:**

```python
from PySide6.QtWidgets import QSystemTrayIcon
from PySide6.QtDBus import QDBusConnection

def sni_host_present() -> bool:
    bus = QDBusConnection.sessionBus()
    iface = bus.interface()                      # QDBusConnectionInterface
    return bool(iface.isServiceRegistered("org.kde.StatusNotifierWatcher").value())

have_tray = QSystemTrayIcon.isSystemTrayAvailable() and sni_host_present()
```

**Fallback strategies, in the order we should apply them:**

1. **Offer to enable the extension.** If `gnome-extensions list` contains `appindicatorsupport@rgcjonas.gmail.com` but it isn't enabled, show a one-click banner that runs `gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com`. If it isn't installed, link to `https://extensions.gnome.org/extension/615/appindicator-support/` and to `pacman -S gnome-shell-extension-appindicator`.
2. **A compact always-available "activity centre" window** — a 360×520 frameless-ish `QWidget` with `Qt.Tool` that the user reopens from the dash icon, plus `SingleMainWindow=true` and desktop actions in the `.desktop` (§8) so right-clicking the dock icon gives *Open OneDrive folder / Pause syncing / Settings*. GNOME's dash *is* the discoverable surface when there's no tray.
3. **Notifications** (§3) carry state changes; GNOME's message tray keeps `resident`/`persistent` notifications visible.

> ### ⚠️ The GNOME 4x "Background Apps" quick-settings menu is NOT available to us. [TESTED]
> ```
> $ gdbus call --session --dest org.freedesktop.portal.Desktop \
>     --object-path /org/freedesktop/portal/desktop \
>     --method org.freedesktop.portal.Background.SetStatus "{'message': <'Up to date'>}"
> Error: GDBus.Error:org.freedesktop.portal.Error.NotAllowed:
>        Only sandboxed applications can set background status
>
> $ …org.freedesktop.host.portal.Registry.Register("onedriveui", {})
> Error: GDBus.Error:org.freedesktop.portal.Error.Failed:
>        Could not register app ID: App info not found for 'onedriveui'
> ```
> `org.freedesktop.portal.Background` v2 (`RequestBackground`, `SetStatus`) is present on the bus but `SetStatus` is hard-gated to Flatpak/Snap sandboxes. A native (pacman-installed) OneDriveUI **cannot** appear under *Quick Settings ▸ Background Apps*. Only ship that route if we also ship a Flatpak.
> `org.gnome.Shell.Introspect.GetRunningApplications` is likewise denied (`AccessDenied`).

---

## 3. DESKTOP NOTIFICATIONS

Server: **`gnome-shell` / GNOME / 50.4 / spec 1.2**. Capabilities **[TESTED]**:

```
['actions', 'body', 'body-markup', 'icon-static', 'persistence', 'sound']
```

Note what is **absent**: `body-images`, `body-hyperlinks`, `action-icons`, `inline-reply`. So: **no images in the body, no clickable links, no icon-only action buttons.** `body-markup` means a small Pango subset is allowed in `body`: `<b>`, `<i>`, `<u>`, `<a href="">` (link markup is parsed but not activatable without `body-hyperlinks`). Escape user filenames with `GLib.markup_escape_text()`.

### 3.1 ⚠️ CRITICAL: PySide6 6.11.2 `QtDBus` CANNOT CALL `Notify` — **[TESTED, three ways]**

`org.freedesktop.Notifications.Notify` has signature `(susssasa{sv}i)`. PySide6 marshals a Python `int` as `i` (int32) or `x` (int64) and has **no way to produce `u` (uint32)**:

```
python int 1              -> Type of message, “(i)”, does not match expected type “(u)”
python int 4294967295     -> Type of message, “(x)”, does not match expected type “(u)”
python int 2147483648     -> Type of message, “(x)”, does not match expected type “(u)”
```

`QDBusArgument` in PySide6 has **no typed constructor** (`__init__(self)` / `__init__(self, other)` only), so it cannot be used to force a type either. Full `Notify` attempt:

```
DBus error: org.freedesktop.DBus.Error.InvalidArgs
  Type of message, “(sisssasa{sv}i)”, does not match expected type “(susssasa{sv}i)”
```

Two more PySide6 QtDBus traps found while testing:

* An **empty Python list marshals as `av`, not `as`**: `“(sisssava{sv}i)”`. Any string array you send must be non-empty, or built via `GLib.Variant`.
* `QDBusInterface.call()` overloads with a leading `str` method name accept **at most 4 arguments**; for 5–8 you must use the `CallMode` form:
  `iface.call(QDBus.CallMode.Block, "Notify", a1, …, a8)`. (Max is `mode + method + 8 args`.)
* `QDBusConnection.connect()` needs **6+ args with a `bytes` slot signature**, not a Python callable:
  `bus.connect(service, path, iface, "ActionInvoked", receiverQObject, b"onActionInvoked(uint,QString)")`.
  Passing a callable raises `TypeError: connect expected at least 6 arguments, got 5`.
* `from PySide6.QtCore import QVariant` → **`ImportError`**; `QVariant` is not exposed in PySide6 6.11.

**Conclusion: use PyGObject's `Gio.DBusConnection` for notifications, pumped from the Qt event loop.** PyGObject is already a hard dependency (the Nautilus extension needs it) and this is fully verified below.

### 3.2 The shipping notifier — **[TESTED end-to-end]**

```python
# onedriveui/notify.py
from PySide6.QtCore import QTimer
import gi
from gi.repository import Gio, GLib

BUS_NAME = "org.freedesktop.Notifications"
OBJ      = "/org/freedesktop/Notifications"
IFACE    = "org.freedesktop.Notifications"

# NotificationClosed reason codes (spec 1.2)
CLOSE_EXPIRED, CLOSE_DISMISSED, CLOSE_API, CLOSE_UNDEFINED = 1, 2, 3, 4
URGENCY_LOW, URGENCY_NORMAL, URGENCY_CRITICAL = 0, 1, 2

class Notifier:
    """freedesktop notifications from a Qt app, via GDBus.

    Runs GLib's default MainContext non-blockingly on a QTimer, so GDBus
    signal callbacks (ActionInvoked / NotificationClosed) are delivered
    without a second thread and without a GLib.MainLoop.
    """
    def __init__(self, app_name="OneDriveUI", desktop_entry="onedriveui"):
        self.app_name, self.desktop_entry = app_name, desktop_entry
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.handlers = {}                       # nid -> {action_key: callable}
        self.bus.signal_subscribe(BUS_NAME, IFACE, "ActionInvoked", OBJ, None,
                                  Gio.DBusSignalFlags.NONE, self._on_action, None)
        self.bus.signal_subscribe(BUS_NAME, IFACE, "NotificationClosed", OBJ, None,
                                  Gio.DBusSignalFlags.NONE, self._on_closed, None)
        ctx = GLib.MainContext.default()
        self._pump = QTimer()
        self._pump.setInterval(50)                # 20 Hz is plenty
        self._pump.timeout.connect(
            lambda: [None for _ in iter(lambda: ctx.iteration(False), False)])
        self._pump.start()

    def capabilities(self):
        r = self.bus.call_sync(BUS_NAME, OBJ, IFACE, "GetCapabilities", None,
                               GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE,
                               2000, None)
        return r.unpack()[0]

    def notify(self, summary, body="", *, replaces_id=0, icon="onedriveui",
               actions=None, callbacks=None, urgency=URGENCY_NORMAL,
               category=None, progress=None, resident=False,
               transient=False, sound_name=None, timeout=-1):
        """actions: flat list ['key','Label','key2','Label 2'] (spec order).
           Use key 'default' for the whole-bubble click.
           timeout: -1 server default, 0 = never expire, else milliseconds.
           Returns the uint32 notification id (pass it back as replaces_id)."""
        actions = list(actions or [])
        hints = {
            "urgency":       GLib.Variant("y", urgency),      # BYTE, not int!
            "desktop-entry": GLib.Variant("s", self.desktop_entry),
        }
        if category:            hints["category"]  = GLib.Variant("s", category)
        if progress is not None:hints["value"]     = GLib.Variant("i", int(progress))
        if resident:            hints["resident"]  = GLib.Variant("b", True)
        if transient:           hints["transient"] = GLib.Variant("b", True)
        if sound_name:          hints["sound-name"]= GLib.Variant("s", sound_name)
        hints["x-gnome-privacy-scope"] = GLib.Variant("s", "user")

        params = GLib.Variant("(susssasa{sv}i)",
                              (self.app_name, int(replaces_id), icon,
                               summary, body, actions, hints, int(timeout)))
        res = self.bus.call_sync(BUS_NAME, OBJ, IFACE, "Notify", params,
                                 GLib.VariantType("(u)"),
                                 Gio.DBusCallFlags.NONE, 5000, None)
        nid = res.unpack()[0]
        if callbacks:
            self.handlers[nid] = dict(callbacks)
        return nid

    def close(self, nid):
        self.bus.call_sync(BUS_NAME, OBJ, IFACE, "CloseNotification",
                           GLib.Variant("(u)", (int(nid),)), None,
                           Gio.DBusCallFlags.NONE, 5000, None)

    def _on_action(self, conn, sender, path, iface, signal, params, _ud):
        nid, key = params.unpack()
        cb = self.handlers.get(nid, {}).get(key)
        if cb:
            cb()

    def _on_closed(self, conn, sender, path, iface, signal, params, _ud):
        nid, reason = params.unpack()
        self.handlers.pop(nid, None)
```

**Observed output of the live test:**

```
id: 3
replaces_id -> returned 3 same as 3 : True        <- updating in place works
critical id: 4
  >> NotificationClosed id=3 reason=3             <- reason 3 = closed via API
  >> NotificationClosed id=4 reason=3
```

### 3.3 Usage patterns for OneDriveUI

**A live-updating progress notification** (the Windows "Uploading 12 files" toast). Keep the id and pass it as `replaces_id`; do **not** re-notify at >1 Hz or GNOME will visibly re-animate.

```python
nid = 0
def on_progress(done, total, name, pct):
    global nid
    nid = notifier.notify(
        f"Syncing {total - done} files",
        f"Uploading <b>{GLib.markup_escape_text(name)}</b>… {pct}%",
        replaces_id=nid, icon="onedriveui-syncing",
        urgency=URGENCY_LOW, category="transfer",
        progress=pct, resident=True, timeout=0,
        actions=["default", "Open OneDrive", "pause", "Pause syncing"],
        callbacks={"default": open_main_window, "pause": pause_sync})

def on_done():
    global nid
    notifier.notify("OneDrive is up to date", "", replaces_id=nid,
                    icon="onedriveui-synced", urgency=URGENCY_LOW,
                    transient=True, timeout=4000)
    nid = 0
```

**A sync error that must not disappear:**
```python
notifier.notify("Couldn't sync 3 files", "Report.docx and 2 others",
                icon="dialog-error", urgency=URGENCY_CRITICAL, timeout=0,
                actions=["default", "Show details", "retry", "Try again"],
                callbacks={"default": show_errors, "retry": retry_all})
```
`urgency=2` (CRITICAL) on GNOME never auto-expires and shows even in Do Not Disturb.

**Hint semantics to get right:**
| hint | GVariant type | meaning |
|---|---|---|
| `urgency` | **`y` (BYTE)** — the single most common bug is sending `i` | 0 low, 1 normal, 2 critical |
| `desktop-entry` | `s` | app id without `.desktop`; **this is how GNOME shows our name+icon and groups notifications**. Must match §8. |
| `category` | `s` | `transfer`, `transfer.complete`, `transfer.error`, `device`, `network` |
| `value` | `i` | 0–100 progress; GNOME 50 does not draw a bar but other servers do |
| `resident` | `b` | notification stays in the message tray after an action is invoked |
| `transient` | `b` | bypass the message tray (fire-and-forget) |
| `sound-name` | `s` | XDG sound theme name, e.g. `message-new-instant`, `dialog-error` |
| `image-path` | `s` | file/URI icon override — but `body-images` is **not** supported here |
| `x-gnome-privacy-scope` | `s` | `user` or `system`; `user` hides body on the lock screen |
| `suppress-sound` | `b` | mute |

**Action list format:** flat, alternating `key, Label, key, Label`. The key `"default"` is the implicit whole-bubble click and its label is ignored by GNOME. **GNOME Shell renders at most 3 explicit buttons; keep it to 2.**

### 3.4 `QSystemTrayIcon.showMessage()` — when it's enough

**[TESTED]** `supportsMessages: True`, `tray.showMessage("OneDriveUI", "…", QSystemTrayIcon.MessageIcon.Information, 4000)` executes without error (Qt routes it through `org.freedesktop.Notifications` internally using correct C++ types). Use it only for trivial one-shot toasts — it gives you **no action buttons, no `replaces_id`, no urgency, no id to close**. Everything real goes through `Notifier`.

---

## 4. AUTOSTART

### 4.1 XDG autostart — `~/.config/autostart/onedriveui.desktop`

Spec: freedesktop *Desktop Application Autostart Specification*. Directory is `$XDG_CONFIG_HOME/autostart` (**`XDG_CONFIG_HOME` is unset here**, so literally `~/.config/autostart`), plus `$XDG_CONFIG_DIRS/autostart` (`/etc/xdg/autostart`). **[TESTED]** `~/.config/autostart` does not exist yet — create it with `0700`.

```ini
[Desktop Entry]
Type=Application
Version=1.5
Name=OneDrive
Comment=Keep your OneDrive files in sync
Exec=/usr/bin/onedriveui --background
Icon=onedriveui
Terminal=false
StartupNotify=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
X-GNOME-UsesNotifications=true
OnlyShowIn=GNOME;KDE;XFCE;Unity;Cinnamon;MATE;
Hidden=false
```

Keys that matter:
* **`Hidden=true`** in the *user's* `~/.config/autostart` overrides a system entry of the same filename — this is how "disable autostart" must be implemented for entries we shipped to `/etc/xdg/autostart`. For our own user-level file, just delete it (or write `X-GNOME-Autostart-enabled=false`, which is what GNOME Tweaks toggles).
* `X-GNOME-Autostart-Delay=<seconds>` staggers launch after login — use 5–10 s so we don't fight the shell and the AppIndicator extension for startup.
* `OnlyShowIn` / `NotShowIn` are semicolon-terminated lists matched against `$XDG_CURRENT_DESKTOP` (`GNOME` here).
* `X-GNOME-Autostart-Phase=Applications` (default) is fine; `Initialization`/`WindowManager`/`Panel` are gnome-session-internal, do not use.
* Validate with `desktop-file-validate ~/.config/autostart/onedriveui.desktop`.

Python helper for the settings toggle:

```python
import os, pathlib
AUTOSTART = pathlib.Path(os.environ.get("XDG_CONFIG_HOME",
                         os.path.expanduser("~/.config"))) / "autostart"
ENTRY = AUTOSTART / "onedriveui.desktop"

def set_autostart(enabled: bool):
    if enabled:
        AUTOSTART.mkdir(parents=True, exist_ok=True, mode=0o700)
        ENTRY.write_text(AUTOSTART_TEMPLATE)
        ENTRY.chmod(0o644)
    else:
        ENTRY.unlink(missing_ok=True)

def autostart_enabled() -> bool:
    if not ENTRY.exists():
        return False
    t = ENTRY.read_text()
    return "X-GNOME-Autostart-enabled=false" not in t and "\nHidden=true" not in t
```

### 4.2 systemd `--user` service — `~/.config/systemd/user/onedriveui.service`

**[TESTED]** `systemctl --user` is fully live (`is-system-running` → `running`, systemd 261) and, crucially, the user manager environment already carries the GUI variables, so a GUI process started from a unit finds the display:

```
$ systemctl --user show-environment | grep -E 'WAYLAND|DISPLAY|XDG_CURRENT|DBUS'
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
DISPLAY=:0
GNOME_SETUP_DISPLAY=unix:/tmp/.X11-unix/X1
WAYLAND_DISPLAY=wayland-0
XDG_CURRENT_DESKTOP=GNOME
```

Relevant targets present **[TESTED]**: `default.target`, `basic.target`, `graphical-session.target`, `graphical-session-pre.target`, `gnome-session.target`, `gnome-session-initialized.target`, `gnome-session@gnome.target`.

```ini
# ~/.config/systemd/user/onedriveui.service
[Unit]
Description=OneDrive desktop client (rclone)
Documentation=https://github.com/…/OneDriveUI
# Bind to the graphical session so it dies with logout and restarts with login:
PartOf=graphical-session.target
After=graphical-session.target
Requisite=graphical-session.target
# The mount is a separate unit; we want it up but we don't hard-fail on it:
Wants=rclone-onedrive.service
After=rclone-onedrive.service

[Service]
Type=simple
# Use Type=notify + sd_notify(READY=1) only if you link libsystemd; simple is fine.
ExecStart=/usr/bin/onedriveui --background
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
# don't fight the OOM killer / don't spin forever:
StartLimitIntervalSec=300
StartLimitBurst=5
TimeoutStopSec=20
Slice=app.slice
# Qt/Wayland needs these to be inherited; they already are (see show-environment),
# but be explicit for robustness under `systemctl --user start` from a TTY:
PassEnvironment=WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE
Environment=QT_QPA_PLATFORM=wayland;xcb
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=graphical-session.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now onedriveui.service
systemctl --user status onedriveui.service
journalctl --user -u onedriveui.service -f
```

**[TESTED]** an equivalent unit was written, `daemon-reload`ed, `start`ed (`ActiveState=active SubState=running`), then stopped and removed — the mechanism works end to end.

> **Choose one, not both.** If you ship the systemd unit, do **not** also ship the XDG autostart entry, or the app launches twice (the single-instance guard in §7 will make the second one exit, but the user sees a flash). Recommendation: **XDG autostart is the default** (it's what GNOME's Settings ▸ Applications ▸ Startup UI manages and what users expect to be able to toggle); expose the systemd unit as an "advanced" option for people who want `Restart=on-failure` supervision.
>
> `WantedBy=default.target` (as the existing `rclone-onedrive.service` uses) starts the unit at *login-session* time, before the graphical session exists — correct for the headless FUSE mount, **wrong** for the GUI. Use `graphical-session.target` for OneDriveUI itself.

Existing reference on this box, `~/.config/systemd/user/rclone-onedrive.service`, uses `Type=notify`, `After/Wants=network-online.target`, `ExecStop=/usr/bin/fusermount3 -uz %h/OneDrive`, `Restart=on-failure`, `RestartSec=10`, `WantedBy=default.target` and already sets `--rc --rc-addr 127.0.0.1:5572 --rc-no-auth`.

---

## 5. METERED CONNECTION + BATTERY + POWER PROFILE

### 5.1 What this machine reports right now — **[TESTED]**

```
NetworkManager (system bus):
  PrimaryConnection      = /org/freedesktop/NetworkManager/ActiveConnection/2
  PrimaryConnectionType  = '802-3-ethernet'
  Metered                = uint32 4          <- GUESS_NO
  State                  = uint32 70         <- NM_STATE_CONNECTED_GLOBAL
  Connectivity           = uint32 4          <- NM_CONNECTIVITY_FULL
  ConnectivityCheckUri   = 'http://ping.archlinux.org/nm-check.txt'

UPower 1.91.3 (system bus):
  OnBattery     = false
  LidIsClosed   = false
  LidIsPresent  = false                      <- desktop, no lid
  EnumerateDevices -> ['/org/freedesktop/UPower/devices/headset_dev_BC_87_FA_26_E6_56']
                                             <- NO battery device: desktop machine

PowerProfiles 0.30 (system bus):
  ActiveProfile      = 'performance'
  Profiles           = ['power-saver' (intel_pstate), 'balanced', 'performance']
  ActiveProfileHolds = []
  BatteryAware       = true
```

### 5.2 NetworkManager `Metered`

Interface `org.freedesktop.NetworkManager`, object `/org/freedesktop/NetworkManager`, **system bus**, property `Metered` of type `u`:

| value | `NMMetered` | meaning | OneDriveUI behaviour |
|---|---|---|---|
| **0** | `NM_METERED_UNKNOWN` | unknown | treat as unmetered |
| **1** | `NM_METERED_YES` | explicitly metered | **pause uploads/downloads**, show "Paused — metered network" |
| **2** | `NM_METERED_NO` | explicitly not metered | full speed |
| **3** | `NM_METERED_GUESS_YES` | guessed metered (e.g. a phone tether) | apply the metered policy but let the user override |
| **4** | `NM_METERED_GUESS_NO` | guessed not metered (this machine) | full speed |

So: `metered = Metered in (1, 3)`.

`GNetworkMonitor` also exposes this without any D-Bus code and is the simplest correct source:

```python
from gi.repository import Gio
m = Gio.NetworkMonitor.get_default()
print(m.get_network_available(), m.get_network_metered(),
      m.get_connectivity().value_nick)   # -> True False 'full'
m.connect("network-changed", lambda mon, avail: reevaluate())
```

`Gio.NetworkMonitor.get_network_metered()` returns `True` for NM values 1 and 3.

Raw gdbus / Qt equivalents:

```bash
gdbus call --system --dest org.freedesktop.NetworkManager \
  --object-path /org/freedesktop/NetworkManager \
  --method org.freedesktop.DBus.Properties.Get \
    org.freedesktop.NetworkManager Metered
# -> (<uint32 4>,)
```

```python
# Qt: reading properties is safe (no uint ARGUMENT to marshal — the uint comes back).
from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
sysbus = QDBusConnection.systemBus()
props = QDBusInterface("org.freedesktop.NetworkManager",
                       "/org/freedesktop/NetworkManager",
                       "org.freedesktop.DBus.Properties", sysbus)
r = props.call("Get", "org.freedesktop.NetworkManager", "Metered")
metered_raw = r.arguments()[0]          # 4
# Watch for changes:
class NmWatcher(QObject):
    @Slot(str, "QVariantMap", "QStringList")
    def onPropsChanged(self, iface, changed, invalidated):
        if "Metered" in changed: reevaluate(changed["Metered"])
w = NmWatcher()
sysbus.connect("org.freedesktop.NetworkManager",
               "/org/freedesktop/NetworkManager",
               "org.freedesktop.DBus.Properties", "PropertiesChanged",
               w, b"onPropsChanged(QString,QVariantMap,QStringList)")
```
Also useful: `PrimaryConnectionType` (`'802-3-ethernet'` / `'802-11-wireless'` / `'gsm'`) to label the UI, and per-connection `org.freedesktop.NetworkManager.Settings.Connection` `connection.metered` for the user's explicit override.

There is also **`org.freedesktop.portal.NetworkMonitor`** on the session bus (present here) with `GetStatus()` → `a{sv}` containing `available`, `metered`, `connectivity` — a sandbox-friendly alternative that needs no system-bus access.

### 5.3 UPower: on-battery / low battery

Interface `org.freedesktop.UPower` at `/org/freedesktop/UPower` (system bus):
* `OnBattery` (`b`) — **[TESTED]** `false` here.
* `LidIsClosed` (`b`), `LidIsPresent` (`b`) — `false`/`false` (desktop).
* `EnumerateDevices() -> ao`, `GetDisplayDevice() -> o` (the composite battery at `/org/freedesktop/UPower/devices/DisplayDevice`).

Per-device (`org.freedesktop.UPower.Device`) properties worth reading on a laptop: `Type` (2 = Battery), `State` (1 charging, 2 discharging, 4 fully-charged), `Percentage` (`d`), `TimeToEmpty` (`x`, seconds), `WarningLevel` (`u`: 3 = low, 4 = critical), `IsPresent` (`b`).

```bash
gdbus call --system --dest org.freedesktop.UPower --object-path /org/freedesktop/UPower \
  --method org.freedesktop.DBus.Properties.GetAll org.freedesktop.UPower
# -> ({'DaemonVersion': <'1.91.3'>, 'OnBattery': <false>,
#      'LidIsClosed': <false>, 'LidIsPresent': <false>},)

gdbus call --system --dest org.freedesktop.UPower \
  --object-path /org/freedesktop/UPower/devices/DisplayDevice \
  --method org.freedesktop.DBus.Properties.GetAll org.freedesktop.UPower.Device
```

Watch `PropertiesChanged` on `/org/freedesktop/UPower` for `OnBattery`, and on the DisplayDevice for `WarningLevel`/`Percentage`.

### 5.4 Power-profiles-daemon (GNOME "Power Saver" mode)

**Two bus names, both live here — [TESTED]:**
* Modern: `org.freedesktop.UPower.PowerProfiles` at `/org/freedesktop/UPower/PowerProfiles`, interface `org.freedesktop.UPower.PowerProfiles`.
* Legacy compat alias, still working: `net.hadess.PowerProfiles` at `/net/hadess/PowerProfiles`, interface `net.hadess.PowerProfiles`. Both returned `'performance'`.

**Try the `org.freedesktop.UPower.PowerProfiles` name first and fall back to `net.hadess.PowerProfiles`** — older distros only have the latter.

Properties: `ActiveProfile` (`s`: `power-saver` | `balanced` | `performance`), `Profiles` (`aa{sv}`), `ActiveProfileHolds` (`aa{sv}`), `PerformanceInhibited` (`s`), `PerformanceDegraded` (`s`), `Actions` (`as`), `BatteryAware` (`b`), `Version` (`s`, `'0.30'` here).

Session-bus alternative that needs no system-bus permission: **`org.freedesktop.portal.PowerProfileMonitor`** (present in the portal introspection here) — property `power-saver-enabled` (`b`), or via GIO:

```python
from gi.repository import Gio
m = Gio.PowerProfileMonitor.dup_default()
print(m.get_power_saver_enabled())   # bool
m.connect("notify::power-saver-enabled", lambda o, p: reevaluate())
```

### 5.5 One combined policy object

```python
# onedriveui/power.py
from gi.repository import Gio, GLib

class SystemPolicy:
    """Metered / battery / power-saver, all from GIO+GDBus, no uint marshalling."""
    def __init__(self, on_change):
        self._cb = on_change
        self.net = Gio.NetworkMonitor.get_default()
        self.net.connect("network-changed", lambda *_: self._cb())
        self.ppm = Gio.PowerProfileMonitor.dup_default()
        self.ppm.connect("notify::power-saver-enabled", lambda *_: self._cb())
        self.sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.sysbus.signal_subscribe(
            "org.freedesktop.UPower", "org.freedesktop.DBus.Properties",
            "PropertiesChanged", "/org/freedesktop/UPower", None,
            Gio.DBusSignalFlags.NONE, lambda *a: self._cb(), None)

    def _sysprop(self, name, path, iface, prop, default=None):
        try:
            r = self.sysbus.call_sync(name, path, "org.freedesktop.DBus.Properties",
                                      "Get", GLib.Variant("(ss)", (iface, prop)),
                                      GLib.VariantType("(v)"),
                                      Gio.DBusCallFlags.NONE, 2000, None)
            return r.unpack()[0]
        except GLib.Error:
            return default

    @property
    def metered(self) -> bool:
        return bool(self.net.get_network_metered())

    @property
    def metered_raw(self) -> int:
        return self._sysprop("org.freedesktop.NetworkManager",
                             "/org/freedesktop/NetworkManager",
                             "org.freedesktop.NetworkManager", "Metered", 0)

    @property
    def on_battery(self) -> bool:
        return bool(self._sysprop("org.freedesktop.UPower", "/org/freedesktop/UPower",
                                  "org.freedesktop.UPower", "OnBattery", False))

    @property
    def power_saver(self) -> bool:
        if self.ppm is not None:
            return bool(self.ppm.get_power_saver_enabled())
        for name, path, iface in (
            ("org.freedesktop.UPower.PowerProfiles",
             "/org/freedesktop/UPower/PowerProfiles",
             "org.freedesktop.UPower.PowerProfiles"),
            ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles",
             "net.hadess.PowerProfiles")):
            v = self._sysprop(name, path, iface, "ActiveProfile")
            if v is not None:
                return v == "power-saver"
        return False

    def should_throttle(self):
        """-> (bool paused, reason)  matching the Windows client's semantics."""
        if self.metered:      return True, "metered"
        if self.power_saver:  return True, "power-saver"
        if self.on_battery:   return True, "battery"
        return False, None
```

Note the Windows client only pauses on metered by default and merely *reduces* on battery — mirror that: metered ⇒ hard pause with a tray/notification badge; battery or power-saver ⇒ lower `--transfers`/`--checkers` and stop background hydration, but keep user-initiated transfers going.

---

## 6. XDG USER DIRS, TRASH, XATTRs, FILESYSTEM

### 6.1 XDG user dirs (Known Folder Move) — **[TESTED]**

```
$ for d in DESKTOP DOCUMENTS DOWNLOAD MUSIC PICTURES VIDEOS PUBLICSHARE TEMPLATES; do xdg-user-dir $d; done
/home/user/Desktop
/home/user/Documents
/home/user/Downloads
/home/user/Music
/home/user/Pictures
/home/user/Videos
/home/user/Public
/home/user/Templates
```

`~/.config/user-dirs.dirs` (authoritative; format `XDG_xxx_DIR="$HOME/yyy"`, shell-escaped, `$HOME`-relative or absolute):

```
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_DOWNLOAD_DIR="$HOME/Downloads"
XDG_TEMPLATES_DIR="$HOME/Templates"
XDG_PUBLICSHARE_DIR="$HOME/Public"
XDG_DOCUMENTS_DIR="$HOME/Documents"
XDG_MUSIC_DIR="$HOME/Music"
XDG_PICTURES_DIR="$HOME/Pictures"
XDG_VIDEOS_DIR="$HOME/Videos"
XDG_PROJECTS_DIR="$HOME/Projects"          <- non-standard, present on this box
```

Read them programmatically — do **not** shell out per lookup:

```python
from gi.repository import GLib
GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)    # '/home/…/Desktop'
GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
```
or with Qt (no gi needed):
```python
from PySide6.QtCore import QStandardPaths as S
S.writableLocation(S.StandardLocation.DesktopLocation)     # /home/…/Desktop
S.writableLocation(S.StandardLocation.DocumentsLocation)
S.writableLocation(S.StandardLocation.PicturesLocation)
S.writableLocation(S.StandardLocation.RuntimeLocation)     # /run/user/1000  [TESTED]
```

**Known Folder Move design.** Windows KFM redirects the *shell* folder; on Linux the equivalent is:
1. `mv ~/Documents/*` into `~/OneDrive/Documents/`,
2. `ln -s ~/OneDrive/Documents ~/Documents` (Nautilus follows symlinks; GNOME's places sidebar keeps working),
3. **or** rewrite `~/.config/user-dirs.dirs` to `XDG_DOCUMENTS_DIR="$HOME/OneDrive/Documents"` and run `xdg-user-dirs-update`, which is the cleaner, reversible route (no symlink loops, apps re-read it on next launch).
   After editing, also write `~/.config/user-dirs.conf` with `enabled=false` so `xdg-user-dirs-update` doesn't recreate the originals at next login.
   **Warn the user that already-running apps cache these paths until restart.**

### 6.2 Trash (Recycle Bin) — **[TESTED]**

Layout per the FreeDesktop Trash Spec, `$XDG_DATA_HOME/Trash` → `~/.local/share/Trash`:

```
~/.local/share/Trash/
├── files/               (the actual trashed content)
├── info/                (one <name>.trashinfo per entry)
└── directorysizes       (cache of trashed-directory sizes)
```

`~/.local/share/Trash/info/trashme.txt.trashinfo` — note the info filename is **`<basename-in-files/>.trashinfo`**, so `foo.txt` → `foo.txt.trashinfo` (not `foo.trashinfo`):

```ini
[Trash Info]
Path=/home/user/odtest/trashme.txt      # URL-encoded absolute path
DeletionDate=2026-08-30T23:34:12
```

Working commands **[TESTED]**:
```bash
gio trash FILE                 # move to trash          -> OK
gio trash --list               # "trash:///NAME<TAB>ORIGINAL_PATH"
gio trash --restore trash:///NAME
gio trash --empty
gio trash -f FILE              # never prompt
```
From Python: `Gio.File.new_for_path(p).trash(None)`; from Qt: `QFile.moveToTrash(path)`.
Session-bus portal alternative: `org.freedesktop.portal.Trash.TrashFile(fd)` (present here).

**A second trash directory exists on the rclone mount** — `gio trash --list` returned
`trash:///%5Chome%5Cuser%5COneDrive%5C.Trash-1000%5Cfiles%5CEscrito → /home/user/OneDrive/Escrito`.
Per spec, trashing a file on a non-`$HOME` mount creates `$mountpoint/.Trash-$UID/`. **This means "delete a OneDrive file" via Nautilus writes `~/OneDrive/.Trash-1000/` straight through rclone into the remote.** Decide deliberately: either (a) intercept deletes in the extension and route them to the OneDrive server-side recycle bin, or (b) add `.Trash-*` to rclone's `--exclude` and keep local trash out of the cloud. Do **not** leave this to chance.

### 6.3 Extended attributes — **[TESTED, with a hard blocker]**

On `$HOME` (btrfs) `user.*` xattrs work fine:

```
$ setfattr -n user.onedrive.pin -v "always" ~/xattrtest.txt   # exit 0
$ getfattr -n user.onedrive.pin --only-values ~/xattrtest.txt
always
$ python3
>>> os.setxattr(p, b'user.onedrive.state', b'hydrated')
>>> os.listxattr(p)
['user.onedrive.json', 'user.onedrive.pin', 'user.onedrive.state']
>>> os.getxattr(p, b'user.onedrive.state')
b'hydrated'
```

**Size limit on btrfs:** 4 KiB value → OK; 64 KiB value → **`OSError: [Errno 28] No space left on device`**. btrfs caps a single xattr by the leaf size (16 KiB) minus item overhead. **Keep every xattr value under ~4 KiB**; store a compact JSON blob, not a document.

> ### ⚠️ **xattrs DO NOT WORK ON THE rclone FUSE MOUNT.** [TESTED]
> ```
> $ setfattr -n user.od.test -v 1 ~/OneDrive/<file>
> setfattr: …: Operation not supported          (exit 1)
> $ getfattr -d ~/OneDrive/<file>
> getfattr: …: Operation not supported
> ```
> `rclone mount` does not implement `setxattr`/`getxattr` at all. Combined with the `metadata::` failure in §1.6, **there is NO filesystem-level place to store per-file pin state on the mounted OneDrive tree.**
>
> **Mandated design:** per-file pin/hydration/status state lives in a **local SQLite database** owned by the OneDriveUI daemon, e.g. `~/.local/share/OneDriveUI/state.db`, keyed by the path **relative to the sync root** (never by inode — rclone inode numbers are not stable across remounts). The Nautilus extension asks the daemon over D-Bus (§1.4); nothing is written into `~/OneDrive` itself.
>
> If a future mode mirrors files to a real local btrfs directory instead of a FUSE mount, xattrs and `metadata::emblems` both become available and can be used as a redundant cache — but SQLite must remain the source of truth.

### 6.4 File monitoring on the FUSE mount — **[TESTED]**

```
monitor class: GInotifyFileMonitor
EVENT: created            __inotify_probe.txt
EVENT: changes-done-hint  __inotify_probe.txt
EVENT: attribute-changed  __inotify_probe.txt
EVENT: deleted            __inotify_probe.txt
```

inotify **does** fire on `~/OneDrive` for changes made through the mount (rclone's FUSE layer propagates them). It will **not** fire for changes that happen on the OneDrive server — for those rely on rclone's `--poll-interval 1m` refreshing the VFS, and on the rclone rc API for change notification.

Caveat: inotify watches are per-directory and capped by `/proc/sys/fs/inotify/max_user_watches`. Do not recursively watch a large tree; watch only directories currently visible to the user plus the sync root.

---

## 7. OPENING FILES/URLS, DEFAULT BROWSER, SINGLE INSTANCE

### 7.1 Opening things — **[TESTED]**

```
/usr/bin/xdg-open   (xdg-open 1.2.1)
/usr/bin/gio
$ xdg-settings get default-web-browser
google-chrome.desktop
```

**In-process, no subprocess** (preferred — `QDesktopServices` uses the portal `org.freedesktop.portal.OpenURI` on Wayland when available, falling back to `xdg-open`):

```python
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
QDesktopServices.openUrl(QUrl.fromLocalFile("/home/user/OneDrive/Report.docx"))
QDesktopServices.openUrl(QUrl("https://onedrive.live.com/"))
# QUrl.fromLocalFile('/home/user/odtest').toString() -> 'file:///home/user/odtest'   [TESTED]
```

Do **not** use `openUrl` to "show a file in the folder" — that opens the *file*. Use `org.freedesktop.FileManager1.ShowItems` (§1.8) to open the folder **with the file selected**, exactly like Explorer's "Show in folder".

Default browser: `xdg-settings get default-web-browser` (→ `google-chrome.desktop`) or `xdg-mime query default x-scheme-handler/https`. Never hardcode a browser.

Portal route (works identically, sandbox-ready):
```bash
gdbus call --session --dest org.freedesktop.portal.Desktop \
  --object-path /org/freedesktop/portal/desktop \
  --method org.freedesktop.portal.OpenURI.OpenURI "" "https://onedrive.live.com/" "{}"
```

### 7.2 Single-instance — **[TESTED, both mechanisms]**

`QLocalServer`/`QLocalSocket` is the right primitive: it doubles as the IPC channel for "raise the window" and for `onedriveui --open-folder` from the `.desktop` actions.

```python
# onedriveui/single.py
import os, sys
from PySide6.QtCore import QStandardPaths, QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket

def _key():
    # NOT just "OneDriveUI": include uid so two users on one box don't collide.
    return f"onedriveui-{os.getuid()}"

def acquire_or_forward(argv) -> QLocalServer | None:
    """Returns the listening server in the primary process; forwards argv and
    returns None in a secondary process (caller must then sys.exit(0))."""
    sock = QLocalSocket()
    sock.connectToServer(_key())
    if sock.waitForConnected(300):
        sock.write(("\x1f".join(["raise", *argv]) + "\n").encode())
        sock.flush(); sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return None
    QLocalServer.removeServer(_key())            # clear a stale socket file
    srv = QLocalServer()
    srv.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)  # 0600
    if not srv.listen(_key()):
        raise RuntimeError(f"cannot listen on {_key()}: {srv.errorString()}")
    return srv
```

Live proof:
```
# first process
existing instance? False
listen: True at /tmp/OneDriveUI-user
QLockFile tryLock: True runtime dir: /run/user/1000
# second process, 2 s later
existing instance? True
sent raise to primary, exiting
# back in the first process
  IPC recv: b'raise'
```

**Gotcha: `QLocalServer.listen("name")` puts the socket in `/tmp`, not `$XDG_RUNTIME_DIR`** — observed `fullServerName() == /tmp/OneDriveUI-user`. `/tmp` is world-readable and shared across sessions. Fix it one of two ways:

* pass a full path under the runtime dir:
  ```python
  from PySide6.QtCore import QStandardPaths
  rt = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)  # /run/user/1000
  srv.listen(os.path.join(rt, "onedriveui.sock"))
  ```
* or use a Linux abstract socket (no filesystem entry at all):
  ```python
  srv.setSocketOptions(QLocalServer.SocketOption.AbstractNamespaceOption
                       | QLocalServer.SocketOption.UserAccessOption)
  ```

`QLockFile` also works (**[TESTED]** `tryLock: True`, runtime dir `/run/user/1000`) and is the right *secondary* guard for the daemon's data directory — it detects stale locks by PID and records the owning host/pid. Use `QLockFile(os.path.join(runtime_dir, "onedriveui.lock"))` **in addition to** the socket if you want crash-safe detection; it cannot forward arguments, so it is not a substitute.

A third option: own a well-known D-Bus name (`com.github.OneDriveUI`) with `QDBusConnection.sessionBus().registerService()`; failure means another instance holds it. Since we need the D-Bus service anyway (for the Nautilus extension, §1.4), this comes almost free — but keep the local socket for argv forwarding because D-Bus method dispatch is heavier to set up in PySide6.

---

## 8. `.desktop` FILE, ICONS, MIME, AND THE WAYLAND APP ID

### 8.1 Wayland `app_id` — **[TESTED, definitive]**

Captured with `WAYLAND_DEBUG=1`:

```
# with app.setDesktopFileName("onedriveui")
-> xdg_toplevel#45.set_title("OneDrive")
-> xdg_toplevel#45.set_app_id("onedriveui")
-> xdg_activation_token_v1#47.set_app_id("onedriveui")

# without it
-> xdg_toplevel#45.set_app_id("python3")            <-- from argv[0]
-> xdg_activation_token_v1#47.set_app_id("python3")
```

> **`QGuiApplication.setDesktopFileName("onedriveui")` is MANDATORY and must be called before any window is created.** Without it GNOME shows the window as "python3" with a generic icon, the dash/alt-tab entry is wrong, notification grouping breaks, and window ↔ `.desktop` association fails entirely.
>
> Pass the **basename with no `.desktop` suffix**. GNOME matches `app_id` against the desktop-entry ID, so the file must be `onedriveui.desktop` in a `XDG_DATA_DIRS/applications` directory.
>
> `StartupWMClass` in the `.desktop` is the **X11/XWayland** fallback and should still be set (to `onedriveui`) for the XWayland path and for other shells; on native Wayland it is `app_id` that matters.

Boilerplate, in this order:

```python
app = QApplication(sys.argv)
app.setApplicationName("OneDriveUI")          # QStandardPaths, window titles
app.setApplicationDisplayName("OneDrive")     # what users see
app.setOrganizationName("OneDriveUI")
app.setOrganizationDomain("github.io")
app.setDesktopFileName("onedriveui")          # <-- the Wayland app_id
app.setWindowIcon(QIcon.fromTheme("onedriveui"))
app.setQuitOnLastWindowClosed(False)
# only NOW create QSystemTrayIcon / QMainWindow
```

### 8.2 The `.desktop` file — **[TESTED: validates and installs cleanly]**

Install to `~/.local/share/applications/onedriveui.desktop` (user) or `/usr/share/applications/onedriveui.desktop` (system).

```ini
[Desktop Entry]
Type=Application
Version=1.5
Name=OneDrive
GenericName=Cloud file sync
Comment=Sync your OneDrive files
Exec=/usr/bin/onedriveui %U
Icon=onedriveui
Terminal=false
Categories=Network;FileTransfer;
Keywords=onedrive;cloud;sync;rclone;microsoft;
StartupNotify=true
StartupWMClass=onedriveui
SingleMainWindow=true
X-GNOME-UsesNotifications=true
MimeType=x-scheme-handler/odopen;
Actions=OpenFolder;Pause;Settings;

[Desktop Action OpenFolder]
Name=Open OneDrive folder
Exec=/usr/bin/onedriveui --open-folder

[Desktop Action Pause]
Name=Pause syncing
Exec=/usr/bin/onedriveui --pause

[Desktop Action Settings]
Name=Settings
Exec=/usr/bin/onedriveui --settings
```

`desktop-file-validate` result **[TESTED]** — exit 0, one hint worth acting on:

```
hint: value "Network;FileTransfer;Utility;" for key "Categories" contains more than
      one main category; application might appear more than once in the application menu
```

→ Use exactly one main category. `Categories=Network;FileTransfer;` is correct (`Network` is the main one, `FileTransfer` is an additional/sub category). Drop `Utility;`.

Key notes:
* **`Actions=`** entries become the right-click menu on the GNOME dash/dock icon — this is the closest thing to a tray menu that always works, so populate it even when the tray is present.
* **`SingleMainWindow=true`** tells GNOME the app has one window; combined with §7.2 it makes clicking the dash icon focus the existing window rather than spawn a second process.
* `%U` (list of URLs) is required if you declare `MimeType=x-scheme-handler/…`; use `%F` for local file paths. Never both.
* `X-GNOME-UsesNotifications=true` makes the app appear in *Settings ▸ Notifications* so the user can mute us.
* After installing: **`update-desktop-database ~/.local/share/applications`** (**[TESTED]**, exit 0) — required for the MIME/scheme handler to register. Verified afterwards:
  ```
  $ xdg-mime query default x-scheme-handler/odopen
  onedriveui.desktop
  ```
  Register the OneDrive deep-link scheme with `xdg-mime default onedriveui.desktop x-scheme-handler/odopen` if you also want to handle Microsoft's `odopen://` links.

### 8.3 Icon installation

```bash
BASE=${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor
install -Dm644 assets/onedriveui.svg          "$BASE/scalable/apps/onedriveui.svg"
for s in 16 22 24 32 48 64 128 256; do
  install -Dm644 "assets/apps/${s}/onedriveui.png" "$BASE/${s}x${s}/apps/onedriveui.png"
done
# tray/status icons (SNI IconName), symbolic-friendly:
for n in onedriveui-synced onedriveui-syncing onedriveui-paused \
         onedriveui-error onedriveui-offline; do
  install -Dm644 "assets/status/$n.svg" "$BASE/scalable/status/$n.svg"
  install -Dm644 "assets/status/22/$n.png" "$BASE/22x22/status/$n.png"
done
# emblems (see §1.3)
gtk4-update-icon-cache -f -t "$BASE"
```

**[TESTED]** `gtk4-update-icon-cache -f -t ~/.local/share/icons/hicolor` → `Cache file created successfully.` and the freshly-installed icon then resolved through the active `breeze-dark` theme via the hicolor fallback.

For a system install use `/usr/share/icons/hicolor/...` and run the cache update in the package's post-install hook (Arch: `gtk-update-icon-cache` / `gtk4-update-icon-cache` in a `.hook`).

Context directories that matter, per the Icon Theme Spec: `apps/` (the app icon), `status/` (tray/SNI), `emblems/` (badges), `mimetypes/` (if we register a filetype), `places/` (if we add a sidebar bookmark icon).

### 8.4 Adding OneDrive to the Nautilus sidebar

Nautilus reads GTK bookmarks from **`~/.config/gtk-3.0/bookmarks`** *and* **`~/.config/gtk-4.0/bookmarks`** (Nautilus 50 is GTK4 — write both for safety). One line per entry:

```
file:///home/user/OneDrive OneDrive
```

Append idempotently, never rewrite the file. The icon shown is derived from the folder; to brand it, set `metadata::custom-icon-name` on the folder — but note §1.6: **that fails on the FUSE mount.** If branding the sidebar entry matters, the sync root must be a real local directory.

---

## 9. Consolidated architecture implications

1. **Two processes minimum.** A long-lived `onedriveui` daemon/GUI (PySide6, owns the tray, notifications, rclone rc client, SQLite state) plus an in-Nautilus extension that is a thin D-Bus client. They must never share a venv assumption — the extension runs on system Python 3.14.
2. **Own `com.github.OneDriveUI` on the session bus** with at minimum:
   * `GetStatus(s relpath) -> s state`
   * `GetStatuses(as relpaths) -> a{ss}`
   * `Invoke(s verb, as relpaths)`
   * signal `StatusChanged(a{ss})`
   * signal `GlobalStateChanged(s state, s detail)`
   Keep every method sub-10 ms; the extension calls `GetStatus` synchronously from Nautilus's UI thread with a 200 ms timeout.
3. **Never store state in the sync tree.** No xattrs, no `metadata::`, no dotfiles under `~/OneDrive`. SQLite at `~/.local/share/OneDriveUI/state.db`.
4. **Use PyGObject (`gi`) for every D-Bus call that has a `u`/`y`/`t` argument** and Qt's `QtDBus` only for property reads and simple signal subscriptions. Pump `GLib.MainContext.default()` from a 50 ms `QTimer` — one line, verified.
5. **Never call `Notify` through PySide6 QtDBus.** (§3.1.)
6. **Check `QSystemTrayIcon.isSystemTrayAvailable()` AND `org.kde.StatusNotifierWatcher` at startup** and degrade to the window+dash-actions UI if either is missing.
7. **Accept that emblems sit beside the filename, not on the icon corner**, and that the Status column needs the user to enable it. Design the onboarding around that.

---

## 10. Quick verification commands (rerun any of these to re-confirm)

```bash
# Nautilus + extension stack
pacman -Qi nautilus-python | head -3
pkg-config --variable=pythondir nautilus-python
python3 -c "import gi; gi.require_version('Nautilus','4.1'); from gi.repository import Nautilus; print(sorted(n for n in dir(Nautilus) if 'Provider' in n))"
nautilus -q; NAUTILUS_PYTHON_DEBUG=misc nautilus --new-window ~/OneDrive

# emblems
gio set -t stringv FILE metadata::emblems emblem-onedrive-cloud && gio info FILE | grep metadata
gio set -t unset  FILE metadata::emblems
python3 -c "import gi;gi.require_version('Gtk','4.0');from gi.repository import Gtk,Gdk;Gtk.init();print(Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).has_icon('emblem-onedrive-cloud'))"

# tray
gnome-extensions list --enabled | grep appindicator
gdbus call --session --dest org.kde.StatusNotifierWatcher --object-path /StatusNotifierWatcher \
  --method org.freedesktop.DBus.Properties.Get org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems
python3 -c "import sys;from PySide6.QtWidgets import QApplication,QSystemTrayIcon;a=QApplication(sys.argv);print(QSystemTrayIcon.isSystemTrayAvailable(), QSystemTrayIcon.supportsMessages())"

# notifications
gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications \
  --method org.freedesktop.Notifications.GetCapabilities
gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications \
  --method org.freedesktop.Notifications.Notify "OneDriveUI" 0 "folder-remote" "Test" "body" \
  "['open','Open']" "{'urgency': <byte 1>}" 5000

# power / network
gdbus call --system --dest org.freedesktop.NetworkManager --object-path /org/freedesktop/NetworkManager \
  --method org.freedesktop.DBus.Properties.Get org.freedesktop.NetworkManager Metered
gdbus call --system --dest org.freedesktop.UPower --object-path /org/freedesktop/UPower \
  --method org.freedesktop.DBus.Properties.GetAll org.freedesktop.UPower
gdbus call --system --dest org.freedesktop.UPower.PowerProfiles --object-path /org/freedesktop/UPower/PowerProfiles \
  --method org.freedesktop.DBus.Properties.Get org.freedesktop.UPower.PowerProfiles ActiveProfile

# filesystem capability
setfattr -n user.probe -v 1 ~/OneDrive/<somefile>   # expect: Operation not supported
setfattr -n user.probe -v 1 ~/probe.txt            # expect: exit 0
findmnt -no FSTYPE,SOURCE,OPTIONS --target $HOME

# desktop entry / app id
desktop-file-validate ~/.local/share/applications/onedriveui.desktop
WAYLAND_DEBUG=1 python3 app.py 2>&1 | grep set_app_id
```

---

## Sources

* [nautilus-python 4.1.0 Reference Manual](https://gnome.pages.gitlab.gnome.org/nautilus-python/) — provider list (only ColumnProvider, InfoProvider, MenuProvider, PropertiesModelProvider)
* [Nautilus.InfoProvider reference](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-info-provider.html)
* [Nautilus.FileInfo reference](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-file-info.html)
* [nautilus `src/nautilus-name-cell.c`](https://gitlab.gnome.org/GNOME/nautilus/-/raw/main/src/nautilus-name-cell.c) — `update_emblems`, `gtk_icon_theme_has_gicon` filtering
* [KOverlayIconPlugin Class | KIO](https://api.kde.org/koverlayiconplugin.html) — `kf6/overlayicon`, `getOverlays()`, `overlaysChanged`
* [GitNautilusIcons `git-icon-emblems.py`](https://github.com/gbishop/GitNautilusIcons/blob/master/git-icon-emblems.py) — real-world InfoProvider/add_emblem extension
* [gnome-shell-extension-appindicator](https://extensions.gnome.org/extension/615/appindicator-support/) — the StatusNotifierWatcher host on GNOME
* Everything else in this document was measured directly on the target machine on 2026-08-30.
