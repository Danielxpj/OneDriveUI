"""WP-04 — `onedriveui/rc/filters.py`.

Two invariants meet in this file.

**I13** — `- *.partial` is never optional. A SIGKILL mid-transfer leaves
`<name>.<hash>.partial` at the destination; a real run on this machine then
logged::

    INFO  : - Path2    File is new    - big.bin.677c7953.partial
    INFO  : big.bin.677c7953.partial: Copied (new)

**I11** — a filters rewrite must be paired with an immediate `--resync`. The
consequence of forgetting is not theoretical; both spellings were produced here::

    ERROR : Bisync critical error: filters file md5 hash not found (must run --resync): …
    ERROR : Bisync critical error: filters file has changed (must run --resync): …
    ERROR : Bisync aborted. Must run --resync to recover.

so the transaction is tested for the property that makes forgetting survivable:
the previous file is *restored* and `SafetyRefusal` is raised, leaving the
account exactly as syncable as it was.

The syntax assertions are calibrated against the real binary, which answers
``failed to reload "filter" options: malformed rule "-*.partial"`` for a missing
space and the same for a missing sign. The `live` test at the end feeds
`render()` to a real `rclone lsf --filter-from -` and checks both the exit status
and the selected set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from onedriveui import APP_NAME, paths
from onedriveui.atomicio import md5_of_file
from onedriveui.constants import (
    MANDATORY_EXCLUDES,
    REMOTE_TRASH_DIR,
    REMOTE_VERSIONS_DIR,
)
from onedriveui.errors import ConfigError, SafetyRefusal
from onedriveui.rc import filters as filters_mod
from onedriveui.rc.filters import (
    HEADER_LINES,
    MD5_LENGTH,
    FiltersTransaction,
    escape_pattern,
    exclude_rule,
    filters_config,
    md5_of_text,
    needs_resync,
    read_rules,
    render,
    rewrite,
    stored_md5,
    validate,
    write,
    write_md5,
)

ACCOUNT = "onedrive"
HEX = set("0123456789abcdef")


@pytest.fixture
def account_id(_isolate_home):
    """An isolated config dir; `paths` caches nothing, so HOME's patch holds."""
    return ACCOUNT


# ═════════════════════════════════════════════════════════════════════════════
# render
# ═════════════════════════════════════════════════════════════════════════════

class TestRender:

    def test_carries_every_mandatory_exclude_verbatim(self):
        lines = render().splitlines()
        for rule in MANDATORY_EXCLUDES:
            assert rule in lines

    def test_partial_is_always_first_among_the_rules(self):
        """I13. First match wins, so being first is not cosmetic."""
        rules = [ln for ln in render(["Videos"]).splitlines()
                 if ln.startswith(("-", "+"))]
        assert rules[0] == "- *.partial"

    def test_the_mandatory_block_comes_from_the_frozen_contract(self):
        """Never a copy — the mount argv and this file must not drift apart."""
        source = Path(filters_mod.__file__).read_text(encoding="utf-8")
        assert "MANDATORY_EXCLUDES" in source
        assert '"- *.partial"' not in source
        assert "*.tmp" not in source

    def test_excludes_our_own_remote_directories(self):
        lines = render().splitlines()
        assert f"- {REMOTE_TRASH_DIR}/" in lines
        assert f"- {REMOTE_VERSIONS_DIR}/" in lines

    def test_user_folders_are_anchored_and_directory_terminated(self):
        lines = render(["Videos", "Documents/Personal"]).splitlines()
        assert "- /Videos/" in lines
        assert "- /Documents/Personal/" in lines
        assert not any(line.endswith("**") for line in lines)

    def test_folders_are_sorted_so_the_output_is_stable(self):
        """Byte-stability is what lets `write()` say "unchanged" and skip a
        resync — an unsorted render would force one on every settings save."""
        assert render(["Videos", "Apps"]) == render(["Apps", "Videos"])
        assert render(["Videos", "Videos/"]) == render(["Videos"])

    def test_duplicates_and_stray_separators_collapse(self):
        assert render(["/Videos/", "Videos", "Videos/"]) == render(["Videos"])

    def test_glob_metacharacters_in_a_real_folder_name_are_escaped(self):
        assert "- /Weird \\[name\\]/" in render(["Weird [name]"]).splitlines()

    def test_spaces_in_a_folder_name_are_literal(self):
        assert "- /My Folder/" in render(["My Folder"]).splitlines()

    def test_header_names_the_resync_obligation(self):
        text = render()
        assert text.startswith(HEADER_LINES[0])
        assert APP_NAME in text
        assert "--resync" in text
        assert "I11" in text

    def test_header_can_be_suppressed(self):
        """The banner goes; the section comments and every rule stay."""
        bare = render(["Videos"], header=False)
        assert HEADER_LINES[0] not in bare
        assert "- *.partial" in bare.splitlines()
        assert "- /Videos/" in bare.splitlines()

    def test_ends_with_exactly_one_newline_and_no_trailing_spaces(self):
        text = render(["Videos"])
        assert text.endswith("\n")
        assert not text.endswith("\n\n")
        assert all(line == line.rstrip() for line in text.splitlines())

    def test_uses_unix_line_endings_only(self):
        assert "\r" not in render(["Videos"])

    def test_has_no_terminal_catch_all(self):
        """Exclude-style, like the Windows client's "Choose folders"."""
        assert "- **" not in render(["Videos"]).splitlines()

    def test_extra_rules_land_after_the_mandatory_block(self):
        lines = render(["Videos"], extra_rules=["- /Custom/"]).splitlines()
        assert lines.index("- /Custom/") > lines.index("- *.partial")
        assert lines.index("- /Custom/") < lines.index("- /Videos/")

    def test_an_empty_excluded_path_is_a_programming_error(self):
        with pytest.raises(ValueError):
            exclude_rule("")
        with pytest.raises(ValueError):
            exclude_rule("///")

    def test_every_emitted_line_validates(self):
        validate(render(["A", "B/C", "Weird [name]"]).splitlines())


class TestEscaping:

    @pytest.mark.parametrize("raw,escaped", [
        ("plain", "plain"),
        ("star*", "star\\*"),
        ("q?", "q\\?"),
        ("[cls]", "\\[cls\\]"),
        ("{a,b}", "\\{a,b\\}"),
        ("back\\slash", "back\\\\slash"),
        ("a/b", "a/b"),
        ("Imágenes", "Imágenes"),
    ])
    def test_escape_pattern(self, raw, escaped):
        assert escape_pattern(raw) == escaped

    def test_slashes_survive_because_they_are_the_separator(self):
        assert exclude_rule("a/b/c") == "- /a/b/c/"


class TestValidate:

    def test_accepts_the_generated_file(self):
        validate(render(["Videos"]).splitlines())

    def test_accepts_comments_blanks_and_the_reset_marker(self):
        validate(["# comment", "; also a comment", "", "   ", "!", "- x"])

    def test_rejects_a_missing_space(self):
        """rclone: `malformed rule "-*.partial"` — measured **[V]**."""
        with pytest.raises(ConfigError) as caught:
            validate(["-*.partial"])
        assert "malformed rule" in str(caught.value)

    def test_rejects_a_missing_sign(self):
        with pytest.raises(ConfigError):
            validate(["*.partial"])

    def test_rejects_two_spaces(self):
        with pytest.raises(ConfigError):
            validate(["-  *.partial"])

    def test_rejects_an_empty_pattern(self):
        with pytest.raises(ConfigError):
            validate(["- "])

    def test_rejects_a_trailing_space(self):
        """Everything after the single space is the pattern, trailing whitespace
        included — which is almost always a bug."""
        with pytest.raises(ConfigError):
            validate(["- *.partial "])

    def test_names_the_offending_line_number(self):
        with pytest.raises(ConfigError) as caught:
            validate(["- ok", "# fine", "bad"])
        assert "line 3" in str(caught.value)


class TestReadRules:

    def test_drops_comments_and_blanks(self, account_id):
        path = paths.filters_file(account_id)
        path.write_text("# hi\n\n- a\n;also\n!\n  - b  \n", encoding="utf-8")
        assert read_rules(path) == ["- a", "- b"]

    def test_missing_file_is_empty(self, tmp_path):
        assert read_rules(tmp_path / "nope.txt") == []

    def test_round_trips_the_generated_file(self, account_id):
        text = render(["Videos"])
        path = paths.filters_file(account_id)
        path.write_text(text, encoding="utf-8")
        assert read_rules(path)[0] == "- *.partial"
        assert read_rules(path)[-1] == "- /Videos/"


# ═════════════════════════════════════════════════════════════════════════════
# The MD5 sidecar
# ═════════════════════════════════════════════════════════════════════════════

class TestMd5Sidecar:

    def test_digest_matches_md5sum_of_the_file(self, account_id):
        text = render(["Videos"])
        path = paths.filters_file(account_id)
        path.write_text(text, encoding="utf-8")
        assert md5_of_text(text) == md5_of_file(path)

    def test_sidecar_is_32_lowercase_hex_with_no_trailing_newline(self, account_id):
        text = render(["Videos"])
        paths.filters_file(account_id).write_text(text, encoding="utf-8")
        digest = write_md5(account_id, text)
        raw = paths.filters_md5_file(account_id).read_bytes()

        assert len(raw) == MD5_LENGTH == 32
        assert not raw.endswith(b"\n")
        assert raw.decode() == digest == digest.lower()
        assert set(raw.decode()) <= HEX

    def test_sidecar_is_mode_0600(self, account_id):
        paths.filters_file(account_id).write_text(render(), encoding="utf-8")
        write_md5(account_id)
        mode = paths.filters_md5_file(account_id).stat().st_mode & 0o777
        assert mode == 0o600

    def test_sidecar_lives_beside_the_filters_file(self, account_id):
        assert paths.filters_md5_file(account_id).parent == \
            paths.filters_file(account_id).parent
        assert paths.filters_md5_file(account_id).name == \
            paths.filters_file(account_id).name + ".md5"

    def test_stored_md5_of_a_missing_sidecar_is_empty(self, account_id):
        assert stored_md5(account_id) == ""

    def test_stored_md5_ignores_a_non_digest(self, account_id):
        paths.filters_md5_file(account_id).write_text("not a digest", encoding="utf-8")
        assert stored_md5(account_id) == ""

    def test_stored_md5_tolerates_md5sum_style_two_field_output(self, account_id):
        digest = "a" * 32
        paths.filters_md5_file(account_id).write_text(
            f"{digest}  filters.txt\n", encoding="utf-8")
        assert stored_md5(account_id) == digest

    def test_needs_resync_when_the_sidecar_is_missing(self, account_id):
        paths.filters_file(account_id).write_text(render(), encoding="utf-8")
        assert needs_resync(account_id) is True

    def test_needs_resync_when_the_content_moved_on(self, account_id):
        text = render()
        paths.filters_file(account_id).write_text(text, encoding="utf-8")
        write_md5(account_id, text)
        assert needs_resync(account_id) is False
        paths.filters_file(account_id).write_text(render(["Videos"]), encoding="utf-8")
        assert needs_resync(account_id) is True

    def test_no_filters_file_needs_nothing(self, account_id):
        assert needs_resync(account_id) is False


# ═════════════════════════════════════════════════════════════════════════════
# write
# ═════════════════════════════════════════════════════════════════════════════

class TestWrite:

    def test_first_write_reports_changed(self, account_id):
        assert write(account_id, render()) is True
        assert paths.filters_file(account_id).is_file()

    def test_identical_content_reports_unchanged(self, account_id):
        """The acceptance bullet: a no-op settings save never forces a resync."""
        text = render(["Videos"])
        assert write(account_id, text) is True
        assert write(account_id, text) is False
        assert write(account_id, render(["Videos"])) is False

    def test_a_real_change_reports_changed(self, account_id):
        write(account_id, render(["Videos"]))
        assert write(account_id, render(["Videos", "Apps"])) is True

    def test_file_is_mode_0600(self, account_id):
        write(account_id, render())
        assert paths.filters_file(account_id).stat().st_mode & 0o777 == 0o600

    def test_a_change_deletes_the_stale_digest(self, account_id):
        """With no sidecar the next run aborts LOUDLY with `filters file md5 hash
        not found`; a freshly written one would let it run with wrong filters."""
        write(account_id, render())
        write_md5(account_id, render())
        assert paths.filters_md5_file(account_id).is_file()

        write(account_id, render(["Videos"]))
        assert not paths.filters_md5_file(account_id).exists()

    def test_an_unchanged_write_keeps_the_digest(self, account_id):
        text = render()
        write(account_id, text)
        write_md5(account_id, text)
        assert write(account_id, text) is False
        assert paths.filters_md5_file(account_id).is_file()

    def test_refuses_content_without_the_partial_rule(self, account_id):
        with pytest.raises(SafetyRefusal) as caught:
            write(account_id, "- *.tmp\n- desktop.ini\n")
        assert caught.value.invariant == "I13"
        assert not paths.filters_file(account_id).exists()

    def test_refuses_malformed_content_before_touching_the_file(self, account_id):
        write(account_id, render())
        before = paths.filters_file(account_id).read_text(encoding="utf-8")
        with pytest.raises(ConfigError):
            write(account_id, "- *.partial\n-*.tmp\n")
        assert paths.filters_file(account_id).read_text(encoding="utf-8") == before

    def test_write_is_atomic_leaving_no_debris(self, account_id):
        write(account_id, render(["Videos"]))
        names = {p.name for p in paths.config_dir().iterdir()}
        assert not any(".tmp" in name for name in names)


# ═════════════════════════════════════════════════════════════════════════════
# I11 — the transaction
# ═════════════════════════════════════════════════════════════════════════════

class TestFiltersTransaction:

    def test_a_reported_resync_commits_and_records_the_digest(self, account_id):
        with rewrite(account_id, ["Videos"]) as txn:
            assert txn.changed is True
            txn.resynced()
        assert paths.filters_file(account_id).read_text(encoding="utf-8") \
            == render(["Videos"])
        assert stored_md5(account_id) == md5_of_text(render(["Videos"]))
        assert needs_resync(account_id) is False

    def test_forgetting_the_resync_refuses_AND_rolls_back(self, account_id):
        """The account must be left exactly as syncable as it was."""
        with rewrite(account_id, ["Videos"]) as first:
            first.resynced()
        before = paths.filters_file(account_id).read_text(encoding="utf-8")
        before_md5 = stored_md5(account_id)

        with pytest.raises(SafetyRefusal) as caught:
            with rewrite(account_id, ["Videos", "Apps"]):
                pass                              # the caller forgot the resync

        assert caught.value.invariant == "I11"
        assert paths.filters_file(account_id).read_text(encoding="utf-8") == before
        assert stored_md5(account_id) == before_md5
        assert needs_resync(account_id) is False

    def test_rollback_removes_a_file_that_did_not_exist_before(self, account_id):
        with pytest.raises(SafetyRefusal):
            with rewrite(account_id, ["Videos"]):
                pass
        assert not paths.filters_file(account_id).exists()
        assert not paths.filters_md5_file(account_id).exists()

    def test_an_exception_inside_the_block_rolls_back_and_propagates(self, account_id):
        with rewrite(account_id, ["Videos"]) as first:
            first.resynced()
        before = paths.filters_file(account_id).read_text(encoding="utf-8")

        with pytest.raises(RuntimeError, match="resync failed"):
            with rewrite(account_id, ["Videos", "Apps"]) as txn:
                assert txn.changed is True
                raise RuntimeError("resync failed")

        assert paths.filters_file(account_id).read_text(encoding="utf-8") == before

    def test_an_unchanged_rewrite_needs_no_resync(self, account_id):
        with rewrite(account_id, ["Videos"]) as first:
            first.resynced()
        with rewrite(account_id, ["Videos"]) as second:
            assert second.changed is False        # no resync owed, no refusal
        assert needs_resync(account_id) is False

    def test_resynced_on_an_unchanged_transaction_is_harmless(self, account_id):
        with rewrite(account_id, ["Videos"]) as first:
            first.resynced()
        digest = stored_md5(account_id)
        with rewrite(account_id, ["Videos"]) as second:
            second.resynced()
        assert stored_md5(account_id) == digest

    def test_explicit_text_bypasses_render(self, account_id):
        body = "- *.partial\n- /Custom/\n"
        with rewrite(account_id, text=body) as txn:
            txn.resynced()
        assert paths.filters_file(account_id).read_text(encoding="utf-8") == body

    def test_explicit_text_still_obeys_i13(self, account_id):
        with pytest.raises(SafetyRefusal) as caught:
            with rewrite(account_id, text="- *.tmp\n"):
                pass
        assert caught.value.invariant == "I13"

    def test_manual_rollback_is_idempotent(self, account_id):
        txn = FiltersTransaction(account_id, render(["Videos"]))
        with pytest.raises(SafetyRefusal):
            with txn:
                pass
        txn.rollback()
        assert not paths.filters_file(account_id).exists()

    def test_the_transaction_is_the_documented_entry_point(self):
        source = Path(filters_mod.__file__).read_text(encoding="utf-8")
        assert "I11" in source
        assert "resynced()" in source


class TestFiltersConfig:

    def test_feeds_the_guard_its_i11_and_i13_inputs(self, account_id):
        with rewrite(account_id, ["Videos"]) as txn:
            txn.resynced()
        cfg = filters_config(account_id, changed=True, resync=True)
        assert cfg["filters_file"] == str(paths.filters_file(account_id))
        assert "- *.partial" in cfg["filters_lines"]
        assert cfg["filters_changed"] is True
        assert cfg["resync"] is True
        assert cfg["extra_args"] == []

    def test_falls_back_to_the_mandatory_block_with_no_file(self, account_id):
        cfg = filters_config(account_id)
        assert cfg["filters_lines"] == list(MANDATORY_EXCLUDES)

    def test_the_guard_accepts_what_this_produces(self, account_id, tmp_path):
        from onedriveui.rc import guards
        with rewrite(account_id, ["Videos"]) as txn:
            txn.resynced()
        local = tmp_path / "OneDrive-Offline"
        local.mkdir()
        guards.assert_bisync_safe(str(local), "onedrive:Offline",
                                  filters_config(account_id))

    def test_the_guard_refuses_a_change_without_a_resync(self, account_id, tmp_path):
        from onedriveui.rc import guards
        with rewrite(account_id, ["Videos"]) as txn:
            txn.resynced()
        local = tmp_path / "OneDrive-Offline"
        local.mkdir()
        with pytest.raises(SafetyRefusal) as caught:
            guards.assert_bisync_safe(
                str(local), "onedrive:Offline",
                filters_config(account_id, changed=True, resync=False))
        assert caught.value.invariant == "I11"


# ═════════════════════════════════════════════════════════════════════════════
# Against the real rclone binary
# ═════════════════════════════════════════════════════════════════════════════

RCLONE = shutil.which("rclone")


@pytest.mark.live
@pytest.mark.skipif(RCLONE is None, reason="rclone is not installed")
class TestAgainstRealRclone:
    """`rclone lsf --filter-from -` is a pure read of a LOCAL temp tree. No
    network, no remote, no rc daemon, and the user's onedrive: is never named."""

    @staticmethod
    def _tree(root: Path) -> None:
        for rel in ("Documents/Work/w.txt", "Documents/Personal/p.txt",
                    "Documents/top.txt", "Pictures/2024/a.jpg", "Videos/v.mp4",
                    "Apps/Backup/b.bin", "My Folder/x.txt", "Weird [name]/y.txt",
                    ".Trash-1000/directorysizes", "root.txt", "desktop.ini",
                    ".DS_Store", "scratch.tmp", "big.bin.deadbeef.partial",
                    "~$draft.docx", "Notebook.one", "Notebook.onetoc2",
                    f"{REMOTE_TRASH_DIR}/gone.txt",
                    f"{REMOTE_VERSIONS_DIR}/old.txt"):
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")

    def _lsf(self, root: Path, text: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [RCLONE, "lsf", "-R", str(root), "--filter-from", "-"],
            input=text, capture_output=True, text=True, timeout=60,
            env={**os.environ, "RCLONE_CONFIG": os.devnull})

    def test_render_survives_rclone_lsf_with_no_parse_error(self, tmp_path):
        """The acceptance bullet."""
        root = tmp_path / "src"
        self._tree(root)
        done = self._lsf(root, render(["Videos", "Apps", "Documents/Personal",
                                       "My Folder", "Weird [name]"]))
        assert done.returncode == 0, done.stderr
        assert "malformed rule" not in done.stderr
        assert "Failed to" not in done.stderr

    def test_it_selects_exactly_the_intended_subset(self, tmp_path):
        root = tmp_path / "src"
        self._tree(root)
        done = self._lsf(root, render(["Videos", "Apps", "Documents/Personal",
                                       "My Folder", "Weird [name]"]))
        selected = {line for line in done.stdout.splitlines() if not line.endswith("/")}
        assert selected == {"root.txt", "Documents/top.txt",
                            "Documents/Work/w.txt", "Pictures/2024/a.jpg"}

    def test_every_mandatory_exclude_actually_excludes(self, tmp_path):
        root = tmp_path / "src"
        self._tree(root)
        done = self._lsf(root, render())
        listed = set(done.stdout.splitlines())
        for junk in ("big.bin.deadbeef.partial", "desktop.ini", ".DS_Store",
                     "scratch.tmp", "~$draft.docx", "Notebook.one",
                     "Notebook.onetoc2"):
            assert junk not in listed
        for pruned in (".Trash-1000/", f"{REMOTE_TRASH_DIR}/",
                       f"{REMOTE_VERSIONS_DIR}/"):
            assert pruned not in listed

    def test_a_bracketed_folder_name_excludes_only_itself(self, tmp_path):
        """Unescaped, `Weird [name]` would be a character class."""
        root = tmp_path / "src"
        self._tree(root)
        (root / "Weird n").mkdir(exist_ok=True)
        (root / "Weird n" / "keep.txt").write_text("x", encoding="utf-8")
        done = self._lsf(root, render(["Weird [name]"]))
        listed = set(done.stdout.splitlines())
        assert "Weird n/keep.txt" in listed
        assert "Weird [name]/y.txt" not in listed

    def test_rclone_rejects_the_malformed_rules_validate_rejects(self, tmp_path):
        """Calibration: `validate()` must not be stricter or laxer than rclone."""
        root = tmp_path / "src"
        self._tree(root)
        for bad in ("-*.partial\n", "*.partial\n"):
            done = self._lsf(root, bad)
            assert done.returncode != 0
            assert "malformed rule" in done.stderr
            with pytest.raises(ConfigError):
                validate(bad.splitlines())

    def test_the_digest_rclone_would_store_is_the_one_we_compute(self, tmp_path):
        """rclone stores `md5sum filters.txt`'s first field, no newline **[V]**."""
        text = render(["Videos"])
        path = tmp_path / "filters.txt"
        path.write_text(text, encoding="utf-8")
        done = subprocess.run(["md5sum", str(path)], capture_output=True,
                              text=True, timeout=30)
        assert done.returncode == 0
        assert done.stdout.split()[0] == md5_of_text(text)


class TestModuleHygiene:

    def test_every_public_name_exists(self):
        for name in filters_mod.__all__:
            assert hasattr(filters_mod, name), name

    def test_no_qt_import(self):
        source = Path(filters_mod.__file__).read_text(encoding="utf-8")
        assert "PySide6" not in source
