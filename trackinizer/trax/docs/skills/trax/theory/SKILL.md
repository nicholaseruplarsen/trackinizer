---
name: trax-theory
description: ALWAYS invoke this skill when authoring a trackinizer Theory -- a parametric law with a form, fitted parameters, the range it was fitted on, a regime and at least one rival. Do not write the Theory directly -- invoke this skill first.
---

# Theory — authoring expectations

A `Theory` is a named, parametric, falsifiable law: how one quantity depends on
variables you can vary or observe. One law that predicts many configurations,
including ones outside the fitted range, beats a pile of "X does not beat its
parent" verdicts. See `trackinizer/docs/epistemy.md` (Theories and Predictions).

## Completeness bar

- **title**: the law in words ("DK1 wind MAE falls as a power of training history").
- **explains**: the quantity, e.g. `dk1.wind.mae`.
- **form**: the expression, e.g. `MAE(D) = Einf + B * D^-beta`, or `path:function`.
- **variables**: JSON `{name: {"unit", "fitted_range": [lo, hi]}}`. A prediction
  outside `fitted_range` is an extrapolation, the strongest test a law gets.
  Optionally `"curve": [{"x", "lo", "mid", "hi"}]` for the law plot.
- **parameters**: JSON `{name: {"mean", "lo90", "hi90", "n"}}`, the current fit.
- **regime**: where it claims to hold (zones, targets, period, conditions), as
  text or a JSON predicate. A systematic miss outside it is a regime break, not noise.
- **fit_ref**: the fitting code, `path@sha`.
- **rivals**: at least one other Theory explaining the same quantity. Stored once
  per pair, lower seq first: `trax theory 3 rivals theory 7`.

## The loop

1. Before a sweep, write the Theory and its rival. Pick points where their
   predictions differ most relative to noise; sweep 4-6 points along one axis
   with at least one outside the fitted range.
2. Register each point as a Prediction made by the Theory (`../prediction/SKILL.md`)
   before the run.
3. Link the runs you fit on: `trax experiment 12 fits theory 3`, with the fit
   point in the Experiment's `config.fit_point` = `{"at": {"D": 180}, "value": 12.4}`.
4. After each result, refit and update `parameters` (the change is logged), then
   handle every Belief flagged `needs_review`.

`record` (predictions, log score, earned weight against rivals) is written by the
scorer; never set it by hand. A Belief about the law links with
`trax belief 50 about theory 3` and then carries the Theory's earned weight as its
`earned_confidence`.

## Common mistakes

- No rival: nothing can be weighed against it, and it earns no confidence.
- A single-point "law" with no variable: that is a Belief, not a Theory.
- Refitting without logging which runs it was fitted on (`fits` edges).
- Changing the form after a miss without registering the new form as a new Theory
  (patching a degenerating programme).
