# Should x.com messages go into the screener?

**Short answer: yes, but not the part that is already free — and the reason is a
number from this repo's own graveyard, not from the published literature.**

The screener already reads one thing from X: whether the token *declares* an X
account. That feature is finished. It carries essentially no information at the
point this screener looks at a token, and the measurement below says why. What is
not collected, and what Pillar A is entirely made of, is the *message series*:
mentions per hour, unique authors, reply ratio, tier-1 crossover. That needs a
paid credential. This document is the case for spending it, the case against, and
what was built so the decision is one environment variable either way.

---

## 1. What the collected data says about the free half

85 tokens, snapshotted at first observation above $250k mcap between 2026-09-12
and 2026-09-14. Declared-social coverage in that sample:

| Declared | Share of the 85 |
|---|---|
| X / Twitter | **91.8%** (78 of 85) |
| Website | 74.1% (63) |
| Telegram | 28.2% (24) |

`calibration/backtest.py` against a 1.5x-in-6h outcome (76 tokens with a resolved
label, base rate 28.9% [20.0%, 40.0%]):

| Feature | AUC | 95% CI | Tie mass | Verdict |
|---|---|---|---|---|
| `declared_x` | 0.460 | [0.32, 0.60] | **92%** | no information |
| `declared_telegram` | 0.383 | [0.25, 0.52] | 70% | no separation |
| `socials_declared_n` | 0.324 | [0.20, 0.45] | 46% | see below |

**The X boolean is exhausted.** 92% of rows share one value, so it does not rank
the sample; the interval spans 0.5. There is nothing further to learn from
whether a token above $250k has an X link, because nearly all of them do.

## 2. The declared-socials effect points the *other way* here, and that is not a contradiction

CLAUDE.md records a 17.4x graduation lift for all three socials declared
(1.919% vs 0.110%). In this sample the surge rate falls as declared socials rise:

| Declared socials | Reached 1.5x in 6h | Rate | 95% CI |
|---|---|---|---|
| 0 | 1 / 3 | 0.33 | [0.06, 0.79] |
| 1 | 10 / 18 | 0.56 | [0.34, 0.75] |
| 2 | 8 / 35 | 0.23 | [0.12, 0.39] |
| 3 | 3 / 20 | 0.15 | [0.05, 0.36] |

Both can be true, and the reason matters more than either number.

The published lift is measured **over the launch population**, where the question
is "does this token ever reach ~$69k". This sample is **conditioned on already
being above $250k** — every token in it has already cleared that bar. Conditioning
on survival is conditioning on a collider, and a collider can reverse a sign: the
tokens that got to $250k *without* a full social kit had to get there some other
way, which selects for something real, while a complete social kit is also what a
professionally-launched token that is already being distributed looks like.

Read it as a warning, not a finding. Three of the four cells have single-digit
numerators and the intervals overlap. What it does establish is that **the
published lift must not be carried into this lifecycle point unexamined**, which
is precisely what Pillar C does today when it scores the declared triple at
90/65/45/5.

## 3. What is actually missing

Pillar A — **weight 0.28, the largest single weight in the vector** — reads six
fields:

```
mentions_6h  mentions_24h  unique_authors_24h
follower_weighted_reach  tier1_organic_engagements  reply_to_post_ratio
```

All six are null on **every row ever collected**. Pillar A has never resolved, not
once. The composite score every token carries is a renormalised average over the
pillars that did resolve, which is to say the screener's largest weight has never
contributed to a single number it has produced.

That is the gap. It is not "X sentiment would be nice"; it is that the model
described in `prompts/score.md` has never been run.

## 4. Why it cannot be deferred

BUILD_BRIEF.md §1 is blunt about this and it is the one argument that decides it:
**social history for micro-caps is not backfillable.** On-chain history can be
reconstructed from Bitquery, Dune or Helius whenever somebody gets round to it.
The number of accounts that mentioned a ticker at 14:00 last Tuesday is archived
nowhere reachable — X full-archive search is enterprise-tier, and LunarCrush and
Santiment index roughly 4,000 *tracked* assets, which a three-hour-old launch is
not and never will be if it dies.

So every hour the X collector does not run is an hour of Pillar A that no amount
of later spending recovers. That asymmetry is why Phase 0 exists at all.

## 5. The cost, honestly

- X API **v2 recent search** (`/2/tweets/search/recent`, last 7 days) is what
  `collectors/social_x.py` uses. Seven days covers the t+0/+1h/+6h/+24h series
  with room to spare.
- Recent search is **not on the free tier**. It needs a paid tier, and the paid
  tiers meter **posts read per month**, not requests. One search returns up to 100
  posts, so the cap binds on how *loud* the tokens are, not on how many there are.
- Check the current tiers and prices at <https://developer.x.com/en/products/x-api>
  before committing — they have moved more than once and nothing in this repo
  should be trusted as a quote.

Sizing it against this collector, so the number is concrete rather than a shrug:
the cycle runs hourly, snapshots roughly 25–40 tokens a day, and polls each at
four offsets. That is on the order of **100–160 searches a day**, each returning
up to 100 posts. Against a monthly post cap in the low tens of thousands, the
budget binds quickly, which is why `--x-max-searches` exists.

## 6. What was built

The decision is now one environment variable in either direction.

- **`collect.py` runs the X collector on the same hourly cycle as everything else,
  and only when `X_BEARER_TOKEN` is set.** With no token, the cycle summary says
  `{"social_x": {"skipped": "X_BEARER_TOKEN is not set"}}` — stated rather than
  silently absent, because a series nobody is collecting and a series of zeroes
  look identical in a row count afterwards.
- **`X_MAX_SEARCHES_PER_CYCLE` (or `--x-max-searches`) caps the API calls one
  cycle may make.** Every other source here is keyless and free; this one bills.
  An unbudgeted first cycle over a backlog can spend a month's quota before
  anybody reads the log.
- **A poll stopped by the budget writes nothing.** An error row means "we asked and
  it failed", which is an observation about the token; a row saying the same about
  a poll that was never attempted would put this repo's own rate limit into the
  dataset as if it were a fact about the token. The skipped count is returned to
  the caller, where a budget belongs.
- **A poll that fails for any reason writes a row with `error` set** — including
  an exception type the module did not anticipate, which previously escaped the
  loop and silently skipped every remaining snapshot in the cycle.

## 7. The recommendation

**Switch it on.** The argument is not that X mentions predict price — this repo has
measured nothing of the sort and says so everywhere. It is that:

1. the largest weight in the scoring model has never had an input;
2. the data to give it one cannot be bought later at any price; and
3. the free half of the X signal is demonstrably exhausted at 92% tie mass.

Two conditions on switching it on:

- **Set a budget.** `X_MAX_SEARCHES_PER_CYCLE=40` is a sane start: it covers the
  t+0 polls for a day's new snapshots without letting a backlog run away.
- **Do not raise Pillar A's weight because it finally resolves.** A weight of 0.28
  on a pillar that now produces a number is still a guess, and it will be a guess
  that moves every score for the first time. `.claude/rules/stats.md` admits only
  fitted coefficients, and this changes nothing about that.

## 8. What was deliberately not done

- **No X sentiment scoring, and no LLM reading tweets.** `prompts/score.md` Pillar A
  is about counts and their derivatives — author diversity, mention slope, reply
  ratio. Raw counts are stored and derived at read time so a formula change
  re-interprets every row already collected. A sentiment score baked in at write
  time would be a derived number that could never be recomputed.
- **No scraping.** Nitter-style mirrors are unreliable and are not a licence to
  read X. The paid API or nothing.
- **Pillar C's declared-socials weighting was left alone**, despite §2. It is a
  prior from published data and replacing it needs a fit, not a sample of 85 with
  three tokens in one cell. It is flagged here and in `prompts/score.md` instead.
