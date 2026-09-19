from __future__ import annotations

import subprocess

from helios.backend import claude_binary


def test_transient_help_timeout_is_not_cached(monkeypatch, tmp_path):
    binary_path = tmp_path / "claude"
    binary_path.write_text("fixture", encoding="utf-8")
    binary = claude_binary.ClaudeBinary(binary_path, "test")
    calls: list[bool] = []

    monkeypatch.setattr(claude_binary, "find_claude_binary", lambda: binary)

    def timeout(*_args, **_kwargs):
        calls.append(True)
        raise subprocess.TimeoutExpired("claude --help", 4)

    monkeypatch.setattr(claude_binary.subprocess, "run", timeout)
    claude_binary._help_text_cache.clear()

    assert claude_binary.supports_max_budget_usd_flag() is False
    assert claude_binary.supports_max_budget_usd_flag() is False
    assert len(calls) == 2
    assert claude_binary._help_text_cache == {}


def test_capability_probe_is_shared_and_cached_on_success(monkeypatch, tmp_path):
    binary_path = tmp_path / "claude"
    binary_path.write_text("fixture", encoding="utf-8")
    binary = claude_binary.ClaudeBinary(binary_path, "test")
    calls: list[bool] = []

    monkeypatch.setattr(claude_binary, "find_claude_binary", lambda: binary)

    def help_result(*_args, **_kwargs):
        calls.append(True)
        return subprocess.CompletedProcess(
            [], 0, stdout="--effort --max-budget-usd", stderr=""
        )

    monkeypatch.setattr(claude_binary.subprocess, "run", help_result)
    claude_binary._help_text_cache.clear()

    assert claude_binary.supports_effort_flag() is True
    assert claude_binary.supports_max_budget_usd_flag() is True
    assert len(calls) == 1


def test_budget_family_enforcement_requires_documented_minimum_version(
    monkeypatch, tmp_path
):
    binary_path = tmp_path / "claude"
    binary_path.write_text("fixture", encoding="utf-8")
    binary = claude_binary.ClaudeBinary(binary_path, "test")
    monkeypatch.setattr(claude_binary, "find_claude_binary", lambda: binary)

    def result(argv, **_kwargs):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--max-budget-usd", "")
        return subprocess.CompletedProcess(argv, 0, "Claude Code 2.1.216", "")

    monkeypatch.setattr(claude_binary.subprocess, "run", result)
    claude_binary._help_text_cache.clear()
    claude_binary._version_cache.clear()
    assert claude_binary.supports_budget_family_enforcement() is False

    claude_binary._version_cache.clear()

    def current_result(argv, **_kwargs):
        if argv[-1] == "--help":
            return subprocess.CompletedProcess(argv, 0, "--max-budget-usd", "")
        return subprocess.CompletedProcess(argv, 0, "2.1.217 (Claude Code)", "")

    monkeypatch.setattr(claude_binary.subprocess, "run", current_result)
    assert claude_binary.supports_budget_family_enforcement() is True


def test_transient_version_failure_is_not_cached(monkeypatch, tmp_path):
    binary_path = tmp_path / "claude"
    binary_path.write_text("fixture", encoding="utf-8")
    binary = claude_binary.ClaudeBinary(binary_path, "test")
    calls: list[bool] = []
    monkeypatch.setattr(claude_binary, "find_claude_binary", lambda: binary)
    monkeypatch.setattr(
        claude_binary,
        "supports_max_budget_usd_flag",
        lambda: True,
    )

    def timeout(*_args, **_kwargs):
        calls.append(True)
        raise subprocess.TimeoutExpired("claude --version", 4)

    monkeypatch.setattr(claude_binary.subprocess, "run", timeout)
    claude_binary._version_cache.clear()

    assert claude_binary.supports_budget_family_enforcement() is False
    assert claude_binary.supports_budget_family_enforcement() is False
    assert len(calls) == 2
    assert claude_binary._version_cache == {}
