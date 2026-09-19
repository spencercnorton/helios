"""Normalize Codex App Server v2 notifications into Helios driver actions.

The App Server is a bidirectional JSON-RPC transport whose notification
vocabulary differs from both ``codex exec --json`` and Claude's stream-json
format.  This module is deliberately GTK-free: one accumulator owns the
state for one Codex thread and turns notifications into the same
``StreamingAssistant``/``Turn``/``ToolResult`` shapes the existing drivers
already consume, plus small dictionaries for richer native events.

Security boundary: App Server reasoning items can contain both a public
``summary`` and raw hidden ``content``.  Helios only accepts summary fields
and ``item/reasoning/summaryTextDelta``.  ``item/reasoning/textDelta`` and
reasoning ``content`` are never copied into state or emitted in an action.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any

from helios.backend.process.codex_events import (
    ACT_ERROR,
    ACT_RESULT,
    ACT_SESSION_STARTED,
    ACT_STREAMING,
    ACT_TURN,
    Action,
    _strip_shell_wrapper,
)
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.transcript import ToolResult

# App Server v2 notification methods.  Keep these string-exact: the process
# hub uses them to route multiplexed notifications to the right accumulator.
METHOD_THREAD_STARTED = "thread/started"
METHOD_TURN_STARTED = "turn/started"
METHOD_TURN_COMPLETED = "turn/completed"
METHOD_ITEM_STARTED = "item/started"
METHOD_ITEM_COMPLETED = "item/completed"
METHOD_AGENT_MESSAGE_DELTA = "item/agentMessage/delta"
METHOD_PLAN_DELTA = "item/plan/delta"
METHOD_REASONING_SUMMARY_DELTA = "item/reasoning/summaryTextDelta"
METHOD_REASONING_SUMMARY_PART_ADDED = "item/reasoning/summaryPartAdded"
METHOD_REASONING_TEXT_DELTA = "item/reasoning/textDelta"
METHOD_COMMAND_OUTPUT_DELTA = "item/commandExecution/outputDelta"
METHOD_FILE_CHANGE_OUTPUT_DELTA = "item/fileChange/outputDelta"
METHOD_FILE_CHANGE_PATCH_UPDATED = "item/fileChange/patchUpdated"
METHOD_MCP_PROGRESS = "item/mcpToolCall/progress"
METHOD_THREAD_TOKEN_USAGE = "thread/tokenUsage/updated"
METHOD_ACCOUNT_RATE_LIMITS = "account/rateLimits/updated"
METHOD_TURN_DIFF_UPDATED = "turn/diff/updated"
METHOD_TURN_PLAN_UPDATED = "turn/plan/updated"
METHOD_ERROR = "error"

#: Every server notification `feed_notification` has a branch for. Helios opts
#: into the experimental API because collaboration modes require it, so this
#: set is only the actively interpreted surface; the protocol registry also
#: classifies known ignored/unsupported methods and distinguishes true schema
#: drift.
HANDLED_NOTIFICATION_METHODS: frozenset[str] = frozenset(
    {
        METHOD_THREAD_STARTED,
        METHOD_TURN_STARTED,
        METHOD_TURN_COMPLETED,
        METHOD_ITEM_STARTED,
        METHOD_ITEM_COMPLETED,
        METHOD_AGENT_MESSAGE_DELTA,
        METHOD_PLAN_DELTA,
        METHOD_REASONING_SUMMARY_DELTA,
        METHOD_REASONING_SUMMARY_PART_ADDED,
        METHOD_REASONING_TEXT_DELTA,
        METHOD_COMMAND_OUTPUT_DELTA,
        METHOD_FILE_CHANGE_OUTPUT_DELTA,
        METHOD_FILE_CHANGE_PATCH_UPDATED,
        METHOD_MCP_PROGRESS,
        METHOD_THREAD_TOKEN_USAGE,
        METHOD_ACCOUNT_RATE_LIMITS,
        METHOD_TURN_DIFF_UPDATED,
        METHOD_TURN_PLAN_UPDATED,
        METHOD_ERROR,
    }
)

# Native action vocabulary.  Legacy render/finalization actions above retain
# their existing payload classes.  Every new action carries a plain dict so a
# GTK driver can translate it into GObject signals without knowing this
# accumulator's internals.
ACT_ITEM_LIFECYCLE = "item-lifecycle"
ACT_ITEM_DELTA = "item-delta"
ACT_ACTIVITY = "activity"
ACT_MCP_ACTIVITY = "mcp-activity"
ACT_SUBAGENT_ACTIVITY = "subagent-activity"
ACT_TURN_STATUS = "turn-status"
ACT_PLAN_UPDATED = "plan-updated"
ACT_DIFF_UPDATED = "diff-updated"
ACT_USAGE_UPDATED = "usage-updated"
ACT_RATE_LIMIT_UPDATED = "rate-limit-updated"
ACT_CONTEXT_COMPACTED = "context-compacted"

_ACTIVITY_ITEM_TYPES = {
    "commandExecution",
    "fileChange",
    "webSearch",
    "dynamicToolCall",
    "imageView",
    "imageGeneration",
}
_REVIEW_MODE_ITEM_TYPES = {"enteredReviewMode", "exitedReviewMode"}
_MCP_ITEM_TYPES = {"mcpToolCall"}
_SUBAGENT_ITEM_TYPES = {"collabAgentToolCall", "subAgentActivity"}
_TERMINAL_TOOL_STATUSES = {"completed", "failed", "declined"}
_CONTENT_ITEM_TYPES = {"agentMessage", "reasoning", "plan"}
_CONTEXT_COMPACTION_ITEM = "contextCompaction"


@dataclass(slots=True)
class CodexAppEventAccumulator:
    """Accumulate App Server notifications for one thread and active turn."""

    model: str = ""
    thread_id: str = ""
    turn_id: str = ""
    _streaming: StreamingAssistant | None = None
    _block_by_item: dict[str, int] = field(default_factory=dict)
    _item_types: dict[str, str] = field(default_factory=dict)
    _message_phases: dict[str, str | None] = field(default_factory=dict)
    _reasoning_parts: dict[str, list[str]] = field(default_factory=dict)
    _command_output: dict[str, str] = field(default_factory=dict)
    _plan_text: dict[str, str] = field(default_factory=dict)
    _tool_results: list[ToolResult] = field(default_factory=list)
    _completed_tool_ids: set[str] = field(default_factory=set)
    _agent_message_ids: list[str] = field(default_factory=list)
    _review_fallback: tuple[str, str] | None = None
    _lifecycle_seen: set[tuple[str, str]] = field(default_factory=set)
    _last_token_usage: dict[str, Any] = field(default_factory=dict)
    _rate_limits: dict[str, Any] = field(default_factory=dict)
    _turn_started_monotonic: float = 0.0
    _announced: bool = False

    @property
    def streaming(self) -> StreamingAssistant | None:
        """The live object shared with Helios's transcript/activity views."""

        return self._streaming

    @property
    def rate_limits(self) -> dict[str, Any]:
        """Return a defensive snapshot of the merged account limit state."""

        return copy.deepcopy(self._rate_limits)

    def feed_line(self, line: str) -> list[Action]:
        """Parse one App Server JSONL line, ignoring malformed/non-notifications."""

        if not isinstance(line, str) or not line.strip():
            return []
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return []
        return self.feed(message)

    def feed(self, message: dict[str, Any]) -> list[Action]:
        """Consume a JSON-RPC notification object.

        Responses have an ``id`` and no notification ``method``; callers can
        safely pass every decoded server message here because unknown shapes
        are ignored.
        """

        if not isinstance(message, dict):
            return []
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return []
        return self.feed_notification(method, params)

    def feed_notification(self, method: str, params: dict[str, Any]) -> list[Action]:
        """Consume one string-exact App Server v2 notification."""

        if not isinstance(params, dict):
            return []

        # Raw reasoning is model-private.  Drop it before any generic action,
        # buffer, loggable payload, or caller-owned object can retain it.
        if method in {METHOD_REASONING_TEXT_DELTA, METHOD_FILE_CHANGE_OUTPUT_DELTA}:
            return []

        if method == METHOD_ACCOUNT_RATE_LIMITS:
            return self._apply_rate_limits(params)

        if method == METHOD_THREAD_STARTED:
            thread = params.get("thread") or {}
            incoming = thread.get("id") if isinstance(thread, dict) else ""
            if not isinstance(incoming, str) or not incoming:
                return []
            if self.thread_id and incoming != self.thread_id:
                return []
            self.thread_id = incoming
            if not self._announced:
                self._announced = True
                return [Action(ACT_SESSION_STARTED, incoming)]
            return []

        if not self._belongs_to_thread(params):
            return []

        if method == METHOD_TURN_STARTED:
            return self._start_turn(params)
        if method in {METHOD_ITEM_STARTED, METHOD_ITEM_COMPLETED}:
            lifecycle = "started" if method == METHOD_ITEM_STARTED else "completed"
            item = params.get("item") or {}
            return self._apply_item(item, lifecycle, params)
        if method == METHOD_AGENT_MESSAGE_DELTA:
            return self._apply_agent_delta(params)
        if method == METHOD_REASONING_SUMMARY_PART_ADDED:
            return self._apply_reasoning_part(params)
        if method == METHOD_REASONING_SUMMARY_DELTA:
            return self._apply_reasoning_delta(params)
        if method == METHOD_PLAN_DELTA:
            return self._apply_plan_delta(params)
        if method == METHOD_COMMAND_OUTPUT_DELTA:
            return self._apply_command_delta(params)
        if method == METHOD_FILE_CHANGE_PATCH_UPDATED:
            return self._apply_file_patch(params)
        if method == METHOD_MCP_PROGRESS:
            return self._apply_mcp_progress(params)
        if method == METHOD_THREAD_TOKEN_USAGE:
            return self._apply_token_usage(params)
        if method == METHOD_TURN_PLAN_UPDATED:
            return self._apply_turn_plan(params)
        if method == METHOD_TURN_DIFF_UPDATED:
            return self._apply_turn_diff(params)
        if method == METHOD_TURN_COMPLETED:
            return self._finish_turn(params)
        if method == METHOD_ERROR:
            return self._apply_error(params)
        return []

    # -- turn lifecycle -------------------------------------------------

    def _start_turn(self, params: dict[str, Any]) -> list[Action]:
        turn = params.get("turn") or {}
        if not isinstance(turn, dict):
            turn = {}
        turn_id = turn.get("id") or params.get("turnId") or ""
        self.turn_id = turn_id if isinstance(turn_id, str) else ""
        incoming_thread = params.get("threadId") or self.thread_id
        if isinstance(incoming_thread, str) and incoming_thread:
            self.thread_id = incoming_thread
        self._turn_started_monotonic = time.monotonic()
        self._streaming = StreamingAssistant(model=self.model)
        self._block_by_item = {}
        self._item_types = {}
        self._message_phases = {}
        self._reasoning_parts = {}
        self._command_output = {}
        self._plan_text = {}
        self._tool_results = []
        self._completed_tool_ids = set()
        self._agent_message_ids = []
        self._review_fallback = None
        self._lifecycle_seen = set()
        self._last_token_usage = {}
        status = turn.get("status") or "inProgress"
        return [
            Action(ACT_TURN_STATUS, self._turn_status_payload(status, turn)),
            Action(ACT_STREAMING, self._streaming),
        ]

    def _finish_turn(self, params: dict[str, Any]) -> list[Action]:
        turn = params.get("turn") or {}
        if not isinstance(turn, dict):
            turn = {}
        turn_id = turn.get("id") or params.get("turnId") or self.turn_id
        if isinstance(turn_id, str) and turn_id:
            self.turn_id = turn_id

        actions: list[Action] = []
        # The completed turn can repair a missed item notification.  Final
        # item objects are authoritative but lifecycle actions are deduped.
        items = turn.get("items") or []
        if isinstance(items, list):
            tracked_content_ids = {
                item_id
                for item_id in self._block_by_item
                if self._item_types.get(item_id) in _CONTENT_ITEM_TYPES
            }
            for item in items:
                if isinstance(item, dict):
                    actions.extend(self._apply_item(item, "completed", params))
            self._reconcile_authoritative_content(
                items,
                tracked_before_completion=tracked_content_ids,
            )
        self._materialize_review_fallback()

        if self._streaming is not None:
            completed_turn = self._streaming.to_turn()
            completed_turn.tool_results.extend(self._tool_results)
            if completed_turn.has_content:
                actions.append(Action(ACT_TURN, completed_turn))

        status = turn.get("status") or "completed"
        if status not in {"completed", "interrupted", "failed"}:
            status = "failed" if turn.get("error") else "completed"
        actions.append(Action(ACT_TURN_STATUS, self._turn_status_payload(status, turn)))

        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        if status == "failed":
            message = error.get("message") or "Codex turn failed."
            actions.append(Action(ACT_ERROR, str(message)))

        actions.append(Action(ACT_RESULT, self._result_payload(status, turn)))
        self._streaming = None
        # The block-index maps are only valid for the stream we just dropped.
        # Reset them with it so a late/duplicate turn event can't reindex the
        # old slots into a fresh, shorter StreamingAssistant (that raised
        # IndexError in _reconcile_authoritative_content).
        self._block_by_item = {}
        self._item_types = {}
        self._message_phases = {}
        self._tool_results = []
        self._completed_tool_ids = set()
        self._agent_message_ids = []
        self._review_fallback = None
        return actions

    def _turn_status_payload(self, status: Any, turn: dict[str, Any]) -> dict[str, Any]:
        error = turn.get("error")
        payload = {
            "threadId": self.thread_id,
            "turnId": self.turn_id,
            "status": str(status or ""),
            "durationMs": _optional_nonnegative_int(turn.get("durationMs")),
            "error": copy.deepcopy(error) if isinstance(error, dict) else None,
        }
        return payload

    def _result_payload(self, status: str, turn: dict[str, Any]) -> dict[str, Any]:
        duration_ms = _optional_nonnegative_int(turn.get("durationMs"))
        if duration_ms is None:
            duration_ms = 0
            if self._turn_started_monotonic:
                duration_ms = int(
                    (time.monotonic() - self._turn_started_monotonic) * 1000
                )
        last = self._last_token_usage.get("last") or {}
        if not isinstance(last, dict):
            last = {}
        input_tokens = _nonnegative_int(last.get("inputTokens"))
        cached_tokens = _nonnegative_int(last.get("cachedInputTokens"))
        output_tokens = _nonnegative_int(last.get("outputTokens"))
        subtype = {"completed": "success", "interrupted": "aborted"}.get(
            status, "error"
        )
        result = {
            "type": "result",
            "subtype": subtype,
            "provider": "openai",
            "total_cost_usd": 0.0,
            "duration_ms": duration_ms,
            "usage": {
                "input_tokens": max(0, input_tokens - cached_tokens),
                "cache_read_input_tokens": cached_tokens,
                "cache_creation_input_tokens": 0,
                "output_tokens": output_tokens,
            },
            "modelUsage": {},
        }
        if status == "failed" and isinstance(turn.get("error"), dict):
            result["error"] = copy.deepcopy(turn["error"])
        return result

    # -- item lifecycle and streamed deltas -----------------------------

    def _apply_item(
        self,
        item: Any,
        lifecycle: str,
        params: dict[str, Any],
    ) -> list[Action]:
        if not isinstance(item, dict):
            return []
        item_id = item.get("id")
        item_type = item.get("type")
        if not isinstance(item_id, str) or not item_id:
            return []
        if not isinstance(item_type, str) or not item_type:
            return []

        self._item_types[item_id] = item_type
        actions: list[Action] = []
        lifecycle_key = (item_id, lifecycle)
        first_lifecycle = lifecycle_key not in self._lifecycle_seen
        if first_lifecycle:
            self._lifecycle_seen.add(lifecycle_key)
            actions.append(
                Action(
                    ACT_ITEM_LIFECYCLE,
                    {
                        "threadId": _string(params.get("threadId"), self.thread_id),
                        "turnId": _string(params.get("turnId"), self.turn_id),
                        "itemId": item_id,
                        "itemType": item_type,
                        "lifecycle": lifecycle,
                        "item": _public_item(item),
                    },
                )
            )

        if item_type == "agentMessage":
            actions.extend(self._apply_agent_item(item, lifecycle))
        elif item_type == "reasoning":
            actions.extend(self._apply_reasoning_item(item, lifecycle))
        elif item_type == "plan":
            actions.extend(self._apply_plan_item(item, lifecycle, params))
        elif item_type in _ACTIVITY_ITEM_TYPES | _MCP_ITEM_TYPES | _SUBAGENT_ITEM_TYPES:
            actions.extend(self._apply_tool_item(item, lifecycle, params))
        elif item_type in _REVIEW_MODE_ITEM_TYPES and first_lifecycle:
            # Current App Server builds also carry the final output in an
            # ordinary agentMessage. Retain exitedReviewMode.review only as a
            # completion fallback for versions that follow the documented
            # sentinel-only contract; materialization deduplicates it.
            if item_type == "exitedReviewMode" and lifecycle == "completed":
                review = item.get("review")
                if isinstance(review, str) and review.strip():
                    self._review_fallback = (item_id, review)
            phase = "reviewing" if item_type == "enteredReviewMode" else "requesting"
            actions.append(
                Action(
                    ACT_ACTIVITY,
                    {
                        "threadId": _string(params.get("threadId"), self.thread_id),
                        "turnId": _string(params.get("turnId"), self.turn_id),
                        "itemId": item_id,
                        "itemType": item_type,
                        "category": "phase",
                        "phase": phase,
                        "lifecycle": lifecycle,
                    },
                )
            )
        elif item_type == _CONTEXT_COMPACTION_ITEM and first_lifecycle:
            actions.append(
                Action(
                    ACT_CONTEXT_COMPACTED,
                    {
                        "threadId": _string(
                            params.get("threadId"),
                            self.thread_id,
                        ),
                        "turnId": _string(params.get("turnId"), self.turn_id),
                        "itemId": item_id,
                        "lifecycle": lifecycle,
                    },
                )
            )
        return actions

    def _materialize_review_fallback(self) -> None:
        """Use documented review text only when no agent message supplied it."""

        fallback = self._review_fallback
        if fallback is None:
            return
        streaming = self._streaming
        if streaming is not None:
            for item_id in self._agent_message_ids:
                index = self._block_by_item.get(item_id)
                if index is None or index >= len(streaming.blocks):
                    continue
                block = streaming.blocks[index]
                if block.type == "text" and block.text.strip():
                    return
        item_id, review = fallback
        block = self._block_for(item_id)
        block.type = "text"
        block.text = review
        self._mark_ttft(review)

    def _apply_agent_item(self, item: dict[str, Any], lifecycle: str) -> list[Action]:
        item_id = item["id"]
        phase = item.get("phase")
        if phase not in {"commentary", "final_answer"}:
            phase = self._message_phases.get(item_id)
        self._message_phases[item_id] = phase
        if phase != "commentary" and item_id not in self._agent_message_ids:
            self._demote_prior_agent_text(item_id)
            self._agent_message_ids.append(item_id)
        block = self._block_for(item_id)
        block.type = self._agent_block_kind(item_id, phase)
        # Started and completed items carry accumulated text.  Completion is
        # authoritative, including an explicitly empty terminal value.
        if "text" in item or lifecycle == "completed":
            block.text = item.get("text") if isinstance(item.get("text"), str) else ""
        self._mark_ttft(block.text)
        return [Action(ACT_STREAMING, self._ensure_streaming())]

    def _apply_agent_delta(self, params: dict[str, Any]) -> list[Action]:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
            return []
        self._item_types.setdefault(item_id, "agentMessage")
        phase = self._message_phases.get(item_id)
        block = self._block_for(item_id)
        block.type = self._agent_block_kind(item_id, phase)
        block.text += delta
        self._mark_ttft(delta)
        return [
            Action(
                ACT_ITEM_DELTA,
                self._delta_payload(params, item_id, delta, messagePhase=phase),
            ),
            Action(ACT_STREAMING, self._ensure_streaming()),
        ]

    def _apply_reasoning_item(
        self, item: dict[str, Any], lifecycle: str
    ) -> list[Action]:
        item_id = item["id"]
        block = self._block_for(item_id)
        block.type = "reasoning_summary"
        # Never inspect item["content"].  Only the public summary is allowed.
        if "summary" in item or lifecycle == "completed":
            summary = _public_reasoning_summary(item)
            self._reasoning_parts[item_id] = summary
            block.text = _join_summary(summary)
        self._mark_ttft(block.text)
        return [Action(ACT_STREAMING, self._ensure_streaming())]

    def _apply_reasoning_part(self, params: dict[str, Any]) -> list[Action]:
        item_id = params.get("itemId")
        index = params.get("summaryIndex")
        if not isinstance(item_id, str) or not item_id or not isinstance(index, int):
            return []
        if index < 0:
            return []
        parts = self._reasoning_parts.setdefault(item_id, [])
        _ensure_string_index(parts, index)
        self._item_types.setdefault(item_id, "reasoning")
        return [
            Action(
                ACT_ITEM_DELTA,
                self._delta_payload(params, item_id, "", summaryIndex=index),
            )
        ]

    def _apply_reasoning_delta(self, params: dict[str, Any]) -> list[Action]:
        item_id = params.get("itemId")
        index = params.get("summaryIndex")
        delta = params.get("delta")
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(index, int)
            or index < 0
            or not isinstance(delta, str)
        ):
            return []
        parts = self._reasoning_parts.setdefault(item_id, [])
        _ensure_string_index(parts, index)
        parts[index] += delta
        self._item_types.setdefault(item_id, "reasoning")
        block = self._block_for(item_id)
        block.type = "reasoning_summary"
        block.text = _join_summary(parts)
        self._mark_ttft(delta)
        return [
            Action(
                ACT_ITEM_DELTA,
                self._delta_payload(
                    params, item_id, delta, summaryIndex=index, publicReasoning=True
                ),
            ),
            Action(ACT_STREAMING, self._ensure_streaming()),
        ]

    def _apply_plan_item(
        self,
        item: dict[str, Any],
        lifecycle: str,
        params: dict[str, Any],
    ) -> list[Action]:
        item_id = item["id"]
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        self._plan_text[item_id] = text
        block = self._block_for(item_id)
        block.type = "text"
        block.text = text
        payload = {
            "threadId": _string(params.get("threadId"), self.thread_id),
            "turnId": _string(params.get("turnId"), self.turn_id),
            "itemId": item_id,
            "text": text,
            "source": "item",
            "authoritative": lifecycle == "completed",
        }
        return [
            Action(ACT_PLAN_UPDATED, payload),
            Action(ACT_STREAMING, self._ensure_streaming()),
        ]

    def _apply_plan_delta(self, params: dict[str, Any]) -> list[Action]:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
            return []
        text = self._plan_text.get(item_id, "") + delta
        self._plan_text[item_id] = text
        self._item_types.setdefault(item_id, "plan")
        block = self._block_for(item_id)
        block.type = "text"
        block.text = text
        payload = {
            "threadId": _string(params.get("threadId"), self.thread_id),
            "turnId": _string(params.get("turnId"), self.turn_id),
            "itemId": item_id,
            "text": text,
            "delta": delta,
            "source": "item",
            "authoritative": False,
        }
        return [
            Action(ACT_ITEM_DELTA, self._delta_payload(params, item_id, delta)),
            Action(ACT_PLAN_UPDATED, payload),
            Action(ACT_STREAMING, self._ensure_streaming()),
        ]

    def _apply_command_delta(self, params: dict[str, Any]) -> list[Action]:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
            return []
        self._command_output[item_id] = self._command_output.get(item_id, "") + delta
        payload = self._delta_payload(params, item_id, delta)
        payload.update({"category": "command", "phase": "progress"})
        return [Action(ACT_ACTIVITY, payload)]

    def _apply_file_patch(self, params: dict[str, Any]) -> list[Action]:
        raw_changes = params.get("changes")
        if not isinstance(raw_changes, list):
            return []
        changes: list[dict[str, Any]] = []
        diffs: list[str] = []
        for raw in raw_changes:
            if not isinstance(raw, dict):
                continue
            diff = raw.get("diff")
            path = raw.get("path")
            if not isinstance(diff, str) or not isinstance(path, str):
                continue
            change = {
                "path": path,
                "diff": diff,
                "kind": copy.deepcopy(raw.get("kind")),
            }
            changes.append(change)
            if diff:
                diffs.append(diff)
        if not changes:
            return []
        return [
            Action(
                ACT_DIFF_UPDATED,
                {
                    "threadId": _string(params.get("threadId"), self.thread_id),
                    "turnId": _string(params.get("turnId"), self.turn_id),
                    "itemId": _string(params.get("itemId")),
                    "changes": changes,
                    "diff": "\n".join(diffs),
                    "authoritative": False,
                },
            )
        ]

    def _apply_tool_item(
        self,
        item: dict[str, Any],
        lifecycle: str,
        params: dict[str, Any],
    ) -> list[Action]:
        item_id = item["id"]
        name, tool_input = _tool_for_item(item)
        block = self._block_for(item_id)
        block.type = "tool_use"
        block.tool_use_name = name
        block.tool_use_id = item_id
        block.tool_use_input_json = _json_object(tool_input)
        if lifecycle == "completed":
            self._capture_tool_result(item)

        payload = _activity_payload(item, lifecycle, params, self.thread_id, self.turn_id)
        item_type = item.get("type")
        if item_type in _MCP_ITEM_TYPES:
            activity_action = Action(ACT_MCP_ACTIVITY, payload)
        elif item_type in _SUBAGENT_ITEM_TYPES:
            activity_action = Action(ACT_SUBAGENT_ACTIVITY, payload)
        else:
            activity_action = Action(ACT_ACTIVITY, payload)
        return [activity_action, Action(ACT_STREAMING, self._ensure_streaming())]

    def _apply_mcp_progress(self, params: dict[str, Any]) -> list[Action]:
        item_id = params.get("itemId")
        message = params.get("message")
        if not isinstance(item_id, str) or not item_id or not isinstance(message, str):
            return []
        return [
            Action(
                ACT_MCP_ACTIVITY,
                {
                    "threadId": _string(params.get("threadId"), self.thread_id),
                    "turnId": _string(params.get("turnId"), self.turn_id),
                    "itemId": item_id,
                    "itemType": "mcpToolCall",
                    "category": "mcp",
                    "phase": "progress",
                    "message": message,
                },
            )
        ]

    def _capture_tool_result(self, item: dict[str, Any]) -> None:
        item_id = item.get("id") or ""
        if not item_id or item_id in self._completed_tool_ids:
            return
        status = item.get("status")
        item_type = item.get("type") or ""
        if status and status not in _TERMINAL_TOOL_STATUSES:
            return
        content = ""
        is_error = status in {"failed", "declined"}

        if item_type == "commandExecution":
            output = item.get("aggregatedOutput")
            if not isinstance(output, str):
                output = self._command_output.get(item_id, "")
            exit_code = item.get("exitCode")
            if isinstance(exit_code, int) and exit_code != 0:
                is_error = True
            content = output
            if not content and exit_code is not None:
                content = f"exit code {exit_code}"
        elif item_type == "fileChange":
            changes = item.get("changes") or []
            paths = [
                str(change.get("path"))
                for change in changes
                if isinstance(change, dict) and change.get("path")
            ]
            if paths:
                verb = "Failed to change" if is_error else "Changed"
                content = f"{verb} " + ", ".join(paths)
        elif item_type == "mcpToolCall":
            error = item.get("error")
            if error:
                is_error = True
                content = _display_value(error)
            else:
                content = _display_value(item.get("result"))
        elif item_type == "dynamicToolCall":
            success = item.get("success")
            if success is False:
                is_error = True
            content = _display_value(item.get("contentItems"))
        elif item_type == "imageGeneration":
            content = _string(item.get("result"))
            if status and status != "completed":
                is_error = True
        elif item_type == "collabAgentToolCall":
            content = _display_value(item.get("agentsStates"))

        if not content:
            return
        self._completed_tool_ids.add(item_id)
        self._tool_results.append(
            ToolResult(tool_use_id=item_id, content=content, is_error=is_error)
        )

    # -- native state notifications ------------------------------------

    def _apply_token_usage(self, params: dict[str, Any]) -> list[Action]:
        raw = params.get("tokenUsage")
        if not isinstance(raw, dict):
            return []
        token_usage = _normalize_token_usage(raw)
        self._last_token_usage = token_usage
        payload = {
            "threadId": _string(params.get("threadId"), self.thread_id),
            "turnId": _string(params.get("turnId"), self.turn_id),
            "tokenUsage": copy.deepcopy(token_usage),
            # The context meter is per current request. ``total`` is the
            # thread-lifetime accounting counter and would grow past the
            # model window after several turns.
            "usedTokens": token_usage["last"]["totalTokens"],
            "contextWindow": token_usage.get("modelContextWindow"),
        }
        return [Action(ACT_USAGE_UPDATED, payload)]

    def _apply_rate_limits(self, params: dict[str, Any]) -> list[Action]:
        snapshot = params.get("rateLimits")
        if not isinstance(snapshot, dict):
            return []
        self._rate_limits = _merge_non_null(self._rate_limits, snapshot)
        return [
            Action(
                ACT_RATE_LIMIT_UPDATED,
                {"rateLimits": copy.deepcopy(self._rate_limits)},
            )
        ]

    def _apply_turn_plan(self, params: dict[str, Any]) -> list[Action]:
        raw_plan = params.get("plan")
        if not isinstance(raw_plan, list):
            return []
        plan: list[dict[str, str]] = []
        for raw_step in raw_plan:
            if not isinstance(raw_step, dict):
                continue
            step = raw_step.get("step")
            status = raw_step.get("status")
            if isinstance(step, str) and isinstance(status, str):
                plan.append({"step": step, "status": status})
        payload = {
            "threadId": _string(params.get("threadId"), self.thread_id),
            "turnId": _string(params.get("turnId"), self.turn_id),
            "explanation": (
                params.get("explanation")
                if isinstance(params.get("explanation"), str)
                else None
            ),
            "plan": plan,
            "source": "turn",
            "authoritative": True,
        }
        return [Action(ACT_PLAN_UPDATED, payload)]

    def _apply_turn_diff(self, params: dict[str, Any]) -> list[Action]:
        diff = params.get("diff")
        if not isinstance(diff, str):
            return []
        return [
            Action(
                ACT_DIFF_UPDATED,
                {
                    "threadId": _string(params.get("threadId"), self.thread_id),
                    "turnId": _string(params.get("turnId"), self.turn_id),
                    "diff": diff,
                    "authoritative": True,
                },
            )
        ]

    def _apply_error(self, params: dict[str, Any]) -> list[Action]:
        error = params.get("error")
        if not isinstance(error, dict):
            return []
        payload = {
            "threadId": _string(params.get("threadId"), self.thread_id),
            "turnId": _string(params.get("turnId"), self.turn_id),
            "status": "retrying" if params.get("willRetry") else "failed",
            "willRetry": bool(params.get("willRetry")),
            "error": copy.deepcopy(error),
        }
        actions = [Action(ACT_TURN_STATUS, payload)]
        if not payload["willRetry"]:
            actions.append(Action(ACT_ERROR, str(error.get("message") or "Codex error.")))
        return actions

    # -- small state helpers -------------------------------------------

    def _belongs_to_thread(self, params: dict[str, Any]) -> bool:
        incoming = params.get("threadId")
        if not isinstance(incoming, str) or not incoming:
            return True
        if self.thread_id and incoming != self.thread_id:
            return False
        if not self.thread_id:
            self.thread_id = incoming
        return True

    def _ensure_streaming(self) -> StreamingAssistant:
        if self._streaming is None:
            self._streaming = StreamingAssistant(model=self.model)
        return self._streaming

    def _block_for(self, item_id: str) -> Block:
        streaming = self._ensure_streaming()
        index = self._block_by_item.get(item_id)
        if index is None or index >= len(streaming.blocks):
            # A new turn resets the StreamingAssistant (fresh, shorter
            # blocks) while _block_by_item may still hold an index into the
            # previous turn's list. Re-map to a fresh block instead of
            # indexing out of range — a stale/out-of-order Codex item used
            # to crash the whole app with IndexError here.
            streaming.blocks.append(Block(type="text"))
            index = len(streaming.blocks) - 1
            self._block_by_item[item_id] = index
        return streaming.blocks[index]

    def _demote_prior_agent_text(self, current_item_id: str) -> None:
        # Only the latest agent message stays the prominent final answer.
        # Superseded ones are still public agent output, so they demote to a
        # visible lightweight work update — never to private "thinking".
        for item_id in self._agent_message_ids:
            if item_id == current_item_id:
                continue
            index = self._block_by_item.get(item_id)
            if index is None or self._streaming is None:
                continue
            block = self._streaming.blocks[index]
            if block.type == "text":
                block.type = "commentary"

    def _agent_block_kind(self, item_id: str, phase: str | None) -> str:
        """Keep explicit commentary and superseded messages demoted on replay."""
        if phase == "commentary":
            return "commentary"
        if self._agent_message_ids and item_id != self._agent_message_ids[-1]:
            return "commentary"
        return "text"

    def _reconcile_authoritative_content(
        self,
        items: list[Any],
        *,
        tracked_before_completion: set[str] | None = None,
    ) -> None:
        """Repair content from completed ``turn.items`` without reversing it.

        Final item values are authoritative, but their list order is not always
        chronological: after context compaction App Server can replay a final
        answer before commentary that arrived earlier. First-seen order is the
        stronger chronology for every item already tracked before completion.
        Completion-only items are stably inserted beside their nearest preceding
        authoritative anchor (or before the nearest following anchor when there
        is no predecessor), without reordering any previously tracked content.
        Explicit ``final_answer`` phase semantics are stronger than a compacted
        payload's final-first ordering: repaired finals are placed last, and no
        repaired non-final content is inserted after a known explicit final.
        Tool slots remain untouched.
        """
        streaming = self._streaming
        if streaming is None:
            return
        # Defensive: if any tracked content block points past the current
        # stream (stale maps from a prior turn), skip this best-effort
        # reorder rather than IndexError-crash the event loop. The normal
        # path keeps the maps in sync (see _finish_turn); this only trips on
        # malformed or out-of-order events.
        if any(idx >= len(streaming.blocks) for idx in self._block_by_item.values()):
            return

        authoritative_ids: list[str] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            item_type = item.get("type")
            if (
                not isinstance(item_id, str)
                or not item_id
                or item_type not in _CONTENT_ITEM_TYPES
                or item_id not in self._block_by_item
                or item_id in seen
            ):
                continue
            seen.add(item_id)
            authoritative_ids.append(item_id)

        if not authoritative_ids:
            return

        existing_ids = [
            item_id
            for item_id, _index in sorted(
                self._block_by_item.items(), key=lambda entry: entry[1]
            )
            if self._item_types.get(item_id) in _CONTENT_ITEM_TYPES
        ]
        if tracked_before_completion is None:
            # Compatibility for direct callers that cannot distinguish live
            # items from completion repairs: the completed list is their only
            # available ordering signal.
            desired_ids = authoritative_ids + [
                item_id for item_id in existing_ids if item_id not in seen
            ]
        else:
            repaired_ids = [
                item_id
                for item_id in authoritative_ids
                if item_id not in tracked_before_completion
            ]
            if not repaired_ids:
                return

            repaired_set = set(repaired_ids)
            # Removing only completion repairs leaves the relative order of
            # every live content item immutable, even when the authoritative
            # replay itself is final-first after compaction.
            desired_ids = [
                item_id for item_id in existing_ids if item_id not in repaired_set
            ]
            authoritative_index = {
                item_id: index for index, item_id in enumerate(authoritative_ids)
            }

            def is_explicit_final(item_id: str) -> bool:
                return (
                    self._item_types.get(item_id) == "agentMessage"
                    and self._message_phases.get(item_id) == "final_answer"
                )

            # A compacted completion can be final-first even when that final
            # notification was the one event lost live. Explicit phase
            # semantics are stronger than the replay's raw position: defer
            # repaired finals until all non-final repairs are placed.
            deferred_finals = [
                item_id
                for item_id in repaired_ids
                if is_explicit_final(item_id)
            ]
            deferred_final_set = set(deferred_finals)
            for repaired_id in repaired_ids:
                if repaired_id in deferred_final_set:
                    continue
                source_index = authoritative_index[repaired_id]
                predecessor = next(
                    (
                        item_id
                        for item_id in reversed(authoritative_ids[:source_index])
                        if item_id in desired_ids
                        and not is_explicit_final(item_id)
                    ),
                    None,
                )
                if predecessor is not None:
                    insert_at = desired_ids.index(predecessor) + 1
                else:
                    successor = next(
                        (
                            item_id
                            for item_id in authoritative_ids[source_index + 1 :]
                            if item_id in desired_ids
                        ),
                        None,
                    )
                    if successor is not None:
                        insert_at = desired_ids.index(successor)
                    else:
                        insert_at = len(desired_ids)

                # An explicit final_answer is semantically terminal. A raw
                # final-first replay cannot anchor any repaired commentary,
                # reasoning, or plan content after it; cap the insertion at
                # the first known final without changing live-item order.
                first_final = next(
                    (
                        index
                        for index, item_id in enumerate(desired_ids)
                        if is_explicit_final(item_id)
                    ),
                    len(desired_ids),
                )
                insert_at = min(insert_at, first_final)
                desired_ids.insert(insert_at, repaired_id)
            desired_ids.extend(deferred_finals)

        content_slots = sorted(self._block_by_item[item_id] for item_id in existing_ids)
        blocks_by_id = {
            item_id: streaming.blocks[self._block_by_item[item_id]]
            for item_id in existing_ids
        }
        for index, item_id in zip(content_slots, desired_ids, strict=True):
            streaming.blocks[index] = blocks_by_id[item_id]
            self._block_by_item[item_id] = index

        # Rebuild final-answer precedence from the same stable merged order;
        # using raw authoritative order here would reintroduce the compaction
        # reversal even though the blocks themselves were kept chronological.
        agent_ids = set(self._agent_message_ids)
        self._agent_message_ids = [
            item_id for item_id in desired_ids if item_id in agent_ids
        ]
        for item_id in existing_ids:
            if self._item_types.get(item_id) != "agentMessage":
                continue
            index = self._block_by_item[item_id]
            streaming.blocks[index].type = self._agent_block_kind(
                item_id, self._message_phases.get(item_id)
            )

    def _mark_ttft(self, text: str) -> None:
        streaming = self._ensure_streaming()
        if not text or streaming.ttft_ms is not None or not self._turn_started_monotonic:
            return
        streaming.ttft_ms = int(
            (time.monotonic() - self._turn_started_monotonic) * 1000
        )

    def _delta_payload(
        self,
        params: dict[str, Any],
        item_id: str,
        delta: str,
        **extra: Any,
    ) -> dict[str, Any]:
        payload = {
            "threadId": _string(params.get("threadId"), self.thread_id),
            "turnId": _string(params.get("turnId"), self.turn_id),
            "itemId": item_id,
            "itemType": self._item_types.get(item_id, ""),
            "delta": delta,
        }
        payload.update(extra)
        return payload


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    """Return a caller-safe item copy.

    Reasoning items are model-private except for their public summary. Rather
    than copy-minus-``content`` (which would let raw ``text``, ``encrypted_
    content``/``encryptedContent`` camel variants, deltas, or other provider
    metadata leak into lifecycle actions), reasoning items are rebuilt from a
    NARROW allowlist: ``id``, ``type``, and the sanitized public ``summary``.
    Nothing else about a reasoning item ever crosses the boundary.
    """

    if item.get("type") == "reasoning":
        return {
            "id": _string(item.get("id")),
            "type": "reasoning",
            "summary": _public_reasoning_summary(item),
        }
    return copy.deepcopy(item)


def _public_reasoning_summary(item: dict[str, Any]) -> list[str]:
    """Return only a structurally valid public reasoning summary."""
    raw_summary = item.get("summary")
    if not isinstance(raw_summary, list):
        return []
    return [part for part in raw_summary if isinstance(part, str)]


def _tool_for_item(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    item_type = item.get("type") or ""
    if item_type == "commandExecution":
        return "Bash", {
            "command": _strip_shell_wrapper(_string(item.get("command"))),
            "cwd": _string(item.get("cwd")),
        }
    if item_type == "fileChange":
        changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        first = changes[0] if changes else {}
        name = "Write" if first.get("kind") == "add" else "Edit"
        tool_input: dict[str, Any] = {"file_path": _string(first.get("path"))}
        if len(changes) > 1:
            tool_input["additional_files"] = [
                _string(change.get("path")) for change in changes[1:]
            ]
        return name, tool_input
    if item_type == "mcpToolCall":
        server = _string(item.get("server"), "mcp")
        tool = _string(item.get("tool"), "tool")
        arguments = item.get("arguments")
        return f"mcp__{server}__{tool}", (
            copy.deepcopy(arguments) if isinstance(arguments, dict) else {"value": arguments}
        )
    if item_type == "dynamicToolCall":
        arguments = item.get("arguments")
        return _string(item.get("tool"), "tool"), (
            copy.deepcopy(arguments) if isinstance(arguments, dict) else {"value": arguments}
        )
    if item_type == "webSearch":
        tool_input: dict[str, Any] = {"query": _string(item.get("query"))}
        action = item.get("action")
        if isinstance(action, dict):
            tool_input["action"] = copy.deepcopy(action)
        return "WebSearch", tool_input
    if item_type in {"collabAgentToolCall", "subAgentActivity"}:
        prompt = _string(item.get("prompt"))
        description = prompt or _string(item.get("agentPath")) or _string(item.get("tool"))
        return "Agent", {
            "description": description,
            "subagent_type": _string(item.get("tool"), "subagent"),
            "thread_ids": copy.deepcopy(item.get("receiverThreadIds") or []),
        }
    if item_type == "imageView":
        return "Read", {"file_path": _string(item.get("path"))}
    if item_type == "imageGeneration":
        return "ImageGeneration", {}
    return item_type or "tool", {}


def _activity_payload(
    item: dict[str, Any],
    lifecycle: str,
    params: dict[str, Any],
    thread_id: str,
    turn_id: str,
) -> dict[str, Any]:
    item_type = _string(item.get("type"))
    category = {
        "commandExecution": "command",
        "fileChange": "file",
        "mcpToolCall": "mcp",
        "webSearch": "web",
        "dynamicToolCall": "tool",
        "collabAgentToolCall": "subagent",
        "subAgentActivity": "subagent",
        "imageView": "image",
        "imageGeneration": "image",
    }.get(item_type, "tool")
    status = item.get("status")
    payload: dict[str, Any] = {
        "threadId": _string(params.get("threadId"), thread_id),
        "turnId": _string(params.get("turnId"), turn_id),
        "itemId": _string(item.get("id")),
        "itemType": item_type,
        "category": category,
        "phase": lifecycle,
        "status": _string(status, lifecycle),
    }
    keys = {
        "commandExecution": ("command", "cwd", "commandActions", "exitCode", "durationMs"),
        "fileChange": ("changes",),
        "mcpToolCall": (
            "server",
            "tool",
            "arguments",
            "pluginId",
            "appContext",
            "durationMs",
            "error",
        ),
        "webSearch": ("query", "action"),
        "dynamicToolCall": ("tool", "namespace", "arguments", "success", "durationMs"),
        "collabAgentToolCall": (
            "tool",
            "senderThreadId",
            "receiverThreadIds",
            "prompt",
            "model",
            "reasoningEffort",
            "agentsStates",
        ),
        "subAgentActivity": ("agentPath", "agentThreadId", "kind"),
        "imageView": ("path",),
        "imageGeneration": ("savedPath", "revisedPrompt"),
    }.get(item_type, ())
    for key in keys:
        if key in item:
            payload[key] = copy.deepcopy(item[key])
    return payload


def _normalize_token_usage(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "total": _normalize_usage_breakdown(raw.get("total")),
        "last": _normalize_usage_breakdown(raw.get("last")),
        "modelContextWindow": _optional_nonnegative_int(raw.get("modelContextWindow")),
    }


def _normalize_usage_breakdown(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "inputTokens": _nonnegative_int(raw.get("inputTokens")),
        "cachedInputTokens": _nonnegative_int(raw.get("cachedInputTokens")),
        "outputTokens": _nonnegative_int(raw.get("outputTokens")),
        "reasoningOutputTokens": _nonnegative_int(raw.get("reasoningOutputTokens")),
        "totalTokens": _nonnegative_int(raw.get("totalTokens")),
    }


def _merge_non_null(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(current)
    for key, value in update.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_non_null(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            return message
        content = value.get("content")
        rendered = _display_value(content)
        if rendered:
            return rendered
        structured = value.get("structuredContent")
        if structured is not None:
            return _json_value(structured)
        return _json_value(value)
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            if isinstance(entry, dict):
                text = entry.get("text") or entry.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
            rendered = _display_value(entry)
            if rendered:
                parts.append(rendered)
        return "\n".join(parts)
    return str(value)


def _json_object(value: dict[str, Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _json_value(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _join_summary(parts: list[str]) -> str:
    return "\n\n".join(part for part in parts if part)


def _ensure_string_index(parts: list[str], index: int) -> None:
    while len(parts) <= index:
        parts.append("")


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _string(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default
