"""Privileged local broker for Helios OpenRouter inference.

The system service owns the credential and the network call.  Claude's MCP
adapter and Codex dynamic tools receive only a Unix-socket capability and
provider-neutral task schemas.
"""

from __future__ import annotations

import argparse
import dataclasses
import grp
import hashlib
import json
import os
import signal
import socket
import socketserver
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from helios import __version__
from helios.backend.openrouter import (
    PROFILES,
    CancellationToken,
    GatewayError,
    InferenceRequest,
    MessageRole,
    OpenRouterGateway,
    ProfileRef,
    PromptMessage,
)
from helios.backend.router_client import PROTOCOL_VERSION
from helios.backend.router_policy import (
    CANARY_PROFILE,
    TaskSpecError,
    compile_messages,
    decide_route,
    normalize_task_spec,
    validate_result_content,
)
from helios.backend import router_promotion, specialty_registry
from helios.backend.router_tools import CONTRACT_VERSION, tool_names


_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_KEY_STATUS_URL = "https://openrouter.ai/api/v1/key"
# v3 (2026-09-05): the shadow candidate changed profile. Preview consent is
# consent to evaluate a *specific* route, so a persisted v2 bit must not carry
# over to a different model — `_load_state` drops the bit when the id moves.
# Bump this whenever the canary profile changes, not only when dispatch policy
# does. (It happens to be a no-op today: preview is off.)
_CONTROL_POLICY_ID = "shadow-preview.v3"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HEALTH_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class BrokerCommandError(RuntimeError):
    def __init__(self, code: str, message: str, *, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class RouterBroker:
    def __init__(
        self,
        *,
        api_key: str,
        state_dir: Path,
        gateway: OpenRouterGateway | None = None,
        promoted_profiles: frozenset[ProfileRef] = frozenset(),
        verified_endpoints: frozenset[ProfileRef] = frozenset(),
        endpoint_verifier: Callable[[], frozenset[ProfileRef]] | None = None,
        context_authorizer: Callable[
            [dict[str, str], dict[str, Any]],
            bool,
        ]
        | None = None,
    ) -> None:
        self._api_key = api_key
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._state_dir, 0o750)
        self._state_path = self._state_dir / "state.json"
        self._ledger_path = self._state_dir / "commands.sqlite3"
        self._receipt_path = self._state_dir / "receipts.jsonl"
        self._gateway = gateway or OpenRouterGateway()
        self._promoted_profiles = frozenset(promoted_profiles)
        self._verified_endpoints = frozenset(verified_endpoints)
        # Without a verifier the set stays exactly as constructed, which is what
        # a test that pins it wants. `main()` supplies one.
        self._endpoint_verifier = endpoint_verifier
        self._endpoints_checked_at = time.monotonic()
        self._context_authorizer = context_authorizer
        self._lock = threading.RLock()
        self._inference_slots = threading.BoundedSemaphore(2)
        self._enabled, self._policy_epoch = self._load_state()
        self._credential_health = _check_credential(api_key)
        self._credential_checked_at = time.monotonic()
        self._latest_receipt: dict[str, Any] | None = None
        self._idempotent_results: OrderedDict[
            str,
            tuple[str, dict[str, Any]],
        ] = OrderedDict()
        self._active_tokens: dict[str, CancellationToken] = {}
        self._init_command_ledger()

    def handle(self, request: object) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise BrokerCommandError("INVALID_REQUEST", "request must be an object")
        if request.get("protocol_version") != PROTOCOL_VERSION:
            raise BrokerCommandError(
                "PROTOCOL_VERSION_MISMATCH",
                "unsupported router protocol version",
            )
        method = request.get("method")
        params = request.get("params")
        binding = request.get("binding", {})
        if not isinstance(method, str) or method not in tool_names() | {"status", "set_enabled"}:
            raise BrokerCommandError("METHOD_NOT_FOUND", "unknown router method")
        if not isinstance(params, dict) or not isinstance(binding, dict):
            raise BrokerCommandError("INVALID_REQUEST", "invalid params or binding")
        if method in tool_names():
            _validate_binding(binding)

        if method in {"status", "routing_status"}:
            _require_contract(params)
            return self.status()
        if method == "set_enabled":
            # Operator-only: the desktop client sends no native binding. A
            # request that carries one did not come from Settings, and the
            # preview switch is not a model-reachable control.
            if binding:
                raise BrokerCommandError(
                    "INVALID_BINDING",
                    "set_enabled is an operator control and takes no client binding",
                )
            _require_contract(params, allowed={"contract_version", "enabled"})
            if type(params.get("enabled")) is not bool:
                raise BrokerCommandError("INVALID_REQUEST", "enabled must be boolean")
            return self.set_enabled(params["enabled"])
        if method == "search_specialty_tools":
            return self.search_specialties(params)
        if method == "get_specialty_tool":
            return self.get_specialty(params)

        try:
            task = normalize_task_spec(params)
        except TaskSpecError as exc:
            raise BrokerCommandError(exc.code, str(exc)) from exc
        if method == "explain_route":
            return self.explain(task, binding=binding)
        if method == "delegate_task":
            return self.delegate(task, binding=binding)
        raise BrokerCommandError(
            "NOT_IMPLEMENTED",
            "this specialty contract is not active in the canary",
        )

    def status(self, *, refresh_health: bool = True) -> dict[str, Any]:
        if refresh_health:
            self._refresh_health_if_stale()
            self._refresh_endpoints_if_stale()
        health = dict(self._credential_health)
        credential_ready = health.get("status") == "healthy"
        enabled = self._enabled
        canary_promoted = CANARY_PROFILE in self._promoted_profiles
        endpoint_verified = CANARY_PROFILE in self._verified_endpoints
        trusted_context_ready = self._context_authorizer is not None
        canary_eligible = (
            credential_ready
            and canary_promoted
            and endpoint_verified
            and trusted_context_ready
        )
        canary_reasons = []
        if not credential_ready:
            canary_reasons.append("CREDENTIAL_UNHEALTHY")
        if not trusted_context_ready:
            canary_reasons.append("TRUSTED_CONTEXT_COMPILER_REQUIRED")
        if not canary_promoted:
            canary_reasons.append("PAIRED_QUALITY_EVAL_REQUIRED")
        if not endpoint_verified:
            canary_reasons.append("ENDPOINT_VARIANT_PIN_REQUIRED")
        profile_rows = [
            {
                "profile_id": CANARY_PROFILE.profile_id,
                "profile_version": CANARY_PROFILE.version,
                "role": "bounded_text_evaluation",
                "stage": (
                    "role_approved"
                    if (
                        canary_promoted
                        and endpoint_verified
                        and trusted_context_ready
                    )
                    else "manual_evaluation"
                ),
                "eligible": canary_eligible,
                "dispatchable": bool(canary_eligible and enabled),
                "reason_codes": canary_reasons,
            },
            *_quarantined_rows(health.get("account_tier")),
        ]
        automatic_dispatch = any(row["dispatchable"] for row in profile_rows)
        return {
            "contract_version": CONTRACT_VERSION,
            "service_version": __version__,
            "enabled": enabled,
            "policy_epoch": self._policy_epoch,
            "control_policy_id": _CONTROL_POLICY_ID,
            "automatic_dispatch": automatic_dispatch,
            "execution_mode": (
                "active" if automatic_dispatch else "shadow_evaluation"
            ),
            "credential_status": health.get("status", "unknown"),
            "account_tier": health.get("account_tier", "unknown"),
            "profiles": profile_rows,
            "latest_receipt": dict(self._latest_receipt) if self._latest_receipt else None,
        }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._lock:
            transition_to_enabled = enabled and not self._enabled
        if transition_to_enabled:
            self._refresh_health_if_stale(force=True)
            self._refresh_endpoints_if_stale(force=True)
        if enabled and self._credential_health.get("status") != "healthy":
            raise BrokerCommandError(
                "CREDENTIAL_UNHEALTHY",
                "OpenRouter credential health must pass before routing can be enabled",
            )
        with self._lock:
            if self._enabled != enabled:
                self._enabled = enabled
                self._policy_epoch += 1
                self._save_state()
            active_tokens = tuple(self._active_tokens.values()) if not enabled else ()
        for token in active_tokens:
            token.cancel()
        return self.status(refresh_health=False)

    def prepare_shutdown(self) -> None:
        with self._lock:
            active_tokens = tuple(self._active_tokens.values())
        for token in active_tokens:
            token.cancel()

    def _decision_snapshot(self) -> tuple[bool, bool, bool]:
        """One locked read of every mutable input to ``decide_route``.

        ``_credential_health`` is replaced wholesale by the health refresher, so
        reading it separately from ``_enabled`` can mix a pre-refresh enable bit
        with a post-refresh credential status and yield a preview that matches
        neither the before nor the after state.

        ``_verified_endpoints`` is now replaced the same way, by
        ``_refresh_endpoints_if_stale``, so it is read here rather than at the
        call site — the tear this method exists to prevent became reachable the
        moment endpoint verification stopped being frozen at construction. The
        earlier docstring claimed it was already read here; it was not, and this
        is the change that made the claim worth making true.

        ``_promoted_profiles`` remains genuinely immutable: it is loaded once
        from a root-owned record the service cannot write, and nothing rebinds
        it. It stays outside the snapshot until that changes.
        """
        with self._lock:
            return (
                self._enabled,
                self._credential_health.get("status") == "healthy",
                CANARY_PROFILE in self._verified_endpoints,
            )

    def explain(self, task: dict[str, Any], *, binding: dict[str, str]) -> dict[str, Any]:
        """Evaluate a task without dispatching it.

        Must use exactly the inputs ``delegate`` uses. It previously hardcoded
        ``trusted_context=False`` while ``delegate`` passed the real authorizer
        result — harmless only because no authorizer is wired, and guaranteed to
        diverge the moment one is: the single tool whose purpose is to predict
        routing would have reported a hold for tasks that would actually
        dispatch. Reads state under the lock and refreshes credential health for
        the same reason ``status`` does — a stale answer here is a wrong answer.
        """
        self._refresh_health_if_stale()
        self._refresh_endpoints_if_stale()
        enabled, profile_ready, endpoint_verified = self._decision_snapshot()
        decision = decide_route(
            task,
            enabled=enabled,
            profile_ready=profile_ready,
            profile_promoted=CANARY_PROFILE in self._promoted_profiles,
            endpoint_verified=endpoint_verified,
            # Outside the lock on purpose: an external callback that could block
            # or re-enter the broker must not be called while holding it.
            trusted_context=self._authorize_context(binding, task),
        )
        return {
            "contract_version": CONTRACT_VERSION,
            "accepted": True,
            "status": "explained",
            "route": decision.to_dict(),
            "estimated_budget": {
                "currency": "USD",
                "maximum": 0,
                "account_tier": self._credential_health.get("account_tier", "unknown"),
            },
        }

    def delegate(
        self,
        task: dict[str, Any],
        *,
        binding: dict[str, str],
    ) -> dict[str, Any]:
        with self._lock:
            enabled = self._enabled
            policy_epoch = self._policy_epoch
            profile_ready = self._credential_health.get("status") == "healthy"
            endpoint_verified = CANARY_PROFILE in self._verified_endpoints
        decision = decide_route(
            task,
            enabled=enabled,
            profile_ready=profile_ready,
            profile_promoted=CANARY_PROFILE in self._promoted_profiles,
            endpoint_verified=endpoint_verified,
            # Outside the lock on purpose: an external callback that could block
            # or re-enter the broker must not be called while holding it.
            trusted_context=self._authorize_context(binding, task),
        )
        if decision.disposition != "delegate" or decision.profile is None:
            raise BrokerCommandError(
                decision.reason_codes[0],
                "task retained on the primary model",
                data={"route": decision.to_dict()},
            )
        command_id = _command_id(binding)
        task_digest = _digest(task)
        with self._lock:
            cached = self._idempotent_results.get(command_id)
            if cached is not None:
                cached_digest, cached_response = cached
                if cached_digest != task_digest:
                    raise BrokerCommandError(
                        "IDEMPOTENCY_CONFLICT",
                        "native call identity was reused with different task input",
                    )
                self._idempotent_results.move_to_end(command_id)
                return json.loads(json.dumps(cached_response))
        if not self._inference_slots.acquire(blocking=False):
            raise BrokerCommandError(
                "ROUTER_BUSY",
                "the bounded evaluation concurrency limit is in use",
            )

        request_id = f"or_{command_id.split(':', 1)[1][:28]}"
        cancellation = CancellationToken()
        command_claimed = False
        try:
            with self._lock:
                if not self._enabled or self._policy_epoch != policy_epoch:
                    raise BrokerCommandError(
                        "POLICY_CHANGED",
                        "routing policy changed before dispatch",
                    )
            self._claim_command(
                command_id,
                task_digest=task_digest,
                request_id=request_id,
                policy_epoch=policy_epoch,
            )
            command_claimed = True

            system, user = compile_messages(task)
            max_output = 1024 if task["quality_tier"] == "quick" else 2048
            request = InferenceRequest(
                request_id=request_id,
                profile=decision.profile,
                messages=(
                    PromptMessage(MessageRole.SYSTEM, system),
                    PromptMessage(MessageRole.USER, user),
                ),
                max_output_tokens=max_output,
                timeout_seconds=45.0,
            )
            with self._lock:
                if not self._enabled or self._policy_epoch != policy_epoch:
                    self._update_command(
                        command_id,
                        status="failed",
                        error_code="POLICY_CHANGED",
                    )
                    raise BrokerCommandError(
                        "POLICY_CHANGED",
                        "routing policy changed before dispatch",
                    )
                self._active_tokens[request_id] = cancellation

            started = time.monotonic()
            try:
                result = self._gateway.infer(
                    request,
                    api_key=self._api_key,
                    cancellation=cancellation,
                )
                checks = validate_result_content(task, result.content)
            except TaskSpecError as exc:
                self._record_failure(
                    command_id,
                    request_id,
                    task_digest,
                    exc.code,
                )
                raise BrokerCommandError(exc.code, str(exc)) from exc
            except GatewayError as exc:
                code = f"UPSTREAM_{exc.kind.value.upper()}"
                self._record_failure(
                    command_id,
                    request_id,
                    task_digest,
                    code,
                    generation_id=exc.generation_id,
                )
                raise BrokerCommandError(
                    code,
                    "OpenRouter evaluation inference failed safely",
                    data={
                        "retryable": exc.retryable,
                        "attempts": exc.attempts,
                        "partial": exc.partial,
                    },
                ) from exc
            finally:
                with self._lock:
                    self._active_tokens.pop(request_id, None)

            if not result.route.endpoint_confirmed:
                self._record_failure(
                    command_id,
                    request_id,
                    task_digest,
                    "ENDPOINT_VARIANT_UNCONFIRMED",
                    generation_id=result.route.generation_id,
                )
                raise BrokerCommandError(
                    "ENDPOINT_VARIANT_UNCONFIRMED",
                    "serving endpoint variant was not independently confirmed",
                )

            receipt = {
                "schema_version": 1,
                "command_id": command_id,
                "request_id": request_id,
                "binding_digest": _digest(binding),
                "task_digest": task_digest,
                "result_digest": _digest(result.content),
                "profile_id": result.route.profile.profile_id,
                "profile_version": result.route.profile.version,
                "requested_model": result.route.requested_model,
                "actual_model": result.route.actual_model,
                "actual_endpoint_model": result.route.actual_endpoint_model,
                "endpoint": result.route.endpoint.endpoint_id,
                "actual_provider": result.route.actual_provider,
                "endpoint_confirmed": result.route.endpoint_confirmed,
                "generation_id": result.route.generation_id,
                "gateway_attempts": result.route.gateway_attempts,
                "request_digest": result.route.request_digest,
                "finish_reason": result.finish_reason,
                "native_finish_reason": result.native_finish_reason,
                "started_at": result.route.started_at,
                "completed_at": result.route.completed_at,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "policy_epoch": policy_epoch,
                "usage": _jsonable(dataclasses.asdict(result.usage)),
                "status": "completed",
            }
            receipt_ref = self._append_receipt(receipt)
            response = {
                "contract_version": CONTRACT_VERSION,
                "delegation_id": f"dlg_{command_id.split(':', 1)[1][:24]}",
                "status": "completed",
                "accepted": True,
                "route": decision.to_dict(),
                "result": {
                    "summary": result.content,
                    "evidence_refs": [],
                    "artifact_refs": [],
                    "checks": checks,
                    "warnings": [
                        "Evaluation output; the primary model must verify it."
                    ],
                    "usage": _jsonable(dataclasses.asdict(result.usage)),
                },
                "receipt_ref": receipt_ref,
            }
            self._update_command(
                command_id,
                status="completed",
                generation_id=result.route.generation_id,
                receipt_ref=receipt_ref,
            )
            with self._lock:
                self._idempotent_results[command_id] = (
                    task_digest,
                    response,
                )
                self._idempotent_results.move_to_end(command_id)
                while len(self._idempotent_results) > 256:
                    self._idempotent_results.popitem(last=False)
            return json.loads(json.dumps(response))
        except BrokerCommandError:
            raise
        except Exception:
            if command_claimed:
                self._update_command(
                    command_id,
                    status="unknown",
                    error_code="LOCAL_COMPLETION_UNKNOWN",
                )
            raise
        finally:
            self._inference_slots.release()

    def _authorize_context(
        self,
        binding: dict[str, str],
        task: dict[str, Any],
    ) -> bool:
        if self._context_authorizer is None:
            return False
        try:
            return self._context_authorizer(
                dict(binding),
                json.loads(json.dumps(task)),
            ) is True
        except Exception:
            return False

    def search_specialties(self, params: dict[str, Any]) -> dict[str, Any]:
        _require_contract(params, allowed={"contract_version", "query"})
        query = params.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise BrokerCommandError("INVALID_REQUEST", "query must be 1-500 characters")
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "completed",
            "registry_version": specialty_registry.REGISTRY_VERSION,
            "matches": specialty_registry.search(query, limit=5),
        }

    def get_specialty(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return one tool's full contract, including how its output is checked.

        Discovery is useless without this: a caller cannot honor an output
        contract it has only seen summarized. Returning the schemas costs
        nothing — they contain no model id, endpoint, or credential — and every
        tool here is non-dispatchable regardless, so this reveals a shape, not
        a capability.
        """
        _require_contract(params, allowed={"contract_version", "tool_id"})
        tool_id = params.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id.strip() or len(tool_id) > 100:
            raise BrokerCommandError("INVALID_REQUEST", "tool_id must be 1-100 characters")
        contract = specialty_registry.get(tool_id)
        if contract is None:
            raise BrokerCommandError("NOT_FOUND", "unknown specialty tool")
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "completed",
            "tool": contract,
        }

    def _record_failure(
        self,
        command_id: str,
        request_id: str,
        task_digest: str,
        code: str,
        *,
        generation_id: str | None = None,
    ) -> None:
        self._update_command(
            command_id,
            status="failed",
            generation_id=generation_id,
            error_code=code,
        )
        self._append_receipt(
            {
                "schema_version": 1,
                "command_id": command_id,
                "request_id": request_id,
                "task_digest": task_digest,
                "generation_id": generation_id,
                "status": "failed",
                "error_code": code,
            }
        )

    def _init_command_ledger(self) -> None:
        with self._ledger_connection() as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS commands (
                        command_id TEXT PRIMARY KEY,
                        task_digest TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        policy_epoch INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        generation_id TEXT,
                        receipt_ref TEXT,
                        error_code TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute("PRAGMA user_version = 1")
        os.chmod(self._ledger_path, 0o640)

    @contextmanager
    def _ledger_connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._ledger_path, timeout=2.0)
        try:
            connection.execute("PRAGMA busy_timeout = 2000")
            connection.execute("PRAGMA synchronous = FULL")
            yield connection
        finally:
            connection.close()

    def _claim_command(
        self,
        command_id: str,
        *,
        task_digest: str,
        request_id: str,
        policy_epoch: int,
    ) -> None:
        now = time.time()
        try:
            with self._ledger_connection() as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO commands (
                            command_id, task_digest, request_id, policy_epoch,
                            status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'accepted', ?, ?)
                        """,
                        (
                            command_id,
                            task_digest,
                            request_id,
                            policy_epoch,
                            now,
                            now,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            with self._ledger_connection() as connection:
                row = connection.execute(
                    """
                    SELECT task_digest, status, receipt_ref, error_code
                    FROM commands WHERE command_id = ?
                    """,
                    (command_id,),
                ).fetchone()
            if row is None:
                raise
            prior_digest, status, receipt_ref, error_code = row
            if prior_digest != task_digest:
                raise BrokerCommandError(
                    "IDEMPOTENCY_CONFLICT",
                    "native call identity was reused with different task input",
                ) from exc
            raise BrokerCommandError(
                "IDEMPOTENT_REPLAY_UNAVAILABLE",
                "the external call will not be replayed after its durable acceptance",
                data={
                    "prior_status": status,
                    "receipt_ref": receipt_ref,
                    "error_code": error_code,
                },
            ) from exc

    def _update_command(
        self,
        command_id: str,
        *,
        status: str,
        generation_id: str | None = None,
        receipt_ref: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if status not in {"accepted", "completed", "failed", "unknown"}:
            raise ValueError("invalid command status")
        with self._ledger_connection() as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE commands
                    SET status = ?, generation_id = COALESCE(?, generation_id),
                        receipt_ref = COALESCE(?, receipt_ref),
                        error_code = ?, updated_at = ?
                    WHERE command_id = ?
                    """,
                    (
                        status,
                        generation_id,
                        receipt_ref,
                        error_code,
                        time.time(),
                        command_id,
                    ),
                )
        if cursor.rowcount != 1:
            raise RuntimeError("command ledger update did not match one row")

    def _append_receipt(self, receipt: dict[str, Any]) -> str:
        canonical = json.dumps(
            _jsonable(receipt),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        receipt_ref = f"receipt:sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
        with self._lock:
            descriptor = os.open(
                self._receipt_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o640,
            )
            try:
                pending = memoryview((canonical + "\n").encode("utf-8"))
                while pending:
                    written = os.write(descriptor, pending)
                    if written < 1:
                        raise OSError("receipt write made no progress")
                    pending = pending[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._latest_receipt = {
                "receipt_ref": receipt_ref,
                "status": receipt.get("status"),
                "profile_id": receipt.get("profile_id"),
                "completed_at": receipt.get("completed_at"),
                "latency_ms": receipt.get("latency_ms"),
            }
        return receipt_ref

    def _load_state(self) -> tuple[bool, int]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, 0
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != 2
            or raw.get("control_policy_id") != _CONTROL_POLICY_ID
            or type(raw.get("enabled")) is not bool
            or type(raw.get("policy_epoch")) is not int
            or raw["policy_epoch"] < 0
        ):
            return False, 0
        return raw["enabled"], raw["policy_epoch"]

    def _save_state(self) -> None:
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "control_policy_id": _CONTROL_POLICY_ID,
                    "enabled": self._enabled,
                    "policy_epoch": self._policy_epoch,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o640)
        tmp.replace(self._state_path)

    def _refresh_health_if_stale(self, *, force: bool = False) -> None:
        age = time.monotonic() - self._credential_checked_at
        minimum_age = 5 if force else 300
        if age < minimum_age:
            return
        health = _check_credential(self._api_key)
        active_tokens: tuple[CancellationToken, ...] = ()
        with self._lock:
            self._credential_health = health
            self._credential_checked_at = time.monotonic()
            if health.get("status") == "invalid" and self._enabled:
                self._enabled = False
                self._policy_epoch += 1
                self._save_state()
                active_tokens = tuple(self._active_tokens.values())
        for token in active_tokens:
            token.cancel()

    def _refresh_endpoints_if_stale(self, *, force: bool = False) -> None:
        """Re-confirm pinned endpoint variants against the live catalog.

        This used to run exactly once, in ``main()``. Two things made that
        wrong. A pin can die under a running service — OpenRouter retires
        model slugs, and ``free_canary.ling3_novita`` did exactly that — and
        because ``verify_endpoint`` is deliberately fail-closed, a catalog that
        was merely unreachable at boot shut ``ENDPOINT_VARIANT_PIN_REQUIRED``
        until somebody restarted the unit. Neither state was observable from
        ``status()``: both render as the same reason code a held profile shows.

        Fail-closed is preserved, not softened. A refresh that cannot confirm
        drops the profile exactly as construction would have, so verification
        stays positive evidence. What changes is that it recovers on its own.

        Network I/O outside the lock, for the reason ``_refresh_health_if_stale``
        does it: the verifier reaches OpenRouter and must not block a status
        read. Two threads may refresh concurrently; the later write wins and
        both wrote the same kind of evidence.
        """
        if self._endpoint_verifier is None:
            return
        age = time.monotonic() - self._endpoints_checked_at
        if age < (5 if force else 300):
            return
        try:
            verified = frozenset(self._endpoint_verifier())
        except Exception:  # noqa: BLE001 — unverifiable is unverified
            verified = frozenset()
        with self._lock:
            self._verified_endpoints = verified
            self._endpoints_checked_at = time.monotonic()


class _RouterRequestHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5.0)

    def handle(self) -> None:
        request_id: object = None
        try:
            raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        except (socket.timeout, TimeoutError, OSError):
            response = _error_response(
                request_id,
                "REQUEST_TIMEOUT",
                "router request did not arrive within its deadline",
            )
        else:
            if len(raw) > _MAX_REQUEST_BYTES:
                response = _error_response(
                    request_id,
                    "REQUEST_TOO_LARGE",
                    "router request exceeded size limit",
                )
            else:
                try:
                    request = json.loads(raw)
                    request_id = (
                        request.get("id") if isinstance(request, dict) else None
                    )
                    result = self.server.broker.handle(  # type: ignore[attr-defined]
                        request
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response = _error_response(
                        request_id,
                        "INVALID_JSON",
                        "router request was not valid JSON",
                    )
                except BrokerCommandError as exc:
                    response = _error_response(
                        request_id,
                        exc.code,
                        str(exc),
                        data=exc.data,
                    )
                except Exception:
                    response = _error_response(
                        request_id,
                        "INTERNAL_ERROR",
                        "router command failed unexpectedly",
                    )
                else:
                    response = {
                        "protocol_version": PROTOCOL_VERSION,
                        "id": request_id,
                        "result": result,
                    }
        try:
            self.wfile.write(
                json.dumps(
                    response,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                + b"\n"
            )
        except (BrokenPipeError, socket.timeout, OSError):
            return


class RouterUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, path: str, broker: RouterBroker) -> None:
        self.broker = broker
        self._connection_slots = threading.BoundedSemaphore(16)
        super().__init__(path, _RouterRequestHandler)

    def process_request(self, request, client_address) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


def _error_response(
    request_id: object,
    code: str,
    message: str,
    *,
    data: Any = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {
        "protocol_version": PROTOCOL_VERSION,
        "id": request_id,
        "error": error,
    }


def _quarantined_rows(account_tier: object) -> list[dict[str, Any]]:
    """Status rows for every profile the router cannot route to.

    Derived from the profile table rather than hand-copied. The literal rows
    this replaces had drifted twice: they duplicated model identity that lives
    in ``profiles.py`` with nothing enforcing agreement, and they hardcoded
    ``PAID_ACCOUNT_REQUIRED`` — so the status contract reported
    ``account_tier: "paid"`` and "a paid account is required" in the same
    response, which is exactly the kind of self-contradiction that costs an
    operator an hour.

    Only ``CANARY_PROFILE`` is reachable by ``decide_route``; everything else is
    quarantined by construction, and says so.
    """
    rows: list[dict[str, Any]] = []
    for ref, profile in sorted(
        PROFILES.items(), key=lambda item: (item[0].profile_id, item[0].version)
    ):
        if ref == CANARY_PROFILE:
            continue
        reasons = ["PAIRED_QUALITY_EVAL_REQUIRED"]
        # Only claim a paid account is needed when the profile actually costs
        # money and the credential is not already on a paid tier.
        if profile.max_prompt_price_per_million > 0 and account_tier != "paid":
            reasons.append("PAID_ACCOUNT_REQUIRED")
        rows.append({
            "profile_id": ref.profile_id,
            "profile_version": ref.version,
            "role": ref.profile_id.split(".")[0],
            "stage": "quarantined",
            "eligible": False,
            "dispatchable": False,
            "reason_codes": reasons,
        })
    return rows


def _validate_binding(binding: dict[str, Any]) -> None:
    kind = binding.get("kind")
    if kind == "codex":
        required = ("thread_id", "turn_id", "call_id")
    elif kind == "claude":
        required = ("client_binding", "call_id")
    else:
        raise BrokerCommandError("INVALID_BINDING", "unknown native client binding")
    if set(binding) != {"kind", *required} or any(
        not isinstance(binding.get(key), str)
        or not binding[key]
        or len(binding[key]) > 256
        for key in required
    ):
        raise BrokerCommandError("INVALID_BINDING", "invalid native client binding")


def _require_contract(
    params: dict[str, Any],
    *,
    allowed: set[str] | None = None,
) -> None:
    fields = allowed or {"contract_version"}
    if set(params) != fields or params.get("contract_version") != CONTRACT_VERSION:
        raise BrokerCommandError("INVALID_REQUEST", "invalid contract envelope")


def _command_id(binding: dict[str, str]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f"cmd:{digest}"


def _digest(value: object) -> str:
    rendered = (
        value
        if isinstance(value, str)
        else json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return f"sha256:{hashlib.sha256(rendered.encode()).hexdigest()}"


def _jsonable(value: object) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _check_credential(api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        _KEY_STATUS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with _HEALTH_OPENER.open(request, timeout=3) as response:
            raw = response.read(64 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        return {
            "status": "invalid" if exc.code in {401, 403} else "unknown",
            "account_tier": "unknown",
        }
    except (OSError, urllib.error.URLError):
        return {"status": "unknown", "account_tier": "unknown"}
    if len(raw) > 64 * 1024:
        return {"status": "unknown", "account_tier": "unknown"}
    try:
        data = json.loads(raw).get("data", {})
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return {"status": "unknown", "account_tier": "unknown"}
    if not isinstance(data, dict) or type(data.get("is_free_tier")) is not bool:
        return {"status": "unknown", "account_tier": "unknown"}
    return {
        "status": "healthy",
        "account_tier": "free" if data.get("is_free_tier") is True else "paid",
    }


def _read_credential(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise RuntimeError("credential file permissions are unsafe")
    value = path.read_text(encoding="utf-8").strip()
    if (
        len(value) < 16
        or any(character in value for character in ("\r", "\n", "\x00"))
    ):
        raise RuntimeError("credential file is invalid")
    return value


def _prepare_socket_directory(socket_path: Path, socket_group: str) -> int:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    group_id = grp.getgrnam(socket_group).gr_gid
    os.chown(socket_path.parent, -1, group_id)
    os.chmod(socket_path.parent, 0o750)
    return group_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helios Router broker")
    parser.add_argument("--credential", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--socket-group", required=True)
    parser.add_argument(
        "--promotions",
        default=str(router_promotion.DEFAULT_PROMOTIONS_PATH),
        help=(
            "root-owned record of profiles with accepted quality evidence; "
            "absent means nothing dispatches"
        ),
    )
    args = parser.parse_args(argv)

    socket_path = Path(args.socket)
    group_id = _prepare_socket_directory(socket_path, args.socket_group)
    if socket_path.exists() or socket_path.is_socket():
        socket_path.unlink()
    # The three inputs that were previously unreachable. Each is satisfied by
    # evidence the service does not author: a root-owned promotion record it
    # cannot write, OpenRouter's own endpoint catalog, and a context authorizer
    # that re-checks the policy's own invariants. With no promotion record
    # present this is bit-identical to the previous construction — nothing
    # dispatches, which is the property to preserve.
    promoted = router_promotion.load_promotions(Path(args.promotions))
    verified = router_promotion.verified_endpoints()
    broker = RouterBroker(
        api_key=_read_credential(Path(args.credential)),
        state_dir=Path(args.state_dir),
        promoted_profiles=promoted,
        verified_endpoints=verified,
        # Re-checked on a staleness window rather than only here: a pinned model
        # can be retired under a running service, and a catalog that is merely
        # unreachable at boot must not shut the gate until the next restart.
        endpoint_verifier=router_promotion.verified_endpoints,
        context_authorizer=router_promotion.authorize_inline_context,
    )
    server = RouterUnixServer(str(socket_path), broker)
    os.chown(socket_path, -1, group_id)
    os.chmod(socket_path, 0o660)

    stopping = threading.Event()

    def stop(_signum, _frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        broker.prepare_shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
