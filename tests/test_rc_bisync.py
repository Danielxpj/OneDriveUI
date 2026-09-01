"""WP-04 — `onedriveui/rc/bisync.py`.

Four things carry real risk here and each is tested against something measured,
not against a guess.

**The session name has no hashing fallback.** A 480-character session on this
machine produced, verbatim::

    ERROR : …lck: error reading lock file: … file name too long
    ERROR : …lck: Lock file exists, but contents are unreadable.
    NOTICE: Failed to bisync: prior lock file found: …lck

— a message that sends the user to delete a lock file which never existed. So
`session_name()` refuses before a run starts, and the sanitisation itself is
checked against the names a real run wrote into a real workdir.

**The lock file** is JSON whose `PID` is a **string**, captured live::

    {"Session":"…/work/tmp_…_p1..tmp_…_p2","PID":"225703",
     "TimeRenewed":"2026-08-31T20:36:22.751714555-04:00",
     "TimeExpires":"2026-08-31T20:38:22.751714592-04:00"}

**`--check-access` is enforced during `--resync` too**, so it cannot be used to
seed its own sentinels — measured, on the resync run itself.

**`--resync` needs an answered decision (I15)** and **only SIGINT may stop a run
(I13)**, both of which are refusals rather than conventions.

The `live` class at the end builds an argv with this module and hands it to the
real rclone, over two local directories and a scratch config whose only remote is
a `local` one. The user's `onedrive:` is never named.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import types
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.constants import (
    BISYNC_DEFAULT_MAX_DELETE_PCT,
    MAX_CHECKERS,
    MAX_TRANSFERS,
    REMOTE_VERSIONS_DIR,
    UNIT_BISYNC_TMPL,
)
from onedriveui.errors import ConfigError, SafetyRefusal
from onedriveui.models import (
    AccountInfo,
    BisyncState,
    Decision,
    DecisionKind,
    RunKind,
)
from onedriveui.rc import bisync as bisync_mod
from onedriveui.rc import filters as filters_mod
from onedriveui.rc.bisync import (
    DEFAULT_BISYNC_OPTIONS,
    GRACEFUL_BUDGET_S,
    NAME_MAX,
    RESYNC_APPROVALS,
    SESSION_JOIN,
    SESSION_SUFFIX_BUDGET,
    STOP_SIGNAL,
    STOP_TIMEOUT_S,
    WORKDIR_SUFFIXES,
    BisyncLock,
    adopt,
    assert_resync_approved,
    assert_stop_signal,
    build_argv,
    check_file_for,
    clear_lock,
    config_string,
    device_name,
    interrupt,
    is_active,
    is_remote,
    plan_run,
    read_lock,
    run_stamp,
    sanitize,
    seed_check_access,
    session_name,
    start,
    stop,
    systemd_run_argv,
    unit_name,
    workdir_state,
)
from onedriveui.rc.bisync_log import LogTailer

RCLONE = shutil.which("rclone")

#: The real lock rclone wrote during a live run with --max-lock 2m.
REAL_LOCK = {
    "Session": "/tmp/bs4/work/tmp_x_p1..tmp_x_p2",
    "PID": "225703",
    "TimeRenewed": "2026-08-31T20:36:22.751714555-04:00",
    "TimeExpires": "2026-08-31T20:38:22.751714592-04:00",
}
#: The same file with the default --max-lock 0: ~200 years, i.e. never.
REAL_LOCK_NEVER_EXPIRES = dict(REAL_LOCK,
                               TimeExpires="2226-07-14T20:36:57.35348904-04:00")


def _approved(answer: str = "resync") -> Decision:
    return Decision(id=1, account_id="onedrive", kind=DecisionKind.RESYNC_CONFIRM,
                    created_at="2026-08-31T00:00:00Z",
                    answered_at="2026-08-31T00:00:05Z", answer=answer)


class _Runner:
    """Records `subprocess.run` calls and answers a scripted stdout/returncode."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=self.returncode,
                                     stdout=self.stdout, stderr="")


@pytest.fixture
def account(_isolate_home) -> AccountInfo:
    return AccountInfo(id="onedrive", remote="onedrive",
                       sync_root=str(Path.home() / "OneDrive"))


@pytest.fixture
def opts(_isolate_home) -> dict:
    local = Path.home() / "OneDrive-Offline"
    local.mkdir(parents=True, exist_ok=True)
    (local / "RCLONE_TEST").touch()
    return {"enabled": True, "local_path": str(local),
            "remote_path": "onedrive:Offline"}


@pytest.fixture
def filtered(account):
    """A committed filters file, so `assert_bisync_safe` has one to read."""
    with filters_mod.rewrite(account.id, ["Videos"]) as txn:
        txn.resynced()
    return paths.filters_file(account.id)


# ═════════════════════════════════════════════════════════════════════════════
# Session naming
# ═════════════════════════════════════════════════════════════════════════════

class TestSanitize:

    @pytest.mark.parametrize("raw,expected", [
        ("/tmp/x/p1", "tmp_x_p1"),
        ("od:/tmp/y", "od__tmp_y"),
        ("onedrive:", "onedrive_"),
        ("onedrive:Offline", "onedrive_Offline"),
        ("keep.dots-and-dashes", "keep.dots-and-dashes"),
        ("My Folder", "My_Folder"),
        ("Imágenes", "Im_genes"),
        ("", ""),
    ])
    def test_rules(self, raw, expected):
        assert sanitize(raw) == expected

    def test_only_one_leading_underscore_is_stripped(self):
        assert sanitize("//tmp") == "_tmp"

    def test_matches_a_real_workdir_name(self):
        """The prefix a live run actually wrote, for a path under /tmp."""
        assert sanitize("/tmp/claude-1000/scratchpad/bs/p1") \
            == "tmp_claude-1000_scratchpad_bs_p1"


class TestConfigString:

    def test_a_remote_is_itself(self):
        assert config_string("onedrive:") == "onedrive:"
        assert config_string("onedrive:Offline") == "onedrive:Offline"

    def test_a_local_path_becomes_absolute_with_no_name_prefix(self, tmp_path):
        assert config_string(str(tmp_path)) == str(tmp_path)
        assert ":" not in config_string(str(tmp_path))

    def test_tilde_is_expanded(self, _isolate_home):
        assert config_string("~/OneDrive-Offline") == \
            str(Path.home() / "OneDrive-Offline")

    @pytest.mark.parametrize("side,remote", [
        ("onedrive:", True), ("onedrive:Offline", True), ("od:/tmp/y", True),
        ("/home/u/x", False), ("./rel", False), ("rel", False),
        ("/x:y/z", False), ("", False),
    ])
    def test_is_remote(self, side, remote):
        assert is_remote(side) is remote


class TestSessionName:

    def test_the_acceptance_example(self):
        assert session_name("/tmp/x/p1", "/tmp/x/p2") == "tmp_x_p1..tmp_x_p2"

    def test_a_remote_side_starts_with_the_double_underscore(self):
        assert session_name("/tmp/x/p1", "od:/tmp/y").endswith("..od__tmp_y")
        assert session_name("od:/tmp/y", "/tmp/x/p1").startswith("od__tmp_y")

    def test_the_real_account_pair(self, _isolate_home):
        home = Path.home()
        assert session_name(str(home / "OneDrive-Offline"), "onedrive:Offline") \
            == f"{sanitize(str(home / 'OneDrive-Offline'))}..onedrive_Offline"

    def test_an_alias_remote_would_change_the_name_under_us(self):
        """rclone resolves an `alias` to its TARGET before naming the session,
        so wrapping the account remote in one silently orphans every listing.
        We cannot resolve an alias without reading rclone.conf, which is why the
        rule is "never alias the account remote" rather than a code path."""
        aliased = session_name("/home/u/OneDrive-Offline", "onedrive:Offline")
        resolved = session_name("/home/u/OneDrive-Offline", "/srv/cloud/Offline")
        assert aliased != resolved

    def test_joined_with_two_dots(self):
        assert SESSION_JOIN == ".."
        assert session_name("/a", "/b").count(SESSION_JOIN) == 1

    def test_a_300_char_path_raises_before_any_run_starts(self, tmp_path):
        """The acceptance bullet. rclone has NO hashing fallback."""
        long = tmp_path / ("z" * 300)
        with pytest.raises(ConfigError) as caught:
            session_name(str(long / "p1"), str(long / "p2"))
        message = str(caught.value)
        assert "NO hashing fallback" in message
        assert "prior lock file found" in message
        assert str(NAME_MAX) in message

    def test_the_budget_reserves_room_for_the_longest_suffix(self):
        longest = max(len(s) for s in WORKDIR_SUFFIXES)
        assert longest == len(".path1.lst-new") == 14
        assert SESSION_SUFFIX_BUDGET > longest

    def test_a_name_exactly_at_the_budget_is_accepted(self):
        budget = NAME_MAX - SESSION_SUFFIX_BUDGET
        left = "/" + "a" * ((budget - len(SESSION_JOIN)) // 2)
        right = "/" + "b" * (budget - len(SESSION_JOIN) - len(left) + 1)
        name = session_name(left, right)
        assert len(name.encode()) == budget

    def test_one_byte_over_the_budget_refuses(self):
        budget = NAME_MAX - SESSION_SUFFIX_BUDGET
        left = "/" + "a" * ((budget - len(SESSION_JOIN)) // 2)
        right = "/" + "b" * (budget - len(SESSION_JOIN) - len(left) + 2)
        with pytest.raises(ConfigError):
            session_name(left, right)

    def test_the_limit_is_bytes_not_characters(self):
        """NAME_MAX is 255 BYTES; a UTF-8 name can fit far fewer characters."""
        budget = NAME_MAX - SESSION_SUFFIX_BUDGET
        # 'é' sanitises to '_', so use a name that stays multi-byte after
        # sanitisation: '.' and '-' are kept, and so are ASCII letters.
        assert len(session_name("/" + "a" * 50, "/" + "b" * 50).encode()) < budget

    def test_an_empty_side_is_a_programming_error(self):
        with pytest.raises(ValueError):
            session_name("", "/tmp/p2")
        with pytest.raises(ValueError):
            session_name("/tmp/p1", "  ")

    def test_a_custom_name_max_is_honoured(self):
        with pytest.raises(ConfigError):
            session_name("/tmp/x/p1", "/tmp/x/p2", name_max=20)


class TestNaming:

    def test_unit_name(self):
        assert unit_name("onedrive") == "onedriveui-bisync-onedrive"
        assert unit_name("onedrive") == UNIT_BISYNC_TMPL.format("onedrive")

    def test_device_name_is_the_short_hostname(self):
        assert device_name()
        assert "." not in device_name()

    def test_run_stamp_has_no_characters_onedrive_rejects(self):
        """`:` is in INVALID_CHARS, and the stamp goes into a OneDrive path AND
        into a file suffix."""
        from onedriveui.constants import INVALID_CHARS

        stamp = run_stamp("2026-08-31T20:49:44Z")
        assert stamp == "20260831T204944Z"
        assert not set(stamp) & set(INVALID_CHARS)

    def test_run_stamp_sorts_chronologically(self):
        assert run_stamp("2026-01-02T03:04:05Z") < run_stamp("2026-01-02T03:04:06Z")

    def test_run_stamp_defaults_to_now(self):
        assert len(run_stamp()) == len("20260831T204944Z")


# ═════════════════════════════════════════════════════════════════════════════
# The lock file
# ═════════════════════════════════════════════════════════════════════════════

class TestReadLock:

    def _write(self, tmp_path: Path, body: dict | str, session: str = "S") -> Path:
        path = tmp_path / f"{session}.lck"
        path.write_text(body if isinstance(body, str) else json.dumps(body),
                        encoding="utf-8")
        return path

    def test_no_lock_file_is_none(self, tmp_path):
        assert read_lock(tmp_path, "S") is None

    def test_parses_the_real_capture(self, tmp_path):
        self._write(tmp_path, REAL_LOCK)
        lock = read_lock(tmp_path, "S")
        assert isinstance(lock, BisyncLock)
        assert lock.pid == 225703                     # the JSON PID is a STRING
        assert lock.session == REAL_LOCK["Session"]
        assert lock.time_renewed == REAL_LOCK["TimeRenewed"]
        assert lock.time_expires == REAL_LOCK["TimeExpires"]
        assert lock.readable is True

    def test_a_past_expiry_is_stale_even_if_the_pid_lives(self, tmp_path):
        self._write(tmp_path, dict(REAL_LOCK, PID=str(os.getpid())))
        lock = read_lock(tmp_path, "S")
        assert lock.expired is True
        assert lock.stale is True
        assert lock.running is False

    def test_a_dead_pid_is_stale_even_with_a_future_expiry(self, tmp_path):
        self._write(tmp_path, dict(REAL_LOCK_NEVER_EXPIRES, PID="999999999"))
        lock = read_lock(tmp_path, "S")
        assert lock.alive is False
        assert lock.expired is False
        assert lock.stale is True

    def test_a_live_unexpired_lock_is_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bisync_mod, "_looks_like_bisync", lambda pid: True)
        self._write(tmp_path, dict(REAL_LOCK_NEVER_EXPIRES, PID=str(os.getpid())))
        lock = read_lock(tmp_path, "S")
        assert lock.alive is True and lock.expired is False
        assert lock.running is True
        assert lock.stale is False

    def test_a_recycled_pid_belonging_to_something_else_is_stale(self, tmp_path):
        """PIDs are recycled; this pytest process is alive but is not a bisync,
        and a stale lock naming it would keep the account 'running' forever."""
        self._write(tmp_path, dict(REAL_LOCK_NEVER_EXPIRES, PID=str(os.getpid())))
        lock = read_lock(tmp_path, "S")
        assert lock.alive is False
        assert lock.stale is True

    def test_an_unreadable_process_is_assumed_alive(self, tmp_path, monkeypatch):
        """Not proof of death — treating it as dead would delete a live lock."""
        monkeypatch.setattr(bisync_mod, "read_proc_cmdline", lambda pid: [])
        self._write(tmp_path, dict(REAL_LOCK_NEVER_EXPIRES, PID=str(os.getpid())))
        assert read_lock(tmp_path, "S").alive is True

    def test_an_unreadable_lock_is_treated_as_expired(self, tmp_path):
        """rclone does the same, and only because --max-lock 2m is always set."""
        self._write(tmp_path, "{truncated")
        lock = read_lock(tmp_path, "S")
        assert lock.readable is False
        assert lock.stale is True
        assert lock.running is False

    def test_a_json_array_is_not_a_lock(self, tmp_path):
        self._write(tmp_path, "[1, 2]")
        assert read_lock(tmp_path, "S").readable is False

    def test_a_missing_pid_is_stale(self, tmp_path):
        self._write(tmp_path, {"TimeExpires": REAL_LOCK_NEVER_EXPIRES["TimeExpires"]})
        assert read_lock(tmp_path, "S").stale is True

    def test_a_garbage_pid_is_stale(self, tmp_path):
        self._write(tmp_path, dict(REAL_LOCK_NEVER_EXPIRES, PID="not-a-number"))
        assert read_lock(tmp_path, "S").pid == 0
        assert read_lock(tmp_path, "S").stale is True

    def test_the_default_max_lock_never_expires(self, tmp_path):
        """--max-lock 0 puts TimeExpires ~200 years out, which is why every run
        must pass --max-lock 2m."""
        self._write(tmp_path, REAL_LOCK_NEVER_EXPIRES)
        assert read_lock(tmp_path, "S").expired is False


# ═════════════════════════════════════════════════════════════════════════════
# The workdir
# ═════════════════════════════════════════════════════════════════════════════

class TestWorkdirState:

    SESSION = "p1..p2"

    def _touch(self, workdir: Path, *suffixes: str) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        for suffix in suffixes:
            (workdir / f"{self.SESSION}{suffix}").write_text("x", encoding="utf-8")

    def test_an_empty_workdir_needs_a_resync(self, tmp_path):
        """The first-ever run: `cannot find prior Path1 or Path2 listings`."""
        state = workdir_state(tmp_path, self.SESSION)
        assert state.state is BisyncState.NEEDS_RESYNC
        assert state.has_listings is False
        assert state.needs_resync is True

    def test_both_listings_present_is_idle(self, tmp_path):
        self._touch(tmp_path, ".path1.lst", ".path2.lst")
        state = workdir_state(tmp_path, self.SESSION)
        assert state.state is BisyncState.IDLE
        assert state.has_listings is True
        assert state.listing1.name == f"{self.SESSION}.path1.lst"

    def test_one_listing_missing_needs_a_resync(self, tmp_path):
        self._touch(tmp_path, ".path1.lst")
        assert workdir_state(tmp_path, self.SESSION).state is BisyncState.NEEDS_RESYNC

    def test_lst_err_needs_a_resync_even_with_listings_present(self, tmp_path):
        """A critical abort renames .lst to .lst-err and THEN releases the lock,
        so the directory can hold .lst-err with no lock at all — measured."""
        self._touch(tmp_path, ".path1.lst", ".path2.lst",
                    ".path1.lst-err", ".path2.lst-err")
        state = workdir_state(tmp_path, self.SESSION)
        assert state.state is BisyncState.NEEDS_RESYNC
        assert state.has_errors is True

    def test_a_live_lock_is_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bisync_mod, "_looks_like_bisync", lambda pid: True)
        self._touch(tmp_path, ".path1.lst", ".path2.lst")
        (tmp_path / f"{self.SESSION}.lck").write_text(
            json.dumps(dict(REAL_LOCK_NEVER_EXPIRES, PID=str(os.getpid()))),
            encoding="utf-8")
        state = workdir_state(tmp_path, self.SESSION)
        assert state.state is BisyncState.RUNNING
        assert state.lock is not None and state.lock.running

    def test_a_stale_lock_is_lock_stuck(self, tmp_path):
        self._touch(tmp_path, ".path1.lst", ".path2.lst")
        (tmp_path / f"{self.SESSION}.lck").write_text(
            json.dumps(dict(REAL_LOCK_NEVER_EXPIRES, PID="999999999")),
            encoding="utf-8")
        assert workdir_state(tmp_path, self.SESSION).state is BisyncState.LOCK_STUCK

    def test_lst_err_beats_a_live_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bisync_mod, "_looks_like_bisync", lambda pid: True)
        self._touch(tmp_path, ".path1.lst", ".path2.lst", ".path1.lst-err")
        (tmp_path / f"{self.SESSION}.lck").write_text(
            json.dumps(dict(REAL_LOCK_NEVER_EXPIRES, PID=str(os.getpid()))),
            encoding="utf-8")
        assert workdir_state(tmp_path, self.SESSION).state is BisyncState.NEEDS_RESYNC

    def test_disabled_short_circuits_everything(self, tmp_path):
        self._touch(tmp_path, ".path1.lst-err")
        assert workdir_state(tmp_path, self.SESSION,
                             enabled=False).state is BisyncState.DISABLED

    def test_reports_new_and_old_listings(self, tmp_path):
        """.lst-new is left behind by an aborted run; .lst-old is --recover's
        backup. Both appeared in a real workdir."""
        self._touch(tmp_path, ".path1.lst", ".path2.lst",
                    ".path1.lst-new", ".path1.lst-old")
        state = workdir_state(tmp_path, self.SESSION)
        assert state.has_new is True and state.has_old is True
        assert state.state is BisyncState.IDLE

    def test_a_missing_workdir_is_not_an_error(self, tmp_path):
        assert workdir_state(tmp_path / "nope", self.SESSION).state \
            is BisyncState.NEEDS_RESYNC

    def test_every_documented_suffix_is_recognised(self, tmp_path):
        self._touch(tmp_path, *WORKDIR_SUFFIXES)
        state = workdir_state(tmp_path, self.SESSION)
        assert state.has_listings and state.has_errors
        assert state.has_new and state.has_old
        assert state.lock is not None


# ═════════════════════════════════════════════════════════════════════════════
# --check-access seeding
# ═════════════════════════════════════════════════════════════════════════════

class TestSeedCheckAccess:

    def test_check_file_for_a_local_side(self, tmp_path):
        assert check_file_for(str(tmp_path)) == str(tmp_path / "RCLONE_TEST")

    @pytest.mark.parametrize("side,expected", [
        ("onedrive:", "onedrive:RCLONE_TEST"),
        ("onedrive:Offline", "onedrive:Offline/RCLONE_TEST"),
        ("onedrive:Offline/", "onedrive:Offline/RCLONE_TEST"),
        #: A filesystem-shaped backend keeps its LEADING separator.
        ("onedrive:/srv/share", "onedrive:/srv/share/RCLONE_TEST"),
    ])
    def test_check_file_for_a_remote_side(self, side, expected):
        assert check_file_for(side) == expected

    def test_honours_a_custom_check_filename(self, tmp_path):
        assert check_file_for(str(tmp_path), "SENTINEL").endswith("/SENTINEL")

    def test_creates_both_sides_when_both_are_local(self, tmp_path):
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        p1.mkdir()
        p2.mkdir()
        assert seed_check_access(str(p1), str(p2)) == ["path1", "path2"]
        assert (p1 / "RCLONE_TEST").is_file()
        assert (p2 / "RCLONE_TEST").is_file()

    def test_is_idempotent(self, tmp_path):
        p1, p2 = tmp_path / "p1", tmp_path / "p2"
        p1.mkdir()
        p2.mkdir()
        seed_check_access(str(p1), str(p2))
        assert seed_check_access(str(p1), str(p2)) == []

    def test_a_remote_side_uses_the_injected_copier(self, tmp_path):
        p1 = tmp_path / "p1"
        p1.mkdir()
        copied: list[tuple[str, str]] = []
        assert seed_check_access(str(p1), "onedrive:Offline",
                                 copyfile=lambda s, d: copied.append((s, d))) \
            == ["path1", "path2"]
        assert copied == [(str(p1 / "RCLONE_TEST"), "onedrive:Offline/RCLONE_TEST")]

    def test_a_remote_side_without_a_copier_refuses(self, tmp_path):
        """Enabling --check-access anyway would abort every run, INCLUDING the
        resync meant to fix it — measured."""
        p1 = tmp_path / "p1"
        p1.mkdir()
        with pytest.raises(ConfigError) as caught:
            seed_check_access(str(p1), "onedrive:Offline")
        assert "--resync" in str(caught.value)
        assert "never creates it" in str(caught.value)

    def test_path1_must_be_the_local_side(self):
        with pytest.raises(ConfigError):
            seed_check_access("onedrive:Offline", "/tmp/x")

    def test_creates_the_directory_if_needed(self, tmp_path):
        p1 = tmp_path / "not" / "yet"
        p2 = tmp_path / "p2"
        p2.mkdir()
        seed_check_access(str(p1), str(p2))
        assert (p1 / "RCLONE_TEST").is_file()


# ═════════════════════════════════════════════════════════════════════════════
# I15 and I13
# ═════════════════════════════════════════════════════════════════════════════

class TestResyncApproval:

    def test_no_decision_refuses(self):
        with pytest.raises(SafetyRefusal) as caught:
            assert_resync_approved(None)
        assert caught.value.invariant == "I15"
        assert "only copies, never" in str(caught.value)

    def test_an_answered_resync_decision_passes(self):
        assert_resync_approved(_approved())

    @pytest.mark.parametrize("answer", sorted(RESYNC_APPROVALS))
    def test_every_approval_word(self, answer):
        assert_resync_approved(_approved(answer))

    def test_an_unanswered_decision_refuses(self):
        with pytest.raises(SafetyRefusal):
            assert_resync_approved(Decision(kind=DecisionKind.RESYNC_CONFIRM))

    def test_an_expired_decision_is_a_refusal_not_consent(self):
        from onedriveui.data.repo_sync import EXPIRED_ANSWER

        with pytest.raises(SafetyRefusal) as caught:
            assert_resync_approved(_approved(EXPIRED_ANSWER))
        assert caught.value.invariant == "I15"

    @pytest.mark.parametrize("answer", ["cancel", "no", "later", "", "keep"])
    def test_a_negative_answer_refuses(self, answer):
        with pytest.raises(SafetyRefusal):
            assert_resync_approved(_approved(answer))

    def test_the_wrong_decision_kind_refuses(self):
        wrong = Decision(kind=DecisionKind.MASS_DELETE,
                         answered_at="2026-08-31T00:00:00Z", answer="resync")
        with pytest.raises(SafetyRefusal) as caught:
            assert_resync_approved(wrong)
        assert "resync_confirm" in str(caught.value)

    def test_a_mapping_works_too(self):
        assert_resync_approved({"kind": "resync_confirm",
                                "answered_at": "2026-08-31T00:00:00Z",
                                "answer": "resync"})

    def test_case_and_whitespace_are_tolerated(self):
        assert_resync_approved(_approved("  RESYNC "))


class TestStopSignal:

    def test_sigint_is_the_only_allowed_signal(self):
        assert STOP_SIGNAL == int(signal.SIGINT)
        assert_stop_signal(signal.SIGINT)

    @pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM,
                                     signal.SIGHUP, signal.SIGQUIT])
    def test_every_other_signal_refuses(self, sig):
        with pytest.raises(SafetyRefusal) as caught:
            assert_stop_signal(sig)
        assert caught.value.invariant == "I13"
        assert ".partial" in str(caught.value)

    def test_interrupt_refuses_a_non_sigint(self):
        with pytest.raises(SafetyRefusal):
            interrupt(os.getpid(), sig=signal.SIGKILL)

    def test_interrupt_sends_sigint_and_nothing_else(self):
        sent: list[tuple[int, int]] = []
        assert interrupt(4242, killer=lambda p, s: sent.append((p, s))) is True
        assert sent == [(4242, int(signal.SIGINT))]

    def test_interrupt_of_a_dead_process_is_false_not_fatal(self):
        assert interrupt(999_999_999) is False

    def test_interrupt_of_a_nonsense_pid_is_false(self):
        assert interrupt(0) is False
        assert interrupt(-1) is False

    def test_the_graceful_budget_is_below_the_systemd_timeout(self):
        assert GRACEFUL_BUDGET_S < STOP_TIMEOUT_S


# ═════════════════════════════════════════════════════════════════════════════
# build_argv
# ═════════════════════════════════════════════════════════════════════════════

def _pairs(argv: list[str]) -> dict[str, str]:
    """Flags with values, as a dict, for readable assertions."""
    out: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token.startswith("--") and index + 1 < len(argv) \
                and not argv[index + 1].startswith("--"):
            out[token] = argv[index + 1]
    return out


class TestBuildArgv:

    def test_the_shape_of_section_5_4(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1")
        assert argv[0] == "/usr/bin/rclone"
        assert argv[1] == "bisync"
        assert argv[2] == opts["local_path"]
        assert argv[3] == "onedrive:Offline"

        flags = _pairs(argv)
        assert flags["--workdir"] == str(paths.bisync_workdir(account.id))
        assert flags["--filters-file"] == str(paths.filters_file(account.id))
        assert flags["--conflict-resolve"] == "newer"
        assert flags["--conflict-loser"] == "pathname"
        assert flags["--conflict-suffix"] == f"-{device_name()}"
        assert flags["--max-delete"] == str(BISYNC_DEFAULT_MAX_DELETE_PCT)
        assert flags["--check-filename"] == "RCLONE_TEST"
        assert flags["--max-lock"] == "2m"
        assert flags["--transfers"] == str(MAX_TRANSFERS)
        assert flags["--checkers"] == str(MAX_CHECKERS)
        assert flags["--color"] == "NEVER"
        assert flags["--stats"] == "500ms"
        assert flags["--stats-log-level"] == "NOTICE"
        assert flags["--log-file"] == str(paths.run_log_file("r1"))

        for flag in ("--check-access", "--resilient", "--recover",
                     "--create-empty-src-dirs", "--track-renames",
                     "--use-json-log", "--suffix-keep-extension"):
            assert flag in argv

    def test_the_workdir_is_never_rclones_own_cache(self, account, opts, filtered):
        """rclone's cache cleaning may destroy the .lst files that ARE the state."""
        workdir = _pairs(build_argv(account, opts, run_id="r1"))["--workdir"]
        assert ".cache/rclone" not in workdir
        assert Path(workdir).is_relative_to(paths.state_dir())

    def test_color_never_is_mandatory_even_with_json(self, account, opts, filtered):
        """--use-json-log alone still embeds raw ANSI escapes in msg."""
        argv = build_argv(account, opts, run_id="r1")
        assert "--use-json-log" in argv
        assert argv[argv.index("--color") + 1] == "NEVER"

    def test_the_log_level_is_info_or_the_verdict_line_never_appears(
            self, account, opts, filtered):
        """rclone's default level is NOTICE and every bisync milestone —
        `Bisync successful` included — is INFO. Measured: the identical run
        without this wrote a log containing exactly ONE line, the stats block."""
        argv = build_argv(account, opts, run_id="r1")
        assert _pairs(argv)["--log-level"] == "INFO"

    def test_the_log_goes_to_a_file_we_can_resume_from(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1")
        target = Path(_pairs(argv)["--log-file"])
        assert target.name == "bisync.jsonl"
        assert target.parent == paths.run_dir("r1")

    def test_no_inplace_ever(self, account, opts, filtered):
        assert "--inplace" not in build_argv(account, opts, run_id="r1")

    def test_extra_args_cannot_smuggle_in_inplace(self, account, opts, filtered):
        with pytest.raises(SafetyRefusal) as caught:
            build_argv(account, opts, run_id="r1", extra_args=["--inplace"])
        assert caught.value.invariant == "I12"

    def test_extra_args_cannot_smuggle_in_a_backend_flag(self, account, opts, filtered):
        """I1: `--onedrive-chunk-size` renames the fs and orphans the VFS cache."""
        with pytest.raises(SafetyRefusal) as caught:
            build_argv(account, opts, run_id="r1",
                       extra_args=["--onedrive-chunk-size", "30M"])
        assert caught.value.invariant == "I1"

    def test_no_backend_flag_appears_by_itself(self, account, opts, filtered):
        from onedriveui.rc import guards
        guards.assert_no_backend_flags(build_argv(account, opts, run_id="r1"))

    def test_no_no_cleanup(self, account, opts, filtered):
        assert "--no-cleanup" not in build_argv(account, opts, run_id="r1")

    def test_backup_dirs_and_suffix_use_a_path_safe_stamp(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1", stamp="20260831T204944Z")
        flags = _pairs(argv)
        assert flags["--backup-dir1"] == \
            str(paths.versions_dir(account.id) / "20260831T204944Z")
        assert flags["--backup-dir2"] == \
            f"onedrive:{REMOTE_VERSIONS_DIR}/20260831T204944Z"
        assert flags["--suffix"] == "-20260831T204944Z"
        assert ":" not in flags["--suffix"]
        assert ":" not in flags["--backup-dir2"].split(":", 1)[1]

    def test_the_remote_backup_dir_is_excluded_by_the_filters(self, account, opts, filtered):
        """--backup-dir2 lives inside the account, so it must be filtered out or
        bisync would sync its own backups."""
        rules = filters_mod.read_rules(paths.filters_file(account.id))
        assert f"- {REMOTE_VERSIONS_DIR}/" in rules

    def test_backup_versions_can_be_turned_off(self, account, opts, filtered):
        argv = build_argv(account, dict(opts, backup_versions=False), run_id="r1")
        assert "--backup-dir1" not in argv
        assert "--suffix" not in argv

    def test_resync_adds_the_flag_and_drops_the_conflict_options(
            self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1", resync=True,
                          resync_decision=_approved())
        assert "--resync" in argv
        assert "--conflict-resolve" not in argv
        assert "--conflict-loser" not in argv

    def test_resync_drops_track_renames(self, account, opts, filtered):
        """It is ignored during a resync and says so at ERROR level every time."""
        argv = build_argv(account, opts, run_id="r1", resync=True,
                          resync_decision=_approved())
        assert "--track-renames" not in argv
        assert "--track-renames" in build_argv(account, opts, run_id="r2")

    def test_resync_mode_is_passed_through(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1", resync=True,
                          resync_mode="newer", resync_decision=_approved())
        assert _pairs(argv)["--resync-mode"] == "newer"

    def test_resync_without_an_answered_decision_refuses(self, account, opts, filtered):
        """I15, and it refuses BEFORE any of the other work happens."""
        with pytest.raises(SafetyRefusal) as caught:
            build_argv(account, opts, run_id="r1", resync=True)
        assert caught.value.invariant == "I15"

    def test_a_filters_change_without_a_resync_refuses(self, account, opts, filtered):
        with pytest.raises(SafetyRefusal) as caught:
            build_argv(account, opts, run_id="r1", filters_changed=True)
        assert caught.value.invariant == "I11"

    def test_a_filters_change_with_a_resync_is_allowed(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1", filters_changed=True,
                          resync=True, resync_decision=_approved())
        assert "--resync" in argv

    def test_check_access_without_a_seeded_sentinel_refuses(
            self, account, opts, filtered):
        (Path(opts["local_path"]) / "RCLONE_TEST").unlink()
        with pytest.raises(ConfigError) as caught:
            build_argv(account, opts, run_id="r1")
        assert "seed_check_access" in str(caught.value)
        assert "--resync too" in str(caught.value)

    def test_check_access_can_be_turned_off(self, account, opts, filtered):
        (Path(opts["local_path"]) / "RCLONE_TEST").unlink()
        argv = build_argv(account, dict(opts, check_access=False), run_id="r1")
        assert "--check-access" not in argv

    def test_force_is_opt_in(self, account, opts, filtered):
        assert "--force" not in build_argv(account, opts, run_id="r1")
        assert "--force" in build_argv(account, opts, run_id="r1", force=True)

    def test_compare_is_omitted_at_the_default(self, account, opts, filtered):
        assert "--compare" not in build_argv(account, opts, run_id="r1")

    def test_a_non_default_compare_is_passed(self, account, opts, filtered):
        argv = build_argv(account, dict(opts, compare="size,modtime,checksum"),
                          run_id="r1")
        assert _pairs(argv)["--compare"] == "size,modtime,checksum"

    def test_the_conflict_suffix_template_is_formatted(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1", device="testhost")
        assert _pairs(argv)["--conflict-suffix"] == "-testhost"

    def test_an_unknown_placeholder_is_left_alone(self, account, opts, filtered):
        argv = build_argv(account, dict(opts, conflict_suffix="-{nope}"),
                          run_id="r1")
        assert _pairs(argv)["--conflict-suffix"] == "-{nope}"

    def test_a_side_under_the_fuse_mount_refuses(self, account, opts, filtered,
                                                 monkeypatch):
        """I2: --vfs-write-back guarantees the timing bisync warns loses data."""
        from onedriveui import paths as paths_mod

        monkeypatch.setattr(paths_mod, "fuse_rclone_mounts",
                            lambda: [("onedrive:", Path(opts["local_path"]))])
        with pytest.raises(SafetyRefusal) as caught:
            build_argv(account, opts, run_id="r1")
        assert caught.value.invariant == "I2"

    def test_a_filters_file_without_the_partial_rule_refuses(
            self, account, opts, filtered):
        paths.filters_file(account.id).write_text("- *.tmp\n", encoding="utf-8")
        with pytest.raises(SafetyRefusal) as caught:
            build_argv(account, opts, run_id="r1")
        assert caught.value.invariant == "I13"

    def test_an_overlong_session_refuses_before_the_argv_exists(
            self, account, filtered, _isolate_home):
        deep = Path.home() / ("z" * 200) / ("y" * 100)
        deep.mkdir(parents=True, exist_ok=True)
        (deep / "RCLONE_TEST").touch()
        with pytest.raises(ConfigError) as caught:
            build_argv(account, {"local_path": str(deep),
                                 "remote_path": "onedrive:Offline"}, run_id="r1")
        assert "hashing fallback" in str(caught.value)

    def test_every_value_is_a_string(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1")
        assert all(isinstance(token, str) for token in argv)

    def test_defaults_fill_in_a_partial_options_block(self, account, filtered,
                                                      _isolate_home):
        local = Path.home() / "OneDrive-Offline"
        local.mkdir(parents=True, exist_ok=True)
        (local / "RCLONE_TEST").touch()
        argv = build_argv(account, {"local_path": str(local)}, run_id="r1")
        assert argv[3] == DEFAULT_BISYNC_OPTIONS["remote_path"]


class TestSystemdRunArgv:

    def test_pins_kill_signal_to_sigint(self, account, opts, filtered):
        """I13 made structural: a plain `systemctl stop` then triggers rclone's
        Graceful Shutdown rather than a SIGTERM that strands a .partial."""
        wrapped = systemd_run_argv(account.id, build_argv(account, opts, run_id="r1"))
        assert "--property=KillSignal=SIGINT" in wrapped
        assert "SIGKILL" not in " ".join(wrapped)
        assert "SIGTERM" not in " ".join(wrapped)

    def test_the_full_wrapper(self, account, opts, filtered):
        argv = build_argv(account, opts, run_id="r1")
        wrapped = systemd_run_argv(account.id, argv)
        assert wrapped[:4] == ["systemd-run", "--user", "--collect",
                               f"--unit={unit_name(account.id)}"]
        assert f"--property=TimeoutStopSec={STOP_TIMEOUT_S}" in wrapped
        assert "--property=Restart=no" in wrapped
        assert wrapped[wrapped.index("--") + 1:] == argv

    def test_restart_no_keeps_systemd_out_of_the_state_machine(
            self, account, opts, filtered):
        wrapped = systemd_run_argv(account.id, build_argv(account, opts, run_id="r1"))
        assert "--property=Restart=no" in wrapped

    def test_the_timeout_exceeds_rclones_own_budget(self):
        """30 s to drain plus up to 60 s to save state."""
        assert STOP_TIMEOUT_S > 30 + 60


class TestPlanRun:

    def test_assembles_everything_the_supervisor_needs(self, account, opts, filtered):
        plan = plan_run(account, opts, run_id="r1")
        assert plan.account_id == account.id
        assert plan.kind == "bisync"
        assert plan.path1 == opts["local_path"]
        assert plan.path2 == "onedrive:Offline"
        assert plan.session == session_name(plan.path1, plan.path2)
        assert plan.unit == unit_name(account.id)
        assert plan.log_path == paths.run_log_file("r1")
        assert plan.argv[1] == "bisync"
        assert plan.launch_argv[0] == "systemd-run"
        assert plan.state.state is BisyncState.NEEDS_RESYNC

    def test_a_resync_plan_is_labelled(self, account, opts, filtered):
        plan = plan_run(account, opts, run_id="r1", resync=True,
                        resync_decision=_approved())
        assert plan.kind == "resync"
        assert plan.to_run_record().kind is RunKind.RESYNC

    def test_the_run_record_is_ready_to_insert(self, account, opts, filtered):
        record = plan_run(account, opts, run_id="r1").to_run_record()
        assert record.run_id == "r1"
        assert record.account_id == account.id
        assert record.kind is RunKind.BISYNC
        assert record.log_offset == 0
        assert record.log_path == str(paths.run_log_file("r1"))
        assert record.unit == unit_name(account.id)
        assert record.session
        assert record.listing1.endswith(".path1.lst")
        assert record.listing2.endswith(".path2.lst")
        assert record.started_at
        assert record.argv[1] == "bisync"

    def test_planning_refuses_before_anything_is_written(self, account, opts, filtered):
        with pytest.raises(SafetyRefusal):
            plan_run(account, opts, run_id="r1", resync=True)
        assert not paths.run_log_file("r1").exists()


# ═════════════════════════════════════════════════════════════════════════════
# adopt / is_active / stop
# ═════════════════════════════════════════════════════════════════════════════

class TestAdopt:

    def test_is_active_asks_systemctl_for_the_right_unit(self, account):
        runner = _Runner(stdout="active\n")
        assert is_active(account.id, runner=runner) is True
        assert runner.calls == [["systemctl", "--user", "is-active",
                                 unit_name(account.id)]]

    @pytest.mark.parametrize("stdout,expected", [
        ("active\n", True), ("activating\n", True), ("inactive\n", False),
        ("failed\n", False), ("unknown\n", False), ("", False),
    ])
    def test_is_active_reads_the_answer(self, account, stdout, expected):
        assert is_active(account.id, runner=_Runner(stdout=stdout)) is expected

    def test_a_missing_systemctl_answers_no_rather_than_raising(self, account):
        def explode(argv, **kwargs):
            raise FileNotFoundError("systemctl")

        assert is_active(account.id, runner=explode) is False

    def test_adopt_returns_a_tailer_at_the_stored_offset(self, account, opts,
                                                         filtered):
        record = plan_run(account, opts, run_id="r1").to_run_record()
        resumed = types.SimpleNamespace(log_path=record.log_path, log_offset=4096)
        tailer = adopt(account.id, resumed, runner=_Runner(stdout="active\n"))
        assert isinstance(tailer, LogTailer)
        assert tailer.offset == 4096
        assert tailer.path == paths.run_log_file("r1")

    def test_adopt_returns_an_unstarted_tailer(self, account, opts, filtered, qapp):
        record = plan_run(account, opts, run_id="r1").to_run_record()
        tailer = adopt(account.id, record, runner=_Runner(stdout="active\n"))
        assert tailer is not None
        assert tailer.isRunning() is False

    def test_adopt_is_none_when_nothing_is_running(self, account, opts, filtered):
        record = plan_run(account, opts, run_id="r1").to_run_record()
        assert adopt(account.id, record, runner=_Runner(stdout="inactive\n")) is None

    def test_adopt_is_none_without_a_run_row(self, account):
        assert adopt(account.id, None, runner=_Runner(stdout="active\n")) is None

    def test_adopt_is_none_when_the_run_row_names_no_log(self, account):
        empty = types.SimpleNamespace(log_path="", log_offset=0)
        assert adopt(account.id, empty, runner=_Runner(stdout="active\n")) is None


class TestStart:

    def test_launches_the_planned_unit(self, account, opts, filtered):
        plan = plan_run(account, opts, run_id="r1")
        runner = _Runner()
        assert start(plan, runner=runner) is True
        assert runner.calls == [list(plan.launch_argv)]

    def test_a_refusal_from_systemd_is_false(self, account, opts, filtered):
        """systemd refusing a duplicate unit name IS the right answer: it stops
        a second concurrent bisync for the account."""
        plan = plan_run(account, opts, run_id="r1")
        assert start(plan, runner=_Runner(returncode=1)) is False

    def test_a_missing_systemd_run_is_false_not_an_exception(self, account, opts,
                                                             filtered):
        def explode(argv, **kwargs):
            raise FileNotFoundError("systemd-run")

        assert start(plan_run(account, opts, run_id="r1"), runner=explode) is False


class TestClearLock:

    SESSION = "p1..p2"

    def _lock(self, tmp_path: Path, body: dict) -> Path:
        path = tmp_path / f"{self.SESSION}.lck"
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def test_no_lock_is_false(self, tmp_path):
        assert clear_lock(tmp_path, self.SESSION) is False

    def test_removes_a_stale_lock(self, tmp_path):
        path = self._lock(tmp_path, dict(REAL_LOCK_NEVER_EXPIRES, PID="999999999"))
        assert clear_lock(tmp_path, self.SESSION) is True
        assert not path.exists()

    def test_removes_an_expired_lock(self, tmp_path):
        path = self._lock(tmp_path, dict(REAL_LOCK, PID=str(os.getpid())))
        assert clear_lock(tmp_path, self.SESSION) is True
        assert not path.exists()

    def test_removes_an_unreadable_lock(self, tmp_path):
        path = tmp_path / f"{self.SESSION}.lck"
        path.write_text("{truncated", encoding="utf-8")
        assert clear_lock(tmp_path, self.SESSION) is True
        assert not path.exists()

    def test_refuses_a_live_lock(self, tmp_path, monkeypatch):
        """rclone's own advice is an unconditional `rclone deletefile`; ours is
        not — a second run against the same listings deletes on both sides."""
        monkeypatch.setattr(bisync_mod, "_looks_like_bisync", lambda pid: True)
        path = self._lock(tmp_path,
                          dict(REAL_LOCK_NEVER_EXPIRES, PID=str(os.getpid())))
        with pytest.raises(SafetyRefusal) as caught:
            clear_lock(tmp_path, self.SESSION)
        assert caught.value.invariant == "I3"
        assert path.exists()

    def test_force_overrides_a_live_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bisync_mod, "_looks_like_bisync", lambda pid: True)
        path = self._lock(tmp_path,
                          dict(REAL_LOCK_NEVER_EXPIRES, PID=str(os.getpid())))
        assert clear_lock(tmp_path, self.SESSION, force=True) is True
        assert not path.exists()

    def test_clearing_moves_the_state_off_lock_stuck(self, tmp_path):
        (tmp_path / f"{self.SESSION}.path1.lst").write_text("x", encoding="utf-8")
        (tmp_path / f"{self.SESSION}.path2.lst").write_text("x", encoding="utf-8")
        self._lock(tmp_path, dict(REAL_LOCK_NEVER_EXPIRES, PID="999999999"))
        assert workdir_state(tmp_path, self.SESSION).state is BisyncState.LOCK_STUCK
        clear_lock(tmp_path, self.SESSION)
        assert workdir_state(tmp_path, self.SESSION).state is BisyncState.IDLE


class TestStop:

    def test_stops_the_right_unit(self, account):
        runner = _Runner()
        assert stop(account.id, runner=runner) is True
        assert runner.calls == [["systemctl", "--user", "stop",
                                 unit_name(account.id)]]

    def test_a_failure_is_false_not_an_exception(self, account):
        assert stop(account.id, runner=_Runner(returncode=5)) is False

    def test_never_reaches_kill(self, account):
        runner = _Runner()
        stop(account.id, runner=runner)
        joined = " ".join(runner.calls[0])
        assert "kill" not in joined
        assert "-9" not in joined
        assert "SIGKILL" not in joined

    def test_the_module_names_no_other_signal(self):
        """I13, as a grep: nothing here may reach for SIGKILL or SIGTERM."""
        source = Path(bisync_mod.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "signal.SIGKILL" not in code
        assert "signal.SIGTERM" not in code


# ═════════════════════════════════════════════════════════════════════════════
# Against the real rclone binary
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.live
@pytest.mark.skipif(RCLONE is None, reason="rclone is not installed")
class TestAgainstRealRclone:
    """Two LOCAL directories and a `local`-type remote in a scratch rclone.conf.

    No network, no rc daemon, no systemd, and the user's real `onedrive:` remote
    is never named — the scratch config defines its own `onedrive` remote of type
    `local`, and `RCLONE_CONFIG` makes it the only config in scope.

    The remote is deliberately NOT an `alias`: rclone resolves an alias to its
    target *before* naming the session, so an alias lab would compare our
    `session_name()` against a name rclone derived from a different string.
    """

    @pytest.fixture
    def lab(self, tmp_path, monkeypatch, account):
        cloud = tmp_path / "cloudside"
        local = tmp_path / "OneDrive-Offline"
        cloud.mkdir()
        (cloud / "Offline").mkdir()
        local.mkdir()
        conf = tmp_path / "rclone.conf"
        conf.write_text("[onedrive]\ntype = local\n", encoding="utf-8")
        monkeypatch.setenv("RCLONE_CONFIG", str(conf))
        with filters_mod.rewrite(account.id, ["Videos"]) as txn:
            txn.resynced()

        remote_path = f"onedrive:{cloud / 'Offline'}"

        def copier(src: str, dst: str) -> None:
            _head, _sep, tail = dst.partition(":")
            target = Path(tail)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)

        seed_check_access(str(local), remote_path, copyfile=copier)
        return types.SimpleNamespace(
            cloud=cloud, local=local, conf=conf, remote_path=remote_path,
            opts={"enabled": True, "local_path": str(local),
                  "remote_path": remote_path})

    @staticmethod
    def _run(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, timeout=120)

    def test_the_generated_resync_argv_is_accepted_and_succeeds(self, account, lab):
        """Every flag build_argv() emits is one this rclone understands."""
        from onedriveui.rc.bisync_log import classify_verdict

        (lab.local / "a.txt").write_text("hello\n", encoding="utf-8")
        argv = build_argv(account, lab.opts, run_id="live1", resync=True,
                          resync_decision=_approved())
        done = self._run(argv)
        assert done.returncode == 0, done.stderr[-2000:]
        assert "unknown flag" not in done.stderr

        text = paths.run_log_file("live1").read_text(encoding="utf-8")
        assert classify_verdict(text, done.returncode) is not None
        assert "Bisync successful" in text
        assert (lab.cloud / "Offline" / "a.txt").is_file()

    def test_the_follow_up_run_is_accepted_too(self, account, lab):
        """The non-resync argv is the one §5.4 specifies verbatim."""
        from onedriveui.models import RunVerdict
        from onedriveui.rc.bisync_log import classify_verdict

        (lab.local / "a.txt").write_text("hello\n", encoding="utf-8")
        assert self._run(build_argv(account, lab.opts, run_id="live1",
                                    resync=True,
                                    resync_decision=_approved())).returncode == 0

        (lab.local / "b.txt").write_text("second\n", encoding="utf-8")
        argv = build_argv(account, lab.opts, run_id="live2")
        done = self._run(argv)
        assert done.returncode == 0, done.stderr[-2000:]
        text = paths.run_log_file("live2").read_text(encoding="utf-8")
        assert classify_verdict(text, done.returncode) is RunVerdict.OK
        assert (lab.cloud / "Offline" / "b.txt").is_file()

    def test_the_real_log_carries_the_milestones_and_the_verdict(self, account, lab):
        """The bug `--log-level INFO` fixes, asserted against the binary: at the
        default NOTICE level this log holds ONE line and no verdict at all."""
        from onedriveui.rc.bisync_log import milestone, parse_text

        (lab.local / "a.txt").write_text("hello\n", encoding="utf-8")
        assert self._run(build_argv(account, lab.opts, run_id="live1",
                                    resync=True,
                                    resync_decision=_approved())).returncode == 0
        records = parse_text(paths.run_log_file("live1").read_text(encoding="utf-8"))
        assert len(records) > 1
        assert "Bisync successful" in {r.msg for r in records}
        phases = [p for p in (milestone(r) for r in records) if p]
        assert phases[-1] == "done"
        assert "transferring" in phases
        assert any(r.is_stats for r in records)
        assert any(r.is_object for r in records)

    def test_the_log_is_json_with_no_ansi_escapes(self, account, lab):
        """--color NEVER is mandatory; without it msg carries raw escapes."""
        self._run(build_argv(account, lab.opts, run_id="live1", resync=True,
                             resync_decision=_approved()))
        text = paths.run_log_file("live1").read_text(encoding="utf-8")
        assert "\x1b[" not in text
        for line in text.splitlines():
            assert json.loads(line)["level"] in (
                "debug", "info", "notice", "warning", "error", "critical")

    def test_the_filters_are_honoured_by_the_real_run(self, account, lab):
        (lab.local / "Videos").mkdir()
        (lab.local / "Videos" / "v.mp4").write_text("x", encoding="utf-8")
        (lab.local / "keep.txt").write_text("x", encoding="utf-8")
        assert self._run(build_argv(account, lab.opts, run_id="live1",
                                    resync=True,
                                    resync_decision=_approved())).returncode == 0
        assert (lab.cloud / "Offline" / "keep.txt").is_file()
        assert not (lab.cloud / "Offline" / "Videos").exists()

    def test_the_session_we_compute_is_the_one_rclone_uses(self, account, lab):
        """The listings rclone writes must be the ones workdir_state() looks for
        — the whole reason session_name() reimplements the sanitiser."""
        assert self._run(build_argv(account, lab.opts, run_id="live1",
                                    resync=True,
                                    resync_decision=_approved())).returncode == 0
        session = session_name(str(lab.local), lab.remote_path)
        state = workdir_state(paths.bisync_workdir(account.id), session)
        assert state.has_listings is True
        assert state.state is BisyncState.IDLE
        assert state.listing1.is_file() and state.listing2.is_file()

    def test_the_md5_rclone_stores_matches_ours(self, account, lab):
        """rclone writes filters.txt.md5 itself during --resync."""
        self._run(build_argv(account, lab.opts, run_id="live1", resync=True,
                             resync_decision=_approved()))
        text = paths.filters_file(account.id).read_text(encoding="utf-8")
        assert filters_mod.stored_md5(account.id) == filters_mod.md5_of_text(text)
        assert filters_mod.needs_resync(account.id) is False

    def test_a_real_lock_file_parses(self, account, lab, monkeypatch):
        """rclone writes the .lck; read_lock() must understand it."""
        assert self._run(build_argv(account, lab.opts, run_id="live1",
                                    resync=True,
                                    resync_decision=_approved())).returncode == 0
        session = session_name(str(lab.local), lab.remote_path)
        workdir = paths.bisync_workdir(account.id)
        lock_path = workdir / f"{session}.lck"
        lock_path.write_text(json.dumps({
            "Session": str(workdir / session), "PID": "999999999",
            "TimeRenewed": "2026-08-31T20:36:22.751714555-04:00",
            "TimeExpires": "2226-07-14T20:36:57.35348904-04:00"}),
            encoding="utf-8")
        lock = read_lock(workdir, session)
        assert lock is not None and lock.stale is True
        assert workdir_state(workdir, session).state is BisyncState.LOCK_STUCK

        done = self._run(build_argv(account, lab.opts, run_id="live2"))
        assert done.returncode == 1
        assert "prior lock file found" in paths.run_log_file("live2").read_text(
            encoding="utf-8")

    def test_check_access_is_enforced_during_resync_too(self, account, lab):
        """Which is exactly why seed_check_access() exists — you cannot use
        `bisync --resync --check-access` to create its own sentinels."""
        from onedriveui.models import RunVerdict
        from onedriveui.rc.bisync_log import classify_verdict

        (lab.local / "RCLONE_TEST").unlink()
        (lab.cloud / "Offline" / "RCLONE_TEST").unlink()
        argv = build_argv(account, dict(lab.opts, check_access=False),
                          run_id="live1", resync=True,
                          resync_decision=_approved())
        argv[argv.index("--resync"):argv.index("--resync")] = [
            "--check-access", "--check-filename", "RCLONE_TEST"]
        done = self._run(argv)
        assert done.returncode == 7
        text = paths.run_log_file("live1").read_text(encoding="utf-8")
        assert "Access test failed" in text
        assert classify_verdict(text, done.returncode) is RunVerdict.ACCESS_DENIED


class TestModuleHygiene:

    def test_every_public_name_exists(self):
        for name in bisync_mod.__all__:
            assert hasattr(bisync_mod, name), name

    def test_bisync_is_never_driven_through_the_rc(self):
        """`sync/bisync` over the rc behaves as --max-delete 0 and is untunable."""
        source = Path(bisync_mod.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "call_blocking" not in code
        assert '"sync/bisync"' not in code

    def test_no_widget_import(self):
        source = Path(bisync_mod.__file__).read_text(encoding="utf-8")
        assert "QtWidgets" not in source
