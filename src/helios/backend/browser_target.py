"""GTK-free rules for what the browser pane may load and what needs consent.

Agents and the URL bar both hand the pane loose text — ``report.html``,
``localhost:5173``, ``https://…`` — so one resolver turns that into a URL and
refuses anything that is not a web page or a local file. :func:`consent_scope`
decides what an agent may use without asking, matching the Claude desktop
browser: the project's own files and loopback servers are its work; any other
host may hold the person's signed-in data, and any other file may be private.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

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


def consent_scope(url: str, cwd: str = "") -> str:
    """``""`` when an agent may use this page freely, else what to ask about.

    The answer is a host for web pages, a path for files outside the project,
    and the URL itself for anything else (an ``about:blank`` popup inherits its
    opener's origin, so it is never free).
    """
    parts = urlsplit(url or "")
    scheme = parts.scheme.lower()
    if scheme == "file":
        path = os.path.realpath(unquote(parts.path))
        return "" if any(_within(path, root) for root in _free_roots(cwd)) else path
    if scheme in {"http", "https"}:
        host = (parts.hostname or "").lower()
        return "" if _is_loopback(host) else host or url
    return url or "about:blank"


def is_web_scope(scope: str) -> bool:
    """A host, which the person may allow permanently; paths and URLs are one-off."""
    return bool(scope) and "/" not in scope and ":" not in scope


def _free_roots(cwd: str) -> list[str]:
    roots = [tempfile.gettempdir()]
    # A chat rooted at / or the home folder would otherwise free every file.
    if cwd and os.path.realpath(cwd) not in {"/", os.path.realpath(os.path.expanduser("~"))}:
        roots.append(cwd)
    return [os.path.realpath(root) for root in roots]


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    # Dev servers print 0.0.0.0; connecting to it reaches this machine.
    return address.is_loopback or address.is_unspecified
