"""Scriptable stubs for the five engine services the UI talks to.

Every UI work package (WP-11 … WP-14) must be renderable and testable with no
rclone, no daemon and no database. These stubs implement the frozen signatures
of CONTRACTS §10.6 – §10.8 (plus `QuotaService` from ARCHITECTURE §8.4 and
`Notifier` from §10.10) and add a small scripting surface on top:

    services.drive_state(SyncState.SYNCING)   # tray, headline, banner, all of it
    services.raise_every_issue()              # one open issue per IssueCode
    services.fire_every_toast()               # one NotifySpec per NotificationId

Rules kept faithful to the real services:
  * every world-changing action funnels through `Supervisor.do()`, and each call
    is recorded rather than executed;
  * `request_resync()` raises `SafetyRefusal` without an ANSWERED decision row
    (invariant I15);
  * `ShareService.can_revoke()`-style honesty: nothing pretends to succeed at
    something rclone cannot do;
  * signals go out on the real `BUS`, so a widget under test wires up exactly as
    it will in production.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Iterator

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.constants import (
    CACHE_SCAN_INTERVAL_S, MAX_CONCURRENT_PINS, QUOTA_TTL_S, TICK_ACTIVE_MS,
    TICK_IDLE_MS, TOKEN_KEEPALIVE_S, VERIFY_INTERVAL_S,
)
from onedriveui.errors import ACTIONS_FOR, SafetyRefusal, classify
from onedriveui.models import (
    AccountInfo, ActivityEvent, ActivityState, ActivityVerb, BisyncState,
    DaemonHealth, Decision, DecisionKind, Facts, FileState, FileStatus,
    IssueCode, IssueSeverity, MountHealth, NetworkState, NotificationId,
    NotifySpec, PauseIntent, PauseReason, PowerState, QuotaInfo, RecoveryAction,
    SyncIssue, SyncSnapshot, SyncState, TokenHealth, TrayIcon, utcnow_iso,
)
from onedriveui.strings import (
    ISSUE_TITLE, TOAST, TRAY_FOR_STATE, status_line, status_sub, t as _t,
    toast as _toast,
)

__all__ = [
    "FakeServices", "FakeSupervisor", "FakePinner", "FakeIssueEngine",
    "FakeQuotaService", "FakePauseManager", "FakePauseController", "FakeNotifier",
    "ACCOUNT", "facts_for", "snapshot_for",
]

#: The account every stub answers about unless a test supplies another.
ACCOUNT = AccountInfo(
    id="onedrive", remote="onedrive", display_name="Test User",
    email="test@example.com", drive_id="b!fake", drive_type="personal",
    sync_root="/tmp/onedriveui-test/OneDrive", added_at="2026-08-01T00:00:00Z",
)


# ─────────────────────────────────────────────────────────────────────────────
# Facts / snapshots for a given state — what the reducer would have produced
# ─────────────────────────────────────────────────────────────────────────────

def facts_for(state: SyncState, *, account_id: str = ACCOUNT.id, **overrides: Any) -> Facts:
    """A `Facts` consistent with `state`, so a UI test can render a state without
    knowing how the ladder reaches it."""
    base: dict[str, Any] = {
        "account_id": account_id,
        "sampled_at": utcnow_iso(),
        "account_configured": True,
        "daemon_rcd": DaemonHealth.UP,
        "daemon_mount": DaemonHealth.UP,
        "mount": MountHealth.UP,
        "token": TokenHealth.OK,
        "quota": QuotaInfo(total=1_104_880_336_896, used=252_544_077_005,
                           free=852_336_259_891, sampled_at=utcnow_iso()),
        "bisync": BisyncState.DISABLED,
    }
    per_state: dict[SyncState, dict[str, Any]] = {
        SyncState.NOT_RUNNING: {"daemon_rcd": DaemonHealth.DOWN,
                                "daemon_mount": DaemonHealth.DOWN,
                                "mount": MountHealth.DOWN,
                                "account_configured": False},
        SyncState.INITIALIZING: {"daemon_rcd": DaemonHealth.STARTING,
                                 "mount": MountHealth.STARTING,
                                 "startup_elapsed_s": 1.0},
        SyncState.SIGNED_OUT: {"account_configured": False,
                               "token": TokenHealth.UNKNOWN},
        SyncState.ACCOUNT_BLOCKED: {"token": TokenHealth.TENANT_BLOCKED,
                                    "issues_blocking": 1},
        SyncState.AUTH_REQUIRED: {"token": TokenHealth.EXPIRED,
                                  "issues_blocking": 1},
        SyncState.ERROR: {"issues_blocking": 2, "last_error": "quotaLimitReached"},
        SyncState.NEEDS_ATTENTION: {"pending_decisions": 1},
        SyncState.PAUSED_QUOTA: {
            "policy_pause": PauseReason.QUOTA, "issues_blocking": 1,
            "quota": QuotaInfo(total=1_000, used=1_000, free=0,
                               sampled_at=utcnow_iso()),
            "pause": PauseIntent(reason=PauseReason.QUOTA, set_at=utcnow_iso())},
        SyncState.PAUSED_MANUAL: {
            "pause": PauseIntent(reason=PauseReason.MANUAL, set_at=utcnow_iso())},
        SyncState.PAUSED_METERED: {
            "policy_pause": PauseReason.METERED, "network": "metered",
            "pause": PauseIntent(reason=PauseReason.METERED, set_at=utcnow_iso())},
        SyncState.PAUSED_BATTERY: {
            "policy_pause": PauseReason.BATTERY, "power": "saver",
            "pause": PauseIntent(reason=PauseReason.BATTERY, set_at=utcnow_iso())},
        SyncState.OFFLINE: {"network": "offline", "consecutive_net_failures": 3},
        SyncState.MOUNTING: {"mount": MountHealth.STARTING,
                             "daemon_mount": DaemonHealth.STARTING},
        SyncState.SYNCING: {"transfers_active": 2, "uploads_in_progress": 1,
                            "uploads_queued": 3},
        SyncState.PROCESSING: {"checks_active": 4, "scan_in_progress": True},
        SyncState.WARNING: {"issues_error": 3},
        SyncState.INFO_NOTICE: {"info_notice": "OneNote notebooks aren't synced"},
        SyncState.UP_TO_DATE: {},
    }
    base.update(per_state.get(state, {}))
    # `network` and `power` are enums; accept the plain strings used above.
    if isinstance(base.get("network"), str):
        base["network"] = NetworkState(base["network"])
    if isinstance(base.get("power"), str):
        base["power"] = PowerState(base["power"])
    base.update(overrides)
    return Facts(**base)


def _human(n: int) -> str:
    """Decimal (1000) byte sizes, the unit the OneDrive UI uses. The real one is
    `units.human_bytes()` (WP-01); this stub only needs a plausible string."""
    step = 1000.0
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


def _placeholders(state: SyncState, facts: Facts) -> dict[str, Any]:
    """The format arguments each STATUS_LINE / STATUS_SUB template expects. The
    same key means different things in different templates (`{total}` is a file
    count while syncing and a byte total when idle), so they are built per state.
    """
    quota = facts.quota
    if state is SyncState.SYNCING:
        done = facts.transfers_active
        total = done + facts.uploads_queued
        return {"n": facts.transferring_count, "done": done, "total": total,
                "bytes": _human(quota.used), "size": _human(quota.total)}
    if state in (SyncState.UP_TO_DATE, SyncState.INFO_NOTICE):
        return {"used": _human(quota.used), "total": _human(quota.total)}
    if state is SyncState.WARNING:
        return {"n": facts.issues_error}
    if state in (SyncState.ERROR, SyncState.NEEDS_ATTENTION):
        return {"n": facts.issues_blocking}
    if state is SyncState.PAUSED_MANUAL:
        return {"hh": 1, "mm": 30}
    return {"n": facts.transferring_count}


def snapshot_for(state: SyncState, *, facts: Facts | None = None,
                 account: AccountInfo = ACCOUNT, **fmt: Any) -> SyncSnapshot:
    """The `SyncSnapshot` the UI renders for `state` — headline, subtext and tray
    all sourced from `strings.py`, never from a literal."""
    facts = facts or facts_for(state, account_id=account.id)
    tray = TRAY_FOR_STATE[state]
    if tray is TrayIcon.SYNCED and account.kind.value == "business":
        tray = TrayIcon.SYNCED_BIZ
    numbers = _placeholders(state, facts)
    numbers.update(fmt)
    headline = status_line(state, **numbers)
    subtext = status_sub(state, **numbers)
    progress = -1
    if state is SyncState.SYNCING and facts.uploads_queued:
        done = facts.transfers_active
        progress = int(done / max(1, done + facts.uploads_queued) * 100)
    return SyncSnapshot(
        state=state, facts=facts, headline=headline, subtext=subtext,
        tooltip=f"{headline}\n{subtext}".strip(), tray=tray,
        progress_pct=progress, changed_at=utcnow_iso(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor
# ─────────────────────────────────────────────────────────────────────────────

class FakeSupervisor(QObject):
    """`sync/supervisor.Supervisor` without an engine.

    Every mutating call is recorded, never performed. `set_state()` drives the
    whole UI: it emits `BUS.facts_updated` and `BUS.state_changed`, exactly as
    the real tick loop does.
    """

    #: The real Supervisor's scheduled work, in seconds unless named _ms.
    SCHEDULE: dict[str, int] = {
        "tick_idle_ms": TICK_IDLE_MS,
        "tick_active_ms": TICK_ACTIVE_MS,
        "quota_s": QUOTA_TTL_S,
        "cache_scan_s": CACHE_SCAN_INTERVAL_S,
        "verify_s": VERIFY_INTERVAL_S,
        "token_keepalive_s": TOKEN_KEEPALIVE_S,
    }

    def __init__(self, account: AccountInfo = ACCOUNT,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self.running = False
        self._state = SyncState.UP_TO_DATE
        self._snapshot = snapshot_for(self._state, account=account)
        self.actions: list[tuple[RecoveryAction, dict[str, Any]]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: decision ids that have been ANSWERED — request_resync() needs one.
        self.answered_decisions: set[int] = set()
        self.orphan_bytes = 0

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        self.running = True
        self.calls.append(("start", {}))

    def stop(self) -> None:
        self.running = False
        self.calls.append(("stop", {}))

    # ── reads ───────────────────────────────────────────────────────────────
    def state(self) -> SyncState:
        return self._state

    def snapshot(self) -> SyncSnapshot:
        return self._snapshot

    # ── writes (recorded, never performed) ──────────────────────────────────
    def do(self, action: RecoveryAction, **kw: Any) -> None:
        """THE single entry point for every user action."""
        self.actions.append((action, dict(kw)))

    def request_pause(self, reason: PauseReason, hours: int | None) -> None:
        self.calls.append(("request_pause", {"reason": reason, "hours": hours}))
        state = {
            PauseReason.MANUAL: SyncState.PAUSED_MANUAL,
            PauseReason.METERED: SyncState.PAUSED_METERED,
            PauseReason.BATTERY: SyncState.PAUSED_BATTERY,
            PauseReason.QUOTA: SyncState.PAUSED_QUOTA,
        }.get(reason, SyncState.PAUSED_MANUAL)
        self.set_state(state)

    def request_resume(self) -> None:
        self.calls.append(("request_resume", {}))
        self.set_state(SyncState.SYNCING)

    def request_resync(self, *, decision_id: int) -> None:
        """Invariant I15: a resync without an answered decision is a caller bug."""
        if decision_id not in self.answered_decisions:
            raise SafetyRefusal("I15", f"resync without an answered decision "
                                       f"(decision_id={decision_id})")
        self.calls.append(("request_resync", {"decision_id": decision_id}))

    def restart_mount(self, reason: str) -> None:
        self.calls.append(("restart_mount", {"reason": reason}))
        self.set_state(SyncState.MOUNTING)

    def reset_client(self, *, keep_files: bool = True) -> None:
        self.calls.append(("reset_client", {"keep_files": keep_files}))

    def reclaim_orphaned_cache(self) -> int:
        self.calls.append(("reclaim_orphaned_cache", {}))
        freed, self.orphan_bytes = self.orphan_bytes, 0
        return freed

    # ── scripting ───────────────────────────────────────────────────────────
    def set_state(self, state: SyncState, *, facts: Facts | None = None,
                  **fmt: Any) -> SyncSnapshot:
        """Move to `state` and emit exactly what the tick loop would emit."""
        old, self._state = self._state, state
        snap = snapshot_for(state, facts=facts, account=self.account, **fmt)
        self._snapshot = snap
        BUS.facts_updated.emit(snap.facts)
        BUS.state_changed.emit(old, state, snap.facts)
        return snap

    def drive_all_states(self) -> Iterator[SyncSnapshot]:
        """Walk every SyncState in declaration order, emitting each one."""
        for state in SyncState:
            yield self.set_state(state)

    def emit_activity(self, rel_path: str, verb: ActivityVerb = ActivityVerb.UPLOADED,
                      *, state: ActivityState = ActivityState.DONE,
                      size: int = 1024, done: int | None = None) -> ActivityEvent:
        event = ActivityEvent(
            id=len(self.calls) + 1, account_id=self.account.id, rel_path=rel_path,
            name=rel_path.rsplit("/", 1)[-1], verb=verb, state=state,
            direction="up" if verb is ActivityVerb.UPLOADED else "down",
            bytes=size if done is None else done, size=size,
            started_at=utcnow_iso(),
            completed_at=utcnow_iso() if state is not ActivityState.INFLIGHT else None,
        )
        if state is ActivityState.INFLIGHT:
            BUS.activity_updated.emit(event)
        else:
            BUS.activity_appended.emit(event)
        return event

    def require_decision(self, kind: DecisionKind = DecisionKind.MASS_DELETE,
                         payload: dict[str, Any] | None = None) -> Decision:
        decision = Decision(id=len(self.answered_decisions) + 1,
                            account_id=self.account.id, kind=kind,
                            payload=dict(payload or {"count": 250}),
                            created_at=utcnow_iso())
        BUS.decision_required.emit(decision)
        return decision

    def answer_decision(self, decision_id: int, answer: str = "yes") -> None:
        self.answered_decisions.add(decision_id)
        BUS.decision_answered.emit(decision_id, answer)


# ─────────────────────────────────────────────────────────────────────────────
# Pinner
# ─────────────────────────────────────────────────────────────────────────────

class FakePinner(QObject):
    """`sync/pinner.Pinner` — records intent and emits believable progress."""

    progress = Signal(str, int, int)          # rel_path, done, total

    MAX_CONCURRENT_PINS: int = MAX_CONCURRENT_PINS

    def __init__(self, account: AccountInfo = ACCOUNT,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self.pinned: set[str] = set()
        self.online_only: set[str] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sizes: dict[str, tuple[int, int]] = {}
        self._active = 0

    def pin(self, rel_path: str, *, recursive: bool = False) -> None:
        self.calls.append(("pin", {"rel_path": rel_path, "recursive": recursive}))
        self.pinned.add(rel_path)
        self.online_only.discard(rel_path)
        self._emit_state(rel_path, FileState.PINNED, pinned=True)

    def unpin(self, rel_path: str, *, recursive: bool = False) -> None:
        self.calls.append(("unpin", {"rel_path": rel_path, "recursive": recursive}))
        self.pinned.discard(rel_path)
        self._emit_state(rel_path, FileState.LOCAL, pinned=False)

    def free_up_space(self, rel_path: str) -> int:
        self.calls.append(("free_up_space", {"rel_path": rel_path}))
        self.pinned.discard(rel_path)
        self.online_only.add(rel_path)
        local, _total = self.sizing(rel_path)
        self._emit_state(rel_path, FileState.ONLINE_ONLY, pinned=False)
        return local

    def free_up_all(self) -> int:
        self.calls.append(("free_up_all", {}))
        freed = sum(local for local, _t in self.sizes.values())
        self.online_only.update(self.sizes)
        self.pinned.clear()
        return freed

    def download_all(self) -> None:
        self.calls.append(("download_all", {}))
        for rel_path in list(self.sizes):
            self.pin(rel_path)

    def cancel(self, rel_path: str) -> None:
        self.calls.append(("cancel", {"rel_path": rel_path}))
        self._active = max(0, self._active - 1)

    def active(self) -> int:
        return self._active

    def sizing(self, rel_path: str) -> tuple[int, int]:
        """-> (bytes local, bytes total)."""
        return self.sizes.get(rel_path, (0, 0))

    # ── scripting ───────────────────────────────────────────────────────────
    def set_size(self, rel_path: str, local: int, total: int) -> None:
        self.sizes[rel_path] = (local, total)

    def emit_progress(self, rel_path: str, done: int, total: int) -> None:
        self._active = 1 if done < total else 0
        self.progress.emit(rel_path, done, total)
        BUS.pin_progress.emit(rel_path, done, total)

    def _emit_state(self, rel_path: str, state: FileState, *, pinned: bool) -> None:
        local, total = self.sizing(rel_path)
        status = FileStatus(rel_path=rel_path, state=state, size=total,
                            bytes_local=local if state is not FileState.ONLINE_ONLY else 0,
                            pinned=pinned)
        BUS.file_state_changed.emit(self.account.id, rel_path, status)


# ─────────────────────────────────────────────────────────────────────────────
# IssueEngine
# ─────────────────────────────────────────────────────────────────────────────

class FakeIssueEngine(QObject):
    """`sync/issues.IssueEngine` over an in-memory list of `SyncIssue` rows."""

    def __init__(self, account: AccountInfo = ACCOUNT,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.account = account
        self.issues: dict[int, SyncIssue] = {}
        self.executed: list[tuple[RecoveryAction, int, dict[str, Any]]] = []
        self.reconciled = 0
        self._next_id = 1

    # ── ingest ──────────────────────────────────────────────────────────────
    def ingest_transfer_error(self, ev: ActivityEvent) -> int | None:
        if not ev.error:
            return None
        return self.raise_issue(ev.error_kind or IssueCode.UNKNOWN,
                                rel_path=ev.rel_path, raw_error=ev.error).id

    def ingest_log_record(self, rec: dict[str, Any], run_id: str) -> int | None:
        message = str(rec.get("msg") or rec.get("error") or "")
        if not message:
            return None
        code, severity, _actions = classify(message)
        return self.raise_issue(code, severity=severity,
                                rel_path=rec.get("object"), raw_error=message).id

    def ingest_health(self, facts: Facts) -> None:
        if facts.mount is MountHealth.STALE:
            self.raise_issue(IssueCode.MOUNT_DEAD)
        if facts.out_of_space:
            self.raise_issue(IssueCode.DISK_FULL)
        if facts.token is TokenHealth.EXPIRED:
            self.raise_issue(IssueCode.AUTH_EXPIRED)

    def ingest_preflight(self, violations: list[Any]) -> None:
        for violation in violations:
            self.raise_issue(getattr(violation, "code", IssueCode.UNKNOWN),
                             rel_path=getattr(violation, "rel_path", None),
                             detail=getattr(violation, "detail", ""))

    # ── lifecycle ───────────────────────────────────────────────────────────
    def reconcile(self, facts: Facts) -> int:
        """Auto-resolve everything whose condition has cleared."""
        closed = 0
        for issue_id, issue in list(self.issues.items()):
            clear = (
                (issue.code is IssueCode.MOUNT_DEAD and facts.mount is MountHealth.UP)
                or (issue.code is IssueCode.DISK_FULL and not facts.out_of_space)
                or (issue.code is IssueCode.AUTH_EXPIRED and facts.token is TokenHealth.OK)
                or (issue.code is IssueCode.QUOTA_EXCEEDED and not facts.quota.is_full)
            )
            if clear:
                self.resolve(issue_id, resolution="auto")
                closed += 1
        self.reconciled += closed
        return closed

    def execute(self, action: RecoveryAction, issue: SyncIssue, **kw: Any) -> bool:
        self.executed.append((action, issue.id, dict(kw)))
        if action in (RecoveryAction.SKIP, RecoveryAction.RETRY,
                      RecoveryAction.RENAME, RecoveryAction.KEEP_BOTH,
                      RecoveryAction.KEEP_LOCAL, RecoveryAction.KEEP_CLOUD):
            self.resolve(issue.id, resolution=action.value)
        return True

    def mute(self, issue_id: int) -> None:
        issue = self.issues.get(issue_id)
        if issue is not None:
            self.issues[issue_id] = SyncIssue(**{**_asdict(issue), "muted": True})

    def counts(self, account_id: str) -> tuple[int, int, int]:
        """-> (blocking, error, warning) among OPEN issues."""
        rows = [i for i in self.issues.values()
                if i.account_id == account_id and i.resolved_at is None and not i.muted]
        return (
            sum(1 for i in rows if i.severity is IssueSeverity.BLOCKING),
            sum(1 for i in rows if i.severity is IssueSeverity.ERROR),
            sum(1 for i in rows if i.severity is IssueSeverity.WARNING),
        )

    # ── scripting ───────────────────────────────────────────────────────────
    def raise_issue(self, code: IssueCode, *, rel_path: str | None = None,
                    severity: IssueSeverity | None = None, detail: str = "",
                    raw_error: str = "", **fmt: Any) -> SyncIssue:
        """Open one issue, worded from `strings.ISSUE_TITLE` and actioned from
        `errors.ACTIONS_FOR` — never from a literal."""
        issue = SyncIssue(
            id=self._next_id, account_id=self.account.id, code=code,
            severity=severity or _default_severity(code),
            rel_path=rel_path,
            title=_t(ISSUE_TITLE[code], **{"n": 1, "size": "1.2 GB", **fmt}),
            detail=detail, raw_error=raw_error,
            actions=ACTIONS_FOR[code],
            first_seen_at=utcnow_iso(), last_seen_at=utcnow_iso(),
        )
        self.issues[issue.id] = issue
        self._next_id += 1
        BUS.issue_raised.emit(issue)
        return issue

    def raise_every_issue(self) -> list[SyncIssue]:
        """One open issue per IssueCode — the "View sync problems" torture test."""
        return [self.raise_issue(code) for code in IssueCode]

    def resolve(self, issue_id: int, *, resolution: str = "auto") -> None:
        issue = self.issues.get(issue_id)
        if issue is None:
            return
        self.issues[issue_id] = SyncIssue(**{**_asdict(issue),
                                             "resolved_at": utcnow_iso(),
                                             "resolution": resolution})
        BUS.issue_resolved.emit(issue_id)

    def open_issues(self) -> list[SyncIssue]:
        return [i for i in self.issues.values() if i.resolved_at is None]


def _default_severity(code: IssueCode) -> IssueSeverity:
    """The severity ARCHITECTURE §12.2 gives each code."""
    blocking = {
        IssueCode.QUOTA_EXCEEDED, IssueCode.DISK_FULL, IssueCode.AUTH_EXPIRED,
        IssueCode.AUTH_MFA, IssueCode.AUTH_TENANT_BLOCKED,
        IssueCode.MASS_DELETE_BLOCKED, IssueCode.ALL_FILES_CHANGED,
        IssueCode.CHECK_ACCESS_FAILED, IssueCode.NEEDS_RESYNC,
        IssueCode.BISYNC_LOCK_STUCK, IssueCode.BISYNC_CRITICAL,
        IssueCode.MOUNT_DEAD,
    }
    warning = {
        IssueCode.THROTTLED, IssueCode.NETWORK_UNREACHABLE, IssueCode.FILE_IN_USE,
        IssueCode.CONFLICT, IssueCode.PARTIAL_FILE_FOUND,
    }
    info = {IssueCode.ORPHANED_CACHE, IssueCode.ONENOTE_HIDDEN,
            IssueCode.VAULT_INACCESSIBLE}
    if code in blocking:
        return IssueSeverity.BLOCKING
    if code in warning:
        return IssueSeverity.WARNING
    if code in info:
        return IssueSeverity.INFO
    return IssueSeverity.ERROR


def _asdict(issue: SyncIssue) -> dict[str, Any]:
    """`dataclasses.asdict` deep-copies; these rows hold only immutables."""
    return {f: getattr(issue, f) for f in SyncIssue.__slots__}


# ─────────────────────────────────────────────────────────────────────────────
# QuotaService
# ─────────────────────────────────────────────────────────────────────────────

class FakeQuotaService(QObject):
    """`sync/quota.QuotaService` with a settable answer and a refresh counter."""

    def __init__(self, quota: QuotaInfo | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._quota = quota or QuotaInfo(
            total=1_104_880_336_896, used=252_544_077_005, free=852_336_259_891,
            trashed=0, sampled_at=utcnow_iso())
        self.refreshes = 0

    def current(self) -> QuotaInfo:
        return self._quota

    def refresh(self, *, force: bool = False) -> QuotaInfo:
        self.refreshes += 1
        BUS.quota_updated.emit(self._quota)
        return self._quota

    def pct(self) -> float:
        return self._quota.pct

    def tier(self) -> str:
        return self._quota.tier

    def is_full(self) -> bool:
        return self._quota.is_full

    def is_frozen(self) -> bool:
        return self._quota.frozen

    # ── scripting ───────────────────────────────────────────────────────────
    def set_quota(self, *, total: int | None = None, used: int | None = None,
                  trashed: int | None = None, frozen: bool | None = None) -> QuotaInfo:
        total = self._quota.total if total is None else total
        used = self._quota.used if used is None else used
        self._quota = QuotaInfo(
            total=total, used=used, free=max(0, total - used),
            trashed=self._quota.trashed if trashed is None else trashed,
            sampled_at=utcnow_iso(),
            frozen=self._quota.frozen if frozen is None else frozen)
        BUS.quota_updated.emit(self._quota)
        return self._quota

    def set_tier(self, tier: str) -> QuotaInfo:
        """Jump straight to an 'ok' / 'warn' / 'critical' / 'full' bar."""
        total = 1_000_000_000_000
        used = {"ok": 0.10, "warn": 0.85, "critical": 0.95, "full": 1.0}[tier]
        return self.set_quota(total=total, used=int(total * used))


# ─────────────────────────────────────────────────────────────────────────────
# PauseManager
# ─────────────────────────────────────────────────────────────────────────────

class FakePauseManager(QObject):
    """`sync/pause.PauseManager`. `enforce()` counts the queue items it would
    have deferred, which is exactly how pause works against a FUSE mount."""

    #: (hours, label) — the tray submenu, worded in strings.MENU.
    PAUSE_DURATIONS: tuple[tuple[int | None, str], ...] = (
        (2, "2 hours"), (8, "8 hours"), (24, "24 hours"), (None, "Until I resume"),
    )

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._reason = PauseReason.NONE
        self._until: _dt.datetime | None = None
        self.overridden: set[PauseReason] = set()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.deferred = 0
        self.queue_size = 0

    def pause(self, reason: PauseReason, hours: int | None = None) -> None:
        self.calls.append(("pause", {"reason": reason, "hours": hours}))
        self._reason = reason
        self._until = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=hours)
                       if hours else None)
        BUS.pause_changed.emit(reason, self._until)

    def resume(self, reason: PauseReason | None = None) -> None:
        self.calls.append(("resume", {"reason": reason}))
        self._reason = PauseReason.NONE
        self._until = None
        BUS.pause_changed.emit(PauseReason.NONE, None)

    def sync_anyway(self, reason: PauseReason) -> None:
        """The "Sync Anyway" toast button: override one policy pause only."""
        self.calls.append(("sync_anyway", {"reason": reason}))
        self.overridden.add(reason)
        if self._reason is reason:
            self.resume(reason)

    def active(self) -> PauseReason:
        return self._reason

    def until(self) -> _dt.datetime | None:
        return self._until

    def enforce(self, ep: Any = None) -> int:
        """Push every queued upload past the pause deadline; returns the count."""
        self.calls.append(("enforce", {}))
        if self._reason is PauseReason.NONE:
            return 0
        self.deferred += self.queue_size
        return self.queue_size

    def policy_pause(self, *, metered: bool = False, battery: bool = False,
                     quota_full: bool = False) -> PauseReason:
        if quota_full:
            return PauseReason.QUOTA
        if metered and PauseReason.METERED not in self.overridden:
            return PauseReason.METERED
        if battery and PauseReason.BATTERY not in self.overridden:
            return PauseReason.BATTERY
        return PauseReason.NONE


#: ARCHITECTURE §8.4 calls it PauseManager; some call sites say PauseController.
FakePauseController = FakePauseManager


# ─────────────────────────────────────────────────────────────────────────────
# Notifier
# ─────────────────────────────────────────────────────────────────────────────

class FakeNotifier(QObject):
    """`platform/notify.Notifier` without D-Bus. Records every NotifySpec and
    can replay an action button press."""

    action_invoked = Signal(str, str)         # NotificationId value, action id

    MAX_ACTIONS: int = 2

    #: Verified on the target machine — no body-images, no action-icons.
    CAPABILITIES = frozenset({"actions", "body", "body-markup", "icon-static",
                              "persistence", "sound"})

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.sent: list[NotifySpec] = []
        self.closed: list[int] = []
        self.disabled: set[NotificationId] = set()
        self._next_id = 1

    def notify(self, spec: NotifySpec) -> int:
        assert len(spec.actions) <= self.MAX_ACTIONS, (
            f"{spec.id} has {len(spec.actions)} actions; GNOME renders {self.MAX_ACTIONS}")
        if not self.is_enabled(spec.id):
            return 0
        self.sent.append(spec)
        nid, self._next_id = self._next_id, self._next_id + 1
        return nid

    def close(self, nid: int) -> None:
        self.closed.append(nid)

    def capabilities(self) -> frozenset[str]:
        return self.CAPABILITIES

    def is_enabled(self, nid: NotificationId) -> bool:
        return nid not in self.disabled

    # ── scripting ───────────────────────────────────────────────────────────
    def toast(self, nid: NotificationId, **fmt: Any) -> NotifySpec:
        """Build and send the catalogued toast for `nid`, formatted."""
        summary, body, actions = _toast(nid, **{"n": 3, "pct": 92, "name": "Report.docx",
                                                "loser": "Report-thinkpad.docx",
                                                "who": "Alex", "size": "1.2 GB",
                                                "folders": "Desktop and Documents",
                                                "year": 2019, **fmt})
        spec = NotifySpec(id=nid, summary=summary, body=body, actions=actions,
                          urgency=2 if nid in (NotificationId.QUOTA_FULL,
                                               NotificationId.ACCOUNT_BLOCKED) else 1,
                          account_id=ACCOUNT.id)
        self.notify(spec)
        BUS.toast_requested.emit(spec)
        return spec

    def fire_every_toast(self) -> list[NotifySpec]:
        """One NotifySpec per NotificationId, so a toast surface can be swept."""
        return [self.toast(nid) for nid in TOAST]

    def invoke(self, nid: NotificationId, action_id: str) -> None:
        self.action_invoked.emit(nid.value, action_id)
        BUS.notification_action.emit(nid.value, action_id)


# ─────────────────────────────────────────────────────────────────────────────
# The bundle
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class FakeServices:
    """Everything a UI test needs, wired to one account and one BUS."""
    account: AccountInfo = ACCOUNT
    supervisor: FakeSupervisor = field(default=None)      # type: ignore[assignment]
    pinner: FakePinner = field(default=None)              # type: ignore[assignment]
    issues: FakeIssueEngine = field(default=None)         # type: ignore[assignment]
    quota: FakeQuotaService = field(default=None)         # type: ignore[assignment]
    pause: FakePauseManager = field(default=None)         # type: ignore[assignment]
    notifier: FakeNotifier = field(default=None)          # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.supervisor = self.supervisor or FakeSupervisor(self.account)
        self.pinner = self.pinner or FakePinner(self.account)
        self.issues = self.issues or FakeIssueEngine(self.account)
        self.quota = self.quota or FakeQuotaService()
        self.pause = self.pause or FakePauseManager()
        self.notifier = self.notifier or FakeNotifier()

    # ── one-line drivers ────────────────────────────────────────────────────
    def drive_state(self, state: SyncState, **fmt: Any) -> SyncSnapshot:
        return self.supervisor.set_state(state, **fmt)

    def drive_all_states(self) -> list[SyncSnapshot]:
        return list(self.supervisor.drive_all_states())

    def raise_every_issue(self) -> list[SyncIssue]:
        return self.issues.raise_every_issue()

    def fire_every_toast(self) -> list[NotifySpec]:
        return self.notifier.fire_every_toast()

    def seed_activity(self, count: int = 5) -> list[ActivityEvent]:
        verbs = list(ActivityVerb)
        return [self.supervisor.emit_activity(f"Documents/file-{i}.docx",
                                              verbs[i % len(verbs)], size=1024 * (i + 1))
                for i in range(count)]

    def reset(self) -> None:
        """Forget every recorded call, keeping the wiring."""
        self.supervisor.actions.clear()
        self.supervisor.calls.clear()
        self.pinner.calls.clear()
        self.issues.issues.clear()
        self.issues.executed.clear()
        self.pause.calls.clear()
        self.notifier.sent.clear()


def all_services(account: AccountInfo = ACCOUNT) -> FakeServices:
    """Convenience constructor mirroring the `fake_services` fixture."""
    return FakeServices(account=account)
