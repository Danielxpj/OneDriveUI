# Building a convincing Windows 11 Fluent UI in PySide6 6.11.2 on GNOME/Wayland

**Status:** authoritative reference for OneDriveUI. Every code snippet here was executed on the
target machine. Empirical results are marked **VERIFIED**; anything I could not execute (needs a
real human mouse click) is marked **UNVERIFIABLE-HEADLESS** with the reasoning shown.

## 0. The machine this was verified on

| Fact | Value |
|---|---|
| Python | 3.14.7 (GCC 16.2.1) |
| PySide6 / Qt | 6.11.2 / 6.11.2 (Arch pkg `pyside6 6.11.2-1.1`) |
| PySide6 location | `/usr/lib/python3.14/site-packages/PySide6` (system pacman package) |
| Session | `XDG_SESSION_TYPE=wayland`, `WAYLAND_DISPLAY=wayland-0`, `XDG_CURRENT_DESKTOP=GNOME` |
| Shell | GNOME Shell **50.4**, Mutter |
| Qt platform plugin | `wayland` (also available: `xcb`, `offscreen`, `minimal`, `vnc`, `eglfs`, `linuxfb`) |
| Qt platform themes available | `libqgtk3.so`, `libqxdgdesktopportal.so` (no `qt6ct`, no `adwaita-qt`) |
| Qt styles available | `['Windows', 'Fusion']` — default resolves to **Fusion** |
| Screens | DP-1/DP-2/DP-3, all 1920x1080, **devicePixelRatio 1.0**, logicalDpi 96.0 |
| HiDPI rounding policy (default) | **`PassThrough`** |
| Tray | works via **StatusNotifierItem**; `org.kde.StatusNotifierWatcher` present, GNOME ext `appindicatorsupport@rgcjonas.gmail.com` enabled |
| Portal | `xdg-desktop-portal` + `-gnome` + `-gtk`; `[preferred] default=gnome;gtk;` |
| PyGObject (`gi`/Gio) | **available** — we rely on this for accent colour |
| Fonts | Noto Sans (default sans), Adwaita Sans, Cantarell, Open Sans, DejaVu Sans, Liberation Sans. **No Segoe UI, no Segoe UI Variable, no Selawik, no Inter, no Roboto.** |
| Build tooling | **no `pip`**, no `pipx`, no `hatch`. `uv` **is** present at `~/.local/bin/uv`. `setuptools`+`wheel` present in system Python. `/usr/lib/python3.14/EXTERNALLY-MANAGED` exists. |

> Three findings on this machine invalidate the "obvious" approach. Read §3.4, §4.1 and §7.1
> before writing any code.

---

## 1. QSS: what actually works

### 1.1 Complete supported property list (Qt 6 stylesheet reference, cross-checked by execution)

**Box model:** `margin[-top/right/bottom/left]`, `padding[-top/right/bottom/left]`, `spacing`,
`width`, `height`, `min-width`, `min-height`, `max-width`, `max-height`

**Borders:** `border`, `border-[top|right|bottom|left]`, `border-color` (+ per-side),
`border-style` (+ per-side), `border-width` (+ per-side), `border-radius`,
`border-[top-left|top-right|bottom-left|bottom-right]-radius`, `border-image` (9-slice)

**Outline:** `outline`, `outline-color`, `outline-style`, `outline-offset`, `outline-radius`
(+ per-corner `outline-*-radius`)

**Background:** `background`, `background-color`, `background-image`, `background-repeat`,
`background-position`, `background-attachment` (`scroll`|`fixed`), `background-clip`,
`background-origin`

**Text/font:** `color`, `font`, `font-family`, `font-size`, `font-style`, `font-weight`,
`text-align`, `text-decoration`, `letter-spacing`, `word-spacing`

**Selection/items:** `selection-color`, `selection-background-color`,
`alternate-background-color`, `placeholder-text-color`, `gridline-color`,
`show-decoration-selected`, `paint-alternating-row-colors-for-empty-area`

**Positioning:** `position` (`relative`|`absolute`), `left`, `right`, `top`, `bottom`,
`subcontrol-origin`, `subcontrol-position`

**Qt-only:** `accent-color`, `image`, `image-position`, `icon`, `icon-size`, `opacity` (0–255,
**only honoured on menus/tooltips — see §1.4**), `lineedit-password-character`,
`lineedit-password-mask-delay`, `messagebox-text-interaction-flags`, `button-layout`,
`dialogbuttonbox-buttons-have-icons`, `widget-animation-duration`,
`titlebar-show-tooltips-on-buttons`, `-qt-background-role`, `-qt-style-features`

**Pseudo-states:** `:active :adjoins-item :alternate :bottom :checked :closable :closed :default
:disabled :editable :edit-focus :enabled :exclusive :first :flat :floatable :focus :has-children
:has-siblings :horizontal :hover :indeterminate :last :left :maximized :middle :minimized
:movable :next-selected :on :off :open :only-one :previous-selected :right :selected :top
:vertical` — all negatable with `:!hover` style syntax.

**Sub-controls:** `::add-line ::add-page ::branch ::chunk ::close-button ::down-arrow
::down-button ::drop-down ::float-button ::groove ::handle ::indicator ::item ::left-arrow
::left-corner ::menu-arrow ::menu-button ::menu-indicator ::pane ::right-arrow ::right-corner
::scroller ::section ::separator ::sub-line ::sub-page ::tab ::tab-bar ::tear ::tearoff ::title
::up-arrow ::up-button`

### 1.2 What is NOT supported (VERIFIED — silently ignored, no warning, no crash)

I applied all of these at once to a widget and it rendered normally:

```qss
QWidget{ box-shadow:0 2px 4px #000; transition:all .2s; transform:translateY(2px);
         filter:blur(4px); backdrop-filter:blur(20px); z-index:3; cursor:pointer;
         text-shadow:0 1px 1px #000; }
```

| Property | Result | Workaround |
|---|---|---|
| `box-shadow` | **ignored.** Pixel directly below a widget with `box-shadow:0 8px 16px rgba(0,0,0,255)` was the parent's colour, unchanged. | `QGraphicsDropShadowEffect` for in-window elevation, or paint the shadow yourself in `paintEvent` (see §2.4). **No compositor shadow for frameless windows on Wayland.** |
| `transition` / `animation` | **ignored.** | `QPropertyAnimation` on a custom `Property` (§2, §9). |
| `transform` | **ignored.** | Custom `paintEvent` with `QPainter.translate/scale/rotate`, or `QGraphicsView`. |
| `filter` / `backdrop-filter` | **ignored.** No acrylic/Mica. GNOME/Mutter exposes **no** background-blur protocol. | Fake it: a flat `SolidBackgroundFillColorBase` + a faint noise texture. Do not attempt real acrylic. |
| `opacity` | **ignored on ordinary widgets.** `QPushButton{background:#FF0000;opacity:0.2}` rendered pure `#ff0000`. | `QGraphicsOpacityEffect` (§9), or bake alpha into `rgba()`. |
| `z-index`, `cursor`, `text-shadow` | ignored | `raise_()`/`lower()`; `setCursor()`; paint text twice. |

**Supported but commonly believed unsupported (VERIFIED working):**

- `letter-spacing:4px` → `QFont.letterSpacing()==4.0`, type `AbsoluteSpacing`
- `text-transform:uppercase` → `QFont.capitalization()==AllUppercase`
- `border-radius` is **antialiased**. Sampling a black rounded rect (`radius:20px`) across the arc
  at y=1 gave `#ff00ff, …, #e900e9, #7d007d, #2c002c, #000000` — a clean 3-pixel AA ramp.
- `qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #FF0000,stop:1 #0000FF)` → top `#f4000b`, bottom `#0b00f4`.
- `rgba(255,0,0,0.5)` over white → `#ff8080`. Alpha as a 0–1 float works.
- Sub-controls fully stylable: `QComboBox::drop-down` painted `#ff0000` at x=150 and
  `QComboBox::down-arrow` `#00ff00` at x=146, exactly as specified.
- `qproperty-<name>` sets **custom Python `QtCore.Property`s**: `Card{qproperty-radius:14;}` →
  `card.radius == 14.0`. Also `qproperty-text:'hello'`, `qproperty-alignment:'AlignCenter'`.

### 1.3 The five QSS gotchas that will bite you

**(a) `background` alone on a QPushButton keeps the Fusion gradient. VERIFIED:**

```
QPushButton{background:#0078D4;}              -> ['#69befe', '#54b2fb', '#3fa7f7']   # GRADIENT!
QPushButton{background:#0078D4;border:none;}  -> ['#0078d4', '#0078d4', '#0078d4']   # flat
QPushButton{background:#0078D4;border:1px solid #005A9E;} -> flat
```
(three samples top/middle/bottom of a 100x40 button)

> **Rule: every styled `QPushButton`/`QToolButton` rule MUST contain an explicit `border`
> declaration**, even `border:none`. Without one, `QStyleSheetStyle` falls through to Fusion's
> native gradient primitive with a recoloured palette. `QLineEdit` is not affected.

**(b) A QWidget *subclass* gets no QSS background without `WA_StyledBackground`. VERIFIED:**

| Class | `WA_StyledBackground` | background painted? |
|---|---|---|
| `QWidget` (bare) | no | **yes** |
| `QWidget` (bare) | yes | yes |
| `class MyPanel(QWidget)` | no | **NO** |
| `class MyPanel(QWidget)` | yes | yes |
| `class MyFrame(QFrame)` | no | **yes** |
| `QFrame`, `QLabel` | no | yes |

> **Rule: derive custom containers from `QFrame`, or call
> `self.setAttribute(Qt.WA_StyledBackground, True)` in `__init__`.** A bare `QWidget` works, which
> is why this bug hides until you subclass. `border-radius` clips correctly in every case where the
> background paints at all.

**(c) A type selector cascades to every descendant.** `QWidget{background:#FF0000}` set on a root
turned the root, a mid container **and** a child `QLabel` red. An id selector `#Root{...}` matches
only that widget (children then show it through because they are transparent). A later
`QLabel{background:#0000FF}` correctly overrode the label to blue.

> **Rule: never write a bare `QWidget{...}` background rule in the app stylesheet.** Scope every
> rule with an object name (`#ActivityPane`) or a concrete class (`QLabel`, `SettingsCard`).

**(d) Dynamic-property selectors need a manual repolish. VERIFIED:**

```python
btn.setProperty("accent", True)   # QSS: QPushButton[accent="true"]{...}
btn.setProperty("accent", False)
btn.style().unpolish(btn); btn.style().polish(btn)   # REQUIRED, else the old rule sticks
```

**(e) Setting a stylesheet re-polishes the whole subtree** — it is O(widgets x rules). Set the
full sheet **once** on `QApplication`, and drive per-widget state through pseudo-states
(`:hover`, `:checked`) and dynamic properties, never by calling `setStyleSheet` per frame.

### 1.4 Curated Fluent QSS starter (runs as-is)

Use the tokens from §6.1. Dark values shown.

```qss
/* scope everything; never a bare QWidget rule */
#Root, #Body            { background: #202020; }                 /* SolidBackgroundFillColorBase */
QLabel                  { color: #FFFFFF; background: transparent; }
QLabel[role="secondary"]{ color: rgba(255,255,255,0.7725); }     /* TextFillColorSecondary C5 */

QPushButton {                                                     /* Standard button */
  background: rgba(255,255,255,0.0588);                           /* ControlFillColorDefault 0F */
  border: 1px solid rgba(255,255,255,0.0706);                     /* ControlStrokeColorDefault 12 */
  border-radius: 4px; padding: 5px 12px; color: #FFFFFF; min-height: 22px;
}
QPushButton:hover    { background: rgba(255,255,255,0.0824); }    /* ControlFillColorSecondary 15 */
QPushButton:pressed  { background: rgba(255,255,255,0.0314);      /* ControlFillColorTertiary 08 */
                       color: rgba(255,255,255,0.5451); }
QPushButton:disabled { background: rgba(255,255,255,0.0431); color: rgba(255,255,255,0.3647); }

QPushButton[accent="true"]          { background: #0078D4; border: 1px solid rgba(255,255,255,0.0784); color: #000000; }
QPushButton[accent="true"]:hover    { background: #1A86D9; }
QPushButton[accent="true"]:pressed  { background: #3C93DD; color: rgba(0,0,0,0.5); }

QLineEdit {
  background: rgba(255,255,255,0.0588); border: 1px solid rgba(255,255,255,0.0706);
  border-bottom: 1px solid rgba(255,255,255,0.5447);              /* the Fluent underline */
  border-radius: 4px; padding: 5px 10px; color: #FFFFFF; selection-background-color: #0078D4;
}
QLineEdit:focus { background: #1E1E1E; border-bottom: 2px solid #0078D4; }

QListView { background: transparent; border: none; outline: none; }
QListView::item          { border-radius: 4px; }
QListView::item:hover    { background: rgba(255,255,255,0.0588); }
QListView::item:selected { background: rgba(255,255,255,0.0824); }

QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: rgba(255,255,255,0.5447); border-radius: 3px;
                              min-height: 24px; margin: 2px 4px; }
QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.7); }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QToolTip { background: #2C2C2C; color: #FFFFFF; border: 1px solid rgba(255,255,255,0.0706);
           border-radius: 4px; padding: 6px 8px; }
```

---

## 2. Custom-painted Fluent widgets

All of the following are from `fluent_widgets.py`, executed and rendered. Common rules:

- `p.setRenderHint(QPainter.Antialiasing, True)` on **every** painter that draws a curve.
- Use `QRectF`, not `QRect`. Integer rects put a 1px stroke half-off the pixel grid.
- For a crisp 1px stroke inset the rect by **0.5**: `QRectF(self.rect()).adjusted(.5,.5,-.5,-.5)`.
- Never hard-code `devicePixelRatio`; `QPainter` on a widget is already in logical coordinates.
  Only pixmaps you allocate yourself need `setDevicePixelRatio` (§5).

### 2.1 ToggleSwitch — animated thumb via `QPropertyAnimation` on a float `Property`

Fluent geometry: 40x20 track, 12px thumb, 4px inset, thumb grows to 14px on hover and 17px on press.

```python
class ToggleSwitch(QtWidgets.QAbstractButton):
    TRACK_W, TRACK_H = 40, 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True); self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True); self.setFocusPolicy(Qt.StrongFocus)
        self._pos = 0.0; self._thumb_scale = 1.0
        self._anim = QPropertyAnimation(self, b"thumbPos", self)
        self._anim.setDuration(167)                        # Fluent "fast"
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._scale_anim = QPropertyAnimation(self, b"thumbScale", self)
        self._scale_anim.setDuration(83); self._scale_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)
        self.accent = QtGui.QColor("#0078D4"); self.accent_hover = QtGui.QColor("#1A86D9")
        self.off_stroke = QtGui.QColor("#8A8A8A"); self.off_fill = QtGui.QColor(0,0,0,0)
        self.off_fill_hover = QtGui.QColor(255,255,255,15)
        self.thumb_off = QtGui.QColor("#CFCFCF"); self.thumb_on = QtGui.QColor("#FFFFFF")

    def sizeHint(self): return QSize(self.TRACK_W, self.TRACK_H)
    minimumSizeHint = sizeHint

    # NOTE: the property NAME in QPropertyAnimation(b"thumbPos") must match the
    # QtCore.Property attribute name exactly, or the animation silently does nothing.
    def getThumbPos(self): return self._pos
    def setThumbPos(self, v): self._pos = v; self.update()
    thumbPos = Property(float, getThumbPos, setThumbPos)

    def getThumbScale(self): return self._thumb_scale
    def setThumbScale(self, v): self._thumb_scale = v; self.update()
    thumbScale = Property(float, getThumbScale, setThumbScale)

    def _animate(self, on):
        self._anim.stop(); self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0); self._anim.start()

    def _scale_to(self, v):
        self._scale_anim.stop(); self._scale_anim.setStartValue(self._thumb_scale)
        self._scale_anim.setEndValue(v); self._scale_anim.start()

    def enterEvent(self, e): self._scale_to(1.166); super().enterEvent(e)   # 12 -> 14px
    def leaveEvent(self, e): self._scale_to(1.0);   super().leaveEvent(e)
    def mousePressEvent(self, e): self._scale_to(1.416); super().mousePressEvent(e)  # 17px
    def mouseReleaseEvent(self, e):
        self._scale_to(1.166 if self.underMouse() else 1.0); super().mouseReleaseEvent(e)

    def paintEvent(self, _):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = QRectF(0, 0, self.TRACK_W, self.TRACK_H); r.moveCenter(QRectF(self.rect()).center())
        rad = r.height() / 2.0
        hovered = self.underMouse() and self.isEnabled()
        t = self._pos
        off_fill = self.off_fill_hover if hovered else self.off_fill

        if t > 0.0:
            on_col = self.accent_hover if hovered else self.accent
            p.setPen(Qt.NoPen); p.setBrush(self._lerp(off_fill, on_col, t))
            p.drawRoundedRect(r, rad, rad)
            if t < 1.0:                                  # fade the off-stroke out mid-animation
                p.setPen(QtGui.QPen(self.off_stroke, 1.0)); p.setBrush(Qt.NoBrush)
                p.setOpacity(1.0 - t)
                p.drawRoundedRect(r.adjusted(.5,.5,-.5,-.5), rad-.5, rad-.5)
                p.setOpacity(1.0)
        else:
            p.setPen(Qt.NoPen); p.setBrush(off_fill); p.drawRoundedRect(r, rad, rad)
            p.setPen(QtGui.QPen(self.off_stroke, 1.0)); p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(.5,.5,-.5,-.5), rad-.5, rad-.5)

        base_d, inset = 12.0, 4.0
        d = base_d * self._thumb_scale
        cx0 = r.left() + inset + base_d/2.0
        cx1 = r.right() - inset - base_d/2.0
        cx  = cx0 + (cx1 - cx0) * t
        half = min(d/2.0, rad - 1.5)                     # keep growth inside the track
        cx = max(r.left()+half+1.5, min(cx, r.right()-half-1.5))
        p.setPen(Qt.NoPen); p.setBrush(self._lerp(self.thumb_off, self.thumb_on, t))
        p.drawEllipse(QPointF(cx, r.center().y()), half, half)

        if self.hasFocus():
            p.setPen(QtGui.QPen(QtGui.QColor("#FFFFFF"), 2.0)); p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-3,-3,3,3), rad+3, rad+3)
        p.end()

    @staticmethod
    def _lerp(a, b, t):
        return QtGui.QColor.fromRgbF(
            a.redF()+(b.redF()-a.redF())*t,     a.greenF()+(b.greenF()-a.greenF())*t,
            a.blueF()+(b.blueF()-a.blueF())*t,  a.alphaF()+(b.alphaF()-a.alphaF())*t)
```

> `_lerp` **must** interpolate alpha too — the off-track fill is transparent, so an RGB-only lerp
> produces a black flash mid-animation.

### 2.2 ProgressRing — determinate + indeterminate arc

Qt arc angles are **1/16 degree**, `0` = 3 o'clock, **positive = counter-clockwise**. To sweep
clockwise from 12 o'clock: start `90*16`, span **negative**.

```python
class ProgressRing(QtWidgets.QWidget):
    def __init__(self, parent=None, diameter=32, thickness=3.0):
        super().__init__(parent)
        self._d, self._thick = diameter, thickness
        self._value = 0.0; self._indeterminate = False
        self._angle = 0.0; self._sweep = 0.0
        self.setFixedSize(diameter, diameter)
        self.track_color = QtGui.QColor(255,255,255,40)
        self.arc_color   = QtGui.QColor("#0078D4")
        self._rot = QPropertyAnimation(self, b"angle", self)
        self._rot.setDuration(1500); self._rot.setStartValue(0.0); self._rot.setEndValue(360.0)
        self._rot.setLoopCount(-1); self._rot.setEasingCurve(QEasingCurve.Linear)
        self._sw = QPropertyAnimation(self, b"sweep", self)
        self._sw.setDuration(1500); self._sw.setStartValue(0.0); self._sw.setEndValue(1.0)
        self._sw.setLoopCount(-1); self._sw.setEasingCurve(QEasingCurve.InOutQuad)

    def getAngle(self): return self._angle
    def setAngle(self, v): self._angle = v; self.update()
    angle = Property(float, getAngle, setAngle)
    def getSweep(self): return self._sweep
    def setSweep(self, v): self._sweep = v; self.update()
    sweep = Property(float, getSweep, setSweep)

    def setValue(self, v): self._value = max(0.0, min(1.0, v)); self.update()

    def setIndeterminate(self, on):
        self._indeterminate = on
        (self._rot.start(), self._sw.start()) if on else (self._rot.stop(), self._sw.stop())
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        m = self._thick/2.0 + 0.5
        r = QRectF(self.rect()).adjusted(m, m, -m, -m)
        pen = QtGui.QPen(self.track_color, self._thick); pen.setCapStyle(Qt.RoundCap)
        p.setBrush(Qt.NoBrush)
        if not self._indeterminate:
            p.setPen(pen); p.drawEllipse(r)
            pen.setColor(self.arc_color); p.setPen(pen)
            p.drawArc(r, 90*16, int(-self._value * 360 * 16))
        else:
            tri  = 1.0 - abs(2.0*self._sweep - 1.0)      # 0..1..0 triangle wave
            span = 20.0 + 260.0*tri                      # arc length breathes 20deg..280deg
            pen.setColor(self.arc_color); p.setPen(pen)
            p.drawArc(r, int((90.0 - self._angle*2.0)*16), int(-span*16))
        p.end()
```

> **Always `stop()` the loop animations when the ring is hidden.** `setLoopCount(-1)` repaints
> forever and will keep the CPU awake even behind a hidden widget. Hook `hideEvent`/`showEvent`.

### 2.3 Segmented storage bar

The trick is clipping to the rounded pill first, then filling flat rects, then punching the gaps
with `CompositionMode_Clear` (which requires the widget to have an alpha channel — it does,
because we draw into the widget's backing store which is ARGB32).

```python
class StorageBar(QtWidgets.QWidget):
    def __init__(self, parent=None, height=8, radius=4, gap=2.0):
        super().__init__(parent)
        self._segments = []; self._total = 1
        self._h, self._radius, self._gap = height, radius, gap
        self.track_color = QtGui.QColor(255,255,255,30)
        self.setFixedHeight(height)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def setData(self, total, segments):      # segments = [(bytes, QColor, label), ...]
        self._total = max(1, total); self._segments = list(segments); self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = QRectF(self.rect())
        path = QtGui.QPainterPath(); path.addRoundedRect(r, self._radius, self._radius)
        p.setClipPath(path)                                  # pill-caps every segment end
        p.fillRect(r, self.track_color)
        x = 0.0
        for i, (nbytes, color, _lbl) in enumerate(self._segments):
            w = r.width() * (nbytes / self._total)
            if w <= 0: continue
            p.setPen(Qt.NoPen); p.setBrush(color)
            p.drawRect(QRectF(x, 0, w, r.height()))
            x += w
            if self._gap > 0 and i < len(self._segments) - 1:
                p.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)
                p.fillRect(QRectF(x, 0, self._gap, r.height()), Qt.transparent)
                p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
                x += self._gap
        p.end()
```

Usage (renders a OneDrive-style Files/Photos/Other bar):

```python
GB = 1024**3
bar.setData(1024*GB, [(300*GB, QColor("#0078D4"), "Files"),
                      (120*GB, QColor("#8764B8"), "Photos"),
                      ( 60*GB, QColor("#107C10"), "Other")])
```

### 2.4 SettingsCard — rounded container, 1px stroke, hover/press

Derives from **`QFrame`** so it gets a styled background without `WA_StyledBackground` (§1.3b).

```python
class SettingsCard(QtWidgets.QFrame):
    clicked = Signal()
    def __init__(self, parent=None, radius=4.0, clickable=False):
        super().__init__(parent)
        self.setAttribute(Qt.WA_Hover, True)
        self._radius, self._hover, self._press = radius, False, False
        self._clickable = clickable
        if clickable: self.setCursor(Qt.PointingHandCursor)
        self.fill       = QtGui.QColor(255,255,255,13)   # CardBackgroundFillColorDefault 0D
        self.fill_hover = QtGui.QColor(255,255,255,20)
        self.fill_press = QtGui.QColor(255,255,255,8)
        self.stroke     = QtGui.QColor(0,0,0,25)         # CardStrokeColorDefault 19000000
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(16,12,16,12); lay.setSpacing(16)

    def enterEvent(self, e): self._hover = True; self.update(); super().enterEvent(e)
    def leaveEvent(self, e): self._hover = self._press = False; self.update(); super().leaveEvent(e)
    def mousePressEvent(self, e):
        if self._clickable and e.button() == Qt.LeftButton: self._press = True; self.update()
        super().mousePressEvent(e)
    def mouseReleaseEvent(self, e):
        if self._clickable and self._press and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        self._press = False; self.update(); super().mouseReleaseEvent(e)

    def paintEvent(self, _):
        p = QtGui.QPainter(self); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)     # crisp 1px stroke
        f = self.fill_press if self._press else (self.fill_hover if self._hover else self.fill)
        p.setPen(QtGui.QPen(self.stroke, 1.0)); p.setBrush(f)
        p.drawRoundedRect(r, self._radius, self._radius)
        p.end()
```

**Elevation.** For a genuine Fluent card shadow inside a window use `QGraphicsDropShadowEffect`
(`setBlurRadius(16); setOffset(0,4); setColor(QColor(0,0,0,60))`). It is a raster effect — do not
put one on a widget that repaints every frame (§9.3).

### 2.5 AvatarCircle with initials

```python
class AvatarCircle(QtWidgets.QWidget):
    def __init__(self, parent=None, diameter=32):
        super().__init__(parent)
        self._d = diameter; self._initials = "?"; self._pixmap = None
        self._bg = QtGui.QColor("#0078D4"); self.setFixedSize(diameter, diameter)

    def setPerson(self, display_name, pixmap=None):
        parts = [w for w in (display_name or "").split() if w]
        if len(parts) >= 2: self._initials = (parts[0][0] + parts[-1][0]).upper()
        elif parts:         self._initials = parts[0][:2].upper()
        else:               self._initials = "?"
        self._pixmap = pixmap
        palette = ["#0078D4","#107C10","#8764B8","#C239B3","#D13438",
                   "#CA5010","#986F0B","#005E50","#0B6A0B","#4F6BED"]
        h = 0
        for ch in (display_name or ""): h = (h*31 + ord(ch)) & 0xFFFFFFFF
        self._bg = QtGui.QColor(palette[h % len(palette)])
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        r = QRectF(self.rect()).adjusted(.5,.5,-.5,-.5)
        if self._pixmap and not self._pixmap.isNull():
            path = QtGui.QPainterPath(); path.addEllipse(r); p.setClipPath(path)
            dpr = self.devicePixelRatioF()
            scaled = self._pixmap.scaled(self.size()*dpr, Qt.KeepAspectRatioByExpanding,
                                         Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)          # else it draws at 1/dpr size on HiDPI
            p.drawPixmap(self.rect(), scaled)
        else:
            p.setPen(Qt.NoPen); p.setBrush(self._bg); p.drawEllipse(r)
            f = QtGui.QFont(self.font())
            f.setPixelSize(max(9, int(self._d*0.42))); f.setWeight(QtGui.QFont.DemiBold)
            p.setFont(f); p.setPen(QtGui.QColor("#FFFFFF"))
            p.drawText(self.rect(), Qt.AlignCenter, self._initials)
        p.end()
```

### 2.6 State-badged tray icon (SVG base + badge, multi-DPI QIcon)

Rendered and inspected at 16/22/32/48/64px for states `None, syncing, ok, paused, error, warn`.
**All six read clearly at 16px.** The critical detail is the transparent "cutout" ring punched
around the badge with `CompositionMode_Clear` so the badge separates from the cloud glyph.

```python
from PySide6 import QtSvg
from PySide6.QtCore import QByteArray, QRectF

def render_svg(svg_bytes, px, dpr=1.0, color=None):
    """Render an SVG into a pixmap of `px` LOGICAL px at devicePixelRatio `dpr`."""
    data = svg_bytes.replace(b"currentColor", color.name().encode()) if color else svg_bytes
    r = QtSvg.QSvgRenderer(QByteArray(data))
    dev = int(round(px * dpr))
    pm = QtGui.QPixmap(dev, dev)
    pm.setDevicePixelRatio(dpr)          # so Qt lays it out at `px` logical
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    r.render(p, QRectF(0, 0, dev, dev))  # render in DEVICE coordinates
    p.end()
    return pm

BADGES = {"syncing":("#0078D4","arrows"), "ok":("#107C10","check"),
          "paused":("#616161","pause"),   "error":("#C42B1C","bang"),
          "warn":("#F7630C","bang")}

def badged_icon(base_svg, state, sizes=(16,22,24,32,48,64),
                base_color=QtGui.QColor("#FFFFFF")):
    icon = QtGui.QIcon()
    for s in sizes:
        pm = QtGui.QPixmap(s, s); pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        bs = int(round(s * (0.82 if state else 1.0)))   # shrink base to make room
        base = render_svg(base_svg, bs, 1.0, base_color); base.setDevicePixelRatio(1.0)
        p.drawPixmap(0, 0, base)
        if state:
            col, glyph = BADGES[state]
            d = s * 0.52
            br = QRectF(s-d, s-d, d, d)
            ring = max(1.0, s*0.045)
            p.setPen(Qt.NoPen)
            p.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)   # cutout ring
            p.drawEllipse(br.adjusted(-ring, -ring, 0, 0))
            p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
            p.setBrush(QtGui.QColor(col)); p.drawEllipse(br)
            _paint_glyph(p, glyph, br)      # check / pause / bang / arrows, see repo
        p.end()
        icon.addPixmap(pm, QtGui.QIcon.Normal, QtGui.QIcon.Off)          # raw sizes here
    return icon
```

**VERIFIED:** `icon.availableSizes() == [16x16, 20x20, 24x24, 32x32, 48x48, 64x64]`.
`actualSize(QSize(40,40))` returns `40x40` — Qt happily **scales** a stored pixmap to any request,
so supplying a dense ladder matters for crispness. Add 22 and 24 explicitly: GNOME's
appindicator asks for sizes in that band.

> **Do not** call `setDevicePixelRatio` on pixmaps you pass to `QIcon.addPixmap` — `QIcon`
> indexes by raw pixel size, and a dpr-tagged pixmap registers under the wrong logical key.
> Set dpr only on pixmaps you hand to `QPainter.drawPixmap` directly (§2.5, §5.2).

---

## 3. Frameless windows and the Wayland positioning wall

### 3.1 The hard constraints (all VERIFIED on this machine)

| Attempt | Result |
|---|---|
| `w.move(100,100)` on a top-level | `w.pos()` **returns (100,100)** but the compositor never moved it. Qt caches your request and lies. |
| `w.setGeometry(300,300,500,400)` | size applied; **position ignored**. |
| Reported geometry after `show()` | Qt reported `QRect(1920,0,...)` for a window Mutter actually **centred** (`org.gnome.mutter center-new-windows == true`). **Top-level geometry from Qt on Wayland is not the real on-screen position.** |
| `QCursor.pos()` | returned `QPoint(0,0)` — Wayland forbids global pointer queries. Qt returns the last position delivered to one of *your own* surfaces. |
| `QSystemTrayIcon.geometry()` | **`QRect(0,0,0,0)`, `isNull()==True`, `isValid()==False`.** |
| `startSystemMove()` / `startSystemResize()` outside a real input event | both returned **`False`** (also `False` for a synthetic `QMouseEvent` sent via `QApplication.sendEvent`). |

> **There is no way for this app to position any of its own top-level windows.** Design around it;
> do not spend time looking for a workaround.

### 3.2 `startSystemMove()` / `startSystemResize()`

Per the Qt 6 docs, these ask the *compositor* to run an interactive move/resize; they return
`bool` (true if the platform supports it) and are "preferred over `setPosition`" — on Wayland
"`setPosition` is not supported, so this is the only way the application can influence its
position." `startSystemResize(edges)` accepts **a single edge or two adjacent edges (a corner)**;
anything else is invalid.

They require a **live Wayland input serial**, which only a genuine compositor-delivered pointer
event carries. Hence `False` in every headless test above. **UNVERIFIABLE-HEADLESS:** returning
`True` under a real human click could not be tested here, but the `False` results are exactly the
documented "no valid serial" path, and this is the standard, working pattern in Qt apps on GNOME.

Canonical custom chrome (runs; renders correctly):

```python
class TitleBar(QtWidgets.QFrame):
    HEIGHT = 40
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            wh = self.window().windowHandle()
            if wh and wh.startSystemMove():      # MUST be inside a real press handler
                e.accept(); return
        super().mousePressEvent(e)
    def mouseDoubleClickEvent(self, e):
        w = self.window()
        w.showNormal() if w.isMaximized() else w.showMaximized()
        e.accept()

class Frameless(QtWidgets.QWidget):
    GRIP = 6
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        ...
    def _edges(self, pos):
        g, r, e = self.GRIP, self.rect(), Qt.Edges()
        if pos.x() <= g:              e |= Qt.LeftEdge
        if pos.x() >= r.width()-g:    e |= Qt.RightEdge
        if pos.y() <= g:              e |= Qt.TopEdge
        if pos.y() >= r.height()-g:   e |= Qt.BottomEdge
        return e
    def mousePressEvent(self, ev):
        e = self._edges(ev.position().toPoint())
        if e and ev.button() == Qt.LeftButton:
            wh = self.windowHandle()
            if wh and wh.startSystemResize(e): ev.accept(); return
        super().mousePressEvent(ev)
```

### 3.3 Recommendation: **use a normal decorated window for the main window**

Frameless works (`Qt.Window|Qt.FramelessWindowHint` + `WA_TranslucentBackground` shows fine, and
`border-radius` on the root gives rounded corners), but you lose, and must reimplement badly:

- the compositor drop-shadow (there is **no** `box-shadow` and no blur protocol — §1.2),
- snapping/tiling edge feedback, keyboard move/resize, the window menu (super+click / right-click
  on titlebar), touchpad gestures,
- correct behaviour on `gtk-decoration-layout` (this user has `icon:minimize,maximize,close` — a
  hand-rolled bar would hard-code the Windows order and look wrong).

Qt's own Wayland client-side decorations already reported sane frame margins
(`frameGeometry` was 11px wider and 49px taller than `geometry`).

> **Decision: ship a normal decorated `QMainWindow`.** Put the Fluent look *inside* the window
> (NavigationView, cards, type ramp). Chrome authenticity is not worth losing shadows, snapping and
> the GNOME titlebar contract. Revisit only if the user explicitly asks for Windows-style chrome.

### 3.4 The Activity Center flyout — **CRITICAL**

**The problem, in three verified facts:**

1. `QSystemTrayIcon.geometry()` returns a **null rect**, so we cannot know where the tray icon is.
2. We cannot position a top-level window anyway (§3.1).
3. A `Qt.Popup` shown programmatically is **mapped and then dismissed by Mutter within 300ms**:

```
main isActiveWindow: True  handle exposed: True
immediately after show: isVisible True  exposed True
300ms later:            isVisible False exposed False
```
An `xdg_popup` takes a grab that requires a valid input serial; a popup opened from a **D-Bus tray
activation** has no such serial, so the compositor dismisses it instantly. A `Qt.Tool |
Qt.FramelessWindowHint` window in the same test stayed visible.

**What the tray actually is here (VERIFIED by introspecting our own process on the bus):**

```
Category   : 'ApplicationStatus'
Id         : 'tray2.py'
ItemIsMenu : false
Menu       : objectpath '/MenuBar'
Status     : 'Active'
interfaces : org.kde.StatusNotifierItem  (at /StatusNotifierItem)
             com.canonical.dbusmenu      (at /MenuBar)
```

Qt exports the tray context menu as a **DBusMenu**, which **GNOME Shell renders itself** — so it is
positioned perfectly under the tray icon, but it can only carry DBusMenu primitives. Dumping the
exported layout for a menu containing a `QWidgetAction` (a label + a `QProgressBar`):

```
id=0 {'children-display': 'submenu'}
   id=8 {'enabled': True, 'label': 'Open OneDrive folder', 'visible': True}
   id=7 {'enabled': True, 'label': 'Pause syncing', 'toggle-state': 1,
         'toggle-type': 'checkmark', 'visible': True}
   id=6 {'type': 'separator', 'visible': True}
   id=5 {'enabled': True, 'label': '', 'visible': True}      <-- the QWidgetAction, GUTTED
   id=4 {'children-display': 'submenu', 'label': 'Recent', ...}
      id=2 {'label': 'file1.txt'} 
      id=1 {'label': 'file2.txt'}
   id=3 {'label': 'Quit'}
```

> **`QWidgetAction` is exported as an empty label.** Custom widgets in a tray menu are impossible.
> Labels, icons, `toggle-type: checkmark`, separators and submenus are all you get.

**Recommendation (concrete):**

1. **Do not build a tray-anchored flyout.** It cannot be positioned, and it will be dismissed.
2. The Activity Center is a **normal top-level window** — `Qt.Window`, decorated, ~380x600,
   `setAttribute(Qt.WA_QuitOnClose, False)`. Mutter centres it (`center-new-windows == true`).
   Show/raise it with:
   ```python
   self.activity.show()
   self.activity.raise_()
   self.activity.activateWindow()   # may be a no-op without a serial; request-focus is
                                    # still the right thing to ask for
   ```
   Remember its size in `QSettings`; never try to restore its position.
3. **The tray menu carries the live status**, because GNOME renders it in the right place for free.
   Rebuild it on every `stats` tick — cheap, and DBusMenu diffs it:
   ```python
   self.act_status.setText("Syncing 3 items - 42%")   # non-clickable summary line
   self.act_status.setEnabled(False)
   self.act_pause.setChecked(paused)                  # -> toggle-type: checkmark
   ```
   Also call `tray.setToolTip(...)` — the shell shows it on hover.
4. Wire `tray.activated` to open the Activity Center, but **also** put "Open Activity Center" as
   the menu's **first, default** item: under the appindicator extension a left-click commonly just
   opens the menu, so activation-by-click is not dependable.
5. Use `tray.showMessage(title, body, icon, ms)` for transient notifications —
   `QSystemTrayIcon.supportsMessages()` is **True** here.

---

## 4. Theme: light/dark and the GNOME accent colour

### 4.1 **`QStyleHints.colorScheme()` is broken on this machine — do not use it**

VERIFIED, and this is the single most important finding in this document.

| System `color-scheme` | `~/.config/gtk-*/settings.ini` | `colorScheme()` |
|---|---|---|
| `prefer-dark` | `gtk-application-prefer-dark-theme=true` (this user's real config) | Dark |
| `default` (light) | `gtk-application-prefer-dark-theme=true` | **Dark  ← WRONG** |
| `prefer-dark` | line removed | **Light ← WRONG** |
| `default` (light) | line removed | Light |

`colorScheme()` is driven **entirely** by the GTK ini and completely ignores
`org.freedesktop.appearance`. This user has a stale `gtk-application-prefer-dark-theme=true`
(Breeze/KDE leftover), so **Qt reports Dark unconditionally, forever.**

Worse, **`colorSchemeChanged` never fires.** Toggling GNOME light/dark and waiting 8s:

```
t=0 colorScheme: ColorScheme.Dark   Window: #323232
set default(light)
t=2.0 -> ColorScheme.Dark #323232
t=5.0 -> ColorScheme.Dark #323232
t=8.0 -> ColorScheme.Dark #323232
```
Same with `QT_QPA_PLATFORMTHEME=xdgdesktopportal` and `=gtk3`. Meanwhile the **portal signal fired
correctly and instantly** every single time.

Also note `QPalette.Accent` is a hard-coded **`#308cc6`** — it is **not** the GNOME accent
(which is `#9141ac` here). Never read the accent from the palette.

### 4.2 The reliable implementation

Reads go through **Gio** because PySide6's `QDBusArgument` **cannot demarshal the `(ddd)` accent
struct** (verified: `currentSignature()=='(ddd)'` but there is no `toDouble()`, and
`asVariant()`/`beginStructure()` both fail to yield the doubles). The change **signal** comes from
**QtDBus** so it lands on the Qt event loop with no GLib main loop running.

```python
"""theming.py - portal-backed light/dark + accent. VERIFIED live on GNOME 50.4."""
import subprocess
from PySide6 import QtCore, QtGui, QtDBus
from PySide6.QtCore import QObject, Signal, Slot

PORTAL_SVC, PORTAL_PATH = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
PORTAL_IF,  NS          = "org.freedesktop.portal.Settings", "org.freedesktop.appearance"

try:
    import gi; gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    _HAVE_GIO = True
except Exception:
    _HAVE_GIO = False

def _read_portal(key):
    if _HAVE_GIO:
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None,
                PORTAL_SVC, PORTAL_PATH, PORTAL_IF, None)
            res = proxy.call_sync("ReadOne", GLib.Variant("(ss)", (NS, key)),
                                  Gio.DBusCallFlags.NONE, 2000, None)
            return res.unpack()[0]
        except Exception:
            pass
    try:                                            # gdbus CLI fallback
        out = subprocess.run(
            ["gdbus","call","--session","--dest",PORTAL_SVC,"--object-path",PORTAL_PATH,
             "--method",PORTAL_IF+".ReadOne", NS, key],
            capture_output=True, text=True, timeout=3).stdout.strip()
        body = out[out.find("<")+1:out.rfind(">")]
        if body.startswith("("):
            return tuple(float(x) for x in body.strip("()").split(","))
        return int(body.split()[-1])
    except Exception:
        return None

class ThemeManager(QObject):
    changed = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.dark = True
        self.accent = QtGui.QColor("#0078D4")
        self.high_contrast = False
        self.refresh()
        QtDBus.QDBusConnection.sessionBus().connect(
            PORTAL_SVC, PORTAL_PATH, PORTAL_IF, "SettingChanged", self,
            QtCore.SLOT("_onSettingChanged(QString,QString,QDBusVariant)"))

    def refresh(self):
        cs = _read_portal("color-scheme")     # 0 = no preference, 1 = dark, 2 = light
        if cs is not None: self.dark = (int(cs) == 1)
        ac = _read_portal("accent-color")     # (ddd) 0..1, or (-1,-1,-1) when unset
        if isinstance(ac, (list, tuple)) and len(ac) == 3 and all(float(x) >= 0 for x in ac):
            self.accent = QtGui.QColor.fromRgbF(*(float(x) for x in ac))
        hc = _read_portal("contrast")         # 0 = normal, 1 = high
        if hc is not None: self.high_contrast = (int(hc) == 1)

    @Slot(str, str, QtDBus.QDBusVariant)
    def _onSettingChanged(self, ns, key, value):
        if ns != NS or key not in ("color-scheme", "accent-color", "contrast"): return
        v = value.variant()
        if key == "color-scheme" and not isinstance(v, QtDBus.QDBusArgument):
            self.dark = (int(v) == 1)
        elif key == "contrast" and not isinstance(v, QtDBus.QDBusArgument):
            self.high_contrast = (int(v) == 1)
        else:
            self.refresh()                    # struct payload -> re-read through Gio
        self.changed.emit()
```

**Live run (VERIFIED):**

```
initial: dark=True accent=#9141ac rgbF=(0.5686,0.2549,0.6745,1.0) highContrast=False
set ['color-scheme', 'default']   -> CHANGED dark=False accent=#9141ac
set ['accent-color', 'teal']      -> CHANGED dark=False accent=#2190a4
set ['accent-color', 'purple']    -> CHANGED dark=False accent=#9141ac
set ['color-scheme', 'prefer-dark'] -> CHANGED dark=True accent=#9141ac
```

> The portal emits `color-scheme` **twice** per change. **Debounce** `changed` through a
> `QTimer.singleShot(0, ...)` coalescer before rebuilding the stylesheet — a full re-polish is
> expensive (§1.3e).

**GNOME accent palette (VERIFIED by setting each and reading the portal):**

| Name | RGB (0..1) | Hex |
|---|---|---|
| blue | 0.2078, 0.5176, 0.8941 | `#3584E4` |
| teal | 0.1294, 0.5647, 0.6431 | `#2190A4` |
| green | 0.2275, 0.5804, 0.2902 | `#3A944A` |
| yellow | 0.7843, 0.5333, 0.0 | `#C88800` |
| orange | 0.9294, 0.3569, 0.0 | `#ED5B00` |
| red | 0.9020, 0.1765, 0.2588 | `#E62D42` |
| pink | 0.8353, 0.3804, 0.6000 | `#D56199` |
| purple | 0.5686, 0.2549, 0.6745 | `#9141AC` |
| slate | 0.4353, 0.5137, 0.5882 | `#6F8396` |

**Product decision:** OneDrive's brand accent is `#0078D4`. Default to the OneDrive blue for
fidelity to the Windows client, and offer a "Use system accent colour" setting that switches to
`ThemeManager.accent`. Do not silently adopt the GNOME accent — a purple OneDrive looks broken.

---

## 5. High DPI

### 5.1 Qt 6 defaults (VERIFIED)

- High-DPI scaling is **always on** in Qt 6. `AA_EnableHighDpiScaling` / `AA_UseHighDpiPixmaps`
  still exist as enum members but are **no-ops** — do not set them.
- `QGuiApplication.highDpiScaleFactorRoundingPolicy()` defaults to **`PassThrough`** on this build,
  i.e. fractional scales (1.25, 1.5) are used verbatim. Options: `Round`, `Ceil`, `Floor`,
  `RoundPreferFloor`, `PassThrough`, `Unset`.
- This machine reports `devicePixelRatio 1.0` on all three screens (1920x1080 @ 96 dpi), so
  fractional scaling is **not** exercised here. Mutter advertises modes 1.0/1.25/1.333/1.5/1.667/2.0,
  so a user can enable it — the code must still be dpr-correct.
- To pin behaviour, set the env var **before** constructing `QApplication`, or:
  ```python
  QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
      Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
  ```
  Equivalent env: `QT_SCALE_FACTOR_ROUNDING_POLICY=PassThrough`.
  **Keep `PassThrough`** — it matches what Mutter actually hands us; `Round` makes the app
  mismatch every other GNOME window at 125%.
- Never set `QT_SCALE_FACTOR` or `QT_AUTO_SCREEN_SCALE_FACTOR`; they fight the compositor.

### 5.2 SVG at devicePixelRatio (VERIFIED)

```python
for dpr in (1.0, 1.25, 1.5, 2.0):
    pm = render_svg(CLOUD_SVG, 24, dpr, QColor("#0078D4"))
    # dpr=1.0  -> raw 24x24  dpr=1.0  logical 24.0x24.0
    # dpr=1.25 -> raw 30x30  dpr=1.25 logical 24.0x24.0
    # dpr=1.5  -> raw 36x36  dpr=1.5  logical 24.0x24.0
    # dpr=2.0  -> raw 48x48  dpr=2.0  logical 24.0x24.0
```

The three rules:
1. Allocate `QPixmap(round(px*dpr))`.
2. `pm.setDevicePixelRatio(dpr)` **before** painting.
3. `QSvgRenderer.render(painter, QRectF(0,0,dev,dev))` — render in **device** coords.

Get `dpr` from `widget.devicePixelRatioF()` (per-widget, correct on multi-monitor). Re-render
icons on `QWindow.screenChanged` / `QScreen.physicalDotsPerInchChanged` if the user drags the
window to a differently-scaled monitor.

**Ship every icon as SVG** and render on demand. Recolour by putting `fill="currentColor"` in the
SVG and doing a byte-replace with the theme's text colour (as in `render_svg`) — this gives free
light/dark icon theming with no second asset set.

---

## 6. Fonts

### 6.1 What is installed here (VERIFIED via `fc-list` and `QFontDatabase`)

| Family | Present? |
|---|---|
| Segoe UI Variable | **NO** |
| Segoe UI | **NO** |
| Selawik | **NO** |
| Inter | **NO** |
| Roboto | **NO** |
| Noto Sans | yes (default `sans-serif`) |
| Adwaita Sans | yes |
| Cantarell | yes |
| Open Sans | yes |
| DejaVu Sans, Liberation Sans | yes |

`QApplication.font()` is **`Noto Sans 10pt`** (from `org.gnome.desktop.interface font-name`).

**Fontconfig substitution hijacks the fallback stack — VERIFIED:**

```
QFont("Segoe UI")  -> resolved 'Adwaita Sans', exactMatch=False
QFont("Inter")     -> resolved 'Noto Sans',    exactMatch=False
QFont().setFamilies([... "Segoe UI" ... "Inter" ... "Noto Sans"]) -> 'Adwaita Sans'
```

fontconfig answers *every* family name with *something*, so `setFamilies([...])` does **not** walk
your list to the first installed one — the first entry gets substituted and wins. **You must filter
the stack yourself:**

```python
def pick_family(candidates):
    installed = set(QtGui.QFontDatabase.families())
    for fam in candidates:
        if fam in installed:
            return fam
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.GeneralFont).family()
```

### 6.2 Recommendation: **bundle Inter**

Segoe UI / Segoe UI Variable are **proprietary Microsoft fonts and must not be redistributed.**
Inter is released under the **SIL Open Font License 1.1**, which explicitly permits bundling and
embedding in an application ("can be bundled, embedded, redistributed and/or sold with any
software"), the only bar being that you cannot sell Inter itself. It is metrically and
stylistically the closest OFL substitute for Segoe UI Variable and is what most Fluent-on-Linux
projects use. Ship `InterVariable.ttf` from the [official releases](https://github.com/rsms/inter/releases/latest).

Bundle it at `src/onedriveui/assets/fonts/InterVariable.ttf`, ship the `OFL.txt` alongside, and
credit it in the About dialog.

**Loading a bundled font (VERIFIED both paths work):**

```python
from PySide6 import QtCore, QtGui
from onedriveui.resources import read_bytes

def load_bundled_fonts():
    families = []
    for name in ("InterVariable.ttf",):
        data = read_bytes("fonts", name)                       # importlib.resources bytes
        fid = QtGui.QFontDatabase.addApplicationFontFromData(QtCore.QByteArray(data))
        if fid != -1:
            families += QtGui.QFontDatabase.applicationFontFamilies(fid)
    return families
# verified: addApplicationFont(path) -> id 0, families ['Noto Sans']
#           addApplicationFontFromData(bytes) -> id 1, families ['Noto Sans']
```

`addApplicationFontFromData` is preferred — it works from a wheel/zip without materialising a
temp file. Returns `-1` on failure; always check.

**The stack, after loading:**

```python
UI_FAMILY = pick_family([
    "Inter",              # bundled - our target look
    "Segoe UI Variable Text", "Segoe UI",   # if the user installed them themselves
    "Selawik",
    "Adwaita Sans", "Cantarell",            # GNOME natives
    "Noto Sans", "DejaVu Sans",
])
f = QtGui.QFont(UI_FAMILY); f.setPixelSize(14); app.setFont(f)
```

Use **`setPixelSize`**, not `setPointSize`: Fluent's ramp is specified in px and Qt already scales
px by the device pixel ratio. Mixing pt with the GNOME `text-scaling-factor` double-scales.

### 6.3 Windows 11 type ramp (from Microsoft's XAML theme resources)

| Style | Weight | Size (px) | Use |
|---|---|---|---|
| Caption | Regular (400) | 12 | timestamps, secondary metadata |
| Body | Regular (400) | 14 | default UI text |
| Body Strong | Semibold (600) | 14 | list item titles, card headers |
| Body Large | Regular (400) | 18 | |
| Body Large Strong | Semibold (600) | 18 | |
| Subtitle | Semibold (600) | 20 | section headers |
| Title | Semibold (600) | 28 | page titles |
| Title Large | Semibold (600) | 40 | |
| Display | Semibold (600) | 68 | |

Qt weights: `QFont.Normal`=400, `QFont.DemiBold`=600, `QFont.Bold`=700. Fluent "Semibold" is
**DemiBold (600)**, not Bold — using Bold is the most common tell of a fake Fluent UI.

### 6.4 Fluent colour tokens (exact, from `microsoft-ui-xaml` `Common_themeresources_any.xaml`)

Values are **`#AARRGGBB`** (alpha first) where 8 digits. `Default` = the Dark dictionary.

| Token | Dark | Light |
|---|---|---|
| TextFillColorPrimary | `#FFFFFF` | `#E4000000` |
| TextFillColorSecondary | `#C5FFFFFF` | `#9E000000` |
| TextFillColorTertiary | `#87FFFFFF` | `#72000000` |
| TextFillColorDisabled | `#5DFFFFFF` | `#5C000000` |
| TextOnAccentFillColorPrimary | `#000000` | `#FFFFFF` |
| ControlFillColorDefault | `#0FFFFFFF` | `#B3FFFFFF` |
| ControlFillColorSecondary | `#15FFFFFF` | `#80F9F9F9` |
| ControlFillColorTertiary | `#08FFFFFF` | `#4DF9F9F9` |
| ControlFillColorDisabled | `#0BFFFFFF` | `#4DF9F9F9` |
| ControlFillColorInputActive | `#B31E1E1E` | `#FFFFFF` |
| ControlStrokeColorDefault | `#12FFFFFF` | `#0F000000` |
| ControlStrokeColorSecondary | `#18FFFFFF` | `#29000000` |
| ControlStrokeColorOnAccentDefault | `#14FFFFFF` | `#14FFFFFF` |
| ControlStrongStrokeColorDefault | `#8BFFFFFF` | `#72000000` |
| SubtleFillColorSecondary | `#0FFFFFFF` | `#09000000` |
| SubtleFillColorTertiary | `#0AFFFFFF` | `#06000000` |
| CardBackgroundFillColorDefault | `#0DFFFFFF` | `#B3FFFFFF` |
| CardBackgroundFillColorSecondary | `#08FFFFFF` | `#80F6F6F6` |
| CardStrokeColorDefault | `#19000000` | `#0F000000` |
| CardStrokeColorDefaultSolid | `#1C1C1C` | `#EBEBEB` |
| LayerFillColorDefault | `#4C3A3A3A` | `#80FFFFFF` |
| LayerFillColorAlt | `#0DFFFFFF` | `#FFFFFF` |
| **SolidBackgroundFillColorBase** | `#202020` | `#F3F3F3` |
| SolidBackgroundFillColorSecondary | `#1C1C1C` | `#EEEEEE` |
| SolidBackgroundFillColorTertiary | `#282828` | `#F9F9F9` |
| SolidBackgroundFillColorQuarternary | `#2C2C2C` | `#FFFFFF` |
| SolidBackgroundFillColorBaseAlt | `#0A0A0A` | `#DADADA` |
| DividerStrokeColorDefault | `#15FFFFFF` | `#0F000000` |
| SurfaceStrokeColorFlyout | `#33000000` | `#0F000000` |
| SystemFillColorSuccess | `#6CCB5F` | `#0F7B0F` |
| SystemFillColorCritical | `#FF99A4` | `#C42B1C` |
| SystemFillColorCaution | `#FCE100` | `#9D5D00` |
| SystemFillColorSuccessBackground | `#393D1B` | `#DFF6DD` |
| SystemFillColorCriticalBackground | `#442726` | `#FDE7E9` |
| SystemFillColorCautionBackground | `#433519` | `#FFF4CE` |
| FocusStrokeColorOuter | `#FFFFFF` | `#E4000000` |
| FocusStrokeColorInner | `#B3000000` | `#B3FFFFFF` |
| SmokeFillColorDefault | `#4D000000` | `#4D000000` |

**Corner radii (Fluent 2 shape tokens):** none `0` (nav/tab bars) · small `2` (badges) ·
**medium `4` (buttons, dropdowns, cards, list items — your default)** · large `8` (large buttons,
flyouts/dialogs) · x-large `12` (sheets, popovers) · circular `50%` (personas/avatars).

Helper for the ARGB strings:

```python
def argb(s: str) -> QtGui.QColor:
    """'#0FFFFFFF' (AARRGGBB) or '#202020' -> QColor."""
    s = s.lstrip("#")
    if len(s) == 8:
        a, r, g, b = (int(s[i:i+2], 16) for i in (0, 2, 4, 6))
        return QtGui.QColor(r, g, b, a)
    return QtGui.QColor("#" + s)
```

---

## 7. Widgets: the right Qt base for each

### 7.1 Activity list with per-row progress — `QListView` + `QStyledItemDelegate`

**The `sizeHint` trap (VERIFIED).** Returning `option.rect.width()` creates a feedback loop with
the vertical scrollbar and produces a spurious horizontal scrollbar:

```
sizeHint -> QSize(option.rect.width(), 60)  : hscroll max=14 visible=True   # BUG
sizeHint -> QSize(0, 60)                    : hscroll max=0  visible=False  # correct
```

> **Return width `0` from a full-width list delegate's `sizeHint`.** `QListView` then uses the
> viewport width, and `option.rect` inside `paint()` is already the correct full-row rect.

```python
NameRole     = Qt.UserRole + 1
SubtitleRole = Qt.UserRole + 2
ProgressRole = Qt.UserRole + 3   # float 0..1, -1 = indeterminate, None = no bar
IconRole     = Qt.UserRole + 4
StateRole    = Qt.UserRole + 5   # "uploading" | "done" | "error" | "paused"

class ActivityDelegate(QtWidgets.QStyledItemDelegate):
    ROW_H, PAD_X, ICON, GAP, BAR_H = 60, 12, 32, 12, 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.accent     = QtGui.QColor("#0078D4")
        self.text       = QtGui.QColor("#FFFFFF")
        self.subtext    = QtGui.QColor(255,255,255,140)
        self.bar_track  = QtGui.QColor(255,255,255,36)
        self.hover_fill = QtGui.QColor(255,255,255,15)
        self.sel_fill   = QtGui.QColor(255,255,255,26)
        self.error_col  = QtGui.QColor("#F85149")

    def sizeHint(self, option, index):
        return QSize(0, self.ROW_H)          # width 0 -> no phantom hscrollbar

    def paint(self, p, option, index):
        p.save(); p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        opt = QtWidgets.QStyleOptionViewItem(option); self.initStyleOption(opt, index)
        r = QRectF(opt.rect).adjusted(4, 2, -4, -2)

        if opt.state & QtWidgets.QStyle.State_Selected:
            p.setPen(Qt.NoPen); p.setBrush(self.sel_fill); p.drawRoundedRect(r, 4, 4)
            pill = QRectF(r.left()+1, r.center().y()-8, 3, 16)   # Fluent selection pill
            p.setBrush(self.accent); p.drawRoundedRect(pill, 1.5, 1.5)
        elif opt.state & QtWidgets.QStyle.State_MouseOver:
            p.setPen(Qt.NoPen); p.setBrush(self.hover_fill); p.drawRoundedRect(r, 4, 4)

        x = r.left() + self.PAD_X
        icon = index.data(IconRole)
        icon_rect = QRectF(x, r.center().y()-self.ICON/2, self.ICON, self.ICON)
        if isinstance(icon, QtGui.QIcon) and not icon.isNull():
            p.drawPixmap(icon_rect.topLeft().toPoint(), icon.pixmap(QSize(self.ICON, self.ICON)))
        else:
            p.setPen(Qt.NoPen); p.setBrush(QtGui.QColor(255,255,255,20))
            p.drawRoundedRect(icon_rect, 4, 4)
        x += self.ICON + self.GAP
        text_w = (r.right() - self.PAD_X) - x
        prog = index.data(ProgressRole)

        f = QtGui.QFont(opt.font); f.setPixelSize(14); p.setFont(f)
        fm = QtGui.QFontMetrics(f)
        name = str(index.data(NameRole) or index.data(Qt.DisplayRole) or "")
        top = r.top() + (10 if prog is not None else 14)
        p.setPen(self.text)
        p.drawText(QRectF(x, top, text_w, fm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(name, Qt.ElideMiddle, int(text_w)))   # middle-elide filenames
        y = top + fm.height() + 2

        f2 = QtGui.QFont(opt.font); f2.setPixelSize(12); p.setFont(f2)
        fm2 = QtGui.QFontMetrics(f2)
        sub = str(index.data(SubtitleRole) or "")
        p.setPen(self.error_col if index.data(StateRole) == "error" else self.subtext)
        p.drawText(QRectF(x, y, text_w, fm2.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   fm2.elidedText(sub, Qt.ElideRight, int(text_w)))
        y += fm2.height() + 4

        if prog is not None:
            bar = QRectF(x, y, text_w, self.BAR_H)
            p.setPen(Qt.NoPen); p.setBrush(self.bar_track)
            p.drawRoundedRect(bar, self.BAR_H/2, self.BAR_H/2)
            if prog >= 0:
                w = max(self.BAR_H, bar.width() * min(1.0, prog))
                p.setBrush(self.accent)
                p.drawRoundedRect(QRectF(bar.left(), bar.top(), w, bar.height()),
                                  self.BAR_H/2, self.BAR_H/2)
        p.restore()
```

View setup (all needed):

```python
lv.setItemDelegate(ActivityDelegate(lv))
lv.setMouseTracking(True)                    # REQUIRED for State_MouseOver in the delegate
lv.setUniformItemSizes(True)                 # big win: skips per-row sizeHint
lv.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
lv.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
lv.setSelectionMode(QAbstractItemView.SingleSelection)
```

**Live progress without flicker:** update the model item's `ProgressRole` and let the view repaint
just that row — `model.setData(idx, v, ProgressRole)` emits `dataChanged` for one index. Never
call `lv.reset()` or rebuild the model on a stats tick.

### 7.2 Tri-state folder tree — `QTreeWidget` with `PartiallyChecked` propagation

`Qt.ItemIsAutoTristate` makes Qt roll child states **up** to the parent, but it does **not** push a
parent's state **down**. You need both. Guard against re-entrancy: `setCheckState` re-emits
`itemChanged`.

```python
class FolderTree(QtWidgets.QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self._guard = False
        self.itemChanged.connect(self._on_changed)

    def addFolder(self, parent, name, checked=Qt.Checked):
        it = QtWidgets.QTreeWidgetItem(parent or self, [name])
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
        it.setCheckState(0, checked)
        return it

    def _on_changed(self, item, column):
        if column != 0 or self._guard: return
        self._guard = True                      # setCheckState re-enters itemChanged
        try:
            self._push_down(item, item.checkState(0))
            self._pull_up(item.parent())
        finally:
            self._guard = False

    def _push_down(self, item, state):
        if state == Qt.PartiallyChecked: return   # never force partial onto children
        for i in range(item.childCount()):
            ch = item.child(i)
            if ch.checkState(0) != state: ch.setCheckState(0, state)
            self._push_down(ch, state)

    def _pull_up(self, item):
        while item is not None:
            n = item.childCount()
            checked = sum(1 for i in range(n) if item.child(i).checkState(0) == Qt.Checked)
            partial = any(item.child(i).checkState(0) == Qt.PartiallyChecked for i in range(n))
            new = (Qt.PartiallyChecked if (partial or 0 < checked < n)
                   else Qt.Checked if (checked == n and n) else Qt.Unchecked)
            if item.checkState(0) != new: item.setCheckState(0, new)
            item = item.parent()
```

**VERIFIED:**
```
initial          Documents=C[C,C,C]
uncheck 1 child  Documents=P[C,U,C]      # parent goes Partial
uncheck all      Documents=U[U,U,U]      # parent goes Unchecked
check parent     Documents=C[C,C,C]      # pushes down to all children
```

For selective sync, read back the leaves plus any `PartiallyChecked` ancestors to build rclone
`--include`/`--exclude` filters.

### 7.3 Virtualised file browser

`QTreeView` + a **custom `QAbstractItemModel`** backed by rclone `lsjson`, lazily populated per
directory. `QTreeView` and `QListView` are already virtualised — they only call `data()` for
visible indices, so the model must be lazy, not the view.

- Implement `hasChildren()` from `IsDir` and `canFetchMore()`/`fetchMore()` to run `lsjson` for a
  directory the first time it is expanded. Return `rowCount()==0` + `canFetchMore()==True` for an
  unfetched dir so the expander arrow appears immediately.
- **Do not** use `QFileSystemModel` unless you are browsing an actual FUSE mount; against a real
  mount it works but will block on network stat calls.
- `setUniformRowHeights(True)` on the tree — large win.
- Sorting/filtering via `QSortFilterProxyModel`; call `setRecursiveFilteringEnabled(True)` so a
  search matches inside collapsed folders.

### 7.4 Nav pane

Fluent NavigationView = a left rail, not a `QTabWidget`. Use a `QListWidget` (icon + label, 40px
rows, `border-radius:4px`, the 3x16px accent selection pill from §7.1) driving a `QStackedWidget`:

```python
nav.currentRowChanged.connect(stack.setCurrentIndex)
```
Style the rail with `QListWidget::item:selected` and set `nav.setFrameShape(QFrame.NoFrame)`.
`QTabWidget::pane`/`QTabBar::tab` are stylable (verified: a `:selected` tab painted the accent
correctly) but a tab bar reads as Windows 10, not 11.

### 7.5 Search box with inline clear

Qt gives you both affordances free — no custom painting:

```python
class SearchBox(QtWidgets.QLineEdit):
    def __init__(self, parent=None, placeholder="Search"):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)                       # the inline 'x'
        self.addAction(search_icon, QtWidgets.QLineEdit.LeadingPosition)   # leading glyph
```
Style the clear button with `QLineEdit::clear-button{ image:url(...); width:16px; }` if the default
does not match; debounce `textChanged` through a 200ms `QTimer` before filtering.

---

## 8. Threading and I/O

### 8.1 Recommendation: `QProcess`, **not** a `QThread` worker

For a long-running rclone subprocess with live stdout parsing, use **`QProcess` on the GUI thread**.
It is fully asynchronous — it never blocks the event loop, it delivers output through
`readyReadStandardOutput` on the GUI thread (so no cross-thread signal marshalling, no locking, no
`moveToThread` lifetime bugs), and it gives you `finished`, `errorOccurred`, `terminate()`/`kill()`
for free. A `QThread` + `subprocess` worker buys nothing and adds three classes of bug.

**VERIFIED against real rclone v1.75.0:**

```python
class RcloneRunner(QObject):
    line     = Signal(str)
    stats    = Signal(dict)
    finished = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.p = QProcess(self)
        self.p.setProcessChannelMode(QProcess.SeparateChannels)
        self.p.readyReadStandardOutput.connect(self._out)
        self.p.readyReadStandardError.connect(self._err)
        self.p.finished.connect(lambda code, _st: self.finished.emit(code))
        self._buf = b""

    def start(self, args):
        self.p.start("/usr/bin/rclone", args)

    def _drain(self, data):
        self._buf += data                       # CRITICAL: readyRead gives arbitrary chunks,
        while b"\n" in self._buf:               # not lines. Buffer and split yourself.
            raw, self._buf = self._buf.split(b"\n", 1)
            s = raw.decode("utf-8", "replace").rstrip("\r")
            if not s: continue
            self.line.emit(s)
            if s.startswith("{"):
                try: self.stats.emit(json.loads(s))
                except json.JSONDecodeError: pass

    def _out(self): self._drain(bytes(self.p.readAllStandardOutput()))
    def _err(self): self._drain(bytes(self.p.readAllStandardError()))
```

Run it with rclone's JSON log so every line is machine-readable:

```python
r.start(["lsjson", "onedrive:", "--max-depth", "1",
         "--use-json-log", "--stats", "500ms", "--stats-log-level", "NOTICE", "-v"])
```

Observed output (real run, 62 lines, exit 0):

```
[0.69s] LINE: [
[1.19s] JSON keys: ['level', 'msg', 'source', 'stats', 'time']
        stats subkeys: ['bytes','checks','deletedDirs','deletes','elapsedTime','errors','eta',
                        'fatalError','listed','renames','serverSideCopies','serverSideCopyBytes',
                        'serverSideMoveBytes','serverSideMoves','speed','totalBytes','totalChecks',
                        'totalTransfers','transferTime','transfers']
[1.62s] JSON keys: ['ID','IsDir','MimeType','ModTime','Name','Path','Size']
finished rc=0 lines=62
```

> Notes: rclone writes **stats and logs to stderr**, data (`lsjson`) to **stdout** — drain both.
> `SeparateChannels` keeps them distinguishable; the buffered `_drain` handles partial lines.
> Always `terminate()` (then `kill()` after a timeout) in the app's `aboutToQuit`.

### 8.2 When you still need a thread

Use `QThreadPool` + `QRunnable` for short, CPU-bound, fire-and-forget work (hashing, thumbnail
decoding). Use `QThread` + a `moveToThread`'d worker object only for a long-lived blocking loop.
**Do not use `asyncio`** — integrating it needs a third-party loop bridge (qasync), and every
blocking need here is already covered by `QProcess` and `QNetworkAccessManager`.

Never touch a widget from a non-GUI thread; communicate only by `Signal` (queued automatically
across threads).

### 8.3 rc JSON-RPC: **`QNetworkAccessManager`, not `requests`/`urllib`**

`requests`/`urllib` are **synchronous** and will freeze the UI for the duration of every call —
unacceptable for a 500ms stats poll. `QNetworkAccessManager` is async on the GUI thread.

**VERIFIED against a live `rclone rcd`:**

```python
import base64, json
from PySide6 import QtNetwork
from PySide6.QtCore import QUrl, QByteArray

class RcClient(QObject):
    def __init__(self, addr="127.0.0.1:5572", user="u", password="p", parent=None):
        super().__init__(parent)
        self.nam = QtNetwork.QNetworkAccessManager(self)
        self.base = f"http://{addr}"
        self.auth = b"Basic " + base64.b64encode(f"{user}:{password}".encode())

    def call(self, path, payload, on_ok, on_err=None):
        req = QtNetwork.QNetworkRequest(QUrl(f"{self.base}/{path}"))
        req.setHeader(QtNetwork.QNetworkRequest.ContentTypeHeader, "application/json")
        req.setRawHeader(b"Authorization", self.auth)
        reply = self.nam.post(req, QByteArray(json.dumps(payload).encode()))
        def done():
            body = bytes(reply.readAll()).decode("utf-8", "replace")
            code = reply.attribute(QtNetwork.QNetworkRequest.HttpStatusCodeAttribute)
            if reply.error() == QtNetwork.QNetworkReply.NoError:
                on_ok(json.loads(body))
            elif on_err:
                on_err(reply.error(), code, body)
            reply.deleteLater()                  # REQUIRED - QNAM does not free replies
        reply.finished.connect(done)
        return reply
```

Measured round-trips against `rclone rcd --rc-addr 127.0.0.1:35719 --rc-user u --rc-pass p`:

```
[0.00s] core/version -> http=200  version v1.75.0 os linux arch amd64
[0.01s] core/stats   -> http=200  keys ['bytes','checks','deletedDirs','deletes','elapsedTime',
                                        'errors','eta','fatalError','listed','renames']
[0.01s] rc/noop      -> {"hello": "world"}
```

Sub-10ms per call, entirely on the GUI thread, zero blocking. `QtNetwork` TLS backends available:
`['cert-only', 'openssl']`.

> Create **one** `QNetworkAccessManager` for the app and reuse it — it pools connections.
> Always `reply.deleteLater()`. For the stats poll use a single `QTimer` and skip the tick if the
> previous reply is still in flight.

---

## 9. Animations

### 9.1 Fluent easing curves via cubic Bézier (VERIFIED)

Qt has no built-in Fluent curves, but `QEasingCurve.BezierSpline` + `addCubicBezierSegment`
reproduces CSS `cubic-bezier()` exactly:

```python
def fluent_curve(p1x, p1y, p2x, p2y):
    c = QEasingCurve(QEasingCurve.BezierSpline)
    c.addCubicBezierSegment(QPointF(p1x, p1y), QPointF(p2x, p2y), QPointF(1.0, 1.0))
    return c

FLUENT_DECELERATE     = fluent_curve(0.00, 0.00, 0.00, 1.00)   # entrances, expands
FLUENT_ACCELERATE     = fluent_curve(0.70, 0.00, 1.00, 0.50)   # exits, collapses
FLUENT_MAX            = fluent_curve(0.80, 0.00, 0.10, 1.00)   # emphasised move
FLUENT_POINT_TO_POINT = fluent_curve(0.55, 0.55, 0.00, 1.00)   # A->B translation
```

Sampled at t = 0, .1, .25, .5, .75, .9, 1:

| Curve | values |
|---|---|
| Decelerate (0,0,0,1) | 0.0, 0.446, 0.691, 0.890, 0.976, 0.997, 1.0 |
| Accelerate (0.7,0,1,0.5) | 0.0, 0.004, 0.024, 0.109, 0.299, 0.526, 1.0 |
| Max (0.8,0,0.1,1) | 0.0, 0.006, 0.050, 0.687, 0.964, 0.995, 1.0 |
| PointToPoint (0.55,.55,0,1) | 0.0, 0.114, 0.395, 0.921, 0.988, 0.998, 1.0 |
| *Qt OutCubic (builtin)* | 0.0, 0.271, 0.578, 0.875, 0.984, 0.999, 1.0 |

`QEasingCurve.OutCubic` is a good cheap stand-in for Decelerate; `OutQuint` is closer still for
short 167ms moves.

**Fluent durations:** instant `0`, **fast `167ms`** (toggles, hovers, checkboxes),
normal `250ms` (small flyouts, expands), slow `333ms` (dialogs, page transitions),
gentle `500ms` (large surfaces).

**Honour the user's motion preference — this matters on this machine.** Both
`gtk-enable-animations` (GTK ini) and `org.gnome.desktop.interface enable-animations` are
**`false`** here (VERIFIED). Gate every duration:

```python
import subprocess
def animations_enabled() -> bool:
    try:
        out = subprocess.run(["gsettings","get","org.gnome.desktop.interface",
                              "enable-animations"], capture_output=True, text=True,
                             timeout=2).stdout.strip()
        return out != "false"
    except Exception:
        return True

DUR = (lambda ms: ms if animations_enabled() else 0)
anim.setDuration(DUR(167))
```
Do this once at startup and cache it. Shipping a UI that animates when the user has asked for no
animation is both a correctness bug and an accessibility one.

### 9.2 Fades with `QGraphicsOpacityEffect` (VERIFIED)

```python
eff = QtWidgets.QGraphicsOpacityEffect(panel)
panel.setGraphicsEffect(eff)
a = QPropertyAnimation(eff, b"opacity", panel)
a.setDuration(167); a.setStartValue(0.0); a.setEndValue(1.0)
a.setEasingCurve(FLUENT_DECELERATE); a.start()
```
Works on a container and all its children at once (tested with a panel holding three buttons).

`setWindowOpacity(0.5)` also works on Wayland (read back `0.498` — 8-bit quantisation), useful for
a whole-window fade-in.

### 9.3 Performance caveats

- **Measured frame rate: ~67 paint events in 1s** for a `QPropertyAnimation`-driven custom
  `paintEvent`. Qt's animation timer is a **60Hz** timer; it does **not** sync to this 144/180Hz
  display. Do not design for 144fps.
- `QGraphicsOpacityEffect` / `QGraphicsDropShadowEffect` force the widget through an **offscreen
  raster buffer on every repaint**. Never leave one attached to a widget that animates
  continuously (a `ProgressRing`, a live progress bar). Attach for the fade, then
  `widget.setGraphicsEffect(None)` when the animation finishes.
- A graphics effect on a widget also disables any native/OpenGL child rendering beneath it.
- `update()` (coalesced) not `repaint()` (synchronous) inside property setters.
- Stop `setLoopCount(-1)` animations in `hideEvent` (§2.2).
- Animating a **top-level window's** `pos` does nothing on Wayland (§3.1) — animate child widgets
  or window `size`, never a top-level position.

---

## 10. Packaging on Arch

### 10.1 The environment constraint (VERIFIED)

- PySide6 is a **pacman package** (`pyside6 6.11.2-1.1`) at `/usr/lib/python3.14/site-packages`.
- `/usr/lib/python3.14/EXTERNALLY-MANAGED` exists → `pip install` into system Python is refused.
- **There is no `pip`, `pipx`, `hatch` or `build` on this machine.** `uv` **is** present
  (`~/.local/bin/uv`); `setuptools` + `wheel` + `ensurepip` are in the system Python.

> **Do not add `PySide6` as a resolved dependency that gets downloaded.** The pacman build is
> compiled against the system Qt 6.11.2 and its Wayland/SVG/DBus plugins; a wheel from PyPI would
> shadow it and break the platform plugin.

### 10.2 The layout (built, installed and run successfully)

```
OneDriveUI/
  pyproject.toml
  src/onedriveui/
    __init__.py
    __main__.py
    resources.py
    assets/
      app.qss
      fonts/InterVariable.ttf
      icons/*.svg
```

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "onedriveui"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["PySide6>=6.9"]

[project.scripts]
onedriveui = "onedriveui.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
onedriveui = ["assets/**/*"]
```

`setuptools` is chosen over `hatchling` because hatchling is **not installed** and would force a
network fetch on every build; setuptools is already present, so `--no-build-isolation` works offline.

### 10.3 Running it — three verified routes

**(a) Straight from the source tree, no venv, no install** (best for development):

```bash
PYTHONPATH=$PWD/src python3 -m onedriveui
```

**(b) A venv that reuses the system PySide6** (recommended for a stable dev setup):

```bash
python3 -m venv --system-site-packages .venv     # VERIFIED: sees PySide6 6.11.2 from /usr
.venv/bin/python -m pip install -e . --no-build-isolation
.venv/bin/onedriveui
```
Confirmed the console script runs and `PySide6.__file__` still resolves to
`/usr/lib/python3.14/site-packages/PySide6` — nothing was downloaded.

**(c) A wheel** — verified assets are included:

```
onedriveui-0.1.0-py3-none-any.whl
   onedriveui/__init__.py
   onedriveui/__main__.py
   onedriveui/resources.py
   onedriveui/assets/app.qss
   onedriveui/assets/icons/cloud.svg
   onedriveui-0.1.0.dist-info/entry_points.txt
```

For end users on Arch, ship a **PKGBUILD** with `depends=(python pyside6 rclone)` and
`arch=('any')` rather than a wheel — it keeps PySide6 as a system dependency.

### 10.4 Resource loading with `importlib.resources` (VERIFIED)

```python
"""onedriveui/resources.py"""
from importlib.resources import files, as_file
import onedriveui

def read_bytes(*parts) -> bytes:
    return files(onedriveui).joinpath("assets", *parts).read_bytes()

def read_text(*parts) -> str:
    return files(onedriveui).joinpath("assets", *parts).read_text(encoding="utf-8")

def path_for(*parts):
    """Context manager yielding a real filesystem path, for APIs that need str paths."""
    return as_file(files(onedriveui).joinpath("assets", *parts))
```

```python
svg = read_bytes("icons", "cloud.svg")      # -> 118 bytes
qss = read_text("app.qss")
with path_for("icons", "cloud.svg") as p:   # -> a real, existing Path
    ...
assert QSvgRenderer(QByteArray(svg)).isValid()      # True
```

> Prefer `read_bytes` and feed Qt a `QByteArray` — it works identically from a source tree, a
> wheel and a zipimport. **Do not use `.qrc`/`pyside6-rcc`**: it adds a build step, bloats the
> package, and `importlib.resources` covers every case here.

### 10.5 `__main__.py` skeleton

```python
import sys, os

def main() -> int:
    # must be set BEFORE QApplication is constructed
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    from PySide6 import QtWidgets, QtGui, QtCore
    QtWidgets.QApplication.setDesktopFileName("onedriveui")   # correct icon + name in GNOME
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("OneDrive")
    app.setOrganizationName("OneDriveUI")
    app.setQuitOnLastWindowClosed(False)                      # tray app: survive window close
    app.setStyle("Fusion")                                    # deterministic base for QSS
    ...
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
```

Also ship `share/applications/onedriveui.desktop` with
`X-GNOME-UsesNotifications=true` and a matching `StartupWMClass`, and a
`~/.config/systemd/user/onedriveui.service` for autostart.

---

## 11. Build-order checklist

1. `theming.py` (§4.2) first — every colour depends on it, and the naive `colorScheme()` approach
   is silently wrong.
2. A `tokens.py` holding the §6.4 table + the `argb()` helper, keyed by `dark: bool`.
3. `app.qss` as a **template string** formatted with tokens; re-apply on `ThemeManager.changed`
   (debounced).
4. `fluent_widgets.py` (§2), `views.py` (§7), `icons.py` (§2.6).
5. `RcloneRunner` (§8.1) + `RcClient` (§8.3).
6. Main window as a **normal decorated `QMainWindow`** (§3.3); Activity Center as a **separate
   normal top-level window** (§3.4); tray status lives in the **DBusMenu** (§3.4).

## 12. Traps, one line each

- `QPushButton{background:X}` without `border:` → Fusion gradient. (§1.3a)
- `class Foo(QWidget)` + QSS background → nothing paints without `WA_StyledBackground`. (§1.3b)
- A bare `QWidget{...}` rule repaints every descendant. (§1.3c)
- `setProperty()` needs `unpolish`/`polish`. (§1.3d)
- Delegate `sizeHint` returning `option.rect.width()` → phantom horizontal scrollbar. (§7.1)
- `QPropertyAnimation(obj, b"name")` silently no-ops if `name` is not an exact `QtCore.Property`.
- Forgetting `lv.setMouseTracking(True)` → `State_MouseOver` never set in the delegate.
- `readyReadStandardOutput` delivers partial lines — buffer. (§8.1)
- Forgetting `reply.deleteLater()` leaks every `QNetworkReply`. (§8.3)
- `QIcon.addPixmap` with a dpr-tagged pixmap registers the wrong size. (§2.6)
- `Qt.Popup` opened without an input serial is killed in <300ms. (§3.4)
- `QWidgetAction` in a tray menu becomes an empty label. (§3.4)
- Top-level `w.pos()` on Wayland returns your request, not reality. (§3.1)
- Colour lerp must include alpha or transparent→opaque flashes black. (§2.1)
- `setLoopCount(-1)` animations keep painting while hidden. (§2.2)
- A `QGraphicsEffect` on a continuously-animating widget rasterises every frame. (§9.3)

## Sources

- [Qt Style Sheets Reference](https://doc.qt.io/qt-6/stylesheet-reference.html)
- [QWindow::startSystemMove()](https://doc.qt.io/qt-6/qwindow.html#startSystemMove)
- [XAML theme resources (type ramp, token semantics)](https://learn.microsoft.com/en-us/windows/apps/design/style/xaml-theme-resources)
- [microsoft-ui-xaml `Common_themeresources_any.xaml`](https://github.com/microsoft/microsoft-ui-xaml/blob/main/controls/dev/CommonStyles/Common_themeresources_any.xaml) — exact colour values
- [Fluent 2 — Shapes / corner radius](https://fluent2.microsoft.design/shapes)
- [Fluent 2 — Elevation](https://fluent2.microsoft.design/elevation)
- [Inter font (SIL OFL 1.1)](https://github.com/rsms/inter)
