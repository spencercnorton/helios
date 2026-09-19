"""Validate user/env-supplied base URLs before using them in HTTP requests.

Both the scratchpad base URL (env) and the ollama URL (ui-state) are overridable
and then concatenated into request URLs. An override pointing at a non-HTTP
scheme (`file:`, `gopher:`, ...) or a schemeless string would make urllib do
something other than the intended HTTP call. This clamps such overrides back to
a known-good default.
"""

from __future__ import annotations

from urllib.parse import urlparse


def safe_http_url(candidate: str | None, default: str) -> str:
    """Return `candidate` if it's a well-formed http(s) URL, else `default`."""
    if not candidate:
        return default
    try:
        parsed = urlparse(candidate)
    except (ValueError, TypeError):
        return default
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return candidate
    return default
