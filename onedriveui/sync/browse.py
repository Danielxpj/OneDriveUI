"""The remote folder tree, one level at a time.

"Choose folders" needs a tree of what is in the cloud, and the obvious
implementation — list the whole drive recursively — is catastrophic here for one
specific reason: **OneDrive's backend has ``ListR = false``.** rclone therefore
cannot ask Graph for a recursive listing; it issues **one HTTP request per
directory**, walking the tree itself. On a drive with 8 000 folders that is 8 000
Graph requests against a limit of 3 000 per five minutes, and the user is
throttled out of their own account for the next quarter of an hour.

So every listing here is ``dirsOnly`` and one level deep, fetched when a node is
expanded. A user picking two folders out of a hundred pays for the handful of
directories they actually opened.

Two smaller rules with the same origin:

* **``operations/size`` is always ``_async``.** Without ``ListR`` it walks the
  whole subtree, which takes minutes on a large folder — a synchronous call
  would hit the four-second rc timeout and look like a broken client.
* **The TTL is short and the cache is explicit.** Expanding, collapsing and
  re-expanding a node must not re-fetch, but a folder created on another device
  five minutes ago must appear. Sixty seconds is the compromise, and
  :meth:`RemoteBrowser.invalidate` exists for when we *know* something changed.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Final

from PySide6.QtCore import QObject, Signal

from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import AccountInfo, JobHandle, RemoteFolderNode
from onedriveui.rc import ops

log = logging.getLogger(__name__)

__all__ = ["RemoteBrowser", "CACHE_TTL_S"]

#: How long a directory listing stays fresh. Long enough that expanding and
#: collapsing a node costs nothing, short enough that a folder created on the
#: phone five minutes ago shows up.
CACHE_TTL_S: Final = 60.0


class RemoteBrowser(QObject):
    """Lazy, cached, one-level-at-a-time listings of the remote drive.

    Args:
        account: The account.
        endpoint: ``() -> RcEndpoint | None`` for the daemon to ask.
        ttl_s: Cache lifetime.
        monotonic: The clock, injected for tests.
        parent: Qt parent.

    Signals:
        listed: ``(rel_path, children)`` after a successful listing.
        failed: ``(rel_path, message)``.
    """

    listed = Signal(str, list)
    failed = Signal(str, str)

    def __init__(
        self,
        account: AccountInfo,
        *,
        endpoint: Any = None,
        ttl_s: float = CACHE_TTL_S,
        monotonic: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._endpoint = endpoint or (lambda: None)
        self._ttl_s = ttl_s
        self._monotonic = monotonic or time.monotonic
        self._cache: dict[str, tuple[float, list[RemoteFolderNode]]] = {}

    # ═════════════════════════════════════════════════════════════════════════
    # Listing
    # ═════════════════════════════════════════════════════════════════════════

    def children(self, rel_path: str = "", *,
                 dirs_only: bool = True,
                 force: bool = False) -> list[RemoteFolderNode]:
        """One directory's contents. **Never recursive.**

        Args:
            rel_path: The directory, relative to the drive root. ``""`` is the
                root itself.
            dirs_only: Folders only, which is what the folder picker wants and
                what keeps the response small.
            force: Ignore the cache.

        Returns:
            The children, or the cached list, or ``[]`` on failure.

        There is deliberately no ``recursive`` argument. OneDrive has
        ``ListR = false``, so rclone would issue one Graph request per directory
        — 8 000 folders is 8 000 requests against a 3 000-per-five-minutes limit,
        and the user is locked out of their own account for fifteen minutes. A
        recursive listing is not a slow version of this; it is a different and
        much worse thing, and it is not offered.
        """
        cached = self._cache.get(rel_path)
        if not force and cached is not None:
            stamped, nodes = cached
            if self._monotonic() - stamped < self._ttl_s:
                return nodes

        endpoint = self._endpoint()
        if endpoint is None:
            return cached[1] if cached else []

        try:
            nodes = ops.list_dir(self.account.fs, rel_path, ep=endpoint,
                                 dirs_only=dirs_only, recurse=False)
        except (RcError, DaemonUnavailable, OSError) as exc:
            log.warning("could not list %s:%s", self.account.remote, rel_path,
                        exc_info=True)
            self.failed.emit(rel_path, str(exc))
            # The stale list is better than an empty tree: the folders were
            # there a minute ago, and blanking the picker mid-selection would
            # lose whatever the user had ticked.
            return cached[1] if cached else []

        self._cache[rel_path] = (self._monotonic(), nodes)
        self.listed.emit(rel_path, nodes)
        return nodes

    def stat(self, rel_path: str) -> RemoteFolderNode | None:
        """One item's metadata."""
        endpoint = self._endpoint()
        if endpoint is None:
            return None
        try:
            return ops.stat(self.account.fs, rel_path, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.debug("could not stat %s", rel_path, exc_info=True)
            return None

    def search(self, needle: str, *, under: str = "") -> list[RemoteFolderNode]:
        """Find folders by name, one level at a time.

        Args:
            needle: Case-insensitive substring.
            under: Where to start.

        Returns:
            Matching folders among the **already-listed** levels.

        Deliberately shallow: it searches what has been fetched rather than
        walking the drive, for the same ``ListR`` reason. A search that quietly
        issued a few thousand Graph requests would be the worst possible thing
        to put behind a text field that fires as the user types.
        """
        needle = needle.casefold()
        out: list[RemoteFolderNode] = []
        for path, (_stamped, nodes) in self._cache.items():
            if under and not path.startswith(under):
                continue
            out.extend(n for n in nodes if needle in n.name.casefold())
        return sorted(out, key=lambda n: n.rel_path)

    # ═════════════════════════════════════════════════════════════════════════
    # Size
    # ═════════════════════════════════════════════════════════════════════════

    def size(self, rel_path: str = "") -> JobHandle | None:
        """Start an asynchronous ``operations/size``. **Always async.**

        Args:
            rel_path: The folder to measure.

        Returns:
            The job handle to watch, or ``None``.

        Never synchronous. Without ``ListR`` this walks the entire subtree, one
        Graph request per directory, which takes minutes on a large folder — a
        blocking call would hit the four-second rc timeout every time and read
        as a broken client rather than a slow operation.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return None
        remote = f"{self.account.remote}:{rel_path}" if rel_path else self.account.fs
        try:
            return ops.size(remote, ep=endpoint)
        except (RcError, DaemonUnavailable, OSError):
            log.warning("could not start a size job for %s", rel_path,
                        exc_info=True)
            return None

    # ═════════════════════════════════════════════════════════════════════════
    # Cache
    # ═════════════════════════════════════════════════════════════════════════

    def invalidate(self, rel_path: str | None = None) -> None:
        """Drop cached listings.

        Args:
            rel_path: Just this directory and everything under it, or ``None``
                for the whole cache.
        """
        if rel_path is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache
                    if k == rel_path or k.startswith(f"{rel_path}/")]:
            self._cache.pop(key, None)

    @property
    def cached_paths(self) -> list[str]:
        """Which directories have been listed, for diagnostics."""
        return sorted(self._cache)
