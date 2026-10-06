"""Native Claude progress must preserve root/child and terminal boundaries."""

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import ClaudeCliDriver
from helios.widgets.activity_indicator import native_activity_state, STATE_BASH


def _driver():
    driver = ClaudeCliDriver(cwd="/tmp", max_budget_usd=None)
    driver._session_id = "session"
    driver._busy = True
    driver._reset_observed_agents("turn")
    for event in [
        {"type": "message_start", "message": {"model": "claude"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "Bash", "id": "tool-1"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"command":"pytest -q"}'}},
        {"type": "message_stop"},
    ]:
        driver._dispatch_record({"type": "stream_event", "event": event})
    updates = []
    driver.connect("activity-updated", lambda _d, payload: updates.append(payload))
    return driver, updates


def _progress(**overrides):
    return {"type": "tool_progress", "tool_use_id": "tool-1", "tool_name": "Bash",
            "session_id": "session", "parent_tool_use_id": None, "elapsed_time_seconds": 65, **overrides}


def test_known_root_tool_progress_has_native_elapsed_time_without_a_new_turn():
    driver, updates = _driver()
    turns = []
    driver.connect("turn-appended", lambda _d, turn: turns.append(turn))
    driver._dispatch_record(_progress())
    assert native_activity_state(updates[0]) == (STATE_BASH, "pytest -q · 1m 05s elapsed")
    assert turns == []


@pytest.mark.parametrize("overrides", [
    {"heartbeat": True}, {"session_id": "foreign"}, {"tool_use_id": "unknown"},
    {"tool_name": "Edit"}, {"elapsed_time_seconds": True}, {"elapsed_time_seconds": -1},
    {"elapsed_time_seconds": float("nan")}, {"elapsed_time_seconds": float("inf")},
    {"elapsed_time_seconds": 10**1000},
])
def test_heartbeats_foreign_or_malformed_progress_do_not_claim_execution(overrides):
    driver, updates = _driver()
    driver._dispatch_record(_progress(**overrides))
    assert updates == []


def test_tool_result_retires_progress_and_new_turn_clears_old_tools():
    driver, updates = _driver()
    driver._dispatch_record({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tool-1", "content": "passed"}
    ]}})
    driver._dispatch_record(_progress())
    assert updates == []
    driver, updates = _driver()
    driver._reset_observed_agents("next-turn")
    driver._dispatch_record(_progress())
    assert updates == []


def test_child_progress_updates_only_a_known_nonterminal_actor():
    driver, updates = _driver()
    driver._agents = {"parent": {"status": "starting"}}
    driver._dispatch_record(_progress(parent_tool_use_id="parent", heartbeat=True))
    assert driver._agents["parent"]["status"] == "starting"
    driver._dispatch_record(_progress(parent_tool_use_id="parent"))
    assert driver._agents["parent"]["status"] == "working"
    assert updates == []
    driver._agents["parent"]["status"] = "complete"
    driver._dispatch_record(_progress(parent_tool_use_id="parent"))
    driver._dispatch_record(_progress(parent_tool_use_id="unknown"))
    assert driver._agents == {"parent": {"status": "complete"}}
    driver._agents["parent"]["status"] = "needs_input"
    driver._dispatch_record(_progress(parent_tool_use_id="parent"))
    assert driver._agents["parent"]["status"] == "needs_input"


def test_idle_driver_ignores_late_progress():
    driver, updates = _driver()
    driver._busy = False
    driver._dispatch_record(_progress())
    assert updates == []


def test_claude_rate_limit_signal_uses_measured_percent():
    driver, _updates = _driver()
    rows = []
    driver.connect("rate-limit-updated", lambda _d, row: rows.append(row))
    driver._dispatch_record({"type": "rate_limit_event", "rate_limit_info": {
        "rateLimitType": "seven_day_sonnet", "status": "allowed", "utilization": 0.8,
    }})
    assert rows[0]["usedPercent"] == 80
    assert rows[0]["provider"] == "anthropic"
