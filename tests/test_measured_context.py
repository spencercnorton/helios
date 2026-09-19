"""The context meter reads the provider instead of guessing.

GTK-free so CI's python-slim backend job runs it.

Measured 2026-08-07 against claude 2.1.224, `--model sonnet`:

    totalTokens          40,609   == the sum of the NON-deferred categories
    maxTokens            967,000
    rawMaxTokens         967,000
    Autocompact buffer    33,000
    modelUsage.contextWindow  1,000,000

    totalTokens + Free space + Autocompact buffer == 967,000
    967,000 + 33,000                              == 1,000,000

So the two window figures are not in conflict — 1,000,000 is the model's hard
cap and 967,000 is what may be used before autocompaction fires. The second is
the actionable one and is what the CLI's own /context reports.
"""

from __future__ import annotations

from helios.backend.context_breakdown import (
    AUTOCOMPACT,
    deferred_tokens,
    from_measured,
)
from helios.backend.model_catalog import context_window_for

# The live payload, trimmed to the fields Helios reads.
MEASURED = {
    "totalTokens": 40609,
    "maxTokens": 967000,
    "rawMaxTokens": 967000,
    "percentage": 4,
    "autocompactSource": "model-default",
    "categories": [
        {"name": "System prompt", "tokens": 8807},
        {"name": "System tools", "tokens": 18097},
        {"name": "MCP tools (deferred)", "tokens": 25445, "isDeferred": True},
        {"name": "System tools (deferred)", "tokens": 16304, "isDeferred": True},
        {"name": "Custom agents", "tokens": 554},
        {"name": "Memory files", "tokens": 8617},
        {"name": "Skills", "tokens": 2620},
        {"name": "Messages", "tokens": 1914},
        {"name": "Autocompact buffer", "tokens": 33000},
        {"name": "Free space", "tokens": 893391},
    ],
}


def test_segments_reconcile_exactly_with_the_reported_total() -> None:
    """Not approximately — exactly. This is the whole point of replacing the
    4-chars-per-token estimate."""

    b = from_measured(MEASURED)

    content = sum(s.tokens for s in b.segments if s.key != AUTOCOMPACT)
    assert content == MEASURED["totalTokens"] == 40609
    assert b.used == 40609


def test_the_denominator_is_the_autocompact_threshold_not_the_hard_cap() -> None:
    """967,000 is when something happens to the user; 1,000,000 is trivia."""

    assert from_measured(MEASURED).window == 967000


def test_deferred_schemas_are_excluded_from_the_bar() -> None:
    """`isDeferred` rows describe schemas the prompt does NOT carry — the CLI
    itself leaves them out of totalTokens. Counting them as occupancy inverts
    the mechanism deferral exists for, and overstates the fixed cost by 41,749
    tokens here. An earlier audit made exactly that mistake and concluded the
    MCP budget had grown by an order of magnitude."""

    b = from_measured(MEASURED)

    labels = [s.label for s in b.segments]
    assert not any("deferred" in x.lower() for x in labels)
    assert deferred_tokens(MEASURED) == 25445 + 16304 == 41749


def test_free_space_is_not_a_segment() -> None:
    """It is the remainder the bar already draws as empty; adding it would
    peg the bar at 100% forever."""

    assert not any(
        s.label.lower() == "free space" for s in from_measured(MEASURED).segments
    )


def test_the_autocompact_buffer_is_shown_because_it_is_reserved() -> None:
    b = from_measured(MEASURED)
    buf = [s for s in b.segments if s.key == AUTOCOMPACT]
    assert len(buf) == 1 and buf[0].tokens == 33000


def test_a_useless_payload_returns_none_so_the_estimate_survives() -> None:
    """None must mean "keep the estimate", never "render an empty bar"."""

    assert from_measured({}) is None
    assert from_measured({"categories": []}) is None
    assert from_measured({"categories": [{"name": "x", "tokens": 1}]}) is None  # no window
    assert from_measured(None) is None  # type: ignore[arg-type]


# --- the last-resort window estimate ---------------------------------------


def test_plain_aliases_are_no_longer_under_reported() -> None:
    """`--model sonnet` resolved to claude-sonnet-5 with contextWindow
    1,000,000, but the `[1m] in id` rule returned 200,000 for it — wrong by
    5x."""

    assert context_window_for("sonnet") == 1_000_000
    assert context_window_for("claude-sonnet-5") == 1_000_000
    assert context_window_for("claude-opus-5[1m]") == 1_000_000


def test_older_families_are_still_200k() -> None:
    """The trap in substring matching: a bare "opus" rule would also match
    claude-opus-4-5, which really is 200k."""

    assert context_window_for("claude-opus-4-5") == 200_000
    assert context_window_for("claude-opus-4-8") == 200_000
    assert context_window_for("claude-haiku-4-5-20251001") == 200_000
    assert context_window_for("haiku") == 200_000
