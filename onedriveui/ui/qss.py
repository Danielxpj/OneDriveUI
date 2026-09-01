"""The application stylesheet, and every QSS workaround that makes it correct.

`theme.stylesheet()` (WP-00, frozen) carries the Qt-class layer. This module
composes that with the widget-kit layer — the rules that only exist because
`ui/widgets/` defines its own classes — and is the single place the whole sheet
is built, validated and applied.

The five workarounds, all verified, all enforced here in code:

  1. **Every `QPushButton` rule declares a border.** `QPushButton{background:X}`
     with no `border` falls through to Fusion's native gradient primitive with a
     recoloured palette — a flat fill silently renders as a gradient.
     :func:`build` refuses to emit a sheet that breaks this.
  2. **No bare `QWidget{...}` background rule.** A type selector cascades to
     every descendant, so one such rule repaints the entire window. Every rule
     is scoped by an object name or a concrete class; :func:`build` checks.
  3. **`WA_StyledBackground` on every `QWidget` subclass.** A Python subclass of
     `QWidget` gets no QSS background without it (`QFrame` does). Enforced in
     `ui/widgets/`, and asserted by :func:`check_styled_background`.
  4. **Dynamic properties need a manual repolish.** `setProperty()` alone leaves
     the old rule in place; :func:`set_property` does both.
  5. **The focused `QLineEdit` padding compensation.** Focus grows the bottom
     border 1 -> 2 px, so `padding-bottom` drops by 1 or the control jumps
     32 -> 33 px the moment it is clicked.

Setting a stylesheet re-polishes the whole subtree — it is O(widgets x rules) —
so the sheet is built once per theme, applied once on `QApplication`, and every
per-widget state change rides a pseudo-state or a dynamic property instead.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from onedriveui.ui import theme
from onedriveui.ui.theme import METRICS, OBJ, PROP, RADII, SPACING

# ═════════════════════════════════════════════════════════════════════════════
# Selectors. The widget kit's Qt class names, declared once.
#
# PySide6 gives a Python subclass its own metaobject class name, so a QSS type
# selector matches it (verified: `FluentButton{min-width:123px}` -> a 147 px
# sizeHint). `ui/widgets/controls.py` asserts its class names against these, so
# a rename cannot silently unstyle a widget.
# ═════════════════════════════════════════════════════════════════════════════

class SEL:
    """Qt class names the widget-kit rules are scoped to."""

    BUTTON = "FluentButton"
    LINE_EDIT = "FluentLineEdit"
    CHECK_BOX = "FluentCheckBox"
    RADIO_BUTTON = "FluentRadioButton"
    COMBO_BOX = "FluentComboBox"
    TOGGLE = "ToggleSwitch"
    PROGRESS_BAR = "FluentProgressBar"
    PROGRESS_RING = "ProgressRing"
    STORAGE_BAR = "StorageBar"
    AVATAR = "Avatar"


#: Classes that paint themselves entirely in `paintEvent` and must therefore be
#: kept out of the QSS box model: a background or border on one of these would
#: paint underneath the custom art and change its geometry.
CUSTOM_PAINTED: tuple[str, ...] = (
    SEL.TOGGLE, SEL.PROGRESS_BAR, SEL.PROGRESS_RING, SEL.STORAGE_BAR, SEL.AVATAR,
)

#: Human-readable list of the workarounds, for the About pane and the tests.
WORKAROUNDS: tuple[str, ...] = (
    "every QPushButton rule declares a border (or Fusion paints its gradient)",
    "no bare QWidget background rule (a type selector cascades to descendants)",
    "WA_StyledBackground on every QWidget subclass",
    "setProperty() is always followed by unpolish/polish",
    "the focused QLineEdit drops padding-bottom by 1 as its border grows to 2",
)

# ═════════════════════════════════════════════════════════════════════════════
# Derived geometry. Every number below comes out of theme.METRICS; the
# arithmetic is here because QSS min-height applies to the CONTENT box, while
# the Fluent spec quotes the BOX height.
# ═════════════════════════════════════════════════════════════════════════════

#: Button: content 20 + padding 5+5 + border 1+1 = 32. `min-height` is
#: load-bearing — without it the box is the font's natural line box plus the
#: padding, which lands at 29-33 px depending on the resolved face.
BUTTON_BOX_H = (
    METRICS["button_min_h"] + 2 * METRICS["button_pad_v"] + 2
)

#: TextBox: WinUI pins `TextControlThemeMinHeight` at 32, so the content height
#: is pinned both ways. Unpinned, the box tracks the face's line box and a
#: 20 px-tall Noto Sans line makes a 33 px field.
TEXTBOX_CONTENT_H = (
    METRICS["textbox_h"] - METRICS["button_pad_v"] - METRICS["textbox_pad_b"] - 2
)

#: Focused: the bottom border grows 1 -> 2, so padding-bottom drops 6 -> 5 and
#: the box stays 32. Removing this line is what makes a text field jump on click.
TEXTBOX_FOCUS_DELTA = METRICS["textbox_pad_b"] - METRICS["textbox_pad_b_focus"]

#: A square icon button is the same 32 px box as everything else, with the
#: `xs` padding step around a 16 px glyph.
ICON_BUTTON_PAD = SPACING["xs"]
ICON_BUTTON_CONTENT = METRICS["button_h"] - 2 * ICON_BUTTON_PAD - 2

#: The check / radio indicator BOX is 20 px, which is what
#: `controls.indicator_rect()` measures and paints its glyph inside. QSS
#: `width`/`height` on a subcontrol size the CONTENT box and the border is added
#: OUTSIDE it, so the declared size has to come down by the border on each side.
#:
#: This is not pedantry. The Qt-class layer's `QRadioButton::indicator:checked`
#: expresses the filled dot as `border: 6px solid` over a `width: 20px` content
#: box, which makes the *box* 20 + 6 + 6 = 32 px: a checked radio rendered as a
#: 32 px rounded SQUARE (a 10 px radius no longer closes a 32 px box into a
#: circle), sitting 10 px taller than the same radio unchecked, so picking an
#: option visibly re-flowed the rows around it. The kit restates the whole
#: indicator recipe below at a real 20 px box and paints the dot itself.
INDICATOR_BOX = SPACING["xl"]
INDICATOR_BORDER = 1
INDICATOR_CONTENT = INDICATOR_BOX - 2 * INDICATOR_BORDER

#: The CONTROL is taller than its indicator: WinUI pins `CheckBoxMinHeight` at
#: 32 and centres the 20 px box in it. That margin is not decoration — it is
#: where the focus ring goes. At the indicator's own height the ring is drawn
#: through the label's ascenders and clips the last word of the text.
CHOICE_BOX_H = METRICS["button_h"]

#: The accent button's 1 px bottom stroke. WinUI's
#: `AccentControlElevationBorderBrush` is always flipped (ScaleY=-1) so an accent
#: button carries a DARK bottom edge in both themes. Composited here from the
#: live accent over the darkest solid background token, so it stays correct for
#: an arbitrary system accent as well as for OneDrive's own.
ACCENT_EDGE_ALPHA = 0.75


def accent_edge(*, dark: bool | None = None) -> str:
    """The accent button's darker bottom-edge stroke, for either theme."""
    return theme.mix(
        theme.accent("rest", dark=dark),
        theme.T("SolidBackgroundFillColorBaseAlt", dark=True),
        ACCENT_EDGE_ALPHA,
    )


# ═════════════════════════════════════════════════════════════════════════════
# The isolated box recipes. `build()` emits these and the geometry tests apply
# them on their own, so there is exactly one copy of each recipe.
# ═════════════════════════════════════════════════════════════════════════════

def button_box_qss(selector: str = SEL.BUTTON, *,
                   min_height: bool = True,
                   padding: str | None = None) -> str:
    """The Fluent button box, with no colour and no type ramp.

    `padding: 5px 11px` + `min-height: 20px` + a 1 px border measures exactly
    `QSize(55, 32)` at the reference metrics (`fonts.reference_font()`).

    Args:
        selector: The QSS selector to attach the box to.
        min_height: Drop the `min-height` declaration. The box then collapses to
            the face's line box + 12; the parameter exists so a test can prove
            the declaration is load-bearing rather than decorative.
        padding: Override the padding shorthand, e.g. to reproduce the naive
            `5px 11px 11px 6px` mis-ordering that overshoots to 36 px.

    Returns:
        One complete QSS rule, newline-terminated.
    """
    pad = padding or f"{METRICS['button_pad_v']}px {METRICS['button_pad_h']}px"
    lines = [f"{selector} {{", f"  padding: {pad};"]
    if min_height:
        lines.append(f"  min-height: {METRICS['button_min_h']}px;")
    lines.append(
        f"  border-width: 1px; border-style: solid; "
        f"border-radius: {RADII['control']}px;"
    )
    lines.append("}")
    return "\n".join(lines) + "\n"


def textbox_box_qss(selector: str = SEL.LINE_EDIT, *, compensate: bool = True) -> str:
    """The Fluent text-field box: 32 px unfocused AND focused.

    Args:
        selector: The QSS selector to attach the box to.
        compensate: Drop the focused `padding-bottom` compensation, so a test
            can prove the field jumps to 33 px without it.
    """
    pad_t = METRICS["button_pad_v"]
    pad_l = METRICS["textbox_pad_l"]
    pad_b = METRICS["textbox_pad_b"]
    focus_b = METRICS["textbox_pad_b_focus"] if compensate else pad_b
    focus_w = METRICS["focus_outer"]
    return (
        f"{selector} {{\n"
        f"  padding: {pad_t}px {pad_l}px {pad_b}px {pad_l}px;\n"
        f"  min-height: {TEXTBOX_CONTENT_H}px; max-height: {TEXTBOX_CONTENT_H}px;\n"
        f"  border-width: 1px; border-style: solid; "
        f"border-radius: {RADII['control']}px;\n"
        f"}}\n"
        f"{selector}:focus {{\n"
        f"  padding-bottom: {focus_b}px;\n"
        f"  border-width: 1px 1px {focus_w}px 1px; border-style: solid;\n"
        f"}}\n"
    )


# ═════════════════════════════════════════════════════════════════════════════
# The widget-kit layer
# ═════════════════════════════════════════════════════════════════════════════

def widget_kit_qss(*, dark: bool | None = None) -> str:
    """Every rule that exists because `ui/widgets/` defines its own classes.

    Appended after `theme.stylesheet()`, so a declaration here wins a
    specificity tie with the Qt-class layer while still inheriting everything it
    does not restate — QSS merges declarations across matching rules exactly
    like CSS.
    """
    is_dark = theme.current_dark() if dark is None else bool(dark)

    def t(token: str, on: theme.Surface = "base") -> str:
        return theme.T(token, dark=is_dark, on=on)

    def a(role: str) -> str:
        return theme.accent(role, dark=is_dark)

    body_px = theme.font_px("body")
    caption_px = theme.font_px("caption")
    r_ctl = RADII["control"]
    edge = accent_edge(dark=is_dark)

    stroke = t("ControlStrokeColorDefault")
    stroke2 = t("ControlStrokeColorSecondary")
    strong_stroke = t("ControlStrongStrokeColorDefault")
    fill = t("ControlFillColorDefault")
    fill2 = t("ControlFillColorSecondary")
    fill3 = t("ControlFillColorTertiary")
    fill_off = t("ControlFillColorDisabled")
    fill_input = t("ControlFillColorInputActive")
    txt = t("TextFillColorPrimary")
    txt2 = t("TextFillColorSecondary")
    txt3 = t("TextFillColorTertiary")
    txt_off = t("TextFillColorDisabled")

    parts: list[str] = [
        "\n/* ══ OneDriveUI widget kit — generated by ui/qss.py. Do not hand-edit. ══ */\n",
        "\n/* ── the button box. padding 5/11 + min-height 20 + 1 px border = 32 px. ── */\n",
        button_box_qss(SEL.BUTTON),
        # The box recipe above carries no colour, so restate the palette the
        # QPushButton layer would otherwise be the only source of, and add the
        # accent variant's dark bottom edge (AccentControlElevationBorderBrush
        # is ALWAYS flipped, in both themes).
        f"""{SEL.BUTTON} {{
  background: {fill}; border-color: {stroke}; border-bottom-color: {stroke2};
  color: {txt}; font-size: {body_px}px;
}}
{SEL.BUTTON}:hover {{ background: {fill2}; border-color: {stroke}; border-bottom-color: {stroke2}; }}
{SEL.BUTTON}:pressed {{ background: {fill3}; border-color: {stroke}; color: {txt2}; }}
{SEL.BUTTON}:disabled {{ background: {fill_off}; border-color: {stroke}; color: {txt_off}; }}
/* The Windows 11 focus indicator is a two-tone ring OUTSIDE the control
   (FocusRingStyle, ui/widgets/controls.py), not a fatter border. Pin the border
   width back to 1 px or focus grows the box from 32 to 34. */
{SEL.BUTTON}:focus {{
  border-width: 1px; border-style: solid;
  border-color: {stroke}; border-bottom-color: {stroke2};
}}
{SEL.BUTTON}[{PROP.ACCENT}="true"]:focus {{
  border-width: 1px; border-color: {a('rest')}; border-bottom-color: {edge};
}}
{SEL.BUTTON}[{PROP.ACCENT}="true"] {{
  background: {a('rest')}; border-color: {a('rest')}; border-bottom-color: {edge};
  color: {a('text')};
}}
{SEL.BUTTON}[{PROP.ACCENT}="true"]:hover {{
  background: {a('hover')}; border-color: {a('hover')}; border-bottom-color: {edge};
}}
{SEL.BUTTON}[{PROP.ACCENT}="true"]:pressed {{
  background: {a('pressed')}; border-color: {a('pressed')}; border-bottom-color: {a('pressed')};
}}
{SEL.BUTTON}[{PROP.ACCENT}="true"]:disabled {{
  background: {a('disabled')}; border-color: {a('disabled')}; color: {txt_off};
}}
{SEL.BUTTON}#{OBJ.SUBTLE_BUTTON}, {SEL.BUTTON}#{OBJ.ICON_BUTTON}, {SEL.BUTTON}#{OBJ.CLOSE_BUTTON} {{
  background: transparent; border-color: transparent; border-bottom-color: transparent;
}}
/* A square icon button is a 32 px BOX. QSS min-height sizes the CONTENT, so the
   padding and the border have to come back off first, or the box lands at 42. */
{SEL.BUTTON}#{OBJ.ICON_BUTTON}, {SEL.BUTTON}#{OBJ.CLOSE_BUTTON} {{
  padding: {ICON_BUTTON_PAD}px;
  min-width: {ICON_BUTTON_CONTENT}px;
  min-height: {ICON_BUTTON_CONTENT}px; max-height: {ICON_BUTTON_CONTENT}px;
}}
{SEL.BUTTON}#{OBJ.LINK_BUTTON} {{
  background: transparent; border-color: transparent; border-bottom-color: transparent;
  color: {a('rest')}; padding: {METRICS['button_pad_v']}px {SPACING['xs']}px;
}}
{SEL.BUTTON}#{OBJ.LINK_BUTTON}:hover {{
  color: {a('hover')}; background: {t('SubtleFillColorSecondary')}; border-color: transparent;
}}
{SEL.BUTTON}#{OBJ.LINK_BUTTON}:pressed {{
  color: {a('pressed')}; background: {t('SubtleFillColorTertiary')}; border-color: transparent;
}}
{SEL.BUTTON}#{OBJ.LINK_BUTTON}:disabled {{
  color: {txt_off}; background: transparent; border-color: transparent;
}}
""",
        "\n/* ── the text field. 32 px unfocused AND focused. ── */\n",
        textbox_box_qss(SEL.LINE_EDIT),
        f"""{SEL.LINE_EDIT} {{
  background: {fill}; border-color: {stroke}; border-bottom-color: {strong_stroke};
  color: {txt}; font-size: {body_px}px;
  selection-background-color: {a('rest')}; selection-color: {a('text')};
  placeholder-text-color: {txt3};
}}
{SEL.LINE_EDIT}:hover {{ background: {fill2}; }}
{SEL.LINE_EDIT}:focus {{
  background: {fill_input}; border-color: {stroke}; border-bottom-color: {a('rest')};
}}
{SEL.LINE_EDIT}:disabled {{ background: {fill_off}; border-color: {stroke}; color: {txt_off}; }}
{SEL.LINE_EDIT}[readOnly="true"] {{ background: {fill3}; color: {txt2}; }}
{SEL.LINE_EDIT}#{OBJ.SEARCH_BOX} {{ padding-left: {METRICS['textbox_pad_l'] + SPACING['xl']}px; }}
""",
        "\n/* ── combo box: the button box, pinned both ways to 32 px. ── */\n",
        f"""{SEL.COMBO_BOX} {{
  padding: {METRICS['button_pad_v']}px {METRICS['button_pad_h']}px;
  min-height: {METRICS['button_min_h']}px; max-height: {METRICS['button_min_h']}px;
  border-width: 1px; border-style: solid; border-radius: {r_ctl}px;
  background: {fill}; border-color: {stroke}; border-bottom-color: {stroke2};
  color: {txt}; font-size: {body_px}px;
}}
{SEL.COMBO_BOX}:hover {{ background: {fill2}; }}
{SEL.COMBO_BOX}:focus {{
  border-width: 1px; border-color: {stroke}; border-bottom-color: {a('rest')};
}}
{SEL.COMBO_BOX}:disabled {{ background: {fill_off}; color: {txt_off}; }}
{SEL.COMBO_BOX}::drop-down {{ border: none; width: {SPACING['xxl']}px; }}
""",
        "\n/* ── check / radio: 20 px indicator, glyph painted by the widget. ── */\n",
        f"""/* `min-height` gives the ring vertical room; `padding-right` gives it
   horizontal room. Without the latter the widget's sizeHint ends at the last
   glyph of the label and the ring's right stroke is drawn through it. */
{SEL.CHECK_BOX}, {SEL.RADIO_BUTTON} {{
  background: transparent; color: {txt}; font-size: {body_px}px; spacing: {SPACING['s']}px;
  min-height: {CHOICE_BOX_H}px; padding-right: {METRICS['focus_inflate']}px;
}}
{SEL.CHECK_BOX}:disabled, {SEL.RADIO_BUTTON}:disabled {{ color: {txt_off}; }}
/* CONTENT 18 + 1 px border on each side = a 20 px BOX, which is what
   controls.indicator_rect() measures and centres its painted glyph in. The
   whole recipe is restated per state because the Qt-class layer expresses the
   checked radio as a 6 px border, and that inflates its box to 32 px. */
{SEL.CHECK_BOX}::indicator, {SEL.RADIO_BUTTON}::indicator {{
  width: {INDICATOR_CONTENT}px; height: {INDICATOR_CONTENT}px;
  border-width: {INDICATOR_BORDER}px; border-style: solid;
  background: {t('ControlAltFillColorSecondary')}; border-color: {strong_stroke};
}}
{SEL.CHECK_BOX}::indicator {{ border-radius: {r_ctl}px; }}
{SEL.RADIO_BUTTON}::indicator {{ border-radius: {INDICATOR_BOX // 2}px; }}
{SEL.CHECK_BOX}::indicator:hover, {SEL.RADIO_BUTTON}::indicator:hover {{
  background: {t('ControlAltFillColorTertiary')};
}}
{SEL.CHECK_BOX}::indicator:pressed, {SEL.RADIO_BUTTON}::indicator:pressed {{
  background: {t('ControlAltFillColorQuarternary')};
}}
/* `border-width` is restated in every checked rule on purpose: the Qt-class
   layer's `QRadioButton::indicator:checked` carries a `border: 6px solid`, and a
   pseudo-state selector outranks the plain `::indicator` rule above, so leaving
   the width out here lets the 6 px border back in and the box grows to 32. */
{SEL.CHECK_BOX}::indicator:checked, {SEL.CHECK_BOX}::indicator:indeterminate,
{SEL.RADIO_BUTTON}::indicator:checked {{
  background: {a('rest')}; border-color: {a('rest')};
  border-width: {INDICATOR_BORDER}px; border-style: solid;
}}
{SEL.CHECK_BOX}::indicator:checked:hover, {SEL.CHECK_BOX}::indicator:indeterminate:hover,
{SEL.RADIO_BUTTON}::indicator:checked:hover {{
  background: {a('hover')}; border-color: {a('hover')};
  border-width: {INDICATOR_BORDER}px; border-style: solid;
}}
{SEL.CHECK_BOX}::indicator:checked:pressed, {SEL.CHECK_BOX}::indicator:indeterminate:pressed,
{SEL.RADIO_BUTTON}::indicator:checked:pressed {{
  background: {a('pressed')}; border-color: {a('pressed')};
  border-width: {INDICATOR_BORDER}px; border-style: solid;
}}
{SEL.CHECK_BOX}::indicator:disabled, {SEL.RADIO_BUTTON}::indicator:disabled {{
  background: {fill_off}; border-color: {t('ControlStrongFillColorDisabled')};
  border-width: {INDICATOR_BORDER}px; border-style: solid;
}}
{SEL.CHECK_BOX}::indicator:checked:disabled,
{SEL.CHECK_BOX}::indicator:indeterminate:disabled,
{SEL.RADIO_BUTTON}::indicator:checked:disabled {{
  background: {a('disabled')}; border-color: {a('disabled')};
  border-width: {INDICATOR_BORDER}px; border-style: solid;
}}
""",
        "\n/* ── custom-painted widgets keep the QSS box model off. ── */\n",
        ", ".join(CUSTOM_PAINTED)
        + " {\n  background: transparent; border: none; padding: 0px; margin: 0px;\n}\n",
        f"""{SEL.PROGRESS_BAR} {{
  min-height: {METRICS['progress_fill_h']}px; max-height: {METRICS['progress_fill_h']}px;
}}
{SEL.STORAGE_BAR} {{
  min-height: {METRICS['ac_bar_h']}px; max-height: {METRICS['ac_bar_h']}px;
}}
""",
        "\n/* ── the caption role, restated for the kit's own labels. ── */\n",
        f"""QLabel[{PROP.TYPE}="caption"][{PROP.ROLE}="secondary"] {{
  font-size: {caption_px}px; color: {txt2};
}}
""",
    ]
    return "".join(parts)


# ═════════════════════════════════════════════════════════════════════════════
# Parsing and validation
# ═════════════════════════════════════════════════════════════════════════════

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_BORDER_RE = re.compile(r"(?<![\w-])border(?:-width|-style|-color|-top|-right|"
                        r"-bottom|-left|-top-color|-right-color|-bottom-color|"
                        r"-left-color)?\s*:")
_BACKGROUND_RE = re.compile(r"(?<![\w-])background(?:-color)?\s*:")
#: Anything that ends up drawn as a QPushButton primitive by Fusion.
_BUTTONISH = ("QPushButton", "QToolButton", "QCommandLinkButton", SEL.BUTTON)


def rules(sheet: str) -> tuple[tuple[str, str], ...]:
    """Split a stylesheet into `(selector, body)` pairs, comments removed.

    QSS has no nesting, so a flat split on braces is exact.
    """
    out: list[tuple[str, str]] = []
    text = _COMMENT_RE.sub(" ", sheet)
    for chunk in text.split("}"):
        head, brace, body = chunk.partition("{")
        if not brace:
            continue
        selector = " ".join(head.split())
        if selector:
            out.append((selector, body.strip()))
    return tuple(out)


def _selector_targets_button(selector: str) -> bool:
    """True when a rule paints a button's own box (not one of its sub-controls).

    A `::sub-control` rule (`::menu-indicator`, `::drop-down`) does not go
    through the button primitive and legitimately carries no border.
    """
    if "::" in selector:
        return False
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        # Only the LAST simple selector is the widget being painted;
        # "#Card QPushButton" paints a QPushButton, "QPushButton QLabel" a label.
        leaf = part.split()[-1]
        base = leaf.split(":")[0].split("[")[0].split("#")[0]
        if base in _BUTTONISH:
            return True
        if not base and leaf.startswith("#"):
            continue
    return False


def pushbutton_rules_without_border(sheet: str) -> tuple[str, ...]:
    """Selectors that fill a button but declare no border.

    Each one renders Fusion's gradient instead of the flat fill it asked for.
    """
    bad: list[str] = []
    for selector, body in rules(sheet):
        if not _selector_targets_button(selector):
            continue
        if not _BACKGROUND_RE.search(body):
            continue
        if not _BORDER_RE.search(body):
            bad.append(selector)
    return tuple(bad)


def unscoped_rules(sheet: str) -> tuple[str, ...]:
    """Selectors that would repaint every descendant of whatever they match.

    A bare `QWidget` or `*` rule carrying a background cascades to the entire
    subtree; every rule must be scoped by an object name or a concrete class.
    """
    bad: list[str] = []
    for selector, body in rules(sheet):
        if not _BACKGROUND_RE.search(body):
            continue
        for part in selector.split(","):
            part = part.strip()
            if part in ("QWidget", "*"):
                bad.append(selector)
                break
    return tuple(bad)


def check_focus_compensation(sheet: str) -> bool:
    """True when every focused text-field rule pays back its grown border.

    The focused bottom border is `focus_outer` px instead of 1, so the focused
    rule must drop `padding-bottom` by exactly that difference.
    """
    seen = False
    for selector, body in rules(sheet):
        if ":focus" not in selector:
            continue
        if SEL.LINE_EDIT not in selector and "QLineEdit" not in selector:
            continue
        if "padding-bottom" not in body:
            continue
        seen = True
        want = f"padding-bottom: {METRICS['textbox_pad_b_focus']}px"
        if want not in " ".join(body.split()):
            return False
    return seen


def validate(sheet: str) -> None:
    """Raise `ValueError` if the sheet breaks a workaround.

    Called by :func:`build`, so a regression fails at construction time with the
    offending selector named, rather than rendering a Fusion gradient that
    nobody notices until a screenshot.
    """
    borderless = pushbutton_rules_without_border(sheet)
    if borderless:
        raise ValueError(
            "qss: these button rules fill without a border and will render "
            f"Fusion's gradient: {borderless}"
        )
    unscoped = unscoped_rules(sheet)
    if unscoped:
        raise ValueError(
            f"qss: these rules cascade to every descendant: {unscoped}"
        )
    if not check_focus_compensation(sheet):
        raise ValueError(
            "qss: the focused text field does not compensate its grown bottom "
            "border; the control will jump from 32 to 33 px on focus"
        )


def check_styled_background(widget: QWidget) -> bool:
    """True when `widget` can actually paint a QSS background.

    A direct `QWidget` subclass needs `WA_StyledBackground`; `QFrame` and every
    concrete Qt control already paint one.
    """
    from PySide6.QtWidgets import QFrame

    if widget.testAttribute(Qt.WidgetAttribute.WA_StyledBackground):
        return True
    if isinstance(widget, QFrame):
        return True
    return type(widget).__mro__[1] is not QWidget


# ═════════════════════════════════════════════════════════════════════════════
# Build / apply / repolish
# ═════════════════════════════════════════════════════════════════════════════

_CACHE: dict[tuple[bool, str], str] = {}


def build(*, dark: bool | None = None) -> str:
    """The complete application stylesheet for one theme.

    `theme.stylesheet()` first (the Qt-class layer), then the widget kit. The
    result is validated and cached: building is cheap, but *applying* is
    O(widgets x rules), so callers must not rebuild per frame.
    """
    is_dark = theme.current_dark() if dark is None else bool(dark)
    key = (is_dark, theme.accent("rest", dark=is_dark))
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    sheet = theme.stylesheet(dark=is_dark) + widget_kit_qss(dark=is_dark)
    validate(sheet)
    _CACHE[key] = sheet
    return sheet


def invalidate() -> None:
    """Drop the built-sheet cache. Call after the accent or the theme changes."""
    _CACHE.clear()


#: Qt's own name for the one style whose metrics do not fight QSS.
FUSION = "fusion"


def ensure_fusion(app: QApplication | None = None) -> bool:
    """Put the application on Fusion, unless something already owns the style.

    `QStyleFactory.keys()` is exactly `['Windows', 'Fusion']` here, and a
    platform theme plugin renders differently on every desktop, so Fusion is the
    only correct base.

    A style that reports an **empty** `objectName()` is either the
    `QStyleSheetStyle` wrapper Qt installs once a sheet exists, or a
    `QProxyStyle` the application installed deliberately — the two-tone focus
    ring is one. Replacing either would silently throw the ring away, so this
    only ever promotes a *named* platform style.

    Returns:
        True if the style was changed.
    """
    target = app if app is not None else QApplication.instance()
    if target is None:
        return False
    style = target.style()
    if style is None:
        return False
    name = style.objectName().lower()
    if not name or name == FUSION:
        return False
    target.setStyle("Fusion")
    return True


def apply(app: QApplication | None = None, *, dark: bool | None = None) -> str:
    """Apply the sheet to the application. Returns what was applied."""
    target = app if app is not None else QApplication.instance()
    sheet = build(dark=dark)
    if target is None:
        return sheet
    ensure_fusion(target)
    target.setStyleSheet(sheet)
    return sheet


def repolish(widget: QWidget, *, deep: bool = False) -> None:
    """Re-evaluate `widget`'s rules after a dynamic property changed.

    `setProperty()` on its own leaves the previously matched rule in place —
    this is the manual repolish Qt requires.
    """
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()
    if deep:
        for child in widget.findChildren(QWidget):
            child_style = child.style()
            if child_style is not None:
                child_style.unpolish(child)
                child_style.polish(child)
            child.update()


def set_property(widget: QWidget, name: str, value: object) -> None:
    """`setProperty()` + the repolish it always needs. Never call one alone."""
    widget.setProperty(name, value)
    repolish(widget)


def set_object_name(widget: QWidget, name: str) -> None:
    """`setObjectName()` + repolish, so an id-scoped rule takes effect at once."""
    widget.setObjectName(name)
    repolish(widget)


__all__ = [
    "SEL", "CUSTOM_PAINTED", "WORKAROUNDS",
    "BUTTON_BOX_H", "TEXTBOX_CONTENT_H", "TEXTBOX_FOCUS_DELTA",
    "ICON_BUTTON_PAD", "ICON_BUTTON_CONTENT",
    "ACCENT_EDGE_ALPHA", "accent_edge",
    "button_box_qss", "textbox_box_qss", "widget_kit_qss",
    "rules", "pushbutton_rules_without_border", "unscoped_rules",
    "check_focus_compensation", "check_styled_background", "validate",
    "FUSION", "ensure_fusion",
    "build", "invalidate", "apply", "repolish", "set_property", "set_object_name",
]
