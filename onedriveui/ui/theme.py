"""FROZEN CONTRACT. The complete Fluent design token set.

Qt gotchas baked into this module:
  * QColor("#AARRGGBB") is valid; QColor("#RRGGBBAA") is NOT and silently yields
    the wrong colour. Every literal here is opaque #RRGGBB or Qt-order #AARRGGBB.
  * Alpha does not compose predictably across separate QSS rules, which is why
    every fill token is pre-composited.
  * QSS silently ignores: box-shadow, transition, transform, opacity, filter,
    backdrop-filter, text-overflow, z-index, cursor, :not(), CSS variables,
    calc(), rem/em, and linear-gradient() (Qt's is qlineargradient).
  * `QPushButton { background: X }` with NO border declaration renders the Fusion
    GRADIENT, not a flat fill. Always declare a border.
  * A Python SUBCLASS of QWidget ignores QSS backgrounds without
    WA_StyledBackground. Derive containers from QFrame instead.
  * A bare `QWidget { ... }` selector cascades to EVERY descendant. Always scope.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Literal

from PySide6.QtCore import (
    QEasingCurve, QObject, QPointF, QTimer, Signal, Slot,
)

from onedriveui.bus import BUS
from onedriveui.models import ThemeMode

try:  # QtDBus carries the portal's SettingChanged signal onto the Qt event loop.
    from PySide6 import QtDBus as _QtDBus
    _QDBusVariant = _QtDBus.QDBusVariant
except Exception:  # pragma: no cover - QtDBus is part of the pacman PySide6 build
    _QtDBus = None
    _QDBusVariant = object

try:  # Gio, because QDBusArgument cannot demarshal the (ddd) accent struct.
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio as _Gio, GLib as _GLib

    _HAVE_GIO = True
except Exception:  # pragma: no cover
    _Gio = None
    _GLib = None
    _HAVE_GIO = False

Surface = Literal["base", "layer"]

# ═════════════════════════════════════════════════════════════════════════════
# 1. Base surfaces (opaque, no alpha) — the Mica substitutes
# ═════════════════════════════════════════════════════════════════════════════

BASE_LIGHT  = "#F3F3F3"   # SolidBackgroundFillColorBase   — window background
BASE_DARK   = "#202020"
LAYER_LIGHT = "#FFFFFF"   # SolidBackgroundFillColorQuarternary — flyout / card surface
LAYER_DARK  = "#2C2C2C"

# ═════════════════════════════════════════════════════════════════════════════
# 2. Tokens, pre-composited. Key -> (light_on_base, light_on_layer,
#                                    dark_on_base,  dark_on_layer)
# ═════════════════════════════════════════════════════════════════════════════

_COMPOSITED: dict[str, tuple[str, str, str, str]] = {
    # ── text ───────────────────────────────────────────────────────────────
    # NOTE: TextFillColorPrimary light is #E4000000 (89% black) and flattens to
    # #1A1A1A. Painting pure black text is the fastest way to look wrong.
    "TextFillColorPrimary":            ("#1A1A1A", "#1B1B1B", "#FFFFFF", "#FFFFFF"),
    "TextFillColorSecondary":          ("#5C5C5C", "#616161", "#CCCCCC", "#CFCFCF"),
    "TextFillColorTertiary":           ("#868686", "#8D8D8D", "#969696", "#9C9C9C"),
    "TextFillColorDisabled":           ("#9B9B9B", "#A3A3A3", "#717171", "#797979"),
    "TextFillColorInverse":            ("#FFFFFF", "#FFFFFF", "#1A1A1A", "#1B1B1B"),

    # ── control fills ──────────────────────────────────────────────────────
    "ControlFillColorDefault":         ("#FBFBFB", "#FFFFFF", "#2D2D2D", "#383838"),
    "ControlFillColorSecondary":       ("#F6F6F6", "#FCFCFC", "#323232", "#3D3D3D"),
    "ControlFillColorTertiary":        ("#F5F5F5", "#FDFDFD", "#272727", "#333333"),
    "ControlFillColorDisabled":        ("#F5F5F5", "#FDFDFD", "#2A2A2A", "#353535"),
    "ControlFillColorInputActive":     ("#FFFFFF", "#FFFFFF", "#1E1E1E", "#1E1E1E"),
    "ControlSolidFillColorDefault":    ("#FFFFFF", "#FFFFFF", "#454545", "#454545"),
    "ControlStrongFillColorDefault":   ("#868686", "#8D8D8D", "#9A9A9A", "#9F9F9F"),
    "ControlStrongFillColorDisabled":  ("#A6A6A6", "#AEAEAE", "#575757", "#606060"),

    # ── subtle / alt fills (hover and press states) ────────────────────────
    "SubtleFillColorSecondary":        ("#EAEAEA", "#F6F6F6", "#2D2D2D", "#383838"),
    "SubtleFillColorTertiary":         ("#EDEDED", "#F9F9F9", "#292929", "#343434"),
    "ControlAltFillColorSecondary":    ("#EDEDED", "#F9F9F9", "#1D1D1D", "#282828"),
    "ControlAltFillColorTertiary":     ("#E5E5E5", "#F0F0F0", "#2A2A2A", "#353535"),
    "ControlAltFillColorQuarternary":  ("#DCDCDC", "#E7E7E7", "#303030", "#3B3B3B"),

    # ── strokes ────────────────────────────────────────────────────────────
    "ControlStrokeColorDefault":       ("#E5E5E5", "#F0F0F0", "#303030", "#3B3B3B"),
    # The Fluent "1 px bottom stroke": light theme puts the DARKER secondary
    # stroke on the BOTTOM (the gradient carries ScaleY=-1); dark theme puts the
    # BRIGHTER one on the TOP. In Qt this is just border-bottom-color /
    # border-top-color.
    "ControlStrokeColorSecondary":     ("#CCCCCC", "#D6D6D6", "#353535", "#404040"),
    "CardStrokeColorDefault":          ("#E5E5E5", "#F0F0F0", "#1D1D1D", "#282828"),
    "DividerStrokeColorDefault":       ("#E5E5E5", "#F0F0F0", "#323232", "#3D3D3D"),
    "ControlStrongStrokeColorDefault": ("#868686", "#8D8D8D", "#9A9A9A", "#9F9F9F"),
    "SurfaceStrokeColorFlyout":        ("#E5E5E5", "#F0F0F0", "#1A1A1A", "#232323"),
    "SurfaceStrokeColorDefault":       ("#C1C1C1", "#C8C8C8", "#424242", "#494949"),

    # ── cards and layers ───────────────────────────────────────────────────
    "CardBackgroundFillColorDefault":  ("#FBFBFB", "#FFFFFF", "#2B2B2B", "#373737"),
    "CardBackgroundFillColorSecondary":("#F5F5F5", "#FAFAFA", "#272727", "#333333"),
    "LayerFillColorDefault":           ("#F9F9F9", "#FFFFFF", "#3A3A3A", "#3A3A3A"),
    "SmokeFillColorDefault":           ("#AAAAAA", "#B2B2B2", "#161616", "#1F1F1F"),

    # ── solid backgrounds ──────────────────────────────────────────────────
    "SolidBackgroundFillColorBase":    ("#F3F3F3", "#F3F3F3", "#202020", "#202020"),
    "SolidBackgroundFillColorBaseAlt": ("#DADADA", "#DADADA", "#0A0A0A", "#0A0A0A"),
    "SolidBackgroundFillColorSecondary":("#EEEEEE", "#EEEEEE", "#1C1C1C", "#1C1C1C"),
    "SolidBackgroundFillColorTertiary":("#F9F9F9", "#F9F9F9", "#282828", "#282828"),
    "SolidBackgroundFillColorQuarternary":("#FFFFFF", "#FFFFFF", "#2C2C2C", "#2C2C2C"),

    # ── focus ring (two-tone, NO accent) ───────────────────────────────────
    "FocusStrokeColorOuter":           ("#1A1A1A", "#1B1B1B", "#FFFFFF", "#FFFFFF"),
    "FocusStrokeColorInner":           ("#FFFFFF", "#FFFFFF", "#000000", "#000000"),

    # ── status (already opaque in the source) ──────────────────────────────
    "SystemFillColorSuccess":          ("#0F7B0F", "#0F7B0F", "#6CCB5F", "#6CCB5F"),
    "SystemFillColorSuccessBackground":("#DFF6DD", "#DFF6DD", "#393D1B", "#393D1B"),
    "SystemFillColorCaution":          ("#9D5D00", "#9D5D00", "#FCE100", "#FCE100"),
    "SystemFillColorCautionBackground":("#FFF4CE", "#FFF4CE", "#433519", "#433519"),
    # NOTE: SystemErrorTextColor (#C50500/#FFF000) in generic.xaml is a legacy
    # Windows 8 token. The CURRENT error colour is SystemFillColorCritical.
    "SystemFillColorCritical":         ("#C42B1C", "#C42B1C", "#FF99A4", "#FF99A4"),
    "SystemFillColorCriticalBackground":("#FDE7E9", "#FDE7E9", "#442726", "#442726"),
    "SystemFillColorNeutral":          ("#868686", "#8D8D8D", "#8B8B8B", "#909090"),
    "SystemFillColorSolidNeutral":     ("#8A8A8A", "#8A8A8A", "#9D9D9D", "#9D9D9D"),
    "SystemFillColorSolidAttentionBackground":("#F7F7F7", "#F7F7F7", "#2E2E2E", "#2E2E2E"),
}

#: Every token name, frozen — the parametrised token test iterates this.
TOKENS: tuple[str, ...] = tuple(_COMPOSITED)

# ═════════════════════════════════════════════════════════════════════════════
# 3. Accent
# ═════════════════════════════════════════════════════════════════════════════

#: Windows 11's default system accent ramp, verified.
ACCENT_RAMP_SYSTEM: dict[str, str] = {
    "Light3": "#99EBFF", "Light2": "#4CC2FF", "Light1": "#0091F8",
    "Base":   "#0078D4",
    "Dark1":  "#0067C0", "Dark2":  "#003E92", "Dark3":  "#001A68",
}

#: The OneDrive brand blue (#0364B8) expanded into an equivalent 7-stop ramp by
#: measuring the per-stop HSL delta of the system ramp and reapplying it. The
#: transform round-trips the system ramp exactly, which validates it.
ACCENT_RAMP_ONEDRIVE: dict[str, str] = {
    "Light3": "#82E1FD", "Light2": "#36B2FC", "Light1": "#047BDB",
    "Base":   "#0364B8",
    "Dark1":  "#0355A4", "Dark2":  "#023077", "Dark3":  "#01124E",
}

#: WinUI picks a DIFFERENT ramp stop per theme:
#:   AccentFillColorDefaultBrush = SystemAccentColorDark1  in LIGHT
#:                               = SystemAccentColorLight2 in DARK
#: Using the base #0364B8 in both themes is wrong in BOTH.
#: Hover = the same colour at 90 % opacity; pressed = 80 %. Pre-composited here.
ACCENT_ONEDRIVE = {
    "light": {"rest": "#0355A4", "hover": "#1B65AC", "pressed": "#3375B4",
              "disabled": "#BFBFBF", "text": "#FFFFFF"},
    # Text on accent is BLACK in dark theme, because the dark accent is a light
    # blue. Hardcoding white text on accent buttons breaks dark-mode contrast.
    "dark":  {"rest": "#36B2FC", "hover": "#34A3E6", "pressed": "#3295D0",
              "disabled": "#434343", "text": "#000000"},
}

ACCENT_ROLES: tuple[str, ...] = ("rest", "hover", "pressed", "disabled", "text")

#: Hover and pressed are the rest colour at these opacities over the surface.
ACCENT_HOVER_ALPHA = 0.9
ACCENT_PRESSED_ALPHA = 0.8

#: GNOME's nine accents, VERIFIED by setting each and reading the portal. Kept
#: so the "Use system accent colour" setting can name the colour the user chose.
GNOME_ACCENTS: dict[str, str] = {
    "blue": "#3584E4", "teal": "#2190A4", "green": "#3A944A", "yellow": "#C88800",
    "orange": "#ED5B00", "red": "#E62D42", "pink": "#D56199", "purple": "#9141AC",
    "slate": "#6F8396",
}

#: The OneDrive logo is a FOUR-FLAT-SHAPE construction, not a gradient. viewBox
#: "0 5.5 32 20.5" — it is WIDER THAN TALL and must never be stretched to square.
LOGO_COLORS = {"rear_top": "#0364B8", "left": "#0078D4",
               "right": "#1490DF", "front": "#28A8EA"}
LOGO_VIEWBOX = (0.0, 5.5, 32.0, 20.5)

# ═════════════════════════════════════════════════════════════════════════════
# 4. Geometry
# ═════════════════════════════════════════════════════════════════════════════

RADII = {
    "control": 4,        # ControlCornerRadius
    "overlay": 8,        # OverlayCornerRadius — flyouts, dialogs, menus
    "toggle_track": 10,  # the 20 px-tall pill
    "progress_fill": 1.5,
    "progress_track": 0.5,
    "hover_pill": 4,
    "selection_indicator": 2,
}

SPACING = {"xxs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 20, "xxl": 24, "xxxl": 32}

METRICS = {
    # Button: ButtonPadding 11,5,11,6 + 1 px border. `padding: 5px 11px` with
    # `min-height: 20px` measures EXACTLY QSize(55, 32). Omitting min-height
    # gives 33 px.
    "button_h": 32, "button_pad_h": 11, "button_pad_v": 5, "button_min_h": 20,
    # TextBox: min height 32, padding 10,5,6,6, border 1 -> focused 1,1,1,2.
    # The focused bottom border grows 1->2 px, so padding-bottom must drop by 1
    # or the control jumps 32 -> 33 px on focus.
    "textbox_h": 32, "textbox_pad_l": 10, "textbox_pad_b": 6, "textbox_pad_b_focus": 5,
    # Windows 11 ToggleSwitch — the WINUI2 template, NOT the legacy Windows 10
    # 44x20/10 px one that ships in microsoft-ui-xaml@main.
    "toggle_track_w": 40, "toggle_track_h": 20,
    "toggle_knob": 12, "toggle_knob_box": 20, "toggle_travel": 20,
    "toggle_knob_hover": 14, "toggle_knob_press_w": 17, "toggle_knob_press_h": 14,
    # ProgressBar: the TRACK (1 px) is THINNER than the FILL (3 px). Intentional.
    "progress_fill_h": 3, "progress_track_h": 1,
    "ring_stroke": 4,
    # SettingsCard / SettingsExpander (CommunityToolkit, verbatim)
    "card_min_h": 68, "card_pad": 16, "card_icon": 20, "card_icon_gap": 20,
    "card_desc_size": 12, "card_content_min_w": 120, "card_wrap_threshold": 476,
    "expander_header_pad": (16, 16, 4, 16), "expander_child_pad": (58, 8, 44, 8),
    "expander_chevron": 32,
    # NavigationView
    "nav_open_w": 320, "nav_compact_w": 48, "nav_item_h": 36,
    "nav_item_margin": (4, 2), "nav_icon_box": 40, "nav_glyph": 16,
    "nav_indicator_w": 3, "nav_indicator_h": 16, "nav_toggle": (40, 36),
    # Flyout (FlyoutContentPadding 16,15,16,17)
    "flyout_pad": (16, 15, 16, 17), "flyout_min_w": 96, "flyout_max_w": 456,
    # Focus ring: 2 px outer + 1 px inner, inflated 3 px outside the control,
    # ring radius = control radius + 3. It carries NO accent colour.
    "focus_outer": 2, "focus_inner": 1, "focus_inflate": 3,
    # Activity Center
    "ac_width": 360, "ac_header_h": 64, "ac_storage_h": 56, "ac_footer_h": 48,
    "ac_row_h_2line": 56, "ac_row_h_1line": 48, "ac_inset": 16,
    "ac_bar_w": 328, "ac_bar_h": 4,
    # Badges
    "tray_badge": 10, "tray_badge_ring": 1,
}

# ═════════════════════════════════════════════════════════════════════════════
# 5. Typography — Windows 11 ramp, in PIXELS. Only 400 and 600 are ever used;
#    never Bold(700), never italic. Sentence case everywhere.
# ═════════════════════════════════════════════════════════════════════════════

TYPE: dict[str, tuple[int, int, int]] = {   # name -> (px, line_height, weight)
    "caption":           (12, 16, 400),
    "body":              (14, 20, 400),
    "body_strong":       (14, 20, 600),
    "body_large":        (18, 24, 400),
    "body_large_strong": (18, 24, 600),
    "subtitle":          (20, 28, 600),
    "title":             (28, 36, 600),
    "title_large":       (40, 52, 600),
    "display":           (68, 92, 600),
}

#: Segoe UI (Variable) is proprietary and must not be redistributed. Inter is
#: SIL OFL-1.1 and explicitly permits bundling. Selawik (Microsoft's own OFL
#: face) is metrically compatible with Segoe UI and is the first choice when it
#: is vendored.
#: WARNING: fontconfig SUBSTITUTES every unknown family, so
#: QFont.setFamilies([...]) does NOT walk to the first installed family —
#: ui/fonts.py must filter candidates against QFontDatabase.families() itself.
FONT_CANDIDATES: tuple[str, ...] = ("Selawik", "Inter", "Adwaita Sans", "Noto Sans", "Cantarell")

#: Noto Sans at 14 px measures lineSpacing 19.0, not the ramp's 20 — so line
#: heights must be set explicitly (QTextBlockFormat.setLineHeight(20, FixedHeight)
#: or a fixed widget height), regardless of which face resolves.
FALLBACK_LINE_HEIGHT_DELTA = 1

# ═════════════════════════════════════════════════════════════════════════════
# 6. Motion. Every duration passes through duration(), which returns 0 when
#    animations are disabled — and BOTH gtk-enable-animations and
#    org.gnome.desktop.interface enable-animations are FALSE on this machine.
#    Qt's animation timer is 60 Hz and does not sync to a 144/180 Hz display.
# ═════════════════════════════════════════════════════════════════════════════

DURATION = {
    "faster": 83,     # ControlFasterAnimationDuration — toggle knob
    "fast":   167,    # ControlFastAnimationDuration
    "normal": 250,    # ControlNormalAnimationDuration
    "slow":   350,
    "flyout": 150,    # Activity Center open: fade + 16 px rise
}

#: Fluent's standard curve is KeySpline (0,0,0,1). Reproduced exactly in Qt with
#: QEasingCurve(BezierSpline).addCubicBezierSegment(QPointF(0,0), QPointF(0,1),
#: QPointF(1,1)) — verified: valueForProgress(0.5) == 0.8899. OutCubic is close
#: but NOT identical; use the explicit bezier.
CURVES = {
    "decelerate":     ((0.0, 0.0), (0.0, 1.0)),      # curveDecelerateMid
    "easy_ease":      ((0.33, 0.0), (0.67, 1.0)),
    "accelerate":     ((0.8, 0.0), (0.78, 1.0)),     # curveAccelerateMin
    "point_to_point": ((0.55, 0.55), (0.0, 1.0)),
}

# ═════════════════════════════════════════════════════════════════════════════
# 7. Elevation. QSS has NO box-shadow. QGraphicsDropShadowEffect's blur radius
#    is roughly 2x the CSS blur (Qt's is the kernel diameter, CSS's is ~2 sigma),
#    it paints INSIDE the widget's own bounds (so a popup must reserve
#    blurRadius of layout margin on every side), and it is EXCLUSIVE — one
#    QGraphicsEffect per widget, so a shadow and an opacity effect cannot
#    coexist. Also add Qt.NoDropShadowWindowHint or the compositor adds a
#    second shadow.
# ═════════════════════════════════════════════════════════════════════════════

SHADOWS = {                       # (qt_blur_radius, dy, alpha_light, alpha_dark)
    "card":   (8,   2,  31, 61),
    "flyout": (32,  8,  36, 71),   # Fluent shadow16
    "dialog": (128, 32, 51, 122),  # Fluent shadow64
}


# ═════════════════════════════════════════════════════════════════════════════
# 7b. Object names. Every QSS rule below that is not a plain Qt class is scoped
#     by one of these. Widgets set them with setObjectName(theme.OBJ.X) so no
#     module ever writes a styling literal.
# ═════════════════════════════════════════════════════════════════════════════

class OBJ:
    ROOT              = "Root"                # the window's styled backdrop
    BODY              = "Body"                # a page's scroll content
    CARD              = "Card"                # SettingsCard / any layer surface
    CARD_SECONDARY    = "CardSecondary"
    FLYOUT            = "Flyout"              # Activity Center, popups
    DIALOG_SURFACE    = "DialogSurface"
    NAV_PANE          = "NavPane"
    NAV_LIST          = "NavList"
    HEADER            = "Header"
    FOOTER            = "Footer"
    DIVIDER           = "Divider"
    ACTIVITY_LIST     = "ActivityList"
    ACTIVITY_HEADER   = "ActivityHeader"
    STORAGE_BAR       = "StorageBar"
    BANNER_INFO       = "BannerInfo"
    BANNER_SUCCESS    = "BannerSuccess"
    BANNER_CAUTION    = "BannerCaution"
    BANNER_CRITICAL   = "BannerCritical"
    SEARCH_BOX        = "SearchBox"
    LINK_BUTTON       = "LinkButton"
    SUBTLE_BUTTON     = "SubtleButton"
    ICON_BUTTON       = "IconButton"
    CLOSE_BUTTON      = "CloseButton"
    WIZARD_PAGE       = "WizardPage"
    TOOLBAR           = "Toolbar"
    STATUS_STRIP      = "StatusStrip"


#: Dynamic properties the QSS keys off. Remember gotcha (d): after setProperty()
#: you MUST unpolish/polish the widget or the old rule sticks.
class PROP:
    ACCENT   = "accent"      # QPushButton[accent="true"]
    ROLE     = "role"        # QLabel[role="secondary"|"tertiary"|"disabled"|...]
    SEVERITY = "severity"    # QLabel[severity="critical"|"caution"|"success"]
    TYPE     = "type"        # QLabel[type="caption"|"body_strong"|"subtitle"|...]
    SELECTED = "selected"
    COMPACT  = "compact"


# ═════════════════════════════════════════════════════════════════════════════
# 8. Public API
# ═════════════════════════════════════════════════════════════════════════════

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_SURFACE_OFFSET: dict[str, int] = {"base": 0, "layer": 1}
_THEME_OFFSET: dict[bool, int] = {False: 0, True: 2}

# Fail at import rather than paint a wrong colour at runtime: every literal in
# the table must be an opaque #RRGGBB.
for _tok, _row in _COMPOSITED.items():
    if len(_row) != 4 or not all(_HEX_RE.match(v) for v in _row):
        raise ValueError(f"theme: token {_tok!r} is not four #RRGGBB literals: {_row!r}")
for _name, _ramp in (("system", ACCENT_RAMP_SYSTEM), ("onedrive", ACCENT_RAMP_ONEDRIVE)):
    if not all(_HEX_RE.match(v) for v in _ramp.values()):
        raise ValueError(f"theme: accent ramp {_name!r} holds a non-#RRGGBB literal")
del _tok, _row, _name, _ramp

#: ARCHITECTURE §8 names these; they are the on-base column of the table above,
#: kept as plain token -> #RRGGBB maps for call sites that never leave the
#: window background. Use T(..., on="layer") inside a card or flyout.
TOKENS_LIGHT: dict[str, str] = {k: v[0] for k, v in _COMPOSITED.items()}
TOKENS_LIGHT_LAYER: dict[str, str] = {k: v[1] for k, v in _COMPOSITED.items()}
TOKENS_DARK: dict[str, str] = {k: v[2] for k, v in _COMPOSITED.items()}
TOKENS_DARK_LAYER: dict[str, str] = {k: v[3] for k, v in _COMPOSITED.items()}

#: ARCHITECTURE §8 name for the accent table.
ACCENT = ACCENT_ONEDRIVE

# ─────────────────────────────────────────────────────────────────────────────
# Colour arithmetic (pure Python — no QColor, so it works before QApplication)
# ─────────────────────────────────────────────────────────────────────────────

def _rgb(hex_: str) -> tuple[int, int, int]:
    if not _HEX_RE.match(hex_):
        raise ValueError(f"theme: {hex_!r} is not an opaque #RRGGBB literal")
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(c))) for c in rgb)


def mix(fg: str, bg: str, alpha: float) -> str:
    """Composite `fg` at `alpha` (0..1) over the opaque `bg`. This is the exact
    transform that produced every pre-composited value in this module."""
    f, b = _rgb(fg), _rgb(bg)
    return _hex(tuple(round(f[i] * alpha + b[i] * (1.0 - alpha)) for i in range(3)))


# Proof the composite formula reproduces the shipped accent table. A change to
# `mix()` that breaks the accent ramp fails at import, not on screen.
for _theme, _surface in (("light", BASE_LIGHT), ("dark", BASE_DARK)):
    _rest = ACCENT_ONEDRIVE[_theme]["rest"]
    if mix(_rest, _surface, ACCENT_HOVER_ALPHA) != ACCENT_ONEDRIVE[_theme]["hover"]:
        raise ValueError(f"theme: accent hover for {_theme} does not round-trip")
    if mix(_rest, _surface, ACCENT_PRESSED_ALPHA) != ACCENT_ONEDRIVE[_theme]["pressed"]:
        raise ValueError(f"theme: accent pressed for {_theme} does not round-trip")
del _theme, _surface, _rest


# ─────────────────────────────────────────────────────────────────────────────
# Live theme state
# ─────────────────────────────────────────────────────────────────────────────

#: The live ThemeManager, set by ThemeManager.start(). T(dark=None) asks it.
_ACTIVE: "ThemeManager | None" = None

#: Cached portal answer for the no-manager case (tests, the Nautilus helper, any
#: code that resolves a token before app.py has built the manager).
_DETECTED_DARK: bool | None = None
_ANIMATIONS: bool | None = None

PORTAL_SVC = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IF = "org.freedesktop.portal.Settings"
PORTAL_NS = "org.freedesktop.appearance"


def _read_portal(key: str):
    """Read one org.freedesktop.appearance key. Gio first (QDBusArgument cannot
    demarshal the `(ddd)` accent struct), then the gdbus CLI, then None."""
    if _HAVE_GIO:
        try:
            proxy = _Gio.DBusProxy.new_for_bus_sync(
                _Gio.BusType.SESSION, _Gio.DBusProxyFlags.NONE, None,
                PORTAL_SVC, PORTAL_PATH, PORTAL_IF, None)
            res = proxy.call_sync("ReadOne", _GLib.Variant("(ss)", (PORTAL_NS, key)),
                                  _Gio.DBusCallFlags.NONE, 2000, None)
            return res.unpack()[0]
        except Exception:
            pass
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", PORTAL_SVC,
             "--object-path", PORTAL_PATH, "--method", PORTAL_IF + ".ReadOne",
             PORTAL_NS, key],
            capture_output=True, text=True, timeout=3).stdout.strip()
        body = out[out.find("<") + 1:out.rfind(">")]
        if body.startswith("("):
            return tuple(float(x) for x in body.strip("()").split(","))
        return int(body.split()[-1])
    except Exception:
        return None


def _qt_color_scheme_dark() -> bool | None:
    """QStyleHints.colorScheme(), used ONLY when the portal is unreachable.

    It is driven entirely by ~/.config/gtk-{3,4}.0/settings.ini and IGNORES
    org.freedesktop.appearance; on this machine a stale
    `gtk-application-prefer-dark-theme=true` makes it report Dark forever and
    colorSchemeChanged never fires. Last resort only."""
    try:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import Qt

        app = QGuiApplication.instance()
        if app is None:
            return None
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass
    return None


def _detect_dark() -> bool:
    """Portal -> QStyleHints -> light. Cached; ThemeManager.start() resets it."""
    global _DETECTED_DARK
    if _DETECTED_DARK is None:
        cs = _read_portal("color-scheme")   # 0 no preference, 1 dark, 2 light
        if cs is not None:
            try:
                _DETECTED_DARK = int(cs) == 1
            except (TypeError, ValueError):
                _DETECTED_DARK = None
        if _DETECTED_DARK is None:
            qt = _qt_color_scheme_dark()
            _DETECTED_DARK = bool(qt) if qt is not None else False
    return _DETECTED_DARK


def current_dark() -> bool:
    """The live theme: the running ThemeManager if there is one, else a cached
    portal read. Never raises."""
    if _ACTIVE is not None:
        return _ACTIVE.is_dark()
    return _detect_dark()


def invalidate_detection() -> None:
    """Drop the cached portal answer (tests monkeypatch the environment)."""
    global _DETECTED_DARK, _ANIMATIONS
    _DETECTED_DARK = None
    _ANIMATIONS = None
    _STYLESHEET_CACHE.clear()


def animations_enabled() -> bool:
    """False when the desktop has asked for no animation. BOTH
    gtk-enable-animations and org.gnome.desktop.interface enable-animations are
    FALSE on the target machine, so this is the normal answer here, not an edge
    case. Cached — it is read on every duration()."""
    global _ANIMATIONS
    if _ANIMATIONS is None:
        env = os.environ.get("ONEDRIVEUI_ANIMATIONS")
        if env is not None:
            _ANIMATIONS = env.strip().lower() not in ("0", "false", "no", "off")
            return _ANIMATIONS
        value = True
        try:
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"],
                capture_output=True, text=True, timeout=2).stdout.strip()
            if out:
                value = out.lower() != "false"
        except Exception:
            value = True
        if value:
            # The GTK ini is the second lever, and the one Qt itself reads.
            for ini in (os.path.expanduser("~/.config/gtk-4.0/settings.ini"),
                        os.path.expanduser("~/.config/gtk-3.0/settings.ini")):
                try:
                    with open(ini, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            key, _, val = line.partition("=")
                            if key.strip() == "gtk-enable-animations":
                                value = val.strip().lower() not in ("0", "false")
                                break
                except OSError:
                    continue
        _ANIMATIONS = value
    return _ANIMATIONS


# ─────────────────────────────────────────────────────────────────────────────
# Token resolution
# ─────────────────────────────────────────────────────────────────────────────

def T(token: str, *, dark: bool | None = None, on: Surface = "base") -> str:
    """Resolve a Fluent token to an opaque '#RRGGBB' for the current theme.

    `on` selects which surface the (originally translucent) token is composited
    over: "base" for the window background, "layer" for a card or flyout.
    `dark=None` means "ask the live ThemeManager".
    Raises KeyError for an unknown token — a typo must fail loudly, not paint black.
    """
    try:
        row = _COMPOSITED[token]
    except KeyError:
        raise KeyError(
            f"unknown Fluent token {token!r}; the frozen set is theme.TOKENS"
        ) from None
    try:
        offset = _SURFACE_OFFSET[on]
    except KeyError:
        raise ValueError(f"theme.T: `on` must be 'base' or 'layer', not {on!r}") from None
    is_dark = current_dark() if dark is None else bool(dark)
    return row[_THEME_OFFSET[is_dark] + offset]


def accent(role: str = "rest", *, dark: bool | None = None) -> str:
    """role in {"rest","hover","pressed","disabled","text"}."""
    is_dark = current_dark() if dark is None else bool(dark)
    table = ACCENT_ONEDRIVE["dark" if is_dark else "light"]
    if _ACTIVE is not None and _ACTIVE.use_system_accent:
        table = _system_accent_table(_ACTIVE.system_accent_hex(), is_dark)
    try:
        return table[role]
    except KeyError:
        raise KeyError(
            f"unknown accent role {role!r}; the frozen set is theme.ACCENT_ROLES"
        ) from None


def _system_accent_table(rest: str, is_dark: bool) -> dict[str, str]:
    """Build the five accent roles from an arbitrary system accent, using the
    same 90 %/80 % composite that produced the OneDrive table."""
    surface = BASE_DARK if is_dark else BASE_LIGHT
    r, g, b = _rgb(rest)
    # Relative luminance decides whether text on the accent is black or white.
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return {
        "rest": rest,
        "hover": mix(rest, surface, ACCENT_HOVER_ALPHA),
        "pressed": mix(rest, surface, ACCENT_PRESSED_ALPHA),
        "disabled": ACCENT_ONEDRIVE["dark" if is_dark else "light"]["disabled"],
        "text": "#000000" if lum > 0.55 else "#FFFFFF",
    }


def accent_ramp(*, system: bool = False) -> dict[str, str]:
    """The 7-stop ramp. OneDrive's by default; Windows' system ramp on request."""
    return dict(ACCENT_RAMP_SYSTEM if system else ACCENT_RAMP_ONEDRIVE)


def base(*, dark: bool | None = None) -> str:
    """The window background."""
    is_dark = current_dark() if dark is None else bool(dark)
    return BASE_DARK if is_dark else BASE_LIGHT


def layer(*, dark: bool | None = None) -> str:
    """The card / flyout surface."""
    is_dark = current_dark() if dark is None else bool(dark)
    return LAYER_DARK if is_dark else LAYER_LIGHT


def surface(on: Surface = "base", *, dark: bool | None = None) -> str:
    """The opaque colour of the named surface itself."""
    if on == "base":
        return base(dark=dark)
    if on == "layer":
        return layer(dark=dark)
    raise ValueError(f"theme.surface: `on` must be 'base' or 'layer', not {on!r}")


def shadow(name: str, *, dark: bool | None = None) -> tuple[int, int, int]:
    """-> (qt_blur_radius, dy, alpha) for QGraphicsDropShadowEffect."""
    try:
        blur, dy, a_light, a_dark = SHADOWS[name]
    except KeyError:
        raise KeyError(f"unknown shadow {name!r}; the frozen set is {tuple(SHADOWS)}") from None
    is_dark = current_dark() if dark is None else bool(dark)
    return blur, dy, (a_dark if is_dark else a_light)


# ─────────────────────────────────────────────────────────────────────────────
# Type ramp
# ─────────────────────────────────────────────────────────────────────────────

def _type_row(role: str) -> tuple[int, int, int]:
    try:
        return TYPE[role]
    except KeyError:
        raise KeyError(f"unknown type role {role!r}; the frozen set is {tuple(TYPE)}") from None


def font_px(role: str) -> int:
    return _type_row(role)[0]


def line_height(role: str) -> int:
    return _type_row(role)[1]


def weight(role: str) -> int:
    return _type_row(role)[2]


# ─────────────────────────────────────────────────────────────────────────────
# Motion
# ─────────────────────────────────────────────────────────────────────────────

def duration(name_or_ms: str | int) -> int:
    """Return the duration in ms, or 0 when animations are disabled."""
    if isinstance(name_or_ms, str):
        try:
            ms = DURATION[name_or_ms]
        except KeyError:
            raise KeyError(
                f"unknown duration {name_or_ms!r}; the frozen set is {tuple(DURATION)}"
            ) from None
    else:
        ms = int(name_or_ms)
    return ms if animations_enabled() else 0


def curve(name: str) -> QEasingCurve:
    """-> QEasingCurve built from CURVES[name] as an explicit BezierSpline."""
    try:
        (p1x, p1y), (p2x, p2y) = CURVES[name]
    except KeyError:
        raise KeyError(f"unknown curve {name!r}; the frozen set is {tuple(CURVES)}") from None
    c = QEasingCurve(QEasingCurve.Type.BezierSpline)
    c.addCubicBezierSegment(QPointF(p1x, p1y), QPointF(p2x, p2y), QPointF(1.0, 1.0))
    return c


# ─────────────────────────────────────────────────────────────────────────────
# The stylesheet
# ─────────────────────────────────────────────────────────────────────────────

_STYLESHEET_CACHE: dict[tuple[bool, str], str] = {}


def stylesheet(*, dark: bool | None = None) -> str:
    """The complete application QSS for the given theme. Built once per theme
    change; re-applying it is an expensive full re-polish, so ThemeManager
    debounces (the XDG portal emits color-scheme SettingChanged TWICE per change)."""
    is_dark = current_dark() if dark is None else bool(dark)
    key = (is_dark, accent("rest", dark=is_dark))
    sheet = _STYLESHEET_CACHE.get(key)
    if sheet is None:
        sheet = _build_stylesheet(is_dark)
        _STYLESHEET_CACHE[key] = sheet
    return sheet


def _build_stylesheet(dark: bool) -> str:
    def t(token: str, on: Surface = "base") -> str:
        return T(token, dark=dark, on=on)

    def a(role: str) -> str:
        return accent(role, dark=dark)

    r_ctl = RADII["control"]
    r_ovl = RADII["overlay"]
    bg = base(dark=dark)
    lay = layer(dark=dark)

    txt = t("TextFillColorPrimary")
    txt_l = t("TextFillColorPrimary", "layer")
    txt2 = t("TextFillColorSecondary")
    txt2_l = t("TextFillColorSecondary", "layer")
    txt3 = t("TextFillColorTertiary")
    txt_off = t("TextFillColorDisabled")

    fill = t("ControlFillColorDefault")
    fill2 = t("ControlFillColorSecondary")
    fill3 = t("ControlFillColorTertiary")
    fill_off = t("ControlFillColorDisabled")
    fill_input = t("ControlFillColorInputActive")
    fill_l = t("ControlFillColorDefault", "layer")
    fill2_l = t("ControlFillColorSecondary", "layer")
    fill3_l = t("ControlFillColorTertiary", "layer")

    stroke = t("ControlStrokeColorDefault")
    stroke2 = t("ControlStrokeColorSecondary")
    stroke_l = t("ControlStrokeColorDefault", "layer")
    stroke2_l = t("ControlStrokeColorSecondary", "layer")
    strong_stroke = t("ControlStrongStrokeColorDefault")
    card_stroke = t("CardStrokeColorDefault")
    divider = t("DividerStrokeColorDefault")
    flyout_stroke = t("SurfaceStrokeColorFlyout")

    card_bg = t("CardBackgroundFillColorDefault")
    card_bg2 = t("CardBackgroundFillColorSecondary")
    subtle2 = t("SubtleFillColorSecondary")
    subtle3 = t("SubtleFillColorTertiary")
    subtle2_l = t("SubtleFillColorSecondary", "layer")
    subtle3_l = t("SubtleFillColorTertiary", "layer")
    alt2 = t("ControlAltFillColorSecondary")
    alt3 = t("ControlAltFillColorTertiary")

    strong_fill = t("ControlStrongFillColorDefault")
    strong_off = t("ControlStrongFillColorDisabled")
    focus_outer = t("FocusStrokeColorOuter")

    ok = t("SystemFillColorSuccess")
    ok_bg = t("SystemFillColorSuccessBackground")
    caution = t("SystemFillColorCaution")
    caution_bg = t("SystemFillColorCautionBackground")
    crit = t("SystemFillColorCritical")
    crit_bg = t("SystemFillColorCriticalBackground")
    neutral = t("SystemFillColorNeutral")
    info_bg = t("SystemFillColorSolidAttentionBackground")

    body_px = font_px("body")
    caption_px = font_px("caption")

    # Every rule is scoped: never a bare `QWidget { background: … }`, which
    # cascades to every descendant (gotcha (c)).
    return f"""
/* ══ OneDriveUI — generated by ui/theme.py. Do not hand-edit. ══
   theme: {'dark' if dark else 'light'}   accent: {a('rest')} */

/* ── surfaces ─────────────────────────────────────────────────────────── */
QMainWindow, QDialog {{ background: {bg}; }}
#{OBJ.ROOT}, #{OBJ.BODY}, #{OBJ.WIZARD_PAGE} {{ background: {bg}; color: {txt}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QStackedWidget {{ background: transparent; }}
QSplitter::handle {{ background: {divider}; }}

#{OBJ.CARD} {{
  background: {card_bg}; border: 1px solid {card_stroke};
  border-radius: {r_ctl}px; min-height: {METRICS['card_min_h']}px;
}}
#{OBJ.CARD}:hover {{ background: {subtle2}; }}
#{OBJ.CARD_SECONDARY} {{
  background: {card_bg2}; border: 1px solid {card_stroke};
  border-radius: {r_ctl}px;
}}
#{OBJ.FLYOUT}, #{OBJ.DIALOG_SURFACE} {{
  background: {lay}; border: 1px solid {flyout_stroke}; border-radius: {r_ovl}px;
}}
#{OBJ.NAV_PANE} {{ background: {bg}; border: none; border-right: 1px solid {divider}; }}
#{OBJ.HEADER} {{ background: transparent; border: none; border-bottom: 1px solid {divider}; }}
#{OBJ.FOOTER} {{ background: transparent; border: none; border-top: 1px solid {divider}; }}
#{OBJ.DIVIDER} {{ background: {divider}; border: none; max-height: 1px; min-height: 1px; }}
#{OBJ.TOOLBAR}, #{OBJ.STATUS_STRIP} {{ background: transparent; border: none; }}

/* ── text ─────────────────────────────────────────────────────────────── */
QLabel {{ background: transparent; color: {txt}; font-size: {body_px}px; }}
QLabel[{PROP.ROLE}="secondary"] {{ color: {txt2}; }}
QLabel[{PROP.ROLE}="tertiary"]  {{ color: {txt3}; }}
QLabel[{PROP.ROLE}="disabled"]  {{ color: {txt_off}; }}
QLabel:disabled                 {{ color: {txt_off}; }}
QLabel[{PROP.TYPE}="caption"]   {{ font-size: {caption_px}px; }}
QLabel[{PROP.TYPE}="body_strong"] {{ font-size: {body_px}px; font-weight: {weight('body_strong')}; }}
QLabel[{PROP.TYPE}="body_large"]  {{ font-size: {font_px('body_large')}px; }}
QLabel[{PROP.TYPE}="body_large_strong"] {{ font-size: {font_px('body_large_strong')}px; font-weight: 600; }}
QLabel[{PROP.TYPE}="subtitle"]  {{ font-size: {font_px('subtitle')}px; font-weight: 600; }}
QLabel[{PROP.TYPE}="title"]     {{ font-size: {font_px('title')}px; font-weight: 600; }}
QLabel[{PROP.TYPE}="title_large"] {{ font-size: {font_px('title_large')}px; font-weight: 600; }}
QLabel[{PROP.SEVERITY}="success"]  {{ color: {ok}; }}
QLabel[{PROP.SEVERITY}="caution"]  {{ color: {caution}; }}
QLabel[{PROP.SEVERITY}="critical"] {{ color: {crit}; }}
QLabel[{PROP.SEVERITY}="neutral"]  {{ color: {neutral}; }}
#{OBJ.CARD} QLabel, #{OBJ.FLYOUT} QLabel, #{OBJ.DIALOG_SURFACE} QLabel {{ color: {txt_l}; }}
#{OBJ.CARD} QLabel[{PROP.ROLE}="secondary"],
#{OBJ.FLYOUT} QLabel[{PROP.ROLE}="secondary"],
#{OBJ.DIALOG_SURFACE} QLabel[{PROP.ROLE}="secondary"] {{ color: {txt2_l}; }}

/* ── buttons. EVERY rule declares a border or Fusion paints its gradient. ─ */
QPushButton {{
  background: {fill}; border: 1px solid {stroke};
  border-bottom-color: {stroke2}; border-radius: {r_ctl}px;
  padding: {METRICS['button_pad_v']}px {METRICS['button_pad_h']}px;
  min-height: {METRICS['button_min_h']}px; color: {txt}; font-size: {body_px}px;
}}
QPushButton:hover    {{ background: {fill2}; border: 1px solid {stroke}; border-bottom-color: {stroke2}; }}
QPushButton:pressed  {{ background: {fill3}; border: 1px solid {stroke}; color: {txt2}; }}
QPushButton:disabled {{ background: {fill_off}; border: 1px solid {stroke}; color: {txt_off}; }}
QPushButton:focus    {{ border: {METRICS['focus_outer']}px solid {focus_outer}; }}
QPushButton:default  {{ border: 1px solid {stroke}; border-bottom-color: {stroke2}; }}

QPushButton[{PROP.ACCENT}="true"] {{
  background: {a('rest')}; border: 1px solid {a('rest')}; color: {a('text')};
}}
QPushButton[{PROP.ACCENT}="true"]:hover    {{ background: {a('hover')}; border: 1px solid {a('hover')}; }}
QPushButton[{PROP.ACCENT}="true"]:pressed  {{ background: {a('pressed')}; border: 1px solid {a('pressed')}; }}
QPushButton[{PROP.ACCENT}="true"]:disabled {{ background: {a('disabled')}; border: 1px solid {a('disabled')}; color: {txt_off}; }}

#{OBJ.SUBTLE_BUTTON}, #{OBJ.ICON_BUTTON}, #{OBJ.CLOSE_BUTTON} {{
  background: transparent; border: 1px solid transparent; border-radius: {r_ctl}px;
  padding: {METRICS['button_pad_v']}px {METRICS['button_pad_h']}px; color: {txt};
}}
#{OBJ.SUBTLE_BUTTON}:hover, #{OBJ.ICON_BUTTON}:hover, #{OBJ.CLOSE_BUTTON}:hover {{
  background: {subtle2}; border: 1px solid transparent;
}}
#{OBJ.SUBTLE_BUTTON}:pressed, #{OBJ.ICON_BUTTON}:pressed, #{OBJ.CLOSE_BUTTON}:pressed {{
  background: {subtle3}; border: 1px solid transparent; color: {txt2};
}}
#{OBJ.SUBTLE_BUTTON}:disabled, #{OBJ.ICON_BUTTON}:disabled, #{OBJ.CLOSE_BUTTON}:disabled {{
  background: transparent; border: 1px solid transparent; color: {txt_off};
}}
#{OBJ.ICON_BUTTON}, #{OBJ.CLOSE_BUTTON} {{ padding: 4px; min-width: 32px; min-height: 32px; }}

#{OBJ.LINK_BUTTON} {{
  background: transparent; border: 1px solid transparent; padding: 0px 2px;
  color: {a('rest')}; text-align: left;
}}
#{OBJ.LINK_BUTTON}:hover    {{ color: {a('hover')}; border: 1px solid transparent; }}
#{OBJ.LINK_BUTTON}:pressed  {{ color: {a('pressed')}; border: 1px solid transparent; }}
#{OBJ.LINK_BUTTON}:disabled {{ color: {txt_off}; border: 1px solid transparent; }}

QToolButton {{
  background: transparent; border: 1px solid transparent;
  border-radius: {r_ctl}px; padding: 4px; color: {txt};
}}
QToolButton:hover   {{ background: {subtle2}; border: 1px solid transparent; }}
QToolButton:pressed {{ background: {subtle3}; border: 1px solid transparent; }}
QToolButton:checked {{ background: {alt3}; border: 1px solid transparent; }}
QToolButton::menu-indicator {{ width: 0px; height: 0px; }}

#{OBJ.CARD} QPushButton, #{OBJ.FLYOUT} QPushButton, #{OBJ.DIALOG_SURFACE} QPushButton {{
  background: {fill_l}; border: 1px solid {stroke_l}; border-bottom-color: {stroke2_l}; color: {txt_l};
}}
#{OBJ.CARD} QPushButton:hover, #{OBJ.FLYOUT} QPushButton:hover,
#{OBJ.DIALOG_SURFACE} QPushButton:hover {{ background: {fill2_l}; border: 1px solid {stroke_l}; }}
#{OBJ.CARD} QPushButton:pressed, #{OBJ.FLYOUT} QPushButton:pressed,
#{OBJ.DIALOG_SURFACE} QPushButton:pressed {{ background: {fill3_l}; border: 1px solid {stroke_l}; }}
#{OBJ.CARD} QPushButton[{PROP.ACCENT}="true"], #{OBJ.FLYOUT} QPushButton[{PROP.ACCENT}="true"],
#{OBJ.DIALOG_SURFACE} QPushButton[{PROP.ACCENT}="true"] {{
  background: {a('rest')}; border: 1px solid {a('rest')}; color: {a('text')};
}}

/* ── text entry. Focus grows the bottom border 1->2 px, so padding-bottom
      drops by 1 or the control jumps 32 -> 33 px. ──────────────────────── */
QLineEdit, QPlainTextEdit, QTextEdit {{
  background: {fill}; border: 1px solid {stroke}; border-bottom: 1px solid {strong_stroke};
  border-radius: {r_ctl}px; color: {txt};
  padding: {METRICS['button_pad_v']}px {METRICS['textbox_pad_l']}px {METRICS['textbox_pad_b']}px {METRICS['textbox_pad_l']}px;
  min-height: {METRICS['button_min_h']}px; font-size: {body_px}px;
  selection-background-color: {a('rest')}; selection-color: {a('text')};
}}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover {{ background: {fill2}; }}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
  background: {fill_input}; border: 1px solid {stroke};
  border-bottom: {METRICS['focus_outer']}px solid {a('rest')};
  padding-bottom: {METRICS['textbox_pad_b_focus']}px;
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
  background: {fill_off}; border: 1px solid {stroke}; color: {txt_off};
}}
QLineEdit[readOnly="true"] {{ background: {fill3}; color: {txt2}; }}
#{OBJ.SEARCH_BOX} {{ padding-left: {METRICS['textbox_pad_l']}px; }}

/* ── combo, spin ──────────────────────────────────────────────────────── */
QComboBox {{
  background: {fill}; border: 1px solid {stroke}; border-bottom-color: {stroke2};
  border-radius: {r_ctl}px; padding: {METRICS['button_pad_v']}px {METRICS['button_pad_h']}px;
  min-height: {METRICS['button_min_h']}px; color: {txt}; font-size: {body_px}px;
}}
QComboBox:hover    {{ background: {fill2}; }}
QComboBox:disabled {{ background: {fill_off}; color: {txt_off}; }}
QComboBox::drop-down {{ border: none; width: 30px; }}
QComboBox::down-arrow {{ width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
  background: {lay}; border: 1px solid {flyout_stroke}; border-radius: {r_ovl}px;
  color: {txt_l}; padding: 4px; outline: none;
  selection-background-color: {subtle2_l}; selection-color: {txt_l};
}}
QSpinBox, QDoubleSpinBox {{
  background: {fill}; border: 1px solid {stroke}; border-bottom: 1px solid {strong_stroke};
  border-radius: {r_ctl}px; padding: {METRICS['button_pad_v']}px 8px;
  min-height: {METRICS['button_min_h']}px; color: {txt};
  selection-background-color: {a('rest')}; selection-color: {a('text')};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{
  background: {fill_input}; border-bottom: {METRICS['focus_outer']}px solid {a('rest')};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
  background: transparent; border: none; width: 24px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{ background: {subtle2}; }}

/* ── check / radio ────────────────────────────────────────────────────── */
QCheckBox, QRadioButton {{ background: transparent; color: {txt}; font-size: {body_px}px; spacing: 8px; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {txt_off}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 20px; height: 20px; }}
QCheckBox::indicator:unchecked {{
  background: {alt2}; border: 1px solid {strong_stroke}; border-radius: {r_ctl}px;
}}
QCheckBox::indicator:unchecked:hover {{ background: {alt3}; }}
QCheckBox::indicator:checked, QCheckBox::indicator:indeterminate {{
  background: {a('rest')}; border: 1px solid {a('rest')}; border-radius: {r_ctl}px;
}}
QCheckBox::indicator:checked:hover, QCheckBox::indicator:indeterminate:hover {{
  background: {a('hover')}; border: 1px solid {a('hover')};
}}
QCheckBox::indicator:disabled {{ background: {fill_off}; border: 1px solid {strong_off}; }}
QRadioButton::indicator:unchecked {{
  background: {alt2}; border: 1px solid {strong_stroke}; border-radius: 10px;
}}
QRadioButton::indicator:checked {{
  background: {a('rest')}; border: 6px solid {a('rest')}; border-radius: 10px;
}}
QRadioButton::indicator:disabled {{ background: {fill_off}; border: 1px solid {strong_off}; }}

/* ── lists, trees, tables ─────────────────────────────────────────────── */
QListView, QTreeView, QTableView, QListWidget, QTreeWidget {{
  background: transparent; border: none; outline: none; color: {txt};
  font-size: {body_px}px; show-decoration-selected: 0;
}}
QListView::item, QTreeView::item, QListWidget::item, QTreeWidget::item {{
  border-radius: {RADII['hover_pill']}px; padding: 4px; color: {txt};
}}
QListView::item:hover, QTreeView::item:hover,
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {subtle2}; }}
QListView::item:selected, QTreeView::item:selected,
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {subtle3}; color: {txt}; }}
QTreeView::branch {{ background: transparent; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
  background: transparent; border: none; border-bottom: 1px solid {divider};
  padding: 6px 8px; color: {txt2}; font-size: {caption_px}px;
}}
#{OBJ.ACTIVITY_LIST} {{ background: transparent; border: none; outline: none; }}
#{OBJ.ACTIVITY_LIST}::item {{ border-radius: {RADII['hover_pill']}px; }}
#{OBJ.ACTIVITY_LIST}::item:hover {{ background: {subtle2_l}; }}
#{OBJ.ACTIVITY_LIST}::item:selected {{ background: {subtle3_l}; color: {txt_l}; }}
#{OBJ.NAV_LIST} {{ background: transparent; border: none; outline: none; }}
#{OBJ.NAV_LIST}::item {{
  border-radius: {RADII['hover_pill']}px; min-height: {METRICS['nav_item_h']}px;
  padding-left: 12px; color: {txt};
}}
#{OBJ.NAV_LIST}::item:hover {{ background: {subtle2}; }}
#{OBJ.NAV_LIST}::item:selected {{ background: {alt3}; color: {txt}; }}

/* ── scrollbars — Fluent's thin overlay ───────────────────────────────── */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0px; border: none; }}
QScrollBar::handle:vertical {{
  background: {strong_fill}; border-radius: 3px; min-height: 24px; margin: 2px 4px;
}}
QScrollBar::handle:vertical:hover {{ background: {strong_stroke}; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0px; border: none; }}
QScrollBar::handle:horizontal {{
  background: {strong_fill}; border-radius: 3px; min-width: 24px; margin: 4px 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {strong_stroke}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; border: none; background: transparent; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ── progress. The TRACK (1 px) is thinner than the FILL (3 px). ──────── */
QProgressBar {{
  background: {alt3}; border: none; border-radius: 1px;
  max-height: {METRICS['progress_fill_h']}px; min-height: {METRICS['progress_fill_h']}px;
  text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {a('rest')}; border-radius: 1px; }}
#{OBJ.STORAGE_BAR} {{
  background: {alt3}; border: none; border-radius: 2px;
  min-height: {METRICS['ac_bar_h']}px; max-height: {METRICS['ac_bar_h']}px;
}}

/* ── slider ───────────────────────────────────────────────────────────── */
QSlider::groove:horizontal {{ background: {alt3}; height: 4px; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {a('rest')}; height: 4px; border-radius: 2px; }}
QSlider::handle:horizontal {{
  background: {a('rest')}; border: 4px solid {lay}; width: 12px; height: 12px;
  margin: -8px 0px; border-radius: 10px;
}}
QSlider::handle:horizontal:hover {{ border: 3px solid {lay}; }}
QSlider::handle:horizontal:disabled {{ background: {strong_off}; }}

/* ── tabs ─────────────────────────────────────────────────────────────── */
QTabWidget::pane {{ background: transparent; border: none; border-top: 1px solid {divider}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
  background: transparent; border: none; padding: 8px 12px;
  color: {txt2}; font-size: {body_px}px;
}}
QTabBar::tab:hover {{ background: {subtle2}; border-radius: {r_ctl}px; }}
QTabBar::tab:selected {{ color: {txt}; border-bottom: {METRICS['nav_indicator_w']}px solid {a('rest')}; }}

/* ── menus, tooltips ──────────────────────────────────────────────────── */
QMenu {{
  background: {lay}; border: 1px solid {flyout_stroke}; border-radius: {r_ovl}px;
  padding: 4px; color: {txt_l}; font-size: {body_px}px;
}}
QMenu::item {{ background: transparent; padding: 7px 28px 7px 12px; border-radius: {r_ctl}px; }}
QMenu::item:selected {{ background: {subtle2_l}; }}
QMenu::item:disabled {{ color: {txt_off}; }}
QMenu::separator {{ background: {divider}; height: 1px; margin: 4px 8px; }}
QMenu::indicator {{ width: 16px; height: 16px; }}
QToolTip {{
  background: {lay}; color: {txt_l}; border: 1px solid {flyout_stroke};
  border-radius: {r_ctl}px; padding: 6px 8px; font-size: {body_px}px;
}}

/* ── group box, frames ────────────────────────────────────────────────── */
QGroupBox {{
  background: transparent; border: 1px solid {card_stroke}; border-radius: {r_ctl}px;
  margin-top: 12px; padding-top: 8px; color: {txt};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0px 4px; color: {txt2}; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ background: {divider}; border: none; }}

/* ── banners ──────────────────────────────────────────────────────────── */
#{OBJ.BANNER_INFO} {{
  background: {info_bg}; border: 1px solid {card_stroke}; border-radius: {r_ctl}px; padding: 12px;
}}
#{OBJ.BANNER_SUCCESS} {{
  background: {ok_bg}; border: 1px solid {ok}; border-radius: {r_ctl}px; padding: 12px;
}}
#{OBJ.BANNER_CAUTION} {{
  background: {caution_bg}; border: 1px solid {caution}; border-radius: {r_ctl}px; padding: 12px;
}}
#{OBJ.BANNER_CRITICAL} {{
  background: {crit_bg}; border: 1px solid {crit}; border-radius: {r_ctl}px; padding: 12px;
}}

/* ── activity center chrome ───────────────────────────────────────────── */
#{OBJ.ACTIVITY_HEADER} {{
  background: transparent; border: none; border-bottom: 1px solid {divider};
  min-height: {METRICS['ac_header_h']}px;
}}
""".strip() + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# ThemeManager
# ─────────────────────────────────────────────────────────────────────────────

class ThemeManager(QObject):
    """The theme source of truth.

    QStyleHints.colorScheme() is driven ENTIRELY by ~/.config/gtk-{3,4}.0/settings.ini
    `gtk-application-prefer-dark-theme` and ignores org.freedesktop.appearance;
    on this machine a stale `=true` makes Qt report Dark even when GNOME is light,
    and colorSchemeChanged NEVER fires. DO NOT USE IT.

    QPalette.Accent is a hard-coded #308cc6 and is NOT the system accent.

    The XDG portal is reliable. It is read via Gio because PySide6's QDBusArgument
    cannot demarshal the accent-colour `(ddd)` struct.
    """

    #: (dark, accent hex). Mirrors BUS.theme_changed for local listeners.
    changed = Signal(bool, str)

    #: The portal emits color-scheme SettingChanged TWICE per change and a full
    #: re-polish is O(widgets x rules), so every change is coalesced by this.
    DEBOUNCE_MS = 60

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dark: bool = False
        self._system_accent: str = ACCENT_RAMP_SYSTEM["Base"]
        self._high_contrast: bool = False
        self._mode: ThemeMode = ThemeMode.SYSTEM
        self.use_system_accent: bool = False
        self._app = None
        self._started = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self._emit_changed)

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        """Read the portal once and subscribe to SettingChanged. Idempotent."""
        global _ACTIVE
        _ACTIVE = self
        invalidate_detection()
        self.refresh()
        if not self._started:
            self._started = True
            self._subscribe()

    def _subscribe(self) -> None:
        if _QtDBus is not None:
            try:
                _QtDBus.QDBusConnection.sessionBus().connect(
                    PORTAL_SVC, PORTAL_PATH, PORTAL_IF, "SettingChanged", self,
                    b"_onSettingChanged(QString,QString,QDBusVariant)")
            except Exception:
                pass
        # Secondary trigger only — the VALUE still comes from the portal.
        try:
            from PySide6.QtGui import QGuiApplication

            app = QGuiApplication.instance()
            if app is not None:
                app.styleHints().colorSchemeChanged.connect(self._on_qt_hint)
        except Exception:
            pass

    def stop(self) -> None:
        global _ACTIVE
        self._timer.stop()
        if _ACTIVE is self:
            _ACTIVE = None

    # ── state ────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        """Re-read every appearance key from the portal."""
        cs = _read_portal("color-scheme")     # 0 no preference, 1 dark, 2 light
        portal_dark: bool | None = None
        if cs is not None:
            try:
                portal_dark = int(cs) == 1
            except (TypeError, ValueError):
                portal_dark = None
        if portal_dark is None:
            qt = _qt_color_scheme_dark()
            portal_dark = bool(qt) if qt is not None else self._dark
        if self._mode is ThemeMode.LIGHT:
            self._dark = False
        elif self._mode is ThemeMode.DARK:
            self._dark = True
        else:
            self._dark = portal_dark

        ac = _read_portal("accent-color")     # (ddd) 0..1, or (-1,-1,-1) unset
        if isinstance(ac, (list, tuple)) and len(ac) == 3 and all(float(x) >= 0 for x in ac):
            self._system_accent = _hex(tuple(round(float(x) * 255) for x in ac))

        hc = _read_portal("contrast")         # 0 normal, 1 high
        if hc is not None:
            try:
                self._high_contrast = int(hc) == 1
            except (TypeError, ValueError):
                pass

    def is_dark(self) -> bool:
        return self._dark

    def accent_hex(self) -> str:
        """The accent the UI actually paints — OneDrive's by default."""
        return accent("rest", dark=self._dark)

    def system_accent_hex(self) -> str:
        """The desktop's accent, as reported by the portal. Not painted unless
        `use_system_accent` is set: a purple OneDrive looks broken."""
        return self._system_accent

    def high_contrast(self) -> bool:
        return self._high_contrast

    def animations_enabled(self) -> bool:
        return animations_enabled()

    def mode(self) -> ThemeMode:
        return self._mode

    def set_mode(self, mode: ThemeMode) -> None:
        """SYSTEM follows the portal; LIGHT / DARK pin the theme."""
        if mode == self._mode:
            return
        self._mode = ThemeMode(mode)
        self.refresh()
        self._schedule()

    def set_use_system_accent(self, enabled: bool) -> None:
        if bool(enabled) == self.use_system_accent:
            return
        self.use_system_accent = bool(enabled)
        _STYLESHEET_CACHE.clear()
        self._schedule()

    # ── application ──────────────────────────────────────────────────────
    def apply(self, app) -> None:
        """Fusion + the full stylesheet + a matching palette. Call ONCE before
        any widget exists, and again only on a real theme change."""
        if app is None:
            return
        self._app = app
        try:
            app.setStyle("Fusion")
        except Exception:
            pass
        self._apply_palette(app)
        app.setStyleSheet(stylesheet(dark=self._dark))

    def _apply_palette(self, app) -> None:
        """QSS does not reach every native primitive (menu shadows, item view
        backgrounds during a drag), so the palette is kept in step with it."""
        try:
            from PySide6.QtGui import QColor, QPalette
        except Exception:
            return
        dark = self._dark
        p = QPalette()
        p.setColor(QPalette.ColorRole.Window, QColor(base(dark=dark)))
        p.setColor(QPalette.ColorRole.WindowText, QColor(T("TextFillColorPrimary", dark=dark)))
        p.setColor(QPalette.ColorRole.Base, QColor(layer(dark=dark)))
        p.setColor(QPalette.ColorRole.AlternateBase,
                   QColor(T("SolidBackgroundFillColorSecondary", dark=dark)))
        p.setColor(QPalette.ColorRole.Text, QColor(T("TextFillColorPrimary", dark=dark, on="layer")))
        p.setColor(QPalette.ColorRole.Button, QColor(T("ControlFillColorDefault", dark=dark)))
        p.setColor(QPalette.ColorRole.ButtonText, QColor(T("TextFillColorPrimary", dark=dark)))
        p.setColor(QPalette.ColorRole.ToolTipBase, QColor(layer(dark=dark)))
        p.setColor(QPalette.ColorRole.ToolTipText, QColor(T("TextFillColorPrimary", dark=dark, on="layer")))
        p.setColor(QPalette.ColorRole.Highlight, QColor(accent("rest", dark=dark)))
        p.setColor(QPalette.ColorRole.HighlightedText, QColor(accent("text", dark=dark)))
        p.setColor(QPalette.ColorRole.Link, QColor(accent("rest", dark=dark)))
        p.setColor(QPalette.ColorRole.PlaceholderText, QColor(T("TextFillColorTertiary", dark=dark)))
        for group in (QPalette.ColorGroup.Disabled,):
            p.setColor(group, QPalette.ColorRole.WindowText, QColor(T("TextFillColorDisabled", dark=dark)))
            p.setColor(group, QPalette.ColorRole.Text, QColor(T("TextFillColorDisabled", dark=dark)))
            p.setColor(group, QPalette.ColorRole.ButtonText, QColor(T("TextFillColorDisabled", dark=dark)))
        try:
            app.setPalette(p)
        except Exception:
            pass

    # ── change plumbing ──────────────────────────────────────────────────
    @Slot(str, str, _QDBusVariant)
    def _onSettingChanged(self, ns: str, key: str, value) -> None:
        if ns != PORTAL_NS or key not in ("color-scheme", "accent-color", "contrast"):
            return
        # The struct payload cannot be demarshalled by QDBusArgument, so every
        # change re-reads through Gio rather than trusting the signal argument.
        self.refresh()
        self._schedule()

    @Slot()
    def _on_qt_hint(self, *_args) -> None:
        self.refresh()
        self._schedule()

    def _schedule(self) -> None:
        self._timer.start()

    def _emit_changed(self) -> None:
        _STYLESHEET_CACHE.clear()
        if self._app is not None:
            self.apply(self._app)
        hexa = self.accent_hex()
        self.changed.emit(self._dark, hexa)
        BUS.theme_changed.emit(self._dark, hexa)


#: ARCHITECTURE §8 calls it ThemeWatcher; CONTRACTS §7 calls it ThemeManager.
#: One class, both names.
ThemeWatcher = ThemeManager


def manager() -> ThemeManager | None:
    """The live ThemeManager, or None before app.py builds one."""
    return _ACTIVE


__all__ = [
    "Surface", "BASE_LIGHT", "BASE_DARK", "LAYER_LIGHT", "LAYER_DARK",
    "TOKENS", "TOKENS_LIGHT", "TOKENS_LIGHT_LAYER", "TOKENS_DARK", "TOKENS_DARK_LAYER",
    "ACCENT", "ACCENT_ONEDRIVE", "ACCENT_RAMP_SYSTEM", "ACCENT_RAMP_ONEDRIVE",
    "ACCENT_ROLES", "ACCENT_HOVER_ALPHA", "ACCENT_PRESSED_ALPHA", "GNOME_ACCENTS",
    "LOGO_COLORS", "LOGO_VIEWBOX",
    "RADII", "SPACING", "METRICS", "TYPE", "FONT_CANDIDATES",
    "FALLBACK_LINE_HEIGHT_DELTA", "DURATION", "CURVES", "SHADOWS", "OBJ", "PROP",
    "T", "accent", "accent_ramp", "base", "layer", "surface", "shadow", "mix",
    "font_px", "line_height", "weight", "duration", "curve", "stylesheet",
    "current_dark", "invalidate_detection", "animations_enabled", "manager",
    "ThemeManager", "ThemeWatcher",
]
