"""The command inventory behind the composer's `/` completion popover.

The CLI hands Helios its whole command surface on `initialize` — built-ins,
user commands and skills, each with a description and an `argumentHint` — and
the palette's `AgentCommand` describes the provider-native control actions.
Both flatten into the one row shape the popover renders, so the completion
list is a single ranked list rather than two.

Keep this module GTK-free: it runs in the python-slim CI lane.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """One completable `/name`, described by whoever advertised it."""

    name: str
    description: str = ""
    argument_hint: str = ""
    source: str = ""


def _text(value: object) -> str:
    return str(value).strip() if value else ""


def _row(item: object) -> SlashCommand | None:
    """One CLI dict or `AgentCommand`-shaped object as a row, or None."""

    if isinstance(item, dict):
        name = _text(item.get("name"))
        description = _text(item.get("description"))
        hint = _text(item.get("argumentHint") or item.get("argument_hint"))
        source = _text(item.get("source"))
    else:
        name = _text(getattr(item, "name", ""))
        description = _text(getattr(item, "description", ""))
        # An AgentCommand advertises a boolean, never the CLI's hint string.
        hint = "[arguments]" if getattr(item, "accepts_arguments", False) else ""
        source = _text(getattr(item, "source", ""))
    name = name.lstrip("/")
    if not name:
        return None
    return SlashCommand(
        name=name,
        description=description,
        argument_hint=hint,
        source=source,
    )


def normalize_commands(raw: object) -> tuple[SlashCommand, ...]:
    """Flatten an advertised command list into name-sorted rows.

    Nameless entries are dropped and the first row for a name wins, so a
    provider-native command shadows a same-named CLI one rather than the list
    showing both. Matching is case-insensitive, so the de-duplication is too.
    """

    if not isinstance(raw, (list, tuple)):
        return ()
    seen: set[str] = set()
    rows: list[SlashCommand] = []
    for item in raw:
        row = _row(item)
        if row is None or row.name.casefold() in seen:
            continue
        seen.add(row.name.casefold())
        rows.append(row)
    return tuple(sorted(rows, key=lambda row: row.name))


def _is_subsequence(needle: str, name: str) -> bool:
    # The classic one-pass test: each character consumes more of the same
    # iterator, so order is enforced without an index.
    remaining = iter(name)
    return all(char in remaining for char in needle)


def match_commands(
    commands: tuple[SlashCommand, ...] | list[SlashCommand],
    typed: str,
    limit: int = 8,
) -> tuple[SlashCommand, ...]:
    """Rank `commands` against the text typed after the leading slash.

    Three tiers, best first: prefix, then substring, then subsequence (so
    `cmpt` still reaches `compact`). Input order is preserved inside a tier —
    `normalize_commands` returns name order, which is what the popover wants.
    """

    needle = str(typed or "").strip().casefold()
    if not needle:
        return tuple(commands)[:limit]
    prefix: list[SlashCommand] = []
    substring: list[SlashCommand] = []
    subsequence: list[SlashCommand] = []
    for command in commands:
        name = command.name.casefold()
        if name.startswith(needle):
            prefix.append(command)
        elif needle in name:
            substring.append(command)
        elif _is_subsequence(needle, name):
            subsequence.append(command)
    return tuple(prefix + substring + subsequence)[:limit]


def completion_text(command: SlashCommand) -> str:
    """The text one accepted row writes — trailing space so arguments follow."""

    return f"/{command.name} "
