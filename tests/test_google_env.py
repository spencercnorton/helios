"""Portable subscription discovery and billing-route regression tests."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from helios.backend import google_env as ge
from helios.backend import model_catalog as mc


USAGE_RESULT = {
    "status": "SUCCESS", "response": "", "conversation_id": "",
    "command": {
        "name": "usage",
        "data": {"groups": [{
            "name": "Gemini Models",
            "buckets": [{"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.98}],
        }]},
    },
}


@pytest.fixture
def agy(monkeypatch, tmp_path):
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HELIOS_GOOGLE_BINARY", str(binary))
    monkeypatch.setattr(ge, "_AUTH_CACHE", None)
    return binary


def write_settings(tmp_path, contents):
    path = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contents))
    return path


def test_invalid_override_does_not_fall_back_to_another_cli(monkeypatch):
    monkeypatch.setenv("HELIOS_GOOGLE_BINARY", "/missing/explicit-agy")
    monkeypatch.setattr(ge.shutil, "which", lambda _name: "/unrelated/gemini")
    with pytest.raises(ge.GoogleBinaryNotFound, match="not an executable"):
        ge.find_google_binary()


def test_gemini_cli_is_not_an_antigravity_candidate(monkeypatch, tmp_path):
    monkeypatch.delenv("HELIOS_GOOGLE_BINARY", raising=False)
    checked = []

    def which(name):
        checked.append(name)
        return None

    monkeypatch.setattr(ge.shutil, "which", which)
    monkeypatch.setattr(ge.os, "access", lambda *_args: False)
    with pytest.raises(ge.GoogleBinaryNotFound):
        ge.find_google_binary()
    assert "gemini" not in checked
    assert all(not name.endswith("/gemini") for name in checked)


@pytest.mark.parametrize("provider", ["gemini", "vertex", "custom", None, 7])
def test_api_provider_settings_are_refused(provider, agy, tmp_path, monkeypatch):
    write_settings(tmp_path, {"modelProvider": provider})
    monkeypatch.setattr(ge.subprocess, "run", lambda *_a, **_k: pytest.fail("must not contact API"))
    assert ge.subscription_configuration_error()
    with pytest.raises(ge.GoogleSubscriptionRequired):
        ge.google_subscription_env()
    status = ge.fetch_auth_status()
    assert status.logged_in is False
    assert status.models == ()
    assert status.ok is False


def test_malformed_settings_fail_closed(agy, tmp_path):
    path = write_settings(tmp_path, {})
    path.write_text("{ broken")
    assert "Cannot verify" in ge.subscription_configuration_error()


@pytest.mark.parametrize("credits", [True, "false", 0, None, {}])
def test_ai_credit_overages_cannot_bypass_subscription_only_route(credits, agy, tmp_path):
    write_settings(tmp_path, {"useG1Credits": credits})
    with pytest.raises(ge.GoogleSubscriptionRequired, match="AI-credit"):
        ge.google_subscription_env()
    assert not ge.fetch_auth_status().ok
    assert ge.cached_auth_status() is None


def test_explicit_ai_credit_fallback_disabled_is_allowed(agy, tmp_path):
    write_settings(tmp_path, {"useG1Credits": False})
    assert ge.subscription_configuration_error() == ""
    assert ge.google_subscription_env()


def test_subscription_env_drops_api_and_cloud_route_overrides(agy, monkeypatch):
    names = (
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS", "GEMINI_BASE_URL", "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL", "AGY_MODEL_PROVIDER", "CLOUDSDK_CORE_PROJECT",
    )
    for name in names:
        monkeypatch.setenv(name, "must-not-inherit")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = ge.google_subscription_env()
    assert all(name not in env for name in names)
    assert "HELIOS_GOOGLE_BINARY" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"]


def test_catalog_parser_keeps_exact_google_rows_and_labels():
    output = (
        "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
        "claude-5.5-thinking\tClaude 5.5\n"
        "gemini-3.1-pro-low\tGemini 3.1 Pro (Low)\n"
        "gemini-3.8-flash-high\tDuplicate\n"
        "unknown header\n"
        "gemini-invalid model\tUnsafe identifier\n"
        "gemini-no-label\t\n"
    )
    assert ge.parse_model_catalog(output) == (
        ge.GoogleModel("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
        ge.GoogleModel("gemini-3.1-pro-low", "Gemini 3.1 Pro (Low)"),
    )


def test_directory_and_api_key_never_prove_sign_in(agy, tmp_path, monkeypatch):
    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-enable-api")
    monkeypatch.setattr(ge.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=""))
    status = ge.fetch_auth_status()
    assert not status.logged_in
    assert not status.ok
    assert status.email == ""
    assert status.models == ()


def test_live_catalog_is_cached_and_available_without_auth_proof(agy, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] <= 8
        if argv[1:] == ["models"]:
            return SimpleNamespace(returncode=0, stdout="gemini-3.8-flash-high\tGemini Flash High\n")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(ge.subprocess, "run", run)
    status = ge.fetch_auth_status()
    assert status.ok
    assert not status.logged_in
    assert status.email == ""
    assert status.plan_name == ""
    entries, source = mc.google_entries(auth=status)
    assert source == "agy-models"
    assert [entry.id for entry in entries] == ["gemini-3.8-flash-high"]
    assert entries[0].label == "Gemini Flash High"
    assert "context" not in entries[0].description
    first_calls = len(calls)
    assert ge.fetch_auth_status().models == status.models
    assert ge.cached_auth_status().models == status.models
    assert len(calls) == first_calls
    ge.fetch_auth_status(force=True)
    assert len(calls) == first_calls * 2


def test_native_quota_success_proves_sign_in_without_inventing_an_identity(agy, monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["models"]:
            return SimpleNamespace(returncode=0, stdout="gemini-3.8-flash-high\tGemini Flash High\n")
        assert argv[1:] == ["--print", "/usage", "--output-format", "json"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(USAGE_RESULT))

    monkeypatch.setattr(ge.subprocess, "run", run)
    status = ge.fetch_auth_status()
    assert status.logged_in
    assert status.auth_method == "oauth-quota"
    assert status.email == ""
    assert status.auth_warning == ""
    assert "not reported" in status.plan_name
    assert status.raw == {}
    assert len(calls) == 2


@pytest.mark.parametrize("result", [
    {},
    [],
    {**USAGE_RESULT, "status": "ERROR"},
    {**USAGE_RESULT, "command": {"name": "other", "data": USAGE_RESULT["command"]["data"]}},
    {**USAGE_RESULT, "command": {"name": "usage", "data": {"groups": []}}},
    {**USAGE_RESULT, "command": {"name": "usage", "data": {"groups": [{"name": "Gemini", "buckets": []}]}}},
])
def test_malformed_or_failed_usage_cannot_prove_authentication(result):
    assert not ge.account_quota_verified(json.dumps(result))


def test_quota_timeout_retains_honest_model_catalog(agy, monkeypatch):
    def run(argv, **kwargs):
        if argv[1:] == ["models"]:
            return SimpleNamespace(returncode=0, stdout="gemini-3.8-flash-high\tGemini Flash High\n")
        raise subprocess.TimeoutExpired("agy", kwargs["timeout"])

    monkeypatch.setattr(ge.subprocess, "run", run)
    status = ge.fetch_auth_status()
    assert status.ok
    assert not status.logged_in
    assert status.models
    assert status.auth_warning


def test_api_setting_change_invalidates_a_successful_catalog(agy, tmp_path, monkeypatch):
    monkeypatch.setattr(ge.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout="gemini-3.8-flash-high\tGemini Flash High\n",
    ))
    assert ge.fetch_auth_status().models
    write_settings(tmp_path, {"modelProvider": "gemini"})
    assert not ge.fetch_auth_status().ok
    assert ge.cached_auth_status() is None
    assert mc.google_entries() == ([], "unavailable")


def test_timeout_never_restores_invented_models(agy, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("agy", kwargs["timeout"])

    monkeypatch.setattr(ge.subprocess, "run", run)
    status = ge.fetch_auth_status()
    assert not status.ok
    assert status.models == ()
    assert mc.google_entries(auth=status)[0] == []


def test_cached_ui_lookup_never_starts_a_subprocess(agy, monkeypatch):
    monkeypatch.setattr(ge.subprocess, "run", lambda *_a, **_k: pytest.fail("UI must not block"))
    assert mc.google_entries(cached_only=True) == ([], "discovery-pending")


def test_empty_catalog_has_no_executable_default():
    assert mc.preferred_google_model([]) == ""
    assert mc.context_window_for("gemini-3.8-flash-high") == 0
