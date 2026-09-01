"""Tests for `onedriveui.platform.notify`.

The fake bus deliberately builds a **real** `GLib.Variant` from the signature
and arguments `Notifier` hands it. That is the whole point: the `(susssasa{sv}i)`
marshalling — the `u` PySide6's QtDBus cannot produce, and the `y`-not-`i`
urgency byte — is exercised for real on every unit test, with no D-Bus traffic.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from gi.repository import Gio, GLib

from onedriveui import APP_DISPLAY_NAME, APP_ID
from onedriveui.bus import BUS
from onedriveui.models import NotificationId, NotifySpec, TrayIcon
from onedriveui.platform import glibpump
from onedriveui.platform import notify as N
from onedriveui.platform.dbus import Bus
from onedriveui.strings import TOAST

PLATFORM_DIR = Path(__file__).resolve().parent.parent / "onedriveui" / "platform"


def _platform_modules() -> list[tuple[Path, ast.Module]]:
    """Every module in `onedriveui/platform/`, parsed. Docstrings may *name* a
    banned API; only real imports and calls are violations."""
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(PLATFORM_DIR.glob("*.py"))
    ]


# ═════════════════════════════════════════════════════════════════════════════
# A session bus that marshals for real but never reaches D-Bus
# ═════════════════════════════════════════════════════════════════════════════

class FakeBus:
    """Records calls, builds the real GVariant, and replays subscriptions."""

    def __init__(self, *, capabilities: tuple[str, ...] | None = None) -> None:
        self.calls: list[SimpleNamespace] = []
        self.handlers: dict[tuple[str, str], list] = {}
        self.next_id = 100
        self.capabilities = (
            tuple(sorted(N.EXPECTED_CAPABILITIES)) if capabilities is None
            else tuple(capabilities)
        )
        self.fail_methods: set[str] = set()
        self.unsubscribed: list[int] = []
        self._sub_id = 0

    # -- surface used by Notifier -------------------------------------------
    def available(self) -> bool:
        return True

    def call(self, name, path, iface, method, *, signature=None, args=(),
             reply=None, timeout_ms=2000, auto_start=False):
        # The real marshalling. A wrong signature raises here, in the test.
        variant = GLib.Variant(signature, tuple(args)) if signature else None
        self.calls.append(SimpleNamespace(
            name=name, path=path, iface=iface, method=method,
            signature=signature, args=tuple(args), variant=variant,
            reply=reply, timeout_ms=timeout_ms,
        ))
        if method in self.fail_methods:
            self.fail_methods.discard(method)
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), "fake failure", Gio.IOErrorEnum.FAILED
            )
        if method == "Notify":
            replaces = int(args[1])
            if replaces:
                return (replaces,)
            self.next_id += 1
            return (self.next_id,)
        if method == "GetCapabilities":
            return (list(self.capabilities),)
        if method == "GetServerInformation":
            return ("gnome-shell", "GNOME", "50.4", "1.2")
        return ()

    def call_or_none(self, name, path, iface, method, **kwargs):
        try:
            return self.call(name, path, iface, method, **kwargs)
        except GLib.Error:
            return None

    def subscribe(self, name, iface, signal, path, handler):
        self._sub_id += 1
        self.handlers.setdefault((iface, signal), []).append(handler)
        return self._sub_id

    def unsubscribe(self, sub_id):
        self.unsubscribed.append(sub_id)

    # -- test helpers --------------------------------------------------------
    def emit(self, signal: str, *args) -> None:
        for handler in self.handlers.get((N.NOTIFY_IFACE, signal), []):
            handler(*args)

    def of(self, method: str) -> list[SimpleNamespace]:
        return [c for c in self.calls if c.method == method]

    def last(self, method: str) -> SimpleNamespace:
        rows = self.of(method)
        assert rows, f"no {method} call was made"
        return rows[-1]


@pytest.fixture(autouse=True)
def _no_platform_leaks():
    """The GLib pump is a process-wide singleton and `BUS` is a process-wide
    QObject. A pump left running here would keep iterating GLib inside another
    package's tests, so every test in this module hands it back."""
    yield
    glibpump.shutdown()


@pytest.fixture
def fake_bus() -> FakeBus:
    return FakeBus()


@pytest.fixture
def notifier(qapp, fake_bus):
    """A Notifier on the fake bus, shut down (and unhooked from BUS) after."""
    instance = N.Notifier(bus=fake_bus, settings=None)
    try:
        yield instance
    finally:
        instance.shutdown()
        glibpump.shutdown()


def hints_of(call) -> dict[str, GLib.Variant]:
    """The `a{sv}` hint dict of a recorded `Notify` call."""
    return call.args[6]


# ═════════════════════════════════════════════════════════════════════════════
# Contract
# ═════════════════════════════════════════════════════════════════════════════

class TestContract:
    def test_max_actions_is_two(self):
        assert N.Notifier.MAX_ACTIONS == 2

    def test_toasts_is_the_frozen_strings_table(self):
        assert N.TOASTS is TOAST

    def test_no_toast_declares_more_than_max_actions(self):
        for nid, (_summary, _body, actions) in N.TOASTS.items():
            buttons = [a for a, _ in actions if a != N.DEFAULT_ACTION]
            assert len(buttons) <= N.Notifier.MAX_ACTIONS, nid

    @pytest.mark.parametrize("nid", list(NotificationId))
    def test_every_notification_id_has_a_policy(self, nid):
        policy = N.POLICY[nid]
        assert isinstance(policy, N.ToastPolicy)
        assert N.URGENCY_LOW <= policy.urgency <= N.URGENCY_CRITICAL
        assert policy.timeout_ms >= -1
        assert policy.setting in ({""} | set(N.DEFAULT_ENABLED))

    @pytest.mark.parametrize("nid", list(NotificationId))
    def test_every_toast_resolves_to_an_installed_icon_name(self, nid):
        from onedriveui.ui import icons

        name = N.icon_name(nid)
        assert name
        assert name in icons.THEME_ICON_NAMES

    def test_the_signature_is_the_one_qtdbus_cannot_send(self):
        assert N.NOTIFY_SIGNATURE == "(susssasa{sv}i)"
        assert N.NOTIFY_REPLY == "(u)"

    def test_qsystemtrayicon_showmessage_is_never_called(self):
        """ARCHITECTURE.md §7.6: it silently drops every action button."""
        for path, tree in _platform_modules():
            called = {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            assert "showMessage" not in called, path
            names = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            } | {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            assert "QSystemTrayIcon" not in names, path

    def test_qtdbus_is_never_imported(self):
        """PySide6 6.11.2 cannot marshal the uint32 `Notify` needs."""
        for path, tree in _platform_modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "QtDBus" not in alias.name, path
                elif isinstance(node, ast.ImportFrom):
                    assert "QtDBus" not in (node.module or ""), path
                    for alias in node.names:
                        assert "QtDBus" not in alias.name, path


# ═════════════════════════════════════════════════════════════════════════════
# Text safety — body-markup is ON
# ═════════════════════════════════════════════════════════════════════════════

class TestEscaping:
    def test_escape_escapes_markup(self):
        assert N.escape("A & B <b>x</b>") == "A &amp; B &lt;b&gt;x&lt;/b&gt;"

    def test_escape_stringifies(self):
        assert N.escape(42) == "42"

    def test_plain_text_is_well_formed(self):
        assert N.markup_is_well_formed("Report.docx was uploaded") is True

    def test_a_bare_ampersand_is_not_well_formed(self):
        assert N.markup_is_well_formed("Q & A.txt") is False

    def test_balanced_markup_is_well_formed(self):
        assert N.markup_is_well_formed("Uploading <b>a.txt</b>") is True

    def test_safe_body_escapes_only_what_would_break(self):
        assert N.safe_body("Uploading <b>a.txt</b>") == "Uploading <b>a.txt</b>"
        assert N.safe_body("Q & A.txt") == "Q &amp; A.txt"
        assert N.safe_body("a<b.txt") == "a&lt;b.txt"

    def test_build_escapes_the_body_but_not_the_summary(self):
        spec = N.build(
            NotificationId.SHARED_WITH_ME, who="Ann & Bo", name="R&D <plan>.docx"
        )
        assert spec.summary == "Ann & Bo shared R&D <plan>.docx with you"
        spec2 = N.build(NotificationId.FILE_BLOCKED, name="R&D <plan>.docx")
        assert spec2.body == "R&amp;D &lt;plan&gt;.docx may be unsafe and wasn't synced."

    def test_build_uses_the_frozen_table(self):
        spec = N.build(NotificationId.SYNC_ISSUES, n=3)
        assert spec.summary == TOAST[NotificationId.SYNC_ISSUES][0]
        assert spec.body == "3 files couldn't be synced."
        assert spec.actions == TOAST[NotificationId.SYNC_ISSUES][2]

    def test_build_applies_the_policy(self):
        spec = N.build(NotificationId.MASS_DELETE, n=9)
        policy = N.POLICY[NotificationId.MASS_DELETE]
        assert spec.urgency == policy.urgency == N.URGENCY_CRITICAL
        assert spec.timeout_ms == policy.timeout_ms == N.TIMEOUT_NEVER
        assert spec.resident is True

    def test_a_missing_format_key_renders_the_placeholder(self):
        spec = N.build(NotificationId.SYNC_ISSUES)
        assert "{n}" in spec.body


# ═════════════════════════════════════════════════════════════════════════════
# Marshalling
# ═════════════════════════════════════════════════════════════════════════════

class TestMarshalling:
    def test_notify_sends_the_exact_signature(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_COMPLETE)
        call = fake_bus.last("Notify")
        assert call.name == N.NOTIFY_NAME
        assert call.path == N.NOTIFY_PATH
        assert call.iface == N.NOTIFY_IFACE
        assert call.signature == "(susssasa{sv}i)"
        assert call.variant.get_type_string() == "(susssasa{sv}i)"
        assert call.reply == "(u)"

    def test_urgency_is_a_gvariant_byte(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_COMPLETE)
        urgency = hints_of(fake_bus.last("Notify"))[N.HINT_URGENCY]
        assert urgency.get_type_string() == "y"
        assert urgency.get_byte() == N.URGENCY_LOW

    def test_urgency_critical_does_not_raise_a_gvariant_type_error(
        self, notifier, fake_bus
    ):
        server_id = notifier.toast(NotificationId.MASS_DELETE, n=7)
        assert server_id > 0
        urgency = hints_of(fake_bus.last("Notify"))[N.HINT_URGENCY]
        assert urgency.get_type_string() == "y"
        assert urgency.get_byte() == N.URGENCY_CRITICAL

    @pytest.mark.parametrize("value,expected", [(-5, 0), (0, 0), (1, 1), (2, 2), (99, 2)])
    def test_urgency_is_clamped_into_the_byte_range(
        self, notifier, fake_bus, value, expected
    ):
        notifier.notify(NotifySpec(id=NotificationId.SYNC_COMPLETE,
                                   summary="s", urgency=value))
        assert hints_of(fake_bus.last("Notify"))[N.HINT_URGENCY].get_byte() == expected

    def test_desktop_entry_hint_is_the_app_id(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_COMPLETE)
        hints = hints_of(fake_bus.last("Notify"))
        assert hints[N.HINT_DESKTOP_ENTRY].get_string() == APP_ID == "onedriveui"

    def test_app_name_and_privacy_scope(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_COMPLETE)
        call = fake_bus.last("Notify")
        assert call.args[0] == APP_DISPLAY_NAME
        hints = hints_of(call)
        assert hints[N.HINT_PRIVACY_SCOPE].get_string() == N.PRIVACY_SCOPE_USER

    def test_category_comes_from_the_policy(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_PAUSED_METERED)
        hints = hints_of(fake_bus.last("Notify"))
        assert hints[N.HINT_CATEGORY].get_string() == N.CATEGORY_NETWORK

    def test_transient_and_resident_hints(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_COMPLETE)
        hints = hints_of(fake_bus.last("Notify"))
        assert hints[N.HINT_TRANSIENT].get_boolean() is True
        assert N.HINT_RESIDENT not in hints

        notifier.toast(NotificationId.MASS_DELETE, n=3)
        hints = hints_of(fake_bus.last("Notify"))
        assert hints[N.HINT_RESIDENT].get_boolean() is True
        assert N.HINT_TRANSIENT not in hints

    def test_icon_is_the_tray_name_for_the_toast(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_PAUSED_MANUAL)
        assert fake_bus.last("Notify").args[2] == TrayIcon.PAUSED.value

    def test_timeout_comes_from_the_spec(self, notifier, fake_bus):
        notifier.toast(NotificationId.MASS_DELETE, n=1)
        assert fake_bus.last("Notify").args[7] == N.TIMEOUT_NEVER
        notifier.toast(NotificationId.SYNC_COMPLETE)
        assert fake_bus.last("Notify").args[7] == N.TIMEOUT_SERVER_DEFAULT

    def test_an_empty_action_list_still_marshals_as_as(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_COMPLETE)
        call = fake_bus.last("Notify")
        assert call.args[5] == []
        assert call.variant.get_child_value(5).get_type_string() == "as"

    def test_a_hostile_filename_never_breaks_the_body(self, notifier, fake_bus):
        notifier.notify(NotifySpec(id=NotificationId.FILE_BLOCKED,
                                   summary="s", body="a<b & c.txt"))
        assert fake_bus.last("Notify").args[4] == "a&lt;b &amp; c.txt"

    def test_an_empty_summary_falls_back_to_the_app_name(self, notifier, fake_bus):
        notifier.notify(NotifySpec(id=NotificationId.SYNC_COMPLETE, summary=""))
        assert fake_bus.last("Notify").args[3] == APP_DISPLAY_NAME


# ═════════════════════════════════════════════════════════════════════════════
# Actions
# ═════════════════════════════════════════════════════════════════════════════

class TestActions:
    def test_actions_are_flattened_in_spec_order(self, notifier, fake_bus):
        notifier.toast(NotificationId.MASS_DELETE, n=4)
        assert fake_bus.last("Notify").args[5] == [
            "restore", "Restore files", "delete", "Delete them",
        ]

    def test_actions_beyond_the_cap_are_dropped(self, notifier, fake_bus, caplog):
        spec = NotifySpec(
            id=NotificationId.SYNC_ISSUES, summary="s",
            actions=(("a", "A"), ("b", "B"), ("c", "C")),
        )
        with caplog.at_level("WARNING", logger=N.__name__):
            notifier.notify(spec)
        assert fake_bus.last("Notify").args[5] == ["a", "A", "b", "B"]
        assert "MAX_ACTIONS" in caplog.text

    def test_the_default_action_does_not_count_against_the_cap(
        self, notifier, fake_bus
    ):
        spec = NotifySpec(
            id=NotificationId.SYNC_ISSUES, summary="s",
            actions=((N.DEFAULT_ACTION, "Open"), ("a", "A"), ("b", "B")),
        )
        notifier.notify(spec)
        assert fake_bus.last("Notify").args[5] == [
            "default", "Open", "a", "A", "b", "B",
        ]

    def test_actions_are_dropped_when_the_server_cannot_render_them(self, qapp):
        bus = FakeBus(capabilities=("body", "body-markup"))
        instance = N.Notifier(bus=bus, listen_to_bus=False)
        try:
            instance.toast(NotificationId.MASS_DELETE, n=2)
            assert bus.last("Notify").args[5] == []
        finally:
            instance.shutdown()
            glibpump.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# replaces_id
# ═════════════════════════════════════════════════════════════════════════════

class TestReplacesId:
    def test_the_first_send_uses_zero(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        assert fake_bus.last("Notify").args[1] == 0

    def test_resending_replaces_instead_of_stacking(self, notifier, fake_bus):
        notifier.THROTTLE_MS = 0
        first = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        second = notifier.toast(NotificationId.SYNC_ISSUES, n=2)
        assert first == second
        assert fake_bus.of("Notify")[1].args[1] == first

    def test_different_toasts_get_independent_ids(self, notifier, fake_bus):
        notifier.THROTTLE_MS = 0
        issues = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        paused = notifier.toast(NotificationId.SYNC_PAUSED_MANUAL)
        assert issues != paused
        assert fake_bus.of("Notify")[1].args[1] == 0

    def test_server_id_for_reports_the_live_bubble(self, notifier):
        server_id = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        assert notifier.server_id_for(NotificationId.SYNC_ISSUES) == server_id
        assert notifier.server_id_for(NotificationId.QUOTA_FULL) == 0

    def test_replaces_id_survives_a_close(self, notifier, fake_bus):
        notifier.THROTTLE_MS = 0
        first = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        fake_bus.emit("NotificationClosed", first, N.CLOSE_DISMISSED)
        notifier.toast(NotificationId.SYNC_ISSUES, n=2)
        assert fake_bus.of("Notify")[1].args[1] == first


# ═════════════════════════════════════════════════════════════════════════════
# Settings
# ═════════════════════════════════════════════════════════════════════════════

class TestIsEnabled:
    def test_defaults_match_the_config_schema(self, notifier):
        assert notifier.is_enabled(NotificationId.SYNC_PAUSED_MANUAL) is True
        assert notifier.is_enabled(NotificationId.SYNC_ISSUES) is True
        assert notifier.is_enabled(NotificationId.MEMORIES) is False
        assert notifier.is_enabled(NotificationId.OTHER_ACCOUNTS) is False

    def test_safety_toasts_are_never_gated(self, notifier):
        notifier.set_settings(lambda _key: False)
        for nid in (NotificationId.MOUNT_LOST, NotificationId.ENGINE_DEAD,
                    NotificationId.NEEDS_RESYNC, NotificationId.VAULT_WARNING,
                    NotificationId.VAULT_LOCKED, NotificationId.MOUNT_RESTORED):
            assert notifier.is_enabled(nid) is True, nid

    def test_a_mapping_provider_is_honoured(self, notifier):
        notifier.set_settings({"notifications.sync_issues": False})
        assert notifier.is_enabled(NotificationId.SYNC_ISSUES) is False
        assert notifier.is_enabled(NotificationId.CONFLICT_DETECTED) is True

    def test_a_callable_provider_is_honoured(self, notifier):
        seen: list[str] = []

        def provider(key: str) -> bool:
            seen.append(key)
            return key != "notifications.conflicts"

        notifier.set_settings(provider)
        assert notifier.is_enabled(NotificationId.CONFLICT_DETECTED) is False
        assert notifier.is_enabled(NotificationId.SYNC_ISSUES) is True
        assert "notifications.conflicts" in seen

    def test_a_broken_provider_falls_back_to_the_default(self, notifier, caplog):
        def provider(_key: str) -> bool:
            raise RuntimeError("config not loaded")

        notifier.set_settings(provider)
        with caplog.at_level("ERROR", logger=N.__name__):
            assert notifier.is_enabled(NotificationId.SYNC_ISSUES) is True
        assert "settings lookup" in caplog.text

    def test_a_disabled_toast_sends_nothing(self, notifier, fake_bus):
        notifier.set_settings({"notifications.sync_issues": False})
        assert notifier.toast(NotificationId.SYNC_ISSUES, n=2) == 0
        assert fake_bus.of("Notify") == []
        assert notifier.suppressed == 1


# ═════════════════════════════════════════════════════════════════════════════
# Throttling
# ═════════════════════════════════════════════════════════════════════════════

class TestThrottle:
    def test_a_burst_is_coalesced_into_one_send(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        for count in range(2, 8):
            notifier.toast(NotificationId.SYNC_ISSUES, n=count)
        assert len(fake_bus.of("Notify")) == 1
        assert notifier.throttled == 6

    def test_an_identical_repeat_is_dropped_not_queued(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        assert len(fake_bus.of("Notify")) == 1
        assert notifier.throttled == 0
        assert notifier.stats()["pending"] == 0

    def test_flush_sends_the_newest_pending_spec(self, notifier, fake_bus, monkeypatch):
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        notifier.toast(NotificationId.SYNC_ISSUES, n=2)
        notifier.toast(NotificationId.SYNC_ISSUES, n=9)
        assert len(fake_bus.of("Notify")) == 1

        base = notifier._last_sent_ms[NotificationId.SYNC_ISSUES]
        notifier._last_sent_ms[NotificationId.SYNC_ISSUES] = base - 5000
        assert notifier.flush_pending() == 1
        assert fake_bus.of("Notify")[-1].args[4] == "9 files couldn't be synced."

    def test_flush_reschedules_what_is_still_inside_the_window(self, notifier):
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        notifier.toast(NotificationId.SYNC_ISSUES, n=2)
        assert notifier.flush_pending() == 0
        assert notifier.stats()["pending"] == 1
        assert notifier._flush_timer.isActive()

    def test_the_flush_timer_delivers_without_help(self, notifier, fake_bus, qtbot):
        notifier.THROTTLE_MS = 40
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        notifier.toast(NotificationId.SYNC_ISSUES, n=2)
        assert len(fake_bus.of("Notify")) == 1
        qtbot.wait(160)
        assert len(fake_bus.of("Notify")) == 2

    def test_different_toasts_do_not_throttle_each_other(self, notifier, fake_bus):
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        notifier.toast(NotificationId.QUOTA_FULL)
        notifier.toast(NotificationId.SYNC_PAUSED_MANUAL)
        assert len(fake_bus.of("Notify")) == 3

    def test_a_throttled_call_returns_the_live_id(self, notifier):
        first = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        assert notifier.toast(NotificationId.SYNC_ISSUES, n=2) == first


# ═════════════════════════════════════════════════════════════════════════════
# Signal routing
# ═════════════════════════════════════════════════════════════════════════════

class TestRouting:
    def test_action_invoked_reaches_the_qt_signal_and_the_bus(
        self, notifier, fake_bus, bus_spy
    ):
        bus_spy.watch("notification_action")
        seen: list[tuple[str, str]] = []
        notifier.action_invoked.connect(lambda k, a: seen.append((k, a)))

        server_id = notifier.toast(NotificationId.MASS_DELETE, n=5)
        fake_bus.emit("ActionInvoked", server_id, "restore")

        assert seen == [("mass_delete", "restore")]
        assert bus_spy.of("notification_action") == [("mass_delete", "restore")]

    def test_the_default_action_routes_too(self, notifier, fake_bus):
        seen: list[tuple[str, str]] = []
        notifier.action_invoked.connect(lambda k, a: seen.append((k, a)))
        server_id = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        fake_bus.emit("ActionInvoked", server_id, N.DEFAULT_ACTION)
        assert seen == [("sync_issues", "default")]

    def test_another_applications_notification_is_ignored(
        self, notifier, fake_bus, bus_spy
    ):
        bus_spy.watch("notification_action")
        seen: list[tuple[str, str]] = []
        notifier.action_invoked.connect(lambda k, a: seen.append((k, a)))
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        fake_bus.emit("ActionInvoked", 999_999, "whatever")
        assert seen == []
        assert bus_spy.of("notification_action") == []

    def test_notification_closed_records_the_reason(self, notifier, fake_bus):
        server_id = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        fake_bus.emit("NotificationClosed", server_id, N.CLOSE_DISMISSED)
        assert notifier.last_close_reason[NotificationId.SYNC_ISSUES] == N.CLOSE_DISMISSED

    def test_a_closed_notification_stops_routing_actions(self, notifier, fake_bus):
        seen: list[tuple[str, str]] = []
        notifier.action_invoked.connect(lambda k, a: seen.append((k, a)))
        server_id = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        fake_bus.emit("NotificationClosed", server_id, N.CLOSE_EXPIRED)
        fake_bus.emit("ActionInvoked", server_id, "view")
        assert seen == []

    def test_bus_toast_requested_drives_the_notifier(self, notifier, fake_bus):
        BUS.toast_requested.emit(N.build(NotificationId.QUOTA_FULL))
        assert len(fake_bus.of("Notify")) == 1
        assert fake_bus.last("Notify").args[3] == "Your OneDrive is full"

    def test_a_bad_bus_payload_is_logged_not_raised(self, notifier, fake_bus, caplog):
        with caplog.at_level("WARNING", logger=N.__name__):
            BUS.toast_requested.emit({"not": "a spec"})
        assert fake_bus.of("Notify") == []
        assert "not a NotifySpec" in caplog.text

    def test_shutdown_unhooks_the_bus(self, notifier, fake_bus):
        notifier.shutdown()
        BUS.toast_requested.emit(N.build(NotificationId.QUOTA_FULL))
        assert fake_bus.of("Notify") == []
        assert len(fake_bus.unsubscribed) == 2

    def test_listen_to_bus_can_be_declined(self, qapp, fake_bus):
        instance = N.Notifier(bus=fake_bus, listen_to_bus=False)
        try:
            BUS.toast_requested.emit(N.build(NotificationId.QUOTA_FULL))
            assert fake_bus.of("Notify") == []
        finally:
            instance.shutdown()
            glibpump.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# Capabilities, closing, robustness
# ═════════════════════════════════════════════════════════════════════════════

class TestCapabilitiesAndClose:
    def test_capabilities_are_read_and_cached(self, notifier, fake_bus):
        assert notifier.capabilities() == N.EXPECTED_CAPABILITIES
        assert notifier.capabilities() == N.EXPECTED_CAPABILITIES
        assert len(fake_bus.of("GetCapabilities")) == 1

    def test_refresh_capabilities_re_reads(self, notifier, fake_bus):
        notifier.capabilities()
        notifier.refresh_capabilities()
        assert len(fake_bus.of("GetCapabilities")) == 2

    def test_capabilities_are_empty_when_the_server_fails(self, notifier, fake_bus):
        fake_bus.fail_methods.add("GetCapabilities")
        assert notifier.capabilities() == frozenset()

    def test_server_info(self, notifier):
        assert notifier.server_info() == ("gnome-shell", "GNOME", "50.4", "1.2")

    def test_close_sends_a_uint32(self, notifier, fake_bus):
        server_id = notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        notifier.close(server_id)
        call = fake_bus.last("CloseNotification")
        assert call.signature == "(u)"
        assert call.variant.get_type_string() == "(u)"
        assert call.args == (server_id,)

    def test_close_ignores_zero(self, notifier, fake_bus):
        notifier.close(0)
        assert fake_bus.of("CloseNotification") == []

    def test_close_toast_finds_the_live_bubble(self, notifier, fake_bus):
        server_id = notifier.toast(NotificationId.QUOTA_FULL)
        notifier.close_toast(NotificationId.QUOTA_FULL)
        assert fake_bus.last("CloseNotification").args == (server_id,)

    def test_close_all(self, notifier, fake_bus):
        notifier.toast(NotificationId.QUOTA_FULL)
        notifier.toast(NotificationId.SYNC_ISSUES, n=1)
        notifier.close_all()
        assert len(fake_bus.of("CloseNotification")) == 2


class TestRobustness:
    def test_a_failing_notify_never_raises(self, notifier, fake_bus, caplog):
        fake_bus.fail_methods.add("Notify")
        with caplog.at_level("WARNING", logger=N.__name__):
            assert notifier.toast(NotificationId.QUOTA_FULL) == 0
        assert notifier.failed == 1
        assert "Notify(quota_full) failed" in caplog.text

    def test_notify_rejects_a_non_spec(self, notifier):
        with pytest.raises(TypeError):
            notifier.notify("not a spec")

    def test_notify_is_refused_off_the_gui_thread(self, notifier):
        import threading

        from onedriveui.errors import SafetyRefusal

        captured: list[BaseException] = []

        def worker() -> None:
            try:
                notifier.notify(N.build(NotificationId.SYNC_COMPLETE))
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)
        assert len(captured) == 1
        assert isinstance(captured[0], SafetyRefusal)

    def test_stats(self, notifier):
        notifier.toast(NotificationId.QUOTA_FULL)
        notifier.set_settings({"notifications.memories": False})
        notifier.toast(NotificationId.MEMORIES, year=2019)
        stats = notifier.stats()
        assert stats["sent"] == 1
        assert stats["suppressed"] == 1
        assert stats["failed"] == 0

    def test_the_pump_is_started_by_construction(self, qapp, fake_bus):
        glibpump.shutdown()
        instance = N.Notifier(bus=fake_bus, listen_to_bus=False)
        try:
            assert glibpump.current() is not None
            assert glibpump.current().is_running
        finally:
            instance.shutdown()
            glibpump.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# The real Bus helper, exercised against the real session bus
# ═════════════════════════════════════════════════════════════════════════════

def _session_bus_present() -> bool:
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")) or Path(
        f"/run/user/{os.getuid()}/bus"
    ).exists()


requires_session_bus = pytest.mark.skipif(
    not _session_bus_present(), reason="no session bus"
)


@pytest.mark.live
@requires_session_bus
class TestBusLive:
    def test_the_session_bus_connects(self, qapp):
        bus = Bus.session()
        assert bus.available() is True
        assert bus.unique_name().startswith(":")

    def test_a_typed_call_round_trips(self, qapp):
        result = Bus.session().call(
            N.NOTIFY_NAME, N.NOTIFY_PATH, N.NOTIFY_IFACE, "GetCapabilities",
            reply="(as)",
        )
        assert isinstance(result[0], list)

    def test_call_or_none_swallows_a_missing_peer(self, qapp):
        assert Bus.session().call_or_none(
            "no.such.Service.OneDriveUI", "/x", "no.such.Iface", "Nope", reply="(s)"
        ) is None

    def test_call_raises_for_a_missing_peer(self, qapp):
        with pytest.raises(GLib.Error):
            Bus.session().call(
                "no.such.Service.OneDriveUI", "/x", "no.such.Iface", "Nope", reply="(s)"
            )

    def test_name_has_owner(self, qapp):
        bus = Bus.session()
        assert bus.name_has_owner("org.freedesktop.DBus") is True
        assert bus.name_has_owner("no.such.Service.OneDriveUI") is False

    def test_a_subscription_is_delivered_through_the_pump(self, qapp):
        """The exact transport `ActionInvoked` rides."""
        glibpump.shutdown()
        pump = glibpump.install()
        bus = Bus.session()
        seen: list[tuple[int, str]] = []
        sub = bus.subscribe(
            None, N.NOTIFY_IFACE, "ActionInvoked", N.NOTIFY_PATH,
            lambda nid, action: seen.append((int(nid), str(action))),
        )
        try:
            bus.connection.emit_signal(
                None, N.NOTIFY_PATH, N.NOTIFY_IFACE, "ActionInvoked",
                GLib.Variant("(us)", (4_294_967_295, "restore")),
            )
            for _ in range(50):
                pump.drain()
                if seen:
                    break
            assert seen == [(4_294_967_295, "restore")]
        finally:
            bus.unsubscribe(sub)
            glibpump.shutdown()

    def test_a_raising_handler_never_reaches_glib(self, qapp, caplog):
        glibpump.shutdown()
        pump = glibpump.install()
        bus = Bus.session()

        def explode(*_args):
            raise RuntimeError("handler bug")

        sub = bus.subscribe(
            None, N.NOTIFY_IFACE, "ActionInvoked", N.NOTIFY_PATH, explode
        )
        try:
            with caplog.at_level("ERROR"):
                bus.connection.emit_signal(
                    None, N.NOTIFY_PATH, N.NOTIFY_IFACE, "ActionInvoked",
                    GLib.Variant("(us)", (1, "x")),
                )
                for _ in range(50):
                    pump.drain()
                    if "handler bug" in caplog.text:
                        break
            assert "handler bug" in caplog.text
        finally:
            bus.unsubscribe(sub)
            glibpump.shutdown()


# ═════════════════════════════════════════════════════════════════════════════
# Live acceptance against the real notification server
# ═════════════════════════════════════════════════════════════════════════════

def _notification_server_present() -> bool:
    if not _session_bus_present():
        return False
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return False
    try:
        return Bus.session().name_has_owner(N.NOTIFY_NAME)
    except Exception:  # noqa: BLE001 - probing must never fail collection
        return False


@pytest.mark.live
@requires_session_bus
class TestNotifierLive:
    """Real bubbles on the real GNOME shell. Kept short and closed immediately."""

    @pytest.fixture
    def live_notifier(self, qapp):
        if not _notification_server_present():
            pytest.skip("no org.freedesktop.Notifications on the session bus")
        glibpump.shutdown()
        glibpump.install()
        instance = N.Notifier(listen_to_bus=False)
        instance.THROTTLE_MS = 0
        try:
            yield instance
        finally:
            instance.close_all()
            glibpump.iterate()
            instance.shutdown()
            glibpump.shutdown()

    def test_capabilities_are_the_six_advertised_here(self, live_notifier):
        assert live_notifier.capabilities() == N.EXPECTED_CAPABILITIES

    def test_the_server_is_the_gnome_shell(self, live_notifier):
        name, _vendor, _version, spec = live_notifier.server_info()
        assert name
        assert spec

    def test_two_actions_and_urgency_critical_reach_the_server(self, live_notifier):
        spec = N.build(NotificationId.MASS_DELETE, n=3)
        assert spec.urgency == N.URGENCY_CRITICAL
        assert len(spec.actions) == 2
        server_id = live_notifier.notify(spec)
        assert server_id > 0, "urgency=2 must not raise a GVariant type error"
        live_notifier.close(server_id)

    def test_re_notifying_replaces_rather_than_stacks(self, live_notifier):
        first = live_notifier.notify(N.build(NotificationId.MASS_DELETE, n=3))
        second = live_notifier.notify(N.build(NotificationId.MASS_DELETE, n=4))
        assert first > 0
        assert second == first
        live_notifier.close(first)

    def test_close_produces_a_real_notification_closed_signal(self, live_notifier, qtbot):
        server_id = live_notifier.notify(N.build(NotificationId.SYNC_COMPLETE))
        live_notifier.close(server_id)
        for _ in range(40):
            glibpump.iterate()
            qtbot.wait(25)
            if NotificationId.SYNC_COMPLETE in live_notifier.last_close_reason:
                break
        assert live_notifier.last_close_reason[NotificationId.SYNC_COMPLETE] == (
            N.CLOSE_API
        )
