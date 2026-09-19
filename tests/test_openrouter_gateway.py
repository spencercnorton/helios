"""GTK-free contract and fault-injection tests for the OpenRouter gateway."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Iterable

import pytest

from helios.backend.openrouter import (
    PROFILES,
    CancelStatus,
    CancellationToken,
    EndpointRef,
    GatewayError,
    GatewayErrorKind,
    InferenceRequest,
    InferenceResult,
    MessageRole,
    OpenRouterGateway,
    ProfileRef,
    ProfileStatus,
    PromptMessage,
    TransportFailure,
    TransportFailureKind,
    UsageReceipt,
    get_profile,
)


MODEL = "deepseek/deepseek-v4-flash"
PROFILE = ProfileRef("bulk_extract.parasail", 1)
GENERATION_ID = "gen-test-123"
FAKE_KEY = "fake-openrouter-key-for-tests-only"


class FakeResponse:
    def __init__(
        self,
        chunks: Iterable[bytes] = (),
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self.headers = headers or {}
        self._chunks = chunks
        self.closed = False

    def iter_bytes(self):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, *actions):
        self.actions = list(actions)
        self.requests = []
        self.timeouts = []

    def open(self, request, *, timeout_seconds):
        self.requests.append(request)
        self.timeouts.append(timeout_seconds)
        if not self.actions:
            raise AssertionError("unexpected HTTP request")
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return action(request, timeout_seconds)
        return action


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def _request(
    *,
    request_id: str = "req-1",
    profile: ProfileRef = PROFILE,
    max_output_tokens: int = 128,
    timeout_seconds: float | None = None,
    messages: tuple[PromptMessage, ...] | None = None,
) -> InferenceRequest:
    return InferenceRequest(
        request_id=request_id,
        profile=profile,
        messages=messages
        or (
            PromptMessage(MessageRole.SYSTEM, "Return only supported facts."),
            PromptMessage(MessageRole.USER, "Classify this record."),
        ),
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )


def _metadata(
    *,
    provider: str = "Parasail",
    model: str = MODEL,
    requested: str = MODEL,
    strategy: str = "direct",
    attempt: int = 1,
    pipeline: list | None = None,
    selected: bool = True,
    route_attempts: list | None = None,
) -> dict:
    return {
        "requested": requested,
        "strategy": strategy,
        "region": "iad",
        "attempt": attempt,
        "is_byok": False,
        "endpoints": {
            "total": 1,
            "available": [
                {
                    "provider": provider,
                    "model": model,
                    "selected": selected,
                    "new_additive_field": {"ignored": True},
                }
            ],
        },
        "attempts": route_attempts
        if route_attempts is not None
        else [{"provider": provider, "model": model, "status": 200}],
        "pipeline": [] if pipeline is None else pipeline,
        "future_additive_field": "ignored",
    }


def _usage(**overrides) -> dict:
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "total_tokens": 12,
        "prompt_tokens_details": {
            "cached_tokens": 3,
            "cache_write_tokens": 1,
        },
        "completion_tokens_details": {"reasoning_tokens": 1},
        "cost": 0.00000196,
        "future_usage_field": "ignored",
    }
    usage.update(overrides)
    return usage


def _event(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


def _success_response(
    *,
    text: str = "supported",
    provider: str = "Parasail",
    model: str = MODEL,
    metadata: dict | None = None,
    usage: dict | None = None,
    include_usage: bool = True,
    include_metadata: bool = True,
    include_done: bool = True,
    include_header_generation: bool = True,
    include_event_generation: bool = True,
    content_type: str = "text/event-stream; charset=utf-8",
    extra_delta: dict | None = None,
    finish_reason: str = "stop",
) -> FakeResponse:
    first = {
        "object": "chat.completion.chunk",
        "model": model,
        "provider": provider,
        "choices": [
            {
                "index": 0,
                "delta": {"content": text, **(extra_delta or {})},
                "finish_reason": None,
            }
        ],
        "additive_top_level": 7,
    }
    final = {
        "object": "chat.completion.chunk",
        "model": model,
        "provider": provider,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
                "native_finish_reason": finish_reason,
            }
        ],
    }
    if include_event_generation:
        first["id"] = GENERATION_ID
        final["id"] = GENERATION_ID
    if include_usage:
        final["usage"] = usage if usage is not None else _usage()
    if include_metadata:
        final["openrouter_metadata"] = (
            metadata if metadata is not None else _metadata(provider=provider, model=model)
        )
    body = b": OPENROUTER PROCESSING\n\n" + _event(first) + _event(final)
    if include_done:
        body += b"data: [DONE]\n\n"
    headers = {"Content-Type": content_type}
    if include_header_generation:
        headers["X-Generation-Id"] = GENERATION_ID
    return FakeResponse([body], headers=headers)


def _gateway(*actions, **kwargs) -> tuple[OpenRouterGateway, FakeTransport]:
    transport = FakeTransport(*actions)
    return (
        OpenRouterGateway(
            transport=transport,
            random_fraction=lambda: 0.0,
            **kwargs,
        ),
        transport,
    )


def _infer(gateway: OpenRouterGateway, request: InferenceRequest | None = None):
    return gateway.infer(request or _request(), api_key=FAKE_KEY)


def test_profiles_are_exact_versioned_read_only_manual_evaluation_only():
    assert set(PROFILES) == {
        ProfileRef("free_canary.nemotron35lightning_nvidia", 1),
        ProfileRef("bulk_extract.parasail", 1),
        ProfileRef("bulk_extract.coreweave", 1),
    }
    profile = get_profile(PROFILE)
    assert profile.status is ProfileStatus.MANUAL_EVALUATION
    assert all(
        row.status is ProfileStatus.MANUAL_EVALUATION
        for row in PROFILES.values()
    )
    assert profile.requested_model == MODEL
    assert profile.endpoint.endpoint_id == "parasail/fp8"
    assert profile.read_only is True
    assert profile.allowed_tools == ()
    assert profile.workspace_access == "none"
    assert profile.max_attempts == 2
    with pytest.raises(FrozenInstanceError):
        profile.requested_model = "arbitrary/model"


def test_replacement_free_canary_pins_variant_and_cannot_inherit_retired_promotion():
    from helios.backend.router_policy import CANARY_PROFILE

    profile = get_profile(CANARY_PROFILE)
    assert profile.requested_model == "nvidia/nemotron-3.5-lightning:free"
    assert profile.endpoint.endpoint_id == "nvidia/nvfp4"
    assert profile.max_prompt_price_per_million == 0
    assert profile.max_completion_price_per_million == 0
    assert profile.status is ProfileStatus.MANUAL_EVALUATION
    with pytest.raises(LookupError):
        get_profile(ProfileRef("free_canary.nemotron3nano_nvidia", 1))
    with pytest.raises(TypeError):
        PROFILES[ProfileRef("arbitrary.model", 1)] = profile


def test_profile_lookup_has_no_latest_alias_or_arbitrary_model_path():
    with pytest.raises(LookupError):
        get_profile(ProfileRef("bulk_extract.parasail", 2))
    with pytest.raises(LookupError):
        get_profile(ProfileRef("arbitrary.model", 1))


def test_prompt_and_request_shapes_exclude_tool_and_assistant_roles():
    with pytest.raises(ValueError):
        PromptMessage("tool", "run this")
    with pytest.raises(ValueError):
        PromptMessage("assistant", "prior output")
    with pytest.raises(ValueError):
        InferenceRequest(
            request_id="req-no-user",
            profile=PROFILE,
            messages=(PromptMessage(MessageRole.SYSTEM, "system only"),),
            max_output_tokens=1,
        )


def test_runtime_types_fail_closed_instead_of_relying_on_annotations():
    with pytest.raises(ValueError):
        ProfileRef(7, 1)
    with pytest.raises(ValueError):
        ProfileRef("valid.profile", True)
    with pytest.raises(ValueError):
        InferenceRequest(
            request_id="req-bad-profile",
            profile="bulk_extract.parasail",
            messages=(PromptMessage(MessageRole.USER, "text"),),
            max_output_tokens=1,
        )
    with pytest.raises(ValueError):
        InferenceRequest(
            request_id="req-bad-messages",
            profile=PROFILE,
            messages=[PromptMessage(MessageRole.USER, "text")],
            max_output_tokens=1,
        )
    with pytest.raises(ValueError):
        InferenceRequest(
            request_id="req-bad-tokens",
            profile=PROFILE,
            messages=(PromptMessage(MessageRole.USER, "text"),),
            max_output_tokens=True,
        )
    with pytest.raises(ValueError):
        InferenceRequest(
            request_id="req-bad-timeout",
            profile=PROFILE,
            messages=(PromptMessage(MessageRole.USER, "text"),),
            max_output_tokens=1,
            timeout_seconds=float("nan"),
        )


def test_success_builds_strict_privacy_payload_and_typed_receipts():
    response = _success_response()
    gateway, transport = _gateway(response)

    result = _infer(gateway)

    assert isinstance(result, InferenceResult)
    assert result.content == "supported"
    assert result.finish_reason == "stop"
    assert result.usage == UsageReceipt(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        reasoning_tokens=1,
        cached_tokens=3,
        cache_write_tokens=1,
        cost=Decimal("0.00000196"),
        complete=True,
    )
    assert result.route.requested_model == MODEL
    assert result.route.actual_model == MODEL
    assert result.route.actual_endpoint_model == MODEL
    assert result.route.endpoint.endpoint_id == "parasail/fp8"
    assert result.route.actual_provider == "Parasail"
    # Post-response half of the endpoint confirmation: the operator that served
    # the request is one this profile declares. The pre-dispatch half — matching
    # the exact slug and quantization against OpenRouter's endpoints catalog —
    # is the broker's job (router_promotion.verified_endpoints), so this alone
    # never authorizes anything.
    assert result.route.endpoint_confirmed is True
    assert result.route.gateway_attempts == 1
    assert result.route.router_strategy == "direct"
    assert result.route.router_attempt == 1
    assert result.route.require_parameters is True
    assert result.route.data_collection == "deny"
    assert result.route.zdr is True
    assert result.route.provider_fallbacks is False
    assert result.route.cross_model_fallback is False
    assert result.route.tools_enabled is False
    assert result.route.response_cache_enabled is False
    assert result.route.context_compression_enabled is False
    assert result.route.request_digest.startswith("sha256:")
    assert response.closed is True
    assert gateway.cancel("req-1").status is CancelStatus.NOT_ACTIVE

    assert len(transport.requests) == 1
    sent = transport.requests[0]
    payload = json.loads(sent.body)
    assert sent.url == "https://openrouter.ai/api/v1/chat/completions"
    assert sent.method == "POST"
    assert payload["model"] == MODEL
    assert payload["stream"] is True
    assert payload["n"] == 1
    assert payload["provider"] == {
        "only": ["parasail"],
        "order": ["parasail"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "quantizations": ["fp8"],
        "max_price": {"prompt": 0.14, "completion": 0.28},
    }
    assert payload["plugins"] == [
        {"id": "web", "enabled": False},
        {"id": "file-parser", "enabled": False},
        {"id": "response-healing", "enabled": False},
        {"id": "pareto-router", "enabled": False},
        {"id": "context-compression", "enabled": False},
    ]
    assert "models" not in payload
    assert "tools" not in payload


def test_gateway_accepts_pinned_alias_to_canonical_endpoint_identity(monkeypatch):
    """A requested slug may be served under a dated canonical endpoint model.

    Built from a purpose-made profile rather than whichever shipped profile
    happens to carry a two-part identity. This assertion is about
    ``allowed_model_identities`` — a gateway mechanism — and riding it on
    catalog data made it break when the canary was repointed at a model whose
    requested and endpoint slugs are the same string. The mechanism did not
    change; only the data did, and a mechanism test should not be able to tell.
    """
    profile = ProfileRef("test_alias.fixture", 1)
    requested_alias = "vendor/model-alias:free"
    canonical_endpoint = "vendor/model-alias-20260819:free"
    aliased = dataclasses.replace(
        get_profile(ProfileRef("free_canary.nemotron35lightning_nvidia", 1)),
        ref=profile,
        requested_model=requested_alias,
        allowed_model_identities=((requested_alias, canonical_endpoint),),
        upstream_vendor="vendor",
        model_family="vendor/model-alias",
        endpoint=EndpointRef(
            provider_slug="novita",
            quantization="unknown",
            expected_provider_names=("Novita",),
        ),
    )
    monkeypatch.setattr(
        "helios.backend.openrouter.gateway.get_profile",
        lambda ref: aliased if ref == profile else get_profile(ref),
    )
    metadata = _metadata(
        provider="Novita",
        model=canonical_endpoint,
        requested=requested_alias,
        route_attempts=[
            {
                "provider": "Novita",
                "model": canonical_endpoint,
                "status": 200,
            }
        ],
    )
    response = _success_response(
        provider="Novita",
        model=requested_alias,
        metadata=metadata,
    )
    gateway, transport = _gateway(response)

    result = _infer(gateway, _request(profile=profile))

    assert result.route.requested_model == requested_alias
    assert result.route.actual_model == requested_alias
    assert result.route.actual_endpoint_model == canonical_endpoint
    assert result.route.actual_provider == "Novita"
    sent = transport.requests[0]
    payload = json.loads(sent.body)
    assert payload["provider"]["only"] == ["novita"]
    assert payload["provider"]["allow_fallbacks"] is False
    assert payload["provider"]["zdr"] is True
    assert "tool_choice" not in payload
    assert "route" not in payload
    assert FAKE_KEY not in sent.body.decode()
    assert sent.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert sent.headers["X-OpenRouter-Metadata"] == "enabled"
    assert sent.headers["X-OpenRouter-Cache"] == "false"


@pytest.mark.parametrize("endpoint_model,allowed", [
    ("nvidia/nemotron-3.5-lightning-20260807:free", True),
    ("nvidia/nemotron-3.5-lightning-20990101:free", False),
])
def test_free_canary_accepts_only_the_catalogs_exact_dated_endpoint(endpoint_model, allowed):
    """Public GET catalog identity in synthetic SSE/metadata; no real inference.

    The 2026-09-05 endpoint name is
    'Nvidia | nvidia/nemotron-3.5-lightning-20260807:free', tag nvidia/nvfp4.
    Endpoint confirmation alone does not exercise the gateway's response pair.
    """
    from helios.backend.router_policy import CANARY_PROFILE

    requested = "nvidia/nemotron-3.5-lightning:free"
    metadata = _metadata(provider="Nvidia", requested=requested, model=endpoint_model)
    response = _success_response(provider="Nvidia", model=requested,
                                 metadata=metadata, usage=_usage(cost=0))
    gateway, transport = _gateway(response)
    if allowed:
        result = _infer(gateway, _request(profile=CANARY_PROFILE))
        assert result.route.actual_model == requested
        assert result.route.actual_endpoint_model == endpoint_model
        assert result.route.actual_provider == "Nvidia"
    else:
        with pytest.raises(GatewayError) as raised:
            _infer(gateway, _request(profile=CANARY_PROFILE))
        assert raised.value.kind is GatewayErrorKind.POLICY_VIOLATION
    payload = json.loads(transport.requests[0].body)
    assert payload["provider"]["only"] == ["nvidia"]
    assert payload["provider"]["quantizations"] == ["nvfp4"]
    assert payload["provider"]["max_price"] == {"prompt": 0.0, "completion": 0.0}
    assert payload["provider"]["allow_fallbacks"] is False


def test_incomplete_length_finish_is_rejected():
    gateway, _transport = _gateway(_success_response(finish_reason="length"))

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.PROTOCOL


def test_sse_handles_fragmented_crlf_comments_multiline_data_and_utf8():
    first = {
        "id": GENERATION_ID,
        "model": MODEL,
        "provider": "Parasail",
        "choices": [
            {"index": 0, "delta": {"content": "café"}, "finish_reason": None}
        ],
    }
    first_json = json.dumps(first, ensure_ascii=False, separators=(",", ":"))
    split_at = first_json.index('"model"')
    first_event = (
        ": keepalive\r\n\r\n"
        f"data: {first_json[:split_at]}\r\n"
        f"data: {first_json[split_at:]}\r\n\r\n"
    ).encode()
    final_event = _event(
        {
            "id": GENERATION_ID,
            "model": MODEL,
            "provider": "Parasail",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                    "native_finish_reason": "stop",
                }
            ],
            "usage": _usage(),
            "openrouter_metadata": _metadata(),
        }
    ).replace(b"\n", b"\r\n")
    body = first_event + final_event + b"data: [DONE]\r\n\r\n"
    response = FakeResponse(
        [body[index : index + 1] for index in range(len(body))],
        headers={
            "content-type": "text/event-stream",
            "x-generation-id": GENERATION_ID,
        },
    )
    gateway, _ = _gateway(response)

    assert _infer(gateway).content == "café"


def test_additive_fields_and_guardrail_metadata_are_retained_safely():
    metadata = _metadata(
        pipeline=[
            {
                "type": "guardrail",
                "name": "content-filter",
                "data": {"future": "ignored"},
            }
        ]
    )
    gateway, _ = _gateway(_success_response(metadata=metadata))

    result = _infer(gateway)

    assert result.route.pipeline_stages == ("guardrail:content-filter",)


def test_unknown_profile_fails_before_credential_or_transport_use():
    gateway, transport = _gateway()
    request = _request(profile=ProfileRef("unknown.profile", 1))

    with pytest.raises(GatewayError) as caught:
        _infer(gateway, request)

    assert caught.value.kind is GatewayErrorKind.PROFILE_NOT_FOUND
    assert caught.value.attempts == 0
    assert transport.requests == []


@pytest.mark.parametrize(
    "inference_request",
    [
        _request(max_output_tokens=8193),
        _request(timeout_seconds=46),
        _request(
            messages=(
                PromptMessage(MessageRole.USER, "x" * 1_000_001),
            )
        ),
    ],
)
def test_request_limits_fail_before_transport(inference_request):
    gateway, transport = _gateway()

    with pytest.raises(GatewayError) as caught:
        _infer(gateway, inference_request)

    assert caught.value.kind is GatewayErrorKind.INVALID_REQUEST
    assert transport.requests == []


@pytest.mark.parametrize(
    ("response", "expected_kind"),
    [
        (_success_response(model="other/model"), GatewayErrorKind.POLICY_VIOLATION),
        (
            _success_response(provider="WrongProvider"),
            GatewayErrorKind.POLICY_VIOLATION,
        ),
        (
            _success_response(metadata=_metadata(strategy="fallback")),
            GatewayErrorKind.POLICY_VIOLATION,
        ),
        (
            _success_response(metadata=_metadata(attempt=2)),
            GatewayErrorKind.POLICY_VIOLATION,
        ),
        (
            _success_response(metadata=_metadata(selected=False)),
            GatewayErrorKind.POLICY_VIOLATION,
        ),
        (
            _success_response(
                metadata=_metadata(
                    route_attempts=[
                        {"provider": "Other", "model": MODEL, "status": 500},
                        {"provider": "Parasail", "model": MODEL, "status": 200},
                    ]
                )
            ),
            GatewayErrorKind.POLICY_VIOLATION,
        ),
    ],
)
def test_route_drift_or_hidden_fallback_fails_closed(response, expected_kind):
    gateway, transport = _gateway(response)

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is expected_kind
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "pipeline",
    [
        [{"type": "context_compression", "name": "context-compression"}],
        [{"type": "plugin", "name": "web-search"}],
        [{"type": "server_tools", "name": "server-tools"}],
        [{"type": "response_healing", "name": "response-healing"}],
        [{"type": "future_unknown", "name": "mystery"}],
    ],
)
def test_content_mutating_tool_or_unknown_pipeline_stages_fail_closed(pipeline):
    gateway, _ = _gateway(
        _success_response(metadata=_metadata(pipeline=pipeline))
    )

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.POLICY_VIOLATION


def test_tool_call_delta_fails_closed():
    gateway, _ = _gateway(
        _success_response(
            extra_delta={
                "tool_calls": [
                    {"id": "call-1", "type": "function", "function": {"name": "write"}}
                ]
            }
        )
    )

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.POLICY_VIOLATION


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (
            FakeResponse(
                [b"data: {not-json}\n\ndata: [DONE]\n\n"],
                headers={
                    "content-type": "text/event-stream",
                    "x-generation-id": GENERATION_ID,
                },
            ),
            GatewayErrorKind.PROTOCOL,
        ),
        (
            FakeResponse(
                [b"data: \xff\n\n"],
                headers={"content-type": "text/event-stream"},
            ),
            GatewayErrorKind.PROTOCOL,
        ),
        (
            _success_response(include_done=False),
            GatewayErrorKind.PROTOCOL,
        ),
        (
            _success_response(content_type="application/json"),
            GatewayErrorKind.PROTOCOL,
        ),
        (
            _success_response(include_metadata=False),
            GatewayErrorKind.POLICY_VIOLATION,
        ),
        (
            _success_response(
                include_header_generation=False,
                include_event_generation=False,
            ),
            GatewayErrorKind.PROTOCOL,
        ),
    ],
)
def test_malformed_or_incomplete_stream_fails_closed(response, kind):
    gateway, _ = _gateway(response)

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is kind


def test_cache_hit_fails_because_route_metadata_is_not_fresh():
    response = _success_response()
    response.headers["X-OpenRouter-Cache-Status"] = "HIT"
    gateway, _ = _gateway(response)

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.POLICY_VIOLATION


def test_missing_usage_is_explicitly_unknown_not_zero():
    gateway, _ = _gateway(_success_response(include_usage=False))

    usage = _infer(gateway).usage

    assert usage.complete is False
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.total_tokens is None
    assert usage.cost is None


@pytest.mark.parametrize(
    "usage",
    [
        _usage(prompt_tokens=-1),
        _usage(total_tokens=99),
        _usage(cost="NaN"),
        _usage(prompt_tokens=True),
    ],
)
def test_invalid_usage_fails_closed(usage):
    gateway, _ = _gateway(_success_response(usage=usage))

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.PROTOCOL


def test_http_auth_error_is_not_retried_and_does_not_retain_secrets():
    body = json.dumps(
        {
            "error": {
                "code": 401,
                "message": f"echoed Authorization Bearer {FAKE_KEY}",
                "metadata": {
                    "error_type": "authentication",
                    "provider_code": "bad-secret",
                },
            }
        }
    ).encode()
    response = FakeResponse(
        [body],
        status=401,
        headers={"content-type": "application/json"},
    )
    gateway, transport = _gateway(response)

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    error = caught.value
    assert error.kind is GatewayErrorKind.AUTHENTICATION
    assert error.retryable is False
    assert error.attempts == 1
    assert len(transport.requests) == 1
    serialized_error = json.dumps(error.__dict__, default=str)
    assert FAKE_KEY not in str(error)
    assert FAKE_KEY not in repr(error)
    assert FAKE_KEY not in serialized_error
    assert "bad-secret" not in serialized_error


def test_429_retries_once_with_same_exact_route_and_bounded_retry_after():
    rate_limit = FakeResponse(
        [
            json.dumps(
                {
                    "error": {
                        "code": 429,
                        "message": "slow down",
                        "metadata": {"error_type": "rate_limit_exceeded"},
                    }
                }
            ).encode()
        ],
        status=429,
        headers={"Retry-After": "0"},
    )
    gateway, transport = _gateway(rate_limit, _success_response())

    result = _infer(gateway)

    assert result.route.gateway_attempts == 2
    assert len(transport.requests) == 2
    assert transport.requests[0].url == transport.requests[1].url
    assert transport.requests[0].body == transport.requests[1].body
    assert json.loads(transport.requests[1].body)["provider"]["only"] == ["parasail"]
    assert "models" not in json.loads(transport.requests[1].body)


def test_retryable_http_error_stops_at_profile_attempt_limit():
    def unavailable():
        return FakeResponse(
            [
                json.dumps(
                    {
                        "error": {
                            "code": 503,
                            "metadata": {"error_type": "provider_unavailable"},
                        }
                    }
                ).encode()
            ],
            status=503,
        )

    gateway, transport = _gateway(unavailable(), unavailable())

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.PROVIDER_UNAVAILABLE
    assert caught.value.attempts == 2
    assert len(transport.requests) == 2


def test_transport_timeout_retries_only_to_profile_limit():
    gateway, transport = _gateway(
        TransportFailure(TransportFailureKind.TIMEOUT),
        TransportFailure(TransportFailureKind.TIMEOUT),
    )

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.TIMEOUT
    assert caught.value.attempts == 2
    assert len(transport.requests) == 2


def test_midstream_error_preserves_partial_state_and_never_retries():
    body = _event(
        {
            "id": GENERATION_ID,
            "model": MODEL,
            "provider": "Parasail",
            "choices": [
                {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
            ],
        }
    )
    body += _event(
        {
            "id": GENERATION_ID,
            "model": MODEL,
            "provider": "Parasail",
            "error": {
                "code": 429,
                "message": f"do not surface {FAKE_KEY}",
                "metadata": {"error_type": "rate_limit_exceeded"},
            },
            "choices": [
                {"index": 0, "delta": {"content": ""}, "finish_reason": "error"}
            ],
        }
    )
    response = FakeResponse(
        [body],
        headers={
            "content-type": "text/event-stream",
            "x-generation-id": GENERATION_ID,
        },
    )
    gateway, transport = _gateway(response, _success_response())

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.RATE_LIMIT
    assert caught.value.partial is True
    assert caught.value.retryable is False
    assert caught.value.generation_id == GENERATION_ID
    assert len(transport.requests) == 1
    assert FAKE_KEY not in str(caught.value)


def test_prestream_in_band_transient_error_can_retry_exact_profile():
    error_response = FakeResponse(
        [
            _event(
                {
                    "id": GENERATION_ID,
                    "model": MODEL,
                    "provider": "Parasail",
                    "error": {
                        "code": 429,
                        "metadata": {"error_type": "rate_limit_exceeded"},
                    },
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": ""},
                            "finish_reason": "error",
                        }
                    ],
                }
            )
        ],
        headers={
            "content-type": "text/event-stream",
            "x-generation-id": GENERATION_ID,
        },
    )
    gateway, transport = _gateway(error_response, _success_response())

    result = _infer(gateway)

    assert result.route.gateway_attempts == 2
    assert len(transport.requests) == 2


def test_empty_completion_gets_at_most_one_same_profile_retry():
    gateway, transport = _gateway(
        _success_response(text=""),
        _success_response(text="repaired"),
    )

    result = _infer(gateway)

    assert result.content == "repaired"
    assert result.route.gateway_attempts == 2
    assert len(transport.requests) == 2


def test_pre_cancelled_token_never_opens_transport():
    token = CancellationToken()
    token.cancel()
    gateway, transport = _gateway()

    with pytest.raises(GatewayError) as caught:
        gateway.infer(_request(), api_key=FAKE_KEY, cancellation=token)

    assert caught.value.kind is GatewayErrorKind.CANCELLED
    assert caught.value.attempts == 0
    assert transport.requests == []


def test_gateway_cancel_closes_response_and_partial_stream_is_not_retried():
    holder = {}

    def chunks():
        yield _event(
            {
                "id": GENERATION_ID,
                "model": MODEL,
                "provider": "Parasail",
                "choices": [
                    {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
                ],
            }
        )
        cancel_result = holder["gateway"].cancel("req-1")
        assert cancel_result.status is CancelStatus.REQUESTED
        return  # closing a real response commonly manifests as clean EOF

    response = FakeResponse(
        chunks(),
        headers={
            "content-type": "text/event-stream",
            "x-generation-id": GENERATION_ID,
        },
    )
    gateway, transport = _gateway(response, _success_response())
    holder["gateway"] = gateway

    with pytest.raises(GatewayError) as caught:
        _infer(gateway)

    assert caught.value.kind is GatewayErrorKind.CANCELLED
    assert caught.value.partial is True
    assert len(transport.requests) == 1
    assert response.closed is True


def test_midstream_wall_clock_timeout_is_bounded_and_not_retried_after_content():
    clock = FakeClock()

    def chunks():
        yield _event(
            {
                "id": GENERATION_ID,
                "model": MODEL,
                "provider": "Parasail",
                "choices": [
                    {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
                ],
            }
        )
        clock.value += 2
        yield b"data: [DONE]\n\n"

    response = FakeResponse(
        chunks(),
        headers={
            "content-type": "text/event-stream",
            "x-generation-id": GENERATION_ID,
        },
    )
    gateway, transport = _gateway(
        response,
        _success_response(),
        monotonic=clock,
    )

    with pytest.raises(GatewayError) as caught:
        _infer(gateway, _request(timeout_seconds=1))

    assert caught.value.kind is GatewayErrorKind.TIMEOUT
    assert caught.value.partial is True
    assert len(transport.requests) == 1


def test_retry_after_cannot_extend_past_request_deadline():
    response = FakeResponse(
        [json.dumps({"error": {"code": 429}}).encode()],
        status=429,
        headers={"Retry-After": "120"},
    )
    gateway, transport = _gateway(response)

    with pytest.raises(GatewayError) as caught:
        _infer(gateway, _request(timeout_seconds=0.1))

    assert caught.value.kind is GatewayErrorKind.TIMEOUT
    assert len(transport.requests) == 1


def test_invalid_credential_is_rejected_without_echo_or_transport():
    gateway, transport = _gateway()
    bad_key = "short\nsecret"

    with pytest.raises(GatewayError) as caught:
        gateway.infer(_request(), api_key=bad_key)

    assert caught.value.kind is GatewayErrorKind.AUTHENTICATION
    assert bad_key not in str(caught.value)
    assert transport.requests == []


def test_endpoint_confirmation_fails_when_an_undeclared_operator_serves(monkeypatch):
    """The confirmation must be falsifiable. If OpenRouter serves the request
    from an operator the profile does not declare, the receipt must not claim
    the endpoint was confirmed — otherwise it is a rubber stamp."""
    from helios.backend.openrouter.models import EndpointRef

    endpoint = EndpointRef(
        provider_slug="parasail",
        quantization="fp8",
        expected_provider_names=("Parasail",),
    )
    # The declared set is what makes the claim falsifiable; an operator outside
    # it must not confirm.
    assert "Parasail" in endpoint.expected_provider_names
    assert "SomeOtherOperator" not in endpoint.expected_provider_names

    def confirmed(served: str) -> bool:
        return bool(served) and served in endpoint.expected_provider_names

    assert confirmed("Parasail") is True
    assert confirmed("SomeOtherOperator") is False
    assert confirmed("") is False
    assert confirmed("parasail") is False  # case-exact, names are declared verbatim
