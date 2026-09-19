"""Provider-neutral coordination facade used by the current Helios client.

This compatibility layer keeps GTK concerns out of Work persistence and prompt
construction.  It can move behind the planned supervisor IPC without changing
the provider adapters' delivery contract.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from helios.backend import model_catalog, session_goals
from helios.backend.execution_plan import ExecutionPlan
from helios.backend.process.message_queue import PreparedPrompt
from helios.backend.session_goals import GoalState
from helios.backend.sensitive_text import scrub_sensitive
from helios.backend.work_context import (
    DEFAULT_MAX_UPDATES,
    CollaborationUpdate,
    build_packet,
    wrap_user_prompt,
)
from helios.backend.work_store import (
    CONTROL_PLANE_AUDIT_EVENT_TYPES,
    ExecutionAttempt,
    Participant,
    Work,
    WorkEvent,
    WorkNotFoundError,
    WorkStore,
)
from helios.log import get_logger

_log = get_logger("work-coordinator")
_MAX_LEDGER_TEXT = 12_000


class WorkCoordinator:
    """Small orchestration surface shared by Claude and Codex drivers."""

    def __init__(self, store: WorkStore | None = None) -> None:
        self.store = store or WorkStore()

    def ensure_work(
        self,
        *,
        work_id: str = "",
        cwd: str = "",
        lead_provider: str = model_catalog.PROVIDER_ANTHROPIC,
        objective: str = "",
        definition_of_done: str = "",
        mode: str = "single",
    ) -> Work:
        safe_objective, _ = scrub_sensitive(objective)
        safe_definition, _ = scrub_sensitive(definition_of_done)
        return self.store.ensure_work(
            work_id or None,
            cwd=cwd,
            lead_provider=lead_provider,
            objective=safe_objective,
            definition_of_done=safe_definition,
            mode=mode,
        )

    def fork_work(
        self,
        *,
        source_work_id: str,
        provider: str,
        native_id: str,
        model: str = "",
    ) -> tuple[Work, Participant]:
        """Create an independent single-provider Work for a native fork.

        A fork is never a participant rebind: rebinding would move the source
        Work's only provider onto the new thread and make the preserved source
        transcript lie about its model context. User-owned Goal acceptance is
        copied; provider-owned execution-plan revisions are intentionally not.
        """

        source = self.store.get_work(source_work_id)
        if source is None:
            raise ValueError(f"unknown source Work: {source_work_id}")
        native_id = str(native_id or "").strip()
        if not native_id:
            raise ValueError("forked native identity is required")
        work = self.store.create_work(
            objective=source.objective,
            definition_of_done=source.definition_of_done,
            cwd=source.cwd,
            mode="single",
            status="active",
            lead_provider=provider,
            metadata={
                "forked_from_work_id": source.work_id,
                "forked_from_native_id": self.resume_id(source.work_id, provider),
            },
        )
        participant = self.bind_participant(
            work.work_id,
            provider,
            native_id=native_id,
            model=model,
        )
        self.store.append_control_event(
            work.work_id,
            "work.forked",
            {
                "source_work_id": source.work_id,
                "provider": provider,
                "native_id": native_id,
            },
            provider=provider,
        )

        source_goal = session_goals.get_goal(source.work_id)
        if source_goal is None and source.objective:
            source_goal = GoalState(
                objective=source.objective,
                definition_of_done=source.definition_of_done,
                cwd=source.cwd,
                provider=provider,
            )
        if source_goal is not None:
            session_goals.set_goal(
                work.work_id,
                replace(
                    source_goal,
                    status="active",
                    cwd=source.cwd,
                    provider=provider,
                    created_at="",
                    updated_at="",
                ),
            )
        return work, participant

    def resolve_native_work(
        self,
        provider: str,
        native_id: str,
        *,
        cwd: str = "",
        model: str = "",
    ) -> Work:
        """Lazily attach an existing native transcript to one logical Work."""

        work = self.store.ensure_work_for_native(
            provider,
            native_id,
            cwd=cwd,
            model=model,
            native_kind=(
                "thread"
                if provider == model_catalog.PROVIDER_OPENAI
                else "session"
            ),
        )
        participant = self.participant(work.work_id, provider)
        # Looking at a retired historical transcript must not fence the newer
        # active binding. Activation happens only if the user actually sends.
        if participant is not None and participant.native_id == native_id:
            self.bind_participant(
                work.work_id,
                provider,
                native_id=native_id,
                model=model,
            )
        session_goals.rekey_goal(native_id, work.work_id)
        return work

    def bind_participant(
        self,
        work_id: str,
        provider: str,
        *,
        native_id: str = "",
        model: str = "",
    ) -> Participant:
        work = self.store.get_work(work_id)
        if work is None:
            raise ValueError(f"unknown Work: {work_id}")
        native_kwargs = (
            {"native_thread_id": native_id}
            if provider == model_catalog.PROVIDER_OPENAI
            else {"native_session_id": native_id}
        )
        role = "lead" if provider == work.lead_provider else "partner"
        metadata = {"model": model} if model else None
        return self.store.bind_participant(
            work_id,
            provider,
            role=role,
            metadata=metadata,
            **native_kwargs,
        )

    def participant(self, work_id: str, provider: str) -> Participant | None:
        return self.store.participant_for_provider(work_id, provider)

    def select_lead(self, work_id: str, provider: str) -> Work:
        """Project the user-selected provider as current Work lead."""

        work = self.store.get_work(work_id)
        if work is None:
            raise ValueError(f"unknown Work: {work_id}")
        if work.lead_provider != provider:
            work = self.store.update_work(work_id, lead_provider=provider)
        for participant in self.store.list_participants(work_id):
            role = "lead" if participant.provider == provider else "partner"
            if participant.role != role:
                self.store.update_participant(participant.participant_id, role=role)
        return work

    def resume_id(self, work_id: str, provider: str) -> str:
        participant = self.participant(work_id, provider)
        return participant.native_id if participant is not None else ""

    def detach_native(self, native_id: str) -> str:
        """Retire a deleted transcript binding without deleting its Work."""

        for provider in (
            model_catalog.PROVIDER_ANTHROPIC,
            model_catalog.PROVIDER_OPENAI,
        ):
            work_id = self.store.work_id_for_native_session(provider, native_id)
            if not work_id:
                continue
            participant = self.participant(work_id, provider)
            if participant is None:
                return work_id
            if participant.native_id != native_id:
                # Historical retired binding: deleting its transcript must not
                # detach the participant's newer active native context.
                return work_id
            kwargs = (
                {"native_thread_id": ""}
                if provider == model_catalog.PROVIDER_OPENAI
                else {"native_session_id": ""}
            )
            self.store.update_participant(
                participant.participant_id,
                expected_generation=participant.generation,
                **kwargs,
            )
            self.store.append_event(
                work_id,
                "binding.retired",
                {"provider": provider, "native_id": native_id},
                provider=provider,
            )
            return work_id
        return ""

    def prepare_prompt(
        self,
        *,
        work_id: str,
        provider: str,
        text: str,
        goal: GoalState | None,
        include_goal_envelope: bool = True,
        include_goal_supplement: bool = False,
    ) -> str | PreparedPrompt:
        """Build the next bounded ledger delta with post-delivery cursor ACK."""

        if not work_id:
            return _wrap_goal_context(
                text,
                goal,
                include_envelope=include_goal_envelope,
                include_supplement=include_goal_supplement,
            )
        participant = self.participant(work_id, provider)
        if participant is None:
            self.bind_participant(work_id, provider)
        snapshot = self.store.snapshot_collaboration_prompt(
            work_id,
            provider,
            limit=DEFAULT_MAX_UPDATES,
        )
        participant = snapshot.participant
        work = snapshot.work
        safe_goal = _safe_goal(goal)
        if not snapshot.contract_accepted:
            return _wrap_goal_context(
                text,
                safe_goal,
                include_envelope=include_goal_envelope,
                include_supplement=include_goal_supplement,
            )
        packet = build_packet(
            work_id,
            [
                _collaboration_update(event)
                for event in snapshot.events
                if not _is_control_plane_audit_event(event)
            ],
            accepted_state=_accepted_state(work, safe_goal),
        )
        wire_text = wrap_user_prompt(text, packet)
        wire_text = _wrap_goal_context(
            wire_text,
            safe_goal,
            include_envelope=include_goal_envelope,
            include_supplement=include_goal_supplement,
        )
        through_seq = _contiguous_prompt_boundary(
            participant.last_event_seq,
            snapshot.events,
            packet.through_seq,
        )
        if through_seq <= participant.last_event_seq:
            return wire_text

        participant_id = participant.participant_id
        generation = participant.generation
        def acknowledge() -> None:
            self.store.acknowledge_collaboration_prompt(
                participant_id,
                through_seq=through_seq,
                expected_generation=generation,
            )

        return PreparedPrompt(wire_text, acknowledge)

    def record_user_message(self, driver: Any, text: str) -> WorkEvent | None:
        bounded, redacted, truncated = _bounded_text(text)
        return self._record_driver_event(
            driver,
            "user.message",
            {"text": bounded, "redacted": redacted, "truncated": truncated},
        )

    def record_context_degradation(
        self,
        *,
        work_id: str,
        provider: str,
    ) -> WorkEvent:
        """Durably audit an allowed single-Work goal-only fallback.

        Exception prose is intentionally absent: coordinator/storage failures
        can carry prompt or credential fragments and this event is a durable
        operator record.
        """

        work = self.store.get_work(work_id)
        if work is None or work.mode != "single":
            raise ValueError("context degradation requires a verified single Work")
        return self.store.append_control_event(
            work_id,
            "context.degraded",
            {
                "reason_code": "context.prepare_failed",
                "fallback": "goal-only",
            },
            provider=provider,
        )

    def record_turn(self, driver: Any, turn: Any) -> WorkEvent | None:
        if getattr(turn, "role", "") != "assistant":
            return None
        text, redacted, truncated = _bounded_text(
            getattr(turn, "text", "") or ""
        )
        tools = [
            str(getattr(tool, "name", "") or "")
            for tool in (getattr(turn, "tool_uses", None) or [])
            if getattr(tool, "name", "")
        ]
        if not text and not tools:
            return None
        return self._record_driver_event(
            driver,
            "participant.contribution",
            {
                "text": text,
                "tools": tools,
                "redacted": redacted,
                "truncated": truncated,
            },
        )

    def record_execution_plan(
        self,
        driver: Any,
        payload: dict[str, Any],
    ) -> ExecutionPlan | None:
        """Persist one authoritative native plan without entering model context."""

        identity = _driver_identity(driver)
        if identity is None or not isinstance(payload, dict):
            return None
        work_id, provider, participant_id, generation = identity
        native_turn_id = str(payload.get("turnId") or "").strip()
        steps = payload.get("plan")
        if not native_turn_id or not isinstance(steps, list):
            return None
        try:
            return self.store.record_execution_plan(
                work_id,
                participant_id,
                provider=provider,
                expected_participant_generation=generation,
                native_turn_id=native_turn_id,
                explanation=payload.get("explanation") or "",
                steps=steps,
            )
        except Exception as exc:
            _log.warning(
                "could not record execution plan for %s/%s: %s",
                work_id,
                native_turn_id,
                exc,
            )
            return _latest_execution_plan(self.store, work_id)

    def interrupt_execution_plan(
        self,
        driver: Any,
        payload: dict[str, Any],
    ) -> ExecutionPlan | None:
        """Persist failed/interrupted turn state without false completion."""

        identity = _driver_identity(driver)
        if identity is None or not isinstance(payload, dict):
            return None
        work_id, provider, participant_id, generation = identity
        native_turn_id = str(payload.get("turnId") or "").strip()
        if not native_turn_id:
            return None
        error = payload.get("error")
        fallback_reason = (
            "Turn failed before the plan completed."
            if str(payload.get("status") or "") == "failed"
            else "Turn interrupted before the plan completed."
        )
        reason = (
            str(error.get("message") or fallback_reason)
            if isinstance(error, dict)
            else fallback_reason
        )
        try:
            return self.store.interrupt_execution_plan(
                work_id,
                participant_id,
                provider=provider,
                expected_participant_generation=generation,
                native_turn_id=native_turn_id,
                reason=reason,
            )
        except Exception as exc:
            _log.warning(
                "could not interrupt execution plan for %s/%s: %s",
                work_id,
                native_turn_id,
                exc,
            )
            return _latest_execution_plan(self.store, work_id)

    def record_goal(
        self,
        *,
        work_id: str,
        provider: str,
        goal: GoalState,
    ) -> WorkEvent | None:
        work = self.store.get_work(work_id)
        if work is None:
            raise ValueError(f"unknown Work: {work_id}")
        safe_goal = _safe_goal(goal)
        updates: dict[str, str] = {}
        if work.objective != safe_goal.objective:
            updates["objective"] = safe_goal.objective
        # the first writer `works.definition_of_done` has ever had.
        # update_work() already computes `starts_new_contract` and already
        # refuses to reset an accepted epoch on an edit, so this needs no
        # migration — the store layer was finished, only nothing ever called it
        # with this field.
        #
        # WRITE-ONLY-WHEN-PRESENT, on purpose. A goal edit that carries no
        # stopping condition must never CLEAR one that is already set: several
        # callers build a GoalState without it (native goal reconciliation, the
        # legacy session-keyed path), and letting any of them blank the field
        # would silently revoke contract acceptance on a tandem Work. Caught by
        # test_ui_goal_edit_does_not_skip_unseen_partner_contribution. Clearing a
        # stopping condition should be an explicit act, not a side effect.
        if (
            safe_goal.definition_of_done
            and work.definition_of_done != safe_goal.definition_of_done
        ):
            updates["definition_of_done"] = safe_goal.definition_of_done
        if work.status != "budgetLimited" and work.status != goal.status:
            updates["status"] = goal.status
        if updates:
            self.store.update_work(work_id, **updates)
        payload = {
            "objective": safe_goal.objective,
            "definition_of_done": safe_goal.definition_of_done,
            "status": safe_goal.status,
            "items": [
                {"text": item.text, "status": item.status} for item in safe_goal.items
            ],
        }
        return self.store.append_event(
            work_id,
            "goal.updated",
            payload,
            # Goal edits are shared UI state, not proof that the selected
            # provider consumed every preceding foreign event. Do not advance
            # its cursor here; only delivered prompts and direct user/model
            # turns have that causal guarantee.
            provider=provider,
        )

    def clear_goal(self, *, work_id: str, provider: str) -> WorkEvent:
        """Clear canonical objective and leave an ordered tombstone for peers."""

        work = self.store.get_work(work_id)
        if work is None:
            raise ValueError(f"unknown Work: {work_id}")
        updates = {"objective": ""} if work.objective else {}
        if work.status != "budgetLimited" and work.status != "active":
            updates["status"] = "active"
        if updates:
            self.store.update_work(work_id, **updates)
        return self.store.append_event(
            work_id,
            "goal.cleared",
            {
                "text": "Shared objective was cleared; do not continue the prior objective.",
            },
            provider=provider,
        )

    def mark_budget_exhausted(
        self,
        *,
        work_id: str,
        provider: str,
        details: dict[str, Any] | None = None,
    ) -> Work:
        """Persist a family breaker so process respawns cannot renew it."""

        try:
            return self.store.mark_budget_exhausted(
                work_id,
                provider=provider,
                details=details if isinstance(details, dict) else None,
            )
        except WorkNotFoundError as exc:
            raise ValueError(f"unknown Work: {work_id}") from exc

    def clear_budget_exhausted(self, *, work_id: str, reason: str = "") -> Work:
        """Reopen a budget-closed Work on explicit operator action."""

        try:
            return self.store.clear_budget_exhausted(work_id, reason=reason)
        except WorkNotFoundError as exc:
            raise ValueError(f"unknown Work: {work_id}") from exc

    def execution_block_reason(self, work_id: str) -> str:
        work = self.store.get_work(work_id)
        if work is None:
            return (
                "Helios could not verify this Work's execution state. "
                "The message was not sent."
            )
        if work.status == "budgetLimited":
            return (
                "This Work reached its execution budget. Clear it from the menu "
                "(Clear budget block) to continue, or start a new Work."
            )
        return ""

    def start_execution_attempt(
        self,
        *,
        work_id: str,
        participant_id: str,
        provider: str,
        participant_generation: int,
        metadata: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        workspace_root: str = "",
        permission_mode: str = "",
    ) -> ExecutionAttempt:
        """Durably admit one provider turn before its first external side effect.

        ``workspace_root`` and the write-intent projection are retained as
        forensic facts. Schema v9 removed the workspace lease, so neither fact
        participates in admission; the actual guards are Work and provider
        lanes plus a recoverable pre-turn checkpoint.
        """

        return self.store.start_execution_attempt(
            work_id,
            participant_id,
            provider=provider,
            expected_participant_generation=participant_generation,
            metadata=metadata,
            attempt_id=attempt_id,
            workspace_root=workspace_root,
            write_intent=permission_mode != "plan",
        )

    def reclaim_orphaned_execution_attempts(self) -> list[ExecutionAttempt]:
        """Free lanes held by a Helios that exited mid-turn. Startup only."""

        return self.store.reclaim_orphaned_execution_attempts()

    def record_execution_dispatch(
        self,
        attempt_id: str,
        *,
        wire_prompt_text: str,
        provider_request_key: str,
    ) -> ExecutionAttempt:
        return self.store.record_execution_dispatch(
            attempt_id,
            wire_prompt_text=wire_prompt_text,
            provider_request_key=provider_request_key,
        )

    def record_execution_acceptance(
        self,
        attempt_id: str,
        *,
        accepted_turn_id: str,
        provider_request_key: str,
    ) -> ExecutionAttempt:
        return self.store.record_execution_acceptance(
            attempt_id,
            accepted_turn_id=accepted_turn_id,
            provider_request_key=provider_request_key,
        )

    def record_execution_stop(
        self,
        attempt_id: str,
        *,
        acknowledgement: dict[str, Any] | None = None,
        queue_disposition: str = "",
    ) -> ExecutionAttempt:
        return self.store.record_execution_stop(
            attempt_id,
            acknowledgement=acknowledgement,
            queue_disposition=queue_disposition,
        )

    def finish_execution_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        terminal_reason: str = "",
        terminal_receipt: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        cost_micro_usd: int | None = None,
        stop_acknowledgement: dict[str, Any] | None = None,
        queue_disposition: str = "",
    ) -> ExecutionAttempt:
        """Persist terminal accounting before exposing a result to the UI."""

        return self.store.finish_execution_attempt(
            attempt_id,
            status=status,
            terminal_reason=terminal_reason,
            terminal_receipt=terminal_receipt,
            usage=usage,
            cost_micro_usd=cost_micro_usd,
            stop_acknowledgement=stop_acknowledgement,
            queue_disposition=queue_disposition,
        )

    def _record_driver_event(
        self,
        driver: Any,
        event_type: str,
        payload: dict[str, Any],
    ) -> WorkEvent | None:
        work_id = str(getattr(driver, "_helios_work_id", "") or "")
        provider = str(
            getattr(driver, "_helios_participant_provider", "") or ""
        )
        participant_id = str(
            getattr(driver, "_helios_participant_id", "") or ""
        )
        generation = getattr(driver, "_helios_participant_generation", None)
        if not work_id or not provider or not participant_id:
            return None
        try:
            return self.store.append_event(
                work_id,
                event_type,
                payload,
                provider=provider,
                emitting_participant_id=participant_id,
                expected_participant_generation=generation,
            )
        except Exception as exc:
            # Transcript delivery must remain available if the compatibility
            # ledger is temporarily unavailable.  At-least-once packet ACKs
            # still ensure peer context itself is never silently consumed.
            _log.warning("could not record %s for %s: %s", event_type, work_id, exc)
            return None


def tag_driver(driver: Any, participant: Participant) -> None:
    """Attach immutable Work routing hints to a provider driver."""

    driver._helios_work_id = participant.work_id
    driver._helios_participant_provider = participant.provider
    driver._helios_participant_id = participant.participant_id
    driver._helios_participant_generation = participant.generation


def _driver_identity(driver: Any) -> tuple[str, str, str, int] | None:
    work_id = str(getattr(driver, "_helios_work_id", "") or "")
    provider = str(getattr(driver, "_helios_participant_provider", "") or "")
    participant_id = str(getattr(driver, "_helios_participant_id", "") or "")
    generation = getattr(driver, "_helios_participant_generation", None)
    if (
        not work_id
        or not provider
        or not participant_id
        or not isinstance(generation, int)
        or generation <= 0
    ):
        return None
    return work_id, provider, participant_id, generation


def _wrap_goal_context(
    text: str,
    goal: GoalState | None,
    *,
    include_envelope: bool,
    include_supplement: bool,
) -> str:
    if include_envelope:
        return session_goals.wrap_user_prompt(text, goal)
    if include_supplement:
        return session_goals.wrap_goal_supplement(text, goal)
    return text


def _latest_execution_plan(store: WorkStore, work_id: str) -> ExecutionPlan | None:
    """Preserve the last durable truth when a late/new write is rejected."""

    try:
        return store.get_execution_plan(work_id)
    except Exception as exc:
        _log.warning("could not recover latest execution plan for %s: %s", work_id, exc)
        return None


def _collaboration_update(event: WorkEvent) -> CollaborationUpdate:
    payload = event.payload
    text = _event_text(event)
    artifact_ids: list[str] = []
    one_artifact = payload.get("artifact_id")
    if isinstance(one_artifact, str) and one_artifact:
        artifact_ids.append(one_artifact)
    many_artifacts = payload.get("artifact_ids")
    if isinstance(many_artifacts, list):
        artifact_ids.extend(
            item for item in many_artifacts if isinstance(item, str) and item
        )
    return CollaborationUpdate(
        seq=event.seq,
        kind=event.event_type,
        provider=event.provider,
        text=text,
        artifact_ids=tuple(dict.fromkeys(artifact_ids)),
    )


def _is_control_plane_audit_event(event: WorkEvent) -> bool:
    """Keep control-plane receipts out of model-authored context."""

    return (
        event.control_plane and event.event_type in CONTROL_PLANE_AUDIT_EVENT_TYPES
    )


def _contiguous_prompt_boundary(
    starting_seq: int,
    raw_events: tuple[WorkEvent, ...],
    rendered_through_seq: int,
) -> int:
    """Return the raw ledger prefix represented or intentionally elided.

    Execution-attempt events are durable audit records, not collaborator
    claims. They can be acknowledged without rendering, but never past an
    ordinary event that the bounded packet did not include. This preserves the
    same no-gap guarantee when an event lands between snapshot and provider
    acceptance.
    """

    boundary = starting_seq
    for event in raw_events:
        if _is_control_plane_audit_event(event):
            boundary = event.seq
            continue
        if event.seq <= rendered_through_seq:
            boundary = event.seq
            continue
        break
    return boundary


def _event_text(event: WorkEvent) -> str:
    payload = event.payload
    for key in ("text", "objective", "definition_of_done", "summary", "claim"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            if event.event_type == "goal.updated" and key == "objective":
                return f"Shared objective: {value} (status: {payload.get('status', 'active')})"
            return value
    participant = payload.get("provider")
    if event.event_type in {"participant.created", "binding.attached"} and participant:
        return f"{participant} participant binding is available"
    if event.event_type == "artifact.recorded":
        return f"Artifact recorded ({payload.get('media_type', 'unknown type')})"
    # Preserve cursor progress with a compact, non-secret description rather
    # than serializing arbitrary payloads into another model's prompt.
    return f"{event.event_type} recorded in the Work ledger"


def _bounded_text(value: Any) -> tuple[str, bool, bool]:
    text, redacted = scrub_sensitive(value)
    text = text.strip()
    if len(text) <= _MAX_LEDGER_TEXT:
        return text, redacted, False
    return text[: _MAX_LEDGER_TEXT - 1].rstrip() + "…", redacted, True


def _safe_goal(goal: GoalState | None) -> GoalState | None:
    if goal is None:
        return None
    objective, _ = scrub_sensitive(goal.objective)
    # Scrubbed like the objective, and named explicitly: this function rebuilds
    # the dataclass field by field, so a new field that is not listed here is
    # silently dropped on its way to the store and the envelope.
    definition_of_done, _ = scrub_sensitive(goal.definition_of_done)
    items = []
    for item in goal.items:
        item_text, _ = scrub_sensitive(item.text)
        items.append(session_goals.GoalPlanItem(item_text, item.status))
    return GoalState(
        objective=objective,
        definition_of_done=definition_of_done,
        status=goal.status,
        items=items,
        cwd=goal.cwd,
        provider=goal.provider,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )


def _accepted_state(work: Work, goal: GoalState | None) -> str:
    objective = work.objective.strip()
    lines = [
        f"Status: {work.status}",
        f"Mode: {work.mode}",
        f"Current lead: {work.lead_provider}",
        (
            f"Objective: {objective}"
            if objective
            else "Objective: unset; do not infer or continue prior scope"
        ),
    ]
    if work.definition_of_done:
        lines.append(f"Definition of done: {work.definition_of_done}")
    if goal is not None and goal.items:
        lines.append("Accepted checklist:")
        lines.extend(
            f"- [{item.status}] {item.text}" for item in goal.items[:20]
        )
    return "\n".join(lines)
