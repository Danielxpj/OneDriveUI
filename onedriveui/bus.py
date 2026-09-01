"""FROZEN CONTRACT. The one application-wide event bus.

Rules:
  * Every cross-module signal is declared HERE and nowhere else.
  * Nobody subclasses EventBus. Modules emit and connect.
  * Payloads are frozen dataclasses or primitives — never a mutable dict,
    never a QWidget.
  * BUS is created before QApplication and lives on the GUI thread, so a signal
    emitted from a worker is delivered with Qt.QueuedConnection automatically.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from onedriveui.models import (
    AccountInfo, ActivityEvent, ConflictInfo, DaemonHealth, Decision, Facts,
    FileStatus, MountHealth, PauseReason, QuotaInfo, RunRecord, SyncIssue,
    SyncState, VaultState,
)


class EventBus(QObject):
    # ── state ────────────────────────────────────────────────────────────────
    facts_updated           = Signal(Facts)                  # sync/facts.py
    state_changed           = Signal(SyncState, SyncState, Facts)  # old, new, facts

    # ── transfers and activity ───────────────────────────────────────────────
    transfers_updated       = Signal(list)                   # list[TransferInfo]
    activity_appended       = Signal(ActivityEvent)
    activity_updated        = Signal(ActivityEvent)

    # ── quota ────────────────────────────────────────────────────────────────
    quota_updated           = Signal(QuotaInfo)

    # ── issues, conflicts, decisions ─────────────────────────────────────────
    issue_raised            = Signal(SyncIssue)
    issue_resolved          = Signal(int)                    # issue id
    conflict_detected       = Signal(ConflictInfo)
    decision_required       = Signal(Decision)
    decision_answered       = Signal(int, str)               # decision id, answer

    # ── per-file state ───────────────────────────────────────────────────────
    file_state_changed      = Signal(str, str, FileStatus)   # account_id, rel_path, status
    file_states_invalidated = Signal(str, list)              # account_id, list[str]
    pin_progress            = Signal(str, int, int)          # rel_path, done, total

    # ── runs ─────────────────────────────────────────────────────────────────

    # ── processes ────────────────────────────────────────────────────────────
    daemon_health           = Signal(str, DaemonHealth)      # "rcd" | "mount", health
    daemon_restarted        = Signal(str, str)               # kind, new execute_id
    mount_health            = Signal(str, MountHealth)       # account_id, health

    # ── accounts and auth ────────────────────────────────────────────────────
    account_added           = Signal(AccountInfo)
    account_updated         = Signal(AccountInfo)
    account_removed         = Signal(str)                    # account_id
    auth_url_ready          = Signal(str)                    # the 127.0.0.1:53682 authUrl
    auth_finished           = Signal(bool, str)              # ok, message

    # ── controls ─────────────────────────────────────────────────────────────
    pause_changed           = Signal(PauseReason, object)    # reason, datetime|None
    bandwidth_changed       = Signal(object)                 # BandwidthState
    config_changed          = Signal(str)                    # dotted key, e.g. "mount.transfers"
    theme_changed           = Signal(bool, str)              # dark, accent hex

    # ── notifications and IPC ────────────────────────────────────────────────
    toast_requested         = Signal(object)                 # NotifySpec
    notification_action     = Signal(str, str)               # toast key, action id
    ipc_action_requested    = Signal(str, list)              # verb, list[abs path]

    # ── misc ─────────────────────────────────────────────────────────────────
    log_line                = Signal(str)


#: The singleton. Import this, never construct another EventBus.
BUS = EventBus()

#: Every signal name declared above, frozen. `ui/`, `sync/` and `rc/` tests
#: assert against this rather than re-listing the catalogue, so a signal that is
#: added without updating ARCHITECTURE.md §11 fails a test instead of silently
#: existing.
SIGNAL_NAMES: tuple[str, ...] = (
    "facts_updated", "state_changed",
    "transfers_updated", "activity_appended", "activity_updated",
    "quota_updated",
    "issue_raised", "issue_resolved", "conflict_detected",
    "decision_required", "decision_answered",
    "file_state_changed", "file_states_invalidated", "pin_progress",
    "daemon_health", "daemon_restarted", "mount_health",
    "account_added", "account_updated", "account_removed",
    "auth_url_ready", "auth_finished",
    "pause_changed", "bandwidth_changed", "config_changed", "theme_changed",
    "toast_requested", "notification_action", "ipc_action_requested",
    "log_line",
)

__all__ = ["BUS", "EventBus", "SIGNAL_NAMES"]
