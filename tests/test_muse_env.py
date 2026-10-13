"""GTK-free contracts for Muse discovery, catalog, and account probes."""

from __future__ import annotations

import uuid

from helios.backend import model_catalog, muse_env
from helios.backend.muse_binary import MuseBinaryNotFound
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
