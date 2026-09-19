"""HeliosApplication color-scheme robustness (the login portal-race fix).

Real-GTK: constructing an Adw.Application needs gi + Adw; skipped on the
GTK-free CI image. We only exercise the wiring (no run loop, no window).
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

from helios.app import HeliosApplication  # noqa: E402


def test_portal_scheme_watch_connects_once():
    """The portal color-scheme watch is installed and idempotent — so repeated
    activate() calls don't stack duplicate handlers that each re-apply."""
    app = HeliosApplication()
    assert app._scheme_watch_id == 0
    app._watch_portal_color_scheme()
    first = app._scheme_watch_id
    assert first != 0
    app._watch_portal_color_scheme()  # calling again must not reconnect
    assert app._scheme_watch_id == first


def test_reapply_saved_scheme_is_one_shot():
    """The post-present re-assert returns False so GLib runs it exactly once."""
    app = HeliosApplication()
    assert app._reapply_saved_scheme_once() is False
