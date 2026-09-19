"""Non-LLM verifiers for specialty-tool output.

These are what make delegation to a cheaper model defensible: the output is
checkable without asking another model. So the tests care most about the cases
a plausible-looking wrong answer produces — a quote that reads right but does
not appear in the source, a line range past the end of the file, a label
outside the taxonomy, a documented parameter the function does not have.

A verifier that only catches malformed JSON would be worthless: JSON validity
is at ceiling for every model worth using and predicts nothing about whether
the content is true.
"""

from __future__ import annotations

import pytest

from helios.backend.specialty_verifiers import (
    verify_classification,
    verify_digest,
    verify_extraction,
    verify_localization,
)

SOURCE = "\n".join([
    "def load(path):",                    # 1
    "    with open(path) as handle:",     # 2
    "        return handle.read()",       # 3
    "",                                   # 4
    "TIMEOUT = 30",                       # 5
])


def codes(findings):
    return [f.code for f in findings]


class TestDigest:
    def test_accepts_a_grounded_claim(self):
        out = {"claims": [
            {"lines": [1, 3], "quote": "open(path)", "claim": "load() reads a file"},
        ]}
        assert verify_digest(out, source=SOURCE) == []

    def test_rejects_a_quote_absent_from_the_source(self):
        """The failure mode that matters: fluent, plausible, fabricated."""
        out = {"claims": [
            {"lines": [1, 3], "quote": "open(path, encoding='utf-8')",
             "claim": "load() decodes as UTF-8"},
        ]}
        assert codes(verify_digest(out, source=SOURCE)) == ["QUOTE_NOT_IN_SOURCE"]

    def test_rejects_a_quote_from_outside_the_cited_lines(self):
        """The quote is real but the citation points elsewhere — the claim is
        still unsupported by the evidence it offers."""
        out = {"claims": [
            {"lines": [1, 2], "quote": "TIMEOUT = 30", "claim": "timeout is 30"},
        ]}
        assert codes(verify_digest(out, source=SOURCE)) == ["QUOTE_NOT_IN_SOURCE"]

    def test_rejects_a_range_past_the_end_of_the_source(self):
        out = {"claims": [{"lines": [4, 99], "quote": "x", "claim": "y"}]}
        assert codes(verify_digest(out, source=SOURCE)) == ["SPAN_OUT_OF_RANGE"]

    @pytest.mark.parametrize("lines", [
        [0, 2],            # 1-based
        [3, 1],            # inverted
        [1],               # wrong arity
        "1-3",             # wrong type
        [True, True],      # bool is an int subclass
        None,
    ])
    def test_rejects_malformed_ranges(self, lines):
        out = {"claims": [{"lines": lines, "quote": "def load", "claim": "y"}]}
        assert codes(verify_digest(out, source=SOURCE)) == ["BAD_SPAN"]

    def test_reports_an_empty_result(self):
        assert codes(verify_digest({"claims": []}, source=SOURCE)) == ["EMPTY_RESULT"]

    def test_never_raises_on_garbage(self):
        for junk in (None, [], "text", {"claims": "no"}, {"claims": [None, 7]}):
            assert isinstance(verify_digest(junk, source=SOURCE), list)


class TestExtraction:
    SCHEMA = {
        "type": "object",
        "required": ["name", "port"],
        "properties": {
            "name": {"type": "string"},
            "port": {"type": "integer"},
            "mode": {"type": "string", "enum": ["tcp", "udp"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "owner": {"type": "object", "required": ["email"],
                      "properties": {"email": {"type": "string"}}},
        },
    }

    def test_accepts_a_conforming_object(self):
        out = {"name": "helios", "port": 8080, "mode": "tcp", "tags": ["a"],
               "owner": {"email": "x@y.z"}}
        assert verify_extraction(out, schema=self.SCHEMA) == []

    def test_reports_a_missing_required_leaf(self):
        """The failure JSON-validity checks cannot see."""
        assert codes(verify_extraction({"name": "helios"}, schema=self.SCHEMA)) == [
            "MISSING_FIELD"
        ]

    def test_reports_a_null_required_leaf(self):
        out = {"name": "helios", "port": None}
        assert codes(verify_extraction(out, schema=self.SCHEMA)) == ["NULL_LEAF"]

    def test_reports_a_type_mismatch(self):
        out = {"name": "helios", "port": "8080"}
        assert codes(verify_extraction(out, schema=self.SCHEMA)) == ["TYPE_MISMATCH"]

    def test_a_boolean_is_not_an_integer(self):
        """bool subclasses int in Python; a naive isinstance check accepts it."""
        out = {"name": "helios", "port": True}
        assert codes(verify_extraction(out, schema=self.SCHEMA)) == ["TYPE_MISMATCH"]

    def test_reports_a_value_outside_the_enum(self):
        out = {"name": "helios", "port": 1, "mode": "sctp"}
        assert codes(verify_extraction(out, schema=self.SCHEMA)) == ["NOT_IN_ENUM"]

    def test_checks_nested_objects_and_array_items(self):
        out = {"name": "h", "port": 1, "tags": ["ok", 7], "owner": {}}
        found = codes(verify_extraction(out, schema=self.SCHEMA))
        assert "TYPE_MISMATCH" in found
        assert "MISSING_FIELD" in found

    def test_fails_closed_on_a_schema_it_cannot_read(self):
        """An empty finding list means "verified", and the caller accepts the
        cheap worker's output on an empty list — so a keyword this subset does
        not implement must be reported, never ignored. Otherwise the caller is
        told a constraint held when it was never checked."""
        for unsupported in (
            {"type": "string", "minLength": 5},
            {"type": "array", "items": {"type": "string"}, "minItems": 2},
            {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            {"$ref": "#/$defs/thing"},
            {"type": ["string", "null"]},
            {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}}},
        ):
            found = verify_extraction({"a": "x"}, schema=unsupported)
            assert codes(found) == ["UNSUPPORTED_SCHEMA"], unsupported

    def test_reports_an_object_schema_that_omits_the_type_keyword(self):
        """`type` is optional in JSON Schema and routinely omitted on objects
        carrying properties/required. Without inferring it, the walk is gated on
        isinstance(value, dict) and a bare string is reported as sound."""
        schema = {"required": ["name"], "properties": {"name": {"type": "string"}}}
        assert codes(verify_extraction("not an object", schema=schema)) == ["TYPE_MISMATCH"]
        assert codes(verify_extraction(None, schema=schema)) == ["TYPE_MISMATCH"]
        assert codes(verify_extraction({}, schema=schema)) == ["MISSING_FIELD"]
        assert verify_extraction({"name": "ok"}, schema=schema) == []

    @pytest.mark.parametrize("schema,why", [
        ({"type": "date"}, "unknown type name"),
        ({"type": "uuid"}, "unknown type name"),
        ({"type": "object", "properties": {"secret": False}}, "boolean subschema"),
        ({"type": "object", "properties": {"a": True}}, "boolean subschema"),
        ({"type": "object", "properties": "not-an-object"}, "malformed properties"),
        ({"type": "object", "required": "name"}, "malformed required"),
        ({"type": "object", "required": [1, 2]}, "non-string required entries"),
        ({"enum": "tcp"}, "malformed enum"),
        ({"type": "array", "items": "string"}, "malformed items"),
    ])
    def test_rejects_malformed_keyword_values_not_just_unknown_names(self, schema, why):
        """Validating keyword NAMES alone left the same fail-open hole one
        layer down: {"type": "date"} passes a name check, then _JSON_TYPES has
        no entry, no type check runs, and arbitrary output is certified."""
        found = verify_extraction({"secret": "x", "a": 1, "name": "n"}, schema=schema)
        assert codes(found) == ["UNSUPPORTED_SCHEMA"], why

    def test_still_accepts_a_fully_interpretable_schema(self):
        assert verify_extraction(
            {"name": "helios", "port": 1},
            schema={"type": "object", "required": ["name", "port"],
                    "properties": {"name": {"type": "string"},
                                   "port": {"type": "integer"}}},
        ) == []

    def test_never_raises_on_a_junk_schema(self):
        assert isinstance(verify_extraction({"a": 1}, schema={}), list)
        assert isinstance(verify_extraction({"a": 1}, schema=None), list)


class TestClassification:
    TAXONOMY = frozenset({"bug", "feature", "docs"})

    def test_accepts_a_grounded_label(self):
        out = {"labels": [{"evidence_spans": [[1, 2]], "label_id": "bug"}],
               "abstain": False}
        assert verify_classification(out, taxonomy=self.TAXONOMY, source=SOURCE) == []

    def test_rejects_an_invented_label(self):
        out = {"labels": [{"evidence_spans": [[1, 2]], "label_id": "chore"}],
               "abstain": False}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY, source=SOURCE)) == [
            "LABEL_NOT_IN_TAXONOMY"
        ]

    def test_rejects_a_label_with_no_evidence(self):
        out = {"labels": [{"evidence_spans": [], "label_id": "bug"}], "abstain": False}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY, source=SOURCE)) == [
            "MISSING_FIELD"
        ]

    def test_abstention_is_a_valid_result(self):
        out = {"labels": [], "abstain": True, "reason": "the item is out of scope"}
        assert verify_classification(out, taxonomy=self.TAXONOMY, source=SOURCE) == []

    def test_abstention_requires_a_reason(self):
        out = {"labels": [], "abstain": True}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY, source=SOURCE)) == [
            "MISSING_FIELD"
        ]

    def test_neither_labels_nor_abstention_is_an_empty_result(self):
        out = {"labels": [], "abstain": False}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY, source=SOURCE)) == [
            "EMPTY_RESULT"
        ]

    def test_rejects_more_than_one_label_when_single_label_was_requested(self):
        """multi_label is in the published input contract, so it has to reach
        the verifier — otherwise "pick one" silently returns many."""
        out = {"labels": [{"evidence_spans": [[1, 1]], "label_id": "bug"},
                          {"evidence_spans": [[1, 1]], "label_id": "docs"}],
               "abstain": False}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY,
                                           source=SOURCE, multi_label=False)) == [
            "TOO_MANY_LABELS"
        ]
        assert verify_classification(out, taxonomy=self.TAXONOMY,
                                     source=SOURCE, multi_label=True) == []

    def test_rejects_a_duplicated_label(self):
        out = {"labels": [{"evidence_spans": [[1, 1]], "label_id": "bug"},
                          {"evidence_spans": [[2, 2]], "label_id": "bug"}],
               "abstain": False}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY,
                                           source=SOURCE)) == ["DUPLICATE_LABEL"]

    def test_rejects_abstaining_while_also_labelling(self):
        out = {"labels": [{"evidence_spans": [[1, 1]], "label_id": "bug"}],
               "abstain": True, "reason": "unsure"}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY,
                                           source=SOURCE)) == ["CONTRADICTORY_RESULT"]

    def test_rejects_an_unresolvable_evidence_span(self):
        out = {"labels": [{"evidence_spans": [[1, 99]], "label_id": "bug"}],
               "abstain": False}
        assert codes(verify_classification(out, taxonomy=self.TAXONOMY, source=SOURCE)) == [
            "SPAN_OUT_OF_RANGE"
        ]


class TestLocalization:
    INVENTORY = {"src/app.py": 120, "tests/test_app.py": 40}

    def test_accepts_real_paths_and_ranges(self):
        out = {"explanation": "entry point", "files": {"src/app.py": [[10, 25]]}}
        assert verify_localization(out, inventory=self.INVENTORY) == []

    def test_rejects_a_hallucinated_path(self):
        out = {"explanation": "", "files": {"src/does_not_exist.py": [[1, 2]]}}
        assert codes(verify_localization(out, inventory=self.INVENTORY)) == [
            "PATH_NOT_FOUND"
        ]

    def test_rejects_a_range_past_the_end_of_the_file(self):
        out = {"explanation": "", "files": {"tests/test_app.py": [[30, 500]]}}
        assert codes(verify_localization(out, inventory=self.INVENTORY)) == [
            "SPAN_OUT_OF_RANGE"
        ]

    def test_bounds_the_explanation(self):
        """The value of this tool is locations. Ungradable prose from a weaker
        model is the documented failure mode."""
        out = {"explanation": "x" * 5_000, "files": {"src/app.py": [[1, 2]]}}
        assert codes(verify_localization(out, inventory=self.INVENTORY)) == [
            "EXPLANATION_TOO_LONG"
        ]

    def test_requires_a_string_explanation(self):
        """The schema marks it required, and a non-string value would skip the
        length ceiling entirely — letting an arbitrary payload ride through."""
        assert codes(verify_localization({"files": {"src/app.py": [[1, 2]]}},
                                         inventory=self.INVENTORY)) == ["MISSING_FIELD"]
        assert codes(verify_localization({"explanation": {"a": "x" * 100_000},
                                          "files": {"src/app.py": [[1, 2]]}},
                                         inventory=self.INVENTORY)) == ["TYPE_MISMATCH"]

    def test_reports_no_locations(self):
        out = {"explanation": "nothing found", "files": {}}
        assert codes(verify_localization(out, inventory=self.INVENTORY)) == ["EMPTY_RESULT"]
