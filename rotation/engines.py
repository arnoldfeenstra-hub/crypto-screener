"""The four engines behind a Carpet tile, plus the session.

Each one is reverse-engineered from what the screenshot's tooltip and chips say,
not from source code; ``docs/carpet.md`` gives the evidence for each and marks what
is inference. All four read *log returns on a common bar grid* over one window of
``W`` bars (the "READ ON" setting -- 1h bars, two sessions, is W = 48).

THE ROTATION  Relative-rotation quadrant against the group benchmark: RS-Ratio
              (where the name's relative strength sits in its own window) and
              RS-Momentum (whether that ratio is rising). JdK's exact formulas are
              unpublished; this is the open z-score approximation.
THE WEB       Rich or cheap to the group *after beta*: the name's residual drift
              over the window, in standard errors. It is the web of every name
              tied to the one benchmark.
THE STRINGS   Rich or cheap to each correlated name separately: one hedged pair
              spread per partner, z-scored in its window. "Tautest" is the most
              stretched string. They agree with the web when both lean one way.
THE LIQUIDITY Recent money traded per bar against the window's typical bar:
              HOT, normal, or thin.

Missing data stays missing. A bar a coin did not print contributes no return; a
reading whose window is short of bars is ``None`` with a reason, never filled.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import median

from rotation.hyperliquid import Candle

# --- thresholds -----------------------------------------------------------------

#: Share of the window's bars a reading needs. Below it the reading is withheld.
MIN_COVERAGE = 0.9
#: |z| at which the web calls a name rich or cheap.
WEB_THRESHOLD = 1.0
#: |z| at which the strings lean, and at which they are stretched.
STRINGS_LEAN = 1.0
STRINGS_STRETCHED = 2.0
#: Return correlation a pair needs before it is strung at all.
STRING_MIN_CORR = 0.5
#: Recent-to-typical money ratio for HOT and for thin.
LIQUIDITY_HOT = 1.5
LIQUIDITY_THIN = 0.67

QUADRANTS = ("LEADING", "WEAKENING", "LAGGING", "IMPROVING")


# --- small statistics, None-aware ----------------------------------------------


def _clean(values: Sequence[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def zscore_last(values: Sequence[float]) -> float | None:
    """Where the last value sits in the window, in standard deviations."""
    if len(values) < 3:
        return None
    sd = stdev(values)
    if sd == 0:
        return 0.0
    return (values[-1] - mean(values)) / sd


def paired(a: Sequence[float | None], b: Sequence[float | None]) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for x, y in zip(a, b, strict=True):
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    return xs, ys


def covariance(xs: Sequence[float], ys: Sequence[float]) -> float:
    mx, my = mean(xs), mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (len(xs) - 1)


def correlation(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    sx, sy = stdev(xs), stdev(ys)
    if sx == 0 or sy == 0:
        return None
    return covariance(xs, ys) / (sx * sy)


def beta(ys: Sequence[float], xs: Sequence[float]) -> float | None:
    """Slope of ys on xs."""
    if len(xs) < 3:
        return None
    var = stdev(xs) ** 2
    if var == 0:
        return None
    return covariance(xs, ys) / var


# --- the grid -------------------------------------------------------------------


@dataclass
class Grid:
    """Every coin's closes and money on one shared timeline. Gaps are ``None``."""

    times: list[int]
    closes: dict[str, list[float | None]]
    money: dict[str, list[float | None]]
    opens: dict[str, list[float | None]]
    returns: dict[str, list[float | None]] = field(default_factory=dict)
    bench: list[float | None] = field(default_factory=list)

    @classmethod
    def build(cls, candles: dict[str, list[Candle]]) -> Grid:
        times = sorted({c.t for series in candles.values() for c in series})
        index = {t: i for i, t in enumerate(times)}
        closes: dict[str, list[float | None]] = {}
        money: dict[str, list[float | None]] = {}
        opens: dict[str, list[float | None]] = {}
        for coin, series in candles.items():
            cl: list[float | None] = [None] * len(times)
            mo: list[float | None] = [None] * len(times)
            op: list[float | None] = [None] * len(times)
            for candle in series:
                i = index[candle.t]
                cl[i] = candle.c if candle.c and candle.c > 0 else None
                mo[i] = candle.notional
                op[i] = candle.o if candle.o and candle.o > 0 else None
            closes[coin], money[coin], opens[coin] = cl, mo, op
        grid = cls(times=times, closes=closes, money=money, opens=opens)
        grid._derive()
        return grid

    def _derive(self) -> None:
        n = len(self.times)
        for coin, cl in self.closes.items():
            rets: list[float | None] = [None] * n
            for i in range(1, n):
                a, b = cl[i - 1], cl[i]
                if a is not None and b is not None:
                    rets[i] = math.log(b / a)
            self.returns[coin] = rets
        # Equal-weight benchmark: the mean log return of the members that printed
        # the bar, if at least half of them did. Equal weight so the group is the
        # group, not BTC with passengers.
        members = len(self.closes)
        quorum = max(2, math.ceil(members / 2)) if members > 1 else 1
        bench: list[float | None] = [None] * n
        for i in range(n):
            present = [r[i] for r in self.returns.values() if r[i] is not None]
            if len(present) >= quorum:
                bench[i] = mean(present)
        self.bench = bench


def _coverage(values: Sequence[float | None]) -> float:
    return (len(values) - values.count(None)) / len(values) if values else 0.0


# --- THE ROTATION ---------------------------------------------------------------


@dataclass(frozen=True)
class Rotation:
    ratio: float
    momentum: float
    quadrant: str
    tail: list[tuple[float, float]]  # oldest first, last point is now


def quadrant_of(ratio: float, momentum: float) -> str:
    if ratio >= 100:
        return "LEADING" if momentum >= 100 else "WEAKENING"
    return "IMPROVING" if momentum >= 100 else "LAGGING"


def momentum_lag(window: int) -> int:
    return max(2, window // 6)


def rotation(grid: Grid, coin: str, window: int, tail: int = 6) -> Rotation | None:
    rets = grid.returns[coin]
    n = len(rets)
    lag = momentum_lag(window)
    needed = 2 * window + lag + tail
    if n < needed:
        return None
    recent_rel = [
        (r - b) if r is not None and b is not None else None
        for r, b in zip(rets[n - needed :], grid.bench[n - needed :], strict=True)
    ]
    if _coverage(recent_rel) < MIN_COVERAGE:
        return None
    # Relative log price: the running sum of the name's excess return. A bar with
    # no print adds nothing, which is why coverage is checked first.
    rs: list[float] = []
    level = 0.0
    for step in recent_rel:
        level += step or 0.0
        rs.append(level)
    ratio = [
        100.0 + (zscore_last(rs[i - window + 1 : i + 1]) or 0.0)
        for i in range(window - 1, len(rs))
    ]
    change = [ratio[i] - ratio[i - lag] for i in range(lag, len(ratio))]
    momentum = [
        100.0 + (zscore_last(change[i - window + 1 : i + 1]) or 0.0)
        for i in range(window - 1, len(change))
    ]
    ratio = ratio[-len(momentum) :]
    points = list(zip(ratio[-tail:], momentum[-tail:], strict=True))
    r_now, m_now = points[-1]
    return Rotation(
        ratio=r_now,
        momentum=m_now,
        quadrant=quadrant_of(r_now, m_now),
        tail=[(round(r, 3), round(m, 3)) for r, m in points],
    )


# --- THE WEB --------------------------------------------------------------------


@dataclass(frozen=True)
class Web:
    z: float
    beta: float
    label: str  # rich | fair | cheap


def web_label(z: float) -> str:
    if z >= WEB_THRESHOLD:
        return "rich"
    if z <= -WEB_THRESHOLD:
        return "cheap"
    return "fair"


def web(grid: Grid, coin: str, window: int) -> Web | None:
    rets = grid.returns[coin][-window:]
    bench = grid.bench[-window:]
    if len(rets) < window or _coverage(paired_mask(rets, bench)) < MIN_COVERAGE:
        return None
    ys, xs = paired(rets, bench)
    b = beta(ys, xs)
    if b is None:
        return None
    residuals = [y - b * x for y, x in zip(ys, xs, strict=True)]
    sd = stdev(residuals)
    if sd == 0:
        return None
    # Residual drift over the window in standard errors. The mean residual is not
    # removed first: it is the thing being measured.
    z = sum(residuals) / (sd * math.sqrt(len(residuals)))
    return Web(z=z, beta=b, label=web_label(z))


def paired_mask(a: Sequence[float | None], b: Sequence[float | None]) -> list[float | None]:
    return [0.0 if x is not None and y is not None else None for x, y in zip(a, b, strict=True)]


# --- THE STRINGS ----------------------------------------------------------------


@dataclass(frozen=True)
class StringPull:
    partner: str
    z: float
    corr: float


@dataclass(frozen=True)
class Strings:
    score: float
    label: str
    strung: int
    tautest: StringPull


def strings_label(score: float) -> str:
    side = "rich" if score > 0 else "cheap"
    if abs(score) >= STRINGS_STRETCHED:
        return f"stretched {side}"
    if abs(score) >= STRINGS_LEAN:
        return side
    return "slack"


def string_pull(grid: Grid, coin: str, partner: str, window: int) -> StringPull | None:
    a = grid.returns[coin][-window:]
    b = grid.returns[partner][-window:]
    if len(a) < window or _coverage(paired_mask(a, b)) < MIN_COVERAGE:
        return None
    ys, xs = paired(a, b)
    rho = correlation(ys, xs)
    if rho is None or rho < STRING_MIN_CORR:
        return None
    hedge = beta(ys, xs)
    if hedge is None:
        return None
    spread: list[float] = []
    level = 0.0
    for y, x in zip(ys, xs, strict=True):
        level += y - hedge * x
        spread.append(level)
    z = zscore_last(spread)
    if z is None:
        return None
    return StringPull(partner=partner, z=z, corr=rho)


def strings(grid: Grid, coin: str, window: int) -> Strings | None:
    pulls = [
        pull
        for partner in grid.closes
        if partner != coin
        for pull in [string_pull(grid, coin, partner, window)]
        if pull is not None
    ]
    if not pulls:
        return None
    weight = sum(p.corr for p in pulls)
    score = sum(p.corr * p.z for p in pulls) / weight
    tautest = max(pulls, key=lambda p: abs(p.z))
    return Strings(score=score, label=strings_label(score), strung=len(pulls), tautest=tautest)


def strings_agreement(web_reading: Web | None, strings_reading: Strings | None) -> str | None:
    """'agree' / 'disagree' when both lean; None when either is neutral or missing."""
    if web_reading is None or strings_reading is None or web_reading.label == "fair":
        return None
    if abs(strings_reading.score) < STRINGS_LEAN:
        return None
    same_side = (web_reading.z > 0) == (strings_reading.score > 0)
    return "agree" if same_side else "disagree"


# --- THE LIQUIDITY --------------------------------------------------------------


@dataclass(frozen=True)
class Liquidity:
    rel: float
    label: str  # HOT | normal | thin


def liquidity_recent_bars(window: int) -> int:
    return max(1, window // 8)


def liquidity(grid: Grid, coin: str, window: int) -> Liquidity | None:
    k = liquidity_recent_bars(window)
    money = grid.money[coin]
    if len(money) < window + k:
        return None
    recent = _clean(money[-k:])
    base = _clean(money[-(window + k) : -k])
    if len(recent) < k or len(base) < MIN_COVERAGE * window:
        return None
    typical = median(base)
    if typical <= 0:
        return None
    rel = mean(recent) / typical
    label = "HOT" if rel >= LIQUIDITY_HOT else "thin" if rel <= LIQUIDITY_THIN else "normal"
    return Liquidity(rel=rel, label=label)


# --- THE SESSION ----------------------------------------------------------------

DAY_MS = 86_400_000


def session_change(grid: Grid, coin: str) -> float | None:
    """Last close against the open of the first bar of the current UTC day."""
    closes, opens = grid.closes[coin], grid.opens[coin]
    last_i = next((i for i in range(len(closes) - 1, -1, -1) if closes[i] is not None), None)
    if last_i is None:
        return None
    session_start = grid.times[last_i] - grid.times[last_i] % DAY_MS
    first_i = next(
        (i for i in range(len(grid.times)) if grid.times[i] >= session_start), None
    )
    if first_i is None or opens[first_i] is None:
        return None
    return closes[last_i] / opens[first_i] - 1.0
