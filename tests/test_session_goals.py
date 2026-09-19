from __future__ import annotations

import json

from helios.backend import session_goals as sg
from helios.backend.transcript import parse_transcript


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(sg, "_PATH", tmp_path / ".helios" / "session-goals.json")
    sg.reload()


def test_goal_store_roundtrip_and_clear(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    goal = sg.GoalState(
        objective="Ship Goal Mode",
        items=[sg.GoalPlanItem("write tests", "in-progress")],
        cwd="/repo",
        provider="openai",
    )
    sg.set_goal("sid", goal)
    sg.reload()

    stored = sg.get_goal("sid")
    assert stored is not None
    assert stored.objective == "Ship Goal Mode"
    assert stored.status == sg.GOAL_ACTIVE
    assert stored.items == [sg.GoalPlanItem("write tests", sg.PLAN_IN_PROGRESS)]
    assert stored.cwd == "/repo"
    assert stored.provider == "openai"
    assert stored.created_at
    assert stored.updated_at

    sg.clear_goal("sid")
    sg.reload()
    assert sg.get_goal("sid") is None


def test_rekey_goal_moves_legacy_session_entry_idempotently(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal("claude-session", sg.GoalState("Shared objective"))

    moved = sg.rekey_goal("claude-session", "work-1")

    assert moved is not None
    assert moved.objective == "Shared objective"
    assert sg.get_goal("claude-session") is None
    assert sg.get_goal("work-1").objective == "Shared objective"
    assert sg.rekey_goal("claude-session", "work-1").objective == "Shared objective"


def test_rekey_goal_canonical_work_entry_wins_and_legacy_is_removed(
    monkeypatch, tmp_path
):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal("claude-session", sg.GoalState("Stale objective"))
    sg.set_goal("work-1", sg.GoalState("Canonical objective"))

    moved = sg.rekey_goal("claude-session", "work-1")

    assert moved.objective == "Canonical objective"
    assert sg.get_goal("claude-session") is None
    sg.clear_goal("work-1")
    assert sg.get_goal("work-1") is None
    assert sg.rekey_goal("claude-session", "work-1") is None


def test_corrupt_goal_file_recovers(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    path = sg._path()
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    sg.reload()
    assert sg.get_goal("sid") is None


def test_goal_save_oserror_does_not_propagate(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    sg.set_goal("sid", sg.GoalState("Keep working"))

    assert sg.get_goal("sid").objective == "Keep working"
    assert not sg._path().exists()


def test_default_path_tracks_dynamic_state_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(sg, "_PATH", None)
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "one"))
    sg.reload()
    sg.set_goal("work-1", sg.GoalState("First"))
    assert (tmp_path / "one" / "session-goals.json").is_file()

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "two"))
    sg.reload()
    assert sg.get_goal("work-1") is None
    sg.set_goal("work-2", sg.GoalState("Second"))
    assert (tmp_path / "two" / "session-goals.json").is_file()


def test_goal_store_redacts_named_and_quoted_credentials(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal(
        "work-secret",
        sg.GoalState(
            'Deploy with MY_CLIENT_SECRET="hunter two words"',
            items=[sg.GoalPlanItem("Use password=hunter2")],
        ),
    )

    raw = sg._path().read_text(encoding="utf-8")
    stored = sg.get_goal("work-secret")
    assert "hunter two words" not in raw
    assert "hunter2" not in raw
    assert "[REDACTED BY HELIOS]" in stored.objective
    assert "[REDACTED BY HELIOS]" in stored.items[0].text


def test_prompt_wrap_and_strip_roundtrip():
    goal = sg.GoalState(
        "Finish the project",
        items=[
            sg.GoalPlanItem("inspect code", sg.PLAN_COMPLETED),
            sg.GoalPlanItem("implement", sg.PLAN_IN_PROGRESS),
        ],
    )
    wrapped = sg.wrap_user_prompt("please proceed", goal)
    assert "Objective: Finish the project" in wrapped
    assert "- [completed] inspect code" in wrapped
    assert "- [in_progress] implement" in wrapped
    assert sg.strip_goal_envelope(wrapped) == "please proceed"


def test_native_goal_supplement_preserves_acceptance_without_objective_duplication():
    goal = sg.GoalState(
        "Native objective already synchronized",
        definition_of_done="Focused tests pass",
        items=[
            sg.GoalPlanItem("Preserve existing behavior", sg.PLAN_COMPLETED),
            sg.GoalPlanItem("Verify the new path", sg.PLAN_PENDING),
        ],
    )

    wrapped = sg.wrap_goal_supplement("please proceed", goal)

    assert "Objective: Native objective already synchronized" not in wrapped
    assert "Done when: Focused tests pass" in wrapped
    assert "Acceptance checklist:" in wrapped
    assert "- [pending] Verify the new path" in wrapped
    assert "separate native execution plan" in wrapped
    assert sg.strip_goal_envelope(wrapped) == "please proceed"


def test_native_goal_supplement_is_omitted_without_missing_acceptance_fields():
    goal = sg.GoalState("Native objective only")

    assert sg.wrap_goal_supplement("continue", goal) == "continue"


def test_prompt_not_wrapped_for_inactive_statuses():
    for status in (sg.GOAL_PAUSED, sg.GOAL_COMPLETE, sg.GOAL_BLOCKED):
        goal = sg.GoalState("x", status=status)
        assert sg.wrap_user_prompt("hello", goal) == "hello"


def test_plan_item_normalization():
    items = sg.normalize_plan_items([
        {"content": "  First task  ", "status": "in-progress"},
        {"text": "Second task", "status": "done"},
        {"task": "Third task", "state": "blocked"},
        {"title": "Fourth task", "status": "mystery"},
        {"text": "Second task", "status": "done"},
        {"status": "pending"},
    ])
    assert items == [
        sg.GoalPlanItem("First task", sg.PLAN_IN_PROGRESS),
        sg.GoalPlanItem("Second task", sg.PLAN_COMPLETED),
        sg.GoalPlanItem("Third task", sg.PLAN_BLOCKED),
        sg.GoalPlanItem("Fourth task", sg.PLAN_PENDING),
    ]


def test_transcript_parser_strips_goal_envelope(tmp_path):
    wrapped = sg.wrap_user_prompt("original text", sg.GoalState("Finish it"))
    path = tmp_path / "s.jsonl"
    path.write_text(
        json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": wrapped}],
            },
        }) + "\n",
        encoding="utf-8",
    )
    turns = parse_transcript(path)
    assert len(turns) == 1
    assert turns[0].text == "original text"


def test_goal_objective_validation_enforces_native_limit():
    assert sg.objective_validation_error("") == "Enter an objective."
    assert sg.objective_validation_error("x" * 4000) == ""
    assert "4,001" in sg.objective_validation_error("x" * 4001)
    assert "4,000" in sg.objective_validation_error("x" * 4001)
