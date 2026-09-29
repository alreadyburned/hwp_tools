"""Check the reading layer (outline, range reads, search, diff) and the edit result views,
including on a generated ~100-page document.

Run:  <venv python> tests/reader_test.py [workdir]      (PYTHONPATH=server when testing the source tree)
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import warnings

from hwpx import HwpxDocument

from hwp_mcp import formats, ops, reader
from hwp_mcp.api import HwpDoc
from hwp_mcp.store import Store, ToolError

warnings.filterwarnings("ignore")

FILLER = ("본 사업은 지역 산업의 경쟁력을 높이고 일자리를 창출하기 위하여 추진되었으며, 관계 기관과의 협의를 거쳐 "
          "세부 과제를 확정하였다. 추진 과정에서 나타난 문제점은 차기 계획에 반영하여 개선할 예정이다. ")


def add(doc: HwpxDocument, text: str, style: str = "바탕글", size: float | None = None, bold: bool = False):
    st = formats.resolve_style(doc, style)
    p = doc.sections[-1].add_paragraph(text, style_id_ref=st.get("id"), para_pr_id_ref=st.get("paraPrIDRef"),
                                       char_pr_id_ref=st.get("charPrIDRef"))
    if size or bold:
        kw = {"size_pt": size} if size else {}
        if bold:
            kw["bold"] = True
        from hwp_mcp.model import run_elements
        formats.apply_char_format(doc, run_elements(p.element), formats.char_kwargs(kw))
    return p


def save(doc: HwpxDocument, path: str) -> str:
    with open(path, "wb") as fh:
        fh.write(doc.to_bytes())
    return path


def big_document(path: str) -> str:
    """10 chapters x 5 sections x 12 paragraphs of ~250 characters, a table per chapter."""
    doc = HwpxDocument.new()
    for c in range(1, 11):
        add(doc, f"제{c}장 사업 영역 {c}", "개요 1")
        for s in range(1, 6):
            add(doc, f"{c}.{s} 세부 과제 {c}-{s}", "개요 2")
            for k in range(12):
                text = f"[{c}-{s}-{k}] " + FILLER + FILLER[:60]
                if (c, s, k) == (7, 3, 4):
                    text = "희귀한 반도체 장비 국산화 성과가 두드러졌다. " + text
                add(doc, text, "본문")
        holder = add(doc, "")
        tbl = holder.add_table(3, 3)
        for r in range(3):
            for col in range(3):
                tbl.set_cell_text(r, col, f"장{c} 행{r} 열{col}" if (c, r, col) != (4, 2, 1) else "특수예산 12억")
    return save(doc, path)


def check_outline_big(store: Store, path: str) -> None:
    t = time.perf_counter()
    out = reader.outline(store, path)
    took = time.perf_counter() - t
    print(out[:600], "\n...", f"\n[outline {took:.2f}s, {len(out)} chars]")
    assert "Headings: outline levels" in out
    assert re.search(r"^s10  p\d+-p\d+  제10장", out, re.M), "top-level chapters listed"
    assert "  s7.3  " in out, "second level fits in the line budget (60 headings)"
    assert len(out.splitlines()) < reader.OUTLINE_MAX_LINES + 5
    one = reader.outline(store, path, section="s7")
    assert one.count("\n  s7.") == 5 and "s8" not in one, one
    shallow = reader.outline(store, path, depth=1)
    assert "deeper heading(s) hidden" in shallow and "s7.3" not in shallow


def check_read_big(store: Store, path: str) -> None:
    start = reader.read_document(store, path)
    assert "Large document" in start and 'Continue with range="p' in start, start[-300:]
    assert len(start) <= reader.READ_BUDGET + 500
    sec = reader.read_document(store, path, "s7.3")
    print(sec[:400], "\n...")
    assert "In: s7 제7장" in sec and "희귀한 반도체" in sec
    assert "Continue with" not in sec, "one section fits in one read"
    m = re.search(r'Continue with range="(p\d+-p\d+)"', start)
    nxt = reader.read_document(store, path, m.group(1))
    assert nxt.splitlines()[2].startswith("--- p"), nxt[:200]
    tbl = reader.read_document(store, path, "t3.r1-")
    assert "t3.r1 |" in tbl and "t3.r2 |" in tbl and "t3.r0 |" not in tbl, tbl
    for bad in ("x1", "s99", "p999999", "t3.r9"):
        try:
            reader.read_document(store, path, bad)
        except ToolError as exc:
            print("   expected error:", str(exc)[:100])
        else:
            raise AssertionError(f"range {bad} should fail")


def check_search_big(store: Store, path: str) -> None:
    t = time.perf_counter()
    out = reader.search(store, path, "반도체 장비 국산화")
    print(out.splitlines()[:3], f"[search {time.perf_counter() - t:.2f}s]")
    first = out.splitlines()[1]
    assert first.startswith("*p") and "[s7.3 " in first, first
    loose = reader.search(store, path, "국산화한 반도체의 성과")  # other endings and order
    assert "[s7.3 " in loose.splitlines()[1], loose
    cell = reader.search(store, path, "특수예산")
    assert re.search(r"^\*t3\.r2 \[s4\.5 ", cell.splitlines()[1]), cell
    assert "No match" in reader.search(store, path, "양자컴퓨터")


def check_heuristics(work: str) -> None:
    store = Store()
    doc = HwpxDocument.new()
    for text in ["2026년 업무 계획", "Ⅰ. 추진 배경", "1. 현황", "가. 인력 현황", FILLER, "나. 예산 현황", FILLER,
                 "2. 문제점", FILLER, "Ⅱ. 추진 계획", "1. 목표", FILLER, "1) 세부 목표는 다음과 같이 정한다.", FILLER]:
        add(doc, text)
    path = save(doc, os.path.join(work, "patterns.hwpx"))
    out = reader.outline(store, path)
    print(out)
    assert "numbering" in out
    assert re.search(r"^s1  p2-p9  Ⅰ\. 추진 배경", out, re.M), out  # p0 is the new document's blank paragraph
    assert re.search(r"^  s1\.1  p3-p7  1\. 현황", out, re.M) and re.search(r"^    s1\.1\.2  p6-p7  나\.", out, re.M), out
    assert "세부 목표" not in out, "a sentence is not a heading"
    assert re.search(r"^s0  p0-p1", out, re.M), "the title before the first heading"

    doc = HwpxDocument.new()
    add(doc, "연구 보고서", size=20, bold=True)
    for h in ("서론", "방법", "결과"):
        add(doc, h, size=14, bold=True)
        add(doc, "소제목 가", bold=True)
        add(doc, FILLER)
        add(doc, FILLER)
    path = save(doc, os.path.join(work, "fonts.hwpx"))
    out = reader.outline(store, path)
    print(out)
    assert "font size" in out and re.search(r"^s3  p\d+-p\d+  결과", out, re.M), out
    assert re.search(r"^  s1\.1  p\d+-p\d+  소제목 가", out, re.M), out

    doc = HwpxDocument.new()
    for i in range(150):  # ~18k characters -> several fixed parts
        add(doc, f"{i}번째 문단. " + FILLER)
    path = save(doc, os.path.join(work, "plain.hwpx"))
    out = reader.outline(store, path)
    print(out.splitlines()[:3])
    assert "fixed parts" in out and "s3 " in out


def check_edit_views_and_diff(work: str) -> None:
    store = Store()
    path = os.path.join(work, "diff.hwpx")
    if os.path.exists(path):
        os.remove(path)
    ops.create_document(store, path, False, {})
    r = ops.insert_paragraph(store, path, "제목\n첫째 문단입니다.\n둘째 문단입니다.\n셋째 문단입니다.", None, None, None)
    assert "Now:" in r and "p3 [" in r, r
    d = reader.diff(store, path)
    assert '~ p0: "" -> "제목"' in d and "+ p3 [바탕글]: 셋째" in d, d  # vs the empty new document
    d = reader.diff(store, path, "session")
    assert "(the file was created then)" in d and "+ p0 [바탕글]: 제목" in d, d

    r = ops.insert_paragraph(store, path, "끼운 문단", "p1", None, None)
    print(r)
    assert "shifted by +1 (the old p2 is now p3)" in r and "p2 [" in r, r
    d = reader.diff(store, path)
    print(d)
    assert "0 changed, 1 added, 0 deleted" in d and "+ p2" in d, d

    r = ops.replace_text(store, path, "둘째", "두 번째", None, False, None)
    assert "p3: 두 번째 문단입니다." in r, r
    d = reader.diff(store, path)
    print(d)
    assert '~ p3: "둘째 문단입니다." -> "두 번째 문단입니다."' in d, d

    r = ops.format_text(store, path, "p3", "번째", None, None, None, {"bold": True, "color": "#FF0000"})
    print(r)
    assert 'p3[2:4] "번째":' in r and "bold" in r and "#FF0000" in r, r
    d = reader.diff(store, path)
    print(d)
    assert re.search(r'f p3: "번째" .* -> .*bold', d) and "0 changed, 0 added, 0 deleted, 1 format-only" in d, d

    r = ops.set_paragraph_format(store, path, "p0", {"align": "center"})
    assert "Now:" in r and "center" in r, r
    assert "paragraph " in reader.diff(store, path)

    r = ops.delete_paragraphs(store, path, "p1-p2")
    print(r)
    assert "shifted by -2 (the old p3 is now p1)" in r and "p1 [" in r and "두 번째" in r, r
    d = reader.diff(store, path)
    assert d.count("\n- (was p") == 2, d

    ops.insert_table(store, path, "p0", 2, 2, None, None, False)
    r = ops.set_cell_text(store, path, 0, [["가", "나"], ["다", "라"]], 0, 0)
    assert "t0.r1 | c0: 다 | c1: 라" in r, r
    d = reader.diff(store, path)
    assert '~ t0.r0.c0' in d or "t0.r0.c0" in d, d

    ops.undo(store, path)
    d = reader.diff(store, path, "session")
    print(d)
    assert "Changes in this session" in d and "<table 2x2>" in d

    copy = os.path.join(work, "diff_copy.hwpx")
    ops.save_as(store, path, copy, True)
    ops.replace_text(store, copy, "셋째", "세 번째", None, False, None)
    d = reader.diff(store, copy, against=path)
    assert "Compared with diff.hwpx" in d and "세 번째" in d, d

    fresh = Store()
    try:
        reader.diff(fresh, path)
    except ToolError as exc:
        print("   expected error:", str(exc)[:80])
    else:
        raise AssertionError("diff without history should fail")

    doc = HwpDoc.open(path)
    doc.set_paragraph_text("p0", "새 제목")
    d = doc.diff()
    assert '"제목" -> "새 제목"' in d, d
    assert "s1" in doc.outline() or "fixed parts" in doc.outline()
    assert "새 제목" in doc.search("제목")
    assert "p0 [" in doc.read(start=0, limit=2)


def check_diff_big(store: Store, path: str) -> None:
    ops.replace_text(store, path, "희귀한", "드문", None, False, None)
    t = time.perf_counter()
    d = reader.diff(store, path)
    took = time.perf_counter() - t
    print(d, f"[diff {took:.2f}s]")
    assert "1 changed, 0 added, 0 deleted, 0 format-only" in d, d


def main() -> None:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hwpreader-")
    os.makedirs(work, exist_ok=True)
    big = big_document(os.path.join(work, "big.hwpx"))
    store = Store()
    t = time.perf_counter()
    listing = reader.read_document(store, big)
    print(listing.splitlines()[0], f"[open+read {time.perf_counter() - t:.2f}s]")
    check_outline_big(store, big)
    check_read_big(store, big)
    check_search_big(store, big)
    check_diff_big(store, big)
    check_heuristics(work)
    check_edit_views_and_diff(work)
    print("\nALL READER CHECKS PASSED ->", work)


if __name__ == "__main__":
    main()
