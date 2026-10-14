"""The chart behind a tile: candles plus the studies drawn on the second screenshot.

    white line    EMA(9) on the chart's own bars -- the fast line hugging price
    magenta line  session VWAP, reset at 00:00 UTC
    cyan line     EMA(20) of the "up-rung": the next bar size up, built from the
                  chart's bars and stepped so a bar only sees up-rung bars that
                  had already closed
    BOX           a zone price keeps returning to. Built from swing pivots: a
                  swing high opens a SELL box over its upper wick, a swing low a
                  BUY box over its lower wick, later pivots inside the zone count
                  as touches (the "x6"; three or more makes a box), and a close
                  through the far side marks it BROKEN
    TPR           a swing that probes past the previous swing on its side and
                  closes back inside it -- the failed break
    heat strip    bars whose volume is at least twice the trailing median, red
                  when the bar closed down and amber when it closed up

The screenshot's order ticket is not reproduced. This repo places no orders.
Hyperliquid serves nothing finer than one-minute bars, so the screenshot's 5s and
30s charts have no equivalent here -- the minute is the floor.
"""

from __future__ import annotations

import time
from statistics import median
from typing import Any

from rotation.hyperliquid import INTERVAL_MS, SOURCE_NAME, Candle, HyperliquidInfo

DEFAULT_BARS = 800
MAX_BARS = 2000
UP_RUNG = {"1m": "5m", "5m": "30m", "15m": "1h", "30m": "4h", "1h": "4h", "4h": "1d"}
FAST_EMA = 9
RUNG_EMA = 20
PIVOT_SPAN = 3
HEAT_LOOKBACK = 50
HEAT_MIN_REL = 2.0
#: Pivots a zone needs before it is drawn as a box. Two swings near one price are
#: a coincidence often enough; three is a level the tape keeps coming back to.
MIN_TOUCHES = 3
DAY_MS = 86_400_000

NOTICE = (
    "Studies describe the tape already printed. None is a signal, and BUY / SELL on "
    "a box names the side of the zone, not an instruction."
)


def ema(values: list[float | None], span: int) -> list[float | None]:
    """EMA over the bars that printed; a gap leaves the line blank at that bar."""
    alpha = 2.0 / (span + 1)
    out: list[float | None] = []
    state: float | None = None
    seen = 0
    for v in values:
        if v is None:
            out.append(None)
            continue
        state = v if state is None else alpha * v + (1 - alpha) * state
        seen += 1
        out.append(state if seen >= span else None)
    return out


def session_vwap(candles: list[Candle]) -> list[float | None]:
    out: list[float | None] = []
    day = None
    pv = vol = 0.0
    for c in candles:
        if c.t // DAY_MS != day:
            day, pv, vol = c.t // DAY_MS, 0.0, 0.0
        if c.v is not None and c.h is not None and c.l is not None and c.c is not None:
            pv += c.v * (c.h + c.l + c.c) / 3.0
            vol += c.v
        out.append(pv / vol if vol > 0 else None)
    return out


def up_rung_ema(candles: list[Candle], interval: str) -> tuple[str | None, list[float | None]]:
    rung = UP_RUNG.get(interval)
    if rung is None:
        return None, [None] * len(candles)
    rung_ms, bar_ms = INTERVAL_MS[rung], INTERVAL_MS[interval]
    per = rung_ms // bar_ms
    buckets: dict[int, list[Candle]] = {}
    for c in candles:
        buckets.setdefault(c.t // rung_ms, []).append(c)
    keys = sorted(buckets)
    # Only complete buckets are closed rung bars; a short one is still printing.
    closes = [
        buckets[k][-1].c if len(buckets[k]) == per else None for k in keys
    ]
    line = ema(closes, RUNG_EMA)
    by_bucket = dict(zip(keys, line, strict=True))
    out: list[float | None] = []
    for c in candles:
        # The last rung bar that had closed before this bar opened.
        out.append(by_bucket.get(c.t // rung_ms - 1))
    return rung, out


def pivots(candles: list[Candle], span: int = PIVOT_SPAN) -> list[tuple[int, str]]:
    """Fractal swings: a bar whose high (low) beats the ``span`` bars either side."""
    found: list[tuple[int, str]] = []
    for i in range(span, len(candles) - span):
        window = candles[i - span : i + span + 1]
        if any(c.h is None or c.l is None for c in window):
            continue
        h, low = candles[i].h, candles[i].l
        left, right = candles[i - span : i], candles[i + 1 : i + span + 1]
        if all(c.h < h for c in left) and all(c.h <= h for c in right):
            found.append((i, "high"))
        if all(c.l > low for c in left) and all(c.l >= low for c in right):
            found.append((i, "low"))
    return found


def typical_range(candles: list[Candle]) -> float | None:
    ranges = [c.h - c.l for c in candles if c.h is not None and c.l is not None and c.h > c.l]
    return median(ranges) if ranges else None


def boxes(candles: list[Candle], pivot_list: list[tuple[int, str]]) -> dict[str, Any]:
    tol = typical_range(candles)
    if tol is None:
        return {"boxes": [], "touches": []}
    max_height = 3 * tol
    active: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    touches: list[dict[str, Any]] = []
    pivot_at = {i: [] for i, _ in pivot_list}
    for i, kind in pivot_list:
        pivot_at[i].append(kind)

    for i, c in enumerate(candles):
        # A close through the far side breaks a box.
        if c.c is not None:
            for box in list(active):
                through = c.c > box["top"] if box["side"] == "SELL" else c.c < box["bottom"]
                if through and i > box["last"]:
                    box["broken_at"] = i
                    box["end"] = i
                    active.remove(box)
                    done.append(box)
        for kind in pivot_at.get(i, []) if c.c is not None else []:
            side = "SELL" if kind == "high" else "BUY"
            if side == "SELL":
                hi, lo = c.h, max(c.o or c.c, c.c)
            else:
                hi, lo = min(c.o or c.c, c.c), c.l
            lo = min(lo, hi - tol / 2)
            hi = max(hi, lo + tol / 2)
            price = c.h if side == "SELL" else c.l
            host = next(
                (
                    b
                    for b in active
                    if b["side"] == side
                    and b["bottom"] - tol <= price <= b["top"] + tol
                    and max(b["top"], hi) - min(b["bottom"], lo) <= max_height
                ),
                None,
            )
            if host is None:
                active.append(
                    {"side": side, "top": hi, "bottom": lo, "start": i, "last": i,
                     "touches": 1, "broken_at": None, "end": None}
                )
            else:
                host["top"], host["bottom"] = max(host["top"], hi), min(host["bottom"], lo)
                host["touches"] += 1
                host["last"] = i
                touches.append({"i": i, "side": side, "price": price, "box": id(host)})
    last = len(candles) - 1
    for box in active:
        box["end"] = last
    every = [b for b in done + active if b["touches"] >= MIN_TOUCHES]
    for b in every:
        state = "BROKEN" if b["broken_at"] is not None else b["side"]
        b["label"] = f"BOX ×{b['touches']} · {state}"
    every.sort(key=lambda b: b["start"])
    # A touch is marked only on a box that is drawn.
    drawn = {id(b) for b in every}
    marks = [{k: v for k, v in t.items() if k != "box"} for t in touches if t["box"] in drawn]
    return {"boxes": every, "touches": marks}


def tprs(candles: list[Candle], pivot_list: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """Probe past the previous swing, close back inside it."""
    out: list[dict[str, Any]] = []
    prev: dict[str, int] = {}
    for i, kind in pivot_list:
        j = prev.get(kind)
        c = candles[i]
        if j is not None and c.c is not None:
            ref = candles[j]
            if kind == "high" and c.h > ref.h and c.c < ref.h:
                out.append({"i": i, "side": "high", "price": c.h, "swept": ref.h})
            if kind == "low" and c.l < ref.l and c.c > ref.l:
                out.append({"i": i, "side": "low", "price": c.l, "swept": ref.l})
        prev[kind] = i
    return out


def heat(candles: list[Candle]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, c in enumerate(candles):
        if c.v is None or c.o is None or c.c is None:
            continue
        trail = [x.v for x in candles[max(0, i - HEAT_LOOKBACK) : i] if x.v is not None]
        if len(trail) < HEAT_LOOKBACK // 2:
            continue
        typical = median(trail)
        if typical <= 0:
            continue
        rel = c.v / typical
        if rel >= HEAT_MIN_REL:
            out.append({"i": i, "rel": round(rel, 2), "side": "down" if c.c < c.o else "up"})
    return out


def headline(coin: str, interval: str, n: int, rung: str | None) -> str:
    hours = n * INTERVAL_MS[interval] / 3_600_000
    tape = f"{hours:.1f} h of tape" if hours < 72 else f"{hours / 24:.1f} d of tape"
    parts = [coin, interval, f"{n} bars", tape]
    if rung:
        parts.append(f"up-rung {rung}")
    return " · ".join(parts)


def build_chart(
    coin: str,
    interval: str = "5m",
    bars: int = DEFAULT_BARS,
    *,
    info: Any = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval!r}; known: {sorted(INTERVAL_MS)}")
    bars = max(50, min(bars, MAX_BARS))
    info = info or HyperliquidInfo()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    candles = info.candles(coin, interval, now_ms - bars * INTERVAL_MS[interval], now_ms)
    candles = candles[-bars:]
    piv = pivots(candles)
    rung, rung_line = up_rung_ema(candles, interval)
    box_result = boxes(candles, piv)
    return {
        "tool": "the-carpet-chart",
        "source": SOURCE_NAME,
        "coin": coin,
        "interval": interval,
        "up_rung": rung,
        "headline": headline(coin, interval, len(candles), rung),
        "generated_at_ms": now_ms,
        "notice": NOTICE,
        "candles": [[c.t, c.o, c.h, c.l, c.c, c.v] for c in candles],
        "lines": {
            "fast_ema": ema([c.c for c in candles], FAST_EMA),
            "vwap": session_vwap(candles),
            "rung_ema": rung_line,
        },
        "boxes": box_result["boxes"],
        "touches": box_result["touches"],
        "tpr": tprs(candles, piv),
        "heat": heat(candles),
    }
