"""JSONL transcript parser.

Each line in a session .jsonl is one of:
  * `type=user`            -- user turn (role:user, content: text|content blocks)
  * `type=assistant`       -- model turn (role:assistant, content: blocks)
  * `type=queue-operation` -- internal, ignored
  * `type=system`          -- harness/system message (occasional)
  * `type=summary`         -- compaction summary
  * sidechain markers (`isSidechain: true`) -- background-agent traffic

Content blocks we render:
  * text                  -- markdown text
  * tool_use              -- an outbound tool call (name + input)
  * tool_result           -- the result of a tool call
  * thinking              -- extended thinking
  * image                 -- (rare in transcripts; show a placeholder)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, NamedTuple

from helios.backend.session_goals import strip_goal_envelope
from helios.backend.work_context import strip_work_envelope


# Sentinel tool_result messages produced by Helios's AskUserQuestion workaround
# (see backend.process.cli_driver.respond_to_question). Answering a question is
# delivered as a tool DENY + a follow-up user message, so the CLI records these
# as `is_error` tool_results — but they are NOT failures. The transcript view
# matches on them to render a neutral info row instead of a scary "tool error".
QUESTION_ANSWERED_MESSAGE = "User answered in the follow-up message."
QUESTION_DISMISSED_MESSAGE = "User dismissed the question without answering."


@dataclass(slots=True)
class ToolUse:
    name: str
    input: dict
    id: str = ""


@dataclass(slots=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


def activity_items(turn: "Turn") -> list[tuple[str, object, str]]:
    """Ordered (kind, obj, tool_name) triples for one turn's tool activity.

    Each tool_use is immediately followed by the result carrying its
    tool_use_id, so a transcript row can say WHICH tool produced the output.
    Results with no id, or an id matching no invocation, keep their transcript
    order and are appended after the paired items with an empty name.
    GTK-free so the pairing is testable on the slim CI image.

    ponytail: the first invocation claims every result sharing its id.
    ``session_insights._ambiguous_tool_ids`` shows ids do get reused; per-call
    disambiguation only if a real transcript shows it mattering.
    """
    by_id: dict[str, list[int]] = {}
    for idx, tr in enumerate(turn.tool_results):
        if tr.tool_use_id:
            by_id.setdefault(tr.tool_use_id, []).append(idx)
    items: list[tuple[str, object, str]] = []
    paired: set[int] = set()
    for tu in turn.tool_uses:
        items.append(("tool_use", tu, tu.name))
        for idx in by_id.get(tu.id, ()) if tu.id else ():
            if idx in paired:
                continue
            paired.add(idx)
            items.append(("tool_result", turn.tool_results[idx], tu.name))
    for idx, tr in enumerate(turn.tool_results):
        if idx not in paired:
            items.append(("tool_result", tr, ""))
    return items


# Provider-neutral assistant content taxonomy (H0.5). A turn's assistant
# content is ONE ordered sequence of spans, each tagged with a kind:
#   text              -- the final answer (prominent)
#   commentary        -- OpenAI agentMessage phase=commentary; a public work
#                        update, rendered as visible lightweight progress
#   reasoning_summary -- the PUBLIC reasoning summary (never raw hidden
#                        chain-of-thought); separately labeled and collapsible
#   thinking          -- legacy provider thinking (Claude extended thinking);
#                        the catch-all collapsed bucket
# Cross-lane ORDER is authoritative: it is the source order the model produced
# content in, preserved through streaming -> finalize -> persist -> reload so
# every stage renders and plans identically.
CONTENT_KINDS = ("text", "thinking", "commentary", "reasoning_summary")

# Record-level marker (set by CodexTranscriptWriter) meaning the assistant
# content is an explicit ordered span sequence and must reload verbatim. Its
# ABSENCE on a helios-codex record selects the pre-H0.5 multi-text→thinking
# migration. Owned here (the parse side); the writer imports it.
CONTENT_SCHEMA_KEY = "heliosContentSchema"


@dataclass(slots=True)
class ContentSpan:
    """One ordered span of assistant content. ``kind`` is a CONTENT_KINDS value."""

    kind: str
    text: str


class _SpanView:
    """Live, writable per-kind projection over a Turn's ordered ``content``.

    A backward-compatibility shim: legacy callers keep reading and appending
    ``turn.<kind>_parts`` (e.g. ``turn.thinking_parts.append(...)``) while the
    single source of truth stays the ordered ``Turn.content`` list — no parallel
    lists, no competing truth. Every read reflects the current content.

    Intended (and tested) list-facade operations:
      * read:   iterate, ``len()``, index/slice, ``in``, truthiness, ``==`` a
                list, ``view + list`` / ``list + view`` (yields a plain list).
      * append: ``.append(text)`` / ``.extend(texts)`` insert spans of this
                kind at the END of ``content`` (call order).
      * assign: ``turn.k_parts = [...]`` / ``turn.k_parts += [...]`` route to the
                setter, which replaces this kind's spans IN PLACE (positionally),
                leaving all other spans' positions untouched; self-assignment is
                an exact no-op. See ``Turn._replace_kind``.
    Operations a real ``list`` has but this deliberately does NOT (call
    ``list(view)`` first): in-place ``.sort()``/``.reverse()``/``.insert()``/
    ``.pop()``/``.remove()``, and identity (each access returns a fresh view).
    """

    __slots__ = ("_turn", "_kind")

    def __init__(self, turn: Turn, kind: str) -> None:
        self._turn = turn
        self._kind = kind

    def _items(self) -> list[str]:
        return [s.text for s in self._turn.content if s.kind == self._kind]

    def append(self, text: str) -> None:
        self._turn.content.append(ContentSpan(self._kind, text))

    def extend(self, texts: Any) -> None:
        for text in texts:
            self.append(text)

    def __iter__(self):
        return iter(self._items())

    def __len__(self) -> int:
        return sum(1 for s in self._turn.content if s.kind == self._kind)

    def __getitem__(self, index):
        return self._items()[index]

    def __contains__(self, item: object) -> bool:
        return item in self._items()

    def __bool__(self) -> bool:
        return any(s.kind == self._kind for s in self._turn.content)

    def __add__(self, other: Any) -> list[str]:
        return self._items() + list(other)

    def __radd__(self, other: Any) -> list[str]:
        return list(other) + self._items()

    def __eq__(self, other: object) -> bool:
        try:
            return self._items() == list(other)  # type: ignore[arg-type]
        except TypeError:
            return NotImplemented

    def __repr__(self) -> str:
        return repr(self._items())


class Turn:
    """One conversation turn. Assistant content lives in the ordered
    ``content`` list; ``tool_uses``/``tool_results`` stay separate collections.

    ``activity_content_index`` records the number of content spans that
    preceded the first tool block.  Tool details remain in their existing
    separate collections; the boundary is only the small piece of source-order
    metadata needed to place their single collapsed activity group.

    The ``*_parts`` attributes are live per-kind views over ``content`` (read
    and append), kept for backward compatibility with existing callers and
    hand-built Turns. Legacy per-lane construction kwargs are also accepted and
    folded into ``content`` in a deterministic order.
    """

    __slots__ = (
        "role",
        "content",
        "tool_uses",
        "tool_results",
        "activity_content_index",
        "timestamp",
        "uuid",
        "is_sidechain",
        "is_meta",
    )

    def __init__(
        self,
        role: str,
        content: list[ContentSpan] | None = None,
        tool_uses: list[ToolUse] | None = None,
        tool_results: list[ToolResult] | None = None,
        timestamp: str = "",
        uuid: str = "",
        is_sidechain: bool = False,
        is_meta: bool = False,
        *,
        activity_content_index: int | None = None,
        text_parts: list[str] | None = None,
        thinking_parts: list[str] | None = None,
        commentary_parts: list[str] | None = None,
        reasoning_summary_parts: list[str] | None = None,
    ) -> None:
        self.role = role
        self.content = list(content) if content else []
        self.tool_uses = tool_uses if tool_uses is not None else []
        self.tool_results = tool_results if tool_results is not None else []
        self.activity_content_index = activity_content_index
        self.timestamp = timestamp
        self.uuid = uuid
        self.is_sidechain = is_sidechain
        self.is_meta = is_meta
        # Deterministic fold of any legacy per-lane kwargs: collapsed detail
        # first (reasoning summary, thinking), then visible progress, then the
        # final answer — a stable order for Turns built without span metadata.
        for kind, parts in (
            ("reasoning_summary", reasoning_summary_parts),
            ("thinking", thinking_parts),
            ("commentary", commentary_parts),
            ("text", text_parts),
        ):
            for part in parts or []:
                self.content.append(ContentSpan(kind, part))

    def add(self, kind: str, text: str) -> None:
        """Append one ordered content span — the canonical mutation entry point."""

        self.content.append(ContentSpan(kind, text))

    def _replace_kind(self, kind: str, values: Any) -> None:
        # Positional, order-preserving replacement of one kind's spans:
        #  * self-assignment (values is a live view over this content) is an
        #    exact no-op — every span keeps its position;
        #  * ``turn.k_parts = [a, b]`` overwrites the k-spans in place (first
        #    k-span -> a, second -> b), leaving all OTHER spans where they are;
        #  * surplus new values append at the end; missing ones drop the tail.
        # Snapshot first: ``values`` may read this same content mid-mutation.
        if (
            isinstance(values, _SpanView)
            and values._turn is self
            and values._kind == kind
        ):
            return
        snapped = list(values)
        new_content: list[ContentSpan] = []
        supply = iter(snapped)
        for span in self.content:
            if span.kind != kind:
                new_content.append(span)
                continue
            try:
                span.text = next(supply)
            except StopIteration:
                continue  # fewer new values than existing spans -> drop tail
            new_content.append(span)
        for extra in supply:  # more new values than existing spans -> append
            new_content.append(ContentSpan(kind, extra))
        self.content = new_content

    # -- legacy per-lane views (read + append route through ``content``) --

    @property
    def text_parts(self) -> _SpanView:
        return _SpanView(self, "text")

    @text_parts.setter
    def text_parts(self, values: Any) -> None:
        self._replace_kind("text", values)

    @property
    def thinking_parts(self) -> _SpanView:
        return _SpanView(self, "thinking")

    @thinking_parts.setter
    def thinking_parts(self, values: Any) -> None:
        self._replace_kind("thinking", values)

    @property
    def commentary_parts(self) -> _SpanView:
        return _SpanView(self, "commentary")

    @commentary_parts.setter
    def commentary_parts(self, values: Any) -> None:
        self._replace_kind("commentary", values)

    @property
    def reasoning_summary_parts(self) -> _SpanView:
        return _SpanView(self, "reasoning_summary")

    @reasoning_summary_parts.setter
    def reasoning_summary_parts(self, values: Any) -> None:
        self._replace_kind("reasoning_summary", values)

    @property
    def text(self) -> str:
        return "\n\n".join(
            s.text for s in self.content if s.kind == "text" and s.text.strip()
        )

    @property
    def has_content(self) -> bool:
        return bool(self.content or self.tool_uses or self.tool_results)

    @property
    def dt(self) -> datetime | None:
        if not self.timestamp:
            return None
        try:
            # Python's fromisoformat doesn't grok trailing Z until 3.11
            ts = self.timestamp.replace("Z", "+00:00")
            return datetime.fromisoformat(ts).astimezone()
        except ValueError:
            return None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Turn):
            return NotImplemented
        return (
            self.role == other.role
            and self.content == other.content
            and self.tool_uses == other.tool_uses
            and self.tool_results == other.tool_results
            and self.activity_content_index == other.activity_content_index
            and self.timestamp == other.timestamp
            and self.uuid == other.uuid
            and self.is_sidechain == other.is_sidechain
            and self.is_meta == other.is_meta
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return (
            f"Turn(role={self.role!r}, content={self.content!r}, "
            f"tool_uses={self.tool_uses!r}, tool_results={self.tool_results!r}, "
            f"activity_content_index={self.activity_content_index!r}, "
            f"timestamp={self.timestamp!r}, uuid={self.uuid!r}, "
            f"is_sidechain={self.is_sidechain!r}, is_meta={self.is_meta!r})"
        )


def parse_transcript(path: Path, *, include_sidechain: bool = False) -> list[Turn]:
    """Read the full transcript and return a list of Turns.

    Phase 1 keeps this simple: linear list ordered by file order. We don't yet
    reconstruct the parentUuid tree — most sessions are linear anyway, and the
    out-of-order cases (resume/edit-rewind) are rare enough that a future
    pass can layer it on.
    """
    turns: list[Turn] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                turn = turn_from_record(obj)
                if turn is None:
                    continue
                if turn.is_sidechain and not include_sidechain:
                    continue
                if not turn.has_content:
                    continue
                turns.append(turn)
    except OSError:
        pass
    return turns


def session_has_content(path: Path) -> bool:
    """True if the transcript holds at least one real (non-sidechain, content-
    bearing) turn.

    Errs on the side of "has content" for anything it cannot positively confirm
    as empty: an unreadable file, or a line that will not parse, returns True.
    This matters because callers HIDE or ARCHIVE sessions this reports False
    for — so an "unknown" state must never be treated as empty (a transiently
    locked/half-written real session would otherwise get swept into the
    archive). Only a fully-readable transcript whose every record is a
    sidecar/meta/empty line (e.g. an `ai-title` stub) returns False.

    Short-circuits on the first content-bearing turn, so it stays cheap on
    large transcripts."""
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return True  # can't read → unknown → conservative (keep/don't sweep)
    try:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                return True  # malformed line → can't be sure it's empty
            turn = turn_from_record(obj)
            if turn is None:
                continue  # sidecar/meta record (ai-title, mode, …) → not content
            if turn.is_sidechain:
                continue
            if turn.has_content:
                return True
        return False
    except OSError:
        return True  # read error mid-stream → unknown → conservative
    finally:
        f.close()


def session_is_descendant_only(path: Path) -> bool:
    """Prove that a transcript contains descendant activity and no root turn.

    SessionList is reserved for primary conversations.  Claude sidechain
    records normally share their root transcript, but a provider/version can
    materialize a child-only JSONL beside it.  Such a file must not become a
    primary sidebar row, including during the new-file grace window.

    This is intentionally conservative: unreadable or malformed input returns
    ``False`` so an uncertain real conversation is never hidden.  A mixed file
    containing even one primary content turn is also a primary session.
    """

    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return False
    saw_descendant_content = False
    try:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                return False
            turn = turn_from_record(obj)
            if turn is None or not turn.has_content:
                continue
            if not turn.is_sidechain:
                return False
            saw_descendant_content = True
        return saw_descendant_content
    except OSError:
        return False
    finally:
        f.close()


def _turn_from_line(raw: str, include_sidechain: bool) -> Turn | None:
    """One transcript line -> a renderable Turn, or None if it is filtered out.

    Extracted so `iter_transcript` and `read_transcript_since` provably apply
    the same filter — a drift between them would show different turns
    depending on whether a session was opened or followed.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        # Valid JSON, wrong shape: `null`, `[]`, a bare string. turn_from_record
        # assumes a mapping.
        return None
    try:
        turn = turn_from_record(obj)
    except Exception:
        # Belt to the isinstance braces above, and deliberately kept.
        #
        # Measured: turn_from_record raises AttributeError on a non-dict
        # top-level value (`null`, `[]`, a bare string, a number) — that is what
        # the isinstance guard is for, and disabling only this except leaves the
        # tests green. Eight adversarial in-dict shapes (message=null,
        # content=99, content=[1,2], a tool_result whose content is a dict, a
        # tool_use whose input is a string, timestamp=12345, ...) were all
        # handled without raising, so no trigger for this arm is known today.
        #
        # It stays because this is the trust boundary for a file another process
        # writes, the follow path reaches it from a GLib callback where an
        # escape breaks every later update, and the code this MR replaced had a
        # blanket `except Exception` around the whole iteration. Dropping it
        # would be a quiet reduction in robustness rather than a simplification.
        return None
    if turn is None:
        return None
    if turn.is_sidechain and not include_sidechain:
        return None
    if not turn.has_content:
        return None
    return turn


def iter_transcript(path: Path, *, include_sidechain: bool = False) -> Iterator[Turn]:
    """Streaming version — yields Turn objects one at a time."""
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        for raw in f:
            turn = _turn_from_line(raw, include_sidechain)
            if turn is not None:
                yield turn
    finally:
        f.close()


class TranscriptTail(NamedTuple):
    """Turns parsed from one byte range of a transcript, plus where to resume."""

    turns: list[Turn]
    offset: int  # byte position just past the last COMPLETE line consumed
    restarted: bool  # file shrank/was replaced; `turns` is a full re-read


def read_transcript_since(
    path: Path, offset: int = 0, *, include_sidechain: bool = False
) -> TranscriptTail:
    """Parse only what was appended after ``offset``.

    Binary mode on purpose: text mode's `seek()` takes opaque cookies, not byte
    counts, and universal-newline translation makes `len(line)` a lie. Reading
    bytes keeps the returned offset exactly comparable to `st_size`.

    ``offset`` advances only past newline-terminated lines. A record still
    being written has no trailing newline yet, so it is left unconsumed and
    re-read whole on the next call rather than being split into two halves that
    both fail to parse — and silently dropped, since `_turn_from_line` swallows
    JSONDecodeError.
    """
    turns: list[Turn] = []
    pos, restarted = (0 if offset < 0 else offset), False
    try:
        with path.open("rb") as f:
            # Size comes from the OPEN descriptor, not a prior path.stat().
            # Statting first left a window where the file could be truncated or
            # replaced in between, so `restarted` described the old file while
            # the read targeted the new one.
            #
            # ponytail: this NARROWS the race, it does not close it. The inode
            # can still be truncated after this fstat, or the path atomically
            # replaced after the open, in which case we read the old unlinked
            # inode and return an offset that no longer means anything. Closing
            # that properly needs st_dev/st_ino identity tracking across calls;
            # it is not built because no in-tree writer rewrites a transcript in
            # place (the archiver and project mover both rename, which raises
            # OSError here and correctly holds the offset). Add the ino check if
            # one ever appears — do not paper over it with a size heuristic.
            size = os.fstat(f.fileno()).st_size
            # ponytail: a shrink is the rotation signal. A rewriter that
            # replaces a file in place AND grows it would fool this; add an
            # st_ino check if one appears.
            restarted = offset > size
            pos = 0 if restarted or offset < 0 else offset
            f.seek(pos)
            for raw in f:
                if not raw.endswith(b"\n"):
                    # Trailing bytes with no newline. PARSING decides, not the
                    # newline: a record that decodes is complete — JSON is not
                    # valid until its closing brace, so a half-written line
                    # cannot decode — and one that does not is a write in
                    # flight, left unconsumed to be re-read whole next time.
                    # Either way the offset stays honest, so no duplicate can
                    # appear; and consuming a complete record matters because
                    # JSONL does not require a trailing newline, so on the
                    # follow path a producer that closes without one would
                    # otherwise strand that turn until some unrelated write.
                    turn = _turn_from_line(
                        raw.decode("utf-8", "replace"), include_sidechain
                    )
                    if turn is not None:
                        turns.append(turn)
                        pos += len(raw)
                    break
                pos += len(raw)
                turn = _turn_from_line(
                    raw.decode("utf-8", "replace"), include_sidechain
                )
                if turn is not None:
                    turns.append(turn)
    except OSError:
        return TranscriptTail([], offset, False)
    return TranscriptTail(turns, pos, restarted)


def strip_injected_context(text: str) -> str:
    """Strip nested Helios wrappers regardless of their composition order."""

    cleaned = text
    # Current wrappers can be nested in either order by prompt providers. A
    # tiny fixed-point loop also keeps old transcripts clean if that order
    # changes during migration.
    for _ in range(4):
        previous = cleaned
        cleaned = strip_work_envelope(strip_goal_envelope(cleaned))
        if cleaned == previous:
            break
    return cleaned


def _int_or_zero(*candidates: Any) -> int:
    """First candidate that is a usable integer, else 0.

    `compactMetadata` is camelCase in the transcript file and snake_case on
    the SDK stream, so both spellings have to be accepted.
    """

    for value in candidates:
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def turn_from_record(obj: dict[str, Any]) -> Turn | None:
    rec_type = obj.get("type")
    if rec_type not in ("user", "assistant", "system"):
        return None
    msg = obj.get("message") or {}
    if not msg and rec_type != "system":
        return None

    if rec_type == "system" and obj.get("subtype") == "fork_boundary":
        boundary = Turn(
            role="system",
            timestamp=obj.get("timestamp") or "",
            uuid=obj.get("uuid") or "",
            is_meta=True,
        )
        boundary.add(
            "text",
            "Conversation forked here. This branch has an independent Work; "
            "the source conversation was preserved.",
        )
        return boundary

    if rec_type == "system" and obj.get("subtype") == "compact_boundary":
        # A compaction boundary is a `system` record whose text lives at the
        # TOP level, not under `message` — so it produced a content-less Turn
        # and `iter_transcript`'s `has_content` guard dropped it. That is why
        # the boundary never appeared: not `is_meta` (which gates only derived
        # text — insights, handoff tails, the archiver — never rendering), and
        # not the fold on the next line, which matches what the CLI itself
        # does (`isMeta || isVisibleInTranscriptOnly || isCompactSummary`).
        #
        # Scoped strictly to this subtype on purpose. Rendering `system`
        # records generally would start surfacing local-command and hook XML
        # that has never been shown, which is an unbounded blast radius.
        # Both spellings of the CONTAINER, not just of the keys inside it.
        # cli_driver.py:1630 reads camelCase for the live stream and that is
        # what the binary emits today, but nothing here has been observed on
        # a real record — so a snake_case container must not silently cost the
        # token figures.
        meta = obj.get("compactMetadata") or obj.get("compact_metadata") or {}
        pre = _int_or_zero(meta.get("preTokens"), meta.get("pre_tokens"))
        post = _int_or_zero(meta.get("postTokens"), meta.get("post_tokens"))
        # Both figures or neither. `postTokens` is patched on after the marker
        # is built and the CLI itself defends against its absence, so gating
        # on `pre` alone would render "148,000 → 0 tokens" — which reads as
        # "the context was emptied". Worst case here is no numbers, never
        # wrong ones.
        detail = f" — {pre:,} → {post:,} tokens" if pre and post else ""
        boundary = Turn(
            role="system",
            timestamp=obj.get("timestamp") or "",
            uuid=obj.get("uuid") or "",
            is_meta=True,
        )
        # Deliberately not "everything above this line": a compaction may
        # preserve a trailing segment, so that phrasing would be false.
        boundary.add(
            "text",
            f"Context compacted here{detail}. Earlier turns were replaced "
            "by a summary.",
        )
        return boundary

    turn = Turn(
        role=msg.get("role") or rec_type,
        timestamp=obj.get("timestamp") or "",
        uuid=obj.get("uuid") or "",
        is_sidechain=bool(obj.get("isSidechain")),
        is_meta=bool(obj.get("isMeta") or obj.get("isCompactSummary")),
    )

    content = msg.get("content")
    if isinstance(content, str):
        if turn.role == "user":
            content = strip_injected_context(content)
        if content.strip():
            turn.text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                t = block.get("text") or ""
                if turn.role == "user":
                    t = strip_injected_context(t)
                if t.strip():
                    turn.text_parts.append(t)
            elif btype == "thinking":
                t = block.get("thinking") or block.get("text") or ""
                if t.strip():
                    turn.thinking_parts.append(t)
            elif btype == "commentary":
                # Helios-authored (helios-codex) public work update.
                t = block.get("text") or ""
                if t.strip():
                    turn.commentary_parts.append(t)
            elif btype == "reasoning_summary":
                # Helios-authored (helios-codex) PUBLIC reasoning summary — never
                # raw hidden reasoning (that is dropped at the event boundary).
                t = block.get("text") or ""
                if t.strip():
                    turn.reasoning_summary_parts.append(t)
            elif btype == "tool_use":
                if turn.activity_content_index is None:
                    turn.activity_content_index = len(turn.content)
                turn.tool_uses.append(
                    ToolUse(
                        name=block.get("name") or "(tool)",
                        input=block.get("input") or {},
                        id=block.get("id") or "",
                    )
                )
            elif btype == "tool_result":
                if turn.activity_content_index is None:
                    turn.activity_content_index = len(turn.content)
                tr_content = block.get("content")
                if isinstance(tr_content, list):
                    parts = []
                    for sub in tr_content:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text") or "")
                    text = "\n".join(p for p in parts if p)
                else:
                    text = tr_content if isinstance(tr_content, str) else ""
                turn.tool_results.append(
                    ToolResult(
                        tool_use_id=block.get("tool_use_id") or "",
                        content=text or "",
                        is_error=bool(block.get("is_error")),
                    )
                )
    if (
        turn.role == "user"
        and turn.tool_results
        and not turn.content
        # Conservative on purpose. No `type=user` record in 238 real Claude
        # transcripts carries a tool_use (32,333 tool_result records checked,
        # zero mixed) and Claude's schema makes tool_use assistant-only — but
        # Codex and OpenRouter write their own transcripts through this same
        # parser, and a retag is a CLASSIFICATION. A mixed record is not
        # "transport for a tool's output", so it keeps its speaker.
        and not turn.tool_uses
    ):
        # A `type=user` record carrying ONLY tool_results is the CLI's transport
        # for a tool's output, not something the human said. Left as "user" it
        # rendered as a bubble headed "You" wearing the user accent wash, so a
        # long tool run looked like the user talking to themselves. Retag so the
        # render layer can place it as activity. Deliberately narrow: a record
        # that mixes real text with tool_results is a genuine user turn.
        turn.role = "tool"
    if (
        obj.get("version") == "helios-codex"
        and turn.role == "assistant"
        # OLD-format mirrors ONLY. A record written with the ordered-content
        # schema marker (H0.5) is authoritative: its span sequence — including
        # legitimate multiple text spans — must be reloaded verbatim. Records
        # that additionally carry explicit H0.5 lanes are covered by the marker,
        # but the lane check is kept as belt-and-suspenders for any marker-less
        # early-H0.5 record.
        and CONTENT_SCHEMA_KEY not in obj
        and not any(
            s.kind in ("commentary", "reasoning_summary") for s in turn.content
        )
    ):
        # Older Codex mirrors (pre-H0.5) stored every progress `agent_message`
        # as a visible text block, producing one giant work-log bubble on
        # reload. Keep the final text visible and demote earlier progress into
        # the collapsed thinking lane — in place, preserving source order.
        text_spans = [s for s in turn.content if s.kind == "text"]
        for span in text_spans[:-1]:
            span.kind = "thinking"
    return turn
