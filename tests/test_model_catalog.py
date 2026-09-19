"""model_catalog — binary scan, family gating, pinned-id cleanup, OpenAI
filtering. All GTK-free (CI runs on python-slim)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helios.backend import model_catalog as mc


# A miniature "claude binary": junk bytes with embedded model ids and alias
# strings, shaped like what `strings`-ing the real 2.1.170 bundle shows.
FAKE_BINARY = (
    b"\x00\x7fELF junk junk claude-fable-5 more junk claude-opus-4-8\x00"
    b'claude-opus-4-7 claude-opus-4 claude-opus-4-0 claude-opus-4-6-fast '
    b'claude-opus-4-5-20251101 claude-opus-4-5 claude-opus-4-1-20250805-v1 '
    b'claude-sonnet-4-6 claude-haiku-4-5-20251001 claude-haiku-4-5 '
    b'claude-mythos-5 claude-code-20250219 claude-instant-1 '
    b'"fable[1m]" "opus[1m]" "sonnet[1m]" "fable" "opus" "mythos" junk'
)

HELP_TEXT = """
  --model <model>   Model for the current session. Provide an alias for the
                    latest model (e.g. 'fable', 'opus', or 'sonnet') or a
                    model's full name (e.g. 'claude-fable-5').
  --name <name>     Set a 'display' name for this session
"""


@pytest.fixture()
def fake_binary(tmp_path: Path) -> Path:
    p = tmp_path / "claude-bin"
    p.write_bytes(FAKE_BINARY)
    return p


def test_scan_finds_ids_and_onem_aliases(fake_binary: Path):
    scan = mc.scan_claude_binary(fake_binary)
    assert "claude-fable-5" in scan["ids"]
    assert "claude-opus-4-8" in scan["ids"]
    assert "claude-haiku-4-5-20251001" in scan["ids"]
    # mythos id is present in the raw scan (gating happens later)
    assert "claude-mythos-5" in scan["ids"]
    assert set(scan["onem"]) == {"fable", "opus", "sonnet"}


def test_scan_handles_chunk_boundaries(tmp_path: Path):
    # Place an id straddling the chunk boundary to exercise the overlap.
    # NUL-terminated like real string-table entries in the bundle.
    pad = b"x" * (mc._SCAN_CHUNK - 10)
    p = tmp_path / "big-bin"
    p.write_bytes(pad + b"claude-opus-4-8" + b"\x00" * 100)
    scan = mc.scan_claude_binary(p)
    assert "claude-opus-4-8" in scan["ids"]


def test_help_alias_parsing():
    aliases = mc.parse_help_aliases(HELP_TEXT)
    assert {"fable", "opus", "sonnet"} <= aliases
    assert "claude" not in aliases


def test_family_gate_excludes_unreleased_families():
    families = mc.family_gate(
        {"fable", "opus", "sonnet", "haiku", "mythos", "code", "instant"},
        onem={"fable", "opus", "sonnet"},
        help_aliases={"fable", "opus", "sonnet"},
    )
    assert families == ["fable", "opus", "sonnet", "haiku"]
    assert "mythos" not in families


def test_family_gate_admits_new_family_via_onem_signal():
    # A future family ships: the binary gains "newfam[1m]" before we ever
    # hardcode it — it must appear without a Helios release.
    families = mc.family_gate(
        {"fable", "newfam"}, onem={"fable", "newfam"}, help_aliases=set()
    )
    assert "newfam" in families


def test_clean_pinned_dedupes_variants():
    ids = [
        "claude-opus-4-8", "claude-opus-4", "claude-opus-4-0",
        "claude-opus-4-6-fast", "claude-opus-4-5-20251101", "claude-opus-4-5",
        "claude-opus-4-1-20250805-v1", "claude-haiku-4-5-20251001",
        "claude-haiku-4-5", "claude-fable-5", "claude-mythos-5",
    ]
    cleaned = mc.clean_pinned_ids(ids, ["fable", "opus", "haiku"])
    got = [p.id for p in cleaned]
    assert "claude-fable-5" in got
    assert "claude-opus-4-8" in got
    # bare -4 dropped in favor of -4-0
    assert "claude-opus-4" not in got and "claude-opus-4-0" in got
    # -fast / -v1 variants dropped
    assert all("fast" not in i and "-v1" not in i for i in got)
    # date twin collapsed onto the short form
    assert "claude-opus-4-5" in got and "claude-opus-4-5-20251101" not in got
    assert "claude-haiku-4-5" in got and "claude-haiku-4-5-20251001" not in got
    # gated family excluded
    assert "claude-mythos-5" not in got
    # ordering: fable family first, then opus newest-first
    assert got[0] == "claude-fable-5"
    opus_ids = [i for i in got if "opus" in i]
    assert opus_ids[0] == "claude-opus-4-8"


def test_build_entries_shape(fake_binary: Path):
    scan = mc.scan_claude_binary(fake_binary)
    entries = mc.build_anthropic_entries(scan, help_aliases={"fable", "opus", "sonnet"})
    ids = [e.id for e in entries]
    labels = {e.id: e.label for e in entries}
    # 1M alias precedes the standard alias for every family that advertises it.
    assert ids[0] == "fable[1m]"
    assert ids[1] == "fable"
    assert "fable" in ids and "haiku" in ids
    # haiku has no [1m] alias in the fixture, so it must not gain a fake one.
    assert "haiku[1m]" not in ids
    assert "" in ids  # the settings-owned default stays selectable
    assert labels["claude-fable-5"] == "Fable 5"
    assert labels["claude-opus-4-8"] == "Opus 4.8"
    # groups present for popover headers
    assert {e.group for e in entries} >= {"Latest", "Fable", "Opus"}


def test_anthropic_entries_cache_roundtrip(tmp_path: Path, monkeypatch, fake_binary: Path):
    from dataclasses import dataclass

    monkeypatch.setattr(mc, "_CATALOG_CACHE", tmp_path / "model-catalog.json")

    @dataclass
    class FakeBinary:
        path: Path

    import helios.backend.claude_binary as cb

    monkeypatch.setattr(cb, "find_claude_binary", lambda: FakeBinary(path=fake_binary))
    monkeypatch.setattr(mc, "fetch_help_aliases", lambda *_a, **_k: {"fable", "opus", "sonnet"})
    orig_scan = mc.scan_claude_binary

    first = mc.anthropic_entries()
    assert first[0].id == "fable[1m]"
    cache = json.loads((tmp_path / "model-catalog.json").read_text())
    assert cache["fingerprint"]["size"] == fake_binary.stat().st_size
    assert cache["anthropic_policy_rev"] == mc._ANTHROPIC_POLICY_REV

    # Second call: cache hit (poison the scanner to prove it isn't called).
    monkeypatch.setattr(mc, "scan_claude_binary", lambda *_: (_ for _ in ()).throw(AssertionError("rescan!")))
    second = mc.anthropic_entries()
    assert [e.id for e in second] == [e.id for e in first]

    # Binary "updated" → fingerprint mismatch → rescan happens.
    fake_binary.write_bytes(FAKE_BINARY + b' claude-opus-4-9 ')
    monkeypatch.setattr(mc, "scan_claude_binary", orig_scan)
    third = mc.anthropic_entries()
    assert "claude-opus-4-9" in [e.id for e in third]


def test_fallback_when_binary_missing(monkeypatch):
    import helios.backend.claude_binary as cb

    def boom():
        raise cb.ClaudeBinaryNotFound("nope")

    monkeypatch.setattr(cb, "find_claude_binary", boom)
    entries = mc.anthropic_entries()
    assert entries == mc.FALLBACK_ANTHROPIC


def test_context_window_reflects_the_selected_alias():
    """UPDATED 2026-08-07 after measuring, not after reasoning.

    This previously asserted `opus` == 200_000, encoding the era when the
    latest opus was 4.x and `[1m]` was the extended variant. Measured against
    claude 2.1.224: `--model opus` resolves to claude-opus-5 reporting
    `modelUsage.contextWindow` 1,000,000, and `--model sonnet` resolves to
    claude-sonnet-5 at 1,000,000 too. The bare alias means "latest in this
    family", and the latest of these families is now 1M — so the old
    assertion made the fallback under-report by 5x.

    `haiku` stays 200k: it was NOT measured (the probe resolved to a different
    model), and the conservative default is the right answer for an
    unmeasured family in a last-resort estimator.
    """

    assert mc.context_window_for("opus[1m]") == 1_000_000
    assert mc.context_window_for("opus") == 1_000_000
    assert mc.context_window_for("fable[1m]") == 1_000_000
    # Still 200k — versioned older ids must not be caught by the alias rule.
    assert mc.context_window_for("claude-opus-4-5") == 200_000
    assert mc.context_window_for("haiku") == 200_000


def test_cached_rows_round_trip_including_1m_and_settings_default():
    entries = mc._entries_from_cache(
        [
            ["fable[1m]", "Fable (1M)", "Latest"],
            ["", "Default from settings", "Other"],
            ["fable", "Fable", "Latest"],
        ]
    )
    assert [entry.id for entry in entries] == ["fable[1m]", "", "fable"]


def test_policy_rev_bump_rebuilds_a_stale_p0_filtered_catalog(tmp_path, monkeypatch):
    """A live install cached its picker rows under policy rev 1, which had the
    1M aliases filtered out. Serving that cache would silently keep them gone,
    so a rev mismatch must invalidate it even when the binary is unchanged."""
    cache = tmp_path / "model-catalog.json"
    monkeypatch.setattr(mc, "_CATALOG_CACHE", cache)
    monkeypatch.setattr(mc, "_binary_fingerprint", lambda _p: {"size": 1})

    import helios.backend.claude_binary as cb

    monkeypatch.setattr(
        cb, "find_claude_binary", lambda: type("B", (), {"path": tmp_path / "claude"})()
    )
    mc._write_json(
        cache,
        {
            "fingerprint": {"size": 1},
            "anthropic_policy_rev": 1,
            "entries": [["fable", "Fable (latest)", "Latest"]],
        },
    )

    assert mc.claude_binary_changed() is True


# ── OpenAI side ────────────────────────────────────────────────────────────

APP_SERVER_MODELS = [
    {
        "id": "gpt-5.6-sol",
        "displayName": "GPT-5.6-Sol",
        "description": "Latest frontier agentic coding model.",
        "hidden": False,
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Lower latency"},
            {"reasoningEffort": "max", "description": "Maximum reasoning"},
            {"reasoningEffort": "ultra", "description": "Automatic delegation"},
        ],
        "defaultReasoningEffort": "low",
        "inputModalities": ["text", "image"],
        "serviceTiers": [
            {"id": "priority", "name": "Fast", "description": "1.5x speed"},
        ],
        "isDefault": True,
    },
    {
        "id": "gpt-5.6-luna",
        "displayName": "GPT-5.6-Luna",
        "hidden": False,
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Lower latency"},
            {"reasoningEffort": "max", "description": "Maximum reasoning"},
        ],
        "defaultReasoningEffort": "medium",
        "inputModalities": ["text", "image"],
        "isDefault": False,
    },
]


def test_openai_dated_label():
    entries = mc.build_openai_entries([("gpt-5.3-2025-12-01", 0)])
    assert entries[0].label == "GPT-5.3 (2025-12-01)"


def test_openai_entry_labels():
    entries = mc.build_openai_entries([("gpt-5.4-mini", 0), ("o4-mini", 0)])
    by_id = {e.id: e for e in entries}
    assert by_id["gpt-5.4-mini"].label == "GPT-5.4 Mini"
    assert by_id["o4-mini"].label == "o4-mini"
    assert all(e.provider == mc.PROVIDER_OPENAI for e in entries)
    assert all(e.group == "OpenAI" for e in entries)


def test_app_server_entries_preserve_capabilities_and_default():
    entries = mc.build_app_server_openai_entries(APP_SERVER_MODELS)
    assert [entry.id for entry in entries] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    sol = entries[0]
    assert sol.is_default is True
    assert sol.default_effort == "low"
    assert [key for key, _description in sol.reasoning_efforts] == [
        "low", "max", "ultra",
    ]
    assert sol.input_modalities == ("text", "image")
    assert sol.service_tiers == (("priority", "Fast", "1.5x speed"),)
    assert mc.preferred_openai_model(entries) == "gpt-5.6-sol"


def test_preferred_openai_model_is_first_agent_ranked_entry():
    entries = mc.build_openai_entries([
        ("gpt-5.5-codex", 0),
        ("gpt-5.6-pro", 0),
    ])
    assert mc.preferred_openai_model(entries) == "gpt-5.5-codex"
    assert mc.preferred_openai_model([]) == ""


def test_astra_preserves_native_default_efforts_and_fast_tier():
    # Shape and capabilities observed from the app server's model/list on 2026-09-19.
    levels = ["low", "medium", "high", "xhigh", "max", "ultra"]
    entries = mc.build_app_server_openai_entries([{
        "id": "gpt-6-astra",
        "model": "gpt-6-astra",
        "displayName": "GPT-6-Astra",
        "supportedReasoningEfforts": [
            {"reasoningEffort": key, "description": key} for key in levels
        ],
        "defaultReasoningEffort": "medium",
        "inputModalities": ["text", "image"],
        "serviceTiers": [{"id": "priority", "name": "Fast", "description": "2x speed, increased usage"}],
        "isDefault": True,
    }])
    assert mc.provider_for(entries[0].id) == mc.PROVIDER_OPENAI
    assert mc.preferred_openai_model(entries) == "gpt-6-astra"
    assert entries[0].default_effort == "medium"
    assert [key for key, _ in entries[0].reasoning_efforts] == levels
    assert entries[0].service_tiers == (("priority", "Fast", "2x speed, increased usage"),)


def test_openai_entries_logged_out_never_starts_app_server(monkeypatch):
    import helios.backend.codex_env as ce

    monkeypatch.setattr(
        ce,
        "fetch_auth_status",
        lambda: ce.CodexAuth(ok=True, logged_in=False, detail="Not logged in"),
    )
    monkeypatch.setattr(
        ce,
        "fetch_app_server_capabilities",
        lambda: (_ for _ in ()).throw(AssertionError("started app server")),
    )

    assert mc.openai_entries() == ([], "not-logged-in")


def test_openai_entries_chatgpt_uses_authoritative_app_server(monkeypatch):
    import helios.backend.codex_env as ce

    auth = ce.CodexAuth(
        ok=True,
        logged_in=True,
        detail="ChatGPT account",
        mode="chatgpt",
    )
    monkeypatch.setattr(
        ce,
        "fetch_app_server_capabilities",
        lambda: ce.CodexAppServerCapabilities(
            tuple(APP_SERVER_MODELS), ("default", "plan")
        ),
    )

    entries, status = mc.openai_entries(auth=auth)

    assert status == "app-server"
    assert [entry.id for entry in entries] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    assert all(entry.provider == mc.PROVIDER_OPENAI for entry in entries)
    assert mc.preferred_openai_model(entries) == "gpt-5.6-sol"


def test_openai_entries_chatgpt_has_curated_failure_fallback(monkeypatch):
    import helios.backend.codex_env as ce

    auth = ce.CodexAuth(
        ok=True,
        logged_in=True,
        detail="ChatGPT account",
        mode="chatgpt",
    )
    monkeypatch.setattr(
        ce,
        "fetch_app_server_capabilities",
        lambda: (_ for _ in ()).throw(ce.CodexAppServerError("old CLI")),
    )

    entries, status = mc.openai_entries(auth=auth)

    assert status == "chatgpt-fallback"
    assert mc.openai_workflow_modes() == ("default",)
    assert [entry.id for entry in entries][:3] == [
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    ]


@pytest.mark.parametrize(
    "app_models",
    [
        [],
        [{"id": "text-embedding-fixture"}],
        [{"id": "gpt-5-hidden-fixture", "hidden": True}],
    ],
)
def test_successful_empty_app_server_catalog_never_uses_chatgpt_fallback(
    monkeypatch,
    app_models,
):
    import helios.backend.codex_env as ce

    auth = ce.CodexAuth(
        ok=True,
        logged_in=True,
        detail="ChatGPT account",
        mode="chatgpt",
    )
    monkeypatch.setattr(
        ce,
        "fetch_app_server_capabilities",
        lambda: ce.CodexAppServerCapabilities(tuple(app_models), ("default",)),
    )

    entries, status = mc.openai_entries(auth=auth)

    assert entries == []
    assert status == "app-server-empty"


@pytest.mark.parametrize("mode", ["apikey", "access_token", "authenticated"])
def test_openai_entries_opaque_live_auth_uses_app_server(monkeypatch, mode: str):
    import helios.backend.codex_env as ce

    auth = ce.CodexAuth(
        ok=True,
        logged_in=True,
        detail="Codex account",
        mode=mode,
    )
    monkeypatch.setattr(
        ce,
        "fetch_app_server_capabilities",
        lambda: ce.CodexAppServerCapabilities(
            tuple(APP_SERVER_MODELS), ("default", "plan")
        ),
    )

    entries, status = mc.openai_entries(auth=auth)

    assert status == "app-server"
    assert [entry.id for entry in entries] == ["gpt-5.6-sol", "gpt-5.6-luna"]


def test_same_mode_account_switch_never_reuses_first_accounts_catalog(monkeypatch):
    """ChatGPT A -> B remains opaque even though both report mode=chatgpt."""
    import helios.backend.codex_env as ce

    auth = ce.CodexAuth(
        ok=True,
        logged_in=True,
        detail="ChatGPT account",
        mode="chatgpt",
    )
    account_models = iter([
        [{"id": "gpt-5.6-sol", "displayName": "Account A"}],
        [{"id": "gpt-5.6-luna", "displayName": "Account B"}],
    ])
    calls = {"app_server": 0}

    def discover():
        calls["app_server"] += 1
        return ce.CodexAppServerCapabilities(
            tuple(next(account_models)), ("default", "plan")
        )

    monkeypatch.setattr(ce, "fetch_app_server_capabilities", discover)

    first, first_status = mc.openai_entries(auth=auth)
    second, second_status = mc.openai_entries(auth=auth)

    assert calls["app_server"] == 2
    assert first_status == second_status == "app-server"
    assert [entry.id for entry in first] == ["gpt-5.6-sol"]
    assert [entry.id for entry in second] == ["gpt-5.6-luna"]


def test_apikey_mode_never_pairs_live_status_with_stale_file_key(monkeypatch):
    """A keyring credential and stale auth.json candidate must never mix."""
    import helios.backend.codex_env as ce

    auth = ce.CodexAuth(
        ok=True,
        logged_in=True,
        detail="OpenAI API key",
        mode="apikey",
    )
    raw_key_reads = {"count": 0}
    direct_fetches = {"count": 0}

    def stale_file_candidate():
        raw_key_reads["count"] += 1
        return "fixture-stale-file-value"

    def direct_fetch(_key):
        direct_fetches["count"] += 1
        raise AssertionError("direct API fallback used")

    # Install the retired seam names as tripwires. openai_entries must not
    # consult either one, even when the live CLI reports generic API-key mode.
    monkeypatch.setattr(ce, "read_api_key", stale_file_candidate, raising=False)
    monkeypatch.setattr(mc, "fetch_openai_models", direct_fetch, raising=False)
    monkeypatch.setattr(
        ce,
        "fetch_app_server_capabilities",
        lambda: (_ for _ in ()).throw(ce.CodexAppServerError("unavailable")),
    )

    entries, status = mc.openai_entries(auth=auth)

    assert entries == []
    assert status == "apikey-catalog-unavailable"
    assert raw_key_reads["count"] == 0
    assert direct_fetches["count"] == 0


# ── cross-provider helpers ─────────────────────────────────────────────────


def test_provider_routing():
    assert mc.provider_for("fable[1m]") == mc.PROVIDER_ANTHROPIC
    assert mc.provider_for("claude-opus-4-8") == mc.PROVIDER_ANTHROPIC
    assert mc.provider_for("") == mc.PROVIDER_ANTHROPIC
    assert mc.provider_for("gpt-5.4-mini") == mc.PROVIDER_OPENAI
    assert mc.provider_for("o4-mini") == mc.PROVIDER_OPENAI
    assert mc.provider_for("codex-mini-latest") == mc.PROVIDER_OPENAI
    assert mc.provider_for("chatgpt-4o-latest") == mc.PROVIDER_OPENAI


def test_context_windows():
    assert mc.context_window_for("fable[1m]") == 1_000_000
    # See test_context_window_reflects_the_selected_alias: measured 1M on
    # claude 2.1.224, where this used to assert 200_000.
    assert mc.context_window_for("opus") == 1_000_000
    assert mc.context_window_for("", default_anthropic="fable[1m]") == 1_000_000
    assert mc.context_window_for("gpt-5.4-mini") == 400_000
    assert mc.context_window_for("gpt-5.6-sol") == 1_050_000
    assert mc.context_window_for("o4-mini") == 200_000
    assert mc.context_window_for("gpt-4.1") == 1_000_000
