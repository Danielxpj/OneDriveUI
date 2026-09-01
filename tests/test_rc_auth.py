"""WP-03 — `onedriveui/rc/auth.py`.

Sign-in is the one flow where getting the *sequence* wrong hangs the
application, so this file drives the whole rc walk end to end against `FakeRc`
with a real Qt event loop:

    config/create (nonInteractive)          -> a question, not a prompt
      -> config/create (continue + _async)  -> the call that BLOCKS on the
                                               browser, hence _async
      -> config/oauthstatus                 -> {"status":"running","authUrl":…}
      -> job/status                         -> the next question, or the end
      -> config/oauthstop + job/stop        -> cancel

Nothing here binds 127.0.0.1:53682 — that port belongs to rclone and to nothing
else on this machine — so the port check is exercised through
`callback_port_free(host, port)` on a scratch port and through a substituted
predicate. No browser is ever opened: the opener is injected.

`rclone authorize` is exercised against a stub executable that reproduces the
two-channel behaviour exactly: the link on **stderr**, the token blob on
**stdout** between the paste markers.

Invariant I14 has its own test: no token, and no OAuth `state`, may reach a log
record.
"""

from __future__ import annotations

import os
import socket
import stat
import sys
import time

import pytest

from onedriveui.constants import OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT, TOKEN_KEEPALIVE_S
from onedriveui.errors import OneDriveUIError, RcError, SafetyRefusal
from onedriveui.models import TokenHealth
from onedriveui.rc import auth
from onedriveui.rc.client import call_blocking
from tests.fakes import fake_rc as fake_rc_module

REMOTE = "onedrive"
STATE = "*oauth-islocal,choose_type,,"
SECRET_STATE = "SUPERSECRETSTATEVALUE"
AUTH_URL = f"http://127.0.0.1:53682/auth?state={SECRET_STATE}"

#: Step 1's verbatim shape, from `docs/research/rclone-rc-api.md` §9.3.
STEP_IS_LOCAL = {
    "Error": "", "Result": "", "State": STATE,
    "Option": {
        "Name": "config_is_local", "Type": "bool",
        "Help": "Use web browser to automatically authenticate rclone…",
        "Default": True, "DefaultStr": "true", "ValueStr": "true",
        "Exclusive": True,
        "Examples": [{"Value": "true", "Help": "Yes"},
                     {"Value": "false", "Help": "No"}],
        "Advanced": False, "Hide": 0, "IsPassword": False, "Required": False,
        "Sensitive": False, "Value": None, "FieldName": "",
    },
}

STEP_DRIVE_TYPE = {
    "Error": "", "Result": "", "State": "*all-set,3,false",
    "Option": {"Name": "drive_type", "Type": "string", "Help": "Drive type",
               "Default": "personal", "DefaultStr": "personal",
               "ValueStr": "personal", "Advanced": False, "Hide": 0,
               "IsPassword": False, "Required": False, "Sensitive": False},
}

#: The end of the walk: no `Option` left.
FINAL = {"Error": "", "Result": "", "State": "", "Option": None}


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def wait_until(qtbot, predicate, timeout_ms: int = 4000) -> bool:
    """Pump the event loop until ``predicate`` holds or the budget runs out."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if predicate():
            return True
        qtbot.wait(2)
    return bool(predicate())


class Opener:
    """A stand-in for ``QDesktopServices.openUrl``. No browser is ever launched."""

    def __init__(self, ok: bool = True) -> None:
        self.urls: list[str] = []
        self.ok = ok

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return self.ok


@pytest.fixture
def rc(fake_rc):
    """The fake daemon with the config endpoints scripted for a normal walk."""
    fake_rc.set("config/oauthstatus", {"status": "running", "authUrl": AUTH_URL})
    fake_rc.set("config/oauthstop", {})
    fake_rc.set("config/unset", {"removed": ["token"]})
    fake_rc.set("config/delete", {})
    return fake_rc


@pytest.fixture
def opener() -> Opener:
    return Opener()


@pytest.fixture
def flow(rc, opener, qapp):
    """An `AuthFlow` polling fast enough for a test, with the port check off."""
    obj = auth.AuthFlow(rc, open_url=opener, poll_ms=1, check_port=False)
    try:
        yield obj
    finally:
        if obj.running:
            obj.cancel()


class FlowSpy:
    def __init__(self, obj: auth.AuthFlow) -> None:
        self.urls: list[str] = []
        self.done: list[tuple[bool, str]] = []
        self.steps: list[str] = []
        obj.url_ready.connect(self.urls.append)
        obj.finished.connect(lambda ok, msg: self.done.append((ok, msg)))
        obj.step.connect(self.steps.append)


@pytest.fixture
def spy(flow) -> FlowSpy:
    return FlowSpy(flow)


def script_walk(rc, *, blocking_polls: int = 3, output=FINAL,
                creates=(STEP_IS_LOCAL,)) -> dict:
    """Script a walk whose blocking step stays outstanding for a few polls.

    Returns a mutable counter so a test can assert how often `job/status` was
    asked — the browser step really does stay unfinished while the user types a
    password, which is what gives `config/oauthstatus` time to answer.
    """
    counter = {"polls": 0}
    rc.script("config/create", list(creates), repeat_last=True)
    rc.script("config/update", list(creates), repeat_last=True)

    def job_status(params):
        counter["polls"] += 1
        job_id = int(params.get("jobid", 0))
        if counter["polls"] < blocking_polls:
            return {"id": job_id, "finished": False, "success": False,
                    "error": "", "output": None, "group": "job/1",
                    "executeId": rc.execute_id,
                    "endTime": fake_rc_module.GO_ZERO_TIME}
        return {"id": job_id, "finished": True, "success": True, "error": "",
                "output": dict(output), "executeId": rc.execute_id}

    rc.set("job/status", job_status)
    return counter


# ═════════════════════════════════════════════════════════════════════════════
# The callback port
# ═════════════════════════════════════════════════════════════════════════════

def test_oauth_callback_is_the_constant_pair():
    assert auth.OAUTH_CALLBACK == (OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT)
    assert auth.OAUTH_CALLBACK == ("127.0.0.1", 53682)


def test_callback_port_free_defaults_to_rclones_own_port():
    """The defaults are the constants, so production probes 53682 and nothing
    else. It is not probed *here*: 53682 belongs to rclone, and binding it —
    even for the microsecond a probe takes — could lose a real sign-in the race
    for its own callback server."""
    import inspect

    signature = inspect.signature(auth.callback_port_free)
    assert signature.parameters["host"].default == OAUTH_CALLBACK_HOST
    assert signature.parameters["port"].default == OAUTH_CALLBACK_PORT


def test_callback_port_free_is_false_while_something_holds_the_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        assert auth.callback_port_free("127.0.0.1", port) is False
    finally:
        sock.close()
    assert auth.callback_port_free("127.0.0.1", port) is True


def test_the_port_is_checked_before_anything_is_sent(rc, opener, qapp,
                                                     monkeypatch):
    """The check is FIRST. Starting the walk with the port taken produces a
    sign-in that hangs with no explanation, which is the whole point."""
    monkeypatch.setattr(auth, "callback_port_free", lambda *a, **kw: False)
    obj = auth.AuthFlow(rc, open_url=opener)
    spy = FlowSpy(obj)
    assert obj.start(REMOTE) is False
    assert obj.running is False
    assert rc.calls == [], "not one rc call may be made once the port is taken"
    assert spy.done and spy.done[0][0] is False
    assert "53682" in spy.done[0][1]


def test_the_port_check_can_be_skipped_only_deliberately(rc, opener, qapp,
                                                         monkeypatch):
    monkeypatch.setattr(auth, "callback_port_free", lambda *a, **kw: False)
    obj = auth.AuthFlow(rc, open_url=opener, check_port=False)
    script_walk(rc)
    assert obj.start(REMOTE) is True
    obj.cancel()


# ═════════════════════════════════════════════════════════════════════════════
# The rc OAuth walk
# ═════════════════════════════════════════════════════════════════════════════

def test_step_one_is_non_interactive_and_not_a_continue(flow, rc, spy, qtbot):
    script_walk(rc)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: rc.count("config/create") >= 1)
    first = rc.calls_to("config/create")[0]
    assert first.params["name"] == REMOTE
    assert first.params["type"] == "onedrive"
    assert first.params["opt"]["nonInteractive"] is True
    assert first.params["opt"]["noOutput"] is True
    assert "continue" not in first.params["opt"]
    assert first.async_ is False, "step one answers a question immediately"


def test_the_browser_step_is_async_and_echoes_the_state(flow, rc, spy, qtbot):
    """Answering `config_is_local=true` starts rclone's local webserver and
    blocks the HTTP request — observed hanging for over a minute — so that one
    call must be `_async`."""
    script_walk(rc)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: rc.count("config/create") >= 2)
    second = rc.calls_to("config/create")[1]
    assert second.async_ is True
    assert second.params["opt"]["continue"] is True
    assert second.params["opt"]["state"] == STATE
    assert second.params["opt"]["result"] == "true"
    assert second.params["opt"]["nonInteractive"] is True
    assert spy.steps[0] == "config_is_local"


def test_parameters_are_re_sent_on_every_step(flow, rc, qtbot):
    """`--continue` keeps no memory: every value already answered has to be
    supplied again on each invocation."""
    script_walk(rc)
    flow.start(REMOTE, parameters={"region": "global"})
    assert wait_until(qtbot, lambda: rc.count("config/create") >= 2)
    for record in rc.calls_to("config/create"):
        assert record.params["parameters"] == {"region": "global"}


def test_the_auth_url_is_published_and_opened(flow, rc, spy, opener, qtbot):
    script_walk(rc, blocking_polls=6)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.urls))
    assert spy.urls == [AUTH_URL]
    assert opener.urls == [AUTH_URL]
    assert flow.auth_url == AUTH_URL


def test_the_auth_url_reaches_the_bus(rc, opener, qapp, qtbot, bus_spy):
    bus_spy.watch("auth_url_ready", "auth_finished")
    script_walk(rc, blocking_polls=6)
    obj = auth.AuthFlow(rc, open_url=opener, poll_ms=1, check_port=False)
    try:
        obj.start(REMOTE)
        assert wait_until(qtbot, lambda: bus_spy.count("auth_url_ready") == 1)
        assert bus_spy.last("auth_url_ready") == (AUTH_URL,)
        assert wait_until(qtbot, lambda: bus_spy.count("auth_finished") == 1)
        assert bus_spy.last("auth_finished") == (True, REMOTE)
    finally:
        if obj.running:
            obj.cancel()


def test_a_stopped_oauth_server_is_not_mistaken_for_a_url(flow, rc, spy, qtbot):
    rc.set("config/oauthstatus", {"status": "stopped"})
    script_walk(rc, blocking_polls=4)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.urls == []


def test_the_walk_finishes_when_a_response_has_no_option(flow, rc, spy, qtbot):
    script_walk(rc)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.done == [(True, REMOTE)]
    assert flow.running is False


def test_a_question_after_the_oauth_step_is_answered_with_its_default(
        flow, rc, spy, qtbot):
    """rclone keeps asking after the token lands — drive type, for instance —
    and the walk has to keep answering until an `Option`-less response."""
    script_walk(rc, output=STEP_DRIVE_TYPE, creates=(STEP_IS_LOCAL, FINAL))
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.done == [(True, REMOTE)]
    assert spy.steps == ["config_is_local", "drive_type"]
    last = rc.calls_to("config/create")[-1]
    assert last.params["opt"]["result"] == "personal"
    assert last.params["opt"]["state"] == "*all-set,3,false"


def test_an_answer_hook_overrides_the_default(rc, opener, qapp, qtbot):
    answers: list[str] = []

    def answer(option):
        answers.append(str(option.get("Name")))
        return "business" if option.get("Name") == "drive_type" else ""

    obj = auth.AuthFlow(rc, open_url=opener, answer=answer, poll_ms=1,
                        check_port=False)
    spy = FlowSpy(obj)
    script_walk(rc, output=STEP_DRIVE_TYPE, creates=(STEP_IS_LOCAL, FINAL))
    try:
        obj.start(REMOTE)
        assert wait_until(qtbot, lambda: bool(spy.done))
    finally:
        if obj.running:
            obj.cancel()
    assert answers == ["config_is_local", "drive_type"]
    assert rc.calls_to("config/create")[-1].params["opt"]["result"] == "business"


def test_update_re_authenticates_an_existing_remote(flow, rc, spy, qtbot):
    """`rclone config reconnect` has no non-interactive mode and must never be
    shelled out to; `config/update` is the supported route."""
    script_walk(rc)
    flow.start(REMOTE, update=True)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert rc.count("config/update") >= 2
    assert rc.count("config/create") == 0


def test_the_paste_a_token_branch_fails_with_a_clear_message(flow, rc, spy, qtbot):
    """Reachable only if `config_is_local` was answered "false". This
    application always has a browser, so it never should be."""
    token_step = {"Error": "", "State": "*oauth-authorize,choose_type,,",
                  "Option": {"Name": "config_token", "Type": "string",
                             "Required": True, "Help": "paste it"}}
    rc.script("config/create", [token_step], repeat_last=True)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    ok, message = spy.done[0]
    assert ok is False
    assert "browser" in message


def test_an_error_in_the_response_fails_the_walk(flow, rc, spy, qtbot):
    rc.script("config/create", [{"Error": "failed to configure OneDrive",
                                 "State": "", "Option": None}],
              repeat_last=True)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.done[0] == (False, "failed to configure OneDrive")


def test_a_failed_call_fails_the_walk(flow, rc, spy, qtbot):
    rc.fail("config/create", status=500,
            message='didn\'t find section in config file ("nope")')
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.done[0][0] is False
    assert "config file" in spy.done[0][1]


def test_a_daemon_restart_during_sign_in_is_reported(flow, rc, spy, qtbot):
    def job_status(params):
        return {"id": int(params.get("jobid", 0)), "finished": False,
                "executeId": "a-different-execute-id"}

    rc.script("config/create", [STEP_IS_LOCAL], repeat_last=True)
    rc.set("job/status", job_status)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.done[0][0] is False
    assert "restarted" in spy.done[0][1]


def test_an_expired_sign_in_job_is_reported(flow, rc, spy, qtbot):
    rc.script("config/create", [STEP_IS_LOCAL], repeat_last=True)
    rc.fail("job/status", status=500, message="job not found")
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.done[0][0] is False
    assert "expired" in spy.done[0][1]


def test_the_walk_cannot_loop_for_ever(flow, rc, spy, qtbot):
    """A response that keeps asking the same question would otherwise wedge
    sign-in with no way out."""
    repeat = {"Error": "", "State": "*all-set,0,false",
              "Option": {"Name": "client_id", "Type": "string",
                         "DefaultStr": "", "Default": ""}}
    rc.script("config/create", [repeat], repeat_last=True)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: bool(spy.done))
    assert spy.done[0][0] is False
    assert str(auth.MAX_CONFIG_STEPS) in spy.done[0][1]


def test_starting_twice_is_refused(flow, rc, qtbot):
    script_walk(rc, blocking_polls=1000)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: flow.job_id != 0)
    with pytest.raises(OneDriveUIError, match="already in progress"):
        flow.start(REMOTE)


def test_start_needs_a_remote_name(flow):
    with pytest.raises(ValueError):
        flow.start("")


# ═════════════════════════════════════════════════════════════════════════════
# Cancelling
# ═════════════════════════════════════════════════════════════════════════════

def test_cancel_stops_the_oauth_server_and_the_job(flow, rc, spy, qtbot):
    """`config/oauthstop` is what unblocks the held HTTP request; the job is
    stopped too, so a create that had already answered cannot finish behind our
    back."""
    script_walk(rc, blocking_polls=1000)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: flow.job_id != 0)
    job_id = flow.job_id

    flow.cancel()

    assert rc.count("config/oauthstop") == 1
    assert rc.last("job/stop").params == {"jobid": job_id}
    assert spy.done[0][0] is False
    assert "cancelled" in spy.done[0][1]
    assert flow.running is False


def test_cancel_tolerates_no_oauth_in_progress(flow, rc, spy, qtbot):
    """`config/oauthstop` with nothing running answers HTTP 500 `no oauth
    authentication is in progress`. Benign."""
    rc.fail("config/oauthstop", status=500,
            message="no oauth authentication is in progress")
    script_walk(rc, blocking_polls=1000)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: flow.job_id != 0)
    flow.cancel()
    qtbot.wait(10)
    assert spy.done[0][0] is False


def test_cancel_when_idle_does_nothing(flow, rc):
    flow.cancel()
    assert rc.count("config/oauthstop") == 0


def test_nothing_is_emitted_after_a_cancel(flow, rc, spy, qtbot):
    script_walk(rc, blocking_polls=1000)
    flow.start(REMOTE)
    assert wait_until(qtbot, lambda: flow.job_id != 0)
    flow.cancel()
    before = len(spy.done)
    qtbot.wait(30)
    assert len(spy.done) == before


# ═════════════════════════════════════════════════════════════════════════════
# Invariant I14 — no token, and no OAuth state, in a log record
# ═════════════════════════════════════════════════════════════════════════════

def test_the_oauth_state_never_reaches_a_log_record(flow, rc, spy, qtbot,
                                                    caplog):
    with caplog.at_level("DEBUG", logger="onedriveui.rc.auth"):
        script_walk(rc, blocking_polls=6)
        flow.start(REMOTE)
        assert wait_until(qtbot, lambda: bool(spy.done))
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET_STATE not in blob
    assert "[redacted]" in blob, "the URL is still logged, minus its state"
    assert spy.urls == [AUTH_URL], "the caller still gets the real URL"


def test_a_token_in_an_error_is_redacted_before_logging(flow, rc, spy, qtbot,
                                                        caplog):
    secret = "M.C5_BAY.0.U.-SECRETREFRESHTOKEN"
    rc.script("config/create", [{
        "Error": f'failed to configure: {{"refresh_token":"{secret}"}}',
        "State": "", "Option": None}], repeat_last=True)
    with caplog.at_level("DEBUG", logger="onedriveui.rc.auth"):
        flow.start(REMOTE)
        assert wait_until(qtbot, lambda: bool(spy.done))
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in blob


def test_the_config_endpoints_that_leak_a_token_are_refused(rc):
    """`config/dump` and `config/get` return the refresh token in the clear, so
    the transport refuses them outright (I14)."""
    for path in ("config/dump", "config/get"):
        with pytest.raises(SafetyRefusal) as excinfo:
            call_blocking(rc.endpoint, path, {"name": REMOTE})
        assert excinfo.value.invariant == "I14"


# ═════════════════════════════════════════════════════════════════════════════
# probe_token
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def probe_rc(fake_rc, monkeypatch):
    """A fake daemon reached through `auth`'s own blocking helper."""
    monkeypatch.setattr(auth, "call_blocking", fake_rc_module.call_blocking)
    fake_rc.set("config/delete", {})
    fake_rc.set("config/unset", {"removed": ["token"]})
    return fake_rc


def test_probe_token_is_ok_when_about_answers(probe_rc):
    assert auth.probe_token("onedrive:", ep=probe_rc.endpoint) is TokenHealth.OK
    assert probe_rc.last("operations/about").params == {"fs": "onedrive:"}


def test_aadsts65005_is_tenant_blocked_and_aadsts50076_is_mfa(probe_rc):
    """WP-03 acceptance. They are different states: only MFA is fixed by
    re-authenticating. Offering "Sign in again" for an unmanaged tenant is a
    loop the user cannot escape — an administrator must claim the domain."""
    probe_rc.auth_error = ("AADSTS65005: Using application 'rclone' is "
                           "currently not supported for your organization")
    blocked = auth.probe_token("onedrive:", ep=probe_rc.endpoint)
    assert blocked is TokenHealth.TENANT_BLOCKED
    assert auth.is_reauth_fixable(blocked) is False

    probe_rc.auth_error = "invalid_grant: AADSTS50076: multi-factor required"
    mfa = auth.probe_token("onedrive:", ep=probe_rc.endpoint)
    assert mfa is TokenHealth.MFA
    assert auth.is_reauth_fixable(mfa) is True

    assert blocked is not mfa


@pytest.mark.parametrize("message", [
    'failed to configure OneDrive: empty token found - please run "rclone config reconnect"',
    "couldn't fetch token: invalid_grant",
    "token has expired",
    "failed to get token",
])
def test_the_no_usable_token_phrasings_are_expired(probe_rc, message):
    probe_rc.auth_error = message
    health = auth.probe_token("onedrive:", ep=probe_rc.endpoint)
    assert health is TokenHealth.EXPIRED
    assert auth.is_reauth_fixable(health) is True


def test_a_network_error_is_unknown_not_signed_out(probe_rc):
    """An offline laptop has not been signed out."""
    probe_rc.auth_error = "dial tcp 13.107.42.12:443: i/o timeout"
    assert auth.probe_token("onedrive:", ep=probe_rc.endpoint) is TokenHealth.UNKNOWN


def test_an_unreachable_daemon_is_unknown(probe_rc):
    probe_rc.stop()
    assert auth.probe_token("onedrive:", ep=probe_rc.endpoint) is TokenHealth.UNKNOWN


@pytest.mark.parametrize("text,health", [
    ("AADSTS65005", TokenHealth.TENANT_BLOCKED),
    ("AADSTS50076", TokenHealth.MFA),
    ("empty token found", TokenHealth.EXPIRED),
    ("invalid_grant", TokenHealth.EXPIRED),
    ("", TokenHealth.UNKNOWN),
    ("connection refused", TokenHealth.UNKNOWN),
])
def test_classify_auth_error_uses_the_shared_table(text, health):
    assert auth.classify_auth_error(text) is health


def test_classify_prefers_the_more_specific_aadsts_code():
    """Both codes arrive wrapped in `invalid_grant`, which on its own would mean
    EXPIRED. The table's order is what keeps them distinguishable."""
    assert auth.classify_auth_error(
        "invalid_grant: AADSTS50076: MFA") is TokenHealth.MFA
    assert auth.classify_auth_error(
        "access_denied: AADSTS65005: unmanaged") is TokenHealth.TENANT_BLOCKED


def test_probe_token_never_logs_the_error_unredacted(probe_rc, caplog):
    probe_rc.auth_error = 'refresh_token: "M.C5-SECRET"'
    with caplog.at_level("DEBUG", logger="onedriveui.rc.auth"):
        auth.probe_token("onedrive:", ep=probe_rc.endpoint)
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert "M.C5-SECRET" not in blob


# ═════════════════════════════════════════════════════════════════════════════
# keepalive — the 90-day non-use expiry
# ═════════════════════════════════════════════════════════════════════════════

def test_keepalive_is_due_when_it_has_never_run():
    assert auth.keepalive_due(None) is True
    assert auth.keepalive_due("") is True
    assert auth.keepalive_due("not a timestamp") is True


def test_keepalive_is_due_after_a_day_and_not_before():
    assert auth.keepalive_due("2026-08-31T00:00:00Z",
                              now="2026-08-31T23:59:00Z") is False
    assert auth.keepalive_due("2026-08-30T00:00:00Z",
                              now="2026-08-31T00:00:01Z") is True


def test_the_keepalive_interval_is_the_shared_constant():
    assert TOKEN_KEEPALIVE_S == 24 * 3600
    assert auth.keepalive_due("2026-08-30T12:00:00Z", now="2026-08-31T12:00:00Z",
                              interval_s=TOKEN_KEEPALIVE_S) is True


def test_keepalive_skips_when_it_is_too_soon(probe_rc):
    result = auth.keepalive("onedrive:", ep=probe_rc.endpoint,
                            last_at="2026-08-31T11:00:00Z",
                            now="2026-08-31T12:00:00Z")
    assert result is None
    assert probe_rc.count("operations/about") == 0


def test_keepalive_probes_when_it_is_due(probe_rc):
    result = auth.keepalive("onedrive:", ep=probe_rc.endpoint,
                            last_at="2026-08-20T12:00:00Z",
                            now="2026-08-31T12:00:00Z")
    assert result is TokenHealth.OK
    assert probe_rc.count("operations/about") == 1


def test_keepalive_can_be_forced(probe_rc):
    assert auth.keepalive("onedrive:", ep=probe_rc.endpoint,
                          last_at="2026-08-31T11:59:00Z",
                          now="2026-08-31T12:00:00Z",
                          force=True) is TokenHealth.OK


def test_token_keepalive_announces_a_due_probe_without_blocking(qapp):
    """The probe is a blocking rc call, so this object never runs one itself."""
    keeper = auth.TokenKeepalive()
    fired: list[int] = []
    reported: list[TokenHealth] = []
    keeper.due_now.connect(lambda: fired.append(1))
    keeper.probed.connect(reported.append)
    try:
        keeper.start(last_at=None)
        assert fired == [1]
        keeper.report(TokenHealth.OK, at="2026-08-31T12:00:00Z")
        assert reported == [TokenHealth.OK]
        assert keeper.last_at == "2026-08-31T12:00:00Z"
    finally:
        keeper.stop()


def test_token_keepalive_stays_quiet_when_it_is_not_due(qapp):
    from onedriveui.models import utcnow_iso

    keeper = auth.TokenKeepalive()
    fired: list[int] = []
    keeper.due_now.connect(lambda: fired.append(1))
    try:
        keeper.start(last_at=utcnow_iso())
        assert fired == []
    finally:
        keeper.stop()


# ═════════════════════════════════════════════════════════════════════════════
# unlink_account
# ═════════════════════════════════════════════════════════════════════════════

def test_unlink_removes_the_remote(probe_rc):
    assert auth.unlink_account(REMOTE, ep=probe_rc.endpoint) is True
    assert probe_rc.last("config/delete").params == {"name": REMOTE}
    assert probe_rc.count("config/unset") == 0


def test_unlink_can_keep_the_remote_and_drop_only_the_token(probe_rc):
    probe_rc.set("config/unset", {"removed": ["token"]})
    assert auth.unlink_account(f"{REMOTE}:", ep=probe_rc.endpoint,
                               keep_remote=True) is True
    assert probe_rc.last("config/unset").params == {"name": REMOTE,
                                                    "keys": ["token"]}
    assert probe_rc.count("config/delete") == 0


def test_unlink_of_an_unconfigured_remote_is_false(probe_rc):
    assert auth.unlink_account("nosuchremote", ep=probe_rc.endpoint) is False
    assert probe_rc.count("config/delete") == 0


def test_unlink_never_reads_the_config_back(probe_rc):
    """`config/get` would return the refresh token in the clear (I14);
    `config/listremotes` answers the same question safely."""
    auth.unlink_account(REMOTE, ep=probe_rc.endpoint)
    probe_rc.assert_never("config/get")
    probe_rc.assert_never("config/dump")
    assert probe_rc.count("config/listremotes") == 1


def test_unlink_needs_a_name(probe_rc):
    with pytest.raises(ValueError):
        auth.unlink_account(":", ep=probe_rc.endpoint)


def test_unlink_propagates_a_refusal(probe_rc):
    probe_rc.fail("config/delete", status=500, message="config is read-only")
    with pytest.raises(RcError):
        auth.unlink_account(REMOTE, ep=probe_rc.endpoint)


# ═════════════════════════════════════════════════════════════════════════════
# The `rclone authorize` fallback
# ═════════════════════════════════════════════════════════════════════════════

AUTHORIZE_STDERR = (
    'NOTICE: Make sure your Redirect URL is set to "http://localhost:53682/" '
    "in your custom config.\n"
    "NOTICE: Please go to the following link: "
    "http://127.0.0.1:53682/auth?state=xYdtscsz0dcZQDPTPETewA\n"
    "NOTICE: Log in and authorize rclone for access\n"
    "NOTICE: Waiting for code...\n"
)

TOKEN_BLOB = ('{"access_token":"EwBYFAKE","token_type":"Bearer",'
              '"refresh_token":"M.C5FAKE","expiry":"2026-09-01T00:00:00Z"}')

AUTHORIZE_STDOUT = (
    f"Paste the following into your remote machine --->\n"
    f"{TOKEN_BLOB}\n"
    f"<---End paste\n"
)


def test_parse_authorize_url_reads_the_stderr_notice():
    url = auth.parse_authorize_url(AUTHORIZE_STDERR)
    assert url == "http://127.0.0.1:53682/auth?state=xYdtscsz0dcZQDPTPETewA"


def test_parse_authorize_url_before_the_notice_arrives():
    assert auth.parse_authorize_url("NOTICE: Make sure your Redirect URL") == ""
    assert auth.parse_authorize_url("") == ""


def test_parse_authorize_token_reads_between_the_markers():
    assert auth.parse_authorize_token(AUTHORIZE_STDOUT) == TOKEN_BLOB


def test_parse_authorize_token_waits_for_the_closing_marker():
    partial = f"{auth.AUTHORIZE_START_MARKER}\n{TOKEN_BLOB}"
    assert auth.parse_authorize_token(partial) == ""
    assert auth.parse_authorize_token("") == ""


def _stub_rclone(tmp_path, *, stdout: str, stderr: str, code: int = 0):
    """A stand-in for the rclone binary that reproduces both channels."""
    script = tmp_path / "rclone-stub.py"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import sys, time\n"
        f"sys.stderr.write({stderr!r})\n"
        "sys.stderr.flush()\n"
        "time.sleep(0.05)\n"
        f"sys.stdout.write({stdout!r})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({code})\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)
    return script


def test_the_fallback_reads_the_link_from_stderr_and_the_token_from_stdout(
        tmp_path, qapp, qtbot):
    """The two channels carry different things and must be drained separately:
    merging them interleaves NOTICE lines into the token blob."""
    script = _stub_rclone(tmp_path, stdout=AUTHORIZE_STDOUT,
                          stderr=AUTHORIZE_STDERR)
    opener = Opener()
    fallback = auth.AuthorizeFallback(rclone_path=str(script), open_url=opener,
                                      emit_bus=False)
    done: list[tuple[bool, str]] = []
    urls: list[str] = []
    fallback.url_ready.connect(urls.append)
    fallback.finished.connect(lambda ok, msg: done.append((ok, msg)))

    fallback.start("onedrive")
    assert wait_until(qtbot, lambda: bool(done), timeout_ms=10_000)

    assert done[0][0] is True
    assert urls == ["http://127.0.0.1:53682/auth?state=xYdtscsz0dcZQDPTPETewA"]
    assert opener.urls == urls
    assert fallback.token == TOKEN_BLOB
    assert fallback.auth_url == urls[0]


def test_the_fallback_reports_a_failure_when_no_token_arrives(tmp_path, qapp,
                                                              qtbot):
    script = _stub_rclone(tmp_path, stdout="",
                          stderr="NOTICE: Failed to get code\n", code=1)
    fallback = auth.AuthorizeFallback(rclone_path=str(script),
                                      open_url=Opener(), emit_bus=False)
    done: list[tuple[bool, str]] = []
    fallback.finished.connect(lambda ok, msg: done.append((ok, msg)))
    fallback.start("onedrive")
    assert wait_until(qtbot, lambda: bool(done), timeout_ms=10_000)
    assert done[0][0] is False
    assert fallback.token == ""


def test_the_fallback_reports_a_missing_binary(tmp_path, qapp, qtbot):
    fallback = auth.AuthorizeFallback(
        rclone_path=str(tmp_path / "definitely-not-here"),
        open_url=Opener(), emit_bus=False)
    done: list[tuple[bool, str]] = []
    fallback.finished.connect(lambda ok, msg: done.append((ok, msg)))
    fallback.start("onedrive")
    assert wait_until(qtbot, lambda: bool(done), timeout_ms=10_000)
    assert done[0][0] is False
    assert "could not run" in done[0][1]


def test_the_fallback_passes_auth_no_open_browser(tmp_path, qapp, qtbot):
    """rclone must not open the browser itself: we do it, so the flow can be
    rendered and cancelled from our own dialog."""
    marker = tmp_path / "argv.txt"
    script = tmp_path / "argv-stub.py"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import sys\n"
        f"open({str(marker)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
        f"sys.stdout.write({AUTHORIZE_STDOUT!r})\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    fallback = auth.AuthorizeFallback(rclone_path=str(script),
                                      open_url=Opener(), emit_bus=False)
    done: list[tuple[bool, str]] = []
    fallback.finished.connect(lambda ok, msg: done.append((ok, msg)))
    fallback.start("onedrive")
    assert wait_until(qtbot, lambda: bool(done), timeout_ms=10_000)
    assert marker.read_text().splitlines() == [
        "authorize", "onedrive", "--auth-no-open-browser"]


def test_the_fallback_never_logs_the_token(tmp_path, qapp, qtbot, caplog):
    script = _stub_rclone(tmp_path, stdout=AUTHORIZE_STDOUT,
                          stderr=AUTHORIZE_STDERR)
    fallback = auth.AuthorizeFallback(rclone_path=str(script),
                                      open_url=Opener(), emit_bus=False)
    done: list[tuple[bool, str]] = []
    fallback.finished.connect(lambda ok, msg: done.append((ok, msg)))
    with caplog.at_level("DEBUG", logger="onedriveui.rc.auth"):
        fallback.start("onedrive")
        assert wait_until(qtbot, lambda: bool(done), timeout_ms=10_000)
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert "M.C5FAKE" not in blob
    assert "EwBYFAKE" not in blob
    assert "xYdtscsz0dcZQDPTPETewA" not in blob, "the state is a bearer too"


def test_cancelling_the_fallback_kills_the_child(tmp_path, qapp, qtbot):
    """The child holds port 53682 until it gets a code, so cancelling has to
    actually kill it."""
    script = tmp_path / "sleeper.py"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import time\n"
        "time.sleep(30)\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    fallback = auth.AuthorizeFallback(rclone_path=str(script),
                                      open_url=Opener(), emit_bus=False)
    done: list[tuple[bool, str]] = []
    fallback.finished.connect(lambda ok, msg: done.append((ok, msg)))
    fallback.start("onedrive")
    assert wait_until(qtbot, lambda: fallback.running, timeout_ms=10_000)
    fallback.cancel()
    assert wait_until(qtbot, lambda: not fallback.running, timeout_ms=10_000)
    assert done and done[0][0] is False


def test_two_fallbacks_at_once_are_refused(tmp_path, qapp, qtbot):
    script = tmp_path / "sleeper2.py"
    script.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(30)\n",
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    fallback = auth.AuthorizeFallback(rclone_path=str(script),
                                      open_url=Opener(), emit_bus=False)
    try:
        fallback.start("onedrive")
        assert wait_until(qtbot, lambda: fallback.running, timeout_ms=10_000)
        with pytest.raises(OneDriveUIError, match="already running"):
            fallback.start("onedrive")
    finally:
        fallback.cancel()


def test_the_stub_helper_is_executable(tmp_path):
    script = _stub_rclone(tmp_path, stdout="", stderr="")
    assert os.access(script, os.X_OK)
