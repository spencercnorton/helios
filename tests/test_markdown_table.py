"""Parser and GTK rendering coverage for GFM-style pipe tables."""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from helios.widgets.markdown import _parse, _split_table_row, render_markdown  # noqa: E402


def _children(widget: Gtk.Widget) -> list[Gtk.Widget]:
    children: list[Gtk.Widget] = []
    child = widget.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    return children


def _table_block(source: str):
    blocks = _parse(source)
    assert len(blocks) == 1
    assert blocks[0].kind == "table"
    return blocks[0]


def test_split_row_supports_wrapperless_and_escaped_pipes():
    assert _split_table_row(r"one | two \| also two | three") == [
        "one",
        "two | also two",
        "three",
    ]


def test_split_row_removes_only_unescaped_wrapper_pipes():
    assert _split_table_row(r"| one | two \| three |") == ["one", "two | three"]
    assert _split_table_row(r"one \| two") is None


def test_valid_table_parses_header_rows_and_alignment():
    block = _table_block(
        "| Left | Center | Right |\n"
        "| :--- | :---: | ---: |\n"
        "| **one** | two | three |\n"
        "| four | five | six |"
    )
    assert block.table_header == ["Left", "Center", "Right"]
    assert block.table_alignments == ["left", "center", "right"]
    assert block.table_rows == [
        ["**one**", "two", "three"],
        ["four", "five", "six"],
    ]


def test_table_accepts_optional_outer_pipes_and_escaped_cell_pipe():
    block = _table_block(
        "Name | Meaning\n"
        "--- | ---\n"
        r"A \| B | both" + "\n"
    )
    assert block.table_rows == [["A | B", "both"]]


def test_short_rows_are_padded_and_surplus_cells_ignored():
    block = _table_block(
        "| A | B |\n"
        "| --- | --- |\n"
        "| one |\n"
        "| two | three | surplus |"
    )
    assert block.table_rows == [["one", ""], ["two", "three"]]


@pytest.mark.parametrize(
    "source",
    [
        "A | B\n-- | --\none | two",
        "A | B\n--- | words\none | two",
        "A | B\n--- | --- | ---\none | two",
        "A | B\n---\none | two",
        "A | B\n- - - | ---\none | two",
    ],
)
def test_malformed_table_stays_paragraph_text(source: str):
    blocks = _parse(source)
    assert all(block.kind != "table" for block in blocks)
    assert "|" in " ".join(block.text for block in blocks if block.kind == "para")


def test_table_starts_a_new_block_after_paragraph_without_blank_line():
    blocks = _parse("Before\nA | B\n--- | ---\none | two\n\nAfter")
    assert [block.kind for block in blocks] == ["para", "table", "para"]
    assert blocks[0].text == "Before"
    assert blocks[2].text == "After"


def test_existing_block_syntax_takes_precedence_over_table_detection():
    blocks = _parse("# Heading | detail\n--- | ---")
    assert [block.kind for block in blocks] == ["heading", "para"]

    blocks = _parse("- Item | detail\n--- | ---")
    assert [block.kind for block in blocks] == ["ul", "para"]


def _rendered_table(source: str) -> Gtk.ListBox:
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("GTK display unavailable")
    root = render_markdown(source)
    children = _children(root)
    assert len(children) == 1
    table = children[0]
    assert isinstance(table, Gtk.ListBox)
    return table


def test_renderer_builds_accessible_wrapping_rows_and_cells():
    table = _rendered_table(
        "| Left | Center | Right |\n"
        "| :--- | :---: | ---: |\n"
        "| alpha | a long value that must be allowed to wrap | omega |"
    )
    assert table.get_accessible_role() == Gtk.AccessibleRole.TABLE

    rows = _children(table)
    assert len(rows) == 2
    assert all(row.get_accessible_role() == Gtk.AccessibleRole.ROW for row in rows)

    header_cells = _children(rows[0].get_child())
    body_cells = _children(rows[1].get_child())
    assert [cell.get_accessible_role() for cell in header_cells] == [
        Gtk.AccessibleRole.COLUMN_HEADER,
    ] * 3
    assert [cell.get_accessible_role() for cell in body_cells] == [
        Gtk.AccessibleRole.CELL,
    ] * 3
    assert all(cell.get_wrap() for cell in header_cells + body_cells)
    assert [cell.get_xalign() for cell in body_cells] == pytest.approx([0.0, 0.5, 1.0])


def test_renderer_displays_cells_without_pipe_syntax():
    table = _rendered_table("| Name | Value |\n| --- | --- |\n| A \\| B | 3 |")
    rendered_text = [
        cell.get_text()
        for row in _children(table)
        for cell in _children(row.get_child())
    ]
    assert rendered_text == ["Name", "Value", "A | B", "3"]
    assert all("---" not in text for text in rendered_text)
