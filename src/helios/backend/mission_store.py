"""Read-only data layer for the Missions pane — the `~/.tandem/` on-disk contract.

`tandem` (the estate's tandem repository) is the headless dual-engine mission orchestrator. Its
state lives entirely on disk under ``~/.tandem/`` (overridable via
``$TANDEM_STATE_DIR``, read at *call* time — G12) and is written by external
processes: a `tandem mission run` in a terminal, cron, or another session.
Helios never writes to this tree; this module only parses it. The one
mutation path in the whole feature is the Missions pane's Approve button,
which shells out to `tandem mission approve <slug>` (see `mission_pane.py`) —
never a direct file write.

This module is a GTK-free seam (mirrors `plan_summary.py`'s role for
PlanPane): it must import with no `gi` dependency because CI runs the test
suite in a `python:3.13-slim` container that has no GTK stack installed at
all (see `tests/test_mission_store.py::test_no_gi_import`).

Facts this module encodes, verified against the tandem repository `main@85097be`
(source file:line cites — see MISSION-PANE-PLAN.md §3 for the full table):

  * G1  — the mission spec is TOML (`mission.toml`, spec.py:45-51), parsed
          with the stdlib `tomllib` module. There is no YAML anywhere.
  * G2  — there is no standalone "gate" phase. Gating flips the *gated
          phase's own* status to `"gated"` (engine.py:190-199) and the
          mission's overall status to `"gated"` too.
  * G3  — quota stops are recorded as a normal phase `status="failed"` with
          a `note` prefixed literally `"quota: "` (engine.py:181-182). There
          is no separate "quota" status value anywhere in the schema.
  * G4  — the mission's overall `status` field essentially never becomes
          `"running"` or `"failed"` in practice (engine.py only ever writes
          `pending -> gated -> pending -> done / aborted` for it), so a
          useful "what is this mission doing right now" label has to be
          *derived* from the phase list, not read off `status` directly.
  * G5  — the atomic writer for `state.json` writes to a sibling file named
          literally `state.json.tmp` in the same directory, then
          `os.replace()`s it into place (state.py:169-174). Because of the
          rename-based swap, `state.json` itself can never be torn-read; a
          file-monitor consumer just needs to ignore `*.tmp` change events
          so it doesn't try to parse the temp file mid-write.
  * G6  — `artifacts/slices.json` is a point-in-time snapshot written once
          by the plan phase; it drifts out of date as soon as
          implement/review/fix mutate slice records in place
          (engine.py:225-228, 281-283). `state.json` is the sole live
          source of truth for slice state — this module never reads
          `slices.json`.
  * G7  — `review_verdict` is produced by a plain substring heuristic
          (`"NO FINDINGS" in text.upper()`, engine.py:386) and must be
          treated as advisory, not authoritative. `"escalate"` only ever
          appears in a slice's `review_verdict` field, never in its
          `status` field, and means `max_fix_rounds` was exhausted.
  * G8  — mission slugs are unvalidated raw CLI arguments joined straight
          into filesystem paths (mission/cli.py:52). Callers of this module
          must treat every slug as untrusted text (escape before rendering
          as markup) and this module itself must never raise on an
          unparseable or partially-written `state.json` — such a mission
          directory is simply skipped.
  * G11 — usage dict shapes differ by provider and must never be summed
          together: codex's `input_tokens` is *inclusive* of
          `cached_input_tokens` (codex_events.py:198), while Claude's usage
          counters are additive (cli_driver.py:781-823, the "7M/1M meter
          bug" comment). `usage_rollup()` therefore keys its output by
          provider (`"anthropic"` / `"openai"`) and never adds the two
          together into a combined total.
  * G12 — `$TANDEM_STATE_DIR` overrides the state root and is read at call
          time (logbook.py:16-18), not cached at import time — this is what
          makes the whole module testable with `tmp_path` fixtures via
          `monkeypatch.setenv("TANDEM_STATE_DIR", ...)`.

All parsing here is defensive by design: `json.loads` / `tomllib.load` calls
are wrapped in try/except and degrade to `None`/skip rather than raising, so
a mission mid-write, a corrupt file, or a future schema addition never
crashes the pane.
"""

from __future__ import annotations

import json
import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Fixed phase order the tandem engine always writes (state.py). Rendered as
# 8 fixed rows in the pane regardless of what's actually present in a given
# state.json, so the pipeline shape never jumps around.
PHASE_ORDER: tuple[str, ...] = (
    "plan",
    "audit",
    "reconcile",
    "implement",
    "review",
    "fix",
    "verify",
    "report",
)

# Which engine (for G11's usage-rollup provider split) is responsible for
# each phase. Slices don't carry a phase name directly, so slice usage is
# rolled into whichever provider ran review/fix — see `usage_rollup()`.
_PHASE_PROVIDER: dict[str, str] = {
    "plan": "anthropic",
    "reconcile": "anthropic",
    "implement": "anthropic",
    "fix": "anthropic",
    "audit": "openai",
    "review": "openai",
}

_QUOTA_NOTE_PREFIX = "quota: "


def tandem_root() -> Path:
    """Root of the tandem state tree — `$TANDEM_STATE_DIR` or `~/.tandem`.

    Read at *call* time (G12), never cached, so tests can redirect it per-run
    with `monkeypatch.setenv("TANDEM_STATE_DIR", ...)` and production code
    picks up a change without a restart.
    """
    override = os.environ.get("TANDEM_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tandem"


def missions_dir() -> Path:
    return tandem_root() / "missions"


def log_path() -> Path:
    return tandem_root() / "log.jsonl"


@dataclass(slots=True)
class MissionPhase:
    name: str
    status: str = "pending"  # pending | running | done | failed | gated | aborted
    started_at: float | None = None
    ended_at: float | None = None
    session_ref: str = ""
    artifacts: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(slots=True)
class MissionSlice:
    id: str = ""
    title: str = ""
    spec: str = ""
    status: str = ""
    session_ref: str = ""
    worktree: str = ""
    branch: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    review_verdict: str = ""  # '' | clean | findings | escalate (G7)
    fix_rounds: int = 0
    test_result: str = ""


@dataclass(slots=True)
class MissionSpecLite:
    goal: str = ""
    repo: str = ""
    base_branch: str = "main"
    test_command: str = ""
    gates: list[str] = field(default_factory=list)
    models: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class Mission:
    slug: str
    path: Path
    created_at: float | None = None
    raw_status: str = "pending"
    phases: list[MissionPhase] = field(default_factory=list)
    slices: list[MissionSlice] = field(default_factory=list)
    spec: MissionSpecLite | None = None


def _is_safe_slug(s: str) -> bool:
    """G8: mission slugs are unvalidated raw CLI args joined into filesystem
    paths by the tandem CLI itself, and the *only* mutation path in this
    whole feature (`tandem mission approve <slug>`) takes a slug straight
    from this module. The canonical slug MUST always be the mission
    directory's own name — never a value read out of `state.json`, which is
    attacker/bug-controllable content sitting inside that same directory.
    This validator is the single shared gate for both the directory-scan
    path (`_parse_mission_dir`) and the by-name lookup path
    (`load_mission`)."""
    return bool(s) and "/" not in s and "\\" not in s and s not in (".", "..") and not s.startswith(".")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _as_float(v: Any) -> float | None:
    """Coerce an untrusted state.json numeric field to float|None. A bool,
    string, list, or object becomes None rather than reaching arithmetic
    (e.g. `ended_at - started_at`) and crashing the render."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _phase_from_dict(name: str, data: dict[str, Any]) -> MissionPhase:
    artifacts = data.get("artifacts")
    usage = data.get("usage")
    # G8: artifact entries are untrusted state.json content — keep only strings
    # so a malformed entry (e.g. [123] or an object) can never reach the
    # render/containment path and crash it with a TypeError.
    safe_artifacts = (
        [a for a in artifacts if isinstance(a, str)]
        if isinstance(artifacts, list)
        else []
    )
    return MissionPhase(
        name=name,
        status=str(data.get("status", "pending")),
        started_at=_as_float(data.get("started_at")),
        ended_at=_as_float(data.get("ended_at")),
        session_ref=str(data.get("session_ref", "")),
        artifacts=safe_artifacts,
        usage=dict(usage) if isinstance(usage, dict) else {},
        note=str(data.get("note", "")),
    )


def _slice_from_dict(data: dict[str, Any]) -> MissionSlice:
    usage = data.get("usage")
    fix_rounds = data.get("fix_rounds", 0)
    try:
        fix_rounds = int(fix_rounds)
    except (TypeError, ValueError):
        fix_rounds = 0
    return MissionSlice(
        id=str(data.get("id", "")),
        title=str(data.get("title", "")),
        spec=str(data.get("spec", "")),
        status=str(data.get("status", "")),
        session_ref=str(data.get("session_ref", "")),
        worktree=str(data.get("worktree", "")),
        branch=str(data.get("branch", "")),
        usage=dict(usage) if isinstance(usage, dict) else {},
        review_verdict=str(data.get("review_verdict", "")),
        fix_rounds=fix_rounds,
        test_result=str(data.get("test_result", "")),
    )


def _spec_from_dict(data: dict[str, Any]) -> MissionSpecLite:
    gates = data.get("gates")
    models = data.get("models")
    return MissionSpecLite(
        goal=str(data.get("goal", "")),
        repo=str(data.get("repo", "")),
        base_branch=str(data.get("base_branch", "main")),
        test_command=str(data.get("test_command", "")),
        # G8: gates come from an untrusted mission.toml array. Keep only
        # strings — a nested array like [["reconcile"]] would otherwise reach
        # `set(spec.gates)` in the render and raise TypeError (unhashable).
        gates=[g for g in gates if isinstance(g, str)] if isinstance(gates, list) else [],
        models=dict(models) if isinstance(models, dict) else {},
    )


def _parse_mission_dir(mission_dir: Path) -> Mission | None:
    """Parse one `missions/<slug>/` directory. Returns None (skip) rather
    than raising for anything missing/unparseable (G8) — an untrusted or
    half-written mission directory must never crash the pane.

    SECURITY: the canonical slug is always `mission_dir.name`, never the
    `state.json` `"mission"` field. `state.json` is untrusted content that
    can be edited independent of the directory it sits in, and the slug
    parsed here is what eventually reaches `tandem mission approve <slug>`
    (the pane's one mutation path) — trusting file content for that value
    would let a crafted `state.json` redirect the approve subprocess at an
    arbitrary CLI argument. If the directory name itself isn't a safe slug,
    the whole directory is skipped rather than parsed.
    """
    if not _is_safe_slug(mission_dir.name):
        return None

    state_path = mission_dir / "state.json"
    state = _read_json(state_path)
    if state is None:
        return None

    slug = mission_dir.name

    phases_raw = state.get("phases")
    phases: list[MissionPhase] = []
    if isinstance(phases_raw, dict):
        # Preserve the fixed pipeline order regardless of dict insertion
        # order (json preserves insertion order, but don't rely on it).
        seen = set()
        for name in PHASE_ORDER:
            if name in phases_raw and isinstance(phases_raw[name], dict):
                phases.append(_phase_from_dict(name, phases_raw[name]))
                seen.add(name)
        for name, data in phases_raw.items():
            if name not in seen and isinstance(data, dict):
                phases.append(_phase_from_dict(str(name), data))
    elif isinstance(phases_raw, list):
        for entry in phases_raw:
            if isinstance(entry, dict) and "name" in entry:
                phases.append(_phase_from_dict(str(entry["name"]), entry))

    slices_raw = state.get("slices")
    slices: list[MissionSlice] = []
    if isinstance(slices_raw, list):
        for entry in slices_raw:
            if isinstance(entry, dict):
                slices.append(_slice_from_dict(entry))
    elif isinstance(slices_raw, dict):
        for _key, entry in slices_raw.items():
            if isinstance(entry, dict):
                slices.append(_slice_from_dict(entry))

    spec_data = _read_toml(mission_dir / "mission.toml")
    spec = _spec_from_dict(spec_data) if spec_data is not None else None

    created_at = state.get("created_at")
    if not isinstance(created_at, (int, float)):
        created_at = None

    return Mission(
        slug=slug,
        path=mission_dir,
        created_at=created_at,
        raw_status=str(state.get("status", "pending")),
        phases=phases,
        slices=slices,
        spec=spec,
    )


def list_missions() -> list[Mission]:
    """Scan `missions/*/state.json`, skipping anything unparseable (G8).

    Never raises — a missing `~/.tandem/missions/` directory (tandem never
    run yet) simply yields an empty list.
    """
    root = missions_dir()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    missions: list[Mission] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        # Defensive: ignore any stray *.tmp path even at the mission-dir
        # level, matching the file-monitor's tmp-ignore rule (G5).
        if entry.name.endswith(".tmp"):
            continue
        mission = _parse_mission_dir(entry)
        if mission is not None:
            missions.append(mission)
    return missions


def load_mission(slug: str) -> Mission | None:
    """Load a single mission by slug. `slug` is treated as untrusted text
    (G8) — it is joined onto `missions_dir()` but never shelled out or used
    to build markup without escaping by the caller. Uses the same
    `_is_safe_slug` gate as `_parse_mission_dir` so both entry points reject
    path traversal and other unsafe names identically."""
    if not _is_safe_slug(slug):
        return None
    mission_dir = missions_dir() / slug
    if not mission_dir.is_dir():
        return None
    return _parse_mission_dir(mission_dir)


def is_quota_stop(phase: MissionPhase) -> bool:
    """G3: quota stops are `status == "failed"` with `note` prefixed
    literally `"quota: "` — there is no dedicated quota status."""
    return phase.status == "failed" and phase.note.startswith(_QUOTA_NOTE_PREFIX)


def derived_state(mission: Mission) -> str:
    """Derive a human mission state from the phase list (G4): the top-level
    `status` field essentially never becomes "running"/"failed" in practice,
    so the pane must infer these from phase statuses instead.

    Precedence: running > gated > quota-stopped > failed > overall status.
    """
    statuses = [p.status for p in mission.phases]
    if any(s == "running" for s in statuses):
        return "running"
    if any(s == "gated" for s in statuses):
        return "gated"
    if any(is_quota_stop(p) for p in mission.phases):
        return "quota-stopped"
    if any(s == "failed" for s in statuses):
        return "failed"
    return mission.raw_status


def gated_phase(mission: Mission) -> MissionPhase | None:
    """The phase currently holding the gate, if any (G2: gating is a phase
    status, not a standalone pipeline step)."""
    for phase in mission.phases:
        if phase.status == "gated":
            return phase
    return None


def _merge_usage(total: dict[str, int], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        # Untrusted state.json: skip bool (isinstance(True, int)) and
        # non-finite floats (json.loads accepts `1e10000` -> inf, and
        # int(inf) later raises OverflowError in the render).
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        total[key] = total.get(key, 0) + value


def usage_rollup(mission: Mission) -> dict[str, dict[str, int]]:
    """Per-provider usage totals (G11) — Claude's additive counters and
    codex's cached-inclusive `input_tokens` must never be summed together,
    so the result is always keyed by provider (`"anthropic"` / `"openai"`),
    never combined into one grand total.

    Phase usage is attributed by `_PHASE_PROVIDER`. Slice usage (implement is
    Claude, review is codex) is split the same way: implement/fix rounds are
    Claude, review passes are codex — since a single `MissionSlice` record
    doesn't carry a separate breakdown, all of a slice's `usage` dict is
    attributed to codex if it has a `review_verdict` set (it went through
    review), otherwise to Claude. Unknown/unattributable phase names are
    skipped rather than guessed at.
    """
    rollup: dict[str, dict[str, int]] = {"anthropic": {}, "openai": {}}
    for phase in mission.phases:
        provider = _PHASE_PROVIDER.get(phase.name)
        if provider is None or not phase.usage:
            continue
        _merge_usage(rollup[provider], phase.usage)
    for sl in mission.slices:
        if not sl.usage:
            continue
        provider = "openai" if sl.review_verdict else "anthropic"
        _merge_usage(rollup[provider], sl.usage)
    return rollup


def tandem_activity(hours: float = 5.0) -> dict[str, int]:
    """Tail `log.jsonl` and count calls per model within the trailing
    `hours` window. Tolerates a missing file and malformed/partial lines —
    the log is append-only but a reader mid-write could see a truncated
    final line.
    """
    path = log_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    import time

    cutoff = time.time() - hours * 3600.0
    counts: dict[str, int] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        model = row.get("model")
        if not model:
            continue
        model = str(model)
        counts[model] = counts.get(model, 0) + 1
    return counts
