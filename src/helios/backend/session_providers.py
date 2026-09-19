"""Resolve which backend produced a given session without guessing.

Claude sessions are self-describing (the `claude` CLI writes its own project
JSONLs), but they're all Anthropic. Codex/GPT sessions are mirrored to disk by
Helios itself (see [process.codex_transcript]); to know a session's provider
without re-reading every transcript on every sidebar render, we keep a tiny
side index: session_id -> provider.

The index is consulted in two places:
  * the sidebar, to render the per-row GPT/Claude chip;
  * MainWindow, to route a resume to the right driver (Codex vs claude).

A missing entry means *unknown*.  Passing an opaque native id to the wrong CLI
can create a new conversation while presenting it as a resume, so routing
callers must require positive, non-conflicting evidence. JSON under
``~/.helios/`` is owner-only, mirroring [project_names] / [ui_state].
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from helios.backend import model_catalog
from helios.log import get_logger
from helios.paths import state_dir

_lock = threading.Lock()
_cache: dict[str, str] | None = None
_cache_malformed = False
_log = get_logger("session-providers")
_KNOWN_PROVIDERS = frozenset(
    {
        model_catalog.PROVIDER_ANTHROPIC,
        model_catalog.PROVIDER_OPENAI,
        model_catalog.PROVIDER_OPENROUTER,
    }
)
_CODEX_MIRROR_VERSION = "helios-codex"
_OPENROUTER_MIRROR_VERSION = "helios-openrouter"
_CLAUDE_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True, slots=True)
class ProviderResolution:
    """Provider evidence for one transcript/native conversation."""

    provider: str = ""
    sources: tuple[str, ...] = ()
    conflicted: bool = False

    @property
    def known(self) -> bool:
        return self.provider in _KNOWN_PROVIDERS and not self.conflicted


def _path() -> Path:
    """Resolve the provider-index path at call time.

    Reading ``state_dir()`` on every call means that changing
    ``HELIOS_STATE_DIR`` (e.g. between test fixtures) is automatically
    respected without needing to patch a module-level constant.
    """
    return state_dir() / "session-providers.json"


def _load() -> dict[str, str]:
    global _cache, _cache_malformed
    if _cache is not None:
        return _cache
    p = _path()
    if not p.is_file():
        _cache = {}
        _cache_malformed = False
        return _cache
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _cache = {}
        _cache_malformed = True
        return _cache
    if not isinstance(data, dict):
        _cache = {}
        _cache_malformed = True
        return _cache
    _cache_malformed = False
    _cache = {
        str(k): (v if isinstance(v, str) else "__invalid_provider_value__")
        for k, v in data.items()
    }
    return _cache


def _save(data: dict[str, str]) -> bool:
    # See project_names._save for the 0700/0600 rationale.
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(p.parent, 0o700)
        except OSError:
            pass
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(p)
        return True
    except OSError as e:
        _log.warning("could not save session provider index: %s", e)
        return False


@lru_cache(maxsize=2048)
def _provider_from_transcript(
    path_text: str,
    session_id: str,
    mtime_ns: int,
    size: int,
) -> ProviderResolution:
    """Inspect provider-authored version markers from one stable file image."""

    del mtime_ns, size  # cache-key material; the path is the value source
    providers: set[str] = set()
    try:
        with Path(path_text).open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"version"' not in line and '"sessionId"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                recorded_id = record.get("sessionId")
                if (
                    isinstance(recorded_id, str)
                    and recorded_id
                    and recorded_id != session_id
                ):
                    return ProviderResolution(
                        sources=("transcript-session-id-conflict",),
                        conflicted=True,
                    )
                # A version marker is provider evidence only on a record that
                # explicitly owns this exact native id.  Arbitrary app JSONL
                # files and records without sessionId must remain unknown.
                if recorded_id != session_id:
                    continue
                version = record.get("version")
                if version == _CODEX_MIRROR_VERSION:
                    providers.add(model_catalog.PROVIDER_OPENAI)
                elif version == _OPENROUTER_MIRROR_VERSION:
                    providers.add(model_catalog.PROVIDER_OPENROUTER)
                elif (
                    isinstance(version, str)
                    and _CLAUDE_VERSION_RE.fullmatch(version.strip()) is not None
                ):
                    providers.add(model_catalog.PROVIDER_ANTHROPIC)
                if len(providers) > 1:
                    return ProviderResolution(
                        sources=("transcript-conflict",),
                        conflicted=True,
                    )
    except OSError:
        return ProviderResolution()
    if not providers:
        return ProviderResolution()
    return ProviderResolution(
        provider=next(iter(providers)),
        sources=("transcript",),
    )


def resolve_provider(
    session_id: str,
    transcript_path: Path | None = None,
    *,
    conversation_store=None,
) -> ProviderResolution:
    """Collect all available provider evidence and fail closed on conflict.

    The routing index is authoritative in the common path. If its atomic write
    failed, the per-conversation execution record is a second durable witness;
    the provider-authored transcript version marker is a third. Evidence is
    collected before authority is ranked: disagreement is a conflict, never a
    reason to guess Claude for a GPT thread (or vice versa).
    """
    if not session_id:
        return ProviderResolution()
    evidence: list[tuple[str, str]] = []
    with _lock:
        data = _load()
        index_malformed = _cache_malformed
        recorded_present = session_id in data
        recorded = data.get(session_id, "")
    if index_malformed:
        return ProviderResolution(sources=("index-unreadable",), conflicted=True)
    if recorded_present and recorded not in _KNOWN_PROVIDERS:
        return ProviderResolution(sources=("index-invalid",), conflicted=True)
    if recorded in _KNOWN_PROVIDERS:
        evidence.append(("index", recorded))
    try:
        from helios.backend import conversation_perms

        settings_store = conversation_store or conversation_perms.store()
        conversation_owners = settings_store.providers_for_session(session_id)
    except Exception:
        return ProviderResolution(
            sources=("conversation-settings-unreadable",),
            conflicted=True,
        )
    if conversation_owners - _KNOWN_PROVIDERS or len(conversation_owners) > 1:
        return ProviderResolution(
            sources=("conversation-settings-conflict",),
            conflicted=True,
        )
    if conversation_owners:
        evidence.append(
            ("conversation-settings", next(iter(conversation_owners)))
        )
    if transcript_path is not None:
        try:
            stat = Path(transcript_path).stat()
        except OSError:
            transcript = ProviderResolution()
        else:
            transcript = _provider_from_transcript(
                str(Path(transcript_path)),
                session_id,
                stat.st_mtime_ns,
                stat.st_size,
            )
        if transcript.conflicted:
            return transcript
        if transcript.known:
            evidence.append(("transcript", transcript.provider))
    providers = {provider for _source, provider in evidence}
    sources = tuple(source for source, _provider in evidence)
    if len(providers) > 1:
        return ProviderResolution(sources=sources, conflicted=True)
    if not providers:
        return ProviderResolution()
    provider = next(iter(providers))
    return ProviderResolution(provider=provider, sources=sources)


def chip_style(resolution: ProviderResolution | None) -> tuple[str, str, str]:
    """(label, css class, tooltip) for a sidebar backend chip.

    ``None`` means "not resolved yet", which is NOT the same claim as
    "unknown": it renders as an ellipsis, so a pending row never asserts
    ownership it has not established.
    """
    if resolution is None:
        return (
            "…",
            "helios-provider-unknown",
            "Resolving which backend owns this session…",
        )
    if resolution.known:
        if resolution.provider == model_catalog.PROVIDER_OPENAI:
            return ("GPT", "helios-provider-gpt", "GPT (OpenAI) session")
        if resolution.provider == model_catalog.PROVIDER_OPENROUTER:
            return ("OR", "helios-provider-openrouter", "OpenRouter session")
        if resolution.provider == model_catalog.PROVIDER_ANTHROPIC:
            return ("Claude", "helios-provider-claude", "Claude (Anthropic) session")
        # Explicit, not a fallthrough. `known` only means the provider is in
        # _KNOWN_PROVIDERS and unconflicted; letting anything else land on the
        # Claude branch makes an ownership claim the evidence does not support
        # the moment a fourth provider, a stale value or a typo appears. This
        # chip is a fail-closed surface — falling through to Unknown is the
        # whole contract.
    if resolution.conflicted:
        return (
            "Conflict",
            "helios-provider-conflict",
            "Provider evidence conflicts — view only",
        )
    return (
        "Unknown",
        "helios-provider-unknown",
        "Provider ownership is unknown — view only",
    )


def provider_for(session_id: str) -> str:
    """Return a positively resolved provider, or empty when unsafe to infer."""

    return resolve_provider(session_id).provider


def is_openai(session_id: str) -> bool:
    return provider_for(session_id) == model_catalog.PROVIDER_OPENAI


def is_openrouter(session_id: str) -> bool:
    return provider_for(session_id) == model_catalog.PROVIDER_OPENROUTER


def set_provider(session_id: str, provider: str) -> bool:
    """Record an explicit provider claim for ``session_id``.

    Both providers are persisted.  Absence is reserved for unknown ownership,
    never used as an implicit Claude claim.
    """
    if not session_id or provider not in _KNOWN_PROVIDERS:
        return False
    global _cache, _cache_malformed
    with _lock:
        data = _load()
        if data.get(session_id) == provider:
            return True
        if data.get(session_id) in _KNOWN_PROVIDERS:
            _log.error(
                "refusing to overwrite %s provider identity from %s to %s",
                session_id,
                data[session_id],
                provider,
            )
            return False
        updated = dict(data)
        updated[session_id] = provider
        if not _save(updated):
            return False
        _cache = updated
        _cache_malformed = False
        return True


def forget(session_id: str) -> bool:
    """Drop a session's entry (e.g. when its transcript is deleted)."""
    if not session_id:
        return False
    global _cache, _cache_malformed
    with _lock:
        data = _load()
        if session_id not in data:
            if _cache_malformed:
                if not _save(dict(data)):
                    return False
                _cache_malformed = False
            return True
        updated = dict(data)
        del updated[session_id]
        if not _save(updated):
            return False
        _cache = updated
        _cache_malformed = False
        return True


def reload() -> None:
    """Drop the in-memory cache so the next read re-reads the file."""
    global _cache, _cache_malformed
    with _lock:
        _cache = None
        _cache_malformed = False
    _provider_from_transcript.cache_clear()
