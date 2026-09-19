"""Frozen OpenRouter profiles, pinned against a dated live-catalog snapshot.

A profile names an exact model and endpoint variant. The catalog is not frozen:
OpenRouter retires model slugs (notably ``:free`` variants) and moves operators,
so a pin that verified when it was written can stop resolving. ``verify_endpoint``
is the gate that notices, and it fails closed — an unconfirmable profile simply
never dispatches. When a pin dies, replace it from a live ``/endpoints``
response; do not re-copy an older snapshot."""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

from .models import (
    EndpointRef,
    ProfileRef,
    ProfileStatus,
    ReadOnlyModelProfile,
)


CATALOG_SNAPSHOT = "openrouter-rfc-2026-07-23"


_PROFILES = {
    # Live catalog 2026-09-05: the old Nemotron 3 Nano :free route now
    # has no endpoints. This new profile cannot inherit its quality promotion.
    # Nvidia publishes the new endpoint as nvidia/nvfp4; both operator and
    # quantization remain constrained, and preview consent advances to v3.
    ProfileRef("free_canary.nemotron35lightning_nvidia", 1): ReadOnlyModelProfile(
        schema_version=1,
        ref=ProfileRef("free_canary.nemotron35lightning_nvidia", 1),
        status=ProfileStatus.MANUAL_EVALUATION,
        requested_model="nvidia/nemotron-3.5-lightning:free",
        allowed_model_identities=(
            (
                "nvidia/nemotron-3.5-lightning:free",
                "nvidia/nemotron-3.5-lightning:free",
            ),
            # Public endpoint name on 2026-09-05 advertises this dated model.
            # The response alias and selected endpoint identity are distinct
            # fields; permit this exact pair without allowing arbitrary dates.
            (
                "nvidia/nemotron-3.5-lightning:free",
                "nvidia/nemotron-3.5-lightning-20260807:free",
            ),
        ),
        upstream_vendor="nvidia",
        model_family="nemotron-3.5/lightning",
        endpoint=EndpointRef(
            provider_slug="nvidia",
            quantization="nvfp4",
            expected_provider_names=("Nvidia", "NVIDIA"),
        ),
        catalog_snapshot="openrouter-live-2026-09-05",
        prompt_adapter_version=1,
        max_context_tokens=256_000,
        # Conservative until token-aware capsule accounting lands.
        max_input_chars=200_000,
        max_output_tokens=4_096,
        max_response_bytes=1024 * 1024,
        timeout_seconds=45.0,
        max_attempts=1,
        max_prompt_price_per_million=Decimal("0"),
        max_completion_price_per_million=Decimal("0"),
    ),
    ProfileRef("bulk_extract.parasail", 1): ReadOnlyModelProfile(
        schema_version=1,
        ref=ProfileRef("bulk_extract.parasail", 1),
        status=ProfileStatus.MANUAL_EVALUATION,
        requested_model="deepseek/deepseek-v4-flash",
        allowed_model_identities=(
            ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
        ),
        upstream_vendor="deepseek",
        model_family="deepseek/v4",
        endpoint=EndpointRef(
            provider_slug="parasail",
            quantization="fp8",
            expected_provider_names=("Parasail",),
        ),
        catalog_snapshot=CATALOG_SNAPSHOT,
        prompt_adapter_version=1,
        max_context_tokens=1_048_576,
        max_input_chars=1_000_000,
        max_output_tokens=8_192,
        max_response_bytes=2 * 1024 * 1024,
        timeout_seconds=45.0,
        max_attempts=2,
        max_prompt_price_per_million=Decimal("0.14"),
        max_completion_price_per_million=Decimal("0.28"),
    ),
    ProfileRef("bulk_extract.coreweave", 1): ReadOnlyModelProfile(
        schema_version=1,
        ref=ProfileRef("bulk_extract.coreweave", 1),
        status=ProfileStatus.MANUAL_EVALUATION,
        requested_model="deepseek/deepseek-v4-flash",
        allowed_model_identities=(
            ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
        ),
        upstream_vendor="deepseek",
        model_family="deepseek/v4",
        endpoint=EndpointRef(
            provider_slug="coreweave",
            quantization="fp8",
            expected_provider_names=("CoreWeave", "Coreweave"),
        ),
        catalog_snapshot=CATALOG_SNAPSHOT,
        prompt_adapter_version=1,
        max_context_tokens=1_048_576,
        max_input_chars=1_000_000,
        max_output_tokens=8_192,
        max_response_bytes=2 * 1024 * 1024,
        timeout_seconds=45.0,
        max_attempts=2,
        max_prompt_price_per_million=Decimal("0.14"),
        max_completion_price_per_million=Decimal("0.28"),
    ),
}


PROFILES = MappingProxyType(_PROFILES)


def get_profile(ref: ProfileRef) -> ReadOnlyModelProfile:
    """Return one exact profile or fail; no aliases, latest, or model lookup."""

    try:
        return PROFILES[ref]
    except KeyError as exc:
        raise LookupError(
            f"OpenRouter profile {ref.profile_id!r} version {ref.version} is not registered"
        ) from exc
