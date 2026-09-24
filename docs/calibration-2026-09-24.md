# Calibration, 2026-09-24 — `fitted-v1`

The first time the screener's weight vector was replaced by fitted coefficients. The
machine-readable record is [`scoring/weights/fitted-v1.json`](../scoring/weights/fitted-v1.json);
this page is the reasoning around it.

**In one paragraph.** A logistic fit on 190 tokens, judged on the 82 that triggered after
them, ranks a 1.5x-within-6h move with AUC **0.67 [0.52, 0.83]**, against **0.64 [0.48, 0.79]**
for the priors it replaces. That is a small improvement, measured on one neutral-market
window of 82 tokens with 17 positives, and far below the published benchmark of 0.858. It
was adopted before Phase 0's gate was met, on the owner's instruction to calibrate, because
the gate cannot be met as the collector stands. It is not an edge. And the same ranking,
old or new, marks tokens that are *less* likely to survive a day.

## The vector

| Pillar | Prior (`priors-v3`) | Fitted (`fitted-v1`) | Logistic coefficient | Why |
|---|---|---|---|---|
| Attention velocity | 0.28 | 0.00 | 0 | never observed at a trigger: no X collector ran |
| Community depth | 0.20 | 0.00 | −0.16 | fitted negative on the 40 tokens with a Telegram count; clamped |
| Lineage & meta fit | 0.15 | 0.00 | −0.73 | fitted negative; clamped |
| On-chain structure | 0.22 | **0.55** | +0.57 | |
| Asymmetry & timing | 0.15 | **0.37** | +0.38 | |
| Mindshare | 0.00 | **0.08** | +0.08 | collinear with on-chain structure (both read 24h volume) |
| Momentum & flow | 0.00 | 0.00 | 0 | never observed at a trigger: its fields postdate these snapshots |

Coefficients are over pillar scores divided by 100, with a missingness indicator per pillar
(`calibration/fit.py::LogisticModel`). The deployed vector clamps negatives to zero,
renormalises to one and rounds to two places (`LogisticModel.deployable_weights`). It is
that rounded vector, not the logistic model, that every figure below scores. Prompts
present weights as importances, so a negative weight is not allowed.

Lineage at a trigger is the declared-socials score alone, because position in a meta and
trend crossover are never collected. Its negative coefficient is the reversal
`prompts/score.md` noted in version 5: tokens above $250k with fewer declared socials surged
more often. The backtest finds that pattern does not survive stratifying by token age. So
the zero means "no support at this lifecycle point", not "socials hurt".

## Data

- The journal as of the collector's last run before the outage: last trigger 2026-09-20
  20:18 UTC. **279 triggered tokens**: 227 Solana, 37 BNB Chain, 8 Ethereum, 7 Base. No
  Robinhood Chain token has triggered yet.
- **One row per token, as it was scored at its trigger.** The collector re-scores recent
  snapshots every hour, so a token has about a hundred score rows. Only the first was
  computed from what was known at the trigger: a median 1.3 minutes after the snapshot, and
  1.6 minutes at the 90th percentile. That row is the lifecycle-matched one.
  `rows_from_store` recomputes its pillars from the stored input packet with the current
  code, so every row shares one pillar definition. Fitting on every score row instead
  weighted tokens by how long they stayed in the re-score window, and put a token's copies
  on both sides of the split. That was fixed in the commit before this one.
- **Pillar coverage at the trigger** (272 labelled tokens): lineage, on-chain, asymmetry and
  mindshare resolve on all 272; community on 40; attention and momentum on none.
- **Outcome**: `max_multiple_6h ≥ 1.5`, the backtest's default (`calibration/backtest.py`),
  fixed before any fit ran. 272 tokens resolved, 62 positive, a base rate of 22.8%
  [18.2%, 28.1%]. That is not the ~2% graduation rate in CLAUDE.md. These tokens are
  sampled above $250k and have already cleared that bar.

| Market-cap band at trigger | Tokens | Reached 1.5x in 6h | Rate |
|---|---|---|---|
| < $500k | 149 | 36 | 24% |
| $500k – $2m | 95 | 19 | 20% |
| $2m – $10m | 19 | 7 | 37% |
| > $10m | 9 | 0 | 0% |

## Split

Forward in time, never at random. The oldest 70% form the training set: **190 tokens**,
12 Sep 18:43 to 17 Sep 22:17 UTC, base rate 23.7%. The newest 30% form the test set:
**82 tokens**, 17 Sep 22:17 to 20 Sep 12:22 UTC, with 17 positives and a base rate of
20.7% [13.4%, 30.7%]. No deployer was recorded at any trigger, so the deployer grouping
the rules ask for could not be applied. One row per token prevents only token-level
leakage.

## Results, out of sample

| On the 82 held-out tokens | Fitted | Priors |
|---|---|---|
| AUC (0.50 is a coin flip; benchmark 0.858) | **0.67** [0.52, 0.83] | 0.64 [0.48, 0.79] |
| Top-decile lift (8 tokens) | 1.81x [0.66, 3.35] | 2.41x [1.04, 3.79] |
| AUC on the final score the board ranks by¹ | 0.66 [0.51, 0.81] | 0.63 [0.47, 0.79] |
| Top-decile lift on the final score | 1.81x [0.66, 3.35] | 1.21x [0.34, 2.85] |

¹ After the completeness multiplier and the regime modifier. They are applied under either
vector but can reorder tokens, so the vector was checked on this number too.

The logistic model itself scores 0.64 [0.48, 0.79]. That equals the priors' figure by
coincidence: the two rankings barely agree (rank correlation 0.21), but each orders the
same 706 of the 1,105 positive–negative pairs correctly.

**Other outcomes**, using only tokens that triggered after the training window, so the
fitted vector never saw them:

| Outcome | Tokens | Positives | Fitted AUC | Priors AUC |
|---|---|---|---|---|
| 2x within 1h | 87 | 5 | 0.72 [0.47, 0.98] | 0.65 [0.38, 0.92] |
| 2x within 6h | 81 | 13 | 0.64 [0.47, 0.82] | 0.60 [0.42, 0.77] |
| 2x within 24h | 62 | 12 | 0.60 [0.41, 0.78] | 0.62 [0.43, 0.81] |
| Survived 24h (≥ 20% of trigger mcap) | 62 | 38 | **0.27** [0.14, 0.40] | **0.29** [0.15, 0.43] |
| 72h, 7d | 0 | — | not measurable yet | |

On the other surge horizons the two vectors cannot be told apart. Survival is the finding
that matters: **both rankings run backwards on it**, with intervals that exclude 0.50. A
high score goes with a smaller chance of still holding a fifth of the trigger market cap a
day later. The screener ranks a short move, not a coin that lasts. The web page states
this next to the weights.

## The checks in `.claude/rules/stats.md`

| Rule | Status |
|---|---|
| Every reported number is out of sample | ✅ |
| Split forward in time | ✅ |
| Hold out the most recent window and look at it only once | ⚠️ The window was scored more than once while the loader's leakage bug was being fixed. No weight, label or threshold was chosen by looking at it: the label came from the backtest, and the 2x thresholds for the other outcomes were fixed before any fit. |
| Group by deployer and meta tag | ❌ Not possible: none was recorded at any trigger |
| Fit only on tokens compared at the same point in their life | ✅ Each token's first score, at its trigger |
| ≥ 20 dead per survivor | ❌ 28 dead to 24 survivors at 7 days (1.2 : 1). For this outcome, negatives outnumber positives 3.4 : 1. |
| Out-of-sample AUC and concordance against 0.858 | ✅ Reported; 0.67, far below |
| Base rate and top-decile lift | ✅ |
| Broken out by regime | ⚠️ Reported, but every held-out token was in a neutral market. Hot and cold are untested. |
| Confidence intervals | ✅ They assume independent rows. Tokens from the same hour are not independent, so the true intervals are wider. |
| Phase 0 gate: ≥ 300 tokens with complete social series | ❌ 0 of 300 |

`FitResult.checks()` passed every check it encodes except the gate:

- positive events in the test set;
- lower AUC bound above 0.50;
- AUC above the priors';
- top-decile lift above 1.

## Why it was adopted anyway

The owner asked for the model's inputs to be calibrated, twice. The gate that forbids it
cannot open as things stand:

- **Complete social series** need the X collector, which needs a paid `X_BEARER_TOKEN`.
  Without one the count stays at 0 of 300 indefinitely.
- **20 dead per survivor** was written with the launch population in mind, where nearly
  every token dies. At a $250k trigger, tokens have already survived their launch. The
  ratio is 1.2 and will not approach 20 however long the collector runs.

A vector fitted to outcomes, that cleared every other check, is a better-founded starting
point than one nobody ever fitted, as long as nobody reads it as more than it is. The page,
the API and every scored row say what it is. The change can be undone:

- `PRIOR_WEIGHTS` keeps the priors, so reverting takes one assignment and a version bump.
- Every score row carries `weights_version`, so rows scored under `priors-*` and
  `fitted-v1` stay distinguishable in the dataset.

Whether the gate should be restated for a $250k trigger, for example as dead per survivor
at 24 hours or as negatives per positive on the chosen outcome, is the owner's decision.
This fit does not make it.

## Next

1. **Restart the collector.** It has not run since 2026-09-20: the journal outgrew
   GitHub's file limit, which PR #10 fixes. Every hour it stays down is an hour of tokens
   that can never be collected, because social data cannot be backfilled.
2. **Re-fit when a new window has resolved**, and adopt the result only if its record
   clears the same checks against the vector then in force:
   `python -m calibration.report --force --record scoring/weights/fitted-v2.json`,
   then copy `weights` into `scoring/pillars.py::WEIGHTS`, bump `WEIGHTS_VERSION` and
   `prompt_version`, and add a note to `prompts/score.md`.
3. **Watch for a hot or cold window** before trusting any of this in a different market.
4. **An X API key** would make attention velocity measurable at the trigger. It is the
   largest prior weight (0.28) that no fit has been able to test.
