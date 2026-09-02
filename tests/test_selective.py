"""WP-08 — `sync/selective.py` and `sync/browse.py`.

The headline property: **a failed resync prunes nothing.** The order is write
filters, resync, then prune, and getting it backwards means a failed resync has
already destroyed the local copies of files that are still fine in the cloud.

The second: the prune goes to the freedesktop trash. Unticking a folder in a
settings dialog is not a delete confirmation.

For `browse`, everything follows from OneDrive having `ListR = false`: rclone
issues one Graph request per directory, so a recursive listing of an 8 000-folder
drive throttles the user out of their own account for fifteen minutes. There is
no recursive option, and `size()` is always asynchronous.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.data import db, repo_files
from onedriveui.data.writer import DbWriter
from onedriveui.errors import RcError, SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    JobHandle,
    RcEndpoint,
    RemoteFolderNode,
    RunVerdict,
    utcnow_iso,
)
from onedriveui.rc import filters, ops
from onedriveui.sync.browse import CACHE_TTL_S, RemoteBrowser
from onedriveui.sync.selective import SelectiveSync

ENDPOINT = RcEndpoint(kind="rcd", port=17800)


@pytest.fixture
def account(tmp_path) -> AccountInfo:
    root = tmp_path / "OneDrive"
    (root / "Photos").mkdir(parents=True)
    (root / "Photos" / "a.jpg").write_bytes(b"x" * 100)
    (root / "Photos" / "b.jpg").write_bytes(b"y" * 200)
    (root / "Documents").mkdir()
    (root / "Documents" / "notes.txt").write_text("keep me")
    return AccountInfo(id="onedrive", remote="onedrive", sync_root=str(root))


@pytest.fixture
def store(_isolate_home, qapp, account):
    writer = DbWriter(paths.db_file())
    assert writer.start_writer()
    writer.submit_sync(
        lambda conn: conn.execute(
            "INSERT INTO accounts (id, remote, sync_root, added_at) VALUES (?,?,?,?)",
            (account.id, account.remote, account.sync_root, utcnow_iso())),
        urgent=True)
    try:
        yield writer
    finally:
        writer.stop()
        db.close_all()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = Path(base) / name
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


# ═════════════════════════════════════════════════════════════════════════════
# Selective sync
# ═════════════════════════════════════════════════════════════════════════════

class TestApply:

    def test_a_real_change_restarts_the_mount(self, qapp, account, store):
        """Unticking a folder has to reach rclone, and only a restart does that.

        The rules are command-line arguments, so a running mount cannot be told
        about a new one. Before this was wired, `apply()` persisted the
        selection, evicted the cache and returned — and the mount carried on
        serving every folder the user had just unticked.
        """
        calls = []
        selective = SelectiveSync(account, writer=store,
                                  remount=lambda: calls.append("remount") or True)
        selective.apply(["Photos"], prune=False)
        assert calls == ["remount"]

    def test_a_remount_failure_does_not_lose_the_selection(self, qapp, account,
                                                           store):
        """The selection is already persisted and correct; the mount picks it up
        on its next start either way. A restart that fails must not raise into a
        dialog the user has already confirmed."""
        def boom():
            raise RuntimeError("systemd said no")

        selective = SelectiveSync(account, writer=store, remount=boom)
        selective.apply(["Photos"], prune=False)
        assert selective.excluded() == ["Photos"]

    def test_a_failed_resync_prunes_nothing(self, qapp, account, store):
        """The whole reason for the ordering. Pruning first means a failed
        resync has already destroyed local copies of files that are still
        perfectly fine in the cloud."""
        root = Path(account.sync_root)
        before = tree_hash(root)
        selective = SelectiveSync(account, writer=store,
                                  resync=lambda acc: RunVerdict.CRITICAL_SOFT)
        with pytest.raises(SafetyRefusal) as excinfo:
            selective.apply(["Photos"])
        assert excinfo.value.invariant == "I11"
        assert tree_hash(root) == before

    def test_a_missing_resync_runner_commits_nothing(self, qapp, account, store):
        """A filters change without a resync locks the account out of syncing
        (invariant I11), so it is refused rather than half-done."""
        root = Path(account.sync_root)
        before = tree_hash(root)
        selective = SelectiveSync(account, writer=store, resync=None)
        with pytest.raises(SafetyRefusal):
            selective.apply(["Photos"])
        assert tree_hash(root) == before

    def test_a_successful_resync_prunes(self, qapp, account, store):
        selective = SelectiveSync(account, writer=store,
                                  resync=lambda acc: RunVerdict.OK)
        result = selective.apply(["Photos"])
        assert result.trashed == ["Photos"]
        assert not (Path(account.sync_root) / "Photos").exists()

    def test_the_other_folders_are_untouched(self, qapp, account, store):
        selective = SelectiveSync(account, writer=store,
                                  resync=lambda acc: RunVerdict.OK)
        selective.apply(["Photos"])
        assert (Path(account.sync_root) / "Documents" / "notes.txt").read_text() \
            == "keep me"

    def test_prune_false_leaves_the_files(self, qapp, account, store):
        """What a user who unticked a folder only to stop *uploading* it wants."""
        selective = SelectiveSync(account, writer=store,
                                  resync=lambda acc: RunVerdict.OK)
        selective.apply(["Photos"], prune=False)
        assert (Path(account.sync_root) / "Photos" / "a.jpg").exists()

    def test_an_unchanged_selection_does_not_resync(self, qapp, account, store):
        """A settings save that changed nothing must not force a full resync."""
        calls: list[str] = []

        def resync(acc):
            calls.append("resync")
            return RunVerdict.OK

        selective = SelectiveSync(account, writer=store, resync=resync)
        selective.apply(["Photos"])       # the first write is a real change
        assert calls == ["resync"]
        selective.apply(["Photos"])       # the same selection again
        assert calls == ["resync"]

    def test_the_selection_is_persisted(self, qapp, account, store):
        selective = SelectiveSync(account, writer=store,
                                  resync=lambda acc: RunVerdict.OK)
        selective.apply(["Photos"])
        store.flush()
        assert "Photos" in repo_files.excluded_paths(account.id)

    def test_the_filters_digest_matches_the_file(self, qapp, account, store):
        """The BUILD_PLAN's acceptance case: a filters edit paired with a resync
        leaves `filters.txt.md5` matching the file it describes."""
        selective = SelectiveSync(account, writer=store,
                                  resync=lambda acc: RunVerdict.OK)
        selective.apply(["Photos"])
        assert filters.needs_resync(account.id) is False


class TestPrune:

    def test_it_goes_to_the_trash_never_to_unlink(self, qapp, account, store,
                                                  monkeypatch):
        """Invariant I10. Unticking a folder is not a delete confirmation, and a
        user who did it by accident must get their files back from the trash
        rather than by re-downloading 40 GB."""
        import os as _os

        monkeypatch.setattr(_os, "unlink",
                            lambda *a, **kw: pytest.fail("unlinked a user file"))
        monkeypatch.setattr(_os, "remove",
                            lambda *a, **kw: pytest.fail("removed a user file"))
        result = SelectiveSync(account, writer=store).prune_local(["Photos"])
        assert result.trashed == ["Photos"]

    def test_the_trashed_files_are_recoverable(self, qapp, account, store):
        from onedriveui.platform import trash

        SelectiveSync(account, writer=store).prune_local(["Photos"])
        entries = trash.list_trash(trash.home_trash())
        assert any("Photos" in str(e.original_path) for e in entries)

    def test_a_path_outside_the_sync_root_is_refused(self, qapp, account, store,
                                                     tmp_path):
        """A bug that fed this an absolute path from elsewhere would otherwise
        trash the wrong tree."""
        outsider = tmp_path / "elsewhere"
        outsider.mkdir()
        (outsider / "important.txt").write_text("not yours")
        result = SelectiveSync(account, writer=store).prune_local(
            [f"../{outsider.name}"])
        assert result.trashed == []
        assert (outsider / "important.txt").exists()

    def test_a_missing_folder_is_not_an_error(self, qapp, account, store):
        result = SelectiveSync(account, writer=store).prune_local(["Gone"])
        assert result.trashed == []
        assert result.skipped == []

    def test_the_bytes_are_counted_for_the_confirmation(self, qapp, account, store):
        result = SelectiveSync(account, writer=store).prune_local(["Photos"])
        assert result.bytes_freed == 300


class TestPreview:

    def test_it_reports_what_would_go_without_touching_anything(
            self, qapp, account, store):
        """"Choose folders" looks like a filter and behaves like a delete, so
        the dialog has to say so before the user commits."""
        root = Path(account.sync_root)
        before = tree_hash(root)
        result = SelectiveSync(account, writer=store).preview(["Photos"])
        assert sorted(result.trashed) == ["Photos/a.jpg", "Photos/b.jpg"]
        assert result.bytes_freed == 300
        assert tree_hash(root) == before


class TestMountExcludes:

    def test_the_mount_sees_the_same_selection(self, qapp, account, store):
        """A folder filtered out of bisync but still visible through the mount
        is a folder the user can open, edit and expect to sync."""
        rules = SelectiveSync(account, writer=store).as_mount_excludes(["Photos"])
        assert rules == [filters.exclude_rule("Photos")]

    def test_it_reads_the_database_when_not_given_a_list(self, qapp, account, store):
        repo_files.set_selection(account.id, "Photos", False, writer=store)
        store.flush()
        assert SelectiveSync(account, writer=store).as_mount_excludes() == [
            filters.exclude_rule("Photos")]


class TestExcludeOne:

    def test_it_records_without_resyncing(self, qapp, account, store):
        """A single right-click must not trigger a full resync of the account."""
        calls: list[str] = []
        selective = SelectiveSync(account, writer=store,
                                  resync=lambda acc: calls.append("resync"))
        selective.exclude("Photos/a.jpg")
        store.flush()
        assert calls == []
        assert "Photos/a.jpg" in repo_files.excluded_paths(account.id)

    def test_an_empty_path_does_nothing(self, qapp, account, store):
        SelectiveSync(account, writer=store).exclude("")
        store.flush()
        assert repo_files.excluded_paths(account.id) == []


# ═════════════════════════════════════════════════════════════════════════════
# Remote browsing
# ═════════════════════════════════════════════════════════════════════════════

class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def listings(monkeypatch):
    """Record every `operations/list` and control what it answers."""
    calls: list[tuple[str, bool, bool]] = []
    answer: dict[str, object] = {"nodes": [], "raise": None}

    def fake(fs, remote="", *, ep, dirs_only=False, recurse=False, **kw):
        calls.append((remote, dirs_only, recurse))
        if answer["raise"] is not None:
            raise answer["raise"]
        return list(answer["nodes"])

    monkeypatch.setattr(ops, "list_dir", fake)
    return calls, answer


def browser(account, clock=None) -> RemoteBrowser:
    return RemoteBrowser(account, endpoint=lambda: ENDPOINT,
                         monotonic=clock or Clock())


class TestBrowse:

    def test_listings_are_never_recursive(self, qapp, account, listings):
        """OneDrive has `ListR = false`: rclone issues one Graph request per
        directory, so 8 000 folders is 8 000 requests against a limit of 3 000
        per five minutes."""
        calls, _answer = listings
        browser(account).children("Photos")
        assert calls == [("Photos", True, False)]

    def test_the_listing_is_cached(self, qapp, account, listings):
        calls, _answer = listings
        brow = browser(account)
        brow.children("Photos")
        brow.children("Photos")
        assert len(calls) == 1

    def test_the_cache_expires(self, qapp, account, listings):
        """A folder created on the phone five minutes ago has to show up."""
        calls, _answer = listings
        clock = Clock()
        brow = browser(account, clock)
        brow.children("Photos")
        clock.advance(CACHE_TTL_S + 1)
        brow.children("Photos")
        assert len(calls) == 2

    def test_force_bypasses_the_cache(self, qapp, account, listings):
        calls, _answer = listings
        brow = browser(account)
        brow.children("Photos")
        brow.children("Photos", force=True)
        assert len(calls) == 2

    def test_a_failure_keeps_the_stale_list(self, qapp, account, listings):
        """Blanking the picker mid-selection would lose whatever the user had
        ticked."""
        _calls, answer = listings
        answer["nodes"] = [RemoteFolderNode(rel_path="Photos/2026", name="2026",
                                            is_dir=True)]
        brow = browser(account)
        assert len(brow.children("Photos")) == 1
        answer["raise"] = RcError("operations/list", 500, {"error": "no"})
        assert len(brow.children("Photos", force=True)) == 1

    def test_a_first_failure_answers_empty(self, qapp, account, listings):
        _calls, answer = listings
        answer["raise"] = RcError("operations/list", 500, {"error": "no"})
        assert browser(account).children("Photos") == []

    def test_no_endpoint_answers_empty(self, qapp, account):
        brow = RemoteBrowser(account, endpoint=lambda: None)
        assert brow.children("Photos") == []

    def test_invalidate_drops_the_subtree(self, qapp, account, listings):
        calls, _answer = listings
        brow = browser(account)
        brow.children("Photos")
        brow.children("Photos/2026")
        brow.children("Documents")
        brow.invalidate("Photos")
        assert brow.cached_paths == ["Documents"]

    def test_invalidate_everything(self, qapp, account, listings):
        brow = browser(account)
        brow.children("Photos")
        brow.invalidate()
        assert brow.cached_paths == []

    def test_search_stays_inside_what_was_fetched(self, qapp, account, listings):
        """A search that quietly issued a few thousand Graph requests is the
        worst possible thing behind a field that fires as the user types."""
        calls, answer = listings
        answer["nodes"] = [
            RemoteFolderNode(rel_path="Photos/2026", name="2026", is_dir=True),
            RemoteFolderNode(rel_path="Photos/Camera", name="Camera", is_dir=True),
        ]
        brow = browser(account)
        brow.children("Photos")
        before = len(calls)
        assert [n.name for n in brow.search("cam")] == ["Camera"]
        assert len(calls) == before

    def test_size_is_always_asynchronous(self, qapp, account, monkeypatch):
        """Without `ListR` this walks the whole subtree — minutes on a large
        folder, which a blocking call would turn into a four-second timeout."""
        seen: list[str] = []

        def fake_size(fs, *, ep, group="", label="", timeout_s=None):
            seen.append(fs)
            return JobHandle(job_id=1, execute_id="e", group=group,
                             path="operations/size")

        monkeypatch.setattr(ops, "size", fake_size)
        handle = browser(account).size("Photos")
        assert handle is not None
        assert seen == ["onedrive:Photos"]

    def test_size_of_the_whole_drive(self, qapp, account, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(ops, "size", lambda fs, **kw: seen.append(fs) or
                            JobHandle(job_id=1, execute_id="e", group="",
                                      path="operations/size"))
        browser(account).size()
        assert seen == ["onedrive:"]
