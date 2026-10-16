"""GTK-free rules for what the browser pane may load and how it is addressed.

Agents and the URL bar both hand the pane loose text — ``report.html``,
``localhost:5173``, ``https://…`` — so one resolver turns that into a URL and
refuses anything that is not a web page or a local file. ``is_local`` decides
which pages an agent may read without asking: files and loopback servers are
the agent's own work; any other host may hold the person's signed-in data.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_LOOPBACK_HOST = re.compile(
    r"^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])(:\d+)?(?:[/?#]|$)", re.IGNORECASE
)

_FILE_SUFFIXES = frozenset(
    "html htm xhtml pdf svg png jpg jpeg gif webp txt json xml csv js css".split()
)


class TargetError(ValueError):
    """The text names nothing the pane will load."""


def resolve(target: str, cwd: str = "") -> str:
    """Turn agent or URL-bar text into a loadable URL, or raise TargetError."""
    text = (target or "").strip()
    if not text:
        raise TargetError("Nothing to open.")
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")) or lowered == "about:blank":
        return text
    if lowered.startswith("file://"):
        if not urlsplit(text).path.startswith("/"):
            raise TargetError("A file URL must name an absolute path.")
        return text
    if _LOOPBACK_HOST.match(text):
        return f"http://{text}"
    if _SCHEME.match(text) and not re.match(r"^[^/:]+:\d", text):
        raise TargetError(f"Unsupported address: {text.split(':', 1)[0]}: pages only.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(cwd or ".") / path
    if path.exists():
        return path.resolve().as_uri()
    host = text.split("/", 1)[0]
    suffix = host.rsplit(".", 1)[-1].split(":", 1)[0].lower() if "." in host else ""
    # "report.html" is a missing file, not a website; a domain has a real TLD.
    if suffix.isalpha() and suffix not in _FILE_SUFFIXES and " " not in text \
            and not text.startswith((".", "/", "~")):
        return f"https://{text}"
    raise TargetError(f"No such file: {path}")


def is_local(url: str) -> bool:
    """Files, blank pages and loopback servers; never a remote host."""
    parts = urlsplit(url or "")
    scheme = parts.scheme.lower()
    if scheme in {"file", "about"}:
        return True
    if scheme not in {"http", "https"}:
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    # Dev servers print 0.0.0.0; connecting to it reaches this machine.
    return address.is_loopback or address.is_unspecified


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()
