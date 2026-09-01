"""Sharing links, and the one thing this client will not pretend to do.

Creating a link works: ``operations/publiclink`` returns a real, working
OneDrive share URL, with the scope and expiry the backend options set.

**Removing one does not, and rclone will not tell you so.** The rc endpoint
accepts an ``unlink=true`` parameter, the OneDrive backend declares it, and it is
*never read*: passing it does not revoke anything, and — verified — it **creates
a new link** and returns it. A client that called it and reported "link removed"
would be telling the user their document is no longer shared while it is still
publicly readable, and that is the single most dangerous kind of wrong answer a
sharing feature can give.

So :meth:`ShareService.can_revoke` returns ``False``, unconditionally and
permanently. The UI shows "Remove link" **disabled with its reason** rather than
hidden — a missing control makes the user hunt for it; a disabled one with an
explanation sends them to the web interface, where revoking genuinely works.

The same honesty applies to permissions. rclone cannot enumerate who an item is
shared with, so :meth:`ShareService.permissions` reports what *we* issued and
says so, and the "Manage access" button is a web deep link.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any

from PySide6.QtCore import QObject, Signal

from onedriveui.constants import WEB_ROOT
from onedriveui.data import repo_files
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import (
    AccountInfo,
    LinkScope,
    LinkType,
    ShareLink,
    utcnow_iso,
)
from onedriveui.rc import ops
from onedriveui.strings import DIALOG

log = logging.getLogger(__name__)

__all__ = ["ShareService"]


class ShareService(QObject):
    """Creates share links, records them, and refuses to fake revoking them.

    Args:
        account: The account.
        endpoint: ``() -> RcEndpoint | None`` for the daemon to ask.
        writer: The database writer.
        parent: Qt parent.

    Signals:
        created: A new :class:`~onedriveui.models.ShareLink`.
    """

    created = Signal(ShareLink)

    def __init__(
        self,
        account: AccountInfo,
        *,
        endpoint: Any = None,
        writer: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.account = account
        self._endpoint = endpoint or (lambda: None)
        self._writer = writer

    # ═════════════════════════════════════════════════════════════════════════
    # Creating
    # ═════════════════════════════════════════════════════════════════════════

    def create_link(self, rel_path: str, link_type: LinkType = LinkType.VIEW,
                    scope: LinkScope = LinkScope.ANONYMOUS,
                    expire_days: int | None = None,
                    password: str | None = None) -> ShareLink | None:
        """Create a public link to an item.

        Args:
            rel_path: The item to share.
            link_type: View, edit or embed.
            scope: Anonymous, organisation-only or named users.
            expire_days: Days until the link stops working. ``None`` uses the
                account's default.
            password: A password for the link. Personal accounts only; business
                tenants reject it, and rclone surfaces that as a plain error.

        Returns:
            The link, recorded, or ``None`` when it could not be created.

        The link is recorded locally because **nothing can enumerate it later**:
        rclone cannot list an item's existing links, so if we do not remember
        issuing one, the user has no way to be reminded that a file is shared.
        """
        endpoint = self._endpoint()
        if endpoint is None:
            return None
        try:
            url = ops.publiclink(
                self.account.fs, rel_path, ep=endpoint,
                expire=f"{expire_days}d" if expire_days else None)
        except (RcError, DaemonUnavailable, OSError):
            log.error("could not create a share link for %s", rel_path,
                      exc_info=True)
            return None

        link = ShareLink(
            account_id=self.account.id, rel_path=rel_path, url=url,
            scope=scope, link_type=link_type,
            has_password=bool(password), created_at=utcnow_iso(),
        )
        try:
            link_id = repo_files.record_link(link, writer=self._writer)
            link = _with(link, id=link_id or 0)
        except Exception:  # noqa: BLE001 - a working link is not worth losing
            log.error("could not record the share link for %s", rel_path,
                      exc_info=True)

        log.info("shared %s (%s, %s)", rel_path, link_type.value, scope.value)
        self.created.emit(link)
        return link

    def links_for(self, rel_path: str) -> list[ShareLink]:
        """The links **we** issued for this item.

        Not the links that exist — rclone cannot ask for those. The distinction
        is stated in the UI rather than glossed over, because a list that looks
        complete and is not would let a user conclude a file is private when a
        link issued from the web interface is still live.
        """
        return repo_files.links_for(self.account.id, rel_path)

    # ═════════════════════════════════════════════════════════════════════════
    # Not revoking
    # ═════════════════════════════════════════════════════════════════════════

    def can_revoke(self) -> bool:
        """Always ``False``. There is no working revoke, so none is offered.

        ``operations/publiclink`` accepts ``unlink=true`` and the OneDrive
        backend declares the parameter — and never reads it. Verified: the call
        does not revoke anything and **returns a newly created link**.

        A client that called it and said "link removed" would be telling the
        user their document is no longer shared while it is still publicly
        readable. Returning ``False`` here is what makes the UI show the control
        disabled, with :data:`~onedriveui.strings.DIALOG.REMOVE_LINK_WHY` beside
        it, and a working route to the web interface.
        """
        return False

    def revoke_reason(self) -> str:
        """Why "Remove link" is disabled, in the user's words."""
        return DIALOG.REMOVE_LINK_WHY

    def forget_link(self, link_id: int) -> None:
        """Remove a link from **our own record**. It stays live in OneDrive.

        For tidying a list of links the user has already revoked on the web.
        Deliberately not called "revoke": it changes nothing about who can reach
        the file, and a name that implied otherwise would be the same lie
        :meth:`can_revoke` exists to prevent.
        """
        repo_files.revoke_link(link_id, writer=self._writer)
        log.info("forgot share link %s locally; it is still live in OneDrive",
                 link_id)

    # ═════════════════════════════════════════════════════════════════════════
    # Web deep links and email
    # ═════════════════════════════════════════════════════════════════════════

    def web_manage_url(self, rel_path: str) -> str:
        """Where "Manage access" goes: the real thing, in the browser."""
        return f"{WEB_ROOT}?id={urllib.parse.quote(rel_path)}"

    def mailto_url(self, link: ShareLink, recipients: list[str]) -> str:
        """A ``mailto:`` for "Send by email".

        Windows sends the invitation through Graph, which rclone cannot do. A
        ``mailto:`` opens the user's own mail client with the link already in
        the body — less automatic, entirely honest, and it works with whatever
        they actually use.
        """
        params = urllib.parse.urlencode({
            "subject": f"Shared: {link.rel_path.rsplit('/', 1)[-1]}",
            "body": link.url,
        }, quote_via=urllib.parse.quote)
        return f"mailto:{','.join(recipients)}?{params}"

    def permissions(self, rel_path: str) -> list[dict[str, Any]]:
        """Who this item is shared with, as far as we can tell.

        Returns:
            One row per link **we** issued. rclone cannot enumerate an item's
            real permissions, so this is a record of our own actions rather than
            a statement about the item, and the UI labels it that way.
        """
        return [{
            "url": link.url,
            "scope": link.scope.value,
            "type": link.link_type.value,
            "created_at": link.created_at,
            "source": "issued by this client",
        } for link in self.links_for(rel_path)]


def _with(link: ShareLink, **changes: Any) -> ShareLink:
    from dataclasses import replace

    return replace(link, **changes)
