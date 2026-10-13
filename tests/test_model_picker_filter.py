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

from helios.backend import model_catalog  # noqa: E402
from helios.backend.model_catalog import ModelEntry  # noqa: E402
from helios.widgets.chat_toolbar import ChatToolbar  # noqa: E402


def entries():
    return [
        ModelEntry("anthropic/claude-sonnet-4", "Claude Sonnet 4", "Anthropic"),
        ModelEntry("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash", "DeepSeek"),
        ModelEntry("openai/gpt-5", "GPT-5", "OpenAI"),
        ModelEntry("z-ai/glm-5.2", "GLM 5.2", "Z.AI"),
    ]


def rendered(widget) -> list[str]:
    """Labels of every Gtk.Button under `widget`, recursively."""
    from gi.repository import Gtk

    labels: list[str] = []

    def walk(node):
        if isinstance(node, Gtk.Button):
            labels.append(node.get_label() or "")
        child = node.get_first_child() if hasattr(node, "get_first_child") else None
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(widget)
    return labels


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


class TestPickerHygieneHelpers:
    def test_dedupe_first_wins_per_provider_and_id(self):
        rows = [
            ModelEntry("opus", "Opus", "Latest"),
            ModelEntry("opus", "Opus AGAIN", "Latest"),
            ModelEntry("opus", "Opus OR", "OpenRouter",
                       provider=model_catalog.PROVIDER_OPENROUTER),
            ModelEntry("sonnet", "Sonnet", "Latest"),
        ]
        unique = model_catalog.dedupe_entries(rows)
        assert [e.label for e in unique] == ["Opus", "Opus OR", "Sonnet"]

    def test_visible_entries_hides_but_keeps_selection_and_default(self):
        rows = entries() + [ModelEntry("", "Default", "Other")]
        visible = model_catalog.visible_entries(
            rows,
            ["z-ai/glm-5.2", "deepseek/deepseek-v4-flash", ""],
            keep_id="deepseek/deepseek-v4-flash")
        assert [e.id for e in visible] == [
            "anthropic/claude-sonnet-4", "deepseek/deepseek-v4-flash",
            "openai/gpt-5", ""]

    def test_collapse_latest_twins_one_row_per_family(self):
        pool = [
            ModelEntry("opus[1m]", "Opus 1M", "Latest"),
            ModelEntry("opus", "Opus", "Latest"),
            ModelEntry("sonnet[1m]", "Sonnet 1M", "Latest"),
            ModelEntry("sonnet", "Sonnet", "Latest"),
            ModelEntry("", "Default", "Other"),
            ModelEntry("claude-opus-4-8", "Opus 4.8", "Opus"),
        ]
        current, older = model_catalog.collapse_latest_twins(pool)
        assert [e.id for e in current] == ["opus[1m]", "sonnet[1m]", ""]
        assert [e.id for e in older] == ["opus", "sonnet", "claude-opus-4-8"]


class TestSearchAlwaysAvailable:
    def test_toolbar_exposes_the_filler(self):
        assert callable(ChatToolbar._fill_model_rows)

    def test_small_catalog_still_gets_a_search_field(self):
        """Search used to appear only above 25 total rows, which hid it on
        exactly the smaller multi-provider setups that need it."""
        from gi.repository import Gtk

        if not Gtk.init_check():
            pytest.skip("GTK display is unavailable")
        toolbar = ChatToolbar()
        toolbar.set_choices(entries())  # 4 rows — far below the old threshold
        popover = toolbar._model_btn.get_popover()
        found = []

        def walk(widget):
            if isinstance(widget, Gtk.SearchEntry):
                found.append(widget)
            child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
            while child is not None:
                walk(child)
                child = child.get_next_sibling()

        walk(popover.get_child())
        assert found, "model popover has no search field for a small catalog"


class TestHiddenModelsInToolbar:
    @pytest.fixture()
    def toolbar(self):
        from gi.repository import Gtk

        if not Gtk.init_check():
            pytest.skip("GTK display is unavailable")
        from helios.backend import ui_state

        store = ui_state.store()
        previous = store.get(ui_state.HIDDEN_MODELS_KEY, [])
        bar = ChatToolbar()
        bar._choices = [
            ModelEntry("sonnet", "Claude Sonnet", "Latest"),
            ModelEntry("z-ai/glm-5.2", "GLM 5.2", "Z.AI",
                       provider=model_catalog.PROVIDER_OPENROUTER),
        ]
        yield bar, store
        store.set(ui_state.HIDDEN_MODELS_KEY, previous)

    def test_hidden_model_disappears_until_selected(self, toolbar):
        from gi.repository import Gtk
        from helios.backend import ui_state

        bar, store = toolbar
        store.set(ui_state.HIDDEN_MODELS_KEY, ["z-ai/glm-5.2"])
        bar._model_alias = "sonnet"
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Search renders the flat cross-provider list, so GLM would show
        # here if hiding did not remove it first.
        bar._fill_model_rows(box, Gtk.Popover(), "glm")
        assert not any("GLM 5.2" in text for text in rendered(box))

        # The selected model is never hidden out from under its session.
        bar._model_alias = "z-ai/glm-5.2"
        box2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar._fill_model_rows(box2, Gtk.Popover(), "glm")
        assert any("GLM 5.2" in text for text in rendered(box2))


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


def test_current_claude_quota_names_are_readable():
    from helios.widgets.chat_toolbar import rate_limit_label
    assert rate_limit_label("seven_day") == "Weekly usage"
    assert rate_limit_label("seven_day_sonnet") == "Weekly (Sonnet only)"
    assert rate_limit_label("seven_day_opus") == "Weekly (Opus only)"
    assert rate_limit_label("overage") == "Extra usage"


def test_quota_popover_skips_duplicate_rebuilds_and_removes_obsolete_windows():
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    from helios.widgets.chat_toolbar import _ContextPopover
    popover = _ContextPopover()
    rebuilds = []
    popover._rebuild_limits_rows = lambda: rebuilds.append(True)
    row = {"provider": "openai", "rateLimitType": "codex_secondary", "usedPercent": 30}
    popover.update_rate_limit(row)
    popover.update_rate_limit(dict(row))
    assert rebuilds == [True]
    popover.update_rate_limit({"provider": "openai", "rateLimitType": "codex_secondary", "removed": True})
    assert rebuilds == [True, True]
    assert popover._rate_limits == {}
