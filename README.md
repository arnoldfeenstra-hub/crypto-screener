# crypto-screener

Memecoin screener built to [CLAUDE.md](CLAUDE.md) and [BUILD_BRIEF.md](BUILD_BRIEF.md).

**What this is: a data collector with a scorer attached.** It watches new Solana pools,
snapshots each token once at a fixed trigger, tracks forward outcomes, filters for safety,
and scores what survives. Per CLAUDE.md the scoring weights are **uncalibrated priors —
guesses** — until Phase 2 replaces them with fitted coefficients, and Phase 2 needs weeks of
forward collection that has not happened. Nothing it emits is a prediction, and there is no
code path that can place an order.

## Status

| Phase | Status |
|---|---|
| 0.1 trigger watcher | Built |
| 0.2 snapshot writer | Built |
| 0.3 social collectors (X + Telegram) | Built |
| 0.4 outcome tracker | Built |
| 0.5 on-chain backfill | Built |
| 1 hard filters | Built, 8/8 filters |
| 1 scoring runner | Built, paper mode only |
| 2 calibration (fit + report) | Built, gated on Phase 0 exit criteria |
| 3 live ranking | **Not built, and should not be** — gated on Phase 2 measuring an edge |
| Web viewer | Built, `web/`, ready for Vercel |

336 tests, no network, ~6s. `ruff` clean.

## Try the whole pipeline offline

No API key needed — a recorded fixture drives it end to end:

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

`vercel.json` and `.vercelignore` follow the same pattern as your zittingsrooster project:
static `web/`, no install step, no build step.

Node 24 and Vercel CLI 59 are installed (winget, user scope). **The remaining step needs
your account** — I can't authenticate as you:

```bash
vercel login
```

Then:

```bash
./deploy.ps1 -Production
```

`deploy.ps1` re-exports the JSON from the database and deploys `web/`. It checks auth first
and stops with instructions rather than failing halfway.

The alternative, matching zittingsrooster exactly, is to push this repo to GitHub and connect
it in the Vercel dashboard — Vercel then rebuilds on every push. That needs a `gh auth login`
first; `gh` is installed but not logged in.

## Running against live data

```bash
cp .env.example .env      # then fill in BITQUERY_TOKEN
python -m collectors.bitquery --probe          # verify the queries against the live schema
python -m collectors.trigger_watcher --chain solana --regime neutral
python -m collectors.outcomes --refresh-labels  # on a schedule
python -m collectors.social_tg                  # no key needed (public t.me previews)
python -m collectors.social_x                   # needs X_BEARER_TOKEN
```

**Probe first.** The three GraphQL queries in `collectors/bitquery.py` are written against
Bitquery's documented Solana EAP schema but have never run against it. The parsers are tested
and total; the query text is the unverified part. `--probe` runs each once and reports what
came back; re-record `fixtures/bitquery_responses.json` from its output if a shape differs,
and the parser tests will point at exactly what to change.

## Honest limitations

- **The Bitquery queries are unverified.** See above. Everything downstream is tested; this
  is the seam where reality gets in.
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
- **Only Solana has a source.** BNB is "same shape" per §1 but a separate query set;
  `BitqueryFeed.poll()` raises rather than silently collecting nothing.

## Layout

Follows BUILD_BRIEF.md §5. Extra modules inside `collectors/` (`config`, `schema`, `store`,
`metrics`, `social_base`) are the shared spine the named modules build on.

```
collectors/
  trigger_watcher.py   0.1 — the trigger rule, and the poll loop around it
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
scoring/runner.py          Phase 1 — filters → pillars → narrative → stored row
calibration/fit.py         Phase 2 — time split, logistic fit, AUC/lift/intervals
calibration/report.py      Phase 2 — the verdict, with two ways to say "no"
export_web.py              DuckDB → web/screener-data.json
web/                       static viewer (Vercel)
tests/                     336 tests, no network
```

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
