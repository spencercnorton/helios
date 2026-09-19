"""Translucent sidebar + accent — the half that needs a real GTK stack.

Separate module on purpose: `pytest.importorskip` below skips this entire file in
the GTK-free `tests` lane, so anything that must run there belongs in
test_glass_sidebar.py instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CSS = (
    Path(__file__).resolve().parents[1]
    / "src/helios/resources/style/helios.css"
).read_text(encoding="utf-8")

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from gi.repository import Adw, Gtk  # noqa: E402

Adw.init()


def test_apply_glass_adds_and_removes_the_class() -> None:
    from helios.app import GLASS_CSS_CLASS, apply_glass

    w = Gtk.Box()
    assert apply_glass(w, True) is True
    assert GLASS_CSS_CLASS in w.get_css_classes()

    assert apply_glass(w, False) is False
    assert GLASS_CSS_CLASS not in w.get_css_classes()


def test_apply_glass_is_idempotent() -> None:
    """Called on every activate, and the settings switch can fire repeatedly."""
    from helios.app import GLASS_CSS_CLASS, apply_glass

    w = Gtk.Box()
    apply_glass(w, True)
    apply_glass(w, True)
    assert w.get_css_classes().count(GLASS_CSS_CLASS) == 1


def test_apply_glass_routes_stored_strings_through_normalize() -> None:
    from helios.app import GLASS_CSS_CLASS, apply_glass

    w = Gtk.Box()
    apply_glass(w, "false")
    assert GLASS_CSS_CLASS not in w.get_css_classes()


def test_accent_rgb_returns_a_usable_triple_on_either_libadwaita() -> None:
    """Must not raise on 1.5.0 (CI) or 1.9.x (the workstation).

    A raise here would happen inside a Cairo draw handler, where PyGObject
    swallows it at the C boundary and the ring just stops painting — a failure
    with no traceback anywhere.
    """
    from helios.widgets.chat_toolbar import _accent_rgb

    for is_dark in (True, False):
        rgb = _accent_rgb(is_dark)
        assert len(rgb) == 3
        assert all(isinstance(c, float) and 0.0 <= c <= 1.0 for c in rgb), rgb


def test_accent_rgb_falls_back_when_the_api_is_absent(monkeypatch) -> None:
    """Simulates libadwaita 1.5.0 (the ubuntu:24.04 CI toolkit) on a 1.9 box.

    `monkeypatch.delattr` on the gi module does NOT work — gi resolves repository
    attributes lazily, so the deleted name is re-imported on the next getattr and
    the guard is never exercised. Verified: that version of this test passed
    while returning a real accent triple. Swapping the module reference for a
    stand-in with no such attribute is what actually takes the fallback branch.
    """
    from helios.widgets import chat_toolbar

    class _NoAccentAdw:
        """1.5.0's surface: a StyleManager, and no accent helpers at all."""

        StyleManager = chat_toolbar.Adw.StyleManager

    monkeypatch.setattr(chat_toolbar, "Adw", _NoAccentAdw)
    assert not hasattr(_NoAccentAdw, "accent_color_to_standalone_rgba")

    assert chat_toolbar._accent_rgb(True) == chat_toolbar._FALLBACK_ACCENT_RGB[True]
    assert chat_toolbar._accent_rgb(False) == chat_toolbar._FALLBACK_ACCENT_RGB[False]


def test_accent_rgb_actually_reads_the_system_accent_when_available() -> None:
    """Guards against the fallback silently becoming the only path.

    If the guard were inverted (or the API name misspelled) every test above
    would still pass while the arc quietly went back to a fixed color. Forcing
    two different system accents must produce two different triples.
    """
    from helios.widgets import chat_toolbar

    if not hasattr(chat_toolbar.Adw, "accent_color_to_standalone_rgba"):
        pytest.skip("libadwaita < 1.6 has no accent API to read")

    to_standalone = chat_toolbar.Adw.accent_color_to_standalone_rgba
    blue = to_standalone(chat_toolbar.Adw.AccentColor.BLUE, True)
    red = to_standalone(chat_toolbar.Adw.AccentColor.RED, True)
    assert (blue.red, blue.green, blue.blue) != (red.red, red.green, red.blue)

    live = chat_toolbar._accent_rgb(True)
    assert live != chat_toolbar._FALLBACK_ACCENT_RGB[True] or live == (
        blue.red,
        blue.green,
        blue.blue,
    ), "accent came back as the fallback on a toolkit that has the API"


def test_the_stylesheet_still_parses() -> None:
    """The glass block adds selectors GTK 4.14 (CI) must also accept."""
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)  # raises on a parse error
