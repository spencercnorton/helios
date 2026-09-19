"""Streaming-message aggregation shapes, shared by both CLI drivers.

Moved out of cli_driver so GTK-free code (codex_events, tests on the slim
CI image) can build and inspect them without importing gi.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

from helios.backend.transcript import ToolUse, Turn


@dataclass(slots=True)
class Block:
    """A single content block in a streaming assistant message."""

    # "text" (final answer) | "thinking" (legacy provider thinking) |
    # "commentary" (public work update) | "reasoning_summary" (public
    # reasoning summary — never raw hidden reasoning) | "tool_use"
    type: str
    text: str = ""
    tool_use_name: str = ""
    tool_use_id: str = ""
    tool_use_input_json: str = ""  # accumulating raw JSON


@dataclass(slots=True)
class StreamingAssistant:
    """An in-progress assistant message, assembled from streamed deltas.

    The transcript view holds a reference; whenever this changes we tell the
    view to re-render the corresponding bubble in place.
    """

    blocks: list[Block] = field(default_factory=list)
    ttft_ms: int | None = None
    model: str = ""
    stopped: bool = False

    def to_turn(self) -> Turn:
        # Preserve block (source) order in the turn's ordered content so
        # finalize and reload render/plan identically to the live stream.
        turn = Turn(role="assistant")
        for b in self.blocks:
            if b.type in ("text", "thinking", "commentary", "reasoning_summary"):
                if b.text:
                    turn.add(b.type, b.text)
            elif b.type == "tool_use":
                if turn.activity_content_index is None:
                    turn.activity_content_index = len(turn.content)
                try:
                    inp = json.loads(b.tool_use_input_json) if b.tool_use_input_json else {}
                except json.JSONDecodeError:
                    inp = {"_raw": b.tool_use_input_json}
                turn.tool_uses.append(
                    ToolUse(name=b.tool_use_name, input=inp, id=b.tool_use_id)
                )
        return turn

    def apply_stream_event(self, ev: dict) -> None:
        """Apply one Anthropic stream_event payload (claude wire format).

        Defensive against malformed / out-of-order events (claude CLI schema
        drift, a corrupt line): a block start with no usable index is appended,
        and a delta with a missing/negative index — or one arriving before any
        content_block_start — is dropped rather than raising IndexError out of
        the driver's stdout callback, which would strand the session mid-reply.
        """
        etype = ev.get("type")
        if etype == "message_start":
            msg = ev.get("message") or {}
            self.model = msg.get("model") or self.model
        elif etype == "content_block_start":
            cb = ev.get("content_block") or {}
            btype = cb.get("type") or "text"
            block = Block(type=btype)
            if btype == "tool_use":
                block.tool_use_name = cb.get("name") or ""
                block.tool_use_id = cb.get("id") or ""
            elif btype == "thinking":
                block.text = cb.get("thinking") or ""
            elif btype == "text":
                block.text = cb.get("text") or ""
            idx = ev.get("index")
            if not isinstance(idx, int) or idx < 0:
                self.blocks.append(block)  # no usable index — append at end
            else:
                self._ensure_index(idx)
                self.blocks[idx] = block
        elif etype == "content_block_delta":
            block = self._block_for_delta(ev.get("index"))
            if block is None:
                return  # malformed/out-of-order delta — drop it
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                block.text += delta.get("text") or ""
            elif dtype == "thinking_delta":
                block.text += delta.get("thinking") or ""
            elif dtype == "input_json_delta":
                block.tool_use_input_json += delta.get("partial_json") or ""
        elif etype == "message_stop":
            self.stopped = True

    def _block_for_delta(self, idx) -> Block | None:
        """Resolve the target block for a delta, tolerating bad indices.

        Deltas normally target the most recently opened block. A missing or
        negative index falls back to the last block (the original behavior for
        the common case); when no block exists yet, returns None so the caller
        drops the delta instead of crashing on ``self.blocks[-1]``.
        """
        if isinstance(idx, int) and 0 <= idx < len(self.blocks):
            return self.blocks[idx]
        if self.blocks:
            return self.blocks[-1]
        return None

    def _ensure_index(self, idx: int) -> None:
        while len(self.blocks) <= idx:
            self.blocks.append(Block(type="text"))


# Back-compat name used by cli_driver internals before the move.
_Block = Block


# ── live Markdown settling (T-1) ──

#: Mirrors widgets.markdown._FENCE minus the language capture. Duplicated
#: rather than imported because markdown.py pulls in gi/GtkSource, which this
#: module exists to stay clear of; test_message_bubble's parse-equivalence
#: test is the guard against the two drifting apart.
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*[\w.+\-]*\s*$")


def stable_markdown_prefix(text: str) -> int:
    """Character offset up to which *text* is safe to render as finished
    Markdown: the end of the last blank line that is NOT inside an open code
    fence. Every block form widgets.markdown._parse recognises (para, heading,
    ul, ol, table, quote, rule) terminates at a blank line, and a fenced block
    is the only one that spans one — so _parse(text[:n]) + _parse(text[n:])
    equals _parse(text) exactly. 0 when nothing has settled."""
    safe = offset = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if fence is None:
            m = _FENCE_RE.match(stripped)
            if m:
                fence = m.group(1)
            elif not stripped.strip():
                safe = offset + len(line)
        elif stripped.startswith(fence[0] * len(fence)) and stripped.strip().count(
            fence[0]
        ) >= len(fence):
            fence = None  # close condition copied verbatim from markdown.py
        offset += len(line)
    return safe


# ── terminal turn accounting (M-6a) ──

_SQLITE_INTEGER_MAX = (1 << 63) - 1
_MICRO_USD_PER_USD = 1_000_000


def terminal_cost_micro_usd(result: dict) -> int | None:
    """Convert Claude's authoritative terminal dollar cost to integer micro-USD."""

    raw = result.get("total_cost_usd")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    scaled = value * _MICRO_USD_PER_USD
    if not math.isfinite(scaled):
        return None
    rounded = int(round(scaled))
    if rounded > _SQLITE_INTEGER_MAX:
        return None
    return rounded


def terminal_tokens(result: dict) -> tuple[int, int] | None:
    """(input, output) counters from a native terminal result's ``usage``.

    Same counter rules as the cost parser: a bool is not a count and neither
    is a negative. All-or-nothing on purpose — half a pair would render as
    "0 out", which reads as a measurement rather than a gap.
    """

    # A provider that runs several requests per turn reports two different
    # things: what is resident in the window right now (``usage``, which the
    # context-fill projection reads) and what this turn actually consumed
    # (``turn_usage``). The footer describes the turn, so it takes the latter
    # when a driver supplies it.
    usage = result.get("turn_usage")
    if not isinstance(usage, dict):
        usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    values = [usage.get("input_tokens"), usage.get("output_tokens")]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
        return None
    # On Claude almost the whole prompt is cache reads/writes and
    # `input_tokens` alone is single digits; "in" means the prompt as sent
    # (the same three-way sum docs/CLAUDE-CONTEXT-BUDGET.md measures with).
    cached = 0
    for key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        extra = usage.get(key)
        if isinstance(extra, int) and not isinstance(extra, bool) and extra > 0:
            cached += extra
    return values[0] + cached, values[1]


def format_turn_footer(result: dict) -> str:
    """One line of per-turn accounting: ``"1,234 in · 567 out · $0.0123"``.

    Sub-dollar turns keep four decimals — most turns cost fractions of a cent,
    and "$0.00" on a real one reads as broken. Each half is dropped when the
    provider did not report it (Codex and OpenRouter results carry no
    ``total_cost_usd``), so an empty string means nothing was known and the
    caller shows no footer at all.
    """

    parts = []
    tokens = terminal_tokens(result)
    if tokens is not None:
        parts += [f"{tokens[0]:,} in", f"{tokens[1]:,} out"]
    micro = terminal_cost_micro_usd(result)
    # ponytail: a reported 0.0 is treated as "not priced" rather than shown as
    # $0.0000 — no provider bills a real turn at zero, so it is always a gap.
    if micro:
        parts.append(
            f"${micro / 1e6:.4f}"
            if micro < _MICRO_USD_PER_USD
            else f"${micro / 1e6:.2f}"
        )
    return " · ".join(parts)


def is_background_wakeup(result: object) -> bool:
    """A `result` the CLI emitted because a background subagent finished.

    It wakes the root for a turn nobody sent; it owns no attempt and must not
    touch the footer of the turn on screen (GPT cross-check, 2026-09-03).
    """
    if not isinstance(result, dict):
        return False
    origin = result.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "task-notification"
