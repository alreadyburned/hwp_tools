"""Validate a Hangul document and show its structure.

Usage: python -m hwp_mcp.check <file.hwpx|file.hwp> [--png out.png] [--format] [--quiet]

Exit code 0 = the file passes the OWPML schema and Hancom open-safety checks, 1 = problems.
"""

from __future__ import annotations

import argparse
import sys
import warnings

import hwpx

from .api import HwpDoc
from .store import ToolError


def check_file(path: str) -> list[str]:
    problems: list[str] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if path.lower().endswith(".hwpx"):
            report = hwpx.validate_editor_open_safety(path)
            problems += [str(e) for e in report.blocking_package_errors]
            if report.document_validation_error:
                problems.append(str(report.document_validation_error))
            if not report.reopen_ok:
                problems.append(f"reopen failed: {report.reopen_error}")
        else:
            problems += HwpDoc.open(path).check()
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m hwp_mcp.check")
    ap.add_argument("file")
    ap.add_argument("--png", help="also render an approximate preview image to this path")
    ap.add_argument("--format", action="store_true", help="show paragraph/character format per paragraph")
    ap.add_argument("--quiet", action="store_true", help="only print the verdict")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        problems = check_file(args.file)
        doc = HwpDoc.open(args.file)
    except ToolError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
    if problems:
        print("FAIL:\n  - " + "\n  - ".join(problems))
    else:
        print("OK: valid, safe to open in Hancom Office.")
    if not args.quiet:
        print(doc.read(show_format=args.format))
    if args.png:
        try:
            print("preview:", doc.preview_png(args.png))
        except ToolError as exc:
            print(f"preview not available: {exc}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
