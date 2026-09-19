"""GTK-free: every branch of `summarize_hook`.

Payload keys are verbatim from the CLI (claude 2.1.258) per PLAN-UI-2026-08-SPECS:
subtype, hook_event, hook_name, hook_id, and on hook_response also outcome,
exit_code, stdout, stderr.
"""

from __future__ import annotations

import json

from helios.backend.hook_events import HookNotice, summarize_hook


def _response(**overrides) -> dict:
    payload = {
        "subtype": "hook_response",
        "hook_event": "PreToolUse",
        "hook_name": "Bash",
        "hook_id": "h1",
        "outcome": "success",
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
    }
    payload.update(overrides)
    return payload


# ── progress subtypes never surface ───────────────────────────────────────


def test_hook_started_is_dropped():
    assert summarize_hook({"subtype": "hook_started"}) is None


def test_hook_progress_is_dropped():
    assert summarize_hook({"subtype": "hook_progress"}) is None


# ── a clean hook_response is dropped ──────────────────────────────────────


def test_success_exit_zero_no_decision_is_dropped():
    assert summarize_hook(_response()) is None


def test_additional_context_with_no_decision_is_dropped():
    stdout = json.dumps({"hookSpecificOutput": {"additionalContext": "fyi"}})
    assert summarize_hook(_response(stdout=stdout)) is None


# ── block/deny/ask decisions ──────────────────────────────────────────────


def test_top_level_block_decision_is_a_warning():
    stdout = json.dumps({"decision": "block", "reason": "no secrets in bash"})
    notice = summarize_hook(_response(stdout=stdout))
    assert notice == HookNotice(
        "warning", "Hook PreToolUse · Bash blocked", "no secrets in bash"
    )


def test_hook_specific_deny_decision_is_a_warning_titled_blocked():
    stdout = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "policy",
            }
        }
    )
    notice = summarize_hook(_response(stdout=stdout))
    assert notice == HookNotice("warning", "Hook PreToolUse · Bash blocked", "policy")


def test_hook_specific_ask_decision_is_a_warning_titled_asked():
    stdout = json.dumps(
        {"hookSpecificOutput": {"permissionDecision": "ask", "permissionDecisionReason": "confirm?"}}
    )
    notice = summarize_hook(_response(stdout=stdout))
    assert notice == HookNotice("warning", "Hook PreToolUse · Bash asked", "confirm?")


def test_a_decision_wins_even_on_a_nonzero_exit():
    """The decision check runs before the outcome/exit-code check — a hook
    that blocked and also happened to exit non-zero is still a "blocked"
    warning, not a "failed" error."""
    stdout = json.dumps({"decision": "block", "reason": "no"})
    notice = summarize_hook(_response(stdout=stdout, outcome="error", exit_code=1))
    assert notice.severity == "warning"
    assert notice.title == "Hook PreToolUse · Bash blocked"


# ── failures ───────────────────────────────────────────────────────────────


def test_nonzero_exit_is_an_error():
    notice = summarize_hook(_response(exit_code=2, stderr="boom"))
    assert notice == HookNotice("error", "Hook PreToolUse · Bash failed (exit 2)", "boom")


def test_non_success_outcome_with_exit_zero_is_still_an_error():
    notice = summarize_hook(_response(outcome="cancelled", stderr="timed out"))
    assert notice == HookNotice(
        "error", "Hook PreToolUse · Bash failed (exit 0)", "timed out"
    )


def test_error_detail_prefers_stderr_over_stdout():
    notice = summarize_hook(
        _response(exit_code=1, stdout="stdout text", stderr="stderr text")
    )
    assert notice.detail == "stderr text"


def test_error_detail_falls_back_to_stdout_when_stderr_is_empty():
    notice = summarize_hook(_response(exit_code=1, stdout="stdout text", stderr=""))
    assert notice.detail == "stdout text"


def test_error_detail_is_truncated_to_the_last_400_chars():
    long_stderr = "x" * 500 + "TAIL"
    notice = summarize_hook(_response(exit_code=1, stderr=long_stderr))
    assert notice.detail == long_stderr[-400:]
    assert notice.detail.endswith("TAIL")
    assert len(notice.detail) == 400


# ── malformed stdout must not crash ────────────────────────────────────────


def test_malformed_stdout_falls_back_to_the_outcome_check():
    notice = summarize_hook(_response(stdout="not valid json {"))
    assert notice is None  # success/exit 0/no parseable decision


def test_malformed_stdout_on_a_failure_still_yields_an_error():
    notice = summarize_hook(_response(exit_code=1, stdout="not valid json {", stderr=""))
    assert notice == HookNotice(
        "error", "Hook PreToolUse · Bash failed (exit 1)", "not valid json {"
    )


def test_stdout_that_is_valid_json_but_not_an_object_is_not_a_decision():
    notice = summarize_hook(_response(stdout="[1, 2, 3]"))
    assert notice is None


def test_hook_text_is_scrubbed_before_it_becomes_a_transcript_row():
    """A review finding: a hook is an arbitrary external program and its output
    becomes a durable transcript row, so it goes through the same scrubber as
    every other provider-derived string. The tokens here are assembled at
    runtime so no credential-shaped literal sits in the repo."""
    fake_pat = "glpat-" + "A" * 24
    fake_key = "sk-ant-" + "B" * 24

    failed = summarize_hook(
        {
            "subtype": "hook_response",
            "hook_event": "PreToolUse",
            "hook_name": "guard",
            "outcome": "blocked",
            "exit_code": 1,
            "stdout": "",
            "stderr": f"auth failed with {fake_pat}",
        }
    )
    assert failed is not None and failed.severity == "error"
    assert fake_pat not in failed.detail
    assert "REDACTED" in failed.detail

    blocked = summarize_hook(
        {
            "subtype": "hook_response",
            "hook_event": "PreToolUse",
            "hook_name": "guard",
            "outcome": "success",
            "exit_code": 0,
            "stdout": json.dumps({"decision": "block", "reason": f"leaked {fake_key}"}),
        }
    )
    assert blocked is not None and blocked.severity == "warning"
    assert fake_key not in blocked.detail
    assert "REDACTED" in blocked.detail
