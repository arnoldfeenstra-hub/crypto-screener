# Statistics — no fitting without an out-of-sample split

Binding on everything in `calibration/`. Phase 2 does not start until Phase 0's exit
criteria are met (≥300 triggered tokens with complete social series, ≥20 dead per survivor).

## Splitting

1. No metric is ever reported on data the model saw. Every number in a calibration report
   is out-of-sample or it is not reported.
2. Split **forward in time**, never at random. A random split leaks the future: tokens
   from the same launch hour share a regime, a meta, and often a deployer.
3. Hold out the most recent window entirely and touch it once, at the end.
4. Group by deployer and by meta tag when splitting. The same deployer's tokens in both
   train and test is leakage.

## Fitting

1. Fit on the lifecycle-matched sample only — every token snapshotted at the same trigger
   (§4). Never compare a winner at peak against a loser at launch.
2. The negative class is the point. Target ≥20 dead per survivor; if the negative class is
   small the model learns to say yes.
3. Report out-of-sample AUC **and** concordance against the published benchmark of 0.858.
   Below it, the honest statement is that nothing has been built yet.
4. Report the base rate next to every result. A 2% base rate makes accuracy meaningless;
   report lift over base rate in the top decile instead.
5. Break every result out by regime (hot / neutral / cold). A signal that works only in a
   hot tape is a signal that fails exactly when it costs money.
6. Confidence intervals on every reported figure. A point estimate from 300 tokens with a
   2% base rate is roughly six positive events; say so.

## Not allowed

- Fitting on survivors. Reverse-engineering from winners identifies what is currently
  being pumped, which is what is currently being sold into.
- Dropping rows with missing features. Model missingness; do not delete the row.
- Reporting a metric after having looked at the test set to choose the model.
- Replacing the weight vector in `prompts/score.md` with anything but fitted coefficients
  that cleared the checks above, with `prompt_version` bumped in the same commit.
