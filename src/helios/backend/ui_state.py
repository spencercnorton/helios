"""Persistent UI/layout state — survives restarts.

Small JSON-backed key/value store for things the window should remember between
launches: which side panels are shown, the window size, the paned splitter
positions, and the sticky model/effort selection. Mirrors the storage approach
of [project_names.py] (JSON under ~/.helios/, 0600).

This is deliberately schema-light: callers `get(key, default)` / `set(key,
value)` with plain JSON-serializable values. Unknown/stale keys are harmless —
a missing key just returns the caller's default, so older state files keep
working as the UI grows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

from helios.log import get_logger

_log = get_logger("ui_state")

_DEFAULT_PATH = Path.home() / ".helios" / "ui-state.json"
_LOCK = Lock()

# Model ids the user ticked in Settings → Providers → OpenRouter. These are the
# first tier of the OpenRouter model picker; the other several hundred stay
# behind the "Older models" disclosure and the search box. Empty list = never
# configured, in which case the picker shows the catalog unfiltered.
OPENROUTER_PICKER_KEY = "openrouter_picker_models"


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as e:
        # Disk full / read-only ~/.helios (e.g. the iCloud-overlay wedge) must
        # never let a persistence failure escape into a GTK signal handler. The
        # atomic tmp+replace means a failure leaves any existing file intact.
        _log.warning("could not save %s: %s", path.name, e)


class UiStateStore:
    """Thread-safe persistent key/value bag for window/layout state."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_PATH
        with _LOCK:
            self._data = _load(self._path)

    def get(self, key: str, default: Any = None) -> Any:
        with _LOCK:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with _LOCK:
            if self._data.get(key) == value:
                return  # no-op: don't rewrite the file for an unchanged value
            self._data[key] = value
            _save(self._path, self._data)

    def update(self, **pairs: Any) -> None:
        """Set several keys, writing the file at most once."""
        with _LOCK:
            changed = False
            for key, value in pairs.items():
                if self._data.get(key) != value:
                    self._data[key] = value
                    changed = True
            if changed:
                _save(self._path, self._data)


_global: UiStateStore | None = None


def store() -> UiStateStore:
    """Process-wide singleton."""
    global _global
    if _global is None:
        _global = UiStateStore()
    return _global


# Working directory for new chats, set in Settings → Defaults.
#
# $HOME is a discovery root, not an executable workspace: incident finding #6
# (2026-08-03) was that HOME served as both, so project_perms clamps a HOME cwd
# to read-only below the UI layer. Before this key existed the new-chat default
# was hardcoded to $HOME, which meant a fresh chat could never edit — with
# no way to change it short of picking a folder by hand every time.
DEFAULT_CWD_KEY = "default_cwd"


def configured_default_cwd() -> str:
    """The explicitly configured new-chat directory, or ``""`` when there is none.

    Distinct from :func:`default_cwd` on purpose. That one answers "where do we
    open?" and so must always name a directory; this one answers "did the user
    actually choose one?", which the caller needs in order to tell an explicit
    choice apart from the $HOME fallback. Collapsing the two is what would make
    an unset setting silently outrank the executable-project heuristic and land
    every fresh chat back in read-only $HOME — the v0.58.0/v0.59.0 regression.

    Returns ``""`` when unset, unreadable, or no longer a directory, so a stale
    setting degrades to the heuristic rather than to a cwd that is gone.
    """

    configured = str(store().get(DEFAULT_CWD_KEY, "") or "").strip()
    if not configured:
        return ""
    try:
        if Path(configured).is_dir():
            return configured
    except OSError as e:  # unreadable parent, stale network mount
        _log.warning("default cwd %s is unreachable: %s", configured, e)
    else:
        _log.warning("default cwd %s is not a directory; ignoring", configured)
    return ""


def default_cwd() -> str:
    """Configured new-chat working directory, or $HOME when unusable.

    Falling back rather than raising is deliberate: an unmounted or deleted
    directory must not stop Helios from opening. The read-only clamp then makes
    the degraded state visible instead of silently granting write access
    somewhere unintended.
    """

    return configured_default_cwd() or str(Path.home())


# ---------------------------------------------------------------------------
# Effort-level migration helpers
# ---------------------------------------------------------------------------

# Map old int token values (stored under "effort") to the new string keys
# stored under "effort_level".
_INT_TO_EFFORT_KEY: dict[int, str] = {
    0:     "off",
    4000:  "low",
    8000:  "medium",
    16000: "high",
    32000: "xhigh",
}
_DEFAULT_EFFORT_KEY = "high"

# Retired keys and what they become. "ultracode" was a boolean settings flag
# standing in the slot where the CLI's real top level (`--effort max`) belongs;
# it forced Workflow-tool orchestration as a side effect of asking for more
# reasoning. Anyone sitting on it wanted the deepest reasoning available, so
# they get exactly that — without the orchestration mandate. Without this they
# would silently land on "high", two stops down from what they chose.
_RETIRED_EFFORT_KEYS: dict[str, str] = {"ultracode": "max"}


def canonical_effort_key(key: str) -> str:
    """Map a stored effort key onto the one the UI still offers.

    Applied on every read rather than by rewriting stored state: conversation
    records, per-provider memory and the global default all persist this key
    independently, and a migration that missed one would leave that surface
    silently falling back to "high".
    """
    return _RETIRED_EFFORT_KEYS.get(key, key)


def migrate_effort_level(s: "UiStateStore") -> str:
    """Return the current effort level key, migrating old int values on the fly.

    Reads "effort_level" (new str key) first, mapping any retired key onto its
    replacement.  If absent, falls back to the old "effort" int key and maps it
    to the nearest new key.  Never raises; always returns a valid key string.
    """
    new_key = s.get("effort_level", None)
    if isinstance(new_key, str) and new_key:
        return canonical_effort_key(new_key)
    # Try old int-based key.
    old_int = s.get("effort", None)
    if isinstance(old_int, int):
        return _INT_TO_EFFORT_KEY.get(old_int, _DEFAULT_EFFORT_KEY)
    return _DEFAULT_EFFORT_KEY
