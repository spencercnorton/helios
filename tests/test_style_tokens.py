"""Static guards keeping the motion/radius tokens in `_motion.py` authoritative.

GTK 4.14 (Ubuntu 24.04) has no CSS custom properties and `@define-color` is
colors only, so `helios.css` cannot reference `_motion.py`. "Declared once" is
therefore achievable only as a single Python source of truth plus static checks
that stop the two drifting apart.

Deliberately GTK-free — `re`, `pathlib`, and `helios.widgets._motion` itself —
so this runs in the `python:3.13-slim` `tests` lane alongside
`test_css_invariants.py`. Importing `_motion` here is not incidental: being
importable without PyGObject is the whole reason that module is written the way
it is, and this import is the only thing that proves it in CI.

One assert for now: no literal transition duration survives in `src/`. The
stylesheet-side asserts the M-3 spec prescribes (duration allowlist, no `pt;`)
land with the CSS sweep in M-3b — they would fail today, because M-3a changes no
CSS.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "helios"
sys.path.insert(0, str(_SRC.parent))

from helios.widgets._motion import BASE_MS, DURATIONS, FAST_MS  # noqa: E402

def _literal_duration_sites(path: Path) -> list[int]:
    """Line numbers where a transition duration is passed as an int literal.

    `ast` rather than a regex, for two reasons. A line-based pattern misses the
    multiline form — `widget.set_transition_duration(\n    200\n)` — which is
    exactly how a long receiver expression gets formatted, so the invariant
    could regress unnoticed. And it cannot tell code from a comment or a
    docstring mentioning the call.

    Both spellings are covered: the setter, and the kwargs constructor form
    `Gtk.Revealer(transition_duration=200)`. Kwargs construction is house style
    elsewhere in `src/`, so a setter-only guard would leave the obvious door
    open. A named constant is fine and is the point — only literals fail.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "set_transition_duration"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, int)
        ):
            hits.append(node.lineno)
        for kw in node.keywords:
            if (
                kw.arg == "transition_duration"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, int)
            ):
                hits.append(kw.value.lineno)
    return sorted(hits)


def test_no_literal_transition_duration_survives() -> None:
    offenders = [
        f"{path.relative_to(_SRC.parent.parent)}:{lineno}"
        for path in sorted(_SRC.rglob("*.py"))
        for lineno in _literal_duration_sites(path)
    ]
    assert not offenders, (
        f"pass a token from helios.widgets._motion (FAST_MS={FAST_MS}, "
        f"BASE_MS={BASE_MS}), not a literal: " + ", ".join(offenders)
    )


def test_motion_module_stays_importable_without_pygobject() -> None:
    """The slim `tests` lane has no PyGObject, and `_motion` lives under
    `helios/widgets/` — a package full of `gi` importers. That is exactly the
    shape that silently acquires a transitive `gi` import and stops being
    reachable from that lane, with nothing failing to say so.

    The module-level import at the top of this file is the real proof: in the
    slim lane it would raise ImportError and this file would go red. The static
    check below is the fast local signal for the same thing.
    """
    src = (_SRC / "widgets" / "_motion.py").read_text()
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.match(r"\s*(import gi\b|from gi\b|from helios\.widgets\.\w)", line)
    ]
    assert not offenders, (
        "_motion.py must stay import-free of gi and of gi-importing siblings; "
        f"found: {offenders}"
    )
    assert DURATIONS == (FAST_MS, BASE_MS, 320)
