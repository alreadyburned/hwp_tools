"""Run every ```python block of skill/hwp-direct/*.md against a prepared document.

The skill documents are what a model copies from, so their code must work verbatim.
Run:  <venv python> tests/skill_recipes_test.py [workdir]
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from hwp_mcp.api import HH, HP, HwpDoc, HwpError  # noqa: F401  (names used by the recipes)
from hwp_mcp.check import check_file

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "skill" / "hwp-direct" / "SKILL.md", ROOT / "skill" / "hwp-direct" / "reference.md"]


def prepare(path: str) -> None:
    doc = HwpDoc.new(path, overwrite=True)
    doc.insert_paragraph("사업 보고서")                                            # p0
    doc.insert_paragraph("개요")                                                   # p1
    doc.insert_paragraph("핵심 지표는 매출과 이익이다.")                            # p2
    doc.insert_paragraph("매출은 18.4% 증가했다. 자료는 통계청 발표를 따른다.")      # p3
    for i in range(4, 10):
        doc.insert_paragraph(f"본문 문단 {i}. 줄간격과 테두리 시험용 문장입니다.")    # p4-p9
    doc.insert_table(data=[["구분", "값", "비고"]] + [[f"항목{i}", str(i), ""] for i in range(1, 5)])  # t0, 5 rows
    doc.save()


def blocks() -> list[tuple[str, str]]:
    out = []
    for md in DOCS:
        for i, code in enumerate(re.findall(r"```python\n(.*?)```", md.read_text(encoding="utf-8"), re.S)):
            out.append((f"{md.name}#{i}", code))
    return out


def main() -> None:
    work = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="hwpskill-")
    os.makedirs(work, exist_ok=True)
    listings = []
    for n, (name, code) in enumerate(blocks()):  # recipes are independent: fresh document for each
        path = os.path.join(work, f"recipe{n}.hwpx")
        prepare(path)
        code = code.replace(r'HwpDoc.open(r"C:\docs\report.hwpx")', "HwpDoc.open(path)")
        env = {"doc": HwpDoc.open(path), "path": path, "HwpDoc": HwpDoc, "HwpError": HwpError, "HH": HH, "HP": HP}
        print(f"--- {name}")
        exec(compile(code, name, "exec"), env)  # noqa: S102 - trusted repo docs
        if os.path.normcase(os.path.abspath(env["doc"].path)) == os.path.normcase(os.path.abspath(path)):
            env["doc"].save()
        problems = check_file(path)
        assert not problems, (name, problems)
        listings.append(HwpDoc.open(path).read(show_format=True))
    listing = "\n".join(listings)
    for needle in ("tabs right 150mm dot", "line fixed 18pt", "border/fill", "footnote:", "link -> https://kostat.go.kr",
                   "<equation:", "부록"):
        assert needle in listing, f"missing {needle!r}"
    print(f"\nALL {len(blocks())} SKILL RECIPES RAN ->", work)


if __name__ == "__main__":
    main()
