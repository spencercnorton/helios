"""GTK-free contracts for Muse discovery, catalog, and account probes."""

from __future__ import annotations

import uuid

import pytest

from helios.backend import model_catalog, muse_env, project_perms
from helios.backend import muse_binary as mb
from helios.backend.muse_binary import MuseBinaryNotFound
from helios.backend.provider_identity import (
    DEFAULT_PROVIDER_REGISTRY,
    MUSE_NATIVE,
    NativeBindingKind,
    ProviderDescriptor,
    RuntimeKind,
)
from helios.backend.muse_env import MuseAuthStatus, MuseModel


def test_uuid7_is_version_7():
    value = uuid.UUID(muse_env.uuid7())
    assert value.version == 7


def test_parse_models_keeps_live_rows_and_efforts():
    models = muse_env._parse_models({
        "models": [{
            "modelId": "muse-wonder-1.0", "displayLabel": "muse-wonder-1.0",
            "contextLimit": 1007997, "outputLimit": 128000,
            "variants": ["minimal", "high", "bogus"],
            "defaultReasoningEffort": "high", "isDefault": True,
        }, {"displayLabel": "no id — dropped"}],
    })
    assert len(models) == 1
    assert models[0].id == "muse-wonder-1.0"
    assert models[0].context_limit == 1007997
    assert models[0].reasoning_efforts == (("minimal", "minimal"), ("high", "high"))
    assert models[0].is_default is True


def test_parse_models_tolerates_unknown_variants_string():
    # Echo hosts report variants:"unknown" — no efforts may be invented.
    models = muse_env._parse_models({"models": [{
        "modelId": "muse-spark-1.3", "displayLabel": "muse-spark-1.3",
        "variants": "unknown"}]})
    assert models[0].reasoning_efforts == ()


def test_fetch_auth_status_without_binary_never_raises(monkeypatch):
    monkeypatch.setattr(muse_env, "_cache", None)

    def missing():
        raise MuseBinaryNotFound("no muse here")

    monkeypatch.setattr(muse_env, "find_muse_binary", missing)
    status = muse_env.fetch_auth_status(force=True)
    assert status.logged_in is False
    assert status.catalog_status == "no-binary"
    assert "no muse here" in status.error


def test_set_api_key_refuses_blank_without_spawning(monkeypatch):
    monkeypatch.setattr(muse_env, "_muse_path", lambda: None)
    ok, _msg = muse_env.set_api_key("   ")
    assert ok is False


def test_provider_classification_and_window():
    assert model_catalog.provider_for("muse-wonder-full-1.0") == model_catalog.PROVIDER_MUSE
    # A vendor/model id is an OpenRouter route, never the native provider.
    assert model_catalog.provider_for("meta/muse-wonder-1.0") == model_catalog.PROVIDER_OPENROUTER
    assert model_catalog.context_window_for("muse-wonder-1.0") == 1007997
    # Native Muse models stay out of OpenRouter choices.
    assert model_catalog.openrouter_model_selectable("meta/muse-wonder-1.0") is False


def test_muse_entries_require_live_proof():
    auth = MuseAuthStatus(
        True, state="accountLogin", catalog_status="muse-serve",
        models=(MuseModel("muse-wonder-1.0", "muse-wonder-1.0",
                          context_limit=1007997, is_default=True),),
    )
    entries, status = model_catalog.muse_entries(auth=auth)
    assert status == "muse-serve"
    assert [e.id for e in entries] == ["muse-wonder-1.0"]
    assert model_catalog.preferred_muse_model(entries) == "muse-wonder-1.0"

    failed = MuseAuthStatus(False, error="boom", catalog_status="probe-failed")
    entries, status = model_catalog.muse_entries(auth=failed)
    assert entries == [] and status == "probe-failed"


@pytest.fixture
def muse(monkeypatch, tmp_path):
    binary = tmp_path / "muse"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("HELIOS_MUSE_BINARY", str(binary))
    monkeypatch.setattr(muse_env, "_cache", None)
    return binary


def test_muse_is_a_native_primary_provider():
    assert MUSE_NATIVE.provider_id == model_catalog.PROVIDER_MUSE == "muse"
    assert MUSE_NATIVE.runtime_kind is RuntimeKind.NATIVE
    assert MUSE_NATIVE.native_binding_kind is NativeBindingKind.SESSION
    assert MUSE_NATIVE.supports_primary_driver is True
    assert MUSE_NATIVE.catalog_source == "muse-serve"
    assert DEFAULT_PROVIDER_REGISTRY.require("muse") is MUSE_NATIVE
    assert ProviderDescriptor.from_json(MUSE_NATIVE.to_json()) == MUSE_NATIVE
    assert model_catalog.PROVIDER_LABELS[model_catalog.PROVIDER_MUSE] == "Muse"


@pytest.mark.parametrize(
    "model_id",
    ["muse-wonder-full-1.0", "muse-spark-1.3", "muse_wonder"],
)
def test_muse_model_ids_route_to_muse(model_id):
    assert model_catalog.provider_for(model_id) == model_catalog.PROVIDER_MUSE


@pytest.mark.parametrize(
    ("model_id", "provider"),
    [
        ("meta-llama/llama-3.3-70b-instruct", model_catalog.PROVIDER_OPENROUTER),
        ("gemini-3-pro", model_catalog.PROVIDER_GOOGLE),
        ("gpt-5.5", model_catalog.PROVIDER_OPENAI),
        ("opus", model_catalog.PROVIDER_ANTHROPIC),
        ("", model_catalog.PROVIDER_ANTHROPIC),
    ],
)
def test_existing_provider_routing_is_unchanged(model_id, provider):
    assert model_catalog.provider_for(model_id) == provider


def test_muse_models_stay_out_of_openrouter_choices():
    assert model_catalog.openrouter_model_selectable("meta/muse-wonder-1.0") is False


def test_muse_selects_every_permission_mode_like_other_providers():
    assert project_perms.modes_for_provider("muse") == project_perms.PERMISSION_MODES
    assert project_perms.effective_execution_mode(
        "bypassPermissions", "/some/project", provider="muse"
    ) == "bypassPermissions"


def test_invalid_override_does_not_fall_back_to_another_cli(monkeypatch):
    monkeypatch.setenv("HELIOS_MUSE_BINARY", "/missing/explicit-muse")
    monkeypatch.setattr(mb.shutil, "which", lambda _name: "/unrelated/claude")
    with pytest.raises(mb.MuseBinaryNotFound):
        mb.find_muse_binary()


def test_no_binary_means_no_models(monkeypatch):
    def missing():
        raise mb.MuseBinaryNotFound("no muse")

    monkeypatch.setattr(muse_env, "_cache", None)
    monkeypatch.setattr(muse_env, "find_muse_binary", missing)
    status = muse_env.fetch_auth_status()
    assert status.ok is False
    assert status.models == ()
    entries, _source = model_catalog.muse_entries(auth=status)
    assert entries == []


def test_failed_serve_probe_fails_closed(muse, monkeypatch):
    def boom(*_args, **_kwargs):
        raise muse_env.MuseServeError("probe failed")

    monkeypatch.setattr(muse_env, "serve_query", boom)
    status = muse_env.fetch_auth_status()
    assert status.ok is False
    assert status.models == ()
    assert model_catalog.muse_entries(auth=status)[0] == []
    assert model_catalog.preferred_muse_model([]) == ""


def test_cached_only_muse_entries_are_pending_not_invented(monkeypatch):
    monkeypatch.setattr(muse_env, "_cache", None)
    assert model_catalog.muse_entries(cached_only=True) == ([], "discovery-pending")


def test_list_mcp_servers_reads_muse_settings_and_handles_empty(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    assert muse_env.list_mcp_servers() == []

    muse_dir = tmp_path / ".config" / "muse"
    muse_dir.mkdir(parents=True)
    settings_file = muse_dir / "settings.json"
    settings_file.write_text("", encoding="utf-8")
    assert muse_env.list_mcp_servers() == []

    settings_file.write_text(json.dumps({
        "mcpServers": {
            "code-intel": {"command": "python3", "args": ["-m", "intel"]},
        }
    }), encoding="utf-8")
    servers = muse_env.list_mcp_servers()
    assert len(servers) == 1
    assert servers[0]["name"] == "code-intel"
    assert servers[0]["command"] == "python3"
    assert servers[0]["args"] == ["-m", "intel"]
