from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.widgets.activity_indicator import (  # noqa: E402
    STATE_AGENT,
    STATE_BASH,
    STATE_EDITING,
    STATE_GENERIC_TOOL,
    native_activity_state,
)


def test_native_command_progress_uses_running_state_without_raw_output():
    state, detail = native_activity_state(
        {
            "category": "command",
            "phase": "progress",
            "delta": "secret command output must not be surfaced",
        }
    )

    assert state == STATE_BASH
    assert detail == ""


def test_native_file_and_mcp_activity_have_provider_specific_details():
    assert native_activity_state(
        {
            "category": "file",
            "changes": [{"path": "/repo/src/app.py", "kind": "update"}],
        }
    ) == (STATE_EDITING, ".../src/app.py")
    assert native_activity_state(
        {
            "category": "mcp",
            "server": "github",
            "tool": "search_code",
        }
    ) == (STATE_GENERIC_TOOL, "github · search_code")


def test_native_subagent_progress_reuses_agent_state():
    assert native_activity_state(
        {"category": "subagent", "message": "Reviewing permission flow"}
    ) == (STATE_AGENT, "Reviewing permission flow")
