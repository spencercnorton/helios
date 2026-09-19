from __future__ import annotations

import inspect
import types

import pytest

pytest.importorskip("gi")

from helios.backend import model_catalog, project_perms  # noqa: E402
from helios.widgets.settings_dialog import (  # noqa: E402
    PERMISSION_MODES,
    SettingsDialog,
    _router_awaiting_count,
    _router_profile_summary,
    _router_unavailable_subtitle,
)
from helios.widgets import settings_dialog as settings_module  # noqa: E402


def test_global_permission_picker_offers_bypass_as_an_explicit_choice():
    keys = {key for key, _label, _desc in PERMISSION_MODES}
    assert "bypassPermissions" in keys
    assert "default" in keys  # the safe fallback is still offered


def test_permission_picker_copy_is_explicitly_a_safe_conversation_fallback():
    subtitle = SettingsDialog._permission_subtitle(0)

    assert "unconfigured conversations" in subtitle
    assert "Default permissions" in inspect.getsource(settings_module)


def test_permission_policy_surfaces_have_no_future_chat_copy():
    for module in (settings_module, project_perms):
        source = inspect.getsource(module).lower()
        assert "next chat" not in source
        assert "live chat" not in source


def _dialog_shell() -> SettingsDialog:
    return SettingsDialog.__new__(SettingsDialog)


def test_ollama_settings_save_then_check_and_ignore_edited_stale_results(monkeypatch):
    from gi.repository import Adw, Gtk
    from helios.backend.ollama_titles import ModelReadiness

    Adw.init()
    saved, pending = [], []
    fake = types.SimpleNamespace(
        _closed=False,
        _ollama_check_busy=False,
        _ollama_check_generation=0,
        _ollama_url_row=Adw.EntryRow(),
        _ollama_model_row=Adw.EntryRow(),
        _ollama_status_row=Adw.ActionRow(),
        _ollama_check_btn=Gtk.Button(),
        _run_async=lambda work, done: pending.append((work, done)),
    )
    fake._apply_ollama_check = types.MethodType(SettingsDialog._apply_ollama_check, fake)
    fake._ollama_url_row.set_text("http://server:11434/")
    fake._ollama_model_row.set_text("qwen3.6:35b")
    monkeypatch.setattr(settings_module, "ui_state_store", lambda: types.SimpleNamespace(update=lambda **values: saved.append(values)))
    SettingsDialog._on_ollama_check(fake)
    assert saved == [{"ollama_url": "http://server:11434", "ollama_title_model": "qwen3.6:35b"}]
    assert len(pending) == 1 and not fake._ollama_check_btn.get_sensitive()
    SettingsDialog._on_ollama_check(fake)
    assert len(pending) == 1
    SettingsDialog._on_ollama_inputs_changed(fake)
    pending[0][1](ModelReadiness(True, "qwen3.6:35b"))
    assert "Settings changed" in fake._ollama_status_row.get_subtitle()
    assert fake._ollama_check_btn.get_sensitive()
    fake._ollama_url_row.set_text("file:///invalid")
    SettingsDialog._on_ollama_check(fake)
    assert len(saved) == 1 and len(pending) == 1
    assert "http://" in fake._ollama_status_row.get_subtitle()


def test_ollama_check_ready_missing_model_failure_and_closed_delivery():
    from gi.repository import Adw, Gtk
    from helios.backend.ollama_titles import ModelReadiness

    Adw.init()
    fake = types.SimpleNamespace(
        _closed=False, _ollama_check_generation=1, _ollama_check_busy=True,
        _ollama_check_btn=Gtk.Button(), _ollama_status_row=Adw.ActionRow(),
    )
    SettingsDialog._apply_ollama_check(fake, ModelReadiness(True, "model"), 1)
    assert "No text was generated" in fake._ollama_status_row.get_subtitle()
    SettingsDialog._apply_ollama_check(fake, ModelReadiness(False, "missing"), 1)
    assert "not installed" in fake._ollama_status_row.get_subtitle()
    SettingsDialog._apply_ollama_check(fake, OSError("offline"), 1)
    assert "connection check failed" in fake._ollama_status_row.get_subtitle()
    fake._closed = True
    fake._ollama_check_btn.set_sensitive(False)
    SettingsDialog._apply_ollama_check(fake, ModelReadiness(True, "model"), 1)
    assert not fake._ollama_check_btn.get_sensitive()


def test_router_copy_counts_manual_evaluation_as_awaiting():
    profiles = [
        {
            "stage": "manual_evaluation",
            "eligible": False,
            "dispatchable": False,
        },
        {
            "stage": "quarantined",
            "eligible": False,
            "dispatchable": False,
        },
    ]

    assert _router_awaiting_count(profiles) == 2
    assert _router_profile_summary(profiles) == (
        "0 dispatchable · 1 manual evaluation · 1 quarantined"
    )


def test_router_unavailable_copy_preserves_last_authoritative_state():
    assert "last confirmed on" in _router_unavailable_subtitle(True)
    assert "last confirmed off" in _router_unavailable_subtitle(False)
    assert "state is unknown" in _router_unavailable_subtitle(None)


def test_initial_model_choices_preserve_unknown_selected_model():
    dlg = _dialog_shell()

    choices = dlg._initial_model_choices("custom-provider-model")

    assert choices[0] == ("custom-provider-model", "custom-provider-model")
    assert ("fable", "Fable (latest)") in choices


def test_initial_model_choices_keep_a_persisted_1m_model():
    dlg = _dialog_shell()

    choices = dlg._initial_model_choices("fable[1m]")

    # Present exactly once — the fallback list already carries it, so the
    # "selected model is missing" insert must not duplicate the row.
    assert [model_id for model_id, _label in choices].count("fable[1m]") == 1


def test_initial_model_choices_do_not_authorize_persisted_gpt_model():
    dlg = _dialog_shell()

    choices = dlg._initial_model_choices("gpt-5.6-sol")

    assert all(
        model_catalog.provider_for(model_id) != model_catalog.PROVIDER_OPENAI
        for model_id, _label in choices
    )


def test_only_app_server_openai_rows_are_selectable():
    rows = [
        model_catalog.ModelEntry(
            "gpt-5.6-sol",
            "GPT-5.6 Sol",
            "OpenAI",
            provider=model_catalog.PROVIDER_OPENAI,
        )
    ]

    assert SettingsDialog._selectable_openai_models(rows, "app-server") == rows
    assert SettingsDialog._selectable_openai_models(rows, "chatgpt-fallback") == []
    assert SettingsDialog._selectable_openai_models(rows, "app-server-empty") == []


def test_refresh_does_not_reinsert_unverified_selected_gpt_model():
    class ModelRow:
        def set_model(self, model):
            self.model = model

        def set_selected(self, selected):
            self.selected = selected

        def set_subtitle(self, subtitle):
            self.subtitle = subtitle

    fake = types.SimpleNamespace(
        _selected_model_id="gpt-5.6-sol",
        _model_choices=[],
        _model_row=ModelRow(),
    )

    types.MethodType(SettingsDialog._set_model_choices, fake)([("fable", "Fable")])

    assert fake._model_choices == [("fable", "Fable")]
    assert fake._selected_model_id == "fable"


def test_entry_pairs_keep_ids_and_labels():
    entries = [
        model_catalog.ModelEntry("model-a", "Model A", "Group"),
        model_catalog.ModelEntry("model-b", "Model B", "Group"),
    ]

    assert SettingsDialog._entry_pairs(entries) == [
        ("model-a", "Model A"),
        ("model-b", "Model B"),
    ]


def test_live_codex_mcp_snapshot_replaces_existing_rows():
    class FakeGroup:
        def __init__(self) -> None:
            self.rows: list[object] = []

        def add(self, row) -> None:
            self.rows.append(row)

        def remove(self, row) -> None:
            self.rows.remove(row)

    dlg = _dialog_shell()
    dlg._closed = False
    dlg._codex_mcp_group = FakeGroup()
    stale = object()
    dlg._codex_mcp_rows = [stale]
    dlg._codex_mcp_group.rows = [stale]
    dlg._make_codex_mcp_row = lambda server: (server["name"], server["status"])

    dlg.set_codex_mcp_servers(
        [
            {"name": "github", "status": "ready"},
            {"name": "docs", "status": "starting"},
        ]
    )

    assert dlg._codex_mcp_rows == [
        ("github", "ready"),
        ("docs", "starting"),
    ]
    assert dlg._codex_mcp_group.rows == dlg._codex_mcp_rows


def test_key_update_refuses_active_gpt_session_before_login(monkeypatch):
    class BusyLease:
        def __enter__(self):
            raise settings_module.CodexCredentialUpdateInUseError(
                "Close all active GPT sessions before changing OpenAI credentials."
            )

        def __exit__(self, *_args):
            return False

    class BusyHub:
        def credential_update(self):
            return BusyLease()

    login_calls: list[str] = []
    monkeypatch.setattr(settings_module, "get_shared_hub", lambda: BusyHub())
    monkeypatch.setattr(
        settings_module.codex_env,
        "login_with_api_key",
        lambda key: login_calls.append(key),
    )
    credential = "fixture-live-session-credential"

    result = SettingsDialog._save_codex_api_key(credential)

    assert login_calls == []
    assert result == (
        False,
        "Close all active GPT sessions before changing OpenAI credentials.",
        [],
        "active-gpt-sessions",
    )
    assert credential not in repr(result)


def test_key_update_never_reflects_partial_login_diagnostics(monkeypatch):
    class Lease:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class Hub:
        def credential_update(self):
            return Lease()

    credential = "fixture-sensitive-credential"
    monkeypatch.setattr(settings_module, "get_shared_hub", lambda: Hub())
    monkeypatch.setattr(
        settings_module.codex_env,
        "login_with_api_key",
        lambda _key: (False, "server rejected fixture-sens…tial"),
    )

    result = SettingsDialog._save_codex_api_key(credential)

    assert result == (
        False,
        "Codex rejected the credential. Check the key and try again.",
        [],
        "credential-update-failed",
    )
    assert "fixture-sens" not in repr(result)


def test_successful_login_remains_success_when_catalog_discovery_raises(monkeypatch):
    class Lease:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class Hub:
        def credential_update(self):
            return Lease()

    monkeypatch.setattr(settings_module, "get_shared_hub", lambda: Hub())
    monkeypatch.setattr(
        settings_module.codex_env,
        "login_with_api_key",
        lambda _key: (True, "safe success"),
    )
    monkeypatch.setattr(
        settings_module.model_catalog,
        "openai_entries",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("discovery failed")),
    )

    result = SettingsDialog._save_codex_api_key("fixture-credential")

    assert result == (
        True,
        "Key saved by Codex's configured credential store.",
        [],
        "credential-update-catalog-unavailable",
    )


def test_successful_key_update_with_empty_catalog_clears_old_gpt_rows():
    calls: list[tuple] = []

    class Row:
        def set_sensitive(self, value):
            calls.append(("sensitive", value))

        def set_text(self, value):
            calls.append(("key-text", value))

    class StatusRow:
        def set_title(self, value):
            calls.append(("title", value))

        def set_subtitle(self, value):
            calls.append(("subtitle", value))

    fake = types.SimpleNamespace(
        _closed=False,
        _key_row=Row(),
        _codex_status_row=StatusRow(),
        _model_choices=[("fable", "Fable"), ("gpt-5-old", "Prior account GPT")],
        _selected_model_id="fable",
        _set_codex_icon=lambda *args: calls.append(("icon", *args)),
    )
    fake._replace_openai_choices = types.MethodType(
        SettingsDialog._replace_openai_choices,
        fake,
    )
    fake._set_model_choices = lambda choices: calls.append(("choices", choices))
    fake._entry_pairs = lambda entries: [(entry.id, entry.label) for entry in entries]

    types.MethodType(SettingsDialog._apply_key_result, fake)((
        True,
        "Key saved by Codex's configured credential store.",
        [],
        "apikey-catalog-unavailable",
    ))

    assert ("choices", [("fable", "Fable")]) in calls
    assert all("gpt-5-old" not in repr(call) for call in calls if call[0] == "choices")


def test_fallback_key_result_is_informational_and_never_activates_models():
    calls: list[tuple] = []
    replaced: list[list[model_catalog.ModelEntry]] = []

    class Row:
        def set_sensitive(self, value):
            calls.append(("sensitive", value))

        def set_text(self, value):
            calls.append(("key-text", value))

    class StatusRow:
        def set_title(self, value):
            calls.append(("title", value))

        def set_subtitle(self, value):
            calls.append(("subtitle", value))

    fallback = [
        model_catalog.ModelEntry(
            "gpt-5.6-sol",
            "GPT-5.6 Sol",
            "OpenAI",
            provider=model_catalog.PROVIDER_OPENAI,
            is_default=True,
        )
    ]
    fake = types.SimpleNamespace(
        _closed=False,
        _key_row=Row(),
        _codex_status_row=StatusRow(),
        _replace_openai_choices=lambda models: replaced.append(models),
        _set_codex_icon=lambda *args: calls.append(("icon", *args)),
    )

    types.MethodType(SettingsDialog._apply_key_result, fake)((
        True,
        "Key saved by Codex's configured credential store.",
        fallback,
        "chatgpt-fallback",
    ))

    assert replaced == [[]]
    rendered = " ".join(str(value) for call in calls for value in call[1:])
    assert "informational only" in rendered.lower()
    assert "no GPT models were activated" in rendered
    assert "models available" not in rendered


def test_catalog_fallback_ui_is_unverified_without_default_or_picker_rows():
    choices: list[list[tuple[str, str]]] = []

    class RecordingRow:
        def __init__(self) -> None:
            self.title = ""
            self.subtitle = ""

        def set_title(self, value):
            self.title = value

        def set_subtitle(self, value):
            self.subtitle = value

    status_row = RecordingRow()
    catalog_row = RecordingRow()
    anthropic = [model_catalog.ModelEntry("fable", "Fable", "Anthropic")]
    fallback = [
        model_catalog.ModelEntry(
            "gpt-5.6-sol",
            "GPT-5.6 Sol",
            "OpenAI",
            provider=model_catalog.PROVIDER_OPENAI,
            reasoning_efforts=(("high", "High"),),
            service_tiers=(("fast", "Fast", "Priority"),),
            is_default=True,
        )
    ]
    fake = types.SimpleNamespace(
        _closed=False,
        _codex_update_running=False,
        _codex_generation=3,
        _set_model_choices=lambda value: choices.append(value),
        _entry_pairs=lambda entries: [(entry.id, entry.label) for entry in entries],
        _codex_status_row=status_row,
        _codex_catalog_row=catalog_row,
        _set_codex_icon=lambda *_args: None,
    )
    auth = types.SimpleNamespace(
        ok=True,
        logged_in=True,
        detail="Signed in with ChatGPT",
    )

    types.MethodType(SettingsDialog._apply_codex, fake)(
        (auth, "codex-test", anthropic, fallback, "chatgpt-fallback"),
        generation=3,
    )

    assert choices == [[("fable", "Fable")]]
    assert "informational" in status_row.subtitle.lower()
    assert "unverified" in status_row.subtitle.lower()
    assert "models available" not in status_row.subtitle
    assert "Informational fallback" in catalog_row.subtitle
    assert "unverified" in catalog_row.subtitle.lower()
    assert "not selectable" in catalog_row.subtitle
    assert "Default:" not in catalog_row.subtitle
    assert "Reasoning:" not in catalog_row.subtitle
    assert "Fast tier" not in catalog_row.subtitle


def test_failed_login_attempt_clears_prior_gpt_rows_fail_closed():
    choices: list[list[tuple[str, str]]] = []
    fake = types.SimpleNamespace(
        _closed=False,
        _key_row=types.SimpleNamespace(set_sensitive=lambda _value: None),
        _codex_status_row=types.SimpleNamespace(
            set_title=lambda _value: None,
            set_subtitle=lambda _value: None,
        ),
        _model_choices=[("fable", "Fable"), ("gpt-5-old", "Old GPT")],
        _selected_model_id="fable",
        _set_codex_icon=lambda *_args: None,
        _set_model_choices=lambda value: choices.append(value),
        _entry_pairs=lambda entries: [(entry.id, entry.label) for entry in entries],
    )
    fake._replace_openai_choices = types.MethodType(
        SettingsDialog._replace_openai_choices,
        fake,
    )

    types.MethodType(SettingsDialog._apply_key_result, fake)((
        False,
        "Codex rejected the credential. Check the key and try again.",
        [],
        "credential-update-failed",
    ))

    assert choices == [[("fable", "Fable")]]


def test_closed_dialog_still_notifies_app_once_after_successful_key_update():
    notifications: list[str] = []
    fake = types.SimpleNamespace(
        _closed=True,
        _codex_update_running=True,
        _codex_generation=7,
        _codex_credentials_changed=lambda: notifications.append("changed"),
        _apply_key_result=lambda _result: pytest.fail("closed dialog touched UI"),
    )

    result = (
        True,
        "Key saved by Codex's configured credential store.",
        [],
        "apikey-catalog-unavailable",
    )
    finished = types.MethodType(SettingsDialog._finish_key_update, fake)(result)

    assert finished is False
    assert notifications == ["changed"]
    assert fake._codex_generation == 8
    assert fake._codex_update_running is False


def test_failed_login_attempt_conservatively_notifies_app_but_busy_refusal_does_not():
    notifications: list[str] = []
    fake = types.SimpleNamespace(
        _closed=True,
        _codex_update_running=True,
        _codex_generation=1,
        _codex_credentials_changed=lambda: notifications.append("changed"),
        _apply_key_result=lambda _result: pytest.fail("closed dialog touched UI"),
    )
    finish = types.MethodType(SettingsDialog._finish_key_update, fake)

    finish((False, "safe failure", [], "credential-update-failed"))
    finish((False, "stop sessions", [], "active-gpt-sessions"))

    assert notifications == ["changed"]


def test_old_codex_loader_result_is_rejected_after_key_update():
    fake = types.SimpleNamespace(
        _closed=False,
        _codex_update_running=False,
        _codex_generation=4,
        _set_model_choices=lambda _choices: pytest.fail("stale picker applied"),
    )
    stale = (
        object(),
        "codex-test",
        [],
        [],
        "not-logged-in",
    )

    types.MethodType(SettingsDialog._apply_codex, fake)(stale, generation=3)
