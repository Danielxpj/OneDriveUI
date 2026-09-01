"""The bisync filters file — selective sync, and the ``--resync`` it obliges.

``filters-<account>.txt`` is what "Choose folders" writes. rclone guards it with
an MD5 sidecar: on a ``--resync`` run bisync stores the digest beside the file,
and on **every** later run it re-checks it. A mismatch is a *critical* abort —
exit 7, ``.lst`` renamed to ``.lst-err``, and every subsequent run refuses until
a ``--resync`` happens. Measured, both spellings **[V]**::

    ERROR : Bisync critical error: filters file md5 hash not found (must run --resync): …
    ERROR : Bisync critical error: filters file has changed (must run --resync): …
    ERROR : Bisync aborted. Must run --resync to recover.

That is invariant **I11**: a rewrite of this file is *always* paired with an
immediate ``--resync``, in one transaction. A crash between the two locks the
account out of syncing entirely. This module makes that coupling structural
rather than a rule reviewers must remember — see :func:`rewrite`, which restores
the previous file and raises :class:`SafetyRefusal` if the resync it demanded
never happened.

Invariant **I13** lives here too: ``- *.partial`` is never optional. A
``SIGKILL`` mid-transfer leaves ``<name>.<hash>.partial`` at the destination, and
without that rule the next run treats the fragment as a genuine new file and
syncs it everywhere — measured **[V]**::

    INFO  : - Path2    File is new               - big.bin.677c7953.partial
    INFO  : big.bin.677c7953.partial: Copied (new)

Syntax rules, all load-bearing (rclone rejects a violation outright with
``failed to reload "filter" options: malformed rule "…"`` **[V]**):

* the first non-whitespace character must be ``+``, ``-``, ``#``, ``;`` or ``!``;
* **exactly one space** between the sign and the pattern;
* everything after that space is the pattern, trailing whitespace included —
  which is why nothing here ever emits a trailing space;
* forward slashes only; a leading ``/`` anchors to the sync root; a trailing
  ``/`` matches a directory and prunes everything beneath it, so ``**`` is
  unnecessary and slower;
* rules are evaluated top to bottom, first match wins.

The rendered output of :func:`render` was fed to
``rclone lsf -R --filter-from -`` and parsed without error, selecting exactly the
intended subset **[V]**.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from onedriveui import APP_NAME, paths
from onedriveui.atomicio import atomic_write_text, md5_of_bytes
from onedriveui.constants import MANDATORY_EXCLUDES
from onedriveui.errors import ConfigError, SafetyRefusal
from onedriveui.paths import FILE_MODE
from onedriveui.rc import guards

__all__ = [
    "GLOB_METACHARACTERS",
    "HEADER_LINES",
    "MD5_LENGTH",
    "FiltersTransaction",
    "escape_pattern",
    "exclude_rule",
    "filters_config",
    "md5_of_text",
    "needs_resync",
    "read_rules",
    "render",
    "rewrite",
    "stored_md5",
    "validate",
    "write",
    "write_md5",
]

log = logging.getLogger(__name__)

#: Characters rclone treats as glob metacharacters and that can legally appear in
#: a real file name. A folder literally called ``Weird [name]`` must be escaped or
#: the rule silently becomes a character class and excludes the wrong things.
GLOB_METACHARACTERS: Final[str] = "*?[]{}\\"

#: The banner every generated file carries. The middle line is the one that
#: matters: a hand edit here is a *critical* bisync abort until a ``--resync``.
HEADER_LINES: Final[tuple[str, ...]] = (
    f"# {APP_NAME} selective-sync filters — GENERATED FILE, do not edit by hand.",
    "# Any change to this file REQUIRES an immediate bisync --resync (invariant I11):",
    "# until one runs, every bisync aborts with 'filters file has changed'.",
)

#: ``md5sum``'s first field: 32 lowercase hex characters, no filename, and — the
#: part that is easy to get wrong — **no trailing newline** **[V]**.
MD5_LENGTH: Final[int] = 32

#: A valid rule: a sign, exactly one space, then a non-empty pattern.
_RULE_RE: Final[re.Pattern[str]] = re.compile(r"^[+-] \S.*$")

#: Lines rclone ignores entirely: blank, or a comment. ``!`` on its own line
#: clears every rule defined above it — legal, and something we never emit.
_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*[#;]")

_SECTION_MANDATORY: Final[str] = "# --- never sync these, at any depth (invariants I11/I13) ---"
_SECTION_USER: Final[str] = "# --- folders unchecked in \"Choose folders\" (anchored to the sync root) ---"


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────

def escape_pattern(name: str) -> str:
    """Escape the glob metacharacters in a literal file or folder name.

    Args:
        name: One path component or a relative path. Forward slashes are
            **not** escaped — they are the path separator rclone expects.

    Returns:
        The name with ``*``, ``?``, ``[``, ``]``, ``{``, ``}`` and ``\\`` each
        prefixed by a backslash, so a folder genuinely named ``Weird [name]``
        excludes itself and nothing else.
    """
    out: list[str] = []
    for char in str(name):
        if char in GLOB_METACHARACTERS:
            out.append("\\")
        out.append(char)
    return "".join(out)


def exclude_rule(rel_path: str) -> str:
    """One anchored directory-exclusion rule for a folder the user unchecked.

    Args:
        rel_path: The folder relative to the sync root, forward slashes, with or
            without surrounding separators — e.g. ``"Documents/Personal"``.

    Returns:
        ``"- /Documents/Personal/"``: anchored with a leading ``/`` so only the
        top-level match is excluded, terminated with ``/`` so it matches a
        directory and prunes everything beneath it, and **without** a trailing
        ``**``, which rclone would only have to expand more slowly.

    Raises:
        ValueError: ``rel_path`` is empty or is only separators. Emitting
            ``"- //"`` would exclude the entire sync root.
    """
    rel = str(rel_path).replace("\\", "/").strip("/")
    if not rel:
        raise ValueError(
            "exclude_rule(): an empty path would render '- //' and exclude the "
            "entire sync root")
    return f"- /{escape_pattern(rel)}/"


def render(excluded_paths: Iterable[str] = (), *,
           header: bool = True,
           extra_rules: Sequence[str] = ()) -> str:
    """Build the whole filters file.

    Layout: the banner, then :data:`~onedriveui.constants.MANDATORY_EXCLUDES`
    **verbatim and first** (they are the ones invariants I11 and I13 depend on,
    and first-match-wins means order is semantics), then any caller-supplied
    extra rules, then one anchored directory rule per unchecked folder, sorted so
    two identical folder sets always render byte-identically — which is what
    makes :func:`write` able to say "unchanged" and skip a resync.

    There is deliberately **no** trailing ``- **``. This is the "sync everything
    except…" model, which is what the Windows client's "Choose folders" produces.
    An include-style file would need one; a caller that wants that passes its own
    rules through ``extra_rules``.

    Args:
        excluded_paths: Folders the user unchecked, relative to the sync root.
            Duplicates and stray separators are tolerated.
        header: Emit the banner. Off only for a diff-friendly comparison.
        extra_rules: Fully-formed rules to insert after the mandatory block —
            each validated like everything else.

    Returns:
        The file's exact text: UTF-8, ``\\n`` line endings, one trailing newline,
        no trailing whitespace on any line.

    Raises:
        ValueError: An excluded path was empty.
        ConfigError: A rule is malformed — rclone would refuse the whole file
            with ``malformed rule``.
        SafetyRefusal: invariant ``"I13"`` — the result somehow lacks
            ``- *.partial``. It cannot happen while ``MANDATORY_EXCLUDES`` is
            the frozen contract it is, and the assertion is here so that it
            cannot start happening.
    """
    lines: list[str] = []
    if header:
        lines.extend(HEADER_LINES)
        lines.append("")
    lines.append(_SECTION_MANDATORY)
    lines.extend(MANDATORY_EXCLUDES)

    extras = [str(rule).rstrip() for rule in extra_rules if str(rule).strip()]
    if extras:
        lines.append("")
        lines.extend(extras)

    folders = sorted({str(p).replace("\\", "/").strip("/")
                      for p in excluded_paths if str(p).strip("/")})
    if folders:
        lines.append("")
        lines.append(_SECTION_USER)
        lines.extend(exclude_rule(folder) for folder in folders)

    text = "\n".join(lines) + "\n"
    validate(text.splitlines())
    guards.assert_partial_excluded(text.splitlines())
    return text


def validate(lines: Iterable[str]) -> None:
    """Refuse a filters file rclone would reject.

    rclone parses the file once, at startup, and a single bad line kills the
    whole run with ``failed to reload "filter" options: malformed rule "…"``
    **[V]** — including on a ``--resync``, which is the run that would have
    unlocked the account. Catching it here turns a locked-out account into a
    validation error at the moment the user clicks Save.

    Args:
        lines: The file's lines, without their newlines.

    Raises:
        ConfigError: The first offending line, quoted with what is wrong. The
            two failures measured against the real binary are a missing space
            (``-*.partial``) and a missing sign (``*.partial``).
    """
    for number, raw in enumerate(lines, start=1):
        line = str(raw)
        if not line.strip() or _COMMENT_RE.match(line) or line.strip() == "!":
            continue
        stripped = line.lstrip()
        if stripped != line.rstrip() and stripped.rstrip() != stripped:
            raise ConfigError(
                f"filters line {number}: {line!r} has trailing whitespace, which "
                f"rclone treats as part of the pattern")
        if stripped[0] not in "+-":
            raise ConfigError(
                f"filters line {number}: {line!r} — a rule must begin with '+' "
                f"or '-'; rclone answers 'malformed rule'")
        if not _RULE_RE.match(stripped):
            raise ConfigError(
                f"filters line {number}: {line!r} — a rule needs exactly one "
                f"space between the sign and a non-empty pattern; rclone "
                f"answers 'malformed rule'")


def read_rules(path: Path | str) -> list[str]:
    """The active rules of a filters file, comments and blanks dropped.

    Args:
        path: The filters file.

    Returns:
        The rule lines, stripped of surrounding whitespace, in file order —
        which is evaluation order. ``[]`` when the file does not exist.
    """
    try:
        text = Path(os.path.expanduser(str(path))).read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines()
            if line.strip() and not _COMMENT_RE.match(line) and line.strip() != "!"]


# ─────────────────────────────────────────────────────────────────────────────
# The MD5 sidecar
# ─────────────────────────────────────────────────────────────────────────────

def md5_of_text(text: str) -> str:
    """The digest bisync will compute for this exact content.

    Args:
        text: The filters file's full text.

    Returns:
        32 lowercase hex characters — byte-identical to ``md5sum``'s first
        field, which is what rclone stores **[V]**.
    """
    return md5_of_bytes(text.encode("utf-8"))


def stored_md5(account_id: str) -> str:
    """The digest recorded beside the filters file, if any.

    Args:
        account_id: The account.

    Returns:
        32 lowercase hex characters, or ``""`` when the sidecar is missing,
        empty or not a digest. ``""`` means "bisync will demand a ``--resync``":
        an absent sidecar produces ``filters file md5 hash not found (must run
        --resync)`` **[V]**, exactly like a mismatch.
    """
    try:
        raw = paths.filters_md5_file(account_id).read_text(encoding="utf-8")
    except OSError:
        return ""
    digest = raw.strip().split()[0].lower() if raw.strip() else ""
    if len(digest) != MD5_LENGTH or any(c not in "0123456789abcdef" for c in digest):
        return ""
    return digest


def write_md5(account_id: str, text: str | None = None) -> str:
    """Record the filters digest, in rclone's own format.

    **Only ever call this after a ``--resync`` has actually succeeded.** Writing
    it at any other moment tells the next bisync run that a changed filters file
    is unchanged, which is precisely the silent-wrong-filters failure the digest
    exists to prevent.

    Args:
        account_id: The account.
        text: The content to hash. Defaults to the filters file on disk.

    Returns:
        The digest written.

    Raises:
        OSError: The sidecar could not be written, or ``text`` was omitted and
            the filters file is unreadable.
    """
    if text is None:
        text = paths.filters_file(account_id).read_text(encoding="utf-8")
    digest = md5_of_text(text)
    #: 0600 and NO trailing newline — byte-for-byte what rclone writes **[V]**.
    atomic_write_text(paths.filters_md5_file(account_id), digest, mode=FILE_MODE)
    return digest


def needs_resync(account_id: str) -> bool:
    """Whether bisync would refuse to run until a ``--resync`` happens.

    Args:
        account_id: The account.

    Returns:
        True when the filters file exists and its digest does not match the
        sidecar — including when the sidecar is missing, which rclone treats
        identically. False when there is no filters file at all: bisync then runs
        unfiltered and has nothing to compare.
    """
    filters = paths.filters_file(account_id)
    try:
        text = filters.read_text(encoding="utf-8")
    except OSError:
        return False
    return md5_of_text(text) != stored_md5(account_id)


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────

def write(account_id: str, text: str) -> bool:
    """Write the filters file, reporting whether the content actually changed.

    A ``True`` return is a **commitment**: invariant I11 says the caller must now
    run ``--resync`` in the same transaction. Do not call this directly unless
    you are inside :func:`rewrite`, which enforces that; it is public because
    provisioning and tests legitimately need the primitive.

    On a change the stale ``.md5`` sidecar is **deleted**, not rewritten. That is
    deliberate: with no sidecar the next bisync aborts loudly with ``filters file
    md5 hash not found (must run --resync)`` **[V]**, whereas a freshly written
    sidecar would let it run with filters that no longer match its listings.
    Failing loudly is the only safe direction.

    Args:
        account_id: The account.
        text: The full file content, from :func:`render`.

    Returns:
        True if the bytes on disk are now different from what was there before —
        which is exactly when a ``--resync`` is mandatory. False when the content
        was already identical, so a no-op settings save never forces one.

    Raises:
        ConfigError: The content is malformed (:func:`validate`).
        SafetyRefusal: invariant ``"I13"`` — the content lacks ``- *.partial``.
        OSError: The file could not be written. The previous content survives:
            the write is atomic.
    """
    validate(text.splitlines())
    guards.assert_partial_excluded(text.splitlines())

    target = paths.filters_file(account_id)
    try:
        previous = target.read_text(encoding="utf-8")
    except OSError:
        previous = None
    if previous == text:
        log.debug("filters for %r unchanged; no --resync needed", account_id)
        return False

    atomic_write_text(target, text, mode=FILE_MODE)
    paths.filters_md5_file(account_id).unlink(missing_ok=True)
    log.info("filters for %r rewritten (%d bytes); a --resync is now MANDATORY "
             "(invariant I11)", account_id, len(text.encode("utf-8")))
    return True


class FiltersTransaction:
    """The I11 transaction: rewrite the filters file, then ``--resync`` — or undo.

    A filters change without an immediate ``--resync`` is a critical bisync abort
    until one happens, and a crash between the two locks the account out of
    syncing. This context manager makes that impossible to forget by making the
    forgetting *undo itself*:

    * on entry the previous file and digest are captured;
    * :attr:`changed` says whether a resync is now owed;
    * :meth:`resynced` is how the caller reports that it ran and succeeded;
    * on exit, if a resync was owed and never reported — or the block raised —
      the previous file **and** its digest are restored, so the account is left
      exactly as syncable as it was, and :class:`SafetyRefusal` is raised.

    Use :func:`rewrite` rather than constructing this directly::

        with filters.rewrite(account.id, excluded) as txn:
            if txn.changed:
                verdict = run_bisync(resync=True)      # I15: answered decision
                if verdict is RunVerdict.OK:
                    txn.resynced()

    Attributes:
        account_id: The account.
        text: The content that was written.
        changed: Whether the write actually changed the file.
    """

    __slots__ = ("account_id", "text", "changed", "_previous", "_previous_md5",
                 "_resynced", "_entered")

    def __init__(self, account_id: str, text: str) -> None:
        """
        Args:
            account_id: The account.
            text: The full file content to write, from :func:`render`.
        """
        self.account_id = str(account_id)
        self.text = text
        self.changed = False
        self._previous: str | None = None
        self._previous_md5: str = ""
        self._resynced = False
        self._entered = False

    def __enter__(self) -> "FiltersTransaction":
        """Capture the old state, then write the new file.

        Returns:
            Self, with :attr:`changed` filled in.

        Raises:
            ConfigError: The content is malformed.
            SafetyRefusal: invariant ``"I13"``.
            OSError: The write failed; nothing was changed.
        """
        target = paths.filters_file(self.account_id)
        try:
            self._previous = target.read_text(encoding="utf-8")
        except OSError:
            self._previous = None
        self._previous_md5 = stored_md5(self.account_id)
        self._entered = True
        self.changed = write(self.account_id, self.text)
        return self

    def resynced(self) -> None:
        """Report that the mandatory ``--resync`` ran and succeeded.

        Records the new digest so the next ordinary run passes rclone's own
        check, and releases the rollback. Calling it when nothing changed is
        harmless.
        """
        self._resynced = True
        if self.changed:
            write_md5(self.account_id, self.text)

    def rollback(self) -> None:
        """Restore the previous filters file and digest.

        Called automatically on an unmet obligation. Restoring the *digest* too
        is the point: a restored file with no sidecar would still demand a
        resync, which is the lockout this exists to avoid.
        """
        if not self.changed:
            return
        target = paths.filters_file(self.account_id)
        if self._previous is None:
            target.unlink(missing_ok=True)
            paths.filters_md5_file(self.account_id).unlink(missing_ok=True)
        else:
            atomic_write_text(target, self._previous, mode=FILE_MODE)
            if self._previous_md5:
                atomic_write_text(paths.filters_md5_file(self.account_id),
                                  self._previous_md5, mode=FILE_MODE)
            else:
                paths.filters_md5_file(self.account_id).unlink(missing_ok=True)
        self.changed = False
        log.warning("rolled the filters file for %r back to its previous "
                    "content: the mandatory --resync did not happen",
                    self.account_id)

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 tb: TracebackType | None) -> bool:
        """Enforce invariant I11.

        Returns:
            False — an exception raised inside the block always propagates, after
            the filters file has been rolled back.

        Raises:
            SafetyRefusal: invariant ``"I11"`` — the block completed normally,
                the file changed, and no ``--resync`` was reported. The file has
                been restored first, so the account still syncs.
        """
        owed = self.changed and not self._resynced
        if owed:
            self.rollback()
        if exc_type is not None:
            return False
        if owed:
            raise SafetyRefusal(
                "I11",
                f"the filters file for {self.account_id!r} was rewritten without "
                f"the --resync that must accompany it; bisync would abort "
                f"critically (exit 7, .lst -> .lst-err) on every later run. The "
                f"previous file has been restored — call "
                f"FiltersTransaction.resynced() once the resync succeeds")
        return False


def rewrite(account_id: str, excluded_paths: Iterable[str] = (), *,
            text: str | None = None,
            extra_rules: Sequence[str] = ()) -> FiltersTransaction:
    """Open the invariant-I11 transaction around a filters rewrite.

    This is the only supported way to change selective sync. The returned object
    is a context manager; see :class:`FiltersTransaction` for the contract.

    Args:
        account_id: The account.
        excluded_paths: Folders the user unchecked, relative to the sync root.
        text: A pre-rendered file, bypassing :func:`render`. For tests and for a
            migration that must reproduce an exact byte sequence.
        extra_rules: Passed through to :func:`render`.

    Returns:
        An unentered :class:`FiltersTransaction`. Nothing is written until it is
        entered.
    """
    body = text if text is not None else render(excluded_paths,
                                                extra_rules=extra_rules)
    return FiltersTransaction(account_id, body)


def filters_config(account_id: str, *, changed: bool = False,
                   resync: bool = False,
                   extra_args: Sequence[str] = ()) -> dict[str, Any]:
    """The mapping :func:`~onedriveui.rc.guards.assert_bisync_safe` expects.

    Assembling it here keeps the guard's I11/I13 inputs and the file this module
    writes from ever disagreeing.

    Args:
        account_id: The account.
        changed: Whether the filters file was just rewritten.
        resync: Whether the run about to start carries ``--resync``.
        extra_args: Any extra rclone arguments, checked against I12.

    Returns:
        ``{"filters_file", "filters_lines", "filters_changed", "resync",
        "extra_args"}``.
    """
    path = paths.filters_file(account_id)
    return {
        "filters_file": str(path),
        "filters_lines": read_rules(path) or list(MANDATORY_EXCLUDES),
        "filters_changed": bool(changed),
        "resync": bool(resync),
        "extra_args": list(extra_args),
    }
