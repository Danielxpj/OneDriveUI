"""Questions only a human can answer, and what happens when nobody does.

A decision is raised when the safe action and the requested action disagree and
the difference is destructive: 4 000 files are about to be deleted; every file
on the drive looks different; a resync is being asked for. In each case the
engine *could* proceed, and in each case proceeding without asking is how a
sync client destroys somebody's work.

Three rules make this trustworthy rather than merely careful:

**Silence is never consent.** An unanswered decision expires after seven days,
matching Microsoft's own policy — and expiring it means **not doing the thing**.
It resolves to "we did not delete them", the payload records that explicitly,
and nothing is retried. A design where a timeout meant "go ahead" would turn a
laptop left closed for a week into a data-loss event.

**Decisions survive a crash.** They live in SQLite, not in a dialog's state, so
a client that is killed mid-question comes back still asking it. The alternative
— an unanswered question silently vanishing — leaves sync wedged with no
explanation the user can act on.

**A safety abort is never automatically retried with ``--force``.** When rclone
says ``Safety abort: too many deletes (>25%, 4231 of 16000)`` it has stopped for
exactly the right reason. :meth:`DecisionCenter.on_maxdelete_abort` records the
numbers, raises one decision, and issues **zero** rclone commands. Retrying with
``--force`` on the user's behalf is the single most destructive thing this
codebase could be made to do.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.constants import DECISION_EXPIRY_DAYS
from onedriveui.data import repo_sync
from onedriveui.models import (
    AccountInfo,
    Decision,
    DecisionKind,
    RunRecord,
    utcnow_iso,
)

log = logging.getLogger(__name__)

__all__ = ["DecisionCenter", "MAXDELETE_RE", "ALLCHANGED_RE", "parse_maxdelete",
           "ANSWER_YES", "ANSWER_NO", "ANSWER_EXPIRED"]

#: rclone v1.75.0's safety abort, captured verbatim:
#: ``Safety abort: too many deletes (>25%, 4231 of 16000) on Path1 "…".
#: Run with --force if desired.``
MAXDELETE_RE: Final = re.compile(
    r"too many deletes\s*\(\s*>\s*(?P<percent>\d+)\s*%\s*,\s*"
    r"(?P<deletes>\d+)\s+of\s+(?P<total>\d+)\s*\)"
    r"(?:\s+on\s+(?P<side>Path\d))?",
    re.IGNORECASE)

#: The other safety abort: every file on one side looks different, which almost
#: always means the wrong folder was chosen rather than that the user really did
#: rewrite 16 000 files.
ALLCHANGED_RE: Final = re.compile(
    r"all files were changed", re.IGNORECASE)

#: The three answers a decision can carry. `ANSWER_EXPIRED` is deliberately not
#: `ANSWER_NO`: "the user declined" and "nobody was there" are different facts,
#: and only one of them means the question is worth asking again.
ANSWER_YES: Final = "yes"
ANSWER_NO: Final = "no"
ANSWER_EXPIRED: Final = "expired"


def parse_maxdelete(text: str) -> dict[str, Any] | None:
    """Pull the numbers out of a ``Safety abort: too many deletes`` line.

    Args:
        text: The log text, or the whole log.

    Returns:
        ``{"percent", "deletes", "total", "side"}``, or ``None`` when the line
        is not there. The numbers matter: "delete 4 231 of your 16 000 files?"
        is a question a user can answer, and "sync stopped for safety reasons"
        is not.
    """
    match = MAXDELETE_RE.search(text or "")
    if match is None:
        return None
    return {
        "percent": int(match.group("percent")),
        "deletes": int(match.group("deletes")),
        "total": int(match.group("total")),
        "side": match.group("side") or "Path1",
    }


class DecisionCenter(QObject):
    """Raises, answers and expires the questions that stop destructive work.

    Args:
        account: The account.
        writer: The database writer. These writes are urgent: a decision lost to
            a crash is a question that silently stopped being asked.
        parent: Qt parent.

    Signals:
        required: A new :class:`~onedriveui.models.Decision` needing an answer.
        answered: ``(decision_id, answer)``.
    """

    required = Signal(Decision)
    answered = Signal(int, str)

    def __init__(
        self,
        account: AccountInfo,
        *,
        writer: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._writer = writer

    # ═════════════════════════════════════════════════════════════════════════
    # Raising
    # ═════════════════════════════════════════════════════════════════════════

    def require(self, kind: DecisionKind, payload: dict[str, Any],
                expires_in_days: int = DECISION_EXPIRY_DAYS) -> int:
        """Record a question and ask it.

        Args:
            kind: What is being asked.
            payload: Everything needed to render the question *and* to act on
                the answer. It is stored, so a client restarted three days later
                can still show the dialog with the real numbers in it rather
                than a vague warning.
            expires_in_days: How long before silence resolves to "no".

        Returns:
            The decision id. Pass it to
            :meth:`~onedriveui.sync.supervisor.Supervisor.request_resync` and
            friends — invariant I15 requires it.
        """
        decision = Decision(
            account_id=self.account.id,
            kind=kind,
            payload=dict(payload),
            created_at=utcnow_iso(),
        )
        decision_id = repo_sync.create_decision(
            decision, expiry_days=expires_in_days, writer=self._writer)
        stored = _with(decision, id=decision_id)
        log.warning("decision %s required for %s: %s",
                    decision_id, self.account.id, kind.value)
        self.required.emit(stored)
        BUS.decision_required.emit(stored)
        return decision_id

    # ═════════════════════════════════════════════════════════════════════════
    # Answering
    # ═════════════════════════════════════════════════════════════════════════

    def answer(self, decision_id: int, answer: str) -> None:
        """Record the user's answer.

        Args:
            decision_id: The row.
            answer: ``"yes"``, ``"no"``, or a kind-specific string. Stored
                verbatim, because ``assert_resync_approved`` reads it back and
                an approval has to be an approval, not a truthy value.
        """
        repo_sync.answer_decision(decision_id, answer, writer=self._writer)
        log.info("decision %s answered %r", decision_id, answer)
        self.answered.emit(decision_id, answer)
        BUS.decision_answered.emit(decision_id, answer)

    def pending(self, account_id: str | None = None) -> list[Decision]:
        """Every unanswered, unexpired decision."""
        return repo_sync.pending_decisions(account_id or self.account.id)

    def expire_stale(self) -> int:
        """Age out unanswered decisions. **Expiry means DO NOT DELETE.**

        Returns:
            How many expired.

        The direction is the whole point. An unanswered "delete 4 231 files?"
        that reaches its seventh day resolves to *not deleting them*: the row is
        closed, nothing is retried, and the next run will ask again if the
        condition still holds. A timeout that meant "go ahead" would turn a
        laptop left closed for a week into a data-loss event, which is why
        Microsoft's own policy works this way too.
        """
        expired = repo_sync.expire_decisions(self.account.id, writer=self._writer)
        if expired:
            log.info("%d decisions for %s expired unanswered; NOTHING was "
                     "deleted and nothing was retried",
                     len(expired), self.account.id)
        return len(expired)

    # ═════════════════════════════════════════════════════════════════════════
    # Safety aborts
    # ═════════════════════════════════════════════════════════════════════════

    def on_maxdelete_abort(self, run: RunRecord,
                           parsed: dict[str, Any] | None = None) -> int:
        """Turn rclone's delete-safety abort into one question. **Runs nothing.**

        Args:
            run: The aborted run.
            parsed: :func:`parse_maxdelete`'s output, or ``None`` to parse
                ``run.summary`` here.

        Returns:
            The decision id, or ``0`` when the log did not actually carry a
            safety abort.

        **No rclone command is issued, on any path through this method.** rclone
        stopped because a quarter of the drive was about to disappear, and it
        was right to. Re-running with ``--force`` is a decision belonging to the
        person whose files they are; doing it automatically — even "just this
        once, because it is probably a mount that was not ready" — is the single
        most destructive thing this codebase could be made to do.
        """
        numbers = parsed if parsed is not None else parse_maxdelete(run.summary or "")
        if not numbers:
            log.debug("run %s was not a delete-safety abort", run.run_id)
            return 0

        log.error("run %s aborted for safety: %s of %s deletions (>%s%%) on %s; "
                  "asking the user and running nothing",
                  run.run_id, numbers.get("deletes"), numbers.get("total"),
                  numbers.get("percent"), numbers.get("side"))
        return self.require(DecisionKind.MASS_DELETE, {
            **numbers,
            "run_id": run.run_id,
            "count": numbers.get("deletes", 0),
            "nothing_was_deleted": True,
        })

    def on_allchanged_abort(self, run: RunRecord) -> int:
        """The other safety abort: every file on one side looks different.

        Args:
            run: The aborted run.

        Returns:
            The decision id, or ``0``.

        Almost always the wrong folder rather than a genuine rewrite of 16 000
        files — a mount that was not ready when the run started presents an
        empty directory, and an empty directory looks exactly like "the user
        deleted everything". Asking is cheap; being wrong is not.
        """
        if not ALLCHANGED_RE.search(run.summary or ""):
            return 0
        log.error("run %s aborted: all files were changed. This is usually the "
                  "wrong folder, not a real change; running nothing", run.run_id)
        return self.require(DecisionKind.ALL_CHANGED, {
            "run_id": run.run_id,
            "nothing_was_deleted": True,
        })


def _with(decision: Decision, **changes: Any) -> Decision:
    from dataclasses import replace

    return replace(decision, **changes)
