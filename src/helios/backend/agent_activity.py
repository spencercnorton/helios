"""Provider-neutral, GTK-free observations of native agent activity.

This module deliberately models *observations*, not an authoritative process
tree.  A provider can report that an actor is working, waiting, or terminal;
Helios does not infer that the actor exists outside the exact Work/root-turn
scope that produced that report, and it does not infer cancellation from a UI
state.  Durable topology and acknowledged recursive cancellation belong to


The model is intentionally independent of GTK so provider adapters, causal
fences, and monotonic status rules can run in the slim CI image.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol


class AgentObservedStatus(str, Enum):
    """Small provider-neutral status vocabulary for observed actors."""

    STARTING = "starting"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


TERMINAL_AGENT_STATUSES = frozenset(
    {
        AgentObservedStatus.COMPLETED,
        AgentObservedStatus.FAILED,
        AgentObservedStatus.STOPPED,
    }
)


@dataclass(frozen=True, slots=True)
class AgentActivityScope:
    """Causal owner for one provider's root-turn activity projection."""

    provider: str
    work_id: str
    root_turn_id: str
    root_actor_id: str

    @property
    def is_valid(self) -> bool:
        return all(
            isinstance(value, str) and value.strip()
            for value in (
                self.provider,
                self.work_id,
                self.root_turn_id,
                self.root_actor_id,
            )
        )


@dataclass(frozen=True, slots=True)
class AgentObservation:
    """One adapter-normalized provider observation."""

    actor_id: str
    status: AgentObservedStatus
    provider_status: str = ""
    name: str = ""
    role: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """Current monotonic projection for one actor in one exact scope."""

    scope: AgentActivityScope
    actor_id: str
    status: AgentObservedStatus
    provider_status: str
    name: str
    role: str
    detail: str
    ordinal: int
    #: Monotonic clock reading when this actor first entered the projection,
    #: and when it first reached a terminal status (0.0 while it has not).
    #: These belong to the model rather than to the dock because the Plan pane
    #: rebuilds its rows from scratch on every revision and has nowhere to keep
    #: an origin — and because "when did Helios first see this actor" is a
    #: lifecycle fact about the observation, which is what this module owns.
    #: Both default to 0.0 so a hand-built AgentActivity stays legal and simply
    #: reports no duration.
    first_observed_at: float = 0.0
    terminal_at: float = 0.0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_AGENT_STATUSES

    def elapsed(self, now: float) -> float:
        """Seconds observed, frozen once terminal. 0.0 when unstamped.

        Deliberately measured from FIRST OBSERVATION, not from any provider
        start time: no provider reports one, and inventing it from a tool_use
        timestamp would be the kind of inference the rest of this module
        refuses to make. It answers "how long has Helios been watching this",
        which is the question a stuck fan-out actually raises.
        """
        if not self.first_observed_at:
            return 0.0
        return max(0.0, (self.terminal_at or now) - self.first_observed_at)

    @property
    def group(self) -> str:
        if self.status is AgentObservedStatus.NEEDS_INPUT:
            return "needs_you"
        if self.is_terminal:
            return "done"
        if self.status is AgentObservedStatus.UNKNOWN:
            return "observed"
        return "active"


@dataclass(frozen=True, slots=True)
class AgentActivitySnapshot:
    """Immutable view consumed by the dock and optional Plan pane."""

    scope: AgentActivityScope | None = None
    activities: tuple[AgentActivity, ...] = ()
    revision: int = 0

    @property
    def active(self) -> tuple[AgentActivity, ...]:
        return tuple(item for item in self.activities if item.group == "active")

    @property
    def needs_you(self) -> tuple[AgentActivity, ...]:
        return tuple(item for item in self.activities if item.group == "needs_you")

    @property
    def done(self) -> tuple[AgentActivity, ...]:
        return tuple(item for item in self.activities if item.group == "done")

    @property
    def observed(self) -> tuple[AgentActivity, ...]:
        return tuple(item for item in self.activities if item.group == "observed")


class AgentActivityAdapter(Protocol):
    """Metadata-only adapter contract for one provider surface."""

    provider: str
    available: bool

    def observations(self, snapshot: Any) -> tuple[AgentObservation, ...]: ...


class CodexAgentActivityAdapter:
    """Normalize keyed Codex App Server ``agentsStates`` snapshots."""

    provider = "openai"
    available = True

    def observations(self, snapshot: Any) -> tuple[AgentObservation, ...]:
        observations: list[AgentObservation] = []
        for actor_id, state in _codex_agent_states(snapshot):
            status_text = _first_text(state, "status", "kind", "phase")
            observations.append(
                AgentObservation(
                    actor_id=actor_id,
                    status=normalize_agent_status(status_text),
                    provider_status=status_text,
                    name=_first_text(
                        state,
                        "name",
                        "agentNickname",
                        "agentRole",
                    ),
                    role=_first_text(state, "role", "agentRole", "tool"),
                    detail=_first_text(state, "message", "path", "agentPath"),
                )
            )
        return tuple(observations)


class ClaudeAgentActivityAdapter:
    """Normalize the Claude driver's turn-scoped delegation actors.

    This replaces the long-standing dormant placeholder, whose objection was
    specific and correct: *tool-use records prove that delegation was requested,
    but do not provide a complete, causally paired actor lifecycle*, so
    manufacturing running/completed states from them would be less truthful than
    showing nothing.

    That precondition is now met rather than waived. Every state the driver
    reports is keyed on the `Task`/`Workflow` tool_use_id, which the child's own
    stream and terminal records carry back as `parent_tool_use_id`:

    * ``starting`` — the delegation request, provider-proven but nothing observed
      from the child yet. Deliberately not ``running``.
    * ``working``  — the child emitted content under that parent id.
    * ``complete`` / ``error`` / ``stopped`` — the child's own terminal record,
      or the root-level tool_result that unblocks the root model.

    Nothing is derived from elapsed time, ordering, or the absence of a signal,
    so an actor whose child never reports simply stays ``starting`` instead of
    being promoted to a state Helios cannot see.
    """

    provider = "anthropic"
    available = True

    def observations(self, snapshot: Any) -> tuple[AgentObservation, ...]:
        if not isinstance(snapshot, dict):
            return ()
        observations: list[AgentObservation] = []
        for actor_id, state in snapshot.items():
            if not isinstance(actor_id, str) or not actor_id.strip():
                continue
            if not isinstance(state, dict):
                continue
            status_text = _first_text(state, "status")
            observations.append(
                AgentObservation(
                    actor_id=actor_id,
                    status=normalize_agent_status(status_text),
                    provider_status=status_text,
                    name=_first_text(state, "name", "role", "tool"),
                    role=_first_text(state, "role", "tool"),
                    detail=_first_text(state, "message"),
                )
            )
        return tuple(observations)


class AgentActivityModel:
    """Turn-scoped actor model with stale-scope and status regression fences."""

    def __init__(
        self,
        adapters: tuple[AgentActivityAdapter, ...] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        selected = (
            adapters
            if adapters is not None
            else (
                CodexAgentActivityAdapter(),
                ClaudeAgentActivityAdapter(),
            )
        )
        self._adapters = {adapter.provider: adapter for adapter in selected}
        self._scope: AgentActivityScope | None = None
        self._records: dict[str, AgentActivity] = {}
        self._next_ordinal = 0
        self._revision = 0
        self._retired_scopes: set[AgentActivityScope] = set()
        # Injected so duration assertions are not wall-clock races. Monotonic,
        # never time.time(): a suspend or an NTP step must not make a running
        # agent look like it finished before it started.
        self._clock = clock

    @property
    def scope(self) -> AgentActivityScope | None:
        return self._scope

    def reset(self) -> AgentActivitySnapshot:
        """Detach the visible conversation and discard its in-memory view."""

        changed = self._scope is not None or bool(self._records)
        self._scope = None
        self._records.clear()
        self._next_ordinal = 0
        self._retired_scopes.clear()
        if changed:
            self._revision += 1
        return self.snapshot()

    def begin_scope(self, scope: AgentActivityScope) -> AgentActivitySnapshot:
        """Activate a provider-confirmed root turn and clear the prior turn.

        A scope retired by a newer root turn cannot be reactivated by a late
        callback.  ``reset`` intentionally clears that fence when the user
        navigates away and later returns to a still-live conversation; sender
        guards then establish the visible driver before its snapshot is read.
        """

        if not scope.is_valid:
            return self.reset()
        if scope == self._scope:
            return self.snapshot()
        if scope in self._retired_scopes:
            return self.snapshot()
        if self._scope is not None:
            self._retire(self._scope)
        self._scope = scope
        self._records.clear()
        self._next_ordinal = 0
        self._revision += 1
        return self.snapshot()

    def observe(self, scope: AgentActivityScope, raw_snapshot: Any) -> AgentActivitySnapshot:
        """Merge one exact-scope provider snapshot without regressing state."""

        if not scope.is_valid:
            return self.snapshot()
        if self._scope is None:
            self.begin_scope(scope)
        if scope != self._scope or scope in self._retired_scopes:
            return self.snapshot()
        adapter = self._adapters.get(scope.provider)
        if adapter is None or not adapter.available:
            return self.snapshot()

        changed = False
        for observation in adapter.observations(raw_snapshot):
            actor_id = observation.actor_id.strip()
            if not actor_id or actor_id == scope.root_actor_id:
                continue
            current = self._records.get(actor_id)
            if current is None:
                self._next_ordinal += 1
                now = self._clock()
                terminal = observation.status in TERMINAL_AGENT_STATUSES
                self._records[actor_id] = AgentActivity(
                    scope=scope,
                    actor_id=actor_id,
                    status=observation.status,
                    provider_status=observation.provider_status,
                    name=observation.name or actor_id[:8],
                    role=observation.role or "subagent",
                    detail=observation.detail,
                    ordinal=self._next_ordinal,
                    first_observed_at=now,
                    # An actor first seen ALREADY terminal has a duration of
                    # zero, not one that keeps growing. Its whole life happened
                    # before Helios looked.
                    terminal_at=now if terminal else 0.0,
                )
                changed = True
                continue

            # A conflicting late event may improve stable identity metadata,
            # but must not repaint terminal status or explanatory detail.
            # Same-terminal enrichment remains safe.
            if current.is_terminal and observation.status is not current.status:
                updated = replace(
                    current,
                    name=observation.name or current.name,
                    role=observation.role or current.role,
                )
                if updated != current:
                    self._records[actor_id] = updated
                    changed = True
                continue

            status = _monotonic_status(current.status, observation.status)
            provider_status = current.provider_status
            if status is not current.status:
                provider_status = observation.provider_status or status.value
            elif not provider_status and observation.provider_status:
                provider_status = observation.provider_status
            updated = replace(
                current,
                status=status,
                provider_status=provider_status,
                name=observation.name or current.name,
                role=observation.role or current.role,
                detail=observation.detail or current.detail,
                # Stamped on the FIRST crossing only. _monotonic_status already
                # forbids leaving a terminal state, so this can never be reset
                # by a later event, and a completed agent's duration stops
                # growing the moment it lands.
                terminal_at=(
                    self._clock()
                    if status in TERMINAL_AGENT_STATUSES and not current.terminal_at
                    else current.terminal_at
                ),
            )
            if updated != current:
                self._records[actor_id] = updated
                changed = True

        if changed:
            self._revision += 1
        return self.snapshot()

    def snapshot(self) -> AgentActivitySnapshot:
        activities = tuple(sorted(self._records.values(), key=lambda item: item.ordinal))
        return AgentActivitySnapshot(
            scope=self._scope,
            activities=activities,
            revision=self._revision,
        )

    def _retire(self, scope: AgentActivityScope) -> None:
        self._retired_scopes.add(scope)


def normalize_agent_status(value: Any) -> AgentObservedStatus:
    text = str(value or "").strip()
    folded = "".join(character for character in text.lower() if character.isalnum())
    if folded in {"pendinginit", "pending", "starting", "initializing", "queued"}:
        return AgentObservedStatus.STARTING
    if folded in {"inprogress", "running", "active", "working"}:
        return AgentObservedStatus.RUNNING
    if folded in {
        "needsinput",
        "requiresinput",
        "awaitinginput",
        "waitingforinput",
        "pendingapproval",
        "requiresapproval",
        "awaitingapproval",
        "approvalrequired",
    }:
        return AgentObservedStatus.NEEDS_INPUT
    if folded in {"completed", "complete", "success", "succeeded", "done"}:
        return AgentObservedStatus.COMPLETED
    if folded in {"errored", "error", "failed", "notfound"}:
        return AgentObservedStatus.FAILED
    if folded in {
        "interrupted",
        "shutdown",
        "stopped",
        "cancelled",
        "canceled",
        "aborted",
    }:
        return AgentObservedStatus.STOPPED
    return AgentObservedStatus.UNKNOWN


def agent_status_label(status: AgentObservedStatus) -> str:
    return {
        AgentObservedStatus.STARTING: "Starting",
        AgentObservedStatus.RUNNING: "Working",
        AgentObservedStatus.NEEDS_INPUT: "Needs you",
        AgentObservedStatus.COMPLETED: "Complete",
        AgentObservedStatus.FAILED: "Error",
        AgentObservedStatus.STOPPED: "Stopped",
        AgentObservedStatus.UNKNOWN: "Observed",
    }[status]


def _monotonic_status(
    current: AgentObservedStatus,
    observed: AgentObservedStatus,
) -> AgentObservedStatus:
    if current in TERMINAL_AGENT_STATUSES:
        return current
    if current in {AgentObservedStatus.RUNNING, AgentObservedStatus.NEEDS_INPUT}:
        if observed is AgentObservedStatus.STARTING:
            return current
    return observed


def _codex_agent_states(snapshot: Any) -> tuple[tuple[str, dict[str, Any]], ...]:
    container = snapshot
    if isinstance(snapshot, dict):
        item = snapshot.get("item")
        if isinstance(item, dict) and "agentsStates" in item:
            container = item.get("agentsStates")
        else:
            for key in ("agentsStates", "agents", "states"):
                if key in snapshot:
                    container = snapshot.get(key)
                    break

    states: list[tuple[str, dict[str, Any]]] = []
    if isinstance(container, dict):
        for actor_id, raw_state in container.items():
            if isinstance(raw_state, dict):
                state = dict(raw_state)
            elif isinstance(raw_state, str):
                state = {"status": raw_state}
            else:
                continue
            states.append((str(actor_id), state))
    elif isinstance(container, list):
        for index, raw_state in enumerate(container):
            if not isinstance(raw_state, dict):
                continue
            actor_id = (
                raw_state.get("threadId")
                or raw_state.get("id")
                or raw_state.get("name")
                or f"agent-{index + 1}"
            )
            states.append((str(actor_id), dict(raw_state)))
    return tuple(states)


def _first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
