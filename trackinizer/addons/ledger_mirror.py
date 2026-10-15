"""Mirror scorer ledgers into Experiments, inside the server, with no step to forget.

A research loop's scorer is the single writer of its ledger, and the graph
carries one Experiment per ledger row so Beliefs can cite results. Mirroring
used to be a script run after each scoring; whenever it was skipped or its
server was down, the graph silently fell behind the ledger (15 rows behind on
2026-10-15). This server service tails each configured ledger and creates the
missing Experiments itself, so the graph is a derived view of the ledger.

Each row becomes one Experiment:

- ``title`` the row's ``run_id``; ``description`` its hypothesis;
- labels ``ledger:<id>`` (the dedup key) and ``ledger-source:<name>``;
- ``outcome`` one line per scored pair and window; ``config`` the metrics;
- a CodeChange for the row's ``sha`` (reused when one exists);
- ``produced_by`` the Issue whose title starts with the run's hypothesis
  number (``H27_wind_unavail`` -> ``H27: ...``). A run with no such Issue
  goes under one stub Issue per ledger, ``UNFILED <name>: ...``, so it keeps
  a parent and the stub's children show what still needs filing.

Creation is idempotent twice over: rows already mirrored are skipped by label,
and each submit carries an idempotency key derived from the row id, so a
crash between the submit and its edge cannot create a duplicate. Nothing here
edits or deletes; a ledger row that changes after it was mirrored (which an
append-only ledger never does) is left as first mirrored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import asyncio
import json
import logging
import re
import uuid

from configgle import Fig

from trackinizer.addons.addon import AddonManifest, ServerContext
from trackinizer.addons.research import read_ledger
from trackinizer.types.inquiries import CodeChange, Experiment, Issue
from trackinizer.wire.bodies import SubmitCodeChange, SubmitExperiment, SubmitIssue


if TYPE_CHECKING:
    from trackinizer.server.store.core import Store


__all__ = ["LedgerMirror", "issue_prefix", "outcome_line"]


_LOGGER = logging.getLogger(__name__)

ACTOR = "trackinizer-ledger-mirror"
"""The audit author of every mirrored row."""

_NAMESPACE = uuid.UUID("5b0d7a3e-3c1f-4f5e-9a51-6d1d2b6c9e10")
"""Namespace for the idempotency key each mirrored row's submit carries."""

# A config larger than this keeps only its dev window, as the old mirror did.
_MAX_CONFIG_BYTES = 60_000

# Live-pipeline run families and the Issue each belongs to, checked in order.
_FAMILIES: tuple[tuple[str, str], ...] = (
    ("ORACLE_", "H26"),
    ("live_h21_candidate", "H21"),
    ("live_initial", "H18"),
    ("live_icon_eu", "H14a"),
    ("live_archives", "H14b"),
    ("live_", "H14"),
)


def issue_prefix(row: Mapping[str, object]) -> str | None:
    """Return the hypothesis number a ledger row belongs to, e.g. ``H27``.

    Args:
      row: One ledger row.

    Returns:
      prefix: ``H`` plus two digits and an optional letter, or None.

    """
    run_id = str(row.get("source_run_id") or row.get("run_id") or "")
    for start, issue in _FAMILIES:
        if run_id.startswith(start):
            return issue
    found = re.match(r"H\d{2}[a-z]?", run_id.removeprefix("PLACEBO_"))
    return found[0] if found else None


def outcome_line(metrics: Mapping[str, object]) -> str:
    """Summarize every scored pair of every window in one line.

    Args:
      metrics: The row's ``metrics``: ``{window: {"pairs": {pair: {...}}}}``.

    Returns:
      line: ``window pair: skill CI; MAE`` parts joined by ``; ``, plus the
        trade PnL where a market ledger states one.

    """
    parts: list[str] = []
    for window, scored in metrics.items():
        pairs = scored.get("pairs") if isinstance(scored, Mapping) else None
        if not isinstance(pairs, Mapping):
            continue
        for pair, value in pairs.items():
            if not isinstance(value, Mapping) or value.get("skill") is None:
                continue
            part = f"{window} {pair}: {_pct(value.get('skill'))}"
            ci = value.get("skill_ci90")
            if isinstance(ci, Sequence) and len(ci) == 2:
                part += f" CI90 [{_pct(ci[0])},{_pct(ci[1])}]"
            mae = value.get("mae")
            if isinstance(mae, int | float):
                part += f"; MAE {mae:.1f}"
            trade = value.get("trade")
            if isinstance(trade, Mapping) and isinstance(trade.get("pnl"), int | float):
                part += f"; PnL {trade['pnl']:.3f}"
            parts.append(part)
    return "; ".join(parts) or "unscored"


def _size(path: str) -> int:
    """Return a ledger's size in bytes, 0 when it does not exist yet."""
    found = Path(path).expanduser()
    return found.stat().st_size if found.exists() else 0


def _pct(value: object) -> str:
    """Write a fraction as signed percent."""
    return f"{float(value) * 100:+.1f}%" if isinstance(value, int | float) else "n/a"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Ledger:
    name: str
    path: str


class LedgerMirror:
    """Keep one Experiment per ledger row, created by the server itself."""

    class Config(Fig["LedgerMirror"]):
        ledgers: tuple[tuple[str, str], ...] = (
            ("forecast", "~/Desktop/Windrunner/wr-data/forecast/ledger.jsonl"),
            ("market", "~/Desktop/Windrunner/wr-data/forecast/market_ledger.jsonl"),
        )
        """``(name, path)`` per ledger; the name becomes a ``ledger-source:`` label."""

        interval_sec: float = 60.0
        """Seconds between sweeps."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def manifest(self) -> AddonManifest:
        """Contribute the mirror as one server service."""
        return AddonManifest(
            title="Ledger mirror",
            description="Creates one Experiment per scorer-ledger row, inside the server.",
            server_services=[
                MirrorService(
                    ledgers=[
                        _Ledger(name=name, path=path)
                        for name, path in self.config.ledgers
                    ],
                    interval_sec=self.config.interval_sec,
                ),
            ],
        )


@dataclass(slots=True)
class MirrorService:
    """The mirror sweep, as a long-running task inside the server."""

    ledgers: list[_Ledger]
    interval_sec: float = 60.0
    name: str = "sweep"
    description: str = "Creates one Experiment per new ledger row."
    _sizes: dict[str, int] = field(default_factory=dict)

    async def run(self, context: ServerContext) -> None:
        """Sweep every ``interval_sec`` until cancelled."""
        while True:
            try:
                created = await self.sweep(context.store)
                if created:
                    _LOGGER.info("ledger mirror created %d experiments", created)
            except Exception:
                _LOGGER.exception("ledger mirror sweep failed; retrying next interval")
            await asyncio.sleep(self.interval_sec)

    async def sweep(self, store: Store, *, force: bool = False) -> int:
        """Mirror every ledger row no Experiment mirrors yet.

        Args:
          store: The server's store.
          force: Read the ledgers even if none changed size since last sweep.

        Returns:
          created: How many Experiments were created.

        """
        loaded: list[tuple[str, list[dict[str, object]]]] = []
        for ledger in self.ledgers:
            size = await asyncio.to_thread(_size, ledger.path)
            if not force and self._sizes.get(ledger.name) == size:
                continue
            loaded.append(
                (ledger.name, await asyncio.to_thread(read_ledger, ledger.path))
            )
            self._sizes[ledger.name] = size
        if not loaded:
            return 0
        experiments = [
            item
            for item in await store.list_kind("Experiment", limit=100_000)
            if isinstance(item, Experiment)
        ]
        mirrored = {
            label
            for item in experiments
            for label in item.labels or ()
            if label.startswith("ledger:")
        }
        issues = {
            item.title.split(":", 1)[0].strip(): item.id
            for item in await store.list_kind("Issue", limit=100_000)
            if isinstance(item, Issue)
        }
        changes = {
            item.sha: item.id
            for item in await store.list_kind("CodeChange", limit=100_000)
            if isinstance(item, CodeChange) and item.sha
        }
        created = 0
        for name, rows in loaded:
            for row in rows:
                key = f"ledger:{row.get('id')}"
                if not row.get("id") or key in mirrored:
                    continue
                await self._mirror(
                    store, row, name=name, issues=issues, changes=changes
                )
                mirrored.add(key)
                created += 1
        return created

    async def _mirror(
        self,
        store: Store,
        row: Mapping[str, object],
        *,
        name: str,
        issues: dict[str, uuid.UUID],
        changes: dict[str, uuid.UUID],
    ) -> None:
        """Create one row's Experiment, its CodeChange and its Issue edge."""
        row_id = str(row["id"])
        metrics = row.get("metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        config: dict[str, object] = {str(k): v for k, v in metrics.items()}
        if len(json.dumps(config, default=str).encode()) > _MAX_CONFIG_BYTES:
            config = {"dev": metrics.get("dev", {}), "trimmed": True}
        codechanges: list[uuid.UUID] = []
        sha = row.get("sha")
        if isinstance(sha, str) and sha:
            if sha not in changes:
                changes[sha] = await store.submit(
                    SubmitCodeChange(
                        title=sha,
                        sha=sha,
                        account=ACTOR,
                        idempotency_key=uuid.uuid5(_NAMESPACE, f"sha:{sha}"),
                    ),
                    api_key_id=None,
                    actor=ACTOR,
                )
            codechanges.append(changes[sha])
        description = str(row.get("hypothesis") or "")
        if row.get("reference"):
            description += f" Reference: {row['reference']}"
        experiment_id = await store.submit(
            SubmitExperiment(
                title=str(row.get("run_id") or row_id),
                description=description or None,
                status="complete",
                labels=[f"ledger:{row_id}", f"ledger-source:{name}"],
                outcome=outcome_line(metrics),
                config=config,
                codechanges=codechanges or None,
                account=ACTOR,
                idempotency_key=uuid.uuid5(_NAMESPACE, f"ledger:{row_id}"),
            ),
            api_key_id=None,
            actor=ACTOR,
        )
        prefix = issue_prefix(row)
        issue = issues.get(prefix or "") or issues.get((prefix or "")[:3])
        if issue is None:
            issue = await self._stub(store, name, issues)
        _ = await store.add_edge(
            from_id=experiment_id,
            to_id=issue,
            edge_kind="produced_by",
            actor=ACTOR,
        )

    async def _stub(
        self, store: Store, name: str, issues: dict[str, uuid.UUID]
    ) -> uuid.UUID:
        """Return the ledger's stub Issue for unfiled runs, creating it once."""
        key = f"UNFILED {name}"
        if key not in issues:
            issues[key] = await store.submit(
                SubmitIssue(
                    title=f"{key}: ledger runs with no hypothesis Issue",
                    description=(
                        "Created by the ledger mirror. Each child Experiment is a "
                        "scored run whose hypothesis has no Issue yet: file one and "
                        "move the produced_by edge."
                    ),
                    account=ACTOR,
                    idempotency_key=uuid.uuid5(_NAMESPACE, f"stub:{name}"),
                ),
                api_key_id=None,
                actor=ACTOR,
            )
        return issues[key]
