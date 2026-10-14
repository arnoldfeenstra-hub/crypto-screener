"""THE CARPET: the Hyperliquid reader, the four engines, the chart studies, the endpoint.

Every test runs on hand-built candles or ``rotation/synthetic.py`` -- never on the
network (``tests/conftest.py`` enforces that).
"""

from __future__ import annotations

import ast
import io
import json
import math
import sys
from pathlib import Path

import pytest

from rotation import engines
from rotation.carpet import GROUPS, build_carpet, the_cross
from rotation.chart import (
    MIN_TOUCHES,
    boxes,
    build_chart,
    ema,
    heat,
    pivots,
    session_vwap,
    tprs,
    up_rung_ema,
)
from rotation.hyperliquid import (
    ALLOWED_QUERY_TYPES,
    INFO_URL,
    Candle,
    HyperliquidError,
    HyperliquidInfo,
    parse_candles,
    parse_meta_and_ctxs,
)
from rotation.synthetic import DEMO_NOW_MS, SyntheticInfo, demo_payload

REPO_ROOT = Path(__file__).resolve().parent.parent
HOUR = 3_600_000
DAY = 86_400_000


def walk(returns: list[float], start: float = 100.0, t0: int = 0, step: int = HOUR,
         money: list[float] | None = None) -> list[Candle]:
    """Candles whose closes follow ``returns`` exactly."""
    out, price = [], start
    for i, r in enumerate(returns):
        close = price * math.exp(r)
        notional = money[i] if money else 1_000_000.0
        out.append(Candle(t=t0 + i * step, o=price, h=max(price, close) * 1.001,
                          l=min(price, close) * 0.999, c=close, v=notional / close))
        price = close
    return out


def market(n: int, seed: int = 1) -> list[float]:
    import random

    rng = random.Random(seed)
    return [rng.gauss(0, 0.004) for _ in range(n)]


# --- the reader -----------------------------------------------------------------


class TestReader:
    def test_only_two_read_only_queries_exist(self):
        assert frozenset({"metaAndAssetCtxs", "candleSnapshot"}) == ALLOWED_QUERY_TYPES

    def test_anything_else_is_refused_before_a_request_is_built(self):
        def opener(*a, **k):
            raise AssertionError("no request should be made")

        with pytest.raises(ValueError, match="not a read-only query"):
            HyperliquidInfo(opener=opener)._query("order")

    def test_the_module_can_reach_one_url_and_signs_nothing(self):
        source = (REPO_ROOT / "rotation" / "hyperliquid.py").read_text()
        tree = ast.parse(source)
        urls = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("http")
        }
        assert urls == {INFO_URL}
        assert INFO_URL.endswith("/info")
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        for forbidden in ("private_key", "sign", "signature", "nonce", "wallet", "Account"):
            assert forbidden not in names

    def test_it_posts_the_allowlisted_body_to_the_info_url(self):
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout):
            seen["url"], seen["method"] = request.full_url, request.get_method()
            seen["body"] = json.loads(request.data)
            return Response(json.dumps([{"t": 1, "o": "1", "h": "2", "l": "0.5",
                                          "c": "1.5", "v": "10"}]).encode())

        candles = HyperliquidInfo(opener=opener).candles("BTC", "1h", 0, 10)
        assert seen["url"] == INFO_URL and seen["method"] == "POST"
        assert seen["body"]["type"] == "candleSnapshot"
        assert seen["body"]["req"]["coin"] == "BTC"
        assert candles[0].c == 1.5

    def test_retries_then_gives_up_with_a_named_error(self):
        import urllib.error

        calls = []

        def opener(request, timeout):
            calls.append(1)
            raise urllib.error.URLError("down")

        with pytest.raises(HyperliquidError):
            HyperliquidInfo(opener=opener, sleep=lambda s: None).asset_contexts()
        assert len(calls) == 3

    def test_nan_and_garbage_are_missing_not_zero(self):
        [c] = parse_candles([{"t": 5, "o": "NaN", "h": "x", "l": None, "c": "2", "v": "inf"}])
        assert (c.o, c.h, c.l, c.c, c.v) == (None, None, None, 2.0, None)
        assert c.notional is None

    def test_contexts_align_by_index_and_skip_delisted(self):
        raw = [
            {"universe": [{"name": "BTC"}, {"name": "OLD", "isDelisted": True}, {"name": "ETH"}]},
            [
                {"markPx": "100", "dayNtlVlm": "5", "openInterest": "2",
                 "impactPxs": ["99.9", "100.1"]},
                {"markPx": "1"},
                {"markPx": "10"},
            ],
        ]
        ctxs = parse_meta_and_ctxs(raw)
        assert set(ctxs) == {"BTC", "ETH"}
        assert ctxs["BTC"].open_interest_usd == 200
        assert ctxs["BTC"].impact_spread_bps == pytest.approx(20)
        assert ctxs["ETH"].day_ntl_vlm is None


# --- the engines ----------------------------------------------------------------


def three_names(n: int = 160, lead: float = 0.002, lag: float = -0.002) -> engines.Grid:
    m = market(n)
    rng_a, rng_b, rng_c = (market(n, s) for s in (11, 12, 13))
    return engines.Grid.build({
        "UP": walk([x + lead + 0.3 * e for x, e in zip(m, rng_a, strict=True)]),
        "MID": walk([x + 0.3 * e for x, e in zip(m, rng_b, strict=True)]),
        "DOWN": walk([x + lag + 0.3 * e for x, e in zip(m, rng_c, strict=True)]),
    })


class TestEngines:
    @pytest.mark.parametrize(
        ("ratio", "momentum", "quadrant"),
        [(101, 101, "LEADING"), (101, 99, "WEAKENING"), (99, 99, "LAGGING"),
         (99, 101, "IMPROVING")],
    )
    def test_quadrants_follow_the_rotation_plane(self, ratio, momentum, quadrant):
        assert engines.quadrant_of(ratio, momentum) == quadrant

    def test_a_missing_bar_stays_missing(self):
        a = walk(market(10))
        b = walk(market(10, 2))
        del b[4]
        grid = engines.Grid.build({"A": a, "B": b})
        assert grid.closes["B"][4] is None
        assert grid.returns["B"][4] is None and grid.returns["B"][5] is None

    def test_an_outperformer_is_right_of_centre_and_rich(self):
        grid = three_names()
        rot = engines.rotation(grid, "UP", 48)
        web = engines.web(grid, "UP", 48)
        assert rot.ratio > 100 and rot.quadrant in ("LEADING", "WEAKENING")
        assert web.label == "rich"
        assert engines.web(grid, "DOWN", 48).label == "cheap"
        assert engines.rotation(grid, "DOWN", 48).ratio < 100

    def test_too_few_bars_is_no_reading(self):
        grid = three_names(n=60)
        assert engines.rotation(grid, "UP", 48) is None

    def test_a_gappy_window_is_withheld_not_filled(self):
        m = market(160)
        gappy = walk(m)
        gappy = [c for i, c in enumerate(gappy) if i % 5 != 0]  # 20% of bars missing
        grid = engines.Grid.build({"A": walk(m), "B": walk(market(160, 3)), "G": gappy})
        assert engines.rotation(grid, "G", 48) is None
        assert engines.web(grid, "G", 48) is None

    def test_labels_and_thresholds(self):
        assert engines.web_label(1.0) == "rich"
        assert engines.web_label(0.99) == "fair"
        assert engines.web_label(-1.2) == "cheap"
        assert engines.strings_label(2.4) == "stretched rich"
        assert engines.strings_label(-1.5) == "cheap"
        assert engines.strings_label(0.5) == "slack"

    def test_strings_agree_only_when_both_lean_the_same_way(self):
        pull = engines.StringPull("X", 2.0, 0.8)
        rich = engines.Web(z=1.5, beta=1.0, label="rich")
        fair = engines.Web(z=0.2, beta=1.0, label="fair")
        assert engines.strings_agreement(rich, engines.Strings(1.5, "rich", 3, pull)) == "agree"
        assert engines.strings_agreement(rich, engines.Strings(-1.5, "cheap", 3, pull)) == (
            "disagree"
        )
        assert engines.strings_agreement(rich, engines.Strings(0.3, "slack", 3, pull)) is None
        assert engines.strings_agreement(fair, engines.Strings(1.5, "rich", 3, pull)) is None

    def test_strings_tie_only_correlated_names(self):
        n = 120
        m = market(n)
        grid = engines.Grid.build({
            "A": walk([x + 0.2 * e for x, e in zip(m, market(n, 21), strict=True)]),
            "B": walk([x + 0.2 * e for x, e in zip(m, market(n, 22), strict=True)]),
            "NOISE": walk(market(n, 99)),
        })
        s = engines.strings(grid, "A", 48)
        assert s.strung == 1 and s.tautest.partner == "B"
        assert engines.strings(grid, "NOISE", 48) is None

    def test_liquidity_reads_recent_money_against_the_window(self):
        n, k = 60, engines.liquidity_recent_bars(48)
        hot = [1e6] * (n - k) + [3e6] * k
        thin = [1e6] * (n - k) + [2e5] * k
        grid = engines.Grid.build({
            "H": walk(market(n), money=hot),
            "T": walk(market(n, 2), money=thin),
            "N": walk(market(n, 3)),
        })
        assert engines.liquidity(grid, "H", 48).label == "HOT"
        assert engines.liquidity(grid, "T", 48).label == "thin"
        assert engines.liquidity(grid, "N", 48).label == "normal"

    def test_session_is_measured_from_the_utc_day_open(self):
        t0 = 10 * DAY - 3 * HOUR  # three bars before midnight, three after
        candles = walk([0.01] * 6, start=100.0, t0=t0)
        grid = engines.Grid.build({"A": candles})
        day_open = candles[3].o
        assert engines.session_change(grid, "A") == pytest.approx(candles[-1].c / day_open - 1)


# --- the carpet -----------------------------------------------------------------


class TestCarpet:
    def test_it_reads_the_screenshots_twelve_names(self):
        carpet = build_carpet(info=SyntheticInfo(), now_ms=DEMO_NOW_MS)
        assert carpet["group"]["label"] == "HL Crypto"
        assert {n["coin"] for n in carpet["names"]} == set(GROUPS["hl-crypto"].names)
        assert all(n["rotation"] for n in carpet["names"])
        assert "not predictive" in carpet["notice"]

    def test_the_cross_shares_add_up_and_chips_are_only_mid_turn_names(self):
        carpet = build_carpet(info=SyntheticInfo(), now_ms=DEMO_NOW_MS)
        cross = carpet["cross"]
        assert sum(cross["shares"].values()) == pytest.approx(1.0, abs=1e-3)
        assert cross["right_share"] == pytest.approx(
            cross["shares"]["LEADING"] + cross["shares"]["WEAKENING"], abs=1e-3
        )
        assert cross["summary"].startswith("THE CROSS · ")
        assert cross["chips"]
        for chip in cross["chips"]:
            assert chip["quadrant"] in ("IMPROVING", "WEAKENING")
            assert chip["web"] in ("rich", "cheap")
            assert chip["text"].endswith(chip["quadrant"])

    def test_a_name_with_no_candles_is_reported_missing_not_drawn(self):
        carpet = build_carpet(info=SyntheticInfo(missing=("STRK",)), now_ms=DEMO_NOW_MS)
        assert "STRK" not in {n["coin"] for n in carpet["names"]}
        assert carpet["missing"][0]["coin"] == "STRK"

    def test_unknown_money_is_not_guessed_and_not_counted(self):
        names = [
            {"coin": "A", "money_usd": 100.0, "rotation": {"quadrant": "LEADING"},
             "web": {"label": "fair"}},
            {"coin": "B", "money_usd": None, "rotation": {"quadrant": "LAGGING"},
             "web": {"label": "cheap"}},
        ]
        cross = the_cross(names, "G")
        assert cross["shares"]["LEADING"] == 1.0 and cross["counts"]["LAGGING"] == 0

    def test_reading_text_matches_the_tooltip_pattern(self):
        carpet = build_carpet(info=SyntheticInfo(), now_ms=DEMO_NOW_MS)
        for n in carpet["names"]:
            assert n["reading"].startswith(f"{n['web']['label'].upper()} to HL Crypto · ")
            assert f"{n['rotation']['quadrant']} — " in n["reading"]

    def test_top_n_group_is_chosen_by_money(self):
        carpet = build_carpet("hl-top20", info=SyntheticInfo(), now_ms=DEMO_NOW_MS)
        assert carpet["group"]["names"][:2] == ["BTC", "ETH"]

    @pytest.mark.parametrize(("group", "read_on"), [("nope", "1h"), ("hl-crypto", "7m")])
    def test_bad_settings_are_refused(self, group, read_on):
        with pytest.raises(ValueError):
            build_carpet(group, read_on, info=SyntheticInfo(), now_ms=DEMO_NOW_MS)


# --- the chart ------------------------------------------------------------------


def bar(t, o, h, low, c, v=1.0):
    return Candle(t=t, o=o, h=h, l=low, c=c, v=v)


class TestChart:
    def test_ema_starts_after_its_span_and_skips_gaps(self):
        line = ema([1.0, 1.0, None, 1.0, 1.0], 3)
        assert line[:3] == [None, None, None] and line[4] == 1.0

    def test_vwap_resets_each_utc_day(self):
        candles = [bar(DAY - HOUR, 10, 10, 10, 10, 1), bar(DAY, 20, 20, 20, 20, 1)]
        assert session_vwap(candles) == [10.0, 20.0]

    def test_up_rung_line_never_sees_an_unfinished_rung_bar(self):
        step = 300_000
        candles = [bar(i * step, 1, 1, 1, float(i), 1) for i in range(6 * 40)]
        rung, line = up_rung_ema(candles, "5m")
        assert rung == "30m"
        # A bar inside rung bucket k only ever carries the EMA through bucket k-1.
        for i, c in enumerate(candles):
            if line[i] is not None:
                assert line[i] <= (c.t // 1_800_000) * 6 - 1

    def test_three_touches_make_a_box_and_a_close_through_breaks_it(self):
        candles, t = [], 0
        # Three swing highs at ~110, each with a wick, then a close above.
        for _ in range(3):
            for p in (100, 102, 104, 110, 104, 102, 100):
                candles.append(bar(t, p - 1, p, p - 2, p - 1))
                t += 1
        candles.append(bar(t, 109, 115, 108, 114))
        piv = pivots(candles)
        result = boxes(candles, piv)
        [box] = [b for b in result["boxes"] if b["side"] == "SELL"]
        assert box["touches"] == 3 >= MIN_TOUCHES
        assert box["broken_at"] == len(candles) - 1
        assert box["label"] == "BOX ×3 · BROKEN"
        assert len(result["touches"]) == 2  # the retests, not the first swing

    def test_tpr_is_a_probe_past_the_last_swing_that_closes_back(self):
        seq = [(99, 100, 98, 99), (100, 101, 99, 100), (101, 103, 100, 102), (102, 110, 101, 104),
               (103, 104, 100, 101), (101, 102, 99, 100), (100, 101, 98, 99),
               (99, 100, 97, 98), (98, 99, 96, 97), (99, 112, 98, 108),
               (104, 105, 100, 101), (100, 101, 98, 99), (99, 100, 97, 98)]
        candles = [bar(i, *s) for i, s in enumerate(seq)]
        [tpr] = tprs(candles, pivots(candles))
        assert tpr["side"] == "high" and tpr["swept"] == 110 and tpr["price"] == 112

    def test_heat_marks_volume_spikes_by_direction(self):
        candles = [bar(i, 1, 1, 1, 1, 1) for i in range(60)]
        candles.append(bar(60, 2, 2, 1, 1, 5))
        [h] = heat(candles)
        assert h["i"] == 60 and h["side"] == "down" and h["rel"] == 5

    def test_build_chart_carries_every_study(self):
        chart = build_chart("BTC", "5m", 800, info=SyntheticInfo(), now_ms=DEMO_NOW_MS)
        assert len(chart["candles"]) == 800
        assert chart["headline"].startswith("BTC · 5m · 800 bars · 66.7 h of tape · up-rung 30m")
        assert chart["boxes"] and chart["tpr"] and chart["heat"]
        assert all(b["touches"] >= MIN_TOUCHES for b in chart["boxes"])
        assert "not an instruction" in chart["notice"]

    def test_unsupported_interval_is_refused(self):
        with pytest.raises(ValueError):
            build_chart("BTC", "30s", info=SyntheticInfo(), now_ms=DEMO_NOW_MS)


# --- the endpoint, the deploy, the page -----------------------------------------


class TestEndpoint:
    def respond(self, query, info=None):
        from api.carpet import respond

        return respond(query, info=info or SyntheticInfo())

    def test_carpet_and_chart(self):
        status, carpet = self.respond("group=hl-crypto&read_on=1h")
        assert status == 200 and carpet["tool"] == "the-carpet"
        status, chart = self.respond("chart=eth&interval=15m&bars=200")
        assert status == 200 and chart["coin"] == "ETH" and len(chart["candles"]) == 200

    def test_bad_request_is_a_400(self):
        status, body = self.respond("group=nope")
        assert status == 400 and "unknown group" in body["error"]

    def test_upstream_down_is_a_503(self):
        class Down(SyntheticInfo):
            def asset_contexts(self):
                raise HyperliquidError("nope")

        status, body = self.respond("", info=Down())
        assert status == 503 and "Hyperliquid" in body["error"]

    def test_the_function_imports_stdlib_and_rotation_only(self):
        files = [REPO_ROOT / "api" / "carpet.py", *(REPO_ROOT / "rotation").glob("*.py")]
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    roots = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    assert root in sys.stdlib_module_names or root in ("rotation", "__future__"), (
                        f"{path.name} imports {root}"
                    )

    def test_vercel_ships_the_function_with_its_package(self):
        config = json.loads((REPO_ROOT / "vercel.json").read_text())
        assert "rotation" in config["functions"]["api/carpet.py"]["includeFiles"]

    def test_the_carpet_project_alone_serves_the_carpet_at_its_root(self):
        """Two Vercel projects build this repo from one vercel.json, so the root
        rewrite keys on the host: the-carpet gets the Carpet at /, while
        crypto-screener keeps the memecoin screener there."""
        import re

        config = json.loads((REPO_ROOT / "vercel.json").read_text())
        [rule] = [r for r in config["rewrites"] if r["source"] == "/"]
        assert rule["destination"] == "/carpet.html"
        [host] = [h["value"] for h in rule["has"] if h["type"] == "host"]
        for carpet_host in (
            "the-carpet.vercel.app",
            "the-carpet-fean.vercel.app",
            "the-carpet-git-claude-screener-tool-reverse-en-8e99b7-fean.vercel.app",
            "the-carpet-a1b2c3d4e-fean.vercel.app",
        ):
            assert re.fullmatch(host, carpet_host), carpet_host
        for screener_host in (
            "crypto-screener-fean.vercel.app",
            "crypto-screener-git-claude-screener-tool-reverse-en-8e99b7-fean.vercel.app",
            "the-carpet.vercel.app.evil.com",
        ):
            assert not re.fullmatch(host, screener_host), screener_host


class TestPageAndDemo:
    def page(self):
        return (REPO_ROOT / "web" / "carpet.html").read_text()

    def test_the_page_says_what_it_is_and_has_no_order_ticket(self):
        page = self.page()
        assert "Descriptive, not predictive" in page
        assert "SYNTHETIC DEMO — not market data" in page
        for ticket in ("STOP", "TRAIL", "GTC", "▲ BUY"):
            assert ticket not in page

    def test_the_demo_file_is_marked_synthetic_and_current(self):
        stored = json.loads((REPO_ROOT / "web" / "carpet-demo.json").read_text())
        assert stored["synthetic"] is True
        assert stored["carpet"]["synthetic"] is True
        fresh = json.loads(json.dumps(demo_payload()))
        assert stored == fresh, "web/carpet-demo.json is stale: python -m rotation.synthetic"
