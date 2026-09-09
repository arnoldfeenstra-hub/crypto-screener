"""What a chain source reports about one token at one moment.

:class:`TokenMetrics` is the boundary between "some API said this" and the section 4
snapshot. Source clients (``collectors/bitquery.py``) parse into it; the trigger
watcher reads two of its fields; ``collectors/snapshot.py`` maps it onto the
snapshot schema.

Every field is optional except identity and the observation time. A source that
does not report a value leaves it ``None`` -- hard rule 3, never impute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from collectors.schema import clean


@dataclass(frozen=True, slots=True)
class Observation:
    """The trigger inputs, and nothing else that the rule is allowed to see.

    Deliberately narrow. The trigger rule must be identical for every token
    (BUILD_BRIEF.md section 3, "Same rule for every token, no exceptions"), so the
    rule is a function of two numbers and the identity fields are carried alongside
    for logging rather than passed into the decision.
    """

    chain: str
    contract: str
    observed_at_ms: int
    mcap_usd: float | None = None
    holder_count: int | None = None


@dataclass(frozen=True, slots=True)
class TokenMetrics:
    """One token as a chain source reports it at ``observed_at_ms``."""

    chain: str
    contract: str
    observed_at_ms: int
    source: str

    ticker: str | None = None
    name: str | None = None
    first_seen_at_ms: int | None = None  # pool creation; None means unknown age

    mcap_usd: float | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None

    holder_count: int | None = None
    holders_growth_6h_pct: float | None = None
    top10_ex_lp_pct: float | None = None

    mint_revoked: bool | None = None
    freeze_active: bool | None = None
    lp_locked_until_ms: int | None = None

    deployer_address: str | None = None
    deployer_prior_launches: int | None = None
    deployer_prior_rugs: int | None = None

    bundled_supply_pct: float | None = None
    sniper_wallets: int | None = None
    initial_buy_sol: float | None = None

    declared_telegram: bool | None = None
    declared_x: bool | None = None
    declared_website: bool | None = None

    listings: list[str] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def observation(self) -> Observation:
        """Narrow to just what the trigger rule may look at."""
        return Observation(
            chain=self.chain,
            contract=self.contract,
            observed_at_ms=self.observed_at_ms,
            mcap_usd=clean(self.mcap_usd),
            holder_count=self.holder_count,
        )

    def age_minutes(self) -> int | None:
        """Minutes between pool creation and this observation, or ``None`` if unknown.

        Unknown stays unknown: an age of zero would claim the token launched at the
        instant it was observed, which is a guess, and a wrong one for anything
        found by a backfill.
        """
        if self.first_seen_at_ms is None:
            return None
        delta_ms = self.observed_at_ms - self.first_seen_at_ms
        if delta_ms < 0:
            return None
        return int(delta_ms // 60_000)
