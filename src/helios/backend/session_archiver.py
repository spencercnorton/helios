"""Automatic old-session archival.

Sessions older than the retention window are moved out of
``~/.claude/projects`` so they disappear from the active sidebar, but the raw
JSONL is preserved under ``~/.helios/session-archive``. A caller may supply a
summarizer to write a short memory note when the transcript has durable value;
automatic archival itself never creates unmetered cloud-model work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from helios.backend import projects, session_providers
from helios.backend.claude_binary import (
    ClaudeBinaryNotFound,
    find_claude_binary,
    supports_max_budget_usd_flag,
)
from helios.backend.process.env_scrub import CLAUDE_AUTH_ENV, scrubbed_child_env
from helios.backend.projects import Session
from helios.backend.session_state import discover_active_session_ids
from helios.backend.transcript import parse_transcript
from helios.log import get_logger

_log = get_logger("session-archiver")

RETENTION_DAYS = 4
RETENTION_SECONDS = RETENTION_DAYS * 86400
ARCHIVE_ROOT = Path.home() / ".helios" / "session-archive"
MAX_PER_RUN = 8
SUMMARY_MAX_BUDGET_USD = 0.25

_SUMMARY_PROMPT = """\
You are reviewing an old coding-assistant session before archival.

Return NO_MEMORY if the transcript contains no durable project/user knowledge.
Otherwise write a concise markdown memory note with:
- what mattered
- files/systems involved
- any follow-up or gotcha worth preserving

Do not include chat filler, tool logs, or generic process commentary.

----- BEGIN TRANSCRIPT TAIL -----
{tail}
----- END TRANSCRIPT TAIL -----
"""


@dataclass(slots=True)
class ArchiveResult:
    session_id: str
    archived_to: Path
    memory_path: Path | None = None


@dataclass(slots=True)
class ArchiveReport:
    archived: list[ArchiveResult] = field(default_factory=list)
    skipped_live: int = 0
    errors: list[str] = field(default_factory=list)


Summarizer = Callable[[Session], str]


def archive_old_sessions(
    *,
    now: float | None = None,
    live_ids: set[str] | None = None,
    max_sessions: int = MAX_PER_RUN,
    summarizer: Summarizer | None = None,
) -> ArchiveReport:
    """Archive local sessions older than four days.

    ``summarizer`` is injectable for explicit/manual workflows and tests. The
    automatic production path does not call a cloud model: archival is
    housekeeping, not an authorization to create a separate provider budget.
    """
    now = now if now is not None else time.time()
    live_ids = discover_active_session_ids(extra=live_ids or set())
    summarizer = summarizer or _no_summary
    report = ArchiveReport()
    cutoff = now - RETENTION_SECONDS
    archived_count = 0
    for session in projects.discover_local_sessions():
        if archived_count >= max_sessions:
            break
        if session.session_id in live_ids:
            report.skipped_live += 1
            continue
        if session.mtime >= cutoff:
            continue
        try:
            memory_path = _write_memory_if_useful(session, summarizer(session))
            dest = _move_session_to_archive(session)
            session_providers.forget(session.session_id)
            report.archived.append(
                ArchiveResult(
                    session_id=session.session_id,
                    archived_to=dest,
                    memory_path=memory_path,
                )
            )
            archived_count += 1
        except Exception as e:
            _log.warning("failed to archive session %s: %s", session.session_id, e)
            report.errors.append(f"{session.session_id}: {e}")
    return report


def _no_summary(_session: Session) -> str:
    """Default archiver policy: preserve data without hidden model spend."""
    return ""


def summarize_with_haiku(session: Session) -> str:
    tail = _transcript_tail_for_prompt(session)
    if not tail:
        return ""
    try:
        binary = find_claude_binary().path
    except ClaudeBinaryNotFound:
        return ""
    if not supports_max_budget_usd_flag():
        return ""
    prompt = _SUMMARY_PROMPT.format(tail=tail)
    try:
        proc = subprocess.run(
            [
                str(binary),
                "--print",
                "--model",
                "haiku",
                "--max-thinking-tokens",
                "0",
                "--max-budget-usd",
                f"{SUMMARY_MAX_BUDGET_USD:g}",
                # Scoped down to what summarising a transcript needs, the same
                # way title generation was before. Measured on the development workstation
                # 2026-08-05: 34,377 prompt tokens with `--setting-sources
                # user`, 23,965 with these three flags — 10,412 fewer (30%)
                # for the same job. It also matters for trust: the tail is
                # untrusted text, and `--setting-sources user` put the global
                # CLAUDE.md, its memory index and every configured MCP server
                # in front of a model that is reading it.
                #
                # ORDER IS LOAD-BEARING: `--mcp-config` is variadic, so it must
                # never be the last option before the positional prompt or it
                # swallows it and the model summarises nothing. Keep a
                # non-variadic flag between them (see the argv test).
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--setting-sources",
                "",
                "--permission-mode",
                "dontAsk",
                "--allowed-tools",
                "",
                "--no-session-persistence",
                prompt,
            ],
            cwd=str(Path.home()),
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
            # Same scoped policy as the interactive Claude driver.
            env=scrubbed_child_env(keep=CLAUDE_AUTH_ENV),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    text = proc.stdout.strip()
    if not text or text.upper().startswith("NO_MEMORY"):
        return ""
    return text


def _transcript_tail_for_prompt(session: Session, *, max_turns: int = 14) -> str:
    parts: list[str] = []
    for turn in parse_transcript(session.path):
        if turn.role not in ("user", "assistant") or turn.is_meta:
            continue
        text = turn.text.strip()
        if not text:
            continue
        if len(text) > 1200:
            text = text[:1197] + "..."
        parts.append(f"{turn.role.upper()}: {text}")
    return "\n\n".join(parts[-max_turns:])


def _write_memory_if_useful(session: Session, summary: str) -> Path | None:
    summary = (summary or "").strip()
    if not summary:
        return None
    mem_dir = session.project.path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    path = mem_dir / f"archived-session-{session.session_id[:8]}.md"
    title = session.ensure_title()
    body = (
        "---\n"
        f"name: archived-session-{session.session_id[:8]}\n"
        f"description: Summary extracted before Helios archived session {session.session_id}\n"
        "metadata:\n"
        "  node_type: memory\n"
        "  type: session-archive\n"
        f"  originSessionId: {session.session_id}\n"
        "---\n\n"
        f"# Archived session: {title}\n\n"
        f"- Session: `{session.session_id}`\n"
        f"- CWD: `{session.project.cwd}`\n"
        f"- Archived source: `{session.path}`\n\n"
        f"{summary}\n"
    )
    path.write_text(body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _move_session_to_archive(session: Session) -> Path:
    dest_dir = ARCHIVE_ROOT / session.project.dirname
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(dest_dir / session.path.name)
    shutil.move(str(session.path), str(dest))

    aux = session.path.with_suffix("")
    if aux.is_dir():
        shutil.move(str(aux), str(_unique_path(dest_dir / aux.name)))
    return dest


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        cand = path.with_name(f"{stem}.{i}{suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"too many archive collisions for {path}")
