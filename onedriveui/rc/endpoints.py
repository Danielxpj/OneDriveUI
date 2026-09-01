"""rc endpoints: port bind-probe, per-launch credentials, ``endpoints.json``.

An rc daemon is equivalent to shell access as this user — ``core/command`` runs
arbitrary rclone command lines and ``config/*`` reads the OAuth token — so every
listener OneDriveUI starts is bound to loopback with a fresh random password, and
the record of it lives in a ``0600`` file inside the ``0700``
``$XDG_RUNTIME_DIR/onedriveui`` directory.

Two ports on this machine are permanently off limits and one is reserved:

* **5572** is rclone's default rc port and is *already in use* here by the user's
  own ``rclone mount onedrive: ~/OneDrive --rc --rc-no-auth``. Binding it is
  impossible and, worse, finding a daemon there is not evidence it is ours.
* **5573** was used for the empirical rc research and may still be live.
* **53682** is rclone's fixed OAuth callback ``bindPort``; taking it would break
  sign-in.

All three are in :data:`onedriveui.constants.RC_FORBIDDEN_PORTS`, and
:func:`pick_free_port` skips them even when a caller passes a range that contains
them.
"""

from __future__ import annotations

import errno
import json
import logging
import secrets
import socket
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from onedriveui import APP_ID, paths
from onedriveui.atomicio import atomic_write_text
from onedriveui.constants import RC_FORBIDDEN_PORTS, RC_PORT_RANGE
from onedriveui.errors import OneDriveUIError
from onedriveui.models import RcEndpoint
from onedriveui.paths import DIR_MODE, FILE_MODE

__all__ = [
    "ENDPOINTS_SCHEMA",
    "RC_USER",
    "clear_endpoints",
    "endpoint_key",
    "forget_endpoint",
    "generate_credentials",
    "known_ports",
    "load_endpoints",
    "pick_free_port",
    "port_is_free",
    "save_endpoint",
    "with_identity",
]

log = logging.getLogger(__name__)

#: The rc basic-auth username (ARCHITECTURE §5.2). Fixed, because the secret is
#: the password; reusing the application id keeps it recognisable in `ps`.
RC_USER = APP_ID

#: `endpoints.json`'s own version, so a future layout change is detectable rather
#: than silently mis-parsed. Bumping it makes `load_endpoints()` return {}.
ENDPOINTS_SCHEMA = 1

_KIND_RCD = "rcd"
_KIND_MOUNT = "mount"


# ─────────────────────────────────────────────────────────────────────────────
# Ports
# ─────────────────────────────────────────────────────────────────────────────

def port_is_free(port: int, *, host: str = "127.0.0.1") -> bool:
    """Can we bind ``host:port`` right now?

    A real ``bind()`` is the only honest test. ``SO_REUSEADDR`` is deliberately
    *not* set: with it, Linux would let us bind a port in ``TIME_WAIT`` that a
    peer still considers taken, which is exactly the false positive that would
    hand two rclone daemons the same address.

    Args:
        port: TCP port to probe.
        host: Interface to probe on. Always loopback in this application.

    Returns:
        True if the bind succeeded (the socket is closed again immediately).
        False for ``EADDRINUSE``, ``EACCES`` and every other bind failure.
    """
    if port in RC_FORBIDDEN_PORTS:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, int(port)))
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL):
            log.debug("bind probe on %s:%d failed unexpectedly: %s", host, port, exc)
        return False
    finally:
        sock.close()
    return True


def pick_free_port(*, host: str = "127.0.0.1",
                   ports: Iterable[int] = RC_PORT_RANGE,
                   exclude: Iterable[int] = ()) -> int:
    """Bind-probe ``ports`` in order and return the first that is free.

    Args:
        host: Interface to probe. Loopback in production.
        ports: Candidates, tried in the order given. Defaults to
            :data:`~onedriveui.constants.RC_PORT_RANGE` (17800–17899).
        exclude: Ports the caller has already handed out in this startup but has
            not bound yet — the rcd port while the mount port is being chosen.

    Returns:
        A port that was bindable a moment ago. It is not reserved: the caller
        must start its listener promptly and cope with a loss of the race.

        Never 5572, 5573 or 53682, whatever ``ports`` contains.

    Raises:
        OneDriveUIError: every candidate was busy.
    """
    skip = set(exclude) | set(RC_FORBIDDEN_PORTS)
    tried = 0
    for port in ports:
        if port in skip:
            continue
        tried += 1
        if port_is_free(port, host=host):
            return int(port)
    raise OneDriveUIError(
        f"no free rc port on {host}: {tried} candidates probed, all busy "
        f"(5572/5573/53682 are permanently excluded)"
    )


def generate_credentials() -> tuple[str, str]:
    """A fresh HTTP-basic credential pair for one daemon launch.

    Returns:
        ``(user, password)``. The password is ``secrets.token_urlsafe(32)`` —
        256 bits of ``os.urandom``, URL-safe base64, so it survives a systemd
        unit file, a ``ps`` line and an HTTP header without quoting.

        Never reused across launches: a leaked password is worthless the moment
        the daemon restarts.
    """
    return RC_USER, secrets.token_urlsafe(32)


def with_identity(ep: RcEndpoint, **fields: Any) -> RcEndpoint:
    """Return a copy of ``ep`` with ``fields`` replaced.

    :class:`~onedriveui.models.RcEndpoint` is frozen, so the ownership proof
    cannot write ``pid``/``starttime``/``execute_id`` back into it in place.

    Args:
        ep: The endpoint to copy.
        **fields: Any :class:`RcEndpoint` field name.

    Returns:
        A new frozen endpoint.
    """
    return replace(ep, **fields)


# ─────────────────────────────────────────────────────────────────────────────
# endpoints.json
# ─────────────────────────────────────────────────────────────────────────────

def endpoint_key(kind: str, account_id: str = "") -> str:
    """The key an endpoint is stored and looked up under.

    Args:
        kind: ``"rcd"`` or ``"mount"``.
        account_id: Required for mounts, ignored for the rcd — there is exactly
            one control plane per session but one data plane per account.

    Returns:
        ``"rcd"`` or ``"mount:<account_id>"``.
    """
    if kind == _KIND_RCD:
        return _KIND_RCD
    return f"{_KIND_MOUNT}:{account_id}"


def _endpoint_to_dict(ep: RcEndpoint) -> dict[str, Any]:
    return {
        "kind": ep.kind, "host": ep.host, "port": ep.port, "user": ep.user,
        "password": ep.password, "pid": ep.pid, "starttime": ep.starttime,
        "execute_id": ep.execute_id, "mountpoint": ep.mountpoint,
        "account_id": ep.account_id,
    }


def _endpoint_from_dict(kind: str, raw: Any) -> RcEndpoint | None:
    """Rebuild one endpoint, tolerating any junk a hand-edit may have left."""
    if not isinstance(raw, dict):
        return None
    try:
        return RcEndpoint(
            kind=str(raw.get("kind") or kind),
            host=str(raw.get("host") or "127.0.0.1"),
            port=int(raw.get("port") or 0),
            user=str(raw.get("user") or ""),
            password=str(raw.get("password") or ""),
            pid=int(raw.get("pid") or 0),
            starttime=int(raw.get("starttime") or 0),
            execute_id=str(raw.get("execute_id") or ""),
            mountpoint=str(raw.get("mountpoint") or ""),
            account_id=str(raw.get("account_id") or ""),
        )
    except (TypeError, ValueError):
        return None


def _read_document() -> dict[str, Any]:
    """The raw ``endpoints.json`` object, or a fresh empty one.

    Never raises. The file is per-boot scratch state: a truncated or hand-mangled
    one means "we know nothing", which correctly makes the supervisor provision a
    new daemon rather than crash at startup.
    """
    path = paths.endpoints_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {"version": ENDPOINTS_SCHEMA, _KIND_RCD: None, _KIND_MOUNT: {}}
    if not isinstance(raw, dict) or raw.get("version") != ENDPOINTS_SCHEMA:
        return {"version": ENDPOINTS_SCHEMA, _KIND_RCD: None, _KIND_MOUNT: {}}
    mounts = raw.get(_KIND_MOUNT)
    return {
        "version": ENDPOINTS_SCHEMA,
        _KIND_RCD: raw.get(_KIND_RCD),
        _KIND_MOUNT: mounts if isinstance(mounts, dict) else {},
    }


def _write_document(doc: dict[str, Any]) -> Path:
    """Persist the document at ``0600`` inside the ``0700`` runtime directory."""
    path = paths.endpoints_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    atomic_write_text(
        path, json.dumps(doc, indent=2, sort_keys=True) + "\n", mode=FILE_MODE)
    return path


def load_endpoints() -> dict[str, RcEndpoint]:
    """Every endpoint recorded this boot.

    Returns:
        ``{endpoint_key: RcEndpoint}``, e.g. ``{"rcd": …, "mount:onedrive": …}``.
        Empty when the file is missing, unreadable, of a foreign schema version,
        or garbage — all of which mean "provision from scratch", never "crash".

        The endpoints are *claims*, not proof: a recorded port may now hold a
        stranger's daemon. Everything here must still pass
        ``RcdSupervisor.verify_ownership()`` before it is driven.
    """
    doc = _read_document()
    out: dict[str, RcEndpoint] = {}
    rcd = _endpoint_from_dict(_KIND_RCD, doc.get(_KIND_RCD))
    if rcd is not None and rcd.port:
        out[_KIND_RCD] = rcd
    for account_id, raw in (doc.get(_KIND_MOUNT) or {}).items():
        mount = _endpoint_from_dict(_KIND_MOUNT, raw)
        if mount is not None and mount.port:
            key = endpoint_key(_KIND_MOUNT, mount.account_id or str(account_id))
            out[key] = mount
    return out


def save_endpoint(ep: RcEndpoint) -> Path:
    """Record ``ep`` in ``endpoints.json``, replacing any previous entry.

    Args:
        ep: The endpoint. ``ep.kind`` decides the slot; a mount also needs
            ``ep.account_id``.

    Returns:
        The path written.

    Raises:
        ValueError: ``ep.kind`` is not ``"rcd"`` or ``"mount"``, or a mount
            endpoint carries no ``account_id`` — it would be unaddressable.
        OSError: The runtime directory is unwritable.
    """
    if ep.kind not in (_KIND_RCD, _KIND_MOUNT):
        raise ValueError(f"RcEndpoint.kind must be 'rcd' or 'mount', not {ep.kind!r}")
    if ep.kind == _KIND_MOUNT and not ep.account_id:
        raise ValueError("a mount endpoint needs an account_id to be addressable")
    doc = _read_document()
    if ep.kind == _KIND_RCD:
        doc[_KIND_RCD] = _endpoint_to_dict(ep)
    else:
        doc[_KIND_MOUNT][ep.account_id] = _endpoint_to_dict(ep)
    return _write_document(doc)


def forget_endpoint(kind: str, account_id: str = "") -> Path:
    """Drop one endpoint, e.g. after the daemon it described was stopped.

    Args:
        kind: ``"rcd"`` or ``"mount"``.
        account_id: The account, for a mount.

    Returns:
        The path written. Forgetting an endpoint that was never recorded is a
        no-op that still rewrites the file, so the caller need not check first.
    """
    doc = _read_document()
    if kind == _KIND_RCD:
        doc[_KIND_RCD] = None
    else:
        doc[_KIND_MOUNT].pop(account_id, None)
    return _write_document(doc)


def clear_endpoints() -> Path:
    """Forget every endpoint — used on shutdown and by ``--reset``.

    Returns:
        The path written.
    """
    return _write_document(
        {"version": ENDPOINTS_SCHEMA, _KIND_RCD: None, _KIND_MOUNT: {}})


def known_ports(extra: Sequence[RcEndpoint] = ()) -> set[int]:
    """Every port this application believes it already owns.

    Fed to :func:`pick_free_port`'s ``exclude`` so a second daemon started in the
    same startup cannot be handed the port the first one is still binding.

    Args:
        extra: Endpoints held in memory that are not on disk yet.

    Returns:
        The set of ports.
    """
    ports = {ep.port for ep in load_endpoints().values() if ep.port}
    ports |= {ep.port for ep in extra if ep.port}
    return ports
