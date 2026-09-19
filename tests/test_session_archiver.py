from __future__ import annotations

import json
import os

from helios.backend import session_archiver
import helios.backend.projects as P
import helios.backend.session_providers as SP


def _record(role: str, text: str) -> str:
    return json.dumps({
        "type": role,
        "cwd": "/home/u/proj",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    })


def _session(tmp_path, *, age_s: int = 10 * 86400):
    proj_dir = tmp_path / "projects" / "-home-u-proj"
    proj_dir.mkdir(parents=True)
    path = proj_dir / "sid12345.jsonl"
    path.write_text("\n".join([
        _record("user", "fix auth"),
        _record("assistant", "changed auth.py"),
    ]) + "\n", encoding="utf-8")
    now = 2_000_000.0
    old = now - age_s
    os.utime(path, (old, old))
    return now, path


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(session_archiver, "ARCHIVE_ROOT", tmp_path / "archive")
    # HELIOS_STATE_DIR is already redirected to tmp_path by the autouse
    # isolate_state_dir fixture in conftest.py.
    monkeypatch.setattr(
        session_archiver,
        "discover_active_session_ids",
        lambda extra=None: set(extra or ()),
    )
    SP.reload()


def test_archives_old_session_and_writes_memory(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    now, path = _session(tmp_path)
    SP.set_provider("sid12345", "openai")

    report = session_archiver.archive_old_sessions(
        now=now,
        summarizer=lambda _s: "Durable auth finding.",
    )

    assert len(report.archived) == 1
    assert not path.exists()
    assert report.archived[0].archived_to.exists()
    assert report.archived[0].memory_path is not None
    assert "Durable auth finding" in report.archived[0].memory_path.read_text()
    assert SP.provider_for("sid12345") == ""


def test_skips_recent_and_live_sessions(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    now, path = _session(tmp_path, age_s=2 * 86400)
    report = session_archiver.archive_old_sessions(now=now)
    assert report.archived == []
    assert path.exists()

    os.utime(path, (now - 10 * 86400, now - 10 * 86400))
    report = session_archiver.archive_old_sessions(now=now, live_ids={"sid12345"})
    assert report.archived == []
    assert report.skipped_live == 1
    assert path.exists()


def test_no_memory_written_when_summary_empty(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    now, path = _session(tmp_path)
    report = session_archiver.archive_old_sessions(now=now, summarizer=lambda _s: "")
    assert len(report.archived) == 1
    assert report.archived[0].memory_path is None
    assert not path.exists()


def test_automatic_archival_never_starts_hidden_cloud_summary(
    monkeypatch, tmp_path
):
    _redirect(monkeypatch, tmp_path)
    now, path = _session(tmp_path)
    monkeypatch.setattr(
        session_archiver,
        "summarize_with_haiku",
        lambda _session: (_ for _ in ()).throw(
            AssertionError("automatic archive called Claude")
        ),
    )

    report = session_archiver.archive_old_sessions(now=now)

    assert len(report.archived) == 1
    assert report.archived[0].memory_path is None
    assert not path.exists()


def _capture_summary_argv(monkeypatch, tmp_path):
    """Run summarize_with_haiku against a stub subprocess and return its argv."""

    seen: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "durable note"
        stderr = ""

    monkeypatch.setattr(
        session_archiver, "find_claude_binary", lambda: type("B", (), {"path": "/c"})()
    )
    monkeypatch.setattr(session_archiver, "supports_max_budget_usd_flag", lambda: True)
    monkeypatch.setattr(
        session_archiver.subprocess,
        "run",
        lambda argv, **kw: (seen.append(list(argv)), _Proc())[1],
    )
    _redirect(monkeypatch, tmp_path)
    _now, path = _session(tmp_path)
    session = P.Session(
        project=None, session_id="sid12345", path=path, mtime=0.0, size=path.stat().st_size
    )
    assert session_archiver.summarize_with_haiku(session) == "durable note"
    return seen[0]


def test_summary_spawn_carries_no_user_context(monkeypatch, tmp_path):
    """The transcript tail is untrusted text and naming what it contains needs
    no CLAUDE.md, no memory index and no MCP servers. Measured on the workstation
    2026-08-05: 34,377 prompt tokens before, 23,965 after."""

    argv = _capture_summary_argv(monkeypatch, tmp_path)

    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--setting-sources") + 1] == ""
    # Still a no-tools, budget-capped, non-persisted call.
    assert argv[argv.index("--allowed-tools") + 1] == ""
    assert "--no-session-persistence" in argv


def test_summary_prompt_survives_the_variadic_options(monkeypatch, tmp_path):
    """`--mcp-config` and `--allowed-tools` are variadic: if either is the last
    option before the positional prompt, the CLI eats the prompt as another
    value and the model summarises nothing. Only the exit code would look
    fine, so pin the ordering."""

    argv = _capture_summary_argv(monkeypatch, tmp_path)

    assert argv[-1].startswith("You are reviewing an old coding-assistant session")
    for variadic in ("--mcp-config", "--allowed-tools"):
        follower = argv[argv.index(variadic) + 2]
        assert follower.startswith("--"), f"{variadic} value must be followed by a flag"
