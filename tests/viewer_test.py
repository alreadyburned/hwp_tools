"""Check the editable viewer (hwp_mcp.viewer): paragraph addresses in the HTML, pasting paragraphs
from another document with their formatting, pasting text, page margins, and the command line
the VS Code extension uses.

Run:  <venv python> tests/viewer_test.py [workdir]      (PYTHONPATH=server when testing the source tree)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings

from hwp_mcp import check, reader, viewer
from hwp_mcp.api import HwpDoc
from hwp_mcp.model import body_paragraphs, paragraph_text
from hwp_mcp.store import Store, ToolError

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "samples", "샘플_사업보고서.hwpx")


def texts(path: str) -> list[str]:
    return [paragraph_text(p.element) for p in body_paragraphs(Store().open(path))]


def run_cli(op: dict) -> dict:
    p = subprocess.run([sys.executable, "-m", "hwp_mcp.viewer", "apply"], input=json.dumps(op).encode("utf-8"),
                       capture_output=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return json.loads(p.stdout.decode("utf-8").strip().splitlines()[-1])


def check_annotate() -> None:
    html = ('<section class="hwpx-preview-page"><p class="hwpx-paragraph">a</p><p class="hwpx-paragraph">&nbsp;</p>'
            '<table class="hwpx-table"><tr><td><p class="hwpx-paragraph">cell</p></td></tr></table>'
            '<p class="hwpx-paragraph">b</p></section>')
    out, ok = viewer.annotate(html, 3)
    assert ok, out
    assert re.findall(r'data-addr="(p\d)"', out) == ["p0", "p1", "p1", "p2"], out  # the table carries p1
    assert '<td><p class="hwpx-paragraph">cell' in out, "cell paragraphs are not body paragraphs"
    same, ok = viewer.annotate(html, 4)
    assert not ok and same == html, "a count mismatch leaves the viewer read-only"


def check_render(work: str) -> None:
    for name in ("샘플_사업보고서.hwpx", "샘플_사업보고서.hwp"):
        html = viewer.render(os.path.join(ROOT, "samples", name))
        info = json.loads(re.search(r'id="hwp-doc-info">(.*?)</script>', html).group(1))
        assert info["editable"] and info["paragraphs"] == 16, info
        assert info["sections"][0]["margins"] == {"left": 25.0, "right": 25.0, "top": 20.0, "bottom": 15.0,
                                                  "header": 15.0, "footer": 15.0}, info
        assert info["hwp"] == name.endswith(".hwp")
        assert len(set(re.findall(r'data-addr="(p\d+)"', html))) == 16
    print("render: 16 addressed paragraphs, margins in the info block")


def check_paste_paragraphs(work: str) -> None:
    store = Store()
    src = os.path.join(work, "src.hwpx")
    shutil.copy(SAMPLE, src)
    dst = os.path.join(work, "dst.hwpx")
    doc = HwpDoc.new(dst, overwrite=True)
    doc.insert_paragraph("첫 문단\n끝 문단")
    doc.save()
    # heading + unit note + table (with merged cells and fills) after p0
    r = viewer.paste_paragraphs(store, dst, "p0", src, ["p5", "p6", "p7"])
    assert r["inserted"] == ["p1", "p2", "p3"], r
    assert texts(dst)[:5] == ["첫 문단", "분기별 실적", "(단위: 억 원)", "", "끝 문단"], texts(dst)
    listing = reader.read_document(store, dst, "p1-p3", show_format=True)
    print(listing)
    assert "p1 [개요 1] 분기별 실적" in listing and "맑은 고딕 14pt bold #1F3864" in listing, "style and look kept"
    assert "c1(merged r4-4 c1-4): 클라우드" in listing, "merged table cells kept"
    # an image and its caption at the end; the picture's binary comes along
    r = viewer.paste_paragraphs(store, dst, None, src, ["p9", "p10"])
    assert r["inserted"] == ["p5", "p6"] and texts(dst)[6].startswith("그림 1."), (r, texts(dst))
    assert "<image g0: 110x59.6 mm>" in reader.read_document(store, dst, "p5")
    assert check.check_file(dst) == [], "the result opens cleanly"
    # from a .hwp source into a .hwp target
    hwp_src = os.path.join(work, "src.hwp")
    shutil.copy(os.path.join(ROOT, "samples", "샘플_사업보고서.hwp"), hwp_src)
    hwp_dst = os.path.join(work, "dst.hwp")
    HwpDoc.open(dst).save(hwp_dst)
    r = viewer.paste_paragraphs(Store(), hwp_dst, "p0", hwp_src, ["p3", "p4"])
    assert texts(hwp_dst)[1:3] == ["개요", texts(hwp_src)[4]], texts(hwp_dst)[:4]
    # stale copy: the source lost paragraphs since it was copied
    try:
        viewer.paste_paragraphs(store, dst, "p0", src, ["p99"])
    except ToolError as exc:
        print("   expected error:", exc)
    else:
        raise AssertionError("pasting paragraphs that no longer exist should fail")


def check_paste_text(work: str) -> None:
    store = Store()
    path = os.path.join(work, "text.hwpx")
    shutil.copy(SAMPLE, path)
    r = viewer.paste_text(store, path, "p4", "외부에서 복사한 첫 줄\r\n둘째 줄\r\n")
    assert r["inserted"] == ["p5", "p6"], r
    assert texts(path)[5:7] == ["외부에서 복사한 첫 줄", "둘째 줄"]
    listing = reader.read_document(store, path, "p4-p5", show_format=True)
    fmt = re.findall(r"\{(.*)\}", listing)
    assert fmt[0].split(";")[0] == fmt[1].split(";")[0], f"the pasted text takes the anchor's look: {fmt}"
    blank = os.path.join(work, "blank.hwpx")
    HwpDoc.new(blank, overwrite=True).save()
    r = viewer.paste_text(store, blank, None, "하나\n둘")
    assert r["inserted"] == ["p0", "p1"] and texts(blank) == ["하나", "둘"], (r, texts(blank))
    for bad in ("", "\n\n"):
        try:
            viewer.paste_text(store, blank, None, bad)
        except ToolError:
            pass
        else:
            raise AssertionError("empty clipboard text should fail")


def check_margins(work: str) -> None:
    store = Store()
    path = os.path.join(work, "margins.hwpx")
    doc = HwpDoc.new(path, overwrite=True)
    doc.insert_paragraph("1구역")
    doc.add_section(orientation="landscape")
    doc.insert_paragraph("2구역")
    doc.save()
    viewer.set_margins(store, path, None, {"left": 20, "right": 18.5, "top": 12})
    info = viewer.section_info(Store().open(path))
    assert [s["margins"]["left"] for s in info] == [20, 20] and info[1]["margins"]["right"] == 18.5, info
    viewer.set_margins(store, path, 1, {"header": 5, "footer": 5})
    info = viewer.section_info(Store().open(path))
    assert info[0]["margins"]["header"] == 15 and info[1]["margins"]["header"] == 5, info
    assert info[1]["width_mm"] > info[1]["height_mm"], "landscape section keeps its orientation"
    for bad in ({"left": 120, "right": 90}, {"top": -1}, {"gutter": 3}, {}):
        before = open(path, "rb").read()
        try:
            viewer.set_margins(store, path, 0, bad)
        except ToolError as exc:
            print("   expected error:", exc)
        else:
            raise AssertionError(f"margins {bad} should fail")
        assert open(path, "rb").read() == before, "a rejected edit leaves the file untouched"


def check_cli(work: str) -> None:
    path = os.path.join(work, "cli.hwpx")
    shutil.copy(SAMPLE, path)
    ok = run_cli({"op": "paste_text", "path": path, "after": "p0", "text": "CLI 문단"})
    assert ok == {"ok": True, "inserted": ["p1"], "message": "붙여넣었습니다: 문단 1개 (텍스트)"}, ok
    bad = run_cli({"op": "margins", "path": path, "section": 0, "margins": {"left": 200}})
    assert bad["ok"] is False and "0-150 mm" in bad["message"], bad
    unknown = run_cli({"op": "explode", "path": path})
    assert unknown["ok"] is False, unknown
    html = subprocess.run([sys.executable, "-m", "hwp_mcp.viewer", "render", path], capture_output=True,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8")).stdout.decode("utf-8")
    assert 'data-addr="p1"' in html and "CLI 문단" in html


def main() -> None:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hwpviewer-")
    os.makedirs(work, exist_ok=True)
    check_annotate()
    check_render(work)
    check_paste_paragraphs(work)
    check_paste_text(work)
    check_margins(work)
    check_cli(work)
    print("\nALL VIEWER CHECKS PASSED ->", work)


if __name__ == "__main__":
    main()
