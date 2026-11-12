"""The knowledge pass against PGlite: coord import, native scoring, house prior, review."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import json
import sqlite3

import pytest
import pytest_asyncio

from trackinizer.addons.knowledge import (
    CoordSnapshot,
    KnowledgeService,
    summarise,
)
from trackinizer.addons.scoring import empirical_quantiles, score
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import Belief, Experiment, Prediction, Theory
from trackinizer.wire.bodies import SubmitBelief, SubmitExperiment


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from trackinizer.lib.postgres import PGliteEngine


Q = {"q05": 11.0, "q25": 11.8, "q50": 12.3, "q75": 12.9, "q95": 13.8}
T0 = datetime(2026, 11, 1, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


def _service(tmp_path: Path, ledger: Path) -> KnowledgeService:
    return KnowledgeService(
        coord_host="",
        cache_dir=tmp_path / "coord",
        ledger_paths=(str(ledger),),
        house_prior_metric="delta_pt",
        house_prior_path=tmp_path / "house-prior.json",
    )


def _coord_row(pid: str, *, theory: str, observed: float | None, ticket: str) -> dict[str, object]:
    spec = {
        "quantity": {"file": "metrics.json", "key": "dk1.wind.mae", "better": "lower"},
        "quantiles": Q,
        "theory": theory,
        "source": "theory",
        "note": "D=540",
    }
    row: dict[str, object] = {
        "id": pid,
        "effort": "e1",
        "registered": int(T0.timestamp()),
        "spec": json.dumps(spec),
        "ticket": ticket,
        "r2_key": None,
        "theory": theory,
        "source": "theory",
        "status": "open",
    }
    if observed is not None:
        row.update(status="resolved", resolved=int(T0.timestamp()) + 3600, **score(Q, observed))
    return row


def _snapshot(*, deleted_belief: bool = False) -> CoordSnapshot:
    beliefs = [
        {"id": "B42", "claim": "dev NWP used the 12:00 gate", "theory": None, "stated": 0.9,
         "assumes": [], "cites": ["e1-0001"], "updated": "2026-11-02T10:00:00", "reviewed": None},
    ]
    if not deleted_belief:
        beliefs.append(
            {"id": "B6", "claim": "06Z adds 2-3 pt", "theory": "T-scaling", "stated": 0.8,
             "assumes": ["B42"], "cites": ["e1-0001", "e1-p0001"], "updated": "2026-11-01T10:00:00",
             "reviewed": None},
        )
    return CoordSnapshot(
        predictions=[
            _coord_row("e1-p0001", theory="T-scaling", observed=12.1, ticket="e1-0001"),
            _coord_row("e1-p0002", theory="T-saturated", observed=12.1, ticket="e1-0001"),
        ],
        tickets=[
            {"id": "e1-0001", "effort": "e1", "created": 0, "state": "done",
             "spec": json.dumps({"title": "D=540 retrain"}), "detail": "ok"},
        ],
        theories={
            "e1": [
                {"id": "T-scaling", "quantity": "dk1.wind.mae", "form": "MAE(D) = Einf + B*D^-beta",
                 "variables": {"D": {"unit": "days", "fitted_range": [90, 365]}},
                 "parameters": {"beta": {"mean": 0.4, "lo90": 0.3, "hi90": 0.55}},
                 "regime": "DK1 wind", "rivals": ["T-saturated"], "fit_ref": "fit.py@abc"},
                {"id": "T-saturated", "quantity": "dk1.wind.mae", "form": "flat after 180 d",
                 "parameters": {}, "regime": "DK1 wind", "rivals": ["T-scaling"]},
            ],
        },
        beliefs={"e1": beliefs},
        status={
            "e1": {
                "ts": int(T0.timestamp()),
                "theories": {"T-scaling": {"n_predictions": 1, "logscore": 0.3, "earned": 0.6,
                                           "shared_with_rivals": 1, "rivals": ["T-saturated"], "no_rivals": False}},
                "beliefs": {"B6": {"stated": 0.8, "earned": 0.6, "gap": 0.2, "needs_review": True,
                                   "because": ["B42"], "missing_assumes": []}},
                "flags": ["Belief B6 needs review: assumes B42, changed since"],
            },
        },
    )


async def _by_label(store: Store, kind: str, label: str) -> object:
    for row in await store.list_kind(kind, limit=10_000):  # pyright: ignore[reportArgumentType]
        if label in (row.labels or ()):
            return row
    return None


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_coord_import_is_idempotent_and_keeps_coords_scores(
    store: Store, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    service = _service(tmp_path, ledger)
    await service.sweep(store, snapshot=_snapshot())
    await service.sweep(store, snapshot=_snapshot())

    predictions = [
        p for p in await store.list_kind("Prediction", limit=100) if isinstance(p, Prediction)
    ]
    assert len(predictions) == 2
    p1 = await _by_label(store, "Prediction", "coord:e1-p0001")
    assert isinstance(p1, Prediction)
    assert p1.registered == T0
    assert p1.quantiles == Q
    assert p1.resolution is not None
    assert p1.resolution["status"] == "resolved"
    assert p1.resolution["scorer"] == "coord"
    assert "shadow_disagrees" not in p1.resolution

    theory = await _by_label(store, "Theory", "coord:e1/T-scaling")
    assert isinstance(theory, Theory)
    assert theory.record is not None
    assert theory.record["earned"] == 0.6

    b6 = await _by_label(store, "Belief", "coord:e1/B6")
    assert isinstance(b6, Belief)
    assert b6.confidence == 0.8
    assert b6.earned_confidence == 0.6
    assert b6.needs_review is True

    experiment = await _by_label(store, "Experiment", "coord:e1-0001")
    assert isinstance(experiment, Experiment)
    assert experiment.status == "complete"

    async with store.engine.acquire() as conn:
        kinds = sorted(
            r["edge_kind"]
            for r in await conn.fetch("SELECT edge_kind FROM edges WHERE edge_kind <> 'produced_by'")
        )
    # 2 predicts, 2 tests, 1 rivals, 1 about, 1 assumes, 3 favors (B42<-ticket, B6<-ticket, B6<-p1).
    assert kinds == sorted(
        ["predicts"] * 2 + ["tests"] * 2 + ["rivals", "about", "assumes"] + ["favors"] * 3
    )

    found = await summarise(store, service, now=T0 + timedelta(days=1))
    assert found["import"]["shadow_disagreements"] == []  # pyright: ignore[reportIndexIssue]
    assert found["calibration"]["all"]["n"] == 2  # pyright: ignore[reportIndexIssue]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_tampered_coord_score_is_reported_and_a_deleted_file_retires(
    store: Store, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    service = _service(tmp_path, ledger)
    await service.sweep(store, snapshot=_snapshot())
    snapshot = _snapshot(deleted_belief=True)
    snapshot.predictions[1]["pit"] = 0.99
    snapshot.predictions[1]["status"] = "resolved"
    await service.sweep(store, snapshot=snapshot)
    assert [d["prediction"] for d in service.report.shadow_disagreements] == ["e1-p0002"]
    b6 = await _by_label(store, "Belief", "coord:e1/B6")
    assert isinstance(b6, Belief)
    assert b6.status == "invalid"


def _ledger_row(run: str, skill: float, when: datetime, pair: str = "DK1/wind_mw") -> str:
    return json.dumps(
        {"id": run, "run_id": run, "utc": when.isoformat(),
         "metrics": {"dev": {"pairs": {pair: {"skill": skill}}}}}
    )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_preregs_resolve_from_the_ledger_and_feed_the_house_prior(
    store: Store, tmp_path: Path
) -> None:
    await reset_schema(store.engine)  # pyright: ignore[reportArgumentType]
    await store.bootstrap()
    now = datetime.now(UTC)
    rows = [_ledger_row("PARENT", 0.10, now - timedelta(days=30))]
    deltas = [0.5 * (i % 7) - 1.0 for i in range(30)]
    for i, delta in enumerate(deltas):
        exp = await store.submit_experiment(
            SubmitExperiment(
                account="agent@x",
                title=f"prereg {i}",
                labels=["prereg"],
                config={"prediction": {"run_id": f"R{i}", "pair": "DK1/wind_mw", "metric": "delta_pt",
                                       "parent": "PARENT", "lo": 0.0, "hi": 2.0}},
            ),
        )
        assert exp
        # The result lands after the registration, so nothing is late.
        rows.append(_ledger_row(f"R{i}", 0.10 + delta / 100, datetime.now(UTC) + timedelta(minutes=i)))
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(rows) + "\n")
    service = _service(tmp_path, ledger)
    await service.sweep(store, snapshot=None)

    predictions = [
        p for p in await store.list_kind("Prediction", limit=100) if isinstance(p, Prediction)
    ]
    assert len(predictions) == 30
    assert all("legacy-interval" in (p.labels or ()) for p in predictions)
    resolved = [p for p in predictions if (p.resolution or {}).get("status") == "resolved"]
    assert len(resolved) == 30
    assert {p.resolution["verdict"] for p in resolved} == {"hit", "miss"}  # pyright: ignore[reportOptionalSubscript]
    assert "pit" not in resolved[0].resolution  # pyright: ignore[reportOperatorIssue]

    prior = json.loads((tmp_path / "house-prior.json").read_text())
    assert prior["n"] == 30
    expected = empirical_quantiles(deltas)
    assert prior["quantiles"]["q50"] == pytest.approx(expected["q50"], abs=1e-3)
    theory = await _by_label(store, "Theory", "house-prior")
    assert isinstance(theory, Theory)
    # Every backtest point used only outcomes registered before it: none were, so none.
    assert service.backtest == []

    found = await summarise(store, service)
    assert found["calibration"]["all"]["n"] == 0  # pyright: ignore[reportIndexIssue]  -- legacy rows stay out.


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_changed_assumption_flags_the_belief_until_reviewed(
    store: Store, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    service = _service(tmp_path, ledger)
    b6 = await store.submit_belief(SubmitBelief(account="a@x", title="06Z adds 2-3 pt", confidence=0.8))
    b42 = await store.submit_belief(SubmitBelief(account="a@x", title="dev NWP used the 12:00 gate"))
    await store.add_edge(from_id=b6, to_id=b42, edge_kind="assumes", actor="tester")
    await service.sweep(store, snapshot=None)
    row = await store.get_inquiry(b6)
    assert isinstance(row, Belief)
    # #42 was written after #6 (as on Windrunner), so #6 needs review at once.
    assert row.needs_review is True

    await store.set_needs_review(b6, False, actor="nicholas", reason="still holds")  # noqa: FBT003
    await service.sweep(store, snapshot=None)
    row = await store.get_inquiry(b6)
    assert isinstance(row, Belief)
    assert row.needs_review is False

    await store.set_confidence(b42, 0.95, actor="agent")
    await service.sweep(store, snapshot=None)
    row = await store.get_inquiry(b6)
    assert isinstance(row, Belief)
    assert row.needs_review is True
    stale = (await summarise(store, service))["beliefs"]
    assert any(b["seq"] == row.seq and b["needs_review"] for b in stale)  # pyright: ignore[reportGeneralTypeIssues]


def test_snapshot_reads_a_pulled_mirror(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    (export / "predictions.jsonl").write_text(json.dumps(_coord_row("e1-p0001", theory="T", observed=None, ticket="e1-0001")) + "\n")
    db = sqlite3.connect(export / "ledger.sqlite")
    db.execute("CREATE TABLE tickets(id TEXT, effort TEXT, created INTEGER, state TEXT, spec TEXT, "
               "started INTEGER, ended INTEGER, spent REAL, detail TEXT)")
    db.execute("INSERT INTO tickets VALUES ('e1-0001','e1',0,'done','{}',1,2,0.1,'')")
    db.commit()
    db.close()
    theories = tmp_path / "knowledge" / "e1" / "theories"
    theories.mkdir(parents=True)
    (theories / "T.json").write_text(json.dumps({"id": "T", "form": "x"}))
    (theories / "wrong.json").write_text(json.dumps({"id": "other"}))
    (tmp_path / "knowledge" / "e1.json").write_text(json.dumps({"flags": ["x"]}))
    found = CoordSnapshot.read(tmp_path)
    assert [p["id"] for p in found.predictions] == ["e1-p0001"]
    assert [t["id"] for t in found.tickets] == ["e1-0001"]
    assert [t["id"] for t in found.theories["e1"]] == ["T"]
    assert found.status["e1"]["flags"] == ["x"]


@pytest.mark.parametrize(
    ("outcome", "value"),
    [
        ("HIT. HU S2F vs S2FP +3.93 [2.15,5.75] (pred 1..6).", 3.93),
        ("MISS (low). BE L5_hyb vs L5_full -6.1 pts CI90 [-10.7,-1.8] (pred 0..8)", -6.1),
        ("MISS (high). DE wind skill R0 -24.3 -> R1 -19.3; delta R1-R0 +4.94 pts CI90 [+1.92,+7.79]", 4.94),
        ("MISS (low): F26L_blend_full_DK2 delta +4.92 pts, CI90 [1.08,8.23] vs prereg [5,18]", 4.92),
        ("HIT. no number here", None),
    ],
)
def test_reported_delta_reads_the_head_of_an_outcome(outcome: str, value: float | None) -> None:
    from trackinizer.addons.knowledge import reported_delta

    assert reported_delta(outcome) == value
