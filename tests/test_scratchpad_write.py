"""Tests for the scratchpad write path (backend/scratchpad.py).

Network is faked at the urllib seam — these verify the request we build,
not the service.
"""
from __future__ import annotations

import json

import pytest

import helios.backend.scratchpad as S


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_a) -> bool:
        return False


def test_write_entry_posts_expected_payload(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["content_type"] = req.get_header("Content-type")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(b'{"key": "k", "size_bytes": 1, "expires_at": 0}')

    monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
    S.write_entry(
        "handoff/x-2026-06-11",
        {"session_id": "abc"},
        summary="the summary",
        tags=["handoff", "helios"],
        ttl_hours=24.0,
        created_by="helios@workstation",
    )
    assert captured["method"] == "POST"
    assert captured["url"] == f"{S.BASE_URL}/scratch/write"
    assert captured["content_type"] == "application/json"
    assert captured["body"] == {
        "key": "handoff/x-2026-06-11",
        "data": {"session_id": "abc"},
        "summary": "the summary",
        "tags": ["handoff", "helios"],
        "ttl_hours": 24.0,
        "created_by": "helios@workstation",
    }


def test_write_entry_wraps_network_failure(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise S.urllib.error.URLError("down")

    monkeypatch.setattr(S.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(S.ScratchpadError):
        S.write_entry("k", {"a": 1})
