"""WP-02 — `onedriveui/rc/guards.py`.

Every one of the refusals, plus the two negative directions that matter more than
the positive ones: a guard that never fires is useless, and a guard that fires on
the application's own argv is worse than useless.

The fuse-mount tests drive `paths._PROC_MOUNTS` at a synthetic file rather than
the real `/proc/self/mounts`, so the *logic* is proved deterministically; the
`live`-marked tests then confirm the same answers against this machine's actual
`~/OneDrive` mount.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from onedriveui import paths
from onedriveui.constants import MANDATORY_EXCLUDES
from onedriveui.errors import SafetyRefusal
from onedriveui.models import AccountInfo
from onedriveui.rc import guards
from tests.conftest import REAL_HOME
from tests.fakes.fake_rc import BANNED_PATHS

# A verbatim /proc/self/mounts line for a live rclone mount, including the
# {HASH} device name a backend flag produces and the octal escaping the kernel
# applies to a space in a path.
_FUSE_LINE = ("onedrive{{MxOuf}}: {mountpoint} fuse.rclone "
              "rw,nosuid,nodev,relatime,user_id=1000,group_id=1000 0 0\n")
_OTHER_LINES = (
    "proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n"
    "/dev/nvme0n1p2 / ext4 rw,relatime 0 0\n"
)


@pytest.fixture
def fake_mounts(tmp_path, monkeypatch):
    """Publish a synthetic `/proc/self/mounts` and return a setter for it."""
    proc = tmp_path / "mounts"
    proc.write_text(_OTHER_LINES, encoding="utf-8")
    monkeypatch.setattr(paths, "_PROC_MOUNTS", proc)

    def declare(*mountpoints: Path | str) -> None:
        text = _OTHER_LINES + "".join(
            _FUSE_LINE.format(mountpoint=str(m).replace(" ", r"\040"))
            for m in mountpoints)
        proc.write_text(text, encoding="utf-8")

    return declare


# ═════════════════════════════════════════════════════════════════════════════
# I1 — no backend option on a command line
# ═════════════════════════════════════════════════════════════════════════════

class TestNoBackendFlags:
    def test_the_mount_argv_of_section_5_3_passes(self):
        """The application's own argv must never trip its own guard."""
        argv = [
            "/usr/bin/rclone", "mount", "onedrive:", "/home/u/OneDrive",
            "--vfs-cache-mode", "full", "--cache-dir", "/home/u/.cache/rclone",
            "--vfs-cache-max-size", "50G", "--vfs-cache-max-age", "720h",
            "--transfers", "4", "--checkers", "8", "--tpslimit", "8",
            "--file-perms", "0644", "--devname", "OneDrive",
            "--rc", "--rc-addr", "127.0.0.1:17800",
            "--user-agent", "ISV|OneDriveUI|OneDriveUI/0.1.0",
            "--use-json-log", "--color", "NEVER", "--log-level", "INFO",
        ]
        guards.assert_no_backend_flags(argv)

    def test_onedrive_chunk_size_is_refused_with_invariant_i1(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_no_backend_flags(
                ["rclone", "mount", "onedrive:", "/mnt",
                 "--onedrive-chunk-size", "30M"])
        assert excinfo.value.invariant == "I1"
        assert "--onedrive-chunk-size" in str(excinfo.value)
        assert "onedrive{HASH}:" in str(excinfo.value)

    def test_the_joined_value_form_is_refused_too(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_no_backend_flags(["rclone", "--onedrive-chunk-size=30M"])
        assert excinfo.value.invariant == "I1"

    @pytest.mark.parametrize("flag", [
        "--drive-chunk-size", "--s3-upload-cutoff", "--local-no-sparse",
        "--onedrive-no-versions", "--onedrive-delta", "--crypt-password",
    ])
    def test_every_backend_family_is_refused(self, flag):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_no_backend_flags(["rclone", "mount", flag, "x"])
        assert excinfo.value.invariant == "I1"

    @pytest.mark.parametrize("token", [
        ":local,nounc:/tmp",
        "onedrive,chunk_size=30M:Documents",
        ":onedrive,delta=true:",
    ])
    def test_connection_strings_are_refused(self, token):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_no_backend_flags(["rclone", "lsjson", token])
        assert excinfo.value.invariant == "I1"
        assert "connection string" in str(excinfo.value)

    @pytest.mark.parametrize("token", [
        "onedrive:", "onedrive:Documents", "/home/u/OneDrive",
        "127.0.0.1:17800", "ISV|OneDriveUI|OneDriveUI/0.1.0",
    ])
    def test_ordinary_arguments_are_not_mistaken_for_connection_strings(self, token):
        guards.assert_no_backend_flags(["rclone", "mount", token])

    def test_the_two_global_flags_that_look_like_backend_flags_are_allowed(self):
        """`--cache-dir` and `--http-proxy` are the only two collisions in the
        whole of rclone v1.75.0, and §5.3's argv needs `--cache-dir`."""
        guards.assert_no_backend_flags(
            ["rclone", "mount", "--cache-dir", "/c", "--http-proxy", "http://p"])
        assert guards.BACKEND_PREFIX_EXEMPT == {"--cache-dir", "--http-proxy"}

    def test_a_bare_backend_name_without_an_option_is_not_a_flag(self):
        guards.assert_no_backend_flags(["rclone", "--local"])

    def test_a_non_string_token_is_refused(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_no_backend_flags(["rclone", 30])       # type: ignore[list-item]
        assert excinfo.value.invariant == "I1"

    def test_the_backend_list_covers_the_two_invariant_i1_names(self):
        assert "onedrive" in guards.BACKEND_PREFIXES
        assert "drive" in guards.BACKEND_PREFIXES


# ═════════════════════════════════════════════════════════════════════════════
# I12 — never --inplace
# ═════════════════════════════════════════════════════════════════════════════

class TestNoInplace:
    @pytest.mark.parametrize("token", ["--inplace", "--inplace=true"])
    def test_inplace_is_refused(self, token):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_no_inplace(["rclone", "copy", "a", "b", token])
        assert excinfo.value.invariant == "I12"

    def test_a_clean_argv_passes(self):
        guards.assert_no_inplace(["rclone", "copy", "a", "b", "--partial-suffix",
                                  ".partial"])

    def test_a_lookalike_is_not_refused(self):
        guards.assert_no_inplace(["rclone", "copy", "--inplace-nothing"])


# ═════════════════════════════════════════════════════════════════════════════
# I2 — nothing under a fuse mount
# ═════════════════════════════════════════════════════════════════════════════

class TestNotUnderFuse:
    def test_a_path_inside_the_mount_is_refused(self, fake_mounts, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_not_under_fuse(mount / "Documents" / "x.docx", "sync")
        assert excinfo.value.invariant == "I2"
        assert "sync" in str(excinfo.value)

    def test_the_mountpoint_itself_is_refused(self, fake_mounts, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)
        with pytest.raises(SafetyRefusal):
            guards.assert_not_under_fuse(mount, "bisync path1")

    def test_a_path_that_does_not_exist_yet_still_answers(self, fake_mounts, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)
        with pytest.raises(SafetyRefusal):
            guards.assert_not_under_fuse(mount / "not" / "created" / "yet", "copy")

    def test_a_symlink_into_the_mount_is_resolved_first(self, fake_mounts, tmp_path):
        """A KFM'd ~/Documents is a symlink into the mount; comparing the
        unresolved path would let it through."""
        mount = tmp_path / "OneDrive"
        (mount / "Documents").mkdir(parents=True)
        fake_mounts(mount)
        link = tmp_path / "Documents"
        link.symlink_to(mount / "Documents")
        with pytest.raises(SafetyRefusal):
            guards.assert_not_under_fuse(link, "sync")

    def test_a_disjoint_path_passes(self, fake_mounts, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        guards.assert_not_under_fuse(offline, "bisync path1")

    def test_with_no_fuse_mounts_at_all_nothing_is_refused(self, fake_mounts, tmp_path):
        guards.assert_not_under_fuse(tmp_path / "anything", "sync")

    @pytest.mark.live
    def test_the_real_onedrive_mount_is_refused_on_this_machine(self):
        """ARCHITECTURE acceptance: assert_not_under_fuse(~/OneDrive/x) raises here."""
        target = REAL_HOME / "OneDrive"
        if not any(target == mount for _fs, mount in paths.fuse_rclone_mounts()):
            pytest.skip("no fuse.rclone mount at ~/OneDrive on this machine")
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_not_under_fuse(target / "x", "sync")
        assert excinfo.value.invariant == "I2"


class TestDisjoint:
    def test_a_folder_inside_the_mount_is_refused(self, tmp_path):
        mount = tmp_path / "OneDrive"
        (mount / "Offline").mkdir(parents=True)
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_disjoint(mount / "Offline", [mount])
        assert excinfo.value.invariant == "I2"

    def test_a_mount_inside_the_folder_is_refused_too(self, tmp_path):
        """Containment is fatal in both directions."""
        root = tmp_path / "sync"
        (root / "OneDrive").mkdir(parents=True)
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_disjoint(root, [root / "OneDrive"])
        assert excinfo.value.invariant == "I2"
        assert "is inside" in str(excinfo.value)

    def test_equality_is_refused(self, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_disjoint(mount, [mount])
        assert "IS the mountpoint" in str(excinfo.value)

    def test_siblings_pass(self, tmp_path):
        (tmp_path / "OneDrive").mkdir()
        (tmp_path / "OneDrive-Offline").mkdir()
        guards.assert_disjoint(tmp_path / "OneDrive-Offline",
                               [tmp_path / "OneDrive"])

    def test_a_name_prefix_is_not_containment(self, tmp_path):
        """~/OneDrive-Offline starts with ~/OneDrive as a *string* but is not
        inside it."""
        (tmp_path / "OneDrive").mkdir()
        (tmp_path / "OneDriveOther").mkdir()
        guards.assert_disjoint(tmp_path / "OneDriveOther", [tmp_path / "OneDrive"])

    def test_an_empty_mount_list_passes(self, tmp_path):
        guards.assert_disjoint(tmp_path, [])


class TestDbNotOnFuse:
    def test_a_database_under_the_mount_is_refused(self, fake_mounts, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_db_not_on_fuse(mount / "state.db")
        assert excinfo.value.invariant == "I2"
        assert "WAL" in str(excinfo.value)

    def test_a_database_in_the_data_dir_passes(self, fake_mounts):
        guards.assert_db_not_on_fuse(paths.db_file())


# ═════════════════════════════════════════════════════════════════════════════
# I3 — a dirty or queued cache item is irreplaceable
# ═════════════════════════════════════════════════════════════════════════════

class TestEvictSafe:
    def test_a_dirty_sidecar_is_refused(self):
        meta = {"Size": 10, "Rs": [{"Pos": 0, "Size": 10}], "Dirty": True}
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_evict_safe(meta, set(), "Documents/report.docx")
        assert excinfo.value.invariant == "I3"
        assert "Dirty" in str(excinfo.value)

    def test_an_item_in_the_upload_queue_is_refused(self):
        meta = {"Size": 10, "Rs": [{"Pos": 0, "Size": 10}], "Dirty": False}
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_evict_safe(meta, {"Documents/report.docx"},
                                     "Documents/report.docx")
        assert excinfo.value.invariant == "I3"
        assert "vfs/queue" in str(excinfo.value)

    def test_the_queue_name_is_matched_past_a_leading_slash(self):
        """vfs/queue reports 'a path within the VFS'; a sidecar walk yields the
        same path relative to pathMeta. They must still compare equal."""
        with pytest.raises(SafetyRefusal):
            guards.assert_evict_safe({}, {"/Documents/report.docx"},
                                     "Documents/report.docx")

    def test_a_clean_unqueued_item_passes(self):
        meta = {"Size": 10, "Rs": [{"Pos": 0, "Size": 10}], "Dirty": False}
        guards.assert_evict_safe(meta, {"other.bin"}, "Documents/report.docx")

    def test_an_empty_sidecar_passes(self):
        guards.assert_evict_safe({}, set(), "never-opened.txt")


# ═════════════════════════════════════════════════════════════════════════════
# I4 — cache paths come from vfs/stats
# ═════════════════════════════════════════════════════════════════════════════

class TestCachePathsFromStats:
    def test_the_two_paths_come_straight_off_vfs_stats(self):
        stats = {
            "fs": "onedrive{MxOuf}:",
            "diskCache": {
                "path": "/home/u/.cache/rclone/vfs/onedrive{MxOuf}",
                "pathMeta": "/home/u/.cache/rclone/vfsMeta/onedrive{MxOuf}",
                "bytesUsed": 178709025, "files": 22,
            },
        }
        data, meta = guards.assert_cache_paths_from_stats(stats)
        assert data.endswith("vfs/onedrive{MxOuf}")
        assert meta.endswith("vfsMeta/onedrive{MxOuf}")

    def test_a_vfs_with_the_cache_off_is_refused(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_cache_paths_from_stats({"fs": "onedrive:", "inUse": 1})
        assert excinfo.value.invariant == "I4"

    def test_an_empty_path_is_refused_rather_than_returned(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_cache_paths_from_stats(
                {"diskCache": {"path": "", "pathMeta": "/x"}})
        assert excinfo.value.invariant == "I4"


# ═════════════════════════════════════════════════════════════════════════════
# I13 — - *.partial is not optional
# ═════════════════════════════════════════════════════════════════════════════

class TestPartialExcluded:
    def test_the_frozen_mandatory_excludes_satisfy_it(self):
        guards.assert_partial_excluded(MANDATORY_EXCLUDES)

    def test_a_filters_file_without_it_is_refused(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_partial_excluded(["- *.tmp", "- desktop.ini", "+ **"])
        assert excinfo.value.invariant == "I13"
        assert "*.partial" in str(excinfo.value)

    def test_surrounding_whitespace_does_not_hide_the_rule(self):
        guards.assert_partial_excluded(["  - *.partial  ", "+ **"])

    def test_an_empty_filters_file_is_refused(self):
        with pytest.raises(SafetyRefusal):
            guards.assert_partial_excluded([])


# ═════════════════════════════════════════════════════════════════════════════
# The bisync preflight — I2 + I11 + I12 + I13 together
# ═════════════════════════════════════════════════════════════════════════════

class TestBisyncSafe:
    @staticmethod
    def _cfg(**overrides):
        cfg = {
            "filters_lines": list(MANDATORY_EXCLUDES),
            "filters_changed": False,
            "resync": False,
            "extra_args": [],
        }
        cfg.update(overrides)
        return cfg

    def test_a_clean_offline_folder_passes(self, fake_mounts, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        fake_mounts(mount)
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        guards.assert_bisync_safe(str(offline), "onedrive:Offline", self._cfg())

    def test_path1_under_the_mount_is_refused(self, fake_mounts, tmp_path):
        mount = tmp_path / "OneDrive"
        (mount / "Offline").mkdir(parents=True)
        fake_mounts(mount)
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bisync_safe(str(mount / "Offline"), "onedrive:Offline",
                                      self._cfg())
        assert excinfo.value.invariant == "I2"

    def test_two_overlapping_local_sides_are_refused(self, fake_mounts, tmp_path):
        outer = tmp_path / "a"
        (outer / "b").mkdir(parents=True)
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bisync_safe(str(outer), str(outer / "b"), self._cfg())
        assert excinfo.value.invariant == "I2"

    def test_a_filters_change_without_a_resync_is_refused(self, fake_mounts, tmp_path):
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bisync_safe(
                str(offline), "onedrive:Offline",
                self._cfg(filters_changed=True, resync=False))
        assert excinfo.value.invariant == "I11"

    def test_a_filters_change_paired_with_a_resync_passes(self, fake_mounts, tmp_path):
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        guards.assert_bisync_safe(str(offline), "onedrive:Offline",
                                  self._cfg(filters_changed=True, resync=True))

    def test_inplace_smuggled_through_extra_args_is_refused(self, fake_mounts, tmp_path):
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bisync_safe(str(offline), "onedrive:Offline",
                                      self._cfg(extra_args=["--inplace"]))
        assert excinfo.value.invariant == "I12"

    def test_filters_without_the_partial_rule_are_refused(self, fake_mounts, tmp_path):
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bisync_safe(str(offline), "onedrive:Offline",
                                      self._cfg(filters_lines=["- *.tmp"]))
        assert excinfo.value.invariant == "I13"

    def test_the_filters_file_is_read_when_no_lines_are_supplied(
            self, fake_mounts, tmp_path):
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        filters = tmp_path / "filters.txt"
        filters.write_text("- *.tmp\n", encoding="utf-8")
        cfg = self._cfg(filters_lines=None, filters_file=str(filters))
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bisync_safe(str(offline), "onedrive:Offline", cfg)
        assert excinfo.value.invariant == "I13"

    def test_an_unreadable_filters_file_is_refused_not_ignored(
            self, fake_mounts, tmp_path):
        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        cfg = self._cfg(filters_lines=None, filters_file=str(tmp_path / "gone.txt"))
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bisync_safe(str(offline), "onedrive:Offline", cfg)
        assert excinfo.value.invariant == "I13"

    def test_a_dataclass_style_config_works_as_well_as_a_mapping(
            self, fake_mounts, tmp_path):
        import types

        offline = tmp_path / "OneDrive-Offline"
        offline.mkdir()
        cfg = types.SimpleNamespace(filters_lines=list(MANDATORY_EXCLUDES),
                                    filters_changed=False, resync=False,
                                    extra_args=[])
        guards.assert_bisync_safe(str(offline), "onedrive:Offline", cfg)


# ═════════════════════════════════════════════════════════════════════════════
# I7, I8, I14 — endpoints nobody may call
# ═════════════════════════════════════════════════════════════════════════════

class TestRcPathAllowed:
    @pytest.mark.parametrize("path", [
        "mount/mount", "mount/unmount", "mount/unmountall", "mount/listmounts",
    ])
    def test_the_mount_family_raises_i7(self, path):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_rc_path_allowed(path)
        assert excinfo.value.invariant == "I7"

    def test_operations_cleanup_raises_i8(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_rc_path_allowed("operations/cleanup")
        assert excinfo.value.invariant == "I8"
        assert "VERSIONS" in str(excinfo.value)

    @pytest.mark.parametrize("path", ["config/dump", "config/get"])
    def test_the_token_leaking_endpoints_raise_i14(self, path):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_rc_path_allowed(path)
        assert excinfo.value.invariant == "I14"
        assert "refresh token" in str(excinfo.value)

    @pytest.mark.parametrize("path", [
        "core/stats", "rc/noop", "job/list", "job/status", "vfs/stats",
        "vfs/queue", "operations/list", "operations/about", "config/create",
        "config/listremotes", "sync/bisync", "core/bwlimit",
    ])
    def test_every_endpoint_the_application_needs_is_allowed(self, path):
        guards.assert_rc_path_allowed(path)

    def test_a_leading_slash_does_not_smuggle_a_banned_path_through(self):
        with pytest.raises(SafetyRefusal):
            guards.assert_rc_path_allowed("/mount/mount")

    def test_the_ban_list_covers_everything_the_wp00_fake_bans(self):
        """The fake daemon asserts on these; the guard must refuse them first."""
        assert BANNED_PATHS <= guards.BANNED_RC_PATHS


class TestBundleSafe:
    def test_a_bundle_naming_config_dump_is_refused(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bundle_safe(["app.log", "rc/config/dump.json"])
        assert excinfo.value.invariant == "I14"

    def test_a_bundle_naming_endpoints_json_is_refused(self):
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bundle_safe(
                ["app.log", "/run/user/1000/onedriveui/endpoints.json"])
        assert excinfo.value.invariant == "I14"
        assert "rc password" in str(excinfo.value)

    def test_a_bundle_naming_rclone_conf_is_refused(self):
        """rclone.conf is the file the OAuth refresh token actually lives in.

        The rc paths and endpoints.json were already covered; this is the third
        secret source I14 names, and the one a well-meaning "attach my config"
        button would reach for first.
        """
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.assert_bundle_safe(
                ["app.log", "/home/u/.config/rclone/rclone.conf"])
        assert excinfo.value.invariant == "I14"
        assert "refresh token" in str(excinfo.value)

    def test_the_redacted_sibling_is_still_allowed(self):
        """applog ships rclone-config-redacted.ini; it must not be caught."""
        guards.assert_bundle_safe(
            ["report.txt", "recent.log", "rclone-config-redacted.ini",
             "rclone-version.txt"])

    def test_every_secret_file_is_refused_by_basename(self):
        for name in guards.SECRET_FILES:
            with pytest.raises(SafetyRefusal) as excinfo:
                guards.assert_bundle_safe([f"/some/deep/path/{name}"])
            assert excinfo.value.invariant == "I14"

    def test_the_real_bundle_manifest_passes_its_own_guard(self):
        """What applog actually puts in the archive must satisfy the guard."""
        guards.assert_bundle_safe(
            ["report.txt", "recent.log", "config.json",
             "rclone-config-redacted.ini", "rclone-version.txt"])

    def test_an_ordinary_bundle_passes(self):
        guards.assert_bundle_safe(
            ["app.log", "rclone-redacted.conf", "state.json", "bisync.jsonl"])


# ═════════════════════════════════════════════════════════════════════════════
# Turning a mounted path back into a remote path
# ═════════════════════════════════════════════════════════════════════════════

class TestRewriteMountPathToRemote:
    @staticmethod
    def _account(root):
        return AccountInfo(id="onedrive", remote="onedrive", sync_root=str(root))

    def test_a_file_inside_the_mount_becomes_a_remote_path(self, tmp_path):
        mount = tmp_path / "OneDrive"
        (mount / "Documents").mkdir(parents=True)
        target = mount / "Documents" / "report.docx"
        target.write_text("x", encoding="utf-8")
        assert (guards.rewrite_mount_path_to_remote(target, self._account(mount))
                == "onedrive:Documents/report.docx")

    def test_the_mountpoint_itself_becomes_the_bare_remote(self, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        assert (guards.rewrite_mount_path_to_remote(mount, self._account(mount))
                == "onedrive:")

    def test_a_path_outside_the_mount_is_refused(self, tmp_path):
        mount = tmp_path / "OneDrive"
        mount.mkdir()
        (tmp_path / "elsewhere").mkdir()
        with pytest.raises(SafetyRefusal) as excinfo:
            guards.rewrite_mount_path_to_remote(tmp_path / "elsewhere",
                                                self._account(mount))
        assert excinfo.value.invariant == "I2"

    def test_the_result_never_carries_a_leading_slash(self, tmp_path):
        mount = tmp_path / "OneDrive"
        (mount / "a" / "b").mkdir(parents=True)
        out = guards.rewrite_mount_path_to_remote(mount / "a" / "b",
                                                  self._account(mount))
        assert out == "onedrive:a/b"
        assert ":/" not in out

    def test_a_relative_component_is_normalised_away(self, tmp_path):
        mount = tmp_path / "OneDrive"
        (mount / "a").mkdir(parents=True)
        messy = os.path.join(str(mount), "a", "..", "a")
        assert (guards.rewrite_mount_path_to_remote(messy, self._account(mount))
                == "onedrive:a")
