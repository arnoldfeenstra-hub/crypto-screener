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
| 0.7 safety source (GoPlus + RugCheck, keyless) | Built, answers 6 of the 8 hard filters |
| 0.8 scheduled collector + append-only journal | Built, `collect.py` + GitHub Actions |
| 1 hard filters | Built, 8/8 filters, and they now answer |
| 1 scoring runner | Built, paper mode only |
| 2 calibration (fit + report) | Built, gated on Phase 0 exit criteria |
| 3 live ranking | **Not built, and should not be** — gated on Phase 2 measuring an edge |
| Web viewer | Built, `web/` + `api/screener.py`, chain and mindshare filters |

554 tests, no network, ~8s. `ruff` clean, and the suite is *enforced* offline: `tests/conftest.py` blocks real requests, so a test that reaches the internet fails loudly instead of passing on someone else's uptime.

## Run it against real data

No API key, no signup. DexScreener, GoPlus and RugCheck are all keyless. One command does
a whole cycle — restore, poll, re-price, label, score, export, journal:

```bash
python -m collect --chains solana,bnb,base --regime neutral --export
python -m collect --summary-only          # what the journal holds
```

Run that on a schedule and Phase 0 actually accumulates. `.github/workflows/collect.yml`
does it every 30 minutes and commits the journal back to the repo; read the cost note at
the top of that file before leaving it on for a private repo.

The individual steps still exist if you want them:

```bash
python -m collectors.dexscreener --probe --chains solana,bnb   # see what comes back
python -m collectors.dexscreener --discover-chains             # every chainId it returns
python -m collectors.safety --probe --chain bnb 0xTOKEN        # what the filters can answer
python -m collectors.trigger_watcher --chains solana,bnb,base --once --db data/screener.duckdb
python -m scoring.runner --db data/screener.duckdb --safety --regime neutral
python -m collectors.outcomes --reprice --refresh-labels --db data/screener.duckdb
python -m export_web --db data/screener.duckdb
```

## Where the dataset lives

`state/*.jsonl` — an append-only JSONL journal, committed to the repo. It is rebuilt into
DuckDB at the start of each run and appended to at the end, so the database is a working
copy and the journal is the dataset.

JSONL rather than the DuckDB file because the DuckDB file is one binary blob rewritten in
full on every run: a job firing twice an hour would add a multi-megabyte object to git
history every time. A journal appends lines. It is cheap in git, greppable by a human, and
append-only *in the file format* rather than as a promise about SQL — which is hard rule 1
expressed as a file layout.

## Safety — what makes the filters answer

Six of the eight hard filters are facts about a contract, not predictions. Until
`collectors/safety.py` existed none of them had a source, so every row was excluded as
*unmeasured* rather than judged — honest, but not a screen.

- **GoPlus Token Security** covers the EVM chains *and* Solana with one keyless API:
  honeypot, buy/sell tax, mint and freeze authority, LP holders and their lock state, top
  holders, proxy and ownership.
- **GoPlus address security** on the deployer wallet answers `deployer_history` — the one
  filter no token-level API can. Deduplicated per deployer and capped, because keyless
  GoPlus is 30 requests a minute.
- **RugCheck** is a genuine second opinion on Solana: LP locked percentage per market and a
  `rugged` flag GoPlus has no equivalent for.

Two things are deliberately **not** concluded:

- **A locked LP is not a 30-day lock.** Neither API reports a lock expiry, and that is
  exactly what `check_liquidity_lock` asks about. A burn address holding the LP gives
  `lp_burned=True` — burning is irreversible, so the question does not arise. "Locked,
  expiry unknown" stays unknown.
- **Deployer history on Solana stays unknown.** GoPlus address security covers EVM
  addresses only. Unknown excludes; it is not a claim that the deployer is clean.

Where the two sources disagree, `merge` takes the unsafe answer and never lets an unknown
overwrite a measurement. Sources disagree because one of them is stale, and picking the
reassuring one is how a screen quietly stops screening.

Safety runs **after** the trigger, never before it. GoPlus reports holder counts on EVM and
not on Solana; if that reached the trigger, the 500-holder condition would be fireable on
one chain and not another, and a pooled cross-chain sample with a chain-dependent entry rule
cannot be interpreted.

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

### Push to GitHub, deploy to Vercel

Two ways. **Pick one** — with both switched on, every push deploys twice.

**A. `.github/workflows/deploy.yml` (in the repo).** Add three repository secrets under
*Settings → Secrets and variables → Actions*:

| Secret | Where it comes from |
|---|---|
| `VERCEL_TOKEN` | Vercel → Account Settings → Tokens → Create |
| `VERCEL_ORG_ID` | `.vercel/project.json` after one `vercel link`, or Project → Settings |
| `VERCEL_PROJECT_ID` | same place |

Then every push deploys: production on the default branch, a preview URL on any other
branch. Until those secrets exist the job succeeds with a notice saying what to set,
rather than putting a red cross on every push.

It also runs daily at 06:20 UTC. That is not redundant: the collector commits with
`GITHUB_TOKEN`, and a push made with that token never starts another workflow, so without
the schedule the dataset it accumulates would sit in the repo and never reach the page.
Before deploying, it re-imports `api/screener.py` with `duckdb`, `requests`, `numpy` and
`pandas` blocked — that function runs on Vercel with nothing installed, and an accidental
import of one of them is the failure that builds fine and then 500s on every request.

**B. Vercel's own Git integration.** One click in the Vercel dashboard, no secrets. Simpler,
but it deploys on *every* push — including the collector's data commit every 30 minutes,
roughly 48 a day against a Hobby limit of 100. Put `[skip ci]` in `collect.yml`'s commit
message if you want to suppress those.

### Deploying by hand

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
Optimism, Blast, Sui, TON, Tron.

**Robinhood Chain.** Registered as an EVM chain (it is an Arbitrum Orbit rollup, so that
much is a property of the chain). Its DexScreener `chainId` is *not* hardcoded, because
nobody here has seen DexScreener return one and a guessed string produces the worst outcome
available: a request that quietly matches nothing, indistinguishable from a quiet chain. So
it is configuration, and switching it on takes two commands and no code change:

```bash
python -m collectors.dexscreener --discover-chains     # prints every chainId seen
export SCREENER_CHAIN_IDS="robinhood=<the id it printed>"
python -m collect --chains solana,bnb,robinhood
```

The same variable binds any chain the registry does not yet know
(`"robinhood=abc,newchain=def"`), and the GitHub Actions workflow reads it from a repository
variable of the same name. Until it is bound, `--chains robinhood` fails **by name** rather
than collecting nothing: a silent skip and a quiet chain look identical in the counts
afterwards. Note that GoPlus has no chain id for it either, so its safety checks will read
unknown — which excludes — until they do.

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
- **Three of six pillars resolve to null on Phase 0 data**, so a composite score today is
  computed from on-chain structure and asymmetry only, renormalised over what resolved.
- **The GoPlus and RugCheck response shapes are unverified too**, for the same reason as
  DexScreener's, and it matters more here: a price parsed wrong is a wrong number, but a
  safety field parsed wrong is a token that passes a filter it should have failed. The
  parsers are total and every field defaults to `None`, so a shape change degrades to
  *unknown* — which excludes — rather than to a false pass. Run
  `python -m collectors.safety --probe` before trusting a green verdict.
- **Only Solana has a Bitquery source.** DexScreener covers the other chains;
  `BitqueryFeed.poll()` still raises for a non-Solana chain rather than silently collecting
  nothing.
- **The live endpoint stores nothing.** It is a view. Rows shown there are not dataset rows,
  and the page says so; only the collector writes.

## Layout

Follows BUILD_BRIEF.md §5. Extra modules inside `collectors/` (`config`, `schema`, `store`,
`metrics`, `social_base`) are the shared spine the named modules build on.

```
collect.py                 one full cycle: restore, poll, re-price, label, score, journal
collectors/
  chains.py            canonical chain names, EVM flags, DexScreener id mapping
  httpjson.py          shared read-only JSON GET: throttle, retry, no method that writes
  journal.py           append-only JSONL persistence, so a schedule can carry state
  safety.py            GoPlus + RugCheck: what makes six of the eight filters answer
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
web/                       static viewer (Vercel), chain + mindshare + safety
state/                     the dataset, as an append-only JSONL journal (tracked in git)
.github/workflows/         collect.yml (the schedule) and deploy.yml (push -> Vercel)
tests/                     554 tests, network access blocked by conftest
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
