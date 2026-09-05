"""WP-08 — `sync/pinner.py` and `sync/filestate.py`.

Files On-Demand has two directions and each has one way to get it badly wrong:

* hydrating with `sendfile()`/`copy_file_range()` silently produces a file of
  zeros with a correct length, so the read is done in explicit blocks;
* freeing a `Dirty` or queued item destroys bytes that exist on this disk and
  nowhere else, so it is refused and raised as an issue.

And `statuses()` is on Nautilus's own UI thread with no way to answer later, so
it is measured rather than assumed.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.constants import MAX_CONCURRENT_PINS
from onedriveui.data import db, repo_files
from onedriveui.data.writer import DbWriter
from onedriveui.errors import SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    CacheEntry,
    FileState,
    IssueCode,
    RcEndpoint,
    utcnow_iso,
)
from onedriveui.rc import vfs
from onedriveui.sync.filestate import BUDGET_MS, FileStateService
from onedriveui.sync.issues import IssueEngine
from onedriveui.sync.pinner import (
    EVICTOR_LOG_MARKER,
    Pinner,
    RepinWatcher,
    hydrate_file,
)

ENDPOINT = RcEndpoint(kind="mount", port=17801, account_id="onedrive")


@pytest.fixture
def account(tmp_path) -> AccountInfo:
    root = tmp_path / "OneDrive"
    root.mkdir()
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


def pinner(account, store, **kwargs) -> Pinner:
    kwargs.setdefault("endpoint", lambda: ENDPOINT)
    return Pinner(account, writer=store, **kwargs)


# ═════════════════════════════════════════════════════════════════════════════
# Hydration
# ═════════════════════════════════════════════════════════════════════════════

class TestHydrate:

    def test_it_reads_the_whole_file(self, tmp_path):
        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * 9_000_000)
        assert hydrate_file(path) == 9_000_000

    def test_progress_is_reported_per_block(self, tmp_path):
        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * 9_000_000)
        seen: list[tuple[int, int]] = []
        hydrate_file(path, block=4 * 1024 * 1024,
                     progress=lambda done, total: seen.append((done, total)))
        assert len(seen) == 3
        assert seen[-1] == (9_000_000, 9_000_000)

    def test_cancel_stops_between_blocks(self, tmp_path):
        """Whatever arrived stays cached; a cancel is not a rollback."""
        path = tmp_path / "big.bin"
        path.write_bytes(b"x" * 9_000_000)
        read = hydrate_file(path, block=1024, cancel=lambda: True)
        assert read == 0

    def test_an_empty_file_is_fine(self, tmp_path):
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        assert hydrate_file(path) == 0

    def test_the_fast_paths_are_not_used(self, tmp_path, monkeypatch):
        """`sendfile()` and `copy_file_range()` either fail on FUSE or copy holes
        as zeros — a "download" producing zeros with a correct length would be
        far worse than one that failed."""
        import os as _os
        import shutil as _shutil

        monkeypatch.setattr(_os, "sendfile",
                            lambda *a, **kw: pytest.fail("used sendfile"),
                            raising=False)
        monkeypatch.setattr(_os, "copy_file_range",
                            lambda *a, **kw: pytest.fail("used copy_file_range"),
                            raising=False)
        monkeypatch.setattr(_shutil, "copyfileobj",
                            lambda *a, **kw: pytest.fail("used shutil"))
        path = tmp_path / "a.bin"
        path.write_bytes(b"y" * 5_000)
        assert hydrate_file(path) == 5_000


# ═════════════════════════════════════════════════════════════════════════════
# Pinning
# ═════════════════════════════════════════════════════════════════════════════

class TestPin:

    def test_the_pin_is_recorded_before_the_download(self, qapp, account, store):
        """A crash mid-hydration must leave an unsatisfied pin the next start-up
        finishes, not a half-downloaded file nothing knows was wanted."""
        Path(account.sync_root, "a.txt").write_text("x")
        recorded: list[str] = []
        pin = pinner(account, store, submit=lambda work: recorded.append("later"))
        pin.pin("a.txt")
        store.flush()
        assert [p.rel_path for p in repo_files.pins(account.id)] == ["a.txt"]
        assert recorded == ["later"]

    def test_hydration_marks_the_pin_satisfied(self, qapp, account, store):
        Path(account.sync_root, "a.txt").write_text("x" * 100)
        pin = pinner(account, store)
        pin.pin("a.txt")
        store.flush()
        assert repo_files.unsatisfied_pins(account.id) == []

    def test_at_most_three_hydrations_run_at_once(self, qapp, account, store):
        """Beyond four parallel streams OneDrive returns HTTP 429, and rclone's
        retry then makes the whole batch slower than a queue of three."""
        started: list[str] = []
        for i in range(10):
            Path(account.sync_root, f"f{i}.txt").write_text("x")
        pin = pinner(account, store, submit=lambda work: started.append("job"))
        for i in range(10):
            pin.pin(f"f{i}.txt")
        assert len(started) == MAX_CONCURRENT_PINS
        assert Pinner.MAX_CONCURRENT_PINS == MAX_CONCURRENT_PINS

    def test_the_overflow_is_left_on_disk_not_in_memory(self, qapp, account, store):
        """The pin rows are already persisted and unsatisfied, so a sweep picks
        them up — an in-memory queue would lose the same work on a crash."""
        for i in range(10):
            Path(account.sync_root, f"f{i}.txt").write_text("x")
        pin = pinner(account, store, submit=lambda work: None)
        for i in range(10):
            pin.pin(f"f{i}.txt")
        store.flush()
        assert len(repo_files.unsatisfied_pins(account.id)) == 10

    def test_unpin_does_not_delete_the_file(self, qapp, account, store):
        """Unpinning and freeing are two different requests. Conflating them
        makes "I don't need this pinned" silently take the file offline."""
        path = Path(account.sync_root, "a.txt")
        path.write_text("keep me")
        pin = pinner(account, store)
        pin.pin("a.txt")
        pin.unpin("a.txt")
        store.flush()
        assert path.read_text() == "keep me"
        assert repo_files.pins(account.id) == []

    def test_recursive_pin_covers_the_tree(self, qapp, account, store):
        folder = Path(account.sync_root, "Photos")
        folder.mkdir()
        for i in range(3):
            (folder / f"p{i}.jpg").write_bytes(b"x")
        started: list[str] = []
        pin = pinner(account, store, submit=lambda work: started.append("job"))
        pin.pin("Photos", recursive=True)
        assert len(started) == MAX_CONCURRENT_PINS

    def test_cancel_stops_it(self, qapp, account, store):
        pin = pinner(account, store, submit=lambda work: None)
        pin.pin("a.txt")
        pin.cancel("a.txt")
        assert pin.active() == 0


# ═════════════════════════════════════════════════════════════════════════════
# Freeing
# ═════════════════════════════════════════════════════════════════════════════

class TestFreeUpSpace:

    def _vfs(self, monkeypatch, *, freed=1024, refuse=False, queue=()):
        from onedriveui.models import DiskCacheInfo

        monkeypatch.setattr(vfs, "disk_cache_info", lambda ep: DiskCacheInfo())
        monkeypatch.setattr(vfs, "queue", lambda ep, **kw: list(queue))

        def evict(info, rel_path, queue_names):
            if refuse:
                raise SafetyRefusal("I3", f"{rel_path} has un-uploaded changes")
            return freed

        monkeypatch.setattr(vfs, "evict", evict)

    def test_it_frees_and_reports_the_bytes(self, qapp, account, store, monkeypatch):
        self._vfs(monkeypatch, freed=4096)
        assert pinner(account, store).free_up_space("a.txt") == 4096

    def test_a_dirty_file_is_refused(self, qapp, account, store, monkeypatch):
        """Those bytes exist on this disk and nowhere else on the planet."""
        self._vfs(monkeypatch, refuse=True)
        assert pinner(account, store).free_up_space("a.txt") == 0

    def test_the_refusal_becomes_a_visible_issue(self, qapp, account, store,
                                                 monkeypatch):
        """Not an exception the user sees as a traceback, and not silence."""
        self._vfs(monkeypatch, refuse=True)
        issues = IssueEngine(account, writer=store)
        pinner(account, store, issues=issues).free_up_space("a.txt")
        assert issues.open_issues()[0].code is IssueCode.FILE_IN_USE

    def test_the_pin_is_cleared_when_the_space_is_taken(self, qapp, account, store,
                                                        monkeypatch):
        Path(account.sync_root, "a.txt").write_text("x")
        self._vfs(monkeypatch)
        pin = pinner(account, store)
        pin.pin("a.txt")
        pin.free_up_space("a.txt")
        store.flush()
        assert repo_files.pins(account.id) == []

    def test_no_endpoint_frees_nothing(self, qapp, account, store):
        assert Pinner(account, writer=store,
                      endpoint=lambda: None).free_up_space("a.txt") == 0

    def test_free_up_all_skips_rather_than_stopping(self, qapp, account, store,
                                                    monkeypatch):
        """One un-uploaded file must not stop the other nine thousand being
        reclaimed."""
        from onedriveui.models import DiskCacheInfo

        entries = [CacheEntry(rel_path="clean.txt", size=100, bytes_local=100),
                   CacheEntry(rel_path="dirty.txt", size=100, bytes_local=100,
                              dirty=True),
                   CacheEntry(rel_path="also-clean.txt", size=100, bytes_local=100)]
        monkeypatch.setattr(vfs, "disk_cache_info", lambda ep: DiskCacheInfo())
        monkeypatch.setattr(vfs, "queue", lambda ep, **kw: [])
        monkeypatch.setattr(vfs, "scan", lambda info, generation, *a, **kw: iter(entries))
        monkeypatch.setattr(vfs, "evict", lambda info, rel_path, names: 100)
        assert pinner(account, store).free_up_all() == 200


# ═════════════════════════════════════════════════════════════════════════════
# The repin watcher
# ═════════════════════════════════════════════════════════════════════════════

class TestRepinWatcher:

    def test_an_evicted_pin_is_re_queued(self, qapp, account, store):
        """rclone's evictor has no idea a file was pinned. Without this,
        "Always keep on this device" quietly stops being true after a week."""
        Path(account.sync_root, "a.txt").write_text("x")
        pin = pinner(account, store, submit=lambda work: None)
        pin.pin("a.txt")
        store.flush()
        assert RepinWatcher(pin, account).sweep() == ["a.txt"]

    def test_a_satisfied_pin_is_left_alone(self, qapp, account, store):
        Path(account.sync_root, "a.txt").write_text("x")
        pin = pinner(account, store)
        pin.pin("a.txt")
        store.flush()
        assert RepinWatcher(pin, account).sweep() == []

    def test_the_journal_line_is_recognised(self, qapp, account, store):
        Path(account.sync_root, "a.txt").write_text("x")
        pin = pinner(account, store, submit=lambda work: None)
        pin.pin("a.txt")
        store.flush()
        watcher = RepinWatcher(pin, account)
        line = f"INFO : a.txt: {EVICTOR_LOG_MARKER}"
        assert watcher.on_log_lines([line]) == ["a.txt"]

    def test_an_unrelated_line_does_nothing(self, qapp, account, store):
        pin = pinner(account, store, submit=lambda work: None)
        assert RepinWatcher(pin, account).on_log_lines(
            ["INFO : a.txt: Copied (new)"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# File state
# ═════════════════════════════════════════════════════════════════════════════

class TestFileState:

    def service(self, account, store) -> FileStateService:
        return FileStateService(account, writer=store, endpoint=lambda: ENDPOINT)

    def seed(self, account, store, *entries: CacheEntry) -> None:
        repo_files.upsert_cache_rows(account.id, list(entries), 1,
                                     writer=store, sync=True)

    def test_an_unscanned_path_is_unknown_not_online_only(self, qapp, account, store):
        """They render identically and mean opposite things: "we have not looked"
        versus "this is definitely not on your disk"."""
        svc = self.service(account, store)
        assert svc.status("never-seen.txt").state is FileState.UNKNOWN

    def test_a_cached_file_reports_its_state(self, qapp, account, store):
        self.seed(account, store,
                  CacheEntry(rel_path="a.txt", size=100, bytes_local=100,
                             state=FileState.LOCAL))
        svc = self.service(account, store)
        assert svc.status("a.txt").state is FileState.LOCAL

    def test_a_pinned_local_file_is_pinned(self, qapp, account, store):
        self.seed(account, store,
                  CacheEntry(rel_path="a.txt", size=100, bytes_local=100,
                             state=FileState.LOCAL))
        repo_files.set_pin(account.id, "a.txt", writer=store)
        store.flush()
        svc = self.service(account, store)
        svc.refresh_overlays()
        status = svc.status("a.txt")
        assert status.state is FileState.PINNED
        assert status.pinned is True

    def test_an_exclusion_outranks_everything(self, qapp, account, store):
        """Showing "Sync problem" on a file the user deliberately excluded is
        just wrong."""
        self.seed(account, store,
                  CacheEntry(rel_path="a.txt", size=100, state=FileState.LOCAL))
        repo_files.set_selection(account.id, "a.txt", False, writer=store)
        store.flush()
        svc = self.service(account, store)
        svc.refresh_overlays()
        assert svc.status("a.txt").state is FileState.EXCLUDED

    def test_an_open_issue_outranks_the_cache_state(self, qapp, account, store):
        self.seed(account, store,
                  CacheEntry(rel_path="a.txt", size=100, state=FileState.LOCAL))
        IssueEngine(account, writer=store).raise_issue(
            IssueCode.UPLOAD_FAILED, rel_path="a.txt")
        svc = self.service(account, store)
        svc.refresh_overlays()
        status = svc.status("a.txt")
        assert status.state is FileState.ERROR
        assert status.has_error is True

    def test_every_path_asked_about_gets_an_answer(self, qapp, account, store):
        """A missing key would make the Nautilus extension raise on its UI
        thread, which is the one place an exception is unrecoverable."""
        svc = self.service(account, store)
        asked = ["a.txt", "b.txt", "c/d.txt"]
        assert set(svc.statuses(asked)) == set(asked)

    def test_a_thousand_paths_stay_inside_the_ipc_budget(self, qapp, account, store):
        """Nautilus calls `update_file_info` on its own UI thread and cannot be
        told to wait, so this is measured rather than assumed."""
        self.seed(account, store, *[
            CacheEntry(rel_path=f"f{i}.txt", size=10, bytes_local=10,
                       state=FileState.LOCAL) for i in range(1_000)])
        svc = self.service(account, store)
        svc.refresh_overlays()
        paths = [f"f{i}.txt" for i in range(1_000)]

        started = time.monotonic()
        result = svc.statuses(paths)
        elapsed_ms = (time.monotonic() - started) * 1000.0

        assert len(result) == 1_000
        assert elapsed_ms < BUDGET_MS * 2, f"{elapsed_ms:.1f} ms for 1000 paths"

    def test_a_database_failure_answers_unknown_rather_than_raising(
            self, qapp, account, store, monkeypatch):
        """The extension must never see a traceback."""
        def explode(*a, **kw):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(repo_files, "file_states", explode)
        svc = self.service(account, store)
        assert svc.status("a.txt").state is FileState.UNKNOWN

    def test_invalidation_reaches_the_bus(self, qapp, account, store, bus_spy):
        """Nautilus caches what we last told it and will not ask again."""
        bus_spy.watch("file_states_invalidated")
        self.service(account, store).invalidate(["a.txt"])
        assert bus_spy.last("file_states_invalidated") == (account.id, ["a.txt"])

    def test_invalidating_nothing_says_nothing(self, qapp, account, store, bus_spy):
        bus_spy.watch("file_states_invalidated")
        self.service(account, store).invalidate([])
        assert bus_spy.count("file_states_invalidated") == 0

    def test_after_a_scan_an_unindexed_path_is_online_only(self, qapp, account, store):
        """Once the VFS cache has been walked, "no row" means "not on disk":
        the file is served from the cloud, which is exactly the cloud badge."""
        self.seed(account, store, CacheEntry(rel_path="cached.txt",
                                             state=FileState.LOCAL, size=1,
                                             bytes_local=1))
        svc = self.service(account, store)
        assert svc.status("cached.txt").state is FileState.LOCAL
        assert svc.status("never-opened.txt").state is FileState.ONLINE_ONLY
