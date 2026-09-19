"""What one OpenRouter turn becomes: content spans, reasoning replay, budgets.

The defect these guard against was found by rendering the real transcript
widgets against a real turn on 2026-09-03. ``_stream_round`` appended a new
``Block`` for every SSE delta, so a turn's answer arrived as one ordered
content span *per event*: measured 200 spans for 200 deltas where the Claude
path produces 1. Each span is rendered as its own surface, so a markdown table
split across two deltas could not render as a table, and the model's reasoning
drew 31 separate collapsed "Reasoning summary" rows in a single turn.

The second half is the wire half. OpenRouter documents that a turn continuing
through tool results has to carry the model's own ``reasoning_details`` back
unchanged; Helios parsed the plain ``reasoning`` string for display and threw
the structured blocks away.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from helios.backend.openrouter import chat as or_chat
from helios.backend.openrouter.history import build_assistant_message

pytest.importorskip("gi")

from helios.backend.process.openrouter_driver import _grow_block  # noqa: E402
from helios.backend.process.streaming import (  # noqa: E402
    Block,
    StreamingAssistant,
    format_turn_footer,
)


def _sse(*chunks: dict):
    """Frame dicts as the SSE byte stream ``_consume_stream`` reads."""
    import json

    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    yield body.encode("utf-8")


def _consume(*chunks: dict):
    return list(
        or_chat._consume_stream(
            _sse(*chunks),
            token=or_chat.CancellationToken(),
            deadline=float("inf"),
            monotonic=lambda: 0.0,
        )
    )


class TestDeltaAccumulation:
    def test_two_hundred_deltas_make_one_span(self):
        streaming = StreamingAssistant()
        for index in range(200):
            _grow_block(streaming, "text", f"tok{index} ")
        assert len(streaming.blocks) == 1
        assert len(streaming.to_turn().content) == 1

    def test_a_markdown_table_split_across_deltas_survives(self):
        """The failure mode in one line: no individual fragment is a table."""
        streaming = StreamingAssistant()
        for fragment in ("| File", " | Size |\n", "|---|-", "--|\n", "| a.py", " | 1 |\n"):
            _grow_block(streaming, "text", fragment)
        spans = streaming.to_turn().content
        assert len(spans) == 1
        assert spans[0].text == "| File | Size |\n|---|---|\n| a.py | 1 |\n"

    def test_a_kind_change_opens_a_new_block(self):
        streaming = StreamingAssistant()
        _grow_block(streaming, "reasoning_summary", "thinking ")
        _grow_block(streaming, "reasoning_summary", "more ")
        _grow_block(streaming, "text", "answer")
        assert [b.type for b in streaming.blocks] == ["reasoning_summary", "text"]
        assert streaming.blocks[0].text == "thinking more "

    def test_a_tool_call_between_rounds_separates_the_text(self):
        """Round two's answer must not be glued onto round one's."""
        streaming = StreamingAssistant()
        _grow_block(streaming, "text", "first")
        streaming.blocks.append(Block(type="tool_use", tool_use_name="Read"))
        _grow_block(streaming, "text", "second")
        assert [b.type for b in streaming.blocks] == ["text", "tool_use", "text"]
        assert [b.text for b in streaming.blocks if b.type == "text"] == [
            "first",
            "second",
        ]


class TestReasoningReplay:
    def _chunk(self, detail: dict, *, finish: str | None = None) -> dict:
        choice: dict = {"index": 0, "delta": {"reasoning_details": [detail]}}
        if finish:
            choice["finish_reason"] = finish
        return {"id": "gen-1", "choices": [choice]}

    def test_fragments_are_reassembled_in_order(self):
        events = _consume(
            self._chunk({"type": "reasoning.text", "text": "The ", "format": "unknown", "index": 0}),
            self._chunk({"type": "reasoning.text", "text": "plan", "index": 0}),
            self._chunk({"type": "reasoning.summary", "summary": "**Doing it**", "index": 1}),
            self._chunk({"type": "reasoning.text", "text": "!", "index": 0}, finish="stop"),
        )
        done = events[-1]
        assert isinstance(done, or_chat.Done)
        assert done.completion.reasoning_details == (
            {"type": "reasoning.text", "text": "The plan!", "format": "unknown",
             "index": 0},
            {"type": "reasoning.summary", "summary": "**Doing it**", "index": 1},
        )

    def test_metadata_from_the_first_fragment_is_not_overwritten(self):
        events = _consume(
            self._chunk({"type": "reasoning.text", "text": "a", "format": "anthropic-claude-v1",
                         "signature": "sig", "index": 0}),
            self._chunk({"type": "reasoning.text", "text": "b", "format": "", "index": 0},
                        finish="stop"),
        )
        detail = events[-1].completion.reasoning_details[0]
        assert detail["format"] == "anthropic-claude-v1"
        assert detail["signature"] == "sig"
        assert detail["text"] == "ab"

    def test_an_unindexed_detail_still_round_trips(self):
        events = _consume(
            self._chunk({"type": "reasoning.text", "text": "solo"}, finish="stop"),
        )
        assert events[-1].completion.reasoning_details == (
            {"type": "reasoning.text", "text": "solo"},
        )

    def test_two_unindexed_details_are_not_merged_into_one(self):
        """A review finding: two blocks that cannot be shown to belong together
        must not be joined."""
        events = _consume(
            self._chunk({"type": "reasoning.text", "text": "first"}),
            self._chunk({"type": "reasoning.summary", "summary": "second"},
                        finish="stop"),
        )
        assert events[-1].completion.reasoning_details == (
            {"type": "reasoning.text", "text": "first"},
            {"type": "reasoning.summary", "summary": "second"},
        )

    def test_every_field_survives_including_ones_this_build_never_saw(self):
        """Measured against the live API 2026-09-03: a non-streamed response
        carries `index` on every block and `id`/`data` on OpenAI's encrypted
        ones. A whitelist cannot be right about a field it does not know
        exists, and the block has to go back matching what the model made."""
        events = _consume(
            self._chunk({"type": "reasoning.encrypted", "data": "gAAAA",
                         "format": "openai-responses-v1", "id": "rs_1",
                         "index": 1, "future_field": {"nested": True}}),
            self._chunk({"type": "reasoning.encrypted", "data": "BBBB", "index": 1},
                        finish="stop"),
        )
        assert events[-1].completion.reasoning_details == (
            {"type": "reasoning.encrypted", "data": "gAAAABBBB",
             "format": "openai-responses-v1", "id": "rs_1", "index": 1,
             "future_field": {"nested": True}},
        )

    def test_an_absurd_index_is_dropped_rather_than_buffered(self):
        events = _consume(
            self._chunk({"type": "reasoning.text", "text": "x", "index": 9_999}, finish="stop"),
        )
        assert events[-1].completion.reasoning_details == ()

    def test_a_turn_with_no_reasoning_sends_no_key(self):
        message, _run = build_assistant_message("hi", [], "stop", ())
        assert "reasoning_details" not in message

    def test_the_replayed_assistant_message_carries_the_blocks(self):
        call = SimpleNamespace(id="c1", name="Read", arguments_json="{}")
        details = ({"type": "reasoning.text", "text": "why"},)
        message, run = build_assistant_message("", [call], "tool_calls", details)
        assert run is True
        assert message["reasoning_details"] == [{"type": "reasoning.text", "text": "why"}]
        assert [c["function"]["name"] for c in message["tool_calls"]] == ["Read"]


class TestTurnFooterScope:
    def test_the_footer_describes_the_turn_not_the_last_round(self):
        """A three-round turn reported one round's tokens beside three rounds'
        cumulative cost, on the same line."""
        result = {
            "usage": {"input_tokens": 1_496, "output_tokens": 50,
                      "cache_read_input_tokens": 1_280,
                      "cache_creation_input_tokens": 0},
            "turn_usage": {"input_tokens": 2_600, "output_tokens": 195,
                           "cache_read_input_tokens": 1_280,
                           "cache_creation_input_tokens": 0},
            "total_cost_usd": 0.000296,
        }
        assert format_turn_footer(result) == "3,880 in · 195 out · $0.0003"

    def test_a_provider_without_turn_usage_is_unchanged(self):
        result = {
            "usage": {"input_tokens": 10, "output_tokens": 2,
                      "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0},
            "total_cost_usd": 0.5,
        }
        assert format_turn_footer(result) == "10 in · 2 out · $0.5000"


class TestReasoningEffortIsOffered:
    """The driver mapped Helios effort keys onto OpenRouter's unified
    `reasoning` parameter from the day the provider landed, with tests and an
    endpoint check — and the toolbar disabled the control with the comment
    "not wired in v1". Finished backend, switch off."""

    def _row(self, **over):
        row = {
            "id": "vendor/model",
            "name": "Vendor: Model",
            "context_length": 128_000,
            "supported_parameters": ["tools", "tool_choice", "reasoning"],
        }
        row.update(over)
        return row

    def test_a_reasoning_model_offers_the_mapped_levels(self):
        from helios.backend.openrouter import catalog as or_catalog

        entry = or_catalog.build_entries([self._row()])[0]
        assert [key for key, _label in entry.reasoning_efforts] == [
            "off", "low", "medium", "high",
        ]
        assert entry.default_effort == "medium"

    def test_every_offered_level_maps_to_a_wire_value(self):
        """Offering a level Helios cannot express would be a lie."""
        from helios.backend.openrouter import catalog as or_catalog

        entry = or_catalog.build_entries([self._row()])[0]
        for key, _label in entry.reasoning_efforts:
            assert or_chat.reasoning_for_effort(key) is not None

    def test_a_model_without_reasoning_offers_nothing(self):
        from helios.backend.openrouter import catalog as or_catalog

        entry = or_catalog.build_entries(
            [self._row(supported_parameters=["tools", "tool_choice"])]
        )[0]
        assert entry.reasoning_efforts == ()
        assert entry.default_effort == ""

    def test_the_portable_parameter_is_checked_not_the_openai_one(self):
        from helios.backend.openrouter import catalog as or_catalog

        assert or_catalog.supports_reasoning(
            self._row(supported_parameters=["reasoning_effort"])
        ) is False
        assert or_catalog.supports_reasoning(self._row()) is True

    def test_a_malformed_row_fails_closed(self):
        from helios.backend.openrouter import catalog as or_catalog

        assert or_catalog.supports_reasoning(None) is False
        assert or_catalog.supports_reasoning({"supported_parameters": "reasoning"}) is False

    def test_a_staged_effort_is_dropped_when_the_endpoint_refuses_it(
        self, tmp_path, monkeypatch
    ):
        """The catalog row is a model-level claim; the pinned endpoint decides.
        Sending `reasoning` to an endpoint that does not declare it makes
        `require_parameters: true` route the request away."""
        from helios.backend.openrouter.routes import Route
        from helios.backend.process import openrouter_driver as od

        monkeypatch.setattr(od.or_key, "load_key", lambda: "sk-or-v1-testkey0000000000")

        def _route(supports: bool):
            return Route(
                model="vendor/model", provider_slug="p", provider_name="P",
                context_length=128_000, max_completion_tokens=8_000,
                quantization="fp8", supports_tools=True,
                supports_reasoning=supports, implicit_caching=False,
                explicit_caching=False, input_price=0.0, cache_read_price=0.0,
            )

        monkeypatch.setattr(od.or_routes, "route_for", lambda _m: _route(False))
        drv = od.OpenRouterDriver(cwd=str(tmp_path), model="vendor/model", effort="high")
        drv.start()
        assert drv.effort_key == ""

        monkeypatch.setattr(od.or_routes, "route_for", lambda _m: _route(True))
        drv2 = od.OpenRouterDriver(cwd=str(tmp_path), model="vendor/model", effort="high")
        drv2.start()
        assert drv2.effort_key == "high"

    def test_an_unknown_effort_key_is_not_staged(self, tmp_path, monkeypatch):
        from helios.backend.process import openrouter_driver as od

        monkeypatch.setattr(od.or_key, "load_key", lambda: "sk-or-v1-testkey0000000000")
        monkeypatch.setattr(od.or_routes, "route_for", lambda _m: None)
        drv = od.OpenRouterDriver(
            cwd=str(tmp_path), model="vendor/model", effort="ludicrous"
        )
        assert drv.effort_key == ""


class TestTheNameRuleStaysOnOneRealLine:
    """A review finding: ``[^\\S\\n]`` stops an LF but still admits CR,
    vertical tab, form feed, NEL and the Unicode separators, so a lone-CR file
    could still let a name swallow the next line's first word."""

    @pytest.mark.parametrize(
        "sep",
        ["\r", "\x0b", "\x0c", "\x85", "\u2028", "\u00a0"],
        ids=["cr", "vtab", "formfeed", "nel", "line-sep", "nbsp"],
    )
    def test_only_space_and_tab_can_separate(self, sep):
        from helios.backend.sensitive_text import scrub_sensitive

        text = f"is_secret:{sep}next_word_here"
        assert scrub_sensitive(text)[0] == text

    @pytest.mark.parametrize("sep", ["", " ", "\t", "  ", " \t "], ids=["none", "sp", "tab", "sp2", "mixed"])
    def test_space_and_tab_still_separate(self, sep):
        from helios.backend.sensitive_text import scrub_sensitive

        cleaned, changed = scrub_sensitive(f"password{sep}={sep}hunter2correcthorse")
        assert changed is True, sep
        assert "hunter2correcthorse" not in cleaned
