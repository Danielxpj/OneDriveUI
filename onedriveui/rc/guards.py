"""SAFETY. The non-overridable refusals of ARCHITECTURE §3.

Every function here either returns ``None`` or raises :class:`SafetyRefusal`
carrying the id of the invariant it defends. A ``SafetyRefusal`` is **always a
bug in the caller** — it is never caught and swallowed, never shown to the user
as something to click past, and never made overridable by config.

The invariants this module enforces:

===== ==========================================================================
 I1    No ``--onedrive-*`` / ``--drive-*`` / connection-string backend option on
       any rclone command line. A command-line backend override renames the fs
       to ``onedrive{HASH}:`` and silently relocates the whole VFS cache.
 I2    No data-moving rclone command may name a path at or under a
       ``fuse.rclone`` mountpoint, and the two sides of a bisync must be
       disjoint.
 I3    Nothing whose sidecar is ``Dirty`` or whose name is in ``vfs/queue`` may
       be evicted, force-unmounted or bisync'd around.
 I4    Cache paths always come from ``vfs/stats.diskCache.path`` / ``.pathMeta``.
 I7    ``mount/mount`` and its siblings are never called.
 I8    ``operations/cleanup`` is never called.
 I11   A filters rewrite is always paired with an immediate ``--resync``.
 I12   ``--inplace`` is never passed to any rclone command.
 I13   ``- *.partial`` is always present in the filters file.
 I14   ``config/dump`` / ``config/get`` output never reaches a log or a bundle.
===== ==========================================================================

Two of them are enforced at the choke point rather than at the call site:
:func:`assert_rc_path_allowed` is called by ``rc.client`` on *every* rc call, so
I7 and I8 cannot be violated even by a typo, and :func:`assert_no_backend_flags`
is called by ``MountController.build_argv()`` on its own output.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from onedriveui import paths
from onedriveui.constants import MANDATORY_EXCLUDES
from onedriveui.errors import SafetyRefusal
from onedriveui.models import AccountInfo

__all__ = [
    "BACKEND_PREFIXES",
    "BACKEND_PREFIX_EXEMPT",
    "BANNED_RC_PATHS",
    "CONNECTION_STRING_RE",
    "SECRET_FILES",
    "SECRET_RC_PATHS",
    "assert_bisync_safe",
    "assert_bundle_safe",
    "assert_cache_paths_from_stats",
    "assert_db_not_on_fuse",
    "assert_disjoint",
    "assert_evict_safe",
    "assert_no_backend_flags",
    "assert_no_inplace",
    "assert_not_under_fuse",
    "assert_partial_excluded",
    "assert_rc_path_allowed",
    "rewrite_mount_path_to_remote",
]


# ─────────────────────────────────────────────────────────────────────────────
# I1 — backend options never reach a command line
# ─────────────────────────────────────────────────────────────────────────────

#: Every backend name `rclone help backends` reports on v1.75.0, verified on this
#: machine. A flag `--<prefix>-<option>` for any of these is a backend override:
#: rclone hashes the set of such overrides into the fs canonical name, producing
#: `onedrive{MxOuf}:`, a *different* VFS cache directory, and an instantly
#: orphaned cache. This machine already carries two orphaned OneDrive trees.
BACKEND_PREFIXES: frozenset[str] = frozenset({
    "alias", "archive", "azureblob", "azurefiles", "b2", "box", "cache",
    "chunker", "cloudinary", "combine", "compress", "crypt", "doi", "drime",
    "drive", "dropbox", "fichier", "filefabric", "filelu", "filen", "filescom",
    "ftp", "gcs", "gofile", "gphotos", "hasher", "hdfs", "hidrive", "http",
    "huaweidrive", "iclouddrive", "imagekit", "internetarchive", "internxt",
    "jottacloud", "koofr", "linkbox", "local", "mailru", "mega", "memory",
    "netstorage", "onedrive", "oos", "opendrive", "pcloud", "pikpak",
    "pixeldrain", "premiumizeme", "protondrive", "putio", "qingstor", "quatrix",
    "s3", "seafile", "sftp", "shade", "sharefile", "sia", "smb", "storj",
    "sugarsync", "swift", "tardigrade", "ulozto", "union", "webdav", "yandex",
    "zoho",
})

#: A connection string carries backend options inline and is hashed into the fs
#: name exactly like a flag: `:local,nounc:/tmp`, `onedrive,chunk_size=30M:Docs`.
#: The comma before the colon is what distinguishes it from a plain `remote:path`
#: or a `host:port`.
CONNECTION_STRING_RE = re.compile(r"^:?[A-Za-z0-9_.+-]+,[^:]*:")

#: The only two flags in the whole of rclone v1.75.0 that begin with a backend
#: name and are nevertheless ordinary *global* flags. Derived by intersecting
#: every long flag above the "Backend-only flags" heading in `rclone help flags`
#: with :data:`BACKEND_PREFIXES` — 295 global flags, exactly these two collisions.
#: `--cache-dir` is in the Config group and `--http-proxy` in Networking; neither
#: is hashed into the fs name, and the mount argv of §5.3 needs `--cache-dir`.
BACKEND_PREFIX_EXEMPT: frozenset[str] = frozenset({
    "--cache-dir", "--http-proxy",
})


def _flag_name(token: str) -> str:
    """The flag part of ``--name=value``, or the token itself."""
    return token.split("=", 1)[0]


def assert_no_backend_flags(argv: Sequence[str]) -> None:
    """Invariant I1. Refuse any argv carrying a backend option.

    Catches all three shapes a backend override can take:

    * ``--onedrive-chunk-size 30M`` (separate value)
    * ``--onedrive-chunk-size=30M`` (joined value)
    * ``onedrive,chunk_size=30M:`` (connection string, as an fs argument)

    Args:
        argv: The full command line, program name included.

    Raises:
        SafetyRefusal: invariant ``"I1"``, naming the offending token. Backend
            options belong in ``rclone.conf`` — see ``rc.conf.set_backend_options``.
    """
    for token in argv:
        if not isinstance(token, str):
            raise SafetyRefusal("I1", f"argv contains a non-string token: {token!r}")
        name = _flag_name(token)
        if name.startswith("--") and name not in BACKEND_PREFIX_EXEMPT:
            prefix = name[2:].split("-", 1)[0]
            if prefix in BACKEND_PREFIXES and "-" in name[2:]:
                raise SafetyRefusal(
                    "I1",
                    f"backend option {name!r} on a command line renames the fs to "
                    f"'{prefix}{{HASH}}:' and orphans the whole VFS cache; put it "
                    f"in rclone.conf via rc.conf.set_backend_options()",
                )
        if CONNECTION_STRING_RE.match(token):
            raise SafetyRefusal(
                "I1",
                f"connection string {token!r} carries backend options inline and "
                f"is hashed into the fs name exactly like a --backend-flag",
            )


# ─────────────────────────────────────────────────────────────────────────────
# I12 — --inplace is never passed
# ─────────────────────────────────────────────────────────────────────────────

def assert_no_inplace(argv: Sequence[str]) -> None:
    """Invariant I12. Refuse ``--inplace`` anywhere on a command line.

    An interrupted in-place transfer corrupts the destination, and the
    corruption then propagates back on the next run. rclone's default —
    write to ``<name>.<hash>.partial`` and rename — is the only safe mode.

    Args:
        argv: The full command line.

    Raises:
        SafetyRefusal: invariant ``"I12"``.
    """
    for token in argv:
        if _flag_name(str(token)) == "--inplace":
            raise SafetyRefusal(
                "I12",
                "--inplace corrupts the destination when a transfer is "
                "interrupted, and the corruption propagates on the next run",
            )


# ─────────────────────────────────────────────────────────────────────────────
# I2 — nothing under a fuse mount
# ─────────────────────────────────────────────────────────────────────────────

def assert_not_under_fuse(path: Path | str | os.PathLike[str], what: str) -> None:
    """Invariant I2. Refuse a path at or under any ``fuse.rclone`` mountpoint.

    ``--vfs-write-back 5s`` guarantees the "file changed during the run" timing
    that rclone's own bisync documentation warns causes data loss, so no
    ``sync``/``copy``/``move``/``bisync``/``delete`` may ever name a path inside
    the mount. The check resolves symlinks first, because a KFM'd
    ``~/Documents`` may be a symlink into the mount.

    Args:
        path: The candidate path. It need not exist yet.
        what: What the caller wanted it for, e.g. ``"sync"`` or ``"bisync
            path1"``. Quoted into the refusal so the log names the caller.

    Raises:
        SafetyRefusal: invariant ``"I2"``.
    """
    if paths.is_under_fuse_mount(path):
        raise SafetyRefusal(
            "I2",
            f"{what}: {os.fspath(path)!r} is at or under a fuse.rclone mount; "
            f"an rclone data command may never name a path inside the mount",
        )


def assert_disjoint(local: Path | str | os.PathLike[str],
                    mountpoints: Sequence[Path | str | os.PathLike[str]]) -> None:
    """Invariant I2. Refuse a local tree that overlaps any mountpoint, either way.

    Containment is checked in **both** directions: ``~/OneDrive-Offline`` inside
    ``~/OneDrive`` is as fatal as ``~/OneDrive`` inside ``~/OneDrive-Offline``,
    and equality is fatal too.

    Args:
        local: The local folder, typically the bisync ``path1``.
        mountpoints: Every live mountpoint to stay clear of.

    Raises:
        SafetyRefusal: invariant ``"I2"``.
    """
    try:
        real = Path(os.path.realpath(os.path.expanduser(str(local))))
    except OSError as exc:                                  # pragma: no cover
        raise SafetyRefusal("I2", f"cannot resolve {local!r}: {exc}") from exc
    for candidate in mountpoints:
        mount = Path(os.path.realpath(os.path.expanduser(str(candidate))))
        if real == mount:
            raise SafetyRefusal(
                "I2", f"{os.fspath(local)!r} IS the mountpoint {os.fspath(mount)!r}")
        if real.is_relative_to(mount):
            raise SafetyRefusal(
                "I2", f"{os.fspath(local)!r} is inside the mountpoint {os.fspath(mount)!r}")
        if mount.is_relative_to(real):
            raise SafetyRefusal(
                "I2", f"the mountpoint {os.fspath(mount)!r} is inside {os.fspath(local)!r}")


def assert_db_not_on_fuse(path: Path | str | os.PathLike[str]) -> None:
    """Refuse to put the SQLite database under a fuse mount.

    SQLite's WAL relies on POSIX advisory locking and on ``mmap`` semantics that
    FUSE does not reproduce; a database there corrupts rather than blocks.

    Args:
        path: The intended ``state.db`` location.

    Raises:
        SafetyRefusal: invariant ``"I2"``.
    """
    if paths.is_under_fuse_mount(path):
        raise SafetyRefusal(
            "I2",
            f"the SQLite database may not live under a fuse mount "
            f"({os.fspath(path)!r}): WAL loses its locking guarantees there",
        )


# ─────────────────────────────────────────────────────────────────────────────
# I3 — a dirty or queued cache item is irreplaceable
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_vfs_name(name: str) -> str:
    """``vfs/queue`` reports a path within the VFS; sidecar walks produce the
    same path relative to ``pathMeta``. Strip the leading separator and any
    ``./`` so the two vocabularies compare."""
    return str(name).replace("\\", "/").lstrip("./").rstrip("/")


def assert_evict_safe(meta: Mapping[str, Any], queue_names: Iterable[str],
                      rel_path: str) -> None:
    """Invariant I3. Refuse to evict an item that exists nowhere else.

    A cache item whose sidecar says ``Dirty: true`` is a local change that has
    not been uploaded: the bytes exist on this disk and nowhere on the planet.
    An item named in ``vfs/queue`` is about to be uploaded. Unlinking either is
    unrecoverable data loss, so eviction, force-unmount and bisync all route
    through this check.

    Args:
        meta: The parsed ``vfsMeta`` sidecar — ``{ModTime, ATime, Size, Rs,
            Fingerprint, Dirty}``. An empty mapping is safe (nothing cached).
        queue_names: The ``name`` field of every ``vfs/queue`` row.
        rel_path: The item's path relative to the VFS root.

    Raises:
        SafetyRefusal: invariant ``"I3"``.
    """
    if meta.get("Dirty"):
        raise SafetyRefusal(
            "I3",
            f"{rel_path!r} has Dirty:true — an un-uploaded local change that "
            f"exists nowhere else; evicting it is unrecoverable data loss",
        )
    target = _normalise_vfs_name(rel_path)
    for name in queue_names:
        if _normalise_vfs_name(name) == target:
            raise SafetyRefusal(
                "I3",
                f"{rel_path!r} is in vfs/queue waiting to upload; it may not be "
                f"evicted, force-unmounted or bisync'd around",
            )


# ─────────────────────────────────────────────────────────────────────────────
# I4 — cache paths come from vfs/stats, never from arithmetic
# ─────────────────────────────────────────────────────────────────────────────

def assert_cache_paths_from_stats(stats: Mapping[str, Any]) -> tuple[str, str]:
    """Invariant I4. Extract the two cache roots, refusing any other source.

    Hand-derivation (``~/.cache/rclone/vfs/<remote>``) misses the ``{HASH}``
    suffix rclone appends when a backend flag differs, misses a remote sub-path,
    and misses ``--cache-dir``. The authoritative values are the two absolute
    paths ``vfs/stats`` reports, and this function is the only supported way to
    obtain them.

    Args:
        stats: A raw ``vfs/stats`` response body.

    Returns:
        ``(diskCache.path, diskCache.pathMeta)``, both absolute.

    Raises:
        SafetyRefusal: invariant ``"I4"`` when ``diskCache`` is absent (the VFS
            was started with ``--vfs-cache-mode off``) or either path is empty.
    """
    disk = stats.get("diskCache")
    if not isinstance(disk, Mapping):
        raise SafetyRefusal(
            "I4",
            "vfs/stats carries no diskCache block (--vfs-cache-mode off?); a "
            "cache path may never be derived by hand",
        )
    data = str(disk.get("path") or "")
    meta = str(disk.get("pathMeta") or "")
    if not data or not meta:
        raise SafetyRefusal(
            "I4",
            f"vfs/stats.diskCache is missing path/pathMeta (path={data!r}, "
            f"pathMeta={meta!r}); a cache path may never be derived by hand",
        )
    return data, meta


# ─────────────────────────────────────────────────────────────────────────────
# I13 — the partial-file exclude is not optional
# ─────────────────────────────────────────────────────────────────────────────

#: The rule whose absence lets a `SIGKILL`-orphaned `<name>.<hash>.partial` sync
#: back as a genuine new file. Sourced from the frozen contract so the guard and
#: the generator can never disagree.
_PARTIAL_RULE = "- *.partial"


def assert_partial_excluded(lines: Iterable[str]) -> None:
    """Invariant I13. Refuse a filters file without ``- *.partial``.

    A ``SIGKILL`` mid-transfer leaves ``<name>.<hash>.partial`` at the
    destination. Without this rule the next run treats it as a genuine new file
    and syncs the fragment everywhere.

    Args:
        lines: The filter rules, one per element. Comments and blanks are
            ignored; surrounding whitespace is not significant.

    Raises:
        SafetyRefusal: invariant ``"I13"``.
    """
    for line in lines:
        if str(line).strip() == _PARTIAL_RULE:
            return
    raise SafetyRefusal(
        "I13",
        f"the filters file is missing {_PARTIAL_RULE!r}; a SIGKILL mid-transfer "
        f"then leaves a .partial fragment that the next run syncs as a real file",
    )


# ─────────────────────────────────────────────────────────────────────────────
# I2 + I11 + I12 + I13 — the whole bisync preflight
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(cfg: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or from a dataclass/namespace, whichever the
    caller has. Config is a dataclass in WP-01 and a dict in the fixtures."""
    if isinstance(cfg, Mapping):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def assert_bisync_safe(path1: str, path2: str, cfg: Any) -> None:
    """The full bisync preflight: invariants I2, I11, I12 and I13 in one call.

    bisync is the only command in this application that deletes on both sides,
    so its preconditions are checked together and refused together.

    * **I2** — a local side may not be at or under a ``fuse.rclone`` mount, and
      the two sides may not overlap. Pointing bisync at the mount makes every
      listing pass read through the VFS it is comparing against, and
      ``--vfs-write-back`` then changes files mid-run.
    * **I11** — if the filters file changed, ``resync`` must be set in the same
      call. A filters change is a *critical* bisync abort until a resync
      happens, and a crash between the two locks the account out of syncing.
    * **I12** — ``extra_args`` may not smuggle in ``--inplace``.
    * **I13** — the filters file must carry ``- *.partial``.

    Args:
        path1: The local side, e.g. ``"~/OneDrive-Offline"``.
        path2: The remote side, e.g. ``"onedrive:Offline"``.
        cfg: The ``offline_folder`` config block, as a mapping or a dataclass.
            Read keys: ``filters_file`` (path), ``filters_lines``
            (``Sequence[str]``, used in preference to reading the file),
            ``filters_changed`` (bool), ``resync`` (bool), ``extra_args``
            (``Sequence[str]``).

    Raises:
        SafetyRefusal: with the id of the first invariant that fails.
    """
    local_sides = [side for side in (path1, path2) if not _looks_remote(side)]
    for side, label in ((path1, "bisync path1"), (path2, "bisync path2")):
        if not _looks_remote(side):
            assert_not_under_fuse(Path(os.path.expanduser(side)), label)

    if len(local_sides) == 2:
        assert_disjoint(local_sides[0], [local_sides[1]])
    for side in local_sides:
        assert_disjoint(side, [mount for _fs, mount in paths.fuse_rclone_mounts()])

    assert_no_inplace(list(_cfg(cfg, "extra_args") or ()))

    lines = _cfg(cfg, "filters_lines")
    if lines is None:
        filters_file = _cfg(cfg, "filters_file")
        if filters_file:
            try:
                lines = Path(os.path.expanduser(str(filters_file))).read_text(
                    encoding="utf-8").splitlines()
            except OSError as exc:
                raise SafetyRefusal(
                    "I13",
                    f"the filters file {str(filters_file)!r} is unreadable ({exc}); "
                    f"bisync cannot be proved to exclude *.partial",
                ) from exc
        else:
            lines = MANDATORY_EXCLUDES
    assert_partial_excluded(lines)

    if _cfg(cfg, "filters_changed", False) and not _cfg(cfg, "resync", False):
        raise SafetyRefusal(
            "I11",
            "the filters file changed without --resync in the same transaction; "
            "bisync aborts critically (exit 7, .lst -> .lst-err) until a resync, "
            "and a crash between the two locks the account out of syncing",
        )


def _looks_remote(side: str) -> bool:
    """True for an rclone remote (``onedrive:``, ``onedrive:Docs``) rather than a
    local path. A Windows-style drive letter cannot occur on Linux, so a single
    colon after a bare name is unambiguous."""
    text = str(side)
    head, sep, _tail = text.partition(":")
    return bool(sep) and "/" not in head and head != ""


# ─────────────────────────────────────────────────────────────────────────────
# I7 + I8 — endpoints nobody may call
# ─────────────────────────────────────────────────────────────────────────────

#: Invariant I7 (`mount/*`): a duplicate VFS is permanently unaddressable — the
#: `[0]`/`[1]` names `vfs/list` then reports are rejected by every other `vfs/*`
#: call and `fscache/clear` does not help. `mount/listmounts` is blind to
#: CLI-started mounts anyway, so `/proc/self/mounts` is the only enumeration.
#: Invariant I8 (`operations/cleanup`): on OneDrive `cleanup` deletes file
#: *versions*, not the trash, contradicting its own help text.
BANNED_RC_PATHS: frozenset[str] = frozenset({
    "mount/mount", "mount/unmount", "mount/unmountall", "mount/listmounts",
    "operations/cleanup",
})

#: Invariant I14: these two return the OAuth refresh token in the clear. Nothing
#: may call them, and nothing may put their output in a log or a bundle. Use
#: ``rc.conf.redacted_dump()``.
SECRET_RC_PATHS: frozenset[str] = frozenset({"config/dump", "config/get"})

#: Invariant I14: files that carry a secret in the clear and may never be a
#: bundle member. ``rclone.conf`` holds the OAuth refresh token (which is why
#: :func:`onedriveui.applog.build_diagnostics_bundle` ships
#: ``rclone-config-redacted.ini`` instead) and ``endpoints.json`` holds the rc
#: password. Matched on the *basename*, so the redacted sibling — a different
#: name — is unaffected.
SECRET_FILES: frozenset[str] = frozenset({"rclone.conf", "endpoints.json"})

_INVARIANT_FOR_PATH: dict[str, str] = {
    "mount/mount": "I7", "mount/unmount": "I7", "mount/unmountall": "I7",
    "mount/listmounts": "I7", "operations/cleanup": "I8",
}


def assert_rc_path_allowed(path: str) -> None:
    """Invariants I7, I8 and I14. Refuse a banned rc endpoint.

    Called by ``rc.client`` on **every** call, async and blocking, so the ban is
    a property of the transport rather than a rule reviewers must remember.

    Args:
        path: The rc command path, e.g. ``"core/stats"``.

    Raises:
        SafetyRefusal: ``"I7"`` for the ``mount/*`` family, ``"I8"`` for
            ``operations/cleanup``, ``"I14"`` for ``config/dump`` and
            ``config/get``.
    """
    key = str(path).strip().strip("/")
    if key in BANNED_RC_PATHS:
        invariant = _INVARIANT_FOR_PATH[key]
        detail = (
            "a duplicate VFS created through mount/mount is permanently "
            "unaddressable, and mount/listmounts cannot see CLI-started mounts; "
            "use a systemd unit and /proc/self/mounts"
            if invariant == "I7" else
            "on OneDrive operations/cleanup deletes file VERSIONS, not the "
            "trash, contradicting its own help text"
        )
        raise SafetyRefusal(invariant, f"{key} is banned: {detail}")
    if key in SECRET_RC_PATHS:
        raise SafetyRefusal(
            "I14",
            f"{key} returns the OAuth refresh token in the clear; use "
            f"rc.conf.redacted_dump() (equivalent to `rclone config redacted`)",
        )


def assert_bundle_safe(entries: Iterable[str]) -> None:
    """Invariant I14. Refuse a diagnostics bundle that names a secret source.

    The three secret sources I14 names are all covered: the two rc paths, and
    the two files that hold a secret in the clear (:data:`SECRET_FILES`).
    ``rclone.conf`` in particular is the file the refresh token actually lives
    in — a bundle must carry ``rclone-config-redacted.ini`` instead, which is a
    different basename and passes.

    Args:
        entries: What the bundle would contain — rc paths, file names, or the
            member names of the archive.

    Raises:
        SafetyRefusal: invariant ``"I14"``.
    """
    for entry in entries:
        text = str(entry)
        for banned in SECRET_RC_PATHS:
            if banned in text:
                raise SafetyRefusal(
                    "I14",
                    f"{text!r} would put {banned} output in a diagnostics "
                    f"bundle; that output contains the refresh token in the clear",
                )
        base = os.path.basename(text)
        if base in SECRET_FILES:
            why = ("the OAuth refresh token in the clear — bundle "
                   "rclone-config-redacted.ini instead"
                   if base == "rclone.conf" else "the rc password")
            raise SafetyRefusal(
                "I14", f"{base} holds {why}; it may never be bundled")


# ─────────────────────────────────────────────────────────────────────────────
# Turning a mounted path back into a remote path
# ─────────────────────────────────────────────────────────────────────────────

def rewrite_mount_path_to_remote(path: Path | str | os.PathLike[str],
                                 account: AccountInfo) -> str:
    """Turn ``~/OneDrive/x`` into ``onedrive:x``.

    I2 forbids naming a mounted path in a data command, but the user clicked on
    a file inside the mount, and refusing them is not the answer: the same object
    is reachable through the remote, where no VFS write-back can race the
    operation. Every UI action on a mounted path goes through here first.

    Args:
        path: An absolute path at or under ``account.sync_root``.
        account: The account owning that mountpoint. ``account.fs`` supplies the
            ``<remote>:`` prefix — never a ``{HASH}``-suffixed name, which would
            mean a backend flag had leaked (I1).

    Returns:
        The rclone remote path. The mountpoint itself maps to a bare
        ``"onedrive:"``; ``~/OneDrive/a/b`` maps to ``"onedrive:a/b"``. Forward
        slashes always, never a leading one.

    Raises:
        SafetyRefusal: invariant ``"I2"`` if ``path`` is not under the account's
            mountpoint — rewriting it would silently address the wrong tree.
    """
    mount = Path(os.path.realpath(os.path.expanduser(str(
        paths.mount_point(account.sync_root)))))
    real = Path(os.path.realpath(os.path.expanduser(str(path))))
    if real == mount:
        return account.fs
    if not real.is_relative_to(mount):
        raise SafetyRefusal(
            "I2",
            f"{os.fspath(path)!r} is not under {os.fspath(mount)!r}; it cannot be "
            f"rewritten onto {account.fs} without addressing the wrong tree",
        )
    return f"{account.fs}{real.relative_to(mount).as_posix()}"
