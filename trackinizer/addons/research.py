"""Audit a research loop's own epistemics: predictions, beliefs, and the ledger.

A loop that writes results to a ledger, plans in Issues and records findings as
Beliefs drifts in predictable ways. It forgets to say what it expected before
a result, so its calibration is unknowable. It keeps a Belief at high
confidence after a later result undercuts it. It stops writing Beliefs at all
while results keep arriving. And the Experiments that mirror the ledger go
stale whenever the mirroring step is skipped. This addon reads the graph and
the ledger and reports each of those, at ``GET /api/addons/research/audit``.

Conventions it reads, all on existing kinds:

- **Pre-registration.** An Experiment labelled ``prereg`` whose ``config``
  holds ``prediction``: ``{"run_id", "pair", "metric", "lo", "hi"}`` and
  optionally ``"parent"`` and ``"window"`` (default ``dev``). ``metric`` is
  ``skill_pt`` (the run's skill in percentage points) or ``delta_pt`` (its
  skill minus the parent's). It counts only if the Experiment was created
  before the ledger row it predicts.
- **Mirror.** An Experiment mirrors ledger row ``ID`` when labelled
  ``ledger:ID``.
- **Review.** A Belief labelled ``reviewed`` passed an adversarial review.

The ledger is the source of truth and the only file read; nothing here writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import comb
from pathlib import Path
from typing import Literal

import asyncio
import json

from configgle import Fig
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from trackinizer.addons.addon import AddonManifest
from trackinizer.server.api._deps import get_store
from trackinizer.types.inquiries import Belief, Experiment


__all__ = [
    "Audit",
    "Prediction",
    "Research",
    "audit",
    "read_ledger",
]


PREREG_LABEL = "prereg"
REVIEWED_LABEL = "reviewed"
LEDGER_PREFIX = "ledger:"


@dataclass(frozen=True, slots=True, kw_only=True)
class Prediction:
    """One pre-registered prediction and, once its run is scored, its result."""

    experiment: int
    run_id: str
    pair: str
    metric: str
    lo: float
    hi: float
    registered: datetime
    observed: float | None = None
    scored: datetime | None = None
    verdict: Literal["hit", "miss_low", "miss_high", "pending", "late", "invalid"] = (
        "pending"
    )
    note: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Calibration:
    """How the resolved predictions fared as a set."""

    resolved: int
    hits: int
    misses_low: int
    misses_high: int
    sign_p: float | None
    """Two-sided sign-test p-value that misses fall low as often as high."""

    mean_offset: float | None
    """Mean of (observed - midpoint) / half-width; 0 is centred, below -1 is
    a miss low on average."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BeliefFlag:
    """A Belief the audit thinks needs attention, and why."""

    belief: int
    title: str
    confidence: float | None
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Audit:
    """Everything the audit found."""

    ledger_rows: int
    unmirrored: list[str] = field(default_factory=list)
    """Ledger row ids no Experiment mirrors."""

    unparented: int = 0
    """Ledger rows that name no parent run."""

    predictions: list[Prediction] = field(default_factory=list)
    calibration: Calibration | None = None
    beliefs: list[BeliefFlag] = field(default_factory=list)
    last_belief: datetime | None = None
    results_since_last_belief: int = 0


def read_ledger(path: Path | str) -> list[dict[str, object]]:
    """Read every row of a JSONL ledger; a torn last line is skipped.

    Args:
      path: The ledger file; a leading ``~`` is expanded.

    Returns:
      rows: Its rows, in file order.

    """
    path = Path(path).expanduser()
    rows: list[dict[str, object]] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def audit(
    ledger: Sequence[Mapping[str, object]],
    experiments: Sequence[Experiment],
    beliefs: Sequence[Belief],
    *,
    now: datetime,
    stale_after: timedelta = timedelta(days=2),
) -> Audit:
    """Audit predictions, the ledger mirror and the Beliefs.

    Args:
      ledger: The ledger rows, in file order.
      experiments: Every Experiment.
      beliefs: Every Belief.
      now: The current time.
      stale_after: How long Beliefs may go unwritten while results arrive
        before that is flagged.

    Returns:
      audit: What was found.

    """
    mirrored = {
        label.removeprefix(LEDGER_PREFIX)
        for experiment in experiments
        for label in experiment.labels or ()
        if label.startswith(LEDGER_PREFIX)
    }
    unmirrored = [
        str(row.get("id")) for row in ledger if str(row.get("id")) not in mirrored
    ]
    unparented = sum(1 for row in ledger if not row.get("parent"))
    predictions = [
        _resolve(experiment, ledger)
        for experiment in experiments
        if PREREG_LABEL in (experiment.labels or ())
    ]
    last_belief = max((belief.created for belief in beliefs), default=None)
    since = [
        row
        for row in ledger
        if last_belief is None or (_when(row) or now) > last_belief
    ]
    flags = _belief_flags(beliefs, experiments)
    if last_belief is not None and since and now - last_belief > stale_after:
        flags.append(
            BeliefFlag(
                belief=0,
                title="(no Belief written)",
                confidence=None,
                reason=(
                    f"{len(since)} ledger results since the last Belief "
                    f"({last_belief:%Y-%m-%d}); findings are not being recorded"
                ),
            ),
        )
    return Audit(
        ledger_rows=len(ledger),
        unmirrored=unmirrored,
        unparented=unparented,
        predictions=predictions,
        calibration=_calibration(predictions),
        beliefs=flags,
        last_belief=last_belief,
        results_since_last_belief=len(since),
    )


def _when(row: Mapping[str, object]) -> datetime | None:
    """When a ledger row was written."""
    stamp = row.get("utc")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _skill_pt(row: Mapping[str, object], window: str, pair: str) -> float | None:
    """Return a ledger row's skill for one pair, in percentage points."""
    metrics = row.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    scored = metrics.get(window)
    pairs = scored.get("pairs") if isinstance(scored, Mapping) else None
    found = pairs.get(pair) if isinstance(pairs, Mapping) else None
    skill = found.get("skill") if isinstance(found, Mapping) else None
    return float(skill) * 100 if isinstance(skill, int | float) else None


def _resolve(
    experiment: Experiment, ledger: Sequence[Mapping[str, object]]
) -> Prediction:
    """Read one pre-registration and judge it against the ledger."""
    config = experiment.config or {}
    spec = config.get("prediction")
    if not isinstance(spec, Mapping):
        return _invalid(experiment, "config.prediction is missing")
    try:
        run_id = str(spec["run_id"])
        pair = str(spec["pair"])
        metric = str(spec.get("metric", "skill_pt"))
        lo, hi = float(spec["lo"]), float(spec["hi"])  # pyright: ignore[reportArgumentType]
    except (KeyError, TypeError, ValueError):
        return _invalid(experiment, "prediction needs run_id, pair, lo and hi")
    if lo > hi or metric not in ("skill_pt", "delta_pt"):
        return _invalid(experiment, "lo > hi, or metric is not skill_pt/delta_pt")
    window = str(spec.get("window", "dev"))
    base = Prediction(
        experiment=experiment.seq,
        run_id=run_id,
        pair=pair,
        metric=metric,
        lo=lo,
        hi=hi,
        registered=experiment.created,
    )
    row = _latest(ledger, run_id)
    if row is None:
        return base
    observed = _skill_pt(row, window, pair)
    if observed is not None and metric == "delta_pt":
        parent = _latest(ledger, str(spec.get("parent", "")))
        parent_skill = None if parent is None else _skill_pt(parent, window, pair)
        observed = None if parent_skill is None else observed - parent_skill
    scored = _when(row)
    if observed is None:
        return _replace(
            base, scored=scored, verdict="invalid", note="no score for that pair"
        )
    if scored is not None and experiment.created >= scored:
        return _replace(
            base,
            observed=observed,
            scored=scored,
            verdict="late",
            note="registered after the result",
        )
    verdict: Literal["hit", "miss_low", "miss_high"] = (
        "miss_low" if observed < lo else "miss_high" if observed > hi else "hit"
    )
    return _replace(base, observed=observed, scored=scored, verdict=verdict)


def _replace(base: Prediction, **changes: object) -> Prediction:
    """Return ``base`` with some fields changed."""
    values = {name: getattr(base, name) for name in Prediction.__dataclass_fields__}
    values.update(changes)
    return Prediction(**values)  # pyright: ignore[reportArgumentType]


def _invalid(experiment: Experiment, note: str) -> Prediction:
    """Record a pre-registration the audit cannot read."""
    return Prediction(
        experiment=experiment.seq,
        run_id="",
        pair="",
        metric="",
        lo=0.0,
        hi=0.0,
        registered=experiment.created,
        verdict="invalid",
        note=note,
    )


def _latest(
    ledger: Sequence[Mapping[str, object]], run_id: str
) -> Mapping[str, object] | None:
    """Return the newest ledger row for a run."""
    for row in reversed(ledger):
        if row.get("run_id") == run_id:
            return row
    return None


def _calibration(predictions: Sequence[Prediction]) -> Calibration | None:
    """Summarize the resolved predictions."""
    resolved = [p for p in predictions if p.verdict in ("hit", "miss_low", "miss_high")]
    if not resolved:
        return None
    low = sum(1 for p in resolved if p.verdict == "miss_low")
    high = sum(1 for p in resolved if p.verdict == "miss_high")
    offsets = [
        (p.observed - (p.lo + p.hi) / 2) / ((p.hi - p.lo) / 2)
        for p in resolved
        if p.observed is not None and p.hi > p.lo
    ]
    return Calibration(
        resolved=len(resolved),
        hits=len(resolved) - low - high,
        misses_low=low,
        misses_high=high,
        sign_p=_sign_test(low, high),
        mean_offset=sum(offsets) / len(offsets) if offsets else None,
    )


def _sign_test(low: int, high: int) -> float | None:
    """Two-sided exact binomial p that misses split evenly between low and high."""
    n = low + high
    if n == 0:
        return None
    k = min(low, high)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def _belief_flags(
    beliefs: Sequence[Belief], experiments: Sequence[Experiment]
) -> list[BeliefFlag]:
    """Flag confident Beliefs that rest on too little or were never reviewed."""
    by_id = {experiment.id: experiment for experiment in experiments}
    flags: list[BeliefFlag] = []
    for belief in beliefs:
        if belief.status != "active":
            continue
        confidence = belief.confidence
        labels = belief.labels or ()
        evidence = [edge for edge in belief.proved_by if edge.valence > 0]
        if (
            confidence is not None
            and confidence >= 0.8
            and REVIEWED_LABEL not in labels
        ):
            flags.append(
                BeliefFlag(
                    belief=belief.seq,
                    title=belief.title,
                    confidence=confidence,
                    reason="confidence >= 0.8 without an adversarial review",
                ),
            )
        if belief.judgement == "proven" and not evidence:
            flags.append(
                BeliefFlag(
                    belief=belief.seq,
                    title=belief.title,
                    confidence=confidence,
                    reason="judged proven with no proving Experiment linked",
                ),
            )
        contradicted = [
            by_id[edge.id].title
            for edge in belief.proved_by
            if edge.valence < 0 and edge.id in by_id
        ]
        if contradicted and belief.judgement == "proven":
            flags.append(
                BeliefFlag(
                    belief=belief.seq,
                    title=belief.title,
                    confidence=confidence,
                    reason="still proven although disproved by "
                    + ", ".join(contradicted),
                ),
            )
    return flags


def render(found: Audit) -> str:
    """Write an audit as short plain text for a terminal or an agent."""
    lines = [f"ledger: {found.ledger_rows} rows"]
    if found.unmirrored:
        lines.append(
            f"  {len(found.unmirrored)} not mirrored as Experiments: "
            + ", ".join(found.unmirrored[:12])
            + (" ..." if len(found.unmirrored) > 12 else ""),
        )
    lines.append(f"  {found.unparented} name no parent run")
    lines.append(f"predictions: {len(found.predictions)} pre-registered")
    for p in found.predictions:
        observed = "" if p.observed is None else f" observed {p.observed:+.2f}"
        lines.append(
            f"  Experiment#{p.experiment} {p.run_id} {p.pair} {p.metric} "
            f"[{p.lo:+.2f}, {p.hi:+.2f}]{observed}: {p.verdict}"
            + (f" ({p.note})" if p.note else ""),
        )
    if (cal := found.calibration) is not None:
        offset = (
            "" if cal.mean_offset is None else f", mean offset {cal.mean_offset:+.2f}"
        )
        p_value = "" if cal.sign_p is None else f", sign test p={cal.sign_p:.3f}"
        lines.append(
            f"calibration: {cal.hits}/{cal.resolved} hit, {cal.misses_low} low, "
            f"{cal.misses_high} high{p_value}{offset}",
        )
    lines.append(f"beliefs: {len(found.beliefs)} flags")
    grouped: dict[str, list[BeliefFlag]] = {}
    for flag in found.beliefs:
        grouped.setdefault(flag.reason, []).append(flag)
    for reason, flags in grouped.items():
        if len(flags) > 3:
            refs = ", ".join(f"#{flag.belief}" for flag in flags)
            lines.append(f"  {len(flags)} Beliefs: {reason} ({refs})")
            continue
        for flag in flags:
            ref = f"Belief#{flag.belief}" if flag.belief else "-"
            lines.append(f"  {ref} {flag.title[:70]}: {reason}")
    return "\n".join(lines) + "\n"


class Research:
    """Report the research loop's calibration, ledger drift and Belief health."""

    class Config(Fig["Research"]):
        ledger_path: str = "~/Desktop/Windrunner/wr-data/forecast/ledger.jsonl"
        """The ledger the loop's scorer writes."""

        stale_after_days: float = 2.0
        """How long Beliefs may go unwritten while results arrive."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def manifest(self) -> AddonManifest:
        """Contribute the audit route."""
        router = APIRouter()
        config = self.config

        async def audit_route(
            request: Request,
            output: Literal["text", "json"] = "text",
        ) -> object:
            store = get_store(request)
            experiments = [
                item
                for item in await store.list_kind("Experiment", limit=10_000)
                if isinstance(item, Experiment)
            ]
            beliefs = [
                item
                for item in await store.list_kind("Belief", limit=10_000)
                if isinstance(item, Belief)
            ]
            found = audit(
                await asyncio.to_thread(read_ledger, config.ledger_path),
                experiments,
                beliefs,
                now=datetime.now(UTC),
                stale_after=timedelta(days=config.stale_after_days),
            )
            if output == "json":
                return json.loads(json.dumps(found, default=_plain))
            return PlainTextResponse(render(found))

        router.add_api_route("/audit", audit_route, methods=["GET"])
        return AddonManifest(
            title="Research audit",
            description=(
                "Calibration of pre-registered predictions, ledger-to-Experiment "
                "drift, and Beliefs that need review."
            ),
            routers=[router],
        )


def _plain(value: object) -> object:
    """JSON-encode the dataclasses and datetimes an audit holds."""
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}  # pyright: ignore[reportAttributeAccessIssue]
    raise TypeError(type(value).__name__)
