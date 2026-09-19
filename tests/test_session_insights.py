from __future__ import annotations

from helios.backend.session_insights import insights_for_turns
from helios.backend.transcript import ToolResult, ToolUse, Turn


def _turn(*uses: ToolUse, results: tuple[ToolResult, ...] = ()) -> Turn:
    turn = Turn(role="assistant")
    turn.tool_uses.extend(uses)
    turn.tool_results.extend(results)
    return turn


def _titles(turns: list[Turn], **kwargs) -> list[str]:
    return [item.title for item in insights_for_turns(turns, **kwargs)]


def test_edit_records_a_verification_gap():
    insights = insights_for_turns(
        [
            _turn(
                ToolUse(name="Edit", id="e", input={"file_path": "app.py"}),
                results=(ToolResult("e", "changed", is_error=False),),
            )
        ]
    )

    needed = next(
        item for item in insights if item.title == "Verification not established"
    )
    assert needed.status == "warn"
    assert needed.detail.startswith("1 file was recorded as changed")
    assert "do not establish" in needed.detail
    assert needed.evidence == (
        "Recorded change: app.py · cwd unavailable",
    )


def test_pass_after_edit_is_a_neutral_command_fact_not_file_verification():
    turns = [
        _turn(
            ToolUse(name="Edit", id="e", input={"file_path": "app.py"}),
            results=(ToolResult("e", "changed", is_error=False),),
        ),
        _turn(
            ToolUse(
                name="Bash",
                id="v",
                input={"command": "pytest -q", "cwd": "/repo"},
            ),
            results=(ToolResult("v", "1 passed", is_error=False),),
        ),
    ]

    insights = insights_for_turns(turns, default_scope="/repo")
    passed = next(item for item in insights if item.title == "Check command passed")

    assert passed.status == "info"
    assert "does not establish" in passed.detail
    assert passed.evidence == ("Recorded cwd: /repo", "Passed: pytest -q")
    assert "Verification not established" in _titles(
        turns, default_scope="/repo"
    )


def test_pass_in_another_worktree_does_not_clear_changed_files():
    turns = [
        _turn(
            ToolUse(
                name="Edit",
                id="e",
                input={"file_path": "/repo-a/app.py"},
            ),
            results=(ToolResult("e", "changed", is_error=False),),
        ),
        _turn(
            ToolUse(
                name="Bash",
                id="v",
                input={"command": "pytest -q", "cwd": "/repo-b"},
            ),
            results=(ToolResult("v", "1 passed", is_error=False),),
        ),
    ]

    insights = insights_for_turns(turns)

    assert [item.title for item in insights] == [
        "Check command passed",
        "Verification not established",
    ]
    needed = insights[-1]
    assert "/repo-a/app.py" in needed.evidence[0]
    assert "/repo-b" not in needed.evidence[0]


def test_same_workspace_pass_still_does_not_claim_change_coverage():
    turns = [
        _turn(
            ToolUse(name="Edit", id="e", input={"file_path": "/repo/app.py"}),
            results=(ToolResult("e", "changed", is_error=False),),
        ),
        _turn(
            ToolUse(
                name="Bash",
                id="v",
                input={"command": "pytest -q", "cwd": "/repo"},
            ),
            results=(ToolResult("v", "1 passed", is_error=False),),
        ),
    ]

    assert _titles(turns) == [
        "Check command passed",
        "Verification not established",
    ]


def test_check_cwd_or_absolute_target_never_claims_change_coverage():
    edit = _turn(
        ToolUse(name="Edit", id="e", input={"file_path": "/repo-a/app.py"}),
        results=(ToolResult("e", "changed", is_error=False),),
    )
    checks = (
        ToolUse(
            name="Bash",
            id="other-target",
            input={"command": "pytest /repo-b/tests -q", "cwd": "/repo-a"},
        ),
        ToolUse(
            name="Bash",
            id="root-cwd",
            input={"command": "pytest -q", "cwd": "/"},
        ),
    )

    for check in checks:
        turns = [
            edit,
            _turn(
                check,
                results=(ToolResult(check.id, "1 passed", is_error=False),),
            ),
        ]
        assert _titles(turns) == [
            "Check command passed",
            "Verification not established",
        ]


def test_relative_change_with_unknown_scope_is_never_cleared_by_a_check():
    turns = [
        _turn(
            ToolUse(name="Edit", id="e", input={"file_path": "app.py"}),
            results=(ToolResult("e", "changed", is_error=False),),
        ),
        _turn(
            ToolUse(
                name="Bash",
                id="v",
                input={"command": "pytest -q", "cwd": "/repo"},
            ),
            results=(ToolResult("v", "1 passed", is_error=False),),
        ),
    ]

    assert _titles(turns) == [
        "Check command passed",
        "Verification not established",
    ]


def test_edit_after_latest_pass_is_reported_chronologically():
    turns = [
        _turn(
            ToolUse(name="Bash", id="v", input={"command": "pytest -q"}),
            results=(ToolResult("v", "passed", is_error=False),),
        ),
        _turn(
            ToolUse(name="Edit", id="e", input={"file_path": "new.py"}),
            results=(ToolResult("e", "changed", is_error=False),),
        ),
    ]

    insights = insights_for_turns(turns)

    assert "Check command passed" in [item.title for item in insights]
    needed = next(
        item for item in insights if item.title == "Verification not established"
    )
    assert needed.evidence == (
        "Recorded change: new.py · cwd unavailable",
    )


def test_failed_check_is_actionable_and_same_check_pass_resolves_it():
    failed = _turn(
        ToolUse(
            name="Bash",
            id="bad",
            input={"command": "pytest -q", "cwd": "/repo"},
        ),
        results=(ToolResult("bad", "1 failed", is_error=True),),
    )
    failure = next(
        item
        for item in insights_for_turns([failed])
        if item.title == "Check command failed"
    )
    assert failure.status == "error"
    assert "Result: 1 failed" in failure.evidence

    passed = _turn(
        ToolUse(
            name="Bash",
            id="good",
            input={"command": "pytest -q", "cwd": "/repo"},
        ),
        results=(ToolResult("good", "1 passed", is_error=False),),
    )
    assert _titles([failed, passed]) == ["Check command passed"]


def test_pass_in_another_recorded_cwd_does_not_hide_failure():
    turns = [
        _turn(
            ToolUse(
                name="Bash",
                id="bad",
                input={"command": "pytest -q", "cwd": "/repo-a"},
            ),
            results=(ToolResult("bad", "1 failed", is_error=True),),
        ),
        _turn(
            ToolUse(
                name="Bash",
                id="good",
                input={"command": "pytest -q", "cwd": "/repo-b"},
            ),
            results=(ToolResult("good", "1 passed", is_error=False),),
        ),
    ]

    failure = next(
        item
        for item in insights_for_turns(turns)
        if item.title == "Check command failed"
    )

    assert "Recorded cwd: /repo-a" in failure.evidence


def test_missing_check_result_is_unknown_not_running_or_passed():
    insights = insights_for_turns(
        [_turn(ToolUse(name="Bash", id="v", input={"command": "ruff check ."}))]
    )

    unknown = next(item for item in insights if item.title == "Check outcome unknown")
    assert unknown.status == "info"
    assert "cannot claim" in unknown.detail
    assert unknown.evidence[-1] == "No result: ruff check ."


def test_generic_shell_failures_are_activity_not_quality_defects():
    insights = insights_for_turns(
        [
            _turn(
                ToolUse(name="Bash", id="bad", input={"command": "rg missing"}),
                results=(ToolResult("bad", "no matches", is_error=True),),
            )
        ]
    )

    assert [(item.title, item.status) for item in insights] == [
        ("Current request", "info")
    ]


def test_additional_files_count_and_failed_edit_does_not_add_a_path():
    turns = [
        _turn(
            ToolUse(
                name="Write",
                id="many",
                input={
                    "file_path": "a.py",
                    "additional_files": ["b.py", "c.py"],
                },
            ),
            results=(ToolResult("many", "changed", is_error=False),),
        ),
        _turn(
            ToolUse(name="Edit", id="bad", input={"file_path": "never.py"}),
            results=(ToolResult("bad", "permission denied", is_error=True),),
        ),
    ]

    insights = insights_for_turns(turns)
    needed = next(
        item for item in insights if item.title == "Verification not established"
    )

    assert needed.detail.startswith("3 files were recorded as changed")
    assert not any("never.py" in line for line in needed.evidence)
    assert "File change failed" in [item.title for item in insights]


def test_missing_edit_result_is_unknown_not_a_claimed_change():
    insights = insights_for_turns(
        [_turn(ToolUse(name="Edit", id="e", input={"file_path": "app.py"}))]
    )

    unknown = next(
        item for item in insights if item.title == "File change outcome unknown"
    )

    assert unknown.status == "info"
    assert unknown.evidence == ("No result: app.py · cwd unavailable",)
    assert "Verification not established" not in [item.title for item in insights]


def test_identical_replayed_tool_id_is_counted_once():
    replay = ToolUse(name="Edit", id="same", input={"file_path": "changed.py"})
    turns = [
        _turn(replay, results=(ToolResult("same", "changed", is_error=False),)),
        _turn(replay),
    ]

    needed = next(
        item
        for item in insights_for_turns(turns)
        if item.title == "Verification not established"
    )

    assert needed.evidence == (
        "Recorded change: changed.py · cwd unavailable",
    )


def test_identical_duplicate_results_are_one_recorded_outcome():
    edit = ToolUse(name="Edit", id="same", input={"file_path": "changed.py"})
    duplicate = ToolResult("same", "changed", is_error=False)

    titles = _titles([_turn(edit, results=(duplicate, duplicate))])

    assert titles == ["Verification not established"]


def test_conflicting_edit_results_fail_closed_as_unknown():
    turns = [
        _turn(
            ToolUse(name="Edit", id="e", input={"file_path": "app.py"}),
            results=(
                ToolResult("e", "changed", is_error=False),
                ToolResult("e", "permission denied", is_error=True),
            ),
        )
    ]

    unknown = insights_for_turns(turns)[0]

    assert unknown.title == "File change outcome unknown"
    assert unknown.evidence == (
        "Conflicting results: app.py · cwd unavailable",
    )


def test_conflicting_check_results_fail_closed_as_unknown():
    turns = [
        _turn(
            ToolUse(name="Bash", id="v", input={"command": "pytest -q"}),
            results=(
                ToolResult("v", "1 passed", is_error=False),
                ToolResult("v", "1 failed", is_error=True),
            ),
        )
    ]

    unknown = insights_for_turns(turns)[0]

    assert unknown.title == "Check outcome unknown"
    assert unknown.evidence[-1] == "Conflicting results: pytest -q"


def test_reused_tool_id_for_different_calls_fails_closed():
    turns = [
        _turn(
            ToolUse(name="Edit", id="reused", input={"file_path": "a.py"}),
            ToolUse(name="Edit", id="reused", input={"file_path": "b.py"}),
            results=(ToolResult("reused", "changed", is_error=False),),
        )
    ]

    unknown = insights_for_turns(turns)[0]

    assert unknown.title == "File change outcome unknown"
    assert {line.split(": ", 1)[1].split(" ·", 1)[0] for line in unknown.evidence} == {
        "a.py",
        "b.py",
    }


def test_false_positive_and_masked_commands_are_not_checks():
    commands = (
        "rg test src",
        "git checkout feature/checks",
        "echo pytest -q",
        "pytest -q; true",
        "pytest -q || true",
        "pytest --collect-only -q",
        "ruff format .",
        "ruff check --fix .",
        "git diff --check",
        "pytest -q & wait",
        "/bin/bash -lc 'pytest -q' || true",
        "/bin/bash -lc 'pytest -q'\ntrue",
        "ruff check --exit-zero .",
        "tox --notest",
        "nox --list",
        "nox -l",
        "ctest -N",
        "pytest --fixtures",
        "make test -n",
        "pytest --markers",
        "pytest --cache-show",
        "ruff check --show-files",
        "ruff check --show-settings",
        "tox -l",
        "tox -a",
        "tox list",
        "tox config",
        "ctest --list-presets",
        "go test -list=.",
        "make test -ns",
        "npm run test --if-present",
        "npm test --ignore-scripts",
        "pyright --createstub pkg",
        "shellcheck --list-optional",
        "shellcheck -V",
        "pytest $(echo --collect-only)",
        "PYTEST_ADDOPTS=--collect-only pytest",
        "go test -exec=true ./...",
        "MAKEFLAGS=-n make test",
        "pytest --funcargs",
        "ruff check -e .",
        "tox devenv .venv",
        "MAKEFLAGS=-ns make test",
        "go test -count 0 ./...",
        "NPM_CONFIG_IGNORE_SCRIPTS=true npm test",
    )
    turns = [
        _turn(
            ToolUse(name="Edit", id="edit", input={"file_path": "app.py"}),
            results=(ToolResult("edit", "changed", is_error=False),),
        )
    ]
    turns.extend(
        _turn(
            ToolUse(name="Bash", id=f"c{index}", input={"command": command}),
            results=(ToolResult(f"c{index}", "ok", is_error=False),),
        )
        for index, command in enumerate(commands)
    )

    assert _titles(turns) == ["Verification not established"]


def test_understood_cd_chain_and_environment_prefix_are_recognized():
    command = "cd /repo && PYTHONPATH=src python3 -m pytest -q && ruff check ."
    turns = [
        _turn(
            ToolUse(name="Bash", id="v", input={"command": command}),
            results=(ToolResult("v", "passed", is_error=False),),
        )
    ]

    passed = next(
        item
        for item in insights_for_turns(turns)
        if item.title == "Check command passed"
    )

    assert passed.evidence[0] == "Recorded cwd: /repo"


def test_direct_bash_wrapper_is_unwrapped():
    turns = [
        _turn(
            ToolUse(
                name="Bash",
                id="v",
                input={"command": "/bin/bash -lc 'python3 -m pytest -q'"},
            ),
            results=(ToolResult("v", "passed", is_error=False),),
        )
    ]

    assert _titles(turns) == ["Check command passed"]


def test_check_identity_preserves_whitespace_inside_quoted_arguments():
    turns = [
        _turn(
            ToolUse(
                name="Bash",
                id="bad",
                input={"command": "ruff check 'dir  one'", "cwd": "/repo"},
            ),
            results=(ToolResult("bad", "failed", is_error=True),),
        ),
        _turn(
            ToolUse(
                name="Bash",
                id="other",
                input={"command": "ruff check 'dir one'", "cwd": "/repo"},
            ),
            results=(ToolResult("other", "passed", is_error=False),),
        ),
    ]

    insights = insights_for_turns(turns)
    assert "Check command failed" in [item.title for item in insights]
    failure = next(item for item in insights if item.title == "Check command failed")
    assert "Failed: ruff check 'dir  one'" in failure.evidence


def test_unknown_newer_edit_does_not_hide_older_unresolved_change_failure():
    turns = [
        _turn(
            ToolUse(name="Edit", id="bad", input={"file_path": "old.py"}),
            results=(ToolResult("bad", "failed", is_error=True),),
        ),
        _turn(ToolUse(name="Edit", id="unknown", input={"file_path": "new.py"})),
    ]

    assert _titles(turns) == ["File change failed", "File change outcome unknown"]


def test_successful_retry_resolves_failure_for_the_same_file_only():
    failed_a = _turn(
        ToolUse(name="Edit", id="bad-a", input={"file_path": "a.py"}),
        results=(ToolResult("bad-a", "failed", is_error=True),),
    )
    failed_b = _turn(
        ToolUse(name="Edit", id="bad-b", input={"file_path": "b.py"}),
        results=(ToolResult("bad-b", "failed", is_error=True),),
    )
    retry_a = _turn(
        ToolUse(name="Edit", id="good-a", input={"file_path": "a.py"}),
        results=(ToolResult("good-a", "changed", is_error=False),),
    )

    insights = insights_for_turns([failed_a, failed_b, retry_a])
    failure = next(item for item in insights if item.title == "File change failed")

    assert any("b.py" in line for line in failure.evidence)
    assert not any("a.py" in line for line in failure.evidence)
    assert "Verification not established" in [item.title for item in insights]


def test_unknown_retry_does_not_resolve_a_recorded_change_failure():
    turns = [
        _turn(
            ToolUse(name="Edit", id="bad", input={"file_path": "app.py"}),
            results=(ToolResult("bad", "failed", is_error=True),),
        ),
        _turn(ToolUse(name="Edit", id="unknown", input={"file_path": "app.py"})),
    ]

    assert _titles(turns) == ["File change failed", "File change outcome unknown"]


def test_unparsed_cwd_changers_are_rejected_as_check_evidence():
    commands = (
        "pushd /repo-b && pytest -q",
        "(cd /repo-b && pytest -q)",
        "cd -P /repo-b && pytest -q",
        'cd "$HOME" && pytest -q',
        "cd - && pytest -q",
        'cd "$(pwd)/sub" && pytest -q',
        "cd ~/repo-b && pytest -q",
        "cd repo-b && pytest -q",
    )
    for index, command in enumerate(commands):
        turns = [
            _turn(
                ToolUse(
                    name="Edit",
                    id=f"e{index}",
                    input={"file_path": f"f{index}.py"},
                ),
                results=(ToolResult(f"e{index}", "changed", is_error=False),),
            ),
            _turn(
                ToolUse(
                    name="Bash",
                    id=f"v{index}",
                    input={"command": command, "cwd": "/repo-a"},
                ),
                results=(ToolResult(f"v{index}", "passed", is_error=False),),
            ),
        ]
        assert _titles(turns) == ["Verification not established"], command


def test_unknown_cwd_pass_is_neutral_and_never_claims_file_coverage():
    turns = [
        _turn(
            ToolUse(name="Edit", id="e", input={"file_path": "/other/app.py"}),
            results=(ToolResult("e", "changed", is_error=False),),
        ),
        _turn(
            ToolUse(name="Bash", id="v", input={"command": "pytest -q"}),
            results=(ToolResult("v", "passed", is_error=False),),
        ),
    ]

    passed = next(
        item
        for item in insights_for_turns(turns)
        if item.title == "Check command passed"
    )

    assert passed.status == "info"
    assert passed.evidence[0] == "Recorded cwd: unavailable"
    assert "does not establish" in passed.detail


def test_session_default_scope_is_shown_for_claude_check_evidence():
    turns = [
        _turn(
            ToolUse(name="Bash", id="v", input={"command": "pytest -q"}),
            results=(ToolResult("v", "passed", is_error=False),),
        )
    ]

    passed = next(
        item
        for item in insights_for_turns(turns, default_scope="/repo")
        if item.title == "Check command passed"
    )

    assert passed.evidence[0] == "Recorded cwd: /repo"


def test_latest_user_request_resets_historical_57_and_26_counts():
    old = _turn(
        *(
            ToolUse(name="Bash", id=f"bad-{index}", input={"command": "false"})
            for index in range(57)
        ),
        results=tuple(
            ToolResult(f"bad-{index}", "failed", is_error=True) for index in range(57)
        ),
    )
    old.tool_uses.extend(
        ToolUse(name="Edit", id=f"edit-{index}", input={"file_path": f"f{index}.py"})
        for index in range(26)
    )
    request = Turn(role="user", text_parts=["What is the current status?"])

    insights = insights_for_turns([old, request])

    assert [(item.title, item.status) for item in insights] == [
        ("Current request", "info")
    ]
    assert "57" not in insights[0].detail
    assert "26" not in insights[0].detail


def test_tool_result_user_record_does_not_start_a_new_request_window():
    request = Turn(role="user", text_parts=["Make the change"])
    use = _turn(ToolUse(name="Edit", id="e", input={"file_path": "app.py"}))
    result_turn = Turn(
        role="user",
        tool_results=[ToolResult("e", "changed", is_error=False)],
    )

    assert "Verification not established" in _titles([request, use, result_turn])


def test_replayed_pre_request_tool_id_is_not_new_request_work():
    replay = ToolUse(name="Edit", id="stable", input={"file_path": "old.py"})
    old = _turn(
        replay,
        results=(ToolResult("stable", "changed", is_error=False),),
    )
    request = Turn(role="user", text_parts=["What happened?"])

    assert _titles([old, request, _turn(replay)]) == ["Current request"]


def test_failure_excerpt_skips_stream_fd_noise():
    turns = [
        _turn(
            ToolUse(name="Bash", id="v", input={"command": "pytest -q"}),
            results=(
                ToolResult(
                    "v",
                    "\n".join(
                        (
                            "Failed to create stream fd: Operation not permitted",
                            "FAILED tests/test_one.py::test_one - AssertionError",
                            "FAILED tests/test_two.py::test_two - AssertionError",
                        )
                    ),
                    is_error=True,
                ),
            ),
        )
    ]

    failure = next(
        item
        for item in insights_for_turns(turns)
        if item.title == "Check command failed"
    )
    result = next(line for line in failure.evidence if line.startswith("Result:"))

    assert "stream fd" not in result
    assert "test_one" in result
    assert "test_two" in result


def test_no_build_or_check_evidence_is_informational():
    insights = insights_for_turns(
        [_turn(ToolUse(name="Read", input={"file_path": "app.py"}))]
    )

    assert [(item.title, item.status) for item in insights] == [
        ("Current request", "info")
    ]
