"""Which page each body paragraph is on.

Hancom stores its line layout in every paragraph it saves (<hp:linesegarray>, also carried over
from .hwp PARA_LINE_SEG records). Bit 0 of a line's flags marks the first line of a page, so for a
document saved by Hancom the page of every paragraph is exact.

Paragraphs without that layout (created by a script, or edited here: an edit drops the stale
layout) are estimated from font size, line spacing, text width and object sizes. Estimated page
breaks are approximate; when an estimated break is followed by a line Hancom marks as a page
start, the two are taken to be the same break so it is not counted twice.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from hwpx import HwpxDocument

from . import formats
from .model import HP, first_char_pr, mm_to_hu, paragraph_text, run_elements

PAGE_START = 0x1   # lineseg flag: first line of a page
CELL_PADDING = 282  # HWPUNIT, default top + bottom cell margins (0.5 mm each)


@dataclass
class PageInfo:
    spans: list[tuple[int, int]]  # (first page, last page) per body paragraph, 1-based
    exact: bool                   # every paragraph had Hancom's layout
    count: int

    def label(self, first: int, last: int) -> str:
        a, b = self.spans[first][0], self.spans[last][1]
        return f"page {a}" if a == b else f"page {a}-{b}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _geometry(section) -> tuple[int, int, int]:
    """(body height, body width, columns) of a section in HWPUNIT."""
    props = section.properties
    size, m = props.page_size, props.page_margins
    w, h = int(size.width), int(size.height)
    if (size.orientation or "").upper() == "NARROWLY":
        w, h = h, w
    height = h - int(m.top) - int(m.bottom) - int(m.header or 0) - int(m.footer or 0)
    width = w - int(m.left) - int(m.right) - int(m.gutter or 0)
    col = section.element.find(f".//{HP}colPr")
    cols = max(1, int(col.get("colCount", "1"))) if col is not None else 1
    return max(height, 1000), max(width, 1000), cols


class _Estimator:
    def __init__(self, doc: HwpxDocument):
        self.doc = doc
        # python-hwpx re-serializes the header on every style lookup: cache per style id
        self.char = lru_cache(maxsize=None)(self._char)
        self.para = lru_cache(maxsize=None)(self._para)
        self.break_before = lru_cache(maxsize=None)(self._break_before)

    def _break_before(self, para_pr) -> bool:
        return bool(formats.describe_para_pr(self.doc, para_pr).get("page_break_before"))

    def _char(self, char_pr) -> float:
        return float(formats.describe_char_pr(self.doc, char_pr).get("size_pt", 10)) * 100

    def _para(self, para_pr) -> tuple[str, float, float, float, float]:
        d = formats.describe_para_pr(self.doc, para_pr)
        kind, value = "percent", float(d.get("line_spacing_percent", 160))
        if d.get("line_spacing"):  # e.g. "fixed 20pt"
            m = re.match(r"(\w+) ([\d.]+)pt", d["line_spacing"])
            if m:
                kind, value = m.group(1), float(m.group(2)) * 100
        indent = mm_to_hu(float(d.get("indent_left_mm", 0)) + float(d.get("indent_right_mm", 0)))
        return kind, value, float(d.get("space_before_pt", 0)) * 100, float(d.get("space_after_pt", 0)) * 100, indent

    def line_height(self, el) -> float:
        size = self.char(first_char_pr(el))
        kind, value, *_ = self.para(el.get("paraPrIDRef"))
        if kind == "percent":
            return size * value / 100
        if kind == "fixed":
            return value
        if kind == "between_lines":
            return size + value
        return max(size * 1.0, value)  # at_least

    def text_height(self, text: str, el, width: float) -> float:
        size = self.char(first_char_pr(el))
        lines = 0
        for part in text.split("\n"):
            w = sum(size * (1.0 if ord(c) >= 0x2E80 else 0.3 if c == " " else 0.55) for c in part)
            lines += max(1, math.ceil(w / max(width, 1)))
        return lines * self.line_height(el)

    def table_height(self, tbl) -> float:
        """Stored table height, or an estimate from its cell texts when that is larger."""
        sz = tbl.find(HP + "sz")
        stored = int(sz.get("height", "0")) if sz is not None else 0
        rows: dict[int, float] = {}
        for tc in tbl.iter(HP + "tc"):
            if tc.getparent() is None or tc.getparent().getparent() is not tbl:
                continue  # a cell of a nested table
            addr, span, csz = tc.find(HP + "cellAddr"), tc.find(HP + "cellSpan"), tc.find(HP + "cellSz")
            if addr is None or (span is not None and span.get("rowSpan", "1") != "1"):
                continue
            width = int(csz.get("width", "0")) - 1020 if csz is not None else 8000
            h = CELL_PADDING + sum(self.text_height(paragraph_text(p), p, width)
                                   for p in tc.iter(HP + "p") if p.getparent().getparent() is tc)
            r = int(addr.get("rowAddr", "0"))
            rows[r] = max(rows.get(r, 0), h)
        return max(stored, sum(rows.values()))

    def space_after(self, el) -> float:
        return self.para(el.get("paraPrIDRef"))[3]

    def paragraph_height(self, el, width: float) -> float:
        _, _, before, after, indent = self.para(el.get("paraPrIDRef"))
        objects = 0.0
        for run in run_elements(el):
            for child in run:
                name = _local(child.tag)
                if name == "tbl":
                    objects += self.table_height(child)
                elif name in ("pic", "equation", "rect", "ellipse", "container", "ole", "chart", "textart"):
                    pos = child.find(HP + "pos")
                    sz = child.find(HP + "sz")
                    if sz is not None and (pos is None or pos.get("treatAsChar") != "0"):  # floating ones take no line space
                        objects += int(sz.get("height", "0"))
        text = paragraph_text(el)
        body = self.text_height(text, el, width - indent) if text.strip() or not objects else 0
        return before + after + body + objects


def _linesegs(el) -> list[tuple[int, int, int, int]]:
    arr = el.find(HP + "linesegarray")
    if arr is None:
        return []
    out = []
    for seg in arr.findall(HP + "lineseg"):
        out.append((int(seg.get("vertpos", "0")), int(seg.get("vertsize", "0")),
                    int(seg.get("spacing", "0")), int(seg.get("flags", "0"))))
    return out


def _forced_break(est: "_Estimator", el) -> bool:
    return el.get("pageBreak") == "1" or est.break_before(el.get("paraPrIDRef"))


def page_info(doc: HwpxDocument, body: list) -> PageInfo:
    est = _Estimator(doc)
    geometry = {id(s.element): _geometry(s) for s in doc.sections}
    spans: list[tuple[int, int]] = []
    started = 0         # page starts seen in Hancom's layout (after the first line)
    extra = 0           # page breaks added by estimation
    pending = 0         # estimated breaks since the last measured paragraph
    y = 0.0
    exact = True
    section = None
    for i, p in enumerate(body):
        el = p.element
        height, width, cols = geometry.get(id(p.section.element), (70000, 45000, 1))
        capacity = height * cols
        segs = _linesegs(el)
        new_section = section is not None and p.section.element is not section
        section = p.section.element
        if segs:
            if i > 0 and segs[0][3] & PAGE_START:
                if pending and extra:
                    extra -= 1  # the estimated break is this real one
                started += 1
            first = 1 + started + extra
            for seg in segs[1:]:
                if seg[3] & PAGE_START:
                    started += 1
            last_seg = segs[-1]
            y = last_seg[0] + last_seg[1] + last_seg[2]
            pending = 0
            for run in run_elements(el):  # a table running past the page bottom
                for child in run:
                    if _local(child.tag) == "tbl":
                        overflow = int((last_seg[0] + est.table_height(child)) // capacity)
                        if overflow:
                            extra += overflow
                            pending += overflow
                            y = (last_seg[0] + est.table_height(child)) % capacity
            spans.append((first, 1 + started + extra))
            continue
        exact = False
        if i > 0 and (new_section or _forced_break(est, el)) and y > 0:
            extra += 1
            pending += 1
            y = 0.0
        first = 1 + started + extra
        y += est.paragraph_height(el, width / cols)
        while y - est.space_after(el) > capacity:  # trailing spacing may hang past the page end
            y -= capacity
            extra += 1
            pending += 1
        y = min(y, capacity)
        spans.append((first, 1 + started + extra))
    count = spans[-1][1] if spans else 1
    return PageInfo(spans, exact, count)
