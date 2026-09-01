"""The Fluent widget kit.

Everything here paints with `ui.theme` tokens, animates through `ui.motion` and
is styled by `ui.qss`. Nothing in this package may import `sync/`, `rc/` or
`platform/`: the kit has to render in a standalone gallery with no rclone, no
daemon and no network, which is what makes a visual regression cheap to look at.

Import surface
--------------
`controls`    buttons, the Windows 11 toggle switch, text and choice fields,
              and the two-tone focus-ring proxy style
`indicators`  progress ring, progress bar, storage bar, avatar, status badges
"""

from __future__ import annotations

from onedriveui.ui.widgets.controls import (
    ButtonVariant, FluentButton, FluentCheckBox, FluentComboBox, FluentLineEdit,
    FluentRadioButton, FocusRingStyle, ThemeAware, ToggleSwitch, icon_button,
    indicator_rect, lerp_color, paint_focus_ring, restyle,
)
from onedriveui.ui.widgets.indicators import (
    ANGLE_UNIT, AVATAR_PALETTE, Avatar, BADGE_FRACTION, BAR_SEGMENT,
    FULL_CIRCLE, FluentProgressBar, INDETERMINATE_MS, ProgressRing,
    ProgressTone, QUOTA_CAUTION, QUOTA_CRITICAL, RING_DIAMETER, START_ANGLE,
    StatusBadge, StorageBar, SWEEP_MAX_DEG, SWEEP_MIN_DEG, avatar_colour,
    initials_for, paint_status_badge, status_badge_pixmap, status_badge_size,
)

__all__ = [
    # controls
    "ButtonVariant", "FluentButton", "FluentCheckBox", "FluentComboBox",
    "FluentLineEdit", "FluentRadioButton", "FocusRingStyle", "ThemeAware",
    "ToggleSwitch", "icon_button", "indicator_rect", "lerp_color",
    "paint_focus_ring", "restyle",
    # indicators
    "ANGLE_UNIT", "AVATAR_PALETTE", "Avatar", "BADGE_FRACTION", "BAR_SEGMENT",
    "FULL_CIRCLE", "FluentProgressBar", "INDETERMINATE_MS", "ProgressRing",
    "ProgressTone", "QUOTA_CAUTION", "QUOTA_CRITICAL", "RING_DIAMETER",
    "START_ANGLE", "StatusBadge", "StorageBar", "SWEEP_MAX_DEG",
    "SWEEP_MIN_DEG", "avatar_colour", "initials_for", "paint_status_badge",
    "status_badge_pixmap", "status_badge_size",
]
