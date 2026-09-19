"""MainWindow model-catalog worker coalescing regressions."""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios import main_window as mw  # noqa: E402
from helios.backend import model_catalog  # noqa: E402
from helios.backend.session_router import ChatTarget  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


class _Toggle:
    def __init__(self, label=None):
        self.label = label
        self.active = False
        self.sensitive = True
        self.tooltip = ""

    def set_group(self, _other):
        return None

    def set_sensitive(self, value):
        self.sensitive = value

    def set_tooltip_text(self, value):
        self.tooltip = value

    def set_active(self, value):
        self.active = value

    def connect(self, *_args):
        return 1


class _Box:
    def __init__(self, **_kwargs):
        self.children = []

    def add_css_class(self, _value):
        return None

    def set_valign(self, _value):
        return None

    def append(self, child):
        self.children.append(child)


def test_persisted_gpt_does_not_enable_toggle_before_catalog(monkeypatch):
    monkeypatch.setattr(mw.Gtk, "ToggleButton", _Toggle)
    monkeypatch.setattr(mw.Gtk, "Box", _Box)
    fake = types.SimpleNamespace(
        _model="gpt-5-persisted",
        _provider_guard=False,
        _on_provider_toggled=lambda *_args: None,
    )

    MainWindow._build_provider_toggle(fake)

    assert fake._gpt_toggle.active is True
    assert fake._gpt_toggle.sensitive is False
    assert fake._gpt_toggle.tooltip == "Checking OpenAI model availability…"


def test_persisted_gpt_does_not_enable_explicit_new_chat_action_before_catalog():
    fake = types.SimpleNamespace(
        _model="gpt-5-persisted",
        _openai_catalog_authoritative=False,
        _openai_entries=[],
        _new_gpt_btn=_Toggle(),
    )

    MainWindow._sync_new_chat_actions(fake)

    assert fake._new_gpt_btn.sensitive is False


def test_authoritative_catalog_reenables_valid_persisted_gpt():
    entry = model_catalog.ModelEntry(
        "gpt-5-current",
        "GPT Current",
        "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )
    toggle = _Toggle()
    fake = types.SimpleNamespace(
        _catalog_refresh_running=True,
        _catalog_force_pending=False,
        _destroyed=False,
        _model=entry.id,
        _chat_toolbar=types.SimpleNamespace(set_choices=lambda _entries: None),
        _gpt_toggle=toggle,
        _normalize_openai_model_memory=lambda: None,
        _sync_effort_sensitivity=lambda: None,
        _sync_execution_control=lambda: None,
        _sync_new_chat_actions=lambda: None,
    )

    applied = types.MethodType(MainWindow._apply_model_catalog, fake)(
        [entry],
        True,
    )

    assert applied is False
    assert fake._model == entry.id
    assert fake._openai_catalog_authoritative is True
    assert fake._openai_entries == [entry]
    assert toggle.sensitive is True
    assert toggle.tooltip == "Chat with GPT (OpenAI)"


def test_chatgpt_fallback_rows_are_not_published_by_catalog_worker(monkeypatch):
    workers: list[object] = []
    idles: list[tuple[object, tuple]] = []
    delivered: list[tuple[list[str], bool, tuple[str, ...]]] = []
    claude = model_catalog.ModelEntry("fable", "Fable", "Latest")
    fallback = model_catalog.ModelEntry(
        "gpt-fallback",
        "GPT Fallback",
        "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target
            workers.append(self)

        def start(self):
            return None

    monkeypatch.setattr(mw.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        mw.GLib,
        "idle_add",
        lambda callback, *args: idles.append((callback, args)) or len(idles),
    )
    monkeypatch.setattr(
        "helios.backend.claude_binary.supports_effort_flag",
        lambda: True,
    )
    monkeypatch.setattr(
        model_catalog,
        "anthropic_entries",
        lambda **_kwargs: [claude],
    )
    monkeypatch.setattr(
        model_catalog,
        "openai_entries",
        lambda **_kwargs: ([fallback], "chatgpt-fallback"),
    )
    fake = types.SimpleNamespace(
        _catalog_refresh_running=False,
        _catalog_force_pending=False,
        _apply_model_catalog=lambda entries, authoritative, workflows: delivered.append(
            ([entry.id for entry in entries], authoritative, workflows)
        ),
    )

    MainWindow._refresh_model_catalog(fake)
    workers.pop().target()
    callback, args = idles.pop()
    callback(*args)

    assert delivered == [([claude.id], False, ("default",))]


def test_non_authoritative_fallback_cannot_enable_or_authorize_gpt():
    selected: list[tuple[str, bool]] = []
    choices: list[list[str]] = []
    claude = model_catalog.ModelEntry("fable", "Fable", "Latest")
    fallback = model_catalog.ModelEntry(
        "gpt-fallback",
        "GPT Fallback",
        "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )
    gpt_toggle = _Toggle()
    new_gpt = _Toggle()
    fake = types.SimpleNamespace(
        _catalog_refresh_running=True,
        _catalog_force_pending=False,
        _destroyed=False,
        _model=fallback.id,
        _provider_models={model_catalog.PROVIDER_ANTHROPIC: claude.id},
        _chat_toolbar=types.SimpleNamespace(
            set_choices=lambda entries: choices.append([entry.id for entry in entries])
        ),
        _gpt_toggle=gpt_toggle,
        _new_gpt_btn=new_gpt,
        _normalize_openai_model_memory=lambda: None,
        _sync_effort_sensitivity=lambda: None,
        _sync_execution_control=lambda: None,
        _selected_provider=lambda: model_catalog.PROVIDER_OPENAI,
        _apply_model_choice=lambda model, quiet=False: selected.append((model, quiet)),
        _toast=lambda _message: pytest.fail("catalogued Claude fallback ignored"),
    )
    fake._sync_new_chat_actions = types.MethodType(
        MainWindow._sync_new_chat_actions,
        fake,
    )

    applied = types.MethodType(MainWindow._apply_model_catalog, fake)(
        [claude, fallback],
        False,
    )
    fake._valid_anthropic_fallback_model = types.MethodType(
        MainWindow._valid_anthropic_fallback_model,
        fake,
    )
    allowed = types.MethodType(MainWindow._guard_openai_driver_activation, fake)()

    assert applied is False
    assert choices == [[claude.id]]
    assert fake._openai_catalog_authoritative is False
    assert fake._openai_entries == []
    assert gpt_toggle.sensitive is False
    assert new_gpt.sensitive is False
    assert selected == [(claude.id, True)]
    assert allowed is True  # only after choosing the safe Claude fallback


def test_non_authoritative_fallback_rows_cannot_reuse_or_spawn_gpt():
    fallback = model_catalog.ModelEntry(
        "gpt-fallback",
        "GPT Fallback",
        "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )
    messages: list[str] = []
    fake = types.SimpleNamespace(
        _model=fallback.id,
        _openai_catalog_authoritative=False,
        # Defense in depth: even an accidentally retained fallback row cannot
        # cross the authority bit and reach the existing accepting driver.
        _openai_entries=[fallback],
        _catalog_entries_by_id={},
        _provider_models={model_catalog.PROVIDER_OPENAI: fallback.id},
        _driver=types.SimpleNamespace(is_accepting_input=True),
        _next_chat=ChatTarget(
            project=types.SimpleNamespace(cwd="/repo", read_only=False)
        ),
        _selected_provider=lambda: model_catalog.PROVIDER_OPENAI,
        _toast=messages.append,
    )
    fake._valid_anthropic_fallback_model = types.MethodType(
        MainWindow._valid_anthropic_fallback_model,
        fake,
    )
    fake._guard_openai_driver_activation = types.MethodType(
        MainWindow._guard_openai_driver_activation,
        fake,
    )

    ready = types.MethodType(MainWindow._ensure_driver, fake)()

    assert ready is False
    assert messages == [
        "No OpenAI models available — add a key in Settings → Providers."
    ]


def test_ensure_driver_refuses_precatalog_persisted_gpt_before_reuse():
    messages: list[str] = []
    fake = types.SimpleNamespace(
        _model="gpt-5-persisted",
        _openai_catalog_authoritative=False,
        _openai_entries=[],
        _catalog_entries_by_id={},
        _provider_models={model_catalog.PROVIDER_OPENAI: "gpt-5-persisted"},
        _driver=types.SimpleNamespace(is_accepting_input=True),
        _next_chat=ChatTarget(
            project=types.SimpleNamespace(cwd="/repo", read_only=False)
        ),
        _selected_provider=lambda: model_catalog.PROVIDER_OPENAI,
        _toast=messages.append,
    )
    fake._valid_anthropic_fallback_model = types.MethodType(
        MainWindow._valid_anthropic_fallback_model,
        fake,
    )
    fake._guard_openai_driver_activation = types.MethodType(
        MainWindow._guard_openai_driver_activation,
        fake,
    )

    ready = types.MethodType(MainWindow._ensure_driver, fake)()

    assert ready is False
    assert messages == [
        "No OpenAI models available — add a key in Settings → Providers."
    ]


def test_invalid_programmatic_gpt_model_falls_back_to_catalogued_claude():
    selected: list[tuple[str, bool]] = []
    claude = model_catalog.ModelEntry("fable", "Fable", "Latest")
    current_gpt = model_catalog.ModelEntry(
        "gpt-5-current",
        "GPT Current",
        "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )
    fake = types.SimpleNamespace(
        _model="gpt-5-retired",
        _openai_catalog_authoritative=True,
        _openai_entries=[current_gpt],
        _catalog_entries_by_id={claude.id: claude, current_gpt.id: current_gpt},
        _provider_models={
            model_catalog.PROVIDER_ANTHROPIC: claude.id,
            model_catalog.PROVIDER_OPENAI: "gpt-5-retired",
        },
        _selected_provider=lambda: model_catalog.PROVIDER_OPENAI,
        _apply_model_choice=lambda model, quiet=False: selected.append((model, quiet)),
        _toast=lambda _message: pytest.fail("valid Claude fallback was ignored"),
    )
    fake._valid_anthropic_fallback_model = types.MethodType(
        MainWindow._valid_anthropic_fallback_model,
        fake,
    )

    allowed = types.MethodType(MainWindow._guard_openai_driver_activation, fake)()

    assert allowed is True
    assert selected == [("fable", True)]


def test_exact_catalogued_gpt_model_passes_driver_guard():
    entry = model_catalog.ModelEntry(
        "gpt-5-current",
        "GPT Current",
        "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )
    fake = types.SimpleNamespace(
        _model=entry.id,
        _openai_catalog_authoritative=True,
        _openai_entries=[entry],
        _selected_provider=lambda: model_catalog.PROVIDER_OPENAI,
        _apply_model_choice=lambda *_args, **_kwargs: pytest.fail("valid GPT changed"),
        _toast=lambda _message: pytest.fail("valid GPT rejected"),
    )

    allowed = types.MethodType(MainWindow._guard_openai_driver_activation, fake)()

    assert allowed is True


def test_force_during_inflight_discards_old_result_and_reruns(monkeypatch):
    workers: list[object] = []
    idles: list[tuple[object, tuple]] = []
    applied: list[list[str]] = []
    discovery_forces: list[tuple[str, bool]] = []

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target
            workers.append(self)

        def start(self):
            return None

    monkeypatch.setattr(mw.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        mw.GLib,
        "idle_add",
        lambda callback, *args: idles.append((callback, args)) or len(idles),
    )

    def anthropic_entries(*, force=False):
        discovery_forces.append(("anthropic", force))
        return [
            model_catalog.ModelEntry(
                f"claude-{'forced' if force else 'old'}",
                "Claude",
                "Latest",
            )
        ]

    def openai_entries(*, force=False):
        discovery_forces.append(("openai", force))
        if force:
            raise RuntimeError("new account catalog unavailable")
        return [], "not-logged-in"

    monkeypatch.setattr(model_catalog, "anthropic_entries", anthropic_entries)
    monkeypatch.setattr(model_catalog, "openai_entries", openai_entries)
    monkeypatch.setattr(
        "helios.backend.claude_binary.supports_effort_flag",
        lambda: True,
    )

    fake = types.SimpleNamespace(
        _catalog_refresh_running=False,
        _catalog_force_pending=False,
        _destroyed=False,
        _model="",
        _openai_catalog_authoritative=True,
        _openai_entries=[
            model_catalog.ModelEntry(
                "gpt-prior-account",
                "Prior GPT",
                "OpenAI",
                provider=model_catalog.PROVIDER_OPENAI,
            )
        ],
        _chat_toolbar=types.SimpleNamespace(
            set_choices=lambda entries: applied.append([entry.id for entry in entries]),
        ),
        _gpt_toggle=types.SimpleNamespace(
            set_sensitive=lambda _value: None,
            set_tooltip_text=lambda _value: None,
        ),
            _normalize_openai_model_memory=lambda: None,
            _sync_effort_sensitivity=lambda: None,
            _sync_execution_control=lambda: None,
            _sync_new_chat_actions=lambda: None,
    )
    fake._refresh_model_catalog = types.MethodType(
        MainWindow._refresh_model_catalog,
        fake,
    )
    fake._apply_model_catalog = types.MethodType(
        MainWindow._apply_model_catalog,
        fake,
    )

    fake._refresh_model_catalog(force=False)
    assert len(workers) == 1
    fake._refresh_model_catalog(force=True)
    assert fake._catalog_force_pending is True
    assert fake._openai_catalog_authoritative is False
    assert fake._openai_entries == []
    assert len(workers) == 1

    workers.pop(0).target()
    callback, args = idles.pop(0)
    assert callback(*args) is False

    # The pre-credential result was discarded and exactly one forced worker
    # replaced it; no old rows were ever published.
    assert applied == []
    assert len(workers) == 1
    assert fake._catalog_refresh_running is True

    workers.pop(0).target()
    callback, args = idles.pop(0)
    assert callback(*args) is False

    assert discovery_forces == [
        ("anthropic", False),
        ("openai", False),
        ("anthropic", True),
        ("openai", True),
    ]
    # Forced OpenAI failure still applies the safe Anthropic baseline, which
    # clears any GPT rows retained from the previous account.
    assert applied == [["claude-forced"]]
    assert fake._catalog_refresh_running is False
    assert fake._catalog_force_pending is False


def test_credential_callback_does_not_refresh_destroyed_window():
    fake = types.SimpleNamespace(
        _destroyed=True,
        _refresh_model_catalog=lambda **_kwargs: pytest.fail("refreshed after close"),
    )

    types.MethodType(MainWindow._on_codex_credentials_changed, fake)()


def test_empty_openai_catalog_switches_active_gpt_to_valid_anthropic_model():
    calls: list[tuple] = []

    class State:
        def __init__(self):
            self.values: dict[str, str] = {}

        def set(self, key, value):
            self.values[key] = value

    class Toggle:
        def __init__(self, active=False):
            self.active = active
            self.sensitive = True

        def get_active(self):
            return self.active

        def set_active(self, active):
            self.active = active
            calls.append(("active", active))

        def set_sensitive(self, sensitive):
            self.sensitive = sensitive
            calls.append(("sensitive", sensitive))

        def set_tooltip_text(self, text):
            calls.append(("tooltip", text))

    toolbar = types.SimpleNamespace(
        set_choices=lambda entries: calls.append(
            ("choices", [entry.id for entry in entries])
        ),
        set_model=lambda model: calls.append(("model", model)),
        set_context_model=lambda model: calls.append(("context", model)),
    )
    state = State()
    claude_toggle = Toggle(active=False)
    gpt_toggle = Toggle(active=True)
    fake = types.SimpleNamespace(
        _catalog_refresh_running=True,
        _catalog_force_pending=False,
        _destroyed=False,
        _model="gpt-5-old",
        _provider_models={
            model_catalog.PROVIDER_ANTHROPIC: "opus",
            model_catalog.PROVIDER_OPENAI: "gpt-5-old",
        },
        _provider_guard=False,
        _ui_state=state,
        _chat_toolbar=toolbar,
        _claude_toggle=claude_toggle,
        _gpt_toggle=gpt_toggle,
            _sync_assistant_labels=lambda: calls.append(("assistant", "Claude")),
            _sync_effort_sensitivity=lambda: calls.append(("effort",)),
            _sync_execution_control=lambda: calls.append(("execution",)),
            _sync_new_chat_actions=lambda: calls.append(("new-chat-actions",)),
        _refresh_openrouter_credits=lambda: calls.append(("or-credits",)),
    )
    fake._sync_provider_toggle = types.MethodType(
        MainWindow._sync_provider_toggle,
        fake,
    )
    fake._apply_model_choice = types.MethodType(MainWindow._apply_model_choice, fake)
    fake._normalize_openai_model_memory = types.MethodType(
        MainWindow._normalize_openai_model_memory,
        fake,
    )
    apply_catalog = types.MethodType(MainWindow._apply_model_catalog, fake)
    entries = [
        model_catalog.ModelEntry("opus", "Opus", "Latest"),
        model_catalog.ModelEntry("", "Default", "Other"),
    ]

    assert apply_catalog(entries, False) is False

    assert fake._model == "opus"
    assert fake._provider_models == {
        model_catalog.PROVIDER_ANTHROPIC: "opus",
        model_catalog.PROVIDER_OPENAI: "",
    }
    assert state.values == {
        "model_openai": "",
        "model": "opus",
        "model_anthropic": "opus",
    }
    assert ("model", "opus") in calls
    assert ("context", "opus") in calls
    assert claude_toggle.active is True
    assert gpt_toggle.sensitive is False
    assert ("new-chat-actions",) in calls
    assert fake._openai_entries == []


def test_empty_openai_catalog_uses_applied_default_and_invalidates_stale_memory():
    state: dict[str, str] = {}
    selected: list[str] = []
    fake = types.SimpleNamespace(
        _model="gpt-5-retired",
        _openai_entries=[],
        _catalog_entries_by_id={
            "": model_catalog.ModelEntry("", "Default", "Other"),
        },
        _provider_models={
            model_catalog.PROVIDER_ANTHROPIC: "missing-anthropic-model",
            model_catalog.PROVIDER_OPENAI: "gpt-5-retired",
        },
        _ui_state=types.SimpleNamespace(set=lambda key, value: state.__setitem__(key, value)),
        _apply_model_choice=lambda model, quiet=False: selected.append(model),
    )

    types.MethodType(MainWindow._normalize_openai_model_memory, fake)()

    assert selected == [""]
    assert fake._provider_models[model_catalog.PROVIDER_OPENAI] == ""
    assert state["model_openai"] == ""


def test_degraded_provider_mismatch_clears_only_foreign_resume_id():
    project = object()
    target = ChatTarget(project=project, resume_id="gpt-native-id", work_id="work-1")
    fake = types.SimpleNamespace(
        _next_chat=target,
        _selected_provider=lambda: model_catalog.PROVIDER_ANTHROPIC,
    )
    assert mw.session_providers.set_provider(
        "gpt-native-id",
        model_catalog.PROVIDER_OPENAI,
    )

    assert types.MethodType(MainWindow._fence_mismatched_target_resume, fake)()

    assert fake._next_chat is not target
    assert fake._next_chat.project is project
    assert fake._next_chat.work_id == "work-1"
    assert fake._next_chat.resume_id == ""


def test_provider_fence_preserves_matching_resume_id():
    target = ChatTarget(project=object(), resume_id="claude-native-id", work_id="work-1")
    fake = types.SimpleNamespace(
        _next_chat=target,
        _selected_provider=lambda: model_catalog.PROVIDER_ANTHROPIC,
    )
    assert mw.session_providers.set_provider(
        "claude-native-id",
        model_catalog.PROVIDER_ANTHROPIC,
    )

    assert types.MethodType(MainWindow._fence_mismatched_target_resume, fake)()

    assert fake._next_chat is target


# ── provider toggle default resolution (v0.42.2) ─────────────────────────

_OR_ENTRIES = [
    model_catalog.ModelEntry(
        "deepseek/deepseek-chat",
        "DeepSeek Chat",
        "DeepSeek",
        provider=model_catalog.PROVIDER_OPENROUTER,
    ),
    model_catalog.ModelEntry(
        "qwen/qwen3",
        "Qwen3",
        "Qwen",
        provider=model_catalog.PROVIDER_OPENROUTER,
    ),
]


def _toggle_fake(chosen, toasts, *, provider_models=None):
    fake = types.SimpleNamespace(
        _provider_guard=False,
        _model="claude-opus-4-8",
        _provider_models=dict(provider_models or {}),
        _apply_model_choice=lambda alias, **_: chosen.append(alias),
        _toast=lambda message: toasts.append(message),
        _sync_provider_toggle=lambda: None,
    )
    fake._model_for_provider = types.MethodType(
        MainWindow._model_for_provider, fake
    )
    return fake


def test_openrouter_toggle_uses_catalog_default(monkeypatch):
    """Regression: the OpenRouter toggle read a ``_openrouter_entries``
    attribute that was never assigned, so it always toasted "No OpenRouter
    models available" even with a saved key and a fetched catalog."""
    monkeypatch.setattr(
        mw.model_catalog, "openrouter_entries", lambda: (_OR_ENTRIES, "cached")
    )
    chosen: list[str] = []
    toasts: list[str] = []
    fake = _toggle_fake(chosen, toasts)
    btn = types.SimpleNamespace(get_active=lambda: True)

    MainWindow._on_provider_toggled(fake, btn, model_catalog.PROVIDER_OPENROUTER)

    assert chosen == ["deepseek/deepseek-chat"]
    assert toasts == []


def test_openrouter_toggle_prefers_remembered_model(monkeypatch):
    monkeypatch.setattr(
        mw.model_catalog, "openrouter_entries", lambda: (_OR_ENTRIES, "cached")
    )
    chosen: list[str] = []
    toasts: list[str] = []
    fake = _toggle_fake(
        chosen,
        toasts,
        provider_models={model_catalog.PROVIDER_OPENROUTER: "qwen/qwen3"},
    )
    btn = types.SimpleNamespace(get_active=lambda: True)

    MainWindow._on_provider_toggled(fake, btn, model_catalog.PROVIDER_OPENROUTER)

    assert chosen == ["qwen/qwen3"]
    assert toasts == []


@pytest.mark.parametrize("remembered", ["openai/gpt-6-astra", "anthropic/claude-fable-5-1"])
def test_openrouter_toggle_does_not_restore_excluded_native_models(monkeypatch, remembered):
    monkeypatch.setattr(
        mw.model_catalog, "openrouter_entries", lambda: (_OR_ENTRIES, "cached")
    )
    chosen, toasts = [], []
    fake = _toggle_fake(
        chosen, toasts,
        provider_models={model_catalog.PROVIDER_OPENROUTER: remembered},
    )
    btn = types.SimpleNamespace(get_active=lambda: True)
    MainWindow._on_provider_toggled(fake, btn, model_catalog.PROVIDER_OPENROUTER)
    assert chosen == ["deepseek/deepseek-chat"]
    assert toasts == []


def test_openrouter_toggle_toasts_only_when_catalog_empty(monkeypatch):
    monkeypatch.setattr(
        mw.model_catalog, "openrouter_entries", lambda: ([], "no-key")
    )
    chosen: list[str] = []
    toasts: list[str] = []
    fake = _toggle_fake(chosen, toasts)
    btn = types.SimpleNamespace(get_active=lambda: True)

    MainWindow._on_provider_toggled(fake, btn, model_catalog.PROVIDER_OPENROUTER)

    assert chosen == []
    assert toasts == [
        "No OpenRouter models available — add a key in Settings → Providers."
    ]


def test_openai_toggle_still_resolves_catalog_default():
    entry = model_catalog.ModelEntry(
        "gpt-5-codex",
        "GPT Codex",
        "OpenAI",
        provider=model_catalog.PROVIDER_OPENAI,
    )
    chosen: list[str] = []
    toasts: list[str] = []
    fake = _toggle_fake(chosen, toasts)
    fake._openai_entries = [entry]
    btn = types.SimpleNamespace(get_active=lambda: True)

    MainWindow._on_provider_toggled(fake, btn, model_catalog.PROVIDER_OPENAI)

    assert chosen == ["gpt-5-codex"]
    assert toasts == []


def test_new_chat_with_provider_toast_names_openrouter():
    """The shared no-models toast named OpenAI even for OpenRouter clicks."""
    toasts: list[str] = []
    fake = types.SimpleNamespace(
        _model_for_provider=lambda _provider: "",
        _toast=lambda message: toasts.append(message),
        _sync_provider_toggle=lambda: None,
    )

    MainWindow._start_new_chat_with_provider(
        fake, model_catalog.PROVIDER_OPENROUTER
    )

    assert toasts == [
        "No OpenRouter models available — add a key in Settings → Providers."
    ]
