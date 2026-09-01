"""WP-03 — `onedriveui/rc/ops.py`.

Every test here pins one of the traps the module exists to encode, and each one
was checked against the live daemon on this machine before it was written:

  * `Path` is relative to `fs`, not to `fs`+`remote`
    (`{"fs":"onedrive:","remote":"AFC"}` really answers `Path: "AFC/Representación"`).
  * `operations/stat` on a missing path is HTTP **200** `{"item": null}` while
    `operations/list` on a missing directory is HTTP **404**.
  * OneDrive directories report `Size: -1`.
  * `operations/fsinfo` answers `Name: "onedrive{MxOuf}"` through a daemon with
    backend overrides — which is exactly what 127.0.0.1:5572 reports here.
  * `operations/uploadfile` takes its parameters in the QUERY STRING and names
    the destination from the multipart `filename=`.
  * `operations/size` and `operations/check` are always `_async`.

`FakeRc` supplies the daemon for everything except `uploadfile`, which is
tested against a real loopback `ThreadingHTTPServer` because what is under test
*is* the wire format. No rclone, no network, no daemon of ours.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import Capabilities, QuotaInfo, RcEndpoint
from onedriveui.rc import ops
from tests.fakes import fake_rc as fake_rc_module

FS = "onedrive:"


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clear_capability_cache():
    """The fsinfo cache is module-global; no test may see another's answer."""
    ops.invalidate_capabilities()
    yield
    ops.invalidate_capabilities()


@pytest.fixture
def rc(fake_rc, monkeypatch):
    """`ops.call_blocking` routed into the fake daemon's registry."""
    monkeypatch.setattr(ops, "call_blocking", fake_rc_module.call_blocking)
    return fake_rc


@pytest.fixture
def ep(rc) -> RcEndpoint:
    return rc.endpoint


@pytest.fixture
def tree(rc):
    """A small remote: Docs/a.txt, Docs/Sub/, Docs/Sub/b.txt, and a root file."""
    rc.add_file("top.txt", size=11)
    rc.add_file("Docs/a.txt", size=6)
    rc.add_file("Docs/Sub", is_dir=True)
    rc.add_file("Docs/Sub/b.txt", size=7)
    return rc


# ═════════════════════════════════════════════════════════════════════════════
# Trap 1 — `Path` is relative to `fs`, NOT to `fs` + `remote`
# ═════════════════════════════════════════════════════════════════════════════

def test_list_dir_builds_rel_path_from_name_not_from_path(tree, ep):
    """The headline acceptance criterion.

    `list_dir(fs="onedrive:", remote="Docs")` must produce `Docs/a.txt`. A naive
    implementation that prefixes `remote` onto the returned `Path` produces
    `Docs/Docs/a.txt`, which is the bug this whole function exists to prevent.
    """
    rows = ops.list_dir(fs=FS, remote="Docs", ep=ep)
    by_name = {row.name: row for row in rows}
    assert set(by_name) == {"a.txt", "Sub"}
    assert by_name["a.txt"].rel_path == "Docs/a.txt"
    assert by_name["Sub"].rel_path == "Docs/Sub"
    # The trap, stated as an assertion rather than a comment.
    assert "Docs/Docs" not in by_name["a.txt"].rel_path


def test_rel_path_for_survives_a_backend_that_returns_a_relative_path():
    """A `Path` relative to `remote` (which rclone does not do, but a wrapper
    backend could) must still come out right, because the row is built from
    `Name` and `Path` only contributes the sub-directory."""
    assert ops.rel_path_for("Docs", {"Name": "a.txt", "Path": "a.txt"}) == "Docs/a.txt"
    assert ops.rel_path_for("Docs", {"Name": "a.txt",
                                     "Path": "Docs/a.txt"}) == "Docs/a.txt"


def test_rel_path_for_keeps_the_sub_directory_of_a_recursive_row():
    assert ops.rel_path_for("Docs", {"Name": "b.txt",
                                     "Path": "Docs/Sub/b.txt"}) == "Docs/Sub/b.txt"
    assert ops.rel_path_for("", {"Name": "b.txt",
                                 "Path": "Sub/b.txt"}) == "Sub/b.txt"


def test_rel_path_for_at_the_root_is_the_bare_name():
    assert ops.rel_path_for("", {"Name": "top.txt", "Path": "top.txt"}) == "top.txt"


def test_list_dir_recursive_keeps_the_full_sub_path(tree, ep):
    rows = ops.list_dir(fs=FS, remote="Docs", ep=ep, recurse=True)
    assert "Docs/Sub/b.txt" in {row.rel_path for row in rows}


def test_list_dir_at_the_root_needs_no_prefix(tree, ep):
    rows = ops.list_dir(fs=FS, ep=ep)
    assert {row.rel_path for row in rows} == {"top.txt"}


# ═════════════════════════════════════════════════════════════════════════════
# Trap 2 — stat says 200/None, list says 404
# ═════════════════════════════════════════════════════════════════════════════

def test_stat_on_a_missing_path_returns_none(tree, ep):
    """HTTP 200 with `{"item": null}` — absence is a value, not an error."""
    assert ops.stat(FS, "Docs/nope.txt", ep=ep) is None
    assert ops.stat(FS, "no/such/dir", ep=ep) is None


def test_list_dir_on_a_missing_directory_raises_is_not_found(tree, ep):
    with pytest.raises(RcError) as excinfo:
        ops.list_dir(fs=FS, remote="no-such-dir", ep=ep)
    assert excinfo.value.is_not_found
    assert excinfo.value.status == 404


def test_stat_returns_the_requested_path(tree, ep):
    node = ops.stat(FS, "Docs/a.txt", ep=ep)
    assert node is not None
    assert node.rel_path == "Docs/a.txt"
    assert node.name == "a.txt"
    assert node.size == 6
    assert node.is_dir is False


def test_parse_stat_of_a_null_item_is_none():
    assert ops.parse_stat({"item": None}) is None
    assert ops.parse_stat({}) is None
    assert ops.parse_stat(None) is None


# ═════════════════════════════════════════════════════════════════════════════
# Trap 3 — OneDrive directories report Size: -1
# ═════════════════════════════════════════════════════════════════════════════

def test_directories_report_minus_one_and_it_is_preserved(tree, ep):
    rows = ops.list_dir(fs=FS, remote="Docs", ep=ep)
    folder = next(row for row in rows if row.is_dir)
    assert folder.size == -1, "a -1 must reach the UI so it can render blank"


def test_a_row_without_a_size_key_still_reads_minus_one():
    node = ops.node_from_row({"Name": "x", "IsDir": True})
    assert node.size == -1


# ═════════════════════════════════════════════════════════════════════════════
# Trap 4 — Capabilities.name strips the {HASH} suffix
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("raw,expected", [
    ("onedrive{MxOuf}", "onedrive"),
    ("onedrive{MxOuf}:", "onedrive:"),
    ("onedrive", "onedrive"),
    ("onedrive:", "onedrive:"),
    ("", ""),
])
def test_strip_hash_suffix(raw, expected):
    assert ops.strip_hash_suffix(raw) == expected


def test_capabilities_strips_the_hash_suffix_before_display(rc, ep):
    """The live mount on this machine reports `onedrive{MxOuf}` because it was
    started with `--onedrive-chunk-size 30M` — the exact failure invariant I1
    exists to prevent. The suffix must never reach the UI."""
    rc.fsinfo["Name"] = "onedrive{MxOuf}"
    caps = ops.capabilities(FS, ep=ep)
    assert caps.name == "onedrive"
    assert isinstance(caps, Capabilities)


def test_capabilities_reports_onedrives_real_feature_set(rc, ep):
    caps = ops.capabilities(FS, ep=ep)
    assert caps.list_r is False, "OneDrive has ListR=false; --fast-list is a no-op"
    assert caps.public_link is True
    assert caps.change_notify is True
    assert caps.case_insensitive is True
    assert caps.has("About") is True
    assert caps.has("SetTier") is False
    assert caps.hashes == ("quickxor",)
    assert caps.precision_ns == 1_000_000_000


def test_capabilities_are_cached_per_execute_id(rc, ep):
    ops.capabilities(FS, ep=ep)
    ops.capabilities(FS, ep=ep)
    assert rc.count("operations/fsinfo") == 1
    ops.capabilities(FS, ep=ep, use_cache=False)
    assert rc.count("operations/fsinfo") == 2


def test_capabilities_cache_is_keyed_by_execute_id(rc, ep):
    ops.capabilities(FS, ep=ep)
    rc.restart()
    ops.capabilities(FS, ep=rc.endpoint)
    assert rc.count("operations/fsinfo") == 2, (
        "a restarted daemon has a new executeId and must not be served a "
        "cached answer")


def test_invalidate_capabilities_drops_the_cache(rc, ep):
    ops.capabilities(FS, ep=ep)
    assert ops.invalidate_capabilities(ep) == 1
    ops.capabilities(FS, ep=ep)
    assert rc.count("operations/fsinfo") == 2


def test_supports_is_false_when_the_probe_itself_fails(rc, ep):
    rc.fail("operations/fsinfo", status=500, message="boom")
    assert ops.supports(FS, "PublicLink", ep=ep) is False


def test_supports_gates_settier_off_for_onedrive(rc, ep):
    assert ops.supports(FS, "SetTier", ep=ep) is False
    assert ops.supports(FS, "Purge", ep=ep) is True


# ═════════════════════════════════════════════════════════════════════════════
# Trap 5 — size() and check() are ALWAYS async
# ═════════════════════════════════════════════════════════════════════════════

def test_size_is_always_async(rc, ep):
    handle = ops.size("onedrive:Docs", ep=ep, group="onedriveui/size/acc",
                      label="Docs")
    record = rc.last("operations/size")
    assert record is not None and record.async_ is True, (
        "OneDrive has ListR=false: a synchronous size walk holds an HTTP "
        "request open for minutes")
    assert record.params["_group"] == "onedriveui/size/acc"
    assert handle.job_id > 0
    assert handle.execute_id == rc.execute_id
    assert handle.group == "onedriveui/size/acc"
    assert handle.path == "operations/size"
    assert handle.label == "Docs"


def test_check_is_always_async(rc, ep):
    handle = ops.check("/home/u/OneDrive", FS, ep=ep,
                       group="onedriveui/check/acc")
    record = rc.last("operations/check")
    assert record is not None and record.async_ is True
    assert record.params["srcFs"] == "/home/u/OneDrive"
    assert record.params["dstFs"] == FS
    assert handle.group == "onedriveui/check/acc"


def test_a_job_without_a_group_records_rclones_invented_name(rc, ep):
    handle = ops.size(FS, ep=ep)
    assert handle.group == f"job/{handle.job_id}"


def test_size_without_a_jobid_is_an_error(rc, ep, monkeypatch):
    """A daemon that answered inline means `_async` did not take: that has to
    surface, not be mistaken for a job with id 0."""
    monkeypatch.setattr(ops, "call_blocking",
                        lambda *a, **kw: {"count": 1, "bytes": 2})
    with pytest.raises(RcError, match="without a jobid"):
        ops.size(FS, ep=ep)


def test_parse_size_and_parse_check_decode_the_job_output():
    assert ops.parse_size({"bytes": 8388620, "count": 3, "sizeless": 0}) == (
        ops.SizeResult(bytes=8388620, count=3, sizeless=0))
    result = ops.parse_check({
        "success": False, "status": "6 differences found", "hashType": "md5",
        "combined": ["+ big.bin", "= a.txt"], "match": ["a.txt"],
        "differ": [], "error": [],
        "missingOnDst": ["big.bin", "sub/b.txt"], "missingOnSrc": [],
    })
    assert result.success is False
    assert result.hash_type == "md5"
    assert result.missing_on_dst == ("big.bin", "sub/b.txt")
    assert result.match == ("a.txt",)
    assert result.differences == 2


def test_parse_check_of_an_empty_body_is_all_empty():
    result = ops.parse_check({})
    assert result.combined == () and result.errors == ()
    assert result.differences == 0


# ═════════════════════════════════════════════════════════════════════════════
# operations/about
# ═════════════════════════════════════════════════════════════════════════════

def test_about_returns_a_quota(rc, ep):
    quota = ops.about(FS, ep=ep)
    assert isinstance(quota, QuotaInfo)
    assert quota.total == 1_104_880_336_896
    assert quota.used == 252_544_077_005
    assert quota.free == 852_336_259_891
    assert quota.sampled_at


def test_about_tolerates_a_backend_that_omits_keys(rc, ep):
    """The local backend omits `trashed`; OneDrive omits `other` and `objects`."""
    rc.set_quota(total=100, used=40, trashed=None)
    quota = ops.about(FS, ep=ep)
    assert (quota.total, quota.used, quota.free, quota.trashed) == (100, 40, 60, 0)


def test_parse_about_of_an_empty_body_is_all_zero():
    quota = ops.parse_about({})
    assert (quota.total, quota.used, quota.free, quota.trashed) == (0, 0, 0, 0)
    assert quota.pct == 0.0


def test_about_propagates_an_auth_failure(rc, ep):
    rc.auth_error = "couldn't fetch token: invalid_grant"
    with pytest.raises(RcError):
        ops.about(FS, ep=ep)


def test_about_on_a_dead_daemon_raises_daemon_unavailable(rc, ep):
    rc.stop()
    with pytest.raises(DaemonUnavailable):
        ops.about(FS, ep=ep)


# ═════════════════════════════════════════════════════════════════════════════
# Directory and file mutations
# ═════════════════════════════════════════════════════════════════════════════

def test_mkdir_purge_and_deletefile_send_fs_and_remote(rc, ep):
    ops.mkdir(FS, "New/Folder", ep=ep)
    ops.purge(FS, "Old", ep=ep)
    ops.deletefile(FS, "Old/x.txt", ep=ep)
    ops.rmdir(FS, "Empty", ep=ep)
    assert rc.last("operations/mkdir").params == {"fs": FS, "remote": "New/Folder"}
    assert rc.last("operations/purge").params == {"fs": FS, "remote": "Old"}
    assert rc.last("operations/deletefile").params["remote"] == "Old/x.txt"
    assert rc.last("operations/rmdir").params["remote"] == "Empty"


def test_rmdirs_carries_leave_root(rc, ep):
    rc.set("operations/rmdirs", {})
    ops.rmdirs(FS, "Tree", ep=ep, leave_root=False)
    assert rc.last("operations/rmdirs").params["leaveRoot"] is False


def test_leading_and_trailing_slashes_are_normalised(rc, ep):
    ops.mkdir(FS, "/New/Folder/", ep=ep)
    assert rc.last("operations/mkdir").params["remote"] == "New/Folder"


def test_copyfile_and_movefile_use_the_four_key_form(rc, ep):
    ops.copyfile(FS, "a.txt", FS, "b.txt", ep=ep)
    ops.movefile(FS, "b.txt", FS, "c.txt", ep=ep)
    copy = rc.last("operations/copyfile").params
    assert copy == {"srcFs": FS, "srcRemote": "a.txt",
                    "dstFs": FS, "dstRemote": "b.txt"}
    move = rc.last("operations/movefile").params
    assert move["srcRemote"] == "b.txt" and move["dstRemote"] == "c.txt"


# ═════════════════════════════════════════════════════════════════════════════
# operations/publiclink
# ═════════════════════════════════════════════════════════════════════════════

def test_publiclink_returns_the_url(rc, ep):
    url = ops.publiclink(FS, "Docs/a.txt", ep=ep, expire="24h")
    assert url.startswith("https://1drv.ms/")
    params = rc.last("operations/publiclink").params
    assert params["expire"] == "24h"
    assert params["unlink"] is False


def test_publiclink_warns_when_asked_to_unlink(rc, ep, caplog):
    """Verified in the v1.75.0 source: the OneDrive backend accepts `unlink` and
    never reads it, so `unlink=True` CREATES a link. "Remove link" must never be
    wired to this."""
    with caplog.at_level("WARNING", logger="onedriveui.rc.ops"):
        ops.publiclink(FS, "Docs/a.txt", ep=ep, unlink=True)
    assert any("ignores that flag" in record.message for record in caplog.records)


# ═════════════════════════════════════════════════════════════════════════════
# settier, hashsum, core/version, core/bwlimit
# ═════════════════════════════════════════════════════════════════════════════

def test_settier_fails_on_onedrive(rc, ep):
    rc.fail("operations/settier", status=500,
            message="remote onedrive does not support settier")
    with pytest.raises(RcError) as excinfo:
        ops.settier(FS, "archive", ep=ep)
    assert "does not support settier" in excinfo.value.message
    assert rc.last("operations/settier").params == {"fs": FS, "tier": "archive"}


def test_hashsum_parses_the_two_space_separated_lines(rc, ep):
    rc.set("operations/hashsum", {
        "hashType": "quickxor",
        "hashsum": ["d166c83af5c6  a.txt", "aabbccddeeff  my report.docx"]})
    sums = ops.hashsum(FS, ep=ep)
    assert sums == {"a.txt": "d166c83af5c6",
                    "my report.docx": "aabbccddeeff"}
    assert rc.last("operations/hashsum").params["hashType"] == "quickxor"


def test_hashsumfile_returns_one_hash(rc, ep):
    rc.set("operations/hashsumfile", {"hashType": "quickxor", "hash": "beef"})
    assert ops.hashsumfile(FS, "a.txt", ep=ep) == "beef"


def test_core_version_gates_on_decomposed(rc, ep):
    info = ops.core_version(ep=ep)
    assert info.version == "v1.75.0"
    assert info.decomposed == (1, 75, 0)
    assert info.at_least(1, 75, 0) is True
    assert info.at_least(1, 76) is False
    assert info.os == "linux"


def test_version_without_decomposed_is_never_good_enough():
    assert ops.parse_version({"version": "v1.75.0"}).at_least(1, 0) is False


def test_set_bwlimit_echoes_binary_units(rc, ep):
    """`1M:100k` comes back as `1Mi:100Ki`, so the echo can never be
    string-compared with the request."""
    limit = ops.set_bwlimit("1M:100k", ep=ep)
    assert limit.rate == "1Mi:100Ki"
    assert limit.rate != "1M:100k"
    assert limit.tx == 1024 * 1024, "Tx is UPLOAD"
    assert limit.rx == 100 * 1024, "Rx is DOWNLOAD"
    assert limit.unlimited is False


def test_get_bwlimit_queries_without_a_rate(rc, ep):
    limit = ops.get_bwlimit(ep=ep)
    assert limit.rate == "off"
    assert limit.unlimited is True
    assert "rate" not in rc.last("core/bwlimit").params


def test_set_bwlimit_off_is_unlimited(rc, ep):
    assert ops.set_bwlimit("off", ep=ep).unlimited is True


# ═════════════════════════════════════════════════════════════════════════════
# Metadata columns
# ═════════════════════════════════════════════════════════════════════════════

def test_metadata_supplies_the_modified_by_and_malware_columns(rc, ep):
    rc.add_file("Docs/clean.txt", size=1)
    rc.add_file("Docs/bad.exe", size=2, malware=True)
    rows = {row.name: row for row in ops.list_dir(fs=FS, remote="Docs", ep=ep)}
    assert rows["clean.txt"].created_by == "Test User"
    assert rows["clean.txt"].modified_by == "Test User"
    assert rows["clean.txt"].malware_detected is False
    assert rows["bad.exe"].malware_detected is True


def test_metadata_can_be_turned_off(rc, ep):
    rc.add_file("Docs/clean.txt", size=1)
    rows = ops.list_dir(fs=FS, remote="Docs", ep=ep, metadata=False)
    assert rows[0].modified_by == ""
    assert "metadata" not in (rc.last("operations/list").params.get("opt") or {})


def test_list_options_are_only_sent_when_asked_for(rc, ep):
    rc.add_file("Docs/x", is_dir=True)
    ops.list_dir(fs=FS, remote="Docs", ep=ep, dirs_only=True, no_mod_time=True,
                 show_hash=True, hash_types=("quickxor",))
    opt = rc.last("operations/list").params["opt"]
    assert opt["dirsOnly"] is True
    assert opt["noModTime"] is True
    assert opt["hashTypes"] == ["quickxor"]
    assert "filesOnly" not in opt


def test_hashes_land_on_the_node():
    node = ops.node_from_row(
        {"Name": "a.txt", "Path": "a.txt", "Size": 6,
         "Hashes": {"quickxor": "d166c83a"}}, "")
    assert node.quickxor == "d166c83a"


# ═════════════════════════════════════════════════════════════════════════════
# Trap 6 — operations/uploadfile, against a real HTTP server
# ═════════════════════════════════════════════════════════════════════════════

class _UploadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):                                # noqa: N802 - BaseHTTPRequestHandler
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        server.requests.append({
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "authorization": self.headers.get("Authorization", ""),
            "body": self.rfile.read(length) if length else b"",
        })
        payload = json.dumps(server.reply).encode()
        self.send_response(server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Rclone-Jobid", "7")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upload_server():
    """A loopback server that records exactly what `uploadfile` puts on the wire."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), _UploadHandler)
    server.requests = []
    server.reply = {}
    server.status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, RcEndpoint(kind="rcd", host="127.0.0.1", port=port,
                                 user="onedriveui", password="s3cret")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_uploadfile_puts_its_parameters_in_the_query_string(upload_server, tmp_path):
    """The body is the multipart payload, so a JSON body would be ignored: fs,
    remote and _group all have to travel in the URL."""
    server, endpoint = upload_server
    source = tmp_path / "report.docx"
    source.write_bytes(b"hello world")

    rel = ops.uploadfile(source, FS, "Docs", ep=endpoint,
                         group="onedriveui/upload/acc")

    assert rel == "Docs/report.docx"
    request = server.requests[-1]
    path, _, query = request["path"].partition("?")
    assert path == "/operations/uploadfile"
    assert urllib.parse.parse_qs(query) == {
        "fs": [FS], "remote": ["Docs"], "_group": ["onedriveui/upload/acc"]}


def test_uploadfile_names_the_destination_from_the_multipart_filename(
        upload_server, tmp_path):
    """`-F "f1=@a.txt"` writes `a.txt`, not `f1`: the name comes from
    `filename=`, never from the field name."""
    server, endpoint = upload_server
    source = tmp_path / "local-name.bin"
    source.write_bytes(b"\x00\x01\x02\x03")

    ops.uploadfile(source, FS, "", ep=endpoint, name="Wanted Name.bin")

    body = server.requests[-1]["body"].decode("utf-8", "replace")
    assert 'name="file"' in body
    assert 'filename="Wanted Name.bin"' in body
    assert "local-name.bin" not in body


def test_uploadfile_streams_the_bytes_verbatim(upload_server, tmp_path):
    server, endpoint = upload_server
    payload = bytes(range(256)) * 40
    source = tmp_path / "blob.bin"
    source.write_bytes(payload)

    ops.uploadfile(source, FS, "Docs", ep=endpoint)

    body = server.requests[-1]["body"]
    assert payload in body
    assert body.startswith(b"------OneDriveUI")
    assert body.rstrip().endswith(b"--")
    assert server.requests[-1]["content_type"].startswith(
        "multipart/form-data; boundary=")


def test_uploadfile_attaches_basic_auth(upload_server, tmp_path):
    """`--rc-no-auth` exempts nothing in v1.75.0, so credentials always go."""
    server, endpoint = upload_server
    source = tmp_path / "a.txt"
    source.write_text("x")
    ops.uploadfile(source, FS, "", ep=endpoint)
    assert server.requests[-1]["authorization"].startswith("Basic ")


def test_uploadfile_refuses_a_name_with_a_separator(upload_server, tmp_path):
    _server, endpoint = upload_server
    source = tmp_path / "a.txt"
    source.write_text("x")
    with pytest.raises(ValueError, match="leaf"):
        ops.uploadfile(source, FS, "Docs", ep=endpoint, name="sub/a.txt")


def test_uploadfile_raises_rcerror_on_a_rejection(upload_server, tmp_path):
    server, endpoint = upload_server
    server.status = 500
    server.reply = {"error": "directory not found", "input": {},
                    "path": "operations/uploadfile", "status": 500}
    source = tmp_path / "a.txt"
    source.write_text("x")
    with pytest.raises(RcError) as excinfo:
        ops.uploadfile(source, FS, "Nope", ep=endpoint)
    assert excinfo.value.message == "directory not found"


def test_uploadfile_reports_an_unreachable_daemon(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    endpoint = RcEndpoint(kind="rcd", host="127.0.0.1", port=port)
    source = tmp_path / "a.txt"
    source.write_text("x")
    with pytest.raises(DaemonUnavailable):
        ops.uploadfile(source, FS, "", ep=endpoint, timeout_s=2.0)


def test_uploadfile_lets_a_local_file_error_through(upload_server, tmp_path):
    """An unreadable source is the caller's problem and must not be re-labelled
    "the daemon is unreachable"."""
    _server, endpoint = upload_server
    with pytest.raises(OSError):
        ops.uploadfile(tmp_path / "missing.bin", FS, "", ep=endpoint)


def test_quote_filename_escapes_quotes_and_strips_newlines():
    assert ops._quote_filename('a"b') == 'a\\"b'
    assert ops._quote_filename("a\\b") == "a\\\\b"
    assert ops._quote_filename("a\r\nb") == "ab"


# ═════════════════════════════════════════════════════════════════════════════
# Live smoke — READ-ONLY against this machine's real OneDrive
# ═════════════════════════════════════════════════════════════════════════════

LIVE_RC = RcEndpoint(kind="mount", host="127.0.0.1", port=5572)


def _live_or_skip() -> RcEndpoint:
    from onedriveui.rc.client import is_alive

    if not is_alive(LIVE_RC, timeout_s=1.0):
        pytest.skip("no rclone rc on 127.0.0.1:5572")
    return LIVE_RC


@pytest.mark.live
def test_live_about_returns_a_real_quota():
    """WP-03 acceptance: `about("onedrive:")` returns a QuotaInfo whose
    total/used/free are non-zero.

    Read-only against the user's own pre-existing `rclone mount --rc` daemon:
    `operations/about` mutates nothing, and nothing here reconfigures, restarts
    or writes to that process or to the remote.
    """
    endpoint = _live_or_skip()
    quota = ops.about("onedrive:", ep=endpoint, timeout_s=20.0)
    assert quota.total > 0
    assert quota.used > 0
    assert quota.free > 0
    assert quota.used + quota.free <= quota.total * 1.01
    assert 0.0 < quota.pct < 100.0


@pytest.mark.live
def test_live_fsinfo_reports_the_hash_suffixed_name():
    """The live daemon was started with `--onedrive-chunk-size 30M`, so it
    really does answer `Name: "onedrive{MxOuf}"`. Read-only."""
    endpoint = _live_or_skip()
    caps = ops.capabilities("onedrive:", ep=endpoint, timeout_s=20.0)
    assert caps.name == "onedrive", "the {HASH} suffix must be stripped"
    assert caps.list_r is False
    assert caps.public_link is True
    assert "quickxor" in caps.hashes


@pytest.mark.live
def test_live_list_path_is_relative_to_fs_not_to_remote():
    """The trap, proved against the real remote. Read-only listing."""
    endpoint = _live_or_skip()
    top = ops.list_dir(fs="onedrive:", ep=endpoint, dirs_only=True,
                       metadata=False, timeout_s=30.0)
    if not top:
        pytest.skip("the live OneDrive root has no directories")
    folder = top[0]
    assert folder.rel_path == folder.name
    assert folder.size == -1, "OneDrive directories report Size: -1"
    children = ops.list_dir(fs="onedrive:", remote=folder.name, ep=endpoint,
                            metadata=False, timeout_s=30.0)
    for child in children:
        assert child.rel_path == f"{folder.name}/{child.name}"
        assert not child.rel_path.startswith(f"{folder.name}/{folder.name}/")


@pytest.mark.live
def test_live_stat_of_a_missing_path_is_none_while_list_is_404():
    """The two conventions really do disagree. Read-only."""
    endpoint = _live_or_skip()
    missing = "__onedriveui_no_such_path__"
    assert ops.stat("onedrive:", missing, ep=endpoint, timeout_s=20.0) is None
    with pytest.raises(RcError) as excinfo:
        ops.list_dir(fs="onedrive:", remote=missing, ep=endpoint, timeout_s=20.0)
    assert excinfo.value.status == 404
