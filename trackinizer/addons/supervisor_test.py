"""Tests for the failed-turn supervisor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import uuid

import pytest

from trackinizer.addons.supervisor import (
    FAILED_LABEL,
    Supervisor,
    SupervisorService,
    retry_after,
)
from trackinizer.lib.agent.types.sessions import SessionRecord, UncategorizedRecord
from trackinizer.server.inbound import InboundQueue
from trackinizer.server.store.core import Store
from trackinizer.server.store.session_ir import SessionManifest
from trackinizer.types.session_records import SessionRecordRow


_SESSION = uuid.UUID("00000000-0000-4000-8000-0000000000aa")
_T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _at(minutes: float) -> str:
    return (_T0 + timedelta(minutes=minutes)).isoformat()


def _started(minutes: float) -> SessionRecord:
    return UncategorizedRecord(
        timestamp=_at(minutes),
        kind="event_msg/task_started",
        payload={"turn_id": "t"},
    )


def _complete(minutes: float, *, error: str | None = None, code: str = "other") -> SessionRecord:
    payload: dict[str, object] = {"turn_id": "t", "last_agent_message": "done"}
    if error is not None:
        payload = {
            "turn_id": "t",
            "last_agent_message": None,
            "error": {"message": error, "codex_error_info": code},
        }
    return UncategorizedRecord(
        timestamp=_at(minutes),
        kind="event_msg/task_complete",
        payload=payload,  # pyright: ignore[reportArgumentType]
    )


@dataclass
class _Store:
    records: list[SessionRecord] = field(default_factory=list)
    labels: list[tuple[str, bool]] = field(default_factory=list)

    async def read_session_manifests(self, session_id: uuid.UUID) -> list[SessionManifest]:
        del session_id
        return [
            SessionManifest(
                part=0,
                name="rollout.jsonl",
                metadata={},
                ir_id=uuid.uuid4(),
                format="codex",
                records=len(self.records),
            ),
        ]

    async def read_session_records(
        self,
        session_id: uuid.UUID,
        *,
        part: int,
        after_idx: int = -1,
        limit: int = 500,
        plaintext_only: bool = False,
    ) -> list[SessionRecordRow]:
        del plaintext_only
        return [
            SessionRecordRow.of(session_id=session_id, part=part, idx=idx, record=record)
            for idx, record in enumerate(self.records)
            if idx > after_idx
        ][:limit]

    async def add_label(self, target_id: uuid.UUID, label: str, **_: object) -> None:
        del target_id
        self.labels.append((label, True))

    async def remove_label(self, target_id: uuid.UUID, label: str, **_: object) -> None:
        del target_id
        self.labels.append((label, False))


def _setup(records: list[SessionRecord]) -> tuple[SupervisorService, _Store, InboundQueue]:
    store = _Store(records=records)
    inbound = InboundQueue(poller_ttl_sec=1e9)
    inbound.mark_poller(_SESSION)
    return SupervisorService(Supervisor.Config()), store, inbound


async def _sweep(service: SupervisorService, store: _Store, inbound: InboundQueue, minutes: float) -> None:
    await service.sweep(cast(Store, store), inbound, now=_T0 + timedelta(minutes=minutes))


@pytest.mark.asyncio
async def test_transient_failure_is_labelled_then_resumed_after_backoff() -> None:
    service, store, inbound = _setup(
        [_started(0), _complete(10, error="stream disconnected before completion")],
    )
    await _sweep(service, store, inbound, 10.5)
    assert store.labels == [(FAILED_LABEL, True)]
    assert inbound.pending(_SESSION) == 0  # the first backoff is 60 s
    await _sweep(service, store, inbound, 11.5)
    queued = inbound.drain(_SESSION)
    assert len(queued) == 1
    assert "stream disconnected" in queued[0].text
    await _sweep(service, store, inbound, 30)
    assert inbound.pending(_SESSION) == 0  # one resume per failure


@pytest.mark.asyncio
async def test_success_clears_the_label_and_the_streak() -> None:
    service, store, inbound = _setup(
        [_started(0), _complete(10, error="stream disconnected")],
    )
    await _sweep(service, store, inbound, 12)
    store.records += [_started(13), _complete(20)]
    await _sweep(service, store, inbound, 21)
    assert store.labels[-1] == (FAILED_LABEL, False)
    assert service.watches[_SESSION].resumes == 0


@pytest.mark.asyncio
async def test_a_streak_stops_after_its_backoffs_are_spent() -> None:
    service, store, inbound = _setup([_started(0), _complete(1, error="network error")])
    minute = 1.0
    for _ in range(5):
        minute += 20
        await _sweep(service, store, inbound, minute)
        inbound.drain(_SESSION)
        store.records += [_started(minute), _complete(minute + 0.5, error="network error")]
        minute += 0.5
        await _sweep(service, store, inbound, minute)
    assert service.watches[_SESSION].resumes == len(Supervisor.Config().backoff_sec)


@pytest.mark.asyncio
async def test_a_usage_limit_resumes_after_it_lifts() -> None:
    service, store, inbound = _setup(
        [
            _started(0),
            _complete(
                1,
                error="You've hit your usage limit. Try again at Oct 9th, 2026 4:30 PM.",
                code="usage_limit_exceeded",
            ),
        ],
    )
    # 4:30 PM at UTC+2 is 14:30 UTC, and the resume waits two minutes past it.
    await _sweep(service, store, inbound, 150)
    assert inbound.pending(_SESSION) == 0
    await _sweep(service, store, inbound, 153)
    assert inbound.pending(_SESSION) == 1


@pytest.mark.asyncio
async def test_a_permanent_error_is_labelled_but_never_resumed() -> None:
    service, store, inbound = _setup(
        [_started(0), _complete(1, error="context window exceeded", code="context_window_exceeded")],
    )
    await _sweep(service, store, inbound, 2)
    await _sweep(service, store, inbound, 200)
    assert store.labels == [(FAILED_LABEL, True)]
    assert inbound.pending(_SESSION) == 0


@pytest.mark.asyncio
async def test_an_old_failure_is_not_resumed() -> None:
    service, store, inbound = _setup([_started(0), _complete(1, error="network error")])
    await _sweep(service, store, inbound, 60 * 24)
    await _sweep(service, store, inbound, 60 * 24 + 30)
    assert inbound.pending(_SESSION) == 0


def test_retry_after_reads_codex_usage_message() -> None:
    message = (
        "You've hit your usage limit. Visit https://chatgpt.com/settings/usage to "
        "purchase more credits or try again at Oct 14th, 2026 12:39 PM."
    )
    assert retry_after(message, tz=timedelta(hours=2)) == datetime(
        2026, 10, 14, 10, 39, tzinfo=UTC,
    )
    assert retry_after("no time here") is None
