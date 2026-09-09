"""Trigger tests.

BUILD_BRIEF.md section 6 asks for these specifically, "especially that the same
trigger rule applies to every token".

That property is what the calibration design rests on. Every token has to enter the
dataset at the same point in its life, or the model is fitted on a comparison
between winners at their peak and losers at launch -- the exact failure prompts/
score.md step 5 warns produces "a model that reliably identifies what is currently
being pumped, which is also what is currently being sold into".

So the tests below try to break that property four ways: behaviourally (does the
decision change when irrelevant fields change), structurally (can the rule even see
anything else), at the boundaries (is the threshold applied identically), and over
time (does a token get a second chance at a nicer moment).
"""

from __future__ import annotations

import ast
import builtins
import inspect
import itertools
import textwrap

import pytest

from collectors.metrics import Observation, TokenMetrics
from collectors.store import Store
from collectors.trigger_watcher import (
    TRIGGER_HOLDER_COUNT,
    TRIGGER_HOLDERS,
    TRIGGER_MCAP,
    TRIGGER_MCAP_USD,
    TriggerWatcher,
    evaluate,
    evaluate_metrics,
    evaluate_observation,
)

BASE_MS = 1788912000000  # 2026-09-09T00:00:00Z


def metrics(**overrides) -> TokenMetrics:
    """A token record with every field defaulted, so a test can vary exactly one."""
    defaults = dict(
        chain="solana",
        contract="Tok1111111111111111111111111111111111111111",
        observed_at_ms=BASE_MS,
        source="test",
        ticker="$TEST",
        first_seen_at_ms=BASE_MS - 3_600_000,
        mcap_usd=100_000.0,
        holder_count=100,
    )
    defaults.update(overrides)
    return TokenMetrics(**defaults)


# ---------------------------------------------------------------------------
# The rule is the same for every token
# ---------------------------------------------------------------------------


class TestSameRuleForEveryToken:
    def test_rule_takes_only_the_two_trigger_inputs(self):
        """The signature is the guarantee: it cannot favour a token it cannot see."""
        params = list(inspect.signature(evaluate).parameters)
        assert params == ["mcap_usd", "holder_count"]

    def test_rule_reads_no_module_state_beyond_its_own_thresholds(self):
        """No hidden inputs -- no allowlist, no per-chain table, no clock.

        Parses the function and checks every free name it loads. A future edit that
        reaches for a lookup table, an environment variable, or the time of day
        fails here rather than silently skewing the cohort.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(evaluate)))
        func = tree.body[0]
        bound = {arg.arg for arg in func.args.args}
        bound |= {
            node.id
            for node in ast.walk(func)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        loaded = {
            node.id
            for node in ast.walk(func)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        # Builtins are not state; `float` and `int` appear only in the annotations.
        loaded -= set(dir(builtins))
        assert loaded - bound <= {
            "_crossed",
            "TriggerDecision",
            "TRIGGER_MCAP_USD",
            "TRIGGER_HOLDER_COUNT",
            "TRIGGER_MCAP",
            "TRIGGER_HOLDERS",
        }

    @pytest.mark.parametrize(
        ("mcap", "holders"),
        list(
            itertools.product(
                [None, 0.0, 249_999.99, 250_000.0, 9e9],
                [None, 0, 499, 500, 90_000],
            )
        ),
    )
    def test_identical_inputs_give_identical_decisions_whatever_the_token(self, mcap, holders):
        """Two tokens agreeing on the two numbers and on nothing else decide alike.

        The decoy differs in chain, address, ticker, age, liquidity, volume, price,
        deployer, authorities and declared socials -- including the socials triple,
        which is the strongest known predictor in the schema and therefore the most
        tempting thing to quietly let influence entry.
        """
        plain = metrics(mcap_usd=mcap, holder_count=holders)
        decoy = metrics(
            chain="bnb",
            contract="Zzz9999999999999999999999999999999999999999",
            ticker="$FAMOUS",
            observed_at_ms=BASE_MS + 86_400_000,
            first_seen_at_ms=BASE_MS - 999_000_000,
            mcap_usd=mcap,
            holder_count=holders,
            liquidity_usd=5_000_000.0,
            volume_24h_usd=99_000_000.0,
            price_usd=1.23,
            deployer_address="KnownGoodDep1oyer",
            deployer_prior_rugs=0,
            mint_revoked=True,
            freeze_active=False,
            declared_telegram=True,
            declared_x=True,
            declared_website=True,
            listings=["dex", "aggregator", "cex_perp", "cex_spot"],
        )
        assert evaluate_metrics(plain) == evaluate_metrics(decoy)

    def test_evaluate_is_deterministic(self):
        first = evaluate(250_000.0, 500)
        for _ in range(50):
            assert evaluate(250_000.0, 500) == first

    def test_no_caller_can_bend_a_threshold(self):
        """Thresholds are module constants, not arguments.

        Changing one has to be a commit, not a flag someone passes on a Tuesday for
        one promising token.
        """
        for fn in (evaluate, evaluate_observation, evaluate_metrics):
            assert not [p for p in inspect.signature(fn).parameters if "threshold" in p]
        watcher_params = set(inspect.signature(TriggerWatcher.__init__).parameters)
        assert not {"threshold", "mcap_threshold", "holders_threshold"} & watcher_params


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_constants_match_the_brief(self):
        assert TRIGGER_MCAP_USD == 250_000.0
        assert TRIGGER_HOLDER_COUNT == 500
        # The two members of the section 4 `trigger` enum, spelled as the schema does.
        assert TRIGGER_MCAP == "mcap_250k"
        assert TRIGGER_HOLDERS == "holders_500"

    @pytest.mark.parametrize(
        ("mcap", "expected"),
        [
            (249_999.99, False),
            (250_000.0, True),  # the threshold itself crosses
            (250_000.01, True),
            (0.0, False),
            (-1.0, False),
        ],
    )
    def test_mcap_boundary(self, mcap, expected):
        assert evaluate(mcap, None).fired is expected

    @pytest.mark.parametrize(
        ("holders", "expected"),
        [(499, False), (500, True), (501, True), (0, False)],
    )
    def test_holder_boundary(self, holders, expected):
        assert evaluate(None, holders).fired is expected


# ---------------------------------------------------------------------------
# Which trigger fired
# ---------------------------------------------------------------------------


class TestWhicheverFirst:
    def test_mcap_only(self):
        decision = evaluate(300_000.0, 12)
        assert (decision.trigger, decision.mcap_crossed, decision.holders_crossed) == (
            TRIGGER_MCAP,
            True,
            False,
        )

    def test_holders_only(self):
        decision = evaluate(1_000.0, 640)
        assert (decision.trigger, decision.mcap_crossed, decision.holders_crossed) == (
            TRIGGER_HOLDERS,
            False,
            True,
        )

    def test_both_crossed_in_one_observation_keeps_both_flags(self):
        """"Whichever first" is unanswerable when one poll shows both already met.

        The tie-break is fixed and not per-token, and the second flag is recorded
        rather than discarded, so the row still says what was true.
        """
        decision = evaluate(400_000.0, 900)
        assert decision.trigger == TRIGGER_MCAP
        assert decision.mcap_crossed and decision.holders_crossed
        assert decision.both_crossed

    def test_neither(self):
        decision = evaluate(1_000.0, 3)
        assert decision.fired is False
        assert decision.trigger is None


# ---------------------------------------------------------------------------
# Missing data never triggers (hard rule 3)
# ---------------------------------------------------------------------------


class TestMissingDataNeverTriggers:
    def test_both_missing(self):
        assert evaluate(None, None).fired is False

    def test_missing_mcap_does_not_block_a_holders_trigger(self):
        decision = evaluate(None, 700)
        assert decision.trigger == TRIGGER_HOLDERS
        assert decision.mcap_crossed is False

    def test_missing_never_becomes_zero_on_the_row(self):
        """Missing and zero both fail to cross, but they are not the same row.

        The trigger treats them alike -- neither is a crossing -- while the snapshot
        keeps them apart: ``None`` means nobody measured, ``0.0`` means someone
        measured zero. Collapsing the two is the imputation hard rule 3 forbids, and
        it would be invisible in the dataset afterwards.
        """
        assert evaluate(None, None).fired is False
        assert evaluate(0.0, 0).fired is False

        with Store() as store:
            watcher = TriggerWatcher(store, source="test")
            unknown = watcher.offer(
                metrics(contract="Unknown1", mcap_usd=None, holder_count=640)
            )
            measured = watcher.offer(
                metrics(contract="Measured1", mcap_usd=0.0, holder_count=640)
            )
            assert unknown is not None and measured is not None
            assert unknown.market.mcap_usd is None
            assert measured.market.mcap_usd == 0.0
            # The completeness modifier is how a model learns to tell them apart.
            assert unknown.completeness()[0] < measured.completeness()[0]

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_broken_numbers_are_missing_not_enormous(self, bad):
        """An infinite mcap is a broken response, not a token worth $inf."""
        assert evaluate(bad, None).fired is False

    def test_nan_holder_count_does_not_trigger(self):
        assert evaluate(None, float("nan")).fired is False  # type: ignore[arg-type]

    def test_observation_carries_only_the_two_inputs(self):
        obs = metrics(mcap_usd=float("nan"), holder_count=501).observation()
        assert obs.mcap_usd is None  # NaN normalised on the way in
        assert set(Observation.__dataclass_fields__) == {
            "chain",
            "contract",
            "observed_at_ms",
            "mcap_usd",
            "holder_count",
        }


# ---------------------------------------------------------------------------
# One snapshot per token, at first crossing
# ---------------------------------------------------------------------------


class TestFiresExactlyOnce:
    def test_a_token_below_the_trigger_is_not_snapshotted(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="test")
            assert watcher.offer(metrics(mcap_usd=100_000.0, holder_count=10)) is None
            assert store.snapshot_count() == 0

    def test_fires_on_the_poll_where_it_first_crosses_not_before(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="test")
            assert watcher.offer(metrics(mcap_usd=180_000.0, holder_count=200)) is None
            snapshot = watcher.offer(metrics(mcap_usd=260_000.0, holder_count=300))
            assert snapshot is not None
            assert snapshot.trigger == TRIGGER_MCAP
            # The row records the crossing observation, not the earlier quiet one.
            assert snapshot.market.mcap_usd == 260_000.0

    def test_never_fires_twice_however_far_it_runs(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="test")
            first = watcher.offer(metrics(mcap_usd=260_000.0, holder_count=300))
            assert first is not None
            for mcap in (300_000.0, 1_000_000.0, 50_000.0, 9_000_000.0):
                assert watcher.offer(metrics(mcap_usd=mcap, holder_count=5_000)) is None
            assert store.snapshot_count() == 1
            assert watcher.stats.fired == 1
            assert watcher.stats.already_triggered == 4

    def test_a_restarted_watcher_does_not_re_fire(self):
        """Restart safety without a state file: the append-only table is the state."""
        with Store() as store:
            TriggerWatcher(store, source="test").offer(metrics(mcap_usd=260_000.0))
            fresh = TriggerWatcher(store, source="test")  # cold cache, same database
            assert fresh.offer(metrics(mcap_usd=999_000.0)) is None
            assert store.snapshot_count() == 1

    def test_two_watchers_racing_produce_one_row(self, monkeypatch):
        """The database has the last word, so a lost race is not a lost dataset.

        Simulates the real window: B checks, sees nothing, and A commits before B's
        insert lands. B must lose quietly and leave A's row alone -- not overwrite
        it, and not crash the cycle and lose every other token in the batch.
        """
        with Store() as store:
            a = TriggerWatcher(store, source="a")
            b = TriggerWatcher(store, source="b")
            token = metrics(mcap_usd=260_000.0)

            assert a.offer(token) is not None
            written = store.fetch_by_contract(token.chain, token.contract)

            monkeypatch.setattr(store, "has_triggered", lambda *_: False)
            assert b.offer(token) is None

            assert store.snapshot_count() == 1
            assert b.stats.races_lost == 1
            monkeypatch.undo()
            assert store.fetch_by_contract(token.chain, token.contract) == written

    def test_separate_tokens_each_get_their_own_row(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="test")
            for i in range(5):
                watcher.offer(metrics(contract=f"Tok{i}", mcap_usd=260_000.0))
            assert store.snapshot_count() == 5

    def test_same_address_on_two_chains_is_two_tokens(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="test")
            assert watcher.offer(metrics(chain="solana", mcap_usd=260_000.0)) is not None
            assert watcher.offer(metrics(chain="bnb", mcap_usd=260_000.0)) is not None
            assert store.snapshot_count() == 2

    def test_dry_run_writes_nothing(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="test", dry_run=True)
            assert watcher.offer(metrics(mcap_usd=260_000.0)) is not None
            assert store.snapshot_count() == 0


# ---------------------------------------------------------------------------
# The watcher loop
# ---------------------------------------------------------------------------


class TestWatcherLoop:
    def test_a_failing_poll_delays_a_snapshot_but_does_not_skip_one(self):
        class FlakyFeed:
            source_name = "flaky"

            def __init__(self):
                self.calls = 0

            def poll(self):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("source down")
                return [metrics(mcap_usd=260_000.0)]

        with Store() as store:
            watcher = TriggerWatcher(store, source="test")
            watcher.run(FlakyFeed(), poll_seconds=0, max_cycles=2)
            assert store.snapshot_count() == 1

    def test_regime_is_recorded_per_batch_not_inferred_per_token(self):
        with Store() as store:
            watcher = TriggerWatcher(store, source="test", regime="cold")
            snapshot = watcher.offer(metrics(mcap_usd=260_000.0))
            assert snapshot is not None
            assert snapshot.regime == "cold"


# ---------------------------------------------------------------------------
# End to end, on the recorded replay fixture
# ---------------------------------------------------------------------------


class TestReplayEndToEnd:
    @staticmethod
    def _run():
        from pathlib import Path

        from collectors.bitquery import ReplayFeed

        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "solana_replay.json"
        feed = ReplayFeed.from_path(fixture)
        store = Store()
        watcher = TriggerWatcher(store, source=feed.source_name, regime="neutral")
        watcher.run(feed, poll_seconds=0, max_cycles=3)
        return store, watcher

    def test_expected_tokens_fire_and_nothing_else_does(self):
        store, watcher = self._run()
        with store:
            assert watcher.stats.fired == 6
            assert store.snapshot_count() == 6
            fired = store.triggered_contracts("solana")
            assert not any(c.startswith("NULL7") for c in fired)

    def test_trigger_breakdown_matches_the_fixture(self):
        store, _ = self._run()
        with store:
            assert store.trigger_breakdown() == {"mcap_250k": 4, "holders_500": 2}

    def test_the_token_that_crosses_late_is_captured_at_its_crossing(self):
        store, _ = self._run()
        with store:
            row = store.fetch_by_contract(
                "solana", "LATE3xqvT1qkNfPjCtq8xJ2mWq6rZ9dHbKcVnA4uYpS"
            )
            assert row is not None
            assert row["trigger_kind"] == "mcap_250k"
            assert row["market_mcap_usd"] == 402_000.0  # poll 2, not poll 1's 180k
            assert row["age_at_trigger_minutes"] == 120

    def test_a_token_with_no_mcap_still_enters_on_holders(self):
        store, _ = self._run()
        with store:
            row = store.fetch_by_contract(
                "solana", "HOLD2zYxWvUtSrQpOnMlKjIhGfEdCbA9876543210zz"
            )
            assert row is not None
            assert row["trigger_kind"] == "holders_500"
            assert row["market_mcap_usd"] is None  # unknown stays unknown
            assert row["trigger_mcap_crossed"] is False
            assert row["holders_count"] == 512

    def test_the_one_cent_below_token_waits_for_its_holders(self):
        store, _ = self._run()
        with store:
            row = store.fetch_by_contract(
                "solana", "EDGE4ppppQQQQrrrrSSSStttt1111UUUUvvvvWWWWxx"
            )
            assert row is not None
            # 249_999.99 never crossed; the 500th holder in poll 3 did.
            assert row["trigger_kind"] == "holders_500"
            assert row["holders_count"] == 500
