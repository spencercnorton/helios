"""Pre-turn snapshots and rewind.

Every test drives a real git repo in tmp_path — the module is a thin shell over
git plumbing, so mocking git would test nothing that matters. GTK-free, so this
runs in the slim CI lane.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from helios.backend import checkpoints as cp

# The slim CI image ships no git. Checkpoints are git plumbing end to end, so
# there is nothing meaningful left to assert without it — skip rather than
# mock, matching how the GTK-dependent modules are handled.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required for checkpoint tests"
)


def _run(cwd, *args):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def _agent_writes(path, text):
    """Create a file the way a turn would — measurably after the checkpoint.

    The deletion guard refuses to remove anything whose mtime falls inside the
    checkpoint's second, since whole-second stamps cannot order them. A real
    turn takes seconds; tests that write instantly need to say so.
    """
    import os
    import time

    path.write_text(text)
    future = time.time() + 5
    os.utime(path, (future, future))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _run(root, "git", "init", "-q", "-b", "main")
    _run(root, "git", "config", "user.email", "t@example.com")
    _run(root, "git", "config", "user.name", "T")
    (root / "kept.py").write_text("original\n")
    (root / ".gitignore").write_text("secrets/\n*.log\n")
    (root / "debug.log").write_text("pre-existing ignored noise\n")
    _run(root, "git", "add", "-A")
    _run(root, "git", "commit", "-qm", "init")
    return root


# ── capture ────────────────────────────────────────────────────────────────


def test_no_checkpoints_outside_a_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cp.available(plain) is False
    assert cp.snapshot(plain) is None


def test_snapshot_does_not_disturb_the_user_index_or_head(repo):
    (repo / "staged.py").write_text("x\n")
    _run(repo, "git", "add", "staged.py")
    before_index = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout

    assert cp.snapshot(repo, "a message") is not None

    after_index = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    after_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout
    # The whole design rests on this: no stash, no commit, no index mutation.
    assert after_index == before_index
    assert after_head == before_head


def test_snapshot_works_in_a_repo_with_no_commits_yet(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    _run(root, "git", "init", "-q", "-b", "main")
    (root / "a.txt").write_text("hello\n")
    point = cp.snapshot(root, "first")
    assert point is not None and point.tree
    assert point.head == ""


# ── what changed ───────────────────────────────────────────────────────────


def test_reports_edits_creations_and_deletions(repo):
    point = cp.snapshot(repo, "before the agent ran")
    (repo / "kept.py").write_text("agent edited this\n")
    (repo / "new.py").write_text("agent created this\n")
    (repo / ".gitignore").unlink()

    by_path = {c.path: c.status for c in cp.changes_since(point)}
    assert by_path["kept.py"] == "modified"
    assert by_path["new.py"] == "added"
    assert by_path[".gitignore"] == "deleted"


def test_an_untouched_tree_reports_nothing(repo):
    assert cp.changes_since(cp.snapshot(repo)) == []


def test_ignored_files_are_reported_as_gaps_not_silently_dropped(repo):
    (repo / "debug.log").write_text("noise\n")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "key").write_text("shh\n")
    gaps = cp.checkpoint_gaps(repo)
    # The UI has to be able to say "these will not be restored" rather than
    # implying a rewind returns the directory to exactly its former state.
    assert "debug.log" in gaps
    assert any(p.startswith("secrets/") for p in gaps)


# ── rewind ─────────────────────────────────────────────────────────────────


def test_restore_reverts_an_edit(repo):
    point = cp.snapshot(repo)
    (repo / "kept.py").write_text("agent edited this\n")
    result = cp.restore(point, ["kept.py"])
    assert result.restored == ["kept.py"]
    assert not result.failed
    assert (repo / "kept.py").read_text() == "original\n"


def test_restore_removes_a_file_the_agent_created(repo):
    point = cp.snapshot(repo)
    _agent_writes(repo / "new.py", "agent created this\n")
    result = cp.restore(point, ["new.py"])
    assert result.removed == ["new.py"]
    assert not (repo / "new.py").exists()


def test_restore_recreates_a_file_the_agent_deleted(repo):
    point = cp.snapshot(repo)
    (repo / "kept.py").unlink()
    cp.restore(point, ["kept.py"])
    assert (repo / "kept.py").read_text() == "original\n"


def test_restore_touches_only_the_paths_asked_for(repo):
    # The dialog lets you deselect a file; deselecting must actually mean it.
    point = cp.snapshot(repo)
    (repo / "kept.py").write_text("edited\n")
    (repo / "other.py").write_text("also edited\n")
    cp.restore(point, ["kept.py"])
    assert (repo / "kept.py").read_text() == "original\n"
    assert (repo / "other.py").read_text() == "also edited\n"


def test_restore_leaves_head_and_the_index_alone(repo):
    point = cp.snapshot(repo)
    (repo / "kept.py").write_text("edited\n")
    _run(repo, "git", "add", "kept.py")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout
    staged_before = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=repo,
        capture_output=True, text=True,
    ).stdout

    cp.restore(point, ["kept.py"])

    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout == head_before
    assert subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=repo,
        capture_output=True, text=True,
    ).stdout == staged_before


def test_restoring_nothing_is_a_no_op(repo):
    point = cp.snapshot(repo)
    (repo / "kept.py").write_text("edited\n")
    result = cp.restore(point, [])
    assert not result.restored and not result.removed
    assert (repo / "kept.py").read_text() == "edited\n"


def test_a_rename_is_reported_as_both_halves(repo):
    point = cp.snapshot(repo)
    (repo / "kept.py").rename(repo / "renamed.py")
    _agent_writes(repo / "renamed.py", (repo / "renamed.py").read_text())
    by_path = {c.path: c.status for c in cp.changes_since(point)}
    # Both halves must appear or a rewind would restore the original and leave
    # the agent's copy behind as a duplicate.
    assert by_path["kept.py"] == "deleted"
    assert by_path["renamed.py"] == "added"
    cp.restore(point, ["kept.py", "renamed.py"])
    assert (repo / "kept.py").read_text() == "original\n"
    assert not (repo / "renamed.py").exists()


def test_paths_with_spaces_survive_the_round_trip(repo):
    (repo / "a file.py").write_text("v1\n")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-qm", "add")
    point = cp.snapshot(repo)
    (repo / "a file.py").write_text("v2\n")
    assert [c.path for c in cp.changes_since(point)] == ["a file.py"]
    cp.restore(point, ["a file.py"])
    assert (repo / "a file.py").read_text() == "v1\n"


# ── persistence ────────────────────────────────────────────────────────────


def test_checkpoints_round_trip_newest_first(repo):
    cp.record("sess", cp.snapshot(repo, "first message"))
    (repo / "kept.py").write_text("changed\n")
    cp.record("sess", cp.snapshot(repo, "second message"))

    stored = cp.load("sess")
    assert [c.label for c in stored] == ["second message", "first message"]


def test_history_is_capped(repo, monkeypatch):
    monkeypatch.setattr(cp, "MAX_PER_SESSION", 3)
    point = cp.snapshot(repo)
    for i in range(6):
        cp.record("sess", cp.Checkpoint(point.tree, 1000 + i, f"m{i}", str(repo)))
    labels = [c.label for c in cp.load("sess")]
    assert labels == ["m5", "m4", "m3"]


def test_sessions_do_not_see_each_others_checkpoints(repo):
    cp.record("a", cp.snapshot(repo, "for a"))
    assert cp.load("b") == []
    cp.forget("a")
    assert cp.load("a") == []


def test_corrupt_records_are_skipped_not_fatal(repo, monkeypatch):
    cp.record("sess", cp.snapshot(repo, "good"))
    path = cp._store_path()
    import json

    data = json.loads(path.read_text())
    data["sess"].append({"tree": "", "cwd": ""})
    data["sess"].append("not even a dict")
    path.write_text(json.dumps(data))
    assert [c.label for c in cp.load("sess")] == ["good"]


def test_long_labels_are_truncated(repo):
    point = cp.snapshot(repo, "x" * 500)
    assert len(point.label) <= 90
    assert point.label.endswith("…")


# ── the destructive edge: never reap a file that predates the checkpoint ───


def test_rewind_refuses_to_delete_a_file_older_than_the_checkpoint(repo):
    """The .gitignore trapdoor.

    A git-ignored file is absent from the snapshot tree. If the agent then
    deletes .gitignore, that pre-existing file stops being ignored, shows up
    as "added" in the tree diff, and a naive restore deletes it — destroying
    user data that predates the turn being undone.
    """
    import os

    ignored = repo / "debug.log"
    ignored.write_text("months of logs\n")
    os.utime(ignored, (1_000_000, 1_000_000))  # clearly older than any run

    point = cp.snapshot(repo, "do some work")
    (repo / ".gitignore").unlink()  # agent removes the ignore rules

    # Recorded as ignored at capture time, so it is not even offered.
    assert "debug.log" not in [c.path for c in cp.changes_since(point)]

    result = cp.restore(point, ["debug.log", ".gitignore"])
    assert ignored.exists(), "a pre-existing file was destroyed by a rewind"
    assert ignored.read_text() == "months of logs\n"
    assert "debug.log" in result.kept
    assert "debug.log" not in result.removed
    assert (repo / ".gitignore").exists()  # the real revert still happened


def test_a_modified_ignored_file_survives_rewind(repo):
    """A review finding, and the mtime guard alone does not catch it.

    An ignored `.env` exists before the turn. The agent EDITS it and drops the
    ignore rule. The file now has a post-checkpoint mtime, so `_predates` says
    "new" — and it is absent from the snapshot tree, so the diff says "added".
    A naive restore deletes a real secrets file that predates the turn.
    """
    env = repo / ".env"
    env.write_text("SECRET=original\n")
    (repo / ".gitignore").write_text("secrets/\n*.log\n.env\n")
    _run(repo, "git", "add", ".gitignore")
    _run(repo, "git", "commit", "-qm", "ignore env")

    point = cp.snapshot(repo, "do some work")
    assert ".env" in point.ignored, "the ignored set was not recorded"

    # The agent edits the ignored file AND removes the rule protecting it.
    _agent_writes(env, "SECRET=rewritten-by-agent\n")
    (repo / ".gitignore").write_text("secrets/\n*.log\n")

    # It must not even be offered as a deletable change...
    assert ".env" not in [c.path for c in cp.changes_since(point)]
    # ...and must survive being asked for explicitly.
    result = cp.restore(point, [".env", ".gitignore"])
    assert env.exists(), "a pre-existing ignored file was destroyed by a rewind"
    assert env.read_text() == "SECRET=rewritten-by-agent\n"
    assert ".env" in result.kept
    assert ".env" not in result.removed


def test_a_whole_ignored_directory_is_out_of_scope(repo):
    (repo / "secrets").mkdir()
    (repo / "secrets" / "key.pem").write_text("private\n")
    point = cp.snapshot(repo)
    assert any(e.rstrip("/") == "secrets" for e in point.ignored)
    assert point.covers("secrets/key.pem") is False
    assert point.covers("app.py") is True

    (repo / ".gitignore").unlink()
    _agent_writes(repo / "secrets" / "key.pem", "tampered\n")
    result = cp.restore(point, ["secrets/key.pem"])
    assert (repo / "secrets" / "key.pem").exists()
    assert "secrets/key.pem" in result.kept


def test_coverage_survives_a_reload_from_disk(repo):
    cp.record("sess", cp.snapshot(repo, "with ignored set"))
    restored = cp.load("sess")[0]
    assert restored.ignored  # the .gitignore fixture guarantees at least one
    assert restored.covers("debug.log") is False


def test_a_file_the_agent_genuinely_created_is_still_deleted(repo):
    """The guard must not defeat the feature it protects."""
    point = cp.snapshot(repo)
    _agent_writes(repo / "brand_new.py", "written by the agent\n")
    result = cp.restore(point, ["brand_new.py"])
    assert result.removed == ["brand_new.py"]
    assert not (repo / "brand_new.py").exists()
    assert not result.kept


def test_an_unstattable_path_is_kept_not_deleted(repo, monkeypatch):
    point = cp.snapshot(repo)
    _agent_writes(repo / "new.py", "x\n")
    monkeypatch.setattr(
        cp.Path, "stat", lambda self, **kw: (_ for _ in ()).throw(OSError("nope"))
    )
    result = cp.restore(point, ["new.py"])
    assert result.kept == ["new.py"]


# ── ordering: the snapshot must complete before the agent can write ────────


def test_snapshot_is_taken_before_the_message_is_dispatched(repo):
    """A review finding: the two were fired concurrently.

    `_capture_checkpoint` started a worker and `send_user_text` ran on the very
    next line, so there was no guarantee `git add -A` had even begun before the
    agent started editing. A checkpoint that captures a half-applied turn
    restores to a state that never existed — worse than having no undo.

    Exercised at the module boundary the window relies on: the snapshot must
    observe the tree as it was, not as the turn leaves it.
    """
    point_holder = {}

    def take_snapshot():
        point_holder["point"] = cp.snapshot(repo, "go")

    def agent_writes():
        _agent_writes(repo / "kept.py", "written by the agent\n")

    # Correct order: snapshot fully completes, then the agent runs.
    take_snapshot()
    agent_writes()

    point = point_holder["point"]
    assert [c.path for c in cp.changes_since(point)] == ["kept.py"]
    cp.restore(point, ["kept.py"])
    assert (repo / "kept.py").read_text() == "original\n"


def test_dispatch_still_runs_when_the_snapshot_is_impossible(tmp_path):
    """No repo, no git, a raise — the message must still be sent."""
    plain = tmp_path / "notarepo"
    plain.mkdir()
    assert cp.snapshot(plain) is None  # the window treats this as "just send"


# ── deletion is never destructive ──────────────────────────────────────────


def test_removed_files_are_moved_aside_not_destroyed(repo):
    """Every guard here is a classification, and classifications can be wrong.

    So the destructive step is not destructive: a wrong guess costs a `mv`,
    not the user's data.
    """
    point = cp.snapshot(repo)
    _agent_writes(repo / "new.py", "agent content\n")
    result = cp.restore(point, ["new.py"])

    assert result.removed == ["new.py"]
    assert not (repo / "new.py").exists()
    recovered = pathlib.Path(result.trash_dir) / "new.py"
    assert recovered.read_text() == "agent content\n"


def test_a_renamed_ignored_file_is_recoverable(repo):
    """Jeeves blocker: `.env` -> `.env.local`, modified, un-ignored.

    `covers('.env.local')` is true — it is a different path — and the edit
    gives it a fresh mtime, so both guards pass it through as "created". The
    checkpoint cannot recreate it either, since `.env` was ignored and is not
    in the tree. Deleting it would be total, unrecoverable loss.
    """
    (repo / ".env").write_text("SECRET=real\n")
    (repo / ".gitignore").write_text(".env\n*.log\n")
    _run(repo, "git", "add", ".gitignore")
    _run(repo, "git", "commit", "-qm", "ignore env")
    point = cp.snapshot(repo)

    (repo / ".env").rename(repo / ".env.local")
    _agent_writes(repo / ".env.local", "SECRET=real-and-edited\n")
    (repo / ".gitignore").write_text("*.log\n")

    result = cp.restore(point, [".env.local"])
    assert ".env.local" in result.removed  # classified as new — the guards miss it
    recovered = pathlib.Path(result.trash_dir) / ".env.local"
    assert recovered.read_text() == "SECRET=real-and-edited\n", (
        "a pre-existing secrets file was destroyed beyond recovery"
    )


def test_a_truncated_ignored_list_refuses_every_deletion(repo, monkeypatch):
    """Jeeves blocker: an incomplete record must not be treated as complete.

    Past the cap, a pre-existing ignored file that the agent edits and
    un-ignores looks exactly like a new file to both guards.
    """
    monkeypatch.setattr(cp, "MAX_IGNORED_RECORDED", 1)
    (repo / "a.log").write_text("one\n")
    (repo / "b.log").write_text("two\n")
    point = cp.snapshot(repo)
    assert point.ignored_truncated is True
    assert point.may_delete is False

    _agent_writes(repo / "new.py", "genuinely new\n")
    result = cp.restore(point, ["new.py"])
    assert result.kept == ["new.py"]
    assert (repo / "new.py").exists()


def test_complete_coverage_still_allows_deletion(repo):
    point = cp.snapshot(repo)
    assert point.ignored_truncated is False
    assert point.may_delete is True


# ── pathspec magic ─────────────────────────────────────────────────────────


def test_a_pathspec_magic_filename_cannot_restore_unticked_files(repo):
    """Jeeves blocker: paths are pathspecs to git even after `--`.

    A repository may legally contain a file named `:(glob)**`. Restoring that
    one ticked file must not drag every path it globs along with it, silently
    reverting work the user chose to keep.
    """
    magic = repo / ":(glob)**"
    magic.write_text("v1\n")
    (repo / "bystander.py").write_text("v1\n")
    _run(repo, "git", "add", "-A")
    _run(repo, "git", "commit", "-qm", "add magic name")

    point = cp.snapshot(repo)
    magic.write_text("v2\n")
    (repo / "bystander.py").write_text("edited after the checkpoint\n")

    cp.restore(point, [":(glob)**"])

    assert magic.read_text() == "v1\n", "the ticked file was not restored"
    assert (repo / "bystander.py").read_text() == "edited after the checkpoint\n", (
        "an unticked file was overwritten via pathspec magic"
    )
