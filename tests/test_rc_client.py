"""WP-02 — `onedriveui/rc/client.py`.

`RcClient` is the one module that cannot be proved against a dict, because the
thing being tested *is* the HTTP behaviour: POST-only, basic auth, the
`X-Rclone-Jobid` header, the 4-key error envelope, the timeout, and the
`deleteLater()` on every reply. So these tests run a real
`ThreadingHTTPServer` on a probed loopback port that speaks exactly what rclone
v1.75.0 speaks, including its quirks. No network, no rclone, no daemon.

`JobWatcher` is driven through the WP-00 `fake_rc` fake instead, because what it
needs is a daemon whose `executeId` can be changed and whose jobs can be expired
on command.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtCore import QEventLoop, QTimer

from onedriveui.constants import OFFLINE_FAILURE_THRESHOLD, RC_TIMEOUT_S
from onedriveui.errors import DaemonUnavailable, RcError, SafetyRefusal
from onedriveui.models import JobHandle, RcEndpoint
from onedriveui.rc.client import (
    JOBID_HEADER,
    JobWatcher,
    RcClient,
    build_params,
    call_blocking,
    is_alive,
)

USER, PASSWORD = "onedriveui", "s3cret-for-tests"


# ═════════════════════════════════════════════════════════════════════════════
# A loopback rc server that reproduces rclone v1.75.0's wire behaviour
# ═════════════════════════════════════════════════════════════════════════════

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):          # keep pytest output clean
        pass

    # rclone answers GET/HEAD on an rc path with 404 and PUT with 405.
    def do_GET(self):
        self._plain(404, "Not Found")

    def do_PUT(self):
        self._plain(405, "Method Not Allowed")

    def do_POST(self):
        server = self.server
        server.requests.append(self.path)
        auth = self.headers.get("Authorization", "")
        server.auth_seen.append(auth)
        if not server.no_auth and auth != _basic(USER, PASSWORD):
            self.send_response(401)
            self.send_header("Www-Authenticate", 'Basic realm=""')
            self._body(b"401 Unauthorized", "text/plain")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            params = json.loads(raw or b"{}")
        except ValueError:
            params = {}
        server.bodies.append(params)

        path = self.path.lstrip("/")
        if server.hang_for and path in server.hang_for:
            # Accept the request and never answer: the client's own deadline is
            # the only thing that can end this.
            server.hanging.set()
            server.release.wait(timeout=30)
            return

        server.jobid += 1
        handler = server.routes.get(path)
        if handler is None:
            self._json(404, {"error": f'couldn\'t find method "{path}"',
                             "input": params, "path": path, "status": 404})
            return
        status, payload = handler(params)
        self._json(status, payload)

    # ── wire helpers ────────────────────────────────────────────────────────

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header(JOBID_HEADER, str(self.server.jobid))
        self.send_header("Server", "rclone/v1.75.0")
        self._body(json.dumps(payload).encode(), "application/json")

    def _plain(self, status, text):
        self.send_response(status)
        self._body(text.encode(), "text/plain")

    def _body(self, data: bytes, content_type: str):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class _RcServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """The timeout tests abort mid-request on purpose; the resulting broken
        pipe is the expected outcome, not a traceback worth printing."""


@pytest.fixture
def rc_server():
    """A live loopback rc server plus the `RcEndpoint` that reaches it.

    Bound on an OS-chosen ephemeral port rather than one out of
    `RC_PORT_RANGE`: this fixture is about the client, and taking 17800 for the
    length of every test here would collide with whatever else in the suite is
    exercising the port picker.
    """
    server = _RcServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    server.requests = []
    server.bodies = []
    server.auth_seen = []
    server.jobid = 0
    server.no_auth = False
    server.hang_for = set()
    server.hanging = threading.Event()
    server.release = threading.Event()
    server.routes = {
        "rc/noop": lambda params: (200, params),
        "core/version": lambda params: (200, {"version": "v1.75.0",
                                              "decomposed": [1, 75, 0]}),
        "core/pid": lambda params: (200, {"pid": 4242}),
        "core/stats": lambda params: (200, {"bytes": 0, "eta": None,
                                            "errors": 0, "speed": 0}),
        "job/list": lambda params: (200, {"executeId": "exec-1", "jobids": [1],
                                          "runningIds": [], "finishedIds": [1]}),
        "sync/copy": lambda params: (200, {"jobid": 37, "executeId": "exec-1"}),
        "rc/error": lambda params: (
            500, {"error": "arbitrary error", "input": params,
                  "path": "rc/error", "status": 500}),
        "operations/list": lambda params: (
            404, {"error": "error in ListJSON: directory not found",
                  "input": params, "path": "operations/list", "status": 404}),
        "echo": lambda params: (200, params),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.endpoint = RcEndpoint(kind="rcd", host="127.0.0.1", port=port,
                                 user=USER, password=PASSWORD)
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def dead_endpoint():
    """A loopback port with nothing listening on it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return RcEndpoint(kind="rcd", host="127.0.0.1", port=port,
                      user=USER, password=PASSWORD)


def _await(qtbot, call, timeout_ms: int = 5000):
    """Pump the event loop until `call` delivers, and return (result, error)."""
    outcome: dict = {}
    loop = QEventLoop()
    call.succeeded.connect(lambda body: (outcome.update(result=body), loop.quit()))
    call.failed.connect(lambda err: (outcome.update(error=err), loop.quit()))
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(timeout_ms)
    if not call.delivered:
        loop.exec()
    return outcome.get("result"), outcome.get("error")


# ═════════════════════════════════════════════════════════════════════════════
# Parameter assembly
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildParams:
    def test_the_four_special_parameters_are_merged(self):
        body = build_params({"srcFs": "/a", "dstFs": "/b"},
                            group="sync/Docs", async_=True,
                            config={"Transfers": 4}, filt={"MaxSize": "250G"})
        assert body == {
            "srcFs": "/a", "dstFs": "/b", "_group": "sync/Docs", "_async": True,
            "_config": {"Transfers": 4}, "_filter": {"MaxSize": "250G"},
        }

    def test_nothing_is_added_when_nothing_is_asked_for(self):
        assert build_params({"fs": "onedrive:"}) == {"fs": "onedrive:"}
        assert build_params(None) == {}

    def test_async_false_does_not_emit_the_key(self):
        """rclone treats `_async: false` as present; omitting it is not the same
        as sending false, and a synchronous call must stay synchronous."""
        assert "_async" not in build_params({}, async_=False)

    def test_an_explicit_key_wins_over_the_keyword(self):
        body = build_params({"_group": "mine"}, group="theirs")
        assert body["_group"] == "mine"

    def test_the_caller_s_dict_is_not_mutated(self):
        original = {"fs": "onedrive:"}
        build_params(original, group="g", config={"Transfers": 4})
        assert original == {"fs": "onedrive:"}

    def test_the_config_and_filter_blocks_are_copied(self):
        config = {"Transfers": 4}
        body = build_params({}, config=config)
        config["Transfers"] = 99
        assert body["_config"]["Transfers"] == 4


# ═════════════════════════════════════════════════════════════════════════════
# The asynchronous client
# ═════════════════════════════════════════════════════════════════════════════

class TestRcClient:
    def test_a_successful_call_delivers_the_parsed_body(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        result, error = _await(qtbot, client.call("core/version"))
        assert error is None
        assert result["version"] == "v1.75.0"
        client.close()

    def test_every_call_is_a_post(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        _await(qtbot, client.call("rc/noop"))
        assert rc_server.requests == ["/rc/noop"]
        client.close()

    def test_the_body_is_json_so_booleans_survive(self, qtbot, rc_server):
        """Form and query parameters arrive as strings; `_async: true` would
        become the string "true" and be ignored."""
        client = RcClient(rc_server.endpoint)
        _await(qtbot, client.call("echo", {"n": 7}, async_=True,
                                  group="g", config={"DryRun": True}))
        body = rc_server.bodies[-1]
        assert body["n"] == 7 and body["_async"] is True
        assert body["_config"] == {"DryRun": True}
        client.close()

    def test_basic_auth_is_attached_to_every_request(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        _await(qtbot, client.call("rc/noop"))
        assert rc_server.auth_seen[-1] == _basic(USER, PASSWORD)
        client.close()

    def test_a_wrong_password_produces_an_rc_error_not_a_hang(self, qtbot, rc_server):
        bad = RcEndpoint(kind="rcd", host="127.0.0.1", port=rc_server.endpoint.port,
                         user=USER, password="wrong")
        client = RcClient(bad)
        result, error = _await(qtbot, client.call("rc/noop"))
        assert result is None
        assert isinstance(error, RcError) and error.status == 401
        client.close()

    def test_the_jobid_header_is_read_off_every_reply(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        call = client.call("rc/noop")
        _await(qtbot, call)
        assert call.jobid >= 1
        assert call.header(JOBID_HEADER) == str(call.jobid)
        assert call.header("x-rclone-jobid") == str(call.jobid)
        client.close()

    def test_the_four_key_error_envelope_is_parsed(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        _result, error = _await(qtbot, client.call("rc/error", {"parameter": "BAD"}))
        assert isinstance(error, RcError)
        assert error.status == 500
        assert error.message == "arbitrary error"
        assert error.body["input"] == {"parameter": "BAD"}
        assert error.body["path"] == "rc/error"
        assert error.body["status"] == 500
        client.close()

    def test_a_404_is_recognised_as_not_found(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        _result, error = _await(qtbot, client.call(
            "operations/list", {"fs": "onedrive:", "remote": "nope"}))
        assert isinstance(error, RcError) and error.is_not_found
        client.close()

    def test_an_async_reply_becomes_a_job_handle(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        call = client.call("sync/copy", {"srcFs": "/a", "dstFs": "/b"},
                           async_=True, group="sync/Docs")
        _await(qtbot, call)
        handle = call.handle(label="Copy Docs")
        assert isinstance(handle, JobHandle)
        assert (handle.job_id, handle.execute_id) == (37, "exec-1")
        assert handle.group == "sync/Docs" and handle.path == "sync/copy"
        assert handle.label == "Copy Docs"
        client.close()

    def test_a_synchronous_reply_has_no_job_handle(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        call = client.call("core/version")
        _await(qtbot, call)
        assert call.handle() is None
        client.close()

    def test_a_dead_daemon_produces_daemon_unavailable_not_a_crash(
            self, qtbot, dead_endpoint):
        client = RcClient(dead_endpoint)
        result, error = _await(qtbot, client.call("rc/noop"))
        assert result is None
        assert isinstance(error, DaemonUnavailable)
        assert error.status == 503
        client.close()

    def test_a_hung_daemon_is_abandoned_at_the_timeout(self, qtbot, rc_server):
        rc_server.hang_for = {"core/stats"}
        client = RcClient(rc_server.endpoint)
        _result, error = _await(
            qtbot, client.call("core/stats", timeout_s=0.25), timeout_ms=6000)
        assert isinstance(error, DaemonUnavailable)
        assert "timed out" in error.message
        client.close()

    def test_the_default_timeout_is_the_frozen_four_seconds(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        call = client.call("rc/noop")
        assert call._timer.interval() == int(RC_TIMEOUT_S * 1000) == 4000
        _await(qtbot, call)
        client.close()

    def test_every_reply_is_deleted(self, qtbot, rc_server):
        """A leaked QNetworkReply holds its socket and its buffer for the life of
        the process; there is a `deleteLater()` on the single exit path."""
        from shiboken6 import isValid

        replies = []
        client = RcClient(rc_server.endpoint)
        client._nam.finished.connect(replies.append)
        for _ in range(5):
            _await(qtbot, client.call("rc/noop"))
        qtbot.wait(60)
        qtbot.process(3)
        assert len(replies) == 5
        assert [isValid(reply) for reply in replies] == [False] * 5
        client.close()

    def test_exactly_one_signal_fires_per_call(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        seen = []
        call = client.call("rc/noop")
        call.succeeded.connect(lambda body: seen.append("ok"))
        call.failed.connect(lambda err: seen.append("fail"))
        _await(qtbot, call)
        qtbot.wait(50)
        assert seen == ["ok"]
        client.close()

    def test_close_makes_later_calls_fail_rather_than_reach_the_daemon(
            self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        client.close()
        before = len(rc_server.requests)
        _result, error = _await(qtbot, client.call("rc/noop"))
        assert isinstance(error, DaemonUnavailable)
        assert "closed" in error.message
        assert len(rc_server.requests) == before

    def test_close_is_idempotent(self, rc_server):
        client = RcClient(rc_server.endpoint)
        client.close()
        client.close()

    def test_set_endpoint_repoints_the_client(self, qtbot, rc_server, dead_endpoint):
        client = RcClient(dead_endpoint)
        client.set_endpoint(rc_server.endpoint)
        assert client.endpoint == rc_server.endpoint
        result, error = _await(qtbot, client.call("core/version"))
        assert error is None and result["version"] == "v1.75.0"
        client.close()

    def test_an_aborted_call_emits_nothing(self, qtbot, rc_server):
        client = RcClient(rc_server.endpoint)
        seen = []
        call = client.call("rc/noop")
        call.succeeded.connect(seen.append)
        call.failed.connect(seen.append)
        call.abort()
        qtbot.wait(80)
        assert seen == []
        client.close()

    @pytest.mark.parametrize("path", ["mount/mount", "operations/cleanup",
                                      "config/dump", "config/get"])
    def test_a_banned_endpoint_is_refused_before_anything_is_sent(
            self, rc_server, path):
        """I7, I8 and I14 are properties of the transport, not rules reviewers
        have to remember."""
        client = RcClient(rc_server.endpoint)
        with pytest.raises(SafetyRefusal):
            client.call(path, {})
        assert rc_server.requests == []
        client.close()


# ═════════════════════════════════════════════════════════════════════════════
# The blocking twin
# ═════════════════════════════════════════════════════════════════════════════

class TestCallBlocking:
    def test_it_returns_the_parsed_body(self, rc_server):
        assert call_blocking(rc_server.endpoint, "core/version")["version"] == "v1.75.0"

    def test_it_posts_with_basic_auth(self, rc_server):
        call_blocking(rc_server.endpoint, "rc/noop", {"a": 1})
        assert rc_server.requests == ["/rc/noop"]
        assert rc_server.auth_seen[-1] == _basic(USER, PASSWORD)
        assert rc_server.bodies[-1] == {"a": 1}

    def test_an_error_envelope_becomes_an_rc_error(self, rc_server):
        with pytest.raises(RcError) as excinfo:
            call_blocking(rc_server.endpoint, "rc/error", {"parameter": "BAD"})
        assert excinfo.value.status == 500
        assert excinfo.value.message == "arbitrary error"

    def test_a_dead_daemon_raises_daemon_unavailable(self, dead_endpoint):
        with pytest.raises(DaemonUnavailable) as excinfo:
            call_blocking(dead_endpoint, "rc/noop", timeout_s=1.0)
        assert excinfo.value.status == 503
        assert "connection failed" in excinfo.value.message

    def test_a_hung_daemon_raises_rather_than_blocking_forever(self, rc_server):
        rc_server.hang_for = {"core/stats"}
        with pytest.raises(DaemonUnavailable):
            call_blocking(rc_server.endpoint, "core/stats", timeout_s=0.25)

    def test_a_non_json_401_body_is_still_normalised(self, rc_server):
        bad = RcEndpoint(kind="rcd", host="127.0.0.1", port=rc_server.endpoint.port,
                         user=USER, password="wrong")
        with pytest.raises(RcError) as excinfo:
            call_blocking(bad, "rc/noop")
        assert excinfo.value.status == 401
        assert excinfo.value.body["status"] == 401
        assert "401" in excinfo.value.message

    @pytest.mark.parametrize("path", ["mount/listmounts", "operations/cleanup",
                                      "config/dump"])
    def test_banned_endpoints_are_refused_here_too(self, rc_server, path):
        with pytest.raises(SafetyRefusal):
            call_blocking(rc_server.endpoint, path)
        assert rc_server.requests == []


class TestIsAlive:
    def test_a_live_daemon_answers(self, rc_server):
        assert is_alive(rc_server.endpoint) is True
        assert rc_server.requests == ["/rc/noop"]

    def test_a_dead_port_answers_false_without_raising(self, dead_endpoint):
        assert is_alive(dead_endpoint, timeout_s=1.0) is False

    def test_a_portless_endpoint_answers_false(self):
        assert is_alive(RcEndpoint(kind="rcd")) is False

    def test_a_daemon_that_rejects_our_credentials_is_not_alive_for_us(
            self, rc_server):
        bad = RcEndpoint(kind="rcd", host="127.0.0.1", port=rc_server.endpoint.port,
                         user=USER, password="wrong")
        assert is_alive(bad) is False


# ═════════════════════════════════════════════════════════════════════════════
# JobWatcher — the four ends of an async job
# ═════════════════════════════════════════════════════════════════════════════

class _Spy:
    """Record which of the four terminal signals fired."""

    def __init__(self, watcher: JobWatcher) -> None:
        self.finished: list[dict] = []
        self.failed: list[object] = []
        self.expired = 0
        self.lost = 0
        watcher.finished.connect(self.finished.append)
        watcher.failed.connect(self.failed.append)
        watcher.expired.connect(self._on_expired)
        watcher.lost.connect(self._on_lost)

    def _on_expired(self) -> None:
        self.expired += 1

    def _on_lost(self) -> None:
        self.lost += 1

    @property
    def total(self) -> int:
        return len(self.finished) + len(self.failed) + self.expired + self.lost


@pytest.fixture
def manual_rc(qapp):
    """A `FakeRc` whose replies are delivered only when the test says so."""
    from tests.fakes.fake_rc import FakeRc, reset_registry

    rc = FakeRc(deliver_mode="manual")
    try:
        yield rc
    finally:
        rc.close()
        reset_registry()


def _handle(manual_rc, job_id: int = 1, group: str = "sync/Docs") -> JobHandle:
    return JobHandle(job_id=job_id, execute_id=manual_rc.execute_id, group=group,
                     path="sync/copy")


class TestJobWatcher:
    def test_a_finished_job_emits_finished_with_its_whole_status(self, manual_rc):
        manual_rc.jobs[1] = {"id": 1, "finished": True, "success": True,
                             "error": "", "group": "sync/Docs",
                             "executeId": manual_rc.execute_id,
                             "output": {"list": [{"Name": "a.txt"}]}}
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        manual_rc.flush()
        assert spy.total == 1
        assert spy.finished[0]["output"]["list"][0]["Name"] == "a.txt"

    def test_a_running_job_keeps_the_watcher_open(self, manual_rc):
        manual_rc.jobs[1] = {"id": 1, "finished": False, "success": False,
                             "error": "", "group": "sync/Docs",
                             "executeId": manual_rc.execute_id, "output": None}
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        manual_rc.flush()
        assert spy.total == 0
        assert watcher.running is True

    def test_an_expired_job_with_an_unchanged_execute_id_emits_expired(
            self, manual_rc):
        """HTTP 500 `job not found` on its own is ambiguous. With the SAME
        executeId it means the job finished and its outcome is simply no longer
        knowable — the activity row is `interrupted`, never `error`."""
        manual_rc.jobs[1] = {"id": 1, "finished": True, "success": True,
                             "error": "", "executeId": manual_rc.execute_id,
                             "output": {}}
        manual_rc.expire_jobs(1)
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        manual_rc.flush()        # job/status -> 500 job not found
        manual_rc.flush()        # job/list   -> unchanged executeId
        assert (spy.expired, spy.lost, len(spy.failed)) == (1, 0, 0)

    def test_a_changed_execute_id_emits_lost_rather_than_expired(self, manual_rc):
        """A different executeId is the definition of "the daemon restarted":
        every job id, mount, VFS and byte of history is gone."""
        handle = _handle(manual_rc)
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(handle)
        manual_rc.restart(new_execute_id="a-brand-new-uuid")
        manual_rc.flush()        # job/status -> 500 job not found (job 1 is gone)
        manual_rc.flush()        # job/list   -> a NEW executeId
        assert (spy.lost, spy.expired, len(spy.failed)) == (1, 0, 0)

    def test_a_status_reply_carrying_a_new_execute_id_is_lost_immediately(
            self, manual_rc):
        """No second round trip is needed when job/status itself gives it away:
        the handle was minted before the restart, so its executeId is stale even
        though a job with that id happens to exist again."""
        stale = JobHandle(job_id=1, execute_id="uuid-from-the-previous-daemon",
                          group="sync/Docs", path="sync/copy")
        assert stale.execute_id != manual_rc.execute_id
        manual_rc.jobs[1] = {"id": 1, "finished": True, "success": True,
                             "error": "", "output": {}}
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(stale)
        manual_rc.flush()
        assert (spy.lost, len(spy.finished)) == (1, 0)

    def test_a_real_error_emits_failed(self, manual_rc):
        manual_rc.fail("job/status", status=400,
                       message='Didn\'t find key "jobid" in input')
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        manual_rc.flush()
        assert len(spy.failed) == 1
        assert isinstance(spy.failed[0], RcError)
        assert spy.failed[0].status == 400

    def test_a_transient_outage_is_tolerated_before_failing(self, manual_rc):
        """A daemon restart looks like an outage first and a changed executeId
        second; bailing on the first refused connection would never see the
        `lost`."""
        manual_rc.jobs[1] = {"id": 1, "finished": False, "executeId":
                             manual_rc.execute_id, "output": None}
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        manual_rc.flush()
        manual_rc.stop()
        for _ in range(OFFLINE_FAILURE_THRESHOLD - 1):
            watcher._tick()
            manual_rc.flush()
            assert spy.total == 0
        watcher._tick()
        manual_rc.flush()
        assert len(spy.failed) == 1
        assert isinstance(spy.failed[0], DaemonUnavailable)

    def test_stop_emits_nothing_and_is_idempotent(self, manual_rc):
        manual_rc.jobs[1] = {"id": 1, "finished": True, "executeId":
                             manual_rc.execute_id, "output": {}}
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        watcher.stop()
        watcher.stop()
        manual_rc.flush()
        assert spy.total == 0
        assert watcher.running is False

    def test_watching_a_second_job_replaces_the_first(self, manual_rc):
        manual_rc.jobs[2] = {"id": 2, "finished": True, "executeId":
                             manual_rc.execute_id, "output": {"n": 2}}
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc, job_id=1))
        watcher.watch(_handle(manual_rc, job_id=2))
        assert watcher.handle.job_id == 2
        manual_rc.flush()
        assert len(spy.finished) == 1
        assert spy.finished[0]["output"] == {"n": 2}

    def test_only_one_terminal_signal_ever_fires(self, manual_rc):
        manual_rc.jobs[1] = {"id": 1, "finished": True, "executeId":
                             manual_rc.execute_id, "output": {}}
        watcher = JobWatcher(manual_rc)
        spy = _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        manual_rc.flush()
        watcher._tick()
        manual_rc.flush()
        assert spy.total == 1

    def test_the_watcher_polls_job_status_for_the_handle_s_job_id(self, manual_rc):
        manual_rc.jobs[7] = {"id": 7, "finished": True, "executeId":
                             manual_rc.execute_id, "output": {}}
        watcher = JobWatcher(manual_rc)
        _Spy(watcher)
        watcher.watch(_handle(manual_rc, job_id=7))
        manual_rc.flush()
        assert manual_rc.last("job/status").params["jobid"] == 7

    def test_it_never_calls_a_banned_endpoint(self, manual_rc):
        manual_rc.jobs[1] = {"id": 1, "finished": True, "executeId":
                             manual_rc.execute_id, "output": {}}
        watcher = JobWatcher(manual_rc)
        _Spy(watcher)
        watcher.watch(_handle(manual_rc))
        manual_rc.flush()
        for banned in ("mount/mount", "mount/listmounts", "operations/cleanup",
                       "config/dump", "config/get"):
            manual_rc.assert_never(banned)
