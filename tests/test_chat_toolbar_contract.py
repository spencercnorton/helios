"""Static guard: every method the window calls on the toolbar must exist.

v0.69.0 added `set_measured_breakdown` to `_ContextPopover` but not the
`ChatToolbar` delegator beside it, so `_on_cli_context_usage` raised
AttributeError on every `get_context_usage` answer — 105 times in one uptime —
and the context meter silently kept showing the 4-chars/token estimate. Nothing
failed loudly because PyGObject swallows a signal-handler exception at the C
boundary, so the driver's own turn accounting carried on unharmed.

The check is structural (AST, no GTK import — CI runs python-slim) and covers
the whole call surface rather than the one method that broke: a delegator added
to the wrong class in this file is caught the next time CI runs, not the next
time someone opens the context popover.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "helios"

#: Real `Gtk.Widget` inheritance, so absent from ChatToolbar's own class body.
#: Deliberately an explicit list: a name is only exempt once someone confirms
#: GTK provides it, which is the whole point of the guard.
_INHERITED = {"connect", "set_visible"}


def _methods_of(class_name: str, module: Path) -> set[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {
        node.name
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _calls_on(attribute: str, module: Path) -> set[str]:
    """Method names invoked as `self.<attribute>.<name>(...)`."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == attribute
    }


def test_window_only_calls_toolbar_methods_that_exist() -> None:
    called = _calls_on("_chat_toolbar", _SRC / "main_window.py")
    assert called, "AST match found no toolbar calls — the guard has gone blind"

    defined = _methods_of("ChatToolbar", _SRC / "widgets" / "chat_toolbar.py")
    missing = sorted(called - defined - _INHERITED)
    assert not missing, (
        f"main_window calls {missing} on ChatToolbar, which does not define them. "
        "A method placed on _ContextPopover still needs its ChatToolbar delegator."
    )


def test_measured_breakdown_reaches_the_popover() -> None:
    """The regression itself: the delegator exists on both classes."""
    widgets = _SRC / "widgets" / "chat_toolbar.py"
    assert "set_measured_breakdown" in _methods_of("ChatToolbar", widgets)
    assert "set_measured_breakdown" in _methods_of("_ContextPopover", widgets)


def test_no_class_reads_an_attribute_it_never_binds() -> None:
    """`_ContextPopover.set_measured_breakdown` read `self._destroyed`, which
    nothing in that class ever assigned, so it raised AttributeError on every
    call — swallowed by PyGObject at the signal boundary. The existing
    name-presence test cannot see this: it only proves the method is defined,
    never that its body runs."""
    tree = ast.parse((_SRC / "widgets" / "chat_toolbar.py").read_text(encoding="utf-8"))
    offenders: dict[str, list[str]] = {}
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        bound = {
            n.name
            for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for stmt in cls.body:  # class-level constants (_TWEEN_MS, SIZE, ...)
            targets = list(getattr(stmt, "targets", []))
            if isinstance(stmt, ast.AnnAssign):
                targets.append(stmt.target)
            bound.update(t.id for t in targets if isinstance(t, ast.Name))
        read: set[str] = set()
        for node in ast.walk(cls):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
            ):
                (bound if isinstance(node.ctx, ast.Store) else read).add(node.attr)
        missing = sorted(read - bound)
        if missing:
            offenders[cls.name] = missing
    assert not offenders, (
        f"{offenders} — a private attribute is read but never assigned in its "
        "own class. GTK signal handlers swallow the AttributeError."
    )
