"""WP-02 — `onedriveui/rc/conf.py`.

The acceptance that matters most is negative: after `set_backend_options()` the
`token` line must come back **byte-identical**. `rclone.conf` holds the OAuth
refresh token, and a round trip through `configparser` would rewrite that line's
spacing and mangle a token containing a `%`. Several tests here compare exact
bytes rather than parsed values for exactly that reason.

Nothing shells out to rclone. `redacted_dump()` reproduces `rclone config
redacted` in Python, and the expected output below was checked against the real
binary on this machine.
"""

from __future__ import annotations

import stat

import pytest

from onedriveui import paths
from onedriveui.constants import ONEDRIVE_CHUNK_MULTIPLE
from onedriveui.errors import ConfigError, SafetyRefusal
from onedriveui.models import AccountKind
from onedriveui.rc import conf

# A realistic rclone.conf: an OAuth token blob with punctuation a naive rewriter
# would mangle (`%`, `:`, `=`, `{`), a second remote, comments and blank lines.
TOKEN_LINE = (
    'token = {"access_token":"EwBIA8l6BAAUs5%2Bp","token_type":"Bearer",'
    '"refresh_token":"M.C523_BAY.0.U.-Cq0m","expiry":"2026-09-01T04:12:07.1-04:00"}'
)
SAMPLE = f"""# rclone configuration, hand-edited once
[onedrive]
type = onedrive
{TOKEN_LINE}
drive_id = 1A2B3C4D5E6F7890
drive_type = personal

[work]
type = onedrive
drive_type = business
{TOKEN_LINE}
"""


@pytest.fixture
def rclone_conf(monkeypatch, tmp_path):
    """A real `rclone.conf` at 0600, pointed at by `$RCLONE_CONFIG`."""
    path = tmp_path / "rclone.conf"
    path.write_text(SAMPLE, encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("RCLONE_CONFIG", str(path))
    return path


def _line(path, prefix: str) -> str:
    return next(l for l in path.read_text(encoding="utf-8").splitlines()
                if l.startswith(prefix))


# ═════════════════════════════════════════════════════════════════════════════
# Reading
# ═════════════════════════════════════════════════════════════════════════════

class TestRead:
    def test_it_points_at_rclone_s_own_config_by_default(self, monkeypatch):
        monkeypatch.delenv("RCLONE_CONFIG", raising=False)
        assert conf.raw_text() == ""            # isolated HOME, no config yet
        assert paths.rclone_conf().name == "rclone.conf"

    def test_rclone_config_overrides_the_location(self, rclone_conf):
        assert conf.raw_text() == SAMPLE

    def test_a_missing_file_reads_as_empty_rather_than_raising(self, monkeypatch,
                                                               tmp_path):
        monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "nope.conf"))
        assert conf.raw_text() == ""
        assert conf.read() == {}
        assert conf.remotes() == []

    def test_the_sections_parse_in_file_order(self, rclone_conf):
        parsed = conf.read()
        assert list(parsed) == ["onedrive", "work"]
        assert parsed["onedrive"]["type"] == "onedrive"
        assert parsed["onedrive"]["drive_id"] == "1A2B3C4D5E6F7890"

    def test_the_token_value_survives_parsing_intact(self, rclone_conf):
        """`=`, `:` and `%` inside the JSON blob must not be treated as syntax."""
        token = conf.read()["onedrive"]["token"]
        assert token == TOKEN_LINE.split(" = ", 1)[1]
        assert "%2B" in token and '"expiry":' in token

    def test_comments_and_blanks_are_dropped(self, rclone_conf):
        assert all(not k.startswith("#") for k in conf.read()["onedrive"])

    def test_remote_type_and_drive_type(self, rclone_conf):
        assert conf.remote_type("onedrive") == "onedrive"
        assert conf.remote_type("onedrive:") == "onedrive"
        assert conf.drive_type("onedrive") == "personal"
        assert conf.drive_type("work:") == "business"

    def test_an_unknown_remote_answers_empty(self, rclone_conf):
        assert conf.remote_type("nope") == ""
        assert conf.drive_type("nope") == ""

    def test_the_fingerprint_tracks_content(self, rclone_conf):
        before = conf.config_fingerprint()
        assert len(before) == 64
        rclone_conf.write_text(SAMPLE + "\n[extra]\ntype = local\n", encoding="utf-8")
        assert conf.config_fingerprint() != before

    def test_the_fingerprint_contains_no_secret(self, rclone_conf):
        digest = conf.config_fingerprint()
        assert "refresh_token" not in digest
        assert "M.C523" not in digest


# ═════════════════════════════════════════════════════════════════════════════
# Redaction — invariant I14
# ═════════════════════════════════════════════════════════════════════════════

class TestRedactedDump:
    def test_the_token_never_appears(self, rclone_conf):
        """config/dump and config/get return it in the clear; this is the only
        thing that may reach a diagnostics bundle."""
        dumped = conf.redacted_dump()
        assert "refresh_token" not in dumped
        assert "M.C523_BAY" not in dumped
        assert "EwBIA8l6BAAUs5" not in dumped

    def test_it_matches_what_rclone_config_redacted_produces(self, monkeypatch,
                                                             tmp_path):
        """Verified against the real binary on this machine: for a `[onedrive]`
        section it replaces token, drive_id and client_secret, and leaves type
        and drive_type alone."""
        path = tmp_path / "rclone.conf"
        path.write_text(
            "[demo]\ntype = onedrive\ntoken = {\"a\":1}\ndrive_id = b!abc\n"
            "drive_type = personal\nclient_secret = shhh\n", encoding="utf-8")
        monkeypatch.setenv("RCLONE_CONFIG", str(path))
        assert conf.redacted_dump() == (
            "[demo]\n"
            "type = onedrive\n"
            "token = XXX\n"
            "drive_id = XXX\n"
            "drive_type = personal\n"
            "client_secret = XXX\n"
            "\n"
            "### Double check the config for sensitive info before posting publicly\n"
        )

    def test_an_empty_config_still_produces_the_warning_footer(self, monkeypatch,
                                                              tmp_path):
        monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "nope.conf"))
        assert conf.REDACTED_FOOTER in conf.redacted_dump()

    def test_the_sensitive_set_is_rclone_s_own(self):
        """97 names, read out of `config/providers` on rclone v1.75.0."""
        assert len(conf.SENSITIVE_KEYS) == 97
        for name in ("token", "refresh_token", "access_token", "client_secret",
                     "drive_id", "pass", "password", "link_password"):
            assert name in conf.SENSITIVE_KEYS
        for name in ("type", "drive_type", "chunk_size", "delta"):
            assert name not in conf.SENSITIVE_KEYS

    def test_a_redacted_dump_is_safe_to_bundle(self, rclone_conf):
        from onedriveui.rc import guards

        guards.assert_bundle_safe(["rclone-redacted.conf"])
        assert conf.REDACTION == "XXX"


# ═════════════════════════════════════════════════════════════════════════════
# Writing — invariant I1's only escape hatch
# ═════════════════════════════════════════════════════════════════════════════

class TestSetBackendOptions:
    def test_the_token_line_is_byte_identical_afterwards(self, rclone_conf):
        """Acceptance. This is why the rewrite is line-based rather than a
        configparser round trip."""
        before = rclone_conf.read_bytes()
        conf.set_backend_options("onedrive", {"chunk_size": "10M", "delta": True})
        after = rclone_conf.read_text(encoding="utf-8")
        assert TOKEN_LINE in after
        assert after.count(TOKEN_LINE) == before.decode().count(TOKEN_LINE) == 2
        assert _line(rclone_conf, "token = ") == TOKEN_LINE

    def test_a_new_option_round_trips(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert conf.read()["onedrive"]["chunk_size"] == "10M"

    def test_an_existing_option_is_replaced_in_place(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        conf.set_backend_options("onedrive", {"chunk_size": "20M"})
        text = rclone_conf.read_text(encoding="utf-8")
        assert text.count("chunk_size") == 1
        assert conf.read()["onedrive"]["chunk_size"] == "20M"

    def test_booleans_render_the_way_rclone_writes_them(self, rclone_conf):
        conf.set_backend_options("onedrive", {"delta": True, "no_versions": False})
        assert _line(rclone_conf, "delta = ") == "delta = true"
        assert _line(rclone_conf, "no_versions = ") == "no_versions = false"

    def test_none_removes_an_option(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        conf.set_backend_options("onedrive", {"chunk_size": None})
        assert "chunk_size" not in conf.read()["onedrive"]
        assert TOKEN_LINE in rclone_conf.read_text(encoding="utf-8")

    def test_removing_something_that_is_not_there_is_a_no_op(self, rclone_conf):
        before = rclone_conf.read_text(encoding="utf-8")
        conf.set_backend_options("onedrive", {"never_set": None})
        assert rclone_conf.read_text(encoding="utf-8") == before

    def test_the_other_section_is_untouched(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert conf.read()["work"] == {
            "type": "onedrive", "drive_type": "business",
            "token": TOKEN_LINE.split(" = ", 1)[1],
        }

    def test_comments_and_blank_lines_survive(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        text = rclone_conf.read_text(encoding="utf-8")
        assert text.startswith("# rclone configuration, hand-edited once\n")
        assert "\n\n[work]" in text

    def test_the_new_option_lands_inside_its_own_section(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        text = rclone_conf.read_text(encoding="utf-8")
        assert text.index("chunk_size") < text.index("[work]")

    def test_the_file_mode_is_preserved(self, rclone_conf):
        """rclone creates it 0600; widening it would publish the refresh token."""
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert stat.S_IMODE(rclone_conf.stat().st_mode) == 0o600

    def test_an_unusual_but_deliberate_mode_is_preserved_too(self, rclone_conf):
        rclone_conf.chmod(0o640)
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert stat.S_IMODE(rclone_conf.stat().st_mode) == 0o640

    def test_the_write_leaves_no_temporary_file_behind(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert [p.name for p in rclone_conf.parent.iterdir()] == ["rclone.conf"]

    def test_the_trailing_newline_is_preserved(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert rclone_conf.read_text(encoding="utf-8").endswith("\n")

    def test_an_unknown_remote_is_refused(self, rclone_conf):
        with pytest.raises(ConfigError, match="not in the rclone config"):
            conf.set_backend_options("nosuchremote", {"chunk_size": "10M"})

    def test_an_empty_config_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "nope.conf"))
        with pytest.raises(ConfigError, match="empty or missing"):
            conf.set_backend_options("onedrive", {"chunk_size": "10M"})

    def test_an_empty_option_mapping_is_a_no_op(self, rclone_conf):
        before = rclone_conf.read_text(encoding="utf-8")
        conf.set_backend_options("onedrive", {})
        assert rclone_conf.read_text(encoding="utf-8") == before

    def test_the_key_case_does_not_matter(self, rclone_conf):
        conf.set_backend_options("onedrive", {"Chunk_Size": "10M"})
        assert conf.read()["onedrive"]["chunk_size"] == "10M"


class TestChunkSizeValidation:
    """Graph requires a resumable-upload chunk that is a multiple of 320 KiB."""

    def test_the_recommended_value_is_accepted(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert 10 * 1024 ** 2 % ONEDRIVE_CHUNK_MULTIPLE == 0

    @pytest.mark.parametrize("bad", ["10000000", "1M", "7M", "0", "off"])
    def test_a_non_multiple_or_a_disabled_value_is_refused(self, rclone_conf, bad):
        """1 MiB is 1 048 576, which 327 680 does not divide; "off" (-1) is not a
        chunk size at all."""
        with pytest.raises(ConfigError, match="320 KiB"):
            conf.set_backend_options("onedrive", {"chunk_size": bad})

    def test_nonsense_is_refused(self, rclone_conf):
        with pytest.raises(ConfigError):
            conf.set_backend_options("onedrive", {"chunk_size": "banana"})

    def test_binary_suffixes_are_accepted(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10Mi"})
        assert conf.read()["onedrive"]["chunk_size"] == "10Mi"


class TestInvariantI9:
    """A Personal drive cannot delete versions and does not implement
    permanentDelete, so these two must never be turned on there."""

    @pytest.mark.parametrize("option", ["no_versions", "hard_delete"])
    def test_turning_it_on_for_a_personal_drive_is_refused(self, rclone_conf,
                                                           option):
        with pytest.raises(SafetyRefusal) as excinfo:
            conf.set_backend_options("onedrive", {option: True})
        assert excinfo.value.invariant == "I9"
        assert "personal" in str(excinfo.value)

    @pytest.mark.parametrize("option", ["no_versions", "hard_delete"])
    def test_turning_it_off_is_always_fine(self, rclone_conf, option):
        conf.set_backend_options("onedrive", {option: False})
        assert conf.read()["onedrive"][option] == "false"

    def test_a_string_true_is_caught_too(self, rclone_conf):
        with pytest.raises(SafetyRefusal):
            conf.set_backend_options("onedrive", {"no_versions": "true"})

    def test_a_business_drive_may_set_them(self, rclone_conf):
        conf.set_backend_options("work", {"hard_delete": True})
        assert conf.read()["work"]["hard_delete"] == "true"

    def test_nothing_else_in_the_file_moved(self, rclone_conf):
        conf.set_backend_options("work", {"hard_delete": True})
        assert _line(rclone_conf, "token = ") == TOKEN_LINE


# ═════════════════════════════════════════════════════════════════════════════
# The recommended set
# ═════════════════════════════════════════════════════════════════════════════

class TestRecommendedBackendOptions:
    def test_the_personal_recommendation_is_the_specified_one(self):
        """Acceptance: no_versions=false, hard_delete=false, delta=true,
        chunk_size=10M."""
        assert conf.recommended_backend_options("personal") == {
            "chunk_size": "10M",
            "delta": "true",
            "no_versions": "false",
            "hard_delete": "false",
        }

    def test_an_account_kind_enum_works_too(self):
        assert (conf.recommended_backend_options(AccountKind.PERSONAL)
                == conf.recommended_backend_options("personal"))

    def test_business_gets_the_same_safe_baseline(self):
        assert (conf.recommended_backend_options("business")
                == conf.recommended_backend_options("personal"))

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            conf.recommended_backend_options("enterprise")

    def test_the_recommendation_applies_cleanly_to_a_personal_remote(self,
                                                                     rclone_conf):
        """It must not trip I9 on the very drive type it is recommended for."""
        conf.set_backend_options("onedrive",
                                 conf.recommended_backend_options("personal"))
        section = conf.read()["onedrive"]
        assert section["chunk_size"] == "10M"
        assert section["delta"] == "true"
        assert section["no_versions"] == "false"
        assert section["hard_delete"] == "false"
        assert _line(rclone_conf, "token = ") == TOKEN_LINE


class TestMissingOptions:
    def test_it_reports_only_what_would_change(self, rclone_conf):
        wanted = conf.recommended_backend_options("personal")
        assert conf.missing_options("onedrive", wanted) == wanted
        conf.set_backend_options("onedrive", wanted)
        assert conf.missing_options("onedrive", wanted) == {}

    def test_a_changed_value_is_reported(self, rclone_conf):
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert conf.missing_options("onedrive", {"chunk_size": "20M"}) == {
            "chunk_size": "20M"}

    def test_a_removal_is_reported_only_when_the_key_exists(self, rclone_conf):
        assert conf.missing_options("onedrive", {"chunk_size": None}) == {}
        conf.set_backend_options("onedrive", {"chunk_size": "10M"})
        assert conf.missing_options("onedrive", {"chunk_size": None}) == {
            "chunk_size": None}

    def test_booleans_compare_after_rendering(self, rclone_conf):
        conf.set_backend_options("onedrive", {"delta": True})
        assert conf.missing_options("onedrive", {"delta": True}) == {}


# ═════════════════════════════════════════════════════════════════════════════
# Invariant I1 in the environment
# ═════════════════════════════════════════════════════════════════════════════

class TestNoBackendEnv:
    def test_a_backend_option_in_the_environment_is_refused(self):
        """`RCLONE_ONEDRIVE_CHUNK_SIZE` produces the identical `{HASH}` rename a
        `--onedrive-chunk-size` flag does."""
        with pytest.raises(SafetyRefusal) as excinfo:
            conf.assert_no_backend_env(["RCLONE_ONEDRIVE_CHUNK_SIZE"])
        assert excinfo.value.invariant == "I1"

    def test_a_mapping_works_as_well_as_a_list(self):
        with pytest.raises(SafetyRefusal):
            conf.assert_no_backend_env({"RCLONE_DRIVE_CHUNK_SIZE": "8M"})

    @pytest.mark.parametrize("name", [
        "RCLONE_CONFIG", "RCLONE_TRANSFERS", "HOME", "XDG_RUNTIME_DIR",
    ])
    def test_ordinary_variables_pass(self, name):
        conf.assert_no_backend_env([name])

    @pytest.mark.parametrize("name", ["RCLONE_CACHE_DIR", "RCLONE_HTTP_PROXY"])
    def test_the_two_global_lookalikes_pass(self, name):
        """The env twins of `--cache-dir` and `--http-proxy`, the only two global
        flags in rclone v1.75.0 whose names start with a backend prefix."""
        conf.assert_no_backend_env([name])
