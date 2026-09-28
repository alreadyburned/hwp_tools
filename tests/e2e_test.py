"""End-to-end test: drive the MCP server over stdio and check the written files.

Run:  <venv python> tests/e2e_test.py [workdir]
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

import hwpx
from hwpx import HwpxDocument


class Client:
    def __init__(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, "-m", "hwp_mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, env=env)
        self.i = 0
        init = self.req("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "e2e", "version": "0"}})
        self.instructions = init["result"].get("instructions", "")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg) -> None:
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def req(self, method, params=None):
        self.i += 1
        self._send({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params or {}})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            msg = json.loads(line)
            if msg.get("id") == self.i:
                return msg

    def call(self, tool, expect_error=False, **args):
        r = self.req("tools/call", {"name": tool, "arguments": args})["result"]
        text = "".join(c.get("text", "") for c in r["content"])
        err = bool(r.get("isError"))
        status = "ERR" if err else "ok "
        print(f"[{status}] {tool}: {text[:300]}")
        if err != expect_error:
            raise AssertionError(f"{tool} {'failed' if err else 'unexpectedly succeeded'}: {text}")
        return text


def png(w: int, h: int, rgb) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def check_file(path: str) -> HwpxDocument:
    report = hwpx.validate_editor_open_safety(path)
    assert not report.blocking_package_errors and report.reopen_ok and not report.document_validation_error, report
    return HwpxDocument.open(path)


def main() -> None:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hwpmcp-")
    os.makedirs(work, exist_ok=True)
    doc_path = os.path.join(work, "report.hwpx")
    img = os.path.join(work, "logo.png")
    with open(img, "wb") as fh:
        fh.write(png(120, 60, (30, 90, 200)))

    c = Client()
    tools = c.req("tools/list")["result"]["tools"]
    print(f"{len(tools)} tools; instructions {len(c.instructions)} chars")

    c.call("hwp_create_document", path=doc_path, overwrite=True)
    c.call("hwp_insert_paragraph", path=doc_path, text="2026년 사업 보고서")
    c.call("hwp_insert_paragraph", path=doc_path, text="개요\n본 보고서는 2026년 사업 성과를 정리한다. 핵심 지표는 매출과 이익이다.\n세부 내용은 아래 표와 같다.")
    c.call("hwp_format_text", path=doc_path, target="p0", size_pt=20, bold=True, color="#1F4E79", font="맑은 고딕")
    c.call("hwp_set_paragraph_format", path=doc_path, target="p0", align="center", space_after_pt=12)
    c.call("hwp_apply_style", path=doc_path, target="p1", style="개요 1")
    c.call("hwp_format_text", path=doc_path, target="p2", match="매출", bold=True, color="#C00000")
    c.call("hwp_format_text", path=doc_path, target="p2", match="이익", occurrence=1, underline=True, highlight="#FFFF00")
    c.call("hwp_set_paragraph_format", path=doc_path, target="p2-p3", first_line_indent_mm=10, line_spacing_percent=180)
    c.call("hwp_create_style", path=doc_path, name="강조 본문", base_style="본문", bold=True, color="#2E75B6", space_after_pt=6)
    c.call("hwp_apply_style", path=doc_path, target="p3", style="강조 본문")
    c.call("hwp_insert_table", path=doc_path, after="p3", header_row=True,
           data=[["구분", "상반기", "하반기", "합계"], ["매출", "120", "150", "270"], ["이익", "20", "35", "55"]])
    c.call("hwp_set_table_layout", path=doc_path, table=0, column_widths_mm=[40, 40, 40, 30], align="center", repeat_header_row=True)
    c.call("hwp_table_structure", path=doc_path, table=0, action="insert_row_below", index=2)
    c.call("hwp_set_cell_text", path=doc_path, table=0, data=[["비용", "100", "115", "215"]], start_row=3)
    c.call("hwp_table_structure", path=doc_path, table=0, action="insert_column_right", index=3)
    c.call("hwp_set_cell_text", path=doc_path, table=0, data=[["비고"], ["-"], ["-"], ["추정"]], start_col=4)
    c.call("hwp_merge_cells", path=doc_path, cells="t0.r1-2.c4")
    c.call("hwp_format_cells", path=doc_path, cells="t0.r*.c*", borders="outer", border_type="solid", border_width_mm=0.4)
    c.call("hwp_format_cells", path=doc_path, cells="t0.r3.c*", fill_color="#FFF2CC", vertical_align="middle")
    c.call("hwp_set_paragraph_format", path=doc_path, target="t0.r1-3.c1-3", align="right")
    c.call("hwp_format_text", path=doc_path, target="t0.r1.c0", bold=True)
    c.call("hwp_insert_paragraph", path=doc_path, text="그림 1. 로고", after="p4", style="본문")
    c.call("hwp_insert_image", path=doc_path, image_path=img, after="p4", width_mm=50)
    c.call("hwp_insert_image", path=doc_path, image_path=img, after="t0.r0.c0.p0", width_mm=10)
    c.call("hwp_edit_image", path=doc_path, image=0, width_mm=15)
    c.call("hwp_insert_paragraph", path=doc_path, text="첫째 항목\n둘째 항목\n셋째 항목")
    listing = c.call("hwp_read_document", path=doc_path)
    last = int(listing.split("paragraphs (p0-p")[1].split(")")[0])
    c.call("hwp_set_list", path=doc_path, target=f"p{last - 2}-p{last}", kind="bullet")
    c.call("hwp_replace_text", path=doc_path, find="2026년", replace="2027년")
    c.call("hwp_page_setup", path=doc_path, margin_left_mm=25, margin_right_mm=25)
    c.call("hwp_set_header_footer", path=doc_path, kind="footer", page_number="dash")
    c.call("hwp_set_header_footer", path=doc_path, kind="header", text="대외비", align="right")
    c.call("hwp_insert_paragraph", path=doc_path, text="맨 앞 문단", after="start")
    c.call("hwp_delete_paragraphs", path=doc_path, target="p0")
    # errors the model should get clear messages for
    c.call("hwp_format_text", path=doc_path, target="p99", bold=True, expect_error=True)
    c.call("hwp_format_text", path=doc_path, target="p1", match="없는 문자열", bold=True, expect_error=True)
    c.call("hwp_apply_style", path=doc_path, target="p1", style="없는 스타일", expect_error=True)
    c.call("hwp_set_cell_text", path=doc_path, table=0, data=[["x"]], start_row=40, expect_error=True)
    # undo
    c.call("hwp_insert_paragraph", path=doc_path, text="undo me")
    c.call("hwp_undo", path=doc_path)
    print(c.call("hwp_read_document", path=doc_path, show_format=True))
    print(c.call("hwp_get_paragraph", path=doc_path, target="p2"))
    print(c.call("hwp_get_table", path=doc_path, table=0, include_format=True)[:1500])
    c.call("hwp_find_text", path=doc_path, text="이익")
    c.call("hwp_list_styles", path=doc_path)

    d = check_file(doc_path)
    texts = [p.text for p in d.paragraphs]
    assert texts[0] == "2027년 사업 보고서", texts[:3]
    assert "undo me" not in texts
    assert not any("맨 앞 문단" in t for t in texts)

    # .hwp round trip through the tools
    hwp_path = os.path.join(work, "report.hwp")
    c.call("hwp_save_as", path=doc_path, new_path=hwp_path, overwrite=True)
    c.call("hwp_format_text", path=hwp_path, target="p0", italic=True)
    c.call("hwp_insert_paragraph", path=hwp_path, text="HWP 파일에 추가한 문단")
    d2 = HwpxDocument.open(hwp_path)
    assert d2.paragraphs[-1].text == "HWP 파일에 추가한 문단"
    with open(hwp_path, "rb") as fh:
        assert fh.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    with zipfile.ZipFile(doc_path) as z:
        assert any(n.startswith("BinData/") for n in z.namelist())
    print("\nALL CHECKS PASSED ->", work)


if __name__ == "__main__":
    main()
