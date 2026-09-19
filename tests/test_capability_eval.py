from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helios.backend.capability_eval import compare, fingerprint, grade_run, validate_suite


@pytest.fixture
def suite():
    return {"version": 1, "id": "continuity-v1", "cases": [
        {"id": "constraint", "checks": [{"kind": "json_equals", "path": "result.json",
                                           "expected": {"database": "PostgreSQL"}}]},
    ]}


def make_run(root, suite, *, output=None, surface="helios"):
    root.mkdir()
    artifacts = root / "case-0"
    artifacts.mkdir()
    (artifacts / "result.json").write_text(json.dumps(
        {"database": "PostgreSQL"} if output is None else output))
    manifest = {"version": 1, "suite_sha256": fingerprint(suite), "surface": surface,
                "model": "model-test", "settings_sha256": "a" * 64,
                "environment_sha256": "b" * 64, "runs": [{
                    "case_id": "constraint", "repeat": 0, "status": "completed",
                    "duration_ms": 100, "input_tokens": 20, "output_tokens": 10,
                    "cost_usd": None, "interventions": 0, "artifacts": "case-0"}]}
    (root / "run.json").write_text(json.dumps(manifest))
    return manifest


def test_paired_artifact_regression_beats_self_report_and_never_promotes(tmp_path, suite):
    make_run(tmp_path / "native", suite, surface="codex")
    make_run(tmp_path / "helios", suite, output={"database": "SQLite"})
    result = compare(suite, tmp_path / "native", tmp_path / "helios")
    assert result["regressions"] == ["constraint:0"]
    assert result["baseline"]["passed"] == 1
    assert result["candidate"]["passed"] == 0
    assert result["promotion_authorized"] is False
    assert "SQLite" not in json.dumps(result)
    assert "PostgreSQL" not in json.dumps(result)


def test_same_artifacts_pass_but_missing_artifacts_fail(tmp_path, suite):
    make_run(tmp_path / "run", suite)
    assert grade_run(suite, tmp_path / "run")["passed"] == 1
    (tmp_path / "run/case-0/result.json").unlink()
    assert grade_run(suite, tmp_path / "run")["passed"] == 0


@pytest.mark.parametrize("change", ["model", "settings_sha256", "environment_sha256"])
def test_pairs_reject_mismatched_conditions(tmp_path, suite, change):
    make_run(tmp_path / "left", suite)
    manifest = make_run(tmp_path / "right", suite)
    manifest[change] = "other-model" if change == "model" else "c" * 64
    (tmp_path / "right/run.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="matching"):
        compare(suite, tmp_path / "left", tmp_path / "right")


@pytest.mark.parametrize("change", ["missing", "duplicate", "suite", "nan", "boolean"])
def test_incomplete_or_corrupt_runs_never_pass(tmp_path, suite, change):
    make_run(tmp_path / "left", suite)
    manifest = make_run(tmp_path / "right", suite)
    if change == "missing":
        manifest["runs"] = []
    elif change == "duplicate":
        manifest["runs"] *= 2
    elif change == "suite":
        manifest["suite_sha256"] = "0" * 64
    elif change == "nan":
        manifest["runs"][0]["duration_ms"] = float("nan")
    else:
        manifest["runs"][0]["repeat"] = True
    (tmp_path / "right/run.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        compare(suite, tmp_path / "left", tmp_path / "right")


def test_symlink_artifact_escape_is_rejected(tmp_path, suite):
    make_run(tmp_path / "run", suite)
    outside = tmp_path / "private.json"
    outside.write_text('{"private": "never include"}')
    target = tmp_path / "run/case-0/result.json"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        grade_run(suite, tmp_path / "run")


def test_unsupported_rubric_and_boolean_numeric_confusion_are_rejected(tmp_path, suite):
    changed = copy.deepcopy(suite)
    changed["cases"][0]["checks"][0]["kind"] = "model_says_pass"
    with pytest.raises(ValueError, match="unsupported"):
        validate_suite(changed)
    suite["cases"][0]["checks"][0]["expected"] = {"ok": False}
    make_run(tmp_path / "run", suite, output={"ok": 0})
    assert grade_run(suite, tmp_path / "run")["passed"] == 0


@pytest.mark.parametrize("location", ["manifest", "artifact"])
def test_fifo_inputs_fail_without_blocking(tmp_path, suite, location):
    make_run(tmp_path / "run", suite)
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite))
    target = tmp_path / "run" / ("run.json" if location == "manifest" else "case-0/result.json")
    target.unlink()
    os.mkfifo(target)
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/helios-eval"),
         str(suite_path), "--run", str(tmp_path / "run")],
        capture_output=True, timeout=5,
    )
    assert process.returncode == (2 if location == "manifest" else 1)


def test_duplicate_json_key_cannot_turn_wrong_answer_into_pass(tmp_path, suite):
    make_run(tmp_path / "run", suite)
    (tmp_path / "run/case-0/result.json").write_text(
        '{"database":"SQLite","database":"PostgreSQL"}')
    assert grade_run(suite, tmp_path / "run")["passed"] == 0


def test_unrepresentable_metric_is_invalid_input(tmp_path, suite):
    manifest = make_run(tmp_path / "run", suite)
    manifest["runs"][0]["duration_ms"] = 10**400
    (tmp_path / "run/run.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="metric"):
        grade_run(suite, tmp_path / "run")


def test_duplicate_capture_or_surface_never_counts_as_comparison(tmp_path, suite):
    make_run(tmp_path / "left", suite)
    make_run(tmp_path / "right", suite)
    with pytest.raises(ValueError, match="independent"):
        compare(suite, tmp_path / "left", tmp_path / "left")
    with pytest.raises(ValueError, match="distinct"):
        compare(suite, tmp_path / "left", tmp_path / "right")


def test_declared_surfaces_prevent_swapped_or_mislabeled_pair(tmp_path, suite):
    make_run(tmp_path / "native", suite, surface="codex")
    make_run(tmp_path / "helios", suite)
    with pytest.raises(ValueError, match="declared"):
        compare(suite, tmp_path / "native", tmp_path / "helios",
                baseline_surface="claude", candidate_surface="helios")
    assert compare(suite, tmp_path / "native", tmp_path / "helios",
                   baseline_surface="codex", candidate_surface="helios")["regressions"] == []
