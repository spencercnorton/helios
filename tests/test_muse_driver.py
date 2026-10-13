"""Unit tests for the Muse MSP driver (no real `muse serve` process)."""

from __future__ import annotations

import io
import json

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

from helios.backend.agent_activity import MuseAgentActivityAdapter  # noqa: E402
from helios.backend.process.muse_driver import (  # noqa: E402
    MuseServeDriver,
    _normalize_tool,
    _translate_answers,
)
from helios.backend.process.streaming import StreamingAssistant  # noqa: E402

MODEL = "muse-wonder-1.0"


@pytest.fixture(autouse=True)
def drain_driver_callbacks():
    yield
    while GLib.MainContext.default().iteration(False):
        pass


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        pass

    def frames(self):
        return [json.loads(data.decode("utf-8")) for data in self.writes]


class _FakeProcess:
    def __init__(self):
        self.pid = 123457
        self.stdin = _FakeStdin()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode


def _driver(tmp_path) -> MuseServeDriver:
    driver = MuseServeDriver(cwd=str(tmp_path), model=MODEL)
    driver._proc = _FakeProcess()
    driver._session_id = "session-1"
    driver._root_turn_id = "turn-1"
    driver._streaming = StreamingAssistant(model=MODEL)
    driver._is_busy = True
    return driver


def test_tool_names_normalize_to_helios_vocabulary():
    name, parsed = _normalize_tool("edit_file", {"path": "a.py", "find": "x", "replace": "y"})
    assert name == "Edit"
    assert parsed["file_path"] == "a.py"
    assert parsed["old_string"] == "x" and parsed["new_string"] == "y"
    assert _normalize_tool("bash", {"command": "ls"})[0] == "Bash"
    assert _normalize_tool("unknown_tool", {})[0] == "unknown_tool"


def test_agent_message_streams_and_reminders_stay_hidden(tmp_path):
    driver = _driver(tmp_path)
    driver._on_item({"itemId": "r1", "kind": "reminderChild", "turnId": "turn-1",
                     "status": "inProgress"}, terminal=False)
    driver._on_item({"itemId": "m1", "kind": "agentMessage", "turnId": "turn-1",
                     "status": "inProgress", "text": ""}, terminal=False)
    driver._on_delta({"itemId": "m1", "field": "text", "delta": "pong"})
    assert driver._agents == {}
    turn = driver._streaming.to_turn()
    assert turn.content and "pong" in turn.content[0].text


def test_subagent_lifecycle_feeds_dock_snapshot(tmp_path):
    driver = _driver(tmp_path)
    emitted = []
    driver.connect("agents-updated", lambda _d, snap: emitted.append(snap))
    driver._on_item({"itemId": "a1", "kind": "subagent", "turnId": "turn-1",
                     "status": "inProgress", "role": "reviewer",
                     "objective": "check auth", "subagentId": "sub-1"}, terminal=False)
    assert driver._agents["a1"]["status"] == "starting"
    driver._on_item({"itemId": "a1", "kind": "subagent", "turnId": "turn-1",
                     "status": "inProgress", "role": "reviewer"}, terminal=False)
    assert driver._agents["a1"]["status"] == "working"
    driver._on_item({"itemId": "a1", "kind": "subagent", "turnId": "turn-1",
                     "status": "completed", "role": "reviewer"}, terminal=True)
    assert driver._agents["a1"]["status"] == "complete"
    assert emitted  # dock was notified at every transition
    # The shared adapter must accept the snapshot unchanged.
    observations = MuseAgentActivityAdapter().observations(driver.observed_agent_snapshot())
    assert [(o.actor_id, o.status.value) for o in observations] == [("a1", "completed")]


def test_workflow_children_become_actors(tmp_path):
    driver = _driver(tmp_path)
    driver._on_item({"itemId": "w1", "kind": "workflow", "turnId": "turn-1",
                     "status": "inProgress",
                     "children": [{"childId": "c1", "attempt": 1, "status": "completed",
                                   "failureKind": "boom", "label": "scan"}]},
                    terminal=False)
    # "completed" with a failureKind means the child failed (MSP rule).
    assert driver._agents["w1:c1:1"]["status"] == "error"


def test_approval_uses_question_dialog_and_decides(tmp_path):
    driver = _driver(tmp_path)
    asked = []
    driver.connect("question-asked", lambda _d, payload, token: asked.append((payload, token)))
    driver._on_approval_requested({
        "approvalId": "ap-1", "toolName": "bash",
        "currentRequirementId": {"approvalId": "ap-1", "sourceIndex": 0},
        "availableChoices": [
            {"choiceId": "yes", "label": "Allow once", "decision": "approved", "scope": "once"},
            {"choiceId": "no", "label": "Deny", "decision": "denied", "scope": "once"},
        ]})
    assert asked and asked[0][1] == "ap-1"
    driver.answer_question("ap-1", "Allow once")
    frames = driver._proc.stdin.frames()
    decide = [f for f in frames if f.get("method") == "approval/decide"]
    assert decide and decide[0]["params"]["choiceId"] == "yes"
    # An unrecognized answer denies instead of hanging.
    driver._on_approval_requested({
        "approvalId": "ap-2", "toolName": "bash",
        "currentRequirementId": {"approvalId": "ap-2", "sourceIndex": 0},
        "availableChoices": [
            {"choiceId": "yes", "label": "Allow once", "decision": "approved", "scope": "once"},
            {"choiceId": "no", "label": "Deny", "decision": "denied", "scope": "once"},
        ]})
    driver.answer_question("ap-2", None)
    decide2 = [f for f in driver._proc.stdin.frames()
               if f.get("method") == "approval/decide"][-1]
    assert decide2["params"]["choiceId"] == "no"


def test_translate_answers_shapes():
    prompt = {"questions": [
        {"id": "q1", "selection": {"mode": "single"}},
        {"id": "q2", "selection": {"mode": "multiple"}},
    ]}
    rows = _translate_answers(prompt, {"0": {"answers": ["A"]}, "1": {"answers": ["X", "Y"]}})
    assert rows == [
        {"questionId": "q1", "selectedLabel": "A"},
        {"questionId": "q2", "selectedLabels": ["X", "Y"]},
    ]
    assert _translate_answers(prompt, None) is None
    assert _translate_answers({"questions": [{"id": "q", "selection": {"mode": "single"}}]}, "B") == [
        {"questionId": "q", "selectedLabel": "B"}]


def test_serve_args_follow_permission_mode():
    from helios.backend.process.muse_driver import MUSE_INTERACTIVE_ENV, serve_args

    assert "--disable-sandbox" in serve_args("muse-x", "bypassPermissions")
    assert "--disable-sandbox" not in serve_args("muse-x", "default")
    plan = serve_args("muse-x", "plan")
    assert "--disable-shell" in plan and "--disable-sandbox" not in plan
    assert {"META_API_KEY", "NORVI_TRACKER_API_TOKEN"} <= MUSE_INTERACTIVE_ENV
