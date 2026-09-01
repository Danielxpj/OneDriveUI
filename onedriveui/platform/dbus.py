"""A thin, typed Gio session/system-bus helper.

Everything D-Bus in this application goes through here, and here goes through
`Gio` — never `PySide6.QtDBus`. The reason is not taste:

* PySide6 6.11.2 marshals a Python `int` as `i` or `x` and has **no way** to
  produce a `u` (uint32). `QDBusArgument` has no typed constructor either, so
  `org.freedesktop.Notifications.Notify` (signature `susssasa{sv}i`) is
  uncallable from Qt — verified three ways on the target machine.
* An empty Python list marshals as `av`, not `as`.
* `QDBusInterface.call()` accepts at most 4 positional arguments in its plain
  form; `Notify` takes 8.
* `QDBusConnection.connect()` needs a `bytes` slot signature, not a callable.

`GLib.Variant` has none of those problems, which is why every call here carries
an **explicit signature string** rather than letting a marshaller guess.

Threading: `Gio` is GUI-thread only (ARCHITECTURE.md §7). Every public method
asserts it, and every signal subscription is delivered by the `glibpump` timer.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Final, Iterable

from gi.repository import Gio, GLib

from onedriveui.platform.glibpump import assert_gui_thread, ensure_started

log = logging.getLogger(__name__)

#: Default reply timeout. A session-bus round trip to gnome-shell or to the
#: systemd user manager measures well under 10 ms here; 2 s is the "the peer is
#: wedged" ceiling, not an expected latency.
DEFAULT_TIMEOUT_MS: Final[int] = 2000

PROPERTIES_IFACE: Final[str] = "org.freedesktop.DBus.Properties"
INTROSPECTABLE_IFACE: Final[str] = "org.freedesktop.DBus.Introspectable"
DBUS_NAME: Final[str] = "org.freedesktop.DBus"
DBUS_PATH: Final[str] = "/org/freedesktop/DBus"
DBUS_IFACE: Final[str] = "org.freedesktop.DBus"


class Bus:
    """A cached connection to one D-Bus bus, with typed calls and subscriptions.

    Two process-wide instances are expected — `Bus.session()` and
    `Bus.system()`. Both connect lazily: constructing a `Bus` performs no I/O,
    so a machine with no session bus at all still imports and runs, it just
    reports `available() is False` and returns defaults everywhere.

    Attributes:
        bus_type: The `Gio.BusType` this instance talks to.
    """

    _SINGLETONS: dict[Gio.BusType, "Bus"] = {}

    def __init__(self, bus_type: Gio.BusType = Gio.BusType.SESSION) -> None:
        """Create an unconnected bus handle.

        Args:
            bus_type: `Gio.BusType.SESSION` or `Gio.BusType.SYSTEM`.
        """
        self.bus_type = bus_type
        self._connection: Gio.DBusConnection | None = None
        self._connect_failed = False
        self._proxies: dict[tuple[str, str, str], Gio.DBusProxy] = {}
        self._subscriptions: set[int] = set()

    # ── singletons ───────────────────────────────────────────────────────────

    @classmethod
    def session(cls) -> "Bus":
        """The process-wide session-bus handle (notifications, systemd --user)."""
        return cls._singleton(Gio.BusType.SESSION)

    @classmethod
    def system(cls) -> "Bus":
        """The process-wide system-bus handle (NetworkManager, UPower)."""
        return cls._singleton(Gio.BusType.SYSTEM)

    @classmethod
    def _singleton(cls, bus_type: Gio.BusType) -> "Bus":
        bus = cls._SINGLETONS.get(bus_type)
        if bus is None:
            bus = cls(bus_type)
            cls._SINGLETONS[bus_type] = bus
        return bus

    @classmethod
    def reset_singletons(cls) -> None:
        """Drop the cached handles. For tests and for a clean shutdown."""
        for bus in list(cls._SINGLETONS.values()):
            bus.close()
        cls._SINGLETONS.clear()

    # ── connection ───────────────────────────────────────────────────────────

    @property
    def connection(self) -> Gio.DBusConnection | None:
        """The live `Gio.DBusConnection`, connecting on first use.

        Returns:
            The connection, or `None` if this bus is unreachable. A failed
            connect is remembered so a missing system bus costs one attempt, not
            one per fact tick.

        Raises:
            SafetyRefusal: If touched off the GUI thread.
        """
        assert_gui_thread("Bus.connection")
        if self._connection is None and not self._connect_failed:
            try:
                self._connection = Gio.bus_get_sync(self.bus_type, None)
            except GLib.Error as exc:
                self._connect_failed = True
                log.warning("no %s bus: %s", self.bus_type.value_nick, exc.message)
        return self._connection

    def available(self) -> bool:
        """Whether this bus can be reached at all."""
        return self.connection is not None

    def unique_name(self) -> str:
        """This process's unique name on the bus, e.g. `:1.147`, or `""`."""
        connection = self.connection
        return connection.get_unique_name() if connection is not None else ""

    def close(self) -> None:
        """Drop every subscription and proxy and forget the connection.

        The connection itself is shared process-wide by GLib and is not closed.
        """
        connection = self._connection
        if connection is not None:
            for sub_id in list(self._subscriptions):
                try:
                    connection.signal_unsubscribe(sub_id)
                except (GLib.Error, TypeError):  # pragma: no cover - teardown race
                    pass
        self._subscriptions.clear()
        self._proxies.clear()
        self._connection = None
        self._connect_failed = False

    # ── calls ────────────────────────────────────────────────────────────────

    def call(
        self,
        name: str,
        path: str,
        iface: str,
        method: str,
        *,
        signature: str | None = None,
        args: Iterable[Any] = (),
        reply: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        auto_start: bool = False,
    ) -> tuple[Any, ...]:
        """Call a method with an explicit GVariant signature.

        Args:
            name: Bus name of the peer, e.g. `org.freedesktop.Notifications`.
            path: Object path.
            iface: Interface name.
            method: Method name.
            signature: The full argument signature, e.g. `"(susssasa{sv}i)"`.
                `None` means the method takes no arguments.
            args: The argument tuple matching `signature`.
            reply: The expected reply signature, e.g. `"(u)"`. `None` accepts and
                discards whatever comes back.
            timeout_ms: Reply timeout.
            auto_start: Allow D-Bus activation of a not-yet-running service.

        Returns:
            The unpacked reply tuple, or `()` when `reply` is `None`.

        Raises:
            SafetyRefusal: If called off the GUI thread.
            GLib.Error: If the bus is unavailable or the peer returns an error.
        """
        assert_gui_thread(f"Bus.call({iface}.{method})")
        connection = self.connection
        if connection is None:
            raise GLib.Error.new_literal(
                Gio.io_error_quark(),
                f"no {self.bus_type.value_nick} bus for {iface}.{method}",
                Gio.IOErrorEnum.NOT_FOUND,
            )
        params = GLib.Variant(signature, tuple(args)) if signature else None
        reply_type = GLib.VariantType(reply) if reply else None
        flags = (
            Gio.DBusCallFlags.NONE if auto_start else Gio.DBusCallFlags.NO_AUTO_START
        )
        result = connection.call_sync(
            name, path, iface, method, params, reply_type, flags, int(timeout_ms), None
        )
        return tuple(result.unpack()) if result is not None else ()

    def call_or_none(
        self,
        name: str,
        path: str,
        iface: str,
        method: str,
        **kwargs: Any,
    ) -> tuple[Any, ...] | None:
        """`call()` that returns `None` instead of raising.

        Args:
            name: Bus name of the peer.
            path: Object path.
            iface: Interface name.
            method: Method name.
            **kwargs: As `call()`.

        Returns:
            The unpacked reply tuple, or `None` if the call failed.
        """
        try:
            return self.call(name, path, iface, method, **kwargs)
        except GLib.Error as exc:
            log.debug("%s.%s failed: %s", iface, method, exc.message)
            return None

    # ── properties ───────────────────────────────────────────────────────────

    def get_property(
        self,
        name: str,
        path: str,
        iface: str,
        prop: str,
        default: Any = None,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> Any:
        """Read one property via `org.freedesktop.DBus.Properties.Get`.

        Reading is always safe from Qt too — the `uint32` comes back, it is not
        marshalled outbound — but it goes through Gio here for one code path.

        Args:
            name: Bus name of the peer.
            path: Object path.
            iface: The interface the property belongs to.
            prop: Property name.
            default: Returned when the peer, the bus or the property is missing.
            timeout_ms: Reply timeout.

        Returns:
            The unwrapped property value, or `default`.
        """
        result = self.call_or_none(
            name, path, PROPERTIES_IFACE, "Get",
            signature="(ss)", args=(iface, prop), reply="(v)", timeout_ms=timeout_ms,
        )
        if not result:
            return default
        return result[0]

    def get_all(
        self,
        name: str,
        path: str,
        iface: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict[str, Any]:
        """Read every property of an interface in one round trip.

        Args:
            name: Bus name of the peer.
            path: Object path.
            iface: Interface name.
            timeout_ms: Reply timeout.

        Returns:
            A `{property: value}` dict, empty if unavailable.
        """
        result = self.call_or_none(
            name, path, PROPERTIES_IFACE, "GetAll",
            signature="(s)", args=(iface,), reply="(a{sv})", timeout_ms=timeout_ms,
        )
        if not result:
            return {}
        return dict(result[0])

    # ── proxies ──────────────────────────────────────────────────────────────

    def proxy(
        self,
        name: str,
        path: str,
        iface: str,
        *,
        auto_start: bool = False,
    ) -> Gio.DBusProxy | None:
        """A cached `Gio.DBusProxy`, for peers we talk to repeatedly.

        Args:
            name: Bus name of the peer.
            path: Object path.
            iface: Interface name.
            auto_start: Allow D-Bus activation of the service.

        Returns:
            The proxy, or `None` if it could not be created.

        Raises:
            SafetyRefusal: If called off the GUI thread.
        """
        assert_gui_thread(f"Bus.proxy({iface})")
        key = (name, path, iface)
        cached = self._proxies.get(key)
        if cached is not None:
            return cached
        connection = self.connection
        if connection is None:
            return None
        flags = Gio.DBusProxyFlags.NONE if auto_start else Gio.DBusProxyFlags.DO_NOT_AUTO_START
        try:
            created = Gio.DBusProxy.new_sync(
                connection, flags, None, name, path, iface, None
            )
        except GLib.Error as exc:
            log.debug("proxy %s %s %s failed: %s", name, path, iface, exc.message)
            return None
        self._proxies[key] = created
        return created

    def name_has_owner(self, name: str) -> bool:
        """Whether a bus name is currently owned by some process.

        Args:
            name: The well-known bus name to test.

        Returns:
            True if the name has an owner right now.
        """
        result = self.call_or_none(
            DBUS_NAME, DBUS_PATH, DBUS_IFACE, "NameHasOwner",
            signature="(s)", args=(name,), reply="(b)",
        )
        return bool(result[0]) if result else False

    # ── signals ──────────────────────────────────────────────────────────────

    def subscribe(
        self,
        name: str | None,
        iface: str,
        signal: str,
        path: str | None,
        handler: Callable[..., None],
    ) -> int:
        """Subscribe to a D-Bus signal, delivered through the GLib pump.

        The handler is called with the signal's arguments already unpacked and
        spread as positional arguments — `handler(nid, action_key)` for
        `ActionInvoked`, `handler(iface, changed, invalidated)` for
        `PropertiesChanged`. It runs on the GUI thread, inside a pump tick.

        A handler that raises is logged and swallowed: an exception propagating
        into GLib's dispatcher would abort the pump and take every other D-Bus
        consumer down with it.

        Args:
            name: Sender bus name to filter on, or `None` for any sender.
            iface: Interface name to filter on.
            signal: Signal name to filter on.
            path: Object path to filter on, or `None` for any path.
            handler: Callable receiving the unpacked signal arguments.

        Returns:
            A subscription id for `unsubscribe()`, or 0 if the bus is missing.

        Raises:
            SafetyRefusal: If called off the GUI thread.
        """
        assert_gui_thread(f"Bus.subscribe({iface}.{signal})")
        connection = self.connection
        if connection is None:
            log.warning(
                "cannot subscribe to %s.%s: no %s bus",
                iface, signal, self.bus_type.value_nick,
            )
            return 0
        # Signals only arrive while something iterates the GLib context.
        ensure_started()

        def _dispatch(
            _connection: Gio.DBusConnection,
            _sender: str,
            _path: str,
            _iface: str,
            _signal: str,
            params: GLib.Variant,
            _user_data: Any,
        ) -> None:
            try:
                handler(*params.unpack())
            except Exception:  # noqa: BLE001 - must never reach GLib's dispatcher
                log.exception("handler for %s.%s raised", iface, signal)

        sub_id = connection.signal_subscribe(
            name, iface, signal, path, None, Gio.DBusSignalFlags.NONE, _dispatch, None
        )
        self._subscriptions.add(sub_id)
        return sub_id

    def unsubscribe(self, sub_id: int) -> None:
        """Cancel a subscription returned by `subscribe()`.

        Args:
            sub_id: The subscription id. 0 and unknown ids are ignored.
        """
        if not sub_id:
            return
        self._subscriptions.discard(sub_id)
        connection = self._connection
        if connection is None:
            return
        try:
            connection.signal_unsubscribe(sub_id)
        except (GLib.Error, TypeError):  # pragma: no cover - teardown race
            log.debug("signal_unsubscribe(%d) failed", sub_id)

    # ── introspection ────────────────────────────────────────────────────────

    def introspect(self, name: str, path: str) -> str:
        """The raw introspection XML of an object, or `""`.

        Args:
            name: Bus name of the peer.
            path: Object path.

        Returns:
            The XML document, or an empty string if unavailable.
        """
        result = self.call_or_none(
            name, path, INTROSPECTABLE_IFACE, "Introspect", reply="(s)"
        )
        return str(result[0]) if result else ""


def session() -> Bus:
    """Shorthand for `Bus.session()`."""
    return Bus.session()


def system() -> Bus:
    """Shorthand for `Bus.system()`."""
    return Bus.system()


__all__ = [
    "DBUS_IFACE",
    "DBUS_NAME",
    "DBUS_PATH",
    "DEFAULT_TIMEOUT_MS",
    "INTROSPECTABLE_IFACE",
    "PROPERTIES_IFACE",
    "Bus",
    "session",
    "system",
]
