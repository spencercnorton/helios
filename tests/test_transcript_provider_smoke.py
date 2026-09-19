from __future__ import annotations

import json
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from helios.backend import session_providers as SP  # noqa: E402
from helios.backend.projects import Project, Session  # noqa: E402
from helios.widgets.session_list import _provider_chip  # noqa: E402
from helios.widgets.transcript_view import TranscriptView  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()


def _labels(root: Gtk.Widget) -> list[str]:
    found: list[str] = []

    def visit(widget: Gtk.Widget) -> None:
        if isinstance(widget, Gtk.Label):
            found.append(widget.get_label())
        child = widget.get_first_child()
        while child is not None:
            visit(child)
            child = child.get_next_sibling()

    visit(root)
    return found


def test_openai_transcript_uses_gpt_assistant_label(monkeypatch, tmp_path):
    # HELIOS_STATE_DIR is already redirected to tmp_path by the autouse
    # isolate_state_dir fixture in conftest.py; just drop the cache.
    SP.reload()
    session_id = "thread-openai"
    SP.set_provider(session_id, "openai")

    path = tmp_path / "thread-openai.jsonl"
    rows = [
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello from GPT"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    project = Project(dirname="-tmp-proj", cwd=str(tmp_path), path=tmp_path)
    session = Session(
        project=project,
        session_id=session_id,
        path=path,
        mtime=time.time(),
        size=path.stat().st_size,
    )

    view = TranscriptView()
    view.set_session(session)

    labels = _labels(view)
    assert view._assistant_label == "GPT"
    assert "GPT" in labels
    assert "Claude" not in labels


def test_unknown_transcript_is_neutral_and_never_labeled_claude(tmp_path):
    path = tmp_path / "opaque.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    project = Project(dirname="-tmp-proj", cwd=str(tmp_path), path=tmp_path)
    session = Session(
        project=project,
        session_id="opaque",
        path=path,
        mtime=time.time(),
        size=path.stat().st_size,
    )

    view = TranscriptView()
    view.set_session(session)
    chip = _provider_chip(SP.resolve_provider(session.session_id, session.path))

    assert view._assistant_label == "Assistant"
    assert "Claude" not in _labels(view)
    assert chip.get_label() == "Unknown"
    assert chip.has_css_class("helios-provider-unknown")
    assert not chip.has_css_class("helios-provider-claude")
