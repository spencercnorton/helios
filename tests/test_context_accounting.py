"""The context meter's numerator.

The meter's whole job is to be trustworthy, and it was not: `result.usage`
sums every API request a turn made, so a turn with three tool calls reported
roughly three times the tokens actually resident in the window.

The implementation deliberately lives outside ``cli_driver`` so this entire
regression suite runs in the GTK-free slim CI lane.
"""

from __future__ import annotations

import types

from helios.backend.process.context_accounting import (
    main_model_context,
    model_family,
    prompt_tokens,
)


# ── numerator ──────────────────────────────────────────────────────────────


def _driver_stub(model: str = "haiku") -> types.SimpleNamespace:
    """The state `_main_model_context` actually reads. Constructing a real
    driver would spawn claude."""
    return types.SimpleNamespace(
        _model=model,
        _ctx_request_input=0,
        _ctx_request_output=0,
    )


def _result_record(*, iterations, totals, window=200_000, model="claude-haiku-4-5"):
    usage = dict(totals)
    if iterations is not None:
        usage["iterations"] = iterations
    return {
        "usage": usage,
        "modelUsage": {model: {"contextWindow": window}},
    }


def test_turn_with_several_requests_reports_the_last_one_not_the_sum():
    # Measured from a real 3-request turn: the top-level usage sums all three,
    # so the old numerator claimed 102k of a window holding 34k.
    record = _result_record(
        iterations=[
            {
                "input_tokens": 8,
                "output_tokens": 30,
                "cache_read_input_tokens": 33_637,
                "cache_creation_input_tokens": 1_002,
            }
        ],
        totals={
            "input_tokens": 26,
            "output_tokens": 284,
            "cache_read_input_tokens": 84_630,
            "cache_creation_input_tokens": 17_103,
        },
    )
    used, window = main_model_context(_driver_stub(), record)
    assert used == 8 + 30 + 33_637 + 1_002
    assert window == 200_000
    assert used < 40_000  # not the 102k the sum would have given


def test_missing_iterations_falls_back_to_the_live_per_request_counts():
    fake = _driver_stub()
    fake._ctx_request_input = 33_637
    fake._ctx_request_output = 30
    record = _result_record(
        iterations=None,
        totals={
            "input_tokens": 26,
            "output_tokens": 284,
            "cache_read_input_tokens": 84_630,
            "cache_creation_input_tokens": 17_103,
        },
    )
    used, _window = main_model_context(fake, record)
    assert used == 33_667


def test_single_request_turn_is_unchanged_by_the_fix():
    # One request: the sum and the last request are the same thing. This is
    # the case the old code got right, and must keep getting right.
    only = {
        "input_tokens": 10,
        "output_tokens": 250,
        "cache_read_input_tokens": 12_000,
        "cache_creation_input_tokens": 300,
    }
    record = _result_record(iterations=[dict(only)], totals=dict(only))
    used, _window = main_model_context(_driver_stub(), record)
    assert used == 12_560


def test_selected_family_and_1m_variant_choose_the_matching_window():
    record = {
        "usage": {"input_tokens": 1},
        "modelUsage": {
            "claude-haiku-4-5": {"contextWindow": 200_000},
            "claude-sonnet-4-6": {"contextWindow": 200_000},
            "claude-sonnet-4-6[1m]": {"contextWindow": 1_000_000},
        },
    }
    assert main_model_context(_driver_stub("sonnet[1m]"), record) == (
        1,
        1_000_000,
    )


def test_unknown_family_falls_back_to_the_largest_reported_window():
    record = {
        "usage": {"input_tokens": 2},
        "modelUsage": {
            "side-model": {"contextWindow": 32_000},
            "main-model": {"contextWindow": 128_000},
        },
    }
    assert main_model_context(_driver_stub("future-model"), record) == (
        2,
        128_000,
    )


def test_model_family_and_prompt_tokens_are_gtk_free_helpers():
    assert model_family("CLAUDE-OPUS-4-6") == "opus"
    assert model_family("future-model") == ""
    assert prompt_tokens(
        {
            "input_tokens": 3,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 7,
        }
    ) == 15
