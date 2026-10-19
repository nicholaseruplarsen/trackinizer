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
    assert prediction.verdict == "miss"
    assert prediction.direction == "low"
    assert prediction.source == "ledger"


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


def _judged(
    seq: int, outcome: str, author: str = "a", metric: str = "pnl_eur_mwh"
) -> Experiment:
    return Experiment(
        seq=seq,
        title=f"PRED {seq}",
        labels=("prereg",),
        config={
            "prediction": {
                "run_id": f"X{seq}",
                "pair": "IE/ida1_dah",
                "metric": metric,
                "lo": 0.0,
                "hi": None,
                "author": author,
            }
        },
        outcome=outcome,
        created=_T0,
    )


def test_an_agents_written_verdict_is_read_for_any_metric() -> None:
    experiments = [
        _judged(1, "HIT: MET adds +0.08 on IE ida1_dah"),
        _judged(2, "MISS (high): S4 dev +0.41 vs prereg +0.05..+0.35", author="b"),
        _judged(3, "MISS (low, n.s.): replay -0.08"),
        _judged(4, "HALF: DE 0.433 HIT, DK1 0.212 MISS"),
        _judged(5, "9/9 HIT (low-information: near-determined)"),
        _judged(6, "3 HIT / 4 MISS. GBM-HLG vs P_v5"),
        _judged(7, "(1) MISS: PC1 = all-day level shift"),
        _judged(8, "NULL (primary interval HIT, effect MISS)"),
        _judged(9, ""),
    ]
    found = audit([], experiments, [], now=_T0)
    verdicts = [(p.verdict, p.direction) for p in found.predictions]
    assert verdicts == [
        ("hit", None),
        ("miss", "high"),
        ("miss", "low"),
        ("half", None),
        ("hit", None),
        ("half", None),
        ("miss", None),
        ("null", None),
        ("pending", None),
    ]
    calibration = found.calibration
    assert calibration is not None
    assert (calibration.resolved, calibration.hits, calibration.halves) == (7, 2, 2)
    assert (
        calibration.misses_low,
        calibration.misses_high,
        calibration.misses_unstated,
    ) == (1, 1, 1)
    assert calibration.nulls == 1
    assert calibration.uninformative == 1
    assert calibration.by_author["b"] == (0, 0, 1)
    assert abs(calibration.hit_rate - 3 / 7) < 1e-9
    text = render(found)
    assert "hit rate 43%" in text
    assert "1 direction unstated" in text


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


def test_a_comma_joined_label_counts_as_prereg() -> None:
    experiment = Experiment(
        seq=1,
        title="PRED",
        labels=("prereg,theory,qb8",),
        config={"prediction": {"run_id": "Q", "metric": "r2", "lo": 0.1, "hi": None}},
        outcome="HIT: R2 0.2",
        created=_T0,
    )
    found = audit([], [experiment], [], now=_T0)
    assert [p.verdict for p in found.predictions] == ["hit"]


def test_error_budget_lists_oracles_and_unmeasured_components() -> None:
    oracle = Experiment(
        seq=7,
        title="O1 weather oracle",
        labels=("oracle",),
        config={
            "oracle": [
                {
                    "pair": "DK1/wind_mw",
                    "component": "weather",
                    "headroom_pt": 14.0,
                    "ci": [12.0, 16.0],
                    "run_id": "ORACLE_O1",
                },
            ],
        },
        created=_T0,
    )
    ledger = [
        _row("o1", "ORACLE_O1", 1, {"DK1/wind_mw": 12.5}),
        _row("o2", "ORACLE_O2_solar_price", 1, {"DK1/solar_mw": 1.5}),
    ]
    found = audit(
        ledger,
        [oracle],
        [],
        now=_T0,
        pairs=("DK1/wind_mw", "DK1/solar_mw"),
        components=("weather", "curtailment"),
    )
    assert [(o.pair, o.component, o.headroom_pt) for o in found.oracles] == [
        ("DK1/wind_mw", "weather", 14.0),
        ("DK1/solar_mw", "unlabelled", 1.5),
    ]
    assert found.unmeasured == {
        "DK1/wind_mw": ["curtailment"],
        "DK1/solar_mw": ["weather", "curtailment"],
    }
    assert "DK1/solar_mw unmeasured: weather, curtailment" in render(found)
