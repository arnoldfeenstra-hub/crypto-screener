"""Safety source -- the thing that turns "unmeasured" into a verdict.

BUILD_BRIEF.md section 1 lists RugCheck, GoPlus and Honeypot.is under
"Safety/rug checks". Until this module existed none of them was wired up, and the
consequence was the single largest accuracy problem in the repo: six of the eight
hard filters in ``filters/hard_filters.py`` could only ever answer *unknown*, so
almost every row was excluded as unmeasured rather than judged. An exclusion for
lack of evidence is honest, but it is not a screen. This is the screen.

Sources, both keyless
---------------------
* **GoPlus Token Security** -- one API covering the EVM chains *and* Solana with
  the same envelope. It answers honeypot, buy/sell tax, mint and freeze authority,
  LP holders and their lock state, top holders, proxy and ownership. It is the
  primary source and the only one that works off Solana.
* **RugCheck** -- Solana only, and a genuine second opinion rather than a
  duplicate: it reports LP locked percentage per market and a ``rugged`` flag that
  GoPlus has no equivalent for.

Where they overlap they are merged by :func:`merge`, which never lets an unknown
overwrite a measurement and never silently prefers the more optimistic answer --
a rejection from either source stands.

What is deliberately *not* concluded
------------------------------------
**A locked LP is not a lock that lasts 30 days.** ``check_liquidity_lock`` asks
whether LP is burned, or locked at least 30 days out. Neither API reports a lock
*expiry*. So a burn address holding the LP gives ``lp_burned=True`` -- burning is
irreversible, and the 30-day question does not arise -- while "locked, expiry
unknown" stays ``None``. Reading a locker balance as a 30-day lock would be
inventing the one number the filter is actually about.

**A holder count from a safety API never reaches the trigger.** GoPlus reports
``holder_count`` on EVM and not on Solana. Feeding it into the trigger would make
the 500-holder condition fireable on one chain and not another -- a chain-dependent
entry rule, which is exactly the confound that makes a pooled cross-chain sample
uninterpretable. Safety is fetched *after* the trigger has already decided, and its
holder count is stored as an observation, never as a trigger input.

Storage
-------
Reports go to the append-only ``safety_observations`` table, one row per fetch,
never an edit to the snapshot. A token re-checked tomorrow gets a second row and
both stay: "the mint authority was live when we looked and revoked later" is a real
sequence of events and a valuable one.

Stdlib only, so ``api/screener.py`` can use it too. No credential anywhere in this
module; both APIs are keyless.

Provenance warning, same as ``collectors/dexscreener.py``: the parsers below are
written to the documented response schemas and verified against
``fixtures/safety_responses.json``, which was hand-built to those schemas rather
than recorded off the wire. Run ``python -m collectors.safety --probe <chain>
<contract>`` before trusting a verdict, and re-record the fixture if a shape
differs. The parsers are total, so a shape change degrades to ``None`` -- unknown,
which excludes -- rather than to a wrong verdict.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from collectors import chains
from collectors.httpjson import HttpJsonError, JsonGetClient, Throttle
from collectors.schema import now_ms

log = logging.getLogger("safety")

GOPLUS_BASE = "https://api.gopluslabs.io"
RUGCHECK_BASE = "https://api.rugcheck.xyz"

# Keyless GoPlus is documented at 30 requests/minute. RugCheck's free tier is not
# published; 60/minute is a deliberately conservative guess for a free endpoint.
RATE_LIMIT_PER_MINUTE = {"goplus": 30, "rugcheck": 60}

# GoPlus batches EVM addresses in one query string. Kept modest: a long URL is the
# most common way a batched GET quietly starts failing.
MAX_ADDRESSES_PER_REQUEST = 20

# GoPlus numeric chain ids, by this repo's canonical chain name. A chain absent
# here has no safety source and its filters stay unknown -- which is the correct
# answer, and is why Robinhood Chain will show as unmeasured until GoPlus covers it.
GOPLUS_CHAIN_IDS: dict[str, str] = {
    "ethereum": "1",
    "bnb": "56",
    "polygon": "137",
    "base": "8453",
    "arbitrum": "42161",
    "avalanche": "43114",
    "optimism": "10",
    "blast": "81457",
    "tron": "tron",
}

# Chains with their own GoPlus endpoint rather than a numeric id.
GOPLUS_NATIVE_PATHS: dict[str, str] = {
    "solana": "/api/v1/solana/token_security",
    "sui": "/api/v1/sui/token_security",
}

# Addresses and tags that mean "these tokens are gone". Burning is irreversible,
# which is the only reason a burn can be read as satisfying a lock requirement.
BURN_ADDRESSES = frozenset(
    {
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "0xdead000000000000000042069420694206942069",
        "11111111111111111111111111111111",
        "1nc1nerator11111111111111111111111111111111",
    }
)
BURN_TAGS = ("burn", "null", "dead", "incinerat")

# Tags marking a holder that is not a holder in the sense the concentration filter
# means: pooled liquidity, a locker, or an exchange's omnibus wallet.
NON_HOLDER_TAGS = (
    "lp",
    "liquidity",
    "pool",
    "lock",
    "vesting",
    "burn",
    "null",
    "dead",
    "exchange",
    "cex",
    "binance",
    "coinbase",
    "okx",
    "bybit",
    "kraken",
    "gate",
    "bitget",
)

TOP_HOLDER_COUNT = 10


class SafetyError(RuntimeError):
    """A safety lookup failed after retries."""


@dataclass(frozen=True, slots=True)
class SafetyReport:
    """What a safety source could establish about one token.

    Every field is optional and ``None`` means *not established*, never *fine*.
    ``filters/hard_filters.py`` treats unknown as excluding, so a field this module
    cannot fill costs the token a place in the ranking rather than buying it one.
    """

    chain: str
    contract: str
    source: str
    collected_at_ms: int

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
    lp_locked_pct: float | None = None

    # Concentration and ownership
    top10_ex_lp_pct: float | None = None
    holder_count: int | None = None
    upgradeable: bool | None = None
    admin_renounced: bool | None = None

    # Deployer. The address is read off the token; the rug history is a second
    # lookup against GoPlus address security, and only EVM chains have one.
    deployer_address: str | None = None
    deployer_prior_rugs: int | None = None

    # Whole-token verdicts that no single filter maps to, kept because they are
    # evidence and the graveyard is the dataset.
    rugged: bool | None = None
    risk_labels: tuple[str, ...] = ()
    error: str | None = None

    @property
    def measured_fields(self) -> int:
        return sum(
            1
            for f in fields(self)
            if f.name
            not in ("chain", "contract", "source", "collected_at_ms", "risk_labels", "error")
            and getattr(self, f.name) is not None
        )

    def to_filter_fields(self) -> dict[str, Any]:
        """The subset ``FilterInput`` accepts, with unknowns left out entirely.

        Omitted rather than passed as ``None`` so a caller merging several sources
        cannot have a later unknown blank an earlier measurement.
        """
        candidate = {
            "honeypot": self.honeypot,
            "sells_failing": self.sells_failing,
            "buy_tax_pct": self.buy_tax_pct,
            "sell_tax_pct": self.sell_tax_pct,
            "mint_revoked": self.mint_revoked,
            "freeze_active": self.freeze_active,
            "lp_burned": self.lp_burned,
            "top10_ex_lp_pct": self.top10_ex_lp_pct,
            "upgradeable": self.upgradeable,
            "admin_renounced": self.admin_renounced,
            "deployer_prior_rugs": self.deployer_prior_rugs,
        }
        return {k: v for k, v in candidate.items() if v is not None}

    def to_row(self, snapshot_id: str | None = None) -> dict[str, Any]:
        row = asdict(self)
        row["risk_labels"] = json.dumps(list(self.risk_labels))
        row["snapshot_id"] = snapshot_id
        row["ts"] = self.collected_at_ms
        return row


def merge(first: SafetyReport | None, second: SafetyReport | None) -> SafetyReport | None:
    """Combine two reports on the same token.

    Two rules, and they are the whole point of the function:

    1. **An unknown never overwrites a measurement.** A source that did not answer
       has said nothing, and nothing must not erase something.
    2. **The unsafe answer wins a disagreement.** If one source says the mint
       authority is live and the other says it is revoked, the token is treated as
       having a live mint authority. Sources disagree because one of them is stale
       or wrong, and picking the reassuring one is how a screen quietly stops
       screening.
    """
    if first is None:
        return second
    if second is None:
        return first

    # Fields where True is the dangerous answer, and where either source asserting
    # it settles the matter.
    dangerous_true = ("honeypot", "sells_failing", "freeze_active", "upgradeable", "rugged")
    # Fields where False is the dangerous answer.
    dangerous_false = ("mint_revoked", "lp_burned", "admin_renounced")

    merged: dict[str, Any] = {}
    for name in dangerous_true:
        values = [getattr(first, name), getattr(second, name)]
        present = [v for v in values if v is not None]
        merged[name] = bool(any(present)) if present else None
    for name in dangerous_false:
        values = [getattr(first, name), getattr(second, name)]
        present = [v for v in values if v is not None]
        merged[name] = bool(all(present)) if present else None

    # Numeric fields: the more pessimistic reading, which is the larger tax, the
    # larger concentration, and the smaller locked percentage.
    for name, pick in (
        ("buy_tax_pct", max),
        ("sell_tax_pct", max),
        ("top10_ex_lp_pct", max),
        ("lp_locked_pct", min),
        ("holder_count", max),
        # More prior rugs is the pessimistic reading, and the one to keep.
        ("deployer_prior_rugs", max),
    ):
        present = [
            v for v in (getattr(first, name), getattr(second, name)) if v is not None
        ]
        merged[name] = pick(present) if present else None

    sources = sorted({first.source, second.source})
    return replace(
        first,
        source="+".join(sources),
        deployer_address=first.deployer_address or second.deployer_address,
        collected_at_ms=max(first.collected_at_ms, second.collected_at_ms),
        risk_labels=tuple(sorted(set(first.risk_labels) | set(second.risk_labels))),
        error=first.error or second.error,
        **merged,
    )


# --- parsing helpers ---------------------------------------------------------


def _flag(value: Any) -> bool | None:
    """GoPlus sends booleans as the strings "0" and "1". Anything else is unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes"):
        return True
    if text in ("0", "false", "no"):
        return False
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _percent_from_fraction(value: Any) -> float | None:
    """GoPlus reports taxes and holdings as fractions: "0.05" means 5%."""
    number = _number(value)
    return None if number is None else number * 100.0


def _is_burn(address: Any, tag: Any = None) -> bool:
    text = str(address or "").strip().lower()
    if text in BURN_ADDRESSES:
        return True
    blob = f"{tag or ''}".strip().lower()
    return any(marker in blob for marker in BURN_TAGS)


def _is_non_holder(tag: Any) -> bool:
    blob = f"{tag or ''}".strip().lower()
    return any(marker in blob for marker in NON_HOLDER_TAGS)


def _lp_state(lp_holders: Any) -> tuple[bool | None, float | None]:
    """``(lp_burned, lp_locked_pct)`` from a GoPlus-shaped LP holder list.

    Three outcomes, and keeping them apart is what makes the liquidity filter
    trustworthy:

    * **Burned** -- LP sits at a burn address. Irreversible, so it satisfies the
      lock requirement outright.
    * **Neither burned nor locked** -- holders are visible and none is locked or
      burned. That is evidence, and ``check_liquidity_lock`` rejects on it.
    * **Locked, expiry unknown** -- ``None``. Neither API reports a lock expiry,
      and the filter's question is specifically whether the lock outlasts 30 days.
    """
    if not isinstance(lp_holders, list) or not lp_holders:
        return None, None

    burned_pct = 0.0
    locked_pct = 0.0
    saw_any = False
    for entry in lp_holders:
        if not isinstance(entry, dict):
            continue
        saw_any = True
        percent = _percent_from_fraction(entry.get("percent")) or 0.0
        if _is_burn(entry.get("address") or entry.get("account"), entry.get("tag")):
            burned_pct += percent
        elif _flag(entry.get("is_locked")):
            locked_pct += percent

    if not saw_any:
        return None, None
    total_secured = burned_pct + locked_pct
    if burned_pct >= 50.0:
        return True, total_secured
    if total_secured <= 0.0:
        return False, 0.0
    return None, total_secured


def _top10_ex_lp(holders: Any) -> float | None:
    """Top-10 concentration excluding LP, lockers, burns and exchange wallets.

    Returns ``None`` when the source gave no holder list -- an empty answer is not
    a concentration of zero.
    """
    if not isinstance(holders, list) or not holders:
        return None
    percents = []
    for entry in holders:
        if not isinstance(entry, dict):
            continue
        tag = entry.get("tag")
        if _is_non_holder(tag) or _is_burn(entry.get("address") or entry.get("account"), tag):
            continue
        percent = _percent_from_fraction(entry.get("percent"))
        if percent is not None:
            percents.append(percent)
    if not percents:
        return None
    return sum(sorted(percents, reverse=True)[:TOP_HOLDER_COUNT])


# --- parsers (pure, total) ---------------------------------------------------


def parse_goplus_evm(
    payload: Any, chain: str, *, collected_at_ms: int | None = None
) -> dict[str, SafetyReport]:
    """Parse a GoPlus EVM ``token_security`` response into ``{contract: report}``.

    Contract keys are lowercased by GoPlus; they are kept lowercase here and the
    caller matches case-insensitively.
    """
    collected = collected_at_ms if collected_at_ms is not None else now_ms()
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}

    out: dict[str, SafetyReport] = {}
    for address, data in result.items():
        if not isinstance(data, dict):
            continue
        lp_burned, lp_locked_pct = _lp_state(data.get("lp_holders"))

        owner = str(data.get("owner_address") or "").strip().lower()
        renounced: bool | None = None
        if "owner_address" in data:
            # An empty or burn owner means ownership is gone. A live owner that can
            # also take ownership back is not renounced by any reading.
            renounced = not owner or _is_burn(owner)
            if _flag(data.get("can_take_back_ownership")) is True:
                renounced = False

        risks = [
            name
            for name in (
                "is_blacklisted",
                "is_whitelisted",
                "hidden_owner",
                "selfdestruct",
                "external_call",
                "trading_cooldown",
                "personal_slippage_modifiable",
                "slippage_modifiable",
                "transfer_pausable",
                "anti_whale_modifiable",
            )
            if _flag(data.get(name)) is True
        ]

        mintable = _flag(data.get("is_mintable"))
        creator = data.get("creator_address") or data.get("owner_address") or None
        out[str(address).lower()] = SafetyReport(
            chain=chain,
            contract=str(address),
            source="goplus",
            collected_at_ms=collected,
            honeypot=_flag(data.get("is_honeypot")),
            sells_failing=_flag(data.get("cannot_sell_all")),
            buy_tax_pct=_percent_from_fraction(data.get("buy_tax")),
            sell_tax_pct=_percent_from_fraction(data.get("sell_tax")),
            mint_revoked=None if mintable is None else not mintable,
            # EVM has no freeze authority. `transfer_pausable` is the analogue that
            # produces the same outcome for a holder: the tokens cannot be moved.
            freeze_active=_flag(data.get("transfer_pausable")),
            lp_burned=lp_burned,
            lp_locked_pct=lp_locked_pct,
            top10_ex_lp_pct=_top10_ex_lp(data.get("holders")),
            holder_count=(
                int(_number(data.get("holder_count")))
                if _number(data.get("holder_count")) is not None
                else None
            ),
            upgradeable=_flag(data.get("is_proxy")),
            admin_renounced=renounced,
            deployer_address=str(creator) if creator else None,
            risk_labels=tuple(risks),
        )
    return out


def parse_goplus_solana(
    payload: Any, *, collected_at_ms: int | None = None
) -> dict[str, SafetyReport]:
    """Parse a GoPlus Solana ``token_security`` response into ``{mint: report}``."""
    collected = collected_at_ms if collected_at_ms is not None else now_ms()
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}

    out: dict[str, SafetyReport] = {}
    for mint, data in result.items():
        if not isinstance(data, dict):
            continue
        lp_burned, lp_locked_pct = _lp_state(data.get("lp_holders"))

        mintable = _flag((data.get("mintable") or {}).get("status"))
        freezable = _flag((data.get("freezable") or {}).get("status"))

        # A non-transferable mint cannot be sold at all. That is the Solana shape of
        # the honeypot question, and it is the one thing here that maps to it.
        non_transferable = _flag(data.get("non_transferable"))

        transfer_fee = data.get("transfer_fee")
        fee_pct = None
        if isinstance(transfer_fee, dict) and transfer_fee:
            fee_pct = _number(transfer_fee.get("transfer_fee_percent"))
            if fee_pct is None:
                fee_pct = _percent_from_fraction(transfer_fee.get("transfer_fee"))

        risks = [
            name
            for name, value in (
                ("closable", (data.get("closable") or {}).get("status")),
                ("metadata_mutable", (data.get("metadata_mutable") or {}).get("status")),
                (
                    "balance_mutable_authority",
                    (data.get("balance_mutable_authority") or {}).get("status"),
                ),
                ("default_account_state_frozen", data.get("default_account_state")),
                ("transfer_hook", "1" if data.get("transfer_hook") else "0"),
            )
            if _flag(value) is True
        ]

        creators = data.get("creators")
        creator = None
        if isinstance(creators, list) and creators:
            first = creators[0]
            creator = first.get("address") if isinstance(first, dict) else first

        out[str(mint)] = SafetyReport(
            chain="solana",
            contract=str(mint),
            source="goplus",
            collected_at_ms=collected,
            sells_failing=non_transferable,
            # A transfer fee is charged on both sides on Solana.
            buy_tax_pct=fee_pct,
            sell_tax_pct=fee_pct,
            mint_revoked=None if mintable is None else not mintable,
            freeze_active=freezable,
            lp_burned=lp_burned,
            lp_locked_pct=lp_locked_pct,
            top10_ex_lp_pct=_top10_ex_lp(data.get("holders")),
            # Solana programs are not EVM proxies; hard_filters already answers the
            # proxy question from the chain registry, so nothing is asserted here.
            deployer_address=str(creator) if creator else None,
            # deployer_prior_rugs stays None on Solana: GoPlus address security
            # covers EVM addresses only, and there is no keyless equivalent. Unknown
            # excludes, which is the correct and conservative answer -- it is not a
            # claim that the deployer is clean.
            risk_labels=tuple(risks),
        )
    return out


def parse_rugcheck(
    payload: Any, contract: str, *, collected_at_ms: int | None = None
) -> SafetyReport | None:
    """Parse a RugCheck ``/report`` response. Solana only."""
    collected = collected_at_ms if collected_at_ms is not None else now_ms()
    if not isinstance(payload, dict) or not payload:
        return None

    # RugCheck reports an authority as an address, or null when it is revoked. The
    # key being absent means the report did not cover it, which is not the same.
    mint_revoked = None
    if "mintAuthority" in payload:
        mint_revoked = not payload.get("mintAuthority")
    freeze_active = None
    if "freezeAuthority" in payload:
        freeze_active = bool(payload.get("freezeAuthority"))

    locked_pcts = []
    burned = None
    markets = payload.get("markets")
    if isinstance(markets, list):
        for market in markets:
            lp = (market or {}).get("lp") if isinstance(market, dict) else None
            if not isinstance(lp, dict):
                continue
            pct = _number(lp.get("lpLockedPct"))
            if pct is not None:
                locked_pcts.append(pct)
            if _flag(lp.get("lpBurned")) is True:
                burned = True

    top10 = None
    holders = payload.get("topHolders")
    if isinstance(holders, list) and holders:
        percents = [
            _number(h.get("pct"))
            for h in holders
            if isinstance(h, dict) and not _is_non_holder(h.get("owner"))
        ]
        clean = [p for p in percents if p is not None]
        if clean:
            top10 = sum(sorted(clean, reverse=True)[:TOP_HOLDER_COUNT])

    risks = tuple(
        str(risk.get("name"))
        for risk in (payload.get("risks") or [])
        if isinstance(risk, dict) and risk.get("name")
    )

    total_holders = _number(payload.get("totalHolders"))
    creator = payload.get("creator") or payload.get("creatorAddress")
    return SafetyReport(
        chain="solana",
        contract=contract,
        source="rugcheck",
        deployer_address=str(creator) if creator else None,
        collected_at_ms=collected,
        mint_revoked=mint_revoked,
        freeze_active=freeze_active,
        lp_burned=burned,
        lp_locked_pct=min(locked_pcts) if locked_pcts else None,
        top10_ex_lp_pct=top10,
        holder_count=int(total_holders) if total_holders is not None else None,
        rugged=payload.get("rugged") if isinstance(payload.get("rugged"), bool) else None,
        risk_labels=risks,
    )


# Address-security flags that mean this wallet has been involved in taking other
# people's money. `check_deployer_history` asks whether the deployer is "linked to
# >=1 prior confirmed rug or soft-rug"; these are the keyless, sourced answer to it.
DEPLOYER_RUG_FLAGS = (
    "honeypot_related_address",
    "phishing_activities",
    "financial_crime",
    "stealing_attack",
    "blackmail_activities",
    "fake_token",
    "cybercrime",
)


def parse_goplus_address_security(payload: Any, address: str) -> tuple[int | None, tuple[str, ...]]:
    """``(prior_rugs, labels)`` from a GoPlus address-security response.

    A clean response -- one that answered the questions and said no to all of them
    -- is a real zero and lets ``check_deployer_history`` pass. A response that
    carries none of the keys answered nothing, so the count stays ``None`` and the
    filter keeps excluding. The difference between "checked, clean" and "not
    checked" is the entire value of this lookup.
    """
    if not isinstance(payload, dict):
        return None, ()
    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        return None, ()

    answered = [name for name in DEPLOYER_RUG_FLAGS if name in result]
    malicious = _number(result.get("number_of_malicious_contracts_created"))
    if not answered and malicious is None:
        return None, ()

    hits = tuple(name for name in answered if _flag(result.get(name)) is True)
    count = len(hits)
    if malicious is not None and malicious > 0:
        count += int(malicious)
    return count, hits


# --- clients -----------------------------------------------------------------


def _throttle() -> Throttle:
    return Throttle(RATE_LIMIT_PER_MINUTE)


class GoPlusClient(JsonGetClient):
    """Keyless GoPlus token-security client. Reads only."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", GOPLUS_BASE)
        kwargs.setdefault("throttle", _throttle())
        super().__init__(**kwargs)

    def address_security(self, chain: str, address: str) -> Any:
        """Reputation of one wallet. EVM chains only -- GoPlus has no Solana route."""
        chain_id = GOPLUS_CHAIN_IDS.get(chain)
        if not chain_id:
            raise SafetyError(f"GoPlus address security does not cover {chain!r}")
        return self.get(f"/api/v1/address_security/{address}", "goplus", {
            "chain_id": chain_id
        })

    def token_security(self, chain: str, addresses: Sequence[str]) -> Any:
        if not addresses:
            return {}
        joined = ",".join(addresses[:MAX_ADDRESSES_PER_REQUEST])
        native = GOPLUS_NATIVE_PATHS.get(chain)
        if native:
            return self.get(native, "goplus", {"contract_addresses": joined})
        chain_id = GOPLUS_CHAIN_IDS.get(chain)
        if not chain_id:
            raise SafetyError(f"GoPlus has no chain id for {chain!r}")
        return self.get(f"/api/v1/token_security/{chain_id}", "goplus", {
            "contract_addresses": joined
        })


class RugCheckClient(JsonGetClient):
    """Keyless RugCheck client. Solana only, one token per request."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", RUGCHECK_BASE)
        kwargs.setdefault("throttle", _throttle())
        super().__init__(**kwargs)

    def report(self, mint: str) -> Any:
        return self.get(f"/v1/tokens/{mint}/report", "rugcheck")


@dataclass
class SafetySource:
    """Fetches safety reports for a batch of tokens, across chains.

    Failure is per token, never per batch: a chain GoPlus does not cover, or an
    endpoint having a bad minute, leaves those tokens unmeasured and the rest
    measured. Unmeasured excludes, so a degraded fetch makes the screener more
    conservative rather than less.
    """

    goplus: GoPlusClient | None = None
    rugcheck: RugCheckClient | None = None
    use_rugcheck: bool = True
    # RugCheck is one request per token, so it is only worth spending on the tokens
    # that got far enough to matter. Zero disables it.
    rugcheck_limit: int = 25
    # Address security is one request per *unique deployer*, and it is the only
    # thing that can answer check_deployer_history. Deduplicated, then capped:
    # keyless GoPlus is 30 requests a minute, so an uncapped sweep would take
    # longer than the caller is willing to wait. Zero disables it.
    deployer_limit: int = 25

    def __post_init__(self) -> None:
        self.goplus = self.goplus or GoPlusClient()
        if self.use_rugcheck and self.rugcheck is None:
            self.rugcheck = RugCheckClient()

    def _add_deployer_history(
        self, reports: dict[tuple[str, str], SafetyReport]
    ) -> dict[tuple[str, str], SafetyReport]:
        """Look up each distinct deployer once and attach the result to its tokens.

        Deduplicated because one deployer commonly launches many tokens, and a
        per-token lookup would spend the whole rate limit re-asking the same
        question.
        """
        wanted: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for key, report in reports.items():
            deployer = _deployer_key(report)
            if deployer is not None:
                wanted.setdefault(deployer, []).append(key)
        if not wanted:
            return reports

        # Deployers behind the most tokens first: one lookup answers more rows.
        ordered = sorted(wanted.items(), key=lambda kv: -len(kv[1]))[: self.deployer_limit]
        for (chain, address), token_keys in ordered:
            try:
                payload = self.goplus.address_security(chain, address)
            except (HttpJsonError, SafetyError):
                log.info("address security unavailable for %s on %s", address, chain)
                continue
            prior_rugs, labels = parse_goplus_address_security(payload, address)
            if prior_rugs is None:
                continue
            for key in token_keys:
                report = reports[key]
                reports[key] = replace(
                    report,
                    deployer_prior_rugs=prior_rugs,
                    risk_labels=tuple(sorted(set(report.risk_labels) | set(labels))),
                )
        return reports

    def fetch(
        self, tokens: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], SafetyReport]:
        """``{(chain, contract): report}`` for whatever could be established."""
        out: dict[tuple[str, str], SafetyReport] = {}
        by_chain: dict[str, list[str]] = {}
        for chain, contract in tokens:
            by_chain.setdefault(chain, []).append(contract)

        for chain, contracts in by_chain.items():
            if chain not in GOPLUS_CHAIN_IDS and chain not in GOPLUS_NATIVE_PATHS:
                log.info("no safety source for chain %s; %d tokens stay unmeasured",
                         chain, len(contracts))
                continue
            for batch in [
                contracts[i : i + MAX_ADDRESSES_PER_REQUEST]
                for i in range(0, len(contracts), MAX_ADDRESSES_PER_REQUEST)
            ]:
                try:
                    payload = self.goplus.token_security(chain, batch)
                except (HttpJsonError, SafetyError):
                    log.exception("goplus lookup failed for %d tokens on %s", len(batch), chain)
                    continue
                reports = (
                    parse_goplus_solana(payload)
                    if chain == "solana"
                    else parse_goplus_evm(payload, chain)
                )
                lowered = {c.lower(): c for c in batch}
                for key, report in reports.items():
                    original = lowered.get(key.lower())
                    if original is None:
                        continue
                    out[(chain, original)] = replace(report, contract=original)

        if self.deployer_limit:
            out = self._add_deployer_history(out)

        if self.use_rugcheck and self.rugcheck is not None:
            solana = [c for chain, c in tokens if chain == "solana"][: self.rugcheck_limit]
            for contract in solana:
                try:
                    payload = self.rugcheck.report(contract)
                except HttpJsonError:
                    log.info("rugcheck had nothing for %s", contract)
                    continue
                report = parse_rugcheck(payload, contract)
                if report is not None:
                    key = ("solana", contract)
                    merged = merge(out.get(key), report)
                    if merged is not None:
                        out[key] = merged
        return out


def _deployer_key(report: SafetyReport) -> tuple[str, str] | None:
    if not report.deployer_address or report.chain not in GOPLUS_CHAIN_IDS:
        return None
    return (report.chain, report.deployer_address.lower())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="safety",
        description=(
            "Probe the safety sources for one or more tokens and print what could be "
            "established. Needs no API key. Reads only."
        ),
    )
    parser.add_argument("--probe", action="store_true", help="run the lookup")
    parser.add_argument("--chain", default="solana", help="chain name (default: solana)")
    parser.add_argument("contracts", nargs="*", help="contract addresses")
    parser.add_argument("--no-rugcheck", action="store_true", help="GoPlus only")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    if not args.probe or not args.contracts:
        parser.print_help()
        return 0

    chain = chains.canonical(args.chain) or args.chain
    source = SafetySource(use_rugcheck=not args.no_rugcheck)
    reports = source.fetch([(chain, c) for c in args.contracts])

    payload = {
        "chain": chain,
        "requested": len(args.contracts),
        "measured": len(reports),
        "unmeasured": [c for c in args.contracts if (chain, c) not in reports],
        "reports": {
            contract: {
                k: v
                for k, v in asdict(report).items()
                if v is not None and k not in ("chain", "contract")
            }
            for (_, contract), report in reports.items()
        },
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
