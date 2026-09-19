"""Model picker: one provider at a time, current models first.

The picker used to render every catalog entry from every provider into one
scroll box — so a Claude session offered OpenAI and OpenRouter rows that
picking would have silently switched provider. And Claude's binary scan
produces every pinned version it can find, burying the four aliases anyone
actually chooses.

The widget needs real GTK; these exercise the split logic directly, matching
the convention in test_model_picker_filter.py.
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend import model_catalog, ui_state  # noqa: E402
from helios.backend.model_catalog import ModelEntry  # noqa: E402
from helios.widgets.chat_toolbar import ChatToolbar  # noqa: E402

ANTHROPIC = model_catalog.PROVIDER_ANTHROPIC
OPENAI = model_catalog.PROVIDER_OPENAI
OPENROUTER = model_catalog.PROVIDER_OPENROUTER


def _catalog() -> list[ModelEntry]:
    return [
        ModelEntry("fable[1m]", "Fable (latest · 1M context)", "Latest"),
        ModelEntry("opus", "Opus (latest)", "Latest"),
        ModelEntry("sonnet", "Sonnet (latest)", "Latest"),
        ModelEntry("", "Default (from Claude settings)", "Other"),
        ModelEntry("claude-opus-4-5", "Opus 4.5", "Opus"),
        ModelEntry("claude-sonnet-4-5", "Sonnet 4.5", "Sonnet"),
        ModelEntry("gpt-5.6-sol", "GPT-5.6 Sol", "OpenAI", OPENAI),
        ModelEntry("gpt-5.5", "GPT-5.5", "OpenAI", OPENAI),
        ModelEntry("anthropic/claude-opus-4.8", "Opus 4.8", "Anthropic", OPENROUTER),
        ModelEntry("deepseek/deepseek-v4", "DeepSeek V4", "DeepSeek", OPENROUTER),
        ModelEntry("z-ai/glm-5.2", "GLM 5.2", "Z.AI", OPENROUTER),
    ]


def _toolbar(provider: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(_choices=_catalog(), _provider_filter=provider)


def pool(provider: str) -> list[str]:
    return [e.id for e in ChatToolbar._provider_pool(_toolbar(provider))]


def split(provider: str) -> tuple[list[str], list[str]]:
    fake = _toolbar(provider)
    current, older = ChatToolbar._split_by_currency(
        fake, ChatToolbar._provider_pool(fake)
    )
    return [e.id for e in current], [e.id for e in older]


def test_claude_picker_offers_no_openai_or_openrouter_rows():
    assert pool(ANTHROPIC) == [
        "fable[1m]",
        "opus",
        "sonnet",
        "",
        "claude-opus-4-5",
        "claude-sonnet-4-5",
    ]


def test_each_provider_sees_only_its_own_models():
    assert pool(OPENAI) == ["gpt-5.6-sol", "gpt-5.5"]
    assert pool(OPENROUTER) == [
        "anthropic/claude-opus-4.8",
        "deepseek/deepseek-v4",
        "z-ai/glm-5.2",
    ]
    assert set(pool(ANTHROPIC)) & set(pool(OPENROUTER)) == set()


def test_claude_pinned_versions_fold_behind_the_disclosure():
    current, older = split(ANTHROPIC)
    # Aliases (incl. the 1M variant and the settings-owned default) stay up
    # front; only the pinned dated versions fold away.
    assert current == ["fable[1m]", "opus", "sonnet", ""]
    assert older == ["claude-opus-4-5", "claude-sonnet-4-5"]


def test_short_openai_catalog_shows_everything_up_front():
    current, older = split(OPENAI)
    assert current == ["gpt-5.6-sol", "gpt-5.5"]
    assert older == []


def test_openrouter_first_tier_is_the_configured_shortlist(monkeypatch):
    monkeypatch.setattr(
        ui_state,
        "store",
        lambda: types.SimpleNamespace(
            get=lambda _key, default=None: ["z-ai/glm-5.2"]
        ),
    )
    current, older = split(OPENROUTER)
    assert current == ["z-ai/glm-5.2"]
    assert older == ["anthropic/claude-opus-4.8", "deepseek/deepseek-v4"]


def test_unconfigured_openrouter_shows_the_whole_catalog(monkeypatch):
    # Never present an empty first tier with the real list hidden behind a
    # disclosure — that reads as "no models available".
    monkeypatch.setattr(
        ui_state,
        "store",
        lambda: types.SimpleNamespace(get=lambda _key, default=None: []),
    )
    current, older = split(OPENROUTER)
    assert current == pool(OPENROUTER)
    assert older == []
