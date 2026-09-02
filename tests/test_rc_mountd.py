"""WP-02 — `onedriveui/rc/mountd.py`.

Two things dominate: the argv must be byte-for-byte the one ARCHITECTURE §5.3
specifies (a single changed backend flag orphans the whole VFS cache), and
liveness must be the *conjunction* of a `/proc` line and a working `statvfs`.

The stale-mount test deliberately uses `/` — a real mountpoint, so
`os.path.ismount()` genuinely returns `True` — with a synthetic
`/proc/self/mounts` calling it `fuse.rclone` and a `statvfs` that raises
`ENOTCONN`. That is exactly the state a `kill -9` on an rclone mount leaves
behind, and it proves `is_live()` and `os.path.ismount()` disagree about it.
"""

from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest

from onedriveui import APP_DISPLAY_NAME, USER_AGENT, paths
from onedriveui.constants import (
    MANDATORY_EXCLUDES,
    MAX_CHECKERS,
    MAX_TRANSFERS,
    MOUNT_RESTART_LADDER_S,
    MOUNT_RESTART_MAX_PER_HOUR,
    REMOTE_TRASH_DIR,
    REMOTE_VERSIONS_DIR,
    UNIT_MOUNT_TMPL,
)
from onedriveui.errors import ConfigError, SafetyRefusal
from onedriveui.models import AccountInfo, MountHealth, RcEndpoint
from onedriveui.rc import FUSERMOUNT3
from onedriveui.rc import endpoints as _endpoints
from onedriveui.rc import guards
from onedriveui.rc import mountd as mountd_mod
from onedriveui.rc.mountd import (
    DEFAULT_MOUNT_OPTIONS,
    MOUNT_EXCLUDE_DIRS,
    MountController,
    fusermount_unmount,
    is_live,
    rclone_mounts,
    systemctl_status_text,
)
from tests.conftest import REAL_HOME

CREDS = ("onedriveui", "s3cret")
PORT = 17801

_FUSE_LINE = ("onedrive: {mountpoint} fuse.rclone "
              "rw,nosuid,nodev,relatime,user_id=1000,group_id=1000 0 0\n")
_OTHER_LINES = "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"


class _Systemd:
    """Records what the controller asked the service manager to do."""

    def __init__(self) -> None:
        self.units: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.status = ""

    def write_unit(self, name, text):
        self.calls.append(("write_unit", name))
        self.units[name] = text
        return name

    def daemon_reload(self):
        self.calls.append(("daemon_reload", ""))

    def enable(self, name):
        self.calls.append(("enable", name))

    def start(self, name):
        self.calls.append(("start", name))

    def stop(self, name):
        self.calls.append(("stop", name))

    def restart(self, name):
        self.calls.append(("restart", name))

    def is_active(self, name) -> bool:
        return ("start", name) in self.calls

    def status_text(self, name) -> str:
        return self.status

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _name in self.calls]


@pytest.fixture
def account(tmp_path) -> AccountInfo:
    root = tmp_path / "OneDrive"
    return AccountInfo(id="onedrive", remote="onedrive", sync_root=str(root))


@pytest.fixture
def systemd() -> _Systemd:
    return _Systemd()


@pytest.fixture
def controller(systemd, monkeypatch) -> MountController:
    """A controller whose restart ladder fires inline instead of on a QTimer."""
    fired: list[int] = []

    def schedule(ms, fn):
        fired.append(ms)
        fn()

    ctl = MountController(systemd, schedule=schedule)
    ctl.scheduled_ms = fired          # type: ignore[attr-defined]
    monkeypatch.setattr(mountd_mod, "fusermount_unmount",
                        lambda *a, **kw: True)
    return ctl


@pytest.fixture
def fake_mounts(tmp_path, monkeypatch):
    """Publish a synthetic `/proc/self/mounts` and return a setter."""
    proc = tmp_path / "mounts"
    proc.write_text(_OTHER_LINES, encoding="utf-8")
    monkeypatch.setattr(paths, "_PROC_MOUNTS", proc)

    def declare(*mountpoints) -> None:
        proc.write_text(
            _OTHER_LINES + "".join(_FUSE_LINE.format(mountpoint=str(m))
                                   for m in mountpoints), encoding="utf-8")

    return declare


# ═════════════════════════════════════════════════════════════════════════════
# build_argv — ARCHITECTURE §5.3, exactly
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildArgv:
    @pytest.fixture
    def argv(self, controller, account):
        return controller.build_argv(account, PORT, CREDS)

    def test_the_whole_argv_is_the_specified_one(self, controller, account):
        """Every token of §5.3, in order. This is the assertion that would catch
        a well-meaning edit adding a backend flag or dropping a cap."""
        mountpoint = str(paths.mount_point(account.sync_root))
        assert controller.build_argv(account, PORT, CREDS) == [
            "/usr/bin/rclone", "mount", "onedrive:", mountpoint,
            "--vfs-cache-mode", "full",
            "--cache-dir", os.path.expanduser("~/.cache/rclone"),
            "--vfs-cache-max-size", "50G",
            "--vfs-cache-max-age", "720h",
            "--vfs-cache-min-free-space", "5G",
            "--vfs-cache-poll-interval", "1m",
            "--vfs-write-back", "5s",
            "--vfs-fast-fingerprint",
            "--dir-cache-time", "1h",
            "--poll-interval", "60s",
            "--attr-timeout", "1s",
            "--vfs-read-chunk-size", "32M",
            "--vfs-read-chunk-size-limit", "512M",
            "--transfers", "4", "--checkers", "8",
            "--tpslimit", "8", "--tpslimit-burst", "10",
            "--retries", "3", "--low-level-retries", "10",
            "--file-perms", "0644", "--dir-perms", "0755", "--umask", "022",
            "--devname", "OneDrive",
            "--exclude", "/.Trash-1000/**",
            "--exclude", "/.onedriveui-trash/**",
            "--exclude", "/.onedriveui-versions/**",
            "--rc", "--rc-addr", f"127.0.0.1:{PORT}",
            "--rc-user", "onedriveui", "--rc-pass", "s3cret",
            "--user-agent", USER_AGENT,
            "--use-json-log", "--color", "NEVER", "--log-level", "INFO",
        ]

    def test_the_users_unticked_folders_reach_the_argv(self, systemd, account):
        """"Choose folders" is only real if its rules are on the command line.

        They were not. `SelectiveSync.as_mount_excludes()` had no caller
        anywhere in the product and `build_argv()` took no rules, so unticking a
        folder evicted its cache and recorded the exclusion while the mount went
        on serving it. Nothing failed, which is why nobody noticed.

        `--filter`, not `--exclude`: the rules carry the leading `- ` that
        rclone reads only under `--filter`.
        """
        ctl = MountController(
            systemd,
            excludes=lambda _a: ["- /Photos/", "- /Work/Archive/"])
        argv = ctl.build_argv(account, PORT, CREDS)
        pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1)]
        assert ("--filter", "- /Photos/") in pairs
        assert ("--filter", "- /Work/Archive/") in pairs
        # The mandatory excludes are still there and still `--exclude`.
        assert ("--exclude", "/.onedriveui-trash/**") in pairs
        # And a controller with no selection adds no filters at all.
        assert "--filter" not in MountController(systemd).build_argv(
            account, PORT, CREDS)

    def test_the_selection_provider_can_be_attached_after_construction(
            self, systemd, account):
        """`build_engine()` builds the controller before the selection service,
        so the provider arrives late. It must still reach the argv."""
        ctl = MountController(systemd)
        assert "--filter" not in ctl.build_argv(account, PORT, CREDS)
        ctl.set_excludes_provider(lambda _a: ["- /Photos/"])
        assert ("--filter" in ctl.build_argv(account, PORT, CREDS))

    def test_it_passes_its_own_backend_flag_guard(self, argv):
        """Acceptance: assert_no_backend_flags(build_argv(...)) passes."""
        guards.assert_no_backend_flags(argv)
        guards.assert_no_inplace(argv)

    def test_injecting_a_backend_flag_raises_safety_refusal_i1(self, systemd,
                                                               account):
        """Acceptance: `--onedrive-chunk-size 30M` raises SafetyRefusal with
        invariant "I1"."""
        options = dict(DEFAULT_MOUNT_OPTIONS,
                       extra_args=["--onedrive-chunk-size", "30M"])
        ctl = MountController(systemd, options=lambda _a: options)
        with pytest.raises(SafetyRefusal) as excinfo:
            ctl.build_argv(account, PORT, CREDS)
        assert excinfo.value.invariant == "I1"
        assert "--onedrive-chunk-size" in str(excinfo.value)

    def test_injecting_inplace_raises_safety_refusal_i12(self, systemd, account):
        options = dict(DEFAULT_MOUNT_OPTIONS, extra_args=["--inplace"])
        ctl = MountController(systemd, options=lambda _a: options)
        with pytest.raises(SafetyRefusal) as excinfo:
            ctl.build_argv(account, PORT, CREDS)
        assert excinfo.value.invariant == "I12"

    def test_a_harmless_extra_arg_is_appended(self, systemd, account):
        options = dict(DEFAULT_MOUNT_OPTIONS, extra_args=["--no-checksum"])
        ctl = MountController(systemd, options=lambda _a: options)
        assert ctl.build_argv(account, PORT, CREDS)[-1] == "--no-checksum"

    @pytest.mark.parametrize("flag,why", [
        ("--daemon", "broken with --rc --rc-addr in v1.75.0: the parent binds "
                     "the port before forking"),
        ("--allow-other", "needs a root edit of /etc/fuse.conf"),
        ("--vfs-read-chunk-streams", "parallel streams cause Graph 429s"),
        ("--inplace", "an interrupted in-place transfer corrupts the destination"),
        ("--fast-list", "ListR is false on OneDrive; it is a no-op"),
        ("--onedrive-chunk-size", "invariant I1"),
    ])
    def test_the_deliberate_omissions_stay_omitted(self, argv, flag, why):
        assert flag not in argv, why

    def test_the_caps_are_enforced_even_against_a_bad_config(self, systemd, account):
        """OneDrive Personal 429s above 4 transfers; config validation should
        catch it first, but the argv builder is the last line."""
        options = dict(DEFAULT_MOUNT_OPTIONS, transfers=16, checkers=64)
        ctl = MountController(systemd, options=lambda _a: options)
        argv = ctl.build_argv(account, PORT, CREDS)
        assert argv[argv.index("--transfers") + 1] == str(MAX_TRANSFERS) == "4"
        assert argv[argv.index("--checkers") + 1] == str(MAX_CHECKERS) == "8"

    def test_poll_interval_must_stay_below_dir_cache_time(self, systemd, account):
        """Otherwise the directory cache outlives every poll and remote changes
        never appear."""
        options = dict(DEFAULT_MOUNT_OPTIONS, poll_interval_s=3600,
                       dir_cache_time_s=3600)
        ctl = MountController(systemd, options=lambda _a: options)
        with pytest.raises(ConfigError, match="strictly below"):
            ctl.build_argv(account, PORT, CREDS)

    def test_the_devname_is_the_display_name_not_a_new_literal(self, argv):
        assert argv[argv.index("--devname") + 1] == APP_DISPLAY_NAME == "OneDrive"

    def test_the_excludes_are_derived_from_the_frozen_contract(self):
        """So the mount argv and the bisync filters file can never drift."""
        assert set(MOUNT_EXCLUDE_DIRS) == {
            ".Trash-1000", REMOTE_TRASH_DIR, REMOTE_VERSIONS_DIR}
        for directory in MOUNT_EXCLUDE_DIRS:
            assert f"- {directory}/" in MANDATORY_EXCLUDES

    def test_a_custom_cache_dir_is_expanded(self, systemd, account, tmp_path):
        options = dict(DEFAULT_MOUNT_OPTIONS, cache_dir="~/elsewhere")
        ctl = MountController(systemd, options=lambda _a: options)
        argv = ctl.build_argv(account, PORT, CREDS)
        value = argv[argv.index("--cache-dir") + 1]
        assert value == os.path.expanduser("~/elsewhere")
        assert "~" not in value

    def test_a_partial_option_block_is_filled_from_the_defaults(self, systemd,
                                                                account):
        ctl = MountController(systemd, options=lambda _a: {"transfers": 2})
        argv = ctl.build_argv(account, PORT, CREDS)
        assert argv[argv.index("--transfers") + 1] == "2"
        assert argv[argv.index("--checkers") + 1] == "8"

    def test_fast_fingerprint_can_be_turned_off(self, systemd, account):
        options = dict(DEFAULT_MOUNT_OPTIONS, fast_fingerprint=False)
        ctl = MountController(systemd, options=lambda _a: options)
        assert "--vfs-fast-fingerprint" not in ctl.build_argv(account, PORT, CREDS)

    def test_the_fs_is_the_bare_remote_with_no_hash_suffix(self, argv, account):
        assert argv[2] == account.fs == "onedrive:"
        assert "{" not in argv[2]


# ═════════════════════════════════════════════════════════════════════════════
# unit_text
# ═════════════════════════════════════════════════════════════════════════════

class TestUnitText:
    @pytest.fixture
    def text(self, controller, account):
        return controller.unit_text(account, PORT, CREDS)

    def test_it_is_type_notify(self, text):
        """`Type=simple` would report `active` before the mount exists and every
        consumer would race it."""
        assert "Type=notify" in text

    def test_exec_stop_is_a_lazy_fusermount3(self, text, account):
        mountpoint = str(paths.mount_point(account.sync_root))
        assert f"ExecStop={FUSERMOUNT3} -uz {mountpoint}" in text

    def test_exec_start_pre_clears_a_previous_crash_s_corpse(self, text):
        line = next(l for l in text.splitlines() if l.startswith("ExecStartPre="))
        assert line.startswith("ExecStartPre=-")     # `-` so a no-op cannot abort
        assert "fusermount3 -uz" in line

    def test_kill_mode_is_mixed_with_a_two_minute_stop_timeout(self, text):
        assert "KillMode=mixed" in text
        assert "TimeoutStopSec=120" in text

    def test_it_restarts_on_failure_after_ten_seconds(self, text):
        assert "Restart=on-failure" in text
        assert "RestartSec=10" in text

    def test_network_online_target_is_absent(self, text):
        assert "network-online.target" not in text

    def test_the_exec_start_is_the_argv(self, text, controller, account):
        argv = controller.build_argv(account, PORT, CREDS)
        exec_start = next(l for l in text.splitlines() if l.startswith("ExecStart="))
        assert exec_start == "ExecStart=" + " ".join(argv)

    def test_a_percent_in_the_mountpoint_is_escaped(self, systemd, tmp_path):
        odd = AccountInfo(id="odd", remote="onedrive",
                          sync_root=str(tmp_path / "One%Drive"))
        text = MountController(systemd).unit_text(odd, PORT, CREDS)
        assert "One%%Drive" in text
        assert "One%D" not in text.replace("One%%D", "")

    def test_the_unit_is_a_concrete_instance_name(self, account):
        assert (MountController.unit_name(account)
                == UNIT_MOUNT_TMPL.format("onedrive")
                == "onedriveui-mount@onedrive.service")


# ═════════════════════════════════════════════════════════════════════════════
# I6 — liveness
# ═════════════════════════════════════════════════════════════════════════════

class TestIsLive:
    def test_an_unmounted_path_is_down(self, fake_mounts, tmp_path):
        assert is_live(tmp_path / "OneDrive") is MountHealth.DOWN

    def test_a_mounted_path_with_a_working_statvfs_is_up(self, fake_mounts,
                                                         tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)
        assert is_live(mount) is MountHealth.UP

    def test_a_proc_line_with_enotconn_is_stale_while_ismount_still_says_true(
            self, fake_mounts, monkeypatch):
        """The post-`kill -9` state, reproduced: the /proc entry survives,
        `os.path.ismount()` keeps returning True, and every access is ENOTCONN.
        `/` is used because it is a genuine mountpoint, so `ismount` is real."""
        fake_mounts("/")
        real_statvfs = os.statvfs

        def statvfs(path):
            if os.path.realpath(str(path)) == "/":
                raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")
            return real_statvfs(path)

        monkeypatch.setattr(os, "statvfs", statvfs)
        assert os.path.ismount("/") is True
        assert is_live("/") is MountHealth.STALE

    @pytest.mark.parametrize("code", [errno.ENOTCONN, errno.ENODEV, errno.EIO,
                                      errno.ESTALE])
    def test_every_dead_fuse_errno_is_stale(self, fake_mounts, tmp_path,
                                            monkeypatch, code):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)

        def statvfs(path):
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(os, "statvfs", statvfs)
        assert is_live(mount) is MountHealth.STALE

    def test_a_vanished_mountpoint_is_down(self, fake_mounts, tmp_path,
                                           monkeypatch):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)

        def statvfs(path):
            raise OSError(errno.ENOENT, "No such file or directory")

        monkeypatch.setattr(os, "statvfs", statvfs)
        assert is_live(mount) is MountHealth.DOWN

    def test_a_non_rclone_fuse_mount_is_not_ours(self, tmp_path, monkeypatch):
        proc = tmp_path / "mounts"
        proc.write_text(f"gvfsd-fuse {tmp_path / 'gvfs'} fuse.gvfsd-fuse rw 0 0\n",
                        encoding="utf-8")
        monkeypatch.setattr(paths, "_PROC_MOUNTS", proc)
        assert is_live(tmp_path / "gvfs") is MountHealth.DOWN

    def test_rclone_mounts_reads_the_device_name_verbatim(self, tmp_path,
                                                          monkeypatch):
        """The `{HASH}` suffix must survive: strip it before display, never
        before comparing."""
        proc = tmp_path / "mounts"
        proc.write_text("onedrive{MxOuf}: /home/u/OneDrive fuse.rclone rw 0 0\n",
                        encoding="utf-8")
        monkeypatch.setattr(paths, "_PROC_MOUNTS", proc)
        assert rclone_mounts() == [("onedrive{MxOuf}:", Path("/home/u/OneDrive"))]

    @pytest.mark.live
    def test_the_real_onedrive_mount_on_this_machine_is_up(self):
        """This machine runs `rclone mount onedrive: ~/OneDrive`; I6 must agree
        with `os.path.ismount()` while the mount is healthy."""
        target = REAL_HOME / "OneDrive"
        if not any(target == mount for _fs, mount in paths.fuse_rclone_mounts()):
            pytest.skip("no fuse.rclone mount at ~/OneDrive on this machine")
        assert os.path.ismount(target) is True
        assert is_live(target) is MountHealth.UP

    @pytest.mark.live
    def test_the_real_mount_carries_no_hash_suffixed_device_name(self):
        """Invariant I1, measured on the live mount.

        This test used to assert the opposite, and was right to: the mount at
        `~/OneDrive` was started by hand with `--onedrive-chunk-size 30M` on the
        command line, which renames the filesystem to `onedrive{MxOuf}:` and
        puts its VFS cache in a directory `onedrive:` will never look in — two
        abandoned cache trees were the evidence. OneDriveUI now owns that
        mountpoint and passes backend options through the rclone config, where
        they do not become part of the fs name. A `{` here means the defect is
        back."""
        names = [fs for fs, mount in rclone_mounts()
                 if mount == REAL_HOME / "OneDrive"]
        if not names:
            pytest.skip("no fuse.rclone mount at ~/OneDrive on this machine")
        assert "{" not in names[0], f"backend flags leaked: {names[0]}"


# ═════════════════════════════════════════════════════════════════════════════
# fusermount3 and StatusText
# ═════════════════════════════════════════════════════════════════════════════

class TestFusermount:
    def test_lazy_unmount_uses_uz(self, monkeypatch, tmp_path):
        seen: list[list[str]] = []

        def run(argv, **kw):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(subprocess, "run", run)
        assert fusermount_unmount(tmp_path / "OneDrive", lazy=True) is True
        assert seen[0][:2] == [FUSERMOUNT3, "-uz"]

    def test_non_lazy_uses_u(self, monkeypatch, tmp_path):
        seen: list[list[str]] = []
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (
            seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")))
        fusermount_unmount(tmp_path / "OneDrive", lazy=False)
        assert seen[0][1] == "-u"

    def test_not_mounted_counts_as_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                            subprocess.CompletedProcess(
                                argv, 1, "", "fusermount3: entry for /x not found "
                                              "in /etc/mtab"))
        assert fusermount_unmount(tmp_path / "OneDrive") is True

    def test_a_real_failure_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                            subprocess.CompletedProcess(argv, 1, "",
                                                        "fusermount3: failed"))
        assert fusermount_unmount(tmp_path / "OneDrive") is False

    def test_a_missing_helper_is_reported_not_raised(self, monkeypatch, tmp_path):
        def run(argv, **kw):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(subprocess, "run", run)
        assert fusermount_unmount(tmp_path / "OneDrive") is False


class TestStatusText:
    def test_it_parses_systemctl_show(self, monkeypatch):
        line = ("[23:29] vfs cache: objects 3 (was 3) in use 0, to upload 0, "
                "uploading 0, total size 2.525Mi")
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                            subprocess.CompletedProcess(argv, 0, line + "\n", ""))
        assert systemctl_status_text("onedriveui-mount@onedrive.service") == line

    def test_it_tolerates_a_key_prefixed_answer(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                            subprocess.CompletedProcess(argv, 0,
                                                        "StatusText=hello\n", ""))
        assert systemctl_status_text("u") == "hello"

    def test_an_unknown_unit_yields_the_empty_string(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                            subprocess.CompletedProcess(argv, 1, "", "no such unit"))
        assert systemctl_status_text("nope.service") == ""

    def test_a_missing_systemctl_yields_the_empty_string(self, monkeypatch):
        def run(argv, **kw):
            raise FileNotFoundError("systemctl")

        monkeypatch.setattr(subprocess, "run", run)
        assert systemctl_status_text("u") == ""

    def test_the_controller_prefers_the_injected_service_manager(self, controller,
                                                                 systemd, account,
                                                                 monkeypatch):
        systemd.status = "from the bus"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                            pytest.fail("systemctl must not be shelled out to "
                                        "when systemd.status_text answers"))
        assert controller.status_text(account) == "from the bus"

    def test_it_falls_back_to_systemctl_when_the_bus_call_fails(self, systemd,
                                                                account,
                                                                monkeypatch):
        def boom(name):
            raise RuntimeError("no session bus")

        systemd.status_text = boom
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                            subprocess.CompletedProcess(argv, 0, "fallback\n", ""))
        assert MountController(systemd).status_text(account) == "fallback"


# ═════════════════════════════════════════════════════════════════════════════
# ensure_mounted / unmount
# ═════════════════════════════════════════════════════════════════════════════

class TestEnsureMounted:
    def test_it_writes_enables_and_starts_the_unit(self, controller, systemd,
                                                   account, fake_mounts, qapp):
        controller.ensure_mounted(account)
        assert systemd.verbs == ["write_unit", "daemon_reload", "enable", "start"]
        unit = MountController.unit_name(account)
        assert "Type=notify" in systemd.units[unit]

    def test_it_records_the_mount_s_own_rc_endpoint(self, controller, account,
                                                    fake_mounts, qapp):
        controller.ensure_mounted(account)
        ep = controller.endpoint(account)
        assert ep is not None and ep.kind == "mount"
        assert ep.account_id == "onedrive"
        assert ep.mountpoint == str(paths.mount_point(account.sync_root))
        assert _endpoints.load_endpoints()["mount:onedrive"].port == ep.port
        assert f"--rc-addr 127.0.0.1:{ep.port}" in (
            controller._systemd.units[MountController.unit_name(account)])

    def test_a_live_mount_is_left_alone(self, controller, systemd, account,
                                        fake_mounts, qapp):
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        controller.ensure_mounted(account)
        assert systemd.calls == []

    def test_a_stale_mount_is_cleared_before_starting(self, controller, systemd,
                                                      account, fake_mounts,
                                                      monkeypatch, qapp):
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        cleared: list = []
        monkeypatch.setattr(os, "statvfs", lambda p: (_ for _ in ()).throw(
            OSError(errno.ENOTCONN, "Transport endpoint is not connected")))
        monkeypatch.setattr(mountd_mod, "fusermount_unmount",
                            lambda p, **kw: cleared.append(str(p)) or True)
        controller.ensure_mounted(account)
        assert cleared == [str(mountpoint)]
        assert "start" in systemd.verbs

    def test_the_mountpoint_is_created_if_absent(self, controller, account,
                                                 fake_mounts, qapp):
        mountpoint = paths.mount_point(account.sync_root)
        assert not mountpoint.exists()
        controller.ensure_mounted(account)
        assert mountpoint.is_dir()

    def test_a_file_where_the_mountpoint_should_be_is_refused(self, controller,
                                                              account,
                                                              fake_mounts, qapp):
        from onedriveui.errors import MountLost

        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.parent.mkdir(parents=True, exist_ok=True)
        mountpoint.write_text("not a directory", encoding="utf-8")
        with pytest.raises(MountLost):
            controller.ensure_mounted(account)

    def test_health_transitions_reach_the_bus(self, controller, account,
                                              fake_mounts, bus_spy, qapp):
        bus_spy.watch("mount_health")
        controller.ensure_mounted(account)
        seen = bus_spy.of("mount_health")
        assert ("onedrive", MountHealth.DOWN) in seen
        assert ("onedrive", MountHealth.STARTING) in seen


class TestUnmount:
    @staticmethod
    def _idle(monkeypatch):
        """No upload in flight, so the I3 guard on unmount() stands aside."""
        monkeypatch.setattr(MountController, "uploads_in_progress",
                            lambda self, account: 0)

    def test_it_stops_the_unit_and_detaches(self, systemd, account, fake_mounts,
                                            monkeypatch, qapp):
        seen: list[tuple[str, bool]] = []
        monkeypatch.setattr(mountd_mod, "fusermount_unmount",
                            lambda p, lazy=True: seen.append((str(p), lazy)) or True)
        self._idle(monkeypatch)
        ctl = MountController(systemd, schedule=lambda ms, fn: fn())
        ctl.ensure_mounted(account)
        ctl.unmount(account)
        assert ("stop", MountController.unit_name(account)) in systemd.calls
        assert seen == [(str(paths.mount_point(account.sync_root)), True)]
        assert ctl.endpoint(account) is None
        assert _endpoints.load_endpoints() == {}

    def test_it_never_calls_the_banned_rc_endpoints(self, systemd, account,
                                                    fake_mounts, monkeypatch, qapp):
        """I7: mount/unmount cannot see a CLI-started mount and would not destroy
        the VFS anyway."""
        monkeypatch.setattr(mountd_mod, "fusermount_unmount", lambda *a, **kw: True)
        self._idle(monkeypatch)
        calls: list[str] = []
        monkeypatch.setattr(mountd_mod, "call_blocking",
                            lambda ep, path, params=None, timeout_s=30.0:
                            calls.append(path) or {})
        ctl = MountController(systemd, schedule=lambda ms, fn: fn())
        ctl.ensure_mounted(account)
        ctl.unmount(account)
        assert not any(path.startswith("mount/") for path in calls)

    # ── invariant I3 on the force-unmount path ──────────────────────────────

    def test_it_refuses_while_an_upload_is_in_flight(self, systemd, account,
                                                     fake_mounts, monkeypatch,
                                                     qapp):
        """I3: -uz destroys the VFS, and a file mid-upload lives only there."""
        detached: list[object] = []
        monkeypatch.setattr(mountd_mod, "fusermount_unmount",
                            lambda *a, **kw: detached.append(a) or True)
        monkeypatch.setattr(MountController, "uploads_in_progress",
                            lambda self, account: 3)
        ctl = MountController(systemd, schedule=lambda ms, fn: fn())
        ctl.ensure_mounted(account)
        systemd.calls.clear()
        with pytest.raises(SafetyRefusal) as excinfo:
            ctl.unmount(account)
        assert excinfo.value.invariant == "I3"
        assert detached == []
        assert not any(call[0] == "stop" for call in systemd.calls)
        # the endpoint is NOT forgotten: the mount is still live and drivable
        assert ctl.endpoint(account) is not None

    def test_an_unreadable_upload_count_is_not_evidence_of_safety(
            self, systemd, account, fake_mounts, monkeypatch, qapp):
        """-1 means vfs/stats could not be asked, which is not zero."""
        monkeypatch.setattr(mountd_mod, "fusermount_unmount", lambda *a, **kw: True)
        monkeypatch.setattr(MountController, "uploads_in_progress",
                            lambda self, account: -1)
        ctl = MountController(systemd, schedule=lambda ms, fn: fn())
        ctl.ensure_mounted(account)
        with pytest.raises(SafetyRefusal) as excinfo:
            ctl.unmount(account)
        assert excinfo.value.invariant == "I3"

    def test_force_is_the_explicit_user_override(self, systemd, account,
                                                 fake_mounts, monkeypatch, qapp):
        detached: list[object] = []
        monkeypatch.setattr(mountd_mod, "fusermount_unmount",
                            lambda p, lazy=True: detached.append(str(p)) or True)
        monkeypatch.setattr(MountController, "uploads_in_progress",
                            lambda self, account: 3)
        ctl = MountController(systemd, schedule=lambda ms, fn: fn())
        ctl.ensure_mounted(account)
        ctl.unmount(account, force=True)
        assert detached == [str(paths.mount_point(account.sync_root))]

    def test_a_stale_mount_is_always_detachable(self, systemd, account,
                                                fake_mounts, monkeypatch, qapp):
        """An ENOTCONN corpse serves nobody; only detaching it recovers."""
        detached: list[object] = []
        monkeypatch.setattr(mountd_mod, "fusermount_unmount",
                            lambda p, lazy=True: detached.append(str(p)) or True)
        monkeypatch.setattr(MountController, "uploads_in_progress",
                            lambda self, account: -1)
        monkeypatch.setattr(MountController, "health",
                            lambda self, account: MountHealth.STALE)
        ctl = MountController(systemd, schedule=lambda ms, fn: fn())
        ctl.unmount(account)
        assert detached == [str(paths.mount_point(account.sync_root))]


# ═════════════════════════════════════════════════════════════════════════════
# The upload-aware restart ladder — invariant I3
# ═════════════════════════════════════════════════════════════════════════════

class TestRestart:
    @staticmethod
    def _with_uploads(controller, monkeypatch, count):
        monkeypatch.setattr(MountController, "uploads_in_progress",
                            lambda self, account: count)

    def test_it_refuses_while_an_upload_is_in_flight(self, controller, systemd,
                                                     account, fake_mounts,
                                                     monkeypatch, qapp):
        """Invariant I3: a file being uploaded exists in the VFS cache and
        nowhere else."""
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        self._with_uploads(controller, monkeypatch, 2)
        controller.restart(account, "mount looks wedged")
        assert systemd.calls == []
        assert controller.restarts_this_hour(account) == 0

    def test_it_refuses_when_the_upload_count_cannot_be_read(self, controller,
                                                             systemd, account,
                                                             fake_mounts,
                                                             monkeypatch, qapp):
        """"We could not ask" is not "there are none"."""
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        self._with_uploads(controller, monkeypatch, -1)
        controller.restart(account, "probe")
        assert systemd.calls == []

    def test_it_proceeds_when_nothing_is_uploading(self, controller, systemd,
                                                   account, fake_mounts,
                                                   monkeypatch, qapp):
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        self._with_uploads(controller, monkeypatch, 0)
        controller.restart(account, "poll interval changed")
        assert ("restart", MountController.unit_name(account)) in systemd.calls

    def test_a_stale_mount_is_restarted_even_with_uploads_pending(
            self, controller, systemd, account, fake_mounts, monkeypatch, qapp):
        """An ENOTCONN corpse serves nobody; its uploads are already lost, and
        only a restart brings the account back."""
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        monkeypatch.setattr(os, "statvfs", lambda p: (_ for _ in ()).throw(
            OSError(errno.ENOTCONN, "Transport endpoint is not connected")))
        self._with_uploads(controller, monkeypatch, 5)
        controller.restart(account, "mount is stale")
        assert ("restart", MountController.unit_name(account)) in systemd.calls

    def test_the_ladder_is_ten_thirty_then_two_minutes(self, controller, systemd,
                                                       account, fake_mounts,
                                                       monkeypatch, qapp):
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        self._with_uploads(controller, monkeypatch, 0)
        for _ in range(MOUNT_RESTART_MAX_PER_HOUR):
            controller.restart(account, "flapping")
        assert controller.scheduled_ms == [
            MOUNT_RESTART_LADDER_S[0] * 1000,
            MOUNT_RESTART_LADDER_S[1] * 1000,
            MOUNT_RESTART_LADDER_S[2] * 1000,
        ] == [10_000, 30_000, 120_000]

    def test_it_stops_after_three_restarts_in_an_hour(self, controller, systemd,
                                                      account, fake_mounts,
                                                      monkeypatch, qapp):
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)
        self._with_uploads(controller, monkeypatch, 0)
        for _ in range(MOUNT_RESTART_MAX_PER_HOUR):
            controller.restart(account, "flapping")
        systemd.calls.clear()
        controller.restart(account, "flapping")
        assert systemd.calls == []
        assert controller.restarts_this_hour(account) == MOUNT_RESTART_MAX_PER_HOUR

    def test_a_systemd_failure_does_not_propagate(self, systemd, account,
                                                  fake_mounts, monkeypatch, qapp):
        mountpoint = paths.mount_point(account.sync_root)
        mountpoint.mkdir(parents=True)
        fake_mounts(mountpoint)

        def boom(name):
            raise RuntimeError("no session bus")

        systemd.restart = boom
        ctl = MountController(systemd, schedule=lambda ms, fn: fn())
        monkeypatch.setattr(MountController, "uploads_in_progress",
                            lambda self, account: 0)
        ctl.restart(account, "probe")           # logs, does not raise


class TestUploadsInProgress:
    def test_it_reads_disk_cache_uploads_in_progress(self, systemd, account,
                                                     monkeypatch, qapp):
        ep = RcEndpoint(kind="mount", host="127.0.0.1", port=17801,
                        account_id="onedrive")
        _endpoints.save_endpoint(ep)
        monkeypatch.setattr(
            mountd_mod, "call_blocking",
            lambda e, path, params=None, timeout_s=30.0: {
                "fs": "onedrive:", "diskCache": {"uploadsInProgress": 3,
                                                 "uploadsQueued": 7}})
        assert MountController(systemd).uploads_in_progress(account) == 3

    def test_no_endpoint_means_unknown(self, systemd, account, qapp):
        assert MountController(systemd).uploads_in_progress(account) == -1

    def test_an_unreachable_vfs_means_unknown(self, systemd, account, monkeypatch,
                                              qapp):
        from onedriveui.errors import DaemonUnavailable

        _endpoints.save_endpoint(RcEndpoint(kind="mount", host="127.0.0.1",
                                            port=17801, account_id="onedrive"))

        def boom(*a, **kw):
            raise DaemonUnavailable("vfs/stats", 503, {})

        monkeypatch.setattr(mountd_mod, "call_blocking", boom)
        assert MountController(systemd).uploads_in_progress(account) == -1

    def test_a_cache_off_vfs_means_unknown(self, systemd, account, monkeypatch,
                                           qapp):
        """`diskCache` is present only when --vfs-cache-mode > off."""
        _endpoints.save_endpoint(RcEndpoint(kind="mount", host="127.0.0.1",
                                            port=17801, account_id="onedrive"))
        monkeypatch.setattr(mountd_mod, "call_blocking",
                            lambda *a, **kw: {"fs": "onedrive:", "inUse": 1})
        assert MountController(systemd).uploads_in_progress(account) == -1
