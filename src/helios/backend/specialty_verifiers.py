"""Non-LLM verifiers for specialty-tool output.

Every specialty tool in the registry is delegable *because* its output can be
checked without asking another model. That is the whole selection rule: a cheap
model plus a cheap deterministic check beats an expensive model with no check,
and it makes the "is this model good enough" question moot for the failure
modes that actually matter.

These are the checks. Each takes the parsed worker output plus the evidence the
host already holds (the source text, the taxonomy, the file inventory) and
returns a list of ``Finding``. An empty list means the output is structurally
sound; anything else means fall back to the primary model. They never call a
model, never touch the network, and never raise on malformed input — a worker
returning garbage is an expected outcome, not an exception.

Deliberately *not* a JSON Schema engine. The repo ships no runtime dependencies
and a general validator is far more code than the five contracts need; only
``extract_structured`` accepts a caller-supplied schema, so it gets a minimal
subset check (type, required, enum, nested objects and arrays) and nothing
else. Note what is being checked and what is not: JSON *validity* is at ceiling
for every model worth using and predicts nothing. What separates a usable
extraction from a plausible-looking one is exact leaf values, resolvable spans,
and quotes that really appear in the source — so that is what these verify.

GTK-free.
"""

from __future__ import annotations


from helios.backend.specialty_metadata import verify_metadata
from helios.backend.specialty_schema import Finding, check_against_schema, fail

__all__ = [
    "Finding",
    "verify_classification",
    "verify_digest",
    "verify_extraction",
    "verify_localization",
    "verify_metadata",
]

_MAX_EXPLANATION_CHARS = 2_000

def _rows(value: object, key: str, findings: list[Finding]) -> list:
    """Pull a list field, recording a finding when it is missing or wrong."""
    if not isinstance(value, dict):
        fail(findings, "NOT_AN_OBJECT", "$", "output is not a JSON object")
        return []
    rows = value.get(key)
    if not isinstance(rows, list):
        fail(findings, "MISSING_FIELD", f"$.{key}", f"{key} must be an array")
        return []
    return rows


def _span(raw: object) -> tuple[int, int] | None:
    """Coerce a [start, end] pair. Rejects bools — ``bool`` is an ``int``."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    start, end = raw
    if type(start) is not int or type(end) is not int:
        return None
    if start < 1 or end < start:
        return None
    return start, end


# ── digest_source ──────────────────────────────────────────────────────────


def verify_digest(output: object, *, source: str) -> list[Finding]:
    """Every claim must cite a resolvable line range and quote the source exactly.

    This is the check that makes a cheap summarizer safe to use. Faithfulness
    is the one place small models measurably match frontier ones, but only for
    claims grounded in supplied text — so an unresolvable range or a quote that
    does not appear verbatim is treated as a fabrication, not a formatting nit.
    """
    findings: list[Finding] = []
    # split("\n"), not splitlines(): splitlines() also breaks on \r, \v,
    # \f and U+2028, so the "\n".join round-trip below would drop the real
    # separator and reject a verbatim CRLF quote as a fabrication.
    lines = source.split("\n")
    claims = _rows(output, "claims", findings)
    if not claims and not findings:
        fail(findings, "EMPTY_RESULT", "$.claims", "no claims returned")

    for index, claim in enumerate(claims):
        at = f"$.claims[{index}]"
        if not isinstance(claim, dict):
            fail(findings, "NOT_AN_OBJECT", at, "claim must be an object")
            continue
        text = claim.get("claim")
        if not isinstance(text, str) or not text.strip():
            fail(findings, "MISSING_FIELD", f"{at}.claim", "claim text is required")

        span = _span(claim.get("lines"))
        if span is None:
            fail(findings, "BAD_SPAN", f"{at}.lines", "lines must be [start, end], 1-based")
            continue
        start, end = span
        if end > len(lines):
            fail(
                findings,
                "SPAN_OUT_OF_RANGE",
                f"{at}.lines",
                f"line {end} exceeds the {len(lines)}-line source",
            )
            continue

        quote = claim.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            fail(findings, "MISSING_FIELD", f"{at}.quote", "quote is required")
            continue
        cited = "\n".join(lines[start - 1 : end])
        if quote not in cited:
            fail(
                findings,
                "QUOTE_NOT_IN_SOURCE",
                f"{at}.quote",
                "quote is not a substring of the cited lines",
            )
    return findings


# ── extract_structured ─────────────────────────────────────────────────────


def verify_extraction(output: object, *, schema: dict) -> list[Finding]:
    """Check the extraction against the caller's schema, by leaf value.

    **Fails closed on schemas it cannot read.** An empty finding list means
    "verified", and the caller's rule is to accept the cheap worker's output on
    an empty list — so silently ignoring a keyword this subset does not
    implement would certify a constraint that was never checked. A schema using
    ``minLength``, ``pattern``, ``anyOf``, ``$ref``, a union ``type``, or
    anything else outside ``_SUPPORTED_KEYWORDS`` is reported as
    ``UNSUPPORTED_SCHEMA`` and routed to the primary model, which is the honest
    answer: for that schema, no non-LLM verifier exists, and the registry's
    admission rule therefore is not satisfied.

    Absent fields are reported rather than defaulted — a missing leaf is the
    failure mode that survives every JSON-validity check.
    """
    return check_against_schema(output, schema)


# ── classify_closed ────────────────────────────────────────────────────────


def verify_classification(
    output: object,
    *,
    taxonomy: set[str] | frozenset[str],
    source: str,
    multi_label: bool = True,
) -> list[Finding]:
    """Labels must come from the taxonomy and cite spans that resolve.

    Abstention is a first-class outcome: a classifier that declines is useful,
    one that guesses confidently is not. So ``abstain`` requires a reason, and
    an abstaining result is not required to carry labels.
    """
    findings: list[Finding] = []
    if not isinstance(output, dict):
        fail(findings, "NOT_AN_OBJECT", "$", "output is not a JSON object")
        return findings

    abstain = output.get("abstain")
    if type(abstain) is not bool:
        fail(findings, "MISSING_FIELD", "$.abstain", "abstain must be a boolean")
        abstain = False
    reason = output.get("reason")
    if abstain and (not isinstance(reason, str) or not reason.strip()):
        fail(findings, "MISSING_FIELD", "$.reason", "abstaining requires a reason")

    labels = _rows(output, "labels", findings)
    if not labels and not abstain and not findings:
        fail(findings, "EMPTY_RESULT", "$.labels", "no labels and no abstention")
    if abstain and labels:
        fail(findings, "CONTRADICTORY_RESULT", "$.abstain",
              "abstained while also returning labels")
    if not multi_label and len(labels) > 1:
        fail(findings, "TOO_MANY_LABELS", "$.labels",
              f"single-label request returned {len(labels)} labels")

    seen: set[str] = set()
    line_count = len(source.split("\n"))
    for index, label in enumerate(labels):
        at = f"$.labels[{index}]"
        if not isinstance(label, dict):
            fail(findings, "NOT_AN_OBJECT", at, "label must be an object")
            continue
        label_id = label.get("label_id")
        if not isinstance(label_id, str) or label_id not in taxonomy:
            fail(findings, "LABEL_NOT_IN_TAXONOMY", f"{at}.label_id",
                  "label is outside the declared taxonomy")
        elif label_id in seen:
            fail(findings, "DUPLICATE_LABEL", f"{at}.label_id",
                  f"{label_id!r} was already assigned")
        else:
            seen.add(label_id)
        spans = label.get("evidence_spans")
        if not isinstance(spans, list) or not spans:
            fail(findings, "MISSING_FIELD", f"{at}.evidence_spans",
                  "at least one evidence span is required")
            continue
        for span_index, raw in enumerate(spans):
            span = _span(raw)
            if span is None:
                fail(findings, "BAD_SPAN", f"{at}.evidence_spans[{span_index}]",
                      "span must be [start, end], 1-based")
            elif span[1] > line_count:
                fail(findings, "SPAN_OUT_OF_RANGE", f"{at}.evidence_spans[{span_index}]",
                      f"line {span[1]} exceeds the {line_count}-line source")
    return findings


# ── localize_code ──────────────────────────────────────────────────────────


def verify_localization(output: object, *, inventory: dict[str, int]) -> list[Finding]:
    """Paths must exist and ranges must fall inside them.

    ``inventory`` maps a repo-relative path to its line count — the host builds
    it, so a hallucinated file or an out-of-range hunk is caught before the
    primary model ever sees it. The explanation is length-bounded on purpose:
    the value of this tool is the *locations*, and free-form prose from a
    weaker model is the failure mode that misleads the caller precisely because
    it cannot be graded.
    """
    findings: list[Finding] = []
    if not isinstance(output, dict):
        fail(findings, "NOT_AN_OBJECT", "$", "output is not a JSON object")
        return findings

    explanation = output.get("explanation")
    if explanation is None:
        fail(findings, "MISSING_FIELD", "$.explanation", "explanation is required")
    elif not isinstance(explanation, str):
        # Without this a non-string value skips the ceiling below, so an
        # arbitrarily large payload rides through under that key.
        fail(findings, "TYPE_MISMATCH", "$.explanation", "explanation must be a string")
    elif len(explanation) > _MAX_EXPLANATION_CHARS:
        fail(findings, "EXPLANATION_TOO_LONG", "$.explanation",
              f"explanation exceeds {_MAX_EXPLANATION_CHARS} characters")

    files = output.get("files")
    if not isinstance(files, dict):
        fail(findings, "MISSING_FIELD", "$.files", "files must be an object")
        return findings
    if not files:
        fail(findings, "EMPTY_RESULT", "$.files", "no locations returned")

    for path, ranges in files.items():
        at = f"$.files[{path!r}]"
        if path not in inventory:
            fail(findings, "PATH_NOT_FOUND", at, "path is not in the repository inventory")
            continue
        if not isinstance(ranges, list) or not ranges:
            fail(findings, "MISSING_FIELD", at, "at least one line range is required")
            continue
        limit = inventory[path]
        for index, raw in enumerate(ranges):
            span = _span(raw)
            if span is None:
                fail(findings, "BAD_SPAN", f"{at}[{index}]",
                      "range must be [start, end], 1-based")
            elif span[1] > limit:
                fail(findings, "SPAN_OUT_OF_RANGE", f"{at}[{index}]",
                      f"line {span[1]} exceeds the {limit}-line file")
    return findings
