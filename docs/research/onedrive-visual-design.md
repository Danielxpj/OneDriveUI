# OneDrive for Windows 11 — Pixel-Level Visual Design Specification

**Purpose.** This is the single source of truth for the *look* of OneDriveUI. Every number here is
either (a) extracted verbatim from Microsoft's shipping XAML sources, (b) taken from Microsoft Learn
documentation, or (c) explicitly marked `[DERIVED]` where it is a design decision we make to match
screenshots because Microsoft publishes no number.

**Provenance of the hard numbers** (downloaded and parsed on 2026-08-30):

| Source | What it gave us |
|---|---|
| `microsoft/microsoft-ui-xaml@main : controls/dev/CommonStyles/Common_themeresources_any.xaml` | Every Fluent colour token, light + dark, exact ARGB |
| `microsoft/microsoft-ui-xaml@main : dxaml/xcp/dxaml/themes/generic.xaml` (2,045,878 bytes) | Control templates, type styles, control metrics |
| `microsoft/microsoft-ui-xaml@winui2/main : dev/CommonStyles/ToggleSwitch_themeresources.xaml` | **The Windows 11 ToggleSwitch template** (see §6 warning) |
| `microsoft/microsoft-ui-xaml@main : .../CornerRadius_themeresources.xaml`, `Button_themeresources.xaml`, `TextBox_themeresources.xaml`, `ProgressBar*.xaml`, `ProgressRing*.xaml`, `FlyoutPresenter_themeresources.xaml`, `Expander_themeresources.xaml`, `NavigationView_themeresources.xaml` | Geometry + per-control brush mapping |
| `CommunityToolkit/Windows@main : components/SettingsControls/src/**` | `SettingsCard` / `SettingsExpander` metrics (the Windows 11 Settings look) |
| `microsoft/fluentui@master : packages/tokens/src/**` | Fluent 2 shadow / motion / spacing / radius scales |
| Microsoft Learn: *Typography in Windows*, *Acrylic material*, *System backdrops*, *What do the OneDrive icons mean?* | Type ramp, material recipe, icon semantics |
| Wikimedia Commons: `Microsoft_Office_OneDrive_(2019–2025).svg` | **The official OneDrive logo path data and its 4 exact blues** |

> **Reading convention.** Colours are written `#AARRGGBB` when they carry alpha (XAML order) and
> `#RRGGBB` when opaque. Qt/QSS wants `rgba(r,g,b,a)` or `#AARRGGBB` in `QColor`, **not** `#RRGGBBAA` —
> see §9.4.

---

## 0. The layering model (read this first)

Windows 11 stacks four kinds of surface. Get this wrong and nothing else will look right.

```
  Layer 3  Flyout / dialog / menu     Acrylic (or its solid fallback) + 1px SurfaceStrokeColorFlyout + r=8 + shadow
  Layer 2  Card / control            CardBackgroundFillColorDefault / ControlFillColorDefault + 1px stroke + r=4
  Layer 1  Content layer ("Layer")   LayerFillColorDefault  (the white rounded panel inside a window)
  Layer 0  Window base               Mica  -> on Linux: SolidBackgroundFillColorBase (#F3F3F3 / #202020)
```

Crucially, layers 1–2 are **translucent whites/blacks over the base**, not opaque colours. On Wayland
we cannot do real Mica or Acrylic (no host-backdrop sampling), so §1.6 gives the **pre-composited
opaque hex** for every one of them. Use those. They are pixel-identical to what Windows shows when
"Transparency effects" is off, which is a shipping, supported Windows appearance.

---

## 1. Colour

### 1.1 Base / Mica substitutes (opaque, no alpha)

| Token | Light | Dark | Use |
|---|---|---|---|
| `SolidBackgroundFillColorBase` | `#F3F3F3` | `#202020` | **Window background** (Mica fallback). Settings window, Activity Center body. |
| `SolidBackgroundFillColorBaseAlt` | `#DADADA` | `#0A0A0A` | Mica Alt fallback — tab-strip / title-bar-heavy windows. |
| `SolidBackgroundFillColorSecondary` | `#EEEEEE` | `#1C1C1C` | Recessed regions. |
| `SolidBackgroundFillColorTertiary` | `#F9F9F9` | `#282828` | **Raised content panel** (right pane of Settings). |
| `SolidBackgroundFillColorQuarternary` | `#FFFFFF` | `#2C2C2C` | Flyout solid fallback. |
| `SolidBackgroundFillColorQuinary` | `#FDFDFD` | `#333333` | — |
| `SolidBackgroundFillColorSenary` | `#FFFFFF` | `#373737` | — |
| `SolidBackgroundFillColorTransparent` | `#00F3F3F3` | `#00202020` | Transparent-but-same-hue. |

### 1.2 Full Fluent token table (verbatim from `Common_themeresources_any.xaml`)

Alpha and RGB are split so you can build `QColor(r,g,b,a)` directly.

#### Text

| Token | Light A | Light RGB | Dark A | Dark RGB |
|---|---|---|---|---|
| `TextFillColorPrimary` | `E4` | `#000000` | `FF` | `#FFFFFF` |
| `TextFillColorSecondary` | `9E` | `#000000` | `C5` | `#FFFFFF` |
| `TextFillColorTertiary` | `72` | `#000000` | `87` | `#FFFFFF` |
| `TextFillColorDisabled` | `5C` | `#000000` | `5D` | `#FFFFFF` |
| `TextFillColorInverse` | `FF` | `#FFFFFF` | `E4` | `#000000` |
| `AccentTextFillColorDisabled` | `5C` | `#000000` | `5D` | `#FFFFFF` |
| `TextOnAccentFillColorPrimary` | `FF` | `#FFFFFF` | `FF` | `#000000` |
| `TextOnAccentFillColorSecondary` | `B3` | `#FFFFFF` | `80` | `#000000` |
| `TextOnAccentFillColorDisabled` | `FF` | `#FFFFFF` | `87` | `#FFFFFF` |
| `TextOnAccentFillColorSelectedText` | `FF` | `#FFFFFF` | `FF` | `#FFFFFF` |

#### Control fills

| Token | Light A | Light RGB | Dark A | Dark RGB |
|---|---|---|---|---|
| `ControlFillColorDefault` | `B3` | `#FFFFFF` | `0F` | `#FFFFFF` |
| `ControlFillColorSecondary` | `80` | `#F9F9F9` | `15` | `#FFFFFF` |
| `ControlFillColorTertiary` | `4D` | `#F9F9F9` | `08` | `#FFFFFF` |
| `ControlFillColorQuarternary` | `C2` | `#F3F3F3` | `0F` | `#FFFFFF` |
| `ControlFillColorDisabled` | `4D` | `#F9F9F9` | `0B` | `#FFFFFF` |
| `ControlFillColorTransparent` | `00` | `#FFFFFF` | `00` | `#FFFFFF` |
| `ControlFillColorInputActive` | `FF` | `#FFFFFF` | `B3` | `#1E1E1E` |
| `ControlStrongFillColorDefault` | `72` | `#000000` | `8B` | `#FFFFFF` |
| `ControlStrongFillColorDisabled` | `51` | `#000000` | `3F` | `#FFFFFF` |
| `ControlSolidFillColorDefault` | `FF` | `#FFFFFF` | `FF` | `#454545` |

#### Subtle / alt fills

| Token | Light A | Light RGB | Dark A | Dark RGB |
|---|---|---|---|---|
| `SubtleFillColorTransparent` | `00` | `#FFFFFF` | `00` | `#FFFFFF` |
| `SubtleFillColorSecondary` | `09` | `#000000` | `0F` | `#FFFFFF` |
| `SubtleFillColorTertiary` | `06` | `#000000` | `0A` | `#FFFFFF` |
| `SubtleFillColorDisabled` | `00` | `#FFFFFF` | `00` | `#FFFFFF` |
| `ControlAltFillColorTransparent` | `00` | `#FFFFFF` | `00` | `#FFFFFF` |
| `ControlAltFillColorSecondary` | `06` | `#000000` | `19` | `#000000` |
| `ControlAltFillColorTertiary` | `0F` | `#000000` | `0B` | `#FFFFFF` |
| `ControlAltFillColorQuarternary` | `18` | `#000000` | `12` | `#FFFFFF` |
| `ControlAltFillColorDisabled` | `00` | `#FFFFFF` | `00` | `#FFFFFF` |
| `AccentFillColorDisabled` | `37` | `#000000` | `28` | `#FFFFFF` |

#### Strokes

| Token | Light A | Light RGB | Dark A | Dark RGB |
|---|---|---|---|---|
| `ControlStrokeColorDefault` | `0F` | `#000000` | `12` | `#FFFFFF` |
| `ControlStrokeColorSecondary` | `29` | `#000000` | `18` | `#FFFFFF` |
| `ControlStrokeColorOnAccentDefault` | `14` | `#FFFFFF` | `14` | `#FFFFFF` |
| `ControlStrokeColorOnAccentSecondary` | `66` | `#000000` | `23` | `#000000` |
| `ControlStrokeColorOnAccentTertiary` | `37` | `#000000` | `37` | `#000000` |
| `ControlStrokeColorOnAccentDisabled` | `0F` | `#000000` | `33` | `#000000` |
| `ControlStrokeColorForStrongFillWhenOnImage` | `59` | `#FFFFFF` | `6B` | `#000000` |
| `CardStrokeColorDefault` | `0F` | `#000000` | `19` | `#000000` |
| `CardStrokeColorDefaultSolid` | `FF` | `#EBEBEB` | `FF` | `#1C1C1C` |
| `ControlStrongStrokeColorDefault` | `72` | `#000000` | `8B` | `#FFFFFF` |
| `ControlStrongStrokeColorDisabled` | `37` | `#000000` | `28` | `#FFFFFF` |
| `SurfaceStrokeColorDefault` | `66` | `#757575` | `66` | `#757575` |
| `SurfaceStrokeColorFlyout` | `0F` | `#000000` | `33` | `#000000` |
| `SurfaceStrokeColorInverse` | `15` | `#FFFFFF` | `0F` | `#000000` |
| `DividerStrokeColorDefault` | `0F` | `#000000` | `15` | `#FFFFFF` |
| `FocusStrokeColorOuter` | `E4` | `#000000` | `FF` | `#FFFFFF` |
| `FocusStrokeColorInner` | `B3` | `#FFFFFF` | `B3` | `#000000` |

#### Cards / layers / smoke

| Token | Light A | Light RGB | Dark A | Dark RGB |
|---|---|---|---|---|
| `CardBackgroundFillColorDefault` | `B3` | `#FFFFFF` | `0D` | `#FFFFFF` |
| `CardBackgroundFillColorSecondary` | `80` | `#F6F6F6` | `08` | `#FFFFFF` |
| `CardBackgroundFillColorTertiary` | `FF` | `#FFFFFF` | `12` | `#FFFFFF` |
| `SmokeFillColorDefault` | `4D` | `#000000` | `4D` | `#000000` |
| `LayerFillColorDefault` | `80` | `#FFFFFF` | `4C` | `#3A3A3A` |
| `LayerFillColorAlt` | `FF` | `#FFFFFF` | `0D` | `#FFFFFF` |
| `LayerOnAcrylicFillColorDefault` | `40` | `#FFFFFF` | `09` | `#FFFFFF` |
| `LayerOnAccentAcrylicFillColorDefault` | `40` | `#FFFFFF` | `09` | `#FFFFFF` |
| `LayerOnMicaBaseAltFillColorDefault` | `B3` | `#FFFFFF` | `73` | `#3A3A3A` |
| `LayerOnMicaBaseAltFillColorSecondary` | `0A` | `#000000` | `0F` | `#FFFFFF` |
| `LayerOnMicaBaseAltFillColorTertiary` | `FF` | `#F9F9F9` | `FF` | `#2C2C2C` |
| `LayerOnMicaBaseAltFillColorTransparent` | `00` | `#000000` | `00` | `#FFFFFF` |

#### Solid backgrounds (Mica substitutes)

| Token | Light A | Light RGB | Dark A | Dark RGB |
|---|---|---|---|---|
| `SolidBackgroundFillColorBase` | `FF` | `#F3F3F3` | `FF` | `#202020` |
| `SolidBackgroundFillColorSecondary` | `FF` | `#EEEEEE` | `FF` | `#1C1C1C` |
| `SolidBackgroundFillColorTertiary` | `FF` | `#F9F9F9` | `FF` | `#282828` |
| `SolidBackgroundFillColorQuarternary` | `FF` | `#FFFFFF` | `FF` | `#2C2C2C` |
| `SolidBackgroundFillColorQuinary` | `FF` | `#FDFDFD` | `FF` | `#333333` |
| `SolidBackgroundFillColorSenary` | `FF` | `#FFFFFF` | `FF` | `#373737` |
| `SolidBackgroundFillColorTransparent` | `00` | `#F3F3F3` | `00` | `#202020` |
| `SolidBackgroundFillColorBaseAlt` | `FF` | `#DADADA` | `FF` | `#0A0A0A` |

#### System / status

| Token | Light A | Light RGB | Dark A | Dark RGB |
|---|---|---|---|---|
| `SystemFillColorSuccess` | `FF` | `#0F7B0F` | `FF` | `#6CCB5F` |
| `SystemFillColorCaution` | `FF` | `#9D5D00` | `FF` | `#FCE100` |
| `SystemFillColorCritical` | `FF` | `#C42B1C` | `FF` | `#FF99A4` |
| `SystemFillColorNeutral` | `72` | `#000000` | `8B` | `#FFFFFF` |
| `SystemFillColorSolidNeutral` | `FF` | `#8A8A8A` | `FF` | `#9D9D9D` |
| `SystemFillColorAttentionBackground` | `80` | `#F6F6F6` | `08` | `#FFFFFF` |
| `SystemFillColorSuccessBackground` | `FF` | `#DFF6DD` | `FF` | `#393D1B` |
| `SystemFillColorCautionBackground` | `FF` | `#FFF4CE` | `FF` | `#433519` |
| `SystemFillColorCriticalBackground` | `FF` | `#FDE7E9` | `FF` | `#442726` |
| `SystemFillColorNeutralBackground` | `06` | `#000000` | `08` | `#FFFFFF` |
| `SystemFillColorSolidAttentionBackground` | `FF` | `#F7F7F7` | `FF` | `#2E2E2E` |
| `SystemFillColorSolidNeutralBackground` | `FF` | `#F3F3F3` | `FF` | `#2E2E2E` |

### 1.3 Accent colour

Windows generates a **7-stop ramp** from a single accent colour. WinUI then *picks a different stop
per theme* — this is the single most-missed detail when people clone Fluent:

| Brush | Light theme resolves to | Dark theme resolves to |
|---|---|---|
| `AccentFillColorDefaultBrush` | `SystemAccentColorDark1` | `SystemAccentColorLight2` |
| `AccentFillColorSecondaryBrush` (hover) | `SystemAccentColorDark1` @ **90 % opacity** | `SystemAccentColorLight2` @ **90 %** |
| `AccentFillColorTertiaryBrush` (pressed) | `SystemAccentColorDark1` @ **80 % opacity** | `SystemAccentColorLight2` @ **80 %** |
| `AccentFillColorDisabledBrush` | `#37000000` (21.6 % black) | `#28FFFFFF` (15.7 % white) |
| `AccentTextFillColorPrimaryBrush` | `SystemAccentColorDark2` | `SystemAccentColorLight3` |
| `AccentTextFillColorSecondaryBrush` | `SystemAccentColorDark3` | `SystemAccentColorLight3` |
| `AccentTextFillColorTertiaryBrush` | `SystemAccentColorDark1` | `SystemAccentColorLight2` |
| `AccentTextFillColorDisabledBrush` | `#5C000000` | `#5DFFFFFF` |
| Text **on** accent | `#FFFFFFFF` | `#FF000000` (**black** on light-blue!) |
| Text on accent, pressed | `#B3FFFFFF` | `#80000000` |
| Selection highlight | `SystemAccentColor` (base) | `SystemAccentColor` (base) |

#### Ramp A — Windows 11 default accent (“communication blue”), verified against the shipping palette

| Stop | Hex |
|---|---|
| `SystemAccentColorLight3` | `#99EBFF` |
| `SystemAccentColorLight2` | `#4CC2FF` |
| `SystemAccentColorLight1` | `#0091F8` |
| `SystemAccentColor` | `#0078D4` |
| `SystemAccentColorDark1` | `#0067C0` |
| `SystemAccentColorDark2` | `#003E92` |
| `SystemAccentColorDark3` | `#001A68` |

#### Ramp B — OneDrive brand blue `#0364B8` (our default accent)

Derived by applying the *same* per-stop HSL transform that produces Ramp A (ΔH/ΔL in degrees /
percentage points, saturation held at the source colour's). The transform round-trips Ramp A exactly,
which validates it.

| Stop | ΔH | ΔL | Hex |
|---|---|---|---|
| `Light3` | −14.27° | +38.43 pp | `#82E1FD` |
| `Light2` | −5.59° | +23.33 pp | `#36B2FC` |
| `Light1` | −1.12° | +7.06 pp | `#047BDB` |
| **Base** | 0 | 0 | `#0364B8` |
| `Dark1` | +1.77° | −3.92 pp | `#0355A4` |
| `Dark2` | +8.48° | −12.94 pp | `#023077` |
| `Dark3` | +18.96° | −21.18 pp | `#01124E` |

**Resulting concrete accent fills (what you actually paint):**

| State | Light theme | Dark theme |
|---|---|---|
| Accent rest | `#0355A4` | `#36B2FC` |
| Accent hover (90 % opacity) | `rgba(3,85,164,0.9)` → flattened over `#F3F3F3` = **`#1B65AC`** | `rgba(54,178,252,0.9)` → over `#202020` = **`#34A3E6`** |
| Accent pressed (80 % opacity) | `rgba(3,85,164,0.8)` → over `#F3F3F3` = **`#3375B4`** | `rgba(54,178,252,0.8)` → over `#202020` = **`#3295D0`** |
| Accent disabled | `#BFBFBF` (flattened over `#F3F3F3`) | `#434343` (over `#202020`) |
| Text on accent | `#FFFFFF` | `#000000` |

> If you prefer the shipping Windows look verbatim (OneDrive's chrome actually follows the *system*
> accent, not the brand blue — the brand blue only appears in the logo/tray icon), swap Ramp B for
> Ramp A. Design the theme file so this is one constant.

### 1.4 Status colours (verbatim)

| Semantic | Token | Light | Dark |
|---|---|---|---|
| Success (fg / icon) | `SystemFillColorSuccess` | `#0F7B0F` | `#6CCB5F` |
| Success background | `SystemFillColorSuccessBackground` | `#DFF6DD` | `#393D1B` |
| Caution / warning (fg) | `SystemFillColorCaution` | `#9D5D00` | `#FCE100` |
| Caution background | `SystemFillColorCautionBackground` | `#FFF4CE` | `#433519` |
| Critical / error (fg) | `SystemFillColorCritical` | `#C42B1C` | `#FF99A4` |
| Critical background | `SystemFillColorCriticalBackground` | `#FDE7E9` | `#442726` |
| Neutral (fg) | `SystemFillColorNeutral` | `#72000000` | `#8BFFFFFF` |
| Neutral solid | `SystemFillColorSolidNeutral` | `#8A8A8A` | `#9D9D9D` |
| Attention (info) | `SystemFillColorAttentionBrush` | = `SystemAccentColor` | = `SystemAccentColorLight2` |
| Attention background | `SystemFillColorAttentionBackground` | `#80F6F6F6` | `#08FFFFFF` |
| Info-bar informational bg (solid) | `SystemFillColorSolidAttentionBackground` | `#F7F7F7` | `#2E2E2E` |

The OneDrive sync-status semantics map onto these: **synced = success green**, **paused/attention =
caution**, **error = critical**, **syncing = accent**.

### 1.5 Material recipes (Mica / Acrylic) and the Linux fallback

| Material | Theme | TintColor | TintOpacity | LuminosityOpacity | FallbackColor |
|---|---|---|---|---|---|
| Background Acrylic (`DesktopAcrylicBackdrop`, `AcrylicBackgroundFillColorDefaultBrush`) | Light | `#FCFCFC` | `0.0` | `0.85` | `#FCFCFC` |
| Background Acrylic | Dark | `#2C2C2C` | `0.15` | `0.96` | `#2C2C2C` |
| Mica (`MicaBackdrop`, Kind=`Base`) | Light / Dark | wallpaper-derived | — | — | `#F3F3F3` / `#202020` |
| Mica Alt (Kind=`BaseAlt`) | Light / Dark | wallpaper-derived | — | — | `#DADADA` / `#0A0A0A` |

Acrylic recipe layer order (Learn, *Acrylic material* → “How we designed acrylic”):
`background → gaussian blur → exclusion blend → colour/tint overlay → noise`.

**Linux/Wayland decision.** GNOME 4x on Wayland gives no backdrop sampling and no per-window blur for
foreign toolkits. Build every "acrylic" surface as its **FallbackColor**, exactly as Windows does when
transparency effects are off. Layer tokens named `LayerOnAcrylic*` then also collapse to their
flattened values in §1.6. Do **not** attempt `QGraphicsBlurEffect` on a screen grab — it is slow,
flickers on Wayland, and cannot see other windows anyway.

### 1.6 Pre-composited (flattened) opaque values — **use these in QSS**

Alpha-over-alpha does not compose correctly across separate QSS rules, and Qt's `rgba()` in a
stylesheet composites against whatever the parent painted, which for a `QScrollArea` viewport is often
not what you expect. These are the alpha tokens already flattened onto the two surfaces you will
actually use.

| Token | Light on `#F3F3F3` | Light on `#FFFFFF` | Dark on `#202020` | Dark on `#2C2C2C` |
|---|---|---|---|---|
| `ControlFillColorDefault` | `#FBFBFB` | `#FFFFFF` | `#2D2D2D` | `#383838` |
| `ControlFillColorSecondary` | `#F6F6F6` | `#FCFCFC` | `#323232` | `#3D3D3D` |
| `ControlFillColorTertiary` | `#F5F5F5` | `#FDFDFD` | `#272727` | `#333333` |
| `ControlFillColorDisabled` | `#F5F5F5` | `#FDFDFD` | `#2A2A2A` | `#353535` |
| `CardBackgroundFillColorDefault` | `#FBFBFB` | `#FFFFFF` | `#2B2B2B` | `#373737` |
| `CardBackgroundFillColorSecondary` | `#F5F5F5` | `#FAFAFA` | `#272727` | `#333333` |
| `SubtleFillColorSecondary` | `#EAEAEA` | `#F6F6F6` | `#2D2D2D` | `#383838` |
| `SubtleFillColorTertiary` | `#EDEDED` | `#F9F9F9` | `#292929` | `#343434` |
| `ControlStrokeColorDefault` | `#E5E5E5` | `#F0F0F0` | `#303030` | `#3B3B3B` |
| `ControlStrokeColorSecondary` | `#CCCCCC` | `#D6D6D6` | `#353535` | `#404040` |
| `CardStrokeColorDefault` | `#E5E5E5` | `#F0F0F0` | `#1D1D1D` | `#282828` |
| `DividerStrokeColorDefault` | `#E5E5E5` | `#F0F0F0` | `#323232` | `#3D3D3D` |
| `ControlStrongStrokeColorDefault` | `#868686` | `#8D8D8D` | `#9A9A9A` | `#9F9F9F` |
| `TextFillColorPrimary` | `#1A1A1A` | `#1B1B1B` | `#FFFFFF` | `#FFFFFF` |
| `TextFillColorSecondary` | `#5C5C5C` | `#616161` | `#CCCCCC` | `#CFCFCF` |
| `TextFillColorTertiary` | `#868686` | `#8D8D8D` | `#969696` | `#9C9C9C` |
| `TextFillColorDisabled` | `#9B9B9B` | `#A3A3A3` | `#717171` | `#797979` |
| `ControlAltFillColorSecondary` | `#EDEDED` | `#F9F9F9` | `#1D1D1D` | `#282828` |
| `ControlAltFillColorTertiary` | `#E5E5E5` | `#F0F0F0` | `#2A2A2A` | `#353535` |
| `ControlAltFillColorQuarternary` | `#DCDCDC` | `#E7E7E7` | `#303030` | `#3B3B3B` |
| `AccentFillColorDisabled` | `#BFBFBF` | `#C8C8C8` | `#434343` | `#4D4D4D` |
| `ControlStrongFillColorDisabled` | `#A6A6A6` | `#AEAEAE` | `#575757` | `#606060` |
| `SurfaceStrokeColorFlyout` | `#E5E5E5` | `#F0F0F0` | `#1A1A1A` | `#232323` |
| `SurfaceStrokeColorDefault` | `#C1C1C1` | `#C8C8C8` | `#424242` | `#494949` |
| `SmokeFillColorDefault` | `#AAAAAA` | `#B2B2B2` | `#161616` | `#1F1F1F` |

> `TextFillColorPrimary` light flattens to `#1A1A1A`, **not** `#000000`. Painting pure black text is
> the fastest way to make a Fluent clone look wrong.

---

## 2. Typography

### 2.1 The Windows 11 type ramp (Microsoft Learn, *Typography in Windows*)

| Style name | Optical size | Weight | Size / line-height (epx) | Qt weight |
|---|---|---|---|---|
| **Caption** | Small | Regular | **12 / 16** | `QFont.Weight.Normal` (400) |
| **Body** | Text | Regular | **14 / 20** | 400 |
| **Body Strong** | Text | Semibold | **14 / 20** | `QFont.Weight.DemiBold` (600) |
| **Body Large** | Text | Regular | **18 / 24** | 400 |
| **Body Large Strong** | Text | Semibold | **18 / 24** | 600 |
| **Subtitle** | Display | Semibold | **20 / 28** | 600 |
| **Title** | Display | Semibold | **28 / 36** | 600 |
| **Title Large** | Display | Semibold | **40 / 52** | 600 |
| **Display** | Display | Semibold | **68 / 92** | 600 |

Segoe UI Variable weight axis: Thin 100, Light 300, Semilight 350, Regular 400, Semibold 600, Bold 700.
**Windows 11 UI uses only Regular and Semibold.** Never italic; never true Bold (700) — Semibold is the
emphasis weight. Minimum legible sizes per Microsoft: 14 px Semibold / 12 px Regular. Sentence case
everywhere, including titles. Default alignment left.

WinUI's own control default (`ControlContentThemeFontSize`) is **14** and `ToolTipContentThemeFontSize`
is **12** — i.e. every button, text box, list item and toggle label is Body/14.

### 2.2 Where each style is used in OneDriveUI

| Element | Style |
|---|---|
| Activity Center account name, Settings page title | Subtitle 20/28 semibold |
| Settings window section header ("Sync and backup") | Body Strong 14/20 semibold |
| Settings card title, activity row primary line, button label | Body 14/20 |
| Settings card subtitle, activity row secondary line, storage caption, timestamps | Caption 12/16, `TextFillColorSecondary` |
| Empty-state headline in the flyout | Body Large 18/24 |

### 2.3 Linux font fallback stack

Segoe UI Variable is not redistributable. The stack, in order:

```
"Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Selawik", "Inter",
"Noto Sans", "Cantarell", "DejaVu Sans", sans-serif
```

**Verified on this machine (`fc-match`, 2026-08-30):** `Segoe UI Variable` → Noto Sans, `Selawik` →
Noto Sans, `Inter` → Noto Sans, `Cantarell` → Cantarell. So **neither Selawik nor Inter is installed**.

**Action for implementers: bundle Selawik.** Selawik is Microsoft's own open-source, *metrically
compatible* substitute for Segoe UI (SIL OFL, `github.com/Microsoft/Selawik`), in Regular, Semilight,
Light, Semibold, Bold. Metric compatibility means every advance width matches Segoe UI, so all the
pixel measurements in this document stay true. Ship the TTFs in `resources/fonts/` and register them
before creating any widget:

```python
from PySide6.QtGui import QFontDatabase, QFont
for f in ("Selawik-Regular.ttf", "Selawik-Semibold.ttf", "Selawik-Bold.ttf"):
    QFontDatabase.addApplicationFont(f":/fonts/{f}")

def ui_font(px: int, semibold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI",
                   "Selawik", "Inter", "Noto Sans", "Cantarell", "DejaVu Sans"])
    f.setPixelSize(px)                      # NEVER setPointSize — the ramp is in px
    f.setWeight(QFont.Weight.DemiBold if semibold else QFont.Weight.Normal)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)  # matches DirectWrite look
    return f
```

`QFont.setFamilies()` (Qt ≥ 5.13) implements a real CSS-style fallback list; `setFamily()` with a
comma-joined string does **not** and will silently give you the default font.

### 2.4 Measured metrics of the fallback (why you must set line height explicitly)

Measured on this machine with PySide6 6.11.2, `QFontMetricsF`:

| Font @ px | ascent | descent | lineSpacing | cap height | x-height |
|---|---|---|---|---|---|
| Noto Sans 12 | 12.8 | 3.5 | **16.3** | 8.6 | 6.4 |
| Noto Sans 14 | 15.0 | 4.1 | **19.0** | 10.0 | 7.5 |
| Noto Sans 18 | 19.2 | 5.3 | **24.5** | 12.9 | 9.6 |
| Noto Sans 20 | 21.4 | 5.9 | **27.2** | 14.3 | 10.7 |
| Noto Sans 28 | 29.9 | 8.2 | **38.1** | 20.0 | 15.0 |
| Cantarell 14 | 13.8 | 3.0 | 16.8 | 9.7 | 6.8 |

Natural line spacing (19.0 at 14 px) is close to but not equal to the ramp (20). **Do not rely on it.**
For multi-line text set line height explicitly:

```python
blk = QTextBlockFormat(); blk.setLineHeight(20, QTextBlockFormat.LineHeightTypes.FixedHeight)
```
and for single-line labels give the widget a fixed height equal to the ramp line height so vertical
rhythm is exact.

---

## 3. Geometry, spacing and elevation

### 3.1 Corner radii (verbatim `CornerRadius_themeresources.xaml`)

| Token | Value | Applies to |
|---|---|---|
| `ControlCornerRadius` | **4 px** (all four corners) | Buttons, text boxes, list-item hover pills, settings cards, checkboxes(3) |
| `OverlayCornerRadius` | **8 px** | Flyouts, menus, dialogs, tooltips, teaching tips |
| Toggle switch track | **10 px** (= height/2, pill) | `RadiusX=10 RadiusY=10` on a 20 px-tall track |
| Toggle switch knob | fully circular | `CornerRadius=7` on a 12 px border (over-specified pill) |
| Progress bar | **1.5 px** fill, **0.5 px** track | `ProgressBarCornerRadius` / `ProgressBarTrackCornerRadius` |
| Window (client-side decoration) | 8 px | Win11 top corners; on GNOME use 8 px on all four |
| Avatar / person picture | circular | — |

Fluent 2 radius scale for reference: `None 0, Small 2, Medium 4, Large 6, XLarge 8, 2XL 12, 3XL 16,
4XL 24, 5XL 32, 6XL 40, Circular 10000`.

### 3.2 Control heights and paddings (verbatim from WinUI)

| Control | Metric | Value |
|---|---|---|
| Button | `ButtonPadding` | `11,5,11,6` (L,T,R,B) |
| Button | `ButtonBorderThemeThickness` | `1` |
| Button | resulting height | **32 px** (see §7.1 for the exact QSS that yields 32) |
| Button | `FocusVisualMargin` | `-3` |
| TextBox | `TextControlThemeMinHeight` / `MinWidth` | **32** / 64 |
| TextBox | `TextControlThemePadding` | `10,5,6,6` |
| TextBox | `TextControlBorderThemeThickness` | `1`; focused `1,1,1,2` |
| TextBox | `TextBoxTopHeaderMargin` | `0,0,0,8` |
| TextBox | inner (clear) button margin / icon size | `0,4,4,4` / 12 px |
| ToggleSwitch | track | **40 × 20**, r=10 |
| ToggleSwitch | `ToggleSwitchThemeMinWidth` | 154 |
| ToggleSwitch | pre/post content margin | 10 / 10; gap column **12 px** |
| ToggleSwitch | header margin | `0,0,0,4` |
| ToggleSwitch | `FocusVisualMargin` | `-7,-3,-7,-3` |
| ListViewItem | `ListViewItemMinHeight` | **40** |
| TreeViewItem | min height | 32 |
| Expander (WinUI) | `ExpanderMinHeight` | **48** |
| Expander | header padding / content padding | `16,0,0,0` / `16` |
| Expander | chevron button / glyph | 32 × 32 / 12 px glyph, margin `20,0,8,0` |
| ContentDialog | min/max width, min height, max height | 320 / 548 / 184 / 756 |
| ContentDialog buttons | height, min/max width | 32, 130 / 202 |
| Flyout | min/max width, min/max height | 96 / 456, 40 / 758 |
| Flyout | `FlyoutContentPadding` | `16,15,16,17` |
| Flyout | border | 1 px `SurfaceStrokeColorFlyout`, radius 8 |
| NavigationView | compact pane / open pane | 48 / 320 |
| NavigationView | item min height (left) | **36**, icon box 40 × 16 |
| NavigationView | selection indicator | **3 × 16 px, radius 2**, accent |
| NavigationView | pane toggle button | 40 × 36 |
| CommandBar (`AppBarThemeCompactHeight`) | | 40 |
| Segoe-style icon default font size | `ControlContentThemeFontSize` | 14 (icons drawn at 16) |

### 3.3 Spacing scale

Use only these values: **4, 8, 12, 16, 20, 24, 32**. (Fluent 2 tokens: `XXS 2, XS 4, SNudge 6, S 8,
MNudge 10, M 12, L 16, XL 20, XXL 24, XXXL 32`. The 2/6/10 nudges exist for icon optical alignment
only — do not use them for layout.)

Stroke widths: `Thin 1, Thick 2, Thicker 3, Thickest 4`.

### 3.4 Focus ring

Windows 11 uses a **two-tone** focus ring, not a single accent ring:

* Outer stroke: **2 px**, `FocusStrokeColorOuter` = `#E4000000` (light) / `#FFFFFFFF` (dark).
* Inner stroke: **1 px**, `FocusStrokeColorInner` = `#B3FFFFFF` (light) / `#B3000000` (dark), drawn
  *inside* the outer one so the ring reads on any background.
* The ring sits **outside** the control by the control's `FocusVisualMargin` (−3 px for buttons,
  meaning the ring is inflated 3 px beyond the control bounds).
* Corner radius of the ring = control radius + 3 (so 7 px around a 4 px button).

There is **no** accent-coloured focus ring in Windows 11 for standard controls. The accent underline
on a focused `TextBox` is a different thing (§7.4).

### 3.5 Elevation / shadows

Fluent 2 shadow tokens (from `fluentui/packages/tokens/src/utils/shadows.ts` plus the light/dark
shadow colours):

| Token | Geometry | Light colours | Dark colours |
|---|---|---|---|
| `shadow2` | `0 0 2px ambient, 0 1px 2px key` | ambient `rgba(0,0,0,.12)`, key `rgba(0,0,0,.14)` | `.24` / `.28` |
| `shadow4` | `0 0 2px, 0 2px 4px` | ″ | ″ |
| `shadow8` | `0 0 2px, 0 4px 8px` | ″ | ″ |
| `shadow16` | `0 0 2px, 0 8px 16px` | ″ | ″ |
| `shadow28` | `0 0 8px, 0 14px 28px` | ambient `.20`, key `.24` | ambient `.40`, key `.48` |
| `shadow64` | `0 0 8px, 0 32px 64px` | ″ | ″ |

Mapping for us:

| Surface | Token | Qt implementation |
|---|---|---|
| Settings card at rest | none (1 px stroke only) | — |
| Tooltip | `shadow4` | `QGraphicsDropShadowEffect(blur=8, dy=2, color=#24000000)` |
| **Activity Center flyout** | `shadow16` | blur **32**, `dy=8`, `#24000000` light / `#47000000` dark |
| Context menu | `shadow16` | same |
| Modal dialog | `shadow64` | blur **128**, `dy=32`, `#3D000000` light / `#7A000000` dark, plus a full-window `SmokeFillColorDefault` `#4D000000` scrim |

Note `QGraphicsDropShadowEffect.setBlurRadius()` is roughly **2 × the CSS blur radius**, because Qt's
radius is the full kernel diameter while CSS's is ~2σ. The blur numbers in the right-hand column are
already doubled; do not double them again.

---

## 4. Motion

### 4.1 Curves and durations (verbatim)

WinUI's own constants (`Common_themeresources_any.xaml`, global section):

| Resource | Value |
|---|---|
| `ControlFastOutSlowInKeySpline` | **`0,0,0,1`** (cubic-bezier(0, 0, 0, 1)) |
| `ControlFasterAnimationDuration` | `00:00:00.083` → **83 ms** |
| `ControlFastAnimationDuration` | `00:00:00.167` → **167 ms** |
| `ControlFastAnimationAfterDuration` | `00:00:00.168` → 168 ms |
| `ControlNormalAnimationDuration` | `00:00:00.250` → **250 ms** |

Fluent 2 curve tokens (web, used for larger motion):

| Token | cubic-bezier |
|---|---|
| `curveAccelerateMax` | `0.9, 0.1, 1, 0.2` |
| `curveAccelerateMid` | `1, 0, 1, 1` |
| `curveAccelerateMin` | `0.8, 0, 0.78, 1` |
| `curveDecelerateMax` | `0.1, 0.9, 0.2, 1` |
| `curveDecelerateMid` | `0, 0, 0, 1` ← **the Fluent “standard” curve** |
| `curveDecelerateMin` | `0.33, 0, 0.1, 1` |
| `curveEasyEaseMax` | `0.8, 0, 0.2, 1` |
| `curveEasyEase` | `0.33, 0, 0.67, 1` |
| `curveLinear` | `0, 0, 1, 1` |

Fluent 2 durations: `ultraFast 50, faster 100, fast 150, normal 200, gentle 250, slow 300,
slower 400, ultraSlow 500` (ms).

**House rules for OneDriveUI:** 83 ms for hover/pressed colour and knob-size changes, **150 ms** for
small state transitions, **250 ms** for flyout open / expander expand / page transitions, **350 ms**
only for the storage-bar fill on first show. Use `curveDecelerateMid` (0,0,0,1) for anything entering,
`curveAccelerateMin` for anything leaving.

### 4.2 Reproducing the curve in Qt (verified)

```python
from PySide6.QtCore import QEasingCurve, QPointF
FLUENT_STANDARD = QEasingCurve(QEasingCurve.Type.BezierSpline)
FLUENT_STANDARD.addCubicBezierSegment(QPointF(0.0, 0.0), QPointF(0.0, 1.0), QPointF(1.0, 1.0))
# verified: valueForProgress(0.5) == 0.8899  -> correctly front-loaded/decelerating
```

`QEasingCurve.Type.OutCubic` is *not* the same curve (0.5 → 0.875 is close but the tail differs);
use the explicit bezier. `addCubicBezierSegment` takes the two control points **and** the end point,
and the segments must end at (1,1).

---

## 5. The Activity Center flyout

Microsoft publishes no dimensions for the OneDrive Activity Center. Everything in this section is
`[DERIVED]`: it is a buildable specification produced by measuring the shipping client's proportions
against the Fluent metrics above, and it is internally consistent (every number is on the 4 px grid
and reuses a WinUI token). Treat these as **normative for our clone**.

### 5.1 Overall frame

| Property | Value |
|---|---|
| Width | **360 px** (fixed; never resizes) |
| Height | **content-driven, min 320, max 620** — the list scrolls, header and footer are pinned |
| Corner radius | **8 px** (`OverlayCornerRadius`), all four corners |
| Border | 1 px `SurfaceStrokeColorFlyout` → flattened `#E5E5E5` light / `#1A1A1A` dark |
| Background | Acrylic fallback: `#FCFCFC` light / `#2C2C2C` dark. (Not `#F3F3F3`: flyouts sit a layer above the window base.) |
| Shadow | `shadow16` → `QGraphicsDropShadowEffect(blurRadius=32, xOffset=0, yOffset=8, color=#24000000 / #47000000)` |
| Window flags | `Qt.Popup \| Qt.FramelessWindowHint \| Qt.NoDropShadowWindowHint` + `WA_TranslucentBackground` |
| Placement | bottom-right, **12 px** from the screen work-area right edge and **12 px** above the panel |
| Open animation | fade 0→1 over 150 ms + translate **+16 px → 0** vertically, `curveDecelerateMid` |
| Close animation | fade 1→0 over 100 ms, `curveAccelerateMin` |

Because the flyout is translucent-cornered, paint the rounded body yourself in `paintEvent` (see
§9.8) — a `border-radius` in QSS on a top-level translucent window leaves square corners on some
Wayland compositors.

### 5.2 Vertical stack

```
┌────────────────────────────────── 360 ──────────────────────────────────┐
│  HEADER                                                        h = 64   │  ← 12 top, 12 bottom pad
│   [avatar 32] account name (Body Strong 14)          [⋯ 32] [⚙ 32]      │
│               user@example.com (Caption 12, secondary)                   │
├──────────────────────────────────────────────────────────────────────────┤
│  STATUS STRIP (optional, only when not "Up to date")           h = 40   │
│   [status icon 16]  "Syncing 12 files"  (Body 14)                        │
├──────────────────────────────────────────────────────────────────────────┤
│  STORAGE BLOCK                                                 h = 56   │
│   "3.2 GB of 5 GB used"  (Caption 12, secondary)                         │
│   ▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░  bar 328 × 4, r = 2                  │
├──────────────────────────────────────────────────────────────────────────┤
│  SECTION HEADER  "Recent activity"  (Body Strong 14)           h = 36   │
├──────────────────────────────────────────────────────────────────────────┤
│  ACTIVITY LIST  (QListView, scrollable)                                  │
│   row                                                          h = 56   │
│   row                                                          h = 56   │
│   …                                                                      │
├──────────────────────────────────────────────────────────────────────────┤
│  FOOTER                                                        h = 48   │
│   [📁 Open folder]  [🌐 View online]              [⏸ Pause syncing]      │
└──────────────────────────────────────────────────────────────────────────┘
```

Horizontal content inset: **16 px** left and right throughout → content width **328 px**.

### 5.3 Header block (h = 64)

| Element | Geometry |
|---|---|
| Avatar | 32 × 32, circular, at x = 16, vertically centred |
| Gap avatar → text | 12 px |
| Account display name | Body Strong 14/20, `TextFillColorPrimary`, baseline row 1 |
| Account email / plan | Caption 12/16, `TextFillColorSecondary`, 2 px below row 1 |
| Text block total height | 20 + 2 + 16 = 38, vertically centred in 64 |
| “More” (⋯) and “Settings” (⚙) buttons | 32 × 32 subtle icon buttons, 16 px glyphs, 4 px apart, right-aligned ending at x = 344 |
| Divider below header | 1 px `DividerStrokeColorDefault` (`#E5E5E5` / `#323232`), full 360 width |

### 5.4 Storage block (h = 56)

| Element | Geometry |
|---|---|
| Caption line | Caption 12/16 at y = 12, `TextFillColorSecondary`, e.g. “3.2 GB of 5 GB used” |
| Bar | **328 × 4**, radius 2, at y = 36 |
| Track colour | `ControlStrongStrokeColorDefault` → `#868686` light / `#9A9A9A` dark (this is what WinUI's `ProgressBarBackground` resolves to) |
| Fill colour | Accent (`#0355A4` / `#36B2FC`) |
| Fill at ≥ 90 % used | `SystemFillColorCaution` `#9D5D00` / `#FCE100` |
| Fill at 100 % / over quota | `SystemFillColorCritical` `#C42B1C` / `#FF99A4` |
| Fill animation on first show | width 0 → target over **350 ms**, `curveDecelerateMid` |

Note the OneDrive client's storage bar is **thicker than WinUI's stock 3 px ProgressBar** — 4 px reads
better at this width and lands on the 4 px grid.

### 5.5 Activity row (h = 56)

```
 16    │ 32 │ 12 │                        216                       │ 12 │ 20 │ 16
┌──────┼────┼────┼──────────────────────────────────────────────────┼────┼────┼──────┐
│      │icon│    │ Quarterly report.xlsx        (Body 14/20)         │    │ st │      │  56
│      │ 32 │    │ Uploaded · 2 minutes ago     (Caption 12/16, sec) │    │ 20 │      │
└──────┴────┴────┴──────────────────────────────────────────────────┴────┴────┴──────┘
```

| Element | Geometry |
|---|---|
| Row height | **56 px** (2-line row). Use **48 px** for single-line rows (e.g. “Pause” menu entries). |
| File-type icon | 32 × 32 at x = 16 (a 20 px glyph centred in a 32 px box is also acceptable) |
| Primary text | Body 14/20, `TextFillColorPrimary`, elided right (`Qt.ElideRight`) |
| Secondary text | Caption 12/16, `TextFillColorSecondary`, 2 px gap below primary |
| Trailing status glyph | 20 × 20 at x = 324 |
| Hover background | `SubtleFillColorSecondary` → `#EAEAEA` / `#2D2D2D`, radius 4, inset 4 px left/right (pill spans x = 4 … 356) |
| Pressed background | `SubtleFillColorTertiary` → `#EDEDED` / `#292929` |
| Selected | accent-tinted: `AccentFillColorDefault` @ 10 % + a 3 × 16 px accent bar at x = 4, radius 2 |
| Row separator | **none** — Fluent lists use whitespace, not rules |
| Progress on an in-flight row | a 2 px accent bar pinned to the bottom edge of the row, spanning the text column |

### 5.6 Icon sizes used in the flyout

| Size | Where |
|---|---|
| **12 px** | chevrons, the inner “clear” glyph of a text box |
| **16 px** | header command glyphs (⋯, ⚙), status strip icon, footer button glyphs, file-status badges |
| **20 px** | settings-card header icons (`SettingsCardHeaderIconMaxSize = 20`), trailing row status |
| **24 px** | empty-state / large status glyph |
| **32 px** | avatar, file-type icon in an activity row |
| **48 px** | empty-state illustration |

### 5.7 Footer (h = 48)

Three subtle (transparent-background) buttons, 32 px tall, 8 px apart, 16 px from each edge; icon 16 px
+ 8 px gap + Body 14 label. Left group left-aligned, “Pause syncing” right-aligned. 1 px
`DividerStrokeColorDefault` above the footer.

---

## 6. The Settings window

### 6.1 Frame

| Property | Value |
|---|---|
| Default size | **1024 × 720** logical px `[DERIVED]`; minimum **640 × 480** |
| Background | `SolidBackgroundFillColorBase` `#F3F3F3` / `#202020` (Mica fallback) |
| Content pane background | `SolidBackgroundFillColorTertiary` `#F9F9F9` / `#282828`, rounded **8 px top-left only** where it meets the nav, 1 px `CardStrokeColorDefault` on its top and left edges (`NavigationViewContentGridBorderThickness = 1,1,0,0`) |
| Title bar | 32 px tall, draggable, transparent over the base; title Body 14 at x = 48 (after a 16 px app icon at x = 16) |

### 6.2 Left navigation (the Windows 11 Settings pattern)

| Property | Value |
|---|---|
| Open pane width | **320 px** (`NavigationView.OpenPaneLength` default) |
| Compact pane width | 48 px (`NavigationViewCompactPaneLength`) — collapse below 640 px window width |
| Nav item height | **36 px** (`NavigationViewItemOnLeftMinHeight`) |
| Nav item margin | `4,2` (`NavigationViewItemButtonMargin`) → 4 px horizontal inset, 2 px vertical gap |
| Nav item radius | 4 px |
| Icon box | 40 px wide, 16 px glyph |
| Label | Body 14, 4 px after the icon box |
| Selection indicator | **3 × 16 px, radius 2**, accent, at the left edge of the item, vertically centred |
| Selected background | `SubtleFillColorSecondary`; hover `SubtleFillColorSecondary`; pressed `SubtleFillColorTertiary` |
| Pane header row | 40 px min height |
| Page header margin | `56,44,0,0` when using the NavigationView header slot |

Nav sections for OneDriveUI: **Sync and backup · Account · Notifications · Network · About**.

### 6.3 The settings card (verbatim from `CommunityToolkit/Windows` `SettingsCard`)

| Token | Value |
|---|---|
| `SettingsCardMinHeight` | **68 px** |
| `SettingsCardMinWidth` | 148 |
| `SettingsCardPadding` | **`16,16,16,16`** |
| `SettingsCardBorderThickness` | **1** |
| Corner radius | `ControlCornerRadius` = **4 px** |
| `SettingsCardHeaderIconMaxSize` | **20 px** |
| `SettingsCardHeaderIconMargin` | `2,0,20,0` → icon sits 2 px in, then a **20 px** gap to the text |
| `SettingsCardDescriptionFontSize` | **12 px** |
| `SettingsCardContentMinWidth` | 120 |
| `SettingsCardActionIconMaxSize` / margin | 13 px chevron / `14,0,0,0` |
| `SettingsCardVerticalHeaderContentSpacing` | 8 (when the card wraps to 2 rows) |
| `SettingsCardWrapThreshold` / `…NoIconThreshold` | 476 / 286 — below this width the control drops to its own line |
| Disabled icon opacity | 0.4 |

Brush mapping:

| State | Background | Border |
|---|---|---|
| Rest | `CardBackgroundFillColorDefault` → `#FBFBFB` / `#2B2B2B` | `CardStrokeColorDefault` → `#E5E5E5` / `#1D1D1D` |
| Hover (clickable cards only) | `ControlFillColorSecondary` → `#F6F6F6` / `#323232` | `ControlElevationBorderBrush` (gradient, §7.2) |
| Pressed | `ControlFillColorTertiary` → `#F5F5F5` / `#272727` | `ControlStrokeColorDefault` |
| Disabled | `ControlFillColorDisabled` | `ControlStrokeColorDefault` |
| Foreground | `TextFillColorPrimary`; pressed → `TextFillColorSecondary`; disabled → `TextFillColorDisabled` |

Layout inside the 68 px card: `[20 px icon] 20gap [title Body 14 / description Caption 12] …flex… [control, min 120 px] [optional 13 px chevron]`, all vertically centred, 16 px padding all round.

**Cards stack with a 4 px vertical gap** in a group; group heading is Body Strong 14 with 24 px above
and 8 px below.

### 6.4 The settings expander (`SettingsExpander`)

| Token | Value |
|---|---|
| `SettingsExpanderHeaderPadding` | **`16,16,4,16`** |
| `SettingsExpanderItemPadding` | **`58,8,44,8`** — the 58 px left indent aligns child rows with the header's text column |
| `ClickableSettingsExpanderItemPadding` | `58,8,16,8` |
| `SettingsExpanderItemBorderThickness` | `0,1,0,0` (a 1 px rule *above* each child row) |
| `SettingsExpanderContentMinHeight` | 16 |
| `SettingsExpanderChevronButtonWidth/Height` | **32 × 32** |
| Chevron glyph | 12 px, rotates 0° → 180° over **150 ms**, `curveEasyEase` |
| Header corner radius when expanded | top corners 4, bottom corners 0; the content block takes bottom 4 |

WinUI's plain `Expander` for comparison: min height **48**, header padding `16,0,0,0`, content padding
`16`, chevron margin `20,0,8,0`, content border `1,0,1,1` (down) / `1,1,1,0` (up).

---

## 7. Controls, exactly

### 7.1 Buttons

Brush mapping is verbatim from `Button_themeresources.xaml`.

#### Standard button

| State | Background | Border | Foreground |
|---|---|---|---|
| Rest | `ControlFillColorDefault` → `#FBFBFB` / `#2D2D2D` | `ControlElevationBorderBrush` (§7.2) | `TextFillColorPrimary` → `#1A1A1A` / `#FFFFFF` |
| Hover | `ControlFillColorSecondary` → `#F6F6F6` / `#323232` | `ControlElevationBorderBrush` | `TextFillColorPrimary` |
| Pressed | `ControlFillColorTertiary` → `#F5F5F5` / `#272727` | flat `ControlStrokeColorDefault` `#E5E5E5` / `#303030` | `TextFillColorSecondary` → `#5C5C5C` / `#CCCCCC` |
| Disabled | `ControlFillColorDisabled` → `#F5F5F5` / `#2A2A2A` | flat `ControlStrokeColorDefault` | `TextFillColorDisabled` → `#9B9B9B` / `#717171` |

**The pressed state removes the elevation gradient** — that is what makes a Windows 11 button look
like it "sinks".

#### Accent button

| State | Background | Border | Foreground |
|---|---|---|---|
| Rest | `AccentFillColorDefault` → `#0355A4` / `#36B2FC` | `AccentControlElevationBorderBrush` | `#FFFFFF` / `#000000` |
| Hover | accent @ 90 % → `#1B65AC` / `#34A3E6` | `AccentControlElevationBorderBrush` | same |
| Pressed | accent @ 80 % → `#3375B4` / `#3295D0` | **transparent** | `TextOnAccentFillColorSecondary` `#B3FFFFFF` / `#80000000` |
| Disabled | `#BFBFBF` / `#434343` | transparent | `#FFFFFF` / `#87FFFFFF` |

#### Subtle button / icon button

| State | Background | Border | Foreground |
|---|---|---|---|
| Rest | transparent | transparent | `TextFillColorPrimary` |
| Hover | `SubtleFillColorSecondary` → `#EAEAEA` / `#2D2D2D` | same as bg | `TextFillColorPrimary` |
| Pressed | `SubtleFillColorTertiary` → `#EDEDED` / `#292929` | same as bg | `TextFillColorSecondary` |
| Disabled | transparent | transparent | `TextFillColorDisabled` |

#### Hyperlink button

Foreground `AccentTextFillColorPrimary` (`#023077` light / `#82E1FD` dark); hover adds
`SubtleFillColorSecondary` background and switches to `AccentTextFillColorSecondary`; pressed uses
`AccentTextFillColorTertiary`. No underline at rest.

### 7.2 The "1 px bottom stroke" — how it actually works

Windows 11 buttons do **not** have a plain 1 px border. They use `ControlElevationBorderBrush`, a
`LinearGradientBrush` in **absolute** mapping mode from `(0,0)` to `(0,3)` — i.e. a 3-device-pixel-tall
gradient anchored to one edge:

* **Light theme:** the brush carries `RelativeTransform ScaleY=-1`, so the stops are flipped and the
  **darker** colour lands on the **bottom** edge:
  `offset 0.33 → ControlStrokeColorSecondary #29000000` (≈16 % black) and
  `offset 1.0 → ControlStrokeColorDefault #0F000000` (≈6 % black).
  Result: sides/top ≈ `#E5E5E5`, bottom ≈ `#CCCCCC`.
* **Dark theme:** no flip, so the **brighter** colour lands on the **top** edge:
  `offset 0.33 → #18FFFFFF` (≈9 % white), `offset 1.0 → #12FFFFFF` (≈7 % white).
  Result: top ≈ `#353535`, sides/bottom ≈ `#303030`.
* `AccentControlElevationBorderBrush` is the same idea with
  `ControlStrokeColorOnAccentSecondary #66000000/#23000000` at 0.33 and
  `ControlStrokeColorOnAccentDefault #14FFFFFF` at 1.0, **always** flipped (`ScaleY=-1`) so accent
  buttons get a dark bottom edge in both themes.
* `CircleElevationBorderBrush` (used for the toggle knob) is `RelativeToBoundingBox` 0→1 with stops at
  0.50/0.70 — a subtle vertical shading on the 12 px knob.

In Qt this is trivially expressible in QSS because border colours can be set per side:

```css
QPushButton {
    border: 1px solid #E5E5E5;
    border-bottom-color: #CCCCCC;   /* light theme: darker bottom */
}
```
(dark theme: `border: 1px solid #303030; border-top-color: #353535;`)

### 7.3 ToggleSwitch — **exact geometry**

> ⚠ **Trap.** `dxaml/xcp/dxaml/themes/generic.xaml` in `microsoft-ui-xaml@main` still carries the
> *legacy* Windows 10 template (44 × 20 track, 10 px knob, 2 px stroke, translate 24). The **Windows 11**
> template is the one in `winui2/main : dev/CommonStyles/ToggleSwitch_themeresources.xaml`. Use the
> Windows 11 numbers below.

| Part | Geometry |
|---|---|
| `OuterBorder` (off track) | **40 × 20**, `RadiusX/Y = 10`, `StrokeThickness = ToggleSwitchOuterBorderStrokeThickness = 1` |
| `SwitchKnobBounds` (on track) | **40 × 20**, r = 10, `StrokeThickness = ToggleSwitchOnStrokeThickness = 0` |
| `SwitchKnob` container | **20 × 20**, left-aligned |
| Knob at rest | **12 × 12** circle, `HorizontalAlignment=Center` inside the 20 box; off-knob `Margin="-1,0,0,0"`, on-knob `Margin="0,0,1,0"` (a 1 px optical nudge toward the track edge) |
| Knob on hover | animates to **14 × 14** over `ControlFasterAnimationDuration` (**83 ms**) with `KeySpline 0,0,0,1` |
| Knob on press | animates to **17 wide × 14 tall** (the “stretch”), plus `VisualState.Setters` re-anchor it: On-knob `HorizontalAlignment=Right, Margin="0,0,3,0"`; Off-knob `HorizontalAlignment=Left, Margin="3,0,0,0"` |
| Knob travel | `KnobTranslateTransform.X: 0 → 20` when On (`Duration=0`; the *animation* is the `RepositionThemeAnimation` on the transition) |
| Off→On / On→Off transition | `RepositionThemeAnimation` on `SwitchKnob`; track cross-fade `OuterBorder.Opacity 1→0` and `SwitchKnobBounds.Opacity 0→1` over 83 ms |
| Gap to label | 12 px fixed column; `ToggleSwitchPreContentMargin`/`PostContentMargin` = 10 each; min width 154 |
| Focus visual margin | `-7,-3,-7,-3` |

Colours (all verbatim `StaticResource` mappings, same in both themes because the underlying tokens flip):

| Part / state | Token → light / dark |
|---|---|
| Track off, rest | `ControlAltFillColorSecondary` → `#EDEDED` / `#1D1D1D` |
| Track off, hover | `ControlAltFillColorTertiary` → `#E5E5E5` / `#2A2A2A` |
| Track off, pressed | `ControlAltFillColorQuarternary` → `#DCDCDC` / `#303030` |
| Track off, disabled | `ControlAltFillColorDisabled` → transparent |
| Track-off stroke, rest/hover/pressed | `ControlStrongStrokeColorDefault` → `#868686` / `#9A9A9A` |
| Track-off stroke, disabled | `ControlStrongStrokeColorDisabled` → `#A6A6A6` / `#575757` |
| Track on, rest | `AccentFillColorDefault` → `#0355A4` / `#36B2FC` |
| Track on, hover | `AccentFillColorSecondary` (90 %) → `#1B65AC` / `#34A3E6` |
| Track on, pressed | `AccentFillColorTertiary` (80 %) → `#3375B4` / `#3295D0` |
| Track on, disabled | `AccentFillColorDisabled` → `#BFBFBF` / `#434343` |
| Knob off | `TextFillColorSecondary` → `#5C5C5C` / `#CCCCCC` |
| Knob off, disabled | `TextFillColorDisabled` → `#9B9B9B` / `#717171` |
| Knob on | `TextOnAccentFillColorPrimary` → `#FFFFFF` / **`#000000`** |
| Knob on, disabled | `TextOnAccentFillColorDisabled` → `#FFFFFF` / `#87FFFFFF` |
| Knob-on stroke | `CircleElevationBorderBrush` (subtle vertical gradient, 0.50/0.70 stops) |
| Container background (the hit-target) | `SubtleFillColorTransparent` in every state — the toggle has **no** hover pill |

### 7.4 TextBox / LineEdit

| State | Background | Border |
|---|---|---|
| Rest | `ControlFillColorDefault` → `#FBFBFB` / `#2D2D2D` | `TextControlElevationBorderBrush` |
| Hover | `ControlFillColorSecondary` → `#F6F6F6` / `#323232` | same |
| Focused | `ControlFillColorInputActive` → `#FFFFFF` / `#B31E1E1E` (≈`#232323` over `#202020`) | `TextControlElevationBorderFocusedBrush` |
| Disabled | `ControlFillColorDisabled` | flat `ControlStrokeColorDefault` |

`TextControlElevationBorderBrush` is a 2-px-absolute vertical gradient, `ScaleY=-1`, stops
`0.5 → ControlStrongStrokeColorDefault` and `1.0 → ControlStrokeColorDefault`. Practically: **1 px
`#E5E5E5` on three sides and 1 px `#868686` along the bottom** (light); `#303030` / `#9A9A9A` (dark).

`TextControlElevationBorderFocusedBrush` replaces the bottom stop with `SystemAccentColorLight2` and
the border thickness becomes `1,1,1,2` — i.e. **a 2 px accent underline** on focus. That is the
Windows 11 focused text field, and it is *not* the focus ring of §3.4.

Placeholder: `TextFillColorSecondary`. Selection: `SystemAccentColor` (base ramp stop).
Inner clear-button: 12 px glyph, margin `0,4,4,4`, hover `SubtleFillColorSecondary`.

### 7.5 ProgressBar (verbatim `ProgressBar_themeresources.xaml`)

| Property | Value |
|---|---|
| `ProgressBarMinHeight` | **3 px** (the fill) |
| `ProgressBarTrackHeight` | **1 px** (the track is thinner than the fill!) |
| `ProgressBarCornerRadius` | **1.5** |
| `ProgressBarTrackCornerRadius` | **0.5** |
| `ProgressBarBorderThemeThickness` | 0 |
| Fill | `AccentFillColorDefaultBrush` |
| Track | `ControlStrongStrokeColorDefault` → `#868686` / `#9A9A9A` |
| Paused | `SystemFillColorCaution` `#9D5D00` / `#FCE100` |
| Error | `SystemFillColorCritical` `#C42B1C` / `#FF99A4` |
| Indeterminate | a fill segment of ~**33 %** of the track width sweeping left→right, then a second shorter one; period **2 s**, easing `curveEasyEaseMax` |

For the Activity Center storage bar we deviate to 4 px / r 2 (§5.4) — flag that as an intentional
local override, not a token change.

### 7.6 ProgressRing (verbatim `ProgressRing_themeresources.xaml`)

| Property | Value |
|---|---|
| `ProgressRingStrokeThickness` | **4 px** |
| Default diameter | 32 px (16 px small variant halves the stroke to 2) |
| Foreground | `AccentFillColorDefaultBrush` |
| Background | `ControlFillColorTransparent` (i.e. no visible track) |
| Indeterminate motion | Windows uses a 6-dot Lottie; the accepted static-XAML equivalent is an **arc sweeping from 30° to 300° of extent while rotating**, period **2 s**, `curveEasyEase`. Cap style: round. |

In Qt: `QPainter.drawArc` on a `QConicalGradient`-free plain pen, `pen.setCapStyle(Qt.RoundCap)`,
driven by a `QVariantAnimation(0→360, duration=2000, loopCount=-1)`.

---

## 8. Iconography

### 8.1 The OneDrive cloud logo — exact SVG

This is the official Microsoft 365 OneDrive mark (2019–2025 generation, still the one used by the
Windows sync client and the tray). Path data and colours are **verbatim** from Microsoft's shipping
asset `OfficeCore10_32x_24x_20x_16x_01-22-2019`.

The logo is a **four-shape flat construction**, not a gradient. Reading back-to-front:

| # | Shape | Fill | What it is |
|---|---|---|---|
| 1 | Rear-top lobe | **`#0364B8`** | the small dark cloud bump at the top-right |
| 2 | Left lobe | **`#0078D4`** | the mid-blue left bulge |
| 3 | Right lobe | **`#1490DF`** | the light-blue right bulge |
| 4 | Front body | **`#28A8EA`** | the big lightest sweep across the bottom |

`viewBox="0 5.5 32 20.5"` — the mark is **wider than it is tall** (32 × 20.5) and is *not* square.
When placing it in a square box, letterbox it vertically; do not stretch.

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 5.5 32 20.5">
 <path fill="#0364b8" d="M12.20245,11.19292l.00031-.0011,6.71765,4.02379,4.00293-1.68451.00018.00068A6.4768,6.4768,0,0,1,25.5,13c.14764,0,.29358.0067.43878.01639a10.00075,10.00075,0,0,0-18.041-3.01381C7.932,10.00215,7.9657,10,8,10A7.96073,7.96073,0,0,1,12.20245,11.19292Z"/>
 <path fill="#0078d4" d="M12.20276,11.19182l-.00031.0011A7.96073,7.96073,0,0,0,8,10c-.0343,0-.06805.00215-.10223.00258A7.99676,7.99676,0,0,0,1.43732,22.57277l5.924-2.49292,2.63342-1.10819,5.86353-2.46746,3.06213-1.28859Z"/>
 <path fill="#1490df" d="M25.93878,13.01639C25.79358,13.0067,25.64764,13,25.5,13a6.4768,6.4768,0,0,0-2.57648.53178l-.00018-.00068-4.00293,1.68451,1.16077.69528L23.88611,18.19l1.66009.99438,5.67633,3.40007a6.5002,6.5002,0,0,0-5.28375-9.56805Z"/>
 <path fill="#28a8ea" d="M25.5462,19.18437,23.88611,18.19l-3.80493-2.2791-1.16077-.69528L15.85828,16.5042,9.99475,18.97166,7.36133,20.07985l-5.924,2.49292A7.98889,7.98889,0,0,0,8,26H25.5a6.49837,6.49837,0,0,0,5.72253-3.41556Z"/>
</svg>
```

**Underlying construction, if you need to redraw at another size:** three circles plus one capsule,
all clipped to a common bottom line at y = 26 in the 32-unit grid.

| Element | Centre | Radius |
|---|---|---|
| Large left circle | **(8, 18)** — verified: both arc endpoints are 7.998 units away | **8** |
| Upper cloud circle | **(16.25, 15.50)** — solved from the arc endpoints (25.93878, 13.01639) → (7.89778, 10.00258) | **10** — its top lands exactly on y = 5.5, the viewBox top |
| Right circle | **(25.5, 19.5)** | **6.5** |
| Baseline | y = **26** = 18 + 8 = 19.5 + 6.5 | all three circles are tangent to it; everything is clipped flat here |

The colour split lines are the chords between those circles, which is why the four fills look like
overlapping translucent lobes even though every fill is opaque.

The 2025-refresh icon (`Microsoft_OneDrive_Icon_(2025 - present).svg`) replaces the four flats with
seven radial gradients over a 648 × 431 viewBox. **Do not use it for the tray**: at 16 px the gradients
mud together. Use the flat 2019 mark for anything ≤ 32 px.

**Monochrome variant (tray on a themed panel):** collapse all four fills to a single colour and use
only shapes 2+4 unioned, so the silhouette stays readable at 16 px.

### 8.2 Tray (notification-area) state icons

Base: the flat OneDrive cloud at **16 × 16** (also ship 20, 24, 32 for HiDPI). Badges are composited
into the **bottom-right** quadrant of the 16 px box, sized **10 × 10**, with a 1 px cut-out ring in the
panel background colour so the badge separates from the cloud.

| State | Badge | Colour | Notes |
|---|---|---|---|
| **Up to date** | none | cloud in brand blues | Idle state. |
| **Syncing** | two circular arrows chasing each other | white glyph on accent `#0355A4` / `#36B2FC` circle | Rotate the badge 360° per **1.5 s**, linear. |
| **Paused** | pause bars (two 2 × 6 px rounded rects, 2 px apart) | white on `SystemFillColorSolidNeutral` `#8A8A8A` / `#9D9D9D` | |
| **Signed out / setup incomplete** | the whole cloud desaturated to `#8A8A8A` + a 2 px diagonal slash at 45° | `#8A8A8A` | Learn: “greyed-out icon with a line through it”. |
| **Sync error** | white ✕ in a filled circle | `SystemFillColorCritical` `#C42B1C` / `#FF99A4` | |
| **Attention / warning** | white “!” in a triangle | `SystemFillColorCaution` `#9D5D00` / `#FCE100` | Learn describes both a yellow triangle and a brown “!”; use the triangle. |
| **Blocked account** | white horizontal bar in a filled circle (“no entry”) | `#C42B1C` | |
| **Informational** | white “i” in a filled circle | accent | “New messages about how best to use OneDrive.” |

### 8.3 File-status overlay badges (File Explorer / our file browser)

Placed **bottom-left** of the file/folder icon, **10 × 10** on a 16 px icon, **12 × 12** on a 20 px icon,
**16 × 16** on a 32 px icon. Every badge sits on a 1 px cut-out ring in the list background so it reads
over any thumbnail.

| Meaning | Shape | Colours |
|---|---|---|
| **Online-only** (cloud file, not downloaded) | cloud **outline**, 1.5 px stroke, hollow | stroke `#0078D4`; on dark `#4CC2FF` |
| **Locally available** (downloaded on demand) | circle **outline** + green check inside | stroke + check `SystemFillColorSuccess` `#0F7B0F` / `#6CCB5F`; fill transparent/white |
| **Always keep on this device** | **filled** circle with a white check | fill `#0F7B0F` / `#6CCB5F`, glyph `#FFFFFF` |
| **Syncing** | two circular arrows | `#0078D4` / `#4CC2FF`, rotating 360°/1.5 s |
| **Sync error** | filled circle with white ✕ | `#C42B1C` / `#FF99A4` |
| **Shared** | two overlapping person silhouettes | `TextFillColorSecondary` `#5C5C5C` / `#CCCCCC` |
| **Blocked by policy** | grey circle with a flat horizontal bar | `SystemFillColorSolidNeutral` `#8A8A8A` / `#9D9D9D` |

(Semantics from Microsoft Support, *What do the OneDrive icons mean?*. The colour assignments are ours,
chosen from the Fluent status tokens so they stay correct in dark mode.)

### 8.4 UI glyphs

Windows uses **Segoe Fluent Icons** (`SymbolThemeFontFamily = "Segoe Fluent Icons,Segoe MDL2 Assets"`),
which is **not licensed for Linux redistribution**. Use **Fluent UI System Icons**
(`github.com/microsoft/fluentui-system-icons`, **MIT**) instead — same design language, SVG, with
`Regular` (1.5 px stroke) and `Filled` variants at 12/16/20/24/28/32/48 native sizes. Rule: use the
SVG whose native size equals the pixel size you are drawing at; never scale a 24 px glyph to 16 px, the
stroke weight will be wrong.

Glyph map for OneDriveUI: `cloud`, `cloud_sync`, `cloud_checkmark`, `cloud_off`, `arrow_sync`,
`pause`, `play`, `settings`, `more_horizontal`, `folder`, `folder_open`, `globe`, `person_circle`,
`checkmark_circle`, `error_circle`, `warning`, `info`, `chevron_down`, `chevron_right`, `dismiss`,
`open`, `share`, `delete`, `arrow_upload`, `arrow_download`.

Render SVGs through `QSvgRenderer` into a `QPixmap` at `devicePixelRatio` and recolour by painting with
`QPainter.CompositionMode_SourceIn` — do **not** ship one file per theme colour.

---

## 9. Doing all of this in Qt / PySide6 6.11.2

### 9.1 What is QSS-able, what needs QPainter, what needs an animation

| Visual feature | Mechanism | Notes |
|---|---|---|
| Flat fills, per-side border colours, radius 4/8, padding, font size/weight | **QSS** | Fully supported. |
| The elevation bottom-stroke | **QSS** (`border-bottom-color`) | See §7.2. |
| Hover / pressed / disabled / focus colour states | **QSS** pseudo-states `:hover :pressed :disabled :focus :checked` | No transition — colour snaps. Acceptable for 83 ms changes. |
| Linear gradients as backgrounds | **QSS** `qlineargradient(x1,y1,x2,y2, stop:…)` | Works; coordinates are 0–1 of the widget box, so the *absolute* 3 px WinUI gradient cannot be reproduced — use per-side border colours instead. |
| Rounded corners on a **top-level translucent** window | **QPainter** | QSS `border-radius` on a frameless translucent window leaves square corners under several Wayland compositors. Paint a `QPainterPath.addRoundedRect` yourself. |
| Drop shadows | **`QGraphicsDropShadowEffect`** | QSS has **no** `box-shadow`. See §9.5 for the Wayland caveat. |
| Toggle switch, progress ring, storage bar, status badges, focus ring | **QPainter** subclass | These are not stylable primitives. |
| Any animation (knob travel, knob size, chevron rotation, flyout fade) | **`QPropertyAnimation`** on a Python `Property` + `update()` | QSS cannot animate. |
| Opacity of a whole widget | `QGraphicsOpacityEffect` or `QWidget.setWindowOpacity` (top level) | Effects and shadows **cannot be stacked** on one widget — QGraphicsEffect is exclusive. |
| Text elision | `QFontMetrics.elidedText` in `paintEvent`, or `QLabel` + manual | QSS `text-overflow` does not exist. |
| Per-item hover in a list | `QStyledItemDelegate` + `QStyle.State_MouseOver` | QSS `::item:hover` works for simple cases but cannot do the 4 px-inset pill. Use a delegate. |

**QSS features that do *not* exist and will silently do nothing:** `box-shadow`, `transition`,
`transform`, `text-overflow`, `opacity` (on most widgets), `filter`, `backdrop-filter`, `:not()`,
CSS variables, `calc()`, `rem`/`em` units, and `linear-gradient()` (Qt's is `qlineargradient`).

Colour literal formats, **verified empirically on this machine**: `QColor("#AARRGGBB")` works
(`QColor("#800364B8")` → r3 g100 b184 **a128**); `QColor("#RRGGBBAA")` does **not**. In a stylesheet,
`rgba(3,85,164,0.9)`, `rgba(3,85,164,230)` and `rgba(3,85,164,90%)` all parse. Prefer `rgba()` with a
0–1 float in QSS and `#AARRGGBB` in Python `QColor` — and never write `#RRGGBBAA`, which silently
produces the wrong colour.

Also: **setting *any* QSS property on a widget replaces its native painting** for that widget's
background/border, and a `QPushButton` styled with a background loses its subcontrol layout. That is
fine here — we want full control.

### 9.2 Theme file skeleton

Generate QSS from a Python dict so light/dark share one template:

```python
LIGHT = dict(
    base="#F3F3F3", layer="#FFFFFF", card="#FBFBFB", card_hover="#F6F6F6",
    card_press="#F5F5F5", card_stroke="#E5E5E5", elev_bottom="#CCCCCC",
    divider="#E5E5E5", flyout_bg="#FCFCFC", flyout_stroke="#E5E5E5",
    text1="#1A1A1A", text2="#5C5C5C", text3="#868686", text_dis="#9B9B9B",
    subtle_hover="#EAEAEA", subtle_press="#EDEDED",
    accent="#0355A4", accent_hover="#1B65AC", accent_press="#3375B4",
    accent_dis="#BFBFBF", on_accent="#FFFFFF", on_accent_press="#B3FFFFFF",
    strong_stroke="#868686", strong_stroke_dis="#A6A6A6",
    toggle_off="#EDEDED", toggle_off_hover="#E5E5E5", toggle_off_press="#DCDCDC",
    success="#0F7B0F", caution="#9D5D00", critical="#C42B1C",
    focus_outer="#1A1A1A", focus_inner="#FFFFFF",
)
DARK = dict(
    base="#202020", layer="#2C2C2C", card="#2B2B2B", card_hover="#323232",
    card_press="#272727", card_stroke="#1D1D1D", elev_bottom="#303030",
    divider="#323232", flyout_bg="#2C2C2C", flyout_stroke="#1A1A1A",
    text1="#FFFFFF", text2="#CCCCCC", text3="#969696", text_dis="#717171",
    subtle_hover="#2D2D2D", subtle_press="#292929",
    accent="#36B2FC", accent_hover="#34A3E6", accent_press="#3295D0",
    accent_dis="#434343", on_accent="#000000", on_accent_press="#80000000",
    strong_stroke="#9A9A9A", strong_stroke_dis="#575757",
    toggle_off="#1D1D1D", toggle_off_hover="#2A2A2A", toggle_off_press="#303030",
    success="#6CCB5F", caution="#FCE100", critical="#FF99A4",
    focus_outer="#FFFFFF", focus_inner="#000000",
)
qss = QSS_TEMPLATE.format(**(DARK if dark_mode else LIGHT))
```

Dark-mode detection on GNOME: read `org.gnome.desktop.interface color-scheme` over the
`org.freedesktop.portal.Settings` D-Bus portal, and fall back to `gsettings get`. Repaint on change.

### 9.3 QSS — buttons (yields exactly 32 px, verified)

```css
/* ---------- Standard ---------- */
QPushButton {{
    background: {card};
    border: 1px solid {card_stroke};
    border-bottom-color: {elev_bottom};      /* the Fluent elevation stroke */
    border-radius: 4px;
    padding: 5px 11px 5px 11px;
    min-height: 20px;                        /* 20 + 5 + 5 + 1 + 1 = 32 */
    color: {text1};
    font-size: 14px;
}}
QPushButton:hover   {{ background: {card_hover}; }}
QPushButton:pressed {{ background: {card_press}; border-color: {card_stroke}; color: {text2}; }}
QPushButton:disabled{{ background: {card_press}; border-color: {card_stroke}; color: {text_dis}; }}

/* ---------- Accent ---------- */
QPushButton[accent="true"] {{
    background: {accent};
    border: 1px solid rgba(0,0,0,0.40);      /* ControlStrokeColorOnAccentSecondary #66000000 */
    border-top-color: rgba(255,255,255,0.08);/* ControlStrokeColorOnAccentDefault  #14FFFFFF */
    color: {on_accent};
}}
QPushButton[accent="true"]:hover   {{ background: {accent_hover}; }}
QPushButton[accent="true"]:pressed {{ background: {accent_press}; border-color: transparent;
                                      color: {on_accent_press}; }}
QPushButton[accent="true"]:disabled{{ background: {accent_dis}; border-color: transparent;
                                      color: {text_dis}; }}

/* ---------- Subtle / icon ---------- */
QPushButton[subtle="true"] {{ background: transparent; border: 1px solid transparent; color: {text1}; }}
QPushButton[subtle="true"]:hover   {{ background: {subtle_hover}; border-color: {subtle_hover}; }}
QPushButton[subtle="true"]:pressed {{ background: {subtle_press}; border-color: {subtle_press};
                                      color: {text2}; }}

/* ---------- Hyperlink ---------- */
QPushButton[link="true"] {{ background: transparent; border: none; color: {accent}; padding: 5px 4px; }}
QPushButton[link="true"]:hover {{ background: {subtle_hover}; border-radius: 4px; }}
```

Toggle a dynamic property then re-polish:
`btn.setProperty("accent", True); btn.style().unpolish(btn); btn.style().polish(btn)`.

### 9.4 QSS — text field, cards, list, nav

```css
QLineEdit {{
    background: {card};
    border: 1px solid {card_stroke};
    border-bottom: 1px solid {strong_stroke};   /* the WinUI "strong" bottom stroke */
    border-radius: 4px;
    padding: 4px 10px 4px 10px;
    min-height: 20px;                            /* -> 32 px total, verified */
    color: {text1}; font-size: 14px;
    selection-background-color: {accent};
    selection-color: {on_accent};
}}
QLineEdit:hover   {{ background: {card_hover}; }}
QLineEdit:focus   {{ background: {layer};
                     border-bottom: 2px solid {accent};
                     padding-bottom: 3px; }}   /* keep the box 32 px when the border grows */
QLineEdit:disabled{{ background: {card_press}; border-color: {card_stroke}; color: {text_dis}; }}

/* Settings card */
QFrame#SettingsCard {{
    background: {card};
    border: 1px solid {card_stroke};
    border-radius: 4px;
    min-height: 68px;
    padding: 16px;
}}
QFrame#SettingsCard[clickable="true"]:hover {{ background: {card_hover};
                                               border-bottom-color: {elev_bottom}; }}

/* Section heading */
QLabel[role="sectionHeader"] {{ font-size: 14px; font-weight: 600; color: {text1};
                                margin-top: 24px; margin-bottom: 8px; }}
QLabel[role="caption"]       {{ font-size: 12px; color: {text2}; }}

/* Nav */
QListWidget#Nav {{ background: transparent; border: none; outline: none; padding: 4px; }}
QListWidget#Nav::item {{ height: 36px; border-radius: 4px; padding-left: 12px;
                         margin: 2px 4px; color: {text1}; }}
QListWidget#Nav::item:hover    {{ background: {subtle_hover}; }}
QListWidget#Nav::item:selected {{ background: {subtle_hover}; color: {text1}; }}

/* Scrollbar: Windows 11 thin overlay */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {text3}; min-height: 24px;
                               border-radius: 2px; margin: 2px 4px 2px 6px; }}  /* 2px wide at rest */
QScrollBar::handle:vertical:hover {{ background: {text2}; margin: 2px 3px 2px 3px; }} /* 6px on hover */
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page
                              {{ background: none; border: none; height: 0; }}
```

The Windows 11 scrollbar is **2 px wide at rest, 6 px on hover**, inside a 12 px gutter, with the
arrow buttons only appearing on hover. Faking the width change with `margin` (as above) is the only
way to do it in QSS.

### 9.5 Shadows on Wayland — the caveat

`QGraphicsDropShadowEffect` paints **inside** the widget's own bounds, so a popup must reserve margin
for its shadow:

```python
SHADOW = 32   # blur radius
class Flyout(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                               | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW + 8)   # +8 for the y-offset
        self.body = QWidget(self); lay.addWidget(self.body)
        eff = QGraphicsDropShadowEffect(self.body)
        eff.setBlurRadius(SHADOW); eff.setOffset(0, 8)
        eff.setColor(QColor(0, 0, 0, 36))        # #24000000 light  (#47 = 71 for dark)
        self.body.setGraphicsEffect(eff)
```

Then the flyout's *logical* size is 360 + 2·32 = 424 wide. Position it accounting for that margin.
Because `WA_TranslucentBackground` + a `QGraphicsEffect` forces a software raster path, keep the
shadowed widget small — never put a shadow on the whole Settings window.

`Qt.WindowType.NoDropShadowWindowHint` stops the compositor from adding a *second* shadow.

### 9.6 Custom-painted Fluent ToggleSwitch (complete, matches §7.3)

```python
from PySide6.QtCore import (Property, QEasingCurve, QPointF, QPropertyAnimation,
                            QRectF, Qt, Signal)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton

def fluent_curve() -> QEasingCurve:
    c = QEasingCurve(QEasingCurve.Type.BezierSpline)
    c.addCubicBezierSegment(QPointF(0, 0), QPointF(0, 1), QPointF(1, 1))
    return c

class ToggleSwitch(QAbstractButton):
    TRACK_W, TRACK_H, BOX = 40, 20, 20          # WinUI: 40x20 track, 20x20 knob box
    KNOB, KNOB_HOVER = 12.0, 14.0               # rest / hover diameter
    KNOB_PRESS_W, KNOB_PRESS_H = 17.0, 14.0     # pressed stretch

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.TRACK_W, self.TRACK_H)
        self._pos = 0.0        # 0 = off, 1 = on
        self._kw = self.KNOB
        self._kh = self.KNOB
        self._anim_pos = QPropertyAnimation(self, b"knobPos", self)
        self._anim_pos.setDuration(150); self._anim_pos.setEasingCurve(fluent_curve())
        self._anim_w = QPropertyAnimation(self, b"knobW", self)
        self._anim_w.setDuration(83);  self._anim_w.setEasingCurve(fluent_curve())
        self._anim_h = QPropertyAnimation(self, b"knobH", self)
        self._anim_h.setDuration(83);  self._anim_h.setEasingCurve(fluent_curve())
        self.toggled.connect(self._on_toggled)

    def _get_pos(self): return self._pos
    def _set_pos(self, v): self._pos = v; self.update()
    knobPos = Property(float, _get_pos, _set_pos)
    def _get_kw(self): return self._kw
    def _set_kw(self, v): self._kw = v; self.update()
    knobW = Property(float, _get_kw, _set_kw)
    def _get_kh(self): return self._kh
    def _set_kh(self, v): self._kh = v; self.update()
    knobH = Property(float, _get_kh, _set_kh)

    def _animate(self, anim, to):
        anim.stop(); anim.setEndValue(to); anim.start()

    def _on_toggled(self, on): self._animate(self._anim_pos, 1.0 if on else 0.0)
    def enterEvent(self, e):
        self._animate(self._anim_w, self.KNOB_HOVER); self._animate(self._anim_h, self.KNOB_HOVER)
    def leaveEvent(self, e):
        self._animate(self._anim_w, self.KNOB); self._animate(self._anim_h, self.KNOB)
    def mousePressEvent(self, e):
        self._animate(self._anim_w, self.KNOB_PRESS_W); self._animate(self._anim_h, self.KNOB_PRESS_H)
        super().mousePressEvent(e)
    def mouseReleaseEvent(self, e):
        t = self.KNOB_HOVER if self.underMouse() else self.KNOB
        self._animate(self._anim_w, t); self._animate(self._anim_h, t)
        super().mouseReleaseEvent(e)

    def paintEvent(self, _):
        T = self.palette_tokens                      # your LIGHT/DARK dict
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled, hover, down = self.isEnabled(), self.underMouse(), self.isDown()
        track = QRectF(0.5, 0.5, self.TRACK_W - 1, self.TRACK_H - 1)

        # --- OFF track: fill + 1px strong stroke; ON track: accent fill, no stroke
        if not enabled:
            off_fill, off_stroke = QColor(0, 0, 0, 0), QColor(T["strong_stroke_dis"])
            on_fill = QColor(T["accent_dis"])
        else:
            off_fill = QColor(T["toggle_off_press"] if down else
                              T["toggle_off_hover"] if hover else T["toggle_off"])
            off_stroke = QColor(T["strong_stroke"])
            on_fill = QColor(T["accent_press"] if down else
                             T["accent_hover"] if hover else T["accent"])
        # cross-fade the two tracks by self._pos, exactly as the XAML opacity animation does
        p.setPen(QPen(off_stroke, 1)); p.setBrush(off_fill)
        p.setOpacity(1.0 - self._pos); p.drawRoundedRect(track, 10, 10)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(on_fill)
        p.setOpacity(self._pos);     p.drawRoundedRect(QRectF(0, 0, self.TRACK_W, self.TRACK_H), 10, 10)
        p.setOpacity(1.0)

        # --- knob: box travels 0 -> 20, knob centred in the 20px box (+/-1px optical nudge)
        box_x = self._pos * (self.TRACK_W - self.BOX)          # 0 .. 20
        cx = box_x + self.BOX / 2 + (1.0 if self._pos > 0.5 else -1.0)
        cy = self.TRACK_H / 2
        knob = QRectF(cx - self._kw / 2, cy - self._kh / 2, self._kw, self._kh)
        if not enabled:
            col = QColor(T["text_dis"]) if self._pos < .5 else QColor(T["on_accent"])
        else:
            col = QColor(T["text2"]) if self._pos < .5 else QColor(T["on_accent"])
        p.setBrush(col); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(knob, self._kh / 2, self._kh / 2)
```

### 9.7 Custom-painted focus ring (§3.4)

```python
def draw_focus_ring(p: QPainter, rect: QRectF, radius: float, outer: str, inner: str, inflate=3.0):
    r = rect.adjusted(-inflate, -inflate, inflate, inflate)
    rr = radius + inflate
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(outer), 2)); p.drawRoundedRect(r.adjusted(1, 1, -1, -1), rr, rr)
    p.setPen(QPen(QColor(inner), 1)); p.drawRoundedRect(r.adjusted(2, 2, -2, -2), rr - 1, rr - 1)
```

Call it from a `QProxyStyle.drawPrimitive` override for `PE_FrameFocusRect` so every widget gets it,
and set `QApplication.setStyle(QStyleFactory.create("Fusion"))` first. Verified on this machine,
`QStyleFactory.keys()` returns exactly `['Windows', 'Fusion']` — **Fusion** is the one to use; its
metrics do not fight QSS, and unlike a platform theme plugin it renders identically on GNOME, KDE and
any other desktop the user might run.

### 9.8 Rounded translucent popup body

```python
def paintEvent(self, _):
    p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
    path = QPainterPath(); path.addRoundedRect(r, 8, 8)
    p.fillPath(path, QColor(T["flyout_bg"]))
    p.setPen(QPen(QColor(T["flyout_stroke"]), 1)); p.drawPath(path)
```

### 9.9 HiDPI

Qt 6 enables high-DPI scaling by default; do not set the legacy attributes. Set
`QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)`
so 1.25×/1.5× GNOME fractional scaling produces fractional device pixels rather than rounding to 1× or
2× — otherwise every 1 px stroke in this document turns into a 2 px stroke. All measurements in this
document are **logical** px.

---

## 10. Cheat sheet

| Thing | Light | Dark |
|---|---|---|
| Window background | `#F3F3F3` | `#202020` |
| Content panel | `#F9F9F9` | `#282828` |
| Card / control fill | `#FBFBFB` | `#2B2B2B` |
| Card / control stroke | `#E5E5E5` | `#1D1D1D` |
| Elevation bottom stroke | `#CCCCCC` | (top) `#353535` |
| Divider | `#E5E5E5` | `#323232` |
| Flyout background / stroke | `#FCFCFC` / `#E5E5E5` | `#2C2C2C` / `#1A1A1A` |
| Text primary / secondary / tertiary / disabled | `#1A1A1A` / `#5C5C5C` / `#868686` / `#9B9B9B` | `#FFFFFF` / `#CCCCCC` / `#969696` / `#717171` |
| Hover / pressed overlay | `#EAEAEA` / `#EDEDED` | `#2D2D2D` / `#292929` |
| Accent rest / hover / pressed / disabled | `#0355A4` / `#1B65AC` / `#3375B4` / `#BFBFBF` | `#36B2FC` / `#34A3E6` / `#3295D0` / `#434343` |
| Text on accent | `#FFFFFF` | `#000000` |
| Success / caution / critical | `#0F7B0F` / `#9D5D00` / `#C42B1C` | `#6CCB5F` / `#FCE100` / `#FF99A4` |
| Strong stroke (toggle outline, progress track) | `#868686` | `#9A9A9A` |

| Thing | Value |
|---|---|
| Radius: control / overlay | 4 / 8 |
| Button, text field, chevron button | 32 px tall |
| List row / nav item / expander header | 40 / 36 / 48 |
| Settings card | 68 tall, 16 padding, 4 radius, 20 icon, 20 icon-gap |
| Activity row | 56 (2-line) / 48 (1-line) |
| Toggle | 40 × 20 track, 12 knob (14 hover, 17 × 14 press), travel 20 |
| Progress bar | 3 fill / 1 track, r 1.5 / 0.5; ring stroke 4 |
| Spacing scale | 4 8 12 16 20 24 32 |
| Focus ring | 2 px outer + 1 px inner, inflated 3 px, radius +3 |
| Durations | 83 / 150 / 250 (/350 storage bar) ms |
| Standard curve | `cubic-bezier(0, 0, 0, 1)` |
| Flyout | 360 wide, radius 8, shadow blur 32 dy 8 |
| Settings window | 1024 × 720, nav 320 (compact 48) |

---

## 11. Open items for the implementer

1. **Accent choice.** Ramp B (OneDrive brand `#0364B8`) is our default. Add a setting to follow the
   GNOME accent colour instead; the ramp transform in §1.3 generates the seven stops from any base.
2. **The Activity Center numbers in §5 are `[DERIVED]`.** Once we have a first build, screenshot it
   beside a real Windows 11 OneDrive flyout at 100 % scaling and reconcile.
3. **Selawik must be vendored** before any pixel measurement is trusted (§2.3).
4. **Fluent UI System Icons must be vendored** (MIT) — Segoe Fluent Icons cannot ship on Linux (§8.4).
5. The 2025 gradient OneDrive mark exists; decide per-surface whether to use it (large) or the flat
   2019 mark (≤ 32 px). §8.1.

---

## Appendix A — What was verified empirically on this machine (2026-08-30)

| Claim | How it was checked | Result |
|---|---|---|
| PySide6 / Qt version | `PySide6.__version__`, `QtCore.qVersion()` | **6.11.2 / 6.11.2** |
| Fluent standard curve is reproducible | `QEasingCurve(BezierSpline)` + `addCubicBezierSegment((0,0),(0,1),(1,1))` | `valueForProgress(0.5) == 0.8899` ✔ |
| A QSS button with `padding: 5px 11px; min-height: 20px; border: 1px` is 32 px | `QPushButton.sizeHint()` offscreen | `QSize(55, 32)` ✔ |
| Same recipe for `QLineEdit` (`padding: 4px 10px`, 1 px + 2 px bottom border) | `QLineEdit.sizeHint()` | `QSize(145, 32)` ✔ |
| Naive `padding: 5px 11px 11px 6px` without `min-height` overshoots | `sizeHint()` | `QSize(51, **33**)` ✘ — this is why `min-height: 20px` is mandatory |
| `Segoe UI Variable`, `Selawik`, `Inter` are absent | `fc-match` | all three resolve to **Noto Sans** — must vendor Selawik |
| `Cantarell` is present | `fc-match Cantarell` | `Cantarell-VF.otf` ✔ |
| Fallback font metrics | `QFontMetricsF` at 12/14/18/20/28 px | see §2.4 |
| `QColor` accepts `#AARRGGBB` | `QColor("#800364B8")` | r3 g100 b184 a128 ✔ |
| Available Qt styles | `QStyleFactory.keys()` | `['Windows', 'Fusion']` |
| WinUI colour tokens | parsed `Common_themeresources_any.xaml` (55,806 B) | 84 tokens × light/dark, zero dark-only keys |
| WinUI control geometry | parsed `generic.xaml` (2,045,878 B) + 8 per-control theme files | see §3.2 |
| OneDrive logo colours & path | official Microsoft asset via Wikimedia Commons | `#0364b8 #0078d4 #1490df #28a8ea`, viewBox `0 5.5 32 20.5` |
| OneDrive logo circle geometry | solved the arc endpoints from the path data | centres (8,18) r8, (16.25,15.50) r10, (25.5,19.5) r6.5, baseline y=26 — all mutually tangent ✔ |

## Appendix B — Source URLs

- `https://raw.githubusercontent.com/microsoft/microsoft-ui-xaml/main/dxaml/xcp/dxaml/themes/generic.xaml`
- `https://raw.githubusercontent.com/microsoft/microsoft-ui-xaml/main/controls/dev/CommonStyles/Common_themeresources_any.xaml`
- `https://raw.githubusercontent.com/microsoft/microsoft-ui-xaml/main/controls/dev/CommonStyles/{Button,TextBox,CornerRadius,Common,FlyoutPresenter}_themeresources.xaml`
- `https://raw.githubusercontent.com/microsoft/microsoft-ui-xaml/main/controls/dev/{ProgressBar,ProgressRing,Expander,NavigationView}/*_themeresources.xaml`
- `https://raw.githubusercontent.com/microsoft/microsoft-ui-xaml/winui2/main/dev/CommonStyles/ToggleSwitch_themeresources.xaml` ← **the Windows 11 toggle**
- `https://raw.githubusercontent.com/CommunityToolkit/Windows/main/components/SettingsControls/src/SettingsCard/SettingsCard.xaml`
- `https://raw.githubusercontent.com/CommunityToolkit/Windows/main/components/SettingsControls/src/SettingsExpander/SettingsExpander.xaml`
- `https://raw.githubusercontent.com/microsoft/fluentui/master/packages/tokens/src/{global/brandColors.ts,global/curves.ts,global/durations.ts,global/spacings.ts,global/borderRadius.ts,utils/shadows.ts,alias/lightColor.ts,alias/darkColor.ts}`
- `https://learn.microsoft.com/en-us/windows/apps/design/signature-experiences/typography`
- `https://learn.microsoft.com/en-us/windows/apps/design/style/acrylic`
- `https://learn.microsoft.com/en-us/windows/apps/develop/ui/system-backdrops`
- `https://support.microsoft.com/en-us/office/what-do-the-onedrive-icons-mean-11143026-8000-44f8-aaa9-67c985aa49b3`
- `https://valer100.github.io/winaccent/colors/accent-color-and-shades/` (default accent ramp)
- `https://upload.wikimedia.org/wikipedia/commons/5/59/Microsoft_Office_OneDrive_%282019%E2%80%932025%29.svg`
