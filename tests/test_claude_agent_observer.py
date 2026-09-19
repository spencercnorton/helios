"""Claude's causal delegation observer.

The dormant adapter this replaces objected that tool-use records prove only that
delegation was *requested*. These tests pin that every state Helios shows is
keyed on an id the provider supplied, and that an actor Helios cannot see is
never promoted to one it can.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend.agent_activity import (  # noqa: E402
    AgentObservedStatus,
    ClaudeAgentActivityAdapter,
)
from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402
from helios.backend.transcript import ToolResult, ToolUse, Turn  # noqa: E402


def _driver() -> ClaudeCliDriver:
    drv = ClaudeCliDriver.__new__(ClaudeCliDriver)
    ClaudeCliDriver.__init__(drv, cwd="/tmp")
    return drv


def _emissions(drv) -> list[dict]:
    seen: list[dict] = []
    drv.connect("agents-updated", lambda _d, snap: seen.append(snap))
    return seen


def _turn_with_task(actor_id="toolu_1", name="Task", **inp) -> Turn:
    return Turn(role="assistant", tool_uses=[ToolUse(name=name, input=inp, id=actor_id)])


# --- driver: causal transitions ------------------------------------------


def test_a_task_tool_use_seeds_starting_not_running() -> None:
    """The request is proven; the child has not been observed yet."""

    drv = _driver()
    seen = _emissions(drv)

    drv._note_delegation_requests(_turn_with_task(subagent_type="Explore"))

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "starting"
    assert seen and seen[-1]["toolu_1"]["role"] == "Explore"


def test_a_non_delegating_tool_is_not_an_actor() -> None:
    drv = _driver()

    drv._note_delegation_requests(
        Turn(role="assistant", tool_uses=[ToolUse(name="Read", input={}, id="toolu_9")])
    )

    assert drv.observed_agent_snapshot() == {}


def test_workflow_counts_as_delegation_too() -> None:
    drv = _driver()

    drv._note_delegation_requests(_turn_with_task(name="Workflow", description="audit"))

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "starting"


def test_child_output_under_the_parent_id_promotes_to_working() -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task(subagent_type="Explore"))

    drv._note_child_progress("toolu_1")

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "working"


def test_progress_for_an_unknown_parent_is_ignored() -> None:
    """No actor may be invented from a parent id we never saw requested."""

    drv = _driver()

    drv._note_child_progress("toolu_unknown")

    assert drv.observed_agent_snapshot() == {}


@pytest.mark.parametrize(
    "subtype,expected",
    [
        ("success", "complete"),
        ("error_during_execution", "error"),
        ("aborted", "stopped"),
        ("interrupted", "stopped"),
        ("something_new", "error"),
    ],
)
def test_the_childs_own_terminal_sets_the_outcome(subtype, expected) -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task())
    drv._note_child_progress("toolu_1")

    drv._note_child_terminal("toolu_1", subtype)

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == expected


def test_a_late_child_event_never_resurrects_a_finished_actor() -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task())
    drv._note_child_terminal("toolu_1", "success")

    drv._note_child_progress("toolu_1")

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "complete"


def test_the_root_tool_result_also_closes_the_actor() -> None:
    """The arrival-guaranteed signal: it is what unblocks the root model."""

    drv = _driver()
    drv._note_delegation_requests(_turn_with_task())

    drv._note_delegation_results(
        Turn(role="user", tool_results=[ToolResult(tool_use_id="toolu_1", content="ok")])
    )

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "complete"


def test_an_error_tool_result_is_not_reported_as_success() -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task())

    drv._note_delegation_results(
        Turn(
            role="user",
            tool_results=[ToolResult(tool_use_id="toolu_1", content="boom", is_error=True)],
        )
    )

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "error"


def test_a_new_root_turn_clears_the_projection_and_says_so() -> None:
    """Turn-scoped: last turn's actors must not linger in the dock."""

    drv = _driver()
    drv._note_delegation_requests(_turn_with_task())
    seen = _emissions(drv)

    drv._reset_observed_agents("msg_2")

    assert drv.observed_agent_snapshot() == {}
    assert drv.observed_agent_root_turn_id == "msg_2"
    assert seen == [{}], "clearing must emit, or the dock keeps stale rows"


def test_the_root_turn_id_is_a_property_not_a_bound_method() -> None:
    """`_observed_agent_scope` reads it via getattr; a method would pass is_valid."""

    assert isinstance(_driver().observed_agent_root_turn_id, str)


def test_the_snapshot_is_a_copy() -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task())

    drv.observed_agent_snapshot()["toolu_1"]["status"] = "tampered"

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "starting"


# --- adapter -------------------------------------------------------------


def test_the_adapter_is_live_now() -> None:
    adapter = ClaudeAgentActivityAdapter()
    assert adapter.provider == "anthropic"
    assert adapter.available is True


def test_the_adapter_normalizes_each_driver_state() -> None:
    adapter = ClaudeAgentActivityAdapter()
    snapshot = {
        "a": {"status": "starting", "name": "Explore", "role": "Explore"},
        "b": {"status": "working", "name": "b"},
        "c": {"status": "complete"},
        "d": {"status": "error", "message": "boom"},
        "e": {"status": "stopped"},
    }

    by_id = {o.actor_id: o for o in adapter.observations(snapshot)}

    assert by_id["a"].status is AgentObservedStatus.STARTING
    assert by_id["b"].status is AgentObservedStatus.RUNNING
    assert by_id["c"].status is AgentObservedStatus.COMPLETED
    assert by_id["d"].status is AgentObservedStatus.FAILED
    assert by_id["d"].detail == "boom"
    assert by_id["e"].status is AgentObservedStatus.STOPPED


def test_the_adapter_tolerates_junk() -> None:
    adapter = ClaudeAgentActivityAdapter()

    assert adapter.observations(None) == ()
    assert adapter.observations("nope") == ()
    assert adapter.observations({"": {"status": "working"}}) == ()
    assert adapter.observations({"a": "not-a-dict"}) == ()


# --- wire-name drift and the background-task lifecycle ----
#
# Every literal below was captured off `claude` 2.1.223 on 2026-08-06, not
# invented. The captures are described in the private model-usage capture notes.


def test_the_wire_name_agent_seeds_an_actor() -> None:
    """2.1.223 advertises `Task` in system/init but emits `name: "Agent"`.

    Matching only the advertised name seeded nothing at all, so the dock was
    silently empty for every Claude delegation. Assert on the WIRE name: the
    two have diverged once and can again.
    """

    drv = _driver()

    drv._note_delegation_requests(
        _turn_with_task(name="Agent", subagent_type="Explore", description="Count files")
    )

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "starting"


def test_an_async_launch_tool_result_does_not_complete_a_background_child() -> None:
    """The root tool_result is NOT a terminal for a backgrounded child.

    Measured: the root gets `"Async agent launched successfully."` 36 records
    before the child finished. Closing on it would show every subagent complete
    the instant it launched — worst possible answer during a fan-out.
    """

    drv = _driver()
    drv._note_delegation_requests(_turn_with_task(name="Agent", subagent_type="Explore"))
    drv._note_task_record(
        "task_started",
        {"task_id": "aefec392efb9ad2b5", "tool_use_id": "toolu_1", "subagent_type": "Explore"},
    )

    drv._note_delegation_results(
        Turn(
            role="user",
            tool_results=[
                ToolResult(
                    tool_use_id="toolu_1",
                    content="Async agent launched successfully. (This tool result is "
                    "internal metadata)",
                )
            ],
        )
    )

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "working"


def test_task_notification_is_the_terminal_for_a_task_managed_child() -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task(name="Agent", subagent_type="Explore"))
    drv._note_task_record("task_started", {"tool_use_id": "toolu_1"})

    drv._note_task_record(
        "task_notification",
        {"tool_use_id": "toolu_1", "status": "completed", "summary": "18"},
    )

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "complete"


def test_a_task_record_for_an_unknown_id_creates_no_actor() -> None:
    """Same rule as the rest of the observer: no actor without a seeded id."""

    drv = _driver()

    drv._note_task_record("task_notification", {"tool_use_id": "toolu_unknown",
                                                "status": "completed"})

    assert drv.observed_agent_snapshot() == {}


def test_a_late_task_record_never_resurrects_a_terminal_actor() -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task(name="Agent"))
    drv._note_task_record("task_notification", {"tool_use_id": "toolu_1",
                                                "status": "completed"})

    drv._note_task_record("task_progress", {"tool_use_id": "toolu_1"})

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "complete"


def test_a_failed_task_notification_is_an_error_not_a_success() -> None:
    drv = _driver()
    drv._note_delegation_requests(_turn_with_task(name="Agent"))

    drv._note_task_record("task_notification", {"tool_use_id": "toolu_1", "status": "failed"})

    snap = drv.observed_agent_snapshot()["toolu_1"]
    assert snap["status"] == "error"
    assert snap["message"] == "failed"


def test_a_foreground_tool_result_still_closes_a_plain_actor() -> None:
    """No task_* lifecycle seen → the tool_result remains the guaranteed close."""

    drv = _driver()
    drv._note_delegation_requests(_turn_with_task(name="Agent"))

    drv._note_delegation_results(
        Turn(role="user", tool_results=[ToolResult(tool_use_id="toolu_1", content="18")])
    )

    assert drv.observed_agent_snapshot()["toolu_1"]["status"] == "complete"
