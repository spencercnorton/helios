"""A handoff's resume line has to actually work.

`_resume_command` branched on OpenAI and then fell through to
`claude --resume` for everything else — so an **OpenRouter** session, whose
agent loop runs in-process and whose state lives in
`~/.helios/openrouter-sessions/`, was handed a command for a CLI it does not
have. The implicit else is the defect: any provider added later inherits
Claude's command the same way, silently.

These tests pin one line per provider and pin that an unknown provider now
fails closed rather than impersonating Claude.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from helios.backend import model_catalog, session_handoff, session_providers


def _session(sid: str = "s-1") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=sid,
        path=Path("/tmp/does-not-matter.jsonl"),
        project=SimpleNamespace(cwd="/home/alice/work"),
        mtime=0.0,
    )


@pytest.fixture
def as_provider(monkeypatch):
    def _apply(provider: str, known: bool = True):
        monkeypatch.setattr(
            session_providers,
            "resolve_provider",
            lambda *_a, **_k: SimpleNamespace(known=known, provider=provider),
        )

    return _apply


def test_anthropic_resumes_through_the_claude_cli(as_provider) -> None:
    as_provider(model_catalog.PROVIDER_ANTHROPIC)
    assert session_handoff._resume_command(_session()) == "claude --resume s-1"


def test_openai_resumes_through_codex_exec(as_provider) -> None:
    """Verified against codex-cli 0.146.1: `exec` still carries `resume`.
    AGENTS.md retires Helios's automatic `codex exec` DRIVER fallback, which is
    a different thing from the command a human runs."""

    as_provider(model_catalog.PROVIDER_OPENAI)
    assert session_handoff._resume_command(_session()) == "codex exec resume s-1"


def test_openrouter_has_no_shell_resume(as_provider) -> None:
    """The defect. It used to return `claude --resume s-1` for a session with
    no CLI at all."""

    as_provider(model_catalog.PROVIDER_OPENROUTER)
    assert session_handoff._resume_command(_session()) is None


def test_an_unknown_provider_fails_closed_instead_of_impersonating_claude(
    as_provider,
) -> None:
    """The root cause: an implicit else. A provider added tomorrow must not
    inherit Claude's command by omission."""

    as_provider("some-future-provider")
    with pytest.raises(ValueError, match="some-future-provider"):
        session_handoff._resume_command(_session())


def test_unresolved_provider_still_raises(as_provider) -> None:
    as_provider("", known=False)
    with pytest.raises(ValueError, match="unresolved"):
        session_handoff._resume_command(_session())


# --- the two consumers must not paste None into their output ---------------


def test_summary_says_so_rather_than_printing_none(as_provider, monkeypatch) -> None:
    as_provider(model_catalog.PROVIDER_OPENROUTER)
    monkeypatch.setattr(session_handoff, "host_name", lambda: "workstation")
    monkeypatch.setattr(
        session_handoff, "default_recipient_provider", lambda *_a, **_k: "anthropic"
    )

    text = session_handoff.default_summary(_session(), "Some title")

    assert "None" not in text
    assert "no CLI" in text


def test_payload_does_not_build_a_broken_shell_line(as_provider, monkeypatch) -> None:
    as_provider(model_catalog.PROVIDER_OPENROUTER)
    monkeypatch.setattr(session_handoff, "host_name", lambda: "workstation")
    monkeypatch.setattr(
        session_handoff, "default_recipient_provider", lambda *_a, **_k: "anthropic"
    )

    payload = session_handoff.build_payload(
        _session(), "Some title", include_tail=False
    )

    resume = payload["resume"]
    assert "None" not in resume
    # No `cd … && <prose>` — that would look runnable and silently fail.
    assert "&&" not in resume
    assert "/home/alice/work" in resume
