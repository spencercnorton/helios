"""Tests for project discovery helpers: dir-name encoding, cwd-from-transcript,
and the empty-dir sweep."""
from __future__ import annotations

import json
from pathlib import Path

import helios.backend.projects as P
from helios.backend.projects import (
    decode_project_dirname,
    encode_project_dirname,
)


def test_encode_roundtrip_simple():
    assert encode_project_dirname("/home/alice") == "-home-alice"


def test_encode_decode_existing_path(tmp_path):
    # decode prefers an interpretation that exists on disk.
    real = tmp_path / "a" / "b"
    real.mkdir(parents=True)
    enc = encode_project_dirname(str(real))
    assert decode_project_dirname(enc) == str(real)


def test_decode_nonexistent_falls_back_to_slashes():
    assert decode_project_dirname("-home-alice-nope") == "/home/alice/nope"


def test_cwd_from_transcript_reads_field(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"type": "user", "cwd": "/srv/weird-dash/proj", "message": {}}) + "\n",
        encoding="utf-8",
    )
    assert P._cwd_from_transcript(p) == "/srv/weird-dash/proj"


def test_cwd_from_transcript_skips_records_without_cwd(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"type": "summary"}) + "\n"
        + json.dumps({"type": "assistant", "cwd": "/the/cwd"}) + "\n",
        encoding="utf-8",
    )
    assert P._cwd_from_transcript(p) == "/the/cwd"


def test_cwd_from_transcript_empty_when_absent(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"type": "user"}) + "\n", encoding="utf-8")
    assert P._cwd_from_transcript(p) == ""


def test_scan_project_dir_uses_transcript_cwd(tmp_path):
    # A dir whose dash-decode would be WRONG, but the transcript has the truth.
    d = tmp_path / "-srv-weird-dash-proj"
    d.mkdir()
    (d / "s.jsonl").write_text(
        json.dumps({"type": "user", "cwd": "/srv/weird-dash/proj"}) + "\n",
        encoding="utf-8",
    )
    proj = P._scan_project_dir(d, origin="local", read_only=False)
    assert proj is not None
    assert proj.cwd == "/srv/weird-dash/proj"


def test_scan_project_dir_skips_empty(tmp_path):
    d = tmp_path / "-empty"
    d.mkdir()
    assert P._scan_project_dir(d, origin="local", read_only=False) is None


def test_sweep_removes_only_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    empty = tmp_path / "-empty"
    empty.mkdir()
    full = tmp_path / "-full"
    full.mkdir()
    (full / "s.jsonl").write_text("{}", encoding="utf-8")
    removed = P.sweep_empty_project_dirs()
    assert removed == 1
    assert not empty.exists()
    assert full.exists()


# ── Unified session list backend ─────────────────────────────────────


def test_is_throwaway_cwd():
    assert P.is_throwaway_cwd("/tmp")
    assert P.is_throwaway_cwd("/tmp/tmp-abc123")
    assert P.is_throwaway_cwd("/home/alice/_tmp_sess_repo")
    assert P.is_throwaway_cwd("/srv/_tmp_x/inner")
    assert not P.is_throwaway_cwd("/home/alice")
    assert not P.is_throwaway_cwd("/home/alice/helios")
    assert not P.is_throwaway_cwd("/home/alice/tmpfiles")  # 'tmp' prefix ≠ /tmp


def _write_session(proj_dir, name: str, cwd: str, mtime: float) -> None:
    import os
    proj_dir.mkdir(parents=True, exist_ok=True)
    f = proj_dir / f"{name}.jsonl"
    f.write_text(
        json.dumps({"type": "user", "cwd": cwd, "message": {"content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    os.utime(f, (mtime, mtime))


def test_discover_local_sessions_merges_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    a = tmp_path / "-home-u"
    b = tmp_path / "-home-u-proj"
    _write_session(a, "old", "/home/u", 1000.0)
    _write_session(a, "newest", "/home/u", 3000.0)
    _write_session(b, "middle", "/home/u/proj", 2000.0)

    sessions = P.discover_local_sessions()
    assert [s.session_id for s in sessions] == ["newest", "middle", "old"]
    # Sessions keep their project backref (origin metadata for the row chips).
    assert sessions[1].project.cwd == "/home/u/proj"
    assert all(not s.project.read_only for s in sessions)


def test_discover_pool_sessions_skips_self_host(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "POOL_ROOT", tmp_path)
    monkeypatch.setattr(P, "_self_pool_host", lambda: "workstation")
    _write_session(tmp_path / "workstation" / "-home-u", "mine", "/home/u", 1000.0)
    _write_session(tmp_path / "macbook" / "-Users-x", "theirs", "/Users/x", 2000.0)

    sessions = P.discover_pool_sessions()
    assert [s.session_id for s in sessions] == ["theirs"]
    assert sessions[0].project.origin == "macbook"
    assert sessions[0].project.read_only


def test_pool_sessions_exclude_only_proven_descendant_transcripts(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(P, "POOL_ROOT", tmp_path)
    monkeypatch.setattr(P, "_self_pool_host", lambda: "workstation")
    project = tmp_path / "macbook" / "-Users-x"
    _write_session(project, "root", "/Users/x", 1000.0)

    sidechain = json.dumps(
        {
            "type": "assistant",
            "cwd": "/Users/x",
            "isSidechain": True,
            "message": {"role": "assistant", "content": "child work"},
        }
    )
    child = project / "child.jsonl"
    child.write_text(sidechain + "\n", encoding="utf-8")
    mixed = project / "mixed.jsonl"
    mixed.write_text(
        sidechain
        + "\n"
        + json.dumps(
            {
                "type": "user",
                "cwd": "/Users/x",
                "message": {"role": "user", "content": "root request"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    unreadable = project / "unreadable.jsonl"
    unreadable.write_text("root content cannot be classified\n", encoding="utf-8")

    real_open = Path.open

    def _pool_open(path: Path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("simulated remote read failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _pool_open)

    sessions = P.discover_pool_sessions()

    assert {session.session_id for session in sessions} == {
        "root",
        "mixed",
        "unreadable",
    }
    assert all(session.project.read_only for session in sessions)


def test_ensure_local_project_creates_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    cwd = str(tmp_path / "work")
    proj = P.ensure_local_project(cwd)
    assert proj.cwd == cwd
    assert proj.path.is_dir()
    assert not proj.read_only
    # Idempotent: second call reuses the same dir.
    again = P.ensure_local_project(cwd)
    assert again.path == proj.path


def test_ensure_local_project_picks_up_existing_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path)
    cwd = "/home/u/thing"
    d = tmp_path / P.encode_project_dirname(cwd)
    _write_session(d, "s1", cwd, 1000.0)
    proj = P.ensure_local_project(cwd)
    assert proj.cwd == cwd
    assert [s.session_id for s in proj.load_sessions()] == ["s1"]


# ── Stale-throwaway auto-archive ─────────────────────────────────────

# All archive tests pin `now` so age math is deterministic, and use cwds
# under tmp_path (never real /tmp) so existence checks are controlled.
_NOW = 10_000_000.0
_OLD = _NOW - 20 * 86400  # well past the 14-day threshold
_FRESH = _NOW - 2 * 86400


def _archive_env(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(P, "ARCHIVE_DIR", tmp_path / "archive")
    return tmp_path / "projects", tmp_path / "archive"


def test_archive_moves_dead_throwaway(tmp_path, monkeypatch):
    projects, archive = _archive_env(tmp_path, monkeypatch)
    gone_cwd = str(tmp_path / "_tmp_sess_repo")  # throwaway, never created
    d = projects / P.encode_project_dirname(gone_cwd)
    _write_session(d, "s1", gone_cwd, _OLD)
    assert P.archive_stale_throwaways(now=_NOW) == 1
    assert not d.exists()
    assert (archive / d.name / "s1.jsonl").is_file()


def test_archive_skips_non_throwaway(tmp_path, monkeypatch):
    projects, _ = _archive_env(tmp_path, monkeypatch)
    # NOT under tmp_path: pytest tmp dirs live in /tmp, which is itself a
    # throwaway prefix. This cwd is gone and old — but a real project path.
    gone_cwd = "/home/u/real-project-c9e1a"
    d = projects / P.encode_project_dirname(gone_cwd)
    _write_session(d, "s1", gone_cwd, _OLD)
    assert P.archive_stale_throwaways(now=_NOW) == 0
    assert d.exists()


def test_archive_skips_recent(tmp_path, monkeypatch):
    projects, _ = _archive_env(tmp_path, monkeypatch)
    gone_cwd = str(tmp_path / "_tmp_recent")
    d = projects / P.encode_project_dirname(gone_cwd)
    _write_session(d, "s1", gone_cwd, _FRESH)
    assert P.archive_stale_throwaways(now=_NOW) == 0
    assert d.exists()


def test_archive_skips_multi_transcript(tmp_path, monkeypatch):
    projects, _ = _archive_env(tmp_path, monkeypatch)
    gone_cwd = str(tmp_path / "_tmp_busy")
    d = projects / P.encode_project_dirname(gone_cwd)
    _write_session(d, "s1", gone_cwd, _OLD)
    _write_session(d, "s2", gone_cwd, _OLD)
    assert P.archive_stale_throwaways(now=_NOW) == 0
    assert d.exists()


def test_archive_skips_live_cwd(tmp_path, monkeypatch):
    projects, _ = _archive_env(tmp_path, monkeypatch)
    live_cwd = tmp_path / "_tmp_inflight"
    live_cwd.mkdir()  # the working dir still exists — work may be in flight
    d = projects / P.encode_project_dirname(str(live_cwd))
    _write_session(d, "s1", str(live_cwd), _OLD)
    assert P.archive_stale_throwaways(now=_NOW) == 0
    assert d.exists()


def test_archive_collision_gets_suffix(tmp_path, monkeypatch):
    projects, archive = _archive_env(tmp_path, monkeypatch)
    gone_cwd = str(tmp_path / "_tmp_again")
    d = projects / P.encode_project_dirname(gone_cwd)
    _write_session(d, "s1", gone_cwd, _OLD)
    # Same dirname already archived once before.
    prior = archive / d.name
    prior.mkdir(parents=True)
    (prior / "old.jsonl").write_text("{}", encoding="utf-8")
    assert P.archive_stale_throwaways(now=_NOW) == 1
    assert (archive / f"{d.name}.1" / "s1.jsonl").is_file()
    assert (prior / "old.jsonl").is_file()  # earlier archive untouched
