# crypto-screener

Memecoin screener built to [CLAUDE.md](CLAUDE.md) and [BUILD_BRIEF.md](BUILD_BRIEF.md).

**What this is: a data collector with a scorer attached.** It watches new pools across
Solana, BNB Chain, Base and Ethereum, snapshots each token once at a fixed trigger, tracks
forward outcomes, filters for safety, and scores what survives. Per CLAUDE.md the scoring
weights are **uncalibrated priors — guesses** — until Phase 2 replaces them with fitted
coefficients, and Phase 2 needs weeks of forward collection that has not happened. Nothing
it emits is a prediction, and there is no code path that can place an order.

Data comes from **DexScreener**, which needs no API key. The deployed page also carries a
live endpoint (`api/screener.py`) that fetches DexScreener at request time, so it shows real
tokens whether or not a collector has ever run.

## Status

| Phase | Status |
|---|---|
| 0.0 live source (DexScreener, keyless, multi-chain) | Built |
| 0.1 trigger watcher | Built |
| 0.2 snapshot writer | Built |
| 0.3 social collectors (X + Telegram) | Built |
| 0.4 outcome tracker | Built |
| 0.5 on-chain backfill | Built |
| 0.6 mindshare (share-of-attention variable) | Built, weight 0.00 in the composite |
| 1 hard filters | Built, 8/8 filters |
| 1 scoring runner | Built, paper mode only |
| 2 calibration (fit + report) | Built, gated on Phase 0 exit criteria |
| 3 live ranking | **Not built, and should not be** — gated on Phase 2 measuring an edge |
| Web viewer | Built, `web/` + `api/screener.py`, chain and mindshare filters |

467 tests, no network, ~6s. `ruff` clean.

## Run it against real data

No API key, no signup. DexScreener is keyless:

```bash
python -m collectors.dexscreener --probe --chains solana,bnb          # see what comes back
python -m collectors.trigger_watcher --chains solana,bnb,base --once --db data/screener.duckdb --regime neutral
python -m scoring.runner --db data/screener.duckdb --regime neutral
python -m export_web --db data/screener.duckdb
```

Leave the watcher running (drop `--once`) and it polls forever, firing one snapshot per
token at first crossing. `python -m collectors.outcomes --reprice --refresh-labels` on a
schedule fills the forward labels.

## Try the whole pipeline offline

A recorded fixture drives it end to end, no network at all:

```bash
python -m collectors.trigger_watcher --replay fixtures/solana_replay.json --cycles 3 --poll-seconds 0 --db data/demo.duckdb --regime neutral
python -m scoring.runner --db data/demo.duckdb --regime neutral
python -m calibration.report --db data/demo.duckdb
python -m export_web --db data/demo.duckdb
```

Then open `web/index.html` (through a local server, since it fetches JSON):

```bash
python -m http.server 8899 --directory web
```

## Deploying to Vercel

The deployment is two halves, both from the **repo root** (not from `web/`):

- `web/` — the static page, served as-is. No build step.
- `api/screener.py` — one Python serverless function that polls DexScreener at request time
  and returns a live, scored ranking. **Stdlib only**, so there is no `requirements.txt` and
  nothing is installed at deploy time; `.vercelignore` deliberately hides `pyproject.toml`
  so the builder does not try.

If the repo is connected to a Vercel project, pushing to the branch deploys it. Otherwise:

```bash
vercel login          # authenticates as you, in a browser; cannot be automated
./deploy.sh --prod    # macOS/Linux
.\deploy.ps1 -Production   # Windows
```

Both scripts re-export `web/screener-data.json` from the database first, check auth, and
stop with instructions rather than failing halfway.

**If the function ever causes trouble**, deleting `api/` and the `functions` block from
`vercel.json` leaves a working static deploy — the page falls back to the exported dataset
on its own and says on the page that the live view is unavailable.

## The other collectors

```bash
python -m collectors.social_tg                  # no key needed (public t.me previews)
python -m collectors.social_x                   # needs X_BEARER_TOKEN
cp .env.example .env                            # BITQUERY_TOKEN, for the Bitquery source
python -m collectors.bitquery --probe           # verify its queries against the live schema
python -m collectors.trigger_watcher --source bitquery --chain solana
```

Bitquery is still wired up and is the only source that can answer holder counts, but it needs
a paid key and its three GraphQL queries have never run against the live endpoint. DexScreener
is the default source because it needs neither.

## Mindshare

A token's **share of the attention observed across the tokens polled with it**, from three
raw components — 24h transaction count, 24h volume, and DexScreener boost spend — each taken
as a share of the universe total and averaged over the ones that resolved.

- It is on-chain and paid attention, **not** social mentions. It is not a substitute for the
  X collector and must not be read as one.
- **It carries weight 0.00 in the composite score.** `.claude/rules/stats.md` allows only
  fitted coefficients into the weight vector, and mindshare has no outcome data behind it
  yet. So it is collected, scored, displayed, filtered on and handed to calibration — and it
  changes no score until Phase 2 fits it. `tests/test_mindshare.py` asserts that the
  composite is numerically identical with the pillar present and absent.
- The **raw components and the universe totals they were divided by** are both stored, so the
  share can be recomputed when the formula changes rather than being stranded
  (`collectors.mindshare.recompute_share`).
- **The universe is a biased sample**: a token enters it by being boosted or profiled on
  DexScreener. That is stated on the page and recorded on every row as `universe_size`.

The most informative part is the **organic tilt**: boost spend share divided by trade-count
share. Above 1, more of the token's visibility was bought than traded. Bought mindshare and
earned mindshare are identical in a share number and are opposite signals.

## Chains

`collectors/chains.py` is the single registry. DexScreener calls BNB Chain `bsc` and
BUILD_BRIEF.md section 4 calls it `bnb`; every source normalises through `canonical()` on the
way in, so one chain is never two rows in a `GROUP BY` or two entries in the page's chain
filter. Covered today: Solana, BNB Chain, Ethereum, Base, Arbitrum, Polygon, Avalanche,
Optimism, Blast, Sui, TON, Tron. Robinhood Chain is registered as a named target with no
source — `--chains robinhood` fails by name rather than collecting nothing, because a silent
skip and a quiet day look identical in the counts afterwards.

The trigger never sees the chain, so tokens from every chain enter the sample on identical
terms. `filters/hard_filters.py` asks the registry whether a chain is EVM rather than
matching the literal string `"solana"`, so the proxy check is right on Sui and TON too — and
returns *unknown* for a chain the registry has never met, rather than handing it Solana's
free pass.

## Honest limitations

- **The DexScreener response shapes are unverified against the live API.** The parsers are
  tested and total, but `fixtures/dexscreener_responses.json` was written to the documented
  schema, not recorded off the wire — the session that wrote the module had no egress to
  `api.dexscreener.com`. Run `python -m collectors.dexscreener --probe` first; if a shape
  differs, re-record the fixture and the parser tests will point at exactly what to change.
  Because the parsers are total, a shape change degrades to nulls rather than crashing.
- **DexScreener reports no holder counts.** So only the `mcap_250k` half of the trigger can
  fire from it — a missing holder count never crosses 500 — and pillar D loses its holder
  growth component. Visible in `trigger_breakdown`.
- **Discovery is boosted and profiled tokens, not every new pool.** There is no keyless
  new-pool firehose. A token reaches the sample because someone paid to boost it or filled in
  its profile, which is a real selection effect on the population *and* the denominator of
  every mindshare figure. Stated on the page rather than hidden.
- **The Bitquery queries are unverified.** Everything downstream is tested; this is the other
  seam where reality gets in.
- **Telegram message rates and unique speakers are not collected.** The public t.me preview
  gives member and online counts without any credential, which is why the collector runs
  today — but pillar B cares most about *speaker ratio*, and that needs a real Telegram
  client reading group history. `TelegramCollector` takes any object with `fetch(handle)`, so
  that slots in without touching storage or scheduling. This is the highest-value gap.
- **`flows` and `launch` are never populated.** Cohort flow and bundle/sniper analysis need
  heavier per-wallet queries that aren't written. Null, not zero, so the rows stay honest —
  but `data_completeness` sits near 0.35 and the composite is multiplied by it.
- **Three of five pillars resolve to null on Phase 0 data**, so a composite score today is
  computed from on-chain structure and asymmetry only, renormalised over what resolved.
- **Nearly every row is excluded as *unmeasured*, not rejected.** Sellability, LP burn and
  proxy admin come from a safety source (RugCheck / GoPlus / Honeypot.is) that isn't wired
  up. Unknown never passes a filter, so the exclusion is correct — it just isn't evidence.
  The page shows both counts separately.
- **Only Solana has a Bitquery source.** DexScreener covers the other chains;
  `BitqueryFeed.poll()` still raises for a non-Solana chain rather than silently collecting
  nothing.
- **The live endpoint stores nothing.** It is a view. Rows shown there are not dataset rows,
  and the page says so; only the collector writes.

## Layout

Follows BUILD_BRIEF.md §5. Extra modules inside `collectors/` (`config`, `schema`, `store`,
`metrics`, `social_base`) are the shared spine the named modules build on.

```
collectors/
  chains.py            canonical chain names, EVM flags, DexScreener id mapping
  dexscreener.py       the live source: keyless client, pure parsers, multi-chain feed
  mindshare.py         share-of-observed-attention, with the denominators kept
  trigger_rule.py      the trigger rule alone, importable without DuckDB
  trigger_watcher.py   0.1 — the poll loop around it
  snapshot.py          0.2 — §4 schema mapping + read-only inspection CLI
  social_x.py          0.3 — X API v2 recent search, raw counts only
  social_tg.py         0.3 — public t.me previews, no credentials
  social_base.py       0.3 — offsets, raw-count record, read-time derivation
  outcomes.py          0.4 — re-pricing and the five forward labels
  backfill.py          0.5 — historical crossing reconstruction
  bitquery.py          Solana source: client, pure parsers, replay feed
  schema.py store.py metrics.py config.py
filters/hard_filters.py    Phase 1 — the eight checks, three-way outcomes
scoring/pillars.py         Phase 1 — deterministic pillar maths (what Phase 2 fits)
scoring/candidate.py       Phase 1 — snapshot row → candidate packet, no store needed
scoring/prompt_meta.py     Phase 1 — prompt_version and the SYSTEM block
scoring/runner.py          Phase 1 — filters → pillars → narrative → stored row
calibration/fit.py         Phase 2 — time split, logistic fit, AUC/lift/intervals
calibration/report.py      Phase 2 — the verdict, with two ways to say "no"
export_web.py              DuckDB → web/screener-data.json
api/screener.py            Vercel function: live DexScreener → scored ranking (stdlib only)
web/                       static viewer (Vercel), chain + mindshare filters
tests/                     467 tests, no network
```

Four modules were split out so `api/screener.py` can share the repo's real logic instead of
reimplementing it on the serverless side: `collectors/trigger_rule.py`,
`scoring/candidate.py`, `scoring/prompt_meta.py`, and the DuckDB import inside
`collectors/snapshot.py` made lazy. Every original import site still works — the old modules
re-export the moved names, and `tests/test_chains_and_live.py` asserts the endpoint holds the
*same function objects* the collector does, so the live and stored rankings cannot drift.

## The invariants the tests defend

**The trigger rule is identical for every token.** It is a pure function of two numbers:

```python
def evaluate(mcap_usd: float | None, holder_count: int | None) -> TriggerDecision
```

It cannot see the ticker, chain, deployer, socials or clock, because it is not given them.
Checked behaviourally (a decoy differing in every other field decides identically across a
25-case grid) and structurally (the function's AST is parsed and every free name it loads is
asserted against a fixed set). `collectors/backfill.py` imports the same function rather than
reimplementing it, and a test asserts that identity, so the live and historical halves of the
sample cannot drift apart.

**Nothing is ever updated or deleted.** `tests/test_store_append_only.py` parses `store.py`'s
own AST and fails if `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `ALTER`, `REPLACE INTO` or
`ON CONFLICT` appears in any non-docstring string literal. Labels, scores, re-prices and
social counts all live in their own append-only tables for this reason — filling a label later
would otherwise be an edit to a snapshot row.

**Nothing is imputed.** Missing is `null` — never 0, never a mean, never carried forward. A
missing value never crosses a threshold; NaN and infinity are treated as missing rather than
as very large numbers; a failed social poll is stored as a row with an `error`, never as a
count of zero. That last one matters most: social history cannot be backfilled, so a zero
written today is a wrong number nobody can ever correct.

**A horizon that has not elapsed is null.** `max_multiple_7d` from two hours of data would be
the most damaging kind of wrong number — plausible, pessimistic, and mixed in with real ones.

**Calibration cannot flatter itself.** Splits are chronological with no shuffle or seed
parameter to reach for; a deployer straddling the cut is moved wholly into train; AUC averages
ties so a model that separates nothing scores 0.5 rather than 1.0; every figure is
out-of-sample and carries a Wilson interval. A test fits on pure noise at a realistic 2% base
rate and asserts the report says there is no edge.

## What has to happen before any of this means anything

Phase 0 exits at ≥300 tokens with complete social series and ≥20 dead per survivor. The web
page shows progress against exactly that, and `calibration/report.py` refuses to fit below it
unless forced (and says so in the report if forced).

Until then the answer to "does the top decile beat the base rate out of sample" is *unknown*,
and per BUILD_BRIEF.md §3 that is the only question that decides whether this is a screener or
an expensive way to launder a coin flip as a decision.
