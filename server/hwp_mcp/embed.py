"""Optional meaning-based (semantic) search: text embeddings from a small local model ("builtin")
or an Ollama server, combined with the keyword search in reader.search.

Off unless configured. The VS Code extension writes ~/.hwp-mcp/settings.json:
    {"embedding": {"provider": "builtin" | "ollama" | "none", "ollama_url": "...", "ollama_model": "..."}}
Environment variables override it: HWP_MCP_EMBEDDING, HWP_MCP_OLLAMA_URL, HWP_MCP_OLLAMA_MODEL
(and HWP_MCP_HOME moves ~/.hwp-mcp). Settings are re-read on every search, so no restart is needed.

    python -m hwp_mcp.embed prepare     install the runtime and download the model (progress on stdout)
    python -m hwp_mcp.embed status      one line: ready, or why not

Vectors are cached in ~/.hwp-mcp/cache/vectors.sqlite by model and text hash, so a document is
embedded once and later searches (and edits) only embed paragraphs whose text changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# multilingual-e5-small (MIT), int8 ONNX export from the official repository, pinned by revision and hash.
BUILTIN = {
    "id": "multilingual-e5-small-int8",
    "base": "https://huggingface.co/intfloat/multilingual-e5-small/resolve/614241f622f53c4eeff9890bdc4f31cfecc418b3/",
    "files": {
        "model.onnx": ("onnx/model_qint8_avx512_vnni.onnx", 118346824,
                       "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
        "tokenizer.json": ("onnx/tokenizer.json", 17082730,
                           "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
    },
}
RUNTIME = ["onnxruntime>=1.20", "tokenizers>=0.20", "numpy>=1.26"]
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "bge-m3"
BATCH = 32


def home() -> Path:
    return Path(os.environ.get("HWP_MCP_HOME") or Path.home() / ".hwp-mcp")


def settings() -> dict[str, str]:
    conf: dict[str, Any] = {}
    try:
        conf = json.loads((home() / "settings.json").read_text(encoding="utf-8")).get("embedding", {})
    except (OSError, ValueError):
        pass
    return {
        "provider": (os.environ.get("HWP_MCP_EMBEDDING") or conf.get("provider") or "none").lower(),
        "ollama_url": (os.environ.get("HWP_MCP_OLLAMA_URL") or conf.get("ollama_url") or OLLAMA_URL).rstrip("/"),
        "ollama_model": os.environ.get("HWP_MCP_OLLAMA_MODEL") or conf.get("ollama_model") or OLLAMA_MODEL,
    }


class NotReady(Exception):
    """Semantic search is configured but cannot run (message says why and how to fix it)."""


# ---------------------------------------------------------------------------
# embedders
# ---------------------------------------------------------------------------
class BuiltinEmbedder:
    def __init__(self) -> None:
        self.dir = home() / "models" / BUILTIN["id"]
        self.name = BUILTIN["id"]
        if not (self.dir / "ready.json").exists():
            raise NotReady("the built-in embedding model is not downloaded yet "
                           "(VS Code: command \"hwp_tools: 임베딩 모델 준비\", or python -m hwp_mcp.embed prepare)")
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise NotReady(f"the embedding runtime is not installed ({exc.name}); run python -m hwp_mcp.embed prepare") from exc
        self.np = np
        self.tok = Tokenizer.from_file(str(self.dir / "tokenizer.json"))
        self.tok.enable_truncation(512)
        self.tok.enable_padding()
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(self.dir / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def embed(self, texts: list[str], kind: str) -> list:
        np = self.np
        prefix = "query: " if kind == "query" else "passage: "  # e5 is trained with these prefixes
        out = []
        for i in range(0, len(texts), BATCH):
            enc = self.tok.encode_batch([prefix + t for t in texts[i:i + BATCH]])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feed)[0]
            v = (hidden * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)  # mean pooling
            out.extend(v / np.linalg.norm(v, axis=1, keepdims=True))
        return out


class OllamaEmbedder:
    def __init__(self, url: str, model: str) -> None:
        try:
            import numpy as np
        except ImportError as exc:
            raise NotReady("numpy is not installed; run python -m hwp_mcp.embed prepare") from exc
        self.np, self.url, self.model = np, url, model
        self.name = f"ollama:{model}"

    def embed(self, texts: list[str], kind: str) -> list:
        np = self.np
        out = []
        for i in range(0, len(texts), BATCH):
            try:
                data = _post(f"{self.url}/api/embed", {"model": self.model, "input": texts[i:i + BATCH]}, timeout=120)
            except urllib.error.HTTPError as exc:
                raise NotReady(f"Ollama could not embed with {self.model!r} ({exc.code}): "
                               f"is it pulled? run: ollama pull {self.model}") from exc
            except (urllib.error.URLError, OSError) as exc:
                raise NotReady(f"Ollama is not reachable at {self.url} ({exc})") from exc
            for vec in data["embeddings"]:
                v = np.asarray(vec, dtype=np.float32)
                out.append(v / (np.linalg.norm(v) or 1))
        return out


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


_embedder: tuple[tuple, Any] | None = None
_override: Any = None  # tests inject an embedder here


def get_embedder():
    """The configured embedder, or None when semantic search is off. Raises NotReady."""
    global _embedder
    if _override is not None:
        return _override
    conf = settings()
    provider = conf["provider"]
    if provider in ("", "none", "off"):
        return None
    key = (provider, conf["ollama_url"], conf["ollama_model"])
    if _embedder and _embedder[0] == key:
        return _embedder[1]
    if provider == "builtin":
        emb = BuiltinEmbedder()
    elif provider == "ollama":
        emb = OllamaEmbedder(conf["ollama_url"], conf["ollama_model"])
    else:
        raise NotReady(f'unknown embedding provider "{provider}" (use none, builtin or ollama)')
    _embedder = (key, emb)
    return emb


# ---------------------------------------------------------------------------
# vector cache + background indexing
# ---------------------------------------------------------------------------
def text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class VectorCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS vec (model TEXT, key TEXT, data BLOB, PRIMARY KEY (model, key))")
        self.lock = threading.Lock()

    def get(self, model: str, keys: list[str]) -> dict[str, bytes]:
        found: dict[str, bytes] = {}
        with self.lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                rows = self.db.execute(
                    f"SELECT key, data FROM vec WHERE model = ? AND key IN ({','.join('?' * len(chunk))})", [model, *chunk])
                found.update(rows.fetchall())
        return found

    def put(self, model: str, items: list[tuple[str, bytes]]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?, ?)", [(model, k, d) for k, d in items])
            self.db.commit()


_cache: VectorCache | None = None
_pending: dict[str, str] = {}   # key -> text still to embed in the background
_worker: threading.Thread | None = None
_state_lock = threading.Lock()
_last_error: str | None = None


def cache() -> VectorCache:
    global _cache
    if _cache is None:
        _cache = VectorCache(home() / "cache" / "vectors.sqlite")
    return _cache


def _embed_and_store(emb, texts: dict[str, str]) -> None:
    keys = list(texts)
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        vecs = emb.embed([texts[k] for k in chunk], "passage")
        cache().put(emb.name, [(k, v.astype("float32").tobytes()) for k, v in zip(chunk, vecs)])


def _background(emb) -> None:
    global _worker, _last_error
    while True:
        with _state_lock:
            batch = dict(list(_pending.items())[:BATCH * 4])
            if not batch:
                _worker = None
                return
        try:
            _embed_and_store(emb, batch)
            _last_error = None
        except Exception as exc:  # noqa: BLE001 - reported on the next search
            _last_error = str(exc)
            with _state_lock:
                _pending.clear()
                _worker = None
            return
        with _state_lock:
            for k in batch:
                _pending.pop(k, None)


def _queue(emb, texts: dict[str, str]) -> None:
    global _worker
    with _state_lock:
        _pending.update(texts)
        if _worker is None and _pending:
            _worker = threading.Thread(target=_background, args=(emb,), daemon=True, name="hwp-embed")
            _worker.start()


def warm(texts: list[str]) -> None:
    """Start embedding a document's texts in the background (no-op when semantic search is off)."""
    try:
        emb = get_embedder()
    except NotReady:
        return
    if emb is None:
        return
    keys = {text_key(t): t for t in texts if t.strip()}
    have = cache().get(emb.name, list(keys))
    missing = {k: t for k, t in keys.items() if k not in have}
    if missing:
        _queue(emb, missing)


def similarities(texts: list[str], query: str, wait: float = 8.0) -> tuple[list[float | None] | None, str]:
    """Cosine similarity of each text to the query, None for texts not embedded yet.

    Embeds missing texts for up to ``wait`` seconds, then leaves the rest to a background
    thread. Returns (None, reason) when semantic search is off or not ready."""
    global _last_error
    try:
        emb = get_embedder()
    except NotReady as exc:
        return None, f"meaning search unavailable: {exc}"
    if emb is None:
        return None, ""
    earlier, _last_error = _last_error, None  # report a background failure once, then retry
    np = emb.np
    keys = [text_key(t) for t in texts]
    have = cache().get(emb.name, keys)
    missing = {k: t for k, t in zip(keys, texts) if k not in have and t.strip()}
    try:
        if missing:
            deadline = time.monotonic() + wait
            todo = list(missing.items())
            while todo and time.monotonic() < deadline:
                part, todo = dict(todo[:BATCH]), todo[BATCH:]
                _embed_and_store(emb, part)
            if todo:
                _queue(emb, dict(todo))
            have = cache().get(emb.name, keys)
        q = emb.embed([query], "query")[0]
    except Exception as exc:  # noqa: BLE001 - fall back to keyword search, say why
        return None, f"meaning search unavailable: {exc}"
    scores: list[float | None] = []
    for k in keys:
        blob = have.get(k)
        scores.append(float(np.frombuffer(blob, dtype=np.float32) @ q) if blob else None)
    done = sum(s is not None for s in scores) / max(1, sum(bool(t.strip()) for t in texts))
    notes = [] if done >= 0.999 else [f"meaning index covers {done:.0%} of the document so far; indexing continues"]
    if earlier:
        notes.append(f"background indexing had failed ({earlier}); retried")
    return scores, "; ".join(notes)


# ---------------------------------------------------------------------------
# prepare / status (run by the VS Code extension)
# ---------------------------------------------------------------------------
def _progress(pct: float, msg: str) -> None:
    print(f"PROGRESS {pct:.0f} {msg}", flush=True)


def _pip_install(packages: list[str]) -> None:
    missing = []
    for spec in packages:
        name = spec.split(">")[0].split("=")[0]
        try:
            __import__(name)
        except ImportError:
            missing.append(spec)
    if missing:
        _progress(2, "installing " + ", ".join(missing))
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing], check=True)


def _download(url: str, dest: Path, size: int, sha256: str, start: float, span: float) -> None:
    if dest.exists() and dest.stat().st_size == size and _sha256(dest) == sha256:
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    got = 0
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            fh.write(block)
            h.update(block)
            got += len(block)
            _progress(start + span * got / size, f"downloading {dest.name} {got >> 20}/{size >> 20} MB")
    if got != size or h.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{dest.name}: download is corrupt (size {got}, sha256 {h.hexdigest()})")
    os.replace(tmp, dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def prepare() -> str:
    conf = settings()
    provider = conf["provider"]
    if provider == "builtin":
        _pip_install(RUNTIME)
        d = home() / "models" / BUILTIN["id"]
        d.mkdir(parents=True, exist_ok=True)
        files = list(BUILTIN["files"].items())
        for n, (local, (remote, size, sha)) in enumerate(files):
            _download(BUILTIN["base"] + remote, d / local, size, sha, 5 + 90 * n / len(files), 90 / len(files))
        (d / "ready.json").write_text(json.dumps({"base": BUILTIN["base"]}), encoding="utf-8")
    elif provider == "ollama":
        _pip_install(["numpy>=1.26"])
        url, model = conf["ollama_url"], conf["ollama_model"]
        try:
            with urllib.request.urlopen(f"{url}/api/tags", timeout=10) as resp:
                tags = json.loads(resp.read())
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"Ollama is not reachable at {url}: install and start Ollama first ({exc})") from exc
        names = {m.get("name", "") for m in tags.get("models", [])}
        if model not in names and f"{model}:latest" not in names:
            req = urllib.request.Request(f"{url}/api/pull", json.dumps({"model": model, "stream": True}).encode(),
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3600) as resp:
                for line in resp:
                    ev = json.loads(line)
                    if ev.get("error"):
                        raise RuntimeError(f"ollama pull {model}: {ev['error']}")
                    if ev.get("total"):
                        _progress(5 + 90 * ev.get("completed", 0) / ev["total"], f"ollama pull {model}: {ev.get('status', '')}")
    else:
        return "Semantic search is off (provider none); nothing to prepare."
    global _embedder
    _embedder = None
    emb = get_embedder()
    emb.embed(["준비 확인"], "query")  # load and run once
    _progress(100, "ready")
    return f"Semantic search ready: {emb.name}"


def status() -> str:
    try:
        emb = get_embedder()
        if emb is None:
            return "off"
        emb.embed(["확인"], "query")  # really usable: model present (Ollama may lack it), runtime loads
    except NotReady as exc:
        return f"not ready: {exc}"
    return f"ready: {emb.name}"


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        print(prepare() if cmd == "prepare" else status())
    except Exception as exc:  # noqa: BLE001 - shown to the user by the extension
        print(f"ERROR {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
