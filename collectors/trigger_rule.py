"""The trigger rule, on its own, with nothing else in the module.

Split out of ``collectors/trigger_watcher.py`` so the rule can be imported by code
that must not drag in DuckDB -- the Vercel function in ``api/`` runs on stdlib
only. ``trigger_watcher`` re-exports every name below, so
``from collectors.trigger_watcher import evaluate`` still returns *this* function
object, and ``collectors/backfill.py`` still shares it. The point of the split is
that there is one rule; a second copy for the serverless path would be exactly the
drift the identity test exists to catch.

BUILD_BRIEF.md section 3:

    Subscribe to new pools on target chains. Fire a snapshot the moment a token
    first crosses the trigger: *either* $250k mcap *or* 500 holders, whichever
    first. Record which trigger fired. Same rule for every token, no exceptions.

"Same rule for every token, no exceptions" is the load-bearing sentence, because
the whole calibration design rests on a lifecycle-matched sample: every token
captured at the same point in its life. A per-token exception, a manual override, a
"this one looks interesting so grab it early" -- any of those silently turns the
dataset into the thing CLAUDE.md warns about, a comparison of winners at peak
against losers at launch.

So the rule is a pure function of exactly two numbers:

    evaluate(mcap_usd, holder_count) -> TriggerDecision

It cannot see the ticker, the chain, the deployer, the social profile, or the
clock, because it is not given them. There is no allowlist, no skiplist, and no
threshold parameter anywhere in this module. Changing a threshold means editing a
module constant and bumping the schema, which is a visible commit, not a runtime
flag someone can pass on a Tuesday.

Adding chains does not change any of this. The rule never sees the chain, so a BNB
token and a Solana token crossing $250k enter the dataset on identical terms --
which is the only way a cross-chain sample can be pooled or split by chain later
without the split itself being a confound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

# The trigger. One threshold pair, applied to every token on every chain.
TRIGGER_MCAP_USD: Final[float] = 250_000.0
TRIGGER_HOLDER_COUNT: Final[int] = 500

# The two members of the section 4 `trigger` enum.
TRIGGER_MCAP: Final[str] = "mcap_250k"
TRIGGER_HOLDERS: Final[str] = "holders_500"


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """Whether a token crossed, and on which condition."""

    fired: bool
    trigger: str | None
    mcap_crossed: bool
    holders_crossed: bool

    @property
    def both_crossed(self) -> bool:
        return self.mcap_crossed and self.holders_crossed


def _crossed(value: float | int | None, threshold: float) -> bool:
    """A threshold test that treats missing data as missing, never as zero.

    ``None`` does not cross. NaN and infinity do not cross either: they are what a
    broken upstream response looks like, not measurements. This is hard rule 3 at
    the one place where imputing a zero would quietly change which tokens enter
    the dataset.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    number = float(value)
    if not math.isfinite(number):
        return False
    return number >= threshold


def evaluate(mcap_usd: float | None, holder_count: int | None) -> TriggerDecision:
    """The trigger rule. A pure function of two numbers, identical for every token.

    Fires on ``mcap_usd >= 250_000`` or ``holder_count >= 500``, whichever the
    watcher sees first.

    When a single observation shows both conditions already met -- which happens
    when a token crosses between two polls, or when a backfill hands us a token
    long past both thresholds -- "whichever first" is unanswerable from the data,
    so ``trigger`` is set to ``mcap_250k`` by a fixed tie-break and both crossing
    flags are recorded on the row. The tie-break is arbitrary but constant; what
    matters is that it is not per-token, and that ``trigger_holders_crossed``
    preserves what actually happened.
    """
    mcap_crossed = _crossed(mcap_usd, TRIGGER_MCAP_USD)
    holders_crossed = _crossed(holder_count, TRIGGER_HOLDER_COUNT)
    if mcap_crossed:
        trigger = TRIGGER_MCAP
    elif holders_crossed:
        trigger = TRIGGER_HOLDERS
    else:
        trigger = None
    return TriggerDecision(
        fired=trigger is not None,
        trigger=trigger,
        mcap_crossed=mcap_crossed,
        holders_crossed=holders_crossed,
    )
