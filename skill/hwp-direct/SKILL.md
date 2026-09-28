---
name: hwp-direct
description: Edit or create Hangul documents (.hwpx/.hwp) with a Python script when the hwp_* MCP tools are not enough - tabs/table of contents with dot leaders, fixed line spacing, paragraph borders/shading, footnotes/endnotes, hyperlinks, bookmarks, equations, text boxes, floating images, diagonal/vertical table cells, sections with different page setup, multi-column parts, memos, tracked changes, form fields (누름틀), character effects - or when many edits should be done in one pass. Use for any 한글/HWP/HWPX task that needs finer control than the MCP tools.
---

# Direct HWP/HWPX editing with Python (hwp_mcp.api)

## When to use what

| Situation | Use |
|---|---|
| Ordinary edits: text, character/paragraph format, styles, lists, tables, images, page setup, header/footer | the `hwp_*` MCP tools |
| A feature the MCP tools lack (see the description above), or 10+ edits in one go, or a large document | **this skill**: a Python script using `hwp_mcp.api` |

Do not mix both on the same file at the same time: finish the script (it saves once), then MCP tools may continue.

## How to run a script

Python with the library installed (created by the VS Code extension):
- bash: `PYTHONIOENCODING=utf-8 ~/.hwp-mcp/venv/Scripts/python.exe script.py`
- PowerShell: `$env:PYTHONIOENCODING="utf-8"; & "$env:USERPROFILE\.hwp-mcp\venv\Scripts\python.exe" script.py`

(On macOS/Linux the interpreter is `~/.hwp-mcp/venv/bin/python`.) Write the script to a scratch file as UTF-8.

## Workflow (always)

1. **Look first.** Run `python -m hwp_mcp.check FILE --format` (prints validity + every paragraph with its address and format). Addresses: `p12` body paragraph, `t0.r1.c2` table cell, `t0.r1.c2.p0` paragraph in a cell, `t0.r0.c*` whole row, `p3-p8` range.
2. **Write one script**: open → edits → `doc.save()`.
3. **Verify**: `python -m hwp_mcp.check FILE --format --png preview.png`, then look at the PNG. The PNG is an approximate preview (no table borders, images show as placeholders, no bullets/auto numbers); use it to check order, text and emphasis, not exact layout.

```python
from hwp_mcp.api import HwpDoc, HwpError

doc = HwpDoc.open(r"C:\docs\report.hwpx")        # new file: HwpDoc.new(path, overwrite=True)
print(doc.read(show_format=True))                  # optional: see addresses inside the script
doc.insert_paragraph("목차", style="개요 1", after="p0")
doc.set_tabs("p2-p4", [{"pos_mm": doc.text_width_mm(), "type": "right", "leader": "dot"}])
print(doc.save())                                  # validates; raises and writes nothing if invalid
```

## Rules - follow them exactly

1. **Never catch `HwpError`.** If anything raises, the file on disk is untouched. Read the message, fix the script, run the whole script again.
2. **Addresses shift.** Inserting/deleting paragraphs renumbers everything after that point. Either re-read (`doc.read()`), work from the end of the document toward the start, or use the address printed in the returned message (`"Inserted 1 paragraph(s) at p7 ..."`).
3. **Units:** font size pt, lengths mm, line spacing %, colors `"#RRGGBB"`. Raw XML uses HWPUNIT: 1 mm = 283.465, 1 pt = 100.
4. **Styles 개요 1-7 and lists number themselves.** Never type "1." or "•" into such paragraphs.
5. **Tab positions are measured from the left margin** and must be ≤ `doc.text_width_mm()` (a right stop at the margin = exactly that value).
6. **Raw XML:** never change a shared definition in the header (charPr/paraPr/borderFill/tabPr) in place - other paragraphs use it too. Use `doc.derive(...)` or `doc.modify_para_pr / modify_char_pr / modify_cells`. After editing a paragraph's XML by hand call `doc.touch(paragraph)`. See [reference.md](reference.md) before any raw XML work.
7. **.hwp files** are edited through conversion; if `HwpDoc.open(...)` edits raise "content the converter could not carry over", save a copy first: `HwpDoc.open(src).save(r"...\copy.hwpx")`, then edit the copy.
8. Charts are not supported natively: draw the chart as a PNG (e.g. with matplotlib if installed) and `insert_image` it.

## API

### Same as the MCP tools (no `path` argument, no `hwp_` prefix)
`read(start=0, limit=300, max_chars=400, show_format=False)`, `get_paragraph(t)`, `find_text(text)`,
`insert_paragraph(text, after=None|"start"|"end"|addr, style=None, like=None)` ("\n" = new paragraph, "\t" = tab),
`set_paragraph_text(t, text)`, `replace_text(find, replace, target=None)`, `delete_paragraphs(t)`,
`format_text(t, match=None, occurrence=None, start=None, end=None, font=, size_pt=, bold=, italic=, underline=, strikethrough=, color=, highlight=, superscript=, subscript=, char_width_percent=, letter_spacing_percent=)`,
`set_paragraph_format(t, align=, line_spacing_percent=, space_before_pt=, space_after_pt=, indent_left_mm=, indent_right_mm=, first_line_indent_mm=, keep_with_next=, keep_lines=, page_break_before=)`,
`list_styles()`, `apply_style(t, style)`, `create_style(name, base_style=None, **format)`, `set_list(t, "bullet"|"number"|"none", level=1, bullet_char=, number_format=)`,
`insert_table(after=, rows=, cols=, data=[[..]], column_widths_mm=[..], header_row=False)`, `get_table(i)`, `set_cell_text(i, [[..]], start_row=0, start_col=0)`, `merge_cells("t0.r0.c0-2")`, `split_cell(addr)`,
`format_cells(cells, fill_color=, border_type=, border_width_mm=, border_color=, borders="all"|"outer"|"inner"|"left,top", vertical_align=, padding_mm=)`,
`table_structure(i, "insert_row_below"|"insert_row_above"|"delete_row"|"insert_column_right"|"insert_column_left"|"delete_column"|"delete_table", index, count=1)`,
`set_table_layout(i, column_widths_mm=, row_heights_mm=, align=, repeat_header_row=)`,
`insert_image(path, after=, width_mm=, height_mm=, align="center")`, `edit_image(g, width_mm=, height_mm=, delete=False)`,
`page_setup(section=0, paper=, orientation=, margin_*_mm=, header_margin_mm=, footer_margin_mm=, columns=, column_gap_mm=)`,
`set_header_footer("header"|"footer", text=None, page_number=None|"number"|"dash"|"page_of_total", align="center")`.

Extra `format_text` options: `shadow_color="#A0A0A0"`, `outline="SOLID"`, `emboss=True`, `engrave=True`, `underline_shape="WAVE"|"DOUBLE_SLIM"|"DOT"|...`, `underline_color=`.

### Advanced helpers (only here)
| Call | Effect |
|---|---|
| `set_tabs(t, [{"pos_mm": 150, "type": "right", "leader": "dot"}])` | tab stops; types left/right/center/decimal; leaders none/dot/dash/solid/... ; `[]` clears |
| `set_line_spacing(t, 20, kind="fixed")` | kind percent (%), fixed / at_least / between_lines (pt) |
| `set_paragraph_border(t, border_type="solid", border_width_mm=0.12, border_color=, fill_color=None, sides="all", padding_mm=1)` | box/shading around paragraphs; consecutive ones join |
| `add_footnote(t, text, after_match=None, endnote=False)` | 각주/미주 mark after the matched text (default paragraph end) |
| `add_hyperlink(t, url, match="text")` / `add_hyperlink(t, url, text="new")` | link existing text / append link text |
| `add_bookmark(t, name, at="start")` | 책갈피 |
| `add_equation(latex, target=None, after=None, size_pt=11)` | inline at end of `target`, else new centered paragraph after `after`; LaTeX subset (`\frac`, `\sqrt`, `^`, `_`, `\sum`, Greek, ...) or `script=` Hancom EqEdit |
| `add_textbox(text, after=, width_mm=80, height_mm=20, border_color=, fill_color=, padding_mm=2, vertical_align="center", align="left", floating=None)` | 글상자; `floating={"x_mm":..,"y_mm":..,"relative_to":"paper"/"page"/"para","wrap":"square"/"top_and_bottom"/"behind_text"/"in_front_of_text"}` |
| `float_image(g, x_mm=, y_mm=, relative_to="para", wrap="square")` | make image gN floating |
| `set_cell_options(cells, diagonal="slash"/"backslash"/"cross"/"none", padding_mm=3 or {"left":..}, text_direction="vertical")` | cell diagonal, per-side padding, 세로쓰기 |
| `add_section(**page)` | new section (starts a new page) at the end, e.g. `orientation="landscape"`; returns its index; `insert_paragraph()` then writes into it |
| `set_columns(t, 2, gap_mm=8, separator="SOLID")` | 다단 from paragraph t on (1 = back to one column) |
| `add_memo(t, text, author=)` | 메모 |
| `track_insert(t, text)`, `track_delete(t, match)`, `track_replace(t, old, new)` | 변경 추적 marks |
| `add_form_field(t, name, prompt=)`, `fill_form_field(name, value)` | 누름틀 |
| `text_width_mm(section=0)`, `last_paragraph()` | text area width; address of the last body paragraph |
| `check()` / `save(path=None)` / `preview_png(out)` | validate / write (new path = save as, .hwp or .hwpx) / approximate PNG |

### Raw access (read [reference.md](reference.md) first)
`doc.raw` (python-hwpx `HwpxDocument`), `doc.header`, `doc.paragraph(addr)` / `doc.paragraphs(addr)` (objects with `.element`, lxml), `doc.table(i)`, `doc.cells(addr)`, `doc.image_element(g)`, `doc.get_def(kind, id)` (read-only),
`doc.derive(kind, base_id, modify) -> new_id`, `doc.modify_para_pr(t, fn)`, `doc.modify_char_pr(t, fn, match=...)`, `doc.modify_cells(cells, border_fill=fn, cell=fn)`, `doc.touch(paragraph)`. Namespaces: `from hwp_mcp.api import HP, HH, HC`.

## Recipes

**Table of contents with dot leaders**
```python
doc.insert_paragraph("1. 서론\t3\n2. 사업 현황\t5\n3. 향후 계획\t9", after="p1")   # -> p2-p4
doc.set_tabs("p2-p4", [{"pos_mm": doc.text_width_mm(), "type": "right", "leader": "dot"}])
```

**Emphasis box + fixed line spacing**
```python
doc.set_paragraph_border("p5", border_color="#2E75B6", fill_color="#DEEAF6", border_width_mm=0.4, padding_mm=2)
doc.set_line_spacing("p6-p9", 18, kind="fixed")
```

**Footnote, link, equation**
```python
doc.add_footnote("p3", "출처: 통계청(2026)", after_match="18.4%")
doc.add_hyperlink("p3", "https://kostat.go.kr", match="통계청")
doc.add_equation(r"\frac{a+b}{2} \geq \sqrt{ab}", after="p3")
```

**Header cell with a diagonal, vertical text in the first column**
```python
doc.set_cell_options("t0.r0.c0", diagonal="backslash")
doc.set_cell_options("t0.r1-4.c0", text_direction="vertical")
```

**Landscape appendix section with two columns**
```python
doc.add_section(orientation="landscape")
doc.insert_paragraph("부록")           # fills the new section's empty first paragraph
doc.set_columns(doc.last_paragraph(), 2, separator="SOLID")
```

**Raw: letter spacing -5% only on one word (clone-and-modify)**
```python
doc.modify_char_pr("p2", lambda cp: cp.find(f"{HH}spacing").set("hangul", "-5"), match="핵심 지표")
```
