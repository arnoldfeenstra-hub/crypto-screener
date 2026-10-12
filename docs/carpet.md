# The Carpet: reverse-engineered from two screenshots

`web/carpet.html` · `api/carpet.py` · `rotation/`

The Carpet is a rotation screener for Hyperliquid perps. It is rebuilt from two
screenshots: a treemap titled "THE CARPET" and a futures chart with boxes and turn
markers. It has no source code to go on, so every rule below was inferred from what
the screen shows. This file records each inference, the evidence for it, and how sure
it is. If a better screenshot contradicts a rule, change the rule here first.

It is a separate tool from the memecoin collector, and it runs under the same hard
rules: it is descriptive, it holds no keys, it has no order path, and missing data stays
missing.

## What the screenshots show

**Screenshot 1, the carpet.** It is a treemap of 12 Hyperliquid names: SOL, XRP, HYPE,
BTC, PUMP, ENA and ETH in a teal IMPROVING region; NEAR and ZEC in a green LEADING region;
ZRO and W in a yellow WEAKENING region; and STRK in a red strip.

- **Controls:** SOURCE *HL Crypto*, COLOUR *THE ROTATION · the quadrant*, READ ON
  *1H · two sessions*, LAYOUT *ROTATION · the quadrants*, SIZE *the money traded*, and
  buttons for REFRESH, PRESENT, ADAM, PANIC and THE KEY.
- **Header:** *12 names · 1 group · the engines on 1H · 5.7s · 10:14:49 AM ET*.
- **Cross line:** *THE CROSS · 28% of the money on the right (LEADING + WEAKENING) ·
  22% LEADING · 68% IMPROVING*.
- **Chips:** six of them, for example *W HL Crypto RICH to HL Crypto · the strings agree
  (tautest vs ENA) · HOT liquidity · WEAKENING* and *HYPE HL Crypto CHEAP to HL Crypto ·
  IMPROVING*.
- **Tooltip (ZEC-USD):** the session +2.20% · THE WEB *rich* · THE STRINGS *stretched
  rich* · THE LIQUIDITY *thin* · THE ROTATION *LEADING*, followed by *RICH to HL Crypto ·
  thin liquidity · LEADING — the premium still chased: the fade the study paid*.

**Screenshot 2, the chart.** It is NQ futures on 30s and 5s bars.

- **Headline:** *800 bars · 13.3 h of tape · up-rung 5m · THE MINUTE STANDS IN — the
  seconds are filling*.
- **Lines and zones:** a white line hugging price, a magenta line, and a smooth cyan
  line. There are shaded zones labelled *BOX ×6 · BUY*, *BOX ×6 · SELL* and *BOX ×N ·
  BROKEN*, with *BOX* square markers at retests and *TPR* dots at swing extremes.
- **Below the chart:** a red and amber strip under the bars, and an order ticket.

## The rotation (high confidence)

**Evidence:**

- The four quadrant names are the standard relative rotation graph (RRG) names.
- "28% of the money on the right (LEADING + WEAKENING)" says that LEADING and WEAKENING
  share a side, which is RS-Ratio > 100 on an RRG.
- The quadrant layout puts IMPROVING above the red strip on the left, and LEADING above
  WEAKENING on the right. That is the RRG plane, with momentum on the vertical axis.

**Rule:** benchmark = equal-weight mean log return of the group. rs = cumulative excess
log return. RS-Ratio = 100 + z(rs) over the window. RS-Momentum = 100 + z(Δ RS-Ratio over
W/6 bars) over the window. JdK's exact smoothing is unpublished; this is the usual open
approximation, so values sit around 97–103 rather than JdK's scale.

**Layout:** left column width = money on the left; within each column the rows split by
money; inside each quadrant the tiles are a squarified treemap.

## The web (medium confidence)

**Evidence:**

- Its labels are *rich* and *RICH/CHEAP to HL Crypto*, so it is valuation relative to
  the group.
- It mostly agrees with the quadrant side (ZEC is rich and LEADING; SOL, BTC and HYPE are
  cheap and IMPROVING), but not always. ETH, XRP and PUMP are IMPROVING with no chip, so
  they must read "fair". The web is therefore close to RS-Ratio but is a separate test
  with a dead band.
- "The web" suggests every name tied to one hub.

**Rule:** regress the name's returns on the benchmark's over the window and sum the
residuals in standard errors (z). |z| ≥ 1 is rich or cheap, otherwise fair. Beta
adjustment is the inferred difference from RS-Ratio: a 1.3-beta name rallying with the
group is not rich.

## The strings (medium confidence on the idea, low on the details)

**Evidence:**

- *the strings agree (tautest vs ENA)* names one other coin, so the strings are pairwise,
  and "tautest" is the most stretched pair.
- *stretched rich* is a graded version of the web's vocabulary.
- *agree* means they lean the same way as the web.

**Rule:** one string to every other name with return correlation ≥ 0.5. Each string is
the beta-hedged spread, z-scored within its window. The strings' verdict is the
correlation-weighted mean z: ±1 leans, and ±2 is "stretched". Tautest = max |z|. The
strings agree (or disagree) when both they and the web lean.

## The liquidity (low confidence)

**Evidence:** the labels are *HOT* and *thin*. HOT shows up on the names with the most
money (SOL, BTC), but ZEC is large too and reads thin. That points to a measure of
volume *relative to the name's own norm*, not absolute size.

**Rule:** money per bar over the last W/8 bars against the window's median bar. ≥ 1.5×
is HOT, ≤ 0.67× is thin. The tooltip also shows Hyperliquid's impact spread, the cost of
crossing the book. That cost is the other plausible meaning of "thin", so it is shown
rather than guessed at.

## The cross (high confidence)

**Evidence:** every chip in the screenshot is IMPROVING or WEAKENING, the two quadrants a
name passes through while crossing sides. Every chip has a RICH or CHEAP web. IMPROVING
names with no chip (ETH, XRP, PUMP) would be fair.

**Rule:** a chip is shown when the quadrant is IMPROVING or WEAKENING *and* the web is not
fair. The chip text is web · strings agreement (if any) · liquidity (if not normal) ·
quadrant, which is the screenshot's order.

## The session, the money, the read-on

- **The session:** last close against the first bar of the current UTC day. The
  screenshot shows ET times, so its session may open at 18:00 ET instead. This is
  configurable in code, and UTC was chosen because Hyperliquid's daily candle uses it.
- **The money traded:** Hyperliquid's 24h notional volume (`dayNtlVlm`). A name with no
  reported size is listed beneath the carpet, never drawn at a guessed size.
- **READ ON 1H · two sessions:** 48 one-hour bars. The other presets follow the same
  pattern: 15m and one session (96), 4h and a week (42), 1d and a month (30).

## The chart (screenshot 2)

| On screen | Rebuilt as | Confidence |
|---|---|---|
| white line hugging price | EMA 9 on the chart's bars | medium |
| magenta line | session VWAP, reset at 00:00 UTC | medium |
| cyan line, smooth, "up-rung 5m" | EMA 20 of the next bar size up, built from closed rung bars only | medium |
| BOX ×N · BUY / SELL | Zone opened by a swing pivot's wick: a swing low gives BUY, a swing high gives SELL. Later pivots within one typical bar range count as touches, and three or more touches make a box | medium |
| BROKEN | a close through the box's far side | high |
| BOX square markers | each retest touch on a drawn box | high |
| TPR | a swing that probes past the previous swing on its side and closes back inside it. The acronym's expansion is unknown | low |
| red / amber strip | bars with volume ≥ 2× the trailing 50-bar median, red when the bar closed down and amber when it closed up | medium |
| "THE MINUTE STANDS IN — the seconds are filling" | Hyperliquid has no sub-minute candles, so this rebuild's floor is 1m and there is no 5s or 30s chart | — |

## Deliberately not reproduced

- **The order ticket** (STP, STOP, TRAIL, DAY/GTC, BUY). Hard rule 4: this repo places
  no orders. `tests/test_rotation.py` checks that the page carries none of it, and that
  the Hyperliquid reader can reach only the read-only `/info` URL, sends only two
  allowlisted queries, and signs nothing.
- **"the fade the study paid".** That is a backtest claim. No study exists here, and
  per `.claude/rules/stats.md` none would count without an out-of-sample split, so the
  phrase is replaced by a description of the state.
- **ADAM, PANIC, and the HQ / Pocket / Scanner / Battle Station / Analyst / Radar / Pairs
  tabs.** A still frame shows nothing of what they do.

## Data

The data source is Hyperliquid's public `POST /info`, using `metaAndAssetCtxs` and
`candleSnapshot`, with no key. This differs from `collectors/httpjson.py`, which is
GET-only; the reason is in the docstring of `rotation/hyperliquid.py`.

`web/carpet-demo.json` is a **synthetic** market from `rotation/synthetic.py`. The page
falls back to it, under a red SYNTHETIC banner, when `/api/carpet` is not deployed.
Regenerate it with `python -m rotation.synthetic`; a test fails when it is stale.
