# Data integrity — append-only, no imputation

These rules are enforced in code (`collectors/store.py`) and in tests
(`tests/test_store_append_only.py`). Do not weaken either.

## Append-only

1. A snapshot row, once written, is never updated and never deleted. The graveyard is
   the dataset; pruning dead tokens destroys the thing being built.
2. `collectors/store.py` may contain no `UPDATE`, `DELETE`, `DROP TABLE`, `TRUNCATE`,
   `REPLACE INTO`, `INSERT OR REPLACE`, or `ON CONFLICT ... DO UPDATE` statement. A test
   greps the module for these and fails the build if one appears.
3. Forward labels (§2 of `BUILD_BRIEF.md`) are filled *hours to days* after the snapshot.
   Writing them into the snapshot row would be an update, so they do not live there.
   They go in a separate append-only `labels` table keyed by `snapshot_id`. Reads join.
   The `labels.filled_at` field in the §4 schema is a marker, not storage.
4. Same for re-pricing: `outcome_observations` is append-only; each re-price is a new row.
5. A token triggers exactly once. A second `append_snapshot` for the same
   `(chain, contract)` raises `AppendOnlyViolation` — it never overwrites. The uniqueness
   is a database constraint, not a convention.
6. Corrections are new rows with a later `ts` and a `supersedes` pointer, never edits.

## No imputation

1. A field that could not be collected is `None` / SQL `NULL`. Never zero, never a mean,
   never a carried-forward value, never a guess.
2. Missingness is a feature: `data_completeness` is computed from the null count and
   carried on the row. It feeds the Data Completeness modifier in `prompts/score.md`.
3. `float('nan')` from an upstream API is missing data, not a number. `schema.py`
   normalizes NaN to `None` on the way in.
4. A missing value never satisfies a threshold. An unknown market cap does not cross
   $250k. See `collectors/trigger_watcher.py::evaluate`.

## Provenance

1. Every row records `source` and `collected_at_ms`. A row whose origin cannot be named
   should not be written.
2. Every scored row stores `prompt_version` and the full input snapshot that produced it
   (hard rule 6). A score without its inputs cannot be back-tested.
