from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from helios.backend.capabilities import build_snapshot, timestamp


NOW = 1_780_000_000.0


def snapshot(driver=None, provider="openrouter", cwd="/selected"):
    return build_snapshot(driver, provider, cwd, now=NOW, host="test-execution-host")


def receipt(**overrides):
    return {"path": "/repo/AGENTS.md", "resolved_path": "/repo/AGENTS.md",
            "provider": "openrouter", "sha256": "a" * 64, "bytes": 240,
            "loaded_at": NOW - 100, "mtime_ns": int((NOW - 1000) * 10**9),
            "precedence": 0, **overrides}


def test_copies_actual_driver_identity_over_selected_provider_and_workspace():
    driver = SimpleNamespace(provider="openai", _cwd="/actual", model="gpt-native")
    report = snapshot(driver)
    assert report.provider == "openai"
    assert report.model == "gpt-native"
    assert report.cwd == "/actual" and report.cwd_reported
    assert report.execution_host == "test-execution-host"
    assert "Codex loads AGENTS.md natively" in report.instruction_note


def test_absent_report_does_not_claim_zero_connected_or_effective_context():
    report = snapshot()
    assert not report.attached and not report.tools_reported
    assert not report.servers and not report.instructions
    assert report.cwd == "/selected" and not report.cwd_reported
    assert "has not been verified" in report.instruction_note


def test_configured_inventory_and_auth_are_not_connection_evidence():
    report = snapshot(SimpleNamespace(init_tools=[], init_mcp_servers=[
        {"name": "pending-estate", "status": "configured"},
        {"name": "native-estate", "authStatus": "authenticated", "tools": {"search": {}}},
        {"name": "failed-estate", "status": "failed", "checked_at": NOW - 60},
    ]))
    assert [(r.status, r.tool_count) for r in report.servers] == [
        ("configured", None), ("Not reported", 1), ("failed", None)]
    assert report.servers[0].checked_at == "Not reported"
    assert report.servers[2].checked_at == timestamp(NOW - 60)
    assert report.servers[2].checked_at != report.copied_at


def test_native_instructions_never_adopt_openrouter_receipts_or_claude_content():
    driver = SimpleNamespace(provider="openai", init_instruction_sources=[receipt()],
                             claude_context="Private role instructions")
    report = snapshot(driver)
    assert not report.instructions
    assert "Private" not in repr(report)
    assert "no effective file receipts" in report.instruction_note


def test_receipts_keep_loaded_hash_time_and_precedence_without_rechecking_disk(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("The snapshot must not open files or start discovery")
    monkeypatch.setattr("builtins.open", forbidden)
    driver = SimpleNamespace(provider="openrouter", init_instruction_sources=[
        receipt(path="/repo/src/AGENTS.md", precedence=1), receipt(),
    ], start=forbidden, discover=forbidden)
    report = snapshot(driver)
    assert [r.precedence for r in report.instructions] == [0, 1]
    assert report.instructions[0].sha256 == "a" * 64
    assert report.instructions[0].loaded_at == timestamp(NOW - 100)
    assert report.instructions[0].modified_at == timestamp(NOW - 1000)
    assert "current disk freshness is unknown" in report.instruction_note


def test_unverified_or_wrong_provider_sources_are_not_presented_as_receipts():
    rows = [receipt(sha256="not-a-hash"), receipt(provider="anthropic"),
            receipt(bytes=-2), receipt(precedence=True), receipt(path="")]
    report = snapshot(SimpleNamespace(init_instruction_sources=rows))
    assert not report.instructions
    assert "has not been verified" in report.instruction_note


def test_whitelist_does_not_copy_env_schemas_errors_or_arbitrary_attributes():
    secret = "test-credential-that-must-not-display"
    driver = SimpleNamespace(init_tools=[{"name": "search", "inputSchema": {"secret": secret}}],
                             init_mcp_servers=[{"name": "estate", "status": "failed",
                                                "env": {"API_KEY": secret}, "error": secret}],
                             init_instruction_sources=[receipt(text=secret)], credential=secret)
    report = snapshot(driver)
    assert report.tools == ("search",)
    assert secret not in repr(asdict(report))


def test_snapshot_is_detached_from_later_report_mutation():
    driver = SimpleNamespace(init_tools=["search", "search"],
                             init_mcp_servers=[{"name": "estate", "status": "configured"}],
                             init_instruction_sources=[receipt()])
    report = snapshot(driver)
    driver.init_tools.append("write")
    driver.init_mcp_servers[0]["status"] = "connected"
    driver.init_instruction_sources[0]["sha256"] = "b" * 64
    assert report.tools == ("search",)
    assert report.servers[0].status == "configured"
    assert report.instructions[0].sha256 == "a" * 64


@pytest.mark.parametrize("value", [None, True, "today", float("nan"), float("inf"), -1, 10**400])
def test_invalid_timestamp_remains_unknown(value):
    assert timestamp(value) == "Not reported"
