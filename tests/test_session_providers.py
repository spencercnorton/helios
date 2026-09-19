"""Fail-closed provider ownership resolution."""

from __future__ import annotations

import json

from helios.backend.conversation_perms import ConversationPermsStore
from helios.backend import session_providers as providers


class _Owners:
    def __init__(self, *owners: str) -> None:
        self.owners = frozenset(owners)

    def providers_for_session(self, _session_id: str) -> frozenset[str]:
        return self.owners


class _BrokenOwners:
    def providers_for_session(self, _session_id: str) -> frozenset[str]:
        raise OSError("settings store unreadable")


def _transcript(tmp_path, session_id: str, *versions: str):
    path = tmp_path / f"{session_id}.jsonl"
    rows = [
        {"sessionId": session_id, "version": version, "type": "assistant"}
        for version in versions
    ]
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_no_evidence_is_unknown():
    resolution = providers.resolve_provider("missing", conversation_store=_Owners())

    assert not resolution.known
    assert not resolution.conflicted
    assert resolution.provider == ""
    assert providers.provider_for("missing") == ""


def test_each_durable_source_can_resolve_one_provider(tmp_path):
    providers.set_provider("from-index", "anthropic")
    assert providers.resolve_provider(
        "from-index", conversation_store=_Owners()
    ).provider == "anthropic"
    assert providers.resolve_provider(
        "from-settings", conversation_store=_Owners("openai")
    ).provider == "openai"
    path = _transcript(tmp_path, "from-transcript", "helios-codex")
    assert providers.resolve_provider(
        "from-transcript", path, conversation_store=_Owners()
    ).provider == "openai"


def test_explicit_anthropic_round_trips_and_forget_returns_unknown():
    providers.set_provider("claude-id", "anthropic")
    providers.reload()
    assert providers.provider_for("claude-id") == "anthropic"

    providers.forget("claude-id")
    providers.reload()
    assert providers.provider_for("claude-id") == ""


def test_explicit_identity_cannot_be_silently_relabelled():
    assert providers.set_provider("stable", "anthropic") is True
    assert providers.set_provider("stable", "openai") is False
    assert providers.provider_for("stable") == "anthropic"


def test_all_agree_is_resolved(tmp_path):
    session_id = "agree"
    providers.set_provider(session_id, "anthropic")
    path = _transcript(tmp_path, session_id, "2.1.17")

    resolution = providers.resolve_provider(
        session_id,
        path,
        conversation_store=_Owners("anthropic"),
    )

    assert resolution.known
    assert resolution.provider == "anthropic"
    assert resolution.sources == ("index", "conversation-settings", "transcript")


def test_any_cross_source_disagreement_is_conflict(tmp_path):
    session_id = "disagree"
    providers.set_provider(session_id, "openai")
    path = _transcript(tmp_path, session_id, "2.1.17")

    resolution = providers.resolve_provider(
        session_id,
        path,
        conversation_store=_Owners("openai"),
    )

    assert resolution.conflicted
    assert not resolution.known
    assert resolution.provider == ""


def test_multiple_or_invalid_conversation_namespaces_conflict():
    both = providers.resolve_provider(
        "collision",
        conversation_store=_Owners("anthropic", "openai"),
    )
    invalid = providers.resolve_provider(
        "invalid",
        conversation_store=_Owners("local-model"),
    )

    assert both.conflicted
    assert invalid.conflicted


def test_invalid_index_claim_conflicts(tmp_path):
    (tmp_path / "session-providers.json").write_text(
        '{"bad": "local-model"}\n',
        encoding="utf-8",
    )
    providers.reload()

    resolution = providers.resolve_provider("bad", conversation_store=_Owners())

    assert resolution.conflicted
    assert resolution.sources == ("index-invalid",)


def test_non_string_index_claim_conflicts(tmp_path):
    (tmp_path / "session-providers.json").write_text(
        '{"bad": 42}\n',
        encoding="utf-8",
    )
    providers.reload()

    resolution = providers.resolve_provider("bad", conversation_store=_Owners())

    assert resolution.conflicted
    assert resolution.sources == ("index-invalid",)


def test_unreadable_conversation_store_conflicts_instead_of_disappearing():
    resolution = providers.resolve_provider(
        "opaque",
        conversation_store=_BrokenOwners(),
    )

    assert resolution.conflicted
    assert resolution.sources == ("conversation-settings-unreadable",)


def test_malformed_or_non_mapping_index_conflicts_for_every_lookup(tmp_path):
    path = tmp_path / "session-providers.json"
    for raw in ("{broken", "[]"):
        path.write_text(raw, encoding="utf-8")
        providers.reload()

        resolution = providers.resolve_provider(
            "otherwise-known",
            conversation_store=_Owners("openai"),
        )

        assert resolution.conflicted
        assert resolution.sources == ("index-unreadable",)


def test_invalid_conversation_record_remains_conflicted_when_index_agrees(tmp_path):
    session_id = "invalid-settings"
    providers.set_provider(session_id, "openai")
    path = tmp_path / "conversation-perms.json"
    path.write_text(
        json.dumps(
            {
                "openai": {
                    session_id: {"permission_mode": "future-mode"},
                }
            }
        ),
        encoding="utf-8",
    )

    resolution = providers.resolve_provider(
        session_id,
        conversation_store=ConversationPermsStore(path),
    )

    assert resolution.conflicted
    assert resolution.sources == ("conversation-settings-conflict",)


def test_corrupt_conversation_namespace_conflicts_for_every_lookup(tmp_path):
    path = tmp_path / "conversation-perms.json"
    path.write_text(json.dumps({"openai": ["unknown-ids"]}), encoding="utf-8")

    resolution = providers.resolve_provider(
        "otherwise-known",
        conversation_store=ConversationPermsStore(path),
    )

    assert resolution.conflicted
    assert resolution.sources == ("conversation-settings-conflict",)


def test_malformed_conversation_file_conflicts_for_every_lookup(tmp_path):
    path = tmp_path / "conversation-perms.json"
    path.write_text("{broken", encoding="utf-8")

    resolution = providers.resolve_provider(
        "otherwise-known",
        conversation_store=ConversationPermsStore(path),
    )

    assert resolution.conflicted
    assert resolution.sources == ("conversation-settings-conflict",)


def test_transcript_mixed_markers_and_mismatched_id_conflict(tmp_path):
    mixed = _transcript(tmp_path, "mixed", "helios-codex", "2.1.17")
    wrong = tmp_path / "wrong.jsonl"
    wrong.write_text(
        json.dumps({"sessionId": "someone-else", "version": "2.1.17"}) + "\n",
        encoding="utf-8",
    )

    assert providers.resolve_provider(
        "mixed", mixed, conversation_store=_Owners()
    ).conflicted
    assert providers.resolve_provider(
        "wrong", wrong, conversation_store=_Owners()
    ).conflicted


def test_arbitrary_or_unowned_version_marker_is_not_claude(tmp_path):
    path = tmp_path / "arbitrary.jsonl"
    path.write_text(
        json.dumps({"sessionId": "arbitrary", "version": "my-app"})
        + "\n"
        + json.dumps({"version": "2.1.17"})
        + "\n",
        encoding="utf-8",
    )

    resolution = providers.resolve_provider(
        "arbitrary", path, conversation_store=_Owners()
    )

    assert not resolution.known
    assert not resolution.conflicted


def test_transcript_resolution_is_read_only(tmp_path):
    path = _transcript(tmp_path, "pure", "helios-codex")
    index = tmp_path / "session-providers.json"

    assert providers.resolve_provider(
        "pure", path, conversation_store=_Owners()
    ).provider == "openai"
    assert not index.exists()


def test_transcript_cache_invalidates_when_file_image_changes(tmp_path):
    session_id = "rewritten"
    path = _transcript(tmp_path, session_id, "2.1.17")
    assert providers.resolve_provider(
        session_id,
        path,
        conversation_store=_Owners(),
    ).provider == "anthropic"

    path = _transcript(tmp_path, session_id, "helios-codex")
    resolution = providers.resolve_provider(
        session_id,
        path,
        conversation_store=_Owners(),
    )

    assert resolution.known
    assert resolution.provider == "openai"


def test_chip_style_never_claims_ownership_without_evidence():
    """The mapping is where a wrong branch silently mislabels a session."""
    assert providers.chip_style(None)[0] == "…"
    assert providers.chip_style(providers.ProviderResolution())[0] == "Unknown"
    assert (
        providers.chip_style(providers.ProviderResolution(conflicted=True))[0]
        == "Conflict"
    )

    known = {
        "openai": ("GPT", "helios-provider-gpt"),
        "openrouter": ("OR", "helios-provider-openrouter"),
        "anthropic": ("Claude", "helios-provider-claude"),
    }
    for provider, (label, css) in known.items():
        resolution = providers.ProviderResolution(
            provider=provider, sources=("index",)
        )
        assert resolution.known, provider
        assert providers.chip_style(resolution)[:2] == (label, css)

    # Each state gets its own label, so no two can be confused on the row.
    labels = [
        providers.chip_style(r)[0]
        for r in (
            None,
            providers.ProviderResolution(),
            providers.ProviderResolution(conflicted=True),
            *(
                providers.ProviderResolution(provider=p, sources=("index",))
                for p in known
            ),
        )
    ]
    assert len(set(labels)) == len(labels)


def test_an_unrecognised_known_provider_never_claims_claude(monkeypatch) -> None:
    """`known` is not "is Anthropic".

    It only means the provider is in _KNOWN_PROVIDERS and unconflicted, so a
    Claude branch reached by FALLTHROUGH asserts Anthropic ownership for any
    known value that is not OpenAI or OpenRouter. Unreachable today -- the set
    holds exactly the three -- so the fourth provider has to be simulated, which
    is precisely the scenario: the day someone adds one, the chip must not
    silently label it Claude. The alternative to this monkeypatch is shipping a
    branch nothing can exercise.
    """
    monkeypatch.setattr(
        providers, "_KNOWN_PROVIDERS",
        frozenset(providers._KNOWN_PROVIDERS | {"some-future-provider"}),
    )
    resolution = providers.ProviderResolution(
        provider="some-future-provider", sources=("index",)
    )
    assert resolution.known, "precondition: the simulated provider must be known"

    label, css, _tooltip = providers.chip_style(resolution)

    assert (label, css) != ("Claude", "helios-provider-claude"), (
        "an unrecognised known provider claimed Anthropic ownership"
    )
