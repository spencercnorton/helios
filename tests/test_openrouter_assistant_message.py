"""The assistant-message invariant for OpenRouter sessions (v0.43.0).

Before this, the driver appended an assistant message carrying ``tool_calls``
and *then* decided whether to run them, breaking out when ``finish_reason``
wasn't exactly ``"tool_calls"``. The tools never ran, no ``role:"tool"``
replies were appended, and the orphaned message was persisted — so every later
turn in that session 400'd, and it survived restart and resume.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from helios.backend.openrouter.history import build_assistant_message


def _call(index: int = 0, arguments: str = '{"path": "a.py"}'):
    return SimpleNamespace(
        id=f"call_{index}", name="Read", arguments_json=arguments
    )


class TestExecution:
    def test_tool_calls_run_on_the_canonical_finish_reason(self):
        message, execute = build_assistant_message("", [_call()], "tool_calls")
        assert execute is True
        assert message["tool_calls"][0]["id"] == "call_0"

    @pytest.mark.parametrize("reason", ["stop", "", "STOP", "end_turn", None])
    def test_tool_calls_run_despite_a_non_canonical_finish_reason(self, reason):
        """The regression. Providers do report a plain stop alongside real tool
        calls; the presence of the calls is what's authoritative."""
        message, execute = build_assistant_message("", [_call()], reason or "")
        assert execute is True
        assert "tool_calls" in message

    def test_no_tool_calls_ends_the_round(self):
        message, execute = build_assistant_message("done", [], "stop")
        assert execute is False
        assert message == {"role": "assistant", "content": "done"}


class TestTruncation:
    def test_a_length_truncated_turn_emits_no_unanswered_tool_calls(self):
        """Truncated arguments are unusable, so the calls are dropped rather
        than persisted with no replies."""
        message, execute = build_assistant_message(
            "partial", [_call(arguments='{"pa')], "length"
        )
        assert execute is False
        assert "tool_calls" not in message
        assert message["content"] == "partial"

    def test_a_length_truncated_turn_with_no_text_produces_no_message(self):
        message, execute = build_assistant_message("", [_call()], "length")
        assert execute is False
        assert message is None


class TestWireValidity:
    def test_every_emitted_tool_call_would_be_answered(self):
        calls = [_call(i) for i in range(3)]
        message, execute = build_assistant_message("", calls, "tool_calls")
        assert execute is True
        emitted = [c["id"] for c in message["tool_calls"]]
        assert emitted == ["call_0", "call_1", "call_2"]

    def test_an_empty_round_produces_no_message(self):
        """An assistant message with neither content nor tool_calls is not a
        valid wire message; it must not enter the history."""
        assert build_assistant_message("", [], "stop") == (None, False)

    def test_message_is_json_serializable(self):
        message, _ = build_assistant_message("hi", [_call()], "tool_calls")
        json.dumps(message)
