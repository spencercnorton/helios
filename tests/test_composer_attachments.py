"""Composer.add_attachments — the @path token insertion behind the + button
and drag-drop. Needs gi + a display (builds a real Gtk widget); skipped on the
GTK-free CI image, matching the other widget tests.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

from helios.widgets.composer import Composer  # noqa: E402


def test_add_attachments_into_empty_composer():
    c = Composer()
    c.add_attachments(["/tmp/a.py", "/tmp/b.txt"])
    assert c.current_text() == "@/tmp/a.py @/tmp/b.txt "


def test_add_attachments_quotes_paths_with_spaces():
    c = Composer()
    c.add_attachments(["/home/me/My Project/spec.md", "/tmp/a.py"])
    # The space path is quoted to stay one @-token; the simple one stays bare.
    assert c.current_text() == '@"/home/me/My Project/spec.md" @/tmp/a.py '


def test_add_attachments_escapes_quotes_and_backslashes():
    # A crafted filename with a quote must not terminate the @-token early or
    # inject extra @refs — quote + backslash are escaped inside the token.
    c = Composer()
    c.add_attachments(['/tmp/a" b.md'])
    assert c.current_text() == '@"/tmp/a\\" b.md" '
    c2 = Composer()
    c2.add_attachments(["/tmp/back\\slash.md"])
    assert c2.current_text() == '@"/tmp/back\\\\slash.md" '


def test_add_attachments_spaces_existing_text():
    c = Composer()
    c.set_text("look at")
    c.add_attachments(["/tmp/a.py"])
    assert c.current_text() == "look at @/tmp/a.py "


def test_add_attachments_no_double_space():
    c = Composer()
    c.set_text("look at ")  # already ends with a space
    c.add_attachments(["/tmp/a.py"])
    assert c.current_text() == "look at @/tmp/a.py "


def test_add_attachments_ignores_empty_and_readonly():
    c = Composer()
    c.add_attachments([""])              # nothing usable
    assert c.current_text() == ""
    c.set_read_only(True)
    c.add_attachments(["/tmp/a.py"])     # read-only: refused
    assert c.current_text() == ""
