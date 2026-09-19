from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_no_gi_import():
    """mission_store must be importable with **no** `gi` dependency — CI runs
    the suite in a python:3.13-slim container with no GTK stack installed at
    all (see the module docstring).

    Verified in a *fresh subprocess*, never by deleting `gi` from this
    process's ``sys.modules``. On a GTK-capable box the full suite has already
    imported and initialised `gi`/GTK by the time this runs; wiping it and
    forcing a re-import re-registers the process-global GObject type system,
    which segfaults the next widget construction (e.g. PlanPane). That crash
    is invisible in gi-less CI but fatal when the whole suite runs in one
    process locally. A subprocess reproduces the CI condition hermetically
    without touching this interpreter's GTK state.
    """
    src = Path(__file__).resolve().parent.parent / "src"
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src) + (os.pathsep + existing if existing else "")
    check = (
        "import sys\n"
        "import helios.backend.mission_store  # noqa: F401\n"
        "leaked = sorted(m for m in sys.modules if m == 'gi' or m.startswith('gi.'))\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", check],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "importing helios.backend.mission_store pulled in `gi` (or failed):\n"
        f"{result.stderr}"
    )


def _write_mission(
    tmp_path,
    slug: str,
    *,
    status: str = "pending",
    phases: dict | None = None,
    slices: list | None = None,
    created_at: float | None = None,
    spec_toml: str | None = None,
    corrupt_state: bool = False,
):
    mission_dir = tmp_path / "missions" / slug
    (mission_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    state_path = mission_dir / "state.json"
    if corrupt_state:
        state_path.write_text("{not json", encoding="utf-8")
        return mission_dir
    state = {
        "mission": slug,
        "created_at": created_at if created_at is not None else time.time(),
        "status": status,
        "phases": phases or {},
        "slices": slices or [],
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    if spec_toml is not None:
        (mission_dir / "mission.toml").write_text(spec_toml, encoding="utf-8")
    return mission_dir


def _phase(status="pending", note="", usage=None):
    return {"status": status, "note": note, "usage": usage or {}}


@pytest.fixture(autouse=True)
def tandem_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    return tmp_path


def test_tandem_root_reads_env_at_call_time(tmp_path, monkeypatch):
    from helios.backend import mission_store

    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path / "first"))
    assert mission_store.tandem_root() == tmp_path / "first"
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path / "second"))
    assert mission_store.tandem_root() == tmp_path / "second"


def test_list_missions_empty_when_no_dir(tmp_path):
    from helios.backend import mission_store

    assert mission_store.list_missions() == []


def test_happy_path_parse(tmp_path):
    from helios.backend import mission_store

    spec_toml = """
goal = "Ship the mission pane"
repo = "/home/alice/helios"
base_branch = "main"
test_command = "python3 -m pytest -q"
gates = ["reconcile"]

[models]
plan = "opus"
implement = "sonnet"
audit = "gpt-5.5"
review = "gpt-5.5"
"""
    _write_mission(
        tmp_path,
        "ship-it",
        status="pending",
        phases={
            "plan": _phase("done", usage={"input_tokens": 1000, "output_tokens": 200}),
            "audit": _phase("done", usage={"input_tokens": 500}),
            "reconcile": _phase("gated"),
            "implement": _phase("pending"),
            "review": _phase("pending"),
            "fix": _phase("pending"),
            "verify": _phase("pending"),
            "report": _phase("pending"),
        },
        slices=[
            {
                "id": "s1",
                "title": "auth store",
                "status": "done",
                "review_verdict": "clean",
                "fix_rounds": 0,
                "usage": {"input_tokens": 300},
            }
        ],
        spec_toml=spec_toml,
    )

    missions = mission_store.list_missions()
    assert len(missions) == 1
    mission = missions[0]
    assert mission.slug == "ship-it"
    assert mission.raw_status == "pending"
    assert [p.name for p in mission.phases] == list(mission_store.PHASE_ORDER)
    assert mission.spec is not None
    assert mission.spec.goal == "Ship the mission pane"
    assert mission.spec.gates == ["reconcile"]
    assert mission.spec.models["audit"] == "gpt-5.5"
    assert len(mission.slices) == 1
    assert mission.slices[0].review_verdict == "clean"

    loaded = mission_store.load_mission("ship-it")
    assert loaded is not None
    assert loaded.slug == "ship-it"


def test_malformed_artifact_entries_are_filtered_to_strings(tmp_path):
    # G8: state.json is untrusted. A phase whose "artifacts" list contains
    # non-string junk (numbers, objects, null) must be filtered to strings at
    # parse time so the render/containment path never sees a non-str entry and
    # crashes on `mission_path / rel`. The mission must still parse.
    from helios.backend import mission_store

    phases = {name: _phase("pending") for name in mission_store.PHASE_ORDER}
    phases["plan"] = {
        "status": "done",
        "note": "",
        "usage": {},
        "artifacts": ["artifacts/PLAN.md", 123, {"x": 1}, None, "artifacts/slices.json"],
    }
    _write_mission(tmp_path, "junk-artifacts", status="pending", phases=phases)

    missions = mission_store.list_missions()
    assert len(missions) == 1
    plan = next(p for p in missions[0].phases if p.name == "plan")
    assert plan.artifacts == ["artifacts/PLAN.md", "artifacts/slices.json"]
    assert all(isinstance(a, str) for a in plan.artifacts)


def test_artifacts_non_list_becomes_empty(tmp_path):
    from helios.backend import mission_store

    phases = {name: _phase("pending") for name in mission_store.PHASE_ORDER}
    phases["plan"] = {"status": "done", "note": "", "usage": {}, "artifacts": "PLAN.md"}
    _write_mission(tmp_path, "str-artifacts", status="pending", phases=phases)

    missions = mission_store.list_missions()
    plan = next(p for p in missions[0].phases if p.name == "plan")
    assert plan.artifacts == []


def test_malformed_gates_filtered_to_strings(tmp_path):
    # G8: mission.toml is untrusted. A nested-array gate like [["reconcile"]]
    # must be filtered so `set(spec.gates)` in the render can't raise
    # TypeError: unhashable type: 'list'.
    from helios.backend import mission_store

    spec_toml = 'goal = "g"\nrepo = "/x"\ngates = [["reconcile"], "audit", 5]\n'
    _write_mission(tmp_path, "bad-gates", spec_toml=spec_toml)
    missions = mission_store.list_missions()
    assert missions[0].spec is not None
    assert missions[0].spec.gates == ["audit"]
    # the critical property: gates are hashable (render does set(gates))
    assert set(missions[0].spec.gates) == {"audit"}


def test_malformed_phase_timestamps_coerced_to_none(tmp_path):
    # started_at/ended_at feed `ended_at - started_at` in the render; a
    # non-numeric value from state.json must become None, not crash.
    from helios.backend import mission_store

    phases = {name: _phase("pending") for name in mission_store.PHASE_ORDER}
    phases["plan"] = {
        "status": "done",
        "note": "",
        "usage": {},
        "started_at": "not-a-number",
        "ended_at": ["also", "bad"],
    }
    _write_mission(tmp_path, "bad-ts", status="pending", phases=phases)
    plan = next(p for p in mission_store.list_missions()[0].phases if p.name == "plan")
    assert plan.started_at is None
    assert plan.ended_at is None


def test_non_finite_and_bool_token_values_excluded_from_rollup(tmp_path):
    # json.loads accepts Infinity/NaN; int(inf) later raises OverflowError in
    # the render. Non-finite and bool usage values must be dropped from the
    # rollup so the token formatter only ever sees finite ints.
    import math

    from helios.backend import mission_store

    phases = {name: _phase("pending") for name in mission_store.PHASE_ORDER}
    phases["plan"] = {
        "status": "done",
        "note": "",
        "usage": {"input_tokens": float("inf"), "output_tokens": 200, "flag": True},
    }
    _write_mission(tmp_path, "inf-tokens", status="pending", phases=phases)
    rollup = mission_store.usage_rollup(mission_store.list_missions()[0])
    anthropic = rollup["anthropic"]
    assert "input_tokens" not in anthropic  # inf dropped
    assert "flag" not in anthropic          # bool dropped
    assert anthropic["output_tokens"] == 200
    assert all(math.isfinite(v) for v in anthropic.values())


def test_infinity_from_raw_json_does_not_reach_rollup(tmp_path):
    # Prove it via literal JSON (how it actually arrives on disk), not a
    # Python float("inf") — json.loads parses `Infinity` to a float.
    from helios.backend import mission_store

    mission_dir = tmp_path / "missions" / "raw-inf"
    (mission_dir / "artifacts").mkdir(parents=True)
    state = (
        '{"mission":"raw-inf","created_at":1.0,"status":"done","slices":[],'
        '"phases":{"plan":{"status":"done","usage":{"input_tokens":Infinity}}}}'
    )
    (mission_dir / "state.json").write_text(state, encoding="utf-8")
    rollup = mission_store.usage_rollup(mission_store.list_missions()[0])
    assert "input_tokens" not in rollup["anthropic"]


def test_missing_or_corrupt_state_json_skipped(tmp_path):
    from helios.backend import mission_store

    _write_mission(tmp_path, "good-one", status="done", phases={"plan": _phase("done")})
    _write_mission(tmp_path, "corrupt-one", corrupt_state=True)
    # A dir with no state.json at all.
    empty_dir = tmp_path / "missions" / "no-state"
    empty_dir.mkdir(parents=True)

    missions = mission_store.list_missions()
    slugs = {m.slug for m in missions}
    assert slugs == {"good-one"}
    assert mission_store.load_mission("corrupt-one") is None
    assert mission_store.load_mission("no-state") is None
    assert mission_store.load_mission("does-not-exist") is None


def test_load_mission_rejects_path_traversal_slug(tmp_path):
    from helios.backend import mission_store

    _write_mission(tmp_path, "safe", status="done", phases={})
    assert mission_store.load_mission("../safe") is None
    assert mission_store.load_mission("safe/../../etc") is None
    assert mission_store.load_mission("..") is None


def test_is_safe_slug_rejects_unsafe_values():
    from helios.backend import mission_store

    assert mission_store._is_safe_slug("normal-slug") is True
    assert mission_store._is_safe_slug("") is False
    assert mission_store._is_safe_slug(".") is False
    assert mission_store._is_safe_slug("..") is False
    assert mission_store._is_safe_slug(".hidden") is False
    assert mission_store._is_safe_slug("a/b") is False
    assert mission_store._is_safe_slug("a\\b") is False
    assert mission_store._is_safe_slug("../etc") is False


def test_slug_comes_from_directory_name_not_state_json(tmp_path):
    """[MAJOR — security] The canonical slug MUST be `mission_dir.name`,
    never `state.json`'s `"mission"` field — that field flows straight into
    `tandem mission approve <slug>` (the only mutation path), so trusting
    file content there would let a crafted state.json redirect the approve
    subprocess at an arbitrary CLI argument."""
    from helios.backend import mission_store

    mission_dir = tmp_path / "missions" / "real-dir-name"
    mission_dir.mkdir(parents=True)
    # state.json claims a totally different (and dangerous-looking) slug.
    state = {
        "mission": "../../../etc/pwned; rm -rf /",
        "created_at": time.time(),
        "status": "gated",
        "phases": {},
        "slices": [],
    }
    (mission_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")

    missions = mission_store.list_missions()
    assert len(missions) == 1
    # The malicious "mission" field value must never surface as the slug.
    assert missions[0].slug == "real-dir-name"
    assert "etc/pwned" not in missions[0].slug
    assert "rm -rf" not in missions[0].slug

    loaded = mission_store.load_mission("real-dir-name")
    assert loaded is not None
    assert loaded.slug == "real-dir-name"


def test_unsafe_mission_dir_name_is_skipped(tmp_path):
    """A mission directory whose own name fails `_is_safe_slug` (e.g. a
    dotfile-style dir, or one that somehow got created with a traversal-like
    name) must be skipped entirely by the directory scan, not parsed with a
    fallback slug."""
    from helios.backend import mission_store

    missions_root = tmp_path / "missions"
    missions_root.mkdir(parents=True)

    hidden_dir = missions_root / ".hidden-mission"
    hidden_dir.mkdir()
    (hidden_dir / "state.json").write_text(
        json.dumps({"mission": ".hidden-mission", "status": "pending", "phases": {}, "slices": []}),
        encoding="utf-8",
    )

    _write_mission(tmp_path, "good-mission", status="done", phases={})

    missions = mission_store.list_missions()
    slugs = {m.slug for m in missions}
    assert slugs == {"good-mission"}
    # load_mission must reject it too, via the same shared validator.
    assert mission_store.load_mission(".hidden-mission") is None


def test_tmp_files_ignored_by_list_missions(tmp_path):
    from helios.backend import mission_store

    _write_mission(tmp_path, "real-mission", status="done", phases={})
    # A stray *.tmp mission-level dir (mirrors the state.json.tmp sibling
    # convention) must never be treated as a real mission.
    tmp_dir = tmp_path / "missions" / "real-mission.tmp"
    tmp_dir.mkdir(parents=True)
    (tmp_dir / "state.json").write_text(json.dumps({"mission": "real-mission.tmp"}))

    missions = mission_store.list_missions()
    slugs = {m.slug for m in missions}
    assert slugs == {"real-mission"}


def test_is_quota_stop_classification():
    from helios.backend import mission_store

    quota_phase = mission_store.MissionPhase(
        name="implement", status="failed", note="quota: 5h limit hit"
    )
    real_failure = mission_store.MissionPhase(
        name="implement", status="failed", note="test command exited 1"
    )
    not_failed = mission_store.MissionPhase(name="implement", status="done", note="quota: stale note")

    assert mission_store.is_quota_stop(quota_phase) is True
    assert mission_store.is_quota_stop(real_failure) is False
    assert mission_store.is_quota_stop(not_failed) is False


@pytest.mark.parametrize(
    "phases,expected",
    [
        ({"plan": _phase("done"), "audit": _phase("running")}, "running"),
        ({"plan": _phase("done"), "reconcile": _phase("gated")}, "gated"),
        (
            {"plan": _phase("done"), "implement": _phase("failed", note="quota: hit 5h limit")},
            "quota-stopped",
        ),
        ({"plan": _phase("done"), "implement": _phase("failed", note="boom")}, "failed"),
        ({"plan": _phase("done"), "audit": _phase("done")}, None),  # falls through to raw_status
    ],
)
def test_derived_state_precedence(tmp_path, phases, expected):
    from helios.backend import mission_store

    _write_mission(tmp_path, "m", status="pending", phases=phases)
    mission = mission_store.load_mission("m")
    assert mission is not None
    result = mission_store.derived_state(mission)
    if expected is None:
        assert result == "pending"
    else:
        assert result == expected


def test_derived_state_running_beats_gated():
    from helios.backend import mission_store

    mission = mission_store.Mission(
        slug="x",
        path=None,  # type: ignore[arg-type]
        raw_status="pending",
        phases=[
            mission_store.MissionPhase(name="plan", status="gated"),
            mission_store.MissionPhase(name="audit", status="running"),
        ],
    )
    assert mission_store.derived_state(mission) == "running"


def test_gated_phase_returns_the_gated_one():
    from helios.backend import mission_store

    mission = mission_store.Mission(
        slug="x",
        path=None,  # type: ignore[arg-type]
        raw_status="gated",
        phases=[
            mission_store.MissionPhase(name="plan", status="done"),
            mission_store.MissionPhase(name="reconcile", status="gated"),
        ],
    )
    phase = mission_store.gated_phase(mission)
    assert phase is not None
    assert phase.name == "reconcile"

    no_gate = mission_store.Mission(
        slug="y",
        path=None,  # type: ignore[arg-type]
        raw_status="pending",
        phases=[mission_store.MissionPhase(name="plan", status="done")],
    )
    assert mission_store.gated_phase(no_gate) is None


def test_usage_rollup_never_sums_providers(tmp_path):
    """G11: codex input_tokens are cached-inclusive, Claude's are additive —
    the rollup must always keep them in separate provider buckets, never
    combined into one grand total."""
    from helios.backend import mission_store

    _write_mission(
        tmp_path,
        "usage-mission",
        status="pending",
        phases={
            "plan": _phase("done", usage={"input_tokens": 1000, "output_tokens": 200}),
            "implement": _phase("done", usage={"input_tokens": 2000, "output_tokens": 400}),
            "audit": _phase("done", usage={"input_tokens": 5000, "cached_input_tokens": 4000}),
            "review": _phase("done", usage={"input_tokens": 3000, "cached_input_tokens": 1000}),
        },
        slices=[
            {"id": "s1", "usage": {"input_tokens": 100}, "review_verdict": ""},
            {"id": "s2", "usage": {"input_tokens": 50}, "review_verdict": "clean"},
        ],
    )
    mission = mission_store.load_mission("usage-mission")
    assert mission is not None
    rollup = mission_store.usage_rollup(mission)

    assert set(rollup.keys()) == {"anthropic", "openai"}
    # Claude (plan + implement + slice s1, unreviewed -> anthropic bucket)
    assert rollup["anthropic"]["input_tokens"] == 1000 + 2000 + 100
    assert rollup["anthropic"]["output_tokens"] == 200 + 400
    # codex (audit + review + slice s2, has review_verdict -> openai bucket)
    assert rollup["openai"]["input_tokens"] == 5000 + 3000 + 50
    assert rollup["openai"]["cached_input_tokens"] == 4000 + 1000
    # Never combined.
    assert "input_tokens" not in rollup
    total_keys = set(rollup["anthropic"]) | set(rollup["openai"])
    assert "anthropic+openai" not in total_keys


def test_usage_rollup_empty_mission_is_all_empty_dicts():
    from helios.backend import mission_store

    mission = mission_store.Mission(slug="x", path=None, raw_status="pending")  # type: ignore[arg-type]
    rollup = mission_store.usage_rollup(mission)
    assert rollup == {"anthropic": {}, "openai": {}}


def test_tandem_activity_window_filtering(tmp_path):
    from helios.backend import mission_store

    now = time.time()
    log_path = tmp_path / "log.jsonl"
    rows = [
        {"ts": now - 60, "model": "opus", "cmd": "mission/x/plan"},
        {"ts": now - 3600, "model": "opus", "cmd": "mission/x/plan"},
        {"ts": now - 6 * 3600, "model": "gpt-5.5", "cmd": "mission/x/audit"},  # outside 5h window
        {"ts": now - 100, "model": "gpt-5.5", "cmd": "mission/x/audit"},
    ]
    log_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    counts = mission_store.tandem_activity(hours=5.0)
    assert counts == {"opus": 2, "gpt-5.5": 1}


def test_tandem_activity_tolerates_missing_file_and_malformed_lines(tmp_path):
    from helios.backend import mission_store

    # No log.jsonl at all.
    assert mission_store.tandem_activity() == {}

    log_path = tmp_path / "log.jsonl"
    now = time.time()
    lines = [
        json.dumps({"ts": now, "model": "opus"}),
        "{not valid json at all",
        "",
        json.dumps({"ts": now, "no_model_field": True}),
        json.dumps([1, 2, 3]),  # valid json, not a dict
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    counts = mission_store.tandem_activity()
    assert counts == {"opus": 1}
