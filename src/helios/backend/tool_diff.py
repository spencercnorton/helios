"""Unified diffs for file-editing tool inputs. GTK-free so the text shaping
is testable on the slim CI image."""

from __future__ import annotations

import difflib

MAX_DIFF_CHARS = 4000  # same budget as message_bubble._format_tool_input

#: Tools whose input describes a file write. `edit_diff` renders these; the
#: transcript also lists their `file_path`s as the turn's "Files changed".
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def edit_path(inp: dict) -> str:
    """The file an editing tool targets, or "" — `notebook_path` for
    NotebookEdit, `file_path` for everything else."""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "notebook_path"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def edit_diff(name: str, inp: dict) -> str | None:
    """Unified diff for an Edit / MultiEdit / Write / NotebookEdit input, or
    None when the input does not carry before/after text — Codex mirrors a
    file_change as Edit/Write with only `file_path`
    (process/codex_events.py:263-270 and codex_app_events.py:1066), so None is
    the common, expected answer there and the caller must fall back to the JSON
    rendering."""
    if not isinstance(inp, dict):
        return None
    path = edit_path(inp)
    pairs: list[tuple[str, str]] = []
    if name in ("Write", "NotebookEdit"):
        # Both replace a whole unit (a file, a notebook cell) with new text and
        # carry no "before", so both render as an all-additions diff.
        content = inp.get("content" if name == "Write" else "new_source")
        if not isinstance(content, str):
            return None
        pairs.append(("", content))
    elif name == "Edit":
        old, new = inp.get("old_string"), inp.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        pairs.append((old, new))
    elif name == "MultiEdit":
        edits = inp.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        for e in edits:
            if not isinstance(e, dict):
                return None
            old, new = e.get("old_string"), e.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                return None
            pairs.append((old, new))
    else:
        return None
    label = path or "(file)"
    out: list[str] = []
    for i, (old, new) in enumerate(pairs):
        chunk = list(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile=f"a/{label}",
                tofile=f"b/{label}",
                lineterm="",
                n=3,
            )
        )
        out.extend(chunk[2:] if i and len(chunk) >= 2 else chunk)  # one header only
    text = "\n".join(out)
    if not text.strip():
        return None
    return (
        text[:MAX_DIFF_CHARS] + "\n… (diff truncated)"
        if len(text) > MAX_DIFF_CHARS
        else text
    )
