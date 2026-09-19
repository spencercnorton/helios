"""Falsifiable quality signals for the current user request.

A conversation transcript does not know whether background participants later
verified a branch, whether two commands used the same worktree revision, or
which files a passing test actually covered. This module reports only recorded
facts. It deliberately never promotes transcript evidence to Work-wide
"verified" state; that authority belongs to structured Work observations.
"""

from __future__ import annotations

import re
import shlex
from collections import defaultdict
from dataclasses import dataclass, field
from os import path as ospath
from typing import Iterable

from helios.backend.transcript import ToolResult, ToolUse, Turn


BUILD_TOOLS = {"edit", "multiedit", "write", "notebookedit"}

# Match a verifier only as the first executable (after an understood leading
# ``cd … &&``). Arbitrary setup chains are not interpreted: accepting a pytest
# token anywhere made ``pushd other-worktree && pytest`` look like a check in
# the transport cwd. Unsafe/masking shell forms are rejected below.
_COMMAND_START = r"^"
_ENV_PREFIX = r"(?:(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)"
_RUNNER_PREFIX = r"(?:(?:uv|poetry)\s+run\s+)?"
_VERIFY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        _COMMAND_START
        + _ENV_PREFIX
        + _RUNNER_PREFIX
        + r"(?:python\d*(?:\.\d+)?\s+-m\s+)?pytest(?:\s|$)",
        _COMMAND_START
        + _ENV_PREFIX
        + _RUNNER_PREFIX
        + r"python\d*(?:\.\d+)?\s+-m\s+(?:unittest|compileall|mypy)(?:\s|$)",
        _COMMAND_START
        + _ENV_PREFIX
        + _RUNNER_PREFIX
        + r"(?:python\d*(?:\.\d+)?\s+-m\s+)?ruff\s+check(?:\s|$)",
        _COMMAND_START
        + _ENV_PREFIX
        + _RUNNER_PREFIX
        + r"(?:python\d*(?:\.\d+)?\s+-m\s+)?ruff\s+format\s+--check(?:\s|$)",
        _COMMAND_START
        + _ENV_PREFIX
        + _RUNNER_PREFIX
        + r"(?:mypy|pyright|shellcheck|tox|nox|ctest)(?:\s|$)",
        _COMMAND_START
        + _ENV_PREFIX
        + r"(?:npm|pnpm|yarn)\s+(?:test|run\s+(?:test|lint|check|typecheck))(?:\s|$)",
        _COMMAND_START + _ENV_PREFIX + r"cargo\s+(?:test|check|clippy)(?:\s|$)",
        _COMMAND_START + _ENV_PREFIX + r"go\s+(?:test|vet)(?:\s|$)",
        _COMMAND_START + _ENV_PREFIX + r"(?:make|meson)\s+(?:test|check|lint)(?:\s|$)",
    )
)

_LEADING_CD = re.compile(
    r"^\s*cd\s+(?:--\s+)?"
    r"(?P<target>'[^']*'|\"[^\"]*\"|[^\s;&|]+)\s*"
    r"&&\s*(?P<remainder>.+)$",
    re.DOTALL,
)

_NON_VERIFY_OPTIONS = frozenset(
    {
        "--co",
        "--cache-show",
        "--collect-only",
        "--createstub",
        "--diff",
        "--exit-zero",
        "--fixtures",
        "--fixtures-per-test",
        "--funcargs",
        "--fix",
        "--fix-only",
        "--help",
        "--if-present",
        "--ignore-scripts",
        "--list",
        "--list-optional",
        "--list-presets",
        "--list-sessions",
        "--list-tests",
        "--listtests",
        "--listenvs",
        "--markers",
        "--notest",
        "--print-labels",
        "--setup-only",
        "--setup-plan",
        "--show-only",
        "--show-files",
        "--show-settings",
        "--showconfig",
        "--version",
        "-h",
    }
)

_STATIC_CD_FORBIDDEN = frozenset("$`*?[]{}()<>!")


@dataclass(slots=True)
class SessionInsight:
    title: str
    status: str  # ok | warn | error | info
    detail: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class _CheckObservation:
    outcome: str  # passed | failed | unknown
    command: str
    scope: str
    result: str = ""
    unknown_reason: str = ""
    position: int = -1

    @property
    def key(self) -> tuple[str, str]:
        return self.scope, self.command.strip()


@dataclass(frozen=True, slots=True)
class _ChangeObservation:
    path: str
    scope: str
    position: int

    @property
    def key(self) -> tuple[str, str]:
        normalized_path = ospath.normpath(self.path)
        if ospath.isabs(normalized_path):
            return "", normalized_path
        return self.scope, normalized_path


@dataclass(slots=True)
class _ObservedResult:
    state: str  # recorded | missing | conflicted
    result: ToolResult | None = None


def insights_for_turns(
    turns: Iterable[Turn], *, default_scope: str = ""
) -> list[SessionInsight]:
    turn_list = list(turns)
    request_start = _current_request_start(turn_list)
    request_turns = turn_list[request_start:]
    default_scope = _normalize_scope(default_scope)
    results_by_id = _results_by_id(request_turns)
    seen_tool_uses = _historical_tool_uses(turn_list[:request_start])
    ambiguous_tool_ids = _ambiguous_tool_ids(turn_list)

    recorded_changes: dict[tuple[str, str], _ChangeObservation] = {}
    unknown_changes: dict[
        tuple[str, str], tuple[_ChangeObservation, str]
    ] = {}
    unresolved_change_failures: dict[
        tuple[str, str], tuple[_ChangeObservation, str]
    ] = {}
    latest_check: _CheckObservation | None = None
    unresolved_failures: dict[tuple[str, str], _CheckObservation] = {}
    position = 0

    for turn in request_turns:
        for tool in turn.tool_uses:
            position += 1
            if tool.id and seen_tool_uses.get(tool.id) == (tool.name, tool.input):
                continue  # reloaded stable item, not a second invocation
            if tool.id:
                seen_tool_uses[tool.id] = (tool.name, tool.input)
            observed_result = (
                _ObservedResult("conflicted")
                if tool.id in ambiguous_tool_ids
                else _take_result(results_by_id, tool)
            )

            if _is_build_tool(tool):
                paths = _change_paths(tool)
                if not paths:
                    continue
                changes = tuple(
                    _ChangeObservation(
                        path=path,
                        scope=_change_scope(tool, default_scope),
                        position=position,
                    )
                    for path in paths
                )
                if observed_result.state != "recorded":
                    reason = (
                        "Conflicting results"
                        if observed_result.state == "conflicted"
                        else "No result"
                    )
                    for change in changes:
                        unknown_changes[change.key] = (change, reason)
                    continue
                result = observed_result.result
                assert result is not None
                if result.is_error:
                    for change in changes:
                        unresolved_change_failures[change.key] = (
                            change,
                            _failure_excerpt(result.content),
                        )
                    continue
                for change in changes:
                    unknown_changes.pop(change.key, None)
                    unresolved_change_failures.pop(change.key, None)
                    recorded_changes[change.key] = change
                continue

            check_script = _check_script(tool)
            if check_script is None:
                continue
            command = str(tool.input.get("command") or "").strip()
            scope = _display_scope(tool, default_scope)
            if observed_result.state != "recorded":
                latest_check = _CheckObservation(
                    "unknown",
                    command,
                    scope,
                    unknown_reason=(
                        "Conflicting results"
                        if observed_result.state == "conflicted"
                        else "No result"
                    ),
                    position=position,
                )
            else:
                result = observed_result.result
                assert result is not None
                if result.is_error:
                    latest_check = _CheckObservation(
                        "failed",
                        command,
                        scope,
                        _failure_excerpt(result.content),
                        position=position,
                    )
                    unresolved_failures[latest_check.key] = latest_check
                    continue
                latest_check = _CheckObservation(
                    "passed", command, scope, position=position
                )
                unresolved_failures.pop(latest_check.key, None)

    latest_failure = max(
        unresolved_failures.values(),
        key=lambda observation: observation.position,
        default=None,
    )
    return _render_insights(
        recorded_changes=recorded_changes,
        unknown_changes=unknown_changes,
        unresolved_change_failures=unresolved_change_failures,
        latest_check=latest_check,
        latest_failure=latest_failure,
    )


def _render_insights(
    *,
    recorded_changes: dict[tuple[str, str], _ChangeObservation],
    unknown_changes: dict[
        tuple[str, str], tuple[_ChangeObservation, str]
    ],
    unresolved_change_failures: dict[
        tuple[str, str], tuple[_ChangeObservation, str]
    ],
    latest_check: _CheckObservation | None,
    latest_failure: _CheckObservation | None,
) -> list[SessionInsight]:
    insights: list[SessionInsight] = []
    if unresolved_change_failures:
        failures = sorted(
            unresolved_change_failures.values(),
            key=lambda item: item[0].position,
            reverse=True,
        )
        count = len(failures)
        insights.append(
            SessionInsight(
                "File change failed",
                "warn",
                (
                    f"{count} file{'s were' if count != 1 else ' was'} targeted "
                    "by failed edit/write actions, with no successful retry "
                    "for the same file recorded in this request."
                ),
                tuple(
                    _change_result_evidence(change, "Failed", result)
                    for change, result in failures[:5]
                ),
            )
        )

    if unknown_changes:
        unknowns = sorted(
            unknown_changes.values(),
            key=lambda item: item[0].position,
            reverse=True,
        )
        insights.append(
            SessionInsight(
                "File change outcome unknown",
                "info",
                (
                    "Edit/write actions have no single unambiguous recorded "
                    "result. Helios cannot claim that those files changed."
                ),
                tuple(
                    _change_result_evidence(change, reason)
                    for change, reason in unknowns[:5]
                ),
            )
        )

    if latest_failure:
        insights.append(
            SessionInsight(
                "Check command failed",
                "error",
                (
                    "A check command failed in this request and no later pass "
                    "with the same command and recorded cwd resolved it."
                ),
                (
                    _scope_evidence(latest_failure.scope),
                    f"Failed: {_command_excerpt(latest_failure.command, limit=160)}",
                    *(
                        (f"Result: {latest_failure.result}",)
                        if latest_failure.result
                        else ()
                    ),
                ),
            )
        )
    if latest_check and latest_check.outcome == "unknown":
        insights.append(
            SessionInsight(
                "Check outcome unknown",
                "info",
                (
                    "A check command has no single unambiguous recorded result. "
                    "Helios cannot claim that it passed or is still running."
                ),
                (
                    _scope_evidence(latest_check.scope),
                    (
                        f"{latest_check.unknown_reason}: "
                        f"{_command_excerpt(latest_check.command, limit=160)}"
                    ),
                ),
            )
        )
    elif latest_check and latest_check.outcome == "passed":
        insights.append(
            SessionInsight(
                "Check command passed",
                "info",
                (
                    "The command returned success. Conversation evidence does "
                    "not establish which file changes or revision it covered."
                ),
                (
                    _scope_evidence(latest_check.scope),
                    f"Passed: {_command_excerpt(latest_check.command, limit=160)}",
                ),
            )
        )

    if recorded_changes:
        count = len(recorded_changes)
        insights.append(
            SessionInsight(
                "Verification not established",
                "warn",
                (
                    f"{count} file{'s were' if count != 1 else ' was'} recorded "
                    "as changed in this request. Passing commands are shown "
                    "separately and do not establish the revision or files "
                    "they covered."
                ),
                _changed_evidence(recorded_changes.values()),
            )
        )

    if not insights:
        insights.append(
            SessionInsight(
                "Current request",
                "info",
                "No build or check issue is recorded in this request.",
            )
        )
    return insights


def _current_request_start(turns: list[Turn]) -> int:
    start = 0
    for index, turn in enumerate(turns):
        if _is_user_request(turn):
            start = index
    return start


def _is_user_request(turn: Turn) -> bool:
    return (
        turn.role == "user"
        and not turn.is_meta
        and not turn.is_sidechain
        and any(span.kind == "text" and span.text.strip() for span in turn.content)
    )


def _historical_tool_uses(turns: list[Turn]) -> dict[str, tuple[str, dict]]:
    return {
        tool.id: (tool.name, tool.input)
        for turn in turns
        for tool in turn.tool_uses
        if tool.id
    }


def _ambiguous_tool_ids(turns: list[Turn]) -> set[str]:
    """Find IDs reused for non-identical calls, which cannot be correlated."""

    signatures: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for turn in turns:
        for tool in turn.tool_uses:
            if not tool.id:
                continue
            signature = (tool.name, tool.input)
            if not any(signature == existing for existing in signatures[tool.id]):
                signatures[tool.id].append(signature)
    return {
        tool_id
        for tool_id, unique_signatures in signatures.items()
        if len(unique_signatures) > 1
    }


def _results_by_id(turns: list[Turn]) -> dict[str, list[ToolResult]]:
    results: dict[str, list[ToolResult]] = defaultdict(list)
    for turn in turns:
        for result in turn.tool_results:
            if result.tool_use_id:
                results[result.tool_use_id].append(result)
    return results


def _take_result(
    results_by_id: dict[str, list[ToolResult]], tool: ToolUse
) -> _ObservedResult:
    if not tool.id or not results_by_id[tool.id]:
        return _ObservedResult("missing")
    unique_results = {
        (result.content, result.is_error): result
        for result in results_by_id[tool.id]
    }
    if len(unique_results) != 1:
        return _ObservedResult("conflicted")
    return _ObservedResult("recorded", next(iter(unique_results.values())))


def _is_build_tool(tool: ToolUse) -> bool:
    return tool.name.lower() in BUILD_TOOLS


def _change_paths(tool: ToolUse) -> tuple[str, ...]:
    primary = (
        tool.input.get("file_path")
        or tool.input.get("path")
        or tool.input.get("notebook_path")
        or ""
    )
    raw_paths: list[object] = [primary]
    additional = tool.input.get("additional_files")
    if isinstance(additional, (list, tuple)):
        raw_paths.extend(additional)
    return tuple(dict.fromkeys(str(path) for path in raw_paths if str(path).strip()))


def _change_scope(tool: ToolUse, default_scope: str) -> str:
    return _normalize_scope(str(tool.input.get("cwd") or "")) or default_scope


def _check_script(tool: ToolUse) -> str | None:
    if tool.name.lower() != "bash":
        return None
    command = _shell_script(str(tool.input.get("command") or ""))
    command = command.replace("\\\n", " ").strip()
    if "\n" in command or ";" in command or "|" in command:
        return None
    if re.search(r"(?:^|\s)&(?:\s|$)", command):
        return None
    leading_cd = _leading_cd(command)
    candidate = leading_cd[1] if leading_cd else command
    if _is_non_verifying_mode(candidate):
        return None
    return candidate if any(p.search(candidate) for p in _VERIFY_PATTERNS) else None


def _shell_script(command: str) -> str:
    """Unwrap a direct ``bash -lc '...'`` invocation when unambiguous."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if (
        len(tokens) == 3
        and ospath.basename(tokens[0]) in {"bash", "sh", "zsh"}
        and tokens[1] in {"-c", "-lc"}
    ):
        return tokens[2]
    return command


def _leading_cd(command: str) -> tuple[str, str] | None:
    match = _LEADING_CD.fullmatch(command.strip())
    if match is None:
        return None
    try:
        target_parts = shlex.split(match.group("target"))
    except ValueError:
        return None
    if len(target_parts) != 1:
        return None
    target = target_parts[0]
    if (
        target == "-"
        or target.startswith("~")
        or any(character in target for character in _STATIC_CD_FORBIDDEN)
        or (
            not ospath.isabs(target)
            and target not in {".", ".."}
            and not target.startswith(("./", "../"))
        )
    ):
        return None
    return target, match.group("remainder").strip()


def _is_non_verifying_mode(command: str) -> bool:
    """Reject known masking, listing, collection-only, and dry-run modes."""

    if any(marker in command for marker in ("$", "`", "<(", ">(", "{", "}")):
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True
    options = {token.split("=", 1)[0].lower() for token in tokens}
    for token in tokens:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            value = token.split("=", 1)[1]
            options.update(
                part.split("=", 1)[0].lower() for part in value.split()
            )
    if options & _NON_VERIFY_OPTIONS or any(
        token.lower().startswith("--help") for token in tokens
    ):
        return True

    executable = _first_executable(tokens)
    if executable == "ctest" and any(
        token.startswith("-")
        and not token.startswith("--")
        and "N" in token[1:]
        for token in tokens
    ):
        return True
    if executable == "nox" and (
        "--json" in options
        or any(
            re.fullmatch(r"-[A-Za-z]+", token) and "l" in token[1:].lower()
            for token in tokens
        )
    ):
        return True
    if executable == "tox" and (
        any(
            token.lower()
            in {"config", "devenv", "exec", "list", "provision", "quickstart"}
            for token in tokens
        )
        or any(
            re.fullmatch(r"-[A-Za-z]+", token)
            and {"a", "l"} & set(token[1:].lower())
            for token in tokens
        )
    ):
        return True
    if executable == "go" and (
        options & {"-exec", "-list"}
        or any(token.lower() == "-count=0" for token in tokens)
        or any(
            token.lower() == "-count"
            and index + 1 < len(tokens)
            and tokens[index + 1] == "0"
            for index, token in enumerate(tokens)
        )
    ):
        return True
    if executable in {"make", "gmake"} and options & {
        "--dry-run",
        "--just-print",
        "--question",
        "--recon",
        "--touch",
        "-n",
        "-q",
        "-t",
    }:
        return True
    if executable in {"make", "gmake"} and any(
        re.fullmatch(r"-[A-Za-z]+", token)
        and {"n", "q", "t"} & set(token[1:].lower())
        for token in tokens
    ):
        return True
    if executable in {"make", "gmake"} and any(
        token.upper().startswith("MAKEFLAGS=")
        and any(
            re.fullmatch(r"-?[A-Za-z]+", part)
            and {"n", "q", "t"} & set(part.lstrip("-").lower())
            for part in token.split("=", 1)[1].split()
        )
        for token in tokens
    ):
        return True
    if executable == "ruff" and "-e" in tokens:
        return True
    if executable == "shellcheck" and "-V" in tokens:
        return True
    if executable in {"npm", "pnpm", "yarn"} and any(
        token.upper().startswith("NPM_CONFIG_IGNORE_SCRIPTS=")
        and token.split("=", 1)[1].strip().lower() in {"1", "on", "true", "yes"}
        for token in tokens
    ):
        return True
    return False


def _first_executable(tokens: list[str]) -> str:
    index = 0
    if tokens and tokens[0] == "env":
        index += 1
    while index < len(tokens) and re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]
    ):
        index += 1
    if (
        index + 1 < len(tokens)
        and tokens[index] in {"uv", "poetry"}
        and tokens[index + 1] == "run"
    ):
        index += 2
    if index >= len(tokens):
        return ""
    executable = ospath.basename(tokens[index]).lower()
    if (
        re.fullmatch(r"python\d*(?:\.\d+)?", executable)
        and index + 2 < len(tokens)
        and tokens[index + 1] == "-m"
    ):
        return tokens[index + 2].lower()
    return executable


def _display_scope(tool: ToolUse, default_scope: str) -> str:
    """Return cwd as evidence only; it never certifies file coverage."""

    cwd = _normalize_scope(str(tool.input.get("cwd") or "")) or default_scope
    leading_cd = _leading_cd(_shell_script(str(tool.input.get("command") or "")))
    if leading_cd is None:
        return cwd
    target = ospath.expanduser(leading_cd[0])
    if ospath.isabs(target):
        return _normalize_scope(target)
    return _normalize_scope(ospath.join(cwd, target)) if cwd else ""


def _normalize_scope(scope: str) -> str:
    return ospath.normpath(scope) if scope.strip() else ""


def _scope_evidence(scope: str) -> str:
    return (
        f"Recorded cwd: {scope}"
        if scope
        else "Recorded cwd: unavailable"
    )


def _change_result_evidence(
    change: _ChangeObservation, label: str, result: str = ""
) -> str:
    scope = f" · cwd {change.scope}" if change.scope else " · cwd unavailable"
    suffix = f" · {result}" if result else ""
    return f"{label}: {change.path}{scope}{suffix}"


def _changed_evidence(
    changes: Iterable[_ChangeObservation],
) -> tuple[str, ...]:
    ordered = sorted(changes, key=lambda change: (change.path, change.scope))
    evidence = [
        _change_result_evidence(change, "Recorded change")
        for change in ordered[:5]
    ]
    if len(ordered) > 5:
        evidence.append(f"And {len(ordered) - 5} more files")
    return tuple(evidence)


def _compact(text: str, *, limit: int = 100) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def _command_excerpt(text: str, *, limit: int = 160) -> str:
    """Truncate command evidence without changing its shell arguments."""

    excerpt = text.strip().replace("\r", r"\r").replace("\n", r"\n")
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 3] + "..."


def _failure_excerpt(text: str) -> str:
    """Prefer actionable failure lines over repeated host/sandbox preamble."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    meaningful = [
        line
        for line in lines
        if line != "Failed to create stream fd: Operation not permitted"
    ]
    for line in reversed(meaningful):
        if re.search(r"\b\d+\s+(?:failed|errors?)\b", line, re.IGNORECASE):
            return _compact(line)
    pytest_failures = [
        line for line in meaningful if re.match(r"^(?:FAILED|ERROR)\s+", line)
    ]
    if pytest_failures:
        return _compact("; ".join(pytest_failures[-2:]))
    if meaningful:
        return _compact(meaningful[-1])
    return _compact(text)
