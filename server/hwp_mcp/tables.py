"""Table structure edits, sizing and cell formatting."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lxml import etree as ET

from hwpx import HwpxDocument
from hwpx import table_patch as tp
from hwpx.oxml import HwpxOxmlTable

from . import formats
from .model import (
    HP, P_TAG, RUN_TAG, T_TAG, CellTarget, cell_grid, clear_layout_cache, first_char_pr, get_table,
    hu_to_mm, mm_to_hu, normalize_color, table_cells,
)
from .store import ToolError


def _tstr(tbl_el) -> str:
    return ET.tostring(tbl_el, encoding="unicode")


def _replace_table(table: HwpxOxmlTable, new_xml: str) -> None:
    try:
        tp._validate_or_raise(new_xml)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Refused: the table grid would become invalid ({exc}).") from exc
    new_el = ET.fromstring(new_xml)
    old = table.element
    old.getparent().replace(old, new_el)
    table.paragraph.section.mark_dirty()


def _structure(table: HwpxOxmlTable, fn, *args) -> None:
    try:
        new_xml = fn(_tstr(table.element), *args)
    except tp.TableStructureError as exc:
        raise ToolError(f"Refused: {exc}") from exc
    _replace_table(table, new_xml)


def _blank_cell(tc_el) -> None:
    sub = tc_el.find(f"{HP}subList")
    if sub is None:
        return
    paras = sub.findall(P_TAG)
    for extra in paras[1:]:
        sub.remove(extra)
    if not paras:
        return
    p = paras[0]
    runs = [r for r in p if r.tag == RUN_TAG]
    for run in runs:
        for child in list(run):
            if child.tag == T_TAG:
                run.remove(child)
    for run in runs[1:]:
        if len(run) == 0:
            p.remove(run)
    if runs:
        runs[0].append(ET.Element(T_TAG))
    clear_layout_cache(p)


def _tc_rows(tbl_el) -> list[tuple[Any, int, int, int, int]]:
    out = []
    for tc in tbl_el.iter(f"{HP}tc"):
        if tc.getparent().getparent() is not tbl_el:
            continue  # nested table cell
        addr, span = tc.find(f"{HP}cellAddr"), tc.find(f"{HP}cellSpan")
        out.append((tc, int(addr.get("rowAddr")), int(addr.get("colAddr")),
                    int(span.get("rowSpan", 1)), int(span.get("colSpan", 1))))
    return out


def _require_flat(table: HwpxOxmlTable) -> None:
    if len(list(table.element.iter(f"{HP}tbl"))) > 1:
        raise ToolError("Structure edits are not supported for tables that contain nested tables.")


def insert_rows(doc: HwpxDocument, ti: int, index: int, count: int, above: bool) -> str:
    table = get_table(doc, ti)
    _require_flat(table)
    rows = table.row_count
    if not 0 <= index < rows:
        raise ToolError(f"Row {index} does not exist in t{ti} (rows 0-{rows - 1}).")
    if not 1 <= count <= 100:
        raise ToolError("count must be between 1 and 100.")
    has_vmerge = any(rs > 1 for _tc, _r, _c, rs, _cs in _tc_rows(table.element))
    if not above:
        _structure(table, tp._insert_row_by_clone, index, count)
        first_new = index + 1
    elif not has_vmerge:
        # Clone the row itself (so the new rows take its format), then move the clones above it.
        _structure(table, tp._insert_row_by_clone, index, count)
        order = list(range(rows + count))
        order = order[:index] + order[index + 1:index + 1 + count] + [index] + order[index + 1 + count:]
        _structure(get_table(doc, ti), tp._reorder_rows, order)
        first_new = index
    elif index > 0:
        # Rows cannot be reordered across vertical merges: clone the previous row instead.
        _structure(table, tp._insert_row_by_clone, index - 1, count)
        first_new = index
    else:
        raise ToolError("Cannot insert above row 0 of a table with vertically merged cells; insert below row 0 instead.")
    table = get_table(doc, ti)
    for tc, r, _c, _rs, _cs in _tc_rows(table.element):
        if first_new <= r < first_new + count:
            _blank_cell(tc)
    return f"Inserted {count} empty row(s) at row {first_new} of t{ti}; the table now has {table.row_count} rows."


def delete_rows(doc: HwpxDocument, ti: int, index: int, count: int) -> str:
    table = get_table(doc, ti)
    _require_flat(table)
    rows = list(range(index, index + count))
    if index < 0 or rows[-1] >= table.row_count:
        raise ToolError(f"Rows {index}-{rows[-1]} are out of range for t{ti} (rows 0-{table.row_count - 1}).")
    if count >= table.row_count:
        raise ToolError("Cannot delete every row; delete the table instead (action \"delete_table\").")
    _structure(table, tp._delete_rows, rows)
    return f"Deleted row(s) {index}-{rows[-1]} of t{ti}; {get_table(doc, ti).row_count} rows remain."


def delete_columns(doc: HwpxDocument, ti: int, index: int, count: int) -> str:
    table = get_table(doc, ti)
    _require_flat(table)
    cols = list(range(index, index + count))
    if index < 0 or cols[-1] >= table.column_count:
        raise ToolError(f"Columns {index}-{cols[-1]} are out of range for t{ti} (columns 0-{table.column_count - 1}).")
    if count >= table.column_count:
        raise ToolError("Cannot delete every column; delete the table instead (action \"delete_table\").")
    _structure(table, lambda t, c: tp._collapse_empty_rows(tp._delete_columns(t, c)), cols)
    return f"Deleted column(s) {index}-{cols[-1]} of t{ti}; {get_table(doc, ti).column_count} columns remain."


def column_widths(table: HwpxOxmlTable) -> list[int]:
    ncol = table.column_count
    widths: dict[int, int] = {}
    cells = _tc_rows(table.element)
    for tc, _r, c, _rs, cs in cells:
        if cs == 1:
            widths.setdefault(c, int(tc.find(f"{HP}cellSz").get("width")))
    for tc, _r, c, _rs, cs in cells:
        span = range(c, c + cs)
        unknown = [x for x in span if x not in widths]
        if cs > 1 and unknown:
            rest = int(tc.find(f"{HP}cellSz").get("width")) - sum(widths.get(x, 0) for x in span)
            for x in unknown:
                widths[x] = max(1, rest // len(unknown))
    return [widths.get(c, 0) for c in range(ncol)]


def row_heights(table: HwpxOxmlTable) -> list[int]:
    heights: dict[int, int] = {}
    for tc, r, _c, rs, _cs in _tc_rows(table.element):
        if rs == 1:
            heights.setdefault(r, int(tc.find(f"{HP}cellSz").get("height")))
    return [heights.get(r, 0) for r in range(table.row_count)]


def insert_columns(doc: HwpxDocument, ti: int, index: int, count: int, left: bool) -> str:
    table = get_table(doc, ti)
    _require_flat(table)
    ncol = table.column_count
    if not 0 <= index < ncol:
        raise ToolError(f"Column {index} does not exist in t{ti} (columns 0-{ncol - 1}).")
    if not 1 <= count <= 50:
        raise ToolError("count must be between 1 and 50.")
    tbl = deepcopy(table.element)
    widths = column_widths(table)
    k = index if left else index + 1  # first new column index
    ref_col = index
    new_w = widths[ref_col] or 1000
    total_before = sum(widths) or 1
    cells = _tc_rows(tbl)
    original = {(r, c): (tc, r, c, rs, cs) for tc, r, c, rs, cs in cells}
    anchor_at: dict[tuple[int, int], tuple] = {}
    for entry in cells:
        _tc, r, c, rs, cs = entry
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                anchor_at[(rr, cc)] = entry
    covered: set[int] = set()  # rows whose new column cells are covered by a widened cell
    for tc, r, c, rs, cs in cells:
        addr, span, sz = tc.find(f"{HP}cellAddr"), tc.find(f"{HP}cellSpan"), tc.find(f"{HP}cellSz")
        if c >= k:
            addr.set("colAddr", str(c + count))
        elif c < k < c + cs:
            span.set("colSpan", str(cs + count))
            sz.set("width", str(int(sz.get("width")) + new_w * count))
            covered.update(range(r, r + rs))
    rows_el = [tr for tr in tbl if tr.tag == f"{HP}tr"]
    for r in range(table.row_count):
        if r in covered:
            continue
        ref = anchor_at.get((r, ref_col))
        if ref is None or ref[1] != r:
            continue  # this row is covered by a vertical merge that starts above
        _rtc, _rr, _rc, rrs, _rcs = ref
        for n in range(count):
            new_tc = deepcopy(ref[0])
            _blank_cell(new_tc)
            new_tc.find(f"{HP}cellAddr").set("colAddr", str(k + n))
            new_tc.find(f"{HP}cellAddr").set("rowAddr", str(r))
            new_tc.find(f"{HP}cellSpan").set("colSpan", "1")
            new_tc.find(f"{HP}cellSpan").set("rowSpan", str(rrs))
            new_tc.find(f"{HP}cellSz").set("width", str(new_w))
            tr = rows_el[r]
            after = [x for x in tr if x.tag == f"{HP}tc" and int(x.find(f"{HP}cellAddr").get("colAddr")) > k + n]
            if after:
                after[0].addprevious(new_tc)
            else:
                tr.append(new_tc)
        if rrs > 1:
            covered.update(range(r + 1, r + rrs))
    tbl.set("colCnt", str(ncol + count))
    # Keep the table's overall width: scale every cell back to the old total.
    scale = total_before / (total_before + new_w * count)
    for tc, *_ in _tc_rows(tbl):
        sz = tc.find(f"{HP}cellSz")
        sz.set("width", str(max(1, int(round(int(sz.get("width")) * scale)))))
    _replace_table(table, _tstr(tbl))
    return f"Inserted {count} empty column(s) at column {k} of t{ti}; the table now has {ncol + count} columns."


def delete_table(doc: HwpxDocument, ti: int) -> str:
    table = get_table(doc, ti)
    tbl = table.element
    run = tbl.getparent()
    run.remove(tbl)
    p = run.getparent()
    table.paragraph.section.mark_dirty()
    clear_layout_cache(p)
    return f"Deleted table t{ti}. Later tables are renumbered (t{ti + 1} is now t{ti})."


def set_layout(
    doc: HwpxDocument, ti: int, *, column_widths_mm: list[float] | None, row_heights_mm: list[float] | None,
    align: str | None, repeat_header_row: bool | None,
) -> str:
    table = get_table(doc, ti)
    done = []
    if column_widths_mm is not None:
        _require_flat(table)
        if len(column_widths_mm) != table.column_count:
            raise ToolError(
                f"column_widths_mm needs {table.column_count} values (one per column); got {len(column_widths_mm)}."
            )
        widths = {i: mm_to_hu(w) for i, w in enumerate(column_widths_mm)}
        if min(widths.values()) < mm_to_hu(3):
            raise ToolError("Each column must be at least 3 mm wide.")
        _structure(table, tp._set_column_widths, widths)
        table = get_table(doc, ti)
        table.element.find(f"{HP}sz").set("width", str(sum(widths.values())))
        done.append(f"column widths {column_widths_mm} mm (total {sum(column_widths_mm):g} mm)")
    if row_heights_mm is not None:
        _require_flat(table)
        if len(row_heights_mm) != table.row_count:
            raise ToolError(f"row_heights_mm needs {table.row_count} values (one per row); got {len(row_heights_mm)}.")
        heights = {i: mm_to_hu(h) for i, h in enumerate(row_heights_mm)}
        _structure(table, tp._set_row_heights, heights)
        table = get_table(doc, ti)
        table.element.find(f"{HP}sz").set("height", str(sum(heights.values())))
        done.append(f"row heights {row_heights_mm} mm (rows grow automatically if the text needs more space)")
    if align is not None:
        a = align.lower()
        if a not in ("left", "center", "right"):
            raise ToolError('align must be "left", "center" or "right".')
        pos = table.element.find(f"{HP}pos")
        pos.set("horzAlign", a.upper())
        if pos.get("treatAsChar") == "1":
            # An inline table follows its paragraph's alignment.
            formats.apply_para_format(doc, [table.paragraph], {"alignment": a})
        done.append(f"aligned {a}")
    if repeat_header_row is not None:
        table.element.set("repeatHeader", "1" if repeat_header_row else "0")
        for tc, r, *_ in _tc_rows(table.element):
            if r == 0:
                tc.set("header", "1" if repeat_header_row else "0")
        done.append(("repeats" if repeat_header_row else "does not repeat") + " row 0 on each page")
    if not done:
        raise ToolError("Nothing to change: pass column_widths_mm, row_heights_mm, align or repeat_header_row.")
    table.paragraph.section.mark_dirty()
    return f"t{ti}: " + "; ".join(done) + "."


# ---------------------------------------------------------------------------
# cell formatting
# ---------------------------------------------------------------------------
_OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


def _sides_for(ct: CellTarget, borders: str, box: tuple[int, int, int, int]) -> set[str]:
    r0, c0, r1, c1 = box
    ci = ct.info
    outer = set()
    if ci.row == r0:
        outer.add("top")
    if ci.row + ci.row_span - 1 == r1:
        outer.add("bottom")
    if ci.col == c0:
        outer.add("left")
    if ci.col + ci.col_span - 1 == c1:
        outer.add("right")
    b = borders.lower().replace(" ", "")
    if b == "all":
        return set(formats.SIDES)
    if b == "outer":
        return outer
    if b == "inner":
        return set(formats.SIDES) - outer
    sides = set(b.split(","))
    bad = sides - set(formats.SIDES)
    if bad:
        raise ToolError('borders must be "all", "outer", "inner" or a comma list of left,right,top,bottom.')
    return sides


def format_cells(
    doc: HwpxDocument, cells: list[CellTarget], *, fill_color: str | None, border_type: str | None,
    border_width_mm: float | None, border_color: str | None, borders: str, vertical_align: str | None,
    padding_mm: float | None,
) -> str:
    if not cells:
        raise ToolError("No cells matched the target.")
    border_change = any(v is not None for v in (border_type, border_width_mm, border_color))
    fill = normalize_color(fill_color, allow_none=True) if fill_color is not None else None
    if not (border_change or fill or vertical_align or padding_mm is not None):
        raise ToolError("Nothing to change: pass fill_color, border_*, vertical_align or padding_mm.")

    line = None
    if border_change:
        btype = formats.BORDER_TYPES.get((border_type or "solid").lower())
        if btype is None:
            raise ToolError("border_type must be one of: " + ", ".join(formats.BORDER_TYPES) + ".")
        line = (btype, formats.border_width_str(border_width_mm if border_width_mm is not None else 0.12),
                normalize_color(border_color or "#000000"))

    # side changes per physical cell element, including neighbours across changed edges
    changes: dict[int, tuple[Any, dict[str, tuple]]] = {}

    def add(tc_el, side: str) -> None:
        changes.setdefault(id(tc_el), (tc_el, {}))[1][side] = line

    by_table: dict[int, list[CellTarget]] = {}
    for ct in cells:
        by_table.setdefault(ct.table_index, []).append(ct)
    for ti, group in by_table.items():
        box = (
            min(c.info.row for c in group), min(c.info.col for c in group),
            max(c.info.row + c.info.row_span - 1 for c in group), max(c.info.col + c.info.col_span - 1 for c in group),
        )
        grid = cell_grid(group[0].table)
        for ct in group:
            if line is None:
                continue
            ci = ct.info
            for side in _sides_for(ct, borders, box):
                add(ci.cell.element, side)
                if side in ("top", "bottom"):
                    rr = ci.row - 1 if side == "top" else ci.row + ci.row_span
                    neighbours = {id(grid[(rr, c)].cell.element): grid[(rr, c)] for c in range(ci.col, ci.col + ci.col_span) if (rr, c) in grid}
                else:
                    cc = ci.col - 1 if side == "left" else ci.col + ci.col_span
                    neighbours = {id(grid[(r, cc)].cell.element): grid[(r, cc)] for r in range(ci.row, ci.row + ci.row_span) if (r, cc) in grid}
                for nb in neighbours.values():
                    add(nb.cell.element, _OPPOSITE[side])

    targets = {id(ct.info.cell.element): ct.info.cell.element for ct in cells}
    for key, tc in targets.items():
        changes.setdefault(key, (tc, {}))
    for key, (tc, sides) in changes.items():
        cell_fill = fill if key in targets else None
        if sides or cell_fill:
            tc.set("borderFillIDRef", formats.derive_border_fill(doc, tc.get("borderFillIDRef"), sides=sides, fill=cell_fill))
    if vertical_align is not None:
        va = {"top": "TOP", "middle": "CENTER", "center": "CENTER", "bottom": "BOTTOM"}.get(vertical_align.lower())
        if va is None:
            raise ToolError('vertical_align must be "top", "middle" or "bottom".')
        for tc in targets.values():
            tc.find(f"{HP}subList").set("vertAlign", va)
    if padding_mm is not None:
        pad = str(mm_to_hu(padding_mm))
        for tc in targets.values():
            tc.set("hasMargin", "1")
            m = tc.find(f"{HP}cellMargin")
            for side in formats.SIDES:
                m.set(side, pad)
    cells[0].table.paragraph.section.mark_dirty()
    parts = []
    if fill:
        parts.append(f"fill {fill}")
    if line:
        parts.append(f"{borders} borders {line[0].lower()} {line[1]} {line[2]}")
    if vertical_align:
        parts.append(f"vertical-align {vertical_align}")
    if padding_mm is not None:
        parts.append(f"padding {padding_mm:g} mm")
    return f"Formatted {len(cells)} cell(s) ({', '.join(c.address for c in cells[:12])}{', ...' if len(cells) > 12 else ''}): " + "; ".join(parts) + "."


def describe_table(doc: HwpxDocument, ti: int, include_format: bool) -> dict[str, Any]:
    table = get_table(doc, ti)
    widths = column_widths(table)
    out: dict[str, Any] = {
        "table": f"t{ti}",
        "rows": table.row_count,
        "columns": table.column_count,
        "column_widths_mm": [hu_to_mm(w) for w in widths],
        "row_min_heights_mm": [hu_to_mm(h) for h in row_heights(table)],
        "cells": [],
    }
    for ci in table_cells(table):
        cell: dict[str, Any] = {"address": f"t{ti}.r{ci.row}.c{ci.col}", "text": ci.cell.text}
        if ci.row_span > 1 or ci.col_span > 1:
            cell["merged"] = f"rows {ci.row}-{ci.row + ci.row_span - 1}, cols {ci.col}-{ci.col + ci.col_span - 1}"
        if include_format:
            tc = ci.cell.element
            fmt = formats.describe_border_fill(doc, tc.get("borderFillIDRef"))
            sub = tc.find(f"{HP}subList")
            if sub is not None and sub.get("vertAlign"):
                fmt["vertical_align"] = {"CENTER": "middle"}.get(sub.get("vertAlign"), sub.get("vertAlign").lower())
            paras = ci.cell.paragraphs
            if paras:
                pdesc = formats.describe_para_pr(doc, paras[0].element.get("paraPrIDRef"))
                if pdesc.get("align"):
                    fmt["text_align"] = pdesc["align"]
                char =formats.short_char_desc(formats.describe_char_pr(doc, first_char_pr(paras[0].element)))
                if char:
                    fmt["text_format"] = char
            if len(paras) > 1:
                fmt["paragraphs"] = len(paras)
            cell["format"] = fmt
        out["cells"].append(cell)
    return out
