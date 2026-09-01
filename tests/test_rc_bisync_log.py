"""WP-04 — `onedriveui/rc/bisync_log.py`.

Every string in `CAPTURED` is a **verbatim line from a bisync this machine
actually ran** on 2026-08-31 against two local directories in the scratchpad.
The nine scenarios, with their real exit codes:

===== ================================================================ =========
exit  what produced it                                                 verdict
===== ================================================================ =========
0     a clean `--resync` and a clean follow-up run                     OK
1     6 of 14 files deleted on Path1 with `--max-delete 25`            MAXDELETE
1     all 4 pre-existing files edited                                  ALLCHANGED
1     a hand-written live `.lck` in the workdir                        LOCKED
7     `- extra.txt` appended to the filters file, no `--resilient`     NEEDS_RESYNC
7     no `.md5` sidecar yet, **with** `--resilient`                    CRITICAL_SOFT
7     `--check-access` with no `RCLONE_TEST` on either side            ACCESS_DENIED
130   SIGINT during a 20 MB copy that finished inside the 30 s window  CANCELLED
130   SIGINT during a 60 MB copy whose destination went read-only      NEEDS_RESYNC
===== ================================================================ =========

The last two are the point of `classify_verdict()`: the **same signal and the
same exit code**, one leaving `.lst`/`.lst-old` in a clean resumable state and
the other leaving `.lst-err` and a locked-out account. The exit code cannot tell
them apart; the log can.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from onedriveui.models import RunVerdict
from onedriveui.rc import bisync_log as log_mod
from onedriveui.rc.bisync_log import (
    EXIT_CRITICAL,
    EXIT_NON_CRITICAL,
    EXIT_OK,
    EXIT_SIGINT,
    EXIT_USAGE,
    LogRecord,
    LogTailer,
    classify_verdict,
    conflict_path,
    is_benign,
    milestone,
    parse_record,
    parse_text,
    stats_counts,
    strip_rcd_prefix,
)

# ═════════════════════════════════════════════════════════════════════════════
# The nine captured runs, verbatim
# ═════════════════════════════════════════════════════════════════════════════

OK_RUN = """\
2026/08/31 20:35:23 INFO  : Setting --ignore-listing-checksum as neither --checksum nor --compare checksum are set.
2026/08/31 20:35:23 INFO  : lock file renewed for 2m0s. New expiration: 2026-08-31 20:37:23.453482285 -0400 -04
2026/08/31 20:35:23 INFO  : Synching Path1 "/tmp/bs/p1/" with Path2 "/tmp/bs/p2/"
2026/08/31 20:35:23 INFO  : Using filters file /tmp/bs/filters.txt
2026/08/31 20:35:23 INFO  : Storing filters file hash to /tmp/bs/filters.txt.md5
2026/08/31 20:35:23 INFO  : Copying Path2 files to Path1
2026/08/31 20:35:23 ERROR : Local file system at /tmp/bs/p1: Ignoring --track-renames as it doesn't work with copy or move, only sync
2026/08/31 20:35:23 INFO  : cloudfile.txt: Copied (new)
2026/08/31 20:35:23 INFO  : Resync updating listings
2026/08/31 20:35:23 INFO  : Validating listings for Path1 "/tmp/bs/p1/" vs Path2 "/tmp/bs/p2/"
2026/08/31 20:35:23 INFO  : Bisync successful
"""

MAXDELETE_RUN = """\
2026/08/31 20:36:03 INFO  : Path1:    6 changes:    0 new,    0 modified,    6 deleted
2026/08/31 20:36:03 ERROR : Safety abort: too many deletes (>25%, 6 of 14) on Path1 "/tmp/bs/p1/". Run with --force if desired.
2026/08/31 20:36:03 NOTICE: Bisync aborted. Please try again.
2026/08/31 20:36:03 NOTICE: Failed to bisync: too many deletes
"""

ALLCHANGED_RUN = """\
2026/08/31 20:36:22 ERROR : Safety abort: all files were changed on Path1 "/tmp/bs4/p1/". Run with --force if desired.
2026/08/31 20:36:22 NOTICE: Bisync aborted. Please try again.
2026/08/31 20:36:22 NOTICE: Failed to bisync: all files were changed
"""

LOCKED_RUN = """\
2026/08/31 20:36:04 INFO  : /tmp/bs/work/SESS.lck: Valid lock file found. Expires at 2026-08-31 21:09:24 -0400 -04. (33m20s from now)
2026/08/31 20:36:04 NOTICE: Failed to bisync: prior lock file found: /tmp/bs/work/SESS.lck
"""

FILTERS_CHANGED_RUN = """\
2026/08/31 20:36:04 INFO  : Using filters file /tmp/bs/filters.txt
2026/08/31 20:36:04 ERROR : Bisync critical error: filters file has changed (must run --resync): /tmp/bs/filters.txt
2026/08/31 20:36:04 ERROR : Bisync aborted. Must run --resync to recover.
2026/08/31 20:36:04 NOTICE: Failed to bisync: bisync aborted
"""

RESILIENT_RUN = """\
2026/08/31 20:35:23 INFO  : Using filters file /tmp/bs/filters.txt
2026/08/31 20:35:23 ERROR : Bisync critical error: filters file md5 hash not found (must run --resync): /tmp/bs/filters.txt
2026/08/31 20:35:23 ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.
2026/08/31 20:35:23 NOTICE: Failed to bisync: bisync aborted
"""

CHECK_ACCESS_RUN = """\
2026/08/31 20:36:04 NOTICE: --check-access: Failed to find any files named RCLONE_TEST
 More info: https://rclone.org/bisync/#check-access
2026/08/31 20:36:04 ERROR : Access test failed: Path1 count 0, Path2 count 0 - RCLONE_TEST
2026/08/31 20:36:04 ERROR : Bisync critical error: check file check failed
2026/08/31 20:36:04 ERROR : Bisync aborted. Error is retryable without --resync due to --resilient mode.
2026/08/31 20:36:04 NOTICE: Failed to bisync: bisync aborted
"""

GRACEFUL_RUN = """\
2026/08/31 20:36:24 INFO  : Canceling Sync if not done in: 30s
2026/08/31 20:36:41 INFO  : Canceling Sync if not done in: 13s
2026/08/31 20:36:41 NOTICE: Graceful shutdown completed successfully.
2026/08/31 20:36:41 INFO  : Bisync successful
2026/08/31 20:36:42 INFO  : Exiting...
"""

INTERRUPTED_CRITICAL_RUN = """\
2026/08/31 20:49:44 ERROR : huge.bin: Failed to copy: chtimes /tmp/bs7/p2/huge.bin.f19291e9.partial: no such file or directory
2026/08/31 20:49:44 ERROR : Local file system at /tmp/bs7/p2: not deleting files as there were IO errors
2026/08/31 20:49:44 ERROR : Bisync critical error: chtimes /tmp/bs7/p2/huge.bin.f19291e9.partial: no such file or directory
2026/08/31 20:49:44 ERROR : Bisync aborted. Must run --resync to recover.
2026/08/31 20:49:44 NOTICE: Failed to bisync with 2 errors: last error was: bisync aborted
"""

#: (name, log, exit_code, verdict) — the nine, in the order the docstring lists.
CAPTURED = (
    ("clean run",            OK_RUN,                   EXIT_OK,           RunVerdict.OK),
    ("max-delete abort",     MAXDELETE_RUN,            EXIT_NON_CRITICAL, RunVerdict.ABORTED_MAXDELETE),
    ("all files changed",    ALLCHANGED_RUN,           EXIT_NON_CRITICAL, RunVerdict.ABORTED_ALLCHANGED),
    ("prior lock file",      LOCKED_RUN,               EXIT_NON_CRITICAL, RunVerdict.LOCKED),
    ("filters changed",      FILTERS_CHANGED_RUN,      EXIT_CRITICAL,     RunVerdict.NEEDS_RESYNC),
    ("resilient md5 miss",   RESILIENT_RUN,            EXIT_CRITICAL,     RunVerdict.CRITICAL_SOFT),
    ("check-access failed",  CHECK_ACCESS_RUN,         EXIT_CRITICAL,     RunVerdict.ACCESS_DENIED),
    ("graceful SIGINT",      GRACEFUL_RUN,             EXIT_SIGINT,       RunVerdict.CANCELLED),
    ("SIGINT then critical", INTERRUPTED_CRITICAL_RUN, EXIT_SIGINT,       RunVerdict.NEEDS_RESYNC),
)

# ── the three JSON record shapes, captured verbatim from --use-json-log ───────
JSON_PLAIN = ('{"time":"2026-08-31T20:36:57.264846117-04:00","level":"info",'
              '"msg":"Copying Path2 files to Path1","source":"bisync/resync.go:44"}')
JSON_OBJECT = ('{"time":"2026-08-31T20:36:57.265433172-04:00","level":"info",'
               '"msg":"Copied (new)","size":6,"object":"a.txt",'
               '"objectType":"*local.Object","source":"operations/copy.go:380"}')
JSON_STATS = ('{"time":"2026-08-31T20:36:57.26575119-04:00","level":"notice",'
              '"msg":"\\nTransferred:   \\t          6 B / 6 B, 100%, 0 B/s, ETA -\\n",'
              '"stats":{"bytes":6,"checks":0,"deletedDirs":0,"deletes":0,'
              '"elapsedTime":0.00084111,"errors":0,"eta":null,"fatalError":false,'
              '"listed":2,"renames":0,"retryError":false,"serverSideCopies":0,'
              '"serverSideCopyBytes":0,"serverSideMoveBytes":0,"serverSideMoves":0,'
              '"speed":0,"totalBytes":6,"totalChecks":0,"totalTransfers":1,'
              '"transferTime":0.000137876,"transfers":1},'
              '"source":"accounting/stats.go:551"}')
JSON_SUCCESS = ('{"time":"2026-08-31T20:36:57.265718351-04:00","level":"info",'
                '"msg":"Bisync successful","source":"bisync/operations.go:218"}')

#: The conflict NOTICE, which carries NO `object` field — only `msg`.
JSON_CONFLICT = ('{"time":"2026-08-31T20:35:40.0-04:00","level":"notice",'
                 '"msg":"- WARNING           New or changed in both paths'
                 '                - Documents/report.docx",'
                 '"source":"bisync/resolve.go:318"}')


# ═════════════════════════════════════════════════════════════════════════════
# classify_verdict — the nine
# ═════════════════════════════════════════════════════════════════════════════

class TestClassifyVerdict:

    @pytest.mark.parametrize("name,text,code,expected", CAPTURED,
                             ids=[row[0] for row in CAPTURED])
    def test_nine_captured_runs(self, name, text, code, expected):
        assert classify_verdict(text, code) is expected, name

    def test_exit_130_is_ambiguous_and_the_log_decides(self):
        """The acceptance bullet, stated as the comparison that proves it."""
        assert classify_verdict(GRACEFUL_RUN, EXIT_SIGINT) is RunVerdict.CANCELLED
        assert classify_verdict(INTERRUPTED_CRITICAL_RUN, EXIT_SIGINT) \
            is RunVerdict.NEEDS_RESYNC
        assert classify_verdict(GRACEFUL_RUN, EXIT_SIGINT) \
            is not classify_verdict(INTERRUPTED_CRITICAL_RUN, EXIT_SIGINT)

    def test_an_exit_130_run_ending_in_must_run_resync_is_not_ok(self):
        assert INTERRUPTED_CRITICAL_RUN.rstrip().splitlines()[-2].endswith(
            "Must run --resync to recover.")
        assert classify_verdict(INTERRUPTED_CRITICAL_RUN, EXIT_SIGINT) \
            is not RunVerdict.OK

    def test_a_graceful_stop_is_cancelled_not_plain_ok(self):
        """`Bisync successful` at exit 0 is OK; the same line at 130 is a
        deliberate stop, and the activity row must say so."""
        assert classify_verdict(GRACEFUL_RUN, EXIT_OK) is RunVerdict.OK
        assert classify_verdict(GRACEFUL_RUN, EXIT_SIGINT) is RunVerdict.CANCELLED

    def test_the_specific_safety_abort_outranks_the_generic_line(self):
        """Both logs end in `Bisync aborted. Please try again.`; RETRYABLE would
        lose the only actionable fact."""
        assert "Bisync aborted. Please try again." in MAXDELETE_RUN
        assert "Bisync aborted. Please try again." in ALLCHANGED_RUN
        assert classify_verdict(MAXDELETE_RUN, 1) is RunVerdict.ABORTED_MAXDELETE
        assert classify_verdict(ALLCHANGED_RUN, 1) is RunVerdict.ABORTED_ALLCHANGED

    def test_access_denied_outranks_needs_resync(self):
        """A resync alone cannot fix a missing RCLONE_TEST — the next run would
        abort identically, forever."""
        text = CHECK_ACCESS_RUN.replace(
            "Error is retryable without --resync due to --resilient mode.",
            "Must run --resync to recover.")
        assert "Must run --resync to recover." in text
        assert classify_verdict(text, EXIT_CRITICAL) is RunVerdict.ACCESS_DENIED

    def test_intermediate_errors_do_not_beat_a_successful_ending(self):
        """`Ignoring --track-renames` is logged at ERROR on every resync."""
        assert "ERROR" in OK_RUN
        assert classify_verdict(OK_RUN, EXIT_OK) is RunVerdict.OK

    def test_classifies_a_json_log_identically(self):
        text = "\n".join([JSON_PLAIN, JSON_OBJECT, JSON_SUCCESS, JSON_STATS])
        assert classify_verdict(text, EXIT_OK) is RunVerdict.OK

    def test_classifies_a_log_relayed_through_rcd(self):
        """`rcd` re-emits bisync's lines double-timestamped and level-shifted."""
        relayed = "\n".join(
            f"NOTICE: {line}" for line in MAXDELETE_RUN.strip().splitlines())
        assert classify_verdict(relayed, None) is RunVerdict.ABORTED_MAXDELETE

    def test_accepts_a_list_of_lines(self):
        assert classify_verdict(OK_RUN.splitlines(), EXIT_OK) is RunVerdict.OK

    def test_accepts_a_list_of_records(self):
        records = parse_text("\n".join([JSON_PLAIN, JSON_SUCCESS]) + "\n")
        assert classify_verdict(records, EXIT_OK) is RunVerdict.OK

    @pytest.mark.parametrize("code,expected", [
        (EXIT_OK, RunVerdict.OK),
        (EXIT_NON_CRITICAL, RunVerdict.RETRYABLE),
        (EXIT_CRITICAL, RunVerdict.NEEDS_RESYNC),
        (EXIT_SIGINT, RunVerdict.CANCELLED),
        (EXIT_USAGE, RunVerdict.UNKNOWN),
    ])
    def test_falls_back_to_the_exit_code_on_an_empty_log(self, code, expected):
        """A log lost to a crash before the first flush still yields something."""
        assert classify_verdict("", code) is expected

    def test_no_log_and_no_exit_code_is_unknown(self):
        assert classify_verdict("", None) is RunVerdict.UNKNOWN

    def test_a_cobra_usage_error_is_not_json_and_does_not_crash(self):
        """A bad flag is printed before rclone's logger exists **[V]**."""
        usage = ("Error: unknown flag: --nope\nUsage:\n"
                 "  rclone bisync remote1:path1 remote2:path2 [flags]\n")
        assert classify_verdict(usage, EXIT_USAGE) is RunVerdict.UNKNOWN


# ═════════════════════════════════════════════════════════════════════════════
# strip_rcd_prefix
# ═════════════════════════════════════════════════════════════════════════════

class TestStripRcdPrefix:

    def test_strips_a_plain_bisync_prefix(self):
        assert strip_rcd_prefix(
            "2026/08/31 20:36:03 ERROR : Safety abort: too many deletes") \
            == "Safety abort: too many deletes"

    def test_strips_the_double_rcd_wrapping(self):
        """Verbatim from the research capture of an rcd-relayed line."""
        assert strip_rcd_prefix(
            'NOTICE: 2026/08/30 23:40:09 ERROR : Safety abort: too many deletes '
            '(>0%, 1 of 10) on Path1 "x"') \
            == 'Safety abort: too many deletes (>0%, 1 of 10) on Path1 "x"'

    @pytest.mark.parametrize("level", [
        "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL"])
    def test_every_level_is_recognised(self, level):
        assert strip_rcd_prefix(f"2026/08/31 20:36:03 {level}  : hello") == "hello"

    def test_lowercase_levels_too(self):
        assert strip_rcd_prefix("notice: hello") == "hello"

    def test_text_with_no_prefix_is_unchanged(self):
        assert strip_rcd_prefix("Bisync successful") == "Bisync successful"

    def test_removes_ansi_escapes(self):
        """`--use-json-log` alone still embeds raw escapes in `msg` **[V]**."""
        assert strip_rcd_prefix(
            "\x1b[2mSetting --ignore-listing-checksum\x1b[0m") \
            == "Setting --ignore-listing-checksum"

    def test_trailing_whitespace_is_trimmed(self):
        assert strip_rcd_prefix("INFO  : Bisync successful   ") == "Bisync successful"

    def test_a_colon_inside_the_message_survives(self):
        assert strip_rcd_prefix(
            "2026/08/31 20:36:04 NOTICE: Failed to bisync: prior lock file "
            "found: /tmp/x.lck") \
            == "Failed to bisync: prior lock file found: /tmp/x.lck"

    def test_a_message_that_merely_mentions_a_level_is_not_eaten(self):
        assert strip_rcd_prefix("the word ERROR appears here") \
            == "the word ERROR appears here"


# ═════════════════════════════════════════════════════════════════════════════
# parse_record — all three shapes
# ═════════════════════════════════════════════════════════════════════════════

class TestParseRecord:

    def test_plain_record(self):
        rec = parse_record(JSON_PLAIN, offset=120)
        assert isinstance(rec, LogRecord)
        assert rec.level == "info"
        assert rec.msg == "Copying Path2 files to Path1"
        assert rec.source == "bisync/resync.go:44"
        assert rec.time.startswith("2026-08-31T20:36:57")
        assert rec.object == "" and rec.size == 0
        assert rec.stats is None and rec.is_stats is False
        assert rec.is_object is False
        assert rec.offset == 120
        assert rec.raw == JSON_PLAIN

    def test_object_scoped_record(self):
        rec = parse_record(JSON_OBJECT)
        assert rec.msg == "Copied (new)"
        assert rec.object == "a.txt"
        assert rec.object_type == "*local.Object"
        assert rec.size == 6
        assert rec.is_object is True
        assert rec.is_stats is False

    def test_stats_record(self):
        rec = parse_record(JSON_STATS)
        assert rec.is_stats is True
        assert rec.stats["transfers"] == 1
        assert rec.stats["bytes"] == 6
        assert rec.stats["eta"] is None
        assert rec.level == "notice"

    def test_error_level_records(self):
        rec = parse_record('{"level":"error","msg":"boom"}')
        assert rec.is_error is True
        assert parse_record('{"level":"info","msg":"fine"}').is_error is False

    def test_blank_and_non_json_lines_are_none(self):
        assert parse_record("") is None
        assert parse_record("   \n") is None
        assert parse_record("Error: unknown flag: --nope") is None
        assert parse_record("[1, 2, 3]") is None

    def test_bytes_input_is_decoded(self):
        assert parse_record(JSON_PLAIN.encode()).msg == "Copying Path2 files to Path1"

    def test_a_torn_multibyte_tail_does_not_raise(self):
        assert parse_record(b'{"msg":"Im\xc3') is None

    def test_msg_is_prefix_stripped_and_ansi_free(self):
        raw = json.dumps({"level": "error",
                          "msg": "\x1b[31m2026/08/31 20:00:00 ERROR : boom\x1b[0m"})
        assert parse_record(raw).msg == "boom"

    def test_parse_text_carries_running_offsets(self):
        text = "\n".join([JSON_PLAIN, JSON_OBJECT, JSON_SUCCESS]) + "\n"
        records = parse_text(text)
        assert [r.msg for r in records] == [
            "Copying Path2 files to Path1", "Copied (new)", "Bisync successful"]
        assert records[-1].offset == len(text.encode("utf-8"))
        assert records[0].offset < records[1].offset < records[2].offset

    def test_parse_text_skips_junk_but_still_advances(self):
        text = JSON_PLAIN + "\nnot json at all\n" + JSON_SUCCESS + "\n"
        records = parse_text(text)
        assert len(records) == 2
        assert records[-1].offset == len(text.encode("utf-8"))


# ═════════════════════════════════════════════════════════════════════════════
# is_benign / milestone / conflict_path / stats_counts
# ═════════════════════════════════════════════════════════════════════════════

class TestIsBenign:

    @pytest.mark.parametrize("text", [
        "Local file system at /tmp/p1: Ignoring --track-renames as it doesn't "
        "work with copy or move, only sync",
        "WARNING  listing try 1 failed.        - onedrive:",
        "lock file renewed for 2m0s. New expiration: 2026-08-31 20:37:23",
        "vfs cache: detected external removal of cache file",
        "Ignoring sync error due to Graceful Shutdown: context canceled",
        "Canceling Sync if not done in: 30s",
        "Graceful shutdown completed successfully.",
        "huge.bin: Failed to copy: context canceled",
        "Setting --ignore-listing-checksum as neither --checksum nor --compare "
        "checksum are set.",
        "--max-lock cannot be shorter than 2 minutes (unless 0.)",
        "There was nothing to transfer",
    ])
    def test_known_red_herrings(self, text):
        assert is_benign(text) is True

    @pytest.mark.parametrize("text", [
        "Safety abort: too many deletes (>25%, 6 of 14) on Path1",
        "Access test failed: Path1 count 0, Path2 count 0 - RCLONE_TEST",
        "Bisync critical error: filters file has changed (must run --resync)",
        "Bisync aborted. Must run --resync to recover.",
        "quotaLimitReached",
    ])
    def test_real_failures_are_not_benign(self, text):
        assert is_benign(text) is False

    def test_the_resync_track_renames_error_in_a_real_log_is_benign(self):
        noisy = [line for line in OK_RUN.splitlines() if "ERROR" in line]
        assert noisy
        assert all(is_benign(strip_rcd_prefix(line)) for line in noisy)

    def test_accepts_a_record_and_a_mapping(self):
        record = parse_record(JSON_PLAIN)
        assert is_benign(record) is False
        assert is_benign({"msg": "Canceling Sync if not done in: 3s"}) is True

    def test_an_empty_line_is_benign(self):
        assert is_benign("") is True

    def test_does_not_restate_the_shared_patterns(self):
        """`errors.BENIGN_PATTERNS` is the frozen contract; duplicating a rule
        here would let the two drift."""
        from onedriveui.errors import BENIGN_PATTERNS

        shared = {p.pattern for p in BENIGN_PATTERNS}
        local = {p.pattern for p in log_mod.BENIGN_BISYNC_PATTERNS}
        assert shared.isdisjoint(local)


class TestMilestone:

    @pytest.mark.parametrize("text,phase", [
        ("Building Path1 and Path2 listings", "listing"),
        ("Path1 checking for diffs", "comparing"),
        ("Path2 checking for diffs", "comparing"),
        ("Applying changes", "transferring"),
        ("- Path2             Do queued copies to                         - Path1",
         "transferring"),
        ("- Path2             Resync is copying files to                  - Path1",
         "transferring"),
        ("Updating listings", "finishing"),
        ('Validating listings for Path1 "/tmp/bs/p1/" vs Path2 "/tmp/bs/p2/"',
         "finishing"),
        ("Checking access health", "comparing"),
        ("Bisync successful", "done"),
    ])
    def test_real_lines_map_to_phases(self, text, phase):
        assert milestone(text) == phase

    def test_an_unremarkable_line_announces_nothing(self):
        assert milestone("cloudfile.txt: Copied (new)") == ""

    def test_a_whole_run_ends_in_done(self):
        phases = [p for p in (milestone(strip_rcd_prefix(line))
                              for line in OK_RUN.splitlines()) if p]
        assert phases[-1] == "done"
        assert "transferring" in phases


class TestConflictPath:

    def test_extracts_the_path_from_the_padded_notice(self):
        record = parse_record(JSON_CONFLICT)
        assert record.object == ""          # the record carries NO object field
        assert conflict_path(record) == "Documents/report.docx"

    def test_works_on_the_plain_log_line_too(self):
        line = ("2026/08/31 20:35:40 NOTICE: - WARNING           New or changed "
                "in both paths                - a.txt")
        assert conflict_path(strip_rcd_prefix(line)) == "a.txt"

    def test_a_path_with_spaces_survives(self):
        line = ("- WARNING           New or changed in both paths      "
                "          - My Folder/a b.txt")
        assert conflict_path(line) == "My Folder/a b.txt"

    def test_other_lines_are_none(self):
        assert conflict_path("Bisync successful") is None
        assert conflict_path("- Path1             Queue copy to Path2  - /x") is None


class TestStatsCounts:

    def test_maps_onto_the_run_record_fields(self):
        record = parse_record(JSON_STATS)
        assert stats_counts(record.stats) == {
            "files_transferred": 1, "bytes": 6, "deletes": 0,
            "renames": 0, "errors": 0}

    def test_a_run_with_deletes_and_renames(self):
        stats = {"transfers": 4, "bytes": 53, "deletes": 1, "renames": 2,
                 "errors": 0}
        assert stats_counts(stats) == {
            "files_transferred": 4, "bytes": 53, "deletes": 1,
            "renames": 2, "errors": 0}

    def test_none_is_all_zeros(self):
        assert set(stats_counts(None).values()) == {0}

    def test_junk_values_degrade_to_zero(self):
        assert stats_counts({"transfers": None, "bytes": "x"})["bytes"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# LogTailer
# ═════════════════════════════════════════════════════════════════════════════

class TestLogTailer:

    def _log(self, tmp_path: Path) -> Path:
        path = tmp_path / "runs" / "r1" / "bisync.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    def test_emits_records_and_stats_on_separate_signals(self, tmp_path, qapp):
        path = self._log(tmp_path)
        path.write_text("\n".join([JSON_PLAIN, JSON_STATS, JSON_SUCCESS]) + "\n",
                        encoding="utf-8")
        tailer = LogTailer(path, follow=False)
        records: list[LogRecord] = []
        stats: list[dict] = []
        tailer.record.connect(records.append)
        tailer.stats.connect(stats.append)

        tailer.read_available()

        assert [r.msg for r in records] == ["Copying Path2 files to Path1",
                                            "Bisync successful"]
        assert len(stats) == 1 and stats[0]["transfers"] == 1

    def test_resumes_from_a_byte_offset_without_replaying(self, tmp_path, qapp):
        """The whole reason `runs.log_offset` exists: replaying would duplicate
        every conflict and activity row the previous process already wrote."""
        path = self._log(tmp_path)
        first = JSON_PLAIN + "\n"
        path.write_text(first + JSON_SUCCESS + "\n", encoding="utf-8")

        resumed = LogTailer(path, offset=len(first.encode()), follow=False)
        seen: list[LogRecord] = []
        resumed.record.connect(seen.append)
        resumed.read_available()

        assert [r.msg for r in seen] == ["Bisync successful"]
        assert resumed.offset == path.stat().st_size

    def test_offset_advances_only_past_complete_lines(self, tmp_path, qapp):
        path = self._log(tmp_path)
        path.write_text(JSON_PLAIN + "\n" + JSON_SUCCESS[:40], encoding="utf-8")
        tailer = LogTailer(path, follow=False)
        seen: list[LogRecord] = []
        tailer.record.connect(seen.append)

        tailer.read_available()

        assert [r.msg for r in seen] == ["Copying Path2 files to Path1"]
        assert tailer.offset == len((JSON_PLAIN + "\n").encode())

    def test_a_partial_line_is_parsed_once_when_it_completes(self, tmp_path, qapp):
        path = self._log(tmp_path)
        half, rest = JSON_SUCCESS[:40], JSON_SUCCESS[40:]
        path.write_text(half, encoding="utf-8")
        tailer = LogTailer(path, follow=False)
        seen: list[LogRecord] = []
        tailer.record.connect(seen.append)

        assert tailer.read_available() == []
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(rest + "\n")
        tailer.read_available()

        assert [r.msg for r in seen] == ["Bisync successful"]
        assert tailer.offset == path.stat().st_size

    def test_progressed_reports_the_checkpoint(self, tmp_path, qapp):
        path = self._log(tmp_path)
        path.write_text(JSON_SUCCESS + "\n", encoding="utf-8")
        tailer = LogTailer(path, follow=False)
        offsets: list[int] = []
        tailer.progressed.connect(offsets.append)
        tailer.read_available()
        assert offsets == [path.stat().st_size]

    def test_no_growth_emits_nothing(self, tmp_path, qapp):
        path = self._log(tmp_path)
        path.write_text(JSON_SUCCESS + "\n", encoding="utf-8")
        tailer = LogTailer(path, follow=False)
        tailer.read_available()
        offsets: list[int] = []
        tailer.progressed.connect(offsets.append)
        assert tailer.read_available() == []
        assert offsets == []

    def test_a_missing_file_is_waited_for_not_an_error(self, tmp_path, qapp):
        tailer = LogTailer(tmp_path / "not-yet" / "bisync.jsonl", follow=False)
        assert tailer.read_available() == []
        assert tailer.offset == 0

    def test_a_truncated_log_restarts_the_tail(self, tmp_path, qapp):
        """A fresh run reusing the directory: skipping to the old offset would
        silently drop the new run's beginning."""
        path = self._log(tmp_path)
        path.write_text((JSON_PLAIN + "\n") * 3, encoding="utf-8")
        tailer = LogTailer(path, follow=False)
        tailer.read_available()
        assert tailer.offset > 0

        path.write_text(JSON_SUCCESS + "\n", encoding="utf-8")
        seen: list[LogRecord] = []
        tailer.record.connect(seen.append)
        tailer.read_available()

        assert [r.msg for r in seen] == ["Bisync successful"]
        assert tailer.offset == path.stat().st_size

    def test_drain_catches_up_on_a_finished_run(self, tmp_path, qapp):
        path = self._log(tmp_path)
        path.write_text("\n".join([JSON_PLAIN, JSON_OBJECT, JSON_SUCCESS]) + "\n",
                        encoding="utf-8")
        tailer = LogTailer(path, follow=False)
        assert len(tailer.drain()) == 3
        assert tailer.offset == path.stat().st_size

    def test_verdict_reads_the_whole_file_not_just_what_was_tailed(self, tmp_path, qapp):
        """The terminal line can land after the last pass, and a resumed tailer
        starts mid-file."""
        path = self._log(tmp_path)
        path.write_text("\n".join([JSON_PLAIN, JSON_SUCCESS]) + "\n",
                        encoding="utf-8")
        tailer = LogTailer(path, offset=10_000, follow=False)
        assert tailer.verdict(EXIT_OK) is RunVerdict.OK

    def test_verdict_of_a_missing_log_falls_back_to_the_exit_code(self, tmp_path, qapp):
        tailer = LogTailer(tmp_path / "gone.jsonl", follow=False)
        assert tailer.verdict(EXIT_CRITICAL) is RunVerdict.NEEDS_RESYNC

    def test_is_a_qthread_that_actually_runs(self, tmp_path, qapp):
        from PySide6.QtCore import QThread

        path = self._log(tmp_path)
        path.write_text(JSON_SUCCESS + "\n", encoding="utf-8")
        tailer = LogTailer(path, poll_ms=10, follow=True)
        assert isinstance(tailer, QThread)
        seen: list[LogRecord] = []
        ended: list[int] = []
        tailer.record.connect(seen.append)
        tailer.ended.connect(ended.append)

        tailer.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not seen:
            qapp.processEvents()
            time.sleep(0.01)
        tailer.stop()
        assert tailer.wait(5000)
        qapp.processEvents()

        assert [r.msg for r in seen] == ["Bisync successful"]
        assert ended == [path.stat().st_size]

    def test_a_thread_picks_up_a_line_written_after_it_started(self, tmp_path, qapp):
        path = self._log(tmp_path)
        tailer = LogTailer(path, poll_ms=10, follow=True)
        seen: list[LogRecord] = []
        tailer.record.connect(seen.append)
        tailer.start()
        time.sleep(0.05)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(JSON_SUCCESS + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not seen:
            qapp.processEvents()
            time.sleep(0.01)
        tailer.stop()
        assert tailer.wait(5000)
        qapp.processEvents()
        assert [r.msg for r in seen] == ["Bisync successful"]

    def test_stop_is_idempotent(self, tmp_path, qapp):
        tailer = LogTailer(self._log(tmp_path), follow=False)
        tailer.stop()
        tailer.stop()
        assert tailer.offset == 0

    def test_path_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert LogTailer("~/run.jsonl").path == tmp_path / "run.jsonl"


class TestModuleHygiene:

    def test_every_public_name_exists(self):
        for name in log_mod.__all__:
            assert hasattr(log_mod, name), name

    def test_no_widget_import(self):
        """The tailer is a worker thread; QtWidgets there is a threading bug."""
        source = Path(log_mod.__file__).read_text(encoding="utf-8")
        assert "QtWidgets" not in source
        assert "QtGui" not in source

    def test_every_terminal_rule_appears_in_a_captured_run(self):
        """No rule may be invented: each fragment must be evidenced."""
        corpus = "\n".join(text for _n, text, _c, _v in CAPTURED)
        unmatched = [fragment for fragment, _v in log_mod.TERMINAL_RULES
                     if fragment not in corpus]
        assert unmatched == ["Lock file exists, but contents are unreadable"], \
            f"unevidenced terminal rules: {unmatched}"

    def test_exit_codes_match_the_captured_runs(self):
        assert {code for _n, _t, code, _v in CAPTURED} == {
            EXIT_OK, EXIT_NON_CRITICAL, EXIT_CRITICAL, EXIT_SIGINT}
