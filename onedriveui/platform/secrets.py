"""The vault passphrase, in the login keyring — and nothing else.

This module stores exactly one class of secret: the passphrase for the
gocryptfs Personal Vault container. It deliberately does **not** touch the
OneDrive OAuth token. `rclone.conf` owns that (invariant I1: backend options and
credentials live there and nowhere else), rclone refreshes it in place, and a
second copy in the keyring would be a second thing to leak, to expire and to get
out of sync.

The transport is libsecret through GObject introspection —
`Secret.password_store_sync` / `password_lookup_sync` / `password_clear_sync` —
which talks the `org.freedesktop.Secret` D-Bus API to whatever Secret Service is
running (gnome-keyring on the target machine, KWallet elsewhere). Attributes on
each item make it findable: `application`, `kind`, and the account id, so a
multi-account install stores one passphrase per account.

## When there is no keyring

This is the failure mode that has to be *clear*, because it is common:
headless sessions, a locked keyring, and distributions where no Secret Service
is installed all present the same way. So:

* `available()` answers the question up front, with a real round trip, and
  caches nothing that could go stale across a keyring unlock.
* `lookup()` returns `None` — indistinguishable from "no passphrase stored",
  which is correct, because in both cases the UI must ask the user.
* `store()` **raises `OneDriveUIError`** rather than returning False. Silently
  failing to save a passphrase the user just typed is the one outcome that
  loses data: they would set up a vault, restart, and find themselves locked
  out of a container whose passphrase they believed was saved.
* `unavailable_reason()` gives the UI something specific to show instead of a
  generic failure.

Threading: libsecret's `*_sync` calls block on D-Bus. They are fast against a
running keyring, but a locked one prompts the user, so treat every call here as
potentially slow and never make one on a paint path. `Secret` is a GObject
library sharing GLib's default main context, so calls belong on the GUI thread
alongside everything else in this package.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from onedriveui import APP_ID, APP_NAME
from onedriveui.errors import OneDriveUIError

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

#: The libsecret schema name. Namespaced so nothing else can collide with it and
#: so `secret-tool search` finds our items by name.
SCHEMA_NAME: Final[str] = "com.github.OneDriveUI.Secret"

#: Attribute keys. Every stored item carries all three, which is what makes
#: lookup exact rather than a search.
ATTR_APPLICATION: Final[str] = "application"
ATTR_KIND: Final[str] = "kind"
ATTR_ACCOUNT: Final[str] = "account"

#: `kind` values. Only one exists today, and the constant is here so a second
#: kind can never be added by typing a bare string at a call site.
KIND_VAULT: Final[str] = "vault-passphrase"

#: The value of the `application` attribute on every item we own.
APPLICATION: Final[str] = APP_ID

#: The label the user sees in Seahorse / KWalletManager. It should say what the
#: secret is for, because an unlabelled entry is one a user deletes.
LABEL_TEMPLATE: Final[str] = f"{APP_NAME} Personal Vault passphrase ({{account}})"

#: Used when no account id is given, so a single-account install still has a
#: well-defined key rather than an empty attribute.
DEFAULT_ACCOUNT: Final[str] = "default"

#: What `unavailable_reason()` reports when `gi` has no `Secret` typelib at all
#: — the `libsecret` package is not installed.
NO_TYPELIB: Final[str] = "libsecret (gir1.2-secret-1 / libsecret) is not installed"

#: What it reports when the typelib is present but no service answered.
NO_SERVICE: Final[str] = "no keyring is running on the session bus"


def _secret() -> Any:
    """Import the `Secret` typelib.

    Returns:
        The `Secret` module.

    Raises:
        OneDriveUIError: If the typelib is missing, which means libsecret is not
            installed.
    """
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except (ImportError, ValueError) as exc:
        raise OneDriveUIError(f"{NO_TYPELIB}: {exc}") from exc
    return Secret


def _glib() -> Any:
    """The `GLib` module, for its error type.

    Returns:
        The `GLib` module.
    """
    from gi.repository import GLib

    return GLib


def schema() -> Any:
    """The libsecret schema describing our items.

    Built fresh on each call rather than cached at import time: constructing it
    touches the typelib, and this module must be importable on a machine with no
    libsecret at all so that `available()` can report that cleanly.

    Returns:
        A `Secret.Schema`.

    Raises:
        OneDriveUIError: If libsecret is not installed.
    """
    secret = _secret()
    return secret.Schema.new(
        SCHEMA_NAME,
        secret.SchemaFlags.NONE,
        {
            ATTR_APPLICATION: secret.SchemaAttributeType.STRING,
            ATTR_KIND: secret.SchemaAttributeType.STRING,
            ATTR_ACCOUNT: secret.SchemaAttributeType.STRING,
        },
    )


def attributes(account_id: str = "", *, kind: str = KIND_VAULT) -> dict[str, str]:
    """The attribute set identifying one stored secret.

    Args:
        account_id: The account the secret belongs to. Empty means
            `DEFAULT_ACCOUNT`.
        kind: Which secret. Only `KIND_VAULT` exists today.

    Returns:
        `{application, kind, account}`.
    """
    return {
        ATTR_APPLICATION: APPLICATION,
        ATTR_KIND: kind,
        ATTR_ACCOUNT: account_id or DEFAULT_ACCOUNT,
    }


def label(account_id: str = "") -> str:
    """The human-readable label shown in a keyring manager.

    Args:
        account_id: The account the secret belongs to.

    Returns:
        The label.
    """
    return LABEL_TEMPLATE.format(account=account_id or DEFAULT_ACCOUNT)


# ═════════════════════════════════════════════════════════════════════════════
# Availability
# ═════════════════════════════════════════════════════════════════════════════

def unavailable_reason() -> str:
    """Why secrets cannot be stored, in words the UI can show.

    Performs a real round trip to the Secret Service rather than trusting a
    cached answer: a keyring that was locked a minute ago may be unlocked now,
    and the user must not have to restart the application after unlocking it.

    Returns:
        `""` when the keyring is usable, otherwise a one-line explanation.
    """
    try:
        secret = _secret()
    except OneDriveUIError as exc:
        return str(exc)
    glib = _glib()
    try:
        service = secret.Service.get_sync(secret.ServiceFlags.NONE, None)
    except glib.Error as exc:
        return f"{NO_SERVICE}: {exc.message}"
    if service is None:
        return NO_SERVICE
    return ""


def available() -> bool:
    """Whether a keyring is reachable right now.

    Returns:
        True if a secret can be stored and read back.
    """
    return not unavailable_reason()


# ═════════════════════════════════════════════════════════════════════════════
# The three operations
# ═════════════════════════════════════════════════════════════════════════════

def store(passphrase: str, account_id: str = "", *,
          kind: str = KIND_VAULT,
          collection: str | None = None) -> bool:
    """Save a secret in the login keyring.

    Raises rather than returning False on failure. A passphrase the user just
    typed and believes is saved, but is not, locks them out of their own vault
    at the next launch — the one failure here that destroys access to data.

    Args:
        passphrase: The secret. An empty string is rejected: storing one would
            be indistinguishable from having no passphrase at all.
        account_id: The account it belongs to.
        kind: Which secret. Only `KIND_VAULT` exists today.
        collection: A libsecret collection name, or `None` for the default
            login keyring.

    Returns:
        True on success.

    Raises:
        ValueError: If `passphrase` is empty.
        OneDriveUIError: If libsecret is missing, no keyring is running, or the
            service refused to store the item. The message says which.
    """
    if not passphrase:
        raise ValueError("refusing to store an empty passphrase")
    secret = _secret()
    glib = _glib()
    target = collection if collection is not None else secret.COLLECTION_DEFAULT
    try:
        ok = secret.password_store_sync(
            schema(), attributes(account_id, kind=kind), target,
            label(account_id), passphrase, None,
        )
    except glib.Error as exc:
        raise OneDriveUIError(
            f"could not save the passphrase to the keyring: {exc.message}"
        ) from exc
    if not ok:
        raise OneDriveUIError(
            "the keyring refused to save the passphrase; it may be locked")
    log.info("stored the %s secret for account %r", kind, account_id or DEFAULT_ACCOUNT)
    return True


def lookup(account_id: str = "", *, kind: str = KIND_VAULT) -> str | None:
    """Read a secret back.

    Args:
        account_id: The account it belongs to.
        kind: Which secret.

    Returns:
        The passphrase, or `None` when nothing is stored **or** no keyring is
        reachable. Both mean the same thing to the caller: ask the user.
    """
    try:
        secret = _secret()
    except OneDriveUIError as exc:
        log.info("cannot read the keyring: %s", exc)
        return None
    glib = _glib()
    try:
        value = secret.password_lookup_sync(
            schema(), attributes(account_id, kind=kind), None)
    except glib.Error as exc:
        log.info("keyring lookup failed: %s", exc.message)
        return None
    return value if value else None


def clear(account_id: str = "", *, kind: str = KIND_VAULT) -> bool:
    """Delete a stored secret.

    Args:
        account_id: The account it belongs to.
        kind: Which secret.

    Returns:
        True if an item was removed. False when there was nothing to remove or
        no keyring is reachable — neither is an error, because the outcome the
        caller wanted (no stored secret) holds either way.
    """
    try:
        secret = _secret()
    except OneDriveUIError as exc:
        log.info("cannot clear the keyring: %s", exc)
        return False
    glib = _glib()
    try:
        removed = secret.password_clear_sync(
            schema(), attributes(account_id, kind=kind), None)
    except glib.Error as exc:
        log.info("keyring clear failed: %s", exc.message)
        return False
    if removed:
        log.info("cleared the %s secret for account %r",
                 kind, account_id or DEFAULT_ACCOUNT)
    return bool(removed)


def has(account_id: str = "", *, kind: str = KIND_VAULT) -> bool:
    """Whether a secret is stored.

    Args:
        account_id: The account it belongs to.
        kind: Which secret.

    Returns:
        True if a passphrase can be read back.
    """
    return lookup(account_id, kind=kind) is not None


__all__ = [
    "SCHEMA_NAME", "ATTR_APPLICATION", "ATTR_KIND", "ATTR_ACCOUNT",
    "KIND_VAULT", "APPLICATION", "LABEL_TEMPLATE", "DEFAULT_ACCOUNT",
    "NO_TYPELIB", "NO_SERVICE",
    "schema", "attributes", "label",
    "available", "unavailable_reason", "store", "lookup", "clear", "has",
]
