"""Build shared-scratchpad handoff payloads from a local Helios session.

"Hand off this session" writes one `handoff/<slug>-<date>` entry to the
shared scratchpad so any Helios session on the tailnet can pick the work up:
where it ran, how to resume it, and the tail of the conversation. This module
is the GTK-free payload side; the dialog (widgets/handoff_dialog.py) owns the
UI and the actual network write (backend/scratchpad.py).
"""

from __future__ import annotations

import re
import socket
from datetime import date, datetime, timezone
from pathlib import Path

from helios.backend.projects import Session
from helios.backend import model_catalog, session_providers
from helios.backend.transcript import parse_transcript

KEY_PREFIX = "handoff/"
REPLY_PREFIX = "agent-replies/"

# Tail sizing: enough conversation for the next session to orient without
# shipping the whole transcript into a store meant for digests.
TAIL_TURNS = 12
TAIL_CHARS = 700

_SLUG_MAX = 48

#: Shown instead of a shell command for providers Helios runs in-process.
_NO_SHELL_RESUME = "reopen this session in Helios — it has no CLI to resume from"

_PROVIDER_LABELS = {
    model_catalog.PROVIDER_ANTHROPIC: "Claude",
    model_catalog.PROVIDER_OPENAI: "GPT",
}


def host_name() -> str:
    """Short hostname, matching the session-pool subdir convention."""
    return socket.gethostname().split(".")[0].lower()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:_SLUG_MAX].rstrip("-")


def suggest_key(title: str, session_id: str, *, today: date | None = None) -> str:
    """`handoff/<slug>-<YYYY-MM-DD>` — the key namespace every Helios session
    already watches via session_context."""
    day = (today or date.today()).isoformat()
    slug = _slugify(title) or (session_id[:8].lower() if session_id else "session")
    return f"{KEY_PREFIX}{slug}-{day}"


def suggest_reply_key(session_id: str, *, today: date | None = None) -> str:
    day = (today or date.today()).isoformat()
    sid = (session_id or "session")[:8].lower()
    return f"{REPLY_PREFIX}{sid}-{day}"


def default_summary(session: Session, title: str) -> str:
    """Prefill for the editable summary field. The convention for good
    handoff summaries is specifics over labels — this gives the resume
    coordinates and leaves the findings to the human."""
    resolution = session_providers.resolve_provider(
        session.session_id,
        session.path,
    )
    if not resolution.known:
        return (
            f"{title} — provider ownership is unverified; hand-off is disabled "
            f"until Helios can validate {session.session_id}."
        )
    resume = _resume_command(session)
    source = provider_label(resolution.provider)
    recipient = provider_label(
        default_recipient_provider(session.session_id, session.path)
    )
    return (
        f"{title} — {source} handoff for {recipient}, from Helios on {host_name()} "
        f"(cwd {session.project.cwd}). "
        f"Resume: {resume if resume else _NO_SHELL_RESUME}"
    )


def transcript_tail(
    path: Path, *, turns: int = TAIL_TURNS, max_chars: int = TAIL_CHARS
) -> list[dict]:
    """Last `turns` user/assistant text messages, each truncated. Tool calls,
    thinking, and meta records are skipped — the tail is for orientation, not
    replay. Returns [] when the transcript is missing or unreadable."""
    out: list[dict] = []
    for t in parse_transcript(path):
        if t.role not in ("user", "assistant") or t.is_meta:
            continue
        text = t.text.strip()
        if not text:
            continue
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        entry = {"role": t.role, "text": text}
        if t.timestamp:
            entry["t"] = t.timestamp
        out.append(entry)
    return out[-turns:]


def build_payload(
    session: Session,
    title: str,
    *,
    include_tail: bool = True,
    recipient_provider: str = "",
) -> dict:
    """The entry's `data` blob: resume coordinates + optional conversation
    tail. Everything a cold session needs to decide whether to pick this up."""
    resolution = session_providers.resolve_provider(
        session.session_id,
        session.path,
    )
    if not resolution.known:
        state = "conflicting" if resolution.conflicted else "unknown"
        raise ValueError(
            f"session provider ownership is {state}; refusing to publish a resume command"
        )
    resume = _resume_command(session)
    source_provider = resolution.provider
    recipient_provider = recipient_provider or default_recipient_provider(
        session.session_id,
        session.path,
    )
    reply_key = suggest_reply_key(session.session_id)
    payload: dict = {
        "origin": {"app": "helios", "host": host_name()},
        "kind": "agent-contact",
        "title": title,
        "session_id": session.session_id,
        "cwd": session.project.cwd,
        "provider": source_provider,
        "from_provider": source_provider,
        "to_provider": recipient_provider,
        "from_agent": provider_label(source_provider),
        "to_agent": provider_label(recipient_provider),
        "reply_key": reply_key,
        "reply_tags": contact_tags(recipient_provider, source_provider, is_reply=True),
        "instructions": (
            "Recipient agent: use transcript_tail and resume coordinates to orient. "
            f"If a response is useful, write it to scratchpad key {reply_key} "
            "with the supplied reply_tags."
        ),
        "resume": (
            f"cd {session.project.cwd} && {resume}"
            if resume
            else f"{_NO_SHELL_RESUME} (cwd {session.project.cwd})"
        ),
        "last_activity": datetime.fromtimestamp(
            session.mtime, tz=timezone.utc
        ).isoformat(),
        "transcript_path": str(session.path),
    }
    if include_tail:
        payload["transcript_tail"] = transcript_tail(session.path)
    return payload


def _resume_command(session: Session) -> str | None:
    """The shell command that reopens this session, or `None` if none exists.

    Exhaustive by provider on purpose. This used to be `if OPENAI: codex …`
    followed by a bare `return claude --resume …`, so **OpenRouter inherited
    Claude's command** — and an OpenRouter session has no CLI at all: the agent
    loop runs in-process and its state lives in `~/.helios/openrouter-sessions/`.
    A handoff whose resume line does not work is worse than one that says so.

    An unknown provider now raises rather than falling through to Claude: the
    implicit else is what shipped the bug, and the next provider added would
    have inherited it the same way.
    """
    resolution = session_providers.resolve_provider(
        session.session_id,
        session.path,
    )
    if not resolution.known:
        raise ValueError("cannot resume a session whose provider is unresolved")
    if resolution.provider == model_catalog.PROVIDER_ANTHROPIC:
        return f"claude --resume {session.session_id}"
    if resolution.provider == model_catalog.PROVIDER_OPENAI:
        # Verified against codex-cli 0.146.1: `exec` still carries a `resume`
        # subcommand. What AGENTS.md retires is Helios's own automatic
        # `codex exec` driver fallback, not the command a human types.
        return f"codex exec resume {session.session_id}"
    if resolution.provider == model_catalog.PROVIDER_OPENROUTER:
        return None
    raise ValueError(f"no resume command for provider {resolution.provider!r}")


def default_recipient_provider(
    session_id: str,
    transcript_path: Path | None = None,
) -> str:
    provider = session_providers.resolve_provider(
        session_id,
        transcript_path,
    ).provider
    if provider == model_catalog.PROVIDER_OPENAI:
        return model_catalog.PROVIDER_ANTHROPIC
    return model_catalog.PROVIDER_OPENAI


def provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider, provider or "agent")


def contact_tags(
    source_provider: str,
    recipient_provider: str,
    *,
    is_reply: bool = False,
) -> list[str]:
    tags = [
        "handoff",
        "helios",
        "agent-contact",
        f"from-{source_provider}",
        f"to-{recipient_provider}",
    ]
    if is_reply:
        tags.append("agent-reply")
    return tags
