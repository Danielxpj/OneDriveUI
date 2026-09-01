"""OneDriveUI's test suite.

Importing this package is enough to make Qt headless: `QT_QPA_PLATFORM` is set
before any PySide6 module is imported, here and in `conftest.py`, because a
QApplication built against the real compositor would open windows on the
developer's desktop during a test run.
"""

from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
#: Keep every duration deterministic: `theme.duration()` otherwise shells out to
#: `gsettings` and returns 0 on this machine, which would silently disable every
#: animation assertion.
os.environ.setdefault("ONEDRIVEUI_ANIMATIONS", "1")
