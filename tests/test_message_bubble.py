"""Tests for message_bubble's per-call cards and activity-group batching.

Every test here needs gi: message_bubble imports it at module import time, so
the whole file skips on the slim CI image. The pairing logic itself is GTK-free
and lives in tests/test_transcript_activity.py, which does run there.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.transcript import ToolResult, ToolUse, Turn


def _make_turn(n_uses: int, n_results: int) -> Turn:
    """Build a Turn with n_uses ToolUses and n_results ToolResults."""
    turn = Turn(role="assistant")
    for i in range(n_uses):
        turn.tool_uses.append(ToolUse(name=f"tool_{i}", input={"i": i}, id=f"u{i}"))
    for j in range(n_results):
        turn.tool_results.append(
            ToolResult(tool_use_id=f"u{j}", content=f"result {j}", is_error=False)
        )
    return turn


def test_copy_text_prefers_final_answer():
    """Copy button text is the final answer, not the thinking/commentary."""
    from helios.widgets.message_bubble import _message_copy_text
    from helios.backend.transcript import ContentSpan

    turn = Turn(role="assistant")
    turn.content.extend(
        [
            ContentSpan("commentary", "WORK"),
            ContentSpan("text", "ANSWER-ONE"),
            ContentSpan("text", "ANSWER-TWO"),
        ]
    )
    assert _message_copy_text(turn) == "ANSWER-ONE\n\nANSWER-TWO"


def test_copy_text_falls_back_when_no_final_answer():
    """A commentary-only message still copies something (never empty)."""
    from helios.widgets.message_bubble import _message_copy_text
    from helios.backend.transcript import ContentSpan

    turn = Turn(role="assistant")
    turn.content.append(ContentSpan("commentary", "ONLY-WORK-UPDATE"))
    assert _message_copy_text(turn) == "ONLY-WORK-UPDATE"


# ---------------------------------------------------------------------------
# GTK tests — skipped when gi / display unavailable
# ---------------------------------------------------------------------------

try:
    import gi  # noqa: F401

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk  # noqa: F401

    _GTK_AVAILABLE = True
except Exception:
    _GTK_AVAILABLE = False

_skip_gtk = pytest.mark.skipif(not _GTK_AVAILABLE, reason="GTK4/gi not available")


def _count_box_children(box) -> int:
    """Count direct children of a Gtk.Box."""
    n = 0
    child = box.get_first_child()
    while child is not None:
        n += 1
        child = child.get_next_sibling()
    return n


def _direct_children(box) -> list[object]:
    children: list[object] = []
    child = box.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    return children


def _widget_text(widget) -> str:
    """Collect visible label/text-view text below a GTK widget."""
    parts: list[str] = []

    def walk(current) -> None:
        if isinstance(current, Gtk.Label):
            parts.append(current.get_text())
        elif isinstance(current, Gtk.TextView):
            buf = current.get_buffer()
            parts.append(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        child = current.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(widget)
    return "\n".join(parts)


def _pump_idle(max_iterations: int = 10_000) -> int:
    """Drain pending GLib idle callbacks; return iterations consumed."""
    ctx = GLib.MainContext.default()
    i = 0
    while ctx.pending() and i < max_iterations:
        ctx.iteration(may_block=False)
        i += 1
    return i


@_skip_gtk
def test_message_bubble_content_lanes_render_in_distinct_surfaces():
    """Final commentary is grouped separately while the answer stays visible."""
    from helios.widgets.message_bubble import MessageBubble

    turn = Turn(role="assistant")
    turn.reasoning_summary_parts.append("PUBLIC-REASONING")
    turn.thinking_parts.append("LEGACY-THINKING")
    turn.commentary_parts.append("WORK-UPDATE-TEXT")
    turn.text_parts.append("FINAL-ANSWER-TEXT")

    bubble = MessageBubble(turn)

    expanders: list[tuple[str, str]] = []  # (label, body text)
    captions: list[str] = []

    def _body_text(tv) -> str:
        buf = tv.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def walk(w) -> None:
        if isinstance(w, Gtk.Expander):
            child = w.get_child()
            body = _body_text(child) if isinstance(child, Gtk.TextView) else ""
            expanders.append((w.get_label(), body))
        elif isinstance(w, Gtk.Label):
            captions.append(w.get_text())
        c = w.get_first_child()
        while c is not None:
            walk(c)
            c = c.get_next_sibling()

    walk(bubble)

    labels = {label for label, _ in expanders}
    assert "Reasoning summary" in labels
    assert "Thinking" in labels
    # Public reasoning + legacy thinking live in their own collapsibles.
    assert ("Reasoning summary", "PUBLIC-REASONING") in expanders
    assert ("Thinking", "LEGACY-THINKING") in expanders
    # Commentary is its own collapsed public lane, never hidden under Thinking.
    assert "Work updates · 1" in labels
    assert all("WORK-UPDATE-TEXT" not in body for _, body in expanders)
    assert "FINAL-ANSWER-TEXT" in captions

    work_group = next(
        w
        for w in _direct_children(_body_of(bubble))
        if isinstance(w, Gtk.Expander) and w.get_label() == "Work updates · 1"
    )
    assert work_group.get_child() is None  # details are lazy and collapsed
    work_group.set_expanded(True)
    assert "WORK-UPDATE-TEXT" in _widget_text(work_group.get_child())


@_skip_gtk
def test_message_bubble_has_copy_button():
    """A finalized bubble exposes a 'Copy message' button (whole-message copy,
    since drag-selection can't cross the per-block sibling widgets)."""
    from helios.widgets.message_bubble import MessageBubble

    turn = Turn(role="assistant")
    turn.text_parts.append("HELLO")
    bubble = MessageBubble(turn)

    found = []

    def walk(w):
        if isinstance(w, Gtk.Button) and w.get_tooltip_text() == "Copy message":
            found.append(w)
        c = w.get_first_child()
        while c is not None:
            walk(c)
            c = c.get_next_sibling()

    walk(bubble)
    assert len(found) == 1
    # Handler runs without error (clipboard set is a no-op-safe path).
    found[0].emit("clicked")


@_skip_gtk
def test_tool_only_bubble_copy_button_is_inert():
    """A tool-only assistant turn (no prose) has nothing to copy, so its copy
    button is invisible and untargetable — but still ALLOCATED, because that
    button is what holds the header row at the same height as a prose turn's.
    Deliberately not matched on the tooltip: the inert path leaves the tooltip
    unset, so a tooltip walk would find nothing and pass while asserting
    nothing at all."""
    from helios.widgets.message_bubble import MessageBubble, _message_copy_text

    turn = _make_turn(n_uses=2, n_results=1)  # tool activity, zero text spans
    assert _message_copy_text(turn) == ""

    header = MessageBubble(turn).get_first_child()
    found = []
    child = header.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button):
            found.append(child)
        child = child.get_next_sibling()

    assert len(found) == 1, "the copy button must still be allocated"
    btn = found[0]
    assert btn.get_opacity() == 0.0
    assert btn.get_can_target() is False
    assert btn.get_can_focus() is False
    assert btn.get_tooltip_text() is None


@_skip_gtk
def test_bubble_headers_all_measure_the_same_height():
    """The streaming bubble's header and the resting bubble's header are one
    shape, so finalizing a turn moves nothing below it.

    Before this, the live header was a bare caption row (measured 14px) and a
    finalized prose header carried a `.flat .circular` copy button that
    libadwaita floors at 36px — so every prose turn shoved the transcript down
    22px the instant it completed, inside a sticky-bottom scroller.

    "Tool-only" here means an ASSISTANT turn whose content is only tool_uses;
    it has a header and is in scope. The role="tool" turn from D-3 has NO
    header at all (its bubble's first child is the body) and is deliberately
    out of scope — test_tool_result_only_record_renders_without_a_speaker_header
    owns that case. Do not "fix" this test by giving that turn a header back.
    """
    from helios.widgets.message_bubble import MessageBubble, StreamingBubble

    def header_height(bubble) -> int:
        return bubble.get_first_child().measure(Gtk.Orientation.VERTICAL, -1)[1]

    prose = Turn(role="assistant", timestamp="2026-08-18T12:00:00Z")
    prose.text_parts.append("HELLO")
    tool_only = _make_turn(n_uses=2, n_results=1)

    heights = {
        "streaming": header_height(StreamingBubble()),
        "prose": header_height(MessageBubble(prose)),
        "tool_only": header_height(MessageBubble(tool_only)),
    }
    assert len(set(heights.values())) == 1, heights
    # ...and it is the tall shape that wins, not all three collapsed to the
    # bare caption row — which would "pass" by deleting the copy button.
    assert heights["prose"] > 20, heights


@_skip_gtk
def test_streaming_bubble_renders_new_lanes_without_duplication():
    """The live StreamingBubble maps each block to exactly one surface — the
    public reasoning summary collapsible, a visible commentary label, the final
    text — with no duplication when content grows across flushes."""
    from helios.backend.process.streaming import Block, StreamingAssistant
    from helios.widgets.message_bubble import StreamingBubble

    s = StreamingAssistant(model="gpt")
    s.blocks.append(Block(type="reasoning_summary", text="R"))
    s.blocks.append(Block(type="commentary", text="C"))
    s.blocks.append(Block(type="text", text="T"))  # last block = in-progress

    bubble = StreamingBubble()
    bubble.update(s)
    s.blocks[2].text = "TT"  # more of the in-progress answer arrives
    bubble.update(s)

    labels: list[str] = []
    n = 0
    child = bubble._body.get_first_child()
    while child is not None:
        n += 1
        if isinstance(child, Gtk.Expander):
            labels.append(child.get_label())
        child = child.get_next_sibling()

    assert n == 3  # one widget per block — no duplication across flushes
    assert "Reasoning summary" in labels
    assert "Thinking" not in labels  # commentary is never collapsed as thinking


def _surface_sequence(box) -> list[str]:
    """Classify each DIRECT child of a body box, in order, into a surface kind:
    collapsed detail, live work update, activity, or final text."""
    seq: list[str] = []
    child = box.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Expander):
            label = child.get_label()
            if label == "Reasoning summary":
                seq.append("reasoning")
            elif label.startswith("Work updates"):
                seq.append("work_updates")
            elif child.has_css_class("helios-tool-use"):
                seq.append("activity")
            else:
                seq.append("thinking")
        elif isinstance(child, Gtk.Box) and child.has_css_class("helios-commentary"):
            seq.append("work_update")
        else:
            seq.append("final")
        child = child.get_next_sibling()
    return seq


def _body_of(bubble) -> object:
    child = bubble.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Box) and child.has_css_class("helios-bubble-body"):
            return child
        child = child.get_next_sibling()
    raise AssertionError("no body box found")


@_skip_gtk
def test_message_bubble_preserves_interleaved_content_order():
    """Grouped updates occupy their first source position; final stays last."""
    from helios.widgets.message_bubble import MessageBubble
    from helios.backend.transcript import ContentSpan

    turn = Turn(role="assistant")
    turn.content.extend(
        [
            ContentSpan("commentary", "C1"),
            ContentSpan("reasoning_summary", "R1"),
            ContentSpan("commentary", "C2"),
            ContentSpan("text", "F"),
        ]
    )

    bubble = MessageBubble(turn)
    seq = _surface_sequence(_body_of(bubble))
    assert seq == ["work_updates", "reasoning", "final"]
    assert seq.count("work_updates") == 1

    group = _direct_children(_body_of(bubble))[0]
    assert isinstance(group, Gtk.Expander)
    assert group.get_label() == "Work updates · 2"
    assert group.get_child() is None
    group.set_expanded(True)
    text = _widget_text(group.get_child())
    assert text.index("C1") < text.index("C2")


@_skip_gtk
def test_finalized_activity_keeps_first_tool_chronology():
    """Finalized and reloaded bubbles keep commentary -> tool -> answer."""
    from helios.backend.process.streaming import Block, StreamingAssistant
    from helios.backend.transcript import turn_from_record
    from helios.widgets.message_bubble import MessageBubble

    streaming = StreamingAssistant(model="gpt")
    streaming.blocks.extend(
        [
            Block(type="commentary", text="I am checking the repository."),
            Block(
                type="tool_use",
                tool_use_name="Bash",
                tool_use_id="cmd-1",
                tool_use_input_json='{"command":"rg TODO"}',
            ),
            Block(type="text", text="The check is complete."),
        ]
    )

    turn = streaming.to_turn()
    assert turn.activity_content_index == 1
    assert _surface_sequence(_body_of(MessageBubble(turn))) == [
        "work_updates",
        "activity",
        "final",
    ]

    reloaded = turn_from_record(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "commentary", "text": "I am checking."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "id": "cmd-1",
                        "input": {"command": "rg TODO"},
                    },
                    {"type": "text", "text": "The check is complete."},
                ],
            },
        }
    )
    assert reloaded is not None
    assert reloaded.activity_content_index == 1
    assert _surface_sequence(_body_of(MessageBubble(reloaded))) == [
        "work_updates",
        "activity",
        "final",
    ]


@_skip_gtk
def test_streaming_bubble_preserves_interleaved_order_and_identity():
    """H0.5 findings #2 + #4: the live StreamingBubble renders interleaved
    content in source order, active commentary has a stable Work-update
    identity immediately, and finalization neither reorders nor duplicates."""
    from helios.backend.process.streaming import Block, StreamingAssistant
    from helios.widgets.message_bubble import StreamingBubble

    s = StreamingAssistant(model="gpt")
    s.blocks.append(Block(type="commentary", text="C1"))  # active commentary
    bubble = StreamingBubble()
    bubble.update(s)
    # Immediately (before any later block) the active commentary is a labeled
    # Work update, not a bare unlabeled styled label.
    assert _surface_sequence(_body_of(bubble)) == ["work_update"]

    # More content arrives, interleaving lanes; the last stays in progress.
    s.blocks.append(Block(type="reasoning_summary", text="R1"))
    s.blocks.append(Block(type="commentary", text="C2"))
    s.blocks.append(Block(type="text", text="F"))
    bubble.update(s)
    s.blocks[3].text = "F final"  # grow the in-progress answer
    bubble.update(s)

    seq = _surface_sequence(_body_of(bubble))
    assert seq == ["work_update", "reasoning", "work_update", "final"]
    assert seq.count("work_update") == 2  # C1 finalized + C2, no duplicate/merge


@_skip_gtk
def test_streaming_tools_share_one_persistent_compact_expander():
    """Many live commands consume one row whose identity survives updates."""
    from helios.backend.process.streaming import Block, StreamingAssistant
    from helios.widgets.message_bubble import StreamingBubble

    streaming = StreamingAssistant(model="gpt")
    streaming.blocks.extend(
        Block(
            type="tool_use",
            tool_use_name="Bash",
            tool_use_id=f"cmd-{i}",
            tool_use_input_json=f'{{"command":"echo {i}"}}',
        )
        for i in range(70)
    )

    bubble = StreamingBubble()
    bubble.update(streaming)
    children = _direct_children(bubble._body)
    assert len(children) == 1
    expander = children[0]
    assert isinstance(expander, Gtk.Expander)
    assert expander.get_label() == "Working · 70 shell commands"
    assert expander.get_child() is None  # command widgets are lazy

    streaming.blocks.append(
        Block(
            type="tool_use",
            tool_use_name="Read",
            tool_use_id="read-1",
            tool_use_input_json='{"file_path":"README.md"}',
        )
    )
    bubble.update(streaming)
    children = _direct_children(bubble._body)
    assert children == [expander]
    assert expander.get_label() == "Working · 70 shell commands · 1 other action"


@_skip_gtk
def test_streaming_activity_shutdown_cancels_pending_detail_build():
    """A detached live bubble cannot retain a batched GTK idle chain."""
    from helios.backend.process.streaming import Block, StreamingAssistant
    from helios.widgets.message_bubble import StreamingBubble

    streaming = StreamingAssistant(model="gpt")
    streaming.blocks.extend(
        Block(type="tool_use", tool_use_name="Bash", tool_use_id=f"cmd-{i}")
        for i in range(70)
    )
    bubble = StreamingBubble()
    bubble.update(streaming)
    expander = _direct_children(bubble._body)[0]
    expander.set_expanded(True)
    assert expander._activity_source_ids

    bubble.shutdown()

    assert bubble._destroyed is True
    assert expander._activity_build_closed is True
    assert expander._activity_source_ids == set()
    assert _count_box_children(expander.get_child()) < 70
    bubble.update(streaming)  # inert after teardown


@_skip_gtk
def test_final_activity_summary_counts_invocations_not_results():
    """Results remain readable details but never double the action count."""
    from helios.widgets.message_bubble import _activity_group

    turn = Turn(role="assistant")
    turn.tool_uses.extend(
        [
            ToolUse(name="Bash", input={"command": "one"}, id="b1"),
            ToolUse(name="Bash", input={"command": "two"}, id="b2"),
            ToolUse(name="Read", input={"file_path": "README.md"}, id="r1"),
        ]
    )
    turn.tool_results.extend(
        ToolResult(tool_use_id=tool.id, content="ok") for tool in turn.tool_uses
    )

    expander = _activity_group(turn)
    assert expander.get_label() == "Ran 2 shell commands · 1 other action"
    assert expander.get_child() is None
    expander.set_expanded(True)
    # One card per call, with the answering result inside it — not six rows.
    assert _count_box_children(expander.get_child()) == 3


@_skip_gtk
def test_final_non_shell_activity_uses_tool_action_wording():
    from helios.widgets.message_bubble import _activity_group

    turn = Turn(role="assistant")
    turn.tool_uses.extend(
        ToolUse(name="Read", input={"file_path": str(i)}) for i in range(2)
    )
    turn.tool_results.extend(
        ToolResult(tool_use_id="", content="ok") for _ in range(20)
    )

    expander = _activity_group(turn)
    assert expander.get_label() == "Ran 2 tool actions"


@_skip_gtk
def test_small_group_no_idle_scheduled():
    """A group with <= _ACTIVITY_FIRST_BATCH items: body complete after expand,
    no idle callbacks needed."""
    from helios.widgets.message_bubble import (
        _ACTIVITY_FIRST_BATCH,
        _activity_cards,
        _activity_group,
        _activity_items,
    )

    turn = _make_turn(n_uses=5, n_results=5)  # 5 answered calls -> 5 cards
    assert len(_activity_cards(_activity_items(turn))) <= _ACTIVITY_FIRST_BATCH

    expander = _activity_group(turn)
    assert expander.get_label() == "Ran 5 tool actions"
    assert expander.get_child() is None  # body not built yet

    expander.set_expanded(True)

    body = expander.get_child()
    assert body is not None
    assert _count_box_children(body) == 5  # all 5 built synchronously


@_skip_gtk
def test_large_group_incremental_build():
    """A group with 60 items: expand → first 20 built synchronously, then idle
    callbacks complete the remaining 40."""
    from helios.widgets.message_bubble import (
        _ACTIVITY_FIRST_BATCH,
        _activity_group,
    )

    N = 60
    turn = _make_turn(n_uses=N, n_results=0)

    expander = _activity_group(turn)
    expander.set_expanded(True)

    body = expander.get_child()
    assert body is not None

    # Immediately after expand: first batch only
    after_expand = _count_box_children(body)
    assert after_expand == min(_ACTIVITY_FIRST_BATCH, N)

    # Pump idle until no more pending work
    _pump_idle()

    # After draining: all widgets present
    assert _count_box_children(body) == N


@_skip_gtk
def test_large_group_mixed_uses_and_results():
    """35 calls, 25 of them answered — 35 cards present after idle drain."""
    from helios.widgets.message_bubble import _activity_group

    turn = _make_turn(n_uses=35, n_results=25)
    expander = _activity_group(turn)
    expander.set_expanded(True)

    _pump_idle()

    body = expander.get_child()
    assert body is not None
    assert _count_box_children(body) == 35


@_skip_gtk
def test_collapse_mid_build_no_crash():
    """Collapsing the expander while idle callbacks are still queued must not
    crash and must not keep appending to the (now-orphaned) box."""
    from helios.widgets.message_bubble import (
        _ACTIVITY_FIRST_BATCH,
        _activity_group,
    )

    N = 100
    turn = _make_turn(n_uses=N, n_results=0)
    expander = _activity_group(turn)
    expander.set_expanded(True)

    body = expander.get_child()
    after_first = _count_box_children(body)
    assert after_first == _ACTIVITY_FIRST_BATCH

    # Collapse before idle callbacks fire
    expander.set_expanded(False)

    # Pump — callbacks should abort cleanly, no crash
    _pump_idle()

    # The body widget is still the same object but build stopped
    count_after_collapse = _count_box_children(body)
    assert count_after_collapse < N  # didn't finish building


@_skip_gtk
def test_reexpand_after_collapse_completes_build():
    """Collapsing mid-build then re-expanding must rebuild the group in FULL —
    the earlier bug left a partial child that short-circuited the expand
    handler, permanently truncating the group."""
    from helios.widgets.message_bubble import _activity_group

    N = 100
    turn = _make_turn(n_uses=N, n_results=0)
    expander = _activity_group(turn)

    expander.set_expanded(True)
    expander.set_expanded(False)  # collapse before the idle chain finishes
    _pump_idle()
    assert expander.get_child() is None  # partial child was dropped

    # Re-expand and let it finish — every action must now be present.
    expander.set_expanded(True)
    _pump_idle()
    body = expander.get_child()
    assert body is not None
    assert _count_box_children(body) == N


@_skip_gtk
def test_tool_result_only_record_renders_without_a_speaker_header():
    """End-to-end wiring: turn_from_record retags a tool-result-only record and
    MessageBubble drops its header, so the row carries no "You" label and no
    per-row timestamp — just its activity expander."""
    from helios.backend.transcript import turn_from_record
    from helios.widgets.message_bubble import MessageBubble

    turn = turn_from_record(
        {
            "type": "user",
            "timestamp": "2026-08-17T12:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}
                ],
            },
        }
    )
    bubble = MessageBubble(turn)

    # No header box at all: the bubble's first child IS the body. Asserting the
    # absence of the "You" string is not enough — an unsuppressed header would
    # simply read "Tool" and sail past it.
    first = bubble.get_first_child()
    assert first is not None
    assert first.has_css_class(
        "helios-bubble-body"
    ), "tool-result-only bubble grew a header row"
    # ...and the body still has its activity group. Without this the row could
    # render as an empty 20px box -- header correctly suppressed, content gone --
    # and the assertion above would still pass.
    assert (
        _count_box_children(first) == 1
    ), "the tool-result expander must survive the header suppression"


# ---------------------------------------------------------------------------
# T-3/T-4: one card per call, its state chip, and its diff
# ---------------------------------------------------------------------------


def _chip_text(card) -> str:
    """The state chip is the last label in a card's header widget."""
    header = card.get_label_widget()
    return header.get_last_child().get_text()


@_skip_gtk
def test_card_reads_running_until_its_result_is_attached():
    from helios.widgets.message_bubble import _tool_use_widget

    tu = ToolUse(name="Bash", input={"command": "make test"}, id="b1")

    assert _chip_text(_tool_use_widget(tu)) == "running"
    assert _chip_text(_tool_use_widget(tu, [ToolResult("b1", "ok")])) == "done"
    assert _chip_text(_tool_use_widget(tu, [], "1.4s")) == "running · 1.4s"


@_skip_gtk
def test_a_failed_card_says_error_without_being_expanded():
    from helios.widgets.message_bubble import _tool_use_widget

    tu = ToolUse(name="Bash", input={"command": "false"}, id="b1")
    card = _tool_use_widget(tu, [ToolResult("b1", "boom", is_error=True)])

    assert _chip_text(card) == "error"
    assert card.has_css_class("helios-tool-error")
    assert not card.get_expanded()


@_skip_gtk
def test_an_answered_question_is_not_a_failed_card():
    """Answering an AskUserQuestion arrives as an is_error tool_result."""
    from helios.backend.transcript import QUESTION_ANSWERED_MESSAGE
    from helios.widgets.message_bubble import _tool_use_widget

    tu = ToolUse(name="AskUserQuestion", input={}, id="q1")
    card = _tool_use_widget(
        tu, [ToolResult("q1", QUESTION_ANSWERED_MESSAGE, is_error=True)]
    )

    assert _chip_text(card) == "done"


@_skip_gtk
def test_a_card_names_its_tool_and_state_for_assistive_tech():
    """The chip is a bare word beside an icon, so the card carries the whole
    sentence as its accessible name. GTK exposes no getter for a property's
    value, so the composed string is asserted at its source."""
    from helios.widgets.message_bubble import _card_accessible_label, _tool_use_widget

    tu = ToolUse(name="Read", input={"file_path": "a.py"}, id="r1")
    card = _tool_use_widget(tu)

    assert Gtk.test_accessible_has_property(card, Gtk.AccessibleProperty.LABEL)
    assert _card_accessible_label(tu, "running", "") == "Read tool call, running"
    assert _card_accessible_label(tu, "error", "2.0s") == "Read tool call, error, 2.0s"


@_skip_gtk
def test_an_edit_renders_as_a_diff_and_a_codex_edit_does_not():
    """The Codex fallback is the important half: that mirror carries only a
    file_path, and it must keep the JSON view rather than show an empty diff."""
    from helios.widgets.code_block import CodeBlock
    from helios.widgets.message_bubble import _tool_use_widget

    edit = _tool_use_widget(
        ToolUse(
            name="Edit",
            input={"file_path": "a.py", "old_string": "one", "new_string": "two"},
            id="e1",
        )
    )
    codex = _tool_use_widget(ToolUse(name="Edit", input={"file_path": "a.py"}, id="e2"))

    assert isinstance(edit.get_child().get_first_child(), CodeBlock)
    assert isinstance(codex.get_child().get_first_child(), Gtk.TextView)
    edit.set_expanded(True)  # a collapsed expander keeps its child out of the tree
    assert "-one" in _widget_text(edit) and "+two" in _widget_text(edit)


@_skip_gtk
def test_a_result_is_reachable_in_full_past_the_preview_cut():
    from helios.widgets.message_bubble import MAX_TOOL_PREVIEW, _tool_result_widget

    body = "x" * (MAX_TOOL_PREVIEW * 4 + 500)
    row = _tool_result_widget(ToolResult("b1", body), "Bash")
    row.set_expanded(True)  # a collapsed expander keeps its child out of the tree
    button = row.get_child().get_last_child()

    assert row.get_label().startswith("↳ Bash result")
    assert "… (truncated)" in _widget_text(row)
    assert isinstance(button, Gtk.Button)
    assert button.get_label() == f"Show full output ({len(body):,} chars)"

    button.emit("clicked")

    assert "… (truncated)" not in _widget_text(row)
    assert not isinstance(row.get_child().get_last_child(), Gtk.Button)


@_skip_gtk
def test_the_group_label_counts_errors():
    from helios.widgets.message_bubble import _activity_group

    turn = Turn(role="assistant")
    turn.tool_uses.extend(
        ToolUse(name="Bash", input={"command": str(i)}, id=f"b{i}") for i in range(4)
    )
    turn.tool_results.append(ToolResult("b2", "boom", is_error=True))

    assert _activity_group(turn).get_label() == "Ran 4 shell commands · 1 error"


@_skip_gtk
def test_files_changed_lists_the_paths_a_turn_touched():
    from helios.widgets.message_bubble import _files_changed_label

    uses = [
        ToolUse(name="Write", input={"file_path": "/tmp/a.py", "content": ""}, id="1"),
        ToolUse(name="Edit", input={"file_path": "/tmp/a.py"}, id="2"),  # deduped
        ToolUse(name="NotebookEdit", input={"notebook_path": "/tmp/nb.ipynb"}, id="3"),
        ToolUse(name="Bash", input={"command": "ls"}, id="4"),  # not an edit
    ]

    assert _files_changed_label(uses).get_text() == "Files changed: a.py, nb.ipynb"
    assert _files_changed_label(uses[3:]) is None


@_skip_gtk
def test_files_changed_stops_at_six_names():
    from helios.widgets.message_bubble import _files_changed_label

    uses = [
        ToolUse(name="Write", input={"file_path": f"/tmp/f{i}.py", "content": ""})
        for i in range(9)
    ]

    assert _files_changed_label(uses).get_text().endswith("f5.py +3")


@_skip_gtk
def test_attach_tool_result_resolves_a_card_delivered_in_a_later_record():
    """The pairing that spans two bubbles: the assistant record carries the
    call, a later user record carries its output."""
    from helios.widgets.message_bubble import MessageBubble

    turn = Turn(role="assistant", timestamp="2026-08-17T12:00:00Z")
    turn.tool_uses.append(ToolUse(name="Bash", input={"command": "ls"}, id="b1"))
    bubble = MessageBubble(turn)
    group = bubble._activity_expander
    group.set_expanded(True)
    assert _chip_text(group.get_child().get_first_child()) == "running"

    assert bubble.attach_tool_result(ToolResult("b1", "ok"), "2026-08-17T12:00:03Z")

    card = group.get_child().get_first_child()
    assert _chip_text(card) == "done · 3.0s"
    card.set_expanded(True)
    assert "↳ Bash result  ·  ok" in _widget_text(card)
    # The shared Turn is NOT mutated — plan_pane and context_breakdown hold it.
    assert turn.tool_results == []


@_skip_gtk
def test_attach_tool_result_refuses_a_call_this_bubble_does_not_own():
    from helios.widgets.message_bubble import MessageBubble

    turn = Turn(role="assistant")
    turn.tool_uses.append(ToolUse(name="Bash", input={"command": "ls"}, id="b1"))
    bubble = MessageBubble(turn)

    assert not bubble.attach_tool_result(ToolResult("other", "ok"))
    assert not bubble.attach_tool_result(ToolResult("", "ok"))
    assert bubble.attach_tool_result(ToolResult("b1", "ok"))
    # Already resolved: a second result for the same id belongs to a later call.
    assert not bubble.attach_tool_result(ToolResult("b1", "again"))


@_skip_gtk
def test_an_attached_error_repaints_the_group_label():
    from helios.widgets.message_bubble import MessageBubble

    turn = Turn(role="assistant")
    turn.tool_uses.append(ToolUse(name="Bash", input={"command": "false"}, id="b1"))
    bubble = MessageBubble(turn)

    bubble.attach_tool_result(ToolResult("b1", "boom", is_error=True))

    assert bubble._activity_expander.get_label() == "Ran 1 shell command · 1 error"


# ---------------------------------------------------------------------------
# T-1: the live text block renders its settled Markdown prefix
# ---------------------------------------------------------------------------


def _start_text(index: int) -> dict:
    return {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "text", "text": ""},
    }


def _text_delta(text: str, index: int = 0) -> dict:
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }


def _streaming_text(*chunks: str):
    """A StreamingBubble fed one text block per chunk, updated once."""
    from helios.backend.process.streaming import StreamingAssistant
    from helios.widgets.message_bubble import StreamingBubble

    s = StreamingAssistant()
    for i, chunk in enumerate(chunks):
        s.apply_stream_event(_start_text(i))
        s.apply_stream_event(_text_delta(chunk, i))
    bubble = StreamingBubble()
    bubble.update(s)
    return s, bubble


@_skip_gtk
def test_settled_paragraph_is_rendered_markdown_before_message_stop():
    """The T-1 point: no raw text left waiting for the finalize snap."""
    s, bubble = _streaming_text("# Title\n\nStill typing")

    seg = bubble._segments[0]
    children = _direct_children(seg.widget)
    assert len(children) == 2  # one rendered chunk + the live tail label
    assert isinstance(children[0], Gtk.Box)  # markdown, not a bare label
    assert "Title" in _widget_text(children[0])
    assert children[1].get_text() == "Still typing"
    assert seg.stable_len == len("# Title\n\n")


@_skip_gtk
def test_an_unsettled_first_paragraph_stays_in_the_tail_label():
    """The documented ceiling — nothing renders until a blank line lands."""
    s, bubble = _streaming_text("One long paragraph, no blank line yet")

    children = _direct_children(bubble._segments[0].widget)
    assert len(children) == 1
    assert children[0].get_text() == "One long paragraph, no blank line yet"


@_skip_gtk
def test_finalize_does_not_rebuild_the_chunks_already_rendered():
    s, bubble = _streaming_text("First para.\n\nsecond, unsettled")
    seg = bubble._segments[0]
    chunk = _direct_children(seg.widget)[0]

    # A second block starting means block 0 is complete: it finalizes.
    s.apply_stream_event(_start_text(1))
    s.apply_stream_event(_text_delta("next", 1))
    bubble.update(s)

    children = _direct_children(seg.widget)
    assert children[0] is chunk  # parsed once, never re-parsed
    assert len(children) == 2  # the tail label became its own render
    assert seg.label is None
    assert _widget_text(seg.widget).count("First para.") == 1


@_skip_gtk
def test_a_whitespace_only_text_block_leaves_nothing_behind():
    """Settled whitespace parses to nothing: rendering it would leave an empty
    box holding the body's spacing open, where the old code dropped the block."""
    s, bubble = _streaming_text("\n\n  \n")
    assert len(_direct_children(bubble._segments[0].widget)) == 1  # tail label only

    s.apply_stream_event(_start_text(1))
    s.apply_stream_event(_text_delta("real answer", 1))
    bubble.update(s)

    assert bubble._segments[0].widget is None
    assert _direct_children(bubble._body) == [bubble._segments[1].widget]


@_skip_gtk
def test_files_changed_lists_only_edits_that_succeeded():
    """A review finding: built from tool_uses alone the line appeared while a call
    was still unresolved and survived a denied or errored edit."""
    turn = Turn(
        role="assistant",
        timestamp="2026-09-03T12:00:00Z",
        tool_uses=[
            ToolUse(name="Write", input={"file_path": "/x/kept.py", "content": "a"}, id="w1"),
            ToolUse(name="Write", input={"file_path": "/x/denied.py", "content": "b"}, id="w2"),
        ],
    )
    from helios.widgets.message_bubble import MessageBubble

    bubble = MessageBubble(turn)

    def line():
        label = bubble._files_label
        return label.get_label() if label is not None else None

    assert line() is None, "nothing has landed yet"

    bubble.attach_tool_result(ToolResult(tool_use_id="w2", content="denied", is_error=True))
    assert line() is None, "a failed edit changed no file"

    bubble.attach_tool_result(ToolResult(tool_use_id="w1", content="File created"))
    assert line() == "Files changed: kept.py"
