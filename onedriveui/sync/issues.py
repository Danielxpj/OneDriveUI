"""Everything that went wrong, once each, with something to do about it.

An issue is a problem the user can act on. That definition does the work: a
transient 429 is not an issue (rclone retries it and it goes away), a file that
cannot be uploaded because its name contains a colon *is* one, and the
difference is whether there is a button worth showing.

Three properties are what make an issue list usable rather than a wall:

**Deduplication.** An issue is keyed on ``(account, code, rel_path)``. The same
file failing every four hundred milliseconds for an hour is **one** row with an
``occurrences`` counter, not nine thousand. Without this the list is unreadable
within a minute of anything going wrong, and the one genuinely new problem is
buried under repeats of the old one.

**Auto-resolution.** :meth:`IssueEngine.reconcile` closes issues the world has
already fixed — the token that was renewed, the drive that has space again, the
mount that came back. An error list that only ever grows teaches the user to
ignore it, and then it is worth nothing when it matters.

**Every issue carries its fixes.** :data:`~onedriveui.errors.ACTIONS_FOR` maps
each code to the actions that can resolve it, and they are executed through
:meth:`IssueEngine.execute`. An issue with no action is a complaint; an issue
with a wrong action is worse than a complaint.

One deliberate omission: nothing here ever calls ``core/stats-reset``. That call
also wipes ``core/transferred``, which is the only record that a transfer
happened at all, so resetting from an error path would destroy the activity
history while trying to tidy up a counter.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.data import repo_sync
from onedriveui.errors import ACTIONS_FOR, classify
from onedriveui.models import (
    AccountInfo,
    ActivityEvent,
    BisyncState,
    ConflictInfo,
    DaemonHealth,
    Facts,
    IssueCode,
    IssueSeverity,
    MountHealth,
    RecoveryAction,
    SyncIssue,
    TokenHealth,
    utcnow_iso,
)
from onedriveui.strings import issue_title
from onedriveui.sync.preflight import Violation

log = logging.getLogger(__name__)

__all__ = ["IssueEngine", "AUTO_RESOLVE"]

#: Codes that a healthy observation closes on its own, and the predicate that
#: says so. Everything absent from this table needs a human — a bad filename
#: does not fix itself, and pretending otherwise makes the issue disappear while
#: the file stays unsynced.
AUTO_RESOLVE: Final[dict[IssueCode, str]] = {
    IssueCode.AUTH_EXPIRED: "the token was renewed",
    IssueCode.AUTH_MFA: "the token was renewed",
    IssueCode.AUTH_TENANT_BLOCKED: "the tenant allowed the application",
    IssueCode.QUOTA_EXCEEDED: "the drive has space again",
    IssueCode.DISK_FULL: "the local disk has space again",
    IssueCode.NETWORK_UNREACHABLE: "the network came back",
    IssueCode.MOUNT_DEAD: "the mount came back",
    IssueCode.THROTTLED: "the throttling stopped",
    IssueCode.NEEDS_RESYNC: "the resync succeeded",
    IssueCode.BISYNC_LOCK_STUCK: "the lock cleared",
    IssueCode.BISYNC_CRITICAL: "a run completed cleanly",
    IssueCode.ORPHANED_CACHE: "the orphaned cache was reclaimed",
}


class IssueEngine(QObject):
    """Raises, deduplicates, resolves and fixes issues.

    Args:
        account: The account.
        writer: The database writer.
        supervisor: The :class:`~onedriveui.sync.supervisor.Supervisor`, for the
            actions this engine does not perform itself. Injected rather than
            imported, so the two are not circular.
        pinner: WP-08's pinner, for the space actions.
        selective: WP-08's selective-sync service, for "Stop syncing this item".
        conflicts: The conflict detector, for the three conflict answers.
        parent: Qt parent.

    Signals:
        raised: A new or re-observed :class:`~onedriveui.models.SyncIssue`.
        resolved: ``(issue_id, resolution)``.
    """

    raised = Signal(SyncIssue)
    resolved = Signal(int, str)

    def __init__(
        self,
        account: AccountInfo,
        *,
        writer: Any = None,
        supervisor: Any = None,
        pinner: Any = None,
        selective: Any = None,
        conflicts: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._writer = writer
        self._supervisor = supervisor
        self._pinner = pinner
        self._selective = selective
        self._conflicts = conflicts

    # ═════════════════════════════════════════════════════════════════════════
    # Raising
    # ═════════════════════════════════════════════════════════════════════════

    def raise_issue(self, code: IssueCode, *, rel_path: str = "",
                    detail: str = "", raw: str = "",
                    severity: IssueSeverity | None = None,
                    actions: tuple[RecoveryAction, ...] | None = None,
                    **fmt: Any) -> int | None:
        """Record an issue, or bump the one that is already there.

        Args:
            code: What went wrong.
            rel_path: The file, or ``""`` for an account-wide problem.
            detail: The specific explanation — the offending characters, the
                measured size. Shown beside the title.
            raw: The original error text, for the diagnostics bundle. Never
                shown to the user: rclone's wording is precise and unreadable.
            severity: Overrides the default for this code.
            actions: Overrides :data:`~onedriveui.errors.ACTIONS_FOR`.
            **fmt: Values for the title template.

        Returns:
            The issue id, or ``None`` when it could not be recorded.

        The upsert is on ``(account, code, rel_path)``, so a file failing every
        tick for an hour stays one row with a counter rather than becoming nine
        thousand.
        """
        issue = SyncIssue(
            account_id=self.account.id,
            code=code,
            severity=severity or _default_severity(code),
            rel_path=rel_path,
            title=issue_title(code, **fmt),
            detail=detail,
            raw_error=raw,
            actions=tuple(actions if actions is not None else ACTIONS_FOR.get(code, ())),
            first_seen_at=utcnow_iso(),
            last_seen_at=utcnow_iso(),
        )
        try:
            issue_id = repo_sync.raise_issue(issue, writer=self._writer)
        except Exception:  # noqa: BLE001 - failing to log a failure must not cascade
            log.error("could not record the %s issue on %r",
                      code.value, rel_path, exc_info=True)
            return None

        stored = _with(issue, id=issue_id)
        log.info("issue %s: %s %s", issue_id, code.value, rel_path or "(account)")
        self.raised.emit(stored)
        BUS.issue_raised.emit(stored)
        return issue_id

    def ingest_transfer_error(self, event: ActivityEvent) -> int | None:
        """Turn a failed transfer into an issue, if it is worth one.

        Args:
            event: The failed :class:`~onedriveui.models.ActivityEvent`.

        Returns:
            The issue id, or ``None`` when the error was benign.

        The direction matters: an unrecognised upload failure and an
        unrecognised download failure need different wording and different
        fixes, so it is passed to :func:`~onedriveui.errors.classify` rather
        than defaulted to ``UNKNOWN``.
        """
        if not event.error:
            return None
        code, severity, actions = classify(
            event.error, rel_path=event.rel_path, direction=event.direction)
        return self.raise_issue(code, rel_path=event.rel_path,
                                detail=event.error, raw=event.error,
                                severity=severity, actions=actions)

    def ingest_log_record(self, record: dict[str, Any], run_id: str = "") -> int | None:
        """Turn one bisync log line into an issue, if it is worth one."""
        text = str(record.get("msg") or record.get("message") or "")
        if not text or str(record.get("level", "")).lower() not in ("error", "fatal"):
            return None
        code, severity, actions = classify(text, rel_path=record.get("object"))
        return self.raise_issue(code, rel_path=str(record.get("object") or ""),
                                detail=text, raw=text, severity=severity,
                                actions=actions)

    def ingest_health(self, facts: Facts) -> None:
        """Raise the account-wide issues the ladder's rungs are built on.

        These are the ones with no file attached: the mount is gone, the token
        expired, the drive is full. They are raised here rather than derived in
        the reducer because an issue is a *durable* record with a fix attached,
        and the ladder only needs a count.
        """
        if facts.mount is MountHealth.STALE:
            self.raise_issue(IssueCode.MOUNT_DEAD,
                             detail="the FUSE mount stopped responding")
        if facts.token is TokenHealth.EXPIRED:
            self.raise_issue(IssueCode.AUTH_EXPIRED)
        elif facts.token is TokenHealth.MFA:
            self.raise_issue(IssueCode.AUTH_MFA)
        elif facts.token is TokenHealth.TENANT_BLOCKED:
            self.raise_issue(IssueCode.AUTH_TENANT_BLOCKED)
        if facts.quota.is_full:
            self.raise_issue(IssueCode.QUOTA_EXCEEDED)
        if facts.out_of_space:
            self.raise_issue(IssueCode.DISK_FULL)
        if facts.bisync is BisyncState.LOCK_STUCK:
            self.raise_issue(IssueCode.BISYNC_LOCK_STUCK)
        elif facts.bisync is BisyncState.CRITICAL:
            self.raise_issue(IssueCode.BISYNC_CRITICAL)
        elif facts.bisync is BisyncState.NEEDS_RESYNC:
            self.raise_issue(IssueCode.NEEDS_RESYNC)

    def ingest_preflight(self, violations: list[Violation]) -> None:
        """Record name and size problems found before an upload was attempted.

        Catching them here is the whole point of the preflight scan: the
        alternative is the user learning about a colon in a filename minutes
        later, from a failed upload, on a file they have moved on from.
        """
        for violation in violations:
            self.raise_issue(violation.code, rel_path=violation.rel_path,
                             detail=violation.detail)

    def raise_conflict(self, conflict: ConflictInfo) -> int | None:
        """Surface a conflict as an issue carrying its three answers."""
        return self.raise_issue(
            IssueCode.CONFLICT, rel_path=conflict.rel_path,
            detail=f"the other copy is at {conflict.loser_path}")

    # ═════════════════════════════════════════════════════════════════════════
    # Resolving
    # ═════════════════════════════════════════════════════════════════════════

    def reconcile(self, facts: Facts) -> int:
        """Close the issues the world has already fixed.

        Args:
            facts: The current observation.

        Returns:
            How many were auto-resolved.

        A list that only ever grows teaches the user to stop reading it, and
        then it is worth nothing on the day it matters. Only the codes in
        :data:`AUTO_RESOLVE` are eligible: a bad filename does not fix itself,
        and closing it because nothing failed this tick would hide a file that
        is still not syncing.
        """
        healed: list[tuple[IssueCode, str]] = []

        if facts.token is TokenHealth.OK:
            for code in (IssueCode.AUTH_EXPIRED, IssueCode.AUTH_MFA,
                         IssueCode.AUTH_TENANT_BLOCKED):
                healed.append((code, AUTO_RESOLVE[code]))
        # `total == 0` means `about` has not answered yet, not that space
        # appeared; closing on that would clear a genuinely full drive's issue
        # every time the network hiccupped.
        if facts.quota.total > 0 and not facts.quota.is_full:
            healed.append((IssueCode.QUOTA_EXCEEDED,
                           AUTO_RESOLVE[IssueCode.QUOTA_EXCEEDED]))
        if not facts.out_of_space:
            healed.append((IssueCode.DISK_FULL, AUTO_RESOLVE[IssueCode.DISK_FULL]))
        if facts.mount is MountHealth.UP:
            healed.append((IssueCode.MOUNT_DEAD, AUTO_RESOLVE[IssueCode.MOUNT_DEAD]))
        if facts.daemon_rcd is DaemonHealth.UP and facts.network.value == "online":
            healed.append((IssueCode.NETWORK_UNREACHABLE,
                           AUTO_RESOLVE[IssueCode.NETWORK_UNREACHABLE]))
        if facts.bisync in (BisyncState.IDLE, BisyncState.DISABLED):
            for code in (IssueCode.BISYNC_LOCK_STUCK, IssueCode.BISYNC_CRITICAL):
                healed.append((code, AUTO_RESOLVE[code]))
        if facts.transfers_active == 0:
            healed.append((IssueCode.THROTTLED, AUTO_RESOLVE[IssueCode.THROTTLED]))

        closed = 0
        for code, reason in healed:
            closed += self._resolve_code(code, reason)
        if closed:
            log.info("auto-resolved %d issues for %s", closed, self.account.id)
        return closed

    def _resolve_code(self, code: IssueCode, resolution: str) -> int:
        try:
            ids = repo_sync.resolve_issues_by_code(
                self.account.id, code, resolution, writer=self._writer)
        except Exception:  # noqa: BLE001
            log.error("could not auto-resolve %s", code.value, exc_info=True)
            return 0
        for issue_id in ids or ():
            self.resolved.emit(issue_id, resolution)
            BUS.issue_resolved.emit(issue_id)
        return len(ids or ())

    def resolve(self, issue_id: int, resolution: str) -> None:
        """Close one issue explicitly."""
        repo_sync.resolve_issue(issue_id, resolution, writer=self._writer)
        self.resolved.emit(issue_id, resolution)
        # `BUS.issue_resolved` carries the id alone; the resolution text is on
        # the row, and a listener that needs it reads the row.
        BUS.issue_resolved.emit(issue_id)

    def mute(self, issue_id: int) -> None:
        """Stop an issue counting toward the tray icon.

        Muting hides it from the *counts*, not from the list. A muted issue must
        not keep the tray in ERROR — that is the whole reason a user mutes one —
        but it stays visible and unresolved, because the file still is not
        syncing and pretending otherwise would be a lie told for tidiness.
        """
        repo_sync.mute_issue(issue_id, muted=True, writer=self._writer)
        log.info("issue %s muted; it no longer colours the tray icon", issue_id)

    def open_issues(self) -> list[SyncIssue]:
        return repo_sync.open_issues(self.account.id)

    def counts(self, account_id: str | None = None) -> tuple[int, int, int]:
        """``(blocking, error, warning)`` for the ladder and the badge."""
        counts = repo_sync.issue_counts(account_id or self.account.id)
        return (counts[IssueSeverity.BLOCKING.value],
                counts[IssueSeverity.ERROR.value],
                counts[IssueSeverity.WARNING.value])

    # ═════════════════════════════════════════════════════════════════════════
    # Fixing
    # ═════════════════════════════════════════════════════════════════════════

    def execute(self, action: RecoveryAction, issue: SyncIssue | None = None,
                **kw: Any) -> bool:
        """Perform one recovery action on one issue.

        Args:
            action: What to do. Must be one of the issue's own ``actions``,
                which is what the UI offered.
            issue: The issue being fixed, or ``None`` for an account-wide one.
            **kw: Action arguments — ``new_name`` for a rename, and so on.

        Returns:
            True when the action was performed. False means it could not be —
            a missing service, an action this engine does not own — and the
            issue is left open rather than being marked fixed.

        Raises:
            KeyError: The action has no handler. Loud on purpose: an action that
                silently passes is a button that does nothing.
        """
        handler = self._HANDLERS.get(action)
        if handler is None:
            raise KeyError(f"no handler for recovery action {action!r}")
        log.info("executing %s on issue %s (%s)", action.value,
                 getattr(issue, "id", None), getattr(issue, "rel_path", ""))
        try:
            return bool(getattr(self, handler)(issue, **kw))
        except Exception:  # noqa: BLE001 - a failed fix leaves the issue open
            log.error("recovery action %s failed", action.value, exc_info=True)
            return False

    # ── handled here ────────────────────────────────────────────────────────
    def _fix_retry(self, issue: SyncIssue | None, **kw: Any) -> bool:
        """Close the issue and let the next tick try again.

        There is nothing to re-run: the VFS write-back queue retries on its own
        schedule, so "Try again" means "stop showing me this, I expect it to
        work now" rather than a command to rclone.
        """
        if issue is not None and issue.id:
            self.resolve(issue.id, "retried")
        return True

    def _fix_skip(self, issue: SyncIssue | None, **kw: Any) -> bool:
        """Ignore this one. The file stays exactly where it is."""
        if issue is not None and issue.id:
            self.resolve(issue.id, "ignored")
        return True

    def _fix_rename(self, issue: SyncIssue | None, *, new_name: str = "",
                    **kw: Any) -> bool:
        """Rename a file so OneDrive will accept it.

        The default comes from :func:`~onedriveui.sync.preflight.suggest`, which
        is deterministic — the same offending name always proposes the same
        replacement, so two machines repairing the same file agree.
        """
        if issue is None or not issue.rel_path:
            return False
        from pathlib import Path

        from onedriveui.sync.preflight import suggest

        source = Path(self.account.sync_root).expanduser() / issue.rel_path
        target = source.with_name(new_name or suggest(source.name))
        if target == source:
            return False
        if not source.exists():
            log.warning("cannot rename %s: it is no longer there", source)
            return False
        # `Path.rename` is `rename(2)`: it replaces the destination silently and
        # irreversibly. The fix for "this name is not allowed" must never be
        # "and your other file is gone" — and the suggested name is deterministic,
        # so two files hitting the same rule propose the *same* target, which is
        # exactly when this collides.
        if target.exists():
            log.warning("cannot rename %s to %s: the target already exists",
                        source, target.name)
            return False
        source.rename(target)
        self.resolve(issue.id, f"renamed to {target.name}")
        return True

    def _fix_mute(self, issue: SyncIssue | None, **kw: Any) -> bool:
        if issue is None or not issue.id:
            return False
        self.mute(issue.id)
        return True

    # ── delegated ───────────────────────────────────────────────────────────
    def _via_supervisor(self, action: RecoveryAction, issue: SyncIssue | None,
                        **kw: Any) -> bool:
        if self._supervisor is None:
            log.warning("no supervisor wired; %s was not performed", action.value)
            return False
        self._supervisor.do(action, issue=issue, **kw)
        return True

    def _fix_sign_in(self, issue, **kw):
        return self._via_supervisor(RecoveryAction.SIGN_IN, issue, **kw)

    def _fix_free_up_space(self, issue, **kw):
        kw.setdefault("rel_path", getattr(issue, "rel_path", "") or None)
        return self._via_supervisor(RecoveryAction.FREE_UP_SPACE, issue, **kw)

    def _fix_get_more_storage(self, issue, **kw):
        return self._via_supervisor(RecoveryAction.GET_MORE_STORAGE, issue, **kw)

    def _fix_resync(self, issue, **kw):
        return self._via_supervisor(RecoveryAction.RESYNC, issue, **kw)

    def _fix_restart_mount(self, issue, **kw):
        kw.setdefault("reason", "the user asked from the issue list")
        return self._via_supervisor(RecoveryAction.RESTART_MOUNT, issue, **kw)

    def _fix_reclaim_cache(self, issue, **kw):
        return self._via_supervisor(RecoveryAction.RECLAIM_CACHE, issue, **kw)

    def _fix_open_web(self, issue, **kw):
        return self._via_supervisor(RecoveryAction.OPEN_WEB, issue, **kw)

    def _fix_show_in_folder(self, issue, **kw):
        kw.setdefault("path", str(issue.rel_path) if issue else "")
        return self._via_supervisor(RecoveryAction.SHOW_IN_FOLDER, issue, **kw)

    def _fix_unlock_bisync(self, issue, **kw):
        return self._via_supervisor(RecoveryAction.UNLOCK_BISYNC, issue, **kw)

    def _fix_force_delete(self, issue, **kw):
        """Proceed with a delete a safety abort stopped. **Needs a decision.**

        Never reachable without an answered decision row: the guard lives in
        `Supervisor.request_resync` / `bisync.assert_resync_approved`, and this
        only forwards the request.
        """
        return self._via_supervisor(RecoveryAction.FORCE_DELETE, issue, **kw)

    def _fix_restore_from_backup(self, issue, **kw):
        return self._via_supervisor(RecoveryAction.RESTORE_FROM_BACKUP, issue, **kw)

    def _fix_stop_syncing_item(self, issue, **kw):
        """Exclude an item from sync. **Never deletes it.**

        The local copy stays until a resync has succeeded, and even then the
        prune goes to the freedesktop trash rather than to `unlink()` — see
        invariant I10 and WP-08's selective sync.
        """
        if self._selective is None:
            return self._via_supervisor(RecoveryAction.STOP_SYNCING_ITEM, issue, **kw)
        self._selective.exclude(getattr(issue, "rel_path", "") or kw.get("rel_path", ""))
        return True

    # ── conflicts ───────────────────────────────────────────────────────────
    def _fix_keep_both(self, issue, **kw):
        return self._resolve_conflict(issue, "keep_both")

    def _fix_keep_local(self, issue, **kw):
        return self._resolve_conflict(issue, "keep_local")

    def _fix_keep_cloud(self, issue, **kw):
        return self._resolve_conflict(issue, "keep_cloud")

    def _resolve_conflict(self, issue: SyncIssue | None, resolution: str) -> bool:
        if issue is None or not issue.rel_path:
            return False
        if self._conflicts is not None:
            for conflict in self._conflicts.open_conflicts():
                if conflict.rel_path == issue.rel_path and conflict.id:
                    self._conflicts.resolve(conflict.id, resolution)
        if issue.id:
            self.resolve(issue.id, resolution)
        return True

    #: Every :class:`~onedriveui.models.RecoveryAction`, mapped to a method. A
    #: test asserts the coverage is total, because `ACTIONS_FOR` can offer any
    #: of them and an offered button that does nothing is worse than no button.
    _HANDLERS: Final[dict[RecoveryAction, str]] = {
        RecoveryAction.RETRY: "_fix_retry",
        RecoveryAction.RENAME: "_fix_rename",
        RecoveryAction.SKIP: "_fix_skip",
        RecoveryAction.KEEP_BOTH: "_fix_keep_both",
        RecoveryAction.KEEP_LOCAL: "_fix_keep_local",
        RecoveryAction.KEEP_CLOUD: "_fix_keep_cloud",
        RecoveryAction.SIGN_IN: "_fix_sign_in",
        RecoveryAction.FREE_UP_SPACE: "_fix_free_up_space",
        RecoveryAction.GET_MORE_STORAGE: "_fix_get_more_storage",
        RecoveryAction.RESYNC: "_fix_resync",
        RecoveryAction.FORCE_DELETE: "_fix_force_delete",
        RecoveryAction.RESTORE_FROM_BACKUP: "_fix_restore_from_backup",
        RecoveryAction.UNLOCK_BISYNC: "_fix_unlock_bisync",
        RecoveryAction.RESTART_MOUNT: "_fix_restart_mount",
        RecoveryAction.RECLAIM_CACHE: "_fix_reclaim_cache",
        RecoveryAction.OPEN_WEB: "_fix_open_web",
        RecoveryAction.SHOW_IN_FOLDER: "_fix_show_in_folder",
        RecoveryAction.STOP_SYNCING_ITEM: "_fix_stop_syncing_item",
    }


def _default_severity(code: IssueCode) -> IssueSeverity:
    """How loud an issue of this code is by default.

    Blocking means nothing can progress until it is dealt with, and it is what
    puts the tray icon into ERROR — so it is reserved for the account-wide
    problems that really do stop everything, never for one bad filename among
    thirty thousand good ones.
    """
    blocking = {
        IssueCode.AUTH_EXPIRED, IssueCode.AUTH_MFA, IssueCode.AUTH_TENANT_BLOCKED,
        IssueCode.QUOTA_EXCEEDED, IssueCode.DISK_FULL, IssueCode.MOUNT_DEAD,
        IssueCode.BISYNC_CRITICAL, IssueCode.BISYNC_LOCK_STUCK,
        IssueCode.MALWARE_DETECTED, IssueCode.MASS_DELETE_BLOCKED,
    }
    warning = {
        IssueCode.THROTTLED, IssueCode.ORPHANED_CACHE, IssueCode.ONENOTE_HIDDEN,
        IssueCode.PARTIAL_FILE_FOUND, IssueCode.VAULT_INACCESSIBLE,
        IssueCode.NETWORK_UNREACHABLE,
    }
    if code in blocking:
        return IssueSeverity.BLOCKING
    if code in warning:
        return IssueSeverity.WARNING
    return IssueSeverity.ERROR


def _with(issue: SyncIssue, **changes: Any) -> SyncIssue:
    from dataclasses import replace

    return replace(issue, **changes)
