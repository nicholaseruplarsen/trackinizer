"""Theories, Predictions and the theory edges, against PGlite.

A Prediction is frozen at submit: its quantity, quantiles, maker and
registration time are refused on edit, and only the scorer's resolution moves.
The six theory edges are admitted only between the kinds ``docs/epistemy.md``
names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.types.errors import ConflictError
from trackinizer.types.inquiries import Belief, Prediction, Theory
from trackinizer.wire.bodies import (
    SubmitBelief,
    SubmitExperiment,
    SubmitPrediction,
    SubmitTheory,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trackinizer.lib.postgres import PGliteEngine


ACCOUNT = "tester@example.com"
QUANTILES = {"q05": 11.0, "q25": 11.8, "q50": 12.3, "q75": 12.9, "q95": 13.8}


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_theory_round_trips_with_its_fit_and_rivals(store: Store) -> None:
    rival = await store.submit_theory(
        SubmitTheory(account=ACCOUNT, title="Saturated by 180 days", explains="mae"),
    )
    theory = await store.submit_theory(
        SubmitTheory(
            account=ACCOUNT,
            title="Data scaling",
            explains="dk1.wind.mae",
            form="MAE(D) = Einf + B * D^-beta",
            variables={"D": {"unit": "days", "fitted_range": [90, 365]}},
            parameters={"beta": {"mean": 0.4, "lo90": 0.3, "hi90": 0.55}},
            regime="DK1/DK2 wind, dev window",
            fit_ref="src/fit_scaling.py@abc1234",
            rivals=[rival],
        ),
    )
    row = await store.get_inquiry(theory)
    assert isinstance(row, Theory)
    assert row.form == "MAE(D) = Einf + B * D^-beta"
    assert row.parameters == {"beta": {"mean": 0.4, "lo90": 0.3, "hi90": 0.55}}
    assert row.regime == "DK1/DK2 wind, dev window"
    async with store.engine.acquire() as conn:
        edge = await conn.fetchrow(
            "SELECT from_id, to_id FROM edges WHERE edge_kind = 'rivals'",
        )
    # Stored once per pair, lower seq (the older rival) first.
    assert (edge["from_id"], edge["to_id"]) == (rival, theory)

    await store.set_parameters(
        theory,
        {"beta": {"mean": 0.45, "lo90": 0.35, "hi90": 0.6}},
        actor="tester",
        reason="refit with D=540",
    )
    refit = await store.get_inquiry(theory)
    assert isinstance(refit, Theory)
    assert refit.parameters == {"beta": {"mean": 0.45, "lo90": 0.35, "hi90": 0.6}}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_prediction_is_frozen_at_submit(store: Store) -> None:
    theory = await store.submit_theory(SubmitTheory(account=ACCOUNT, title="Law"))
    before = datetime.now(UTC)
    prediction = await store.submit_prediction(
        SubmitPrediction(
            account=ACCOUNT,
            title="MAE at D=540",
            quantity={"key": "dk1.wind.mae", "better": "lower", "run_id": "R540"},
            quantiles=QUANTILES,
            made_by="theory",
            theory=theory,
        ),
    )
    row = await store.get_inquiry(prediction)
    assert isinstance(row, Prediction)
    assert row.quantiles == QUANTILES
    assert row.made_by == "theory"
    assert row.registered is not None
    assert row.registered >= before

    with pytest.raises(ConflictError):
        await store._set_field(  # noqa: SLF001 -- no route exists to try.
            prediction,
            {"q05": 0.0, "q25": 1.0, "q50": 2.0, "q75": 3.0, "q95": 4.0},
            column="prediction_quantiles",
            api_key_id=None,
            actor="tester",
        )
    async with store.engine.acquire() as conn:
        kinds = {
            r["edge_kind"]
            for r in await conn.fetch(
                "SELECT edge_kind FROM edges WHERE from_id = $1", prediction
            )
        }
    assert kinds == {"predicts"}

    await store.set_resolution(
        prediction,
        {"status": "resolved", "observed": 12.1, "pit": 0.42},
        actor="scorer",
    )
    resolved = await store.get_inquiry(prediction)
    assert isinstance(resolved, Prediction)
    assert resolved.resolution == {"status": "resolved", "observed": 12.1, "pit": 0.42}


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_coord_import_keeps_coords_registration_time(store: Store) -> None:
    registered = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)
    prediction = await store.submit_prediction(
        SubmitPrediction(
            account=ACCOUNT,
            title="coord e1-p0001",
            quantity={"key": "x", "ticket": "e1-0001", "file": "metrics.json"},
            quantiles=QUANTILES,
        ),
        registered=registered,
    )
    row = await store.get_inquiry(prediction)
    assert isinstance(row, Prediction)
    assert row.registered == registered


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_the_theory_edges_join_only_their_kinds(store: Store) -> None:
    theory = await store.submit_theory(SubmitTheory(account=ACCOUNT, title="T"))
    prediction = await store.submit_prediction(
        SubmitPrediction(
            account=ACCOUNT,
            title="P",
            quantity={"key": "x"},
            quantiles=QUANTILES,
        ),
    )
    experiment = await store.submit_experiment(
        SubmitExperiment(account=ACCOUNT, title="E"),
    )
    # The dependent is the older Belief: #6 assumes #42, linked after both exist.
    dependent = await store.submit_belief(SubmitBelief(account=ACCOUNT, title="B6"))
    assumption = await store.submit_belief(SubmitBelief(account=ACCOUNT, title="B42"))

    for from_id, to_id, kind in (
        (prediction, theory, "predicts"),
        (experiment, prediction, "tests"),
        (experiment, theory, "fits"),
        (dependent, assumption, "assumes"),
        (dependent, theory, "about"),
    ):
        _, created = await store.add_edge(
            from_id=from_id, to_id=to_id, edge_kind=kind, actor="tester"
        )
        assert created, kind

    with pytest.raises(Exception):  # noqa: B017, PT011 -- the edge CHECK refuses it.
        await store.add_edge(
            from_id=theory, to_id=prediction, edge_kind="predicts", actor="tester"
        )


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_a_belief_carries_earned_confidence_and_review_state(
    store: Store,
) -> None:
    belief = await store.submit_belief(
        SubmitBelief(account=ACCOUNT, title="06Z adds 2-3 pt", confidence=0.8),
    )
    await store.set_earned_confidence(belief, 0.31, actor="scorer")
    await store.set_needs_review(belief, True, actor="scorer")  # noqa: FBT003
    await store.set_review_note(belief, "still holds after #42", actor="tester")
    row = await store.get_inquiry(belief)
    assert isinstance(row, Belief)
    assert row.confidence == 0.8
    assert row.earned_confidence == 0.31
    assert row.needs_review is True
    assert row.review_note == "still holds after #42"
