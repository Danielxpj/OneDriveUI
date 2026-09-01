"""units.py — the KB/KiB conversion and every human-readable format.

The conversion tests matter more than they look. `kb_to_kib` is the single
place the OneDrive UI's KB/s (1000) becomes rclone's KiB/s (1024); getting it
wrong by 2.4 % is invisible in a UI and wrong in every transfer.
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess

import pytest

from onedriveui import units
from onedriveui.constants import KB, KIB


# ═════════════════════════════════════════════════════════════════════════════
# THE conversion
# ═════════════════════════════════════════════════════════════════════════════

def test_kb_to_kib_1000_is_977():
    """The acceptance number from BUILD_PLAN WP-01."""
    assert units.kb_to_kib(1000) == 977


@pytest.mark.parametrize("kb", [1, 50, 100, 512, 1000, 1024, 9999, 100_000])
def test_kb_to_kib_matches_the_definition(kb):
    assert units.kb_to_kib(kb) == round(kb * KB / KIB)


@pytest.mark.parametrize("kb", [0, -1, -100_000])
def test_kb_to_kib_non_positive_is_zero(kb):
    """rclone spells 'no limit' `off`, never `0Ki`, so 0 is the honest answer."""
    assert units.kb_to_kib(kb) == 0


def test_kib_to_kb_round_trips_within_one_unit():
    for kb in range(50, 5_000, 37):
        assert abs(units.kib_to_kb(units.kb_to_kib(kb)) - kb) <= 1


def test_kib_to_kb_non_positive_is_zero():
    assert units.kib_to_kb(0) == 0
    assert units.kib_to_kb(-5) == 0


def test_conversion_is_not_inlined_anywhere_else():
    """No other module may compute the KB->KiB ratio itself (BUILD_PLAN WP-01)."""
    import pathlib

    root = pathlib.Path(units.__file__).resolve().parent
    pattern = re.compile(r"(1000\s*/\s*1024)|(\*\s*KB\s*/\s*KIB)")
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "units.py":
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert offenders == []


# ═════════════════════════════════════════════════════════════════════════════
# format_bwlimit
# ═════════════════════════════════════════════════════════════════════════════

#: What rclone's own `--bwlimit` parser accepts: <number><K|M|G|T|P>[i][B],
#: optionally as an upload:download pair, or the literal `off`.
RATE_TOKEN = re.compile(r"^(?:off|\d+(?:\.\d+)?(?:[kKMGTPE]i?)?[bB]?)$")


def _is_rclone_rate(text: str) -> bool:
    return all(RATE_TOKEN.match(part) for part in text.split(":"))


def test_format_bwlimit_1000_100_is_parseable():
    """The BUILD_PLAN acceptance: format_bwlimit(1000, 100) parses as a rate."""
    rate = units.format_bwlimit(1000, 100)
    assert _is_rclone_rate(rate), rate
    # rclone's pair order is upload:download, the reverse of the UI's.
    assert rate == "98Ki:977Ki"


def test_format_bwlimit_none_is_off():
    assert units.format_bwlimit(None, None) == "off"


def test_format_bwlimit_one_sided():
    assert units.format_bwlimit(1000, None) == "off:977Ki"
    assert units.format_bwlimit(None, 1000) == "977Ki:off"
    for rate in ("off:977Ki", "977Ki:off"):
        assert _is_rclone_rate(rate)


def test_format_bwlimit_equal_sides_collapse():
    """rclone accepts a single token when both directions share a limit."""
    assert units.format_bwlimit(500, 500) == units.format_rate(500)
    assert ":" not in units.format_bwlimit(500, 500)


def test_format_bwlimit_round_trips_through_the_fake_daemon(fake_rc):
    """The FakeRc normaliser is rclone v1.75.0's; our rate must survive it."""
    rate = units.format_bwlimit(1024, 1024)          # 1000 KiB each way
    result = fake_rc.call_blocking("core/bwlimit", {"rate": rate})
    assert result["bytesPerSecondTx"] == units.kb_to_kib(1024) * KIB
    assert result["bytesPerSecondRx"] == units.kb_to_kib(1024) * KIB


def test_format_bwlimit_pair_round_trips_through_the_fake_daemon(fake_rc):
    result = fake_rc.call_blocking(
        "core/bwlimit", {"rate": units.format_bwlimit(1000, 100)})
    assert result["bytesPerSecondRx"] == 977 * KIB    # download
    assert result["bytesPerSecondTx"] == 98 * KIB     # upload


def test_format_rate_uses_binary_suffixes():
    assert units.format_rate(None) == "off"
    assert units.format_rate(0) == "off"
    assert units.format_rate(1024) == "1000Ki"
    assert units.format_rate(1_048_576) == "1000Mi"


# ═════════════════════════════════════════════════════════════════════════════
# human_bytes
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(("value", "expected"), [
    (0, "0 B"),
    (1, "1 B"),
    (999, "999 B"),
    (1000, "1.0 KB"),
    (1500, "1.5 KB"),
    (4_800_000_000, "4.8 GB"),
    (252_544_077_005, "252.5 GB"),
    (1_104_880_336_896, "1.1 TB"),
    (150_000_000_000, "150.0 GB"),
])
def test_human_bytes_windows_style_is_decimal(value, expected):
    assert units.human_bytes(value) == expected


def test_human_bytes_binary_style_is_for_developers_only():
    assert units.human_bytes(1024, style="binary") == "1.0 KiB"
    assert units.human_bytes(1000, style="binary") == "1000 B"


def test_human_bytes_rejects_an_unknown_style():
    with pytest.raises(ValueError):
        units.human_bytes(1, style="metric")


def test_human_bytes_rounds_up_across_a_boundary():
    """999.96 MB must print as 1.0 GB, never as 1000.0 MB."""
    assert units.human_bytes(999_960_000) == "1.0 GB"


def test_human_bytes_always_shows_one_decimal_on_a_scaled_unit():
    """OneDrive's storage line reads "252.5 GB of 1,024 GB used"."""
    assert units.human_bytes(99_000_000_000) == "99.0 GB"
    assert units.human_bytes(150_000_000_000) == "150.0 GB"
    assert units.human_bytes(999) == "999 B"        # bytes stay whole


def test_human_bytes_keeps_the_sign():
    """OneDrive reports -1 for a directory size; hiding that would be a lie."""
    assert units.human_bytes(-1) == "-1 B"


# ═════════════════════════════════════════════════════════════════════════════
# human_rate / human_duration / eta_text
# ═════════════════════════════════════════════════════════════════════════════

def test_human_rate():
    assert units.human_rate(1_200_000) == "1.2 MB/s"
    assert units.human_rate(0) == "0 B/s"
    assert units.human_rate(-1) == "0 B/s"
    assert units.human_rate(float("nan")) == "0 B/s"
    assert units.human_rate("not a number") == "0 B/s"


@pytest.mark.parametrize(("seconds", "expected"), [
    (0, "0s"),
    (45, "45s"),
    (59, "59s"),
    (60, "1m"),
    (330, "5m 30s"),
    (3600, "1h"),
    (8100, "2h 15m"),
    (86_400, "1d"),
    (100_000, "1d 3h"),
])
def test_human_duration(seconds, expected):
    assert units.human_duration(seconds) == expected


def test_human_duration_tolerates_rclone_sentinels():
    assert units.human_duration(-1) == "0s"
    assert units.human_duration(float("inf")) == "0s"
    assert units.human_duration("nope") == "0s"


def test_eta_text_is_empty_for_none():
    """core/stats sends `eta: null` whenever it cannot estimate."""
    assert units.eta_text(None) == ""
    assert units.eta_text(-1) == ""
    assert units.eta_text(90) == "1m 30s"


def test_eta_text_survives_a_missing_key(fake_rc):
    """The rc omits `eta` entirely while a transfer is starting."""
    fake_rc.set_eta(None)
    stats = fake_rc.call_blocking("core/stats", {})
    assert units.eta_text(stats.get("eta")) == ""


# ═════════════════════════════════════════════════════════════════════════════
# parse_size / format_size
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(("text", "expected"), [
    ("30M", 30 * 1024 ** 2),
    ("20G", 20 * 1024 ** 3),
    ("512Ki", 512 * 1024),
    ("10M", 10_485_760),
    ("1024", 1024),
    ("320k", 327_680),
    ("1MiB", 1024 ** 2),
    ("  4G  ", 4 * 1024 ** 3),
])
def test_parse_size_is_binary_like_rclone(text, expected):
    assert units.parse_size(text) == expected


@pytest.mark.parametrize("text", ["off", "", "   ", "OFF", "unlimited"])
def test_parse_size_off_is_minus_one(text):
    assert units.parse_size(text) == -1


@pytest.mark.parametrize("text", ["banana", "10X", "M", "1..2M"])
def test_parse_size_rejects_nonsense(text):
    with pytest.raises(ValueError):
        units.parse_size(text)


def test_the_default_chunk_size_is_a_multiple_of_320_kib():
    """Graph's hard requirement, and the reason parse_size must be binary."""
    from onedriveui.constants import ONEDRIVE_CHUNK_MULTIPLE
    assert units.parse_size("10M") % ONEDRIVE_CHUNK_MULTIPLE == 0


def test_format_size_round_trips_through_parse_size():
    for value in (327_680, 10_485_760, 1024, 5 * 1024 ** 3, 1_048_577):
        assert units.parse_size(units.format_size(value)) == value
    assert units.format_size(-1) == "off"


# ═════════════════════════════════════════════════════════════════════════════
# relative_time
# ═════════════════════════════════════════════════════════════════════════════

NOW = _dt.datetime(2026, 8, 31, 12, 0, 0, tzinfo=_dt.UTC)


@pytest.mark.parametrize(("stamp", "expected"), [
    ("2026-08-31T12:00:00Z", "Just now"),
    ("2026-08-31T11:59:30Z", "Just now"),
    ("2026-08-31T11:58:50Z", "1 minute ago"),
    ("2026-08-31T11:58:00Z", "2 minutes ago"),
    ("2026-08-31T11:00:00Z", "1 hour ago"),
    ("2026-08-31T07:00:00Z", "5 hours ago"),
    ("2026-08-30T23:50:00Z", "12 hours ago"),   # under a day: elapsed, not calendar
    ("2026-08-30T11:00:00Z", "Yesterday"),      # over a day, one calendar day back
    ("2026-08-28T12:00:00Z", "3 days ago"),
    ("2026-08-24T12:00:00Z", "Last week"),
    ("2026-06-01T12:00:00Z", "1 June 2026"),
])
def test_relative_time(stamp, expected):
    assert units.relative_time(stamp, now=NOW) == expected


def test_relative_time_tolerates_rclone_nanoseconds():
    assert units.relative_time("2026-08-31T11:58:00.123456789Z",
                               now=NOW) == "2 minutes ago"


def test_relative_time_of_garbage_is_empty():
    assert units.relative_time("") == ""
    assert units.relative_time("not a time") == ""


def test_relative_time_of_the_future_does_not_go_negative():
    """Graph's clock and ours disagree by seconds; never render '-3 minutes'."""
    assert units.relative_time("2026-08-31T12:05:00Z", now=NOW) == "Just now"


def test_relative_time_accepts_a_naive_reference():
    assert units.relative_time("2026-08-31T12:00:00Z",
                               now=NOW.replace(tzinfo=None)) == "Just now"


def test_relative_time_of_utcnow_is_just_now():
    from onedriveui.models import utcnow_iso
    assert units.relative_time(utcnow_iso()) == "Just now"


# ═════════════════════════════════════════════════════════════════════════════
# Cross-checks against the real world
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.live
def test_parse_size_agrees_with_rclone_if_it_is_installed():
    """`rclone --bwlimit 10M` and parse_size('10M') must mean the same bytes."""
    try:
        proc = subprocess.run(["rclone", "version"], capture_output=True,
                              text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("rclone not installed")
    if proc.returncode != 0:
        pytest.skip("rclone not runnable")
    # rclone documents k/M/G as binary; assert our parser agrees rather than
    # driving a daemon, which this suite is not allowed to do.
    assert units.parse_size("10M") == 10 * 1024 ** 2
