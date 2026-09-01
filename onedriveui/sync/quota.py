"""Storage quota: how much is left, and how confident we are about it.

``operations/about`` is one Graph request, and it is asked for in three
different places — the storage ring, the ladder's full-drive rung, and the token
health probe, since a call that returns 401 has told us about the token as well
as about the space. Doing it three times per tick would burn the per-user rate
limit on a number that changes in gigabytes. So it happens at most once every
five minutes and every caller reads the cache.

The five-minute TTL has one important exception: a **forced** refresh after a
large job. A user who has just freed 40 GB and still sees "Your OneDrive is
full" for four more minutes concludes the client is broken, and they are not
being unreasonable.

Two subtleties in what comes back:

* **``trashed`` reads 0 on OneDrive Personal**, even though the web recycle bin
  is real and can hold a great deal. It is never rendered as a storage tile,
  because a confident "0 bytes in the recycle bin" beside a full drive is worse
  than saying nothing.
* **A frozen account still reports free space.** Microsoft freezes an account
  that has been over quota for 30 days: it goes read-only while ``about`` keeps
  answering normally. That is detected from the write failures, not from the
  numbers, which is why :meth:`QuotaService.is_frozen` is set by the error path
  rather than computed here.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.constants import QUOTA_TTL_S
from onedriveui.errors import DaemonUnavailable, RcError, is_auth_failure
from onedriveui.models import AccountInfo, QuotaInfo, TokenHealth
from onedriveui.rc import auth, ops

log = logging.getLogger(__name__)

__all__ = ["QuotaService", "TIER_LABELS"]

#: The four bands the storage ring and the notices use. `QuotaInfo.tier` is the
#: authority on the thresholds; this only names them.
TIER_LABELS: Final[tuple[str, ...]] = ("ok", "warn", "critical", "full")


class QuotaService(QObject):
    """Cached ``operations/about``, and the token health that comes with it.

    Args:
        account: The account to ask about.
        endpoint: ``() -> RcEndpoint | None`` for the daemon to ask. A callable
            because the endpoint changes when a daemon restarts.
        ttl_s: How long a sample stays fresh.
        monotonic: The clock, injected for tests.
        parent: Qt parent.

    Signals:
        updated: A new :class:`~onedriveui.models.QuotaInfo`. Mirrors
            :data:`~onedriveui.bus.BUS.quota_updated`.
    """

    updated = Signal(QuotaInfo)

    def __init__(
        self,
        account: AccountInfo,
        *,
        endpoint: Any = None,
        ttl_s: float = QUOTA_TTL_S,
        monotonic: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._endpoint = endpoint or (lambda: None)
        self._ttl_s = ttl_s
        self._monotonic = monotonic or time.monotonic

        self._quota = QuotaInfo()
        self._token = TokenHealth.UNKNOWN
        self._sampled_at: float | None = None
        self._frozen = False

    # ═════════════════════════════════════════════════════════════════════════
    # Reads
    # ═════════════════════════════════════════════════════════════════════════

    def current(self) -> QuotaInfo:
        """The cached quota. Never blocks, never calls out.

        Returns an empty :class:`~onedriveui.models.QuotaInfo` before the first
        successful sample — and ``total == 0`` is the signal every consumer uses
        to mean "we have not learned anything yet", which is why a zero total
        must never be treated as a full drive.
        """
        return QuotaInfo(total=self._quota.total, used=self._quota.used,
                         free=self._quota.free, trashed=self._quota.trashed,
                         sampled_at=self._quota.sampled_at, frozen=self._frozen)

    def token(self) -> TokenHealth:
        """Token health, as a by-product of the last ``about``.

        The cheapest token probe there is: a call that has to happen anyway,
        whose 401 classifies the failure. Asking separately would double the
        request rate to learn the same thing.
        """
        return self._token

    def pct(self) -> float:
        return self._quota.pct

    def tier(self) -> str:
        """``"ok"`` / ``"warn"`` / ``"critical"`` / ``"full"``."""
        return self._quota.tier

    def is_full(self) -> bool:
        return self._quota.is_full

    def is_frozen(self) -> bool:
        """Has the account been frozen for being over quota for 30 days?

        Not computed from the numbers: a frozen account keeps reporting free
        space while refusing every write. Set by
        :meth:`note_write_failure` from the error path.
        """
        return self._frozen

    @property
    def age_s(self) -> float | None:
        """How old the cached sample is, or ``None`` if there has never been one."""
        if self._sampled_at is None:
            return None
        return self._monotonic() - self._sampled_at

    # ═════════════════════════════════════════════════════════════════════════
    # Refreshing
    # ═════════════════════════════════════════════════════════════════════════

    def refresh(self, *, force: bool = False) -> QuotaInfo:
        """Re-read ``operations/about`` if the cache is stale, or if forced.

        Args:
            force: Ignore the TTL. Use after anything that moved a lot of bytes
                — a completed upload batch, "Free up space", emptying the trash
                — because the alternative is telling a user who has just freed
                40 GB that their drive is still full for another four minutes.

        Returns:
            The current quota, refreshed or cached.

        Blocking, and therefore for an ``IOPool`` worker: the GUI thread reads
        :meth:`current` instead.
        """
        if not force and not self._stale():
            return self.current()

        endpoint = self._endpoint()
        if endpoint is None:
            log.debug("no endpoint to ask about %s's quota", self.account.id)
            return self.current()

        try:
            quota = ops.about(self.account.fs, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError) as exc:
            self._note_failure(exc)
            return self.current()

        self._quota = quota
        self._sampled_at = self._monotonic()
        self._token = TokenHealth.OK
        if self._frozen and not quota.is_full:
            # Space appeared, so whatever froze the account has been resolved.
            log.info("%s is no longer frozen", self.account.id)
            self._frozen = False
        log.debug("quota for %s: %d of %d used (%s)", self.account.id,
                  quota.used, quota.total, quota.tier)
        result = self.current()
        self.updated.emit(result)
        BUS.quota_updated.emit(result)
        return result

    def note_write_failure(self, error: str) -> bool:
        """Classify a failed write, for the frozen and 507 cases.

        Args:
            error: The error text from the failed operation.

        Returns:
            True when this was a storage failure rather than an ordinary one.

        HTTP **507 Insufficient Storage** is an rclone ``FatalError``: it is
        never retried, and it is the only definitive statement that the drive is
        full. It also arrives *before* ``about`` catches up, which is why the
        ladder reads a latch rather than the number.
        """
        text = (error or "").lower()
        if "507" in text or "insufficient storage" in text or "quotalimitreached" in text:
            log.warning("%s reported 507 / quota exceeded", self.account.id)
            return True
        if "accessdenied" in text and "quota" in text:
            # An account frozen for being over quota for 30 days answers
            # `about` normally and refuses every write. The numbers cannot see
            # this; only a rejected write can.
            self._frozen = True
            log.warning("%s appears to be frozen (read-only over quota)",
                        self.account.id)
            return True
        return False

    # ═════════════════════════════════════════════════════════════════════════
    # Internals
    # ═════════════════════════════════════════════════════════════════════════

    def _stale(self) -> bool:
        age = self.age_s
        return age is None or age >= self._ttl_s

    def _note_failure(self, exc: Exception) -> None:
        """Turn a failed ``about`` into token health, when it says something.

        A network failure says nothing about the token and must not be allowed
        to report one: showing "Sign in required" because the wifi dropped sends
        the user through an OAuth flow to fix a problem that was not there.
        """
        text = str(exc)
        if is_auth_failure(text):
            self._token = auth.classify_auth_error(text)
            log.warning("about() for %s failed on auth: %s",
                        self.account.id, self._token.value)
            return
        log.debug("about() for %s failed transiently: %s", self.account.id, text)
