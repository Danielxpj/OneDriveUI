"""WP-06 — `sync/bandwidth.py`.

Three verified rclone behaviours drive every test here:

* `_config.BwLimit` is accepted and echoed and does **not** throttle, so
  `core/bwlimit` is the only call that may be used;
* `core/bwlimit` echoes the rate back normalised to binary units, so the echo may
  never be string-compared against what was sent;
* the limit is process state, not configuration, so a restarted daemon comes back
  unlimited and has to be told again.
"""

from __future__ import annotations

import pytest

from onedriveui.constants import (
    AUTO_UPLOAD_PERCENT,
    BANDWIDTH_CEIL_KB,
    BANDWIDTH_FLOOR_KB,
)
from onedriveui.errors import RcError
from onedriveui.models import BandwidthState, RcEndpoint
from onedriveui.rc import ops
from onedriveui.sync.bandwidth import (
    AutoUploadController,
    BandwidthController,
    clamp_kb,
)
from onedriveui.units import kb_to_kib

RCD = RcEndpoint(kind="rcd", port=17800)
MOUNT = RcEndpoint(kind="mount", port=17801)


@pytest.fixture
def sent(monkeypatch):
    """Record every `core/bwlimit` call instead of making it."""
    calls: list[tuple[str, str]] = []

    def fake(rate, *, ep, timeout_s=None):
        calls.append((ep.kind, rate))
        # rclone normalises the echo to binary units, so the returned rate
        # deliberately does NOT match what was sent.
        return ops.BwLimit(rate="1Mi:100Ki", tx=1, rx=1)

    monkeypatch.setattr(ops, "set_bwlimit", fake)
    return calls


def controller(endpoints=(RCD, MOUNT)) -> BandwidthController:
    return BandwidthController(endpoints=lambda: list(endpoints))


# ═════════════════════════════════════════════════════════════════════════════
# Applying
# ═════════════════════════════════════════════════════════════════════════════

class TestApply:

    def test_the_limit_goes_to_both_daemons(self, qapp, sent):
        """The mount daemon is the one that moves bytes for ordinary file
        operations; throttling only the control plane limits almost nothing."""
        controller().apply(BandwidthState(download_kb=1000, upload_kb=100))
        assert [kind for kind, _rate in sent] == ["rcd", "mount"]

    def test_the_rate_is_rclones_upload_colon_download_order(self, qapp, sent):
        """rclone's pair order is the reverse of the settings page's."""
        controller().apply(BandwidthState(download_kb=1000, upload_kb=100))
        assert sent[0][1] == "98Ki:977Ki"

    def test_no_limit_is_off(self, qapp, sent):
        controller().apply(BandwidthState())
        assert sent[0][1] == "off"

    def test_the_echo_is_never_compared_against_what_was_sent(self, qapp, sent):
        """rclone re-normalises, so `98Ki:977Ki` can come home spelled
        differently. The configured KB values are the record."""
        ctrl = controller()
        ctrl.apply(BandwidthState(download_kb=1000, upload_kb=100))
        assert ctrl.current().download_kb == 1000
        assert ctrl.current().upload_kb == 100

    def test_the_conversion_goes_through_units(self, qapp, sent):
        """One function owns KB/s to KiB/s; a second copy would drift 2.4 %."""
        controller().apply(BandwidthState(download_kb=1000, upload_kb=None))
        assert f"{kb_to_kib(1000)}Ki" in sent[0][1]

    def test_a_daemon_that_refuses_does_not_stop_the_other(self, qapp, monkeypatch):
        done: list[str] = []

        def flaky(rate, *, ep, timeout_s=None):
            if ep.kind == "rcd":
                raise RcError("core/bwlimit", 500, {"error": "no"})
            done.append(ep.kind)
            return ops.BwLimit()

        monkeypatch.setattr(ops, "set_bwlimit", flaky)
        controller().apply(BandwidthState(download_kb=500))
        assert done == ["mount"]

    def test_the_intent_is_remembered_even_when_nothing_accepted_it(
            self, qapp, monkeypatch):
        """`reapply_after_restart()` exists precisely for this case."""
        monkeypatch.setattr(ops, "set_bwlimit", lambda *a, **kw: 1 / 0)
        ctrl = BandwidthController(endpoints=lambda: [])
        ctrl.apply(BandwidthState(download_kb=500))
        assert ctrl.current().download_kb == 500

    def test_the_bus_carries_the_change(self, qapp, sent, bus_spy):
        bus_spy.watch("bandwidth_changed")
        controller().apply(BandwidthState(download_kb=1000))
        assert bus_spy.count("bandwidth_changed") == 1


class TestClamping:

    def test_unlimited_stays_unlimited(self):
        assert clamp_kb(None) is None

    def test_below_the_floor_is_raised(self):
        """Below about 50 KB/s a large upload stops progressing at all rather
        than merely going slowly; "never finish" is not a limit worth offering."""
        assert clamp_kb(1) == BANDWIDTH_FLOOR_KB

    def test_above_the_ceiling_is_capped(self):
        assert clamp_kb(10_000_000) == BANDWIDTH_CEIL_KB

    def test_the_clamp_is_applied_before_anything_is_sent(self, qapp, sent):
        controller().apply(BandwidthState(download_kb=1, upload_kb=1))
        expected = f"{kb_to_kib(BANDWIDTH_FLOOR_KB)}Ki"
        assert sent[0][1] == expected


class TestReapply:

    def test_a_restarted_daemon_is_told_again(self, qapp, sent):
        """`core/bwlimit` is process state: a restarted daemon starts unlimited,
        and without this the user's limit silently disappears while the settings
        page keeps claiming it is set."""
        ctrl = controller()
        ctrl.apply(BandwidthState(download_kb=1000))
        sent.clear()
        ctrl.reapply_after_restart()
        assert [kind for kind, _rate in sent] == ["rcd", "mount"]

    def test_nothing_is_re_sent_when_nothing_was_limited(self, qapp, sent):
        ctrl = controller()
        ctrl.apply(BandwidthState())
        sent.clear()
        ctrl.reapply_after_restart()
        assert sent == []


class TestSetAuto:

    def test_turning_it_on_lifts_the_manual_upload_limit(self, qapp, sent):
        ctrl = controller()
        ctrl.apply(BandwidthState(download_kb=1000, upload_kb=500))
        ctrl.set_auto(True)
        assert ctrl.current().upload_auto is True
        assert ctrl.current().upload_kb is None

    def test_the_download_limit_is_untouched(self, qapp, sent):
        ctrl = controller()
        ctrl.apply(BandwidthState(download_kb=1000, upload_kb=500))
        ctrl.set_auto(True)
        assert ctrl.current().download_kb == 1000

    def test_the_default_share_is_microsofts_seventy_percent(self, qapp, sent):
        ctrl = controller()
        ctrl.set_auto(True)
        assert ctrl.current().auto_percent == AUTO_UPLOAD_PERCENT


# ═════════════════════════════════════════════════════════════════════════════
# "Adjust automatically"
# ═════════════════════════════════════════════════════════════════════════════

class TestAutoUploadController:

    def test_the_limit_is_lifted_while_measuring(self, qapp, sent):
        """Achieved throughput under a limit describes the limit, not the
        connection. A controller that only sampled while throttled would take
        70 % of its own 70 % every period until uploads crawled."""
        ctrl = controller()
        ctrl.apply(BandwidthState(download_kb=1000, upload_kb=500))
        auto = AutoUploadController(ctrl, throughput=lambda: 10_000_000.0)
        auto._begin_measurement()
        assert ctrl.current().upload_kb is None

    def test_it_applies_seventy_percent_of_what_it_measured(self, qapp, sent):
        ctrl = controller()
        auto = AutoUploadController(ctrl, throughput=lambda: 10_000_000.0)
        auto._begin_measurement()
        auto._end_measurement()
        assert auto.measured_kb == 10_000
        assert ctrl.current().upload_kb == 7_000

    def test_an_idle_connection_teaches_nothing_and_changes_nothing(self, qapp, sent):
        """Throttling to a floor derived from an idle connection would cap the
        next real upload at 50 KB/s."""
        ctrl = controller()
        auto = AutoUploadController(ctrl, throughput=lambda: 0.0)
        auto._begin_measurement()
        auto._end_measurement()
        assert ctrl.current().upload_kb is None

    def test_the_measurement_is_clamped_like_any_other_limit(self, qapp, sent):
        ctrl = controller()
        auto = AutoUploadController(ctrl, throughput=lambda: 1_000.0)
        auto._begin_measurement()
        auto._end_measurement()
        assert ctrl.current().upload_kb == BANDWIDTH_FLOOR_KB

    def test_a_broken_throughput_source_does_not_crash_it(self, qapp, sent):
        ctrl = controller()
        auto = AutoUploadController(ctrl, throughput=lambda: "not a number")
        auto._begin_measurement()
        auto._end_measurement()
        assert ctrl.current().upload_kb is None

    def test_stop_cancels_both_timers(self, qapp, sent):
        ctrl = controller()
        auto = AutoUploadController(ctrl, throughput=lambda: 1.0)
        auto.start()
        auto.stop()
        assert not auto._period.isActive()
        assert not auto._burst.isActive()
