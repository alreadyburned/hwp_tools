"""Tool implementations. Every public function takes the Store and plain arguments
and returns the text (or JSON-able dict) that the tool reports back."""

from __future__ import annotations

import os
import re
import struct
from copy import deepcopy
from pathlib import Path
from typing import Any

from lxml import etree as ET

from hwpx import HwpxDocument
from hwpx.oxml import HwpxOxmlParagraph

from . import formats, tables
from .model import (
    HP, P_TAG, PIC_TAG, RUN_TAG, T_TAG, TBL_TAG, ParaTarget, all_pictures, body_paragraphs, cell_grid, clear_layout_cache,
    first_char_pr, get_table, hu_to_mm, iter_text_paragraphs, merge_adjacent_runs, mm_to_hu, paragraph_text,
    replace_range,
    resolve_cells, resolve_one_paragraph, resolve_paragraphs, run_elements, run_text, runs_in_range, t_text,
    table_cells, top_tables,
)
from .store import Store, ToolError, normalize_path

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = text.replace("\t", "\\t").replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[:limit] + f"…(+{len(text) - limit} chars)"


def _style_name(doc: HwpxDocument, style_id: str | None) -> str:
    st = doc.styles.get(style_id) if style_id is not None else None
    return st.name if st is not None and st.name else f"style {style_id}"


_OBJECT_NAMES = {"tbl", "pic", "equation", "rect", "ellipse", "line", "arc", "polygon", "curve", "connectLine",
                 "container", "textart", "ole", "video", "chart"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _para_objects(p_el) -> list:
    """Tables, pictures, equations and drawing objects anchored in the paragraph's runs."""
    out = []
    for run in run_elements(p_el):
        for child in run:
            if _local(child.tag) in _OBJECT_NAMES:
                out.append(child)
    return out


def _object_label(doc: HwpxDocument, obj, pic_index: dict) -> str:
    name = _local(obj.tag)
    if name == "tbl":
        return f"<table t{_table_index_of(doc, obj)}: {obj.get('rowCnt')} rows x {obj.get('colCnt')} cols>"
    sz = obj.find(f"{HP}sz")
    size = f"{hu_to_mm(int(sz.get('width'))):g}x{hu_to_mm(int(sz.get('height'))):g} mm" if sz is not None else ""
    pos = obj.find(f"{HP}pos")
    floating = " floating" if pos is not None and pos.get("treatAsChar") == "0" else ""
    if name == "pic":
        return f"<image g{pic_index.get(id(obj), '?')}: {size}{floating}>"
    if name == "equation":
        script = obj.findtext(f"{HP}script") or ""
        return f"<equation: {_clip(script, 60)}>"
    inner = [paragraph_text(p) for p in obj.iter(P_TAG)]
    if inner and any(inner):
        return f"<textbox ({_local(obj.tag)}) {size}{floating}: {_clip(' / '.join(inner), 80)}>"
    return f"<shape {name} {size}{floating}>"


def _inline_view(doc: HwpxDocument, p_el, pic_index: dict, max_chars: int) -> str:
    """Paragraph text with object labels in document order, text clipped to max_chars."""
    parts: list[str] = []
    used = hidden = 0
    for run in run_elements(p_el):
        for child in run:
            if child.tag == T_TAG:
                chunk = t_text(child)
                keep = chunk[: max(0, max_chars - used)]
                used += len(keep)
                hidden += len(chunk) - len(keep)
                if keep:
                    parts.append(keep.replace("\t", "\\t").replace("\n", "\\n"))
            elif _local(child.tag) in _OBJECT_NAMES:
                parts.append(f" {_object_label(doc, child, pic_index)} ")
    view = "".join(parts).strip()
    if hidden:
        view += f"…(+{hidden} chars)"
    return view or "(empty)"


def _control_notes(p_el) -> list[str]:
    """Footnotes, endnotes, links, bookmarks and fields in the paragraph (not inside nested objects)."""
    notes = []
    for run in run_elements(p_el):
        for ctrl in run.findall(f"{HP}ctrl"):
            for c in ctrl:
                name = _local(c.tag)
                if name in ("footNote", "endNote"):
                    text = " ".join(paragraph_text(p) for p in c.iter(P_TAG))
                    notes.append(f"{'footnote' if name == 'footNote' else 'endnote'}: {_clip(text, 40)}")
                elif name == "bookmark":
                    notes.append(f"bookmark {c.get('name')!r}")
                elif name == "fieldBegin":
                    kind = c.get("type", "")
                    if kind == "HYPERLINK":
                        notes.append(f"link -> {c.get('name')}")
                    elif kind == "CLICK_HERE":
                        notes.append(f"form field {c.get('name')!r}")
                    elif kind == "MEMO":
                        notes.append("memo")
                    elif kind:
                        notes.append(f"field {kind.lower()}")
    return notes


def _page_info(doc: HwpxDocument, section_index: int = 0) -> str:
    props = doc.sections[section_index].properties
    size, m = props.page_size, props.page_margins
    w, h = hu_to_mm(size.width), hu_to_mm(size.height)
    landscape = (size.orientation or "").upper() == "NARROWLY"
    if landscape:
        w, h = h, w
    names = {(210.0, 297.0): "A4", (297.0, 420.0): "A3", (182.0, 257.0): "B5", (257.0, 364.0): "B4",
             (215.9, 279.4): "Letter", (215.9, 355.6): "Legal", (148.0, 210.0): "A5"}
    key = (min(w, h), max(w, h))
    name = next((n for (a, b), n in names.items() if abs(a - key[0]) < 1 and abs(b - key[1]) < 1), "custom")
    return (
        f"{name} {'landscape' if landscape else 'portrait'} {w:g}x{h:g} mm; margins left {hu_to_mm(m.left):g}, "
        f"right {hu_to_mm(m.right):g}, top {hu_to_mm(m.top):g}, bottom {hu_to_mm(m.bottom):g}, "
        f"header {hu_to_mm(m.header):g}, footer {hu_to_mm(m.footer):g} mm"
    )


def _text_width_hu(doc: HwpxDocument, section_index: int = 0) -> int:
    props = doc.sections[section_index].properties
    size, m = props.page_size, props.page_margins
    width = size.height if (size.orientation or "").upper() == "NARROWLY" else size.width
    return int(width) - int(m.left) - int(m.right) - int(m.gutter or 0)


def _blank_last_section_paragraph(doc: HwpxDocument):
    """The only paragraph of the last section if it is empty (fresh document or new section), else None."""
    paras = doc.sections[-1].paragraphs
    if len(paras) == 1 and paragraph_text(paras[0].element) == "" and not _para_objects(paras[0].element):
        return paras[0]
    return None


def _move_section_controls(old_first, new_first) -> None:
    """Keep section-level controls (secPr, column/page controls) in the first paragraph."""
    runs = run_elements(old_first)
    if not runs:
        return
    first = runs[0]
    controls = []
    for child in first:
        if child.tag == T_TAG and t_text(child):
            break
        if child.tag in (HP + "secPr", HP + "ctrl"):
            controls.append(child)
    if not controls:
        return
    holder = ET.Element(RUN_TAG, {"charPrIDRef": first.get("charPrIDRef", "0")})
    for c in controls:
        first.remove(c)
        holder.append(c)
    new_runs = run_elements(new_first)
    if new_runs:
        new_runs[0].addprevious(holder)
    else:
        new_first.insert(0, holder)
    if len(first) == 0:
        if len(run_elements(old_first)) > 1:
            old_first.remove(first)
        else:
            first.append(ET.Element(T_TAG))
    clear_layout_cache(old_first)


def _format_source(doc: HwpxDocument, style: str | None, like: str | None, anchor: ParaTarget | None):
    """(styleIDRef, paraPrIDRef, charPrIDRef) for a new paragraph."""
    if style is not None and like is not None:
        raise ToolError("Pass either style or like, not both.")
    if like is not None:
        src = resolve_one_paragraph(doc, like).paragraph.element
        return src.get("styleIDRef"), src.get("paraPrIDRef"), first_char_pr(src)
    if style is None and anchor is not None and "." in anchor.address:
        src = anchor.paragraph.element  # inside a table cell: match the neighbouring paragraph
        return src.get("styleIDRef"), src.get("paraPrIDRef"), first_char_pr(src)
    st = formats.resolve_style(doc, style if style is not None else 0)
    if st.get("type", "PARA").upper() != "PARA":
        raise ToolError(f'"{st.get("name")}" is a character style; use a paragraph style here.')
    return st.get("id"), st.get("paraPrIDRef"), st.get("charPrIDRef")


def _new_paragraph(doc: HwpxDocument, after: str | None, fmt: tuple) -> tuple[HwpxOxmlParagraph, str]:
    """Create an empty paragraph at the requested place. Returns it and a location note."""
    style_id, para_pr, char_pr = fmt
    where = (after or "end").strip().lower()
    if where == "end":
        section = doc.sections[-1]
        ref_el = None
    elif where == "start":
        first = body_paragraphs(doc)[0]
        section, ref_el = first.section, None
    else:
        anchor = resolve_one_paragraph(doc, where)
        section, ref_el = anchor.paragraph.section, anchor.paragraph.element
    p = section.add_paragraph("", style_id_ref=style_id, para_pr_id_ref=para_pr, char_pr_id_ref=char_pr)
    el = p.element
    if where == "start":
        first_el = body_paragraphs(doc)[0].element
        first_el.addprevious(el)
        _move_section_controls(first_el, el)
    elif ref_el is not None:
        ref_el.addnext(el)
    for run in run_elements(el):
        run.set("charPrIDRef", str(char_pr))
    section.mark_dirty()
    return HwpxOxmlParagraph(el, section), where


def _address_of(doc: HwpxDocument, p_el) -> str:
    for t in iter_text_paragraphs(doc):
        if t.paragraph.element is p_el:
            return t.address
    return "?"


def _table_index_of(doc: HwpxDocument, tbl_el) -> int:
    for i, (t, _) in enumerate(top_tables(doc)):
        if t.element is tbl_el:
            return i
    return -1


def _check_hwp_safe(store: Store, doc: HwpxDocument, path: str) -> None:
    lost = store.conversion_warnings(doc)
    if lost and path.lower().endswith(".hwp"):
        raise ToolError(
            "This .hwp file contains content the converter could not carry over ("
            + "; ".join(lost)
            + "). Editing it in place would lose that content. Use hwp_save_as to write a copy "
            "(.hwpx recommended) and edit the copy."
        )


class _Edit:
    """``with _Edit(store, path) as doc`` - edit with the .hwp safety check."""

    def __init__(self, store: Store, path: str):
        self.store, self.path = store, normalize_path(path)
        self._cm = None

    def __enter__(self) -> HwpxDocument:
        self._cm = self.store.edit(self.path)
        doc = self._cm.__enter__()
        try:
            _check_hwp_safe(self.store, doc, self.path)
        except BaseException as exc:
            self._cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return doc

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


def create_document(store: Store, path: str, overwrite: bool, page: dict[str, Any]) -> str:
    path = normalize_path(path)
    if os.path.exists(path) and not overwrite:
        raise ToolError(f"{path} already exists. Pass overwrite=true to replace it, or choose another path.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc = HwpxDocument.new()
    if any(v is not None for v in page.values()):
        _apply_page_setup(doc, 0, page)
    store.forget(path)
    store.create(path, doc)
    return (
        f"Created {path} ({'HWP 5.0' if path.lower().endswith('.hwp') else 'HWPX'}). "
        f"Page: {_page_info(doc)}. It has one empty paragraph p0; the first hwp_insert_paragraph fills it."
    )


def save_as(store: Store, path: str, new_path: str, overwrite: bool) -> str:
    src = normalize_path(path)
    dst = normalize_path(new_path)
    if os.path.exists(dst) and not overwrite:
        raise ToolError(f"{dst} already exists. Pass overwrite=true to replace it.")
    doc = store.open(src)
    lost = store.conversion_warnings(doc)
    store.forget(dst)
    store.create(dst, doc)
    store.forget(src)
    store.forget(dst)
    msg = f"Saved a copy as {dst} ({'HWP 5.0' if dst.lower().endswith('.hwp') else 'HWPX'}). Edit the new path from now on."
    if lost:
        msg += " Content not carried over from the original .hwp: " + "; ".join(lost) + "."
    return msg


def undo(store: Store, path: str) -> str:
    return store.undo(normalize_path(path))


def format_summary(doc: HwpxDocument, el) -> str:
    """Short paragraph + character format of a paragraph, e.g. "center, line 160%; 맑은 고딕 14pt bold"."""
    pdesc = formats.short_para_desc(formats.describe_para_pr(doc, el.get("paraPrIDRef")))
    cdesc = formats.short_char_desc(formats.describe_char_pr(doc, first_char_pr(el)))
    distinct = {r.get("charPrIDRef") for r in run_elements(el) if run_text(r)}
    return "; ".join(x for x in (pdesc, cdesc + (" (+mixed runs)" if len(distinct) > 1 else "")) if x)


def paragraph_line(doc: HwpxDocument, el, address: str, pic_index: dict, max_chars: int, show_format: bool) -> str:
    """One listing line: `p3 [style] text  [notes]  {format}`."""
    line = f"{address} [{_style_name(doc, el.get('styleIDRef'))}] " + _inline_view(doc, el, pic_index, max_chars)
    notes = _control_notes(el)
    if notes:
        line += "  [" + "; ".join(notes) + "]"
    if show_format:
        fmt = format_summary(doc, el)
        if fmt:
            line += f"  {{{fmt}}}"
    return line


def table_rows(doc: HwpxDocument, ti: int, max_chars: int, rows: range | None = None) -> list[str]:
    """`tN.rR | c0: ... | c1: ...` lines for the table's rows (all rows, or those in ``rows``)."""
    table = get_table(doc, ti)
    by_row: dict[int, list[str]] = {}
    for ci in table_cells(table):
        if rows is not None and ci.row not in rows:
            continue
        label = f"c{ci.col}"
        if ci.col_span > 1 or ci.row_span > 1:
            label += f"(merged r{ci.row}-{ci.row + ci.row_span - 1} c{ci.col}-{ci.col + ci.col_span - 1})"
        by_row.setdefault(ci.row, []).append(f"{label}: {_clip(ci.cell.text, max_chars)}")
    return [f"t{ti}.r{r} | " + " | ".join(by_row[r]) for r in sorted(by_row)]


def _result_view(doc: HwpxDocument, addresses: list[str], limit: int = 5, max_chars: int = 160) -> str:
    """The affected paragraphs as they are now, so the caller can check an edit without re-reading."""
    if not addresses:
        return ""
    pics = all_pictures(doc)
    pic_index = {id(el): i for i, el in enumerate(pics)}
    lines = []
    for addr in addresses[:limit]:
        t = resolve_one_paragraph(doc, addr)
        lines.append("  " + paragraph_line(doc, t.paragraph.element, t.address, pic_index, max_chars, True))
    if len(addresses) > limit:
        lines.append(f"  ... {len(addresses) - limit} more")
    return "\nNow:\n" + "\n".join(lines)


def _shift_note(first_after: str, delta: int) -> str:
    """How later addresses moved after inserting/deleting paragraphs before ``first_after``."""
    m = re.match(r"^(.*?)(\d+)$", first_after)
    if not m or delta == 0:
        return ""
    prefix, n = m.group(1), int(m.group(2))
    return f"\nAddresses after this point shifted by {delta:+d} (the old {prefix}{n} is now {prefix}{n + delta})."


def get_paragraph(store: Store, path: str, target: str) -> dict[str, Any]:
    doc = store.open(normalize_path(path))
    t = resolve_one_paragraph(doc, target)
    el = t.paragraph.element
    pics = all_pictures(doc)
    runs, pos = [], 0
    for run in run_elements(el):
        text = "".join(t_text(c) for c in run if c.tag == T_TAG)
        if not text:
            continue
        info = {"start": pos, "end": pos + len(text), "text": text}
        info.update(formats.describe_char_pr(doc, run.get("charPrIDRef")))
        runs.append(info)
        pos += len(text)
    return {
        "address": t.address,
        "text": paragraph_text(el),
        "style": _style_name(doc, el.get("styleIDRef")),
        "paragraph_format": formats.describe_para_pr(doc, el.get("paraPrIDRef")),
        "runs": runs,
        "objects": [_object_label(doc, o, {id(g): i for i, g in enumerate(pics)}) for o in _para_objects(el)],
        "controls": _control_notes(el),
    }


def find_text(store: Store, path: str, text: str, ignore_case: bool, max_results: int) -> dict[str, Any]:
    if not text:
        raise ToolError("text is empty.")
    doc = store.open(normalize_path(path))
    flags = re.IGNORECASE if ignore_case else 0
    pattern = re.compile(re.escape(text), flags)
    results, total = [], 0
    for t in iter_text_paragraphs(doc):
        ptext = paragraph_text(t.paragraph.element)
        for m in pattern.finditer(ptext):
            total += 1
            if len(results) < max_results:
                a, b = max(0, m.start() - 25), min(len(ptext), m.end() + 25)
                results.append({
                    "address": t.address, "start": m.start(), "end": m.end(),
                    "context": ("…" if a else "") + ptext[a:b].replace("\n", "\\n") + ("…" if b < len(ptext) else ""),
                })
    return {"matches": total, "shown": len(results), "results": results}


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


def insert_paragraph(store: Store, path: str, text: str, after: str | None, style: str | None, like: str | None) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    with _Edit(store, path) as doc:
        anchor = None
        if after and after.strip().lower() not in ("end", "start"):
            anchor = resolve_one_paragraph(doc, after)
        fmt = _format_source(doc, style, like, anchor)
        created = []
        cursor = after
        start_idx = 0
        blank = _blank_last_section_paragraph(doc) if after is None or after.strip().lower() == "end" else None
        if blank is not None:
            p0 = blank
            el = p0.element
            el.set("styleIDRef", str(fmt[0]))
            el.set("paraPrIDRef", str(fmt[1]))
            replace_range(el, 0, 0, lines[0], fmt[2])
            for run in run_elements(el):
                run.set("charPrIDRef", str(fmt[2]))
            p0.section.mark_dirty()
            created.append(el)
            start_idx = 1
            cursor = _address_of(doc, el)
        for line in lines[start_idx:]:
            p, _ = _new_paragraph(doc, cursor, fmt)
            replace_range(p.element, 0, 0, line, fmt[2])
            created.append(p.element)
            cursor = _address_of(doc, p.element)
        addresses = [_address_of(doc, e) for e in created]
        added = len(addresses) - start_idx
        followed = _next_address(doc, addresses[-1]) is not None
    span = addresses[0] if len(addresses) == 1 else f"{addresses[0]}-{addresses[-1].split('.')[-1]}"
    return (
        f"Inserted {len(addresses)} paragraph(s) at {span} with style \"{_style_name(doc, str(fmt[0]))}\"."
        + (_shift_note(addresses[start_idx], added) if followed else "")
        + _result_view(doc, addresses)
    )


def _next_address(doc: HwpxDocument, address: str) -> str | None:
    """The address right after ``address`` in the same container, if that paragraph exists."""
    m = re.match(r"^(.*?)(\d+)$", address)
    nxt = f"{m.group(1)}{int(m.group(2)) + 1}"
    try:
        resolve_one_paragraph(doc, nxt)
    except ToolError:
        return None
    return nxt


def set_paragraph_text(store: Store, path: str, target: str, text: str) -> str:
    with _Edit(store, path) as doc:
        t = resolve_one_paragraph(doc, target)
        el = t.paragraph.element
        replace_range(el, 0, len(paragraph_text(el)), text, first_char_pr(el))
        t.paragraph.section.mark_dirty()
    return f"Replaced the text of {t.address}." + _result_view(doc, [t.address], max_chars=400)


def delete_paragraphs(store: Store, path: str, target: str) -> str:
    with _Edit(store, path) as doc:
        targets = resolve_paragraphs(doc, target)
        removed, cleared = [], []
        # group by container so we never empty a section or a cell
        # (lxml proxies are recreated on access, so never key by id(); compare elements directly)
        containers: list[tuple[Any, list]] = []
        for t in targets:
            parent = t.paragraph.element.getparent()
            group = next((g for c, g in containers if c == parent), None)
            if group is None:
                containers.append((parent, [t]))
            else:
                group.append(t)
        for _parent, group in containers:
            parent = group[0].paragraph.element.getparent()
            siblings = [c for c in parent if c.tag == HP + "p"]
            doomed = {id(t.paragraph.element) for t in group}
            survivors = [s for s in siblings if id(s) not in doomed]
            for t in group:
                el = t.paragraph.element
                if not survivors and t is group[0]:
                    # last paragraph of a section/cell: clear it instead of removing it
                    for run in run_elements(el):
                        for child in list(run):
                            if child.tag in (T_TAG, TBL_TAG, PIC_TAG):
                                run.remove(child)
                        if len(run) == 0:
                            run.append(ET.Element(T_TAG))
                    clear_layout_cache(el)
                    cleared.append(t.address)
                    continue
                if el is siblings[0] and survivors:
                    _move_section_controls(el, survivors[0])
                parent.remove(el)
                removed.append(t.address)
            group[0].paragraph.section.mark_dirty()
    msg = f"Deleted {len(removed)} paragraph(s)" + (f": {', '.join(removed[:20])}" if removed else "") + "."
    if cleared:
        msg += f" Cleared (not removed, a section/cell must keep one paragraph): {', '.join(cleared)}."
    if removed:
        m = re.match(r"^(.*?)(\d+)$", removed[0])
        prefix, first = m.group(1), int(m.group(2))
        same = [r for r in removed if r.startswith(prefix) and r[len(prefix):].isdigit()]
        last = max(int(r[len(prefix):]) for r in same)
        if len(same) == len(removed) == last - first + 1:  # one contiguous block
            try:
                view = _result_view(doc, [removed[0]])
                msg += f"\nAddresses after it shifted by -{len(removed)} (the old {prefix}{last + 1} is now {removed[0]})." + view
            except ToolError:
                pass  # the block was at the end: nothing follows it
        else:
            msg += "\nRemaining paragraphs were renumbered; re-read before using more addresses."
    return msg


def replace_text(store: Store, path: str, find: str, replace: str, target: str | None, ignore_case: bool, max_count: int | None) -> str:
    if not find:
        raise ToolError("find is empty.")
    with _Edit(store, path) as doc:
        scope = resolve_paragraphs(doc, target) if target else list(iter_text_paragraphs(doc))
        pattern = re.compile(re.escape(find), re.IGNORECASE if ignore_case else 0)
        count, where, views = 0, [], []
        for t in scope:
            el = t.paragraph.element
            matches = list(pattern.finditer(paragraph_text(el)))
            if max_count is not None:
                matches = matches[: max(0, max_count - count)]
            for m in reversed(matches):
                replace_range(el, m.start(), m.end(), replace)
            if matches:
                count += len(matches)
                where.append(t.address)
                t.paragraph.section.mark_dirty()
                if len(views) < 5:  # earlier text is untouched, so the first match still starts at m.start()
                    new_text, s = paragraph_text(el), matches[0].start()
                    a, b = max(0, s - 40), min(len(new_text), s + len(replace) + 40)
                    views.append(f"  {t.address}: " + ("…" if a else "") + _clip(new_text[a:b], 200)
                                 + ("…" if b < len(new_text) else ""))
            if max_count is not None and count >= max_count:
                break
        if count == 0:
            raise ToolError(f'"{find}" was not found' + (f" in {target}" if target else "") + ". Nothing changed.")
    return (f"Replaced {count} occurrence(s) in {', '.join(where[:20])}{' ...' if len(where) > 20 else ''}."
            + "\nNow:\n" + "\n".join(views) + (f"\n  ... {len(where) - 5} more paragraph(s)" if len(where) > 5 else ""))


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------


def apply_to_text(doc: HwpxDocument, target: str, match: str | None, occurrence: int | None,
                  start: int | None, end: int | None, fn) -> list[str]:
    """Call fn(runs) on the text runs selected by target + match/occurrence or start/end.

    Runs are split so that the selection boundaries fall on run boundaries. Returns the
    formatted spans as "address[start:end]" labels; raises if nothing matched.
    """
    if match is not None and (start is not None or end is not None):
        raise ToolError("Use either match or start/end, not both.")
    targets = resolve_paragraphs(doc, target)
    if (start is not None or end is not None) and len(targets) != 1:
        raise ToolError("start/end offsets need a single-paragraph target.")
    done: list[str] = []
    seen = 0
    for t in targets:
        el = t.paragraph.element
        text = paragraph_text(el)
        if match is not None:
            spans = [(m.start(), m.end()) for m in re.finditer(re.escape(match), text)]
            if occurrence is not None:
                picked = []
                for s in spans:
                    seen += 1
                    if seen == occurrence:
                        picked.append(s)
                spans = picked
        elif start is not None or end is not None:
            s, e = start or 0, len(text) if end is None else end
            if not 0 <= s < e <= len(text):
                raise ToolError(f"Invalid range {s}-{e}: {t.address} has {len(text)} characters.")
            spans = [(s, e)]
        else:
            spans = [(0, len(text))] if text else []
            if not text:  # empty paragraph: format its runs so typed text picks it up
                fn(run_elements(el))
                done.append(t.address)
        for s, e in reversed(spans):
            fn(runs_in_range(el, s, e))
            done.append(f"{t.address}[{s}:{e}]")
        if spans:
            merge_adjacent_runs(el)
            clear_layout_cache(el)
            t.paragraph.section.mark_dirty()
    if not done:
        what = f'"{match}"' + (f" (occurrence {occurrence})" if occurrence else "")
        raise ToolError(f"{what} was not found in {target}. Nothing changed. Use hwp_find_text to locate text.")
    return done


def format_text(store: Store, path: str, target: str, match: str | None, occurrence: int | None,
                start: int | None, end: int | None, opts: dict[str, Any], extra: dict[str, Any] | None = None) -> str:
    """``extra`` passes further python-hwpx ensure_run options (shadow, outline, emboss, ...) - API only."""
    kw = formats.char_kwargs(opts)
    kw.update({k: v for k, v in (extra or {}).items() if v is not None})
    if not kw:
        raise ToolError("No formatting given. Pass at least one of: " + ", ".join(formats.CHAR_OPTION_KEYS) + ".")
    with _Edit(store, path) as doc:
        done = apply_to_text(doc, target, match, occurrence, start, end,
                             lambda runs: formats.apply_char_format(doc, runs, kw))
    applied = ", ".join(f"{k}={v}" for k, v in {**opts, **(extra or {})}.items()
                        if v is not None and (k in formats.CHAR_OPTION_KEYS or k in (extra or {})))
    return (f"Applied {applied} to {len(done)} span(s): {', '.join(done[:15])}{' ...' if len(done) > 15 else ''}."
            + _span_view(doc, done))


_SPAN_RE = re.compile(r"^(.*)\[(\d+):(\d+)\]$")


def _span_view(doc: HwpxDocument, spans: list[str], limit: int = 5) -> str:
    """The character format now in effect for formatted spans labelled "address[start:end]"."""
    lines = []
    for label in spans[:limit]:
        m = _SPAN_RE.match(label)
        if not m:
            continue
        el = resolve_one_paragraph(doc, m.group(1)).paragraph.element
        start, end = int(m.group(2)), int(m.group(3))
        pos, char_pr = 0, None
        for run in run_elements(el):
            n = len(run_text(run))
            if n and pos <= start < pos + n:
                char_pr = run.get("charPrIDRef")
                break
            pos += n
        desc = formats.short_char_desc(formats.describe_char_pr(doc, char_pr))
        lines.append(f'  {label} "{_clip(paragraph_text(el)[start:end], 40)}": {desc}')
    if not lines:
        return ""
    more = f"\n  ... {len(spans) - limit} more" if len(spans) > limit else ""
    return "\nNow:\n" + "\n".join(lines) + more


def set_paragraph_format(store: Store, path: str, target: str, opts: dict[str, Any]) -> str:
    kw = formats.para_kwargs(opts)
    if not kw:
        raise ToolError("No formatting given. Pass at least one of: " + ", ".join(formats.PARA_OPTION_KEYS) + ".")
    with _Edit(store, path) as doc:
        targets = resolve_paragraphs(doc, target)
        formats.apply_para_format(doc, [t.paragraph for t in targets], kw)
    applied = ", ".join(f"{k}={v}" for k, v in opts.items() if v is not None and k in formats.PARA_OPTION_KEYS)
    return (f"Applied {applied} to {len(targets)} paragraph(s): {', '.join(t.address for t in targets[:15])}"
            f"{' ...' if len(targets) > 15 else ''}." + _result_view(doc, [t.address for t in targets], limit=3, max_chars=60))


def list_styles(store: Store, path: str) -> dict[str, Any]:
    doc = store.open(normalize_path(path))
    return {"styles": formats.list_styles(doc)}


def apply_style(store: Store, path: str, target: str, style: str, keep_char_format: bool) -> str:
    with _Edit(store, path) as doc:
        st = formats.resolve_style(doc, style)
        targets = resolve_paragraphs(doc, target)
        formats.apply_style(doc, [t.paragraph for t in targets], st, keep_char_format)
        para = formats.describe_para_pr(doc, st.get("paraPrIDRef"))
    note = f' Note: this style adds automatic numbering ({para["numbering"]}); do not type numbers like "1." in the text.' if para.get("numbering") else ""
    return (f'Applied style "{st.get("name")}" to {len(targets)} paragraph(s): {", ".join(t.address for t in targets[:15])}.{note}'
            + _result_view(doc, [t.address for t in targets], limit=3, max_chars=60))


def create_style(store: Store, path: str, name: str, base_style: str | None, char_opts: dict, para_opts: dict) -> str:
    char_kw, para_kw = formats.char_kwargs(char_opts), formats.para_kwargs(para_opts)
    with _Edit(store, path) as doc:
        info = formats.create_style(doc, name, base_style, char_kw, para_kw)
    return f'Created paragraph style "{info["name"]}" (id {info["id"]}). Apply it with hwp_apply_style.'


def set_list(store: Store, path: str, target: str, kind: str, level: int, bullet_char: str | None,
             number_format: str | None, start_number: int | None) -> str:
    kind = kind.lower()
    if kind not in ("bullet", "number", "none"):
        raise ToolError('kind must be "bullet", "number" or "none".')
    if not 1 <= level <= 7:
        raise ToolError("level must be between 1 and 7.")
    with _Edit(store, path) as doc:
        targets = resolve_paragraphs(doc, target)
        body = [p.element for p in body_paragraphs(doc)]
        idx = []
        for t in targets:
            pos = next((i for i, el in enumerate(body) if el == t.paragraph.element), None)
            if pos is None:
                raise ToolError(f"{t.address}: lists can only be applied to body paragraphs (p<N>), not table cells.")
            idx.append(pos)
        if kind == "none":
            for t in targets:
                pp = doc.styles.paragraph_property(t.paragraph.element.get("paraPrIDRef"))
                if pp is not None and pp.heading is not None and (pp.heading.type or "NONE") != "NONE":
                    _clear_heading(doc, t.paragraph)
        else:
            fmt_map = {None: None, "1": "DIGIT", "digit": "DIGIT", "가": "HANGUL_SYLLABLE", "a": "LATIN_SMALL",
                       "A": "LATIN_CAPITAL", "i": "ROMAN_SMALL", "I": "ROMAN_CAPITAL", "①": "CIRCLED_DIGIT",
                       "ㄱ": "HANGUL_JAMO"}
            nf = fmt_map.get(number_format, number_format)
            doc.styles.apply_list_format(paragraph_indexes=idx, kind=kind, level=level, bullet_char=bullet_char,
                                         number_format=nf, start=start_number)
        for t in targets:
            clear_layout_cache(t.paragraph.element)
    return f"Set list kind={kind} level={level} on {len(targets)} paragraph(s): {', '.join(t.address for t in targets[:15])}."


def _clear_heading(doc: HwpxDocument, paragraph) -> None:
    """Derive a paraPr without numbering/bullet (heading type NONE) for the paragraph."""
    header = doc.parts.headers[0]
    base = paragraph.element.get("paraPrIDRef")
    container = header.element.find(f".//{formats.HH}paraProperties")
    src = next((pp for pp in container if pp.get("id") == base), None)
    if src is None:
        return
    new = deepcopy(src)
    for h in new.iter(f"{formats.HH}heading"):
        h.set("type", "NONE")
        h.set("idRef", "0")
        h.set("level", "0")
    new_id = str(max(int(pp.get("id")) for pp in container) + 1)
    new.set("id", new_id)
    container.append(new)
    container.set("itemCnt", str(len(container)))
    header.mark_dirty()
    paragraph.element.set("paraPrIDRef", new_id)
    paragraph.section.mark_dirty()


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def insert_table(store: Store, path: str, after: str | None, rows: int | None, cols: int | None,
                 data: list[list[Any]] | None, column_widths_mm: list[float] | None, header_row: bool) -> str:
    if data is not None:
        if not data or not all(isinstance(r, list) for r in data):
            raise ToolError("data must be a list of rows, e.g. [[\"이름\", \"나이\"], [\"홍길동\", \"30\"]].")
        rows = rows or len(data)
        cols = cols or max(len(r) for r in data)
    if not rows or not cols:
        raise ToolError("Pass rows and cols (or data).")
    if not (1 <= rows <= 500 and 1 <= cols <= 60):
        raise ToolError("rows must be 1-500 and cols 1-60.")
    with _Edit(store, path) as doc:
        where = (after or "end").strip().lower()
        if where not in ("end", "start") and "." in where:
            raise ToolError("Tables can only be placed after body paragraphs (p<N>), not inside table cells.")
        holder, _ = _new_paragraph(doc, after, _format_source(doc, None, None, None))
        section_index = next(i for i, s in enumerate(doc.sections) if s.element is holder.section.element)
        width = _text_width_hu(doc, section_index)
        table = holder.add_table(rows, cols, width=width)
        if data:
            for r, row in enumerate(data[:rows]):
                for c, value in enumerate(row[:cols]):
                    if value is not None and str(value) != "":
                        table.set_cell_text(r, c, str(value), split_paragraphs=True)
        ti = _table_index_of(doc, table.element)
        if column_widths_mm:
            tables.set_layout(doc, ti, column_widths_mm=column_widths_mm, row_heights_mm=None, align=None, repeat_header_row=None)
        if header_row:
            cells = resolve_cells(doc, f"t{ti}.r0.c*")
            tables.format_cells(doc, cells, fill_color="#E7E6E6", border_type=None, border_width_mm=None,
                                border_color=None, borders="all", vertical_align="middle", padding_mm=None)
            paras = resolve_paragraphs(doc, f"t{ti}.r0.c*")
            for t in paras:
                formats.apply_char_format(doc, run_elements(t.paragraph.element), {"bold": True})
            formats.apply_para_format(doc, [t.paragraph for t in paras], {"alignment": "center"})
        address = _address_of(doc, holder.element)
    return (
        f"Inserted table t{ti} ({rows} rows x {cols} cols) in paragraph {address}. "
        f"Cell addresses are t{ti}.r<row>.c<col> (0-based)."
    )


def get_table_info(store: Store, path: str, table: int, include_format: bool) -> dict[str, Any]:
    doc = store.open(normalize_path(path))
    return tables.describe_table(doc, table, include_format)


def set_cell_text(store: Store, path: str, table: int, data: list[list[Any]], start_row: int, start_col: int) -> str:
    if not data or not all(isinstance(r, list) for r in data):
        raise ToolError('data must be a 2D list, e.g. [["A1", "B1"], ["A2", "B2"]]; use [["text"]] for one cell.')
    with _Edit(store, path) as doc:
        tbl = get_table(doc, table)
        grid = cell_grid(tbl)
        written, skipped = 0, []
        for dr, row in enumerate(data):
            for dc, value in enumerate(row):
                if value is None:
                    continue
                r, c = start_row + dr, start_col + dc
                ci = grid.get((r, c))
                if ci is None:
                    raise ToolError(f"t{table}.r{r}.c{c} is outside the table ({tbl.row_count} rows x {tbl.column_count} cols). Insert rows/columns first with hwp_table_structure.")
                if (ci.row, ci.col) != (r, c):
                    if str(value) != "":
                        skipped.append(f"t{table}.r{r}.c{c} (inside merged cell t{table}.r{ci.row}.c{ci.col})")
                    continue
                ci.cell.set_text(str(value), preserve_format=True, split_paragraphs=True)
                for p in ci.cell.paragraphs:
                    clear_layout_cache(p.element)
                written += 1
        tbl.paragraph.section.mark_dirty()
    msg = f"Wrote {written} cell(s) in t{table}."
    if skipped:
        msg += " Skipped (write to the merged cell's top-left address instead): " + ", ".join(skipped)
    shown = range(start_row, start_row + min(len(data), 5))
    msg += "\nNow:\n" + "\n".join("  " + line for line in table_rows(doc, table, 60, shown))
    if len(data) > 5:
        msg += f"\n  ... {len(data) - 5} more row(s)"
    return msg


def _rect(doc: HwpxDocument, cells: str) -> tuple[int, int, int, int, int]:
    targets = resolve_cells(doc, cells)
    if not targets or len({t.table_index for t in targets}) != 1 or "," in cells:
        raise ToolError('cells must be one rectangular range in one table, e.g. "t0.r0.c0-2" or "t0.r1-3.c1".')
    r0 = min(t.info.row for t in targets)
    c0 = min(t.info.col for t in targets)
    r1 = max(t.info.row + t.info.row_span - 1 for t in targets)
    c1 = max(t.info.col + t.info.col_span - 1 for t in targets)
    return targets[0].table_index, r0, c0, r1, c1


def merge_cells(store: Store, path: str, cells: str) -> str:
    with _Edit(store, path) as doc:
        ti, r0, c0, r1, c1 = _rect(doc, cells)
        if (r0, c0) == (r1, c1):
            raise ToolError("The range covers a single cell; nothing to merge.")
        tbl = get_table(doc, ti)
        try:
            tbl.merge_cells(r0, c0, r1, c1)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Cannot merge: {exc}") from exc
        tbl.paragraph.section.mark_dirty()
    return f"Merged t{ti} rows {r0}-{r1}, cols {c0}-{c1} into cell t{ti}.r{r0}.c{c0} (texts were joined)."


def split_cell(store: Store, path: str, cell: str) -> str:
    with _Edit(store, path) as doc:
        targets = resolve_cells(doc, cell)
        if len(targets) != 1:
            raise ToolError('cell must address one cell, e.g. "t0.r0.c1".')
        t = targets[0]
        if t.info.row_span == 1 and t.info.col_span == 1:
            raise ToolError(f"{t.address} is not a merged cell.")
        try:
            t.table.split_merged_cell(t.info.row, t.info.col)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"Cannot split: {exc}") from exc
        t.table.paragraph.section.mark_dirty()
    return f"Split {t.address} back into {t.info.row_span * t.info.col_span} cells."


def format_cells(store: Store, path: str, cells: str, **kw) -> str:
    with _Edit(store, path) as doc:
        return tables.format_cells(doc, resolve_cells(doc, cells), **kw)


def table_structure(store: Store, path: str, table: int, action: str, index: int | None, count: int) -> str:
    action = action.lower()
    with _Edit(store, path) as doc:
        if action == "delete_table":
            return tables.delete_table(doc, table)
        if index is None:
            raise ToolError(f"index is required for {action}.")
        if action in ("insert_row_above", "insert_row_below"):
            return tables.insert_rows(doc, table, index, count, above=action.endswith("above"))
        if action in ("insert_column_left", "insert_column_right"):
            return tables.insert_columns(doc, table, index, count, left=action.endswith("left"))
        if action == "delete_row":
            return tables.delete_rows(doc, table, index, count)
        if action == "delete_column":
            return tables.delete_columns(doc, table, index, count)
    raise ToolError(
        "action must be one of insert_row_above, insert_row_below, delete_row, insert_column_left, "
        "insert_column_right, delete_column, delete_table."
    )


def set_table_layout(store: Store, path: str, table: int, **kw) -> str:
    with _Edit(store, path) as doc:
        return tables.set_layout(doc, table, **kw)


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def _image_info(data: bytes) -> tuple[str, int | None, int | None]:
    """(format, width_px, height_px) from the file header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return "gif", w, h
    if data[:2] == b"BM":
        w, h = struct.unpack("<ii", data[18:26])
        return "bmp", w, abs(h)
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpg", w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        return "jpg", None, None
    raise ToolError("Unsupported image format. Use PNG, JPEG, GIF or BMP.")


def _picture_size(doc, data: bytes, width_mm, height_mm, max_width_hu: int) -> tuple[str, int, int]:
    fmt, wpx, hpx = _image_info(data)
    ratio = (hpx / wpx) if wpx and hpx else 0.75
    if width_mm is None and height_mm is None:
        w_hu = mm_to_hu((wpx or 400) * 25.4 / 96)
        w_hu = min(w_hu, max_width_hu)
        h_hu = int(w_hu * ratio)
    elif width_mm is not None and height_mm is not None:
        w_hu, h_hu = mm_to_hu(width_mm), mm_to_hu(height_mm)
    elif width_mm is not None:
        w_hu = mm_to_hu(width_mm)
        h_hu = int(w_hu * ratio)
    else:
        h_hu = mm_to_hu(height_mm)
        w_hu = int(h_hu / ratio)
    if w_hu <= 0 or h_hu <= 0:
        raise ToolError("Image size must be positive.")
    return fmt, w_hu, h_hu


def insert_image(store: Store, path: str, image_path: str, after: str | None, width_mm: float | None,
                 height_mm: float | None, align: str) -> str:
    src = Path(os.path.expanduser(image_path.strip().strip('"')))
    if not src.is_file():
        raise ToolError(f"Image file not found: {src}")
    data = src.read_bytes()
    with _Edit(store, path) as doc:
        anchor = resolve_one_paragraph(doc, after) if after and after.strip().lower() not in ("end", "start") else None
        holder, _ = _new_paragraph(doc, after, _format_source(doc, None, None, anchor))
        if anchor is not None and "." in anchor.address:
            cell_el = anchor.paragraph.element.getparent().getparent()
            max_w = int(cell_el.find(f"{HP}cellSz").get("width")) - 1200
        else:
            max_w = _text_width_hu(doc)
        fmt, w_hu, h_hu = _picture_size(doc, data, width_mm, height_mm, max_w)
        item_id = doc.add_image(data, fmt)
        holder.add_picture(item_id, width=w_hu, height=h_hu, treat_as_char=True)
        a = (align or "center").lower()
        if a not in ("left", "center", "right"):
            raise ToolError('align must be "left", "center" or "right".')
        formats.apply_para_format(doc, [holder], {"alignment": a})
        pics = all_pictures(doc)
        pic_el = next(el for el in holder.element.iter(PIC_TAG))
        gi = next(i for i, el in enumerate(pics) if el is pic_el)
        address = _address_of(doc, holder.element)
    return f"Inserted image g{gi} ({hu_to_mm(w_hu):g} x {hu_to_mm(h_hu):g} mm, {a}) in paragraph {address}."


def edit_image(store: Store, path: str, image: int, width_mm: float | None, height_mm: float | None, delete: bool) -> str:
    with _Edit(store, path) as doc:
        pics = all_pictures(doc)
        if not 0 <= image < len(pics):
            raise ToolError(f"Image g{image} does not exist; the document has {len(pics)} image(s). Call hwp_read_document.")
        pic = pics[image]
        run = pic.getparent()
        p_el = run.getparent()
        section = next(s for s in doc.sections if p_el in s.element.iter(HP + "p"))
        if delete:
            run.remove(pic)
            clear_layout_cache(p_el)
            section.mark_dirty()
            return f"Deleted image g{image}. Later images are renumbered."
        if width_mm is None and height_mm is None:
            raise ToolError("Pass width_mm and/or height_mm, or delete=true.")
        sz = pic.find(f"{HP}sz")
        old_w, old_h = int(sz.get("width")), int(sz.get("height"))
        ratio = old_h / old_w if old_w else 1
        w_hu = mm_to_hu(width_mm) if width_mm is not None else int(mm_to_hu(height_mm) / ratio)
        h_hu = mm_to_hu(height_mm) if height_mm is not None else int(w_hu * ratio)
        item = pic.find(f".//{formats.HC}img").get("binaryItemIDRef")
        # Build a fresh picture element of the new size and swap it in, keeping placement.
        scratch = section.add_paragraph("")
        try:
            scratch.add_picture(item, width=w_hu, height=h_hu, treat_as_char=True)
            new_pic = next(scratch.element.iter(PIC_TAG))
            old_pos, new_pos = pic.find(f"{HP}pos"), new_pic.find(f"{HP}pos")
            if old_pos is not None and new_pos is not None:
                new_pic.replace(new_pos, old_pos)
            for attr in ("textWrap", "textFlow", "zOrder"):
                if pic.get(attr) is not None:
                    new_pic.set(attr, pic.get(attr))
            run.replace(pic, new_pic)
        finally:
            scratch.element.getparent().remove(scratch.element)
        clear_layout_cache(p_el)
        section.mark_dirty()
    return f"Resized image g{image} to {hu_to_mm(w_hu):g} x {hu_to_mm(h_hu):g} mm."


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------
_PAPERS = {"a4": "A4", "a3": "A3", "a5": "A5", "b4": "B4", "b5": "B5", "letter": "LETTER", "legal": "LEGAL"}


def _apply_page_setup(doc: HwpxDocument, section: int, page: dict[str, Any]) -> None:
    kw: dict[str, Any] = {"section_index": section}
    if page.get("paper"):
        p = _PAPERS.get(str(page["paper"]).lower())
        if p is None:
            raise ToolError("paper must be one of: " + ", ".join(v for v in _PAPERS.values()) + ".")
        kw["paper_size"] = p
    if page.get("orientation"):
        o = str(page["orientation"]).lower()
        if o not in ("portrait", "landscape"):
            raise ToolError('orientation must be "portrait" or "landscape".')
        kw["orientation"] = o
    for key in ("margin_left_mm", "margin_right_mm", "margin_top_mm", "margin_bottom_mm",
                "header_margin_mm", "footer_margin_mm", "columns", "column_gap_mm"):
        if page.get(key) is not None:
            kw[key] = page[key]
    if not 0 <= section < len(doc.sections):
        raise ToolError(f"section must be 0-{len(doc.sections) - 1}.")
    doc.set_page_setup(**kw)


def page_setup(store: Store, path: str, section: int, page: dict[str, Any]) -> str:
    if not any(v is not None for v in page.values()):
        raise ToolError("Nothing to change.")
    with _Edit(store, path) as doc:
        _apply_page_setup(doc, section, page)
        info = _page_info(doc, section)
    return f"Section {section} page setup: {info}."


def header_footer(store: Store, path: str, kind: str, text: str | None, page_number: str | None,
                  align: str, section: int, remove: bool) -> str:
    kind = kind.lower()
    if kind not in ("header", "footer"):
        raise ToolError('kind must be "header" or "footer".')
    with _Edit(store, path) as doc:
        if not 0 <= section < len(doc.sections):
            raise ToolError(f"section must be 0-{len(doc.sections) - 1}.")
        if remove:
            (doc.remove_header if kind == "header" else doc.remove_footer)(section_index=section)
            return f"Removed the {kind} of section {section}."
        a = (align or "center").upper()
        if a not in ("LEFT", "CENTER", "RIGHT"):
            raise ToolError('align must be "left", "center" or "right".')
        if page_number:
            fmt = page_number.lower()
            if fmt not in ("number", "dash", "page_of_total"):
                raise ToolError('page_number must be "number" (1), "dash" (- 1 -) or "page_of_total" (1 / 10).')
            prefix, suffix = ("- ", " -") if fmt == "dash" else ("", "")
            if text:
                prefix = text + " " + prefix
            doc.set_page_number(target=kind, format="page_of_total" if fmt == "page_of_total" else "page",
                                align=a, position=f"{'TOP' if kind == 'header' else 'BOTTOM'}_{a}",
                                prefix=prefix, suffix=suffix, section_index=section)
            label = f'"{text}" + page number' if text else "page number"
            return f"Set the {kind} of section {section} to {label} ({fmt}), {a.lower()}-aligned."
        if not text:
            raise ToolError("Pass text and/or page_number, or remove=true.")
        hf = doc.set_header_footer(kind=kind, text=text, section_index=section)
        sec = doc.sections[section]
        paras = [HwpxOxmlParagraph(el, sec) for el in hf.element.iter(HP + "p")]
        formats.apply_para_format(doc, paras, {"alignment": a.lower()})
        return f'Set the {kind} of section {section} to "{text}", {a.lower()}-aligned.'
