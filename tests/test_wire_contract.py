"""The driver's wire contract: what actually reaches claude's stdin.

Two defects with the same shape — Helios looked correct at the call site and
was wrong on the wire — so every assertion here reads the bytes handed to
stdin rather than trusting that a method was called.

1. `/compact` was wrapped in the Goal envelope by the prompt-context provider,
   and a slash command is only a command as the LEADING token. The button
   toasted "Compacting…" and nothing happened. The existing coverage in
   test_compaction_visibility.py could not catch it: its `_FakeClaude` stubs
   `send_user_text`, which is the method the bug lived in.

2. An unhandled `control_request` subtype was answered with a bare `return`,
   while `_handle_control_request`'s own docstring records that an unanswered
   request stalls the CLI.
"""

from __future__ import annotations

import json

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402


class _CapturedStdin:
    """Stands in for the Gio stdin pipe, recording whole frames."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write_all(self, data: bytes, _cancellable=None):
        for line in data.decode("utf-8").splitlines():
            if line.strip():
                self.frames.append(json.loads(line))
        return (True, len(data))


def _driver() -> tuple[ClaudeCliDriver, _CapturedStdin]:
    drv = ClaudeCliDriver.__new__(ClaudeCliDriver)
    ClaudeCliDriver.__init__(drv, cwd="/tmp")
    stdin = _CapturedStdin()
    drv._stdin = stdin
    return drv, stdin


def _install_goal_envelope(drv: ClaudeCliDriver) -> None:
    """The real shape of the bug: session_goals wraps outgoing user text."""

    drv._prompt_context_provider = lambda _d, text: (
        "--- BEGIN HELIOS GOAL ---\nObjective: ship it\n"
        f"--- END HELIOS GOAL ---\n{text}"
    )


# --- 1. the command must lead the frame -----------------------------------


def test_compact_reaches_stdin_as_a_bare_command() -> None:
    drv, stdin = _driver()
    _install_goal_envelope(drv)
    # Bypass admission/persistence, which are not what this test is about.
    drv._execution_block_reason = lambda: ""
    drv._begin_execution_attempt = lambda: ""
    drv._record_execution_dispatch = lambda _e: True
    drv._confirmed_execution_native_id = lambda: ""
    # `is_running` is a read-only property over these two.
    drv._proc = object()
    drv._closed = False

    drv.send_user_text("/compact", with_context=False)

    sent = stdin.frames[-1]["message"]["content"][0]["text"]
    assert sent == "/compact", f"the command must lead the frame, got {sent!r}"


def test_user_text_still_gets_the_goal_envelope() -> None:
    """The fix must not disarm the envelope for ordinary messages — that
    envelope is how a Work's objective reaches the model."""

    drv, stdin = _driver()
    _install_goal_envelope(drv)
    drv._execution_block_reason = lambda: ""
    drv._begin_execution_attempt = lambda: ""
    drv._record_execution_dispatch = lambda _e: True
    drv._confirmed_execution_native_id = lambda: ""
    # `is_running` is a read-only property over these two.
    drv._proc = object()
    drv._closed = False

    drv.send_user_text("hello")

    sent = stdin.frames[-1]["message"]["content"][0]["text"]
    assert sent.startswith("--- BEGIN HELIOS GOAL ---")
    assert sent.endswith("hello")


# --- 2. every control_request gets an answer -------------------------------


@pytest.mark.parametrize(
    "subtype",
    ["elicitation", "hook_callback", "mcp_message", "oauth_token_refresh"],
)
def test_an_unhandled_control_request_is_refused_not_ignored(subtype: str) -> None:
    drv, stdin = _driver()

    drv._handle_control_request(
        {"request_id": "req-7", "request": {"subtype": subtype}}
    )

    assert stdin.frames, f"{subtype} got no response — this stalls the CLI"
    response = stdin.frames[-1]
    assert response["type"] == "control_response"
    assert response["response"]["request_id"] == "req-7"
    # Deliberately an error, not an empty success: claiming to have handled
    # hook_callback would be worse than refusing it.
    assert response["response"]["subtype"] == "error"
    assert subtype in response["response"]["error"]


def test_can_use_tool_is_still_routed_normally() -> None:
    """The refusal must not swallow the one subtype Helios does implement."""

    drv, _stdin = _driver()
    asked: list[str] = []
    drv.connect("question-asked", lambda _d, _p, tid: asked.append(tid))

    drv._handle_control_request(
        {
            "request_id": "req-8",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "tool_use_id": "tu-1",
                "input": {"questions": []},
            },
        }
    )

    assert asked == ["tu-1"]
