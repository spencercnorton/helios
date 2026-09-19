from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from helios.backend import model_catalog, session_providers
from helios.backend.projects import Session
from helios.backend.transcript import Turn, iter_transcript, read_transcript_since
from helios.widgets.message_bubble import MessageBubble, StreamingBubble


INITIAL_TURN_LIMIT = 150
OLDER_TURN_BATCH = 75



def _aligned_start(turns, start: int) -> int:
    """Never cut a render batch between a call and its results.

    A results-only turn directly follows the turn that made the calls. A cut
    that lands on it puts the results in one batch and the call in the next,
    and batch-scoped pairing cannot join them — measured on a real 506-turn
    session: three of five Load-earlier cuts split a pair. Walk the cut back
    onto the call.
    """
    while 0 < start < len(turns):
        turn = turns[start]
        if turn.role == "tool" and turn.tool_results and not turn.tool_uses:
            start -= 1
            continue
        break
    return start

class TranscriptView(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-transcript")
        self._destroyed = False

        # Banner at top — title, mtime, session id.
        self._banner = _Banner()
        self.append(self._banner)

        # Scrollable conversation.
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_hexpand(True)
        self._scroller.set_vexpand(True)
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # The bulk load hides the list (opacity 0) so batch pop-in and the
        # landing scroll's convergence jumps are not visible. That left a
        # completely blank pane with no feedback, which reads as "the click did
        # nothing" — and the natural response, clicking again, used to restart
        # the render. Overlay a spinner so the wait is legible instead.
        self._loading_overlay = Gtk.Overlay()
        self._loading_overlay.set_child(self._scroller)
        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(32, 32)
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._spinner.set_valign(Gtk.Align.CENTER)
        self._spinner.set_visible(False)
        self._loading_overlay.add_overlay(self._spinner)
        # Jump-to-bottom. The pin releases 60px off the tail (SCROLL_PIN_SLACK)
        # and only three paths re-pin it, so without this the way back is a
        # manual scroll. `osd` is the stock GTK translucent-pill style — no new CSS.
        self._jump_btn = Gtk.Button.new_from_icon_name("go-bottom-symbolic")
        self._jump_btn.set_tooltip_text("Jump to latest")
        self._jump_btn.add_css_class("circular")
        self._jump_btn.add_css_class("osd")
        self._jump_btn.set_halign(Gtk.Align.END)
        self._jump_btn.set_valign(Gtk.Align.END)
        self._jump_btn.set_margin_end(18)
        self._jump_btn.set_margin_bottom(18)
        self._jump_btn.set_visible(False)
        self._jump_btn.connect("clicked", self._on_jump_to_bottom)
        self._loading_overlay.add_overlay(self._jump_btn)
        self.append(self._loading_overlay)

        self._clamp = Adw.Clamp()
        self._clamp.set_maximum_size(900)
        self._clamp.set_tightening_threshold(700)

        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._list.add_css_class("helios-transcript-list")
        self._list.set_margin_top(16)
        self._list.set_margin_bottom(48)
        self._list.set_margin_start(16)
        self._list.set_margin_end(16)
        self._clamp.set_child(self._list)

        # Empty placeholder.
        self._empty = Adw.StatusPage()
        self._empty.set_icon_name("user-available-symbolic")
        self._empty.set_title("No session selected")
        self._empty.set_description("Pick a session from the list to view its transcript.")

        self._scroller.set_child(self._empty)

        self._session: Session | None = None
        self._assistant_label = "Claude"
        self._render_token = 0
        # Count of finalized turns currently shown. NOT the follow cursor —
        # `_follow_offset` is. Kept as the render counter (append_turn,
        # _render_batch) so bubbles can be indexed by turn number.
        self._rendered_turns = 0
        #: tool_use ids of the message currently streaming, and results that
        #: arrived for them before the message finalized. Measured on the live
        #: wire (2026-09-03): the CLI emits tool_result user records BEFORE the
        #: message_stop that finalizes the assistant message, so a result
        #: routinely reaches the transcript before the card it belongs to.
        self._live_call_ids: set[str] = set()
        self._early_results: list = []
        # Byte position in the followed transcript just past the last COMPLETE
        # line rendered. Always set by the same read that produced the turns.
        self._follow_offset = 0
        self._history_start_index = 0
        self._load_earlier_row: Gtk.Widget | None = None
        self._streaming_bubble: StreamingBubble | None = None
        # Queued messages (typed mid-turn, awaiting auto-send), qid -> row.
        # Invariant: queued rows are always the LAST children of _list —
        # everything else inserts above them via _append_content().
        self._queued_rows: dict[int, Gtk.Widget] = {}
        # Streaming throttle: claude can emit ~50 stream_event deltas/second
        # on a long response. Each one used to trigger a full re-render of
        # the in-flight bubble (allocating Pango labels, GtkSourceView5
        # buffers, etc.), which got expensive once code blocks showed up.
        # We coalesce: store the latest streaming object and flush it from
        # a single 60ms timer.
        self._streaming_latest = None
        self._streaming_flush_id: int = 0
        self._idle_source_ids: set[int] = set()

        # While True, the sticky-bottom handler does NOT scroll on content
        # changes — set during initial bulk-load of a session so the user
        # doesn't watch ~190 bubbles turbo-scroll past. Once the last batch
        # has appended, we set this False and run the landing scroll.
        # Set before the first _set_pinned() call below, which reads it.
        self._bulk_loading = False
        # Sticky-bottom auto-scroll. We track whether the user is "near"
        # the bottom (within SCROLL_PIN_SLACK pixels). If so, every time
        # content grows we re-pin them. If they scroll up, we release the
        # pin so we don't yank them back.
        self._set_pinned(True)
        # Landing-scroll state (post-bulk-load convergence to bottom).
        self._landing_remaining = 0
        self._landing_last_upper = -1.0
        self._landing_deadline = 0.0
        # Keep references to the connected signal handlers so we can
        # safely disconnect them when the adjustment is replaced.
        self._vadj_value_handler = None
        self._vadj_upper_handler = None
        self._wire_scroll_signals()

    SCROLL_PIN_SLACK = 60  # pixels — within this distance from the bottom counts as "pinned"

    def shutdown(self) -> None:
        """Cancel every source owned by the transcript and make queued idle
        callbacks inert.  Driver handlers are guarded at MainWindow too, but a
        stream flush/render batch scheduled *before* close owns its own route
        back into this widget and therefore must self-defend. Idempotent."""
        if self._destroyed:
            return
        self._destroyed = True
        self._render_token += 1  # invalidates render + landing idle chains
        self._landing_remaining = 0
        self._bulk_loading = False
        self._cancel_pending_stream_flush()
        for source_id in tuple(self._idle_source_ids):
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
        self._idle_source_ids.clear()

        # Cancel MessageBubble's independently-owned batched activity idles.
        child = self._list.get_first_child()
        while child is not None:
            self._shutdown_content_child(child)
            child = child.get_next_sibling()

        # Adjustment signals can enqueue a final scroll idle during teardown.
        adj = self._scroller.get_vadjustment()
        if adj is not None:
            for attr in ("_vadj_value_handler", "_vadj_upper_handler"):
                handler = getattr(self, attr, None)
                if handler is not None:
                    try:
                        adj.disconnect(handler)
                    except Exception:
                        pass
                    setattr(self, attr, None)

    def _wire_scroll_signals(self) -> None:
        adj = self._scroller.get_vadjustment()
        if adj is None:
            return
        # value-changed: user scrolled (or programmatic). Update the pin.
        self._vadj_value_handler = adj.connect("value-changed", self._on_vadj_value_changed)
        # changed: page-size or upper changed (content grew/shrunk). Follow
        # if the user was pinned.
        self._vadj_upper_handler = adj.connect("changed", self._on_vadj_changed)

    def _queue_idle(self, callback, *args) -> int:
        """Own a one-shot idle so shutdown can remove it before finalization."""
        if self._destroyed:
            return 0
        source: list[int] = [0]

        def _run() -> bool:
            self._idle_source_ids.discard(source[0])
            callback(*args)
            return False

        source[0] = GLib.idle_add(_run)
        self._idle_source_ids.add(source[0])
        return source[0]

    @staticmethod
    def _shutdown_content_child(widget: Gtk.Widget) -> None:
        """Stop child-owned callbacks before the child is detached."""
        shutdown = getattr(widget, "shutdown", None)
        if callable(shutdown):
            shutdown()

    def _remove_content_child(self, widget: Gtk.Widget) -> None:
        """Dispose one transcript child before removing it from the list."""
        self._shutdown_content_child(widget)
        self._list.remove(widget)

    def _clear_content(self) -> None:
        self._live_call_ids.clear()
        self._early_results.clear()
        while (child := self._list.get_first_child()) is not None:
            self._remove_content_child(child)

    def _append_content(self, widget: Gtk.Widget) -> None:
        """Add transcript content above any queued-message rows, which stay
        pinned to the tail until they're sent (or removed)."""
        if self._destroyed:
            return
        if self._queued_rows:
            first_queued = next(iter(self._queued_rows.values()))
            # insert_child_after(w, None) means "at the first position" —
            # correct for the only case prev can be None (queued rows only).
            self._list.insert_child_after(widget, first_queued.get_prev_sibling())
        else:
            self._list.append(widget)

    def append_turn(self, turn) -> None:
        """Add a finalized Turn to the live transcript.

        Any in-progress streaming bubble gets removed first (the canonical
        Turn supersedes it).
        """
        if self._destroyed:
            return
        if turn.role == "tool" and turn.tool_results and not turn.tool_uses:
            # Live results: land on a resting card, or wait for the streaming
            # message that made the call to finalize. Doing this BEFORE the
            # streaming bubble is cleared keeps a mid-stream result from
            # tearing the live bubble down and re-creating it below the row.
            turn = self._absorb_live_results(turn)
            if turn is None:
                return
        elif self._early_results and not (turn.role == "assistant" and turn.tool_uses):
            # The message that owned the held results never finalized (an
            # interrupt, a new prompt): show them on their own rather than
            # dropping them with the next clear.
            self._flush_early_results()
        self._cancel_pending_stream_flush()
        # Animate the entrance only for genuinely fresh bubbles. When this turn
        # is finalizing a streaming response, the content was already visible —
        # fading it back in from 0 would read as a blink — so skip it there.
        fresh = self._streaming_bubble is None
        self._clear_streaming_bubble()
        if not turn.has_content:
            return
        # When a NEW turn (especially the user's own message) is appended,
        # they almost always want to follow it — re-pin even if they had
        # scrolled up earlier.
        if turn.role == "user":
            self._set_pinned(True)
        self._append_turn_bubble(turn, animate_in=fresh)
        # The adjustment's "changed" signal will fire once the new bubble
        # is laid out — that's when we actually know the new upper bound.
        # No need for an idle_add here.

    def set_last_turn_footer(self, text: str) -> None:
        """Caption the newest assistant bubble with its turn's cost/tokens.

        Queued rows are pinned after the content (see _append_content), so they
        are skipped; if what remains is not an assistant bubble then the turn
        this would annotate is no longer last and nothing happens. Empty text
        clears an existing footer, so a provider that reports nothing leaves no
        empty row behind.
        """
        child = self._list.get_last_child()
        while isinstance(child, _QueuedRow):
            child = child.get_prev_sibling()
        if isinstance(child, MessageBubble) and child.has_css_class(
            "helios-bubble-assistant"
        ):
            child.set_footer(text)

    def _append_turn_bubble(self, turn, *, animate_in: bool = False) -> None:
        """Render one finalized Turn, first giving a results-only record the
        chance to land on the cards that made the calls.

        The CLI writes a tool_result as its own user-role record, so the call
        and its output are always two separate Turns — rendered independently
        that is the standalone "Tool results" row the transcript used to show
        for every single tool call. Every append path routes through here so a
        reload pairs exactly like a live turn does.
        """
        if turn.role == "tool" and turn.tool_results and not turn.tool_uses:
            turn = self._pair_tool_results(turn)
            if turn is None:
                return
        bubble = MessageBubble(
            turn,
            animate_in=animate_in,
            assistant_label=self._assistant_label,
        )
        self._append_content(bubble)
        self._rendered_turns += 1
        if turn.tool_uses:
            self._drain_early_results(bubble)

    def _absorb_live_results(self, turn):
        """Pair a live results-only turn, holding what the streaming message owns.

        Returns the Turn of results that must still render on their own, or
        None when nothing is left to render.
        """
        turn = self._pair_tool_results(turn)
        if turn is None:
            return None
        held = [tr for tr in turn.tool_results if tr.tool_use_id in self._live_call_ids]
        if not held:
            return turn
        self._early_results.extend((tr, turn) for tr in held)
        rest = [tr for tr in turn.tool_results if tr.tool_use_id not in self._live_call_ids]
        if not rest:
            return None
        return Turn(
            role=turn.role,
            tool_results=rest,
            timestamp=turn.timestamp,
            uuid=turn.uuid,
            is_sidechain=turn.is_sidechain,
            is_meta=turn.is_meta,
            activity_content_index=turn.activity_content_index,
        )

    def _flush_early_results(self) -> None:
        self._live_call_ids.clear()
        held, self._early_results = self._early_results, []
        for tr, source in held:
            self._append_content(
                MessageBubble(
                    Turn(role=source.role, tool_results=[tr], timestamp=source.timestamp),
                    assistant_label=self._assistant_label,
                )
            )

    def _drain_early_results(self, bubble) -> None:
        """The message that made the calls has landed: attach what was held."""
        self._live_call_ids.clear()
        if not self._early_results:
            return
        held, self._early_results = self._early_results, []
        leftovers: list = []
        for tr, source in held:
            if not bubble.attach_tool_result(tr, source.timestamp):
                leftovers.append((tr, source))
        for tr, source in leftovers:
            # ponytail: one standalone row per orphan, in arrival order; the
            # id belonged to the live message but no card claimed it.
            self._append_content(
                MessageBubble(
                    Turn(role=source.role, tool_results=[tr], timestamp=source.timestamp),
                    assistant_label=self._assistant_label,
                )
            )

    def _pair_tool_results(self, turn, candidates=None):
        """Hand each result to the most recent bubble still waiting for it.

        Returns a Turn carrying only the results that matched nothing (those
        keep the standalone rendering), or None when every result was claimed.
        ``candidates`` restricts the search to one batch of bubbles, which is
        what a backwards insertion (Load earlier) needs: those turns precede
        everything already on screen, so scanning the whole list would let an
        old result claim a newer card.
        """
        unclaimed = [
            tr
            for tr in turn.tool_results
            if not self._attach_tool_result(tr, turn, candidates)
        ]
        if not unclaimed:
            return None
        if len(unclaimed) == len(turn.tool_results):
            return turn
        return Turn(
            role=turn.role,
            tool_results=unclaimed,
            timestamp=turn.timestamp,
            uuid=turn.uuid,
            is_sidechain=turn.is_sidechain,
            is_meta=turn.is_meta,
            activity_content_index=turn.activity_content_index,
        )

    def _attach_tool_result(self, tr, turn, candidates=None) -> bool:
        if candidates is not None:
            for bubble in reversed(candidates):
                if bubble.attach_tool_result(tr, turn.timestamp):
                    return True
            return False
        child = self._list.get_last_child()
        while child is not None:
            if isinstance(child, MessageBubble) and child.attach_tool_result(
                tr, turn.timestamp
            ):
                return True
            child = child.get_prev_sibling()
        return False

    def append_meta_turn(self, turn) -> None:
        """Insert a live system marker without replacing streamed content.

        A provider compaction can complete in the middle of a model turn. The
        ordinary ``append_turn`` path correctly clears the streaming bubble
        when a finalized model turn supersedes it; a metadata boundary does
        not supersede that content and must leave it intact. When the model
        turn later finalizes, its replacement bubble lands after this marker,
        matching the durable transcript order.
        """

        if self._destroyed or not turn.has_content:
            return
        self._append_content(
            MessageBubble(
                turn,
                animate_in=True,
                assistant_label=self._assistant_label,
            )
        )
        self._rendered_turns += 1

    # ── queued messages (typed while a turn was in flight) ──

    def append_queued(self, qid: int, text: str, on_remove) -> None:
        """Show a pending message row at the transcript tail. `on_remove` is
        called with `qid` when the user clicks its ✕."""
        if self._destroyed:
            return
        row = _QueuedRow(qid, text, on_remove)
        self._queued_rows[qid] = row
        self._list.append(row)
        # The user just wrote this — follow it.
        self._set_pinned(True)

    def remove_queued(self, qid: int) -> None:
        """Drop a queued row (sent, removed by the user, or handed back)."""
        if self._destroyed:
            return
        row = self._queued_rows.pop(qid, None)
        if row is not None:
            try:
                self._remove_content_child(row)
            except Exception:
                pass  # already gone with a full clear

    def clear_queued(self) -> None:
        if self._destroyed:
            return
        for qid in list(self._queued_rows):
            self.remove_queued(qid)

    # ── provider errors / hook notices ──

    def append_error(self, message: str) -> None:
        """Record a provider failure in the transcript.

        A toast is gone in four seconds; the error that ended a turn has to
        stay readable and copyable. Deliberately NOT append_turn(): that
        clears the streaming bubble, and an error arriving mid-stream must
        not erase the partial response it is explaining.
        """
        if self._destroyed:
            return
        self._append_content(
            MessageBubble(Turn(role="error", text_parts=[message]), animate_in=True)
        )
        self._set_pinned(True)

    def append_notice(self, title: str, detail: str, severity: str) -> None:
        """Record a compact meta row for a hook lifecycle notice (blocked,
        asked, or failed). Same non-clearing contract as append_error: a
        live streaming bubble must survive a notice landing mid-turn.
        """
        if self._destroyed:
            return
        # ponytail: severity is threaded through for callers but not yet
        # used here — both warning and error notices render identically as
        # role="hook". Split into per-severity roles/CSS if that distinction
        # earns its keep.
        text = f"{title}\n\n{detail}" if detail else title
        self._append_content(
            MessageBubble(Turn(role="hook", text_parts=[text]), animate_in=True)
        )
        self._set_pinned(True)

    # Stream throttle: at most this often will we redo the in-flight bubble.
    # 60ms ≈ 16.6 fps, well below the perceptual threshold for "smooth" and
    # well above what claude emits structurally meaningful state at.
    _STREAM_RENDER_INTERVAL_MS = 60

    def show_streaming_assistant(self, streaming) -> None:
        """Stash the latest streaming snapshot; flush via the coalescing
        timer.

        Without this, every stream_event delta (~50/s on a long response)
        would re-render the in-flight bubble per delta and the UI would
        visibly stutter once code blocks appeared.
        """
        if self._destroyed:
            return
        self._streaming_latest = streaming
        self._live_call_ids.update(
            block.tool_use_id
            for block in getattr(streaming, "blocks", ()) or ()
            if getattr(block, "type", "") == "tool_use" and getattr(block, "tool_use_id", "")
        )
        if self._streaming_flush_id == 0:
            self._streaming_flush_id = GLib.timeout_add(
                self._STREAM_RENDER_INTERVAL_MS, self._flush_streaming
            )

    def _flush_streaming(self) -> bool:
        self._streaming_flush_id = 0
        if self._destroyed:
            self._streaming_latest = None
            return False
        streaming = self._streaming_latest
        self._streaming_latest = None
        if streaming is None:
            return False
        # One persistent bubble, mutated in place (see StreamingBubble) — the
        # old remove+append-a-fresh-MessageBubble per flush was the visible
        # flicker during streaming. Created lazily on first real content so
        # an empty shell never shows.
        if self._streaming_bubble is None:
            if not any(b.text or b.tool_use_name for b in streaming.blocks):
                return False
            self._streaming_bubble = StreamingBubble(
                assistant_label=self._assistant_label
            )
            self._append_content(self._streaming_bubble)
        self._streaming_bubble.update(streaming)
        return False  # one-shot
        # Same as append_turn: rely on the vadjustment::changed signal to
        # auto-follow if pinned.

    def _set_pinned(self, pinned: bool) -> None:
        """Single writer for the sticky-bottom pin; the jump button is its
        inverse, hidden while a bulk load is hiding the list anyway."""
        self._pinned_to_bottom = pinned
        self._jump_btn.set_visible(not pinned and not self._bulk_loading)

    def _on_vadj_value_changed(self, adj) -> None:
        """User scrolled. Update the pin: pinned iff near the bottom."""
        if self._destroyed:
            return
        # During bulk-load we synthesize a lot of scroll events as bubbles
        # land; don't let any of them flip the user's pin state.
        if self._bulk_loading:
            return
        dist_from_bottom = adj.get_upper() - adj.get_page_size() - adj.get_value()
        self._set_pinned(dist_from_bottom <= self.SCROLL_PIN_SLACK)

    def _on_vadj_changed(self, adj) -> None:
        """Content size changed. If pinned, scroll to the new bottom."""
        if self._destroyed:
            return
        # Short-circuit while bulk-loading a historical session — otherwise
        # the sticky-bottom would fire per batch and the user would watch
        # the transcript turbo-scroll past them. set_session/_render_batch
        # do the single final jump-to-bottom themselves when the last
        # batch lands.
        if self._bulk_loading:
            return
        if not self._pinned_to_bottom:
            return
        # Defer one tick so any in-progress layout settles before we read upper.
        self._queue_idle(self._scroll_to_end_now, adj)

    def _scroll_to_end_now(self, adj) -> bool:
        if self._destroyed:
            return False
        # Suppress the value-changed handler while we programmatically move
        # the scrollbar — otherwise our own move would trigger the pin logic
        # before layout is complete and could flicker.
        adj.handler_block(self._vadj_value_handler)
        try:
            adj.set_value(adj.get_upper() - adj.get_page_size())
        finally:
            adj.handler_unblock(self._vadj_value_handler)
        return False

    def _on_jump_to_bottom(self, _btn: Gtk.Button) -> None:
        if self._destroyed:
            return
        adj = self._scroller.get_vadjustment()
        if adj is None:
            return
        self._set_pinned(True)
        self._scroll_to_end_now(adj)

    # ── Landing scroll (post-bulk-load) ──
    # When a historical transcript finishes loading, we want to land at the
    # bottom. Single-shot scrolling races with GTK's layout pipeline: long
    # markdown paragraphs and code blocks need multiple measure passes to
    # finalize `upper`, and if we scroll-to-(stale-upper) the user ends up
    # mid-conversation. This routine keeps scrolling on every layout-change
    # signal until `upper` stops moving (two consecutive identical readings)
    # or a max-attempt budget runs out.
    _LANDING_MAX_ATTEMPTS = 12

    # Wall-clock ceiling on the hidden window, independent of the attempt count.
    # Each attempt forces a full re-measure of a tall transcript, and a session
    # that is actively streaming keeps `upper` moving so convergence may never be
    # reached — 12 attempts then cost 12 layout passes before anything appears.
    # Showing a transcript that is a few pixels off the bottom beats showing
    # nothing: the sticky-bottom handler corrects it on the next frame anyway.
    _LANDING_MAX_SECONDS = 0.75

    def _start_landing_scroll(self) -> None:
        if self._destroyed:
            return
        adj = self._scroller.get_vadjustment()
        if adj is None:
            self._set_loading(False)  # nothing to converge on — just show
            return
        self._landing_remaining = self._LANDING_MAX_ATTEMPTS
        self._landing_last_upper = -1.0
        self._landing_deadline = time.monotonic() + self._LANDING_MAX_SECONDS
        # Kick the first attempt on the next idle to let layout settle once.
        self._queue_idle(self._landing_step, adj, self._render_token)

    def _landing_step(self, adj, token: int) -> bool:
        # A session switch mid-landing starts its own load + landing — this
        # stale loop must neither scroll nor reveal the new session's
        # half-loaded list.
        if self._destroyed or token != self._render_token:
            return False
        # Stop conditions:
        #   * Out of attempts — give up so we don't busy-loop forever
        #   * Upper stable for two consecutive checks — we're done
        # Either way the list gets revealed: the convergence jumps happened
        # while it was hidden, so the user sees the transcript appear once,
        # parked at the bottom.
        if self._landing_remaining <= 0 or time.monotonic() >= self._landing_deadline:
            self._set_loading(False)
            return False
        upper = adj.get_upper()
        # Always scroll to current bottom.
        self._scroll_to_end_now(adj)
        if upper == self._landing_last_upper:
            # Stable. We landed.
            self._set_loading(False)
            return False
        self._landing_last_upper = upper
        self._landing_remaining -= 1
        # Re-check on the next idle so any further layout pass can run.
        self._queue_idle(self._landing_step, adj, token)
        return False

    def _clear_streaming_bubble(self) -> None:
        if self._streaming_bubble is not None:
            try:
                self._remove_content_child(self._streaming_bubble)
            except Exception:
                pass
            self._streaming_bubble = None

    def _cancel_pending_stream_flush(self) -> None:
        if self._streaming_flush_id:
            try:
                GLib.source_remove(self._streaming_flush_id)
            except Exception:
                pass
            self._streaming_flush_id = 0
        self._streaming_latest = None

    def show_live_session(self, cwd: str, model: str) -> None:
        """Prepare the view for a brand-new live session."""
        if self._destroyed:
            return
        provider = model_catalog.provider_for(model)
        self._assistant_label = (
            "GPT" if provider == model_catalog.PROVIDER_OPENAI
            else "OpenRouter" if provider == model_catalog.PROVIDER_OPENROUTER
            else "Claude"
        )
        self._render_token += 1
        self._session = None
        self._rendered_turns = 0
        self._follow_offset = 0
        self._history_start_index = 0
        self._load_earlier_row = None
        self._cancel_pending_stream_flush()
        self._clear_streaming_bubble()
        self._queued_rows.clear()
        self._clear_content()
        self._set_loading(False)  # nothing to bulk-load — show immediately
        self._scroller.set_child(self._clamp)
        self._banner.set_live(cwd=cwd, model=model)

    def append_new_from_disk(self) -> int:
        """Append transcript records written since the last render.

        Used to live-follow a session that has no in-process driver emitting
        events (e.g. one being driven by an external `claude`/IDE, or a remote
        pool session). Reads only the bytes appended since the last render and
        appends what they contain. Returns how many were appended. No-op (0) if
        no on-disk session is set, history is still batching in, or there's a
        streaming bubble (driver-backed)."""
        if (
            self._destroyed
            or self._session is None
            or self._bulk_loading
            or self._streaming_bubble is not None
        ):
            return 0
        tail = read_transcript_since(self._session.path, self._follow_offset)
        if tail.restarted:
            # Truncated or replaced under us — a byte offset cannot reconcile
            # that. Rebuild from scratch; set_session re-reads from 0.
            self.set_session(self._session)
            return 0
        self._follow_offset = tail.offset
        # Fade in only a lone arrival. A burst (several records in one
        # debounce window) fading in as a stack reads as flicker, not polish.
        animate = len(tail.turns) == 1
        for turn in tail.turns:
            self._append_turn_bubble(turn, animate_in=animate)
        return len(tail.turns)

    def set_session(self, session: Session | None) -> None:
        if self._destroyed:
            return
        # Re-selecting the session that is already mid-load is a NO-OP.
        # Bumping the token here aborted the in-flight batch chain and started
        # the whole render again, so clicking a second time because nothing
        # appeared genuinely made it slower — the load could be pushed out
        # indefinitely by an impatient user. Identity, not equality: a caller
        # handing us a refreshed Session object for the same id must not restart
        # the render either.
        if (
            self._bulk_loading
            and session is not None
            and self._session is not None
            and session.session_id == self._session.session_id
        ):
            return
        self._render_token += 1
        token = self._render_token
        self._session = session
        resolution = (
            session_providers.resolve_provider(session.session_id, session.path)
            if session is not None
            else session_providers.ProviderResolution()
        )
        self._assistant_label = (
            "GPT"
            if resolution.known
            and resolution.provider == model_catalog.PROVIDER_OPENAI
            else "OpenRouter"
            if resolution.known
            and resolution.provider == model_catalog.PROVIDER_OPENROUTER
            else "Claude"
            if resolution.known
            and resolution.provider == model_catalog.PROVIDER_ANTHROPIC
            else "Assistant"
        )
        self._rendered_turns = 0
        # Cleared here too so the `session is None` return below cannot leave a
        # cursor pointing into a file we are no longer showing.
        self._follow_offset = 0
        self._history_start_index = 0
        self._load_earlier_row = None
        self._cancel_pending_stream_flush()
        # The clear below already removed every child; just drop the stale
        # refs so the next stream flush / queue append starts fresh instead
        # of remove()-ing widgets that are no longer children.
        self._streaming_bubble = None
        self._queued_rows.clear()

        # Clear current bubbles.
        self._clear_content()

        if session is None:
            self._set_loading(False)
            self._scroller.set_child(self._empty)
            self._banner.clear()
            return

        self._scroller.set_child(self._clamp)
        self._banner.set_session(session)
        # New session: suppress sticky-bottom while we bulk-load every
        # historical bubble (otherwise the auto-scroll fires per batch and
        # the user watches the entire conversation turbo-scroll by). We
        # re-enable + jump to bottom after the final batch. The list is also
        # HIDDEN (opacity 0) for the duration: the batch pop-in and the
        # landing scroll's convergence jumps were visible as glitches when
        # switching sessions — now the transcript appears in one piece,
        # already at the bottom.
        self._bulk_loading = True
        self._set_pinned(True)  # final state when load completes
        self._set_loading(True)

        # Build only the recent tail as widgets. Older turns stay available via
        # the "Load earlier" row, which avoids constructing hundreds of GTK
        # subtrees when opening long sessions.
        # The follow cursor is a FILE position, not a render position: this
        # reads to EOF even though only the last INITIAL_TURN_LIMIT turns
        # become widgets. The offset must come from the same read that
        # produced the turns, never from a second stat that can race it.
        tail = read_transcript_since(session.path)
        turns = tail.turns
        self._follow_offset = tail.offset
        self._history_start_index = _aligned_start(
            turns, max(0, len(turns) - INITIAL_TURN_LIMIT)
        )
        self._rendered_turns = self._history_start_index
        if self._history_start_index > 0:
            self._prepend_load_earlier_row()

        # Render the initial tail lazily in idle batches — keeps the UI
        # responsive while the recent transcript appears.
        iterator = iter(turns[self._history_start_index:])
        self._render_batch(iterator, token, first=True)

    def _set_loading(self, loading: bool) -> None:
        """Hide/reveal the transcript list around a bulk load. CSS fades the
        reveal (opacity transition on .helios-transcript-list).

        The spinner is what the user actually sees while the list is hidden.
        `Gtk.Spinner` must be stopped as well as hidden — an invisible spinning
        spinner keeps requesting frames.
        """
        if self._destroyed:
            return
        if loading:
            self._list.add_css_class("helios-loading")
            self._spinner.set_visible(True)
            self._spinner.start()
            self._jump_btn.set_visible(False)  # an OSD pill over a spinner is noise
        else:
            self._list.remove_css_class("helios-loading")
            self._spinner.stop()
            self._spinner.set_visible(False)

    def _render_batch(self, iterator, token: int, first: bool = False) -> bool:
        if self._destroyed or token != self._render_token:
            return False  # stop — session changed
        BATCH = 8
        finished = False
        for _ in range(BATCH):
            try:
                turn = next(iterator)
            except StopIteration:
                finished = True
                break
            self._append_turn_bubble(turn)
        if finished:
            # Last batch is in. Re-engage sticky-bottom — and then keep
            # scrolling to the new bottom until `upper` stops changing.
            # Big transcripts with long markdown paragraphs and code
            # blocks can take multiple layout passes to fully measure;
            # without persistence, our one-shot scroll often fires while
            # GTK is still on an intermediate `upper` value and we land
            # mid-conversation (or worse, at the top).
            self._finish_bulk_load()
            return False
        # Continue in idle. The vadjustment::changed handler is short-
        # circuited by `_bulk_loading` until we're done.
        self._queue_idle(self._render_batch, iterator, token, False)
        return False

    def _finish_bulk_load(self) -> None:
        """Leave bulk-load mode, then drain anything that arrived during it.

        The drain is not optional. `append_new_from_disk` returns 0 while
        `_bulk_loading` is set, and its only caller — `_flush_follow` in
        main_window — is a one-shot GLib timeout that re-arms only on the NEXT
        `Gio.FileMonitor::changed`. So a record written while the bulk render
        was still going had its wakeup consumed and discarded: if it was the
        last write of the turn, the transcript stayed permanently short until
        the user reselected the session.

        That window is the normal case, not an edge: `_start_following` installs
        the monitor before `set_session` runs, and this load is slow enough to
        need a spinner and a hidden list, i.e. routinely longer than the 300ms
        follow debounce.
        """
        self._bulk_loading = False
        self.append_new_from_disk()
        if self._bulk_loading:
            # The drain found `restarted` and re-entered set_session, which
            # started a REPLACEMENT load and set the flag again. This call is
            # now finishing an obsolete render: landing-scroll it and we scroll
            # against a list that is still batching, and clear feedback the new
            # load is relying on. The new load owns its own landing scroll.
            return
        self._start_landing_scroll()

    def _prepend_load_earlier_row(self) -> None:
        if self._load_earlier_row is not None:
            try:
                self._remove_content_child(self._load_earlier_row)
            except Exception:
                pass
        self._load_earlier_row = _LoadEarlierRow(self._on_load_earlier)
        self._list.insert_child_after(self._load_earlier_row, None)

    def _on_load_earlier(self) -> None:
        if self._destroyed or self._session is None or self._history_start_index <= 0:
            return
        old_row = self._load_earlier_row
        if old_row is not None:
            try:
                self._remove_content_child(old_row)
            except Exception:
                pass
            self._load_earlier_row = None

        new_start = max(0, self._history_start_index - OLDER_TURN_BATCH)
        try:
            turns = list(iter_transcript(self._session.path))
        except Exception:
            turns = []
        new_start = _aligned_start(turns, new_start)
        slice_end = min(self._history_start_index, len(turns))
        older = turns[new_start:slice_end]
        prev: Gtk.Widget | None = None
        if new_start > 0:
            self._prepend_load_earlier_row()
            prev = self._load_earlier_row
        # Pair inside this batch only. These turns all precede what is
        # already rendered, so the whole-list scan would attach an old result
        # to a newer card — and without pairing at all, every result in the
        # batch fell back to the standalone row this release exists to remove.
        batch: list[MessageBubble] = []
        for turn in older:
            if turn.role == "tool" and turn.tool_results and not turn.tool_uses:
                turn = self._pair_tool_results(turn, batch)
                if turn is None:
                    continue
            bubble = MessageBubble(turn, assistant_label=self._assistant_label)
            self._list.insert_child_after(bubble, prev)
            prev = bubble
            batch.append(bubble)
        self._history_start_index = new_start


class _QueuedRow(Gtk.Box):
    """A user message waiting its turn — dashed user-style bubble with a
    "Queued" pill and a ✕ to pull it back out of the queue."""

    def __init__(self, qid: int, text: str, on_remove) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("helios-bubble")
        self.add_css_class("helios-bubble-user")
        self.add_css_class("helios-bubble-queued")
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(10)
        header.set_margin_top(2)
        you = Gtk.Label(label="You", xalign=0)
        you.add_css_class("caption-heading")
        you.add_css_class("helios-role-user")
        header.append(you)
        pill = Gtk.Label(label="Queued")
        pill.add_css_class("helios-queued-pill")
        pill.set_valign(Gtk.Align.CENTER)
        header.append(pill)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)

        remove_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        remove_btn.set_tooltip_text("Remove from queue")
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("circular")
        remove_btn.add_css_class("helios-queued-remove")
        remove_btn.set_margin_end(6)
        remove_btn.connect("clicked", lambda *_: on_remove(qid))
        header.append(remove_btn)
        self.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.set_margin_start(10)
        body.set_margin_end(10)
        body.set_margin_top(10)
        body.set_margin_bottom(10)
        body.add_css_class("helios-bubble-body")
        body.add_css_class("helios-bubble-body-user")
        label = Gtk.Label(label=text, xalign=0)
        label.set_wrap(True)
        label.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
        label.set_selectable(True)
        label.set_margin_start(10)
        label.set_margin_end(10)
        label.set_margin_top(10)
        label.set_margin_bottom(10)
        body.append(label)
        self.append(body)


class _LoadEarlierRow(Gtk.Box):
    def __init__(self, on_click) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_halign(Gtk.Align.CENTER)
        self.set_margin_top(4)
        self.set_margin_bottom(10)
        btn = Gtk.Button(label="Load earlier")
        btn.add_css_class("flat")
        btn.set_tooltip_text("Load older turns from this session")
        btn.connect("clicked", lambda *_: on_click())
        self.append(btn)


class _Banner(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("helios-banner")
        self.set_margin_top(12)
        self.set_margin_bottom(8)
        self.set_margin_start(20)
        self.set_margin_end(20)

        self._title = Gtk.Label(xalign=0)
        self._title.add_css_class("title-3")
        self._title.set_ellipsize(3)
        self.append(self._title)

        sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._sub_cwd = Gtk.Label(xalign=0)
        self._sub_cwd.add_css_class("caption")
        self._sub_cwd.add_css_class("dim-label")
        self._sub_id = Gtk.Label(xalign=0)
        self._sub_id.add_css_class("caption")
        self._sub_id.add_css_class("dim-label")
        sub.append(self._sub_cwd)
        sub.append(self._sub_id)
        self.append(sub)

    def set_session(self, s: Session) -> None:
        title = s.ensure_title()
        self._title.set_label(title)
        self._sub_cwd.set_label(s.project.cwd)
        self._sub_id.set_label(f"session {s.session_id}")

    def set_live(self, *, cwd: str, model: str) -> None:
        self._title.set_label("New chat")
        self._sub_cwd.set_label(cwd)
        self._sub_id.set_label(model or "default model")

    def clear(self) -> None:
        self._title.set_label("")
        self._sub_cwd.set_label("")
        self._sub_id.set_label("")
