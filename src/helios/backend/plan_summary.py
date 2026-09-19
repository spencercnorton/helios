"""Session plan extraction for the right-side Plan pane.

The model does not always emit a formal plan. This module therefore prefers
explicit TodoWrite/Todo-list tool calls when present, and otherwise derives a
compact phase map from the tools and streamed blocks already in the transcript.
GTK code consumes the small dataclasses here; tests stay GTK-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Iterable

from helios.backend.execution_plan import ExecutionPlan
from helios.backend.transcript import ToolUse, Turn


PHASES = (
    ("orient", "Orient", "Read the request and gather the right context."),
    ("plan", "Plan", "Choose the work sequence before touching code."),
    ("build", "Build", "Apply the implementation changes."),
    ("verify", "Verify", "Run checks and inspect the result."),
    ("handoff", "Handoff", "Summarize outcome, risks, and next steps."),
)

RESEARCH_TOOLS = {
    "read",
    "grep",
    "glob",
    "ls",
    "webfetch",
    "websearch",
    "mcp__intel__search",
    "mcp__intel__semantic_search",
}
BUILD_TOOLS = {"edit", "multiedit", "write", "notebookedit"}
VERIFY_WORDS = ("test", "pytest", "ruff", "mypy", "compile", "lint", "check")


@dataclass(slots=True)
class PlanStep:
    text: str
    status: str = "pending"  # pending | active | done | blocked | interrupted
    detail: str = ""
    task_id: str = ""


@dataclass(slots=True)
class PlanPhase:
    key: str
    title: str
    description: str
    status: str = "pending"


@dataclass(slots=True)
class PlanSummary:
    phases: list[PlanPhase]
    steps: list[PlanStep] = field(default_factory=list)
    active_detail: str = ""
    source: str = "inferred"
    completed_count: int = 0
    total_count: int = 0
    revision: int = 0
    plan_status: str = ""


_CONTENT_KINDS = ("thinking", "reasoning_summary", "commentary")


def summarize_turns(turns: Iterable[Turn]) -> PlanSummary:
    """Build a PlanSummary for the latest user request in a transcript.

    A transcript can contain many completed requests.  Plan state is
    request-local: an old final answer, TodoWrite list, or verification command
    must not make a new commentary-only request look handed off or resurrect a
    stale plan.  The latest text-bearing user turn is the request boundary;
    assistant/tool-result messages after it belong to the current request.
    """
    current, user_seen = _current_request_turns(list(turns))
    return _summarize_turn_window(current, user_seen=user_seen)


def _current_request_turns(turns: list[Turn]) -> tuple[list[Turn], bool]:
    """Return the latest request window and whether it has real user context."""
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        if turn.role == "user" and turn.text.strip():
            return turns[index:], True
    return turns, False


def _summarize_turn_window(
    turns: Iterable[Turn], *, user_seen: bool
) -> PlanSummary:
    """Summarize one request window shared by live/final/reload paths."""
    steps: list[PlanStep] = []
    seen_tools: list[ToolUse] = []
    has_final_text = False
    has_thinking = False

    for turn in turns:
        if turn.role == "assistant":
            # "assistant_seen" for phase purposes means the assistant produced
            # a FINAL answer (text) — the same rule the live summarizer uses, so
            # a commentary/reasoning-only turn does not prematurely reach
            # plan=done / handoff=active on finalize.
            if turn.text.strip():
                has_final_text = True
            if any(
                s.kind in _CONTENT_KINDS and s.text.strip()
                for s in turn.content
            ):
                has_thinking = True
            explicit = _steps_from_turn(turn)
            if explicit:
                steps = explicit
        seen_tools.extend(turn.tool_uses)

    return _summarize(
        steps,
        seen_tools,
        user_seen=user_seen,
        has_final_text=has_final_text,
        has_thinking=has_thinking,
    )


def summarize_streaming(
    streaming, turns: Iterable[Turn] | None = None
) -> PlanSummary:
    """Build a PlanSummary from a live StreamingAssistant-like object.

    ``turns`` supplies finalized history when available (the Plan pane passes
    it).  The live block is appended to that history and summarized through the
    same latest-request selector used after finalization and reload.  Standalone
    callers retain the historical assumption that a live stream has an active
    user request even when they do not provide the user Turn itself.
    """
    blocks = list(getattr(streaming, "blocks", []) or [])
    current = Turn(role="assistant")
    for block in blocks:
        btype = getattr(block, "type", "")
        if btype in (*_CONTENT_KINDS, "text"):
            text = getattr(block, "text", "") or ""
            if text:
                current.add(btype, text)
        elif btype == "tool_use":
            current.tool_uses.append(_tool_from_stream_block(block))

    if turns is not None:
        return summarize_turns([*turns, current])
    return _summarize_turn_window([current], user_seen=True)


def _summarize(
    steps: list[PlanStep],
    tools: list[ToolUse],
    *,
    user_seen: bool,
    has_final_text: bool,
    has_thinking: bool,
) -> PlanSummary:
    """Shared phase derivation for the live and finalized summarizers.

    Both paths feed identical signals here, so the same content yields the same
    PlanSummary whether it is streaming, finalized, or reloaded. Content with
    no explicit steps uses the same canonical ``inferred`` source at every
    stage; ``live`` is not a distinct source taxonomy.
    """
    phase_status = _phase_status_from_tools(tools, user_seen, has_final_text)
    if has_thinking and not tools:
        _mark_phase(phase_status, "plan", "active")
    if steps and any(step.status == "active" for step in steps):
        _mark_phase(phase_status, "build", "active")
    elif steps and all(step.status == "done" for step in steps):
        _mark_phase(phase_status, "verify", "active")
    elif steps:
        _mark_phase(phase_status, "plan", "done")
    return PlanSummary(
        phases=_build_phases(_normalize_phase_status(phase_status)),
        steps=steps or _fallback_steps(phase_status),
        active_detail=_active_detail_from_tools(tools),
        source="explicit" if steps else "inferred",
    )


def empty_summary() -> PlanSummary:
    phase_status = {key: "pending" for key, *_ in PHASES}
    return PlanSummary(phases=_build_phases(phase_status), steps=[], source="empty")


def summarize_native_plan(plan: object, explanation: str = "") -> PlanSummary:
    """Translate Codex App Server ``turn/plan/updated`` into a PlanSummary."""
    raw_steps = plan if isinstance(plan, list) else []
    steps: list[PlanStep] = []
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        text = str(item.get("step") or "").strip()
        if not text:
            continue
        status = {
            "pending": "pending",
            "inProgress": "active",
            "completed": "done",
        }.get(str(item.get("status") or ""), "pending")
        steps.append(PlanStep(text=text, status=status))

    phase_status = {key: "pending" for key, *_ in PHASES}
    if steps:
        phase_status["orient"] = "done"
        phase_status["plan"] = "done"
        if all(step.status == "done" for step in steps):
            phase_status["build"] = "done"
            phase_status["verify"] = "active"
        else:
            phase_status["build"] = "active"
    active = next((step.text for step in steps if step.status == "active"), "")
    return PlanSummary(
        phases=_build_phases(_normalize_phase_status(phase_status)),
        steps=steps,
        active_detail=(explanation or active).strip(),
        source="native",
        completed_count=sum(step.status == "done" for step in steps),
        total_count=len(steps),
    )


def summarize_execution_plan(plan: ExecutionPlan) -> PlanSummary:
    """Translate a durable provider-owned snapshot into Plan-pane state."""

    status_map = {
        "pending": "pending",
        "inProgress": "active",
        "completed": "done",
        "blocked": "blocked",
        "interrupted": "interrupted",
        "dropped": "dropped",
    }
    steps = [
        PlanStep(
            step.text,
            status_map.get(step.status, "pending"),
            step.blocked_reason or step.evidence,
            step.task_id,
        )
        for step in plan.steps
    ]
    phase_status = {key: "pending" for key, *_ in PHASES}
    if steps:
        phase_status["orient"] = "done"
        phase_status["plan"] = "done"
        if plan.status == "completed":
            phase_status["build"] = "done"
            phase_status["verify"] = "active"
        elif plan.status in {"blocked", "interrupted"}:
            phase_status["build"] = plan.status
        else:
            phase_status["build"] = "active"
    active = next(
        (step.text for step in steps if step.status == "active"),
        "",
    )
    exceptional = next(
        (
            step.text
            for step in steps
            if step.status in {"blocked", "interrupted"}
        ),
        "",
    )
    detail = plan.explanation or active or exceptional
    if plan.status == "interrupted" and exceptional:
        detail = f"Interrupted · {exceptional}"
    elif plan.status == "blocked" and exceptional:
        detail = f"Blocked · {exceptional}"
    elif plan.status == "completed":
        detail = "All tasks complete; validating the Work definition of done."
    return PlanSummary(
        phases=_build_phases(_normalize_phase_status(phase_status)),
        steps=steps,
        active_detail=detail.strip(),
        source="execution",
        completed_count=plan.completed_count,
        total_count=plan.total_count,
        revision=plan.revision,
        plan_status=plan.status,
    )


def _phase_status_from_tools(
    tools: list[ToolUse], user_seen: bool, assistant_seen: bool
) -> dict[str, str]:
    status = {key: "pending" for key, *_ in PHASES}
    if user_seen:
        status["orient"] = "done"
        status["plan"] = "active"
    if assistant_seen:
        status["plan"] = "done"
        status["handoff"] = "active"
    for tool in tools:
        name = tool.name.lower()
        if name in RESEARCH_TOOLS or name.startswith("mcp__"):
            _mark_phase(status, "orient", "done")
            _mark_phase(status, "plan", "done")
        elif name in BUILD_TOOLS:
            _mark_phase(status, "build", "active")
        elif name == "todowrite":
            _mark_phase(status, "plan", "done")
        elif name == "bash" and _looks_like_verification(tool.input.get("command", "")):
            _mark_phase(status, "build", "done")
            _mark_phase(status, "verify", "active")
        elif name == "bash":
            _mark_phase(status, "build", "active")

    if status["verify"] == "active" and assistant_seen:
        status["verify"] = "done"
        status["handoff"] = "active"
    return status


def _mark_phase(status: dict[str, str], key: str, value: str) -> None:
    rank = {"pending": 0, "active": 1, "done": 2}
    if rank[value] >= rank[status.get(key, "pending")]:
        status[key] = value
    if value in {"active", "done"}:
        for phase_key, *_ in PHASES:
            if phase_key == key:
                break
            if status[phase_key] == "pending":
                status[phase_key] = "done"


def _build_phases(status: dict[str, str]) -> list[PlanPhase]:
    return [
        PlanPhase(key=key, title=title, description=desc, status=status[key])
        for key, title, desc in PHASES
    ]


def _normalize_phase_status(status: dict[str, str]) -> dict[str, str]:
    """Keep the phase rail legible: one active phase, earlier phases done."""
    normalized = dict(status)
    active_indices = [
        i for i, (key, *_rest) in enumerate(PHASES)
        if normalized.get(key) == "active"
    ]
    if not active_indices:
        return normalized
    active_index = min(active_indices)
    for i, (key, *_rest) in enumerate(PHASES):
        if i < active_index:
            normalized[key] = "done"
        elif i == active_index:
            normalized[key] = "active"
        elif normalized.get(key) == "active":
            normalized[key] = "pending"
    return normalized


def _fallback_steps(status: dict[str, str]) -> list[PlanStep]:
    active = next((p for p, state in status.items() if state == "active"), "")
    if active == "build":
        return [PlanStep("Apply the next concrete code change.", "active")]
    if active == "verify":
        return [PlanStep("Run the relevant checks for this change.", "active")]
    if active == "handoff":
        return [PlanStep("Prepare the final handoff for this session.", "active")]
    if active == "plan":
        return [PlanStep("Shape the implementation plan from the request.", "active")]
    return []


def _steps_from_turn(turn: Turn) -> list[PlanStep]:
    for tool in reversed(turn.tool_uses):
        if _is_todo_tool(tool):
            steps = _steps_from_todo_input(tool.input)
            if steps:
                return steps
    # Walk assistant content newest-first in true SOURCE order (not a fixed
    # per-lane order), so a later commentary revising an earlier reasoning wins
    # — matching the live stream, so stream/final/reload pick the same plan.
    for span in reversed(turn.content):
        steps = _steps_from_text(span.text)
        if steps:
            return steps
    return []


def _steps_from_todo_input(inp: dict) -> list[PlanStep]:
    todos = inp.get("todos") or []
    if not isinstance(todos, list):
        return []
    steps = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        text = item.get("content") or item.get("text") or item.get("title") or ""
        if not text:
            continue
        status = _normalize_status(item.get("status") or "")
        steps.append(PlanStep(str(text), status))
    return steps


def _steps_from_text(text: str) -> list[PlanStep]:
    steps: list[PlanStep] = []
    for line in text.splitlines():
        stripped = line.strip()
        m = re.match(r"^(?:[-*]\s+\[(?P<check>[ xX-])\]|(?:\d+[.)]|[-*])\s+)(?P<text>.+)$", stripped)
        if not m:
            continue
        item = m.group("text").strip()
        if not item or len(item) > 180:
            continue
        check = m.groupdict().get("check")
        status = "pending"
        if check and check.lower() == "x":
            status = "done"
        elif check == "-":
            status = "active"
        steps.append(PlanStep(item, status))
    return steps[:8]


def _tool_from_stream_block(block) -> ToolUse:
    raw = getattr(block, "tool_use_input_json", "") or ""
    try:
        inp = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        inp = {}
    return ToolUse(
        name=getattr(block, "tool_use_name", "") or "(tool)",
        input=inp,
        id=getattr(block, "tool_use_id", "") or "",
    )


def _active_detail_from_tools(tools: list[ToolUse]) -> str:
    if not tools:
        return ""
    tool = tools[-1]
    name = tool.name
    if name == "Bash":
        return str(tool.input.get("command") or "")[:120]
    path = tool.input.get("file_path") or tool.input.get("path") or ""
    if path:
        return f"{name} {path}"
    if name == "TodoWrite":
        return "Updating the task list"
    return name


def _is_todo_tool(tool: ToolUse) -> bool:
    return tool.name.lower() == "todowrite"


def _normalize_status(value: str) -> str:
    v = value.lower()
    if v in {"completed", "complete", "done"}:
        return "done"
    if v in {"in_progress", "in-progress", "active", "doing"}:
        return "active"
    return "pending"


def _looks_like_verification(command: str) -> bool:
    c = command.lower()
    return any(word in c for word in VERIFY_WORDS)
