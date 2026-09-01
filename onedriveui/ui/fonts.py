"""Font loading and the Windows 11 type ramp, in PIXELS.

Three facts drive every line of this module, all verified on the target machine:

  * **fontconfig substitutes every unknown family.** `QFont("Segoe UI")` resolves
    to *Adwaita Sans*, `QFont("Inter")` to *Noto Sans*, and
    `QFont.setFamilies([...])` does **not** walk the list to the first installed
    entry — the first name gets substituted and wins. A fallback stack is
    therefore useless unless it is filtered against
    `QFontDatabase.families()` first, which is what :func:`family` does.
  * **`setPixelSize`, never `setPointSize`.** The Fluent ramp is specified in
    device-independent pixels and Qt already scales px by the device pixel
    ratio; mixing points in double-scales against GNOME's
    `text-scaling-factor`.
  * **DemiBold (600), never Bold (700).** Windows 11 uses only Regular and
    Semibold; Bold is the single most common tell of a fake Fluent UI.
    :func:`qt_weight` raises on anything else, so the mistake cannot be made.

The ramp itself, the family candidates and the line heights are **not declared
here** — they are the frozen contract in :mod:`onedriveui.ui.theme` and are read
back out of it.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF, QGuiApplication

from onedriveui.ui import theme

# ═════════════════════════════════════════════════════════════════════════════
# The stack
# ═════════════════════════════════════════════════════════════════════════════

#: Segoe UI (Variable) is proprietary and is never shipped; it is listed first
#: only because a user may have installed it themselves, in which case it is the
#: correct face and every metric in the design spec is exact.
PROPRIETARY_FAMILIES: tuple[str, ...] = (
    "Segoe UI Variable Text",
    "Segoe UI Variable",
    "Segoe UI",
)

#: The redistributable tail: WP-00's frozen candidate list (Selawik, Inter,
#: Adwaita Sans, Noto Sans, Cantarell) plus the last-resort DejaVu. Anything not
#: installed is filtered out by :func:`family`; if nothing matches, Qt's own
#: general font ("sans-serif") answers.
FAMILY_STACK: tuple[str, ...] = (
    PROPRIETARY_FAMILIES + tuple(theme.FONT_CANDIDATES) + ("DejaVu Sans",)
)

#: Qt weight per ramp weight. Only these two exist in the Windows 11 UI.
WEIGHTS: dict[int, QFont.Weight] = {
    400: QFont.Weight.Normal,
    600: QFont.Weight.DemiBold,
}

#: The face and size the box geometry in `ARCHITECTURE §4` was measured against
#: — GNOME's default `org.gnome.desktop.interface font-name`. `sizeHint()` width
#: is font-metric dependent, so a pixel-exact geometry assertion has to pin the
#: metrics or it measures the developer's desktop font instead of the recipe.
#: This is the ONE place a point size appears, and it is never painted.
REFERENCE_FAMILY = "Noto Sans"
REFERENCE_POINT_SIZE = 10

if REFERENCE_FAMILY not in theme.FONT_CANDIDATES:  # pragma: no cover - import guard
    raise ValueError(
        f"fonts: reference family {REFERENCE_FAMILY!r} is not one of "
        f"theme.FONT_CANDIDATES {theme.FONT_CANDIDATES!r}"
    )

#: Loaded font files, so a second `load_fonts()` does not re-register them.
_LOADED: dict[str, tuple[int, tuple[str, ...]]] = {}

#: Resolved-family and QFont caches. Dropped by `invalidate()`.
_FAMILY: str | None = None
_FONTS: dict[tuple[str, str], QFont] = {}

#: `$ONEDRIVEUI_FONTS` overrides the bundled directory, for tests and for a
#: system-wide install that keeps its data outside the wheel.
_ENV_FONTS = "ONEDRIVEUI_FONTS"

#: Font containers `QFontDatabase` will accept.
FONT_SUFFIXES: tuple[str, ...] = (".ttf", ".otf", ".ttc", ".otc")


# ═════════════════════════════════════════════════════════════════════════════
# Package data
# ═════════════════════════════════════════════════════════════════════════════

def font_asset_dir() -> Path | None:
    """Locate the bundled font directory.

    Mirrors `icons.asset_root()`: the environment override first, then
    `onedriveui/assets/fonts` (the installed wheel layout), then
    `<repo>/assets/fonts` (a source checkout).

    Returns:
        The directory, or None when no font has been vendored yet. WP-14 ships
        the TTFs; until then this is legitimately None and the stack falls
        through to an installed system family.
    """
    env = os.environ.get(_ENV_FONTS)
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(here.parent.parent / "assets" / "fonts")
    candidates.append(here.parent.parent.parent / "assets" / "fonts")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def font_asset_files() -> tuple[Path, ...]:
    """Every vendored font file, in a stable order."""
    directory = font_asset_dir()
    if directory is None:
        return ()
    found = [
        path
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix.lower() in FONT_SUFFIXES
    ]
    return tuple(found)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Loading
# ═════════════════════════════════════════════════════════════════════════════

def load_fonts() -> tuple[str, ...]:
    """Register every vendored font with Qt and return the families it added.

    Uses `addApplicationFontFromData`, not `addApplicationFont`: the byte path
    works from a wheel or a zip without materialising a temp file, and it is the
    only one that survives a zipimport install. A file that Qt rejects returns
    -1 and is skipped rather than raising — a broken vendored font must not stop
    the application from starting, it must only cost us the preferred face.

    Idempotent: a file already registered in this process is not added twice.

    Returns:
        The families Qt reports for the files it accepted, deduplicated, in load
        order. Empty when nothing is vendored — which is not an error.
    """
    families: list[str] = []
    for path in font_asset_files():
        key = str(path)
        cached = _LOADED.get(key)
        if cached is not None:
            for name in cached[1]:
                if name not in families:
                    families.append(name)
            continue
        data = _read(path)
        if not data:
            continue
        font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
        if font_id == -1:
            continue
        added = tuple(QFontDatabase.applicationFontFamilies(font_id))
        _LOADED[key] = (font_id, added)
        for name in added:
            if name not in families:
                families.append(name)
    if families:
        invalidate()
    return tuple(families)


def loaded_families() -> tuple[str, ...]:
    """The families this process has registered from package data."""
    out: list[str] = []
    for _font_id, added in _LOADED.values():
        for name in added:
            if name not in out:
                out.append(name)
    return tuple(out)


def unload_fonts() -> None:
    """Remove every application font this module registered. Tests only."""
    for font_id, _added in _LOADED.values():
        QFontDatabase.removeApplicationFont(font_id)
    _LOADED.clear()
    invalidate()


def invalidate() -> None:
    """Drop the resolved family and the QFont cache.

    Called after a font load and by tests that change what is installed. The
    cache is keyed by role, not by theme, because a QFont carries no colour.
    """
    global _FAMILY
    _FAMILY = None
    _FONTS.clear()


# ═════════════════════════════════════════════════════════════════════════════
# Family resolution
# ═════════════════════════════════════════════════════════════════════════════

def available_families() -> frozenset[str]:
    """Every family Qt can actually resolve, or an empty set before QGuiApplication.

    `QFontDatabase.families()` needs a live QGuiApplication; asking earlier
    aborts the process, so the no-application case answers empty and
    :func:`family` falls through to the last candidate.
    """
    if QGuiApplication.instance() is None:
        return frozenset()
    return frozenset(QFontDatabase.families())


def system_family() -> str:
    """Qt's own general UI font — the "sans-serif" end of the stack."""
    if QGuiApplication.instance() is None:
        return FAMILY_STACK[-1]
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def family() -> str:
    """The first family in :data:`FAMILY_STACK` that is genuinely installed.

    This filter is the whole point of the module. `QFont.setFamilies(stack)`
    looks like it does the same job and does not: fontconfig answers *every*
    family name with *something*, so the first entry is substituted and wins,
    and an app that trusts it renders in whatever fontconfig felt like.

    Returns:
        An installed family name; :func:`system_family` when none of the
        candidates is present.
    """
    global _FAMILY
    if _FAMILY is None:
        installed = available_families()
        for candidate in FAMILY_STACK:
            if candidate in installed:
                _FAMILY = candidate
                break
        else:
            _FAMILY = system_family()
    return _FAMILY


def resolved_stack() -> tuple[str, ...]:
    """Only the installed members of the stack, in preference order.

    Handed to `QFont.setFamilies()` so Qt's own fallback walk can supply a
    missing glyph without fontconfig getting the chance to substitute a family
    that is not there at all.
    """
    installed = available_families()
    found = tuple(name for name in FAMILY_STACK if name in installed)
    return found or (system_family(),)


# ═════════════════════════════════════════════════════════════════════════════
# The ramp
# ═════════════════════════════════════════════════════════════════════════════

def qt_weight(role: str) -> QFont.Weight:
    """The Qt weight for a ramp role.

    Raises:
        ValueError: if the ramp ever grows a weight that is not 400 or 600.
            Bold (700) is not a Windows 11 UI weight and must not be reachable.
    """
    value = theme.weight(role)
    try:
        return WEIGHTS[value]
    except KeyError:
        raise ValueError(
            f"fonts: type role {role!r} asks for weight {value}; Windows 11 uses "
            f"only {sorted(WEIGHTS)} — never Bold(700)"
        ) from None


def font(role: str = "body", *, family_name: str | None = None) -> QFont:
    """A QFont for one role of the Windows 11 type ramp.

    Args:
        role: A key of `theme.TYPE` — "caption", "body", "body_strong", ...
        family_name: Override the resolved family. Used by the geometry harness
            and by nothing else in the application.

    Returns:
        A pixel-sized QFont at the ramp's size and weight, never italic. The
        object is cached and shared; call sites must not mutate it in place.

    Raises:
        KeyError: for a role that is not in the ramp.
    """
    resolved = family_name or family()
    key = (role, resolved)
    cached = _FONTS.get(key)
    if cached is not None:
        return cached
    size = theme.font_px(role)          # raises KeyError on an unknown role
    out = QFont()
    if family_name is None:
        out.setFamilies(list(resolved_stack()))
    else:
        out.setFamilies([family_name])
    out.setPixelSize(size)
    out.setWeight(qt_weight(role))
    out.setItalic(False)
    # DirectWrite does not hint; hinting the fallback face is what makes a
    # Fluent clone look like a GTK app at 14 px.
    out.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if out.pointSize() != -1:  # pragma: no cover - setPixelSize guarantees this
        raise ValueError("fonts: the ramp is pixel-sized; setPointSize is banned")
    _FONTS[key] = out
    return out


def line_height(role: str = "body") -> int:
    """The ramp line height in px.

    Never derive this from `QFontMetrics.lineSpacing()`: Noto Sans at 14 px
    measures 19.0 against the ramp's 20, so vertical rhythm drifts by a pixel a
    row. Set it explicitly — a fixed widget height for one line, or
    `QTextBlockFormat.setLineHeight(line_height(role), FixedHeight)` for many.
    """
    return theme.line_height(role)


def metrics(role: str = "body") -> QFontMetricsF:
    """`QFontMetricsF` for a role, for eliding and hit-testing."""
    return QFontMetricsF(font(role))


def elide(text: str, width: float, role: str = "body") -> str:
    """Middle-elide `text` to `width` px.

    QSS has no `text-overflow`, so every elision in the app is measured here.
    File names elide in the MIDDLE so the extension stays readable.
    """
    from PySide6.QtCore import Qt

    return metrics(role).elidedText(text, Qt.TextElideMode.ElideMiddle, int(width))


# ═════════════════════════════════════════════════════════════════════════════
# Application
# ═════════════════════════════════════════════════════════════════════════════

def apply_app_font(app: QGuiApplication | None = None) -> QFont:
    """Load the vendored faces and set the application font to Body 14/400.

    Call once at startup, before any widget is constructed: changing the
    application font later re-lays-out every widget in the process.

    Returns:
        The QFont that was applied.
    """
    target = app if app is not None else QGuiApplication.instance()
    load_fonts()
    body = font("body")
    if target is not None:
        target.setFont(body)
    return body


def reference_font() -> QFont:
    """The pinned face the box geometry was measured against.

    NOT a UI font — nothing paints with it. It exists so a geometry test can
    assert an exact `sizeHint()` without the assertion moving when the
    developer's desktop font changes. This is the only `setPointSize` in the
    package and it is deliberate: the reference is a *desktop default*, which is
    expressed in points.
    """
    out = QFont()
    out.setFamilies([REFERENCE_FAMILY])
    out.setPointSize(REFERENCE_POINT_SIZE)
    out.setWeight(QFont.Weight.Normal)
    out.setItalic(False)
    return out


__all__ = [
    "PROPRIETARY_FAMILIES", "FAMILY_STACK", "WEIGHTS", "FONT_SUFFIXES",
    "REFERENCE_FAMILY", "REFERENCE_POINT_SIZE",
    "font_asset_dir", "font_asset_files",
    "load_fonts", "loaded_families", "unload_fonts", "invalidate",
    "available_families", "system_family", "family", "resolved_stack",
    "qt_weight", "font", "line_height", "metrics", "elide",
    "apply_app_font", "reference_font",
]
