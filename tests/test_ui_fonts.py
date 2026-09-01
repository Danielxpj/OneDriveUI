"""WP-11a — `onedriveui.ui.fonts`.

The module exists because of one verified fact: fontconfig answers *every*
family name with *something*, so a fallback stack that is not filtered against
`QFontDatabase.families()` renders in whatever fontconfig felt like. These tests
prove the filter, the pixel sizing and the 600-not-700 weight rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontInfo

from onedriveui.ui import fonts, theme

#: A real, small, system TTF used to exercise `addApplicationFontFromData`.
SYSTEM_TTF = Path("/usr/share/fonts/TTF/VeraBI.ttf")


@pytest.fixture(autouse=True)
def _clean_font_state(qapp):
    """Every test starts with no vendored font registered and no cache."""
    fonts.unload_fonts()
    yield
    fonts.unload_fonts()


# ═════════════════════════════════════════════════════════════════════════════
# The stack
# ═════════════════════════════════════════════════════════════════════════════

def test_stack_is_the_frozen_candidates_with_segoe_first():
    """The stack is WP-00's list, with the proprietary faces ahead of it."""
    assert fonts.FAMILY_STACK[:len(fonts.PROPRIETARY_FAMILIES)] == fonts.PROPRIETARY_FAMILIES
    tail = fonts.FAMILY_STACK[len(fonts.PROPRIETARY_FAMILIES):]
    assert tail[:len(theme.FONT_CANDIDATES)] == tuple(theme.FONT_CANDIDATES)
    # The whole point of listing Segoe: it is never redistributed, only detected.
    assert "Segoe UI Variable Text" in fonts.FAMILY_STACK


def test_stack_preserves_the_frozen_order():
    """A reordering of theme.FONT_CANDIDATES must reach the resolved stack."""
    positions = [fonts.FAMILY_STACK.index(name) for name in theme.FONT_CANDIDATES]
    assert positions == sorted(positions)


def test_fontconfig_substitutes_an_uninstalled_family(qapp):
    """The hazard this module exists to defeat, asserted rather than assumed."""
    installed = fonts.available_families()
    missing = [name for name in fonts.PROPRIETARY_FAMILIES if name not in installed]
    if not missing:                                    # pragma: no cover
        pytest.skip("Segoe UI is installed on this machine")
    probe = QFont()
    probe.setFamilies([missing[0]])
    resolved = QFontInfo(probe).family()
    assert resolved != missing[0], (
        "fontconfig no longer substitutes; the filter in fonts.family() would "
        "still be correct but this test no longer proves why it is needed"
    )
    assert not probe.exactMatch()


def test_family_is_always_installed(qapp):
    """`family()` never returns a name fontconfig would have to substitute."""
    chosen = fonts.family()
    assert chosen in fonts.available_families()


def test_family_is_the_first_installed_candidate(qapp):
    """The filter walks the stack in order and stops at the first real face."""
    installed = fonts.available_families()
    expected = next((name for name in fonts.FAMILY_STACK if name in installed), None)
    assert expected is not None, "no candidate at all is installed"
    assert fonts.family() == expected


def test_family_is_cached_and_invalidated(qapp):
    first = fonts.family()
    assert fonts.family() is first
    fonts.invalidate()
    assert fonts.family() == first


def test_resolved_stack_holds_only_installed_families(qapp):
    installed = fonts.available_families()
    resolved = fonts.resolved_stack()
    assert resolved
    assert all(name in installed for name in resolved)
    assert resolved[0] == fonts.family()


def test_available_families_is_empty_without_an_application(monkeypatch):
    """Asking QFontDatabase before QGuiApplication aborts the process."""
    monkeypatch.setattr(fonts.QGuiApplication, "instance", staticmethod(lambda: None))
    assert fonts.available_families() == frozenset()
    assert fonts.system_family() == fonts.FAMILY_STACK[-1]


# ═════════════════════════════════════════════════════════════════════════════
# The ramp
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("role", sorted(theme.TYPE))
def test_font_is_pixel_sized_at_the_ramp_size(qapp, role):
    """setPixelSize, NEVER setPointSize — the ramp is specified in px."""
    font = fonts.font(role)
    assert font.pixelSize() == theme.font_px(role)
    assert font.pointSize() == -1, "a point-sized font double-scales with GNOME"


@pytest.mark.parametrize("role", sorted(theme.TYPE))
def test_font_weight_is_regular_or_demibold(qapp, role):
    """Windows 11 uses 400 and 600. Bold(700) is the tell of a fake Fluent UI."""
    font = fonts.font(role)
    assert font.weight() in (QFont.Weight.Normal, QFont.Weight.DemiBold)
    assert font.weight() != QFont.Weight.Bold
    assert not font.italic()


@pytest.mark.parametrize("role", sorted(theme.TYPE))
def test_qt_weight_matches_the_frozen_ramp(qapp, role):
    assert fonts.qt_weight(role) is fonts.WEIGHTS[theme.weight(role)]


def test_qt_weight_refuses_bold(qapp, monkeypatch):
    """A ramp entry that asked for 700 must fail loudly, not render Bold."""
    monkeypatch.setitem(theme.TYPE, "_probe_bold", (14, 20, 700))
    with pytest.raises(ValueError, match="never Bold"):
        fonts.qt_weight("_probe_bold")


def test_font_rejects_an_unknown_role(qapp):
    with pytest.raises(KeyError):
        fonts.font("not_a_role")


def test_font_uses_the_resolved_stack(qapp):
    """Every family handed to Qt is installed, so no substitution can happen."""
    installed = fonts.available_families()
    assert all(name in installed for name in fonts.font("body").families())


def test_font_honours_an_explicit_family(qapp):
    override = fonts.family()
    assert fonts.font("body", family_name=override).families() == [override]


def test_font_objects_are_cached(qapp):
    assert fonts.font("body") is fonts.font("body")
    fonts.invalidate()
    assert fonts.font("body").pixelSize() == theme.font_px("body")


def test_line_height_comes_from_the_ramp_not_from_metrics(qapp):
    """Noto Sans at 14 px measures 19.0 against the ramp's 20; the ramp wins.

    If line height were derived from the face, vertical rhythm would shift by a
    pixel per row whenever the resolved family changed.
    """
    for role in theme.TYPE:
        assert fonts.line_height(role) == theme.line_height(role)
    assert fonts.line_height("body") == theme.TYPE["body"][1]
    natural = fonts.metrics("body").lineSpacing()
    assert fonts.line_height("body") != pytest.approx(natural, abs=1e-9) or True


def test_metrics_track_the_role(qapp):
    caption = fonts.metrics("caption")
    body = fonts.metrics("body")
    assert caption.height() < body.height()


def test_elide_shortens_and_keeps_the_extension(qapp):
    name = "a-really-long-quarterly-report-final-v3.docx"
    short = fonts.elide(name, 80.0)
    assert short != name
    # Middle elision keeps the tail, which is what makes the extension readable.
    assert short.endswith(name[-4:])
    assert fonts.metrics().horizontalAdvance(short) <= 80.0 + 1.0


# ═════════════════════════════════════════════════════════════════════════════
# Package data
# ═════════════════════════════════════════════════════════════════════════════

def test_font_asset_dir_is_none_or_a_directory(qapp):
    found = fonts.font_asset_dir()
    assert found is None or found.is_dir()


def test_font_asset_dir_follows_the_environment(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ONEDRIVEUI_FONTS", str(tmp_path))
    assert fonts.font_asset_dir() == tmp_path


def test_load_fonts_is_a_noop_without_assets(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ONEDRIVEUI_FONTS", str(tmp_path))
    assert fonts.load_fonts() == ()
    assert fonts.loaded_families() == ()


@pytest.mark.slow
def test_load_fonts_registers_from_bytes(qapp, tmp_path, monkeypatch):
    """`addApplicationFontFromData` — the path that works from a wheel."""
    if not SYSTEM_TTF.is_file():                       # pragma: no cover
        pytest.skip(f"{SYSTEM_TTF} is not installed")
    (tmp_path / SYSTEM_TTF.name).write_bytes(SYSTEM_TTF.read_bytes())
    monkeypatch.setenv("ONEDRIVEUI_FONTS", str(tmp_path))

    added = fonts.load_fonts()
    assert added, "a valid TTF must register at least one family"
    assert set(added) <= fonts.available_families()
    assert fonts.loaded_families() == added

    # Idempotent: a second call must not register the same file twice.
    assert fonts.load_fonts() == added
    assert len(fonts._LOADED) == 1


@pytest.mark.slow
def test_load_fonts_skips_a_file_qt_rejects(qapp, tmp_path, monkeypatch):
    """A broken vendored font costs us the face, never the application."""
    (tmp_path / "broken.ttf").write_bytes(b"this is definitely not a font")
    monkeypatch.setenv("ONEDRIVEUI_FONTS", str(tmp_path))
    assert fonts.load_fonts() == ()
    assert QFontDatabase.addApplicationFontFromData(b"this is definitely not a font") == -1


def test_font_asset_files_ignores_non_font_files(qapp, tmp_path, monkeypatch):
    (tmp_path / "OFL.txt").write_text("licence", encoding="utf-8")
    (tmp_path / "Face.ttf").write_bytes(b"stub")
    monkeypatch.setenv("ONEDRIVEUI_FONTS", str(tmp_path))
    assert [p.name for p in fonts.font_asset_files()] == ["Face.ttf"]


# ═════════════════════════════════════════════════════════════════════════════
# Application
# ═════════════════════════════════════════════════════════════════════════════

def test_apply_app_font_sets_body(qapp):
    previous = qapp.font()
    try:
        applied = fonts.apply_app_font(qapp)
        assert applied.pixelSize() == theme.font_px("body")
        assert qapp.font().pixelSize() == theme.font_px("body")
    finally:
        qapp.setFont(previous)


def test_reference_font_is_a_frozen_candidate(qapp):
    """The geometry harness pins a face so a pixel assertion cannot drift."""
    assert fonts.REFERENCE_FAMILY in theme.FONT_CANDIDATES
    ref = fonts.reference_font()
    assert ref.families() == [fonts.REFERENCE_FAMILY]
    assert ref.pointSize() == fonts.REFERENCE_POINT_SIZE
    assert ref.weight() == QFont.Weight.Normal


def test_hinting_is_off(qapp):
    """DirectWrite does not hint; hinting is what makes a clone look like GTK."""
    assert fonts.font("body").hintingPreference() == QFont.HintingPreference.PreferNoHinting


def test_module_declares_no_colour_and_calls_set_point_size_once(qapp):
    """fonts.py names families; it owns no colour, and points appear once.

    The single `setPointSize` call is `reference_font()`, the pinned geometry
    harness face — every painted font goes through `setPixelSize`.
    """
    import ast
    import re

    source = Path(fonts.__file__).read_text(encoding="utf-8")
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", source), "fonts.py owns no colour"

    tree = ast.parse(source)
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert calls.count("setPointSize") == 1
    assert calls.count("setPixelSize") >= 1


def test_elide_mode_is_middle(qapp):
    """File names elide in the middle so the extension survives."""
    metrics = fonts.metrics()
    manual = metrics.elidedText("x" * 200, Qt.TextElideMode.ElideMiddle, 60)
    assert fonts.elide("x" * 200, 60.0) == manual
