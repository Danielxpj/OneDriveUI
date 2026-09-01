"""The one place a state becomes a toast or a banner.

Without this module there would be four: the tray would raise a toast, the
Activity Center would raise a banner, the Settings page would show its own
warning, and the notifier would fire on a bus signal. They would disagree within
a week, and the user would see "Your OneDrive is full" three times for one event
and not at all for the next.

So every notice — toast and banner alike — is decided here, from the state and
the facts, and the rules that make it bearable are:

**A toast is for a change, a banner is for a condition.** Entering
``PAUSED_QUOTA`` toasts once; *being* in ``PAUSED_QUOTA`` shows a banner for as
long as it lasts. Toasting a condition every tick is how a notification system
becomes something the user turns off.

**The settings toggles are honoured here, once.** Each toggle is checked in one
place, so "turn off sync issue notifications" cannot be obeyed by the tray and
ignored by the notifier.

**Every action button routes back onto the bus.** A toast's "Sync Anyway" and a
banner's "Free up space" reach the same
:meth:`~onedriveui.sync.supervisor.Supervisor.do` as the menu item of the same
name — so the guards apply identically no matter which of the three the user
pressed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.data import repo_files
from onedriveui.models import (
    AccountInfo,
    Facts,
    IssueCode,
    NotificationId,
    PauseReason,
    RecoveryAction,
    SyncIssue,
    SyncState,
)
from onedriveui.strings import ACTION_LABEL, issue_title

log = logging.getLogger(__name__)

#: How long the same toast is suppressed for, across restarts. Five minutes is
#: longer than any crash-restart loop the units allow (five starts in five
#: minutes) and shorter than any interval a user would call "it never tells me".
TOAST_MIN_INTERVAL_S: int = 300

__all__ = ["NoticeCenter", "Notice", "TOAST_FOR_STATE", "SETTING_FOR_TOAST"]


@dataclass(frozen=True, slots=True)
class Notice:
    """One banner: what it says, how loud, and what can be done about it.

    Attributes:
        code: The issue behind it, for the icon and the wording.
        title: The headline, always from ``strings``.
        detail: The second line, or ``""``.
        actions: ``(action, label)`` pairs. Each routes through ``do()``.
        severity: ``"error"``, ``"warning"`` or ``"info"`` — the InfoBar tone.
        dismissible: Whether the user may close it. A condition they cannot
            currently fix is **not** dismissible: closing "Your OneDrive is
            full" would leave a client that silently syncs nothing.
    """

    code: IssueCode
    title: str
    detail: str = ""
    actions: tuple[tuple[RecoveryAction, str], ...] = ()
    severity: str = "warning"
    dismissible: bool = True


#: State -> the toast raised on **entering** it. States absent from this table
#: are not worth interrupting anyone for.
TOAST_FOR_STATE: Final[dict[SyncState, NotificationId]] = {
    SyncState.PAUSED_QUOTA: NotificationId.QUOTA_FULL,
    SyncState.AUTH_REQUIRED: NotificationId.SIGN_IN_REQUIRED,
    SyncState.ACCOUNT_BLOCKED: NotificationId.ACCOUNT_BLOCKED,
    SyncState.WARNING: NotificationId.SYNC_ISSUES,
    SyncState.ERROR: NotificationId.MOUNT_LOST,
    SyncState.NEEDS_ATTENTION: NotificationId.MASS_DELETE,
    SyncState.PAUSED_METERED: NotificationId.SYNC_PAUSED_METERED,
    SyncState.PAUSED_BATTERY: NotificationId.SYNC_PAUSED_BATTERY,
    SyncState.PAUSED_MANUAL: NotificationId.SYNC_PAUSED_MANUAL,
    SyncState.UP_TO_DATE: NotificationId.SYNC_COMPLETE,
}

#: Toast -> the ``notifications.*`` config key that governs it. Checked in **one**
#: place, so a toggle cannot be honoured by one surface and ignored by another.
SETTING_FOR_TOAST: Final[dict[NotificationId, str]] = {
    NotificationId.SYNC_PAUSED_MANUAL: "notifications.paused",
    NotificationId.SYNC_PAUSED_METERED: "notifications.paused",
    NotificationId.SYNC_PAUSED_BATTERY: "notifications.paused",
    NotificationId.SYNC_RESUMED: "notifications.paused",
    NotificationId.SYNC_ISSUES: "notifications.sync_issues",
    NotificationId.CONFLICT_DETECTED: "notifications.conflicts",
    NotificationId.MASS_DELETE: "notifications.mass_delete",
    NotificationId.FIRST_DELETE: "notifications.mass_delete",
    NotificationId.SHARED_WITH_ME: "notifications.shared_or_edited",
    NotificationId.SHARED_ITEM_EDITED: "notifications.shared_or_edited",
    NotificationId.MEMORIES: "notifications.memories",
    NotificationId.OTHER_ACCOUNTS: "notifications.other_accounts",
    NotificationId.SYNC_COMPLETE: "notifications.sync_complete",
}

#: Conditions the user cannot dismiss, because dismissing them would leave a
#: client that appears healthy and silently syncs nothing.
_PERSISTENT: Final[frozenset[IssueCode]] = frozenset({
    IssueCode.QUOTA_EXCEEDED, IssueCode.DISK_FULL, IssueCode.AUTH_EXPIRED,
    IssueCode.AUTH_MFA, IssueCode.AUTH_TENANT_BLOCKED, IssueCode.MOUNT_DEAD,
    IssueCode.BISYNC_CRITICAL, IssueCode.NEEDS_RESYNC,
})


class NoticeCenter(QObject):
    """Turns states, latches and issues into toasts and banners. Once each.

    Args:
        account: The account.
        notifier: WP-10's :class:`~onedriveui.platform.notify.Notifier`.
        supervisor: The Supervisor, for the action buttons.
        config_get: ``(dotted_key, default) -> value`` for the notification
            toggles.
        parent: Qt parent.

    Signals:
        banner_changed: The :class:`Notice` to show, or ``None`` to clear it.
        toast_raised: The :class:`~onedriveui.models.NotificationId` that fired.
    """

    banner_changed = Signal(object)
    toast_raised = Signal(object)

    def __init__(
        self,
        account: AccountInfo,
        *,
        notifier: Any = None,
        supervisor: Any = None,
        config_get: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._notifier = notifier
        self._supervisor = supervisor
        self._config_get = config_get or (lambda key, default=None: True)
        self._banner: Notice | None = None
        self._dismissed: set[IssueCode] = set()

    def connect_bus(self) -> None:
        """Listen for the events that produce notices."""
        BUS.state_changed.connect(self._on_state_changed)
        BUS.issue_raised.connect(self._on_issue_raised)
        BUS.notification_action.connect(self._on_toast_action)

    # ═════════════════════════════════════════════════════════════════════════
    # Toasts — for changes
    # ═════════════════════════════════════════════════════════════════════════

    def _on_state_changed(self, old: SyncState, new: SyncState,
                          facts: Facts) -> None:
        if facts.account_id and facts.account_id != self.account.id:
            return
        if old is new:
            return
        self.set_banner(self.banner_for(new, facts))
        toast = TOAST_FOR_STATE.get(new)
        if toast is None:
            return
        if new is SyncState.UP_TO_DATE and old not in (SyncState.SYNCING,
                                                       SyncState.PROCESSING):
            # "Sync complete" is only news after something was syncing. Firing
            # it on every return to idle would toast on start-up and after every
            # transient hiccup.
            return
        self.raise_toast(toast, facts)

    def raise_toast(self, nid: NotificationId, facts: Facts | None = None,
                    **fmt: Any) -> bool:
        """Fire one toast, if the user has not turned this kind off.

        Args:
            nid: Which toast.
            facts: The observation, for the template values.
            **fmt: Extra template values.

        Returns:
            True when it was sent.
        """
        if not self.enabled(nid):
            log.debug("%s is turned off in settings", nid.value)
            return False
        if self._notifier is None:
            return False

        # Persistent rate limiting. `repo_files.should_show()` says why it lives
        # in the database rather than in the notifier: "so it survives a restart
        # — a crash loop must not produce a toast storm". It had no caller, so
        # the only thing standing between the user and a toast per launch was
        # this process's own memory, which a restart wipes. A client that
        # restarts five times in a minute — which is exactly what the mount
        # unit's `Restart=always` produces on a bad day — raised five identical
        # toasts.
        key = f"{self.account.id}:{nid.value}"
        try:
            if not repo_files.should_show(key,
                                          min_interval_s=TOAST_MIN_INTERVAL_S):
                log.debug("%s was shown too recently; not repeating it", nid.value)
                return False
        except Exception:  # noqa: BLE001 - a missing row must not silence a toast
            log.debug("could not check the toast rate limit", exc_info=True)
        numbers = dict(fmt)
        if facts is not None:
            numbers.setdefault("n", facts.issues_error or facts.issues_blocking)
        try:
            self._notifier.toast(nid, account_id=self.account.id, **numbers)
        except Exception:  # noqa: BLE001 - a missing notification daemon is not fatal
            log.warning("could not raise the %s toast", nid.value, exc_info=True)
            return False
        try:
            repo_files.note_notification(key, account_id=self.account.id)
        except Exception:  # noqa: BLE001 - the toast was already shown
            log.debug("could not record the toast", exc_info=True)
        self.toast_raised.emit(nid)
        return True

    def enabled(self, nid: NotificationId) -> bool:
        """Is this kind of toast turned on?

        Checked here and nowhere else, so a toggle cannot be honoured by the
        tray and ignored by the notifier.
        """
        key = SETTING_FOR_TOAST.get(nid)
        if key is None:
            return True          # hazards are not optional
        return bool(self._config_get(key, True))

    # ═════════════════════════════════════════════════════════════════════════
    # Banners — for conditions
    # ═════════════════════════════════════════════════════════════════════════

    def banner_for(self, state: SyncState, facts: Facts) -> Notice | None:
        """The banner a state calls for, or ``None``.

        This is what makes rungs 13–15 legible: while files are transferring the
        *headline* says so and the unresolved errors appear here, underneath,
        rather than replacing it. Both facts are visible at once, which is what
        Windows does and the only arrangement in which neither hides the other.
        """
        if state is SyncState.PAUSED_QUOTA:
            code = IssueCode.DISK_FULL if facts.out_of_space \
                else IssueCode.QUOTA_EXCEEDED
            return self._notice(code, severity="warning", actions=(
                (RecoveryAction.GET_MORE_STORAGE,
                 ACTION_LABEL[RecoveryAction.GET_MORE_STORAGE]),
                (RecoveryAction.FREE_UP_SPACE,
                 ACTION_LABEL[RecoveryAction.FREE_UP_SPACE])))

        if state is SyncState.AUTH_REQUIRED:
            return self._notice(IssueCode.AUTH_EXPIRED, severity="error",
                                actions=((RecoveryAction.SIGN_IN,
                                          ACTION_LABEL[RecoveryAction.SIGN_IN]),))

        if state is SyncState.ACCOUNT_BLOCKED:
            return self._notice(IssueCode.AUTH_TENANT_BLOCKED, severity="error",
                                actions=((RecoveryAction.OPEN_WEB,
                                          ACTION_LABEL[RecoveryAction.OPEN_WEB]),))

        if state is SyncState.ERROR:
            code = (IssueCode.MOUNT_DEAD if facts.mount.value == "stale"
                    else IssueCode.UNKNOWN)
            return self._notice(code, severity="error", actions=(
                (RecoveryAction.RESTART_MOUNT,
                 ACTION_LABEL[RecoveryAction.RESTART_MOUNT]),))

        if state is SyncState.NEEDS_ATTENTION:
            return self._notice(IssueCode.NEEDS_RESYNC, severity="warning",
                                actions=((RecoveryAction.RESYNC,
                                          ACTION_LABEL[RecoveryAction.RESYNC]),))

        if state in (SyncState.PAUSED_METERED, SyncState.PAUSED_BATTERY):
            return None          # the toast carries "Sync Anyway"; a banner too
                                 # would say the same thing twice

        if facts.issues_error and state in (SyncState.SYNCING,
                                            SyncState.PROCESSING,
                                            SyncState.WARNING):
            return self._notice(IssueCode.UPLOAD_FAILED, severity="warning",
                                n=facts.issues_error, actions=(
                                    (RecoveryAction.RETRY,
                                     ACTION_LABEL[RecoveryAction.RETRY]),))

        if state is SyncState.INFO_NOTICE and facts.info_notice:
            return Notice(code=IssueCode.ORPHANED_CACHE,
                          title=facts.info_notice, severity="info",
                          actions=((RecoveryAction.RECLAIM_CACHE,
                                    ACTION_LABEL[RecoveryAction.RECLAIM_CACHE]),))
        return None

    def _notice(self, code: IssueCode, *, severity: str = "warning",
                actions: tuple[tuple[RecoveryAction, str], ...] = (),
                **fmt: Any) -> Notice:
        return Notice(code=code, title=issue_title(code, **fmt),
                      actions=actions, severity=severity,
                      dismissible=code not in _PERSISTENT)

    def set_banner(self, notice: Notice | None) -> None:
        """Publish a banner, or clear it. Emits only on a change."""
        if notice is not None and notice.code in self._dismissed:
            notice = None
        if notice == self._banner:
            return
        self._banner = notice
        self.banner_changed.emit(notice)

    def banner(self) -> Notice | None:
        return self._banner

    def dismiss(self) -> bool:
        """Close the current banner, if the user is allowed to.

        Returns:
            True when it was closed.

        A non-dismissible banner stays. Letting the user close "Your OneDrive is
        full" would leave a client that looks healthy and silently syncs
        nothing — the notice is the only remaining signal that anything is wrong.
        """
        if self._banner is None:
            return True
        if not self._banner.dismissible:
            log.debug("%s cannot be dismissed while it is still true",
                      self._banner.code.value)
            return False
        self._dismissed.add(self._banner.code)
        self._banner = None
        self.banner_changed.emit(None)
        return True

    def _on_issue_raised(self, issue: SyncIssue) -> None:
        """A newly raised issue clears its own dismissal.

        The user dismissed a *previous* occurrence; a fresh one is new
        information and gets to be shown again.
        """
        if issue.account_id != self.account.id:
            return
        self._dismissed.discard(issue.code)

    # ═════════════════════════════════════════════════════════════════════════
    # Actions
    # ═════════════════════════════════════════════════════════════════════════

    def _on_toast_action(self, key: str, action_id: str) -> None:
        """A toast's button was pressed. Route it exactly like a menu item."""
        if action_id == "sync_anyway":
            self.sync_anyway()
            return
        try:
            action = RecoveryAction(action_id)
        except ValueError:
            log.debug("toast %s reported an unknown action %r", key, action_id)
            return
        self.act(action)

    def act(self, action: RecoveryAction, **kw: Any) -> None:
        """Perform a notice's action — through ``do()``, like everything else."""
        if self._supervisor is None:
            log.debug("no supervisor wired; %s was not performed", action.value)
            return
        self._supervisor.do(action, **kw)

    def sync_anyway(self) -> None:
        """The automatic pauses' one button.

        Overrides *this* pause for a window; it does not turn the policy off.
        The user is saying "not right now", not "never ask again about metered
        connections", and a toast button must not be able to disable a
        safeguard permanently.
        """
        if self._supervisor is None:
            return
        pause = getattr(self._supervisor, "_pause", None)
        if pause is None or not hasattr(pause, "sync_anyway"):
            self._supervisor.request_resume()
            return
        for reason in (PauseReason.METERED, PauseReason.BATTERY):
            if pause.active() is reason:
                pause.sync_anyway(reason)
                return
