"""The ledger mirror creates one Experiment per ledger row, against PGlite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import json

import pytest
import pytest_asyncio

from trackinizer.addons.ledger_mirror import (
    MirrorService,
    _Ledger,
    issue_prefix,
    outcome_line,
)
from trackinizer.lib.postgres.testing import reset_schema
from trackinizer.server.embedders.stub import StubEmbedder
from trackinizer.server.store.core import Store
from trackinizer.types.inquiries import Experiment, Issue
from trackinizer.wire.bodies import SubmitIssue


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from trackinizer.lib.postgres import PGliteEngine


@pytest_asyncio.fixture(loop_scope="session")
async def store(pglite_engine: PGliteEngine) -> AsyncIterator[Store]:
    """Return a bootstrapped Store over the session's shared PGlite engine."""
    await reset_schema(pglite_engine)
    built = Store(pglite_engine, embed=StubEmbedder())
    await built.bootstrap()
    yield built


def _row(row_id: str, run_id: str, sha: str = "abc123") -> dict[str, object]:
    return {
        "id": row_id,
        "run_id": run_id,
        "sha": sha,
        "hypothesis": f"{run_id} helps.",
        "metrics": {
            "dev": {
                "pairs": {
                    "DK1/wind_mw": {
                        "skill": -0.007,
                        "skill_ci90": [-0.033, 0.019],
                        "mae": 245.7,
                    },
                    "DK1/solar_mw": {"skill": None},
                },
            },
        },
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_rows_become_experiments_once(store: Store, tmp_path: Path) -> None:
    await store.submit(
        SubmitIssue(title="H27: PIT offshore unavailability", account="t"),
        api_key_id=None,
        actor="t",
    )
    ledger = tmp_path / "ledger.jsonl"
    _write(
        ledger,
        [_row("a1", "H27_wind_unavail"), _row("b2", "PM1_tracker", sha="def456")],
    )
    service = MirrorService(ledgers=[_Ledger(name="forecast", path=str(ledger))])

    assert await service.sweep(store) == 2
    assert await service.sweep(store, force=True) == 0

    experiments = [
        e for e in await store.list_kind("Experiment") if isinstance(e, Experiment)
    ]
    by_title = {e.title: e for e in experiments}
    assert set(by_title) == {"H27_wind_unavail", "PM1_tracker"}
    h27 = by_title["H27_wind_unavail"]
    assert "ledger:a1" in (h27.labels or ())
    assert h27.status == "complete"
    assert h27.outcome == "dev DK1/wind_mw: -0.7% CI90 [-3.3%,+1.9%]; MAE 245.7"
    assert len(h27.codechanges or ()) == 1

    issues = {
        i.title: i for i in await store.list_kind("Issue") if isinstance(i, Issue)
    }
    stub = issues["UNFILED forecast: ledger runs with no hypothesis Issue"]
    assert [edge.id for edge in by_title["PM1_tracker"].produced_by] == [stub.id]
    assert [edge.id for edge in h27.produced_by] == [
        issues["H27: PIT offshore unavailability"].id
    ]


@pytest.mark.db_pglite
@pytest.mark.asyncio(loop_scope="session")
async def test_appended_rows_are_picked_up(store: Store, tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _write(ledger, [_row("a1", "S_S1_base")])
    service = MirrorService(ledgers=[_Ledger(name="market", path=str(ledger))])
    assert await service.sweep(store) == 1
    assert await service.sweep(store) == 0  # unchanged size: not even read
    _write(ledger, [_row("a1", "S_S1_base"), _row("c3", "S_S2_more")])
    assert await service.sweep(store) == 1


def test_issue_prefix() -> None:
    assert issue_prefix({"run_id": "H27b_wind_park"}) == "H27b"
    assert issue_prefix({"run_id": "PLACEBO_H29_wind_spread"}) == "H29"
    assert issue_prefix({"run_id": "ORACLE_O2_solar_price"}) == "H26"
    assert issue_prefix({"run_id": "live_h25h27"}) == "H14"
    assert issue_prefix({"run_id": "PM1_tracker"}) is None


def test_outcome_line_includes_trade_pnl() -> None:
    line = outcome_line(
        {
            "dev": {
                "pairs": {
                    "DE/ida1_dah": {"skill": 0.25, "mae": 4.1, "trade": {"pnl": 0.2424}}
                }
            }
        },
    )
    assert line == "dev DE/ida1_dah: +25.0%; MAE 4.1; PnL 0.242"
    assert outcome_line({}) == "unscored"
