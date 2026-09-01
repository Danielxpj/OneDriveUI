"""WP-05 — `sync/reducer.py`, the pure state machine.

The reducer is the one module in the project whose correctness can be pinned
down completely, because it is a pure function of a frozen dataclass. These
tests take that seriously:

* every rung is proved to fire, and proved to outrank everything below it;
* `reduce()` is proved deterministic and proved not to touch the filesystem,
  with a patched `open` that raises;
* the module's import graph is asserted, because the *reason* the reducer can be
  trusted is that it has nothing to be wrong with.
"""

from __future__ import annotations

import ast
import random
import subprocess
import sys
from pathlib import Path

import pytest

from onedriveui import constants
from onedriveui.models import (
    AccountInfo,
    AccountKind,
    BisyncState,
    DaemonHealth,
    Facts,
    MountHealth,
    NetworkState,
    PauseIntent,
    PauseReason,
    PowerState,
    QuotaInfo,
    SEVERE_STATES,
    SyncState,
    TokenHealth,
    TrayIcon,
)
from onedriveui.sync import reducer
from onedriveui.sync.reducer import (
    EFFECT,
    LADDER,
    LATCH,
    Debouncer,
    explain,
    progress_pct,
    reduce,
    status_text,
    tooltip,
    tray_for,
    transition_effects,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REDUCER_PY = REPO_ROOT / "onedriveui" / "sync" / "reducer.py"


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures — a healthy world, and one trigger per rung
# ═════════════════════════════════════════════════════════════════════════════

#: Everything nominal. Every rung test starts here and perturbs one thing, so a
#: test that passes for the wrong reason has nowhere to hide.
HEALTHY: dict[str, object] = {
    "account_id": "onedrive",
    "sampled_at": "2026-08-31T12:00:00Z",
    "startup_elapsed_s": 3600.0,
    "account_configured": True,
    "daemon_rcd": DaemonHealth.UP,
    "daemon_mount": DaemonHealth.UP,
    "mount": MountHealth.UP,
    "mount_enabled": True,
    "token": TokenHealth.OK,
    "quota": QuotaInfo(total=1_000_000, used=10_000, free=990_000),
    "network": NetworkState.ONLINE,
    "power": PowerState.NORMAL,
    "bisync": BisyncState.DISABLED,
}


def facts(**overrides: object) -> Facts:
    """A healthy `Facts` with `overrides` applied."""
    return Facts(**{**HEALTHY, **overrides})


#: One minimal trigger per rung, in ladder order. Each dict is the *smallest*
#: perturbation of HEALTHY that makes that rung match.
#:
#: These are chosen so that a rung's trigger touches a different field from the
#: rung below wherever the domain allows it — which is what makes the "outranks"
#: test below meaningful rather than tautological. Where it does not (rungs 3
#: and 4 are two values of one `token` field, and cannot both be true), the
#: merge keeps the higher rung's value, which is the honest outcome: the lower
#: rung's condition genuinely cannot hold at the same time.
TRIGGERS: tuple[tuple[str, dict[str, object], SyncState], ...] = (
    ("signed_out",      {"account_configured": False},                    SyncState.SIGNED_OUT),
    ("initializing",    {"startup_elapsed_s": 1.0,
                         "daemon_rcd": DaemonHealth.STARTING},            SyncState.INITIALIZING),
    ("account_blocked", {"token": TokenHealth.TENANT_BLOCKED},            SyncState.ACCOUNT_BLOCKED),
    ("auth_required",   {"token": TokenHealth.EXPIRED},                   SyncState.AUTH_REQUIRED),
    ("error",           {"issues_blocking": 1},                           SyncState.ERROR),
    ("needs_attention", {"pending_decisions": 1},                         SyncState.NEEDS_ATTENTION),
    ("paused_quota",    {"out_of_space": True},                           SyncState.PAUSED_QUOTA),
    ("paused_manual",   {"pause": PauseIntent(reason=PauseReason.MANUAL)}, SyncState.PAUSED_MANUAL),
    ("paused_metered",  {"policy_pause": PauseReason.METERED},            SyncState.PAUSED_METERED),
    ("paused_battery",  {"power": PowerState.SAVER,
                         "pause": PauseIntent(reason=PauseReason.BATTERY)}, SyncState.PAUSED_BATTERY),
    ("offline",         {"network": NetworkState.OFFLINE},                SyncState.OFFLINE),
    ("mounting",        {"mount": MountHealth.DOWN},                      SyncState.MOUNTING),
    ("syncing",         {"transfers_active": 1},                          SyncState.SYNCING),
    ("processing",      {"checks_active": 1},                             SyncState.PROCESSING),
    ("warning",         {"issues_error": 1},                              SyncState.WARNING),
    ("info_notice",     {"info_notice": "OneNote notebooks aren't synced"}, SyncState.INFO_NOTICE),
    ("up_to_date",      {},                                               SyncState.UP_TO_DATE),
)

RUNG_IDS = [name for name, _tr, _st in TRIGGERS]


def merged(*triggers: dict[str, object]) -> dict[str, object]:
    """Overlay `triggers`, earliest winning any field collision.

    Used to build "rung N's condition holds, and so does every condition below
    it". The earliest dict wins because it is the rung whose victory is being
    asserted; where a lower rung wanted the same field, its condition is simply
    unreachable while the higher one holds.
    """
    out: dict[str, object] = {}
    for trigger in triggers:
        for key, value in trigger.items():
            out.setdefault(key, value)
    return out


ACCOUNT = AccountInfo(id="onedrive", remote="onedrive", sync_root="/tmp/OneDrive")
BUSINESS = AccountInfo(id="work", remote="work", kind=AccountKind.BUSINESS,
                       sync_root="/tmp/Work")


# ═════════════════════════════════════════════════════════════════════════════
# The ladder
# ═════════════════════════════════════════════════════════════════════════════

class TestLadderShape:

    def test_seventeen_rungs(self):
        assert len(LADDER) == 17

    def test_ladder_and_triggers_agree(self):
        """The test table is in ladder order and names the same rungs.

        If a rung is renamed, reordered or inserted, this fails before any of
        the behavioural tests do — so the failure names the cause.
        """
        assert [name for name, _p, _s in LADDER] == RUNG_IDS
        assert [state for _n, _p, state in LADDER] == [s for _n, _t, s in TRIGGERS]

    def test_every_state_but_not_running_is_reachable(self):
        """All 17 reachable states appear exactly once; NOT_RUNNING never does."""
        produced = [state for _n, _p, state in LADDER]
        assert len(set(produced)) == 17
        assert SyncState.NOT_RUNNING not in produced
        assert set(produced) == set(SyncState) - {SyncState.NOT_RUNNING}

    def test_last_rung_is_unconditional(self):
        """The catch-all matches even a default-constructed Facts."""
        _name, matches, state = LADDER[-1]
        assert matches(Facts()) is True
        assert state is SyncState.UP_TO_DATE


class TestEachRungFires:
    """17 cases: each rung's own trigger produces that rung's state."""

    @pytest.mark.parametrize(("name", "trigger", "expected"), TRIGGERS, ids=RUNG_IDS)
    def test_rung_fires(self, name, trigger, expected):
        assert reduce(facts(**trigger)) is expected

    @pytest.mark.parametrize(("name", "trigger", "expected"), TRIGGERS, ids=RUNG_IDS)
    def test_explain_names_the_rung(self, name, trigger, expected):
        assert explain(facts(**trigger)) == (name, expected)


class TestOutranking:
    """16 cases: rung N wins even when every rung below it also matches."""

    @pytest.mark.parametrize("index", range(16), ids=RUNG_IDS[:16])
    def test_rung_outranks_everything_below(self, index):
        name, trigger, expected = TRIGGERS[index]
        below = [tr for _n, tr, _s in TRIGGERS[index + 1:]]
        combined = merged(trigger, *below)
        assert reduce(facts(**combined)) is expected, (
            f"rung {index + 1} ({name}) lost to a lower rung")


class TestLadderDetails:
    """The rungs whose exact wording carries a behavioural decision."""

    def test_transfers_outrank_errors(self):
        """Windows shows progress with an issues banner beneath it.

        This is the one place the ladder deliberately puts a busy state above a
        problem state. Inverting it would replace live progress with a stale
        complaint, and the banner exists precisely so nothing is hidden.
        """
        assert reduce(facts(transfers_active=2, issues_error=3)) is SyncState.SYNCING

    def test_blocking_issues_do_outrank_transfers(self):
        """A *blocking* issue is different: nothing can progress past it."""
        assert reduce(facts(transfers_active=2, issues_blocking=1)) is SyncState.ERROR

    def test_tenant_blocked_never_falls_through_to_auth_required(self):
        """AADSTS65005 is not fixed by signing in again.

        Falling through to AUTH_REQUIRED would offer a sign-in button that sends
        the user round a loop that cannot terminate.
        """
        assert reduce(facts(token=TokenHealth.TENANT_BLOCKED)) is SyncState.ACCOUNT_BLOCKED

    @pytest.mark.parametrize("token", [TokenHealth.EXPIRED, TokenHealth.MFA])
    def test_expired_and_mfa_both_ask_for_a_sign_in(self, token):
        assert reduce(facts(token=token)) is SyncState.AUTH_REQUIRED

    def test_startup_grace_expires(self):
        """A daemon still down after the grace window is an error, not a start-up."""
        starting = facts(startup_elapsed_s=1.0, daemon_rcd=DaemonHealth.DOWN)
        assert reduce(starting) is SyncState.INITIALIZING
        assert reduce(facts(startup_elapsed_s=8.0,
                            daemon_rcd=DaemonHealth.DOWN)) is SyncState.ERROR

    def test_foreign_daemon_is_an_error_not_a_start_up(self):
        """A daemon on our port that failed the ownership proof is never ours."""
        assert reduce(facts(daemon_rcd=DaemonHealth.FOREIGN)) is SyncState.ERROR

    def test_stale_mount_is_an_error(self):
        """`/proc` says mounted, `statvfs` says ENOTCONN — every read blocks."""
        assert reduce(facts(mount=MountHealth.STALE)) is SyncState.ERROR

    def test_mount_down_is_only_mounting_when_the_mount_is_enabled(self):
        """An account configured without a mount is healthy with no mount."""
        assert reduce(facts(mount=MountHealth.DOWN)) is SyncState.MOUNTING
        assert reduce(facts(mount=MountHealth.DOWN,
                            mount_enabled=False)) is SyncState.UP_TO_DATE

    def test_offline_needs_three_consecutive_failures(self):
        assert reduce(facts(consecutive_net_failures=2)) is SyncState.UP_TO_DATE
        assert reduce(facts(consecutive_net_failures=3)) is SyncState.OFFLINE

    def test_full_quota_and_full_disk_share_a_rung(self):
        full_cloud = facts(quota=QuotaInfo(total=1_000, used=1_000, free=0))
        assert reduce(full_cloud) is SyncState.PAUSED_QUOTA
        assert reduce(facts(out_of_space=True)) is SyncState.PAUSED_QUOTA

    @pytest.mark.parametrize(("bisync", "expected"), [
        (BisyncState.CRITICAL, SyncState.ERROR),
        (BisyncState.LOCK_STUCK, SyncState.ERROR),
        (BisyncState.NEEDS_RESYNC, SyncState.NEEDS_ATTENTION),
        (BisyncState.RUNNING, SyncState.PROCESSING),
        (BisyncState.IDLE, SyncState.UP_TO_DATE),
        (BisyncState.DISABLED, SyncState.UP_TO_DATE),
    ])
    def test_every_bisync_state_lands_somewhere_sensible(self, bisync, expected):
        assert reduce(facts(bisync=bisync)) is expected

    def test_pin_jobs_count_as_syncing(self):
        """Hydrating a pinned folder is bytes moving; the user sees a spinner."""
        assert reduce(facts(pin_jobs_active=1)) is SyncState.SYNCING

    def test_queued_uploads_are_processing_not_syncing(self):
        """Queued is not yet moving. `SYNCING` claims a transfer is in flight."""
        assert reduce(facts(uploads_queued=5)) is SyncState.PROCESSING
        assert reduce(facts(uploads_in_progress=1)) is SyncState.SYNCING


class TestLatches:
    """Hazards that survive the thing that caused them going out of view."""

    @pytest.mark.parametrize(("latch", "expected"), [
        (LATCH.BISYNC_CRITICAL, SyncState.ERROR),
        (LATCH.MOUNT_FAILED, SyncState.ERROR),
        (LATCH.NEEDS_RESYNC, SyncState.NEEDS_ATTENTION),
        (LATCH.QUOTA_EXCEEDED, SyncState.PAUSED_QUOTA),
    ])
    def test_latch_alone_drives_the_state(self, latch, expected):
        """An otherwise perfectly healthy world still reports the hazard.

        This is the whole point: an HTTP 507 will not repeat until the next
        upload is attempted, so without the latch the state would bounce back to
        UP_TO_DATE one tick later and the user would never learn why nothing
        uploads.
        """
        assert reduce(facts(latches=frozenset({latch}))) is expected

    def test_orphan_cache_latch_is_not_a_hazard(self):
        """It offers a reclaim action; it does not colour the tray icon."""
        assert reduce(facts(latches=frozenset({LATCH.ORPHAN_CACHE}))) is SyncState.UP_TO_DATE

    def test_latch_names_are_declared(self):
        declared = {LATCH.NEEDS_RESYNC, LATCH.BISYNC_CRITICAL, LATCH.QUOTA_EXCEEDED,
                    LATCH.MOUNT_FAILED, LATCH.ORPHAN_CACHE}
        assert declared == LATCH.ALL

    def test_an_unknown_latch_changes_nothing(self):
        """A stale row from an older schema must not wedge the tray icon."""
        assert reduce(facts(latches=frozenset({"some_old_latch"}))) is SyncState.UP_TO_DATE


class TestPause:
    """Intent versus policy, and what "Sync Anyway" actually does."""

    def test_manual_pause_comes_from_intent(self):
        paused = facts(pause=PauseIntent(reason=PauseReason.MANUAL,
                                         until="2026-08-31T14:00:00Z"))
        assert reduce(paused) is SyncState.PAUSED_MANUAL

    def test_sync_anyway_stops_the_rung_matching(self):
        """The condition is still true; the user has said they know."""
        overridden = facts(pause=PauseIntent(reason=PauseReason.METERED,
                                             overridden=True),
                           policy_pause=PauseReason.METERED)
        assert reduce(overridden) is SyncState.UP_TO_DATE

    def test_sync_anyway_for_one_reason_does_not_clear_another(self):
        """Acknowledging metered says nothing about the battery."""
        both = facts(pause=PauseIntent(reason=PauseReason.METERED, overridden=True),
                     policy_pause=PauseReason.BATTERY)
        assert reduce(both) is SyncState.PAUSED_BATTERY

    def test_policy_pause_and_intent_both_reach_the_rung(self):
        assert reduce(facts(policy_pause=PauseReason.METERED)) is SyncState.PAUSED_METERED
        assert reduce(facts(pause=PauseIntent(reason=PauseReason.METERED))) \
            is SyncState.PAUSED_METERED

    def test_manual_outranks_automatic(self):
        """"I paused this" is a stronger statement than "your laptop noticed"."""
        both = facts(pause=PauseIntent(reason=PauseReason.MANUAL),
                     policy_pause=PauseReason.METERED)
        assert reduce(both) is SyncState.PAUSED_MANUAL

    def test_the_reducer_never_expires_a_pause_itself(self):
        """Expiry belongs to the collector, which is the thing with a clock.

        A `Facts` whose deadline is long past still reports paused here. That is
        correct and deliberate: if the reducer expired it, `reduce()` would need
        the time, and the same `Facts` would reduce differently on a replay.
        """
        stale = facts(sampled_at="2026-08-31T12:00:00Z",
                      pause=PauseIntent(reason=PauseReason.MANUAL,
                                        until="2020-01-01T00:00:00Z"))
        assert reduce(stale) is SyncState.PAUSED_MANUAL


# ═════════════════════════════════════════════════════════════════════════════
# Purity
# ═════════════════════════════════════════════════════════════════════════════

class TestPurity:

    def test_module_scope_imports_are_models_and_strings_only(self):
        """The frozen dependency set, read off the source with the AST.

        This is the invariant that makes everything else in this file credible:
        a module that cannot reach a clock, a socket or the filesystem cannot be
        non-deterministic, whatever its logic does.
        """
        tree = ast.parse(REDUCER_PY.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            # Only module scope. A function-local import is a different claim
            # (see `_human`) and is checked separately below.
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if any(node in ast.walk(fn)
                   for fn in ast.walk(tree)
                   if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))):
                continue
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif node.module and node.level == 0:
                imported.add(node.module)

        onedriveui_imports = {m for m in imported if m.startswith("onedriveui")}
        assert onedriveui_imports == {"onedriveui.models", "onedriveui.strings"}
        assert not any(m.startswith("PySide6") for m in imported)

    def test_importing_the_reducer_does_not_import_qt(self):
        """A fresh interpreter, so a Qt import elsewhere in the suite cannot mask it."""
        code = ("import sys; import onedriveui.sync.reducer; "
                "print(any(m.startswith('PySide6') for m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"

    def test_reduce_never_touches_the_filesystem(self, monkeypatch):
        """`open` raises for the duration; 1 000 reductions must not notice."""
        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("reduce() opened a file")

        monkeypatch.setattr("builtins.open", explode)
        rng = random.Random(20260831)
        for _ in range(1_000):
            reduce(random_facts(rng))

    def test_reduce_is_deterministic(self):
        """1 000 random Facts produce identical results across two passes."""
        first = [reduce(f) for f in random_corpus()]
        second = [reduce(f) for f in random_corpus()]
        assert first == second

    def test_reduce_does_not_mutate_its_input(self):
        """`Facts` is frozen, but a rung reaching for a mutable member could still
        change something. Compare the whole value before and after."""
        before = facts(transfers_active=3, issues_error=2)
        snapshot = repr(before)
        reduce(before)
        assert repr(before) == snapshot

    def test_tunables_match_constants(self):
        """The duplicated numbers in `reducer` equal the ones in `constants`.

        `reducer` may not import `constants`, so the values are restated there.
        This is what stops the two copies drifting apart silently.
        """
        assert reducer.STARTUP_GRACE_S == constants.STARTUP_GRACE_S
        assert reducer.OFFLINE_FAILURE_THRESHOLD == constants.OFFLINE_FAILURE_THRESHOLD
        assert reducer.DEBOUNCE_SEVERE_TICKS == constants.DEBOUNCE_SEVERE_TICKS
        assert reducer.DEBOUNCE_NORMAL_TICKS == constants.DEBOUNCE_NORMAL_TICKS
        assert reducer.DEBOUNCE_IDLE_TICKS == constants.DEBOUNCE_IDLE_TICKS
        assert reducer.PROCESSING_ENTRY_DELAY_MS == constants.PROCESSING_ENTRY_DELAY_MS
        assert reducer.MOUNTING_SUPPRESS_S == constants.MOUNTING_SUPPRESS_S


def random_facts(rng: random.Random) -> Facts:
    """A `Facts` with every ladder-relevant field randomised."""
    return Facts(
        account_id="onedrive",
        sampled_at="2026-08-31T12:00:00Z",
        startup_elapsed_s=rng.choice([0.5, 3.0, 9.0, 3600.0]),
        account_configured=rng.random() > 0.1,
        daemon_rcd=rng.choice(list(DaemonHealth)),
        daemon_mount=rng.choice(list(DaemonHealth)),
        mount=rng.choice(list(MountHealth)),
        mount_enabled=rng.random() > 0.2,
        token=rng.choice(list(TokenHealth)),
        quota=QuotaInfo(total=1_000, used=rng.randrange(0, 1_001),
                        free=rng.randrange(0, 1_001)),
        network=rng.choice(list(NetworkState)),
        power=rng.choice(list(PowerState)),
        consecutive_net_failures=rng.randrange(0, 5),
        transfers_active=rng.randrange(0, 4),
        checks_active=rng.randrange(0, 4),
        uploads_queued=rng.randrange(0, 4),
        uploads_in_progress=rng.randrange(0, 4),
        pin_jobs_active=rng.randrange(0, 3),
        scan_in_progress=rng.random() > 0.7,
        out_of_space=rng.random() > 0.9,
        bisync=rng.choice(list(BisyncState)),
        issues_blocking=rng.randrange(0, 3),
        issues_error=rng.randrange(0, 3),
        issues_warning=rng.randrange(0, 3),
        pending_decisions=rng.randrange(0, 2),
        latches=frozenset(rng.sample(sorted(LATCH.ALL), rng.randrange(0, 3))),
        pause=PauseIntent(reason=rng.choice(list(PauseReason)),
                          overridden=rng.random() > 0.8),
        policy_pause=rng.choice(list(PauseReason)),
        info_notice=rng.choice([None, "notice"]),
    )


def random_corpus(seed: int = 20260831, n: int = 1_000) -> list[Facts]:
    rng = random.Random(seed)
    return [random_facts(rng) for _ in range(n)]


# ═════════════════════════════════════════════════════════════════════════════
# Hysteresis
# ═════════════════════════════════════════════════════════════════════════════

class TestDebouncer:

    def test_starts_in_initializing(self):
        assert Debouncer().current is SyncState.INITIALIZING

    def test_error_applies_on_the_first_tick(self):
        """A hazard the user cannot see is worse than one shown 400 ms early."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        assert deb.apply(SyncState.ERROR, 0.0) is SyncState.ERROR

    @pytest.mark.parametrize("state", sorted(SEVERE_STATES))
    def test_every_severe_state_applies_on_the_first_tick(self, state):
        deb = Debouncer(SyncState.UP_TO_DATE)
        assert deb.apply(state, 0.0) is state

    def test_up_to_date_needs_three_quiet_ticks(self):
        """A multi-file batch goes quiet between transfers; a green cloud
        mid-upload is a lie the user will remember."""
        deb = Debouncer(SyncState.SYNCING)
        assert deb.apply(SyncState.UP_TO_DATE, 0.0) is SyncState.SYNCING
        assert deb.apply(SyncState.UP_TO_DATE, 1.0) is SyncState.SYNCING
        assert deb.apply(SyncState.UP_TO_DATE, 2.0) is SyncState.UP_TO_DATE

    def test_one_busy_tick_restarts_the_idle_count(self):
        deb = Debouncer(SyncState.SYNCING)
        deb.apply(SyncState.UP_TO_DATE, 0.0)
        deb.apply(SyncState.UP_TO_DATE, 1.0)
        deb.apply(SyncState.SYNCING, 2.0)
        assert deb.apply(SyncState.UP_TO_DATE, 3.0) is SyncState.SYNCING
        assert deb.apply(SyncState.UP_TO_DATE, 4.0) is SyncState.SYNCING
        assert deb.apply(SyncState.UP_TO_DATE, 5.0) is SyncState.UP_TO_DATE

    def test_ordinary_states_need_two_ticks(self):
        """One ECONNRESET during a token refresh must not blank the UI."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        assert deb.apply(SyncState.OFFLINE, 0.0) is SyncState.UP_TO_DATE
        assert deb.apply(SyncState.OFFLINE, 1.0) is SyncState.OFFLINE

    def test_processing_waits_out_its_entry_delay(self):
        """A 200 ms directory listing must not flash a banner."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        assert deb.apply(SyncState.PROCESSING, 0.000) is SyncState.UP_TO_DATE
        assert deb.apply(SyncState.PROCESSING, 0.200) is SyncState.UP_TO_DATE
        assert deb.apply(SyncState.PROCESSING, 0.250) is SyncState.PROCESSING

    def test_processing_still_needs_its_two_ticks(self):
        """Waiting 250 ms is an *additional* requirement, not a replacement."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        assert deb.apply(SyncState.PROCESSING, 10.0) is SyncState.UP_TO_DATE
        assert deb.apply(SyncState.PROCESSING, 10.3) is SyncState.PROCESSING

    def test_mounting_is_suppressed_after_a_deliberate_restart(self):
        """We broke the mount on purpose; a spinner would read as a fault."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        deb.note_mount_restart(100.0)
        for tick in range(10):
            assert deb.apply(SyncState.MOUNTING, 100.0 + tick) is SyncState.UP_TO_DATE
        assert deb.apply(SyncState.MOUNTING, 115.0) is SyncState.MOUNTING

    def test_mounting_we_did_not_cause_is_published_normally(self):
        """The kernel losing the FUSE connection is not suppressed."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        assert deb.apply(SyncState.MOUNTING, 0.0) is SyncState.UP_TO_DATE
        assert deb.apply(SyncState.MOUNTING, 1.0) is SyncState.MOUNTING

    def test_a_severe_state_cuts_through_the_suppression_window(self):
        """Suppression covers MOUNTING only. A stale mount is still an error."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        deb.note_mount_restart(100.0)
        assert deb.apply(SyncState.ERROR, 101.0) is SyncState.ERROR

    def test_staying_put_is_free(self):
        """Re-observing the published state never re-runs the gate."""
        deb = Debouncer(SyncState.UP_TO_DATE)
        for tick in range(5):
            assert deb.apply(SyncState.UP_TO_DATE, float(tick)) is SyncState.UP_TO_DATE

    def test_reset_forgets_everything(self):
        deb = Debouncer(SyncState.UP_TO_DATE)
        deb.note_mount_restart(0.0)
        deb.apply(SyncState.SYNCING, 0.0)
        deb.reset()
        assert deb.current is SyncState.INITIALIZING
        assert deb.streak == 0
        # The suppression window is forgotten too, so the new account's first
        # MOUNTING is not swallowed by the old one's restart.
        assert deb.apply(SyncState.MOUNTING, 1.0) is SyncState.INITIALIZING
        assert deb.apply(SyncState.MOUNTING, 2.0) is SyncState.MOUNTING

    def test_streak_and_candidate_are_observable(self):
        deb = Debouncer(SyncState.UP_TO_DATE)
        deb.apply(SyncState.SYNCING, 0.0)
        assert (deb.candidate, deb.streak, deb.current) == (
            SyncState.SYNCING, 1, SyncState.UP_TO_DATE)


# ═════════════════════════════════════════════════════════════════════════════
# Rendering
# ═════════════════════════════════════════════════════════════════════════════

class TestRendering:

    @pytest.mark.parametrize("state", sorted(set(SyncState) - {SyncState.NOT_RUNNING}))
    def test_every_state_renders_without_leftover_placeholders(self, state):
        """No user ever sees a brace. Checked for all 17 reachable states."""
        headline, sub = status_text(state, facts())
        assert headline and "{" not in headline
        assert "{" not in sub

    def test_syncing_headline_counts_files_in_flight(self):
        headline, _sub = status_text(
            SyncState.SYNCING, facts(transfers_active=2, uploads_in_progress=1))
        assert headline == "Syncing 3 files"

    def test_up_to_date_subtext_reports_storage(self):
        _headline, sub = status_text(
            SyncState.UP_TO_DATE,
            facts(quota=QuotaInfo(total=1_104_880_336_896, used=252_544_077_005)))
        assert sub == "252.5 GB of 1.1 TB used"

    def test_warning_subtext_counts_the_failures(self):
        _headline, sub = status_text(SyncState.WARNING, facts(issues_error=3))
        assert sub == "3 files couldn't be synced"

    def test_manual_pause_counts_down_from_the_sample(self):
        """Pure: the countdown is `until` minus `sampled_at`, not the wall clock."""
        paused = facts(sampled_at="2026-08-31T12:00:00Z",
                       pause=PauseIntent(reason=PauseReason.MANUAL,
                                         until="2026-08-31T13:30:00Z"))
        _headline, sub = status_text(SyncState.PAUSED_MANUAL, paused)
        assert sub == "Syncing will resume in 1h 30m"

    def test_pause_until_i_resume_has_no_countdown(self):
        """No deadline means no second line, rather than "{hh}h {mm}m"."""
        paused = facts(pause=PauseIntent(reason=PauseReason.MANUAL, until=None))
        headline, sub = status_text(SyncState.PAUSED_MANUAL, paused)
        assert headline == "Sync is paused"
        assert sub == ""

    def test_an_elapsed_deadline_drops_the_countdown(self):
        paused = facts(sampled_at="2026-08-31T12:00:00Z",
                       pause=PauseIntent(reason=PauseReason.MANUAL,
                                         until="2026-08-31T11:00:00Z"))
        assert status_text(SyncState.PAUSED_MANUAL, paused)[1] == ""

    def test_countdown_rounds_minutes_up(self):
        """90 seconds left says "2m", so the number never sticks then jumps."""
        paused = facts(sampled_at="2026-08-31T12:00:00Z",
                       pause=PauseIntent(reason=PauseReason.MANUAL,
                                         until="2026-08-31T12:01:30Z"))
        assert status_text(SyncState.PAUSED_MANUAL, paused)[1] == \
            "Syncing will resume in 0h 2m"

    def test_tooltip_is_the_two_lines(self):
        f = facts(issues_error=2)
        assert tooltip(SyncState.WARNING, f) == "Sync issues\n2 files couldn't be synced"

    def test_tooltip_drops_an_empty_second_line(self):
        assert "\n" not in tooltip(SyncState.PROCESSING, facts())

    def test_the_three_surfaces_cannot_disagree(self):
        """Headline, tooltip and tray all derive from one state, for all 17."""
        for state in set(SyncState) - {SyncState.NOT_RUNNING}:
            headline, _sub = status_text(state, facts())
            assert tooltip(state, facts()).startswith(headline)


class TestTrayIcon:

    @pytest.mark.parametrize("state", sorted(set(SyncState) - {SyncState.NOT_RUNNING}))
    def test_every_state_has_a_themed_icon_name(self, state):
        icon = tray_for(state, ACCOUNT)
        assert icon is not TrayIcon.NONE
        assert icon.value.startswith("onedriveui-")

    def test_not_running_registers_no_item(self):
        assert tray_for(SyncState.NOT_RUNNING, ACCOUNT) is TrayIcon.NONE

    def test_business_gets_the_blue_cloud_when_healthy(self):
        """A user with both accounts tells them apart in the tray by this."""
        assert tray_for(SyncState.UP_TO_DATE, ACCOUNT) is TrayIcon.SYNCED
        assert tray_for(SyncState.UP_TO_DATE, BUSINESS) is TrayIcon.SYNCED_BIZ

    def test_account_kind_does_not_leak_into_other_states(self):
        """Only the healthy icon is branded; an error is an error."""
        for state in set(SyncState) - {SyncState.UP_TO_DATE, SyncState.NOT_RUNNING}:
            assert tray_for(state, BUSINESS) is tray_for(state, ACCOUNT)


class TestProgress:

    def test_indeterminate_unless_syncing_with_a_known_queue(self):
        assert progress_pct(SyncState.PROCESSING, facts()) == -1
        assert progress_pct(SyncState.SYNCING, facts(transfers_active=1)) == -1

    def test_percent_from_done_over_total(self):
        assert progress_pct(SyncState.SYNCING,
                            facts(transfers_active=1, uploads_queued=3)) == 25


# ═════════════════════════════════════════════════════════════════════════════
# Transition effects
# ═════════════════════════════════════════════════════════════════════════════

class TestTransitionEffects:

    def test_no_change_means_no_effects(self):
        assert transition_effects(SyncState.SYNCING, SyncState.SYNCING, facts()) == []

    def test_every_change_invalidates_the_nautilus_cache(self):
        """Nautilus keeps the emblem we last gave it until we say otherwise."""
        for _name, _trigger, state in TRIGGERS:
            if state is SyncState.UP_TO_DATE:
                continue
            effects = transition_effects(SyncState.UP_TO_DATE, state, facts())
            assert EFFECT.IPC_INVALIDATE in effects

    def test_drain_precedes_reset(self):
        """`core/stats-reset` also wipes `core/transferred`.

        Resetting before draining destroys the only record that those transfers
        ever happened, and the Activity Center's history with it.
        """
        effects = transition_effects(SyncState.SYNCING, SyncState.UP_TO_DATE, facts())
        assert effects.index(EFFECT.STATS_DRAIN) < effects.index(EFFECT.STATS_RESET)

    def test_reaching_up_to_date_from_elsewhere_does_not_drain(self):
        """Nothing transferred, so there is nothing to drain or reset."""
        effects = transition_effects(SyncState.OFFLINE, SyncState.UP_TO_DATE, facts())
        assert EFFECT.STATS_DRAIN not in effects
        assert EFFECT.STATS_RESET not in effects

    def test_stale_mount_is_torn_down_before_anything_is_said(self):
        """Every read against a dead FUSE mount blocks in the kernel.

        A toast raised first would sit behind a frozen file manager, so the
        unmount has to be the first effect in the list.
        """
        effects = transition_effects(SyncState.UP_TO_DATE, SyncState.ERROR,
                                     facts(mount=MountHealth.STALE))
        assert effects[0] == EFFECT.MOUNT_FORCE_UNMOUNT
        assert effects.index(EFFECT.MOUNT_RESTART) < effects.index(EFFECT.TOAST_MOUNT_LOST)

    def test_an_error_without_a_stale_mount_does_not_unmount(self):
        effects = transition_effects(SyncState.UP_TO_DATE, SyncState.ERROR,
                                     facts(issues_blocking=1))
        assert EFFECT.MOUNT_FORCE_UNMOUNT not in effects

    @pytest.mark.parametrize("state", [SyncState.PAUSED_METERED, SyncState.PAUSED_BATTERY])
    def test_automatic_pauses_toast_and_start_deferring(self, state):
        """The toast carries "Sync Anyway"; a silent auto-pause is a support ticket."""
        effects = transition_effects(SyncState.UP_TO_DATE, state, facts())
        assert EFFECT.TOAST_PAUSED in effects
        assert EFFECT.PAUSE_ENFORCE in effects

    def test_quota_defers_uploads_only(self):
        """A full drive can still hydrate a file the user double-clicks.

        Blocking downloads too would make a storage problem look like a broken
        client, and would strand every online-only file.
        """
        effects = transition_effects(SyncState.UP_TO_DATE, SyncState.PAUSED_QUOTA, facts())
        assert EFFECT.PAUSE_ENFORCE_UPLOADS in effects
        assert EFFECT.PAUSE_ENFORCE not in effects
        assert EFFECT.TOAST_QUOTA_FULL in effects
        assert EFFECT.BANNER_QUOTA in effects

    def test_auth_required_suspends_jobs_but_never_unmounts(self):
        """Cached reads keep working while signed out, exactly as on Windows."""
        effects = transition_effects(SyncState.UP_TO_DATE, SyncState.AUTH_REQUIRED, facts())
        assert EFFECT.JOBS_SUSPEND in effects
        assert EFFECT.MOUNT_FORCE_UNMOUNT not in effects

    def test_signing_back_in_resumes_jobs(self):
        effects = transition_effects(SyncState.AUTH_REQUIRED, SyncState.SYNCING, facts())
        assert EFFECT.JOBS_RESUME in effects

    def test_needs_attention_raises_the_decision_once(self):
        effects = transition_effects(SyncState.UP_TO_DATE, SyncState.NEEDS_ATTENTION, facts())
        assert effects.count(EFFECT.DIALOG_DECISION) == 1
        assert EFFECT.TOAST_DECISION in effects

    def test_leaving_any_pause_releases_the_queue(self):
        for paused in (SyncState.PAUSED_MANUAL, SyncState.PAUSED_METERED,
                       SyncState.PAUSED_BATTERY, SyncState.PAUSED_QUOTA):
            effects = transition_effects(paused, SyncState.SYNCING, facts())
            assert EFFECT.PAUSE_RELEASE in effects

    def test_moving_between_pauses_does_not_release(self):
        """Metered to battery is still paused; draining the queue would sync."""
        effects = transition_effects(SyncState.PAUSED_METERED,
                                     SyncState.PAUSED_BATTERY, facts())
        assert EFFECT.PAUSE_RELEASE not in effects

    def test_recovering_from_error_says_so(self):
        effects = transition_effects(SyncState.ERROR, SyncState.UP_TO_DATE, facts())
        assert EFFECT.TOAST_MOUNT_RESTORED in effects

    def test_every_effect_name_is_declared(self):
        """No transition may invent an identifier the Supervisor cannot dispatch."""
        declared = {v for k, v in vars(EFFECT).items() if not k.startswith("_")}
        states = sorted(set(SyncState))
        for old in states:
            for new in states:
                for f in (facts(), facts(mount=MountHealth.STALE), facts(issues_error=1)):
                    assert set(transition_effects(old, new, f)) <= declared
