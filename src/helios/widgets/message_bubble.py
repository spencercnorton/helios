from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from helios.backend.process.streaming import stable_markdown_prefix
from helios.backend.tool_diff import EDIT_TOOLS, edit_diff, edit_path
from helios.backend.transcript import (
    QUESTION_ANSWERED_MESSAGE,
    QUESTION_DISMISSED_MESSAGE,
    ToolResult,
    ToolUse,
    Turn,
    activity_items,
)

MAX_TOOL_PREVIEW = 600  # characters
MAX_TOOL_INPUT = 400

# Activity-group incremental build: first batch is built synchronously on
# expand; remaining items arrive via GLib.idle_add to keep the main thread
# responsive for turns with hundreds of tool actions.
_ACTIVITY_FIRST_BATCH = 20
_ACTIVITY_BATCH = 20
_WORK_UPDATES_FIRST_BATCH = 5
_WORK_UPDATES_BATCH = 5

# Benign AskUserQuestion outcomes that arrive as `is_error` tool_results but
# should render as neutral info rows rather than tool errors. Maps the sentinel
# content (from cli_driver.respond_to_question) to a friendly row label.
_QUESTION_RESULT_LABELS = {
    QUESTION_ANSWERED_MESSAGE: "↳ answered",
    QUESTION_DISMISSED_MESSAGE: "↳ dismissed",
}


def _message_copy_text(turn: Turn) -> str:
    """Text the bubble's Copy button puts on the clipboard: the final answer,
    or — when a message is only commentary/reasoning/tool activity — every
    visible span joined, so the button never copies nothing."""
    return turn.text or "\n\n".join(s.text for s in turn.content if s.text.strip())


def _copy_to_clipboard(text: str) -> None:
    display = Gdk.Display.get_default()
    if display is not None and text:
        display.get_clipboard().set(text)


def _bubble_header(
    label_text: str, role: str, ts_text: str, copy_text, *, inert: bool = False
) -> Gtk.Box:
    """The one header shape the live and the resting bubble both use.

    The copy button is ALWAYS allocated. Measured: a `.flat.circular` button
    floors the row at 36px while a bare caption row measures 14px (15px without
    `Adw.init()`, which is what the gtk_tests lane gets — `conftest.py` does not
    call it). The floor comes from the stock GTK Adwaita theme, not from
    libadwaita: it is present either way. So a header that only grew the button
    once the turn finalized shoved everything below it down 22px inside the
    sticky-bottom scroller. When there is nothing to copy (``inert``) the button stays
    present but invisible and untargetable, so the row height never changes.
    ``copy_text`` is a callable because the live bubble's text is still
    growing when its header is built.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    box.set_margin_start(10)
    box.set_margin_top(2)

    label = Gtk.Label(label=label_text, xalign=0)
    label.add_css_class("caption-heading")
    label.add_css_class(f"helios-role-{role}")
    box.append(label)

    if ts_text:
        ts = Gtk.Label(label=ts_text, xalign=0)
        ts.add_css_class("caption")
        ts.add_css_class("dim-label")
        box.append(ts)

    spacer = Gtk.Box()
    spacer.set_hexpand(True)
    box.append(spacer)

    # A whole message spans several sibling widgets (one per markdown/code
    # block), so drag-selection can't cross them — offer an explicit
    # copy-the-whole-message button instead, mirroring CodeBlock's.
    btn = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
    btn.add_css_class("flat")
    btn.add_css_class("circular")
    btn.set_valign(Gtk.Align.CENTER)
    if inert:
        btn.set_opacity(0.0)
        btn.set_can_target(False)
        btn.set_can_focus(False)
        # Opacity and can_target hide it from the eye and the pointer, not from
        # assistive tech. Without this a screen reader announces an unnamed
        # button on every tool-only assistant turn — which, after this change,
        # is every tool turn in the transcript.
        btn.update_state([Gtk.AccessibleState.HIDDEN], [True])
    else:
        btn.set_tooltip_text("Copy message")
        btn.connect("clicked", lambda _b: _copy_to_clipboard(copy_text()))
    box.append(btn)
    return box


class MessageBubble(Gtk.Box):
    def __init__(
        self,
        turn: Turn,
        *,
        animate_in: bool = False,
        assistant_label: str = "Claude",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._destroyed = False
        self._assistant_label = assistant_label
        self._turn = turn
        # Results delivered in a LATER transcript record (attach_tool_result).
        # Deliberately NOT written back into `turn`: plan_pane and
        # context_breakdown hold the same Turn object and count tool_results,
        # so mutating it would double-count the same output.
        self._attached: list[ToolResult] = []
        self._elapsed: dict[str, str] = {}  # tool_use_id -> "1.4s"
        self._activity_expander: Gtk.Expander | None = None
        self.add_css_class("helios-bubble")
        self.add_css_class(f"helios-bubble-{turn.role}")
        # One-shot fade-in for freshly-arrived live messages. Opacity-only, so
        # it never shifts layout or fights the transcript's sticky-bottom
        # scroll. Bulk-loaded history and the constantly-rebuilt streaming
        # bubble pass animate_in=False (no flicker / no 190-bubble fade storm).
        if animate_in:
            self.add_css_class("helios-bubble-enter")
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        # A tool-result-only turn has no speaker. Rendering it headerless leaves
        # just its _activity_group expander, which is what it actually is.
        if turn.role != "tool":
            self.append(self._build_header(turn))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        body.set_margin_start(10)
        body.set_margin_end(10)
        body.set_margin_top(10)
        body.set_margin_bottom(10)
        body.add_css_class("helios-bubble-body")
        body.add_css_class(f"helios-bubble-body-{turn.role}")
        self.append(body)
        self._body = body
        self._footer: Gtk.Label | None = None  # set_footer's caption, if any

        # Render assistant content in its original source order (matching the
        # live stream), so an interleaved sequence like commentary → reasoning
        # summary → commentary → final answer keeps that sequence — each span
        # is its own labeled surface, never regrouped or merged across lanes.
        # Tool results are detail rows belonging to their invocation, not
        # additional user-visible "actions".  Keep every finalized tool trace
        # behind one compact, lazy expander -- even a single invocation often
        # has a result row and should not consume two transcript rows.
        activity = None
        if turn.tool_uses or turn.tool_results:
            activity = _activity_group(turn, self._resting_activity_items)
            activity._tool_elapsed = self._elapsed  # type: ignore[attr-defined]
            self._activity_expander = activity
        for widget in _content_widgets(
            turn.content,
            activity=activity,
            activity_content_index=turn.activity_content_index,
        ):
            body.append(widget)
        self._files_label: Gtk.Widget | None = None
        self._files_slot = body
        self._sync_files_changed()

    def _sync_files_changed(self) -> None:
        """Rebuild the files-changed line from edits that actually landed.

        Built from tool_uses alone it appeared while a call was unresolved and
        survived a denied or errored edit, telling the user a file changed
        when none did.
        """
        slot = getattr(self, "_files_slot", None)
        if slot is None:
            return
        if self._files_label is not None:
            slot.remove(self._files_label)
            self._files_label = None
        succeeded = {
            res.tool_use_id
            for res in self._all_tool_results()
            if res.tool_use_id and not res.is_error
        }
        edits = [tu for tu in self._turn.tool_uses if tu.id in succeeded]
        label = _files_changed_label(edits)
        if label is not None:
            self._files_label = label
            slot.append(label)
            # Stay under the footer when one is already showing.
            footer = getattr(self, "_footer", None)
            if footer is not None:
                slot.reorder_child_after(footer, label)

    def set_footer(self, text: str) -> None:
        """Show (or, on empty text, clear) a per-turn caption under the body.

        Replaces rather than stacks: a turn is annotated once, and a second
        call for the same bubble is an update, not a second line.
        """
        if self._footer is not None:
            self._body.remove(self._footer)
            self._footer = None
        if not text:
            return
        self._footer = Gtk.Label(label=text, xalign=1)
        self._footer.add_css_class("caption")
        self._footer.add_css_class("dim-label")
        # The cost half is Claude's own reported figure, which work_store.py
        # records as an API-equivalent estimate — on a subscription nothing
        # here was charged, and the footer must not read as a bill.
        self._footer.set_tooltip_text(
            "Reported turn usage. On a subscription the cost is an "
            "API-equivalent estimate, not an amount charged."
        )
        self._body.append(self._footer)

    def attach_tool_result(self, tr: ToolResult, timestamp: str = "") -> bool:
        """Land a tool_result on the card that made the call.

        The CLI delivers a tool_result in its own user-role record, one or more
        records after the assistant message carrying the tool_use — so the pair
        spans two bubbles and only the earlier one can resolve it. Returns True
        when this bubble owned a still-unresolved call with that id, which is
        how the transcript knows whether the result still needs a standalone
        row of its own.
        """
        if self._destroyed or not tr.tool_use_id:
            return False
        if not any(tu.id == tr.tool_use_id for tu in self._turn.tool_uses):
            return False
        if any(
            existing.tool_use_id == tr.tool_use_id
            for existing in (*self._turn.tool_results, *self._attached)
        ):
            return False  # already resolved — a second result is not ours
        self._attached.append(tr)
        elapsed = _elapsed_text(self._turn.timestamp, timestamp)
        if elapsed:
            self._elapsed[tr.tool_use_id] = elapsed
        if self._activity_expander is not None:
            self._activity_expander.set_label(self._activity_label())
            # Rebuilds only if the group is open; a collapsed one picks the new
            # result up from _resting_activity_items on its next expand.
            # ponytail: an OPEN group rebuilds every card per arriving result —
            # O(n²) on a turn whose group the user left expanded, bounded by the
            # existing 20-per-idle batching. Swap in the one card by index if a
            # long tool run is ever measured to stutter.
            _invalidate_lazy_group(self._activity_expander)
        self._sync_files_changed()
        return True

    def _all_tool_results(self) -> list[ToolResult]:
        return [*self._turn.tool_results, *self._attached]

    def _resting_activity_items(self) -> list[tuple[str, object, str]]:
        """This bubble's pairing: the turn's own results plus any attached
        later. A throwaway Turn keeps `activity_items` the single pairing."""
        if not self._attached:
            return activity_items(self._turn)
        return activity_items(
            Turn(
                role=self._turn.role,
                tool_uses=self._turn.tool_uses,
                tool_results=self._all_tool_results(),
            )
        )

    def _activity_label(self) -> str:
        return _tool_activity_label(
            self._turn.tool_uses, errors=_error_count(self._all_tool_results())
        )

    def shutdown(self) -> None:
        """Cancel any incremental activity-group build owned by this bubble.

        The idle closure retains its expander/box even after the transcript is
        detached, so parent teardown must explicitly invalidate its token.
        Idempotent."""
        if self._destroyed:
            return
        self._destroyed = True
        _cancel_activity_builds(self)

    def _build_header(self, turn: Turn) -> Gtk.Widget:
        dt = turn.dt
        return _bubble_header(
            {
                "user": "You",
                "assistant": self._assistant_label,
                "system": "System",
            }.get(turn.role, turn.role.title()),
            turn.role,
            dt.strftime("%b %-d, %H:%M") if dt is not None else "",
            lambda: _message_copy_text(self._turn),
            # A tool-only turn has nothing to copy, but the button still has
            # to hold the row open at the same height as a prose turn's.
            inert=not _message_copy_text(turn),
        )


def _text_view(text: str, *, selectable: bool = True) -> Gtk.Widget:
    """Phase 2: full markdown rendering via helios.widgets.markdown."""
    from helios.widgets.markdown import render_markdown

    return render_markdown(text)


# Collapsed content lanes: block type -> (expander label, css class). Both are
# public and never carry raw hidden reasoning.
_COLLAPSIBLE_LANES = {
    "thinking": ("Thinking", "helios-thinking"),
    "reasoning_summary": ("Reasoning summary", "helios-reasoning"),
}


def _work_update(text: str) -> Gtk.Widget:
    """Render one commentary/progress item inside a work-update surface."""
    from helios.widgets.markdown import render_markdown

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("helios-commentary")
    caption = Gtk.Label(label="Work update", xalign=0)
    caption.add_css_class("caption-heading")
    caption.add_css_class("dim-label")
    box.append(caption)
    box.append(render_markdown(text))
    return box


def _work_updates_group(updates: list[str]) -> Gtk.Widget:
    """A collapsed, lazy group for finalized public progress updates."""
    expander = Gtk.Expander()
    expander.set_label(f"Work updates · {len(updates)}")
    expander.add_css_class("helios-commentary")
    _init_lazy_group(expander)
    expander.set_expanded(False)
    expander.connect("notify::expanded", _on_work_updates_expanded, updates)
    return expander


def _span_widget(kind: str, text: str) -> Gtk.Widget | None:
    """Build the resting widget for one ordered content span (or None if empty)."""
    if not text.strip():
        return None
    lane = _COLLAPSIBLE_LANES.get(kind)
    if lane is not None:
        label, css = lane
        return _collapsible(label, text, css=css)
    if kind == "commentary":
        return _work_update(text)
    return _text_view(text, selectable=True)  # text / unknown -> final answer


def _content_widgets(
    spans,
    *,
    activity: Gtk.Widget | None = None,
    activity_content_index: int | None = None,
) -> list[Gtk.Widget]:
    """Map ordered spans to widgets, grouping finalized commentary once.

    The group occupies the first commentary span's position and its children
    retain commentary order.  All other surfaces retain their source order.
    That is the closest lossless projection possible while collapsing several
    non-contiguous updates into one transcript row.  The likewise-grouped tool
    activity occupies the boundary where its first invocation appeared.  Old
    turns without that boundary retain the legacy activity-at-end fallback.
    """
    updates = [
        span.text for span in spans if span.kind == "commentary" and span.text.strip()
    ]
    widgets: list[Gtk.Widget] = []
    updates_inserted = False
    activity_at = len(spans)
    if activity is not None and activity_content_index is not None:
        activity_at = min(max(activity_content_index, 0), len(spans))
    activity_inserted = False
    for index, span in enumerate(spans):
        if activity is not None and index == activity_at:
            widgets.append(activity)
            activity_inserted = True
        if span.kind == "commentary":
            if updates and not updates_inserted:
                widgets.append(_work_updates_group(updates))
                updates_inserted = True
            continue
        widget = _span_widget(span.kind, span.text)
        if widget is not None:
            widgets.append(widget)
    if activity is not None and not activity_inserted:
        widgets.append(activity)
    return widgets


class _Segment:
    """Per-content-block render state inside a StreamingBubble."""

    __slots__ = ("kind", "widget", "label", "final", "clen", "stable_len")

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.widget: Gtk.Widget | None = None  # created lazily on first content
        self.label: Gtk.Label | None = None  # live inner label (commentary box)
        self.final = False
        self.clen = -1  # rendered content length (change detector)
        self.stable_len = 0  # text already rendered as markdown (see T-1)


class StreamingBubble(Gtk.Box):
    """The in-flight assistant message, updated IN PLACE as deltas land.

    MessageBubble is immutable — the old streaming path rebuilt one from
    scratch on every coalesced flush (~16×/s): full markdown parse, fresh
    GtkSourceView buffers, remove+append in the transcript box. That widget
    churn was the visible flicker/jumpiness while a reply streamed in, and
    it reset expander state (an opened Thinking section snapped shut).

    This widget keeps one child per content block and mutates only the last
    (in-progress) one. Blocks stream strictly in order, so when block i+1
    starts, block i is complete — it gets its one-time final render
    (markdown for text, the standard expander for tool calls) and is never
    touched again. The in-progress text block is a box of already-rendered
    Markdown chunks — every prefix `stable_markdown_prefix` has declared
    settled, each parsed exactly once — above a plain wrapped Label carrying
    the unsettled tail. On message_stop the canonical Turn replaces this whole
    widget with a regular MessageBubble; because both sides are rendered
    Markdown by then, that swap is visually inert.
    """

    def __init__(self, *, assistant_label: str = "Claude") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._destroyed = False
        self.add_css_class("helios-bubble")
        self.add_css_class("helios-bubble-assistant")
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        # Allocated but inert. Allocating it is what makes the header the same
        # height as the finalized bubble's — the entire point of this change.
        # Making it INERT is because a streaming bubble is transient: it is
        # replaced by a real MessageBubble, which has a working copy button, the
        # moment the turn completes. A live copy button here would have to
        # handle `_source is None` on the first frame of every turn, or show a
        # visible control that silently does nothing — precisely what
        # `_message_copy_text`'s "so the button never copies nothing" invariant
        # exists to prevent. Height parity does not need it, so it does not
        # exist.
        self.append(
            _bubble_header(assistant_label, "assistant", "", lambda: "", inert=True)
        )

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._body.set_margin_start(10)
        self._body.set_margin_end(10)
        self._body.set_margin_top(10)
        self._body.set_margin_bottom(10)
        self._body.add_css_class("helios-bubble-body")
        self._body.add_css_class("helios-bubble-body-assistant")
        self.append(self._body)

        self._segments: list[_Segment] = []
        self._source = None  # the StreamingAssistant we're rendering
        self._activity_expander: Gtk.Expander | None = None
        self._activity_fingerprint: tuple[tuple[str, str, str], ...] = ()

    # ── public ──

    def update(self, streaming) -> None:
        """Sync to the latest StreamingAssistant snapshot."""
        if self._destroyed:
            return
        # A new message within the same turn means a NEW StreamingAssistant
        # (e.g. the previous one ended empty, so no canonical Turn replaced
        # this bubble). Index-aligned segments from the old message would be
        # garbage — restart clean.
        if self._source is not streaming:
            if self._source is not None:
                for seg in self._segments:
                    self._drop_widget(seg)
                self._segments.clear()
                self._activity_expander = None
                self._activity_fingerprint = ()
            self._source = streaming
        blocks = streaming.blocks
        last = len(blocks) - 1
        for i, b in enumerate(blocks):
            if i >= len(self._segments):
                self._segments.append(_Segment(b.type))
            seg = self._segments[i]
            content = b.tool_use_input_json if b.type == "tool_use" else b.text
            if seg.final and seg.kind == b.type and len(content) == seg.clen:
                continue  # finalized + unchanged: the common case
            if seg.kind != b.type:
                # The aggregator's placeholder ("text") got replaced by the
                # real block type at content_block_start — restart the segment.
                self._drop_widget(seg)
                seg.kind = b.type
                seg.final = False
            if i < last:
                self._finalize_segment(seg, i, b, content)
            else:
                self._stream_segment(seg, i, b, content)

    def shutdown(self) -> None:
        """Cancel lazy detail builders before this live bubble is detached."""
        if self._destroyed:
            return
        self._destroyed = True
        _cancel_activity_builds(self)


    # ── per-segment rendering ──

    def _stream_segment(self, seg: _Segment, i: int, b, content: str) -> None:
        seg.clen = len(content)
        if b.type == "tool_use":
            self._sync_live_activity()
            return
        if not content:
            return
        if b.type in _COLLAPSIBLE_LANES:
            self._stream_collapsible(seg, i, b.type, content)
            return
        if b.type == "commentary":
            # Give active commentary its stable "Work update" identity right
            # away (a captioned box with a live label), not a bare label that
            # only becomes a work update once a later block arrives.
            if seg.widget is None:
                seg.widget, seg.label = self._streaming_work_update()
                self._insert_at(i, seg.widget)
            if seg.label is not None:
                seg.label.set_text(content)
            return
        # text — the settled markdown above a plain label holding the live
        # tail, so the swap to the resting MessageBubble is visually inert.
        if seg.widget is None:
            seg.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            seg.label = self._stream_text_label()
            seg.widget.append(seg.label)
            self._insert_at(i, seg.widget)
        # ponytail: a block whose closing blank line has not arrived yet stays
        # raw in the tail label — a long final paragraph renders plain until
        # message_stop. Conservative on purpose: splitting mid-block would
        # render differently from the resting bubble. Upgrade path is a real
        # incremental parser, not a looser boundary.
        n = stable_markdown_prefix(content)
        if n > seg.stable_len and seg.label is not None:
            chunk = content[seg.stable_len : n]
            seg.stable_len = n
            # A settled run of pure whitespace parses to nothing; rendering it
            # would leave an empty box holding the parent's spacing open, so
            # only the prefix advances. `_finalize_segment` then still sees an
            # empty widget and drops the segment, exactly as it always did.
            if chunk.strip():
                # Lands after the last rendered chunk (position 0 when there is
                # none, since prev is then None) — always before the tail label.
                seg.widget.insert_child_after(
                    _text_view(chunk), seg.label.get_prev_sibling()
                )
        if seg.label is not None:
            tail = content[seg.stable_len :]
            seg.label.set_text(tail)
            # An empty label still claims a line's height; hiding it keeps the
            # settled text from growing a blank gap under it between flushes.
            seg.label.set_visible(bool(tail))

    def _finalize_segment(self, seg: _Segment, i: int, b, content: str) -> None:
        seg.final = True
        seg.clen = len(content)
        if b.type in _COLLAPSIBLE_LANES:
            # The streaming expander IS the final rendering — just sync text.
            self._stream_collapsible(seg, i, b.type, content)
            return
        if b.type == "commentary":
            self._finalize_commentary(seg, i, content)
            return
        if b.type == "tool_use":
            self._sync_live_activity()
            return
        else:  # text
            if seg.widget is not None and seg.label is not None:
                # Streamed: every settled chunk is already rendered, so only
                # the tail is left to parse. Never rebuild what is on screen.
                tail = content[seg.stable_len :]
                seg.widget.remove(seg.label)
                seg.label = None
                if tail.strip():
                    seg.widget.append(_text_view(tail))
                if seg.widget.get_first_child() is None:
                    self._drop_widget(seg)
                return
            new = _text_view(content) if content.strip() else None
        if new is None:
            self._drop_widget(seg)
            return
        if seg.widget is not None:
            self._body.insert_child_after(new, seg.widget)
            self._body.remove(seg.widget)
        else:
            self._insert_at(i, new)
        seg.widget = new

    def _finalize_commentary(self, seg: _Segment, i: int, content: str) -> None:
        # Keep the streaming Work-update box + caption (stable identity, no
        # duplicate widget); only swap the live label for the final markdown.
        if not content.strip():
            self._drop_widget(seg)
            return
        from helios.widgets.markdown import render_markdown

        body = render_markdown(content)
        if seg.widget is not None and seg.label is not None:
            seg.widget.remove(seg.label)
            seg.widget.append(body)
            seg.label = None
            return
        new = _work_update(content)
        if seg.widget is not None:
            self._body.insert_child_after(new, seg.widget)
            self._body.remove(seg.widget)
        else:
            self._insert_at(i, new)
        seg.widget = new

    def _stream_collapsible(
        self, seg: _Segment, i: int, btype: str, content: str
    ) -> None:
        if not content:
            return
        label, css = _COLLAPSIBLE_LANES[btype]
        if seg.widget is None:
            seg.widget = _collapsible(label, content, css=css)
            self._insert_at(i, seg.widget)
        else:
            child = seg.widget.get_child()
            if isinstance(child, Gtk.TextView):
                child.get_buffer().set_text(content)

    # ── widget helpers ──

    @staticmethod
    def _stream_text_label() -> Gtk.Label:
        lbl = Gtk.Label(xalign=0)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR — matches markdown.py
        lbl.set_selectable(False)  # selection would be lost per flush anyway
        return lbl

    @staticmethod
    def _streaming_work_update() -> tuple[Gtk.Widget, Gtk.Label]:
        """A live Work-update box (caption + updatable label) for commentary."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("helios-commentary")
        caption = Gtk.Label(label="Work update", xalign=0)
        caption.add_css_class("caption-heading")
        caption.add_css_class("dim-label")
        box.append(caption)
        label = Gtk.Label(xalign=0)
        label.set_wrap(True)
        label.set_wrap_mode(2)
        label.set_selectable(False)
        box.append(label)
        return box, label

    @staticmethod
    def _parsed_tool_use(b) -> ToolUse:
        import json

        try:
            inp = json.loads(b.tool_use_input_json) if b.tool_use_input_json else {}
        except json.JSONDecodeError:
            inp = {"_raw": b.tool_use_input_json}
        return ToolUse(name=b.tool_use_name, input=inp, id=b.tool_use_id)

    def _live_tool_uses(self) -> list[ToolUse]:
        if self._source is None:
            return []
        return [
            self._parsed_tool_use(block)
            for block in self._source.blocks
            if block.type == "tool_use" and block.tool_use_name
        ]

    def _live_activity_items(self) -> list[tuple[str, object, str]]:
        return [("tool_use", tool, tool.name) for tool in self._live_tool_uses()]

    def _sync_live_activity(self) -> None:
        """Represent every streamed tool block with one persistent expander."""
        if self._source is None:
            return
        blocks = [
            block
            for block in self._source.blocks
            if block.type == "tool_use" and block.tool_use_name
        ]
        if not blocks:
            return

        fingerprint = tuple(
            (block.tool_use_id, block.tool_use_name, block.tool_use_input_json)
            for block in blocks
        )
        tools = [self._parsed_tool_use(block) for block in blocks]

        if self._activity_expander is None:
            first_index = next(
                i
                for i, block in enumerate(self._source.blocks)
                if block.type == "tool_use" and block.tool_use_name
            )
            expander = _new_activity_expander(
                _tool_activity_label(tools, live=True), self._live_activity_items
            )
            self._activity_expander = expander
            self._activity_fingerprint = fingerprint
            owner = self._segments[first_index]
            owner.widget = expander
            self._insert_at(first_index, expander)
            return

        self._activity_expander.set_label(_tool_activity_label(tools, live=True))
        if fingerprint != self._activity_fingerprint:
            self._activity_fingerprint = fingerprint
            _invalidate_lazy_group(self._activity_expander)

    def _insert_at(self, seg_index: int, widget: Gtk.Widget) -> None:
        """Insert keeping body children aligned with segment order (segments
        without content have no widget and are skipped)."""
        prev = None
        for s in self._segments[:seg_index]:
            if s.widget is not None:
                prev = s.widget
        self._body.insert_child_after(widget, prev)

    def _drop_widget(self, seg: _Segment) -> None:
        if seg.widget is not None:
            if seg.widget is self._activity_expander:
                _cancel_activity_builds(seg.widget)
                self._activity_expander = None
                self._activity_fingerprint = ()
            self._body.remove(seg.widget)
            seg.widget = None
        seg.label = None
        seg.stable_len = 0  # the rendered chunks went with the widget


def _collapsible(title: str, body: str, css: str = "") -> Gtk.Widget:
    expander = Gtk.Expander()
    expander.set_label(title)
    expander.set_expanded(False)
    if css:
        expander.add_css_class(css)

    view = Gtk.TextView()
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_monospace(False)
    view.set_left_margin(8)
    view.set_right_margin(8)
    view.set_top_margin(6)
    view.set_bottom_margin(6)
    view.get_buffer().set_text(body)
    expander.set_child(view)
    return expander


def _tool_input_widget(tu: ToolUse) -> Gtk.Widget:
    """What was asked of the tool: a diff for a file edit, else its JSON."""
    diff = edit_diff(tu.name, tu.input)
    if diff is not None:
        from helios.widgets.code_block import CodeBlock

        return CodeBlock(diff, "diff")
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_monospace(True)
    view.set_left_margin(8)
    view.set_right_margin(8)
    view.set_top_margin(6)
    view.set_bottom_margin(6)
    view.get_buffer().set_text(_format_tool_input(tu.input))
    return view


def _tool_state(results: list[ToolResult]) -> str:
    """A card's lifecycle: running until its result lands, then done/error."""
    if not results:
        return "running"
    if any(_is_tool_failure(tr) for tr in results):
        return "error"
    return "done"


def _card_accessible_label(tu: ToolUse, state: str, elapsed: str) -> str:
    """What a screen reader announces for a card. The state is in the chip, and
    a chip is a bare word next to an icon — say the whole thing out loud."""
    return f"{tu.name} tool call, {state}{', ' + elapsed if elapsed else ''}"


def _tool_card_header(tu: ToolUse, state: str, elapsed: str) -> Gtk.Widget:
    summary = _summarize_tool_input(tu.input)
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    title = Gtk.Label(
        label=f"⚙ {tu.name}{'  ·  ' + summary if summary else ''}", xalign=0
    )
    title.add_css_class("helios-tool-card-title")
    title.set_hexpand(True)
    box.append(title)
    chip = Gtk.Label(label=f"{state} · {elapsed}" if elapsed else state)
    chip.add_css_class("helios-tool-state")
    chip.add_css_class(f"helios-tool-state-{state}")
    box.append(chip)
    return box


def _tool_use_widget(
    tu: ToolUse, results: list[ToolResult] | None = None, elapsed: str = ""
) -> Gtk.Widget:
    """One tool call as its own card: the invocation, its state chip, and —
    once the CLI sends it back — the output it produced, inside the same row."""
    results = list(results or ())
    state = _tool_state(results)
    expander = Gtk.Expander()
    expander.set_label_widget(_tool_card_header(tu, state, elapsed))
    expander.add_css_class("helios-tool-use")
    if state == "error":
        expander.add_css_class("helios-tool-error")
    expander.set_expanded(False)
    expander.update_property(
        [Gtk.AccessibleProperty.LABEL], [_card_accessible_label(tu, state, elapsed)]
    )

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    body.append(_tool_input_widget(tu))
    for tr in results:
        body.append(_tool_result_widget(tr, tu.name))
    expander.set_child(body)
    return expander


def _activity_cards(
    items: list[tuple[str, object, str]],
) -> list[tuple[object, list[ToolResult]]]:
    """Group the paired (kind, obj, name) stream into one entry per card.

    A tool_use heads a card and the results carrying its id go inside it; a
    result that pairs with nothing heads its own entry and keeps the standalone
    row it has always had.
    """
    cards: list[tuple[object, list[ToolResult]]] = []
    for kind, obj, _name in items:
        if kind == "tool_use":
            cards.append((obj, []))
        elif (
            cards
            and isinstance(cards[-1][0], ToolUse)
            and obj.tool_use_id  # type: ignore[union-attr]
            and cards[-1][0].id == obj.tool_use_id  # type: ignore[union-attr]
        ):
            cards[-1][1].append(obj)  # type: ignore[arg-type]
        else:
            cards.append((obj, []))
    return cards


def _card_widget(head: object, results: list[ToolResult], elapsed: dict) -> Gtk.Widget:
    if isinstance(head, ToolUse):
        return _tool_use_widget(head, results, elapsed.get(head.id, ""))
    return _tool_result_widget(head)  # type: ignore[arg-type]


def _files_changed_label(tool_uses: list[ToolUse]) -> Gtk.Widget | None:
    """"Files changed: a.py, b.py" — the one line that answers what a turn
    touched without expanding anything. None when it touched no files."""
    paths: list[str] = []
    for tu in tool_uses:
        if tu.name not in EDIT_TOOLS:
            continue
        path = edit_path(tu.input)
        if path and path not in paths:
            paths.append(path)
    if not paths:
        return None
    text = "Files changed: " + ", ".join(p.rsplit("/", 1)[-1] for p in paths[:6])
    if len(paths) > 6:
        text += f" +{len(paths) - 6}"
    label = Gtk.Label(label=text, xalign=0)
    label.set_wrap(True)
    label.add_css_class("caption")
    label.add_css_class("dim-label")
    label.add_css_class("helios-files-changed")
    return label


def _elapsed_text(started: str, finished: str) -> str:
    """How long a call took, from the two record timestamps that bracket it.

    ponytail: measured from the assistant MESSAGE, not per-call start — the
    transcript carries no per-call start, so on a multi-call message every
    card's clock begins together. Blank whenever either stamp is missing or
    unparseable, which is what makes it safe to show unconditionally.
    """
    # A throwaway Turn is the one ISO-8601 parser this codebase has (Turn.dt).
    start = Turn(role="assistant", timestamp=started).dt
    end = Turn(role="assistant", timestamp=finished).dt
    if start is None or end is None:
        return ""
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return ""
    return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"


def _is_shell_tool(name: str) -> bool:
    """Return whether *name* is one of the providers' shell-call aliases."""
    leaf = name.casefold().replace("-", "_").rsplit(".", 1)[-1]
    return leaf in {"bash", "shell", "exec_command", "command_execution"}


def _is_tool_failure(tr: ToolResult) -> bool:
    """A real tool error. Answering an AskUserQuestion arrives as an `is_error`
    result too (see _QUESTION_RESULT_LABELS) and is not a failure."""
    return tr.is_error and tr.content.strip() not in _QUESTION_RESULT_LABELS


def _error_count(results: list[ToolResult]) -> int:
    return sum(_is_tool_failure(tr) for tr in results)


def _tool_activity_label(
    tool_uses: list[ToolUse], *, live: bool = False, errors: int = 0
) -> str:
    """Compact invocation-only summary; tool results never inflate its count.

    Failures DO get counted — "Ran 4 shell commands · 1 error" is the whole
    reason to open the group, so it cannot be hidden two clicks deep.
    """
    shell_count = sum(_is_shell_tool(tool.name) for tool in tool_uses)
    other_count = len(tool_uses) - shell_count
    tail = f" · {errors} error{'' if errors == 1 else 's'}" if errors else ""

    if live:
        prefix = "Working"
        if not tool_uses:
            return f"{prefix}…"
        parts: list[str] = []
        if shell_count:
            noun = "shell command" if shell_count == 1 else "shell commands"
            parts.append(f"{shell_count} {noun}")
        if other_count:
            noun = "other action" if other_count == 1 else "other actions"
            parts.append(f"{other_count} {noun}")
        return f"{prefix} · " + " · ".join(parts)

    if shell_count:
        noun = "shell command" if shell_count == 1 else "shell commands"
        label = f"Ran {shell_count} {noun}"
        if other_count:
            other = "other action" if other_count == 1 else "other actions"
            label += f" · {other_count} {other}"
        return label + tail
    if other_count:
        noun = "tool action" if other_count == 1 else "tool actions"
        return f"Ran {other_count} {noun}{tail}"
    return "Tool results" + tail


def _init_lazy_group(expander: Gtk.Expander) -> None:
    """Attach shared lifecycle state used by lazy, incrementally built groups."""
    expander._activity_build_closed = False  # type: ignore[attr-defined]
    expander._activity_source_ids = set()  # type: ignore[attr-defined]
    expander._activity_complete = False  # type: ignore[attr-defined]


def _new_activity_expander(label: str, item_source) -> Gtk.Expander:
    expander = Gtk.Expander()
    expander.set_label(label)
    expander.add_css_class("helios-tool-use")
    _init_lazy_group(expander)
    expander._activity_item_source = item_source  # type: ignore[attr-defined]
    expander.set_expanded(False)
    expander.connect("notify::expanded", _on_activity_group_expanded, item_source)
    return expander


def _activity_group(turn: Turn, item_source=None) -> Gtk.Widget:
    """The turn's one collapsed activity group. ``item_source`` overrides the
    turn itself for a bubble that can also receive results later."""
    return _new_activity_expander(
        _tool_activity_label(turn.tool_uses, errors=_error_count(turn.tool_results)),
        turn if item_source is None else item_source,
    )


def _activity_items(item_source) -> list[tuple[str, object, str]]:
    if callable(item_source):
        return list(item_source())
    return activity_items(item_source)


def _on_activity_group_expanded(expander: Gtk.Expander, _param, item_source) -> None:
    if getattr(expander, "_activity_build_closed", False):
        return
    if not expander.get_expanded():
        return
    # A fully-built group is left in place; a *partially* built one (collapsed
    # mid-idle-build) was dropped by _cancel_on_collapse, so it rebuilds here.
    if getattr(expander, "_activity_complete", False):
        return
    if expander.get_child() is not None:  # defensive: unexpected leftover child
        expander.set_child(None)

    items = _activity_cards(_activity_items(item_source))
    elapsed = getattr(expander, "_tool_elapsed", {})

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    # Build the first batch synchronously so the user sees something instantly.
    first = items[:_ACTIVITY_FIRST_BATCH]
    for head, results in first:
        box.append(_card_widget(head, results, elapsed))

    expander.set_child(box)

    remaining = items[_ACTIVITY_FIRST_BATCH:]
    if not remaining:
        expander._activity_complete = True  # type: ignore[attr-defined]
        return

    # A monotonic build token stored ON the expander (one-element list so the
    # collapse handler and idle chain share one mutable cell). Bumping it
    # cancels the in-flight idle chain. The collapse handler reads the token
    # off the expander rather than closing over this build's cell, so it can be
    # connected exactly once per expander (no per-rebuild handler pileup).
    token = [0]
    expander._activity_build_token = token  # type: ignore[attr-defined]

    if not getattr(expander, "_activity_collapse_wired", False):
        expander.connect("notify::expanded", _cancel_on_collapse)
        expander._activity_collapse_wired = True  # type: ignore[attr-defined]

    def _append_batch(offset: int, generation: int) -> bool:
        # Stop if: token changed (collapsed or a new expand happened), or the
        # expander child was removed/replaced.
        if getattr(expander, "_activity_build_closed", False):
            return GLib.SOURCE_REMOVE
        if token[0] != generation:
            return GLib.SOURCE_REMOVE
        if expander.get_child() is not box:
            return GLib.SOURCE_REMOVE
        batch = remaining[offset : offset + _ACTIVITY_BATCH]
        if not batch:
            return GLib.SOURCE_REMOVE
        for head, results in batch:
            box.append(_card_widget(head, results, elapsed))
        next_offset = offset + _ACTIVITY_BATCH
        if next_offset < len(remaining):
            _queue_activity_idle(expander, _append_batch, next_offset, generation)
        else:
            expander._activity_complete = True  # type: ignore[attr-defined]
        return GLib.SOURCE_REMOVE

    _queue_activity_idle(expander, _append_batch, 0, token[0])


def _on_work_updates_expanded(
    expander: Gtk.Expander, _param, updates: list[str]
) -> None:
    """Build finalized commentary only if the user asks to read its details."""
    if getattr(expander, "_activity_build_closed", False):
        return
    if not expander.get_expanded():
        return
    if getattr(expander, "_activity_complete", False):
        return
    if expander.get_child() is not None:
        expander.set_child(None)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    first = updates[:_WORK_UPDATES_FIRST_BATCH]
    for update in first:
        box.append(_work_update(update))
    expander.set_child(box)

    remaining = updates[_WORK_UPDATES_FIRST_BATCH:]
    if not remaining:
        expander._activity_complete = True  # type: ignore[attr-defined]
        return

    token = [0]
    expander._activity_build_token = token  # type: ignore[attr-defined]
    if not getattr(expander, "_activity_collapse_wired", False):
        expander.connect("notify::expanded", _cancel_on_collapse)
        expander._activity_collapse_wired = True  # type: ignore[attr-defined]

    def _append_batch(offset: int, generation: int) -> bool:
        if getattr(expander, "_activity_build_closed", False):
            return GLib.SOURCE_REMOVE
        if token[0] != generation or expander.get_child() is not box:
            return GLib.SOURCE_REMOVE
        batch = remaining[offset : offset + _WORK_UPDATES_BATCH]
        for update in batch:
            box.append(_work_update(update))
        next_offset = offset + _WORK_UPDATES_BATCH
        if next_offset < len(remaining):
            _queue_activity_idle(expander, _append_batch, next_offset, generation)
        else:
            expander._activity_complete = True  # type: ignore[attr-defined]
        return GLib.SOURCE_REMOVE

    _queue_activity_idle(expander, _append_batch, 0, token[0])


def _invalidate_lazy_group(expander: Gtk.Expander) -> None:
    """Drop stale details while preserving the persistent expander itself."""
    if getattr(expander, "_activity_build_closed", False):
        return
    token = getattr(expander, "_activity_build_token", None)
    if token is not None:
        token[0] += 1
    _remove_activity_sources(expander)
    expander._activity_complete = False  # type: ignore[attr-defined]
    expander.set_child(None)
    if expander.get_expanded():
        # Rebuild from the live provider immediately so an open expander never
        # presents stale command details.
        _on_activity_group_expanded(
            expander,
            None,
            expander._activity_item_source,  # type: ignore[attr-defined]
        )


def _queue_activity_idle(expander: Gtk.Expander, callback, *args) -> int:
    """Schedule and own one activity-build idle on its expander."""
    if getattr(expander, "_activity_build_closed", False):
        return 0
    source: list[int] = [0]

    def _run() -> bool:
        expander._activity_source_ids.discard(source[0])  # type: ignore[attr-defined]
        callback(*args)
        return False

    source[0] = GLib.idle_add(_run)
    expander._activity_source_ids.add(source[0])  # type: ignore[attr-defined]
    return source[0]


def _remove_activity_sources(widget: Gtk.Widget) -> None:
    sources = getattr(widget, "_activity_source_ids", None)
    if sources is None:
        return
    for source_id in tuple(sources):
        try:
            GLib.source_remove(source_id)
        except Exception:
            pass
    sources.clear()


def _cancel_activity_builds(widget: Gtk.Widget) -> None:
    """Recursively invalidate activity idle chains below *widget*.

    Checking the Python-only closed marker is deliberately the first operation
    in each queued batch, so a callback dispatched after teardown returns
    without inspecting or mutating a finalizing GTK subtree."""
    token = getattr(widget, "_activity_build_token", None)
    if token is not None:
        token[0] += 1
    if hasattr(widget, "_activity_build_closed"):
        widget._activity_build_closed = True  # type: ignore[attr-defined]
        _remove_activity_sources(widget)
    child = widget.get_first_child()
    while child is not None:
        next_child = child.get_next_sibling()
        _cancel_activity_builds(child)
        child = next_child


def _cancel_on_collapse(expander: Gtk.Expander, _param) -> None:
    """On collapse, abort any in-flight idle build. If the build was still
    incomplete, drop the partial child so the next expand rebuilds in full
    (otherwise the leftover child would short-circuit the expand handler and
    the group would stay permanently truncated)."""
    if getattr(expander, "_activity_build_closed", False):
        return
    if expander.get_expanded():
        return
    token = getattr(expander, "_activity_build_token", None)
    if token is not None:
        token[0] += 1
    _remove_activity_sources(expander)
    if not getattr(expander, "_activity_complete", False):
        expander.set_child(None)


def _tool_result_widget(tr: ToolResult, name: str = "") -> Gtk.Widget:
    content_stripped = tr.content.strip()

    # Answering (or dismissing) an AskUserQuestion prompt is delivered as a
    # tool DENY, which the CLI records as an `is_error` tool_result — but it is
    # not a failure. Render those as neutral info rows, never as a tool error.
    question_label = _QUESTION_RESULT_LABELS.get(content_stripped)
    is_benign_question = question_label is not None
    show_as_error = tr.is_error and not is_benign_question

    if is_benign_question:
        label = question_label
        preview = ""
    else:
        if tr.is_error:
            label = f"⚠ {name} error" if name else "⚠ tool error"
        else:
            label = f"↳ {name} result" if name else "↳ tool result"
        preview = content_stripped.splitlines()[0] if content_stripped else "(empty)"
        if len(preview) > 80:
            preview = preview[:77] + "..."

    expander = Gtk.Expander()
    expander.set_label(f"{label}  ·  {preview}" if preview else label)
    expander.add_css_class("helios-tool-result")
    if show_as_error:
        expander.add_css_class("helios-tool-error")
    expander.set_expanded(False)

    full = tr.content
    truncated = len(full) > MAX_TOOL_PREVIEW * 4
    content = full[: MAX_TOOL_PREVIEW * 4] + "\n\n… (truncated)" if truncated else full

    view = Gtk.TextView()
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_monospace(True)
    view.set_left_margin(8)
    view.set_right_margin(8)
    view.set_top_margin(6)
    view.set_bottom_margin(6)
    view.get_buffer().set_text(content)
    if not truncated:
        expander.set_child(view)
        return expander
    # Truncation stays the default (a 2 MB buffer in a TextView is why it
    # exists) but stops being terminal: one click gets the rest.
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.append(view)
    more = Gtk.Button(label=f"Show full output ({len(full):,} chars)")
    more.add_css_class("flat")
    more.set_halign(Gtk.Align.START)
    more.connect("clicked", lambda btn: (view.get_buffer().set_text(full), box.remove(btn)))
    box.append(more)
    expander.set_child(box)
    return expander


def _summarize_tool_input(inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "command", "pattern", "path", "url", "query"):
        if key in inp and isinstance(inp[key], str):
            v = inp[key].strip()
            if not v:
                continue
            if len(v) > 80:
                v = v[:77] + "..."
            return v
    return ""


def _format_tool_input(inp: dict) -> str:
    import json

    try:
        text = json.dumps(inp, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(inp)
    if len(text) > MAX_TOOL_INPUT * 10:
        text = text[: MAX_TOOL_INPUT * 10] + "\n... (truncated)"
    return text
