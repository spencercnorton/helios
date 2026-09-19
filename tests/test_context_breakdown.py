"""What the context breakdown claims, and what it refuses to claim.

The split is an estimate over the transcript; the total it sits under is a
measurement. These pin the boundary between the two — an estimate that
overflows the measurement is worse than no estimate at all.

Deliberately GTK-free so the slim CI lane runs it.
"""

from __future__ import annotations

from helios.backend import context_breakdown as cb
from helios.backend.transcript import ContentSpan, ToolResult, ToolUse, Turn


def _assistant(text="", thinking="", tools=()):
    turn = Turn(role="assistant")
    if thinking:
        turn.content.append(ContentSpan("thinking", thinking))
    if text:
        turn.content.append(ContentSpan("text", text))
    for name, payload in tools:
        turn.tool_uses.append(ToolUse(name=name, input=payload))
    return turn


def _user(text="", results=()):
    turn = Turn(role="user")
    if text:
        turn.content.append(ContentSpan("text", text))
    for content in results:
        turn.tool_results.append(ToolResult(tool_use_id="t", content=content))
    return turn


def test_split_never_exceeds_the_measured_total():
    # A long conversation against a deliberately small measured total: the
    # estimate has to yield to the measurement, not the other way round.
    turns = [_user("u" * 40_000), _assistant(text="a" * 40_000)]
    result = cb.summarize(turns, used=5_000, window=200_000)
    assert sum(s.tokens for s in result.segments) <= 5_000
    assert result.used == 5_000


def test_unseen_system_cost_becomes_the_baseline_segment():
    turns = [_user("hello"), _assistant(text="hi")]
    result = cb.summarize(turns, used=20_000, window=200_000)
    baseline = next(s for s in result.segments if s.key == cb.BASELINE)
    # Two tiny messages cannot account for 20k — the rest is the system
    # prompt, tool schemas and CLAUDE.md, which the transcript never shows.
    assert baseline.tokens > 19_000


def test_baseline_absorbs_estimation_error_so_it_is_not_exact():
    # The baseline is measured-total minus an ESTIMATE, so every error in the
    # conversation counts lands in it. Nothing may present it as exact: the
    # same measured total with different conversation content yields a
    # different baseline, which an exact number could not do.
    small = cb.summarize([_user("hi")], used=20_000, window=200_000)
    large = cb.summarize([_user("x" * 20_000)], used=20_000, window=200_000)
    base_small = next(s for s in small.segments if s.key == cb.BASELINE).tokens
    base_large = next(s for s in large.segments if s.key == cb.BASELINE).tokens
    assert base_small != base_large
    assert not hasattr(cb.Segment, "is_estimated")


def test_tool_output_is_attributed_to_tools_not_to_the_user():
    # The point of the whole widget: showing that a 100k grep is what filled
    # the window, not the conversation.
    turns = [
        _user("find it"),
        _assistant(tools=[("Grep", {"pattern": "x"})]),
        _user(results=["hit\n" * 20_000]),
    ]
    result = cb.summarize(turns, used=60_000, window=200_000)
    by_key = {s.key: s.tokens for s in result.segments}
    assert by_key[cb.TOOLS] > by_key.get(cb.USER, 0) * 10


def test_sidechain_turns_are_not_counted():
    # A subagent ran in its own window; only its summary came back to ours.
    sidechain = _assistant(text="z" * 40_000)
    sidechain.is_sidechain = True
    with_sub = cb.summarize([_user("go"), sidechain], used=30_000, window=200_000)
    without = cb.summarize([_user("go")], used=30_000, window=200_000)
    assert [(s.key, s.tokens) for s in with_sub.segments] == [
        (s.key, s.tokens) for s in without.segments
    ]


def test_no_usage_yields_no_segments():
    assert cb.summarize([_user("hi")], used=0, window=200_000).segments == ()


def test_thinking_is_its_own_slice():
    turns = [_assistant(thinking="t" * 8_000, text="short")]
    result = cb.summarize(turns, used=50_000, window=200_000)
    assert any(s.key == cb.THINKING and s.tokens > 1_500 for s in result.segments)


_MEASURED = {
    "categories": [{"name": "System prompt", "tokens": 4268}, {"name": "Free space", "tokens": 974194}],
    "totalTokens": 25806,
    "maxTokens": 1000000,
    "memoryFiles": [{"path": "/home/x/.claude/CLAUDE.md", "type": "User", "tokens": 7913}],
    "skills": {
        "totalSkills": 23,
        "includedSkills": 23,
        "tokens": 3715,
        "skillFrontmatter": [
            {"name": "ponytail", "source": "userSettings", "tokens": 279},
            {"name": "resolve-estate-credential", "tokens": 208},
            {"name": "ship-gitlab-mr", "tokens": 189},
            {"name": "tandem", "tokens": 126},
        ],
    },
    "agents": [{"agentType": "gpt", "tokens": 328}, {"agentType": "implementer", "tokens": 230}],
    "messageBreakdown": {
        "toolCallTokens": 0,
        "toolResultTokens": 1200,
        "attachmentTokens": 2767,
        "assistantMessageTokens": 0,
        "userMessageTokens": 40,
    },
    "autoCompactThreshold": 967000,
    "isAutoCompactEnabled": True,
}


def test_measured_details_keep_files_skills_agents_and_the_threshold():
    """The 2.1.258 payload shape from docs/CLAUDE-PARITY-PLAN.md Probe 1."""
    details = cb.details_from_measured(_MEASURED)
    assert details is not None
    assert details.memory_files == (("/home/x/.claude/CLAUDE.md", 7913),)
    assert details.skills[0] == ("ponytail", 279)
    assert details.skills_count == 23 and details.skills_total == 3715
    assert details.agents == (("gpt", 328), ("implementer", 230))
    assert details.tool_results == 1200 and details.attachments == 2767
    assert details.autocompact_threshold == 967000
    assert details.autocompact_enabled is True

    lines = cb.describe_details(details, 1000000, home="/home/x")
    assert lines[0].startswith("Memory files: 7,913 tokens — ~/.claude/CLAUDE.md 7,913")
    assert "Skills: 23 loaded, 3,715 tokens — largest ponytail 279" in lines[1]
    assert lines[2] == "Custom agents: gpt 328, implementer 230"
    assert lines[3] == "Messages: 1,200 tool results · 2,767 attachments · 40 user"
    assert lines[4] == "Auto-compacts at 967,000 tokens (96% of the window)."


def test_measured_details_are_absent_for_a_bare_payload():
    assert cb.details_from_measured({"categories": [{"name": "x", "tokens": 1}]}) is None
    assert cb.details_from_measured("nope") is None
    off = cb.details_from_measured({"autoCompactThreshold": 5, "isAutoCompactEnabled": False})
    assert off is not None
    assert cb.describe_details(off, 100) == ["Auto-compaction is off for this session."]
