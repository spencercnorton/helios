"""Static guards on `helios.css` — the properties a stylesheet edit must keep.

GTK composites the real thing, so none of this proves a pixel. What it does
prove is that the *classes of defect* found in the 2026-08 UI audit cannot
silently return:

* a `#ffffff`-in-both-palettes token used as foreground over a translucent
  fill (unreadable in the light theme — D-4),
* two status dots separated only by an animation, which reduced motion then
  flattens into one (A-4a/b),
* white text on a brand fill below the WCAG AA floor (A-4c),
* a reduced-motion block that has to be extended by hand for every new
  keyframe (M-2),
* a class styled here but applied nowhere, or applied nowhere but styled here
  (T-2 — the check that would have caught all five unstyled lane classes and
  both dead rule groups before they shipped).

Deliberately GTK-free: `re` + `pathlib` only, no `gi`, so this runs in the
`python:3.13-slim` `tests` lane. The stylesheet is resolved by path for the
same reason — `importlib.resources` would import the `helios` package and drag
`gi` in with it.

The parser is a flat `{...}` scan rather than a real CSS parser. `helios.css`
has no nested at-rules containing the selectors these checks care about, and a
nested block would at worst make a check under-report — never false-positive.
Comment stripping is mandatory, not cosmetic: the file interleaves `/* ... */`
inside declaration blocks, and several of those comments name the very tokens
being scanned for.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CSS = _ROOT / "src" / "helios" / "resources" / "style" / "helios.css"
_SRC = _ROOT / "src" / "helios"

#: WCAG 2.1 AA floor for normal-size text.
_AA_NORMAL = 4.5


def _body() -> str:
    """Stylesheet text with every comment removed."""
    return re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)


def _rules(body: str) -> list[tuple[str, str]]:
    """(selector, declarations) for each top-level `{...}` block."""
    return [
        (" ".join(m.group(1).split()), m.group(2))
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", body)
    ]


def _declarations_for(body: str, needle: str, *, reduced_motion: bool) -> str:
    """Declarations of the single rule whose selector contains `needle`.

    `.helios-status-question` is styled twice — once normally, once under
    `.reduced-motion` — and the two carry different assertions, so the caller
    has to say which. Insisting on exactly one match means a third rule for the
    same state fails loudly instead of being silently ignored.
    """
    matches = [
        decls
        for sel, decls in _rules(body)
        if needle in sel and ("reduced-motion" in sel) is reduced_motion
    ]
    assert len(matches) == 1, (
        f"expected exactly one {'reduced-motion' if reduced_motion else 'base'} "
        f"rule matching {needle!r}, got {len(matches)}"
    )
    return matches[0]


# ── D-4: no white-in-both-palettes token over a translucent fill ──────────


def test_accent_fg_is_never_used_over_a_translucent_background() -> None:
    offenders = [
        sel
        for sel, decls in _rules(_body())
        if re.search(r"background(-color)?\s*:\s*alpha\(", decls)
        and "@accent_fg_color" in decls
    ]
    assert not offenders, (
        f"{offenders} set @accent_fg_color (#ffffff in both palettes) over a "
        "translucent background. Use @view_fg_color, or a solid "
        "@accent_bg_color fill."
    )


# ── A-4a/b: the question dot carries a non-colour channel ────────────────


def test_question_dot_differs_from_awaiting_by_more_than_colour() -> None:
    """`.helios-status-awaiting` is the same @warning_color disc.

    Only the inset ring tells them apart once colour is off the table
    (greyscale, colour-blindness) — the glow does not, because it is the same
    hue as the fill.
    """
    decls = _declarations_for(_body(), ".helios-status-question", reduced_motion=False)
    assert "inset" in decls, (
        ".helios-status-question must differ from .helios-status-awaiting by "
        "shape, not only by colour/animation: both are @warning_color discs."
    )


def test_question_dot_keeps_its_shape_channel_under_reduced_motion() -> None:
    """The reduced-motion override is what created the bug: it strips the
    animation, so the shape channel has to be restated here or the two amber
    states collapse into one indistinguishable disc."""
    decls = _declarations_for(_body(), ".helios-status-question", reduced_motion=True)
    assert "inset" in decls, (
        "the reduced-motion .helios-status-question override drops the inset "
        "ring, flattening it back into a plain @warning_color disc."
    )


# ── A-4c: white text on a brand fill must clear AA ───────────────────────


def _luminance(hex_colour: str) -> float:
    """WCAG relative luminance of `#rrggbb`."""
    channels = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    lo, hi = sorted((la, lb))
    return (hi + 0.05) / (lo + 0.05)


def test_hardcoded_foreground_on_hardcoded_fill_clears_aa() -> None:
    """Asserts the ratio, not the absence of a literal.

    A ban on `color: #ffffff` would be a source-text pin: it goes stale the
    moment the same defect reappears as `#fff` or `#fefefe`, and it would need
    a hand-written exemption for `.helios-provider-conflict`, whose dark fill
    genuinely does clear AA against white. Computing the ratio exempts that one
    on the merits and catches any future chip.
    """
    failures = []
    for sel, decls in _rules(_body()):
        bg = re.search(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{6})\s*;", decls)
        # `(?<!-)` so this does not match the `-color` of `background-color`.
        fg = re.search(r"(?<!-)\bcolor\s*:\s*(#[0-9a-fA-F]{6})\s*;", decls)
        if not (bg and fg):
            continue
        ratio = _contrast(fg.group(1), bg.group(1))
        if ratio < _AA_NORMAL:
            failures.append(f"{sel}: {fg.group(1)} on {bg.group(1)} = {ratio:.2f}:1")
    assert not failures, (
        f"below the {_AA_NORMAL}:1 WCAG AA floor: {failures}. Either darken the "
        "fill or switch to a tint + @view_fg_color (see .helios-provider-chip)."
    )


# ── M-2: reduced motion kills animation by blanket, not by enumeration ───


def test_reduced_motion_kills_animation_before_any_per_class_rule() -> None:
    """The blanket has to come first.

    Scoped to the *restore* rules — the reduced-motion rules that set something
    besides `animation`. Those are the ones whose declarations the blanket must
    not be able to clobber. A reduced-motion rule that only says
    `animation: none` (`.helios-bubble-enter`, which sits with its keyframes
    ~670 lines earlier) is redundant with the blanket either way, so its
    position is not part of the invariant and needs no hand-written exemption.
    """
    body = _body()
    blanket = re.search(r"\.helios-window\.reduced-motion\s*\*\s*\{[^{}]*animation\s*:\s*none", body)
    assert blanket, (
        "no blanket `.helios-window.reduced-motion * { animation: none; }` — "
        "without it every new @keyframes needs a matching opt-out by hand, "
        "which is how .helios-mission-marker-running kept its glow."
    )
    restores = [
        m.start()
        for m in re.finditer(r"(\.helios-window\.reduced-motion\s+[^{}]*)\{([^{}]*)\}", body)
        if re.search(r"(?<!-)\b(?!animation\b)[a-z-]+\s*:", m.group(2))
    ]
    assert restores, "expected reduced-motion rules that restore a resting appearance"
    assert blanket.start() < min(restores), (
        "the blanket reduced-motion rule must precede the per-class restore "
        "rules; the ones below it only re-state resting opacity/box-shadow."
    )


# ── T-2: styled classes and applied classes must be the same set ─────────

#: Classes defined in `helios.css` with no caller in `src/`, left alone on
#: purpose because they are outside this MR's four items. Both are dead
#: selector *lines* inside otherwise-live multi-selector groups — the same
#: shape as the `.helios-project-row` lines T-2 removed:
_KNOWN_DEAD = {
    # `.helios-effort-popover contents,` paired with the live
    # `.helios-execution-popover contents`.
    "helios-effort-popover",
}


def _applied_classes() -> tuple[set[str], set[str]]:
    """(literal class names, dynamic `f"helios-x-{...}"` prefixes) used in src/."""
    literals: set[str] = set()
    prefixes: set[str] = set()
    for module in _SRC.rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        literals |= set(re.findall(r"[\"'](helios-[a-z0-9-]+)[\"']", text))
        prefixes |= set(re.findall(r"[\"'](helios-[a-z0-9-]*-)\{", text))
    return literals, prefixes


def test_every_styled_helios_class_has_a_python_caller() -> None:
    """The invariant both halves of T-2 violated.

    Prefix-aware because the widgets compose class names at runtime
    (`f"helios-mission-marker-{status_key}"`), so a literal-only scan reports
    ~30 live classes as dead. Prefix matching removes those without weakening
    the check: neither `.helios-project-row` nor `.helios-context-levelbar`
    had a literal *or* a prefix, which is exactly why both were deletable.
    """
    literals, prefixes = _applied_classes()
    defined = set(re.findall(r"\.(helios-[a-z0-9-]+)", _body()))
    unused = sorted(
        name
        for name in defined - _KNOWN_DEAD
        if name not in literals and not any(name.startswith(p) for p in prefixes)
    )
    assert not unused, (
        f"styled in helios.css but applied nowhere in src/: {unused}. Delete "
        "the rule, or add it to _KNOWN_DEAD with a reason if it is a pending "
        "follow-up."
    )


# NOT asserted here: the mirror check, "every class applied in src/ has a rule".
# That is the direction which would have caught the five unstyled lane classes
# this MR styles, and it is worth having — but it cannot be made sound with a
# regex at this scope. `helios-` string literals in src/ are also driver task
# ids and notification ids (`helios-titlegen-ollama`, `helios-scratch-read`,
# `helios-question-<sid>`), so an unscoped scan reports 38 names, most of them
# not CSS at all. Narrowing to literals on a line mentioning `css_class` cuts
# that to 12 plausible ones (helios-memory-editor, helios-shared-row,
# helios-markdown-table, helios-work-btn, …) but loses recall the forward check
# above depends on, because plenty of live classes are composed in tuples and
# dicts away from their call site. Those 12 are a real finding and a separate
# item; allow-listing them here would turn this module into a check that cannot
# fail. Follow-up, not this MR.
