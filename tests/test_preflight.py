"""WP-07 — `sync/preflight.py`.

OneDrive's naming rules are Windows' naming rules, and Linux has none of them, so
these tests are mostly a transcription of Microsoft's published restrictions plus
the two properties that make the module usable: `suggest()` is deterministic, and
whatever it proposes passes `validate_name()`.
"""

from __future__ import annotations

import pytest

from onedriveui.constants import (
    INVALID_CHARS,
    MAX_FILE_BYTES,
    MAX_REL_PATH_CHARS,
    MAX_TOTAL_PATH_CHARS,
    RESERVED_NAMES,
)
from onedriveui.models import IssueCode
from onedriveui.sync.preflight import (
    scan_tree,
    suggest,
    validate_name,
    validate_path,
    validate_size,
)


class TestValidateName:

    @pytest.mark.parametrize("char", sorted(INVALID_CHARS))
    def test_every_invalid_character_is_rejected(self, char):
        violation = validate_name(f"report{char}2026.docx")
        assert violation is not None
        assert violation.code is IssueCode.NAME_INVALID
        assert char in violation.detail

    @pytest.mark.parametrize("name", sorted(RESERVED_NAMES))
    def test_every_reserved_name_is_rejected(self, name):
        assert validate_name(name) is not None

    def test_a_dos_device_is_reserved_with_an_extension_too(self):
        """Windows reserves `NUL` whether or not it has a suffix, so `NUL.txt`
        is as unusable as `NUL`."""
        assert validate_name("NUL.txt") is not None
        assert validate_name("nul.log") is not None

    def test_a_trailing_space_is_rejected(self):
        """Windows strips it silently on creation, so the name would not
        survive its own round trip."""
        assert validate_name("report ") is not None

    def test_a_trailing_period_is_rejected(self):
        assert validate_name("report.") is not None

    def test_an_office_lock_prefix_is_rejected(self):
        """`~$` files are Office's own locks; uploading them races a program
        that is about to delete them."""
        assert validate_name("~$Budget.xlsx") is not None

    def test_a_sharepoint_prefix_is_rejected_anywhere_in_the_name(self):
        """OneDrive for Business is SharePoint, and `_vti_` is its internal
        prefix — reserved in the middle of a name as well as at the start."""
        assert validate_name("my_vti_notes.txt") is not None

    def test_an_empty_name_is_rejected(self):
        assert validate_name("") is not None

    def test_an_ordinary_name_is_accepted(self):
        """The BUILD_PLAN's acceptance case, and the one that matters most: a
        validator that rejects real filenames is worse than none."""
        assert validate_name("My Report (final).docx") is None

    @pytest.mark.parametrize("name", [
        "notes.txt", "Ünïcödé.md", "a.b.c.tar.gz", "2026-08-31 backup.zip",
        "file with  spaces.pdf", "CONCERT.mp3", "communication.log",
        "  leading spaces are fine.txt",
    ])
    def test_valid_names_are_not_rejected(self, name):
        """`CONCERT` and `communication` start with `CON` but are not it."""
        assert validate_name(name) is None


class TestValidatePath:

    def test_every_component_is_checked(self):
        assert validate_path("Documents/bad:name/file.txt", "/home/u/OneDrive") is not None

    def test_a_long_relative_path_is_rejected(self):
        long_path = "a/" * (MAX_REL_PATH_CHARS // 2 + 1)
        violation = validate_path(long_path, "/home/u/OneDrive")
        assert violation is not None
        assert violation.code is IssueCode.PATH_TOO_LONG

    def test_the_folder_counts_toward_the_total(self):
        """Moving a synced folder deeper can break files that were fine before,
        which is why the sync root's own length is part of the check."""
        rel = "x" * (MAX_REL_PATH_CHARS - 1)
        deep_root = "/" + "d" * (MAX_TOTAL_PATH_CHARS - MAX_REL_PATH_CHARS)
        assert validate_path(rel, "/home/u") is None
        assert validate_path(rel, deep_root) is not None

    def test_the_violation_names_the_whole_path(self):
        """The user has to be able to find the file, and the component alone
        does not tell them where it is."""
        violation = validate_path("Docs/bad:name.txt", "/home/u/OneDrive")
        assert violation.rel_path == "Docs/bad:name.txt"
        assert violation.suggested_name == "bad_name.txt"

    def test_an_ordinary_path_is_accepted(self):
        assert validate_path("Documents/2026/My Report.docx", "/home/u/OneDrive") is None


class TestValidateSize:

    def test_a_small_file_is_fine(self, tmp_path):
        path = tmp_path / "small.bin"
        path.write_bytes(b"x" * 10)
        assert validate_size(path) is None

    def test_a_huge_file_is_rejected(self, tmp_path):
        path = tmp_path / "huge.bin"
        path.write_bytes(b"")
        # A sparse file is enough: `getsize` reports the apparent size.
        with open(path, "wb") as handle:
            handle.truncate(MAX_FILE_BYTES + 1)
        violation = validate_size(path)
        assert violation is not None
        assert violation.code is IssueCode.FILE_TOO_LARGE
        assert violation.suggested_name is None      # renaming cannot fix it

    def test_a_vanished_file_is_not_a_naming_problem(self, tmp_path):
        assert validate_size(tmp_path / "gone.txt") is None


class TestSuggest:

    def test_it_is_deterministic(self):
        """The BUILD_PLAN's acceptance case. This is the pre-filled value in the
        Rename dialog and the automatic choice for an unattended repair — a
        suggestion that varied between runs would resolve the same conflict
        differently each time, and two machines would disagree."""
        assert suggest("a:b?.txt") == suggest("a:b?.txt") == "a_b_.txt"

    @pytest.mark.parametrize("name", [
        "a:b?.txt", "report.", "report ", "~$Budget.xlsx", "my_vti_x.txt",
        "NUL", "NUL.txt", "CON.log", "desktop.ini", ".lock", '"quoted".md',
        "|pipe|.txt", "***.doc", "   ", "a" * 5,
    ])
    def test_whatever_it_proposes_is_valid(self, name):
        """The property that makes the suggestion worth offering."""
        assert validate_name(suggest(name)) is None

    def test_the_extension_is_preserved(self):
        """A `.docx` that stops opening in Word is a worse outcome than an
        awkward filename."""
        assert suggest("bad:name.docx").endswith(".docx")
        assert suggest("desktop.ini").endswith(".ini")

    def test_invalid_characters_become_underscores_not_nothing(self):
        """Deleting them would collapse `a:b.txt` and `ab.txt` onto one name,
        turning a naming problem into a data-loss problem."""
        assert suggest("a:b.txt") == "a_b.txt"
        assert suggest("a:b.txt") != suggest("ab.txt")

    def test_a_name_that_is_entirely_invalid_still_produces_something(self):
        assert suggest("...")
        assert validate_name(suggest("...")) is None


class TestScanTree:

    def test_it_finds_violations(self, tmp_path):
        (tmp_path / "Docs").mkdir()
        (tmp_path / "Docs" / "fine.txt").write_text("ok")
        (tmp_path / "Docs" / "bad:name.txt").write_text("no")
        found = {v.rel_path for v in scan_tree(tmp_path)}
        assert found == {"Docs/bad:name.txt"}

    def test_a_clean_tree_yields_nothing(self, tmp_path):
        (tmp_path / "a.txt").write_text("ok")
        assert list(scan_tree(tmp_path)) == []

    def test_the_budget_stops_it(self, tmp_path):
        """A first-run scan of a 300 000-item drive must not block its caller."""
        for i in range(50):
            folder = tmp_path / f"d{i}"
            folder.mkdir()
            (folder / "bad:name.txt").write_text("no")
        assert len(list(scan_tree(tmp_path, budget_ms=0))) < 50

    def test_a_missing_root_is_not_an_error(self, tmp_path):
        assert list(scan_tree(tmp_path / "gone")) == []
