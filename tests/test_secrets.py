"""Tests for `onedriveui.platform.secrets`.

Two behaviours matter more than the happy path:

* **`store()` raises, it does not return False.** A passphrase the user typed
  and believes is saved, but is not, locks them out of their own vault at the
  next launch. That is the only failure here that costs access to data, so it
  must be impossible to ignore.
* **`lookup()` never raises.** "Nothing stored" and "no keyring reachable" are
  the same fact to a caller — ask the user — so both answer `None`.

The live tests run against the real Secret Service on this machine
(gnome-keyring), under an unmistakably throwaway account id, and always clear
what they wrote in a `finally`. They skip cleanly on a machine with no keyring.
"""

from __future__ import annotations

import pytest

from onedriveui import APP_ID, APP_NAME
from onedriveui.errors import OneDriveUIError
from onedriveui.platform import secrets as S

#: Namespaced so a stray item is obviously ours and obviously disposable.
THROWAWAY = "pytest-wp10b-throwaway"


@pytest.fixture
def clean_keyring():
    """Remove the throwaway item before and after, whatever the test did."""
    S.clear(THROWAWAY)
    try:
        yield THROWAWAY
    finally:
        S.clear(THROWAWAY)


def _keyring_live() -> bool:
    return S.available()


live = pytest.mark.skipif(not _keyring_live(), reason="no Secret Service running")


# ═════════════════════════════════════════════════════════════════════════════
# Schema and attributes
# ═════════════════════════════════════════════════════════════════════════════

def test_schema_name_is_namespaced():
    assert S.SCHEMA_NAME.startswith("com.github.OneDriveUI")


def test_attributes_identify_one_secret():
    attrs = S.attributes("acct-1")
    assert attrs == {
        S.ATTR_APPLICATION: APP_ID,
        S.ATTR_KIND: S.KIND_VAULT,
        S.ATTR_ACCOUNT: "acct-1",
    }


def test_attributes_default_the_account():
    """A single-account install still needs a well-defined key."""
    assert S.attributes("")[S.ATTR_ACCOUNT] == S.DEFAULT_ACCOUNT
    assert S.attributes()[S.ATTR_ACCOUNT] == S.DEFAULT_ACCOUNT


def test_attributes_are_per_account():
    assert S.attributes("a") != S.attributes("b")


def test_label_says_what_the_secret_is_for():
    """An unlabelled keyring entry is one a user deletes."""
    text = S.label("acct-1")
    assert APP_NAME in text
    assert "Vault" in text
    assert "acct-1" in text


def test_only_the_vault_kind_exists():
    """The OAuth token lives in rclone.conf (I1); a second copy is a second leak."""
    assert S.KIND_VAULT == "vault-passphrase"


@live
def test_schema_builds_against_the_real_typelib():
    assert S.schema() is not None


# ═════════════════════════════════════════════════════════════════════════════
# Availability
# ═════════════════════════════════════════════════════════════════════════════

def test_available_agrees_with_unavailable_reason():
    assert S.available() == (S.unavailable_reason() == "")


def test_no_typelib_is_reported_clearly(monkeypatch):
    def no_typelib():
        raise OneDriveUIError(f"{S.NO_TYPELIB}: nope")

    monkeypatch.setattr(S, "_secret", no_typelib)

    assert S.available() is False
    assert S.NO_TYPELIB in S.unavailable_reason()


def test_no_service_is_reported_clearly(monkeypatch):
    from gi.repository import Gio, GLib

    class FakeSecret:
        ServiceFlags = type("F", (), {"NONE": 0})

        class Service:
            @staticmethod
            def get_sync(_flags, _cancellable):
                raise GLib.Error.new_literal(
                    Gio.io_error_quark(), "Cannot autolaunch D-Bus",
                    Gio.IOErrorEnum.FAILED)

    monkeypatch.setattr(S, "_secret", lambda: FakeSecret)

    assert S.available() is False
    reason = S.unavailable_reason()
    assert S.NO_SERVICE in reason
    assert "autolaunch" in reason


def test_a_none_service_is_unavailable(monkeypatch):
    class FakeSecret:
        ServiceFlags = type("F", (), {"NONE": 0})

        class Service:
            @staticmethod
            def get_sync(_flags, _cancellable):
                return None

    monkeypatch.setattr(S, "_secret", lambda: FakeSecret)
    assert S.unavailable_reason() == S.NO_SERVICE


def test_availability_is_not_cached(monkeypatch):
    """A keyring unlocked a moment ago must not need an application restart."""
    calls: list[int] = []

    def counting():
        calls.append(1)
        raise OneDriveUIError("nope")

    monkeypatch.setattr(S, "_secret", counting)
    S.available()
    S.available()
    assert len(calls) == 2


# ═════════════════════════════════════════════════════════════════════════════
# The clear failure mode
# ═════════════════════════════════════════════════════════════════════════════

def test_store_raises_when_libsecret_is_missing(monkeypatch):
    """Never a silent False: losing a typed passphrase locks the vault."""
    monkeypatch.setattr(S, "_secret",
                        lambda: (_ for _ in ()).throw(OneDriveUIError(S.NO_TYPELIB)))

    with pytest.raises(OneDriveUIError) as caught:
        S.store("hunter2", "acct")

    assert S.NO_TYPELIB in str(caught.value)


def test_store_raises_when_the_service_refuses(monkeypatch):
    from gi.repository import Gio, GLib

    class FakeSecret:
        COLLECTION_DEFAULT = "default"
        SchemaFlags = type("F", (), {"NONE": 0})
        SchemaAttributeType = type("A", (), {"STRING": 0})

        class Schema:
            @staticmethod
            def new(*_a):
                return object()

        @staticmethod
        def password_store_sync(*_a):
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), "keyring is locked", Gio.IOErrorEnum.FAILED)

    monkeypatch.setattr(S, "_secret", lambda: FakeSecret)

    with pytest.raises(OneDriveUIError) as caught:
        S.store("hunter2", "acct")

    assert "keyring is locked" in str(caught.value)


def test_store_raises_when_the_service_says_no(monkeypatch):
    class FakeSecret:
        COLLECTION_DEFAULT = "default"
        SchemaFlags = type("F", (), {"NONE": 0})
        SchemaAttributeType = type("A", (), {"STRING": 0})

        class Schema:
            @staticmethod
            def new(*_a):
                return object()

        @staticmethod
        def password_store_sync(*_a):
            return False

    monkeypatch.setattr(S, "_secret", lambda: FakeSecret)

    with pytest.raises(OneDriveUIError) as caught:
        S.store("hunter2", "acct")

    assert "locked" in str(caught.value)


def test_store_refuses_an_empty_passphrase():
    """Storing one would be indistinguishable from storing nothing."""
    with pytest.raises(ValueError):
        S.store("", "acct")


def test_lookup_never_raises(monkeypatch):
    monkeypatch.setattr(S, "_secret",
                        lambda: (_ for _ in ()).throw(OneDriveUIError("gone")))
    assert S.lookup("acct") is None
    assert S.has("acct") is False


def test_lookup_returns_none_on_a_glib_error(monkeypatch):
    from gi.repository import Gio, GLib

    class FakeSecret:
        SchemaFlags = type("F", (), {"NONE": 0})
        SchemaAttributeType = type("A", (), {"STRING": 0})

        class Schema:
            @staticmethod
            def new(*_a):
                return object()

        @staticmethod
        def password_lookup_sync(*_a):
            raise GLib.Error.new_literal(
                Gio.io_error_quark(), "locked", Gio.IOErrorEnum.FAILED)

    monkeypatch.setattr(S, "_secret", lambda: FakeSecret)
    assert S.lookup("acct") is None


def test_clear_never_raises(monkeypatch):
    monkeypatch.setattr(S, "_secret",
                        lambda: (_ for _ in ()).throw(OneDriveUIError("gone")))
    assert S.clear("acct") is False


def test_lookup_of_an_empty_stored_value_is_none(monkeypatch):
    class FakeSecret:
        SchemaFlags = type("F", (), {"NONE": 0})
        SchemaAttributeType = type("A", (), {"STRING": 0})

        class Schema:
            @staticmethod
            def new(*_a):
                return object()

        @staticmethod
        def password_lookup_sync(*_a):
            return ""

    monkeypatch.setattr(S, "_secret", lambda: FakeSecret)
    assert S.lookup("acct") is None


# ═════════════════════════════════════════════════════════════════════════════
# Live — the real keyring on this machine
# ═════════════════════════════════════════════════════════════════════════════

@live
def test_live_store_lookup_clear_round_trip(clean_keyring):
    account = clean_keyring
    assert S.lookup(account) is None
    assert S.has(account) is False

    assert S.store("correct horse battery staple", account) is True

    assert S.lookup(account) == "correct horse battery staple"
    assert S.has(account) is True
    assert S.clear(account) is True
    assert S.lookup(account) is None


@live
def test_live_store_overwrites(clean_keyring):
    account = clean_keyring
    S.store("first", account)
    S.store("second", account)
    assert S.lookup(account) == "second"


@live
def test_live_clear_of_a_missing_secret_is_false(clean_keyring):
    assert S.clear(clean_keyring) is False


@live
def test_live_accounts_do_not_collide(clean_keyring):
    other = clean_keyring + "-b"
    try:
        S.store("aaa", clean_keyring)
        S.store("bbb", other)
        assert S.lookup(clean_keyring) == "aaa"
        assert S.lookup(other) == "bbb"
    finally:
        S.clear(other)


@live
def test_live_a_unicode_passphrase_survives(clean_keyring):
    passphrase = "pässwörd — 日本語 🔐"
    S.store(passphrase, clean_keyring)
    assert S.lookup(clean_keyring) == passphrase
