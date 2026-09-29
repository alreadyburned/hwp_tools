"""Run a battery of edits on copies of real Hancom documents.

Run:  <venv python> tests/corpus_test.py <samples dir> <work dir>
Each step is saved and validated by the Store; failures are collected, not fatal.
"""

from __future__ import annotations

import glob
import os
import shutil
import struct
import sys
import time
import traceback
import zlib

from hwp_mcp import ops, reader
from hwp_mcp.model import body_paragraphs, paragraph_text, top_tables
from hwp_mcp.store import Store, ToolError


def png(path: str) -> str:
    raw = b"".join(b"\x00" + b"\x10\x80\xd0" * 40 for _ in range(20))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 40, 20, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def run_file(src: str, work: str, img: str) -> tuple[list[str], list[str]]:
    name = os.path.basename(src)
    path = os.path.join(work, name)
    shutil.copy(src, path)
    store = Store()
    ok, bad = [], []

    def step(label, fn, *a, **k):
        t0 = time.perf_counter()
        try:
            fn(store, *a, **k)
            ok.append(f"{label} ({time.perf_counter() - t0:.2f}s)")
        except ToolError as exc:
            bad.append(f"{label}: ToolError: {exc}")
        except Exception:  # noqa: BLE001
            bad.append(f"{label}: CRASH {traceback.format_exc(limit=3)}")

    step("read", reader.read_document, path, None, 200, True)
    step("outline", reader.outline, path)
    step("search", reader.search, path, "사업 계획")
    try:
        doc = store.open(path)
    except ToolError as exc:
        return ok, [f"open: {exc}"]
    body = body_paragraphs(doc)
    text_idx = next((i for i, p in enumerate(body) if len(paragraph_text(p.element).strip()) >= 4), None)
    if text_idx is not None:
        text = paragraph_text(body[text_idx].element)
        word = text.strip()[:2]
        step("get_paragraph", ops.get_paragraph, path, f"p{text_idx}")
        step("format_match", ops.format_text, path, f"p{text_idx}", word, 1, None, None, {"bold": True, "color": "#C00000"})
        step("format_range", ops.format_text, path, f"p{text_idx}", None, None, 0, min(3, len(text)), {"size_pt": 14, "italic": True})
        step("para_format", ops.set_paragraph_format, path, f"p{text_idx}", {"align": "center", "line_spacing_percent": 200, "first_line_indent_mm": 5})
        step("replace", ops.replace_text, path, word, word + "★", f"p{text_idx}", False, 1)
        step("set_text", ops.set_paragraph_text, path, f"p{text_idx}", "전체 교체된 문단\t탭 포함")
        step("apply_style", ops.apply_style, path, f"p{text_idx}", "0", False)
    step("insert_after_p0", ops.insert_paragraph, path, "삽입 문단 A\n삽입 문단 B", "p0", None, None)
    step("insert_start", ops.insert_paragraph, path, "문서 맨 앞", "start", None, None)
    step("delete_p0", ops.delete_paragraphs, path, "p0")
    step("insert_end", ops.insert_paragraph, path, "문서 끝", None, None, None)
    step("create_style", ops.create_style, path, "테스트스타일", None, {"bold": True, "size_pt": 12}, {"align": "center"})
    step("list", ops.set_list, path, "p1-p2", "number", 1, None, "가", None)
    step("new_table", ops.insert_table, path, "p1", None, None, [["a", "b"], ["c", "d"]], [30, 50], True)
    doc = store.open(path)
    n_tables = len(top_tables(doc))
    for ti in range(min(n_tables, 3)):
        step(f"t{ti}.get", ops.get_table_info, path, ti, True)
        step(f"t{ti}.fill_row0", ops.format_cells, path, f"t{ti}.r0.c*", fill_color="#DDEBF7", border_type="solid",
             border_width_mm=0.4, border_color="#1F4E79", borders="outer", vertical_align="middle", padding_mm=None)
        step(f"t{ti}.cell_text", ops.set_cell_text, path, ti, [["셀 수정"]], 0, 0)
        step(f"t{ti}.cell_para", ops.set_paragraph_format, path, f"t{ti}.r0.c0", {"align": "right"})
        step(f"t{ti}.row_below", ops.table_structure, path, ti, "insert_row_below", 0, 1)
        step(f"t{ti}.row_above", ops.table_structure, path, ti, "insert_row_above", 0, 1)
        step(f"t{ti}.col_right", ops.table_structure, path, ti, "insert_column_right", 0, 1)
        step(f"t{ti}.col_left", ops.table_structure, path, ti, "insert_column_left", 0, 1)
        step(f"t{ti}.del_row", ops.table_structure, path, ti, "delete_row", 1, 1)
        step(f"t{ti}.del_col", ops.table_structure, path, ti, "delete_column", 1, 1)
        step(f"t{ti}.merge", ops.merge_cells, path, f"t{ti}.r0.c0-1")
        step(f"t{ti}.layout", ops.set_table_layout, path, ti, column_widths_mm=None, row_heights_mm=None, align="center", repeat_header_row=True)
    step("image", ops.insert_image, path, img, "p1", 30, None, "center")
    step("page_setup", ops.page_setup, path, 0, {"margin_left_mm": 20, "margin_right_mm": 20})
    step("footer", ops.header_footer, path, "footer", None, "dash", "center", 0, False)
    step("read_end", reader.read_document, path, "p0-p49", 80, False)
    step("diff", reader.diff, path, "session")
    return ok, bad


def main() -> None:
    samples, work = sys.argv[1], sys.argv[2]
    os.makedirs(work, exist_ok=True)
    img = png(os.path.join(work, "img.png"))
    files = sorted(glob.glob(os.path.join(samples, "*.hwp")) + glob.glob(os.path.join(samples, "*.hwpx")))
    total_bad = 0
    for src in files:
        t0 = time.perf_counter()
        ok, bad = run_file(src, work, img)
        total_bad += len(bad)
        print(f"== {os.path.basename(src)}: {len(ok)} ok, {len(bad)} failed, {time.perf_counter() - t0:.1f}s")
        for b in bad:
            print("   - " + b.replace("\n", "\n     ")[:900])
    print(f"\nTOTAL FAILURES: {total_bad}")


if __name__ == "__main__":
    main()
