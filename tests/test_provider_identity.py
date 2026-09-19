"""Strict GTK-free provider/model identity contracts."""

from __future__ import annotations

import json

import pytest

from helios.backend import model_catalog
from helios.backend.provider_identity import (
    ANTHROPIC_NATIVE,
    DEFAULT_PROVIDER_REGISTRY,
    LOCAL_WORKER,
    OPENAI_NATIVE,
    OPENROUTER_GATEWAY,
    CredentialOwner,
    IdentityValidationError,
    ModelRef,
    NativeBindingKind,
    ProviderDescriptor,
    ProviderRegistry,
    RouteProfileRef,
    RuntimeKind,
)

_SNAPSHOT = "sha256:" + ("a" * 64)


def _model_ref(**overrides) -> ModelRef:
    values = {
        "execution_provider": "openrouter",
        "catalog_source": "openrouter",
        "requested_model": "vendor/model-slug",
        "upstream_vendor": "vendor",
        "model_family": "vendor/family",
        "route_profile": RouteProfileRef("code_readonly", 3),
        "catalog_snapshot": _SNAPSHOT,
        "independence_domain": "vendor/family",
    }
    values.update(overrides)
    return ModelRef(**values)


def test_builtin_registry_has_native_gateway_and_local_identities():
    assert ANTHROPIC_NATIVE.provider_id == model_catalog.PROVIDER_ANTHROPIC
    assert OPENAI_NATIVE.provider_id == model_catalog.PROVIDER_OPENAI
    assert ANTHROPIC_NATIVE.runtime_kind is RuntimeKind.NATIVE
    assert OPENAI_NATIVE.native_binding_kind is NativeBindingKind.THREAD
    assert OPENROUTER_GATEWAY.runtime_kind is RuntimeKind.GATEWAY
    assert OPENROUTER_GATEWAY.credential_owner is CredentialOwner.HELIOS_SUPERVISOR
    assert LOCAL_WORKER.runtime_kind is RuntimeKind.LOCAL
    assert LOCAL_WORKER.credential_owner is CredentialOwner.NONE
    assert [item.provider_id for item in DEFAULT_PROVIDER_REGISTRY.descriptors] == [
        "anthropic",
        "local",
        "openai",
        "openrouter",
    ]


def test_provider_descriptor_exact_canonical_round_trip():
    payload = OPENROUTER_GATEWAY.to_dict()

    assert payload == {
        "schema_version": 1,
        "provider_id": "openrouter",
        "display_name": "OpenRouter",
        "runtime_kind": "gateway",
        "credential_owner": "helios-supervisor",
        "catalog_source": "openrouter",
        "driver_factory_key": "openrouter-gateway",
        "capability_discovery_key": "openrouter-catalog",
        "transcript_kind": "helios-route-receipt",
        "native_binding_kind": "none",
        "supports_primary_driver": True,
        "supports_delegated_worker": True,
    }
    assert ProviderDescriptor.from_dict(payload) == OPENROUTER_GATEWAY
    assert ProviderDescriptor.from_json(OPENROUTER_GATEWAY.to_json()) == (
        OPENROUTER_GATEWAY
    )
    assert OPENROUTER_GATEWAY.to_json() == json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("provider_id", "OpenRouter"),
        ("runtime_kind", "remote"),
        ("credential_owner", "api-key"),
        ("native_binding_kind", "conversation"),
        ("supports_primary_driver", 1),
    ],
)
def test_provider_descriptor_rejects_invalid_wire_values(field, value):
    payload = OPENROUTER_GATEWAY.to_dict()
    payload[field] = value

    with pytest.raises(IdentityValidationError):
        ProviderDescriptor.from_dict(payload)


def test_provider_descriptor_rejects_missing_unknown_and_duplicate_json_fields():
    missing = OPENROUTER_GATEWAY.to_dict()
    del missing["catalog_source"]
    unknown = {**OPENROUTER_GATEWAY.to_dict(), "api_key": "not-identity"}

    with pytest.raises(IdentityValidationError, match="missing=catalog_source"):
        ProviderDescriptor.from_dict(missing)
    with pytest.raises(IdentityValidationError, match="unknown=api_key"):
        ProviderDescriptor.from_dict(unknown)
    with pytest.raises(IdentityValidationError, match="duplicate JSON field"):
        ProviderDescriptor.from_json(
            OPENROUTER_GATEWAY.to_json()[:-1] + ',"provider_id":"other"}'
        )


def test_provider_descriptor_enforces_runtime_binding_and_role_invariants():
    common = {
        "provider_id": "example",
        "display_name": "Example",
        "credential_owner": CredentialOwner.NATIVE_RUNTIME,
        "catalog_source": "example",
        "driver_factory_key": "example",
        "capability_discovery_key": "example",
        "transcript_kind": "example-jsonl",
        "supports_primary_driver": True,
        "supports_delegated_worker": False,
    }
    with pytest.raises(IdentityValidationError, match="require a native_binding"):
        ProviderDescriptor(
            **common,
            runtime_kind=RuntimeKind.NATIVE,
            native_binding_kind=NativeBindingKind.NONE,
        )
    with pytest.raises(IdentityValidationError, match="cannot claim a native"):
        ProviderDescriptor(
            **common,
            runtime_kind=RuntimeKind.GATEWAY,
            native_binding_kind=NativeBindingKind.SESSION,
        )
    with pytest.raises(IdentityValidationError, match="at least one execution"):
        ProviderDescriptor(
            **{
                **common,
                "supports_primary_driver": False,
                "supports_delegated_worker": False,
            },
            runtime_kind=RuntimeKind.NATIVE,
            native_binding_kind=NativeBindingKind.SESSION,
        )


def test_route_profile_exact_round_trip_and_validation():
    profile = RouteProfileRef("research_synthesis", 7)
    assert profile.to_dict() == {
        "schema_version": 1,
        "profile_id": "research_synthesis",
        "profile_version": 7,
    }
    assert RouteProfileRef.from_json(profile.to_json()) == profile

    with pytest.raises(IdentityValidationError):
        RouteProfileRef("Research", 1)
    with pytest.raises(IdentityValidationError):
        RouteProfileRef("research", True)
    with pytest.raises(IdentityValidationError):
        RouteProfileRef.from_dict(
            {
                "schema_version": 1,
                "profile_id": "research",
                "profile_version": 1,
                "model": "hidden",
            }
        )


def test_model_ref_matches_rfc_flat_shape_and_round_trips():
    ref = _model_ref()
    payload = ref.to_dict()

    assert payload == {
        "schema_version": 1,
        "execution_provider": "openrouter",
        "catalog_source": "openrouter",
        "requested_model": "vendor/model-slug",
        "upstream_vendor": "vendor",
        "model_family": "vendor/family",
        "profile_id": "code_readonly",
        "profile_version": 3,
        "catalog_snapshot": _SNAPSHOT,
        "independence_domain": "vendor/family",
    }
    assert ModelRef.from_dict(payload) == ref
    assert ModelRef.from_json(ref.to_json()) == ref
    assert ref.profile_id == "code_readonly"
    assert ref.profile_version == 3


def test_direct_model_ref_has_explicit_null_profile_pair():
    ref = _model_ref(route_profile=None)
    payload = ref.to_dict()

    assert payload["profile_id"] is None
    assert payload["profile_version"] is None
    assert ModelRef.from_dict(payload) == ref


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 0),
        ("execution_provider", "OpenRouter"),
        ("requested_model", "vendor/model slug"),
        ("upstream_vendor", "Vendor"),
        ("model_family", "vendor//family"),
        ("catalog_snapshot", "sha256:abc"),
        ("independence_domain", "vendor family"),
    ],
)
def test_model_ref_rejects_inexact_identity(field, value):
    payload = _model_ref().to_dict()
    payload[field] = value

    with pytest.raises(IdentityValidationError):
        ModelRef.from_dict(payload)


def test_model_ref_profile_fields_are_atomic_and_exactly_typed():
    payload = _model_ref().to_dict()
    payload["profile_version"] = None
    with pytest.raises(IdentityValidationError, match="both be set"):
        ModelRef.from_dict(payload)

    payload = _model_ref().to_dict()
    payload["profile_version"] = True
    with pytest.raises(IdentityValidationError, match="positive integer"):
        ModelRef.from_dict(payload)


def test_registry_is_canonical_replayable_and_rejects_provider_conflicts():
    registry = ProviderRegistry((OPENROUTER_GATEWAY, ANTHROPIC_NATIVE))

    assert [item.provider_id for item in registry.descriptors] == [
        "anthropic",
        "openrouter",
    ]
    assert ProviderRegistry.from_json(registry.to_json()) == registry
    assert registry.require("openrouter") is OPENROUTER_GATEWAY
    with pytest.raises(IdentityValidationError, match="unknown execution provider"):
        registry.require("missing")
    with pytest.raises(IdentityValidationError, match="duplicate provider_id"):
        ProviderRegistry((OPENROUTER_GATEWAY, OPENROUTER_GATEWAY))


def test_registry_validates_provider_and_catalog_without_model_name_inference():
    assert (
        DEFAULT_PROVIDER_REGISTRY.validate_model_ref(_model_ref())
        is OPENROUTER_GATEWAY
    )

    # An unmistakably OpenAI-looking model remains OpenRouter execution when
    # the explicit identity says so.
    routed_openai = _model_ref(
        requested_model="openai/gpt-5.6",
        upstream_vendor="openai",
        model_family="openai/gpt-5",
        independence_domain="openai/gpt-5",
    )
    assert (
        DEFAULT_PROVIDER_REGISTRY.validate_model_ref(routed_openai).provider_id
        == "openrouter"
    )

    with pytest.raises(IdentityValidationError, match="catalog_source"):
        DEFAULT_PROVIDER_REGISTRY.validate_model_ref(
            _model_ref(catalog_source="local-registry")
        )
    with pytest.raises(IdentityValidationError, match="unknown execution provider"):
        DEFAULT_PROVIDER_REGISTRY.validate_model_ref(
            _model_ref(execution_provider="unknown")
        )


def test_registry_wire_shape_is_closed_and_provider_rows_must_be_objects():
    payload = DEFAULT_PROVIDER_REGISTRY.to_dict()
    payload["future"] = []
    with pytest.raises(IdentityValidationError, match="unknown=future"):
        ProviderRegistry.from_dict(payload)

    with pytest.raises(IdentityValidationError, match="must be an array"):
        ProviderRegistry.from_dict(
            {"schema_version": 1, "providers": "anthropic"}
        )
    with pytest.raises(IdentityValidationError, match="entries must be objects"):
        ProviderRegistry.from_dict({"schema_version": 1, "providers": [42]})
