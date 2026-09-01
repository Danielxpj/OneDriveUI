"""OneDriveUI's Qt layer.

Import order matters exactly once, at startup: `ui.theme` must be imported and
`ThemeManager.apply()` called BEFORE any widget is constructed, because setting a
stylesheet on a live widget tree is an O(widgets x rules) re-polish.

Nothing in this package may import `sync/`, `rc/` or `platform/` — the widgets
are driven entirely through `onedriveui.bus.BUS` and through service objects
injected by `app.py`. `ui/theme.py` and `ui/icons.py` are FROZEN CONTRACTS
(WP-00); everything else in here is owned by WP-11 … WP-13.

The widget kit (WP-11a), in the order a startup must touch it:

    ui.fonts    load the vendored faces, resolve the family against
                `QFontDatabase.families()`, build the pixel-sized type ramp.
                `fonts.apply_app_font(app)` runs BEFORE any widget exists.
    ui.qss      `theme.stylesheet()` + the widget-kit layer, validated for the
                five QSS workarounds, then `qss.apply(app)` — once.
    ui.motion   Fluent easing as explicit Bézier splines, every duration gated
                through `theme.duration()`, loops that stop when hidden.
    ui.widgets  the controls and indicators themselves.

Nothing is imported here: `ui.theme` has to be importable before `ui.icons`,
and `ui.icons` imports `ui.theme`, so an eager import in this module would be a
cycle at startup.
"""

from __future__ import annotations

__all__: list[str] = []
