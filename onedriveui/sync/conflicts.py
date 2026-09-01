"""Two people edited the same file, and both edits have to survive.

A conflict is the one sync outcome where doing nothing is unacceptable and doing
the obvious thing is worse. "Newest wins" silently destroys somebody's work;
"ask every time" makes a 200-file conflict unusable. Windows resolves it by
**keeping both**, renaming the loser after the device that produced it:

    Budget.xlsx  ->  Budget-LaptopName.xlsx

and that exact shape is reproduced here, down to the hyphen and the *short*
hostname — ``socket.gethostname().split(".")[0]``, so a machine whose FQDN is
``laptop.lan`` contributes ``-laptop``, not ``-laptop.lan``. A user with both
clients must see one naming convention, not two.

Conflicts are found two ways, and both are needed:

* **From the live bisync log**, which names them as they happen. Fast and
  precise, and gone the moment the log rotates.
* **By globbing the tree** for ``*.conflict1`` (rclone's own suffix) and
  ``*-<hostname>.*`` (ours). Slower, but it survives a crash, a log rotation and
  a machine that was switched off while the other one synced — and those are
  exactly the cases where a user finds a mystery file weeks later and cannot
  work out what it is.

Both Windows policies are offered. ``ASK`` raises an issue carrying the three
answers — keep both, keep the local copy, keep the cloud copy — and leaves both
files in place until the user picks one; ``KEEP_BOTH`` skips the question and
just tells them afterwards. Neither ever deletes a version, and "newest wins" is
not offered at all.
"""

from __future__ import annotations

import logging
import re
import socket
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.data import repo_sync
from onedriveui.models import (
    AccountInfo,
    ConflictInfo,
    ConflictPolicy,
    utcnow_iso,
)

log = logging.getLogger(__name__)

__all__ = ["ConflictDetector", "conflict_suffix", "device_name",
           "rename_for_conflict", "CONFLICT_LOG_RE", "RCLONE_CONFLICT_RE"]

#: rclone's own bisync conflict line. Captured from v1.75.0's output; the
#: numeric suffix is what `--conflict-suffix` produces when both sides changed.
CONFLICT_LOG_RE: Final = re.compile(
    r"^(?P<time>\S+\s+\S+)?\s*(?:NOTICE|INFO)\s*:\s*"
    r"(?P<path>.+?)\s*:\s*"
    r"(?:Files are conflicting|Conflict detected|"
    r"renaming (?:to|as) (?P<renamed>\S+))",
    re.IGNORECASE)

#: rclone's default conflict suffix on the filesystem: `name.conflict1`,
#: `name.conflict2`, … A file matching this was renamed by rclone rather than by
#: us, and both are ours to surface.
RCLONE_CONFLICT_RE: Final = re.compile(r"\.conflict\d+$")


def device_name() -> str:
    """This machine's short hostname, as Windows would spell it in a conflict.

    Short, not fully qualified: Windows uses the NetBIOS-style computer name, so
    a host whose FQDN is ``laptop.lan`` contributes ``laptop``. Getting this
    wrong produces ``Budget-laptop.lan.xlsx``, which is both ugly and — because
    of the extra dot — a different extension as far as some programs are
    concerned.
    """
    try:
        return socket.gethostname().split(".")[0] or "device"
    except OSError:  # pragma: no cover - gethostname does not fail in practice
        return "device"


def conflict_suffix(name: str | None = None) -> str:
    """The suffix appended to the losing copy: ``"-" + short hostname``.

    Args:
        name: Override the device name, for a user who has renamed their PC in
            settings. ``None`` uses :func:`device_name`.

    Returns:
        e.g. ``"-laptop"``. Reproduces Windows byte for byte, so a file that
        round-trips through both clients keeps one convention.
    """
    return f"-{name or device_name()}"


def rename_for_conflict(rel_path: str, name: str | None = None) -> str:
    """Where the losing copy goes.

    Args:
        rel_path: The contested path.
        name: The device name override.

    Returns:
        The same path with the suffix inserted **before** the extension —
        ``Budget.xlsx`` becomes ``Budget-laptop.xlsx``, not
        ``Budget.xlsx-laptop``. That is not cosmetic: appending after the
        extension makes the file stop opening in the program that wrote it,
        which is precisely the outcome "keep both" exists to avoid.
    """
    path = Path(rel_path)
    suffix = conflict_suffix(name)
    return str(path.with_name(f"{path.stem}{suffix}{path.suffix}"))


class ConflictDetector(QObject):
    """Finds conflicts, records them, and applies the chosen policy.

    Args:
        account: The account.
        policy: ``ASK`` or ``KEEP_BOTH``, from config.
        device: The device name for the suffix, from config.
        writer: The database writer.
        issues: The :class:`~onedriveui.sync.issues.IssueEngine`. Under the
            ``ASK`` policy the conflict is raised there — it is an issue with
            three fixes (keep both, keep local, keep cloud), not one of the
            destructive safety gates the decisions table exists for.
        parent: Qt parent.

    Signals:
        detected: A new :class:`~onedriveui.models.ConflictInfo`.
    """

    detected = Signal(ConflictInfo)

    def __init__(
        self,
        account: AccountInfo,
        *,
        policy: ConflictPolicy = ConflictPolicy.ASK,
        device: str | None = None,
        writer: Any = None,
        issues: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self.policy = policy
        self.device = device or device_name()
        self._writer = writer
        self._issues = issues

    # ═════════════════════════════════════════════════════════════════════════
    # Detection
    # ═════════════════════════════════════════════════════════════════════════

    def from_log(self, lines: Iterable[str], run_id: str = "") -> list[ConflictInfo]:
        """Scan bisync log lines for conflicts as they happen.

        Args:
            lines: Log lines, in order.
            run_id: The run they belong to, for the activity trail.

        Returns:
            One :class:`~onedriveui.models.ConflictInfo` per conflict, already
            recorded. Fast and precise — and useless five minutes later, when
            the log has rotated, which is why :meth:`from_tree` exists too.
        """
        found: list[ConflictInfo] = []
        for line in lines:
            match = CONFLICT_LOG_RE.search(line or "")
            if match is None:
                continue
            rel_path = (match.group("path") or "").strip()
            if not rel_path:
                continue
            found.append(self.record(rel_path, run_id=run_id,
                                     loser_path=match.group("renamed") or ""))
        return found

    def from_tree(self, root: Path | str) -> list[ConflictInfo]:
        """Find conflict *files* already on disk.

        Args:
            root: The sync root to walk.

        Returns:
            The conflicts found. This is the durable half of detection: it
            survives a crash, a rotated log and a machine that was off while the
            other one synced, which are exactly the cases where a user finds a
            mystery ``Budget-desktop.xlsx`` weeks later with no idea what it is.
        """
        root = Path(root)
        found: list[ConflictInfo] = []
        for path in self._conflict_files(root):
            rel = str(path.relative_to(root))
            found.append(self.record(self._original_of(rel), loser_path=rel))
        return found

    def _conflict_files(self, root: Path) -> Iterator[Path]:
        suffix = conflict_suffix(self.device)
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if RCLONE_CONFLICT_RE.search(path.name):
                yield path
            elif path.stem.endswith(suffix):
                yield path

    def _original_of(self, rel_path: str) -> str:
        """The name the conflicting copy was renamed *from*."""
        path = Path(rel_path)
        if RCLONE_CONFLICT_RE.search(path.name):
            return str(path.with_name(RCLONE_CONFLICT_RE.sub("", path.name)))
        suffix = conflict_suffix(self.device)
        if path.stem.endswith(suffix):
            return str(path.with_name(f"{path.stem[:-len(suffix)]}{path.suffix}"))
        return rel_path

    # ═════════════════════════════════════════════════════════════════════════
    # Recording and resolving
    # ═════════════════════════════════════════════════════════════════════════

    def record(self, rel_path: str, *, loser_path: str = "",
               run_id: str = "") -> ConflictInfo:
        """Persist a conflict and apply the policy.

        Args:
            rel_path: The contested path.
            loser_path: Where the losing copy went, if it has already been
                renamed.
            run_id: The bisync run, when there was one.

        Returns:
            The recorded conflict.
        """
        conflict = ConflictInfo(
            account_id=self.account.id,
            rel_path=rel_path,
            loser_path=loser_path or rename_for_conflict(rel_path, self.device),
            detected_at=utcnow_iso(),
            run_id=run_id,
        )
        try:
            conflict_id = repo_sync.add_conflict(conflict, writer=self._writer)
            conflict = _with(conflict, id=conflict_id)
        except Exception:  # noqa: BLE001 - a conflict must be surfaced regardless
            log.error("could not record the conflict on %s", rel_path, exc_info=True)

        log.warning("conflict on %s; the other copy is at %s",
                    rel_path, conflict.loser_path)
        self.detected.emit(conflict)
        BUS.conflict_detected.emit(conflict)
        self._apply_policy(conflict)
        return conflict

    def _apply_policy(self, conflict: ConflictInfo) -> None:
        """`ASK` raises an issue; `KEEP_BOTH` is already done by the rename.

        Neither policy ever deletes a version. "Newest wins" is not offered at
        all — it is the one resolution that silently destroys work, and no
        amount of it being convenient makes that acceptable in a file
        synchroniser.

        Under `ASK` the conflict becomes a `SyncIssue`, not a `Decision`. The
        distinction is real: a decision gates something destructive that is
        about to happen and resolves to "no" on silence, whereas a conflict has
        *already* been resolved safely by keeping both copies and is waiting for
        the user to say which one they want. Nothing is blocked while they
        think about it, and `ACTIONS_FOR[CONFLICT]` already names the three
        answers.
        """
        if self.policy is ConflictPolicy.KEEP_BOTH:
            log.info("keep-both policy: %s stays as %s",
                     conflict.rel_path, conflict.loser_path)
            return
        if self._issues is None:
            log.debug("no issue engine wired; the conflict on %s is recorded "
                      "but not surfaced", conflict.rel_path)
            return
        self._issues.raise_conflict(conflict)

    def open_conflicts(self) -> list[ConflictInfo]:
        """Every unresolved conflict for this account."""
        return repo_sync.open_conflicts(self.account.id)

    def resolve(self, conflict_id: int, resolution: str) -> None:
        """Mark a conflict settled.

        Args:
            conflict_id: The row.
            resolution: What was chosen — ``"keep_both"``, ``"keep_local"``,
                ``"keep_cloud"``. Recorded verbatim so the activity feed can say
                what happened rather than that "a conflict was resolved".
        """
        repo_sync.resolve_conflict(conflict_id, resolution, writer=self._writer)
        log.info("conflict %s resolved: %s", conflict_id, resolution)


def _with(conflict: ConflictInfo, **changes: Any) -> ConflictInfo:
    from dataclasses import replace

    return replace(conflict, **changes)
