"""Editable viewer for the VS Code extension: the preview HTML with paragraph addresses, and the
edits the viewer makes (paste from another document or as text, page margins).

    python -m hwp_mcp.viewer render <file>        HTML on stdout
    python -m hwp_mcp.viewer apply < op.json      apply one edit, JSON result on stdout

Edits go through the same validated, atomic save as the MCP tools, so a failed edit leaves the
file untouched.
"""

from __future__ import annotations

import json
import os
import re
import sys
import warnings
from typing import Any

from hwpx import HwpxDocument

from . import ops, preview
from .model import body_paragraphs, hu_to_mm
from .store import Store, ToolError, normalize_path

MARGIN_KEYS = {"left": "margin_left_mm", "right": "margin_right_mm", "top": "margin_top_mm",
               "bottom": "margin_bottom_mm", "header": "header_margin_mm", "footer": "footer_margin_mm"}

# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
_TAG = re.compile(r"<(/?)(section|td|p|table)\b([^>]*)>", re.IGNORECASE)


def annotate(html: str, paragraphs: int) -> tuple[str, bool]:
    """Add data-addr="p<N>" to each body paragraph (and its tables) of the viewer HTML.

    Body paragraphs are the <p class="hwpx-paragraph"> directly inside a page <section>, in
    document order, one per paragraph; a table carries the address of the paragraph it is
    anchored in (the <p> just before it). Returns (html, editable); editable is False when the
    count does not match the document, and then nothing is annotated."""
    out, pos = [], 0
    in_page = td = p_depth = 0
    count = 0
    current = None
    for m in _TAG.finditer(html):
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name == "section" and "hwpx-preview-page" in attrs and not closing:
            in_page += 1
        elif name == "section" and closing and in_page:
            in_page -= 1
        elif name == "td":
            td += -1 if closing else 1
        elif name == "p":
            if closing:
                p_depth = max(0, p_depth - 1)
                continue
            if in_page and td == 0 and p_depth == 0 and "hwpx-paragraph" in attrs:
                current = f"p{count}"
                count += 1
                out.append(html[pos:m.end(2)] + f' data-addr="{current}"')
                pos = m.end(2)
            if not attrs.rstrip().endswith("/"):
                p_depth += 1
        elif name == "table" and not closing and in_page and td == 0 and current and "hwpx-table" in attrs:
            out.append(html[pos:m.end(2)] + f' data-addr="{current}"')
            pos = m.end(2)
    if count != paragraphs:
        return html, False
    out.append(html[pos:])
    return "".join(out), True


def section_info(doc: HwpxDocument) -> list[dict[str, Any]]:
    info = []
    for n, section in enumerate(doc.sections):
        props = section.properties
        size, m = props.page_size, props.page_margins
        w, h = hu_to_mm(size.width), hu_to_mm(size.height)
        if (size.orientation or "").upper() == "NARROWLY":
            w, h = h, w
        info.append({
            "section": n, "width_mm": w, "height_mm": h,
            "margins": {"left": hu_to_mm(m.left), "right": hu_to_mm(m.right), "top": hu_to_mm(m.top),
                        "bottom": hu_to_mm(m.bottom), "header": hu_to_mm(m.header), "footer": hu_to_mm(m.footer)},
        })
    return info


def render(path: str) -> str:
    path = normalize_path(path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        doc = HwpxDocument.open(path)
        html = preview.render(path)
    html, editable = annotate(html, len(body_paragraphs(doc)))
    info = {"path": path, "editable": editable, "paragraphs": len(body_paragraphs(doc)),
            "sections": section_info(doc), "hwp": path.lower().endswith(".hwp")}
    data = json.dumps(info, ensure_ascii=False).replace("</", "<\\/")
    tag = f'<script type="application/json" id="hwp-doc-info">{data}</script>'
    return html.replace("</body>", tag + "</body>") if "</body>" in html else html + tag


# ---------------------------------------------------------------------------
# edits
# ---------------------------------------------------------------------------
def _body_index(address: str) -> int:
    m = re.fullmatch(r"p(\d+)", (address or "").strip().lower())
    if not m:
        raise ToolError(f'"{address}" is not a body paragraph address (p<N>).')
    return int(m.group(1))


def _section_position(doc: HwpxDocument, index: int) -> tuple[int, int]:
    """(section index, paragraph index inside that section) of body paragraph ``index``."""
    for s, section in enumerate(doc.sections):
        n = len(section.paragraphs)
        if index < n:
            return s, index
        index -= n
    raise ToolError("paragraph out of range")


def paste_paragraphs(store: Store, path: str, after: str | None, source: str, addresses: list[str]) -> dict:
    """Copy body paragraphs (with their tables, images and formatting) from another document."""
    from hwpx.tools.document_merge import insert_document

    source = normalize_path(source)
    if not os.path.exists(source):
        raise ToolError(f"The copied document no longer exists: {source}")
    keep = sorted({_body_index(a) for a in addresses})
    if not keep:
        raise ToolError("Nothing was copied.")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        src = HwpxDocument.open(source)
    body = body_paragraphs(src)
    if keep[-1] >= len(body):
        raise ToolError("The copied paragraphs changed in the source document; copy them again.")
    for i, p in reversed(list(enumerate(body))):  # leave only the copied paragraphs
        if i not in keep:
            p.element.getparent().remove(p.element)
    for section in src.sections:
        section.mark_dirty()
    with ops._Edit(store, path) as doc:
        count = len(body_paragraphs(doc))
        if after is None:
            s, j = len(doc.sections) - 1, len(doc.sections[-1].paragraphs) - 1
            first = count
        else:
            k = _body_index(after)
            if k >= count:
                raise ToolError(f"{after} does not exist; the document has {count} paragraphs.")
            s, j = _section_position(doc, k)
            first = k + 1
        try:
            insert_document(doc, src, after_paragraph_index=j, target_section_index=s)
        except Exception as exc:  # noqa: BLE001 - unsupported content in the copy
            raise ToolError(f"Cannot paste with formatting: {exc}") from exc
    n = len(keep)
    return {"inserted": [f"p{i}" for i in range(first, first + n)],
            "message": f"붙여넣었습니다: 문단 {n}개 (서식 유지)"}


def paste_text(store: Store, path: str, after: str | None, text: str) -> dict:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not text.strip():
        raise ToolError("The clipboard has no text to paste.")
    path = normalize_path(path)
    ops.insert_paragraph(store, path, text, after, None, after)  # like=after: take its look
    lines = text.count("\n") + 1
    total = len(body_paragraphs(store.open(path)))
    # at the end: the last lines (a blank document's empty first paragraph is filled, not kept)
    first = _body_index(after) + 1 if after else total - lines
    return {"inserted": [f"p{i}" for i in range(first, first + lines)],
            "message": f"붙여넣었습니다: 문단 {lines}개 (텍스트)"}


def set_margins(store: Store, path: str, section: int | None, margins: dict[str, float]) -> dict:
    page = {}
    for key, value in margins.items():
        if key not in MARGIN_KEYS:
            raise ToolError(f"Unknown margin {key!r}.")
        if value is None:
            continue
        value = float(value)
        if not 0 <= value <= 150:
            raise ToolError(f"The {key} margin must be 0-150 mm.")
        page[MARGIN_KEYS[key]] = value
    if not page:
        raise ToolError("No margin given.")
    with ops._Edit(store, path) as doc:
        targets = range(len(doc.sections)) if section is None else [section]
        for s in targets:
            if not 0 <= s < len(doc.sections):
                raise ToolError(f"Section {s} does not exist.")
            info = section_info(doc)[s]
            m = {**info["margins"], **{k: float(v) for k, v in margins.items() if v is not None}}
            if m["left"] + m["right"] > info["width_mm"] - 20:
                raise ToolError("Left + right margins leave less than 20 mm of text width.")
            if m["top"] + m["bottom"] + m["header"] + m["footer"] > info["height_mm"] - 20:
                raise ToolError("Top, bottom, header and footer margins leave less than 20 mm of text height.")
            ops._apply_page_setup(doc, s, page)
    where = "모든 구역" if section is None else f"구역 {section + 1}"
    return {"inserted": [], "message": f"여백을 바꿨습니다 ({where})"}


def apply(op: dict[str, Any]) -> dict[str, Any]:
    store = Store()
    path = op.get("path") or ""
    kind = op.get("op")
    if kind == "paste_paragraphs":
        return paste_paragraphs(store, path, op.get("after"), op.get("source") or "", op.get("addresses") or [])
    if kind == "paste_text":
        return paste_text(store, path, op.get("after"), op.get("text") or "")
    if kind == "margins":
        return set_margins(store, path, op.get("section"), op.get("margins") or {})
    raise ToolError(f"Unknown viewer operation {kind!r}.")


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) == 3 and argv[1] == "render":
        sys.stdout.write(render(argv[2]))
        return 0
    if len(argv) == 2 and argv[1] == "apply":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = {"ok": True, **apply(json.loads(sys.stdin.buffer.read().decode("utf-8")))}
        except ToolError as exc:
            result = {"ok": False, "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
