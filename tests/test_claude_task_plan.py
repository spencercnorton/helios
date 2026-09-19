"""Claude's task list as the durable execution plan, subagent-originated prompts,
and thinking-token activity (v0.89.0, parity plan Phase 3B/3D).

Shapes measured on claude 2.1.258 with CLAUDE_CODE_ENABLE_TODO_TOOLS=1
(2026-09-03): TaskCreate {subject, description} -> "Task #N created
successfully: <subject>"; TaskUpdate {taskId, status} -> "Updated task #N
status"; TaskList {} -> "#N [status] subject" lines; a child-originated
can_use_tool carries `agent_id` == the child's task id.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import (  # noqa: E402
    INTERACTIVE_CHILD_ENV,
    ClaudeCliDriver,
    _parse_task_list,
    _plan_status,
)
from helios.backend.transcript import ToolResult, ToolUse, Turn  # noqa: E402
from helios.widgets.activity_indicator import (  # noqa: E402
    STATE_AGENT,
    STATE_THINKING,
    native_activity_state,
)


def _driver():
    driver = ClaudeCliDriver(cwd="/tmp", permission_mode="default")
    driver._write_stdin_line = (lambda frames: frames.append)([])
    driver._proc = object()
    driver._stdin = object()
    driver._session_id = "sess"
    return driver


def _plans(driver):
    seen: list[dict] = []
    driver.connect("plan-updated", lambda _d, payload: seen.append(payload))
    return seen


def _assistant(*uses):
    return Turn(role="assistant", tool_uses=list(uses))


def _results(*pairs):
    return Turn(role="tool", tool_results=[ToolResult(tool_use_id=i, content=c) for i, c in pairs])


def test_task_create_waits_for_its_id_then_emits_the_codex_shape():
    driver = _driver()
    plans = _plans(driver)
    driver._reset_observed_agents("turn-7")
    driver._note_task_tools(
        _assistant(ToolUse(name="TaskCreate", input={"subject": "Say hello"}, id="tu1"))
    )
    assert plans == [], "no id yet, nothing to show"

    driver._note_task_tool_results(_results(("tu1", "Task #1 created successfully: Say hello")))

    assert plans == [
        {
            "threadId": "sess",
            "turnId": "turn-7",
            "explanation": None,
            "plan": [{"step": "Say hello", "status": "pending"}],
            "source": "turn",
            "authoritative": True,
        }
    ]


def test_task_update_moves_status_and_deleted_removes():
    driver = _driver()
    plans = _plans(driver)
    driver._note_task_tools(
        _assistant(
            ToolUse(name="TaskCreate", input={"subject": "A"}, id="c1"),
            ToolUse(name="TaskCreate", input={"subject": "B"}, id="c2"),
        )
    )
    driver._note_task_tool_results(
        _results(("c1", "Task #1 created successfully: A"), ("c2", "Task #2 created successfully: B"))
    )
    driver._note_task_tools(
        _assistant(ToolUse(name="TaskUpdate", input={"taskId": "1", "status": "in_progress"}, id="u1"))
    )
    driver._note_task_tool_results(_results(("u1", "Updated task #1 status")))
    assert plans[-1]["plan"] == [
        {"step": "A", "status": "inProgress"},
        {"step": "B", "status": "pending"},
    ]
    driver._note_task_tools(
        _assistant(
            ToolUse(name="TaskUpdate", input={"taskId": "1", "status": "completed"}, id="u2"),
            ToolUse(name="TaskUpdate", input={"taskId": "2", "status": "deleted"}, id="u3"),
        )
    )
    driver._note_task_tool_results(_results(("u2", "Updated task #1 status"), ("u3", "Updated task #2 status")))
    assert plans[-1]["plan"] == [{"step": "A", "status": "completed"}]


def test_an_errored_task_tool_result_changes_nothing():
    driver = _driver()
    plans = _plans(driver)
    driver._note_task_tools(_assistant(ToolUse(name="TaskCreate", input={"subject": "X"}, id="c1")))
    driver._note_task_tool_results(
        Turn(role="tool", tool_results=[ToolResult(tool_use_id="c1", content="boom", is_error=True)])
    )
    assert plans == []


def test_task_list_output_resyncs_the_whole_plan():
    driver = _driver()
    plans = _plans(driver)
    driver._note_task_tools(_assistant(ToolUse(name="TaskList", input={}, id="l1")))
    driver._note_task_tool_results(
        _results(("l1", "#1 [completed] Say hello\n#2 [in_progress] Say goodbye\n#3 [pending] Wave"))
    )
    assert plans[-1]["plan"] == [
        {"step": "Say hello", "status": "completed"},
        {"step": "Say goodbye", "status": "inProgress"},
        {"step": "Wave", "status": "pending"},
    ]
    assert _parse_task_list("no tasks") == {}


def test_todowrite_applies_at_once_for_older_clis():
    driver = _driver()
    plans = _plans(driver)
    driver._note_task_tools(
        _assistant(
            ToolUse(
                name="TodoWrite",
                input={
                    "todos": [
                        {"content": "one", "status": "completed"},
                        {"content": "two", "status": "in_progress"},
                        {"content": "", "status": "pending"},
                    ]
                },
                id="t1",
            )
        )
    )
    assert plans[-1]["plan"] == [
        {"step": "one", "status": "completed"},
        {"step": "two", "status": "inProgress"},
    ]


def test_plan_status_vocabulary():
    assert _plan_status("in_progress") == "inProgress"
    assert _plan_status("inProgress") == "inProgress"
    assert _plan_status("done") == "completed"
    assert _plan_status("weird") == "pending"
    assert _plan_status(None) == "pending"


def test_the_interactive_spawn_turns_the_task_tools_on():
    assert INTERACTIVE_CHILD_ENV == {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"}


# --- subagent-originated prompts ------------------------------------------


def _seed_child(driver, actor_id="toolu_child", task_id="a689dc80"):
    driver._note_delegation_requests(
        Turn(role="assistant", tool_uses=[ToolUse(name="Agent", input={"description": "Prober"}, id=actor_id)])
    )
    driver._note_task_record("task_started", {"task_id": task_id, "tool_use_id": actor_id})


def test_a_subagents_approval_names_it_and_marks_it_waiting():
    driver = _driver()
    asked: list[tuple[dict, str]] = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    snapshots: list[dict] = []
    driver.connect("agents-updated", lambda _d, snap: snapshots.append(snap))
    _seed_child(driver)

    driver._dispatch_record(
        {
            "type": "control_request",
            "request_id": "r1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "touch /tmp/x"},
                "tool_use_id": "toolu_bash",
                "agent_id": "a689dc80",
            },
        }
    )
    payload, token = asked[0]
    assert payload["caption"].startswith("Asked by subagent: Prober.")
    assert snapshots[-1]["toolu_child"]["status"] == "needs_input"

    driver.answer_question(token, "Allow once")
    assert snapshots[-1]["toolu_child"]["status"] == "working"


def test_a_subagents_question_is_released_on_cancel_and_unknown_agents_are_named_generically():
    driver = _driver()
    asked: list[tuple[dict, str]] = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    snapshots: list[dict] = []
    driver.connect("agents-updated", lambda _d, snap: snapshots.append(snap))
    _seed_child(driver)

    driver._dispatch_record(
        {
            "type": "control_request",
            "request_id": "q1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": [{"question": "Go?", "options": [{"label": "Yes"}]}]},
                "tool_use_id": "toolu_q",
                "agent_id": "a689dc80",
            },
        }
    )
    assert asked[0][0]["caption"] == "Asked by subagent: Prober."
    assert snapshots[-1]["toolu_child"]["status"] == "needs_input"
    driver._dispatch_record({"type": "control_cancel_request", "request_id": "q1"})
    assert snapshots[-1]["toolu_child"]["status"] == "working"

    driver._dispatch_record(
        {
            "type": "control_request",
            "request_id": "r2",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "ls"},
                "tool_use_id": "toolu_b2",
                "agent_id": "not-an-actor",
            },
        }
    )
    assert asked[-1][0]["caption"].startswith("Asked by a subagent.")


# --- thinking tokens -------------------------------------------------------


def test_thinking_tokens_become_activity_with_a_count():
    driver = _driver()
    seen: list[dict] = []
    driver.connect("activity-updated", lambda _d, payload: seen.append(payload))
    driver._dispatch_record(
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50, "estimated_tokens_delta": 50}
    )
    driver._dispatch_record({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 0})
    assert seen == [{"category": "thinking", "tokens": 50}]
    assert native_activity_state({"category": "thinking", "tokens": 1200}) == (
        STATE_THINKING,
        "~1,200 thinking tokens",
    )
    assert native_activity_state({"category": "agents", "count": 1}) == (STATE_AGENT, "1 subagent running")
    assert native_activity_state({"category": "agents", "count": 3}) == (STATE_AGENT, "3 subagents running")


def test_results_that_land_before_message_stop_are_not_lost():
    """Measured on the live wire (2026-09-03): the CLI emits the tool_result
    user records BEFORE the message_stop that finalizes the assistant message,
    so the result routinely precedes the call's registration."""
    driver = _driver()
    plans = _plans(driver)
    driver._reset_observed_agents("turn-1")
    # Results first…
    driver._note_task_tool_results(
        _results(("c1", "Task #1 created successfully: Say hello"), ("c2", "Task #2 created successfully: Say goodbye"))
    )
    assert plans == []
    # …then the assistant message that made the calls.
    driver._note_task_tools(
        _assistant(
            ToolUse(name="TaskCreate", input={"subject": "Say hello"}, id="c1"),
            ToolUse(name="TaskCreate", input={"subject": "Say goodbye"}, id="c2"),
        )
    )
    assert plans[-1]["plan"] == [
        {"step": "Say hello", "status": "pending"},
        {"step": "Say goodbye", "status": "pending"},
    ]
    # Same for an update arriving early.
    driver._note_task_tool_results(_results(("u1", "Updated task #1 status")))
    driver._note_task_tools(
        _assistant(ToolUse(name="TaskUpdate", input={"taskId": "1", "status": "completed"}, id="u1"))
    )
    assert plans[-1]["plan"][0] == {"step": "Say hello", "status": "completed"}
    assert driver._early_task_results == {}
    # A result that is not task-shaped is never buffered.
    driver._note_task_tool_results(_results(("x9", "File created successfully at: /tmp/a")))
    assert driver._early_task_results == {}


# --- GPT-review fixes (2026-09-03) -----------------------------------------


def test_an_errored_task_tool_result_still_clears_its_registration():
    driver = _driver()
    driver._note_task_tools(_assistant(ToolUse(name="TaskCreate", input={"subject": "X"}, id="c1")))
    driver._note_task_tool_results(
        Turn(role="tool", tool_results=[ToolResult(tool_use_id="c1", content="boom", is_error=True)])
    )
    assert driver._pending_task_tools == {}


def test_a_waiting_subagent_stays_waiting_until_its_last_prompt_is_answered():
    driver = _driver()
    asked: list[tuple[dict, str]] = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    _seed_child(driver)

    def prompt(request_id, tool_use_id):
        driver._dispatch_record(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "ls"},
                    "tool_use_id": tool_use_id,
                    "agent_id": "a689dc80",
                },
            }
        )

    prompt("r1", "t1")
    prompt("r2", "t2")
    assert driver.observed_agent_snapshot()["toolu_child"]["status"] == "needs_input"
    # Child output while a prompt is open must not repaint it as working.
    driver._note_child_progress("toolu_child")
    driver._note_child_message("toolu_child", {"message": {"content": [{"type": "text", "text": "hi"}]}})
    assert driver.observed_agent_snapshot()["toolu_child"]["status"] == "needs_input"

    driver.answer_question(asked[0][1], "Allow once")
    assert driver.observed_agent_snapshot()["toolu_child"]["status"] == "needs_input", "one prompt still open"
    driver.answer_question(asked[1][1], "Allow once")
    assert driver.observed_agent_snapshot()["toolu_child"]["status"] == "working"


def test_an_early_failed_result_leaves_no_permanent_pending_entry():
    """A review finding: a failed task-tool result can arrive before the call is
    registered; without a tombstone the registration sat in the pending map
    forever and grew session state without bound."""
    driver = _driver()
    plans = _plans(driver)

    # Result first (error), registration second — the live wire order.
    driver._note_task_tool_results(
        Turn(role="tool", tool_results=[ToolResult(tool_use_id="c9", content="quota", is_error=True)])
    )
    driver._note_task_tools(_assistant(ToolUse(name="TaskCreate", input={"subject": "X"}, id="c9")))

    assert driver._pending_task_tools == {}, "no registration may outlive its failed result"
    assert driver._early_task_results == {}, "the tombstone is consumed"
    assert plans == [], "a failed create adds no task"

    # The buffer stays bounded even under a storm of early failures.
    for i in range(80):
        driver._note_task_tool_results(
            Turn(role="tool", tool_results=[ToolResult(tool_use_id=f"z{i}", content="no", is_error=True)])
        )
    assert len(driver._early_task_results) <= 64
