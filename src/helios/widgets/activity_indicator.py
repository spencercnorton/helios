"""Activity indicator — the slim "Claude is …" strip above the composer.

Shows what the agent is actually doing, turn-by-turn:

  ● Thinking          12s · ~840 tokens · Esc to stop
  📖 Reading message_bubble.py
  ▸  Running pkill -f helios
  ✎  Editing main_window.py
  ● Responding…

Visual behavior (the "professional polish" contract):
  * The strip reveals ONCE per turn and stays until the result lands —
    including while the model is writing its visible answer (Responding).
    It used to hide during text blocks, which made it slide in and out
    repeatedly on think → write → tool → write turns. One reveal, one
    dismiss.
  * Crossfades between states use a fixed A/B pair of labels per stack —
    flip text on the hidden one, fade to it. (The previous one-label-per-
    unique-string cache grew without bound: every bash command minted a
    permanent Gtk.Label. Review M1.)
  * While thinking, the title cycles through a small set of verbs every few
    seconds and carries a soft breathing animation (CSS, reduced-motion
    aware).
  * A live metrics suffix — elapsed seconds, ~streamed tokens, and the
    Esc-to-stop hint — ticks once a second while the strip is revealed.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402
from helios.widgets._motion import BASE_MS


# Semantic activity states. Order doesn't matter — keyed by string.
STATE_IDLE = "idle"
STATE_THINKING = "thinking"
STATE_RESPONDING = "responding"
STATE_READING = "reading"
STATE_EDITING = "editing"
STATE_WRITING = "writing"
STATE_BASH = "bash"
STATE_WEB_SEARCH = "web_search"
STATE_WEB_FETCH = "web_fetch"
STATE_SEARCH_CODE = "search_code"
STATE_FIND_FILES = "find_files"
STATE_AGENT = "agent"
STATE_TODO = "todo"
STATE_GENERIC_TOOL = "generic_tool"
# Provider-truth phases the CLI reports directly. Helios cannot infer
# either one from the stream, because during both of them the stream is silent:
# a retrying request produces no deltas, and compaction happens between turns.
STATE_RETRYING = "retrying"
STATE_COMPACTING = "compacting"
STATE_REVIEWING = "reviewing"

# States rendered with the pulsing dot instead of a tool icon.
_DOT_STATES = {
    STATE_THINKING,
    STATE_RESPONDING,
    STATE_COMPACTING,
    STATE_REVIEWING,
}

#: States that mean "this is not normal forward progress" — the icon goes
#: amber so a retry storm does not look like ordinary work.
_WARN_STATES = {STATE_RETRYING}

# Icon name per tool state.
_STATE_ICONS = {
    STATE_READING: "text-x-generic-symbolic",
    STATE_EDITING: "document-edit-symbolic",
    STATE_WRITING: "document-new-symbolic",
    STATE_BASH: "utilities-terminal-symbolic",
    STATE_WEB_SEARCH: "system-search-symbolic",
    STATE_WEB_FETCH: "web-browser-symbolic",
    STATE_SEARCH_CODE: "edit-find-symbolic",
    STATE_FIND_FILES: "folder-symbolic",
    STATE_AGENT: "system-users-symbolic",
    STATE_TODO: "checkbox-checked-symbolic",
    STATE_GENERIC_TOOL: "emblem-system-symbolic",
    STATE_RETRYING: "view-refresh-symbolic",
}

# Title prefix per state. Detail text gets appended as a dim suffix.
_STATE_LABELS = {
    STATE_THINKING: "Thinking",
    STATE_RESPONDING: "Responding",
    STATE_RETRYING: "Retrying",
    STATE_COMPACTING: "Compacting context",
    STATE_REVIEWING: "Reviewing changes",
    STATE_READING: "Reading",
    STATE_EDITING: "Editing",
    STATE_WRITING: "Writing",
    STATE_BASH: "Running",
    STATE_WEB_SEARCH: "Searching the web",
    STATE_WEB_FETCH: "Fetching",
    STATE_SEARCH_CODE: "Searching files",
    STATE_FIND_FILES: "Finding files",
    STATE_AGENT: "Delegating to agent",
    STATE_TODO: "Updating tasks",
    STATE_GENERIC_TOOL: "Using",
}

# Rotated while the model thinks — varied enough to feel alive, restrained
# enough for a tool you stare at all day.
_THINKING_VERBS = ("Thinking", "Reasoning", "Considering", "Working it out")
_VERB_CYCLE_SECONDS = 4


class _CrossfadeLabel(Gtk.Stack):
    """Two fixed labels, flipped A/B with a crossfade on text change.

    Constant memory regardless of how many distinct strings pass through —
    the fix for the unbounded label-cache leak (review M1)."""

    def __init__(self, *css_classes: str, hexpand: bool = False) -> None:
        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.set_transition_duration(BASE_MS)
        self.set_hexpand(hexpand)
        self._labels: list[Gtk.Label] = []
        for name in ("a", "b"):
            lbl = Gtk.Label(label="", xalign=0)
            for c in css_classes:
                lbl.add_css_class(c)
            lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            lbl.set_hexpand(hexpand)
            self.add_named(lbl, name)
            self._labels.append(lbl)
        self._current = 0
        self._text = ""

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        nxt = 1 - self._current
        self._labels[nxt].set_label(text)
        self.set_visible_child(self._labels[nxt])
        self._current = nxt

    def set_text_immediate(self, text: str) -> None:
        """No crossfade — for the 1 Hz metrics tick, where a fade would
        smear the numbers."""
        self._text = text
        self._labels[self._current].set_label(text)

    def add_label_css_class(self, css: str) -> None:
        for lbl in self._labels:
            lbl.add_css_class(css)

    def remove_label_css_class(self, css: str) -> None:
        for lbl in self._labels:
            lbl.remove_css_class(css)


class ActivityIndicator(Gtk.Revealer):
    """Slim "Claude is doing X" strip. See module docstring."""

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_transition_duration(BASE_MS)
        self.set_reveal_child(False)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self.set_margin_top(2)
        self.set_margin_bottom(4)

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        outer.add_css_class("helios-activity")
        outer.set_hexpand(True)

        # ── Icon area — crossfades between a pulsing dot and tool icons ──
        self._icon_stack = Gtk.Stack()
        self._icon_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._icon_stack.set_transition_duration(BASE_MS)
        self._icon_stack.set_size_request(18, 18)

        # The pulsing dot — animated entirely via CSS keyframes on the class.
        self._pulse_dot = Gtk.Box()
        self._pulse_dot.add_css_class("helios-pulse-dot")
        self._pulse_dot.set_valign(Gtk.Align.CENTER)
        self._pulse_dot.set_halign(Gtk.Align.CENTER)
        self._icon_stack.add_named(self._pulse_dot, "dot")

        for state, icon_name in _STATE_ICONS.items():
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(15)
            img.add_css_class("helios-activity-icon")
            img.set_valign(Gtk.Align.CENTER)
            self._icon_stack.add_named(img, state)

        outer.append(self._icon_stack)

        # ── Title + detail — fixed A/B crossfade labels ──
        text_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.CENTER)

        self._title = _CrossfadeLabel("helios-activity-title")
        text_box.append(self._title)

        self._detail = _CrossfadeLabel(
            "helios-activity-detail", "dim-label", hexpand=True
        )
        text_box.append(self._detail)

        outer.append(text_box)

        # ── Metrics suffix: "12s · ~840 tokens · Esc to stop" ──
        self._metrics = _CrossfadeLabel("helios-activity-metrics", "dim-label")
        self._metrics.set_valign(Gtk.Align.CENTER)
        outer.append(self._metrics)

        self.set_child(outer)

        # Turn-scoped live state.
        self._state = STATE_IDLE
        self._turn_started: float = 0.0
        self._token_estimate: int = 0
        self._tick_id: int = 0
        self._verb_index: int = 0
        self._last_verb_flip: float = 0.0
        self._destroyed = False

    # ── Public API ─────────────────────────────────────────────────────

    def set_activity(self, state: str, detail: str = "") -> None:
        """Show the strip in the given state.

        Calling with STATE_IDLE ends the turn: hides the strip and resets
        the elapsed/token meters."""
        # A driver stopped by window close still emits a final activity event;
        # after shutdown this must not re-arm the 1s tick against a finalizing
        # widget.
        if self._destroyed:
            return
        if state == STATE_IDLE:
            self.clear()
            return

        first_reveal = not self.get_reveal_child()
        if first_reveal:
            self._turn_started = time.monotonic()
            self._token_estimate = 0
            self._verb_index = 0
            self._last_verb_flip = time.monotonic()
            self._metrics.set_text_immediate("")

        # Amber icon for the states that mean "stalled, not progressing".
        warn_icon = self._icon_stack.get_child_by_name(STATE_RETRYING)
        if warn_icon is not None:
            if state in _WARN_STATES:
                warn_icon.add_css_class("helios-activity-warn")
            else:
                warn_icon.remove_css_class("helios-activity-warn")

        if state in _DOT_STATES:
            self._icon_stack.set_visible_child_name("dot")
        elif self._icon_stack.get_child_by_name(state):
            self._icon_stack.set_visible_child_name(state)
        else:
            self._icon_stack.set_visible_child_name(STATE_GENERIC_TOOL)

        if state == STATE_THINKING:
            title = _THINKING_VERBS[self._verb_index % len(_THINKING_VERBS)]
        else:
            title = _STATE_LABELS.get(state, state.replace("_", " ").title())
        self._title.set_text(title)
        self._detail.set_text(_shorten(detail, 80))

        # Soft breathing on the title only while the dot states are showing.
        if state in _DOT_STATES:
            self._title.add_label_css_class("helios-activity-breathe")
        else:
            self._title.remove_label_css_class("helios-activity-breathe")

        self._state = state
        if first_reveal:
            self.set_reveal_child(True)
        self._ensure_tick()

    def set_token_estimate(self, tokens: int) -> None:
        """Streamed-so-far token estimate for the metrics suffix (the exact
        count only exists when the turn's result lands)."""
        if self._destroyed:
            return
        self._token_estimate = max(0, tokens)

    def clear(self) -> None:
        if self._destroyed:
            return
        self._state = STATE_IDLE
        self.set_reveal_child(False)
        self._stop_tick()
        self._turn_started = 0.0
        self._token_estimate = 0

    # ── Internals ──────────────────────────────────────────────────────

    def _ensure_tick(self) -> None:
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(1000, self._on_tick)

    def _stop_tick(self) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def shutdown(self) -> None:
        """Stop the 1s tick so it can't fire against a finalizing widget on
        window close, and refuse further ``set_activity`` calls. Idempotent."""
        self._destroyed = True
        self._stop_tick()

    def _on_tick(self) -> bool:
        # A tick already dispatched into the main loop when shutdown() ran must
        # drop itself WITHOUT inspecting or mutating GTK (no get_reveal_child /
        # label writes) against the finalizing widget.
        if self._destroyed:
            self._tick_id = 0
            return False
        if self._state == STATE_IDLE or not self.get_reveal_child():
            self._tick_id = 0
            return False
        now = time.monotonic()

        # Rotate the thinking verb without resetting detail/metrics.
        if (
            self._state == STATE_THINKING
            and now - self._last_verb_flip >= _VERB_CYCLE_SECONDS
        ):
            self._verb_index += 1
            self._last_verb_flip = now
            self._title.set_text(
                _THINKING_VERBS[self._verb_index % len(_THINKING_VERBS)]
            )

        parts = []
        if self._turn_started:
            parts.append(_format_elapsed(now - self._turn_started))
        if self._token_estimate > 0:
            parts.append(f"~{_format_tokens(self._token_estimate)} tokens")
        parts.append("Esc to stop")
        self._metrics.set_text_immediate(" · ".join(parts))
        return True


def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _format_tokens(n: int) -> str:
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _shorten(s: str, max_len: int) -> str:
    s = " ".join(s.split())  # collapse whitespace
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


# ── State derivation from a StreamingAssistant ────────────────────────────


def derive_state(blocks) -> tuple[str, str]:
    """Inspect a StreamingAssistant's blocks and return (state, detail).

    Used by MainWindow to translate driver streaming events into activity.
    """
    if not blocks:
        return STATE_THINKING, ""

    last = blocks[-1]
    if last.type in ("text", "commentary"):
        # The model is producing visible output — the final answer, or a public
        # commentary work update. Either way it is responding, not hidden
        # thinking; keep the strip up (it used to flicker away here).
        return STATE_RESPONDING, ""
    # thinking / reasoning_summary (and any other non-answer, non-tool block)
    # read as in-progress "thinking" on the strip.
    if last.type != "tool_use":
        return STATE_THINKING, ""

    name = last.tool_use_name or ""
    # Parse partial JSON best-effort — the input stream comes as deltas.
    inp = _safe_partial_input(last.tool_use_input_json)

    return _state_for_tool(name, inp)


def activity_summary(state: str, detail: str = "", *, width: int = 44) -> str:
    """One line of "what is it doing", for surfaces narrower than the strip.

    Same vocabulary as the strip so a session reads the same whether it is the
    visible one or a row in the sidebar. Thinking uses the fixed word rather
    than the strip's cycling verbs: a sidebar row is glanced at, not watched,
    and a label that changes wording on its own is noise there.
    """

    if not state or state == STATE_IDLE:
        return ""
    title = _STATE_LABELS.get(state, state.replace("_", " ").title())
    text = _shorten(detail, width)
    return f"{title} {text}" if text else title


def native_activity_state(payload: dict) -> tuple[str, str]:
    """Translate a Codex App Server lifecycle payload for the activity strip.

    Tool start/completion events also update the normal streaming blocks, but
    command-output and MCP progress notifications do not.  Keeping this small
    adapter here gives those native progress signals the same visual vocabulary
    as Claude tool calls without displaying raw command output.
    """

    if not isinstance(payload, dict):
        return STATE_THINKING, ""

    category = str(payload.get("category") or "tool")

    # ── Claude provider-truth phases ──────────────────────────
    if category == "retry":
        return STATE_RETRYING, _retry_detail(payload)

    if category == "phase":
        phase = str(payload.get("phase") or "")
        if phase == "compacting":
            return STATE_COMPACTING, ""
        if phase == "reviewing":
            return STATE_REVIEWING, ""
        if phase == "idle":
            return STATE_IDLE, ""
        # "requesting" means the HTTP request is in flight and nothing has
        # streamed back yet — the strip's own word for that is Thinking.
        # Any phase a future CLI adds is deliberately not rendered: a raw
        # enum name in the strip is worse than the state it replaces.
        return STATE_THINKING, ""

    if category == "thinking":
        tokens = payload.get("tokens")
        detail = f"~{tokens:,} thinking tokens" if isinstance(tokens, int) and tokens > 0 else ""
        return STATE_THINKING, detail

    if category == "agents":
        count = payload.get("count")
        count = count if isinstance(count, int) and count > 0 else 0
        noun = "subagent" if count == 1 else "subagents"
        return STATE_AGENT, f"{count} {noun} running" if count else ""

    item_type = str(payload.get("itemType") or "")
    message = str(payload.get("message") or "").strip()

    if category == "command":
        command = str(payload.get("command") or "").strip()
        return STATE_BASH, _shorten(command, 120) if command else ""

    if category == "file":
        changes = payload.get("changes")
        first = changes[0] if isinstance(changes, list) and changes else {}
        path = str(first.get("path") or "") if isinstance(first, dict) else ""
        kind = str(first.get("kind") or "") if isinstance(first, dict) else ""
        state = STATE_WRITING if kind == "add" else STATE_EDITING
        return state, _shorten_path(path)

    if category == "web":
        query = str(payload.get("query") or "").strip()
        return STATE_WEB_SEARCH, _shorten(query, 120) if query else ""

    if category == "subagent":
        detail = (
            message
            or str(payload.get("prompt") or "").strip()
            or str(payload.get("agentPath") or "").strip()
            or str(payload.get("tool") or "").strip()
        )
        return STATE_AGENT, _shorten(detail, 120) if detail else ""

    if category == "mcp":
        server = str(payload.get("server") or "").strip()
        tool = str(payload.get("tool") or "").strip()
        identity = " · ".join(part for part in (server, tool) if part)
        detail = message or identity
        return STATE_GENERIC_TOOL, _shorten(detail, 120) if detail else "MCP"

    if category == "image" and item_type == "imageView":
        path = str(payload.get("path") or "")
        return STATE_READING, _shorten_path(path)

    detail = (
        message
        or str(payload.get("tool") or "").strip()
        or item_type
    )
    return STATE_GENERIC_TOOL, _shorten(detail, 120) if detail else "tool"


def _retry_detail(payload: dict) -> str:
    """"attempt 2 of 5 · HTTP 529 · next try in 3s" from a system/api_retry.

    Field names and nullability are the installed CLI's own zod schema
    (claude 2.1.245): attempt/max_retries/retry_delay_ms are ints and
    error_status is `int | null` — null for connection errors and timeouts,
    which is exactly the case where a bare "Retrying" tells the user nothing.
    """

    parts: list[str] = []
    attempt = payload.get("attempt")
    total = payload.get("max_retries")
    if isinstance(attempt, int) and isinstance(total, int) and total > 0:
        parts.append(f"attempt {attempt} of {total}")
    elif isinstance(attempt, int):
        parts.append(f"attempt {attempt}")
    status = payload.get("error_status")
    if isinstance(status, int):
        parts.append(f"HTTP {status}")
    else:
        # error_status is null for connection-level failures. Saying so beats
        # an empty gap that reads like the retry had no cause.
        parts.append("connection error")
    delay = payload.get("retry_delay_ms")
    if isinstance(delay, int) and delay > 0:
        parts.append(f"next try in {max(1, round(delay / 1000))}s")
    return " · ".join(parts)


def estimate_streamed_tokens(blocks) -> int:
    """Rough live token count for the metrics suffix: chars/4 across every
    streamed block. The point is motion in the right magnitude, not
    accounting — the result record corrects it at turn end."""
    chars = 0
    for b in blocks:
        chars += len(b.text or "") + len(b.tool_use_input_json or "")
    return chars // 4


def _state_for_tool(name: str, inp: dict) -> tuple[str, str]:
    n = name.lower()
    file_path = inp.get("file_path", "") or ""
    short_path = _shorten_path(file_path)

    if n == "read":
        return STATE_READING, short_path
    if n == "edit":
        return STATE_EDITING, short_path
    if n == "write":
        return STATE_WRITING, short_path
    if n == "notebookedit":
        return STATE_EDITING, short_path
    if n == "bash":
        cmd = (inp.get("command") or "").strip()
        return STATE_BASH, cmd
    if n == "websearch":
        q = (inp.get("query") or "").strip()
        return STATE_WEB_SEARCH, q
    if n == "webfetch":
        u = (inp.get("url") or "").strip()
        return STATE_WEB_FETCH, _shorten_url(u)
    if n == "grep":
        return STATE_SEARCH_CODE, (inp.get("pattern") or "").strip()
    if n == "glob":
        return STATE_FIND_FILES, (inp.get("pattern") or "").strip()
    if n in ("task", "agent"):
        sub = inp.get("subagent_type") or inp.get("description") or ""
        return STATE_AGENT, sub
    if n == "workflow":
        # Previously unmapped, so a fan-out across many agents fell through to
        # the generic "Working" line and looked identical to running one grep
        # The Agent Dock carries per-actor detail; this line just has
        # to say a workflow is what is running.
        return STATE_AGENT, _shorten(str(inp.get("description") or "workflow"), 120)
    if n == "todowrite":
        todos = inp.get("todos") or []
        if isinstance(todos, list) and todos:
            return STATE_TODO, f"{len(todos)} task{'s' if len(todos) != 1 else ''}"
        return STATE_TODO, ""
    if n.startswith("mcp__"):
        # mcp__server__tool → "server · tool" reads better than the raw id.
        parts = name.split("__")
        if len(parts) >= 3:
            return STATE_GENERIC_TOOL, f"{parts[1]} · {parts[2]}"
        return STATE_GENERIC_TOOL, name
    # Fallback for any other tool name.
    return STATE_GENERIC_TOOL, name


def _safe_partial_input(raw: str) -> dict:
    """Tool-use input arrives as JSON deltas — try to parse, fall back to
    extracting common keys from the raw partial string."""
    import json
    import re

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Best-effort grab of the first string value for each known key.
    out: dict = {}
    for key in ("file_path", "command", "query", "url", "pattern", "subagent_type", "description"):
        m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            try:
                out[key] = bytes(m.group(1), "utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                out[key] = m.group(1)
    return out


def _shorten_path(path: str) -> str:
    if not path:
        return ""
    # Show the last two segments — recognizable but compact.
    parts = path.replace("\\", "/").split("/")
    parts = [p for p in parts if p]
    if len(parts) <= 2:
        return path
    return ".../" + "/".join(parts[-2:])


def _shorten_url(url: str) -> str:
    if not url:
        return ""
    # Strip scheme + trim long paths.
    s = url
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if len(s) > 60:
        s = s[:57] + "..."
    return s
