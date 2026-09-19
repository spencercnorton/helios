"""`edit_diff` — the text shaping behind a file edit rendered as a diff.

GTK-free (difflib only) so it runs on the slim CI image. The assertion that
earns its keep is the Codex-shaped one: that provider mirrors a file change as
an Edit carrying only `file_path`, and the caller MUST fall back to the JSON
rendering rather than show an empty diff.
"""

from __future__ import annotations

from helios.backend.tool_diff import MAX_DIFF_CHARS, edit_diff, edit_path


def test_edit_shows_the_replaced_line_once_under_one_header() -> None:
    diff = edit_diff(
        "Edit",
        {"file_path": "a.py", "old_string": "old line", "new_string": "new line"},
    )

    assert diff is not None
    assert "-old line" in diff and "+new line" in diff
    assert diff.count("--- a/a.py") == 1
    assert diff.startswith("--- a/a.py")


def test_write_is_an_all_additions_diff_from_an_empty_file() -> None:
    diff = edit_diff("Write", {"file_path": "new.py", "content": "one\ntwo\n"})

    assert diff is not None
    assert "@@ -0,0" in diff
    assert "+one" in diff and "+two" in diff
    assert not any(line.startswith("-") for line in diff.splitlines()[2:])


def test_multiedit_emits_one_header_for_all_its_edits() -> None:
    diff = edit_diff(
        "MultiEdit",
        {
            "file_path": "a.py",
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "beta", "new_string": "BETA"},
            ],
        },
    )

    assert diff is not None
    assert diff.count("--- a/a.py") == 1
    assert "+ALPHA" in diff and "+BETA" in diff


def test_notebook_edit_diffs_the_new_cell_source() -> None:
    diff = edit_diff(
        "NotebookEdit",
        {"notebook_path": "nb.ipynb", "cell_id": "c1", "new_source": "print(1)"},
    )

    assert diff is not None
    assert "b/nb.ipynb" in diff
    assert "+print(1)" in diff


def test_codex_shaped_edit_falls_back_to_the_json_rendering() -> None:
    """Codex mirrors a file_change as an Edit with only `file_path`. Returning
    None is what keeps those sessions showing the input they do have."""
    assert edit_diff("Edit", {"file_path": "x.py"}) is None
    assert edit_diff("Write", {"file_path": "x.py"}) is None
    assert edit_diff("MultiEdit", {"file_path": "x.py"}) is None
    assert edit_diff("NotebookEdit", {"notebook_path": "x.ipynb"}) is None


def test_a_non_editing_tool_has_no_diff() -> None:
    assert edit_diff("Bash", {"command": "ls"}) is None
    assert edit_diff("Edit", "not a dict") is None


def test_a_no_op_edit_has_no_diff() -> None:
    assert edit_diff("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "x"}) is None


def test_an_oversized_diff_says_so() -> None:
    diff = edit_diff(
        "Write",
        {"file_path": "big.py", "content": "\n".join(f"line {i}" for i in range(2000))},
    )

    assert diff is not None
    assert diff.endswith("… (diff truncated)")
    assert len(diff) <= MAX_DIFF_CHARS + len("\n… (diff truncated)")


def test_edit_path_prefers_file_path_then_notebook_path() -> None:
    assert edit_path({"file_path": "a.py"}) == "a.py"
    assert edit_path({"notebook_path": "nb.ipynb"}) == "nb.ipynb"
    assert edit_path({"file_path": "  "}) == ""
    assert edit_path({}) == ""
