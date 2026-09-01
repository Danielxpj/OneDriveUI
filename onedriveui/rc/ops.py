"""Typed wrappers over ``operations/*``, ``core/version`` and ``core/bwlimit``.

Every function here exists because the raw endpoint has a trap in it. The seven
that matter, all measured against rclone v1.75.0 on this machine
(``docs/research/rclone-rc-api.md`` §5, ``rclone-onedrive-backend.md`` §11):

1. **``Path`` is relative to ``fs``, never to ``fs``+``remote``.**
   ``{"fs": "onedrive:", "remote": "AFC"}`` answers ``Path: "AFC/Representación"``
   with ``Name: "Representación"``. Prefixing ``remote`` onto ``Path`` yields
   ``AFC/AFC/…``; :func:`rel_path_for` builds the row from ``Name`` and takes
   only the *extra* sub-directory a recursive walk added.
2. **The two failure conventions disagree.** ``operations/stat`` on a missing
   path answers **HTTP 200** ``{"item": null}``, while ``operations/list`` on a
   missing directory answers **HTTP 404**. :func:`stat` returns ``None``;
   :func:`list_dir` lets the :class:`~onedriveui.errors.RcError` out with
   ``is_not_found`` set.
3. **``operations/uploadfile`` takes its parameters in the QUERY STRING** — its
   body is the multipart payload — **and names the destination from the part's
   ``filename=``**, not from the field name. It is the one endpoint that cannot
   go through :func:`~onedriveui.rc.client.call_blocking`.
4. **OneDrive directories report ``Size: -1``.** Render blank, never "-1 bytes".
   :class:`~onedriveui.models.RemoteFolderNode` defaults to ``-1`` for that.
5. **``ListR`` is false on OneDrive**, so ``operations/size`` and
   ``operations/check`` walk the tree one Graph request per directory and can
   run for minutes. :func:`size` and :func:`check` are therefore **always**
   ``_async`` and return a :class:`~onedriveui.models.JobHandle`.
6. **``Capabilities.name`` can carry a ``{HASH}`` suffix.** Queried through a
   daemon whose backend was overridden on the command line, ``fsinfo`` answers
   ``Name: "onedrive{MxOuf}"`` — which is exactly what this machine's live mount
   reports. :func:`strip_hash_suffix` removes it before display.
7. **``settier`` and ``backend/command`` are unsupported on OneDrive** and
   ``publiclink``'s ``unlink`` flag is a verified no-op that still returns a URL.
   Gate every optional affordance on :func:`capabilities`, never on a
   backend-name check.

Threading (ARCHITECTURE §7.6). Everything here is **synchronous** and belongs on
an ``IOPool`` worker; none of it may be called from the GUI thread. A GUI-thread
caller issues ``RcClient.call()`` itself and feeds the reply to the matching
``parse_*`` function, which is pure and does no I/O.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import re
import secrets
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import (
    Capabilities,
    JobHandle,
    QuotaInfo,
    RcEndpoint,
    RemoteFolderNode,
    utcnow_iso,
)
from onedriveui.rc import guards
from onedriveui.rc.client import build_params, call_blocking

__all__ = [
    "BwLimit",
    "Capabilities",
    "CheckResult",
    "SizeResult",
    "UPLOAD_TIMEOUT_S",
    "VersionInfo",
    "about",
    "capabilities",
    "check",
    "copyfile",
    "core_version",
    "deletefile",
    "get_bwlimit",
    "hashsum",
    "hashsumfile",
    "invalidate_capabilities",
    "list_dir",
    "mkdir",
    "movefile",
    "node_from_row",
    "parse_about",
    "parse_bwlimit",
    "parse_check",
    "parse_fsinfo",
    "parse_list",
    "parse_size",
    "parse_stat",
    "parse_version",
    "publiclink",
    "purge",
    "rel_path_for",
    "rmdir",
    "rmdirs",
    "set_bwlimit",
    "settier",
    "size",
    "stat",
    "strip_hash_suffix",
    "supports",
    "uploadfile",
]

log = logging.getLogger(__name__)

#: How long an ``operations/uploadfile`` may take before the socket is torn
#: down. Mirrors rclone's own ``Timeout`` default (5 minutes) rather than the
#: 4 s GUI budget: this is a real upload on an ``IOPool`` thread.
UPLOAD_TIMEOUT_S: Final[float] = 300.0

#: rclone appends a hash of the backend options that did **not** come from
#: ``rclone.conf`` to the fs name — ``onedrive{MxOuf}:``. It shows up in
#: ``vfs/list``, in the cache paths and in ``operations/fsinfo``'s ``Name``.
_HASH_SUFFIX_RE = re.compile(r"\{[^{}]*\}")

#: One ``hashsum`` output line: the hash, two spaces, then the name (which may
#: itself contain spaces, so the split is bounded).
_HASHSUM_LINE_RE = re.compile(r"^(\S+)\s\s?(.*)$")

#: What a size means when it is not known. OneDrive answers ``-1`` for every
#: directory and the model default matches, so this is only reached when a row
#: omits ``Size`` entirely.
_UNKNOWN_SIZE: Final[int] = -1

#: Capability cache, keyed by daemon identity **and** ``executeId`` so a
#: restarted daemon can never serve a stale answer.
_CAPABILITY_CACHE: dict[tuple[str, int, str, str], Capabilities] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Small typed results for the endpoints models.py does not cover
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SizeResult:
    """``operations/size``. ``sizeless`` counts objects of unknown length."""

    bytes: int = 0
    count: int = 0
    sizeless: int = 0


@dataclass(frozen=True, slots=True)
class CheckResult:
    """``operations/check``.

    ``combined`` uses rclone's prefixes: ``=`` match, ``+`` missing on the
    destination, ``-`` missing on the source, ``*`` differ, ``!`` error.
    """

    success: bool = False
    status: str = ""
    hash_type: str = ""
    combined: tuple[str, ...] = ()
    match: tuple[str, ...] = ()
    differ: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    missing_on_src: tuple[str, ...] = ()
    missing_on_dst: tuple[str, ...] = ()

    @property
    def differences(self) -> int:
        """How many rows are not a clean match."""
        return len(self.differ) + len(self.missing_on_src) + len(self.missing_on_dst)


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """``core/version``. Gate features on :meth:`at_least`, never on the string."""

    version: str = ""
    decomposed: tuple[int, ...] = ()
    os: str = ""
    arch: str = ""
    go_version: str = ""
    is_beta: bool = False

    def at_least(self, *parts: int) -> bool:
        """Is this rclone at least ``parts``?

        Args:
            *parts: The version to compare against, e.g. ``at_least(1, 75, 0)``.

        Returns:
            True when ``decomposed`` is greater than or equal to ``parts``.
            False when the daemon reported no ``decomposed`` array at all, which
            is the safe answer for a build too old to be trusted.
        """
        if not self.decomposed:
            return False
        wanted = tuple(int(p) for p in parts)
        seen = self.decomposed[:len(wanted)]
        seen = seen + (0,) * (len(wanted) - len(seen))
        return seen >= wanted


@dataclass(frozen=True, slots=True)
class BwLimit:
    """``core/bwlimit``. ``tx`` is **upload**, ``rx`` is **download**.

    ``rate`` comes back normalised to binary units (``1M:100k`` is echoed as
    ``1Mi:100Ki``), so it may never be string-compared with what was sent.
    """

    rate: str = "off"
    bytes_per_second: int = -1
    tx: int = -1
    rx: int = -1

    @property
    def unlimited(self) -> bool:
        """True when neither direction is throttled."""
        return self.tx < 0 and self.rx < 0


# ─────────────────────────────────────────────────────────────────────────────
# Coercion helpers — every rc field is read with .get()
# ─────────────────────────────────────────────────────────────────────────────

def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    return bool(value)


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _call(ep: RcEndpoint, path: str, params: Mapping[str, Any] | None = None,
          timeout_s: float | None = None) -> dict[str, Any]:
    """One blocking rc call, with ``call_blocking``'s own default timeout.

    The timeout is passed through only when the caller named one, so this module
    never restates the 30 s default that ``rc.client`` owns.
    """
    body = dict(params or {})
    if timeout_s is None:
        return call_blocking(ep, path, body)
    return call_blocking(ep, path, body, timeout_s=timeout_s)


# ─────────────────────────────────────────────────────────────────────────────
# Names and paths — the `Path`-relative-to-`fs` trap
# ─────────────────────────────────────────────────────────────────────────────

def strip_hash_suffix(name: str) -> str:
    """Remove rclone's ``{HASH}`` config-hash suffix from an fs or backend name.

    Args:
        name: ``"onedrive{MxOuf}"``, ``"onedrive{MxOuf}:"`` or a plain name.

    Returns:
        The name without the brace group, colon preserved. A name with no
        suffix is returned unchanged.

    The suffix is rclone's hash of the backend options that did not come from
    ``rclone.conf``. This machine's live mount reports ``onedrive{MxOuf}:``
    because it was started with ``--onedrive-chunk-size 30M`` — the exact
    failure invariant I1 exists to prevent. It must never reach the UI.
    """
    return _HASH_SUFFIX_RE.sub("", _str(name))


def rel_path_for(remote: str, row: Mapping[str, Any]) -> str:
    """Build a row's path **relative to ``fs``**, from ``Name``.

    Args:
        remote: The ``remote`` the listing was requested with — the caller's own
            current directory, relative to ``fs``.
        row: One ``operations/list`` entry.

    Returns:
        The POSIX path relative to ``fs``, with no leading slash.

    ``Path`` in the response is relative to ``fs``, **not** to ``fs``+``remote``:
    listing ``{"fs": "onedrive:", "remote": "AFC"}`` returns
    ``Path: "AFC/Representación"``. Joining ``remote`` onto that produces
    ``AFC/AFC/Representación``, which is the bug this function exists to make
    impossible. The row is therefore built from ``Name``, and ``Path`` is
    consulted for one thing only: the extra sub-directory that a ``recurse``
    listing walked into. A backend that answered a ``Path`` relative to
    ``remote`` instead would still come out correct.
    """
    base = _str(remote).strip("/")
    name = _str(row.get("Name")).strip("/")
    path = _str(row.get("Path")).strip("/")
    if not name:
        name = path.rpartition("/")[2]
    parent = path.rpartition("/")[0] if path else ""
    if base and not (parent == base or parent.startswith(f"{base}/")):
        # `Path` did not agree with the directory we asked for. Ours wins.
        parent = base
    return f"{parent}/{name}" if parent else name


def node_from_row(row: Mapping[str, Any], remote: str = "") -> RemoteFolderNode:
    """Turn one ``operations/list``/``operations/stat`` entry into a node.

    Args:
        row: The raw entry.
        remote: The directory the listing was requested with.

    Returns:
        A :class:`~onedriveui.models.RemoteFolderNode`. ``size`` stays ``-1``
        for a directory, which is what OneDrive itself reports and what the UI
        renders as blank.

    ``Metadata`` costs no extra API call and carries exactly the columns the
    Windows client shows: ``created-by-display-name``,
    ``last-modified-by-display-name`` and ``malware-detected`` (the "blocked
    file" state).
    """
    meta = row.get("Metadata")
    meta = meta if isinstance(meta, Mapping) else {}
    hashes = row.get("Hashes")
    hashes = hashes if isinstance(hashes, Mapping) else {}
    rel_path = rel_path_for(remote, row)
    return RemoteFolderNode(
        rel_path=rel_path,
        name=_str(row.get("Name")) or rel_path.rpartition("/")[2],
        is_dir=_bool(row.get("IsDir")),
        size=_int(row.get("Size", _UNKNOWN_SIZE), _UNKNOWN_SIZE),
        mod_time=_str(row.get("ModTime")),
        mime_type=_str(row.get("MimeType")),
        item_id=_str(row.get("ID")),
        quickxor=_str(hashes.get("quickxor")),
        created_by=_str(meta.get("created-by-display-name")),
        modified_by=_str(meta.get("last-modified-by-display-name")),
        malware_detected=_str(meta.get("malware-detected")).lower() == "true",
        children_loaded=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pure parsers — safe on the GUI thread, for an async RcClient reply
# ─────────────────────────────────────────────────────────────────────────────

def parse_about(body: Mapping[str, Any] | None,
                sampled_at: str = "") -> QuotaInfo:
    """``operations/about`` → :class:`~onedriveui.models.QuotaInfo`.

    Args:
        body: The response. Every key is optional per backend — the local
            backend omits ``trashed``, OneDrive omits ``other`` and ``objects``
            — so all four are read with ``.get()``.
        sampled_at: The observation stamp. Defaults to now.

    Returns:
        The quota. ``trashed`` reads ``0`` on OneDrive Personal even though the
        web recycle bin is real, so it must never be rendered as a storage tile.
    """
    data = body or {}
    return QuotaInfo(
        total=_int(data.get("total")),
        used=_int(data.get("used")),
        free=_int(data.get("free")),
        trashed=_int(data.get("trashed")),
        sampled_at=sampled_at or utcnow_iso(),
    )


def parse_list(body: Mapping[str, Any] | None,
               remote: str = "") -> list[RemoteFolderNode]:
    """``operations/list`` → nodes, built from ``Name`` (see :func:`rel_path_for`)."""
    rows = (body or {}).get("list")
    if not isinstance(rows, (list, tuple)):
        return []
    return [node_from_row(row, remote) for row in rows
            if isinstance(row, Mapping)]


def parse_stat(body: Mapping[str, Any] | None,
               remote: str = "") -> RemoteFolderNode | None:
    """``operations/stat`` → a node, or ``None`` for a missing path.

    ``{"item": null}`` arrives with **HTTP 200**, so absence is a value here and
    an exception in :func:`list_dir`. That asymmetry is rclone's, not ours.
    """
    item = (body or {}).get("item")
    if not isinstance(item, Mapping):
        return None
    node = node_from_row(item, "")
    rel = _str(remote).strip("/") or node.rel_path
    if rel == node.rel_path:
        return node
    return RemoteFolderNode(
        rel_path=rel, name=node.name, is_dir=node.is_dir, size=node.size,
        mod_time=node.mod_time, mime_type=node.mime_type, item_id=node.item_id,
        quickxor=node.quickxor, created_by=node.created_by,
        modified_by=node.modified_by, malware_detected=node.malware_detected,
        children_loaded=node.children_loaded,
    )


def parse_fsinfo(body: Mapping[str, Any] | None) -> Capabilities:
    """``operations/fsinfo`` → :class:`~onedriveui.models.Capabilities`.

    ``Name`` is stripped of any ``{HASH}`` suffix (§ module docstring, trap 6).
    Every feature the backend did not report is absent from ``features`` and
    therefore reads ``False`` through :meth:`Capabilities.has`.
    """
    data = body or {}
    features = data.get("Features")
    features = features if isinstance(features, Mapping) else {}
    return Capabilities(
        name=strip_hash_suffix(_str(data.get("Name"))),
        root=_str(data.get("Root")),
        precision_ns=_int(data.get("Precision")),
        hashes=_strings(data.get("Hashes")),
        features={str(k): bool(v) for k, v in features.items()},
    )


def parse_version(body: Mapping[str, Any] | None) -> VersionInfo:
    """``core/version`` → :class:`VersionInfo`."""
    data = body or {}
    raw = data.get("decomposed")
    decomposed = tuple(_int(part) for part in raw) if isinstance(
        raw, (list, tuple)) else ()
    return VersionInfo(
        version=_str(data.get("version")),
        decomposed=decomposed,
        os=_str(data.get("os")),
        arch=_str(data.get("arch")),
        go_version=_str(data.get("goVersion")),
        is_beta=_bool(data.get("isBeta")),
    )


def parse_bwlimit(body: Mapping[str, Any] | None) -> BwLimit:
    """``core/bwlimit`` → :class:`BwLimit`. ``Tx`` is upload, ``Rx`` download."""
    data = body or {}
    return BwLimit(
        rate=_str(data.get("rate"), "off"),
        bytes_per_second=_int(data.get("bytesPerSecond"), -1),
        tx=_int(data.get("bytesPerSecondTx"), -1),
        rx=_int(data.get("bytesPerSecondRx"), -1),
    )


def parse_size(body: Mapping[str, Any] | None) -> SizeResult:
    """``operations/size`` output → :class:`SizeResult`.

    Feed it ``job/status.output``: :func:`size` is always ``_async``.
    """
    data = body or {}
    return SizeResult(
        bytes=_int(data.get("bytes")),
        count=_int(data.get("count")),
        sizeless=_int(data.get("sizeless")),
    )


def parse_check(body: Mapping[str, Any] | None) -> CheckResult:
    """``operations/check`` output → :class:`CheckResult`."""
    data = body or {}
    return CheckResult(
        success=_bool(data.get("success")),
        status=_str(data.get("status")),
        hash_type=_str(data.get("hashType")),
        combined=_strings(data.get("combined")),
        match=_strings(data.get("match")),
        differ=_strings(data.get("differ")),
        errors=_strings(data.get("error")),
        missing_on_src=_strings(data.get("missingOnSrc")),
        missing_on_dst=_strings(data.get("missingOnDst")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

def about(fs: str, *, ep: RcEndpoint,
          timeout_s: float | None = None) -> QuotaInfo:
    """Storage quota for ``fs``.

    Args:
        fs: The remote, e.g. ``"onedrive:"``.
        ep: The daemon to ask.
        timeout_s: Socket timeout; ``rc.client``'s default when omitted.

    Returns:
        The quota, sampled now. This drives the Windows-style storage ring,
        which is drawn from ``used``/``total``.

    Raises:
        RcError: The backend does not implement ``About``, or the token is
            dead — this is also the cheapest token probe there is, which is
            exactly what :func:`onedriveui.rc.auth.probe_token` uses it for.
        DaemonUnavailable: The daemon did not answer.
    """
    return parse_about(_call(ep, "operations/about", {"fs": fs}, timeout_s))


def list_dir(fs: str, remote: str = "", *, ep: RcEndpoint,
             dirs_only: bool = False, files_only: bool = False,
             recurse: bool = False, metadata: bool = True,
             no_mod_time: bool = False, no_mime_type: bool = False,
             show_hash: bool = False, hash_types: Sequence[str] = (),
             timeout_s: float | None = None) -> list[RemoteFolderNode]:
    """One directory listing, as nodes whose ``rel_path`` is relative to ``fs``.

    Args:
        fs: The remote, e.g. ``"onedrive:"``.
        remote: The directory inside ``fs``, e.g. ``"Docs"``. ``""`` is the root.
        ep: The daemon to ask.
        dirs_only: Only directories — what the folder picker wants.
        files_only: Only files.
        recurse: Walk the whole subtree. **Expensive on OneDrive**, which has
            ``ListR: false``: one Graph request per directory.
        metadata: Ask for ``Metadata``. It costs no extra API call and carries
            "Modified by" and the malware flag.
        no_mod_time: Skip the modtime lookup.
        no_mime_type: Omit ``MimeType``.
        show_hash: Include a ``Hashes`` object.
        hash_types: Which hashes, e.g. ``("quickxor",)``. Only with ``show_hash``.
        timeout_s: Socket timeout.

    Returns:
        The rows. ``rel_path`` is ``<remote>/<Name>``, built by
        :func:`rel_path_for`, never by prefixing ``remote`` onto ``Path``.

    Raises:
        RcError: HTTP 404 with ``is_not_found`` when the directory does not
            exist. Note that :func:`stat` reports absence as ``None`` at HTTP
            200 instead — the two endpoints genuinely disagree.
        DaemonUnavailable: The daemon did not answer.
    """
    opt: dict[str, Any] = {}
    if dirs_only:
        opt["dirsOnly"] = True
    if files_only:
        opt["filesOnly"] = True
    if recurse:
        opt["recurse"] = True
    if metadata:
        opt["metadata"] = True
    if no_mod_time:
        opt["noModTime"] = True
    if no_mime_type:
        opt["noMimeType"] = True
    if show_hash:
        opt["showHash"] = True
        if hash_types:
            opt["hashTypes"] = list(hash_types)
    params: dict[str, Any] = {"fs": fs, "remote": _str(remote).strip("/")}
    if opt:
        params["opt"] = opt
    body = _call(ep, "operations/list", params, timeout_s)
    return parse_list(body, params["remote"])


def stat(fs: str, remote: str, *, ep: RcEndpoint, metadata: bool = True,
         files_only: bool = False,
         timeout_s: float | None = None) -> RemoteFolderNode | None:
    """One object, or ``None`` when it does not exist.

    Args:
        fs: The remote.
        remote: The path inside ``fs``.
        ep: The daemon to ask.
        metadata: Ask for ``Metadata``.
        files_only: Skip the directory lookup — much faster when a file is all
            you care about.
        timeout_s: Socket timeout.

    Returns:
        The node, or ``None``.

    A missing path is **HTTP 200 ``{"item": null}``**, not an error, so this
    never raises for absence. ``operations/list`` answers 404 for the same
    situation; both behaviours are reproduced faithfully rather than smoothed
    over, because callers need to tell "gone" from "unreachable".
    """
    opt: dict[str, Any] = {}
    if metadata:
        opt["metadata"] = True
    if files_only:
        opt["filesOnly"] = True
    params: dict[str, Any] = {"fs": fs, "remote": _str(remote).strip("/")}
    if opt:
        params["opt"] = opt
    return parse_stat(_call(ep, "operations/stat", params, timeout_s),
                      params["remote"])


def capabilities(fs: str, *, ep: RcEndpoint, use_cache: bool = True,
                 timeout_s: float | None = None) -> Capabilities:
    """Feature-detect ``fs`` with ``operations/fsinfo``.

    Args:
        fs: The remote.
        ep: The daemon to ask.
        use_cache: Reuse the answer for this ``(daemon, executeId, fs)``.
            Features cannot change under a running daemon, and a restart brings
            a new ``executeId``, so the cache can never go stale.
        timeout_s: Socket timeout.

    Returns:
        The capabilities, with ``name`` already stripped of any ``{HASH}``.

    **Never branch on the backend name.** ``PublicLink`` gates Share,
    ``ChangeNotify`` gates live refresh, ``CleanUp`` gates version pruning,
    ``CaseInsensitive`` gates the rename-collision warning, ``ListR`` says
    whether a recursive walk is affordable (it is not, on OneDrive), and
    ``Hashes`` names the only hash a local comparison can use (``quickxor``).
    """
    key = (ep.host, int(ep.port), ep.execute_id, fs)
    if use_cache:
        cached = _CAPABILITY_CACHE.get(key)
        if cached is not None:
            return cached
    caps = parse_fsinfo(_call(ep, "operations/fsinfo", {"fs": fs}, timeout_s))
    _CAPABILITY_CACHE[key] = caps
    return caps


def invalidate_capabilities(ep: RcEndpoint | None = None) -> int:
    """Forget cached :func:`capabilities`.

    Args:
        ep: Drop only this daemon's entries. ``None`` drops everything.

    Returns:
        How many entries were dropped.
    """
    if ep is None:
        count = len(_CAPABILITY_CACHE)
        _CAPABILITY_CACHE.clear()
        return count
    doomed = [key for key in _CAPABILITY_CACHE
              if key[0] == ep.host and key[1] == int(ep.port)]
    for key in doomed:
        _CAPABILITY_CACHE.pop(key, None)
    return len(doomed)


def supports(fs: str, feature: str, *, ep: RcEndpoint,
             use_cache: bool = True) -> bool:
    """Is ``feature`` available on ``fs``?

    Args:
        fs: The remote.
        feature: A ``Features`` key, e.g. ``"PublicLink"``, ``"ListR"``.
        ep: The daemon to ask.
        use_cache: See :func:`capabilities`.

    Returns:
        The flag, or ``False`` when the probe itself failed — the safe answer
        for gating an optional affordance.
    """
    try:
        return capabilities(fs, ep=ep, use_cache=use_cache).has(feature)
    except (RcError, OSError) as exc:
        log.info("fsinfo probe for %s on %s failed (%s); assuming unsupported",
                 feature, fs, exc)
        return False


def core_version(*, ep: RcEndpoint,
                 timeout_s: float | None = None) -> VersionInfo:
    """``core/version``. Gate on :meth:`VersionInfo.at_least`, not the string."""
    return parse_version(_call(ep, "core/version", {}, timeout_s))


def hashsum(fs: str, *, ep: RcEndpoint, hash_type: str = "quickxor",
            download: bool = False, base64: bool = False,
            timeout_s: float | None = None) -> dict[str, str]:
    """Hash every object under ``fs``.

    Args:
        fs: The remote, which may include a path (``"onedrive:Docs"``).
        ep: The daemon to ask.
        hash_type: ``"quickxor"`` is the only hash OneDrive implements; the
            local backend implements it too, which is what makes a cheap
            local-versus-remote comparison possible at all.
        download: Compute by downloading. Very slow; only for a backend that
            cannot hash server-side.
        base64: Return base64 rather than hex.
        timeout_s: Socket timeout.

    Returns:
        ``{name: hash}``. rclone answers ``["<hash>  <name>", …]``; the two-space
        separator is fixed, and a name may itself contain spaces.
    """
    body = _call(ep, "operations/hashsum", {
        "fs": fs, "hashType": hash_type,
        "download": bool(download), "base64": bool(base64)}, timeout_s)
    out: dict[str, str] = {}
    for line in body.get("hashsum") or []:
        match = _HASHSUM_LINE_RE.match(_str(line))
        if match is not None:
            out[match.group(2)] = match.group(1)
    return out


def hashsumfile(fs: str, remote: str, *, ep: RcEndpoint,
                hash_type: str = "quickxor", download: bool = False,
                base64: bool = False,
                timeout_s: float | None = None) -> str:
    """One object's hash, or ``""`` when the backend could not produce it."""
    body = _call(ep, "operations/hashsumfile", {
        "fs": fs, "remote": _str(remote).strip("/"), "hashType": hash_type,
        "download": bool(download), "base64": bool(base64)}, timeout_s)
    return _str(body.get("hash"))


# ─────────────────────────────────────────────────────────────────────────────
# Mutations
# ─────────────────────────────────────────────────────────────────────────────

def mkdir(fs: str, remote: str, *, ep: RcEndpoint,
          timeout_s: float | None = None) -> None:
    """Create a directory. Answers ``{}``; creating an existing one is benign."""
    _call(ep, "operations/mkdir",
          {"fs": fs, "remote": _str(remote).strip("/")}, timeout_s)


def rmdir(fs: str, remote: str, *, ep: RcEndpoint,
          timeout_s: float | None = None) -> None:
    """Remove an **empty** directory. Use :func:`purge` for a populated one."""
    _call(ep, "operations/rmdir",
          {"fs": fs, "remote": _str(remote).strip("/")}, timeout_s)


def rmdirs(fs: str, remote: str, *, ep: RcEndpoint, leave_root: bool = True,
           timeout_s: float | None = None) -> None:
    """Remove every empty directory under ``remote``.

    Args:
        fs: The remote.
        remote: The subtree.
        ep: The daemon to ask.
        leave_root: Keep ``remote`` itself even when it ends up empty.
        timeout_s: Socket timeout.
    """
    _call(ep, "operations/rmdirs",
          {"fs": fs, "remote": _str(remote).strip("/"),
           "leaveRoot": bool(leave_root)}, timeout_s)


def purge(fs: str, remote: str, *, ep: RcEndpoint,
          timeout_s: float | None = None) -> None:
    """Delete a directory **and everything in it**, server-side.

    OneDrive reports ``Purge: true``, so this is one Graph call rather than a
    recursive walk. It is irreversible from our side: pair it with
    ``sync.trashbin`` when the deletion came from our own UI.
    """
    _call(ep, "operations/purge",
          {"fs": fs, "remote": _str(remote).strip("/")}, timeout_s)


def deletefile(fs: str, remote: str, *, ep: RcEndpoint,
               timeout_s: float | None = None) -> None:
    """Delete one file."""
    _call(ep, "operations/deletefile",
          {"fs": fs, "remote": _str(remote).strip("/")}, timeout_s)


def copyfile(src_fs: str, src_remote: str, dst_fs: str, dst_remote: str, *,
             ep: RcEndpoint, timeout_s: float | None = None) -> None:
    """Copy one file. Server-side when both sides are the same OneDrive remote."""
    _call(ep, "operations/copyfile", {
        "srcFs": src_fs, "srcRemote": _str(src_remote).strip("/"),
        "dstFs": dst_fs, "dstRemote": _str(dst_remote).strip("/")}, timeout_s)


def movefile(src_fs: str, src_remote: str, dst_fs: str, dst_remote: str, *,
             ep: RcEndpoint, timeout_s: float | None = None) -> None:
    """Move or rename one file.

    A rename is this call with ``src_fs == dst_fs``. OneDrive reports
    ``Move: true``, so it is server-side and instant — no bytes move.
    """
    _call(ep, "operations/movefile", {
        "srcFs": src_fs, "srcRemote": _str(src_remote).strip("/"),
        "dstFs": dst_fs, "dstRemote": _str(dst_remote).strip("/")}, timeout_s)


def publiclink(fs: str, remote: str, *, ep: RcEndpoint,
               expire: str | None = None, unlink: bool = False,
               timeout_s: float | None = None) -> str:
    """Create (or fetch) a public share link.

    Args:
        fs: The remote.
        remote: The item to share.
        ep: The daemon to ask.
        expire: A duration string such as ``"24h"``. Omitted means rclone's
            ``off``, which Graph reads as roughly a century.
        unlink: **Do not use.** OneDrive's backend accepts the parameter and
            never reads it: the body unconditionally POSTs ``createLink``, so
            ``unlink=True`` *creates* a link and returns its URL. Passing it is
            logged as a warning; "Remove link" must be disabled in the UI
            instead (``ShareService.can_revoke()`` is hard-wired ``False``).
        timeout_s: Socket timeout.

    Returns:
        The URL. For a **file** on a ``region=global`` personal drive rclone
        rewrites the ``1drv.ms`` share URL into a direct-download URL; for a
        folder it returns the share URL unchanged. That differs from what
        Windows' "Copy link" produces and cannot be avoided through rclone.

    Raises:
        RcError: The backend has ``PublicLink: false``, or the organisation
            forbids anonymous links.
    """
    if unlink:
        log.warning("operations/publiclink was asked to unlink %s: OneDrive "
                    "ignores that flag and will CREATE a link instead", remote)
    params: dict[str, Any] = {"fs": fs, "remote": _str(remote).strip("/"),
                              "unlink": bool(unlink)}
    if expire:
        params["expire"] = str(expire)
    return _str(_call(ep, "operations/publiclink", params, timeout_s).get("url"))


def settier(fs: str, tier: str, *, ep: RcEndpoint,
            timeout_s: float | None = None) -> None:
    """Set the storage class of every object under ``fs``.

    Args:
        fs: The remote.
        tier: The backend's tier name.
        ep: The daemon to ask.
        timeout_s: Socket timeout.

    Raises:
        RcError: **Always, on OneDrive** — ``remote onedrive does not support
            settier``, because ``Features.SetTier`` is false. Gate the whole
            "storage class" affordance on ``supports(fs, "SetTier", ep=ep)``
            and never show it for a OneDrive account.
    """
    _call(ep, "operations/settier", {"fs": fs, "tier": str(tier)}, timeout_s)


# ─────────────────────────────────────────────────────────────────────────────
# Always-async: the two calls that walk the whole tree
# ─────────────────────────────────────────────────────────────────────────────

def _handle(reply: Mapping[str, Any], path: str, group: str,
            label: str) -> JobHandle:
    """Turn an ``_async`` reply into a :class:`~onedriveui.models.JobHandle`."""
    if "jobid" not in reply:
        raise RcError(path, 500, {
            "error": f"{path} answered without a jobid: {sorted(reply)}",
            "input": {}, "path": path, "status": 500})
    job_id = _int(reply.get("jobid"))
    return JobHandle(
        job_id=job_id,
        execute_id=_str(reply.get("executeId")),
        # Without an explicit `_group` rclone invents `job/<id>`; recording the
        # real name is what lets core/stats be polled for this job's progress.
        group=group or f"job/{job_id}",
        path=path,
        label=label,
        started_at=utcnow_iso(),
    )


def size(fs: str, *, ep: RcEndpoint, group: str = "", label: str = "",
         timeout_s: float | None = None) -> JobHandle:
    """Start an ``operations/size`` walk. **Always ``_async``.**

    Args:
        fs: The remote, which may include a path (``"onedrive:Documents"``).
        ep: The daemon to ask.
        group: The ``_group`` to attribute progress to. Poll it with
            ``core/stats {"group": …}`` and show ``listed`` as the progress
            reading — there is no total to divide by.
        label: A human label for the activity row.
        timeout_s: Socket timeout for *starting* the job, not for running it.

    Returns:
        The handle. Watch it with
        :class:`~onedriveui.rc.client.JobWatcher` or
        :class:`~onedriveui.rc.jobs.JobRegistry`, then decode
        ``job/status.output`` with :func:`parse_size`.

    OneDrive has ``ListR: false``, so this is one Graph request per directory
    and can run for minutes on a real tree. Running it synchronously would hold
    an HTTP request open for the duration and block whichever thread issued it,
    which is why ``_async`` is not optional here.
    """
    reply = _call(ep, "operations/size",
                  build_params({"fs": fs}, group=group or None, async_=True),
                  timeout_s)
    return _handle(reply, "operations/size", group, label)


def check(src_fs: str, dst_fs: str, *, ep: RcEndpoint, group: str = "",
          label: str = "", one_way: bool = False, download: bool = False,
          combined: bool = True, match: bool = False, differ: bool = True,
          missing_on_src: bool = True, missing_on_dst: bool = True,
          errors: bool = True,
          timeout_s: float | None = None) -> JobHandle:
    """Start an ``operations/check`` comparison. **Always ``_async``.**

    Args:
        src_fs: The source remote.
        dst_fs: The destination remote.
        ep: The daemon to ask.
        group: The ``_group`` for progress.
        label: A human label.
        one_way: Only report what is missing on the destination.
        download: Compare by downloading both sides. Necessary only when the
            two backends share no hash; OneDrive and local both implement
            ``quickxor``, so they do.
        combined: Ask for the ``combined`` list — the one the UI renders.
        match: Ask for the list of matching names. Off: it is the whole tree.
        differ: Ask for the differing names.
        missing_on_src: Ask for names absent from the source.
        missing_on_dst: Ask for names absent from the destination.
        errors: Ask for names that could not be compared.
        timeout_s: Socket timeout for starting the job.

    Returns:
        The handle; decode ``job/status.output`` with :func:`parse_check`.

    This is the engine behind a Windows-style "what is out of date" panel. Like
    :func:`size` it walks the tree without ``ListR`` and must never be run
    synchronously.
    """
    params: dict[str, Any] = {
        "srcFs": src_fs, "dstFs": dst_fs,
        "oneWay": bool(one_way), "download": bool(download),
        "combined": bool(combined), "match": bool(match),
        "differ": bool(differ), "missingOnSrc": bool(missing_on_src),
        "missingOnDst": bool(missing_on_dst), "error": bool(errors),
    }
    reply = _call(ep, "operations/check",
                  build_params(params, group=group or None, async_=True),
                  timeout_s)
    return _handle(reply, "operations/check", group, label)


# ─────────────────────────────────────────────────────────────────────────────
# core/bwlimit — the ONLY working runtime throttle
# ─────────────────────────────────────────────────────────────────────────────

def get_bwlimit(*, ep: RcEndpoint, timeout_s: float | None = None) -> BwLimit:
    """Read the current bandwidth limit without changing it."""
    return parse_bwlimit(_call(ep, "core/bwlimit", {}, timeout_s))


def set_bwlimit(rate: str, *, ep: RcEndpoint,
                timeout_s: float | None = None) -> BwLimit:
    """Set the process-wide bandwidth limit.

    Args:
        rate: ``"off"``, a single limit (``"1M"``), or ``"<upload>:<download>"``
            (``"1M:100k"``). Suffixes are binary. The caller converts the
            OneDrive UI's KB/s into rclone's KiB/s with
            :func:`onedriveui.units.kb_to_kib` — the one place that conversion
            is allowed to happen.
        ep: The daemon to throttle.
        timeout_s: Socket timeout.

    Returns:
        The limit as the daemon now holds it. ``rate`` comes back **normalised
        to binary units** (``1M:100k`` → ``1Mi:100Ki``), so never string-compare
        it with what you sent — compare :attr:`BwLimit.tx` / :attr:`BwLimit.rx`.

    ``_config.BwLimit`` on an individual job is accepted, reflected by
    ``options/local``, and does **not** throttle anything: ``--bwlimit`` is a
    single process-wide token bucket. This endpoint is the only thing that
    works, and it applies to every in-flight job at once.
    """
    return parse_bwlimit(
        _call(ep, "core/bwlimit", {"rate": str(rate)}, timeout_s))


# ─────────────────────────────────────────────────────────────────────────────
# operations/uploadfile — query-string parameters, multipart body
# ─────────────────────────────────────────────────────────────────────────────

def _quote_filename(name: str) -> str:
    """Escape a name for a ``filename="…"`` multipart parameter.

    Backslash and double quote are escaped as RFC 2616 quoted-string requires
    (Go's ``mime`` unescapes both); CR and LF are stripped because a header
    cannot carry them at all and a name containing one would otherwise inject a
    second part into the body.
    """
    cleaned = name.replace("\r", "").replace("\n", "")
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def uploadfile(local_path: Path | str, fs: str, remote: str = "", *,
               ep: RcEndpoint, name: str | None = None, group: str = "",
               timeout_s: float = UPLOAD_TIMEOUT_S) -> str:
    """Upload one local file into ``fs``/``remote``.

    Args:
        local_path: The file to send. Streamed, never read into memory.
        fs: The destination remote, e.g. ``"onedrive:"``.
        remote: The destination **directory** inside ``fs``. It must already
            exist — call :func:`mkdir` first.
        ep: The daemon to upload through.
        name: The destination file name. Defaults to ``local_path.name``. It
            must be a leaf: a name containing ``/`` is rejected, because the
            destination directory comes from ``remote`` and nothing else.
        group: A ``_group`` so ``core/stats`` can show progress. Sent as a
            query parameter like every other parameter of this endpoint.
        timeout_s: Socket timeout for the whole transfer.

    Returns:
        The destination path relative to ``fs``.

    Raises:
        ValueError: ``name`` contains a path separator.
        OSError: ``local_path`` cannot be opened.
        DaemonUnavailable: The daemon did not answer.
        RcError: The daemon rejected the upload.
        SafetyRefusal: The path is banned (it is not, but the check is applied
            to every rc call in this codebase without exception).

    This endpoint is unlike every other one in the API, in two ways that break
    naive code. Its **parameters go in the query string**, because the request
    body is the multipart payload — a JSON body is simply ignored. And the
    **destination file name comes from the part's ``filename=`` attribute**, not
    from the field name: ``-F "f1=@a.txt"`` writes ``a.txt``, not ``f1``. The
    response is a bare ``{}`` with no size confirmation, so a caller that needs
    proof must follow up with :func:`stat`.

    For a whole tree, or for anything large enough to want retries and
    multi-threaded upload, use ``sync/copy`` instead: it gives a job id, real
    progress and rclone's own retry ladder.
    """
    path = "operations/uploadfile"
    guards.assert_rc_path_allowed(path)
    source = Path(local_path)
    filename = name if name is not None else source.name
    if "/" in filename or "\\" in filename:
        raise ValueError(
            f"uploadfile destination name must be a leaf, not {filename!r}: "
            f"the directory comes from remote={remote!r}")
    directory = _str(remote).strip("/")

    query: dict[str, str] = {"fs": fs, "remote": directory}
    if group:
        query["_group"] = group
    target = f"/{path}?{urllib.parse.urlencode(query)}"

    boundary = f"----OneDriveUI{secrets.token_hex(16)}"
    preamble = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; '
        f'filename="{_quote_filename(filename)}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    epilogue = f"\r\n--{boundary}--\r\n".encode("ascii")
    file_size = source.stat().st_size

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(preamble) + file_size + len(epilogue)),
        "Authorization": _basic_auth(ep),
    }
    # Opened outside the network try/except on purpose: an unreadable source
    # file is the caller's problem and must surface as the OSError it is, never
    # be re-labelled "the daemon is unreachable".
    handle = source.open("rb")
    conn = http.client.HTTPConnection(ep.host, ep.port, timeout=timeout_s)
    try:
        conn.putrequest("POST", target, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        conn.send(preamble)
        # http.client streams a file object in blocks, so a 4 GiB upload costs
        # one block of memory, not four gigabytes.
        conn.send(handle)
        conn.send(epilogue)
        response = conn.getresponse()
        status = int(response.status)
        raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise DaemonUnavailable(path, 503, {
            "error": f'connection failed: Post "{ep.base_url}/{path}": {exc}',
            "input": dict(query), "path": path, "status": 503}) from exc
    finally:
        conn.close()
        handle.close()

    if status >= 400:
        raise RcError(path, status, _error_body(raw, path, status, query))
    log.info("uploaded %s -> %s%s (%d bytes)", source.name, fs,
             f"/{directory}" if directory else "", file_size)
    return f"{directory}/{filename}" if directory else filename


def _basic_auth(ep: RcEndpoint) -> str:
    """The ``Authorization`` header for ``ep``.

    ``--rc-no-auth`` exempts nothing in v1.75.0 (all 101 commands report
    ``NoAuth: false``), so credentials are attached unconditionally, exactly as
    ``rc.client`` does.
    """
    token = base64.b64encode(
        f"{ep.user}:{ep.password}".encode()).decode("ascii")
    return f"Basic {token}"


def _error_body(raw: bytes, path: str, status: int,
                query: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise an upload failure into rclone's 4-key envelope."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None
    if isinstance(parsed, dict) and "error" in parsed:
        return parsed
    text = raw.decode("utf-8", "replace").strip()
    return {"error": text or f"HTTP {status}", "input": dict(query),
            "path": path, "status": status}
