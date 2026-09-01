"""Desktop notifications — every toast this application shows.

**Gio only. `PySide6.QtDBus` cannot send a notification at all.**
`org.freedesktop.Notifications.Notify` has signature `susssasa{sv}i`; PySide6
6.11.2 marshals a Python `int` as `i` or `x` and has no way to produce the `u`
that `replaces_id` needs, and `QDBusArgument` has no typed constructor to force
one. Verified on the target machine:

    Type of message, "(sisssasa{sv}i)", does not match expected type
                     "(susssasa{sv}i)"

**`QSystemTrayIcon.showMessage()` is banned** (ARCHITECTURE.md §7.6). It works,
but it silently drops every action button, has no `replaces_id`, no urgency and
returns no id to close — so a "Sync paused / Sync Anyway" toast would arrive as
a dead-end bubble.

Two traps this module exists to not fall into:

* **`urgency` is a GVariant BYTE `y`, not `i`.** It is the single most common
  bug in freedesktop notification code.
* **`body-markup` is ON here** (the server advertises it), so every value
  interpolated into a body — always a filename — goes through
  `GLib.markup_escape_text()`. `notify()` additionally refuses to send a body
  that is not well-formed markup, escaping it wholesale instead.

Server measured on the target machine: `gnome-shell` / GNOME / 50.4 / spec 1.2,
capabilities exactly `{actions, body, body-markup, icon-static, persistence,
sound}` — no `body-images`, no `body-hyperlinks`, no `action-icons`, no
`inline-reply`. GNOME renders about three action buttons; `MAX_ACTIONS` is 2.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Final, Mapping, NamedTuple
from xml.etree import ElementTree

from gi.repository import GLib
from PySide6.QtCore import QObject, QTimer, Signal

from onedriveui import APP_DISPLAY_NAME, APP_ID
from onedriveui.bus import BUS
from onedriveui.models import NotificationId, NotifySpec, TrayIcon
from onedriveui.platform import glibpump
from onedriveui.platform.dbus import Bus
from onedriveui.platform.glibpump import assert_gui_thread
from onedriveui.strings import TOAST, t

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Protocol constants (freedesktop Desktop Notifications 1.2)
# ─────────────────────────────────────────────────────────────────────────────

NOTIFY_NAME: Final[str] = "org.freedesktop.Notifications"
NOTIFY_PATH: Final[str] = "/org/freedesktop/Notifications"
NOTIFY_IFACE: Final[str] = "org.freedesktop.Notifications"

#: The signature PySide6's QtDBus cannot produce. This is why we are on Gio.
NOTIFY_SIGNATURE: Final[str] = "(susssasa{sv}i)"
NOTIFY_REPLY: Final[str] = "(u)"
CLOSE_SIGNATURE: Final[str] = "(u)"

#: `Notify` can be slow while the shell animates; give it more than a property read.
NOTIFY_TIMEOUT_MS: Final[int] = 5000

URGENCY_LOW: Final[int] = 0
URGENCY_NORMAL: Final[int] = 1
URGENCY_CRITICAL: Final[int] = 2

CLOSE_EXPIRED: Final[int] = 1
CLOSE_DISMISSED: Final[int] = 2
CLOSE_API: Final[int] = 3
CLOSE_UNDEFINED: Final[int] = 4

#: `timeout_ms` sentinels understood by the server.
TIMEOUT_SERVER_DEFAULT: Final[int] = -1
TIMEOUT_NEVER: Final[int] = 0

CAP_ACTIONS: Final[str] = "actions"
CAP_BODY: Final[str] = "body"
CAP_BODY_MARKUP: Final[str] = "body-markup"
CAP_ICON_STATIC: Final[str] = "icon-static"
CAP_PERSISTENCE: Final[str] = "persistence"
CAP_SOUND: Final[str] = "sound"

#: What gnome-shell 50.4 advertises on the target machine. Used by the live
#: acceptance test; never used to gate behaviour — `capabilities()` is.
EXPECTED_CAPABILITIES: Final[frozenset[str]] = frozenset({
    CAP_ACTIONS, CAP_BODY, CAP_BODY_MARKUP, CAP_ICON_STATIC,
    CAP_PERSISTENCE, CAP_SOUND,
})

CATEGORY_TRANSFER: Final[str] = "transfer"
CATEGORY_TRANSFER_COMPLETE: Final[str] = "transfer.complete"
CATEGORY_TRANSFER_ERROR: Final[str] = "transfer.error"
CATEGORY_DEVICE: Final[str] = "device"
CATEGORY_NETWORK: Final[str] = "network"

HINT_URGENCY: Final[str] = "urgency"
HINT_DESKTOP_ENTRY: Final[str] = "desktop-entry"
HINT_CATEGORY: Final[str] = "category"
HINT_TRANSIENT: Final[str] = "transient"
HINT_RESIDENT: Final[str] = "resident"
HINT_PRIVACY_SCOPE: Final[str] = "x-gnome-privacy-scope"
PRIVACY_SCOPE_USER: Final[str] = "user"

#: The whole-bubble click. GNOME ignores its label and never draws a button.
DEFAULT_ACTION: Final[str] = "default"

#: Re-exported, never re-declared: the toast table is `strings.TOAST`.
TOASTS = TOAST


# ─────────────────────────────────────────────────────────────────────────────
# Per-toast policy
# ─────────────────────────────────────────────────────────────────────────────

class ToastPolicy(NamedTuple):
    """How one `NotificationId` is presented.

    Attributes:
        tray: The themed icon name to send, via `TrayIcon`'s values. `NONE`
            falls back to the application icon.
        urgency: 0 low, 1 normal, 2 critical. Critical never auto-expires on
            GNOME and shows through Do Not Disturb.
        timeout_ms: -1 server default, 0 never expire, else milliseconds.
        transient: Bypass the message tray — fire and forget.
        resident: Stay in the tray after an action is invoked.
        category: freedesktop notification category.
        setting: The dotted `config.notifications.*` key that gates this toast.
            Empty means the toast is never gated: mount loss, a dead engine, a
            required resync and vault state are safety signals, not chatter.
    """

    tray: TrayIcon
    urgency: int
    timeout_ms: int
    transient: bool
    resident: bool
    category: str
    setting: str


_PAUSED = "notifications.paused"
_SHARED = "notifications.shared_or_edited"
_MASS_DELETE = "notifications.mass_delete"
_MEMORIES = "notifications.memories"
_OTHER_ACCOUNTS = "notifications.other_accounts"
_SYNC_ISSUES = "notifications.sync_issues"
_CONFLICTS = "notifications.conflicts"
_SYNC_COMPLETE = "notifications.sync_complete"
_ALWAYS = ""

POLICY: dict[NotificationId, ToastPolicy] = {
    NotificationId.SYNC_PAUSED_MANUAL: ToastPolicy(
        TrayIcon.PAUSED, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, False, True,
        CATEGORY_TRANSFER, _PAUSED),
    NotificationId.SYNC_PAUSED_METERED: ToastPolicy(
        TrayIcon.PAUSED, URGENCY_NORMAL, TIMEOUT_SERVER_DEFAULT, False, True,
        CATEGORY_NETWORK, _PAUSED),
    NotificationId.SYNC_PAUSED_BATTERY: ToastPolicy(
        TrayIcon.PAUSED, URGENCY_NORMAL, TIMEOUT_SERVER_DEFAULT, False, True,
        CATEGORY_DEVICE, _PAUSED),
    NotificationId.SYNC_RESUMED: ToastPolicy(
        TrayIcon.SYNCED, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_TRANSFER, _PAUSED),
    NotificationId.SYNC_ISSUES: ToastPolicy(
        TrayIcon.WARNING, URGENCY_NORMAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _SYNC_ISSUES),
    NotificationId.SIGN_IN_REQUIRED: ToastPolicy(
        TrayIcon.SIGNED_OUT, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _SYNC_ISSUES),
    NotificationId.ACCOUNT_BLOCKED: ToastPolicy(
        TrayIcon.BLOCKED, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _SYNC_ISSUES),
    NotificationId.QUOTA_WARNING: ToastPolicy(
        TrayIcon.WARNING, URGENCY_NORMAL, TIMEOUT_SERVER_DEFAULT, False, True,
        CATEGORY_TRANSFER_ERROR, _SYNC_ISSUES),
    NotificationId.QUOTA_FULL: ToastPolicy(
        TrayIcon.ERROR, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _SYNC_ISSUES),
    NotificationId.LOW_DISK: ToastPolicy(
        TrayIcon.WARNING, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_DEVICE, _SYNC_ISSUES),
    NotificationId.MASS_DELETE: ToastPolicy(
        TrayIcon.WARNING, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER, _MASS_DELETE),
    NotificationId.FIRST_DELETE: ToastPolicy(
        TrayIcon.INFO, URGENCY_NORMAL, TIMEOUT_SERVER_DEFAULT, False, False,
        CATEGORY_TRANSFER, _MASS_DELETE),
    NotificationId.SHARED_WITH_ME: ToastPolicy(
        TrayIcon.INFO, URGENCY_NORMAL, TIMEOUT_SERVER_DEFAULT, False, False,
        CATEGORY_TRANSFER_COMPLETE, _SHARED),
    NotificationId.SHARED_ITEM_EDITED: ToastPolicy(
        TrayIcon.INFO, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, False, False,
        CATEGORY_TRANSFER_COMPLETE, _SHARED),
    NotificationId.CONFLICT_DETECTED: ToastPolicy(
        TrayIcon.WARNING, URGENCY_NORMAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _CONFLICTS),
    NotificationId.FILE_BLOCKED: ToastPolicy(
        TrayIcon.ERROR, URGENCY_NORMAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _SYNC_ISSUES),
    NotificationId.NAME_INVALID: ToastPolicy(
        TrayIcon.WARNING, URGENCY_NORMAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _SYNC_ISSUES),
    NotificationId.MOUNT_LOST: ToastPolicy(
        TrayIcon.ERROR, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_DEVICE, _ALWAYS),
    NotificationId.MOUNT_RESTORED: ToastPolicy(
        TrayIcon.SYNCED, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_DEVICE, _ALWAYS),
    NotificationId.ENGINE_DEAD: ToastPolicy(
        TrayIcon.ERROR, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_DEVICE, _ALWAYS),
    NotificationId.NEEDS_RESYNC: ToastPolicy(
        TrayIcon.ERROR, URGENCY_CRITICAL, TIMEOUT_NEVER, False, True,
        CATEGORY_TRANSFER_ERROR, _ALWAYS),
    NotificationId.BACKUP_COMPLETE: ToastPolicy(
        TrayIcon.SYNCED, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_TRANSFER_COMPLETE, _SYNC_COMPLETE),
    NotificationId.DOWNLOAD_ALL_DONE: ToastPolicy(
        TrayIcon.SYNCED, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_TRANSFER_COMPLETE, _SYNC_COMPLETE),
    NotificationId.FREE_UP_SPACE_DONE: ToastPolicy(
        TrayIcon.SYNCED, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_TRANSFER_COMPLETE, _SYNC_COMPLETE),
    NotificationId.VAULT_WARNING: ToastPolicy(
        TrayIcon.INFO, URGENCY_NORMAL, TIMEOUT_NEVER, False, True,
        CATEGORY_DEVICE, _ALWAYS),
    NotificationId.VAULT_LOCKED: ToastPolicy(
        TrayIcon.INFO, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_DEVICE, _ALWAYS),
    NotificationId.MEMORIES: ToastPolicy(
        TrayIcon.INFO, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_TRANSFER_COMPLETE, _MEMORIES),
    NotificationId.OTHER_ACCOUNTS: ToastPolicy(
        TrayIcon.INFO, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_TRANSFER_COMPLETE, _OTHER_ACCOUNTS),
    NotificationId.SYNC_COMPLETE: ToastPolicy(
        TrayIcon.SYNCED, URGENCY_LOW, TIMEOUT_SERVER_DEFAULT, True, False,
        CATEGORY_TRANSFER_COMPLETE, _SYNC_COMPLETE),
}

#: Fallback for `is_enabled()` when no settings provider is injected. These are
#: the defaults from ARCHITECTURE.md §9 `notifications.*`; `config.py` (WP-01)
#: is the authority once the composition root injects it.
DEFAULT_ENABLED: dict[str, bool] = {
    _PAUSED: True,
    _SHARED: True,
    _MASS_DELETE: True,
    _MEMORIES: False,
    _OTHER_ACCOUNTS: False,
    _SYNC_ISSUES: True,
    _CONFLICTS: True,
    _SYNC_COMPLETE: True,
}

# Structural coverage, checked at import: a NotificationId added to models.py
# without a policy here fails the import rather than sending an untyped toast at
# 3 a.m. The same guard as `ui/icons.py` applies to TRAY_FOR_STATE.
_missing_policy = [n.name for n in NotificationId if n not in POLICY]
if _missing_policy:  # pragma: no cover - import-time contract check
    raise ValueError(f"notify: POLICY is missing {_missing_policy}")
_missing_toast = [n.name for n in NotificationId if n not in TOASTS]
if _missing_toast:  # pragma: no cover - import-time contract check
    raise ValueError(f"notify: strings.TOAST is missing {_missing_toast}")
_bad_setting = sorted(
    {p.setting for p in POLICY.values()} - set(DEFAULT_ENABLED) - {_ALWAYS}
)
if _bad_setting:  # pragma: no cover - import-time contract check
    raise ValueError(f"notify: POLICY names unknown settings {_bad_setting}")


# ─────────────────────────────────────────────────────────────────────────────
# Text safety — body-markup is ON
# ─────────────────────────────────────────────────────────────────────────────

def escape(text: object) -> str:
    """Escape a value for interpolation into a notification body.

    `body-markup` is advertised by this server, so a filename containing `&`,
    `<` or `>` would otherwise be parsed as Pango markup and either vanish or
    break the bubble.

    Args:
        text: Any value; stringified first.

    Returns:
        The markup-escaped text.
    """
    return GLib.markup_escape_text(str(text))


def markup_is_well_formed(body: str) -> bool:
    """Whether a body would survive the server's Pango markup parser.

    Args:
        body: The candidate body text.

    Returns:
        True if the body contains no markup at all, or parses as well-formed
        markup. A body with a stray `<` or a bare `&` returns False.
    """
    if "<" not in body and "&" not in body:
        return True
    try:
        ElementTree.fromstring(f"<span>{body}</span>")
    except ElementTree.ParseError:
        return False
    return True


def safe_body(body: str) -> str:
    """A body guaranteed to be safe to send with `body-markup` on.

    Deliberate markup written by `build()` (or by a caller that escaped its own
    values) is preserved; anything that would not parse is escaped wholesale, so
    a filename like `a<b&c.txt` shows literally instead of eating the bubble.

    Args:
        body: The candidate body text.

    Returns:
        `body` unchanged, or its fully escaped form.
    """
    if markup_is_well_formed(body):
        return body
    return escape(body)


def build(nid: NotificationId, *, account_id: str = "", **fmt: object) -> NotifySpec:
    """Build a `NotifySpec` for a toast from `strings.TOAST`.

    This is the only sanctioned way to produce a toast: the wording, the action
    ids and the action labels all come from the frozen table, and the format
    values are escaped **for the body only** — the summary is not markup-parsed,
    so escaping it there would show a literal `&amp;` to the user.

    Args:
        nid: Which toast.
        account_id: The account this toast belongs to, for multi-account routing.
        **fmt: Template values, e.g. `n=3`, `name="Report.docx"`.

    Returns:
        A `NotifySpec` ready for `Notifier.notify()`.

    Raises:
        KeyError: If `nid` is not a known `NotificationId`.
    """
    key = NotificationId(nid)
    summary_template, body_template, actions = TOASTS[key]
    policy = POLICY[key]
    summary = t(summary_template, **fmt)
    body = t(body_template, **{name: escape(value) for name, value in fmt.items()})
    return NotifySpec(
        id=key,
        summary=summary,
        body=body,
        actions=tuple(actions),
        urgency=policy.urgency,
        timeout_ms=policy.timeout_ms,
        transient=policy.transient,
        resident=policy.resident,
        account_id=account_id,
    )


def icon_name(nid: NotificationId) -> str:
    """The themed icon name for a toast.

    `TrayIcon`'s values *are* the installed icon names (see its docstring in
    `models.py`), so no icon-name literal appears here.

    Args:
        nid: Which toast.

    Returns:
        A themed icon name; the application icon when the policy names none.
    """
    tray = POLICY[NotificationId(nid)].tray
    return str(tray.value) if tray.value else APP_ID


# ─────────────────────────────────────────────────────────────────────────────
# The notifier
# ─────────────────────────────────────────────────────────────────────────────

class Notifier(QObject):
    """Sends every toast, and routes every action button back onto the bus.

    Signals:
        action_invoked: `(NotificationId value, action id)` when the user presses
            an action button, or clicks the bubble (action id `"default"`).
            Also mirrored onto `BUS.notification_action`.

    Attributes:
        MAX_ACTIONS: GNOME renders about three buttons; two is the cap the whole
            application designs to, and `strings.TOAST` never exceeds it.
        THROTTLE_MS: Minimum interval between two sends of the *same* toast.
            Re-notifying faster than ~1 Hz makes GNOME visibly re-animate the
            bubble, so a burst is coalesced into one delayed send.
    """

    action_invoked = Signal(str, str)

    MAX_ACTIONS: int = 2
    THROTTLE_MS: int = 1000

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        bus: Bus | None = None,
        settings: Callable[[str], bool] | Mapping[str, bool] | None = None,
        app_name: str = APP_DISPLAY_NAME,
        desktop_entry: str = APP_ID,
        listen_to_bus: bool = True,
    ) -> None:
        """Create a notifier and subscribe to the server's signals.

        Args:
            parent: Optional Qt parent.
            bus: The session bus to use. Defaults to the process-wide one.
            settings: Either a callable taking a dotted `notifications.*` key and
                returning a bool, or a mapping of the same. `None` uses
                `DEFAULT_ENABLED`.
            app_name: `app_name` sent to the server; GNOME shows the
                `desktop-entry` name instead, but other servers use this.
            desktop_entry: The `desktop-entry` hint — the `.desktop` basename.
                This is how GNOME shows our name and icon and groups our bubbles.
            listen_to_bus: Connect `BUS.toast_requested` to `notify()`, making
                the bus signal the application-wide way to raise a toast.

        Raises:
            SafetyRefusal: If constructed off the GUI thread.
        """
        assert_gui_thread("Notifier()")
        super().__init__(parent)
        self._bus = bus if bus is not None else Bus.session()
        self._settings = settings
        self._app_name = app_name
        self._desktop_entry = desktop_entry

        self._server_id: dict[NotificationId, int] = {}
        self._key_by_server: dict[int, NotificationId] = {}
        self._last_sent_ms: dict[NotificationId, float] = {}
        self._last_spec: dict[NotificationId, NotifySpec] = {}
        self._pending: dict[NotificationId, NotifySpec] = {}
        self._capabilities: frozenset[str] | None = None
        self._subscriptions: list[int] = []
        self._listening = False

        self.sent = 0
        self.suppressed = 0
        self.throttled = 0
        self.failed = 0
        self.last_close_reason: dict[NotificationId, int] = {}

        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self.flush_pending)

        # Action buttons only work if something drains the GLib context.
        glibpump.ensure_started()
        self._subscribe()
        if listen_to_bus:
            BUS.toast_requested.connect(self._on_bus_toast)
            self._listening = True

    # ── wiring ───────────────────────────────────────────────────────────────

    def _subscribe(self) -> None:
        """Subscribe to `ActionInvoked` and `NotificationClosed`."""
        self._subscriptions = [
            self._bus.subscribe(
                NOTIFY_NAME, NOTIFY_IFACE, "ActionInvoked", NOTIFY_PATH, self._on_action
            ),
            self._bus.subscribe(
                NOTIFY_NAME, NOTIFY_IFACE, "NotificationClosed", NOTIFY_PATH,
                self._on_closed,
            ),
        ]

    def shutdown(self) -> None:
        """Unsubscribe, unhook the bus and drop pending sends."""
        self._flush_timer.stop()
        self._pending.clear()
        for sub_id in self._subscriptions:
            self._bus.unsubscribe(sub_id)
        self._subscriptions.clear()
        if self._listening:
            try:
                BUS.toast_requested.disconnect(self._on_bus_toast)
            except (RuntimeError, TypeError):  # pragma: no cover - teardown race
                pass
            self._listening = False

    def _on_bus_toast(self, spec: object) -> None:
        """Slot for `BUS.toast_requested`.

        Args:
            spec: A `NotifySpec`. Anything else is logged and dropped, because a
                bad payload must never take the sync engine down.
        """
        if not isinstance(spec, NotifySpec):
            log.warning("toast_requested carried %r, not a NotifySpec", type(spec))
            return
        self.notify(spec)

    # ── settings ─────────────────────────────────────────────────────────────

    def set_settings(
        self, settings: Callable[[str], bool] | Mapping[str, bool] | None
    ) -> None:
        """Replace the notification-settings source.

        Args:
            settings: A callable, a mapping, or `None` for the built-in defaults.
        """
        self._settings = settings

    def is_enabled(self, nid: NotificationId) -> bool:
        """Whether the user has this class of toast switched on.

        Toasts whose policy names no setting — mount loss, a dead engine, a
        required resync, vault state — are always enabled: they are safety
        signals, not chatter.

        Args:
            nid: Which toast.

        Returns:
            True if the toast may be shown.
        """
        policy = POLICY.get(NotificationId(nid))
        if policy is None or not policy.setting:
            return True
        key = policy.setting
        source = self._settings
        if source is None:
            return DEFAULT_ENABLED.get(key, True)
        try:
            if callable(source):
                return bool(source(key))
            return bool(source.get(key, DEFAULT_ENABLED.get(key, True)))
        except Exception:  # noqa: BLE001 - a broken config must not block a toast
            log.exception("notification settings lookup for %s failed", key)
            return DEFAULT_ENABLED.get(key, True)

    # ── server capabilities ──────────────────────────────────────────────────

    def capabilities(self) -> frozenset[str]:
        """The server's capabilities, cached after the first successful read.

        On the target machine: `{actions, body, body-markup, icon-static,
        persistence, sound}` — no `body-images`, no `body-hyperlinks`, no
        `action-icons`, no `inline-reply`.

        Returns:
            The capability set; empty if the server could not be reached.
        """
        if self._capabilities is not None:
            return self._capabilities
        result = self._bus.call_or_none(
            NOTIFY_NAME, NOTIFY_PATH, NOTIFY_IFACE, "GetCapabilities", reply="(as)"
        )
        caps = frozenset(str(c) for c in result[0]) if result else frozenset()
        if caps:
            self._capabilities = caps
        return caps

    def refresh_capabilities(self) -> frozenset[str]:
        """Drop the cache and re-read the server's capabilities."""
        self._capabilities = None
        return self.capabilities()

    def server_info(self) -> tuple[str, str, str, str]:
        """`(name, vendor, version, spec_version)` from `GetServerInformation`.

        Returns:
            The four strings, or four empty strings if unavailable.
        """
        result = self._bus.call_or_none(
            NOTIFY_NAME, NOTIFY_PATH, NOTIFY_IFACE, "GetServerInformation",
            reply="(ssss)",
        )
        if not result or len(result) != 4:
            return ("", "", "", "")
        return (str(result[0]), str(result[1]), str(result[2]), str(result[3]))

    # ── sending ──────────────────────────────────────────────────────────────

    def notify(self, spec: NotifySpec) -> int:
        """Show (or update) a toast.

        The server id is remembered per `NotificationId`, so re-notifying the
        same toast **replaces the existing bubble in place** instead of stacking
        a second one — which is what makes "Syncing 12 files" a live-updating
        toast rather than a notification storm.

        Args:
            spec: What to show. `spec.id` selects the icon, the category and the
                config gate.

        Returns:
            The server's notification id, for `close()`. 0 when the toast was
            suppressed by settings, coalesced by the throttle before its first
            send, or rejected by the server.

        Raises:
            SafetyRefusal: If called off the GUI thread.
            TypeError: If `spec` is not a `NotifySpec`.
        """
        assert_gui_thread("Notifier.notify()")
        if not isinstance(spec, NotifySpec):
            raise TypeError(f"notify() takes a NotifySpec, got {type(spec).__name__}")
        key = NotificationId(spec.id)
        if not self.is_enabled(key):
            self.suppressed += 1
            log.debug("toast %s suppressed by settings", key.value)
            return 0

        now_ms = time.monotonic() * 1000.0
        last_ms = self._last_sent_ms.get(key)
        if last_ms is not None and (now_ms - last_ms) < self.THROTTLE_MS:
            if self._last_spec.get(key) == spec:
                # Identical content inside the window: nothing would change.
                self._pending.pop(key, None)
                return self._server_id.get(key, 0)
            self.throttled += 1
            self._pending[key] = spec
            self._arm_flush(self.THROTTLE_MS - (now_ms - last_ms))
            return self._server_id.get(key, 0)
        return self._send(spec)

    def _arm_flush(self, delay_ms: float) -> None:
        """Schedule `flush_pending()`, never later than an already-armed flush.

        Args:
            delay_ms: How long until the throttle window for the newest pending
                toast expires.
        """
        delay = max(1, int(delay_ms))
        if self._flush_timer.isActive() and self._flush_timer.remainingTime() <= delay:
            return
        self._flush_timer.start(delay)

    def flush_pending(self) -> int:
        """Send every pending toast whose throttle window has expired.

        Returns:
            The number of toasts sent.
        """
        if not self._pending:
            return 0
        now_ms = time.monotonic() * 1000.0
        soonest: float | None = None
        sent = 0
        for key in list(self._pending):
            last_ms = self._last_sent_ms.get(key)
            waited = self.THROTTLE_MS if last_ms is None else now_ms - last_ms
            if waited >= self.THROTTLE_MS:
                spec = self._pending.pop(key)
                self._send(spec)
                sent += 1
            else:
                remaining = self.THROTTLE_MS - waited
                soonest = remaining if soonest is None else min(soonest, remaining)
        if soonest is not None:
            self._arm_flush(soonest)
        return sent

    def _send(self, spec: NotifySpec) -> int:
        """Marshal and send one `Notify` call.

        Args:
            spec: What to show.

        Returns:
            The server's notification id, or 0 if the call failed.
        """
        key = NotificationId(spec.id)
        policy = POLICY[key]
        replaces_id = self._server_id.get(key, 0)
        actions = self._flat_actions(spec)
        hints = {
            # BYTE 'y', never 'i'. This is the single most common bug in
            # freedesktop notification code.
            HINT_URGENCY: GLib.Variant("y", self._clamp_urgency(spec.urgency)),
            # How GNOME shows our name and icon, and how it groups our bubbles.
            HINT_DESKTOP_ENTRY: GLib.Variant("s", self._desktop_entry),
            HINT_CATEGORY: GLib.Variant("s", policy.category),
            HINT_PRIVACY_SCOPE: GLib.Variant("s", PRIVACY_SCOPE_USER),
        }
        if spec.transient:
            hints[HINT_TRANSIENT] = GLib.Variant("b", True)
        if spec.resident:
            hints[HINT_RESIDENT] = GLib.Variant("b", True)

        summary = spec.summary or self._app_name
        body = safe_body(spec.body)
        args = (
            self._app_name,
            int(replaces_id),
            icon_name(key),
            summary,
            body,
            actions,
            hints,
            int(spec.timeout_ms),
        )
        try:
            result = self._bus.call(
                NOTIFY_NAME, NOTIFY_PATH, NOTIFY_IFACE, "Notify",
                signature=NOTIFY_SIGNATURE, args=args, reply=NOTIFY_REPLY,
                timeout_ms=NOTIFY_TIMEOUT_MS,
            )
        except GLib.Error as exc:
            self.failed += 1
            log.warning("Notify(%s) failed: %s", key.value, exc.message)
            return 0
        except (TypeError, ValueError) as exc:
            # A marshalling mistake is our bug; log it loudly and keep syncing.
            self.failed += 1
            log.exception("Notify(%s) could not be marshalled: %s", key.value, exc)
            return 0

        server_id = int(result[0]) if result else 0
        self.sent += 1
        self._last_sent_ms[key] = time.monotonic() * 1000.0
        self._last_spec[key] = spec
        if server_id:
            self._server_id[key] = server_id
            self._key_by_server[server_id] = key
        return server_id

    def _flat_actions(self, spec: NotifySpec) -> list[str]:
        """Flatten `((id, label), ...)` into the spec's `[id, label, ...]` form.

        The `"default"` action is the implicit whole-bubble click and does not
        count against `MAX_ACTIONS`. Anything beyond the cap is dropped with a
        warning rather than raising: a wording bug must not lose the toast.

        Args:
            spec: The toast being sent.

        Returns:
            The flat action list, empty when the server has no `actions`
            capability.
        """
        if not spec.actions:
            return []
        if CAP_ACTIONS not in self.capabilities():
            log.debug(
                "server has no 'actions' capability; dropping %d button(s) from %s",
                len(spec.actions), spec.id,
            )
            return []
        flat: list[str] = []
        buttons = 0
        for action_id, label in spec.actions:
            if action_id == DEFAULT_ACTION:
                flat.extend([action_id, str(label)])
                continue
            if buttons >= self.MAX_ACTIONS:
                log.warning(
                    "toast %s declares more than MAX_ACTIONS=%d buttons; %r dropped",
                    spec.id, self.MAX_ACTIONS, action_id,
                )
                continue
            flat.extend([str(action_id), str(label)])
            buttons += 1
        return flat

    @staticmethod
    def _clamp_urgency(urgency: int) -> int:
        """Clamp an urgency into the byte range the `y` hint accepts.

        Args:
            urgency: The requested urgency.

        Returns:
            An int in `[URGENCY_LOW, URGENCY_CRITICAL]`.
        """
        return max(URGENCY_LOW, min(URGENCY_CRITICAL, int(urgency)))

    # ── closing ──────────────────────────────────────────────────────────────

    def close(self, nid: int) -> None:
        """Close a notification by its **server** id.

        Args:
            nid: The id returned by `notify()`. 0 is ignored.
        """
        if not nid:
            return
        self._bus.call_or_none(
            NOTIFY_NAME, NOTIFY_PATH, NOTIFY_IFACE, "CloseNotification",
            signature=CLOSE_SIGNATURE, args=(int(nid),),
        )

    def close_toast(self, nid: NotificationId) -> None:
        """Close whichever bubble is currently showing a given toast.

        Args:
            nid: Which toast.
        """
        key = NotificationId(nid)
        self._pending.pop(key, None)
        server_id = self._server_id.get(key, 0)
        if server_id:
            self.close(server_id)

    def close_all(self) -> None:
        """Close every bubble this notifier currently owns."""
        for server_id in list(self._server_id.values()):
            self.close(server_id)

    def server_id_for(self, nid: NotificationId) -> int:
        """The server id currently associated with a toast, or 0.

        Args:
            nid: Which toast.

        Returns:
            The server's notification id, or 0 if none has been sent.
        """
        return self._server_id.get(NotificationId(nid), 0)

    # ── incoming signals ─────────────────────────────────────────────────────

    def _on_action(self, server_id: int, action_id: str) -> None:
        """Handle `ActionInvoked(uint32 id, string action_key)`.

        Args:
            server_id: The server's notification id.
            action_id: The action key, or `"default"` for the bubble click.
        """
        key = self._key_by_server.get(int(server_id))
        if key is None:
            return  # another application's notification
        log.info("toast %s action %r invoked", key.value, action_id)
        self.action_invoked.emit(str(key.value), str(action_id))
        BUS.notification_action.emit(str(key.value), str(action_id))

    def _on_closed(self, server_id: int, reason: int) -> None:
        """Handle `NotificationClosed(uint32 id, uint32 reason)`.

        The `NotificationId -> server id` mapping is deliberately **kept**: a
        stale `replaces_id` makes the server create a fresh bubble and return a
        fresh id, whereas dropping it here would race a replace that GNOME
        reports as a close of the old id.

        Args:
            server_id: The server's notification id.
            reason: 1 expired, 2 dismissed, 3 closed via the API, 4 undefined.
        """
        key = self._key_by_server.pop(int(server_id), None)
        if key is None:
            return
        self.last_close_reason[key] = int(reason)
        log.debug("toast %s closed (reason %d)", key.value, int(reason))

    # ── convenience ──────────────────────────────────────────────────────────

    def toast(
        self, nid: NotificationId, *, account_id: str = "", **fmt: object
    ) -> int:
        """`build()` + `notify()` in one call.

        Args:
            nid: Which toast.
            account_id: The account this toast belongs to.
            **fmt: Template values for `strings.TOAST`.

        Returns:
            The server's notification id, or 0.
        """
        return self.notify(build(nid, account_id=account_id, **fmt))

    def stats(self) -> dict[str, int]:
        """Counters for the About pane and the diagnostics bundle."""
        return {
            "sent": self.sent,
            "suppressed": self.suppressed,
            "throttled": self.throttled,
            "failed": self.failed,
            "pending": len(self._pending),
            "live": len(self._key_by_server),
        }


__all__ = [
    "CAP_ACTIONS",
    "CAP_BODY",
    "CAP_BODY_MARKUP",
    "CAP_ICON_STATIC",
    "CAP_PERSISTENCE",
    "CAP_SOUND",
    "CLOSE_API",
    "CLOSE_DISMISSED",
    "CLOSE_EXPIRED",
    "CLOSE_UNDEFINED",
    "DEFAULT_ACTION",
    "DEFAULT_ENABLED",
    "EXPECTED_CAPABILITIES",
    "NOTIFY_IFACE",
    "NOTIFY_NAME",
    "NOTIFY_PATH",
    "NOTIFY_REPLY",
    "NOTIFY_SIGNATURE",
    "POLICY",
    "TIMEOUT_NEVER",
    "TIMEOUT_SERVER_DEFAULT",
    "TOASTS",
    "URGENCY_CRITICAL",
    "URGENCY_LOW",
    "URGENCY_NORMAL",
    "Notifier",
    "ToastPolicy",
    "build",
    "escape",
    "icon_name",
    "markup_is_well_formed",
    "safe_body",
]
