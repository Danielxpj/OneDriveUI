"""applog.py — rotation, the ring buffer, redaction and the bundle.

Invariant I14 is the whole point: an OAuth refresh token is a durable
credential for the user's entire OneDrive, and rclone prints it in the clear
from `config show`, `config/dump` and `config/get`. Nothing this module writes
may contain one, and a diagnostics bundle must not even contain the STRING
`refresh_token` — a bundle that did would both advertise what to grep for and
prove the pattern had matched only half of what it should.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path

import pytest

from onedriveui import applog, paths

#: A real-shaped rclone.conf token line. The values are structurally identical
#: to a live one (see docs/research/rclone-onedrive-backend.md §2.3) and are
#: fabricated.
TOKEN_LINE = (
    'token = {"access_token":"EwBYA8l6BAAUs5-HQn0N_9SnFwKGr3zGmQIAAAeHy'
    'kDL8Zb0","token_type":"Bearer","refresh_token":"M.C555_BAY.0.U.-Cl9'
    'abcdefghijklmnop","expiry":"2026-08-31T13:00:00.000000000Z",'
    '"expires_in":3600}')

SECRETS = ("EwBYA8l6", "M.C555_BAY", "refresh_token", "access_token")


@pytest.fixture(autouse=True)
def _clean_logging():
    """Never leave handlers attached to the shared `onedriveui` logger."""
    applog.uninstall()
    applog.RING.clear()
    yield
    applog.uninstall()
    applog.RING.clear()


# ═════════════════════════════════════════════════════════════════════════════
# redact
# ═════════════════════════════════════════════════════════════════════════════

def test_redact_removes_a_real_shaped_oauth_token():
    """BUILD_PLAN acceptance."""
    cleaned = applog.redact(f"loaded remote: {TOKEN_LINE}")
    for secret in SECRETS:
        assert secret not in cleaned
    assert applog.REDACTED in cleaned


def test_redact_removes_the_key_name_not_just_the_value():
    """`"refresh_token": "[redacted]"` would still leak what the file holds."""
    cleaned = applog.redact('{"refresh_token": "M.C555_BAY.0.U.-Cl9"}')
    assert "refresh_token" not in cleaned
    assert "M.C555" not in cleaned


@pytest.mark.parametrize(("line", "secret"), [
    ("GET /auth?state=Nq8vXyZ123abc HTTP/1.1", "Nq8vXyZ123abc"),
    ("Authorization: Basic b25lZHJpdmV1aTpzM2NyZXQ=", "b25lZHJpdmV1aTpzM2NyZXQ="),
    ("Authorization: Bearer EwBYA8l6BAAU", "EwBYA8l6BAAU"),
    ("rclone rcd --rc-user u --rc-pass hunter2GENERATED", "hunter2GENERATED"),
    ('{"rc-pass": "s3cret-generated"}', "s3cret-generated"),
    ('{"pass": "sekrit"}', "sekrit"),
    ("https://login.live.com/oauth?code=M.R3_BAY.abcdef", "M.R3_BAY.abcdef"),
    ('{"client_secret": "abc123def"}', "abc123def"),
])
def test_redact_covers_every_documented_pattern(line, secret):
    assert secret not in applog.redact(line)


def test_redact_leaves_ordinary_lines_alone():
    for line in ("INFO  Transferred: 12 / 12, 100%",
                 "vfs cache: cleaned: objects 3 (was 3)",
                 "mounting onedrive: at /home/u/OneDrive",
                 "Documents/Report.docx: uploaded 4.8 MB"):
        assert applog.redact(line) == line


def test_redact_of_empty_is_empty():
    assert applog.redact("") == ""
    assert applog.redact(None) is None


def test_redact_patterns_are_compiled_and_documented():
    assert applog.REDACT_PATTERNS
    for pattern, replacement in applog.REDACT_PATTERNS:
        assert isinstance(pattern, re.Pattern)
        assert "refresh_token" not in replacement


# ═════════════════════════════════════════════════════════════════════════════
# The ring buffer
# ═════════════════════════════════════════════════════════════════════════════

def test_ring_buffer_keeps_the_last_500_lines():
    assert applog.RING_CAPACITY == 500
    ring = applog.RingBuffer(emit_signal=False)
    logger = logging.getLogger("test.ring")
    logger.handlers = [ring]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for n in range(600):
        logger.info("line %d", n)
    lines = ring.lines()
    assert len(lines) == 500
    assert "line 100" in lines[0]
    assert "line 599" in lines[-1]
    assert len(ring) == 500


def test_ring_buffer_redacts_before_storing():
    ring = applog.RingBuffer(emit_signal=False)
    logger = logging.getLogger("test.ring.redact")
    logger.handlers = [ring]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("config: %s", TOKEN_LINE)
    stored = ring.text()
    for secret in SECRETS:
        assert secret not in stored


def test_ring_buffer_limit_takes_from_the_end():
    ring = applog.RingBuffer(capacity=10, emit_signal=False)
    logger = logging.getLogger("test.ring.limit")
    logger.handlers = [ring]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for n in range(10):
        logger.info("n=%d", n)
    assert len(ring.lines(3)) == 3
    assert "n=9" in ring.lines(3)[-1]
    assert ring.lines(0) == []
    assert len(ring.lines()) == 10


def test_ring_buffer_emits_log_line_on_the_bus(bus_spy, qapp):
    bus_spy.watch("log_line")
    applog.install(stderr=False)
    applog.get_logger("test").warning("something happened")
    assert bus_spy.count("log_line") >= 1
    assert "something happened" in bus_spy.last("log_line")[0]


def test_ring_buffer_clear():
    ring = applog.RingBuffer(emit_signal=False)
    logger = logging.getLogger("test.ring.clear")
    logger.handlers = [ring]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("x")
    assert len(ring) == 1
    ring.clear()
    assert len(ring) == 0
    assert list(ring) == []


# ═════════════════════════════════════════════════════════════════════════════
# install
# ═════════════════════════════════════════════════════════════════════════════

def test_install_writes_a_rotating_file_at_0600():
    applog.install("DEBUG", stderr=False)
    applog.get_logger("rc.client").info("hello")
    log = paths.log_file()
    assert log.exists()
    assert log.stat().st_mode & 0o777 == 0o600
    assert "hello" in log.read_text(encoding="utf-8")
    assert applog.is_installed()


def test_install_rotates_at_five_megabytes_times_five():
    assert applog.LOG_MAX_BYTES == 5 * 1024 * 1024
    assert applog.LOG_BACKUP_COUNT == 5
    applog.install("INFO", stderr=False, max_bytes=2048, backup_count=2)
    logger = applog.get_logger("rotate")
    for n in range(400):
        logger.info("padding line %04d %s", n, "x" * 100)
    files = sorted(p.name for p in paths.log_dir().iterdir())
    assert "app.log" in files
    assert "app.log.1" in files
    assert "app.log.3" not in files      # backupCount is honoured


def test_install_is_idempotent_and_does_not_duplicate_lines():
    applog.install(stderr=False)
    applog.install(stderr=False)
    applog.install(stderr=False)
    applog.get_logger("dup").warning("only once")
    text = paths.log_file().read_text(encoding="utf-8")
    assert text.count("only once") == 1


def test_the_log_file_is_redacted():
    applog.install(stderr=False)
    applog.get_logger("auth").info("rclone.conf: %s", TOKEN_LINE)
    text = paths.log_file().read_text(encoding="utf-8")
    for secret in SECRETS:
        assert secret not in text


def test_install_survives_an_unwritable_log_directory(tmp_path, monkeypatch):
    """Losing the log must never stop the sync client."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        logger = applog.install(stderr=False, log_file=blocked / "app.log")
        logger.info("still logging")            # must not raise
        assert applog.RING.text().count("still logging") >= 1
    finally:
        blocked.chmod(0o700)


def test_get_logger_namespaces_under_the_app_root():
    assert applog.get_logger("rc.client").name == "onedriveui.rc.client"
    assert applog.get_logger("").name == "onedriveui"
    assert applog.get_logger("onedriveui.data.db").name == "onedriveui.data.db"


def test_records_do_not_escape_to_the_python_root_logger():
    """A third-party basicConfig must not get an unredacted copy."""
    applog.install(stderr=False)
    assert logging.getLogger(applog.ROOT_NAME).propagate is False


def test_set_level_changes_the_threshold():
    applog.install("WARNING", stderr=False)
    applog.get_logger("lvl").info("suppressed")
    applog.set_level("DEBUG")
    applog.get_logger("lvl").debug("shown")
    text = paths.log_file().read_text(encoding="utf-8")
    assert "suppressed" not in text
    assert "shown" in text


def test_an_unknown_level_name_falls_back_to_info():
    applog.install("NONSENSE", stderr=False)
    assert logging.getLogger(applog.ROOT_NAME).level == logging.INFO


def test_uninstall_detaches_everything():
    applog.install(stderr=False)
    applog.uninstall()
    assert applog.is_installed() is False
    assert logging.getLogger(applog.ROOT_NAME).handlers == []


# ═════════════════════════════════════════════════════════════════════════════
# The diagnostics bundle
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def dirty_fixture(tmp_config):
    """A config dir and a log both polluted with a real-shaped token."""
    applog.install(stderr=False)
    applog.get_logger("auth").info("rclone.conf says: %s", TOKEN_LINE)
    # A hand-written config.json containing a secret it should never hold.
    data = tmp_config.reload()
    data["accounts"][0]["backend"]["link_password"] = "s3cret"
    data["accounts"][0]["_leaked"] = TOKEN_LINE
    tmp_config.write(data)
    # A log file written before install() ran, therefore never redacted.
    (paths.log_dir() / "app.log.1").write_text(
        f"pre-install line\n{TOKEN_LINE}\n", encoding="utf-8")
    return tmp_config


def test_a_bundle_contains_zero_occurrences_of_refresh_token(dirty_fixture,
                                                             tmp_path):
    """BUILD_PLAN acceptance, over every member of the archive."""
    bundle = applog.build_diagnostics_bundle(
        tmp_path / "diag.zip", rclone_path="/nonexistent/rclone")
    text = applog.bundle_text(bundle)
    assert "refresh_token" not in text
    for secret in SECRETS:
        assert secret not in text


def test_a_bundle_redacts_a_log_file_written_before_install(dirty_fixture,
                                                            tmp_path):
    bundle = applog.build_diagnostics_bundle(
        tmp_path / "diag.zip", rclone_path="/nonexistent/rclone")
    with zipfile.ZipFile(bundle) as archive:
        rotated = archive.read("logs/app.log.1").decode("utf-8")
    assert "M.C555_BAY" not in rotated
    assert "pre-install line" in rotated


def test_a_bundle_has_the_expected_members(dirty_fixture, tmp_path):
    bundle = applog.build_diagnostics_bundle(
        tmp_path / "diag.zip", rclone_path="/nonexistent/rclone")
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"report.txt", "recent.log", "config.json", "manifest.json",
            "rclone-config-redacted.ini", "rclone-version.txt"} <= names
    assert any(n.startswith("logs/") for n in names)
    assert "report.txt" in manifest["members"]
    assert any("I14" in reason for reason in manifest["excluded"])


def test_a_bundle_never_embeds_endpoints_json_or_rclone_conf(dirty_fixture,
                                                             tmp_path):
    """endpoints.json holds the rc password; rclone.conf holds the token (I14)."""
    paths.endpoints_file().write_text(
        json.dumps({"rcd": {"port": 17801, "pass": "generated-secret"}}),
        encoding="utf-8")
    bundle = applog.build_diagnostics_bundle(
        tmp_path / "diag.zip", rclone_path="/nonexistent/rclone")
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
    assert not any("endpoints" in n for n in names)
    assert not any("rclone.conf" in n for n in names)
    assert "generated-secret" not in applog.bundle_text(bundle)


def test_a_bundle_defaults_into_the_cache_dir(dirty_fixture):
    bundle = applog.build_diagnostics_bundle(rclone_path="/nonexistent/rclone")
    assert bundle.parent == paths.cache_dir()
    assert bundle.name.startswith("diagnostics-")
    assert bundle.suffix == ".zip"
    assert bundle.stat().st_mode & 0o777 == 0o600


def test_the_bundle_uses_config_redacted_and_never_config_dump():
    """I14, asserted against the source rather than a mock."""
    source = Path(applog.__file__).read_text(encoding="utf-8")
    assert '"config", "redacted"' in source
    # The banned endpoints appear only inside prose, never as a call. Every
    # spelling that could reach a subprocess or an rc POST is checked.
    for banned in ('"config/dump"', "'config/dump'",
                   '"config/get"', "'config/get'",
                   '"config", "show"', '"config", "dump"',
                   '"config", "get"', '"show"'):
        assert banned not in source


def test_rclone_config_redacted_reports_a_missing_binary_instead_of_raising():
    out = applog.rclone_config_redacted("/nonexistent/rclone")
    assert "failed" in out
    assert applog.REDACTED not in out       # nothing to redact in an error


def test_rclone_config_redacted_redacts_whatever_rclone_returned(monkeypatch):
    """Belt and braces: a future rclone that redacts one fewer field."""
    class FakeCompleted:
        returncode = 0
        stdout = f"[onedrive]\ntype = onedrive\n{TOKEN_LINE}\n"
        stderr = ""

    monkeypatch.setattr(applog.subprocess, "run",
                        lambda *a, **k: FakeCompleted())
    out = applog.rclone_config_redacted("/usr/bin/rclone")
    for secret in SECRETS:
        assert secret not in out
    assert "[onedrive]" in out


def test_a_bundle_can_be_built_with_nothing_included(tmp_path):
    bundle = applog.build_diagnostics_bundle(
        tmp_path / "minimal.zip", include_logs=False, include_config=False,
        include_rclone=False)
    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {"report.txt", "recent.log",
                                           "manifest.json"}


def test_the_report_names_the_live_fuse_mounts(tmp_path):
    bundle = applog.build_diagnostics_bundle(
        tmp_path / "diag.zip", include_logs=False, include_config=False,
        include_rclone=False)
    with zipfile.ZipFile(bundle) as archive:
        report = archive.read("report.txt").decode("utf-8")
    from onedriveui import __version__
    assert __version__ in report
    assert "fuse.rclone mounts" in report
    assert str(paths.db_file()) in report


def test_the_bundle_embeds_the_recent_ring_lines(tmp_path):
    applog.install(stderr=False)
    logger = applog.get_logger("bundle")
    for n in range(10):
        logger.warning("event %d", n)
    bundle = applog.build_diagnostics_bundle(
        tmp_path / "diag.zip", include_logs=False, include_config=False,
        include_rclone=False)
    with zipfile.ZipFile(bundle) as archive:
        recent = archive.read("recent.log").decode("utf-8")
    assert "event 9" in recent
    assert applog.BUNDLE_LOG_LINES == 200
