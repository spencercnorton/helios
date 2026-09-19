"""Rendered markdown labels are selectable but not tab stops (unless linked).

GTK lane only: markdown.py imports gi at module scope and the assertions use
real Gtk.Label properties and window focus traversal.
tests/test_markdown_inline.py documents the same limitation for this module.
"""

from __future__ import annotations

import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    # Importing helios.widgets.markdown pulls in code_block, which requires
    # GtkSource 5. Skip rather than error where it is absent — the same guard
    # tests/test_transcript_load_feedback.py uses. Without it this file raises
    # at collection on any host without gir1.2-gtksource-5 (every macOS dev box
    # here), which turns a portability gap into a new red line in the suite.
    gi.require_version("GtkSource", "5")
except ValueError:  # pragma: no cover - environment-dependent
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from gi.repository import GLib, Gtk  # noqa: E402

from helios.widgets.markdown import render_markdown  # noqa: E402


def _labels(widget: Gtk.Widget) -> list[Gtk.Label]:
    """Every Gtk.Label in the subtree, depth-first."""
    found: list[Gtk.Label] = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Label):
            found.append(child)
        found.extend(_labels(child))
        child = child.get_next_sibling()
    return found


# Every construct that calls _selectable_text: heading, paragraph, quote, list
# item, and both table header and body cells. Testing only a paragraph proves
# the one branch an author is least likely to break, because it is the one the
# CHANGELOG names — a regression re-adding set_selectable(True) to a table cell
# (the site with the most instances per turn) would ship silently.
_ALL_CONSTRUCTS = (
    "# Head\n\n"
    "Just a plain paragraph.\n\n"
    "> a quote\n\n"
    "- item one\n\n"
    "| a | b |\n| --- | --- |\n| c | d |\n"
)


def test_no_rendered_construct_is_a_tab_stop() -> None:
    labels = _labels(render_markdown(_ALL_CONSTRUCTS))
    assert len(labels) >= 8, (
        f"expected labels from all five constructs, got {len(labels)}: "
        f"{[lbl.get_label() for lbl in labels]}"
    )
    focusable = [lbl.get_label() for lbl in labels if lbl.get_focusable()]
    assert not focusable, f"still tab stops: {focusable}"


def test_plain_paragraph_is_selectable_but_not_focusable() -> None:
    labels = _labels(render_markdown("Just a plain paragraph."))
    assert labels, "expected at least one label"
    assert all(lbl.get_selectable() for lbl in labels)
    assert not any(lbl.get_focusable() for lbl in labels)


def test_paragraph_with_link_stays_focusable() -> None:
    labels = _labels(render_markdown("See [docs](https://example.com) for more."))
    assert labels, "expected at least one label"
    linked = [lbl for lbl in labels if '<a href="' in lbl.get_label()]
    assert linked, "expected the link markup to survive _inline_markup"
    assert all(lbl.get_focusable() for lbl in linked)
    assert all(lbl.get_can_focus() for lbl in linked)
    assert all(lbl.get_selectable() for lbl in linked)


def test_tab_traverses_real_transcript_and_leaves_in_both_directions() -> None:
    """Properties alone missed a selectable-label focus trap in real GTK."""
    from helios.backend.transcript import Turn
    from helios.widgets.transcript_view import TranscriptView

    transcript = TranscriptView()
    transcript.show_live_session(cwd="/example", model="example")
    for role in ("user", "assistant"):
        turn = Turn(role=role)
        turn.add("text", "A plain paragraph.")
        transcript.append_turn(turn)
    after = Gtk.Button(label="After transcript")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(transcript)
    box.append(after)
    window = Gtk.Window()
    window.set_child(box)
    window.set_default_size(600, 500)
    window.present()
    context = GLib.MainContext.default()
    try:
        deadline = time.monotonic() + 2
        while not transcript.get_mapped() and time.monotonic() < deadline:
            context.iteration(False)
        assert transcript.get_mapped()

        def copy_button(bubble):
            return bubble.get_first_child().get_last_child()

        first = copy_button(transcript._list.get_first_child())
        second = copy_button(transcript._list.get_last_child())
        assert first.get_tooltip_text() == second.get_tooltip_text() == "Copy message"
        deadline = time.monotonic() + 2
        while second.get_width() == 0 and time.monotonic() < deadline:
            context.iteration(False)
        assert second.get_width() > 0
        first.grab_focus()
        for direction, expected in (
            (Gtk.DirectionType.TAB_FORWARD, second),
            (Gtk.DirectionType.TAB_FORWARD, after),
            (Gtk.DirectionType.TAB_BACKWARD, second),
            (Gtk.DirectionType.TAB_BACKWARD, first),
        ):
            window.child_focus(direction)
            assert window.get_focus() is expected, (direction, window.get_focus(), expected)
    finally:
        transcript.shutdown()
        window.destroy()
