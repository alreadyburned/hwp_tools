"""XML-level helpers: units, paragraph text model, run splitting and addresses.

Text model
----------
A paragraph's text is the concatenation of its ``hp:t`` nodes, where the inline
elements ``hp:tab`` and ``hp:lineBreak`` count as "\\t" and "\\n" (one
character each). Controls, tables and pictures inside runs contribute no text.
All offsets used by the tools (find results, format ranges) are in this model.

Addresses
---------
``p12``            body paragraph 12 (document order across all sections)
``p3-p8`` / ``p*`` range / all body paragraphs
``t0.r1.c2``       cell (row 1, col 2) of table 0 -- all of its paragraphs
``t0.r1.c2.p0``    first paragraph inside that cell
``t0.r0.c*``       whole row 0;  ``t0.r1-3.c0`` rows 1..3 of column 0
Several addresses can be joined with commas: ``p1,p4,t0.r0.c*``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from lxml import etree as ET

from hwpx import HwpxDocument
from hwpx.oxml import HwpxOxmlParagraph, HwpxOxmlTable, HwpxOxmlTableCell

from .store import ToolError

NS_HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
NS_HH = "http://www.hancom.co.kr/hwpml/2011/head"
NS_HC = "http://www.hancom.co.kr/hwpml/2011/core"
HP = "{%s}" % NS_HP
HH = "{%s}" % NS_HH
HC = "{%s}" % NS_HC

T_TAG = HP + "t"
RUN_TAG = HP + "run"
P_TAG = HP + "p"
TBL_TAG = HP + "tbl"
PIC_TAG = HP + "pic"

HWPUNIT_PER_MM = 7200 / 25.4
HWPUNIT_PER_PT = 100


def mm_to_hu(mm: float) -> int:
    return int(round(mm * HWPUNIT_PER_MM))


def hu_to_mm(hu: float) -> float:
    return round(hu / HWPUNIT_PER_MM, 1)


def normalize_color(color: str | None, *, allow_none: bool = False) -> str | None:
    if color is None:
        return None
    c = color.strip()
    if allow_none and c.lower() in ("none", "transparent", ""):
        return "none"
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", c):
        return "#" + c.lstrip("#").upper()
    raise ToolError(f'Invalid color "{color}". Use "#RRGGBB", e.g. "#FF0000".')


# ---------------------------------------------------------------------------
# paragraph text model
# ---------------------------------------------------------------------------
_INLINE_TEXT = {HP + "tab": "\t", HP + "lineBreak": "\n"}


def _inline_text(el) -> str:
    return _INLINE_TEXT.get(el.tag, "")


def run_elements(p_el) -> list:
    return [c for c in p_el if c.tag == RUN_TAG]


def t_text(t_el) -> str:
    parts = [t_el.text or ""]
    for child in t_el:
        parts.append(_inline_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def run_text(run_el) -> str:
    return "".join(t_text(c) for c in run_el if c.tag == T_TAG)


def paragraph_text(p_el) -> str:
    return "".join(run_text(r) for r in run_elements(p_el))


def _t_tokens(t_el) -> list:
    """Content of an ``hp:t`` as a list of str / element tokens."""
    tokens: list = []
    if t_el.text:
        tokens.append(t_el.text)
    for child in t_el:
        tail = child.tail
        child.tail = None
        tokens.append(child)
        if tail:
            tokens.append(tail)
    return tokens


def _fill_t(t_el, tokens: list) -> None:
    t_el.text = None
    for child in list(t_el):
        t_el.remove(child)
    last = None
    for tok in tokens:
        if isinstance(tok, str):
            if last is None:
                t_el.text = (t_el.text or "") + tok
            else:
                last.tail = (last.tail or "") + tok
        else:
            t_el.append(tok)
            last = tok


def _token_len(tok) -> int:
    return len(tok) if isinstance(tok, str) else len(_inline_text(tok))


def _split_t(t_el, k: int):
    """Split an ``hp:t`` at character k (0 < k < len); returns the new right part."""
    tokens = _t_tokens(t_el)
    left, right, pos = [], [], 0
    for tok in tokens:
        n = _token_len(tok)
        if pos + n <= k:
            left.append(tok)
        elif pos >= k:
            right.append(tok)
        else:  # only strings can straddle
            left.append(tok[: k - pos])
            right.append(tok[k - pos:])
        pos += n
    new_t = ET.Element(t_el.tag, attrib=dict(t_el.attrib))
    _fill_t(t_el, left)
    _fill_t(new_t, right)
    return new_t


def _split_run(run_el, k: int) -> None:
    """Split a run so that a run boundary falls at character k inside it."""
    new_run = ET.Element(run_el.tag, attrib=dict(run_el.attrib))
    pos = 0
    moving = False
    for child in list(run_el):
        if moving:
            run_el.remove(child)
            new_run.append(child)
            continue
        if child.tag != T_TAG:
            continue
        n = len(t_text(child))
        if pos + n <= k:
            pos += n
            if pos == k:
                moving = True
            continue
        right_t = _split_t(child, k - pos)
        new_run.append(right_t)
        moving = True
    run_el.addnext(new_run)


def split_at(p_el, offset: int) -> None:
    pos = 0
    for run in run_elements(p_el):
        n = len(run_text(run))
        if pos < offset < pos + n:
            _split_run(run, offset - pos)
            return
        pos += n


def runs_in_range(p_el, start: int, end: int) -> list:
    """Split runs at start/end and return the text runs fully inside [start, end)."""
    split_at(p_el, start)
    split_at(p_el, end)
    out, pos = [], 0
    for run in run_elements(p_el):
        n = len(run_text(run))
        if n and pos >= start and pos + n <= end:
            out.append(run)
        pos += n
    return out


def make_t(text: str):
    t = ET.Element(T_TAG)
    tokens: list = []
    buf = ""
    for ch in text:
        if ch in "\t\n":
            if buf:
                tokens.append(buf)
                buf = ""
            if ch == "\t":
                tokens.append(ET.Element(HP + "tab", {"width": "4000", "leader": "0", "type": "1"}))
            else:
                tokens.append(ET.Element(HP + "lineBreak"))
        else:
            buf += ch
    if buf:
        tokens.append(buf)
    _fill_t(t, tokens)
    return t


def clear_layout_cache(p_el) -> None:
    for child in list(p_el):
        if child.tag == HP + "linesegarray":
            p_el.remove(child)


def _mergeable_t(el) -> bool:
    return el.tag == T_TAG and not el.attrib


def merge_adjacent_runs(p_el) -> None:
    """Join neighbouring text-only runs with the same character format (undo split noise)."""
    prev = None
    for run in run_elements(p_el):
        text_only = len(run) > 0 and all(_mergeable_t(c) for c in run)
        if not text_only:
            prev = None
        elif prev is not None and prev.getnext() is run and dict(prev.attrib) == dict(run.attrib):
            for child in list(run):
                prev.append(child)
            p_el.remove(run)
        else:
            prev = run
    for run in run_elements(p_el):
        children = list(run)
        for a, b in zip(children, children[1:]):
            if _mergeable_t(a) and _mergeable_t(b):
                _fill_t(b, _t_tokens(a) + _t_tokens(b))
                run.remove(a)


def replace_range(p_el, start: int, end: int, text: str, default_char_pr: str | None = None) -> None:
    """Replace characters [start, end) of a paragraph with text.

    The inserted text takes the character format of the first replaced run
    (or of the run just before the insertion point for a pure insertion).
    Non-text children of replaced runs (controls, objects) are kept.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    targets = runs_in_range(p_el, start, end) if end > start else []
    if targets:
        host = targets[0]
        first_idx = next(i for i, c in enumerate(host) if c.tag == T_TAG)
        for run in targets:
            for child in list(run):
                if child.tag == T_TAG:
                    run.remove(child)
        for run in targets[1:]:
            if len(run) == 0:
                p_el.remove(run)
        host.insert(first_idx, make_t(text))
    elif text:
        _insert_text(p_el, start, text, default_char_pr)
    merge_adjacent_runs(p_el)
    clear_layout_cache(p_el)


def _insert_text(p_el, offset: int, text: str, default_char_pr: str | None) -> None:
    split_at(p_el, offset)
    runs = run_elements(p_el)
    pos = 0
    for run in runs:
        n = len(run_text(run))
        if n and pos + n == offset:
            run.append(make_t(text))  # extend the text run that ends here
            return
        if pos == offset and (n or run is runs[-1]):
            first_t = next((c for c in run if c.tag == T_TAG), None)
            if first_t is not None:
                first_t.addprevious(make_t(text))
            else:
                run.append(make_t(text))
            return
        pos += n
    run = ET.Element(RUN_TAG, {"charPrIDRef": str(default_char_pr if default_char_pr is not None else 0)})
    run.append(make_t(text))
    p_el.insert(0, run)


def first_char_pr(p_el) -> str | None:
    fallback = None
    for run in run_elements(p_el):
        cp = run.get("charPrIDRef")
        if fallback is None:
            fallback = cp
        if run_text(run):
            return cp
    return fallback


# ---------------------------------------------------------------------------
# document structure
# ---------------------------------------------------------------------------
def body_paragraphs(doc: HwpxDocument) -> list[HwpxOxmlParagraph]:
    return list(doc.paragraphs)


def top_tables(doc: HwpxDocument) -> list[tuple[HwpxOxmlTable, int]]:
    """Tables anchored directly in body paragraphs, with the paragraph index."""
    out = []
    for i, p in enumerate(body_paragraphs(doc)):
        for run in run_elements(p.element):
            for child in run:
                if child.tag == TBL_TAG:
                    out.append((HwpxOxmlTable(child, p), i))
    return out


def all_pictures(doc: HwpxDocument) -> list:
    out = []
    for section in doc.sections:
        out.extend(section.element.iter(PIC_TAG))
    return out


def get_table(doc: HwpxDocument, index: int) -> HwpxOxmlTable:
    tables = top_tables(doc)
    if not 0 <= index < len(tables):
        raise ToolError(
            f"Table t{index} does not exist; the document has {len(tables)} table(s)"
            + (f" (t0-t{len(tables) - 1})" if tables else "")
            + ". Call hwp_read_document to see table addresses."
        )
    return tables[index][0]


@dataclass
class CellInfo:
    row: int
    col: int
    row_span: int
    col_span: int
    cell: HwpxOxmlTableCell


def table_cells(table: HwpxOxmlTable) -> list[CellInfo]:
    """Physical (anchor) cells in row-major order."""
    out = []
    for row in table.rows:
        for cell in row.cells:
            (r, c), (rs, cs) = cell.address, cell.span
            out.append(CellInfo(r, c, rs, cs, cell))
    out.sort(key=lambda ci: (ci.row, ci.col))
    return out


def cell_grid(table: HwpxOxmlTable) -> dict[tuple[int, int], CellInfo]:
    """Map every logical (row, col) to the anchor cell covering it."""
    grid: dict[tuple[int, int], CellInfo] = {}
    for ci in table_cells(table):
        for r in range(ci.row, ci.row + ci.row_span):
            for c in range(ci.col, ci.col + ci.col_span):
                grid[(r, c)] = ci
    return grid


# ---------------------------------------------------------------------------
# address parsing
# ---------------------------------------------------------------------------
@dataclass
class ParaTarget:
    address: str
    paragraph: HwpxOxmlParagraph


@dataclass
class CellTarget:
    table_index: int
    table: HwpxOxmlTable
    info: CellInfo

    @property
    def address(self) -> str:
        return f"t{self.table_index}.r{self.info.row}.c{self.info.col}"


_P_RE = re.compile(r"^p(\d+|\*)(?:-p?(\d+))?$")
_CELL_RE = re.compile(
    r"^t(\d+)\.r(\d+(?:-\d+)?|\*)\.c(\d+(?:-\d+)?|\*)(?:\.p(\d+(?:-\d+)?|\*))?$"
)


def _span(spec: str, limit: int, what: str, addr: str) -> list[int]:
    if spec == "*":
        return list(range(limit))
    if "-" in spec:
        a, b = (int(x) for x in spec.split("-"))
    else:
        a = b = int(spec)
    if a > b:
        a, b = b, a
    if b >= limit:
        raise ToolError(f'"{addr}": {what} {b} is out of range (valid: 0-{limit - 1}).')
    return list(range(a, b + 1))


def _split_addresses(target: str) -> list[str]:
    parts = [a.strip().replace(" ", "") for a in (target or "").split(",")]
    parts = [a for a in parts if a]
    if not parts:
        raise ToolError('target is empty. Use an address such as "p3", "p2-p5" or "t0.r0.c1".')
    return [a.lower() for a in parts]


def _bad_address(addr: str) -> ToolError:
    return ToolError(
        f'Invalid address "{addr}". Use "p<N>" for body paragraphs (e.g. "p3", "p2-p5", "p*") '
        'or "t<T>.r<R>.c<C>" for table cells (e.g. "t0.r1.c2", "t0.r0.c*", "t0.r1.c2.p0").'
    )


def resolve_cells(doc: HwpxDocument, target: str) -> list[CellTarget]:
    out: list[CellTarget] = []
    seen: set[tuple[int, int, int]] = set()
    for addr in _split_addresses(target):
        m = _CELL_RE.match(addr)
        if not m:
            raise ToolError(f'"{addr}" is not a table-cell address. Use e.g. "t0.r1.c2", "t0.r0.c*" or "t0.r*.c*".')
        ti = int(m.group(1))
        table = get_table(doc, ti)
        grid = cell_grid(table)
        rows = _span(m.group(2), table.row_count, "row", addr)
        cols = _span(m.group(3), table.column_count, "column", addr)
        for r in rows:
            for c in cols:
                ci = grid.get((r, c))
                if ci is None:
                    continue
                key = (ti, ci.row, ci.col)
                if key not in seen:
                    seen.add(key)
                    out.append(CellTarget(ti, table, ci))
    return out


def resolve_paragraphs(doc: HwpxDocument, target: str) -> list[ParaTarget]:
    out: list[ParaTarget] = []
    seen: set[int] = set()
    body = None
    for addr in _split_addresses(target):
        m = _P_RE.match(addr)
        if m:
            if body is None:
                body = body_paragraphs(doc)
            if m.group(1) == "*":
                idx = list(range(len(body)))
            else:
                spec = m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
                try:
                    idx = _span(spec, len(body), "paragraph", addr)
                except ToolError:
                    raise ToolError(
                        f'"{addr}" is out of range: the document body has {len(body)} paragraphs '
                        f"(p0-p{len(body) - 1}). Call hwp_read_document to see addresses."
                    )
            for i in idx:
                if id(body[i].element) not in seen:
                    seen.add(id(body[i].element))
                    out.append(ParaTarget(f"p{i}", body[i]))
            continue
        m = _CELL_RE.match(addr)
        if not m:
            raise _bad_address(addr)
        pspec = m.group(4)
        cell_addr = addr.rsplit(".p", 1)[0] if pspec is not None else addr
        for ct in resolve_cells(doc, cell_addr):
            paras = ct.info.cell.paragraphs
            idx = list(range(len(paras))) if pspec in (None, "*") else _span(pspec, len(paras), "cell paragraph", addr)
            for i in idx:
                if id(paras[i].element) not in seen:
                    seen.add(id(paras[i].element))
                    out.append(ParaTarget(f"{ct.address}.p{i}", paras[i]))
    return out


def resolve_one_paragraph(doc: HwpxDocument, target: str) -> ParaTarget:
    targets = resolve_paragraphs(doc, target)
    if len(targets) != 1:
        raise ToolError(
            f'"{target}" matches {len(targets)} paragraphs; this parameter needs exactly one '
            '(e.g. "p3" or "t0.r1.c2.p0").'
        )
    return targets[0]


def iter_text_paragraphs(doc: HwpxDocument) -> Iterable[ParaTarget]:
    """Every paragraph that can hold text: body paragraphs and table-cell paragraphs."""
    tables_by_para: dict[int, list[int]] = {}
    for ti, (_, pi) in enumerate(top_tables(doc)):
        tables_by_para.setdefault(pi, []).append(ti)
    for i, p in enumerate(body_paragraphs(doc)):
        yield ParaTarget(f"p{i}", p)
        for ti in tables_by_para.get(i, []):
            table = get_table(doc, ti)
            for ci in table_cells(table):
                for k, cp in enumerate(ci.cell.paragraphs):
                    yield ParaTarget(f"t{ti}.r{ci.row}.c{ci.col}.p{k}", cp)
