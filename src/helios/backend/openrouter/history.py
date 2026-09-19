"""Authoritative OpenRouter message history for lossless resume.

OpenRouter is stateless — every turn re-sends the full conversation. The
Claude-format JSONL mirror (``openrouter_transcript``) is display-only and
elides large tool results, so it can't serve as the model's memory on resume.
This module keeps the exact OpenAI message array under
``~/.helios/openrouter-sessions/<session_id>.json`` (0600, atomic replace).

If the history file is missing (e.g. state dir wiped), ``messages_from_mirror``
reconstructs a best-effort array from the elided transcript so the session is
amnesiac rather than broken.

Because every turn re-sends everything, the array also has to be *bounded*:
``compact`` drops whole oldest exchanges once the estimated prompt exceeds the
model's window. Without it a session that fills its window raises
``CONTEXT_LENGTH`` on every subsequent send, permanently — the history is never
trimmed, so the next attempt is the same oversized request.

GTK-free.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from helios.backend import model_catalog
from helios.backend.openrouter.continuity import CAPSULE_PREFIX, ContextArchive, capsule, source_records
from helios.backend.process.codex_transcript import _VERSION_TAGS
from helios.log import get_logger
from helios.paths import state_dir

_log = get_logger("openrouter-history")

__all__ = [
    "HistoryLog",
    "build_assistant_message",
    "compact",
    "estimate_tokens",
    "messages_from_mirror",
    "prune",
]

_MIRROR_VERSION = _VERSION_TAGS[model_catalog.PROVIDER_OPENROUTER]

# ponytail: chars/4 instead of a real tokenizer. No tokenizer is available for
# an arbitrary OpenRouter model anyway (345 models, many vocabularies), and the
# CONTEXT_LENGTH retry in the driver is the backstop when this under-counts.
# Upgrade path if sessions start mis-trimming: per-vendor tiktoken/HF tokenizer.
_CHARS_PER_TOKEN = 4

_TRIM_MARKER = "[Helios trimmed {count} earlier message(s) to fit the context window.]"
_TRIM_MARKER_PREFIX = "[Helios trimmed "


def _history_dir() -> Path:
    return state_dir() / "openrouter-sessions"


class HistoryLog:
    """Append-only OpenAI message array, persisted atomically."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.path = _history_dir() / f"{session_id}.json"

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        messages = data.get("messages") if isinstance(data, dict) else None
        return messages if isinstance(messages, list) else []

    def save(self, messages: list[dict]) -> None:
        try:
            self.save_strict(messages)
        except OSError as e:
            _log.warning("could not save OpenRouter history %s: %s", self.path, e)

    def save_strict(self, messages: list[dict]) -> None:
        """Atomically persist history, propagating readiness failures.

        Routine post-turn snapshots remain best-effort through ``save``. The
        provider admission gate uses this strict form so no HTTP worker can be
        released until the exact replay array is durably recoverable.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"session_id": self.session_id, "messages": messages}),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def append(self, message: dict) -> None:
        messages = self.load()
        messages.append(message)
        self.save(messages)


def build_assistant_message(
    text: str,
    tool_calls,
    finish_reason: str,
    reasoning_details=(),
) -> tuple[dict | None, bool]:
    """Assemble one assistant message and say whether its tools may run.

    Returns ``(message | None, execute_calls)``.

    The invariant: a message carrying ``tool_calls`` is only ever produced when
    its matching ``role:"tool"`` replies will follow it. OpenAI-format
    endpoints reject a request whose tool calls are unanswered, and this array
    is persisted and reloaded verbatim on resume — so one orphaned message
    breaks every later turn in the session, permanently, across restart.

    Two rules follow. A turn cut off by the output limit has truncated
    arguments, so its calls are dropped from the message rather than emitted
    and left unanswered. And the test is the *presence* of tool calls rather
    than ``finish_reason == "tool_calls"``: providers do report a plain "stop"
    alongside real tool calls, and trusting that label was what orphaned the
    message.
    """
    calls = list(tool_calls or ())
    execute_calls = bool(calls) and finish_reason != "length"
    message: dict = {"role": "assistant"}
    if text:
        message["content"] = text
    details = [d for d in (reasoning_details or ()) if isinstance(d, dict) and d]
    if details:
        # Replayed verbatim and only when the model produced some. OpenRouter
        # documents that a continuation across tool results has to carry the
        # model's own reasoning blocks back — "the model will continue building
        # that existing response" — and that the sequence must match what it
        # generated. Dropping them made every multi-round turn on a reasoning
        # model resume from a thread the model could no longer see.
        message["reasoning_details"] = details
    if execute_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in calls
        ]
    if "content" not in message and "tool_calls" not in message:
        return None, execute_calls
    return message, execute_calls


def prune(*, max_age_days: float = 30.0, keep: int = 200) -> int:
    """Delete old session histories. Returns how many files were removed.

    These files are the least visible thing Helios writes and the most
    sensitive: the authoritative message array holds the conversation verbatim
    *plus every file body the Read tool returned*, and nothing else in the
    system removes them — the four-day session archiver moves transcripts out
    of the sidebar but never touches this directory, so it grew for the life of
    the install.

    Age first, then a newest-N cap so a burst of sessions in one week cannot
    accumulate without bound either. Best-effort: a file that cannot be removed
    is logged and skipped, never raised, because pruning runs on a session-start
    path and must not be able to prevent a session from starting.
    """
    directory = _history_dir()
    try:
        files = [p for p in directory.glob("*.json") if p.is_file()]
    except OSError:
        return 0

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    cutoff = time.time() - max_age_days * 86400.0
    doomed = {p for p in files if mtime(p) < cutoff}
    survivors = sorted((p for p in files if p not in doomed), key=mtime, reverse=True)
    doomed.update(survivors[keep:])

    removed = 0
    for path in doomed:
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            _log.warning("could not prune %s: %s", path, e)
            continue
        try:
            ContextArchive(path.stem).delete()
        except OSError as e:
            _log.warning("could not prune context archive for %s: %s", path.stem, e)
    if removed:
        _log.info("pruned %d OpenRouter session histor%s",
                  removed, "y" if removed == 1 else "ies")
    return removed


def estimate_tokens(messages: list[dict]) -> int:
    """Rough prompt size for ``messages`` (see ``_CHARS_PER_TOKEN``)."""
    total = 0
    for message in messages:
        try:
            total += len(json.dumps(message, ensure_ascii=False))
        except (TypeError, ValueError):
            total += len(str(message))
    return total // _CHARS_PER_TOKEN


def _exchange_starts(messages: list[dict], head: int) -> list[int]:
    """Indices where a user-initiated exchange begins, after the head prefix.

    An exchange is a ``user`` message plus every assistant/tool message that
    follows it. Dropping whole exchanges is what keeps the array wire-valid:
    an ``assistant`` message carrying ``tool_calls`` must always be followed by
    its matching ``role:"tool"`` replies, and OpenAI-format endpoints reject the
    request outright if it isn't.
    """
    return [
        index
        for index in range(head, len(messages))
        if isinstance(messages[index], dict) and messages[index].get("role") == "user"
    ]


def compact(messages: list[dict], *, max_tokens: int, archive: ContextArchive | None = None) -> tuple[list[dict], int]:
    """Archive oldest whole exchanges and retain bounded, quoted user context.

    Returns ``(messages, dropped)``. The leading system prefix is preserved
    verbatim and the newest exchange is never dropped — a single turn that
    alone exceeds the window is the model's problem to report, not something
    trimming can fix. ``dropped`` is 0 when nothing changed, in which case the
    original list object is returned unchanged.
    """
    if max_tokens <= 0 or estimate_tokens(messages) <= max_tokens:
        return messages, 0

    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    # A marker left by an earlier compaction is part of the head prefix; it is
    # rewritten rather than accumulated.
    prefix = list(messages[:head])
    previously_dropped = 0
    has_marker = bool(prefix and str(prefix[-1].get("content", "")).startswith(_TRIM_MARKER_PREFIX))
    if has_marker:
        previously_dropped = _marker_count(prefix.pop())
    previous_capsule = None
    if has_marker and head < len(messages) and str(messages[head].get("content", "")).startswith(CAPSULE_PREFIX):
        previous_capsule = messages[head]
        head += 1

    starts = _exchange_starts(messages, head)
    if len(starts) <= 1:
        return messages, 0

    # Load/validate source history once, not once per candidate cut. Large
    # tool-heavy histories otherwise repeatedly decode the entire archive.
    archived = archive.snapshot() if archive is not None else None

    # Keep the newest exchange no matter what; drop from the oldest inward.
    for cut in starts[1:]:
        dropped = cut - head
        removed = messages[head:cut]
        if archive is not None:
            records, checkpoint = archive.preview(removed, sequence=previously_dropped, snapshot=archived)
        else:
            records, checkpoint = source_records(removed, "unarchived"), ""
            if previous_capsule is not None:
                try:
                    prior = json.loads(previous_capsule["content"][len(CAPSULE_PREFIX):])
                    records = prior.get("user_excerpts", []) + records
                except (KeyError, TypeError, ValueError):
                    pass
        candidate = prefix + [_trim_marker(dropped + previously_dropped)]
        tail = messages[cut:]
        available = max(0, (max_tokens - estimate_tokens(candidate + tail)) * _CHARS_PER_TOKEN - 16)
        retained = capsule(records, checkpoint, max_chars=available)
        if retained is not None:
            candidate.append(retained)
        candidate += tail
        if estimate_tokens(candidate) <= max_tokens or cut == starts[-1]:
            if archive is not None:
                # Never shrink the authoritative replay array until every
                # removed source is recoverable. A failed write leaves it intact.
                archive.append(removed, sequence=previously_dropped)
            return candidate, dropped
    return messages, 0


def _trim_marker(count: int) -> dict:
    return {"role": "system", "content": _TRIM_MARKER.format(count=count)}


def _marker_count(marker: dict) -> int:
    digits = "".join(
        ch for ch in str(marker.get("content", "")) if ch.isdigit()
    )
    try:
        return int(digits)
    except ValueError:
        return 0


def messages_from_mirror(transcript_path: Path, session_id: str) -> list[dict]:
    """Best-effort OpenAI message array from a Claude-format mirror transcript.

    Used only when the authoritative history file is gone. Tool results that
    were elided for display arrive truncated — the session resumes with partial
    memory rather than failing.
    """
    messages: list[dict] = []
    try:
        with Path(transcript_path).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("sessionId") != session_id:
                    continue
                version = record.get("version")
                if version != _MIRROR_VERSION:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                converted = _record_to_message(message)
                if converted is not None:
                    if isinstance(converted, list):
                        messages.extend(converted)
                    else:
                        messages.append(converted)
    except OSError:
        return []
    return messages


def _record_to_message(message: dict) -> dict | list[dict] | None:
    role = message.get("role")
    content = message.get("content")
    if role == "user":
        if isinstance(content, list):
            text = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        elif isinstance(content, str):
            text = content
        else:
            return None
        return {"role": "user", "content": text} if text else None
    if role == "assistant":
        return _assistant_to_message(content)
    return None


def _assistant_to_message(content) -> list[dict] | None:
    if not isinstance(content, list):
        return None
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
        elif kind == "tool_use":
            tool_calls.append({
                "id": part.get("id", ""),
                "type": "function",
                "function": {
                    "name": part.get("name", ""),
                    "arguments": json.dumps(part.get("input", {})),
                },
            })
        elif kind == "tool_result":
            tool_results.append({
                "role": "tool",
                "tool_call_id": part.get("tool_use_id", ""),
                "content": part.get("content", ""),
            })
    out: list[dict] = []
    assistant: dict = {"role": "assistant"}
    if text_parts:
        assistant["content"] = "\n".join(text_parts)
    if tool_calls:
        assistant["tool_calls"] = tool_calls
    if text_parts or tool_calls:
        out.append(assistant)
    out.extend(tool_results)
    return out or None
