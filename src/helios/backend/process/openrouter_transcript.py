"""OpenRouter transcript mirror — a thin Claude-format JSONL writer.

OpenRouter has no native session store, so Helios mirrors each turn into a
Claude-compatible project JSONL (the same scheme Codex uses) so the sidebar,
transcript view, title generation, and provider routing all work for free.

The authoritative message history for resume lives in
``openrouter.history`` — this mirror is display-only, with the same elision
caps as the Codex mirror.
"""

from __future__ import annotations

from helios.backend import model_catalog
from helios.backend.process.codex_transcript import CodexTranscriptWriter

__all__ = ["OpenRouterTranscriptWriter"]


class OpenRouterTranscriptWriter(CodexTranscriptWriter):
    """Mirrors OpenRouter turns into a Claude-format project JSONL."""

    def __init__(self, cwd: str, thread_id: str = "") -> None:
        super().__init__(cwd, thread_id, provider=model_catalog.PROVIDER_OPENROUTER)
