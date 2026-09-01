"""Tests for `onedriveui.platform.power`.

Two layers: the Gio monitors (faked, so `metered()` can be made to flip) and the
D-Bus fallbacks (a fake system bus, so NetworkManager, UPower and both
power-profiles-daemon bus names can be exercised without one). The live tests
assert what this machine actually reports — `Metered = uint32 4` (GUESS_NO),
`OnBattery = false` — which is the acceptance criterion for this module.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from onedriveui.errors import SafetyRefusal
from onedriveui.models import NetworkState, PauseReason, PowerState
from onedriveui.platform import glibpump
from onedriveui.platform import power as P
from onedriveui.platform.dbus import Bus


# ═════════════════════════════════════════════════════════════════════════════
# Stand-ins
# ═════════════════════════════════════════════════════════════════════════════

class FakeNetworkMonitor:
    """A `Gio.NetworkMonitor` stand-in with connectable signals."""

    def __init__(self, *, available=True, metered=False, connectivity=P.NM_CONNECTIVITY_FULL):
        self.available = available
        self.metered = metered
        self._connectivity = connectivity
        self.handlers: dict[str, list] = {}
        self._next = 0

    def get_network_available(self) -> bool:
        return self.available

    def get_network_metered(self) -> bool:
        return self.metered

    def get_connectivity(self):
        return self._connectivity

    def connect(self, signal, handler):
        self._next += 1
        self.handlers.setdefault(signal, []).append(handler)
        return self._next

    def disconnect(self, _handle):
        return None

    def emit(self, signal="network-changed"):
        for handler in self.handlers.get(signal, []):
            handler(self, self.available)


class FakePowerMonitor:
    """A `Gio.PowerProfileMonitor` stand-in."""

    def __init__(self, *, power_saver=False):
        self.power_saver = power_saver
        self.handlers: dict[str, list] = {}
        self._next = 0

    def get_power_saver_enabled(self) -> bool:
        return self.power_saver

    def connect(self, signal, handler):
        self._next += 1
        self.handlers.setdefault(signal, []).append(handler)
        return self._next

    def disconnect(self, _handle):
        return None

    def emit(self, signal="notify::power-saver-enabled"):
        for handler in self.handlers.get(signal, []):
            handler(self, None)


class FakeSystemBus:
    """A system bus whose properties are a plain dict."""

    def __init__(self, properties: dict[tuple[str, str], object] | None = None):
        self.properties = dict(properties or {})
        self.reads: list[tuple[str, str]] = []
        self.handlers: dict[tuple[str, str], list] = {}
        self.unsubscribed: list[int] = []
        self._next = 0

    def available(self) -> bool:
        return True

    def get_property(self, name, path, iface, prop, default=None, **_kwargs):
        self.reads.append((name, prop))
        return self.properties.get((name, prop), default)

    def get_all(self, name, path, iface, **_kwargs):
        return {
            prop: value
            for (owner, prop), value in self.properties.items()
            if owner == name
        }

    def call_or_none(self, *_args, **_kwargs):
        return None

    def subscribe(self, name, iface, signal, path, handler):
        self._next += 1
        self.handlers.setdefault((name, signal), []).append(handler)
        return self._next

    def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)

    def emit(self, name, signal, *args):
        for handler in self.handlers.get((name, signal), []):
            handler(*args)


def make_policy(qapp, **kwargs) -> P.PowerPolicy:
    """A policy wired to stand-ins, with sensible unmetered defaults."""
    kwargs.setdefault("network_monitor", FakeNetworkMonitor())
    kwargs.setdefault("power_monitor", FakePowerMonitor())
    kwargs.setdefault("system_bus", FakeSystemBus())
    return P.PowerPolicy(**kwargs)


@pytest.fixture(autouse=True)
def _no_platform_leaks():
    """The GLib pump is a process-wide singleton and `BUS` is a process-wide
    QObject. A pump left running here would keep iterating GLib inside another
    package's tests, so every test in this module hands it back."""
    yield
    glibpump.shutdown()


@pytest.fixture
def policy(qapp):
    instance = make_policy(qapp)
    try:
        yield instance
    finally:
        instance.shutdown()
        glibpump.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════════

class TestConstants:
    def test_the_nmmetered_enum_values(self):
        assert (P.NM_METERED_UNKNOWN, P.NM_METERED_YES, P.NM_METERED_NO,
                P.NM_METERED_GUESS_YES, P.NM_METERED_GUESS_NO) == (0, 1, 2, 3, 4)

    def test_only_yes_and_guess_yes_are_metered(self):
        assert P.NM_METERED_TRUE == frozenset({1, 3})

    @pytest.mark.parametrize("value,expected", [
        (P.NM_METERED_UNKNOWN, False),
        (P.NM_METERED_YES, True),
        (P.NM_METERED_NO, False),
        (P.NM_METERED_GUESS_YES, True),
        (P.NM_METERED_GUESS_NO, False),
    ])
    def test_nm_metered_is_metered(self, value, expected):
        assert P.nm_metered_is_metered(value) is expected

    def test_the_modern_power_profiles_name_is_tried_first(self):
        assert P.POWER_PROFILE_SERVICES[0][0] == "org.freedesktop.UPower.PowerProfiles"
        assert P.POWER_PROFILE_SERVICES[1][0] == "net.hadess.PowerProfiles"

    def test_constructing_off_the_gui_thread_is_refused(self, qapp):
        import threading

        captured: list[BaseException] = []

        def worker():
            try:
                make_policy(qapp, connect_signals=False)
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert len(captured) == 1
        assert isinstance(captured[0], SafetyRefusal)


# ═════════════════════════════════════════════════════════════════════════════
# Metered
# ═════════════════════════════════════════════════════════════════════════════

class TestMetered:
    def test_unmetered_by_default(self, policy):
        assert policy.metered() is False

    def test_metered_flips_under_a_patched_fake(self, policy):
        assert policy.metered() is False
        policy._net.metered = True
        assert policy.metered() is True

    def test_nm_metered_value_is_read_from_the_system_bus(self, qapp):
        bus = FakeSystemBus({(P.NM_NAME, P.PROP_METERED): P.NM_METERED_GUESS_NO})
        instance = make_policy(qapp, system_bus=bus)
        try:
            assert instance.nm_metered_value() == P.NM_METERED_GUESS_NO
            assert instance.metered() is False
        finally:
            instance.shutdown()

    @pytest.mark.parametrize("raw,expected", [(0, False), (1, True), (2, False),
                                              (3, True), (4, False)])
    def test_the_nm_fallback_decides_when_there_is_no_monitor(self, qapp, raw, expected):
        bus = FakeSystemBus({(P.NM_NAME, P.PROP_METERED): raw})
        instance = P.PowerPolicy(
            network_monitor=None, power_monitor=FakePowerMonitor(), system_bus=bus
        )
        instance._net = None  # simulate a build with no GNetworkMonitor
        try:
            assert instance.nm_metered_value() == raw
            assert instance.metered() is expected
        finally:
            instance.shutdown()

    def test_nm_is_ored_in_when_the_monitor_says_no(self, qapp):
        bus = FakeSystemBus({(P.NM_NAME, P.PROP_METERED): P.NM_METERED_YES})
        instance = make_policy(qapp, system_bus=bus)
        try:
            assert instance._net.get_network_metered() is False
            assert instance.metered() is True
        finally:
            instance.shutdown()

    def test_an_unreachable_networkmanager_never_pauses(self, policy):
        assert policy.nm_metered_value() == P.NM_METERED_UNKNOWN
        assert policy.metered() is False

    def test_the_raw_value_is_cached(self, policy):
        policy.nm_metered_value()
        policy.nm_metered_value()
        assert len([r for r in policy._sys.reads if r[1] == P.PROP_METERED]) == 1

    def test_network_changed_invalidates_the_cache(self, policy):
        policy.nm_metered_value()
        policy._net.emit("network-changed")
        policy.nm_metered_value()
        assert len([r for r in policy._sys.reads if r[1] == P.PROP_METERED]) == 2

    def test_a_properties_changed_signal_updates_the_value(self, policy):
        assert policy.nm_metered_value() == P.NM_METERED_UNKNOWN
        policy._sys.emit(
            P.NM_NAME, P.PROPERTIES_CHANGED, P.NM_IFACE,
            {P.PROP_METERED: P.NM_METERED_YES}, [],
        )
        assert policy.nm_metered_value() == P.NM_METERED_YES
        assert policy.metered() is True

    def test_another_interfaces_properties_are_ignored(self, policy):
        policy._sys.emit(
            P.NM_NAME, P.PROPERTIES_CHANGED, "org.freedesktop.NetworkManager.Device",
            {P.PROP_METERED: P.NM_METERED_YES}, [],
        )
        assert policy.nm_metered_value() == P.NM_METERED_UNKNOWN

    def test_a_hostile_metered_value_degrades_to_unknown(self, qapp):
        bus = FakeSystemBus({(P.NM_NAME, P.PROP_METERED): "not-a-number"})
        instance = make_policy(qapp, system_bus=bus)
        try:
            assert instance.nm_metered_value() == P.NM_METERED_UNKNOWN
            assert instance.metered() is False
        finally:
            instance.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# Power saver and battery
# ═════════════════════════════════════════════════════════════════════════════

class TestPowerSaver:
    def test_the_monitor_is_the_primary_source(self, policy):
        assert policy.power_saver() is False
        policy._ppm.power_saver = True
        assert policy.power_saver() is True

    def test_the_modern_power_profiles_name_is_the_first_fallback(self, qapp):
        name, _path, _iface = P.POWER_PROFILE_SERVICES[0]
        bus = FakeSystemBus({(name, P.PROP_ACTIVE_PROFILE): P.PROFILE_POWER_SAVER})
        instance = make_policy(qapp, power_monitor=None, system_bus=bus)
        instance._ppm = None
        try:
            assert instance.power_saver() is True
        finally:
            instance.shutdown()

    def test_the_legacy_hadess_name_is_the_second_fallback(self, qapp):
        name = P.POWER_PROFILE_SERVICES[1][0]
        bus = FakeSystemBus({(name, P.PROP_ACTIVE_PROFILE): P.PROFILE_POWER_SAVER})
        instance = make_policy(qapp, power_monitor=None, system_bus=bus)
        instance._ppm = None
        try:
            assert instance.power_saver() is True
            assert (P.POWER_PROFILE_SERVICES[0][0], P.PROP_ACTIVE_PROFILE) in [
                (n, p) for n, p in instance._sys.reads
            ], "the modern name must be tried first"
        finally:
            instance.shutdown()

    def test_a_non_saver_profile_is_not_power_saving(self, qapp):
        name = P.POWER_PROFILE_SERVICES[0][0]
        bus = FakeSystemBus({(name, P.PROP_ACTIVE_PROFILE): "performance"})
        instance = make_policy(qapp, power_monitor=None, system_bus=bus)
        instance._ppm = None
        try:
            assert instance.power_saver() is False
        finally:
            instance.shutdown()

    def test_no_source_at_all_is_not_power_saving(self, qapp):
        instance = make_policy(qapp, power_monitor=None)
        instance._ppm = None
        try:
            assert instance.power_saver() is False
        finally:
            instance.shutdown()

    def test_a_power_saver_notify_re_evaluates(self, policy):
        seen: list[tuple] = []
        policy.changed.connect(lambda n, p: seen.append((n, p)))
        policy._ppm.power_saver = True
        policy._ppm.emit()
        assert seen == [(NetworkState.ONLINE, PowerState.SAVER)]


class TestOnBattery:
    def test_false_without_upower(self, policy):
        assert policy.on_battery() is False

    def test_read_from_upower(self, qapp):
        bus = FakeSystemBus({(P.UPOWER_NAME, P.PROP_ON_BATTERY): True})
        instance = make_policy(qapp, system_bus=bus)
        try:
            assert instance.on_battery() is True
        finally:
            instance.shutdown()

    def test_cached_and_refreshed_by_the_signal(self, policy):
        assert policy.on_battery() is False
        policy.on_battery()
        assert len([r for r in policy._sys.reads if r[1] == P.PROP_ON_BATTERY]) == 1
        policy._sys.emit(
            P.UPOWER_NAME, P.PROPERTIES_CHANGED, P.UPOWER_IFACE,
            {P.PROP_ON_BATTERY: True}, [],
        )
        assert policy.on_battery() is True

    def test_being_on_battery_alone_is_not_battery_saver(self, qapp):
        """Windows pauses on metered but only throttles on battery."""
        bus = FakeSystemBus({(P.UPOWER_NAME, P.PROP_ON_BATTERY): True})
        instance = make_policy(qapp, system_bus=bus)
        try:
            assert instance.on_battery() is True
            assert instance.power_saver() is False
            assert instance.power_state() is PowerState.NORMAL
            assert instance.should_throttle() == (False, PauseReason.NONE)
        finally:
            instance.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# Derived state
# ═════════════════════════════════════════════════════════════════════════════

class TestState:
    def test_online_and_normal(self, policy):
        assert policy.state() == (NetworkState.ONLINE, PowerState.NORMAL)

    def test_metered_state(self, policy):
        policy._net.metered = True
        assert policy.network_state() is NetworkState.METERED

    def test_offline_state(self, policy):
        policy._net.available = False
        assert policy.network_state() is NetworkState.OFFLINE

    def test_offline_outranks_metered(self, policy):
        policy._net.available = False
        policy._net.metered = True
        assert policy.network_state() is NetworkState.OFFLINE

    def test_saver_state(self, policy):
        policy._ppm.power_saver = True
        assert policy.power_state() is PowerState.SAVER

    def test_connectivity_is_reported(self, policy):
        assert policy.connectivity() == P.NM_CONNECTIVITY_FULL

    def test_connectivity_falls_back_to_networkmanager(self, qapp):
        bus = FakeSystemBus({(P.NM_NAME, P.PROP_CONNECTIVITY): P.NM_CONNECTIVITY_PORTAL})
        instance = make_policy(qapp, system_bus=bus)
        instance._net = None
        try:
            assert instance.connectivity() == P.NM_CONNECTIVITY_PORTAL
        finally:
            instance.shutdown()

    def test_online_defaults_to_true_when_nothing_can_answer(self, qapp):
        instance = make_policy(qapp)
        instance._net = None
        try:
            assert instance.online() is True
        finally:
            instance.shutdown()

    def test_a_limited_connection_still_counts_as_online(self, qapp):
        bus = FakeSystemBus({(P.NM_NAME, P.PROP_CONNECTIVITY): P.NM_CONNECTIVITY_LIMITED})
        instance = make_policy(qapp, system_bus=bus)
        instance._net = None
        try:
            assert instance.online() is True
        finally:
            instance.shutdown()

    def test_no_connectivity_is_offline(self, qapp):
        bus = FakeSystemBus({(P.NM_NAME, P.PROP_CONNECTIVITY): P.NM_CONNECTIVITY_NONE})
        instance = make_policy(qapp, system_bus=bus)
        instance._net = None
        try:
            assert instance.online() is False
        finally:
            instance.shutdown()


class TestShouldThrottle:
    def test_nothing_to_do_when_normal(self, policy):
        assert policy.should_throttle() == (False, PauseReason.NONE)

    def test_metered_pauses(self, policy):
        policy._net.metered = True
        assert policy.should_throttle() == (True, PauseReason.METERED)

    def test_power_saver_pauses(self, policy):
        policy._ppm.power_saver = True
        assert policy.should_throttle() == (True, PauseReason.BATTERY)

    def test_metered_outranks_power_saver(self, policy):
        policy._net.metered = True
        policy._ppm.power_saver = True
        assert policy.should_throttle() == (True, PauseReason.METERED)


class TestChangedSignal:
    def test_emitted_once_per_transition(self, policy):
        seen: list[tuple] = []
        policy.changed.connect(lambda n, p: seen.append((n, p)))

        policy._net.metered = True
        policy._net.emit()
        policy._net.emit()
        policy._net.emit()

        assert seen == [(NetworkState.METERED, PowerState.NORMAL)]

    def test_a_return_transition_emits_again(self, policy):
        seen: list[tuple] = []
        policy.changed.connect(lambda n, p: seen.append((n, p)))
        policy._net.metered = True
        policy._net.emit()
        policy._net.metered = False
        policy._net.emit()
        assert seen == [
            (NetworkState.METERED, PowerState.NORMAL),
            (NetworkState.ONLINE, PowerState.NORMAL),
        ]

    def test_refresh_drops_the_caches_and_re_evaluates(self, policy):
        policy.nm_metered_value()
        policy.on_battery()
        seen: list[tuple] = []
        policy.changed.connect(lambda n, p: seen.append((n, p)))
        policy._sys.properties[(P.NM_NAME, P.PROP_METERED)] = P.NM_METERED_YES
        policy.refresh()
        assert seen == [(NetworkState.METERED, PowerState.NORMAL)]

    def test_shutdown_detaches_every_subscription(self, policy):
        assert policy._subscriptions
        subscriptions = list(policy._subscriptions)
        bus = policy._sys
        policy.shutdown()
        assert bus.unsubscribed == subscriptions
        assert policy._handles == []


class TestSnapshot:
    def test_snapshot_reports_every_input(self, policy):
        snapshot = policy.snapshot()
        assert snapshot["metered"] is False
        assert snapshot["nm_metered"] == P.NM_METERED_UNKNOWN
        assert snapshot["network_state"] == NetworkState.ONLINE.value
        assert snapshot["power_state"] == PowerState.NORMAL.value
        assert snapshot["has_network_monitor"] is True
        assert snapshot["has_power_monitor"] is True

    def test_missing_monitors_are_reported(self, qapp):
        instance = make_policy(qapp)
        instance._net = None
        instance._ppm = None
        try:
            snapshot = instance.snapshot()
            assert snapshot["has_network_monitor"] is False
            assert snapshot["has_power_monitor"] is False
        finally:
            instance.shutdown()


class TestMonitorConstruction:
    def test_a_broken_network_monitor_degrades_to_none(self, monkeypatch):
        monkeypatch.setattr(
            P.Gio, "NetworkMonitor",
            SimpleNamespace(get_default=lambda: (_ for _ in ()).throw(TypeError())),
        )
        assert P._default_network_monitor() is None

    def test_a_missing_power_profile_monitor_degrades_to_none(self, monkeypatch):
        monkeypatch.setattr(
            P.Gio, "PowerProfileMonitor",
            SimpleNamespace(dup_default=lambda: (_ for _ in ()).throw(AttributeError())),
        )
        assert P._default_power_monitor() is None

    def test_try_connect_tolerates_a_source_without_the_signal(self):
        assert P._try_connect(object(), "network-changed", lambda: None) == 0

    def test_the_pump_is_started_by_construction(self, qapp):
        glibpump.shutdown()
        instance = make_policy(qapp)
        try:
            assert glibpump.current() is not None
            assert glibpump.current().is_running
        finally:
            instance.shutdown()
            glibpump.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# Live: what this machine actually reports
# ═════════════════════════════════════════════════════════════════════════════

def _system_bus_present() -> bool:
    return Path("/run/dbus/system_bus_socket").exists() or bool(
        os.environ.get("DBUS_SYSTEM_BUS_ADDRESS")
    )


@pytest.mark.live
@pytest.mark.skipif(not _system_bus_present(), reason="no system bus")
class TestLive:
    @pytest.fixture
    def live_policy(self, qapp):
        glibpump.shutdown()
        instance = P.PowerPolicy()
        try:
            yield instance
        finally:
            instance.shutdown()
            Bus.reset_singletons()
            glibpump.shutdown()

    def test_the_real_gio_monitors_exist(self, live_policy):
        assert live_policy._net is not None
        assert live_policy._ppm is not None

    def test_metered_is_false_on_this_machine(self, live_policy):
        assert live_policy.metered() is False

    def test_networkmanager_reports_guess_no(self, live_policy):
        assert live_policy.nm_metered_value() == P.NM_METERED_GUESS_NO == 4

    def test_this_machine_is_not_on_battery(self, live_policy):
        assert live_policy.on_battery() is False

    def test_this_machine_is_online(self, live_policy):
        assert live_policy.online() is True
        assert live_policy.connectivity() == P.NM_CONNECTIVITY_FULL

    def test_the_derived_state_is_online_and_normal(self, live_policy):
        assert live_policy.state() == (NetworkState.ONLINE, PowerState.NORMAL)
        assert live_policy.should_throttle() == (False, PauseReason.NONE)

    def test_power_profiles_daemon_answers_on_the_modern_name(self, live_policy):
        name, path, iface = P.POWER_PROFILE_SERVICES[0]
        profile = Bus.system().get_property(name, path, iface, P.PROP_ACTIVE_PROFILE)
        assert profile in ("power-saver", "balanced", "performance")

    def test_the_legacy_bus_name_still_answers_here(self, live_policy):
        name, path, iface = P.POWER_PROFILE_SERVICES[1]
        profile = Bus.system().get_property(name, path, iface, P.PROP_ACTIVE_PROFILE)
        assert profile in ("power-saver", "balanced", "performance")
