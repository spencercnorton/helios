"""Stable, provider-neutral tool contracts for Helios task delegation.

Keeping one canonical copy prevents the two native drivers from teaching their
models subtly different routing semantics.  Model names, endpoint names,
credentials, and dollar budgets are deliberately absent: callers describe a
bounded job and Helios owns route selection.

Who actually consumes these schemas, because the answer is not symmetric and
this docstring used to imply it was:

* **Claude**, via the local ``helios-router`` MCP server (``mcp_tools``).
  Advertised only when the broker reports it can dispatch — see
  ``router_client.dispatch_available`` and ``cli_driver``.
* **The broker's** final validation layer.
* **Codex App Server ``dynamicTools`` (``codex_dynamic_tools``) — built, and
  deliberately not wired.** The *receiving* half in ``codex_app_driver`` is
  complete and tested: ``_run_dynamic_tool_request`` authorizes by thread, turn
  and generation, calls the broker, and maps a policy refusal to a valid tool
  outcome. What is missing is the advertisement, because ``build_thread_params``
  never sends ``dynamicTools``. That is the standard-Work containment contract
  (``agents.enabled=false``, restored dynamic tools rejected), not an oversight,
  so do not "finish" it by adding the parameter — turning it on is a policy
  decision about GPT delegation, and it should not happen while the broker
  cannot dispatch anyway.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


CONTRACT_VERSION = 1
CODEX_NAMESPACE = "helios"
MCP_SERVER_NAME = "helios-router"

TASK_KINDS = (
    "search",
    "extract",
    "summarize",
    "classify",
    "draft_docs",
    "draft_code",
    "draft_tests",
    "review",
    "research_synthesis",
    "other",
)

_DELIVERABLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "string", "minLength": 1, "maxLength": 2000},
        "format": {
            "type": "string",
            "enum": ["text", "markdown", "json", "patch", "artifact_refs"],
        },
        "output_schema": {"type": "object"},
    },
    "required": ["description", "format"],
}

_CONTEXT_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "work_event_range",
                "artifact",
                "workspace_path",
                "workspace_diff",
                "inline",
            ],
        },
        "ref": {"type": "string", "minLength": 1, "maxLength": 4096},
    },
    "required": ["kind", "ref"],
}

_CAPABILITY_HINTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "workspace": {
            "type": "string",
            "enum": ["none", "read", "isolated_write"],
        },
        "network": {"type": "boolean"},
        "tools": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "string",
                "pattern": "^[a-z0-9_.-]{1,64}$",
            },
        },
        "multimodal": {"type": "boolean"},
        "minimum_context_tokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2_000_000,
        },
    },
    "required": [
        "workspace",
        "network",
        "tools",
        "multimodal",
        "minimum_context_tokens",
    ],
}

DELEGATE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "contract_version": {"type": "integer", "const": CONTRACT_VERSION},
        "objective": {"type": "string", "minLength": 1, "maxLength": 4000},
        "task_kind": {"type": "string", "enum": list(TASK_KINDS)},
        "deliverable": _DELIVERABLE_SCHEMA,
        "context_refs": {
            "type": "array",
            "maxItems": 64,
            "items": _CONTEXT_REF_SCHEMA,
        },
        "constraints": {
            "type": "array",
            "maxItems": 32,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "capability_hints": _CAPABILITY_HINTS_SCHEMA,
        "quality_tier": {
            "type": "string",
            "enum": ["quick", "balanced", "deep", "critic"],
        },
        "independence": {
            "type": "string",
            "enum": [
                "same_family_ok",
                "different_model_family",
                "different_vendor",
            ],
        },
        # "async" is deliberately absent: there is no delegation lifecycle
        # (no get_delegation/cancel_delegation), so it was advertised and then
        # rejected on every call. Advertising a mode that always fails teaches
        # the model a contract the broker does not honour.
        "wait_mode": {"type": "string", "enum": ["bounded"]},
    },
    "required": [
        "contract_version",
        "objective",
        "task_kind",
        "deliverable",
        "quality_tier",
        "context_refs",
        "constraints",
        "capability_hints",
        "independence",
        "wait_mode",
    ],
}

EXPLAIN_ROUTE_SCHEMA: dict[str, Any] = copy.deepcopy(DELEGATE_TASK_SCHEMA)

ROUTING_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "contract_version": {"type": "integer", "const": CONTRACT_VERSION},
    },
    "required": ["contract_version"],
}

SEARCH_SPECIALTY_TOOLS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "contract_version": {"type": "integer", "const": CONTRACT_VERSION},
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["contract_version", "query"],
}

GET_SPECIALTY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "contract_version": {"type": "integer", "const": CONTRACT_VERSION},
        "tool_id": {"type": "string", "minLength": 1, "maxLength": 100},
    },
    "required": ["contract_version", "tool_id"],
}


_DEFERRED_TOOLS = frozenset({"search_specialty_tools", "get_specialty_tool"})


TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "delegate_task",
        "description": (
            "Offload one bounded, low-risk task through Helios's quality-first "
            "router. Use for separable extraction, classification, summaries, "
            "research subproblems, boilerplate, localization, or an independent "
            "review when the result can be checked. Keep ambiguous intent, "
            "secrets, security decisions, destructive actions, production "
            "authority, and the final user answer on the primary model. Helios "
            "may retain or reject the task when no evaluated route is eligible."
        ),
        "inputSchema": DELEGATE_TASK_SCHEMA,
    },
    {
        "name": "explain_route",
        "description": (
            "Evaluate the same task contract without dispatching it. Returns "
            "retain/delegate/human-gate reasoning and profile eligibility. Use "
            "when unsure whether a task should leave the primary model."
        ),
        "inputSchema": EXPLAIN_ROUTE_SCHEMA,
    },
    {
        "name": "routing_status",
        "description": (
            "Read Helios Router health, the user enable/disable state, eligible "
            "profile count, promotion stages, and the latest content-free "
            "receipt summary. This never reveals credentials or hidden prompts."
        ),
        "inputSchema": ROUTING_STATUS_SCHEMA,
    },
    {
        "name": "search_specialty_tools",
        "description": (
            "Find up to five task-specific Helios tools or planned specialties "
            "by capability. Results state whether each specialty is eligible, "
            "canary-only, quarantined, or unavailable without exposing raw "
            "model IDs."
        ),
        "inputSchema": SEARCH_SPECIALTY_TOOLS_SCHEMA,
    },
    {
        "name": "get_specialty_tool",
        "description": (
            "Read one specialty tool's full contract: input schema, output "
            "schema, how its output is verified without a model, what the host "
            "must supply for that verification, and the evidence (or absence "
            "of evidence) behind delegating it. Use after "
            "search_specialty_tools to see the exact shape before relying on "
            "one. Provider-neutral: no vendor or credential detail is exposed."
        ),
        "inputSchema": GET_SPECIALTY_TOOL_SCHEMA,
    },
)


def mcp_tools() -> list[dict[str, Any]]:
    """Return isolated MCP tool definitions safe for caller mutation."""

    return [copy.deepcopy(tool) for tool in TOOL_DEFINITIONS]


def codex_dynamic_tools() -> list[dict[str, Any]]:
    """Return the namespace-wrapped Codex App Server dynamic-tool contract."""

    return [
        {
            "type": "namespace",
            "name": CODEX_NAMESPACE,
            "description": (
                "Helios quality-first specialist routing. The primary GPT model "
                "retains user intent, authority, acceptance, and final synthesis."
            ),
            "tools": [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool["description"],
                    # Discovery tools are looked up on demand; the routing
                    # contracts stay resident so a turn can use them directly.
                    "deferLoading": tool["name"] in _DEFERRED_TOOLS,
                    "inputSchema": copy.deepcopy(tool["inputSchema"]),
                }
                for tool in TOOL_DEFINITIONS
            ],
        }
    ]


def tool_names() -> frozenset[str]:
    return frozenset(str(tool["name"]) for tool in TOOL_DEFINITIONS)


def router_mcp_launcher_path() -> Path:
    """Return the trusted repo launcher used by native Claude sessions."""

    return Path(__file__).resolve().parents[3] / "scripts" / "helios-router-mcp"


def claude_mcp_config(
    binding_id: str,
    *,
    socket_path: str,
    launcher_path: str | None = None,
) -> str:
    """Build a compact, credential-free ``--mcp-config`` JSON string."""

    if not isinstance(binding_id, str) or not binding_id:
        raise ValueError("binding_id must be a non-empty string")
    if not isinstance(socket_path, str) or not socket_path:
        raise ValueError("socket_path must be a non-empty string")
    launcher = launcher_path or str(router_mcp_launcher_path())
    return json.dumps(
        {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "type": "stdio",
                    "command": launcher,
                    "args": [
                        "--binding",
                        binding_id,
                        "--socket",
                        socket_path,
                    ],
                }
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
