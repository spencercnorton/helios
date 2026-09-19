"""Auto-compaction stops being invisible, and can be triggered.

`--autocompact auto` has been on since v0.61.0, so the CLI compacts silently
mid-session. Helios received the `compact_boundary` record and dropped it, which
meant neither the user nor the next reader could tell whether an answer came
from the transcript or from a summary of it. The terminal shows this; the app
did not.
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402
from helios.backend.process.codex_app_driver import CodexAppServerDriver  # noqa: E402
from helios.backend.process.message_queue import MessageDelivery  # noqa: E402
from helios.backend import model_catalog  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


def _driver() -> ClaudeCliDriver:
    drv = ClaudeCliDriver.__new__(ClaudeCliDriver)
    ClaudeCliDriver.__init__(drv, cwd="/tmp")
    return drv


# --- the driver surfaces the record ---------------------------------------


def test_a_compact_boundary_is_emitted_not_swallowed() -> None:
    drv = _driver()
    seen: list[dict] = []
    drv.connect("context-compacted", lambda _d, info: seen.append(info))

    drv._dispatch_record(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {"trigger": "auto", "pre_tokens": 148000},
        }
    )

    assert seen == [{"trigger": "auto", "pre_tokens": 148000}]


def test_a_manual_compaction_is_distinguishable_from_an_automatic_one() -> None:
    """Only one of the two is a surprise, so the UI must be able to tell."""

    drv = _driver()
    seen: list[dict] = []
    drv.connect("context-compacted", lambda _d, info: seen.append(info))

    drv._dispatch_record(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {"trigger": "manual"},
        }
    )

    assert seen[0]["trigger"] == "manual"


def test_missing_metadata_degrades_to_auto_rather_than_crashing() -> None:
    drv = _driver()
    seen: list[dict] = []
    drv.connect("context-compacted", lambda _d, info: seen.append(info))

    drv._dispatch_record({"type": "system", "subtype": "compact_boundary"})

    assert seen == [{"trigger": "auto", "pre_tokens": 0}]


def test_the_init_record_still_works() -> None:
    """The new branch sits in front of `init`; it must not shadow it."""

    drv = _driver()
    started: list[tuple] = []
    drv.connect("session-started", lambda _d, *a: started.append(a))

    drv._dispatch_record(
        {"type": "system", "subtype": "init", "session_id": "s1", "cwd": "/tmp"}
    )

    assert started and started[0][0] == "s1"


# --- the window shows it ---------------------------------------------------


class _Toolbar:
    def __init__(self) -> None:
        self.noted: list[tuple] = []
        self.busy = False

    def note_compaction(self, trigger, pre_tokens):
        self.noted.append((trigger, pre_tokens))

    def set_busy(self, busy):
        self.busy = bool(busy)


class _Composer:
    def __init__(self) -> None:
        self.busy = False
        self.focused = 0

    def set_busy(self, busy):
        self.busy = bool(busy)

    def grab_input_focus(self):
        self.focused += 1


class _Activity:
    def __init__(self) -> None:
        self.state = ""

    def set_activity(self, state, _detail=""):
        self.state = state

    def clear(self):
        self.state = ""


class _Transcript:
    def __init__(self) -> None:
        self.turns = []

    def append_turn(self, turn):
        self.turns.append(turn)

    def append_meta_turn(self, turn):
        self.turns.append(turn)


def _window(drv, provider=model_catalog.PROVIDER_ANTHROPIC):
    toasts: list[str] = []
    w = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda c: c is drv,
        _selected_provider=lambda: provider,
        _driver_matches_selected_provider=lambda _candidate: True,
        _driver_matches_target_binding=lambda _candidate: True,
        _chat_toolbar=_Toolbar(),
        _composer=_Composer(),
        _activity=_Activity(),
        _transcript=_Transcript(),
        _restore_in_flight=False,
        _native_pending_user={},
        _provider_dispatch_in_progress=set(),
        _toast=toasts.append,
    )
    return w, toasts


def test_an_automatic_compaction_toasts_and_is_recorded() -> None:
    drv = _driver()
    w, toasts = _window(drv)

    MainWindow._on_context_compacted(w, drv, {"trigger": "auto", "pre_tokens": 148000})

    assert w._chat_toolbar.noted == [("auto", 148000)]
    assert len(w._transcript.turns) == 1
    assert "Context compacted here" in w._transcript.turns[0].text
    assert toasts and "auto-compacted" in toasts[0]
    assert "148,000" in toasts[0]


def test_a_manual_compaction_is_recorded_without_a_toast() -> None:
    """The user just asked for it — telling them it happened is noise."""

    drv = _driver()
    w, toasts = _window(drv)

    MainWindow._on_context_compacted(w, drv, {"trigger": "manual", "pre_tokens": 9})

    assert w._chat_toolbar.noted == [("manual", 9)]
    assert w._composer.focused == 1
    assert toasts == []


def test_a_background_driver_does_not_report_into_the_visible_chat() -> None:
    drv = _driver()
    other = _driver()
    w, toasts = _window(drv)

    MainWindow._on_context_compacted(w, other, {"trigger": "auto"})

    assert w._chat_toolbar.noted == []
    assert toasts == []


# --- manual trigger --------------------------------------------------------


class _FakeClaude(ClaudeCliDriver):
    """A real ClaudeCliDriver subclass: `_compact_current_chat` does an
    isinstance check, and `is_accepting_input` is a read-only property."""

    def __init__(self, accepting: bool = True, *, busy: bool = False) -> None:
        ClaudeCliDriver.__init__(self, cwd="/tmp")
        self._accepting = accepting
        self._busy = busy
        self._session_id = "claude-session"
        self._init_slash_commands = {"compact"}
        self.sent: list[str] = []
        self.sent_with_context: list[bool] = []

    @property
    def is_accepting_input(self) -> bool:
        return self._accepting

    def send_user_text(self, text: str, *, with_context: bool = True):
        self.sent.append(text)
        self.sent_with_context.append(with_context)
        self._busy = True
        return MessageDelivery("accepted")


def test_compacting_sends_the_providers_own_command() -> None:
    """Deliberately the CLI's /compact, not a Helios-side summariser: a second
    summary would be a second source of truth about what the model remembers."""

    drv = _FakeClaude(accepting=True)
    w, toasts = _window(drv)
    w._driver = drv

    MainWindow._compact_current_chat(w)

    assert drv.sent == ["/compact"]
    assert toasts == ["Compacting…"]
    assert w._composer.busy
    assert w._chat_toolbar.busy
    assert w._activity.state == "compacting"
    # The command must bypass the prompt-context provider, or the Goal
    # envelope buries it and the model reads the whole thing as prose.
    # This assertion is the call-site half; test_wire_contract.py owns the
    # half that reads the bytes actually handed to stdin, because a stub of
    # `send_user_text` can never see a bug that lives inside it.
    assert drv.sent_with_context == [False]


def test_compacting_mid_turn_is_refused_with_a_reason() -> None:
    drv = _FakeClaude(accepting=True, busy=True)
    w, toasts = _window(drv)
    w._driver = drv

    MainWindow._compact_current_chat(w)

    assert drv.sent == []
    assert toasts and "Wait for the current provider operation" in toasts[0]


def test_compacting_with_no_claude_chat_says_so() -> None:
    w, toasts = _window(None)
    w._driver = None

    MainWindow._compact_current_chat(w)

    assert toasts and "Start or resume a Claude conversation" in toasts[0]


class _FakeCodex(CodexAppServerDriver):
    def __init__(self) -> None:
        CodexAppServerDriver.__init__(
            self,
            cwd="/tmp",
            model="gpt-5.6-sol",
            hub=object(),
            token_budget=None,
        )
        self._native_mode = True
        self._app_acc.thread_id = "codex-session"
        self.compaction_requests = 0
        self.review_requests: list[str] = []
        self.fork_requests = 0

    def request_compaction(self) -> bool:
        self.compaction_requests += 1
        self._busy = True
        return True

    def request_review(self, instructions: str = "") -> MessageDelivery:
        self.review_requests.append(instructions)
        self._busy = True
        return MessageDelivery("pending")

    def request_fork(self) -> MessageDelivery:
        self.fork_requests += 1
        self._busy = True
        return MessageDelivery("pending")


def test_gpt_compacting_calls_app_server_control_not_user_text() -> None:
    drv = _FakeCodex()
    w, toasts = _window(drv, model_catalog.PROVIDER_OPENAI)
    w._driver = drv

    MainWindow._compact_current_chat(w)

    assert drv.compaction_requests == 1
    assert drv._mirror._pending_user == []
    assert toasts == ["Compacting…"]


def test_typed_gpt_compact_is_intercepted_before_prompt_dispatch() -> None:
    drv = _FakeCodex()
    w, _toasts = _window(drv, model_catalog.PROVIDER_OPENAI)
    w._driver = drv

    MainWindow._on_composer_send(w, None, "/compact")

    assert drv.compaction_requests == 1
    assert drv._mirror._pending_user == []


def test_typed_gpt_review_is_native_and_keeps_arguments_out_of_turn_start() -> None:
    drv = _FakeCodex()
    w, toasts = _window(drv, model_catalog.PROVIDER_OPENAI)
    w._driver = drv

    MainWindow._on_composer_send(w, None, "/review focus on races")

    assert drv.review_requests == ["focus on races"]
    assert w._native_pending_user[id(drv)] == (drv, "/review focus on races")
    assert drv._mirror._pending_user == []
    assert w._activity.state == "reviewing"
    assert toasts == ["Reviewing changes…"]


def test_typed_gpt_fork_is_native_control_plane_work() -> None:
    drv = _FakeCodex()
    w, toasts = _window(drv, model_catalog.PROVIDER_OPENAI)
    w._driver = drv

    MainWindow._on_composer_send(w, None, "/fork")

    assert drv.fork_requests == 1
    assert drv._mirror._pending_user == []
    assert w._activity.state == "thinking"
    assert toasts == ["Forking conversation…"]


def test_accepted_fork_clones_work_settings_then_selects_new_chat(
    monkeypatch,
    tmp_path,
) -> None:
    drv = _FakeCodex()
    drv._helios_work_id = "source-work"
    mirror = tmp_path / "fork-thread.jsonl"
    mirror.write_text("mirror\n", encoding="utf-8")
    clone_calls = []
    monkeypatch.setattr(
        "helios.main_window.clone_transcript_for_fork",
        lambda **kwargs: clone_calls.append(kwargs) or mirror,
    )
    provider_calls = []
    monkeypatch.setattr(
        "helios.main_window.session_providers.set_provider",
        lambda *args: provider_calls.append(args) or True,
    )

    class Coordinator:
        def __init__(self):
            self.calls = []

        def fork_work(self, **kwargs):
            self.calls.append(kwargs)
            return object(), object()

    class Perms:
        def __init__(self):
            self.saved = []

        def get_settings(self, provider, session_id):
            assert (provider, session_id) == ("openai", "codex-session")
            return types.SimpleNamespace(
                permission_mode="auto",
                effort_key="high",
                workflow_mode="default",
            )

        def set(self, *args, **kwargs):
            self.saved.append((args, kwargs))
            return True

    class Sessions:
        def __init__(self):
            self.reloads = []
            self.revealed = []

        def reload(self, **kwargs):
            self.reloads.append(kwargs)

        def reveal_session(self, session_id):
            self.revealed.append(session_id)
            return True

    coordinator = Coordinator()
    perms = Perms()
    sessions = Sessions()
    w, toasts = _window(drv, model_catalog.PROVIDER_OPENAI)
    w._driver = drv
    w._work_coordinator = coordinator
    w._conversation_perms = perms
    w._sessions = sessions

    MainWindow._on_codex_thread_forked(
        w,
        drv,
        {
            "source_thread_id": "codex-session",
            "thread_id": "fork-thread",
            "stop_requested": False,
        },
    )

    assert clone_calls == [
        {
            "cwd": "/tmp",
            "source_thread_id": "codex-session",
            "fork_thread_id": "fork-thread",
        }
    ]
    assert coordinator.calls == [
        {
            "source_work_id": "source-work",
            "provider": "openai",
            "native_id": "fork-thread",
            "model": "gpt-5.6-sol",
        }
    ]
    assert perms.saved == [
        (
            ("openai", "fork-thread", "auto"),
            {"effort_key": "high", "workflow_mode": "default"},
        )
    ]
    assert provider_calls == [("fork-thread", "openai")]
    assert sessions.reloads == [
        {"preserve_selection": True, "rescan_pool": False}
    ]
    assert sessions.revealed == ["fork-thread"]
    assert w._activity.state == ""
    assert toasts == ["Conversation forked into a new independent Work."]
