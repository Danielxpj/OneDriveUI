"""Catching the names OneDrive will reject, before it rejects them.

OneDrive's naming rules are Windows' naming rules, and Linux has none of them.
``My: Report?.docx`` is a perfectly ordinary filename here and is unsyncable
there — so without this module the first the user hears about it is a failed
upload some minutes later, on a file they have already moved on from.

Every rule below is Microsoft's published restriction, and the reasons for the
odd-looking ones are worth keeping in view:

* **A trailing space or period is invalid** because Windows silently strips them
  when creating a file, so ``report.`` and ``report`` become the same name and
  the round trip is not stable.
* **``~$`` is reserved** because Office writes lock files with that prefix.
  Syncing them means racing a program that is going to delete them, and every
  such upload is wasted.
* **``_vti_`` is reserved anywhere in the path**, not just at the start: it is a
  SharePoint internal prefix, and OneDrive for Business is SharePoint.
* **The DOS device names** (``CON``, ``PRN``, ``AUX``, ``NUL``, ``COM1``…) are
  reserved with *or without* an extension, so ``NUL.txt`` is as invalid as
  ``NUL``.

Everything here is pure: no I/O except :func:`validate_size`, which stats one
file, and :func:`scan_tree`, which walks a directory under a time budget. That
matters because :func:`suggest` has to be *deterministic* — the rename it
proposes is the default in a dialog, and a default that changed between runs
would make the same conflict resolve differently each time it appeared.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from onedriveui.constants import (
    INVALID_CHARS,
    MAX_FILE_BYTES,
    MAX_REL_PATH_CHARS,
    MAX_TOTAL_PATH_CHARS,
    RESERVED_NAMES,
    RESERVED_PREFIXES,
    RESERVED_SUBSTRINGS,
)
from onedriveui.models import IssueCode

log = logging.getLogger(__name__)

__all__ = ["Violation", "validate_name", "validate_path", "validate_size",
           "suggest", "scan_tree", "REPLACEMENT"]

#: What an invalid character becomes. An underscore rather than a deletion, so
#: two files that differ only in their invalid characters do not collapse onto
#: one name — which would turn a naming problem into a data-loss problem.
REPLACEMENT: Final = "_"

#: The reserved names, upper-cased once. `RESERVED_NAMES` also carries
#: `desktop.ini` and `.lock`, which are not DOS devices but are equally
#: unsyncable, so the comparison is on the whole name as well as on the stem.
_RESERVED_UPPER: Final[frozenset[str]] = frozenset(n.upper() for n in RESERVED_NAMES)


@dataclass(frozen=True, slots=True)
class Violation:
    """One reason a path cannot be synced.

    Attributes:
        rel_path: The path, relative to the sync root.
        code: Which rule it broke.
        detail: A short, specific explanation — the characters at fault, the
            measured length. Shown beside the title, never instead of it.
        suggested_name: What :func:`suggest` proposes, when a rename would fix
            it. ``None`` when nothing can: a 300 GB file is not fixable by
            renaming it.
    """

    rel_path: str
    code: IssueCode
    detail: str
    suggested_name: str | None = None


# ═════════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════════

def validate_name(name: str) -> Violation | None:
    """Check one path component against OneDrive's naming rules.

    Args:
        name: A single file or folder name, never a path.

    Returns:
        The first :class:`Violation` found, or ``None`` when the name is fine.
        First, not all: the dialog shows one reason and one fix, and
        :func:`suggest` repairs every problem at once regardless.
    """
    if not name:
        return Violation("", IssueCode.NAME_INVALID, "the name is empty")

    bad = sorted({c for c in name if c in INVALID_CHARS})
    if bad:
        return Violation(name, IssueCode.NAME_INVALID,
                         "".join(bad), suggest(name))

    if name != name.rstrip(" ."):
        # Windows strips these silently when creating the file, so the name
        # would not survive its own round trip.
        return Violation(name, IssueCode.NAME_INVALID,
                         "it ends with a space or a period", suggest(name))

    for prefix in RESERVED_PREFIXES:
        if name.startswith(prefix):
            return Violation(name, IssueCode.RESERVED_NAME,
                             f"names starting with {prefix!r} are reserved", None)

    for substring in RESERVED_SUBSTRINGS:
        if substring in name:
            return Violation(name, IssueCode.RESERVED_NAME,
                             f"{substring!r} is reserved by SharePoint", None)

    upper = name.upper()
    # A DOS device name is reserved with or without an extension, so `NUL.txt`
    # is as unusable as `NUL`.
    if upper in _RESERVED_UPPER or upper.split(".")[0] in _RESERVED_UPPER:
        return Violation(name, IssueCode.RESERVED_NAME,
                         f"{name!r} is a reserved name", None)

    return None


def validate_path(rel_path: str, sync_root: Path | str) -> Violation | None:
    """Check a whole relative path: every component, plus both length limits.

    Args:
        rel_path: The path relative to `sync_root`, with ``/`` separators.
        sync_root: The account's local folder. Its length counts toward the
            total limit, which is why moving a synced folder deeper can break
            files that were fine before.

    Returns:
        The first :class:`Violation`, or ``None``.
    """
    if len(rel_path) > MAX_REL_PATH_CHARS:
        return Violation(rel_path, IssueCode.PATH_TOO_LONG,
                         f"{len(rel_path)} characters, limit "
                         f"{MAX_REL_PATH_CHARS}", None)

    total = len(str(sync_root).rstrip("/")) + 1 + len(rel_path)
    if total > MAX_TOTAL_PATH_CHARS:
        return Violation(rel_path, IssueCode.PATH_TOO_LONG,
                         f"{total} characters including the folder, limit "
                         f"{MAX_TOTAL_PATH_CHARS}", None)

    for part in rel_path.split("/"):
        if not part:
            continue
        violation = validate_name(part)
        if violation is not None:
            # Report the full path, so the user can find it, but keep the
            # component-level detail and suggestion.
            return Violation(rel_path, violation.code, violation.detail,
                             violation.suggested_name)
    return None


def validate_size(path: Path | str) -> Violation | None:
    """Check a file against OneDrive's 250 GB ceiling.

    Args:
        path: The local file.

    Returns:
        A :class:`Violation` when it is too large, or ``None`` — including when
        the file cannot be stat'ed at all, because a vanished file is somebody
        else's problem and not a naming one.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > MAX_FILE_BYTES:
        return Violation(str(path), IssueCode.FILE_TOO_LARGE,
                         f"{size} bytes, limit {MAX_FILE_BYTES}", None)
    return None


# ═════════════════════════════════════════════════════════════════════════════
# Repair
# ═════════════════════════════════════════════════════════════════════════════

def suggest(name: str) -> str:
    """Propose a valid name. **Deterministic** — the same input always answers
    the same thing.

    That property is the whole point. This is the pre-filled value in the Rename
    dialog and the automatic choice when a batch is repaired unattended; a
    suggestion that varied between runs would resolve the same conflict
    differently each time it came up, and two machines repairing the same file
    would disagree.

    Args:
        name: The offending name.

    Returns:
        A name that :func:`validate_name` accepts. The extension is preserved
        where there is one, because a ``.docx`` that stops opening in Word is a
        worse outcome than an awkward filename.
    """
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""

    cleaned = "".join(REPLACEMENT if c in INVALID_CHARS else c for c in stem)
    cleaned = cleaned.rstrip(" .")
    for prefix in RESERVED_PREFIXES:
        while cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    for substring in RESERVED_SUBSTRINGS:
        cleaned = cleaned.replace(substring, REPLACEMENT)
    cleaned = cleaned.strip() or "file"

    if cleaned.upper() in _RESERVED_UPPER:
        cleaned = f"{cleaned}{REPLACEMENT}"

    suffix = "".join(REPLACEMENT if c in INVALID_CHARS else c for c in suffix)
    suffix = suffix.rstrip(" .")
    candidate = f"{cleaned}.{suffix}" if suffix else cleaned

    # Belt and braces: the result must pass the validator it exists to satisfy.
    # If a rule is added above and not reflected here, this fails loudly at the
    # one call site rather than producing a rename that fails on upload.
    if validate_name(candidate) is not None:
        # A whole-name reservation such as `desktop.ini`, which the stem-level
        # check above cannot see. Prefix rather than truncate, so the extension
        # survives: a `.docx` that stops opening in Word is a worse outcome than
        # an awkward filename.
        cleaned = f"{REPLACEMENT}{cleaned}"
        candidate = f"{cleaned}.{suffix}" if suffix else cleaned
    return candidate


# ═════════════════════════════════════════════════════════════════════════════
# Scanning
# ═════════════════════════════════════════════════════════════════════════════

def scan_tree(root: Path | str, budget_ms: int = 250) -> Iterator[Violation]:
    """Walk a tree yielding violations, under a time budget.

    Args:
        root: The folder to check.
        budget_ms: Stop after this long. A first-run scan of a 300 000-item
            drive would otherwise block whatever called it; the caller resumes
            from where it stopped on the next pass, and the ladder shows
            "Processing changes" meanwhile.

    Yields:
        One :class:`Violation` per offending path, deepest last.

    The budget is checked between directories rather than between files, so a
    single enormous directory can overrun it. That is deliberate: aborting
    mid-directory would report a partial set of violations for it and the caller
    could not tell which.
    """
    root = Path(root)
    started = time.monotonic()
    for base, dirs, files in os.walk(root):
        if (time.monotonic() - started) * 1000.0 > budget_ms:
            log.debug("preflight scan of %s stopped at its %d ms budget",
                      root, budget_ms)
            return
        dirs.sort()
        for name in sorted(dirs) + sorted(files):
            path = Path(base) / name
            try:
                rel = str(path.relative_to(root))
            except ValueError:  # pragma: no cover - os.walk cannot produce this
                continue
            violation = validate_path(rel, root)
            if violation is not None:
                yield violation
                continue
            if name in files:
                size = validate_size(path)
                if size is not None:
                    yield Violation(rel, size.code, size.detail, None)
