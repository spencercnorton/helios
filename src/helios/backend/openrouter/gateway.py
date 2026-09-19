"""Fail-closed, GTK-free OpenRouter chat-completions gateway.

This is intentionally a narrow manual-evaluation transport:

* exact versioned profiles from :mod:`profiles`, never arbitrary model IDs;
* text-only system/user messages, never tools or workspace capabilities;
* one exact provider+quantization endpoint and no provider/model fallback;
* mandatory ZDR, data-collection denial, parameter support, metadata, and
  response-cache opt-out;
* bounded streaming, timeout, retry, and cancellation;
* content-free/redacted failures.

It does not load credentials, expose a UI/MCP surface, route tasks, or write
Helios state.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .models import (
    CancelResult,
    CancelStatus,
    InferenceRequest,
    InferenceResult,
    ReadOnlyModelProfile,
    RouteReceipt,
    UsageReceipt,
)
from .profiles import get_profile
from .sse import SSEProtocolError, iter_sse_events
from .transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TransportFailure,
    TransportFailureKind,
    UrlLibTransport,
)


CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_MAX_ERROR_BODY_BYTES = 64 * 1024
_MAX_SSE_EVENT_BYTES = 512 * 1024
_SAFE_ERROR_TYPE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_DISABLED_PLUGIN_IDS = (
    "web",
    "file-parser",
    "response-healing",
    "pareto-router",
    "context-compression",
)


class GatewayErrorKind(StrEnum):
    INVALID_REQUEST = "invalid_request"
    PROFILE_NOT_FOUND = "profile_not_found"
    POLICY_VIOLATION = "policy_violation"
    AUTHENTICATION = "authentication"
    PAYMENT_REQUIRED = "payment_required"
    RATE_LIMIT = "rate_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    HTTP = "http"
    PROTOCOL = "protocol"
    EMPTY_RESPONSE = "empty_response"


class GatewayError(Exception):
    """A safe failure envelope that never retains wire bodies or credentials."""

    def __init__(
        self,
        kind: GatewayErrorKind,
        *,
        request_id: str,
        attempts: int = 0,
        retryable: bool = False,
        http_status: int | None = None,
        provider_error_type: str | None = None,
        partial: bool = False,
        generation_id: str | None = None,
        retry_after_seconds: float | None = None,
    ):
        self.kind = kind
        self.request_id = request_id
        self.attempts = attempts
        self.retryable = retryable
        self.http_status = http_status
        self.provider_error_type = provider_error_type
        self.partial = partial
        self.generation_id = generation_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"OpenRouter request failed ({kind.value})")

    def with_partial(self) -> GatewayError:
        if self.partial:
            return self
        return GatewayError(
            self.kind,
            request_id=self.request_id,
            attempts=self.attempts,
            retryable=False,
            http_status=self.http_status,
            provider_error_type=self.provider_error_type,
            partial=True,
            generation_id=self.generation_id,
            retry_after_seconds=None,
        )


class CancellationToken:
    """Thread-safe cooperative cancellation with response-close callbacks."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_callback_id = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        with self._lock:
            was_cancelled = self._event.is_set()
            self._event.set()
            callbacks = tuple(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # Cancellation must remain content-free and best effort.
                pass
        return not was_cancelled

    def wait(self, timeout_seconds: float) -> bool:
        return self._event.wait(timeout_seconds)

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            if self._event.is_set():
                call_now = True
                callback_id = -1
            else:
                call_now = False
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._callbacks[callback_id] = callback
        if call_now:
            try:
                callback()
            except Exception:
                pass

        def remove() -> None:
            if callback_id < 0:
                return
            with self._lock:
                self._callbacks.pop(callback_id, None)

        return remove


@dataclass(slots=True)
class _StreamState:
    content_parts: list[str]
    event_generation_id: str | None = None
    actual_model: str | None = None
    top_level_provider: str | None = None
    finish_reason: str | None = None
    native_finish_reason: str | None = None
    usage: UsageReceipt | None = None
    metadata: Mapping[str, object] | None = None
    done: bool = False


class OpenRouterGateway:
    """Synchronous gateway suitable for execution on a supervisor worker thread."""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
        random_fraction: Callable[[], float] = random.random,
    ):
        self._transport = transport or UrlLibTransport()
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._random_fraction = random_fraction
        self._active_lock = threading.Lock()
        self._active: dict[str, CancellationToken] = {}

    def infer(
        self,
        request: InferenceRequest,
        *,
        api_key: str,
        cancellation: CancellationToken | None = None,
    ) -> InferenceResult:
        """Execute one exact-profile inference.

        The credential is supplied by the future supervisor credential broker
        for this call only. The gateway does not read, cache, log, or return it.
        """

        profile = self._resolve_and_validate(request)
        self._validate_credential(api_key, request.request_id)
        token = cancellation or CancellationToken()
        self._register_active(request.request_id, token)
        started_at = self._timestamp()
        started_monotonic = self._monotonic()
        timeout_seconds = min(
            profile.timeout_seconds,
            request.timeout_seconds or profile.timeout_seconds,
        )
        deadline = started_monotonic + timeout_seconds
        wire_request, request_digest = self._build_http_request(
            request,
            profile,
            api_key,
        )

        try:
            last_error: GatewayError | None = None
            for attempt in range(1, profile.max_attempts + 1):
                self._check_abort(
                    request.request_id,
                    token,
                    deadline,
                    attempts=attempt - 1,
                )
                try:
                    response = self._transport.open(
                        wire_request,
                        timeout_seconds=max(0.001, deadline - self._monotonic()),
                    )
                except TransportFailure as exc:
                    last_error = self._transport_error(
                        exc,
                        request_id=request.request_id,
                        attempts=attempt,
                    )
                else:
                    remove_cancel_callback = token.add_callback(
                        lambda: _safe_close(response)
                    )
                    deadline_timer = threading.Timer(
                        max(0.0, deadline - self._monotonic()),
                        _safe_close,
                        args=(response,),
                    )
                    deadline_timer.daemon = True
                    deadline_timer.start()
                    try:
                        if response.status != 200:
                            last_error = self._http_error(
                                response,
                                request_id=request.request_id,
                                token=token,
                                deadline=deadline,
                                attempts=attempt,
                            )
                        else:
                            return self._consume_success(
                                response,
                                request=request,
                                profile=profile,
                                token=token,
                                deadline=deadline,
                                gateway_attempts=attempt,
                                request_digest=request_digest,
                                started_at=started_at,
                            )
                    except GatewayError as exc:
                        last_error = exc
                    except TransportFailure as exc:
                        last_error = self._transport_error(
                            exc,
                            request_id=request.request_id,
                            attempts=attempt,
                        )
                    finally:
                        deadline_timer.cancel()
                        remove_cancel_callback()
                        _safe_close(response)

                assert last_error is not None
                if (
                    not last_error.retryable
                    or last_error.partial
                    or attempt >= profile.max_attempts
                ):
                    raise last_error
                self._wait_before_retry(
                    request_id=request.request_id,
                    token=token,
                    deadline=deadline,
                    attempt=attempt,
                    retry_after_seconds=last_error.retry_after_seconds,
                )
            raise last_error or GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request.request_id,
            )
        finally:
            self._unregister_active(request.request_id, token)

    def cancel(self, request_id: str) -> CancelResult:
        """Request cancellation and close an active response, if present."""

        with self._active_lock:
            token = self._active.get(request_id)
        if token is None:
            return CancelResult(request_id=request_id, status=CancelStatus.NOT_ACTIVE)
        token.cancel()
        return CancelResult(request_id=request_id, status=CancelStatus.REQUESTED)

    def _resolve_and_validate(
        self,
        request: InferenceRequest,
    ) -> ReadOnlyModelProfile:
        try:
            profile = get_profile(request.profile)
        except LookupError as exc:
            raise GatewayError(
                GatewayErrorKind.PROFILE_NOT_FOUND,
                request_id=request.request_id,
            ) from exc
        if not profile.read_only or profile.allowed_tools or profile.workspace_access != "none":
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request.request_id,
            )
        if request.max_output_tokens > profile.max_output_tokens:
            raise GatewayError(
                GatewayErrorKind.INVALID_REQUEST,
                request_id=request.request_id,
            )
        if request.timeout_seconds is not None and request.timeout_seconds > profile.timeout_seconds:
            raise GatewayError(
                GatewayErrorKind.INVALID_REQUEST,
                request_id=request.request_id,
            )
        input_chars = sum(len(message.content) for message in request.messages)
        if input_chars > profile.max_input_chars:
            raise GatewayError(
                GatewayErrorKind.INVALID_REQUEST,
                request_id=request.request_id,
            )
        return profile

    @staticmethod
    def _validate_credential(api_key: str, request_id: str) -> None:
        if (
            not isinstance(api_key, str)
            or len(api_key) < 16
            or api_key != api_key.strip()
            or any(character in api_key for character in ("\r", "\n", "\x00"))
        ):
            raise GatewayError(
                GatewayErrorKind.AUTHENTICATION,
                request_id=request_id,
            )

    def _register_active(self, request_id: str, token: CancellationToken) -> None:
        with self._active_lock:
            if request_id in self._active:
                raise GatewayError(
                    GatewayErrorKind.INVALID_REQUEST,
                    request_id=request_id,
                )
            self._active[request_id] = token

    def _unregister_active(self, request_id: str, token: CancellationToken) -> None:
        with self._active_lock:
            if self._active.get(request_id) is token:
                self._active.pop(request_id, None)

    @staticmethod
    def _build_http_request(
        request: InferenceRequest,
        profile: ReadOnlyModelProfile,
        api_key: str,
    ) -> tuple[HttpRequest, str]:
        provider = {
            "only": [profile.endpoint.provider_slug],
            "order": [profile.endpoint.provider_slug],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
            "quantizations": [profile.endpoint.quantization],
            "max_price": {
                "prompt": float(profile.max_prompt_price_per_million),
                "completion": float(profile.max_completion_price_per_million),
            },
        }
        payload = {
            "model": profile.requested_model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "stream": True,
            "n": 1,
            "max_tokens": request.max_output_tokens,
            "provider": provider,
            "plugins": [
                {"id": plugin_id, "enabled": False}
                for plugin_id in _DISABLED_PLUGIN_IDS
            ],
        }
        canonical_body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = f"sha256:{hashlib.sha256(canonical_body).hexdigest()}"
        return (
            HttpRequest(
                method="POST",
                url=CHAT_COMPLETIONS_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "X-OpenRouter-Metadata": "enabled",
                    "X-OpenRouter-Cache": "false",
                    "X-OpenRouter-Title": "Helios",
                },
                body=canonical_body,
            ),
            digest,
        )

    def _consume_success(
        self,
        response: HttpResponse,
        *,
        request: InferenceRequest,
        profile: ReadOnlyModelProfile,
        token: CancellationToken,
        deadline: float,
        gateway_attempts: int,
        request_digest: str,
        started_at: str,
    ) -> InferenceResult:
        headers = _lower_headers(response.headers)
        content_type = headers.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request.request_id,
                attempts=gateway_attempts,
            )
        if headers.get("x-openrouter-cache-status", "").upper() == "HIT":
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request.request_id,
                attempts=gateway_attempts,
            )
        header_generation_id = headers.get("x-generation-id")
        state = _StreamState(content_parts=[])

        def checked_chunks() -> Iterable[bytes]:
            for chunk in response.iter_bytes():
                self._check_abort(
                    request.request_id,
                    token,
                    deadline,
                    attempts=gateway_attempts,
                )
                yield chunk
            # A cancellation/deadline watchdog may close the socket cleanly,
            # producing EOF rather than an exception. Preserve the real cause.
            self._check_abort(
                request.request_id,
                token,
                deadline,
                attempts=gateway_attempts,
            )

        try:
            events = iter_sse_events(
                checked_chunks(),
                max_event_bytes=min(
                    _MAX_SSE_EVENT_BYTES,
                    profile.max_response_bytes,
                ),
                max_stream_bytes=profile.max_response_bytes,
            )
            for event in events:
                self._check_abort(
                    request.request_id,
                    token,
                    deadline,
                    attempts=gateway_attempts,
                )
                if state.done:
                    raise GatewayError(
                        GatewayErrorKind.PROTOCOL,
                        request_id=request.request_id,
                        attempts=gateway_attempts,
                        partial=_has_content(state),
                    )
                if event.data == "[DONE]":
                    state.done = True
                    continue
                self._apply_stream_event(
                    event.data,
                    state=state,
                    request_id=request.request_id,
                    profile=profile,
                    attempts=gateway_attempts,
                )
        except SSEProtocolError as exc:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request.request_id,
                attempts=gateway_attempts,
                partial=_has_content(state),
                generation_id=state.event_generation_id or header_generation_id,
            ) from exc
        except TransportFailure as exc:
            error = self._transport_error(
                exc,
                request_id=request.request_id,
                attempts=gateway_attempts,
            )
            raise error.with_partial() if _has_content(state) else error
        except GatewayError as exc:
            raise exc.with_partial() if _has_content(state) and not exc.partial else exc

        if not state.done:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request.request_id,
                attempts=gateway_attempts,
                partial=_has_content(state),
                generation_id=state.event_generation_id or header_generation_id,
            )
        generation_id = state.event_generation_id or header_generation_id
        if not generation_id:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request.request_id,
                attempts=gateway_attempts,
            )
        if (
            state.event_generation_id
            and header_generation_id
            and state.event_generation_id != header_generation_id
        ):
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request.request_id,
                attempts=gateway_attempts,
            )
        if state.metadata is None:
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request.request_id,
                attempts=gateway_attempts,
                generation_id=generation_id,
            )
        (
            router_strategy,
            router_attempt,
            router_region,
            selected_provider,
            selected_model,
            pipeline_stages,
        ) = self._validate_router_metadata(
            state.metadata,
            profile=profile,
            request_id=request.request_id,
            attempts=gateway_attempts,
            top_level_provider=state.top_level_provider,
            actual_model=state.actual_model,
        )
        content = "".join(state.content_parts)
        if not content.strip():
            raise GatewayError(
                GatewayErrorKind.EMPTY_RESPONSE,
                request_id=request.request_id,
                attempts=gateway_attempts,
                retryable=True,
                generation_id=generation_id,
            )
        if state.finish_reason != "stop":
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request.request_id,
                attempts=gateway_attempts,
                partial=True,
                generation_id=generation_id,
            )
        usage = state.usage or UsageReceipt(
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            reasoning_tokens=None,
            cached_tokens=None,
            cache_write_tokens=None,
            cost=None,
            complete=False,
        )
        return InferenceResult(
            request_id=request.request_id,
            content=content,
            finish_reason=state.finish_reason,
            native_finish_reason=state.native_finish_reason,
            route=RouteReceipt(
                profile=profile.ref,
                catalog_snapshot=profile.catalog_snapshot,
                requested_model=profile.requested_model,
                actual_model=state.actual_model or profile.requested_model,
                actual_endpoint_model=selected_model,
                endpoint=profile.endpoint,
                actual_provider=selected_provider,
                # Half of a two-sided confirmation, and only half deliberately.
                #
                # This side is the post-response check: the operator that
                # actually served the request is one this profile declares. On
                # its own it is weak, which is why it was hardcoded False — a
                # base provider slug can cover future operator variants, so a
                # match here does not by itself prove the exact variant.
                #
                # The other side is pre-dispatch and lives outside this module:
                # the broker confirms the profile's exact slug *and*
                # quantization against OpenRouter's own endpoints catalog before
                # the profile is admitted at all (router_promotion
                # .verified_endpoints), and a profile it could not confirm never
                # reaches dispatch. Neither side can self-certify — the catalog
                # is evidence the service did not author, and this check cannot
                # pass unless a declared operator really served the response.
                endpoint_confirmed=bool(selected_provider)
                and selected_provider in profile.endpoint.expected_provider_names,
                generation_id=generation_id,
                gateway_attempts=gateway_attempts,
                router_strategy=router_strategy,
                router_attempt=router_attempt,
                router_region=router_region,
                pipeline_stages=pipeline_stages,
                request_digest=request_digest,
                started_at=started_at,
                completed_at=self._timestamp(),
                require_parameters=True,
                data_collection="deny",
                zdr=True,
                provider_fallbacks=False,
                cross_model_fallback=False,
                tools_enabled=False,
                response_cache_enabled=False,
                context_compression_enabled=False,
            ),
            usage=usage,
        )

    def _apply_stream_event(
        self,
        raw_data: str,
        *,
        state: _StreamState,
        request_id: str,
        profile: ReadOnlyModelProfile,
        attempts: int,
    ) -> None:
        try:
            payload = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError) as exc:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            ) from exc
        if not isinstance(payload, dict):
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            )

        generation_id = payload.get("id")
        if generation_id is not None:
            if not isinstance(generation_id, str) or not generation_id:
                raise GatewayError(
                    GatewayErrorKind.PROTOCOL,
                    request_id=request_id,
                    attempts=attempts,
                )
            if (
                state.event_generation_id is not None
                and state.event_generation_id != generation_id
            ):
                raise GatewayError(
                    GatewayErrorKind.PROTOCOL,
                    request_id=request_id,
                    attempts=attempts,
                )
            state.event_generation_id = generation_id

        model = payload.get("model")
        if model is not None:
            if model not in profile.expected_response_models:
                raise GatewayError(
                    GatewayErrorKind.POLICY_VIOLATION,
                    request_id=request_id,
                    attempts=attempts,
                )
            state.actual_model = model

        provider = payload.get("provider")
        if provider is not None:
            if not isinstance(provider, str) or not provider.strip():
                raise GatewayError(
                    GatewayErrorKind.PROTOCOL,
                    request_id=request_id,
                    attempts=attempts,
                )
            state.top_level_provider = provider

        if "error" in payload:
            error_type, status = _parse_wire_error(payload["error"])
            kind, retryable = _classify_error(status, error_type)
            raise GatewayError(
                kind,
                request_id=request_id,
                attempts=attempts,
                retryable=retryable and not _has_content(state),
                http_status=status,
                provider_error_type=error_type,
                partial=_has_content(state),
                generation_id=state.event_generation_id,
            )

        choices = payload.get("choices")
        if choices is not None:
            if not isinstance(choices, list) or len(choices) > 1:
                raise GatewayError(
                    GatewayErrorKind.PROTOCOL,
                    request_id=request_id,
                    attempts=attempts,
                )
            if choices:
                choice = choices[0]
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    raise GatewayError(
                        GatewayErrorKind.PROTOCOL,
                        request_id=request_id,
                        attempts=attempts,
                    )
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise GatewayError(
                        GatewayErrorKind.PROTOCOL,
                        request_id=request_id,
                        attempts=attempts,
                    )
                if delta.get("tool_calls") or delta.get("function_call"):
                    raise GatewayError(
                        GatewayErrorKind.POLICY_VIOLATION,
                        request_id=request_id,
                        attempts=attempts,
                    )
                content = delta.get("content")
                if content is not None:
                    if not isinstance(content, str):
                        raise GatewayError(
                            GatewayErrorKind.PROTOCOL,
                            request_id=request_id,
                            attempts=attempts,
                        )
                    state.content_parts.append(content)
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None:
                    if not isinstance(finish_reason, str) or not finish_reason:
                        raise GatewayError(
                            GatewayErrorKind.PROTOCOL,
                            request_id=request_id,
                            attempts=attempts,
                        )
                    state.finish_reason = finish_reason
                native_finish_reason = choice.get("native_finish_reason")
                if native_finish_reason is not None:
                    if not isinstance(native_finish_reason, str):
                        raise GatewayError(
                            GatewayErrorKind.PROTOCOL,
                            request_id=request_id,
                            attempts=attempts,
                        )
                    state.native_finish_reason = native_finish_reason

        if "usage" in payload:
            state.usage = _parse_usage(
                payload["usage"],
                request_id=request_id,
                attempts=attempts,
            )
        if "openrouter_metadata" in payload:
            metadata = payload["openrouter_metadata"]
            if not isinstance(metadata, dict):
                raise GatewayError(
                    GatewayErrorKind.PROTOCOL,
                    request_id=request_id,
                    attempts=attempts,
                )
            state.metadata = metadata

    @staticmethod
    def _validate_router_metadata(
        metadata: Mapping[str, object],
        *,
        profile: ReadOnlyModelProfile,
        request_id: str,
        attempts: int,
        top_level_provider: str | None,
        actual_model: str | None,
    ) -> tuple[str, int, str | None, str, str, tuple[str, ...]]:
        requested = metadata.get("requested")
        strategy = metadata.get("strategy")
        router_attempt = metadata.get("attempt")
        region = metadata.get("region")
        if requested != profile.requested_model or strategy != "direct":
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        if type(router_attempt) is not int or router_attempt != 1:
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        if region is not None and not isinstance(region, str):
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            )

        endpoints = metadata.get("endpoints")
        if not isinstance(endpoints, dict):
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        available = endpoints.get("available")
        if not isinstance(available, list):
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        selected = [
            endpoint
            for endpoint in available
            if isinstance(endpoint, dict) and endpoint.get("selected") is True
        ]
        if len(selected) != 1:
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        selected_provider = selected[0].get("provider")
        selected_model = selected[0].get("model")
        if (
            not isinstance(selected_provider, str)
            or selected_model not in profile.expected_endpoint_models
        ):
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        if not _provider_matches(selected_provider, profile):
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        if top_level_provider is not None and not _same_provider(
            top_level_provider,
            selected_provider,
        ):
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )
        response_model = actual_model or profile.requested_model
        if not profile.allows_model_identity(response_model, selected_model):
            raise GatewayError(
                GatewayErrorKind.POLICY_VIOLATION,
                request_id=request_id,
                attempts=attempts,
            )

        route_attempts = metadata.get("attempts")
        if route_attempts is not None:
            if not isinstance(route_attempts, list) or len(route_attempts) > 1:
                raise GatewayError(
                    GatewayErrorKind.POLICY_VIOLATION,
                    request_id=request_id,
                    attempts=attempts,
                )
            for route_attempt in route_attempts:
                if (
                    not isinstance(route_attempt, dict)
                    or route_attempt.get("model")
                    not in profile.expected_endpoint_models
                    or route_attempt.get("status") != 200
                    or not isinstance(route_attempt.get("provider"), str)
                    or not _provider_matches(route_attempt["provider"], profile)
                ):
                    raise GatewayError(
                        GatewayErrorKind.POLICY_VIOLATION,
                        request_id=request_id,
                        attempts=attempts,
                    )

        pipeline = metadata.get("pipeline", [])
        if not isinstance(pipeline, list):
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            )
        pipeline_stages: list[str] = []
        for stage in pipeline:
            if not isinstance(stage, dict):
                raise GatewayError(
                    GatewayErrorKind.PROTOCOL,
                    request_id=request_id,
                    attempts=attempts,
                )
            stage_type = stage.get("type")
            stage_name = stage.get("name")
            if not isinstance(stage_type, str) or not isinstance(stage_name, str):
                raise GatewayError(
                    GatewayErrorKind.PROTOCOL,
                    request_id=request_id,
                    attempts=attempts,
                )
            # Guardrails may inspect/block, but any content-mutating, tool, or
            # unknown stage is outside this profile's authority.
            if stage_type != "guardrail":
                raise GatewayError(
                    GatewayErrorKind.POLICY_VIOLATION,
                    request_id=request_id,
                    attempts=attempts,
                )
            pipeline_stages.append(f"{stage_type}:{stage_name}")

        return (
            str(strategy),
            router_attempt,
            region,
            selected_provider,
            selected_model,
            tuple(pipeline_stages),
        )

    def _http_error(
        self,
        response: HttpResponse,
        *,
        request_id: str,
        token: CancellationToken,
        deadline: float,
        attempts: int,
    ) -> GatewayError:
        body_parts: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            self._check_abort(request_id, token, deadline, attempts=attempts)
            if not isinstance(chunk, bytes):
                break
            remaining = _MAX_ERROR_BODY_BYTES - size
            if remaining <= 0:
                break
            body_parts.append(chunk[:remaining])
            size += min(len(chunk), remaining)
        error_type, wire_status = _parse_error_body(b"".join(body_parts))
        status = response.status
        if wire_status is not None and wire_status != status:
            error_type = "invalid_error_envelope"
        kind, retryable = _classify_error(status, error_type)
        retry_after = _parse_retry_after(_lower_headers(response.headers))
        return GatewayError(
            kind,
            request_id=request_id,
            attempts=attempts,
            retryable=retryable,
            http_status=status,
            provider_error_type=error_type,
            retry_after_seconds=retry_after,
        )

    @staticmethod
    def _transport_error(
        failure: TransportFailure,
        *,
        request_id: str,
        attempts: int,
    ) -> GatewayError:
        kind = (
            GatewayErrorKind.TIMEOUT
            if failure.kind is TransportFailureKind.TIMEOUT
            else GatewayErrorKind.PROVIDER_UNAVAILABLE
        )
        return GatewayError(
            kind,
            request_id=request_id,
            attempts=attempts,
            retryable=True,
        )

    def _wait_before_retry(
        self,
        *,
        request_id: str,
        token: CancellationToken,
        deadline: float,
        attempt: int,
        retry_after_seconds: float | None,
    ) -> None:
        base = min(2.0, 0.25 * (2 ** (attempt - 1)))
        jitter = max(0.0, min(1.0, self._random_fraction())) * base * 0.25
        delay = min(2.0, retry_after_seconds if retry_after_seconds is not None else base + jitter)
        remaining = deadline - self._monotonic()
        if remaining <= delay:
            raise GatewayError(
                GatewayErrorKind.TIMEOUT,
                request_id=request_id,
                attempts=attempt,
            )
        if token.wait(delay):
            raise GatewayError(
                GatewayErrorKind.CANCELLED,
                request_id=request_id,
                attempts=attempt,
            )

    def _check_abort(
        self,
        request_id: str,
        token: CancellationToken,
        deadline: float,
        *,
        attempts: int,
    ) -> None:
        if token.cancelled:
            raise GatewayError(
                GatewayErrorKind.CANCELLED,
                request_id=request_id,
                attempts=attempts,
            )
        if self._monotonic() >= deadline:
            raise GatewayError(
                GatewayErrorKind.TIMEOUT,
                request_id=request_id,
                attempts=attempts,
            )

    def _timestamp(self) -> str:
        value = self._utcnow()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_usage(raw: object, *, request_id: str, attempts: int) -> UsageReceipt:
    if not isinstance(raw, dict):
        raise GatewayError(
            GatewayErrorKind.PROTOCOL,
            request_id=request_id,
            attempts=attempts,
        )

    def token(name: str, source: Mapping[str, object] = raw) -> int | None:
        value = source.get(name)
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            )
        return value

    prompt_tokens = token("prompt_tokens")
    completion_tokens = token("completion_tokens")
    total_tokens = token("total_tokens")
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
        raise GatewayError(
            GatewayErrorKind.PROTOCOL,
            request_id=request_id,
            attempts=attempts,
        )
    cached_tokens = token("cached_tokens", prompt_details)
    cache_write_tokens = token("cache_write_tokens", prompt_details)
    reasoning_tokens = token("reasoning_tokens", completion_details)
    raw_cost = raw.get("cost")
    cost: Decimal | None = None
    if raw_cost is not None:
        if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float, str)):
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            )
        try:
            cost = Decimal(str(raw_cost))
        except InvalidOperation as exc:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            ) from exc
        if not cost.is_finite() or cost < 0:
            raise GatewayError(
                GatewayErrorKind.PROTOCOL,
                request_id=request_id,
                attempts=attempts,
            )
    if (
        prompt_tokens is not None
        and completion_tokens is not None
        and total_tokens is not None
        and total_tokens != prompt_tokens + completion_tokens
    ):
        raise GatewayError(
            GatewayErrorKind.PROTOCOL,
            request_id=request_id,
            attempts=attempts,
        )
    complete = all(
        value is not None
        for value in (prompt_tokens, completion_tokens, total_tokens, cost)
    )
    return UsageReceipt(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        cost=cost,
        complete=complete,
    )


def _parse_error_body(body: bytes) -> tuple[str | None, int | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return _parse_wire_error(payload.get("error"))


def _parse_wire_error(raw: object) -> tuple[str | None, int | None]:
    if not isinstance(raw, dict):
        return None, None
    status = raw.get("code")
    if type(status) is not int:
        status = None
    metadata = raw.get("metadata")
    error_type = metadata.get("error_type") if isinstance(metadata, dict) else None
    if not isinstance(error_type, str) or not _SAFE_ERROR_TYPE_RE.fullmatch(error_type):
        error_type = None
    return error_type, status


def _classify_error(
    status: int | None,
    error_type: str | None,
) -> tuple[GatewayErrorKind, bool]:
    if error_type == "authentication" or status == 401:
        return GatewayErrorKind.AUTHENTICATION, False
    if error_type == "payment_required" or status == 402:
        return GatewayErrorKind.PAYMENT_REQUIRED, False
    if error_type == "rate_limit_exceeded" or status == 429:
        return GatewayErrorKind.RATE_LIMIT, True
    if error_type == "timeout" or status == 408:
        return GatewayErrorKind.TIMEOUT, True
    if error_type in {"provider_overloaded", "provider_unavailable", "server"}:
        return GatewayErrorKind.PROVIDER_UNAVAILABLE, True
    if status in {502, 503, 504, 529}:
        return GatewayErrorKind.PROVIDER_UNAVAILABLE, True
    if status in {400, 403, 404, 405, 409, 413, 422}:
        return GatewayErrorKind.HTTP, False
    return GatewayErrorKind.HTTP, bool(status is not None and status >= 500)


def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not 0 <= seconds <= 2:
        return 2.0 if seconds > 2 else None
    return seconds


def _provider_matches(value: str, profile: ReadOnlyModelProfile) -> bool:
    normalized = _normalize_provider(value)
    expected = {
        _normalize_provider(profile.endpoint.provider_slug),
        *(
            _normalize_provider(name)
            for name in profile.endpoint.expected_provider_names
        ),
    }
    return normalized in expected


def _same_provider(left: str, right: str) -> bool:
    return _normalize_provider(left) == _normalize_provider(right)


def _normalize_provider(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _has_content(state: _StreamState) -> bool:
    return any(part != "" for part in state.content_parts)


def _safe_close(response: HttpResponse) -> None:
    try:
        response.close()
    except Exception:
        # Close is best effort on cancellation/deadline paths and must not
        # replace the typed gateway outcome with transport implementation data.
        pass
