"""Tests for the hand-off payload builder (backend/session_handoff.py)."""
from __future__ import annotations

import json
from datetime import date

import pytest

import helios.backend.projects as P
import helios.backend.session_handoff as H
import helios.backend.session_providers as SP


def _record(role: str, text: str, *, ts: str = "", meta: bool = False) -> dict:
    rec = {
        "type": role,
        "message": {"role": role, "content": text},
        "cwd": "/home/u/proj",
    }
    if ts:
        rec["timestamp"] = ts
    if meta:
        rec["isMeta"] = True
    return rec


def _mk_session(tmp_path, records: list[dict]) -> P.Session:
    proj_dir = tmp_path / "-home-u-proj"
    proj_dir.mkdir(parents=True)
    f = proj_dir / "abc12345-0000-1111-2222-333344445555.jsonl"
    f.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    proj = P._scan_project_dir(proj_dir, origin="local", read_only=False)
    assert proj is not None
    session = proj.load_sessions()[0]
    SP.set_provider(session.session_id, "anthropic")
    return session


# ── suggest_key ──────────────────────────────────────────────────────


def test_suggest_key_slugs_title():
    key = H.suggest_key("Fix the GPU bug!", "abc12345", today=date(2026, 6, 11))
    assert key == "handoff/fix-the-gpu-bug-2026-06-11"


def test_suggest_key_falls_back_to_session_id():
    key = H.suggest_key("???", "ABC12345-rest", today=date(2026, 6, 11))
    assert key == "handoff/abc12345-2026-06-11"


def test_suggest_reply_key_uses_session_prefix():
    key = H.suggest_reply_key("ABC12345-rest", today=date(2026, 6, 11))
    assert key == "agent-replies/abc12345-2026-06-11"


def test_suggest_key_truncates_long_titles():
    key = H.suggest_key("x" * 200, "abc", today=date(2026, 6, 11))
    slug = key.removeprefix("handoff/").removesuffix("-2026-06-11")
    assert len(slug) <= 48


# ── default_summary / build_payload ──────────────────────────────────


def test_default_summary_has_resume_coordinates(tmp_path):
    s = _mk_session(tmp_path, [_record("user", "hello")])
    summary = H.default_summary(s, "My session")
    assert "My session" in summary
    assert s.session_id in summary
    assert s.project.cwd in summary
    assert "Claude handoff for GPT" in summary


def test_build_payload_core_fields(tmp_path):
    s = _mk_session(tmp_path, [_record("user", "hello")])
    payload = H.build_payload(s, "My session", include_tail=False)
    assert payload["title"] == "My session"
    assert payload["session_id"] == s.session_id
    assert payload["cwd"] == "/home/u/proj"
    assert payload["kind"] == "agent-contact"
    assert payload["from_provider"] == "anthropic"
    assert payload["to_provider"] == "openai"
    assert payload["reply_key"].startswith("agent-replies/")
    assert payload["reply_tags"] == [
        "handoff", "helios", "agent-contact", "from-openai", "to-anthropic", "agent-reply"
    ]
    assert s.session_id in payload["resume"]
    assert "/home/u/proj" in payload["resume"]
    assert "transcript_tail" not in payload


def test_build_payload_tail_text_only(tmp_path):
    s = _mk_session(
        tmp_path,
        [
            _record("user", "question one", ts="2026-06-11T10:00:00Z"),
            _record("assistant", "answer one"),
            _record("user", "meta noise", meta=True),  # skipped
            _record("assistant", ""),  # no text -> skipped
        ],
    )
    tail = H.build_payload(s, "t")["transcript_tail"]
    assert [(e["role"], e["text"]) for e in tail] == [
        ("user", "question one"),
        ("assistant", "answer one"),
    ]
    assert tail[0]["t"] == "2026-06-11T10:00:00Z"


def test_openai_session_contacts_claude_and_uses_codex_resume(monkeypatch, tmp_path):
    # HELIOS_STATE_DIR is already redirected to tmp_path by the autouse
    # isolate_state_dir fixture in conftest.py; just drop the cache.
    SP.reload()
    s = _mk_session(tmp_path, [_record("user", "hello")])
    SP.forget(s.session_id)
    SP.set_provider(s.session_id, "openai")

    payload = H.build_payload(s, "GPT session", include_tail=False)

    assert payload["from_provider"] == "openai"
    assert payload["to_provider"] == "anthropic"
    assert "codex exec resume" in payload["resume"]
    assert H.contact_tags("openai", "anthropic") == [
        "handoff", "helios", "agent-contact", "from-openai", "to-anthropic"
    ]


def test_unresolved_session_never_publishes_a_guessed_resume(tmp_path):
    s = _mk_session(tmp_path, [_record("user", "hello")])
    SP.forget(s.session_id)

    assert "provider ownership is unverified" in H.default_summary(s, "Unknown")
    with pytest.raises(ValueError, match="provider ownership is unknown"):
        H.build_payload(s, "Unknown")


def test_transcript_tail_truncates_and_limits(tmp_path):
    long_text = "y" * 2000
    records = [_record("user", f"msg {i}") for i in range(20)]
    records.append(_record("assistant", long_text))
    s = _mk_session(tmp_path, records)
    tail = H.transcript_tail(s.path)
    assert len(tail) == H.TAIL_TURNS
    assert tail[-1]["text"].endswith("…")
    assert len(tail[-1]["text"]) <= H.TAIL_CHARS


def test_transcript_tail_missing_file_is_empty(tmp_path):
    s = _mk_session(tmp_path, [_record("user", "hello")])
    s.path.unlink()
    assert H.transcript_tail(s.path) == []
