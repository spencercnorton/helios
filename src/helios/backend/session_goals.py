"""Persistent Helios-native goals for provider-neutral Work.

Goal Mode is intentionally a Helios layer, not a provider feature. The app
stores the objective/checklist once, injects a compact context envelope into
outbound prompts for either backend, and strips that envelope back out when
reading native transcripts so search/history stay clean. Existing files may
still contain provider session ids as keys; :func:`rekey_goal` migrates those
lazily when a native session is attached to a Work.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from helios.log import get_logger
from helios.paths import state_dir
from helios.backend.sensitive_text import scrub_sensitive

GoalStatus = Literal["active", "paused", "complete", "blocked"]
PlanStatus = Literal["pending", "in_progress", "completed", "blocked"]

GOAL_ACTIVE: GoalStatus = "active"
GOAL_PAUSED: GoalStatus = "paused"
GOAL_COMPLETE: GoalStatus = "complete"
GOAL_BLOCKED: GoalStatus = "blocked"
GOAL_STATUSES: set[str] = {GOAL_ACTIVE, GOAL_PAUSED, GOAL_COMPLETE, GOAL_BLOCKED}

PLAN_PENDING: PlanStatus = "pending"
PLAN_IN_PROGRESS: PlanStatus = "in_progress"
PLAN_COMPLETED: PlanStatus = "completed"
PLAN_BLOCKED: PlanStatus = "blocked"
PLAN_STATUSES: set[str] = {
    PLAN_PENDING,
    PLAN_IN_PROGRESS,
    PLAN_COMPLETED,
    PLAN_BLOCKED,
}

# Codex App Server's native thread goal contract accepts at most 4,000
# characters. Helios keeps legacy/provider-neutral goals intact, but new UI
# edits enforce this limit so native and canonical objectives cannot diverge.
MAX_GOAL_OBJECTIVE_CHARS = 4000

# Test/back-compat override. Production resolves state_dir() at call time so
# HELIOS_STATE_DIR profiles cannot split Work state from Goal state.
_PATH: Path | None = None
_BEGIN = "--- BEGIN HELIOS GOAL CONTEXT ---"
_END = "--- END HELIOS GOAL CONTEXT ---"
_USER_PREFIX = "User request:"

_lock = threading.Lock()
_cache: dict[str, "GoalState"] | None = None
_log = get_logger("session-goals")


@dataclass(slots=True)
class GoalPlanItem:
    text: str
    status: PlanStatus = PLAN_PENDING


@dataclass(slots=True)
class GoalState:
    objective: str
    # The stopping condition.: this shipped in the schema at v2 and had
    # no writer anywhere, which is why contract_epoch was 0 on every Work. It
    # is the model's working brief first and a collaboration permission second.
    definition_of_done: str = ""
    status: GoalStatus = GOAL_ACTIVE
    items: list[GoalPlanItem] = field(default_factory=list)
    cwd: str = ""
    provider: str = ""
    created_at: str = ""
    updated_at: str = ""


def get_goal(session_id: str) -> GoalState | None:
    if not session_id:
        return None
    with _lock:
        goal = _load().get(session_id)
        return _clone_goal(goal) if goal is not None else None


def set_goal(session_id: str, goal: GoalState) -> None:
    if not session_id:
        return
    normalized = _normalize_goal(goal)
    if not normalized.objective:
        clear_goal(session_id)
        return
    now = _now()
    if not normalized.created_at:
        normalized.created_at = now
    normalized.updated_at = now
    with _lock:
        data = _load()
        data[session_id] = normalized
        _save(data)


def clear_goal(session_id: str) -> None:
    if not session_id:
        return
    with _lock:
        data = _load()
        if data.pop(session_id, None) is not None:
            _save(data)


def rekey_goal(legacy_session_id: str, work_id: str) -> GoalState | None:
    """Atomically move a legacy session-keyed goal beneath ``work_id``.

    The canonical Work entry wins if both keys exist. The legacy alias is
    removed either way, preventing a cleared Work goal from resurfacing when
    an old native transcript is selected later. Safe to call repeatedly.
    """

    if not work_id:
        return None
    with _lock:
        data = _load()
        if not legacy_session_id or legacy_session_id == work_id:
            goal = data.get(work_id)
            return _clone_goal(goal) if goal is not None else None
        legacy = data.pop(legacy_session_id, None)
        canonical = data.get(work_id)
        if canonical is None and legacy is not None:
            data[work_id] = legacy
            canonical = legacy
        if legacy is not None:
            _save(data)
        return _clone_goal(canonical) if canonical is not None else None


def reload() -> None:
    global _cache
    with _lock:
        _cache = None


def progress_counts(goal: GoalState | None) -> tuple[int, int]:
    if goal is None:
        return (0, 0)
    total = len(goal.items)
    done = sum(1 for item in goal.items if item.status == PLAN_COMPLETED)
    return (done, total)


def item_status_for(*, checked: bool, original: str) -> PlanStatus:
    """Resolve a checklist item's status from the dialog checkbox.

    Checked always means completed. When unchecked, an agent-set in_progress
    or blocked status is preserved (so editing the goal does not wipe the
    model's live plan state); anything else becomes pending.
    """
    if checked:
        return PLAN_COMPLETED
    normalized = _normalize_plan_status(original)
    if normalized in (PLAN_IN_PROGRESS, PLAN_BLOCKED):
        return normalized
    return PLAN_PENDING


def objective_validation_error(objective: str) -> str:
    length = len(str(objective or "").strip())
    if length == 0:
        return "Enter an objective."
    if length > MAX_GOAL_OBJECTIVE_CHARS:
        return (
            f"Goal is {length:,} characters; the native limit is "
            f"{MAX_GOAL_OBJECTIVE_CHARS:,}."
        )
    return ""


def wrap_user_prompt(text: str, goal: GoalState | None) -> str:
    if goal is None or goal.status != GOAL_ACTIVE:
        return text
    goal = _normalize_goal(goal)
    if not goal.objective:
        return text

    plan_lines = []
    for item in goal.items:
        plan_lines.append(f"- [{item.status}] {item.text}")
    plan_text = "\n".join(plan_lines) if plan_lines else "- No acceptance items supplied"

    # the stopping condition ships in the envelope for EVERY Work,
    # single or tandem — that is what makes the field worth filling in. The
    # checklist is explicitly acceptance state; provider-owned TodoWrite/native
    # plans remain a separate operational layer.
    #
    # The old third instruction was self-referential: "when the objective is
    # complete, say so" left the model to invent its own completion test, which
    # is what an 81-subagent fan-out looks like from the inside. With a stated
    # condition the instruction can name it, and forbid widening scope instead.
    done_line = f"Done when: {goal.definition_of_done}\n" if goal.definition_of_done else ""
    if goal.definition_of_done:
        closing = (
            '- Stop when "Done when" is satisfied, and say which part satisfied it.\n'
            "- If it cannot be satisfied, stop and say what blocks it. Do not widen scope.\n"
        )
    else:
        closing = (
            "- When the objective is complete, say so clearly and summarize validation.\n"
        )

    return (
        f"{_BEGIN}\n"
        f"Objective: {goal.objective}\n"
        f"{done_line}"
        f"Status: {goal.status}\n"
        "Acceptance checklist:\n"
        f"{plan_text}\n"
        "Instructions:\n"
        "- Work toward the objective across turns until it is complete or blocked.\n"
        "- Keep a separate execution plan using TodoWrite/todo_list when useful.\n"
        "- Do not rewrite the user-owned acceptance checklist from that plan.\n"
        f"{closing}"
        f"{_END}\n\n"
        f"{_USER_PREFIX}\n"
        f"{text}"
    )


def wrap_goal_supplement(text: str, goal: GoalState | None) -> str:
    """Ship acceptance state omitted by a provider's native goal contract.

    Codex App Server owns the synchronized objective/status/token budget, but
    its native goal has no definition-of-done or checklist fields. Repeating
    the objective would create two authorities, so this envelope contains only
    the missing user-owned acceptance contract.
    """

    if goal is None or goal.status != GOAL_ACTIVE:
        return text
    goal = _normalize_goal(goal)
    if not goal.definition_of_done and not goal.items:
        return text

    lines = [
        _BEGIN,
        "Acceptance context (the native goal remains authoritative):",
    ]
    if goal.definition_of_done:
        lines.append(f"Done when: {goal.definition_of_done}")
    if goal.items:
        lines.append("Acceptance checklist:")
        lines.extend(f"- [{item.status}] {item.text}" for item in goal.items)
    lines.extend(
        (
            "Instructions:",
            "- Treat this as user-owned acceptance state, not as your execution plan.",
            "- Keep a separate native execution plan current while you work.",
        )
    )
    if goal.definition_of_done:
        lines.extend(
            (
                '- Stop when "Done when" is satisfied, and say which part satisfied it.',
                "- If it cannot be satisfied, stop and say what blocks it. Do not widen scope.",
            )
        )
    else:
        lines.append(
            "- Do not claim completion until the native objective and acceptance checklist are satisfied."
        )
    return "\n".join((*lines, _END, "", _USER_PREFIX, text))


def strip_goal_envelope(text: str) -> str:
    if not isinstance(text, str):
        return ""
    leading = text.lstrip()
    if not leading.startswith(_BEGIN):
        return text
    end_idx = leading.find(_END)
    if end_idx < 0:
        return text
    rest = leading[end_idx + len(_END):]
    rest = rest.lstrip()
    if rest.startswith(_USER_PREFIX):
        rest = rest[len(_USER_PREFIX):].lstrip("\r\n ")
    return rest


def normalize_plan_items(items: Any) -> list[GoalPlanItem]:
    if not isinstance(items, list):
        return []
    out: list[GoalPlanItem] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        text = ""
        status: Any = PLAN_PENDING
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            for key in ("content", "text", "task", "title"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    text = value
                    break
            status = item.get("status") or item.get("state") or PLAN_PENDING
        if not text.strip():
            continue
        plan_item = GoalPlanItem(
            text=" ".join(text.split()),
            status=_normalize_plan_status(status),
        )
        ident = (plan_item.text, plan_item.status)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(plan_item)
    return out


def _load() -> dict[str, GoalState]:
    global _cache
    if _cache is not None:
        return _cache
    path = _path()
    if not path.is_file():
        _cache = {}
        return _cache
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, GoalState] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        goal = _goal_from_json(value)
        if goal is not None and goal.objective:
            out[key] = goal
    _cache = out
    return _cache


def _save(data: dict[str, GoalState]) -> None:
    raw = {sid: _goal_to_json(goal) for sid, goal in data.items() if goal.objective}
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as e:
        _log.warning("could not save session goals: %s", e)


def _path() -> Path:
    return _PATH if _PATH is not None else state_dir() / "session-goals.json"


def _goal_from_json(value: Any) -> GoalState | None:
    if not isinstance(value, dict):
        return None
    return _normalize_goal(
        GoalState(
            objective=str(value.get("objective") or ""),
            definition_of_done=str(value.get("definition_of_done") or ""),
            status=_normalize_goal_status(value.get("status")),
            items=normalize_plan_items(value.get("items") or value.get("plan") or []),
            cwd=str(value.get("cwd") or ""),
            provider=str(value.get("provider") or ""),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
        )
    )


def _goal_to_json(goal: GoalState) -> dict[str, Any]:
    goal = _normalize_goal(goal)
    return {
        "objective": goal.objective,
        "definition_of_done": goal.definition_of_done,
        "status": goal.status,
        "items": [
            {"text": item.text, "status": item.status}
            for item in normalize_plan_items([
                {"text": item.text, "status": item.status} for item in goal.items
            ])
        ],
        "cwd": goal.cwd,
        "provider": goal.provider,
        "created_at": goal.created_at,
        "updated_at": goal.updated_at,
    }


def _normalize_goal(goal: GoalState) -> GoalState:
    objective, _ = scrub_sensitive(goal.objective)
    definition_of_done, _ = scrub_sensitive(goal.definition_of_done)
    safe_items = []
    for item in goal.items:
        text, _ = scrub_sensitive(item.text)
        safe_items.append({"text": text, "status": item.status})
    return GoalState(
        objective=objective.strip(),
        definition_of_done=definition_of_done.strip(),
        status=_normalize_goal_status(goal.status),
        items=normalize_plan_items(safe_items),
        cwd=goal.cwd,
        provider=goal.provider,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )


def _normalize_goal_status(value: Any) -> GoalStatus:
    status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if status in GOAL_STATUSES:
        return status  # type: ignore[return-value]
    if status in {"done", "completed"}:
        return GOAL_COMPLETE
    return GOAL_ACTIVE


def _normalize_plan_status(value: Any) -> PlanStatus:
    status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if status in PLAN_STATUSES:
        return status  # type: ignore[return-value]
    if status in {"done", "complete", "completed", "success", "succeeded"}:
        return PLAN_COMPLETED
    if status in {"doing", "started", "inprogress", "in_progress"}:
        return PLAN_IN_PROGRESS
    return PLAN_PENDING


def _clone_goal(goal: GoalState) -> GoalState:
    # `replace` rather than a field-by-field rebuild: this used to enumerate
    # every field, which silently DROPPED `definition_of_done` on the way out of
    # Get_goal() even though it was correctly written to disk. Six
    # places rebuilt GoalState this way; the ones that only transform a subset
    # now use replace() so a future field cannot vanish the same way.
    return replace(
        goal, items=[GoalPlanItem(item.text, item.status) for item in goal.items]
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
