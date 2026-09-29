"""Direct scripting API for Hangul documents - for Python scripts written by an AI agent.

    from hwp_mcp.api import HwpDoc
    doc = HwpDoc.open(r"C:\\docs\\report.hwpx")      # or HwpDoc.new(path)
    print(doc.read())                               # addresses: p0, p1, t0.r1.c2 ...
    doc.insert_paragraph("제목", style="개요 1")
    doc.format_text("p0", match="제목", size_pt=16, bold=True)
    doc.save()                                      # validates first; writes nothing if invalid

Everything happens in memory until save(). If any call raises, stop: the file on disk
is untouched, so fix the script and run it again from the start.

Layers
1. The same operations as the MCP tools (same names without "hwp_", no path argument).
2. Advanced helpers that the MCP tools do not offer (tabs, fixed line spacing, paragraph
   borders, footnotes, hyperlinks, equations, text boxes, floating images, cell diagonals,
   sections, memos, tracked changes, form fields, columns).
3. Raw access with safe "clone and modify" helpers for anything else:
   doc.raw (python-hwpx HwpxDocument), doc.header, doc.paragraph(addr), doc.table(i),
   doc.derive(kind, base_id, modify), doc.modify_para_pr / modify_char_pr / modify_cells.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import warnings
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator

from lxml import etree as ET

import hwpx
from hwpx import HwpxDocument
from hwpx.oxml import HwpxOxmlParagraph, HwpxOxmlShape, HwpxOxmlTable, HwpxOxmlTableCell

from . import formats, ops, reader
from .model import (
    HC, HH, HP, PIC_TAG, RUN_TAG, T_TAG, all_pictures, clear_layout_cache, get_table, hu_to_mm, mm_to_hu,
    body_paragraphs, normalize_color, paragraph_text, resolve_cells, resolve_one_paragraph, resolve_paragraphs,
    run_elements, run_text, runs_in_range, split_at,
)
from .store import Store, ToolError, _write_atomically, normalize_path

HwpError = ToolError

_CONTAINERS = {
    "charPr": "charProperties",
    "paraPr": "paraProperties",
    "borderFill": "borderFills",
    "tabPr": "tabProperties",
}
# OWPML LineType2 order, used for the numeric leader code of inline <hp:tab/> elements
_LINE_TYPES = ["NONE", "SOLID", "DOT", "DASH", "DASH_DOT", "DASH_DOT_DOT", "LONG_DASH", "CIRCLE",
               "DOUBLE_SLIM", "SLIM_THICK", "THICK_SLIM", "SLIM_THICK_SLIM"]
_TAB_TYPES = {"LEFT": 1, "RIGHT": 2, "CENTER": 3, "DECIMAL": 4}
_HWPUNITCHAR_NS = "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar"


class _BatchStore(Store):
    """A Store that edits one in-memory document and never writes (HwpDoc.save does)."""

    def __init__(self, doc: HwpxDocument) -> None:
        super().__init__()
        self.doc = doc

    def open(self, path: str) -> HwpxDocument:
        return self.doc

    @contextmanager
    def edit(self, path: str) -> Iterator[HwpxDocument]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield self.doc


class HwpDoc:
    """One Hangul document held in memory. See the module docstring."""

    def __init__(self, path: str, raw: HwpxDocument) -> None:
        self.path = path
        self.raw = raw
        self._store = _BatchStore(raw)

    # ------------------------------------------------------------------ open/save
    @classmethod
    def open(cls, path: str) -> "HwpDoc":
        path = normalize_path(path)
        if not os.path.exists(path):
            raise ToolError(f"File not found: {path}. Use HwpDoc.new(path) for a new document.")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return cls(path, HwpxDocument.open(path))

    @classmethod
    def new(cls, path: str, *, overwrite: bool = False, **page: Any) -> "HwpDoc":
        """New empty document (A4 portrait unless page options are given: paper, orientation,
        margin_left_mm, ...). Nothing is written until save()."""
        path = normalize_path(path)
        if os.path.exists(path) and not overwrite:
            raise ToolError(f"{path} already exists. Pass overwrite=True or choose another path.")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            doc = cls(path, HwpxDocument.new())
            if page:
                ops._apply_page_setup(doc.raw, 0, page)
        return doc

    def _prepare(self) -> None:
        # Raw edits may not have marked their part dirty; re-serialize every XML part.
        for section in self.raw.sections:
            section.mark_dirty()
        self.header.mark_dirty()

    def check(self) -> list[str]:
        """Validate without writing. Returns a list of problems (empty = OK)."""
        self._prepare()
        try:
            self._store._serialize(self.path, self.raw)
        except ToolError as exc:
            return [str(exc)]
        return []

    def save(self, path: str | None = None, *, overwrite: bool = True) -> str:
        """Validate and write. With a new path (.hwpx or .hwp) this is "save as" and later
        saves go to that path. Raises (and writes nothing) if validation fails."""
        target = normalize_path(path) if path else self.path
        if path and os.path.exists(target) and not overwrite and target != self.path:
            raise ToolError(f"{target} already exists.")
        self._prepare()
        data = self._store._serialize(target, self.raw)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        _write_atomically(target, data)
        self.path = target
        return f"Saved {target} ({len(data):,} bytes, validated)."

    def preview_png(self, out_png: str, *, width: int = 1000, height: int = 1400) -> str:
        """Render an approximate preview (first page area) to PNG with a headless Chrome/Edge.
        Borders, images, bullets and auto numbers may be missing - it is a layout sanity check,
        not what Hancom shows. Saves nothing to the document."""
        from .preview import render
        browser = _find_browser()
        if browser is None:
            raise ToolError("No Chrome or Edge found for rendering the preview.")
        self._prepare()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "doc.hwpx")
            with open(src, "wb") as fh:
                fh.write(self.raw.to_bytes())
            html = os.path.join(tmp, "preview.html")
            with open(html, "w", encoding="utf-8") as fh:
                fh.write(render(src))
            out = str(Path(out_png).resolve())
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--user-data-dir={os.path.join(tmp, 'profile')}", f"--screenshot={out}",
                            f"--window-size={width},{height}", Path(html).as_uri()],
                           capture_output=True, timeout=120, check=False)
        if not os.path.exists(out):
            raise ToolError("The browser did not produce a screenshot.")
        return out

    # ------------------------------------------------------------------ layer 1: MCP operations
    def read(self, range: str | None = None, max_chars: int = 600, show_format: bool = False, *,
             start: int | None = None, limit: int | None = None) -> str:
        """range: section id ("s2.1"), "p10-p40", "p10-", "t3"...; start/limit are the older form."""
        if start is not None or limit is not None:
            first = start or 0
            range = f"p{first}-p{first + (limit or 300) - 1}"
        return reader.read_document(self._store, self.path, range, max_chars, show_format)

    def outline(self, section: str | None = None, depth: int | None = None) -> str:
        return reader.outline(self._store, self.path, section, depth)

    def search(self, query: str, max_results: int = 10) -> str:
        return reader.search(self._store, self.path, query, max_results)

    def diff(self, against: str | None = None) -> str:
        """What this script changed so far: the document in memory vs the file on disk
        (or vs ``against``). Call before save() to check the edits."""
        other = normalize_path(against) if against else self.path
        if not os.path.exists(other):
            return "Changes (new document):\n" + reader.diff_documents(None, self.raw)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            old = HwpxDocument.open(other)
        return f"Changes vs {os.path.basename(other)}:\n" + reader.diff_documents(old, self.raw)

    def get_paragraph(self, target: str) -> dict:
        return ops.get_paragraph(self._store, self.path, target)

    def find_text(self, text: str, ignore_case: bool = False, max_results: int = 50) -> dict:
        return ops.find_text(self._store, self.path, text, ignore_case, max_results)

    def insert_paragraph(self, text: str, after: str | None = None, style: str | None = None, like: str | None = None) -> str:
        return ops.insert_paragraph(self._store, self.path, text, after, style, like)

    def set_paragraph_text(self, target: str, text: str) -> str:
        return ops.set_paragraph_text(self._store, self.path, target, text)

    def replace_text(self, find: str, replace: str, target: str | None = None, ignore_case: bool = False,
                     max_count: int | None = None) -> str:
        return ops.replace_text(self._store, self.path, find, replace, target, ignore_case, max_count)

    def delete_paragraphs(self, target: str) -> str:
        return ops.delete_paragraphs(self._store, self.path, target)

    def format_text(self, target: str, match: str | None = None, occurrence: int | None = None,
                    start: int | None = None, end: int | None = None, *, font: str | None = None,
                    size_pt: float | None = None, bold: bool | None = None, italic: bool | None = None,
                    underline: bool | None = None, strikethrough: bool | None = None, color: str | None = None,
                    highlight: str | None = None, superscript: bool | None = None, subscript: bool | None = None,
                    char_width_percent: int | None = None, letter_spacing_percent: int | None = None,
                    shadow_color: str | None = None, outline: str | None = None, emboss: bool | None = None,
                    engrave: bool | None = None, underline_shape: str | None = None,
                    underline_color: str | None = None) -> str:
        """Character format. Beyond the MCP tool: shadow_color ("#RRGGBB" drop shadow),
        outline ("SOLID"/"DOT"/... or "NONE"), emboss, engrave, underline_shape ("SOLID",
        "DASH", "DOT", "DOUBLE_SLIM", "WAVE", ...), underline_color."""
        opts = dict(font=font, size_pt=size_pt, bold=bold, italic=italic, underline=underline,
                    strikethrough=strikethrough, color=color, highlight=highlight, superscript=superscript,
                    subscript=subscript, char_width_percent=char_width_percent,
                    letter_spacing_percent=letter_spacing_percent)
        extra = dict(shadow=normalize_color(shadow_color) if shadow_color else None,
                     outline=outline.upper() if outline else None, emboss=emboss, engrave=engrave,
                     underline_shape=underline_shape.upper() if underline_shape else None,
                     underline_color=normalize_color(underline_color) if underline_color else None)
        return ops.format_text(self._store, self.path, target, match, occurrence, start, end, opts, extra)

    def set_paragraph_format(self, target: str, **fmt: Any) -> str:
        """align, line_spacing_percent, space_before_pt, space_after_pt, indent_left_mm,
        indent_right_mm, first_line_indent_mm, keep_with_next, keep_lines, page_break_before."""
        _check_keys(fmt, formats.PARA_OPTION_KEYS)
        return ops.set_paragraph_format(self._store, self.path, target, fmt)

    def list_styles(self) -> list[dict]:
        return ops.list_styles(self._store, self.path)["styles"]

    def apply_style(self, target: str, style: str, keep_char_format: bool = False) -> str:
        return ops.apply_style(self._store, self.path, target, style, keep_char_format)

    def create_style(self, name: str, base_style: str | None = None, **fmt: Any) -> str:
        """fmt: font, size_pt, bold, italic, underline, color, align, line_spacing_percent,
        space_before_pt, space_after_pt, indent_left_mm, first_line_indent_mm."""
        char_opts = {k: fmt.pop(k) for k in list(fmt) if k in formats.CHAR_OPTION_KEYS}
        _check_keys(fmt, formats.PARA_OPTION_KEYS)
        return ops.create_style(self._store, self.path, name, base_style, char_opts, fmt)

    def set_list(self, target: str, kind: str, level: int = 1, bullet_char: str | None = None,
                 number_format: str | None = None, start_number: int | None = None) -> str:
        return ops.set_list(self._store, self.path, target, kind, level, bullet_char, number_format, start_number)

    def insert_table(self, after: str | None = None, rows: int | None = None, cols: int | None = None,
                     data: list[list[Any]] | None = None, column_widths_mm: list[float] | None = None,
                     header_row: bool = False) -> str:
        return ops.insert_table(self._store, self.path, after, rows, cols, data, column_widths_mm, header_row)

    def get_table(self, table: int, include_format: bool = False) -> dict:
        return ops.get_table_info(self._store, self.path, table, include_format)

    def set_cell_text(self, table: int, data: list[list[Any]], start_row: int = 0, start_col: int = 0) -> str:
        return ops.set_cell_text(self._store, self.path, table, data, start_row, start_col)

    def merge_cells(self, cells: str) -> str:
        return ops.merge_cells(self._store, self.path, cells)

    def split_cell(self, cell: str) -> str:
        return ops.split_cell(self._store, self.path, cell)

    def format_cells(self, cells: str, *, fill_color: str | None = None, border_type: str | None = None,
                     border_width_mm: float | None = None, border_color: str | None = None, borders: str = "all",
                     vertical_align: str | None = None, padding_mm: float | None = None) -> str:
        return ops.format_cells(self._store, self.path, cells, fill_color=fill_color, border_type=border_type,
                                border_width_mm=border_width_mm, border_color=border_color, borders=borders,
                                vertical_align=vertical_align, padding_mm=padding_mm)

    def table_structure(self, table: int, action: str, index: int | None = None, count: int = 1) -> str:
        return ops.table_structure(self._store, self.path, table, action, index, count)

    def set_table_layout(self, table: int, *, column_widths_mm: list[float] | None = None,
                         row_heights_mm: list[float] | None = None, align: str | None = None,
                         repeat_header_row: bool | None = None) -> str:
        return ops.set_table_layout(self._store, self.path, table, column_widths_mm=column_widths_mm,
                                    row_heights_mm=row_heights_mm, align=align, repeat_header_row=repeat_header_row)

    def insert_image(self, image_path: str, after: str | None = None, width_mm: float | None = None,
                     height_mm: float | None = None, align: str = "center") -> str:
        return ops.insert_image(self._store, self.path, image_path, after, width_mm, height_mm, align)

    def edit_image(self, image: int, width_mm: float | None = None, height_mm: float | None = None,
                   delete: bool = False) -> str:
        return ops.edit_image(self._store, self.path, image, width_mm, height_mm, delete)

    def page_setup(self, section: int = 0, **page: Any) -> str:
        """paper, orientation, margin_left_mm, margin_right_mm, margin_top_mm, margin_bottom_mm,
        header_margin_mm, footer_margin_mm, columns, column_gap_mm."""
        _check_keys(page, ("paper", "orientation", "margin_left_mm", "margin_right_mm", "margin_top_mm",
                           "margin_bottom_mm", "header_margin_mm", "footer_margin_mm", "columns", "column_gap_mm"))
        return ops.page_setup(self._store, self.path, section, page)

    def set_header_footer(self, kind: str, text: str | None = None, page_number: str | None = None,
                          align: str = "center", section: int = 0, remove: bool = False) -> str:
        return ops.header_footer(self._store, self.path, kind, text, page_number, align, section, remove)

    # ------------------------------------------------------------------ addressing / raw access
    @property
    def header(self):
        """python-hwpx HwpxOxmlHeader (fonts, charPr, paraPr, borderFill, styles...)."""
        return self.raw.parts.headers[0]

    def paragraphs(self, target: str) -> list[HwpxOxmlParagraph]:
        return [t.paragraph for t in resolve_paragraphs(self.raw, target)]

    def paragraph(self, target: str) -> HwpxOxmlParagraph:
        return resolve_one_paragraph(self.raw, target).paragraph

    def table(self, index: int) -> HwpxOxmlTable:
        return get_table(self.raw, index)

    def cells(self, target: str) -> list[HwpxOxmlTableCell]:
        return [c.info.cell for c in resolve_cells(self.raw, target)]

    def image_element(self, index: int):
        pics = all_pictures(self.raw)
        if not 0 <= index < len(pics):
            raise ToolError(f"Image g{index} does not exist ({len(pics)} images).")
        return pics[index]

    def _text_width_hu(self, paragraph: HwpxOxmlParagraph) -> int:
        index = next(i for i, sec in enumerate(self.raw.sections) if sec.element == paragraph.section.element)
        return ops._text_width_hu(self.raw, index)

    def text_width_mm(self, section: int = 0) -> float:
        """Width of the text area (page width minus left/right margins) - the right edge for tabs/objects."""
        return hu_to_mm(ops._text_width_hu(self.raw, section))

    def last_paragraph(self) -> str:
        """Address of the last body paragraph, e.g. "p40"."""
        return f"p{len(body_paragraphs(self.raw)) - 1}"

    def text_of(self, target: str) -> str:
        return paragraph_text(self.paragraph(target).element)

    @staticmethod
    def touch(paragraph: HwpxOxmlParagraph) -> None:
        """Call after editing a paragraph's XML by hand: drops its layout cache, marks it changed."""
        clear_layout_cache(paragraph.element)
        paragraph.section.mark_dirty()

    # ------------------------------------------------------------------ layer 3: clone-and-modify
    def _container(self, kind: str):
        if kind not in _CONTAINERS:
            raise ToolError(f"kind must be one of {list(_CONTAINERS)}.")
        el = self.header.element.find(f".//{HH}{_CONTAINERS[kind]}")
        if el is None:
            el = ET.SubElement(self.header.element.find(f"{HH}refList"), f"{HH}{_CONTAINERS[kind]}", {"itemCnt": "0"})
        return el

    def get_def(self, kind: str, def_id: str | int):
        """The header element of a definition (read-only use! it is shared). kind: charPr, paraPr, borderFill, tabPr."""
        for el in self._container(kind):
            if el.get("id") == str(def_id):
                return el
        raise ToolError(f"{kind} id {def_id} not found.")

    def derive(self, kind: str, base_id: str | int | None, modify: Callable[[Any], None]) -> str:
        """Clone definition ``base_id`` of ``kind`` (charPr/paraPr/borderFill/tabPr), let
        ``modify(element)`` change the clone, and return the id of an identical existing entry
        or of the newly added one. Never edit shared definitions in place - use this."""
        container = self._container(kind)
        items = [el for el in container if el.tag == f"{HH}{kind}"]
        base = next((el for el in items if el.get("id") == str(base_id)), None)
        if base is None:
            if not items:
                raise ToolError(f"No {kind} definitions to derive from.")
            base = items[0]
        new = deepcopy(base)
        modify(new)
        key = formats._canonical(new)
        for el in items:
            if formats._canonical(el) == key:
                return el.get("id")
        new_id = str(max(int(el.get("id")) for el in items) + 1)
        new.set("id", new_id)
        container.append(new)
        container.set("itemCnt", str(len(items) + 1))
        self.header.mark_dirty()
        return new_id

    def modify_para_pr(self, target: str, modify: Callable[[Any], None]) -> list[str]:
        """Give each target paragraph a derived paraPr changed by ``modify(hh:paraPr element)``.
        Remember: inside <hp:switch>, <hp:default> margin/lineSpacing(non-percent)/tab values are 2x <hp:case>."""
        out = []
        for t in resolve_paragraphs(self.raw, target):
            new_id = self.derive("paraPr", t.paragraph.element.get("paraPrIDRef"), modify)
            t.paragraph.element.set("paraPrIDRef", new_id)
            self.touch(t.paragraph)
            out.append(t.address)
        return out

    def modify_char_pr(self, target: str, modify: Callable[[Any], None], match: str | None = None,
                       occurrence: int | None = None, start: int | None = None, end: int | None = None) -> list[str]:
        """Give the selected text a derived charPr changed by ``modify(hh:charPr element)``."""
        cache: dict[str | None, str] = {}

        def apply(runs):
            for run in runs:
                base = run.get("charPrIDRef")
                if base not in cache:
                    cache[base] = self.derive("charPr", base, modify)
                run.set("charPrIDRef", cache[base])

        return ops.apply_to_text(self.raw, target, match, occurrence, start, end, apply)

    def modify_cells(self, cells: str, *, border_fill: Callable[[Any], None] | None = None,
                     cell: Callable[[Any], None] | None = None) -> list[str]:
        """For each cell: derive its borderFill with ``border_fill(hh:borderFill)`` and/or edit
        the <hp:tc> element itself with ``cell(tc)`` (e.g. cellMargin, subList attributes)."""
        out = []
        for ct in resolve_cells(self.raw, cells):
            tc = ct.info.cell.element
            if border_fill is not None:
                tc.set("borderFillIDRef", self.derive("borderFill", tc.get("borderFillIDRef"), border_fill))
            if cell is not None:
                cell(tc)
            ct.table.paragraph.section.mark_dirty()
            out.append(ct.address)
        return out

    # ------------------------------------------------------------------ layer 2: advanced helpers
    def set_line_spacing(self, target: str, value: float, kind: str = "percent") -> list[str]:
        """kind: "percent" (value in %), "fixed" (value in pt, 고정 값), "at_least" (pt, 최소),
        "between_lines" (pt, 여백만 지정)."""
        types = {"percent": "PERCENT", "fixed": "FIXED", "at_least": "AT_LEAST", "between_lines": "BETWEEN_LINES"}
        if kind not in types:
            raise ToolError(f"kind must be one of {list(types)}.")
        case_value = int(round(value)) if kind == "percent" else int(round(value * 100))

        def fn(pp):
            nodes = list(pp.iter(f"{HH}lineSpacing"))
            if not nodes:
                raise ToolError("paraPr has no lineSpacing element.")
            for ls in nodes:
                in_default = ls.getparent().tag == f"{HP}default"
                ls.set("type", types[kind])
                ls.set("value", str(case_value * (2 if in_default and kind != "percent" else 1)))
                ls.set("unit", "HWPUNIT")

        return self.modify_para_pr(target, fn)

    def set_tabs(self, target: str, stops: list[dict[str, Any]], *, auto_tab_right: bool = False) -> list[str]:
        """Tab stops (탭 설정). stops: [{"pos_mm": 160, "type": "right", "leader": "dot"}] with
        type left/right/center/decimal and leader none/dot/dash/solid/... ([] removes custom stops).
        Put "\\t" in the text to jump to the next stop, e.g. "1. 서론\\t3" for a table of contents."""
        items = []
        for s in stops:
            t = str(s.get("type", "left")).upper()
            leader = str(s.get("leader", "none")).upper()
            if t not in _TAB_TYPES:
                raise ToolError(f"tab type {t} not in {list(_TAB_TYPES)}")
            if leader not in _LINE_TYPES:
                raise ToolError(f"tab leader {leader} not in {_LINE_TYPES}")
            items.append((mm_to_hu(float(s["pos_mm"])), t, leader))
        for pt in resolve_paragraphs(self.raw, target):
            if "." in pt.address:
                continue  # table cells: no page-width check
            width = self._text_width_hu(pt.paragraph)
            for pos, _t, _l in items:
                if pos > width:
                    raise ToolError(
                        f"Tab position {hu_to_mm(pos):g} mm is beyond the text width of {pt.address} "
                        f"({hu_to_mm(width):g} mm, measured from the left margin). For a stop at the right "
                        f"margin use pos_mm={hu_to_mm(width):g}.")

        tab_pr = ET.Element(f"{HH}tabPr", {"id": "0", "autoTabLeft": "0", "autoTabRight": "1" if auto_tab_right else "0"})
        for pos, t, leader in items:  # Hancom form: one <hp:switch> per stop, default = 2 x case
            sw = ET.SubElement(tab_pr, f"{HP}switch")
            case = ET.SubElement(sw, f"{HP}case", {f"{HP}required-namespace": _HWPUNITCHAR_NS})
            ET.SubElement(case, f"{HH}tabItem", {"pos": str(pos), "type": t, "leader": leader, "unit": "HWPUNIT"})
            default = ET.SubElement(sw, f"{HP}default")
            ET.SubElement(default, f"{HH}tabItem", {"pos": str(pos * 2), "type": t, "leader": leader})

        def replace_all(el):
            el.attrib.update(tab_pr.attrib)
            for child in list(el):
                el.remove(child)
            for child in tab_pr:
                el.append(deepcopy(child))

        tab_id = self.derive("tabPr", "0", replace_all)
        done = self.modify_para_pr(target, lambda pp: pp.set("tabPrIDRef", tab_id))
        # keep the inline <hp:tab/> caches consistent: n-th tab in a paragraph -> n-th stop
        for p in self.paragraphs(target):
            for n, tab in enumerate(p.element.iter(f"{HP}tab")):
                if n < len(items):
                    tab.set("type", str(_TAB_TYPES[items[n][1]]))
                    tab.set("leader", str(_LINE_TYPES.index(items[n][2])))
        return done

    def set_paragraph_border(self, target: str, *, border_type: str = "solid", border_width_mm: float = 0.12,
                             border_color: str = "#000000", fill_color: str | None = None,
                             sides: str = "all", padding_mm: float = 1.0) -> list[str]:
        """Border and/or background around paragraphs (문단 테두리/배경). sides: "all" or a comma list
        of left,right,top,bottom. Consecutive paragraphs with the same border are joined into one box."""
        btype = formats.BORDER_TYPES.get(border_type.lower())
        if btype is None:
            raise ToolError(f"border_type must be one of {list(formats.BORDER_TYPES)}")
        side_set = set(formats.SIDES) if sides == "all" else {s.strip() for s in sides.split(",")}
        line = (btype, formats.border_width_str(border_width_mm), normalize_color(border_color))
        bf_id = formats.derive_border_fill(
            self.raw, "1", sides={s: (line if s in side_set else ("NONE", "0.1 mm", "#000000")) for s in formats.SIDES},
            fill=normalize_color(fill_color, allow_none=True) if fill_color else None)
        pad = str(mm_to_hu(padding_mm))

        def fn(pp):
            border = pp.find(f"{HH}border")
            if border is None:
                border = ET.SubElement(pp, f"{HH}border")
            border.attrib.update({"borderFillIDRef": bf_id, "offsetLeft": pad, "offsetRight": pad,
                                  "offsetTop": pad, "offsetBottom": pad, "connect": "1", "ignoreMargin": "0"})

        return self.modify_para_pr(target, fn)

    def set_cell_options(self, cells: str, *, diagonal: str | None = None, padding_mm: dict[str, float] | float | None = None,
                         text_direction: str | None = None) -> list[str]:
        """diagonal: "slash" (/), "backslash" (\\), "cross" (X) or "none"; padding_mm: number or
        {"left":..,"right":..,"top":..,"bottom":..}; text_direction: "horizontal" or "vertical" (세로쓰기)."""
        border_fill = None
        if diagonal is not None:
            d = diagonal.lower()
            if d not in ("slash", "backslash", "cross", "none"):
                raise ToolError('diagonal must be "slash", "backslash", "cross" or "none".')

            def border_fill(bf):
                for tag, on in (("slash", d in ("slash", "cross")), ("backSlash", d in ("backslash", "cross"))):
                    el = bf.find(f"{HH}{tag}")
                    el.set("type", "CENTER" if on else "NONE")
                diag = bf.find(f"{HH}diagonal")
                if diag is not None and d != "none":
                    diag.set("type", "SOLID")
        cell_fn = None
        if padding_mm is not None or text_direction is not None:
            pads = padding_mm if isinstance(padding_mm, dict) else ({s: padding_mm for s in formats.SIDES} if padding_mm is not None else {})
            if text_direction is not None and text_direction.lower() not in ("horizontal", "vertical"):
                raise ToolError('text_direction must be "horizontal" or "vertical".')

            def cell_fn(tc):
                if pads:
                    tc.set("hasMargin", "1")
                    m = tc.find(f"{HP}cellMargin")
                    for side, v in pads.items():
                        if side not in formats.SIDES:
                            raise ToolError(f"padding side {side} not in {formats.SIDES}")
                        m.set(side, str(mm_to_hu(v)))
                if text_direction is not None:
                    tc.find(f"{HP}subList").set("textDirection", text_direction.upper())
        if border_fill is None and cell_fn is None:
            raise ToolError("Nothing to change.")
        return self.modify_cells(cells, border_fill=border_fill, cell=cell_fn)

    @staticmethod
    def _new_controls(p_el, before: list) -> list:
        """Controls directly in p_el's runs that are not in ``before`` (a list, keeping the proxies alive)."""
        return [c for c in p_el.iter(f"{HP}ctrl") if c not in before and c.getparent().getparent() == p_el]

    def _place_control(self, p_el, ctrl, match: str | None, at: str) -> None:
        """Move an <hp:ctrl> into its own run placed just after ``match`` (or at the start/end)."""
        old_run = ctrl.getparent()
        old_run.remove(ctrl)
        if len(old_run) == 0:
            p_el.remove(old_run)
        run_el = p_el.makeelement(RUN_TAG, {"charPrIDRef": old_run.get("charPrIDRef", "0")})
        run_el.append(ctrl)
        offset = _find_span(p_el, match)[1] if match is not None else (0 if at == "start" else len(paragraph_text(p_el)))
        split_at(p_el, offset)
        acc = 0
        for run in run_elements(p_el):
            n = len(run_text(run))
            if acc == offset and n:  # first text run starting at the offset
                run.addprevious(run_el)
                return
            acc += n
        runs = run_elements(p_el)
        if runs:
            runs[-1].addnext(run_el)
        else:
            p_el.insert(0, run_el)

    def add_footnote(self, target: str, text: str, after_match: str | None = None, *, endnote: bool = False) -> str:
        """Footnote (각주) or endnote (미주) mark after ``after_match`` (default: end of the paragraph)."""
        p = self.paragraph(target)
        if after_match is not None:
            _find_span(p.element, after_match)  # fail before changing anything
        before = list(p.element.iter(f"{HP}ctrl"))
        (p.add_endnote if endnote else p.add_footnote)(text)
        for ctrl in self._new_controls(p.element, before):
            self._place_control(p.element, ctrl, after_match, "end")
        self.touch(p)
        return f"Added {'endnote' if endnote else 'footnote'} to {target}."

    def add_hyperlink(self, target: str, url: str, match: str | None = None, text: str | None = None) -> str:
        """Make existing text ``match`` a link (blue, underlined), or append ``text`` as a link at the end."""
        p = self.paragraph(target)
        if (match is None) == (text is None):
            raise ToolError("Pass exactly one of match (existing text) or text (new link text).")
        if match is not None:
            span = _find_span(p.element, match)  # fail before changing anything
        before = run_elements(p.element)
        p.add_hyperlink(url, match or text)
        new_runs = [r for r in run_elements(p.element) if r not in before]
        if len(new_runs) != 3:
            raise ToolError("Unexpected hyperlink structure from python-hwpx.")
        begin_run, text_run, end_run = new_runs
        if match is not None:
            for r in new_runs:
                p.element.remove(r)
            selected = runs_in_range(p.element, *span)
            selected[0].addprevious(begin_run)
            selected[-1].addnext(end_run)
            formats.apply_char_format(self.raw, selected, {"color": "#0000FF", "underline": True})
        self.touch(p)
        return f"Linked {match or text!r} in {target} to {url}."

    def add_bookmark(self, target: str, name: str, at: str = "start") -> str:
        """Bookmark (책갈피) at the start or end of a paragraph."""
        p = self.paragraph(target)
        before = list(p.element.iter(f"{HP}ctrl"))
        p.add_bookmark(name)
        for ctrl in self._new_controls(p.element, before):
            self._place_control(p.element, ctrl, None, at)
        self.touch(p)
        return f"Added bookmark {name!r} at the {at} of {target}."

    def add_equation(self, latex: str | None = None, *, script: str | None = None, target: str | None = None,
                     after: str | None = None, size_pt: float = 11) -> str:
        """Equation (수식) from LaTeX (or a Hancom EqEdit script). Inline at the end of paragraph
        ``target``, or as a new centered paragraph after ``after`` ("end" by default)."""
        if (latex is None) == (script is None):
            raise ToolError("Pass exactly one of latex or script.")
        if script is None:
            try:
                script = hwpx.latex_to_eqedit(latex)
            except Exception as exc:  # noqa: BLE001
                raise ToolError(f"Unsupported LaTeX for Hancom equations: {exc}") from exc
        if target is not None:
            p = self.paragraph(target)
            where = target
        else:
            self.insert_paragraph("", after=after)
            p = self._last_inserted(after)
            formats.apply_para_format(self.raw, [p], {"alignment": "center"})
            where = "new paragraph"
        p.add_equation(script, base_unit=int(size_pt * 100))
        self.touch(p)
        return f"Added equation to {where}: {script}"

    def _last_inserted(self, after: str | None) -> HwpxOxmlParagraph:
        """The paragraph a preceding insert_paragraph(after=after) call just created."""
        if after in (None, "end"):
            return self.raw.paragraphs[-1]
        if after == "start":
            return body_paragraphs(self.raw)[0]
        anchor = resolve_one_paragraph(self.raw, after).paragraph
        nxt = anchor.element.getnext()
        return HwpxOxmlParagraph(nxt, anchor.section)

    def add_textbox(self, text: str, *, after: str | None = None, width_mm: float = 80, height_mm: float = 20,
                    border_color: str = "#000000", border_width_mm: float = 0.12, fill_color: str | None = None,
                    padding_mm: float = 2, vertical_align: str = "center", align: str = "left",
                    floating: dict[str, Any] | None = None) -> str:
        """Text box (글상자): a rectangle holding text. Inline in a new paragraph after ``after``
        (aligned by ``align``), or floating when ``floating={"x_mm":..,"y_mm":..,"relative_to":"paper"|"page"|"para",
        "wrap": "square"|"top_and_bottom"|"behind_text"|"in_front_of_text"}``. Returns a message with the
        paragraph address; format the text inside via doc.textbox_paragraphs()."""
        self.insert_paragraph("", after=after)
        p = self._last_inserted(after)
        rect = p.add_rectangle(mm_to_hu(width_mm), mm_to_hu(height_mm), line_color=normalize_color(border_color),
                               line_width=str(mm_to_hu(border_width_mm)),
                               fill_color=normalize_color(fill_color) if fill_color else None,
                               treat_as_char=floating is None)
        pad = mm_to_hu(padding_mm)
        va = {"top": "TOP", "center": "CENTER", "middle": "CENTER", "bottom": "BOTTOM"}[vertical_align.lower()]
        rect.set_draw_text(text, margin={"left": pad, "right": pad, "top": pad, "bottom": pad}, vert_align=va)
        if floating:
            self._float(rect.element, floating)
        else:
            formats.apply_para_format(self.raw, [p], {"alignment": align})
        self.touch(p)
        return f"Added text box in {ops._address_of(self.raw, p.element)}."

    def textbox_paragraphs(self, target: str) -> list[HwpxOxmlParagraph]:
        """Paragraphs inside the text box(es) of a paragraph (for doc.raw-level formatting)."""
        p = self.paragraph(target)
        return [HwpxOxmlParagraph(el, p.section) for el in p.element.iter(f"{HP}p") if el is not p.element]

    def _float(self, obj_el, spec: dict[str, Any]) -> None:
        rel = str(spec.get("relative_to", "para")).upper()
        if rel not in ("PAPER", "PAGE", "PARA"):
            raise ToolError('relative_to must be "paper", "page" or "para".')
        wrap = str(spec.get("wrap", "square")).upper()
        if wrap not in ("SQUARE", "TOP_AND_BOTTOM", "BEHIND_TEXT", "IN_FRONT_OF_TEXT"):
            raise ToolError('wrap must be "square", "top_and_bottom", "behind_text" or "in_front_of_text".')
        pos = obj_el.find(f"{HP}pos")
        pos.attrib.update({
            "treatAsChar": "0", "affectLSpacing": "0", "flowWithText": "1" if rel == "PARA" else "0",
            "allowOverlap": "1" if wrap in ("BEHIND_TEXT", "IN_FRONT_OF_TEXT") else "0", "holdAnchorAndSO": "0",
            "vertRelTo": rel, "horzRelTo": "COLUMN" if rel == "PARA" else rel, "vertAlign": "TOP", "horzAlign": "LEFT",
            "vertOffset": str(mm_to_hu(float(spec.get("y_mm", 0)))), "horzOffset": str(mm_to_hu(float(spec.get("x_mm", 0)))),
        })
        obj_el.set("textWrap", wrap)
        obj_el.set("textFlow", "BOTH_SIDES")

    def float_image(self, image: int, *, x_mm: float = 0, y_mm: float = 0, relative_to: str = "para",
                    wrap: str = "square") -> str:
        """Turn inline image gN into a floating one at (x_mm, y_mm) from the paragraph/page/paper
        top-left, with text wrap square/top_and_bottom/behind_text/in_front_of_text."""
        pic = self.image_element(image)
        self._float(pic, {"x_mm": x_mm, "y_mm": y_mm, "relative_to": relative_to, "wrap": wrap})
        p_el = pic.getparent().getparent()
        clear_layout_cache(p_el)
        for s in self.raw.sections:
            s.mark_dirty()
        return f"Image g{image} now floats at ({x_mm}, {y_mm}) mm from the {relative_to}, wrap {wrap}."

    def add_section(self, **page: Any) -> int:
        """Start a new section (구역, begins on a new page) at the end; page options as page_setup.
        Returns its index. Later insert_paragraph(after="end") calls go into it."""
        self.raw.add_section()
        index = len(self.raw.sections) - 1
        if page:
            self.page_setup(index, **page)
        return index

    def set_columns(self, target: str, count: int, *, gap_mm: float = 8, separator: str | None = None) -> str:
        """Multi-column layout (다단) starting at paragraph ``target``; count=1 returns to one column.
        separator: None or a line type such as "SOLID"."""
        p = self.paragraph(target)
        kw: dict[str, Any] = {"same_gap": mm_to_hu(gap_mm), "paragraph": p}
        if separator:
            kw.update(separator_type=separator.upper(), separator_width="0.12 mm", separator_color="#000000")
        self.raw.set_columns(count, **kw)
        self.touch(p)
        return f"{count} column(s) from {target}."

    def add_memo(self, target: str, text: str, author: str | None = None) -> str:
        """Memo (메모) attached to a paragraph."""
        p = self.paragraph(target)
        self.raw.add_memo_with_anchor(text, paragraph=p, author=author)
        self.touch(p)
        return f"Added memo to {target}."

    def track_insert(self, target: str, text: str, author: str = "AI") -> str:
        """Tracked insertion (변경 추적) of ``text`` at the end of a paragraph."""
        self.raw.add_tracked_insert(self.paragraph(target), text, author=author)
        return f"Tracked insert in {target}."

    def track_delete(self, target: str, match: str, author: str = "AI") -> str:
        self.raw.add_tracked_delete(self.paragraph(target), match=match, author=author)
        return f"Tracked delete of {match!r} in {target}."

    def track_replace(self, target: str, old: str, new: str, author: str = "AI") -> str:
        self.raw.add_tracked_replace(self.paragraph(target), old, new, author=author)
        return f"Tracked replace {old!r} -> {new!r} in {target}."

    def add_form_field(self, target: str, name: str, prompt: str = "", memo: str = "") -> str:
        """Click-here field (누름틀) appended to a paragraph; fill later with fill_form_field."""
        self.raw.add_form_field(name, prompt=prompt, memo=memo, paragraph=self.paragraph(target))
        return f"Added form field {name!r} to {target}."

    def fill_form_field(self, name: str, value: str) -> str:
        self.raw.fill_form_field(value, name=name)
        for s in self.raw.sections:
            s.mark_dirty()
        return f"Filled form field {name!r}."


def _find_span(p_el, match: str) -> tuple[int, int]:
    text = paragraph_text(p_el)
    pos = text.find(match)
    if pos < 0:
        raise ToolError(f'"{match}" not found in the paragraph.')
    return pos, pos + len(match)


def _check_keys(given: dict[str, Any], allowed) -> None:
    bad = set(given) - set(allowed)
    if bad:
        raise ToolError(f"Unknown option(s) {sorted(bad)}; allowed: {sorted(allowed)}.")


def _find_browser() -> str | None:
    candidates = [
        shutil.which("chrome"), shutil.which("msedge"), shutil.which("google-chrome"), shutil.which("chromium"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((c for c in candidates if c and os.path.exists(c)), None)


__all__ = ["HwpDoc", "HwpError", "HC", "HH", "HP", "mm_to_hu"]
