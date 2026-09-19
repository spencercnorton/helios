"""Prose checks for ``draft_metadata`` output.

Separate from ``specialty_verifiers`` because it is a different kind of check.
The other four verifiers ground a claim in evidence the host already holds — a
line range, a taxonomy, a file inventory — and either it resolves or it does
not. This one has no such anchor: a commit message is prose, and the only
mechanically checkable claim it makes is which parameters a docstring says the
function takes. Everything else here is a heuristic, which is why
``draft_metadata``'s registry entry states plainly that no controlled study
supports delegating it and it rests on always being human-reviewed.

GTK-free.
"""

from __future__ import annotations

import re

from helios.backend.specialty_schema import Finding, fail

__all__ = ["verify_metadata"]

_METADATA_KINDS = frozenset({"commit_message", "pr_description", "docstring"})

# Anchored to marker-shaped text. A bare substring match rejected correct
# output: commit messages and docstrings discuss TODO/FIXME markers constantly.
_PLACEHOLDER_RE = re.compile(
    r"^[ \t]*(?:todo|tbd|fixme|xxx)\b[ \t]*:?[ \t]*$"  # a line that is only a marker
    r"|<[a-z]+(?: [a-z]+)+>"                              # <describe the change>
    r"|\{\{[^}\n]{1,80}\}\}"                            # {{placeholder}}
    r"|\breplace[_ ]me\b"
    r"|\blorem ipsum\b",
    re.IGNORECASE | re.MULTILINE,
)

# ``:param name:`` is unambiguous, so it is parsed anywhere in the text.
_SPHINX_PARAM_RE = re.compile(r"^\s*:param\s+\*{0,2}([A-Za-z_]\w*)\s*:", re.MULTILINE)

# Google ``name (type):`` and NumPy ``name : type`` share one shape once the
# indent requirement is dropped. Dropping it is required: a model returns the
# docstring as a bare string, so NumPy declarations arrive at column zero and an
# indent-anchored pattern silently matches nothing — the check would look like
# it ran and find nothing to complain about. Because the pattern is now loose
# enough to match ordinary prose ("Usage: ..."), it is only applied *inside* a
# parameter section.
_PARAM_DECL_RE = re.compile(
    r"^[ \t]*\*{0,2}([A-Za-z_]\w*(?:[ \t]*,[ \t]*\*{0,2}[A-Za-z_]\w*)*)"
    r"[ \t]*(?:\([^)\n]*\))?[ \t]*:"
)
_PARAM_SECTION_RE = re.compile(
    r"^[ \t]*(?:keyword[ \t]+)?(?:args|arguments|parameters|params)[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)
_OTHER_SECTION_RE = re.compile(
    r"^[ \t]*(?:returns?|yields?|raises?|examples?|notes?|attributes|methods|"
    r"see[ \t]+also|references|warns?|warnings?|other[ \t]+parameters|todo)"
    r"[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)
_UNDERLINE_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$")


def _parameter_section_lines(text: str) -> list[str]:
    """Lines belonging to Args/Parameters sections, both Google and NumPy."""
    lines = text.splitlines()
    collected: list[str] = []
    index = 0
    while index < len(lines):
        if not _PARAM_SECTION_RE.match(lines[index]):
            index += 1
            continue
        index += 1
        if index < len(lines) and _UNDERLINE_RE.match(lines[index]):
            index += 1  # NumPy underline
        while index < len(lines):
            line = lines[index]
            if _PARAM_SECTION_RE.match(line) or _OTHER_SECTION_RE.match(line):
                break
            if index + 1 < len(lines) and _UNDERLINE_RE.match(lines[index + 1]):
                break  # a NumPy heading for the next section
            collected.append(line)
            index += 1
    return collected


def _documented_parameters(text: str) -> list[str]:
    """Parameter names the docstring appears to document, in order."""
    names: list[str] = []

    def add(name: str) -> None:
        name = name.strip().lstrip("*")
        if name and name not in names:
            names.append(name)

    for match in _SPHINX_PARAM_RE.finditer(text):
        add(match.group(1))
    for line in _parameter_section_lines(text):
        match = _PARAM_DECL_RE.match(line)
        if match:
            for part in match.group(1).split(","):
                add(part)
    return names


def verify_metadata(
    output: object,
    *,
    kind: str,
    known_parameters: set[str] | frozenset[str] = frozenset(),
    max_chars: int = 4_000,
) -> list[Finding]:
    """Bound the text, and for docstrings check the parameters actually exist.

    This tool has no benchmark backing anywhere in the literature — it is
    delegated on pragmatics (a human reads every commit message before it
    lands), not on evidence. So the checks are the ones that can be made
    mechanically: no placeholder text, bounded length, and for a docstring,
    every documented parameter must appear in the real signature.
    """
    findings: list[Finding] = []
    if not isinstance(output, dict):
        fail(findings, "NOT_AN_OBJECT", "$", "output is not a JSON object")
        return findings

    text = output.get("text")
    if not isinstance(text, str) or not text.strip():
        fail(findings, "MISSING_FIELD", "$.text", "text is required")
        return findings
    if len(text) > max_chars:
        fail(findings, "TEXT_TOO_LONG", "$.text", f"text exceeds {max_chars} characters")

    # Word-bounded: a bare substring match makes "tbd" fire inside a real word
    # and "xxx" fire on any repeated character, which is a false positive on
    # legitimate text rather than a caught placeholder.
    match = _PLACEHOLDER_RE.search(text)
    if match is not None:
        fail(findings, "PLACEHOLDER_TEXT", "$.text",
              f"text contains the placeholder {match.group(0)!r}")

    if kind not in _METADATA_KINDS:
        fail(findings, "UNKNOWN_KIND", "$", f"unsupported metadata kind {kind!r}")
        return findings

    if kind == "docstring":
        # Parse the docstring TEXT, not a parameter list the worker writes
        # about itself. A self-reported index is not evidence: a worker that
        # invents a parameter in the prose and omits it from the array — or
        # sends an empty array — would otherwise pass, and the artifact a human
        # reads is the text.
        for name in _documented_parameters(text):
            if name not in known_parameters:
                fail(findings, "UNKNOWN_PARAMETER", "$.text",
                      f"{name!r} is documented but is not in the real signature")
    return findings


