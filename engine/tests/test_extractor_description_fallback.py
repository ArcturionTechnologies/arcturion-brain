"""Hermetic test (no pytest) for the markdown extractor's summary fallback chain.

The fleet-wide memory format writes its retrieval hook in `description:` frontmatter;
`summary:` is rare. Ranking weights node.summary at 2.0/term (title 6.0, body 0.5), so
an extractor that ignores `description:` silently discards every hand-written retrieval
description and substitutes a first-paragraph scrape — often a heading or boilerplate.

Guards the chain: summary: → description: → first_paragraph(body).

  1. `summary:` present → used verbatim (description must NOT override it);
  2. no `summary:` but `description:` present → description becomes node summary;
  3. neither → first_paragraph fallback (existing behavior unchanged);
  4. non-string description (YAML list/dict) → coerced to str, never a crash.

Run:  python3 -m engine.tests.test_extractor_description_fallback
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..extractors.markdown import extract_markdown

FAILURES = []


def check(name: str, cond: bool, detail: str = ""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _extract(tmp: Path, text: str) -> dict:
    f = tmp / "node_under_test.md"
    f.write_text(text, encoding="utf-8")
    node, _edges = extract_markdown(f)
    return node


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        n = _extract(tmp, "---\nsummary: explicit summary wins\ndescription: must not override\n---\n\nBody paragraph here.\n")
        check("summary frontmatter wins over description",
              n["summary"] == "explicit summary wins", repr(n["summary"]))

        n = _extract(tmp, "---\nname: some-memory\ndescription: the hand-written retrieval hook\n---\n\nFirst body paragraph that should NOT be the summary.\n")
        check("description used when summary absent",
              n["summary"] == "the hand-written retrieval hook", repr(n["summary"]))

        n = _extract(tmp, "---\nname: bare\n---\n\nFallback paragraph text.\n")
        check("first_paragraph fallback when neither present",
              n["summary"] == "Fallback paragraph text.", repr(n["summary"]))

        n = _extract(tmp, "---\ndescription:\n  - part one\n  - part two\n---\n\nBody.\n")
        check("non-string description coerced, no crash",
              isinstance(n["summary"], str) and n["summary"], repr(n["summary"]))

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
