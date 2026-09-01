"""Installing the Nautilus extension, the icons and the desktop entry.

Four things have to be true before an emblem appears on a file, and three of
them fail silently:

1. **The extension is where nautilus-python looks.**
   ``~/.local/share/nautilus-python/extensions/``, and nowhere else.
2. **The SVGs are installed into ``hicolor``.** Nautilus resolves an emblem by
   *name* against the icon theme. The user's theme here is breeze-dark, which
   has no ``emblem-onedriveui-*``, so resolution falls through to ``hicolor`` —
   which is exactly why our own SVGs have to be there and why this is the case
   that silently produces no emblem at all.
3. **The icon cache has been rebuilt.** ``gtk4-update-icon-cache -f -t``.
   Without it a freshly written SVG is invisible to a running GTK application.
4. **Nautilus has been restarted.** It does **not** hot-reload extensions.
   ``nautilus -q`` and the next window picks it up. There is no way to do this
   for the user without closing their file manager windows, so it is reported
   rather than done.

Every step reports what it did, because "I installed it and nothing happened" is
otherwise impossible to diagnose from the outside.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from onedriveui import paths
from onedriveui.platform import desktop
from onedriveui.ui import icons

log = logging.getLogger(__name__)

__all__ = ["InstallReport", "install", "uninstall", "is_installed",
           "extension_dir", "extension_path", "RESTART_HINT"]

#: What the user has to do that we cannot do for them.
RESTART_HINT: Final = (
    "Nautilus does not reload extensions while it is running. "
    "Run `nautilus -q` and open a new window."
)

#: The one directory nautilus-python looks in for a user-installed extension.
_EXT_SUBPATH: Final = Path("nautilus-python") / "extensions"

#: The filename the extension must have. nautilus-python imports it by stem.
_EXT_FILENAME: Final = "nautilus_onedriveui.py"


@dataclass(slots=True)
class InstallReport:
    """What actually happened, step by step.

    Every field is reported rather than logged and forgotten, because a
    half-installed extension looks exactly like a working one until a file needs
    an emblem.
    """

    extension: Path | None = None
    icons_written: int = 0
    icon_cache_rebuilt: bool = False
    desktop_entry: Path | None = None
    errors: list[str] = field(default_factory=list)
    hint: str = RESTART_HINT

    @property
    def ok(self) -> bool:
        return self.extension is not None and not self.errors


def extension_dir() -> Path:
    """``~/.local/share/nautilus-python/extensions``."""
    return paths.data_dir().parent / _EXT_SUBPATH


def extension_path() -> Path:
    """Where the extension file goes."""
    return extension_dir() / _EXT_FILENAME


def source_path() -> Path:
    """The extension in this checkout or installation."""
    return Path(__file__).resolve().parent / _EXT_FILENAME


def is_installed() -> bool:
    """Is the extension in place?"""
    return extension_path().exists()


# ═════════════════════════════════════════════════════════════════════════════
# Installing
# ═════════════════════════════════════════════════════════════════════════════

def install(*, symlink: bool = True, desktop_entry: bool = True) -> InstallReport:
    """Put the extension, the icons and the desktop entry in place.

    Args:
        symlink: Symlink the extension rather than copying it. Right for a
            development checkout — edits take effect on the next ``nautilus -q``
            — and wrong for a package, where the source may be on a filesystem
            the user's session cannot read.
        desktop_entry: Also install ``onedriveui.desktop``, which is what makes
            ``odopen:`` links work.

    Returns:
        An :class:`InstallReport`. Never raises: a failed install must say what
        failed, not disappear into a traceback the user has to interpret.
    """
    report = InstallReport()
    report.extension = _install_extension(symlink, report)
    report.icons_written = _install_icons(report)
    report.icon_cache_rebuilt = _rebuild_icon_cache(report)
    if desktop_entry:
        report.desktop_entry = _install_desktop_entry(report)

    if report.ok:
        log.info("Nautilus extension installed at %s. %s",
                 report.extension, RESTART_HINT)
    else:
        log.warning("the Nautilus extension did not install cleanly: %s",
                    "; ".join(report.errors))
    return report


def _install_extension(symlink: bool, report: InstallReport) -> Path | None:
    source = source_path()
    target = extension_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        if symlink:
            target.symlink_to(source)
        else:
            shutil.copy2(source, target)
    except OSError as exc:
        report.errors.append(f"could not install the extension: {exc}")
        return None
    return target


def _install_icons(report: InstallReport) -> int:
    """Write every tray, emblem and app SVG into ``hicolor``.

    The step that is invisible when it is skipped. The user's icon theme here is
    breeze-dark, which has no idea what ``emblem-onedriveui-cloud`` is, so
    resolution falls through to ``hicolor`` — and if ours are not there, every
    file gets no emblem and nothing anywhere reports an error.
    """
    try:
        icons.install_theme_icons()
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"could not install the icons: {exc}")
        return 0
    try:
        # `installed_icon_files()` reports the EXPECTED layout, not what is on
        # disk, so the existence check is the whole point: counting its keys
        # would report a successful install even when nothing was written.
        return sum(1 for path in icons.installed_icon_files().values()
                   if path.exists())
    except Exception:  # noqa: BLE001
        return 0


def _rebuild_icon_cache(report: InstallReport) -> bool:
    """``gtk4-update-icon-cache -f -t``.

    A freshly written SVG is invisible to a running GTK application until the
    cache is rebuilt, so skipping this produces the same symptom as not
    installing the icons at all.
    """
    base = paths.icon_theme_dir()
    for tool in ("gtk4-update-icon-cache", "gtk-update-icon-cache"):
        if shutil.which(tool) is None:
            continue
        try:
            result = subprocess.run([tool, "-f", "-t", str(base)],
                                    capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            report.errors.append(f"{tool} failed: {exc}")
            return False
        if result.returncode == 0:
            return True
        report.errors.append(
            f"{tool} exited {result.returncode}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()[:200]}")
        return False
    report.errors.append("no gtk-update-icon-cache; emblems may not appear "
                         "until the session restarts")
    return False


def _install_desktop_entry(report: InstallReport) -> Path | None:
    """The ``.desktop`` file, which is also what registers ``odopen:``."""
    try:
        return desktop.install_desktop_entry()
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"could not install the desktop entry: {exc}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Removing
# ═════════════════════════════════════════════════════════════════════════════

def uninstall(*, remove_icons: bool = False) -> InstallReport:
    """Remove the extension, and optionally the icons.

    Args:
        remove_icons: Also delete the installed SVGs. Off by default: they are
            small, harmless, and shared with the tray, so a user who removes the
            file-manager integration should not also lose their tray icon.

    Returns:
        An :class:`InstallReport` whose ``extension`` is ``None``.
    """
    report = InstallReport(extension=None)
    target = extension_path()
    try:
        if target.exists() or target.is_symlink():
            target.unlink()
    except OSError as exc:
        report.errors.append(f"could not remove the extension: {exc}")

    if remove_icons:
        for path in icons.installed_icon_files().values():
            try:
                os.unlink(path)
            except OSError:
                continue
        _rebuild_icon_cache(report)

    try:
        desktop.remove_nautilus_extension()
    except Exception:  # noqa: BLE001 - best effort; the file above is the real one
        log.debug("desktop.remove_nautilus_extension() failed", exc_info=True)

    log.info("Nautilus extension removed. %s", RESTART_HINT)
    return report


def restart_nautilus() -> bool:
    """Ask Nautilus to quit so the next window loads the extension.

    Returns:
        True when the command was issued.

    Offered but never done automatically during an install: ``nautilus -q``
    closes every file manager window the user has open, and doing that to
    somebody mid-task to make an emblem appear sooner is not a trade worth
    making on their behalf.
    """
    if shutil.which("nautilus") is None:
        return False
    try:
        subprocess.run(["nautilus", "-q"], capture_output=True, timeout=30,
                       check=False)
    except (OSError, subprocess.SubprocessError):
        log.warning("could not restart Nautilus", exc_info=True)
        return False
    return True
