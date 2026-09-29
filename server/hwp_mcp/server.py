"""MCP server exposing HWP/HWPX editing tools (stdio transport).

    python -m hwp_mcp [--profile full|basic]      (or HWP_MCP_PROFILE=basic)

"full" (default) offers every tool. "basic" offers the few tools a small local model needs to
find, change and check text, with shorter instructions and smaller results, so the tool list and
the answers fit a small context window.
"""

from __future__ import annotations

import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")  # keep stderr clean; library deprecation notices are not actionable here

from typing import Annotated, Any, Literal  # noqa: E402

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError as McpToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from pydantic import Field  # noqa: E402

from . import ops, reader  # noqa: E402
from .store import Store, ToolError  # noqa: E402

INSTRUCTIONS = """\
Tools for reading and editing Hangul word-processor documents (.hwpx and .hwp) in place.
These files are binary/zip: never read or write them as text; always use these tools.

Workflow
1. Find what to edit. Short document: hwp_read_document lists every paragraph with its address.
   Long document (it says so): hwp_outline for the section map, then hwp_read_document(range="s2.1")
   for one section, or hwp_search to find the paragraphs about a topic.
2. Edit with the other tools. Every edit is validated and saved to the file immediately
   (no open/save step). hwp_undo reverts the last edit of a file.
3. Check: each edit result shows the affected text as it is now. hwp_diff lists everything the
   last edit (or the whole session, since="session") changed - use it to confirm nothing else moved.
4. Inserting or deleting paragraphs renumbers the paragraphs after that point (the result says
   how); re-read before reusing other old addresses.

Addresses (0-based)
- p12            body paragraph 12;  p3-p8 range;  p* all body paragraphs
- t0.r1.c2       table 0, row 1, column 2 (all paragraphs in that cell)
- t0.r1.c2.p0    first paragraph inside that cell
- t0.r0.c*       whole row 0;  t0.r*.c1 whole column 1;  t0.r1-3.c0-2 a block
- Several addresses can be joined with commas: "p1,p4,t0.r0.c*"
- A merged cell is addressed by its top-left cell.

Units: font size in pt, lengths in mm, line spacing in %, colors as "#RRGGBB".
Formatting is split by kind: character look (font, size, bold, color...) -> hwp_format_text;
paragraph look (alignment, spacing, indents) -> hwp_set_paragraph_format; named styles ->
hwp_apply_style; cell background/borders -> hwp_format_cells. Text inside table cells is formatted
with the same text/paragraph tools using cell addresses.

Not covered by these tools (tab stops / table of contents leaders, fixed line spacing, paragraph
borders, footnotes, hyperlinks, equations, text boxes, floating images, cell diagonals, sections,
columns, memos, tracked changes, form fields): use the "hwp-direct" skill, which edits the file
with a Python script (hwp_mcp.api).
"""

BASIC_INSTRUCTIONS = """\
Tools for Hangul documents (.hwpx, .hwp). These files are binary: never read or write them as
text; always use these tools with the absolute file path.
1. Find: hwp_outline shows the sections of a long document. Then hwp_read_document(range="s2")
   reads one section, or hwp_search("words") finds the paragraphs about a topic.
2. Edit: hwp_replace_text, hwp_set_paragraph_text, hwp_insert_paragraph, hwp_delete_paragraphs,
   hwp_set_cell_text. Each edit is saved at once.
3. Check: read the "Now:" lines of the edit result, then hwp_diff shows everything the edit
   changed. If it is wrong, hwp_undo.
Addresses: p12 = paragraph 12; t0.r1.c2 = table 0, row 1, column 2. Inserting or deleting
paragraphs changes the numbers after that point; the edit result says how.
"""

# Tools of the "basic" profile: find, change and check text.
BASIC_TOOLS = {
    "hwp_outline", "hwp_read_document", "hwp_search", "hwp_get_paragraph", "hwp_replace_text",
    "hwp_set_paragraph_text", "hwp_insert_paragraph", "hwp_delete_paragraphs", "hwp_set_cell_text",
    "hwp_diff", "hwp_undo",
}
# Result size limits per profile: (read characters, diff characters, outline lines).
LIMITS = {"full": (reader.READ_BUDGET, reader.DIFF_BUDGET, reader.OUTLINE_MAX_LINES), "basic": (8000, 4000, 60)}


def _requested_profile() -> str:
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--profile" and i + 1 < len(args):
            return args[i + 1].lower()
        if a.startswith("--profile="):
            return a.split("=", 1)[1].lower()
    return os.environ.get("HWP_MCP_PROFILE", "full").lower()


PROFILE = _requested_profile()
if PROFILE not in LIMITS:
    sys.exit(f'hwp_mcp: unknown profile "{PROFILE}" (use "full" or "basic")')
READ_LIMIT, DIFF_LIMIT, OUTLINE_LINES = LIMITS[PROFILE]

mcp = FastMCP("hwp", instructions=BASIC_INSTRUCTIONS if PROFILE == "basic" else INSTRUCTIONS)
store = Store()

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

Path_ = Annotated[str, Field(description="Absolute path of the .hwpx or .hwp file.")]
Target = Annotated[str, Field(description='Paragraph address(es), e.g. "p3", "p2-p5", "t0.r0.c*", "p1,p4".')]
After = Annotated[
    str | None,
    Field(description='Where to insert: "end" (default), "start", or the address of the paragraph to insert '
          'after, e.g. "p4" or "t0.r1.c0.p0".'),
]


def _run(fn, *args, **kwargs) -> Any:
    try:
        result = fn(store, *args, **kwargs)
    except ToolError as exc:
        raise McpToolError(str(exc)) from None
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, indent=1)
    return result


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------
@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_create_document(
    path: Annotated[str, Field(description="Absolute path for the new file. The extension picks the format: .hwpx (recommended) or .hwp.")],
    overwrite: Annotated[bool, Field(description="Replace the file if it already exists.")] = False,
    paper: Annotated[Literal["A4", "A3", "A5", "B4", "B5", "Letter", "Legal"] | None, Field(description="Paper size (default A4).")] = None,
    orientation: Annotated[Literal["portrait", "landscape"] | None, Field(description="Page orientation (default portrait).")] = None,
    margin_left_mm: float | None = None,
    margin_right_mm: float | None = None,
    margin_top_mm: float | None = None,
    margin_bottom_mm: float | None = None,
) -> str:
    """Create a new, empty Hangul document (default A4 portrait, Hancom default styles such as
    바탕글, 본문, 개요 1-7). Then add content with hwp_insert_paragraph / hwp_insert_table / hwp_insert_image."""
    page = dict(paper=paper, orientation=orientation, margin_left_mm=margin_left_mm, margin_right_mm=margin_right_mm,
                margin_top_mm=margin_top_mm, margin_bottom_mm=margin_bottom_mm)
    return _run(ops.create_document, path, overwrite, page)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_read_document(
    path: Path_,
    range: Annotated[
        str | None,
        Field(description='Part to read. Omit for the start of the document. A section id from hwp_outline '
              '("s2", "s2.1"), paragraphs ("p10-p40", "p10-" to the end, "p10"), or a table ("t3", "t3.r20-").'),
    ] = None,
    max_chars: Annotated[int, Field(description="Truncate each paragraph's text to this many characters.", ge=20)] = 600,
    show_format: Annotated[bool, Field(description="Also show a short paragraph/character format summary per paragraph.")] = False,
) -> str:
    """List paragraphs as `p<N> [style] text`, with tables shown as `<table tN>` followed by their
    cell texts (`tN.r<R> | c0: ... | c1: ...`) and images as `<image gN>`. Use the addresses it prints
    in all other tools. Output is size-limited; when it stops early it says which range to read next."""
    return _run(reader.read_document, path, range, max_chars, show_format, READ_LIMIT)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_outline(
    path: Path_,
    section: Annotated[str | None, Field(description='Show only this section and its subsections, e.g. "s3".')] = None,
    depth: Annotated[int | None, Field(description="Heading levels to show (default: as many as fit).", ge=1, le=10)] = None,
) -> str:
    """Section map of the document: one line per heading with its section id (s1, s2.1, ...),
    paragraph range, and size. Use it to navigate long documents, then read one section with
    hwp_read_document(range="<section id>")."""
    return _run(reader.outline, path, section, depth, OUTLINE_LINES)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_search(
    path: Path_,
    query: Annotated[str, Field(description="Words to look for, e.g. \"예산 집행 실적\". Need not be exact.")],
    max_results: Annotated[int, Field(ge=1, le=50)] = 10,
) -> str:
    """Find the paragraphs and table rows most related to the query, best first, with their
    addresses and section. Word order and endings (조사) need not match; exact phrase matches are
    marked * and ranked first. When meaning search is enabled, parts with a similar meaning but
    different words are included too, marked ~. For exact character offsets use hwp_find_text."""
    return _run(reader.search, path, query, max_results)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_diff(
    path: Path_,
    since: Annotated[
        Literal["last", "session"],
        Field(description='"last": what the most recent edit changed. "session": everything changed since the '
              'first edit of this file in this session.'),
    ] = "last",
    against: Annotated[str | None, Field(description="Instead compare with this other file (e.g. the original copy).")] = None,
) -> str:
    """Report what changed: text changes (before -> after), added and deleted paragraphs, format
    changes (style, paragraph, character, cell) and page setup, with current addresses. Use it to
    verify an edit did what was intended and nothing else."""
    return _run(reader.diff, path, since, against, DIFF_LIMIT)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_get_paragraph(path: Path_, target: Annotated[str, Field(description='One paragraph address, e.g. "p3" or "t0.r1.c2.p0".')]) -> str:
    """Full detail of one paragraph: text, style, paragraph format (alignment, line spacing, indents,
    spacing) and every text run with its character offsets and character format (font, size, bold, color...)."""
    return _run(ops.get_paragraph, path, target)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_find_text(
    path: Path_,
    text: Annotated[str, Field(description="Exact text to search for.")],
    ignore_case: bool = False,
    max_results: Annotated[int, Field(ge=1, le=500)] = 50,
) -> str:
    """Find text in body paragraphs and table cells. Returns each match's paragraph address,
    character offsets [start, end) and surrounding context."""
    return _run(ops.find_text, path, text, ignore_case, max_results)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_save_as(
    path: Path_,
    new_path: Annotated[str, Field(description="Absolute path of the copy; .hwpx or .hwp (the extension picks the format).")],
    overwrite: bool = False,
) -> str:
    """Save a copy of the document under a new path, converting between .hwp and .hwpx if the
    extensions differ. The original file is not changed."""
    return _run(ops.save_as, path, new_path, overwrite)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_undo(path: Path_) -> str:
    """Revert the most recent edit made to this file through these tools (repeatable)."""
    return _run(ops.undo, path)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------
@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_insert_paragraph(
    path: Path_,
    text: Annotated[str, Field(description='Paragraph text. Each "\\n" starts a new paragraph; "\\t" is a tab.')],
    after: After = None,
    style: Annotated[str | None, Field(description='Paragraph style name or id, e.g. "본문", "개요 1". Default "바탕글" (Normal); inside a table cell the default copies the neighbouring paragraph.')] = None,
    like: Annotated[str | None, Field(description='Copy style, paragraph format and character format from this paragraph address instead of using style, e.g. "p5".')] = None,
) -> str:
    """Insert one or more new paragraphs. In a brand-new document the first call fills the empty
    first paragraph p0. Format the new text afterwards with hwp_format_text / hwp_set_paragraph_format."""
    return _run(ops.insert_paragraph, path, text, after, style, like)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_set_paragraph_text(
    path: Path_,
    target: Annotated[str, Field(description='One paragraph address, e.g. "p3" or "t0.r1.c2.p0".')],
    text: Annotated[str, Field(description='New text. "\\n" becomes a line break inside the same paragraph.')],
) -> str:
    """Replace the entire text of one paragraph. The new text takes the character format of the
    paragraph's first text run. To change only part of a paragraph, use hwp_replace_text."""
    return _run(ops.set_paragraph_text, path, target, text)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_replace_text(
    path: Path_,
    find: Annotated[str, Field(description="Exact text to replace.")],
    replace: Annotated[str, Field(description="Replacement text (may be empty to delete).")],
    target: Annotated[str | None, Field(description="Limit to these paragraph address(es); default the whole document including table cells.")] = None,
    ignore_case: bool = False,
    max_count: Annotated[int | None, Field(description="Replace at most this many occurrences (default all).", ge=1)] = None,
) -> str:
    """Find-and-replace text while keeping the surrounding character formatting."""
    return _run(ops.replace_text, path, find, replace, target, ignore_case, max_count)


@mcp.tool(annotations=DESTRUCTIVE, structured_output=False)
def hwp_delete_paragraphs(path: Path_, target: Target) -> str:
    """Delete paragraphs (including any table or image they hold). A section or table cell always keeps
    one paragraph; if every paragraph of one would be deleted, the last one is emptied instead."""
    return _run(ops.delete_paragraphs, path, target)


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_format_text(
    path: Path_,
    target: Target,
    match: Annotated[str | None, Field(description="Format only this exact text inside the target paragraph(s). Omit (with start/end) to format the whole paragraph(s).")] = None,
    occurrence: Annotated[int | None, Field(description="With match: format only the Nth occurrence (1-based, counted across the target). Default all occurrences.", ge=1)] = None,
    start: Annotated[int | None, Field(description="Alternative to match: first character offset (0-based, single paragraph only; see hwp_get_paragraph/hwp_find_text).", ge=0)] = None,
    end: Annotated[int | None, Field(description="Alternative to match: end offset (exclusive).", ge=1)] = None,
    font: Annotated[str | None, Field(description='Font name, e.g. "맑은 고딕", "함초롬바탕", "굴림".')] = None,
    size_pt: Annotated[float | None, Field(description="Font size in points, e.g. 10, 12, 16.")] = None,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
    strikethrough: bool | None = None,
    color: Annotated[str | None, Field(description='Text color "#RRGGBB".')] = None,
    highlight: Annotated[str | None, Field(description='Background highlight "#RRGGBB", or "none" to remove.')] = None,
    superscript: bool | None = None,
    subscript: bool | None = None,
    char_width_percent: Annotated[int | None, Field(description="Character width (장평) in %, 50-200; 100 is normal.")] = None,
    letter_spacing_percent: Annotated[int | None, Field(description="Letter spacing (자간) in %, -50 to 50; 0 is normal.")] = None,
) -> str:
    """Set character formatting (글자 모양) on whole paragraphs or on a part selected by `match` or
    `start`/`end`. Only the options you pass change; everything else is kept. Pass false to turn
    bold/italic/underline/strikethrough off."""
    opts = dict(font=font, size_pt=size_pt, bold=bold, italic=italic, underline=underline, strikethrough=strikethrough,
                color=color, highlight=highlight, superscript=superscript, subscript=subscript,
                char_width_percent=char_width_percent, letter_spacing_percent=letter_spacing_percent)
    return _run(ops.format_text, path, target, match, occurrence, start, end, opts)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_set_paragraph_format(
    path: Path_,
    target: Target,
    align: Literal["left", "center", "right", "justify", "distribute"] | None = None,
    line_spacing_percent: Annotated[float | None, Field(description="Line spacing in % (Hancom default 160).")] = None,
    space_before_pt: Annotated[float | None, Field(description="Space above the paragraph in pt.")] = None,
    space_after_pt: Annotated[float | None, Field(description="Space below the paragraph in pt.")] = None,
    indent_left_mm: Annotated[float | None, Field(description="Left indent (왼쪽 여백) in mm.")] = None,
    indent_right_mm: Annotated[float | None, Field(description="Right indent (오른쪽 여백) in mm.")] = None,
    first_line_indent_mm: Annotated[float | None, Field(description="First-line indent (들여쓰기) in mm; negative for a hanging indent (내어쓰기).")] = None,
    keep_with_next: Annotated[bool | None, Field(description="Keep on the same page as the next paragraph.")] = None,
    keep_lines: Annotated[bool | None, Field(description="Do not split the paragraph across pages.")] = None,
    page_break_before: Annotated[bool | None, Field(description="Start this paragraph on a new page.")] = None,
) -> str:
    """Set paragraph formatting (문단 모양): alignment, line spacing, spacing before/after, indents,
    page-break options. Only the options you pass change. Works on body and table-cell paragraphs."""
    opts = dict(align=align, line_spacing_percent=line_spacing_percent, space_before_pt=space_before_pt,
                space_after_pt=space_after_pt, indent_left_mm=indent_left_mm, indent_right_mm=indent_right_mm,
                first_line_indent_mm=first_line_indent_mm, keep_with_next=keep_with_next, keep_lines=keep_lines,
                page_break_before=page_break_before)
    return _run(ops.set_paragraph_format, path, target, opts)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_list_styles(path: Path_) -> str:
    """List the document's styles (스타일) with id, name, type and a format summary. Styles marked
    auto_numbering add their own numbers/bullets (e.g. 개요 1 -> "1."), so do not type numbers into such paragraphs."""
    return _run(ops.list_styles, path)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_apply_style(
    path: Path_,
    target: Target,
    style: Annotated[str, Field(description='Style name or id from hwp_list_styles, e.g. "개요 1", "본문", "0".')],
    keep_char_format: Annotated[bool, Field(description="Keep existing character formatting instead of resetting it to the style's.")] = False,
) -> str:
    """Apply a named style to paragraphs. Like Hancom, this resets their paragraph and character format
    to the style's (unless keep_char_format=true)."""
    return _run(ops.apply_style, path, target, style, keep_char_format)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_create_style(
    path: Path_,
    name: Annotated[str, Field(description="New style name.")],
    base_style: Annotated[str | None, Field(description='Style to start from (default "바탕글").')] = None,
    font: str | None = None,
    size_pt: float | None = None,
    bold: bool | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
    color: Annotated[str | None, Field(description='"#RRGGBB"')] = None,
    align: Literal["left", "center", "right", "justify", "distribute"] | None = None,
    line_spacing_percent: float | None = None,
    space_before_pt: float | None = None,
    space_after_pt: float | None = None,
    indent_left_mm: float | None = None,
    first_line_indent_mm: float | None = None,
) -> str:
    """Create a new paragraph style from a base style plus character and paragraph settings, so it can be
    applied consistently with hwp_apply_style."""
    char_opts = dict(font=font, size_pt=size_pt, bold=bold, italic=italic, underline=underline, color=color)
    para_opts = dict(align=align, line_spacing_percent=line_spacing_percent, space_before_pt=space_before_pt,
                     space_after_pt=space_after_pt, indent_left_mm=indent_left_mm, first_line_indent_mm=first_line_indent_mm)
    return _run(ops.create_style, path, name, base_style, char_opts, para_opts)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_set_list(
    path: Path_,
    target: Annotated[str, Field(description='Body paragraph address(es), e.g. "p3-p7".')],
    kind: Annotated[Literal["bullet", "number", "none"], Field(description='"bullet", "number", or "none" to remove list formatting.')],
    level: Annotated[int, Field(description="List level 1-7 (1 = outermost).", ge=1, le=7)] = 1,
    bullet_char: Annotated[str | None, Field(description='Bullet character, e.g. "•", "○", "-", "※". Default "•".')] = None,
    number_format: Annotated[str | None, Field(description='Numbering style: "1", "가", "a", "A", "i", "I", "①", "ㄱ". Default "1".')] = None,
    start_number: Annotated[int | None, Field(description="First number of a numbered list.", ge=0)] = None,
) -> str:
    """Turn body paragraphs into a bulleted or numbered list (글머리표/문단 번호), or remove it.
    Do not type the bullet or number characters into the text yourself."""
    return _run(ops.set_list, path, target, kind, level, bullet_char, number_format, start_number)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------
@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_insert_table(
    path: Path_,
    after: After = None,
    rows: Annotated[int | None, Field(description="Number of rows (optional when data is given).", ge=1)] = None,
    cols: Annotated[int | None, Field(description="Number of columns (optional when data is given).", ge=1)] = None,
    data: Annotated[list[list[str | int | float | None]] | None, Field(description='Cell texts row by row, e.g. [["품목","수량"],["사과","3"]]. "\\n" in a text makes separate paragraphs in the cell.')] = None,
    column_widths_mm: Annotated[list[float] | None, Field(description="Width of each column in mm (default: equal columns filling the text width).")] = None,
    header_row: Annotated[bool, Field(description="Style row 0 as a header: gray fill, bold, centered.")] = False,
) -> str:
    """Insert a table (with solid 0.12 mm borders) after a body paragraph. Returns its table index tN.
    Then use hwp_set_cell_text, hwp_merge_cells, hwp_format_cells, hwp_table_structure, hwp_set_table_layout."""
    return _run(ops.insert_table, path, after, rows, cols, data, column_widths_mm, header_row)


@mcp.tool(annotations=READ, structured_output=False)
def hwp_get_table(
    path: Path_,
    table: Annotated[int, Field(description="Table index N from tN.", ge=0)],
    include_format: Annotated[bool, Field(description="Include each cell's fill, borders, alignment and text format.")] = False,
) -> str:
    """Show one table: size, column widths, row heights and every cell (address, text, merged range)."""
    return _run(ops.get_table_info, path, table, include_format)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_set_cell_text(
    path: Path_,
    table: Annotated[int, Field(description="Table index N from tN.", ge=0)],
    data: Annotated[list[list[str | int | float | None]], Field(description='Texts as rows, written starting at (start_row, start_col). One cell: [["text"]]. null leaves a cell unchanged. "\\n" makes separate paragraphs.')],
    start_row: Annotated[int, Field(ge=0)] = 0,
    start_col: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """Write text into table cells (replacing their text, keeping their format). Merged cells take the
    value at their top-left position."""
    return _run(ops.set_cell_text, path, table, data, start_row, start_col)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_merge_cells(path: Path_, cells: Annotated[str, Field(description='Rectangular cell range, e.g. "t0.r0.c0-2" (row 0, columns 0-2) or "t0.r1-3.c0".')]) -> str:
    """Merge a rectangular range of cells into one (셀 합치기). The merged cell keeps the top-left address."""
    return _run(ops.merge_cells, path, cells)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_split_cell(path: Path_, cell: Annotated[str, Field(description='Address of a merged cell, e.g. "t0.r0.c0".')]) -> str:
    """Split a merged cell back into its original single cells (셀 나누기)."""
    return _run(ops.split_cell, path, cell)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_format_cells(
    path: Path_,
    cells: Annotated[str, Field(description='Cell address(es), e.g. "t0.r0.c*" (row 0), "t0.r*.c*" (all), "t0.r1-3.c2".')],
    fill_color: Annotated[str | None, Field(description='Background "#RRGGBB", or "none" to remove.')] = None,
    border_type: Annotated[Literal["solid", "none", "dash", "dot", "dash_dot", "long_dash", "double", "thick_thin", "thin_thick"] | None, Field(description='Line type; "none" removes the lines.')] = None,
    border_width_mm: Annotated[float | None, Field(description="Line width in mm (0.1-5; common 0.12, 0.4).")] = None,
    border_color: Annotated[str | None, Field(description='Line color "#RRGGBB" (default black).')] = None,
    borders: Annotated[str, Field(description='Which lines the border_* settings apply to: "all" (default), "outer" (box around the range), "inner" (lines between cells), or a comma list of "left,right,top,bottom".')] = "all",
    vertical_align: Literal["top", "middle", "bottom"] | None = None,
    padding_mm: Annotated[float | None, Field(description="Inner cell margin on all sides in mm.")] = None,
) -> str:
    """Format table cells (셀 테두리/배경): background color, border lines, vertical alignment, padding.
    For the text inside cells use hwp_format_text / hwp_set_paragraph_format with the same cell addresses."""
    return _run(ops.format_cells, path, cells, fill_color=fill_color, border_type=border_type,
                border_width_mm=border_width_mm, border_color=border_color, borders=borders,
                vertical_align=vertical_align, padding_mm=padding_mm)


@mcp.tool(annotations=DESTRUCTIVE, structured_output=False)
def hwp_table_structure(
    path: Path_,
    table: Annotated[int, Field(description="Table index N from tN.", ge=0)],
    action: Literal["insert_row_above", "insert_row_below", "delete_row", "insert_column_left", "insert_column_right", "delete_column", "delete_table"],
    index: Annotated[int | None, Field(description="Row or column index the action refers to (not needed for delete_table).", ge=0)] = None,
    count: Annotated[int, Field(description="How many rows/columns to insert or delete.", ge=1)] = 1,
) -> str:
    """Insert or delete rows/columns, or delete the whole table. New rows/columns copy the format of the
    row/column at `index` and start empty. Merged cells crossing the insertion point are extended."""
    return _run(ops.table_structure, path, table, action, index, count)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_set_table_layout(
    path: Path_,
    table: Annotated[int, Field(description="Table index N from tN.", ge=0)],
    column_widths_mm: Annotated[list[float] | None, Field(description="Width of every column in mm (one value per column).")] = None,
    row_heights_mm: Annotated[list[float] | None, Field(description="Minimum height of every row in mm (one value per row).")] = None,
    align: Annotated[Literal["left", "center", "right"] | None, Field(description="Horizontal position of the table.")] = None,
    repeat_header_row: Annotated[bool | None, Field(description="Repeat row 0 at the top of each page.")] = None,
) -> str:
    """Set a table's column widths, row heights, horizontal alignment and header-row repetition."""
    return _run(ops.set_table_layout, path, table, column_widths_mm=column_widths_mm, row_heights_mm=row_heights_mm,
                align=align, repeat_header_row=repeat_header_row)


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------
@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_insert_image(
    path: Path_,
    image_path: Annotated[str, Field(description="Absolute path of a PNG, JPEG, GIF or BMP file.")],
    after: After = None,
    width_mm: Annotated[float | None, Field(description="Width in mm. If only one of width/height is given, the other keeps the aspect ratio. Default: natural size, limited to the text width.")] = None,
    height_mm: float | None = None,
    align: Literal["left", "center", "right"] = "center",
) -> str:
    """Insert a picture in its own new paragraph (in the body or inside a table cell via a cell paragraph
    address). The image is embedded in the file. Returns its image index gN."""
    return _run(ops.insert_image, path, image_path, after, width_mm, height_mm, align)


@mcp.tool(annotations=DESTRUCTIVE, structured_output=False)
def hwp_edit_image(
    path: Path_,
    image: Annotated[int, Field(description="Image index N from gN (see hwp_read_document).", ge=0)],
    width_mm: Annotated[float | None, Field(description="New width in mm (height follows the aspect ratio if not given).")] = None,
    height_mm: float | None = None,
    delete: Annotated[bool, Field(description="Delete the image instead of resizing it.")] = False,
) -> str:
    """Resize or delete an existing image."""
    return _run(ops.edit_image, path, image, width_mm, height_mm, delete)


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------
@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_page_setup(
    path: Path_,
    section: Annotated[int, Field(description="Section index (most documents have only section 0).", ge=0)] = 0,
    paper: Literal["A4", "A3", "A5", "B4", "B5", "Letter", "Legal"] | None = None,
    orientation: Literal["portrait", "landscape"] | None = None,
    margin_left_mm: float | None = None,
    margin_right_mm: float | None = None,
    margin_top_mm: float | None = None,
    margin_bottom_mm: float | None = None,
    header_margin_mm: float | None = None,
    footer_margin_mm: float | None = None,
    columns: Annotated[int | None, Field(description="Number of text columns (다단).", ge=1, le=10)] = None,
    column_gap_mm: float | None = None,
) -> str:
    """Set paper size, orientation, page margins and text columns (편집 용지)."""
    page = dict(paper=paper, orientation=orientation, margin_left_mm=margin_left_mm, margin_right_mm=margin_right_mm,
                margin_top_mm=margin_top_mm, margin_bottom_mm=margin_bottom_mm, header_margin_mm=header_margin_mm,
                footer_margin_mm=footer_margin_mm, columns=columns, column_gap_mm=column_gap_mm)
    return _run(ops.page_setup, path, section, page)


@mcp.tool(annotations=EDIT, structured_output=False)
def hwp_set_header_footer(
    path: Path_,
    kind: Literal["header", "footer"],
    text: Annotated[str | None, Field(description="Header/footer text.")] = None,
    page_number: Annotated[Literal["number", "dash", "page_of_total"] | None, Field(description='Add an automatic page number: "number" (1), "dash" (- 1 -), "page_of_total" (1 / 10). Combined with text if both are given.')] = None,
    align: Literal["left", "center", "right"] = "center",
    section: Annotated[int, Field(ge=0)] = 0,
    remove: Annotated[bool, Field(description="Remove the header/footer instead.")] = False,
) -> str:
    """Set (or remove) the page header (머리말) or footer (꼬리말), optionally with an automatic page number."""
    return _run(ops.header_footer, path, kind, text, page_number, align, section, remove)


if PROFILE == "basic":
    for _name in [n for n in globals() if n.startswith("hwp_")]:  # every tool is a function named after it
        if _name not in BASIC_TOOLS:
            mcp.remove_tool(_name)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    mcp.run("stdio")


if __name__ == "__main__":
    main()
