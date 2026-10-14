"""Read-only Hyperliquid market data: the info endpoint, two request types, no keys.

Why this is not ``collectors/httpjson.py``
------------------------------------------
``httpjson`` is GET-only on purpose: a client that cannot send a body cannot be
quietly turned into one that places orders. Hyperliquid's public market data is
only served by ``POST /info`` with a JSON body naming the query, so a GET client
cannot read it at all.

This module keeps the same guarantee a different way. The URL is a constant that
ends in ``/info`` -- the read-only endpoint; orders go to ``/exchange``, which this
module never names. The body is built here from an allowlist of two read-only
query types, never passed through from a caller, and nothing is signed: there is
no key, no nonce, no wallet. ``tests/test_rotation.py`` greps this file to hold all
of that in place.

Stdlib only, because ``api/carpet.py`` runs as a Vercel function with nothing
installed.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("hyperliquid")

INFO_URL = "https://api.hyperliquid.xyz/info"
SOURCE_NAME = "hyperliquid"
USER_AGENT = "crypto-screener/0.1 (carpet; read-only)"

# The only queries this module can send. Both are public market data.
ALLOWED_QUERY_TYPES = frozenset({"metaAndAssetCtxs", "candleSnapshot"})

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HyperliquidError(RuntimeError):
    """A read failed after the retries it was entitled to."""


def _num(value: Any) -> float | None:
    """Hyperliquid sends numbers as strings. Unparseable or NaN is missing, not zero."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


@dataclass(frozen=True)
class Candle:
    t: int  # open time, epoch ms
    o: float | None
    h: float | None
    l: float | None  # noqa: E741 - the exchange's own field name
    c: float | None
    v: float | None  # volume in base units

    @property
    def notional(self) -> float | None:
        """Money traded in the bar, approximated as base volume x typical price."""
        if self.v is None or self.h is None or self.l is None or self.c is None:
            return None
        return self.v * (self.h + self.l + self.c) / 3.0


@dataclass(frozen=True)
class AssetContext:
    coin: str
    mark_px: float | None
    prev_day_px: float | None
    day_ntl_vlm: float | None  # 24h notional volume, USD
    open_interest: float | None  # base units
    funding: float | None  # hourly rate
    impact_bid: float | None
    impact_ask: float | None

    @property
    def open_interest_usd(self) -> float | None:
        if self.open_interest is None or self.mark_px is None:
            return None
        return self.open_interest * self.mark_px

    @property
    def impact_spread_bps(self) -> float | None:
        """Cost of crossing the book for the exchange's impact notional, in bps."""
        if self.impact_bid is None or self.impact_ask is None or not self.mark_px:
            return None
        return (self.impact_ask - self.impact_bid) / self.mark_px * 10_000


def parse_candles(raw: Any) -> list[Candle]:
    candles: list[Candle] = []
    for row in raw or []:
        if not isinstance(row, dict) or row.get("t") is None:
            continue
        candles.append(
            Candle(
                t=int(row["t"]),
                o=_num(row.get("o")),
                h=_num(row.get("h")),
                l=_num(row.get("l")),
                c=_num(row.get("c")),
                v=_num(row.get("v")),
            )
        )
    candles.sort(key=lambda candle: candle.t)
    return candles


def parse_meta_and_ctxs(raw: Any) -> dict[str, AssetContext]:
    """``[meta, ctxs]`` -> contexts by coin. The two lists are aligned by index."""
    if not isinstance(raw, list) or len(raw) != 2:
        raise HyperliquidError("metaAndAssetCtxs: expected [meta, assetCtxs]")
    meta, ctxs = raw
    universe = (meta or {}).get("universe") or []
    out: dict[str, AssetContext] = {}
    for asset, ctx in zip(universe, ctxs or [], strict=False):
        name = (asset or {}).get("name")
        if not name or (asset or {}).get("isDelisted"):
            continue
        ctx = ctx or {}
        impact = ctx.get("impactPxs") or [None, None]
        out[name] = AssetContext(
            coin=name,
            mark_px=_num(ctx.get("markPx")),
            prev_day_px=_num(ctx.get("prevDayPx")),
            day_ntl_vlm=_num(ctx.get("dayNtlVlm")),
            open_interest=_num(ctx.get("openInterest")),
            funding=_num(ctx.get("funding")),
            impact_bid=_num(impact[0]) if len(impact) > 0 else None,
            impact_ask=_num(impact[1]) if len(impact) > 1 else None,
        )
    return out


class HyperliquidInfo:
    """Two read-only queries against the public info endpoint.

    ``opener`` and ``sleep`` are injectable so the tests never open a socket.
    """

    def __init__(
        self,
        *,
        opener: Any = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        sleep: Any = time.sleep,
    ) -> None:
        self.opener = opener
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep

    def _query(self, query_type: str, **fields: Any) -> Any:
        if query_type not in ALLOWED_QUERY_TYPES:
            raise ValueError(f"not a read-only query this client sends: {query_type!r}")
        body = json.dumps({"type": query_type, **fields}).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            request = urllib.request.Request(
                INFO_URL,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            try:
                opener = self.opener or urllib.request.urlopen
                with opener(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_STATUSES:
                    raise HyperliquidError(f"HTTP {exc.code} for {query_type}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_error = exc
            if attempt < self.max_retries:
                self.sleep(min(2**attempt, 8))
        raise HyperliquidError(
            f"{query_type} failed after {self.max_retries} attempts: {last_error}"
        )

    def asset_contexts(self) -> dict[str, AssetContext]:
        return parse_meta_and_ctxs(self._query("metaAndAssetCtxs"))

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval {interval!r}")
        raw = self._query(
            "candleSnapshot",
            req={"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        )
        return parse_candles(raw)
