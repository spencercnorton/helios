"""Client for the shared scratchpad — the network-wide shared context store
(handoffs, repo analyses, infra investigations).

The scratchpad is the actual "single shared space" for every Claude instance
on the tailnet: one HTTP service, written and read live. Helios mostly
*reads* it (listing + viewing entries); the one deliberate write is the
"hand off this session" action (`write_entry`), which publishes a session's
resume coordinates the same way the MCP `scratch_handoff` tool does.

Transport mirrors the MCP shim (~/.claude/mcp/scratchpad/server.py): plain
HTTP, optional X-API-Key header. stdlib urllib so Helios gains no new
dependency. Every call here is blocking network I/O — callers must run them
off the main loop.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from helios.backend.urlcheck import safe_http_url

# The endpoint comes from the session environment (APOLLO_SCRATCHPAD_URL, the
# same name the MCP shim reads); the built-in default is a local listener, so a
# build with no estate configuration talks to nothing off-host. A bad override
# (non-http scheme, schemeless) is clamped back to the default rather than fed
# to urlopen.
BASE_URL = safe_http_url(
    os.environ.get("APOLLO_SCRATCHPAD_URL"), "http://127.0.0.1:9101"
)
API_KEY = os.environ.get("APOLLO_SCRATCHPAD_KEY", "")
TIMEOUT = 10


class ScratchpadError(Exception):
    """Raised for any transport / HTTP failure. Message is user-presentable."""


@dataclass(slots=True)
class Entry:
    key: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    expires_at: float = 0.0
    created_by: str = ""
    size_bytes: int = 0
    data: object = None  # populated by read_entry only

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        return cls(
            key=str(d.get("key", "")),
            summary=str(d.get("summary", "") or ""),
            tags=[str(t) for t in (d.get("tags") or [])],
            created_at=float(d.get("created_at") or 0.0),
            expires_at=float(d.get("expires_at") or 0.0),
            created_by=str(d.get("created_by", "") or ""),
            size_bytes=int(d.get("size_bytes") or 0),
            data=d.get("data"),
        )


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise ScratchpadError(f"scratchpad HTTP {e.code} on {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ScratchpadError("can't reach the scratchpad") from e
    except json.JSONDecodeError as e:
        raise ScratchpadError("scratchpad returned malformed JSON") from e


def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    if API_KEY:
        req.add_header("X-API-Key", API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise ScratchpadError(f"scratchpad HTTP {e.code} on {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ScratchpadError("can't reach the scratchpad") from e
    except json.JSONDecodeError as e:
        raise ScratchpadError("scratchpad returned malformed JSON") from e


def write_entry(
    key: str,
    data: object,
    *,
    summary: str = "",
    tags: list[str] | tuple[str, ...] = (),
    ttl_hours: float = 48.0,
    created_by: str = "helios",
) -> None:
    """Create or overwrite one entry (POST /scratch/write).

    Tag `handoff` is what session_context surfaces to other sessions — the
    caller sets it explicitly (the HTTP API, unlike the MCP scratch_handoff
    shim, adds no tags of its own)."""
    _post(
        "/scratch/write",
        {
            "key": key,
            "data": data,
            "summary": summary,
            "tags": list(tags),
            "ttl_hours": ttl_hours,
            "created_by": created_by,
        },
    )


def list_entries(prefix: str = "", tag: str = "") -> list[Entry]:
    """Summaries of all live entries, newest first."""
    params: dict = {}
    if prefix:
        params["prefix"] = prefix
    if tag:
        params["tag"] = tag
    raw = _get("/scratch/list", params).get("entries", [])
    entries = [Entry.from_dict(e) for e in raw if isinstance(e, dict)]
    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries


def read_entry(key: str) -> Entry:
    """Full entry including its data payload. 404 → ScratchpadError."""
    return Entry.from_dict(_get(f"/scratch/read/{urllib.parse.quote(key, safe='/')}"))


# Ceiling on what the Shared Context pane will lay out. This is a LAYOUT
# threshold, not a data limit: the entry on the scratchpad service is untouched
# and still readable in full with scratch_read(key). Scratchpad entries are
# unbounded — a handoff can be megabytes — and the cost is not the read, it is
# Pango measuring that much text on the main loop. Truncation is always visible
# in the output, never silent.
MAX_DISPLAY_CHARS = 64 * 1024


def format_data(data: object) -> str:
    """Render an entry's data payload for display: strings pass through,
    structures pretty-print as JSON. Capped at MAX_DISPLAY_CHARS."""
    if data is None:
        return ""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(data)
    if len(text) > MAX_DISPLAY_CHARS:
        dropped = len(text) - MAX_DISPLAY_CHARS
        # ponytail: dump-then-slice. The json.dumps is O(n) either way; the
        # layout is what stalls. Stream-cap the encoder only if a payload ever
        # gets big enough that the dump itself is measurable.
        text = text[:MAX_DISPLAY_CHARS] + (
            f"\n\n… ({dropped} more characters not rendered here — "
            f"read the entry in full with scratch_read)"
        )
    return text
