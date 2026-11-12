---
name: trax-prediction
description: ALWAYS invoke this skill when pre-registering a trackinizer Prediction -- quantiles q05..q95 for one measurable quantity, frozen before the run, scored by the scorer, never graded by hand. Do not write the Prediction directly -- invoke this skill first.
---

# Prediction — authoring expectations

A `Prediction` is a predictive distribution for one measurable quantity in one
future run, frozen at submit: `quantity`, `quantiles`, `made_by` and `registered`
can never be edited. The scorer resolves it from the measured value (observed,
PIT, 50%/90% interval scores, log score). You never write HIT or MISS.

## Completeness bar

- **quantity**: JSON naming what is measured and where the scorer reads it:
  - Windrunner ledger: `{"run_id", "pair", "metric": "delta_pt"|"skill_pt", "parent", "window", "better": "higher"}`;
  - coord runs register with `ssh coord predict` instead (rs-shared/notes/research-protocol.md);
  - on a law's axis add `"at": {"D": 540}` so it lands on the law plot.
- **quantiles**: `{"q05", "q25", "q50", "q75", "q95"}`, non-decreasing. The 50%
  interval is q25..q75 and the 90% interval q05..q95: say what you believe, not a
  range wide enough that it cannot miss.
- **made_by**: `theory` (the Theory's current fit; link it, `predicts`), `agent`
  (your judgement) or `house-prior`.
- **override_of**: when you adjust a Theory's prediction by hand, register yours
  separately with `made_by agent` and `override_of` the Theory's. Both are scored,
  which shows whether your judgement beats the law.
- Link the run that measures it: `trax experiment 12 tests prediction 40`. A run
  created before the Prediction was registered makes it `late`.

## Shrink toward the house prior

Windrunner's predictions have run optimistic (misses land low about 2:1). For an
exploratory prediction with no Theory behind it, start from the house prior
(`GET /api/addons/knowledge/house-prior` on the server, or rs-shared/notes/house-prior.json)
and move away from it only as far as your evidence carries you. The prior is
deliberately wide; it is a shrinkage target, not a forecast.

## After the result

Write the interpretation on the Experiment's outcome: `noise` (inside the stated
distribution), `regime break` (a systematic residual along a variable) or
`form failure` (a systematic residual everywhere). Then refit the Theory.

## Common mistakes

- A `lo..hi` range with no stated coverage (the legacy convention).
- Registering after the ledger row exists: it resolves `late` and does not count.
- Grading your own prediction in prose.
