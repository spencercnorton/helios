"""codex_env — credential-opaque live CLI auth truth. GTK-free."""

from __future__ import annotations

import shlex
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios.backend import codex_env as ce


def _fake_codex_status(
    tmp_path: Path,
    monkeypatch,
    text: str,
    *,
    returncode: int = 0,
) -> Path:
    """Install an inert fake CLI whose only observable behavior is login status."""
    binary = tmp_path / "fake-codex"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' {shlex.quote(text)}\n"
        f"exit {returncode}\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    monkeypatch.setattr(
        ce,
        "find_codex_binary",
        lambda: ce.CodexBinary(path=binary, source="env"),
    )
    return binary


def test_auth_mode_ignores_bare_parent_env(tmp_path: Path, monkeypatch):
    # A bare parent key is not a pinned-Codex login. Current mode comes only
    # from CLI status; Helios has no raw credential-reader seam.
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-parent-value")
    _fake_codex_status(tmp_path, monkeypatch, "Logged in using ChatGPT")
    assert ce.auth_mode() == "chatgpt"

    _fake_codex_status(
        tmp_path,
        monkeypatch,
        "Not logged in",
        returncode=1,
    )
    assert ce.auth_mode() == ""


def test_keyring_backed_login_is_live_without_auth_json(tmp_path: Path, monkeypatch):
    """A fake CLI stands in for keyring/auto storage opaque to Helios."""
    _fake_codex_status(tmp_path, monkeypatch, "Logged in using ChatGPT")

    status = ce.fetch_auth_status()

    assert status.ok and status.logged_in
    assert status.mode == "chatgpt"
    assert status.detail == "ChatGPT account"
    assert ce.auth_mode(status) == "chatgpt"


def test_logged_out_cli_cannot_be_overridden_by_file_metadata(
    tmp_path: Path, monkeypatch,
):
    _fake_codex_status(tmp_path, monkeypatch, "Not logged in", returncode=1)

    assert ce.auth_mode() == ""
    assert not hasattr(ce, "read_api_key")


def test_cli_status_modes_are_classified_without_echoing_cli_output(
    tmp_path: Path,
    monkeypatch,
):
    _fake_codex_status(tmp_path, monkeypatch, "Logged in using an API key")
    api = ce.fetch_auth_status()
    assert api.mode == "apikey"
    assert api.detail == "OpenAI API key"

    _fake_codex_status(tmp_path, monkeypatch, "Logged in using an access token")
    access = ce.fetch_auth_status()
    assert access.mode == "access_token"
    assert access.detail == "OpenAI access token"


def test_login_rejects_empty_key():
    ok, msg = ce.login_with_api_key("   ")
    assert not ok
    assert "Empty" in msg


def test_codex_mcp_snapshot_round_trip(tmp_path: Path, monkeypatch):
    snapshot = tmp_path / "codex-mcp.json"
    monkeypatch.setattr(ce, "_MCP_SNAPSHOT_PATH", snapshot)

    ce.save_mcp_snapshot(
        [{"name": "code-intel", "status": "ready", "tools": {"search": {}}}]
    )

    assert ce.load_mcp_snapshot() == [
        {"name": "code-intel", "status": "ready", "tools": {"search": {}}}
    ]


def test_invalid_codex_mcp_snapshot_is_empty(tmp_path: Path, monkeypatch):
    snapshot = tmp_path / "codex-mcp.json"
    snapshot.write_text("not json")
    monkeypatch.setattr(ce, "_MCP_SNAPSHOT_PATH", snapshot)
    assert ce.load_mcp_snapshot() == []


def test_codex_probe_login_and_version_children_get_no_provider_env(monkeypatch):
    monkeypatch.setattr(
        ce,
        "find_codex_binary",
        lambda: ce.CodexBinary(path=Path("/fake/codex"), source="env"),
    )
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(name, "fixture")

    seen: list[set[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(set(kwargs["env"]))
        stdout = "codex-cli test" if argv[-1] == "--version" else "Not logged in"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(ce.subprocess, "run", fake_run)
    ce.fetch_auth_status()
    ce.login_with_api_key("sk-fixture")
    ce.codex_version()

    assert len(seen) == 3
    forbidden = {
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "ANTHROPIC_API_KEY",
    }
    assert all(names.isdisjoint(forbidden) for names in seen)


def test_app_server_model_discovery_gets_no_provider_env(monkeypatch):
    monkeypatch.setattr(
        ce,
        "find_codex_binary",
        lambda: ce.CodexBinary(path=Path("/fake/codex"), source="env"),
    )
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(name, "fixture")

    captured: dict = {}

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = StringIO()
            self.stdout = StringIO()

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(ce.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        ce,
        "_read_rpc_response",
        lambda _proc, request_id, **_kwargs: (
            {} if request_id == 1 else {"data": []}
        ),
    )

    assert ce.fetch_app_server_models() == []
    child_names = set(captured["env"])
    assert child_names.isdisjoint(
        {
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "CODEX_ACCESS_TOKEN",
            "ANTHROPIC_API_KEY",
        }
    )


def test_model_catalog_survives_missing_optional_workflow_discovery(monkeypatch):
    monkeypatch.setattr(
        ce,
        "find_codex_binary",
        lambda: ce.CodexBinary(path=Path("/fake/codex"), source="env"),
    )

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = StringIO()
            self.stdout = StringIO()

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ce.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())

    def response(_proc, request_id, **_kwargs):
        if request_id == 1:
            return {}
        if request_id == 2:
            return {"data": [{"id": "gpt-test"}]}
        raise ce.CodexAppServerError("method not found")

    monkeypatch.setattr(ce, "_read_rpc_response", response)

    capabilities = ce.fetch_app_server_capabilities()
    assert capabilities.models == ({"id": "gpt-test"},)
    assert capabilities.collaboration_modes == ()


def test_is_subscription_billing_tracks_auth_mode(monkeypatch):
    """A ChatGPT login is a subscription; an API key is real per-token spend."""

    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    monkeypatch.setattr(ce, "_billing_cache", {})
    monkeypatch.setattr(ce, "auth_mode", lambda: "chatgpt")
    assert ce.is_subscription_billing() is True

    for mode in ("apikey", "access_token", "authenticated"):
        monkeypatch.setattr(ce, "_billing_cache", {})
        monkeypatch.setattr(ce, "auth_mode", lambda mode=mode: mode)
        assert ce.is_subscription_billing() is False, mode

    # Logged out / unreadable status fails CLOSED, keeping the cap.
    monkeypatch.setattr(ce, "_billing_cache", {})
    monkeypatch.setattr(ce, "auth_mode", lambda: "")
    assert ce.is_subscription_billing() is False


def test_explicit_api_key_outranks_a_chatgpt_login(monkeypatch):
    monkeypatch.setattr(ce, "_billing_cache", {})
    monkeypatch.setattr(ce, "auth_mode", lambda: "chatgpt")
    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    assert ce.is_subscription_billing() is False


def test_billing_result_is_cached_until_refresh(monkeypatch):
    """The cache exists, and `refresh=True` bypasses it."""

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(ce, "_billing_cache", {})
    calls = []

    def mode():
        calls.append(1)
        return "chatgpt"

    monkeypatch.setattr(ce, "auth_mode", mode)
    assert ce.is_subscription_billing() is True
    assert ce.is_subscription_billing() is True
    assert len(calls) == 1

    assert ce.is_subscription_billing(refresh=True) is True
    assert len(calls) == 2


def test_a_login_change_is_not_masked_by_the_cache(monkeypatch):
    """The cache is process-wide and Helios outlives a `codex login`.

    The dangerous direction is subscription → per-token: a stale permissive
    answer would keep the cap off while real money is being spent. Every
    budget resolution therefore refreshes.
    """

    # codex_app_driver imports gi at module scope. Everything else in this
    # module is GTK-free and must keep running on the slim CI lane, so the
    # skip is scoped to this test rather than the file.
    pytest.importorskip("gi")
    from helios.backend.process.codex_app_driver import (
        CODEX_STANDARD_TOKEN_BUDGET,
        default_token_budget,
    )

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(ce, "_billing_cache", {})

    monkeypatch.setattr(ce, "auth_mode", lambda: "chatgpt")
    assert default_token_budget() is None

    # User runs `codex login --with-api-key` — no restart, same process.
    monkeypatch.setattr(ce, "auth_mode", lambda: "apikey")
    assert default_token_budget() == CODEX_STANDARD_TOKEN_BUDGET

    # ...and back again.
    monkeypatch.setattr(ce, "auth_mode", lambda: "chatgpt")
    assert default_token_budget() is None
