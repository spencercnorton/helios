"""Per-project user-visible name overrides.

The display name in the project sidebar defaults to the last two segments of
the project's cwd ("home/spencer", "spencer/helios", ...). Users can override
this with a custom label that survives restarts and travels by cwd.

Storage: a single JSON file at ~/.helios/project-names.json mapping cwd → label.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

from helios.log import get_logger

_log = get_logger("project_names")

_DEFAULT_PATH = Path.home() / ".helios" / "project-names.json"
_LOCK = Lock()


def _load(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Only keep strings.
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _save(path: Path, data: dict[str, str]) -> None:
    try:
        # The parent dir holds user-only data (rename labels, title cache).
        # Force 0700 even if it pre-exists — `mkdir(exist_ok=True, mode=...)`
        # silently skips the mode when the dir is already there.
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        # 0600 before the rename so the published file is never world-readable.
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as e:
        # Disk full / read-only ~/.helios must never escape into a GTK signal
        # handler; atomic tmp+replace leaves any existing file intact.
        _log.warning("could not save %s: %s", path.name, e)


class ProjectNameStore:
    """Thread-safe persistent map of cwd -> display label."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        with _LOCK:
            self._data = _load(self._path)

    def get(self, cwd: str) -> str | None:
        with _LOCK:
            return self._data.get(cwd) or None

    def set(self, cwd: str, label: str) -> None:
        label = label.strip()
        with _LOCK:
            if label:
                self._data[cwd] = label
            else:
                # Empty label = reset to default
                self._data.pop(cwd, None)
            _save(self._path, self._data)

    def clear(self, cwd: str) -> None:
        with _LOCK:
            if cwd in self._data:
                del self._data[cwd]
                _save(self._path, self._data)


_global: ProjectNameStore | None = None


def store() -> ProjectNameStore:
    """Process-wide singleton."""
    global _global
    if _global is None:
        _global = ProjectNameStore()
    return _global
