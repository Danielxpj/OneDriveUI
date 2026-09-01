"""``rclone.conf`` — the only place a backend option is ever allowed to live.

Invariant I1 exists because rclone hashes the set of *command-line* backend
overrides into the fs canonical name: add ``--onedrive-chunk-size 30M`` and
``onedrive:`` silently becomes ``onedrive{MxOuf}:``, which has a different VFS
cache directory. Every materialised file instantly reads as online-only and the
old tree is orphaned on disk forever. This machine already carries two such
trees. The fix is simply to write the option into ``rclone.conf`` instead, where
it produces no hash suffix — and :func:`set_backend_options` is the one function
in the codebase that does it.

Two properties of the writer are load-bearing:

* **The ``token`` line survives byte-identically.** ``rclone.conf`` holds the
  OAuth refresh token, and re-serialising the file through ``configparser`` would
  rewrite that line's spacing and — for a token containing a ``%`` — mangle it
  outright. The rewrite here is line-based: only the keys being set are touched.
* **The write is atomic and keeps the file's existing permissions.** rclone
  creates ``rclone.conf`` at ``0600``; a rewrite that widened that would publish
  the refresh token to every process on the box.

Reading is equally constrained. ``config/dump`` and ``config/get`` return the
refresh token in the clear (invariant I14) and are banned from the rc transport
entirely, so :func:`redacted_dump` reproduces ``rclone config redacted`` in pure
Python instead.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from onedriveui import paths
from onedriveui.atomicio import atomic_write_text
from onedriveui.constants import ONEDRIVE_CHUNK_MULTIPLE
from onedriveui.errors import ConfigError, SafetyRefusal
from onedriveui.models import AccountKind
from onedriveui.paths import FILE_MODE
from onedriveui.rc import _mode_of
from onedriveui.rc.guards import BACKEND_PREFIXES, BACKEND_PREFIX_EXEMPT
from onedriveui.units import parse_size

__all__ = [
    "REDACTION",
    "SENSITIVE_KEYS",
    "config_fingerprint",
    "drive_type",
    "raw_text",
    "read",
    "recommended_backend_options",
    "redacted_dump",
    "remote_type",
    "remotes",
    "set_backend_options",
]

log = logging.getLogger(__name__)

#: What `rclone config redacted` substitutes for a sensitive value. Verified
#: against rclone v1.75.0 on this machine.
REDACTION = "XXX"

#: The trailing line `rclone config redacted` appends to its own output.
REDACTED_FOOTER = "### Double check the config for sensitive info before posting publicly"

#: Every option name any of rclone v1.75.0's 69 providers marks `Sensitive` or
#: `IsPassword`, read out of `config/providers` on this machine. Reproducing the
#: exact set is what makes :func:`redacted_dump` equivalent to `rclone config
#: redacted` — for a `[onedrive]` section that means `token`, `drive_id` and
#: `client_secret` are replaced and `type`/`drive_type` are not.
SENSITIVE_KEYS: frozenset[str] = frozenset({
    "access_grant", "access_key_id", "access_token", "account", "account_id",
    "api_key", "api_password", "api_secret", "api_url", "app_id", "app_token",
    "apple_id", "application_credential_id", "application_credential_name",
    "application_credential_secret", "auth_token", "authorization",
    "base_folder_uuid", "bearer_token", "client_access_token",
    "client_certificate_password", "client_id", "client_refresh_token",
    "client_salted_key_pass", "client_secret", "client_uid", "cloud_name",
    "compartment", "config_credentials", "connection_string", "cookies",
    "deleted_id", "device_id", "domain", "drive_id", "email", "file_password",
    "folder_password", "host", "impersonate", "impersonate_admin", "key",
    "key_file_pass", "key_pem", "library_key", "link_password",
    "mailbox_password", "master_key", "master_keys", "mnemonic", "msi_client_id",
    "msi_mi_res_id", "msi_object_id", "namenode", "namespace", "otp_secret_key",
    "pass", "passphrase", "password", "password2", "permanent_token",
    "plex_password", "plex_token", "plex_username", "private_access_key",
    "private_key", "project_number", "public_key", "refresh_token",
    "resource_key", "root_folder_id", "root_folder_slug", "root_id", "sas_url",
    "secret", "secret_access_key", "service_account_credentials",
    "service_principal_name", "session_id", "session_token", "spn",
    "sse_customer_key", "sse_customer_key_base64", "sse_customer_key_md5",
    "sse_kms_key_id", "team_drive", "tenant", "tenant_domain", "tenant_id",
    "token", "trust_token", "url", "user", "user_id", "user_project",
    "username", "workspace_id",
})

#: The four backend options this application manages. Anything else in the
#: `[onedrive]` section belongs to the user or to rclone's own OAuth flow and is
#: never touched.
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")

#: I9: a Personal drive cannot delete versions and does not implement
#: `permanentDelete`, so these two must never be turned on there.
_PERSONAL_FORBIDDEN: frozenset[str] = frozenset({"no_versions", "hard_delete"})


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────

def _conf_path(path: Path | str | None = None) -> Path:
    """``$RCLONE_CONFIG`` else ``~/.config/rclone/rclone.conf``, unless overridden."""
    return Path(os.path.expanduser(str(path))) if path else paths.rclone_conf()


def raw_text(path: Path | str | None = None) -> str:
    """The config file verbatim.

    Args:
        path: Override for the config location. Defaults to
            :func:`onedriveui.paths.rclone_conf`.

    Returns:
        The file's exact text, or ``""`` when it does not exist yet — an
        unconfigured machine is a normal first-run state, not an error.

    Raises:
        ConfigError: The file exists but cannot be read or decoded.
    """
    target = _conf_path(path)
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc


def read(path: Path | str | None = None) -> dict[str, dict[str, str]]:
    """Parse ``rclone.conf`` into ``{section: {key: value}}``.

    A hand-rolled INI reader rather than ``configparser``, for two reasons: the
    OAuth ``token`` value is a JSON blob containing ``%`` and ``:``, which
    ``configparser``'s interpolation and delimiter handling both mangle; and the
    parse must never raise on a file rclone itself accepts.

    Args:
        path: Override for the config location.

    Returns:
        Sections in file order, with keys lower-cased (rclone is
        case-insensitive on option names) and values stripped of surrounding
        whitespace. Comment lines (``#`` or ``;``) and blanks are dropped.

    Raises:
        ConfigError: The file exists but cannot be read.
    """
    out: dict[str, dict[str, str]] = {}
    section: dict[str, str] | None = None
    for line in raw_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        header = _SECTION_RE.match(line)
        if header is not None:
            section = out.setdefault(header.group(1).strip(), {})
            continue
        if section is None:
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        section[key.strip().lower()] = value.strip()
    return out


def remotes(path: Path | str | None = None) -> list[str]:
    """Every configured remote name, in file order.

    Args:
        path: Override for the config location.

    Returns:
        Names without the trailing colon, e.g. ``["onedrive"]``.
    """
    return list(read(path).keys())


def remote_type(remote: str, path: Path | str | None = None) -> str:
    """The backend type of ``remote``.

    Args:
        remote: Remote name, with or without a trailing colon.
        path: Override for the config location.

    Returns:
        e.g. ``"onedrive"``, or ``""`` when the remote is not configured.
    """
    return read(path).get(remote.rstrip(":"), {}).get("type", "")


def drive_type(remote: str, path: Path | str | None = None) -> str:
    """The OneDrive ``drive_type`` of ``remote``.

    Args:
        remote: Remote name, with or without a trailing colon.
        path: Override for the config location.

    Returns:
        ``"personal"``, ``"business"``, ``"documentLibrary"``, or ``""`` when
        unknown. Invariant I9 keys off this: a Personal drive can neither delete
        versions nor hard-delete.
    """
    return read(path).get(remote.rstrip(":"), {}).get("drive_type", "")


def config_fingerprint(path: Path | str | None = None) -> str:
    """A stable digest of the config file's *content*.

    Used to notice that the user edited ``rclone.conf`` behind our back — a new
    ``drive_type`` or a changed ``chunk_size`` invalidates cached capabilities.

    Args:
        path: Override for the config location.

    Returns:
        Lower-case hex SHA-256 of the raw bytes, or the digest of the empty
        string when the file does not exist. Never contains any secret.
    """
    return hashlib.sha256(raw_text(path).encode("utf-8")).hexdigest()


def redacted_dump(path: Path | str | None = None) -> str:
    """The config file with every sensitive value replaced — invariant I14.

    Equivalent to ``rclone config redacted``, reproduced in Python so a
    diagnostics bundle never has to shell out and never has to touch
    ``config/dump`` or ``config/get``, both of which hand back the refresh token
    in the clear.

    Args:
        path: Override for the config location.

    Returns:
        The rendered config, sections in file order, with every key in
        :data:`SENSITIVE_KEYS` replaced by :data:`REDACTION`, plus rclone's own
        trailing warning line. Safe to attach to a bug report.
    """
    lines: list[str] = []
    for section, options in read(path).items():
        lines.append(f"[{section}]")
        for key, value in options.items():
            lines.append(f"{key} = {REDACTION if key in SENSITIVE_KEYS else value}")
        lines.append("")
    if not lines:
        lines = ["; empty config", ""]
    lines.append(REDACTED_FOOTER)
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Writing
# ─────────────────────────────────────────────────────────────────────────────

def _render_value(value: Any) -> str:
    """Format a Python value the way rclone writes it into ``rclone.conf``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate(remote: str, options: Mapping[str, Any], existing_drive_type: str) -> None:
    """Reject an option set that would break a hard invariant.

    Raises:
        SafetyRefusal: invariant ``"I9"`` for ``no_versions``/``hard_delete`` on
            a Personal drive.
        ConfigError: for a ``chunk_size`` Graph would reject.
    """
    personal = (existing_drive_type or "").lower() == AccountKind.PERSONAL.value
    for key, value in options.items():
        name = key.strip().lower()
        if personal and name in _PERSONAL_FORBIDDEN and _truthy(value):
            raise SafetyRefusal(
                "I9",
                f"{name}=true on {remote!r}, whose drive_type is 'personal': a "
                f"Personal drive cannot delete versions and does not implement "
                f"permanentDelete",
            )
        if name == "chunk_size" and value is not None:
            _validate_chunk_size(value)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return bool(value)


def _validate_chunk_size(value: Any) -> None:
    """Graph requires a resumable-upload chunk that is a multiple of 320 KiB.

    This is an API requirement, not an rclone preference: a chunk of any other
    size is rejected outright by the upload session.
    """
    try:
        size = parse_size(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"chunk_size {value!r} is not a size") from exc
    if size <= 0 or size % ONEDRIVE_CHUNK_MULTIPLE:
        raise ConfigError(
            f"chunk_size {value!r} ({size} bytes) is not a positive multiple of "
            f"{ONEDRIVE_CHUNK_MULTIPLE} (320 KiB), which Microsoft Graph requires"
        )


def _section_bounds(lines: list[str], remote: str) -> tuple[int, int]:
    """``(first_line_after_the_header, one_past_the_last_line)`` for ``[remote]``.

    Raises:
        ConfigError: The section is not in the file.
    """
    start = -1
    for index, line in enumerate(lines):
        header = _SECTION_RE.match(line)
        if header is not None and header.group(1).strip() == remote:
            start = index + 1
            break
    if start < 0:
        raise ConfigError(
            f"remote {remote!r} is not in the rclone config; run the sign-in flow "
            f"before setting backend options on it")
    end = len(lines)
    for index in range(start, len(lines)):
        if _SECTION_RE.match(lines[index]) is not None:
            end = index
            break
    return start, end


def set_backend_options(remote: str, options: Mapping[str, Any], *,
                        path: Path | str | None = None) -> Path:
    """Write backend options into ``rclone.conf``. **The only way this is done.**

    Invariant I1: a backend option on a command line renames the fs to
    ``<backend>{HASH}:``, which relocates the entire VFS cache and turns every
    materialised file online-only. Options set here produce no hash suffix.

    The rewrite is line-based, so every line the caller did not name — the
    ``token`` blob above all — comes through byte for byte. The file is replaced
    atomically at its existing permissions.

    Args:
        remote: Remote name, with or without a trailing colon. It must already
            exist: creating a remote is the sign-in flow's job, not this one's.
        options: ``{option_name: value}``. ``bool`` renders as
            ``true``/``false``; ``None`` **removes** the option. An empty mapping
            is a no-op that still returns the path.
        path: Override for the config location.

    Returns:
        The config path.

    Raises:
        ConfigError: ``remote`` is not configured, or ``chunk_size`` is not a
            positive multiple of 320 KiB.
        SafetyRefusal: invariant ``"I9"`` — ``no_versions`` or ``hard_delete``
            set true on a ``drive_type=personal`` remote.
        OSError: The config directory is unwritable.
    """
    name = remote.rstrip(":")
    target = _conf_path(path)
    text = raw_text(target)
    if not text.strip():
        raise ConfigError(
            f"{target} is empty or missing; remote {name!r} cannot be configured")

    _validate(name, options, drive_type(name, target))
    if not options:
        return target

    # splitlines() drops the information about a trailing newline, so remember it.
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    start, end = _section_bounds(lines, name)
    dropped: set[int] = set()

    for key, value in options.items():
        option = key.strip().lower()
        rendered = None if value is None else f"{option} = {_render_value(value)}"
        replaced = False
        for index in range(start, end):
            match = _KEY_RE.match(lines[index])
            if match is None or match.group(1).lower() != option:
                continue
            if rendered is None:
                dropped.add(index)
            else:
                lines[index] = rendered
            replaced = True
            break
        if replaced or rendered is None:
            continue
        # New option: append at the end of the section, before any trailing blank
        # lines, so the file keeps its blank-line-between-sections shape.
        insert = end
        while insert > start and not lines[insert - 1].strip():
            insert -= 1
        lines.insert(insert, rendered)
        dropped = {i if i < insert else i + 1 for i in dropped}
        end += 1

    kept = [line for index, line in enumerate(lines) if index not in dropped]
    out = "\n".join(kept) + ("\n" if trailing_newline or kept else "")
    written = atomic_write_text(target, out, mode=_mode_of(target, FILE_MODE))
    log.info("rclone.conf: set %s on [%s]", ",".join(sorted(options)), name)
    return written


def recommended_backend_options(kind: str | AccountKind) -> dict[str, str]:
    """The backend options OneDriveUI wants on a fresh remote.

    Each one is a decision, not a default:

    ``chunk_size = 10M``
        A multiple of Graph's mandatory 320 KiB. Chunks are buffered in RAM, so
        the real cost is ``chunk_size × transfers``; 10M × 4 is 40 MiB, which is
        the largest sensible figure for a desktop client.
    ``delta = true``
        Flips ``ListR`` on, which is what makes a listing of the whole drive one
        request instead of one per directory. It only works at the drive root —
        which is exactly where the mount is rooted.
    ``no_versions = false`` and ``hard_delete = false``
        Invariant I9. A Personal drive can neither delete versions nor
        permanently delete, and asking it to fails the whole operation. They are
        pinned off for Business too, because our own recycle bin and version
        history are what the UI exposes.

    Args:
        kind: ``"personal"`` or ``"business"``, or an
            :class:`~onedriveui.models.AccountKind`.

    Returns:
        ``{option: value}`` with values already rendered as ``rclone.conf``
        writes them, ready to hand straight to :func:`set_backend_options`.

    Raises:
        ValueError: ``kind`` is neither of the two account kinds.
    """
    resolved = AccountKind(str(getattr(kind, "value", kind)).lower())
    options = {
        "chunk_size": "10M",
        "delta": "true",
        "no_versions": "false",
        "hard_delete": "false",
    }
    log.debug("recommended backend options for %s: %s", resolved.value, options)
    return options


def missing_options(remote: str, wanted: Mapping[str, Any],
                    path: Path | str | None = None) -> dict[str, Any]:
    """Which of ``wanted`` are absent or different in ``rclone.conf``.

    Lets the caller skip a rewrite — and therefore an atomic replace of a file
    holding the OAuth token — when nothing would change.

    Args:
        remote: Remote name, with or without a trailing colon.
        wanted: The options that should be present.
        path: Override for the config location.

    Returns:
        The subset of ``wanted`` that is not already set to that value.
    """
    current = read(path).get(remote.rstrip(":"), {})
    out: dict[str, Any] = {}
    for key, value in wanted.items():
        option = key.strip().lower()
        if value is None:
            if option in current:
                out[key] = None
            continue
        if current.get(option) != _render_value(value):
            out[key] = value
    return out


def assert_no_backend_env(env: Iterable[str] | Mapping[str, Any]) -> None:
    """Invariant I1, for the environment rather than the command line.

    ``RCLONE_ONEDRIVE_CHUNK_SIZE=30M`` in a unit's ``Environment=`` produces the
    identical ``{HASH}`` rename that a ``--onedrive-chunk-size`` flag does, so a
    unit file is checked for it before being written.

    Args:
        env: Variable names, or a mapping whose keys are variable names.

    Raises:
        SafetyRefusal: invariant ``"I1"``.
    """
    names = env.keys() if isinstance(env, Mapping) else env
    for name in names:
        upper = str(name).upper()
        if not upper.startswith("RCLONE_"):
            continue
        tail = upper[len("RCLONE_"):].lower()
        if f"--{tail.replace('_', '-')}" in BACKEND_PREFIX_EXEMPT:
            continue        # RCLONE_CACHE_DIR / RCLONE_HTTP_PROXY are global
        prefix = tail.split("_", 1)[0]
        if "_" in tail and prefix in BACKEND_PREFIXES:
            raise SafetyRefusal(
                "I1",
                f"{name} is a backend option in the environment; it renames the "
                f"fs to '{prefix}{{HASH}}:' exactly as a --{prefix}-* flag does",
            )
