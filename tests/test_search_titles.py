"""Search rows carry a session title, never the cwd leaf.

GTK-free by construction: `helios.backend.search` must stay importable without
PyGObject, which is the entire point of the `backend.title_store` split.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from helios.backend import search, title_store


def _write_session(projects_dir, dirname, session_id, first_user, needle_line):
    proj = projects_dir / dirname
    proj.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"content": [{"type": "text", "text": first_user}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": needle_line}]}},
    ]
    (proj / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8"
    )


@pytest.fixture
def two_sessions(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(search, "PROJECTS_DIR", projects)
    monkeypatch.setattr(title_store, "_store_singleton", None)
    monkeypatch.setattr(title_store, "_CACHE_PATH", tmp_path / "title-cache.json")
    _write_session(projects, "-home-me-alpha", "sess-titled", "opened alpha", "the needle here")
    _write_session(projects, "-home-me-beta", "sess-plain", "Refactor the beta parser", "a needle too")
    title_store.store().set("sess-titled", "Stored Title")
    return projects


def test_stored_title_wins(two_sessions):
    hits = {h.session_id: h for h in search.search_sessions("needle")}
    assert hits["sess-titled"].title == "Stored Title"


def test_unstored_session_falls_back_to_first_user_message(two_sessions):
    hit = {h.session_id: h for h in search.search_sessions("needle")}["sess-plain"]
    # The defect this guards: the row used to show the cwd leaf.
    assert hit.title == "Refactor the beta parser"
    assert hit.title
    assert "beta" not in hit.project_cwd or hit.title != hit.project_cwd


def test_search_imports_without_pygobject():
    """A gi import creeping into search.py would break the slim CI lane."""
    code = (
        "import sys\n"
        "class Ban:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'gi' or name.startswith('gi.'):\n"
        "            raise AssertionError('helios.backend.search imported gi')\n"
        "sys.meta_path.insert(0, Ban())\n"
        "import helios.backend.search\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
