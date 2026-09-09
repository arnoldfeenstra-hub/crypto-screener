"""Bitquery v2 source for Solana (BUILD_BRIEF.md section 1, "On-chain, live + historical").

Split in two deliberately:

* **Network layer** -- :class:`BitqueryClient`, a thin GraphQL POST with retry. It
  holds the only credential in the process and does nothing else.
* **Parse layer** -- ``parse_*`` functions, pure and total. They take a decoded
  response and return :class:`~collectors.metrics.TokenMetrics`, tolerating every
  field being absent. These are what the tests exercise, against fixtures in
  ``fixtures/``.

The split matters because of what can and cannot be verified without a paid key.
The parsers are verified now, offline, by tests. **The GraphQL query text below is
not.** It is written against Bitquery's Solana EAP schema, but schemas drift and
the EAP one drifts faster than most, so treat the three query constants as a
starting point to check against the live endpoint on first run, not as known-good.
``python -m collectors.bitquery --probe`` runs each one and reports what came back.

Nothing here writes anywhere. The client has no mutation, and hard rule 4 means it
never will: this repo holds no exchange keys and places no orders.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from collectors.config import load_config
from collectors.metrics import TokenMetrics
from collectors.schema import now_ms, to_ms

log = logging.getLogger("bitquery")

DEFAULT_ENDPOINT = "https://streaming.bitquery.io/eap"

# --- Queries ----------------------------------------------------------------
# Unverified against a live endpoint; see the module docstring.

NEW_POOLS_QUERY = """
query NewPools($since: DateTime!, $limit: Int!) {
  Solana {
    DEXPools(
      where: {Block: {Time: {since: $since}}, Pool: {Base: {PostAmount: {gt: "0"}}}}
      orderBy: {descending: Block_Time}
      limit: {count: $limit}
    ) {
      Block { Time }
      Pool {
        Dex { ProtocolName ProtocolFamily }
        Market {
          MarketAddress
          BaseCurrency { MintAddress Symbol Name Uri }
          QuoteCurrency { MintAddress Symbol }
        }
        Base { PostAmount }
        Quote { PostAmount PostAmountInUSD }
      }
    }
  }
}
"""

TOKEN_METRICS_QUERY = """
query TokenMetrics($mints: [String!], $since24h: DateTime!) {
  Solana {
    DEXTradeByTokens(
      where: {Trade: {Currency: {MintAddress: {in: $mints}}},
              Block: {Time: {since: $since24h}}}
      orderBy: {descendingByField: "volume_24h_usd"}
      limit: {count: 500}
    ) {
      Trade { Currency { MintAddress Symbol Name Decimals } PriceInUSD }
      volume_24h_usd: sum(of: Trade_Side_AmountInUSD)
      trades: count
    }
    TokenSupplyUpdates(
      where: {TokenSupplyUpdate: {Currency: {MintAddress: {in: $mints}}}}
      orderBy: {descending: Block_Time}
      limitBy: {by: TokenSupplyUpdate_Currency_MintAddress, count: 1}
    ) {
      TokenSupplyUpdate {
        Marketcap
        PostBalanceInUSD
        Currency {
          MintAddress
          Symbol
          Name
          MintAuthority
          FreezeAuthority
          Uri
        }
      }
    }
  }
}
"""

HOLDER_COUNT_QUERY = """
query HolderCounts($mints: [String!]) {
  Solana {
    BalanceUpdates(
      where: {BalanceUpdate: {Currency: {MintAddress: {in: $mints}}}}
      limit: {count: 100000}
    ) {
      BalanceUpdate { Currency { MintAddress } }
      holders: count(distinct: BalanceUpdate_Account_Owner)
    }
  }
}
"""

class BitqueryError(RuntimeError):
    """A Bitquery request failed, or came back with GraphQL errors."""


@dataclass
class BitqueryClient:
    """Thin GraphQL client. Holds the token; does nothing but read."""

    token: str
    endpoint: str = DEFAULT_ENDPOINT
    timeout: float = 30.0
    max_retries: int = 3
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        self.session = self.session or requests.Session()

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                assert self.session is not None
                response = self.session.post(
                    self.endpoint, json=payload, headers=headers, timeout=self.timeout
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    raise BitqueryError(f"HTTP {response.status_code}: {response.text[:200]}")
                response.raise_for_status()
                body = response.json()
                if body.get("errors"):
                    # A GraphQL error is a query bug, not a transient fault. Retrying
                    # an invalid query just burns quota.
                    raise BitqueryError(f"GraphQL errors: {json.dumps(body['errors'])[:500]}")
                return body.get("data") or {}
            except BitqueryError as exc:
                if "GraphQL errors" in str(exc):
                    raise
                last_error = exc
            except requests.RequestException as exc:
                last_error = exc
            backoff = min(2**attempt, 30)
            log.warning("bitquery attempt %d/%d failed: %s", attempt, self.max_retries, last_error)
            if attempt < self.max_retries:
                time.sleep(backoff)
        raise BitqueryError(f"bitquery failed after {self.max_retries} attempts: {last_error}")


# --- Parsers (pure, total) ---------------------------------------------------


def _get(obj: Any, *path: str) -> Any:
    """Walk a nested dict, returning ``None`` the moment anything is missing."""
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_float(value: Any) -> float | None:
    """Parse a number that upstream may send as a string. Junk becomes ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _declared_socials(uri: Any) -> tuple[bool | None, bool | None, bool | None]:
    """Read the socials triple off a metadata URI string.

    Unknown stays unknown. An absent URI means we did not look, not that the token
    declared nothing -- and since this is the best-evidenced feature in the schema
    (17.4x graduation lift with all three), guessing ``False`` here would corrupt
    the one column most likely to carry real signal.
    """
    if not isinstance(uri, str) or not uri:
        return None, None, None
    lowered = uri.lower()
    return (
        "t.me" in lowered or "telegram" in lowered,
        "twitter.com" in lowered or "x.com" in lowered,
        "http" in lowered,
    )


def parse_new_pools(data: dict[str, Any]) -> list[TokenMetrics]:
    """Turn a ``NewPools`` response into one record per newly-pooled token."""
    pools = _get(data, "Solana", "DEXPools") or []
    observed = now_ms()
    out: list[TokenMetrics] = []
    for entry in pools:
        base = _get(entry, "Pool", "Market", "BaseCurrency") or {}
        mint = base.get("MintAddress")
        if not mint:
            continue
        telegram, x_declared, website = _declared_socials(base.get("Uri"))
        quote_usd = _as_float(_get(entry, "Pool", "Quote", "PostAmountInUSD"))
        out.append(
            TokenMetrics(
                chain="solana",
                contract=mint,
                observed_at_ms=observed,
                source="bitquery",
                ticker=base.get("Symbol"),
                name=base.get("Name"),
                first_seen_at_ms=to_ms(_get(entry, "Block", "Time")),
                # Both sides of a pool are liquidity; the quote side in USD is the
                # part that can actually absorb a sell.
                liquidity_usd=quote_usd,
                declared_telegram=telegram,
                declared_x=x_declared,
                declared_website=website,
                listings=["dex"],
                raw={"pool": entry},
            )
        )
    return out


def parse_token_metrics(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collapse a ``TokenMetrics`` response into ``{mint: fields}``."""
    by_mint: dict[str, dict[str, Any]] = {}

    for entry in _get(data, "Solana", "DEXTradeByTokens") or []:
        currency = _get(entry, "Trade", "Currency") or {}
        mint = currency.get("MintAddress")
        if not mint:
            continue
        fields = by_mint.setdefault(mint, {})
        fields["price_usd"] = _as_float(_get(entry, "Trade", "PriceInUSD"))
        fields["volume_24h_usd"] = _as_float(entry.get("volume_24h_usd"))
        fields["ticker"] = currency.get("Symbol") or fields.get("ticker")
        fields["name"] = currency.get("Name") or fields.get("name")

    for entry in _get(data, "Solana", "TokenSupplyUpdates") or []:
        update = entry.get("TokenSupplyUpdate") or {}
        currency = update.get("Currency") or {}
        mint = currency.get("MintAddress")
        if not mint:
            continue
        fields = by_mint.setdefault(mint, {})
        fields["mcap_usd"] = _as_float(update.get("Marketcap"))
        # Solana revokes an authority by setting it to null. Absent key means the
        # source did not report it, which is not the same as revoked.
        if "MintAuthority" in currency:
            fields["mint_revoked"] = currency.get("MintAuthority") in (None, "")
        if "FreezeAuthority" in currency:
            fields["freeze_active"] = currency.get("FreezeAuthority") not in (None, "")
        telegram, x_declared, website = _declared_socials(currency.get("Uri"))
        if telegram is not None:
            fields["declared_telegram"] = telegram
            fields["declared_x"] = x_declared
            fields["declared_website"] = website
        fields.setdefault("ticker", currency.get("Symbol"))
        fields.setdefault("name", currency.get("Name"))

    return by_mint


def parse_holder_counts(data: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in _get(data, "Solana", "BalanceUpdates") or []:
        mint = _get(entry, "BalanceUpdate", "Currency", "MintAddress")
        holders = _as_int(entry.get("holders"))
        if mint and holders is not None:
            counts[mint] = holders
    return counts


def merge_metrics(
    base: TokenMetrics,
    fields: dict[str, Any] | None,
    holder_count: int | None,
) -> TokenMetrics:
    """Layer per-token metrics onto a pool record without inventing anything.

    A field the metrics query did not return leaves the base value untouched, and
    the base value is itself ``None`` unless the pool query supplied it.
    """
    fields = fields or {}
    from dataclasses import replace

    return replace(
        base,
        ticker=fields.get("ticker") or base.ticker,
        name=fields.get("name") or base.name,
        mcap_usd=fields.get("mcap_usd", base.mcap_usd),
        price_usd=fields.get("price_usd", base.price_usd),
        volume_24h_usd=fields.get("volume_24h_usd", base.volume_24h_usd),
        holder_count=holder_count if holder_count is not None else base.holder_count,
        mint_revoked=fields.get("mint_revoked", base.mint_revoked),
        freeze_active=fields.get("freeze_active", base.freeze_active),
        declared_telegram=fields.get("declared_telegram", base.declared_telegram),
        declared_x=fields.get("declared_x", base.declared_x),
        declared_website=fields.get("declared_website", base.declared_website),
    )


# --- Feeds -------------------------------------------------------------------


@dataclass
class BitqueryFeed:
    """Polls Bitquery and yields one :class:`TokenMetrics` per candidate token."""

    client: BitqueryClient
    chain: str = "solana"
    lookback_minutes: int = 180
    pool_limit: int = 500
    source_name: str = "bitquery"

    @property
    def chain_names(self) -> tuple[str, ...]:
        """What this feed actually covers. One chain; the watcher reports it as-is."""
        return (self.chain,)

    def poll(self) -> list[TokenMetrics]:
        if self.chain != "solana":
            raise NotImplementedError(
                f"chain {self.chain!r} has no source yet; Phase 0 targets Solana first "
                "(BUILD_BRIEF.md section 6). BNB is 'same shape' per section 1 but is a "
                "separate query set and is not written."
            )
        since = _iso_since(self.lookback_minutes)
        pools = parse_new_pools(
            self.client.execute(NEW_POOLS_QUERY, {"since": since, "limit": self.pool_limit})
        )
        if not pools:
            return []

        mints = [p.contract for p in pools]
        metrics_by_mint = parse_token_metrics(
            self.client.execute(
                TOKEN_METRICS_QUERY, {"mints": mints, "since24h": _iso_since(24 * 60)}
            )
        )
        holders_by_mint = parse_holder_counts(
            self.client.execute(HOLDER_COUNT_QUERY, {"mints": mints})
        )
        return [
            merge_metrics(
                pool,
                metrics_by_mint.get(pool.contract),
                holders_by_mint.get(pool.contract),
            )
            for pool in pools
        ]


@dataclass
class ReplayFeed:
    """Replays recorded observations. No network, no credential.

    Lets the whole trigger path be exercised end to end before anyone pays for an
    API key, and gives the trigger tests a realistic input shape.
    """

    batches: list[list[TokenMetrics]]
    source_name: str = "replay"
    chain_names: tuple[str, ...] = ("solana",)
    _cursor: int = 0

    @classmethod
    def from_path(cls, path: Path, chain: str = "solana") -> ReplayFeed:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_batches = payload if isinstance(payload, list) else payload.get("batches", [])
        if raw_batches and isinstance(raw_batches[0], dict):
            raw_batches = [raw_batches]  # a flat list is a single batch
        batches = [
            [_metrics_from_dict(item, chain) for item in batch] for batch in raw_batches
        ]
        # The fixture is replayed onto one chain, so the watcher's summary reports
        # that one rather than whatever --chains happened to say.
        return cls(
            batches=batches,
            source_name=f"replay:{Path(path).name}",
            chain_names=(chain,),
        )

    def poll(self) -> list[TokenMetrics]:
        if self._cursor >= len(self.batches):
            return []
        batch = self.batches[self._cursor]
        self._cursor += 1
        return batch


def _metrics_from_dict(item: dict[str, Any], chain: str) -> TokenMetrics:
    known = {f for f in TokenMetrics.__dataclass_fields__ if f != "raw"}
    kwargs = {k: v for k, v in item.items() if k in known}
    kwargs.setdefault("chain", chain)
    kwargs.setdefault("source", "replay")
    kwargs.setdefault("observed_at_ms", now_ms())
    return TokenMetrics(**kwargs, raw={"replay": item})


def _iso_since(minutes: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bitquery",
        description="Probe the Bitquery queries against the live endpoint and report shapes.",
    )
    parser.add_argument("--probe", action="store_true", help="run each query once")
    parser.add_argument("--lookback-minutes", type=int, default=180)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    config = load_config()
    client = BitqueryClient(token=config.require_bitquery(), endpoint=config.bitquery_endpoint)

    if not args.probe:
        parser.print_help()
        return 0

    report: dict[str, Any] = {}
    since = _iso_since(args.lookback_minutes)
    try:
        pools_raw = client.execute(NEW_POOLS_QUERY, {"since": since, "limit": args.limit})
        pools = parse_new_pools(pools_raw)
        report["new_pools"] = {"parsed": len(pools), "sample": [p.contract for p in pools[:3]]}
        if pools:
            mints = [p.contract for p in pools]
            metrics_raw = client.execute(
                TOKEN_METRICS_QUERY, {"mints": mints, "since24h": _iso_since(1440)}
            )
            report["token_metrics"] = {"parsed": len(parse_token_metrics(metrics_raw))}
            holders_raw = client.execute(HOLDER_COUNT_QUERY, {"mints": mints})
            report["holder_counts"] = {"parsed": len(parse_holder_counts(holders_raw))}
    except BitqueryError as exc:
        report["error"] = str(exc)
        print(json.dumps(report, indent=2))
        return 1

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
