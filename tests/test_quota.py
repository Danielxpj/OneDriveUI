"""WP-06 — `sync/quota.py`.

`operations/about` is one Graph request that answers three questions — how much
space is left, whether the token still works, and whether the drive is full — so
the tests here are mostly about *not asking it*: the TTL, the forced refresh
after a big job, and the cases where a failure says something about the token
and the cases where it says nothing at all.
"""

from __future__ import annotations

import pytest

from onedriveui.constants import QUOTA_TTL_S
from onedriveui.errors import DaemonUnavailable, RcError
from onedriveui.models import AccountInfo, QuotaInfo, RcEndpoint, TokenHealth
from onedriveui.rc import ops
from onedriveui.sync.quota import QuotaService

ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")
ENDPOINT = RcEndpoint(kind="rcd", port=17800)

ROOMY = QuotaInfo(total=1_104_880_336_896, used=252_544_077_005,
                  free=852_336_259_891)


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@pytest.fixture
def about(monkeypatch):
    """Record every `operations/about` call and control what it answers."""
    calls: list[str] = []
    answer: dict[str, object] = {"quota": ROOMY, "raise": None}

    def fake(fs, *, ep, timeout_s=None):
        calls.append(fs)
        if answer["raise"] is not None:
            raise answer["raise"]
        return answer["quota"]

    monkeypatch.setattr(ops, "about", fake)
    return calls, answer


def service(clock=None) -> QuotaService:
    return QuotaService(ACCOUNT, endpoint=lambda: ENDPOINT,
                        monotonic=clock or Clock())


# ═════════════════════════════════════════════════════════════════════════════
# The cache
# ═════════════════════════════════════════════════════════════════════════════

class TestCache:

    def test_the_first_refresh_asks(self, qapp, about):
        calls, _answer = about
        quota = service().refresh()
        assert calls == ["onedrive:"]
        assert quota.total == ROOMY.total

    def test_a_fresh_sample_is_not_re_asked(self, qapp, about):
        """Three callers want this number every tick; one Graph request per five
        minutes is the whole point."""
        calls, _answer = about
        svc = service()
        svc.refresh()
        svc.refresh()
        svc.refresh()
        assert len(calls) == 1

    def test_the_ttl_expires(self, qapp, about):
        calls, _answer = about
        clock = Clock()
        svc = service(clock)
        svc.refresh()
        clock.advance(QUOTA_TTL_S + 1)
        svc.refresh()
        assert len(calls) == 2

    def test_a_forced_refresh_ignores_the_ttl(self, qapp, about):
        """A user who has just freed 40 GB must not be told for four more
        minutes that their drive is still full."""
        calls, _answer = about
        svc = service()
        svc.refresh()
        svc.refresh(force=True)
        assert len(calls) == 2

    def test_current_never_calls_out(self, qapp, about):
        calls, _answer = about
        svc = service()
        svc.current()
        svc.current()
        assert calls == []

    def test_an_unsampled_service_reports_zero_not_full(self, qapp, about):
        """`total == 0` means "we have not learned anything yet". Treating it as
        a full drive would paint the tray yellow before the first request."""
        svc = service()
        assert svc.current().total == 0
        assert svc.is_full() is False

    def test_no_endpoint_is_a_no_op(self, qapp, about):
        calls, _answer = about
        svc = QuotaService(ACCOUNT, endpoint=lambda: None)
        assert svc.refresh().total == 0
        assert calls == []

    def test_the_age_is_observable(self, qapp, about):
        clock = Clock()
        svc = service(clock)
        assert svc.age_s is None
        svc.refresh()
        clock.advance(30)
        assert svc.age_s == 30


class TestTiers:

    @pytest.mark.parametrize(("used", "tier"), [
        (10, "ok"), (850, "warn"), (950, "critical"), (1_000, "full"),
    ])
    def test_the_four_bands(self, qapp, about, used, tier):
        _calls, answer = about
        answer["quota"] = QuotaInfo(total=1_000, used=used, free=1_000 - used)
        svc = service()
        svc.refresh()
        assert svc.tier() == tier

    def test_pct_matches_the_ring(self, qapp, about):
        _calls, answer = about
        answer["quota"] = QuotaInfo(total=1_000, used=250, free=750)
        svc = service()
        svc.refresh()
        assert svc.pct() == 25.0


# ═════════════════════════════════════════════════════════════════════════════
# Token health, as a by-product
# ═════════════════════════════════════════════════════════════════════════════

class TestTokenHealth:

    def test_a_successful_call_proves_the_token(self, qapp, about):
        """The cheapest probe there is: a call that had to happen anyway."""
        svc = service()
        svc.refresh()
        assert svc.token() is TokenHealth.OK

    def test_an_expired_token_is_classified(self, qapp, about):
        _calls, answer = about
        answer["raise"] = RcError("operations/about", 401, {
            "error": "AADSTS700082: The refresh token has expired"})
        svc = service()
        svc.refresh()
        assert svc.token() is TokenHealth.EXPIRED

    def test_a_blocked_tenant_is_classified_separately(self, qapp, about):
        """Re-authenticating will not fix AADSTS65005, so offering a sign-in
        button would send the user round a loop that cannot terminate."""
        _calls, answer = about
        answer["raise"] = RcError("operations/about", 403, {
            "error": "AADSTS65005: the application is blocked"})
        svc = service()
        svc.refresh()
        assert svc.token() is TokenHealth.TENANT_BLOCKED

    def test_a_network_failure_says_nothing_about_the_token(self, qapp, about):
        """Showing "Sign in required" because the wifi dropped sends the user
        through an OAuth flow to fix a problem that was not there."""
        _calls, answer = about
        answer["raise"] = DaemonUnavailable(
            "operations/about", 0, {"error": "connection refused"})
        svc = service()
        svc.refresh()
        assert svc.token() is TokenHealth.UNKNOWN

    def test_a_failure_leaves_the_previous_quota_intact(self, qapp, about):
        """The last known number was true a moment ago; zeroing it would blank
        the storage ring on a hiccup."""
        _calls, answer = about
        svc = service()
        svc.refresh()
        answer["raise"] = DaemonUnavailable(
            "operations/about", 0, {"error": "connection refused"})
        svc.refresh(force=True)
        assert svc.current().total == ROOMY.total


# ═════════════════════════════════════════════════════════════════════════════
# Full and frozen
# ═════════════════════════════════════════════════════════════════════════════

class TestFullAndFrozen:

    def test_a_507_is_recognised(self, qapp, about):
        """rclone treats 507 as a FatalError and never retries it. It is also
        the only definitive statement that the drive is full, and it arrives
        before `about` catches up."""
        svc = service()
        assert svc.note_write_failure("HTTP 507 Insufficient Storage") is True

    def test_quota_limit_reached_is_recognised(self, qapp, about):
        svc = service()
        assert svc.note_write_failure("quotaLimitReached") is True

    def test_an_ordinary_failure_is_not_a_storage_failure(self, qapp, about):
        svc = service()
        assert svc.note_write_failure("connection reset by peer") is False

    def test_a_frozen_account_is_detected_from_the_refusal(self, qapp, about):
        """Microsoft freezes an account 30 days over quota: it goes read-only
        while `about` keeps answering normally, so the numbers cannot see it."""
        svc = service()
        svc.note_write_failure("accessDenied: account is over quota")
        assert svc.is_frozen() is True
        assert svc.current().frozen is True

    def test_space_appearing_thaws_it(self, qapp, about):
        _calls, answer = about
        clock = Clock()
        svc = service(clock)
        svc.note_write_failure("accessDenied: account is over quota")
        answer["quota"] = ROOMY
        svc.refresh(force=True)
        assert svc.is_frozen() is False

    def test_the_bus_carries_a_new_sample(self, qapp, about, bus_spy):
        bus_spy.watch("quota_updated")
        service().refresh()
        assert bus_spy.count("quota_updated") == 1
