"""Code extractor — Python AST imports + shell source lines + JS imports."""

from __future__ import annotations
import ast
import re
from pathlib import Path
from datetime import datetime
from .. import config as C
from ..util import make_node_id, content_hash, first_paragraph, truncate, now_iso
from .markdown import _rel_to_universe


_SHELL_SOURCE_RE = re.compile(r"^\s*(?:source|\.)\s+([^\s;]+)", re.MULTILINE)
_SHELL_CALL_RE = re.compile(
    r"^(?:\s*[A-Za-z0-9_\-/\.]+=\S+\s+)*\s*([A-Za-z][A-Za-z0-9_\-]{1,40}(?:\.sh|\.py)?)\b",
    re.MULTILINE,
)
_JS_IMPORT_RE = re.compile(
    r"""(?:^|\n)\s*(?:import\s+[^'"]*\s+from\s+|import\s+|require\s*\(\s*)['"]([^'"]+)['"]""",
)
_PY_SHEBANG = re.compile(r"^#!.*\bpython", re.IGNORECASE)


def _bare_code_node(path: Path, node_type: str, body: str, raw: bytes) -> dict:
    stat = path.stat()
    rel = _rel_to_universe(path)
    title = path.name
    return {
        "id": make_node_id(path, title),
        "type": node_type,
        "family": "code",
        "title": title,
        "path": rel,
        "owner": None,
        "status": "active",
        "governance_tier": 0,
        "content_hash": content_hash(raw),
        "summary": first_paragraph(_extract_docstring(body) or body, 250),
        "body_excerpt": truncate(body, C.MAX_BODY_CHARS),
        "centrality": 0.0,
        "created": None,
        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "last_reviewed": None,
        "metadata": {"size_bytes": stat.st_size, "executable": bool(stat.st_mode & 0o111)},
    }


def _extract_docstring(text: str) -> str | None:
    # Crude: triple-quoted block at start of file
    s = text.lstrip()
    for q in ('"""', "'''"):
        if s.startswith(q):
            end = s.find(q, 3)
            if end > 0:
                return s[3:end].strip()
    return None


def _python_imports(text: str) -> list[str]:
    imports: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
    return imports


def _python_calls(text: str) -> list[str]:
    """Surface top-level function call names (lightweight, not exhaustive)."""
    calls: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                calls.add(f.id)
            elif isinstance(f, ast.Attribute):
                calls.add(f.attr)
    return sorted(calls)[:50]  # cap per file


def extract_code(path: Path, default_type: str = "Module") -> tuple[dict, list[dict]]:
    try:
        raw = path.read_bytes()
    except Exception:
        return _bare_code_node(path, default_type, "", b""), []
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = ""

    ext = path.suffix.lower()
    node_type = default_type
    if ext == ".py" or _PY_SHEBANG.match(text or ""):
        node_type = "Module" if "/lib/" in str(path) or "test_" not in path.name else "Script"
        if path.stat().st_mode & 0o111:
            node_type = "Script"
    elif ext in {".sh", ".bash", ".zsh"}:
        node_type = "Script"
    elif ext in {".js", ".ts", ".jsx", ".tsx"}:
        node_type = "Module"

    node = _bare_code_node(path, node_type, text, raw)
    edges: list[dict] = []
    asserted_at = now_iso()
    node_id = node["id"]

    if ext == ".py":
        from ..code_references import analyze
        node["metadata"]["python_analysis"] = analyze(text, node["path"])
        # Resolution runs after admitted nodes have entered the same transaction.
    elif ext in {".js", ".ts", ".jsx", ".tsx"}:
        for m in _JS_IMPORT_RE.finditer(text):
            mod = m.group(1)
            edges.append({
                "from": node_id, "to": mod,
                "predicate": "imports", "family": "code",
                "weight": 1.0, "confidence": 0.9,
                "asserted_by": "STATIC_REGEX", "asserted_at": asserted_at,
                "evidence_span": "js-import", "metadata": {},
            })
    elif ext in {".sh", ".bash", ".zsh"}:
        for m in _SHELL_SOURCE_RE.finditer(text):
            sourced = m.group(1)
            edges.append({
                "from": node_id, "to": sourced,
                "predicate": "imports", "family": "code",
                "weight": 1.0, "confidence": 0.9,
                "asserted_by": "STATIC_REGEX", "asserted_at": asserted_at,
                "evidence_span": "sh-source", "metadata": {},
            })

    return node, edges
