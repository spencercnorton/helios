"""Forwarded subagent messages (--forward-subagent-text).

Wire shapes measured on claude 2.1.241 (2026-08-25): with the flag, each
child's records arrive as complete `assistant` / `user` records tagged with
`parent_tool_use_id` — full messages, never stream deltas. Two contracts:

* the child's words may reach exactly one place, the actor projection —
  never the root transcript, never the root accumulators;
* a forwarded child `user` record (its prompt, its tool results) must not
  render as a root "You" turn, which is what the pre-guard dispatch did.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import ClaudeCliDriver

from test_effort_levels import _collect_argv, _make_driver


def _driver_with_actor(status: str = "starting"):
    driver = ClaudeCliDriver(cwd="/tmp", permission_mode="default")
    driver._agents = {
        "tu1": {
            "status": status,
            "name": "Test subagent",
            "role": "general-purpose",
            "message": "",
            "tool": "Task",
        }
    }
    updates: list[dict] = []
    driver.connect("agents-updated", lambda _d, agents: updates.append(agents))
    appended: list[object] = []
    driver.connect("turn-appended", lambda _d, turn: appended.append(turn))
    return driver, updates, appended


def _child_assistant(parent_id: str, blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": blocks, "model": "haiku"},
        "parent_tool_use_id": parent_id,
        "session_id": "child-1",
    }


def test_argv_carries_forward_flag_only_when_supported():
    argv = _collect_argv(_make_driver(), forward_subagent_text_flag=True)
    assert "--forward-subagent-text" in argv
    argv = _collect_argv(_make_driver(), forward_subagent_text_flag=False)
    assert "--forward-subagent-text" not in argv


def test_child_assistant_promotes_actor_and_surfaces_tail():
    driver, updates, appended = _driver_with_actor()
    driver._dispatch_record(
        _child_assistant("tu1", [{"type": "text", "text": "BANANA"}])
    )
    assert appended == []
    assert updates
    actor = updates[-1]["tu1"]
    assert actor["status"] == "working"
    assert actor["message"] == "BANANA"


def test_child_thinking_surfaces_when_no_text_yet():
    driver, updates, _appended = _driver_with_actor()
    driver._dispatch_record(
        _child_assistant(
            "tu1", [{"type": "thinking", "thinking": "considering  the\nrequest"}]
        )
    )
    assert updates[-1]["tu1"]["message"] == "considering the request"


def test_child_assistant_with_unknown_parent_creates_no_actor():
    driver, updates, appended = _driver_with_actor()
    driver._dispatch_record(
        _child_assistant("tu-unknown", [{"type": "text", "text": "hi"}])
    )
    assert "tu-unknown" not in driver._agents
    assert updates == []
    assert appended == []


def test_child_assistant_never_resurrects_a_terminal_actor():
    driver, updates, _appended = _driver_with_actor(status="complete")
    driver._dispatch_record(
        _child_assistant("tu1", [{"type": "text", "text": "late words"}])
    )
    assert driver._agents["tu1"]["status"] == "complete"
    assert driver._agents["tu1"]["message"] == ""
    assert updates == []


def test_child_user_record_is_progress_not_a_root_turn():
    driver, updates, appended = _driver_with_actor()
    driver._dispatch_record(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Reply with BANANA."}],
            },
            "parent_tool_use_id": "tu1",
            "session_id": "child-1",
            "subagent_type": "general-purpose",
        }
    )
    assert appended == []
    assert updates[-1]["tu1"]["status"] == "working"


def test_identical_snippet_does_not_reemit():
    driver, updates, _appended = _driver_with_actor()
    record = _child_assistant("tu1", [{"type": "text", "text": "same"}])
    driver._dispatch_record(record)
    driver._dispatch_record(record)
    assert len(updates) == 1


def test_snippet_prefers_text_collapses_and_tail_truncates():
    snip = ClaudeCliDriver._child_message_snippet
    both = {
        "message": {
            "content": [
                {"type": "thinking", "thinking": "private"},
                {"type": "text", "text": "public  answer"},
            ]
        }
    }
    assert snip(both) == "public answer"
    long_text = "word " * 60
    tail = snip({"message": {"content": [{"type": "text", "text": long_text}]}})
    assert tail.startswith("…")
    assert len(tail) == ClaudeCliDriver._CHILD_SNIPPET_CHARS + 1
    assert snip({"message": {"content": "plain string"}}) == "plain string"
    assert snip({"message": {"content": None}}) == ""
    assert snip({}) == ""
