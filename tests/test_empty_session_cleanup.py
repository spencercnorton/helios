"""Tests for empty-session filtering and orphan sidecar sweep.

Tests cover:
  * session_has_content — helper in transcript.py
  * discover_local_sessions — filters sidecar-only local sessions from sidebar
  * sweep_orphan_sidecar_sessions — moves sidecar-only files to archive
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import helios.backend.projects as P
import helios.backend.session_providers as SP
from helios.backend.transcript import session_has_content, session_is_descendant_only


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _sidecar_line(session_id: str, title: str) -> str:
    """The exact record the Claude CLI writes when it generates an AI title."""
    return json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": session_id})


def _user_line(text: str = "hello") -> str:
    return json.dumps({
        "type": "user",
        "cwd": "/home/u/proj",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    })


def _sidechain_line(text: str = "child work") -> str:
    return json.dumps(
        {
            "type": "assistant",
            "cwd": "/home/u/proj",
            "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def _write_sidecar(proj_dir: Path, name: str, *, age_s: float = 3600) -> Path:
    """Write a sidecar-only (ai-title, no real turns) .jsonl file."""
    proj_dir.mkdir(parents=True, exist_ok=True)
    f = proj_dir / f"{name}.jsonl"
    f.write_text(_sidecar_line(name, f"Some title for {name}") + "\n", encoding="utf-8")
    now_ts = 2_000_000.0
    mtime = now_ts - age_s
    os.utime(f, (mtime, mtime))
    return f


def _write_real(proj_dir: Path, name: str, *, age_s: float = 3600) -> Path:
    """Write a real session with at least one user message."""
    proj_dir.mkdir(parents=True, exist_ok=True)
    f = proj_dir / f"{name}.jsonl"
    f.write_text(_user_line() + "\n", encoding="utf-8")
    now_ts = 2_000_000.0
    mtime = now_ts - age_s
    os.utime(f, (mtime, mtime))
    return f


def _setup(monkeypatch, tmp_path):
    """Redirect all project/archive I/O into tmp_path."""
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(P, "HELIOS_ARCHIVE_ROOT", tmp_path / "helios-archive")
    SP.reload()
    return tmp_path / "projects", tmp_path / "helios-archive"


# ── session_has_content ───────────────────────────────────────────────────────


def test_has_content_sidecar_only(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_sidecar_line("abc", "A title") + "\n", encoding="utf-8")
    assert session_has_content(f) is False


def test_has_content_real_session(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text(_user_line("fix the bug") + "\n", encoding="utf-8")
    assert session_has_content(f) is True


def test_has_content_unreadable_is_conservative(tmp_path):
    # An unreadable/missing file is "unknown", not "empty": callers hide or
    # ARCHIVE empties, so unknown must report True to avoid sweeping a real
    # session that is transiently locked/half-written.
    assert session_has_content(tmp_path / "nonexistent.jsonl") is True


def test_has_content_malformed_line_is_conservative(tmp_path):
    # A line that won't parse means we can't confirm the file is empty.
    f = tmp_path / "bad.jsonl"
    f.write_text("{not valid json\n", encoding="utf-8")
    assert session_has_content(f) is True


def test_has_content_empty_file(tmp_path):
    f = tmp_path / "empty.jsonl"
    f.write_text("", encoding="utf-8")
    assert session_has_content(f) is False


def test_descendant_only_requires_sidechain_content_and_no_primary_turn(tmp_path):
    child = tmp_path / "child.jsonl"
    child.write_text(_sidechain_line() + "\n", encoding="utf-8")
    assert session_is_descendant_only(child) is True

    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        _sidechain_line() + "\n" + _user_line("root request") + "\n",
        encoding="utf-8",
    )
    assert session_is_descendant_only(mixed) is False


def test_descendant_only_is_conservative_for_malformed_or_unreadable_input(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text(_sidechain_line() + "\n{broken\n", encoding="utf-8")
    assert session_is_descendant_only(malformed) is False
    assert session_is_descendant_only(tmp_path / "missing.jsonl") is False


# ── discover_local_sessions — now a PURE lister (no filtering) ────────────────


def test_discover_returns_all_including_ghosts(monkeypatch, tmp_path):
    """discover_local_sessions is a pure lister — filtering is the sidebar's job
    (via drop_empty_sessions), and the archiver wants the full list."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    _write_sidecar(proj, "ghost", age_s=3600)
    _write_real(proj, "real", age_s=3600)

    ids = {s.session_id for s in P.discover_local_sessions()}
    assert ids == {"ghost", "real"}


# ── drop_empty_sessions — the display-layer filter ────────────────────────────

_NOW = 2_000_000.0  # matches the fixed mtime base in the fixtures


def test_drop_filters_old_dead_ghost(monkeypatch, tmp_path):
    """Sidecar-only, past the grace, not live → hidden."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    _write_sidecar(proj, "ghost", age_s=3600)
    kept = P.drop_empty_sessions(
        P.discover_local_sessions(), live_ids=set(), now=_NOW
    )
    assert all(s.session_id != "ghost" for s in kept)


def test_drop_keeps_real_session(monkeypatch, tmp_path):
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    _write_real(proj, "real", age_s=3600)
    kept = P.drop_empty_sessions(
        P.discover_local_sessions(), live_ids=set(), now=_NOW
    )
    assert any(s.session_id == "real" for s in kept)


def test_drop_keeps_recent_ghost(monkeypatch, tmp_path):
    """Within the short grace, a new session that hasn't written its first
    turn yet stays visible."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    _write_sidecar(proj, "new-ghost", age_s=5)  # 5s < 15s grace
    kept = P.drop_empty_sessions(
        P.discover_local_sessions(), live_ids=set(), now=_NOW
    )
    assert any(s.session_id == "new-ghost" for s in kept)


def test_drop_keeps_live_ghost(monkeypatch, tmp_path):
    """A sidecar-only session Helios is actively driving (in live_ids) is kept
    even when old — the chat you just started never winks out."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    _write_sidecar(proj, "live-ghost", age_s=3600)
    kept = P.drop_empty_sessions(
        P.discover_local_sessions(), live_ids={"live-ghost"}, now=_NOW
    )
    assert any(s.session_id == "live-ghost" for s in kept)


def test_drop_keeps_pinned_selected_ghost(monkeypatch, tmp_path):
    """The sidebar pins the currently-selected row's id into the keep set
    (session_list.reload), so an old, driver-less, content-less session the user
    is viewing survives a preserve-selection reload instead of vanishing from
    under them. Modeled here at the pure-function layer: a ghost whose id is
    pinned is kept even with no live driver and past the grace."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    _write_sidecar(proj, "selected-ghost", age_s=3600)
    # keep set = live_ids (empty) | {selected_id}
    kept = P.drop_empty_sessions(
        P.discover_local_sessions(), live_ids={"selected-ghost"}, now=_NOW
    )
    assert any(s.session_id == "selected-ghost" for s in kept)


def test_drop_never_projects_descendant_as_primary_session_row(monkeypatch, tmp_path):
    """The Agent Dock owns descendants, even when their file is live/recent."""

    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    proj.mkdir(parents=True, exist_ok=True)
    child = proj / "child-agent.jsonl"
    child.write_text(_sidechain_line() + "\n", encoding="utf-8")
    os.utime(child, (_NOW - 1, _NOW - 1))

    kept = P.drop_empty_sessions(
        P.discover_local_sessions(),
        live_ids={"child-agent"},
        now=_NOW,
    )

    assert all(session.session_id != "child-agent" for session in kept)


def test_drop_keeps_mixed_root_transcript_with_sidechain_activity(monkeypatch, tmp_path):
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    proj.mkdir(parents=True, exist_ok=True)
    root = proj / "root-session.jsonl"
    root.write_text(
        _sidechain_line() + "\n" + _user_line("primary request") + "\n",
        encoding="utf-8",
    )
    os.utime(root, (_NOW - 3600, _NOW - 3600))

    kept = P.drop_empty_sessions(
        P.discover_local_sessions(),
        live_ids=set(),
        now=_NOW,
    )

    assert any(session.session_id == "root-session" for session in kept)


# ── sweep_orphan_sidecar_sessions ─────────────────────────────────────────────


def test_sweep_moves_sidecar_to_archive(monkeypatch, tmp_path):
    """Sidecar-only file older than 10 min is moved to the archive."""
    projects_dir, archive_root = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    f = _write_sidecar(proj, "ghost", age_s=3600)

    n = P.sweep_orphan_sidecar_sessions(now=2_000_000.0)

    assert n == 1
    assert not f.exists(), "original must be gone after sweep"
    dest = archive_root / "-home-u" / "ghost.jsonl"
    assert dest.exists(), "moved copy must exist in archive"


def test_sweep_does_not_delete_real_session(monkeypatch, tmp_path):
    """Real sessions are never swept."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    f = _write_real(proj, "real", age_s=3600)

    n = P.sweep_orphan_sidecar_sessions(now=2_000_000.0)

    assert n == 0
    assert f.exists()


def test_sweep_skips_live_id(monkeypatch, tmp_path):
    """Sidecar session whose id is in live_ids is left on disk."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    f = _write_sidecar(proj, "live-sid", age_s=3600)

    n = P.sweep_orphan_sidecar_sessions(live_ids={"live-sid"}, now=2_000_000.0)

    assert n == 0
    assert f.exists()


def test_sweep_skips_recent_sidecar(monkeypatch, tmp_path):
    """Sidecar-only sessions younger than 10 min are not swept."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    f = _write_sidecar(proj, "new-ghost", age_s=300)  # 5 min old

    n = P.sweep_orphan_sidecar_sessions(now=2_000_000.0)

    assert n == 0
    assert f.exists()


def test_sweep_clears_provider_entry(monkeypatch, tmp_path):
    """sweep_orphan_sidecar_sessions calls session_providers.forget."""
    projects_dir, _ = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    _write_sidecar(proj, "ghost-openai", age_s=3600)
    SP.set_provider("ghost-openai", "openai")

    P.sweep_orphan_sidecar_sessions(now=2_000_000.0)

    assert SP.provider_for("ghost-openai") == ""  # unknown after forget


def test_sweep_moves_aux_dir_alongside(monkeypatch, tmp_path):
    """The aux dir (same stem, no suffix) is moved together with the .jsonl."""
    projects_dir, archive_root = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    f = _write_sidecar(proj, "ghost", age_s=3600)
    aux = proj / "ghost"
    aux.mkdir()
    (aux / "extra.txt").write_text("data", encoding="utf-8")

    P.sweep_orphan_sidecar_sessions(now=2_000_000.0)

    assert not f.exists()
    assert not aux.exists()
    assert (archive_root / "-home-u" / "ghost.jsonl").exists()
    assert (archive_root / "-home-u" / "ghost" / "extra.txt").exists()


def test_sweep_collision_gets_suffix(monkeypatch, tmp_path):
    """If archive destination already exists a .1 suffix is used."""
    projects_dir, archive_root = _setup(monkeypatch, tmp_path)
    proj = projects_dir / "-home-u"
    f = _write_sidecar(proj, "ghost", age_s=3600)

    # Simulate a prior archive entry.
    prior = archive_root / "-home-u"
    prior.mkdir(parents=True)
    (prior / "ghost.jsonl").write_text("{}", encoding="utf-8")

    P.sweep_orphan_sidecar_sessions(now=2_000_000.0)

    assert not f.exists()
    assert (archive_root / "-home-u" / "ghost.1.jsonl").exists()
    assert (archive_root / "-home-u" / "ghost.jsonl").exists()  # prior untouched
