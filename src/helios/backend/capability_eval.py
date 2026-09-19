"""Offline, artifact-graded paired evaluations. Never invokes or promotes models.

The operator supplies a frozen suite and captures independent run directories.
Checks inspect resulting files, never a model's self-reported success. Reports
contain identifiers, hashes and metrics, not prompts, answers or file contents.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

from helios.backend.sensitive_text import scrub_sensitive

MAX_BYTES = 1_048_576
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")


def _read_file(path: Path) -> bytes:
    # A byte limit cannot protect an open() blocked on a generated FIFO.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("evaluation input must be a regular file")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("evaluation file exceeds 1 MiB")
    return data


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _json(data: bytes):
    return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def read_json(path: Path):
    return _json(_read_file(path))


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _label(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid evaluation identifier")
    if scrub_sensitive(value)[1]:
        raise ValueError("credential-shaped evaluation identifier")
    return value


def _file(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("artifact path must be relative")
    path = root / relative
    if ".." in Path(relative).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("artifact escapes its run directory")
    return path


def validate_suite(suite: object) -> dict:
    if not isinstance(suite, dict) or type(suite.get("version")) is not int or suite["version"] != 1:
        raise ValueError("unsupported evaluation suite")
    _label(suite.get("id"))
    cases = suite.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError("suite must contain 1–100 cases")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("case must be an object")
        name = _label(case.get("id"))
        if name in seen:
            raise ValueError("duplicate suite case")
        seen.add(name)
        checks = case.get("checks")
        if not isinstance(checks, list) or not 1 <= len(checks) <= 30:
            raise ValueError("each case requires 1–30 artifact checks")
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get("kind"), str) or check["kind"] not in {
                "text_equals", "json_equals",
            } or "expected" not in check:
                raise ValueError("unsupported or incomplete artifact check")
            _file(Path("/evaluation"), check.get("path"))
            if check["kind"] == "text_equals" and not isinstance(check["expected"], str):
                raise ValueError("text check requires string expected value")
    return suite


def _number(value: object, *, optional: bool = False, integer: bool = False):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid run metric")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0 or (integer and not isinstance(value, int)):
        raise ValueError("invalid run metric")
    return value


def grade_run(suite: dict, directory: Path) -> dict:
    """Grade a run against frozen expected artifacts; missing runs fail coverage."""
    validate_suite(suite)
    meta = read_json(directory / "run.json")
    required = {"version", "suite_sha256", "surface", "model", "settings_sha256",
                "environment_sha256", "runs"}
    if (not isinstance(meta, dict) or set(meta) != required
            or type(meta["version"]) is not int or meta["version"] != 1):
        raise ValueError("invalid run manifest")
    if meta["suite_sha256"] != fingerprint(suite):
        raise ValueError("run does not match frozen suite")
    for key in ("settings_sha256", "environment_sha256"):
        if not isinstance(meta[key], str) or not re.fullmatch(r"[0-9a-f]{64}", meta[key]):
            raise ValueError("settings/environment fingerprint required")
    surface, model = _label(meta["surface"]), _label(meta["model"])
    if not isinstance(meta["runs"], list) or len(meta["runs"]) > 1000:
        raise ValueError("invalid run list")
    cases = {case["id"]: case for case in suite["cases"]}
    graded, seen = [], set()
    for run in meta["runs"]:
        fields = {"case_id", "repeat", "status", "duration_ms", "input_tokens",
                  "output_tokens", "cost_usd", "interventions", "artifacts"}
        if not isinstance(run, dict) or set(run) != fields:
            raise ValueError("invalid case run")
        case_id = _label(run["case_id"])
        repeat = _number(run["repeat"], integer=True)
        identity = (case_id, repeat)
        if case_id not in cases or identity in seen:
            raise ValueError("unknown or duplicate case run")
        seen.add(identity)
        if not isinstance(run["status"], str) or run["status"] not in {"completed", "failed", "interrupted"}:
            raise ValueError("invalid run status")
        metrics = {key: _number(run[key], optional=key == "cost_usd",
                               integer=key in {"input_tokens", "output_tokens", "interventions"})
                   for key in ("duration_ms", "input_tokens", "output_tokens", "cost_usd", "interventions")}
        artifacts = _file(directory, run["artifacts"])
        checks = []
        for check in cases[case_id]["checks"]:
            path = _file(artifacts, check["path"])
            try:
                data = _read_file(path)
                actual = _json(data) if check["kind"] == "json_equals" else data.decode("utf-8")
                # Canonical JSON equality keeps false distinct from 0.
                passed = fingerprint(actual) == fingerprint(check["expected"])
                checks.append({"passed": passed, "artifact_sha256": hashlib.sha256(data).hexdigest()})
            except (OSError, ValueError, UnicodeError, RecursionError):
                checks.append({"passed": False, "artifact_sha256": None})
        graded.append({"case_id": case_id, "repeat": repeat, **metrics,
                       "passed": run["status"] == "completed" and all(c["passed"] for c in checks),
                       "checks": checks})
    return {"suite_sha256": fingerprint(suite), "surface": surface, "model": model,
            "settings_sha256": meta["settings_sha256"], "environment_sha256": meta["environment_sha256"],
            "cases": graded, "missing_cases": sorted(set(cases) - {name for name, _ in seen}),
            "passed": sum(row["passed"] for row in graded), "total": len(graded)}


def compare(suite: dict, baseline: Path, candidate: Path, *,
            baseline_surface: str | None = None, candidate_surface: str | None = None) -> dict:
    if baseline.resolve() == candidate.resolve():
        raise ValueError("paired evaluation requires independent run directories")
    left, right = grade_run(suite, baseline), grade_run(suite, candidate)
    for field in ("suite_sha256", "model", "settings_sha256", "environment_sha256"):
        if left[field] != right[field]:
            raise ValueError(f"paired evaluation requires matching {field}")
    if left["surface"] == right["surface"]:
        raise ValueError("paired evaluation requires distinct surface labels")
    for grade, expected in ((left, baseline_surface), (right, candidate_surface)):
        if expected is not None and grade["surface"] != _label(expected):
            raise ValueError("run does not match its declared comparison surface")
    rows = {(r["case_id"], r["repeat"]): r for r in left["cases"]}
    other = {(r["case_id"], r["repeat"]): r for r in right["cases"]}
    if rows.keys() != other.keys() or left["missing_cases"] or right["missing_cases"]:
        raise ValueError("paired evaluation requires identical, complete case coverage")
    regressions = [f"{key[0]}:{key[1]}" for key in rows
                   if rows[key]["passed"] and not other[key]["passed"]]
    return {"baseline": left, "candidate": right, "regressions": regressions,
            "promotion_authorized": False,
            "note": "Artifact checks only; human review and routing promotion remain separate."}
