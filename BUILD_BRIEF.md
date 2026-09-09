# Build Brief

Hand this to Claude Code. Build phases in order. Do not skip to Phase 3.

---

## 1. The data situation (read first — it determines the architecture)

You asked me to retrieve the graveyard data. Here is what I found, split by what is
actually obtainable:

### Retrievable retroactively ✅

| What | Source | Notes |
|---|---|---|
| Every SOL launch, mcap curve, holders, LP, deployer history | **Bitquery**, **Dune**, **Helius** | Full history. This is a solved query problem. |
| Graduation / survival outcomes | Bitquery cohort queries | Follow a launch-day cohort forward. Do not use daily ratios. |
| BNB Chain equivalents | Bitquery, Dune | Same shape. |
| A pre-built benchmark | SSRN: survival analysis of 832,941 pump.fun launches, May–Jun 2026 | Kaplan-Meier + Cox, concordance 0.858, social-presence effects already quantified. Read this before fitting anything. |

### Not retrievable retroactively ❌

**Per-token social history for micro-caps.** This is the blocker, and it is the exact data
your thesis depends on.

- X/Twitter full-archive search is enterprise-tier and priced accordingly.
- LunarCrush, Santiment: strong historical time series, but coverage is ~4,000 *tracked* assets.
  A token that launched three hours ago is not in the index and never will be if it dies.
- Kaito, The Tie, Messari: enterprise-only, no self-serve signup.
- Telegram member counts and message rates at a past timestamp are archived nowhere public.

**Consequence:** you cannot backfill "mentions/hr for $DEADCOIN on 14 March." You have to
start logging now and let the dataset accumulate. This is why Phase 0 exists.

### Recommended source stack

- **On-chain, live + historical:** Bitquery (streaming + archive), Helius (SOL webhooks)
- **Safety/rug checks:** RugCheck, GoPlus, Honeypot.is
- **Price/liquidity:** DexScreener, Birdeye
- **Regime + meta detection:** LunarCrush (has an MCP server — wire it into `.mcp.json` and
  Claude Code can query it directly), Cookie.fun for memecoin attention flow
- **Per-token social:** first-party collectors you write (X API, Telegram client)

---

## 2. Horizon: you said "not sure yet" — don't decide

Do not pick one. From a single snapshot, log **all** forward labels:

```
max_multiple_1h, max_multiple_6h, max_multiple_24h, max_multiple_72h, max_multiple_7d
max_drawdown_before_peak_24h, max_drawdown_before_peak_72h
time_to_peak_minutes
survived_24h, survived_7d          (bool: still >20% of snapshot mcap)
```

One collector, five label columns. After ~6 weeks you fit five models and look at which
horizon is actually predictable. Almost certainly it will be the short ones — attention
decays fast — but let the data say it rather than guessing now.

The drawdown-before-peak column matters as much as the multiple. A token that 5x'd after
first going -60% is untradeable in practice; without that column your backtest will look
far better than reality.

---

## 3. Phase plan

### Phase 0 — Collector (build this first, run it for 4–6 weeks)

1. **Trigger watcher.** Subscribe to new pools on target chains. Fire a snapshot the moment a
   token first crosses the trigger: *either* $250k mcap *or* 500 holders, whichever first.
   Record which trigger fired. Same rule for every token, no exceptions.
2. **Snapshot writer.** On trigger, capture the full input schema (§4) and append to DuckDB.
3. **Social collectors.** Start polling X and Telegram for every triggered token at t+0, +1h,
   +6h, +24h. Store raw counts, not derived scores — derived formulas will change, raw won't.
4. **Outcome tracker.** Re-price every snapshotted token on a schedule and fill the label
   columns from §2.
5. **Backfill job.** Separately, pull the on-chain-only history from Bitquery for the last
   90 days. This gives you a large labeled set with no social features — useful on its own
   for fitting the structural pillars while social accumulates.

**Phase 0 exit criteria:** ≥300 triggered tokens with complete social series and ≥20 dead
tokens per survivor. Do not proceed early.

### Phase 1 — Filters and scorer (build alongside Phase 0)

- Implement hard filters as pure functions with unit tests. These do not need calibration —
  a honeypot is a honeypot.
- Wire `prompts/score.md` to the API. Run in **paper mode**: score, log, never surface as a
  recommendation.
- Every scored row stores `prompt_version` and the full input snapshot.

### Phase 2 — Calibration

- Fit logistic regression and gradient boosting on the pillar features against each label.
- Report out-of-sample AUC and concordance. Compare against the 0.858 published benchmark.
- Split evaluation by regime (hot/neutral/cold tape). Expect signals to hold in hot and
  collapse in cold — that finding is itself worth having.
- Replace the prior weight vector in `prompts/score.md` with fitted coefficients.

### Phase 3 — Live ranking

Only after Phase 2 shows the top decile beating the base rate out of sample. If it doesn't,
say so plainly and go back to Phase 0 with better features. A screener with no measured edge
is worse than no screener, because it launders a coin flip as a decision.

---

## 4. Snapshot schema

```json
{
  "snapshot_id": "uuid",
  "ts": 0,
  "trigger": "mcap_250k | holders_500",
  "ticker": "$EXAMPLE",
  "chain": "solana | bnb | robinhood",
  "contract": "...",
  "age_at_trigger_minutes": 0,

  "market": { "mcap_usd": 0, "liquidity_usd": 0, "volume_24h_usd": 0, "price_usd": 0 },
  "holders": { "count": 0, "growth_6h_pct": 0, "top10_ex_lp_pct": 0 },
  "authorities": { "mint_revoked": true, "freeze_active": false, "lp_locked_until": null },
  "deployer": { "address": "...", "prior_launches": 0, "prior_rugs": 0 },
  "launch": { "bundled_supply_pct": 0, "sniper_wallets": 0, "initial_buy_sol": 0 },
  "flows": { "net_flow_by_cohort": {}, "smart_money_entries": 0, "smart_money_hit_rate": null },

  "social_x": { "mentions_6h": 0, "mentions_24h": 0, "unique_authors_24h": 0,
                "follower_weighted_reach": 0, "tier1_organic_engagements": 0,
                "reply_to_post_ratio": 0.0 },
  "social_tg": { "exists": true, "members": 0, "member_growth_6h_pct": 0,
                 "msgs_per_hour": 0, "unique_speakers_24h": 0 },
  "socials_declared": { "telegram": true, "x": true, "website": false },
  "trends": { "google_trends_delta": 0, "tiktok_video_count_delta": 0 },
  "lineage": { "meta_tag": null, "position_in_meta": null },
  "listings": ["dex"],

  "regime": "hot | neutral | cold",
  "labels": { "filled_at": null }
}
```

Note `socials_declared` — the cheapest and best-evidenced feature in the whole schema. It is
a boolean triple available at launch, and published data puts a 17.4x graduation lift on all
three being present. Collect it from block one.

---

## 5. Repo layout

```
crypto-screener/
├── CLAUDE.md
├── BUILD_BRIEF.md
├── .mcp.json                    # lunarcrush, bitquery
├── .claude/
│   ├── settings.json            # deny: any network write, any key material
│   └── rules/
│       ├── data-integrity.md    # append-only, no imputation
│       └── stats.md             # no fitting without out-of-sample split
├── prompts/
│   └── score.md                 # versioned scoring prompt
├── collectors/
│   ├── trigger_watcher.py
│   ├── snapshot.py
│   ├── social_x.py
│   ├── social_tg.py
│   └── outcomes.py
├── filters/
│   └── hard_filters.py          # pure, unit-tested
├── scoring/
│   └── runner.py                # paper mode by default
├── calibration/
│   ├── fit.py
│   └── report.py
└── data/                        # gitignored, DuckDB + parquet
```

---

## 6. First command to run in Claude Code

> Read CLAUDE.md and BUILD_BRIEF.md. Scaffold the repo per §5. Implement Phase 0 items 1–2
> only: the trigger watcher and the snapshot writer, for Solana, using Bitquery. Write unit
> tests for the trigger logic — especially that the same trigger rule applies to every token
> and that snapshots are append-only. Stop before implementing social collectors and show me
> the trigger tests.
