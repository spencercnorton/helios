from __future__ import annotations

from dataclasses import replace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from helios.backend.execution_plan import ExecutionPlan, ExecutionPlanStep  # noqa: E402
from helios.widgets.plan_progress_strip import PlanProgressStrip  # noqa: E402


def _plan(*, status: str = "active") -> ExecutionPlan:
    return ExecutionPlan(
        work_id="work-1",
        plan_id="plan-1",
        revision=1,
        provider="openai",
        participant_id="part-1",
        participant_generation=1,
        native_turn_id="turn-1",
        explanation="",
        steps=(
            ExecutionPlanStep("task-1", "Inspect", "completed"),
            ExecutionPlanStep(
                "task-2",
                "Build",
                "interrupted" if status == "interrupted" else "inProgress",
            ),
            ExecutionPlanStep("task-3", "Verify", "pending"),
        ),
        status=status,
        created_at="2026-09-01T00:00:00Z",
        updated_at="2026-09-01T00:00:00Z",
    )


def test_plan_progress_strip_shows_durable_count_and_opens_plan():
    strip = PlanProgressStrip()
    opened = []
    strip.connect("open-requested", lambda *_: opened.append(True))

    strip.set_plan(_plan())

    assert strip.get_reveal_child() is True
    assert strip._progress.get_label() == "1/3 tasks complete"
    assert strip._active.get_label() == "Build"
    button = strip.get_child()
    button.emit("clicked")
    assert opened == [True]


def test_plan_progress_strip_keeps_interrupted_work_visible_until_replaced():
    strip = PlanProgressStrip()

    strip.set_plan(_plan(status="interrupted"))

    assert strip.get_reveal_child() is True
    assert strip._progress.get_label() == "1/3 tasks complete"
    assert strip._active.get_label() == "Interrupted · Build"

    strip.clear()
    assert strip.get_reveal_child() is False


def test_dropped_only_revision_remains_openable_and_does_not_count_as_complete():
    strip = PlanProgressStrip()
    plan = replace(
        _plan(),
        steps=(ExecutionPlanStep("task-old", "Old scope", "dropped"),),
    )

    strip.set_plan(plan)

    assert strip.get_reveal_child() is True
    assert strip._progress.get_label() == "0/0 tasks complete"
