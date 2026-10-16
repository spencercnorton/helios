"""The agent-facing contract of the browser pane, GTK-free.

Every provider reaches the pane the same way: its CLI launches the
``helios-browser-mcp`` stdio server, which forwards each tool call over the
session bus to the Helios process that spawned the CLI. The window publishes
the bus address once its D-Bus object is registered; until then (no WebKit,
headless tests) :func:`server_entry` returns ``None`` and nothing is injected.
"""

from __future__ import annotations

from pathlib import Path

from helios import APP_ID

MCP_SERVER_NAME = "helios_browser"
OBJECT_PATH = "/" + APP_ID.replace(".", "/") + "/Browser"
INTERFACE = f"{APP_ID}.Browser"
INTERFACE_XML = f"""<node>
  <interface name="{INTERFACE}">
    <method name="Call">
      <arg type="s" name="tool" direction="in"/>
      <arg type="s" name="arguments" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
  </interface>
</node>"""

PACKAGED_LAUNCHER = Path("/usr/lib/helios/helios-browser-mcp")

INSTRUCTIONS = (
    "Helios has a browser pane beside this chat. Use browser_open to show the "
    "person an HTML report, a local file or a dev server (a path, "
    "localhost:PORT or a URL) instead of only describing it. browser_read and "
    "browser_screenshot let you check what rendered; reading a non-local site "
    "asks the person first."
)

_TAB = {"type": "string", "description": "Tab id from browser_open or browser_tabs; defaults to the selected tab."}

TOOLS = [
    {
        "name": "browser_open",
        "description": "Open a page in the Helios browser pane and show it to the person. "
                       "Accepts a file path (relative to the project), localhost:PORT, or an http(s) URL. "
                       "Returns once the page has loaded.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "File path, localhost:PORT or URL."},
                "new_tab": {"type": "boolean", "description": "Open in a new tab (default true)."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "browser_navigate",
        "description": "Load a URL in an existing tab, or go back, forward or reload.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab": _TAB,
                "url": {"type": "string"},
                "action": {"type": "string", "enum": ["back", "forward", "reload"]},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "browser_read",
        "description": "Return a tab's title, URL and visible text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab": _TAB,
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "browser_screenshot",
        "description": "Return a PNG screenshot of what a tab currently shows.",
        "inputSchema": {"type": "object", "properties": {"tab": _TAB}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "browser_tabs",
        "description": "List the open tabs with their ids, titles and URLs.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
]
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)

_bus_address = ""


def publish(bus_address: str) -> None:
    """Called by the window once the pane's D-Bus object is registered."""
    global _bus_address
    _bus_address = bus_address or ""


def launcher_path() -> Path:
    """A source checkout's own launcher first, then the packaged one."""
    repo = Path(__file__).resolve().parents[3] / "scripts" / "helios-browser-mcp"
    return repo if repo.is_file() else PACKAGED_LAUNCHER


def server_entry(cwd: str, agent: str) -> dict | None:
    """``{"command", "args"}`` for one provider session, or None if unavailable."""
    launcher = launcher_path()
    if not _bus_address or not launcher.is_file():
        return None
    return {
        "command": str(launcher),
        "args": ["--bus", _bus_address, "--cwd", cwd or str(Path.home()), "--agent", agent],
    }
