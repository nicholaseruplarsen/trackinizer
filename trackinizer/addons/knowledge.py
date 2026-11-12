"""Theories, Predictions and calibration: import, score, and summarise.

This addon keeps the theory layer (``docs/epistemy.md``) current inside the
server, and serves it at ``GET /api/addons/knowledge/summary``. Each pass:

1. **Imports coord.** Research boxes register predictions with coord and keep
   their Theories and Beliefs as JSON in ``rs-eN/knowledge/``; they cannot
   reach this server. coord's read-only mirror is pulled over rsync
   (``coord-mirror:/export/`` and ``coord-mirror:/knowledge/``) and turned into
   rows, each keyed by a ``coord:<id>`` label:

   - a Prediction per row of ``export/predictions.jsonl`` (``coord:e1-p0007``),
     frozen with coord's own registration time, with coord's resolution
     imported unchanged and recomputed here in shadow;
   - an Experiment per coord ticket (``coord:e1-0012``), with a ``tests`` edge
     to each Prediction bound to it;
   - a Theory per ``knowledge/<eN>/theories/<id>.json`` (``coord:e1/<id>``),
     with ``rivals`` edges and coord's record (earned weight, log score);
   - a Belief per ``knowledge/<eN>/beliefs/<id>.json`` (``coord:e1/<id>``),
     with ``about``, ``assumes`` and ``favors`` (label ``coord-cite``) edges,
     and coord's earned confidence and ``needs_review`` flag.

   A file deleted in R2 disappears from the mirror; its row is marked invalid.

2. **Scores Windrunner's own predictions** (every Prediction not from coord)
   from the scorer ledgers, with the same maths as coord
   (:mod:`trackinizer.addons.scoring`). A legacy ``prereg`` Experiment's
   ``config.prediction`` becomes a Prediction first (label ``prereg:<seq>``);
   one with no quantiles is labelled ``legacy-interval`` and kept out of PIT
   and coverage. A Prediction whose value was in the ledger before it was
   registered resolves ``late``.

3. **Fits the house prior**: the empirical quantiles of every realised
   ``delta_pt`` (a run's skill minus its parent's), kept as the Theory
   labelled ``house-prior``. Its backtest scores each outcome against a prior
   fitted only on outcomes resolved before that prediction was registered.

4. **Derives the record** for Windrunner's own Theories (earned weight against
   rivals on shared quantities) and Beliefs (``earned_confidence`` through an
   ``about`` edge), and flags ``needs_review`` on every Belief whose
   ``assumes`` target changed after it was last updated or reviewed.

Everything this addon writes is attributed to ``trackinizer-knowledge``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import asyncio
import json
import logging
import re
import sqlite3
import subprocess
import uuid

from configgle import Fig
from fastapi import APIRouter, Request

from trackinizer.addons.addon import AddonManifest, ServerContext
from trackinizer.addons.research import read_ledger
from trackinizer.addons.scoring import (
    QK,
    calibration,
    earned_weights,
    empirical_quantiles,
    score,
)
from trackinizer.server.api._deps import get_store
from trackinizer.types.inquiries import (
    Belief,
    Experiment,
    Inquiry,
    Prediction,
    Theory,
)
from trackinizer.wire.bodies import (
    LEGACY_INTERVAL_LABEL,
    SubmitBelief,
    SubmitExperiment,
    SubmitPrediction,
    SubmitTheory,
)


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


__all__ = ["Knowledge", "KnowledgeService", "summarise"]

_LOGGER = logging.getLogger(__name__)

ACTOR = "trackinizer-knowledge"
"""The audit author of every row and edge this addon writes."""

_NAMESPACE = uuid.UUID("6f1d3c7e-9a52-4d0b-8f63-2c4e1b7a9d10")
COORD = "coord:"
PREREG = "prereg:"
HOUSE_PRIOR = "house-prior"
COORD_CITE = "coord-cite"
_TICKET = re.compile(r"^e\d+-\d{4}$")
_PREDICTION = re.compile(r"^e\d+-p\d{4}$")
_EFFORT = re.compile(r"^e\d+$")
_SHADOW_TOLERANCE = 1e-9
_SCORE_KEYS = ("pit", "is50", "is90", "logscore", "cover50", "cover90", "optimism")
_CONTENT_KINDS = ("title", "description", "belief_judgement", "belief_confidence", "status")


def _key(labels: Sequence[str] | None, prefix: str) -> str | None:
    """Return the first label with ``prefix``, without it."""
    for label in labels or ():
        if label.startswith(prefix):
            return label.removeprefix(prefix)
    return None


def _ts(value: object) -> datetime | None:
    """Read a unix time or ISO string as an aware datetime."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str) and value:
        try:
            found = datetime.fromisoformat(value)
        except ValueError:
            return None
        return found if found.tzinfo else found.replace(tzinfo=UTC)
    return None


def _num(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


def _quantiles(value: object) -> dict[str, float] | None:
    """Return valid q05..q95, or None."""
    if not isinstance(value, Mapping):
        return None
    try:
        found = {k: float(cast(float, value[k])) for k in QK}
    except (KeyError, TypeError, ValueError):
        return None
    ordered = [found[k] for k in QK]
    if any(b < a for a, b in zip(ordered, ordered[1:], strict=False)) or ordered[0] == ordered[-1]:
        return None
    return found


# ---------------------------------------------------------------- the ledger


def _skill_pt(row: Mapping[str, object], window: str, pair: str) -> float | None:
    metrics = row.get("metrics")
    scored = metrics.get(window) if isinstance(metrics, Mapping) else None
    pairs = scored.get("pairs") if isinstance(scored, Mapping) else None
    found = pairs.get(pair) if isinstance(pairs, Mapping) else None
    skill = found.get("skill") if isinstance(found, Mapping) else None
    return float(skill) * 100 if isinstance(skill, int | float) else None


@dataclass(slots=True)
class _Ledger:
    """The scorer ledgers, indexed by run id (newest row wins)."""

    rows: dict[str, Mapping[str, object]] = field(default_factory=dict)

    @classmethod
    def read(cls, paths: Sequence[str]) -> _Ledger:
        found = cls()
        for path in paths:
            for row in read_ledger(path):
                run = row.get("run_id")
                if isinstance(run, str) and run:
                    found.rows[run] = row
        return found

    def observe(self, quantity: Mapping[str, object]) -> tuple[float, datetime] | None:
        """Read a ``skill_pt``/``delta_pt`` quantity off the ledger, with its time."""
        run = str(quantity.get("run_id") or "")
        metric = str(quantity.get("metric") or quantity.get("key") or "")
        pair = str(quantity.get("pair") or "")
        row = self.rows.get(run)
        if row is None or metric not in ("skill_pt", "delta_pt") or not pair:
            return None
        window = str(quantity.get("window") or "dev").split()[0]
        value = _skill_pt(row, window, pair)
        if value is not None and metric == "delta_pt":
            parent = self.rows.get(str(quantity.get("parent") or ""))
            base = None if parent is None else _skill_pt(parent, window, pair)
            value = None if base is None else value - base
        when = _ts(row.get("utc"))
        if value is None or when is None:
            return None
        return value, when


# ---------------------------------------------------------------- coord's mirror


@dataclass(frozen=True, slots=True, kw_only=True)
class CoordSnapshot:
    """What coord's read-only mirror held at one pull."""

    predictions: list[dict[str, object]] = field(default_factory=list)
    tickets: list[dict[str, object]] = field(default_factory=list)
    theories: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    beliefs: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    status: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def read(cls, root: Path) -> CoordSnapshot:
        """Read a pulled mirror: ``export/`` and ``knowledge/`` under ``root``."""
        predictions: list[dict[str, object]] = []
        jsonl = root / "export" / "predictions.jsonl"
        if jsonl.exists():
            for line in jsonl.read_text().splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("id"):
                    predictions.append(row)
        tickets: list[dict[str, object]] = []
        ledger = root / "export" / "ledger.sqlite"
        if ledger.exists():
            db = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                tickets = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id, effort, created, state, spec, started, ended, "
                        "spent, detail FROM tickets"
                    )
                ]
            except sqlite3.Error:
                tickets = []
            finally:
                db.close()
        theories: dict[str, list[dict[str, object]]] = {}
        beliefs: dict[str, list[dict[str, object]]] = {}
        status: dict[str, dict[str, object]] = {}
        knowledge = root / "knowledge"
        for effort_dir in sorted(knowledge.iterdir()) if knowledge.exists() else ():
            if effort_dir.is_dir() and _EFFORT.match(effort_dir.name):
                theories[effort_dir.name] = _read_dir(effort_dir / "theories")
                beliefs[effort_dir.name] = _read_dir(effort_dir / "beliefs")
            elif effort_dir.suffix == ".json" and _EFFORT.match(effort_dir.stem):
                try:
                    found = json.loads(effort_dir.read_text())
                except json.JSONDecodeError:
                    continue
                if isinstance(found, dict):
                    status[effort_dir.stem] = found
        return cls(
            predictions=predictions,
            tickets=tickets,
            theories=theories,
            beliefs=beliefs,
            status=status,
        )


def _read_dir(path: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for item in sorted(path.glob("*.json")) if path.exists() else ():
        try:
            found = json.loads(item.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(found, dict) and found.get("id") == item.stem:
            out.append(found)
    return out


def pull_mirror(host: str, root: Path) -> None:
    """rsync coord's ``export/`` and ``knowledge/`` into ``root`` (read-only key)."""
    root.mkdir(parents=True, exist_ok=True)
    for part in ("export", "knowledge"):
        subprocess.run(  # noqa: S603 -- fixed argv, host from config.
            [
                "rsync",
                "-a",
                "--delete",
                "-e",
                "ssh -o BatchMode=yes -o ConnectTimeout=15",
                f"{host}:/{part}/",
                f"{root / part}/",
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )


# ---------------------------------------------------------------- the graph


@dataclass(slots=True)
class _Graph:
    """Every row and theory edge one pass needs, read once."""

    theories: dict[uuid.UUID, Theory]
    predictions: dict[uuid.UUID, Prediction]
    beliefs: dict[uuid.UUID, Belief]
    experiments: dict[uuid.UUID, Experiment]
    edges: set[tuple[uuid.UUID, uuid.UUID, str]]
    labels: dict[str, uuid.UUID]
    """``coord:...`` / ``prereg:...`` / ``house-prior`` key -> row id."""

    @classmethod
    async def read(cls, store: Store) -> _Graph:
        def of[T](kind: str, cls_: type[T]) -> list[T]:
            return [r for r in rows[kind] if isinstance(r, cls_)]

        rows: dict[str, list[Inquiry]] = {}
        for kind in ("Theory", "Prediction", "Belief", "Experiment"):
            rows[kind] = await store.list_kind(
                cast(Inquiry.InquiryKind, kind), limit=100_000, edges=False
            )
        async with store.engine.acquire() as conn:
            edges = {
                (r["from_id"], r["to_id"], r["edge_kind"])
                for r in await conn.fetch(
                    "SELECT from_id, to_id, edge_kind FROM edges WHERE edge_kind = ANY($1)",
                    ["predicts", "tests", "rivals", "fits", "assumes", "about", "favors", "proves"],
                )
            }
        labels: dict[str, uuid.UUID] = {}
        for kind_rows in rows.values():
            for row in kind_rows:
                for label in row.labels or ():
                    if label.startswith((COORD, PREREG)) or label == HOUSE_PRIOR:
                        labels.setdefault(label, row.id)
        return cls(
            theories={r.id: r for r in of("Theory", Theory)},
            predictions={r.id: r for r in of("Prediction", Prediction)},
            beliefs={r.id: r for r in of("Belief", Belief)},
            experiments={r.id: r for r in of("Experiment", Experiment)},
            edges=edges,
            labels=labels,
        )

    def out(self, node: uuid.UUID, kind: str) -> list[uuid.UUID]:
        return [t for f, t, k in self.edges if f == node and k == kind]

    def into(self, node: uuid.UUID, kind: str) -> list[uuid.UUID]:
        return [f for f, t, k in self.edges if t == node and k == kind]


async def _link(
    store: Store,
    graph: _Graph,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
    *,
    valence: float | None = None,
    labels: Sequence[str] = (),
) -> None:
    if (from_id, to_id, kind) in graph.edges or from_id == to_id:
        return
    await store.add_edge(
        from_id=from_id,
        to_id=to_id,
        edge_kind=kind,  # pyright: ignore[reportArgumentType]
        valence=valence,
        labels=labels,
        actor=ACTOR,
    )
    graph.edges.add((from_id, to_id, kind))


async def _unlink(
    store: Store, graph: _Graph, from_id: uuid.UUID, to_id: uuid.UUID, kind: str
) -> None:
    if (from_id, to_id, kind) not in graph.edges:
        return
    await store.remove_edge(
        from_id=from_id,
        to_id=to_id,
        edge_kind=kind,  # pyright: ignore[reportArgumentType]
        actor=ACTOR,
        reason="no longer in coord's knowledge files",
    )
    graph.edges.discard((from_id, to_id, kind))


async def _rivals(store: Store, graph: _Graph, a: uuid.UUID, b: uuid.UUID) -> None:
    """Link a rival pair once, lower seq first."""
    if (a, b, "rivals") in graph.edges or (b, a, "rivals") in graph.edges:
        return
    first, second = sorted((a, b), key=lambda t: graph.theories[t].seq)
    await _link(store, graph, first, second, "rivals")


async def _set(
    store: Store, row: Inquiry, name: str, value: object, *, reason: str = ""
) -> bool:
    """Set one field when it differs; return whether it changed."""
    if getattr(row, name, None) == value:
        return False
    setter = getattr(store, f"set_{name}")
    await setter(row.id, value, actor=ACTOR, reason=reason)
    return True


# ---------------------------------------------------------------- import


@dataclass(slots=True)
class ImportReport:
    """What the last pass did, for the summary."""

    at: datetime | None = None
    pulled: datetime | None = None
    created: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)
    shadow_disagreements: list[dict[str, object]] = field(default_factory=list)
    coord_flags: dict[str, list[str]] = field(default_factory=dict)


async def import_coord(
    store: Store, snapshot: CoordSnapshot, report: ImportReport
) -> None:
    """Bring coord's tickets, predictions, theories and beliefs into the graph."""
    graph = await _Graph.read(store)
    report.shadow_disagreements = []
    report.coord_flags = {
        effort: [str(f) for f in cast(list[object], status.get("flags") or [])]
        for effort, status in snapshot.status.items()
    }

    for ticket in snapshot.tickets:
        await _import_ticket(store, graph, ticket, report)
    for effort, theories in snapshot.theories.items():
        for theory in theories:
            await _import_theory(store, graph, effort, theory, report)
    for row in snapshot.predictions:
        await _import_prediction(store, graph, row, report)
    for effort, theories in snapshot.theories.items():
        for theory in theories:
            tid = graph.labels.get(f"{COORD}{effort}/{theory['id']}")
            for rival in cast(list[object], theory.get("rivals") or []):
                other = graph.labels.get(f"{COORD}{effort}/{rival}")
                if tid and other:
                    await _rivals(store, graph, tid, other)
    for effort, beliefs in snapshot.beliefs.items():
        for belief in beliefs:
            await _import_belief(store, graph, effort, belief, report)
    for effort, beliefs in snapshot.beliefs.items():
        for belief in beliefs:
            await _belief_edges(store, graph, effort, belief)
    for effort, status in snapshot.status.items():
        await _import_status(store, graph, effort, status)
    await _retire_deleted(store, graph, snapshot)


async def _import_ticket(
    store: Store, graph: _Graph, ticket: Mapping[str, object], report: ImportReport
) -> None:
    tid = str(ticket.get("id") or "")
    if not _TICKET.match(tid):
        return
    try:
        spec = json.loads(str(ticket.get("spec") or "{}"))
    except json.JSONDecodeError:
        spec = {}
    state = str(ticket.get("state") or "")
    status: Inquiry.Status = (
        "complete"
        if state == "done"
        else "invalid"
        if state in ("cancelled", "failed", "lost")
        else "active"
    )
    key = f"{COORD}{tid}"
    if key not in graph.labels:
        title = str(spec.get("title") or spec.get("run") or "")[:120] if isinstance(spec, dict) else ""
        created = await store.submit(
            SubmitExperiment(
                title=f"{tid}: {title}" if title else tid,
                status=status,
                labels=["coord", key, f"effort:{ticket.get('effort')}"],
                config=spec if isinstance(spec, dict) else None,
                outcome=str(ticket.get("detail") or "") or None,
                account=ACTOR,
                idempotency_key=uuid.uuid5(_NAMESPACE, key),
            ),
            api_key_id=None,
            actor=ACTOR,
        )
        graph.labels[key] = created
        report.created += 1
        return
    row = graph.experiments.get(graph.labels[key])
    if row is not None and await _set(store, row, "status", status, reason=f"coord ticket {state}"):
        report.updated += 1


async def _import_theory(
    store: Store,
    graph: _Graph,
    effort: str,
    theory: Mapping[str, object],
    report: ImportReport,
) -> None:
    key = f"{COORD}{effort}/{theory['id']}"
    fields: dict[str, object] = {
        "explains": theory.get("quantity"),
        "form": theory.get("form"),
        "variables": theory.get("variables"),
        "parameters": theory.get("parameters"),
        "regime": theory.get("regime"),
        "fit_ref": theory.get("fit_ref"),
    }
    if key not in graph.labels:
        created = await store.submit(
            SubmitTheory(
                title=f"{effort}/{theory['id']}",
                labels=["coord", key, f"effort:{effort}"],
                account=ACTOR,
                idempotency_key=uuid.uuid5(_NAMESPACE, key),
                **cast(dict[str, object], fields),  # pyright: ignore[reportArgumentType]
            ),
            api_key_id=None,
            actor=ACTOR,
        )
        graph.labels[key] = created
        graph.theories[created] = cast(Theory, await store.get_inquiry(created))
        report.created += 1
        return
    row = graph.theories.get(graph.labels[key])
    if row is None:
        return
    changed = False
    for name, value in fields.items():
        changed |= await _set(store, row, name, value, reason="coord knowledge file changed")
    if row.status == "invalid":
        changed |= await _set(store, row, "status", "active", reason="back in coord's files")
    report.updated += int(changed)


async def _import_prediction(
    store: Store, graph: _Graph, row: Mapping[str, object], report: ImportReport
) -> None:
    pid = str(row.get("id") or "")
    if not _PREDICTION.match(pid):
        return
    key = f"{COORD}{pid}"
    effort = str(row.get("effort") or pid.split("-")[0])
    try:
        spec = json.loads(str(row.get("spec") or "{}"))
    except json.JSONDecodeError:
        report.errors.append(f"{pid}: unreadable spec")
        return
    quantity = spec.get("quantity") or {}
    quantiles = _quantiles(spec.get("quantiles"))
    registered = _ts(row.get("registered"))
    if not isinstance(quantity, dict) or quantiles is None or registered is None:
        report.errors.append(f"{pid}: no quantity, quantiles or registration time")
        return
    theory = graph.labels.get(f"{COORD}{effort}/{spec['theory']}") if spec.get("theory") else None
    if key not in graph.labels:
        made_by = str(row.get("source") or spec.get("source") or "agent")
        try:
            body = SubmitPrediction(
                title=f"{pid}: {quantity.get('key') or quantity.get('metric')}",
                description=str(spec.get("note") or "") or None,
                labels=["coord", key, f"effort:{effort}"],
                quantity=quantity,
                quantiles=quantiles,
                made_by=made_by if made_by in ("theory", "agent", "house-prior") else "agent",  # pyright: ignore[reportArgumentType]
                override_of=str(spec["override_of"]) if spec.get("override_of") else None,
                theory=theory,
                account=ACTOR,
                idempotency_key=uuid.uuid5(_NAMESPACE, key),
            )
        except ValueError as err:
            report.errors.append(f"{pid}: {err}")
            return
        created = await store.submit_prediction(
            body, api_key_id=None, actor=ACTOR, registered=registered
        )
        graph.labels[key] = created
        if theory:
            graph.edges.add((created, theory, "predicts"))
        graph.predictions[created] = cast(Prediction, await store.get_inquiry(created))
        report.created += 1
    prediction = graph.predictions.get(graph.labels[key])
    if prediction is None:
        return
    if theory:
        await _link(store, graph, prediction.id, theory, "predicts")
    ticket = graph.labels.get(f"{COORD}{row.get('ticket')}") if row.get("ticket") else None
    if ticket:
        await _link(store, graph, ticket, prediction.id, "tests")
    status = str(row.get("status") or "open")
    if status == "open":
        return
    resolution: dict[str, object] = {
        "status": status,
        "scorer": "coord",
        "resolved": (_ts(row.get("resolved")) or registered).isoformat(),
        "detail": row.get("detail"),
    }
    if status == "resolved":
        resolution.update({k: row.get(k) for k in ("observed", *_SCORE_KEYS)})
        observed = _num(row.get("observed"))
        if observed is not None:
            ours = score(quantiles, observed, better=str(quantity.get("better", "lower")))
            disagree = {
                k: [row.get(k), ours[k]]
                for k in _SCORE_KEYS
                if _num(row.get(k)) is None
                or abs(float(cast(float, row.get(k))) - float(ours[k])) > _SHADOW_TOLERANCE
            }
            if disagree:
                resolution["shadow_disagrees"] = disagree
                report.shadow_disagreements.append({"prediction": pid, "seq": prediction.seq, "fields": disagree})
    if await _set(store, prediction, "resolution", resolution, reason=f"coord: {status}"):
        report.updated += 1


async def _import_belief(
    store: Store,
    graph: _Graph,
    effort: str,
    belief: Mapping[str, object],
    report: ImportReport,
) -> None:
    key = f"{COORD}{effort}/{belief['id']}"
    claim = str(belief.get("claim") or belief["id"])
    stated = _num(belief.get("stated"))
    stated = None if stated is None or not 0 <= stated <= 1 else stated
    reviewed = _ts(belief.get("reviewed"))
    if key not in graph.labels:
        created = await store.submit(
            SubmitBelief(
                title=f"{effort}/{belief['id']}: {claim}"[:300],
                description=claim,
                confidence=stated,
                labels=["coord", key, f"effort:{effort}"],
                account=ACTOR,
                idempotency_key=uuid.uuid5(_NAMESPACE, key),
            ),
            api_key_id=None,
            actor=ACTOR,
        )
        graph.labels[key] = created
        graph.beliefs[created] = cast(Belief, await store.get_inquiry(created))
        report.created += 1
    row = graph.beliefs.get(graph.labels[key])
    if row is None:
        return
    changed = await _set(store, row, "description", claim, reason="coord knowledge file changed")
    changed |= await _set(store, row, "confidence", stated, reason="coord: stated confidence")
    changed |= await _set(store, row, "reviewed_at", reviewed, reason="coord: reviewed")
    if row.status == "invalid":
        changed |= await _set(store, row, "status", "active", reason="back in coord's files")
    report.updated += int(changed)


async def _belief_edges(
    store: Store, graph: _Graph, effort: str, belief: Mapping[str, object]
) -> None:
    """Sync a coord Belief's about/assumes/cites edges with its file."""
    me = graph.labels.get(f"{COORD}{effort}/{belief['id']}")
    if me is None:
        return
    theory = (
        graph.labels.get(f"{COORD}{effort}/{belief['theory']}") if belief.get("theory") else None
    )
    for current in graph.out(me, "about"):
        if current != theory:
            await _unlink(store, graph, me, current, "about")
    if theory:
        await _link(store, graph, me, theory, "about")
    wanted = {
        found
        for a in cast(list[object], belief.get("assumes") or [])
        if (found := graph.labels.get(f"{COORD}{effort}/{a}"))
    }
    for current in graph.out(me, "assumes"):
        if current not in wanted:
            await _unlink(store, graph, me, current, "assumes")
    for target in wanted:
        await _link(store, graph, me, target, "assumes")
    cited = {
        found
        for c in cast(list[object], belief.get("cites") or [])
        if (found := graph.labels.get(f"{COORD}{c}"))
    }
    for citer in graph.into(me, "favors"):
        row = graph.experiments.get(citer) or graph.predictions.get(citer)
        if row is not None and "coord" in (row.labels or ()) and citer not in cited:
            await _unlink(store, graph, citer, me, "favors")
    for citer in cited:
        # Trackinizer's valence is never empty: coord's cites are unsigned, so
        # they take the default mild support and say where they came from.
        await _link(store, graph, citer, me, "favors", valence=0.5, labels=[COORD_CITE])


async def _import_status(
    store: Store, graph: _Graph, effort: str, status: Mapping[str, object]
) -> None:
    as_of = (_ts(status.get("ts")) or datetime.now(UTC)).isoformat()
    for tid, record in cast(Mapping[str, Mapping[str, object]], status.get("theories") or {}).items():
        row = graph.theories.get(graph.labels.get(f"{COORD}{effort}/{tid}") or uuid.UUID(int=0))
        if row is not None:
            wanted = {**record, "scorer": "coord", "as_of": as_of}
            if {k: v for k, v in (row.record or {}).items() if k != "as_of"} != {
                k: v for k, v in wanted.items() if k != "as_of"
            }:
                await store.set_record(row.id, wanted, actor=ACTOR)
    for bid, record in cast(Mapping[str, Mapping[str, object]], status.get("beliefs") or {}).items():
        row = graph.beliefs.get(graph.labels.get(f"{COORD}{effort}/{bid}") or uuid.UUID(int=0))
        if row is None:
            continue
        earned = _num(record.get("earned"))
        await _set(store, row, "earned_confidence", earned, reason="coord: earned against rivals")
        because = ", ".join(str(b) for b in cast(list[object], record.get("because") or []))
        await _set(
            store,
            row,
            "needs_review",
            bool(record.get("needs_review")),
            reason=f"coord: assumes {because}, changed since" if because else "coord",
        )


async def _retire_deleted(store: Store, graph: _Graph, snapshot: CoordSnapshot) -> None:
    """Mark invalid the coord Theories and Beliefs whose files are gone."""
    present = {
        f"{COORD}{effort}/{item['id']}"
        for source in (snapshot.theories, snapshot.beliefs)
        for effort, items in source.items()
        for item in items
    }
    efforts = set(snapshot.theories) | set(snapshot.beliefs)
    for rows in (graph.theories, graph.beliefs):
        for row in rows.values():
            key = next((l for l in row.labels or () if l.startswith(COORD) and "/" in l), None)
            if key is None or key.removeprefix(COORD).split("/")[0] not in efforts:
                continue
            if key not in present and row.status != "invalid":
                await store.set_status(row.id, "invalid", actor=ACTOR, reason="deleted in R2")


# ---------------------------------------------------------------- native scoring


async def migrate_preregs(store: Store, graph: _Graph) -> int:
    """Give every legacy ``prereg`` Experiment's prediction a Prediction row."""
    created = 0
    for experiment in graph.experiments.values():
        labels = {p.strip() for l in experiment.labels or () for p in l.split(",")}
        spec = (experiment.config or {}).get("prediction")
        key = f"{PREREG}{experiment.seq}"
        if "prereg" not in labels or not isinstance(spec, Mapping) or key in graph.labels:
            continue
        quantity = {str(k): v for k, v in spec.items() if k not in ("quantiles", "secondary", "sweep")}
        quantity.setdefault("key", quantity.get("metric"))
        metric = str(quantity.get("metric") or "")
        quantity.setdefault("better", "higher" if metric in ("skill_pt", "delta_pt") else "lower")
        if not quantity.get("key"):
            continue
        quantiles = _quantiles(spec.get("quantiles"))
        labels_out = ["prereg-import", key]
        if quantiles is None:
            labels_out.append(LEGACY_INTERVAL_LABEL)
        try:
            body = SubmitPrediction(
                title=f"prereg #{experiment.seq}: {experiment.title}"[:300],
                labels=labels_out,
                quantity=quantity,
                quantiles=quantiles,
                made_by="agent",
                account=ACTOR,
                idempotency_key=uuid.uuid5(_NAMESPACE, key),
            )
        except ValueError:
            continue
        # Registered when the agent registered it: the prereg Experiment's birth.
        pid = await store.submit_prediction(
            body, api_key_id=None, actor=ACTOR, registered=experiment.created
        )
        graph.labels[key] = pid
        graph.predictions[pid] = cast(Prediction, await store.get_inquiry(pid))
        await _link(store, graph, experiment.id, pid, "tests")
        created += 1
    return created


def _quantity_id(prediction: Prediction) -> str:
    q = prediction.quantity or {}
    return json.dumps(
        {k: q.get(k) for k in ("ticket", "r2_key", "run_id", "pair", "key", "metric", "at", "parent")},
        sort_keys=True,
        default=str,
    )


async def resolve_native(
    store: Store, graph: _Graph, ledger: _Ledger, *, now: datetime
) -> int:
    """Score every open, non-coord Prediction whose value is in the ledger."""
    changed = 0
    for prediction in graph.predictions.values():
        labels = prediction.labels or ()
        if "coord" in labels or (prediction.resolution or {}).get("status") not in (None, "open"):
            continue
        quantity = prediction.quantity or {}
        registered = prediction.registered or prediction.created
        late_by = [
            e
            for e in graph.into(prediction.id, "tests")
            if (exp := graph.experiments.get(e)) is not None
            and exp.created < registered
        ]
        found = ledger.observe(quantity)
        if found is None and not late_by:
            continue
        resolution: dict[str, object] = {"scorer": "trackinizer", "resolved": now.isoformat()}
        if late_by:
            resolution.update(status="late", detail="tested by an Experiment older than the Prediction")
        elif found is not None and found[1] < registered:
            resolution.update(status="late", observed=found[0], detail="the ledger had the value before it was registered")
        elif found is not None:
            observed, _ = found
            resolution.update(status="resolved", observed=observed)
            if prediction.quantiles:
                resolution.update(
                    score(prediction.quantiles, observed, better=str(quantity.get("better", "lower")))
                )
            else:
                lo, hi = _num(quantity.get("lo")), _num(quantity.get("hi"))
                resolution["legacy"] = True
                if lo is not None and hi is not None:
                    resolution["verdict"] = "hit" if lo <= observed <= hi else "miss"
        if await _set(store, prediction, "resolution", resolution, reason=str(resolution["status"])):
            changed += 1
    return changed


def house_prior_points(
    graph: _Graph, ledger: _Ledger, *, metric: str
) -> list[tuple[datetime, float, uuid.UUID]]:
    """Every realised value of ``metric`` among Predictions, with when it landed."""
    points: list[tuple[datetime, float, uuid.UUID]] = []
    for prediction in graph.predictions.values():
        quantity = prediction.quantity or {}
        if str(quantity.get("metric") or quantity.get("key")) != metric:
            continue
        found = ledger.observe(quantity)
        if found is not None:
            points.append((found[1], found[0], prediction.id))
    return sorted(points)


def house_prior_backtest(
    graph: _Graph,
    points: Sequence[tuple[datetime, float, uuid.UUID]],
    *,
    min_n: int = 20,
) -> list[dict[str, object]]:
    """Score each outcome against a prior fitted only on outcomes before it was registered."""
    scored: list[dict[str, object]] = []
    for _when, value, pid in points:
        prediction = graph.predictions[pid]
        registered = prediction.registered or prediction.created
        past = [v for when, v, other in points if when < registered and other != pid]
        if len(past) < min_n:
            continue
        prior = empirical_quantiles(past)
        if prior["q95"] == prior["q05"]:
            continue
        better = str((prediction.quantity or {}).get("better", "higher"))
        row: dict[str, object] = {
            **score(prior, value, better=better),
            "registered": registered,
            "source": "house-prior (backtest)",
            "prediction": prediction.seq,
            "n_prior": len(past),
        }
        scored.append(row)
    return scored


async def fit_house_prior(
    store: Store,
    graph: _Graph,
    points: Sequence[tuple[datetime, float, uuid.UUID]],
    *,
    metric: str,
    now: datetime,
) -> dict[str, object] | None:
    """Keep the house-prior Theory at the empirical quantiles of every realised value."""
    if len(points) < 5:
        return None
    quantiles = empirical_quantiles([v for _, v, _ in points])
    prior = {
        "quantity_kind": metric,
        "quantiles": {k: round(v, 4) for k, v in quantiles.items()},
        "n": len(points),
        "source": "trackinizer house prior: realised outcomes of resolved predictions on Windrunner",
        "fitted_at": now.isoformat(),
    }
    parameters = {k: {"mean": round(v, 4)} for k, v in quantiles.items()}
    key = f"{HOUSE_PRIOR}:{metric}"
    theory_id = graph.labels.get(key)
    if theory_id is None:
        theory_id = await store.submit(
            SubmitTheory(
                title=f"House prior: realised {metric}",
                description=(
                    "The empirical distribution of every realised outcome of this "
                    "quantity. An exploratory prediction is shrunk toward it unless a "
                    "Theory says otherwise."
                ),
                labels=[HOUSE_PRIOR, key],
                explains=metric,
                form=f"{metric} ~ empirical quantiles of realised outcomes",
                parameters=parameters,
                regime="every resolved Windrunner prediction of this quantity",
                account=ACTOR,
                idempotency_key=uuid.uuid5(_NAMESPACE, key),
            ),
            api_key_id=None,
            actor=ACTOR,
        )
        graph.labels[key] = theory_id
        graph.theories[theory_id] = cast(Theory, await store.get_inquiry(theory_id))
    theory = graph.theories[theory_id]
    if theory.parameters != parameters:
        await store.set_parameters(theory_id, parameters, actor=ACTOR, reason=f"refit on n={len(points)}")
        record = {**(theory.record or {}), "n": len(points), "fitted_at": now.isoformat(), "scorer": "trackinizer"}
        await store.set_record(theory_id, record, actor=ACTOR)
    return prior


async def derive_native_records(store: Store, graph: _Graph, *, now: datetime) -> None:
    """Earned weights for Windrunner's own Theories and the Beliefs about them."""
    native = {t.id: t for t in graph.theories.values() if "coord" not in (t.labels or ())}
    by_theory: dict[str, dict[str, float]] = {}
    for tid in native:
        for pid in graph.into(tid, "predicts"):
            p = graph.predictions.get(pid)
            res = (p.resolution or {}) if p else {}
            if p is not None and res.get("status") == "resolved" and _num(res.get("logscore")) is not None:
                by_theory.setdefault(str(tid), {})[_quantity_id(p)] = float(cast(float, res["logscore"]))
    earned: dict[uuid.UUID, float | None] = {}
    for tid, theory in native.items():
        rivals = [*graph.out(tid, "rivals"), *graph.into(tid, "rivals")]
        group = [str(tid), *(str(r) for r in rivals if r in native)]
        weights, shared = earned_weights(by_theory, group)
        earned[tid] = weights.get(str(tid))
        if HOUSE_PRIOR in (theory.labels or ()):
            continue
        scored = by_theory.get(str(tid), {})
        record = {
            "n_predictions": len(graph.into(tid, "predicts")),
            "n_resolved": len(scored),
            "logscore": round(sum(scored.values()), 3),
            "shared_with_rivals": shared,
            "earned": earned[tid],
            "no_rivals": len(group) == 1,
            "scorer": "trackinizer",
        }
        old = {k: v for k, v in (theory.record or {}).items() if k != "as_of"}
        if old != record:
            await store.set_record(tid, {**record, "as_of": now.isoformat()}, actor=ACTOR)
    for belief in graph.beliefs.values():
        if "coord" in (belief.labels or ()):
            continue
        about = [t for t in graph.out(belief.id, "about") if t in native]
        value = earned.get(about[0]) if about else None
        await _set(store, belief, "earned_confidence", value, reason="earned against rivals")


async def propagate_review(store: Store, graph: _Graph) -> list[dict[str, object]]:
    """Flag every Belief whose assumption changed after it was last updated or reviewed.

    A Belief is current as of its last content edit (title, description,
    judgement, confidence or status) or review: ``reviewed_at``, or a person
    clearing ``needs_review``. Only Windrunner's own Beliefs; coord flags its own.
    """
    native = [b for b in graph.beliefs.values() if "coord" not in (b.labels or ())]
    ids = [b.id for b in native]
    if not ids:
        return []
    async with store.engine.acquire() as conn:
        edits = {
            r["subject_id"]: r["at"]
            for r in await conn.fetch(
                "SELECT subject_id, max(created) AS at FROM change_log "
                "WHERE subject_id = ANY($1) AND kind = ANY($2) GROUP BY subject_id",
                ids,
                list(_CONTENT_KINDS),
            )
        }
        cleared = {
            r["subject_id"]: r["at"]
            for r in await conn.fetch(
                "SELECT subject_id, max(created) AS at FROM change_log "
                "WHERE subject_id = ANY($1) AND kind = 'belief_needs_review' "
                "AND new_belief_needs_review = false AND actor <> $2 GROUP BY subject_id",
                ids,
                ACTOR,
            )
        }
    updated = {b.id: edits.get(b.id, b.created) for b in native}
    flagged: list[dict[str, object]] = []
    for belief in native:
        current = max(
            t for t in (updated[belief.id], belief.reviewed_at, cleared.get(belief.id)) if t is not None
        )
        because = [
            graph.beliefs[a].seq
            for a in graph.out(belief.id, "assumes")
            if a in updated and updated[a] > current
        ]
        if because:
            flagged.append({"belief": belief.seq, "because": because})
        await _set(
            store,
            belief,
            "needs_review",
            bool(because),
            reason=(
                "assumes " + ", ".join(f"#{s}" for s in because) + ", changed since"
                if because
                else "reviewed since its assumptions last changed"
            ),
        )
    return flagged


# ---------------------------------------------------------------- service


@dataclass(slots=True)
class KnowledgeService:
    """One pass every ``interval_sec``: pull coord, import, score, derive."""

    coord_host: str
    cache_dir: Path
    ledger_paths: tuple[str, ...]
    house_prior_metric: str
    house_prior_path: Path
    interval_sec: float = 300.0
    name: str = "knowledge"
    description: str = "Imports coord, scores predictions, fits the house prior."
    report: ImportReport = field(default_factory=ImportReport)
    house_prior: dict[str, object] | None = None
    backtest: list[dict[str, object]] = field(default_factory=list)

    async def run(self, context: ServerContext) -> None:
        """Pass every ``interval_sec`` until cancelled."""
        while True:
            try:
                await self.sweep(context.store)
            except Exception:
                _LOGGER.exception("knowledge pass failed; retrying next interval")
            await asyncio.sleep(self.interval_sec)

    async def sweep(self, store: Store, *, snapshot: CoordSnapshot | None = None) -> None:
        """One pass. ``snapshot`` replaces the rsync pull, for tests."""
        now = datetime.now(UTC)
        report = ImportReport(at=now)
        if snapshot is None and self.coord_host:
            try:
                await asyncio.to_thread(pull_mirror, self.coord_host, self.cache_dir)
                report.pulled = now
                snapshot = await asyncio.to_thread(CoordSnapshot.read, self.cache_dir)
            except (OSError, subprocess.SubprocessError) as err:
                report.errors.append(f"coord pull failed: {err}"[:300])
                if self.cache_dir.exists():
                    snapshot = await asyncio.to_thread(CoordSnapshot.read, self.cache_dir)
        if snapshot is not None:
            await import_coord(store, snapshot, report)
        ledger = await asyncio.to_thread(_Ledger.read, self.ledger_paths)
        graph = await _Graph.read(store)
        report.created += await migrate_preregs(store, graph)
        report.updated += await resolve_native(store, graph, ledger, now=now)
        graph = await _Graph.read(store)
        points = house_prior_points(graph, ledger, metric=self.house_prior_metric)
        self.backtest = house_prior_backtest(graph, points)
        prior = await fit_house_prior(store, graph, points, metric=self.house_prior_metric, now=now)
        if prior is not None:
            self.house_prior = prior
            await asyncio.to_thread(_write_json, self.house_prior_path, prior)
        graph = await _Graph.read(store)
        await derive_native_records(store, graph, now=now)
        await propagate_review(store, graph)
        self.report = report


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1, default=str))
    tmp.replace(path)


# ---------------------------------------------------------------- summary


async def summarise(
    store: Store,
    service: KnowledgeService | None = None,
    *,
    now: datetime | None = None,
    uncited_days: float = 14.0,
) -> dict[str, object]:
    """Everything the Theories, calibration and stale-beliefs views show."""
    now = now or datetime.now(UTC)
    graph = await _Graph.read(store)

    def ref(row: Inquiry | None) -> dict[str, object] | None:
        return None if row is None else {"id": str(row.id), "seq": row.seq, "title": row.title}

    predictions: list[dict[str, object]] = []
    for p in sorted(graph.predictions.values(), key=lambda r: r.seq):
        theory = next((graph.theories.get(t) for t in graph.out(p.id, "predicts")), None)
        quantity = p.quantity or {}
        predictions.append(
            {
                "id": str(p.id),
                "seq": p.seq,
                "title": p.title,
                "key": _key(p.labels, COORD) or _key(p.labels, PREREG),
                "origin": "coord" if "coord" in (p.labels or ()) else "windrunner",
                "effort": _key(p.labels, "effort:"),
                "theory": ref(theory),
                "made_by": p.made_by,
                "author": quantity.get("author") or p.account,
                "quantity": quantity,
                "quantiles": p.quantiles,
                "registered": (p.registered or p.created).isoformat(),
                "legacy": LEGACY_INTERVAL_LABEL in (p.labels or ()),
                "resolution": p.resolution,
                "tested_by": [ref(graph.experiments.get(e)) for e in graph.into(p.id, "tests")],
            }
        )
    scored = [
        {
            **{k: (p["resolution"] or {}).get(k) for k in _SCORE_KEYS},  # pyright: ignore[reportAttributeAccessIssue]
            "registered": datetime.fromisoformat(str(p["registered"])),
            "source": p["made_by"],
            "author": p["author"],
            "origin": p["effort"] or p["origin"],
        }
        for p in predictions
        if not p["legacy"] and (p["resolution"] or {}).get("status") == "resolved"  # pyright: ignore[reportAttributeAccessIssue]
    ]
    theories: list[dict[str, object]] = []
    for t in sorted(graph.theories.values(), key=lambda r: r.seq):
        rivals = [*graph.out(t.id, "rivals"), *graph.into(t.id, "rivals")]
        mine = [x for x in predictions if (x["theory"] or {}).get("id") == str(t.id)]  # pyright: ignore[reportAttributeAccessIssue]
        theories.append(
            {
                "id": str(t.id),
                "seq": t.seq,
                "title": t.title,
                "status": t.status,
                "key": _key(t.labels, COORD),
                "origin": "coord" if "coord" in (t.labels or ()) else "windrunner",
                "house_prior": HOUSE_PRIOR in (t.labels or ()),
                "explains": t.explains,
                "form": t.form,
                "variables": t.variables,
                "parameters": t.parameters,
                "regime": t.regime,
                "fit_ref": t.fit_ref,
                "record": t.record,
                "rivals": [ref(graph.theories.get(r)) for r in rivals],
                "fitted_by": [
                    {**(ref(e) or {}), "fit_point": (e.config or {}).get("fit_point")}
                    for x in graph.into(t.id, "fits")
                    if (e := graph.experiments.get(x)) is not None
                ],
                "discussed_by": [ref(graph.beliefs.get(b)) for b in graph.into(t.id, "about")],
                "predictions": [x["seq"] for x in mine],
                "last_refit": t.modified.isoformat(),
            }
        )
    async with store.engine.acquire() as conn:
        last_cited = {
            r["subject_id"]: r["at"]
            for r in await conn.fetch(
                "SELECT subject_id, max(created) AS at FROM change_log WHERE kind = 'edge_added' "
                "AND subject_kind = 'Belief' AND new_peer_edge_kind = ANY($1) GROUP BY subject_id",
                ["proves", "favors"],
            )
        }
    stale: list[dict[str, object]] = []
    for b in sorted(graph.beliefs.values(), key=lambda r: r.seq):
        if b.status in ("invalid", "abandoned"):
            continue
        cited = last_cited.get(b.id)
        reasons: list[str] = []
        if b.needs_review:
            assumes = [graph.beliefs[a].seq for a in graph.out(b.id, "assumes") if a in graph.beliefs]
            reasons.append("needs review: assumes " + ", ".join(f"#{s}" for s in assumes))
        gap = (
            round(b.confidence - b.earned_confidence, 3)
            if b.confidence is not None and b.earned_confidence is not None
            else None
        )
        if gap is not None and abs(gap) >= 0.2:
            reasons.append(f"stated {b.confidence:.2f} vs earned {b.earned_confidence:.2f}")
        if cited is None or now - cited > timedelta(days=uncited_days):
            reasons.append(
                "never cited" if cited is None else f"no new citation in {(now - cited).days} days"
            )
        if reasons:
            stale.append(
                {
                    **(ref(b) or {}),
                    "stated": b.confidence,
                    "earned": b.earned_confidence,
                    "gap": gap,
                    "needs_review": bool(b.needs_review),
                    "reviewed_at": b.reviewed_at.isoformat() if b.reviewed_at else None,
                    "review_note": b.review_note,
                    "last_cited": cited.isoformat() if cited else None,
                    "origin": "coord" if "coord" in (b.labels or ()) else "windrunner",
                    "reasons": reasons,
                }
            )
    report = service.report if service else ImportReport()
    backtest = service.backtest if service else []
    no_rivals = [
        t["seq"]
        for t in theories
        if not t["rivals"] and not t["house_prior"] and t["status"] != "invalid"
    ]
    return {
        "as_of": now.isoformat(),
        "theories": theories,
        "predictions": predictions,
        "calibration": calibration(scored, groups=("source", "author", "origin")),
        "house_prior": {
            "prior": service.house_prior if service else None,
            "backtest": calibration(backtest, groups=("source",))["all"],
        },
        "beliefs": stale,
        "flags": {
            "theories_without_rivals": no_rivals,
            "needs_review": [b["seq"] for b in stale if b["needs_review"]],
            "coord": report.coord_flags,
        },
        "import": {
            "at": report.at.isoformat() if report.at else None,
            "pulled": report.pulled.isoformat() if report.pulled else None,
            "created": report.created,
            "updated": report.updated,
            "errors": report.errors,
            "shadow_disagreements": report.shadow_disagreements,
        },
    }


class Knowledge:
    """Import coord's predictions and knowledge files; score; serve the summary."""

    class Config(Fig["Knowledge"]):
        coord_host: str = "coord-mirror"
        """ssh alias of coord's read-only rsync mirror; empty turns the import off."""

        cache_dir: str = "~/.cache/trackinizer/coord"
        """Where the mirror is pulled to."""

        ledger_paths: tuple[str, ...] = (
            "~/Desktop/Windrunner/wr-data/forecast/ledger.jsonl",
            "~/Desktop/Windrunner/wr-data/forecast/market_ledger.jsonl",
        )
        """The scorer ledgers Windrunner's own predictions resolve from."""

        house_prior_metric: str = "delta_pt"
        """The quantity whose realised outcomes form the house prior."""

        house_prior_path: str = "~/.cache/trackinizer/house-prior.json"
        """Where the current prior is written, in the ``rs-shared/notes/house-prior.json`` shape."""

        interval_sec: float = 300.0
        """Seconds between passes."""

        uncited_days: float = 14.0
        """A Belief with no new citation for this long is listed as stale."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def manifest(self) -> AddonManifest:
        """Contribute the pass as a server service and the summary route."""
        config = self.config
        service = KnowledgeService(
            coord_host=config.coord_host,
            cache_dir=Path(config.cache_dir).expanduser(),
            ledger_paths=config.ledger_paths,
            house_prior_metric=config.house_prior_metric,
            house_prior_path=Path(config.house_prior_path).expanduser(),
            interval_sec=config.interval_sec,
        )
        router = APIRouter()

        async def summary_route(request: Request) -> object:
            found = await summarise(
                get_store(request), service, uncited_days=config.uncited_days
            )
            return json.loads(json.dumps(found, default=str))

        async def house_prior_route() -> object:
            return service.house_prior or {}

        router.add_api_route("/summary", summary_route, methods=["GET"])
        router.add_api_route("/house-prior", house_prior_route, methods=["GET"])
        return AddonManifest(
            title="Theories and calibration",
            description=(
                "Imports coord's predictions and knowledge files, scores Windrunner's "
                "own predictions, fits the house prior, and flags Beliefs to review."
            ),
            server_services=[service],
            routers=[router],
        )
