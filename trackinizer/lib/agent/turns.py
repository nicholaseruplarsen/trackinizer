"""Where an agent's latest turn stands: still running, answered, or failed.

A caller that sends a message into a live session needs to know when the
agent is done with it, and a turn can end three ways: with a reply, with an
error the CLI printed and then went idle on, or by being interrupted. The
error case is the one that matters most. Codex ends a turn whose stream was
cut (``stream disconnected before completion``) or whose compaction failed by
writing ``task_complete`` with an ``error`` and then waiting at its prompt, so
a session can sit idle for hours while whoever sent the work waits for a reply
that is never coming.

The providers mark turn ends differently, so this module reads the IR records
both write and returns one :class:`TurnState`:

- codex brackets every turn: ``event_msg/task_started`` opens it and
  ``event_msg/task_complete`` (its ``error`` set on failure) or
  ``event_msg/turn_aborted`` closes it.
- claude marks no boundary. A turn has ended when an assistant message stops
  for a reason other than a tool call, or when the CLI writes its end-of-turn
  ``stop_hook_summary``. A failed API call is an assistant message flagged
  ``isApiErrorMessage``.

A failure is TRANSIENT when trying the same turn again could succeed: a cut
stream, a network error, an overloaded model. A usage limit or an interrupt
is not, because retrying at once changes nothing or overrides a human.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    SystemMessage,
    ToolCall,
    UncategorizedRecord,
    UserMessage,
)


__all__ = ["TurnState", "is_transient", "latest_turn"]


type Phase = Literal["idle", "running", "reply", "error", "aborted"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnState:
    """How the latest turn in a run of records ended, if it has.

    Attributes:
      phase: ``idle`` when no turn has started in the records read; ``running``
        while one has started and not ended; ``reply``, ``error`` or
        ``aborted`` once it ended.
      text: The reply, or the error message.
      code: The provider's error class (codex ``codex_error_info``), if any.
      transient: Whether retrying the turn could succeed; only an ``error``
        can be transient.
      index: Position, in the records read, of the record that decided the
        phase; ``-1`` for ``idle``.
      started: Position of the record that opened the turn; ``-1`` if none.

    """

    phase: Phase
    text: str | None = None
    code: str | None = None
    transient: bool = False
    index: int = -1
    started: int = -1

    @property
    def ended(self) -> bool:
        """Whether the turn has finished, one way or another."""
        return self.phase in ("reply", "error", "aborted")


# Substrings of an error message that mark a failure of the connection or the
# provider rather than of the request. Matched case-insensitively.
_TRANSIENT_MARKERS = (
    "stream disconnected",
    "network error",
    "transport error",
    "connection reset",
    "connection refused",
    "connection closed",
    "timed out",
    "timeout",
    "overloaded",
    "at capacity",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "error decoding response body",
    "529",
    "503",
    "502",
)

# Codex error classes that say outright whether a retry can help.
_TRANSIENT_CODES = frozenset(
    {"server_overloaded", "stream_error", "internal_server_error"}
)
_PERMANENT_CODES = frozenset(
    {"usage_limit_exceeded", "context_window_exceeded", "unauthorized", "bad_request"},
)

# Claude ends a turn on any stop reason but a tool call.
_CLAUDE_TURN_END = frozenset({"end_turn", "stop_sequence", "max_tokens", "refusal"})


def is_transient(message: str | None, code: str | None = None) -> bool:
    """Say whether a failed turn could succeed if tried again.

    Args:
      message: The error message the CLI recorded.
      code: The provider's error class, when it gave one.

    Returns:
      transient: True for a cut connection or an overloaded provider.

    """
    if code in _PERMANENT_CODES:
        return False
    if code in _TRANSIENT_CODES:
        return True
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def latest_turn(records: Sequence[object]) -> TurnState:
    """Return the state of the latest turn the records show.

    Reads backwards to the most recent turn boundary, so a long transcript
    costs only its tail.

    Args:
      records: A session's records, oldest first.

    Returns:
      state: The latest turn's phase, and its reply or error.

    """
    for index in range(len(records) - 1, -1, -1):
        state = _boundary(records, index)
        if state is not None:
            return state
    return TurnState(phase="idle")


def _boundary(records: Sequence[object], index: int) -> TurnState | None:
    """Return the turn state record ``index`` decides, or None if it decides none."""
    record = records[index]
    if isinstance(record, UncategorizedRecord):
        return _codex_boundary(record, index)
    if isinstance(record, AssistantMessage):
        return _claude_reply(record, index)
    if isinstance(record, SystemMessage) and record.subtype == "stop_hook_summary":
        # Claude's end-of-turn marker: the reply is the last assistant prose.
        return TurnState(
            phase="reply",
            text=_last_assistant_text(records, index),
            index=index,
        )
    if isinstance(record, UserMessage | ToolCall):
        # A prompt, or a tool call the model is waiting on: a turn is under way.
        return TurnState(phase="running", index=index, started=index)
    return None


def _codex_boundary(record: UncategorizedRecord, index: int) -> TurnState | None:
    """Return the turn state one codex bracket record decides."""
    payload = record.payload
    if record.kind == "event_msg/task_started":
        return TurnState(phase="running", index=index, started=index)
    if record.kind == "event_msg/turn_aborted":
        reason = payload.get("reason")
        return TurnState(
            phase="aborted",
            text=reason if isinstance(reason, str) else None,
            index=index,
        )
    if record.kind != "event_msg/task_complete":
        return None
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        code = error.get("codex_error_info")
        text = message if isinstance(message, str) else None
        code_text = code if isinstance(code, str) else None
        return TurnState(
            phase="error",
            text=text,
            code=code_text,
            transient=is_transient(text, code_text),
            index=index,
        )
    reply = payload.get("last_agent_message")
    return TurnState(
        phase="reply",
        text=reply if isinstance(reply, str) else None,
        index=index,
    )


def _claude_reply(record: AssistantMessage, index: int) -> TurnState | None:
    """Return the turn state one claude assistant message decides."""
    extra = record.extra
    if "isApiErrorMessage" in extra:
        return TurnState(
            phase="error",
            text=record.content,
            transient=is_transient(record.content),
            index=index,
        )
    message = extra.get("message")
    stop = message.get("stop_reason") if isinstance(message, Mapping) else None
    if stop in _CLAUDE_TURN_END:
        return TurnState(phase="reply", text=record.content, index=index)
    if stop == "tool_use":
        return TurnState(phase="running", index=index, started=index)
    # Codex assistant messages carry no stop reason; their turn ends at the
    # bracket, which the backward scan reaches first when it exists.
    return None


def _last_assistant_text(records: Sequence[object], before: int) -> str | None:
    """Return the newest assistant prose at or before ``before``."""
    for index in range(before, -1, -1):
        record = records[index]
        if isinstance(record, AssistantMessage) and record.content:
            return record.content
        if isinstance(record, UserMessage):
            return None
    return None
