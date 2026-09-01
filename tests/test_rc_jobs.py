"""WP-03 — `onedriveui/rc/jobs.py`.

What is under test is the bookkeeping around `_async`, not the transport:

  * a **stable** `_group` per user-visible operation, because `core/stats` and
    `job/stopgroup` are both keyed by it and `MaxStatsGroups` caps the map at
    1000;
  * the four different ends a job can reach, and in particular that an
    **expired** job (`job not found` with an unchanged `executeId`) is reported
    as `expired`, never as `failed` — it did finish, its outcome is merely
    unknowable;
  * `invalidate_all()` on an `executeId` change, because a restarted daemon
    numbers its jobs from 1 again and every handle we hold is stale;
  * `core/stats-delete` cleanup, and the ordering that keeps it from destroying
    the group's `core/transferred` history.

`FakeRc` is driven in **manual** delivery mode and pumped by hand, so a whole
job lifecycle — the `_async` reply, the `job/status` polls, the disambiguating
`job/list`, the cleanup — runs deterministically with no event loop and no
sleeping.
"""

from __future__ import annotations

import pytest

from onedriveui.bus import BUS
from onedriveui.errors import RcError, SafetyRefusal
from onedriveui.models import JobHandle, RcEndpoint
from onedriveui.rc import jobs
from tests.fakes.fake_rc import RcFault

GROUP = "onedriveui/pin/onedrive"


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def rc(fake_rc):
    """The fake daemon, holding every reply until `pump()` releases it."""
    fake_rc.deliver_mode = "manual"
    return fake_rc


def pump(rc, rounds: int = 12) -> None:
    """Deliver every pending reply, and every reply those replies provoke.

    One `start()` cascades into an `_async` answer, a `job/status` poll and
    possibly a `job/list` disambiguation, so a single `flush()` is never enough.
    """
    for _ in range(rounds):
        if not rc.pending:
            return
        rc.flush()


def running_status(rc, job_id: int = 0) -> dict:
    """A `job/status` body for a job that has not finished."""
    return {"id": job_id, "finished": False, "success": False, "error": "",
            "output": None, "executeId": rc.execute_id}


@pytest.fixture
def registry(rc, qapp):
    reg = jobs.JobRegistry(rc)
    try:
        yield reg
    finally:
        reg.close()


class Recorder:
    """Collects a registry's five terminal signals."""

    def __init__(self, reg: jobs.JobRegistry) -> None:
        self.started: list[JobHandle] = []
        self.finished: list[tuple[JobHandle, dict]] = []
        self.failed: list[tuple[object, object]] = []
        self.expired: list[JobHandle] = []
        self.lost: list[JobHandle] = []
        self.emptied: list[str] = []
        reg.started.connect(self.started.append)
        reg.finished.connect(lambda h, s: self.finished.append((h, s)))
        reg.failed.connect(lambda h, e: self.failed.append((h, e)))
        reg.expired.connect(self.expired.append)
        reg.lost.connect(self.lost.append)
        reg.group_emptied.connect(self.emptied.append)


@pytest.fixture
def spy(registry) -> Recorder:
    return Recorder(registry)


# ═════════════════════════════════════════════════════════════════════════════
# group_for — the stable name
# ═════════════════════════════════════════════════════════════════════════════

def test_group_for_is_stable_and_namespaced():
    assert jobs.group_for("pin", "onedrive") == "onedriveui/pin/onedrive"
    assert jobs.group_for("pin", "onedrive") == jobs.group_for("pin", "onedrive")


def test_group_for_carries_an_optional_bounded_detail():
    assert jobs.group_for("bisync", "onedrive", "Offline") == (
        "onedriveui/bisync/onedrive/Offline")


def test_group_for_sanitises_every_segment():
    name = jobs.group_for("pin", "acc ount/1", "My Docs & Stuff")
    assert name == "onedriveui/pin/acc_ount_1/My_Docs_Stuff"
    assert all(part for part in name.split("/"))


def test_group_for_without_an_account_is_still_ours():
    assert jobs.group_for("verify") == "onedriveui/verify"
    assert jobs.is_ours(jobs.group_for("verify"))


def test_is_ours_rejects_rclones_own_and_a_strangers_groups():
    assert jobs.is_ours("job/17") is False
    assert jobs.is_ours("bigjob") is False
    assert jobs.is_ours("") is False
    assert jobs.is_ours("onedriveui/pin/acc") is True


def test_split_group_round_trips():
    assert jobs.split_group(jobs.group_for("pin", "acc")) == ("pin", "acc", "")
    assert jobs.split_group(jobs.group_for("bisync", "acc", "Offline")) == (
        ("bisync", "acc", "Offline"))
    assert jobs.split_group("job/3") == ("", "", "")


def test_every_documented_kind_produces_a_distinct_group():
    names = {jobs.group_for(kind, "acc") for kind in jobs.GROUP_KINDS}
    assert len(names) == len(jobs.GROUP_KINDS)


# ═════════════════════════════════════════════════════════════════════════════
# start() — the async handshake
# ═════════════════════════════════════════════════════════════════════════════

def test_start_sends_async_and_the_group(registry, rc, spy):
    registry.start("sync/copy", {"srcFs": "/src", "dstFs": "onedrive:"},
                   group=GROUP, label="Documents")
    record = rc.last("sync/copy")
    assert record is not None
    assert record.async_ is True
    assert record.params["_group"] == GROUP
    assert record.params["srcFs"] == "/src"


def test_start_emits_a_handle_carrying_the_execute_id(registry, rc, spy):
    registry.start("sync/copy", {}, group=GROUP, label="Documents")
    pump(rc)
    assert len(spy.started) == 1
    handle = spy.started[0]
    assert handle.job_id > 0
    assert handle.execute_id == rc.execute_id
    assert handle.group == GROUP
    assert handle.path == "sync/copy"
    assert handle.label == "Documents"
    assert handle.started_at


def test_start_refuses_a_job_with_no_group(registry):
    with pytest.raises(ValueError, match="_group"):
        registry.start("sync/copy", {}, group="")


def test_start_passes_config_and_filter_through(registry, rc):
    registry.start("sync/copy", {"srcFs": "/a", "dstFs": "onedrive:"},
                   group=GROUP, config={"Transfers": 4},
                   filt={"ExcludeRule": ["*.tmp"]})
    record = rc.last("sync/copy")
    assert record.params["_config"] == {"Transfers": 4}
    assert record.params["_filter"] == {"ExcludeRule": ["*.tmp"]}


def test_start_refuses_a_banned_endpoint(qapp):
    """The ban lives in the transport (`rc.guards`), so it fires synchronously
    out of `start()` — before a single byte reaches the network. Driven through
    a real `RcClient` here, because that is where the refusal lives."""
    from onedriveui.rc.client import RcClient

    client = RcClient(RcEndpoint(kind="rcd", host="127.0.0.1", port=17899))
    reg = jobs.JobRegistry(client)
    try:
        for path, invariant in (("mount/mount", "I7"),
                                ("mount/listmounts", "I7"),
                                ("operations/cleanup", "I8"),
                                ("config/dump", "I14")):
            with pytest.raises(SafetyRefusal) as excinfo:
                reg.start(path, {"fs": "onedrive:"}, group=GROUP)
            assert excinfo.value.invariant == invariant
        assert len(reg) == 0
    finally:
        reg.close()
        client.close()


def test_a_ticket_becomes_a_handle(registry, rc):
    ticket = registry.start("sync/copy", {}, group=GROUP)
    assert registry.handle_for(ticket) is None
    assert registry.pending() == [ticket]
    rc.flush()
    assert registry.handle_for(ticket) is not None


# ═════════════════════════════════════════════════════════════════════════════
# Terminal outcomes
# ═════════════════════════════════════════════════════════════════════════════

def test_a_finished_job_reports_its_whole_status_object(registry, rc, spy):
    registry.start("operations/size", {"fs": "onedrive:"}, group=GROUP)
    pump(rc)
    assert len(spy.finished) == 1
    handle, status = spy.finished[0]
    assert handle.path == "operations/size"
    assert status["finished"] is True
    assert status["output"] == {"count": 2, "bytes": 143_330}
    assert not spy.failed and not spy.expired and not spy.lost
    assert registry.active() == []


def test_a_job_that_finished_with_an_error_is_a_failure(registry, rc, spy):
    rc.set("job/status", lambda p: {
        "id": int(p["jobid"]), "finished": True, "success": False,
        "error": "quotaLimitReached", "output": {},
        "executeId": rc.execute_id})
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)
    assert not spy.finished
    assert len(spy.failed) == 1
    handle, error = spy.failed[0]
    assert isinstance(handle, JobHandle)
    assert isinstance(error, RcError)
    assert "quotaLimitReached" in error.message


def test_a_start_that_never_got_a_job_id_fails_with_no_handle(registry, rc, spy):
    rc.fail("sync/copy", status=500, message="didn't find section in config file")
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)
    assert len(spy.failed) == 1
    handle, error = spy.failed[0]
    assert handle is None
    assert isinstance(error, RcError)
    assert len(registry) == 0


def test_an_expired_job_emits_expired_not_failed(registry, rc, spy):
    """`job not found` with an UNCHANGED executeId. The job did finish; its
    outcome is simply no longer knowable, so the activity row becomes
    `interrupted` rather than `error`."""
    rc.script("job/status", [RcFault(status=500, message="job not found")])
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)

    assert len(spy.expired) == 1
    assert not spy.failed, "an expired job is NOT a failure"
    assert not spy.lost
    assert spy.expired[0].job_id > 0
    assert rc.count("job/list") == 1, (
        "'job not found' is ambiguous on its own and must be disambiguated "
        "against the daemon's current executeId")


def test_an_execute_id_change_emits_lost(registry, rc, spy):
    """`job not found` with a DIFFERENT executeId: the daemon restarted, so the
    job id names a different process's job or nothing at all."""
    rc.script("job/status", [RcFault(status=500, message="job not found")])
    rc.set("job/list", {"executeId": "a-brand-new-uuid", "jobids": [],
                        "runningIds": [], "finishedIds": []})
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)

    assert len(spy.lost) == 1
    assert not spy.expired, "a restart is not an expiry"
    assert not spy.failed


def test_a_status_carrying_a_new_execute_id_is_also_lost(registry, rc, spy):
    rc.set("job/status", lambda p: {"id": int(p["jobid"]), "finished": False,
                                    "executeId": "a-brand-new-uuid"})
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)
    assert len(spy.lost) == 1
    assert not spy.finished


def test_a_real_status_error_is_a_failure(registry, rc, spy):
    rc.script("job/status", [RcFault(status=500, message="something broke")])
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)
    assert len(spy.failed) == 1
    assert not spy.expired and not spy.lost


# ═════════════════════════════════════════════════════════════════════════════
# invalidate_all
# ═════════════════════════════════════════════════════════════════════════════

def test_invalidate_all_drops_every_handle_and_reports_lost(registry, rc, spy):
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    registry.start("sync/copy", {}, group=GROUP)
    registry.start("sync/sync", {}, group=GROUP)
    pump(rc)
    assert len(registry.active()) == 2

    dropped = registry.invalidate_all("the daemon restarted")

    assert len(dropped) == 2
    assert len(spy.lost) == 2
    assert registry.active() == []
    assert len(registry) == 0


def test_invalidate_all_sends_no_job_stop(registry, rc):
    """There is nothing left to stop: the process those ids belonged to is gone."""
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)
    registry.invalidate_all("restart")
    assert rc.count("job/stop") == 0


def test_the_bus_daemon_restarted_signal_invalidates(registry, rc, spy):
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    registry.start("sync/copy", {}, group=GROUP)
    pump(rc)
    BUS.daemon_restarted.emit("rcd", "a-new-execute-id")
    assert len(spy.lost) == 1
    assert registry.active() == []


def test_close_disconnects_from_the_bus(rc, qapp):
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    reg = jobs.JobRegistry(rc)
    lost: list[JobHandle] = []
    reg.lost.connect(lost.append)
    reg.start("sync/copy", {}, group=GROUP)
    pump(rc)
    reg.close()
    BUS.daemon_restarted.emit("rcd", "another-id")
    assert lost == [], "a closed registry must not react to the bus"


def test_a_registry_that_never_watched_the_bus_ignores_it(rc, qapp):
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    reg = jobs.JobRegistry(rc, watch_bus=False)
    try:
        reg.start("sync/copy", {}, group=GROUP)
        pump(rc)
        BUS.daemon_restarted.emit("rcd", "x")
        assert len(reg.active()) == 1
    finally:
        reg.close()


# ═════════════════════════════════════════════════════════════════════════════
# Cancellation
# ═════════════════════════════════════════════════════════════════════════════

def test_stop_sends_job_stop_for_a_started_job(registry, rc):
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    ticket = registry.start("sync/copy", {}, group=GROUP)
    pump(rc)
    handle = registry.handle_for(ticket)
    assert registry.stop(ticket) is True
    record = rc.last("job/stop")
    assert record is not None
    assert record.params["jobid"] == handle.job_id
    assert len(registry) == 0


def test_stop_before_the_reply_still_stops_the_job_that_lands(registry, rc):
    """The race that matters: aborting the HTTP request would not stop a job the
    daemon had already begun."""
    ticket = registry.start("sync/copy", {}, group=GROUP)
    assert registry.stop(ticket) is True
    assert rc.count("job/stop") == 0
    pump(rc)
    assert rc.count("job/stop") == 1
    assert len(registry) == 0


def test_stop_of_an_unknown_ticket_is_false(registry):
    assert registry.stop("t999") is False


def test_stop_group_cancels_the_whole_group(registry, rc):
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    registry.start("sync/copy", {}, group=GROUP)
    registry.start("sync/sync", {}, group=GROUP)
    pump(rc)
    assert registry.stop_group(GROUP) is True
    assert rc.last("job/stopgroup").params == {"group": GROUP}
    assert registry.active() == []


def test_stop_group_refuses_a_group_we_did_not_mint(registry, rc):
    """A shared daemon can carry jobs we did not start; cancelling those is
    sabotage."""
    with pytest.raises(ValueError, match="group_for"):
        registry.stop_group("bigjob")
    assert rc.count("job/stopgroup") == 0


# ═════════════════════════════════════════════════════════════════════════════
# core/stats-delete cleanup
# ═════════════════════════════════════════════════════════════════════════════

def test_the_last_job_out_of_a_group_deletes_it(registry, rc, spy):
    registry.start("operations/size", {"fs": "onedrive:"}, group=GROUP)
    pump(rc)
    assert spy.emptied == [GROUP]
    record = rc.last("core/stats-delete")
    assert record is not None
    assert record.params == {"group": GROUP}


def test_a_group_is_not_deleted_while_another_job_is_in_it(registry, rc):
    """A group survives its jobs; only the last one out turns the light off."""
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    first = registry.start("sync/copy", {}, group=GROUP)
    second = registry.start("sync/sync", {}, group=GROUP)
    pump(rc)
    assert len(registry.active()) == 2

    registry.stop(first)
    assert rc.count("core/stats-delete") == 0

    registry.stop(second)
    assert rc.count("core/stats-delete") == 1


def test_before_cleanup_runs_first_so_transferred_is_not_lost(rc, qapp):
    """`core/stats-delete` discards the group's `core/transferred` rows along
    with its counters, and those rows are the only record that the transfers
    happened. The hook has to run before the delete, not after."""
    order: list[str] = []
    rc.set("core/stats-delete", lambda p: order.append("delete") or {})
    reg = jobs.JobRegistry(rc, before_cleanup=lambda g: order.append(f"drain:{g}"))
    try:
        reg.start("operations/size", {"fs": "onedrive:"}, group=GROUP)
        pump(rc)
    finally:
        reg.close()
    assert order == [f"drain:{GROUP}", "delete"]


def test_a_failing_before_cleanup_cancels_the_delete(rc, qapp):
    """If the drain could not run, deleting the group would destroy history that
    was never persisted. Keep the group instead."""
    def boom(_group: str) -> None:
        raise RuntimeError("the writer is down")

    reg = jobs.JobRegistry(rc, before_cleanup=boom)
    try:
        reg.start("operations/size", {"fs": "onedrive:"}, group=GROUP)
        pump(rc)
    finally:
        reg.close()
    assert rc.count("core/stats-delete") == 0


def test_cleanup_can_be_switched_off(rc, qapp):
    reg = jobs.JobRegistry(rc, cleanup_groups=False)
    try:
        reg.start("operations/size", {"fs": "onedrive:"}, group=GROUP)
        pump(rc)
    finally:
        reg.close()
    assert rc.count("core/stats-delete") == 0


def test_a_foreign_group_is_never_deleted(rc, qapp, monkeypatch):
    """Only groups minted by `group_for` are ours to clean up."""
    reg = jobs.JobRegistry(rc)
    try:
        monkeypatch.setattr(jobs, "is_ours", lambda group: False)
        reg.start("operations/size", {"fs": "onedrive:"}, group=GROUP)
        pump(rc)
    finally:
        reg.close()
    assert rc.count("core/stats-delete") == 0


# ═════════════════════════════════════════════════════════════════════════════
# Bookkeeping
# ═════════════════════════════════════════════════════════════════════════════

def test_active_pending_and_groups_track_the_outstanding_work(registry, rc):
    rc.set("job/status", lambda p: running_status(rc, int(p["jobid"])))
    other = jobs.group_for("check", "onedrive")
    registry.start("sync/copy", {}, group=GROUP)
    registry.start("operations/check", {"srcFs": "/a", "dstFs": "b:"},
                   group=other)
    assert len(registry) == 2
    assert len(registry.pending()) == 2
    assert registry.active() == []
    pump(rc)
    assert len(registry.active()) == 2
    assert registry.pending() == []
    assert set(registry.groups()) == {GROUP, other}


def test_start_on_a_closed_registry_raises(rc, qapp):
    reg = jobs.JobRegistry(rc)
    reg.close()
    with pytest.raises(RuntimeError):
        reg.start("sync/copy", {}, group=GROUP)


def test_close_is_idempotent(rc, qapp):
    reg = jobs.JobRegistry(rc)
    reg.close()
    reg.close()


def test_an_endpoint_that_answers_inline_still_completes(registry, rc, spy,
                                                         monkeypatch):
    """A daemon that ignored `_async` must not leave a ticket outstanding for
    ever waiting for a job id that will never come."""
    class _Inline:
        def call(self, path, params=None, **kwargs):
            return rc.call(path, dict(params or {}), **{
                **kwargs, "async_": False})

    reg = jobs.JobRegistry(_Inline())
    seen: list[tuple[JobHandle, dict]] = []
    reg.finished.connect(lambda h, s: seen.append((h, s)))
    try:
        reg.start("operations/size", {"fs": "onedrive:"}, group=GROUP)
        pump(rc)
        assert len(reg) == 0
    finally:
        reg.close()
    assert len(seen) == 1
    assert seen[0][0].job_id == 0
    assert seen[0][1]["output"] == {"count": 2, "bytes": 143_330}
