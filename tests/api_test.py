"""Exercise the direct scripting API (hwp_mcp.api) and check the written XML.

Run:  <venv python> tests/api_test.py [workdir]      (PYTHONPATH=server when testing the source tree)
"""

from __future__ import annotations

import os
import re
import struct
import sys
import tempfile
import zipfile
import zlib

import hwpx
from hwpx import HwpxDocument

from hwp_mcp.api import HH, HP, HwpDoc, HwpError


def png(path: str) -> str:
    raw = b"".join(b"\x00" + b"\x40\xa0\x40" * 60 for _ in range(30))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 60, 30, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def expect_error(fn, *a, **k):
    try:
        fn(*a, **k)
    except HwpError as exc:
        print("   expected error:", str(exc)[:120])
        return
    raise AssertionError(f"{fn.__name__} should have failed")


def main() -> None:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hwpapi-")
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, "api_demo.hwpx")
    img = png(os.path.join(work, "green.png"))

    doc = HwpDoc.new(path, overwrite=True)
    log = lambda m: print("ok ", m)  # noqa: E731

    log(doc.insert_paragraph("고급 기능 시험 문서"))                                      # p0
    log(doc.format_text("p0", size_pt=18, bold=True, shadow_color="#A0A0A0"))
    log(doc.insert_paragraph("목차", style="개요 1"))                                     # p1
    log(doc.insert_paragraph("1. 서론\t3\n2. 본론\t7\n3. 결론\t12"))                       # p2-p4
    print("   tabs", doc.set_tabs("p2-p4", [{"pos_mm": 150, "type": "right", "leader": "dot"}]))
    log(doc.insert_paragraph("고정 줄간격 20pt 문단입니다. 줄간격이 글자 크기와 상관없이 일정합니다."))  # p5
    print("   line", doc.set_line_spacing("p5", 20, kind="fixed"))
    log(doc.insert_paragraph("테두리와 배경이 있는 강조 문단입니다."))                     # p6
    print("   border", doc.set_paragraph_border("p6", border_color="#2E75B6", fill_color="#DEEAF6", border_width_mm=0.4))
    log(doc.insert_paragraph("각주와 미주, 하이퍼링크, 책갈피가 들어간 문단: 한컴 누리집을 참고하라."))  # p7
    log(doc.add_footnote("p7", "각주 내용입니다.", after_match="각주와"))
    log(doc.add_footnote("p7", "미주 내용입니다.", after_match="미주", endnote=True))
    log(doc.add_hyperlink("p7", "https://www.hancom.com", match="한컴 누리집"))
    log(doc.add_bookmark("p7", "본문시작"))
    log(doc.format_text("p7", match="책갈피", outline="SOLID", emboss=True))
    log(doc.add_equation(r"\frac{a+b}{2} \geq \sqrt{ab}"))                               # p8
    log(doc.insert_paragraph("인라인 수식: "))                                              # p9
    log(doc.add_equation(r"E = mc^2", target="p9"))
    log(doc.add_textbox("글상자 안의 텍스트", width_mm=90, height_mm=18, fill_color="#FFF2CC", align="center"))  # p10
    log(doc.add_textbox("떠 있는 글상자", width_mm=50, height_mm=15, floating={"x_mm": 120, "y_mm": 40, "relative_to": "page", "wrap": "in_front_of_text"}))  # p11
    log(doc.insert_image(img, width_mm=30))                                                # p12 g0
    print("   float", doc.float_image(0, x_mm=10, y_mm=5, relative_to="para", wrap="square"))
    log(doc.insert_table(data=[["항목", "값"], ["가", "1"], ["나", "2"]], header_row=True))  # p13 t0
    print("   cells", doc.set_cell_options("t0.r0.c0", diagonal="backslash"),
          doc.set_cell_options("t0.r1-2.c1", padding_mm={"left": 1, "right": 5}),
          doc.set_cell_options("t0.r1.c0", text_direction="vertical"))
    log(doc.insert_paragraph("메모와 변경 추적 대상 문단입니다. 삭제될 문구가 있습니다."))  # p14
    log(doc.add_memo("p14", "검토 필요", author="검토자"))
    log(doc.track_delete("p14", "삭제될 문구가 있습니다.", author="AI"))
    log(doc.track_insert("p14", " 새 문구를 추가합니다.", author="AI"))
    log(doc.insert_paragraph("성명: "))                                                   # p15
    log(doc.add_form_field("p15", "성명", prompt="이름을 입력하세요"))
    log(doc.fill_form_field("성명", "홍길동"))
    log(doc.insert_paragraph("다단 시작 문단. " * 8))                                        # p16
    log(doc.set_columns("p16", 2, separator="SOLID"))
    # low-level: derive a charPr with letter spacing via modify
    print("   raw", doc.modify_char_pr("p16", lambda cp: cp.find(f"{HH}spacing").set("hangul", "-5"), match="다단"))
    sec = doc.add_section(orientation="landscape")
    log(f"section {sec}")
    log(doc.insert_paragraph("가로 방향 새 구역"))
    # errors
    expect_error(doc.set_tabs, "p2", [{"pos_mm": 100, "type": "middle"}])
    expect_error(doc.set_tabs, "p2", [{"pos_mm": 160, "type": "right"}])  # beyond the 150 mm text width
    expect_error(doc.add_hyperlink, "p7", "https://x", match="없는글자")
    expect_error(doc.add_equation, r"\unknowncommand{x}")
    expect_error(doc.set_line_spacing, "p5", 10, kind="double")

    problems = doc.check()
    assert not problems, problems
    print(doc.save())
    print(doc.read(show_format=True))

    # ---- verify the written XML
    report = hwpx.validate_editor_open_safety(path)
    assert not report.blocking_package_errors and report.reopen_ok and not report.document_validation_error, report
    z = zipfile.ZipFile(path)
    header = z.read("Contents/header.xml").decode("utf-8")
    sections = [z.read(n).decode("utf-8") for n in z.namelist() if n.startswith("Contents/section")]
    body = "".join(sections)
    for part in [header] + sections:
        assert not re.search(r"<ns\d+:|xmlns:ns\d+", part), "auto-generated namespace prefix"
    assert re.search(r'<hh:tabItem pos="42520" type="RIGHT" leader="DOT" unit="HWPUNIT"/>', header), "tab case"
    assert re.search(r'<hh:tabItem pos="85040" type="RIGHT" leader="DOT"/>', header), "tab default (2x)"
    assert '<hh:lineSpacing type="FIXED" value="2000" unit="HWPUNIT"/>' in header, "fixed case"
    assert '<hh:lineSpacing type="FIXED" value="4000" unit="HWPUNIT"/>' in header, "fixed default (2x)"
    assert re.search(r'<hp:tab [^>]*leader="2"[^>]*type="2"', body), "inline tab codes RIGHT/DOT"
    assert 'type="HYPERLINK"' in body and "footNote" in body and "endNote" in body
    assert "<hp:equation" in body and "<hp:rect" in body and "drawText" in body
    assert 'textDirection="VERTICAL"' in body and '<hh:backSlash type="CENTER"' in header
    assert len(sections) == 2, "second section"
    listing = HwpDoc.open(path).read(show_format=True)
    for needle in ("<equation:", "<textbox (rect)", "footnote:", "endnote:", "link -> https://www.hancom.com",
                   "bookmark '본문시작'", "form field '성명'", "line fixed 20pt", "border/fill", "tabs right 150mm dot",
                   "floating"):
        assert needle in listing, f"listing lacks {needle!r}"
    assert "(empty)" not in listing, "every paragraph shows its text or objects (new section's blank p is filled)"
    # link wraps the matched text: fieldBegin, then the text, then fieldEnd
    m = re.search(r'fieldBegin[^>]*HYPERLINK.*?</hp:run>(.*?)<hp:fieldEnd', body, re.S)
    assert m and "한컴 누리집" in m.group(1), "hyperlink should wrap the matched text"
    d = HwpxDocument.open(path)
    assert d.paragraphs[7].text.startswith("각주와"), d.paragraphs[7].text
    # .hwp round trip of the advanced document
    hwp = os.path.join(work, "api_demo.hwp")
    print(HwpDoc.open(path).save(hwp))
    print("hwp reopened paragraphs:", len(HwpxDocument.open(hwp).paragraphs))
    try:
        print("preview:", HwpDoc.open(path).preview_png(os.path.join(work, "api_demo.png")))
    except HwpError as exc:
        print("preview skipped:", exc)
    print("\nALL API CHECKS PASSED ->", work)


if __name__ == "__main__":
    main()
