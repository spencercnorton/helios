"""The compaction boundary has to survive the transcript parser.

GTK-free on purpose: CI runs the backend suite in python:3.13-slim.

The filed defect said `is_meta` was hiding the boundary. It was not — `is_meta`
gates only DERIVED text (insights, handoff tails, the archiver, plan-pane
grouping) and never rendering, and `iter_transcript` filters on exactly
`is_sidechain` and `has_content`. The real cause is that `turn_from_record`
reads message text only out of `obj["message"]`, while a `system` record keeps
its text at the top level — so the boundary produced a content-less Turn and
`has_content` dropped it.

The `isMeta or isCompactSummary` fold on the Turn is CORRECT and deliberately
untouched: the CLI itself stamps synthetic with the same three fields.
"""

from __future__ import annotations

from helios.backend.transcript import turn_from_record


def _boundary(meta: dict | None, *, key: str = "compactMetadata") -> dict:
    rec = {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": "2026-08-07T04:00:00Z",
        "uuid": "u-1",
    }
    if meta is not None:
        rec[key] = meta
    return rec


def test_the_boundary_is_no_longer_dropped() -> None:
    turn = turn_from_record(_boundary({"preTokens": 148000, "postTokens": 21000}))

    assert turn is not None, "the boundary record produced no Turn at all"
    assert turn.has_content, "content-less Turns are dropped by iter_transcript"
    assert "compacted" in turn.text.lower()


def test_camelcase_and_snake_case_are_both_accepted() -> None:
    """The transcript file keeps compactMetadata camelCase; only the SDK
    stream snake-cases it. No real compacted transcript exists on any box to
    pin which one lands here, so both are read."""

    camel = turn_from_record(_boundary({"preTokens": 148000, "postTokens": 21000}))
    snake = turn_from_record(_boundary({"pre_tokens": 148000, "post_tokens": 21000}))

    assert "148,000" in camel.text
    assert camel.text == snake.text


def test_a_snake_case_container_is_accepted_too() -> None:
    """Caught in review: accepting both spellings of the nested KEYS while
    reading only a camelCase CONTAINER means a `compact_metadata` record
    renders the boundary with no token figures at all."""

    turn = turn_from_record(
        _boundary({"pre_tokens": 148000, "post_tokens": 21000},
                  key="compact_metadata")
    )

    assert "148,000" in turn.text
    assert "21,000" in turn.text


def test_a_missing_post_count_prints_no_numbers_rather_than_zero() -> None:
    """`postTokens` is patched on after the marker is built and the CLI itself
    defends against its absence. Gating on `pre` alone would render
    "148,000 -> 0 tokens", which reads as "the context was emptied"."""

    turn = turn_from_record(_boundary({"preTokens": 148000}))

    text = turn.text
    assert "148,000" not in text
    assert "0 tokens" not in text
    assert "compacted" in text.lower()


def test_no_metadata_at_all_still_renders_the_marker() -> None:
    turn = turn_from_record(_boundary(None))

    assert turn is not None
    assert turn.has_content


def test_the_marker_does_not_overclaim_what_was_summarised() -> None:
    """A compaction can preserve a trailing segment, so "everything above this
    line is a summary" would be false."""

    text = turn_from_record(_boundary({"preTokens": 9, "postTokens": 3})).text

    assert "everything above" not in text.lower()


def test_a_compact_summary_is_still_meta() -> None:
    """Unchanged behaviour, pinned so the fold is not "fixed" later: the CLI
    marks these synthetic with the same fields, and the four consumers of
    is_meta all want them excluded from derived text."""

    turn = turn_from_record(
        {
            "type": "user",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "…summary…"},
        }
    )

    assert turn is not None and turn.is_meta


def test_other_system_records_are_still_dropped() -> None:
    """Scoped strictly to compact_boundary. Rendering system records generally
    would start surfacing local-command and hook XML."""

    turn = turn_from_record(
        {"type": "system", "subtype": "local_command", "content": "<command>x</command>"}
    )

    assert turn is None or not turn.has_content


def test_tool_result_only_user_record_is_retagged_off_user():
    """Claude ships tool output as type=user/role=user. Rendered as a user turn
    it wore the "You" header and the user accent wash."""
    turn = turn_from_record(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
                ],
            },
        }
    )

    assert turn.role == "tool"
    assert turn.has_content  # still rendered, just not as a person
    assert len(turn.tool_results) == 1


def test_user_text_alongside_a_tool_result_stays_a_user_turn():
    """The retag must be narrow: real prose in the same record is the human."""
    turn = turn_from_record(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "ok"},
                    {"type": "text", "text": "now do the other one"},
                ],
            },
        }
    )

    assert turn.role == "user"


def test_a_tool_use_alongside_a_tool_result_stays_a_user_turn():
    """The other half of the narrowness contract.

    No `type=user` record in 238 real Claude transcripts carries a tool_use --
    32,333 tool_result-bearing records checked, zero mixed -- and Claude's
    schema makes tool_use assistant-only. But Codex and OpenRouter write their
    own transcripts through this same parser, and the retag is a
    classification: a record that ISSUES a tool call is not transport for a
    tool's output, so it keeps its speaker.

    Without this the clause is unpinnable dead code and gets deleted by the
    next reader -- which is exactly what happened once already.
    """
    turn = turn_from_record(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": "ok"},
                    {"type": "tool_use", "name": "Bash", "input": {}, "id": "u"},
                ],
            },
        }
    )

    assert turn.role == "user"
