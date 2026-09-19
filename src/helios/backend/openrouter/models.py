"""Typed, GTK-free domain objects for the OpenRouter gateway boundary.

These types deliberately do not mirror arbitrary OpenRouter request objects.
Callers select one immutable, versioned profile and supply text-only messages;
model, endpoint, privacy, tools, plugins, retry, and budget policy stay outside
caller control.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class ProfileStatus(StrEnum):
    """A profile's promotion state.

    A single-member enum could not represent promotion at all, so "has this
    route earned dispatch" had nowhere to live and every gate had to be a
    separate boolean. These are the states a route moves through; the enum
    existing does not promote anything, and the broker still requires an
    external, root-owned promotion record before any of them dispatches.
    """

    #: Not admissible. Present in the catalog for visibility only.
    QUARANTINED = "quarantined"
    #: Contract-complete and hand-evaluated; the pre-promotion resting state.
    MANUAL_EVALUATION = "manual_evaluation"
    #: Evaluated against live traffic without dispatching.
    SHADOW = "shadow"
    #: Dispatchable to a bounded share, under active comparison.
    CANARY = "canary"
    #: Dispatchable, with accepted paired-quality evidence.
    ELIGIBLE = "eligible"
    #: Withdrawn after drift, instability, or an expired evaluation.
    RETIRED = "retired"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"


class CancelStatus(StrEnum):
    REQUESTED = "requested"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True, slots=True)
class ProfileRef:
    profile_id: str
    version: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not _IDENTIFIER_RE.fullmatch(
            self.profile_id
        ):
            raise ValueError("invalid profile_id")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("profile version must be positive")


@dataclass(frozen=True, slots=True)
class EndpointRef:
    """One OpenRouter operator/quantization routing constraint.

    OpenRouter's public request schema addresses the endpoint through a
    provider slug plus an independent quantization filter. Both constraints
    are pinned and provider fallback is disabled, but a base operator slug can
    still cover future variants; receipts therefore do not claim independent
    endpoint confirmation without an external catalog gate.
    """

    provider_slug: str
    quantization: str
    expected_provider_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.provider_slug, str) or not _IDENTIFIER_RE.fullmatch(
            self.provider_slug
        ):
            raise ValueError("invalid provider slug")
        if not isinstance(self.quantization, str) or self.quantization not in {
            "int4",
            "int8",
            "fp4",
            "nvfp4",
            "fp6",
            "fp8",
            "fp16",
            "bf16",
            "fp32",
            "unknown",
        }:
            raise ValueError("unsupported quantization")
        if not isinstance(self.expected_provider_names, tuple) or not self.expected_provider_names:
            raise ValueError("at least one provider receipt name is required")
        if any(
            not isinstance(name, str) or not name.strip()
            for name in self.expected_provider_names
        ):
            raise ValueError("provider receipt names must be non-empty")

    @property
    def endpoint_id(self) -> str:
        return f"{self.provider_slug}/{self.quantization}"


@dataclass(frozen=True, slots=True)
class ReadOnlyModelProfile:
    """A frozen manual-evaluation profile.

    The authority fields are constants by construction: text generation only,
    no tools, no network tools/plugins, no workspace, and no writes.
    """

    schema_version: int
    ref: ProfileRef
    status: ProfileStatus
    requested_model: str
    allowed_model_identities: tuple[tuple[str, str], ...]
    upstream_vendor: str
    model_family: str
    endpoint: EndpointRef
    catalog_snapshot: str
    prompt_adapter_version: int
    max_context_tokens: int
    max_input_chars: int
    max_output_tokens: int
    max_response_bytes: int
    timeout_seconds: float
    max_attempts: int
    max_prompt_price_per_million: Decimal
    max_completion_price_per_million: Decimal

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported profile schema")
        if not isinstance(self.requested_model, str) or not _MODEL_RE.fullmatch(
            self.requested_model
        ):
            raise ValueError("invalid OpenRouter model slug")
        if (
            not isinstance(self.allowed_model_identities, tuple)
            or not self.allowed_model_identities
        ):
            raise ValueError("at least one allowed model identity is required")
        for identity in self.allowed_model_identities:
            if (
                not isinstance(identity, tuple)
                or len(identity) != 2
                or any(
                    not isinstance(model_slug, str)
                    or not _MODEL_RE.fullmatch(model_slug)
                    for model_slug in identity
                )
            ):
                raise ValueError("invalid allowed model identity")
        if len(set(self.allowed_model_identities)) != len(
            self.allowed_model_identities
        ):
            raise ValueError("duplicate allowed model identity")
        if self.requested_model not in self.expected_response_models:
            raise ValueError("requested model must be an expected response model")
        if not isinstance(self.upstream_vendor, str) or not _IDENTIFIER_RE.fullmatch(
            self.upstream_vendor
        ):
            raise ValueError("invalid upstream vendor")
        if not isinstance(self.model_family, str) or not self.model_family.strip():
            raise ValueError("model family is required")
        if not isinstance(self.catalog_snapshot, str) or not self.catalog_snapshot.strip():
            raise ValueError("catalog snapshot is required")
        if type(self.prompt_adapter_version) is not int or self.prompt_adapter_version < 1:
            raise ValueError("prompt adapter version must be positive")
        if type(self.max_context_tokens) is not int or self.max_context_tokens < 1:
            raise ValueError("context limit must be positive")
        if type(self.max_input_chars) is not int or self.max_input_chars < 1:
            raise ValueError("input limit must be positive")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("output limit must be positive")
        if type(self.max_response_bytes) is not int or self.max_response_bytes < 1:
            raise ValueError("response limit must be positive")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("timeout must be between 0 and 300 seconds")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ValueError("attempt count must be between 1 and 3")
        if (
            not isinstance(self.max_prompt_price_per_million, Decimal)
            or not self.max_prompt_price_per_million.is_finite()
            or self.max_prompt_price_per_million < 0
        ):
            raise ValueError("prompt price ceiling cannot be negative")
        if (
            not isinstance(self.max_completion_price_per_million, Decimal)
            or not self.max_completion_price_per_million.is_finite()
            or self.max_completion_price_per_million < 0
        ):
            raise ValueError("completion price ceiling cannot be negative")

    @property
    def read_only(self) -> bool:
        return True

    @property
    def allowed_tools(self) -> tuple[()]:
        return ()

    @property
    def workspace_access(self) -> str:
        return "none"

    @property
    def expected_response_models(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(row[0] for row in self.allowed_model_identities))

    @property
    def expected_endpoint_models(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(row[1] for row in self.allowed_model_identities))

    def allows_model_identity(
        self,
        response_model: str,
        endpoint_model: str,
    ) -> bool:
        return (response_model, endpoint_model) in self.allowed_model_identities


@dataclass(frozen=True, slots=True)
class PromptMessage:
    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, MessageRole):
            raise ValueError("message role must be system or user")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("message content must be non-empty text")
        if "\x00" in self.content:
            raise ValueError("message content cannot contain NUL")


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    """A caller-controlled request with no model, endpoint, tool, or write knobs."""

    request_id: str
    profile: ProfileRef
    messages: tuple[PromptMessage, ...]
    max_output_tokens: int
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not _REQUEST_ID_RE.fullmatch(
            self.request_id
        ):
            raise ValueError("invalid request_id")
        if not isinstance(self.profile, ProfileRef):
            raise ValueError("profile must be an exact ProfileRef")
        if (
            not isinstance(self.messages, tuple)
            or not self.messages
            or len(self.messages) > 64
            or any(not isinstance(message, PromptMessage) for message in self.messages)
        ):
            raise ValueError("requests require between 1 and 64 messages")
        if not any(message.role is MessageRole.USER for message in self.messages):
            raise ValueError("requests require a user message")
        if (
            type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= 1_000_000
        ):
            raise ValueError("max_output_tokens is out of bounds")
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("request timeout is out of bounds")


@dataclass(frozen=True, slots=True)
class UsageReceipt:
    """Normalized usage.

    ``complete`` is false when OpenRouter omits usage.  Missing values remain
    ``None`` and must never be interpreted as zero.
    """

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    cost: Decimal | None
    complete: bool


@dataclass(frozen=True, slots=True)
class RouteReceipt:
    profile: ProfileRef
    catalog_snapshot: str
    requested_model: str
    actual_model: str
    actual_endpoint_model: str
    endpoint: EndpointRef
    actual_provider: str
    endpoint_confirmed: bool
    generation_id: str
    gateway_attempts: int
    router_strategy: str
    router_attempt: int
    router_region: str | None
    pipeline_stages: tuple[str, ...]
    request_digest: str
    started_at: str
    completed_at: str
    require_parameters: bool
    data_collection: str
    zdr: bool
    provider_fallbacks: bool
    cross_model_fallback: bool
    tools_enabled: bool
    response_cache_enabled: bool
    context_compression_enabled: bool


@dataclass(frozen=True, slots=True)
class InferenceResult:
    request_id: str
    content: str
    finish_reason: str
    native_finish_reason: str | None
    route: RouteReceipt
    usage: UsageReceipt


@dataclass(frozen=True, slots=True)
class CancelResult:
    request_id: str
    status: CancelStatus
