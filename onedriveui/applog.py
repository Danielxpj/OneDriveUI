"""Logging, redaction and the diagnostics bundle.

Three surfaces, one redaction pass in front of all of them:

* a rotating file at ``~/.local/state/onedriveui/logs/app.log`` (5 MB × 5),
* a 500-line in-memory ring buffer that the About page renders and the
  "Report a problem" bundle embeds,
* ``stderr``, for a run started from a terminal or captured by journald.

**Invariant I14 is the reason this module exists in its own file.** rclone's
``config/dump`` and ``config/get`` return the OAuth *refresh token in the
clear*, and ``rclone config show`` prints it too. A refresh token is a durable
credential for the user's entire OneDrive; one of them in a log file a user
pastes into a bug report is a full account compromise. So:

* :func:`redact` runs on **every** record before it reaches any handler, and it
  removes the key name as well as the value — a bundle that still contained the
  literal string ``refresh_token`` would tell an attacker exactly what to grep
  for, and would mean the pattern had matched only half of what it should.
* :func:`build_diagnostics_bundle` shells out to ``rclone config redacted`` and
  never touches ``config/dump``, ``config/get`` or ``config show``. That is not
  a preference; it is the invariant.

Redaction is applied at the *handler* boundary rather than at each call site,
because the only reliable redaction is the one a careless caller cannot skip.
"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import threading
import zipfile
from collections import deque
from pathlib import Path
from typing import Any, Final, Iterable, Iterator

from onedriveui import APP_ID, APP_NAME, USER_AGENT, __version__
from onedriveui import paths
from onedriveui.bus import BUS
from onedriveui.models import utcnow_iso

__all__ = [
    "install", "get_logger", "RingBuffer", "RING", "redact", "REDACT_PATTERNS",
    "REDACTED", "build_diagnostics_bundle", "rclone_config_redacted",
    "RedactingFilter", "LOG_MAX_BYTES", "LOG_BACKUP_COUNT", "RING_CAPACITY",
    "BUNDLE_LOG_LINES", "is_installed", "uninstall", "set_level",
]

#: 5 MB × 5, per ARCHITECTURE §13.
LOG_MAX_BYTES: Final[int] = 5 * 1024 * 1024
LOG_BACKUP_COUNT: Final[int] = 5

#: The ring the About page renders. §5.7 wants "the last 200 redacted stderr
#: lines" available after five failed daemon starts; 500 keeps the lines that
#: led up to them too.
RING_CAPACITY: Final[int] = 500

#: How many ring lines the bundle embeds.
BUNDLE_LOG_LINES: Final[int] = 200

#: What replaces a secret. Deliberately contains none of the key names it
#: replaces, so a bundle can be grepped for ``refresh_token`` and come back
#: empty.
REDACTED: Final[str] = "[redacted]"

_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S"

#: The root logger every module hangs off. Never the true root: a library that
#: logs to the root logger must not end up in the user's app.log.
ROOT_NAME: Final[str] = APP_ID


# ─────────────────────────────────────────────────────────────────────────────
# Redaction
# ─────────────────────────────────────────────────────────────────────────────

#: (pattern, replacement) pairs, applied in order. Order matters: the whole
#: ``token = {...}`` blob is destroyed before the individual key patterns run,
#: so a malformed blob cannot leave a fragment behind.
#:
#: Each pattern is written to swallow the key name too. ``"refresh_token":"M.C5…"``
#: becomes ``[redacted]`` and not ``"refresh_token":"[redacted]"``.
REDACT_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # rclone.conf's single-line token object, and any JSON containing one.
    (re.compile(r"""(?ix)
        \b(?:token|oauth[_-]?token)\s*[:=]\s*
        \{.*?\}
    """), REDACTED),
    # Bare token keys, in JSON, in an INI file, or in a query string.
    (re.compile(r"""(?ix)
        ["']?\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token
                 |client[_-]?secret|api[_-]?key)["']?
        \s*[:=]\s*
        (?:"[^"]*"|'[^']*'|[^\s,;}&"']+)
    """), REDACTED),
    # The rc daemon's generated credentials, on a command line (`--rc-pass x`,
    # `--rc-pass=x`) or in endpoints.json (`"rc-pass": "x"`). The optional quote
    # after the key name matters: without it the JSON form matches only `": "`
    # and leaves the secret itself in the line.
    (re.compile(r"""(?ix)
        (?:--)?\brc[-_]pass(?:word)?\b["']?\s*(?:[=:]|\s)\s*
        (?:"[^"]*"|'[^']*'|[^\s,;}&"']+)
    """), REDACTED),
    (re.compile(r"""(?ix)
        ["']?\b(?:pass|password|passwd|secret)["']?\s*[:=]\s*
        (?:"[^"]*"|'[^']*'|[^\s,;}&"']+)
    """), REDACTED),
    # The OAuth callback rclone opens: the state parameter is a bearer of the
    # in-flight authorisation and must never be pasted into a bug report.
    (re.compile(r"(?i)(auth\?state=)[^\s\"'&]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)([?&]state=)[^\s\"'&]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)([?&]code=)[^\s\"'&]+"), r"\1" + REDACTED),
    # HTTP basic/bearer credentials, as they appear in a request dump.
    (re.compile(r"(?i)(Authorization:\s*)(?:Basic|Bearer|Digest)?\s*\S+"),
     r"\1" + REDACTED),
)


def redact(text: str) -> str:
    """Strip every credential shape from a string.

    Args:
        text: Any log line, rclone output, config excerpt or exception message.

    Returns:
        The same text with each secret — key name included — replaced by
        :data:`REDACTED`. A string containing no secret is returned unchanged.

    The key name is removed along with the value on purpose: leaving
    ``"refresh_token": "[redacted]"`` behind both advertises what the file
    contains and makes it impossible to assert that a diagnostics bundle is
    clean by searching for the word.
    """
    if not text:
        return text
    out = str(text)
    for pattern, replacement in REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFilter(logging.Filter):
    """A logging filter that redacts the formatted message of every record.

    Installed on every handler rather than on the logger, because a handler
    added later by a well-meaning caller would otherwise bypass a logger-level
    filter entirely.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record in place and always keep it.

        Args:
            record: The record about to be emitted.

        Returns:
            Always True — this filter censors, it never drops.
        """
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken %-format in a caller
            message = str(record.msg)
        cleaned = redact(message)
        if cleaned != message or record.args:
            record.msg = cleaned
            record.args = ()
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# The ring buffer
# ─────────────────────────────────────────────────────────────────────────────

class RingBuffer(logging.Handler):
    """The last N formatted, redacted log lines, in memory.

    Doubles as a :class:`logging.Handler` so nothing has to remember to feed it,
    and emits :data:`~onedriveui.bus.BUS` ``log_line`` for each record so the
    About page can append live instead of polling.

    The deque is bounded, so this cannot grow without limit in a long-running
    session, and ``deque.append`` is atomic under the GIL, which is what makes
    it safe to log from the ``DbWriter`` thread and the IOPool.
    """

    def __init__(self, capacity: int = RING_CAPACITY, *, emit_signal: bool = True) -> None:
        """
        Args:
            capacity: How many lines to keep. Older lines fall off the front.
            emit_signal: Emit ``BUS.log_line`` per record. Off for a private
                buffer that is not the application's.
        """
        super().__init__()
        self._lines: deque[str] = deque(maxlen=max(1, int(capacity)))
        self._emit_signal = bool(emit_signal)
        self.addFilter(RedactingFilter())
        self.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))

    @property
    def capacity(self) -> int:
        """The maximum number of retained lines."""
        return self._lines.maxlen or 0

    def emit(self, record: logging.LogRecord) -> None:
        """Format, store and announce one record.

        Args:
            record: The record to keep.

        A failure here is swallowed via ``handleError``: logging must never be
        the thing that crashes the application.
        """
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)
            return
        self._lines.append(line)
        if self._emit_signal:
            try:
                BUS.log_line.emit(line)
            except RuntimeError:  # pragma: no cover - BUS torn down at exit
                pass

    def lines(self, limit: int | None = None) -> list[str]:
        """The retained lines, oldest first.

        Args:
            limit: Return at most this many, taken from the **end** (the most
                recent). ``None`` returns everything.

        Returns:
            A snapshot list; mutating it does not affect the buffer.
        """
        snapshot = list(self._lines)
        if limit is not None and limit >= 0:
            return snapshot[-limit:] if limit else []
        return snapshot

    def text(self, limit: int | None = None) -> str:
        """The retained lines as one newline-joined block."""
        return "\n".join(self.lines(limit))

    def clear(self) -> None:
        """Drop every retained line."""
        self._lines.clear()

    def __len__(self) -> int:
        return len(self._lines)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._lines))


#: The application's ring buffer. Installed by :func:`install`; readable before
#: then, and simply empty.
RING: Final[RingBuffer] = RingBuffer()


# ─────────────────────────────────────────────────────────────────────────────
# Installation
# ─────────────────────────────────────────────────────────────────────────────

_INSTALL_LOCK = threading.Lock()
_INSTALLED: dict[str, Any] = {}


def is_installed() -> bool:
    """True once :func:`install` has attached handlers."""
    return bool(_INSTALLED)


def get_logger(name: str = "") -> logging.Logger:
    """Return the logger a module should use.

    Args:
        name: A dotted module suffix, e.g. ``"rc.client"``. An empty name
            returns the application root logger. A name that already starts
            with the application root is used as-is, so
            ``get_logger(__name__)`` works from anywhere in the package.

    Returns:
        A :class:`logging.Logger` under the ``onedriveui`` root. Its records
        never propagate to Python's true root logger, so a third-party
        ``basicConfig`` cannot duplicate them or send them somewhere unredacted.
    """
    if not name:
        return logging.getLogger(ROOT_NAME)
    if name == ROOT_NAME or name.startswith(ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def install(
    level: str | int = "INFO",
    *,
    log_file: Path | str | None = None,
    stderr: bool = True,
    ring: RingBuffer | None = None,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> logging.Logger:
    """Attach the file, stderr and ring handlers to the application logger.

    Idempotent: calling it twice replaces the handlers rather than duplicating
    every line, which matters because the OOBE and the main window both want to
    raise the log level.

    Args:
        level: ``"DEBUG"``, ``"INFO"``, ``"WARNING"`` or a ``logging`` constant,
            from ``advanced.log_level``.
        log_file: Where to write. Defaults to
            :func:`onedriveui.paths.log_file`.
        stderr: Also write to ``sys.stderr``. Off for a GUI-only run whose
            stderr goes nowhere useful.
        ring: The buffer to feed. Defaults to the module-level :data:`RING`.
        max_bytes: Rotation threshold per file.
        backup_count: How many rotated files to keep.

    Returns:
        The configured application root logger.

    A file that cannot be opened — a read-only or full ``~/.local/state`` — is
    not fatal: the stderr and ring handlers are installed anyway and a warning
    is logged through them. Losing the log must never stop the sync client.
    """
    target = Path(log_file) if log_file is not None else paths.log_file()
    buffer = ring if ring is not None else RING

    with _INSTALL_LOCK:
        logger = logging.getLogger(ROOT_NAME)
        logger.setLevel(_as_level(level))
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if handler is not buffer:
                try:
                    handler.close()
                except Exception:  # pragma: no cover - defensive
                    pass

        formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)
        file_error: str | None = None
        file_handler: logging.Handler | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            file_handler = logging.handlers.RotatingFileHandler(
                str(target), maxBytes=int(max_bytes),
                backupCount=int(backup_count), encoding="utf-8", delay=False)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(RedactingFilter())
            logger.addHandler(file_handler)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        except OSError as exc:
            file_error = f"cannot write {target}: {exc}"

        if stderr:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(formatter)
            stream.addFilter(RedactingFilter())
            logger.addHandler(stream)

        logger.addHandler(buffer)

        _INSTALLED.clear()
        _INSTALLED.update({
            "path": target, "ring": buffer, "file_handler": file_handler,
            "level": logger.level,
        })

    if file_error:
        logger.warning("logging to file disabled: %s", file_error)
    return logger


def set_level(level: str | int) -> None:
    """Change the application log level in place.

    Args:
        level: ``"DEBUG"``, ``"INFO"``, ``"WARNING"`` or a ``logging`` constant.
    """
    resolved = _as_level(level)
    logging.getLogger(ROOT_NAME).setLevel(resolved)
    if _INSTALLED:
        _INSTALLED["level"] = resolved


def uninstall() -> None:
    """Detach and close every handler. Used at shutdown and between tests."""
    with _INSTALL_LOCK:
        logger = logging.getLogger(ROOT_NAME)
        buffer = _INSTALLED.get("ring")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if handler is not buffer:
                try:
                    handler.close()
                except Exception:  # pragma: no cover - defensive
                    pass
        _INSTALLED.clear()


def _as_level(level: str | int) -> int:
    """Coerce a level name or number to a ``logging`` constant."""
    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(str(level).upper())
    return resolved if resolved is not None else logging.INFO


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics bundle
# ─────────────────────────────────────────────────────────────────────────────

def rclone_config_redacted(
    rclone_path: str = "/usr/bin/rclone",
    remote: str | None = None,
    *,
    timeout_s: float = 10.0,
) -> str:
    """Run ``rclone config redacted`` and return its output.

    This is the **only** sanctioned way to describe an rclone remote in a
    document a user might share. ``rclone config show``, the rc ``config/dump``
    and the rc ``config/get`` all print the OAuth refresh token in the clear,
    and invariant I14 forbids every one of them here.

    Args:
        rclone_path: The rclone binary, from ``advanced.rclone_path``.
        remote: A single remote to describe, or ``None`` for all of them.
        timeout_s: How long to wait before giving up.

    Returns:
        rclone's output with :func:`redact` applied on top — belt and braces,
        because a future rclone that redacts one fewer field must not silently
        leak through us. On any failure, a one-line explanation instead; a
        diagnostics bundle that is missing a section is far better than one that
        refuses to be built.
    """
    argv = [str(rclone_path), "config", "redacted"]
    if remote:
        argv.append(str(remote))
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"# `{' '.join(argv)}` failed: {exc}\n"
    body = completed.stdout or ""
    if completed.returncode != 0:
        body += f"\n# exit {completed.returncode}\n{completed.stderr or ''}"
    return redact(body)


def _rclone_version(rclone_path: str, *, timeout_s: float = 10.0) -> str:
    """``rclone version`` output, or an explanation of why it is missing."""
    try:
        completed = subprocess.run(
            [str(rclone_path), "version"], capture_output=True, text=True,
            timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"# rclone version failed: {exc}\n"
    return redact(completed.stdout or completed.stderr or "")


def _environment_report() -> str:
    """A human-readable description of the machine, with no secrets in it."""
    try:
        uname = platform.uname()
        machine = f"{uname.system} {uname.release} {uname.machine}"
    except Exception:  # pragma: no cover - defensive
        machine = "unknown"
    try:
        from PySide6 import __version__ as pyside_version  # noqa: PLC0415
    except Exception:  # pragma: no cover - PySide6 is a hard dependency
        pyside_version = "unavailable"

    mounts = paths.fuse_rclone_mounts()
    lines = [
        f"{APP_NAME} {__version__}",
        f"generated: {utcnow_iso()}",
        f"user-agent: {USER_AGENT}",
        f"python: {sys.version.split()[0]}",
        f"pyside6: {pyside_version}",
        f"platform: {machine}",
        f"desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '?')}"
        f" session={os.environ.get('XDG_SESSION_TYPE', '?')}",
        "",
        "paths:",
        f"  config: {paths.config_file()}",
        f"  database: {paths.db_file()}",
        f"  logs: {paths.log_dir()}",
        f"  runtime: {paths.runtime_dir()}",
        "",
        f"fuse.rclone mounts ({len(mounts)}):",
    ]
    lines += [f"  {fs} -> {mountpoint}" for fs, mountpoint in mounts] or ["  (none)"]
    return redact("\n".join(lines) + "\n")


def _config_snapshot() -> str:
    """``config.json``, redacted, or an explanation.

    Read as text rather than through ``config.load()`` so the bundle shows what
    is actually on disk — including the malformed key that is being reported.
    """
    path = paths.config_file()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"# {path} unreadable: {exc}\n"
    return redact(raw)


def _iter_log_files(log_file: Path) -> Iterable[Path]:
    """The live log plus its rotations, newest first."""
    if log_file.exists():
        yield log_file
    for index in range(1, LOG_BACKUP_COUNT + 1):
        rotated = log_file.with_name(f"{log_file.name}.{index}")
        if rotated.exists():
            yield rotated


def build_diagnostics_bundle(
    destination: Path | str | None = None,
    *,
    rclone_path: str = "/usr/bin/rclone",
    remote: str | None = None,
    ring: RingBuffer | None = None,
    include_logs: bool = True,
    include_config: bool = True,
    include_rclone: bool = True,
) -> Path:
    """Build the "Report a problem" zip.

    Contents:

    ==========================  ====================================
    ``report.txt``              versions, paths, live FUSE mounts
    ``recent.log``              the last :data:`BUNDLE_LOG_LINES` ring lines
    ``config.json``             our own config, redacted
    ``rclone-config-redacted``  ``rclone config redacted`` output
    ``rclone-version.txt``      ``rclone version`` output
    ``logs/app.log*``           the rotating log and its backups
    ``manifest.json``           what was included, and what was refused
    ==========================  ====================================

    Every member is passed through :func:`redact` on the way in, including the
    log files, which were already redacted when written — a file the user
    edited, or one written before :func:`install` ran, must not slip through.

    Args:
        destination: The zip to write. Defaults to
            ``<cache_dir>/diagnostics-<timestamp>.zip``.
        rclone_path: The rclone binary to interrogate.
        remote: Restrict the rclone config section to one remote.
        ring: The buffer to take recent lines from. Defaults to :data:`RING`.
        include_logs: Embed the rotating log files.
        include_config: Embed ``config.json``.
        include_rclone: Run ``rclone config redacted`` and ``rclone version``.

    Returns:
        The path of the written zip.

    Raises:
        OSError: If the archive cannot be written.

    Never reads the rc ``config/dump`` or ``config/get`` endpoints, and never
    embeds ``endpoints.json`` — it holds the rc password (I14).
    """
    buffer = ring if ring is not None else RING
    if destination is None:
        stamp = utcnow_iso().replace(":", "").replace("-", "")
        destination = paths.cache_dir() / f"diagnostics-{stamp}.zip"
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    manifest: dict[str, Any] = {
        "app": APP_NAME,
        "version": __version__,
        "generated_at": utcnow_iso(),
        "members": [],
        "excluded": [
            "endpoints.json (holds the rc password — I14)",
            "rclone.conf (holds the OAuth refresh token in the clear — I14)",
            "rc config/dump and config/get (return the token in the clear — I14)",
        ],
    }

    members: list[tuple[str, str]] = [("report.txt", _environment_report())]
    members.append(("recent.log", buffer.text(BUNDLE_LOG_LINES) + "\n"))
    if include_config:
        members.append(("config.json", _config_snapshot()))
    if include_rclone:
        members.append(("rclone-config-redacted.ini",
                        rclone_config_redacted(rclone_path, remote)))
        members.append(("rclone-version.txt", _rclone_version(rclone_path)))

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            archive.writestr(name, body)
            manifest["members"].append(name)
        if include_logs:
            for log_path in _iter_log_files(paths.log_file()):
                try:
                    raw = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                name = f"logs/{log_path.name}"
                archive.writestr(name, redact(raw))
                manifest["members"].append(name)
        archive.writestr("manifest.json",
                         json.dumps(manifest, indent=2, ensure_ascii=False))
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - exotic filesystem
        pass
    return target


def bundle_text(bundle: Path | str) -> str:
    """Concatenate every text member of a bundle.

    Args:
        bundle: A zip produced by :func:`build_diagnostics_bundle`.

    Returns:
        Every member's decoded content, joined. Exists so a test — or a
        pre-upload check in the UI — can assert in one line that a bundle
        contains no credential.
    """
    chunks: list[str] = []
    with zipfile.ZipFile(bundle) as archive:
        for name in archive.namelist():
            with archive.open(name) as handle:
                chunks.append(
                    io.TextIOWrapper(handle, encoding="utf-8",
                                     errors="replace").read())
    return "\n".join(chunks)
