"""Every provider Helios spawns is told the tracker exists.

The credential grant in `env_scrub.NORVI_TRACKER_ENV` is inert on its own: a
session that holds `NORVI_TRACKER_API_TOKEN` but was never told `norvi-work`
exists will not use it. The two halves are load-bearing together, and they
live in different files, so this pins the second half per provider.

Claude has no other Helios-owned instruction channel — it reads CLAUDE.md
natively — so its half is a `--append-system-prompt` argument, checked at the
source level because the argv is assembled inside `start()`, which spawns.
"""

from __future__ import annotations

from pathlib import Path

import helios
from helios.backend.codex_context import (
    CODEX_DEVELOPER_INSTRUCTIONS,
    TRACKER_POLICY,
)

SRC = Path(helios.__file__).parent


def test_the_policy_names_the_sanctioned_client_and_the_binding():
    """It has to be actionable, not just aspirational.

    Helios ships no OpenProject client of its own (the tracker gateway repository's
    `docs/helios-adapter.md` forbids a second credential/client/outbox), so the
    policy must hand the session the CLI that is installed, and must say which
    work package this chat belongs to.
    """
    assert "norvi-work" in TRACKER_POLICY
    assert "OpenProject" in TRACKER_POLICY
    assert "OP#" in TRACKER_POLICY


def test_the_instance_is_named_by_the_environment_not_the_source(monkeypatch):
    import importlib
    from helios.backend import codex_context

    monkeypatch.setenv("NORVI_TRACKER_URL", "https://tracker.example.test")
    module = importlib.reload(codex_context)
    assert "OpenProject at https://tracker.example.test" in module.TRACKER_POLICY
    monkeypatch.delenv("NORVI_TRACKER_URL")
    module = importlib.reload(codex_context)
    assert "https://" not in module.TRACKER_POLICY


def test_gpt_sessions_get_it_through_developer_instructions():
    assert TRACKER_POLICY in CODEX_DEVELOPER_INSTRUCTIONS


def test_openrouter_sessions_get_it_in_the_system_prompt():
    # The OpenRouter driver imports gi at module level, and the GTK-free CI
    # lane has no gi — green on the workstation, red in CI without this guard. The
    # sibling tests stay source-level on purpose so that lane keeps them.
    import pytest

    pytest.importorskip("gi")
    from helios.backend.process.openrouter_driver import _SYSTEM_PROMPT

    assert TRACKER_POLICY in _SYSTEM_PROMPT


def test_claude_sessions_get_it_via_append_system_prompt():
    source = (SRC / "backend" / "process" / "cli_driver.py").read_text()

    assert '"--append-system-prompt", TRACKER_POLICY,' in source
