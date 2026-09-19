"""Best-effort credential scrubbing for durable/shared Helios context."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED BY HELIOS]"

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-ant-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|"
    # GitLab's whole routable-token family. Verified missing 2026-08-06: a
    # personal access token pasted into an objective or a definition of done
    # reached both the model and the durable Work ledger in the clear, while
    # sk-ant-*, AKIA* and password= were all caught. These are the prefixes
    # GitLab documents as routable tokens; `gl[a-z]{2,8}-` would also match,
    # but an explicit list is greppable and cannot swallow an unrelated word.
    r"gl(?:pat|ptt|dt|rt|cbt|soat|imt|agent|ffct|oat|deploy)-"
    r"[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
# The separator and the value must stay on ONE line. `\s` matches a newline, so
# `token:` at the end of a line used to swallow the whole first word of the NEXT
# line — measured 2026-09-03 over 283 repository source files, where a redaction
# repeatedly ate the following line's leading text. `[^\S\n]` fixed the LF case
# but still admits `\r`, vertical tab, form feed, NEL and the Unicode
# separators, so a lone-CR file (or a form feed) could still cross a boundary
# (a review finding). Only a literal space or tab can separate a name from
# its value, so say exactly that. The quoted branches carry the same rule:
# `[^"\n]*` still admitted CR, vertical tab, form feed, NEL and the Unicode
# separators inside the quotes, so a quoted value could still span a real
# line boundary (round 10).
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b((?:[A-Z0-9]+_)*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|secret|token))\b"
    r"([ \t]*(?:=|:)[ \t]*)"
    # Only genuine line boundaries are excluded from a quoted value — CR, LF,
    # vertical tab, form feed, NEL and the Unicode line/paragraph separators.
    # A space inside quotes is part of the value, not a terminator.
    r"(?:\"[^\"\r\n\v\f\x85\u2028\u2029]*\"|'[^'\r\n\v\f\x85\u2028\u2029]*'|[^\s,;]+)"
)
_CREDENTIAL_URL_RE = re.compile(r"(https?://[^\s/:@]+:)([^\s/@]+)(@)", re.I)


def scrub_sensitive(value: Any, *, named_pairs: bool = True) -> tuple[str, bool]:
    """Return text with recognizable credentials replaced before persistence.

    No detector can prove arbitrary prose secret-free. Helios therefore also
    excludes tool results, environment data, and hidden reasoning from the
    Work ledger; this scrubber covers common pasted credential forms in the
    user/final-text surface that is intentionally shared.

    ``named_pairs=False`` drops the ``name = value`` rule and keeps only the
    rules that match a credential's *shape*. Use it on machine output rather
    than prose. Measured 2026-09-03 over this repository: with the name rule
    on, reading 46 of 283 source files came back damaged — ``token =
    object()`` became ``token = [REDACTED …]``, ``api_key: str`` lost its
    annotation, and an agent reading its own code got nonsense. The rule is
    right for prose, where ``password = hunter2`` really is a password, and
    wrong for source, where those words are variable names. The shape rules
    (private-key blocks, Bearer headers, the known token prefixes, credentials
    embedded in a URL) have no such ambiguity.
    """

    text = str(value or "")
    original = text
    text = _PRIVATE_KEY_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    text = _KNOWN_TOKEN_RE.sub(REDACTED, text)
    if named_pairs:
        text = _NAMED_SECRET_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text
        )
    text = _CREDENTIAL_URL_RE.sub(rf"\1{REDACTED}\3", text)
    return text, text != original
