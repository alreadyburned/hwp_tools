"""Check meaning-based search (hwp_mcp.embed + reader.search): fusion with keyword search, the
vector cache, background indexing, "not ready" fallbacks, and the Ollama provider against a fake
Ollama server. With --real, also download the built-in model and search a generated document.

Needs numpy (installed when embedding is enabled). Run:
    <python with numpy> tests/embed_test.py [workdir] [--real]     (PYTHONPATH=server)
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import time
import warnings

import numpy as np
from hwpx import HwpxDocument

warnings.filterwarnings("ignore")
HOME = tempfile.mkdtemp(prefix="hwp-embed-home-")
os.environ["HWP_MCP_HOME"] = HOME  # never touch the real ~/.hwp-mcp

from hwp_mcp import embed, ops, reader  # noqa: E402
from hwp_mcp.store import Store  # noqa: E402

# Toy "meaning": each concept is one dimension; words of a concept share it.
CONCEPTS = [("예산", "재정", "비용", "돈", "재원"), ("해외", "외국", "수출", "동남아"), ("보안", "해킹", "정보보호"),
            ("어린이집", "육아", "보육"), ("인력", "채용", "증원"), ("전기차", "친환경", "탄소")]


def toy_vector(text: str) -> np.ndarray:
    v = np.full(len(CONCEPTS) + 1, 0.01, dtype=np.float32)
    for d, words in enumerate(CONCEPTS):
        v[d] += sum(text.count(w) for w in words)
    return v / np.linalg.norm(v)


class ToyEmbedder:
    name = "toy"
    np = np

    def __init__(self) -> None:
        self.calls = 0
        self.texts = 0

    def embed(self, texts, kind):
        self.calls += 1
        self.texts += len(texts)
        return [toy_vector(t) for t in texts]


def make_doc(path: str) -> str:
    doc = HwpxDocument.new()
    for text in ["2026년 업무 계획", "Ⅰ. 추진 배경", "올해 예산 집행률은 92%로 목표를 넘었다.",
                 "동남아 시장 진출을 위한 현지 법인 설립을 검토한다.", "Ⅱ. 추진 계획",
                 "전 직원 대상 정보보호 교육을 연 2회 실시한다.", "사내 어린이집 정원을 40명으로 늘린다.",
                 "공용차량 30%를 전기차로 교체한다."]:
        doc.sections[-1].add_paragraph(text)
    with open(path, "wb") as fh:
        fh.write(doc.to_bytes())
    return path


def check_off(store: Store, path: str) -> None:
    out = reader.search(store, path, "재정 상황")
    print(out)
    assert "No match" in out and "Note" not in out and "unavailable" not in out, out


def check_not_ready(store: Store, path: str) -> None:
    os.environ["HWP_MCP_EMBEDDING"] = "builtin"
    out = reader.search(store, path, "예산")
    print(out)
    assert "(keywords)" in out and "built-in embedding model is not downloaded" in out, out
    assert "p3 " in out, "keyword results still come back"
    os.environ["HWP_MCP_EMBEDDING"] = "ollama"
    os.environ["HWP_MCP_OLLAMA_URL"] = "http://127.0.0.1:9"  # nothing listens there
    out = reader.search(store, path, "예산")
    assert "Ollama is not reachable" in out and "p3 " in out, out
    del os.environ["HWP_MCP_EMBEDDING"], os.environ["HWP_MCP_OLLAMA_URL"]
    embed._embedder = None


def check_toy(store: Store, path: str) -> None:
    toy = ToyEmbedder()
    embed._override = toy
    out = reader.search(store, path, "재정 상황")
    print(out)
    assert "(keywords + meaning)" in out and "~ related by meaning only" in out, out
    assert out.splitlines()[1].startswith("~p3 [s1 "), out  # 예산 paragraph, no shared keyword
    both = reader.search(store, path, "예산 집행")
    assert both.splitlines()[1].startswith("*p3 "), both
    first = toy.texts
    reader.search(store, path, "해외 진출")
    assert toy.texts == first + 1, "cached: only the query is embedded again"
    ops.set_paragraph_text(store, path, "p6", "사내 보육 시설 정원을 40명으로 늘린다.")
    out = reader.search(store, path, "육아 지원")
    assert toy.texts == first + 1 + 1 + 1, f"one changed paragraph + the query ({toy.texts - first})"
    assert out.splitlines()[1].lstrip("~* ").startswith("p6 "), out

    # background indexing: nothing embedded inline, the thread finishes the rest
    embed.cache().db.execute("DELETE FROM vec")
    scores, note = embed.similarities(["예산 " + str(i) for i in range(200)], "재정", wait=0)
    assert "covers 0%" in note and all(s is None for s in scores), note
    for _ in range(100):
        if embed._worker is None:
            break
        time.sleep(0.05)
    scores, note = embed.similarities(["예산 " + str(i) for i in range(200)], "재정", wait=0)
    assert note == "" and all(s is not None for s in scores), note
    embed._override = None


class FakeOllama(http.server.BaseHTTPRequestHandler):
    pulled = False

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        names = [{"name": "bge-m3:latest"}] if FakeOllama.pulled else [{"name": "llama3:latest"}]
        self._json({"models": names})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/pull":
            self.send_response(200)
            self.end_headers()
            for done in (0, 50, 100):
                self.wfile.write((json.dumps({"status": "pulling", "total": 100, "completed": done}) + "\n").encode())
            self.wfile.write(b'{"status": "success"}\n')
            FakeOllama.pulled = True
        elif self.path == "/api/embed":
            if not FakeOllama.pulled or body["model"] != "bge-m3":
                self._json({"error": "model not found"}, 404)
            else:
                self._json({"embeddings": [toy_vector(t).tolist() for t in body["input"]]})


def check_ollama(store: Store, path: str) -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with open(os.path.join(HOME, "settings.json"), "w", encoding="utf-8") as fh:
        json.dump({"embedding": {"provider": "ollama", "ollama_url": f"http://127.0.0.1:{server.server_port}"}}, fh)
    embed._embedder = None
    out = reader.search(store, path, "재정")
    assert "could not embed with 'bge-m3' (404)" in out and "ollama pull bge-m3" in out, out
    print(embed.prepare())
    assert FakeOllama.pulled
    out = reader.search(store, path, "재정")
    print(out)
    assert out.splitlines()[1].startswith("~p3 "), out
    assert embed.status() == "ready: ollama:bge-m3"
    server.shutdown()
    os.remove(os.path.join(HOME, "settings.json"))
    embed._embedder = None


def check_real_builtin(work: str) -> None:
    from reader_test import big_document  # the generated ~110k-character document
    os.environ["HWP_MCP_EMBEDDING"] = "builtin"
    embed._embedder = None
    t = time.perf_counter()
    print(embed.prepare())
    print(f"[prepare {time.perf_counter() - t:.1f}s]")
    assert embed.status() == "ready: multilingual-e5-small-int8"
    store = Store()
    small = make_doc(os.path.join(work, "real_small.hwpx"))
    out = reader.search(store, small, "해킹 대비")
    print(out)
    assert out.splitlines()[1].lstrip("~* ").startswith("p6 "), out
    big = big_document(os.path.join(work, "real_big.hwpx"))
    t = time.perf_counter()
    out = reader.search(store, big, "국내 기술로 만든 칩 제조 설비")
    took = time.perf_counter() - t
    print(out[:600], f"\n[first search incl. indexing {took:.1f}s]")
    t = time.perf_counter()
    out = reader.search(store, big, "국내 기술로 만든 칩 제조 설비")
    print(f"[second search {time.perf_counter() - t:.2f}s]")
    assert "Note" not in out and "p435 " in "\n".join(out.splitlines()[:4]), out
    del os.environ["HWP_MCP_EMBEDDING"]


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    work = args[0] if args else tempfile.mkdtemp(prefix="hwpembed-")
    os.makedirs(work, exist_ok=True)
    store = Store()
    path = make_doc(os.path.join(work, "embed.hwpx"))
    check_off(store, path)
    check_not_ready(store, path)
    check_toy(store, path)
    check_ollama(store, path)
    if "--real" in sys.argv:
        sys.path.insert(0, os.path.dirname(__file__))
        check_real_builtin(work)
    print("\nALL EMBED CHECKS PASSED ->", work)


if __name__ == "__main__":
    main()
