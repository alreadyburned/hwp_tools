"""Character shapes, paragraph shapes, styles and border fills."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from lxml import etree as ET

from hwpx import HwpxDocument

from .model import HC, HH, clear_layout_cache, hu_to_mm, normalize_color, run_elements
from .store import ToolError

# ---------------------------------------------------------------------------
# character format
# ---------------------------------------------------------------------------
CHAR_OPTION_KEYS = (
    "font", "size_pt", "bold", "italic", "underline", "strikethrough", "color",
    "highlight", "superscript", "subscript", "char_width_percent", "letter_spacing_percent",
)


def char_kwargs(opts: dict[str, Any]) -> dict[str, Any]:
    """Translate tool options into python-hwpx ``ensure_run`` keyword arguments."""
    kw: dict[str, Any] = {}
    if opts.get("font") is not None:
        kw["font"] = opts["font"]
    if opts.get("size_pt") is not None:
        size = float(opts["size_pt"])
        if not 1 <= size <= 4096:
            raise ToolError("size_pt must be between 1 and 4096.")
        kw["size"] = size
    for src, dst in (("bold", "bold"), ("italic", "italic"), ("underline", "underline"), ("strikethrough", "strike")):
        if opts.get(src) is not None:
            kw[dst] = bool(opts[src])
    if opts.get("color") is not None:
        kw["color"] = normalize_color(opts["color"])
    if opts.get("highlight") is not None:
        kw["highlight"] = normalize_color(opts["highlight"], allow_none=True)
    sup, sub = opts.get("superscript"), opts.get("subscript")
    if sup and sub:
        raise ToolError("superscript and subscript cannot both be true.")
    if sup:
        kw["script"] = "sup"
    elif sub:
        kw["script"] = "sub"
    elif sup is False or sub is False:
        kw["script"] = "none"
    if opts.get("char_width_percent") is not None:
        ratio = int(opts["char_width_percent"])
        if not 50 <= ratio <= 200:
            raise ToolError("char_width_percent must be between 50 and 200.")
        kw["ratio"] = ratio
    if opts.get("letter_spacing_percent") is not None:
        spacing = int(opts["letter_spacing_percent"])
        if not -50 <= spacing <= 50:
            raise ToolError("letter_spacing_percent must be between -50 and 50.")
        kw["letter_spacing"] = spacing
    return kw


def ensure_char_pr(doc: HwpxDocument, base_id: str | None, kw: dict[str, Any]) -> str:
    return str(doc.styles.ensure_run(base_char_pr_id=base_id, **kw))


def apply_char_format(doc: HwpxDocument, runs: list, kw: dict[str, Any]) -> None:
    cache: dict[str | None, str] = {}
    for run in runs:
        base = run.get("charPrIDRef")
        if base not in cache:
            cache[base] = ensure_char_pr(doc, base, kw)
        run.set("charPrIDRef", cache[base])


def describe_char_pr(doc: HwpxDocument, char_pr_id: str | None) -> dict[str, Any]:
    rs = doc.styles.char_property(char_pr_id)
    if rs is None:
        return {}
    a, ch = rs.attributes, rs.child_attributes
    out: dict[str, Any] = {}
    font = doc.styles.font_face(char_pr_id, "HANGUL")
    if font:
        out["font"] = font
    latin = doc.styles.font_face(char_pr_id, "LATIN")
    if latin and latin != font:
        out["font_latin"] = latin
    if a.get("height"):
        out["size_pt"] = int(a["height"]) / 100
    if "bold" in ch:
        out["bold"] = True
    if "italic" in ch:
        out["italic"] = True
    if ch.get("underline", {}).get("type", "NONE") != "NONE":
        out["underline"] = True
    if ch.get("strikeout", {}).get("shape", "NONE") not in ("NONE", "3D"):
        out["strikethrough"] = True
    color = a.get("textColor")
    if color and color.upper() != "#000000":
        out["color"] = color
    shade = a.get("shadeColor")
    if shade and shade.lower() not in ("none", "#ffffff"):
        out["highlight"] = shade
    if "supscript" in ch:
        out["superscript"] = True
    if "subscript" in ch:
        out["subscript"] = True
    ratio = ch.get("ratio", {}).get("hangul")
    if ratio and ratio != "100":
        out["char_width_percent"] = int(ratio)
    spacing = ch.get("spacing", {}).get("hangul")
    if spacing and spacing != "0":
        out["letter_spacing_percent"] = int(spacing)
    return out


def short_char_desc(desc: dict[str, Any]) -> str:
    parts = []
    if "font" in desc:
        parts.append(desc["font"])
    if "size_pt" in desc:
        parts.append(f"{desc['size_pt']:g}pt")
    for key in ("bold", "italic", "underline", "strikethrough", "superscript", "subscript"):
        if desc.get(key):
            parts.append(key)
    if "color" in desc:
        parts.append(desc["color"])
    if "highlight" in desc:
        parts.append(f"highlight {desc['highlight']}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# paragraph format
# ---------------------------------------------------------------------------
_ALIGN_IN = {"left": "left", "center": "center", "right": "right", "justify": "justify", "distribute": "distribute"}
PARA_OPTION_KEYS = (
    "align", "line_spacing_percent", "space_before_pt", "space_after_pt", "indent_left_mm",
    "indent_right_mm", "first_line_indent_mm", "keep_with_next", "keep_lines", "page_break_before",
)


def para_kwargs(opts: dict[str, Any]) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if opts.get("align") is not None:
        align = str(opts["align"]).lower()
        if align not in _ALIGN_IN:
            raise ToolError('align must be one of "left", "center", "right", "justify", "distribute".')
        kw["alignment"] = _ALIGN_IN[align]
    if opts.get("line_spacing_percent") is not None:
        ls = float(opts["line_spacing_percent"])
        if not 50 <= ls <= 500:
            raise ToolError("line_spacing_percent must be between 50 and 500 (Hancom default is 160).")
        kw["line_spacing_percent"] = ls
    for src, dst in (
        ("space_before_pt", "spacing_before_pt"), ("space_after_pt", "spacing_after_pt"),
        ("indent_left_mm", "indent_left_mm"), ("indent_right_mm", "indent_right_mm"),
        ("first_line_indent_mm", "first_line_indent_mm"),
    ):
        if opts.get(src) is not None:
            kw[dst] = float(opts[src])
    for key in ("keep_with_next", "keep_lines", "page_break_before"):
        if opts.get(key) is not None:
            kw[key] = bool(opts[key])
    return kw


def apply_para_format(doc: HwpxDocument, paragraphs: list, kw: dict[str, Any]) -> None:
    doc.styles.apply_paragraph_format(paragraphs=paragraphs, **kw)
    for p in paragraphs:
        clear_layout_cache(p.element)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def describe_para_pr(doc: HwpxDocument, para_pr_id: str | None) -> dict[str, Any]:
    pp = doc.styles.paragraph_property(para_pr_id)
    if pp is None:
        return {}
    out: dict[str, Any] = {}
    if pp.align is not None and pp.align.horizontal:
        out["align"] = pp.align.horizontal.lower()
    ls = pp.line_spacing
    if ls is not None:
        if (ls.spacing_type or "").upper() == "PERCENT":
            out["line_spacing_percent"] = ls.value
        else:
            out["line_spacing"] = f"{ls.value} ({ls.spacing_type})"
    m = pp.margin
    if m is not None:
        out["space_before_pt"] = round(_num(m.prev) / 100, 1)
        out["space_after_pt"] = round(_num(m.next) / 100, 1)
        out["indent_left_mm"] = hu_to_mm(_num(m.left))
        out["indent_right_mm"] = hu_to_mm(_num(m.right))
        out["first_line_indent_mm"] = hu_to_mm(_num(m.intent))
    b = pp.break_setting
    if b is not None:
        for key in ("keep_with_next", "keep_lines", "page_break_before"):
            if getattr(b, key, False):
                out[key] = True
    h = pp.heading
    if h is not None and (h.type or "NONE") != "NONE":
        out["numbering"] = f"{h.type.lower()} level {int(h.level or 0) + 1}"
    return out


def short_para_desc(desc: dict[str, Any]) -> str:
    parts = []
    if desc.get("align"):
        parts.append(desc["align"])
    if desc.get("line_spacing_percent") is not None:
        parts.append(f"line {desc['line_spacing_percent']:g}%")
    if desc.get("first_line_indent_mm"):
        parts.append(f"first-line {desc['first_line_indent_mm']:g}mm")
    if desc.get("indent_left_mm"):
        parts.append(f"left {desc['indent_left_mm']:g}mm")
    if desc.get("space_before_pt"):
        parts.append(f"before {desc['space_before_pt']:g}pt")
    if desc.get("space_after_pt"):
        parts.append(f"after {desc['space_after_pt']:g}pt")
    if desc.get("numbering"):
        parts.append(desc["numbering"])
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# styles
# ---------------------------------------------------------------------------
def _header(doc: HwpxDocument):
    headers = doc.parts.headers
    if not headers:
        raise ToolError("The document has no header part (fonts/styles); it may be damaged.")
    return headers[0]


def _styles_element(doc: HwpxDocument):
    el = _header(doc).element.find(f".//{HH}styles")
    if el is None:
        raise ToolError("The document defines no styles.")
    return el


def list_styles(doc: HwpxDocument) -> list[dict[str, Any]]:
    out = []
    for st in _styles_element(doc).findall(f"{HH}style"):
        info = {
            "id": int(st.get("id")),
            "name": st.get("name"),
            "english_name": st.get("engName"),
            "type": "paragraph" if st.get("type", "PARA").upper() == "PARA" else "character",
        }
        para = describe_para_pr(doc, st.get("paraPrIDRef"))
        char = describe_char_pr(doc, st.get("charPrIDRef"))
        if para.get("numbering"):
            info["auto_numbering"] = para["numbering"]
        info["format"] = "; ".join(x for x in (short_char_desc(char), short_para_desc(para)) if x)
        out.append(info)
    return out


def resolve_style(doc: HwpxDocument, spec: str | int):
    """Return the ``hh:style`` element for a style name, English name or id."""
    styles = _styles_element(doc).findall(f"{HH}style")
    key = str(spec).strip()
    for st in styles:
        if st.get("id") == key:
            return st
    low = key.lower()
    for st in styles:
        if (st.get("name") or "").lower() == low or (st.get("engName") or "").lower() == low:
            return st
    for st in styles:  # tolerate missing spaces, e.g. "개요1"
        if (st.get("name") or "").replace(" ", "").lower() == low.replace(" ", ""):
            return st
    names = ", ".join(f'"{s.get("name")}"' for s in styles[:25])
    raise ToolError(f'Style "{spec}" not found. Available styles: {names}. (hwp_list_styles shows all.)')


def apply_style(doc: HwpxDocument, paragraphs: list, style_el, keep_char_format: bool = False) -> None:
    sid, para_pr, char_pr = style_el.get("id"), style_el.get("paraPrIDRef"), style_el.get("charPrIDRef")
    char_style = style_el.get("type", "PARA").upper() == "CHAR"
    for p in paragraphs:
        el = p.element
        if not char_style:
            el.set("styleIDRef", sid)
            if para_pr is not None:
                el.set("paraPrIDRef", para_pr)
        if (char_style or not keep_char_format) and char_pr is not None:
            for run in run_elements(el):
                run.set("charPrIDRef", char_pr)
        clear_layout_cache(el)
        p.section.mark_dirty()


def create_style(doc: HwpxDocument, name: str, base: str | int | None, char_kw: dict, para_kw: dict) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToolError("Style name is empty.")
    styles_el = _styles_element(doc)
    for st in styles_el.findall(f"{HH}style"):
        if st.get("name") == name:
            raise ToolError(f'A style named "{name}" already exists (id {st.get("id")}). Choose another name.')
    base_el = resolve_style(doc, base if base is not None else 0)
    if base_el.get("type", "PARA").upper() != "PARA":
        raise ToolError("base_style must be a paragraph style.")
    char_pr = base_el.get("charPrIDRef")
    if char_kw:
        char_pr = ensure_char_pr(doc, char_pr, char_kw)
    para_pr = base_el.get("paraPrIDRef")
    if para_kw:
        # Derive the paragraph shape through a scratch paragraph so python-hwpx
        # writes the paraPr exactly as it does for real paragraphs.
        section = doc.sections[0]
        scratch = section.add_paragraph("", para_pr_id_ref=para_pr, style_id_ref=base_el.get("id"))
        try:
            doc.styles.apply_paragraph_format(paragraphs=[scratch], **para_kw)
            para_pr = scratch.element.get("paraPrIDRef")
        finally:
            scratch.element.getparent().remove(scratch.element)
            section.mark_dirty()
    new_id = str(max(int(s.get("id")) for s in styles_el.findall(f"{HH}style")) + 1)
    new_el = deepcopy(base_el)
    new_el.set("id", new_id)
    new_el.set("name", name)
    new_el.set("engName", name)
    new_el.set("nextStyleIDRef", new_id)
    new_el.set("paraPrIDRef", str(para_pr))
    new_el.set("charPrIDRef", str(char_pr))
    styles_el.append(new_el)
    styles_el.set("itemCnt", str(len(styles_el.findall(f"{HH}style"))))
    _header(doc).mark_dirty()
    return {"id": int(new_id), "name": name}


# ---------------------------------------------------------------------------
# border fills (cell borders and shading)
# ---------------------------------------------------------------------------
_BORDER_WIDTHS = (0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
BORDER_TYPES = {
    "none": "NONE", "solid": "SOLID", "dash": "DASH", "dot": "DOT", "dash_dot": "DASH_DOT",
    "long_dash": "LONG_DASH", "double": "DOUBLE_SLIM", "thick_thin": "THICK_SLIM", "thin_thick": "SLIM_THICK",
}
SIDES = ("left", "right", "top", "bottom")


def border_width_str(mm: float) -> str:
    best = min(_BORDER_WIDTHS, key=lambda w: abs(w - mm))
    return f"{best:g} mm" if best not in (1.0, 2.0, 3.0, 4.0, 5.0) else f"{best:.1f} mm"


def _border_fills_element(doc: HwpxDocument):
    el = _header(doc).element.find(f".//{HH}borderFills")
    if el is None:
        raise ToolError("The document has no border-fill list.")
    return el


def _find_border_fill(doc: HwpxDocument, bf_id: str | None):
    for bf in _border_fills_element(doc).findall(f"{HH}borderFill"):
        if bf.get("id") == str(bf_id):
            return bf
    return None


def _canonical(el) -> bytes:
    clone = deepcopy(el)
    clone.attrib.pop("id", None)
    return ET.tostring(clone, method="c14n")


def derive_border_fill(
    doc: HwpxDocument,
    base_id: str | None,
    *,
    sides: dict[str, tuple[str, str, str]] | None = None,
    fill: str | None = None,
) -> str:
    """Clone a borderFill, change sides/fill, and return the id of an equal or new entry.

    ``sides`` maps "left"/"right"/"top"/"bottom" to (TYPE, "0.12 mm", "#000000").
    ``fill`` is "#RRGGBB" or "none".
    """
    container = _border_fills_element(doc)
    base = _find_border_fill(doc, base_id)
    if base is None:
        base = _find_border_fill(doc, "1") or container.findall(f"{HH}borderFill")[0]
    new = deepcopy(base)
    for side, (btype, width, color) in (sides or {}).items():
        el = new.find(f"{HH}{side}Border")
        if el is None:
            el = ET.SubElement(new, f"{HH}{side}Border")
        el.set("type", btype)
        el.set("width", width)
        el.set("color", color)
    if fill is not None:
        brush = new.find(f"{HC}fillBrush")
        if fill == "none":
            if brush is not None:
                new.remove(brush)
        else:
            if brush is None:
                brush = ET.SubElement(new, f"{HC}fillBrush")
            for child in list(brush):
                brush.remove(child)
            ET.SubElement(brush, f"{HC}winBrush", {"faceColor": fill, "hatchColor": "#999999", "alpha": "0"})
    key = _canonical(new)
    existing = container.findall(f"{HH}borderFill")
    for bf in existing:
        if _canonical(bf) == key:
            return bf.get("id")
    new_id = str(max(int(bf.get("id")) for bf in existing) + 1)
    new.set("id", new_id)
    container.append(new)
    container.set("itemCnt", str(len(existing) + 1))
    _header(doc).mark_dirty()
    return new_id


def describe_border_fill(doc: HwpxDocument, bf_id: str | None) -> dict[str, Any]:
    bf = _find_border_fill(doc, bf_id)
    if bf is None:
        return {}
    out: dict[str, Any] = {}
    for side in SIDES:
        el = bf.find(f"{HH}{side}Border")
        if el is not None and el.get("type", "NONE") != "NONE":
            out[f"border_{side}"] = f'{el.get("type").lower()} {el.get("width")} {el.get("color")}'
    brush = bf.find(f"{HC}fillBrush/{HC}winBrush")
    if brush is not None and brush.get("faceColor", "none").lower() != "none":
        out["fill_color"] = brush.get("faceColor")
    return out
