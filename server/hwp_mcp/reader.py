"""Reading documents that do not fit in one tool result: section outline, budgeted range reads,
ranked search and change reports (diff).

A 100-page document is far larger than one tool result, and a small local model cannot hold even
a tenth of it, so the model navigates instead of reading everything:

    hwp_outline -> hwp_read_document(range="s2.1") / hwp_search -> edit -> hwp_diff

Nothing is cached: outline, search index and diff are rebuilt from the parsed document on every
call, so the addresses they print (p12, t0.r1.c2, s2.1) are always current.
"""

from __future__ import annotations

import difflib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hwpx import HwpxDocument

from . import embed, formats
from .model import (
    T_TAG, all_pictures, body_paragraphs, first_char_pr, get_table, hu_to_mm, paragraph_text,
    run_elements, run_text, t_text, table_cells, top_tables,
)
from .ops import (
    HP, _OBJECT_NAMES, _clip, _local, _page_info, _style_name, paragraph_line, table_rows,
)
from .store import Store, ToolError, normalize_path

READ_BUDGET = 20000     # characters per hwp_read_document result
DIFF_BUDGET = 8000      # characters per hwp_diff result
OUTLINE_MAX_LINES = 150  # hwp_outline picks the deepest level that fits in this many lines
PART_CHARS = 4000       # size of the fixed parts used when a document has no headings


# ---------------------------------------------------------------------------
# document map
# ---------------------------------------------------------------------------
@dataclass
class Section:
    sid: str            # "s2.1"
    level: int          # 1 = top level
    title: str
    start: int          # body paragraph index of the heading (or first paragraph)
    end: int = -1       # last body paragraph index, inclusive (children included)
    children: list["Section"] = field(default_factory=list)


@dataclass
class DocMap:
    body: list                      # body paragraphs
    texts: list[str]                # body paragraph texts
    tables_at: dict[int, list[int]]  # body index -> table indexes anchored there
    chars: list[int]                # characters per body paragraph, its tables included
    images: list[int]               # images per body paragraph
    sections: list[Section]         # top-level sections, in order
    method: str                     # how headings were found

    def all_sections(self) -> list[Section]:
        out: list[Section] = []

        def walk(secs):
            for s in secs:
                out.append(s)
                walk(s.children)
        walk(self.sections)
        return out

    def find(self, sid: str) -> Section:
        for s in self.all_sections():
            if s.sid == sid:
                return s
        raise ToolError(f'Section "{sid}" does not exist. Call hwp_outline to see section ids (s1, s2.1, ...).')

    def path_of(self, index: int) -> list[Section]:
        """Sections containing body paragraph ``index``, outermost first."""
        path, secs = [], self.sections
        while True:
            hit = next((s for s in secs if s.start <= index <= s.end), None)
            if hit is None:
                return path
            path.append(hit)
            secs = hit.children


_GA = "가나다라마바사아자차카타파하"
# Numbering patterns that mark headings, in the conventional Korean hierarchy (행정업무운영 편람:
# 1. -> 가. -> 1) -> 가) -> (1) -> (가)); a document's levels are the patterns it uses, in this order.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("제N편/장", re.compile(r"^제\s*\d+\s*[편장부](?:\s|$)")),
    ("제N절", re.compile(r"^제\s*\d+\s*절(?:\s|$)")),
    ("제N조", re.compile(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*[(\s]")),
    ("Ⅰ.", re.compile(r"^[Ⅰ-Ⅿ]+\s*[.)]?\s*\S")),
    ("I.", re.compile(r"^(?:I|II|III|IV|V|VI|VII|VIII|IX|X)\.\s")),
    ("1.", re.compile(r"^\d{1,2}\.\s*[^\d\s.]")),
    ("1.1", re.compile(r"^\d{1,2}\.\d{1,2}\.?\s+\S")),
    ("1.1.1", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,2}\.?\s+\S")),
    ("□", re.compile(r"^[□■]\s*\S")),
    ("가.", re.compile(rf"^[{_GA}]\.\s*\S")),
    ("1)", re.compile(r"^\d{1,2}\)\s*\S")),
    ("가)", re.compile(rf"^[{_GA}]\)\s*\S")),
    ("(1)", re.compile(r"^\(\d{1,2}\)\s*\S")),
    ("(가)", re.compile(rf"^\([{_GA}]\)\s*\S")),
]
_LONG_OK = {"제N편/장", "제N절", "제N조"}  # these stay headings even when the line is long
_SENTENCE_END = re.compile(r"(?:다|요|음|함|임|됨|것)\s*[.。]?\s*$")
_OUTLINE_STYLE = re.compile(r"^(?:개요|제목|heading)\s*(\d+)$", re.IGNORECASE)
_MAX_LEVELS = 4


def _looks_like_heading(text: str) -> bool:
    return len(text) <= 60 and not (len(text) > 15 and _SENTENCE_END.search(text))


def _explicit_headings(doc: HwpxDocument, body, texts) -> dict[int, int]:
    """Paragraphs with an outline level (개요 styles / heading type OUTLINE)."""
    cache: dict[Any, int | None] = {}
    out = {}
    for i, p in enumerate(body):
        if not texts[i].strip():
            continue
        el = p.element
        pid = el.get("paraPrIDRef")
        if pid not in cache:
            pp = doc.styles.paragraph_property(pid)
            h = pp.heading if pp is not None else None
            cache[pid] = int(h.level or 0) + 1 if h is not None and (h.type or "").upper() == "OUTLINE" else None
        level = cache[pid]
        if level is None:
            m = _OUTLINE_STYLE.match(_style_name(doc, el.get("styleIDRef")).strip())
            level = int(m.group(1)) if m else None
        if level is not None:
            out[i] = level
    return out


def _pattern_headings(texts) -> dict[int, int]:
    found: dict[int, str] = {}
    for i, raw in enumerate(texts):
        text = raw.strip()
        for key, rx in _PATTERNS:
            if rx.match(text):
                if key in _LONG_OK or _looks_like_heading(text):
                    found[i] = key
                break
    if len(found) < 2:
        return {}
    order = [k for k, _ in _PATTERNS if k in set(found.values())][:_MAX_LEVELS]
    return {i: order.index(k) + 1 for i, k in found.items() if k in order}


def _format_headings(doc: HwpxDocument, body, texts) -> dict[int, int]:
    """Short paragraphs set larger (or bold) than the body text."""
    cache: dict[Any, tuple[float, bool]] = {}
    info = []
    weight: Counter = Counter()
    for i, p in enumerate(body):
        text = texts[i].strip()
        if not text:
            continue
        cp = first_char_pr(p.element)
        if cp not in cache:
            d = formats.describe_char_pr(doc, cp)
            cache[cp] = (float(d.get("size_pt", 10)), bool(d.get("bold")))
        size, bold = cache[cp]
        weight[size] += len(text)
        info.append((i, text, size, bold))
    if not weight:
        return {}
    body_size = weight.most_common(1)[0][0]
    keys: dict[int, tuple[float, bool]] = {}
    for i, text, size, bold in info:
        if len(text) <= 50 and not _SENTENCE_END.search(text) and (size >= body_size + 1.5 or (bold and size >= body_size)):
            keys[i] = (size, bold)
    counts = Counter(keys.values())
    nonempty = len(info)
    first_rows = [i for i, *_ in info[:3]]
    for i in list(keys):
        k = keys[i]
        if counts[k] > max(3, nonempty // 4):  # too common to be a heading
            del keys[i]
        elif counts[k] == 1 and i in first_rows and len(keys) > 2:  # the document title
            del keys[i]
    if len(keys) < 2:
        return {}
    order = sorted(set(keys.values()), key=lambda k: (-k[0], not k[1]))[:_MAX_LEVELS]
    return {i: order.index(k) + 1 for i, k in keys.items() if k in order}


def _build_tree(headings: dict[int, int], texts, n: int) -> list[Section]:
    top: list[Section] = []
    stack: list[Section] = []
    first = min(headings) if headings else n
    if first > 0:
        top.append(Section("s0", 1, "(start of document)", 0, first - 1))
    counter = 0
    for i in sorted(headings):
        level = headings[i]
        while stack and stack[-1].level >= level:
            stack.pop().end = i - 1
        if stack:
            parent = stack[-1]
            sec = Section(f"{parent.sid}.{len(parent.children) + 1}", level, texts[i].strip(), i)
            parent.children.append(sec)
        else:
            counter += 1
            sec = Section(f"s{counter}", level, texts[i].strip(), i)
            top.append(sec)
        stack.append(sec)
    for sec in stack:
        sec.end = n - 1
    return top


def _fixed_parts(texts, chars, n: int) -> list[Section]:
    parts: list[Section] = []
    start, size = 0, 0
    for i in range(n):
        size += chars[i]
        if size >= PART_CHARS or i == n - 1:
            title = next((t.strip() for t in texts[start:i + 1] if t.strip()), "(no text)")
            parts.append(Section(f"s{len(parts) + 1}", 1, title, start, i))
            start, size = i + 1, 0
    return parts


def build_map(doc: HwpxDocument) -> DocMap:
    body = body_paragraphs(doc)
    texts = [paragraph_text(p.element) for p in body]
    n = len(body)
    tables_at: dict[int, list[int]] = {}
    chars = [len(t) for t in texts]
    for ti, (tbl, pi) in enumerate(top_tables(doc)):
        tables_at.setdefault(pi, []).append(ti)
        chars[pi] += sum(len(ci.cell.text) for ci in table_cells(tbl))
    images = [sum(1 for _ in p.element.iter(HP + "pic")) for p in body]

    headings = _explicit_headings(doc, body, texts)  # explicit markup: one heading is enough
    method = "outline levels (개요 styles)"
    if not headings:
        headings = _pattern_headings(texts)
        method = "guessed from numbering (1., 가., □, ...)"
    if not headings:
        headings = _format_headings(doc, body, texts)
        method = "guessed from font size / bold"
    if headings:
        sections = _build_tree(headings, texts, n)
        front = sections[0]
        if front.sid == "s0" and not any(chars[i] or images[i] for i in range(front.start, front.end + 1)):
            sections.pop(0)  # only blank paragraphs before the first heading
    else:
        sections = _fixed_parts(texts, chars, n)
        method = f"none found; fixed parts of about {PART_CHARS} characters"
    return DocMap(body, texts, tables_at, chars, images, sections, method)


def _stats(m: DocMap, sec: Section) -> str:
    rng = range(sec.start, sec.end + 1)
    total = sum(m.chars[i] for i in rng)
    ntab = sum(len(m.tables_at.get(i, [])) for i in rng)
    nimg = sum(m.images[i] for i in rng)
    parts = [f"{len(rng)} para"]
    if ntab:
        parts.append(f"{ntab} table")
    if nimg:
        parts.append(f"{nimg} image")
    parts.append(f"{_kchars(total)} chars")
    return ", ".join(parts)


def _kchars(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _span(sec: Section) -> str:
    return f"p{sec.start}" if sec.start == sec.end else f"p{sec.start}-p{sec.end}"


# ---------------------------------------------------------------------------
# hwp_outline
# ---------------------------------------------------------------------------
def outline(store: Store, path: str, section: str | None = None, depth: int | None = None,
            max_lines: int = OUTLINE_MAX_LINES) -> str:
    path = normalize_path(path)
    doc = store.open(path)
    m = build_map(doc)
    _warm(doc, m)
    roots = m.sections
    if section:
        roots = [m.find(section.strip().lower())]
    lines_by_depth: dict[int, int] = Counter()

    def count(secs, d):
        for s in secs:
            lines_by_depth[d] += 1
            count(s.children, d + 1)
    count(roots, 1)
    if depth is None:
        depth, total = 1, 0
        for d in sorted(lines_by_depth):
            total += lines_by_depth[d]
            if total > max_lines and d > 1:
                break
            depth = d
    total_chars = sum(m.chars)
    head = [
        f"{os.path.basename(path)}: {len(m.body)} paragraphs (p0-p{len(m.body) - 1}), "
        f"{sum(len(v) for v in m.tables_at.values())} table(s), {sum(m.images)} image(s), "
        f"about {_kchars(total_chars)} characters. Headings: {m.method}.",
        'Read a section with hwp_read_document(range="s2.1"), or find text with hwp_search.',
    ]
    lines: list[str] = []
    hidden = 0

    def emit(secs, d):
        nonlocal hidden
        for s in secs:
            if d > depth:
                hidden += 1 + len(_descendants(s))
                continue
            lines.append(f"{'  ' * (d - 1)}{s.sid}  {_span(s)}  {_clip(s.title, 60)}  ({_stats(m, s)})")
            emit(s.children, d + 1)
    emit(roots, 1)
    if hidden:
        lines.append(f"({hidden} deeper heading(s) hidden; pass depth={depth + 1} or section=<id> to see them.)")
    return "\n".join(head + lines)


def _descendants(sec: Section) -> list[Section]:
    out = []
    for c in sec.children:
        out.append(c)
        out.extend(_descendants(c))
    return out


# ---------------------------------------------------------------------------
# hwp_read_document
# ---------------------------------------------------------------------------
_P_RANGE = re.compile(r"^p(\d+)(?:-(?:p?(\d+))?)?$")
_T_RANGE = re.compile(r"^t(\d+)(?:\.r(\d+)(?:-(?:r?(\d+))?)?)?$")
_S_RANGE = re.compile(r"^s\d+(?:\.\d+)*$")


def read_document(store: Store, path: str, range_: str | None = None, max_chars: int = 600,
                  show_format: bool = False, budget: int = READ_BUDGET) -> str:
    """List paragraphs (and tables) of the whole document or of one range, within ``budget``
    characters. Ranges: "s2.1" (a section from hwp_outline), "p10-p40", "p10-" (to the end),
    "p10" (one paragraph), "t3" (a whole table), "t3.r20-" (table rows from 20)."""
    path = normalize_path(path)
    doc = store.open(path)
    m = build_map(doc)
    _warm(doc, m)
    n = len(m.body)
    spec = (range_ or "").strip().lower().replace(" ", "")
    pics = all_pictures(doc)
    pic_index = {id(el): i for i, el in enumerate(pics)}
    ntab = sum(len(v) for v in m.tables_at.values())
    lines = [f"{os.path.basename(path)}: {len(doc.sections)} section(s), {n} paragraphs"
             + (f" (p0-p{n - 1})" if n else "") + f", {ntab} table(s), {len(pics)} image(s)"]
    lost = store.conversion_warnings(doc)
    if lost:
        lines.append("WARNING (.hwp conversion): " + "; ".join(lost))

    tm = _T_RANGE.match(spec)
    if tm:
        return "\n".join(lines + _read_table(doc, m, int(tm.group(1)), tm, max_chars, budget))

    if not spec:
        start, end = 0, n - 1
        lines.append(f"Page: {_page_info(doc)}")
        if sum(m.chars) > budget:
            lines.append("Large document: this shows the beginning only. Call hwp_outline for the section map "
                         "and read one section at a time, or use hwp_search.")
    elif _S_RANGE.match(spec):
        sec = m.find(spec)
        start, end = sec.start, sec.end
    else:
        pm = _P_RANGE.match(spec)
        if not pm:
            raise ToolError(f'Invalid range "{range_}". Use a section id from hwp_outline ("s2", "s2.1"), '
                            'paragraphs ("p10-p40", "p10-" to the end, "p10") or a table ("t3", "t3.r20-").')
        start = int(pm.group(1))
        if pm.group(2):
            end = int(pm.group(2))
        elif spec.endswith("-"):
            end = n - 1
        else:
            end = start
        if start > end:
            start, end = end, start
        if start >= n:
            raise ToolError(f"p{start} is out of range: the document has {n} paragraphs (p0-p{n - 1}).")
        end = min(end, n - 1)
    where = m.path_of(start)
    if where and spec:
        lines.append("In: " + " > ".join(f"{s.sid} {_clip(s.title, 30)}" for s in where))
    lines.append(f"--- p{start}-p{end} ---")
    used = sum(len(x) + 1 for x in lines)
    for i in range(start, end + 1):
        chunk = [paragraph_line(doc, m.body[i].element, f"p{i}", pic_index, max_chars, show_format)]
        for ti in m.tables_at.get(i, []):
            rows = table_rows(doc, ti, min(max_chars, 80))
            if len(rows) > 30:
                rows = rows[:30] + [f'... {len(rows) - 30} more row(s): hwp_read_document(range="t{ti}.r30-")']
            chunk.extend("    " + r for r in rows)
        size = sum(len(x) + 1 for x in chunk)
        if used + size > budget and i > start:
            lines.append(f'... stopped at the size limit. Continue with range="p{i}-p{end}".')
            break
        lines.extend(chunk)
        used += size
    return "\n".join(lines)


def _read_table(doc, m: DocMap, ti: int, tm, max_chars: int, budget: int) -> list[str]:
    table = get_table(doc, ti)
    pi = next(p for p, tis in m.tables_at.items() if ti in tis)
    first = int(tm.group(2) or 0)
    last = int(tm.group(3)) if tm.group(3) else table.row_count - 1
    if tm.group(2) and not tm.group(3) and not tm.string.endswith("-"):
        last = first
    if not 0 <= first <= last < table.row_count:
        raise ToolError(f"t{ti} has rows r0-r{table.row_count - 1}; the requested rows are out of range.")
    lines = [f"Table t{ti}: {table.row_count} rows x {table.column_count} cols, in paragraph p{pi}",
             f"--- t{ti} rows {first}-{last} ---"]
    used = 0
    for r in range(first, last + 1):
        for row in table_rows(doc, ti, max_chars, range(r, r + 1)):
            if used + len(row) > budget and r > first:
                lines.append(f'... stopped at the size limit. Continue with range="t{ti}.r{r}-".')
                return lines
            lines.append(row)
            used += len(row) + 1
    return lines


# ---------------------------------------------------------------------------
# hwp_search
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[가-힣]+|[0-9a-z]+(?:[.,][0-9]+)*")


def tokens(text: str) -> list[str]:
    """Hangul as character bigrams (so particles and compounds still match), other words whole."""
    out: list[str] = []
    for mt in _TOKEN_RE.finditer(text.lower()):
        w = mt.group()
        if "가" <= w[0] <= "힣":
            out.extend([w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)])
        else:
            out.append(w)
    return out


@dataclass
class _Unit:
    address: str
    index: int      # body paragraph index (for the section path)
    text: str       # searchable text
    display: str    # shown for a hit (table rows keep their cell labels)


def _units(doc: HwpxDocument, m: DocMap) -> list[_Unit]:
    units = []
    for i, text in enumerate(m.texts):
        if text.strip():
            units.append(_Unit(f"p{i}", i, text, text))
        for ti in m.tables_at.get(i, []):
            by_row: dict[int, list] = {}
            for ci in table_cells(get_table(doc, ti)):
                by_row.setdefault(ci.row, []).append(ci)
            for r, cells in sorted(by_row.items()):
                text = " | ".join(ci.cell.text for ci in cells)
                if text.replace("|", "").strip():
                    shown = " | ".join(f"c{ci.col}: {_clip(ci.cell.text, 40)}" for ci in cells)
                    units.append(_Unit(f"t{ti}.r{r}", i, text, shown))
    return units


def _bm25(docs: list[list[str]], query: list[str], k1: float = 1.2, b: float = 0.5) -> tuple[list[float], dict]:
    """BM25 scores, scaled by the idf-weighted share of the query terms each part contains, so a
    paragraph with every query word beats a short one with just one of them."""
    n = len(docs)
    avg = sum(len(d) for d in docs) / n if n else 0
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    terms = set(query)
    idf = {q: math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5)) for q in terms if df[q]}
    weight = sum(idf.values()) or 1
    scores = []
    for d in docs:
        tf = Counter(d)
        s = hit = 0.0
        for q in terms:
            if q in idf and tf[q]:
                s += idf[q] * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * len(d) / (avg or 1)))
                hit += idf[q]
        scores.append(s * (hit / weight) ** 2)
    return scores, idf


def _snippet(text: str, query: str, idf: dict, width: int = 150) -> str:
    low = text.lower()
    pos = low.find(query.lower()) if query else -1
    if pos < 0:
        for tok in sorted(idf, key=idf.get, reverse=True):
            pos = low.find(tok)
            if pos >= 0:
                break
    pos = max(pos, 0)
    a = max(0, pos - width // 3)
    b = min(len(text), a + width)
    return ("…" if a else "") + _clip(text[a:b], width) + ("…" if b < len(text) else "")


def search(store: Store, path: str, query: str, max_results: int = 10) -> str:
    q = " ".join((query or "").split())
    if not q:
        raise ToolError("query is empty.")
    qtok = tokens(q)
    if not qtok:
        raise ToolError("query has no searchable words (Hangul, letters or digits).")
    path = normalize_path(path)
    doc = store.open(path)
    m = build_map(doc)
    units = _units(doc, m)
    scores, idf = _bm25([tokens(u.text) for u in units], qtok)
    flat = [" ".join(u.text.split()).lower() for u in units]
    exact = [q.lower() in f for f in flat]
    ranked = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: (exact[i], scores[i]), reverse=True)
    if ranked:
        top = scores[ranked[0]]
        ranked = [i for i in ranked if exact[i] or scores[i] >= 0.1 * top]
    meaning_only: set[int] = set()
    sims, note = embed.similarities([_embed_text(m, u) for u in units], q)
    if sims is not None:
        # Reciprocal rank fusion of the keyword ranking and the meaning ranking.
        by_meaning = sorted((i for i, s in enumerate(sims) if s is not None), key=lambda i: sims[i], reverse=True)
        if by_meaning:
            # Similarities of unrelated text are not near zero (e5: ~0.75), so keep only the parts that
            # stand out: at least halfway from the median to the best one. Works for any model's scale.
            best, median = sims[by_meaning[0]], sims[by_meaning[len(by_meaning) // 2]]
            floor = median + (best - median) / 2
            by_meaning = [i for i in by_meaning[:max(20, 2 * max_results)] if sims[i] >= floor or i in ranked]
        fused: Counter = Counter()
        for ranking in (ranked, by_meaning):
            for r, i in enumerate(ranking):
                fused[i] += 1 / (RRF_K + r)
        meaning_only = set(by_meaning) - set(ranked)
        ranked = sorted(fused, key=lambda i: (exact[i], fused[i]), reverse=True)
    if not ranked:
        return (f'No match for "{q}". Try other words, fewer words, or hwp_outline to browse the sections.'
                + (f" ({note})" if note else ""))
    mode = "keywords + meaning" if sims is not None else "keywords"
    marks = [x for x, used in (("* exact phrase", any(exact[i] for i in ranked)),
                               ("~ related by meaning only", bool(meaning_only))) if used]
    lines = [f'{len(ranked)} candidate part(s) for "{q}" ({mode}); best {min(len(ranked), max_results)} first'
             + (f" ({', '.join(marks)})" if marks else "") + ":"]
    if note:
        lines.append(f"Note: {note}.")
    for i in ranked[:max_results]:
        u = units[i]
        where = m.path_of(u.index)
        sec = f"[{where[-1].sid} {_clip(where[-1].title, 24)}] " if where else ""
        body = _snippet(u.display, q, idf) if u.address.startswith("p") else _clip(u.display, 240)
        lines.append(f"{'*' if exact[i] else '~' if i in meaning_only else ' '}{u.address} {sec}{body}")
    if len(ranked) > max_results:
        lines.append(f"... {len(ranked) - max_results} more; refine the query or raise max_results.")
    return "\n".join(lines)


RRF_K = 60


def _embed_text(m: DocMap, u: _Unit) -> str:
    """What is embedded for a search unit: its section title gives a short paragraph context."""
    where = m.path_of(u.index)
    return f"{where[-1].title}: {u.text}" if where and where[-1].sid != "s0" else u.text


def _warm(doc: HwpxDocument, m: DocMap) -> None:
    """Start embedding the document in the background when semantic search is on."""
    if embed.settings()["provider"] not in ("", "none", "off"):
        embed.warm([_embed_text(m, u) for u in _units(doc, m)])


# ---------------------------------------------------------------------------
# hwp_diff
# ---------------------------------------------------------------------------
@dataclass
class _Item:
    address: str
    key: str                                  # what is compared for text changes
    text: str                                 # shown
    style: str = ""
    para: str = ""
    cell: str = ""
    runs: list[tuple[int, str]] = field(default_factory=list)  # (length, char format) per text run


def _plain_view(el) -> str:
    """Paragraph text with objects as index-free labels (so renumbered tables/images compare equal)."""
    parts = []
    for run in run_elements(el):
        for child in run:
            if child.tag == T_TAG:
                parts.append(t_text(child))
            elif _local(child.tag) in _OBJECT_NAMES:
                name = _local(child.tag)
                if name == "tbl":
                    parts.append(f"<table {child.get('rowCnt')}x{child.get('colCnt')}>")
                elif name == "equation":
                    parts.append(f"<equation {child.findtext(HP + 'script') or ''}>")
                else:
                    sz = child.find(HP + "sz")
                    size = f" {hu_to_mm(int(sz.get('width'))):g}x{hu_to_mm(int(sz.get('height'))):g}mm" if sz is not None else ""
                    parts.append(f"<{'image' if name == 'pic' else name}{size}>")
    return "".join(parts)


class _Describer:
    """Cached format descriptions for one document."""

    def __init__(self, doc: HwpxDocument):
        self.doc = doc
        self._char: dict = {}
        self._para: dict = {}
        self._style: dict = {}

    def char(self, cid) -> str:
        if cid not in self._char:
            self._char[cid] = formats.short_char_desc(formats.describe_char_pr(self.doc, cid))
        return self._char[cid]

    def para(self, pid) -> str:
        if pid not in self._para:
            self._para[pid] = formats.short_para_desc(formats.describe_para_pr(self.doc, pid))
        return self._para[pid]

    def style(self, sid) -> str:
        if sid not in self._style:
            self._style[sid] = _style_name(self.doc, sid)
        return self._style[sid]

    def item(self, address: str, key_prefix: str, paragraphs: list, cell: str = "") -> _Item:
        text = "\n".join(_plain_view(p) for p in paragraphs)
        runs = []
        for p in paragraphs:
            for run in run_elements(p):
                n = len(run_text(run))
                if n:
                    runs.append((n, self.char(run.get("charPrIDRef"))))
            runs.append((1, ""))  # the "\n" between paragraphs
        first = paragraphs[0]
        return _Item(address, key_prefix + text, text, self.style(first.get("styleIDRef")),
                     self.para(first.get("paraPrIDRef")), cell, runs[:-1])


def _items(doc: HwpxDocument) -> tuple[list[_Item], list[str]]:
    d = _Describer(doc)
    tables_at: dict[int, list[tuple[int, Any]]] = {}
    for ti, (tbl, pi) in enumerate(top_tables(doc)):
        tables_at.setdefault(pi, []).append((ti, tbl))
    items = []
    for i, p in enumerate(body_paragraphs(doc)):
        items.append(d.item(f"p{i}", "", [p.element]))
        for ti, tbl in tables_at.get(i, []):
            for ci in table_cells(tbl):
                cell_el = ci.cell.element
                fill = formats.describe_border_fill(doc, cell_el.get("borderFillIDRef"))
                cell = ", ".join(f"{k} {v}" for k, v in sorted(fill.items())) if fill else ""
                paras = [cp.element for cp in ci.cell.paragraphs]
                items.append(d.item(f"t{ti}.r{ci.row}.c{ci.col}", f"[cell r{ci.row} c{ci.col}] ", paras, cell))
    pages = [_page_info(doc, s) for s in range(len(doc.sections))]
    return items, pages


def _text_change(old: str, new: str, context: int = 20, limit: int = 150) -> str:
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    if not ops:
        return ""
    a0, a1 = max(0, ops[0][1] - context), min(len(old), ops[-1][2] + context)
    b0, b1 = max(0, ops[0][3] - context), min(len(new), ops[-1][4] + context)

    def part(s, x, y):
        return ("…" if x else "") + _clip(s[x:y], limit) + ("…" if y < len(s) else "")
    return f'"{part(old, a0, a1)}" -> "{part(new, b0, b1)}"'


def _format_change(old: _Item, new: _Item) -> list[str]:
    out = []
    if old.style != new.style:
        out.append(f"style {old.style} -> {new.style}")
    if old.para != new.para:
        out.append(f"paragraph {old.para or '-'} -> {new.para or '-'}")
    if old.cell != new.cell:
        out.append(f"cell {old.cell or '-'} -> {new.cell or '-'}")
    if old.text == new.text and old.runs != new.runs:
        a = [f for n, f in old.runs for _ in range(n)]
        b = [f for n, f in new.runs for _ in range(n)]
        spans, i = [], 0
        while i < min(len(a), len(b)):
            if a[i] != b[i]:
                j = i
                while j < len(a) and j < len(b) and a[j] != b[j] and a[j] == a[i] and b[j] == b[i]:
                    j += 1
                spans.append(f'"{_clip(new.text[i:j], 30)}" {a[i] or "-"} -> {b[i] or "-"}')
                i = j
            else:
                i += 1
        out.extend(spans[:3])
        if len(spans) > 3:
            out.append(f"+{len(spans) - 3} more run change(s)")
    return out


def diff_documents(old: HwpxDocument | None, new: HwpxDocument, budget: int = DIFF_BUDGET) -> str:
    """Paragraph-level report of what changed from ``old`` to ``new`` (text, format, pages)."""
    old_items, old_pages = _items(old) if old is not None else ([], [])
    new_items, new_pages = _items(new)
    sm = difflib.SequenceMatcher(None, [x.key for x in old_items], [x.key for x in new_items], autojunk=False)
    changes: list[str] = []
    n_text = n_add = n_del = n_fmt = 0

    def fmt_line(o: _Item, nw: _Item):
        nonlocal n_fmt
        fc = _format_change(o, nw)
        if fc:
            n_fmt += 1
            changes.append(f"f {nw.address}: " + "; ".join(fc))

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for o, nw in zip(old_items[i1:i2], new_items[j1:j2]):
                fmt_line(o, nw)
            continue
        pairs = min(i2 - i1, j2 - j1) if tag == "replace" else 0
        for k in range(pairs):
            o, nw = old_items[i1 + k], new_items[j1 + k]
            n_text += 1
            was = f" (was {o.address})" if o.address != nw.address else ""
            changes.append(f"~ {nw.address}{was}: {_text_change(o.text, nw.text)}")
            fc = _format_change(o, nw)
            if fc:
                changes.append("  format: " + "; ".join(fc))
        for o in old_items[i1 + pairs:i2]:
            n_del += 1
            changes.append(f"- (was {o.address}): {_clip(o.text, 120) or '(empty)'}")
        for nw in new_items[j1 + pairs:j2]:
            n_add += 1
            changes.append(f"+ {nw.address} [{nw.style}]: {_clip(nw.text, 120) or '(empty)'}")
    for s in range(max(len(old_pages), len(new_pages))):
        a = old_pages[s] if s < len(old_pages) else "(none)"
        b = new_pages[s] if s < len(new_pages) else "(none)"
        if a != b:
            changes.append(f"page section {s}: {a} -> {b}")
    if not changes:
        return "No differences in text, formatting or page setup."
    head = (f"{n_text} changed, {n_add} added, {n_del} deleted, {n_fmt} format-only change(s). "
            "Addresses are current ones; ~ changed, + added, - deleted, f format.")
    lines, used = [head], len(head)
    for i, c in enumerate(changes):
        if used + len(c) > budget:
            lines.append(f"... {len(changes) - i} more change(s) not shown.")
            break
        lines.append(c)
        used += len(c) + 1
    return "\n".join(lines)


def _open_bytes(data: bytes | None) -> HwpxDocument | None:
    if data is None:
        return None
    try:
        return HwpxDocument.open(data)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Cannot open the earlier version for comparison: {exc}") from exc


def diff(store: Store, path: str, since: str = "last", against: str | None = None,
         budget: int = DIFF_BUDGET) -> str:
    path = normalize_path(path)
    new = store.open(path)
    if against:
        other = normalize_path(against)
        if not os.path.exists(other):
            raise ToolError(f"File not found: {other}")
        with open(other, "rb") as fh:
            old = _open_bytes(fh.read())
        label = f"Compared with {os.path.basename(other)}"
    else:
        old = _open_bytes(store.earlier_version(path, since))
        label = "Changes by the last edit" if since == "last" else "Changes in this session"
        if old is None:
            label += " (the file was created then)"
    return f"{label}:\n" + diff_documents(old, new, budget)
