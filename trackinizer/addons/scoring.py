"""Proper scoring for quantile Predictions, ported unchanged from coord.

coord (``/opt/coord/lib/knowledge.py``, 2026-11-12) scores every prediction its
research boxes register. Trackinizer scores Windrunner's own predictions with
the same maths, and recomputes coord's scores in shadow so the two scorers
cannot drift apart unnoticed. Keep this module identical in behaviour to
coord's: a change here is a change there.

A prediction states five quantiles, q05/q25/q50/q75/q95. Its CDF is the
piecewise-linear curve through them, with the tails extended linearly to 0 and
1 by a quarter of the outer interval widths.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import math


__all__ = [
    "QK",
    "QS",
    "calibration",
    "cdf",
    "density",
    "earned_weights",
    "empirical_quantiles",
    "interval_score",
    "score",
]


QS: tuple[float, ...] = (0.05, 0.25, 0.5, 0.75, 0.95)
QK: tuple[str, ...] = ("q05", "q25", "q50", "q75", "q95")


def _knots(qv: Sequence[float]) -> list[tuple[float, float]]:
    lo_w, hi_w = qv[1] - qv[0], qv[4] - qv[3]
    return [
        (qv[0] - 0.05 / 0.2 * max(lo_w, 1e-12), 0.0),
        *zip(qv, QS, strict=True),
        (qv[4] + 0.05 / 0.2 * max(hi_w, 1e-12), 1.0),
    ]


def cdf(qv: Sequence[float], y: float) -> float:
    """Piecewise-linear CDF through the quantiles; tails extended linearly to 0 and 1."""
    pts = _knots(qv)
    if y <= pts[0][0]:
        return 0.0
    if y >= pts[-1][0]:
        return 1.0
    for (x0, p0), (x1, p1) in zip(pts, pts[1:], strict=False):
        if x0 <= y <= x1:
            return p0 if x1 == x0 else p0 + (p1 - p0) * (y - x0) / (x1 - x0)
    return 1.0


def density(qv: Sequence[float], y: float) -> float:
    """The density of that CDF at ``y``; 0 outside its support."""
    pts = _knots(qv)
    for (x0, p0), (x1, p1) in zip(pts, pts[1:], strict=False):
        if x0 <= y <= x1 and x1 > x0:
            return (p1 - p0) / (x1 - x0)
    return 0.0


def interval_score(lo: float, hi: float, y: float, alpha: float) -> float:
    """Winkler's interval score for a central ``1 - alpha`` interval; lower is better."""
    return (hi - lo) + (2 / alpha) * max(lo - y, 0) + (2 / alpha) * max(y - hi, 0)


def score(
    quantiles: Mapping[str, float], y: float, *, better: str = "lower"
) -> dict[str, float | int]:
    """Score one observed value against a quantile prediction.

    Args:
      quantiles: ``{"q05", "q25", "q50", "q75", "q95"}``.
      y: The observed value.
      better: ``lower`` or ``higher``: which direction is good, for optimism.

    Returns:
      scores: observed, pit, is50, is90, logscore, cover50, cover90, optimism.
        ``optimism > 0`` means the outcome was worse than the median predicted.

    """
    qv = [float(quantiles[k]) for k in QK]
    pit = cdf(qv, y)
    d = density(qv, y)
    width = max(qv[4] - qv[0], 1e-12)
    # Outside the stated support the density is 0; floor at 1e-3 of the 90%
    # width so one miss is costly, not infinite.
    logscore = math.log(max(d, 1e-3 / width))
    optimism = (pit - 0.5) if better == "lower" else (0.5 - pit)
    return {
        "observed": y,
        "pit": pit,
        "is50": interval_score(qv[1], qv[3], y, 0.5),
        "is90": interval_score(qv[0], qv[4], y, 0.1),
        "logscore": logscore,
        "cover50": int(qv[1] <= y <= qv[3]),
        "cover90": int(qv[0] <= y <= qv[4]),
        "optimism": optimism,
    }


def _summary(rows: Sequence[Mapping[str, float]]) -> dict[str, object]:
    n = len(rows)
    if not n:
        return {"n": 0}

    def mean(key: str) -> float:
        return sum(float(r[key]) for r in rows) / n

    return {
        "n": n,
        "cover50": round(mean("cover50"), 3),
        "cover90": round(mean("cover90"), 3),
        "nominal": [0.5, 0.9],
        "mean_pit": round(mean("pit"), 3),
        "optimism": round(mean("optimism"), 3),
        "mean_is90": round(mean("is90"), 4),
        "mean_logscore": round(mean("logscore"), 3),
        "pit_hist": [
            sum(
                1
                for r in rows
                if i / 10 <= r["pit"] < (i + 1) / 10 or (i == 9 and r["pit"] == 1)
            )
            for i in range(10)
        ],
    }


def calibration(
    rows: Sequence[Mapping[str, object]], *, groups: Sequence[str] = ("source",)
) -> dict[str, object]:
    """Coverage against nominal, mean PIT and optimism: overall, by group and by month.

    Args:
      rows: Resolved, scored predictions, each with the score keys, a
        ``registered`` datetime and the group keys.
      groups: The keys to break the summary down by.

    Returns:
      calibration: ``{"all", "by_<group>"..., "by_month"}``, the shape coord's
        ``calibration`` verb prints.

    """
    scored = [r for r in rows if isinstance(r.get("pit"), int | float)]
    out: dict[str, object] = {"all": _summary(scored)}  # pyright: ignore[reportArgumentType]
    for group in groups:
        by: dict[str, list[Mapping[str, object]]] = {}
        for r in scored:
            by.setdefault(str(r.get(group) or "?"), []).append(r)
        out[f"by_{group}"] = {k: _summary(v) for k, v in sorted(by.items())}  # pyright: ignore[reportArgumentType]
    months: dict[str, list[Mapping[str, object]]] = {}
    for r in scored:
        when = r.get("registered")
        key = when.astimezone(UTC).strftime("%Y-%m") if isinstance(when, datetime) else "?"
        months.setdefault(key, []).append(r)
    out["by_month"] = {k: _summary(v) for k, v in sorted(months.items())}  # pyright: ignore[reportArgumentType]
    return out


def earned_weights(
    by_theory: Mapping[str, Mapping[str, float]], group: Sequence[str]
) -> tuple[dict[str, float | None], int]:
    """Posterior weights of rival Theories from log scores on shared quantities.

    Rivals are compared only on quantities every scored rival predicted, so
    the comparison is a log Bayes factor on the same data. A Theory gets no
    weight (``None``, not 0) until it shares a scored quantity with a rival.

    Args:
      by_theory: Theory id -> {quantity id: log score}.
      group: The Theory and its rivals.

    Returns:
      weights: Theory id -> weight, or ``None``.
      shared: How many quantities the scored rivals share.

    """
    scored = [g for g in group if by_theory.get(g)]
    shared: set[str] = (
        set.intersection(*[set(by_theory[g]) for g in scored])
        if len(scored) > 1
        else set()
    )
    if not shared:
        return dict.fromkeys(group), 0
    totals = {g: sum(by_theory[g][k] for k in shared) for g in scored}
    top = max(totals.values())
    z = sum(math.exp(v - top) for v in totals.values())
    weights: dict[str, float | None] = dict.fromkeys(group)
    weights.update({g: round(math.exp(v - top) / z, 3) for g, v in totals.items()})
    return weights, len(shared)


def empirical_quantiles(values: Sequence[float]) -> dict[str, float]:
    """The q05..q95 of a sample, by linear interpolation between order statistics."""
    ordered = sorted(values)
    n = len(ordered)
    out: dict[str, float] = {}
    for key, q in zip(QK, QS, strict=True):
        pos = q * (n - 1)
        lo = math.floor(pos)
        hi = min(lo + 1, n - 1)
        out[key] = ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)
    return out
