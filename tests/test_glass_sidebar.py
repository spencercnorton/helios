"""Translucent sidebar preference + accent de-duplication — GTK-free half.

Deliberately imports no GTK so it runs in the fast `tests` lane
(python:3.13-slim, no PyGObject), which is the lane that always runs. The
GTK-dependent assertions live in test_glass_sidebar_gtk.py; they CANNOT share a
module with these, because `pytest.importorskip` at module scope skips the whole
file and would take these invariants out of the fast lane with them.
"""

from __future__ import annotations

import re
from pathlib import Path


from helios.backend import appearance

CSS = (
    Path(__file__).resolve().parents[1]
    / "src/helios/resources/style/helios.css"
).read_text(encoding="utf-8")

#: Comment-free view. The comments explain what was REMOVED and name the old
#: tokens verbatim, so asserting against the raw text matches the prose and not
#: the rules.
RULES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


# ── preference coercion (GTK-free) ───────────────────────────────────────


def test_glass_defaults_off() -> None:
    assert appearance.normalize_glass(None) is False
    assert appearance.normalize_glass(False) is False


def test_glass_accepts_real_booleans() -> None:
    assert appearance.normalize_glass(True) is True


def test_a_stored_false_string_does_not_enable_the_feature() -> None:
    """The reason normalize_glass is not just `bool(value)`.

    A hand-edited or older ui_state.json can hold the JSON spelling as text.
    `bool("false")` is True, which would silently turn the sidebar translucent
    for someone who had explicitly turned it off.
    """
    assert appearance.normalize_glass("false") is False
    assert appearance.normalize_glass("False") is False
    assert appearance.normalize_glass("") is False


def test_a_stored_true_string_does_enable_it() -> None:
    assert appearance.normalize_glass("true") is True
    assert appearance.normalize_glass("  TRUE  ") is True


def test_other_types_are_off_not_truthy() -> None:
    assert appearance.normalize_glass(1) is False
    assert appearance.normalize_glass({"on": True}) is False


# ── stylesheet invariants (GTK-free) ─────────────────────────────────────


def test_no_pinned_brand_accent_remains() -> None:
    """The whole point of the accent change: one accent, the system's.

    A reintroduced `@define-color helios-accent` would split the palette again
    and would look correct on any default-orange test box.
    """
    assert "@define-color helios-accent" not in RULES
    assert "@helios-accent" not in RULES


def test_the_quota_marker_follows_the_accent() -> None:
    rule = re.search(r"\.helios-mission-marker-quota\s*\{([^}]*)\}", RULES, re.S)
    assert rule, "quota marker rule missing"
    assert "@accent_color" in rule.group(1)


def test_the_default_sidebar_is_still_opaque() -> None:
    """The invariant that actually protects people who never enabled this.

    An unscoped alpha on the session list would make every install translucent.
    So: the plain rule must still set a solid @sidebar_bg_color, and the alpha
    must appear only inside a `window.helios-glass` selector.
    """
    # The selector list is matched by membership rather than verbatim: it grew
    # a fourth entry (`.helios-context-column`) and pinning the exact text
    # meant the rule could not gain a pane without this test failing for a
    # reason it does not care about. What it cares about is the BODY.
    plain = next(
        (
            m
            for m in re.finditer(r"(^[^{}]*)\{([^}]*)\}", RULES, re.M | re.S)
            if ".helios-session-list" in m.group(1)
            and ".helios-session-column" in m.group(1)
            and ".helios-context" in m.group(1)
        ),
        None,
    )
    assert plain, "the default (unscoped) sidebar rule is gone"
    body = plain.group(2)
    assert "@sidebar_bg_color" in body and "alpha(" not in body, body

    for m in re.finditer(r"([^{}]*)\{[^{}]*alpha\(@sidebar_bg_color[^{}]*\}", RULES):
        assert "window.helios-glass" in m.group(1), (
            f"sidebar alpha applied outside the opt-in class: {m.group(1).strip()!r}"
        )


def test_every_transparent_background_added_here_is_opt_in() -> None:
    """`background-color: transparent` on the window itself is the risky one.

    Unscoped, it would make the whole app see-through for everybody. Restricted
    to the rules mentioning `window`, every one must carry the glass class.
    """
    for m in re.finditer(
        r"([^{}]*\bwindow\b[^{}]*)\{([^{}]*)\}", RULES
    ):
        if "transparent" in m.group(2):
            assert "window.helios-glass" in m.group(1), (
                f"unscoped transparent window rule: {m.group(1).strip()!r}"
            )


def test_the_alpha_sits_on_exactly_one_node() -> None:
    """The seam bug this shipped with, in test form.

    Measured on the development workstation: with the alpha on BOTH the container and the inner list,
    the rows band composited to 92.6% opaque against 72.3% for the empty area
    below it — a visible horizontal seam, because two 0.72 layers stack to
    1-(1-0.72)^2. Uniform 72.3% once only the container paints.
    """
    # Since the inversion the WINDOW carries the alpha, not the sidebar — but the
    # invariant is unchanged and so is the failure it prevents.
    alpha_rules = re.findall(
        r"([^{}]*)\{[^{}]*alpha\(@(?:window|sidebar)_bg_color[^{}]*\}", RULES, re.S
    )
    # Count SELECTORS, not rules. Counting rules is vacuous: adding a second
    # comma-separated selector to the existing rule reintroduces the seam while
    # leaving the rule count at one. (That mutation passed an earlier version of
    # this assertion.)
    selectors = [s.strip() for r in alpha_rules for s in r.split(",") if s.strip()]
    assert len(selectors) == 1, (
        f"exactly one node may carry the glass alpha, found {selectors}"
    )
    assert selectors[0] == "window.helios-glass", selectors[0]
    # The sidebar is the node most likely to be given a second alpha "so it goes
    # glass when unfocused". It must be CLEARED in :backdrop instead, or it
    # composites 0.72 over the window's 0.72 to 92% against the content's 72%.
    back = re.search(
        r"window\.helios-glass \.helios-session-column:backdrop\s*\{([^}]*)\}",
        RULES, re.S,
    )
    assert back and "transparent" in back.group(1), (
        "the column's backdrop state must clear, not re-tint"
    )
    # ...and the list on top of it must not paint, or it covers that clear and
    # the column stays solid while unfocused.
    lst = re.search(
        r"window\.helios-glass \.helios-session-list\s*\{([^}]*)\}", RULES, re.S
    )
    assert lst and "transparent" in lst.group(1), (
        "the list must not paint over the column's backdrop clear"
    )
    # ...and Adwaita's own opaque `list` fill inside the sidebar must be cleared,
    # or it covers the sidebar's own fill completely.
    inner = re.search(
        r"window\.helios-glass \.helios-session-list list\s*\{([^}]*)\}", RULES, re.S
    )
    assert inner and "transparent" in inner.group(1), (
        "the list element inside the sidebar container must be transparent"
    )


def test_glass_never_flattens_the_selected_row() -> None:
    """A review finding, measured and confirmed before it shipped.

    A glass rule matching `.helios-session-row` is specificity (0,2,1) because of
    the `window` element, which beats `.helios-session-row:selected` at (0,2,0).
    With one present, the selected row and a normal row both rendered
    srgba(46,46,50,0.72) — identical, so nothing showed which session was active.

    The earlier hand-written preview missed this because it used an UNSCOPED
    `.helios-session-row` at (0,1,0), which *loses* to :selected. Verifying a
    selector other than the one being shipped proves nothing.
    """
    for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", RULES):
        sel, body = m.group(1), m.group(2)
        if "window.helios-glass" not in sel or "transparent" not in body:
            continue
        for one in (s.strip() for s in sel.split(",")):
            if not one.endswith(".helios-session-row"):
                continue
            assert ":selected" in one or ":hover" in one, (
                f"{one!r} outranks .helios-session-row:selected (0,2,0) and would "
                "erase the selection wash when glass is on"
            )


def test_the_transcript_side_is_glass_not_opaque() -> None:
    """The shape inverted on 2026-08-20; this is the old invariant, reversed.

    It used to assert the transcript was re-painted opaque under glass, on the
    reasoning that prose over a moving desktop is unreadable. The house style is
    now the desktop-wide one (the desktop theme layer, GNOME Settings as reference): solid
    navigation column, glass content. So the transcript must be CLEARED under
    glass, or its default @window_bg_color sits opaquely on the window's alpha
    and the content side goes solid again.

    The readability worry is answered by test_the_alpha_is_safe_over_any_backdrop
    — 0.72 holds at 5.44:1 worst case against every backdrop — not by avoidance.
    """
    default = re.search(r"^\.helios-transcript\s*\{([^}]*)\}", RULES, re.M | re.S)
    assert default, "the default .helios-transcript rule is gone"
    assert "@window_bg_color" in default.group(1), (
        f"the non-glass transcript must stay opaque: {default.group(1).strip()!r}"
    )

    glassed = re.search(
        r"window\.helios-glass \.helios-transcript\s*\{([^}]*)\}", RULES, re.S
    )
    assert glassed, "the transcript must be cleared under glass, or content stays solid"
    assert "transparent" in glassed.group(1), glassed.group(1)

    # Cards still float opaquely on the glass, exactly as the boxed lists in the
    # reference do. The composer must NOT be dragged into the transparent set.
    composer = re.search(r"^\.helios-composer-card\s*\{([^}]*)\}", RULES, re.M | re.S)
    assert composer and "transparent" not in composer.group(1), composer


def test_no_opaque_container_class_survives() -> None:
    """`.helios-glass-opaque` was the old shape's load-bearing piece — a container
    class re-asserting opacity over everything except the session list. The
    inversion deletes it, and it has to go from BOTH the stylesheet and the
    Python, or it lingers as a class that silently paints the content side solid.
    """
    assert "helios-glass-opaque" not in RULES, (
        "the opaque container rule is still in helios.css"
    )
    src = (Path(__file__).resolve().parents[1] / "src/helios/main_window.py").read_text()
    assert 'add_css_class("helios-glass-opaque")' not in src, (
        "main_window.py still applies the opaque container class"
    )


def test_the_header_bar_has_no_rule_under_glass() -> None:
    """No top bar is the whole point of the reference.

    Adw.ToolbarView is flat: the header paints nothing and shows whatever is
    behind it. Leaving it alone is what makes it continuous with the content.
    The previous version re-stated @window_bg_color here, which painted it opaque
    — measured against the reference, an opaque header IS the top bar, and the
    reference has none (content header 40.73 vs 41.22 directly beneath it).

    So this asserts an ABSENCE, which is a thing a future edit will want to undo
    "to fix the header shade". That would reintroduce the bar.
    """
    rule = re.search(r"window\.helios-glass headerbar\s*\{([^}]*)\}", RULES, re.S)
    assert rule is None, (
        "a headerbar rule under glass paints the top bar back on: "
        f"{rule.group(1).strip()!r}" if rule else ""
    )


def _relative_luminance(rgb) -> float:
    def channel(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a, b) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_the_alpha_is_safe_over_any_backdrop() -> None:
    """The alpha is fixed, so it must hold against EVERY possible backdrop.

    A wallpaper-adaptive version was built and removed: a dynamic compositor blur
    samples whatever is behind the WINDOW, which is usually another application
    rather than the desktop. Overlap a white browser and the backdrop is white no
    matter what the wallpaper is, and nothing exposes the composited pixels to
    the app — so a wallpaper-derived alpha guarantees nothing.

    A constant that clears AA at both extremes in both schemes needs no knowledge
    of the backdrop at all:

        backdrop        dark scheme   light scheme
        black             17.5:1         5.44:1
        white              5.51:1       10.9:1

    0.65 drops to 4.46:1 and fails, so this value is load-bearing.
    """
    rule = re.search(r"window\.helios-glass\s*\{([^}]*)\}", RULES, re.S)
    assert rule, "the glass window rule is gone"
    found = re.search(r"alpha\(@window_bg_color,\s*([0-9.]+)\)", rule.group(1))
    assert found, rule.group(1)
    value = float(found.group(1))

    # (surface_bg, text) per libadwaita scheme. Since the inversion this
    # composite is what the TRANSCRIPT sits on, not just the sidebar — which is
    # why the floor matters more now, not less.
    palettes = (((46, 46, 50), (255, 255, 255)), ((235, 235, 237), (50, 50, 55)))
    worst = min(
        _contrast(
            text, tuple(value * s + (1 - value) * b for s, b in zip(sidebar, backdrop))
        )
        for sidebar, text in palettes
        for backdrop in ((0, 0, 0), (128, 128, 128), (255, 255, 255), (255, 240, 150))
    )
    assert worst >= 4.5, (
        f"alpha {value} bottoms out at {worst:.2f}:1 across the schemes and "
        "backdrop extremes — under the 4.5:1 AA floor. The backdrop is arbitrary "
        "window content, so this cannot be recovered by measuring the wallpaper."
    )


def test_the_child_clear_cannot_reach_a_tooltip() -> None:
    """The unreadable session-row tooltip, in test form.

    A tooltip is its own surface, but its CSS node hangs off the window's, and
    libadwaita paints the tooltip's plate on that node (`tooltip.background`).
    So a bare `window.helios-glass > *` cleared it and nothing underneath
    repainted it: measured on the development workstation (GNOME 50 Wayland, GTK 4.22.4), the
    interior read the window's own srgb(32,32,32) instead of srgb(6,6,11) and
    the row's title + cwd hung over the sidebar as bare white text.
    """
    rule = re.search(r"window\.helios-glass\s*>\s*([^{]*)\{([^}]*)\}", RULES)
    assert rule, "the glass child-clear rule is gone"
    assert "transparent" in rule.group(2), rule.group(2)
    assert ":not(tooltip)" in rule.group(1), rule.group(1).strip()


def test_the_glass_keeps_its_edge_cues() -> None:
    """Without these it is a flat rectangle again — the thing that got reverted.

    A pane reads as glass because of its edges: a specular hairline along the top
    and a defined trailing edge. Transparency alone just looks like a hole.
    """
    rule = re.search(
        r"window\.helios-glass \.helios-session-column\s*\{([^}]*)\}", RULES, re.S
    )
    assert rule and "box-shadow" in rule.group(1), (
        "the column lost its edge highlights; transparency alone reads as a "
        "missing asset, not as glass"
    )
    assert rule.group(1).count("inset") >= 2, (
        f"expected both a top hairline and a trailing edge: {rule.group(1).strip()!r}"
    )


def test_every_right_pane_page_sits_on_a_painted_column() -> None:
    """The bug: three of four pages painted, the fourth and the switcher did not.

    `.helios-plan-pane`, `.helios-context` and `.helios-mission-pane` each
    carried their own `@sidebar_bg_color`; `.helios-shared-pane` never did, and
    `Adw.ViewSwitcher` above all four paints nothing at all. Under glass that
    left the switcher strip and the whole Shared page at alpha 0 straight onto
    the desktop — measured 0 in every rendered page at y=5..20, and down to
    y=60 on Shared.

    So the fill has to live on the CONTAINER, and this asserts the container is
    what carries it, not any individual page. A future fifth page inherits it.
    """
    fill = re.search(
        r"(^|\n)([^{}]*\.helios-context-column[^{}]*)\{([^}]*)\}", RULES
    )
    assert fill, ".helios-context-column has no background rule at all"
    assert "@sidebar_bg_color" in fill.group(3), (
        "the right pane column must carry the solid fill; a page-by-page fill "
        f"is what left the switcher transparent: {fill.group(3).strip()!r}"
    )


def test_the_right_pane_pages_do_not_double_paint_the_column() -> None:
    """Alpha on exactly one node, in every state — including this column.

    The column clears in `:backdrop` for the same focus cue as the session
    column. A page that keeps its own fill covers that clear up and this pane
    alone stays solid when the window is unfocused, which is the seam this
    stylesheet has now documented twice.
    """
    assert re.search(
        r"window\.helios-glass \.helios-context-column:backdrop\s*\{[^}]*"
        r"background-color:\s*transparent",
        RULES,
        re.S,
    ), "the right pane column does not clear in :backdrop"
    cleared = re.search(
        r"((?:window\.helios-glass \.helios-context-column [^{},]+,?\s*)+)\{([^}]*)\}",
        RULES,
        re.S,
    )
    assert cleared and "transparent" in cleared.group(2), (
        "the pages inside the right pane must be cleared under glass, or the "
        "column's :backdrop state never shows"
    )
    for page in (".helios-plan-pane", ".helios-context", ".helios-mission-pane"):
        assert page in cleared.group(1), f"{page} still paints over the column"
