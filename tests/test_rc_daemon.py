"""WP-02 — `onedriveui/rc/daemon.py`.

The ownership proof is the point of this module, so most of these tests are
about refusing to drive something. A synthetic `/proc` tree (via `rc.PROC`) makes
every branch of the proof deterministic; the `live`-marked tests then run the
same proof against the *real* foreign rclone this machine is already running on
127.0.0.1:5572 and confirm it is rejected.

`platform/systemd.py` (WP-10) is injected, so `_Systemd` below is both the test
double and an executable statement of the protocol `RcdSupervisor` needs.
"""

from __future__ import annotations

import pathlib
import socket

import pytest

from onedriveui import USER_AGENT
from onedriveui.constants import (
    RC_JOB_EXPIRE,
    RC_JOB_EXPIRE_INTERVAL,
    RCD_MAX_FAILURES,
    UNIT_RCD,
)
from onedriveui.errors import DaemonForeign, DaemonUnavailable
from onedriveui.models import DaemonHealth, RcEndpoint
from onedriveui.rc import daemon as daemon_mod
from onedriveui.rc import endpoints as _endpoints
from onedriveui.rc.daemon import RcdSupervisor, execute_id_of, unit_escape
from tests.conftest import REAL_HOME

FOREIGN_PORT = 5572          # the user's own `rclone mount … --rc-addr 5572`

#: The /proc/<pid>/stat field-22 value the synthetic proc tree and the FakeRc's
#: endpoint agree on, so the anti-PID-reuse check passes unless a test breaks it.
STARTTIME = 55501


class _Systemd:
    """Records what the supervisor asked the service manager to do."""

    def __init__(self) -> None:
        self.units: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.active: set[str] = set()
        self.fail_on: set[str] = set()

    def _note(self, verb: str, name: str) -> None:
        self.calls.append((verb, name))
        if verb in self.fail_on:
            raise RuntimeError(f"{verb} refused by the test double")

    def write_unit(self, name: str, text: str):
        self._note("write_unit", name)
        self.units[name] = text
        return name

    def daemon_reload(self):
        self._note("daemon_reload", "")

    def enable(self, name: str):
        self._note("enable", name)

    def start(self, name: str):
        self._note("start", name)
        self.active.add(name)

    def stop(self, name: str):
        self._note("stop", name)
        self.active.discard(name)

    def restart(self, name: str):
        self._note("restart", name)
        self.active.add(name)

    def is_active(self, name: str) -> bool:
        return name in self.active

    def status_text(self, name: str) -> str:
        return ""

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _name in self.calls]


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    """A synthetic `/proc` whose entries the tests write by hand."""
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(daemon_mod, "PROC", root, raising=False)
    monkeypatch.setattr("onedriveui.rc.PROC", root)

    def add(pid: int, argv: list[str], starttime: int = STARTTIME) -> int:
        entry = root / str(pid)
        entry.mkdir(exist_ok=True)
        (entry / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        # /proc/<pid>/stat: field 1 pid, field 2 (comm), field 3 state, ...
        # Field 22 is starttime. Splitting after the last ") " puts field 3 at
        # index 0, so starttime is index 19 and needs 18 fillers before it.
        # Verified against a real /proc/self/stat.
        tail = " ".join(["0"] * 18 + [str(starttime)] + ["0"] * 32)
        (entry / "stat").write_text(f"{pid} (rclone) S {tail}\n", encoding="utf-8")
        return pid

    return add


@pytest.fixture
def rcd_rc(monkeypatch):
    """Route the supervisor's blocking probes at a `FakeRc`."""
    from tests.fakes import fake_rc as fake_rc_mod
    from tests.fakes.fake_rc import FakeRc, reset_registry

    rc = FakeRc(deliver_mode="manual")
    rc.endpoint = _endpoints.with_identity(rc.endpoint, starttime=STARTTIME)
    monkeypatch.setattr(daemon_mod, "call_blocking", fake_rc_mod.call_blocking)
    monkeypatch.setattr(daemon_mod, "is_alive", fake_rc_mod.is_alive)
    try:
        yield rc
    finally:
        rc.close()
        reset_registry()


def _ours(port: int, *, pid: int = 4242) -> list[str]:
    return ["/usr/bin/rclone", "rcd", "--rc-addr", f"127.0.0.1:{port}",
            "--rc-user", "onedriveui", "--rc-pass", "x",
            "--rc-job-expire-duration", "10m"]


def _foreign_mount(port: int) -> list[str]:
    """The exact argv of the rclone already running on this machine."""
    return ["/usr/bin/rclone", "mount", "onedrive:", "/home/u/OneDrive",
            "--vfs-cache-mode", "full", "--onedrive-chunk-size", "30M",
            "--rc", "--rc-addr", f"127.0.0.1:{port}", "--rc-no-auth"]


# ═════════════════════════════════════════════════════════════════════════════
# The unit file
# ═════════════════════════════════════════════════════════════════════════════

class TestUnitText:
    @pytest.fixture
    def text(self):
        return RcdSupervisor.unit_text(17800, "onedriveui", "s3cret")

    def test_it_runs_rcd_on_our_exact_address(self, text):
        assert "/usr/bin/rclone rcd" in text
        assert "--rc-addr 127.0.0.1:17800" in text
        assert "--rc-user onedriveui --rc-pass s3cret" in text

    def test_the_job_expiry_is_ten_minutes_not_rclone_s_sixty_seconds(self, text):
        """rclone's 60 s default garbage-collects a finished job's `output`
        before a restarted GUI can read it."""
        assert f"--rc-job-expire-duration {RC_JOB_EXPIRE}" in text
        assert f"--rc-job-expire-interval {RC_JOB_EXPIRE_INTERVAL}" in text
        assert RC_JOB_EXPIRE == "10m"

    def test_it_is_a_simple_always_restarting_service(self, text):
        assert "Type=simple" in text
        assert "Restart=always" in text
        assert "RestartSec=5" in text
        assert "WantedBy=default.target" in text

    def test_network_online_target_is_deliberately_absent(self, text):
        """It does not exist in the systemd --user manager; After=/Wants= on it
        are silently ignored, so emitting it would only mislead."""
        assert "network-online.target" not in text

    def test_it_orders_against_the_graphical_session(self, text):
        assert "After=graphical-session-pre.target" in text
        assert "PartOf=graphical-session.target" in text

    def test_it_carries_the_throttle_priority_user_agent(self, text):
        assert f"--user-agent {USER_AGENT}" in text
        assert USER_AGENT.startswith("ISV|OneDriveUI|OneDriveUI/")

    def test_it_logs_structured_json_without_ansi_escapes(self, text):
        assert "--use-json-log" in text
        assert "--color NEVER" in text

    def test_it_disables_the_periodic_stats_line(self, text):
        assert "--stats 0" in text

    def test_the_control_plane_never_mounts_anything(self, text):
        assert " mount " not in text
        assert "--vfs-cache-mode" not in text

    def test_it_carries_no_backend_flag(self):
        from onedriveui.rc import guards

        text = RcdSupervisor.unit_text(17800, "onedriveui", "p")
        exec_start = next(line for line in text.splitlines()
                          if line.startswith("ExecStart="))
        guards.assert_no_backend_flags(exec_start[len("ExecStart="):].split())

    def test_a_percent_in_a_value_is_escaped_for_systemd(self):
        """`%` introduces a specifier; an unescaped one silently expands."""
        text = RcdSupervisor.unit_text(17800, "onedriveui", "a%hb")
        assert "--rc-pass a%%hb" in text
        assert unit_escape("100%") == "100%%"


# ═════════════════════════════════════════════════════════════════════════════
# The ownership proof
# ═════════════════════════════════════════════════════════════════════════════

class TestVerifyOwnership:
    def test_our_own_rcd_is_accepted(self, rcd_rc, fake_proc):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        assert RcdSupervisor.verify_ownership(rcd_rc.endpoint) is True

    def test_a_daemon_whose_argv_says_mount_is_rejected(self, rcd_rc, fake_proc):
        """This is precisely the shape of the rclone already on 127.0.0.1:5572."""
        fake_proc(rcd_rc.pid, _foreign_mount(rcd_rc.endpoint.port))
        assert RcdSupervisor.verify_ownership(rcd_rc.endpoint) is False

    def test_a_daemon_listening_on_another_address_is_rejected(self, rcd_rc, fake_proc):
        fake_proc(rcd_rc.pid, _ours(5572))
        assert RcdSupervisor.verify_ownership(rcd_rc.endpoint) is False

    def test_an_argv_without_rc_addr_at_all_is_rejected(self, rcd_rc, fake_proc):
        fake_proc(rcd_rc.pid, ["/usr/bin/rclone", "rcd", "--rc-no-auth"])
        assert RcdSupervisor.verify_ownership(rcd_rc.endpoint) is False

    def test_a_recycled_pid_is_rejected_on_starttime(self, rcd_rc, fake_proc):
        """`/proc/<pid>/stat` field 22 is what stops a wrapped pid from
        impersonating the daemon we started."""
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port), starttime=99999)
        ep = _endpoints.with_identity(rcd_rc.endpoint, starttime=STARTTIME)
        assert RcdSupervisor.verify_ownership(ep) is False

    def test_a_matching_starttime_is_accepted(self, rcd_rc, fake_proc):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port), starttime=STARTTIME)
        ep = _endpoints.with_identity(rcd_rc.endpoint, starttime=STARTTIME)
        assert RcdSupervisor.verify_ownership(ep) is True

    def test_a_vanished_process_is_rejected_without_raising(self, rcd_rc, fake_proc):
        assert RcdSupervisor.verify_ownership(rcd_rc.endpoint) is False

    def test_an_unreachable_daemon_is_rejected_without_raising(self, rcd_rc):
        rcd_rc.stop()
        assert RcdSupervisor.verify_ownership(rcd_rc.endpoint) is False

    def test_a_portless_endpoint_is_rejected(self):
        assert RcdSupervisor.verify_ownership(RcEndpoint(kind="rcd")) is False

    def test_a_mount_endpoint_is_proved_by_its_mountpoint_not_by_rcd(
            self, rcd_rc, fake_proc):
        port = rcd_rc.endpoint.port
        fake_proc(rcd_rc.pid, _foreign_mount(port)[:4] + [
            "--rc-addr", f"127.0.0.1:{port}"])
        mount_ep = RcEndpoint(kind="mount", host="127.0.0.1", port=port,
                              mountpoint="/home/u/OneDrive", account_id="onedrive")
        assert RcdSupervisor.verify_ownership(mount_ep) is True
        wrong = RcEndpoint(kind="mount", host="127.0.0.1", port=port,
                           mountpoint="/somewhere/else", account_id="onedrive")
        assert RcdSupervisor.verify_ownership(wrong) is False

    def test_the_proof_sends_only_read_only_calls(self, rcd_rc, fake_proc):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        RcdSupervisor.verify_ownership(rcd_rc.endpoint)
        assert {record.path for record in rcd_rc.calls} == {"core/pid"}
        for mutating in ("core/quit", "config/create", "config/update",
                         "operations/purge", "operations/deletefile",
                         "sync/sync", "job/stop"):
            rcd_rc.assert_never(mutating)


class TestProcReaders:
    """The two package-internal `/proc` readers the proof is built on."""

    def test_the_cmdline_splits_on_nul_and_drops_the_terminator(self, fake_proc):
        from onedriveui.rc import read_proc_cmdline

        fake_proc(9001, ["/usr/bin/rclone", "rcd", "--rc-addr", "127.0.0.1:17800"])
        assert read_proc_cmdline(9001) == [
            "/usr/bin/rclone", "rcd", "--rc-addr", "127.0.0.1:17800"]

    def test_a_vanished_process_yields_an_empty_argv(self, fake_proc):
        from onedriveui.rc import read_proc_cmdline

        assert read_proc_cmdline(9002) == []
        assert read_proc_cmdline(-1) == []

    def test_the_starttime_is_field_22(self, fake_proc):
        from onedriveui.rc import read_proc_starttime

        fake_proc(9003, ["rclone", "rcd"], starttime=3506100)
        assert read_proc_starttime(9003) == 3506100

    def test_a_hostile_argv0_containing_a_paren_does_not_shift_the_field(
            self, tmp_path, monkeypatch):
        """Field 2 is the executable name in parentheses and may itself contain
        spaces and `)`, so `split()[21]` is wrong for a process that chose one."""
        from onedriveui.rc import read_proc_starttime

        root = tmp_path / "proc2"
        (root / "9004").mkdir(parents=True)
        monkeypatch.setattr("onedriveui.rc.PROC", root)
        tail = " ".join(["0"] * 18 + ["777777"] + ["0"] * 32)
        (root / "9004" / "stat").write_text(
            f"9004 (evil ) name) S {tail}\n", encoding="utf-8")
        assert read_proc_starttime(9004) == 777777

    def test_an_unreadable_stat_yields_zero_not_an_exception(self, fake_proc):
        from onedriveui.rc import read_proc_starttime

        assert read_proc_starttime(9005) == 0

    @pytest.mark.live
    def test_it_agrees_with_the_real_proc_for_this_process(self):
        """The synthetic tree is only trustworthy if the parser is right about
        the real one."""
        import os as _os

        from onedriveui.rc import read_proc_cmdline, read_proc_starttime

        pid = _os.getpid()
        assert read_proc_starttime(pid) > 0
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
        expected = int(raw[raw.rfind(")") + 1:].split()[19])
        assert read_proc_starttime(pid) == expected
        assert read_proc_cmdline(pid)[0].endswith("python3")


class TestExecuteId:
    def test_it_reads_the_uuid_from_job_list(self, rcd_rc, monkeypatch):
        from tests.fakes import fake_rc as fake_rc_mod

        monkeypatch.setattr(daemon_mod, "call_blocking", fake_rc_mod.call_blocking)
        assert execute_id_of(rcd_rc.endpoint) == rcd_rc.execute_id

    def test_it_changes_after_a_restart(self, rcd_rc, monkeypatch):
        from tests.fakes import fake_rc as fake_rc_mod

        monkeypatch.setattr(daemon_mod, "call_blocking", fake_rc_mod.call_blocking)
        before = execute_id_of(rcd_rc.endpoint)
        rcd_rc.restart()
        after = execute_id_of(rcd_rc.endpoint)
        assert before and after and before != after

    def test_an_unreachable_daemon_yields_the_empty_string_not_an_exception(
            self, rcd_rc, monkeypatch):
        from tests.fakes import fake_rc as fake_rc_mod

        monkeypatch.setattr(daemon_mod, "call_blocking", fake_rc_mod.call_blocking)
        rcd_rc.stop()
        assert execute_id_of(rcd_rc.endpoint) == ""


# ═════════════════════════════════════════════════════════════════════════════
# ensure_running
# ═════════════════════════════════════════════════════════════════════════════

class TestAdoptWithoutEnsureRunning:
    """A process that never called `ensure_running()` must still see the daemon.

    `onedriveui --status`, `--doctor`, a second window — anything short-lived —
    builds its own `RcdSupervisor` and never provisions anything. Before this,
    `health()` read a field only `ensure_running()` fills, so every one of them
    reported DOWN for a daemon that was demonstrably alive and ours. A health
    check that says "down" about a running daemon is worse than no health check.
    """

    def test_a_persisted_endpoint_is_adopted(self, rcd_rc, fake_proc, qapp):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)

        fresh = RcdSupervisor(_Systemd())          # never provisioned anything
        assert fresh.endpoint() is not None
        assert fresh.endpoint().port == rcd_rc.endpoint.port
        assert fresh.health() is DaemonHealth.UP

    def test_adoption_still_has_to_pass_the_ownership_proof(self, rcd_rc, qapp):
        """Adopting is not trusting. The endpoint is read from a file; the proof
        checks the pid, the argv, the exact --rc-addr and the start time."""
        _endpoints.save_endpoint(rcd_rc.endpoint)
        # No `fake_proc`, so /proc cannot corroborate the claim.
        fresh = RcdSupervisor(_Systemd())
        assert fresh.health() is not DaemonHealth.UP

    def test_nothing_persisted_is_still_down(self, qapp):
        _endpoints.clear_endpoints()
        fresh = RcdSupervisor(_Systemd())
        assert fresh.endpoint() is None
        assert fresh.health() is DaemonHealth.DOWN

    def test_a_rejected_foreign_endpoint_is_never_re_adopted(self, rcd_rc, qapp):
        """The regression this nearly caused.

        Adoption re-reads the endpoints file, so a supervisor that had already
        refused a foreign daemon would resurrect it on the next `endpoint()`
        call and hand it to a caller who would then drive it. That is precisely
        what the ownership proof exists to prevent.
        """
        _endpoints.save_endpoint(rcd_rc.endpoint)
        supervisor = RcdSupervisor(_Systemd())
        supervisor._foreign = rcd_rc.endpoint       # as `health()` would set it
        assert supervisor.endpoint() is None

    def test_adopting_starts_nothing(self, rcd_rc, fake_proc, qapp):
        """Asking a question must not provision a daemon as a side effect."""
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        systemd = _Systemd()
        RcdSupervisor(systemd).health()
        assert systemd.calls == []


class TestEnsureRunning:
    def test_a_live_owned_daemon_is_adopted_without_touching_systemd(
            self, rcd_rc, fake_proc, qapp):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        systemd = _Systemd()
        supervisor = RcdSupervisor(systemd)
        ep = supervisor.ensure_running()
        assert systemd.calls == []
        assert ep.port == rcd_rc.endpoint.port
        assert ep.pid == rcd_rc.pid
        assert ep.execute_id == rcd_rc.execute_id
        assert ep.starttime == STARTTIME
        assert supervisor.health() is DaemonHealth.UP

    def test_the_adopted_identity_is_persisted(self, rcd_rc, fake_proc, qapp):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        RcdSupervisor(_Systemd()).ensure_running()
        stored = _endpoints.load_endpoints()["rcd"]
        assert stored.execute_id == rcd_rc.execute_id
        assert stored.starttime == STARTTIME

    def test_a_live_foreign_daemon_raises_rather_than_being_driven(
            self, rcd_rc, fake_proc, qapp):
        """Acceptance: the supervisor raises DaemonForeign rather than adopting,
        reconfiguring or quitting a daemon it cannot prove is ours."""
        fake_proc(rcd_rc.pid, _foreign_mount(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        systemd = _Systemd()
        supervisor = RcdSupervisor(systemd)
        with pytest.raises(DaemonForeign) as excinfo:
            supervisor.ensure_running()
        assert "ownership proof" in str(excinfo.value)
        assert systemd.calls == []
        assert supervisor.health() is DaemonHealth.FOREIGN
        assert supervisor.endpoint() is None
        rcd_rc.assert_never("core/quit")

    def test_a_foreign_daemon_that_goes_away_stops_being_foreign(
            self, rcd_rc, fake_proc, qapp):
        """FOREIGN means "someone else owns your port", which needs different
        words from "nothing is running" — but only while it is true."""
        fake_proc(rcd_rc.pid, _foreign_mount(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        supervisor = RcdSupervisor(_Systemd())
        with pytest.raises(DaemonForeign):
            supervisor.ensure_running()
        assert supervisor.health() is DaemonHealth.FOREIGN
        rcd_rc.stop()
        assert supervisor.health() is DaemonHealth.DOWN

    def test_nothing_recorded_means_provision(self, rcd_rc, fake_proc, monkeypatch,
                                              qapp):
        """With no endpoint on disk the supervisor writes the unit, reloads,
        enables and starts it — in that order — then proves what answered."""
        from tests.fakes.fake_rc import registry

        port = _endpoints.pick_free_port()
        monkeypatch.setattr(_endpoints, "pick_free_port", lambda **kw: port)
        registry[("127.0.0.1", port)] = rcd_rc
        rcd_rc.endpoint = _endpoints.with_identity(rcd_rc.endpoint, port=port)
        fake_proc(rcd_rc.pid, _ours(port))

        systemd = _Systemd()
        supervisor = RcdSupervisor(systemd, startup_grace_s=1.0)
        ep = supervisor.ensure_running()

        assert systemd.verbs == ["write_unit", "daemon_reload", "enable", "start"]
        assert UNIT_RCD in systemd.units
        assert f"--rc-addr 127.0.0.1:{port}" in systemd.units[UNIT_RCD]
        assert ep.port == port
        assert ep.execute_id == rcd_rc.execute_id
        assert ep.pid == rcd_rc.pid
        assert _endpoints.load_endpoints()["rcd"].port == port
        assert supervisor.health() is DaemonHealth.UP

    def test_the_generated_password_is_fresh_and_reaches_the_unit(
            self, rcd_rc, fake_proc, monkeypatch, qapp):
        from tests.fakes.fake_rc import registry

        port = _endpoints.pick_free_port()
        monkeypatch.setattr(_endpoints, "pick_free_port", lambda **kw: port)
        registry[("127.0.0.1", port)] = rcd_rc
        rcd_rc.endpoint = _endpoints.with_identity(rcd_rc.endpoint, port=port)
        fake_proc(rcd_rc.pid, _ours(port))
        systemd = _Systemd()
        ep = RcdSupervisor(systemd, startup_grace_s=1.0).ensure_running()
        assert len(ep.password) == 43
        assert f"--rc-pass {ep.password}" in systemd.units[UNIT_RCD]

    def test_a_unit_that_never_answers_raises_daemon_unavailable(self, qapp,
                                                                 monkeypatch):
        monkeypatch.setattr(daemon_mod, "is_alive", lambda ep, timeout_s=1.0: False)
        systemd = _Systemd()
        supervisor = RcdSupervisor(systemd, startup_grace_s=0.2)
        with pytest.raises(DaemonUnavailable) as excinfo:
            supervisor.ensure_running()
        assert excinfo.value.status == 503
        assert "did not answer rc/noop" in excinfo.value.message
        assert supervisor.health() is DaemonHealth.DOWN

    def test_a_recorded_but_dead_endpoint_provisions_a_new_one(
            self, rcd_rc, monkeypatch, qapp):
        _endpoints.save_endpoint(
            RcEndpoint(kind="rcd", host="127.0.0.1", port=17899, user="u",
                       password="p"))
        monkeypatch.setattr(daemon_mod, "is_alive", lambda ep, timeout_s=1.0: False)
        systemd = _Systemd()
        supervisor = RcdSupervisor(systemd, startup_grace_s=0.05)
        with pytest.raises(DaemonUnavailable):
            supervisor.ensure_running()
        assert "write_unit" in systemd.verbs      # it tried to provision


# ═════════════════════════════════════════════════════════════════════════════
# health, restart, stop
# ═════════════════════════════════════════════════════════════════════════════

class TestHealth:
    def test_no_endpoint_is_down(self, qapp):
        assert RcdSupervisor(_Systemd()).health() is DaemonHealth.DOWN

    def test_health_transitions_are_published_on_the_bus(self, rcd_rc, fake_proc,
                                                         bus_spy, qapp):
        bus_spy.watch("daemon_health")
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        supervisor = RcdSupervisor(_Systemd())
        supervisor.ensure_running()
        rcd_rc.stop()
        assert supervisor.health() is DaemonHealth.DOWN
        seen = bus_spy.of("daemon_health")
        assert ("rcd", DaemonHealth.UP) in seen
        assert ("rcd", DaemonHealth.DOWN) in seen

    def test_a_daemon_that_stops_being_ours_is_foreign_not_up(self, rcd_rc,
                                                              fake_proc, qapp):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        supervisor = RcdSupervisor(_Systemd())
        supervisor.ensure_running()
        fake_proc(rcd_rc.pid, _foreign_mount(rcd_rc.endpoint.port))
        assert supervisor.health() is DaemonHealth.FOREIGN


class TestRestart:
    def _running(self, rcd_rc, fake_proc):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        supervisor = RcdSupervisor(_Systemd(), startup_grace_s=0.5)
        supervisor.ensure_running()
        return supervisor

    def test_a_restart_that_changes_the_execute_id_is_announced(
            self, rcd_rc, fake_proc, bus_spy, qapp):
        bus_spy.watch("daemon_restarted")
        supervisor = self._running(rcd_rc, fake_proc)
        seen: list[str] = []
        supervisor.restarted.connect(seen.append)
        before = rcd_rc.execute_id
        rcd_rc.restart()
        supervisor.restart("three consecutive rc failures")
        assert seen == [rcd_rc.execute_id]
        assert rcd_rc.execute_id != before
        assert ("rcd", rcd_rc.execute_id) in bus_spy.of("daemon_restarted")

    def test_a_restart_that_does_not_change_it_announces_nothing(
            self, rcd_rc, fake_proc, qapp):
        supervisor = self._running(rcd_rc, fake_proc)
        seen: list[str] = []
        supervisor.restarted.connect(seen.append)
        supervisor.restart("probe")
        assert seen == []

    def test_the_unit_is_restarted_not_recreated(self, rcd_rc, fake_proc, qapp):
        supervisor = self._running(rcd_rc, fake_proc)
        supervisor._systemd.calls.clear()
        supervisor.restart("probe")
        assert supervisor._systemd.verbs == ["restart"]

    def test_it_gives_up_after_the_failure_budget(self, rcd_rc, fake_proc, qapp):
        """After five failures in five minutes, stop retrying — a daemon that
        cannot stay up is a Report-a-problem case, not a tighter retry loop."""
        supervisor = self._running(rcd_rc, fake_proc)
        supervisor._grace = 0.01
        rcd_rc.stop()                       # every restart now fails to come back
        for _ in range(RCD_MAX_FAILURES):
            supervisor.restart("flapping")
            assert supervisor.health() is DaemonHealth.DOWN
        supervisor._systemd.calls.clear()
        supervisor.restart("flapping")
        assert supervisor._systemd.calls == []
        assert supervisor.recent_failures > RCD_MAX_FAILURES
        assert supervisor.health() is DaemonHealth.DOWN

    def test_recent_failures_are_counted_in_a_sliding_window(self, rcd_rc,
                                                             fake_proc, qapp):
        supervisor = self._running(rcd_rc, fake_proc)
        assert supervisor.recent_failures == 0
        supervisor.restart("one")
        assert supervisor.recent_failures == 1


class TestStop:
    def test_it_stops_the_unit_and_forgets_the_endpoint(self, rcd_rc, fake_proc,
                                                        qapp):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        systemd = _Systemd()
        supervisor = RcdSupervisor(systemd)
        supervisor.ensure_running()
        supervisor.stop()
        assert ("stop", UNIT_RCD) in systemd.calls
        assert supervisor.endpoint() is None
        assert _endpoints.load_endpoints() == {}
        assert supervisor.health() is DaemonHealth.DOWN

    def test_it_never_sends_core_quit(self, rcd_rc, fake_proc, qapp):
        """rc access is shell access; quitting a daemon we do not own would
        unmount the user's real OneDrive."""
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        supervisor = RcdSupervisor(_Systemd())
        supervisor.ensure_running()
        supervisor.stop()
        rcd_rc.assert_never("core/quit")

    def test_the_endpoint_is_forgotten_even_if_systemd_fails(self, rcd_rc,
                                                             fake_proc, qapp):
        fake_proc(rcd_rc.pid, _ours(rcd_rc.endpoint.port))
        _endpoints.save_endpoint(rcd_rc.endpoint)
        systemd = _Systemd()
        systemd.fail_on.add("stop")
        supervisor = RcdSupervisor(systemd)
        supervisor.ensure_running()
        with pytest.raises(RuntimeError):
            supervisor.stop()
        assert supervisor.endpoint() is None
        assert _endpoints.load_endpoints() == {}


# ═════════════════════════════════════════════════════════════════════════════
# Against this machine's real foreign rclone
# ═════════════════════════════════════════════════════════════════════════════

def _foreign_is_listening() -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", FOREIGN_PORT)) == 0
    finally:
        sock.close()


@pytest.mark.live
class TestAgainstTheForeignDaemon:
    """Read-only probes of the `rclone mount … --rc-addr 127.0.0.1:5572` this
    machine is already running. Only `rc/noop` and `core/pid` are ever sent, and
    neither mutates anything."""

    @pytest.fixture(autouse=True)
    def _require_foreign(self):
        if not _foreign_is_listening():
            pytest.skip(f"nothing is listening on 127.0.0.1:{FOREIGN_PORT}")

    @staticmethod
    def _endpoint() -> RcEndpoint:
        return RcEndpoint(kind="rcd", host="127.0.0.1", port=FOREIGN_PORT,
                          user="onedriveui", password="not-its-password")

    def test_verify_ownership_returns_false_for_it(self):
        """Acceptance: the pre-existing foreign rclone on 127.0.0.1:5572 fails
        the proof — its argv says `mount`, not `rcd`."""
        assert RcdSupervisor.verify_ownership(self._endpoint()) is False

    def test_it_is_not_adopted_by_an_endpoint_claiming_kind_mount(self):
        """The argv proof alone is NOT enough here.

        The foreign process really is `rclone mount onedrive: ~/OneDrive …
        --rc-addr 127.0.0.1:5572`, so a stale or hand-edited endpoints.json
        claiming `kind="mount"` at that mountpoint satisfies every argv test:
        `--rc-addr` is present, `127.0.0.1:5572` is present, and the mountpoint
        is present. Only the forbidden-port refusal stops it being adopted and
        then force-unmounted out from under the user's own client.
        """
        claim = RcEndpoint(kind="mount", host="127.0.0.1", port=FOREIGN_PORT,
                           mountpoint=str(REAL_HOME / "OneDrive"),
                           account_id="onedrive")
        assert RcdSupervisor.verify_ownership(claim) is False

    def test_the_supervisor_raises_daemon_foreign_rather_than_driving_it(self):
        systemd = _Systemd()
        supervisor = RcdSupervisor(systemd)
        supervisor._endpoint = self._endpoint()
        with pytest.raises(DaemonForeign):
            supervisor.ensure_running()
        assert systemd.calls == []
        assert supervisor.health() is DaemonHealth.FOREIGN

    def test_its_port_is_never_handed_out_by_pick_free_port(self):
        assert _endpoints.port_is_free(FOREIGN_PORT) is False
        for _ in range(10):
            assert _endpoints.pick_free_port() != FOREIGN_PORT
