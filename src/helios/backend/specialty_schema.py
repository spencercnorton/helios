"""Fail-closed JSON Schema subset checker for specialty-tool output.

Only ``extract_structured`` takes a caller-supplied schema, so this is the one
place a specialty check must interpret a contract it did not write. It
implements a deliberate subset — ``type``, ``enum``, ``required``,
``properties``, ``items`` — and **rejects anything it cannot interpret** rather
than ignoring it.

That rejection is the whole point. An empty finding list means "verified", and
the caller accepts the cheap worker's output on an empty list, so silently
skipping ``minLength`` or ``anyOf`` would certify a constraint that was never
checked. For a schema this cannot read, no non-LLM verifier exists — which is
exactly the condition under which the registry's admission rule says to stay on
the primary model.

Also owns ``Finding``: it is the shared result type for every specialty check,
and this is the lowest layer, so it lives here to keep the dependency pointing
one way.

Split out of ``specialty_verifiers`` because it is a self-contained concern
with its own vocabulary — and because a single module large enough to exceed
the review diff limit is a module nobody can review.

GTK-free.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Finding", "check_against_schema", "fail"]

_MAX_FINDINGS = 50


@dataclass(frozen=True, slots=True)
class Finding:
    """One reason the output cannot be accepted. ``path`` locates it."""

    code: str
    path: str
    detail: str


def _fail(findings: list[Finding], code: str, path: str, detail: str) -> None:
    if len(findings) < _MAX_FINDINGS:
        findings.append(Finding(code=code, path=path, detail=detail))


def fail(findings: list[Finding], code: str, path: str, detail: str) -> None:
    """Record a finding, bounded so a pathological output cannot grow forever."""
    if len(findings) < _MAX_FINDINGS:
        findings.append(Finding(code=code, path=path, detail=detail))


# Keywords ``_check_value`` can actually interpret. Anything else makes the
# schema uncheckable and is rejected rather than ignored — see verify_extraction.
_SUPPORTED_KEYWORDS = frozenset({
    "type", "enum", "required", "properties", "items",
    "description", "title", "default", "examples",
})


def check_against_schema(output: object, schema: object) -> list[Finding]:
    """Check ``output`` against a caller-supplied schema. Never raises."""
    findings: list[Finding] = []
    if not isinstance(schema, dict):
        fail(findings, "UNSUPPORTED_SCHEMA", "$", "schema must be an object")
        return findings
    _reject_uncheckable(schema, "$", findings)
    if findings:
        return findings
    _check_value(output, schema, "$", findings)
    return findings


def _reject_uncheckable(schema: object, path: str, findings: list[Finding]) -> None:
    """Reject any schema this module cannot fully interpret.

    Checks keyword *values*, not just names. Validating names alone leaves the
    same fail-open hole one layer down: ``{"type": "date"}`` passes a name
    check, then ``_JSON_TYPES`` has no entry, no type check runs, and arbitrary
    output is certified. Same for a boolean subschema (``{"secret": false}`` is
    valid JSON Schema meaning "reject everything"), a non-list ``required``, or
    a non-dict ``properties`` — each would be silently skipped by the walk.
    """
    if len(findings) >= _MAX_FINDINGS:
        return
    # Boolean schemas are valid JSON Schema (true = accept, false = reject) and
    # are not implemented here, so they must be rejected rather than ignored.
    if isinstance(schema, bool):
        fail(findings, "UNSUPPORTED_SCHEMA", path,
             "boolean schemas cannot be checked by this verifier")
        return
    if not isinstance(schema, dict):
        fail(findings, "UNSUPPORTED_SCHEMA", path, "subschema must be an object")
        return

    for key in schema:
        if key not in _SUPPORTED_KEYWORDS:
            fail(findings, "UNSUPPORTED_SCHEMA", path,
                 f"{key!r} cannot be checked by this verifier")

    if "type" in schema:
        declared = schema["type"]
        if not isinstance(declared, str):
            fail(findings, "UNSUPPORTED_SCHEMA", path,
                 "union and non-string 'type' cannot be checked by this verifier")
        elif declared not in _JSON_TYPES:
            fail(findings, "UNSUPPORTED_SCHEMA", path,
                 f"unknown type {declared!r} cannot be checked by this verifier")

    if "enum" in schema and not isinstance(schema["enum"], list):
        fail(findings, "UNSUPPORTED_SCHEMA", path, "'enum' must be an array")

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or not all(
            isinstance(name, str) for name in required
        ):
            fail(findings, "UNSUPPORTED_SCHEMA", path,
                 "'required' must be an array of field names")

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            fail(findings, "UNSUPPORTED_SCHEMA", path, "'properties' must be an object")
        else:
            for name, sub in properties.items():
                _reject_uncheckable(sub, f"{path}.{name}", findings)

    if "items" in schema:
        _reject_uncheckable(schema["items"], f"{path}[]", findings)


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def _check_value(value: object, schema: dict, path: str, findings: list[Finding]) -> None:
    if len(findings) >= _MAX_FINDINGS:
        return
    declared = schema.get("type")
    if isinstance(declared, str):
        expected = _JSON_TYPES.get(declared)
        # bool is a subclass of int; an integer field must not accept True.
        wrong_bool = declared in ("integer", "number") and isinstance(value, bool)
        if expected is not None and (wrong_bool or not isinstance(value, expected)):
            fail(findings, "TYPE_MISMATCH", path, f"expected {declared}")
            return
    # ``type`` is optional in JSON Schema and routinely omitted on objects that
    # already carry properties/required. Without this the walk below is gated on
    # isinstance(value, dict), so a bare string or null would be reported as
    # sound — the caller's schema said "object" in every way except the keyword.
    elif "properties" in schema or "required" in schema:
        if not isinstance(value, dict):
            fail(findings, "TYPE_MISMATCH", path, "expected object")
            return
    elif "items" in schema:
        if not isinstance(value, list):
            fail(findings, "TYPE_MISMATCH", path, "expected array")
            return

    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        fail(findings, "NOT_IN_ENUM", path, "value is outside the declared enum")
        return

    if isinstance(value, dict):
        # Fields already reported as absent or null are skipped below, so one
        # missing leaf yields one finding rather than a second, redundant
        # TYPE_MISMATCH for the same field.
        reported: set[str] = set()
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if not isinstance(name, str):
                    continue
                if name not in value:
                    fail(findings, "MISSING_FIELD", f"{path}.{name}", "required field absent")
                    reported.add(name)
                elif value[name] is None:
                    fail(findings, "NULL_LEAF", f"{path}.{name}", "required field is null")
                    reported.add(name)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, sub in properties.items():
                if isinstance(sub, dict) and name in value and name not in reported:
                    _check_value(value[name], sub, f"{path}.{name}", findings)

    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _check_value(item, items, f"{path}[{index}]", findings)


