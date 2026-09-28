"""Print an approximate HTML preview of a .hwpx/.hwp file to stdout (used by the VS Code extension).

Usage: python -m hwp_mcp.preview <file>
"""

from __future__ import annotations

import sys
import warnings

import hwpx
from hwpx import HwpxDocument


def render(path: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        source: str | bytes = path
        if path.lower().endswith(".hwp"):
            source = HwpxDocument.open(path).to_bytes()
        return hwpx.render_document_viewer(source, title=path.replace("\\", "/").rsplit("/", 1)[-1]).html


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m hwp_mcp.preview <file.hwpx|file.hwp>")
    html = render(sys.argv[1])
    sys.stdout.buffer.write(html.encode("utf-8"))


if __name__ == "__main__":
    main()
