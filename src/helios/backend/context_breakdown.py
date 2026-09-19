"""What is actually filling the context window.

The providers report ONE number: how many tokens the last request carried.
None of them break it down, so the split has to be reconstructed from the
transcript we already have.

The honest shape of the answer:

  * ``used`` is **measured** — it comes straight off the provider's usage
    report and is exact. It is the ONLY exact number here.
  * every segment is **estimated** — text length over a chars-per-token
    ratio, then scaled so the parts sum to the measured total. Ratios are
    stable enough to answer the only question that matters ("what is eating
    my window — tool output, or the conversation?") and are never presented
    as exact.
  * whatever the transcript cannot see (system prompt, tool schemas, MCP
    server definitions, CLAUDE.md) is the residual: measured total minus
    estimated conversation. It is the fixed cost of the session before
    anyone says a word — and it is an estimate too, because subtracting an
    estimate from a measurement gives an estimate. Every error in the
    conversation counts lands in it, and it is usually the largest segment,
    so labelling it "measured" would put the most confident label on the
    least certain number.

GTK-free on purpose: the CI slim image exercises this directly.
"""

from __future__ import annotations

import os

import json
from dataclasses import dataclass

# One token per this many characters of English/code. The usual working
# approximation; wrong by a few percent, which is far inside the precision
# anyone reads off a stacked bar.
_CHARS_PER_TOKEN = 4

# Order is display order, top of the bar first.
BASELINE = "baseline"
TOOLS = "tools"
USER = "user"
ASSISTANT = "assistant"
THINKING = "thinking"

_LABELS: dict[str, str] = {
    BASELINE: "System, tools & memory",
    TOOLS: "Tool calls & results",
    USER: "Your messages",
    ASSISTANT: "Assistant replies",
    THINKING: "Thinking",
}


@dataclass(frozen=True, slots=True)
class Segment:
    key: str
    label: str
    tokens: int


@dataclass(frozen=True, slots=True)
class Breakdown:
    used: int          # measured, exact
    window: int        # the model's cap
    segments: tuple[Segment, ...]

    @property
    def free(self) -> int:
        return max(0, self.window - self.used)

    @property
    def fraction(self) -> float:
        return min(1.0, self.used / self.window) if self.window > 0 else 0.0


def _tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


def _tool_use_chars(tool_use) -> int:
    """A tool call costs its name plus its serialized arguments."""
    try:
        rendered = json.dumps(tool_use.input, default=str)
    except (TypeError, ValueError):
        rendered = str(tool_use.input)
    return len(tool_use.name) + len(rendered)


def raw_counts(turns) -> dict[str, int]:
    """Estimated tokens per category for the conversation itself.

    Sidechain (subagent) turns are excluded: they ran in their own window and
    only their summarized result came back into this one.
    """
    counts = {TOOLS: 0, USER: 0, ASSISTANT: 0, THINKING: 0}
    for turn in turns:
        if getattr(turn, "is_sidechain", False):
            continue
        for span in getattr(turn, "content", ()):
            bucket = THINKING if span.kind in ("thinking", "reasoning_summary") else (
                USER if turn.role == "user" else ASSISTANT
            )
            counts[bucket] += _tokens(span.text)
        for tool_use in getattr(turn, "tool_uses", ()):
            counts[TOOLS] += _tool_use_chars(tool_use) // _CHARS_PER_TOKEN
        for result in getattr(turn, "tool_results", ()):
            counts[TOOLS] += _tokens(result.content)
    return counts


def summarize(turns, used: int, window: int) -> Breakdown:
    """Combine the measured total with the estimated conversation split.

    Estimates are scaled to fit under the measured total, so the bar always
    adds up to what the provider actually reported — an estimate that
    overflows the truth is worse than no estimate at all.
    """
    if used <= 0 or window <= 0:
        return Breakdown(used=max(0, used), window=max(0, window), segments=())

    counts = raw_counts(turns)
    conversation = sum(counts.values())

    if conversation > used:
        # The heuristic ran long. Scale the whole conversation to the measured
        # total rather than inventing a negative baseline.
        scale = used / conversation
        counts = {key: int(value * scale) for key, value in counts.items()}
        conversation = sum(counts.values())

    baseline = max(0, used - conversation)
    ordered = [(BASELINE, baseline)] + [
        (key, counts[key]) for key in (TOOLS, USER, ASSISTANT, THINKING)
    ]
    return Breakdown(
        used=used,
        window=window,
        segments=tuple(
            Segment(key, _LABELS[key], value) for key, value in ordered if value > 0
        ),
    )


# ── measured breakdown, straight from the provider ─────────────────────────
#
# `get_context_usage` (a control_request the CLI answers before the first turn,
# at no token cost) returns the real per-category split. Where it is available
# it replaces the estimate above wholesale — no 4-chars-per-token scaling, and
# no residual standing in for "everything the transcript cannot see".
#
# Measured against claude 2.1.224 on 2026-08-07, `--model sonnet`:
#
#     totalTokens 40,609  = the sum of the NON-deferred categories, exactly
#     maxTokens / rawMaxTokens 967,000
#     modelUsage[...].contextWindow 1,000,000
#     Autocompact buffer 33,000        967,000 + 33,000 = 1,000,000
#     totalTokens + Free space + Autocompact buffer = 967,000
#
# So the two "window" numbers are not in conflict; they answer different
# questions. 1,000,000 is the model's hard cap. 967,000 is what you may use
# before autocompaction fires. The second is the one a user can act on, and it
# is what the CLI's own /context reports, so it is the denominator here.

#: Categories the CLI flags `isDeferred` describe tool schemas the prompt does
#: NOT carry — they are excluded from `totalTokens` by the CLI itself. Counting
#: them as occupancy inverts the whole point of deferral and overstates the
#: fixed cost by tens of thousands of tokens.
DEFERRED = "deferred"
AUTOCOMPACT = "autocompact"


def from_measured(payload: dict) -> Breakdown | None:
    """Build a Breakdown from a `get_context_usage` payload, or None.

    None means "the provider did not give us something usable" — callers must
    keep the estimate rather than rendering an empty bar.
    """

    if not isinstance(payload, dict):
        return None
    categories = payload.get("categories")
    if not isinstance(categories, list) or not categories:
        return None

    window = _as_int(payload.get("maxTokens"))
    if window <= 0:
        return None

    segments: list[Segment] = []
    for entry in categories:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        tokens = _as_int(entry.get("tokens"))
        if not name or tokens <= 0:
            continue
        # "Free space" is the remainder the bar already draws as empty; adding
        # it as a segment would fill the bar to 100% at all times.
        if name.lower() == "free space":
            continue
        if entry.get("isDeferred"):
            # Kept out of the bar deliberately — see DEFERRED above.
            continue
        key = AUTOCOMPACT if "autocompact" in name.lower() else name.lower()
        segments.append(Segment(key=key, label=name, tokens=tokens))

    if not segments:
        return None

    used = _as_int(payload.get("totalTokens"))
    if used <= 0:
        used = sum(s.tokens for s in segments if s.key != AUTOCOMPACT)
    return Breakdown(used=used, window=window, segments=tuple(segments))


def deferred_tokens(payload: dict) -> int:
    """Schema tokens the prompt does NOT carry, for an explanatory line.

    Worth surfacing precisely because it is large and easy to misread: an
    earlier audit quoted these figures as prompt cost and concluded the MCP
    budget had grown by an order of magnitude. It had not.
    """

    if not isinstance(payload, dict):
        return 0
    return sum(
        _as_int(entry.get("tokens"))
        for entry in (payload.get("categories") or [])
        if isinstance(entry, dict) and entry.get("isDeferred")
    )


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# ── the rest of the measured payload ─────────────────────────────────────────
#
# `get_context_usage` also reports WHICH memory files, skills and agents are in
# the window and what they cost, the message split by kind, and the autocompact
# threshold. Measured on claude 2.1.258 (2026-09-02) — see
# docs/CLAUDE-PARITY-PLAN.md, Probe 1. This is what Cursor's context report
# shows and what the 2026-08 budget doc said could not be measured per skill.


@dataclass(frozen=True)
class ContextDetails:
    memory_files: tuple[tuple[str, int], ...]
    skills: tuple[tuple[str, int], ...]
    skills_count: int
    skills_total: int
    agents: tuple[tuple[str, int], ...]
    tool_calls: int
    tool_results: int
    attachments: int
    assistant: int
    user: int
    autocompact_threshold: int
    autocompact_enabled: bool | None


def details_from_measured(payload: dict) -> ContextDetails | None:
    if not isinstance(payload, dict):
        return None
    memory = tuple(
        (str(entry.get("path") or ""), _as_int(entry.get("tokens")))
        for entry in (payload.get("memoryFiles") or [])
        if isinstance(entry, dict) and entry.get("path")
    )
    skills_block = payload.get("skills") if isinstance(payload.get("skills"), dict) else {}
    skills = tuple(
        sorted(
            (
                (str(entry.get("name") or ""), _as_int(entry.get("tokens")))
                for entry in (skills_block.get("skillFrontmatter") or [])
                if isinstance(entry, dict) and entry.get("name")
            ),
            key=lambda item: (-item[1], item[0]),
        )
    )
    agents = tuple(
        sorted(
            (
                (str(entry.get("agentType") or entry.get("name") or ""), _as_int(entry.get("tokens")))
                for entry in (payload.get("agents") or [])
                if isinstance(entry, dict) and (entry.get("agentType") or entry.get("name"))
            ),
            key=lambda item: (-item[1], item[0]),
        )
    )
    messages = (
        payload.get("messageBreakdown")
        if isinstance(payload.get("messageBreakdown"), dict)
        else {}
    )
    enabled = payload.get("isAutoCompactEnabled")
    details = ContextDetails(
        memory_files=memory,
        skills=skills,
        skills_count=_as_int(skills_block.get("includedSkills") or skills_block.get("totalSkills")) or len(skills),
        skills_total=_as_int(skills_block.get("tokens")) or sum(t for _n, t in skills),
        agents=agents,
        tool_calls=_as_int(messages.get("toolCallTokens")),
        tool_results=_as_int(messages.get("toolResultTokens")),
        attachments=_as_int(messages.get("attachmentTokens")),
        assistant=_as_int(messages.get("assistantMessageTokens")),
        user=_as_int(messages.get("userMessageTokens")),
        autocompact_threshold=_as_int(payload.get("autoCompactThreshold")),
        autocompact_enabled=enabled if isinstance(enabled, bool) else None,
    )
    if not (memory or skills or agents or messages or details.autocompact_threshold):
        return None
    return details


def _short_path(path: str, home: str = "") -> str:
    home = home or os.path.expanduser("~")
    return "~" + path[len(home):] if home and path.startswith(home) else path


def describe_details(details: ContextDetails, window: int, *, home: str = "") -> list[str]:
    """Caption lines for the popover. Short, and only for what is present."""
    lines: list[str] = []
    if details.memory_files:
        total = sum(t for _p, t in details.memory_files)
        named = ", ".join(
            f"{_short_path(path, home)} {tokens:,}" for path, tokens in details.memory_files[:3]
        )
        more = len(details.memory_files) - 3
        lines.append(
            f"Memory files: {total:,} tokens — {named}" + (f", +{more} more" if more > 0 else "")
        )
    if details.skills:
        top = ", ".join(f"{name} {tokens:,}" for name, tokens in details.skills[:3])
        lines.append(
            f"Skills: {details.skills_count} loaded, {details.skills_total:,} tokens — largest {top}"
        )
    if details.agents:
        lines.append(
            "Custom agents: " + ", ".join(f"{name} {tokens:,}" for name, tokens in details.agents[:4])
        )
    parts = [
        f"{value:,} {label}"
        for label, value in (
            ("tool results", details.tool_results),
            ("tool calls", details.tool_calls),
            ("attachments", details.attachments),
            ("assistant", details.assistant),
            ("user", details.user),
        )
        if value > 0
    ]
    if parts:
        lines.append("Messages: " + " · ".join(parts))
    if details.autocompact_enabled is False:
        lines.append("Auto-compaction is off for this session.")
    elif details.autocompact_threshold > 0:
        pct = (
            f" ({details.autocompact_threshold * 100 // window}% of the window)"
            if window > 0
            else ""
        )
        lines.append(f"Auto-compacts at {details.autocompact_threshold:,} tokens{pct}.")
    return lines
