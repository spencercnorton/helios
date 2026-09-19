"""Provider capability checks must distinguish degradation from drift.

The tracker item assumed a handshake to inspect. There is not one. Measured
against the installed builds:

  * claude 2.1.245's `initialize` control response has **no** `capabilities`
    key at all — its top-level keys are account/agents/analytics_disabled/
    available_output_styles/commands/current_permission_mode/
    fast_mode_disabled_reason/fast_mode_state/ide_rc_auto_enable_gate/models/
    output_style/pid/remote_control_*/session_state. `system/init` *does*
    carry `capabilities`, and on 2.1.245 it is
    `["interrupt_receipt_v1","interrupt_cancel_queued_v1","msg_lifecycle_v1"]`
    — interrupt/message-lifecycle protocol versions for a path Helios does not
    use (it interrupts with SIGINT). Nothing there gates a Helios feature.
  * Codex App Server 0.152.0 still has no general capability handshake, but it
    does generate an experimental JSON schema and exposes narrower discovery
    calls such as `collaborationMode/list`. Helios pins the generated method
    vocabulary, negotiates workflow modes directly, and classifies every
    server method as handled, deliberately ignored, or known unsupported.

The checks remain asymmetric: Claude degrades by losing a startup-probed flag;
Codex negotiates the surfaces it can and reserves "drift" for methods absent
from the pinned generated schema.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios.backend import claude_binary
from helios.backend.process import codex_app_events as app_events
from helios.backend.process import codex_protocol

_SRC = Path(__file__).resolve().parent.parent / "src" / "helios"


# ── Claude: a dropped optional flag is named, not silently lost ─────────────


def test_report_is_quiet_when_the_cli_can_do_everything(monkeypatch):
    monkeypatch.setattr(claude_binary, "_supports_flag", lambda flag: True)
    monkeypatch.setattr(claude_binary, "_claude_cli_version", lambda: (2, 1, 245))
    assert claude_binary.capability_report() == {
        "version": "2.1.245",
        "degraded": [],
    }


def test_every_missing_flag_is_named_with_what_it_costs(monkeypatch):
    monkeypatch.setattr(claude_binary, "_supports_flag", lambda flag: False)
    monkeypatch.setattr(claude_binary, "_claude_cli_version", lambda: (2, 1, 100))
    report = claude_binary.capability_report()
    degraded = report["degraded"]
    assert len(degraded) == len(claude_binary._OPTIONAL_FLAGS)
    for flag, consequence in claude_binary._OPTIONAL_FLAGS.items():
        assert any(flag in line and consequence in line for line in degraded), flag


def test_a_too_old_cli_that_has_the_budget_flag_is_a_distinct_finding(monkeypatch):
    """Accepting --max-budget-usd is not the same as enforcing it across
    subagents, and only one of the two findings can be true at a time."""

    monkeypatch.setattr(claude_binary, "_supports_flag", lambda flag: True)
    monkeypatch.setattr(claude_binary, "_claude_cli_version", lambda: (2, 1, 100))
    degraded = claude_binary.capability_report()["degraded"]
    assert degraded == ["budget enforcement across Claude's subagents (needs 2.1.217+)"]


def test_an_unreadable_version_does_not_crash_the_report(monkeypatch):
    monkeypatch.setattr(claude_binary, "_supports_flag", lambda flag: True)
    monkeypatch.setattr(claude_binary, "_claude_cli_version", lambda: None)
    report = claude_binary.capability_report()
    assert report["version"] == ""
    # No version means budget-family enforcement is unverified — fail closed.
    assert report["degraded"] == [
        "budget enforcement across Claude's subagents (needs 2.1.217+)"
    ]


# ── Codex: the drift detector must not go blind ─────────────────────────────


def _methods_compared_in(func_name: str, module: Path) -> set[str]:
    """Every `METHOD_*` name mentioned inside one function body."""

    tree = ast.parse(module.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == func_name
    )
    return {
        node.id
        for node in ast.walk(func)
        if isinstance(node, ast.Name) and node.id.startswith("METHOD_")
    }


def test_the_handled_set_covers_every_branch_feed_notification_has():
    """A new branch that forgets to register here would leave a hole in the
    drift detector — the method would be handled but reported as unknown."""

    compared = _methods_compared_in(
        "feed_notification", _SRC / "backend" / "process" / "codex_app_events.py"
    )
    assert compared, "AST match found no METHOD_ names — the guard has gone blind"
    values = {getattr(app_events, name) for name in compared}
    assert values <= app_events.HANDLED_NOTIFICATION_METHODS
    # And nothing stale: every registered method is one the code branches on.
    assert app_events.HANDLED_NOTIFICATION_METHODS == values


def test_a_stable_method_the_accumulator_ignores_is_still_not_drift():
    """`thread/started` is handled before the thread filter; the two reasoning
    methods are dropped on purpose because raw reasoning is model-private."""

    for method in (
        app_events.METHOD_THREAD_STARTED,
        app_events.METHOD_REASONING_TEXT_DELTA,
        app_events.METHOD_FILE_CHANGE_OUTPUT_DELTA,
    ):
        assert method in app_events.HANDLED_NOTIFICATION_METHODS


# ── Codex: an unhandled method is reported once, not swallowed ──────────────


def _codex_driver():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from helios.backend.process.codex_app_driver import CodexAppServerDriver

    driver = CodexAppServerDriver.__new__(CodexAppServerDriver)
    # GObject signal machinery only; the notification path under test reads
    # nothing else off the instance.
    CodexAppServerDriver.__init__(driver, cwd="/tmp", model="gpt-5.6")
    driver._unknown_notification_reported = False
    driver._hub = None
    return driver


def _drift(driver, methods):
    seen: list[dict] = []
    driver.connect("capability-drift", lambda _d, payload: seen.append(payload))
    for method in methods:
        driver._note_unknown_notification(method)
    return seen


def test_a_handled_method_is_never_reported_as_drift(monkeypatch):
    pytest.importorskip("gi")
    from helios.backend.process import codex_app_driver

    monkeypatch.setattr(codex_app_driver, "_UNKNOWN_NOTIFICATION_METHODS", set())
    driver = _codex_driver()
    safe = sorted(
        codex_protocol.HANDLED_NOTIFICATION_METHODS
        | codex_protocol.IGNORED_NOTIFICATION_METHODS
    )
    assert _drift(driver, safe) == []


def test_a_known_unsupported_method_reports_the_capability_not_drift(monkeypatch):
    pytest.importorskip("gi")
    from helios.backend.process import codex_app_driver

    monkeypatch.setattr(codex_app_driver, "_UNSUPPORTED_NOTIFICATION_METHODS", set())
    driver = _codex_driver()
    seen = _drift(driver, ["model/rerouted", "model/rerouted"])
    assert len(seen) == 1
    assert seen[0]["degraded"] == [
        "provider model rerouting is not reflected in the transcript "
        "(model/rerouted)"
    ]
    assert driver._unknown_notification_reported is False


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        (
            "warning",
            {"threadId": "thread-1", "message": " Model changed. "},
            {
                "method": "warning",
                "threadId": "thread-1",
                "message": "Model changed.",
            },
        ),
        (
            "guardianWarning",
            {"threadId": "thread-1", "message": "Review this action."},
            {
                "method": "guardianWarning",
                "threadId": "thread-1",
                "message": "Review this action.",
            },
        ),
        (
            "configWarning",
            {
                "summary": "Unknown setting",
                "details": "Remove it.",
                "path": "/tmp/config.toml",
            },
            {
                "method": "configWarning",
                "message": "Unknown setting",
                "details": "Remove it.",
                "path": "/tmp/config.toml",
            },
        ),
        (
            "deprecationNotice",
            {"summary": "Old field", "details": "Use the new field."},
            {
                "method": "deprecationNotice",
                "message": "Old field",
                "details": "Use the new field.",
            },
        ),
    ],
)
def test_textual_provider_notices_are_projected_without_capability_drift(
    method,
    params,
    expected,
):
    pytest.importorskip("gi")
    driver = _codex_driver()
    driver._native_mode = True
    driver._app_acc.thread_id = "thread-1"
    notices: list[dict] = []
    drift: list[dict] = []
    driver.connect("provider-notice", lambda _d, payload: notices.append(payload))
    driver.connect("capability-drift", lambda _d, payload: drift.append(payload))

    driver._consume_app_notification(method, params)

    assert notices == [expected]
    assert drift == []


def test_malformed_provider_notice_is_not_fabricated(monkeypatch):
    pytest.importorskip("gi")
    from helios.backend.process import codex_app_driver

    driver = _codex_driver()
    driver._native_mode = True
    notices: list[dict] = []
    warnings: list[str] = []
    driver.connect("provider-notice", lambda _d, payload: notices.append(payload))
    monkeypatch.setattr(
        codex_app_driver,
        "_log",
        SimpleNamespace(warning=lambda msg, *a: warnings.append(msg % a)),
    )

    driver._consume_app_notification("warning", {"message": "  "})

    assert notices == []
    assert warnings == ["Codex App Server sent malformed warning notification"]


def test_runtime_notice_toast_uses_provider_message_and_deduplicates():
    pytest.importorskip("gi")
    from helios.main_window import MainWindow

    toasts: list[tuple[str, int]] = []
    window = SimpleNamespace(
        _destroyed=False,
        _toast=lambda text, *, timeout=4: toasts.append((text, timeout)),
    )
    payload = {
        "method": "warning",
        "threadId": "thread-1",
        "message": "  Model changed.  ",
    }

    MainWindow._on_codex_provider_notice(window, None, payload)
    MainWindow._on_codex_provider_notice(window, None, payload)

    assert toasts == [("Codex warning: Model changed.", 8)]
    assert "cannot do" not in toasts[0][0]


def test_guardian_notice_is_never_suppressed_as_a_duplicate():
    pytest.importorskip("gi")
    from helios.main_window import MainWindow

    toasts: list[str] = []
    window = SimpleNamespace(
        _destroyed=False,
        _toast=lambda text, **_kwargs: toasts.append(text),
    )
    payload = {
        "method": "guardianWarning",
        "threadId": "thread-1",
        "message": "Review this action.",
    }

    MainWindow._on_codex_provider_notice(window, None, payload)
    MainWindow._on_codex_provider_notice(window, None, payload)

    assert toasts == [
        "Codex safety warning: Review this action.",
        "Codex safety warning: Review this action.",
    ]


def test_a_renamed_method_is_reported_once_and_names_itself(monkeypatch):
    pytest.importorskip("gi")
    from helios.backend.process import codex_app_driver

    monkeypatch.setattr(codex_app_driver, "_UNKNOWN_NOTIFICATION_METHODS", set())
    driver = _codex_driver()
    warnings: list[str] = []
    monkeypatch.setattr(
        codex_app_driver,
        "_log",
        SimpleNamespace(warning=lambda msg, *a: warnings.append(msg % a)),
    )
    seen = _drift(
        driver,
        ["turn/planV2/updated", "turn/planV2/updated", "item/somethingElse"],
    )
    assert len(seen) == 1, "a rename fires per notification; the toast must not"
    assert seen[0]["provider"] == "codex"
    assert "turn/planV2/updated" in seen[0]["degraded"][0]

    # The log is per method, not per notification: a renamed method arrives on
    # every turn, and one warning each is what makes the log readable.
    assert len(warnings) == 2, warnings
    assert codex_app_driver._UNKNOWN_NOTIFICATION_METHODS == {
        "turn/planV2/updated",
        "item/somethingElse",
    }


def test_each_driver_reports_the_drift_its_own_window_needs(monkeypatch):
    """The process-wide record keeps the log readable; it must not decide that
    a second driver's window has already been told."""

    pytest.importorskip("gi")
    from helios.backend.process import codex_app_driver

    monkeypatch.setattr(codex_app_driver, "_UNKNOWN_NOTIFICATION_METHODS", set())
    warnings: list[str] = []
    monkeypatch.setattr(
        codex_app_driver,
        "_log",
        SimpleNamespace(warning=lambda msg, *a: warnings.append(msg % a)),
    )
    first, second = _codex_driver(), _codex_driver()
    assert len(_drift(first, ["turn/planV2/updated"])) == 1
    assert len(_drift(second, ["turn/planV2/updated"])) == 1
    assert len(warnings) == 1, warnings
