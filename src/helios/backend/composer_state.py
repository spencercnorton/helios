"""Per-conversation composer drafts. GTK-free so the slim CI lane runs it.

The composer is a single buffer shared by every conversation, so switching
sessions used to carry your half-typed message into the next one.
The window parks the outgoing text here under the key of the conversation it
belongs to, and takes it back on the way in.
"""

from __future__ import annotations


def draft_key(target) -> str:
    """Stable per-conversation draft slot.

    Accepts anything with `.session_id` / `.project.cwd` — a `Session` for a
    row selection, a `ChatTarget` (no `session_id`) for a fresh chat. Fresh
    chats key on cwd so two unstarted chats in different folders keep
    separate drafts.
    """

    sid = str(getattr(target, "session_id", "") or "")
    if sid:
        return sid
    cwd = str(getattr(getattr(target, "project", None), "cwd", "") or "")
    return f"new:{cwd}"


class DraftBook:
    """key -> unsent composer text.

    A blank stash EVICTS rather than stores, so clearing the composer in one
    session does not resurrect a stale draft when you come back to it.

    ponytail: in-memory only, no TTL and no cap — drafts die with the window,
    and the book is bounded by how many conversations one window visits.
    Persist to ui-state if anyone asks for drafts to survive a restart.
    """

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def stash(self, key: str, text: str) -> None:
        if key and text.strip():
            self._d[key] = text
        elif key:
            self._d.pop(key, None)

    def take(self, key: str) -> str:
        return self._d.get(key, "")
