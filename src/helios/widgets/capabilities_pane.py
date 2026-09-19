"""Provider-neutral, read-only capabilities pane. Refresh never starts a tool."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk, Pango

from helios.backend.capabilities import CapabilitySnapshot, build_snapshot


def _label(text: str, *, dim: bool = False) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_selectable(True)
    if dim:
        label.add_css_class("dim-label")
    return label


class CapabilitiesPane(Gtk.Box):
    __gsignals__ = {
        "refresh-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_size_request(280, -1)
        self.add_css_class("helios-capabilities")
        header = Gtk.Box(spacing=8)
        for side in ("top", "start", "end"):
            getattr(header, f"set_margin_{side}")(12)
        title = _label("Capabilities")
        title.add_css_class("title-4")
        title.set_hexpand(True)
        header.append(title)
        self._refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh.set_tooltip_text("Refresh the displayed driver report")
        self._refresh.connect("clicked", lambda *_: self.emit("refresh-requested"))
        header.append(self._refresh)
        self.append(header)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(self._body, f"set_margin_{side}")(12)
        scroll.set_child(self._body)
        self.append(scroll)
        self.set_snapshot(build_snapshot(None))

    def set_driver(self, driver: object | None, provider: str = "", cwd: str = "") -> None:
        self.set_snapshot(build_snapshot(driver, provider, cwd))

    def set_snapshot(self, snapshot: CapabilitySnapshot) -> None:
        self.snapshot = snapshot
        while (child := self._body.get_first_child()) is not None:
            self._body.remove(child)
        description = ("Driver report copied " + snapshot.copied_at +
                       ". Refresh copies the latest report; it does not reconnect servers.")
        if not snapshot.attached:
            description = "No attached driver. Select or start a conversation to see its reported capabilities."
        self._body.append(_label(description, dim=True))

        session = self._group("Session")
        provider_names = {"openai": "Codex", "anthropic": "Claude", "openrouter": "OpenRouter"}
        self._row(session, "Provider", provider_names.get(snapshot.provider, snapshot.provider))
        self._row(session, "Model", snapshot.model)
        self._row(session, "App and tool host", snapshot.execution_host)
        self._row(session, "Driver workspace" if snapshot.cwd_reported else "Selected workspace", snapshot.cwd)

        tools = self._group("Tools", "Names reported by the driver; availability and permissions are checked when called.")
        if snapshot.tools:
            expansion = Adw.ExpanderRow(title=f"{len(snapshot.tools)} reported tools")
            expansion.set_use_markup(False)
            self._detail(expansion, "\n".join(snapshot.tools))
            tools.add(expansion)
        else:
            self._row(tools, "Inventory", "No tool names reported" if snapshot.tools_reported else "Report unavailable")

        servers = self._group("MCP servers", "Last reported status. Configured servers have not necessarily connected.")
        if not snapshot.servers:
            self._row(servers, "Inventory", "No server reports available")
        for server in snapshot.servers:
            subtitle = server.status + " · Checked: " + server.checked_at
            if server.tool_count is not None:
                subtitle += f" · {server.tool_count} tools"
            self._row(servers, server.name, subtitle)

        instructions = self._group("Instructions", snapshot.instruction_note)
        for receipt in snapshot.instructions:
            expansion = Adw.ExpanderRow(title=receipt.path)
            expansion.set_use_markup(False)
            expansion.set_subtitle(f"Order {receipt.precedence + 1} · {receipt.byte_count:,} bytes · Loaded {receipt.loaded_at}")
            self._detail(expansion, f"SHA-256\n{receipt.sha256}\n\nResolved path\n{receipt.resolved_path or 'Not reported'}"
                                   f"\n\nFile modified at load\n{receipt.modified_at}")
            instructions.add(expansion)

    def _group(self, title: str, description: str = "") -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title)
        if description:
            group.set_description(description)
        self._body.append(group)
        return group

    @staticmethod
    def _row(group: Adw.PreferencesGroup, title: str, subtitle: str) -> None:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row.set_use_markup(False)
        row.set_subtitle_lines(0)
        group.add(row)

    @staticmethod
    def _detail(expansion: Adw.ExpanderRow, text: str) -> None:
        label = _label(text)
        for side in ("top", "bottom", "start", "end"):
            getattr(label, f"set_margin_{side}")(12)
        expansion.add_row(label)
