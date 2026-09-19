"""Regression tests for inline markdown rendering (_inline_markup).

`markdown` imports gi at module load, so these need the gi/Gtk typelib (no
display) and are skipped on the GTK-free CI image. `_inline_markup` itself is a
pure string function.

The headline case is the placeholder/digit collision the review flagged: it's
NOT present (the code uses \x01 sentinels, not bare integers), and these lock
that in so it can't regress to the bare-integer form.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.widgets.markdown import _inline_markup  # noqa: E402


def _has_control(s: str) -> bool:
    return "\x00" in s or "\x01" in s


# --- the (non-)collision cases --------------------------------------------


def test_code_span_does_not_clobber_literal_digit():
    out = _inline_markup("`a` then the digit 0 here")
    assert out == "<tt>a</tt> then the digit 0 here"
    assert not _has_control(out)


def test_many_spans_with_multidigit_prose_numbers():
    out = _inline_markup("see 1 and `code` and 10 and 100")
    assert out == "see 1 and <tt>code</tt> and 10 and 100"
    assert not _has_control(out)


def test_multiple_spans_and_loose_digits():
    out = _inline_markup("`x` `y` `z` with 0 1 2 in prose")
    assert out == "<tt>x</tt> <tt>y</tt> <tt>z</tt> with 0 1 2 in prose"
    assert not _has_control(out)


# --- basic markup ----------------------------------------------------------


def test_bold_italic_code():
    assert _inline_markup("**b**") == "<b>b</b>"
    assert _inline_markup("_i_") == "<i>i</i>"
    assert _inline_markup("`c`") == "<tt>c</tt>"


def test_pango_metacharacters_escaped():
    out = _inline_markup("a < b & c > d")
    assert out == "a &lt; b &amp; c &gt; d"


# --- link safety (security properties worth locking in) --------------------


def test_safe_link_renders_anchor():
    out = _inline_markup("[site](https://example.com)")
    assert out == '<a href="https://example.com">site</a>'


def test_dangerous_scheme_link_rendered_as_plain_text():
    out = _inline_markup("[x](javascript:alert(1))")
    assert "<a " not in out
    assert "javascript:" not in out
    assert "x" in out


def test_quote_in_url_cannot_break_out_of_href():
    out = _inline_markup('[x](https://e.com/" foreground="red)')
    # The injected attribute must be neutralized (quote escaped), not live.
    assert 'foreground="red"' not in out
    assert "&quot;" in out


# --- forged-sentinel guard -------------------------------------------------


def test_input_sentinel_bytes_are_stripped():
    # A model emitting raw \x01 around a digit must not forge a placeholder.
    out = _inline_markup("`real` and \x010\x01 forged")
    assert not _has_control(out)
    # The code span renders; the forged bit becomes inert plain text.
    assert "<tt>real</tt>" in out
