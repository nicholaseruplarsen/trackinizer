"""Tests for waiting on an agent's turn."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import uuid

from treekle import PlainTree

from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    SessionRecord,
    UncategorizedRecord,
    UserMessage,
)
from trackinizer.lib.agent.types.stored import to_stored
from trackinizer.trax.wait import mark, wait_for_turn
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_session_ir import PartBody, RecordBody


_SESSION = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _started() -> SessionRecord:
    return UncategorizedRecord(kind="event_msg/task_started", payload={"turn_id": "t"})


def _complete(reply: str | None = "RESULT ok", error: str | None = None) -> SessionRecord:
    payload: dict[str, PlainTree] = {"turn_id": "t", "last_agent_message": reply}
    if error is not None:
        payload["error"] = {"message": error, "codex_error_info": "other"}
    return UncategorizedRecord(kind="event_msg/task_complete", payload=payload)


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _Client:
    """A session whose records grow on a schedule keyed by the clock."""

    clock: _Clock
    records: list[SessionRecord] = field(default_factory=list)
    arrivals: list[tuple[float, SessionRecord]] = field(default_factory=list)
    ended_at: float | None = None
    owner: str = "forecaster"

    def _arrive(self) -> None:
        while self.arrivals and self.arrivals[0][0] <= self.clock.now:
            self.records.append(self.arrivals.pop(0)[1])

    def list_kind(
        self,
        kind: str,
        *,
        limit: int = 200,
        filters: Sequence[Filter] = (),
    ) -> list[dict[str, PlainTree]]:
        assert kind == "AgentSession"
        del limit
        if any(f.value != self.owner for f in filters):
            return []
        ended = self.ended_at is not None and self.clock.now >= self.ended_at
        return [
            {
                "id": str(_SESSION),
                "created": "2026-10-14T00:00:00Z",
                "ended": "2026-10-14T01:00:00Z" if ended else None,
            },
        ]

    def read_session_parts(self, session_id: uuid.UUID) -> list[PartBody]:
        self._arrive()
        assert session_id == _SESSION
        return [PartBody(part=0, name="rollout.jsonl", format="codex", records=len(self.records))]

    def read_session_records(
        self,
        session_id: uuid.UUID,
        *,
        part: int = 0,
        after_idx: int = -1,
        plaintext_only: bool = False,
    ) -> list[RecordBody]:
        del session_id, part, plaintext_only
        self._arrive()
        return [
            RecordBody(idx=idx, kind=type(record).__name__, payload=to_stored(record))
            for idx, record in enumerate(self.records)
            if idx > after_idx
        ]


def test_reply_after_a_send_is_returned() -> None:
    clock = _Clock()
    client = _Client(clock, records=[_started(), _complete("old answer")])
    before = mark(client, "forecaster")
    client.arrivals = [
        (5.0, UserMessage(content="claude: do H31")),
        (6.0, _started()),
        (90.0, _complete("RESULT 01ab")),
    ]
    result = wait_for_turn(client, "forecaster", since=before, clock=clock, sleep=clock.sleep)
    assert result.outcome == "reply"
    assert result.text == "RESULT 01ab"
    assert result.exit_code == 0
    assert result.consumed


def test_an_old_reply_does_not_satisfy_a_new_send() -> None:
    clock = _Clock()
    client = _Client(clock, records=[_started(), _complete("old answer")])
    before = mark(client, "forecaster")
    client.arrivals = [(5.0, UserMessage(content="go")), (6.0, _started())]
    result = wait_for_turn(
        client,
        "forecaster",
        since=before,
        timeout_sec=60.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert result.outcome == "timeout"
    assert result.consumed


def test_a_cut_stream_returns_a_transient_error() -> None:
    clock = _Clock()
    client = _Client(clock)
    before = mark(client, "forecaster")
    client.arrivals = [
        (3.0, _started()),
        (40.0, _complete(None, error="stream disconnected before completion")),
    ]
    result = wait_for_turn(client, "forecaster", since=before, clock=clock, sleep=clock.sleep)
    assert result.outcome == "error"
    assert result.transient
    assert result.exit_code == 3


def test_nothing_recorded_after_a_send_is_unconsumed() -> None:
    clock = _Clock()
    client = _Client(clock, records=[_started(), _complete()])
    before = mark(client, "forecaster")
    result = wait_for_turn(
        client,
        "forecaster",
        since=before,
        consume_grace_sec=120.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert result.outcome == "unconsumed"
    assert 120.0 <= result.waited_sec < 125.0


def test_a_session_that_ends_mid_turn_is_reported() -> None:
    clock = _Clock()
    client = _Client(clock, ended_at=30.0)
    before = mark(client, "forecaster")
    client.arrivals = [(3.0, _started())]
    result = wait_for_turn(client, "forecaster", since=before, clock=clock, sleep=clock.sleep)
    assert result.outcome == "ended"


def test_without_a_mark_a_finished_turn_returns_at_once() -> None:
    clock = _Clock()
    client = _Client(
        clock,
        records=[UserMessage(content="go"), _started(), _complete("done")],
    )
    result = wait_for_turn(client, "forecaster", clock=clock, sleep=clock.sleep)
    assert result.outcome == "reply"
    assert result.waited_sec == 0.0


def test_unknown_name_is_no_session() -> None:
    clock = _Clock()
    client = _Client(clock, owner="someone-else")
    result = wait_for_turn(client, "forecaster", clock=clock, sleep=clock.sleep)
    assert result.outcome == "no_session"
    assert result.exit_code == 2


def test_claude_reply_counts_too() -> None:
    clock = _Clock()
    client = _Client(clock)
    before = mark(client, "forecaster")
    client.arrivals = [
        (2.0, UserMessage(content="go")),
        (
            20.0,
            AssistantMessage(content="done", extra={"message": {"stop_reason": "end_turn"}}),
        ),
    ]
    result = wait_for_turn(client, "forecaster", since=before, clock=clock, sleep=clock.sleep)
    assert result.outcome == "reply"
    assert result.text == "done"
