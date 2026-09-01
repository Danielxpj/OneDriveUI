"""Metered network, battery saver and connectivity — the auto-pause inputs.

Two Gio monitors do almost all of the work, and both come free once
`glibpump` is running:

* `Gio.NetworkMonitor` — `get_network_available()`, `get_network_metered()`,
  `get_connectivity()`, plus the `network-changed` signal. Its metered answer is
  NetworkManager's `Metered` property reduced to a bool, True for
  `NM_METERED_YES` (1) and `NM_METERED_GUESS_YES` (3).
* `Gio.PowerProfileMonitor` — `power-saver-enabled`, i.e. GNOME's "Power Saver"
  mode, which is what the Windows client calls battery saver.

Three D-Bus fallbacks cover the cases where those return a base-class default:

* NetworkManager `Metered` (system bus) — the raw `u`, exposed as
  `nm_metered_value()` because "guessed not metered" (4) and "explicitly not
  metered" (2) are different facts and the About pane shows which one we saw.
* UPower `OnBattery` (system bus) — reported separately from battery saver.
  Windows only *pauses* on metered; on battery it merely throttles, so
  `PowerState` deliberately does **not** flip to `SAVER` just because the
  machine is unplugged.
* power-profiles-daemon `ActiveProfile` — try `org.freedesktop.UPower.PowerProfiles`
  first and fall back to the legacy `net.hadess.PowerProfiles`; both are live on
  the target machine, older distributions only ship the latter.

Measured on the target machine, 2026-08-30: `Metered = uint32 4` (GUESS_NO),
`State = 70` (CONNECTED_GLOBAL), `Connectivity = 4` (FULL), `OnBattery = false`,
no battery device at all (a desktop), `ActiveProfile = 'performance'`. So
`metered()` is False here, and must stay False.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from gi.repository import Gio, GLib
from PySide6.QtCore import QObject, Signal

from onedriveui.models import NetworkState, PauseReason, PowerState
from onedriveui.platform import glibpump
from onedriveui.platform.dbus import Bus
from onedriveui.platform.glibpump import assert_gui_thread

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# NetworkManager
# ─────────────────────────────────────────────────────────────────────────────

NM_NAME: Final[str] = "org.freedesktop.NetworkManager"
NM_PATH: Final[str] = "/org/freedesktop/NetworkManager"
NM_IFACE: Final[str] = "org.freedesktop.NetworkManager"

#: `NMMetered`, from NetworkManager's own enum.
NM_METERED_UNKNOWN: Final[int] = 0
NM_METERED_YES: Final[int] = 1
NM_METERED_NO: Final[int] = 2
NM_METERED_GUESS_YES: Final[int] = 3
NM_METERED_GUESS_NO: Final[int] = 4

#: The two values that mean "treat this connection as metered". 3 is a guess
#: (a phone tether, typically) and the user may override it; 1 is explicit.
NM_METERED_TRUE: Final[frozenset[int]] = frozenset({NM_METERED_YES, NM_METERED_GUESS_YES})

#: `NMConnectivityState`, for the label the About pane shows.
NM_CONNECTIVITY_UNKNOWN: Final[int] = 0
NM_CONNECTIVITY_NONE: Final[int] = 1
NM_CONNECTIVITY_PORTAL: Final[int] = 2
NM_CONNECTIVITY_LIMITED: Final[int] = 3
NM_CONNECTIVITY_FULL: Final[int] = 4

# ─────────────────────────────────────────────────────────────────────────────
# UPower and power-profiles-daemon
# ─────────────────────────────────────────────────────────────────────────────

UPOWER_NAME: Final[str] = "org.freedesktop.UPower"
UPOWER_PATH: Final[str] = "/org/freedesktop/UPower"
UPOWER_IFACE: Final[str] = "org.freedesktop.UPower"

#: `(bus name, object path, interface)`, tried in order. The modern name first.
POWER_PROFILE_SERVICES: Final[tuple[tuple[str, str, str], ...]] = (
    ("org.freedesktop.UPower.PowerProfiles",
     "/org/freedesktop/UPower/PowerProfiles",
     "org.freedesktop.UPower.PowerProfiles"),
    ("net.hadess.PowerProfiles",
     "/net/hadess/PowerProfiles",
     "net.hadess.PowerProfiles"),
)
PROFILE_POWER_SAVER: Final[str] = "power-saver"
PROP_ACTIVE_PROFILE: Final[str] = "ActiveProfile"
PROP_METERED: Final[str] = "Metered"
PROP_CONNECTIVITY: Final[str] = "Connectivity"
PROP_ON_BATTERY: Final[str] = "OnBattery"

PROPERTIES_CHANGED: Final[str] = "PropertiesChanged"
PROPERTIES_IFACE: Final[str] = "org.freedesktop.DBus.Properties"


def nm_metered_is_metered(value: int) -> bool:
    """Whether a raw `NMMetered` value means "metered".

    Args:
        value: The raw `Metered` property, 0-4.

    Returns:
        True for `NM_METERED_YES` and `NM_METERED_GUESS_YES` only.
    """
    return int(value) in NM_METERED_TRUE


class PowerPolicy(QObject):
    """Live metered / battery-saver / connectivity state.

    Signals:
        changed: `(NetworkState, PowerState)` whenever the derived state moves.
            Emitted only on an actual transition, so a flapping `network-changed`
            does not churn the fact tick.

    The cached raw system-bus reads (`Metered`, `OnBattery`) are refreshed by
    their own `PropertiesChanged` signals and by `refresh()`, never on a timer:
    the fact collector polls this object at up to 2.5 Hz and a D-Bus round trip
    per property per tick would be pure waste.
    """

    changed = Signal(NetworkState, PowerState)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        network_monitor: Any | None = None,
        power_monitor: Any | None = None,
        system_bus: Bus | None = None,
        connect_signals: bool = True,
    ) -> None:
        """Create the policy object and subscribe to change notifications.

        Args:
            parent: Optional Qt parent.
            network_monitor: Override for `Gio.NetworkMonitor.get_default()`.
                Pass `None` to use the real one, or a stand-in in tests.
            power_monitor: Override for `Gio.PowerProfileMonitor.dup_default()`.
            system_bus: Override for the process-wide system bus.
            connect_signals: Subscribe to `network-changed`, `notify::power-saver-enabled`
                and UPower's `PropertiesChanged`. Off in tests that drive state
                by hand.

        Raises:
            SafetyRefusal: If constructed off the GUI thread.
        """
        assert_gui_thread("PowerPolicy()")
        super().__init__(parent)
        glibpump.ensure_started()

        self._net = network_monitor if network_monitor is not None else _default_network_monitor()
        self._ppm = power_monitor if power_monitor is not None else _default_power_monitor()
        self._sys = system_bus if system_bus is not None else Bus.system()

        self._nm_metered: int | None = None
        self._on_battery: bool | None = None
        self._last_state: tuple[NetworkState, PowerState] | None = None
        self._handles: list[tuple[Any, int]] = []
        self._subscriptions: list[int] = []

        if connect_signals:
            self._connect_signals()

    # ── wiring ───────────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        """Hook every change source. A missing source is simply skipped."""
        if self._net is not None:
            handle = _try_connect(self._net, "network-changed", self._on_network_changed)
            if handle:
                self._handles.append((self._net, handle))
        if self._ppm is not None:
            handle = _try_connect(
                self._ppm, "notify::power-saver-enabled", self._on_power_changed
            )
            if handle:
                self._handles.append((self._ppm, handle))
        sub = self._sys.subscribe(
            NM_NAME, PROPERTIES_IFACE, PROPERTIES_CHANGED, NM_PATH,
            self._on_nm_properties,
        )
        if sub:
            self._subscriptions.append(sub)
        sub = self._sys.subscribe(
            UPOWER_NAME, PROPERTIES_IFACE, PROPERTIES_CHANGED, UPOWER_PATH,
            self._on_upower_properties,
        )
        if sub:
            self._subscriptions.append(sub)

    def shutdown(self) -> None:
        """Disconnect every monitor handler and D-Bus subscription."""
        for source, handle in self._handles:
            try:
                source.disconnect(handle)
            except (TypeError, AttributeError):  # pragma: no cover - teardown race
                pass
        self._handles.clear()
        for sub_id in self._subscriptions:
            self._sys.unsubscribe(sub_id)
        self._subscriptions.clear()

    # ── change handlers ──────────────────────────────────────────────────────

    def _on_network_changed(self, *_args: Any) -> None:
        """`Gio.NetworkMonitor::network-changed`: the raw NM value may have moved."""
        self._nm_metered = None
        self._emit_if_changed()

    def _on_power_changed(self, *_args: Any) -> None:
        """`Gio.PowerProfileMonitor::notify::power-saver-enabled`."""
        self._emit_if_changed()

    def _on_nm_properties(
        self, iface: str, changed: dict[str, Any], _invalidated: list[str]
    ) -> None:
        """NetworkManager `PropertiesChanged`.

        Args:
            iface: The interface whose properties changed.
            changed: The changed properties.
            _invalidated: Property names that must be re-read.
        """
        if iface != NM_IFACE:
            return
        if PROP_METERED in changed:
            self._nm_metered = int(changed[PROP_METERED])
        self._emit_if_changed()

    def _on_upower_properties(
        self, iface: str, changed: dict[str, Any], _invalidated: list[str]
    ) -> None:
        """UPower `PropertiesChanged`.

        Args:
            iface: The interface whose properties changed.
            changed: The changed properties.
            _invalidated: Property names that must be re-read.
        """
        if iface != UPOWER_IFACE:
            return
        if PROP_ON_BATTERY in changed:
            self._on_battery = bool(changed[PROP_ON_BATTERY])
        self._emit_if_changed()

    def _emit_if_changed(self) -> None:
        """Emit `changed` only when the derived state actually moved."""
        current = self.state()
        if current != self._last_state:
            self._last_state = current
            self.changed.emit(current[0], current[1])

    # ── reads ────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Drop the cached system-bus reads and re-evaluate.

        Emits `changed` if the state moved.
        """
        self._nm_metered = None
        self._on_battery = None
        self._emit_if_changed()

    def online(self) -> bool:
        """Whether the network stack believes we have connectivity.

        Returns:
            True when a route to the internet is believed to exist. Falls back
            to NetworkManager's `Connectivity` when no `Gio.NetworkMonitor` is
            available, and finally to True — an offline claim we cannot support
            would wrongly park the whole application in `OFFLINE`.
        """
        if self._net is not None:
            return bool(self._net.get_network_available())
        value = self._sys.get_property(
            NM_NAME, NM_PATH, NM_IFACE, PROP_CONNECTIVITY, None
        )
        if value is None:
            return True
        return int(value) >= NM_CONNECTIVITY_LIMITED

    def connectivity(self) -> int:
        """The raw connectivity level, for display.

        Returns:
            A `Gio.NetworkConnectivity` / `NMConnectivityState` value; both use
            4 for FULL. `NM_CONNECTIVITY_UNKNOWN` when nothing can answer.
        """
        if self._net is not None:
            value = self._net.get_connectivity()
            return int(getattr(value, "real", value))
        value = self._sys.get_property(
            NM_NAME, NM_PATH, NM_IFACE, PROP_CONNECTIVITY, NM_CONNECTIVITY_UNKNOWN
        )
        return int(value)

    def nm_metered_value(self) -> int:
        """NetworkManager's raw `Metered` property, cached.

        Returns:
            0 unknown, 1 yes, 2 no, 3 guess-yes, 4 guess-no.
            `NM_METERED_UNKNOWN` when NetworkManager cannot be reached — 0 is
            not in `NM_METERED_TRUE`, so an unreachable NM never auto-pauses.
        """
        if self._nm_metered is None:
            value = self._sys.get_property(
                NM_NAME, NM_PATH, NM_IFACE, PROP_METERED, NM_METERED_UNKNOWN
            )
            try:
                self._nm_metered = int(value)
            except (TypeError, ValueError):  # pragma: no cover - hostile peer
                self._nm_metered = NM_METERED_UNKNOWN
        return self._nm_metered

    def metered(self) -> bool:
        """Whether the primary connection is metered.

        `Gio.NetworkMonitor` is the primary source; NetworkManager's raw value
        is ORed in so that a build whose monitor is the no-op base class still
        gets the right answer.

        Returns:
            True if uploads and downloads should be paused for metering.
        """
        if self._net is not None and bool(self._net.get_network_metered()):
            return True
        return nm_metered_is_metered(self.nm_metered_value())

    def power_saver(self) -> bool:
        """Whether the desktop is in power-saver ("battery saver") mode.

        Returns:
            True when `Gio.PowerProfileMonitor` says so, or when
            power-profiles-daemon reports `ActiveProfile == "power-saver"`.
        """
        if self._ppm is not None:
            return bool(self._ppm.get_power_saver_enabled())
        for name, path, iface in POWER_PROFILE_SERVICES:
            value = self._sys.get_property(name, path, iface, PROP_ACTIVE_PROFILE, None)
            if value is not None:
                return str(value) == PROFILE_POWER_SAVER
        return False

    def on_battery(self) -> bool:
        """Whether the machine is running on battery, cached.

        Reported separately from `power_saver()` on purpose: the Windows client
        pauses on metered but only *throttles* on battery, so this never on its
        own produces `PowerState.SAVER`.

        Returns:
            True when UPower says `OnBattery`; False when UPower is absent (a
            desktop, as here, has no battery device at all).
        """
        if self._on_battery is None:
            value = self._sys.get_property(
                UPOWER_NAME, UPOWER_PATH, UPOWER_IFACE, PROP_ON_BATTERY, False
            )
            self._on_battery = bool(value)
        return self._on_battery

    # ── derived state ────────────────────────────────────────────────────────

    def network_state(self) -> NetworkState:
        """The `NetworkState` for `Facts.network`."""
        if not self.online():
            return NetworkState.OFFLINE
        if self.metered():
            return NetworkState.METERED
        return NetworkState.ONLINE

    def power_state(self) -> PowerState:
        """The `PowerState` for `Facts.power`.

        Battery saver only. Being merely unplugged is `NORMAL`, matching the
        Windows client's own semantics and the "This PC is in battery saver
        mode" wording in `strings.STATUS_SUB`.
        """
        return PowerState.SAVER if self.power_saver() else PowerState.NORMAL

    def state(self) -> tuple[NetworkState, PowerState]:
        """Both derived states in one call.

        Returns:
            `(network_state(), power_state())`.
        """
        return (self.network_state(), self.power_state())

    def should_throttle(self) -> tuple[bool, PauseReason]:
        """Whether an automatic pause applies, and why.

        Metered outranks battery saver, matching the ladder in
        `ARCHITECTURE.md` §6.3 where `PAUSED_METERED` sits above
        `PAUSED_BATTERY`.

        Returns:
            `(True, PauseReason.METERED | PauseReason.BATTERY)` or
            `(False, PauseReason.NONE)`.
        """
        if self.metered():
            return (True, PauseReason.METERED)
        if self.power_saver():
            return (True, PauseReason.BATTERY)
        return (False, PauseReason.NONE)

    def snapshot(self) -> dict[str, Any]:
        """Everything this object knows, for the About pane and diagnostics."""
        network, power = self.state()
        return {
            "online": self.online(),
            "connectivity": self.connectivity(),
            "metered": self.metered(),
            "nm_metered": self.nm_metered_value(),
            "power_saver": self.power_saver(),
            "on_battery": self.on_battery(),
            "network_state": network.value,
            "power_state": power.value,
            "has_network_monitor": self._net is not None,
            "has_power_monitor": self._ppm is not None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Monitor construction — isolated so a broken Gio build degrades, never crashes
# ─────────────────────────────────────────────────────────────────────────────

def _default_network_monitor() -> Any | None:
    """`Gio.NetworkMonitor.get_default()`, or `None` if it cannot be created."""
    try:
        return Gio.NetworkMonitor.get_default()
    except (GLib.Error, AttributeError, TypeError):  # pragma: no cover - broken gi
        log.warning("Gio.NetworkMonitor unavailable; falling back to NetworkManager")
        return None


def _default_power_monitor() -> Any | None:
    """`Gio.PowerProfileMonitor.dup_default()`, or `None`.

    `Gio.PowerProfileMonitor` needs GLib 2.70+; older builds fall back to
    power-profiles-daemon over D-Bus.
    """
    try:
        return Gio.PowerProfileMonitor.dup_default()
    except (GLib.Error, AttributeError, TypeError):  # pragma: no cover - old glib
        log.warning("Gio.PowerProfileMonitor unavailable; falling back to D-Bus")
        return None


def _try_connect(source: Any, signal: str, handler: Any) -> int:
    """Connect a GObject signal, tolerating a stand-in that lacks it.

    Args:
        source: The GObject (or test double) to connect to.
        signal: The signal name.
        handler: The callable.

    Returns:
        The handler id, or 0 if the signal could not be connected.
    """
    try:
        return int(source.connect(signal, handler))
    except (TypeError, AttributeError) as exc:
        log.debug("cannot connect %s on %r: %s", signal, type(source).__name__, exc)
        return 0


__all__ = [
    "NM_CONNECTIVITY_FULL",
    "NM_CONNECTIVITY_LIMITED",
    "NM_CONNECTIVITY_NONE",
    "NM_CONNECTIVITY_PORTAL",
    "NM_CONNECTIVITY_UNKNOWN",
    "NM_IFACE",
    "NM_METERED_GUESS_NO",
    "NM_METERED_GUESS_YES",
    "NM_METERED_NO",
    "NM_METERED_TRUE",
    "NM_METERED_UNKNOWN",
    "NM_METERED_YES",
    "NM_NAME",
    "NM_PATH",
    "POWER_PROFILE_SERVICES",
    "PROFILE_POWER_SAVER",
    "UPOWER_IFACE",
    "UPOWER_NAME",
    "UPOWER_PATH",
    "PowerPolicy",
    "nm_metered_is_metered",
]
