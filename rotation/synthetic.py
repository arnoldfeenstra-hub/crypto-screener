"""A deterministic, clearly fake market for tests and the offline demo.

Nothing here is market data. Each coin is a one-factor walk -- a shared market
factor times a beta, plus its own drift and noise -- with a few scripted names
so every quadrant gets a resident: one that has run and is still running, one
that ran and is rolling over, one that fell and is turning up, one still falling.

    python -m rotation.synthetic --out web/carpet-demo.json

writes the payload the page falls back to when ``/api/carpet`` is not deployed.
Every payload built from it says ``"synthetic": true``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any

from rotation.hyperliquid import INTERVAL_MS, AssetContext, Candle

BASE_PRICE = {
    "BTC": 112_000.0, "ETH": 4_100.0, "SOL": 205.0, "HYPE": 44.0, "XRP": 2.6, "ENA": 0.62,
    "PUMP": 0.0058, "NEAR": 2.9, "ZEC": 285.0, "ZRO": 1.9, "W": 0.11, "STRK": 0.13,
    "BNB": 1_150.0, "DOGE": 0.25,
}
MONEY = {
    "BTC": 2.9e9, "ETH": 1.9e9, "SOL": 6.5e8, "HYPE": 5.2e8, "XRP": 3.1e8, "ENA": 1.4e8,
    "PUMP": 1.1e8, "NEAR": 6.0e7, "ZEC": 2.6e8, "ZRO": 2.2e7, "W": 1.2e7, "STRK": 1.8e7,
    "BNB": 1.5e8, "DOGE": 2.4e8,
}
# (beta, drift per bar over the first two thirds, drift over the last third)
SCRIPT = {
    "ZEC": (1.1, 0.0015, 0.0030),   # ran, still running      -> leading
    "NEAR": (1.2, 0.0006, 0.0018),
    "W": (1.3, 0.0030, -0.0015),    # ran, rolling over       -> weakening
    "ZRO": (1.2, 0.0025, -0.0010),
    "SOL": (1.1, -0.0020, 0.0010),  # fell, turning up        -> improving
    "HYPE": (0.9, -0.0018, 0.0012),
    "STRK": (1.3, -0.0010, -0.0025),  # still falling         -> lagging
}


def series(coin: str, n: int, interval: str, end_ms: int, seed: int = 7) -> list[Candle]:
    bar_ms = INTERVAL_MS[interval]
    market = random.Random(seed)
    own = random.Random(f"{seed}:{coin}")
    beta, early, late = SCRIPT.get(coin, (1.0, 0.0, 0.0))
    price = BASE_PRICE.get(coin, 10.0)
    per_bar_money = MONEY.get(coin, 5e7) / (86_400_000 / bar_ms)
    start = end_ms - end_ms % bar_ms - (n - 1) * bar_ms
    out: list[Candle] = []
    for i in range(n):
        f = market.gauss(0, 0.004)
        drift = early if i < 2 * n // 3 else late
        r = beta * f + drift + own.gauss(0, 0.003)
        o = price
        c = price * math.exp(r)
        wick = abs(own.gauss(0, 0.002))
        h, low = max(o, c) * (1 + wick), min(o, c) * (1 - abs(own.gauss(0, 0.002)))
        heat = 2.6 if own.random() < 0.04 else 1.0
        if coin in ("SOL", "BTC", "ENA") and i >= n - 6:
            heat = 2.0  # recent volume running hot
        if coin in ("ZEC", "STRK") and i >= n - 6:
            heat = 0.4  # and thin
        money = per_bar_money * heat * (0.7 + 0.6 * own.random())
        typical = (h + low + c) / 3
        out.append(Candle(t=start + i * bar_ms, o=o, h=h, l=low, c=c, v=money / typical))
        price = c
    return out


class SyntheticInfo:
    """Stands in for ``HyperliquidInfo``: same two methods, no network."""

    synthetic = True

    def __init__(self, seed: int = 7, missing: tuple[str, ...] = ()) -> None:
        self.seed = seed
        self.missing = set(missing)
        self.calls: list[tuple[str, ...]] = []

    def asset_contexts(self) -> dict[str, AssetContext]:
        self.calls.append(("metaAndAssetCtxs",))
        return {
            coin: AssetContext(
                coin=coin,
                mark_px=px,
                prev_day_px=px,
                day_ntl_vlm=MONEY.get(coin),
                open_interest=MONEY.get(coin, 0) / px / 3,
                funding=0.0000125,
                impact_bid=px * (1 - 0.0002),
                impact_ask=px * (1 + 0.0002),
            )
            for coin, px in BASE_PRICE.items()
        }

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        self.calls.append(("candleSnapshot", coin, interval))
        if coin in self.missing:
            return []
        n = max(1, (end_ms - start_ms) // INTERVAL_MS[interval])
        return series(coin, int(n), interval, end_ms, self.seed)


#: A fixed clock so the demo file and the tests do not change between runs.
DEMO_NOW_MS = 1_791_800_000_000


def demo_payload() -> dict[str, Any]:
    from rotation.carpet import build_carpet
    from rotation.chart import build_chart

    info = SyntheticInfo()
    carpet = build_carpet(info=info, now_ms=DEMO_NOW_MS, clock=lambda: 0.0)
    charts = {
        coin: build_chart(coin, "5m", 400, info=info, now_ms=DEMO_NOW_MS)
        for coin in carpet["group"]["names"]
    }
    for payload in (carpet, *charts.values()):
        payload["synthetic"] = True
        payload["source"] = "synthetic (rotation/synthetic.py) — not market data"
    return _trim({"carpet": carpet, "charts": charts, "synthetic": True})


def _trim(value: Any) -> Any:
    """Six significant figures: plenty for a fake market, a third of the file size."""
    if isinstance(value, float):
        return float(f"{value:.6g}")
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_trim(v) for v in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the synthetic Carpet demo payload.")
    parser.add_argument("--out", default="web/carpet-demo.json")
    args = parser.parse_args(argv)
    Path(args.out).write_text(json.dumps(demo_payload(), separators=(",", ":")) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
