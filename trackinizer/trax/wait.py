"""Block until an agent's turn ends, and say how it ended.

``trax send`` returns once a message is queued for a live session. That is
not the same as the agent having read it, and not the same as the agent
having answered: a codex turn can die on a cut stream and leave the session
idle at its prompt while the sender waits for a reply that is never coming.
:func:`wait_for_turn` closes that gap from the reader's side. It watches the
records the session uploads and returns on the first of:

- ``reply``: the turn ended with an answer;
- ``error``: the turn ended on an error (``transient`` says whether retrying
  could help);
- ``aborted``: someone interrupted the turn;
- ``ended``: the session itself ended or was closed for silence;
- ``unconsumed``: nothing at all arrived from the session within the grace
  period after a send, so the message never reached the model;
- ``timeout``: the turn is still running at the deadline;
- ``no_session``: no session answers to the name.

It needs nothing from the server beyond the record and inquiry reads every
``trax`` already uses, so it works against a server that predates it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import time
import uuid

from treekle import PlainTree, from_plain

from trackinizer.lib.agent.turns import TurnState, latest_turn
from trackinizer.wire.filters import Filter
from trackinizer.wire.wire_session_ir import PartBody, RecordBody


__all__ = [
    "EXIT_CODES",
    "Mark",
    "Outcome",
    "WaitResult",
    "mark",
    "wait_for_turn",
]


type Outcome = Literal[
    "reply",
    "error",
    "aborted",
    "ended",
    "unconsumed",
    "timeout",
    "no_session",
]


EXIT_CODES: dict[Outcome, int] = {
    "reply": 0,
    "no_session": 2,
    "error": 3,
    "aborted": 4,
    "ended": 5,
    "unconsumed": 6,
    "timeout": 124,
}
"""Process exit status per outcome; ``timeout`` matches ``timeout(1)``."""


# How many trailing records to read when no mark bounds the read. A turn's
# closing bracket is the newest record, so a short tail always holds it.
_TAIL = 400


class WaitClient(Protocol):
    """The reads :func:`wait_for_turn` needs; :class:`Client` has them all."""

    def list_kind(
        self,
        kind: str,
        *,
        limit: int = ...,
        filters: Sequence[Filter] = ...,
    ) -> list[dict[str, PlainTree]]:
        """List rows of one kind."""
        ...

    def read_session_parts(self, session_id: uuid.UUID) -> list[PartBody]:
        """List a session's captured files."""
        ...

    def read_session_records(
        self,
        session_id: uuid.UUID,
        *,
        part: int = 0,
        after_idx: int = -1,
        plaintext_only: bool = False,
    ) -> list[RecordBody]:
        """Read one part's records after an index."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Mark:
    """Where a session's records stood at one moment: its newest part and size."""

    session_id: uuid.UUID
    part: int
    records: int


@dataclass(frozen=True, slots=True, kw_only=True)
class WaitResult:
    """How a wait ended.

    Attributes:
      outcome: Which condition ended it.
      session_id: The session watched, when one was found.
      text: The reply, or the error message.
      code: The provider's error class, when the turn failed with one.
      transient: Whether a failed turn could succeed if retried.
      consumed: Whether anything arrived from the session after the mark.
      waited_sec: How long the wait took.

    """

    outcome: Outcome
    session_id: uuid.UUID | None = None
    text: str | None = None
    code: str | None = None
    transient: bool = False
    consumed: bool = False
    waited_sec: float = 0.0

    @property
    def exit_code(self) -> int:
        """The process exit status this outcome maps to."""
        return EXIT_CODES[self.outcome]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Session:
    """The newest session that answers to a name."""

    id: uuid.UUID
    ended: bool


def find_session(client: WaitClient, actor: str) -> _Session | None:
    """Return the newest session owned by ``actor``, live or not.

    The newest rather than the newest LIVE one: a session that died is
    exactly what a waiter must hear about, not skip past.

    Args:
      client: A trax client.
      actor: The routing name (``--as``) without its ``@``.

    Returns:
      session: Its id and whether it has ended, or None if the name has none.

    """
    rows = client.list_kind(
        "AgentSession",
        limit=50,
        filters=(Filter(field="owner", op="is", value=actor),),
    )
    if not rows:
        return None
    newest = max(rows, key=lambda row: str(row.get("created") or ""))
    return _Session(
        id=uuid.UUID(from_plain(newest.get("id"), str)),
        ended=newest.get("ended") is not None,
    )


def mark(client: WaitClient, actor: str) -> Mark | None:
    """Record where ``actor``'s session stands now, before a send.

    Args:
      client: A trax client.
      actor: The routing name.

    Returns:
      mark: The session's newest part and record count, or None if it has none.

    """
    session = find_session(client, actor)
    if session is None:
        return None
    part, records = _newest_part(client, session.id)
    return Mark(session_id=session.id, part=part, records=records)


def wait_for_turn(
    client: WaitClient,
    actor: str,
    *,
    since: Mark | None = None,
    timeout_sec: float = 3600.0,
    poll_sec: float = 3.0,
    consume_grace_sec: float = 180.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitResult:
    """Wait for the turn ``actor`` is on to end, and report how.

    With ``since`` (a :func:`mark` taken before a send), only a turn that ends
    after the mark counts, so a reply to an earlier message is never mistaken
    for this one; and if nothing at all arrives within ``consume_grace_sec``
    the message is reported ``unconsumed``. Without it, a turn that has
    already ended returns at once and a running one is waited on.

    Args:
      client: A trax client.
      actor: The routing name, without its ``@``.
      since: Where the session stood before the send, if this follows one.
      timeout_sec: How long to wait for the turn to end.
      poll_sec: How often to read the session's new records.
      consume_grace_sec: How long a sent message may go without the session
        recording anything before it counts as never delivered.
      clock: Monotonic seconds.
      sleep: Pauses between polls.

    Returns:
      result: The outcome, with the reply or error when the turn ended.

    """
    start = clock()
    while True:
        waited = clock() - start
        session = find_session(client, actor)
        if session is None:
            return WaitResult(outcome="no_session", waited_sec=waited)
        if since is not None and session.id != since.session_id:
            # The name now belongs to a newer session: the one sent to is gone.
            return WaitResult(
                outcome="ended",
                session_id=since.session_id,
                waited_sec=waited,
            )
        state, consumed = _read_state(client, session.id, since)
        if state is not None and state.ended:
            return WaitResult(
                outcome=state.phase,  # pyright: ignore[reportArgumentType]
                session_id=session.id,
                text=state.text,
                code=state.code,
                transient=state.transient,
                consumed=consumed,
                waited_sec=waited,
            )
        if session.ended:
            return WaitResult(
                outcome="ended",
                session_id=session.id,
                consumed=consumed,
                waited_sec=waited,
            )
        if since is not None and not consumed and waited >= consume_grace_sec:
            return WaitResult(
                outcome="unconsumed",
                session_id=session.id,
                waited_sec=waited,
            )
        if waited >= timeout_sec:
            return WaitResult(
                outcome="timeout",
                session_id=session.id,
                consumed=consumed,
                waited_sec=waited,
            )
        sleep(min(poll_sec, timeout_sec - waited))


def _newest_part(client: WaitClient, session_id: uuid.UUID) -> tuple[int, int]:
    """The session's newest part and how many records it holds."""
    parts = client.read_session_parts(session_id)
    if not parts:
        return 0, 0
    newest = max(parts, key=lambda part: part.part)
    return newest.part, newest.records


def _read_state(
    client: WaitClient,
    session_id: uuid.UUID,
    since: Mark | None,
) -> tuple[TurnState | None, bool]:
    """Read the session's new records and the state of its latest turn.

    Returns:
      state: The latest turn's state from the records after the mark (all
        recent records without one); None when there are none yet.
      consumed: Whether any record arrived after the mark.

    """
    part, count = _newest_part(client, session_id)
    if since is not None and part == since.part:
        after = since.records - 1
    elif since is not None and part < since.part:
        return None, False
    else:
        # A newer part (a codex fork, a claude compaction) or no mark: read
        # the tail, since the turn's end is its newest record.
        after = max(count - _TAIL, 0) - 1 if since is None else -1
    rows = client.read_session_records(
        session_id,
        part=part,
        after_idx=after,
        plaintext_only=True,
    )
    if not rows:
        return None, False
    records = [row.row(session_id, part).record() for row in rows]
    state = latest_turn(records)
    consumed = since is not None
    if state.phase == "idle":
        return None, consumed
    return state, consumed or since is None
