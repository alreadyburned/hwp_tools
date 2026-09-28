"""Loading, validating and saving documents.

Every tool call names a file path; there are no open/close handles. Parsed
documents are cached by path and invalidated when the file changes on disk.
A mutation is validated (OWPML schema + Hancom open-safety) before anything is
written, and the previous bytes are kept so the edit can be undone.
"""

from __future__ import annotations

import os
import tempfile
import threading
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import hwpx
from hwpx import HwpxDocument

UNDO_DEPTH = 30
SUPPORTED_SUFFIXES = (".hwpx", ".hwp")


class ToolError(Exception):
    """An error whose message is shown to the model as the tool result."""


def normalize_path(path: str) -> str:
    if not path or not path.strip():
        raise ToolError("path is empty. Pass the absolute path of a .hwpx or .hwp file.")
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    if p.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ToolError(f"{p.name}: only .hwpx and .hwp files are supported.")
    return str(p)


def _signature(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size


def _write_atomically(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".~hwpmcp-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.replace(tmp, path)
        except PermissionError as exc:
            raise ToolError(
                f"Cannot write {os.path.basename(path)}: the file is locked. "
                "Close it in Hancom Office (or any other program) and retry."
            ) from exc
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Store:
    def __init__(self) -> None:
        self._docs: dict[str, tuple[tuple[int, int], HwpxDocument]] = {}
        self._undo: dict[str, list[bytes | None]] = {}
        self._lock = threading.RLock()

    # -- reading -----------------------------------------------------------
    def open(self, path: str) -> HwpxDocument:
        with self._lock:
            if not os.path.exists(path):
                raise ToolError(
                    f"File not found: {path}. Use hwp_create_document to make a new document."
                )
            sig = _signature(path)
            cached = self._docs.get(path)
            if cached and cached[0] == sig:
                return cached[1]
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    doc = HwpxDocument.open(path)
            except Exception as exc:  # noqa: BLE001 - surfaced to the model
                raise ToolError(f"Cannot open {os.path.basename(path)}: {exc}") from exc
            self._docs[path] = (sig, doc)
            return doc

    def conversion_warnings(self, doc: HwpxDocument) -> list[str]:
        report = getattr(doc, "conversion_report", None)
        if not report:
            return []
        out = []
        for label, items in (("not converted", report.unconverted), ("dropped", report.dropped)):
            for key, count in dict(items).items():
                out.append(f"{key}: {count} {label}")
        return out

    # -- writing -----------------------------------------------------------
    def _serialize(self, path: str, doc: HwpxDocument) -> bytes:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            data = doc.to_bytes()
            report = hwpx.validate_editor_open_safety(data)
            problems = [str(e) for e in report.blocking_package_errors]
            if report.document_validation_error:
                problems.append(str(report.document_validation_error))
            if not report.reopen_ok:
                problems.append(f"reopen failed: {report.reopen_error}")
            if problems:
                raise ToolError(
                    "Edit rejected (nothing was written): the result would not open cleanly "
                    "in Hancom Office: " + "; ".join(problems)
                )
            if path.lower().endswith(".hwp"):
                data = doc.to_bytes(format="hwp")
        return data

    def save(self, path: str, doc: HwpxDocument) -> None:
        with self._lock:
            try:
                data = self._serialize(path, doc)
            except ToolError:
                self._docs.pop(path, None)
                raise
            except Exception as exc:  # noqa: BLE001
                self._docs.pop(path, None)
                raise ToolError(f"Edit rejected (nothing was written): {exc}") from exc
            previous = Path(path).read_bytes() if os.path.exists(path) else None
            _write_atomically(path, data)
            stack = self._undo.setdefault(path, [])
            stack.append(previous)
            del stack[:-UNDO_DEPTH]
            self._docs[path] = (_signature(path), doc)

    @contextmanager
    def edit(self, path: str) -> Iterator[HwpxDocument]:
        """Yield the document for mutation and save it if the block succeeds.

        If the block raises, the cached (partially mutated) model is dropped
        and the file on disk is left untouched.
        """
        with self._lock:
            doc = self.open(path)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    yield doc
            except BaseException:
                self._docs.pop(path, None)
                raise
            self.save(path, doc)

    def create(self, path: str, doc: HwpxDocument) -> None:
        with self._lock:
            self.save(path, doc)

    def undo(self, path: str) -> str:
        with self._lock:
            stack = self._undo.get(path)
            if not stack:
                raise ToolError(
                    "Nothing to undo for this file in the current session "
                    "(undo history only covers edits made through these tools since the server started)."
                )
            previous = stack.pop()
            self._docs.pop(path, None)
            if previous is None:
                os.remove(path)
                return "Undone: the file had been created by the last edit, so it was deleted."
            _write_atomically(path, previous)
            return f"Undone. {len(stack)} more undo step(s) available for this file."

    def forget(self, path: str) -> None:
        with self._lock:
            self._docs.pop(path, None)
