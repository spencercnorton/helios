"""Full-text search across local session transcripts.

A from-scratch scan of `~/.claude/projects/*/*.jsonl` for a query string in the
human-readable text of user/assistant messages (and thinking blocks). One hit
per session — the first matching snippet — newest session first. Dependency-
free and cancellable; meant to run on a worker thread behind a search UI.

We scan local projects only: the remote pool lives on a slow CIFS/iCloud mount
where a full grep would stall for many seconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from helios.backend.projects import PROJECTS_DIR, _first_text, decode_project_dirname
from helios.backend.title_store import store as title_store
from helios.backend.transcript import strip_injected_context

# Don't read unbounded — a pathological multi-hundred-MB transcript shouldn't
# freeze a search. We still find matches in the first slice of any file.
_MAX_BYTES_PER_FILE = 8 * 1024 * 1024
_SNIPPET_RADIUS = 60  # chars of context shown on each side of the match


@dataclass(slots=True)
class SearchHit:
    session_id: str
    project_dirname: str
    project_cwd: str
    snippet: str
    when: float  # transcript mtime, for sorting/labelling
    title: str = ""  # stored title, else first-user-message fallback


def _texts(content) -> list[str]:
    """Pull human-readable strings out of a message `content` field."""
    out: list[str] = []
    if isinstance(content, str):
        if content.strip():
            out.append(content)
    elif isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") in (
                "text",
                "thinking",
                "commentary",
                "reasoning_summary",
            ):
                t = blk.get("text") or blk.get("thinking") or ""
                if t.strip():
                    out.append(t)
    return out


def _snippet(text: str, needle_lower: str) -> str:
    i = text.lower().find(needle_lower)
    if i < 0:
        return ""
    start = max(0, i - _SNIPPET_RADIUS)
    end = min(len(text), i + len(needle_lower) + _SNIPPET_RADIUS)
    frag = " ".join(text[start:end].split())
    if start > 0:
        frag = "…" + frag
    if end < len(text):
        frag = frag + "…"
    return frag


def _fallback_title(content) -> str:
    """First-user-message title, derived exactly as `Session.ensure_title`.

    Deliberately uses projects._first_text (text blocks only) rather than the
    looser `_texts` above, so a search row and a sidebar row for the same
    un-titled session read identically.
    """
    text = strip_injected_context(_first_text(content)).strip()
    if not text:
        return ""
    title = " ".join(text.split())
    return title[:77] + "..." if len(title) > 80 else title


def _first_hit_in_file(path: Path, needle_lower: str, *, want_title: bool = False) -> tuple[str, str]:
    """Return (first matching snippet, fallback title) for `path`.

    Both are "" when absent. `want_title` is what keeps the cheap reject below
    a fast path: a line that cannot contain the needle is only JSON-parsed
    while we still owe the caller a fallback title, i.e. never for a session
    that already has a stored one, and only up to the first user message
    otherwise. Do not drop the flag — ungated, every search parses every line
    of every transcript up to the byte cap.
    """
    snip = ""
    fallback = ""
    try:
        read = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                read += len(line)
                if read > _MAX_BYTES_PER_FILE:
                    break
                matched = needle_lower in line.lower()
                if not matched and not want_title:
                    continue  # cheap reject before the JSON parse
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = obj.get("type")
                if kind not in ("user", "assistant"):
                    continue
                content = (obj.get("message") or {}).get("content")
                if want_title and kind == "user":
                    fallback = _fallback_title(content)
                    if fallback:
                        want_title = False
                if matched and not snip:
                    for text in _texts(content):
                        found = _snippet(text, needle_lower)
                        if found:
                            snip = found
                            break
                if snip and not want_title:
                    break
    except OSError:
        return "", ""
    return snip, fallback


def search_sessions(query: str, *, limit: int = 200, should_cancel=None) -> list[SearchHit]:
    """Search local transcripts for `query` (case-insensitive).

    `should_cancel`, if given, is polled between files; return True from it to
    abandon a superseded search. Returns up to `limit` hits, newest first.
    """
    query = (query or "").strip()
    if len(query) < 2 or not PROJECTS_DIR.exists():
        return []
    needle = query.lower()
    titles = title_store()  # process singleton; .get reads live data, not a snapshot

    # Gather (mtime, file) newest-first so the most recent matches surface
    # first and we can stop early once we hit the limit.
    files: list[tuple[float, Path]] = []
    try:
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir() or "titlegen" in proj.name:
                continue
            for f in proj.glob("*.jsonl"):
                try:
                    files.append((f.stat().st_mtime, f))
                except OSError:
                    continue
    except OSError:
        return []
    files.sort(key=lambda t: t[0], reverse=True)

    hits: list[SearchHit] = []
    for mtime, path in files:
        if should_cancel is not None and should_cancel():
            break
        title = titles.get(path.stem) or ""
        snip, fallback = _first_hit_in_file(path, needle, want_title=not title)
        if not snip:
            continue
        dirname = path.parent.name
        hits.append(
            SearchHit(
                session_id=path.stem,
                project_dirname=dirname,
                project_cwd=decode_project_dirname(dirname),
                snippet=snip,
                when=mtime,
                title=title or fallback,
            )
        )
        if len(hits) >= limit:
            break
    return hits
