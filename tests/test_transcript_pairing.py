"""A tool_result must reach the bubble that made the call, not a row of its own.

The CLI writes a tool_result as its own user-role record after the assistant
record carrying the tool_use, so the pair always spans two Turns. Rendering
them independently is what produced a transcript of "Ran 4 shell commands"
followed by five identical "Tool results" rows. These tests drive the real
`TranscriptView` entry points — `append_turn` for the live path, `set_session`
for a reload — because the routing IS the behaviour under test.
"""

from __future__ import annotations

import json
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from helios.backend.projects import Project, Session  # noqa: E402
from helios.backend.transcript import ToolResult, ToolUse, Turn  # noqa: E402
from helios.widgets.message_bubble import MessageBubble  # noqa: E402
from helios.widgets.transcript_view import TranscriptView  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()


def _bubbles(view: TranscriptView) -> list[MessageBubble]:
    found: list[MessageBubble] = []
    child = view._list.get_first_child()
    while child is not None:
        if isinstance(child, MessageBubble):
            found.append(child)
        child = child.get_next_sibling()
    return found


def _call_turn(*ids: str) -> Turn:
    turn = Turn(role="assistant", timestamp="2026-08-17T12:00:00Z")
    turn.tool_uses.extend(
        ToolUse(name="Bash", input={"command": f"echo {i}"}, id=i) for i in ids
    )
    return turn


def _result_turn(*results: ToolResult) -> Turn:
    return Turn(
        role="tool", tool_results=list(results), timestamp="2026-08-17T12:00:02Z"
    )


def _card_states(bubble: MessageBubble) -> list[str]:
    group = bubble._activity_expander
    group.set_expanded(True)
    states: list[str] = []
    card = group.get_child().get_first_child()
    while card is not None:
        label = card.get_label_widget()
        if label is not None:
            states.append(label.get_last_child().get_text())
        card = card.get_next_sibling()
    return states


def test_a_result_lands_on_the_call_that_made_it() -> None:
    view = TranscriptView()
    view.append_turn(_call_turn("b1"))

    view.append_turn(_result_turn(ToolResult("b1", "ok")))

    assert len(_bubbles(view)) == 1, "the result opened a standalone bubble"
    assert _card_states(_bubbles(view)[0]) == ["done · 2.0s"]


def test_a_result_nothing_asked_for_keeps_its_own_row() -> None:
    """The fallback has to stay: an id matching no call is still output the
    user needs to see."""
    view = TranscriptView()
    view.append_turn(_call_turn("b1"))

    view.append_turn(_result_turn(ToolResult("nobody", "orphan output")))

    assert len(_bubbles(view)) == 2


def test_only_the_unmatched_half_of_a_record_falls_back() -> None:
    view = TranscriptView()
    view.append_turn(_call_turn("b1"))

    view.append_turn(
        _result_turn(ToolResult("b1", "mine"), ToolResult("nobody", "orphan"))
    )

    bubbles = _bubbles(view)
    assert len(bubbles) == 2
    assert _card_states(bubbles[0]) == ["done · 2.0s"]
    assert [tr.tool_use_id for tr in bubbles[1]._turn.tool_results] == ["nobody"]


def test_the_most_recent_unresolved_call_claims_the_result() -> None:
    """Two turns call the same id; the result belongs to the later one."""
    view = TranscriptView()
    view.append_turn(_call_turn("b1"))
    view.append_turn(_result_turn(ToolResult("b1", "first")))
    view.append_turn(_call_turn("b1"))

    view.append_turn(_result_turn(ToolResult("b1", "second")))

    bubbles = _bubbles(view)
    assert len(bubbles) == 2
    assert bubbles[0]._attached[0].content == "first"
    assert bubbles[1]._attached[0].content == "second"


def _session(tmp_path, *records: dict) -> Session:
    path = tmp_path / "s.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return Session(
        project=Project(dirname="-tmp-proj", cwd=str(tmp_path), path=tmp_path),
        session_id="s",
        path=path,
        mtime=time.time(),
        size=path.stat().st_size,
    )


def test_a_reload_pairs_exactly_like_the_live_path(tmp_path) -> None:
    """`set_session` renders through `_render_batch`, not `append_turn` — the
    pairing has to live below both or a restart un-pairs the transcript."""
    session = _session(
        tmp_path,
        {
            "type": "assistant",
            "timestamp": "2026-08-17T12:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "b1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-08-17T12:00:04Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "b1", "content": "ok"}
                ],
            },
        },
    )

    view = TranscriptView()
    view.set_session(session)

    assert len(_bubbles(view)) == 1
    assert _card_states(_bubbles(view)[0]) == ["done · 4.0s"]


def test_load_earlier_pairs_within_its_own_batch(tmp_path) -> None:
    """Regression found on the live v0.88.0 app: `_on_load_earlier` built its
    bubbles directly, so every older result rendered as the standalone "Tool
    results" row this release exists to remove. Pairing must also stay inside
    the batch — these turns precede everything on screen, so a whole-list scan
    would hand an old result to a newer card with the same id.
    """
    call = {
        "type": "assistant",
        "timestamp": "2026-08-17T11:00:00Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "dup", "name": "Bash", "input": {"command": "old"}}
            ],
        },
    }
    result = {
        "type": "user",
        "timestamp": "2026-08-17T11:00:02Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "dup", "content": "old output"}],
        },
    }
    orphan = {
        "type": "user",
        "timestamp": "2026-08-17T11:00:03Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "gone", "content": "no call"}],
        },
    }
    session = _session(tmp_path, call, result, orphan)

    view = TranscriptView()
    # Already on screen: a NEWER call carrying the same id, still unresolved.
    view.append_turn(_call_turn("dup"))
    view._session = session
    view._history_start_index = 3
    view._on_load_earlier()

    bubbles = _bubbles(view)
    standalone = [
        b
        for b in bubbles
        if list(getattr(b._turn, "tool_results", None) or [])
        and not list(getattr(b._turn, "tool_uses", None) or [])
    ]
    assert len(standalone) == 1, "only the result with no call keeps its own row"
    assert standalone[0]._turn.tool_results[0].tool_use_id == "gone"
    # The newer on-screen card must not have been resolved by the old result.
    newest = bubbles[-1]
    assert newest._turn.tool_uses[0].id == "dup"
    assert _card_states(newest) == ["running"]


def test_a_batch_cut_never_splits_a_call_from_its_results(tmp_path) -> None:
    """Measured on a real 506-turn session: three of five Load-earlier cuts
    landed on a results-only turn, leaving its call in the next batch where
    batch-scoped pairing cannot see it. The cut must move back onto the call.
    """
    from helios.widgets import transcript_view as tv

    def call(i):
        return {
            "type": "assistant",
            "timestamp": f"2026-08-17T11:{i:02d}:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": f"c{i}", "name": "Bash", "input": {"command": str(i)}}
                ],
            },
        }

    def result(i):
        return {
            "type": "user",
            "timestamp": f"2026-08-17T11:{i:02d}:02Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"c{i}", "content": "ok"}],
            },
        }

    records = []
    for i in range(6):
        records += [call(i), result(i)]
    session = _session(tmp_path, *records)  # 12 turns: c0 r0 c1 r1 ... c5 r5
    turns = list(tv.iter_transcript(session.path))
    # A cut at index 3 lands on r1; the aligned cut is c1 at index 2.
    assert tv._aligned_start(turns, 3) == 2
    assert tv._aligned_start(turns, 2) == 2
    assert tv._aligned_start(turns, 0) == 0

    view = TranscriptView()
    view._session = session
    # Tail starts on r4 (index 9) — a split — then one Load-earlier batch of 3
    # would start on r2 (index 5): another split. Both must self-align.
    view._history_start_index = tv._aligned_start(turns, 9)
    assert view._history_start_index == 8
    for turn in turns[view._history_start_index:]:
        view._append_turn_bubble(turn)
    tv.OLDER_TURN_BATCH, saved = 3, tv.OLDER_TURN_BATCH
    try:
        while view._history_start_index > 0:
            view._on_load_earlier()
    finally:
        tv.OLDER_TURN_BATCH = saved

    standalone = [
        b
        for b in _bubbles(view)
        if list(getattr(b._turn, "tool_results", None) or [])
        and not list(getattr(b._turn, "tool_uses", None) or [])
    ]
    assert standalone == [], "every result found its call across every cut"
    assert len(_bubbles(view)) == 6


def test_a_result_that_arrives_before_the_message_finalizes_waits_for_its_card() -> None:
    """Measured on the live wire (2026-09-03): the CLI emits tool_result user
    records BEFORE the message_stop that finalizes the assistant message. The
    result must wait for the card, not become a standalone row above it.
    """
    from helios.backend.process.streaming import Block, StreamingAssistant

    view = TranscriptView()
    streaming = StreamingAssistant(blocks=[Block(type="tool_use", tool_use_name="Bash", tool_use_id="b1")])
    view.show_streaming_assistant(streaming)
    view._flush_streaming()
    assert view._streaming_bubble is not None

    view.append_turn(_result_turn(ToolResult("b1", "ok")))

    assert _bubbles(view) == [], "held, not rendered"
    assert view._streaming_bubble is not None, "the live bubble was not torn down"

    view.append_turn(_call_turn("b1"))

    bubbles = _bubbles(view)
    assert len(bubbles) == 1
    assert _card_states(bubbles[0]) == ["done · 2.0s"]
    assert view._early_results == [] and view._live_call_ids == set()


def test_an_early_result_for_a_call_not_in_the_live_message_still_gets_a_row() -> None:
    from helios.backend.process.streaming import Block, StreamingAssistant

    view = TranscriptView()
    view.show_streaming_assistant(
        StreamingAssistant(blocks=[Block(type="tool_use", tool_use_name="Bash", tool_use_id="b1")])
    )
    view._flush_streaming()

    view.append_turn(_result_turn(ToolResult("other", "orphan")))

    rows = [b for b in _bubbles(view) if list(b._turn.tool_results or []) and not list(b._turn.tool_uses or [])]
    assert len(rows) == 1 and rows[0]._turn.tool_results[0].tool_use_id == "other"


def test_held_results_surface_when_their_message_never_finalizes() -> None:
    """An interrupt or a new prompt can end a streaming message before its
    finalized Turn arrives; results held for it must not vanish."""
    from helios.backend.process.streaming import Block, StreamingAssistant

    view = TranscriptView()
    view.show_streaming_assistant(
        StreamingAssistant(blocks=[Block(type="tool_use", tool_use_name="Bash", tool_use_id="b1")])
    )
    view._flush_streaming()
    view.append_turn(_result_turn(ToolResult("b1", "partial")))
    assert view._early_results, "held for the streaming message"

    view.append_turn(Turn(role="user", text_parts=["new prompt"]))

    rows = [b for b in _bubbles(view) if list(b._turn.tool_results or []) and not list(b._turn.tool_uses or [])]
    assert len(rows) == 1 and rows[0]._turn.tool_results[0].content == "partial"
    assert view._early_results == [] and view._live_call_ids == set()
