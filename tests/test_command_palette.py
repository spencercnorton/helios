"""Real-GTK checks for the provider command launcher."""

from __future__ import annotations

import pytest


gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("GTK display unavailable", allow_module_level=True)

from helios.backend.agent_commands import AgentCommand  # noqa: E402
from helios.widgets.command_palette import CommandPalette  # noqa: E402


def test_palette_activates_enabled_rows_and_explains_disabled_ones() -> None:
    parent = Gtk.Window()
    commands = (
        AgentCommand(
            "context.compact",
            "compact",
            "Compact context",
            "Use the provider-owned summary.",
            "Codex App Server · thread/compact/start",
        ),
        AgentCommand(
            "review.start",
            "review",
            "Review changes",
            "Run a native review.",
            "Unavailable",
            supported=False,
            enabled=False,
            unavailable_reason="No native review capability.",
        ),
    )
    palette = CommandPalette(parent, commands)
    seen: list[str] = []
    palette.connect("activated", lambda _palette, command_id: seen.append(command_id))

    enabled = palette._list.get_first_child()
    disabled = enabled.get_next_sibling()
    assert enabled.get_activatable()
    assert not disabled.get_activatable()
    assert "1 available" in palette._status.get_label()

    palette._activate_row(disabled)
    assert seen == []
    palette._activate_row(enabled)
    assert seen == ["context.compact"]

    palette.close()
    parent.close()
