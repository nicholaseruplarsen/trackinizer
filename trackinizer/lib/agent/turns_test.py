"""Tests for reading where an agent's latest turn stands."""

from __future__ import annotations

from io import StringIO

import pytest

from trackinizer.lib.agent.sessions import codex
from trackinizer.lib.agent.turns import is_transient, latest_turn
from trackinizer.lib.agent.types.sessions import (
    AssistantMessage,
    SessionRecord,
    SystemMessage,
    ToolCall,
    UserMessage,
)


def _codex(*lines: str) -> list[SessionRecord]:
    """Normalize codex rollout lines the way capture does."""
    return list(codex.normalize(StringIO("\n".join(lines) + "\n")))


_STARTED = '{"timestamp":"2026-10-08T09:00:00Z","type":"event_msg","payload":{"type":"task_started","turn_id":"t1"}}'
_USER = (
    '{"timestamp":"2026-10-08T09:00:00Z","type":"response_item","payload":'
    '{"type":"message","role":"user","content":[{"type":"input_text","text":"do H27"}]}}'
)


def test_codex_reply_carries_last_agent_message() -> None:
    records = _codex(
        _STARTED,
        _USER,
        '{"timestamp":"2026-10-08T09:05:00Z","type":"event_msg","payload":'
        '{"type":"task_complete","turn_id":"t1","last_agent_message":"RESULT abc"}}',
    )
    state = latest_turn(records)
    assert state.phase == "reply"
    assert state.text == "RESULT abc"
    assert state.ended


def test_codex_stream_cut_is_a_transient_error() -> None:
    # The failure that left the forecaster idle for two days on 2026-10-09.
    records = _codex(
        _STARTED,
        _USER,
        '{"timestamp":"2026-10-08T09:43:35Z","type":"event_msg","payload":'
        '{"type":"task_complete","turn_id":"t1","last_agent_message":null,"error":'
        '{"message":"Error running remote compact task: stream disconnected before '
        'completion: Transport error: network error: error decoding response body",'
        '"codex_error_info":"other"}}}',
    )
    state = latest_turn(records)
    assert state.phase == "error"
    assert state.transient
    assert state.code == "other"
    assert state.text is not None
    assert "stream disconnected" in state.text


def test_codex_usage_limit_is_not_transient() -> None:
    records = _codex(
        _STARTED,
        '{"timestamp":"2026-10-10T08:38:52Z","type":"event_msg","payload":'
        '{"type":"task_complete","turn_id":"t1","last_agent_message":null,"error":'
        '{"message":"You have hit your usage limit.","codex_error_info":'
        '"usage_limit_exceeded"}}}',
    )
    state = latest_turn(records)
    assert state.phase == "error"
    assert not state.transient


def test_codex_interrupt_is_aborted() -> None:
    records = _codex(
        _STARTED,
        '{"timestamp":"2026-10-08T09:01:00Z","type":"event_msg","payload":'
        '{"type":"turn_aborted","turn_id":"t1","reason":"interrupted"}}',
    )
    state = latest_turn(records)
    assert state.phase == "aborted"
    assert state.text == "interrupted"
    assert not state.transient


def test_codex_open_turn_is_running() -> None:
    state = latest_turn(_codex(_STARTED, _USER))
    assert state.phase == "running"
    assert not state.ended


def test_a_new_prompt_after_a_reply_reopens_the_turn() -> None:
    records = _codex(
        _STARTED,
        '{"timestamp":"2026-10-08T09:05:00Z","type":"event_msg","payload":'
        '{"type":"task_complete","turn_id":"t1","last_agent_message":"done"}}',
        _USER,
    )
    assert latest_turn(records).phase == "running"


def test_no_records_is_idle() -> None:
    assert latest_turn([]).phase == "idle"


def _claude_assistant(text: str, stop: str) -> AssistantMessage:
    return AssistantMessage(content=text, extra={"message": {"stop_reason": stop}})


def test_claude_end_turn_is_a_reply() -> None:
    records: list[SessionRecord] = [
        UserMessage(content="go"),
        _claude_assistant("all done", "end_turn"),
    ]
    state = latest_turn(records)
    assert state.phase == "reply"
    assert state.text == "all done"


def test_claude_tool_call_after_an_old_reply_is_running() -> None:
    records: list[SessionRecord] = [
        _claude_assistant("earlier answer", "end_turn"),
        UserMessage(content="next"),
        _claude_assistant("let me look", "tool_use"),
        ToolCall(call_id="c1", name="Bash"),
    ]
    assert latest_turn(records).phase == "running"


def test_claude_api_error_is_an_error() -> None:
    records: list[SessionRecord] = [
        UserMessage(content="go"),
        AssistantMessage(
            content="API Error: 529 overloaded",
            extra={"isApiErrorMessage": True},
        ),
    ]
    state = latest_turn(records)
    assert state.phase == "error"
    assert state.transient


def test_claude_stop_hook_summary_ends_the_turn_with_the_last_prose() -> None:
    records: list[SessionRecord] = [
        UserMessage(content="go"),
        AssistantMessage(content="the answer", extra={}),
        SystemMessage(subtype="stop_hook_summary"),
    ]
    state = latest_turn(records)
    assert state.phase == "reply"
    assert state.text == "the answer"


@pytest.mark.parametrize(
    ("message", "code", "expected"),
    [
        ("stream disconnected before completion", "other", True),
        ("Selected model is at capacity.", "server_overloaded", True),
        ("You have hit your usage limit.", "usage_limit_exceeded", False),
        ("Your input exceeds the context window", "context_window_exceeded", False),
        ("something odd", None, False),
    ],
)
def test_is_transient(message: str, code: str | None, expected: bool) -> None:
    assert is_transient(message, code) is expected
