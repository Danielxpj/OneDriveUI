"""FROZEN CONTRACT. Exception hierarchy plus the ONE place a new error string is
ever taught.

Every error source funnels through classify(): core/transferred[].error,
core/stats.lastError, the rc error envelope, bisync log records, preflight
violations, and health facts.
"""

from __future__ import annotations

import re
from typing import Any

from onedriveui.models import IssueCode, IssueSeverity, RecoveryAction


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class OneDriveUIError(Exception):
    """Base for everything this application raises."""


class RcError(OneDriveUIError):
    """An rc call failed. rclone's error body is a stable 4-key object echoing the
    input: {"error", "input", "path", "status"}; `status` always mirrors HTTP."""

    def __init__(self, path: str, status: int, body: dict[str, Any] | None = None) -> None:
        self.path = path
        self.status = status
        self.body = body or {}
        self.message = str(self.body.get("error", "")) or f"HTTP {status}"
        super().__init__(f"{path}: {self.message}")

    @property
    def is_not_found(self) -> bool:
        return self.status == 404 or "not found" in self.message.lower()

    @property
    def is_job_expired(self) -> bool:
        """Ambiguous on its own — disambiguate against execute_id."""
        return self.status == 500 and "job not found" in self.message.lower()


class DaemonUnavailable(RcError):
    """The daemon did not answer rc/noop within the timeout."""


class DaemonForeign(OneDriveUIError):
    """A live daemon on our port failed the /proc ownership proof. NEVER drive it,
    never core/quit it — rc access is equivalent to shell access as this user."""


class MountLost(OneDriveUIError):
    """The mountpoint is gone or stale (ENOTCONN)."""


class BisyncCritical(OneDriveUIError):
    def __init__(self, verdict: str, log_tail: str = "") -> None:
        self.verdict = verdict
        self.log_tail = log_tail
        super().__init__(f"bisync critical: {verdict}")


class SafetyRefusal(OneDriveUIError):
    """Raised by rc/guards.py. This is ALWAYS a bug in the caller, never a
    user-facing error to be clicked past. It is never caught and swallowed."""

    def __init__(self, invariant: str, detail: str) -> None:
        self.invariant = invariant
        super().__init__(f"[{invariant}] {detail}")


class ConfigError(OneDriveUIError):
    """config.json failed validation in a way that cannot be defaulted around."""


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────

#: Auth failures have no dedicated rclone command; we probe with `about` and
#: match its error text. AADSTS65005 is NOT fixable by re-auth (an admin must
#: claim the domain), which is why it maps to a different code from AADSTS50076.
AUTH_PATTERNS: tuple[tuple[str, IssueCode], ...] = (
    ("aadsts65005",                IssueCode.AUTH_TENANT_BLOCKED),
    ("aadsts50076",                IssueCode.AUTH_MFA),
    ("empty token found",          IssueCode.AUTH_EXPIRED),
    ("invalid_grant",              IssueCode.AUTH_EXPIRED),
    ("couldn't fetch token",       IssueCode.AUTH_EXPIRED),
    ("token has expired",          IssueCode.AUTH_EXPIRED),
    ("failed to get token",        IssueCode.AUTH_EXPIRED),
)

#: Lines that look alarming and are not. NEVER surfaced to the user.
BENIGN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Ignoring --track-renames as it doesn't work with copy or move"),
    re.compile(r"WARNING\s+listing try \d+ failed"),
    re.compile(r"Skipped copy as --dry-run is set"),
    re.compile(r"vfs cache: detected external removal of cache file"),   # that is us, evicting
    re.compile(r"Can't follow symlink without -L/--copy-links"),
    re.compile(r"lock file renewed for"),
)

_RULES: tuple[tuple[re.Pattern[str], IssueCode, IssueSeverity], ...] = (
    (re.compile(r"quotaLimitReached|insufficient storage"
                r"|(?:\b(?:HTTP|status|code|error)\b\W{0,12})507\b"
                r"|\b507\s+Insufficient", re.I),
     IssueCode.QUOTA_EXCEEDED,      IssueSeverity.BLOCKING),
    (re.compile(r"no space left on device|ENOSPC", re.I),
     IssueCode.DISK_FULL,           IssueSeverity.BLOCKING),
    (re.compile(r"pathIsTooLong|path.{0,10}too long", re.I),
     IssueCode.PATH_TOO_LONG,       IssueSeverity.ERROR),
    (re.compile(r"nameContainsInvalidCharacters|invalid characters", re.I),
     IssueCode.NAME_INVALID,        IssueSeverity.ERROR),
    (re.compile(r"malware|virus.{0,12}detected", re.I),
     IssueCode.MALWARE_DETECTED,    IssueSeverity.ERROR),
    (re.compile(r"are same name when lowercase|nameAlreadyExists", re.I),
     IssueCode.CASE_COLLISION,      IssueSeverity.ERROR),
    (re.compile(r"accessDenied|permission denied|unauthorized"
                r"|(?:\b(?:HTTP|status|code|error)\b\W{0,12})403\b"
                r"|\b403\s+Forbidden", re.I),
     IssueCode.PERMISSION_LOST,     IssueSeverity.ERROR),
    (re.compile(r"activityLimitReached|too many requests|Retry-After"
                r"|(?:\b(?:HTTP|status|code|error)\b\W{0,12})429\b"
                r"|\b429\s+Too\s+Many", re.I),
     IssueCode.THROTTLED,           IssueSeverity.WARNING),
    (re.compile(r"connection refused|connection reset|no such host|network is unreachable"
                r"|i/o timeout|EOF|dial tcp", re.I),
     IssueCode.NETWORK_UNREACHABLE, IssueSeverity.WARNING),
    (re.compile(r"text file busy|resource busy|ETXTBSY|EBUSY", re.I),
     IssueCode.FILE_IN_USE,         IssueSeverity.WARNING),
    (re.compile(r"Safety abort: too many deletes", re.I),
     IssueCode.MASS_DELETE_BLOCKED, IssueSeverity.BLOCKING),
    (re.compile(r"Safety abort: all files were changed", re.I),
     IssueCode.ALL_FILES_CHANGED,   IssueSeverity.BLOCKING),
    (re.compile(r"Access test failed", re.I),
     IssueCode.CHECK_ACCESS_FAILED, IssueSeverity.BLOCKING),
    (re.compile(r"Must run --resync to recover|cannot find prior Path1 or Path2 listings"
                r"|filters file has changed", re.I),
     IssueCode.NEEDS_RESYNC,        IssueSeverity.BLOCKING),
    (re.compile(r"prior lock file found", re.I),
     IssueCode.BISYNC_LOCK_STUCK,   IssueSeverity.BLOCKING),
    (re.compile(r"Transport endpoint is not connected|ENOTCONN", re.I),
     IssueCode.MOUNT_DEAD,          IssueSeverity.BLOCKING),
    # ── the rows of ARCHITECTURE §12.2 whose recogniser is a preflight verdict,
    #    a scan finding or a bisync verdict rather than a Graph error string.
    #    They are matched AFTER the rules above so that a bisync abort, a name
    #    with invalid characters or a too-long path always wins over the more
    #    general phrasings below.
    # The Windows reserved names must be matched as a whole PATH COMPONENT,
    # never as a bare word. `\bAUX\b` with re.I matched the English and French
    # word "aux" anywhere in a sentence, and `\bCON\b` matched "con" — so any
    # error mentioning a directory legitimately named `aux` on Linux (where the
    # name is perfectly legal) was reported to the user as a reserved-name
    # violation, complete with a Rename button. Requiring a separator or a quote
    # on the left keeps `/Docs/AUX.txt` and `"CON"` while dropping prose — the
    # left anchor is deliberately NOT `^`, because with re.M that matched any
    # message merely beginning with the word "aux", which is an ordinary French
    # word and an ordinary directory name.
    (re.compile(r"reserved name|is reserved|invalidReservedName|_vti_"
                r"|desktop\.ini|\.lock\b|~\$"
                r"""|[/\\'"](?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])"""
                r"""(?:\.[A-Za-z0-9]+)?(?=[/\\'"\s,:;)\]]|$)""", re.I | re.M),
     IssueCode.RESERVED_NAME,       IssueSeverity.ERROR),
    (re.compile(r"entityTooLarge|requestEntityTooLarge|file (?:is )?too large"
                r"|larger than (?:the )?max|exceeds the maximum(?: file)? size"
                r"|payload too large", re.I),
     IssueCode.FILE_TOO_LARGE,      IssueSeverity.ERROR),
    (re.compile(r"critical error|\bCRITICAL_[A-Z]+\b", re.I),
     IssueCode.BISYNC_CRITICAL,     IssueSeverity.BLOCKING),
    (re.compile(r"orphan(?:ed)? cache|stale vfs cache tree", re.I),
     IssueCode.ORPHANED_CACHE,      IssueSeverity.INFO),
    (re.compile(r"\.partial\b|partial file|partially transferred", re.I),
     IssueCode.PARTIAL_FILE_FOUND,  IssueSeverity.WARNING),
    (re.compile(r"\.onetoc2\b|\.one\b|OneNote", re.I),
     IssueCode.ONENOTE_HIDDEN,      IssueSeverity.INFO),
    (re.compile(r"Personal Vault|personalVault|vault is locked", re.I),
     IssueCode.VAULT_INACCESSIBLE,  IssueSeverity.INFO),
    (re.compile(r"\bconflict(?:s|ed|ing)?\b", re.I),
     IssueCode.CONFLICT,            IssueSeverity.WARNING),
)

ACTIONS_FOR: dict[IssueCode, tuple[RecoveryAction, ...]] = {
    IssueCode.NAME_INVALID:        (RecoveryAction.RENAME, RecoveryAction.SKIP),
    IssueCode.RESERVED_NAME:       (RecoveryAction.RENAME, RecoveryAction.SKIP),
    IssueCode.PATH_TOO_LONG:       (RecoveryAction.RENAME, RecoveryAction.SHOW_IN_FOLDER),
    IssueCode.FILE_TOO_LARGE:      (RecoveryAction.SKIP, RecoveryAction.SHOW_IN_FOLDER),
    IssueCode.QUOTA_EXCEEDED:      (RecoveryAction.GET_MORE_STORAGE, RecoveryAction.FREE_UP_SPACE),
    IssueCode.DISK_FULL:           (RecoveryAction.FREE_UP_SPACE,),
    IssueCode.AUTH_EXPIRED:        (RecoveryAction.SIGN_IN,),
    IssueCode.AUTH_MFA:            (RecoveryAction.SIGN_IN,),
    IssueCode.AUTH_TENANT_BLOCKED: (RecoveryAction.OPEN_WEB,),
    IssueCode.THROTTLED:           (),
    IssueCode.NETWORK_UNREACHABLE: (RecoveryAction.RETRY,),
    IssueCode.MALWARE_DETECTED:    (RecoveryAction.OPEN_WEB, RecoveryAction.SKIP),
    IssueCode.FILE_IN_USE:         (RecoveryAction.RETRY,),
    IssueCode.PERMISSION_LOST:     (RecoveryAction.STOP_SYNCING_ITEM, RecoveryAction.OPEN_WEB),
    IssueCode.CONFLICT:            (RecoveryAction.KEEP_BOTH, RecoveryAction.KEEP_LOCAL,
                                    RecoveryAction.KEEP_CLOUD),
    IssueCode.CASE_COLLISION:      (RecoveryAction.RENAME,),
    IssueCode.MASS_DELETE_BLOCKED: (RecoveryAction.FORCE_DELETE, RecoveryAction.RESTORE_FROM_BACKUP),
    IssueCode.ALL_FILES_CHANGED:   (RecoveryAction.FORCE_DELETE, RecoveryAction.SKIP),
    IssueCode.CHECK_ACCESS_FAILED: (RecoveryAction.RESYNC, RecoveryAction.SKIP),
    IssueCode.NEEDS_RESYNC:        (RecoveryAction.RESYNC,),
    IssueCode.BISYNC_LOCK_STUCK:   (RecoveryAction.UNLOCK_BISYNC,),
    IssueCode.BISYNC_CRITICAL:     (RecoveryAction.RESYNC,),
    IssueCode.MOUNT_DEAD:          (RecoveryAction.RESTART_MOUNT,),
    IssueCode.ORPHANED_CACHE:      (RecoveryAction.RECLAIM_CACHE,),
    IssueCode.PARTIAL_FILE_FOUND:  (RecoveryAction.SKIP, RecoveryAction.RETRY),
    IssueCode.ONENOTE_HIDDEN:      (RecoveryAction.OPEN_WEB,),
    IssueCode.VAULT_INACCESSIBLE:  (RecoveryAction.OPEN_WEB,),
    IssueCode.UPLOAD_FAILED:       (RecoveryAction.RETRY, RecoveryAction.SKIP),
    IssueCode.DOWNLOAD_FAILED:     (RecoveryAction.RETRY, RecoveryAction.SKIP),
    IssueCode.UNKNOWN:             (RecoveryAction.RETRY, RecoveryAction.SKIP),
}


def is_benign(text: str) -> bool:
    """True for rclone lines that look like errors and are not."""
    return any(p.search(text) for p in BENIGN_PATTERNS)


def classify(
    raw: str,
    status: int | None = None,
    rel_path: str | None = None,
    direction: str = "",
) -> tuple[IssueCode, IssueSeverity, tuple[RecoveryAction, ...]]:
    """Map raw rclone / Graph error text (plus an optional HTTP status) onto an
    IssueCode, a severity and the fix actions to offer.

    Never raises. Unmatched text becomes UPLOAD_FAILED / DOWNLOAD_FAILED when a
    direction is known, else UNKNOWN.
    """
    text = raw or ""
    low = text.lower()

    for needle, code in AUTH_PATTERNS:
        if needle in low:
            return code, IssueSeverity.BLOCKING, ACTIONS_FOR[code]

    if status == 507:
        return (IssueCode.QUOTA_EXCEEDED, IssueSeverity.BLOCKING,
                ACTIONS_FOR[IssueCode.QUOTA_EXCEEDED])
    if status in (429, 503):
        return IssueCode.THROTTLED, IssueSeverity.WARNING, ()

    for pattern, code, sev in _RULES:
        if pattern.search(text):
            return code, sev, ACTIONS_FOR[code]

    if direction == "up":
        return (IssueCode.UPLOAD_FAILED, IssueSeverity.ERROR,
                ACTIONS_FOR[IssueCode.UPLOAD_FAILED])
    if direction == "down":
        return (IssueCode.DOWNLOAD_FAILED, IssueSeverity.ERROR,
                ACTIONS_FOR[IssueCode.DOWNLOAD_FAILED])
    return IssueCode.UNKNOWN, IssueSeverity.ERROR, ACTIONS_FOR[IssueCode.UNKNOWN]


def is_auth_failure(text: str) -> bool:
    low = (text or "").lower()
    return any(n in low for n, _ in AUTH_PATTERNS)


def is_fatal(code: IssueCode) -> bool:
    """HTTP 507 is a FatalError in rclone and 400/pathIsTooLong is a NoRetryError:
    neither is ever retried, so both must be surfaced immediately."""
    return code in (IssueCode.QUOTA_EXCEEDED, IssueCode.PATH_TOO_LONG,
                    IssueCode.AUTH_TENANT_BLOCKED)


def is_transient(code: IssueCode) -> bool:
    return code in (IssueCode.THROTTLED, IssueCode.NETWORK_UNREACHABLE,
                    IssueCode.FILE_IN_USE)
