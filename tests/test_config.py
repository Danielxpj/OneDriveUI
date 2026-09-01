"""config.py — every §9 key, load/save/migrate/validate, and the refusals.

Two asymmetric promises are under test:

* loading a broken file NEVER raises — a user must always be able to start the
  app and fix the setting in the UI;
* saving a dangerous configuration ALWAYS raises — we tolerate a bad file we
  did not write, and refuse to author one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from onedriveui import config, paths
from onedriveui.config import AppConfig
from onedriveui.constants import (
    MAX_CHECKERS, MAX_TRANSFERS, ONEDRIVE_CHUNK_MULTIPLE, RC_FORBIDDEN_PORTS,
)
from onedriveui.errors import ConfigError
from onedriveui.units import parse_size
from tests.conftest import REAL_HOME, default_config


@pytest.fixture(autouse=True)
def _forget_save_cache():
    """`save()` remembers the last document per path; keep tests independent."""
    config.forget_write_cache()
    yield
    config.forget_write_cache()


@pytest.fixture
def no_mounts(monkeypatch):
    """No FUSE mounts anywhere, so path validation is deterministic."""
    monkeypatch.setattr(config, "_fuse_mounts", lambda: [])
    monkeypatch.setattr(paths, "fuse_rclone_mounts", lambda: [])
    return []


def valid() -> AppConfig:
    """A configuration built from the conftest defaults, with one account."""
    return AppConfig.from_dict(default_config())


# ═════════════════════════════════════════════════════════════════════════════
# The schema itself
# ═════════════════════════════════════════════════════════════════════════════

def test_every_section_of_architecture_9_exists():
    """17 account sections plus `app` and `advanced`."""
    assert config.section_names() == (
        "app", "advanced", "mount", "backend", "files_on_demand", "bandwidth",
        "pause", "notifications", "safety", "files", "conflicts", "selective",
        "kfm", "offline_folder", "sharing", "vault", "extras", "integration",
        "ui")
    assert len(config.ACCOUNT_SECTIONS) == 17
    assert set(config.SECTION_TYPES) == set(config.ACCOUNT_SECTIONS)


def test_defaults_match_the_frozen_fixture_key_for_key():
    """The conftest fixture is a transcription of §9; ours must equal it.

    Key ORDER is asserted too: the file is meant to be hand-diffable.
    """
    produced = AppConfig.from_dict(default_config()).to_dict()
    expected = default_config()
    assert produced == expected
    assert list(produced) == list(expected)
    assert list(produced["accounts"][0]) == list(expected["accounts"][0])
    for section in expected["accounts"][0]:
        if isinstance(expected["accounts"][0][section], dict):
            assert (list(produced["accounts"][0][section])
                    == list(expected["accounts"][0][section])), section


def test_pure_defaults_have_no_accounts():
    """A fresh install has no account until the OOBE adds one."""
    cfg = config.defaults()
    assert cfg.accounts == []
    assert cfg.app.first_run_complete is False
    assert cfg.account() is None


def test_defaults_with_account_validate(no_mounts):
    assert config.validate(config.defaults(with_account=True)) == []


def test_the_fixture_config_validates(no_mounts):
    assert config.validate(valid()) == []


def test_defaults_are_taken_from_constants_not_retyped():
    cfg = config.defaults(with_account=True)
    acc = cfg.account()
    assert acc.mount.transfers == MAX_TRANSFERS
    assert acc.mount.checkers == MAX_CHECKERS
    assert cfg.advanced.rc_port_range == [17800, 17899]
    from onedriveui import USER_AGENT
    assert cfg.advanced.user_agent == USER_AGENT


def test_account_id_defaults_to_the_remote_name():
    cfg = AppConfig.from_dict({"accounts": [{"remote": "work"}]})
    assert cfg.accounts[0].id == "work"


def test_account_to_account_info_bridges_to_the_frozen_model():
    acc = valid().account()
    acc.display_name = "Test User"
    info = acc.to_account_info()
    assert info.id == "onedrive"
    assert info.fs == "onedrive:"
    assert info.display_name == "Test User"
    assert Path(info.sync_root).is_absolute()


def test_nullable_fields_stay_null():
    acc = valid().account()
    assert acc.display_name is None
    assert acc.email is None
    assert acc.backend.encoding is None
    assert acc.bandwidth.download_kb is None
    assert acc.files_on_demand.auto_free_up_days is None
    assert acc.sharing.default_expiry_days is None
    assert acc.pause.manual_until is None


# ═════════════════════════════════════════════════════════════════════════════
# load — the total function
# ═════════════════════════════════════════════════════════════════════════════

def test_load_reads_a_good_file(tmp_config, no_mounts):
    tmp_config.set("app.theme", "dark")
    cfg = config.load()
    assert cfg.app.theme == "dark"
    assert cfg.account().id == "onedrive"


def test_a_corrupt_config_loads_all_defaults_and_raises_nothing(tmp_config):
    """BUILD_PLAN acceptance."""
    tmp_config.corrupt()
    cfg = config.load()                     # must not raise
    assert cfg.to_dict() == config.defaults().to_dict()
    assert cfg.schema_version == config.CONFIG_SCHEMA_VERSION


def test_a_corrupt_config_is_repaired_from_the_bak(tmp_config):
    """§9: repaired from `.bak` on a JSONDecodeError."""
    good = default_config()
    good["app"]["theme"] = "dark"
    paths.config_bak_file().write_text(json.dumps(good), encoding="utf-8")
    tmp_config.corrupt()
    cfg = config.load()
    assert cfg.app.theme == "dark"
    assert cfg.account() is not None


def test_a_corrupt_config_and_a_corrupt_bak_still_load(tmp_config):
    paths.config_bak_file().write_text("also not json", encoding="utf-8")
    tmp_config.corrupt()
    assert config.load().to_dict() == config.defaults().to_dict()


def test_a_missing_config_loads_defaults():
    assert config.load().to_dict() == config.defaults().to_dict()


@pytest.mark.parametrize("garbage", ["[]", '"a string"', "null", "42"])
def test_a_config_that_is_not_an_object_loads_defaults(tmp_config, garbage):
    tmp_config.path.write_text(garbage, encoding="utf-8")
    assert config.load().to_dict() == config.defaults().to_dict()


def test_a_key_of_the_wrong_type_falls_back_to_its_default(tmp_config):
    data = default_config()
    data["app"]["autostart"] = "not a bool"
    data["advanced"]["keep_logs_days"] = {"nope": 1}
    data["accounts"][0]["mount"]["tpslimit"] = "eight"
    tmp_config.write(data)
    cfg = config.load()
    assert cfg.app.autostart is True                       # the default
    assert cfg.advanced.keep_logs_days == 14
    assert cfg.account().mount.tpslimit == 8.0
    # and the neighbouring good keys survived
    assert cfg.app.start_minimized is True
    assert cfg.account().mount.checkers == 8


def test_a_value_outside_its_enumeration_falls_back(tmp_config):
    data = default_config()
    data["app"]["theme"] = "neon"
    data["accounts"][0]["files"]["name_policy"] = "posix"
    tmp_config.write(data)
    cfg = config.load()
    assert cfg.app.theme == "system"
    assert cfg.account().files.name_policy == "windows"


def test_an_unknown_key_is_dropped_not_carried(tmp_config):
    data = default_config()
    data["app"]["future_option"] = True
    data["accounts"][0]["mount"]["invented"] = 1
    tmp_config.write(data)
    assert "future_option" not in config.load().to_dict()["app"]


def test_a_string_boolean_is_accepted(tmp_config):
    """Hand-edited files really do contain `"true"`."""
    data = default_config()
    data["app"]["autostart"] = "false"
    tmp_config.write(data)
    assert config.load().app.autostart is False


def test_load_clamps_unsafe_values_rather_than_refusing(tmp_config):
    data = default_config()
    data["accounts"][0]["mount"]["transfers"] = 32
    data["accounts"][0]["mount"]["checkers"] = 64
    data["accounts"][0]["mount"]["read_chunk_streams"] = 8
    data["accounts"][0]["mount"]["poll_interval_s"] = 7200
    data["accounts"][0]["backend"]["chunk_size"] = "7M"
    tmp_config.write(data)
    cfg = config.load()
    acc = cfg.account()
    assert acc.mount.transfers == MAX_TRANSFERS
    assert acc.mount.checkers == MAX_CHECKERS
    assert acc.mount.read_chunk_streams == 0
    assert acc.mount.poll_interval_s < acc.mount.dir_cache_time_s
    assert parse_size(acc.backend.chunk_size) % ONEDRIVE_CHUNK_MULTIPLE == 0


def test_clamp_reports_what_it_changed():
    cfg = config.defaults(with_account=True)
    cfg.account().mount.transfers = 99
    cfg.account().safety.refuse_paths_under_mount = False
    fixed = config.clamp(cfg)
    assert "mount.transfers" in fixed
    assert "safety.refuse_paths_under_mount" in fixed
    assert cfg.account().mount.transfers == MAX_TRANSFERS
    assert cfg.account().safety.refuse_paths_under_mount is True


def test_clamp_strips_a_banned_extra_arg():
    cfg = config.defaults(with_account=True)
    cfg.account().mount.extra_args = ["--vfs-fast-fingerprint",
                                      "--onedrive-chunk-size=10M", "--inplace"]
    config.clamp(cfg)
    assert cfg.account().mount.extra_args == ["--vfs-fast-fingerprint"]


def test_load_of_a_clean_file_changes_nothing(tmp_config, no_mounts):
    """A valid document must survive a load/save round trip untouched."""
    before = tmp_config.reload()
    cfg = config.load()
    assert cfg.to_dict() == before


# ═════════════════════════════════════════════════════════════════════════════
# migrate
# ═════════════════════════════════════════════════════════════════════════════

def test_migrate_stamps_the_current_version():
    assert config.migrate({})["schema_version"] == config.CONFIG_SCHEMA_VERSION


def test_migrate_wraps_a_flat_pre_versioned_document():
    """A v0 document had its account sections at the top level."""
    flat = {"remote": "work", "sync_root": "~/Work",
            "mount": {"transfers": 2}, "app": {"theme": "dark"}}
    migrated = config.migrate(flat)
    assert migrated["schema_version"] == 1
    assert len(migrated["accounts"]) == 1
    assert migrated["accounts"][0]["remote"] == "work"
    assert migrated["accounts"][0]["mount"]["transfers"] == 2
    assert migrated["app"] == {"theme": "dark"}     # top-level keys stay put
    assert "mount" not in migrated


def test_migrate_leaves_a_v1_document_alone():
    data = default_config()
    assert config.migrate(data)["accounts"] == data["accounts"]


def test_migrate_does_not_mutate_its_input():
    data = default_config()
    snapshot = json.dumps(data, sort_keys=True)
    config.migrate(data)
    assert json.dumps(data, sort_keys=True) == snapshot


def test_a_newer_schema_version_downgrades_instead_of_failing(tmp_config):
    data = default_config()
    data["schema_version"] = 99
    data["app"]["theme"] = "dark"
    tmp_config.write(data)
    cfg = config.load()
    assert cfg.schema_version == config.CONFIG_SCHEMA_VERSION
    assert cfg.app.theme == "dark"


def test_an_upgraded_flat_document_loads_end_to_end(tmp_config):
    tmp_config.path.write_text(json.dumps({
        "remote": "work", "sync_root": "/tmp/work",
        "mount": {"transfers": 2}}), encoding="utf-8")
    cfg = config.load()
    assert cfg.account().remote == "work"
    assert cfg.account().mount.transfers == 2


# ═════════════════════════════════════════════════════════════════════════════
# validate — the five documented refusals
# ═════════════════════════════════════════════════════════════════════════════

def test_validate_rejects_more_than_four_transfers(no_mounts):
    cfg = valid()
    cfg.account().mount.transfers = 5
    problems = config.validate(cfg, raise_on_error=False)
    assert any("mount.transfers" in p for p in problems)
    with pytest.raises(ConfigError, match="mount.transfers"):
        config.validate(cfg)


def test_validate_rejects_more_than_eight_checkers(no_mounts):
    cfg = valid()
    cfg.account().mount.checkers = 9
    with pytest.raises(ConfigError, match="mount.checkers"):
        config.validate(cfg)


@pytest.mark.parametrize("chunk", ["7M", "1", "100k", "0", "off"])
def test_validate_rejects_a_chunk_size_that_is_not_a_multiple_of_320_kib(
        no_mounts, chunk):
    cfg = valid()
    cfg.account().backend.chunk_size = chunk
    with pytest.raises(ConfigError, match="backend.chunk_size"):
        config.validate(cfg)


@pytest.mark.parametrize("chunk", ["320k", "10M", "640k", "1280k"])
def test_validate_accepts_a_multiple_of_320_kib(no_mounts, chunk):
    cfg = valid()
    cfg.account().backend.chunk_size = chunk
    assert config.validate(cfg) == []


def test_validate_rejects_poll_interval_at_or_above_dir_cache_time(no_mounts):
    cfg = valid()
    cfg.account().mount.poll_interval_s = 3600
    cfg.account().mount.dir_cache_time_s = 3600
    with pytest.raises(ConfigError, match="poll_interval_s"):
        config.validate(cfg)
    cfg.account().mount.poll_interval_s = 3599
    assert config.validate(cfg) == []


def test_validate_rejects_a_sync_root_under_a_fuse_mount(tmp_path, monkeypatch):
    """I2: the sync root must BE a mountpoint, never live under one."""
    mount = tmp_path / "OneDrive"
    (mount / "Nested").mkdir(parents=True)
    monkeypatch.setattr(config, "_fuse_mounts", lambda: [("onedrive:", mount)])

    cfg = valid()
    cfg.account().sync_root = str(mount / "Nested")
    with pytest.raises(ConfigError, match="sync_root"):
        config.validate(cfg)


def test_validate_allows_a_sync_root_that_IS_the_mountpoint(tmp_path, monkeypatch):
    """The normal case: our own mount is live and the root points at it."""
    mount = tmp_path / "OneDrive"
    mount.mkdir()
    monkeypatch.setattr(config, "_fuse_mounts", lambda: [("onedrive:", mount)])
    cfg = valid()
    cfg.account().sync_root = str(mount)
    assert config.validate(cfg) == []


def test_the_default_sync_root_is_accepted_on_this_machine():
    """~/OneDrive really is a live fuse.rclone mount here; that must be legal."""
    cfg = valid()
    cfg.account().sync_root = str(paths.default_sync_root())
    assert config.validate(cfg, raise_on_error=False) == []


# ═════════════════════════════════════════════════════════════════════════════
# validate — the invariants it also protects
# ═════════════════════════════════════════════════════════════════════════════

def test_validate_rejects_a_backend_flag_in_extra_args(no_mounts):
    """I1: a command-line backend option relocates the entire VFS cache."""
    cfg = valid()
    cfg.account().mount.extra_args = ["--onedrive-chunk-size", "10M"]
    with pytest.raises(ConfigError, match="I1"):
        config.validate(cfg)


def test_validate_rejects_inplace(no_mounts):
    """I12."""
    cfg = valid()
    cfg.account().mount.extra_args = ["--inplace"]
    with pytest.raises(ConfigError, match="I12"):
        config.validate(cfg)


def test_validate_rejects_no_versions_on_a_personal_drive(no_mounts):
    """I9: Personal cannot delete versions."""
    cfg = valid()
    cfg.account().backend.no_versions = True
    with pytest.raises(ConfigError, match="I9"):
        config.validate(cfg)


def test_validate_rejects_hard_delete_on_a_personal_drive(no_mounts):
    cfg = valid()
    cfg.account().backend.hard_delete = True
    with pytest.raises(ConfigError, match="I9"):
        config.validate(cfg)


def test_validate_allows_hard_delete_on_business(no_mounts):
    cfg = valid()
    acc = cfg.account()
    acc.kind = "business"
    acc.drive_type = "business"
    acc.backend.hard_delete = True
    assert config.validate(cfg) == []


def test_validate_refuses_to_disable_the_mount_path_guard(no_mounts):
    """safety.refuse_paths_under_mount is read-only (I2)."""
    cfg = valid()
    cfg.account().safety.refuse_paths_under_mount = False
    with pytest.raises(ConfigError, match="refuse_paths_under_mount"):
        config.validate(cfg)


@pytest.mark.parametrize("port", sorted(RC_FORBIDDEN_PORTS))
def test_validate_rejects_a_port_range_covering_a_forbidden_port(no_mounts, port):
    cfg = valid()
    cfg.advanced.rc_port_range = [port - 1, port + 1]
    with pytest.raises(ConfigError, match="rc_port_range"):
        config.validate(cfg)


def test_validate_rejects_an_offline_folder_inside_the_sync_root(no_mounts):
    """I2: bisync may never name a path under the mount."""
    cfg = valid()
    acc = cfg.account()
    acc.sync_root = "/tmp/onedriveui-test/OneDrive"
    acc.offline_folder.enabled = True
    acc.offline_folder.local_path = "/tmp/onedriveui-test/OneDrive/Offline"
    with pytest.raises(ConfigError, match="offline_folder.local_path"):
        config.validate(cfg)


def test_the_default_offline_path_is_disjoint_from_the_default_root(no_mounts):
    """~/OneDrive-Offline must NOT be considered under ~/OneDrive."""
    cfg = valid()
    cfg.account().offline_folder.enabled = True
    assert config.validate(cfg) == []


def test_validate_rejects_a_max_lock_below_the_hard_minimum(no_mounts):
    cfg = valid()
    cfg.account().offline_folder.max_lock = "30s"
    with pytest.raises(ConfigError, match="max_lock"):
        config.validate(cfg)


def test_validate_rejects_bandwidth_outside_the_ui_range(no_mounts):
    cfg = valid()
    cfg.account().bandwidth.download_kb = 10          # below the 50 floor
    with pytest.raises(ConfigError, match="bandwidth.download_kb"):
        config.validate(cfg)
    cfg.account().bandwidth.download_kb = 200_000     # above the ceiling
    with pytest.raises(ConfigError, match="bandwidth.download_kb"):
        config.validate(cfg)


def test_validate_rejects_a_vault_lock_interval_the_ui_cannot_offer(no_mounts):
    cfg = valid()
    cfg.account().vault.auto_lock_minutes = 45
    with pytest.raises(ConfigError, match="auto_lock_minutes"):
        config.validate(cfg)


def test_validate_rejects_read_chunk_streams(no_mounts):
    cfg = valid()
    cfg.account().mount.read_chunk_streams = 4
    with pytest.raises(ConfigError, match="read_chunk_streams"):
        config.validate(cfg)


def test_validate_rejects_duplicate_account_ids(no_mounts):
    cfg = valid()
    cfg.accounts.append(config.AccountConfig(id="onedrive", remote="two"))
    with pytest.raises(ConfigError, match="more than once"):
        config.validate(cfg)


def test_validate_rejects_a_remote_with_a_colon(no_mounts):
    cfg = valid()
    cfg.account().remote = "onedrive:"
    with pytest.raises(ConfigError, match="bare remote name"):
        config.validate(cfg)


def test_validate_rejects_an_active_account_that_does_not_exist(no_mounts):
    cfg = valid()
    cfg.app.active_account_id = "ghost"
    with pytest.raises(ConfigError, match="active_account_id"):
        config.validate(cfg)


def test_validate_reports_every_problem_at_once(no_mounts):
    cfg = valid()
    cfg.account().mount.transfers = 9
    cfg.account().mount.checkers = 9
    cfg.account().backend.chunk_size = "7M"
    problems = config.validate(cfg, raise_on_error=False)
    assert len(problems) >= 3
    with pytest.raises(ConfigError) as excinfo:
        config.validate(cfg)
    for key in ("mount.transfers", "mount.checkers", "backend.chunk_size"):
        assert key in str(excinfo.value)


# ═════════════════════════════════════════════════════════════════════════════
# save
# ═════════════════════════════════════════════════════════════════════════════

def test_save_writes_atomically_with_0600_and_a_bak(tmp_config, no_mounts):
    cfg = config.load()
    cfg.app.theme = "dark"
    written = config.save(cfg)
    assert written == paths.config_file()
    assert written.stat().st_mode & 0o777 == 0o600
    assert json.loads(written.read_text())["app"]["theme"] == "dark"
    assert paths.config_bak_file().exists()
    assert json.loads(paths.config_bak_file().read_text())["app"]["theme"] == "system"


def test_save_refuses_an_invalid_config_and_writes_nothing(tmp_config, no_mounts):
    before = tmp_config.path.read_bytes()
    cfg = config.load()
    cfg.account().mount.transfers = 99
    with pytest.raises(ConfigError):
        config.save(cfg)
    assert tmp_config.path.read_bytes() == before


def test_save_emits_config_changed_with_the_dotted_key(tmp_config, bus_spy,
                                                       no_mounts):
    """BUILD_PLAN acceptance."""
    bus_spy.watch("config_changed")
    cfg = config.load()
    cfg.app.theme = "dark"
    config.save(cfg)
    assert [args[0] for args in bus_spy.of("config_changed")] == ["app.theme"]


def test_save_emits_a_section_first_key_for_an_account_setting(tmp_config,
                                                              bus_spy, no_mounts):
    """The bus documents `mount.transfers`; that is the spelling consumers filter."""
    bus_spy.watch("config_changed")
    cfg = config.load()
    cfg.account().mount.transfers = 2
    cfg.account().bandwidth.download_kb = 500
    cfg.account().bandwidth.limit_download = True
    config.save(cfg)
    keys = [args[0] for args in bus_spy.of("config_changed")]
    assert "mount.transfers" in keys
    assert "bandwidth.download_kb" in keys
    assert "bandwidth.limit_download" in keys


def test_save_emits_account_prefixed_keys_for_account_scalars(tmp_config,
                                                             bus_spy, no_mounts):
    bus_spy.watch("config_changed")
    cfg = config.load()
    cfg.account().display_name = "Someone"
    config.save(cfg)
    assert [args[0] for args in bus_spy.of("config_changed")] == \
        ["account.display_name"]


def test_save_emits_accounts_when_an_account_is_added(tmp_config, bus_spy,
                                                      no_mounts):
    bus_spy.watch("config_changed")
    cfg = config.load()
    cfg.accounts.append(config.AccountConfig(id="work", remote="work",
                                             sync_root="/tmp/work"))
    config.save(cfg)
    assert "accounts" in [args[0] for args in bus_spy.of("config_changed")]


def test_saving_an_unchanged_config_emits_nothing(tmp_config, bus_spy, no_mounts):
    cfg = config.load()
    config.save(cfg)                       # normalise the file first
    bus_spy.watch("config_changed")
    config.save(cfg)
    assert bus_spy.count("config_changed") == 0


def test_save_with_emit_false_is_silent(tmp_config, bus_spy, no_mounts):
    bus_spy.watch("config_changed")
    cfg = config.load()
    cfg.app.theme = "dark"
    config.save(cfg, emit=False)
    assert bus_spy.count("config_changed") == 0


def test_a_first_save_with_no_previous_file_emits_nothing(tmp_path, bus_spy,
                                                          no_mounts):
    bus_spy.watch("config_changed")
    config.save(config.defaults(with_account=True), tmp_path / "config.json")
    assert bus_spy.count("config_changed") == 0


def test_save_then_load_round_trips(tmp_config, no_mounts):
    cfg = config.load()
    cfg.app.theme = "dark"
    cfg.account().mount.transfers = 2
    cfg.account().selective.excluded_paths = ["Photos/Raw", "Archive"]
    cfg.account().ui.window_geometry = {"w": 1024, "h": 720}
    config.save(cfg)
    again = config.load()
    assert again.to_dict() == cfg.to_dict()
    assert again.account().selective.excluded_paths == ["Photos/Raw", "Archive"]
    assert again.account().ui.window_geometry == {"w": 1024, "h": 720}


# ═════════════════════════════════════════════════════════════════════════════
# get / set / account
# ═════════════════════════════════════════════════════════════════════════════

def test_get_and_set_by_dotted_key():
    cfg = valid()
    assert cfg.get("app.theme") == "system"
    assert cfg.get("mount.transfers") == 4
    assert cfg.get("account.sync_root") == "~/OneDrive"
    assert cfg.get("schema_version") == 1
    assert cfg.get("nope.nothing", "fallback") == "fallback"

    assert cfg.set("app.theme", "dark") is True
    assert cfg.set("app.theme", "dark") is False        # unchanged
    assert cfg.get("app.theme") == "dark"


def test_set_coerces_a_string_from_a_line_edit():
    cfg = valid()
    assert cfg.set("mount.transfers", "2") is True
    assert cfg.get("mount.transfers") == 2


def test_set_refuses_a_value_it_cannot_coerce():
    cfg = valid()
    with pytest.raises(ConfigError):
        cfg.set("mount.transfers", "many")
    with pytest.raises(ConfigError):
        cfg.set("app.theme", "neon")        # outside the enumeration


def test_set_of_an_unknown_key_returns_false():
    assert valid().set("nowhere.at.all", 1) is False


def test_account_lookup_honours_the_active_account():
    cfg = valid()
    cfg.accounts.append(config.AccountConfig(id="work", remote="work"))
    assert config.account(cfg).id == "onedrive"
    cfg.app.active_account_id = "work"
    assert config.account(cfg).id == "work"
    assert config.account(cfg, "onedrive").id == "onedrive"
    assert config.account(cfg, "ghost") is None


def test_account_lookup_of_an_empty_config_is_none():
    assert config.account(config.defaults()) is None


def test_dotted_keys_covers_every_field():
    keys = config.dotted_keys()
    assert "app.theme" in keys
    assert "mount.transfers" in keys
    assert "account.sync_root" in keys
    assert "ui.window_geometry" in keys
    assert len(keys) == len(set(keys))
    # 1 top-level + 11 app + 8 advanced + 9 account scalars + every section key
    assert len(keys) == 1 + 11 + 8 + 9 + sum(
        len(config.SECTION_TYPES[name].__dataclass_fields__)
        for name in config.ACCOUNT_SECTIONS)


def test_iter_sections_yields_them_in_order():
    acc = valid().account()
    assert [name for name, _ in config.iter_sections(acc)] == \
        list(config.ACCOUNT_SECTIONS)


# ═════════════════════════════════════════════════════════════════════════════
# changed_keys
# ═════════════════════════════════════════════════════════════════════════════

def test_changed_keys_is_empty_for_identical_documents():
    assert config.changed_keys(default_config(), default_config()) == []


def test_changed_keys_reports_each_key_once():
    old = default_config()
    new = default_config()
    new["app"]["theme"] = "dark"
    new["accounts"][0]["mount"]["transfers"] = 2
    new["accounts"][0]["sync_root"] = "/tmp/x"
    assert config.changed_keys(old, new) == [
        "app.theme", "account.sync_root", "mount.transfers"]


def test_changed_keys_notices_a_removed_account():
    old = default_config()
    new = default_config()
    new["accounts"] = []
    assert "accounts" in config.changed_keys(old, new)


def test_default_device_name_is_a_single_label():
    name = config.default_device_name()
    assert name
    assert "." not in name
    assert "/" not in name


# ═════════════════════════════════════════════════════════════════════════════
# Environment isolation
# ═════════════════════════════════════════════════════════════════════════════

def test_config_never_touches_the_real_home(tmp_config):
    """The autouse HOME isolation must actually be in force."""
    assert str(paths.config_file()).startswith(os.environ["HOME"])
    assert not paths.config_file().is_relative_to(REAL_HOME / ".config")
