"""History compaction for OpenRouter sessions (v0.43.0).

The failure this guards against: OpenRouter re-sends the whole conversation on
every turn, nothing trimmed the array, so once a session filled its context
window every subsequent send raised CONTEXT_LENGTH — identically, forever.
"""

from __future__ import annotations

import json

from helios.backend.openrouter.history import compact, estimate_tokens


def _sized(role: str, chars: int, **extra) -> dict:
    return {"role": role, "content": "x" * chars, **extra}


def _exchange(chars: int) -> list[dict]:
    return [_sized("user", chars), _sized("assistant", chars)]


SYSTEM = {"role": "system", "content": "system prompt"}


class TestNoOp:
    def test_under_budget_returns_the_same_object(self):
        messages = [SYSTEM, *_exchange(10)]
        out, dropped = compact(messages, max_tokens=10_000)
        assert dropped == 0
        assert out is messages

    def test_zero_budget_is_treated_as_no_budget_known(self):
        messages = [SYSTEM, *_exchange(10_000)]
        out, dropped = compact(messages, max_tokens=0)
        assert dropped == 0
        assert out is messages

    def test_single_exchange_is_never_dropped(self):
        """One oversized turn is the model's error to report, not something
        trimming can fix — dropping it would leave no request at all."""
        messages = [SYSTEM, *_exchange(40_000)]
        out, dropped = compact(messages, max_tokens=100)
        assert dropped == 0
        assert out is messages


class TestTrimming:
    def test_drops_oldest_exchanges_and_keeps_the_newest(self):
        messages = [SYSTEM]
        for _ in range(6):
            messages += _exchange(4_000)
        out, dropped = compact(messages, max_tokens=4_000)

        assert dropped > 0
        assert out[0] == SYSTEM
        assert out[1]["role"] == "system"
        assert "trimmed" in out[1]["content"]
        # The newest exchange survives verbatim at the tail.
        assert out[-2:] == messages[-2:]
        assert estimate_tokens(out) <= 4_000

    def test_system_prefix_is_preserved_verbatim(self):
        extra = {"role": "system", "content": "second system message"}
        messages = [SYSTEM, extra]
        for _ in range(5):
            messages += _exchange(4_000)
        out, dropped = compact(messages, max_tokens=3_000)

        assert dropped > 0
        assert out[0] == SYSTEM
        assert out[1] == extra

    def test_marker_is_rewritten_not_accumulated(self):
        messages = [SYSTEM]
        for _ in range(8):
            messages += _exchange(4_000)
        once, first = compact(messages, max_tokens=8_000)
        once = once + _exchange(30_000)
        twice, second = compact(once, max_tokens=8_000)

        markers = [
            m for m in twice
            if m.get("role") == "system" and "trimmed" in str(m.get("content"))
        ]
        assert len(markers) == 1
        # The surviving marker counts both passes, not just the latest.
        assert str(first + second) in markers[0]["content"]

    def test_tool_call_replies_never_outlive_their_assistant_message(self):
        """An assistant message with tool_calls whose role:"tool" replies were
        trimmed away makes every later request 400. Exchanges are the trim
        unit precisely so this cannot happen."""
        messages = [SYSTEM]
        for index in range(6):
            call_id = f"call_{index}"
            messages += [
                _sized("user", 3_000),
                {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "Read", "arguments": "{}"},
                    }],
                },
                {"role": "tool", "tool_call_id": call_id, "content": "y" * 3_000},
            ]
        out, dropped = compact(messages, max_tokens=3_000)

        assert dropped > 0
        answered = {
            m["tool_call_id"] for m in out if m.get("role") == "tool"
        }
        requested = {
            call["id"]
            for m in out
            if m.get("role") == "assistant"
            for call in m.get("tool_calls", [])
        }
        assert requested == answered

    def test_result_is_json_serializable(self):
        messages = [SYSTEM]
        for _ in range(4):
            messages += _exchange(4_000)
        out, _ = compact(messages, max_tokens=2_000)
        json.dumps(out)  # must stay wire-valid


class TestEstimate:
    def test_scales_with_content(self):
        assert estimate_tokens([_sized("user", 4_000)]) > estimate_tokens(
            [_sized("user", 40)]
        )

    def test_survives_unserializable_content(self):
        assert estimate_tokens([{"role": "user", "content": object()}]) > 0
