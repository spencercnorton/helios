"""Root-vs-delegated token accounting for Claude sessions.

The numbers in `TestMeasuredCaptures` are the ones the private model-usage capture notes
recorded off the real wire on 2026-08-06. They are here because the whole claim
is that delegated spend is an *identity* rather than an estimate — zero drift on
turns that ran no subagents — and an identity deserves to be pinned against the
measurement that established it, not against numbers invented to pass.
"""

from __future__ import annotations

import logging

import pytest

from helios.backend.process.spend_accounting import (
    SpendAccumulator,
    model_spend_rows,
    root_turn_tokens,
)


def _result(*, model_total: int, root_turn: int, parent: str = "", models=None):
    """A root `result` record carrying the two channels that matter.

    `modelUsage` is cumulative for the process; top-level `usage` is this
    turn's root-only spend. Everything is loaded into cacheRead/cache_read
    because the split does not care which counter the tokens arrived in.
    """
    record: dict = {
        "type": "result",
        "modelUsage": models
        if models is not None
        else {
            "claude-sonnet-5": {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadInputTokens": model_total,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "contextWindow": 1_000_000,
                "canonicalModel": "claude-sonnet-5",
                "provider": "firstParty",
            }
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": root_turn,
            "cache_creation_input_tokens": 0,
        },
    }
    if parent:
        record["parent_tool_use_id"] = parent
    return record


class TestMeasuredCaptures:
    def test_foreground_capture(self):
        """Turn 1 no subagents, turn 2 two subagents. Doc §3, foreground row."""
        acc = SpendAccumulator()

        first = acc.observe(_result(model_total=48_670, root_turn=48_670))
        assert first.total_tokens == 48_670
        assert first.root_tokens == 48_670
        # Exact zero on a turn with no delegation is what makes the identity
        # trustworthy; an estimate would drift here.
        assert first.delegated_tokens == 0
        assert first.has_delegation is False

        second = acc.observe(_result(model_total=203_975, root_turn=99_514))
        assert second.total_tokens == 203_975
        assert second.root_tokens == 148_184
        assert second.delegated_tokens == 55_791
        assert round(second.delegated_fraction * 100, 1) == 27.4

    def test_background_capture_bills_children_after_the_root_replied(self):
        """Doc §3, background rows: the fan-out's cost lands on later records.

        Turn 2 launches subagents and reports zero delegated because none have
        finished. Turn 3 bills them. Turn 4 adds root work only, so the
        delegated figure must stay put rather than drifting upward.
        """
        acc = SpendAccumulator()

        launched = acc.observe(_result(model_total=148_651, root_turn=148_651))
        assert launched.delegated_tokens == 0

        billed = acc.observe(_result(model_total=252_797, root_turn=51_376))
        assert billed.delegated_tokens == 52_770
        assert round(billed.delegated_fraction * 100, 1) == 20.9

        later = acc.observe(_result(model_total=305_111, root_turn=52_314))
        assert later.delegated_tokens == 52_770


class TestAttribution:
    def test_a_child_result_is_ignored(self):
        """Its spend is already inside the root's cumulative `modelUsage`.

        Counting it on the root side would inflate the subtrahend and erase the
        very delegation the split exists to show.
        """
        acc = SpendAccumulator()
        acc.observe(_result(model_total=200_000, root_turn=100_000))
        assert acc.observe(_result(model_total=9, root_turn=99_999, parent="toolu_1")) is None
        assert acc.snapshot().delegated_tokens == 100_000

    def test_several_root_results_for_one_message_all_count(self):
        """A background subagent finishing wakes the root again (doc §6).

        Those records carry no `parent_tool_use_id`, so they are roots, and for
        a background fan-out they are exactly when the cost lands. Dropping
        them would leave the burn invisible in the case that caused the ticket.
        """
        acc = SpendAccumulator()
        acc.observe(_result(model_total=100_000, root_turn=100_000))
        snapshot = acc.observe(_result(model_total=180_000, root_turn=10_000))
        assert snapshot.root_tokens == 110_000
        assert snapshot.delegated_tokens == 70_000

    def test_per_model_rows_keep_the_side_model_separate(self):
        rows = model_spend_rows(
            _result(
                model_total=0,
                root_turn=0,
                models={
                    "claude-haiku-4-5-20251001": {
                        "inputTokens": 18,
                        "outputTokens": 251,
                        "cacheReadInputTokens": 11_635,
                        "cacheCreationInputTokens": 12_124,
                        "canonicalModel": "claude-haiku-4-5",
                        "provider": "firstParty",
                    },
                    "claude-sonnet-5": {
                        "inputTokens": 10,
                        "outputTokens": 552,
                        "cacheReadInputTokens": 162_172,
                        "cacheCreationInputTokens": 17_213,
                        "canonicalModel": "claude-sonnet-5",
                        "provider": "firstParty",
                    },
                },
            )
        )
        assert [row.canonical for row in rows] == [
            "claude-sonnet-5",
            "claude-haiku-4-5",
        ]  # biggest first
        assert rows[0].total_tokens == 179_947
        assert rows[1].total_tokens == 24_028


@pytest.fixture
def spend_logs(caplog):
    """Capture `helios.spend` records.

    `helios/log.py` sets `propagate = False` on the `helios` logger so app
    output never doubles through the root handler. caplog attaches to root, so
    without re-enabling propagation for the duration it sees nothing and an
    assertion on the warning passes vacuously.
    """
    logger = logging.getLogger("helios")
    previous = logger.propagate
    logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger="helios.spend"):
            yield caplog
    finally:
        logger.propagate = previous


class TestFailsSafely:
    @pytest.mark.parametrize("record", [None, "result", 7, {}, {"modelUsage": []}])
    def test_a_record_with_no_usage_yields_nothing(self, record):
        assert SpendAccumulator().observe(record) is None

    @pytest.mark.parametrize(
        "value", [None, "12", -5, 1.5, True, [], {}]
    )
    def test_a_non_counter_field_reads_as_zero(self, value):
        rows = model_spend_rows(
            {"modelUsage": {"m": {"inputTokens": value, "outputTokens": 10}}}
        )
        assert rows[0].input_tokens == 0
        assert rows[0].output_tokens == 10

    def test_root_turn_tokens_sums_all_four_counters(self):
        assert root_turn_tokens(
            {
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 8,
                }
            }
        ) == 15

    def test_root_turn_tokens_ignores_iterations(self):
        """`usage.iterations[-1]` is the last request — the context meter's
        number. Spend wants everything the turn burned, and reading the wrong
        one here is precisely the confusion this module was split out to
        prevent."""
        assert root_turn_tokens(
            {
                "usage": {
                    "input_tokens": 100,
                    "iterations": [{"input_tokens": 999_999}],
                }
            }
        ) == 100

    def test_delegated_never_renders_negative(self, spend_logs):
        """A negative means the two channels stopped meaning what they were
        measured to mean. Clamp rather than show nonsense — but say so, because
        a silently clamped zero is indistinguishable from a healthy one."""
        acc = SpendAccumulator()
        snapshot = acc.observe(_result(model_total=10, root_turn=5_000))
        assert snapshot.delegated_tokens == 0
        assert snapshot.root_tokens == 10  # never exceeds the total either
        assert snapshot.delegated_fraction == 0.0
        assert any("identity no longer holds" in r.message for r in spend_logs.records)

    def test_the_negative_warning_is_logged_once(self, spend_logs):
        acc = SpendAccumulator()
        for _ in range(5):
            acc.observe(_result(model_total=10, root_turn=5_000))
        assert sum(
            "identity no longer holds" in r.message for r in spend_logs.records
        ) == 1

    def test_zero_total_has_no_fraction(self):
        acc = SpendAccumulator()
        snapshot = acc.observe(_result(model_total=0, root_turn=0))
        assert snapshot.delegated_fraction == 0.0
