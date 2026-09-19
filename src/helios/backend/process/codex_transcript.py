"""Mirror Codex/GPT turns into a Claude-format project JSONL.

The `claude` CLI writes its own `~/.claude/projects/<cwd>/<id>.jsonl` transcript,
which is how Helios's sidebar, transcript view, title generation and resume all
work for free. Codex writes nothing there — its thread lives in
`~/.codex/sessions` in a different shape — so a GPT chat used to be live-only and
vanished on reap/restart.

This writer closes that gap: as a Codex driver runs, we append Claude-compatible
records (one user record per prompt, one assistant record per completed turn) to
a transcript named after the Codex thread id. The result is a first-class
session: it shows in the sidebar (with a GPT chip via [session_providers]),
renders on switch-back, gets a generated title, and resumes — MainWindow routes
the resume back through the Codex driver because the provider index says so.

Lifecycle wrinkle: on a *new* chat the thread id isn't known until Codex emits
`thread.started`, which lands after the first prompt was already sent. So the
first user prompt is buffered and flushed once `bind_thread` learns the id. On a
*resumed* chat the id is known up front, so prompts are written immediately.

Best-effort throughout: a transcript-write failure is logged and swallowed so it
can never take down a live turn.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from helios.backend import model_catalog, projects, session_providers
from helios.backend.transcript import CONTENT_SCHEMA_KEY, Turn
from helios.log import get_logger

_log = get_logger("codex-transcript")

# Maps a provider to the transcript version marker it writes. Adding a new
# mirror provider here also requires a matching branch in
# session_providers._provider_from_transcript.
_VERSION_TAGS: dict[str, str] = {
    model_catalog.PROVIDER_OPENAI: "helios-codex",
    model_catalog.PROVIDER_OPENROUTER: "helios-openrouter",
}

# Ordered-content schema marker value (H0.5). Its PRESENCE (the key is owned by
# transcript.py) means the assistant content is an explicit ordered span
# sequence (text / commentary / reasoning_summary / thinking) reloaded verbatim
# — the pre-H0.5 "collapse extra text blocks into thinking" migration applies
# ONLY to older records that LACK the key. Bump the value if the shape changes.
CONTENT_SCHEMA = "ordered-v1"

# Display-only size caps — the Codex thread's authoritative history lives in
# ~/.codex/sessions and is used for `codex exec resume`; this mirror is shown
# only in the Helios sidebar/transcript view.  Truncating here never changes
# what the model sees on resume.
_MAX_TOOL_RESULT_CHARS = 8_000   # store at most this many chars per tool_result
_MAX_TOOL_USE_INPUT_CHARS = 4_000  # total serialised chars per tool_use input

# Head/tail split for tool_result elision (must sum to less than _MAX_TOOL_RESULT_CHARS)
_TOOL_RESULT_HEAD = 5_000
_TOOL_RESULT_TAIL = 2_000

_ELISION_TEMPLATE = (
    "\n\n… [Helios truncated {n} chars for display;"
    " full output is in the model's own session] …\n\n"
)


def _elide_tool_result(content: str) -> str:
    """Return *content* unchanged if it fits within _MAX_TOOL_RESULT_CHARS.

    Otherwise return a head+elision+tail string that is within the cap.
    The elision marker names the number of dropped characters so the viewer
    can understand what happened.
    """
    if len(content) <= _MAX_TOOL_RESULT_CHARS:
        return content
    dropped = len(content) - _TOOL_RESULT_HEAD - _TOOL_RESULT_TAIL
    marker = _ELISION_TEMPLATE.format(n=dropped)
    return content[:_TOOL_RESULT_HEAD] + marker + content[-_TOOL_RESULT_TAIL:]


def _elide_tool_use_input(input_dict: dict) -> dict:
    """Return a shallow copy of *input_dict* with long string values truncated.

    If the JSON-serialised length of the whole dict is already within
    _MAX_TOOL_USE_INPUT_CHARS it is returned unchanged (same object).  When it
    is over the cap we make a shallow copy and replace any string value that
    would take it over the limit with a truncated-with-marker version.

    We target individual string values rather than the whole blob so the
    resulting dict stays valid JSON and small non-string keys (booleans,
    numbers, nested dicts) are preserved as-is.
    """
    # Fast path: measure first to avoid the copy in the common case.
    if len(json.dumps(input_dict, ensure_ascii=False)) <= _MAX_TOOL_USE_INPUT_CHARS:
        return input_dict

    result: dict = {}
    for k, v in input_dict.items():
        if isinstance(v, str) and len(v) > _MAX_TOOL_USE_INPUT_CHARS:
            head = _MAX_TOOL_USE_INPUT_CHARS // 2
            tail = _MAX_TOOL_USE_INPUT_CHARS // 4
            dropped = len(v) - head - tail
            marker = _ELISION_TEMPLATE.format(n=dropped)
            result[k] = v[:head] + marker + v[-tail:]
        else:
            result[k] = v
    return result


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _fsync_dir(directory) -> None:
    """Durably record a directory entry. A no-op where it is unsupported."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def clone_transcript_for_fork(
    *,
    cwd: str,
    source_thread_id: str,
    fork_thread_id: str,
    provider: str = model_catalog.PROVIDER_OPENAI,
) -> Path:
    """Atomically clone one display mirror beneath a new native thread id.

    Codex owns the authoritative model history. This copy exists so Helios can
    discover, render, and resume the persisted native fork after restart. UUIDs
    and parent links are regenerated to make the branch an independent local
    transcript; source bytes are never modified.
    """

    source_thread_id = str(source_thread_id or "").strip()
    fork_thread_id = str(fork_thread_id or "").strip()
    if (
        not source_thread_id
        or not fork_thread_id
        or source_thread_id == fork_thread_id
        or Path(source_thread_id).name != source_thread_id
        or Path(fork_thread_id).name != fork_thread_id
    ):
        raise ValueError("invalid Codex fork transcript identity")
    provider = provider if provider in _VERSION_TAGS else model_catalog.PROVIDER_OPENAI
    project_dir = projects.PROJECTS_DIR / projects.encode_project_dirname(cwd)
    source_path = project_dir / f"{source_thread_id}.jsonl"
    target_path = project_dir / f"{fork_thread_id}.jsonl"
    if not source_path.is_file():
        raise FileNotFoundError(f"source transcript is unavailable: {source_thread_id}")
    if target_path.exists():
        raise FileExistsError(f"fork transcript already exists: {fork_thread_id}")

    project_dir.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    parent_uuid: str | None = None
    records_written = 0
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=project_dir,
            prefix=f".{fork_thread_id}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temp_path = Path(output.name)
            os.chmod(temp_path, 0o600)
            with source_path.open("r", encoding="utf-8") as source:
                for line_number, line in enumerate(source, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"source transcript line {line_number} is invalid"
                        ) from exc
                    if not isinstance(record, dict):
                        raise ValueError(
                            f"source transcript line {line_number} is not a record"
                        )
                    record_uuid = str(uuid.uuid4())
                    record["uuid"] = record_uuid
                    record["parentUuid"] = parent_uuid
                    record["sessionId"] = fork_thread_id
                    record["cwd"] = cwd
                    record["version"] = _VERSION_TAGS[provider]
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    parent_uuid = record_uuid
                    records_written += 1
            if not records_written:
                raise ValueError("source transcript has no records to fork")
            boundary_uuid = str(uuid.uuid4())
            boundary = {
                "uuid": boundary_uuid,
                "parentUuid": parent_uuid,
                "timestamp": _now_iso(),
                "sessionId": fork_thread_id,
                "cwd": cwd,
                "version": _VERSION_TAGS[provider],
                CONTENT_SCHEMA_KEY: CONTENT_SCHEMA,
                "userType": "external",
                "type": "system",
                "subtype": "fork_boundary",
                "provider": provider,
                "forkMetadata": {"sourceThreadId": source_thread_id},
            }
            output.write(json.dumps(boundary, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        # Re-check immediately before the atomic publish. A native id collision
        # must never overwrite another local conversation.
        if target_path.exists():
            raise FileExistsError(f"fork transcript already exists: {fork_thread_id}")
        # Hard-link publication is atomic and refuses an existing destination;
        # unlike os.replace(), it cannot overwrite a conversation created in a
        # cross-process race between the check above and this syscall.
        os.link(temp_path, target_path)
        published_temp = temp_path
        temp_path = None
        # The bytes were fsynced before the link, but the new directory ENTRY
        # is only in the page cache: a power loss here loses an accepted
        # fork's mirror despite this returning success.
        _fsync_dir(project_dir)
        try:
            published_temp.unlink()
        except OSError as exc:
            # The target link is already complete and authoritative. A failed
            # best-effort cleanup must not report the accepted native branch as
            # missing merely because its hidden temporary hard link survived.
            _log.warning("could not remove fork transcript temporary link: %s", exc)
        return target_path
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


class CodexTranscriptWriter:
    """Appends Claude-format records for one Codex session's turns."""

    def __init__(self, cwd: str, thread_id: str = "", *, provider: str = model_catalog.PROVIDER_OPENAI) -> None:
        self._cwd = cwd
        self._thread_id = thread_id
        self._provider = (
            provider if provider in _VERSION_TAGS else model_catalog.PROVIDER_OPENAI
        )
        self._parent_uuid = ""
        self._pending_user: list[str] = []  # prompts sent before the id was known

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def _path(self) -> Path | None:
        if not self._thread_id:
            return None
        return (
            projects.PROJECTS_DIR
            / projects.encode_project_dirname(self._cwd)
            / f"{self._thread_id}.jsonl"
        )

    def note_user_text(self, text: str) -> None:
        """Record a user prompt. Buffered until the thread id is known."""
        if not text:
            return
        if not self._thread_id:
            self._pending_user.append(text)
            return
        self._write_user(text)

    def bind_thread(self, thread_id: str) -> None:
        """Learn the Codex thread id, flush any buffered prompt(s), and mark the
        session's provider in the provider index so the sidebar/resume agree."""
        if not thread_id or thread_id == self._thread_id:
            # Already bound (resume) — still ensure the index entry exists.
            if self._thread_id:
                session_providers.set_provider(self._thread_id, self._provider)
            return
        self._thread_id = thread_id
        session_providers.set_provider(thread_id, self._provider)
        pending, self._pending_user = self._pending_user, []
        for text in pending:
            self._write_user(text)

    def append_assistant(
        self,
        turn: Turn,
        *,
        model: str = "",
        result: dict | None = None,
        turn_id: str = "",
    ) -> None:
        """Append the assistant record for a completed turn."""
        if turn is None or not turn.has_content:
            return
        content: list[dict] = []
        activity: list[dict] = []
        # Emit assistant content spans in their original source order so a
        # reload reconstructs the exact ordered sequence (no lane regrouping,
        # no type loss). ``thinking`` keeps Claude's ``thinking`` key; the
        # Helios-authored public lanes (commentary / reasoning_summary) store
        # under ``text`` and round-trip via turn_from_record — raw hidden
        # reasoning is never written here.
        for tu in turn.tool_uses:
            activity.append(
                {
                    "type": "tool_use",
                    "id": tu.id,
                    "name": tu.name,
                    "input": _elide_tool_use_input(tu.input),
                }
            )
        for tr in turn.tool_results:
            activity.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tr.tool_use_id,
                    "content": _elide_tool_result(tr.content),
                    "is_error": tr.is_error,
                }
            )

        activity_at = turn.activity_content_index
        if activity_at is not None:
            activity_at = min(max(activity_at, 0), len(turn.content))
        activity_inserted = False
        for index, span in enumerate(turn.content):
            if activity_at is not None and index == activity_at:
                content.extend(activity)
                activity_inserted = True
            if not span.text:
                continue
            if span.kind == "thinking":
                content.append({"type": "thinking", "thinking": span.text})
            elif span.kind in ("text", "commentary", "reasoning_summary"):
                content.append({"type": span.kind, "text": span.text})
        if not activity_inserted:
            content.extend(activity)
        if not content:
            return
        message = {"role": "assistant", "content": content}
        if model:
            message["model"] = model
        usage = (result or {}).get("usage") or {}
        if usage:
            message["usage"] = usage
        payload: dict[str, object] = {"type": "assistant", "message": message}
        if turn_id:
            # Future safe turn-level Restore/Fork needs a durable join between
            # the display mirror and Codex's native history boundary. Older
            # records legitimately lack it and remain ineligible rather than
            # falling back to deprecated thread/rollback.
            payload["providerTurnId"] = turn_id
        self._append_record(payload)

    def append_compaction(
        self,
        *,
        trigger: str,
        pre_tokens: int = 0,
        post_tokens: int = 0,
        turn_id: str = "",
        item_id: str = "",
    ) -> None:
        """Persist a visible provider-owned context boundary.

        The authoritative model history remains in Codex.  This record is the
        durable local explanation that older turns beyond this point may have
        been replaced by Codex's summary; it deliberately stores no summary
        text or hidden reasoning.
        """

        metadata: dict[str, object] = {
            "trigger": "manual" if trigger == "manual" else "auto",
        }
        if pre_tokens > 0:
            metadata["preTokens"] = int(pre_tokens)
        if post_tokens > 0:
            metadata["postTokens"] = int(post_tokens)
        record: dict[str, object] = {
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": metadata,
            "provider": self._provider,
        }
        if turn_id:
            record["providerTurnId"] = turn_id
        if item_id:
            record["providerItemId"] = item_id
        self._append_record(record)

    # ── internals ────────────────────────────────────────────────────────

    def _write_user(self, text: str) -> None:
        self._append(
            "user", {"role": "user", "content": [{"type": "text", "text": text}]}
        )

    def _append(self, rec_type: str, message: dict) -> None:
        self._append_record({"type": rec_type, "message": message})

    def _append_record(self, payload: dict[str, object]) -> None:
        path = self._path()
        if path is None:
            return
        rec_uuid = str(uuid.uuid4())
        record = {
            "uuid": rec_uuid,
            "parentUuid": self._parent_uuid or None,
            "timestamp": _now_iso(),
            "sessionId": self._thread_id,
            "cwd": self._cwd,
            "version": _VERSION_TAGS[self._provider],
            CONTENT_SCHEMA_KEY: CONTENT_SCHEMA,
            "userType": "external",
        }
        record.update(payload)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._parent_uuid = rec_uuid
        except OSError as e:
            _log.warning("codex transcript append failed (%s): %s", path, e)
