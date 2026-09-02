"""FROZEN CONTRACT. The icon-name registry.

Segoe Fluent Icons is not licensed for Linux redistribution, so the glyph
codepoints from Windows documentation are unusable. We ship Fluent UI System
Icons (github.com/microsoft/fluentui-system-icons, MIT) at their NATIVE sizes —
never scale a 24 px glyph to 16 px or the stroke weight is wrong.

Tray icons and file-manager emblems are installed as FILES into
~/.local/share/icons/hicolor/... and referenced by NAME, because:
  * StatusNotifierItem under the GNOME AppIndicator extension cannot reliably
    take raw pixmaps — it transmits an IconName.
  * Nautilus 50 SILENTLY DROPS any emblem missing from the active icon theme
    (the theme here is breeze-dark, which lacks emblem-synchronizing and
    emblem-default), logging only a stderr WARNING. Shipping our own into
    hicolor and running gtk4-update-icon-cache is mandatory.

WP-14 ships the real Fluent art into assets/icons/. Until it lands — and
whenever a stem is missing afterwards — every loader here falls back to a
GENERATED placeholder SVG built by _placeholder_svg(). The placeholders are
real, valid, correctly-proportioned SVG, so nothing 404s, nothing renders a
null pixmap, and no call site has to know which it got.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from onedriveui import APP_ID
from onedriveui.bus import BUS
from onedriveui.constants import SPINNER_FRAME_MS
from onedriveui.models import FileState, SyncState, TrayIcon
from onedriveui.paths import (
    icon_app_dir, icon_emblem_dir, icon_status_dir, icon_theme_dir,
)
#: Declared in strings.py beside STATUS_LINE, which the same tray tooltip
#: renders, so a state can never gain a headline without also gaining an icon.
#: Re-exported here — two tables would be free to disagree.
from onedriveui.strings import TRAY_FOR_STATE
from onedriveui.ui import theme

# ═════════════════════════════════════════════════════════════════════════════
# Tray — hicolor/scalable/status/, plus 16/22/24/32/48 px PNG fallbacks.
# Include 22 and 24 explicitly for GNOME's appindicator.
# ═════════════════════════════════════════════════════════════════════════════

TRAY_ICON_NAMES: tuple[str, ...] = (
    "onedriveui-synced",              # plain white cloud (personal)
    "onedriveui-synced-business",     # plain blue cloud (work/school)
    "onedriveui-syncing",             # frame 0 of the spinner
    "onedriveui-paused",              # cloud + pause badge
    "onedriveui-signedout",           # GREY cloud with a diagonal line
    "onedriveui-error",               # red circle + white cross
    "onedriveui-warning",             # yellow triangle
    "onedriveui-info",                # blue circle with 'i'
    "onedriveui-blocked",             # red 'no entry' circle
    "onedriveui-processing",          # reserved; currently aliases -syncing
)

#: 8 frames == a 1 s rotation at SPINNER_FRAME_MS (125 ms). SNI has NO animation
#: support, so the spinner is a QTimer swapping these names via setIcon().
SPINNER_FRAMES: tuple[str, ...] = tuple(f"onedriveui-syncing-{i}" for i in range(1, 9))

#: 8 frames x 125 ms. Asserted so a change to either constant fails here.
SPINNER_PERIOD_MS = len(SPINNER_FRAMES) * SPINNER_FRAME_MS

#: The raw pixel ladder every themed icon is built at. 22 and 24 are explicit:
#: GNOME's appindicator asks for sizes in that band, and QIcon indexes by RAW
#: pixel size, so a dense ladder is what keeps the tray crisp.
TRAY_PIXMAP_SIZES: tuple[int, ...] = (16, 22, 24, 32, 48, 64)

#: The PNG fallbacks written into hicolor/<N>x<N>/ beside the scalable SVG.
INSTALL_PNG_SIZES: tuple[int, ...] = (16, 22, 24, 32, 48)

# ═════════════════════════════════════════════════════════════════════════════
# Emblems — hicolor/scalable/emblems/.
# Nautilus builds a GThemedIcon from add_emblem("NAME") trying, in order:
#   emblem-NAME -> NAME -> emblem-NAME-symbolic -> NAME-symbolic
# so pass the BARE STEM, e.g. "onedriveui-cloud" resolves emblem-onedriveui-cloud.
# ═════════════════════════════════════════════════════════════════════════════

EMBLEM_STEMS: tuple[str, ...] = (
    "onedriveui-cloud",      # online-only
    "onedriveui-local",      # locally available (green check)
    "onedriveui-pinned",     # always keep on this device (filled green circle)
    "onedriveui-syncing",    # in flight
    "onedriveui-error",      # sync problem
    "onedriveui-shared",     # shared with people
    "onedriveui-excluded",   # not syncing
    "onedriveui-locked",     # Personal Vault
)

EMBLEM_FOR_STATE: dict[FileState, str] = {
    FileState.ONLINE_ONLY: "onedriveui-cloud",
    FileState.PARTIAL:     "onedriveui-syncing",
    FileState.LOCAL:       "onedriveui-local",
    FileState.PINNED:      "onedriveui-pinned",
    FileState.DIRTY:       "onedriveui-syncing",
    FileState.SYNCING:     "onedriveui-syncing",
    FileState.EXCLUDED:    "onedriveui-excluded",
    FileState.ERROR:       "onedriveui-error",
    FileState.UNKNOWN:     "",
}

#: The installed file stem for an emblem. Nautilus resolves emblem-NAME first.
EMBLEM_FILE_PREFIX = "emblem-"

# ═════════════════════════════════════════════════════════════════════════════
# In-app glyphs — Fluent UI System Icons (MIT), bundled as SVG at native sizes.
# Key -> asset stem. Available at 12/16/20/24/28/32/48.
# ═════════════════════════════════════════════════════════════════════════════

GLYPHS: dict[str, str] = {
    # navigation / chrome
    "settings": "settings", "back": "arrow_left", "forward": "arrow_right",
    "chevron_down": "chevron_down", "chevron_right": "chevron_right",
    "chevron_up": "chevron_up", "close": "dismiss", "more": "more_horizontal",
    "kebab": "more_vertical", "search": "search", "refresh": "arrow_sync",
    "open_external": "open", "folder": "folder", "folder_open": "folder_open",
    "file": "document", "image": "image", "video": "video", "music": "music_note_2",
    # sync verbs
    "upload": "arrow_upload", "download": "arrow_download",
    "sync": "arrow_sync_circle", "pause": "pause", "play": "play",
    "cloud": "cloud", "cloud_off": "cloud_off", "cloud_sync": "cloud_sync",
    "pin": "pin", "unpin": "pin_off", "delete": "delete", "restore": "arrow_undo",
    "rename": "rename", "share": "share", "link": "link", "copy": "copy",
    "history": "history", "recycle": "delete_dismiss",
    # status
    "check": "checkmark_circle", "error": "error_circle",
    "warning": "warning", "info": "info", "blocked": "prohibited",
    "lock": "lock_closed", "unlock": "lock_open", "person": "person",
    "people": "people", "storage": "hard_drive", "wifi": "wifi_1",
    "battery": "battery_charge", "metered": "cellular_data_1",

    # ── the rest of the surface. Every glyph the Settings window, the Activity
    #    Center, the dialogs, the OOBE wizard, the tray menu and the file
    #    browser ask for. Nothing outside this table is ever requested.
    # settings nav and cards
    "chevron_left": "chevron_left", "nav_sync": "cloud_sync",
    "nav_account": "person_circle", "nav_notifications": "alert",
    "nav_about": "info", "nav_rclone": "options", "advanced": "options", "help": "question_circle",
    "bug": "bug", "reset": "arrow_reset", "resync": "arrow_repeat_all",
    "add": "add", "add_account": "person_add", "remove": "subtract",
    "unlink": "plug_disconnected", "connected": "plug_connected",
    "choose_folders": "folder_link", "excluded": "filter",
    "bandwidth": "top_speed", "autostart": "power", "quit": "sign_out",
    "signin": "key", "get_storage": "premium", "camera": "camera",
    "screenshot": "screenshot", "collab": "people_team", "merge": "arrow_merge",
    "conflict": "arrow_swap", "notebook": "notebook", "mail": "mail",
    "device": "laptop", "computer": "desktop_tower", "globe": "globe",
    "clock": "clock", "timer": "timer", "calendar": "calendar_ltr",
    "star": "star", "shield": "shield", "vault": "lock_shield",
    "eye": "eye", "eye_off": "eye_off", "save": "save", "edit": "edit",
    "move": "folder_arrow_right", "free_space": "broom", "archive": "folder_zip",
    "battery_saver": "battery_saver", "wifi_off": "wifi_off",
    "home": "home", "flag": "flag", "print_disabled": "prohibited",
    # known-folder backup
    "kfm_desktop": "desktop_tower", "kfm_documents": "document_folder",
    "kfm_pictures": "image_multiple", "kfm_music": "music_note_2",
    "kfm_videos": "video_clip",
    # file types shown by the browser and the activity feed
    "file_pdf": "document_pdf", "file_text": "document_text",
    "file_table": "document_table", "file_slides": "slide_layout",
    "file_code": "code", "file_zip": "folder_zip",
    # list chrome
    "sort": "arrow_sort", "sort_up": "arrow_up", "sort_down": "arrow_down",
    "view_list": "apps_list", "view_grid": "grid", "drag": "re_order_dots_vertical",
    "checkbox_checked": "checkbox_checked", "checkbox_unchecked": "checkbox_unchecked",
    "checkbox_mixed": "checkbox_indeterminate", "spinner": "spinner_ios",
    "dismiss_circle": "dismiss_circle", "checkmark": "checkmark",
    "cloud_download": "cloud_arrow_down", "cloud_upload": "cloud_arrow_up",
    "alert_off": "alert_off", "alert_badge": "alert_badge",
}

GLYPH_SIZES: tuple[int, ...] = (12, 16, 20, 24, 28, 32, 48)

APP_ICON_NAME = "onedriveui"     # hicolor/scalable/apps/onedriveui.svg
if APP_ICON_NAME != APP_ID:
    # The .desktop basename, the Wayland app_id and the icon name are one string
    # to the desktop: a mismatch loses the taskbar icon with no error anywhere.
    raise ValueError(f"icons: APP_ICON_NAME {APP_ICON_NAME!r} != APP_ID {APP_ID!r}")

#: The glyph a verb / severity / file state shows in a list row. Declared here
#: so no widget writes an icon-key literal.
GLYPH_FOR_FILE_STATE: dict[FileState, str] = {
    FileState.ONLINE_ONLY: "cloud",
    FileState.PARTIAL:     "cloud_sync",
    FileState.LOCAL:       "check",
    FileState.PINNED:      "pin",
    FileState.DIRTY:       "upload",
    FileState.SYNCING:     "sync",
    FileState.EXCLUDED:    "blocked",
    FileState.ERROR:       "error",
    FileState.UNKNOWN:     "file",
}

# ═════════════════════════════════════════════════════════════════════════════
# The registry. ICON_NAMES is exhaustive: nothing outside it is ever requested,
# and every entry resolves — from an installed asset or from a placeholder.
# ═════════════════════════════════════════════════════════════════════════════

#: The names installed into hicolor and referenced by QIcon.fromTheme().
THEME_ICON_NAMES: tuple[str, ...] = (
    TRAY_ICON_NAMES
    + SPINNER_FRAMES
    + tuple(EMBLEM_FILE_PREFIX + stem for stem in EMBLEM_STEMS)
    + (APP_ICON_NAME,)
)

#: Every icon this application will ever ask for, frozen.
ICON_NAMES: tuple[str, ...] = THEME_ICON_NAMES + tuple(GLYPHS)

# Coverage is structural, not aspirational: a new enum member without an icon
# fails at import rather than painting nothing at 3 a.m.
_missing_tray = [s.name for s in SyncState if s not in TRAY_FOR_STATE]
if _missing_tray:
    raise ValueError(f"icons: TRAY_FOR_STATE is missing {_missing_tray}")
_missing_emblem = [s.name for s in FileState if s not in EMBLEM_FOR_STATE]
if _missing_emblem:
    raise ValueError(f"icons: EMBLEM_FOR_STATE is missing {_missing_emblem}")
_missing_glyph = [s.name for s in FileState if s not in GLYPH_FOR_FILE_STATE]
if _missing_glyph:
    raise ValueError(f"icons: GLYPH_FOR_FILE_STATE is missing {_missing_glyph}")
_bad = [v for v in EMBLEM_FOR_STATE.values() if v and v not in EMBLEM_STEMS]
if _bad:
    raise ValueError(f"icons: EMBLEM_FOR_STATE names unknown stems {_bad}")
_bad = [v for v in GLYPH_FOR_FILE_STATE.values() if v not in GLYPHS]
if _bad:
    raise ValueError(f"icons: GLYPH_FOR_FILE_STATE names unknown glyph keys {_bad}")
_bad = [t.value for t in TRAY_FOR_STATE.values() if t.value and t.value not in TRAY_ICON_NAMES]
if _bad:
    raise ValueError(f"icons: TRAY_FOR_STATE names unknown tray icons {_bad}")
del _missing_tray, _missing_emblem, _missing_glyph, _bad

# ═════════════════════════════════════════════════════════════════════════════
# Asset lookup. WP-14 installs assets/icons/**; until then (and for any stem it
# does not ship) the loaders fall through to a generated placeholder.
# ═════════════════════════════════════════════════════════════════════════════

#: $ONEDRIVEUI_ASSETS overrides for tests and for a system-wide install.
_ENV_ASSETS = "ONEDRIVEUI_ASSETS"


def asset_root() -> Path | None:
    """The assets/icons tree, or None when it has not been installed yet."""
    env = os.environ.get(_ENV_ASSETS)
    candidates = []
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    candidates.append(here.parent.parent / "assets" / "icons")   # onedriveui/assets/icons
    candidates.append(here.parent.parent.parent / "assets" / "icons")  # repo assets/icons
    for c in candidates:
        if c.is_dir():
            return c
    return None


def asset_path(category: str, stem: str, size: int | None = None) -> Path | None:
    """Locate one SVG. `category` in {status, emblems, apps, glyphs}. A glyph is
    looked up at its NATIVE size first (glyphs/24/name.svg), then unsized."""
    root = asset_root()
    if root is None:
        return None
    tries = []
    if size is not None:
        tries.append(root / category / str(size) / f"{stem}.svg")
    tries.append(root / category / f"{stem}.svg")
    for p in tries:
        if p.is_file():
            return p
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Placeholder art. Real SVG, 24x24 user units, stroke-based so it stays legible
# at 12 px, `currentColor` so icon(color=...) can recolour it.
# ─────────────────────────────────────────────────────────────────────────────

_SVG_OPEN = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'width="24" height="24" fill="none">'
)
#: The badge glyphs are drawn in a 0..10 box, so they get their own template.
_BADGE_SVG_OPEN = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" '
    'width="10" height="10" fill="none">'
)
_STROKE = 'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"'
_FILL = 'fill="currentColor"'

#: A cloud outline that reads at 16 px. Used by every cloud-family glyph.
_CLOUD_D = ("M7 18.5h9.6a4.4 4.4 0 0 0 .5-8.77 6 6 0 0 0-11.53-1.6"
            "A4.6 4.6 0 0 0 7 18.5Z")

_ART: dict[str, str] = {
    # ── arrows and chevrons ────────────────────────────────────────────────
    "arrow_left":      '<path d="M11 5.5 4.5 12l6.5 6.5M4.5 12H20" %s/>',
    "arrow_right":     '<path d="M13 5.5 19.5 12 13 18.5M19.5 12H4" %s/>',
    "arrow_up":        '<path d="M5.5 11 12 4.5l6.5 6.5M12 4.5V20" %s/>',
    "arrow_down":      '<path d="M5.5 13 12 19.5 18.5 13M12 19.5V4" %s/>',
    "chevron_left":    '<path d="M14.5 5.5 8 12l6.5 6.5" %s/>',
    "chevron_right":   '<path d="M9.5 5.5 16 12l-6.5 6.5" %s/>',
    "chevron_up":      '<path d="M5.5 14.5 12 8l6.5 6.5" %s/>',
    "chevron_down":    '<path d="M5.5 9.5 12 16l6.5-6.5" %s/>',
    "arrow_upload":    '<path d="M12 16.5v-11M7 10.5 12 5.5l5 5M5 19h14" %s/>',
    "arrow_download":  '<path d="M12 5.5v11M7 11.5l5 5 5-5M5 19h14" %s/>',
    "arrow_undo":      '<path d="M4.5 10.5h9a5 5 0 0 1 0 10h-4M4.5 10.5 9 6M4.5 10.5 9 15" %s/>',
    "arrow_reset":     '<path d="M19.5 12a7.5 7.5 0 1 1-2.6-5.7M19.5 4.5V9h-4.5" %s/>',
    "arrow_sync":      ('<path d="M4.5 12a7.5 7.5 0 0 1 12.8-5.3M19.5 12a7.5 7.5 0 0 1-12.8 5.3" %s/>'
                        '<path d="M17.5 3v4h-4M6.5 21v-4h4" %s/>'),
    "arrow_repeat_all": ('<path d="M6 8.5h9.5a3.5 3.5 0 0 1 0 7H8.5a3.5 3.5 0 0 1 0-7" %s/>'
                         '<path d="M8 6 5.5 8.5 8 11" %s/>'),
    "arrow_swap":      '<path d="M6 9.5h12M15 6.5l3 3-3 3M18 15.5H6M9 12.5l-3 3 3 3" %s/>',
    "arrow_merge":     '<path d="M12 20V11m0 0L7.5 6.5M12 11l4.5-4.5" %s/>',
    "arrow_sort":      '<path d="M7 5.5v13M7 5.5 4.5 8M7 5.5 9.5 8M17 18.5v-13M17 18.5 14.5 16M17 18.5 19.5 16" %s/>',
    "folder_arrow_right": ('<path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2.2h7.8a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" %s/>'
                           '<path d="M10 13h5m-2-2 2 2-2 2" %s/>'),
    # ── chrome ─────────────────────────────────────────────────────────────
    "dismiss":         '<path d="M6 6l12 12M18 6 6 18" %s/>',
    "checkmark":       '<path d="M5 12.5 9.5 17 19 7" %s/>',
    "add":             '<path d="M12 5v14M5 12h14" %s/>',
    "subtract":        '<path d="M5 12h14" %s/>',
    "more_horizontal": ('<circle cx="6" cy="12" r="1.6" %s/><circle cx="12" cy="12" r="1.6" %s/>'
                        '<circle cx="18" cy="12" r="1.6" %s/>'),
    "more_vertical":   ('<circle cx="12" cy="6" r="1.6" %s/><circle cx="12" cy="12" r="1.6" %s/>'
                        '<circle cx="12" cy="18" r="1.6" %s/>'),
    "re_order_dots_vertical": ('<circle cx="9" cy="6" r="1.4" %s/><circle cx="15" cy="6" r="1.4" %s/>'
                               '<circle cx="9" cy="12" r="1.4" %s/><circle cx="15" cy="12" r="1.4" %s/>'
                               '<circle cx="9" cy="18" r="1.4" %s/><circle cx="15" cy="18" r="1.4" %s/>'),
    "search":          '<circle cx="10.5" cy="10.5" r="5.5" %s/><path d="M14.6 14.6 20 20" %s/>',
    # A COG, not a sun. The previous art was a centred circle with eight radial
    # spokes, which at 20 px reads unmistakably as a sun — and because this is
    # the only glyph the Activity Center shows twice (header gear and footer),
    # the window looked like it had two identical unknown-icon placeholders in
    # it. The teeth are one closed outline rather than separate spokes, so a
    # 1.6 px stroke at 16 px does not merge them into the hub.
    "settings":        ('<circle cx="12" cy="12" r="3.1" %s/>'
                        '<path d="M19.5 13.6a7.7 7.7 0 0 0 0-3.2l1.8-1.4'
                        '-1.9-3.2-2.1.9a7.7 7.7 0 0 0-2.8-1.6L14.1 2.6h-3.8'
                        'l-.4 2.5a7.7 7.7 0 0 0-2.8 1.6l-2.1-.9-1.9 3.2 1.8 1.4'
                        'a7.7 7.7 0 0 0 0 3.2l-1.8 1.4 1.9 3.2 2.1-.9'
                        'a7.7 7.7 0 0 0 2.8 1.6l.4 2.5h3.8l.4-2.5'
                        'a7.7 7.7 0 0 0 2.8-1.6l2.1.9 1.9-3.2z" %s/>'),
    "options":         '<path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h9M17 17h3" %s/><circle cx="15" cy="7" r="2" %s/><circle cx="9" cy="12" r="2" %s/><circle cx="15" cy="17" r="2" %s/>',
    "filter":          '<path d="M4 6h16l-6.2 7.2V19l-3.6-2v-3.8Z" %s/>',
    "grid":            '<rect x="4" y="4" width="7" height="7" rx="1.5" %s/><rect x="13" y="4" width="7" height="7" rx="1.5" %s/><rect x="4" y="13" width="7" height="7" rx="1.5" %s/><rect x="13" y="13" width="7" height="7" rx="1.5" %s/>',
    "apps_list":       '<path d="M9 6.5h11M9 12h11M9 17.5h11" %s/><circle cx="5" cy="6.5" r="1.4" %s/><circle cx="5" cy="12" r="1.4" %s/><circle cx="5" cy="17.5" r="1.4" %s/>',
    "open":            '<path d="M14 4.5h5.5V10M19.5 4.5 11 13" %s/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" %s/>',
    # ── containers ─────────────────────────────────────────────────────────
    "folder":          '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2.2h7.8a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" %s/>',
    "folder_open":     '<path d="M3.5 17.3V7.5a2 2 0 0 1 2-2h3.2l2 2.2h7.8a2 2 0 0 1 2 2v1.3" %s/><path d="M3.5 17.3 6 11h15l-2.5 6.3a2 2 0 0 1-1.9 1.2H5.4a2 2 0 0 1-1.9-1.2Z" %s/>',
    "folder_link":     '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2.2h7.8a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" %s/><path d="M10 14.5h4M11.2 12.8 9.8 14.2 11.2 15.6M12.8 12.8l1.4 1.4-1.4 1.4" %s/>',
    "folder_zip":      '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2.2h7.8a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" %s/><path d="M13 10v1.5M13 13v1.5M13 16v1.5" %s/>',
    "document":        '<path d="M6 4.5h7l5 5V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19Z" %s/><path d="M13 4.5v5h5" %s/>',
    "document_folder": '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.2l2 2.2h7.8a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" %s/><path d="M9.5 11.5h5M9.5 14.5h3" %s/>',
    "document_text":   '<path d="M6 4.5h7l5 5V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19Z" %s/><path d="M13 4.5v5h5M8.5 13h7M8.5 16h5" %s/>',
    "document_pdf":    '<path d="M6 4.5h7l5 5V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19Z" %s/><path d="M13 4.5v5h5M9 17v-4h1.6a1.2 1.2 0 0 1 0 2.4H9" %s/>',
    "document_table":  '<path d="M6 4.5h7l5 5V19a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19Z" %s/><path d="M13 4.5v5h5M8.5 12.5h7M8.5 16h7M12 12.5V19" %s/>',
    "slide_layout":    '<rect x="4" y="5" width="16" height="14" rx="2" %s/><path d="M4 9.5h16M9.5 9.5V19" %s/>',
    "code":            '<path d="M9 8.5 5 12l4 3.5M15 8.5 19 12l-4 3.5M13.5 5.5l-3 13" %s/>',
    "image":           '<rect x="4" y="5" width="16" height="14" rx="2" %s/><circle cx="9" cy="10" r="1.6" %s/><path d="M4.5 17 9.5 13l3.5 3 3-2.4 3.5 3" %s/>',
    "image_multiple":  '<rect x="7" y="4.5" width="12.5" height="11" rx="2" %s/><path d="M16.5 18.5H6.5a2 2 0 0 1-2-2V8" %s/><circle cx="11" cy="8.6" r="1.3" %s/>',
    "video":           '<rect x="3.5" y="6" width="12" height="12" rx="2" %s/><path d="M15.5 11l5-3v8l-5-3Z" %s/>',
    "video_clip":      '<rect x="3.5" y="6" width="17" height="12" rx="2" %s/><path d="M10 9.5 15 12l-5 2.5Z" %s/>',
    "music_note_2":    '<path d="M9 17.5V6.5l9-2v11" %s/><ellipse cx="6.75" cy="17.5" rx="2.25" ry="2" %s/><ellipse cx="15.75" cy="15.5" rx="2.25" ry="2" %s/>',
    "camera":          '<path d="M4 8.5h3l1.5-2h7L17 8.5h3a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 20 18.5H4A1.5 1.5 0 0 1 2.5 17v-7A1.5 1.5 0 0 1 4 8.5Z" %s/><circle cx="12" cy="13" r="3.2" %s/>',
    "screenshot":      '<rect x="4" y="4" width="16" height="16" rx="2.5" %s/><path d="M8.5 4v16M4 8.5h16" %s/>',
    # ── cloud family ───────────────────────────────────────────────────────
    "cloud":           '<path d="' + _CLOUD_D + '" %s/>',
    "cloud_off":       '<path d="' + _CLOUD_D + '" %s/><path d="M4 4l16 16" %s/>',
    "cloud_sync":      '<path d="' + _CLOUD_D + '" %s/><path d="M9.6 13.4a2.6 2.6 0 0 1 4.4-1.4M14.4 15a2.6 2.6 0 0 1-4.4 1.4" %s/>',
    "cloud_arrow_up":  '<path d="' + _CLOUD_D + '" %s/><path d="M12 16.5v-4.8M10 13.7l2-2 2 2" %s/>',
    "cloud_arrow_down": '<path d="' + _CLOUD_D + '" %s/><path d="M12 11.7v4.8M10 14.5l2 2 2-2" %s/>',
    "arrow_sync_circle": '<circle cx="12" cy="12" r="8" %s/><path d="M8.5 12a3.5 3.5 0 0 1 6-2.4M15.5 12a3.5 3.5 0 0 1-6 2.4" %s/><path d="M15 8v2.2h-2.2M9 16v-2.2h2.2" %s/>',
    "spinner_ios":     '<path d="M12 4v3.2M12 16.8V20M20 12h-3.2M7.2 12H4M17.7 6.3l-2.3 2.3M8.6 15.4l-2.3 2.3M17.7 17.7l-2.3-2.3M8.6 8.6 6.3 6.3" %s/>',
    # ── status ─────────────────────────────────────────────────────────────
    "checkmark_circle": '<circle cx="12" cy="12" r="8.2" %s/><path d="M8.2 12.3 11 15l4.8-5.4" %s/>',
    "error_circle":    '<circle cx="12" cy="12" r="8.2" %s/><path d="M12 7.6v5.2" %s/><circle cx="12" cy="16.2" r="1" %s/>',
    "dismiss_circle":  '<circle cx="12" cy="12" r="8.2" %s/><path d="M9.2 9.2 14.8 14.8M14.8 9.2 9.2 14.8" %s/>',
    "warning":         '<path d="M12 4.6 21 19.4H3Z" %s/><path d="M12 10.2v4.2" %s/><circle cx="12" cy="17" r="1" %s/>',
    "info":            '<circle cx="12" cy="12" r="8.2" %s/><path d="M12 11.4v5" %s/><circle cx="12" cy="8.2" r="1" %s/>',
    "prohibited":      '<circle cx="12" cy="12" r="8.2" %s/><path d="M6.2 17.8 17.8 6.2" %s/>',
    "alert":           '<path d="M6.5 10a5.5 5.5 0 0 1 11 0v4l1.6 3H4.9l1.6-3Z" %s/><path d="M10 19.5a2 2 0 0 0 4 0" %s/>',
    "alert_off":       '<path d="M6.5 10a5.5 5.5 0 0 1 11 0v4l1.6 3H4.9l1.6-3Z" %s/><path d="M4 4l16 16" %s/>',
    "alert_badge":     '<path d="M6.5 10a5.5 5.5 0 0 1 11 0v4l1.6 3H4.9l1.6-3Z" %s/><circle cx="18" cy="6.5" r="2.6" %s/>',
    "shield":          '<path d="M12 3.5 19 6v6c0 4-3 7-7 8.5C8 19 5 16 5 12V6Z" %s/>',
    "shield_checkmark": '<path d="M12 3.5 19 6v6c0 4-3 7-7 8.5C8 19 5 16 5 12V6Z" %s/><path d="M9 12.2 11.3 14.5 15.2 10" %s/>',
    "lock_shield":     '<path d="M12 3.5 19 6v6c0 4-3 7-7 8.5C8 19 5 16 5 12V6Z" %s/><rect x="9.4" y="11.4" width="5.2" height="4.4" rx="1" %s/><path d="M10.6 11.4v-1.2a1.4 1.4 0 0 1 2.8 0v1.2" %s/>',
    "lock_closed":     '<rect x="5.5" y="10.5" width="13" height="9" rx="2" %s/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5" %s/>',
    "lock_open":       '<rect x="5.5" y="10.5" width="13" height="9" rx="2" %s/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 6.8-1.2" %s/>',
    "key":             '<circle cx="8" cy="12" r="3.5" %s/><path d="M11.5 12H20M17.5 12v3M14.5 12v2.2" %s/>',
    # ── people ─────────────────────────────────────────────────────────────
    "person":          '<circle cx="12" cy="8.5" r="3.5" %s/><path d="M5.5 19.5a6.5 6.5 0 0 1 13 0" %s/>',
    "person_circle":   '<circle cx="12" cy="12" r="8.2" %s/><circle cx="12" cy="10" r="2.6" %s/><path d="M7.4 18.2a5 5 0 0 1 9.2 0" %s/>',
    "person_add":      '<circle cx="9.5" cy="8.5" r="3.2" %s/><path d="M3.8 19a5.8 5.8 0 0 1 11.4 0" %s/><path d="M18 8v6M15 11h6" %s/>',
    "people":          '<circle cx="9" cy="9" r="3" %s/><path d="M3.5 18.5a5.5 5.5 0 0 1 11 0" %s/><path d="M15.5 7.2a3 3 0 0 1 0 5.6M16.5 14.5a5 5 0 0 1 4 4" %s/>',
    "people_team":     '<circle cx="12" cy="7.5" r="2.8" %s/><path d="M7.5 14.5a4.5 4.5 0 0 1 9 0" %s/><circle cx="5" cy="12.5" r="2.2" %s/><circle cx="19" cy="12.5" r="2.2" %s/><path d="M2.5 19.5a3.5 3.5 0 0 1 5-3.1M21.5 19.5a3.5 3.5 0 0 0-5-3.1" %s/>',
    "share":           '<circle cx="17" cy="6" r="2.6" %s/><circle cx="6.5" cy="12" r="2.6" %s/><circle cx="17" cy="18" r="2.6" %s/><path d="M8.8 10.7 14.7 7.3M8.8 13.3l5.9 3.4" %s/>',
    "link":            '<path d="M10.5 13.5a3.5 3.5 0 0 0 5 0l2.5-2.5a3.5 3.5 0 0 0-5-5L11.8 7.3" %s/><path d="M13.5 10.5a3.5 3.5 0 0 0-5 0L6 13a3.5 3.5 0 0 0 5 5l1.2-1.2" %s/>',
    "mail":            '<rect x="3.5" y="6" width="17" height="12" rx="2" %s/><path d="M4 7.5 12 13l8-5.5" %s/>',
    # ── edit / manage ──────────────────────────────────────────────────────
    "edit":            '<path d="M5 19h3l9.3-9.3a2.1 2.1 0 0 0-3-3L5 16Z" %s/><path d="M14.5 6.5l3 3" %s/>',
    "rename":          '<path d="M4 7.5v9M4 7.5h4M4 16.5h4M8 12h6" %s/><rect x="12.5" y="6.5" width="8" height="11" rx="1.6" %s/>',
    "copy":            '<rect x="8.5" y="8.5" width="11" height="11" rx="2" %s/><path d="M15.5 5.5h-9A2 2 0 0 0 4.5 7.5v9" %s/>',
    "save":            '<path d="M5 5.5h11L19 8.5V19a1.5 1.5 0 0 1-1.5 1.5h-12A1.5 1.5 0 0 1 4 19V7a1.5 1.5 0 0 1 1-1.5Z" %s/><path d="M8 5.5v4h6v-4M8 20v-5h8v5" %s/>',
    "delete":          '<path d="M5 7h14M10 7V5.5h4V7M6.5 7l1 12.5h9L17.5 7" %s/><path d="M10.5 10.5v6M13.5 10.5v6" %s/>',
    "delete_dismiss":  '<path d="M5 7h14M10 7V5.5h4V7M6.5 7l.8 10" %s/><circle cx="16" cy="16" r="4.2" %s/><path d="M14.5 14.5l3 3M17.5 14.5l-3 3" %s/>',
    "history":         '<path d="M4.5 12a7.5 7.5 0 1 0 2.6-5.7" %s/><path d="M4.5 4.5V9H9" %s/><path d="M12 8v4.4l3 1.8" %s/>',
    "pin":             '<path d="M9 3.5h6l-.8 6 3.3 3.3H6.5L9.8 9.5Z" %s/><path d="M12 12.8V20.5" %s/>',
    "pin_off":         '<path d="M9 3.5h6l-.8 6 3.3 3.3H6.5L9.8 9.5Z" %s/><path d="M12 12.8V20.5M4 4l16 16" %s/>',
    "broom":           '<path d="M14.5 4 10 8.5M12.8 10.2 6 17l-1.5 3 3-1.5 6.8-6.8Z" %s/><path d="M12 6.5 17.5 12" %s/>',
    "premium":         '<path d="M4 17.5 6 7l4.2 4.5L12 5.5l1.8 6L18 7l2 10.5Z" %s/><path d="M4 20h16" %s/>',
    "top_speed":       '<path d="M4.5 17.5a7.5 7.5 0 1 1 15 0Z" %s/><path d="M12 15.5 16 9.5" %s/>',
    "power":           '<path d="M12 4v8" %s/><path d="M7.6 7.2a7 7 0 1 0 8.8 0" %s/>',
    "sign_out":        '<path d="M14 7.5V5.5a1.5 1.5 0 0 0-1.5-1.5h-6A1.5 1.5 0 0 0 5 5.5v13A1.5 1.5 0 0 0 6.5 20h6a1.5 1.5 0 0 0 1.5-1.5v-2" %s/><path d="M9.5 12H20M17 9l3 3-3 3" %s/>',
    "plug_disconnected": '<path d="m4.5 19.5 3.6-3.6M19.5 4.5l-3.6 3.6" %s/><path d="m9.4 9.4 5.2 5.2-2 2a3.7 3.7 0 0 1-5.2-5.2Z" %s/><path d="M12.4 6.4a3.7 3.7 0 0 1 5.2 5.2l-1 1" %s/>',
    "plug_connected":  '<path d="M8.5 4.5v4M14 4.5v4M6.5 8.5h11v3a5.5 5.5 0 0 1-11 0Z" %s/><path d="M12 17v3" %s/>',
    "hard_drive":      '<rect x="3.5" y="6" width="17" height="12" rx="2" %s/><path d="M3.5 13h17" %s/><circle cx="17" cy="15.5" r="1.1" %s/>',
    "laptop":          '<rect x="5" y="5.5" width="14" height="9.5" rx="1.6" %s/><path d="M3 18.5h18" %s/>',
    "desktop_tower":   '<rect x="4" y="4.5" width="8" height="15" rx="1.6" %s/><path d="M6.5 8h3M6.5 11h3" %s/><rect x="14" y="7.5" width="6" height="9" rx="1.4" %s/>',
    "globe":           '<circle cx="12" cy="12" r="8.2" %s/><path d="M3.8 12h16.4M12 3.8c2.2 2.4 3.3 5.2 3.3 8.2S14.2 17.8 12 20.2c-2.2-2.4-3.3-5.2-3.3-8.2S9.8 6.2 12 3.8Z" %s/>',
    "home":            '<path d="M4 11 12 4.5 20 11" %s/><path d="M6.5 9.6V19h11V9.6" %s/>',
    "flag":            '<path d="M6 20V4.5M6 5.5h11l-2 3.5 2 3.5H6" %s/>',
    "star":            '<path d="m12 4.5 2.5 5 5.5.8-4 3.9.95 5.5L12 17.1l-4.95 2.6.95-5.5-4-3.9 5.5-.8Z" %s/>',
    "clock":           '<circle cx="12" cy="12" r="8.2" %s/><path d="M12 7.4V12l3.2 1.9" %s/>',
    "timer":           '<circle cx="12" cy="13.5" r="6.8" %s/><path d="M12 10v3.5M9.5 3.5h5" %s/>',
    "calendar_ltr":    '<rect x="4" y="5.5" width="16" height="14" rx="2" %s/><path d="M4 10h16M8.5 3.8v3.4M15.5 3.8v3.4" %s/>',
    "notebook":        '<path d="M6.5 4.5h11A1.5 1.5 0 0 1 19 6v12a1.5 1.5 0 0 1-1.5 1.5h-11Z" %s/><path d="M6.5 4.5v15M9.5 4.5v15" %s/>',
    "bug":             '<rect x="8" y="8" width="8" height="10" rx="4" %s/><path d="M8 12H4.5M16 12h3.5M8.6 8.6 6 6M15.4 8.6 18 6M8.6 17 6 19.5M15.4 17 18 19.5" %s/>',
    "question_circle": '<circle cx="12" cy="12" r="8.2" %s/><path d="M9.9 9.6a2.2 2.2 0 1 1 2.6 2.6v1.4" %s/><circle cx="12.4" cy="16.4" r="1" %s/>',
    "eye":             '<path d="M2.8 12S6.5 6.5 12 6.5 21.2 12 21.2 12 17.5 17.5 12 17.5 2.8 12 2.8 12Z" %s/><circle cx="12" cy="12" r="2.8" %s/>',
    "eye_off":         '<path d="M2.8 12S6.5 6.5 12 6.5c1.6 0 3 .5 4.2 1.1M20.2 10.4c.6.8 1 1.6 1 1.6S17.5 17.5 12 17.5c-1 0-1.9-.2-2.7-.5" %s/><path d="M4 4l16 16" %s/>',
    "pause":           '<path d="M9 5.5v13M15 5.5v13" %s/>',
    "play":            '<path d="M8 5.5 18.5 12 8 18.5Z" %s/>',
    "wifi_1":          '<path d="M9 15.4a4.3 4.3 0 0 1 6 0" %s/><circle cx="12" cy="18.4" r="1.1" %s/>',
    "wifi_off":        '<path d="M9 15.4a4.3 4.3 0 0 1 6 0" %s/><circle cx="12" cy="18.4" r="1.1" %s/><path d="M4 4l16 16" %s/>',
    "cellular_data_1": '<path d="M4.5 19.5h2.6M9.6 19.5V16M14.2 19.5v-6.5M18.8 19.5V6.5" %s/>',
    "battery_charge":  '<rect x="3" y="8" width="16" height="8" rx="2" %s/><path d="M21 10.5v3" %s/><path d="M11.5 9.5 9 12.4h3l-2.2 2.6" %s/>',
    "battery_saver":   '<rect x="3" y="8" width="16" height="8" rx="2" %s/><path d="M21 10.5v3M6.5 12h4M8.5 10v4" %s/>',
    "checkbox_checked": '<rect x="4.5" y="4.5" width="15" height="15" rx="3" %s/><path d="M8.2 12.2 11 15l4.8-5.6" %s/>',
    "checkbox_unchecked": '<rect x="4.5" y="4.5" width="15" height="15" rx="3" %s/>',
    "checkbox_indeterminate": '<rect x="4.5" y="4.5" width="15" height="15" rx="3" %s/><path d="M8.5 12h7" %s/>',
}


def _art_body(stem: str) -> str:
    """The placeholder body for an asset stem. Falls through a family match and
    finally to a generic mark, so EVERY stem yields real, valid SVG."""
    art = _ART.get(stem)
    if art is None:
        for family, alias in (
            ("chevron_", "chevron_right"), ("arrow_", "arrow_right"),
            ("cloud", "cloud"), ("folder", "folder"), ("document", "document"),
            ("person", "person"), ("people", "people"), ("lock", "lock_closed"),
            ("checkbox", "checkbox_unchecked"), ("alert", "alert"),
            ("battery", "battery_charge"), ("wifi", "wifi_1"),
            ("cellular", "cellular_data_1"), ("shield", "shield"),
            ("image", "image"), ("video", "video"), ("music", "music_note_2"),
        ):
            if stem.startswith(family):
                art = _ART[alias]
                break
    if art is None:
        # Generic placeholder: a rounded square with a centred dot. Deliberately
        # unlike any real glyph, so a missing asset is visible, not silent.
        art = '<rect x="4.5" y="4.5" width="15" height="15" rx="3.5" %s/><circle cx="12" cy="12" r="2" %s/>'
    return art % tuple([_STROKE] * art.count("%s"))


def placeholder_svg(stem: str) -> bytes:
    """A complete, valid placeholder SVG document for one asset stem."""
    return (_SVG_OPEN + _art_body(stem) + "</svg>").encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Tray, emblem and app art — coloured, self-contained, legible at 16 px.
# ─────────────────────────────────────────────────────────────────────────────

#: The tray badge palette. Windows uses these exact hues.
BADGES: dict[str, tuple[str, str]] = {          # key -> (fill, glyph)
    "syncing":  ("#0078D4", "arrows"),
    "ok":       ("#0F7B0F", "check"),
    "paused":   ("#616161", "pause"),
    "error":    ("#C42B1C", "cross"),
    "warn":     ("#9D5D00", "bang"),
    "info":     ("#0078D4", "bang"),
    "blocked":  ("#C42B1C", "slash"),
    "offline":  ("#616161", "slash"),
    "locked":   ("#5C5C5C", "lock"),
    "shared":   ("#0078D4", "people"),
    "pinned":   ("#0F7B0F", "dot"),
    "excluded": ("#8A8A8A", "slash"),
}

#: Re-exported from the theme so no module reads the logo geometry twice.
LOGO_COLORS = theme.LOGO_COLORS
LOGO_VIEWBOX = theme.LOGO_VIEWBOX
METRICS_TRAY_BADGE = theme.METRICS["tray_badge"]
METRICS_TRAY_BADGE_RING = theme.METRICS["tray_badge_ring"]

CLOUD_WHITE = "#FFFFFF"
CLOUD_BUSINESS = "#0078D4"
CLOUD_GREY = "#8A8A8A"

#: base name -> (cloud fill, badge key or "") for the generated tray art.
_TRAY_ART: dict[str, tuple[str, str]] = {
    "onedriveui-synced":          (CLOUD_WHITE, ""),
    "onedriveui-synced-business": (CLOUD_BUSINESS, ""),
    "onedriveui-syncing":         (CLOUD_WHITE, "syncing"),
    "onedriveui-processing":      (CLOUD_WHITE, "syncing"),
    "onedriveui-paused":          (CLOUD_WHITE, "paused"),
    "onedriveui-signedout":       (CLOUD_GREY, "offline"),
    "onedriveui-error":           (CLOUD_WHITE, "error"),
    "onedriveui-warning":         (CLOUD_WHITE, "warn"),
    "onedriveui-info":            (CLOUD_WHITE, "info"),
    "onedriveui-blocked":         (CLOUD_WHITE, "blocked"),
}

#: emblem stem -> (ring fill, badge glyph)
_EMBLEM_ART: dict[str, tuple[str, str]] = {
    "onedriveui-cloud":    ("#0078D4", "cloud"),
    "onedriveui-local":    ("#0F7B0F", "check"),
    "onedriveui-pinned":   ("#0F7B0F", "dot"),
    "onedriveui-syncing":  ("#0078D4", "arrows"),
    "onedriveui-error":    ("#C42B1C", "cross"),
    "onedriveui-shared":   ("#0078D4", "people"),
    "onedriveui-excluded": ("#8A8A8A", "slash"),
    "onedriveui-locked":   ("#5C5C5C", "lock"),
}

#: Badge glyph paths, drawn in a 0..10 box and translated into place.
_BADGE_GLYPHS: dict[str, str] = {
    "check":  '<path d="M2.4 5.2 4.2 7 7.6 3.2" fill="none" stroke="#FFFFFF" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
    "cross":  '<path d="M3.2 3.2 6.8 6.8M6.8 3.2 3.2 6.8" fill="none" stroke="#FFFFFF" stroke-width="1.3" stroke-linecap="round"/>',
    "pause":  '<path d="M3.8 3.1v3.8M6.2 3.1v3.8" fill="none" stroke="#FFFFFF" stroke-width="1.3" stroke-linecap="round"/>',
    "bang":   '<path d="M5 2.7v3.1" fill="none" stroke="#FFFFFF" stroke-width="1.3" stroke-linecap="round"/><circle cx="5" cy="7.4" r="0.75" fill="#FFFFFF"/>',
    "slash":  '<path d="M2.9 7.1 7.1 2.9" fill="none" stroke="#FFFFFF" stroke-width="1.3" stroke-linecap="round"/>',
    "dot":    '<circle cx="5" cy="5" r="2" fill="#FFFFFF"/>',
    "arrows": '<path d="M2.6 5a2.4 2.4 0 0 1 4-1.7M7.4 5a2.4 2.4 0 0 1-4 1.7" fill="none" stroke="#FFFFFF" stroke-width="1.2" stroke-linecap="round"/>',
    "cloud":  '<path d="M3 6.6h4.1a1.35 1.35 0 0 0 .15-2.7 1.85 1.85 0 0 0-3.55-.5A1.4 1.4 0 0 0 3 6.6Z" fill="#FFFFFF"/>',
    "people": '<circle cx="4" cy="4" r="1.35" fill="#FFFFFF"/><path d="M1.7 7.9a2.4 2.4 0 0 1 4.6 0Z" fill="#FFFFFF"/><circle cx="7.2" cy="4.4" r="1.05" fill="#FFFFFF"/>',
    "lock":   '<rect x="3.1" y="4.6" width="3.8" height="3" rx="0.7" fill="#FFFFFF"/><path d="M4 4.6v-.8a1 1 0 0 1 2 0v.8" fill="none" stroke="#FFFFFF" stroke-width="0.9"/>',
}


def _badge_markup(badge: str, cx: float, cy: float, r: float, ring: float) -> str:
    """A filled badge circle with a 1 px cut-out ring, in 24-unit coordinates.

    The ring is drawn as a same-coloured stroke in the surrounding colour rather
    than as a composite operation, because an SVG has no CompositionMode_Clear.
    badged() does it properly, with QPainter, for the raster path."""
    fill, glyph = BADGES.get(badge, BADGES["info"])
    scale = (2 * r) / 10.0
    body = _BADGE_GLYPHS.get(glyph, _BADGE_GLYPHS["dot"])
    return (
        f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r + ring:.2f}" fill="#FFFFFF" '
        f'fill-opacity="0.92"/>'
        f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="{fill}"/>'
        f'<g transform="translate({cx - r:.2f} {cy - r:.2f}) scale({scale:.4f})">{body}</g>'
    )


def tray_svg(name: str, frame: int = 0) -> bytes:
    """Generated tray art for one installed name. WP-14 replaces the file; this
    keeps the tray from ever showing a null icon in the meantime."""
    if name in SPINNER_FRAMES:
        idx = SPINNER_FRAMES.index(name)
        return _spinner_svg(idx)
    fill, badge = _TRAY_ART.get(name, (CLOUD_WHITE, ""))
    stroke = "#3A3A3A" if fill == CLOUD_WHITE else "none"
    parts = [_SVG_OPEN]
    if name == "onedriveui-warning":
        parts.append('<path d="M12 3.4 22.2 20.6H1.8Z" fill="#FCE100" stroke="#9D5D00" stroke-width="1"/>')
        parts.append('<path d="M12 9.6v4.6" stroke="#3A2E00" stroke-width="1.8" stroke-linecap="round"/>')
        parts.append('<circle cx="12" cy="17.2" r="1.15" fill="#3A2E00"/>')
    elif name == "onedriveui-error":
        parts.append('<circle cx="12" cy="12" r="9" fill="#C42B1C"/>')
        parts.append('<path d="M8.6 8.6 15.4 15.4M15.4 8.6 8.6 15.4" stroke="#FFFFFF" '
                     'stroke-width="2" stroke-linecap="round"/>')
    elif name == "onedriveui-info":
        parts.append('<circle cx="12" cy="12" r="9" fill="#0078D4"/>')
        parts.append('<circle cx="12" cy="8.1" r="1.15" fill="#FFFFFF"/>')
        parts.append('<path d="M12 11.2v5.4" stroke="#FFFFFF" stroke-width="2" stroke-linecap="round"/>')
    elif name == "onedriveui-blocked":
        parts.append('<circle cx="12" cy="12" r="9" fill="none" stroke="#C42B1C" stroke-width="2.6"/>')
        parti = '<path d="M5.6 18.4 18.4 5.6" stroke="#C42B1C" stroke-width="2.6" stroke-linecap="round"/>'
        parts.append(parti)
    else:
        parts.append(f'<path d="{_CLOUD_D}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
        if name == "onedriveui-signedout":
            parts.append('<path d="M4.5 19.5 19.5 4.5" stroke="#5C5C5C" stroke-width="2" '
                         'stroke-linecap="round"/>')
        elif badge:
            parts.append(_badge_markup(badge, 17.4, 17.4, 5.0, 1.0))
    parts.append("</svg>")
    return "".join(parts).encode("utf-8")


def _spinner_svg(index: int) -> bytes:
    """Frame `index` of the 8-frame rotation: a cloud plus an arc rotated 45 deg
    per frame. SNI has no animation, so ui/tray.py swaps the NAMES on a timer."""
    angle = (360.0 / len(SPINNER_FRAMES)) * index
    return (
        _SVG_OPEN
        + f'<path d="{_CLOUD_D}" fill="{CLOUD_WHITE}" stroke="#3A3A3A" stroke-width="1"/>'
        + f'<g transform="rotate({angle:.1f} 17.4 17.4)">'
        + '<circle cx="17.4" cy="17.4" r="5" fill="#0078D4"/>'
        + '<path d="M17.4 13.9a3.5 3.5 0 1 1-3.5 3.5" fill="none" stroke="#FFFFFF" '
          'stroke-width="1.4" stroke-linecap="round"/>'
        + '<path d="M16.1 13.4h1.6v1.6" fill="none" stroke="#FFFFFF" stroke-width="1.4" '
          'stroke-linecap="round" stroke-linejoin="round"/>'
        + "</g></svg>"
    ).encode("utf-8")


def emblem_svg(stem: str) -> bytes:
    """Generated emblem art. Drawn edge-to-edge: Nautilus composites emblems at
    roughly a quarter of the file icon, so there is no room for padding."""
    fill, glyph = _EMBLEM_ART.get(stem, ("#0078D4", "dot"))
    body = _BADGE_GLYPHS.get(glyph, _BADGE_GLYPHS["dot"])
    return (
        _SVG_OPEN
        + f'<circle cx="12" cy="12" r="11" fill="{fill}" stroke="#FFFFFF" stroke-width="1.6"/>'
        + f'<g transform="translate(3.6 3.6) scale(1.68)">{body}</g>'
        + "</svg>"
    ).encode("utf-8")


def logo_svg() -> bytes:
    """The flat 2019 four-shape OneDrive mark. viewBox '0 5.5 32 20.5' — WIDER
    THAN TALL. The 2025 refresh uses seven radial gradients over a 648x431
    viewBox and turns to mud at 16 px; always use the flat mark at <= 32 px."""
    x, y, w, h = LOGO_VIEWBOX
    c = LOGO_COLORS
    # Four FLAT shapes, back to front, whose union is the cloud silhouette. No
    # gradient: the 2019 mark has none, and a gradient turns to mud at 16 px.
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" '
        f'width="{w}" height="{h}" fill="none">'
        f'<ellipse cx="17.5" cy="13.2" rx="8.2" ry="7.2" fill="{c["rear_top"]}"/>'
        f'<ellipse cx="9.6" cy="17.6" rx="6.6" ry="5.8" fill="{c["left"]}"/>'
        f'<ellipse cx="25.4" cy="17.6" rx="6.6" ry="5.8" fill="{c["right"]}"/>'
        f'<path d="M6.2 19.2h19.6a3.4 3.4 0 0 1 0 6.8H6.2a3.4 3.4 0 0 1 0-6.8Z" '
        f'fill="{c["front"]}"/>'
        f"</svg>"
    ).encode("utf-8")


# ═════════════════════════════════════════════════════════════════════════════
# Loading
# ═════════════════════════════════════════════════════════════════════════════

_ICON_CACHE: dict[tuple, QIcon] = {}
_SVG_CACHE: dict[tuple[str, str, int | None], bytes] = {}


def clear_cache() -> None:
    """Drop every cached QIcon. Called on a theme change: `color=None` resolves
    to the theme's primary text colour, so a stale cache paints the old theme."""
    _ICON_CACHE.clear()
    _SVG_CACHE.clear()


def svg_bytes(category: str, stem: str, size: int | None = None) -> bytes:
    """The SVG document for one asset: the installed file when WP-14 has shipped
    it, else generated art. Never raises, never returns empty."""
    key = (category, stem, size)
    data = _SVG_CACHE.get(key)
    if data is not None:
        return data
    path = asset_path(category, stem, size)
    if path is not None:
        try:
            data = path.read_bytes()
        except OSError:
            data = None
    if not data:
        if category == "status":
            data = tray_svg(stem)
        elif category == "emblems":
            base_stem = stem[len(EMBLEM_FILE_PREFIX):] if stem.startswith(EMBLEM_FILE_PREFIX) else stem
            data = emblem_svg(base_stem)
        elif category == "apps":
            data = logo_svg()
        else:
            data = placeholder_svg(stem)
    _SVG_CACHE[key] = data
    return data


def render_svg(data: bytes, px: int, dpr: float = 1.0) -> QPixmap:
    """-> QPixmap. Allocates round(px*dpr) device pixels, sets the device pixel
    ratio, and renders into the LOGICAL `px` box — see `_render`."""
    return _render(data, px, px, dpr)


def render_svg_rect(data: bytes, w: int, h: int, dpr: float = 1.0) -> QPixmap:
    """render_svg() for art that is not square — the OneDrive mark is wider than
    tall and must never be stretched to square."""
    return _render(data, w, h, dpr)


def _render(data: bytes, w: int, h: int, dpr: float) -> QPixmap:
    """The SVG at `w` x `h` LOGICAL pixels, backed by `dpr` times as many.

    The target rectangle is the logical box, not the device one. Once
    `setDevicePixelRatio()` has been called, a QPainter over the pixmap works in
    logical coordinates — so rendering into `dev_w x dev_h` draws the art at
    `dpr` times its size and the pixmap keeps the top-left corner of it. At
    dpr 2 that is one quarter of an emblem — what `StatusBadge` painted on any
    screen with a device pixel ratio above 1, since it is the one caller that
    passes the device's own ratio through.
    """
    dev_w = max(1, int(round(w * dpr)))
    dev_h = max(1, int(round(h * dpr)))
    pm = QPixmap(dev_w, dev_h)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(data))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, dev_w / dpr, dev_h / dpr))
    painter.end()
    return pm


def _render_image(data: bytes, w: int, h: int) -> QImage:
    """The QImage twin of _render(), for writing PNG fallbacks without a
    QGuiApplication (install_theme_icons may run from the CLI installer)."""
    img = QImage(max(1, w), max(1, h), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(data))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, img.width(), img.height()))
    painter.end()
    return img


def _recolour(pm: QPixmap, color: str) -> QPixmap:
    """Recolour a monochrome pixmap with CompositionMode_SourceIn, preserving
    the alpha channel — the contract's recolouring primitive."""
    out = QPixmap(pm.size())
    out.setDevicePixelRatio(pm.devicePixelRatio())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.drawPixmap(0, 0, pm)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(out.rect(), QColor(color))
    painter.end()
    return out


def glyph_stem(key: str) -> str:
    """-> the Fluent asset stem for a GLYPHS key. Raises KeyError on a typo."""
    try:
        return GLYPHS[key]
    except KeyError:
        raise KeyError(
            f"unknown icon key {key!r}; the frozen set is icons.GLYPHS"
        ) from None


def icon(key: str, size: int = 16, color: str | None = None) -> QIcon:
    """-> QIcon for a GLYPHS key at a NATIVE size. `color` recolours a monochrome
    SVG via QPainter.CompositionMode_SourceIn. Raises KeyError on an unknown key
    and ValueError on a non-native size."""
    stem = glyph_stem(key)
    if size not in GLYPH_SIZES:
        raise ValueError(
            f"icons.icon: {size} is not a native size; never scale a 24 px glyph "
            f"to 16 px. Use one of {GLYPH_SIZES}."
        )
    tint = color or theme.T("TextFillColorPrimary")
    cache_key = ("glyph", key, size, tint)
    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached
    data = svg_bytes("glyphs", stem, size)
    out = QIcon()
    # Two raw sizes so a 2x screen gets a crisp pixmap. NEVER setDevicePixelRatio
    # on a pixmap handed to addPixmap — QIcon indexes by RAW pixel size.
    for scale in (1, 2):
        pm = _render(data, size * scale, size * scale, 1.0)
        out.addPixmap(_recolour(pm, tint), QIcon.Mode.Normal, QIcon.State.Off)
    _ICON_CACHE[cache_key] = out
    return out


def glyph_icon(key: str, color: str | None = None) -> QIcon:
    """-> one QIcon carrying every native GLYPH_SIZES pixmap, for a call site
    that cannot know which size Qt will ask for (a QAction, a delegate)."""
    tint = color or theme.T("TextFillColorPrimary")
    cache_key = ("glyph_all", key, tint)
    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached
    stem = glyph_stem(key)
    out = QIcon()
    for size in GLYPH_SIZES:
        pm = _render(svg_bytes("glyphs", stem, size), size, size, 1.0)
        out.addPixmap(_recolour(pm, tint), QIcon.Mode.Normal, QIcon.State.Off)
    _ICON_CACHE[cache_key] = out
    return out


def _category_for(name: str) -> str:
    if name in TRAY_ICON_NAMES or name in SPINNER_FRAMES:
        return "status"
    if name.startswith(EMBLEM_FILE_PREFIX):
        return "emblems"
    if name == APP_ICON_NAME:
        return "apps"
    return "glyphs"


def named_icon(name: str, sizes: tuple[int, ...] = TRAY_PIXMAP_SIZES) -> QIcon:
    """-> a multi-size QIcon built from the art for an INSTALLED name (a tray
    state, a spinner frame, emblem-<stem>, or the app icon). Used as the
    fallback for QIcon.fromTheme() before install_theme_icons() has run."""
    cache_key = ("named", name, sizes)
    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached
    data = svg_bytes(_category_for(name), name)
    out = QIcon()
    for size in sizes:
        out.addPixmap(_render(data, size, size, 1.0), QIcon.Mode.Normal, QIcon.State.Off)
    _ICON_CACHE[cache_key] = out
    return out


def any_icon(name: str, size: int = 16, color: str | None = None) -> QIcon:
    """-> a QIcon for ANY entry in ICON_NAMES, at any size. The permissive
    sibling of icon(): generic code (a settings row, a test) can resolve a name
    without knowing whether it is a themed icon or a native-size glyph."""
    if name in GLYPHS:
        native = min(GLYPH_SIZES, key=lambda s: (abs(s - size), s))
        return icon(name, native, color)
    if name in THEME_ICON_NAMES:
        return named_icon(name)
    raise KeyError(f"unknown icon name {name!r}; the frozen set is icons.ICON_NAMES")


def tray_icon_name(tray: TrayIcon, frame: int = 0) -> str:
    """-> the themed icon NAME for a tray state. "" for TrayIcon.NONE, which
    means: register no StatusNotifierItem at all."""
    if tray is TrayIcon.NONE or not tray.value:
        return ""
    if tray is TrayIcon.SYNCING:
        return SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
    return str(tray.value)


def tray_icon(tray: TrayIcon, frame: int = 0) -> QIcon:
    """-> QIcon.fromTheme(name). NEVER a raw pixmap: SNI transmits an IconName.
    `frame` selects a SPINNER_FRAMES entry when tray is SYNCING."""
    name = tray_icon_name(tray, frame)
    if not name:
        return QIcon()
    # The fallback matters only before install_theme_icons() has run, or under an
    # icon theme that has not been re-cached yet; QIcon.fromTheme() keeps the
    # NAME when the file is installed, which is what SNI actually transmits.
    return QIcon.fromTheme(name, named_icon(name))


def tray_icon_for_state(state: SyncState, frame: int = 0) -> QIcon:
    """-> the tray QIcon for a SyncState, through the single TRAY_FOR_STATE map."""
    return tray_icon(TRAY_FOR_STATE[state], frame)


def emblem_name(state: FileState) -> str:
    """-> the bare stem for Nautilus.FileInfo.add_emblem(). "" means no emblem."""
    return EMBLEM_FOR_STATE.get(state, "")


def emblem_icon_name(stem: str) -> str:
    """-> the installed file stem, `emblem-<stem>`. Nautilus resolves
    emblem-NAME -> NAME -> emblem-NAME-symbolic -> NAME-symbolic, so add_emblem()
    is given the BARE stem while the FILE carries this prefix."""
    return stem if stem.startswith(EMBLEM_FILE_PREFIX) else EMBLEM_FILE_PREFIX + stem


def emblem_icon(stem: str) -> QIcon:
    """-> QIcon for an emblem, for our own file browser (Nautilus uses the name)."""
    return named_icon(emblem_icon_name(stem))


def logo(px: int) -> QIcon:
    """-> QIcon of the flat 2019 four-shape OneDrive mark. The 2025 refresh uses
    seven radial gradients over a 648x431 viewBox and turns to mud at 16 px —
    always use the flat mark at <= 32 px. The mark is WIDER THAN TALL
    (viewBox '0 5.5 32 20.5') and must not be stretched to square."""
    cache_key = ("logo", px)
    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached
    _, _, vw, vh = LOGO_VIEWBOX
    data = svg_bytes("apps", APP_ICON_NAME)
    out = QIcon()
    for scale in (1, 2):
        w = max(1, int(round(px * scale)))
        h = max(1, int(round(px * scale * vh / vw)))
        out.addPixmap(render_svg_rect(data, w, h, 1.0), QIcon.Mode.Normal, QIcon.State.Off)
    _ICON_CACHE[cache_key] = out
    return out


def app_icon(px: int = 48) -> QIcon:
    """-> the window / .desktop icon. Square, unlike logo(): a window icon that
    is not square is letterboxed by the compositor."""
    sizes = tuple(sorted({px, px * 2, *TRAY_PIXMAP_SIZES}))
    return named_icon(APP_ICON_NAME, sizes)


def badged(base_name: str, badge: str, px: int, *, corner: str = "br") -> QPixmap:
    """-> QPixmap: a base icon with a 10x10 status badge in the bottom-right
    (bottom-LEFT for file overlays), separated by a 1 px cut-out ring painted
    with CompositionMode_Clear so the badge reads at 16 px.

    Do NOT setDevicePixelRatio on a pixmap passed to QIcon.addPixmap — QIcon
    indexes by RAW pixel size. DO set it on pixmaps drawn via QPainter.drawPixmap."""
    if badge and badge not in BADGES:
        raise KeyError(f"unknown badge {badge!r}; the frozen set is {tuple(BADGES)}")
    if corner not in ("br", "bl"):
        raise ValueError(f"icons.badged: corner must be 'br' or 'bl', not {corner!r}")

    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # Shrink the base so the badge has somewhere to sit.
    base_px = int(round(px * (0.82 if badge else 1.0)))
    base_pm = _render(svg_bytes(_category_for(base_name), base_name), base_px, base_px, 1.0)
    base_pm.setDevicePixelRatio(1.0)
    painter.drawPixmap(0, 0, base_pm)

    if badge:
        # METRICS["tray_badge"] is 10 px on the 24 px design grid.
        d = px * (METRICS_TRAY_BADGE / 24.0)
        d = max(6.0, d)
        ring = max(1.0, px * (METRICS_TRAY_BADGE_RING / 16.0))
        x = (px - d) if corner == "br" else 0.0
        rect = QRectF(x, px - d, d, d)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.drawEllipse(rect.adjusted(-ring, -ring, ring if corner == "bl" else 0.0, 0.0))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        fill, glyph = BADGES[badge]
        painter.setBrush(QColor(fill))
        painter.drawEllipse(rect)
        body = _BADGE_GLYPHS.get(glyph, _BADGE_GLYPHS["dot"])
        svg = _BADGE_SVG_OPEN + body + "</svg>"
        glyph_pm = _render(svg.encode("utf-8"), max(1, int(round(d))), max(1, int(round(d))), 1.0)
        glyph_pm.setDevicePixelRatio(1.0)
        painter.drawPixmap(int(round(rect.x())), int(round(rect.y())), glyph_pm)

    painter.end()
    return pm


def badged_icon(base_name: str, badge: str,
                sizes: tuple[int, ...] = TRAY_PIXMAP_SIZES) -> QIcon:
    """-> a multi-size QIcon of badged() output, for the tray fallback path."""
    out = QIcon()
    for size in sizes:
        out.addPixmap(badged(base_name, badge, size), QIcon.Mode.Normal, QIcon.State.Off)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Installation into the icon theme
# ═════════════════════════════════════════════════════════════════════════════

_INDEX_THEME_HEADER = """[Icon Theme]
Name=Hicolor
Comment=Fallback icon theme
Hidden=true
"""


def _write_index_theme(base: Path, dirs: list[str]) -> None:
    """gtk4-update-icon-cache refuses a directory with no index.theme. An
    existing one (the system hicolor, or another app's) is never overwritten."""
    index = base / "index.theme"
    if index.exists():
        return
    lines = [_INDEX_THEME_HEADER, "Directories=" + ",".join(dirs), ""]
    for d in dirs:
        size = 48 if d.startswith("scalable") else int(d.split("/", 1)[0].split("x", 1)[0])
        context = d.rsplit("/", 1)[1].capitalize()
        lines.append(f"[{d}]")
        lines.append(f"Size={size}")
        if d.startswith("scalable"):
            lines.append("MinSize=8")
            lines.append("MaxSize=512")
            lines.append("Type=Scalable")
        else:
            lines.append("Type=Fixed")
        lines.append(f"Context={context}")
        lines.append("")
    try:
        index.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass


def install_theme_icons() -> None:
    """Write every tray, emblem and app SVG into ~/.local/share/icons/hicolor/
    and run `gtk4-update-icon-cache -f -t`. Without this, Nautilus emblems
    silently do not appear."""
    base = icon_theme_dir()
    status_dir = icon_status_dir()
    emblem_dir = icon_emblem_dir()
    app_dir = icon_app_dir()

    jobs: list[tuple[Path, str, str, bytes]] = []
    for name in TRAY_ICON_NAMES + SPINNER_FRAMES:
        jobs.append((status_dir, "status", name, svg_bytes("status", name)))
    for stem in EMBLEM_STEMS:
        fname = emblem_icon_name(stem)
        jobs.append((emblem_dir, "emblems", fname, svg_bytes("emblems", fname)))
    jobs.append((app_dir, "apps", APP_ICON_NAME, svg_bytes("apps", APP_ICON_NAME)))

    for directory, _category, name, data in jobs:
        try:
            (directory / f"{name}.svg").write_bytes(data)
        except OSError:
            continue

    # PNG fallbacks. GNOME's appindicator asks in the 22-24 px band and some
    # hosts never look at the scalable directory at all. Best-effort: a headless
    # installer without a QGuiApplication can still write the SVGs above.
    png_dirs: set[str] = set()
    for size in INSTALL_PNG_SIZES:
        for directory, category, name, data in jobs:
            out_dir = base / f"{size}x{size}" / category
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                if category == "apps":
                    _, _, vw, vh = LOGO_VIEWBOX
                    img = _render_image(data, size, max(1, int(round(size * vh / vw))))
                else:
                    img = _render_image(data, size, size)
                if img.save(str(out_dir / f"{name}.png"), "PNG"):
                    png_dirs.add(f"{size}x{size}/{category}")
            except Exception:
                continue

    dirs = ["scalable/status", "scalable/emblems", "scalable/apps"] + sorted(png_dirs)
    _write_index_theme(base, dirs)

    for tool in ("gtk4-update-icon-cache", "gtk-update-icon-cache"):
        try:
            result = subprocess.run([tool, "-f", "-t", str(base)],
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            break


def installed_icon_files() -> dict[str, Path]:
    """-> {installed name: expected path}. The installer test asserts against
    this rather than re-deriving the layout."""
    out: dict[str, Path] = {}
    for name in TRAY_ICON_NAMES + SPINNER_FRAMES:
        out[name] = icon_status_dir() / f"{name}.svg"
    for stem in EMBLEM_STEMS:
        fname = emblem_icon_name(stem)
        out[fname] = icon_emblem_dir() / f"{fname}.svg"
    out[APP_ICON_NAME] = icon_app_dir() / f"{APP_ICON_NAME}.svg"
    return out


# A theme change re-tints every `color=None` glyph, so the cache must go with it.
BUS.theme_changed.connect(lambda _dark, _accent: clear_cache())


#: ARCHITECTURE §8 names these; CONTRACTS §8 names the tables. One set, both names.
TRAY = TRAY_FOR_STATE
EMBLEM = EMBLEM_FOR_STATE

__all__ = [
    "TRAY_ICON_NAMES", "SPINNER_FRAMES", "SPINNER_PERIOD_MS", "TRAY_FOR_STATE",
    "TRAY", "TRAY_PIXMAP_SIZES", "INSTALL_PNG_SIZES",
    "EMBLEM_STEMS", "EMBLEM_FOR_STATE", "EMBLEM", "EMBLEM_FILE_PREFIX",
    "GLYPHS", "GLYPH_SIZES", "GLYPH_FOR_FILE_STATE", "APP_ICON_NAME",
    "THEME_ICON_NAMES", "ICON_NAMES", "BADGES",
    "LOGO_COLORS", "LOGO_VIEWBOX",
    "asset_root", "asset_path", "svg_bytes", "placeholder_svg",
    "tray_svg", "emblem_svg", "logo_svg",
    "render_svg", "render_svg_rect", "icon", "glyph_icon", "glyph_stem",
    "named_icon", "any_icon", "tray_icon", "tray_icon_name",
    "tray_icon_for_state", "emblem_name", "emblem_icon_name", "emblem_icon",
    "logo", "app_icon", "badged", "badged_icon",
    "install_theme_icons", "installed_icon_files", "clear_cache",
]
