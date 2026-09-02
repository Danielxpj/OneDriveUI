#!/usr/bin/env python3
"""Render the README's screenshots: real windows, composed onto a backdrop.

`scripts/gallery.py` renders the widget *kit*: a contact sheet for judging Fluent
fidelity, which is a developer's picture. `scripts/preview.py` renders one window
per file, on nothing. Neither is what belongs at the top of a README, so this
takes preview.py's windows and arranges them: at 2x, on a backdrop, with the drop
shadow a window has on a desktop and does not have in a `grab()`.

Usage::

    python3 scripts/shots.py                 # writes docs/shot-*.png
    python3 scripts/shots.py --out /tmp/x    # somewhere else
    python3 scripts/shots.py --dpr 1         # smaller files, for a quick look

Every window is built by `preview.py`'s builders, so what is photographed here
is what that script opens on a real display — there is no second, prettier
construction of the UI that only exists for the README.

`HOME` is overridden while the windows are built. The wizard's folder page shows
`Path.home() / "OneDrive"`, and a screenshot in a public README should not carry
the developer's user name.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# `import preview` inside the builders resolves through here. Running the file
# as a script would put it on the path anyway; saying so keeps the import from
# looking like it works by accident.
sys.path.insert(0, str(REPO / "scripts"))

# Before PySide6 is imported anywhere: offscreen so this needs no compositor,
# and the scale factor is read once, when QApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Importing PySide6 does not construct a QApplication, so these are safe here;
# the scale factor is read later, when `main()` creates one.
from PySide6.QtCore import QRectF, Qt                             # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath  # noqa: E402

#: What the home directory looks like in a screenshot. Not a real path — it is
#: never written to, only rendered.
SHOT_HOME = "/home/you"

#: The backdrop each theme's board is painted on. Deliberately NOT the window's
#: own background: a window the same colour as what surrounds it has no edge,
#: and the shadow alone has to carry the boundary.
BACKDROP = {False: "#e9e9ec", True: "#141416"}

#: Padding around the outermost window on a board, in logical pixels.
MARGIN = 28
#: Gap between two windows side by side.
GAP = 24
#: Rounded-corner radius for a window that paints square ones. Settings does —
#: on a desktop the compositor rounds it, and `grab()` sees what the widget
#: painted, not what the screen showed. The flyout rounds itself and keeps it.
RADIUS = 10


def shadow(painter, rect, *, radius: int, dark: bool) -> None:
    """A soft drop shadow around `rect`, drawn as stacked translucent outlines.

    QGraphicsDropShadowEffect would be the obvious tool and cannot be used here:
    it applies while a *widget* is painted, and these are already flat images.
    Twenty-four rounded rectangles at a low alpha, each a little larger than the
    last, add up to a quadratic falloff that reads as a blur.

    The area under `rect` is clipped out before any of them is drawn. Without
    that the twenty-four layers stack into a nearly opaque slab, which is
    invisible under an opaque window and, under a dialog that is transparent
    around its own shadow, appears as a dark rectangle behind it.
    """
    outside = QPainterPath()
    outside.addRect(QRectF(0, 0, 1 << 15, 1 << 15))
    inside = QPainterPath()
    inside.addRoundedRect(QRectF(rect), radius, radius)
    painter.save()
    painter.setClipPath(outside.subtracted(inside))
    steps = 24
    for i in range(steps, 0, -1):
        spread = i * 1.6
        alpha = int((1 - i / steps) ** 2 * (44 if dark else 24))
        if alpha <= 0:
            continue
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, alpha))
        painter.drawRoundedRect(
            QRectF(rect).adjusted(-spread, -spread + 5, spread, spread + 5),
            radius + spread, radius + spread)
    painter.restore()


def paste(painter, image, x: int, y: int, *, dark: bool, radius: int = RADIUS,
          dpr: float = 1.0, decorate: bool = True) -> None:
    """Draw `image` at logical (x, y).

    `decorate` rounds the corners and puts a shadow behind it, which is right
    for a window: Qt hands us square corners and no shadow, because on a real
    desktop both belong to the compositor. It is wrong for a dialog, which
    paints its own Fluent shadow into a transparent margin — rounding that
    would clip the shadow and adding ours would double it.
    """
    rect = QRectF(x, y, image.width() / dpr, image.height() / dpr)
    if not decorate:
        painter.drawImage(rect, image)
        return
    painter.save()
    shadow(painter, rect, radius=radius, dark=dark)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    painter.setClipPath(path)
    painter.drawImage(rect, image)
    painter.restore()


def build_board(size: tuple[int, int], *, dark: bool, dpr: float):
    """A blank board of `size` logical pixels, at `dpr`, filled with the backdrop."""
    image = QImage(int(size[0] * dpr), int(size[1] * dpr),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor(BACKDROP[dark]))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    return image, painter


def grab(app, build, *, dpr: float):
    """Build a window, let it settle, and photograph it."""
    import preview

    widget = build()
    widget.show()
    preview.settle(app, 500)
    shot = widget.grab().toImage()
    widget.hide()
    widget.deleteLater()
    return shot


def hero(app, *, dark: bool, dpr: float):
    """Settings, with the Activity Center overlapping its lower-right corner.

    The one image that has to say what the application *is*: a settings window
    that looks like Windows 11's, and the flyout that is the client's actual
    day-to-day surface, in the relationship they have on screen — the flyout
    sits over the bottom right, where the tray put it, and hangs past both
    edges so the depth is unambiguous.
    """
    import preview

    settings = grab(app, preview.WINDOWS["settings"], dpr=dpr)
    flyout = grab(app, preview.WINDOWS["activity"], dpr=dpr)

    sw, sh = settings.width() / dpr, settings.height() / dpr
    fw, fh = flyout.width() / dpr, flyout.height() / dpr

    # Both windows are placed first and the board is sized to their union, so a
    # flyout taller than the window it overlaps cannot be clipped by a height
    # computed before anyone knew where it would land.
    settings_at = (MARGIN, MARGIN)
    flyout_at = (MARGIN + sw - fw * 0.42, MARGIN + sh - fh + 96)

    right = max(settings_at[0] + sw, flyout_at[0] + fw)
    bottom = max(settings_at[1] + sh, flyout_at[1] + fh)
    board, painter = build_board((int(right + MARGIN), int(bottom + MARGIN)),
                                 dark=dark, dpr=dpr)
    paste(painter, settings, int(settings_at[0]), int(settings_at[1]),
          dark=dark, dpr=dpr)
    paste(painter, flyout, int(flyout_at[0]), int(flyout_at[1]),
          dark=dark, radius=8, dpr=dpr)
    painter.end()
    return board


def row(app, names: list[str], *, dark: bool, dpr: float, align: str = "top"):
    """Several windows or dialogs side by side, top- or centre-aligned."""
    import preview

    everything = {**preview.WINDOWS, **preview._dialogs()}
    shots = [grab(app, everything[name], dpr=dpr) for name in names]
    shots = [shot if not _has_transparent_edge(shot) else trim(shot, dpr=dpr)
             for shot in shots]
    widths = [s.width() / dpr for s in shots]
    heights = [s.height() / dpr for s in shots]
    # A dialog arrives with transparent margins around its own shadow; a window
    # arrives as a solid rectangle. That is the difference `decorate` keys off,
    # and it is visible in the image itself rather than in a list of names.
    solid = [not _has_transparent_edge(shot) for shot in shots]

    width = int(MARGIN * 2 + sum(widths) + GAP * (len(shots) - 1))
    height = int(MARGIN * 2 + max(heights))
    board, painter = build_board((width, height), dark=dark, dpr=dpr)

    x = MARGIN
    for shot, w, h, opaque in zip(shots, widths, heights, solid, strict=True):
        y = MARGIN if align == "top" else int(MARGIN + (max(heights) - h) / 2)
        paste(painter, shot, int(x), y, dark=dark, dpr=dpr, decorate=opaque)
        x += w + GAP
    painter.end()
    return board


def trim(image, *, dpr: float, pad: int = 6):
    """Crop the fully transparent border off `image`, keeping `pad` around it.

    A dialog is grabbed as the whole widget, and most of that widget is the
    transparent margin its shadow needs — `mass_delete` is a 230 px card in a
    514 px image. Composing those untrimmed leaves a board that is two thirds
    empty and looks like a layout mistake.

    The alpha channel is read straight out of the buffer rather than through
    `pixelColor()`: at 2x that is a million calls into Qt per dialog, and this
    is a loop over bytes instead.
    """
    src = image.convertToFormat(QImage.Format.Format_ARGB32)
    width, height, stride = src.width(), src.height(), src.bytesPerLine()
    data = bytes(src.constBits())

    top, bottom, left, right = height, -1, width, -1
    for y in range(height):
        base = y * stride
        row = data[base:base + width * 4]
        # Byte 3 of each little-endian ARGB32 pixel is alpha.
        alphas = row[3::4]
        if max(alphas) == 0:
            continue
        top = min(top, y)
        bottom = y
        first = next(i for i, a in enumerate(alphas) if a)
        last = width - 1 - next(i for i, a in enumerate(reversed(alphas)) if a)
        left = min(left, first)
        right = max(right, last)

    if bottom < 0:                                   # nothing but transparency
        return image
    margin = int(pad * dpr)
    x0 = max(0, left - margin)
    y0 = max(0, top - margin)
    x1 = min(width - 1, right + margin)
    y1 = min(height - 1, bottom + margin)
    cropped = src.copy(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    cropped.setDevicePixelRatio(dpr)
    return cropped


def _has_transparent_edge(image) -> bool:
    """Is the image's top-left pixel transparent? Then it carries its own margin."""
    from PySide6.QtGui import qAlpha

    return qAlpha(image.pixel(0, 0)) == 0


def states(app, *, dark: bool, dpr: float):
    """The file states, as the file manager shows them.

    Files On-Demand is the feature people come looking for and the hardest to
    photograph: it lives in a Nautilus column, and Nautilus cannot be driven
    headless. So this shows the vocabulary itself — the real `StatusBadge`
    widget, painted by the same code the emblems use, beside the label
    `strings.FILE_STATE_LABEL` gives it. Every word here is Windows' own.
    """
    from PySide6.QtWidgets import QGridLayout, QLabel, QWidget

    import preview
    from onedriveui.models import FileState
    from onedriveui.strings import S
    from onedriveui.ui import qss
    from onedriveui.ui.theme import OBJ, PROP, SPACING
    from onedriveui.ui.widgets.indicators import StatusBadge

    shown = (FileState.ONLINE_ONLY, FileState.LOCAL, FileState.PINNED,
             FileState.SYNCING, FileState.EXCLUDED, FileState.ERROR)

    strip = QWidget()
    strip.setObjectName(OBJ.ROOT)
    strip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    grid = QGridLayout(strip)
    grid.setContentsMargins(SPACING["l"], SPACING["l"], SPACING["l"], SPACING["l"])
    grid.setHorizontalSpacing(SPACING["l"])
    grid.setVerticalSpacing(SPACING["s"])
    for column, state in enumerate(shown):
        badge = StatusBadge(strip, state=state, size=SPACING["xl"])
        grid.addWidget(badge, 0, column, Qt.AlignmentFlag.AlignHCenter)
        label = QLabel(S.FILE_STATE_LABEL[str(state)], strip)
        qss.set_property(label, PROP.TYPE, "caption")
        label.setWordWrap(True)
        label.setFixedWidth(132)
        grid.addWidget(label, 1, column, Qt.AlignmentFlag.AlignTop)
    strip.adjustSize()

    strip.show()
    preview.settle(app, 250)
    shot = strip.grab().toImage()
    strip.hide()

    width = int(shot.width() / dpr + MARGIN * 2)
    height = int(shot.height() / dpr + MARGIN * 2)
    board, painter = build_board((width, height), dark=dark, dpr=dpr)
    paste(painter, shot, MARGIN, MARGIN, dark=dark, radius=8, dpr=dpr)
    painter.end()
    return board


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the README screenshots.")
    parser.add_argument("--out", default=str(REPO / "docs"), metavar="DIR")
    parser.add_argument("--dpr", type=float, default=2.0,
                        help="device pixel ratio (default 2, for HiDPI)")
    args = parser.parse_args(argv)

    if args.dpr != 1.0:
        os.environ["QT_SCALE_FACTOR"] = str(args.dpr)
    os.environ["HOME"] = SHOT_HOME

    from PySide6.QtWidgets import QApplication

    import preview

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])

    written: list[Path] = []
    for dark in (True, False):
        preview.apply_theme(app, dark=dark)
        suffix = "dark" if dark else "light"

        boards = {
            f"shot-hero-{suffix}.png": lambda: hero(app, dark=dark, dpr=args.dpr),
            f"shot-wizard-{suffix}.png": lambda: row(
                app, ["wizard", "wizard_folder", "wizard_tutorial"],
                dark=dark, dpr=args.dpr),
            f"shot-states-{suffix}.png": lambda: states(
                app, dark=dark, dpr=args.dpr),
            f"shot-dialogs-{suffix}.png": lambda: row(
                app, ["mass_delete", "free_up", "resync"],
                dark=dark, dpr=args.dpr, align="centre"),
        }
        for name, make in boards.items():
            path = out / name
            make().save(str(path), "PNG")
            written.append(path)
            print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
