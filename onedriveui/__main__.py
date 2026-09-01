"""The command line.

Most of it exists for the same reason: **a graphical client that can only be
asked questions graphically is impossible to support.** When a user reports that
sync is stuck, the useful next step is one line they can paste back, not a
screenshot of a tray tooltip.

    onedriveui --state        one SyncState, no GUI at all  (milestone M1)
    onedriveui --status       the full snapshot as JSON
    onedriveui --doctor       every check this client knows how to run
    onedriveui --diagnostics  a redacted bundle to attach to a report

``--state`` in particular is the whole engine — collector, reducer, supervisor —
with no window, no tray and no notifier. It is the smallest thing that proves
the engine works, and it stays useful forever after that as the answer to "what
does it actually think is happening?".

The default with no arguments is the GUI, started minimised to the tray, because
that is what an autostart entry runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from onedriveui import __version__

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    """Every command, with the reason each one exists in its help text."""
    parser = argparse.ArgumentParser(
        prog="onedriveui",
        description="A OneDrive client for Linux, built on rclone.")
    parser.add_argument("--version", action="version",
                        version=f"onedriveui {__version__}")

    what = parser.add_mutually_exclusive_group()
    what.add_argument("--state", action="store_true",
                      help="print the current sync state and exit (no GUI)")
    what.add_argument("--status", action="store_true",
                      help="print the full status snapshot as JSON and exit")
    what.add_argument("--doctor", action="store_true",
                      help="run every self-check and report what is wrong")
    what.add_argument("--diagnostics", metavar="PATH", nargs="?", const="-",
                      help="write a redacted diagnostics bundle")
    what.add_argument("--install-extension", action="store_true",
                      help="install the Nautilus extension and the icons")
    what.add_argument("--uninstall-extension", action="store_true",
                      help="remove the Nautilus extension")

    # The three desktop-entry actions. GNOME shows them on the launcher's
    # right-click menu, and `desktop-file-validate` does not check that the
    # commands exist — so a missing flag here is a menu item that silently does
    # nothing, discovered only by the user who tries it.
    what.add_argument("--open-folder", action="store_true",
                      help="open the OneDrive folder in the file manager")
    what.add_argument("--pause", metavar="HOURS", nargs="?", const="",
                      help="pause syncing for 2, 8 or 24 hours (default: "
                           "until you resume)")
    what.add_argument("--settings", action="store_true",
                      help="open the Settings window")

    # What the autostart unit runs. `platform/autostart.py` writes
    # `ExecStart=<command> --background`, so this flag is not optional garnish:
    # without it argparse exits 2, the unit fails, systemd retries five times
    # and gives up, and the client silently never starts at login.
    parser.add_argument("--background", action="store_true",
                        help="start minimised to the tray (used by autostart)")
    parser.add_argument("--account", metavar="ID",
                        help="which account to ask about (default: the active one)")
    parser.add_argument("--log-level", default=None,
                        metavar="LEVEL", help="DEBUG, INFO, WARNING, ERROR")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run whatever the arguments asked for.

    Returns:
        A process exit code. ``0`` for success, ``1`` for a reported failure,
        ``2`` for a usage error — so a shell script can branch on it.
    """
    args = build_parser().parse_args(argv)

    if args.install_extension:
        return _install_extension()
    if args.uninstall_extension:
        return _uninstall_extension()
    if args.state:
        return _print_state(args.account)
    if args.status:
        return _print_status(args.account)
    if args.doctor:
        return _doctor(args.account)
    if args.diagnostics is not None:
        return _diagnostics(args.diagnostics)
    if args.open_folder:
        return _open_folder(args.account)
    if args.pause is not None:
        return _pause(args.account, args.pause)
    if args.settings:
        return _run_gui(args, show_settings=True)
    return _run_gui(args)


# ═════════════════════════════════════════════════════════════════════════════
# Headless queries
# ═════════════════════════════════════════════════════════════════════════════

def _headless_engine(account_id: str | None) -> tuple[Any, Any]:
    """Build the engine with no UI, and take one observation.

    Returns:
        ``(application, engine)``, or ``(app, None)`` when no account is
        configured.

    The whole engine runs: the fact collector reads the mount, the daemons, the
    database and the session bus, and the reducer decides. That is what makes
    the answer worth printing — it is the same answer the tray icon is showing,
    arrived at the same way, not a separate guess made for the command line.
    """
    from onedriveui.app import Application

    app = Application([], headless=True)
    accounts = app.accounts()
    if not accounts:
        return app, None
    # `accounts[0]` is the wrong default. With more than one account configured
    # it answers about whichever happens to be first in the file, which is not
    # the one the tray is showing and not the one the user last chose — the
    # symptom is `--status` naming an account you did not ask about. The active
    # account is what every other surface means by "the account".
    active = None
    picker = getattr(getattr(app, "config", None), "account", None)
    if callable(picker):
        try:
            entry = picker()
            active = getattr(entry, "id", None)
        except Exception:  # noqa: BLE001 - a broken config still gets an answer
            active = None
    wanted = account_id or active
    chosen = next((a for a in accounts if a.id == wanted), accounts[0]) \
        if wanted else accounts[0]
    engine = app.engine_for(chosen)

    # One forced `operations/about` before the tick. The quota is normally a
    # scheduled job on a five-minute TTL, and a one-shot command never reaches
    # its first firing — so without this `--status` reports 0 bytes of 0 and
    # `--doctor` says "token valid" about a token it never asked about. Both are
    # questions the user asked out loud; one Graph request is the honest cost of
    # answering them.
    quota = engine.services.get("quota")
    if quota is not None:
        try:
            quota.refresh(force=True)
        except Exception:  # noqa: BLE001 - an unreachable drive is a finding,
            pass                                        # not a crash

    # `collector.tick()`, NOT `supervisor._on_collected(...)`. Collecting facts
    # is an observation; `_on_collected` is where the ladder's *effects* run, and
    # those include restarting and force-unmounting the mount. Asking "what is
    # the state?" must never be able to unmount a live filesystem — and a
    # one-shot command always transitions out of INITIALIZING, so it fired the
    # transition effects every single time. Every reader below reduces from
    # `collector.last()`, which `tick()` fills on its own.
    engine.supervisor.collector.tick()
    return app, engine


def _print_state(account_id: str | None) -> int:
    """One word on stdout. Milestone M1."""
    app, engine = _headless_engine(account_id)
    try:
        if engine is None:
            print("signed_out")
            return 1
        # The raw ladder result, not the debounced one: the debouncer needs
        # several ticks to publish and there is only ever one tick here, so
        # printing its output would always say "initializing".
        from onedriveui.sync.reducer import reduce

        print(reduce(engine.supervisor.collector.last()).value)
        return 0
    finally:
        _shutdown(app, engine)


def _print_status(account_id: str | None) -> int:
    """The whole snapshot, as JSON, for a script or a bug report."""
    app, engine = _headless_engine(account_id)
    try:
        if engine is None:
            print(json.dumps({"state": "signed_out",
                              "detail": "no account is configured"}, indent=2))
            return 1
        from onedriveui.sync.reducer import explain, reduce, status_text

        facts = engine.supervisor.collector.last()
        rung, state = explain(facts)
        headline, subtext = status_text(state, facts)
        print(json.dumps({
            "account": engine.account.id,
            "state": state.value,
            "rung": rung,
            "headline": headline,
            "subtext": subtext,
            "sampled_at": facts.sampled_at,
            # `stale` is the field that explains a surprising answer: a state
            # derived from carried-over values is worth flagging rather than
            # presenting as a fresh observation.
            "stale_sources": sorted(facts.stale),
            "daemon_rcd": facts.daemon_rcd.value,
            "mount": facts.mount.value,
            "token": facts.token.value,
            "transfers_active": facts.transfers_active,
            "uploads_queued": facts.uploads_queued,
            "issues": {"blocking": facts.issues_blocking,
                       "error": facts.issues_error,
                       "warning": facts.issues_warning},
            "latches": sorted(facts.latches),
            "quota": {"total": facts.quota.total, "used": facts.quota.used,
                      "free": facts.quota.free, "tier": facts.quota.tier},
        }, indent=2))
        return 0
    finally:
        _shutdown(app, engine)


def _doctor(account_id: str | None) -> int:
    """Every check this client knows how to run, in one place.

    Returns:
        ``0`` when everything passed.

    Deliberately verbose and deliberately ordered from "is anything installed?"
    to "is anything wrong?", because a user running this has already decided
    something is broken and the useful output is the first line that says ``no``.
    """
    from onedriveui.ext import install as ext_install
    from onedriveui.ui import icons

    checks: list[tuple[str, bool, str]] = []

    app, engine = _headless_engine(account_id)
    try:
        checks.append(("account configured", engine is not None,
                       "run the setup wizard" if engine is None else ""))
        checks.append(("Nautilus extension installed", ext_install.is_installed(),
                       "run: onedriveui --install-extension"))
        # Existence, not the expected layout: `installed_icon_files()` answers
        # "where would they go", so counting its keys would report every icon
        # present on a machine where none were ever written.
        expected = icons.installed_icon_files()
        present = sum(1 for path in expected.values() if path.exists())
        checks.append((f"icons installed in hicolor ({present}/{len(expected)})",
                       present == len(expected),
                       "run: onedriveui --install-extension"))

        if engine is not None:
            facts = engine.supervisor.collector.last()
            checks.append(("control daemon reachable",
                           facts.daemon_rcd.value == "up",
                           f"daemon is {facts.daemon_rcd.value}"))
            checks.append(("mount live", facts.mount.value == "up",
                           f"mount is {facts.mount.value}"))
            checks.append(("token valid", facts.token.value in ("ok", "unknown"),
                           f"token is {facts.token.value}"))
            checks.append(("no blocking issues", facts.issues_blocking == 0,
                           f"{facts.issues_blocking} blocking issue(s)"))
            checks.append(("no hazard latches", not facts.latches,
                           f"latched: {', '.join(sorted(facts.latches))}"))
            checks.append(("every fact source answered", not facts.stale,
                           f"stale: {', '.join(sorted(facts.stale))}"))

        failed = 0
        for label, ok, detail in checks:
            mark = "ok  " if ok else "FAIL"
            print(f"[{mark}] {label}" + (f" — {detail}" if not ok and detail else ""))
            failed += 0 if ok else 1
        return 0 if failed == 0 else 1
    finally:
        _shutdown(app, engine)


def _diagnostics(target: str) -> int:
    """A redacted bundle: logs, config, rclone version, environment."""
    from onedriveui import applog

    try:
        bundle = applog.build_diagnostics_bundle()
    except Exception as exc:  # noqa: BLE001
        print(f"could not build the diagnostics bundle: {exc}", file=sys.stderr)
        return 1
    if target == "-":
        print(applog.bundle_text(bundle))
    else:
        import shutil

        shutil.copy2(bundle, target)
        print(target)
    return 0


# ═════════════════════════════════════════════════════════════════════════════
# Installation
# ═════════════════════════════════════════════════════════════════════════════

def _install_extension() -> int:
    from onedriveui.ext import install as ext_install

    report = ext_install.install()
    if report.extension:
        print(f"extension: {report.extension}")
    print(f"icons: {report.icons_written} written, "
          f"cache {'rebuilt' if report.icon_cache_rebuilt else 'NOT rebuilt'}")
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    # Printed even on success, because it is the step the user has to do and
    # the one that makes the difference between "installed" and "working".
    print(report.hint)
    return 0 if report.ok else 1


def _uninstall_extension() -> int:
    from onedriveui.ext import install as ext_install

    report = ext_install.uninstall()
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    print(report.hint)
    return 0 if not report.errors else 1


# ═════════════════════════════════════════════════════════════════════════════
# The GUI
# ═════════════════════════════════════════════════════════════════════════════

def _run_gui(args: argparse.Namespace, *, show_settings: bool = False) -> int:
    """Run the client, or hand the request to the copy already running.

    The single-instance guard was written, tested and never installed. Without
    it every launcher click, every `odopen:` link and every "Settings" item in
    the desktop entry's context menu started a **second complete client** —
    another tray icon, another engine, another set of pollers, and two
    processes writing the same SQLite file and driving the same mount. Two
    mounts of one remote is the configuration that has already destroyed a file
    on this machine, so this is a safety guard as much as a tidiness one.
    """
    from PySide6.QtWidgets import QApplication

    from onedriveui.app import Application
    from onedriveui.platform.singleinstance import SingleInstance

    # The QApplication first, because QLocalServer needs one — then the guard,
    # and only then `Application`, which opens the database.
    #
    # The order matters: `Application.__init__` runs `db.integrity_check()`,
    # which **renames a database it judges corrupt out of the way**, and starts
    # the shared writer on it. Doing that before discovering we are not the
    # primary instance would have a second launch touch the file the running
    # client is writing. `Application` adopts this QApplication rather than
    # building its own.
    qt = QApplication.instance() or QApplication(sys.argv[:1])

    guard = SingleInstance()
    if not guard.try_acquire():
        # Someone is already running. Hand them the request and leave: a second
        # icon in the tray is not what the user asked for by clicking Settings.
        guard.send(sys.argv)
        return 0

    app = Application(sys.argv[:1])
    if args.log_level:
        from onedriveui import applog

        applog.set_level(args.log_level)

    def _second_launch(argv: list) -> None:
        """A second copy was started; show what it asked for."""
        accounts = app.accounts()
        if not accounts:
            return
        wanted = accounts[0].id
        if "--settings" in argv:
            app.open_settings(wanted)
        else:
            app.open_activity(wanted)

    guard.message.connect(_second_launch)

    app.start()
    if show_settings:
        accounts = app.accounts()
        if accounts:
            app.open_settings(args.account or accounts[0].id)
    try:
        return app.exec()
    finally:
        guard.release()


def _open_folder(account_id: str | None) -> int:
    """Open the sync root in the file manager, without starting the engine.

    Deliberately does not build an engine: this is a launcher menu item, and
    making it wait for a fact tick before opening a folder would be a second of
    nothing happening for no benefit.
    """
    from onedriveui import config
    from onedriveui.platform import desktop

    cfg = config.load()
    account = cfg.account(account_id)
    if account is None:
        print("no account is configured", file=sys.stderr)
        return 1
    return 0 if desktop.open_path(account.resolved_sync_root()) else 1


def _pause(account_id: str | None, hours: str) -> int:
    """Pause a running instance from the launcher menu.

    Talks to the running application rather than pausing "locally": a pause
    written to disk by a second process would be invisible to the one that is
    actually enforcing it until its next config reload, and the queue would keep
    draining in the meantime.
    """
    from onedriveui.platform.ipc import IpcClient

    try:
        parsed: int | None = int(hours) if hours else None
    except ValueError:
        print(f"--pause takes a number of hours, not {hours!r}", file=sys.stderr)
        return 2

    client = IpcClient()
    # `send()` returns False when the socket was never opened, and `request()`
    # reports that as "not running" — so without this line the launcher's
    # "Pause syncing" always failed, whether or not the client was running.
    if not client.connect():
        print("OneDriveUI is not running", file=sys.stderr)
        return 1
    try:
        reply = client.request({"op": "do", "v": 1, "action": "pause",
                                "hours": parsed, "paths": []})
    finally:
        client.close()
    if not reply or reply.get("op") != "ok":
        print("OneDriveUI did not accept the pause", file=sys.stderr)
        return 1
    return 0


def _shutdown(app: Any, engine: Any) -> None:
    """Stop cleanly, so the next start-up does not report an interrupted run."""
    try:
        if engine is not None:
            engine.supervisor.stop()
        if app is not None and getattr(app, "writer", None) is not None:
            app.writer.stop()
    except Exception:  # noqa: BLE001 - we are exiting either way
        pass


if __name__ == "__main__":
    raise SystemExit(main())
