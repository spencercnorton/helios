"""Compact header indicator for the current Work's participants + lead.

Read-only surface of the Work ledger: shown only when a Work has participants
from more than one provider (a real tandem), so solo work stays uncluttered.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from helios.backend import model_catalog

_PROVIDER_LABEL = model_catalog.PROVIDER_LABELS


def provider_label(provider: str) -> str:
    return _PROVIDER_LABEL.get(provider, (provider or "Unknown").title())


class WorkIndicator(Gtk.MenuButton):
    """People-icon header button; its popover lists the Work's mode, lead, and
    each participant's provider + role. Hidden unless the Work spans at least
    two distinct providers."""

    def __init__(self) -> None:
        super().__init__()
        self.set_icon_name("system-users-symbolic")
        self.add_css_class("flat")
        self.add_css_class("helios-work-btn")
        self.set_visible(False)
        self._popover = Gtk.Popover()
        self._popover.add_css_class("helios-work-popover")
        self.set_popover(self._popover)
        self._on_handoff = None  # pinned callback (PyGObject GC); set per update()

    def update(
        self,
        mode: str,
        lead_provider: str,
        participants: list,
        *,
        handoff_label: str | None = None,
        on_handoff=None,
    ) -> None:
        providers = [p for p in participants if getattr(p, "provider", "")]
        if len({p.provider for p in providers}) < 2:
            self.set_visible(False)
            return
        self.set_tooltip_text(
            f"Tandem Work — lead: {provider_label(lead_provider)}. "
            "Click for participants."
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        heading = Gtk.Label(xalign=0)
        heading.add_css_class("heading")
        heading.set_label(f"Tandem Work · {(mode or 'tandem').title()}")
        box.append(heading)
        for participant in sorted(providers, key=lambda p: p.role != "lead"):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            name = Gtk.Label(label=provider_label(participant.provider), xalign=0)
            name.set_hexpand(True)
            row.append(name)
            is_lead = participant.role == "lead"
            role = Gtk.Label(label="Lead" if is_lead else "Partner")
            role.add_css_class("caption")
            role.add_css_class("helios-work-role-lead" if is_lead else "dim-label")
            row.append(role)
            box.append(row)
        # Discoverable hand-off straight from the header when a target session
        # is open; otherwise point back to the Shared Context pane.
        if handoff_label and on_handoff is not None:
            self._on_handoff = on_handoff
            handoff_btn = Gtk.Button(label=f"Hand off to {handoff_label}")
            handoff_btn.add_css_class("suggested-action")
            handoff_btn.set_margin_top(4)
            handoff_btn.connect("clicked", self._on_handoff_clicked)
            box.append(handoff_btn)
        else:
            self._on_handoff = None
            hint = Gtk.Label(xalign=0, wrap=True)
            hint.add_css_class("caption")
            hint.add_css_class("dim-label")
            hint.set_label("Hand off between models from the Shared Context pane.")
            box.append(hint)
        self._popover.set_child(box)
        self.set_visible(True)

    def _on_handoff_clicked(self, _btn) -> None:
        self._popover.popdown()
        if self._on_handoff is not None:
            self._on_handoff()
