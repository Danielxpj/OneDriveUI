"""WP-06 — `sync/accounts.py`.

Two properties carry this module.

**Unlink keeps the files.** Microsoft's own unlink dialog promises it, and the
test below hashes the whole tree before and after to prove we honour it. A client
that deleted 200 GB because someone clicked "Unlink this PC" would deserve
everything that followed.

**Identity comes the hard way.** rclone's `Features.UserInfo` is false for
OneDrive, so there is no call that answers "who is this?". The name is captured
at OAuth, and only when that has nothing do we fall back to reading
`created-by-display-name` off an item in the drive.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from onedriveui.errors import RcError
from onedriveui.models import AccountInfo, AccountKind, RcEndpoint, RemoteFolderNode
from onedriveui.rc import auth, ops
from onedriveui.sync.accounts import ONEDRIVE_TYPE, AccountManager, AccountRuntime

ENDPOINT = RcEndpoint(kind="rcd", port=17800)

RCLONE_CONF = """\
[onedrive]
type = onedrive
drive_type = personal
token = {"access_token":"redacted"}

[work]
type = onedrive
drive_type = business
token = {"access_token":"redacted"}

[backup]
type = s3
provider = AWS
"""


@pytest.fixture
def config_file(tmp_path) -> Path:
    path = tmp_path / "rclone.conf"
    path.write_text(RCLONE_CONF, encoding="utf-8")
    return path


def manager(config_file=None, **kwargs) -> AccountManager:
    kwargs.setdefault("endpoint", lambda: ENDPOINT)
    return AccountManager(config_path=config_file, **kwargs)


def tree_hash(root: Path) -> str:
    """A digest of every path and byte under `root`."""
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = Path(base) / name
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# Enumeration
# ═════════════════════════════════════════════════════════════════════════════

class TestAccounts:

    def test_only_onedrive_remotes_are_reported(self, qapp, config_file):
        """A user's unrelated `s3:` remote is not ours to enumerate, mount or
        delete, and must never be presented as a OneDrive account."""
        found = manager(config_file).accounts()
        assert [a.id for a in found] == ["onedrive", "work"]

    def test_the_drive_type_becomes_the_account_kind(self, qapp, config_file):
        """The tray paints a blue cloud for work/school and a white one for
        personal; a user with both tells them apart by exactly that."""
        found = {a.id: a for a in manager(config_file).accounts()}
        assert found["onedrive"].kind is AccountKind.PERSONAL
        assert found["work"].kind is AccountKind.BUSINESS

    def test_the_first_account_gets_the_plain_folder(self, qapp, config_file):
        found = manager(config_file).accounts()
        assert found[0].sync_root == "~/OneDrive"

    def test_later_accounts_get_their_own_folder(self, qapp, config_file):
        """Two accounts sharing a folder would sync each other's files into
        both drives."""
        found = manager(config_file).accounts()
        assert found[1].sync_root == "~/OneDrive-work"
        assert found[0].sync_root != found[1].sync_root

    def test_the_configured_root_wins(self, qapp, config_file):
        """`config.py` owns that value; this module is not its author."""
        mgr = manager(config_file, sync_root_for=lambda remote, i: f"/data/{remote}")
        assert manager(config_file).accounts()[0].sync_root == "~/OneDrive"
        assert mgr.accounts()[0].sync_root == "/data/onedrive"

    def test_primary_is_the_first_one(self, qapp, config_file):
        assert manager(config_file).primary().id == "onedrive"

    def test_no_accounts_is_not_an_error(self, qapp, tmp_path):
        empty = tmp_path / "empty.conf"
        empty.write_text("", encoding="utf-8")
        mgr = manager(empty)
        assert mgr.accounts() == []
        assert mgr.primary() is None

    def test_the_type_filter_is_declared(self):
        assert ONEDRIVE_TYPE == "onedrive"


class TestRuntimes:

    def test_a_runtime_is_findable_by_id(self, qapp, config_file):
        mgr = manager(config_file)
        account = mgr.accounts()[0]
        runtime = mgr.register_runtime(AccountRuntime(account=account))
        assert mgr.runtime(account.id) is runtime

    def test_an_unknown_id_answers_none(self, qapp, config_file):
        assert manager(config_file).runtime("nope") is None


# ═════════════════════════════════════════════════════════════════════════════
# Identity
# ═════════════════════════════════════════════════════════════════════════════

class TestResolveIdentity:

    def test_a_name_captured_at_oauth_wins(self, qapp, config_file, monkeypatch):
        """The metadata fallback can be wrong — a drive whose visible items were
        all shared in yields somebody else's name — so it is never consulted
        when the good path already answered."""
        monkeypatch.setattr(ops, "list_dir",
                            lambda *a, **kw: pytest.fail("asked despite a name"))
        account = AccountInfo(id="onedrive", remote="onedrive",
                              display_name="Real Name")
        assert manager(config_file).resolve_identity(account).display_name == "Real Name"

    def test_the_fallback_reads_created_by_off_an_item(self, qapp, config_file,
                                                       monkeypatch):
        """rclone's `Features.UserInfo` is false for OneDrive and
        `config userinfo` errors, so this is the only route left for an account
        configured with `rclone config` before this client existed."""
        monkeypatch.setattr(ops, "list_dir", lambda *a, **kw: [
            RemoteFolderNode(rel_path="a.txt", name="a.txt"),
            RemoteFolderNode(rel_path="b.txt", name="b.txt",
                             created_by="Daniel Dughman"),
        ])
        account = AccountInfo(id="onedrive", remote="onedrive")
        resolved = manager(config_file).resolve_identity(account)
        assert resolved.display_name == "Daniel Dughman"

    def test_an_empty_drive_leaves_the_name_empty(self, qapp, config_file, monkeypatch):
        monkeypatch.setattr(ops, "list_dir", lambda *a, **kw: [])
        account = AccountInfo(id="onedrive", remote="onedrive")
        assert not manager(config_file).resolve_identity(account).display_name

    def test_a_failed_listing_is_not_fatal(self, qapp, config_file, monkeypatch):
        """An account with no display name is a cosmetic problem; failing to
        start because of one would be a real one."""
        def explode(*a, **kw):
            raise RcError("operations/list", 500, {"error": "no"})

        monkeypatch.setattr(ops, "list_dir", explode)
        account = AccountInfo(id="onedrive", remote="onedrive")
        assert not manager(config_file).resolve_identity(account).display_name

    def test_no_daemon_leaves_it_alone(self, qapp, config_file):
        mgr = manager(config_file, endpoint=lambda: None)
        account = AccountInfo(id="onedrive", remote="onedrive")
        assert not mgr.resolve_identity(account).display_name


# ═════════════════════════════════════════════════════════════════════════════
# Adding and unlinking
# ═════════════════════════════════════════════════════════════════════════════

class TestAdd:

    def test_adding_stamps_the_time_and_announces_it(self, qapp, config_file, bus_spy):
        bus_spy.watch("account_added")
        account = AccountInfo(id="onedrive", remote="onedrive")
        stored = manager(config_file).add(account)
        assert stored.added_at
        assert bus_spy.count("account_added") == 1

    def test_an_existing_timestamp_is_kept(self, qapp, config_file):
        account = AccountInfo(id="onedrive", remote="onedrive",
                              added_at="2026-01-01T00:00:00Z")
        assert manager(config_file).add(account).added_at == "2026-01-01T00:00:00Z"


class TestUnlink:

    def test_the_local_folder_is_untouched(self, qapp, config_file, tmp_path,
                                           monkeypatch):
        """The headline promise, proved by hashing the tree before and after."""
        root = tmp_path / "OneDrive"
        (root / "Photos").mkdir(parents=True)
        (root / "Photos" / "holiday.jpg").write_bytes(b"\xff\xd8jpeg")
        (root / "notes.txt").write_text("keep me", encoding="utf-8")
        before = tree_hash(root)

        monkeypatch.setattr(auth, "unlink_account", lambda *a, **kw: True)
        account = AccountInfo(id="onedrive", remote="onedrive",
                              sync_root=str(root))
        assert manager(config_file).unlink(account) is True

        assert tree_hash(root) == before
        assert (root / "Photos" / "holiday.jpg").exists()

    def test_keep_files_false_is_refused_rather_than_honoured(self, qapp, config_file):
        """There is no code path in this client that deletes a user's files on
        unlink, so the argument that would ask for it raises instead."""
        account = AccountInfo(id="onedrive", remote="onedrive")
        with pytest.raises(ValueError, match="never deletes"):
            manager(config_file).unlink(account, keep_files=False)

    def test_the_supervisor_is_stopped_before_the_credentials_go(
            self, qapp, config_file, monkeypatch):
        """A running mount must not notice its token vanish and start raising
        auth errors on the way out."""
        order: list[str] = []

        class Supervisor:
            def stop(self):
                order.append("stop")

        monkeypatch.setattr(auth, "unlink_account",
                            lambda *a, **kw: order.append("unlink") or True)
        mgr = manager(config_file)
        account = AccountInfo(id="onedrive", remote="onedrive")
        mgr.register_runtime(AccountRuntime(account=account, supervisor=Supervisor()))
        mgr.unlink(account)
        assert order == ["stop", "unlink"]

    def test_the_bus_is_told(self, qapp, config_file, monkeypatch, bus_spy):
        bus_spy.watch("account_removed")
        monkeypatch.setattr(auth, "unlink_account", lambda *a, **kw: True)
        account = AccountInfo(id="onedrive", remote="onedrive")
        manager(config_file).unlink(account)
        assert bus_spy.last("account_removed") == ("onedrive",)

    def test_a_failed_removal_still_tears_down_locally(self, qapp, config_file,
                                                       monkeypatch):
        """Leaving a half-unlinked account running would keep hammering a token
        the user has told us to forget."""
        def explode(*a, **kw):
            raise RcError("config/delete", 500, {"error": "no"})

        monkeypatch.setattr(auth, "unlink_account", explode)
        mgr = manager(config_file)
        account = AccountInfo(id="onedrive", remote="onedrive")
        mgr.register_runtime(AccountRuntime(account=account))
        assert mgr.unlink(account) is False
        assert mgr.runtime("onedrive") is None

    def test_no_daemon_is_not_a_crash(self, qapp, config_file):
        mgr = manager(config_file, endpoint=lambda: None)
        account = AccountInfo(id="onedrive", remote="onedrive")
        assert mgr.unlink(account) is False
