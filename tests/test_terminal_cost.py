"""Per-turn cost/token accounting: the parsers and the footer they feed.

The parsers live in `backend/process/streaming.py` precisely so they run on
the slim CI image — the cost path feeds `ExecutionTerminalEvidence` and the
work store, and until now its only test sat behind a gi importorskip. Only the
last test in this file needs GTK, and it guards itself.
"""

from __future__ import annotations

import pytest

from helios.backend.process.streaming import (
    format_turn_footer,
    terminal_cost_micro_usd,
    terminal_tokens,
)


# --- terminal_cost_micro_usd: every rejection branch ---------------------


def test_cost_scales_dollars_to_micro_usd():
    assert terminal_cost_micro_usd({"total_cost_usd": 0.0123}) == 12300
    assert terminal_cost_micro_usd({"total_cost_usd": 2}) == 2_000_000
    assert terminal_cost_micro_usd({"total_cost_usd": 0.0}) == 0


def test_cost_rejects_a_bool():
    """True is an int in Python; a flag is not a price."""
    assert terminal_cost_micro_usd({"total_cost_usd": True}) is None
    assert terminal_cost_micro_usd({"total_cost_usd": False}) is None


def test_cost_rejects_non_numbers_and_a_missing_key():
    assert terminal_cost_micro_usd({}) is None
    assert terminal_cost_micro_usd({"total_cost_usd": None}) is None
    assert terminal_cost_micro_usd({"total_cost_usd": "0.01"}) is None


def test_cost_rejects_non_finite_and_negative():
    assert terminal_cost_micro_usd({"total_cost_usd": float("inf")}) is None
    assert terminal_cost_micro_usd({"total_cost_usd": float("nan")}) is None
    assert terminal_cost_micro_usd({"total_cost_usd": -1}) is None


def test_cost_rejects_a_value_over_the_sqlite_integer_cap():
    """The column is a SQLite INTEGER; an overflowing turn is not recorded."""
    assert terminal_cost_micro_usd({"total_cost_usd": 1 << 70}) is None


# --- terminal_tokens -----------------------------------------------------


def test_tokens_reads_the_input_output_pair():
    assert terminal_tokens(
        {"usage": {"input_tokens": 1234, "output_tokens": 567}}
    ) == (1234, 567)


def test_tokens_is_none_without_a_usage_dict():
    assert terminal_tokens({}) is None
    assert terminal_tokens({"usage": None}) is None
    assert terminal_tokens({"usage": []}) is None


def test_tokens_is_all_or_nothing():
    """Half a pair would render as "0 out" — a measurement, not a gap."""
    assert terminal_tokens({"usage": {"input_tokens": 5}}) is None
    assert terminal_tokens({"usage": {"output_tokens": 5}}) is None


def test_tokens_rejects_bools_and_negatives():
    assert terminal_tokens({"usage": {"input_tokens": True, "output_tokens": 1}}) is None
    assert terminal_tokens({"usage": {"input_tokens": 1, "output_tokens": -1}}) is None
    assert (
        terminal_tokens({"usage": {"input_tokens": 1.5, "output_tokens": 1}}) is None
    )


# --- format_turn_footer --------------------------------------------------


def test_footer_joins_tokens_and_cost():
    assert (
        format_turn_footer(
            {
                "total_cost_usd": 0.0123,
                "usage": {"input_tokens": 1234, "output_tokens": 567},
            }
        )
        == "1,234 in · 567 out · $0.0123"
    )


def test_footer_uses_two_decimals_at_or_above_a_dollar():
    assert format_turn_footer({"total_cost_usd": 1.5}) == "$1.50"
    assert format_turn_footer({"total_cost_usd": 1.0}) == "$1.00"  # the boundary
    # Below it, four — so a fraction-of-a-cent turn never reads as "$0.00".
    assert format_turn_footer({"total_cost_usd": 0.999}) == "$0.9990"
    assert format_turn_footer({"total_cost_usd": 0.0099}) == "$0.0099"


def test_footer_omits_a_cost_the_provider_did_not_report():
    assert (
        format_turn_footer({"usage": {"input_tokens": 10, "output_tokens": 2}})
        == "10 in · 2 out"
    )
    assert format_turn_footer({"total_cost_usd": 0.0}) == ""


def test_footer_is_empty_when_nothing_is_known():
    """Codex/OpenRouter terminal payloads carry neither half."""
    assert format_turn_footer({}) == ""
    assert format_turn_footer({"total_cost_usd": "free", "usage": 3}) == ""


# --- the widget seam -----------------------------------------------------


def test_set_last_turn_footer_targets_only_the_last_assistant_bubble():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    try:
        gi.require_version("GtkSource", "5")
    except ValueError:
        pytest.skip("GtkSource 5 unavailable")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    Adw.init()
    from helios.backend.transcript import ContentSpan, Turn
    from helios.widgets.transcript_view import TranscriptView

    def _turn(role: str, text: str) -> Turn:
        turn = Turn(role=role)
        turn.content.append(ContentSpan("text", text))
        return turn

    view = TranscriptView()
    view.append_turn(_turn("assistant", "first answer"))
    view.append_turn(_turn("user", "and then?"))

    # The last bubble is the user's, so there is nothing to annotate.
    view.set_last_turn_footer("10 in · 2 out")
    bubbles = _content_children(view)
    assert all(b._footer is None for b in bubbles)

    view.append_turn(_turn("assistant", "second answer"))
    view.set_last_turn_footer("10 in · 2 out")
    bubbles = _content_children(view)
    assert bubbles[0]._footer is None  # the earlier answer is untouched
    assert bubbles[-1]._footer.get_text() == "10 in · 2 out"

    view.set_last_turn_footer("")
    assert _content_children(view)[-1]._footer is None


def _content_children(view) -> list:
    children = []
    child = view._list.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    return children


def test_input_folds_the_cache_counters_claude_reports():
    """A real Claude result: input_tokens 4, cache_read 30,000, cache_creation 1,200."""
    from helios.backend.process.streaming import format_turn_footer, terminal_tokens

    result = {
        "usage": {
            "input_tokens": 4,
            "output_tokens": 567,
            "cache_read_input_tokens": 30_000,
            "cache_creation_input_tokens": 1_200,
        },
        "total_cost_usd": 0.0123,
    }
    assert terminal_tokens(result) == (31_204, 567)
    assert format_turn_footer(result).startswith("31,204 in · 567 out")
    # Missing or junk cache counters change nothing.
    assert terminal_tokens({"usage": {"input_tokens": 4, "output_tokens": 1, "cache_read_input_tokens": True}}) == (4, 1)


def test_a_background_wakeup_result_is_recognised():
    from helios.backend.process.streaming import is_background_wakeup

    assert is_background_wakeup({"origin": {"kind": "task-notification"}, "usage": {}}) is True
    assert is_background_wakeup({"subtype": "success"}) is False
    assert is_background_wakeup("nope") is False
