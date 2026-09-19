"""Claude control-plane fixes from the 2026-09-02 audit (v0.88.0).

Every frame shape here was measured against claude 2.1.258 with throwaway
stream-json processes before the code was written; the probes are recorded
in docs/CLAUDE-PARITY-PLAN.md §"Method and evidence".
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import (  # noqa: E402
    ClaudeCliDriver,
    _approval_detail,
    _normalize_cli_commands,
    _question_answers,
)
from helios.backend.transcript import ToolUse, Turn  # noqa: E402


def _driver(mode: str = "default"):
    driver = ClaudeCliDriver(cwd="/tmp", permission_mode=mode)
    writes: list[dict] = []
    driver._write_stdin_line = writes.append
    driver._proc = object()
    driver._stdin = object()
    return driver, writes


def _asked(driver):
    seen: list[tuple[dict, str]] = []
    driver.connect("question-asked", lambda _d, payload, token: seen.append((payload, token)))
    return seen


def _can_use_tool(tool_name: str, inp: dict, **extra) -> dict:
    request = {
        "subtype": "can_use_tool",
        "tool_name": tool_name,
        "display_name": tool_name,
        "input": inp,
        "tool_use_id": f"toolu_{tool_name}",
    }
    request.update(extra)
    return {"type": "control_request", "request_id": f"req_{tool_name}", "request": request}


def _inner(frame: dict) -> dict:
    return frame["response"]["response"]


# --- AskUserQuestion -------------------------------------------------------


def test_a_question_is_answered_natively_with_one_allow_frame():
    driver, writes = _driver()
    asked = _asked(driver)
    questions = {
        "questions": [
            {
                "question": "Which colours?",
                "header": "Colours",
                "multiSelect": True,
                "options": [{"label": "Red", "description": ""}, {"label": "Blue", "description": ""}],
            },
            {
                "question": "Which size?",
                "header": "Size",
                "multiSelect": False,
                "options": [{"label": "Small", "description": ""}],
            },
        ]
    }
    driver._dispatch_record(_can_use_tool("AskUserQuestion", questions))
    payload, token = asked[0]
    # The dialog gets ids so it returns an answer map; the wire never sees them.
    assert [q["id"] for q in payload["questions"]] == ["0", "1"]
    assert token == "toolu_AskUserQuestion"

    driver.answer_question(token, {"0": {"answers": ["Red", "Blue"]}, "1": {"answers": ["Purple"]}})

    assert len(writes) == 1, "one control_response, no synthetic user turn"
    inner = _inner(writes[0])
    assert inner["behavior"] == "allow"
    assert inner["updatedInput"]["answers"] == {"Which colours?": "Red, Blue", "Which size?": "Purple"}
    assert "id" not in inner["updatedInput"]["questions"][0]
    assert driver._pending_questions == {}


def test_a_dismissed_question_denies_without_a_follow_up():
    driver, writes = _driver()
    asked = _asked(driver)
    driver._dispatch_record(
        _can_use_tool("AskUserQuestion", {"questions": [{"question": "Go?", "options": [{"label": "Yes"}]}]})
    )
    driver.answer_question(asked[0][1], None)
    assert len(writes) == 1
    assert _inner(writes[0])["behavior"] == "deny"


def test_legacy_string_answers_map_onto_question_text():
    questions = [{"question": "Colour?", "header": "Colour"}, {"question": "Size?", "header": "Size"}]
    assert _question_answers(questions, "Colour: Red\nSize: Large") == {"Colour?": "Red", "Size?": "Large"}
    assert _question_answers(questions[:1], "Blue") == {"Colour?": "Blue"}
    assert _question_answers(questions, {"1": {"answers": ["XL"]}}) == {"Size?": "XL"}


# --- Plan review -----------------------------------------------------------


def _plan_request() -> dict:
    return _can_use_tool(
        "ExitPlanMode",
        {"plan": "# Plan\n\n1. Do the thing", "planFilePath": "/home/x/.claude/plans/p.md"},
        requires_user_interaction=True,
    )


def test_plan_mode_presents_the_plan_instead_of_denying_it():
    driver, writes = _driver("plan")
    asked = _asked(driver)
    driver._dispatch_record(_plan_request())
    assert writes == [], "nothing auto-denied"
    payload, token = asked[0]
    assert payload["detail"] == {"kind": "markdown", "title": "Proposed plan", "text": "# Plan\n\n1. Do the thing"}
    assert "p.md" in payload["caption"]
    assert [o["label"] for o in payload["questions"][0]["options"]] == [
        "Approve plan",
        "Approve and auto-accept edits",
        "Keep planning",
    ]
    assert token.startswith("plan:")


def test_approving_a_plan_allows_and_leaves_plan_mode():
    driver, writes = _driver("plan")
    asked = _asked(driver)
    modes: list[str] = []
    driver.connect("permission-mode-changed", lambda _d, mode: modes.append(mode))
    driver._dispatch_record(_plan_request())

    driver.answer_question(asked[0][1], "Approve plan")

    inner = _inner(writes[0])
    assert inner["behavior"] == "allow"
    assert "updatedPermissions" not in inner
    assert modes == ["default"]
    assert driver.permission_mode == "default"


def test_approving_with_auto_accept_sets_the_mode_on_the_wire_and_locally():
    driver, writes = _driver("plan")
    asked = _asked(driver)
    driver._dispatch_record(_plan_request())
    driver.answer_question(asked[0][1], "Approve and auto-accept edits")
    inner = _inner(writes[0])
    assert inner["updatedPermissions"] == [
        {"type": "setMode", "mode": "acceptEdits", "destination": "session"}
    ]
    assert driver.permission_mode == "acceptEdits"


def test_keeping_planning_denies_with_feedback_the_model_reads():
    driver, writes = _driver("plan")
    asked = _asked(driver)
    driver._dispatch_record(_plan_request())
    driver.answer_question(asked[0][1], "Split step 1 into two")
    inner = _inner(writes[0])
    assert inner["behavior"] == "deny"
    assert inner["message"] == "The user declined this tool request: Split step 1 into two"
    assert driver.permission_mode == "plan"


def test_other_writes_in_plan_mode_are_still_denied():
    driver, writes = _driver("plan")
    driver._dispatch_record(_can_use_tool("Write", {"file_path": "/tmp/x", "content": "y"}))
    assert _inner(writes[0])["behavior"] == "deny"


# --- Approvals carry what the CLI suggested --------------------------------


def test_cli_rule_suggestions_become_the_session_option():
    driver, writes = _driver()
    asked = _asked(driver)
    driver._dispatch_record(
        _can_use_tool(
            "Bash",
            {"command": "touch /tmp/a && rm /tmp/a", "description": "probe"},
            description="Create and delete probe file",
            decision_reason_type="subcommandResults",
            permission_suggestions=[
                {
                    "type": "addRules",
                    "behavior": "allow",
                    "destination": "localSettings",
                    "rules": [
                        {"toolName": "Bash", "ruleContent": "touch /tmp/a"},
                        {"toolName": "Bash", "ruleContent": "rm /tmp/a"},
                    ],
                }
            ],
        )
    )
    payload, token = asked[0]
    question = payload["questions"][0]
    assert question["header"] == "Allow Bash?"
    assert "Create and delete probe file" in question["question"]
    assert "sub-commands" in payload["caption"]
    labels = [o["label"] for o in question["options"]]
    assert labels == ["Allow once", "Allow for this session", "Deny"]
    assert payload["allowOther"] is True

    driver.answer_question(token, "Allow for this session")
    inner = _inner(writes[0])
    assert inner["behavior"] == "allow"
    assert inner["updatedPermissions"] == [
        {
            "type": "addRules",
            "behavior": "allow",
            "destination": "session",
            "rules": [
                {"toolName": "Bash", "ruleContent": "touch /tmp/a"},
                {"toolName": "Bash", "ruleContent": "rm /tmp/a"},
            ],
        }
    ]


def test_a_set_mode_suggestion_offers_auto_accept_and_shows_the_content():
    driver, writes = _driver()
    asked = _asked(driver)
    driver._dispatch_record(
        _can_use_tool(
            "Write",
            {"file_path": "/tmp/note.txt", "content": "hello"},
            permission_suggestions=[{"type": "setMode", "mode": "acceptEdits", "destination": "session"}],
        )
    )
    payload, token = asked[0]
    labels = [o["label"] for o in payload["questions"][0]["options"]]
    assert "Allow and auto-accept edits" in labels
    assert payload["detail"]["title"] == "/tmp/note.txt"
    assert "hello" in payload["detail"]["text"]

    driver.answer_question(token, "Allow and auto-accept edits")
    inner = _inner(writes[0])
    assert inner["behavior"] == "allow"
    assert inner["updatedPermissions"] == [
        {"type": "setMode", "mode": "acceptEdits", "destination": "session"}
    ]
    assert driver.permission_mode == "acceptEdits"


def test_free_text_denies_with_the_reason_and_dismiss_denies_plainly():
    driver, writes = _driver()
    asked = _asked(driver)
    driver._dispatch_record(_can_use_tool("Bash", {"command": "rm -rf /tmp/x"}))
    driver.answer_question(asked[0][1], "not that directory")
    assert _inner(writes[0]) == {
        "behavior": "deny",
        "message": "The user declined this tool request: not that directory",
    }

    driver._dispatch_record(_can_use_tool("Bash", {"command": "ls"}))
    driver.answer_question(asked[1][1], None)
    assert _inner(writes[1]) == {"behavior": "deny", "message": "The user declined this tool request."}


def test_approval_detail_prefers_a_diff_and_falls_back_to_content():
    edit = _approval_detail("Edit", {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})
    assert edit is not None and edit["kind"] in {"diff", "code"}
    assert "x = 2" in edit["text"]
    assert _approval_detail("Bash", {"command": "ls"}) is None
    long_command = "echo " + "a" * 200
    assert _approval_detail("Bash", {"command": long_command})["kind"] == "code"
    assert _approval_detail("Read", {"file_path": "a"}) is None


# --- control_cancel_request --------------------------------------------------


def test_a_cancelled_request_is_forgotten_and_the_ui_told():
    driver, _writes = _driver()
    asked = _asked(driver)
    cancelled: list[str] = []
    driver.connect("question-cancelled", lambda _d, token: cancelled.append(token))
    driver._dispatch_record(_can_use_tool("Bash", {"command": "ls"}))
    token = asked[0][1]

    driver._dispatch_record({"type": "control_cancel_request", "request_id": "req_Bash"})

    assert cancelled == [token]
    assert driver._pending_tool_approvals == {}
    # Answering afterwards is a harmless no-op, not a stale frame.
    driver.answer_question(token, "Allow once")
    assert _writes == []


# --- Fan-out projection ------------------------------------------------------


def _seed(driver, actor_id="toolu_1"):
    driver._note_delegation_requests(
        Turn(role="assistant", tool_uses=[ToolUse(name="Agent", input={"subagent_type": "Explore"}, id=actor_id)])
    )


def test_a_second_root_message_does_not_wipe_the_actors():
    driver, _writes = _driver()
    _seed(driver)
    driver._note_task_record("task_started", {"task_id": "t1", "tool_use_id": "toolu_1"})
    driver._dispatch_record(
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg_2", "role": "assistant"}}}
    )
    assert driver.observed_agent_snapshot()["toolu_1"]["status"] == "working"


def test_task_started_before_the_seed_creates_a_provisional_actor():
    driver, _writes = _driver()
    driver._note_task_record(
        "task_started",
        {"task_id": "t9", "tool_use_id": "toolu_9", "description": "Find things", "subagent_type": "Explore"},
    )
    actor = driver.observed_agent_snapshot()["toolu_9"]
    assert actor["status"] == "working"
    assert actor["name"] == "Find things"
    assert actor["task_id"] == "t9"
    # The later seeding pass must not demote it back to starting.
    _seed(driver, "toolu_9")
    assert driver.observed_agent_snapshot()["toolu_9"]["status"] == "working"


def test_stopped_and_unknown_notification_statuses_are_classified_by_stem():
    driver, _writes = _driver()
    for actor_id, status, expected in (
        ("a", "stopped", "stopped"),
        ("b", "killed", "stopped"),
        ("c", "completed", "complete"),
        ("d", "timed_out", "error"),
    ):
        _seed(driver, actor_id)
        driver._note_task_record("task_notification", {"tool_use_id": actor_id, "status": status})
        assert driver.observed_agent_snapshot()[actor_id]["status"] == expected


def test_stop_task_sends_the_control_request_for_a_live_child():
    driver, writes = _driver()
    _seed(driver)
    assert driver.stop_task("toolu_1") is False, "no task id yet"
    driver._note_task_record("task_started", {"task_id": "t1", "tool_use_id": "toolu_1"})
    assert driver.stop_task("toolu_1") is True
    frame = writes[-1]
    assert frame["type"] == "control_request"
    assert frame["request"] == {"subtype": "stop_task", "task_id": "t1"}
    driver._note_task_record("task_notification", {"tool_use_id": "toolu_1", "status": "stopped"})
    assert driver.stop_task("toolu_1") is False, "terminal actors are not stopped again"


# --- initialize ---------------------------------------------------------------


def test_initialize_declares_per_task_stop_and_keeps_the_command_inventory():
    driver, writes = _driver()
    seen: list[list] = []
    driver.connect("commands-updated", lambda _d, commands: seen.append(commands))
    driver._initialize_control_protocol()
    init = next(w for w in writes if w.get("request", {}).get("subtype") == "initialize")
    assert init["request"]["perTaskStopAffordance"] is True
    assert init["request"]["hooks"] is None

    driver._dispatch_record(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": init["request_id"],
                "response": {
                    "models": [],
                    "commands": [
                        {"name": "compact", "description": "Compact context", "argumentHint": "<focus>"},
                        {"name": "compact", "description": "dup"},
                        {"name": "", "description": "blank"},
                    ],
                },
            },
        }
    )
    assert driver.cli_commands == [
        {"name": "compact", "description": "Compact context", "argument_hint": "<focus>", "source": "claude"}
    ]
    assert seen == [driver.cli_commands]


def test_normalize_cli_commands_strips_slashes_and_ignores_junk():
    assert _normalize_cli_commands([{"name": "/review"}, "x", None]) == [
        {"name": "review", "description": "", "argument_hint": "", "source": "claude"}
    ]
    assert _normalize_cli_commands("nope") == []


def test_hook_records_are_forwarded_without_the_envelope():
    driver, _writes = _driver()
    seen: list[dict] = []
    driver.connect("hook-event", lambda _d, payload: seen.append(payload))
    driver._dispatch_record(
        {
            "type": "system",
            "subtype": "hook_response",
            "uuid": "u",
            "session_id": "s",
            "hook_event": "PreToolUse",
            "hook_name": "guard",
            "outcome": "success",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
    )
    assert seen == [
        {
            "subtype": "hook_response",
            "hook_event": "PreToolUse",
            "hook_name": "guard",
            "outcome": "success",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        }
    ]
