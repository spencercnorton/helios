"""`slash_commands` — the composer's completion inventory and its matcher.

GTK-free by construction: this is the half of the `/` completion the
`python:3.13-slim` CI lane can run.
"""

from __future__ import annotations

from helios.backend.agent_commands import AgentCommand
from helios.backend.slash_commands import (
    SlashCommand,
    completion_text,
    match_commands,
    normalize_commands,
)


def _cli(name: str, description: str = "", hint: str = "") -> dict:
    """One entry shaped like the CLI's `initialize.commands` payload."""
    return {"name": name, "description": description, "argumentHint": hint}


def _named(*names: str) -> tuple[SlashCommand, ...]:
    return normalize_commands([_cli(name) for name in names])


# ── normalize ────────────────────────────────────────────────────────────


def test_normalize_reads_the_cli_payload() -> None:
    rows = normalize_commands(
        [
            _cli("compact", "Compact the conversation", "<focus>"),
            {
                "name": "review",
                "description": "Review changes",
                "argument_hint": "<paths>",
                "source": "user",
            },
        ]
    )
    assert rows == (
        SlashCommand("compact", "Compact the conversation", "<focus>"),
        SlashCommand("review", "Review changes", "<paths>", "user"),
    )


def test_normalize_reads_agent_commands() -> None:
    """The palette's rows are duck-typed in: a boolean, not a hint string."""
    rows = normalize_commands(
        [
            AgentCommand(
                command_id="context.compact",
                name="compact",
                title="Compact context",
                description="Replace older history with the provider summary.",
                source="Claude CLI · /compact",
                accepts_arguments=True,
            ),
            AgentCommand(
                command_id="thread.fork",
                name="fork",
                title="Fork conversation",
                description="Branch at the current head.",
                source="Codex App Server · thread/fork",
            ),
        ]
    )
    assert [(row.name, row.argument_hint) for row in rows] == [
        ("compact", "[arguments]"),
        ("fork", ""),
    ]
    assert rows[0].source == "Claude CLI · /compact"


def test_normalize_drops_blanks_and_bare_slashes() -> None:
    assert normalize_commands([_cli(""), _cli("   "), _cli("/"), {}]) == ()


def test_normalize_strips_a_leading_slash() -> None:
    assert normalize_commands([_cli("/compact")])[0].name == "compact"


def test_normalize_dedupes_by_name_first_wins() -> None:
    rows = normalize_commands(
        [_cli("compact", "native"), _cli("Compact", "cli"), _cli("compact", "cli")]
    )
    assert [(row.name, row.description) for row in rows] == [("compact", "native")]


def test_normalize_sorts_by_name() -> None:
    assert [row.name for row in _named("zulu", "alpha", "mike")] == [
        "alpha",
        "mike",
        "zulu",
    ]


def test_normalize_ignores_a_payload_that_is_not_a_list() -> None:
    assert normalize_commands(None) == ()
    assert normalize_commands("compact") == ()
    assert normalize_commands({"name": "compact"}) == ()


# ── match ────────────────────────────────────────────────────────────────


def test_match_empty_typed_returns_the_first_rows() -> None:
    commands = _named(*"abcdefghijkl")
    assert [row.name for row in match_commands(commands, "")] == list("abcdefgh")


def test_match_prefix_beats_substring_beats_subsequence() -> None:
    commands = _named("compact", "recompact", "cmpt", "unrelated")
    assert [row.name for row in match_commands(commands, "comp")] == [
        "compact",  # prefix
        "recompact",  # substring
    ]
    # `cmp` is a prefix of `cmpt` but only a subsequence of the other two.
    assert [row.name for row in match_commands(commands, "cmp")] == [
        "cmpt",
        "compact",
        "recompact",
    ]


def test_match_is_case_insensitive() -> None:
    assert [row.name for row in match_commands(_named("Compact"), "COM")] == ["Compact"]


def test_match_respects_the_limit() -> None:
    commands = _named(*(f"compact{i}" for i in range(12)))
    assert len(match_commands(commands, "comp")) == 8
    assert len(match_commands(commands, "comp", limit=3)) == 3


def test_match_returns_nothing_when_the_token_matches_nothing() -> None:
    assert match_commands(_named("compact", "fork"), "zzz") == ()


def test_completion_text_leaves_room_for_arguments() -> None:
    assert completion_text(SlashCommand("compact", argument_hint="<focus>")) == (
        "/compact "
    )
