"""Versioned registry of Helios specialty tools.

Five tools, not twelve. The selection rule is a single question: **does a
non-LLM verifier exist for this output?** A compiler, a schema check, a span
that either resolves or does not. Where the answer is yes, a cheap model plus a
deterministic check is defensible on published evidence and cheap to falsify.
Where it is no — open-ended critique, architecture, security judgment, the
final answer — no amount of routing machinery makes delegation safe, so those
are not in here and should not be added.

What each contract inherits from that rule:

* **Evidence before answer.** Every schema orders the evidence fields ahead of
  the conclusion, and ``required`` is ordered to match. Constrained decoding
  that commits to an answer before its justification measurably degrades
  reasoning; the effect has been traced to SDKs sorting schema keys
  alphabetically and putting ``answer`` ahead of ``reasoning``. Preserve this
  order if you edit a schema, and do not let a serializer re-sort it.
* **Structured locations, not prose.** ``localize_code`` returns paths and line
  ranges. A weaker model's free-form summary is the documented failure mode —
  it misleads the caller precisely because it cannot be graded.
* **Abstention is a result.** ``classify_closed`` can decline. A classifier
  that guesses confidently is worse than one that hands back.

Status is deliberately conservative. Every tool ships ``shadow`` — discoverable,
contract-complete, and *not* dispatchable. Promotion to eligible requires a
fresh paired evaluation against the primary model, which does not exist yet; the
broker's dispatch gates are unchanged by this module and remain closed.

GTK-free.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from helios.backend import specialty_verifiers as verifiers

__all__ = [
    "REGISTRY_VERSION",
    "SpecialtyTool",
    "get",
    "search",
    "summaries",
    "tool_ids",
]

REGISTRY_VERSION = 1

# Promotion states. Only SHADOW is reachable today: nothing here is
# dispatchable until a paired evaluation exists, and the broker enforces that
# independently — this constant is a label, never an authorization.
_SHADOW = "shadow"


@dataclass(frozen=True, slots=True)
class SpecialtyTool:
    tool_id: str
    version: int
    task_class: str
    status: str
    summary: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    # Name of the verifier in ``specialty_verifiers`` and the host-supplied
    # evidence it needs. A tool without both is not admissible here.
    verifier: str
    verifier_inputs: tuple[str, ...]
    fallback: str
    parity_evidence: str
    reason_codes: tuple[str, ...] = field(default=("SPECIALTY_EVALUATION_REQUIRED",))


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


_SPAN = {
    "type": "array",
    "items": {"type": "integer", "minimum": 1},
    "minItems": 2,
    "maxItems": 2,
    "description": (
        "Inclusive 1-based [start, end] line range. Lines are counted by "
        "splitting the source on \\n only, so the host, the worker, and the "
        "verifier agree on a single line model."
    ),
}


_TOOLS: tuple[SpecialtyTool, ...] = (
    SpecialtyTool(
        tool_id="digest_source",
        version=1,
        task_class="summarize",
        status=_SHADOW,
        summary=(
            "Digest supplied text into claims, each citing the exact lines it "
            "came from. For reading a long file or log down to what matters."
        ),
        input_schema=_obj(
            {
                "source": {"type": "string", "maxLength": 400_000,
                           "description": "The full text to digest. Inline only."},
                "focus": {"type": "string", "maxLength": 500,
                          "description": "What the caller cares about."},
            },
            ["source"],
        ),
        output_schema=_obj(
            {
                "claims": {
                    "type": "array",
                    "maxItems": 100,
                    "items": _obj(
                        {
                            # Evidence first: the span and the quote precede
                            # the assertion they support.
                            "lines": _SPAN,
                            "quote": {"type": "string", "maxLength": 2_000,
                                      "description": "Exact substring of the cited lines."},
                            "claim": {"type": "string", "maxLength": 1_000},
                        },
                        ["lines", "quote", "claim"],
                    ),
                }
            },
            ["claims"],
        ),
        verifier="verify_digest",
        verifier_inputs=("source",),
        fallback="primary",
        parity_evidence=(
            "Strongest published parity case: small open-weight models match "
            "or beat frontier models on source-grounded faithfulness. Measures "
            "unsupported-claim rate only — not salience — so this tool digests "
            "supplied text and never decides what matters overall."
        ),
    ),
    SpecialtyTool(
        tool_id="extract_structured",
        version=1,
        task_class="extract",
        status=_SHADOW,
        summary=(
            "Fill a caller-supplied JSON schema from supplied text. For pulling "
            "a fixed set of fields out of a document."
        ),
        input_schema=_obj(
            {
                "source": {"type": "string", "maxLength": 400_000},
                "schema": {"type": "object",
                           "description": "JSON Schema for the object to return."},
            },
            ["source", "schema"],
        ),
        output_schema={
            "type": "object",
            "description": "Conforms to the caller-supplied schema.",
        },
        verifier="verify_extraction",
        verifier_inputs=("schema",),
        fallback="primary",
        parity_evidence=(
            "A mid-size open-weight model matches a frontier model on fixed-schema extraction at modest "
            "schema size. Graded on exact leaf values, never JSON validity — "
            "validity is at ceiling for every usable model and predicts "
            "nothing. Large or deeply nested schemas defeat frontier models "
            "too: shard the schema rather than escalating."
        ),
    ),
    SpecialtyTool(
        tool_id="classify_closed",
        version=1,
        task_class="classify",
        status=_SHADOW,
        summary=(
            "Assign labels from a fixed taxonomy, with evidence spans and an "
            "explicit abstention when the item does not fit."
        ),
        input_schema=_obj(
            {
                "source": {"type": "string", "maxLength": 200_000},
                "taxonomy": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 200,
                    "items": _obj(
                        {
                            "label_id": {"type": "string", "maxLength": 100},
                            "description": {"type": "string", "maxLength": 500},
                        },
                        ["label_id"],
                    ),
                },
                "multi_label": {"type": "boolean"},
            },
            ["source", "taxonomy"],
        ),
        output_schema=_obj(
            {
                # Evidence and the abstention decision precede the labels.
                "labels": {
                    "type": "array",
                    "maxItems": 50,
                    "items": _obj(
                        {
                            "evidence_spans": {"type": "array", "minItems": 1,
                                               "maxItems": 20, "items": _SPAN},
                            "label_id": {"type": "string", "maxLength": 100},
                        },
                        ["evidence_spans", "label_id"],
                    ),
                },
                "reason": {"type": "string", "maxLength": 1_000},
                "abstain": {"type": "boolean"},
            },
            ["labels", "abstain"],
        ),
        verifier="verify_classification",
        verifier_inputs=("taxonomy", "source", "multi_label"),
        fallback="primary",
        parity_evidence=(
            "Fine-tuned small classifiers beat zero-shot frontier models on "
            "closed taxonomies when labels exist. That is evidence for task "
            "adaptation, not for small models generally — validate on a frozen "
            "labeled set with macro-F1 and per-class recall before promoting."
        ),
    ),
    SpecialtyTool(
        tool_id="localize_code",
        version=1,
        task_class="code_search",
        status=_SHADOW,
        summary=(
            "Given a question about a repository, return the files and line "
            "ranges worth reading. Locations only — never an edit or a verdict."
        ),
        input_schema=_obj(
            {
                "question": {"type": "string", "maxLength": 2_000},
                "inventory": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5_000,
                    "items": _obj(
                        {
                            "path": {"type": "string", "maxLength": 1_000},
                            "lines": {"type": "integer", "minimum": 0},
                        },
                        ["path", "lines"],
                    ),
                },
            },
            ["question", "inventory"],
        ),
        output_schema=_obj(
            {
                "explanation": {"type": "string", "maxLength": 2_000},
                "files": {
                    "type": "object",
                    "description": "Repo-relative path -> array of [start, end] ranges.",
                    "additionalProperties": {"type": "array", "items": _SPAN},
                },
            },
            ["explanation", "files"],
        ),
        verifier="verify_localization",
        verifier_inputs=("inventory",),
        fallback="primary",
        parity_evidence=(
            "Cheap retrieval with wide recall is well supported; cheap *causal* "
            "localization is not — distinguishing relevant from fault-inducing "
            "is where reasoning still pays. Runs on the primary model until a "
            "task-trained candidate clears a fresh evaluation. Never let this "
            "tool pick the file to edit; it proposes what to read."
        ),
    ),
    SpecialtyTool(
        tool_id="draft_metadata",
        version=1,
        task_class="draft",
        status=_SHADOW,
        summary=(
            "Draft a commit message, PR description, or docstring from a "
            "supplied diff or signature. A human reads it before it lands."
        ),
        input_schema=_obj(
            {
                "kind": {"type": "string",
                         "enum": ["commit_message", "pr_description", "docstring"]},
                "material": {"type": "string", "maxLength": 200_000,
                             "description": "The diff, or the function source."},
                "parameters": {"type": "array", "maxItems": 100,
                               "items": {"type": "string", "maxLength": 200},
                               "description": "Real signature parameters, for docstrings."},
            },
            ["kind", "material"],
        ),
        output_schema=_obj(
            {
                "text": {"type": "string", "maxLength": 4_000},
                "parameters": {"type": "array", "maxItems": 100,
                               "items": {"type": "string", "maxLength": 200},
                               "description": "Parameters the docstring documents."},
            },
            ["text"],
        ),
        verifier="verify_metadata",
        verifier_inputs=("kind", "known_parameters"),
        fallback="primary",
        parity_evidence=(
            "No controlled small-vs-frontier study exists for any of these. "
            "Delegated on pragmatics — the output is short, cheap to reject, "
            "and always human-reviewed — explicitly not on benchmark evidence. "
            "Do not cite parity for this tool."
        ),
    ),
)

_BY_ID: dict[str, SpecialtyTool] = {tool.tool_id: tool for tool in _TOOLS}

# Fail fast at import: a contract whose verifier does not exist is exactly the
# kind of hollow registry entry this module was written to replace.
for _tool in _TOOLS:
    if not callable(getattr(verifiers, _tool.verifier, None)):
        raise RuntimeError(
            f"specialty tool {_tool.tool_id!r} names a missing verifier "
            f"{_tool.verifier!r}"
        )


def tool_ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


def get(tool_id: str) -> dict[str, Any] | None:
    """Full contract for one tool, safe for caller mutation. None if unknown."""
    tool = _BY_ID.get(tool_id)
    if tool is None:
        return None
    return {
        "tool_id": tool.tool_id,
        "version": tool.version,
        "registry_version": REGISTRY_VERSION,
        "task_class": tool.task_class,
        "stage": tool.status,
        "eligible": False,
        "dispatchable": False,
        "reason_codes": list(tool.reason_codes),
        "summary": tool.summary,
        "input_schema": copy.deepcopy(tool.input_schema),
        "output_schema": copy.deepcopy(tool.output_schema),
        "verification": {
            "verifier": tool.verifier,
            "host_inputs": list(tool.verifier_inputs),
        },
        "fallback": tool.fallback,
        "parity_evidence": tool.parity_evidence,
    }


def summaries() -> list[dict[str, Any]]:
    """Discovery rows: identity and stage, never a model id or endpoint."""
    return [
        {
            "tool_id": tool.tool_id,
            "version": tool.version,
            "task_class": tool.task_class,
            "summary": tool.summary,
            "stage": tool.status,
            "eligible": False,
            "reason_codes": list(tool.reason_codes),
        }
        for tool in _TOOLS
    ]


def search(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Rank discovery rows against ``query`` by term overlap.

    Deliberately lexical. Ranking five fixed entries does not justify an
    embedding model, and a deterministic order is worth more here than a
    marginally better one — the caller is choosing a contract, not an answer.
    """
    terms = {t for t in query.lower().replace("_", " ").split() if t}
    rows = summaries()

    def score(row: dict[str, Any]) -> tuple[int, str]:
        text = f"{row['tool_id']} {row['task_class']} {row['summary']}"
        text = text.lower().replace("_", " ")
        return (-sum(term in text for term in terms), str(row["tool_id"]))

    return sorted(rows, key=score)[: max(0, limit)]
