"""Durable provider-neutral Work, participant, event, and artifact storage.

This is the embedded v0.27 foundation.  The same API is intentionally usable
behind the future supervisor boundary, while today's GTK client can adopt Work
identity without waiting for that process split.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from helios.backend.execution_plan import (
    ExecutionPlan,
    interrupt_steps,
    normalize_explanation,
    plan_id_for,
    plan_status_for,
    reconcile_steps,
    steps_from_json,
    steps_to_json,
)
from helios.backend.project_perms import canonical_cwd
from helios.backend.sensitive_text import scrub_sensitive
from helios.paths import state_dir

SCHEMA_VERSION = 11
_CLAUDE_PROVIDER = "anthropic"

# Per-provider concurrency ceilings for the durable admission ledger.
#
# v0.57.1 put every non-Claude provider in ONE database-wide lane, so a second
# GPT Work — or a GPT Work beside an OpenRouter Work — was denied. The
# 2026-08-03 incident evidence does not support that as the containment point:
# 81 Helios subagents produced 89.4% of the runaway workload while the 4 roots
# produced 10%. Root fan-out is bounded per process instead (`agents.enabled`
# false, the 200k billed-token breaker, the native dollar cap), so admission
# only has to stop an unbounded burst, not concurrency itself.
#
# Claude stays uncapped: per-Work admission is its only gate, unchanged from
# v0.57.1. Each canonical non-Claude provider gets its own bounded lane.
# Anything unrecognized — a typo, a future provider id — shares ONE fail-closed
# slot, so a free-form identifier still cannot mint extra concurrency.
_PROVIDER_CONCURRENCY_LIMITS = {"openai": 4, "openrouter": 4}
# Not a valid provider id (`_required_text` rejects empty), so a real provider
# can never collide with the shared unknown lane.
_UNKNOWN_PROVIDER_LANE = ""
_UNKNOWN_PROVIDER_LIMIT = 1
_KNOWN_PROVIDERS = (_CLAUDE_PROVIDER, *sorted(_PROVIDER_CONCURRENCY_LIMITS))
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RECOVERY_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RECOVERY_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+\-=]{0,999}$")
_SQLITE_BUSY_RETRIES = 8
_SQLITE_BUSY_BASE_DELAY = 0.025
_SQLITE_BUSY_MAX_DELAY = 0.25
_RECOVERY_TEXT_LIMIT = 1_000
_RECOVERY_JSON_LIMIT = 16_000
EXECUTION_ATTEMPT_AUDIT_EVENT_TYPES = frozenset(
    {
        "execution.attempt.accepted",
        "execution.attempt.dispatch-prepared",
        "execution.attempt.finished",
        "execution.attempt.started",
        "execution.attempt.stop-recorded",
    }
)
CONTROL_PLANE_AUDIT_EVENT_TYPES = frozenset(
    {*EXECUTION_ATTEMPT_AUDIT_EVENT_TYPES, "context.degraded", "work.forked"}
)
_EXECUTION_ATTEMPT_EVENT_PREFIX = "execution.attempt."
_EXECUTION_METADATA_FIELDS = {
    "caller": "code",
    "effort": "label",
    "execution_surface": "code",
    "model": "label",
    "service_tier": "code",
    "transport": "label",
}
_TERMINAL_RECEIPT_FIELDS = {
    "evidence_type": "terminal_evidence",
    "provider_status": "code",
    "request_id": "identifier",
    "turn_id": "identifier",
    "native_id": "identifier",
    "response_id": "identifier",
    "finish_reason": "code",
    "error_code": "code",
    "http_status": "nonnegative_int",
    "retryable": "bool",
}
_USAGE_RECEIPT_FIELDS = {
    "input_tokens": "nonnegative_int",
    "output_tokens": "nonnegative_int",
    "reasoning_tokens": "nonnegative_int",
    "reasoning_output_tokens": "nonnegative_int",
    "cached_input_tokens": "nonnegative_int",
    "cache_read_input_tokens": "nonnegative_int",
    "cache_creation_input_tokens": "nonnegative_int",
    "total_tokens": "nonnegative_int",
    "tool_calls": "nonnegative_int",
    "turns": "nonnegative_int",
    "requests": "nonnegative_int",
    "duration_ms": "nonnegative_int",
}
_STOP_ACKNOWLEDGEMENT_FIELDS = {
    "acknowledged": "bool",
    "cancellation_confirmed": "bool",
    "provider_status": "code",
    "request_id": "identifier",
    "turn_id": "identifier",
    "native_id": "identifier",
    "signal": "code",
    "error_code": "code",
    "escalated": "bool",
}
_TERMINAL_EVIDENCE_TYPES = frozenset({"provider_terminal", "verified_rejection"})
_ORPHANED_TERMINAL_REASON = "orphaned.helios_exited"
_QUEUE_DISPOSITIONS = frozenset({"held", "restored", "released", "discarded"})
_FINAL_QUEUE_DISPOSITIONS = frozenset({"restored", "released", "discarded"})
_RAW_CONTENT_KEYS = frozenset(
    {
        "body",
        "content",
        "contents",
        "conversation",
        "input",
        "inputs",
        "message_history",
        "messages",
        "last_message",
        "prompt",
        "prompt_text",
        "prompts",
        "raw",
        "request",
        "request_body",
        "response",
        "response_body",
        "result",
        "text",
        "transcript",
        "user_message",
        "user_text",
    }
)
_SECRET_METADATA_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "auth_token",
        "client_secret",
        "cookie",
        "password",
        "passwd",
        "proxy_authorization",
        "secret",
        "set_cookie",
        "token",
        "x_api_key",
        "x_auth_token",
    }
)
_SENSITIVE_HEADER_CONTAINERS = frozenset(
    {"headers", "request_headers", "response_headers"}
)


class WorkStoreError(RuntimeError):
    """Base error for invalid or conflicting Work operations."""


class WorkNotFoundError(WorkStoreError):
    pass


class ParticipantNotFoundError(WorkStoreError):
    pass


class StaleParticipantError(WorkStoreError):
    """A callback targeted a participant binding that has since changed."""


class ExecutionAdmissionError(WorkStoreError):
    """A different Helios execution attempt already owns the single slot."""


@dataclass(frozen=True, slots=True)
class Work:
    work_id: str
    objective: str
    definition_of_done: str
    cwd: str
    mode: str
    status: str
    lead_provider: str
    contract_epoch: int
    contract_start_seq: int
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    work_id: str
    provider: str
    native_session_id: str
    native_thread_id: str
    role: str
    status: str
    generation: int
    last_event_seq: int
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    @property
    def native_id(self) -> str:
        return self.native_thread_id or self.native_session_id


@dataclass(frozen=True, slots=True)
class WorkEvent:
    event_id: str
    work_id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    provider: str
    emitting_participant_id: str
    control_plane: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class CollaborationSnapshot:
    """One transactionally consistent Work/participant prompt view."""

    work: Work
    participant: Participant
    events: tuple[WorkEvent, ...]
    contract_accepted: bool


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    work_id: str
    digest: str
    media_type: str
    size: int
    path: Path
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class ExecutionAttempt:
    """One durably admitted provider turn.

    ``running`` is deliberately both the reservation and execution state.  The
    row is committed before a provider side effect and remains running across a
    crash or ambiguous provider acknowledgement, so Helios fails closed until
    an operator can reconcile it.
    """

    attempt_id: str
    work_id: str
    participant_id: str
    participant_generation: int
    provider: str
    status: str
    terminal_reason: str
    metadata: dict[str, Any]
    prompt_digest: str
    native_binding_id: str
    provider_request_key: str
    accepted_turn_id: str
    terminal_receipt: dict[str, Any]
    usage: dict[str, Any]
    cost_micro_usd: int | None
    stop_acknowledgement: dict[str, Any]
    queue_disposition: str
    started_at: str
    finished_at: str
    updated_at: str
    # v7 workspace write lease. Defaults keep every existing
    # construction valid; '' means the root was not identified.
    workspace_root: str = ""
    write_intent: bool = False


class WorkStore:
    """Thread-safe SQLite store with immutable events and artifacts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_dir() / "work" / "work.db"
        self._root = self.path.parent
        self._root.mkdir(parents=True, exist_ok=True)
        _chmod(self._root, 0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=0.25,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 250")
            self._retry_sqlite_busy(
                lambda: self._conn.execute("PRAGMA journal_mode = WAL").fetchone(),
                operation="enable WAL",
            )
            self._conn.execute("PRAGMA synchronous = FULL")
            self._initialize_schema()
            # Preserve the established runtime write-wait budget. The shorter
            # timeout above is only for bounded initialization retries.
            self._conn.execute("PRAGMA busy_timeout = 10000")
        except Exception:
            self._conn.close()
            raise
        _chmod(self.path, 0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> WorkStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def create_work(
        self,
        *,
        objective: str = "",
        definition_of_done: str = "",
        cwd: str = "",
        mode: str = "single",
        status: str = "active",
        lead_provider: str = "anthropic",
        metadata: dict[str, Any] | None = None,
        work_id: str | None = None,
    ) -> Work:
        work_id = _validate_id(work_id or _new_id("wrk"), "work_id")
        now = _now()
        payload = {
            "objective": _text(objective),
            "definition_of_done": _text(definition_of_done),
            "cwd": _text(cwd),
            "mode": _required_text(mode, "mode"),
            "status": _required_text(status, "status"),
            "lead_provider": _required_text(lead_provider, "lead_provider"),
            "metadata": _mapping(metadata),
        }
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO works (
                        work_id, objective, definition_of_done, cwd, mode,
                        status, lead_provider, contract_epoch,
                        contract_start_seq, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                    """,
                    (
                        work_id,
                        payload["objective"],
                        payload["definition_of_done"],
                        payload["cwd"],
                        payload["mode"],
                        payload["status"],
                        payload["lead_provider"],
                        _json(payload["metadata"]),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkStoreError(f"work already exists: {work_id}") from exc
            created = self._append_event_conn(
                conn,
                work_id,
                "work.created",
                payload,
                created_at=now,
            )
            if _contract_fields_accepted(
                objective=payload["objective"],
                definition_of_done=payload["definition_of_done"],
                cwd=payload["cwd"],
                mode=payload["mode"],
                status=payload["status"],
            ):
                conn.execute(
                    """
                    UPDATE works
                    SET contract_epoch = 1, contract_start_seq = ?
                    WHERE work_id = ?
                    """,
                    (created.seq, work_id),
                )
            row = self._work_row(conn, work_id)
        return _work(row)

    def ensure_work(self, work_id: str | None = None, **creation_fields: Any) -> Work:
        if work_id:
            existing = self.get_work(work_id)
            if existing is not None:
                return existing
        try:
            return self.create_work(work_id=work_id, **creation_fields)
        except WorkStoreError:
            # Another process may have created the same explicit id after our
            # optimistic read. Return the winner instead of failing get-or-create.
            existing = self.get_work(work_id) if work_id else None
            if existing is not None:
                return existing
            raise

    def ensure_work_for_native(
        self,
        provider: str,
        native_id: str,
        *,
        cwd: str = "",
        model: str = "",
        native_kind: str = "",
    ) -> Work:
        """Atomically claim an unseen native id or return its existing Work."""

        provider = _required_text(provider, "provider")
        native_id = _required_text(native_id, "native_id")
        with self._transaction() as conn:
            existing_id = self._work_id_for_native_conn(conn, provider, native_id)
            if existing_id:
                return _work(self._work_row(conn, existing_id))

            work_id = _new_id("wrk")
            participant_id = _new_id("part")
            now = _now()
            conn.execute(
                """
                INSERT INTO works (
                    work_id, objective, definition_of_done, cwd, mode,
                    status, lead_provider, contract_epoch,
                    contract_start_seq, metadata_json, created_at, updated_at
                ) VALUES (?, '', '', ?, 'single', 'active', ?, 0, 0, '{}', ?, ?)
                """,
                (work_id, _text(cwd), provider, now, now),
            )
            self._append_event_conn(
                conn,
                work_id,
                "work.created",
                {
                    "objective": "",
                    "definition_of_done": "",
                    "cwd": _text(cwd),
                    "mode": "single",
                    "status": "active",
                    "lead_provider": provider,
                    "metadata": {},
                },
                created_at=now,
            )
            native_kind = native_kind or (
                "thread" if provider == "openai" else "session"
            )
            if native_kind not in {"session", "thread"}:
                raise ValueError("native_kind must be 'session' or 'thread'")
            is_thread = native_kind == "thread"
            native_session_id = "" if is_thread else native_id
            native_thread_id = native_id if is_thread else ""
            metadata = {"model": model} if model else {}
            conn.execute(
                """
                INSERT INTO participants (
                    participant_id, work_id, provider, native_session_id,
                    native_thread_id, role, status, generation,
                    last_event_seq, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'lead', 'active', 1, 0, ?, ?, ?)
                """,
                (
                    participant_id,
                    work_id,
                    provider,
                    native_session_id,
                    native_thread_id,
                    _json(metadata),
                    now,
                    now,
                ),
            )
            self._activate_binding_conn(
                conn,
                participant_id,
                provider,
                "thread" if is_thread else "session",
                native_id,
                generation=1,
                created_at=now,
            )
            self._append_event_conn(
                conn,
                work_id,
                "participant.created",
                {
                    "participant_id": participant_id,
                    "provider": provider,
                    "role": "lead",
                    "native_session_id": native_session_id,
                    "native_thread_id": native_thread_id,
                },
                provider=provider,
                created_at=now,
            )
            return _work(self._work_row(conn, work_id))

    def get_work(self, work_id: str) -> Work | None:
        if not work_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM works WHERE work_id = ?", (work_id,)
            ).fetchone()
        return _work(row) if row is not None else None

    def list_works(self, status: str | None = None, limit: int | None = None) -> list[Work]:
        sql = "SELECT * FROM works"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, work_id"
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_work(row) for row in rows]

    def update_work(
        self,
        work_id: str,
        *,
        objective: str | None = None,
        definition_of_done: str | None = None,
        cwd: str | None = None,
        mode: str | None = None,
        status: str | None = None,
        lead_provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Work:
        changes = {
            key: value
            for key, value in {
                "objective": objective,
                "definition_of_done": definition_of_done,
                "cwd": cwd,
                "mode": mode,
                "status": status,
                "lead_provider": lead_provider,
            }.items()
            if value is not None
        }
        if metadata is not None:
            changes["metadata"] = _mapping(metadata)
        with self._transaction() as conn:
            row = self._work_row(conn, work_id)
            current = _work(row)
            if current.status == "budgetLimited":
                # Budget exhaustion is terminal for this Work identity.  A stale
                # goal/UI caller may still update descriptive fields, but it
                # cannot revive execution; renewal requires a newly-created Work.
                changes.pop("status", None)
            if not changes:
                return current
            contract_fields = {
                "objective": current.objective,
                "definition_of_done": current.definition_of_done,
                "cwd": current.cwd,
                "mode": current.mode,
                "status": current.status,
            }
            projected_contract_fields = {
                key: _text(changes.get(key, value))
                for key, value in contract_fields.items()
            }
            current_contract_accepted = _contract_fields_accepted(**contract_fields)
            projected_contract_accepted = _contract_fields_accepted(
                **projected_contract_fields
            )
            # Editing an already-accepted objective/DoD does not erase unseen
            # partner evidence. A new epoch begins only after collaboration was
            # disabled/incomplete and is explicitly accepted again.
            starts_new_contract = (
                projected_contract_accepted and not current_contract_accepted
            )
            assignments: list[str] = []
            values: list[Any] = []
            for key, value in changes.items():
                column = "metadata_json" if key == "metadata" else key
                assignments.append(f"{column} = ?")
                values.append(_json(value) if key == "metadata" else _text(value))
            now = _now()
            assignments.append("updated_at = ?")
            values.extend([now, work_id])
            conn.execute(
                f"UPDATE works SET {', '.join(assignments)} WHERE work_id = ?",
                values,
            )
            updated = self._append_event_conn(
                conn,
                work_id,
                "work.updated",
                changes,
                created_at=now,
            )
            if starts_new_contract:
                conn.execute(
                    """
                    UPDATE works
                    SET contract_epoch = ?, contract_start_seq = ?
                    WHERE work_id = ?
                    """,
                    (max(0, current.contract_epoch) + 1, updated.seq, work_id),
                )
            row = self._work_row(conn, work_id)
        return _work(row)

    def mark_budget_exhausted(
        self,
        work_id: str,
        *,
        provider: str = "",
        details: dict[str, Any] | None = None,
    ) -> Work:
        """Atomically trip the durable, one-way budget breaker and audit it."""

        provider = _text(provider)
        payload = _mapping(details) if details is not None else {}
        # The explicit argument is authoritative audit identity even if a
        # provider-shaped key was present in untrusted diagnostic details.
        payload["provider"] = provider
        with self._transaction() as conn:
            current = _work(self._work_row(conn, work_id))
            if current.status == "budgetLimited":
                recorded = conn.execute(
                    """
                    SELECT 1 FROM events
                    WHERE work_id = ? AND event_type = 'budget.exhausted'
                    LIMIT 1
                    """,
                    (work_id,),
                ).fetchone()
                if recorded is None:
                    self._append_event_conn(
                        conn,
                        work_id,
                        "budget.exhausted",
                        payload,
                        provider=provider,
                    )
                return current
            now = _now()
            conn.execute(
                "UPDATE works SET status = ?, updated_at = ? WHERE work_id = ?",
                ("budgetLimited", now, work_id),
            )
            # Preserve the ordinary state-change ledger entry while committing
            # the dedicated breaker event in the very same write transaction.
            self._append_event_conn(
                conn,
                work_id,
                "work.updated",
                {"status": "budgetLimited"},
                created_at=now,
            )
            self._append_event_conn(
                conn,
                work_id,
                "budget.exhausted",
                payload,
                provider=provider,
                created_at=now,
            )
            row = self._work_row(conn, work_id)
        return _work(row)

    def clear_budget_exhausted(self, work_id: str, *, reason: str = "") -> Work:
        """Reopen a Work the budget breaker closed, on explicit operator action.

        The breaker is deliberately one-way for a *process*: a respawn must not
        renew it by itself. But with no reset path at all, one trip abandons a
        conversation permanently — which is exactly what happened on 2026-08-05
        when a notional dollar figure closed a live Work. A human decision is
        the renewal, and it is audited like any other state change.

        No-ops on a Work that is not ``budgetLimited``, so this cannot revive a
        Work that some other rule closed.
        """

        with self._transaction() as conn:
            current = _work(self._work_row(conn, work_id))
            if current.status != "budgetLimited":
                return current
            now = _now()
            conn.execute(
                "UPDATE works SET status = ?, updated_at = ? WHERE work_id = ?",
                ("active", now, work_id),
            )
            self._append_event_conn(
                conn,
                work_id,
                "work.updated",
                {"status": "active"},
                created_at=now,
            )
            self._append_event_conn(
                conn,
                work_id,
                "budget.cleared",
                {"reason": _text(reason) or "operator cleared the budget breaker"},
                created_at=now,
            )
            row = self._work_row(conn, work_id)
        return _work(row)

    def start_execution_attempt(
        self,
        work_id: str,
        participant_id: str,
        *,
        provider: str,
        expected_participant_generation: int,
        metadata: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        workspace_root: str = "",
        write_intent: bool = False,
    ) -> ExecutionAttempt:
        """Atomically reserve the Work, workspace, and provider execution lanes.

        The committed ``running`` row is the reservation.  It intentionally has
        no automatic expiry: after a process crash or ambiguous provider ACK,
        guessing that execution stopped would recreate the runaway condition
        this gate exists to prevent.  Every Work has one lane, so a Work never
        double-dispatches.  Claude may use unrelated Work lanes concurrently;
        each canonical non-Claude provider has a bounded concurrent ceiling and
        every unrecognized provider id shares one slot, so a burst or a
        misspelled/future provider cannot escape the limit across Works or
        Helios processes.
        """

        work_id = _validate_id(work_id, "work_id")
        participant_id = _validate_id(participant_id, "participant_id")
        provider = _required_text(provider, "provider")
        attempt_id = _validate_id(attempt_id or _new_id("attempt"), "attempt_id")
        if expected_participant_generation <= 0:
            raise ValueError("expected_participant_generation must be positive")
        # Canonicalise here rather than trusting the caller: two aliases of one
        # directory (symlink, trailing '..') must take the SAME lease or the
        # lease is decorative.
        workspace_root = canonical_cwd(_text(workspace_root))
        write_intent = bool(write_intent)
        safe_metadata = _recovery_mapping(
            metadata,
            "execution metadata",
            fields=_EXECUTION_METADATA_FIELDS,
        )
        with self._transaction() as conn:
            participant = _participant(self._participant_row(conn, participant_id))
            _check_participant_expectations(
                participant,
                expected_generation=expected_participant_generation,
            )
            if participant.work_id != work_id or participant.provider != provider:
                raise StaleParticipantError(
                    "execution participant no longer belongs to this Work/provider"
                )
            # Despite the historical column name, this is the provider-native
            # session/thread identity visible on the admitted participant, not
            # native_bindings.binding_id. It is storage-owned so callers cannot
            # bind an attempt to an unverified or stale native identity.
            safe_native_binding = _recovery_identifier(
                participant.native_id,
                "participant native identity",
                allow_empty=True,
            )
            existing = conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                attempt = _execution_attempt(existing)
                if (
                    attempt.work_id == work_id
                    and attempt.participant_id == participant_id
                    and attempt.participant_generation
                    == expected_participant_generation
                    and attempt.provider == provider
                    and attempt.status == "running"
                ):
                    if metadata is not None and attempt.metadata != safe_metadata:
                        raise WorkStoreError(
                            f"execution attempt metadata conflicts: {attempt_id}"
                        )
                    if (
                        attempt.native_binding_id
                        and attempt.native_binding_id != safe_native_binding
                    ):
                        raise WorkStoreError(
                            f"execution attempt native binding conflicts: {attempt_id}"
                        )
                    return attempt
                raise WorkStoreError(
                    f"execution attempt identity conflicts: {attempt_id}"
                )

            work = _work(self._work_row(conn, work_id))
            if work.status == "budgetLimited":
                raise ExecutionAdmissionError(
                    "This Work reached its execution budget. Start a new bounded Work."
                )
            conflict = _execution_admission_conflict_conn(
                conn,
                work_id=work_id,
                provider=provider,
                workspace_root=workspace_root,
                write_intent=write_intent,
            )
            if conflict is not None:
                owner, scope = conflict
                raise ExecutionAdmissionError(
                    _execution_denial_message(owner, scope=scope)
                )

            now = _now()
            try:
                conn.execute(
                    """
                    INSERT INTO execution_attempts (
                        attempt_id, work_id, participant_id,
                        participant_generation, provider, status,
                        terminal_reason, metadata_json, prompt_digest,
                        native_binding_id, provider_request_key,
                        accepted_turn_id, terminal_receipt_json, usage_json,
                        cost_micro_usd, stop_acknowledgement_json,
                        queue_disposition, started_at, finished_at, updated_at,
                        workspace_root, write_intent
                    ) VALUES (
                        ?, ?, ?, ?, ?, 'running', '', ?, ?, ?, ?, '', '{}',
                        '{}', NULL, '{}', '', ?, '', ?, ?, ?
                    )
                    """,
                    (
                        attempt_id,
                        work_id,
                        participant_id,
                        expected_participant_generation,
                        provider,
                        _json(safe_metadata),
                        "",
                        safe_native_binding,
                        "",
                        now,
                        now,
                        workspace_root,
                        1 if write_intent else 0,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The partial unique indexes are the final cross-connection
                # guards. Re-resolve only a conflict that applies to this
                # request; an unrelated active lane must never hide another
                # integrity failure.
                conflict = _execution_admission_conflict_conn(
                    conn,
                    work_id=work_id,
                    provider=provider,
                    workspace_root=workspace_root,
                    write_intent=write_intent,
                )
                if conflict is not None:
                    owner, scope = conflict
                    raise ExecutionAdmissionError(
                        _execution_denial_message(owner, scope=scope)
                    ) from exc
                raise
            self._append_event_conn(
                conn,
                work_id,
                "execution.attempt.started",
                {
                    "attempt_id": attempt_id,
                    "participant_id": participant_id,
                    "participant_generation": expected_participant_generation,
                    "provider": provider,
                    "metadata": safe_metadata,
                    "prompt_digest": "",
                    "native_binding_id": safe_native_binding,
                    "provider_request_key": "",
                },
                provider=provider,
                emitting_participant_id=participant_id,
                expected_participant_generation=expected_participant_generation,
                created_at=now,
                allow_reserved_event_type=True,
            )
            row = conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _execution_attempt(row)

    def initialize_openrouter_budget(self, attempt_id: str) -> int:
        """Distinguish guarded, definitely-unsent attempts from legacy unknowns."""
        with self._transaction() as conn:
            attempt = self._openrouter_budget_attempt_conn(conn, attempt_id)
            if attempt.status != "running":
                raise WorkStoreError("OpenRouter budget initialization requires a running attempt")
            used = self._openrouter_spend_conn(conn, attempt)
            conn.execute(
                "INSERT OR IGNORE INTO openrouter_budget_attempts(attempt_id) VALUES (?)",
                (attempt_id,),
            )
            return used

    def openrouter_spend_micro_usd(self, attempt_id: str) -> int:
        """Actual charges plus unreconciled reservations for this entire Work.

        The current attempt is admitted before this read. Older attempts with
        unknown spend cannot be silently treated as zero after an upgrade.
        """
        with self._lock:
            attempt = self._openrouter_budget_attempt_conn(self._conn, attempt_id)
            return self._openrouter_spend_conn(self._conn, attempt)

    def _openrouter_budget_attempt_conn(self, conn, attempt_id: str) -> ExecutionAttempt:
        row = conn.execute(
            "SELECT * FROM execution_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None or row["provider"] != "openrouter":
            raise WorkStoreError("OpenRouter budget requires an admitted OpenRouter attempt")
        return _execution_attempt(row)

    def _openrouter_spend_conn(self, conn, attempt: ExecutionAttempt) -> int:
        total = conn.execute(
            """SELECT COALESCE(SUM(b.charge_micro_usd), 0)
               FROM openrouter_request_budgets b JOIN execution_attempts a
               ON a.attempt_id = b.attempt_id WHERE a.work_id = ?""",
            (attempt.work_id,),
        ).fetchone()[0]
        legacy = conn.execute(
            """SELECT a.* FROM execution_attempts a
               WHERE a.work_id = ? AND a.provider = 'openrouter'
                 AND a.attempt_id != ? AND NOT EXISTS (
                     SELECT 1 FROM openrouter_request_budgets b
                     WHERE b.attempt_id = a.attempt_id)
                 AND NOT EXISTS (SELECT 1 FROM openrouter_budget_attempts i
                                 WHERE i.attempt_id = a.attempt_id)""",
            (attempt.work_id, attempt.attempt_id),
        ).fetchall()
        for row in legacy:
            previous = _execution_attempt(row)
            if previous.cost_micro_usd is not None:
                total += previous.cost_micro_usd
            elif previous.prompt_digest and previous.terminal_receipt.get("evidence_type") not in {
                "local_abort", "verified_rejection",
            }:
                raise WorkStoreError(
                    "This Work has older OpenRouter activity with unknown spend. "
                    "Start a new bounded Work; its cost cannot be assumed to be zero."
                )
        return int(total)

    def reserve_openrouter_request(
        self, attempt_id: str, request_id: str, amount_micro_usd: int,
    ) -> int:
        """Reserve the complete request before HTTP under the admission write lock.

        Reservations survive process loss and orphan-attempt reclamation. A
        provider rejection or a complete cost receipt is required to reduce one.
        """
        from helios.backend.openrouter.budget import (
            WORK_COST_LIMIT_MICRO_USD, SpendLimitReached,
        )

        request_id = _validate_id(request_id, "budget request_id")
        if type(amount_micro_usd) is not int or not 0 <= amount_micro_usd <= WORK_COST_LIMIT_MICRO_USD:
            raise SpendLimitReached("OpenRouter request exceeds the Work spend limit")
        with self._transaction() as conn:
            attempt = self._openrouter_budget_attempt_conn(conn, attempt_id)
            participant = _participant(self._participant_row(conn, attempt.participant_id))
            work = conn.execute("SELECT status FROM works WHERE work_id = ?", (attempt.work_id,)).fetchone()
            if attempt.status != "running" or not attempt.prompt_digest:
                raise WorkStoreError("OpenRouter budget reservation requires durable dispatch")
            if work["status"] == "budgetLimited":
                raise SpendLimitReached("OpenRouter Work is already budget limited")
            if (
                participant.work_id != attempt.work_id
                or participant.provider != attempt.provider
                or participant.generation != attempt.participant_generation
                or participant.status != "active"
            ):
                raise StaleParticipantError("OpenRouter participant changed before request")
            used = self._openrouter_spend_conn(conn, attempt)
            if used + amount_micro_usd > WORK_COST_LIMIT_MICRO_USD:
                raise SpendLimitReached("OpenRouter request exceeds the Work spend limit")
            conn.execute(
                "INSERT OR IGNORE INTO openrouter_budget_attempts(attempt_id) VALUES (?)",
                (attempt_id,),
            )
            # A reused id is never permission to send a second HTTP request.
            conn.execute(
                """INSERT INTO openrouter_request_budgets
                   (request_id, attempt_id, reserved_micro_usd, charge_micro_usd,
                    state, created_at, updated_at) VALUES (?, ?, ?, ?, 'reserved', ?, ?)""",
                (request_id, attempt_id, amount_micro_usd, amount_micro_usd, _now(), _now()),
            )
            return used + amount_micro_usd

    def settle_openrouter_request(
        self, attempt_id: str, request_id: str, *, cost_micro_usd: int,
        rejected: bool = False,
    ) -> int:
        """Record complete reported cost, or a verified pre-response rejection.

        A delayed receipt is bound to its original attempt, independently of
        whether the participant has since rebound. Uncertain requests retain
        their reservation; callers must never settle them as a rejection.
        """
        if type(cost_micro_usd) is not int or cost_micro_usd < 0 or (rejected and cost_micro_usd):
            raise WorkStoreError("Invalid OpenRouter cost receipt")
        state = "rejected" if rejected else "reported"
        with self._transaction() as conn:
            attempt = self._openrouter_budget_attempt_conn(conn, attempt_id)
            row = conn.execute(
                "SELECT * FROM openrouter_request_budgets WHERE request_id = ? AND attempt_id = ?",
                (request_id, attempt_id),
            ).fetchone()
            if row is None:
                raise WorkStoreError("Unknown OpenRouter request reservation")
            if row["state"] != "reserved":
                if row["state"] != state or row["charge_micro_usd"] != cost_micro_usd:
                    raise WorkStoreError("Conflicting OpenRouter cost receipt")
            else:
                conn.execute(
                    """UPDATE openrouter_request_budgets SET charge_micro_usd = ?,
                       state = ?, updated_at = ? WHERE request_id = ?""",
                    (cost_micro_usd, state, _now(), request_id),
                )
            return self._openrouter_spend_conn(conn, attempt)

    def record_execution_dispatch(
        self,
        attempt_id: str,
        *,
        wire_prompt_text: str,
        provider_request_key: str,
    ) -> ExecutionAttempt:
        """Persist a metadata-only dispatch identity before provider I/O.

        ``wire_prompt_text`` is the exact UTF-8 text that will cross the
        provider boundary, after Work context/envelopes are constructed and
        without normalization. Only its SHA-256 digest is retained. The row is
        updated before I/O, so any later transport ambiguity remains blocking.
        """

        return self._record_execution_recovery(
            attempt_id,
            event_type="execution.attempt.dispatch-prepared",
            running_only=True,
            prompt_digest=_prompt_digest(wire_prompt_text),
            provider_request_key=_recovery_identifier(
                provider_request_key,
                "provider request key",
            ),
        )

    def record_execution_acceptance(
        self,
        attempt_id: str,
        *,
        accepted_turn_id: str,
        provider_request_key: str,
    ) -> ExecutionAttempt:
        """Bind a provider acceptance identity without releasing the slot.

        Acceptance is causal only after the exact wire digest has been
        committed and while the attempt remains running. A late/new acceptance
        can therefore never enrich an already released row.
        """

        safe_turn_id = _recovery_identifier(
            accepted_turn_id,
            "accepted turn id",
        )
        safe_request_key = _recovery_identifier(
            provider_request_key,
            "provider request key",
        )
        return self._record_execution_recovery(
            attempt_id,
            event_type="execution.attempt.accepted",
            running_only=True,
            require_dispatch=True,
            accepted_turn_id=safe_turn_id,
            provider_request_key=safe_request_key,
        )

    def record_execution_stop(
        self,
        attempt_id: str,
        *,
        acknowledgement: dict[str, Any] | None = None,
        queue_disposition: str = "",
    ) -> ExecutionAttempt:
        """Persist stop acknowledgement/queue fate without inferring terminality."""

        safe_ack = _recovery_mapping(
            acknowledgement,
            "stop acknowledgement",
            fields=_STOP_ACKNOWLEDGEMENT_FIELDS,
        )
        safe_queue = _queue_disposition(queue_disposition)
        if not safe_ack and not safe_queue:
            raise ValueError("execution stop record requires acknowledgement or queue")
        return self._record_execution_recovery(
            attempt_id,
            event_type="execution.attempt.stop-recorded",
            running_only=True,
            stop_acknowledgement=safe_ack,
            queue_disposition=safe_queue,
        )

    def _record_execution_recovery(
        self,
        attempt_id: str,
        *,
        event_type: str,
        running_only: bool = False,
        require_dispatch: bool = False,
        prompt_digest: str = "",
        provider_request_key: str = "",
        accepted_turn_id: str = "",
        stop_acknowledgement: dict[str, Any] | None = None,
        queue_disposition: str = "",
    ) -> ExecutionAttempt:
        attempt_id = _validate_id(attempt_id, "attempt_id")
        safe_ack = stop_acknowledgement or {}
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise WorkStoreError(f"unknown execution attempt: {attempt_id}")
            current = _execution_attempt(row)
            if (
                event_type == "execution.attempt.dispatch-prepared"
                and current.status != "running"
            ):
                # Dispatch is authorization for the caller's next external
                # side effect, not terminal evidence replay. Never let an
                # exact delayed sender treat a released attempt as admitted.
                raise WorkStoreError(
                    f"execution attempt is already terminal: {attempt_id}"
                )
            if event_type == "execution.attempt.dispatch-prepared":
                participant = _participant(
                    self._participant_row(conn, current.participant_id)
                )
                if (
                    participant.work_id != current.work_id
                    or participant.provider != current.provider
                    or participant.generation != current.participant_generation
                ):
                    raise StaleParticipantError(
                        "execution participant generation changed before dispatch"
                    )
            if require_dispatch and not current.prompt_digest:
                raise WorkStoreError(
                    f"execution attempt has no durable dispatch: {attempt_id}"
                )
            resolved_native_identity = ""
            if event_type in {
                "execution.attempt.dispatch-prepared",
                "execution.attempt.accepted",
                "execution.attempt.stop-recorded",
            }:
                resolved_native_identity = self._execution_native_identity_conn(
                    conn,
                    current,
                )
            correlated_attempt = (
                replace(current, native_binding_id=resolved_native_identity)
                if resolved_native_identity and not current.native_binding_id
                else current
            )

            candidates: tuple[tuple[str, str, Any, Any, bool], ...] = (
                (
                    "prompt_digest",
                    "prompt digest",
                    current.prompt_digest,
                    prompt_digest,
                    False,
                ),
                (
                    "native_binding_id",
                    "native binding",
                    current.native_binding_id,
                    resolved_native_identity,
                    False,
                ),
                (
                    "provider_request_key",
                    "provider request key",
                    current.provider_request_key,
                    provider_request_key,
                    False,
                ),
                (
                    "accepted_turn_id",
                    "accepted turn id",
                    current.accepted_turn_id,
                    accepted_turn_id,
                    False,
                ),
            )
            assignments: list[str] = []
            values: list[Any] = []
            changed_payload: dict[str, Any] = {"attempt_id": attempt_id}
            for column, label, existing, supplied, encode_json in candidates:
                if supplied in ("", None, {}):
                    continue
                if existing not in ("", None, {}):
                    if existing != supplied:
                        raise WorkStoreError(
                            f"execution attempt {label} conflicts: {attempt_id}"
                        )
                    continue
                assignments.append(f"{column} = ?")
                values.append(_json(supplied) if encode_json else supplied)
                if encode_json:
                    changed_payload["stop_acknowledgement_recorded"] = True
                    changed_payload["stop_acknowledged"] = (
                        supplied.get("acknowledged") is True
                    )
                    changed_payload["cancellation_confirmed"] = (
                        supplied.get("acknowledged") is True
                        and supplied.get("cancellation_confirmed") is True
                    )
                else:
                    changed_payload[column] = supplied

            if safe_ack:
                merged_ack, ack_changed = _merge_stop_acknowledgement(
                    current.stop_acknowledgement,
                    safe_ack,
                    attempt_id=attempt_id,
                )
                _validate_stop_acknowledgement_correlation(
                    correlated_attempt,
                    merged_ack,
                )
                if ack_changed:
                    assignments.append("stop_acknowledgement_json = ?")
                    values.append(_json(merged_ack))
                    changed_payload["stop_acknowledgement_recorded"] = True
                    changed_payload["stop_acknowledged"] = (
                        merged_ack.get("acknowledged") is True
                    )
                    changed_payload["cancellation_confirmed"] = (
                        merged_ack.get("acknowledged") is True
                        and merged_ack.get("cancellation_confirmed") is True
                    )

            if queue_disposition:
                if current.queue_disposition == queue_disposition:
                    pass
                elif not _queue_transition_allowed(
                    current.queue_disposition,
                    queue_disposition,
                ):
                    raise WorkStoreError(
                        "execution attempt queue disposition conflicts: "
                        f"{attempt_id}"
                    )
                else:
                    assignments.append("queue_disposition = ?")
                    values.append(queue_disposition)
                    changed_payload["queue_disposition"] = queue_disposition

            if not assignments:
                return current
            if running_only and current.status != "running":
                raise WorkStoreError(
                    f"execution attempt is already terminal: {attempt_id}"
                )
            now = _now()
            assignments.append("updated_at = ?")
            values.extend((now, attempt_id))
            conn.execute(
                f"UPDATE execution_attempts SET {', '.join(assignments)} "
                "WHERE attempt_id = ?",
                values,
            )
            self._append_execution_attempt_event_conn(
                conn,
                current,
                event_type,
                changed_payload,
                created_at=now,
            )
            row = conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _execution_attempt(row)

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
        """Finish an admitted attempt and release the slot in one transaction.

        Terminal retries are idempotent only when their supplied evidence
        agrees with the durable row. A conflicting retry is surfaced instead
        of being mistaken for proof that a different terminal outcome landed.
        """

        attempt_id = _validate_id(attempt_id, "attempt_id")
        status = _required_text(status, "status")
        if status not in {"completed", "failed", "aborted", "budgetLimited"}:
            raise ValueError(f"invalid execution-attempt terminal status: {status}")
        reason = _recovery_code(
            terminal_reason,
            "terminal reason",
            allow_empty=True,
        )
        safe_receipt = (
            None
            if terminal_receipt is None
            else _recovery_mapping(
                terminal_receipt,
                "terminal receipt",
                fields=_TERMINAL_RECEIPT_FIELDS,
            )
        )
        safe_usage = (
            None
            if usage is None
            else _recovery_mapping(
                usage,
                "usage receipt",
                fields=_USAGE_RECEIPT_FIELDS,
            )
        )
        safe_cost = _optional_cost_micro_usd(cost_micro_usd)
        safe_stop = (
            None
            if stop_acknowledgement is None
            else _recovery_mapping(
                stop_acknowledgement,
                "stop acknowledgement",
                fields=_STOP_ACKNOWLEDGEMENT_FIELDS,
            )
        )
        safe_queue = _queue_disposition(queue_disposition)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise WorkStoreError(f"unknown execution attempt: {attempt_id}")
            current = _execution_attempt(row)
            if current.status != "running":
                conflicts = current.status != status
                conflicts = conflicts or bool(
                    reason and reason != current.terminal_reason
                )
                conflicts = conflicts or bool(
                    safe_receipt is not None
                    and safe_receipt != current.terminal_receipt
                )
                conflicts = conflicts or bool(
                    safe_usage is not None and safe_usage != current.usage
                )
                conflicts = conflicts or bool(
                    safe_cost is not None and safe_cost != current.cost_micro_usd
                )
                conflicts = conflicts or bool(
                    safe_stop is not None
                    and safe_stop != current.stop_acknowledgement
                )
                conflicts = conflicts or bool(
                    safe_queue and safe_queue != current.queue_disposition
                )
                if conflicts:
                    raise WorkStoreError(
                        f"execution attempt terminal retry conflicts: {attempt_id}"
                    )
                return current

            resolved_native_identity = self._execution_native_identity_conn(
                conn,
                current,
            )
            if resolved_native_identity and not current.native_binding_id:
                current = replace(
                    current,
                    native_binding_id=resolved_native_identity,
                )

            final_receipt = safe_receipt if safe_receipt is not None else {}
            final_usage = safe_usage if safe_usage is not None else {}
            final_cost = safe_cost
            final_stop = current.stop_acknowledgement
            if safe_stop is not None:
                final_stop, _ = _merge_stop_acknowledgement(
                    final_stop,
                    safe_stop,
                    attempt_id=attempt_id,
                )
            final_queue = current.queue_disposition
            if safe_queue:
                if not _queue_transition_allowed(final_queue, safe_queue):
                    raise WorkStoreError(
                        f"execution attempt queue disposition conflicts: {attempt_id}"
                    )
                final_queue = safe_queue
            _validate_execution_release(
                current,
                status=status,
                terminal_receipt=final_receipt,
                terminal_receipt_supplied=safe_receipt is not None,
                usage_supplied=safe_usage is not None,
                cost_micro_usd=final_cost,
                stop_acknowledgement=final_stop,
                stop_acknowledgement_supplied=safe_stop is not None,
                queue_disposition=final_queue,
            )
            now = _now()
            if status == "budgetLimited":
                work = _work(self._work_row(conn, current.work_id))
                if work.status != "budgetLimited":
                    # Release and Work-breaker persistence are one commit. A
                    # different Helios process can never observe an open slot
                    # while this exhausted Work still appears executable.
                    conn.execute(
                        """
                        UPDATE works SET status = 'budgetLimited', updated_at = ?
                        WHERE work_id = ?
                        """,
                        (now, current.work_id),
                    )
                    self._append_event_conn(
                        conn,
                        current.work_id,
                        "work.updated",
                        {"status": "budgetLimited"},
                        created_at=now,
                    )
            conn.execute(
                """
                UPDATE execution_attempts
                SET status = ?, terminal_reason = ?, terminal_receipt_json = ?,
                    usage_json = ?, cost_micro_usd = ?,
                    stop_acknowledgement_json = ?, queue_disposition = ?,
                    native_binding_id = ?, finished_at = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'running'
                """,
                (
                    status,
                    reason,
                    _json(final_receipt),
                    _json(final_usage),
                    final_cost,
                    _json(final_stop),
                    final_queue,
                    current.native_binding_id,
                    now,
                    now,
                    attempt_id,
                ),
            )
            self._append_execution_attempt_event_conn(
                conn,
                current,
                "execution.attempt.finished",
                {
                    "attempt_id": attempt_id,
                    "status": status,
                    "terminal_reason": reason,
                    "participant_id": current.participant_id,
                    "participant_generation": current.participant_generation,
                    "terminal_receipt_recorded": bool(final_receipt),
                    "usage_recorded": bool(final_usage),
                    "cost_micro_usd": final_cost,
                    "stop_acknowledgement_recorded": bool(final_stop),
                    "stop_acknowledged": (final_stop.get("acknowledged") is True),
                    "cancellation_confirmed": (
                        final_stop.get("acknowledged") is True
                        and final_stop.get("cancellation_confirmed") is True
                    ),
                    "queue_disposition": final_queue,
                },
                created_at=now,
            )
            row = conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _execution_attempt(row)

    def reclaim_orphaned_execution_attempts(
        self,
        attempt_ids: Collection[str] | None = None,
    ) -> list[ExecutionAttempt]:
        """Release attempts a previous Helios left running when it died.

        Call this once at startup, before anything can admit a new attempt.
        Helios is a single-instance application, so at that moment no live
        process owns a ``running`` row: whoever wrote it is gone, and its
        stdio pipe with it. ``_validate_execution_release`` can therefore never
        be satisfied for these rows — no driver survives to produce a receipt —
        and leaving them running bricks their Work permanently, because
        per-Work admission denies every later turn with "already executing".

        The release records only what is known: aborted, no receipt, no usage.
        It never claims the provider turn ended, and a held queue is discarded
        rather than pretended to have been delivered.

        ``attempt_ids`` narrows the sweep to named attempts, for recovering one
        stuck Work by hand without restarting a Helios that is otherwise fine.
        """

        # ponytail: "startup means orphaned" relies on GApplication uniqueness.
        # If Helios ever runs two instances against one work.db, this needs a
        # recorded owner (host pid + boot id) to check instead.
        now = _now()
        reclaimed: list[ExecutionAttempt] = []
        named = None if attempt_ids is None else list(attempt_ids)
        if named is not None and not named:
            return reclaimed
        with self._transaction() as conn:
            filter_sql = ""
            params: tuple[str, ...] = ()
            if named is not None:
                filter_sql = f"AND attempt_id IN ({','.join('?' * len(named))})"
                params = tuple(named)
            rows = conn.execute(
                f"""
                SELECT * FROM execution_attempts
                WHERE status = 'running' {filter_sql}
                ORDER BY started_at, attempt_id
                """,
                params,
            ).fetchall()
            for row in rows:
                attempt = _execution_attempt(row)
                queue = (
                    "discarded"
                    if attempt.queue_disposition == "held"
                    else attempt.queue_disposition
                )
                conn.execute(
                    """
                    UPDATE execution_attempts
                    SET status = 'aborted', terminal_reason = ?,
                        queue_disposition = ?, finished_at = ?, updated_at = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    (
                        _ORPHANED_TERMINAL_REASON,
                        queue,
                        now,
                        now,
                        attempt.attempt_id,
                    ),
                )
                self._append_execution_attempt_event_conn(
                    conn,
                    attempt,
                    "execution.attempt.finished",
                    {
                        "attempt_id": attempt.attempt_id,
                        "status": "aborted",
                        "terminal_reason": _ORPHANED_TERMINAL_REASON,
                        "participant_id": attempt.participant_id,
                        "participant_generation": attempt.participant_generation,
                        "terminal_receipt_recorded": False,
                        "usage_recorded": False,
                        "cost_micro_usd": None,
                        "stop_acknowledgement_recorded": False,
                        "stop_acknowledged": False,
                        "cancellation_confirmed": False,
                        "queue_disposition": queue,
                    },
                    created_at=now,
                )
                reclaimed.append(
                    replace(
                        attempt,
                        status="aborted",
                        terminal_reason=_ORPHANED_TERMINAL_REASON,
                        queue_disposition=queue,
                        finished_at=now,
                        updated_at=now,
                    )
                )
        return reclaimed

    def get_execution_attempt(self, attempt_id: str) -> ExecutionAttempt | None:
        if not attempt_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM execution_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return _execution_attempt(row) if row is not None else None

    def active_execution_attempt(self) -> ExecutionAttempt | None:
        """Return the only running attempt, failing loudly when plural."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM execution_attempts
                WHERE status = 'running'
                ORDER BY started_at, attempt_id
                LIMIT 2
                """
            ).fetchall()
        if len(rows) > 1:
            raise WorkStoreError(
                "multiple execution attempts are running; "
                "use list_active_execution_attempts()"
            )
        return _execution_attempt(rows[0]) if rows else None

    def list_active_execution_attempts(self) -> list[ExecutionAttempt]:
        """Return every durable running lane owner in stable start order."""

        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM execution_attempts
                WHERE status = 'running'
                ORDER BY started_at, attempt_id
                """
            ).fetchall()
        return [_execution_attempt(row) for row in rows]

    def bind_participant(
        self,
        work_id: str,
        provider: str,
        *,
        native_session_id: str = "",
        native_thread_id: str = "",
        role: str = "partner",
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> Participant:
        provider = _required_text(provider, "provider")
        native_session_id = _text(native_session_id)
        native_thread_id = _text(native_thread_id)
        role = _required_text(role, "role")
        status = _required_text(status, "status")
        now = _now()
        with self._transaction() as conn:
            self._work_row(conn, work_id)
            row = conn.execute(
                "SELECT * FROM participants WHERE work_id = ? AND provider = ?",
                (work_id, provider),
            ).fetchone()
            if row is None:
                participant_id = _new_id("part")
                conn.execute(
                    """
                    INSERT INTO participants (
                        participant_id, work_id, provider, native_session_id,
                        native_thread_id, role, status, generation,
                        last_event_seq, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?)
                    """,
                    (
                        participant_id,
                        work_id,
                        provider,
                        native_session_id,
                        native_thread_id,
                        role,
                        status,
                        _json(_mapping(metadata)),
                        now,
                        now,
                    ),
                )
                event_type = "participant.created"
                event_payload = {
                    "participant_id": participant_id,
                    "provider": provider,
                    "role": role,
                    "native_session_id": native_session_id,
                    "native_thread_id": native_thread_id,
                }
                native_kind, native_id = _native_identity(
                    native_session_id, native_thread_id
                )
                self._activate_binding_conn(
                    conn,
                    participant_id,
                    provider,
                    native_kind,
                    native_id,
                    generation=1,
                    created_at=now,
                )
            else:
                current = _participant(row)
                if native_thread_id:
                    new_session = ""
                    new_thread = native_thread_id
                elif native_session_id:
                    new_session = native_session_id
                    new_thread = ""
                else:
                    new_session = current.native_session_id
                    new_thread = current.native_thread_id
                had_native = bool(current.native_session_id or current.native_thread_id)
                binding_changed = had_native and (
                    new_session != current.native_session_id
                    or new_thread != current.native_thread_id
                )
                generation = current.generation + 1 if binding_changed else current.generation
                cursor = 0 if binding_changed else current.last_event_seq
                new_metadata = dict(current.metadata)
                if metadata is not None:
                    new_metadata.update(_mapping(metadata))
                conn.execute(
                    """
                    UPDATE participants SET
                        native_session_id = ?, native_thread_id = ?, role = ?,
                        status = ?, generation = ?, last_event_seq = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE participant_id = ?
                    """,
                    (
                        new_session,
                        new_thread,
                        role,
                        status,
                        generation,
                        cursor,
                        _json(new_metadata),
                        now,
                        current.participant_id,
                    ),
                )
                participant_id = current.participant_id
                event_type = "binding.attached" if (
                    new_session != current.native_session_id
                    or new_thread != current.native_thread_id
                ) else ""
                event_payload = {
                    "participant_id": participant_id,
                    "provider": provider,
                    "generation": generation,
                    "native_session_id": new_session,
                    "native_thread_id": new_thread,
                    "cursor_reset": binding_changed,
                }
                if binding_changed or (
                    not (current.native_session_id or current.native_thread_id)
                    and (new_session or new_thread)
                ):
                    native_kind, native_id = _native_identity(
                        new_session, new_thread
                    )
                    self._activate_binding_conn(
                        conn,
                        participant_id,
                        provider,
                        native_kind,
                        native_id,
                        generation=generation,
                        created_at=now,
                    )
            if event_type:
                self._append_event_conn(
                    conn,
                    work_id,
                    event_type,
                    event_payload,
                    provider=provider,
                    created_at=now,
                )
            row = conn.execute(
                "SELECT * FROM participants WHERE participant_id = ?",
                (participant_id,),
            ).fetchone()
        return _participant(row)

    def participant_for_provider(self, work_id: str, provider: str) -> Participant | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM participants WHERE work_id = ? AND provider = ?",
                (work_id, provider),
            ).fetchone()
        return _participant(row) if row is not None else None

    def work_id_for_native_session(self, provider: str, native_id: str) -> str:
        if not provider or not native_id:
            return ""
        with self._lock:
            return self._work_id_for_native_conn(self._conn, provider, native_id)

    def list_participants(self, work_id: str) -> list[Participant]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM participants WHERE work_id = ? ORDER BY created_at, provider",
                (work_id,),
            ).fetchall()
        return [_participant(row) for row in rows]

    def update_participant(
        self,
        participant_id: str,
        *,
        native_session_id: str | None = None,
        native_thread_id: str | None = None,
        role: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        last_event_seq: int | None = None,
        expected_generation: int | None = None,
        expected_native_session_id: str | None = None,
    ) -> Participant:
        with self._transaction() as conn:
            row = self._participant_row(conn, participant_id)
            current = _participant(row)
            _check_participant_expectations(
                current,
                expected_generation=expected_generation,
                expected_native_session_id=expected_native_session_id,
            )
            new_session = (
                current.native_session_id
                if native_session_id is None
                else _text(native_session_id)
            )
            new_thread = (
                current.native_thread_id
                if native_thread_id is None
                else _text(native_thread_id)
            )
            binding_changed = (
                new_session != current.native_session_id
                or new_thread != current.native_thread_id
            )
            generation = current.generation + 1 if binding_changed else current.generation
            cursor = 0 if binding_changed else current.last_event_seq
            if last_event_seq is not None:
                if binding_changed and last_event_seq:
                    raise WorkStoreError("cannot advance cursor while rebinding participant")
                if last_event_seq < cursor:
                    raise WorkStoreError("participant cursor cannot move backwards")
                latest = self._latest_seq(conn, current.work_id)
                if last_event_seq > latest:
                    raise WorkStoreError("participant cursor exceeds Work ledger")
                cursor = last_event_seq
            conn.execute(
                """
                UPDATE participants SET
                    native_session_id = ?, native_thread_id = ?, role = ?,
                    status = ?, generation = ?, last_event_seq = ?,
                    metadata_json = ?, updated_at = ?
                WHERE participant_id = ?
                """,
                (
                    new_session,
                    new_thread,
                    role if role is not None else current.role,
                    status if status is not None else current.status,
                    generation,
                    cursor,
                    _json(current.metadata if metadata is None else _mapping(metadata)),
                    _now(),
                    participant_id,
                ),
            )
            if binding_changed:
                native_kind, native_id = _native_identity(new_session, new_thread)
                self._activate_binding_conn(
                    conn,
                    participant_id,
                    current.provider,
                    native_kind,
                    native_id,
                    generation=generation,
                    created_at=_now(),
                )
            row = self._participant_row(conn, participant_id)
        return _participant(row)

    def get_execution_plan(self, work_id: str) -> ExecutionPlan | None:
        """Return the latest model-owned plan snapshot for a Work."""

        if not work_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM execution_plans WHERE work_id = ?",
                (work_id,),
            ).fetchone()
        return _execution_plan(row) if row is not None else None

    def list_execution_plan_revisions(
        self,
        work_id: str,
        *,
        plan_id: str = "",
    ) -> list[ExecutionPlan]:
        """Return immutable plan snapshots in their durable write order."""

        sql = "SELECT * FROM execution_plan_revisions WHERE work_id = ?"
        params: list[Any] = [work_id]
        if plan_id:
            sql += " AND plan_id = ?"
            params.append(plan_id)
        # This table retains SQLite rowid, whose insertion order is serialized
        # by the same BEGIN IMMEDIATE transaction as each revision write.
        sql += " ORDER BY rowid"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_execution_plan(row) for row in rows]

    def record_execution_plan(
        self,
        work_id: str,
        participant_id: str,
        *,
        provider: str,
        expected_participant_generation: int,
        native_turn_id: str,
        explanation: Any = "",
        steps: Any = None,
    ) -> ExecutionPlan:
        """Transactionally reconcile one authoritative native plan snapshot.

        The provider does not currently supply stable task ids, so the previous
        revision is matched under the same ``BEGIN IMMEDIATE`` transaction that
        appends the immutable revision and replaces the latest snapshot.
        """

        provider = _required_text(provider, "provider")
        participant_id = _validate_id(participant_id, "participant_id")
        native_turn_id = _required_text(native_turn_id, "native_turn_id")
        if expected_participant_generation <= 0:
            raise ValueError("expected_participant_generation must be positive")
        explanation = normalize_explanation(explanation)

        with self._transaction() as conn:
            self._work_row(conn, work_id)
            participant = _participant(self._participant_row(conn, participant_id))
            if participant.work_id != work_id:
                raise WorkStoreError("execution plan participant belongs to another Work")
            if participant.provider != provider:
                raise WorkStoreError(
                    "execution plan provider does not match participant"
                )
            _check_participant_expectations(
                participant,
                expected_generation=expected_participant_generation,
            )

            current_row = conn.execute(
                "SELECT * FROM execution_plans WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            current = _execution_plan(current_row) if current_row is not None else None
            plan_id = plan_id_for(
                work_id=work_id,
                provider=provider,
                participant_id=participant_id,
                participant_generation=expected_participant_generation,
                native_turn_id=native_turn_id,
            )
            self._check_execution_plan_turn_conn(
                conn,
                work_id=work_id,
                participant_id=participant_id,
                participant_generation=expected_participant_generation,
                native_turn_id=native_turn_id,
                plan_id=plan_id,
                current=current,
            )

            previous = current if current is not None and current.plan_id == plan_id else None
            if previous is not None and previous.status == "interrupted":
                raise StaleParticipantError(
                    "execution plan turn is already terminally interrupted"
                )
            reconciled = reconcile_steps(
                plan_id,
                steps,
                previous.steps if previous is not None else (),
                dropped_reason=explanation,
            )
            status = plan_status_for(reconciled)
            if (
                previous is not None
                and previous.explanation == explanation
                and previous.steps == reconciled
                and previous.status == status
            ):
                return previous

            revision = (previous.revision + 1) if previous is not None else 1
            now = _now()
            plan = ExecutionPlan(
                work_id=work_id,
                plan_id=plan_id,
                revision=revision,
                provider=provider,
                participant_id=participant_id,
                participant_generation=expected_participant_generation,
                native_turn_id=native_turn_id,
                explanation=explanation,
                steps=reconciled,
                status=status,
                created_at=previous.created_at if previous is not None else now,
                updated_at=now,
            )
            self._insert_execution_plan_revision_conn(conn, plan)
            self._replace_execution_plan_conn(conn, plan)
            return plan

    def interrupt_execution_plan(
        self,
        work_id: str,
        participant_id: str,
        *,
        provider: str,
        expected_participant_generation: int,
        native_turn_id: str,
        reason: Any = "",
    ) -> ExecutionPlan | None:
        """Persist interruption without treating turn termination as success."""

        provider = _required_text(provider, "provider")
        participant_id = _validate_id(participant_id, "participant_id")
        native_turn_id = _required_text(native_turn_id, "native_turn_id")
        with self._transaction() as conn:
            self._work_row(conn, work_id)
            participant = _participant(self._participant_row(conn, participant_id))
            if participant.work_id != work_id or participant.provider != provider:
                raise WorkStoreError("execution plan identity does not match participant")
            _check_participant_expectations(
                participant,
                expected_generation=expected_participant_generation,
            )
            row = conn.execute(
                "SELECT * FROM execution_plans WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            if row is None:
                return None
            current = _execution_plan(row)
            if (
                current.participant_id != participant_id
                or current.participant_generation != expected_participant_generation
                or current.provider != provider
                or current.native_turn_id != native_turn_id
            ):
                raise StaleParticipantError(
                    "turn no longer owns the current execution plan"
                )
            if current.status in {"completed", "interrupted"}:
                return current

            interrupted = interrupt_steps(current.steps, reason=str(reason or ""))
            now = _now()
            plan = replace(
                current,
                revision=current.revision + 1,
                steps=interrupted,
                status="interrupted",
                updated_at=now,
            )
            self._insert_execution_plan_revision_conn(conn, plan)
            self._replace_execution_plan_conn(conn, plan)
            return plan

    def append_event(
        self,
        work_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        provider: str | None = None,
        emitting_participant_id: str | None = None,
        expected_participant_generation: int | None = None,
    ) -> WorkEvent:
        event_type = _required_text(event_type, "event_type")
        if event_type.startswith(_EXECUTION_ATTEMPT_EVENT_PREFIX):
            raise WorkStoreError(
                "execution.attempt.* is a reserved control-plane event namespace"
            )
        with self._transaction() as conn:
            event = self._append_event_conn(
                conn,
                work_id,
                event_type,
                payload,
                provider=provider or "",
                emitting_participant_id=emitting_participant_id or "",
                expected_participant_generation=expected_participant_generation,
            )
        return event

    def append_control_event(
        self,
        work_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        provider: str = "",
    ) -> WorkEvent:
        """Append an allow-listed audit event excluded from model context."""

        event_type = _required_text(event_type, "event_type")
        if event_type not in CONTROL_PLANE_AUDIT_EVENT_TYPES:
            raise WorkStoreError(f"invalid control-plane audit type: {event_type}")
        if event_type.startswith(_EXECUTION_ATTEMPT_EVENT_PREFIX):
            raise WorkStoreError(
                "execution.attempt.* events require an execution attempt receipt"
            )
        with self._transaction() as conn:
            return self._append_event_conn(
                conn,
                work_id,
                event_type,
                payload,
                provider=provider,
                allow_reserved_event_type=True,
            )

    def list_events(
        self,
        work_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[WorkEvent]:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        sql = "SELECT * FROM events WHERE work_id = ? AND seq > ? ORDER BY seq"
        params: list[Any] = [work_id, after_seq]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit must be non-negative")
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_event(row) for row in rows]

    def latest_event_seq(self, work_id: str) -> int:
        """Return the current ledger tail without materializing event payloads."""

        with self._lock:
            return self._latest_seq(self._conn, work_id)

    def snapshot_collaboration_prompt(
        self,
        work_id: str,
        provider: str,
        *,
        limit: int | None = None,
    ) -> CollaborationSnapshot:
        """Read one contract-consistent prompt delta and quarantine its prefix.

        Contract transitions, event appends, participant rebinds, and cursor
        movement are all writers. ``BEGIN IMMEDIATE`` serializes this snapshot
        with those operations, so a contract cannot change between checking its
        scope and choosing the ledger boundary. The persisted boundary also
        protects participants that join or rebind after acceptance.
        """

        provider = _required_text(provider, "provider")
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        with self._transaction() as conn:
            work = _work(self._work_row(conn, work_id))
            row = conn.execute(
                "SELECT * FROM participants WHERE work_id = ? AND provider = ?",
                (work_id, provider),
            ).fetchone()
            if row is None:
                raise ParticipantNotFoundError(
                    f"participant not found for Work/provider: {work_id}/{provider}"
                )
            participant = _participant(row)
            latest = self._latest_seq(conn, work_id)
            accepted = _collaboration_contract_accepted(work)

            # Defensive migration/recovery for a contract written by an older
            # client or raw v2-compatible SQL. New writes establish this in
            # create_work/update_work before their transaction commits.
            if accepted and work.contract_epoch <= 0:
                conn.execute(
                    """
                    UPDATE works
                    SET contract_epoch = 1, contract_start_seq = ?
                    WHERE work_id = ?
                    """,
                    (latest, work_id),
                )
                work = _work(self._work_row(conn, work_id))

            cursor_floor = work.contract_start_seq if accepted else latest
            if cursor_floor > latest:
                raise WorkStoreError("Work contract boundary exceeds ledger tail")
            if participant.last_event_seq < cursor_floor:
                conn.execute(
                    """
                    UPDATE participants
                    SET last_event_seq = ?, updated_at = ?
                    WHERE participant_id = ? AND generation = ?
                    """,
                    (
                        cursor_floor,
                        _now(),
                        participant.participant_id,
                        participant.generation,
                    ),
                )
                participant = _participant(
                    self._participant_row(conn, participant.participant_id)
                )

            events: tuple[WorkEvent, ...] = ()
            if accepted:
                sql = (
                    "SELECT * FROM events WHERE work_id = ? AND seq > ? "
                    "ORDER BY seq"
                )
                params: list[Any] = [work_id, participant.last_event_seq]
                if limit is not None:
                    sql += " LIMIT ?"
                    params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                events = tuple(_event(event_row) for event_row in rows)
        return CollaborationSnapshot(work, participant, events, accepted)

    def acknowledge_collaboration_prompt(
        self,
        participant_id: str,
        *,
        through_seq: int,
        expected_generation: int,
    ) -> Participant:
        """ACK a delivered raw prefix plus newly-arrived trailing audit rows.

        Recovery correlation may be persisted after the prompt snapshot but
        before provider acceptance. Those `execution.attempt.*` rows contain
        no collaborator content and may extend the ACK only while contiguous.
        The first ordinary event stops advancement, so a racing peer update is
        never skipped.
        """

        participant_id = _validate_id(participant_id, "participant_id")
        if through_seq < 0:
            raise ValueError("through_seq must be non-negative")
        with self._transaction() as conn:
            participant = _participant(self._participant_row(conn, participant_id))
            _check_participant_expectations(
                participant,
                expected_generation=expected_generation,
            )
            latest = self._latest_seq(conn, participant.work_id)
            if through_seq > latest:
                raise WorkStoreError("participant cursor exceeds Work ledger")
            boundary = max(participant.last_event_seq, through_seq)
            rows = conn.execute(
                """
                SELECT seq, event_type, control_plane FROM events
                WHERE work_id = ? AND seq > ?
                ORDER BY seq
                """,
                (participant.work_id, boundary),
            ).fetchall()
            for row in rows:
                if not _is_execution_audit_type(
                    str(row["event_type"]),
                    control_plane=bool(row["control_plane"]),
                ):
                    break
                boundary = int(row["seq"])
            if boundary != participant.last_event_seq:
                conn.execute(
                    """
                    UPDATE participants SET last_event_seq = ?, updated_at = ?
                    WHERE participant_id = ? AND generation = ?
                    """,
                    (
                        boundary,
                        _now(),
                        participant.participant_id,
                        participant.generation,
                    ),
                )
                participant = _participant(
                    self._participant_row(conn, participant.participant_id)
                )
        return participant

    def publish_artifact(
        self,
        work_id: str,
        content: bytes | str,
        *,
        media_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        digest_hex = hashlib.sha256(data).hexdigest()
        digest = f"sha256:{digest_hex}"
        artifact_id = digest
        media_type = _required_text(media_type, "media_type")
        meta = _mapping(metadata)
        artifact_dir = self._root / work_id / "artifacts"
        final_path = artifact_dir / digest_hex
        with self._lock:
            if self.get_work(work_id) is None:
                raise WorkNotFoundError(work_id)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            _chmod(artifact_dir.parent, 0o700)
            _chmod(artifact_dir, 0o700)
            if final_path.exists():
                existing_data = final_path.read_bytes()
                if hashlib.sha256(existing_data).hexdigest() != digest_hex:
                    raise WorkStoreError(f"artifact digest mismatch at {final_path}")
            else:
                fd, temp_name = tempfile.mkstemp(prefix=".artifact-", dir=artifact_dir)
                temp_path = Path(temp_name)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temp_path, 0o600)
                    os.replace(temp_path, final_path)
                finally:
                    try:
                        temp_path.unlink()
                    except FileNotFoundError:
                        pass
            _chmod(final_path, 0o600)
            now = _now()
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE work_id = ? AND artifact_id = ?",
                    (work_id, artifact_id),
                ).fetchone()
                if row is not None:
                    existing = _artifact(row)
                    if (
                        existing.media_type != media_type
                        or existing.metadata != meta
                        or existing.size != len(data)
                    ):
                        raise WorkStoreError(
                            "immutable artifact already exists with different metadata"
                        )
                    return existing
                conn.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, work_id, digest, media_type, size, path,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        work_id,
                        digest,
                        media_type,
                        len(data),
                        str(final_path),
                        _json(meta),
                        now,
                    ),
                )
                self._append_event_conn(
                    conn,
                    work_id,
                    "artifact.recorded",
                    {
                        "artifact_id": artifact_id,
                        "digest": digest,
                        "media_type": media_type,
                        "size": len(data),
                    },
                    created_at=now,
                )
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE work_id = ? AND artifact_id = ?",
                    (work_id, artifact_id),
                ).fetchone()
        return _artifact(row)

    def get_artifact(self, work_id: str, artifact_id: str) -> Artifact | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE work_id = ? AND artifact_id = ?",
                (work_id, artifact_id),
            ).fetchone()
        return _artifact(row) if row is not None else None

    def list_artifacts(self, work_id: str) -> list[Artifact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE work_id = ? ORDER BY created_at, artifact_id",
                (work_id,),
            ).fetchall()
        return [_artifact(row) for row in rows]

    def _retry_sqlite_busy(self, action, *, operation: str):
        """Run a short initialization action with bounded busy backoff."""

        for attempt in range(_SQLITE_BUSY_RETRIES):
            try:
                return action()
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_busy(exc) or attempt + 1 >= _SQLITE_BUSY_RETRIES:
                    raise
                delay = min(
                    _SQLITE_BUSY_BASE_DELAY * (2**attempt),
                    _SQLITE_BUSY_MAX_DELAY,
                )
                time.sleep(delay)
        raise WorkStoreError(f"SQLite {operation} retry budget exhausted")

    def _initialize_schema(self) -> None:
        """Inspect and migrate under the same serialized write transaction."""

        with self._lock:
            self._retry_sqlite_busy(
                self._initialize_schema_once,
                operation="schema migration",
            )

    def _initialize_schema_once(self) -> None:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise WorkStoreError(
                    f"Work database schema {version} is newer than supported "
                    f"{SCHEMA_VERSION}"
                )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS works (
                    work_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    definition_of_done TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lead_provider TEXT NOT NULL,
                    contract_epoch INTEGER NOT NULL DEFAULT 0
                        CHECK (contract_epoch >= 0),
                    contract_start_seq INTEGER NOT NULL DEFAULT 0
                        CHECK (contract_start_seq >= 0),
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            work_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(works)").fetchall()
            }
            if "contract_epoch" not in work_columns:
                conn.execute(
                    "ALTER TABLE works ADD COLUMN contract_epoch "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (contract_epoch >= 0)"
                )
            if "contract_start_seq" not in work_columns:
                conn.execute(
                    "ALTER TABLE works ADD COLUMN contract_start_seq "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (contract_start_seq >= 0)"
                )

            for statement in (
                """
                CREATE TABLE IF NOT EXISTS participants (
                    participant_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL REFERENCES works(work_id),
                    provider TEXT NOT NULL,
                    native_session_id TEXT NOT NULL DEFAULT '',
                    native_thread_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    last_event_seq INTEGER NOT NULL CHECK (last_event_seq >= 0),
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (work_id, provider)
                )
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS participants_native_session
                    ON participants(provider, native_session_id)
                    WHERE native_session_id <> ''
                """,
                """
                CREATE UNIQUE INDEX IF NOT EXISTS participants_native_thread
                    ON participants(provider, native_thread_id)
                    WHERE native_thread_id <> ''
                """,
                """
                CREATE TABLE IF NOT EXISTS native_bindings (
                    binding_id TEXT PRIMARY KEY,
                    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
                    provider TEXT NOT NULL,
                    native_kind TEXT NOT NULL,
                    native_id TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    retired_at TEXT NOT NULL DEFAULT '',
                    UNIQUE (provider, native_id)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS native_bindings_participant
                    ON native_bindings(participant_id, status)
                """,
                """
                CREATE TABLE IF NOT EXISTS execution_plans (
                    work_id TEXT PRIMARY KEY REFERENCES works(work_id),
                    plan_id TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    provider TEXT NOT NULL,
                    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
                    participant_generation INTEGER NOT NULL CHECK (
                        participant_generation > 0
                    ),
                    native_turn_id TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'completed', 'blocked', 'interrupted')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS execution_plan_revisions (
                    plan_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    work_id TEXT NOT NULL REFERENCES works(work_id),
                    provider TEXT NOT NULL,
                    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
                    participant_generation INTEGER NOT NULL CHECK (
                        participant_generation > 0
                    ),
                    native_turn_id TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('active', 'completed', 'blocked', 'interrupted')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plan_id, revision)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS execution_plan_revisions_work
                    ON execution_plan_revisions(work_id, updated_at, plan_id, revision)
                """,
                """
                CREATE TABLE IF NOT EXISTS execution_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL REFERENCES works(work_id),
                    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
                    participant_generation INTEGER NOT NULL CHECK (
                        participant_generation > 0
                    ),
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'running', 'completed', 'failed', 'aborted',
                            'budgetLimited'
                        )
                    ),
                    terminal_reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL,
                    prompt_digest TEXT NOT NULL DEFAULT '',
                    native_binding_id TEXT NOT NULL DEFAULT '',
                    provider_request_key TEXT NOT NULL DEFAULT '',
                    accepted_turn_id TEXT NOT NULL DEFAULT '',
                    terminal_receipt_json TEXT NOT NULL DEFAULT '{}',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    cost_micro_usd INTEGER DEFAULT NULL CHECK (
                        cost_micro_usd IS NULL OR cost_micro_usd >= 0
                    ),
                    stop_acknowledgement_json TEXT NOT NULL DEFAULT '{}',
                    queue_disposition TEXT NOT NULL DEFAULT '',
                    -- Canonical filesystem root this attempt executes in and
                    -- whether it may mutate it. Schema v9 retains both only as
                    -- forensics; no workspace lease reads them for admission.
                    workspace_root TEXT NOT NULL DEFAULT '',
                    write_intent INTEGER NOT NULL DEFAULT 0 CHECK (
                        write_intent IN (0, 1)
                    ),
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """,
            ):
                conn.execute(statement)

            conn.execute(
                """CREATE TABLE IF NOT EXISTS openrouter_budget_attempts (
                    attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(attempt_id)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS openrouter_request_budgets (
                    request_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL REFERENCES execution_attempts(attempt_id),
                    reserved_micro_usd INTEGER NOT NULL CHECK (reserved_micro_usd >= 0),
                    charge_micro_usd INTEGER NOT NULL CHECK (charge_micro_usd >= 0),
                    state TEXT NOT NULL CHECK (state IN ('reserved', 'reported', 'rejected')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS openrouter_budget_attempt "
                "ON openrouter_request_budgets(attempt_id)"
            )

            attempt_columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(execution_attempts)"
                ).fetchall()
            }
            attempt_migrations = {
                "prompt_digest": (
                    "ALTER TABLE execution_attempts ADD COLUMN prompt_digest "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                "native_binding_id": (
                    "ALTER TABLE execution_attempts ADD COLUMN native_binding_id "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                "provider_request_key": (
                    "ALTER TABLE execution_attempts ADD COLUMN provider_request_key "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                "accepted_turn_id": (
                    "ALTER TABLE execution_attempts ADD COLUMN accepted_turn_id "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                "terminal_receipt_json": (
                    "ALTER TABLE execution_attempts ADD COLUMN "
                    "terminal_receipt_json TEXT NOT NULL DEFAULT '{}'"
                ),
                "usage_json": (
                    "ALTER TABLE execution_attempts ADD COLUMN usage_json "
                    "TEXT NOT NULL DEFAULT '{}'"
                ),
                "cost_micro_usd": (
                    "ALTER TABLE execution_attempts ADD COLUMN cost_micro_usd "
                    "INTEGER DEFAULT NULL CHECK ("
                    "cost_micro_usd IS NULL OR cost_micro_usd >= 0)"
                ),
                "stop_acknowledgement_json": (
                    "ALTER TABLE execution_attempts ADD COLUMN "
                    "stop_acknowledgement_json TEXT NOT NULL DEFAULT '{}'"
                ),
                "queue_disposition": (
                    "ALTER TABLE execution_attempts ADD COLUMN queue_disposition "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                # v7 single-writer workspace lease. Existing rows get
                # '' / 0, which the lease index excludes, so an upgrade cannot
                # retroactively deny an in-flight attempt.
                "workspace_root": (
                    "ALTER TABLE execution_attempts ADD COLUMN workspace_root "
                    "TEXT NOT NULL DEFAULT ''"
                ),
                "write_intent": (
                    "ALTER TABLE execution_attempts ADD COLUMN write_intent "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (write_intent IN (0, 1))"
                ),
            }
            for column, statement in attempt_migrations.items():
                if column not in attempt_columns:
                    conn.execute(statement)

            # Schema v5 used a constant-expression partial unique index that
            # serialized every interactive provider turn across the app; v6
            # narrowed it to one shared non-Claude lane, which v7 carried
            # forward beside the workspace lease. All of those are invalid
            # under v8, which caps concurrency per provider instead. Drop every
            # Helios-owned admission constraint before validation, then
            # recreate the ones v8 still owns. This is one transaction, so
            # malformed rows restore the prior schema on rollback and a
            # same-name but weakened object cannot be trusted.
            #
            # This block is deliberately unconditional rather than keyed on the
            # stored user_version: it is what self-heals a v6/v7 database whose
            # constant-expression lane is still present. Do not gate it on a
            # version comparison — an upgraded process would then keep the
            # mutex it was released to remove.
            conn.execute("DROP INDEX IF EXISTS execution_attempts_single_running")
            conn.execute(
                "DROP INDEX IF EXISTS "
                "execution_attempts_one_running_per_non_claude_provider"
            )
            conn.execute(
                "DROP INDEX IF EXISTS execution_attempts_one_running_per_work"
            )
            conn.execute(
                "DROP INDEX IF EXISTS execution_attempts_one_running_non_claude"
            )
            conn.execute(
                "DROP INDEX IF EXISTS execution_attempts_one_writer_per_workspace"
            )
            for trigger_name in (
                "execution_attempts_identity_insert",
                "execution_attempts_identity_update",
                "participants_execution_identity_update",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")

            self._validate_execution_attempt_migration_conn(conn)

            for statement in (
                """
                CREATE UNIQUE INDEX
                    execution_attempts_one_running_per_work
                    ON execution_attempts (work_id)
                    WHERE status = 'running'
                """,
                # No index recreates the non-Claude lane: schema v8 replaces
                # that hard limit of 1 with per-provider ceilings, which a
                # partial unique index cannot express. The DROP above therefore
                # self-heals a v6/v7 database on first open. The ceiling is
                # enforced in `_execution_admission_conflict_conn` under the
                # same BEGIN IMMEDIATE transaction as the INSERT.
                # Schema v9 does NOT recreate the workspace write lease.
                # It was added 2026-08-05  against a hazard its
                # own commit message described as one that *could* happen, and
                # it is the fourth control from the 2026-08-03 over-correction;
                # the other three were all walked back for over-constraining the
                # app. The real mitigation already exists and is better:
                # `backend/checkpoints.py` snapshots the worktree before every
                # turn, so a clobber is recoverable after the fact rather than
                # forbidden in advance. The unconditional DROP above therefore
                # self-heals a v7/v8 database on first open. `workspace_root`
                # and `write_intent` are KEPT and still written — they are
                # useful forensics, and dropping columns would mean a table
                # rebuild for no benefit.
                """
                CREATE INDEX IF NOT EXISTS execution_attempts_work_started
                    ON execution_attempts(work_id, started_at, attempt_id)
                """,
                """
                CREATE TRIGGER execution_attempts_identity_insert
                    BEFORE INSERT ON execution_attempts
                    WHEN NOT EXISTS (
                        SELECT 1 FROM participants
                        WHERE participant_id = NEW.participant_id
                          AND work_id = NEW.work_id
                          AND provider = NEW.provider
                          AND generation = NEW.participant_generation
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'execution attempt identity does not match participant'
                        );
                    END
                """,
                """
                CREATE TRIGGER execution_attempts_identity_update
                    BEFORE UPDATE OF work_id, participant_id,
                        participant_generation, provider
                    ON execution_attempts
                    WHEN NEW.work_id <> OLD.work_id
                      OR NEW.participant_id <> OLD.participant_id
                      OR NEW.participant_generation <> OLD.participant_generation
                      OR NEW.provider <> OLD.provider
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'execution attempt identity is immutable'
                        );
                    END
                """,
                """
                CREATE TRIGGER participants_execution_identity_update
                    BEFORE UPDATE OF participant_id, work_id, provider
                    ON participants
                    WHEN (
                        NEW.participant_id <> OLD.participant_id
                        OR NEW.work_id <> OLD.work_id
                        OR NEW.provider <> OLD.provider
                    ) AND EXISTS (
                        SELECT 1 FROM execution_attempts
                        WHERE participant_id = OLD.participant_id
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'participant execution identity is immutable'
                        );
                    END
                """,
                """
                INSERT OR IGNORE INTO native_bindings (
                    binding_id, participant_id, provider, native_kind,
                    native_id, generation, status, created_at, retired_at
                )
                SELECT
                    'bind_' || lower(hex(randomblob(16))),
                    participant_id,
                    provider,
                    CASE WHEN native_thread_id <> '' THEN 'thread' ELSE 'session' END,
                    CASE WHEN native_thread_id <> ''
                         THEN native_thread_id ELSE native_session_id END,
                    generation,
                    'active',
                    created_at,
                    ''
                FROM participants
                WHERE native_thread_id <> '' OR native_session_id <> ''
                """,
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL REFERENCES works(work_id),
                    seq INTEGER NOT NULL CHECK (seq > 0),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    emitting_participant_id TEXT NOT NULL,
                    control_plane INTEGER NOT NULL DEFAULT 0 CHECK (
                        control_plane IN (0, 1)
                    ),
                    created_at TEXT NOT NULL,
                    UNIQUE (work_id, seq)
                )
                """,
                "CREATE INDEX IF NOT EXISTS events_work_seq ON events(work_id, seq)",
                """
                UPDATE works
                SET contract_epoch = 1,
                    contract_start_seq = (
                        SELECT COALESCE(MAX(events.seq), 0)
                        FROM events
                        WHERE events.work_id = works.work_id
                    )
                WHERE contract_epoch = 0
                  AND mode = 'tandem'
                  AND status = 'active'
                  AND trim(cwd) <> ''
                  AND trim(objective) <> ''
                  AND trim(definition_of_done) <> ''
                """,
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT NOT NULL,
                    work_id TEXT NOT NULL REFERENCES works(work_id),
                    digest TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    path TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (work_id, artifact_id)
                )
                """,
                """
                CREATE TRIGGER IF NOT EXISTS events_immutable_update
                    BEFORE UPDATE ON events BEGIN
                        SELECT RAISE(ABORT, 'events are immutable');
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS events_immutable_delete
                    BEFORE DELETE ON events BEGIN
                        SELECT RAISE(ABORT, 'events are immutable');
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS artifacts_immutable_update
                    BEFORE UPDATE ON artifacts BEGIN
                        SELECT RAISE(ABORT, 'artifacts are immutable');
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS artifacts_immutable_delete
                    BEFORE DELETE ON artifacts BEGIN
                        SELECT RAISE(ABORT, 'artifacts are immutable');
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS execution_plans_identity_insert
                    BEFORE INSERT ON execution_plans
                    WHEN NOT EXISTS (
                        SELECT 1 FROM participants
                        WHERE participant_id = NEW.participant_id
                          AND work_id = NEW.work_id
                          AND provider = NEW.provider
                          AND generation = NEW.participant_generation
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'execution plan identity does not match participant'
                        );
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS execution_plans_identity_update
                    BEFORE UPDATE OF work_id, participant_id,
                        participant_generation, provider
                    ON execution_plans
                    WHEN NOT EXISTS (
                        SELECT 1 FROM participants
                        WHERE participant_id = NEW.participant_id
                          AND work_id = NEW.work_id
                          AND provider = NEW.provider
                          AND generation = NEW.participant_generation
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'execution plan identity does not match participant'
                        );
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS execution_plan_revisions_identity_insert
                    BEFORE INSERT ON execution_plan_revisions
                    WHEN NOT EXISTS (
                        SELECT 1 FROM participants
                        WHERE participant_id = NEW.participant_id
                          AND work_id = NEW.work_id
                          AND provider = NEW.provider
                          AND generation = NEW.participant_generation
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'execution plan revision identity does not match participant'
                        );
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS execution_plan_revisions_immutable_update
                    BEFORE UPDATE ON execution_plan_revisions BEGIN
                        SELECT RAISE(ABORT, 'execution plan revisions are immutable');
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS execution_plan_revisions_immutable_delete
                    BEFORE DELETE ON execution_plan_revisions BEGIN
                        SELECT RAISE(ABORT, 'execution plan revisions are immutable');
                    END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS participants_plan_identity_update
                    BEFORE UPDATE OF participant_id, work_id, provider
                    ON participants
                    WHEN (
                        NEW.participant_id <> OLD.participant_id
                        OR NEW.work_id <> OLD.work_id
                        OR NEW.provider <> OLD.provider
                    ) AND (
                        EXISTS (
                            SELECT 1 FROM execution_plans
                            WHERE participant_id = OLD.participant_id
                        )
                        OR EXISTS (
                            SELECT 1 FROM execution_plan_revisions
                            WHERE participant_id = OLD.participant_id
                        )
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'participant execution-plan identity is immutable'
                        );
                    END
                """,
            ):
                conn.execute(statement)
            event_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(events)").fetchall()
            }
            if "control_plane" not in event_columns:
                # Pre-v5 events have no trustworthy private provenance. They
                # remain ordinary collaborator-visible rows even if their type
                # collides with a newly-reserved audit name.
                conn.execute(
                    "ALTER TABLE events ADD COLUMN control_plane INTEGER "
                    "NOT NULL DEFAULT 0 CHECK (control_plane IN (0, 1))"
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _validate_execution_attempt_migration_conn(
        self,
        conn: sqlite3.Connection,
    ) -> None:
        """Refuse startup/migration that would bless unsafe attempt rows.

        Schema v4 accepted arbitrary attempt metadata and free-form terminal
        reasons. Updating those rows in place cannot guarantee removal from
        SQLite free pages/WAL, so v5 fails the migration instead of claiming
        that unsafe content was sanitized. An operator can inspect and rebuild
        a development database explicitly; production upgrades from v3 have no
        execution-attempt rows. Schema v6 additionally proves every attempt's
        Work/provider identity against its participant. The admitted generation
        is an immutable attempt snapshot and may legitimately differ after a
        later native rebind, whether the attempt is running or terminal.
        """

        rows = conn.execute(
            """
            SELECT
                attempt.attempt_id,
                attempt.work_id,
                attempt.provider,
                attempt.metadata_json,
                attempt.terminal_reason,
                participant.participant_id AS matched_participant_id,
                participant.work_id AS participant_work_id,
                participant.provider AS participant_provider
            FROM execution_attempts AS attempt
            LEFT JOIN participants AS participant
              ON participant.participant_id = attempt.participant_id
            """
        ).fetchall()
        for row in rows:
            attempt_id = str(row["attempt_id"])
            if row["matched_participant_id"] is None:
                raise WorkStoreError(
                    "unsafe execution attempt identity; migration refused "
                    f"for attempt {attempt_id}: participant is missing"
                )
            if (
                str(row["work_id"]) != str(row["participant_work_id"])
                or str(row["provider"]) != str(row["participant_provider"])
            ):
                raise WorkStoreError(
                    "unsafe execution attempt identity; migration refused "
                    f"for attempt {attempt_id}: Work/provider mismatch"
                )
            try:
                metadata = _strict_json_mapping(str(row["metadata_json"]))
                normalized = _recovery_mapping(
                    metadata,
                    "execution metadata",
                    fields=_EXECUTION_METADATA_FIELDS,
                )
                if normalized != metadata:
                    conn.execute(
                        "UPDATE execution_attempts SET metadata_json = ? "
                        "WHERE attempt_id = ?",
                        (_json(normalized), attempt_id),
                    )
                reason = str(row["terminal_reason"])
                if (
                    _recovery_code(
                        reason,
                        "terminal reason",
                        allow_empty=True,
                    )
                    != reason
                ):
                    raise ValueError("terminal reason would require normalization")
            except (TypeError, ValueError) as exc:
                raise WorkStoreError(
                    "unsafe schema-v4 execution recovery data; migration "
                    f"refused for attempt {attempt_id}"
                ) from exc

    def _check_execution_plan_turn_conn(
        self,
        conn: sqlite3.Connection,
        *,
        work_id: str,
        participant_id: str,
        participant_generation: int,
        native_turn_id: str,
        plan_id: str,
        current: ExecutionPlan | None,
    ) -> None:
        """Fence a late callback once a newer native turn is durable."""

        if current is not None and current.plan_id != plan_id:
            historical = conn.execute(
                "SELECT 1 FROM execution_plan_revisions WHERE plan_id = ? LIMIT 1",
                (plan_id,),
            ).fetchone()
            if historical is not None:
                raise StaleParticipantError(
                    "execution plan turn was already superseded"
                )

        latest_attempt = conn.execute(
            """
            SELECT accepted_turn_id
            FROM execution_attempts
            WHERE work_id = ?
              AND participant_id = ?
              AND participant_generation = ?
              AND accepted_turn_id <> ''
            ORDER BY started_at DESC, attempt_id DESC
            LIMIT 1
            """,
            (work_id, participant_id, participant_generation),
        ).fetchone()
        if (
            latest_attempt is not None
            and str(latest_attempt["accepted_turn_id"]) != native_turn_id
        ):
            raise StaleParticipantError(
                "execution plan turn is not the participant's latest accepted turn"
            )

    def _insert_execution_plan_revision_conn(
        self,
        conn: sqlite3.Connection,
        plan: ExecutionPlan,
    ) -> None:
        conn.execute(
            """
            INSERT INTO execution_plan_revisions (
                plan_id, revision, work_id, provider, participant_id,
                participant_generation, native_turn_id, explanation,
                steps_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _execution_plan_values(plan),
        )

    def _replace_execution_plan_conn(
        self,
        conn: sqlite3.Connection,
        plan: ExecutionPlan,
    ) -> None:
        conn.execute(
            """
            INSERT INTO execution_plans (
                work_id, plan_id, revision, provider, participant_id,
                participant_generation, native_turn_id, explanation,
                steps_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_id) DO UPDATE SET
                plan_id = excluded.plan_id,
                revision = excluded.revision,
                provider = excluded.provider,
                participant_id = excluded.participant_id,
                participant_generation = excluded.participant_generation,
                native_turn_id = excluded.native_turn_id,
                explanation = excluded.explanation,
                steps_json = excluded.steps_json,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (
                plan.work_id,
                plan.plan_id,
                plan.revision,
                plan.provider,
                plan.participant_id,
                plan.participant_generation,
                plan.native_turn_id,
                plan.explanation,
                _json(steps_to_json(plan.steps)),
                plan.status,
                plan.created_at,
                plan.updated_at,
            ),
        )

    class _Transaction:
        def __init__(self, store: WorkStore) -> None:
            self.store = store

        def __enter__(self) -> sqlite3.Connection:
            self.store._lock.acquire()
            try:
                self.store._conn.execute("BEGIN IMMEDIATE")
            except Exception:
                self.store._lock.release()
                raise
            return self.store._conn

        def __exit__(self, exc_type, exc, _tb) -> None:
            try:
                self.store._conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
            finally:
                self.store._lock.release()

    def _transaction(self) -> WorkStore._Transaction:
        return self._Transaction(self)

    def _work_row(self, conn: sqlite3.Connection, work_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
        if row is None:
            raise WorkNotFoundError(work_id)
        return row

    def _participant_row(
        self, conn: sqlite3.Connection, participant_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM participants WHERE participant_id = ?", (participant_id,)
        ).fetchone()
        if row is None:
            raise ParticipantNotFoundError(participant_id)
        return row

    def _latest_seq(self, conn: sqlite3.Connection, work_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        return int(row["seq"])

    def _work_id_for_native_conn(
        self,
        conn: sqlite3.Connection,
        provider: str,
        native_id: str,
    ) -> str:
        row = conn.execute(
            """
            SELECT participants.work_id
            FROM native_bindings
            JOIN participants USING (participant_id)
            WHERE native_bindings.provider = ?
              AND native_bindings.native_id = ?
            UNION
            SELECT work_id FROM participants
            WHERE provider = ? AND (
                native_session_id = ? OR native_thread_id = ?
            )
            LIMIT 1
            """,
            (provider, native_id, provider, native_id, native_id),
        ).fetchone()
        return str(row["work_id"]) if row is not None else ""

    def _append_execution_attempt_event_conn(
        self,
        conn: sqlite3.Connection,
        attempt: ExecutionAttempt,
        event_type: str,
        payload: dict[str, Any],
        *,
        created_at: str,
    ) -> WorkEvent:
        """Append audit evidence and advance its actor only when gap-free."""

        if event_type not in EXECUTION_ATTEMPT_AUDIT_EVENT_TYPES:
            raise WorkStoreError(f"invalid execution-attempt audit type: {event_type}")

        participant = _participant(
            self._participant_row(conn, attempt.participant_id)
        )
        if (
            participant.work_id == attempt.work_id
            and participant.provider == attempt.provider
            and participant.generation == attempt.participant_generation
        ):
            return self._append_event_conn(
                conn,
                attempt.work_id,
                event_type,
                payload,
                provider=attempt.provider,
                emitting_participant_id=attempt.participant_id,
                expected_participant_generation=attempt.participant_generation,
                created_at=created_at,
                allow_reserved_event_type=True,
            )
        # A rebound participant must not be fenced by the old generation's
        # receipt. Its next prompt will skip this audit row through the same
        # contiguous-boundary acknowledgement used for any historical audit.
        return self._append_event_conn(
            conn,
            attempt.work_id,
            event_type,
            payload,
            provider=attempt.provider,
            created_at=created_at,
            allow_reserved_event_type=True,
        )

    def _execution_native_identity_conn(
        self,
        conn: sqlite3.Connection,
        attempt: ExecutionAttempt,
    ) -> str:
        """Resolve only a binding already recorded for the admitted generation."""

        binding = conn.execute(
            """
            SELECT native_id FROM native_bindings
            WHERE participant_id = ? AND provider = ? AND generation = ?
            ORDER BY created_at DESC, binding_id DESC
            LIMIT 1
            """,
            (
                attempt.participant_id,
                attempt.provider,
                attempt.participant_generation,
            ),
        ).fetchone()
        if binding is None:
            return ""
        return _recovery_identifier(
            str(binding["native_id"]),
            "participant native identity",
        )

    def _append_event_conn(
        self,
        conn: sqlite3.Connection,
        work_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        provider: str = "",
        emitting_participant_id: str = "",
        expected_participant_generation: int | None = None,
        created_at: str | None = None,
        allow_reserved_event_type: bool = False,
    ) -> WorkEvent:
        self._work_row(conn, work_id)
        event_type = _required_text(event_type, "event_type")
        if (
            allow_reserved_event_type
            and event_type not in CONTROL_PLANE_AUDIT_EVENT_TYPES
        ):
            raise WorkStoreError(f"invalid control-plane audit type: {event_type}")
        if event_type.startswith(_EXECUTION_ATTEMPT_EVENT_PREFIX):
            if not allow_reserved_event_type:
                raise WorkStoreError(
                    "execution.attempt.* is a reserved control-plane event namespace"
                )
            if event_type not in EXECUTION_ATTEMPT_AUDIT_EVENT_TYPES:
                raise WorkStoreError(
                    f"invalid execution-attempt audit type: {event_type}"
                )
        payload = _mapping(payload)
        participant: Participant | None = None
        if emitting_participant_id:
            participant = _participant(
                self._participant_row(conn, emitting_participant_id)
            )
            if participant.work_id != work_id:
                raise WorkStoreError("emitting participant belongs to another Work")
            _check_participant_expectations(
                participant,
                expected_generation=expected_participant_generation,
            )
            if provider and provider != participant.provider:
                raise WorkStoreError("event provider does not match emitting participant")
            provider = participant.provider
        latest_before = self._latest_seq(conn, work_id)
        seq = latest_before + 1
        event_id = _new_id("evt")
        created_at = created_at or _now()
        conn.execute(
            """
            INSERT INTO events (
                event_id, work_id, seq, event_type, payload_json, provider,
                emitting_participant_id, control_plane, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                work_id,
                seq,
                event_type,
                _json(payload),
                provider,
                emitting_participant_id,
                int(allow_reserved_event_type),
                created_at,
            ),
        )
        if (
            participant is not None
            and participant.last_event_seq == latest_before
        ):
            # The actor can acknowledge its own direct user/model event only
            # when no foreign event landed after its delivered cursor. If a
            # sibling wrote concurrently, preserve the gap so the next prompt
            # receives that contiguous delta (plus an at-least-once echo of
            # this own event) instead of silently skipping peer work.
            conn.execute(
                """
                UPDATE participants SET last_event_seq = ?, updated_at = ?
                WHERE participant_id = ? AND generation = ?
                """,
                (seq, created_at, participant.participant_id, participant.generation),
            )
        return WorkEvent(
            event_id=event_id,
            work_id=work_id,
            seq=seq,
            event_type=event_type,
            payload=payload,
            provider=provider,
            emitting_participant_id=emitting_participant_id,
            control_plane=allow_reserved_event_type,
            created_at=created_at,
        )

    def _activate_binding_conn(
        self,
        conn: sqlite3.Connection,
        participant_id: str,
        provider: str,
        native_kind: str,
        native_id: str,
        *,
        generation: int,
        created_at: str,
    ) -> None:
        conn.execute(
            """
            UPDATE native_bindings
            SET status = 'retired', retired_at = ?
            WHERE participant_id = ? AND status = 'active'
              AND NOT (provider = ? AND native_id = ?)
            """,
            (created_at, participant_id, provider, native_id),
        )
        if not native_id:
            return
        existing = conn.execute(
            """
            SELECT participant_id FROM native_bindings
            WHERE provider = ? AND native_id = ?
            """,
            (provider, native_id),
        ).fetchone()
        if existing is not None and existing["participant_id"] != participant_id:
            raise WorkStoreError(
                f"native binding already belongs to another participant: {native_id}"
            )
        if existing is None:
            conn.execute(
                """
                INSERT INTO native_bindings (
                    binding_id, participant_id, provider, native_kind,
                    native_id, generation, status, created_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, '')
                """,
                (
                    _new_id("bind"),
                    participant_id,
                    provider,
                    native_kind,
                    native_id,
                    generation,
                    created_at,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE native_bindings
                SET native_kind = ?, generation = ?, status = 'active', retired_at = ''
                WHERE provider = ? AND native_id = ?
                """,
                (native_kind, generation, provider, native_id),
            )


def _check_participant_expectations(
    participant: Participant,
    *,
    expected_generation: int | None = None,
    expected_native_session_id: str | None = None,
) -> None:
    if expected_generation is not None and participant.generation != expected_generation:
        raise StaleParticipantError(
            f"participant generation changed: {participant.generation}"
        )
    if (
        expected_native_session_id is not None
        and participant.native_session_id != expected_native_session_id
    ):
        raise StaleParticipantError("participant native session changed")


def _work(row: sqlite3.Row) -> Work:
    return Work(
        work_id=str(row["work_id"]),
        objective=str(row["objective"]),
        definition_of_done=str(row["definition_of_done"]),
        cwd=str(row["cwd"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        lead_provider=str(row["lead_provider"]),
        contract_epoch=int(row["contract_epoch"]),
        contract_start_seq=int(row["contract_start_seq"]),
        metadata=_load_json(row["metadata_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _participant(row: sqlite3.Row) -> Participant:
    return Participant(
        participant_id=str(row["participant_id"]),
        work_id=str(row["work_id"]),
        provider=str(row["provider"]),
        native_session_id=str(row["native_session_id"]),
        native_thread_id=str(row["native_thread_id"]),
        role=str(row["role"]),
        status=str(row["status"]),
        generation=int(row["generation"]),
        last_event_seq=int(row["last_event_seq"]),
        metadata=_load_json(row["metadata_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _execution_plan(row: sqlite3.Row) -> ExecutionPlan:
    return ExecutionPlan(
        work_id=str(row["work_id"]),
        plan_id=str(row["plan_id"]),
        revision=int(row["revision"]),
        provider=str(row["provider"]),
        participant_id=str(row["participant_id"]),
        participant_generation=int(row["participant_generation"]),
        native_turn_id=str(row["native_turn_id"]),
        explanation=str(row["explanation"]),
        steps=steps_from_json(_load_json_list(row["steps_json"])),
        status=str(row["status"]),  # type: ignore[arg-type]
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _execution_plan_values(plan: ExecutionPlan) -> tuple[Any, ...]:
    """Column values for ``execution_plan_revisions`` (plan id first)."""

    return (
        plan.plan_id,
        plan.revision,
        plan.work_id,
        plan.provider,
        plan.participant_id,
        plan.participant_generation,
        plan.native_turn_id,
        plan.explanation,
        _json(steps_to_json(plan.steps)),
        plan.status,
        plan.created_at,
        plan.updated_at,
    )


def _execution_attempt(row: sqlite3.Row) -> ExecutionAttempt:
    return ExecutionAttempt(
        attempt_id=str(row["attempt_id"]),
        work_id=str(row["work_id"]),
        participant_id=str(row["participant_id"]),
        participant_generation=int(row["participant_generation"]),
        provider=str(row["provider"]),
        status=str(row["status"]),
        terminal_reason=str(row["terminal_reason"]),
        metadata=_load_json(row["metadata_json"]),
        prompt_digest=str(row["prompt_digest"]),
        native_binding_id=str(row["native_binding_id"]),
        provider_request_key=str(row["provider_request_key"]),
        accepted_turn_id=str(row["accepted_turn_id"]),
        terminal_receipt=_load_json(row["terminal_receipt_json"]),
        usage=_load_json(row["usage_json"]),
        cost_micro_usd=(
            int(row["cost_micro_usd"])
            if row["cost_micro_usd"] is not None
            else None
        ),
        stop_acknowledgement=_load_json(row["stop_acknowledgement_json"]),
        queue_disposition=str(row["queue_disposition"]),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]),
        updated_at=str(row["updated_at"]),
        workspace_root=str(_row_value(row, "workspace_root", "")),
        write_intent=bool(_row_value(row, "write_intent", 0)),
    )


def _row_value(row: sqlite3.Row, key: str, default: Any) -> Any:
    """Read a column that may be absent on a pre-migration row shape."""

    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _execution_admission_conflict_conn(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    provider: str,
    workspace_root: str = "",
    write_intent: bool = False,
) -> tuple[ExecutionAttempt, str] | None:
    """Return the exact durable lane conflict, most specific scope first.

    Three lanes, checked Work → workspace → provider:

    * **work** — one running attempt per Work (v6).
    The workspace lane was REMOVED in schema v9. Two chats may share a folder:
    the guard it provided is covered after the fact by pre-turn checkpoints, and
    forbidding the work in advance blocked the ordinary case of two sessions in
    one repo. ``workspace_root``/``write_intent`` are still recorded.
    * **provider** — one running non-Claude attempt app-wide (v6).

    A read-only attempt (``write_intent=False``, i.e. Plan) never takes or is
    denied by the workspace lane; readers do not conflict with each other or
    with a writer.
    """

    row = conn.execute(
        """
        SELECT * FROM execution_attempts
        WHERE status = 'running' AND work_id = ?
        ORDER BY started_at, attempt_id
        LIMIT 1
        """,
        (work_id,),
    ).fetchone()
    if row is not None:
        return _execution_attempt(row), "work"

    lane = _provider_lane(provider)
    if lane is None:
        return None
    lane_key, limit = lane
    rows = _running_lane_rows(conn, lane_key)
    if len(rows) < limit:
        return None
    # The oldest owner is the stable, reproducible one to name; a caller
    # waiting on the lane will most likely see that one free up first.
    return _execution_attempt(rows[0]), "provider"


def _provider_lane(provider: str) -> tuple[str, int] | None:
    """Return this provider's ``(lane_key, limit)``, or ``None`` when uncapped.

    Claude is uncapped — per-Work admission is its only gate. Each canonical
    non-Claude provider owns a bounded lane. Every unrecognized id shares one
    fail-closed slot rather than getting a lane of its own.
    """

    if provider == _CLAUDE_PROVIDER:
        return None
    limit = _PROVIDER_CONCURRENCY_LIMITS.get(provider)
    if limit is None:
        return _UNKNOWN_PROVIDER_LANE, _UNKNOWN_PROVIDER_LIMIT
    return provider, limit


def _running_lane_rows(conn: sqlite3.Connection, lane_key: str) -> list[sqlite3.Row]:
    """Running attempts occupying one lane, oldest first.

    Counting inside the caller's ``BEGIN IMMEDIATE`` transaction is what makes
    a bounded cap safe across processes: SQLite admits one writer at a time, so
    the count and the INSERT that follows it cannot interleave with another
    Helios. A partial unique index cannot express "at most N", so unlike the
    per-Work constraint this ceiling has no declarative backstop — do not move
    this call outside the write transaction.
    """

    if lane_key == _UNKNOWN_PROVIDER_LANE:
        placeholders = ",".join("?" * len(_KNOWN_PROVIDERS))
        return conn.execute(
            f"""
            SELECT * FROM execution_attempts
            WHERE status = 'running' AND provider NOT IN ({placeholders})
            ORDER BY started_at, attempt_id
            """,
            _KNOWN_PROVIDERS,
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM execution_attempts
        WHERE status = 'running' AND provider = ?
        ORDER BY started_at, attempt_id
        """,
        (lane_key,),
    ).fetchall()


def _execution_denial_message(owner: ExecutionAttempt, *, scope: str) -> str:
    """Describe the lane owner without exposing its prompt or metadata."""

    owner_details = (
        f"provider={owner.provider}, Work={owner.work_id}, "
        f"attempt={owner.attempt_id}"
    )
    if scope == "work":
        return (
            f"This Work is already executing ({owner_details}). "
            "Wait for it to finish or stop it first."
        )
    if scope != "provider":
        raise ValueError(f"unknown execution conflict scope: {scope}")
    lane = _provider_lane(owner.provider)
    limit = lane[1] if lane is not None else _UNKNOWN_PROVIDER_LIMIT
    if lane is not None and lane[0] == _UNKNOWN_PROVIDER_LANE:
        return (
            f"An unrecognized provider is already executing ({owner_details}). "
            "Helios allows one running attempt at a time for providers it "
            "does not recognize. "
            "Wait for it to finish or stop it first."
        )
    return (
        f"{owner.provider} already has {limit} running "
        f"{'Work' if limit == 1 else 'Works'} ({owner_details} is the oldest). "
        f"Helios allows {limit} concurrent {owner.provider} "
        f"{'attempt' if limit == 1 else 'attempts'}. "
        "Wait for one to finish or stop it first."
    )


def _event(row: sqlite3.Row) -> WorkEvent:
    return WorkEvent(
        event_id=str(row["event_id"]),
        work_id=str(row["work_id"]),
        seq=int(row["seq"]),
        event_type=str(row["event_type"]),
        payload=_load_json(row["payload_json"]),
        provider=str(row["provider"]),
        emitting_participant_id=str(row["emitting_participant_id"]),
        control_plane=bool(row["control_plane"]),
        created_at=str(row["created_at"]),
    )


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        artifact_id=str(row["artifact_id"]),
        work_id=str(row["work_id"]),
        digest=str(row["digest"]),
        media_type=str(row["media_type"]),
        size=int(row["size"]),
        path=Path(str(row["path"])),
        metadata=_load_json(row["metadata_json"]),
        created_at=str(row["created_at"]),
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _native_identity(session_id: str, thread_id: str) -> tuple[str, str]:
    if thread_id:
        return "thread", thread_id
    if session_id:
        return "session", session_id
    return "", ""


def _contract_fields_accepted(
    *,
    objective: str,
    definition_of_done: str,
    cwd: str,
    mode: str,
    status: str,
) -> bool:
    return bool(
        mode == "tandem"
        and status == "active"
        and cwd.strip()
        and objective.strip()
        and definition_of_done.strip()
    )


def _collaboration_contract_accepted(work: Work) -> bool:
    return _contract_fields_accepted(
        objective=work.objective,
        definition_of_done=work.definition_of_done,
        cwd=work.cwd,
        mode=work.mode,
        status=work.status,
    )


def _validate_id(value: str, label: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _text(value: Any) -> str:
    return str(value or "").strip()


def _required_text(value: Any, label: str) -> str:
    text = _text(value)
    if not text:
        raise ValueError(f"{label} must not be empty")
    return text


def _mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("metadata/payload must be a mapping")
    # Round-trip now to reject non-JSON values before opening a transaction.
    return json.loads(_json(value))


def _recovery_mapping(
    value: dict[str, Any] | None,
    label: str,
    *,
    fields: dict[str, str],
) -> dict[str, Any]:
    """Project one recovery receipt through an explicit, flat typed schema."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a mapping")
    cleaned: dict[str, Any] = {}
    seen_keys: set[str] = set()
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise TypeError(f"{label} keys must be strings")
        key = raw_key.strip().lower().replace("-", "_")
        if not key:
            raise ValueError(f"{label} contains an empty key")
        if key in seen_keys:
            raise ValueError(f"{label} contains duplicate normalized fields")
        seen_keys.add(key)
        if (
            key in _RAW_CONTENT_KEYS
            or key.endswith("_prompt")
            or key.endswith("_prompts")
            or key.endswith("_transcript")
        ):
            raise ValueError(
                f"{label} must not contain raw prompt/content field: {raw_key}"
            )
        if (
            key in _SECRET_METADATA_KEYS
            or key in _SENSITIVE_HEADER_CONTAINERS
            or any(key.endswith(f"_{suffix}") for suffix in _SECRET_METADATA_KEYS)
        ):
            raise ValueError(f"{label} must not contain sensitive field: {raw_key}")
        field_type = fields.get(key)
        if field_type is None:
            raise ValueError(f"{label} contains unsupported field: {raw_key}")
        if raw_value in (None, "") and field_type in {
            "code",
            "identifier",
            "label",
        }:
            continue
        cleaned[key] = _recovery_field(raw_value, field_type, f"{label}.{key}")
    encoded = _json(cleaned)
    if len(encoded.encode("utf-8")) > _RECOVERY_JSON_LIMIT:
        raise ValueError(f"{label} exceeds the metadata size limit")
    return cleaned


def _recovery_field(value: Any, field_type: str, label: str) -> Any:
    if field_type == "bool":
        if not isinstance(value, bool):
            raise TypeError(f"{label} must be a boolean")
        return value
    if field_type == "nonnegative_int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{label} must be an integer")
        if value < 0:
            raise ValueError(f"{label} must be non-negative")
        if value > 2**63 - 1:
            raise ValueError(f"{label} exceeds SQLite integer range")
        return value
    if field_type == "terminal_evidence":
        code = _recovery_code(value, label)
        if code not in _TERMINAL_EVIDENCE_TYPES:
            raise ValueError(f"invalid {label}: {code}")
        return code
    if field_type == "code":
        return _recovery_code(value, label)
    if field_type == "identifier":
        return _recovery_identifier(value, label)
    if field_type == "label":
        return _recovery_label(value, label)
    raise RuntimeError(f"unknown recovery field type: {field_type}")


def _recovery_label(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{label} must not be empty")
    if len(candidate) > _RECOVERY_TEXT_LIMIT or any(
        ord(character) < 32 for character in candidate
    ):
        raise ValueError(f"invalid {label}")
    clean, redacted = scrub_sensitive(candidate)
    if redacted:
        raise ValueError(f"{label} contains sensitive text")
    return clean


def _recovery_code(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if value in (None, "") and allow_empty:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if value != value.strip() or not _RECOVERY_CODE_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    clean, redacted = scrub_sensitive(value)
    if redacted or clean != value:
        raise ValueError(f"{label} contains sensitive text")
    return value


def _recovery_identifier(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> str:
    if value in (None, "") and allow_empty:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if value != value.strip() or not _RECOVERY_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    clean, redacted = scrub_sensitive(value)
    if redacted or clean != value:
        raise ValueError(f"{label} contains sensitive text")
    return value


def _prompt_digest(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("wire_prompt_text must be text")
    if not text:
        raise ValueError("wire_prompt_text must not be empty")
    encoded = text.encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _optional_cost_micro_usd(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("cost_micro_usd must be an integer")
    if value < 0:
        raise ValueError("cost_micro_usd must be non-negative")
    if value > 2**63 - 1:
        raise ValueError("cost_micro_usd exceeds SQLite integer range")
    return value


def _queue_disposition(value: Any) -> str:
    if value in (None, ""):
        return ""
    code = _recovery_code(value, "queue disposition")
    if code not in _QUEUE_DISPOSITIONS:
        raise ValueError("invalid queue disposition")
    return code


def _queue_transition_allowed(current: str, requested: str) -> bool:
    if not requested or current == requested:
        return True
    if not current:
        return True
    return current == "held" and requested in _FINAL_QUEUE_DISPOSITIONS


def _merge_stop_acknowledgement(
    existing: dict[str, Any],
    supplied: dict[str, Any],
    *,
    attempt_id: str,
) -> tuple[dict[str, Any], bool]:
    """Monotonically enrich stop evidence without rewriting durable facts."""

    merged = dict(existing)
    changed = False
    monotonic_booleans = {"acknowledged", "cancellation_confirmed", "escalated"}
    for key, value in supplied.items():
        if key in monotonic_booleans:
            prior = merged.get(key)
            if prior is True and value is False:
                raise WorkStoreError(
                    "execution attempt stop acknowledgement regressed: " f"{attempt_id}"
                )
            if prior is None or (prior is False and value is True):
                merged[key] = value
                changed = True
            continue
        if key in merged:
            if merged[key] != value:
                raise WorkStoreError(
                    "execution attempt stop acknowledgement conflicts: " f"{attempt_id}"
                )
            continue
        merged[key] = value
        changed = True

    if (
        merged.get("cancellation_confirmed") is True
        and merged.get("acknowledged") is not True
    ):
        raise WorkStoreError("confirmed cancellation requires provider acknowledgement")
    return merged, changed


def _require_matching_provider_request(
    attempt: ExecutionAttempt,
    evidence: dict[str, Any],
    *,
    label: str,
) -> None:
    if (
        not attempt.provider_request_key
        or evidence.get("request_id") != attempt.provider_request_key
    ):
        raise WorkStoreError(f"{label} does not match the durable provider request")


def _validate_optional_native_identity(
    attempt: ExecutionAttempt,
    evidence: dict[str, Any],
    *,
    label: str,
) -> None:
    supplied_native = str(evidence.get("native_id", "") or "")
    if (
        supplied_native
        and attempt.native_binding_id
        and supplied_native != attempt.native_binding_id
    ):
        raise WorkStoreError(f"{label} does not match the durable native identity")


def _validate_terminal_identity(
    attempt: ExecutionAttempt,
    evidence: dict[str, Any],
    *,
    label: str,
) -> None:
    _require_matching_provider_request(attempt, evidence, label=label)
    _validate_optional_native_identity(attempt, evidence, label=label)
    if attempt.accepted_turn_id:
        if evidence.get("turn_id") != attempt.accepted_turn_id:
            raise WorkStoreError(f"{label} does not match the durable accepted turn")
    elif (
        attempt.native_binding_id
        and evidence.get("native_id") != attempt.native_binding_id
    ):
        raise WorkStoreError(f"{label} does not match the durable native identity")


def _validate_stop_acknowledgement_correlation(
    attempt: ExecutionAttempt,
    acknowledgement: dict[str, Any],
) -> None:
    if not attempt.prompt_digest:
        raise WorkStoreError(
            "stop acknowledgement cannot precede durable provider dispatch"
        )
    _validate_terminal_identity(
        attempt,
        acknowledgement,
        label="stop acknowledgement",
    )


def _validate_execution_release(
    attempt: ExecutionAttempt,
    *,
    status: str,
    terminal_receipt: dict[str, Any],
    terminal_receipt_supplied: bool,
    usage_supplied: bool,
    cost_micro_usd: int | None,
    stop_acknowledgement: dict[str, Any],
    stop_acknowledgement_supplied: bool,
    queue_disposition: str,
) -> None:
    possible_dispatch = bool(attempt.prompt_digest)
    if not possible_dispatch:
        if (
            status != "aborted"
            or terminal_receipt_supplied
            or usage_supplied
            or cost_micro_usd is not None
            or stop_acknowledgement
            or stop_acknowledgement_supplied
        ):
            raise WorkStoreError(
                "pre-dispatch execution may release only as a local abort"
            )
        if queue_disposition == "held":
            raise WorkStoreError(
                "terminal execution requires a final queue disposition"
            )
        return

    if queue_disposition == "held":
        raise WorkStoreError("terminal execution requires a final queue disposition")

    evidence_type = str(terminal_receipt.get("evidence_type", "") or "")
    provider_status = str(terminal_receipt.get("provider_status", "") or "")
    cancellation_confirmed = bool(
        stop_acknowledgement.get("acknowledged") is True
        and stop_acknowledgement.get("cancellation_confirmed") is True
    )
    if terminal_receipt and not evidence_type:
        raise WorkStoreError("terminal receipt requires a typed evidence kind")
    if evidence_type and not provider_status:
        raise WorkStoreError(
            "terminal receipt requires an authoritative provider status"
        )
    if evidence_type == "verified_rejection":
        _require_matching_provider_request(
            attempt,
            terminal_receipt,
            label="verified rejection",
        )
        _validate_optional_native_identity(
            attempt,
            terminal_receipt,
            label="verified rejection",
        )
        if attempt.accepted_turn_id:
            raise WorkStoreError(
                "verified rejection conflicts with durable provider acceptance"
            )
        if terminal_receipt.get("turn_id"):
            raise WorkStoreError(
                "verified rejection cannot identify an accepted provider turn"
            )
        if status not in {"failed", "aborted"}:
            raise WorkStoreError(
                "verified rejection may release only a failed or aborted attempt"
            )
    elif evidence_type == "provider_terminal":
        _validate_terminal_identity(
            attempt,
            terminal_receipt,
            label="terminal receipt",
        )
    if stop_acknowledgement:
        _validate_stop_acknowledgement_correlation(
            attempt,
            stop_acknowledgement,
        )
    if cancellation_confirmed and status != "aborted":
        raise WorkStoreError(
            "acknowledged cancellation may release only an aborted attempt"
        )
    if not evidence_type and not cancellation_confirmed:
        raise WorkStoreError(
            "possible provider dispatch remains uncertain without verified "
            "rejection, provider terminal receipt, or acknowledged cancellation"
        )


def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    detail = str(exc).lower()
    return "locked" in detail or "busy" in detail


def _is_execution_audit_type(event_type: str, *, control_plane: bool) -> bool:
    return control_plane and event_type in EXECUTION_ATTEMPT_AUDIT_EVENT_TYPES


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON-serializable") from exc


def _load_json(value: str) -> dict[str, Any]:
    # Legacy Work/event/artifact metadata may contain Python JSON's historical
    # NaN/Infinity spellings. Keep reads compatible; strict recovery migration
    # and all new writes use the rejecting helpers below.
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else {}


def _load_json_list(value: str) -> list[Any]:
    loaded = json.loads(value)
    return loaded if isinstance(loaded, list) else []


def _strict_json_mapping(value: str) -> dict[str, Any]:
    loaded = json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_strict_json_object,
    )
    if not isinstance(loaded, dict):
        raise ValueError("JSON value must be a mapping")
    return loaded


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate recovery keys before JSON's last-write-wins step."""

    loaded: dict[str, Any] = {}
    normalized_keys: set[str] = set()
    for key, value in pairs:
        normalized = key.strip().lower().replace("-", "_")
        if normalized in normalized_keys:
            raise ValueError("JSON object contains duplicate normalized fields")
        normalized_keys.add(normalized)
        loaded[key] = value
    return loaded


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
