"""Typed, GTK-free provider and model identities.

This module is deliberately limited to immutable identity data.  It does not
discover models, load credentials, construct drivers, or perform inference.
Those runtime concerns consume these values through an explicit registry.

The wire forms are strict and versioned:

* unknown, missing, or duplicate fields are rejected;
* JSON types are not coerced (``True`` is not accepted as version ``1``);
* identifiers are validated before they can reach persistence or receipts;
* serialization is canonical and deterministic.

That strict boundary matters most for gateway model IDs: the requested model's
vendor is not the execution provider, and an endpoint provider is not known
until a later route receipt exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

__all__ = [
    "ANTHROPIC_NATIVE",
    "CredentialOwner",
    "DEFAULT_PROVIDER_REGISTRY",
    "IdentityValidationError",
    "LOCAL_WORKER",
    "ModelRef",
    "NativeBindingKind",
    "OPENAI_NATIVE",
    "OPENROUTER_GATEWAY",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_LOCAL",
    "PROVIDER_OPENAI",
    "PROVIDER_OPENROUTER",
    "ProviderDescriptor",
    "ProviderRegistry",
    "RouteProfileRef",
    "RuntimeKind",
    "SCHEMA_VERSION",
]


SCHEMA_VERSION = 1

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_LOCAL = "local"

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DOMAIN_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$"
)
_SNAPSHOT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MODEL_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")


class RuntimeKind(StrEnum):
    """How Helios reaches an execution provider."""

    NATIVE = "native"
    GATEWAY = "gateway"
    LOCAL = "local"


class CredentialOwner(StrEnum):
    """Process boundary responsible for resolving provider credentials."""

    NATIVE_RUNTIME = "native-runtime"
    HELIOS_SUPERVISOR = "helios-supervisor"
    NONE = "none"


class NativeBindingKind(StrEnum):
    """Opaque provider-native identity used to resume a conversation."""

    NONE = "none"
    SESSION = "session"
    THREAD = "thread"


class IdentityValidationError(ValueError):
    """An identity value or serialized representation is not exact."""


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Static execution-provider metadata.

    A descriptor contains no credentials, GTK objects, live health, model
    eligibility, or driver instances.  ``driver_factory_key`` and discovery
    keys name future registry adapters; importing this module does not activate
    them.
    """

    provider_id: str
    display_name: str
    runtime_kind: RuntimeKind
    credential_owner: CredentialOwner
    catalog_source: str
    driver_factory_key: str
    capability_discovery_key: str
    transcript_kind: str
    native_binding_kind: NativeBindingKind
    supports_primary_driver: bool
    supports_delegated_worker: bool

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "provider_id",
            "display_name",
            "runtime_kind",
            "credential_owner",
            "catalog_source",
            "driver_factory_key",
            "capability_discovery_key",
            "transcript_kind",
            "native_binding_kind",
            "supports_primary_driver",
            "supports_delegated_worker",
        }
    )

    def __post_init__(self) -> None:
        _identifier(self.provider_id, "provider_id")
        _display_name(self.display_name)
        _enum_instance(self.runtime_kind, RuntimeKind, "runtime_kind")
        _enum_instance(self.credential_owner, CredentialOwner, "credential_owner")
        _identifier(self.catalog_source, "catalog_source")
        _identifier(self.driver_factory_key, "driver_factory_key")
        _identifier(
            self.capability_discovery_key,
            "capability_discovery_key",
        )
        _identifier(self.transcript_kind, "transcript_kind")
        _enum_instance(
            self.native_binding_kind,
            NativeBindingKind,
            "native_binding_kind",
        )
        _exact_bool(self.supports_primary_driver, "supports_primary_driver")
        _exact_bool(self.supports_delegated_worker, "supports_delegated_worker")

        if (
            self.runtime_kind is RuntimeKind.NATIVE
            and self.native_binding_kind is NativeBindingKind.NONE
        ):
            raise IdentityValidationError(
                "native providers require a native_binding_kind"
            )
        if (
            self.runtime_kind is not RuntimeKind.NATIVE
            and self.native_binding_kind is not NativeBindingKind.NONE
        ):
            raise IdentityValidationError(
                "gateway/local providers cannot claim a native binding"
            )
        if (
            self.runtime_kind is RuntimeKind.LOCAL
            and self.credential_owner is not CredentialOwner.NONE
        ):
            raise IdentityValidationError(
                "local providers must not claim an external credential owner"
            )
        if not (
            self.supports_primary_driver or self.supports_delegated_worker
        ):
            raise IdentityValidationError(
                "provider must support at least one execution role"
            )

    def to_dict(self) -> dict[str, object]:
        """Return the complete versioned wire representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "runtime_kind": self.runtime_kind.value,
            "credential_owner": self.credential_owner.value,
            "catalog_source": self.catalog_source,
            "driver_factory_key": self.driver_factory_key,
            "capability_discovery_key": self.capability_discovery_key,
            "transcript_kind": self.transcript_kind,
            "native_binding_kind": self.native_binding_kind.value,
            "supports_primary_driver": self.supports_primary_driver,
            "supports_delegated_worker": self.supports_delegated_worker,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProviderDescriptor:
        data = _exact_mapping(value, cls._WIRE_FIELDS, "ProviderDescriptor")
        _schema_version(data["schema_version"])
        return cls(
            provider_id=_exact_string(data["provider_id"], "provider_id"),
            display_name=_exact_string(data["display_name"], "display_name"),
            runtime_kind=_enum_value(
                data["runtime_kind"],
                RuntimeKind,
                "runtime_kind",
            ),
            credential_owner=_enum_value(
                data["credential_owner"],
                CredentialOwner,
                "credential_owner",
            ),
            catalog_source=_exact_string(
                data["catalog_source"],
                "catalog_source",
            ),
            driver_factory_key=_exact_string(
                data["driver_factory_key"],
                "driver_factory_key",
            ),
            capability_discovery_key=_exact_string(
                data["capability_discovery_key"],
                "capability_discovery_key",
            ),
            transcript_kind=_exact_string(
                data["transcript_kind"],
                "transcript_kind",
            ),
            native_binding_kind=_enum_value(
                data["native_binding_kind"],
                NativeBindingKind,
                "native_binding_kind",
            ),
            supports_primary_driver=_exact_bool(
                data["supports_primary_driver"],
                "supports_primary_driver",
            ),
            supports_delegated_worker=_exact_bool(
                data["supports_delegated_worker"],
                "supports_delegated_worker",
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> ProviderDescriptor:
        return cls.from_dict(_strict_json_mapping(value, "ProviderDescriptor"))


@dataclass(frozen=True, slots=True)
class RouteProfileRef:
    """Immutable identity of the routing profile selected for an Attempt."""

    profile_id: str
    profile_version: int

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"schema_version", "profile_id", "profile_version"}
    )

    def __post_init__(self) -> None:
        _identifier(self.profile_id, "profile_id")
        _positive_int(self.profile_version, "profile_version")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RouteProfileRef:
        data = _exact_mapping(value, cls._WIRE_FIELDS, "RouteProfileRef")
        _schema_version(data["schema_version"])
        return cls(
            profile_id=_exact_string(data["profile_id"], "profile_id"),
            profile_version=_positive_int(
                data["profile_version"],
                "profile_version",
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> RouteProfileRef:
        return cls.from_dict(_strict_json_mapping(value, "RouteProfileRef"))


@dataclass(frozen=True, slots=True)
class ModelRef:
    """Exact pre-dispatch model identity.

    ``execution_provider`` identifies the adapter/credential boundary.
    ``upstream_vendor`` and ``model_family`` describe the requested model.
    The endpoint provider and actual response model intentionally do not appear
    here; they belong in the post-dispatch route receipt.
    """

    execution_provider: str
    catalog_source: str
    requested_model: str
    upstream_vendor: str
    model_family: str
    catalog_snapshot: str
    independence_domain: str
    route_profile: RouteProfileRef | None = None

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "execution_provider",
            "catalog_source",
            "requested_model",
            "upstream_vendor",
            "model_family",
            "profile_id",
            "profile_version",
            "catalog_snapshot",
            "independence_domain",
        }
    )

    def __post_init__(self) -> None:
        _identifier(self.execution_provider, "execution_provider")
        _identifier(self.catalog_source, "catalog_source")
        _model_id(self.requested_model)
        _identifier(self.upstream_vendor, "upstream_vendor")
        _domain(self.model_family, "model_family")
        if not _SNAPSHOT_RE.fullmatch(self.catalog_snapshot):
            raise IdentityValidationError(
                "catalog_snapshot must be sha256:<64 lowercase hex characters>"
            )
        _domain(self.independence_domain, "independence_domain")
        if self.route_profile is not None and not isinstance(
            self.route_profile,
            RouteProfileRef,
        ):
            raise IdentityValidationError(
                "route_profile must be a RouteProfileRef or None"
            )

    @property
    def profile_id(self) -> str | None:
        return self.route_profile.profile_id if self.route_profile else None

    @property
    def profile_version(self) -> int | None:
        return self.route_profile.profile_version if self.route_profile else None

    def to_dict(self) -> dict[str, object]:
        """Return the RFC's fixed-shape, flat ModelRef representation."""

        return {
            "schema_version": SCHEMA_VERSION,
            "execution_provider": self.execution_provider,
            "catalog_source": self.catalog_source,
            "requested_model": self.requested_model,
            "upstream_vendor": self.upstream_vendor,
            "model_family": self.model_family,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "catalog_snapshot": self.catalog_snapshot,
            "independence_domain": self.independence_domain,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ModelRef:
        data = _exact_mapping(value, cls._WIRE_FIELDS, "ModelRef")
        _schema_version(data["schema_version"])
        profile_id = data["profile_id"]
        profile_version = data["profile_version"]
        if (profile_id is None) is not (profile_version is None):
            raise IdentityValidationError(
                "profile_id and profile_version must both be set or both be null"
            )
        route_profile = None
        if profile_id is not None:
            route_profile = RouteProfileRef(
                profile_id=_exact_string(profile_id, "profile_id"),
                profile_version=_positive_int(
                    profile_version,
                    "profile_version",
                ),
            )
        return cls(
            execution_provider=_exact_string(
                data["execution_provider"],
                "execution_provider",
            ),
            catalog_source=_exact_string(
                data["catalog_source"],
                "catalog_source",
            ),
            requested_model=_exact_string(
                data["requested_model"],
                "requested_model",
            ),
            upstream_vendor=_exact_string(
                data["upstream_vendor"],
                "upstream_vendor",
            ),
            model_family=_exact_string(
                data["model_family"],
                "model_family",
            ),
            route_profile=route_profile,
            catalog_snapshot=_exact_string(
                data["catalog_snapshot"],
                "catalog_snapshot",
            ),
            independence_domain=_exact_string(
                data["independence_domain"],
                "independence_domain",
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> ModelRef:
        return cls.from_dict(_strict_json_mapping(value, "ModelRef"))


@dataclass(frozen=True, slots=True)
class ProviderRegistry:
    """Replayable immutable provider registry."""

    descriptors: tuple[ProviderDescriptor, ...] = ()

    _WIRE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"schema_version", "providers"}
    )

    def __post_init__(self) -> None:
        if not isinstance(self.descriptors, tuple):
            raise IdentityValidationError("descriptors must be a tuple")
        by_id: dict[str, ProviderDescriptor] = {}
        for descriptor in self.descriptors:
            if not isinstance(descriptor, ProviderDescriptor):
                raise IdentityValidationError(
                    "descriptors must contain only ProviderDescriptor values"
                )
            if descriptor.provider_id in by_id:
                raise IdentityValidationError(
                    f"duplicate provider_id: {descriptor.provider_id}"
                )
            by_id[descriptor.provider_id] = descriptor
        object.__setattr__(
            self,
            "descriptors",
            tuple(by_id[key] for key in sorted(by_id)),
        )

    def get(self, provider_id: str) -> ProviderDescriptor | None:
        _identifier(provider_id, "provider_id")
        return next(
            (
                descriptor
                for descriptor in self.descriptors
                if descriptor.provider_id == provider_id
            ),
            None,
        )

    def require(self, provider_id: str) -> ProviderDescriptor:
        descriptor = self.get(provider_id)
        if descriptor is None:
            raise IdentityValidationError(f"unknown execution provider: {provider_id}")
        return descriptor

    def validate_model_ref(self, model_ref: ModelRef) -> ProviderDescriptor:
        """Validate provider/catalog ownership without guessing from model ID."""

        if not isinstance(model_ref, ModelRef):
            raise IdentityValidationError("model_ref must be a ModelRef")
        descriptor = self.require(model_ref.execution_provider)
        if descriptor.catalog_source != model_ref.catalog_source:
            raise IdentityValidationError(
                "model catalog_source does not match its execution provider"
            )
        return descriptor

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "providers": [
                descriptor.to_dict() for descriptor in self.descriptors
            ],
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProviderRegistry:
        data = _exact_mapping(value, cls._WIRE_FIELDS, "ProviderRegistry")
        _schema_version(data["schema_version"])
        providers = data["providers"]
        if (
            not isinstance(providers, Sequence)
            or isinstance(providers, (str, bytes, bytearray))
        ):
            raise IdentityValidationError("providers must be an array")
        return cls(
            tuple(
                ProviderDescriptor.from_dict(_mapping_item(row, "providers"))
                for row in providers
            )
        )

    @classmethod
    def from_json(cls, value: str) -> ProviderRegistry:
        return cls.from_dict(_strict_json_mapping(value, "ProviderRegistry"))


def _exact_mapping(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IdentityValidationError(f"{label} must be an object")
    data = dict(value)
    keys = set(data)
    missing = expected - keys
    unknown = keys - expected
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown={','.join(sorted(map(str, unknown)))}")
        raise IdentityValidationError(f"invalid {label} fields ({'; '.join(details)})")
    return data


def _mapping_item(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IdentityValidationError(f"{label} entries must be objects")
    return value


def _schema_version(value: object) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise IdentityValidationError(
            f"schema_version must be exactly {SCHEMA_VERSION}"
        )
    return value


def _exact_string(value: object, label: str) -> str:
    if type(value) is not str:
        raise IdentityValidationError(f"{label} must be a string")
    return value


def _exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise IdentityValidationError(f"{label} must be a boolean")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise IdentityValidationError(f"{label} must be a positive integer")
    return value


def _identifier(value: object, label: str) -> str:
    text = _exact_string(value, label)
    if len(text) > 64 or not _IDENTIFIER_RE.fullmatch(text):
        raise IdentityValidationError(
            f"{label} must be a lowercase canonical identifier"
        )
    return text


def _domain(value: object, label: str) -> str:
    text = _exact_string(value, label)
    if len(text) > 256 or not _DOMAIN_RE.fullmatch(text):
        raise IdentityValidationError(f"{label} must be a canonical domain")
    return text


def _model_id(value: object) -> str:
    text = _exact_string(value, "requested_model")
    if not _MODEL_RE.fullmatch(text):
        raise IdentityValidationError(
            "requested_model must be non-empty and contain no whitespace/control characters"
        )
    return text


def _display_name(value: object) -> str:
    text = _exact_string(value, "display_name")
    if (
        not text
        or text != text.strip()
        or len(text) > 80
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise IdentityValidationError(
            "display_name must be trimmed, non-empty display text"
        )
    return text


def _enum_instance(value: object, enum: type[StrEnum], label: str) -> StrEnum:
    if not isinstance(value, enum):
        raise IdentityValidationError(f"{label} must be a {enum.__name__}")
    return value


def _enum_value(value: object, enum: type[StrEnum], label: str) -> StrEnum:
    text = _exact_string(value, label)
    try:
        return enum(text)
    except ValueError as exc:
        raise IdentityValidationError(f"unsupported {label}: {text}") from exc


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _strict_json_mapping(value: str, label: str) -> Mapping[str, object]:
    if type(value) is not str:
        raise IdentityValidationError(f"{label} JSON must be a string")

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        data: dict[str, object] = {}
        for key, item in pairs:
            if key in data:
                raise IdentityValidationError(f"duplicate JSON field: {key}")
            data[key] = item
        return data

    def reject_constant(constant: str) -> None:
        raise IdentityValidationError(f"invalid JSON constant: {constant}")

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except IdentityValidationError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise IdentityValidationError(f"invalid {label} JSON") from exc
    if not isinstance(parsed, Mapping):
        raise IdentityValidationError(f"{label} JSON must contain an object")
    return parsed


ANTHROPIC_NATIVE = ProviderDescriptor(
    provider_id=PROVIDER_ANTHROPIC,
    display_name="Claude",
    runtime_kind=RuntimeKind.NATIVE,
    credential_owner=CredentialOwner.NATIVE_RUNTIME,
    catalog_source="claude-binary",
    driver_factory_key="claude-cli",
    capability_discovery_key="claude-binary",
    transcript_kind="claude-jsonl",
    native_binding_kind=NativeBindingKind.SESSION,
    supports_primary_driver=True,
    supports_delegated_worker=False,
)

OPENAI_NATIVE = ProviderDescriptor(
    provider_id=PROVIDER_OPENAI,
    display_name="GPT",
    runtime_kind=RuntimeKind.NATIVE,
    credential_owner=CredentialOwner.NATIVE_RUNTIME,
    catalog_source="codex-app-server",
    driver_factory_key="codex-app-server",
    capability_discovery_key="codex-app-server",
    transcript_kind="helios-codex-jsonl",
    native_binding_kind=NativeBindingKind.THREAD,
    supports_primary_driver=True,
    supports_delegated_worker=False,
)

OPENROUTER_GATEWAY = ProviderDescriptor(
    provider_id=PROVIDER_OPENROUTER,
    display_name="OpenRouter",
    runtime_kind=RuntimeKind.GATEWAY,
    credential_owner=CredentialOwner.HELIOS_SUPERVISOR,
    catalog_source="openrouter",
    driver_factory_key="openrouter-gateway",
    capability_discovery_key="openrouter-catalog",
    transcript_kind="helios-route-receipt",
    native_binding_kind=NativeBindingKind.NONE,
    supports_primary_driver=True,
    supports_delegated_worker=True,
)

LOCAL_WORKER = ProviderDescriptor(
    provider_id=PROVIDER_LOCAL,
    display_name="Local",
    runtime_kind=RuntimeKind.LOCAL,
    credential_owner=CredentialOwner.NONE,
    catalog_source="local-registry",
    driver_factory_key="local-runtime",
    capability_discovery_key="local-registry",
    transcript_kind="helios-route-receipt",
    native_binding_kind=NativeBindingKind.NONE,
    supports_primary_driver=False,
    supports_delegated_worker=True,
)

DEFAULT_PROVIDER_REGISTRY = ProviderRegistry(
    (
        ANTHROPIC_NATIVE,
        OPENAI_NATIVE,
        OPENROUTER_GATEWAY,
        LOCAL_WORKER,
    )
)
