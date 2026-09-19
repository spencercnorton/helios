from __future__ import annotations

from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from helios.backend.capabilities import build_snapshot
from helios.widgets.capabilities_pane import CapabilitiesPane


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


def test_refresh_is_an_explicit_local_report_request():
    pane = CapabilitiesPane()
    requested = []
    pane.connect("refresh-requested", lambda *_: requested.append(True))
    pane._refresh.emit("clicked")
    assert requested == [True]


def test_provider_switch_clears_old_tools_and_source_receipts():
    pane = CapabilitiesPane()
    pane.set_driver(SimpleNamespace(provider="openrouter", model="model-a", _cwd="/repo",
                                   init_tools=["old-private-tool"], init_mcp_servers=[]))
    pane.set_driver(SimpleNamespace(provider="openai", model="model-b", _cwd="/other"))
    text = " ".join(w.get_label() for w in descendants(pane) if isinstance(w, Gtk.Label))
    assert "old-private-tool" not in text and "model-a" not in text
    assert "model-b" in text and "Codex loads AGENTS.md natively" in text


def test_untrusted_metadata_is_plain_text_and_receipts_render():
    pane = CapabilitiesPane()
    pane.set_snapshot(build_snapshot(SimpleNamespace(
        provider="openrouter", model="<b>literal model</b>", init_tools=["tool-one"],
        init_mcp_servers=[{"name": "<b>literal server</b>", "status": "configured"}],
        init_instruction_sources=[{"provider": "openrouter", "path": "/repo/AGENTS.md",
                                   "resolved_path": "/repo/AGENTS.md", "sha256": "a" * 64,
                                   "bytes": 30, "precedence": 0, "loaded_at": 1_780_000_000.0}],
    ), host="test-host", now=1_780_000_100.0))
    rows = [w for w in descendants(pane) if isinstance(w, Adw.PreferencesRow)]
    assert rows and all(not row.get_use_markup() for row in rows)
    text = " ".join(w.get_label() for w in descendants(pane) if isinstance(w, Gtk.Label))
    assert "<b>literal model</b>" in text
    assert "current disk freshness is unknown" in text
    assert pane.snapshot.instructions[0].sha256 == "a" * 64


@pytest.mark.parametrize("matches, provider_matches", [(False, True), (True, True), (True, False)])
def test_window_report_uses_only_selected_driver_binding(matches, provider_matches):
    from helios.main_window import MainWindow

    pane = CapabilitiesPane()
    driver = SimpleNamespace(provider="openrouter", model="selected-model", _cwd="/repo",
                             init_tools=["private-session-tool"])
    window = SimpleNamespace(_capabilities=pane, _destroyed=False, _driver=driver,
                             _driver_matches_target_binding=lambda _driver: matches,
                             _driver_matches_selected_provider=lambda _driver: provider_matches,
                             _selected_provider=lambda: "openrouter",
                             _current_project_cwd=lambda: "/repo")
    MainWindow._refresh_capabilities(window)
    expected = matches and provider_matches
    assert pane.snapshot.attached is expected
    assert ("private-session-tool" in pane.snapshot.tools) is expected
    window._destroyed = True
    pane.set_driver(None)
    MainWindow._refresh_capabilities(window)
    assert pane.snapshot.attached is False
