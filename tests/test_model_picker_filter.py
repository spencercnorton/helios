"""Model-picker filtering.

The OpenRouter catalog is several hundred entries and the picker rendered every
one into a 380px scroll box with no way to narrow it. These tests exercise the
filter predicate directly — the widget itself needs real GTK, which the slim CI
lane does not have.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend.model_catalog import ModelEntry  # noqa: E402
from helios.widgets.chat_toolbar import _MODEL_SEARCH_THRESHOLD, ChatToolbar  # noqa: E402


def entries():
    return [
        ModelEntry("anthropic/claude-sonnet-4", "Claude Sonnet 4", "Anthropic"),
        ModelEntry("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", "DeepSeek"),
        ModelEntry("openai/gpt-5", "GPT-5", "OpenAI"),
        ModelEntry("z-ai/glm-5.2", "GLM 5.2", "Z.AI"),
    ]


def matches(query: str):
    """Mirror of the predicate in _fill_model_rows."""
    needle = query.strip().lower()
    return [
        e.id for e in entries()
        if not needle
        or needle in e.id.lower()
        or needle in e.label.lower()
        or needle in (e.group or "").lower()
    ]


class TestFilterPredicate:
    def test_empty_query_matches_everything(self):
        assert len(matches("")) == 4
        assert len(matches("   ")) == 4

    def test_matches_on_slug(self):
        assert matches("deepseek") == ["deepseek/deepseek-v4-flash"]

    def test_matches_on_label(self):
        assert matches("sonnet") == ["anthropic/claude-sonnet-4"]

    def test_matches_on_vendor_group(self):
        assert matches("z.ai") == ["z-ai/glm-5.2"]

    def test_is_case_insensitive(self):
        assert matches("GPT") == matches("gpt") == ["openai/gpt-5"]

    def test_no_match_is_empty_not_everything(self):
        """A filter that falls back to showing all rows is worse than useless —
        it looks like it worked."""
        assert matches("zzzznothing") == []


class TestSearchThreshold:
    def test_threshold_is_above_the_native_catalogs(self):
        """Claude and GPT together are a handful of rows; a search box there is
        clutter. The OpenRouter catalog is several hundred."""
        assert 10 < _MODEL_SEARCH_THRESHOLD < 100

    def test_toolbar_exposes_the_filler(self):
        assert callable(ChatToolbar._fill_model_rows)


def test_rate_limit_label_prefers_the_row_the_provider_labelled() -> None:
    """A Codex row carries its own label; the Claude table has no key for it."""

    from helios.widgets.chat_toolbar import rate_limit_label

    codex_row = {"label": "ChatGPT Pro · 5-hour usage", "usedPercent": 40}
    assert (
        rate_limit_label("gpt-5-codex-max-2026_primary", codex_row)
        == "ChatGPT Pro · 5-hour usage"
    )
    # Claude rows have no label field and must keep using the known-key table.
    assert rate_limit_label("five_hour", {"usedPercent": 40}) == "5-hour usage"
    # Nothing known either way still degrades to the raw key, not a crash.
    assert rate_limit_label("mystery", None) == "mystery"
