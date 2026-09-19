import json
import stat

from helios.backend import conversation_perms, session_providers
from helios.backend.conversation_perms import (
    ConversationExecutionSettings,
    ConversationPermsStore,
)


def test_round_trip_is_namespaced_by_provider(tmp_path):
    path = tmp_path / "conversation-perms.json"
    perms = ConversationPermsStore(path)

    assert perms.get("anthropic", "shared-id") == ""
    assert perms.get("anthropic", "shared-id", "default") == "default"
    assert perms.set("anthropic", "shared-id", "plan")
    assert perms.set("openai", "shared-id", "auto")

    reloaded = ConversationPermsStore(path)
    assert reloaded.get("anthropic", "shared-id") == "plan"
    assert reloaded.get("openai", "shared-id") == "auto"
    assert reloaded.get_settings("anthropic", "shared-id") == (
        ConversationExecutionSettings(permission_mode="plan")
    )


def test_record_round_trip_includes_optional_effort(tmp_path):
    path = tmp_path / "conversation-perms.json"
    perms = ConversationPermsStore(path)

    assert perms.set("openai", "thread", "default", effort_key="xhigh")
    assert perms.get_effort("openai", "thread") == "xhigh"
    assert perms.get_settings("openai", "thread") == ConversationExecutionSettings(
        permission_mode="default",
        effort_key="xhigh",
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "openai": {
            "thread": {
                "permission_mode": "default",
                "effort_key": "xhigh",
            }
        }
    }


def test_workflow_round_trip_preserves_other_execution_axes(tmp_path):
    path = tmp_path / "conversation-perms.json"
    perms = ConversationPermsStore(path)

    assert perms.set(
        "openai",
        "thread",
        "auto",
        effort_key="high",
        workflow_mode="plan",
    )
    assert perms.get_workflow("openai", "thread") == "plan"
    assert perms.set_workflow("openai", "thread", "default")
    assert perms.get_settings("openai", "thread") == ConversationExecutionSettings(
        permission_mode="auto",
        effort_key="high",
        workflow_mode="default",
    )
    assert not perms.set_workflow("openai", "thread", "future")


def test_old_record_defaults_to_default_workflow(tmp_path):
    path = tmp_path / "conversation-perms.json"
    path.write_text(
        json.dumps({"openai": {"thread": {"permission_mode": "plan"}}}),
        encoding="utf-8",
    )

    perms = ConversationPermsStore(path)
    assert perms.get_workflow("openai", "thread") == "default"


def test_permission_only_update_preserves_effort_and_explicit_empty_clears_it(
    tmp_path,
):
    perms = ConversationPermsStore(tmp_path / "conversation-perms.json")
    assert perms.set("openai", "thread", "default", effort_key="high")

    assert perms.set("openai", "thread", "plan")
    assert perms.get_settings("openai", "thread") == ConversationExecutionSettings(
        permission_mode="plan",
        effort_key="high",
    )

    assert perms.set("openai", "thread", "plan", effort_key="")
    assert perms.get_effort("openai", "thread", "medium") == "medium"


def test_set_effort_retains_permission_or_requires_one_for_new_record(tmp_path):
    perms = ConversationPermsStore(tmp_path / "conversation-perms.json")

    assert not perms.set_effort("openai", "new-thread", "high")
    assert perms.set_effort(
        "openai",
        "new-thread",
        "high",
        permission_mode="dontAsk",
    )
    assert perms.set_effort("openai", "new-thread", "xhigh")
    assert perms.get_settings("openai", "new-thread") == (
        ConversationExecutionSettings("dontAsk", "xhigh")
    )


def test_provider_is_normalized_but_native_id_remains_case_sensitive(tmp_path):
    perms = ConversationPermsStore(tmp_path / "conversation-perms.json")

    assert perms.set(" OpenAI ", " Thread-A ", "auto")
    assert perms.get("openai", "Thread-A") == "auto"
    assert perms.get("OPENAI", "thread-a") == ""


def test_provider_reverse_lookup_requires_one_unambiguous_namespace(tmp_path):
    perms = ConversationPermsStore(tmp_path / "conversation-perms.json")
    assert perms.set("openai", "thread", "auto")
    assert perms.provider_for_session("thread") == "openai"

    assert perms.set("anthropic", "thread", "plan")
    assert perms.provider_for_session("thread") == ""


def test_session_provider_index_recovers_from_conversation_record(tmp_path):
    conversation_perms.reload()
    session_providers.reload()
    assert conversation_perms.store().set("openai", "thread-recover", "default")
    assert not (tmp_path / "session-providers.json").exists()

    assert session_providers.provider_for("thread-recover") == "openai"

    session_providers.reload()
    assert session_providers.provider_for("thread-recover") == "openai"


def test_invalid_keys_and_modes_are_ignored(tmp_path):
    path = tmp_path / "conversation-perms.json"
    perms = ConversationPermsStore(path)

    assert not perms.set("", "session", "plan")
    assert not perms.set("openai", "", "plan")
    assert not perms.set("openai", "session", "future-mode")
    assert not path.exists()


def test_delete_persists_and_prunes_empty_provider(tmp_path):
    path = tmp_path / "conversation-perms.json"
    perms = ConversationPermsStore(path)
    assert perms.set("anthropic", "session", "dontAsk")

    assert perms.delete("anthropic", "session")
    assert not perms.delete("anthropic", "session")
    assert perms.get("anthropic", "session", "fallback") == "fallback"
    assert json.loads(path.read_text(encoding="utf-8")) == {}

    assert ConversationPermsStore(path).get("anthropic", "session") == ""


def test_malformed_file_is_safe_and_next_write_recovers_it(tmp_path):
    path = tmp_path / "conversation-perms.json"
    path.write_text("{broken", encoding="utf-8")

    perms = ConversationPermsStore(path)
    assert perms.get("anthropic", "session", "default") == "default"
    assert perms.set("anthropic", "session", "acceptEdits")

    assert ConversationPermsStore(path).get("anthropic", "session") == "acceptEdits"


def test_load_filters_bad_shapes_and_unknown_modes(tmp_path):
    path = tmp_path / "conversation-perms.json"
    path.write_text(
        json.dumps(
            {
                "anthropic": {
                    # Bare strings are accepted as the initial development schema.
                    "good": "plan",
                    "unknown": "future-mode",
                    "not-a-mode-string": 42,
                    "": "auto",
                    "modern": {
                        "permission_mode": "auto",
                        "effort_key": "ultracode",
                        "future_field": True,
                    },
                    "bad-record": {"permission_mode": "future-mode"},
                    "bad-effort": {"permission_mode": "default", "effort_key": 42},
                },
                "openai": ["not", "a", "mapping"],
                "": {"session": "auto"},
            }
        ),
        encoding="utf-8",
    )

    perms = ConversationPermsStore(path)
    assert perms.get("anthropic", "good") == "plan"
    assert perms.get("anthropic", "unknown") == ""
    # Stored under the retired key; reads back as its replacement so a
    # conversation saved before v0.50 does not silently drop to the slider's
    # "high" fallback when it is reopened.
    assert perms.get_effort("anthropic", "modern") == "max"
    assert perms.get("anthropic", "bad-record") == ""
    assert perms.get_effort("anthropic", "bad-effort") == ""
    assert perms.get("openai", "session") == ""


def test_default_path_tracks_state_dir(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"

    monkeypatch.setenv("HELIOS_STATE_DIR", str(first))
    first_store = ConversationPermsStore()
    monkeypatch.setenv("HELIOS_STATE_DIR", str(second))
    second_store = ConversationPermsStore()

    assert first_store.path == first / "conversation-perms.json"
    assert second_store.path == second / "conversation-perms.json"


def test_atomic_write_is_owner_only(tmp_path):
    state = tmp_path / "state"
    path = state / "conversation-perms.json"
    perms = ConversationPermsStore(path)

    assert perms.set("openai", "thread", "plan")

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(state.glob(".conversation-perms.json.*.tmp"))


def test_failed_replace_preserves_durable_and_in_memory_value(monkeypatch, tmp_path):
    path = tmp_path / "conversation-perms.json"
    perms = ConversationPermsStore(path)
    assert perms.set("openai", "thread", "plan")

    def fail_replace(_source, _destination):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(conversation_perms.os, "replace", fail_replace)
    assert not perms.set("openai", "thread", "bypassPermissions")
    assert perms.get("openai", "thread") == "plan"
    assert ConversationPermsStore(path).get("openai", "thread") == "plan"
    assert not list(tmp_path.glob(".conversation-perms.json.*.tmp"))


def test_failed_delete_preserves_value(monkeypatch, tmp_path):
    path = tmp_path / "conversation-perms.json"
    perms = ConversationPermsStore(path)
    assert perms.set("anthropic", "session", "auto")

    monkeypatch.setattr(
        conversation_perms.os,
        "replace",
        lambda _source, _destination: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert not perms.delete("anthropic", "session")
    assert perms.get("anthropic", "session") == "auto"


def test_retired_manual_mode_narrows_instead_of_disappearing(tmp_path, monkeypatch):
    """A v0.52.0 conversation saved as "manual" must not lose its override.

    Dropping it would fall through to the global default in
    MainWindow._execution_settings_for_spawn, so a conversation the user pinned
    to a *gated* mode could reopen under a confirmed global Bypass — a widening,
    and for Claude an ungated one. It must narrow to the safe fallback instead.

    Bypass itself is NOT retired: a conversation explicitly pinned to it keeps
    that choice (the provider gate, not this table, is what keeps it Claude-only).
    """
    from helios.backend.conversation_perms import (
        canonical_permission_mode,
        _settings_from_json,
    )
    from helios.backend.project_perms import SAFE_FALLBACK_MODE

    settings = _settings_from_json({"permission_mode": "manual", "effort_key": "high"})

    assert settings is not None, "the override must survive, not be dropped"
    assert settings.permission_mode == SAFE_FALLBACK_MODE
    assert settings.effort_key == "high"

    assert canonical_permission_mode("manual") == SAFE_FALLBACK_MODE
    assert canonical_permission_mode("bypassPermissions") == "bypassPermissions"
    assert canonical_permission_mode("nonsense") == ""
