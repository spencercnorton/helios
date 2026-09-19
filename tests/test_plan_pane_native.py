from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("gi")

from helios.backend.execution_plan import ExecutionPlan, ExecutionPlanStep
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.agent_activity import AgentActivityModel, AgentActivityScope
from helios.backend.plan_summary import summarize_turns
from helios.backend.transcript import ContentSpan, ToolUse, Turn
from helios.widgets import plan_pane as plan_pane_module
from helios.widgets.plan_pane import PlanPane, _diff_summary, _native_agent_states


def test_native_plan_remains_authoritative_until_next_native_turn():
    pane = PlanPane()
    pane.show_native_plan(
        {"plan": [{"step": "Use App Server", "status": "inProgress"}]}
    )
    native = pane._native_plan_summary

    pane.show_streaming(None)

    assert pane._native_plan_summary is native
    assert [step.text for step in native.steps] == ["Use App Server"]

    pane.begin_native_turn()
    assert pane._native_plan_summary is None


def test_durable_execution_plan_survives_next_native_turn_boundary():
    pane = PlanPane()
    plan = ExecutionPlan(
        work_id="work-1",
        plan_id="plan-1",
        revision=1,
        provider="openai",
        participant_id="part-1",
        participant_generation=1,
        native_turn_id="turn-1",
        explanation="Building the feature",
        steps=(
            ExecutionPlanStep("task-1", "Inspect", "completed"),
            ExecutionPlanStep("task-2", "Build", "inProgress"),
        ),
        status="active",
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-01T00:00:00Z",
    )
    pane.show_execution_plan(plan)

    pane.begin_native_turn()

    assert pane._execution_plan_summary is not None
    assert pane._progress.get_label() == "1/2 tasks complete · revision 1"
    assert [step.text for step in pane._execution_plan_summary.steps] == [
        "Inspect",
        "Build",
    ]


def test_durable_task_revision_updates_stable_row_in_place():
    pane = PlanPane()
    first = ExecutionPlan(
        work_id="work-1",
        plan_id="plan-1",
        revision=1,
        provider="openai",
        participant_id="part-1",
        participant_generation=1,
        native_turn_id="turn-1",
        explanation="",
        steps=(ExecutionPlanStep("task-stable", "Build", "inProgress"),),
        status="active",
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-01T00:00:00Z",
    )
    pane.show_execution_plan(first)
    row = pane._step_rows["task-stable"]

    pane.show_execution_plan(
        replace(
            first,
            revision=2,
            steps=(
                ExecutionPlanStep(
                    "task-stable",
                    "Build and verify",
                    "completed",
                    evidence="Focused tests passed",
                ),
            ),
            status="completed",
        )
    )

    assert pane._step_rows["task-stable"] is row
    assert row._label.get_label() == "Build and verify"
    assert row._detail.get_label() == "Focused tests passed"
    assert row._status == "done"


def test_streaming_plan_uses_staged_latest_user_request_boundary(monkeypatch):
    pane = PlanPane()
    old_user = Turn(role="user", text_parts=["Old request"])
    old_assistant = Turn(role="assistant", text_parts=["- [ ] stale pane step"])
    old_assistant.tool_uses.append(
        ToolUse(name="Bash", input={"command": "pytest stale-pane-suite"})
    )
    current_user = Turn(role="user", text_parts=["Current request"])
    for turn in (old_user, old_assistant, current_user):
        pane.append_turn(turn)

    summaries = []
    real_summarize = plan_pane_module.summarize_streaming

    def capture(streaming, turns):
        summary = real_summarize(streaming, turns)
        summaries.append(summary)
        return summary

    monkeypatch.setattr(plan_pane_module, "summarize_streaming", capture)
    pane.show_streaming(
        StreamingAssistant(
            blocks=[Block(type="commentary", text="Working on the current request")]
        )
    )

    summary = summaries[-1]
    assert "stale pane step" not in [step.text for step in summary.steps]
    assert summary.active_detail != "pytest stale-pane-suite"
    assert {phase.key: phase.status for phase in summary.phases}["handoff"] == (
        "pending"
    )


def test_delayed_session_load_merges_staged_user_boundary():
    pane = PlanPane()
    pane._session_id = "session-current"
    pane._token = 10
    current_user = Turn(role="user", text_parts=["Newest accepted request"])

    pane.append_turn(current_user)
    assert pane._token == 10

    stale_user = Turn(role="user", text_parts=["Old request"])
    stale_assistant = Turn(role="assistant", text_parts=["- [ ] stale loaded step"])
    stale_turns = [stale_user, stale_assistant]
    pane._apply_session_state(
        summarize_turns(stale_turns),
        [],
        stale_turns,
        10,
        "session-current",
    )

    assert pane._turns == [stale_user, stale_assistant, current_user]
    assert pane._pending_turns == [current_user]


def test_delayed_session_load_merges_live_final_and_restores_history():
    pane = PlanPane()
    pane._session_id = "session-current"
    pane._token = 10
    current_user = Turn(role="user", text_parts=["Current request"])
    edited = Turn(role="assistant")
    edited.tool_uses.append(ToolUse(name="Edit", input={"file_path": "current.py"}))
    local_final = Turn(role="assistant", text_parts=["Current final answer"])

    pane.append_turn(local_final)
    loaded = [current_user, edited]
    pane._apply_session_state(
        summarize_turns(loaded),
        [],
        loaded,
        10,
        "session-current",
    )

    assert pane._turns == [current_user, edited, local_final]
    assert pane._pending_turns == [local_final]
    assert any(
        insight.title == "File change outcome unknown" for insight in pane._insights
    )


def test_delayed_session_load_deduplicates_persisted_local_suffix():
    pane = PlanPane()
    pane._session_id = "session-current"
    pane._token = 10
    local_user = Turn(role="user", text_parts=["Current request"])
    local_final = Turn(role="assistant", text_parts=["Current final answer"])
    pane.append_turn(local_user)
    pane.append_turn(local_final)

    persisted_user = Turn(
        role="user",
        text_parts=["Current request"],
        uuid="persisted-user",
        timestamp="2026-07-15T12:00:00Z",
    )
    persisted_final = Turn(
        role="assistant",
        text_parts=["Current final answer"],
        uuid="persisted-final",
        timestamp="2026-07-15T12:00:01Z",
    )
    loaded = [persisted_user, persisted_final]
    pane._apply_session_state(
        summarize_turns(loaded),
        [],
        loaded,
        10,
        "session-current",
    )

    assert pane._turns == loaded
    assert pane._pending_turns == []


def test_delayed_load_deduplicates_claude_split_assistant_records():
    pane = PlanPane()
    pane._session_id = "session-current"
    pane._token = 10
    local = Turn(
        role="assistant",
        content=[
            ContentSpan("thinking", "Inspecting"),
            ContentSpan("text", "Current final answer"),
        ],
        tool_uses=[ToolUse(name="Read", input={"file_path": "current.py"}, id="r1")],
    )
    pane.append_turn(local)

    persisted_thinking = Turn(
        role="assistant",
        content=[ContentSpan("thinking", "Inspecting")],
        uuid="persisted-thinking",
    )
    persisted_tool = Turn(
        role="assistant",
        tool_uses=[
            ToolUse(
                name="Read",
                input={"file_path": "current.py", "display_only": "elided"},
                id="r1",
            )
        ],
        uuid="persisted-tool",
    )
    persisted_text = Turn(
        role="assistant",
        content=[ContentSpan("text", "Current final answer")],
        uuid="persisted-text",
    )
    loaded = [persisted_thinking, persisted_tool, persisted_text]
    pane._apply_session_state(
        summarize_turns(loaded),
        [],
        loaded,
        10,
        "session-current",
    )

    assert pane._turns == loaded
    assert pane._pending_turns == []


def test_prior_session_load_is_rejected_after_navigation():
    pane = PlanPane()
    pane._session_id = "session-current"
    pane._token = 11
    current_user = Turn(role="user", text_parts=["Current request"])
    pane._turns = [current_user]
    pane._pending_turns = [current_user]
    prior_turns = [Turn(role="user", text_parts=["Prior session request"])]

    pane._apply_session_state(
        summarize_turns(prior_turns),
        [],
        prior_turns,
        10,
        "session-prior",
    )

    assert pane._turns == [current_user]
    assert pane._pending_turns == [current_user]


def test_native_turn_start_clears_prior_turn_agents():
    pane = PlanPane()
    pane.show_native_agents({"child": {"name": "Reviewer", "status": "completed"}})
    assert pane._agents_section.get_visible() is True

    pane.begin_native_turn()

    assert pane._agents_section.get_visible() is False
    assert pane._agents_box.get_first_child() is None


def test_plan_pane_consumes_provider_neutral_observed_activity() -> None:
    pane = PlanPane()
    model = AgentActivityModel()
    scope = AgentActivityScope("openai", "work-one", "turn-one", "root")
    model.begin_scope(scope)
    snapshot = model.observe(
        scope,
        {
            "child": {
                "status": "needsInput",
                "name": "Reviewer",
                "message": "Choose an approach",
            }
        },
    )

    pane.show_agent_activity(snapshot)

    assert pane._agents_title.get_label() == "Observed agents"
    assert pane._agents_section.get_visible() is True
    assert pane._agents_box.get_first_child() is not None

    pane.show_agent_activity(model.reset())
    assert pane._agents_section.get_visible() is False


def test_native_diff_replaces_the_previous_aggregate_snapshot():
    pane = PlanPane()
    pane.show_native_diff({"diff": "+first\n"})

    pane.show_native_diff({"diff": "+second\n"})

    assert pane._native_diff == "+second\n"
    assert pane._changes_summary.get_label() == "Aggregate diff · 0 files · +1 −0"


def test_diff_summary_counts_latest_aggregate_files_and_lines():
    diff = """diff --git a/src/one.py b/src/one.py
--- a/src/one.py
+++ b/src/one.py
@@ -1 +1,2 @@
-before
+after
+again
diff --git a/tests/test_one.py b/tests/test_one.py
--- a/tests/test_one.py
+++ b/tests/test_one.py
@@ -0,0 +1 @@
+assert True
"""

    assert _diff_summary(diff) == ("src/one.py, tests/test_one.py · 2 files · +3 −1")


def test_native_agent_states_accepts_protocol_item_and_sorts_keys():
    snapshot = {
        "item": {
            "type": "collabAgentToolCall",
            "agentsStates": {
                "thread-b": {"status": "completed"},
                "thread-a": {"status": "running", "message": "Inspecting"},
            },
        }
    }

    assert _native_agent_states(snapshot) == [
        ("thread-a", {"status": "running", "message": "Inspecting"}),
        ("thread-b", {"status": "completed"}),
    ]
