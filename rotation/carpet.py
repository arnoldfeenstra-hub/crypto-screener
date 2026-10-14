"""Assemble THE CARPET: every name's five readings, the quadrants, and THE CROSS.

``build_carpet`` is the whole tool behind ``api/carpet.py``. It takes an injectable
``info`` client so the tests run on synthetic candles and never open a socket.

    python -m rotation.carpet --group hl-crypto --read-on 1h      # live, prints a table
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rotation import engines
from rotation.hyperliquid import (
    INTERVAL_MS,
    SOURCE_NAME,
    AssetContext,
    Candle,
    HyperliquidError,
    HyperliquidInfo,
)

NOTICE = (
    "Descriptive, not predictive. Every reading here describes where a name sits "
    "against its group over the window; none has been tested for what happens next. "
    "No study backs any of these states, and nothing on this page is a recommendation "
    "or an order."
)


@dataclass(frozen=True)
class ReadOn:
    interval: str
    window: int
    label: str


#: The "READ ON" presets. "1H · two sessions" is the screenshot's; the rest follow
#: its pattern of a bar size and a window long enough to mean something on it.
READ_ON: dict[str, ReadOn] = {
    "15m": ReadOn("15m", 96, "one session"),
    "1h": ReadOn("1h", 48, "two sessions"),
    "4h": ReadOn("4h", 42, "a week"),
    "1d": ReadOn("1d", 30, "a month"),
}
DEFAULT_READ_ON = "1h"


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    names: tuple[str, ...] | None  # None = chosen from the live universe
    top_n: int = 0


#: "HL Crypto" is the twelve names in the screenshot's carpet.
GROUPS: dict[str, Group] = {
    "hl-crypto": Group(
        "hl-crypto",
        "HL Crypto",
        ("BTC", "ETH", "SOL", "HYPE", "XRP", "ENA", "PUMP", "NEAR", "ZEC", "ZRO", "W", "STRK"),
    ),
    "hl-majors": Group("hl-majors", "HL Majors", ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE")),
    "hl-top20": Group("hl-top20", "HL Top 20", None, top_n=20),
}
DEFAULT_GROUP = "hl-crypto"

PHRASES: dict[tuple[str, str], str] = {
    ("rich", "LEADING"): "the premium still chased",
    ("rich", "WEAKENING"): "the premium paid, the chase tiring",
    ("rich", "IMPROVING"): "rich past its beta while the ratio turns up from behind",
    ("rich", "LAGGING"): "rich past its beta, losing on the ratio",
    ("cheap", "LEADING"): "cheap past its beta, winning on the ratio",
    ("cheap", "WEAKENING"): "cheap past its beta, the lead fading",
    ("cheap", "IMPROVING"): "the discount being bid back",
    ("cheap", "LAGGING"): "the discount still being sold",
    ("fair", "LEADING"): "in line with its beta, out in front",
    ("fair", "WEAKENING"): "in line with its beta, the lead fading",
    ("fair", "IMPROVING"): "in line with its beta, turning up",
    ("fair", "LAGGING"): "in line with its beta, falling behind",
}


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def choose_names(group: Group, contexts: dict[str, AssetContext]) -> list[str]:
    if group.names is not None:
        return list(group.names)
    ranked = sorted(
        (c for c in contexts.values() if c.day_ntl_vlm is not None),
        key=lambda c: -(c.day_ntl_vlm or 0.0),
    )
    return [c.coin for c in ranked[: group.top_n]]


def fetch_candles(
    info: Any, names: list[str], read_on: ReadOn, now_ms: int, workers: int = 6
) -> tuple[dict[str, list[Candle]], dict[str, str]]:
    bars = 2 * read_on.window + engines.momentum_lag(read_on.window) + 12
    span = bars * INTERVAL_MS[read_on.interval]
    start = now_ms - span

    def one(coin: str) -> tuple[str, list[Candle] | None, str | None]:
        try:
            return coin, info.candles(coin, read_on.interval, start, now_ms), None
        except Exception as exc:
            return coin, None, f"{type(exc).__name__}: {exc}"

    candles: dict[str, list[Candle]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for coin, series, error in pool.map(one, names):
            if series:
                candles[coin] = series
            else:
                errors[coin] = error or "no candles returned"
    return candles, errors


def describe(
    coin: str,
    grid: engines.Grid,
    read_on: ReadOn,
    ctx: AssetContext | None,
    group_label: str,
) -> dict[str, Any]:
    window = read_on.window
    rot = engines.rotation(grid, coin, window)
    web = engines.web(grid, coin, window)
    strs = engines.strings(grid, coin, window)
    liq = engines.liquidity(grid, coin, window)
    agreement = engines.strings_agreement(web, strs)
    closes = grid.closes[coin]
    price = next((c for c in reversed(closes) if c is not None), None)

    reading = None
    if rot is not None and web is not None:
        parts = [f"{web.label.upper()} to {group_label}"]
        if liq is not None and liq.label != "normal":
            parts.append(f"{liq.label} liquidity")
        parts.append(rot.quadrant)
        reading = " · ".join(parts) + " — " + PHRASES[(web.label, rot.quadrant)]

    return {
        "coin": coin,
        "price": price,
        "session_pct": _round(engines.session_change(grid, coin), 6),
        "money_usd": ctx.day_ntl_vlm if ctx else None,
        "oi_usd": _round(ctx.open_interest_usd if ctx else None, 2),
        "funding_hourly": ctx.funding if ctx else None,
        "impact_bps": _round(ctx.impact_spread_bps if ctx else None, 2),
        "rotation": (
            {
                "ratio": _round(rot.ratio, 3),
                "momentum": _round(rot.momentum, 3),
                "quadrant": rot.quadrant,
                "tail": rot.tail,
            }
            if rot
            else None
        ),
        "web": {"z": _round(web.z, 3), "beta": _round(web.beta, 3), "label": web.label}
        if web
        else None,
        "strings": (
            {
                "score": _round(strs.score, 3),
                "label": strs.label,
                "strung": strs.strung,
                "tautest": {
                    "coin": strs.tautest.partner,
                    "z": _round(strs.tautest.z, 3),
                    "corr": _round(strs.tautest.corr, 3),
                },
                "agreement": agreement,
            }
            if strs
            else None
        ),
        "liquidity": {"rel": _round(liq.rel, 3), "label": liq.label} if liq else None,
        "reading": reading,
        "unread_reason": None if rot else "not enough bars in the window to place it",
    }


def chip_text(name: dict[str, Any], group_label: str) -> str:
    parts = [f"{name['web']['label'].upper()} to {group_label}"]
    strs = name.get("strings")
    if strs and strs.get("agreement"):
        parts.append(f"the strings {strs['agreement']} (tautest vs {strs['tautest']['coin']})")
    liq = name.get("liquidity")
    if liq and liq["label"] != "normal":
        parts.append(f"{liq['label']} liquidity")
    parts.append(name["rotation"]["quadrant"])
    return " · ".join(parts)


def the_cross(names: list[dict[str, Any]], group_label: str) -> dict[str, Any]:
    """Money by quadrant, and the names crossing with a rich/cheap web behind them.

    "The cross" in the screenshot lists only IMPROVING and WEAKENING names -- the two
    quadrants a name passes through when it crosses from one side of the plane to
    the other -- and only those the web calls rich or cheap.
    """
    placed = [n for n in names if n["rotation"] and n["money_usd"]]
    total = sum(n["money_usd"] for n in placed)
    shares = {q: 0.0 for q in engines.QUADRANTS}
    counts = {q: 0 for q in engines.QUADRANTS}
    for n in placed:
        shares[n["rotation"]["quadrant"]] += n["money_usd"] / total if total else 0.0
        counts[n["rotation"]["quadrant"]] += 1
    right = shares["LEADING"] + shares["WEAKENING"]

    chips = [
        {
            "coin": n["coin"],
            "quadrant": n["rotation"]["quadrant"],
            "web": n["web"]["label"],
            "text": chip_text(n, group_label),
        }
        for n in names
        if n["rotation"]
        and n["web"]
        and n["rotation"]["quadrant"] in ("IMPROVING", "WEAKENING")
        and n["web"]["label"] != "fair"
    ]
    chips.sort(key=lambda c: (c["quadrant"] != "WEAKENING", c["web"] != "rich", c["coin"]))

    summary = None
    if total:
        summary = (
            f"THE CROSS · {right:.0%} of the money on the right (LEADING + WEAKENING) · "
            f"{shares['LEADING']:.0%} LEADING · {shares['IMPROVING']:.0%} IMPROVING"
        )
    return {
        "summary": summary,
        "right_share": round(right, 4),
        "shares": {q: round(s, 4) for q, s in shares.items()},
        "counts": counts,
        "chips": chips,
    }


def build_carpet(
    group_id: str = DEFAULT_GROUP,
    read_on_id: str = DEFAULT_READ_ON,
    *,
    info: Any = None,
    now_ms: int | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    if group_id not in GROUPS:
        raise ValueError(f"unknown group {group_id!r}; known: {sorted(GROUPS)}")
    if read_on_id not in READ_ON:
        raise ValueError(f"unknown read-on {read_on_id!r}; known: {sorted(READ_ON)}")
    group, read_on = GROUPS[group_id], READ_ON[read_on_id]
    info = info or HyperliquidInfo()
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    started = clock()

    contexts = info.asset_contexts()
    names = choose_names(group, contexts)
    candles, fetch_errors = fetch_candles(info, names, read_on, now_ms)
    grid = engines.Grid.build(candles)

    described = [
        describe(coin, grid, read_on, contexts.get(coin), group.label) for coin in candles
    ]
    described.sort(key=lambda n: -(n["money_usd"] or 0.0))
    elapsed = clock() - started

    return {
        "tool": "the-carpet",
        "source": SOURCE_NAME,
        "generated_at": datetime.fromtimestamp(now_ms / 1000, UTC).isoformat(timespec="seconds"),
        "generated_at_ms": now_ms,
        "elapsed_s": round(elapsed, 2),
        "group": {"id": group.id, "label": group.label, "names": names},
        "read_on": {
            "id": read_on_id,
            "interval": read_on.interval,
            "window": read_on.window,
            "label": read_on.label,
        },
        "notice": NOTICE,
        "names": described,
        "missing": [{"coin": c, "error": e} for c, e in fetch_errors.items()],
        "cross": the_cross(described, group.label),
        "thresholds": {
            "web_rich_cheap_z": engines.WEB_THRESHOLD,
            "strings_lean_z": engines.STRINGS_LEAN,
            "strings_stretched_z": engines.STRINGS_STRETCHED,
            "string_min_corr": engines.STRING_MIN_CORR,
            "liquidity_hot": engines.LIQUIDITY_HOT,
            "liquidity_thin": engines.LIQUIDITY_THIN,
            "momentum_lag_bars": engines.momentum_lag(read_on.window),
            "liquidity_recent_bars": engines.liquidity_recent_bars(read_on.window),
            "min_coverage": engines.MIN_COVERAGE,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--group", default=DEFAULT_GROUP, choices=sorted(GROUPS))
    parser.add_argument("--read-on", default=DEFAULT_READ_ON, choices=sorted(READ_ON))
    args = parser.parse_args(argv)
    try:
        carpet = build_carpet(args.group, args.read_on)
    except HyperliquidError as exc:
        print(f"Hyperliquid is not answering: {exc}", file=sys.stderr)
        return 2
    print(f"{carpet['group']['label']} · {carpet['read_on']['interval']} · "
          f"{carpet['read_on']['label']} · {carpet['elapsed_s']}s")
    if carpet["cross"]["summary"]:
        print(carpet["cross"]["summary"])
    for n in carpet["names"]:
        rot = n["rotation"] or {}
        print(
            f"{n['coin']:>6}  {rot.get('quadrant', '—'):<10} "
            f"web {((n['web'] or {}).get('label') or '—'):<6} "
            f"strings {((n['strings'] or {}).get('label') or '—'):<15} "
            f"liq {((n['liquidity'] or {}).get('label') or '—'):<7} "
            f"{n['reading'] or n['unread_reason']}"
        )
    print(carpet["notice"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
