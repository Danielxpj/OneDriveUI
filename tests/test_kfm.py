"""WP-09 — `sync/kfm.py`, `sync/watcher.py`, `sync/vault.py`, `sync/extras.py`.

The headline test is the interrupted KFM run: kill it after phase one, resume or
roll back, and hash the tree. **No file may exist in only one place we have not
verified**, at any instant, and the hash proves it.

Then the landmine: `~/OneDrive/.Trash-1000` already exists on this machine, and
anything the file manager "deletes" from the OneDrive folder lands in it and gets
uploaded. It is drained into the real trash.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.constants import MASS_DELETE_DEFAULT_THRESHOLD
from onedriveui.data import db, repo_files
from onedriveui.data.writer import DbWriter
from onedriveui.errors import SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    DecisionKind,
    KfmFolder,
    VaultState,
    utcnow_iso,
)
from onedriveui.platform import desktop
from onedriveui.sync.extras import IMAGE_SUFFIXES, CameraImporter, ScreenshotWatcher
from onedriveui.sync.kfm import FOLDERS, KfmManager, read_user_dirs, write_user_dirs
from onedriveui.sync.vault import AUTO_LOCK_CHOICES, WARN_BEFORE_MIN, Vault
from onedriveui.sync.watcher import BURST_WINDOW_S, MOUNT_TRASH_DIR, LocalWatcher


@pytest.fixture
def account(tmp_path, _isolate_home) -> AccountInfo:
    root = Path.home() / "OneDrive"
    root.mkdir(parents=True, exist_ok=True)
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


def tree_hash(*roots: Path) -> str:
    """A digest of every file's path and bytes across several trees."""
    digest = hashlib.sha256()
    for root in roots:
        if not root.is_dir():
            continue
        for base, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                path = Path(base) / name
                digest.update(str(path.relative_to(root)).encode())
                try:
                    digest.update(path.read_bytes())
                except OSError:
                    pass
    return digest.hexdigest()


def seed_documents(count: int = 12) -> Path:
    """A `~/Documents` with real bytes in it."""
    source = Path.home() / "Documents"
    (source / "2026").mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (source / "2026" / f"note{i}.txt").write_text(f"contents {i}" * 20)
    return source


# ═════════════════════════════════════════════════════════════════════════════
# Known Folder Move
# ═════════════════════════════════════════════════════════════════════════════

class TestUserDirs:

    def test_the_five_windows_folders(self):
        assert FOLDERS == (KfmFolder.DESKTOP, KfmFolder.DOCUMENTS,
                           KfmFolder.PICTURES, KfmFolder.MUSIC,
                           KfmFolder.VIDEOS)

    def test_writing_preserves_lines_we_do_not_own(self, qapp, account,
                                                   monkeypatch):
        """The file belongs to `xdg-user-dirs`, not to us."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        path = desktop.user_dirs_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# a comment\nXDG_DOWNLOAD_DIR="$HOME/Downloads"\n',
                        encoding="utf-8")
        write_user_dirs({KfmFolder.DOCUMENTS: Path.home() / "OneDrive/Documents"})
        text = path.read_text(encoding="utf-8")
        assert "# a comment" in text
        assert 'XDG_DOWNLOAD_DIR="$HOME/Downloads"' in text
        assert "OneDrive/Documents" in text

    def test_the_path_is_written_relative_to_home(self, qapp, account, monkeypatch):
        """Which is how xdg-user-dirs writes it, so the file stays portable."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        write_user_dirs({KfmFolder.PICTURES: Path.home() / "OneDrive/Pictures"})
        assert '$HOME/OneDrive/Pictures' in \
            desktop.user_dirs_file().read_text(encoding="utf-8")

    def test_it_round_trips(self, qapp, account, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        target = Path.home() / "OneDrive" / "Documents"
        write_user_dirs({KfmFolder.DOCUMENTS: target})
        assert read_user_dirs()[KfmFolder.DOCUMENTS] == target


class TestPlan:

    def test_it_lists_every_file_and_its_size(self, qapp, account, store,
                                              monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        seed_documents(5)
        write_user_dirs({KfmFolder.DOCUMENTS: Path.home() / "Documents"})
        plan = KfmManager(account, writer=store).plan(KfmFolder.DOCUMENTS)
        assert len(plan.files) == 5
        assert plan.bytes_total > 0

    def test_conflicts_are_named_before_anything_moves(self, qapp, account,
                                                       store, monkeypatch):
        """Discovering a name clash halfway through a 48 GB move is not an
        option, so the dialog gets them up front."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        seed_documents(3)
        write_user_dirs({KfmFolder.DOCUMENTS: Path.home() / "Documents"})
        destination = Path(account.sync_root) / "Documents" / "2026"
        destination.mkdir(parents=True)
        (destination / "note0.txt").write_text("already here")

        plan = KfmManager(account, writer=store).plan(KfmFolder.DOCUMENTS)
        assert plan.conflicts == ["2026/note0.txt"]

    def test_a_missing_folder_plans_nothing(self, qapp, account, store,
                                            monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        write_user_dirs({KfmFolder.MUSIC: Path.home() / "NoSuchFolder"})
        plan = KfmManager(account, writer=store).plan(KfmFolder.MUSIC)
        assert plan.files == []


class TestExecute:

    def _manager(self, account, store, tmp_path) -> KfmManager:
        return KfmManager(account, writer=store, journal_dir=tmp_path / "journal")

    def test_a_complete_run_moves_and_verifies_everything(
            self, qapp, account, store, tmp_path, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        source = seed_documents(6)
        write_user_dirs({KfmFolder.DOCUMENTS: source})
        manager = self._manager(account, store, tmp_path)

        assert manager.execute(manager.plan(KfmFolder.DOCUMENTS)) is True
        destination = Path(account.sync_root) / "Documents"
        assert len(list(destination.rglob("*.txt"))) == 6
        assert read_user_dirs()[KfmFolder.DOCUMENTS] == destination

    def test_no_data_is_lost_when_a_run_is_interrupted(
            self, qapp, account, store, tmp_path, monkeypatch):
        """The BUILD_PLAN's acceptance case, and the reason for the two-phase
        journal: at no instant may a file exist in only one place we have not
        verified. The hash of source-plus-destination proves it.
        """
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        source = seed_documents(8)
        write_user_dirs({KfmFolder.DOCUMENTS: source})
        destination = Path(account.sync_root) / "Documents"
        before = tree_hash(source)

        manager = self._manager(account, store, tmp_path)
        plan = manager.plan(KfmFolder.DOCUMENTS)

        # Interrupt halfway through phase one.
        real_copy = __import__("shutil").copy2
        state = {"n": 0}

        def flaky(src, dst, **kw):
            state["n"] += 1
            if state["n"] > 4:
                raise OSError("interrupted")
            return real_copy(src, dst, **kw)

        monkeypatch.setattr("onedriveui.sync.kfm.shutil.copy2", flaky)
        assert manager.execute(plan) is False

        # Every byte is still reachable: nothing was removed, because phase two
        # never ran, and the journal survived.
        assert manager.has_unfinished_run() is True
        assert tree_hash(source) == before

        # Resuming finishes the job with no loss.
        monkeypatch.setattr("onedriveui.sync.kfm.shutil.copy2", real_copy)
        assert manager.execute(manager.plan(KfmFolder.DOCUMENTS)) is True
        assert tree_hash(destination) == before

    def test_rollback_restores_the_source_from_the_destination(
            self, qapp, account, store, tmp_path, monkeypatch):
        """Rollback copies back from the verified destination rather than
        fishing in the trash, so it cannot fail because the trash was emptied."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        source = seed_documents(4)
        write_user_dirs({KfmFolder.DOCUMENTS: source})
        before = tree_hash(source)

        manager = self._manager(account, store, tmp_path)
        manager.execute(manager.plan(KfmFolder.DOCUMENTS))
        # The journal is cleared by a complete run, so simulate an interrupted
        # one by replaying it: the destination has the files, the source does not.
        import shutil as _shutil

        _shutil.rmtree(source)
        manager._save_journal(
            {"folder": KfmFolder.DOCUMENTS.value, "source": str(source),
             "destination": str(Path(account.sync_root) / "Documents")},
            {f"2026/note{i}.txt" for i in range(4)}, set())

        assert manager.rollback() is True
        assert tree_hash(source) == before

    def test_a_destination_outside_the_sync_root_is_refused(
            self, qapp, account, store, tmp_path, monkeypatch):
        """Moving a user's files somewhere this client cannot account for is not
        a KFM, whatever the arguments say."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        source = seed_documents(2)
        write_user_dirs({KfmFolder.DOCUMENTS: source})
        manager = self._manager(account, store, tmp_path)
        plan = manager.plan(KfmFolder.DOCUMENTS)
        plan.destination = tmp_path / "elsewhere"
        with pytest.raises(SafetyRefusal):
            manager.execute(plan)

    def test_a_failed_verification_stops_rather_than_removing(
            self, qapp, account, store, tmp_path, monkeypatch):
        """A truncated copy that passed for complete would take the original
        with it."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        source = seed_documents(3)
        write_user_dirs({KfmFolder.DOCUMENTS: source})
        before = tree_hash(source)
        manager = self._manager(account, store, tmp_path)
        monkeypatch.setattr(KfmManager, "_verify", lambda self, s, t: False)
        assert manager.execute(manager.plan(KfmFolder.DOCUMENTS)) is False
        assert tree_hash(source) == before

    def test_disable_repoints_without_moving_anything_back(
            self, qapp, account, store, tmp_path, monkeypatch):
        """Dragging 48 GB back out of the sync root is a decision the user
        should make explicitly, not a side effect of unticking a box."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        source = seed_documents(3)
        write_user_dirs({KfmFolder.DOCUMENTS: source})
        manager = self._manager(account, store, tmp_path)
        manager.execute(manager.plan(KfmFolder.DOCUMENTS))
        destination = Path(account.sync_root) / "Documents"
        moved = tree_hash(destination)

        assert manager.disable(KfmFolder.DOCUMENTS) is True
        assert tree_hash(destination) == moved
        assert read_user_dirs()[KfmFolder.DOCUMENTS] == Path.home() / "Documents"


# ═════════════════════════════════════════════════════════════════════════════
# The local watcher
# ═════════════════════════════════════════════════════════════════════════════

class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class RecordingDecisions:
    def __init__(self):
        self.required: list[tuple] = []

    def require(self, kind, payload, expires_in_days=7):
        self.required.append((kind, payload))
        return len(self.required)


class TestWatcher:

    def test_changes_are_coalesced_into_one_batch(self, qapp, account):
        """One text-editor save produces a temporary file, a rename and an
        attribute change; three preflight passes for one save is waste."""
        watcher = LocalWatcher(account)
        batches: list[list[str]] = []
        watcher.changed.connect(batches.append)
        root = Path(account.sync_root)
        for name in ("a.txt", "a.txt~", "a.txt"):
            watcher.note_change(str(root / name))
        watcher.flush()
        assert batches == [["a.txt", "a.txt~"]]

    def test_paths_outside_the_sync_root_are_ignored(self, qapp, account):
        watcher = LocalWatcher(account)
        batches: list[list[str]] = []
        watcher.changed.connect(batches.append)
        watcher.note_change("/etc/passwd")
        watcher.flush()
        assert batches == []

    def test_a_delete_burst_raises_exactly_one_decision(self, qapp, account):
        """The BUILD_PLAN's acceptance case. 250 files disappearing is either
        tidying up or a drive that went away, and those are indistinguishable
        from here — so a human decides."""
        decisions = RecordingDecisions()
        clock = Clock()
        watcher = LocalWatcher(account, decisions=decisions, monotonic=clock)
        root = Path(account.sync_root)
        for i in range(250):
            watcher.note_delete(str(root / f"f{i}.txt"))
        assert watcher.delete_burst() == 250
        assert len(decisions.required) == 1
        assert decisions.required[0][0] is DecisionKind.MASS_DELETE
        # Asked the instant the threshold was crossed, not after all 250: the
        # point is to stop the deletion propagating, and waiting for a round
        # number would let another fifty files go first.
        assert decisions.required[0][1]["count"] == MASS_DELETE_DEFAULT_THRESHOLD

    def test_more_deletions_do_not_re_ask(self, qapp, account):
        """A slow trickle past the threshold must not re-ask every 400 ms."""
        decisions = RecordingDecisions()
        watcher = LocalWatcher(account, decisions=decisions, monotonic=Clock())
        root = Path(account.sync_root)
        for i in range(400):
            watcher.note_delete(str(root / f"f{i}.txt"))
        assert len(decisions.required) == 1

    def test_below_the_threshold_asks_nothing(self, qapp, account):
        decisions = RecordingDecisions()
        watcher = LocalWatcher(account, decisions=decisions, monotonic=Clock())
        root = Path(account.sync_root)
        for i in range(MASS_DELETE_DEFAULT_THRESHOLD - 1):
            watcher.note_delete(str(root / f"f{i}.txt"))
        assert decisions.required == []

    def test_the_window_slides(self, qapp, account):
        clock = Clock()
        watcher = LocalWatcher(account, monotonic=clock)
        root = Path(account.sync_root)
        for i in range(10):
            watcher.note_delete(str(root / f"f{i}.txt"))
        assert watcher.delete_burst() == 10
        clock.advance(BURST_WINDOW_S + 1)
        assert watcher.delete_burst() == 0

    def test_the_decision_records_that_nothing_was_deleted(self, qapp, account):
        decisions = RecordingDecisions()
        watcher = LocalWatcher(account, decisions=decisions, monotonic=Clock())
        root = Path(account.sync_root)
        for i in range(250):
            watcher.note_delete(str(root / f"f{i}.txt"))
        assert decisions.required[0][1]["nothing_was_deleted"] is True


class TestTrashDirLandmine:

    def test_it_drains_a_planted_trash_directory(self, qapp, account, store):
        """The BUILD_PLAN's acceptance case. The file manager creates a trash
        directory inside ANY filesystem it deletes from, the FUSE mount
        included — so a "deleted" file is moved into a hidden directory inside
        the OneDrive folder and promptly uploaded."""
        root = Path(account.sync_root)
        files = root / MOUNT_TRASH_DIR / "files"
        info = root / MOUNT_TRASH_DIR / "info"
        files.mkdir(parents=True)
        info.mkdir(parents=True)
        (files / "x.txt").write_text("deleted, then uploaded")
        (info / "x.txt.trashinfo").write_text(
            "[Trash Info]\nPath=/home/u/OneDrive/x.txt\n"
            "DeletionDate=2026-08-31T12:00:00\n")

        watcher = LocalWatcher(account)
        assert watcher.intercept_trash_dir() >= 1
        assert not (files / "x.txt").exists()

    def test_an_orphaned_entry_is_rescued_too(self, qapp, account, store):
        """A `files/` entry with no `.trashinfo` is invisible to every trash
        browser, so nothing else will ever remove it — and it sits inside the
        sync root being uploaded forever."""
        root = Path(account.sync_root)
        files = root / MOUNT_TRASH_DIR / "files"
        files.mkdir(parents=True)
        (files / "orphan.txt").write_text("nobody will ever find me")

        assert LocalWatcher(account).intercept_trash_dir() == 1
        assert not (files / "orphan.txt").exists()

    def test_it_raises_an_issue_so_the_user_is_told(self, qapp, account, store):
        from onedriveui.sync.issues import IssueEngine

        root = Path(account.sync_root)
        files = root / MOUNT_TRASH_DIR / "files"
        files.mkdir(parents=True)
        (files / "x.txt").write_text("x")

        issues = IssueEngine(account, writer=store)
        LocalWatcher(account, issues=issues).intercept_trash_dir()
        assert issues.open_issues()

    def test_no_trash_directory_is_a_no_op(self, qapp, account):
        assert LocalWatcher(account).intercept_trash_dir() == 0

    def test_the_directory_name_is_uid_specific(self, qapp):
        assert MOUNT_TRASH_DIR == f".Trash-{os.getuid()}"


# ═════════════════════════════════════════════════════════════════════════════
# Vault
# ═════════════════════════════════════════════════════════════════════════════

class TestVault:

    def vault(self, account, tmp_path, clock=None) -> Vault:
        return Vault(account, container=tmp_path / "container",
                     mountpoint=tmp_path / "vault", monotonic=clock or Clock())

    def test_the_cloud_vault_is_labelled_not_faked(self, qapp, account, tmp_path):
        """A user who believes they have unlocked their Personal Vault and puts
        passport scans in it deserves to be right about that."""
        note = self.vault(account, tmp_path).cloud_vault_note()
        assert "cloud Personal Vault" in note
        assert "can't be opened from Linux" in note

    def test_it_is_absent_until_a_container_exists(self, qapp, account, tmp_path):
        assert self.vault(account, tmp_path).state() is VaultState.ABSENT

    def test_availability_is_checked_before_the_feature_is_offered(self, qapp):
        """A toggle that fails on click with "gocryptfs not found" is worse than
        one disabled with a sentence saying what to install."""
        if Vault.available():
            assert Vault.unavailable_reason() in ("", ) or \
                "keyring" in Vault.unavailable_reason().lower()
        else:
            assert "gocryptfs" in Vault.unavailable_reason()

    def test_the_auto_lock_choices_are_windows(self, qapp, account, tmp_path):
        assert AUTO_LOCK_CHOICES == (20, 60, 120, 240)
        vault = self.vault(account, tmp_path)
        vault.set_auto_lock_minutes(60)
        assert vault.auto_lock_minutes() == 60

    def test_an_invalid_interval_falls_back_to_the_default(self, qapp, account,
                                                           tmp_path):
        vault = self.vault(account, tmp_path)
        vault.set_auto_lock_minutes(7)
        assert vault.auto_lock_minutes() == AUTO_LOCK_CHOICES[0]

    def test_the_warning_fires_once_not_per_tick(self, qapp, account, tmp_path,
                                                 monkeypatch):
        """The BUILD_PLAN's acceptance case. A toast reappearing every thirty
        seconds for five minutes would be the most irritating thing here."""
        clock = Clock()
        vault = self.vault(account, tmp_path, clock)
        monkeypatch.setattr(Vault, "is_unlocked", lambda self: True)
        warnings: list[int] = []
        vault.warning.connect(warnings.append)

        vault.touch()
        clock.advance((AUTO_LOCK_CHOICES[0] - WARN_BEFORE_MIN + 1) * 60)
        for _ in range(10):
            vault._on_tick()
        assert len(warnings) == 1

    def test_using_the_vault_earns_a_fresh_warning(self, qapp, account, tmp_path,
                                                   monkeypatch):
        clock = Clock()
        vault = self.vault(account, tmp_path, clock)
        monkeypatch.setattr(Vault, "is_unlocked", lambda self: True)
        warnings: list[int] = []
        vault.warning.connect(warnings.append)

        vault.touch()
        clock.advance((AUTO_LOCK_CHOICES[0] - WARN_BEFORE_MIN + 1) * 60)
        vault._on_tick()
        vault.touch()
        clock.advance((AUTO_LOCK_CHOICES[0] - WARN_BEFORE_MIN + 1) * 60)
        vault._on_tick()
        assert len(warnings) == 2

    def test_it_locks_when_the_idle_time_runs_out(self, qapp, account, tmp_path,
                                                  monkeypatch):
        clock = Clock()
        vault = self.vault(account, tmp_path, clock)
        monkeypatch.setattr(Vault, "is_unlocked", lambda self: True)
        locked: list[bool] = []
        monkeypatch.setattr(Vault, "lock", lambda self: locked.append(True) or True)

        vault.touch()
        clock.advance((AUTO_LOCK_CHOICES[0] + 1) * 60)
        vault._on_tick()
        assert locked == [True]

    def test_unlocked_is_read_from_the_kernel_not_remembered(self, qapp, account,
                                                             tmp_path):
        """The mount can go away underneath us; a remembered flag would claim a
        vault is open when its contents are unreachable."""
        assert self.vault(account, tmp_path).is_unlocked() is False


# ═════════════════════════════════════════════════════════════════════════════
# Extras
# ═════════════════════════════════════════════════════════════════════════════

class TestScreenshots:

    def test_a_file_that_is_still_growing_is_not_moved(self, qapp, account,
                                                       tmp_path):
        """A screenshot tool writes its PNG in pieces; moving it mid-write
        uploads a truncated image and the only clue is a grey thumbnail."""
        clock = Clock()
        source = tmp_path / "Screenshots"
        source.mkdir()
        shot = source / "Screenshot.png"
        shot.write_bytes(b"partial")

        watcher = ScreenshotWatcher(account, source=source, monotonic=clock)
        watcher.note(shot)
        shot.write_bytes(b"partial and then some more")
        watcher._settle()
        assert shot.exists()

    def test_a_settled_file_is_moved_in(self, qapp, account, tmp_path):
        clock = Clock()
        source = tmp_path / "Screenshots"
        source.mkdir()
        shot = source / "Screenshot.png"
        shot.write_bytes(b"complete")

        watcher = ScreenshotWatcher(account, source=source, monotonic=clock)
        watcher.note(shot)
        clock.advance(5)
        watcher._settle()
        assert not shot.exists()
        assert (Path(account.sync_root) / "Pictures/Screenshots"
                / "Screenshot.png").read_bytes() == b"complete"

    def test_only_images_are_considered(self, qapp, account, tmp_path):
        source = tmp_path / "Screenshots"
        source.mkdir()
        other = source / "notes.txt"
        other.write_text("x")
        watcher = ScreenshotWatcher(account, source=source, monotonic=Clock())
        watcher.note(other)
        watcher._settle()
        assert other.exists()

    def test_the_folder_comes_from_xdg_not_a_hardcoded_path(self, qapp, account,
                                                            monkeypatch):
        """A user who moved ~/Pictures — including into OneDrive with KFM — must
        not have their screenshots watched at the old path."""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)
        target = Path.home() / "Elsewhere" / "Pictures"
        write_user_dirs({KfmFolder.PICTURES: target})
        from onedriveui.sync.extras import screenshots_dir

        assert screenshots_dir() == target / "Screenshots"


class TestCameraImport:

    def test_it_copies_and_verifies_rather_than_moving(self, qapp, account,
                                                       tmp_path):
        """The source is removable and can be unplugged mid-operation; a
        half-moved photo library is recoverable from neither end."""
        media = tmp_path / "CAMERA" / "DCIM"
        media.mkdir(parents=True)
        (media / "IMG_0001.jpg").write_bytes(b"\xff\xd8photo")

        count, total = CameraImporter(account).import_from(tmp_path / "CAMERA")
        assert (count, total) == (1, 7)
        assert (media / "IMG_0001.jpg").exists()          # still on the card
        assert (Path(account.sync_root) / "Pictures/Imported"
                / "IMG_0001.jpg").read_bytes() == b"\xff\xd8photo"

    def test_remove_after_is_opt_in(self, qapp, account, tmp_path):
        media = tmp_path / "CAMERA"
        media.mkdir()
        (media / "IMG_0002.jpg").write_bytes(b"x")
        CameraImporter(account).import_from(media, remove_after=True)
        assert not (media / "IMG_0002.jpg").exists()

    def test_an_already_imported_file_is_not_copied_twice(self, qapp, account,
                                                          tmp_path):
        media = tmp_path / "CAMERA"
        media.mkdir()
        (media / "IMG_0003.jpg").write_bytes(b"same")
        importer = CameraImporter(account)
        importer.import_from(media)
        count, _total = importer.import_from(media)
        assert count == 0

    def test_non_media_files_are_left_alone(self, qapp, account, tmp_path):
        media = tmp_path / "CAMERA"
        media.mkdir()
        (media / "AUTORUN.INF").write_text("x")
        assert CameraImporter(account).candidates(media) == []

    def test_the_suffix_list_covers_raw_and_video(self):
        assert ".dng" in IMAGE_SUFFIXES
        assert ".mov" in IMAGE_SUFFIXES
        assert ".heic" in IMAGE_SUFFIXES
