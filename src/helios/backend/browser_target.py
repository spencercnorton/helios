"""GTK-free rules for what the browser pane may load and what needs consent.

Agents and the URL bar both hand the pane loose text — ``report.html``,
``localhost:5173``, ``https://…`` — so one resolver turns that into a URL and
refuses anything that is not a web page or a local file.

Files inside a chat's folder are not loaded as
``file://``: a ``file://`` page can embed any other file in a frame, and a
symlink can point anywhere. They are rewritten to ``helios-file://<root>/…``,
whose handler (in the pane) opens each request and checks the opened file is
still inside that root. :func:`consent_scope` then matches the Claude desktop
browser: those files and loopback servers are the agent's own work; any other
site, file or page asks the person first.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SCHEME = "helios-file"
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_LOOPBACK_HOST = re.compile(
    r"^(localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\])(:\d+)?(?:[/?#]|$)", re.IGNORECASE
)
_FILE_SUFFIXES = frozenset(
    "html htm xhtml pdf svg png jpg jpeg gif webp txt json xml csv js css".split()
)
# Python and WebKit disagree about backslashes and control characters in URLs
# (``http://evil.com\\@localhost`` is localhost to urlsplit, evil.com to WebKit).
_AMBIGUOUS = re.compile(r"[\\\x00-\x1f\x7f]")

#: root key -> real path. Keys are random per run so a page cannot guess one.
_roots: dict[str, str] = {}


class TargetError(ValueError):
    """The text names nothing the pane will load."""


def resolve(target: str, cwd: str = "") -> str:
    """Turn agent or URL-bar text into a loadable URL, or raise TargetError."""
    try:
        return _resolve(target, cwd)
    except ValueError as exc:  # urlsplit on e.g. "file://[::1/x"
        if isinstance(exc, TargetError):
            raise
        raise TargetError(f"Not a usable address: {exc}") from None


def _resolve(target: str, cwd: str) -> str:
    text = (target or "").strip()
    if not text:
        raise TargetError("Nothing to open.")
    if _AMBIGUOUS.search(text):
        raise TargetError("Addresses may not contain backslashes or control characters.")
    lowered = text.lower()
    if lowered.startswith(f"{SCHEME}://"):
        if urlsplit(text).hostname not in _roots:
            raise TargetError("Unknown project file address.")
        return text
    if lowered.startswith(("http://", "https://")) or lowered == "about:blank":
        if " " in text:
            raise TargetError("A web address may not contain spaces.")
        return text
    if lowered.startswith("file://"):
        parts = urlsplit(text)
        path = unquote(parts.path)
        if not path.startswith("/") or _AMBIGUOUS.search(path):
            raise TargetError("A file URL must name a plain absolute path.")
        real = urlsplit(Path(os.path.realpath(path)).as_uri()).path
        return urlunsplit(("file", "", real, parts.query, parts.fragment))
    if _LOOPBACK_HOST.match(text) and " " not in text:
        return f"http://{text}"
    if _SCHEME.match(text) and not re.match(r"^[^/:]+:\d", text):
        raise TargetError(f"Unsupported address: {text.split(':', 1)[0]}: pages only.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(cwd or ".") / path
    if path.exists():
        return Path(os.path.realpath(path)).as_uri()
    host = text.split("/", 1)[0]
    suffix = host.rsplit(".", 1)[-1].split(":", 1)[0].lower() if "." in host else ""
    # "report.html" is a missing file, not a website; a domain has a real TLD.
    if suffix.isalpha() and suffix not in _FILE_SUFFIXES and " " not in text \
            and not text.startswith((".", "/", "~")):
        return f"https://{text}"
    raise TargetError(f"No such file: {path}")


def localize(url: str, cwd: str) -> str:
    """Serve a ``file://`` URL inside a free root through ``helios-file``."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "file":
        return url
    path = os.path.realpath(unquote(parts.path))
    roots = [r for r in free_roots(cwd) if within(path, r)]
    if not roots:
        return url
    root = max(roots, key=len)
    key = next((k for k, r in _roots.items() if r == root), None)
    if key is None:
        key = secrets.token_hex(8)
        _roots[key] = root
    return urlunsplit((SCHEME, key, "/" + quote(os.path.relpath(path, root)), parts.query, parts.fragment))


def delocalize(url: str) -> str:
    """The ``file://`` URL a ``helios-file`` page stands for (for other apps)."""
    parts = urlsplit(url)
    root = _roots.get(parts.hostname or "") if parts.scheme == SCHEME else None
    if root is None:
        return url
    path = os.path.join(root, unquote(parts.path).lstrip("/"))
    return urlunsplit(("file", "", quote(path), parts.query, parts.fragment))


def root_for(key: str) -> str | None:
    return _roots.get(key)


def consent_scope(url: str, cwd: str = "") -> str:
    """``""`` when an agent may use this page freely, else what to ask about.

    The answer is a host for web pages, a path for files, and the URL itself
    for anything else (an ``about:blank`` popup inherits its opener's origin,
    so it is never free). Fails closed: anything unparsable needs consent.
    """
    try:
        parts = urlsplit(url or "")
        scheme = parts.scheme.lower()
        if scheme == SCHEME:
            root = _roots.get(parts.hostname or "")
            # Another chat's project is not this agent's work.
            return "" if root and root in free_roots(cwd) else (root or url)
        if scheme == "file":
            # Never free: a file:// page can frame any file on the machine.
            return os.path.realpath(unquote(parts.path))
        if scheme in {"http", "https"}:
            host = (parts.hostname or "").lower()
            return "" if _is_loopback(host) else host or url
    except ValueError:
        pass
    return url or "about:blank"


def is_web_scope(scope: str) -> bool:
    """A host, which the person may allow permanently; paths and URLs are one-off."""
    return bool(scope) and "/" not in scope and ":" not in scope


def free_roots(cwd: str) -> list[str]:
    """The chat's own folder, unless it contains the home folder.

    Not the temp directory: it is shared by every program, including other
    agents' transcripts.
    """
    if not cwd:
        return []
    real = os.path.realpath(cwd)
    # "/", "/home" or the home folder itself would free the person's files.
    return [] if within(os.path.realpath(os.path.expanduser("~")), real) else [real]


def within(path: str, root: str) -> bool:
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
