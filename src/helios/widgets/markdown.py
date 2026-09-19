"""Pragmatic Markdown → GTK widget tree.

We don't need a CommonMark-complete renderer — Claude's output covers a small
slice of Markdown and we render that slice well:

  * fenced code blocks (```lang)         -> CodeBlock (GtkSourceView5)
  * ATX headings (# .. ######)            -> Gtk.Label with title-N CSS
  * unordered lists (-, *, +)             -> bullet rows
  * ordered lists (1. 2. ...)             -> numbered rows
  * GFM-style pipe tables                  -> accessible compact row grid
  * blockquotes (> )                      -> indented italic label
  * horizontal rules (---, ***, ___)      -> Gtk.Separator
  * inline: **bold**, *italic*, `code`, [link](url)
  * paragraphs                            -> Gtk.Label with Pango markup

What we deliberately punt on:
  * footnotes, definition lists, task lists, raw HTML
  * nested lists deeper than one level (we flatten with indent)
  * setext headings (==== / ---- under text) — claude doesn't emit them

The output is a Gtk.Box of vertically-stacked widgets, ready to slot into a
MessageBubble.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from helios.widgets.code_block import CodeBlock


# --- Block model -----------------------------------------------------------


@dataclass(slots=True)
class _Block:
    kind: str  # "para" | "heading" | "ul" | "ol" | "table" | "quote" | "rule" | "code"
    text: str = ""
    level: int = 0
    lang: str = ""
    items: list[str] | None = None  # for lists
    table_header: list[str] | None = None
    table_rows: list[list[str]] | None = None
    table_alignments: list[str] | None = None


# --- Public API ------------------------------------------------------------


def render_markdown(text: str) -> Gtk.Widget:
    """Returns a Gtk.Box stacking all rendered blocks."""
    container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    container.set_hexpand(True)
    for block in _parse(text):
        widget = _render_block(block)
        if widget is not None:
            container.append(widget)
    return container


# --- Block parser ----------------------------------------------------------


_FENCE = re.compile(r"^(`{3,}|~{3,})\s*([\w.+\-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_RULE = re.compile(r"^(?:[-*_])(?:\s*[-*_]){2,}\s*$")
_UL_ITEM = re.compile(r"^\s{0,3}[-*+]\s+(.+)$")
_OL_ITEM = re.compile(r"^\s{0,3}(\d+)[.)]\s+(.+)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_TABLE_DELIMITER = re.compile(r"^(:)?-{3,}(:)?$")


def _split_table_row(line: str) -> list[str] | None:
    """Split one pipe row, honoring backslash-escaped pipes.

    ``None`` means the line has no delimiter pipe and therefore cannot be a
    table row.  Leading/trailing wrapper pipes are optional.  Backslashes only
    receive special treatment immediately before a pipe; all other Markdown
    escapes are left for the inline renderer.
    """
    source = line.strip()
    if not source:
        return None

    cells: list[str] = []
    cell: list[str] = []
    saw_delimiter = False
    ended_with_delimiter = False
    i = 0
    while i < len(source):
        char = source[i]
        if char == "\\":
            run_start = i
            while i < len(source) and source[i] == "\\":
                i += 1
            slash_count = i - run_start
            if i < len(source) and source[i] == "|":
                cell.extend("\\" * (slash_count // 2))
                if slash_count % 2:
                    cell.append("|")
                    i += 1
                    ended_with_delimiter = False
                    continue
                # An even slash run leaves the pipe unescaped.  Fall through
                # to the delimiter branch after retaining half the slashes.
            else:
                cell.extend("\\" * slash_count)
                ended_with_delimiter = False
                continue

        if i < len(source) and source[i] == "|":
            cells.append("".join(cell).strip())
            cell = []
            saw_delimiter = True
            ended_with_delimiter = True
            i += 1
            continue

        cell.append(source[i])
        ended_with_delimiter = False
        i += 1

    cells.append("".join(cell).strip())
    if not saw_delimiter:
        return None
    if source.startswith("|"):
        cells.pop(0)
    if ended_with_delimiter:
        cells.pop()
    return cells


def _table_start(lines: list[str], index: int) -> tuple[list[str], list[str]] | None:
    """Return a validated table header and its column alignments."""
    if index + 1 >= len(lines):
        return None
    header = _split_table_row(lines[index])
    delimiters = _split_table_row(lines[index + 1])
    if not header or not delimiters or len(header) != len(delimiters):
        return None

    alignments: list[str] = []
    for cell in delimiters:
        match = _TABLE_DELIMITER.fullmatch(cell)
        if match is None:
            return None
        left, right = match.groups()
        if left and right:
            alignments.append("center")
        elif right:
            alignments.append("right")
        else:
            alignments.append("left")
    return header, alignments


def _parse(text: str) -> list[_Block]:
    blocks: list[_Block] = []
    lines = text.splitlines()
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # Fenced code block
        m = _FENCE.match(line)
        if m:
            fence_char = m.group(1)[0]
            fence_len = len(m.group(1))
            lang = m.group(2) or ""
            i += 1
            buf: list[str] = []
            while i < n:
                close = lines[i]
                if close.startswith(fence_char * fence_len) and close.strip().count(fence_char) >= fence_len:
                    i += 1
                    break
                buf.append(lines[i])
                i += 1
            blocks.append(_Block(kind="code", text="\n".join(buf), lang=lang))
            continue

        # Horizontal rule
        if _RULE.match(line.strip()):
            blocks.append(_Block(kind="rule"))
            i += 1
            continue

        # Heading
        m = _HEADING.match(line)
        if m:
            blocks.append(_Block(kind="heading", text=m.group(2).strip(), level=len(m.group(1))))
            i += 1
            continue

        # Blockquote (collect consecutive > lines)
        if _QUOTE.match(line):
            buf = []
            while i < n and _QUOTE.match(lines[i]):
                m = _QUOTE.match(lines[i])
                buf.append(m.group(1))
                i += 1
            blocks.append(_Block(kind="quote", text="\n".join(buf).strip()))
            continue

        # Unordered list
        if _UL_ITEM.match(line):
            items: list[str] = []
            while i < n and _UL_ITEM.match(lines[i]):
                m = _UL_ITEM.match(lines[i])
                items.append(m.group(1))
                i += 1
                # Allow continuation lines indented 2+ spaces.
                while i < n and lines[i].startswith("  ") and not _UL_ITEM.match(lines[i]) and not _OL_ITEM.match(lines[i]):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            blocks.append(_Block(kind="ul", items=items))
            continue

        # Ordered list
        if _OL_ITEM.match(line):
            items = []
            while i < n and _OL_ITEM.match(lines[i]):
                m = _OL_ITEM.match(lines[i])
                items.append(m.group(2))
                i += 1
                while i < n and lines[i].startswith("  ") and not _UL_ITEM.match(lines[i]) and not _OL_ITEM.match(lines[i]):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            blocks.append(_Block(kind="ol", items=items))
            continue

        # GFM-style table.  Keep this after the established block forms so a
        # heading, quote, or list item containing a pipe retains its meaning.
        # Header and delimiter rows must have the same number of cells;
        # malformed table-looking text remains a paragraph.
        table = _table_start(lines, i)
        if table is not None:
            header, alignments = table
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip():
                row = _split_table_row(lines[i])
                if row is None:
                    break
                # GFM pads short rows and ignores surplus cells.
                normalized = (row + [""] * len(header))[:len(header)]
                rows.append(normalized)
                i += 1
            blocks.append(
                _Block(
                    kind="table",
                    table_header=header,
                    table_rows=rows,
                    table_alignments=alignments,
                )
            )
            continue

        # Blank line — paragraph terminator
        if not line.strip():
            i += 1
            continue

        # Default: paragraph — accumulate until blank line or new block
        buf = [line]
        i += 1
        while i < n:
            nxt = lines[i]
            if (not nxt.strip()
                    or _FENCE.match(nxt)
                    or _table_start(lines, i) is not None
                    or _HEADING.match(nxt)
                    or _RULE.match(nxt.strip())
                    or _QUOTE.match(nxt)
                    or _UL_ITEM.match(nxt)
                    or _OL_ITEM.match(nxt)):
                break
            buf.append(nxt)
            i += 1
        blocks.append(_Block(kind="para", text=" ".join(s.strip() for s in buf)))

    return blocks


# --- Block renderers -------------------------------------------------------


def _selectable_text(label: Gtk.Label, markup: str) -> None:
    """Selectable by pointer, but a tab stop only when it holds a link.

    set_selectable(True) also sets focusable in GTK4; without this every
    paragraph in a 150-turn transcript is its own Tab stop. Tradeoff, stated
    honestly: keyboard copy from prose is gone. A widget that can never take
    focus can never receive the key event, so focus-then-Ctrl+C is dead for
    every heading, paragraph, quote, list item and table cell. What survives is
    pointer selection and the right-click Copy item, which is gesture-driven
    and therefore focus-independent, plus the per-block copy button on code
    blocks.
    """
    label.set_selectable(True)
    has_link = '<a href="' in markup
    label.set_focusable(has_link)
    # A selectable Gtk.Label can report focus traversal handled even when
    # focusable=False, leaving Tab stuck on the preceding Copy button. Keep
    # pointer selection but exclude plain prose from traversal altogether.
    label.set_can_focus(has_link)


def _render_block(block: _Block) -> Gtk.Widget | None:
    if block.kind == "code":
        return CodeBlock(block.text, block.lang)

    if block.kind == "rule":
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        return sep

    if block.kind == "heading":
        markup = _inline_markup(block.text)
        lbl = Gtk.Label(label=markup, xalign=0)
        lbl.set_use_markup(True)
        lbl.set_wrap(True)
        _selectable_text(lbl, markup)
        css_class = {
            1: "title-2",
            2: "title-3",
            3: "title-4",
            4: "heading",
            5: "heading",
            6: "heading",
        }.get(block.level, "heading")
        lbl.add_css_class(css_class)
        lbl.set_margin_top(4)
        return lbl

    if block.kind == "para":
        markup = _inline_markup(block.text)
        lbl = Gtk.Label(label=markup, xalign=0)
        lbl.set_use_markup(True)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
        _selectable_text(lbl, markup)
        lbl.set_xalign(0)
        lbl.set_hexpand(True)
        return lbl

    if block.kind == "quote":
        markup = _inline_markup(block.text)
        lbl = Gtk.Label(label=markup, xalign=0)
        lbl.set_use_markup(True)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(2)
        _selectable_text(lbl, markup)
        lbl.add_css_class("helios-blockquote")
        return lbl

    if block.kind in ("ul", "ol"):
        return _render_list(block)

    if block.kind == "table":
        return _render_table(block)

    return None


def _render_list(block: _Block) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    for idx, item in enumerate(block.items or []):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if block.kind == "ul":
            bullet = Gtk.Label(label="•", xalign=0)
        else:
            bullet = Gtk.Label(label=f"{idx + 1}.", xalign=0)
        bullet.add_css_class("helios-bullet")
        bullet.set_valign(Gtk.Align.START)
        bullet.set_margin_top(0)
        row.append(bullet)

        markup = _inline_markup(item)
        text_lbl = Gtk.Label(label=markup, xalign=0)
        text_lbl.set_use_markup(True)
        text_lbl.set_wrap(True)
        text_lbl.set_wrap_mode(2)
        _selectable_text(text_lbl, markup)
        text_lbl.set_hexpand(True)
        text_lbl.set_xalign(0)
        row.append(text_lbl)

        box.append(row)
    return box


def _render_table(block: _Block) -> Gtk.Widget:
    """Render a pipe table as semantic rows rather than raw Markdown text."""
    table = Gtk.ListBox(accessible_role=Gtk.AccessibleRole.TABLE)
    table.set_selection_mode(Gtk.SelectionMode.NONE)
    table.set_hexpand(True)
    table.add_css_class("boxed-list")
    table.add_css_class("helios-markdown-table")

    alignments = block.table_alignments or []
    _append_table_row(table, block.table_header or [], alignments, header=True)
    for cells in block.table_rows or []:
        _append_table_row(table, cells, alignments, header=False)
    return table


def _append_table_row(
    table: Gtk.ListBox,
    cells: list[str],
    alignments: list[str],
    *,
    header: bool,
) -> None:
    row = Gtk.ListBoxRow(accessible_role=Gtk.AccessibleRole.ROW)
    row.set_activatable(False)
    row.set_selectable(False)

    columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    columns.set_homogeneous(True)
    for index, text in enumerate(cells):
        role = Gtk.AccessibleRole.COLUMN_HEADER if header else Gtk.AccessibleRole.CELL
        label = Gtk.Label(xalign=0, accessible_role=role)
        markup = _inline_markup(text)
        label.set_markup(f"<b>{markup}</b>" if header else markup)
        label.set_wrap(True)
        label.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
        _selectable_text(label, markup)
        label.set_hexpand(True)
        label.set_halign(Gtk.Align.FILL)
        label.set_valign(Gtk.Align.START)
        label.set_width_chars(1)
        label.set_max_width_chars(48)
        label.set_margin_start(6)
        label.set_margin_end(6)
        label.set_margin_top(4)
        label.set_margin_bottom(4)
        alignment = alignments[index] if index < len(alignments) else "left"
        label.set_xalign({"left": 0.0, "center": 0.5, "right": 1.0}[alignment])
        columns.append(label)

    row.set_child(columns)
    table.append(row)


# --- Inline markup ---------------------------------------------------------


# Order matters: code spans first so we don't apply bold/italic inside them.
def _inline_markup(text: str) -> str:
    # Escape Pango markup metacharacters before we apply our own.
    safe = (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )
    # Strip the sentinel control char so model text can't forge a placeholder
    # index that restoration would later expand. (The collision-prone bare-int
    # placeholders are long gone; this guards the \x01-delimited sentinels.)
    safe = safe.replace("\x00", "").replace("\x01", "")
    # We'll need to protect text inside code spans from further substitutions.
    placeholders: list[str] = []

    def stash(s: str) -> str:
        placeholders.append(s)
        return f"\x01{len(placeholders) - 1}\x01"

    # `code` spans
    def code_repl(m):
        return stash(f'<tt>{m.group(1)}</tt>')

    safe = re.sub(r"`([^`]+)`", code_repl, safe)

    # Bold (**...** or __...__) — non-greedy, no line break
    safe = re.sub(r"\*\*([^*]+?)\*\*", r"<b>\1</b>", safe)
    safe = re.sub(r"__([^_]+?)__", r"<b>\1</b>", safe)

    # Italic (*...* or _..._) — careful not to match remaining stars
    safe = re.sub(r"(?<![*\w])\*([^*\n]+?)\*(?!\w)", r"<i>\1</i>", safe)
    safe = re.sub(r"(?<![_\w])_([^_\n]+?)_(?!\w)", r"<i>\1</i>", safe)

    # Links [text](url). Both label and url have already been escaped for
    # &<> by the upfront pass; here we additionally:
    #   * escape " and ' in the URL so a malicious link like
    #       [x](https://e.com/" foreground="red)
    #     can't break out of the href attribute and inject Pango markup
    #     (Gtk.Label use_markup) or arbitrary attributes.
    #   * drop obviously dangerous schemes (javascript:, data:, vbscript:)
    #     because Gtk.Label's link activation hands the URL to the user's
    #     `xdg-open`, which will happily launch most things.
    _BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")

    def link_repl(m):
        label = m.group(1)  # already &<>-escaped
        url = m.group(2)    # already &<>-escaped
        url_attr = url.replace('"', "&quot;").replace("'", "&apos;")
        if url_attr.strip().lower().startswith(_BAD_SCHEMES):
            return stash(label)  # render label as plain text, no link
        return stash(f'<a href="{url_attr}">{label}</a>')

    safe = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, safe)

    # Restore stashed placeholders.
    for i, s in enumerate(placeholders):
        safe = safe.replace(f"\x01{i}\x01", s)

    return safe
