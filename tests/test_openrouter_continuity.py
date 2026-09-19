"""Real store persistence and zero-inference context survival."""

import json
import stat
from types import SimpleNamespace

import pytest

from helios.backend.openrouter.continuity import CAPSULE_PREFIX, ContextArchive
from helios.backend.openrouter.history import HistoryLog, compact, estimate_tokens
from helios.backend.process.openrouter_tools import ToolFailed, execution_plan_payload
from helios.backend.work_coordinator import WorkCoordinator, tag_driver
from helios.backend.work_store import WorkStore


def test_openrouter_plan_progress_survives_store_reopen_and_compaction(tmp_path):
    path = tmp_path / "work.db"
    store = WorkStore(path)
    coordinator = WorkCoordinator(store)
    work = coordinator.ensure_work(cwd=str(tmp_path), lead_provider="openrouter")
    participant = coordinator.bind_participant(work.work_id, "openrouter", native_id="or-session")
    driver = SimpleNamespace()
    tag_driver(driver, participant)
    first = execution_plan_payload(
        {"plan": [{"step": "Inspect", "status": "in_progress"},
                  {"step": "Verify", "status": "pending"}]},
        thread_id="or-session", turn_id="gen-first-response",
    )
    initial = coordinator.record_execution_plan(driver, first)
    assert initial is not None  # the original missing-turnId reproduction
    revised = execution_plan_payload(
        {"plan": [{"step": "Inspect", "status": "completed"},
                  {"step": "Verify", "status": "in_progress"}]},
        thread_id="or-session", turn_id="gen-first-response",
    )
    progress = coordinator.record_execution_plan(driver, revised)
    assert progress.completed_count == 1
    assert [step.task_id for step in progress.steps] == [step.task_id for step in initial.steps]
    compact(_messages(), max_tokens=256, archive=ContextArchive("or-session"))
    store.close()
    reopened = WorkStore(path)
    assert reopened.get_execution_plan(work.work_id) == progress
    assert len(reopened.list_execution_plan_revisions(work.work_id)) == 2
    reopened.close()


@pytest.mark.parametrize("arguments", [
    {"plan": []}, {"plan": [{"step": "x", "status": "done-maybe"}]},
    {"plan": [{"step": "x", "status": "completed"}] * 51},
    {"plan": [{"step": "x", "status": "in_progress"}, {"step": "y", "status": "in_progress"}]},
])
def test_invalid_progress_never_becomes_a_false_plan(arguments):
    with pytest.raises(ToolFailed):
        execution_plan_payload(arguments, thread_id="session", turn_id="turn")


def test_plan_cannot_invent_an_unaccepted_turn_identity():
    with pytest.raises(ToolFailed, match="identity"):
        execution_plan_payload({"plan": [{"step": "x", "status": "pending"}]},
                               thread_id="session", turn_id="")


def _messages():
    return [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Required prior decision: use PostgreSQL, never SQLite. " + "context " * 500},
        {"role": "assistant", "content": "Confirmed."},
        {"role": "user", "content": "Continue implementing the earlier design."},
    ]


def test_original_constraint_reproduction_survives_even_tiny_compaction():
    messages = _messages()
    trimmed, dropped = compact(messages, max_tokens=150, archive=ContextArchive("tiny"))
    assert dropped == 2
    assert estimate_tokens(trimmed) <= 150
    assert "use PostgreSQL, never SQLite" in str(trimmed)
    assert not any("PostgreSQL" in str(message) for message in trimmed if message["role"] == "system")


def test_repeated_compaction_and_resume_keep_sources_and_bounded_capsule():
    archive = ContextArchive("resume")
    messages, first = compact(_messages(), max_tokens=256, archive=archive)
    correction = "Correction: use port 6543, not 5432."
    messages += [{"role": "assistant", "content": "evidence " * 1000},
                 {"role": "user", "content": correction},
                 {"role": "assistant", "content": "evidence " * 1000},
                 {"role": "user", "content": "Continue"}]
    twice, second = compact(messages, max_tokens=1024, archive=archive)
    HistoryLog("resume").save_strict(twice)
    resumed = HistoryLog("resume").load()
    assert "PostgreSQL" in str(resumed)
    assert correction in str(resumed)
    assert sum(str(row.get("content", "")).startswith(CAPSULE_PREFIX) for row in resumed) == 1
    assert estimate_tokens(resumed) <= 1024
    assert first > 0 and second > 0
    recovered = json.loads(ContextArchive("resume").read({"query": "PostgreSQL"}))
    assert recovered["records"][0]["role"] == "user"
    assert "PostgreSQL" in recovered["records"][0]["text"]


def test_capsule_discloses_omissions_and_full_source_remains_pageable():
    archive = ContextArchive("large")
    messages = _messages()
    messages[1]["content"] += " exact final constraint: use UUID identifiers."
    trimmed, _ = compact(messages, max_tokens=150, archive=archive)
    retained = next(row for row in trimmed if str(row.get("content", "")).startswith(CAPSULE_PREFIX))
    payload = json.loads(retained["content"][len(CAPSULE_PREFIX):])
    assert payload["omitted_user_characters"] > 0
    response = json.loads(archive.read({"query": "UUID", "limit": 1}))
    pieces = response["records"]
    while response["next_offset"] is not None:
        response = json.loads(archive.read({"query": "UUID", "limit": 1, "offset": response["next_offset"]}))
        pieces += response["records"]
    assert "use UUID identifiers" in "".join(item["text"] for item in pieces)


def test_archive_is_private_atomic_session_scoped_and_omits_reasoning(tmp_path, monkeypatch):
    archive = ContextArchive("first")
    original = [{"role": "assistant", "content": "public", "reasoning_details": [{"text": "private"}]}]
    observed_modes = []
    from helios.backend.openrouter import continuity
    replace = continuity.os.replace
    def inspect_mode(source, destination):
        observed_modes.append(stat.S_IMODE(continuity.os.stat(source).st_mode))
        replace(source, destination)
    monkeypatch.setattr(continuity.os, "replace", inspect_mode)
    archive.append(original)
    assert observed_modes == [0o600]
    assert stat.S_IMODE(archive.path.stat().st_mode) == 0o600
    assert "private" not in archive.read({})
    assert json.loads(ContextArchive("second").read({}))["records"] == []
    with pytest.raises(ValueError, match="paths"):
        archive.read({"path": str(tmp_path / "secret")})
    with pytest.raises(ValueError):
        archive.read({"offset": -1})


def test_failed_archive_write_never_discards_original_history(monkeypatch):
    archive = ContextArchive("write-fails")
    messages = _messages()
    before = json.dumps(messages)
    monkeypatch.setattr(archive, "_save", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        compact(messages, max_tokens=150, archive=archive)
    assert json.dumps(messages) == before


def test_checkpoint_is_bounded_and_remains_model_claims():
    archive = ContextArchive("checkpoint")
    archive.checkpoint("Verified parser tests. Unresolved: database migration.")
    out, _ = compact(_messages(), max_tokens=2048, archive=archive)
    # The long source may fit this budget; the recovery channel still retains
    # the explicit model checkpoint and never labels it as a user constraint.
    assert json.loads(archive.read({}))["model_checkpoint_unverified"].startswith("Verified")
    assert not any("Verified parser" in str(row) for row in out if row["role"] == "system")
    with pytest.raises(ValueError, match="4000"):
        archive.checkpoint("x" * 4001)


def test_symlinked_archive_is_not_a_session_file(tmp_path):
    archive = ContextArchive("symlink")
    archive.path.parent.mkdir(parents=True)
    external = tmp_path / "outside.json"
    external.write_text('{"batches": [], "checkpoint": "secret"}')
    archive.path.symlink_to(external)
    with pytest.raises(OSError, match="symlink"):
        archive.read({})


def test_archive_uses_history_retention_policy():
    from helios.backend.openrouter.history import prune
    archive = ContextArchive("pruned")
    archive.append([{"role": "user", "content": "old context"}])
    HistoryLog("pruned").save_strict([])
    assert archive.path.exists()
    assert prune(keep=0) == 1
    assert not archive.path.exists()


def test_fifo_archive_is_rejected_without_waiting_for_a_writer():
    import os
    archive = ContextArchive("fifo")
    archive.path.parent.mkdir(parents=True)
    os.mkfifo(archive.path)
    with pytest.raises(OSError, match="regular file"):
        archive.load()


@pytest.mark.parametrize("value", [{"batches": [None]}, {"batches": [{"id": "bad", "messages": []}]},
                                  {"batches": [], "checkpoint": {"not": "text"}}])
def test_corrupt_archive_shape_fails_before_compaction(value):
    archive = ContextArchive("corrupt")
    archive.path.parent.mkdir(parents=True)
    archive.path.write_text(json.dumps(value))
    messages = _messages()
    before = json.dumps(messages)
    with pytest.raises(ValueError, match="archive"):
        compact(messages, max_tokens=150, archive=archive)
    assert json.dumps(messages) == before


def test_archive_size_bound_preserves_replay_and_existing_archive(monkeypatch):
    from helios.backend.openrouter import continuity
    archive = ContextArchive("bounded")
    archive.append([{"role": "user", "content": "keep this"}])
    before_archive = archive.path.read_bytes()
    monkeypatch.setattr(continuity, "MAX_ARCHIVE_BYTES", len(before_archive) + 1)
    messages = _messages()
    before = json.dumps(messages)
    with pytest.raises(ValueError, match="new Work"):
        compact(messages, max_tokens=150, archive=archive)
    assert json.dumps(messages) == before
    assert archive.path.read_bytes() == before_archive
    archive.path.write_bytes(b"x" * (len(before_archive) + 2))
    with pytest.raises(ValueError, match="64 MiB"):
        archive.load()


def test_compaction_reads_archive_once_for_candidates_then_once_to_commit(monkeypatch):
    archive = ContextArchive("once")
    archive.append([{"role": "user", "content": "initial requirement"}])
    reads = []
    load = archive.load
    def counted_load():
        reads.append(True)
        return load()
    monkeypatch.setattr(archive, "load", counted_load)
    messages = [{"role": "system", "content": "policy"}]
    for index in range(10):
        messages += [{"role": "user", "content": f"turn {index}"},
                     {"role": "assistant", "content": "evidence " * 1000}]
    compact(messages, max_tokens=100, archive=archive)
    assert len(reads) == 2
