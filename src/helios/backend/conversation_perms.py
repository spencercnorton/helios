"""Durable execution-setting overrides for provider-native conversations.

This store contains only explicit conversation overrides.  Choosing a
workspace/default fallback remains the caller's responsibility: a missing
``(provider, native_session_id)`` pair returns the supplied default without
consulting project settings.

The on-disk shape is nested by provider so two providers may use the same
native identifier without colliding::

    {
      "anthropic": {
        "session-123": {
          "permission_mode": "plan",
          "effort_key": "high",
          "workflow_mode": "default"
        }
      },
      "openai": {
        "thread-456": {"permission_mode": "auto"}
      }
    }

``permission_mode`` is required. ``effort_key`` and ``workflow_mode`` are
optional so old records migrate to the ordinary Default workflow while the
combined execution control remains provider/model independent. Unknown future
record fields are ignored on load.

Writes use an owner-only temporary file followed by an atomic replace.  A
malformed or partially unsupported file is treated as an empty/filtered store
rather than escaping into a GTK callback.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from helios.backend.project_perms import PERMISSION_MODES, SAFE_FALLBACK_MODE
from helios.backend.workflow_modes import (
    DEFAULT_WORKFLOW_MODE,
    WORKFLOW_MODES,
    canonical_workflow_mode,
)

# Retired permission modes and what a stored record becomes. "manual" ("Full
# access, ask") existed only in v0.52.0 before Bypass itself gained the Codex
# approval gate that superseded it.
#
# This must never resolve to "drop the record". A dropped conversation override
# falls through to the global default in MainWindow._execution_settings_for_spawn,
# so a conversation the user explicitly pinned to a *gated* mode could silently
# reopen under a confirmed global Bypass — a permission widening, and for Claude
# an ungated one. Mapping to the safe fallback only ever narrows.
_RETIRED_PERMISSION_MODES: dict[str, str] = {"manual": SAFE_FALLBACK_MODE}


def canonical_permission_mode(mode: str) -> str:
    """Map a stored permission mode onto one the UI still offers.

    Returns "" for anything unrecognised, which callers treat as "no stored
    override" — the same as a corrupt record.
    """
    resolved = _RETIRED_PERMISSION_MODES.get(mode, mode)
    return resolved if resolved in PERMISSION_MODES else ""
from helios.backend.ui_state import canonical_effort_key
from helios.log import get_logger
from helios.paths import state_dir

_log = get_logger("conversation_perms")
_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class ConversationExecutionSettings:
    """Explicit settings attached to one provider-native conversation."""

    permission_mode: str
    effort_key: str = ""
    workflow_mode: str = DEFAULT_WORKFLOW_MODE


def _key(provider: str, native_session_id: str) -> tuple[str, str]:
    """Return a normalized store key, or two empty strings if incomplete."""

    normalized_provider = str(provider or "").strip().lower()
    normalized_session_id = str(native_session_id or "").strip()
    if not normalized_provider or not normalized_session_id:
        return "", ""
    return normalized_provider, normalized_session_id


def _settings_from_json(value: Any) -> ConversationExecutionSettings | None:
    # Accept the original bare permission value as a tiny forward migration
    # seam for development builds created before records gained effort.
    if isinstance(value, str):
        permission_mode = value
        effort_key = ""
        workflow_mode = DEFAULT_WORKFLOW_MODE
    elif isinstance(value, dict):
        permission_mode = value.get("permission_mode")
        effort_value = value.get("effort_key", "")
        effort_key = effort_value.strip() if isinstance(effort_value, str) else ""
        workflow_value = value.get("workflow_mode", DEFAULT_WORKFLOW_MODE)
        workflow_mode = canonical_workflow_mode(workflow_value)
    else:
        return None
    if not isinstance(permission_mode, str):
        return None
    canonical = canonical_permission_mode(permission_mode)
    if not canonical:
        return None
    return ConversationExecutionSettings(
        permission_mode=canonical,
        effort_key=effort_key,
        workflow_mode=workflow_mode,
    )


def _settings_to_json(settings: ConversationExecutionSettings) -> dict[str, str]:
    value = {"permission_mode": settings.permission_mode}
    if settings.effort_key:
        value["effort_key"] = settings.effort_key
    if settings.workflow_mode != DEFAULT_WORKFLOW_MODE:
        value["workflow_mode"] = settings.workflow_mode
    return value


def _load(
    path: Path,
) -> tuple[
    dict[str, dict[str, ConversationExecutionSettings]],
    frozenset[tuple[str, str]],
]:
    if not path.is_file():
        return {}, frozenset()
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, frozenset({("__invalid__", "__all__")})
    if not isinstance(raw, dict):
        return {}, frozenset({("__invalid__", "__all__")})

    data: dict[str, dict[str, ConversationExecutionSettings]] = {}
    invalid_claims: set[tuple[str, str]] = set()
    for raw_provider, raw_sessions in raw.items():
        if not isinstance(raw_sessions, dict):
            # The corrupt namespace may contain any native id.  Absence can no
            # longer be proven for a particular lookup, so fail closed until a
            # successful write repairs the store.
            invalid_claims.add(("__invalid__", "__all__"))
            continue
        provider = (
            raw_provider.strip().lower()
            if isinstance(raw_provider, str)
            else "__invalid__"
        )
        if not provider:
            provider = "__invalid__"
        sessions: dict[str, ConversationExecutionSettings] = {}
        for raw_session_id, raw_settings in raw_sessions.items():
            session_id = (
                raw_session_id.strip()
                if isinstance(raw_session_id, str)
                else str(raw_session_id).strip()
            )
            if not session_id:
                continue
            settings = _settings_from_json(raw_settings)
            if settings is None:
                # Keep malformed evidence distinct from a valid provider
                # claim.  Collapsing this back to ``provider`` would let an
                # agreeing index hide the corrupt execution record.
                invalid_claims.add(("__invalid__", session_id))
                continue
            sessions[session_id] = settings
        if sessions:
            data[provider] = sessions
    return data, frozenset(invalid_claims)


def _save(
    path: Path,
    data: dict[str, dict[str, ConversationExecutionSettings]],
) -> bool:
    """Atomically persist *data*, returning whether it reached disk."""

    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

        # mkstemp creates the file as 0600 before any content is written, so
        # even the pre-rename artifact is never readable by other users.
        fd, temp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        raw = {
            provider: {
                session_id: _settings_to_json(settings)
                for session_id, settings in sessions.items()
            }
            for provider, sessions in data.items()
        }
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        return True
    except OSError as exc:
        _log.warning("could not save %s: %s", path.name, exc)
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


class ConversationPermsStore:
    """Thread-safe execution settings keyed by provider + native session id."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = (
            Path(path)
            if path is not None
            else (state_dir() / "conversation-perms.json")
        )
        with _LOCK:
            self._data, self._invalid_claims = _load(self._path)

    @property
    def path(self) -> Path:
        """The backing path, exposed for diagnostics and focused tests."""

        return self._path

    def get(
        self,
        provider: str,
        native_session_id: str,
        default: str = "",
    ) -> str:
        provider_key, session_key = _key(provider, native_session_id)
        if not provider_key:
            return default
        with _LOCK:
            settings = self._data.get(provider_key, {}).get(session_key)
            return settings.permission_mode if settings is not None else default

    def get_settings(
        self,
        provider: str,
        native_session_id: str,
    ) -> ConversationExecutionSettings | None:
        """Return the complete immutable record, if one is stored."""

        provider_key, session_key = _key(provider, native_session_id)
        if not provider_key:
            return None
        with _LOCK:
            return self._data.get(provider_key, {}).get(session_key)

    def get_effort(
        self,
        provider: str,
        native_session_id: str,
        default: str = "",
    ) -> str:
        settings = self.get_settings(provider, native_session_id)
        if settings is None or not settings.effort_key:
            return default
        # Conversations saved under a retired key (e.g. "ultracode") must not
        # silently degrade to the slider's fallback when they are reopened.
        return canonical_effort_key(settings.effort_key)

    def get_workflow(
        self,
        provider: str,
        native_session_id: str,
        default: str = DEFAULT_WORKFLOW_MODE,
    ) -> str:
        settings = self.get_settings(provider, native_session_id)
        if settings is None:
            return canonical_workflow_mode(default)
        return settings.workflow_mode

    def provider_for_session(self, native_session_id: str) -> str:
        """Recover a provider when exactly one namespace owns ``session_id``.

        The normal routing index is a separate atomic file.  This reverse
        lookup gives it a durable recovery source if that write failed while
        this conversation record succeeded.  An actual cross-provider id
        collision is intentionally ambiguous and returns an empty string.
        """

        matches = self.providers_for_session(native_session_id)
        return next(iter(matches)) if len(matches) == 1 else ""

    def providers_for_session(self, native_session_id: str) -> frozenset[str]:
        """Return every provider namespace claiming ``native_session_id``."""

        session_key = str(native_session_id or "").strip()
        if not session_key:
            return frozenset()
        with _LOCK:
            providers = {
                provider
                for provider, sessions in self._data.items()
                if session_key in sessions
            }
            providers.update(
                provider
                for provider, claimed_session in self._invalid_claims
                if claimed_session in (session_key, "__all__")
            )
            return frozenset(providers)

    def set(
        self,
        provider: str,
        native_session_id: str,
        mode: str,
        *,
        effort_key: str | None = None,
        workflow_mode: str | None = None,
    ) -> bool:
        """Persist settings; a permission-only update preserves saved effort.

        Passing ``effort_key=None`` leaves an existing effort unchanged.  An
        explicit empty string clears it.  Provider/model validation of a
        non-empty effort key belongs to the caller because catalogs evolve.
        """

        provider_key, session_key = _key(provider, native_session_id)
        if (
            not provider_key
            or mode not in PERMISSION_MODES
            or (effort_key is not None and not isinstance(effort_key, str))
            or (
                workflow_mode is not None
                and workflow_mode not in WORKFLOW_MODES
            )
        ):
            return False
        with _LOCK:
            current = self._data.get(provider_key, {}).get(session_key)
            selected_effort = (
                current.effort_key
                if effort_key is None and current is not None
                else str(effort_key or "").strip()
            )
            selected_workflow = (
                current.workflow_mode
                if workflow_mode is None and current is not None
                else canonical_workflow_mode(workflow_mode)
            )
            settings = ConversationExecutionSettings(
                mode,
                selected_effort,
                selected_workflow,
            )
            if current == settings:
                return True
            updated = {key: dict(sessions) for key, sessions in self._data.items()}
            updated.setdefault(provider_key, {})[session_key] = settings
            if not _save(self._path, updated):
                return False
            self._data = updated
            self._invalid_claims = frozenset()
            return True

    def set_effort(
        self,
        provider: str,
        native_session_id: str,
        effort_key: str,
        *,
        permission_mode: str | None = None,
    ) -> bool:
        """Update effort while retaining permission, or seed both settings.

        ``permission_mode`` is required only when the conversation does not
        have a record yet.  This keeps every persisted record independently
        valid while letting the combined control save effort first.
        """

        provider_key, session_key = _key(provider, native_session_id)
        if not provider_key or not isinstance(effort_key, str):
            return False
        with _LOCK:
            current = self._data.get(provider_key, {}).get(session_key)
            selected_mode = (
                current.permission_mode if current is not None else permission_mode
            )
            if selected_mode not in PERMISSION_MODES:
                return False
            settings = ConversationExecutionSettings(
                permission_mode=selected_mode,
                effort_key=effort_key.strip(),
                workflow_mode=(
                    current.workflow_mode
                    if current is not None
                    else DEFAULT_WORKFLOW_MODE
                ),
            )
            if current == settings:
                return True
            updated = {key: dict(sessions) for key, sessions in self._data.items()}
            updated.setdefault(provider_key, {})[session_key] = settings
            if not _save(self._path, updated):
                return False
            self._data = updated
            self._invalid_claims = frozenset()
            return True

    def set_workflow(
        self,
        provider: str,
        native_session_id: str,
        workflow_mode: str,
        *,
        permission_mode: str | None = None,
    ) -> bool:
        """Update workflow while preserving permission and reasoning effort."""

        provider_key, session_key = _key(provider, native_session_id)
        if not provider_key or workflow_mode not in WORKFLOW_MODES:
            return False
        with _LOCK:
            current = self._data.get(provider_key, {}).get(session_key)
            selected_mode = (
                current.permission_mode if current is not None else permission_mode
            )
            if selected_mode not in PERMISSION_MODES:
                return False
            settings = ConversationExecutionSettings(
                permission_mode=selected_mode,
                effort_key=current.effort_key if current is not None else "",
                workflow_mode=workflow_mode,
            )
            if current == settings:
                return True
            updated = {key: dict(sessions) for key, sessions in self._data.items()}
            updated.setdefault(provider_key, {})[session_key] = settings
            if not _save(self._path, updated):
                return False
            self._data = updated
            self._invalid_claims = frozenset()
            return True

    def delete(self, provider: str, native_session_id: str) -> bool:
        """Delete an override, returning whether an entry was removed."""

        provider_key, session_key = _key(provider, native_session_id)
        if not provider_key:
            return False
        with _LOCK:
            sessions = self._data.get(provider_key)
            if sessions is None or session_key not in sessions:
                return False
            updated = {
                key: dict(provider_sessions)
                for key, provider_sessions in self._data.items()
            }
            del updated[provider_key][session_key]
            if not updated[provider_key]:
                del updated[provider_key]
            if not _save(self._path, updated):
                return False
            self._data = updated
            self._invalid_claims = frozenset()
            return True


_global: ConversationPermsStore | None = None


def store() -> ConversationPermsStore:
    """Return the process-wide conversation permission store."""

    global _global
    if _global is None:
        _global = ConversationPermsStore()
    return _global


def reload() -> None:
    """Drop the singleton so tests/path changes re-read the active state dir."""

    global _global
    with _LOCK:
        _global = None
