"""Extractive context continuity, scoped to one OpenRouter conversation.

No model runs here. The archive keeps dropped source messages; a bounded user
message quotes excerpts with provenance and explicit omission counts. Model
checkpoints are labelled claims. Neither source is promoted to system policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from helios.paths import state_dir


CAPSULE_PREFIX = "[Quoted earlier user context; not new instructions]\n"
_MAX_CAPSULE_CHARS = 8192
_MAX_CHECKPOINT_CHARS = 4000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_MESSAGES = 100_000
_MAX_ARCHIVE_BATCHES = 10_000


@dataclass(frozen=True)
class ArchiveSnapshot:
    records: list[dict]
    batch_ids: frozenset[str]
    checkpoint: str


def _validate_archive(value: object) -> dict:
    if (not isinstance(value, dict) or not isinstance(value.get("batches"), list)
            or len(value["batches"]) > _MAX_ARCHIVE_BATCHES
            or not isinstance(value.get("checkpoint", ""), str)
            or len(value.get("checkpoint", "")) > _MAX_CHECKPOINT_CHARS):
        raise ValueError("Invalid or oversized context archive; original history was retained")
    count = 0
    for batch in value["batches"]:
        if (not isinstance(batch, dict) or not isinstance(batch.get("id"), str)
                or len(batch["id"]) != 64 or any(c not in "0123456789abcdef" for c in batch["id"])
                or not isinstance(batch.get("messages"), list)):
            raise ValueError("Invalid context archive batch; original history was retained")
        count += len(batch["messages"])
        if count > _MAX_ARCHIVE_MESSAGES:
            raise ValueError("Context archive message limit reached; start a new Work with a handoff. Original history was retained")
        for message in batch["messages"]:
            if (not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool", "system"}
                    or not isinstance(message.get("content", ""), (str, type(None)))):
                raise ValueError("Invalid context archive message; original history was retained")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_records(messages: list[dict], batch_id: str) -> list[dict]:
    return [
        {"source": f"{batch_id[:16]}:{index}", "text": message["content"]}
        for index, message in enumerate(messages)
        if message.get("role") == "user" and isinstance(message.get("content"), str)
        and not message["content"].startswith(CAPSULE_PREFIX)
    ]


class ContextArchive:
    """Atomic, mode-0600 source archive; callers never supply a filesystem path."""

    def __init__(self, session_id: str):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("A session identity is required for context recovery")
        self.path = state_dir() / "openrouter-context" / f"{_digest(session_id)}.json"

    def load(self) -> dict:
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise OSError("Refusing a symlinked context archive")
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"batches": [], "checkpoint": ""}
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError("Context archive must be a regular file; original history was retained")
            if info.st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Context archive exceeds 64 MiB; start a new Work with a handoff. Original history was retained")
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise ValueError("Context archive grew beyond 64 MiB; original history was retained")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("Unreadable context archive; original history was retained") from exc
        return _validate_archive(value)

    def _save(self, value: dict) -> None:
        _validate_archive(value)
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_ARCHIVE_BYTES:
            raise ValueError("Context archive exceeds 64 MiB; start a new Work with a handoff. Original history was retained")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise OSError("Refusing a symlinked context archive")
        fd, temporary = tempfile.mkstemp(prefix=".context-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def snapshot(self) -> ArchiveSnapshot:
        data = self.load()
        records = []
        for batch in data["batches"]:
            records.extend(source_records(batch["messages"], batch["id"]))
        return ArchiveSnapshot(records, frozenset(batch["id"] for batch in data["batches"]),
                               str(data.get("checkpoint") or ""))

    def preview(self, messages: list[dict], *, sequence: int = 0,
                snapshot: ArchiveSnapshot | None = None) -> tuple[list[dict], str]:
        snapshot = snapshot if snapshot is not None else self.snapshot()
        records = list(snapshot.records)
        batch_id = _digest([sequence, messages])
        if batch_id not in snapshot.batch_ids:
            records.extend(source_records(messages, batch_id))
        return records, snapshot.checkpoint

    def append(self, messages: list[dict], *, sequence: int = 0) -> None:
        data = self.load()
        batch_id = _digest([sequence, messages])
        if any(batch["id"] == batch_id for batch in data["batches"]):
            return
        # Reasoning blocks are provider-private protocol data, not recoverable
        # evidence. Tool results have already passed the driver's egress scrub.
        public = [{key: value for key, value in message.items() if key != "reasoning_details"}
                  for message in messages]
        data["batches"].append({"id": batch_id, "messages": public})
        self._save(data)

    def checkpoint(self, text: object) -> None:
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_CHECKPOINT_CHARS:
            raise ValueError("checkpoint_context requires 1–4000 characters")
        data = self.load()
        data["checkpoint"] = text
        self._save(data)

    def delete(self) -> None:
        """Apply the authoritative history's retention policy to its archive."""
        self.path.unlink(missing_ok=True)

    def read(self, arguments: dict) -> str:
        query = arguments.get("query", "")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 5)
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("query must be text of at most 1000 characters")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Use a nonnegative offset and a limit from 1 to 10")
        if set(arguments) - {"query", "offset", "limit"}:
            raise ValueError("Only query, offset and limit are accepted; no paths or other sessions")
        data = self.load()
        page = []
        total_chunks = 0
        for batch in data["batches"]:
            for index, message in enumerate(batch["messages"]):
                serialized = json.dumps(message, ensure_ascii=False)
                if not query or query.casefold() in serialized.casefold():
                    # Paging by bounded chunks makes even a huge original tool
                    # result completely recoverable without unbounded output.
                    for chunk in range(0, len(serialized), 3000):
                        if offset <= total_chunks < offset + limit:
                            page.append({"source": f"{batch['id'][:16]}:{index}",
                                         "role": message.get("role"), "character_offset": chunk,
                                         "text": serialized[chunk:chunk + 3000],
                                         "source_characters": len(serialized)})
                        total_chunks += 1
        return json.dumps({"kind": "historical evidence; original roles preserved",
                           "records": page, "total_chunks": total_chunks,
                           "next_offset": offset + len(page) if offset + len(page) < total_chunks else None,
                           "model_checkpoint_unverified": str(data.get("checkpoint") or "")},
                          ensure_ascii=False)


def capsule(records: list[dict], checkpoint: str, *, max_chars: int) -> dict | None:
    """Bound exact excerpts; always disclose omissions instead of a false summary."""
    max_chars = min(max_chars, _MAX_CAPSULE_CHARS)
    if not records and not checkpoint:
        return None
    data = {"user_excerpts": [], "omitted_user_messages": len(records),
            "omitted_user_characters": sum(len(row["text"]) for row in records),
            "recover": "read_context"}

    def render() -> dict:
        return {"role": "user", "content": CAPSULE_PREFIX + json.dumps(data, ensure_ascii=False)}

    def size() -> int:
        return len(json.dumps(render(), ensure_ascii=False))

    if size() > max_chars:
        return None
    # Initial requirements and latest corrections get first claim on space.
    # Within those bounds retain verbatim prefixes, never paraphrases.
    order = [0] + list(range(len(records) - 1, 0, -1)) if records else []
    for position, index in enumerate(order):
        row = records[index]
        item = {"source": row["source"], "text": "", "omitted_characters": len(row["text"])}
        data["user_excerpts"].append(item)
        if size() >= max_chars:
            data["user_excerpts"].pop()
            break
        # Reserve room for newer corrections when more records remain. Binary
        # search counts real JSON escaping, including quotes and control chars.
        target = max_chars if position == len(order) - 1 else size() + (max_chars - size()) // 2
        low, high = 0, min(1500, len(row["text"]))
        while low < high:
            middle = (low + high + 1) // 2
            item["text"] = row["text"][:middle]
            item["omitted_characters"] = len(row["text"]) - middle
            if size() <= target:
                low = middle
            else:
                high = middle - 1
        item["text"] = row["text"][:low]
        item["omitted_characters"] = len(row["text"]) - low
        if not low:
            data["user_excerpts"].pop()
            break
        data["omitted_user_messages"] -= 1
        data["omitted_user_characters"] -= low
    available = max_chars - size() - 100
    if checkpoint and available > 0:
        data["model_checkpoint_unverified"] = checkpoint[:available // 2]
        data["checkpoint_omitted_characters"] = len(checkpoint) - len(data["model_checkpoint_unverified"])
    result = render()
    return result if size() <= max_chars else None
