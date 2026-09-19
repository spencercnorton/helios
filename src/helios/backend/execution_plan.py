"""Durable model-owned execution plans.

Execution plans are operational state produced by a provider while it works.
They are deliberately separate from :mod:`session_goals`, whose checklist is
user-owned acceptance state.  This module is GTK-free so the store, tests, and
future supervisor can share one normalization and identity contract.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Literal

from helios.backend.sensitive_text import scrub_sensitive


ExecutionStepStatus = Literal[
    "pending",
    "inProgress",
    "completed",
    "blocked",
    "dropped",
    "interrupted",
]
ExecutionPlanStatus = Literal["active", "completed", "blocked", "interrupted"]

STEP_PENDING: ExecutionStepStatus = "pending"
STEP_IN_PROGRESS: ExecutionStepStatus = "inProgress"
STEP_COMPLETED: ExecutionStepStatus = "completed"
STEP_BLOCKED: ExecutionStepStatus = "blocked"
STEP_DROPPED: ExecutionStepStatus = "dropped"
STEP_INTERRUPTED: ExecutionStepStatus = "interrupted"

COUNTABLE_STEP_STATUSES = frozenset(
    {
        STEP_PENDING,
        STEP_IN_PROGRESS,
        STEP_COMPLETED,
        STEP_BLOCKED,
        STEP_INTERRUPTED,
    }
)
_MAX_PROVIDER_STEPS = 64
_MAX_STORED_STEPS = 128
_MAX_STEP_CHARS = 1_000
_MAX_EXPLANATION_CHARS = 8_000
_SPACE_RE = re.compile(r"\s+")
_WORD_SEPARATOR_RE = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ExecutionPlanStep:
    """One provider-owned task with a Helios-stable local identity."""

    task_id: str
    text: str
    status: ExecutionStepStatus = STEP_PENDING
    evidence: str = ""
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """One immutable snapshot of the latest model-owned Work plan."""

    work_id: str
    plan_id: str
    revision: int
    provider: str
    participant_id: str
    participant_generation: int
    native_turn_id: str
    explanation: str
    steps: tuple[ExecutionPlanStep, ...]
    status: ExecutionPlanStatus
    created_at: str
    updated_at: str

    @property
    def completed_count(self) -> int:
        return sum(step.status == STEP_COMPLETED for step in self.steps)

    @property
    def total_count(self) -> int:
        return sum(step.status in COUNTABLE_STEP_STATUSES for step in self.steps)

    @property
    def active_step(self) -> ExecutionPlanStep | None:
        return next(
            (step for step in self.steps if step.status == STEP_IN_PROGRESS),
            None,
        )


def plan_id_for(
    *,
    work_id: str,
    provider: str,
    participant_id: str,
    participant_generation: int,
    native_turn_id: str,
) -> str:
    """Return a deterministic identity for one participant's native turn."""

    material = "\0".join(
        (
            work_id,
            provider,
            participant_id,
            str(participant_generation),
            native_turn_id,
        )
    )
    return f"plan_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def normalize_explanation(value: Any) -> str:
    text, _redacted = scrub_sensitive(value)
    return _normalize_text(text, max_chars=_MAX_EXPLANATION_CHARS)


def reconcile_steps(
    plan_id: str,
    raw_steps: Any,
    previous_steps: Iterable[ExecutionPlanStep] = (),
    *,
    dropped_reason: str = "",
) -> tuple[ExecutionPlanStep, ...]:
    """Normalize a provider snapshot and preserve task ids across revisions.

    App Server currently supplies step text and status but no task id. Exact
    normalized text matches win first. Remaining edits are matched one-to-one
    only when their wording is conservatively similar; every other task gets a
    deterministic id derived from the plan and its normalized occurrence.
    """

    incoming = _normalize_raw_steps(raw_steps)
    previous = tuple(previous_steps)
    matches = _match_previous_steps(previous, incoming)
    occurrences: dict[str, int] = {}
    reconciled: list[ExecutionPlanStep] = []
    for index, (text, status) in enumerate(incoming):
        identity_text = _identity_text(text)
        occurrence = occurrences.get(identity_text, 0)
        occurrences[identity_text] = occurrence + 1
        prior_index = matches.get(index)
        if prior_index is None:
            task_id = _task_id(plan_id, identity_text, occurrence)
            evidence = ""
            blocked_reason = ""
        else:
            prior = previous[prior_index]
            task_id = prior.task_id
            evidence = prior.evidence if prior.status == status else ""
            blocked_reason = prior.blocked_reason if prior.status == status else ""
        reconciled.append(
            ExecutionPlanStep(
                task_id=task_id,
                text=text,
                status=status,
                evidence=evidence,
                blocked_reason=blocked_reason,
            )
        )
    matched_old = set(matches.values())
    reason = normalize_explanation(dropped_reason) or "Removed by a native plan revision."
    for old_index, prior in enumerate(previous):
        if old_index in matched_old or prior.status == STEP_COMPLETED:
            continue
        if len(reconciled) >= _MAX_STORED_STEPS:
            break
        reconciled.append(
            ExecutionPlanStep(
                task_id=prior.task_id,
                text=prior.text,
                status=STEP_DROPPED,
                evidence=prior.evidence,
                blocked_reason=(
                    prior.blocked_reason
                    if prior.status == STEP_DROPPED and prior.blocked_reason
                    else reason
                ),
            )
        )
    return tuple(reconciled)


def plan_status_for(
    steps: Iterable[ExecutionPlanStep],
    *,
    interrupted: bool = False,
) -> ExecutionPlanStatus:
    steps = tuple(steps)
    if interrupted or any(step.status == STEP_INTERRUPTED for step in steps):
        return "interrupted"
    countable = tuple(
        step for step in steps if step.status in COUNTABLE_STEP_STATUSES
    )
    if countable and all(step.status == STEP_COMPLETED for step in countable):
        return "completed"
    if countable and all(
        step.status in {STEP_COMPLETED, STEP_BLOCKED} for step in countable
    ) and any(step.status == STEP_BLOCKED for step in countable):
        return "blocked"
    return "active"


def interrupt_steps(
    steps: Iterable[ExecutionPlanStep],
    *,
    reason: str = "",
) -> tuple[ExecutionPlanStep, ...]:
    """Mark active tasks interrupted without inventing task completion."""

    reason = normalize_explanation(reason)
    return tuple(
        ExecutionPlanStep(
            task_id=step.task_id,
            text=step.text,
            status=(
                STEP_INTERRUPTED
                if step.status == STEP_IN_PROGRESS
                else step.status
            ),
            evidence=step.evidence,
            blocked_reason=(
                reason
                if step.status == STEP_IN_PROGRESS and reason
                else step.blocked_reason
            ),
        )
        for step in steps
    )


def steps_to_json(steps: Iterable[ExecutionPlanStep]) -> list[dict[str, str]]:
    return [
        {
            "task_id": step.task_id,
            "text": step.text,
            "status": step.status,
            "evidence": step.evidence,
            "blocked_reason": step.blocked_reason,
        }
        for step in steps
    ]


def steps_from_json(value: Any) -> tuple[ExecutionPlanStep, ...]:
    if not isinstance(value, list):
        return ()
    out: list[ExecutionPlanStep] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        task_id = str(item.get("task_id") or "").strip()
        text = _normalize_text(item.get("text"), max_chars=_MAX_STEP_CHARS)
        if not task_id or not text:
            continue
        out.append(
            ExecutionPlanStep(
                task_id=task_id,
                text=text,
                status=normalize_step_status(item.get("status")),
                evidence=_normalize_text(
                    item.get("evidence"), max_chars=_MAX_EXPLANATION_CHARS
                ),
                blocked_reason=_normalize_text(
                    item.get("blocked_reason"), max_chars=_MAX_EXPLANATION_CHARS
                ),
            )
        )
    return tuple(out[:_MAX_STORED_STEPS])


def normalize_step_status(value: Any) -> ExecutionStepStatus:
    status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if status in {"inprogress", "in_progress", "active", "doing", "started"}:
        return STEP_IN_PROGRESS
    if status in {"completed", "complete", "done", "success", "succeeded"}:
        return STEP_COMPLETED
    if status in {"blocked", "stuck"}:
        return STEP_BLOCKED
    if status in {"dropped", "cancelled", "canceled", "removed"}:
        return STEP_DROPPED
    if status in {"interrupted", "aborted", "failed"}:
        return STEP_INTERRUPTED
    return STEP_PENDING


def _normalize_raw_steps(value: Any) -> list[tuple[str, ExecutionStepStatus]]:
    if not isinstance(value, list):
        return []
    out: list[tuple[str, ExecutionStepStatus]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_text = item.get("step") or item.get("text") or item.get("task")
        text, _redacted = scrub_sensitive(raw_text)
        text = _normalize_text(text, max_chars=_MAX_STEP_CHARS)
        if not text:
            continue
        out.append((text, normalize_step_status(item.get("status"))))
        if len(out) >= _MAX_PROVIDER_STEPS:
            break
    return out


def _match_previous_steps(
    previous: tuple[ExecutionPlanStep, ...],
    incoming: list[tuple[str, ExecutionStepStatus]],
) -> dict[int, int]:
    matches: dict[int, int] = {}
    unused_old = set(range(len(previous)))

    # Exact normalized wording is unambiguous, including duplicate occurrences.
    for new_index, (text, _status) in enumerate(incoming):
        identity = _identity_text(text)
        old_index = next(
            (
                index
                for index in sorted(unused_old)
                if _identity_text(previous[index].text) == identity
            ),
            None,
        )
        if old_index is not None:
            matches[new_index] = old_index
            unused_old.remove(old_index)

    candidates: list[tuple[float, int, int]] = []
    for new_index, (text, _status) in enumerate(incoming):
        if new_index in matches:
            continue
        for old_index in unused_old:
            score = _similarity(previous[old_index].text, text)
            if score >= 0.74:
                candidates.append((score, old_index, new_index))
    # Highest-confidence matches win. Stable index tie-breakers make retries
    # deterministic even when two tasks are worded similarly.
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_new = set(matches)
    for _score, old_index, new_index in candidates:
        if old_index not in unused_old or new_index in used_new:
            continue
        matches[new_index] = old_index
        unused_old.remove(old_index)
        used_new.add(new_index)
    return matches


def _similarity(left: str, right: str) -> float:
    left_id = _identity_text(left)
    right_id = _identity_text(right)
    sequence = SequenceMatcher(None, left_id, right_id, autojunk=False).ratio()
    left_tokens = set(left_id.split())
    right_tokens = set(right_id.split())
    union = left_tokens | right_tokens
    overlap = len(left_tokens & right_tokens) / len(union) if union else 0.0
    # A very close character edit is safe on its own; otherwise require enough
    # token continuity to avoid merging two generic neighboring tasks.
    if sequence >= 0.90:
        return sequence
    if overlap < 0.45:
        return 0.0
    return (sequence * 0.75) + (overlap * 0.25)


def _task_id(plan_id: str, identity_text: str, occurrence: int) -> str:
    material = f"{plan_id}\0{identity_text}\0{occurrence}"
    return f"task_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _identity_text(value: str) -> str:
    return _SPACE_RE.sub(
        " ",
        _WORD_SEPARATOR_RE.sub(" ", value.casefold()),
    ).strip()


def _normalize_text(value: Any, *, max_chars: int) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
