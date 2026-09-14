"""DexScreener source -- real market data, no credential, several chains.

BUILD_BRIEF.md section 1 lists DexScreener under "Price/liquidity". It earns a
bigger role than that here for one practical reason: it is the only source in the
stack that returns live market data for a brand-new token on Solana *and* on the
EVM chains, with the same response shape and no API key. Bitquery needs a paid
token and its Solana EAP queries in ``collectors/bitquery.py`` are still unverified;
until one of those changes, this is the module that puts real rows in the database.

What it can and cannot see
--------------------------
It reports market cap, FDV, liquidity, volume and transaction counts over four
windows (5m, 1h, 6h, 24h) with the buy/sell split, per-window price change, pool
age and declared socials. It does **not** report holder counts, mint or freeze authority,
deployer history, or bundling. Those stay ``None`` -- hard rule 3 -- with two
consequences worth stating plainly rather than discovering later:

* Only the ``mcap_250k`` half of the trigger can fire from this source. A missing
  holder count never crosses 500 (``collectors/trigger_rule.py::_crossed``), so the
  holders trigger simply never fires here. That is the correct behaviour and it is
  visible in ``trigger_breakdown``.
* Most hard filters come back ``unknown``, so most rows are excluded as unmeasured
  rather than scored. Also correct, also visible, and the reason a safety source
  (RugCheck / GoPlus) is the highest-value thing to wire in next.

FDV is the exception in the other direction: DexScreener reports it, nothing else
in the repo did, and ``check_liquidity_depth`` prefers it over market cap. That
filter starts returning real verdicts as soon as this source is used.

Stdlib only, deliberately
-------------------------
``urllib.request``, not ``requests``. This module is imported by ``api/screener.py``
running as a Vercel function, where every added dependency is another thing that
can fail at deploy time for a JSON GET that stdlib already does. It also means the
parsers below can be imported anywhere without pulling in the world.

Split, as in ``bitquery.py``: :class:`DexScreenerClient` is the only part that
touches a socket, and the ``parse_*`` functions are pure and total. The parsers are
tested against ``fixtures/dexscreener_responses.json``; the endpoint paths and
rate limits are the part that reality gets a vote on.

Nothing here writes anywhere and nothing here holds a credential. The client issues
GETs and has no method that does anything else (hard rule 4).
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from collectors import chains
from collectors.httpjson import HttpJsonError, JsonGetClient, Throttle
from collectors.metrics import TokenMetrics
from collectors.schema import now_ms

log = logging.getLogger("dexscreener")

BASE_URL = "https://api.dexscreener.com"
SOURCE_NAME = "dexscreener"
USER_AGENT = "crypto-screener/0.1 (phase-0 collector; read-only)"

# Documented per-endpoint limits. Kept as data, and enforced by the client, because
# a keyless public API is a shared resource and the polite ceiling is the real one.
RATE_LIMIT_PER_MINUTE: dict[str, int] = {
    "token-profiles": 60,
    "token-boosts": 60,
    "tokens": 300,
    "token-pairs": 300,
    "search": 300,
}

# `/latest/dex/tokens/{addresses}` takes a comma-separated list, capped upstream.
MAX_ADDRESSES_PER_REQUEST = 30

# Placeholder chain for a seeded address before the API has said where it lives.
# A seed is an address and nothing else -- `/latest/dex/tokens/{address}` needs no
# chainId and returns one, so the chain is read off the response rather than
# assumed. It never reaches a row: poll() replaces it with what came back, or
# drops the seed.
_SEED_CHAIN = "?"


class DexScreenerError(RuntimeError):
    """A DexScreener request failed after retries."""


# --- Network layer -----------------------------------------------------------
#
# Retry and throttle policy lives in collectors/httpjson.py, shared with the safety
# source. `_Throttle` stays as a name here because it is the throttle this client
# uses, now pre-loaded with the per-endpoint limits above.


def _Throttle(*, sleep: Any = None, clock: Any = None, **kwargs: Any) -> Throttle:
    """A Throttle carrying DexScreener's documented per-endpoint limits."""
    extra = {k: v for k, v in (("sleep", sleep), ("clock", clock)) if v is not None}
    return Throttle(RATE_LIMIT_PER_MINUTE, **extra, **kwargs)


@dataclass
class DexScreenerClient(JsonGetClient):
    """Read-only JSON GET client for the public DexScreener API.

    No token, no header beyond a user agent, no method that writes.
    """

    base_url: str = BASE_URL
    throttle: Throttle = field(default_factory=_Throttle)

    def _get(self, path: str, bucket: str, params: dict[str, str] | None = None) -> Any:
        try:
            return self.get(path, bucket, params)
        except HttpJsonError as exc:
            # Re-raised under this module's own error type so callers that catch
            # DexScreenerError keep catching everything this client can raise.
            raise DexScreenerError(str(exc)) from exc

    # Each endpoint is one method, so the rate-limit bucket is decided here rather
    # than guessed at every call site.

    def token_profiles(self) -> list[dict[str, Any]]:
        """Latest tokens with a filled-in DexScreener profile."""
        data = self._get("/token-profiles/latest/v1", "token-profiles")
        return data if isinstance(data, list) else []

    def token_boosts_latest(self) -> list[dict[str, Any]]:
        data = self._get("/token-boosts/latest/v1", "token-boosts")
        return data if isinstance(data, list) else []

    def token_boosts_top(self) -> list[dict[str, Any]]:
        data = self._get("/token-boosts/top/v1", "token-boosts")
        return data if isinstance(data, list) else []

    def pairs_for_tokens(self, addresses: Sequence[str]) -> list[dict[str, Any]]:
        """Every pool for up to 30 token addresses, in one request."""
        if not addresses:
            return []
        joined = ",".join(addresses[:MAX_ADDRESSES_PER_REQUEST])
        data = self._get(f"/latest/dex/tokens/{urllib.parse.quote(joined)}", "tokens")
        if isinstance(data, dict):
            return data.get("pairs") or []
        return data if isinstance(data, list) else []

    def pairs_for_token(self, chain_id: str, address: str) -> list[dict[str, Any]]:
        data = self._get(
            f"/token-pairs/v1/{urllib.parse.quote(chain_id)}/{urllib.parse.quote(address)}",
            "token-pairs",
        )
        if isinstance(data, dict):
            return data.get("pairs") or []
        return data if isinstance(data, list) else []

    def search(self, query: str) -> list[dict[str, Any]]:
        data = self._get("/latest/dex/search", "search", {"q": query})
        if isinstance(data, dict):
            return data.get("pairs") or []
        return data if isinstance(data, list) else []


# --- Parsers (pure, total) ---------------------------------------------------


def _get(obj: Any, *path: str) -> Any:
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_float(value: Any) -> float | None:
    """Parse a number the API may send as a string. Junk and NaN become ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _positive(value: float | None) -> float | None:
    """A non-positive market cap or liquidity is not a small one; it is unusable.

    Returned as ``None`` so it never becomes a denominator or crosses a threshold.
    """
    if value is None:
        return None
    return value if value > 0 else None


def _link_flags(entries: Any) -> tuple[bool, bool, bool]:
    """``(telegram, x, website)`` seen anywhere in a list of link objects.

    Returns three booleans that mean "found", never "absent" -- the caller decides
    whether not-found is ``False`` (we read a link list) or ``None`` (there was no
    link list to read). That distinction is the whole value of the socials triple:
    it is the best-evidenced feature in the schema, 17.4x graduation lift with all
    three, and a guessed ``False`` would corrupt exactly the column most likely to
    carry signal.
    """
    flags, _ = _classify_links(entries)
    return flags


def _link_urls(entries: Any) -> tuple[str | None, str | None]:
    """``(telegram_url, x_url)`` -- the addresses behind the first two flags.

    The booleans are the feature; these are where the forward social series has to
    be collected from. Nothing else in the pipeline knows a token's Telegram group,
    and `collectors/social_tg.py` cannot guess it: a handle derived from a ticker
    is a different channel, usually someone else's.

    The first link of each kind wins. A token listing two Telegram links is
    listing a group and a backup, and there is no basis in the response for
    preferring the second.
    """
    _, urls = _classify_links(entries)
    return urls


def _classify_links(
    entries: Any,
) -> tuple[tuple[bool, bool, bool], tuple[str | None, str | None]]:
    """Flags and addresses from one link list, in a single pass.

    One pass rather than two so a flag can never be True while its address is
    None for the same entry -- which would look exactly like a token whose
    Telegram link could not be read.
    """
    telegram = x_declared = website = False
    telegram_url: str | None = None
    x_url: str | None = None
    if not isinstance(entries, list):
        return (telegram, x_declared, website), (telegram_url, x_url)
    for entry in entries:
        if isinstance(entry, str):
            url, kind = entry, ""
        elif isinstance(entry, dict):
            url = str(entry.get("url") or "")
            kind = str(entry.get("type") or entry.get("label") or "")
        else:
            continue
        blob = f"{kind} {url}".lower()
        if "t.me" in blob or "telegram" in blob:
            telegram = True
            telegram_url = telegram_url or (url or None)
        elif "twitter.com" in blob or "x.com" in blob or "twitter" in blob:
            x_declared = True
            x_url = x_url or (url or None)
        elif url.startswith("http"):
            website = True
    return (telegram, x_declared, website), (telegram_url, x_url)


def declared_socials(pair: dict[str, Any]) -> tuple[bool | None, bool | None, bool | None]:
    """The socials triple for one pair, or ``(None, None, None)`` if it has no info block.

    An absent ``info`` object means DexScreener has no profile for the token, which
    is "we did not look", not "the token declared nothing".
    """
    info = pair.get("info")
    if not isinstance(info, dict):
        return None, None, None
    socials = info.get("socials")
    websites = info.get("websites")
    if socials is None and websites is None:
        return None, None, None
    tg_a, x_a, web_a = _link_flags(socials)
    tg_b, x_b, web_b = _link_flags(websites)
    return tg_a or tg_b, x_a or x_b, web_a or web_b


def declared_social_urls(pair: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(telegram_url, x_url)`` declared on one pair's profile."""
    info = pair.get("info")
    if not isinstance(info, dict):
        return None, None
    tg_a, x_a = _link_urls(info.get("socials"))
    tg_b, x_b = _link_urls(info.get("websites"))
    return tg_a or tg_b, x_a or x_b


def parse_discovery(entries: Any, *, kind: str) -> list[dict[str, Any]]:
    """Normalise a ``token-profiles`` or ``token-boosts`` response.

    Both endpoints return the same envelope: a chain, a token address, and links.
    Boosts add ``amount`` (this boost) and ``totalAmount`` (lifetime), which are
    paid-attention counts and feed :mod:`collectors.mindshare`.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("tokenAddress")
        chain = chains.from_dexscreener(entry.get("chainId"))
        if not address or not chain:
            continue
        telegram, x_declared, website = _link_flags(entry.get("links"))
        telegram_url, x_url = _link_urls(entry.get("links"))
        has_links = isinstance(entry.get("links"), list)
        out.append(
            {
                "chain": chain,
                "contract": str(address),
                "discovery": kind,
                "boost_amount": _as_float(entry.get("amount")),
                "boost_total": _as_float(entry.get("totalAmount")),
                "description": entry.get("description"),
                # No link list means nothing was read, so the triple stays unknown.
                "telegram_url": telegram_url,
                "x_url": x_url,
                "declared_telegram": telegram if has_links else None,
                "declared_x": x_declared if has_links else None,
                "declared_website": website if has_links else None,
            }
        )
    return out


@dataclass(frozen=True, slots=True)
class PairAggregate:
    """One token's pools, added up.

    A token trades in several pools and DexScreener returns one object per pool.
    Liquidity, volume and transaction counts are summed across them because those
    are additive facts about the token. Price, market cap and FDV are read from the
    deepest pool instead: they are the same quantity reported several times, and
    summing them would multiply the token's valuation by its number of pools.
    """

    chain: str
    contract: str
    ticker: str | None = None
    name: str | None = None
    price_usd: float | None = None
    mcap_usd: float | None = None
    fdv_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    volume_6h_usd: float | None = None
    volume_1h_usd: float | None = None
    txns_24h: int | None = None
    txns_6h: int | None = None
    txns_1h: int | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    buys_1h: int | None = None
    sells_1h: int | None = None
    # Price changes as DexScreener reports them, per window. Read from the deepest
    # pool rather than summed -- a percentage is not additive across pools.
    price_change_5m_pct: float | None = None
    price_change_1h_pct: float | None = None
    price_change_6h_pct: float | None = None
    price_change_24h_pct: float | None = None
    boosts_active: float | None = None
    pair_count: int = 0
    first_pair_created_at_ms: int | None = None
    dex_ids: tuple[str, ...] = ()
    declared_telegram: bool | None = None
    declared_x: bool | None = None
    declared_website: bool | None = None
    telegram_url: str | None = None
    x_url: str | None = None


def _sum_optional(values: Iterable[float | None]) -> float | None:
    """Sum the values that exist; ``None`` if none do.

    Never treats a missing pool value as zero -- a token whose only pool did not
    report volume has unknown volume, not zero volume.
    """
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _any_known(triples: list[tuple[bool | None, ...]], index: int) -> bool | None:
    """OR one slot of a list of socials triples, keeping unknown as unknown."""
    seen = [t[index] for t in triples if t[index] is not None]
    return any(seen) if seen else None


def parse_pairs(payload: Any) -> dict[tuple[str, str], PairAggregate]:
    """Aggregate a ``/latest/dex/tokens`` response into one record per token.

    Keyed by ``(chain, contract)`` because the same address can exist on more than
    one chain and they are not the same token.
    """
    if isinstance(payload, dict):
        raw_pairs = payload.get("pairs") or []
    elif isinstance(payload, list):
        raw_pairs = payload
    else:
        raw_pairs = []

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pair in raw_pairs:
        if not isinstance(pair, dict):
            continue
        chain = chains.from_dexscreener(pair.get("chainId"))
        address = _get(pair, "baseToken", "address")
        if not chain or not address:
            continue
        grouped.setdefault((chain, str(address)), []).append(pair)

    out: dict[tuple[str, str], PairAggregate] = {}
    for key, pairs in grouped.items():
        chain, contract = key
        # The deepest pool is the reference for per-token quantities: it is the one
        # whose price is hardest to push, so it is the least wrong single quote.
        primary = max(pairs, key=lambda p: _as_float(_get(p, "liquidity", "usd")) or 0.0)

        created = [_as_int(p.get("pairCreatedAt")) for p in pairs]
        created_known = [c for c in created if c is not None and c > 0]

        # h1 joins h24 and h6 because a 6h-over-24h ratio is the only rate of
        # change the earlier windows could express, and it saturates: a token
        # younger than six hours has txns_6h == txns_24h by construction, so the
        # ratio pins at 4.0 and measures age instead of momentum. The hour window
        # moves that boundary in by five hours; the buy/sell split does not
        # saturate at all, because it is a ratio between two counts over the same
        # window rather than between two windows.
        txn_windows: dict[str, list[int | None]] = {"h24": [], "h6": [], "h1": []}
        splits: dict[str, tuple[list[int | None], list[int | None]]] = {
            "h24": ([], []),
            "h1": ([], []),
        }
        for pair in pairs:
            for window in ("h24", "h6", "h1"):
                block = _get(pair, "txns", window)
                if not isinstance(block, dict):
                    txn_windows[window].append(None)
                    if window in splits:
                        splits[window][0].append(None)
                        splits[window][1].append(None)
                    continue
                b, s = _as_int(block.get("buys")), _as_int(block.get("sells"))
                txn_windows[window].append(None if b is None and s is None else (b or 0) + (s or 0))
                if window in splits:
                    splits[window][0].append(b)
                    splits[window][1].append(s)
        buys, sells = splits["h24"]
        buys_1h, sells_1h = splits["h1"]

        socials = [declared_socials(p) for p in pairs]
        link_urls = [declared_social_urls(p) for p in pairs]
        # Any pool carrying a profile answers for the token; only if none does is
        # the triple unknown.
        triple = tuple(_any_known(socials, index) for index in range(3))

        out[key] = PairAggregate(
            chain=chain,
            contract=contract,
            ticker=_get(primary, "baseToken", "symbol"),
            name=_get(primary, "baseToken", "name"),
            price_usd=_as_float(primary.get("priceUsd")),
            mcap_usd=_positive(_as_float(primary.get("marketCap"))),
            fdv_usd=_positive(_as_float(primary.get("fdv"))),
            liquidity_usd=_sum_optional(
                _as_float(_get(p, "liquidity", "usd")) for p in pairs
            ),
            volume_24h_usd=_sum_optional(_as_float(_get(p, "volume", "h24")) for p in pairs),
            volume_6h_usd=_sum_optional(_as_float(_get(p, "volume", "h6")) for p in pairs),
            volume_1h_usd=_sum_optional(_as_float(_get(p, "volume", "h1")) for p in pairs),
            txns_24h=_as_int(_sum_optional(txn_windows["h24"])),
            txns_6h=_as_int(_sum_optional(txn_windows["h6"])),
            txns_1h=_as_int(_sum_optional(txn_windows["h1"])),
            buys_24h=_as_int(_sum_optional(buys)),
            sells_24h=_as_int(_sum_optional(sells)),
            buys_1h=_as_int(_sum_optional(buys_1h)),
            sells_1h=_as_int(_sum_optional(sells_1h)),
            price_change_5m_pct=_as_float(_get(primary, "priceChange", "m5")),
            price_change_1h_pct=_as_float(_get(primary, "priceChange", "h1")),
            price_change_6h_pct=_as_float(_get(primary, "priceChange", "h6")),
            price_change_24h_pct=_as_float(_get(primary, "priceChange", "h24")),
            boosts_active=_sum_optional(_as_float(_get(p, "boosts", "active")) for p in pairs),
            pair_count=len(pairs),
            # Earliest pool is the token's age; a later pool is a second listing.
            first_pair_created_at_ms=min(created_known) if created_known else None,
            dex_ids=tuple(sorted({str(p.get("dexId")) for p in pairs if p.get("dexId")})),
            declared_telegram=triple[0],
            declared_x=triple[1],
            declared_website=triple[2],
            telegram_url=next((u[0] for u in link_urls if u[0]), None),
            x_url=next((u[1] for u in link_urls if u[1]), None),
        )
    return out


def to_metrics(
    aggregate: PairAggregate,
    *,
    observed_at_ms: int | None = None,
    discovery: dict[str, Any] | None = None,
) -> TokenMetrics:
    """Turn one aggregated token into the repo's source-neutral record.

    Fields DexScreener cannot answer -- holders, authorities, deployer, bundling --
    are left ``None`` rather than defaulted, which is what keeps
    ``data_completeness`` an honest description of the row.
    """
    observed = observed_at_ms if observed_at_ms is not None else now_ms()
    discovery = discovery or {}

    def _pick(field_name: str) -> Any:
        # A pool profile is the better witness; the discovery endpoint's link list
        # is the fallback. Neither is allowed to turn an unknown into a False.
        primary = getattr(aggregate, field_name)
        return primary if primary is not None else discovery.get(field_name)

    return TokenMetrics(
        chain=aggregate.chain,
        contract=aggregate.contract,
        observed_at_ms=observed,
        source=SOURCE_NAME,
        ticker=aggregate.ticker,
        name=aggregate.name,
        first_seen_at_ms=aggregate.first_pair_created_at_ms,
        mcap_usd=aggregate.mcap_usd,
        fdv_usd=aggregate.fdv_usd,
        price_usd=aggregate.price_usd,
        liquidity_usd=aggregate.liquidity_usd,
        volume_24h_usd=aggregate.volume_24h_usd,
        volume_6h_usd=aggregate.volume_6h_usd,
        volume_1h_usd=aggregate.volume_1h_usd,
        txns_24h=aggregate.txns_24h,
        txns_6h=aggregate.txns_6h,
        txns_1h=aggregate.txns_1h,
        buys_24h=aggregate.buys_24h,
        sells_24h=aggregate.sells_24h,
        buys_1h=aggregate.buys_1h,
        sells_1h=aggregate.sells_1h,
        price_change_5m_pct=aggregate.price_change_5m_pct,
        price_change_1h_pct=aggregate.price_change_1h_pct,
        price_change_6h_pct=aggregate.price_change_6h_pct,
        price_change_24h_pct=aggregate.price_change_24h_pct,
        pair_count=aggregate.pair_count,
        boosts_active=aggregate.boosts_active,
        boost_amount=discovery.get("boost_amount"),
        boost_total=discovery.get("boost_total"),
        declared_telegram=_pick("declared_telegram"),
        declared_x=_pick("declared_x"),
        declared_website=_pick("declared_website"),
        telegram_url=_pick("telegram_url"),
        x_url=_pick("x_url"),
        entry_path=discovery.get("discovery"),
        listings=["dex"],
        raw={"dex_ids": list(aggregate.dex_ids), "discovery": discovery.get("discovery")},
    )


# --- Feed --------------------------------------------------------------------


def _batched(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


SEED_TOKENS_ENV = "SCREENER_SEED_TOKENS"


def seed_contracts_from_env(raw: str | None = None) -> tuple[str, ...]:
    """Token addresses from ``SCREENER_SEED_TOKENS``, comma separated.

    Configuration, like ``SCREENER_CHAIN_IDS``, and for the neighbouring reason:
    the operator knows a token exists on a chain the discovery endpoints cannot
    see, and saying so is an assertion they are entitled to make. Order is
    preserved and duplicates are dropped; whitespace and empty entries are
    skipped rather than turned into a request for the empty address.
    """
    import os

    text = raw if raw is not None else os.environ.get(SEED_TOKENS_ENV, "")
    out: list[str] = []
    for item in text.split(","):
        address = item.strip()
        if address and address not in out:
            out.append(address)
    return tuple(out)


@dataclass
class DexScreenerFeed:
    """Polls DexScreener and yields one :class:`TokenMetrics` per candidate token.

    Discovery is the boosted and profiled token lists, not "every new pool" --
    DexScreener has no new-pool firehose on the free tier. That is a **sampling
    bias and it is a real one**: a token appears here because someone paid to boost
    it or filled in its profile. It is recorded on every row (``discovery``) and
    stated on the web page, because a bias you can name is a covariate and a bias
    you cannot is a confound.

    The trigger still decides entry, and it still cannot see any of this.
    """

    client: DexScreenerClient
    chain_names: tuple[str, ...] = ("solana",)
    source_name: str = SOURCE_NAME
    max_tokens_per_poll: int = 120
    include_profiles: bool = True
    # Token addresses to poll regardless of whether anyone boosted or profiled them.
    #
    # Discovery is boosted and profiled tokens, which is a paid-for sample: a chain
    # can be live, trading, and produce an empty poll forever because nobody has
    # spent anything on a token there. Binding such a chain's id and collecting
    # nothing looks identical to a quiet chain -- the exact failure
    # collectors/chains.py refuses to guess its way into.
    #
    # A seed is an operator saying "this token exists, look at it". It does not
    # bypass the trigger: a seeded token is observed on identical terms and enters
    # the dataset only if it crosses, like every other token. What it does change
    # is the *sample*, so every row records how it arrived (`entry_path`), and a
    # seeded token counts in the mindshare universe it was polled with.
    seed_contracts: tuple[str, ...] = ()

    @classmethod
    def for_chains(
        cls, chain_names: Sequence[str], client: DexScreenerClient | None = None, **kwargs: Any
    ) -> DexScreenerFeed:
        resolved = chains.resolve_requested(list(chain_names))
        kwargs.setdefault("seed_contracts", seed_contracts_from_env())
        return cls(
            client=client or DexScreenerClient(),
            chain_names=tuple(resolved),
            **kwargs,
        )

    def discover(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Candidate tokens on the requested chains, with their discovery metadata."""
        found: dict[tuple[str, str], dict[str, Any]] = {}
        wanted = set(self.chain_names)

        sources: list[tuple[str, Any]] = [
            ("boost_top", self.client.token_boosts_top),
            ("boost_latest", self.client.token_boosts_latest),
        ]
        if self.include_profiles:
            sources.append(("profile", self.client.token_profiles))

        for kind, fetch in sources:
            try:
                entries = parse_discovery(fetch(), kind=kind)
            except DexScreenerError:
                # One discovery endpoint being down must not lose the other two.
                # A short poll delays a snapshot; a crashed poll skips one.
                log.exception("discovery source %s failed", kind)
                continue
            for entry in entries:
                if entry["chain"] not in wanted:
                    continue
                key = (entry["chain"], entry["contract"])
                existing = found.get(key)
                if existing is None:
                    found[key] = entry
                else:
                    # Same token from two lists: keep the larger boost figures and
                    # any socials the other list did not have.
                    for numeric in ("boost_amount", "boost_total"):
                        a, b = existing.get(numeric), entry.get(numeric)
                        if b is not None and (a is None or b > a):
                            existing[numeric] = b
                    for flag in ("declared_telegram", "declared_x", "declared_website"):
                        if existing.get(flag) is None:
                            existing[flag] = entry.get(flag)

        # Seeds last, and they never overwrite a discovery entry: a token that was
        # both boosted and seeded arrived by the boost, and its boost figures are
        # real mindshare inputs that a seed placeholder would erase. The chain is
        # left unresolved here because a seed is an address with no chainId
        # attached -- poll() fills it in from what the API actually returns.
        for contract in self.seed_contracts:
            if not any(key[1] == contract for key in found):
                found[(_SEED_CHAIN, contract)] = {
                    "chain": _SEED_CHAIN,
                    "contract": contract,
                    "discovery": "seed",
                }
        return found

    def poll(self) -> list[TokenMetrics]:
        discovered = self.discover()
        if not discovered:
            return []

        # Deterministic order, then cap. Sorting by key rather than by boost size
        # keeps the cap from preferring loud tokens, which would bias the sample
        # on the very axis mindshare measures.
        keys = sorted(discovered)[: self.max_tokens_per_poll]
        observed = now_ms()

        aggregates: dict[tuple[str, str], PairAggregate] = {}
        for batch in _batched([contract for _, contract in keys], MAX_ADDRESSES_PER_REQUEST):
            try:
                aggregates.update(parse_pairs(self.client.pairs_for_tokens(batch)))
            except DexScreenerError:
                log.exception("pair lookup failed for a batch of %d", len(batch))

        # Seeds arrive as an address with no chain. The response carries the chain,
        # so it is matched by contract and the API's answer is used -- the one place
        # a key is completed rather than looked up.
        by_contract: dict[str, tuple[str, PairAggregate]] = {
            contract: (chain, aggregate)
            for (chain, contract), aggregate in aggregates.items()
        }
        wanted = set(self.chain_names)

        out: list[TokenMetrics] = []
        for chain, contract in keys:
            if chain == _SEED_CHAIN:
                resolved = by_contract.get(contract)
                if resolved is None:
                    continue
                chain, aggregate = resolved
                if chain not in wanted:
                    # A seeded address that turned out to live on a chain this run
                    # did not ask for. Dropped rather than collected: seeding must
                    # not widen the sample's chain set by a side effect, or the
                    # chain filter stops describing what was polled.
                    log.info(
                        "seeded token %s is on %s, which was not requested", contract, chain
                    )
                    continue
            else:
                aggregate = aggregates.get((chain, contract))  # type: ignore[assignment]
                if aggregate is None:
                    # Discovered but not yet pooled, or the lookup failed. Nothing
                    # to snapshot: with no market data the trigger cannot fire.
                    continue
            out.append(
                to_metrics(
                    aggregate,
                    observed_at_ms=observed,
                    discovery=discovered[(_SEED_CHAIN, contract)]
                    if (_SEED_CHAIN, contract) in discovered
                    else discovered[(chain, contract)],
                )
            )
        return out


@dataclass
class DexScreenerPriceSource:
    """Re-prices snapshotted tokens for ``collectors/outcomes.py``.

    Same client, same parsers, batched 30 addresses at a time. Returns
    :class:`TokenMetrics`; the tracker turns them into price observations.
    """

    client: DexScreenerClient
    source_name: str = SOURCE_NAME

    def fetch(self, tokens: Sequence[tuple[str, str]]) -> dict[tuple[str, str], TokenMetrics]:
        """``{(chain, contract): metrics}`` for the tokens that came back.

        A token missing from the response is simply absent from the result. It is
        not returned with zeros -- a delisted or dead pool is unknown price, and
        writing a zero market cap would manufacture a total loss the moment
        DexScreener has an outage.
        """
        observed = now_ms()
        out: dict[tuple[str, str], TokenMetrics] = {}
        by_chain: dict[str, list[str]] = {}
        for chain, contract in tokens:
            by_chain.setdefault(chain, []).append(contract)

        for chain, contracts in by_chain.items():
            for batch in _batched(contracts, MAX_ADDRESSES_PER_REQUEST):
                try:
                    aggregates = parse_pairs(self.client.pairs_for_tokens(batch))
                except DexScreenerError:
                    log.exception("re-price failed for %d tokens on %s", len(batch), chain)
                    continue
                for key, aggregate in aggregates.items():
                    if key[0] != chain:
                        continue
                    out[key] = to_metrics(aggregate, observed_at_ms=observed)
        return out


# Broad queries used to widen chain discovery beyond the boost endpoints. Quote
# assets and stablecoins, because whatever else a chain has, it has a pool against
# one of these. Deliberately not chain names: searching "robinhood" would return
# tokens *called* Robinhood on every other chain, which is the kind of near-miss
# that reads as a discovery.
DISCOVERY_QUERIES: tuple[str, ...] = ("USDC", "USDT", "WETH", "WBTC")


def discover_chain_ids(
    client: DexScreenerClient, *, queries: Sequence[str] | None = DISCOVERY_QUERIES
) -> dict[str, dict[str, Any]]:
    """Every ``chainId`` the API actually returns, with counts and how it was seen.

    The answer to "what is Robinhood Chain's DexScreener id" is not something this
    repo can hardcode honestly -- see ``collectors/chains.py``. This asks the API
    instead, and reports what came back with each id flagged as registered or not,
    so an unregistered id can be bound with ``SCREENER_CHAIN_IDS`` without a code
    change.

    Two sources, because one was not enough. The boost and profile endpoints only
    ever return chains that have a *boosted or profiled token right now*, which is
    a small and paid-for sample: a chain can be live, trading and entirely absent
    from it. The search endpoint answers about anything that trades, so sweeping a
    handful of quote assets surfaces chains the discovery endpoints never will.
    Which source saw an id is reported per id rather than pooled, because "seen
    only in search" and "seen in boosts" mean different things about the chain.
    """
    seen: dict[str, int] = {}
    via: dict[str, set[str]] = {}

    def note(raw_id: Any, source: str) -> None:
        if not raw_id:
            return
        raw = str(raw_id)
        seen[raw] = seen.get(raw, 0) + 1
        via.setdefault(raw, set()).add(source)

    for fetch in (
        client.token_boosts_top,
        client.token_boosts_latest,
        client.token_profiles,
    ):
        try:
            entries = fetch()
        except DexScreenerError:
            log.exception("discovery endpoint failed during chain discovery")
            continue
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                note(entry.get("chainId"), "discovery")

    for query in queries or ():
        try:
            pairs = client.search(query)
        except DexScreenerError:
            log.exception("search failed during chain discovery (q=%s)", query)
            continue
        for pair in pairs if isinstance(pairs, list) else []:
            if isinstance(pair, dict):
                note(pair.get("chainId"), "search")

    out: dict[str, dict[str, Any]] = {}
    for raw, count in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])):
        name = chains.canonical(raw)
        entry = chains.get(raw)
        out[raw] = {
            "tokens_seen": count,
            "seen_via": sorted(via.get(raw, ())),
            "canonical_name": name,
            "registered": bool(entry and entry.known),
            "bound_to_a_source": bool(entry and entry.has_dexscreener_source),
        }
    return out


def resolve_token_chain(client: DexScreenerClient, address: str) -> dict[str, Any]:
    """What chain is this token on? Ask the endpoint that does not need to be told.

    ``/latest/dex/tokens/{address}`` takes an address and **no chainId**, and every
    pair it returns carries the ``chainId`` DexScreener files it under. So a single
    token address somebody already has is enough to learn a chain's id -- which is
    the missing step for a chain nobody here can otherwise name.

    That matters most for exactly the chain that has the problem. ``--discover-chains``
    asks the boost, profile and search endpoints, and a chain can be live and absent
    from all of them; a token address is direct evidence, and the id comes back from
    the API rather than from anyone's memory of a URL.

    Reports the id **raw**, beside the canonical name it maps to today. Those differ
    precisely when the chain is not bound yet, which is the case this exists for.
    """
    try:
        payload = client.pairs_for_tokens([address])
    except DexScreenerError as exc:
        return {"address": address, "error": str(exc), "chain_ids": []}

    # Both envelope shapes, exactly as parse_pairs accepts them. The real client
    # unwraps `{"pairs": [...]}` to a list, but this reads the raw chainId rather
    # than the canonical name -- the whole point -- so it cannot reuse parse_pairs
    # for that half, and must not disagree with it about what a payload is.
    if isinstance(payload, dict):
        raw_pairs = payload.get("pairs") or []
    elif isinstance(payload, list):
        raw_pairs = payload
    else:
        raw_pairs = []

    raw_ids: dict[str, int] = {}
    for pair in raw_pairs:
        if isinstance(pair, dict) and pair.get("chainId"):
            raw = str(pair["chainId"])
            raw_ids[raw] = raw_ids.get(raw, 0) + 1

    aggregates = parse_pairs(payload)
    tokens = [
        {
            "chain": chain,
            "ticker": aggregate.ticker,
            "mcap_usd": aggregate.mcap_usd,
            "liquidity_usd": aggregate.liquidity_usd,
            "pairs": aggregate.pair_count,
        }
        for (chain, _), aggregate in aggregates.items()
    ]

    unbound = [
        raw for raw in raw_ids if chains.dexscreener_id(chains.canonical(raw)) != raw
    ]
    return {
        "address": address,
        "chain_ids": sorted(raw_ids),
        "pairs_by_chain_id": raw_ids,
        "tokens": tokens,
        "not_bound": unbound,
        "conclusion": (
            f"No pool came back for {address}. Either the address is wrong, or "
            "DexScreener has not indexed it."
            if not raw_ids
            else (
                "DexScreener files this token under chainId "
                + ", ".join(repr(r) for r in sorted(raw_ids))
                + ". "
                + (
                    f'Bind it: {chains.CHAIN_ID_ENV}="<chain>={sorted(unbound)[0]}".'
                    if unbound
                    else "It is already bound in collectors/chains.py."
                )
            )
        ),
    }


def verify_chain_id(
    client: DexScreenerClient, candidate: str, *, queries: Sequence[str] | None = None
) -> dict[str, Any]:
    """Ask the API whether one candidate ``chainId`` is real, and show the evidence.

    This is the other half of discovery and the half that matters for a chain the
    discovery endpoints cannot see. A human can read a ``chainId`` straight out of
    a DexScreener URL -- ``dexscreener.com/<chainId>/<pair>`` -- but a string read
    off a page is a hypothesis, and binding a wrong one produces the single worst
    failure available here: requests that match nothing, forever, looking exactly
    like a quiet chain.

    So the candidate is tested rather than trusted. ``pairs`` is how many pools
    came back carrying that id and ``sample`` is what they were; zero of both means
    nothing was found *by these queries*, which is not the same as the id being
    wrong, and the returned ``conclusion`` says so in those words.
    """
    found: list[dict[str, Any]] = []
    errors: list[str] = []
    for query in queries or DISCOVERY_QUERIES:
        try:
            pairs = client.search(query)
        except DexScreenerError as exc:
            errors.append(f"{query}: {exc}")
            continue
        for pair in pairs if isinstance(pairs, list) else []:
            if isinstance(pair, dict) and str(pair.get("chainId")) == candidate:
                found.append(
                    {
                        "ticker": _get(pair, "baseToken", "symbol"),
                        "contract": _get(pair, "baseToken", "address"),
                        "dex": pair.get("dexId"),
                        "liquidity_usd": _as_float(_get(pair, "liquidity", "usd")),
                    }
                )
    canonical_name = chains.canonical(candidate)
    return {
        "candidate": candidate,
        "pairs": len(found),
        "sample": found[:5],
        "queries": list(queries or DISCOVERY_QUERIES),
        "query_errors": errors,
        "canonical_name": canonical_name,
        "already_bound": chains.dexscreener_id(canonical_name) == candidate,
        "conclusion": (
            f"DexScreener returns pools on {candidate!r}. Bind it with "
            f'{chains.CHAIN_ID_ENV}="<chain>={candidate}".'
            if found
            else (
                f"No pool carrying {candidate!r} came back from these queries. That is "
                "not proof the id is wrong -- the chain may simply have no pool "
                "matching them -- so try other queries before concluding anything."
            )
        ),
    }


def merge_discovery(metrics: TokenMetrics, discovery: dict[str, Any]) -> TokenMetrics:
    """Layer discovery metadata onto a metrics record without overwriting a measurement."""
    updates: dict[str, Any] = {}
    for numeric in ("boost_amount", "boost_total"):
        if getattr(metrics, numeric) is None and discovery.get(numeric) is not None:
            updates[numeric] = discovery[numeric]
    for flag in ("declared_telegram", "declared_x", "declared_website"):
        if getattr(metrics, flag) is None and discovery.get(flag) is not None:
            updates[flag] = discovery[flag]
    return replace(metrics, **updates) if updates else metrics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dexscreener",
        description=(
            "Probe the DexScreener endpoints and report what came back. Needs no API "
            "key. Reads only; this command cannot write anywhere."
        ),
    )
    parser.add_argument("--probe", action="store_true", help="run each endpoint once")
    parser.add_argument(
        "--discover-chains",
        action="store_true",
        help=(
            "list every chainId the discovery endpoints return, flagged registered "
            "or not. Use it to find an id for a chain this repo does not yet bind "
            f"(then set {chains.CHAIN_ID_ENV}=\"name=<id>\")."
        ),
    )
    parser.add_argument(
        "--resolve-token",
        metavar="ADDRESS",
        help=(
            "ask which chain a token address is on. /latest/dex/tokens takes no "
            "chainId and returns one, so a single address you already have is "
            "enough to learn a chain's id -- including a chain that never shows up "
            "in --discover-chains because nobody has boosted a token on it."
        ),
    )
    parser.add_argument(
        "--verify-chain-id",
        metavar="CHAIN_ID",
        help=(
            "test one candidate chainId against the live API and show the pools it "
            "found. Read the candidate out of a DexScreener URL "
            "(dexscreener.com/<chainId>/<pair>); this says whether it is real "
            "before you bind it."
        ),
    )
    parser.add_argument(
        "--chains",
        default=",".join(chains.default_chain_names()),
        help=f"comma-separated (supported: {', '.join(chains.supported_names())})",
    )
    parser.add_argument("--limit", type=int, default=5, help="tokens to show")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if args.resolve_token:
        print(json.dumps(resolve_token_chain(DexScreenerClient(), args.resolve_token), indent=2))
        return 0

    if args.verify_chain_id:
        try:
            print(json.dumps(verify_chain_id(DexScreenerClient(), args.verify_chain_id), indent=2))
        except DexScreenerError as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 1
        return 0

    if args.discover_chains:
        try:
            found = discover_chain_ids(DexScreenerClient())
        except DexScreenerError as exc:
            print(json.dumps({"error": str(exc)}, indent=2))
            return 1
        unbound = [
            chain_id for chain_id, info in found.items() if not info["bound_to_a_source"]
        ]
        print(
            json.dumps(
                {
                    "chain_ids_seen": found,
                    "not_bound_to_a_source": unbound,
                    "hint": (
                        f'{chains.CHAIN_ID_ENV}="robinhood=<id>" binds one without a '
                        "code change, and a bound chain is collected by default. Ids "
                        'seen only via "search" are still real; ids seen via '
                        '"discovery" additionally had a boosted or profiled token at '
                        "this moment. A chain absent from both may still exist on "
                        "DexScreener -- check a candidate with --verify-chain-id."
                    ),
                },
                indent=2,
            )
        )
        return 0

    if not args.probe:
        parser.print_help()
        return 0

    try:
        feed = DexScreenerFeed.for_chains(args.chains.split(","))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 2

    report: dict[str, Any] = {"chains": list(feed.chain_names)}
    try:
        discovered = feed.discover()
        report["discovered"] = len(discovered)
        report["by_chain"] = {
            chain: sum(1 for c, _ in discovered if c == chain) for chain in feed.chain_names
        }
        metrics = feed.poll()
        report["with_market_data"] = len(metrics)
        report["sample"] = [
            {
                "chain": m.chain,
                "ticker": m.ticker,
                "contract": m.contract,
                "mcap_usd": m.mcap_usd,
                "fdv_usd": m.fdv_usd,
                "liquidity_usd": m.liquidity_usd,
                "volume_24h_usd": m.volume_24h_usd,
                "txns_24h": m.txns_24h,
                "age_minutes": m.age_minutes(),
            }
            for m in metrics[: args.limit]
        ]
    except DexScreenerError as exc:
        report["error"] = str(exc)
        print(json.dumps(report, indent=2))
        return 1

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
