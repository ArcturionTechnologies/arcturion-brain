"""Focused regression test for scalar owner template placeholders."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..extractors.markdown import extract_markdown


def _owner_edges(tmp: Path, frontmatter: str) -> list[dict]:
    path = tmp / "owner-fixture.md"
    path.write_text(f"---\n{frontmatter}\n---\n\nFixture body.\n", encoding="utf-8")
    _node, edges = extract_markdown(path)
    return [e for e in edges if e["predicate"] == "owned_by"]


def main() -> int:
    failures = []
    placeholders = [
        "AGENT_NAME", "<AGENT NAME>", "<AGENT>", "<AGENT_NAME>", "<OWNER>",
    ]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for token in placeholders:
            rendered = f"  {token.lower()}  "
            edges = _owner_edges(tmp, f'owner: "{rendered}"')
            ok = not edges
            print(f"[{'PASS' if ok else 'FAIL'}] scalar placeholder suppressed: {token}")
            if not ok:
                failures.append(token)

        edges = _owner_edges(tmp, "owner: COVENANT")
        ok = len(edges) == 1 and edges[0]["to"] == "COVENANT" and edges[0]["evidence_span"] == "frontmatter:owner"
        print(f"[{'PASS' if ok else 'FAIL'}] real scalar owner preserved")
        if not ok:
            failures.append("real scalar owner")

        edges = _owner_edges(tmp, "owned_by:\n  - AGENT_NAME\n  - COVENANT\nowner: COVENANT")
        ok = [e["to"] for e in edges] == ["AGENT_NAME", "COVENANT"] and all(e["evidence_span"] == "frontmatter:owned_by" for e in edges)
        print(f"[{'PASS' if ok else 'FAIL'}] explicit owned_by list retains typed-edge semantics")
        if not ok:
            failures.append("explicit owned_by list")

    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
