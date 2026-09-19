"""Tests for sidebar status derivation — the working/ready/idle/awaiting logic.

The subtle part is that claude in stream-json mode never writes a `result`
record to the .jsonl, so a finished turn ends on an assistant message. We tell
"working" from "ready" by the assistant's final content block (tool_use vs
text) and by whether a live process is bound. These tests pin that.
"""
from __future__ import annotations

import json
from pathlib import Path

from helios.backend import session_state as ss
from helios.backend.projects import Project, Session


def _write(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "sess.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _session(path: Path) -> Session:
    proj = Project(dirname="-x", cwd="/x", path=path.parent)
    return Session(project=proj, session_id="sess", path=path, mtime=0.0, size=path.stat().st_size)


def _assistant(blocks: list[dict]) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks},
            "timestamp": "2026-06-09T00:00:00Z"}


def _user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


# ── _assistant_ends_with_tool_use ────────────────────────────────────────

def test_ends_with_tool_use_true():
    assert ss._assistant_ends_with_tool_use(
        [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "Bash"}]
    ) is True


def test_ends_with_tool_use_false_on_text_final():
    assert ss._assistant_ends_with_tool_use(
        [{"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "done"}]
    ) is False


def test_ends_with_tool_use_handles_garbage():
    assert ss._assistant_ends_with_tool_use(None) is False
    assert ss._assistant_ends_with_tool_use("nope") is False
    assert ss._assistant_ends_with_tool_use([]) is False


# ── classification: bound (process alive) vs unbound ─────────────────────

def test_text_final_unbound_is_idle(tmp_path):
    p = _write(tmp_path, [_assistant([{"type": "text", "text": "answer"}])])
    assert ss.status_for(_session(p), set()).state == ss.STATE_IDLE


def test_text_final_bound_is_ready(tmp_path):
    p = _write(tmp_path, [_assistant([{"type": "text", "text": "answer"}])])
    assert ss.status_for(_session(p), {"sess"}).state == ss.STATE_READY


def test_tool_use_final_bound_is_working(tmp_path):
    p = _write(tmp_path, [_assistant([{"type": "tool_use", "name": "Bash"}])])
    assert ss.status_for(_session(p), {"sess"}).state == ss.STATE_WORKING


def test_tool_use_final_unbound_is_idle(tmp_path):
    # process gone after a dangling tool_use -> nothing pending from the user
    p = _write(tmp_path, [_assistant([{"type": "tool_use", "name": "Bash"}])])
    assert ss.status_for(_session(p), set()).state == ss.STATE_IDLE


def test_trailing_user_unbound_is_awaiting(tmp_path):
    p = _write(tmp_path, [_assistant([{"type": "text", "text": "a"}]), _user_text("next?")])
    assert ss.status_for(_session(p), set()).state == ss.STATE_AWAITING


def test_trailing_user_bound_is_working(tmp_path):
    p = _write(tmp_path, [_assistant([{"type": "text", "text": "a"}]), _user_text("next?")])
    assert ss.status_for(_session(p), {"sess"}).state == ss.STATE_WORKING


def test_clean_result_unbound_idle_bound_ready(tmp_path):
    recs = [_assistant([{"type": "text", "text": "a"}]),
            {"type": "result", "subtype": "success"}]
    p = _write(tmp_path, recs)
    assert ss.status_for(_session(p), set()).state == ss.STATE_IDLE
    assert ss.status_for(_session(p), {"sess"}).state == ss.STATE_READY


def test_errored_result_is_errored_regardless_of_binding(tmp_path):
    p = _write(tmp_path, [{"type": "result", "subtype": "error", "is_error": True}])
    assert ss.status_for(_session(p), set()).state == ss.STATE_ERRORED
    assert ss.status_for(_session(p), {"sess"}).state == ss.STATE_ERRORED


def test_empty_transcript(tmp_path):
    p = tmp_path / "sess.jsonl"
    p.write_text("", encoding="utf-8")
    assert ss.status_for(_session(p), set()).state == ss.STATE_IDLE
    assert ss.status_for(_session(p), {"sess"}).state == ss.STATE_WORKING


def test_active_alias_is_working():
    assert ss.STATE_ACTIVE == ss.STATE_WORKING
