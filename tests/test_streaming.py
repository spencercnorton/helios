"""Tests for the GTK-free streaming aggregator.

These exercise the malformed / out-of-order event handling that previously
raised IndexError out of the driver's stdout callback (and hung the session
mid-reply). The module imports no gi, so it runs on the slim CI image.
"""

from __future__ import annotations

import pytest

from helios.backend.process.streaming import (
    Block,
    StreamingAssistant,
    stable_markdown_prefix,
)


def _text_start(index: int = 0) -> dict:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }


def _text_delta(text: str, index: int = 0) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


# --- happy path ---------------------------------------------------------


def test_basic_text_message():
    s = StreamingAssistant()
    s.apply_stream_event({"type": "message_start", "message": {"model": "opus"}})
    s.apply_stream_event(_text_start(0))
    s.apply_stream_event(_text_delta("Hello, ", 0))
    s.apply_stream_event(_text_delta("world", 0))
    s.apply_stream_event({"type": "message_stop"})

    assert s.model == "opus"
    assert s.stopped is True
    turn = s.to_turn()
    assert turn.text_parts == ["Hello, world"]


def test_multiple_blocks_thinking_then_tool():
    s = StreamingAssistant()
    s.apply_stream_event(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        }
    )
    s.apply_stream_event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "ponder"},
        }
    )
    s.apply_stream_event(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "name": "Bash", "id": "t1"},
        }
    )
    s.apply_stream_event(
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"cmd":"ls"}'},
        }
    )
    turn = s.to_turn()
    assert turn.thinking_parts == ["ponder"]
    assert len(turn.tool_uses) == 1
    assert turn.tool_uses[0].name == "Bash"
    assert turn.tool_uses[0].input == {"cmd": "ls"}


# --- regression: malformed / out-of-order events must NOT raise ---------


def test_delta_before_any_block_is_dropped():
    """A delta with no preceding content_block_start used to IndexError."""
    s = StreamingAssistant()
    # No content_block_start at all.
    s.apply_stream_event(_text_delta("orphan", 0))
    assert s.blocks == []  # dropped, no crash
    assert s.to_turn().text_parts == []


def test_delta_with_missing_index_before_block_is_dropped():
    s = StreamingAssistant()
    s.apply_stream_event(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}
    )
    assert s.blocks == []


def test_delta_with_negative_index_falls_back_to_last_block():
    s = StreamingAssistant()
    s.apply_stream_event(_text_start(0))
    s.apply_stream_event(
        {
            "type": "content_block_delta",
            "index": -1,
            "delta": {"type": "text_delta", "text": "tail"},
        }
    )
    assert s.to_turn().text_parts == ["tail"]


def test_delta_with_missing_index_targets_last_block():
    """Anthropic deltas target the most recent open block."""
    s = StreamingAssistant()
    s.apply_stream_event(_text_start(0))
    s.apply_stream_event(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "yo"}}
    )
    assert s.to_turn().text_parts == ["yo"]


def test_block_start_with_missing_index_appends():
    s = StreamingAssistant()
    s.apply_stream_event(
        {"type": "content_block_start", "content_block": {"type": "text"}}
    )
    s.apply_stream_event(_text_delta("hi"))  # missing-index delta → last block
    assert len(s.blocks) == 1
    assert s.to_turn().text_parts == ["hi"]


def test_block_start_with_huge_index_pads_without_crash():
    s = StreamingAssistant()
    s.apply_stream_event(_text_start(3))
    assert len(s.blocks) == 4
    s.apply_stream_event(_text_delta("end", 3))
    assert s.blocks[3].text == "end"


def test_delta_index_out_of_range_high_falls_back():
    s = StreamingAssistant()
    s.apply_stream_event(_text_start(0))
    # index 9 doesn't exist → fall back to last block rather than crash.
    s.apply_stream_event(_text_delta("z", 9))
    assert s.to_turn().text_parts == ["z"]


def test_non_int_index_is_tolerated():
    s = StreamingAssistant()
    s.apply_stream_event(
        {
            "type": "content_block_start",
            "index": "bogus",
            "content_block": {"type": "text"},
        }
    )
    assert len(s.blocks) == 1  # appended despite garbage index
    s.apply_stream_event(
        {
            "type": "content_block_delta",
            "index": None,
            "delta": {"type": "text_delta", "text": "ok"},
        }
    )
    assert s.to_turn().text_parts == ["ok"]


def test_tool_use_with_invalid_json_input_does_not_crash_to_turn():
    s = StreamingAssistant()
    s.apply_stream_event(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "name": "X", "id": "i"},
        }
    )
    s.apply_stream_event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{not valid"},
        }
    )
    turn = s.to_turn()
    assert turn.tool_uses[0].input == {"_raw": "{not valid"}


def test_unknown_event_type_is_ignored():
    s = StreamingAssistant()
    s.apply_stream_event({"type": "something_new_from_upstream"})
    s.apply_stream_event({})  # no type at all
    assert s.blocks == []


def test_empty_block_is_not_emitted_as_turn_content():
    s = StreamingAssistant()
    s.apply_stream_event(_text_start(0))  # block created but never filled
    turn = s.to_turn()
    assert turn.text_parts == []
    assert isinstance(s.blocks[0], Block)


# --- stable_markdown_prefix (T-1) ---------------------------------------


def test_stable_prefix_splits_after_a_blank_line():
    assert stable_markdown_prefix("a\n\nb") == 3


def test_stable_prefix_never_lands_inside_an_open_fence():
    """The blank line inside an unterminated code block is not a boundary."""
    assert stable_markdown_prefix("para\n\n```py\ncode\n\nmore") == 6


def test_stable_prefix_settles_a_closed_fence():
    text = "```\ncode\n```\n\nafter"
    n = stable_markdown_prefix(text)
    assert text[:n] == "```\ncode\n```\n\n"


def test_stable_prefix_understands_tilde_fences():
    assert stable_markdown_prefix("~~~\na\n\nb") == 0
    text = "~~~\na\n~~~\n\ntail"
    assert text[: stable_markdown_prefix(text)] == "~~~\na\n~~~\n\n"


def test_stable_prefix_is_zero_before_the_first_blank_line():
    assert stable_markdown_prefix("no blanks yet") == 0
    assert stable_markdown_prefix("") == 0


def test_stable_prefix_leaves_a_trailing_paragraph_unsettled():
    """The documented ceiling: the last block stays raw until it is closed."""
    text = "done\n\nstill writing this one"
    assert text[stable_markdown_prefix(text) :] == "still writing this one"


def test_stable_prefix_always_ends_on_a_line_boundary():
    for text in ("a\n\nb", "```\nc\n```\n\nd", "no blanks yet", ""):
        prefix = text[: stable_markdown_prefix(text)]
        assert prefix == "" or prefix.endswith("\n")


def test_stable_prefix_split_parses_identically():
    """The invariant the duplicated fence regex exists to preserve.

    Needs gi only for markdown._parse (it imports GtkSource), so this one
    test skips on the slim image while the rest of the block runs there.
    """
    pytest.importorskip("gi")
    from helios.widgets.markdown import _parse

    corpus = [
        "# Heading\n\nA paragraph.\n\n- one\n- two\n\ntail",
        "para\n\n```py\ncode\n\nmore code\n```\n\nafter",
        "> quoted\n> lines\n\n1. first\n2. second\n\n---\n\nend",
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\nafter the table",
        "~~~\nfenced\n~~~\n\nnext\n\n",
        "one\n\ntwo\n\nthree",
        "no blank lines at all",
        "",
    ]
    for text in corpus:
        n = stable_markdown_prefix(text)
        assert _parse(text[:n]) + _parse(text[n:]) == _parse(text), text
