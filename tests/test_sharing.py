"""WP-09 — `sync/sharing.py`, `sync/trashbin.py`, `sync/versions.py`.

Three refusals carry these modules:

* **`can_revoke()` is False, always.** rclone's `unlink=true` is a verified
  no-op that *creates* a link. Reporting "link removed" would tell the user their
  document is private while it is still publicly readable.
* **`operations/cleanup` is never called.** On OneDrive it permanently deletes
  every previous version of every file, and it is unsupported on Personal
  accounts entirely.
* **A restore captures the current copy first.** Otherwise "restore" is a
  destructive, unrepeatable operation in a version-history feature.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from onedriveui import paths
from onedriveui.constants import (
    REMOTE_TRASH_DIR,
    REMOTE_VERSIONS_DIR,
    TRASH_RETENTION_DAYS_BUSINESS,
    TRASH_RETENTION_DAYS_PERSONAL,
)
from onedriveui.data import db, repo_files
from onedriveui.data.writer import DbWriter
from onedriveui.errors import RcError
from onedriveui.models import (
    AccountInfo,
    AccountKind,
    LinkScope,
    LinkType,
    RcEndpoint,
    RemoteFolderNode,
    RunKind,
    RunRecord,
    ShareLink,
    TrashEntry,
    VersionEntry,
    utcnow_iso,
)
from onedriveui.rc import ops
from onedriveui.sync import trashbin as trashbin_mod
from onedriveui.sync import versions as versions_mod
from onedriveui.sync.sharing import ShareService
from onedriveui.sync.trashbin import TrashBin, retention_days, trash_path_for
from onedriveui.sync.versions import VersionStore, run_suffix

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")
BUSINESS = AccountInfo(id="work", remote="work", kind=AccountKind.BUSINESS,
                       sync_root="/tmp/Work")
ENDPOINT = RcEndpoint(kind="rcd", port=17800)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def store(_isolate_home, qapp):
    writer = DbWriter(paths.db_file())
    assert writer.start_writer()
    writer.submit_sync(
        lambda conn: conn.execute(
            "INSERT INTO accounts (id, remote, sync_root, added_at) VALUES (?,?,?,?)",
            (ACCOUNT.id, ACCOUNT.remote, ACCOUNT.sync_root, utcnow_iso())),
        urgent=True)
    try:
        yield writer
    finally:
        writer.stop()
        db.close_all()


# ═════════════════════════════════════════════════════════════════════════════
# Sharing
# ═════════════════════════════════════════════════════════════════════════════

class TestShareLinks:

    def service(self, store) -> ShareService:
        return ShareService(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)

    def test_a_link_is_created_and_recorded(self, qapp, store, monkeypatch):
        """Recorded locally because nothing can enumerate it later: rclone
        cannot list an item's existing links, so if we do not remember issuing
        one the user can never be reminded the file is shared."""
        monkeypatch.setattr(ops, "publiclink",
                            lambda fs, remote, **kw: "https://1drv.ms/x/abc")
        link = self.service(store).create_link("Docs/a.docx")
        assert link is not None
        assert link.url == "https://1drv.ms/x/abc"
        store.flush()
        assert [x.url for x in self.service(store).links_for("Docs/a.docx")] == \
            ["https://1drv.ms/x/abc"]

    def test_the_expiry_is_passed_through(self, qapp, store, monkeypatch):
        seen: list[str | None] = []

        def fake(fs, remote, *, ep, expire=None, unlink=False, timeout_s=None):
            seen.append(expire)
            return "https://1drv.ms/x/abc"

        monkeypatch.setattr(ops, "publiclink", fake)
        self.service(store).create_link("a.docx", expire_days=7)
        assert seen == ["7d"]

    def test_unlink_is_never_requested(self, qapp, store, monkeypatch):
        """The parameter is declared, accepted, never read — and passing it
        CREATES a link. No code path may send it."""
        def fake(fs, remote, *, ep, expire=None, unlink=False, timeout_s=None):
            assert unlink is False, "sent unlink=true, which creates a link"
            return "https://1drv.ms/x/abc"

        monkeypatch.setattr(ops, "publiclink", fake)
        self.service(store).create_link("a.docx")

    def test_no_call_anywhere_passes_unlink_true(self):
        """Checked with the AST, not a grep, so that the prose explaining *why*
        it is forbidden does not count as an offence — and so that a future edit
        that quietly reintroduces the call does."""
        offenders = []
        for path in (REPO_ROOT / "onedriveui").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "unlink":
                        continue
                    value = keyword.value
                    if isinstance(value, ast.Constant) and value.value:
                        offenders.append(f"{path}:{node.lineno}")
        assert offenders == []

    def test_a_failure_answers_none(self, qapp, store, monkeypatch):
        def explode(*a, **kw):
            raise RcError("operations/publiclink", 500, {"error": "no"})

        monkeypatch.setattr(ops, "publiclink", explode)
        assert self.service(store).create_link("a.docx") is None


class TestCannotRevoke:

    def test_can_revoke_is_always_false(self, qapp, store):
        """Not "usually", not "unless the backend supports it". Always."""
        service = ShareService(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        assert service.can_revoke() is False

    def test_the_reason_comes_from_strings(self, qapp, store):
        """The control is shown DISABLED WITH ITS REASON, not hidden: a missing
        control makes the user hunt for it, and a disabled one with an
        explanation sends them to the web, where revoking genuinely works."""
        from onedriveui.strings import DIALOG

        service = ShareService(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        assert service.revoke_reason() == DIALOG.REMOVE_LINK_WHY
        assert "website" in service.revoke_reason().lower()

    def test_forgetting_a_link_is_not_called_revoking(self, qapp, store,
                                                      monkeypatch):
        """It changes nothing about who can reach the file, and a name implying
        otherwise would be the same lie `can_revoke()` exists to prevent."""
        service = ShareService(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        assert not hasattr(service, "revoke")
        assert hasattr(service, "forget_link")

    def test_permissions_are_labelled_as_our_own_record(self, qapp, store,
                                                        monkeypatch):
        monkeypatch.setattr(ops, "publiclink",
                            lambda fs, remote, **kw: "https://1drv.ms/x/abc")
        service = ShareService(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        service.create_link("a.docx")
        store.flush()
        rows = service.permissions("a.docx")
        assert rows[0]["source"] == "issued by this client"


class TestMailto:

    def test_it_carries_the_link(self, qapp, store):
        service = ShareService(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        link = ShareLink(account_id=ACCOUNT.id, rel_path="Docs/Report.docx",
                         url="https://1drv.ms/x/abc", scope=LinkScope.ANONYMOUS,
                         link_type=LinkType.VIEW)
        url = service.mailto_url(link, ["a@example.com", "b@example.com"])
        assert url.startswith("mailto:a@example.com,b@example.com?")
        assert "1drv.ms" in url
        assert "Report.docx" in url


# ═════════════════════════════════════════════════════════════════════════════
# Trash
# ═════════════════════════════════════════════════════════════════════════════

class TestSoftDelete:

    def bin_(self, store, account=ACCOUNT) -> TrashBin:
        return TrashBin(account, endpoint=lambda: ENDPOINT, writer=store)

    def test_exactly_one_move_and_zero_deletes(self, qapp, store, monkeypatch):
        """The BUILD_PLAN's acceptance case. Server-side, so a 4 GB file is
        instant and nothing crosses the network."""
        moves: list[tuple[str, str]] = []
        monkeypatch.setattr(ops, "movefile",
                            lambda sf, sr, df, dr, **kw: moves.append((sr, dr)))
        monkeypatch.setattr(ops, "deletefile",
                            lambda *a, **kw: pytest.fail("deleted on a soft delete"))
        monkeypatch.setattr(ops, "purge",
                            lambda *a, **kw: pytest.fail("purged on a soft delete"))

        entry = self.bin_(store).soft_delete("Docs/a.txt")
        assert entry is not None
        assert len(moves) == 1
        assert moves[0][0] == "Docs/a.txt"
        assert moves[0][1].startswith(f"{REMOTE_TRASH_DIR}/")

    def test_the_timestamp_directory_prevents_collisions(self):
        """Two files with the same name deleted an hour apart land in different
        folders, so restoring the first cannot overwrite the second."""
        first = trash_path_for("a.txt", "2026-08-31T12:00:00Z")
        second = trash_path_for("a.txt", "2026-08-31T13:00:00Z")
        assert first != second
        assert first.endswith("/a.txt") and second.endswith("/a.txt")

    def test_the_original_path_is_preserved_underneath(self):
        assert trash_path_for("Docs/2026/a.txt", "2026-08-31T12:00:00Z").endswith(
            "/Docs/2026/a.txt")

    def test_no_colon_reaches_the_remote_path(self):
        """A colon is invalid in a OneDrive path; a trash directory named with
        one would be unsyncable by the sync that created it."""
        assert ":" not in trash_path_for("a.txt", "2026-08-31T12:00:00Z")

    def test_the_retention_matches_microsofts(self):
        assert retention_days(ACCOUNT) == TRASH_RETENTION_DAYS_PERSONAL
        assert retention_days(BUSINESS) == TRASH_RETENTION_DAYS_BUSINESS

    def test_a_failed_move_records_nothing(self, qapp, store, monkeypatch):
        def explode(*a, **kw):
            raise RcError("operations/movefile", 500, {"error": "no"})

        monkeypatch.setattr(ops, "movefile", explode)
        assert self.bin_(store).soft_delete("a.txt") is None
        store.flush()
        assert repo_files.trash_items(ACCOUNT.id) == []


class TestRestore:

    def test_it_round_trips_to_the_original_path(self, qapp, store, monkeypatch):
        """The BUILD_PLAN's acceptance case."""
        moves: list[tuple[str, str]] = []
        monkeypatch.setattr(ops, "movefile",
                            lambda sf, sr, df, dr, **kw: moves.append((sr, dr)))
        bin_ = TrashBin(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        entry = bin_.soft_delete("Docs/a.txt")
        store.flush()

        assert bin_.restore_from_trash(entry.id) is True
        assert moves[-1][1] == "Docs/a.txt"
        assert moves[-1][0] == entry.trash_path

    def test_an_unknown_id_is_not_a_crash(self, qapp, store):
        bin_ = TrashBin(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        assert bin_.restore_from_trash(9_999) is False


class TestPurge:

    def test_cleanup_is_never_called(self, qapp, store, monkeypatch):
        """Invariant I8. On OneDrive `operations/cleanup` permanently deletes
        every previous version of every file, and it is unsupported on Personal
        accounts entirely."""
        assert not hasattr(ops, "cleanup"), \
            "rc/ops must not expose operations/cleanup at all"

    def test_no_call_anywhere_names_operations_cleanup(self):
        """Checked with the AST: the endpoint may appear in `rc/guards.py`'s
        forbidden list and in prose explaining why, and may not appear as an
        argument to a call."""
        offenders = []
        for path in (REPO_ROOT / "onedriveui").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                args = list(node.args) + [k.value for k in node.keywords]
                for arg in args:
                    if isinstance(arg, ast.Constant) and \
                            arg.value == "operations/cleanup":
                        offenders.append(f"{path}:{node.lineno}")
        assert offenders == []

    def test_the_guard_still_forbids_it(self):
        """The other half: the denylist that would refuse the call is present.

        Absent it, the AST test above would pass on a codebase that had simply
        forgotten the endpoint exists — and the next person to need a "clean up
        the trash" call would add one.
        """
        from onedriveui.rc import guards

        source = pathlib.Path(guards.__file__).read_text(encoding="utf-8")
        assert "operations/cleanup" in source
        assert "I8" in source

    def test_expired_items_are_purged_one_by_one(self, qapp, store, monkeypatch):
        purged: list[str] = []
        monkeypatch.setattr(ops, "deletefile",
                            lambda fs, remote, **kw: purged.append(remote))
        repo_files.add_trash(TrashEntry(
            account_id=ACCOUNT.id, rel_path="a.txt",
            trash_path=f"{REMOTE_TRASH_DIR}/old/a.txt",
            deleted_at="2020-01-01T00:00:00Z",
            purge_after="2020-02-01T00:00:00Z"), writer=store)
        store.flush()
        bin_ = TrashBin(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        assert bin_.purge_expired() == 1
        assert purged == [f"{REMOTE_TRASH_DIR}/old/a.txt"]

    def test_a_fresh_item_is_not_purged(self, qapp, store, monkeypatch):
        monkeypatch.setattr(ops, "movefile", lambda *a, **kw: None)
        monkeypatch.setattr(ops, "deletefile",
                            lambda *a, **kw: pytest.fail("purged a fresh item"))
        bin_ = TrashBin(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        bin_.soft_delete("a.txt")
        store.flush()
        assert bin_.purge_expired() == 0

    def test_the_web_bin_is_still_offered(self, qapp, store):
        """A file deleted from the file manager is genuinely in Microsoft's bin
        and not in ours, so the link is offered rather than explained away."""
        from onedriveui.constants import WEB_RECYCLE_BIN

        bin_ = TrashBin(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)
        # Pins the WIRING, not the value: the URL is Microsoft's to change (it
        # moved from `?id=recyclebin` to `/recycle` in 2026), and this must fail
        # if the button stops using the constant, not when Microsoft moves a
        # page.
        assert bin_.web_recyclebin_url() == WEB_RECYCLE_BIN


# ═════════════════════════════════════════════════════════════════════════════
# Versions
# ═════════════════════════════════════════════════════════════════════════════

class TestVersions:

    def store_(self, store) -> VersionStore:
        return VersionStore(ACCOUNT, endpoint=lambda: ENDPOINT, writer=store)

    def test_the_run_suffix_has_no_invalid_characters(self):
        """A colon is invalid in a OneDrive path, so a backup directory named
        with one would be unsyncable by the sync that created it."""
        suffix = run_suffix("2026-08-31T12:00:00Z")
        assert suffix == "20260831T120000Z"
        assert ":" not in suffix and "-" not in suffix

    def test_a_run_is_indexed(self, qapp, store, monkeypatch):
        directory = f"{REMOTE_VERSIONS_DIR}/20260831T120000Z"
        monkeypatch.setattr(ops, "list_dir", lambda fs, remote="", **kw: [
            RemoteFolderNode(rel_path=f"{directory}/Docs/a.txt", name="a.txt",
                             size=100),
        ])
        run = RunRecord(run_id="r1", account_id=ACCOUNT.id, kind=RunKind.BISYNC,
                        started_at="2026-08-31T12:00:00Z")
        assert self.store_(store).index_run(run) == 1
        store.flush()
        assert [v.rel_path for v in repo_files.versions_for(ACCOUNT.id, "Docs/a.txt")] \
            == ["Docs/a.txt"]

    def test_the_web_history_is_linked_because_we_cannot_list_it(self, qapp, store):
        """rclone can delete versions and can neither list nor restore them, so
        the deep link is the honest answer rather than a gap."""
        vs = self.store_(store)
        assert vs.web_version_url("item123").startswith("https://")
        assert "Version history" in vs.WHY_WEB

    def test_restore_captures_the_current_copy_first(self, qapp, store, monkeypatch):
        """A user restoring Tuesday's draft has not decided to discard today's
        work. Overwriting without capturing makes restore destructive and
        unrepeatable, which defeats a version-history feature entirely."""
        copies: list[tuple[str, str]] = []
        monkeypatch.setattr(ops, "copyfile",
                            lambda sf, sr, df, dr, **kw: copies.append((sr, dr)))
        version_id = repo_files.add_version(VersionEntry(
            account_id=ACCOUNT.id, rel_path="Docs/a.txt",
            backup_path=f"{REMOTE_VERSIONS_DIR}/20260831T120000Z/Docs/a.txt",
            captured_at="2026-08-31T12:00:00Z"), writer=store)
        store.flush()

        assert self.store_(store).restore_version(version_id) is True
        # First the current copy is captured, then the old one comes back.
        assert copies[0][0] == "Docs/a.txt"
        assert copies[0][1].startswith(f"{REMOTE_VERSIONS_DIR}/")
        assert copies[1][1] == "Docs/a.txt"

    def test_a_failed_capture_refuses_to_restore(self, qapp, store, monkeypatch):
        """Restoring over an uncaptured current copy is the one outcome this
        method exists to prevent."""
        def explode(*a, **kw):
            raise RcError("operations/copyfile", 500, {"error": "no"})

        monkeypatch.setattr(ops, "copyfile", explode)
        version_id = repo_files.add_version(VersionEntry(
            account_id=ACCOUNT.id, rel_path="Docs/a.txt",
            backup_path=f"{REMOTE_VERSIONS_DIR}/x/Docs/a.txt"), writer=store)
        store.flush()
        assert self.store_(store).restore_version(version_id) is False

    def test_deleting_a_version_cannot_reach_a_live_file(self, qapp, store,
                                                         monkeypatch):
        monkeypatch.setattr(ops, "deletefile",
                            lambda *a, **kw: pytest.fail("deleted a live file"))
        version_id = repo_files.add_version(VersionEntry(
            account_id=ACCOUNT.id, rel_path="Docs/a.txt",
            backup_path="Docs/a.txt"), writer=store)   # not under versions/
        store.flush()
        assert self.store_(store).delete_version(version_id) is False

    def test_the_module_surface_matches_the_contract(self, qapp, store):
        """CONTRACTS §10.9 is written in terms of functions; the UI calls them
        that way."""
        assert callable(versions_mod.versions_for)
        assert callable(versions_mod.restore_version)
        assert callable(versions_mod.web_version_url)
        assert callable(trashbin_mod.soft_delete)
        assert callable(trashbin_mod.restore_from_trash)
        assert callable(trashbin_mod.purge_expired)
        assert callable(trashbin_mod.web_recyclebin_url)
