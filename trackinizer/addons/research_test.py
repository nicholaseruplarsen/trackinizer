"""Tests for the research-loop audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from trackinizer.addons.research import audit, read_ledger, render
from trackinizer.types.inquiries import ArtifactEdge, Belief, Experiment


if TYPE_CHECKING:
    from pathlib import Path


_T0 = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


def _row(
    row_id: str,
    run_id: str,
    hours: float,
    skills: dict[str, float],
    parent: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": row_id,
        "run_id": run_id,
        "utc": (_T0 + timedelta(hours=hours)).isoformat(),
        "metrics": {
            "dev": {
                "pairs": {
                    pair: {"skill": skill / 100} for pair, skill in skills.items()
                }
            }
        },
    }
    if parent is not None:
        row["parent"] = parent
    return row


def _prereg(seq: int, hours: float, **prediction: object) -> Experiment:
    return Experiment(
        seq=seq,
        title=f"PRED {seq}",
        labels=("prereg",),
        config={"prediction": prediction},
        created=_T0 + timedelta(hours=hours),
    )


_LEDGER = [
    _row("a1", "H27_parent", 0, {"DK1/wind_mw": -1.5}),
    _row("b2", "H27", 2, {"DK1/wind_mw": -0.7}, parent="a1"),
    _row("c3", "H29", 4, {"DK1/wind_mw": -0.7}),
]


def test_a_prediction_registered_before_its_result_is_judged() -> None:
    found = audit(
        _LEDGER,
        [
            _prereg(
                1,
                1,
                run_id="H27",
                pair="DK1/wind_mw",
                metric="delta_pt",
                parent="H27_parent",
                lo=1.0,
                hi=3.0,
            )
        ],
        [],
        now=_T0 + timedelta(hours=5),
    )
    (prediction,) = found.predictions
    assert prediction.observed is not None
    assert abs(prediction.observed - 0.8) < 1e-9
    assert prediction.verdict == "miss_low"


def test_a_prediction_written_after_the_result_is_late() -> None:
    found = audit(
        _LEDGER,
        [_prereg(1, 3, run_id="H27", pair="DK1/wind_mw", lo=-1.0, hi=0.0)],
        [],
        now=_T0 + timedelta(hours=5),
    )
    assert found.predictions[0].verdict == "late"
    assert found.calibration is None


def test_an_unscored_run_is_pending() -> None:
    found = audit(
        _LEDGER,
        [_prereg(1, 1, run_id="H31", pair="DK1/wind_mw", lo=0.0, hi=1.0)],
        [],
        now=_T0,
    )
    assert found.predictions[0].verdict == "pending"


def test_calibration_counts_one_sided_misses() -> None:
    ledger = [_row(f"r{i}", f"R{i}", 2, {"DK1/wind_mw": 0.0}) for i in range(8)]
    experiments = [
        _prereg(i, 1, run_id=f"R{i}", pair="DK1/wind_mw", lo=0.5, hi=2.0)
        for i in range(8)
    ]
    found = audit(ledger, experiments, [], now=_T0 + timedelta(hours=3))
    calibration = found.calibration
    assert calibration is not None
    assert calibration.misses_low == 8
    assert calibration.hits == 0
    assert calibration.sign_p is not None
    assert calibration.sign_p < 0.01
    assert calibration.mean_offset is not None
    assert calibration.mean_offset < -1


def test_unmirrored_and_unparented_rows_are_counted() -> None:
    mirror = Experiment(seq=9, title="H27", labels=("ledger:b2",), created=_T0)
    found = audit(_LEDGER, [mirror], [], now=_T0)
    assert found.unmirrored == ["a1", "c3"]
    assert found.unparented == 2


def test_beliefs_needing_attention_are_flagged() -> None:
    experiment = Experiment(seq=5, title="FINAL_DK1_solar", created=_T0)
    against = Experiment(seq=6, title="June 2025 dev check", created=_T0)
    belief = Belief(
        seq=37,
        title="DK1 solar beats MET out-of-sample",
        judgement="proven",
        confidence=0.9,
        proved_by=(
            ArtifactEdge(id=experiment.id, kind="Experiment", valence=0.5),
            ArtifactEdge(id=against.id, kind="Experiment", valence=-0.5),
        ),
        created=_T0,
    )
    reviewed = Belief(
        seq=38,
        title="Reviewed and evidenced",
        judgement="proven",
        confidence=0.9,
        labels=("reviewed",),
        proved_by=(ArtifactEdge(id=experiment.id, kind="Experiment", valence=0.5),),
        created=_T0,
    )
    found = audit([], [experiment, against], [belief, reviewed], now=_T0)
    reasons = {(flag.belief, flag.reason.split(" ")[0]) for flag in found.beliefs}
    assert (37, "confidence") in reasons
    assert (37, "still") in reasons
    assert all(flag.belief != 38 for flag in found.beliefs)


def test_beliefs_going_unwritten_while_results_arrive_is_flagged() -> None:
    old = Belief(
        seq=1, title="old", created=_T0 - timedelta(days=5), labels=("reviewed",)
    )
    found = audit(_LEDGER, [], [old], now=_T0 + timedelta(days=1))
    assert found.results_since_last_belief == 3
    assert any(
        "findings are not being recorded" in flag.reason for flag in found.beliefs
    )
    assert "findings are not being recorded" in render(found)


def test_read_ledger_skips_a_torn_line(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text('{"id": "a"}\n{"id": "b"}\n{"id": ')
    assert [row["id"] for row in read_ledger(path)] == ["a", "b"]
