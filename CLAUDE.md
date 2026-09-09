# Memecoin Screener

Scores newly-launched tokens on Solana / BNB Chain / Robinhood Chain for short-horizon
gain potential, and logs every score so the model can later be calibrated against outcomes.

## Current phase: PHASE 0 — COLLECTION ONLY

The scoring weights are **uncalibrated priors**. They are guesses. Until Phase 2 completes,
this repo is a data collector that happens to emit scores, not a signal generator.

Never describe Phase 0 output as predictive. Never wire it to anything that can place an order.

## Base rates (memorize these before touching the scoring logic)

- Pump.fun graduation rate is somewhere between **0.2% and 2.7%** depending on measurement window
  and method. Published 2026 figures: 0.198% (May–Jun cohort study), 0.26% (mid-June), 2.7%
  (Aug cohort tracking), ~1.4% (all-time Dune).
- **Measurement method changes the number by 4–10x.** Dividing today's graduations by today's
  launches gives ~12% and is wrong. Always follow a launch cohort forward to a fixed horizon.
- Graduation is a *low* bar — it is roughly $69k mcap. Most graduates still round to zero.
- Best-known social factor: launches advertising a Telegram graduate at 1.485% vs 0.166% without
  (8.94x lift). All three socials present: 1.919% vs 0.110% (17.4x lift). Cox HR for Telegram 5.40.
- A published Cox model on 832,941 launches reached **concordance 0.858**. That is the benchmark
  to beat. If our model scores below it, we have not built anything.

The honest framing: even the strongest known social signal moves you from ~0.1% to ~1.9%.
The screener's job is to beat a ~2% base rate, not to find winners.

## Hard rules

1. **Never delete or overwrite a snapshot row.** The graveyard is the dataset. Losers are the
   signal. Any pruning of dead tokens destroys the thing we are building.
2. **Snapshot at a fixed trigger**, identical for every token (see `BUILD_BRIEF.md`). Never
   compare a winner at peak against a loser at launch.
3. **Never impute a missing field.** Missing is `null`, penalized via data-completeness, never
   filled with a mean or a guess.
4. **No trade execution, ever.** No exchange keys, no signing, no order placement in this repo.
   Output is a ranked list a human reads.
5. **No secrets in the repo.** All API keys via env, `.env` gitignored.
6. **Log the score with the input snapshot that produced it**, in the same row. A score without
   its inputs cannot be back-tested.

## Architecture notes

- **LunarCrush is for regime and meta detection, not per-token scoring.** It tracks ~4,000
  established assets; a three-hour-old launch will not be in it. Use it to answer "is the tape
  hot and which narrative is running," then feed that into the regime multiplier.
- **Per-token social must be collected first-party and forward.** There is no usable retroactive
  source for X/Telegram metrics on micro-cap launches. See `BUILD_BRIEF.md` §3.
- On-chain history *is* backfillable (Bitquery, Dune, Helius). Social history is not. This
  asymmetry drives the whole phase plan.

## Conventions

- Python 3.11+, `uv` for deps, `ruff` for lint.
- Storage: DuckDB over Parquet. Append-only. Partition by `snapshot_date`.
- All timestamps UTC, ISO 8601, stored as epoch millis.
- Scoring prompt lives in `prompts/score.md` and is versioned. Bump `prompt_version` on every
  edit and write it into every scored row.
