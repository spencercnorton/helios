"""Helios-executed tools for the OpenRouter chat driver.

Unlike the Claude/Codex providers — whose CLIs execute tools internally —
an OpenRouter session is driven straight from Helios: the model streams
OpenAI-style ``tool_calls`` and Helios executes them here. Tool names use the
Claude vocabulary (``Read``/``Write``/``Edit``/``Bash``/``Grep``/``Glob``) so
the existing activity indicator, transcript bubbles, and transcript mirror
render them without special cases.

Permission gating mirrors the CLI behavior per mode (see project_perms):

* read-only tools (Read/Grep/Glob) *inside the working directory* never prompt;
* ``acceptEdits``/``auto`` auto-approve Write/Edit inside it but ask for Bash;
* ``default`` asks for anything that mutates;
* ``plan``/``dontAsk`` deny anything that would need a prompt;
* ``bypassPermissions`` runs everything, inside or outside the workspace — the
  same contract Claude's Bypass has; the driver only sees it once
  ``effective_execution_mode`` has admitted it for this provider;
* retired/unknown modes narrow to the ordinary ask posture.

Gating is workspace-scoped, not just tool-scoped. A path argument that resolves
outside the working directory forfeits its read-only standing and is judged as
if it mutated: otherwise ``plan`` and ``dontAsk`` — the two modes chosen *for*
safety — would silently auto-approve reading ``~/.ssh/id_rsa`` into the prompt
and shipping it to whichever model the user selected, and ``acceptEdits`` would
auto-approve writing outside the project.

Bash runs with the standard scrubbed child environment (env_scrub) and a
process group so timeouts or Work cancellation reap the whole tree. Bash is never treated as
read-only in any mode, so it needs no separate containment rule. There is
deliberately no filesystem sandbox — same posture as the provider CLIs under
the same modes.

GTK-free so the driver thread and CI (python-slim) can use it.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import os
import re
import signal
import stat
import subprocess
import uuid
from pathlib import Path

from helios.backend.openrouter.gateway import CancellationToken
from helios.backend.process.env_scrub import (
    NORVI_TRACKER_ENV,
    scrubbed_child_env,
)
from helios.backend.sensitive_text import scrub_sensitive
from helios.log import get_logger

_log = get_logger("openrouter-tools")

__all__ = [
    "EDIT_TOOLS",
    "PLAN_TOOLS",
    "normalize_plan",
    "READ_ONLY_TOOLS",
    "TOOL_SCHEMAS",
    "approval_summary",
    "decide",
    "execute_tool",
    "outside_target",
    "touches_outside_workspace",
]

READ_ONLY_TOOLS = frozenset({"Read", "Grep", "Glob"})
EDIT_TOOLS = frozenset({"Write", "Edit"})
#: Proposes a plan and hands control back. Mutates nothing, so it is allowed in
#: every mode — including ``plan``, where it is the only way out. Without it
#: plan mode is enforcement-only: the model is denied every mutating tool and
#: has no way to say what it would have done.
PLAN_TOOLS = frozenset({"ExitPlanMode", "update_plan", "read_context", "checkpoint_context"})

_AUTO = "auto"
_ASK = "ask"
_DENY = "deny"

_READ_MAX_LINES = 2000
_READ_DEFAULT_LINES = 200
_READ_MAX_CHARS = 100_000
_OUTPUT_HEAD = 20_000
_OUTPUT_TAIL = 10_000
_GREP_MAX_MATCHES = 100
_GLOB_MAX_RESULTS = 200
_BASH_DEFAULT_TIMEOUT = 120.0
_BASH_MAX_TIMEOUT = 600.0
_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})

_DENIAL_PLAN = "Plan mode is read-only; {name} would make changes."
_DENIAL_DONTASK = "Never-ask mode refuses actions that would need approval."
_DENIAL_OUTSIDE = (
    "{name} targets a path outside the working directory, which this "
    "permission mode will not approve without asking."
)

# Path argument each tool reads, in the order it is consulted.
_PATH_ARGS: dict[str, tuple[str, ...]] = {
    "Read": ("path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "Grep": ("path",),
    "Glob": ("path",),
}


# ── permission decision ────────────────────────────────────────────────────


def decide(
    tool_name: str,
    permission_mode: str,
    *,
    outside_workspace: bool = False,
) -> str:
    """Map (tool, mode, location) to auto / ask / deny. Unknown tools ask.

    ``outside_workspace`` strips a tool of its read-only/auto-editable standing
    for this call — see the module docstring.
    """
    if tool_name in PLAN_TOOLS or permission_mode == "bypassPermissions":
        return _AUTO
    read_only = tool_name in READ_ONLY_TOOLS and not outside_workspace
    if permission_mode in ("plan", "dontAsk"):
        return _AUTO if read_only else _DENY
    if permission_mode in ("acceptEdits", "auto"):
        auto_edit = tool_name in EDIT_TOOLS and not outside_workspace
        return _AUTO if read_only or auto_edit else _ASK
    # default / anything unknown: ordinary ask posture.
    return _AUTO if read_only else _ASK


def denial_message(
    tool_name: str,
    permission_mode: str,
    *,
    outside_workspace: bool = False,
) -> str:
    if outside_workspace:
        return _DENIAL_OUTSIDE.format(name=tool_name)
    if permission_mode == "plan":
        return _DENIAL_PLAN.format(name=tool_name)
    return _DENIAL_DONTASK.format(name=tool_name)


def _contains(root: Path, target: Path) -> bool:
    """True when ``target`` is ``root`` itself or lies beneath it."""
    return target == root or root in target.parents


def outside_target(tool_name: str, arguments: dict, cwd: str) -> str:
    """The first resolved path argument lying outside ``cwd``, else ``""``.

    Symlinks are followed on both sides before comparison (``_resolve`` and the
    root both call ``resolve()``), so a symlink inside the workspace pointing
    out of it is correctly reported.

    Fails closed and never raises. An unresolvable or malformed path counts as
    outside — the caller then asks instead of auto-running, and the tool itself
    reports the real error. The catch is deliberately broad: ``expanduser()``
    raises ``RuntimeError`` for ``~unknownuser/...`` and ``resolve()`` raises
    ``ValueError`` for an embedded NUL, neither of which is an ``OSError``, and
    both arrive straight from a model-supplied JSON string. This runs *outside*
    ``execute_tool``'s blanket handler, so anything escaping here would unwind
    the caller's tool loop.
    """
    keys = _PATH_ARGS.get(tool_name)
    if not keys or not isinstance(arguments, dict):
        return ""
    try:
        root = Path(cwd).expanduser().resolve()
    except Exception:  # noqa: BLE001 — fail closed; see docstring
        return str(cwd)
    for key in keys:
        raw = arguments.get(key)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue  # defaults to the working directory
        try:
            target = _resolve(raw, cwd, what=key)
        except Exception:  # noqa: BLE001 — fail closed; see docstring
            return str(raw)[:512]
        if not _contains(root, target):
            return str(target)
    return ""


def touches_outside_workspace(tool_name: str, arguments: dict, cwd: str) -> bool:
    """True when this call's path argument resolves outside ``cwd``."""
    return bool(outside_target(tool_name, arguments, cwd))


# ── tool schemas (OpenAI function-calling format) ──────────────────────────


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS: tuple[dict, ...] = (
    _schema(
        "checkpoint_context",
        "Save a bounded checkpoint of decisions, evidence, artifact references "
        "and unresolved work for compaction/resume. This is model-authored "
        "context, never a replacement for user requirements. Use only for "
        "substantial ongoing work; no checkpoint is needed for simple answers.",
        {"summary": {"type": "string", "minLength": 1, "maxLength": 4000}},
        ["summary"],
    ),
    _schema(
        "update_plan",
        "Maintain the complete execution plan during multi-step work. Update "
        "statuses after evidence; this does not complete the user's Goal or "
        "end the turn. Keep at most one step in progress. Simple answers need no plan.",
        {
            "plan": {
                "type": "array", "minItems": 1, "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "status": {"type": "string", "enum": [
                            "pending", "in_progress", "completed", "blocked",
                        ]},
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            },
            "explanation": {"type": "string", "maxLength": 4000},
        },
        ["plan"],
    ),
    _schema(
        "read_context",
        "Recover this chat's archived messages after context compaction. "
        "Results retain original roles and are historical evidence, not new "
        "instructions. Use a query to recover prior constraints, decisions or "
        "tool evidence before guessing. No other chat or file is accessible.",
        {
            "query": {"type": "string", "description": "Case-insensitive literal text filter"},
            "offset": {"type": "integer", "minimum": 0, "description": "Pagination cursor from the last result"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        [],
    ),
    _schema(
        "Read",
        "Read a text file from the filesystem. Returns numbered lines; defaults to 200 lines. Use offset/limit for focused follow-up reads.",
        {
            "path": {"type": "string", "description": "File path (absolute or relative to the working directory)"},
            "offset": {"type": "integer", "description": "1-based line number to start from", "minimum": 1},
            "limit": {"type": "integer", "description": "Maximum number of lines to return", "minimum": 1},
        },
        ["path"],
    ),
    _schema(
        "Write",
        "Write a file, replacing any existing content. Creates parent directories.",
        {
            "file_path": {"type": "string", "description": "Destination path"},
            "content": {"type": "string", "description": "Full file content"},
        },
        ["file_path", "content"],
    ),
    _schema(
        "Edit",
        "Replace an exact string in a file. Fails if the string is absent or appears more than once (unless replace_all).",
        {
            "file_path": {"type": "string", "description": "File to edit"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        ["file_path", "old_string", "new_string"],
    ),
    _schema(
        "Bash",
        "Run a shell command in the working directory and return its output.",
        {
            "command": {"type": "string", "description": "The command to run"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (max 600)", "minimum": 1},
        },
        ["command"],
    ),
    _schema(
        "Grep",
        "Search file contents with a regular expression. Returns path:line:content matches.",
        {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
            "glob": {"type": "string", "description": "Only search files matching this pattern, e.g. *.py"},
        },
        ["pattern"],
    ),
    _schema(
        "ExitPlanMode",
        "Present your implementation plan and hand control back to the user. "
        "Use this when you have finished researching in plan mode. It changes "
        "nothing on disk; the user decides whether to proceed and switches "
        "modes themselves.",
        {
            "plan": {
                "type": "array",
                "description": "Ordered steps you propose to take.",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "description": "One concrete step"},
                    },
                    "required": ["step"],
                    "additionalProperties": False,
                },
            },
            "explanation": {
                "type": "string",
                "description": "Short summary of the approach and its risks.",
            },
        },
        ["plan"],
    ),
    _schema(
        "Glob",
        "Find files by name pattern, recursively.",
        {
            "pattern": {"type": "string", "description": "Filename pattern, e.g. *.py"},
            "path": {"type": "string", "description": "Directory to search (default: working directory)"},
        },
        ["pattern"],
    ),
)


# ── execution ──────────────────────────────────────────────────────────────


def execute_tool(
    tool_name: str,
    arguments: dict,
    *,
    cwd: str,
    cancellation: CancellationToken | None = None,
) -> tuple[str, bool]:
    """Run one approved tool call. Returns (content, is_error). Never raises."""
    try:
        if cancellation is not None and cancellation.cancelled:
            raise ToolFailed("Tool execution cancelled by the user.")
        if tool_name == "Read":
            return _run_read(arguments, cwd), False
        if tool_name == "Write":
            return _run_write(arguments, cwd), False
        if tool_name == "Edit":
            return _run_edit(arguments, cwd), False
        if tool_name == "Bash":
            return _run_bash(arguments, cwd, cancellation=cancellation), False
        if tool_name == "Grep":
            return _run_grep(arguments, cwd), False
        if tool_name == "Glob":
            return _run_glob(arguments, cwd), False
        if tool_name == "ExitPlanMode":
            return _run_exit_plan_mode(arguments), False
        if tool_name == "update_plan":
            payload = normalize_execution_plan(arguments)
            return f"Execution plan updated ({len(payload['plan'])} steps).", False
        return f"Unknown tool: {tool_name}", True
    except ToolFailed as e:
        return str(e), True
    except Exception as e:  # noqa: BLE001 — a tool must never take down a turn
        return f"{tool_name} failed: {e}", True


class ToolFailed(Exception):
    pass


def _resolve(raw: object, cwd: str, *, what: str = "path") -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolFailed(f"missing or invalid {what}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    return path.resolve()


def _elide(text: str, head: int = _OUTPUT_HEAD, tail: int = _OUTPUT_TAIL) -> str:
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n\n… [Helios truncated {dropped} chars] …\n\n{text[-tail:]}"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _open_unique_temp(directory: Path, stem: str, mode: int) -> tuple[int, Path]:
    """A uniquely named temp file, created at ``mode`` with the umask applied.

    ``tempfile.mkstemp`` would do the unique-name and ``O_EXCL`` half — the
    property that closed the planted-temp-symlink vector in v0.89.5 — but it
    hard-codes 0600, and reading the umask to correct that afterwards meant
    calling ``os.umask`` twice, which is process-global under worker threads
. ``O_CREAT | O_EXCL`` with an explicit mode lets the
    kernel apply the real umask itself.

    The caller decides ``mode``, because the safe value differs: **0600 when a
    destination already exists**, so a private file's contents are never
    momentarily world-readable in the temp file (round 10 — measured, a 0600
    file being rewritten under a 0022 umask produced a 0644/0664 temp), and
    0666 only for a genuinely new file, where the umask alone should decide.
    """
    for _attempt in range(8):
        candidate = directory / f".{stem}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(
                str(candidate),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                mode,
            )
        except FileExistsError:
            continue
        return fd, candidate
    raise ToolFailed("could not create a unique temporary file")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Read the destination's mode BEFORE creating the temp file: an existing
    # file's contents must never be exposed more widely than the file itself,
    # not even for the length of one write.
    try:
        existing: int | None = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        existing = None
    fd, tmp = _open_unique_temp(
        path.parent, path.name, 0o600 if existing is not None else 0o666
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        # os.replace does not carry the destination's mode across. Measured
        # (F-8, 2026-09-03): 0644 -> 0600 and 0755 -> 0600 — a script silently
        # stopped being executable. An existing destination keeps its exact
        # mode; a brand-new one keeps the umask-derived mode the kernel gave
        # the temp file, which is what open(path, "w") would have produced.
        if existing is not None:
            os.chmod(tmp, existing)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _run_read(arguments: dict, cwd: str) -> str:
    path = _resolve(arguments.get("path"), cwd)
    if not path.is_file():
        raise ToolFailed(f"not a file: {path}")
    text = _read_text(path)
    lines = text.splitlines()
    offset = arguments.get("offset")
    limit = arguments.get("limit")
    start = max(1, offset) - 1 if type(offset) is int else 0
    count = max(1, min(limit, _READ_MAX_LINES)) if type(limit) is int else _READ_DEFAULT_LINES
    end = start + count
    chunk = lines[start:end]
    body = "\n".join(f"{start + i + 1}: {line}" for i, line in enumerate(chunk))
    if len(body) > _READ_MAX_CHARS:
        body = body[:_READ_MAX_CHARS] + "\n… [truncated]"
    if start > 0 or end < len(lines):
        body += f"\n[showing lines {start + 1}-{start + len(chunk)} of {len(lines)}]"
    return body or "(empty file)"


def _run_write(arguments: dict, cwd: str) -> str:
    path = _resolve(arguments.get("file_path"), cwd, what="file_path")
    content = arguments.get("content")
    if not isinstance(content, str):
        raise ToolFailed("missing or invalid content")
    _atomic_write(path, content)
    return f"Wrote {len(content)} chars to {path}"


def _run_edit(arguments: dict, cwd: str) -> str:
    path = _resolve(arguments.get("file_path"), cwd, what="file_path")
    if not path.is_file():
        raise ToolFailed(f"not a file: {path}")
    old = arguments.get("old_string")
    new = arguments.get("new_string")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise ToolFailed("missing or invalid old_string/new_string")
    text = _read_text(path)
    count = text.count(old)
    if count == 0:
        raise ToolFailed("old_string not found in file")
    replace_all = arguments.get("replace_all") is True
    if count > 1 and not replace_all:
        raise ToolFailed(f"old_string appears {count} times; pass replace_all or a more specific string")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    _atomic_write(path, updated)
    return f"Edited {path} ({count if replace_all else 1} replacement{'s' if replace_all and count > 1 else ''})"


def _run_bash(
    arguments: dict,
    cwd: str,
    *,
    cancellation: CancellationToken | None = None,
) -> str:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolFailed("missing or invalid command")
    timeout = arguments.get("timeout")
    seconds = min(float(timeout), _BASH_MAX_TIMEOUT) if type(timeout) in (int, float) else _BASH_DEFAULT_TIMEOUT
    process = subprocess.Popen(
        ["/bin/bash", "-c", command],
        cwd=cwd,
        # Same grant as the Claude and Codex sessions: this Bash tool is how an
        # OpenRouter session reaches `norvi-work`.
        env=scrubbed_child_env(keep=NORVI_TRACKER_ENV),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    timed_out = False
    cancelled = False

    def kill_process_group() -> None:
        nonlocal cancelled
        cancelled = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    remove_cancel = (
        cancellation.add_callback(kill_process_group)
        if cancellation is not None
        else None
    )
    try:
        try:
            output, _ = process.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_group()
            # Timeout is not a user cancellation even though the same group
            # kill helper performs the termination.
            cancelled = bool(cancellation is not None and cancellation.cancelled)
            output, _ = process.communicate()
    finally:
        if callable(remove_cancel):
            remove_cancel()
    if cancelled:
        raise ToolFailed("Bash cancelled by the user; its process group was killed.")
    text = output.decode("utf-8", errors="replace")
    parts = [_elide(text.rstrip("\n"))] if text.strip() else []
    if timed_out:
        parts.append(f"[killed after {int(seconds)}s timeout]")
    parts.append(f"[exit code {process.returncode}]")
    return "\n".join(parts) or "(no output)"


def normalize_plan(arguments: dict) -> dict:
    """Coerce an ExitPlanMode call into the plan payload the UI consumes.

    Shaped to match Codex's ``turn/plan/updated`` so ``PlanPane`` renders it
    through the same path rather than growing a second plan format. Steps
    arrive as ``{"step": ...}`` and every one starts pending — nothing has been
    done yet, which is the entire point of proposing rather than acting.
    """
    raw = arguments.get("plan")
    steps: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        text = ""
        if isinstance(item, dict):
            text = str(item.get("step") or "").strip()
        elif isinstance(item, str):
            text = item.strip()
        if text:
            steps.append({"step": text[:1000], "status": "pending"})
    explanation = arguments.get("explanation")
    return {
        "plan": steps[:50],
        "explanation": str(explanation)[:4000] if isinstance(explanation, str) else "",
        "source": "turn",
    }


def _run_exit_plan_mode(arguments: dict) -> str:
    payload = normalize_plan(arguments)
    if not payload["plan"]:
        raise ToolFailed("ExitPlanMode requires at least one plan step")
    return (
        f"Plan presented to the user ({len(payload['plan'])} step"
        f"{'s' if len(payload['plan']) != 1 else ''}). "
        "Stop here and wait — the user decides whether to proceed and will "
        "change the permission mode themselves. Do not continue working."
    )


def normalize_execution_plan(arguments: dict) -> dict:
    """Validate a full progress snapshot without silently inventing statuses."""
    raw = arguments.get("plan")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
        raise ToolFailed("update_plan requires 1–50 steps")
    steps = []
    for item in raw:
        if not isinstance(item, dict):
            raise ToolFailed("Each plan step requires step text and a status")
        text = item.get("step")
        status = item.get("status")
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ToolFailed("Plan step text must contain 1–1000 characters")
        if status not in {"pending", "in_progress", "inProgress", "completed", "blocked"}:
            raise ToolFailed("Unknown execution plan status")
        steps.append({"step": text.strip(), "status": "inProgress" if status == "in_progress" else status})
    if sum(item["status"] == "inProgress" for item in steps) > 1:
        raise ToolFailed("Keep at most one root plan step in progress")
    explanation = arguments.get("explanation", "")
    if not isinstance(explanation, str) or len(explanation) > 4000:
        raise ToolFailed("Plan explanation must be text of at most 4000 characters")
    return {"plan": steps, "explanation": explanation, "source": "turn"}


def execution_plan_payload(arguments: dict, *, thread_id: str, turn_id: str, proposal: bool = False) -> dict:
    """Bind an OpenRouter plan to the provider identity admitted by the store."""
    if not thread_id or not turn_id:
        raise ToolFailed("Cannot publish a plan before the provider turn identity is confirmed")
    payload = normalize_plan(arguments) if proposal else normalize_execution_plan(arguments)
    if not payload["plan"]:
        raise ToolFailed("A plan requires at least one step")
    return {**payload, "threadId": thread_id, "turnId": turn_id, "authoritative": True}


def _iter_files(root: Path):
    """Yield files under ``root``, never escaping it.

    ``os.walk`` will not descend a symlinked *directory*, but it still lists
    symlinked *files*, and ``_read_text`` follows them. Without the containment
    check below, a link inside the tree would let ``Grep``/``Glob`` — judged
    in-workspace on their root argument alone, so auto-approved in every mode —
    return the contents of arbitrary files outside it.
    """
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            try:
                if not _contains(root, path.resolve()):
                    continue
            except Exception:  # noqa: BLE001 — unresolvable entry is skipped
                continue
            yield path


@functools.lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path-glob into a regex with `pathlib.full_match` semantics.

    Hand-rolled rather than delegating to ``PurePath.full_match``, which is
    3.13+ while this package declares a 3.11 floor. Delegating and falling
    back to ``fnmatch`` was tried and is wrong, not merely looser: ``fnmatch``
    requires a literal "/" for the "**/" in ``**/*.py``, so a top-level
    ``b.py`` stopped matching — *worse* than the basename matching this
    replaced, on the single most common pattern. Caught by CI, whose
    `gtk_tests` job runs on ubuntu:24.04 (Python 3.12) while the other jobs
    and both developer machines are 3.13+. One implementation on every
    interpreter is the only way the behaviour is the same on all of them;
    `test_openrouter_tool_semantics` proves it equals ``full_match`` wherever
    ``full_match`` exists.

    * ``*`` and ``?`` never cross a separator.
    * ``**`` is only special as a whole segment, and matches zero or more
      segments — so ``**/*.py`` matches ``b.py`` as well as ``src/a.py``, and
      ``src/**/*.py`` matches ``src/a.py``.
    """
    parts = pattern.split("/")
    last = len(parts) - 1
    out: list[str] = []
    for index, part in enumerate(parts):
        if part == "**":
            out.append("(?:[^/]+(?:/[^/]+)*)?" if index == last else "(?:[^/]+/)*")
            continue
        out.append(_glob_segment(part))
        if index != last:
            out.append("/")
    source = "".join(out) + r"\Z"
    try:
        return re.compile(source)
    except re.error:
        # A pattern this translator cannot express safely must not become a
        # tool failure the model has to interpret. Fall back to matching the
        # pattern literally: wrong, but bounded, reported as "no match", and
        # impossible to raise.
        _log.warning("unusable glob pattern %r; matching it literally", pattern)
        return re.compile(re.escape(pattern) + r"\Z")


def _glob_class(body: str) -> str:
    """One glob character class as a regex class, and never as a syntax error.

    The contents used to be spliced into the regex after only translating a
    leading ``!``. That let the model's own pattern decide whether the regex
    compiled: ``[z-a]`` raised ``re.PatternError`` straight out of the tool
    (measured — ``Glob "src/[z-a].py"`` returned "bad character range"), and
    ``[[:alpha:]]`` produced a nested-set ``FutureWarning`` that later Pythons
    make an error.

    Every member is escaped, and a range is emitted as a range only when it is
    a well-formed ascending one — otherwise its three characters are literals,
    which is what a shell does with a reversed range anyway.
    """
    out: list[str] = ["["]
    index = 0
    if body[:1] in ("!", "^"):
        out.append("^")
        index = 1
    if body[index : index + 1] == "]":  # a leading ] is a literal member
        out.append(re.escape("]"))
        index += 1
    while index < len(body):
        char = body[index]
        is_range = (
            char != "-"
            and body[index + 1 : index + 2] == "-"
            and index + 2 < len(body)
            and body[index + 2] != "]"
        )
        if is_range:
            lo, hi = char, body[index + 2]
            if ord(lo) <= ord(hi):
                out.append(f"{re.escape(lo)}-{re.escape(hi)}")
            else:
                # Reversed range: literal members, not a compile error.
                out.extend((re.escape(lo), re.escape("-"), re.escape(hi)))
            index += 3
            continue
        out.append(re.escape(char))
        index += 1
    out.append("]")
    return "".join(out)


def _glob_segment(part: str) -> str:
    """One path segment of a glob as a regex; nothing here matches "/"."""
    out: list[str] = []
    index = 0
    while index < len(part):
        char = part[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            close = index + 1
            if close < len(part) and part[close] in "!^":
                close += 1
            if close < len(part) and part[close] == "]":
                close += 1
            while close < len(part) and part[close] != "]":
                close += 1
            if close >= len(part):
                out.append(re.escape("["))  # unterminated class is a literal
            else:
                body = part[index + 1 : close]
                # An empty class matches nothing and is not valid regex.
                out.append(_glob_class(body) if body else re.escape("[]"))
                index = close + 1
                continue
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _glob_match(file_path: Path, root: Path, pattern: str) -> bool:
    """True when ``pattern`` matches ``file_path``, found under ``root``.

    A pattern with no "/" matches the basename only, at any depth — the
    existing, useful behaviour that lets a bare ``*.py`` find ``src/a.py``.
    Several tests already pin this and it must not regress. A pattern
    containing "/" instead matches the POSIX-style path relative to ``root``.

    F-9, measured 2026-09-03: ``fnmatch(file_path.name, pattern)`` silently
    dropped everything left of the last "/" in the pattern, so ``Glob
    "**/*.py"``, ``Glob "src/*.py"`` and the Grep ``glob:`` filter all
    returned "no matches" against a workspace that plainly contained
    ``src/a.py`` — a silent wrong answer, the worst failure a tool can have.
    """
    if "/" not in pattern:
        return fnmatch.fnmatch(file_path.name, pattern)
    try:
        rel = file_path.relative_to(root)
    except ValueError:
        return False
    return _glob_regex(pattern).match(rel.as_posix()) is not None


def _run_grep(arguments: dict, cwd: str) -> str:
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolFailed("missing or invalid pattern")
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolFailed(f"invalid regex: {e}") from e
    root = _resolve(arguments.get("path") or ".", cwd)
    if not root.exists():
        raise ToolFailed(f"no such path: {root}")
    glob_pat = arguments.get("glob")
    glob_pat = glob_pat if isinstance(glob_pat, str) and glob_pat else None
    matches: list[str] = []
    truncated = False
    for file_path in _iter_files(root):
        if glob_pat and not _glob_match(file_path, root, glob_pat):
            continue
        try:
            lines = _read_text(file_path).splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            if regex.search(line):
                matches.append(f"{file_path}:{lineno}:{line[:500]}")
                if len(matches) >= _GREP_MAX_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    if truncated:
        matches.append(f"[truncated at {_GREP_MAX_MATCHES} matches]")
    return "\n".join(matches) or "(no matches)"


def _run_glob(arguments: dict, cwd: str) -> str:
    pattern = arguments.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolFailed("missing or invalid pattern")
    root = _resolve(arguments.get("path") or ".", cwd)
    if not root.is_dir():
        raise ToolFailed(f"not a directory: {root}")
    found: list[str] = []
    for file_path in _iter_files(root):
        if _glob_match(file_path, root, pattern):
            found.append(str(file_path))
            if len(found) >= _GLOB_MAX_RESULTS:
                found.append(f"[truncated at {_GLOB_MAX_RESULTS} results]")
                break
    return "\n".join(found) or "(no files matched)"


# ── approval prompt text (mirrors cli_driver._tool_approval_summary) ──────

_REDACTED = "[redacted]"
_APPROVAL_TEXT_LIMIT = 1200


def _truncate(text: str, limit: int = _APPROVAL_TEXT_LIMIT) -> str:
    """Cut ``text`` to ``limit`` chars, but say so.

    F-11, measured 2026-09-03: the previous silent ``clean[:1200]`` could cut
    a long Bash command's dangerous tail (or a long path, or the JSON
    fallback) out of the approval dialog with nothing telling the user
    anything was missing — an approval granted on an incomplete view of the
    action.
    """
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}… [+{dropped:,} chars not shown]"


def approval_summary(
    tool_name: str,
    raw_input: object,
    *,
    agent_label: str = "OpenRouter",
    outside_path: str = "",
    destination: str = "",
) -> str:
    """Describe the exact action without echoing recognizable secrets.

    When ``outside_path`` is set, the prompt says so and shows the *resolved*
    destination. Escalating to a prompt is the whole mitigation for an
    out-of-workspace read or write, and it is worth nothing if the dialog looks
    identical to an in-project one — the raw argument can be a relative path or
    a symlink that hides where the write actually lands.

    When ``destination`` is set (F-14), the prompt also discloses — on its
    own line — that this tool's *output*, not just its input, is about to
    leave the machine for that destination. OpenRouter is the one Helios
    provider that ships conversation text and every tool result to a third
    party by design, chosen per session from whichever model the user
    picked; the approval dialog is the one place that choice is made, so it
    is the one place this fact belongs.
    """
    name = str(tool_name or "this tool")
    summary = f"Allow {agent_label} to use {name}?"
    if destination:
        summary += (
            f"\n\n⚠ {name}'s output — not just this request — leaves this machine."
            f"\nSent to: {destination}"
        )
    if outside_path:
        clean_target, _changed = scrub_sensitive(str(outside_path))
        summary += (
            "\n\n⚠ This path is OUTSIDE the working directory."
            f"\nResolves to: {_truncate(clean_target)}"
        )
    if not isinstance(raw_input, dict):
        return summary
    detail_keys = (
        ("command", "Command"),
        ("file_path", "Path"),
        ("path", "Path"),
        ("url", "URL"),
        ("query", "Query"),
        ("pattern", "Pattern"),
    )
    details: list[str] = []
    seen: set[str] = set()
    for key, label in detail_keys:
        value = raw_input.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        clean, _changed = scrub_sensitive(value.strip())
        clean = _truncate(clean)
        identity = f"{label}:{clean}"
        if identity in seen:
            continue
        seen.add(identity)
        details.append(f"{label}: {clean}")
    if not details and raw_input:
        try:
            rendered = json.dumps(
                _redact_tool_input(raw_input),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except (TypeError, ValueError):
            rendered = str(raw_input)
        clean_input, _changed = scrub_sensitive(rendered)
        details.append(f"Input: {_truncate(clean_input)}")
    if details:
        summary += "\n\n" + "\n".join(details)
    return summary


def _redact_tool_input(value: object, field_name: str = "") -> object:
    normalized = field_name.lower().replace("-", "_")
    if any(
        marker in normalized
        for marker in ("password", "passwd", "secret", "token", "api_key", "authorization")
    ):
        return _REDACTED
    if isinstance(value, dict):
        return {str(key): _redact_tool_input(item, str(key)) for key, item in list(value.items())[:30]}
    if isinstance(value, list):
        return [_redact_tool_input(item) for item in value[:30]]
    if isinstance(value, str):
        return scrub_sensitive(value)[0]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)
