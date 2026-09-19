"""Persistent session_id -> title map, shared by the sidebar and search.

Split out of `backend/process/title_generator.py` (which imports gi at module
scope because `TitleGenerator` is a GObject) so GTK-free consumers such as
`backend.search` can read titles. Pure stdlib — no gi, no widgets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

from helios.log import get_logger

_log = get_logger("titlegen")

_CACHE_PATH = Path.home() / ".helios" / "title-cache.json"
_CACHE_LOCK = Lock()

# ── Cache ────────────────────────────────────────────────────────────────


def _load_cache() -> dict[str, str]:
    if not _CACHE_PATH.is_file():
        return {}
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _save_cache(data: dict[str, str]) -> None:
    # See project_names._save for rationale on 0700/0600.
    # Note: mkdir(exist_ok=True, mode=...) skips mode on existing dirs, so we
    # chmod separately to ensure 0700 even on pre-created directories.
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(_CACHE_PATH.parent, 0o700)
        except OSError:
            pass
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(_CACHE_PATH)
    except OSError as e:
        # Disk full / read-only ~/.helios must never escape into the caller
        # (a UI thread); atomic tmp+replace leaves any existing cache intact.
        _log.warning("could not save title cache: %s", e)


class TitleStore:
    """Thread-safe persistent map of session_id -> generated title."""

    def __init__(self) -> None:
        with _CACHE_LOCK:
            self._data = _load_cache()

    def get(self, session_id: str) -> str | None:
        with _CACHE_LOCK:
            return self._data.get(session_id) or None

    def set(self, session_id: str, title: str) -> None:
        title = title.strip()
        if not title:
            return
        with _CACHE_LOCK:
            self._data[session_id] = title
            _save_cache(self._data)

    def set_if_absent(self, session_id: str, title: str) -> bool:
        """Set only when no title is stored yet; return True iff it wrote.

        Lets async title generation land its result without clobbering a title
        that appeared while it was in flight — e.g. a manual rename (which uses
        the forcing `set`). The check-and-set is atomic under the cache lock."""
        title = title.strip()
        if not title:
            return False
        with _CACHE_LOCK:
            if self._data.get(session_id):
                return False
            self._data[session_id] = title
            _save_cache(self._data)
            return True


_store_singleton: TitleStore | None = None
# Dedicated lock for the lazy singleton. NOT _CACHE_LOCK — TitleStore.__init__
# acquires _CACHE_LOCK, so reusing it here would deadlock (Lock is non-reentrant).
_STORE_LOCK = Lock()


def store() -> TitleStore:
    """Return the process-wide TitleStore (created on first use).

    Double-checked locking so a worker thread and the main thread can't each
    build an instance if they first touch the store concurrently — the loser's
    instance would otherwise be discarded and any write through it lost."""
    global _store_singleton
    if _store_singleton is None:
        with _STORE_LOCK:
            if _store_singleton is None:
                _store_singleton = TitleStore()
    return _store_singleton
