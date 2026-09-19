from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from helios.backend import session_goals as sg  # noqa: E402
from helios.widgets.goal_strip import GoalStrip  # noqa: E402


def test_goal_strip_constructs_and_updates_states():
    strip = GoalStrip()
    assert strip.get_reveal_child() is False

    strip.set_goal(
        sg.GoalState(
            "Finish Goal Mode",
            items=[
                sg.GoalPlanItem("inspect", sg.PLAN_COMPLETED),
                sg.GoalPlanItem("ship", sg.PLAN_PENDING),
            ],
        )
    )
    assert strip.get_reveal_child() is True

    strip.set_goal(sg.GoalState("Paused goal", status=sg.GOAL_PAUSED))
    assert strip.get_reveal_child() is True

    strip.clear_goal()
    assert strip.get_reveal_child() is False


def test_native_goal_overlay_shows_exact_status_and_usage_then_clears():
    strip = GoalStrip()
    strip.set_goal(sg.GoalState("Finish Goal Mode", status=sg.GOAL_BLOCKED))

    strip.set_native_goal(
        {
            "goal": {
                "objective": "Finish Goal Mode",
                "status": "usageLimited",
                "tokensUsed": 12345,
                "tokenBudget": 50000,
                "timeUsedSeconds": 245,
            }
        }
    )

    assert strip._status.get_label() == "usageLimited"
    assert strip._native_usage.get_label() == "12,345/50,000 tokens · 245s"
    assert strip._native_usage.get_visible() is True

    strip.clear_native_goal()
    assert strip._status.get_label() == "Blocked"
    assert strip._native_usage.get_visible() is False
