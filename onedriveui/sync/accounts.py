"""The account registry, and the awkward business of finding out who you are.

Enumerating accounts is easy: ``config/listremotes``, filtered to
``type = onedrive``. Naming them is not, and the reason is worth stating,
because the workaround looks like a hack until you know why it is there.

**rclone cannot tell us who the user is.** ``Features.UserInfo`` is *false* for
the OneDrive backend, and ``rclone config userinfo`` errors out against it. The
display name and email that Windows shows at the top of its Activity Center are
simply not available through any rc call. Two routes are left:

1. **Capture it during OAuth**, where the token response carries it. This is the
   good path and the one :meth:`AccountManager.add` uses.
2. **Read it off a file the user owns.** Every item in OneDrive carries
   ``created-by-display-name`` metadata, so listing the drive root and taking the
   name from an item the user created recovers it for an account that was
   configured with ``rclone config`` before this client existed. It is a
   fallback, it can be wrong for a drive whose only items were shared in, and it
   is used only when route 1 has nothing.

The other thing this module is careful about is **unlinking**. "Unlink this PC"
removes credentials and tears down the units. It does not touch a single file
under ``sync_root``, and a test hashes the tree before and after to prove it.
Microsoft's own wording promises the files stay, and a client that quietly
deleted 200 GB because someone clicked "Unlink" would deserve everything that
followed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, Signal

from onedriveui.bus import BUS
from onedriveui.errors import DaemonUnavailable, RcError, SafetyRefusal
from onedriveui.models import AccountInfo, AccountKind, RcEndpoint, utcnow_iso
from onedriveui.rc import auth, conf, ops

log = logging.getLogger(__name__)

__all__ = ["AccountManager", "AccountRuntime", "ONEDRIVE_TYPE"]

#: The only ``rclone.conf`` remote type this client manages. A remote of any
#: other type in the same config file is left strictly alone: the user's
#: existing ``gdrive:`` or ``s3:`` remotes are not ours to enumerate, mount or
#: delete.
ONEDRIVE_TYPE = "onedrive"


def _default_sync_root(remote: str, index: int) -> str:
    """Where an account's folder goes when config has not said.

    The first account gets plain ``~/OneDrive``, matching Windows and matching
    what a user typing the path expects. Later ones are suffixed, because two
    accounts sharing a folder would bisync each other's files into both drives.
    """
    return "~/OneDrive" if index == 0 else f"~/OneDrive-{remote}"


@dataclass(slots=True)
class AccountRuntime:
    """The live objects belonging to one account.

    One of these per account, built by the application and handed back by
    :meth:`AccountManager.runtime`. It exists so that "the supervisor for this
    account" has a name, rather than being found by searching a list every time
    a tray menu is built.
    """

    account: AccountInfo
    supervisor: Any = None
    mount_endpoint: RcEndpoint | None = None
    services: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.account.id


class AccountManager(QObject):
    """Enumerate, identify, add and unlink OneDrive accounts.

    Args:
        endpoint: ``() -> RcEndpoint | None`` for the control-plane daemon.
        config_path: The ``rclone.conf`` to read. ``None`` uses rclone's own
            default location.
        sync_root_for: ``(remote, index) -> path``, so the configured sync root
            wins over the default naming. Injected because ``config.py`` owns
            that value and this module is not its author.
        writer: The database writer, for the ``accounts`` table.
        parent: Qt parent.

    Signals:
        added / updated / removed: Mirror the corresponding ``BUS`` signals.
    """

    added = Signal(AccountInfo)
    updated = Signal(AccountInfo)
    removed = Signal(str)

    def __init__(
        self,
        *,
        endpoint: Any = None,
        config_path: Any = None,
        sync_root_for: Any = None,
        stop_mount: Any = None,
        writer: Any = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._endpoint = endpoint or (lambda: None)
        self._config_path = config_path
        self._sync_root_for = sync_root_for or _default_sync_root
        #: ``(account) -> None`` stopping that account's mount unit. Injected,
        #: because this module must not depend on the rc layer.
        self._stop_mount = stop_mount
        self._writer = writer
        self._runtimes: dict[str, AccountRuntime] = {}

    # ═════════════════════════════════════════════════════════════════════════
    # Enumeration
    # ═════════════════════════════════════════════════════════════════════════

    def accounts(self) -> list[AccountInfo]:
        """Every OneDrive remote in ``rclone.conf``, in file order.

        Returns:
            One :class:`~onedriveui.models.AccountInfo` per remote whose
            ``type`` is ``onedrive``. Remotes of any other type are ignored
            entirely — this client does not manage, mount or list a user's
            unrelated remotes, and must never present them as OneDrive accounts.
        """
        found: list[AccountInfo] = []
        for remote in conf.remotes(self._config_path):
            if conf.remote_type(remote, self._config_path) != ONEDRIVE_TYPE:
                continue
            drive_type = conf.drive_type(remote, self._config_path)
            found.append(AccountInfo(
                id=remote,
                remote=remote,
                kind=(AccountKind.BUSINESS if drive_type == "business"
                      else AccountKind.PERSONAL),
                drive_type=drive_type,
                sync_root=self._sync_root_for(remote, len(found)),
            ))
        return found

    def primary(self) -> AccountInfo | None:
        """The first configured account, or ``None``.

        "Primary" is positional, not special: OneDrive has no notion of a main
        account, and the tray simply shows the first one when nothing else has
        been chosen.
        """
        accounts = self.accounts()
        return accounts[0] if accounts else None

    def runtime(self, account_id: str) -> AccountRuntime | None:
        """The live objects for an account, if it has been started."""
        return self._runtimes.get(account_id)

    def register_runtime(self, runtime: AccountRuntime) -> AccountRuntime:
        """Record an account's live objects."""
        self._runtimes[runtime.id] = runtime
        return runtime

    # ═════════════════════════════════════════════════════════════════════════
    # Identity
    # ═════════════════════════════════════════════════════════════════════════

    def resolve_identity(self, account: AccountInfo) -> AccountInfo:
        """Fill in the display name, the hard way.

        rclone's ``Features.UserInfo`` is **false** for OneDrive and
        ``config userinfo`` errors against it, so there is no call that answers
        "who is this?". What every OneDrive item does carry is
        ``created-by-display-name``, so listing the drive root and taking the
        name off an item the user created recovers it.

        Args:
            account: The account, possibly with no display name.

        Returns:
            A copy with ``display_name`` and ``drive_id`` filled in where they
            could be found, and unchanged where they could not. Never raises: an
            account with no name is a cosmetic problem, and failing to start
            because of one would be a real one.

        The fallback can be wrong. A drive whose visible items were all shared
        in by somebody else yields that person's name, so a name captured during
        OAuth always wins and this is only consulted when there is none.
        """
        if account.display_name:
            return account
        endpoint = self._endpoint()
        if endpoint is None:
            return account
        try:
            nodes = ops.list_dir(account.fs, "", ep=endpoint, metadata=True)
        except (RcError, DaemonUnavailable, OSError):
            log.debug("could not list %s to resolve its identity",
                      account.remote, exc_info=True)
            return account

        for node in nodes:
            if node.created_by:
                log.info("resolved %s's display name from item metadata: %r",
                         account.id, node.created_by)
                return self._with(account, display_name=node.created_by)
        log.debug("no item in %s carried a created-by name", account.remote)
        return account

    @staticmethod
    def _with(account: AccountInfo, **changes: Any) -> AccountInfo:
        from dataclasses import replace

        return replace(account, **changes)

    # ═════════════════════════════════════════════════════════════════════════
    # Adding and unlinking
    # ═════════════════════════════════════════════════════════════════════════

    def add(self, account: AccountInfo) -> AccountInfo:
        """Record a newly authorised account.

        Args:
            account: The account, with whatever the OAuth flow captured. The
                display name from the token response is authoritative and is
                never overwritten by the metadata fallback.

        Returns:
            The stored account.
        """
        stored = self._with(account, added_at=account.added_at or utcnow_iso())
        self._runtimes.setdefault(stored.id, AccountRuntime(account=stored))
        log.info("added account %s (%s)", stored.id, stored.kind.value)
        self.added.emit(stored)
        BUS.account_added.emit(stored)
        return stored

    def unlink(self, account: AccountInfo, *, keep_files: bool = True) -> bool:
        """Sign out and tear down. **Never touches the local folder.**

        Args:
            account: The account to unlink.
            keep_files: Always true, and not negotiable. It is a named argument
                rather than an absent one so that a caller reading the call site
                can see the promise being made; passing ``False`` raises rather
                than deleting anything.

        Returns:
            True when the credentials were removed.

        Raises:
            ValueError: ``keep_files=False``. There is no code path in this
                client that deletes a user's files on unlink, and Microsoft's own
                wording promises they stay.

        The order matters: units are stopped *before* the credentials go, so a
        running mount cannot notice its token vanish and start raising auth
        errors on the way out.
        """
        if not keep_files:
            raise ValueError(
                "unlink() never deletes files under sync_root; the user's "
                "OneDrive folder stays exactly as it is, as Microsoft's own "
                "unlink dialog promises")

        runtime = self._runtimes.pop(account.id, None)
        if runtime is not None and runtime.supervisor is not None:
            runtime.supervisor.stop()

        # The mount unit, which the docstring above promises is stopped before
        # the credentials go and which nothing was stopping. An `rclone mount`
        # left running against a remote that has just been deleted from
        # `rclone.conf` does not stop: it keeps serving the mountpoint and
        # raises auth errors on every operation, which is exactly the "notice
        # its token vanish" case the ordering exists to prevent.
        if self._stop_mount is not None:
            try:
                self._stop_mount(account)
            except SafetyRefusal:
                # Invariant I3: an upload is in flight, and those bytes exist
                # nowhere else. Deleting the credentials now would strand them
                # permanently — the mount would keep running with a token it can
                # no longer refresh. The unlink waits.
                log.error("not unlinking %s: the mount refused to stop while an "
                          "upload was in flight", account.id)
                raise
            except Exception:  # noqa: BLE001 - a stuck mount must not block unlink
                log.warning("could not stop the mount for %s before unlinking",
                            account.id, exc_info=True)

        endpoint = self._endpoint()
        removed = False
        if endpoint is not None:
            try:
                removed = auth.unlink_account(account.remote, ep=endpoint)
            except (RcError, DaemonUnavailable, OSError):
                log.error("could not remove %s from rclone.conf",
                          account.remote, exc_info=True)
        else:
            log.warning("no daemon to unlink %s through", account.remote)

        if not removed:
            # Saying "unlinked" when `config/delete` failed leaves the user
            # believing their credentials are gone from this machine when they
            # are still on disk. The mount is down either way, so the honest
            # report is that the account is still linked.
            log.error("could not unlink %s; its credentials are still in "
                      "rclone.conf", account.id)
            return False

        log.info("unlinked %s; every file under %s was left untouched",
                 account.id, account.sync_root)
        self.removed.emit(account.id)
        BUS.account_removed.emit(account.id)
        return removed
