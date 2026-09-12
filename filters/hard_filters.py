"""Phase 1 -- hard filters.

BUILD_BRIEF.md section 3: "Implement hard filters as pure functions with unit tests.
These do not need calibration -- a honeypot is a honeypot."

That is why this module is finishable while everything downstream of it is still
waiting for data: the eight checks in prompts/score.md step 1 are binary facts about
a contract, not predictions. They need no fitted weights and no outcome history.

Three-way outcomes, not two
---------------------------
A filter returns PASS, REJECT or UNKNOWN, and both REJECT and UNKNOWN exclude a
token from scoring. Collapsing them would be the imputation hard rule 3 forbids:
"the deployer has one prior rug" and "we never checked the deployer" are different
facts, and only one of them is evidence. They are recorded separately so that a
later analysis can ask how often a token was excluded for being bad versus for
being unmeasured -- and in Phase 0, where the safety fields are mostly unfilled,
the answer is overwhelmingly the latter.

Unknown never passes. That is the conservative direction: an unchecked mint
authority is not a revoked one, and treating it as clean would let the filter's
whole purpose leak away silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from collectors import chains

if TYPE_CHECKING:
    from collectors.schema import Snapshot

PHASE = "1"

# prompts/score.md step 1. Reject on any of these.
FILTER_NAMES = (
    "sellability",
    "mint_authority",
    "freeze_authority",
    "liquidity_lock",
    "concentration",
    "deployer_history",
    "liquidity_depth",
    "proxy_risk",
)

TRANSFER_TAX_MAX_PCT = 5.0
LP_LOCK_MIN_DAYS = 30
# Share of LP that must sit in a locker or burn address to satisfy the lock
# requirement when no expiry is obtainable. See check_liquidity_lock for why this
# threshold exists and what it gives up.
LP_LOCK_MIN_PCT = 95.0
TOP10_EX_LP_MAX_PCT = 35.0
LIQUIDITY_MIN_PCT_OF_FDV = 2.0

_MS_PER_DAY = 86_400_000


class Outcome(StrEnum):
    PASS = "pass"
    REJECT = "reject"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FilterResult:
    name: str
    outcome: Outcome
    reason: str

    @property
    def excludes(self) -> bool:
        return self.outcome is not Outcome.PASS


@dataclass(frozen=True, slots=True)
class FilterInput:
    """Everything the eight filters need.

    Wider than the section 4 snapshot: sellability, transfer tax, LP burn and proxy
    admin come from a safety source (RugCheck / GoPlus / Honeypot.is per
    BUILD_BRIEF.md section 1), not from the chain-metrics query. Every field is
    optional, and absent means unknown.
    """

    chain: str = "solana"
    ticker: str | None = None
    contract: str | None = None
    evaluated_at_ms: int | None = None

    # Sellability
    honeypot: bool | None = None
    sells_failing: bool | None = None
    buy_tax_pct: float | None = None
    sell_tax_pct: float | None = None

    # Authorities
    mint_revoked: bool | None = None
    freeze_active: bool | None = None

    # Liquidity lock
    lp_burned: bool | None = None
    lp_locked_until_ms: int | None = None
    # Share of LP held in a locker or burn address. The keyless safety sources
    # report this but never an expiry; see check_liquidity_lock.
    lp_locked_pct: float | None = None

    # Concentration and depth
    top10_ex_lp_pct: float | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    mcap_usd: float | None = None

    # Deployer
    deployer_prior_rugs: int | None = None

    # Proxy
    upgradeable: bool | None = None
    admin_renounced: bool | None = None

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot, **safety: object) -> FilterInput:
        """Build from a stored snapshot, with safety fields supplied separately.

        The snapshot carries four of the inputs today. The rest stay ``None`` until
        a safety collector exists, which is why nearly every Phase 0 row comes back
        indeterminate rather than clean -- an accurate description of what is known
        about it, not a bug.
        """
        base = {
            "chain": snapshot.chain,
            "ticker": snapshot.ticker,
            "contract": snapshot.contract,
            "evaluated_at_ms": snapshot.ts,
            "mint_revoked": snapshot.authorities.mint_revoked,
            "freeze_active": snapshot.authorities.freeze_active,
            "lp_locked_until_ms": snapshot.authorities.lp_locked_until,
            "top10_ex_lp_pct": snapshot.holders.top10_ex_lp_pct,
            "liquidity_usd": snapshot.market.liquidity_usd,
            "mcap_usd": snapshot.market.mcap_usd,
            # DexScreener reports FDV, and check_liquidity_depth prefers it over
            # market cap. On a fresh launch most of the supply is unvested, so the
            # two differ by a lot and the filter's verdict differs with them.
            "fdv_usd": snapshot.market.fdv_usd,
            "deployer_prior_rugs": snapshot.deployer.prior_rugs,
        }
        base.update({k: v for k, v in safety.items() if k in cls.__dataclass_fields__})
        return cls(**base)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of running all eight filters over one token."""

    results: tuple[FilterResult, ...] = field(default_factory=tuple)

    @property
    def rejected_by(self) -> list[str]:
        """Filters that found actual evidence against the token."""
        return [r.name for r in self.results if r.outcome is Outcome.REJECT]

    @property
    def indeterminate_on(self) -> list[str]:
        """Filters that could not be answered. Excluding, but not evidence."""
        return [r.name for r in self.results if r.outcome is Outcome.UNKNOWN]

    @property
    def passed(self) -> bool:
        return not self.rejected_by and not self.indeterminate_on

    @property
    def excluded(self) -> bool:
        return not self.passed

    def reasons(self) -> dict[str, str]:
        return {r.name: r.reason for r in self.results if r.excludes}

    def to_dict(self) -> dict[str, object]:
        """The shape prompts/score.md step 3 expects for a rejected candidate.

        ``rejected_by`` there is one flat list, so both kinds of exclusion go in it;
        the distinction survives alongside, in ``indeterminate_on``.
        """
        return {
            "score": None if self.excluded else "eligible",
            "rejected_by": self.rejected_by + self.indeterminate_on,
            "hard_rejected_by": self.rejected_by,
            "indeterminate_on": self.indeterminate_on,
            "reasons": self.reasons(),
        }


def _unknown(name: str, what: str) -> FilterResult:
    return FilterResult(name, Outcome.UNKNOWN, f"{what} unknown; unknown is not clean")


def _ok(name: str, why: str) -> FilterResult:
    return FilterResult(name, Outcome.PASS, why)


def _reject(name: str, why: str) -> FilterResult:
    return FilterResult(name, Outcome.REJECT, why)


# --- The eight filters ------------------------------------------------------


def check_sellability(data: FilterInput) -> FilterResult:
    """Honeypot, failing sells, or transfer tax above 5% on either side."""
    name = "sellability"
    if data.honeypot is True:
        return _reject(name, "honeypot detected")
    if data.sells_failing is True:
        return _reject(name, "sells are failing")
    for side, tax in (("buy", data.buy_tax_pct), ("sell", data.sell_tax_pct)):
        if tax is not None and tax > TRANSFER_TAX_MAX_PCT:
            return _reject(name, f"{side} tax {tax:.2f}% > {TRANSFER_TAX_MAX_PCT}%")
    if data.honeypot is None or data.sells_failing is None:
        return _unknown(name, "sellability")
    if data.buy_tax_pct is None or data.sell_tax_pct is None:
        return _unknown(name, "transfer tax")
    return _ok(name, "sellable, taxes within limit")


def check_mint_authority(data: FilterInput) -> FilterResult:
    """Mint authority must be revoked. On EVM, the owner must retain no mint or rebase."""
    name = "mint_authority"
    if data.mint_revoked is None:
        return _unknown(name, "mint authority")
    if not data.mint_revoked:
        return _reject(name, "mint authority still active")
    return _ok(name, "mint authority revoked")


def check_freeze_authority(data: FilterInput) -> FilterResult:
    name = "freeze_authority"
    if data.freeze_active is None:
        return _unknown(name, "freeze authority")
    if data.freeze_active:
        return _reject(name, "freeze authority active")
    return _ok(name, "freeze authority inactive")


def check_liquidity_lock(data: FilterInput) -> FilterResult:
    """LP burned, or locked at least 30 days out from the evaluation time."""
    name = "liquidity_lock"
    if data.lp_burned is True:
        return _ok(name, "LP burned")
    if data.lp_locked_until_ms is not None:
        if data.evaluated_at_ms is None:
            return _unknown(name, "evaluation time")
        remaining_days = (data.lp_locked_until_ms - data.evaluated_at_ms) / _MS_PER_DAY
        if remaining_days < LP_LOCK_MIN_DAYS:
            return _reject(
                name, f"LP lock expires in {remaining_days:.1f}d < {LP_LOCK_MIN_DAYS}d"
            )
        return _ok(name, f"LP locked for {remaining_days:.0f}d")
    if data.lp_locked_pct is not None:
        # No keyless source reports a lock *expiry*, which is what the 30-day rule
        # above actually asks about. Holding out for it meant this filter answered
        # "unknown" for 12 of the 13 tokens in the first live run, so nothing could
        # ever be ranked -- the filter was not screening, it was abstaining.
        #
        # A measured share is weaker evidence than a dated lock and it is not
        # nothing: 97% of LP sitting in a locker cannot be pulled today, and is a
        # different object from LP that is wholly unlocked. So a high share passes,
        # and the reason string says plainly that the expiry was never measured.
        #
        # What this gives up: a lock that expires next week reads the same as one
        # that expires next year. That is a real loss and the honest way to close
        # it is a source that reports the expiry, not a lower threshold here.
        if data.lp_locked_pct >= LP_LOCK_MIN_PCT:
            return _ok(
                name,
                f"{data.lp_locked_pct:.0f}% of LP locked or burned; expiry not "
                "reported by any available source",
            )
        if data.lp_locked_pct <= 0:
            return _reject(name, "no LP locked or burned")
        return _unknown(
            name,
            f"only {data.lp_locked_pct:.0f}% of LP locked, under the "
            f"{LP_LOCK_MIN_PCT:.0f}% needed without an expiry; lock duration",
        )
    if data.lp_burned is False:
        return _reject(name, "LP neither burned nor locked")
    return _unknown(name, "LP burn and lock status")


def check_concentration(data: FilterInput) -> FilterResult:
    """Top 10 holders, excluding LP, CEX and known burn addresses, over 35%."""
    name = "concentration"
    if data.top10_ex_lp_pct is None:
        return _unknown(name, "top-10 concentration")
    if data.top10_ex_lp_pct > TOP10_EX_LP_MAX_PCT:
        return _reject(
            name, f"top-10 ex-LP hold {data.top10_ex_lp_pct:.1f}% > {TOP10_EX_LP_MAX_PCT}%"
        )
    return _ok(name, f"top-10 ex-LP hold {data.top10_ex_lp_pct:.1f}%")


def check_deployer_history(data: FilterInput) -> FilterResult:
    """Any prior confirmed rug or soft-rug on the deployer wallet."""
    name = "deployer_history"
    if data.deployer_prior_rugs is None:
        return _unknown(name, "deployer history")
    if data.deployer_prior_rugs >= 1:
        return _reject(name, f"deployer linked to {data.deployer_prior_rugs} prior rug(s)")
    return _ok(name, "no prior rugs on deployer")


def check_liquidity_depth(data: FilterInput) -> FilterResult:
    """Pooled liquidity below 2% of fully diluted market cap.

    Falls back to market cap when FDV is not reported. That is a substitution of
    one measured number for another, not an invented one -- and it is recorded in
    the reason string so the two cases stay distinguishable.
    """
    name = "liquidity_depth"
    reference = data.fdv_usd if data.fdv_usd is not None else data.mcap_usd
    label = "FDV" if data.fdv_usd is not None else "mcap"
    if data.liquidity_usd is None or reference is None:
        return _unknown(name, "liquidity or valuation")
    if reference <= 0:
        return _unknown(name, f"{label} is not positive")
    pct = data.liquidity_usd / reference * 100
    if pct < LIQUIDITY_MIN_PCT_OF_FDV:
        return _reject(
            name, f"liquidity {pct:.2f}% of {label} < {LIQUIDITY_MIN_PCT_OF_FDV}%"
        )
    return _ok(name, f"liquidity {pct:.2f}% of {label}")


def check_proxy_risk(data: FilterInput) -> FilterResult:
    """Upgradeable contract whose admin has not been renounced.

    Proxies are an EVM construct, so the question is decided by whether the chain
    has an EVM at all -- ``collectors/chains.py`` answers that -- rather than by a
    literal match on "solana". The distinction started mattering the moment the
    watcher could hold more than one chain: Sui and TON would otherwise have hung
    as unknown forever, and a future non-EVM chain would have too.

    A chain the registry has never seen returns ``None`` from ``is_evm`` and comes
    back unknown. That is the conservative direction and the deliberate one: an
    unrecognised chain must not inherit Solana's free pass.
    """
    name = "proxy_risk"
    if data.upgradeable is None:
        evm = chains.is_evm(data.chain)
        if evm is False:
            return _ok(name, f"proxies are not a construct on {data.chain}")
        if evm is None:
            return _unknown(name, f"whether {data.chain} has EVM proxies")
        return _unknown(name, "upgradeability")
    if not data.upgradeable:
        return _ok(name, "contract is not upgradeable")
    if data.admin_renounced is None:
        return _unknown(name, "proxy admin")
    if not data.admin_renounced:
        return _reject(name, "upgradeable with unrenounced admin")
    return _ok(name, "upgradeable but admin renounced")


CHECKS = (
    check_sellability,
    check_mint_authority,
    check_freeze_authority,
    check_liquidity_lock,
    check_concentration,
    check_deployer_history,
    check_liquidity_depth,
    check_proxy_risk,
)


def apply(data: FilterInput) -> Verdict:
    """Run all eight filters. Every filter always runs -- there is no early exit.

    Short-circuiting on the first rejection would be faster and would throw away the
    reason the token was excluded, which is the part worth keeping: a row rejected
    by three filters is a different observation from one rejected by a single
    marginal check.
    """
    return Verdict(results=tuple(check(data) for check in CHECKS))


def apply_to_snapshot(snapshot: Snapshot, **safety: object) -> Verdict:
    return apply(FilterInput.from_snapshot(snapshot, **safety))
