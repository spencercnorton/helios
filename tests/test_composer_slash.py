"""The composer's `/` completion popover — visibility, highlight and accept.

Needs gi + a display like the other widget tests. The Composer is parented to
a realized-but-never-presented `Gtk.Window` on purpose: a GtkPopover is its
own native surface, so `popup()` segfaults when its parent has no toplevel,
while `realize()` alone creates that surface without mapping anything onto the
desktop.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

from helios.backend.slash_commands import normalize_commands  # noqa: E402
from helios.widgets.composer import Composer  # noqa: E402

_COMMANDS = normalize_commands(
    [
        {"name": "compact", "description": "Compact the conversation"},
        {"name": "context", "description": "Show context usage"},
        {"name": "cost", "description": "Show session cost"},
        {"name": "recompact", "description": "Compact again", "source": "user"},
        {"name": "review", "description": "Review changes", "argumentHint": "<paths>"},
    ]
)


def _composer(commands=_COMMANDS) -> Composer:
    composer = Composer()
    window = Gtk.Window()
    window.set_child(composer)
    window.realize()
    composer._test_window = window  # pin: the surface popup() anchors to
    composer.set_slash_commands(commands)
    return composer


def _type(composer: Composer, text: str) -> None:
    """Type at the cursor — `set_text` deliberately dismisses the popover."""
    composer._textview.get_buffer().insert_at_cursor(text)


def _press(composer: Composer, keyval, state=0) -> bool:
    return composer._on_key_pressed(None, keyval, 0, state)


def _rows(composer: Composer) -> list[str]:
    popover = composer._slash
    return [
        popover._list.get_row_at_index(i).command.name
        for i in range(len(popover._commands))
    ]


def _cursor(composer: Composer) -> int:
    buf = composer._textview.get_buffer()
    return buf.get_iter_at_mark(buf.get_insert()).get_offset()


def test_slash_opens_the_popover_and_filters_as_you_type():
    c = _composer()
    assert c.slash_commands == _COMMANDS
    _type(c, "/")
    assert c._slash.get_visible()
    assert _rows(c) == ["compact", "context", "cost", "recompact", "review"]
    _type(c, "co")
    assert _rows(c) == ["compact", "context", "cost", "recompact"]
    _type(c, "mpact")
    assert _rows(c) == ["compact", "recompact"]


def test_no_commands_means_no_popover():
    c = _composer(commands=())
    _type(c, "/compact")
    assert not c._slash.get_visible()


def test_popover_closes_when_nothing_matches():
    c = _composer()
    _type(c, "/comp")
    assert c._slash.get_visible()
    _type(c, "zzz")
    assert not c._slash.get_visible()


def test_popover_stays_shut_when_the_first_token_is_not_a_slash():
    c = _composer()
    _type(c, "look at /compact")
    assert not c._slash.get_visible()


def test_popover_closes_when_the_cursor_leaves_the_first_token():
    c = _composer()
    _type(c, "/comp")
    assert c._slash.get_visible()
    buf = c._textview.get_buffer()
    buf.place_cursor(buf.get_start_iter())
    assert not c._slash.get_visible()


def test_arrow_keys_move_the_highlight_and_wrap():
    c = _composer()
    _type(c, "/co")
    assert c._slash.highlighted_command().name == "compact"
    assert _press(c, Gdk.KEY_Down)
    assert c._slash.highlighted_command().name == "context"
    assert _press(c, Gdk.KEY_Up)
    assert _press(c, Gdk.KEY_Up)  # wraps past the top
    assert c._slash.highlighted_command().name == "recompact"
    assert _press(c, Gdk.KEY_Down)  # and back round the bottom
    assert c._slash.highlighted_command().name == "compact"


def test_enter_accepts_the_highlighted_row_without_sending():
    c = _composer()
    sent: list[str] = []
    c.connect("send", lambda _c, text: sent.append(text))
    _type(c, "/comp")
    assert _press(c, Gdk.KEY_Return)
    assert c.current_text() == "/compact "
    assert _cursor(c) == len("/compact ")
    assert sent == []
    assert not c._slash.get_visible()


def test_tab_accepts_and_keeps_the_rest_of_the_buffer():
    c = _composer()
    _type(c, "/comp the last hour")
    buf = c._textview.get_buffer()
    buf.place_cursor(buf.get_iter_at_offset(5))  # back inside the first token
    assert c._slash.get_visible()
    assert _press(c, Gdk.KEY_Tab)
    assert c.current_text() == "/compact the last hour"
    assert _cursor(c) == len("/compact ")


def test_clicking_a_row_accepts_that_row():
    c = _composer()
    _type(c, "/co")
    popover = c._slash
    popover._list.emit("row-activated", popover._list.get_row_at_index(2))
    assert c.current_text() == "/cost "


def test_escape_closes_the_popover_and_changes_nothing():
    c = _composer()
    _type(c, "/comp")
    assert _press(c, Gdk.KEY_Escape)
    assert not c._slash.get_visible()
    assert c.current_text() == "/comp"


def test_ctrl_enter_still_sends_while_the_popover_is_open():
    c = _composer()
    sent: list[str] = []
    c.connect("send", lambda _c, text: sent.append(text))
    _type(c, "/comp")
    assert _press(c, Gdk.KEY_Return, Gdk.ModifierType.CONTROL_MASK)
    assert sent == ["/comp"]
    assert c.current_text() == ""
    assert not c._slash.get_visible()


def test_set_text_and_clear_dismiss_the_popover():
    c = _composer()
    _type(c, "/comp")
    c.set_text("/comp")
    assert not c._slash.get_visible()
    _type(c, "")  # a no-op edit must not resurrect it
    assert not c._slash.get_visible()
    c.clear()
    assert not c._slash.get_visible()


def test_plain_enter_still_inserts_a_newline_with_the_popover_shut():
    c = _composer()
    _type(c, "hello")
    assert not _press(c, Gdk.KEY_Return)
