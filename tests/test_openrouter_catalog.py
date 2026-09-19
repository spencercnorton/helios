"""Tests for the OpenRouter model catalog, key store, and /models fetch."""

from __future__ import annotations

import json
import os
import threading
from unittest.mock import patch

import pytest

from helios.backend import model_catalog as mc
from helios.backend.openrouter import catalog as oc
from helios.backend.openrouter import key as ok


# ── provider_for ──────────────────────────────────────────────────────────


class TestProviderFor:
    def test_slash_routes_to_openrouter(self):
        assert mc.provider_for("anthropic/claude-sonnet-4") == mc.PROVIDER_OPENROUTER
        assert mc.provider_for("openai/gpt-5") == mc.PROVIDER_OPENROUTER
        assert mc.provider_for("google/gemini-2.5-pro") == mc.PROVIDER_OPENROUTER

    def test_openai_prefix_still_openai_without_slash(self):
        assert mc.provider_for("gpt-5.6") == mc.PROVIDER_OPENAI
        assert mc.provider_for("o3-mini") == mc.PROVIDER_OPENAI
        assert mc.provider_for("codex-spark") == mc.PROVIDER_OPENAI

    def test_no_slash_no_openai_prefix_is_anthropic(self):
        assert mc.provider_for("fable[1m]") == mc.PROVIDER_ANTHROPIC
        assert mc.provider_for("claude-opus-4-8") == mc.PROVIDER_ANTHROPIC

    def test_empty_is_anthropic(self):
        assert mc.provider_for("") == mc.PROVIDER_ANTHROPIC
        assert mc.provider_for(None) == mc.PROVIDER_ANTHROPIC  # type: ignore[arg-type]

    def test_slash_wins_over_openai_prefix(self):
        # "openai/gpt-5" has a slash → OpenRouter, not OpenAI
        assert mc.provider_for("openai/gpt-5") == mc.PROVIDER_OPENROUTER


# ── context_window_for ────────────────────────────────────────────────────


class TestContextWindow:
    def test_openrouter_uses_catalog(self, tmp_path):
        # Write a cache with a known context length.
        oc.CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        oc.CACHE_PATH.write_text(
            json.dumps({"rows": [{"id": "test/model", "context_length": 123456}]}),
            encoding="utf-8",
        )
        assert mc.context_window_for("test/model") == 123456

    def test_openrouter_unknown_defaults(self):
        assert mc.context_window_for("nonexistent/model") == 200_000


# ── catalog build_entries ──────────────────────────────────────────────────


class TestBuildEntries:
    def test_groups_by_vendor(self):
        rows = [
            {"id": "google/gemini", "name": "Google: Gemini", "context_length": 400000},
            {"id": "qwen/qwen3", "name": "Qwen: Qwen3", "context_length": 200000},
            {"id": "deepseek/deepseek-chat", "name": "DeepSeek: DeepSeek Chat", "context_length": 64000},
        ]
        entries = oc.build_entries(rows)
        assert [e.group for e in entries] == ["Google", "DeepSeek", "Qwen"]

    def test_strips_vendor_prefix_from_label(self):
        rows = [{
            "id": "google/gemini",
            "name": "Google: Gemini",
            "context_length": 400000,
            "supported_parameters": ["tools", "tool_choice"],
        }]
        entries = oc.build_entries(rows)
        assert entries[0].label == "Gemini"

    def test_drops_invalid_rows(self):
        rows = [
            {"id": "valid/model", "name": "Valid", "context_length": 100000},
            {"id": "noslash", "name": "NoSlash", "context_length": 100},
            {"id": "zero/ctx", "name": "Zero", "context_length": 0},
            {"id": "no/ctx", "name": "NoCtx"},
            {"id": "", "name": "Empty"},
            "not-a-dict",
        ]
        entries = oc.build_entries(rows)
        assert len(entries) == 1
        assert entries[0].id == "valid/model"

    def test_vendor_priority_ordering(self):
        rows = [
            {"id": "zzz/last", "name": "Last", "context_length": 100},
            {"id": "anthropic/claude", "name": "Claude", "context_length": 200},
            {"id": "openai/gpt", "name": "GPT", "context_length": 300},
            {"id": "google/gemini", "name": "Gemini", "context_length": 400},
        ]
        entries = oc.build_entries(rows)
        ids = [e.id for e in entries]
        assert ids == ["google/gemini", "zzz/last"]

    def test_provider_set_on_entries(self):
        rows = [{"id": "google/gemini", "name": "Gemini", "context_length": 400000}]
        entries = oc.build_entries(rows)
        assert entries[0].provider == mc.PROVIDER_OPENROUTER

    @pytest.mark.parametrize("vendor", ["openai", "anthropic", "OpenAI", "Anthropic"])
    def test_native_vendors_are_excluded_including_open_models(self, vendor):
        rows = [
            {"id": f"{vendor}/model", "context_length": 200000},
            {"id": f"{vendor}/model:free", "context_length": 200000},
        ]
        assert oc.build_entries(rows) == []

    def test_native_vendor_words_in_other_model_ids_are_not_excluded(self):
        rows = [{"id": "testvendor/openai-compatible", "context_length": 200000}]
        assert [e.id for e in oc.build_entries(rows)] == ["testvendor/openai-compatible"]


# ── key store ──────────────────────────────────────────────────────────────


class TestKeyStore:
    def test_validate_rejects_short(self):
        with pytest.raises(ok.KeyValidationError):
            ok.validate_key("short")

    def test_validate_rejects_interior_whitespace(self):
        with pytest.raises(ok.KeyValidationError):
            ok.validate_key("x" * 8 + " " + "y" * 8)

    def test_validate_strips_surrounding_whitespace(self):
        result = ok.validate_key("  sk-or-" + "x" * 20 + "  ")
        assert result == "sk-or-" + "x" * 20

    def test_load_returns_empty_when_missing(self, tmp_path):
        with patch.object(ok, "KEY_PATH", tmp_path / "nonexistent.key"):
            assert ok.load_key() == ""

    def test_save_and_load_roundtrip(self, tmp_path):
        key_path = tmp_path / "openrouter.key"
        with patch.object(ok, "KEY_PATH", key_path):
            ok.save_key("sk-or-" + "x" * 30)
            assert ok.load_key() == "sk-or-" + "x" * 30
            # File must be owner-only.
            assert oct(key_path.stat().st_mode & 0o777) == "0o600"

    def test_delete_silent_when_missing(self, tmp_path):
        with patch.object(ok, "KEY_PATH", tmp_path / "nonexistent.key"):
            ok.delete_key()  # must not raise


# ── openrouter_entries (catalog + key integration) ────────────────────────


class TestOpenRouterEntries:
    def test_no_key_returns_empty(self, tmp_path):
        with patch.object(ok, "KEY_PATH", tmp_path / "no.key"), \
             patch.object(oc, "CACHE_PATH", tmp_path / "no-cache.json"):
            entries, status = mc.openrouter_entries()
            assert entries == []
            assert status == "no-key"

    def test_fallback_when_no_cache(self, tmp_path):
        with patch.object(ok, "KEY_PATH", tmp_path / "key"), \
             patch.object(ok, "KEY_PATH", tmp_path / "key"), \
             patch("helios.backend.openrouter.key.load_key", return_value="sk-or-" + "x" * 30), \
             patch.object(oc, "CACHE_PATH", tmp_path / "no-cache.json"):
            entries, status = mc.openrouter_entries()
            assert status == "fallback"
            assert len(entries) == len(mc.FALLBACK_OPENROUTER)
            assert all(mc.openrouter_model_selectable(e.id) for e in entries)

    @pytest.mark.parametrize("force", [False, True])
    def test_legacy_cache_excludes_native_vendors_even_when_refresh_fails(
        self, tmp_path, monkeypatch, force,
    ):
        monkeypatch.setattr(oc, "CACHE_PATH", tmp_path / "models.json")
        monkeypatch.setattr(ok, "load_key", lambda: "fixture-key")
        oc._write_cache([
            {"id": "openai/gpt-6-astra", "context_length": 200000},
            {"id": "anthropic/claude-fable-5", "context_length": 200000},
            {"id": "deepseek/model", "context_length": 200000},
        ])

        def fail_refresh():
            raise oc.CatalogError("offline")

        monkeypatch.setattr(oc, "refresh_models", fail_refresh)
        entries, status = mc.openrouter_entries(force=force)
        assert [e.id for e in entries] == ["deepseek/model"]
        assert status == ("cached-stale" if force else "cached")

    def test_fresh_and_cached_model_choices_have_identical_exclusions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(oc, "CACHE_PATH", tmp_path / "models.json")
        rows = [
            {"id": "openai/gpt-6-astra", "context_length": 200000},
            {"id": "anthropic/claude-fable-5", "context_length": 200000},
            {"id": "qwen/model", "context_length": 200000},
        ]
        monkeypatch.setattr(oc, "fetch_model_rows", lambda **_kwargs: rows)
        assert [e.id for e in oc.refresh_models()] == ["qwen/model"]
        assert [e.id for e in oc.cached_models()] == ["qwen/model"]
        # Preserve pricing and history metadata for existing conversations.
        assert oc.context_length_for("openai/gpt-6-astra") == 200000

    def test_preferred_model_never_restores_a_native_provider_row(self):
        entries = [
            mc.ModelEntry("openai/gpt-6-astra", "Astra", "OpenAI"),
            mc.ModelEntry("anthropic/claude-fable-5", "Fable", "Anthropic"),
            mc.ModelEntry("qwen/model", "Qwen", "Qwen"),
        ]
        assert mc.preferred_openrouter_model(entries) == "qwen/model"
        assert mc.preferred_openrouter_model(entries[:2]) == ""


# ── concurrent cache writes (v0.42.2) ─────────────────────────────────────


class TestConcurrentCacheWrite:
    def test_concurrent_writes_do_not_warn_or_leave_tmp(self, tmp_path):
        """A key save fans out to concurrent refreshes (Settings + main
        window). With a shared tmp path, one winner's replace() orphaned the
        loser's chmod/replace ("No such file or directory: …json.tmp")."""
        cache = tmp_path / "openrouter-models.json"
        barrier = threading.Barrier(2)
        real_chmod = os.chmod

        def synced_chmod(path, mode):
            # Both writers must pass chmod before either may replace(), so a
            # shared tmp name would deterministically race here.
            barrier.wait(timeout=10)
            return real_chmod(path, mode)

        warnings: list = []
        with patch.object(oc, "CACHE_PATH", cache), \
             patch.object(oc.os, "chmod", side_effect=synced_chmod), \
             patch.object(oc._log, "warning", side_effect=lambda *a: warnings.append(a)):
            def writer(vendor: str) -> None:
                oc._write_cache([{"id": f"{vendor}/model", "context_length": 4096}])

            threads = [
                threading.Thread(target=writer, args=("aaa",)),
                threading.Thread(target=writer, args=("zzz",)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert warnings == []
        rows = json.loads(cache.read_text(encoding="utf-8")).get("rows")
        assert rows in (
            [{"id": "aaa/model", "context_length": 4096}],
            [{"id": "zzz/model", "context_length": 4096}],
        )
        assert list(tmp_path.glob("*.tmp")) == []


class TestToolCapabilityLabelling:
    """An OpenRouter session is an agent loop, so a model that cannot call
    tools cannot read a file, run a command, or edit anything — it answers in
    prose and never says why. The catalog knows this per row; the picker did
    not show it."""

    @staticmethod
    def _row(**over):
        row = {
            "id": "vendor/model",
            "name": "Vendor: Model",
            "context_length": 128000,
            "supported_parameters": ["tools", "tool_choice"],
        }
        row.update(over)
        return row

    def test_a_tool_capable_model_is_unmarked(self):
        entry = oc.build_entries([self._row()])[0]
        assert entry.label == "Model"
        assert entry.description == "128k context"

    def test_a_model_without_tools_is_marked_in_the_visible_label(self):
        """In the label, not just the description — the description renders as
        a tooltip, and you cannot hover a decision you did not know you were
        making."""
        entry = oc.build_entries([self._row(supported_parameters=["temperature"])])[0]
        assert entry.label == "Model · no tools"
        assert "Cannot call tools" in entry.description
        assert entry.description.startswith("128k context")

    @pytest.mark.parametrize(
        "params",
        [None, [], ["temperature"], "tools", {"tools": True}, 0],
    )
    def test_anything_that_does_not_advertise_tools_fails_closed(self, params):
        """Mislabelling a capable model is cosmetic; the reverse is the silent
        failure this exists to prevent."""
        assert oc.supports_tools({"supported_parameters": params}) is False

    def test_supports_tools_survives_a_malformed_row(self):
        for row in (None, "row", 7, []):
            assert oc.supports_tools(row) is False

    def test_nothing_is_hidden_from_the_picker(self):
        """No allowlist: a model without tools is still a fine choice for a
        conversation, and most of that group are image and audio models that
        have no business running an agent loop anyway."""
        rows = [
            self._row(id="a/one"),
            self._row(id="b/two", supported_parameters=[]),
        ]
        assert len(oc.build_entries(rows)) == 2
