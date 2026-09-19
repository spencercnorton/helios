"""Tests for the effort-level slider.

The top stop was "max" — a boolean CLI settings flag that resolved
effort to xhigh AND mandated Workflow-tool orchestration — sitting where the
CLI's real top level (`--effort max`) belongs. It is now `max`; ultracode
migrates onto it, and the orchestration mandate is gone (the Task and Workflow
tools remain available to every session either way).

Coverage:
  * chat_toolbar: 6 stops round-trip via _index_for_key and get_effort/set_effort.
  * cli_driver: argv shapes for effort, max, off, fallback.
  * ui_state: migrate_effort_level — old int → new key, retired key → replacement,
    unknown int default, missing key default.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# chat_toolbar and cli_driver import `gi` at module top, so this whole module
# needs PyGObject present. Skip cleanly on the CI image (no GTK), matching the
# repo convention (see tests/test_settings_dialog.py, test_goal_strip.py).
gi = pytest.importorskip("gi")


# ---------------------------------------------------------------------------
# chat_toolbar helpers (GTK-free — import the module-level functions directly)
# ---------------------------------------------------------------------------


def test_effort_stops_have_six_entries():
    from helios.widgets.chat_toolbar import EFFORT_STOPS

    assert len(EFFORT_STOPS) == 6


def test_effort_stop_keys():
    from helios.widgets.chat_toolbar import EFFORT_STOPS

    keys = [k for k, _l, _d in EFFORT_STOPS]
    assert keys == ["off", "low", "medium", "high", "xhigh", "max"]


def test_index_for_key_known_keys():
    from helios.widgets.chat_toolbar import EFFORT_STOPS, _index_for_key

    for expected_idx, (key, _label, _desc) in enumerate(EFFORT_STOPS):
        assert _index_for_key(key) == expected_idx, f"key={key!r}"


def test_index_for_key_unknown_falls_back_to_high():
    from helios.widgets.chat_toolbar import _index_for_key

    idx = _index_for_key("bogus_key")
    from helios.widgets.chat_toolbar import EFFORT_STOPS

    high_idx = next(i for i, (k, _l, _d) in enumerate(EFFORT_STOPS) if k == "high")
    assert idx == high_idx


def test_default_effort_is_high():
    from helios.widgets.chat_toolbar import DEFAULT_EFFORT

    assert DEFAULT_EFFORT == "high"


def test_max_stop_has_tooltip_text():
    from helios.widgets.chat_toolbar import EFFORT_STOPS

    top = next((d for k, _l, d in EFFORT_STOPS if k == "max"), None)
    assert top is not None
    # It describes reasoning depth and its cost — not orchestration, which is
    # no longer coupled to the effort level.
    assert "reasoning" in top.lower()
    assert "workflow" not in top.lower()


def _chat_toolbar_or_skip():
    """Construct the real widget when a GTK display is available."""
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk

    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("GTK display unavailable")
    from helios.widgets.chat_toolbar import ChatToolbar

    return ChatToolbar()


def test_effort_drag_emits_once_per_effective_stop():
    """Animated/fractional drag updates must not amplify outward signals."""
    toolbar = _chat_toolbar_or_skip()
    from gi.repository import Gtk

    toolbar.set_effort("xhigh")
    changed: list[str] = []
    toolbar.connect("effort-changed", lambda _toolbar, key: changed.append(key))

    # Gtk's animated adjustment produces fractional value updates while moving
    # toward a clicked mark.  The old handler recursively snapped and emitted
    # roughly twice for every frame within the same effective stop.
    for step in range(1, 101):
        toolbar._effort_scale.set_value(  # noqa: SLF001 - animation-frame probe
            4 + step / 100
        )
    # Repeated user targets within Max are consumed without invoking
    # Gtk's default animation or re-emitting the same logical selection.
    for requested in (4.5, 4.6, 4.8, 5.0):
        handled = toolbar._effort_scale.emit(  # noqa: SLF001
            "change-value", Gtk.ScrollType.JUMP, requested
        )
        assert handled is True

    assert changed == ["max"]
    assert toolbar.get_effort() == "max"


def test_accessible_range_change_updates_effort_once():
    """Direct range updates used by assistive tech stay fully synchronised."""
    toolbar = _chat_toolbar_or_skip()
    toolbar.set_effort("xhigh")
    changed: list[str] = []
    toolbar.connect("effort-changed", lambda _toolbar, key: changed.append(key))

    # GtkAccessibleRange.set_current_value() reaches Gtk.Range.set_value().
    toolbar._effort_scale.set_value(4.5)  # noqa: SLF001 - AT-equivalent path

    assert toolbar.get_effort() == "max"
    assert toolbar._selected_effort_key == "max"  # noqa: SLF001
    assert toolbar._effort_label.get_label() == "Max"  # noqa: SLF001
    assert changed == ["max"]


def test_programmatic_effort_sync_is_quiet():
    """Restoring provider/session state must not look like a user change."""
    toolbar = _chat_toolbar_or_skip()
    changed: list[str] = []
    toolbar.connect("effort-changed", lambda _toolbar, key: changed.append(key))

    toolbar.set_effort("max")

    assert toolbar.get_effort() == "max"
    assert changed == []


def test_duplicate_effort_signals_do_not_queue_duplicate_toasts():
    """MainWindow remains idempotent if a widget/backend repeats a key."""
    from helios.main_window import MainWindow

    writes: list[tuple[str, str]] = []
    toasts: list[str] = []
    window = SimpleNamespace(
        _destroyed=False,
        _effort_key="xhigh",
        _model="opus[1m]",
        _provider_efforts={"anthropic": "xhigh"},
        _ui_state=SimpleNamespace(set=lambda key, value: writes.append((key, value))),
        _driver=None,
        _next_chat=SimpleNamespace(
            resume_id="",
            project=SimpleNamespace(cwd="/repo", read_only=False),
        ),
        _selected_provider=lambda: "anthropic",
        _staged_permission_mode="",
        _staged_effort_key="",
        _conversation_perms=SimpleNamespace(
            get=lambda *_args: "",
            get_effort=lambda *_args: "",
        ),
        _effective_permission_mode=lambda _cwd: "default",
        _sync_execution_control=lambda: None,
        _toast=toasts.append,
    )

    for _ in range(100):
        MainWindow._on_toolbar_effort_changed(window, None, "max")

    assert window._staged_effort_key == "max"
    assert window._effort_key == "xhigh"
    assert window._provider_efforts["anthropic"] == "xhigh"
    assert writes == []
    # Zero, not one: a successful effort change no longer toasts at all — the
    # toolbar's reasoning label is the confirmation, and a banner per change
    # queued over the composer (Spencer, 2026-08-22).
    assert toasts == []


# ---------------------------------------------------------------------------
# ui_state migration
# ---------------------------------------------------------------------------


def test_migrate_effort_level_new_key_passthrough(tmp_path):
    from helios.backend.ui_state import UiStateStore, migrate_effort_level

    s = UiStateStore(tmp_path / "ui.json")
    s.set("effort_level", "xhigh")
    assert migrate_effort_level(s) == "xhigh"


def test_migrate_effort_level_old_int_16000_to_high(tmp_path):
    from helios.backend.ui_state import UiStateStore, migrate_effort_level

    s = UiStateStore(tmp_path / "ui.json")
    s.set("effort", 16000)
    assert migrate_effort_level(s) == "high"


def test_migrate_effort_level_old_int_map(tmp_path):
    from helios.backend.ui_state import UiStateStore, _INT_TO_EFFORT_KEY, migrate_effort_level

    for token_val, expected_key in _INT_TO_EFFORT_KEY.items():
        s = UiStateStore(tmp_path / f"ui_{token_val}.json")
        s.set("effort", token_val)
        assert migrate_effort_level(s) == expected_key, f"token={token_val}"


def test_migrate_effort_level_unknown_int_defaults_to_high(tmp_path):
    from helios.backend.ui_state import UiStateStore, migrate_effort_level

    s = UiStateStore(tmp_path / "ui.json")
    s.set("effort", 99999)
    assert migrate_effort_level(s) == "high"


def test_migrate_effort_level_empty_store_defaults_to_high(tmp_path):
    from helios.backend.ui_state import UiStateStore, migrate_effort_level

    s = UiStateStore(tmp_path / "ui.json")
    assert migrate_effort_level(s) == "high"


def test_migrate_effort_level_new_key_wins_over_old(tmp_path):
    """If both keys exist, new effort_level wins."""
    from helios.backend.ui_state import UiStateStore, migrate_effort_level

    s = UiStateStore(tmp_path / "ui.json")
    s.set("effort", 4000)
    s.set("effort_level", "xhigh")
    assert migrate_effort_level(s) == "xhigh"


# ---------------------------------------------------------------------------
# cli_driver argv shapes  (GTK-free via mocking)
# ---------------------------------------------------------------------------


class _FakeBinary:
    """Minimal stand-in for ClaudeBinary."""

    def __init__(self, path: str = "/usr/bin/claude") -> None:
        self.path = Path(path)


def _make_driver(**kw):
    """Create a ClaudeCliDriver without importing gi, by mocking the GTK layer."""
    # We only need to test the argv construction logic, not the actual subprocess.
    from helios.backend.process.cli_driver import ClaudeCliDriver

    return ClaudeCliDriver(cwd="/tmp", **kw)


def _collect_argv(
    driver,
    *,
    effort_flag: bool = True,
    max_budget_flag: bool = True,
    forward_subagent_text_flag: bool = False,
    reraise: bool = False,
) -> list[str]:
    """Run driver.start() with subprocess mocked out; return the argv it would use.

    The flag arguments control the corresponding Claude capability probes.
    """
    captured: list[list[str]] = []

    class _FakeLauncher:
        def new(self, _flags):
            return self

        def set_cwd(self, _cwd):
            pass

        def spawnv(self, argv):
            captured.append(list(argv))
            raise RuntimeError("stop-after-capture")

    class _FakeSubprocess:
        def get_identifier(self):
            return "99999"

        def get_stdin_pipe(self):
            return None

        def get_stdout_pipe(self):
            return None

        def get_stderr_pipe(self):
            return None

        def wait_async(self, *_a, **_kw):
            pass

    # We need to mock at the right level. Let's just use monkeypatch-style patching.
    import unittest.mock as mock

    captured_argv: list[str] = []

    def fake_spawnv(argv):
        captured_argv.extend(argv)
        # Return a mock subprocess object.
        proc = mock.MagicMock()
        proc.get_identifier.return_value = "99999"
        proc.get_stdin_pipe.return_value = mock.MagicMock()
        proc.get_stdout_pipe.return_value = mock.MagicMock()
        proc.get_stderr_pipe.return_value = mock.MagicMock()
        proc.wait_async = mock.MagicMock()
        return proc

    launcher_mock = mock.MagicMock()
    launcher_mock.spawnv = fake_spawnv

    with (
        patch("helios.backend.process.cli_driver.find_claude_binary",
              return_value=_FakeBinary()),
        patch("helios.backend.process.cli_driver.supports_effort_flag",
              return_value=effort_flag),
        patch("helios.backend.process.cli_driver.supports_budget_family_enforcement",
              return_value=max_budget_flag),
        patch("helios.backend.process.cli_driver.supports_forward_subagent_text",
              return_value=forward_subagent_text_flag),
        patch("helios.backend.process.cli_driver.shutil.which", return_value=None),
        patch("helios.backend.process.cli_driver.Gio.SubprocessLauncher.new",
              return_value=launcher_mock),
        patch("helios.backend.process.cli_driver.scrub_helios_env"),
        patch.object(driver, "_read_next_stdout_line"),
        patch.object(driver, "_read_next_stderr_line"),
    ):
        try:
            driver.start()
        except Exception:
            if reraise:
                raise

    return captured_argv


def test_effort_high_uses_effort_flag(tmp_path):
    """effort='high' with support → --effort high in argv."""
    driver = _make_driver(effort="high")
    argv = _collect_argv(driver, effort_flag=True)
    assert "--effort" in argv
    idx = argv.index("--effort")
    assert argv[idx + 1] == "high"
    assert "--max-thinking-tokens" not in argv


def test_claude_launch_has_budget_breaker_and_gated_bypass_capability(tmp_path):
    driver = _make_driver(permission_mode="default")
    argv = _collect_argv(driver)

    # --allow-dangerously-skip-permissions makes Bypass *selectable* over the
    # control protocol. The bare --dangerously-skip-permissions would ENABLE it
    # at launch regardless of the selected mode, and must never be passed.
    assert "--allow-dangerously-skip-permissions" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--max-budget-usd") + 1] == "10"
    assert "--max-turns" not in argv


def test_claude_launch_fails_closed_when_budget_capability_is_unverified():
    from helios.backend.process.cli_driver import DriverSpawnError

    driver = _make_driver()
    with pytest.raises(DriverSpawnError, match="required process-family budget"):
        _collect_argv(driver, max_budget_flag=False, reraise=True)


@pytest.mark.parametrize("invalid", [0, -1, float("inf"), float("nan"), "ten"])
def test_claude_budget_must_be_positive_and_finite(invalid):
    """None is no longer invalid — it is the explicit "no dollar cap" value for
    subscription billing. A nonsense *number* must still be rejected."""
    with pytest.raises(ValueError, match="max_budget_usd"):
        _make_driver(max_budget_usd=invalid)


def test_explicit_none_budget_is_accepted_as_no_cap():
    driver = _make_driver(max_budget_usd=None)

    assert driver._max_budget_usd is None
    assert "--max-budget-usd" not in _collect_argv(driver)


def test_launch_fails_closed_only_when_a_dollar_cap_is_actually_wanted():
    """The 2.1.217+ capability gate must not block a subscription account that
    has no dollar cap to enforce in the first place."""
    driver = _make_driver(max_budget_usd=None)

    # max_budget_flag=False simulates a CLI without --max-budget-usd. With no
    # cap wanted, that must NOT raise DriverSpawnError.
    argv = _collect_argv(driver, max_budget_flag=False)

    assert "--max-budget-usd" not in argv


def test_claude_launch_never_restricts_settings_sources(tmp_path):
    """The Router MCP is injected additively via --mcp-config; it must not
    bring --strict-mcp-config, which would drop the user's own MCP servers."""
    driver = _make_driver()
    argv = _collect_argv(driver)

    assert "--strict-mcp-config" not in argv
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local"


def test_claude_launch_matches_app_context_management(tmp_path):
    """Parity with the Claude Code app for the two context-window behaviours.

    Helios runs one long-lived stream-json session per Work, so without
    --autocompact a full window has no remedy but a new chat.
    """
    argv = _collect_argv(_make_driver())

    assert argv[argv.index("--autocompact") + 1] == "auto"
    assert "--exclude-dynamic-system-prompt-sections" in argv
    # That flag is ignored when a custom system prompt is set, so setting one
    # here would silently make the line above meaningless.
    assert "--system-prompt" not in argv


def test_effort_xhigh_uses_effort_flag(tmp_path):
    """effort='xhigh' with support → --effort xhigh."""
    driver = _make_driver(effort="xhigh")
    argv = _collect_argv(driver, effort_flag=True)
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_effort_fallback_no_flag_support(tmp_path):
    """effort='high' without --effort support → --max-thinking-tokens 16000."""
    driver = _make_driver(effort="high")
    argv = _collect_argv(driver, effort_flag=False)
    assert "--effort" not in argv
    assert "--max-thinking-tokens" in argv
    idx = argv.index("--max-thinking-tokens")
    assert argv[idx + 1] == "16000"


def test_effort_off_uses_max_thinking_tokens_zero(tmp_path):
    """effort='off' → --max-thinking-tokens 0 (regardless of flag support)."""
    driver = _make_driver(effort="off")
    argv = _collect_argv(driver, effort_flag=True)
    assert "--effort" not in argv
    assert "--max-thinking-tokens" in argv
    idx = argv.index("--max-thinking-tokens")
    assert argv[idx + 1] == "0"


def test_max_is_a_plain_effort_flag_not_a_settings_injection(tmp_path):
    """max is one of the CLI's own --effort levels — nothing special needed.

    The retired ultracode stop injected `--settings {"ultracode":true,...}`,
    which also switched the session's orchestration policy on. Asking for the
    deepest reasoning must no longer carry that side effect.
    """
    driver = _make_driver(effort="max")
    argv = _collect_argv(driver, effort_flag=True)
    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "max"
    assert "--settings" not in argv
    assert "--max-thinking-tokens" not in argv


def test_live_effort_change_always_clears_ultracode(tmp_path):
    """ultracode is sticky in the CLI's settings, so every change clears it.

    A process started under a settings.json with ultracode on would otherwise
    keep forcing Workflow-tool orchestration while the toolbar showed a plain
    level.
    """
    driver = _make_driver(effort="high")
    sent: list = []
    driver._send_control_request = lambda req, cb: (sent.append(req), True)[1]  # noqa: SLF001
    driver._busy = False  # noqa: SLF001
    driver._control_initialized = True  # noqa: SLF001
    driver._proc = object()  # noqa: SLF001
    driver._stdin = object()  # noqa: SLF001

    driver.set_effort("max")
    assert sent, "no control request was issued"
    settings = sent[0]["settings"]
    assert settings["ultracode"] is False
    assert settings["effortLevel"] == "max"


def test_off_does_not_send_an_invalid_effort_level(tmp_path):
    """"off" is a Helios concept; effortLevel only accepts low..max.

    Sending effortLevel:"off" risks the whole settings apply being rejected,
    which would leave thinking enabled while the UI claimed it was off.
    """
    driver = _make_driver(effort="high")
    sent: list = []
    driver._send_control_request = lambda req, cb: (sent.append(req), True)[1]  # noqa: SLF001
    driver._busy = False  # noqa: SLF001
    driver._control_initialized = True  # noqa: SLF001
    driver._proc = object()  # noqa: SLF001
    driver._stdin = object()  # noqa: SLF001

    driver.set_effort("off")
    assert "effortLevel" not in sent[0]["settings"]
    assert sent[0]["settings"]["ultracode"] is False


def test_legacy_max_thinking_tokens_still_works(tmp_path):
    """max_thinking_tokens=8000 (legacy path) → --max-thinking-tokens 8000."""
    driver = _make_driver(max_thinking_tokens=8000)
    argv = _collect_argv(driver, effort_flag=True)
    assert "--max-thinking-tokens" in argv
    idx = argv.index("--max-thinking-tokens")
    assert argv[idx + 1] == "8000"
    assert "--effort" not in argv


def test_retired_ultracode_key_migrates_to_max():
    """Anyone sitting on ultracode asked for the deepest reasoning available.

    Without the mapping they would land on the slider's "high" fallback — two
    stops below what they chose — silently, on next launch.
    """
    from helios.backend.ui_state import canonical_effort_key

    assert canonical_effort_key("ultracode") == "max"
    assert canonical_effort_key("max") == "max"
    assert canonical_effort_key("high") == "high"
    assert canonical_effort_key("") == ""


def test_stored_ultracode_conversations_reopen_as_max(tmp_path):
    """Per-conversation records persist the key independently of ui-state."""
    from helios.backend import conversation_perms as cp

    store = cp.ConversationPermsStore(tmp_path / "conv.json")
    assert store.set_effort(
        "anthropic", "sess-1", "ultracode", permission_mode="default"
    )
    assert store.get_effort("anthropic", "sess-1") == "max"


# --- billing-model-aware dollar breaker ----------------------------


def test_subscription_billing_omits_the_dollar_cap(monkeypatch):
    """A dollar cap on a subscription bounds a notional number, not money.

    Regression guard: a hardcoded $10 cap terminally killed a live Work at
    $10.07 on a Max subscription, where cost_micro_usd is an API-equivalent
    estimate. No fabricated token/turn cap replaces it — the real ceiling is
    the account rate limit, already delivered as rate_limit_info.
    """
    from helios.backend import claude_env
    from helios.backend.process import cli_driver as cd

    monkeypatch.setattr(claude_env, "is_subscription_billing", lambda **_k: True)
    assert cd.default_max_budget_usd() is None

    driver = _make_driver()
    argv = _collect_argv(driver)
    assert "--max-budget-usd" not in argv


def test_an_auth_change_is_not_masked_by_the_billing_cache(monkeypatch):
    """`_billing_cache` is process-wide and Helios outlives `claude auth login`.

    The dangerous direction is subscription → Console/API: a stale permissive
    answer would keep the dollar cap off while real money is being spent.
    """
    from helios.backend import claude_env
    from helios.backend.process import cli_driver as cd

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_env, "_billing_cache", {})

    monkeypatch.setattr(
        claude_env,
        "fetch_auth_status",
        lambda: SimpleNamespace(
            ok=True, logged_in=True, auth_method="claude.ai", subscription_type="max"
        ),
    )
    assert cd.default_max_budget_usd() is None

    # Same process, user switches to Console billing.
    monkeypatch.setattr(
        claude_env,
        "fetch_auth_status",
        lambda: SimpleNamespace(
            ok=True, logged_in=True, auth_method="console", subscription_type=""
        ),
    )
    assert cd.default_max_budget_usd() == cd.CLAUDE_STANDARD_MAX_BUDGET_USD


def test_per_token_billing_keeps_the_dollar_cap(monkeypatch):
    """An API key is real per-token spend; the breaker must survive there."""
    from helios.backend import claude_env
    from helios.backend.process import cli_driver as cd

    monkeypatch.setattr(claude_env, "is_subscription_billing", lambda **_k: False)
    assert cd.default_max_budget_usd() == cd.CLAUDE_STANDARD_MAX_BUDGET_USD

    driver = _make_driver()
    argv = _collect_argv(driver)
    assert argv[argv.index("--max-budget-usd") + 1] == "10"


def test_billing_detection_fails_closed_to_a_real_cap(monkeypatch):
    """Unreadable auth must not silently remove a spend control."""
    from helios.backend import claude_env

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    claude_env._billing_cache.clear()
    monkeypatch.setattr(
        claude_env, "fetch_auth_status",
        lambda *a, **k: claude_env.AuthStatus(False, error="boom"))

    assert claude_env.is_subscription_billing(refresh=True) is False


def test_explicit_api_key_outranks_a_subscription(monkeypatch):
    """Both can exist; an API key means real per-token billing."""
    from helios.backend import claude_env

    claude_env._billing_cache.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        claude_env, "fetch_auth_status",
        lambda *a, **k: claude_env.AuthStatus(
            True, logged_in=True, auth_method="claude.ai", subscription_type="max"))

    assert claude_env.is_subscription_billing(refresh=True) is False


# ── spend dispatch ────────────────────────────────────────────────


def _spend_result(model_total: int, root_turn: int, **extra) -> dict:
    record = {
        "type": "result",
        "subtype": "success",
        "modelUsage": {
            "claude-sonnet-5": {
                "cacheReadInputTokens": model_total,
                "contextWindow": 1_000_000,
                "canonicalModel": "claude-sonnet-5",
            }
        },
        "usage": {"cache_read_input_tokens": root_turn},
    }
    record.update(extra)
    return record


def test_driver_emits_the_measured_root_delegated_split():
    """Replays the foreground capture in the private model-usage capture notes §3
    through the driver's real record dispatch, so the wiring is pinned to the
    same numbers the accounting module is."""
    driver = _make_driver()
    seen = []
    driver.connect("spend-updated", lambda _d, snapshot: seen.append(snapshot))

    driver._dispatch_record(_spend_result(48_670, 48_670))
    driver._dispatch_record(_spend_result(203_975, 99_514))

    assert len(seen) == 2
    assert seen[0].delegated_tokens == 0
    assert (seen[1].total_tokens, seen[1].root_tokens, seen[1].delegated_tokens) == (
        203_975,
        148_184,
        55_791,
    )


def test_a_child_terminal_emits_no_spend_and_moves_no_total():
    """Child spend is already inside the root's cumulative `modelUsage`.
    Folding the child's own usage in would inflate the root side and erase the
    delegation the split exists to show."""
    driver = _make_driver()
    seen = []
    driver.connect("spend-updated", lambda _d, snapshot: seen.append(snapshot))

    driver._dispatch_record(_spend_result(200_000, 100_000))
    driver._dispatch_record(
        _spend_result(9, 999_999, parent_tool_use_id="toolu_child")
    )

    assert len(seen) == 1
    assert seen[0].delegated_tokens == 100_000


def test_a_task_notification_wakeup_still_reports_spend():
    """A background subagent finishing wakes the root for a turn Helios never
    sent a message for (doc §6), and for a background fan-out that record is
    exactly when the children's cost lands. Skipping it would leave the burn
    invisible in the case that caused the ticket."""
    driver = _make_driver()
    seen = []
    driver.connect("spend-updated", lambda _d, snapshot: seen.append(snapshot))

    driver._dispatch_record(_spend_result(148_651, 148_651))
    driver._dispatch_record(
        _spend_result(252_797, 51_376, origin={"kind": "task-notification"})
    )

    assert len(seen) == 2
    assert seen[1].delegated_tokens == 52_770


def test_a_driver_instance_is_one_process():
    """The invariant the spend accumulator's correctness rests on.

    `start()` returns early once `_proc` is set, and `_proc` is assigned in
    exactly one place and never cleared, so a driver cannot relaunch. That is
    why `_root_tokens` accumulating across the object's whole life is safe:
    the object's life and the process's life are the same span. If this test
    ever fails, the reset at the spawn site is load-bearing rather than
    belt-and-braces.
    """
    import inspect

    from helios.backend.process import cli_driver

    source = inspect.getsource(cli_driver.ClaudeCliDriver)
    assert source.count("self._proc = ") == 1, "a second assignment can relaunch"

    driver = _make_driver()
    first = _collect_argv(driver)
    assert first  # spawned once

    # The fake launcher raises after capturing, so `_proc` stays None in this
    # harness; assert the guard directly instead of via a second capture.
    driver._proc = object()
    assert driver.start() is None
    assert driver._proc is not None


def test_a_fresh_driver_starts_spend_at_zero():
    """Two drivers, two processes, two independent counts — the root total of
    a finished session must never be subtracted from the next one's
    `modelUsage`, which would drive the split negative and hide the delegated
    spend entirely."""
    first = _make_driver()
    first._dispatch_record(_spend_result(200_000, 100_000))
    assert first._spend.snapshot().delegated_tokens == 100_000

    second = _make_driver()
    seen = []
    second.connect("spend-updated", lambda _d, snapshot: seen.append(snapshot))
    second._dispatch_record(_spend_result(30_000, 30_000))

    assert seen[-1].root_tokens == 30_000
    assert seen[-1].delegated_tokens == 0


def test_the_spend_counter_is_rebound_at_the_spawn_boundary():
    """Spend is per-process, so the counter's life starts where the process
    does — not where the object does."""
    driver = _make_driver()
    driver._dispatch_record(_spend_result(200_000, 100_000))
    stale = driver._spend

    _collect_argv(driver)

    assert driver._spend is not stale
    assert driver._spend.snapshot().total_tokens == 0


# ── Router MCP advertisement ───────────────────────────────────────────────


def _router_argv(dispatchable: bool) -> list[str]:
    """argv for a launch where the broker does/does not report dispatchability."""
    with patch(
        "helios.backend.process.cli_driver.dispatch_available",
        return_value=dispatchable,
    ):
        return _collect_argv(_make_driver())


def test_router_mcp_is_not_advertised_when_the_broker_cannot_dispatch():
    """A tool whose only possible answer is "retained" is not worth its schema.

    The injection used to be conditioned on the launcher script existing in the
    repo, which is to say always — including through the entire period the
    broker was structurally unable to dispatch. That is ~1.6k tokens of tool
    schemas and one Python subprocess per session, and the MCP's own
    `instructions` tell the model to use `delegate_task` regularly, so it also
    bought turns spent on a guaranteed refusal.
    """
    argv = _router_argv(False)

    assert "--mcp-config" not in argv
    # Not a blanket MCP ban: the user's own servers arrive through the native
    # settings sources, which must still be unrestricted.
    assert "--strict-mcp-config" not in argv
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local"


def test_router_mcp_is_advertised_when_the_broker_can_dispatch():
    argv = _router_argv(True)

    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert list(config["mcpServers"]) == ["helios-router"]


def test_router_advertisement_never_blocks_the_ui_thread():
    """`start()` runs on the GTK main loop, so this must be a cached read.

    A socket round trip here would let a wedged broker stall every new session
    by the client timeout. The guard is that `cli_driver` imports the cached
    accessor and not `RouterClient`.
    """
    import inspect

    from helios.backend.process import cli_driver

    assert not hasattr(cli_driver, "RouterClient")
    source = inspect.getsource(cli_driver.ClaudeCliDriver.start)
    assert "dispatch_available()" in source
    assert "RouterClient" not in source
