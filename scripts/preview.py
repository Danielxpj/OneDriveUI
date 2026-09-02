#!/usr/bin/env python3
"""Open — or screenshot — the real windows, with no engine behind them.

`scripts/gallery.py` renders the *widget kit*. This renders the *windows*: the
Settings shell, the Activity Center, the setup wizard and every dialog, built
exactly as the application builds them and driven by stubs instead of a running
engine.

That separation is the point. These windows have to be inspectable without an
rclone daemon, a mount, a network or an account, because otherwise the only way
to look at the mass-delete dialog is to delete four thousand files. Every one of
them takes its services by injection, so a stub is a complete substitute.

Usage::

    python3 scripts/preview.py --list                 # what can be shown
    python3 scripts/preview.py settings               # open it on your display
    python3 scripts/preview.py --all --shot /tmp/ui   # PNG of each, offscreen
    python3 scripts/preview.py mass_delete            # one dialog

Nothing here writes outside the directory `--shot` names. The config is built in
memory and never saved, so running this cannot leave a `config.json` behind.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _stub_services() -> dict[str, Any]:
    """Just enough of an engine for a window to render against.

    Deliberately hand-written rather than imported from `tests.fakes`: this
    script has to keep working if the test fakes are refactored, and what it
    needs is four attributes rather than a faithful simulation.
    """
    from onedriveui.models import QuotaInfo

    class Quota:
        def current(self):
            return QuotaInfo(total=1_104_880_336_896, used=252_544_077_005,
                             free=852_336_259_891)

    class Supervisor:
        def do(self, action, **kw):
            print(f"[preview] do({action}, {kw})")

        def request_pause(self, reason, hours):
            print(f"[preview] pause({reason}, {hours})")

        def request_resume(self):
            print("[preview] resume()")

        def reclaim_orphaned_cache(self):
            return 48_200_000_000

    return {"quota": Quota(), "supervisor": Supervisor()}


def _account():
    from onedriveui.models import AccountInfo

    return AccountInfo(id="onedrive", remote="onedrive",
                       display_name="Daniel Dughman",
                       email="daniel@example.com",
                       sync_root=str(Path.home() / "OneDrive"))


def _config():
    """An in-memory config with one account. Never saved."""
    from onedriveui import config
    from tests.conftest import default_config

    return config.AppConfig.from_dict(default_config())


# ═════════════════════════════════════════════════════════════════════════════
# The windows
# ═════════════════════════════════════════════════════════════════════════════

def _settings() -> Any:
    from onedriveui.ui.settings_window import SettingsWindow

    services = _stub_services()
    return SettingsWindow(_account(), config=_config(),
                          supervisor=services["supervisor"], services=services)


def _activity() -> Any:
    """The flyout with a transfer in flight and a few finished rows behind it.

    Two rows left it half the height it has in use and clipped the last one
    mid-line, which is a screenshot that misrepresents the window. The height
    is content-driven, so the fix is content: a realistic list, then the resize
    the application itself does through `preferred_height()`.
    """
    from onedriveui.models import ActivityState, ActivityVerb, SyncState
    from onedriveui.ui.activity_center import ActivityCenter
    from tests.fakes.fake_services import FakeSupervisor

    services = _stub_services()
    supervisor = FakeSupervisor(_account())
    window = ActivityCenter(_account(), supervisor=supervisor,
                            quota=services["quota"])

    supervisor.emit_activity("Documents/Quarterly Report.docx",
                             ActivityVerb.UPLOADED,
                             state=ActivityState.INFLIGHT,
                             size=18_400_000, done=7_900_000)
    supervisor.emit_activity("Photos/holiday.jpg", ActivityVerb.UPLOADED,
                             size=4_200_000)
    supervisor.emit_activity("Budget 2026.xlsx", ActivityVerb.MODIFIED,
                             size=812_000)
    supervisor.emit_activity("Design/logo.svg", ActivityVerb.DOWNLOADED,
                             size=96_000)
    supervisor.emit_activity("Archive/2019", ActivityVerb.FREED,
                             size=2_100_000_000)
    supervisor.set_state(SyncState.SYNCING)

    window.resize(window.WIDTH, window.preferred_height())
    return window


def _wizard(page: str = "welcome") -> Any:
    """The setup wizard, opened on `page`.

    The welcome screen is the one the user sees first and the least
    interesting to look at: it is a title and an email box. The folder and
    tutorial pages are where the wizard actually says something, so they are
    reachable by name rather than by clicking Next four times.
    """
    from onedriveui.ui.wizard import PAGES, SetupWizard

    wizard = SetupWizard(_account(), config=_config(),
                         services=_stub_services())
    while wizard.current_key != page and wizard.current_key != PAGES[-1]:
        wizard.next_page()
    return wizard


WINDOWS: dict[str, Callable[[], Any]] = {
    "settings": _settings,
    "activity": _activity,
    "wizard": _wizard,
    "wizard_folder": lambda: _wizard("folder"),
    "wizard_tutorial": lambda: _wizard("tutorial"),
    "wizard_done": lambda: _wizard("done"),
}


# ═════════════════════════════════════════════════════════════════════════════
# The dialogs
# ═════════════════════════════════════════════════════════════════════════════

def _dialogs() -> dict[str, Callable[[], Any]]:
    from onedriveui.ui.dialogs.file_dialogs import (
        ConflictDialog,
        DownloadAllDialog,
        FreeUpSpaceDialog,
        ShareDialog,
        VersionHistoryDialog,
    )
    from onedriveui.ui.dialogs.misc_dialogs import (
        ChooseFoldersDialog,
        DiskUsageNoteDialog,
        QuitDialog,
        VaultDialog,
    )
    from onedriveui.ui.dialogs.sync_dialogs import (
        FirstDeleteDialog,
        MassDeleteDialog,
        ResetDialog,
        ResyncDialog,
        StopBackupDialog,
        UnlinkDialog,
    )

    return {
        # The one worth looking at first: its PRIMARY button is "Restore files".
        "mass_delete": lambda: MassDeleteDialog(4231),
        "first_delete": lambda: FirstDeleteDialog("holiday.jpg"),
        "resync": ResyncDialog,
        "unlink": UnlinkDialog,
        "reset": ResetDialog,
        "stop_backup": lambda: StopBackupDialog("Desktop",
                                                offer_this_computer_only=True),
        "free_up": lambda: FreeUpSpaceDialog(48_200_000_000),
        "download_all": lambda: DownloadAllDialog(252_544_077_005),
        # "Remove link" is present and DISABLED, with its reason.
        "share": lambda: ShareDialog("Documents/Quarterly Report.docx"),
        "versions": lambda: VersionHistoryDialog("Documents/Report.docx"),
        "conflict": lambda: ConflictDialog("Budget.xlsx", "Budget-laptop.xlsx"),
        "quit": QuitDialog,
        "choose_folders": ChooseFoldersDialog,
        "vault": VaultDialog,
        "disk_usage": DiskUsageNoteDialog,
    }


def apply_theme(app: Any, *, dark: bool) -> None:
    """Install the stylesheet exactly as `app.py` does.

    Not optional decoration. Qt applies a stylesheet to widgets as they are
    polished, so a window built before the sheet is installed keeps Fusion's
    defaults for the rest of its life — the visible symptom is a half-styled
    window, dark cards on a light background, which looks like a design bug in
    the window rather than a missing call in the harness.

    `ThemeManager.apply()` is deliberately not used: it calls
    `setStyle("Fusion")`, which would replace the `FocusRingStyle` proxy and
    take the two-tone focus ring with it.
    """
    from onedriveui.models import ThemeMode
    from onedriveui.ui import icons, qss, theme
    from onedriveui.ui.widgets.controls import FocusRingStyle

    qss.ensure_fusion(app)
    app.setStyle(FocusRingStyle(app.style()))

    manager = theme.ThemeManager(app)
    manager.set_mode(ThemeMode.DARK if dark else ThemeMode.LIGHT)
    manager.start()
    # The palette, which `apply()` would have set and which the sheet does not
    # replace: QSS never paints a QScrollArea's viewport or a QStackedWidget's
    # backdrop, so without this a dark shot comes out with dark cards floating
    # on Fusion's light `Window` — the harness looking like a theme bug.
    manager.apply_palette(app)
    theme.invalidate_detection()
    qss.invalidate()
    icons.clear_cache()
    qss.apply(app, dark=dark)
    globals()["_MANAGER"] = manager        # keep it alive for the session


_MANAGER: Any = None


def settle(app: Any, ms: int) -> None:
    """Pump the event loop for `ms`, then let the widgets repaint.

    A screenshot taken the instant a window is shown catches its animations
    mid-flight: `ToggleSwitch` slides its knob over 150 ms, so a switch that is
    ON renders as OFF and the shot documents a state the application never had.
    Waiting is not politeness, it is correctness.
    """
    from PySide6.QtCore import QElapsedTimer
    from PySide6.QtWidgets import QApplication

    clock = QElapsedTimer()
    clock.start()
    while clock.elapsed() < ms:
        app.processEvents()
    # Qt hands the keyboard to the first focusable widget when a window is
    # shown, so without this every shot carries a focus ring on whatever
    # happens to be first in the tab order.
    focused = QApplication.focusWidget()
    if focused is not None:
        focused.clearFocus()
        app.processEvents()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open or screenshot OneDriveUI's windows, with no engine.")
    parser.add_argument("name", nargs="?", help="which window or dialog")
    parser.add_argument("--list", action="store_true", help="list them all")
    parser.add_argument("--all", action="store_true", help="every one of them")
    parser.add_argument("--shot", metavar="DIR",
                        help="render to PNG instead of opening (implies offscreen)")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--dpr", type=float, default=1.0,
                        help="render shots at this device pixel ratio (2 = HiDPI)")
    parser.add_argument("--settle", type=int, default=600, metavar="MS",
                        help="pump the event loop this long before grabbing")
    args = parser.parse_args(argv)

    if args.shot:
        import os

        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        # HiDPI the way a HiDPI screen does it: the window keeps its logical
        # size and every primitive — text, the SVG glyphs, the toggle tracks —
        # is painted at `dpr` times the resolution, which is what makes a
        # screenshot legible on a retina display. It must be set before
        # QApplication exists, because that is when Qt reads it.
        if args.dpr and args.dpr != 1.0:
            os.environ["QT_SCALE_FACTOR"] = str(args.dpr)

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    apply_theme(app, dark=args.theme == "dark")

    everything = {**WINDOWS, **_dialogs()}

    if args.list or (not args.name and not args.all):
        print("windows:", " ".join(sorted(WINDOWS)))
        print("dialogs:", " ".join(sorted(_dialogs())))
        return 0

    names = sorted(everything) if args.all else [args.name]
    unknown = [n for n in names if n not in everything]
    if unknown:
        print(f"unknown: {', '.join(unknown)}", file=sys.stderr)
        return 2

    built = []
    for name in names:
        widget = everything[name]()
        widget.setWindowTitle(f"OneDriveUI — {name}")
        widget.show()
        built.append((name, widget))

    if not args.shot:
        return int(app.exec())

    out = Path(args.shot)
    out.mkdir(parents=True, exist_ok=True)
    settle(app, args.settle)
    for name, widget in built:
        settle(app, 120)
        path = out / f"{name}-{args.theme}.png"
        shot = widget.grab()
        shot.setDevicePixelRatio(1.0)
        shot.save(str(path), "PNG")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
