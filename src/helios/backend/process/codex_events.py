"""Translate `codex exec --json` JSONL events into claude-driver artifacts.

Pure logic, GTK-free, unit-tested against a captured real event stream
(codex-cli 0.139.0, 2026-06-10):

  {"type":"thread.started","thread_id":"019eb428-…"}
  {"type":"turn.started"}
  {"type":"item.started","item":{"id":"item_1","type":"command_execution",
      "command":"/bin/bash -c 'echo hi'","aggregated_output":"","status":"in_progress"}}
  {"type":"item.completed","item":{"id":"item_1","type":"command_execution",…}}
  {"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"…"}}
  {"type":"item.started","item":{"id":"item_3","type":"file_change",
      "changes":[{"path":"/tmp/x/probe.txt","kind":"add"}],"status":"in_progress"}}
  {"type":"turn.completed","usage":{"input_tokens":31186,"cached_input_tokens":20224,
      "output_tokens":212,"reasoning_output_tokens":66}}
  {"type":"turn.failed","error":{"message":"…"}}
  {"type":"error","message":"Reconnecting... 2/5 (…)"}        ← transient, suppressed

Items are mapped onto the same Block shapes the claude driver streams, so
the transcript view and activity indicator work unchanged: agent_message →
text block, reasoning → thinking block, command_execution → a Bash
tool_use, file_change → Write/Edit tool_uses, web_search → WebSearch, and
so on. Unlike claude there are no intra-item text deltas — blocks appear
when codex reports the item — but item.started still fires ahead of
execution, which is what keeps the activity strip live.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.transcript import ToolResult

# Action kinds yielded to the driver (mirror the GObject signal names).
ACT_SESSION_STARTED = "session-started"
ACT_STREAMING = "assistant-streaming"
ACT_TURN = "turn-appended"
ACT_RESULT = "result"
ACT_ERROR = "error"


@dataclass(slots=True)
class Action:
    kind: str
    payload: object = None


@dataclass(slots=True)
class CodexTurnAccumulator:
    """Feed parsed JSONL objects in; get UI-ready actions out."""

    model: str = ""
    thread_id: str = ""
    _streaming: StreamingAssistant | None = None
    _block_by_item: dict[str, int] = field(default_factory=dict)
    _tool_results: list[ToolResult] = field(default_factory=list)
    _completed_tool_ids: set[str] = field(default_factory=set)
    _agent_message_ids: list[str] = field(default_factory=list)
    _turn_started_monotonic: float = 0.0
    _announced: bool = False

    def feed_line(self, line: str) -> list[Action]:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []
        return self.feed(obj)

    def feed(self, obj: dict) -> list[Action]:
        etype = obj.get("type") or ""
        if etype == "thread.started":
            self.thread_id = obj.get("thread_id") or self.thread_id
            if not self._announced and self.thread_id:
                self._announced = True
                return [Action(ACT_SESSION_STARTED, self.thread_id)]
            return []

        if etype == "turn.started":
            self._turn_started_monotonic = time.monotonic()
            self._streaming = StreamingAssistant(model=self.model)
            self._block_by_item = {}
            self._tool_results = []
            self._completed_tool_ids = set()
            self._agent_message_ids = []
            return [Action(ACT_STREAMING, self._streaming)]

        if etype in ("item.started", "item.updated", "item.completed"):
            return self._apply_item(obj.get("item") or {})

        if etype == "turn.completed":
            return self._finish(obj.get("usage") or {})

        if etype == "turn.failed":
            msg = ((obj.get("error") or {}).get("message")) or "Codex turn failed."
            self._streaming = None
            return [Action(ACT_ERROR, msg)]

        if etype == "error":
            msg = obj.get("message") or ""
            # Mid-turn retries are noise; the terminal failure arrives as
            # turn.failed with the same text.
            if msg.startswith("Reconnecting"):
                return []
            return [Action(ACT_ERROR, msg)] if msg else []

        return []

    # ── item mapping ───────────────────────────────────────────────────

    def _apply_item(self, item: dict) -> list[Action]:
        if self._streaming is None:
            self._streaming = StreamingAssistant(model=self.model)
            self._block_by_item = {}
        block = self._block_for(item)
        if block is None:
            return []
        itype = item.get("type") or ""
        if itype == "agent_message":
            item_id = item.get("id") or ""
            self._mark_latest_agent_message(item_id)
            block.type = self._agent_block_kind(item_id)
            block.text = item.get("text") or ""
        elif itype == "reasoning":
            # `codex exec` collapses the App Server reasoning item's public
            # `summary` into ReasoningItem.text (documented "Agent's reasoning
            # summary"); raw reasoning content is never included. So this is the
            # public reasoning summary lane, not private thinking.
            block.type = "reasoning_summary"
            block.text = item.get("text") or ""
        else:
            name, tool_input = _tool_for_item(item)
            block.type = "tool_use"
            block.tool_use_name = name
            block.tool_use_id = item.get("id") or ""
            block.tool_use_input_json = json.dumps(tool_input)
            self._capture_tool_result(item)
        return [Action(ACT_STREAMING, self._streaming)]

    def _capture_tool_result(self, item: dict) -> None:
        """Preserve observable Codex tool output for the finalized turn."""
        if item.get("status") != "completed":
            return
        item_id = item.get("id") or ""
        if not item_id or item_id in self._completed_tool_ids:
            return
        itype = item.get("type") or ""
        content = ""
        is_error = False
        if itype == "command_execution":
            output = item.get("aggregated_output") or ""
            exit_code = item.get("exit_code")
            if exit_code not in (None, 0):
                is_error = True
            if output:
                content = output
            elif exit_code is not None:
                content = f"exit code {exit_code}"
        elif itype == "file_change":
            changes = item.get("changes") or []
            paths = [str(c.get("path") or "") for c in changes if c.get("path")]
            if paths:
                content = "Changed " + ", ".join(paths)
        if not content:
            return
        self._completed_tool_ids.add(item_id)
        self._tool_results.append(
            ToolResult(tool_use_id=item_id, content=content, is_error=is_error)
        )

    def _block_for(self, item: dict) -> Block | None:
        item_id = item.get("id") or ""
        if not item_id:
            return None
        idx = self._block_by_item.get(item_id)
        if idx is None:
            self._streaming.blocks.append(Block(type="text"))
            idx = len(self._streaming.blocks) - 1
            self._block_by_item[item_id] = idx
        return self._streaming.blocks[idx]

    # ── turn end ───────────────────────────────────────────────────────

    def _finish(self, usage: dict) -> list[Action]:
        actions: list[Action] = []
        if self._streaming is not None:
            turn = self._streaming.to_turn()
            turn.tool_results.extend(self._tool_results)
            if turn.has_content:
                actions.append(Action(ACT_TURN, turn))
        duration_ms = 0
        if self._turn_started_monotonic:
            duration_ms = int((time.monotonic() - self._turn_started_monotonic) * 1000)
        input_tokens = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        # Same shape the claude driver's `result` consumers read. Codex
        # reports input_tokens INCLUSIVE of cached tokens, so don't re-add
        # the cached figure into the cache_read field a claude-style summer
        # would double-count — split it out instead.
        result = {
            "type": "result",
            "subtype": "success",
            "provider": "openai",
            "total_cost_usd": 0.0,
            "duration_ms": duration_ms,
            "usage": {
                "input_tokens": max(0, input_tokens - cached),
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": 0,
                "output_tokens": output_tokens,
            },
            "modelUsage": {},
        }
        actions.append(Action(ACT_RESULT, result))
        self._streaming = None
        self._tool_results = []
        self._completed_tool_ids = set()
        self._agent_message_ids = []
        return actions

    def _mark_latest_agent_message(self, item_id: str) -> None:
        """Codex often emits progress narration as earlier agent_message
        items. Keep only the latest agent_message as the prominent final
        answer; demote prior ones to visible lightweight work updates
        (commentary) so GPT chats read as a work log, not one giant bubble
        and not hidden reasoning."""
        if not item_id:
            return
        if item_id in self._agent_message_ids:
            return
        self._agent_message_ids.append(item_id)
        for prior_id in self._agent_message_ids:
            if prior_id == item_id:
                continue
            idx = self._block_by_item.get(prior_id)
            if idx is None or self._streaming is None:
                continue
            prior = self._streaming.blocks[idx]
            if prior.type == "text":
                prior.type = "commentary"

    def _agent_block_kind(self, item_id: str) -> str:
        """Keep an older message demoted when exec replays it after a newer one."""
        if self._agent_message_ids and item_id != self._agent_message_ids[-1]:
            return "commentary"
        return "text"


def _tool_for_item(item: dict) -> tuple[str, dict]:
    """Map a codex item onto the claude tool vocabulary the activity
    indicator and transcript bubbles already understand."""
    itype = item.get("type") or ""
    if itype == "command_execution":
        return "Bash", {"command": _strip_shell_wrapper(item.get("command") or "")}
    if itype == "file_change":
        changes = item.get("changes") or []
        first = changes[0] if changes else {}
        name = "Write" if (first.get("kind") == "add") else "Edit"
        tool_input: dict = {"file_path": first.get("path") or ""}
        if len(changes) > 1:
            tool_input["additional_files"] = [c.get("path") or "" for c in changes[1:]]
        return name, tool_input
    if itype == "mcp_tool_call":
        server = item.get("server") or "mcp"
        tool = item.get("tool") or "tool"
        return f"mcp__{server}__{tool}", {}
    if itype == "web_search":
        return "WebSearch", {"query": item.get("query") or ""}
    if itype == "todo_list":
        return "TodoWrite", {"todos": item.get("items") or []}
    return itype or "tool", {}


def _strip_shell_wrapper(cmd: str) -> str:
    """codex wraps commands as `/bin/bash -c '…'` — show the payload."""
    for prefix in ("/bin/bash -c ", "/bin/sh -c ", "bash -c ", "sh -c "):
        if cmd.startswith(prefix):
            inner = cmd[len(prefix):].strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
                inner = inner[1:-1]
            return inner
    return cmd


# Sandbox flag mapping: Helios permission modes → codex exec argv. Headless
# codex can't raise interactive approval prompts, so every mode maps to a
# non-interactive sandbox level rather than an ask-y one.
SANDBOX_FLAGS: dict[str, list[str]] = {
    # Bypass requires the App Server lifecycle/budget controls and has no
    # legacy exec mapping. Unknown or unsupported strings stay read-only.
    "acceptEdits": ["-s", "workspace-write"],
    "auto": ["-s", "workspace-write"],
    "dontAsk": ["-s", "workspace-write"],
    "default": ["-s", "workspace-write"],
    "plan": ["-s", "read-only"],
}


def build_exec_argv(
    binary: str,
    *,
    model: str,
    permission_mode: str,
    resume_thread_id: str = "",
    effort: str = "",
) -> list[str]:
    """argv for one turn. The prompt itself travels via stdin (`-`) so huge
    pastes never hit argv limits or /proc exposure."""
    # Flag ordering matters: `exec`-level options (notably `-s/--sandbox`) must
    # precede the `resume` subcommand. codex-cli 0.139's `exec resume` parser
    # rejects `-s` when it appears AFTER `resume <id>` ("unexpected argument
    # '-s'"), which silently broke every session resume — the turn spawned,
    # exited on the arg-parse error, and no reply ever arrived. So we emit all
    # options first, then the `resume <id>` positional, then the `-` stdin
    # prompt marker. (Fresh turns are unaffected — same argv as before.)
    argv = [binary, "exec"]
    argv += ["--json", "--skip-git-repo-check"]
    argv += SANDBOX_FLAGS.get(permission_mode, ["-s", "read-only"])
    if model:
        argv += ["-m", model]
    if effort:
        # `-c` is an exec-level option and therefore must also precede the
        # resume subcommand. JSON string quoting is valid TOML and prevents a
        # malformed/custom effort name from changing the config expression.
        argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    if resume_thread_id:
        argv += ["resume", resume_thread_id]
    argv += ["-"]
    return argv
