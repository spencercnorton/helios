from __future__ import annotations

import json

import pytest

from helios.backend.execution_plan import ExecutionPlan, ExecutionPlanStep
from helios.backend.plan_summary import (
    summarize_execution_plan,
    summarize_native_plan,
    summarize_streaming,
    summarize_turns,
)
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.transcript import ToolUse, Turn


def test_later_commentary_revision_beats_stale_reasoning_across_stream_final_reload(
    monkeypatch, tmp_path
):
    """Regression (H0.5 finding #1): an earlier reasoning summary proposes one
    plan, a LATER commentary revises it. The live plan, the finalized-turn plan,
    and the reloaded-transcript plan must ALL select the newer commentary — the
    stale reasoning must never resurface because lanes lost source order."""
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")

    # Source order: stale reasoning first, revised commentary second.
    streaming = StreamingAssistant(model="gpt-5.6-sol")
    streaming.blocks.append(
        Block(type="reasoning_summary", text="Plan:\n- [ ] old approach")
    )
    streaming.blocks.append(
        Block(type="commentary", text="Revised plan:\n- [ ] new approach")
    )

    def step_texts(summary):
        return [s.text for s in summary.steps]

    live = step_texts(summarize_streaming(streaming))
    assert live == ["new approach"]  # live picks the newer commentary

    final_turn = streaming.to_turn()
    final = step_texts(summarize_turns([final_turn]))
    assert final == live  # finalize agrees with live

    writer = CodexTranscriptWriter("/p", thread_id="tid-plan")
    writer.append_assistant(final_turn, model="gpt-5.6-sol")
    path = tmp_path / "projects" / P.encode_project_dirname("/p") / "tid-plan.jsonl"
    reloaded = parse_transcript(path)[0]
    assert step_texts(summarize_turns([reloaded])) == live  # reload agrees too

    # And the stale approach never wins at any stage.
    assert "old approach" not in final and "old approach" not in live


def _assert_plan_identical_across_stream_final_reload(
    monkeypatch, tmp_path, thread_id, blocks, prompt="do the work"
):
    """Live, finalized, and reloaded PlanSummary must be FULLY equal (dataclass
    equality, not just steps) for the same content plus a realistic user turn."""
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")

    stream = StreamingAssistant(model="gpt-5.6-sol")
    stream.blocks.extend(blocks)
    user = Turn(role="user")
    user.text_parts.append(prompt)
    live = summarize_streaming(stream, [user])

    final_turn = stream.to_turn()
    final = summarize_turns([user, final_turn])

    w = CodexTranscriptWriter("/p", thread_id=thread_id)
    w.note_user_text(prompt)
    w.append_assistant(final_turn, model="gpt-5.6-sol")
    path = tmp_path / "projects" / P.encode_project_dirname("/p") / f"{thread_id}.jsonl"
    reloaded = summarize_turns(parse_transcript(path))

    assert live == final, f"live != final:\n{live}\n{final}"
    assert final == reloaded, f"final != reloaded:\n{final}\n{reloaded}"
    return live


@pytest.mark.parametrize(
    ("case", "blocks"),
    [
        ("empty", []),
        (
            "reasoning",
            [Block(type="reasoning_summary", text="Inspecting the new request.")],
        ),
        (
            "commentary",
            [Block(type="commentary", text="Still working on the new request.")],
        ),
        (
            "read",
            [
                Block(
                    type="tool_use",
                    tool_use_name="Read",
                    tool_use_id="read-current",
                    tool_use_input_json='{"file_path": "current.py"}',
                )
            ],
        ),
        (
            "reasoning-commentary",
            [
                Block(type="reasoning_summary", text="Inspecting current state."),
                Block(type="commentary", text="Checking one more current detail."),
            ],
        ),
    ],
)
def test_latest_request_excludes_prior_plan_tools_and_final_state_across_reload(
    monkeypatch, tmp_path, case, blocks
):
    """A step-less second request cannot inherit the first request's plan."""
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import (
        CONTENT_SCHEMA,
        CodexTranscriptWriter,
    )
    from helios.backend.transcript import CONTENT_SCHEMA_KEY, parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")

    prior_user = Turn(role="user")
    prior_user.text_parts.append("Finish the old request")
    prior_assistant = Turn(role="assistant")
    prior_assistant.add("text", "Old final answer.\n- [ ] stale prior step")
    prior_assistant.tool_uses.append(
        ToolUse(name="Bash", input={"command": "python -m pytest old_suite"})
    )
    current_user = Turn(role="user")
    current_user.text_parts.append("Handle this new request")
    history = [prior_user, prior_assistant, current_user]

    stream = StreamingAssistant(model="gpt-5.6-sol", blocks=list(blocks))
    live = summarize_streaming(stream, history)
    final_turn = stream.to_turn()
    final = summarize_turns([*history, final_turn])

    thread_id = f"tid-current-{case}"
    writer = CodexTranscriptWriter("/p", thread_id=thread_id)
    writer.note_user_text(prior_user.text)
    writer.append_assistant(prior_assistant, model="gpt-5.6-sol")
    writer.note_user_text(current_user.text)
    writer.append_assistant(final_turn, model="gpt-5.6-sol")
    path = tmp_path / "projects" / P.encode_project_dirname("/p") / f"{thread_id}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert all(record[CONTENT_SCHEMA_KEY] == CONTENT_SCHEMA for record in records)
    reloaded = summarize_turns(parse_transcript(path))

    assert live == final == reloaded
    assert "stale prior step" not in [step.text for step in live.steps]
    assert live.active_detail != "python -m pytest old_suite"
    assert {phase.key: phase.status for phase in live.phases}["handoff"] == "pending"


def test_current_request_todowrite_still_wins_over_newer_content_and_old_history(
    monkeypatch, tmp_path
):
    """Request slicing retains the existing TodoWrite-over-content rule."""
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    old_user = Turn(role="user", text_parts=["Old request"])
    old_assistant = Turn(role="assistant", text_parts=["- [ ] stale step"])
    current_user = Turn(role="user", text_parts=["New request"])
    history = [old_user, old_assistant, current_user]
    stream = StreamingAssistant(
        model="gpt-5.6-sol",
        blocks=[
            Block(
                type="tool_use",
                tool_use_name="TodoWrite",
                tool_use_id="todo-current",
                tool_use_input_json=(
                    '{"todos":[{"content":"current todo","status":"in_progress"}]}'
                ),
            ),
            Block(type="commentary", text="Revision:\n- [ ] newer content step"),
        ],
    )

    live = summarize_streaming(stream, history)
    final_turn = stream.to_turn()
    final = summarize_turns([*history, final_turn])
    writer = CodexTranscriptWriter("/p", thread_id="tid-current-todo")
    writer.note_user_text(old_user.text)
    writer.append_assistant(old_assistant)
    writer.note_user_text(current_user.text)
    writer.append_assistant(final_turn)
    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-current-todo.jsonl"
    )
    reloaded = summarize_turns(parse_transcript(path))

    assert live == final == reloaded
    assert [step.text for step in live.steps] == ["current todo"]


@pytest.mark.parametrize("text", ["", "   \n\t"], ids=["empty", "whitespace"])
@pytest.mark.parametrize(
    "tool",
    [
        None,
        Block(
            type="tool_use",
            tool_use_name="Read",
            tool_use_id="read-blank-final",
            tool_use_input_json='{"file_path":"a.py"}',
        ),
        Block(
            type="tool_use",
            tool_use_name="Edit",
            tool_use_id="edit-blank-final",
            tool_use_input_json='{"file_path":"a.py"}',
        ),
        Block(
            type="tool_use",
            tool_use_name="Bash",
            tool_use_id="bash-blank-final",
            tool_use_input_json='{"command":"python -m pytest -q"}',
        ),
    ],
    ids=["no-tool", "read", "edit", "verify"],
)
def test_empty_or_whitespace_text_is_not_a_final_answer_across_reload(
    monkeypatch, tmp_path, text, tool
):
    blocks = [Block(type="text", text=text)]
    if tool is not None:
        blocks.append(tool)
    summary = _assert_plan_identical_across_stream_final_reload(
        monkeypatch,
        tmp_path,
        f"tid-blank-{tool.tool_use_name if tool else 'none'}-{len(text)}",
        blocks,
    )

    assert {phase.key: phase.status for phase in summary.phases}["handoff"] == (
        "pending"
    )


def test_plan_summary_equal_stream_final_reload_reasoning_then_commentary(
    monkeypatch, tmp_path
):
    """reasoning summary + revised commentary, NO final answer: live/final/reload
    PlanSummary must be identical (the assistant_seen final-text rule keeps
    handoff pending in all three)."""
    summary = _assert_plan_identical_across_stream_final_reload(
        monkeypatch,
        tmp_path,
        "tid-eq-rc",
        [
            Block(type="reasoning_summary", text="Plan:\n- [ ] old approach"),
            Block(type="commentary", text="Revised:\n- [ ] new approach"),
        ],
    )
    assert [s.text for s in summary.steps] == ["new approach"]
    status = {p.key: p.status for p in summary.phases}
    assert status["handoff"] == "pending"  # no final answer -> not handed off


def test_plan_summary_equal_stream_final_reload_commentary_only(monkeypatch, tmp_path):
    """commentary-only with a checkbox plan: identical across all three stages."""
    _assert_plan_identical_across_stream_final_reload(
        monkeypatch,
        tmp_path,
        "tid-eq-c",
        [Block(type="commentary", text="Progress:\n- [x] done step\n- [ ] todo step")],
    )


def test_plan_summary_equal_stream_final_reload_truly_step_less(monkeypatch, tmp_path):
    """Plain work updates with no bullets or checkboxes stay fully identical.

    This exercises the fallback-step path and its source label; checkbox-based
    tests produce explicit steps and therefore cannot catch live-vs-inferred
    source drift.
    """
    summary = _assert_plan_identical_across_stream_final_reload(
        monkeypatch,
        tmp_path,
        "tid-eq-no-steps",
        [
            Block(type="reasoning_summary", text="I will inspect the parser next."),
            Block(type="commentary", text="I am checking the persisted record now."),
        ],
    )
    assert summary.source == "inferred"
    assert [step.text for step in summary.steps] == [
        "Shape the implementation plan from the request."
    ]


def test_plan_summary_equal_stream_final_reload_todowrite(monkeypatch, tmp_path):
    """TodoWrite plan with a realistic prior user turn: identical across all
    three stages."""
    _assert_plan_identical_across_stream_final_reload(
        monkeypatch,
        tmp_path,
        "tid-eq-todo",
        [
            Block(
                type="tool_use",
                tool_use_name="TodoWrite",
                tool_use_id="t1",
                tool_use_input_json=(
                    '{"todos": [{"content": "step A", "status": "in_progress"},'
                    ' {"content": "step B", "status": "pending"}]}'
                ),
            )
        ],
    )


def test_todowrite_priority_is_consistent_across_stream_and_final():
    """A TodoWrite plan followed by a LATER commentary revision must resolve to
    the SAME steps live and finalized — the live summarizer and the finalized
    _steps_from_turn share one 'TodoWrite wins, else newest content' rule, so
    they never disagree (no stale/flip-flop plan on finalize)."""
    from helios.backend.process.streaming import Block, StreamingAssistant

    s = StreamingAssistant(model="gpt-5.6-sol")
    s.blocks.append(
        Block(
            type="tool_use",
            tool_use_name="TodoWrite",
            tool_use_id="t1",
            tool_use_input_json=(
                '{"todos": [{"content": "todo plan step", "status": "pending"}]}'
            ),
        )
    )
    s.blocks.append(Block(type="commentary", text="Revised:\n- [ ] content step"))

    live = [st.text for st in summarize_streaming(s).steps]
    final = [st.text for st in summarize_turns([s.to_turn()]).steps]
    assert live == final  # the two summarizers agree
    assert live == ["todo plan step"]  # TodoWrite wins in both


def test_explicit_todowrite_steps_win():
    turn = Turn(role="assistant")
    turn.tool_uses.append(
        ToolUse(
            name="TodoWrite",
            input={
                "todos": [
                    {"content": "Inspect the pane", "status": "completed"},
                    {"content": "Build the Plan tab", "status": "in_progress"},
                    {"content": "Run tests", "status": "pending"},
                ]
            },
        )
    )

    summary = summarize_turns([turn])

    assert summary.source == "explicit"
    assert [s.text for s in summary.steps] == [
        "Inspect the pane",
        "Build the Plan tab",
        "Run tests",
    ]
    assert [s.status for s in summary.steps] == ["done", "active", "pending"]
    assert any(p.key == "build" and p.status == "active" for p in summary.phases)


def test_tools_infer_build_and_verify_phases():
    user = Turn(role="user")
    user.text_parts.append("Fix it")
    assistant = Turn(role="assistant")
    assistant.tool_uses.extend(
        [
            ToolUse(name="Read", input={"file_path": "a.py"}),
            ToolUse(name="Edit", input={"file_path": "a.py"}),
            ToolUse(name="Bash", input={"command": "python3 -m pytest -q"}),
        ]
    )

    summary = summarize_turns([user, assistant])
    states = {p.key: p.status for p in summary.phases}

    assert states["orient"] == "done"
    assert states["build"] == "done"
    assert states["verify"] in {"active", "done"}
    assert summary.active_detail == "python3 -m pytest -q"


def test_only_one_phase_is_active():
    assistant = Turn(role="assistant")
    assistant.text_parts.append("I'll edit this now.")
    assistant.tool_uses.append(ToolUse(name="Edit", input={"file_path": "a.py"}))

    summary = summarize_turns([assistant])

    active = [p.key for p in summary.phases if p.status == "active"]
    assert active == ["build"]


def test_streaming_markdown_plan_extracts_steps():
    streaming = StreamingAssistant(
        blocks=[
            Block(
                type="thinking",
                text="- [x] Read current code\n- [-] Add Plan pane\n- [ ] Verify",
            )
        ]
    )

    summary = summarize_streaming(streaming)

    assert [s.status for s in summary.steps] == ["done", "active", "pending"]
    assert summary.source == "explicit"
    assert any(p.key == "plan" and p.status == "active" for p in summary.phases)


def test_native_codex_plan_is_authoritative():
    summary = summarize_native_plan(
        [
            {"step": "Inspect protocol", "status": "completed"},
            {"step": "Wire native turn", "status": "inProgress"},
            {"step": "Run smoke", "status": "pending"},
        ],
        "Migrating the interactive transport",
    )

    assert summary.source == "native"
    assert [step.status for step in summary.steps] == ["done", "active", "pending"]
    assert summary.active_detail == "Migrating the interactive transport"
    assert [phase.key for phase in summary.phases if phase.status == "active"] == [
        "build"
    ]


def test_completed_native_plan_moves_to_verification():
    summary = summarize_native_plan(
        [{"step": "Implement", "status": "completed"}],
    )
    assert [phase.key for phase in summary.phases if phase.status == "active"] == [
        "verify"
    ]


def test_durable_execution_plan_preserves_counter_and_interruption_state():
    plan = ExecutionPlan(
        work_id="work-1",
        plan_id="plan-1",
        revision=2,
        provider="openai",
        participant_id="part-1",
        participant_generation=1,
        native_turn_id="turn-1",
        explanation="",
        steps=(
            ExecutionPlanStep("task-1", "Inspect", "completed"),
            ExecutionPlanStep("task-2", "Build", "interrupted"),
            ExecutionPlanStep("task-3", "Verify", "pending"),
            ExecutionPlanStep(
                "task-4",
                "Superseded approach",
                "dropped",
                blocked_reason="Replaced after inspection",
            ),
        ),
        status="interrupted",
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-01T00:01:00Z",
    )

    summary = summarize_execution_plan(plan)

    assert summary.source == "execution"
    assert summary.completed_count == 1
    assert summary.total_count == 3
    assert [step.status for step in summary.steps] == [
        "done",
        "interrupted",
        "pending",
        "dropped",
    ]
    assert summary.active_detail == "Interrupted · Build"
    assert summary.steps[-1].detail == "Replaced after inspection"
    assert {phase.key: phase.status for phase in summary.phases}["build"] == (
        "interrupted"
    )
    assert not any(phase.status == "active" for phase in summary.phases)
