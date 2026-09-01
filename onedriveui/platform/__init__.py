"""Platform integration: GLib/D-Bus, notifications, power policy, systemd.

Everything in this package rides **one** event loop. Qt owns the process's main
loop; GLib's default `MainContext` is drained from a 50 ms `QTimer` installed by
`glibpump.install()`. That single pump is what makes `Gio.DBusConnection`,
`Gio.NetworkMonitor`, `Gio.PowerProfileMonitor`, `Gio.FileMonitor` and the
`org.freedesktop.systemd1` proxies work with **no extra threads** — and if it
stalls, every one of them stops delivering *silently*. Treat it as critical
path: `glibpump` is installed before any other module here is constructed.

Why Gio and not `PySide6.QtDBus`: PySide6 6.11.2 cannot marshal a D-Bus
`uint32`, so `org.freedesktop.Notifications.Notify` (signature
`susssasa{sv}i`) is literally uncallable from Qt. See `notify` for the details.

Submodules are imported lazily so that `import onedriveui.platform` costs
nothing and has no side effects; `from onedriveui.platform import notify` and
`onedriveui.platform.notify` both work.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__all__ = [
    "dbus",
    "glibpump",
    "notify",
    "power",
    "systemd",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from onedriveui.platform import dbus, glibpump, notify, power, systemd


def __getattr__(name: str) -> Any:
    """Import a submodule on first attribute access.

    Args:
        name: The submodule name, which must appear in `__all__`.

    Returns:
        The imported submodule.

    Raises:
        AttributeError: If `name` is not one of this package's submodules.
    """
    if name in __all__:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals().keys(), *__all__])
